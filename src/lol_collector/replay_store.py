from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from threading import RLock

from lol_collector.artifacts import ArtifactRecord
from lol_collector.collection_support import MatchSummary
from lol_collector.models import (
    ReplayDownloadStatus,
    ReplaySourceDisposition,
    ReplayValidationStatus,
    normalize_patch,
    utc_now,
)
from lol_collector.replay_archive import ReplayCandidate, ReplayValidation


@dataclass(frozen=True, slots=True)
class ReplayScanRegistration:
    initialized_now: bool
    baseline_count: int
    queued_count: int


@dataclass(frozen=True, slots=True)
class ReplayMetrics:
    sources_total: int
    sources_baseline: int
    sources_queued: int
    replays_total: int
    not_requested: int
    queued: int
    downloading: int
    validated: int
    failed: int
    unavailable: int
    invalid: int


@dataclass(frozen=True, slots=True)
class ReplayPatchMetrics:
    patch: str | None
    total: int
    validated: int


class ReplayStore:
    def __init__(self, connection: sqlite3.Connection, lock: RLock) -> None:
        self.connection = connection
        self.lock = lock

    def register_scan(
        self,
        candidates: tuple[ReplayCandidate, ...],
        *,
        include_existing: bool,
    ) -> ReplayScanRegistration:
        now = utc_now().isoformat()
        baseline_count = 0
        queued_count = 0
        with self.lock, self.connection:
            initialized = self.connection.execute(
                "SELECT 1 FROM replay_capture_meta WHERE key = 'baseline_initialized'"
            ).fetchone()
            initialized_now = initialized is None
            if initialized_now:
                self.connection.execute(
                    "INSERT INTO replay_capture_meta (key, value, updated_at) VALUES ('baseline_initialized', '1', ?)",
                    (now,),
                )
            for candidate in candidates:
                source_path = str(candidate.source_path)
                existing = self.connection.execute(
                    "SELECT * FROM replay_source WHERE source_path = ?",
                    (source_path,),
                ).fetchone()
                if existing is None:
                    disposition = (
                        ReplaySourceDisposition.QUEUED
                        if include_existing or not initialized_now
                        else ReplaySourceDisposition.BASELINE
                    )
                    self._insert_source(candidate, disposition, now)
                    if disposition is ReplaySourceDisposition.BASELINE:
                        baseline_count += 1
                    else:
                        self._queue_replay(candidate, now)
                        queued_count += 1
                    continue
                changed = (
                    int(existing["observed_size"]) != candidate.file_size
                    or int(existing["observed_mtime_ns"]) != candidate.mtime_ns
                )
                disposition = ReplaySourceDisposition(str(existing["disposition"]))
                next_disposition = disposition
                if changed and disposition in {
                    ReplaySourceDisposition.FAILED,
                    ReplaySourceDisposition.UNAVAILABLE,
                }:
                    next_disposition = ReplaySourceDisposition.QUEUED
                    queued_count += 1
                self.connection.execute(
                    "UPDATE replay_source SET game_id = ?, platform_id = ?, observed_size = ?, observed_mtime_ns = ?, last_seen_at = ?, disposition = ?, error_code = CASE WHEN ? THEN NULL ELSE error_code END, error_message = CASE WHEN ? THEN NULL ELSE error_message END, next_retry_at = CASE WHEN ? THEN NULL ELSE next_retry_at END WHERE source_path = ?",
                    (
                        candidate.game_id,
                        candidate.platform_id,
                        candidate.file_size,
                        candidate.mtime_ns,
                        now,
                        next_disposition.value,
                        int(changed),
                        int(changed),
                        int(changed),
                        source_path,
                    ),
                )
                if next_disposition is ReplaySourceDisposition.QUEUED:
                    self._queue_replay(candidate, now)
        return ReplayScanRegistration(initialized_now, baseline_count, queued_count)

    def queue_remote_game(self, game_id: str, source_endpoint: str) -> None:
        if not game_id.isdecimal():
            raise ValueError("game_id must contain decimal digits only")
        now = utc_now().isoformat()
        with self.lock, self.connection:
            self.connection.execute(
                "INSERT INTO replay_artifact (game_id, source_path, download_status, validation_status, acquisition_method, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(game_id) DO UPDATE SET source_path = CASE WHEN replay_artifact.download_status = 'VALIDATED' THEN replay_artifact.source_path ELSE excluded.source_path END, download_status = CASE WHEN replay_artifact.download_status = 'VALIDATED' THEN replay_artifact.download_status WHEN replay_artifact.download_status IN ('UNAVAILABLE', 'DOWNLOADED_BUT_INVALID') THEN replay_artifact.download_status ELSE 'QUEUED' END, acquisition_method = CASE WHEN replay_artifact.download_status = 'VALIDATED' THEN replay_artifact.acquisition_method ELSE excluded.acquisition_method END, updated_at = excluded.updated_at",
                (
                    game_id,
                    source_endpoint,
                    ReplayDownloadStatus.QUEUED.value,
                    ReplayValidationStatus.NOT_VALIDATED.value,
                    "TENCENT_SGP_REPLAY_BINARY",
                    now,
                    now,
                ),
            )

    def pending_remote_games(
        self,
        limit: int,
        cooldown_seconds: float = 30.0,
    ) -> tuple[str, ...]:
        cutoff = (utc_now() - timedelta(seconds=cooldown_seconds)).isoformat()
        with self.lock:
            rows = self.connection.execute(
                "SELECT game_id FROM replay_artifact WHERE acquisition_method = 'TENCENT_SGP_REPLAY_BINARY' AND download_status IN ('QUEUED', 'FAILED') AND (last_checked_at IS NULL OR last_checked_at <= ?) ORDER BY created_at, game_id LIMIT ?",
                (cutoff, limit),
            ).fetchall()
        return tuple(str(row["game_id"]) for row in rows)

    def begin_remote_attempt(self, game_id: str) -> None:
        now = utc_now().isoformat()
        with self.lock, self.connection:
            self.connection.execute(
                "UPDATE replay_artifact SET download_status = ?, validation_status = ?, download_started_at = ?, download_completed_at = NULL, error_code = NULL, error_message = NULL, attempt_count = attempt_count + 1, last_checked_at = ?, updated_at = ? WHERE game_id = ? AND download_status <> ?",
                (
                    ReplayDownloadStatus.DOWNLOADING.value,
                    ReplayValidationStatus.NOT_VALIDATED.value,
                    now,
                    now,
                    now,
                    game_id,
                    ReplayDownloadStatus.VALIDATED.value,
                ),
            )

    def record_remote_validation(
        self,
        game_id: str,
        validation: ReplayValidation,
    ) -> None:
        if not validation.valid or validation.sha256 is None:
            raise ValueError("record_remote_validation requires a valid Replay")
        now = utc_now().isoformat()
        with self.lock, self.connection:
            self.connection.execute(
                "UPDATE replay_artifact SET game_version = ?, patch = ?, file_size = ?, sha256 = ?, validation_status = ?, last_checked_at = ?, updated_at = ? WHERE game_id = ? AND download_status <> ?",
                (
                    validation.game_version,
                    normalize_patch(validation.game_version or ""),
                    validation.file_size,
                    validation.sha256,
                    ReplayValidationStatus.VALIDATED.value,
                    now,
                    now,
                    game_id,
                    ReplayDownloadStatus.VALIDATED.value,
                ),
            )

    def mark_remote_waiting(
        self,
        game_id: str,
        error_code: str,
        error_message: str,
    ) -> None:
        self._mark_remote(
            game_id,
            ReplayDownloadStatus.QUEUED,
            ReplayValidationStatus.NOT_VALIDATED,
            error_code,
            error_message,
            completed=False,
        )

    def mark_remote_failed(
        self,
        game_id: str,
        error_code: str,
        error_message: str,
        *,
        invalid: bool = False,
        validation: ReplayValidation | None = None,
    ) -> None:
        if validation is not None:
            self.record_remote_validation(game_id, validation) if validation.valid else None
        self._mark_remote(
            game_id,
            (
                ReplayDownloadStatus.DOWNLOADED_BUT_INVALID
                if invalid
                else ReplayDownloadStatus.FAILED
            ),
            (
                ReplayValidationStatus.INVALID
                if invalid
                else ReplayValidationStatus.NOT_VALIDATED
            ),
            error_code,
            error_message,
            completed=True,
            validation=validation,
        )

    def mark_remote_unavailable(
        self,
        game_id: str,
        error_code: str,
        error_message: str,
    ) -> None:
        self._mark_remote(
            game_id,
            ReplayDownloadStatus.UNAVAILABLE,
            ReplayValidationStatus.NOT_VALIDATED,
            error_code,
            error_message,
            completed=True,
        )

    def pending_sources(self, limit: int) -> tuple[ReplayCandidate, ...]:
        with self.lock:
            rows = self.connection.execute(
                "SELECT source_path, game_id, platform_id, observed_size, observed_mtime_ns FROM replay_source WHERE disposition = ? AND (next_retry_at IS NULL OR next_retry_at <= ?) ORDER BY first_seen_at, source_path LIMIT ?",
                (ReplaySourceDisposition.QUEUED.value, utc_now().isoformat(), limit),
            ).fetchall()
        return tuple(
            ReplayCandidate(
                source_path=Path(str(row["source_path"])),
                game_id=str(row["game_id"]),
                platform_id=(
                    str(row["platform_id"])
                    if row["platform_id"] is not None
                    else None
                ),
                file_size=int(row["observed_size"]),
                mtime_ns=int(row["observed_mtime_ns"]),
            )
            for row in rows
        )

    def begin_attempt(self, candidate: ReplayCandidate) -> None:
        now = utc_now().isoformat()
        with self.lock, self.connection:
            self._queue_replay(candidate, now)
            self.connection.execute(
                "UPDATE replay_artifact SET download_status = ?, validation_status = ?, download_started_at = ?, download_completed_at = NULL, error_code = NULL, error_message = NULL, acquisition_method = ?, attempt_count = attempt_count + 1, last_checked_at = ?, updated_at = ? WHERE game_id = ? AND download_status <> ?",
                (
                    ReplayDownloadStatus.DOWNLOADING.value,
                    ReplayValidationStatus.NOT_VALIDATED.value,
                    now,
                    "OFFICIAL_CLIENT_REPLAY_PLUS_TENCENT_SGP",
                    now,
                    now,
                    candidate.game_id,
                    ReplayDownloadStatus.VALIDATED.value,
                ),
            )

    def record_source_validation(
        self,
        candidate: ReplayCandidate,
        validation: ReplayValidation,
    ) -> None:
        if not validation.valid or validation.sha256 is None:
            raise ValueError("record_source_validation requires a valid Replay")
        now = utc_now().isoformat()
        with self.lock, self.connection:
            self.connection.execute(
                "UPDATE replay_artifact SET game_version = ?, patch = ?, file_size = ?, sha256 = ?, validation_status = ?, last_checked_at = ?, updated_at = ? WHERE game_id = ? AND download_status <> ?",
                (
                    validation.game_version,
                    normalize_patch(validation.game_version or ""),
                    validation.file_size,
                    validation.sha256,
                    ReplayValidationStatus.VALIDATED.value,
                    now,
                    now,
                    candidate.game_id,
                    ReplayDownloadStatus.VALIDATED.value,
                ),
            )

    def mark_waiting(
        self,
        candidate: ReplayCandidate,
        error_code: str,
        error_message: str,
        delay_seconds: float = 30.0,
    ) -> None:
        now = utc_now().isoformat()
        next_retry = (utc_now() + timedelta(seconds=delay_seconds)).isoformat()
        with self.lock, self.connection:
            self.connection.execute(
                "UPDATE replay_artifact SET download_status = ?, error_code = ?, error_message = ?, last_checked_at = ?, updated_at = ? WHERE game_id = ? AND download_status <> ?",
                (
                    ReplayDownloadStatus.QUEUED.value,
                    error_code,
                    error_message[:1000],
                    now,
                    now,
                    candidate.game_id,
                    ReplayDownloadStatus.VALIDATED.value,
                ),
            )
            self._mark_source(
                candidate,
                ReplaySourceDisposition.QUEUED,
                now,
                error_code,
                error_message,
                next_retry,
            )

    def mark_failed(
        self,
        candidate: ReplayCandidate,
        error_code: str,
        error_message: str,
        *,
        invalid: bool = False,
        validation: ReplayValidation | None = None,
    ) -> None:
        now = utc_now().isoformat()
        download_status = (
            ReplayDownloadStatus.DOWNLOADED_BUT_INVALID
            if invalid
            else ReplayDownloadStatus.FAILED
        )
        validation_status = (
            ReplayValidationStatus.INVALID
            if invalid
            else ReplayValidationStatus.NOT_VALIDATED
        )
        with self.lock, self.connection:
            self.connection.execute(
                "UPDATE replay_artifact SET download_status = ?, validation_status = ?, file_size = ?, sha256 = ?, error_code = ?, error_message = ?, download_completed_at = ?, last_checked_at = ?, updated_at = ? WHERE game_id = ? AND download_status <> ?",
                (
                    download_status.value,
                    validation_status.value,
                    validation.file_size if validation is not None else None,
                    validation.sha256 if validation is not None else None,
                    error_code,
                    error_message[:1000],
                    now,
                    now,
                    now,
                    candidate.game_id,
                    ReplayDownloadStatus.VALIDATED.value,
                ),
            )
            self._mark_source(
                candidate,
                ReplaySourceDisposition.FAILED,
                now,
                error_code,
                error_message,
                None,
            )

    def mark_unavailable(
        self,
        candidate: ReplayCandidate,
        error_code: str,
        error_message: str,
    ) -> None:
        now = utc_now().isoformat()
        with self.lock, self.connection:
            self.connection.execute(
                "UPDATE replay_artifact SET download_status = ?, error_code = ?, error_message = ?, download_completed_at = ?, last_checked_at = ?, updated_at = ? WHERE game_id = ? AND download_status <> ?",
                (
                    ReplayDownloadStatus.UNAVAILABLE.value,
                    error_code,
                    error_message[:1000],
                    now,
                    now,
                    now,
                    candidate.game_id,
                    ReplayDownloadStatus.VALIDATED.value,
                ),
            )
            self._mark_source(
                candidate,
                ReplaySourceDisposition.UNAVAILABLE,
                now,
                error_code,
                error_message,
                None,
            )

    def mark_source_captured(self, candidate: ReplayCandidate) -> None:
        now = utc_now().isoformat()
        with self.lock, self.connection:
            self._mark_source(
                candidate,
                ReplaySourceDisposition.CAPTURED,
                now,
                None,
                None,
                None,
            )

    def commit_pair(
        self,
        *,
        candidate: ReplayCandidate,
        sgp_server_id: str,
        platform_id: str,
        summary: MatchSummary,
        summary_artifact: ArtifactRecord,
        details_artifact: ArtifactRecord,
        rofl_path: str,
        validation: ReplayValidation,
        source_path_override: str | None = None,
        acquisition_method: str = "OFFICIAL_CLIENT_REPLAY_PLUS_TENCENT_SGP",
        track_source: bool = True,
    ) -> int:
        if not validation.valid or validation.sha256 is None:
            raise ValueError("commit_pair requires a validated Replay")
        now = utc_now().isoformat()
        with self.lock, self.connection:
            replay_row = self.connection.execute(
                "SELECT match_id, download_status, sha256 FROM replay_artifact WHERE game_id = ?",
                (candidate.game_id,),
            ).fetchone()
            if replay_row is None:
                raise ValueError("Replay state row is missing before pair commit")
            if (
                str(replay_row["download_status"])
                == ReplayDownloadStatus.VALIDATED.value
            ):
                if str(replay_row["sha256"]) != validation.sha256:
                    raise ValueError(
                        "validated Replay conflict: refusing to replace a different SHA-256"
                    )
                if track_source:
                    self._mark_source(
                        candidate,
                        ReplaySourceDisposition.CAPTURED,
                        now,
                        None,
                        None,
                        None,
                    )
                match_id_value = replay_row["match_id"]
                if match_id_value is None:
                    raise ValueError("validated Replay is missing its match link")
                return int(match_id_value)
            summary_id = self._add_artifact(summary_artifact)
            details_id = self._add_artifact(details_artifact)
            row = self.connection.execute(
                "SELECT id FROM match WHERE sgp_server_id = ? AND game_id = ?",
                (sgp_server_id, candidate.game_id),
            ).fetchone()
            values = (
                summary.game_start_at.isoformat()
                if summary.game_start_at is not None
                else None,
                summary.game_end_at.isoformat()
                if summary.game_end_at is not None
                else None,
                summary.queue_id,
                summary.game_mode,
                summary.map_id,
                summary.game_version or validation.game_version,
                normalize_patch(summary.game_version or validation.game_version or ""),
                summary_id,
                details_id,
            )
            if row is None:
                cursor = self.connection.execute(
                    "INSERT INTO match (sgp_server_id, platform_id, game_id, game_start_at, game_end_at, queue_id, game_mode, map_id, game_version_full, patch_normalized, summary_artifact_id, details_artifact_id, parsed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        sgp_server_id,
                        platform_id,
                        candidate.game_id,
                        *values,
                        now,
                    ),
                )
                match_id = int(cursor.lastrowid)
            else:
                match_id = int(row["id"])
                self.connection.execute(
                    "UPDATE match SET platform_id = ?, game_start_at = ?, game_end_at = ?, queue_id = ?, game_mode = ?, map_id = ?, game_version_full = ?, patch_normalized = ?, summary_artifact_id = ?, details_artifact_id = ?, parsed_at = ? WHERE id = ?",
                    (platform_id, *values, now, match_id),
                )
            replay_update = self.connection.execute(
                "UPDATE replay_artifact SET match_id = ?, details_artifact_id = ?, game_version = ?, patch = ?, rofl_path = ?, source_path = ?, file_size = ?, sha256 = ?, download_status = ?, validation_status = ?, download_completed_at = ?, error_code = NULL, error_message = NULL, acquisition_method = ?, last_checked_at = ?, updated_at = ? WHERE game_id = ? AND download_status <> ?",
                (
                    match_id,
                    details_id,
                    summary.game_version or validation.game_version,
                    normalize_patch(summary.game_version or validation.game_version or ""),
                    rofl_path,
                    source_path_override or str(candidate.source_path),
                    validation.file_size,
                    validation.sha256,
                    ReplayDownloadStatus.VALIDATED.value,
                    ReplayValidationStatus.VALIDATED.value,
                    now,
                    acquisition_method,
                    now,
                    now,
                    candidate.game_id,
                    ReplayDownloadStatus.VALIDATED.value,
                ),
            )
            if replay_update.rowcount != 1:
                raise ValueError("Replay state changed concurrently during pair commit")
            if track_source:
                self._mark_source(
                    candidate,
                    ReplaySourceDisposition.CAPTURED,
                    now,
                    None,
                    None,
                    None,
                )
            return match_id

    def commit_remote_pair(
        self,
        *,
        game_id: str,
        source_endpoint: str,
        sgp_server_id: str,
        platform_id: str,
        summary: MatchSummary,
        summary_artifact: ArtifactRecord,
        details_artifact: ArtifactRecord,
        rofl_path: str,
        validation: ReplayValidation,
    ) -> int:
        candidate = ReplayCandidate(
            source_path=Path(source_endpoint),
            game_id=game_id,
            platform_id=platform_id,
            file_size=validation.file_size,
            mtime_ns=0,
        )
        return self.commit_pair(
            candidate=candidate,
            sgp_server_id=sgp_server_id,
            platform_id=platform_id,
            summary=summary,
            summary_artifact=summary_artifact,
            details_artifact=details_artifact,
            rofl_path=rofl_path,
            validation=validation,
            source_path_override=source_endpoint,
            acquisition_method="TENCENT_SGP_REPLAY_BINARY",
            track_source=False,
        )

    def recover_interrupted(self) -> int:
        now = utc_now().isoformat()
        with self.lock, self.connection:
            rows = self.connection.execute(
                "SELECT game_id, source_path FROM replay_artifact WHERE download_status = ?",
                (ReplayDownloadStatus.DOWNLOADING.value,),
            ).fetchall()
            self.connection.execute(
                "UPDATE replay_artifact SET download_status = ?, error_code = 'INTERRUPTED', error_message = 'Previous capture stopped before commit', updated_at = ? WHERE download_status = ?",
                (
                    ReplayDownloadStatus.QUEUED.value,
                    now,
                    ReplayDownloadStatus.DOWNLOADING.value,
                ),
            )
            for row in rows:
                if row["source_path"] is not None:
                    self.connection.execute(
                        "UPDATE replay_source SET disposition = ?, error_code = 'INTERRUPTED', error_message = 'Previous capture stopped before commit', last_seen_at = ? WHERE source_path = ?",
                        (
                            ReplaySourceDisposition.QUEUED.value,
                            now,
                            str(row["source_path"]),
                        ),
                    )
            return len(rows)

    def get_record(self, game_id: str) -> sqlite3.Row | None:
        with self.lock:
            return self.connection.execute(
                "SELECT r.*, m.summary_artifact_id, s.filesystem_path AS summary_path, d.filesystem_path AS details_path FROM replay_artifact r LEFT JOIN match m ON m.id = r.match_id LEFT JOIN raw_artifact s ON s.id = m.summary_artifact_id LEFT JOIN raw_artifact d ON d.id = r.details_artifact_id WHERE r.game_id = ?",
                (game_id,),
            ).fetchone()

    def list_records(self, limit: int = 50) -> tuple[sqlite3.Row, ...]:
        with self.lock:
            rows = self.connection.execute(
                "SELECT r.*, m.summary_artifact_id, s.filesystem_path AS summary_path, d.filesystem_path AS details_path FROM replay_artifact r LEFT JOIN match m ON m.id = r.match_id LEFT JOIN raw_artifact s ON s.id = m.summary_artifact_id LEFT JOIN raw_artifact d ON d.id = r.details_artifact_id ORDER BY r.updated_at DESC, r.game_id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return tuple(rows)

    def metrics(self) -> ReplayMetrics:
        with self.lock:
            source_counts = {
                str(row["disposition"]): int(row["count"])
                for row in self.connection.execute(
                    "SELECT disposition, COUNT(*) AS count FROM replay_source GROUP BY disposition"
                ).fetchall()
            }
            replay_counts = {
                str(row["download_status"]): int(row["count"])
                for row in self.connection.execute(
                    "SELECT download_status, COUNT(*) AS count FROM replay_artifact GROUP BY download_status"
                ).fetchall()
            }
        return ReplayMetrics(
            sources_total=sum(source_counts.values()),
            sources_baseline=source_counts.get(ReplaySourceDisposition.BASELINE.value, 0),
            sources_queued=source_counts.get(ReplaySourceDisposition.QUEUED.value, 0),
            replays_total=sum(replay_counts.values()),
            not_requested=replay_counts.get(ReplayDownloadStatus.NOT_REQUESTED.value, 0),
            queued=replay_counts.get(ReplayDownloadStatus.QUEUED.value, 0),
            downloading=replay_counts.get(ReplayDownloadStatus.DOWNLOADING.value, 0),
            validated=replay_counts.get(ReplayDownloadStatus.VALIDATED.value, 0),
            failed=replay_counts.get(ReplayDownloadStatus.FAILED.value, 0),
            unavailable=replay_counts.get(ReplayDownloadStatus.UNAVAILABLE.value, 0),
            invalid=replay_counts.get(
                ReplayDownloadStatus.DOWNLOADED_BUT_INVALID.value, 0
            ),
        )

    def patch_metrics(self) -> tuple[ReplayPatchMetrics, ...]:
        with self.lock:
            rows = self.connection.execute(
                "SELECT patch, COUNT(*) AS total, "
                + "SUM(CASE WHEN download_status = 'VALIDATED' THEN 1 ELSE 0 END) "
                + "AS validated FROM replay_artifact GROUP BY patch "
                + "ORDER BY patch IS NULL, patch DESC"
            ).fetchall()
        return tuple(
            ReplayPatchMetrics(
                patch=str(row["patch"]) if row["patch"] is not None else None,
                total=int(row["total"]),
                validated=int(row["validated"] or 0),
            )
            for row in rows
        )

    def _insert_source(
        self,
        candidate: ReplayCandidate,
        disposition: ReplaySourceDisposition,
        now: str,
    ) -> None:
        self.connection.execute(
            "INSERT INTO replay_source (source_path, game_id, platform_id, observed_size, observed_mtime_ns, first_seen_at, last_seen_at, disposition) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                str(candidate.source_path),
                candidate.game_id,
                candidate.platform_id,
                candidate.file_size,
                candidate.mtime_ns,
                now,
                now,
                disposition.value,
            ),
        )

    def _queue_replay(self, candidate: ReplayCandidate, now: str) -> None:
        self.connection.execute(
            "INSERT INTO replay_artifact (game_id, source_path, download_status, validation_status, acquisition_method, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(game_id) DO UPDATE SET source_path = CASE WHEN replay_artifact.download_status = 'VALIDATED' THEN replay_artifact.source_path ELSE excluded.source_path END, download_status = CASE WHEN replay_artifact.download_status = 'VALIDATED' THEN replay_artifact.download_status ELSE 'QUEUED' END, acquisition_method = CASE WHEN replay_artifact.download_status = 'VALIDATED' THEN replay_artifact.acquisition_method ELSE excluded.acquisition_method END, updated_at = excluded.updated_at",
            (
                candidate.game_id,
                str(candidate.source_path),
                ReplayDownloadStatus.QUEUED.value,
                ReplayValidationStatus.NOT_VALIDATED.value,
                "OFFICIAL_CLIENT_REPLAY_PLUS_TENCENT_SGP",
                now,
                now,
            ),
        )

    def _mark_source(
        self,
        candidate: ReplayCandidate,
        disposition: ReplaySourceDisposition,
        now: str,
        error_code: str | None,
        error_message: str | None,
        next_retry_at: str | None,
    ) -> None:
        self.connection.execute(
            "UPDATE replay_source SET disposition = ?, error_code = ?, error_message = ?, next_retry_at = ?, processed_at = CASE WHEN ? IN ('CAPTURED', 'FAILED', 'UNAVAILABLE') THEN ? ELSE processed_at END, last_seen_at = ? WHERE source_path = ?",
            (
                disposition.value,
                error_code,
                error_message[:1000] if error_message is not None else None,
                next_retry_at,
                disposition.value,
                now,
                now,
                str(candidate.source_path),
            ),
        )

    def _add_artifact(self, record: ArtifactRecord) -> int:
        existing = self.connection.execute(
            "SELECT id, sha256 FROM raw_artifact WHERE filesystem_path = ?",
            (record.filesystem_path,),
        ).fetchone()
        if existing is not None:
            if str(existing["sha256"]) != record.sha256:
                raise ValueError("artifact path collision with different SHA-256")
            return int(existing["id"])
        cursor = self.connection.execute(
            "INSERT INTO raw_artifact (artifact_type, owner_type, owner_id, fetched_at, endpoint_template, http_status, byte_size, sha256, filesystem_path, schema_hash, artifact_version) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                record.artifact_type.value,
                record.owner_type,
                record.owner_id,
                record.fetched_at,
                record.endpoint_template,
                record.http_status,
                record.byte_size,
                record.sha256,
                record.filesystem_path,
                record.schema_hash,
                record.artifact_version,
            ),
        )
        return int(cursor.lastrowid)

    def _mark_remote(
        self,
        game_id: str,
        download_status: ReplayDownloadStatus,
        validation_status: ReplayValidationStatus,
        error_code: str,
        error_message: str,
        *,
        completed: bool,
        validation: ReplayValidation | None = None,
    ) -> None:
        now = utc_now().isoformat()
        with self.lock, self.connection:
            self.connection.execute(
                "UPDATE replay_artifact SET download_status = ?, validation_status = ?, file_size = COALESCE(?, file_size), sha256 = COALESCE(?, sha256), error_code = ?, error_message = ?, download_completed_at = CASE WHEN ? THEN ? ELSE NULL END, last_checked_at = ?, updated_at = ? WHERE game_id = ? AND download_status <> ?",
                (
                    download_status.value,
                    validation_status.value,
                    validation.file_size if validation is not None else None,
                    validation.sha256 if validation is not None else None,
                    error_code,
                    error_message[:1000],
                    int(completed),
                    now,
                    now,
                    now,
                    game_id,
                    ReplayDownloadStatus.VALIDATED.value,
                ),
            )
