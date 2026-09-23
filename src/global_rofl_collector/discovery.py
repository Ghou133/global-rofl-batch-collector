from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass

from .db import Database
from .errors import RiotApiError
from .models import MatchRecord
from .platforms import platform_route
from .riot import RANKED_SOLO_QUEUE_ID, RiotApi, parse_match


@dataclass(frozen=True, slots=True)
class DiscoveryResult:
    ladder_counts: dict[str, int]
    histories_queried: int
    details_queried: int
    unique_discovered: int
    eligible_candidates: int


class DiscoveryService:
    def __init__(
        self,
        db: Database,
        api: RiotApi,
        *,
        history_count: int = 20,
        progress: Callable[[str], None] | None = None,
        platform: str = "KR",
    ):
        self.db = db
        self.api = api
        self.history_count = history_count
        self.progress = progress or (lambda _: None)
        self.platform = platform_route(platform).platform

    def _target_match_ids(self, match_ids: Iterable[str]) -> list[str]:
        prefix = f"{self.platform}_"
        return [match_id for match_id in match_ids if match_id.startswith(prefix)]

    def refresh_ladders(self, dataset_id: int, run_id: int) -> dict[str, int]:
        counts: dict[str, int] = {}
        for tier in ("CHALLENGER", "GRANDMASTER", "MASTER"):
            entries = self.api.ladder(tier)
            count = self.db.upsert_players(dataset_id, run_id, tier, entries)
            counts[tier] = count
            self.progress(f"LADDER {tier}: {count}")
        self.db.recompute_all_quality(dataset_id)
        return counts

    @staticmethod
    def eligible(record: MatchRecord, current_patch: str, platform: str = "KR") -> bool:
        return (
            record.platform == platform_route(platform).platform
            and record.queue_id == RANKED_SOLO_QUEUE_ID
            and record.patch == current_patch
            and record.game_duration > 0
            and bool((record.raw.get("info") or {}).get("gameEndTimestamp"))
        )

    def discover(
        self,
        dataset_id: int,
        run_id: int,
        current_patch: str,
        wanted_candidates: int,
    ) -> DiscoveryResult:
        ladder_counts = self.refresh_ladders(dataset_id, run_id)
        tiers = self.db.player_tiers(dataset_id)
        histories_queried = 0
        details_queried = 0
        queried_puuids: set[str] = set()

        def detail_queried() -> None:
            nonlocal details_queried
            details_queried += 1

        known_ids = set(self._target_match_ids(self.db.discovered_match_ids(dataset_id)))
        needed_unique = max(len(known_ids), wanted_candidates)
        if len(known_ids) < needed_unique:
            for tier in ("CHALLENGER", "GRANDMASTER", "MASTER"):
                for player in self.db.player_rows(dataset_id, tier):
                    puuid = str(player["puuid"])
                    match_ids = self._target_match_ids(
                        self.api.match_ids(puuid, count=self.history_count)
                    )
                    histories_queried += 1
                    queried_puuids.add(puuid)
                    self.db.add_discoveries(dataset_id, run_id, puuid, match_ids)
                    known_ids.update(match_ids)
                    if len(known_ids) >= needed_unique:
                        break
                if len(known_ids) >= needed_unique:
                    break

        eligible_count = self._ingest_details(
            dataset_id,
            current_patch,
            tiers,
            known_ids,
            wanted_candidates,
            on_detail=detail_queried,
        )

        # Patch transitions or unavailable games can make a first pool too small. Expand in
        # small history batches, Challenger -> GM -> Master, and stop as soon as backlog is enough.
        if eligible_count < wanted_candidates:
            for tier in ("CHALLENGER", "GRANDMASTER", "MASTER"):
                for player in self.db.player_rows(dataset_id, tier):
                    puuid = str(player["puuid"])
                    match_ids = self._target_match_ids(
                        self.api.match_ids(
                            puuid,
                            count=self.history_count,
                            start=self.history_count if puuid in queried_puuids else 0,
                        )
                    )
                    histories_queried += 1
                    queried_puuids.add(puuid)
                    self.db.add_discoveries(dataset_id, run_id, puuid, match_ids)
                    new_ids = set(match_ids) - known_ids
                    known_ids.update(match_ids)
                    if new_ids:
                        eligible_count = self._ingest_details(
                            dataset_id,
                            current_patch,
                            tiers,
                            new_ids,
                            wanted_candidates,
                            on_detail=detail_queried,
                        )
                    eligible_count = len(self.db.candidates(dataset_id))
                    if eligible_count >= wanted_candidates:
                        break
                if eligible_count >= wanted_candidates:
                    break

        self.db.recompute_all_quality(dataset_id)
        eligible_count = len(self.db.candidates(dataset_id))
        self.progress(
            f"DISCOVERY raw={self.db.stats(dataset_id)['raw_match_discoveries']} "
            f"unique={len(known_ids)} eligible={eligible_count}"
        )
        return DiscoveryResult(
            ladder_counts=ladder_counts,
            histories_queried=histories_queried,
            details_queried=details_queried,
            unique_discovered=len(known_ids),
            eligible_candidates=eligible_count,
        )

    def _ingest_details(
        self,
        dataset_id: int,
        current_patch: str,
        tiers: dict[str, str],
        match_ids: set[str],
        wanted_candidates: int,
        on_detail: Callable[[], None],
    ) -> int:
        eligible = len(self.db.candidates(dataset_id))
        if eligible >= wanted_candidates:
            return eligible
        ordered = sorted(match_ids, reverse=True)
        for match_id in ordered:
            if not match_id.startswith(f"{self.platform}_"):
                continue
            if self.db.match_exists(match_id):
                continue
            on_detail()
            try:
                record = parse_match(self.api.match(match_id), tiers)
            except RiotApiError as exc:
                if exc.code == "API_NOT_FOUND":
                    continue
                raise
            if record.match_id != match_id or record.platform != self.platform:
                raise RiotApiError(
                    "API_SCHEMA_CHANGED",
                    f"Match-V5 identity for {match_id} conflicts with {self.platform}",
                )
            is_eligible = self.eligible(record, current_patch, self.platform)
            self.db.store_match(dataset_id, record, is_eligible)
            if is_eligible:
                eligible += 1
            if eligible >= wanted_candidates:
                break
        return eligible
