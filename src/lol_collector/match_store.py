from __future__ import annotations

import sqlite3
from threading import RLock
from typing import TYPE_CHECKING

from lol_collector.models import ArtifactType, MatchQueueState, RankSnapshot, normalize_patch, utc_now

if TYPE_CHECKING:
    from lol_collector.collection_support import MatchSummary


class MatchStore:
    def __init__(self, connection: sqlite3.Connection, lock: RLock) -> None:
        self.connection = connection
        self.lock = lock

    def discover_match(self, run_id: int, player_id: int, sgp_server_id: str, platform_id: str, game_id: str, history_page: int) -> tuple[int, bool]:
        now = utc_now().isoformat()
        with self.lock, self.connection:
            row = self.connection.execute("SELECT id FROM match WHERE sgp_server_id = ? AND game_id = ?", (sgp_server_id, game_id)).fetchone()
            is_new = row is None
            if row is None:
                cursor = self.connection.execute("INSERT INTO match (sgp_server_id, platform_id, game_id) VALUES (?, ?, ?)", (sgp_server_id, platform_id, game_id))
                match_id = int(cursor.lastrowid)
                self.connection.execute("INSERT INTO match_queue (match_id, state) VALUES (?, ?)", (match_id, MatchQueueState.DISCOVERED.value))
            else:
                match_id = int(row["id"])
            self.connection.execute("INSERT INTO match_discovery (match_id, discovered_by_player_id, run_id, history_page, discovered_at) VALUES (?, ?, ?, ?, ?) ON CONFLICT DO NOTHING", (match_id, player_id, run_id, history_page, now))
            self.connection.execute("INSERT INTO run_match (run_id, match_id, discovered_at) VALUES (?, ?, ?) ON CONFLICT DO NOTHING", (run_id, match_id, now))
            return match_id, is_new

    def save_rank_snapshot(self, snapshot: RankSnapshot) -> int:
        with self.lock, self.connection:
            cursor = self.connection.execute("INSERT INTO player_rank_snapshot (player_id, observed_at, source, region, queue_type, tier, division, lp, wins, losses, raw_artifact_id, schema_version) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (snapshot.player_id, snapshot.observed_at.isoformat(), snapshot.source, snapshot.region, snapshot.queue_type, snapshot.tier, snapshot.division, snapshot.lp, snapshot.wins, snapshot.losses, snapshot.raw_artifact_id, "v1"))
            return int(cursor.lastrowid)

    def update_quality(self, run_id: int, match_id: int, queue_valid: bool, patch_valid: bool, time_valid: bool, participant_count: int, known_rank_count: int, master_count: int, grandmaster_count: int, challenger_count: int, below_master_count: int, required_known: int = 10, threshold: int = 8) -> None:
        master_plus = master_count + grandmaster_count + challenger_count
        target_valid = queue_valid and patch_valid and time_valid and participant_count == 10 and known_rank_count >= required_known and master_plus >= threshold
        with self.lock, self.connection:
            self.connection.execute(
                "UPDATE run_match SET queue_valid = ?, patch_valid = ?, time_valid = ?, known_rank_count = ?, unknown_rank_count = ?, master_count = ?, grandmaster_count = ?, challenger_count = ?, below_master_count = ?, master_plus_count = ?, target_valid = ? WHERE run_id = ? AND match_id = ?",
                (int(queue_valid), int(patch_valid), int(time_valid), known_rank_count, max(0, participant_count - known_rank_count), master_count, grandmaster_count, challenger_count, below_master_count, master_plus, int(target_valid), run_id, match_id),
            )

    def update_metadata(self, match_id: int, summary: MatchSummary) -> None:
        values = (
            summary.game_start_at.isoformat() if summary.game_start_at is not None else None,
            summary.game_end_at.isoformat() if summary.game_end_at is not None else None,
            summary.queue_id,
            summary.game_mode,
            summary.map_id,
            summary.game_version,
            normalize_patch(summary.game_version) if summary.game_version else None,
            match_id,
        )
        with self.lock, self.connection:
            self.connection.execute(
                "UPDATE match SET game_start_at = ?, game_end_at = ?, queue_id = ?, game_mode = ?, map_id = ?, game_version_full = ?, patch_normalized = ? WHERE id = ?",
                values,
            )

    def save_participant(self, match_id: int, participant_id: int, player_id: int, puuid: str, rank_snapshot_id: int | None) -> None:
        with self.lock, self.connection:
            self.connection.execute(
                "INSERT INTO match_participant (match_id, participant_id, player_id, puuid, rank_snapshot_id_used) VALUES (?, ?, ?, ?, ?) ON CONFLICT(match_id, participant_id) DO UPDATE SET player_id = excluded.player_id, rank_snapshot_id_used = excluded.rank_snapshot_id_used",
                (match_id, participant_id, player_id, puuid, rank_snapshot_id),
            )

    def attach_artifact(self, match_id: int, artifact_type: ArtifactType, artifact_id: int) -> None:
        match artifact_type:
            case ArtifactType.SUMMARY:
                column = "summary_artifact_id"
                state = MatchQueueState.SUMMARY_DOWNLOADED
            case ArtifactType.DETAILS:
                column = "details_artifact_id"
                state = MatchQueueState.DETAILS_DOWNLOADED
            case unreachable:
                raise ValueError(f"invalid match artifact type: {unreachable}")
        with self.lock, self.connection:
            self.connection.execute(f"UPDATE match SET {column} = ? WHERE id = ?", (artifact_id, match_id))
            self.connection.execute("UPDATE match_queue SET state = ?, resume_state = ?, next_retry_at = NULL, lease_owner = NULL, lease_expires_at = NULL WHERE match_id = ?", (state.value, state.value, match_id))

    def mark_parsed(self, match_id: int) -> None:
        with self.lock, self.connection:
            self.connection.execute("UPDATE match SET parsed_at = ? WHERE id = ?", (utc_now().isoformat(), match_id))
            self.connection.execute("UPDATE match_queue SET state = ?, resume_state = ?, next_retry_at = NULL, lease_owner = NULL, lease_expires_at = NULL WHERE match_id = ?", (MatchQueueState.PARSED.value, MatchQueueState.PARSED.value, match_id))
