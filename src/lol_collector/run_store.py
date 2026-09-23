from __future__ import annotations

import json
import sqlite3
from threading import RLock

from lol_collector.artifacts import ArtifactRecord
from lol_collector.models import CollectorConfig, RankSnapshot, RunStatus, utc_now


class RunStore:
    def __init__(self, connection: sqlite3.Connection, lock: RLock) -> None:
        self.connection = connection
        self.lock = lock

    def create_run(self, config: CollectorConfig) -> int:
        now = utc_now().isoformat()
        values = (now, RunStatus.CREATED.value, config.region, config.target_patch, config.window_start_at.isoformat(), config.window_end_at.isoformat(), json.dumps(config.target_tiers), config.target_valid_matches, config.master_plus_threshold, config.require_known_rank_count, config.seed)
        with self.lock, self.connection:
            cursor = self.connection.execute(
                "INSERT INTO collection_run (created_at, status, region, target_patch, window_start_at, window_end_at, selected_tiers, target_valid_matches, master_plus_threshold, require_known_rank_count, scheduler_rng_seed) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                values,
            )
            return int(cursor.lastrowid)

    def set_run_status(self, run_id: int, status: RunStatus) -> None:
        with self.lock, self.connection:
            self.connection.execute("UPDATE collection_run SET status = ?, started_at = COALESCE(started_at, ?) WHERE id = ?", (status.value, utc_now().isoformat(), run_id))

    def add_artifact(self, record: ArtifactRecord) -> int:
        with self.lock, self.connection:
            existing = self.connection.execute(
                "SELECT id FROM raw_artifact WHERE filesystem_path = ?",
                (record.filesystem_path,),
            ).fetchone()
            if existing is not None:
                return int(existing["id"])
            cursor = self.connection.execute(
                "INSERT INTO raw_artifact (artifact_type, owner_type, owner_id, fetched_at, endpoint_template, http_status, byte_size, sha256, filesystem_path, schema_hash, artifact_version) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (record.artifact_type.value, record.owner_type, record.owner_id, record.fetched_at, record.endpoint_template, record.http_status, record.byte_size, record.sha256, record.filesystem_path, record.schema_hash, record.artifact_version),
            )
            return int(cursor.lastrowid)

    def record_probe(self, endpoint: str, capability: str, status: str, success: bool, schema_hash: str | None, sanitized_error: str | None, raw_artifact_id: int | None) -> None:
        with self.lock, self.connection:
            self.connection.execute(
                "INSERT INTO capability_probe (probe_time, endpoint, capability, status, success, schema_hash, sanitized_error, raw_artifact_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (utc_now().isoformat(), endpoint, capability, status, int(success), schema_hash, sanitized_error, raw_artifact_id),
            )

    def upsert_player(self, region: str, puuid: str | None, summoner_id: str | None = None, riot_game_name: str | None = None, riot_tagline: str | None = None) -> int:
        now = utc_now().isoformat()
        with self.lock, self.connection:
            existing = self.connection.execute("SELECT id FROM player WHERE region = ? AND puuid IS ?", (region, puuid)).fetchone()
            if existing is not None:
                self.connection.execute("UPDATE player SET summoner_id = COALESCE(?, summoner_id), riot_game_name = COALESCE(?, riot_game_name), riot_tagline = COALESCE(?, riot_tagline), last_seen_at = ? WHERE id = ?", (summoner_id, riot_game_name, riot_tagline, now, existing["id"]))
                return int(existing["id"])
            cursor = self.connection.execute(
                "INSERT INTO player (region, puuid, summoner_id, riot_game_name, riot_tagline, first_seen_at, last_seen_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (region, puuid, summoner_id, riot_game_name, riot_tagline, now, now),
            )
            return int(cursor.lastrowid)

    def save_rank_snapshot(self, snapshot: RankSnapshot) -> int:
        with self.lock, self.connection:
            cursor = self.connection.execute(
                "INSERT INTO player_rank_snapshot (player_id, observed_at, source, region, queue_type, tier, division, lp, wins, losses, raw_artifact_id, schema_version) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (snapshot.player_id, snapshot.observed_at.isoformat(), snapshot.source, snapshot.region, snapshot.queue_type, snapshot.tier, snapshot.division, snapshot.lp, snapshot.wins, snapshot.losses, snapshot.raw_artifact_id, "v1"),
            )
            return int(cursor.lastrowid)
