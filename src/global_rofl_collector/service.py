from __future__ import annotations

import json
import math
import os
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .config import Config
from .db import Database
from .discovery import DiscoveryService
from .errors import CollectorError, ConfigurationError, IntegrityError, ReplayError
from .locking import CollectorLock
from .maintenance import normalize_http_gzip_replays
from .manifest import manifest_path, write_manifest
from .models import Capability
from .platforms import platform_route
from .replay import DownloadManager, ReplayBackendAcquirer, verify_rofl
from .riot import PatchResolver, RiotApi, parse_match


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


class CollectorService:
    def __init__(self, config: Config, *, progress: Callable[[str], None] | None = None):
        self.config = config
        self.progress = progress or (lambda _: None)
        self.config.ensure_directories()
        self.db = Database(config.db_path)
        self.db.migrate()

    def close(self) -> None:
        self.db.close()

    def __enter__(self) -> CollectorService:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _patch(self) -> tuple[str, str]:
        resolver = PatchResolver(
            platform=self.config.platform, timeout=self.config.request_timeout
        )
        try:
            return resolver.resolve()
        finally:
            resolver.close()

    def probe(self) -> dict[str, Any]:
        with CollectorLock(self.config.data_dir / ".collector.lock"):
            return self._probe_locked()

    def _probe_locked(self) -> dict[str, Any]:
        platform = self.config.platform
        match_region = platform_route(platform).match_region
        result: dict[str, Any] = {
            "PLATFORM": platform,
            "MATCH_REGION": match_region,
            "RIOT_API_KEY": "FAIL",
            "LADDER_API": "NOT_TESTED",
            "MATCH_API": "NOT_TESTED",
            "CURRENT_PATCH": "UNKNOWN",
            "LEAGUE_CLIENT": "NOT_TESTED",
            "LCU": "NOT_TESTED",
            "REPLAY_ACQUISITION": "UNKNOWN",
            "DATABASE": "PASS",
            "STORAGE": "PASS",
            "TARGET_PLATFORM_ACCOUNT_REQUIRED": "UNKNOWN",
        }
        if platform == "KR":
            result.update(
                KR_LADDER_API="NOT_TESTED",
                ASIA_MATCH_API="NOT_TESTED",
                KR_ACCOUNT_REQUIRED="UNKNOWN",
            )
        patch, realm_version = self._patch()
        result["CURRENT_PATCH"] = patch
        dataset_id = self.db.dataset(platform, patch, realm_version)
        result["EXISTING_DATASET"] = f"{self.db.verified_count(dataset_id)} VERIFIED"
        self.db.interrupt_stale_runs(dataset_id)
        run_id = self.db.start_run(dataset_id, "probe")
        api: RiotApi | None = None
        acquirer: ReplayBackendAcquirer | None = None
        try:
            key = self.config.require_api_key()
            result["RIOT_API_KEY"] = "PASS"
            api = RiotApi(
                key,
                timeout=self.config.request_timeout,
                min_interval=self.config.api_min_interval,
                max_retries=self.config.api_max_retries,
                platform=platform,
            )
            challenger = api.ladder("CHALLENGER")
            grandmaster = api.ladder("GRANDMASTER")
            self.db.upsert_players(dataset_id, run_id, "CHALLENGER", challenger)
            self.db.upsert_players(dataset_id, run_id, "GRANDMASTER", grandmaster)
            result["LADDER_API"] = "PASS"
            if platform == "KR":
                result["KR_LADDER_API"] = "PASS"
            result["CHALLENGER_PLAYERS"] = len(challenger)
            result["GRANDMASTER_PLAYERS"] = len(grandmaster)

            top = max(challenger, key=lambda item: int(item.get("leaguePoints", 0)))
            match_ids = api.match_ids(str(top["puuid"]), count=self.config.history_count)
            match_ids = [item for item in match_ids if item.startswith(f"{platform}_")]
            if not match_ids:
                raise CollectorError(
                    "NO_RECENT_MATCH",
                    f"Top Challenger has no recent {platform} queue 420 match",
                )
            raw_match = api.match(match_ids[0])
            record = parse_match(raw_match, self.db.player_tiers(dataset_id))
            if record.match_id != match_ids[0] or record.platform != platform:
                raise CollectorError(
                    "API_SCHEMA_CHANGED",
                    f"Match-V5 returned a different identity for requested {platform} match",
                )
            eligible = DiscoveryService.eligible(record, patch, platform)
            self.db.add_discoveries(dataset_id, run_id, str(top["puuid"]), [record.match_id])
            self.db.store_match(dataset_id, record, eligible)
            result["MATCH_API"] = "PASS"
            if platform == "KR":
                result["ASIA_MATCH_API"] = "PASS"
            result["SAMPLE_MATCH"] = record.match_id
            result["SAMPLE_BUILD"] = record.game_version

            acquirer = ReplayBackendAcquirer(self.config)
            result["LEAGUE_CLIENT"] = "PASS"
            result["LCU"] = "PASS"
            probes = acquirer.probe(record.match_id, record.game_id)
            for evidence in probes:
                self.db.record_probe(run_id, record.match_id, patch, evidence)
            route_b = next(
                (probe for probe in probes if probe.route == "B_CURRENT_REPLAY_BACKEND"), None
            )
            if route_b and route_b.capability == Capability.PASS:
                result["REPLAY_ACQUISITION"] = "PASS"
                client_platform = route_b.evidence.get("client_platform")
                requirement = "NO" if client_platform and client_platform != platform else "UNKNOWN"
                result["TARGET_PLATFORM_ACCOUNT_REQUIRED"] = requirement
                if platform == "KR":
                    result["KR_ACCOUNT_REQUIRED"] = requirement
            elif any(probe.capability == Capability.ACTION_REQUIRED for probe in probes):
                result["REPLAY_ACQUISITION"] = "ACTION_REQUIRED"
            else:
                result["REPLAY_ACQUISITION"] = "FAIL"
            result["REPLAY_ROUTES"] = [
                {
                    "route": probe.route,
                    "capability": probe.capability.value,
                    "http_status": probe.http_status,
                    "mechanism": probe.mechanism,
                    "region_evidence": probe.region_evidence,
                    "evidence": probe.evidence,
                }
                for probe in probes
            ]
            status = "PASS" if result["REPLAY_ACQUISITION"] == "PASS" else "BLOCKED"
            self.db.finish_run(
                run_id,
                status,
                capability=result["REPLAY_ACQUISITION"],
                summary=result,
            )
            return result
        except CollectorError as exc:
            self.db.record_error(
                run_id,
                None,
                "probe",
                exc.code,
                exc.message,
                retryable=exc.retryable,
                http_status=exc.http_status,
            )
            self.db.finish_run(run_id, "FAILED", summary={"error": exc.code})
            raise
        finally:
            if acquirer:
                acquirer.close()
            if api:
                api.close()

    def run(self, target: int) -> dict[str, Any]:
        if target <= 0:
            raise ConfigurationError("INVALID_TARGET", "--target must be a positive integer")
        with CollectorLock(self.config.data_dir / ".collector.lock"):
            return self._run_locked(target)

    def _run_locked(self, target: int) -> dict[str, Any]:
        platform = self.config.platform
        match_region = platform_route(platform).match_region
        patch, realm_version = self._patch()
        dataset_id = self.db.dataset(platform, patch, realm_version)
        self.db.interrupt_stale_runs(dataset_id)
        run_id = self.db.start_run(dataset_id, "run", target)
        api: RiotApi | None = None
        acquirer: ReplayBackendAcquirer | None = None
        summary: dict[str, Any] = {
            "platform": platform,
            "match_region": match_region,
            "patch": patch,
            "target": target,
            "run_id": run_id,
        }
        try:
            verified_before = self.db.verified_count(dataset_id)
            summary["verified_before"] = verified_before
            if verified_before >= target:
                path = write_manifest(self.db, dataset_id, self.config.data_dir, patch, platform)
                summary.update(
                    {
                        "status": "TARGET_ALREADY_MET",
                        "verified": verified_before,
                        "manifest": str(path),
                    }
                )
                self.db.finish_run(run_id, "TARGET_REACHED", capability="PASS", summary=summary)
                return summary

            key = self.config.require_api_key()
            api = RiotApi(
                key,
                timeout=self.config.request_timeout,
                min_interval=self.config.api_min_interval,
                max_retries=self.config.api_max_retries,
                platform=platform,
            )
            missing = target - verified_before
            existing_backlog = self.db.recoverable_backlog_count(dataset_id)
            desired_backlog = missing + max(20, math.ceil(missing * 0.25))
            discovery = DiscoveryService(
                self.db,
                api,
                history_count=self.config.history_count,
                progress=self.progress,
                platform=platform,
            ).discover(
                dataset_id,
                run_id,
                patch,
                max(existing_backlog, desired_backlog),
            )
            summary["discovery"] = asdict(discovery)
            candidates = [
                row
                for row in self.db.candidates(dataset_id)
                if str(row["platform"]).upper() == platform
                and str(row["match_id"]).startswith(f"{platform}_")
            ]
            if not candidates:
                raise CollectorError(
                    "NO_ELIGIBLE_MATCHES",
                    f"No completed current-patch {platform} queue 420 matches were found",
                )

            acquirer = ReplayBackendAcquirer(self.config)
            sample = candidates[0]
            probes = acquirer.probe(str(sample["match_id"]), str(sample["game_id"]))
            for evidence in probes:
                self.db.record_probe(run_id, str(sample["match_id"]), patch, evidence)
            route_b = next(
                (probe for probe in probes if probe.route == "B_CURRENT_REPLAY_BACKEND"), None
            )
            if route_b is None or route_b.capability != Capability.PASS:
                capability = route_b.capability.value if route_b else Capability.UNKNOWN.value
                raise CollectorError(
                    "COMPLETED_REPLAY_UNAVAILABLE",
                    f"Completed replay Route B did not pass ({capability})",
                )
            summary["replay_route"] = "B_CURRENT_REPLAY_BACKEND"
            client_platform = route_b.evidence.get("client_platform")
            requirement = "NO" if client_platform and client_platform != platform else "UNKNOWN"
            summary["target_platform_account_required"] = requirement
            if platform == "KR":
                summary["kr_account_required"] = requirement

            manager = DownloadManager(self.db, self.config, acquirer)
            adopted = manager.reconcile(dataset_id, run_id)
            summary["reconciled_downloads"] = adopted
            verified = self.db.verified_count(dataset_id)
            downloaded_this_run = 0
            for candidate in self.db.candidates(dataset_id):
                if verified >= target:
                    break
                try:
                    verification = manager.download(candidate, run_id)
                except ReplayError as exc:
                    self.progress(
                        f"REPLAY {candidate['match_id']}: {exc.code}"
                    )
                    if exc.code in {"REPLAY_AUTH_FAILED"}:
                        raise
                    continue
                downloaded_this_run += 1
                verified += 1
                if downloaded_this_run == 1 or downloaded_this_run % 5 == 0 or verified == target:
                    self.progress(
                        f"VERIFIED {verified}/{target}: {candidate['match_id']} "
                        f"({verification.file_size / 1024 / 1024:.1f} MiB)"
                    )
                if downloaded_this_run % 10 == 0:
                    write_manifest(self.db, dataset_id, self.config.data_dir, patch, platform)

            path = write_manifest(self.db, dataset_id, self.config.data_dir, patch, platform)
            verified = self.db.verified_count(dataset_id)
            summary.update(
                {
                    "verified": verified,
                    "downloaded_this_run": downloaded_this_run,
                    "manifest": str(path),
                    "stats": self.db.stats(dataset_id),
                }
            )
            if verified < target:
                summary["status"] = "CANDIDATE_POOL_EXHAUSTED"
                self.db.finish_run(run_id, "INCOMPLETE", capability="PASS", summary=summary)
                raise CollectorError(
                    "TARGET_NOT_REACHED",
                    f"Verified {verified}/{target}; more discovery candidates are required",
                    retryable=True,
                )
            summary["status"] = "TARGET_REACHED"
            self._write_run_report(run_id, patch, summary)
            self.db.finish_run(run_id, "TARGET_REACHED", capability="PASS", summary=summary)
            return summary
        except CollectorError as exc:
            error_id = self.db.record_error(
                run_id,
                None,
                "run",
                exc.code,
                exc.message,
                retryable=exc.retryable,
                http_status=exc.http_status,
            )
            summary.update({"status": "FAILED", "error": exc.code, "error_id": error_id})
            self._write_run_report(run_id, patch, summary)
            current = self.db.connection.execute(
                "SELECT status FROM runs WHERE id=?", (run_id,)
            ).fetchone()
            if current and current["status"] == "RUNNING":
                self.db.finish_run(run_id, "FAILED", summary=summary)
            raise
        except BaseException as exc:
            self.db.record_error(
                run_id,
                None,
                "run",
                "UNEXPECTED_ERROR",
                type(exc).__name__,
                retryable=False,
            )
            summary.update({"status": "FAILED", "error": type(exc).__name__})
            self._write_run_report(run_id, patch, summary)
            self.db.finish_run(run_id, "FAILED", summary=summary)
            raise
        finally:
            if acquirer:
                acquirer.close()
            if api:
                api.close()

    def _write_run_report(self, run_id: int, patch: str, summary: dict[str, Any]) -> Path:
        path = (
            self.config.data_dir / self.config.platform / patch / "reports" / f"run_{run_id}.json"
        )
        _atomic_json(path, summary)
        return path

    def status(self, *, verify_files: bool = False) -> dict[str, Any]:
        platform = self.config.platform
        dataset = self.db.latest_dataset(platform)
        if dataset is None:
            return {
                "initialized": True,
                "platform": platform,
                "current_patch": "UNKNOWN",
                "message": f"No {platform} dataset has been created. Run collector probe first.",
            }
        dataset_id = int(dataset["id"])
        stats = self.db.stats(dataset_id)
        result: dict[str, Any] = {
            "initialized": True,
            "platform": platform,
            "current_patch": dataset["patch_key"],
            "realm_version": dataset["exact_realm_version"],
            **stats,
            "database": str(self.config.db_path),
            "rofl_root": str(self.config.data_dir / platform / dataset["patch_key"] / "builds"),
            "manifest": str(manifest_path(self.config.data_dir, dataset["patch_key"], platform)),
            "logs": str(self.config.logs_dir),
            "reports": str(
                self.config.data_dir / platform / dataset["patch_key"] / "reports"
            ),
        }
        if verify_files:
            errors: list[str] = []
            for row in self.db.manifest_rows(dataset_id):
                path = self.config.data_dir / row["file_path"]
                try:
                    check = verify_rofl(path, expected_size=int(row["file_size"]))
                    if check.game_version != str(row["game_version_exact"]):
                        raise IntegrityError("ROFL_BUILD_MISMATCH", str(path))
                    if check.sha256 != row["sha256"]:
                        raise IntegrityError("ROFL_HASH_MISMATCH", str(path))
                except CollectorError as exc:
                    errors.append(f"{row['match_id']}: {exc.code}")
            result["integrity_audit"] = "PASS" if not errors else "FAIL"
            result["integrity_errors"] = errors
        return result

    def normalize_http_gzip(self) -> dict[str, Any]:
        with CollectorLock(self.config.data_dir / ".collector.lock"):
            dataset = self.db.latest_dataset(self.config.platform)
            if dataset is None:
                raise ConfigurationError(
                    "DATASET_MISSING",
                    f"No {self.config.platform} dataset exists; run collector probe first",
                )
            return normalize_http_gzip_replays(
                self.db, self.config, dataset, progress=self.progress
            )
