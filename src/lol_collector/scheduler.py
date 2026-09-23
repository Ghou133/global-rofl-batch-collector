from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from lol_collector.models import CollectorConfig, PlayerQueueState, RunStatus
from lol_collector.repository import Repository


@dataclass(frozen=True, slots=True)
class SchedulerDecision:
    stopped: bool
    status: RunStatus
    valid_matches: int
    target_matches: int


@dataclass(frozen=True, slots=True)
class HistoryPage:
    page_index: int
    next_start_index: int
    game_ids: tuple[str, ...]


class CollectionScheduler:
    def __init__(self, repository: Repository, config: CollectorConfig, run_id: int) -> None:
        self.repository = repository
        self.config = config
        self.run_id = run_id

    def decide(self) -> SchedulerDecision:
        counts = self.repository.counts(self.run_id)
        if counts.valid_matches >= self.config.target_valid_matches:
            self.repository.set_run_status(self.run_id, RunStatus.COMPLETE)
            return SchedulerDecision(True, RunStatus.COMPLETE, counts.valid_matches, self.config.target_valid_matches)
        if counts.pending_players == 0 and counts.unique_games == 0 and counts.seed_players == 0:
            self.repository.set_run_status(self.run_id, RunStatus.EXHAUSTED)
            return SchedulerDecision(True, RunStatus.EXHAUSTED, counts.valid_matches, self.config.target_valid_matches)
        self.repository.set_run_status(self.run_id, RunStatus.RUNNING)
        return SchedulerDecision(False, RunStatus.RUNNING, counts.valid_matches, self.config.target_valid_matches)

    def add_history_page(self, player_id: int, page: HistoryPage, sgp_server_id: str, platform_id: str) -> int:
        inserted = 0
        for game_id in page.game_ids:
            _, is_new = self.repository.discover_match(self.run_id, player_id, sgp_server_id, platform_id, game_id, page.page_index)
            inserted += int(is_new)
        self.repository.release_player(self.run_id, player_id, PlayerQueueState.CRAWLING, page.next_start_index, page.page_index + 1)
        return inserted

    def owner(self) -> str:
        return hashlib.sha256(f"{self.run_id}:{self.config.seed}".encode()).hexdigest()[:16]

    def reconcile_raw(self) -> int:
        rows = self.repository.connection.execute("SELECT id, filesystem_path, sha256, byte_size FROM raw_artifact").fetchall()
        broken = 0
        for row in rows:
            path = Path(str(row["filesystem_path"]))
            if not path.is_file() or path.stat().st_size != int(row["byte_size"]):
                broken += 1
                continue
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            broken += int(digest != row["sha256"])
        if broken:
            self.repository.set_run_status(self.run_id, RunStatus.PAUSED_STORAGE_ERROR)
        return broken
