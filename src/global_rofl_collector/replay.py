from __future__ import annotations

import hashlib
import json
import os
import random
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from .config import Config
from .db import Database
from .errors import IntegrityError, LcuError, ReplayError
from .models import Capability, JobState, ProbeEvidence, safe_build_component
from .riot import _retry_after

EDGE_URL_RE = re.compile(
    r"(https://[^\s\"']+)/match-history-query/v3/product/lol/matchId/",
    re.IGNORECASE,
)


def _safe_response_message(response: httpx.Response) -> str:
    try:
        data = response.json()
        if isinstance(data, dict):
            message = data.get("message") or data.get("errorCode")
            if message:
                return str(message)[:500]
    except ValueError:
        pass
    return f"HTTP {response.status_code}"


@dataclass(frozen=True, slots=True)
class Verification:
    file_size: int
    sha256: str
    container: str
    uncompressed_size: int
    inner_prefix_hex: str
    game_version: str
    chunk_count: int
    game_chunks: int
    keyframes: int
    start_keyframes: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "file_size": self.file_size,
            "sha256": self.sha256,
            "container": self.container,
            "uncompressed_size": self.uncompressed_size,
            "inner_prefix_hex": self.inner_prefix_hex,
            "game_version": self.game_version,
            "chunk_count": self.chunk_count,
            "game_chunks": self.game_chunks,
            "keyframes": self.keyframes,
            "start_keyframes": self.start_keyframes,
            "container_layout": "PASS",
            "metadata_json": "PASS",
        }


