from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import timedelta
from threading import RLock

from lol_collector.models import MatchQueueState, PlayerQueueState, RankStatus, utc_now


@dataclass(frozen=True, slots=True)
class Lease:
    entity_id: int
    lease_owner: str
    lease_expires_at: str
    resume_state: str


@dataclass(frozen=True, slots=True)
class QueueCounts:
    seed_players: int
    discovered_players: int
    master_plus_players: int
    pending_players: int
    discovered_games: int
    unique_games: int
    duplicate_games: int
    summary_success: int
    details_success: int
    failed: int
    waiting_retry: int
    valid_matches: int
    rank_10_of_10: int
    master_plus_8: int
    master_plus_5: int
    queried_players: int


class QueueStore:
    def __init__(self, connection: sqlite3.Connection, lock: RLock) -> None:
        self.connection = connection
        self.lock = lock

    def enqueue_player(self, run_id: int, player_id: int) -> None:
        with self.lock, self.connection:
            self.connection.execute("INSERT INTO player_queue (run_id, player_id, state, rank_status, updated_at) VALUES (?, ?, ?, ?, ?) ON CONFLICT(run_id, player_id) DO NOTHING", (run_id, player_id, PlayerQueueState.NEW.value, RankStatus.UNKNOWN.value, utc_now().isoformat()))

    def lease_player(self, run_id: int, owner: str, seconds: int = 120) -> Lease | None:
        return self._lease("player_queue", "player_id", owner, seconds, (PlayerQueueState.NEW.value, PlayerQueueState.MASTER_PLUS.value, PlayerQueueState.CRAWLING.value, PlayerQueueState.FAILED.value), run_id)

    def lease_match(self, owner: str, seconds: int = 120, run_id: int | None = None) -> Lease | None:
        states = (MatchQueueState.DISCOVERED.value, MatchQueueState.SUMMARY_DOWNLOADED.value, MatchQueueState.DETAILS_DOWNLOADED.value, MatchQueueState.FAILED.value)
        if run_id is None:
            return self._lease("match_queue", "match_id", owner, seconds, states, None)
        now = utc_now()
        expiry = now + timedelta(seconds=seconds)
        placeholders = ",".join("?" for _ in states)
        query = f"SELECT match_id, resume_state FROM match_queue WHERE state IN ({placeholders}) AND match_id IN (SELECT match_id FROM run_match WHERE run_id = ?) AND (next_retry_at IS NULL OR next_retry_at <= ?) AND (lease_expires_at IS NULL OR lease_expires_at <= ?) ORDER BY match_id LIMIT 1"
        with self.lock, self.connection:
            row = self.connection.execute(query, (*states, run_id, now.isoformat(), now.isoformat())).fetchone()
            if row is None:
                return None
            match_id = int(row["match_id"])
            self.connection.execute("UPDATE match_queue SET lease_owner = ?, lease_expires_at = ? WHERE match_id = ?", (owner, expiry.isoformat(), match_id))
            return Lease(match_id, owner, expiry.isoformat(), str(row["resume_state"]))

    def _lease(self, table: str, id_column: str, owner: str, seconds: int, states: tuple[str, ...], run_id: int | None) -> Lease | None:
        now = utc_now()
        expiry = now + timedelta(seconds=seconds)
        placeholders = ",".join("?" for _ in states)
        run_filter = " AND run_id = ?" if run_id is not None else ""
        order_by = "updated_at" if table == "player_queue" else "rowid"
        query = f"SELECT {id_column}, resume_state FROM {table} WHERE state IN ({placeholders}) AND (next_retry_at IS NULL OR next_retry_at <= ?) AND (lease_expires_at IS NULL OR lease_expires_at <= ?){run_filter} ORDER BY {order_by} LIMIT 1"
        params: tuple[str, ...] = (*states, now.isoformat(), now.isoformat())
        if run_id is not None:
            params = (*params, str(run_id))
        with self.lock, self.connection:
            row = self.connection.execute(query, params).fetchone()
            if row is None:
                return None
            identity = int(row[id_column])
            where = f"{id_column} = ?" + (" AND run_id = ?" if run_id is not None else "")
            where_args: tuple[int, ...] = (identity, run_id) if run_id is not None else (identity,)
            self.connection.execute(f"UPDATE {table} SET lease_owner = ?, lease_expires_at = ? WHERE {where}", (owner, expiry.isoformat(), *where_args))
            return Lease(identity, owner, expiry.isoformat(), str(row["resume_state"]))

    def release_player(self, run_id: int, player_id: int, state: PlayerQueueState, next_index: int | None = None, pages_crawled: int | None = None) -> None:
        updates = ["state = ?", "lease_owner = NULL", "lease_expires_at = NULL", "updated_at = ?"]
        values: list[str | int] = [state.value, utc_now().isoformat()]
        if next_index is not None:
            updates.append("history_start_index = ?")
            values.append(next_index)
        if pages_crawled is not None:
            updates.append("pages_crawled = ?")
            values.append(pages_crawled)
        values.extend((run_id, player_id))
        with self.lock, self.connection:
            self.connection.execute(f"UPDATE player_queue SET {', '.join(updates)} WHERE run_id = ? AND player_id = ?", values)

    def retry_player(self, run_id: int, player_id: int, delay_seconds: float) -> None:
        retry_at = utc_now() + timedelta(seconds=delay_seconds)
        with self.lock, self.connection:
            self.connection.execute(
                "UPDATE player_queue SET state = ?, next_retry_at = ?, lease_owner = NULL, lease_expires_at = NULL, updated_at = ? WHERE run_id = ? AND player_id = ?",
                (PlayerQueueState.FAILED.value, retry_at.isoformat(), utc_now().isoformat(), run_id, player_id),
            )

    def set_player_rank_status(self, run_id: int, player_id: int, state: PlayerQueueState, rank_status: RankStatus) -> None:
        with self.lock, self.connection:
            self.connection.execute(
                "UPDATE player_queue SET state = ?, rank_status = ?, updated_at = ?, next_retry_at = NULL WHERE run_id = ? AND player_id = ?",
                (state.value, rank_status.value, utc_now().isoformat(), run_id, player_id),
            )

    def retry_match(self, match_id: int, delay_seconds: float) -> None:
        retry_at = utc_now() + timedelta(seconds=delay_seconds)
        with self.lock, self.connection:
            self.connection.execute(
                "UPDATE match_queue SET state = ?, next_retry_at = ?, lease_owner = NULL, lease_expires_at = NULL WHERE match_id = ?",
                (MatchQueueState.FAILED.value, retry_at.isoformat(), match_id),
            )

    def recover_leases(self, run_id: int) -> None:
        """Clear leases left by a process that exited without a shutdown hook."""
        with self.lock, self.connection:
            self.connection.execute(
                "UPDATE player_queue SET lease_owner = NULL, lease_expires_at = NULL WHERE run_id = ?",
                (run_id,),
            )
            self.connection.execute(
                "UPDATE match_queue SET lease_owner = NULL, lease_expires_at = NULL WHERE match_id IN (SELECT match_id FROM run_match WHERE run_id = ?)",
                (run_id,),
            )

    def counts(self, run_id: int) -> QueueCounts:
        query = "SELECT (SELECT COUNT(*) FROM leaderboard_entry le JOIN leaderboard_snapshot ls ON ls.id = le.snapshot_id WHERE ls.run_id = ?) seed_players, (SELECT COUNT(DISTINCT player_id) FROM player_queue WHERE run_id = ?) discovered_players, (SELECT COUNT(DISTINCT pq.player_id) FROM player_queue pq JOIN player_rank_snapshot prs ON prs.player_id = pq.player_id WHERE pq.run_id = ? AND prs.queue_type = 'RANKED_SOLO_5x5' AND prs.tier IN ('MASTER','GRANDMASTER','CHALLENGER')) master_plus_players, (SELECT COUNT(*) FROM player_queue WHERE run_id = ? AND state IN ('NEW','MASTER_PLUS','CRAWLING','FAILED')) pending_players, (SELECT COUNT(*) FROM match_discovery WHERE run_id = ?) discovered_games, (SELECT COUNT(*) FROM run_match WHERE run_id = ?) unique_games, (SELECT MAX(0, COUNT(*) - COUNT(DISTINCT match_id)) FROM match_discovery WHERE run_id = ?) duplicate_games, (SELECT COUNT(*) FROM match_queue mq JOIN run_match rm ON rm.match_id = mq.match_id WHERE rm.run_id = ? AND mq.state IN ('SUMMARY_DOWNLOADED','DETAILS_DOWNLOADED','PARSED')) summary_success, (SELECT COUNT(*) FROM match_queue mq JOIN run_match rm ON rm.match_id = mq.match_id WHERE rm.run_id = ? AND mq.state IN ('DETAILS_DOWNLOADED','PARSED')) details_success, (SELECT COUNT(*) FROM match_queue mq JOIN run_match rm ON rm.match_id = mq.match_id WHERE rm.run_id = ? AND mq.state = 'FAILED') failed, (SELECT COUNT(*) FROM match_queue mq JOIN run_match rm ON rm.match_id = mq.match_id WHERE rm.run_id = ? AND mq.next_retry_at IS NOT NULL) waiting_retry, (SELECT COUNT(*) FROM run_match WHERE run_id = ? AND target_valid = 1) valid_matches, (SELECT COUNT(*) FROM run_match WHERE run_id = ? AND known_rank_count = 10) rank_10_of_10, (SELECT COUNT(*) FROM run_match WHERE run_id = ? AND master_plus_count >= 8) master_plus_8, (SELECT COUNT(*) FROM run_match WHERE run_id = ? AND master_plus_count >= 5) master_plus_5, (SELECT COUNT(*) FROM player_queue WHERE run_id = ? AND state NOT IN ('NEW')) queried_players"
        with self.lock:
            row = self.connection.execute(query, (run_id,) * 16).fetchone()
            return QueueCounts(*(int(row[key] or 0) for key in QueueCounts.__dataclass_fields__))
