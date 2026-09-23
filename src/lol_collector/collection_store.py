from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from lol_collector.artifacts import ArtifactRecord
from lol_collector.collection_support import MatchSummary
from lol_collector.models import (
    JsonObject,
    JsonValue,
    PlayerId,
    PlayerQueueState,
    RankSnapshot,
    RankStatus,
    sanitize_json,
    utc_now,
)
from lol_collector.repository import Repository


SOLO_QUEUE: Final[str] = "RANKED_SOLO_5x5"


@dataclass(frozen=True, slots=True)
class StoredRank:
    snapshot_id: int
    snapshot: RankSnapshot


class CollectionStore:
    def __init__(self, repository: Repository, region: str) -> None:
        self.repository = repository
        self.region = region
        self.connection = repository.connection
        self.lock = repository.lock

    def save_run_context(
        self,
        run_id: int,
        region: str,
        rso_platform_id: str,
        sgp_server_id: str,
        patch: str,
        full_version: str | None,
    ) -> None:
        with self.lock, self.connection:
            self.connection.execute(
                "UPDATE collection_run SET region = ?, rso_platform_id = ?, sgp_server_id = ?, target_patch = ?, target_full_version = ? WHERE id = ?",
                (region, rso_platform_id, sgp_server_id, patch, full_version, run_id),
            )

    def snapshot_exists(self, run_id: int, tier: str) -> bool:
        row = self.connection.execute(
            "SELECT 1 FROM leaderboard_snapshot WHERE run_id = ? AND tier = ? AND entry_count > 0 LIMIT 1",
            (run_id, tier),
        ).fetchone()
        return row is not None

    def save_leaderboard(
        self,
        run_id: int,
        tier: str,
        payload: JsonObject,
        artifact: ArtifactRecord,
    ) -> int:
        artifact_id = self.repository.add_artifact(artifact)
        entries = _leaderboard_rows(payload)
        with self.lock, self.connection:
            cursor = self.connection.execute(
                "INSERT INTO leaderboard_snapshot (run_id, tier, fetched_at, entry_count, pagination_complete, schema_hash, raw_artifact_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (run_id, tier, artifact.fetched_at, len(entries), 1, artifact.schema_hash, artifact_id),
            )
            snapshot_id = int(cursor.lastrowid)
            for entry in entries:
                puuid = _string(entry.get("puuid"))
                if puuid is None:
                    continue
                player_id = self.repository.upsert_player(
                    self.region,
                    puuid,
                    _string(entry.get("summonerId")),
                    _string(entry.get("summonerName")),
                )
                self.connection.execute(
                    "INSERT INTO leaderboard_entry (snapshot_id, player_id, returned_puuid, returned_summoner_id, returned_riot_id, tier, lp, leaderboard_position, raw_entry_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        snapshot_id,
                        player_id,
                        puuid,
                        _string(entry.get("summonerId")),
                        _string(entry.get("riotId")),
                        _string(entry.get("tier")) or tier,
                        _integer(entry.get("leaguePoints")),
                        _integer(entry.get("position")),
                        json.dumps(sanitize_json(entry), ensure_ascii=False, sort_keys=True),
                    ),
                )
                self._save_apex_rank(player_id, entry, tier, artifact_id)
                self.repository.enqueue_player(run_id, player_id)
            return snapshot_id

    def player_puuid(self, player_id: int) -> str | None:
        row = self.connection.execute("SELECT puuid FROM player WHERE id = ?", (player_id,)).fetchone()
        return _string(row["puuid"]) if row is not None else None

    def player_queue_row(self, run_id: int, player_id: int) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM player_queue WHERE run_id = ? AND player_id = ?",
            (run_id, player_id),
        ).fetchone()

    def match_row(self, match_id: int) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM match WHERE id = ?", (match_id,)).fetchone()

    def match_queue_row(self, match_id: int) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM match_queue WHERE match_id = ?", (match_id,)).fetchone()

    def rank_for_player(self, player_id: int) -> StoredRank | None:
        row = self.connection.execute(
            "SELECT * FROM player_rank_snapshot WHERE player_id = ? AND queue_type = ? ORDER BY observed_at DESC, id DESC LIMIT 1",
            (player_id, SOLO_QUEUE),
        ).fetchone()
        if row is None:
            return None
        return StoredRank(int(row["id"]), _rank_from_row(row))

    def rank_for_puuid(self, puuid: str) -> StoredRank | None:
        row = self.connection.execute(
            "SELECT prs.* FROM player_rank_snapshot prs JOIN player p ON p.id = prs.player_id WHERE p.puuid = ? AND prs.queue_type = ? ORDER BY prs.observed_at DESC, prs.id DESC LIMIT 1",
            (puuid, SOLO_QUEUE),
        ).fetchone()
        if row is None:
            return None
        return StoredRank(int(row["id"]), _rank_from_row(row))

    def save_rank(self, snapshot: RankSnapshot) -> StoredRank:
        snapshot_id = self.repository.save_rank_snapshot(snapshot)
        return StoredRank(snapshot_id, snapshot)

    def enqueue_participant(self, run_id: int, player_id: int, snapshot: RankSnapshot | None) -> None:
        state = PlayerQueueState.MASTER_PLUS if snapshot is not None and snapshot.is_master_plus else PlayerQueueState.NOT_MASTER_PLUS
        rank_status = RankStatus.VERIFIED if snapshot is not None else RankStatus.UNKNOWN
        with self.lock, self.connection:
            self.connection.execute(
                "INSERT INTO player_queue (run_id, player_id, state, rank_status, updated_at) VALUES (?, ?, ?, ?, ?) ON CONFLICT(run_id, player_id) DO UPDATE SET state = CASE WHEN player_queue.state IN ('CRAWLED', 'CRAWLING') THEN player_queue.state ELSE excluded.state END, rank_status = excluded.rank_status, updated_at = excluded.updated_at",
                (run_id, player_id, state.value, rank_status.value, utc_now().isoformat()),
            )

    def save_participant(self, match_id: int, index: int, puuid: str, player_id: int, rank: StoredRank | None) -> None:
        self.repository.matches.save_participant(match_id, index, player_id, puuid, rank.snapshot_id if rank else None)

    def update_match_metadata(self, match_id: int, summary: MatchSummary) -> None:
        self.repository.matches.update_metadata(match_id, summary)

    def record_error(
        self,
        run_id: int,
        entity_type: str,
        entity_id: str,
        step: str,
        error_class: str,
        retryable: bool,
        status_code: int | None,
        message: str,
    ) -> None:
        with self.lock, self.connection:
            self.connection.execute(
                "INSERT INTO error_event (run_id, entity_type, entity_id, step, timestamp, error_class, retryable, status_code, sanitized_message) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (run_id, entity_type, entity_id, step, utc_now().isoformat(), error_class, int(retryable), status_code, message[:1000]),
            )

    def _save_apex_rank(self, player_id: int, entry: JsonObject, tier: str, artifact_id: int) -> None:
        rank = RankSnapshot(
            player_id=PlayerId(player_id),
            observed_at=utc_now(),
            source="LCU_APEX",
            region=self.region,
            queue_type=SOLO_QUEUE,
            tier=(_string(entry.get("tier")) or tier).upper(),
            division=_string(entry.get("division")),
            lp=_integer(entry.get("leaguePoints")),
            wins=_integer(entry.get("wins")),
            losses=_integer(entry.get("losses")),
            raw_artifact_id=artifact_id,
        )
        self.repository.save_rank_snapshot(rank)


def _leaderboard_rows(payload: JsonObject) -> list[JsonObject]:
    divisions = payload.get("divisions")
    if isinstance(divisions, list):
        result: list[JsonObject] = []
        for division in divisions:
            if not isinstance(division, dict):
                continue
            standings = division.get("standings")
            if isinstance(standings, list):
                result.extend(entry for entry in standings if isinstance(entry, dict))
        return result
    entries = payload.get("entries")
    return [entry for entry in entries if isinstance(entry, dict)] if isinstance(entries, list) else []


def _rank_from_row(row: sqlite3.Row) -> RankSnapshot:
    return RankSnapshot(
        player_id=PlayerId(int(row["player_id"])),
        observed_at=datetime.fromisoformat(str(row["observed_at"])),
        source=str(row["source"]),
        region=str(row["region"]),
        queue_type=str(row["queue_type"]),
        tier=str(row["tier"]),
        division=_string(row["division"]),
        lp=_integer(row["lp"]),
        wins=_integer(row["wins"]),
        losses=_integer(row["losses"]),
        raw_artifact_id=_integer(row["raw_artifact_id"]),
    )


def _string(value: JsonValue | None) -> str | None:
    return value if isinstance(value, str) else None


def _integer(value: JsonValue | None) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None