def verify_rofl(path: Path, *, expected_size: int | None = None) -> Verification:
    if not path.is_file():
        raise IntegrityError("ROFL_MISSING", f"Replay file does not exist: {path}")
    size = path.stat().st_size
    if size <= 0:
        raise IntegrityError("ROFL_ZERO_BYTE", "Replay file is empty")
    if expected_size is not None and size != expected_size:
        raise IntegrityError(
            "ROFL_TRUNCATED",
            f"Replay size {size} does not match Content-Length {expected_size}",
            retryable=True,
        )

    digest = hashlib.sha256()
    with path.open("rb") as stream:
        prefix = stream.read(96)
        stream.seek(0)
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    lowered = prefix.lstrip().lower()
    if lowered.startswith((b"<html", b"<!doctype", b"{", b"[")):
        raise IntegrityError("ROFL_ERROR_BODY", "Replay response is HTML or JSON")
    if prefix.startswith(b"\x1f\x8b"):
        raise IntegrityError(
            "ROFL_HTTP_GZIP_WRAPPER",
            "Replay still has the HTTP gzip transfer wrapper instead of RIOT container bytes",
        )
    if not prefix.startswith(b"RIOT"):
        raise IntegrityError(
            "ROFL_MALFORMED",
            f"Unknown replay container magic: {prefix[:8].hex()}",
        )

    if size < 0x0F + 0x100 + 4:
        raise IntegrityError("ROFL_TOO_SHORT", "Replay is too short for its container layout")
    version_length = prefix[0x0E]
    header_size = 0x0F + version_length
    if version_length <= 0 or version_length > 64 or len(prefix) < header_size:
        raise IntegrityError("ROFL_VERSION_INVALID", "Replay version field has invalid bounds")
    try:
        game_version = prefix[0x0F:header_size].decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise IntegrityError("ROFL_VERSION_INVALID", "Replay version is not UTF-8") from exc
    if not re.fullmatch(r"[0-9]+(?:\.[0-9A-Za-z]+)+", game_version):
        raise IntegrityError("ROFL_VERSION_INVALID", "Replay version has an unexpected shape")

    with path.open("rb") as stream:
        stream.seek(size - 4)
        metadata_length = int.from_bytes(stream.read(4), "little")
        if metadata_length <= 0 or metadata_length > 64 * 1024 * 1024:
            raise IntegrityError("ROFL_METADATA_INVALID", "Replay metadata length is invalid")
        metadata_end = size - 4
        metadata_start = metadata_end - metadata_length
        signature_start = metadata_start - 0x100
        if signature_start < header_size:
            raise IntegrityError("ROFL_TAIL_INVALID", "Replay tail bounds overlap the header")
        stream.seek(metadata_start)
        try:
            metadata = json.loads(stream.read(metadata_length).decode("utf-8", errors="strict"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise IntegrityError(
                "ROFL_METADATA_INVALID", "Replay metadata JSON is invalid"
            ) from exc
        if not isinstance(metadata, dict):
            raise IntegrityError("ROFL_METADATA_INVALID", "Replay metadata is not an object")

        cursor = header_size
        chunk_count = 0
        game_chunks = 0
        keyframes = 0
        start_keyframes = 0
        while cursor < signature_start:
            if signature_start - cursor < 0x11:
                raise IntegrityError("ROFL_CHUNK_TRUNCATED", "Replay chunk header is truncated")
            stream.seek(cursor)
            header = stream.read(0x11)
            stream_id = int.from_bytes(header[5:9], "little")
            stream_tag = (stream_id >> 24) & 0xFF
            uncompressed_length = int.from_bytes(header[9:13], "little")
            compressed_length = int.from_bytes(header[13:17], "little")
            body_length = compressed_length or uncompressed_length
            if body_length < 0 or body_length > 128 * 1024 * 1024:
                raise IntegrityError("ROFL_CHUNK_INVALID", "Replay chunk length is invalid")
            cursor += 0x11 + body_length
            if cursor > signature_start:
                raise IntegrityError("ROFL_CHUNK_TRUNCATED", "Replay chunk exceeds its region")
            chunk_count += 1
            game_chunks += int(stream_tag == 1)
            keyframes += int(stream_tag == 2)
            start_keyframes += int(stream_tag == 3)
        if cursor != signature_start or chunk_count == 0:
            raise IntegrityError("ROFL_CHUNK_INVALID", "Replay chunk region is not well formed")
    return Verification(
        file_size=size,
        sha256=digest.hexdigest(),
        container="riot-replay-v2",
        uncompressed_size=size,
        inner_prefix_hex=prefix[:32].hex(),
        game_version=game_version,
        chunk_count=chunk_count,
        game_chunks=game_chunks,
        keyframes=keyframes,
        start_keyframes=start_keyframes,
    )


class LcuClient:
    def __init__(self, config: Config, *, client: httpx.Client | None = None):
        self.config = config
        self.lockfile = config.league_install_dir / "lockfile"
        self._client = client
        self._owns_client = client is None
        self._base_url = ""
        self._auth: tuple[str, str] | None = None
        if client is None:
            self._connect()

    def _connect(self) -> None:
        if not self.lockfile.is_file():
            raise LcuError(
                "LEAGUE_CLIENT_CLOSED",
                "ACTION_REQUIRED: LOGIN_TO_LEAGUE_CLIENT (lockfile not found)",
            )
        try:
            parts = self.lockfile.read_text(encoding="utf-8").strip().split(":")
            if len(parts) != 5:
                raise ValueError("unexpected lockfile field count")
            _, _, port, password, protocol = parts
            self._base_url = f"{protocol}://127.0.0.1:{int(port)}"
            self._auth = ("riot", password)
        except (OSError, ValueError) as exc:
            raise LcuError("LCU_LOCKFILE_INVALID", "League lockfile is not usable") from exc
        self._client = httpx.Client(
            base_url=self._base_url,
            auth=self._auth,
            verify=False,
            timeout=httpx.Timeout(30.0, read=120.0),
        )

    def close(self) -> None:
        if self._owns_client and self._client is not None:
            self._client.close()

    def request(
        self, method: str, path: str, *, json_body: dict[str, Any] | None = None
    ) -> httpx.Response:
        if self._client is None:
            raise LcuError("LCU_NOT_CONNECTED", "League Client is not connected")
        try:
            return self._client.request(method, path, json=json_body)
        except httpx.TransportError as exc:
            raise LcuError(
                "LCU_UNREACHABLE", "League Client local API is unreachable", retryable=True
            ) from exc

    def json(self, path: str) -> Any:
        response = self.request("GET", path)
        if response.status_code == 401:
            raise LcuError("LCU_AUTH_FAILED", "League Client rejected local authentication")
        if response.status_code >= 400:
            raise LcuError(
                "LCU_HTTP_ERROR",
                _safe_response_message(response),
                http_status=response.status_code,
            )
        try:
            return response.json()
        except ValueError as exc:
            raise LcuError("LCU_INVALID_RESPONSE", f"LCU returned non-JSON for {path}") from exc

    def configuration(self) -> dict[str, Any]:
        data = self.json("/lol-replays/v1/configuration")
        if not isinstance(data, dict):
            raise LcuError("LCU_SCHEMA_CHANGED", "Unexpected replay configuration response")
        return data

    def current_platform(self) -> str:
        data = self.json("/lol-rso-auth/v1/authorization")
        platform = data.get("currentPlatformId") if isinstance(data, dict) else None
        if not platform:
            raise LcuError(
                "LCU_LOGIN_REQUIRED", "ACTION_REQUIRED: LOGIN_TO_LEAGUE_CLIENT"
            )
        return str(platform).upper()

    def edge_headers(self) -> dict[str, str]:
        token_data = self.json("/lol-rso-auth/v1/authorization/access-token")
        entitlement_data = self.json("/entitlements/v1/token")
        token = token_data.get("token") if isinstance(token_data, dict) else None
        entitlement = (
            entitlement_data.get("accessToken") if isinstance(entitlement_data, dict) else None
        )
        if not token or not entitlement:
            raise LcuError(
                "LCU_LOGIN_REQUIRED", "ACTION_REQUIRED: LOGIN_TO_LEAGUE_CLIENT"
            )
        return {
            "Authorization": f"Bearer {token}",
            "X-Riot-Entitlements-JWT": str(entitlement),
            "Accept": "application/octet-stream,application/json",
        }

    def route_a_request(self, game_id: str) -> httpx.Response:
        return self.request(
            "POST",
            f"/lol-replays/v1/rofls/{game_id}/download",
            json_body={"componentType": "match-history"},
        )

    def discover_edge_base(self) -> str:
        if self.config.replay_edge_base_url:
            return self.config.replay_edge_base_url
        log_dir = self.config.league_install_dir / "Logs" / "LeagueClient Logs"
        logs = sorted(
            log_dir.glob("*_LeagueClient.log"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        for log in logs[:8]:
            try:
                content = log.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            matches = EDGE_URL_RE.findall(content)
            if matches:
                return matches[-1].rstrip("/")
        raise LcuError(
            "REPLAY_EDGE_UNDISCOVERED",
            "Could not discover the current player-platform replay backend from client logs",
        )

    def latest_route_evidence(self, game_id: str) -> str | None:
        log_dir = self.config.league_install_dir / "Logs" / "LeagueClient Logs"
        logs = sorted(
            log_dir.glob("*_LeagueClient.log"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        needle = str(game_id)
        for log in logs[:3]:
            try:
                lines = log.read_text(encoding="utf-8", errors="ignore").splitlines()
            except OSError:
                continue
            relevant = [
                line
                for line in lines
                if needle in line and ("match-history-query" in line or "server response" in line)
            ]
            if relevant:
                excerpt = " | ".join(relevant[-3:])
                # Host/path/status are evidence; opaque request identifiers are not useful.
                return excerpt[:1500]
        return None


class ReplayBackendAcquirer:
    provider = "riot-player-platform-replay-v2"

    def __init__(
        self,
        config: Config,
        *,
        lcu: LcuClient | None = None,
        edge_client: httpx.Client | None = None,
        download_retries: int = 4,
        sleep: Callable[[float], None] = time.sleep,
        rng: random.Random | None = None,
    ):
        self.config = config
        self.lcu = lcu or LcuClient(config)
        self._owns_lcu = lcu is None
        self.edge = edge_client or httpx.Client(
            timeout=httpx.Timeout(30.0, read=180.0), follow_redirects=True
        )
        self._owns_edge = edge_client is None
        self.edge_base: str | None = config.replay_edge_base_url
        self.download_retries = max(1, download_retries)
        self.sleep = sleep
        self.rng = rng or random.Random()

    def close(self) -> None:
        if self._owns_edge:
            self.edge.close()
        if self._owns_lcu:
            self.lcu.close()

    @staticmethod
    def _path(match_id: str, info_type: str) -> str:
        return (
            "/match-history-query/v3/product/lol/matchId/"
            f"{match_id}/infoType/{info_type}"
        )

    def probe(self, match_id: str, game_id: str) -> list[ProbeEvidence]:
        results: list[ProbeEvidence] = []
        config = self.lcu.configuration()
        platform = self.lcu.current_platform()
        target_platform = match_id.split("_", 1)[0].upper()
        route_a_response = self.lcu.route_a_request(game_id)
        time.sleep(1.0)
        route_a_log = self.lcu.latest_route_evidence(game_id)
        route_a_pass = (
            platform == target_platform and route_a_response.status_code in (200, 202, 204)
        )
        results.append(
            ProbeEvidence(
                route="A_CROSS_REGION_LCU",
                capability=Capability.PASS if route_a_pass else Capability.FAIL,
                mechanism="POST /lol-replays/v1/rofls/{gameId}/download",
                http_status=route_a_response.status_code,
                auth_result="LCU_AUTHENTICATED",
                region_evidence=(
                    f"LCU current platform is {target_platform}"
                    if route_a_pass
                    else (
                        f"LCU current platform {platform}; "
                        f"backend lookup used {platform}_{game_id}"
                    )
                ),
                client_log_excerpt=route_a_log,
                evidence={
                    "client_platform": platform,
                    "replays_enabled": bool(config.get("isReplaysEnabled")),
                    "request_accepted": route_a_response.status_code in (200, 202, 204),
                },
            )
        )

        try:
            self.edge_base = self.lcu.discover_edge_base()
            headers = self.lcu.edge_headers()
            with self.edge.stream(
                "GET",
                self.edge_base + self._path(match_id, "replay"),
                headers=headers,
            ) as response:
                # The backend uses HTTP Content-Encoding gzip. The official client stores
                # the decoded RIOT container, so inspect the decoded representation here.
                prefix = next(response.iter_bytes(chunk_size=64), b"")[:64]
                length_value = response.headers.get("Content-Length")
                length = int(length_value) if length_value and length_value.isdigit() else None
                disposition = response.headers.get("Content-Disposition", "")
                valid = (
                    response.status_code == 200
                    and prefix.startswith(b"RIOT")
                    and (length is None or length > 1024)
                    and ".rofl" in disposition.lower()
                )
                capability = Capability.PASS if valid else Capability.FAIL
                results.append(
                    ProbeEvidence(
                        route="B_CURRENT_REPLAY_BACKEND",
                        capability=capability,
                        mechanism=(
                            "Authenticated player-platform match-history-query "
                            f"infoType/replay with explicit {target_platform} match identity"
                        ),
                        http_status=response.status_code,
                        auth_result=(
                            "GLOBAL_ACCOUNT_SESSION_ACCEPTED"
                            if response.status_code == 200
                            else "REJECTED_OR_UNAVAILABLE"
                        ),
                        region_evidence=f"requested {match_id}; client platform {platform}",
                        evidence={
                            "content_type": response.headers.get("Content-Type"),
                            "content_length": length,
                            "content_disposition": disposition,
                            "payload_magic": prefix[:4].decode("ascii", errors="replace"),
                            "content_encoding": response.headers.get("Content-Encoding"),
                            "client_platform": platform,
                        },
                    )
                )
        except (LcuError, httpx.HTTPError, ValueError) as exc:
            results.append(
                ProbeEvidence(
                    route="B_CURRENT_REPLAY_BACKEND",
                    capability=Capability.ACTION_REQUIRED
                    if isinstance(exc, LcuError) and "LOGIN" in exc.code
                    else Capability.FAIL,
                    mechanism="Authenticated player-platform replay lookup",
                    auth_result="UNKNOWN",
                    region_evidence=f"requested {match_id}; client platform {platform}",
                    evidence={"error": type(exc).__name__, "code": getattr(exc, "code", None)},
                )
            )

        route_b_pass = any(
            item.route == "B_CURRENT_REPLAY_BACKEND" and item.capability == Capability.PASS
            for item in results
        )
        results.append(
            ProbeEvidence(
                route="C_OTHER_COMPLETED_REPLAY",
                capability=Capability.UNKNOWN,
                mechanism=(
                    "Not needed because Route B passed" if route_b_pass else "No route proven"
                ),
                evidence={"not_needed": route_b_pass},
            )
        )
        return results

    def download_to(self, match_id: str, partial_path: Path) -> int:
        last_error: ReplayError | None = None
        for attempt in range(self.download_retries):
            try:
                return self._download_once(match_id, partial_path)
            except ReplayError as exc:
                last_error = exc
                if not exc.retryable or attempt + 1 >= self.download_retries:
                    raise
                backoff = min(30.0, float(2**attempt)) + self.rng.uniform(0, 0.5)
                delay = max(backoff, exc.retry_after or 0.0)
                self.sleep(delay)
        assert last_error is not None
        raise last_error

    def _download_once(self, match_id: str, partial_path: Path) -> int:
        self.edge_base = self.edge_base or self.lcu.discover_edge_base()
        headers = self.lcu.edge_headers()
        partial_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with self.edge.stream(
                "GET",
                self.edge_base + self._path(match_id, "replay"),
                headers=headers,
            ) as response:
                if response.status_code == 404:
                    raise ReplayError(
                        "REPLAY_UNAVAILABLE",
                        f"Riot replay backend has no completed replay for {match_id}",
                        http_status=404,
                    )
                if response.status_code in (401, 403):
                    raise ReplayError(
                        "REPLAY_AUTH_FAILED",
                        "League Client session was not accepted by the replay backend",
                        http_status=response.status_code,
                    )
                if response.status_code >= 500:
                    raise ReplayError(
                        "REPLAY_SERVER_ERROR",
                        f"Replay backend returned HTTP {response.status_code}",
                        retryable=True,
                        http_status=response.status_code,
                    )
                if response.status_code == 429:
                    retry_after = _retry_after(
                        response.headers.get("Retry-After"), time.time
                    )
                    raise ReplayError(
                        "REPLAY_RATE_LIMITED",
                        f"Replay backend rate limited the download; retry after "
                        f"{retry_after:.1f}s",
                        retryable=True,
                        http_status=429,
                        retry_after=retry_after,
                    )
                if response.status_code != 200:
                    raise ReplayError(
                        "REPLAY_HTTP_ERROR",
                        f"Replay backend returned HTTP {response.status_code}",
                        http_status=response.status_code,
                    )
                expected_value = response.headers.get("Content-Length")
                expected = (
                    int(expected_value)
                    if expected_value and expected_value.isdigit()
                    else None
                )
                written = 0
                with partial_path.open("wb") as destination:
                    # Match League Client behavior: decode HTTP gzip and persist the official
                    # RIOT container, not the transport wrapper.
                    for chunk in response.iter_bytes(chunk_size=1024 * 1024):
                        if chunk:
                            destination.write(chunk)
                            written += len(chunk)
                    destination.flush()
                    os.fsync(destination.fileno())
                content_encoding = response.headers.get("Content-Encoding", "").lower()
                if expected is not None and not content_encoding and written != expected:
                    raise ReplayError(
                        "REPLAY_TRUNCATED",
                        f"Downloaded {written} bytes, expected {expected}",
                        retryable=True,
                    )
                return written
        except ReplayError:
            raise
        except (httpx.TimeoutException, httpx.NetworkError, httpx.DecodingError) as exc:
            raise ReplayError(
                "REPLAY_NETWORK_ERROR", type(exc).__name__, retryable=True
            ) from exc


def replay_paths(data_dir: Path, row: Any) -> tuple[Path, Path]:
    build = safe_build_component(str(row["game_version_exact"]))
    directory = data_dir / str(row["platform"]) / str(row["patch_key"]) / "builds" / build / "rofl"
    final = directory / f"{row['match_id']}.rofl"
    return final, final.with_suffix(".rofl.partial")


class DownloadManager:
    def __init__(self, db: Database, config: Config, acquirer: ReplayBackendAcquirer):
        self.db = db
        self.config = config
        self.acquirer = acquirer

    @staticmethod
    def _verify_expected_version(row: Any, verification: Verification) -> None:
        if verification.game_version != str(row["game_version_exact"]):
            raise IntegrityError(
                "ROFL_BUILD_MISMATCH",
                f"Replay {row['match_id']} contains build {verification.game_version}",
            )

    def _adopt(self, row: Any, final: Path) -> Verification:
        verification = verify_rofl(final)
        self._verify_expected_version(row, verification)
        relative = final.relative_to(self.config.data_dir).as_posix()
        self.db.record_download(
            str(row["match_id"]),
            file_path=relative,
            file_size=verification.file_size,
            sha256=verification.sha256,
            provider=self.acquirer.provider,
            verification={**verification.as_dict(), "adopted_existing": True},
        )
        return verification

    def _record_corrupt_final(
        self, row: Any, final: Path, run_id: int | None, error: IntegrityError
    ) -> None:
        match_id = str(row["match_id"])
        error_id = self.db.record_error(
            run_id,
            match_id,
            "replay_reconcile",
            error.code,
            error.message,
            retryable=False,
            details={"preserved_path": str(final)},
        )
        state = JobState(str(row["state"]))
        self.db.transition_job(
            match_id,
            state,
            JobState.FAILED_PERMANENT,
            last_error_id=error_id,
        )

    def reconcile(self, dataset_id: int, run_id: int | None = None) -> int:
        adopted = 0
        rows = self.db.connection.execute(
            "SELECT m.*,j.state FROM matches m JOIN replay_jobs j ON j.match_id=m.match_id "
            "WHERE m.dataset_id=? AND j.state!='VERIFIED'",
            (dataset_id,),
        ).fetchall()
        for row in rows:
            final, partial = replay_paths(self.config.data_dir, row)
            if final.is_file():
                try:
                    self._adopt(row, final)
                except IntegrityError as exc:
                    self._record_corrupt_final(row, final, run_id, exc)
                    continue
                adopted += 1
                continue
            if row["state"] == JobState.DOWNLOADING.value:
                self.db.transition_job(
                    str(row["match_id"]),
                    JobState.DOWNLOADING,
                    JobState.FAILED_RETRYABLE,
                    resume_state=JobState.QUEUED,
                )
            # A complete partial can be safely published without another network request.
            if partial.is_file():
                try:
                    verification = verify_rofl(partial)
                    self._verify_expected_version(row, verification)
                except IntegrityError:
                    continue
                final.parent.mkdir(parents=True, exist_ok=True)
                try:
                    os.rename(partial, final)
                except FileExistsError:
                    if not final.is_file():
                        raise
                self._adopt(row, final)
                adopted += 1
        return adopted

    def download(self, row: Any, run_id: int) -> Verification:
        match_id = str(row["match_id"])
        final, partial = replay_paths(self.config.data_dir, row)
        final.parent.mkdir(parents=True, exist_ok=True)
        if final.is_file():
            try:
                return self._adopt(row, final)
            except IntegrityError as exc:
                self._record_corrupt_final(row, final, run_id, exc)
                raise

        state = JobState(str(row["state"]))
        if state in (JobState.ELIGIBLE, JobState.FAILED_RETRYABLE):
            self.db.transition_job(
                match_id, state, JobState.QUEUED, provider=self.acquirer.provider
            )
            state = JobState.QUEUED
        self.db.transition_job(
            match_id,
            state,
            JobState.DOWNLOADING,
            provider=self.acquirer.provider,
            increment_attempts=True,
        )
        try:
            written = self.acquirer.download_to(match_id, partial)
            verification = verify_rofl(partial, expected_size=written)
            self._verify_expected_version(row, verification)
            self.db.transition_job(match_id, JobState.DOWNLOADING, JobState.DOWNLOADED)
            if final.exists():
                raise IntegrityError(
                    "ROFL_FINAL_CONFLICT", f"Refusing to overwrite existing asset {final}"
                )
            os.rename(partial, final)
            relative = final.relative_to(self.config.data_dir).as_posix()
            self.db.record_download(
                match_id,
                file_path=relative,
                file_size=verification.file_size,
                sha256=verification.sha256,
                provider=self.acquirer.provider,
                verification=verification.as_dict(),
            )
            return verification
        except ReplayError as exc:
            error_id = self.db.record_error(
                run_id,
                match_id,
                "replay_download",
                exc.code,
                exc.message,
                retryable=exc.retryable,
                http_status=exc.http_status,
            )
            current = self.db.connection.execute(
                "SELECT state FROM replay_jobs WHERE match_id=?", (match_id,)
            ).fetchone()
            if current and current["state"] in {
                JobState.DOWNLOADING.value,
                JobState.DOWNLOADED.value,
            }:
                if exc.code == "REPLAY_UNAVAILABLE":
                    new_state = JobState.UNAVAILABLE
                    resume = None
                elif exc.retryable:
                    new_state = JobState.FAILED_RETRYABLE
                    resume = JobState.QUEUED
                else:
                    new_state = JobState.FAILED_PERMANENT
                    resume = None
                self.db.transition_job(
                    match_id,
                    JobState(str(current["state"])),
                    new_state,
                    resume_state=resume,
                    last_error_id=error_id,
                )
            raise
