from __future__ import annotations

import sqlite3
from pathlib import Path
from threading import RLock

from lol_collector.artifacts import ArtifactRecord
from lol_collector.match_store import MatchStore
from lol_collector.models import ArtifactType, CollectorConfig, PlayerQueueState, RankSnapshot, RankStatus, RunStatus
from lol_collector.queue_store import Lease, QueueCounts, QueueStore
from lol_collector.replay_store import ReplayStore
from lol_collector.run_store import RunStore
from lol_collector.schema import SCHEMA


class Repository:
    def __init__(self, database_path: Path) -> None:
        database_path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(database_path, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=FULL")
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.lock = RLock()
        self.connection.executescript(SCHEMA)
        self.connection.commit()
        self.runs = RunStore(self.connection, self.lock)
        self.queues = QueueStore(self.connection, self.lock)
        self.matches = MatchStore(self.connection, self.lock)
        self.replays = ReplayStore(self.connection, self.lock)

    def close(self) -> None:
        with self.lock:
            self.connection.close()

    def create_run(self, config: CollectorConfig) -> int:
        return self.runs.create_run(config)

    def set_run_status(self, run_id: int, status: RunStatus) -> None:
        self.runs.set_run_status(run_id, status)

    def add_artifact(self, record: ArtifactRecord) -> int:
        return self.runs.add_artifact(record)

    def record_probe(self, endpoint: str, capability: str, status: str, success: bool, schema_hash: str | None, sanitized_error: str | None, raw_artifact_id: int | None) -> None:
        self.runs.record_probe(endpoint, capability, status, success, schema_hash, sanitized_error, raw_artifact_id)

    def upsert_player(self, region: str, puuid: str | None, summoner_id: str | None = None, riot_game_name: str | None = None, riot_tagline: str | None = None) -> int:
        return self.runs.upsert_player(region, puuid, summoner_id, riot_game_name, riot_tagline)

    def save_rank_snapshot(self, snapshot: RankSnapshot) -> int:
        return self.matches.save_rank_snapshot(snapshot)

    def enqueue_player(self, run_id: int, player_id: int) -> None:
        self.queues.enqueue_player(run_id, player_id)

    def lease_player(self, run_id: int, owner: str, seconds: int = 120) -> Lease | None:
        return self.queues.lease_player(run_id, owner, seconds)

    def lease_match(self, owner: str, seconds: int = 120, run_id: int | None = None) -> Lease | None:
        return self.queues.lease_match(owner, seconds, run_id)

    def retry_match(self, match_id: int, delay_seconds: float) -> None:
        self.queues.retry_match(match_id, delay_seconds)

    def release_player(self, run_id: int, player_id: int, state: PlayerQueueState, next_index: int | None = None, pages_crawled: int | None = None) -> None:
        self.queues.release_player(run_id, player_id, state, next_index, pages_crawled)

    def retry_player(self, run_id: int, player_id: int, delay_seconds: float) -> None:
        self.queues.retry_player(run_id, player_id, delay_seconds)

    def set_player_rank_status(self, run_id: int, player_id: int, state: PlayerQueueState, rank_status: RankStatus) -> None:
        self.queues.set_player_rank_status(run_id, player_id, state, rank_status)

    def recover_leases(self, run_id: int) -> None:
        self.queues.recover_leases(run_id)

    def discover_match(self, run_id: int, player_id: int, sgp_server_id: str, platform_id: str, game_id: str, history_page: int) -> tuple[int, bool]:
        return self.matches.discover_match(run_id, player_id, sgp_server_id, platform_id, game_id, history_page)

    def counts(self, run_id: int) -> QueueCounts:
        return self.queues.counts(run_id)

    def update_quality(self, run_id: int, match_id: int, queue_valid: bool, patch_valid: bool, time_valid: bool, participant_count: int, known_rank_count: int, master_count: int, grandmaster_count: int, challenger_count: int, below_master_count: int, required_known: int = 10, threshold: int = 8) -> None:
        self.matches.update_quality(run_id, match_id, queue_valid, patch_valid, time_valid, participant_count, known_rank_count, master_count, grandmaster_count, challenger_count, below_master_count, required_known, threshold)

    def attach_artifact(self, match_id: int, artifact_type: ArtifactType, artifact_id: int) -> None:
        self.matches.attach_artifact(match_id, artifact_type, artifact_id)

    def mark_parsed(self, match_id: int) -> None:
        self.matches.mark_parsed(match_id)
