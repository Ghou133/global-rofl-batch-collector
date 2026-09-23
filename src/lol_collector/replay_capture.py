from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from lol_collector.artifacts import RawArtifactStore
from lol_collector.collection_runtime import RuntimeSession
from lol_collector.collection_support import parse_match_summary
from lol_collector.models import (
    ArtifactType,
    JsonValue,
    ReplayDownloadStatus,
    ReplayValidationStatus,
    normalize_patch,
)
from lol_collector.replay_archive import (
    ReplayArchiveResult,
    ReplayCandidate,
    archive_replay,
    default_replay_source_dirs,
    discover_replays,
    replay_archive_path,
    validate_replay,
)
from lol_collector.replay_store import ReplayScanRegistration
from lol_collector.repository import Repository
from lol_collector.transport import HttpResponse, HttpTransportError


@dataclass(frozen=True, slots=True)
class ReplayCaptureResult:
    game_id: str
    details_exists: bool
    download_status: str
    validation_status: str
    rofl_path: str | None
    details_path: str | None
    file_size: int | None
    sha256: str | None
    error_code: str | None
    error_message: str | None


@dataclass(frozen=True, slots=True)
class ReplayPairCheck:
    game_id: str
    details_path: str | None
    rofl_path: str | None
    details_exists: bool
    replay_exists: bool
    details_hash_matches: bool
    replay_hash_matches: bool
    replay_valid: bool

    @property
    def valid_pair(self) -> bool:
        return (
            self.details_exists
            and self.replay_exists
            and self.details_hash_matches
            and self.replay_hash_matches
            and self.replay_valid
        )


class ReplayCaptureCoordinator:
    def __init__(
        self,
        repository: Repository,
        data_dir: Path,
        source_dirs: tuple[Path, ...] | None = None,
    ) -> None:
        self.repository = repository
        self.store = repository.replays
        self.data_dir = data_dir.resolve()
        self.archive_root = self.data_dir / "replays"
        self.artifacts = RawArtifactStore(self.data_dir / "collection-artifacts")
        self.source_dirs = source_dirs or default_replay_source_dirs()

    def scan(self, *, include_existing: bool = False) -> ReplayScanRegistration:
        candidates = discover_replays(
            self.source_dirs,
            excluded_root=self.archive_root,
        )
        return self.store.register_scan(
            candidates,
            include_existing=include_existing,
        )

    async def capture_pending(
        self,
        session: RuntimeSession | None,
        limit: int,
    ) -> tuple[ReplayCaptureResult, ...]:
        self.store.recover_interrupted()
        results: list[ReplayCaptureResult] = []
        for candidate in self.store.pending_sources(limit):
            try:
                results.append(await self.capture_one(session, candidate))
            except (OSError, ValueError, sqlite3.Error, RuntimeError) as error:
                self.store.mark_failed(
                    candidate,
                    "CAPTURE_UNEXPECTED_ERROR",
                    type(error).__name__,
                )
                results.append(self._result(candidate.game_id))
        return tuple(results)

    async def capture_remote_pending(
        self,
        session: RuntimeSession | None,
        limit: int,
    ) -> tuple[ReplayCaptureResult, ...]:
        recovered = self.store.recover_interrupted()
        game_ids = self.store.pending_remote_games(
            limit,
            cooldown_seconds=0 if recovered else 30,
        )
        return await self.capture_game_ids(session, game_ids)

    async def capture_game_ids(
        self,
        session: RuntimeSession | None,
        game_ids: tuple[str, ...],
    ) -> tuple[ReplayCaptureResult, ...]:
        results: list[ReplayCaptureResult] = []
        for game_id in game_ids:
            try:
                results.append(await self.capture_game_id(session, game_id))
            except (OSError, ValueError, sqlite3.Error, RuntimeError) as error:
                self._cleanup_remote_staging(game_id)
                source_endpoint = (
                    session.sgp.replay_path(game_id)
                    if session is not None
                    else f"/match-history-query/v3/product/lol/matchId/HN1_{game_id}/infoType/replay"
                )
                self.store.queue_remote_game(game_id, source_endpoint)
                self.store.mark_remote_failed(
                    game_id,
                    "CAPTURE_UNEXPECTED_ERROR",
                    type(error).__name__,
                )
                results.append(self._result(game_id))
        return tuple(results)

    async def capture_game_id(
        self,
        session: RuntimeSession | None,
        game_id: str,
    ) -> ReplayCaptureResult:
        if not game_id.isdecimal():
            raise ValueError("game_id must contain decimal digits only")
        source_endpoint = (
            session.sgp.replay_path(game_id)
            if session is not None
            else f"/match-history-query/v3/product/lol/matchId/HN1_{game_id}/infoType/replay"
        )
        self.store.queue_remote_game(game_id, source_endpoint)
        existing = self.store.get_record(game_id)
        if (
            existing is not None
            and str(existing["download_status"])
            == ReplayDownloadStatus.VALIDATED.value
        ):
            return self._result(game_id)
        if session is None:
            self.store.mark_remote_waiting(
                game_id,
                "CLIENT_UNAVAILABLE",
                "League Client is not running or no authenticated Tencent SGP session is available",
            )
            return self._result(game_id)

        self.store.begin_remote_attempt(game_id)
        self._cleanup_remote_staging(game_id)
        try:
            summary_response = await session.sgp.summary(game_id)
            details_response = await session.sgp.details(game_id)
        except HttpTransportError:
            self.store.mark_remote_waiting(
                game_id,
                "SGP_TRANSPORT_ERROR",
                "Tencent SGP metadata request failed; capture remains queued",
            )
            return self._result(game_id)
        response_error = self._response_error(summary_response, "SUMMARY")
        if response_error is None:
            response_error = self._response_error(details_response, "DETAILS")
        if response_error is not None:
            self._mark_remote_response_error(game_id, response_error)
            return self._result(game_id)

        stage_path = self.data_dir / ".replay-downloads" / f"{game_id}.download"
        try:
            download = await session.sgp.download_replay(game_id, stage_path)
        except (HttpTransportError, AttributeError):
            self.store.mark_remote_waiting(
                game_id,
                "REPLAY_TRANSPORT_ERROR",
                "Replay binary download failed; capture remains queued",
            )
            return self._result(game_id)
        if download.status_code >= 400 or download.path is None:
            self._mark_remote_download_error(game_id, download.status_code)
            return self._result(game_id)
        validation = validate_replay(download.path)
        if not validation.valid:
            self.store.mark_remote_failed(
                game_id,
                validation.error_code or "INVALID_REPLAY",
                validation.error_message or "Downloaded Replay failed validation",
                invalid=True,
                validation=validation,
            )
            download.path.unlink(missing_ok=True)
            return self._result(game_id)
        self.store.record_remote_validation(game_id, validation)

        summary_payload = summary_response.payload
        details_payload = details_response.payload
        summary = parse_match_summary(summary_payload)
        details_game_id = _payload_game_id(details_payload)
        if summary.game_id != game_id or details_game_id != game_id:
            self.store.mark_remote_failed(
                game_id,
                "GAME_ID_MISMATCH",
                f"Expected {game_id}; SUMMARY={summary.game_id or '<missing>'}; DETAILS={details_game_id or '<missing>'}",
            )
            download.path.unlink(missing_ok=True)
            return self._result(game_id)
        replay_patch = normalize_patch(validation.game_version or "")
        summary_patch = normalize_patch(summary.game_version or "")
        if (
            replay_patch is not None
            and summary_patch is not None
            and replay_patch != summary_patch
        ):
            self.store.mark_remote_failed(
                game_id,
                "PATCH_MISMATCH",
                f"Replay patch {replay_patch} does not match SUMMARY patch {summary_patch}",
            )
            download.path.unlink(missing_ok=True)
            return self._result(game_id)

        target = replay_archive_path(
            self.archive_root,
            game_id,
            summary.game_version or validation.game_version,
        )
        archived = archive_replay(download.path, target)
        if not archived.success or archived.target_path is None:
            self.store.mark_remote_failed(
                game_id,
                archived.error_code or "ARCHIVE_FAILED",
                archived.error_message or "Replay archive failed",
                invalid=archived.validation.status is ReplayValidationStatus.INVALID,
                validation=archived.validation,
            )
            download.path.unlink(missing_ok=True)
            return self._result(game_id)
        try:
            summary_artifact = self.artifacts.write_json(
                artifact_type=ArtifactType.SUMMARY,
                owner_type="match",
                owner_id=game_id,
                endpoint_template=f"/match-history-query/v1/products/lol/{{server}}_{game_id}/SUMMARY",
                http_status=summary_response.status_code,
                payload=summary_payload,
            )
            details_artifact = self.artifacts.write_json(
                artifact_type=ArtifactType.DETAILS,
                owner_type="match",
                owner_id=game_id,
                endpoint_template=f"/match-history-query/v1/products/lol/{{server}}_{game_id}/DETAILS",
                http_status=details_response.status_code,
                payload=details_payload,
            )
            self.store.commit_remote_pair(
                game_id=game_id,
                source_endpoint=source_endpoint,
                sgp_server_id=session.server_id,
                platform_id=session.platform,
                summary=summary,
                summary_artifact=summary_artifact,
                details_artifact=details_artifact,
                rofl_path=str(archived.target_path.resolve()),
                validation=archived.validation,
            )
        except (OSError, ValueError) as error:
            self.store.mark_remote_failed(
                game_id,
                "PAIR_COMMIT_FAILED",
                type(error).__name__,
            )
        else:
            download.path.unlink(missing_ok=True)
        return self._result(game_id)

    async def capture_one(
        self,
        session: RuntimeSession | None,
        candidate: ReplayCandidate,
    ) -> ReplayCaptureResult:
        existing = self.store.get_record(candidate.game_id)
        if (
            existing is not None
            and str(existing["download_status"])
            == ReplayDownloadStatus.VALIDATED.value
        ):
            return self._handle_existing(candidate, existing)
        self.store.begin_attempt(candidate)
        source_validation = validate_replay(candidate.source_path)
        if not source_validation.valid:
            self.store.mark_failed(
                candidate,
                source_validation.error_code or "INVALID_REPLAY",
                source_validation.error_message or "Replay source failed validation",
                invalid=True,
                validation=source_validation,
            )
            return self._result(candidate.game_id)
        self.store.record_source_validation(candidate, source_validation)
        if session is None:
            self.store.mark_waiting(
                candidate,
                "CLIENT_UNAVAILABLE",
                "League Client is not running or no authenticated Tencent SGP session is available",
            )
            return self._result(candidate.game_id)
        if (
            candidate.platform_id is not None
            and candidate.platform_id.casefold() != session.platform.casefold()
        ):
            self.store.mark_unavailable(
                candidate,
                "PLATFORM_MISMATCH",
                f"Replay platform {candidate.platform_id} does not match active client platform {session.platform}",
            )
            return self._result(candidate.game_id)

        try:
            summary_response = await session.sgp.summary(candidate.game_id)
            details_response = await session.sgp.details(candidate.game_id)
        except HttpTransportError:
            self.store.mark_waiting(
                candidate,
                "SGP_TRANSPORT_ERROR",
                "Tencent SGP request failed; capture remains queued",
            )
            return self._result(candidate.game_id)

        response_error = self._response_error(summary_response, "SUMMARY")
        if response_error is None:
            response_error = self._response_error(details_response, "DETAILS")
        if response_error is not None:
            error_code, message, unavailable, retryable = response_error
            if unavailable:
                self.store.mark_unavailable(candidate, error_code, message)
            elif retryable:
                self.store.mark_waiting(candidate, error_code, message)
            else:
                self.store.mark_failed(candidate, error_code, message)
            return self._result(candidate.game_id)

        summary_payload = summary_response.payload
        details_payload = details_response.payload
        summary = parse_match_summary(summary_payload)
        details_game_id = _payload_game_id(details_payload)
        if summary.game_id != candidate.game_id or details_game_id != candidate.game_id:
            self.store.mark_failed(
                candidate,
                "GAME_ID_MISMATCH",
                f"Expected {candidate.game_id}; SUMMARY={summary.game_id or '<missing>'}; DETAILS={details_game_id or '<missing>'}",
            )
            return self._result(candidate.game_id)
        replay_patch = normalize_patch(source_validation.game_version or "")
        summary_patch = normalize_patch(summary.game_version or "")
        if (
            replay_patch is not None
            and summary_patch is not None
            and replay_patch != summary_patch
        ):
            self.store.mark_failed(
                candidate,
                "PATCH_MISMATCH",
                f"Replay patch {replay_patch} does not match SUMMARY patch {summary_patch}",
            )
            return self._result(candidate.game_id)

        target = replay_archive_path(
            self.archive_root,
            candidate.game_id,
            summary.game_version or source_validation.game_version,
        )
        archived = archive_replay(candidate.source_path, target)
        if not archived.success or archived.target_path is None:
            self._mark_archive_failure(candidate, archived)
            return self._result(candidate.game_id)

        try:
            summary_artifact = self.artifacts.write_json(
                artifact_type=ArtifactType.SUMMARY,
                owner_type="match",
                owner_id=candidate.game_id,
                endpoint_template=f"/match-history-query/v1/products/lol/{{server}}_{candidate.game_id}/SUMMARY",
                http_status=summary_response.status_code,
                payload=summary_payload,
            )
            details_artifact = self.artifacts.write_json(
                artifact_type=ArtifactType.DETAILS,
                owner_type="match",
                owner_id=candidate.game_id,
                endpoint_template=f"/match-history-query/v1/products/lol/{{server}}_{candidate.game_id}/DETAILS",
                http_status=details_response.status_code,
                payload=details_payload,
            )
            self.store.commit_pair(
                candidate=candidate,
                sgp_server_id=session.server_id,
                platform_id=session.platform,
                summary=summary,
                summary_artifact=summary_artifact,
                details_artifact=details_artifact,
                rofl_path=str(archived.target_path.resolve()),
                validation=archived.validation,
            )
        except (OSError, ValueError) as error:
            self.store.mark_failed(
                candidate,
                "PAIR_COMMIT_FAILED",
                type(error).__name__,
            )
        return self._result(candidate.game_id)

    def check_pair(self, game_id: str) -> ReplayPairCheck | None:
        row = self.store.get_record(game_id)
        if row is None:
            return None
        details_path = (
            Path(str(row["details_path"]))
            if row["details_path"] is not None
            else None
        )
        rofl_path = (
            Path(str(row["rofl_path"])) if row["rofl_path"] is not None else None
        )
        details_exists = details_path is not None and details_path.is_file()
        replay_exists = rofl_path is not None and rofl_path.is_file()
        details_hash_matches = False
        if details_exists and row["details_artifact_id"] is not None:
            details_row = self.repository.connection.execute(
                "SELECT sha256 FROM raw_artifact WHERE id = ?",
                (int(row["details_artifact_id"]),),
            ).fetchone()
            if details_row is not None:
                details_hash_matches = (
                    _sha256_file(details_path) == str(details_row["sha256"])
                )
        replay_hash_matches = False
        replay_valid = False
        if replay_exists:
            replay_validation = validate_replay(rofl_path)
            replay_valid = replay_validation.valid
            replay_hash_matches = (
                replay_validation.sha256 is not None
                and row["sha256"] is not None
                and replay_validation.sha256 == str(row["sha256"])
            )
        return ReplayPairCheck(
            game_id=game_id,
            details_path=str(details_path) if details_path is not None else None,
            rofl_path=str(rofl_path) if rofl_path is not None else None,
            details_exists=details_exists,
            replay_exists=replay_exists,
            details_hash_matches=details_hash_matches,
            replay_hash_matches=replay_hash_matches,
            replay_valid=replay_valid,
        )

    def _handle_existing(
        self,
        candidate: ReplayCandidate,
        row: sqlite3.Row,
    ) -> ReplayCaptureResult:
        archived_path = (
            Path(str(row["rofl_path"])) if row["rofl_path"] is not None else None
        )
        source_validation = validate_replay(candidate.source_path)
        archived_validation = (
            validate_replay(archived_path)
            if archived_path is not None
            else None
        )
        if (
            source_validation.valid
            and archived_validation is not None
            and archived_validation.valid
            and source_validation.sha256 == archived_validation.sha256
            and source_validation.sha256 == row["sha256"]
        ):
            self.store.mark_source_captured(candidate)
        else:
            self.store.mark_failed(
                candidate,
                "VALIDATED_REPLAY_CONFLICT",
                "New source differs from the already validated archive; archive was not overwritten",
                validation=source_validation,
            )
        return self._result(candidate.game_id)

    def _mark_archive_failure(
        self,
        candidate: ReplayCandidate,
        archived: ReplayArchiveResult,
    ) -> None:
        self.store.mark_failed(
            candidate,
            archived.error_code or "ARCHIVE_FAILED",
            archived.error_message or "Replay archive failed",
            invalid=archived.validation.status is ReplayValidationStatus.INVALID,
            validation=archived.validation,
        )

    def _mark_remote_response_error(
        self,
        game_id: str,
        response_error: tuple[str, str, bool, bool],
    ) -> None:
        error_code, message, unavailable, retryable = response_error
        if unavailable:
            self.store.mark_remote_unavailable(game_id, error_code, message)
        elif retryable:
            self.store.mark_remote_waiting(game_id, error_code, message)
        else:
            self.store.mark_remote_failed(game_id, error_code, message)

    def _mark_remote_download_error(self, game_id: str, status_code: int) -> None:
        if status_code == 404:
            self.store.mark_remote_unavailable(
                game_id,
                "REPLAY_UNAVAILABLE",
                "Tencent SGP reports no Replay binary for this gameId",
            )
        elif status_code in {401, 403, 429} or status_code >= 500:
            self.store.mark_remote_waiting(
                game_id,
                "REPLAY_RETRYABLE_HTTP",
                f"Tencent SGP returned HTTP {status_code} for Replay binary",
            )
        else:
            self.store.mark_remote_failed(
                game_id,
                "REPLAY_HTTP_ERROR",
                f"Tencent SGP returned HTTP {status_code} for Replay binary",
            )

    def _cleanup_remote_staging(self, game_id: str) -> None:
        stage_path = self.data_dir / ".replay-downloads" / f"{game_id}.download"
        stage_path.unlink(missing_ok=True)
        stage_path.with_suffix(f"{stage_path.suffix}.partial").unlink(missing_ok=True)

    def _result(self, game_id: str) -> ReplayCaptureResult:
        row = self.store.get_record(game_id)
        if row is None:
            raise RuntimeError(f"Replay state is missing for game {game_id}")
        details_path = (
            str(row["details_path"]) if row["details_path"] is not None else None
        )
        return ReplayCaptureResult(
            game_id=game_id,
            details_exists=details_path is not None and Path(details_path).is_file(),
            download_status=str(row["download_status"]),
            validation_status=str(row["validation_status"]),
            rofl_path=str(row["rofl_path"]) if row["rofl_path"] is not None else None,
            details_path=details_path,
            file_size=int(row["file_size"]) if row["file_size"] is not None else None,
            sha256=str(row["sha256"]) if row["sha256"] is not None else None,
            error_code=str(row["error_code"]) if row["error_code"] is not None else None,
            error_message=(
                str(row["error_message"])
                if row["error_message"] is not None
                else None
            ),
        )

    @staticmethod
    def _response_error(
        response: HttpResponse,
        kind: str,
    ) -> tuple[str, str, bool, bool] | None:
        if response.status_code < 400 and response.payload is not None:
            return None
        if response.status_code in {401, 403}:
            return (
                "CLIENT_AUTH_REQUIRED",
                f"{kind} request requires a fresh authenticated client session",
                False,
                True,
            )
        if response.status_code == 404:
            return (
                f"{kind}_UNAVAILABLE",
                f"Tencent SGP reports no {kind} for this gameId",
                True,
                False,
            )
        if response.status_code == 429 or response.status_code >= 500:
            return (
                f"{kind}_RETRYABLE_HTTP",
                f"Tencent SGP returned HTTP {response.status_code} for {kind}",
                False,
                True,
            )
        if response.payload is None:
            return (
                f"{kind}_EMPTY_BODY",
                f"Tencent SGP returned an empty {kind} response",
                False,
                False,
            )
        return (
            f"{kind}_HTTP_ERROR",
            f"Tencent SGP returned HTTP {response.status_code} for {kind}",
            False,
            False,
        )


def _payload_game_id(payload: JsonValue | None) -> str | None:
    body = payload.get("json") if isinstance(payload, Mapping) else None
    source = body if isinstance(body, Mapping) else payload
    if not isinstance(source, Mapping):
        return None
    value = source.get("gameId")
    return str(value) if isinstance(value, int | str) else None


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()
