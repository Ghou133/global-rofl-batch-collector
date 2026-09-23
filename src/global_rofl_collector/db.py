from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .models import JobState, MatchRecord, ProbeEvidence, Quality


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_info (
    version INTEGER NOT NULL
);
INSERT INTO schema_info(version)
SELECT 1 WHERE NOT EXISTS (SELECT 1 FROM schema_info);

CREATE TABLE IF NOT EXISTS datasets (
    id INTEGER PRIMARY KEY,
    platform TEXT NOT NULL,
    patch_key TEXT NOT NULL,
    exact_realm_version TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(platform, patch_key)
);

CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY,
    dataset_id INTEGER REFERENCES datasets(id),
    command TEXT NOT NULL,
    target_total INTEGER,
    status TEXT NOT NULL,
    capability_status TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    summary_json TEXT
);

CREATE TABLE IF NOT EXISTS players (
    dataset_id INTEGER NOT NULL REFERENCES datasets(id),
    puuid TEXT NOT NULL,
    summoner_id TEXT,
    tier TEXT NOT NULL CHECK(tier IN ('CHALLENGER','GRANDMASTER','MASTER')),
    rank TEXT,
    league_points INTEGER NOT NULL,
    wins INTEGER NOT NULL,
    losses INTEGER NOT NULL,
    retrieved_at TEXT NOT NULL,
    source_run_id INTEGER REFERENCES runs(id),
    PRIMARY KEY(dataset_id, puuid)
);

CREATE TABLE IF NOT EXISTS match_discoveries (
    dataset_id INTEGER NOT NULL REFERENCES datasets(id),
    match_id TEXT NOT NULL,
    puuid TEXT NOT NULL,
    first_run_id INTEGER REFERENCES runs(id),
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    observations INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY(dataset_id, match_id, puuid)
);

CREATE TABLE IF NOT EXISTS matches (
    match_id TEXT PRIMARY KEY,
    dataset_id INTEGER NOT NULL REFERENCES datasets(id),
    platform TEXT NOT NULL,
    game_id TEXT NOT NULL,
    queue_id INTEGER NOT NULL,
    game_version_exact TEXT NOT NULL,
    patch_key TEXT NOT NULL,
    game_creation INTEGER NOT NULL,
    game_duration INTEGER NOT NULL,
    metadata_json TEXT NOT NULL,
    challenger_count INTEGER NOT NULL DEFAULT 0,
    grandmaster_count INTEGER NOT NULL DEFAULT 0,
    master_count INTEGER NOT NULL DEFAULT 0,
    known_apex_count INTEGER NOT NULL DEFAULT 0,
    highest_tier TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    UNIQUE(platform, game_id)
);

CREATE TABLE IF NOT EXISTS match_participants (
    match_id TEXT NOT NULL REFERENCES matches(match_id),
    puuid TEXT NOT NULL,
    PRIMARY KEY(match_id, puuid)
);

CREATE TABLE IF NOT EXISTS replay_jobs (
    match_id TEXT PRIMARY KEY REFERENCES matches(match_id),
    state TEXT NOT NULL,
    resume_state TEXT,
    provider TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT,
    lease_owner TEXT,
    lease_expires_at TEXT,
    last_error_id INTEGER,
    eligibility_reason TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS downloads (
    match_id TEXT PRIMARY KEY REFERENCES matches(match_id),
    file_path TEXT NOT NULL UNIQUE,
    file_size INTEGER NOT NULL CHECK(file_size > 0),
    sha256 TEXT NOT NULL CHECK(length(sha256) = 64),
    provider TEXT NOT NULL,
    downloaded_at TEXT NOT NULL,
    verified_at TEXT NOT NULL,
    verification_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS acquisition_probes (
    id INTEGER PRIMARY KEY,
    run_id INTEGER REFERENCES runs(id),
    route TEXT NOT NULL,
    capability TEXT NOT NULL,
    sample_match_id TEXT,
    mechanism TEXT NOT NULL,
    http_status INTEGER,
    auth_result TEXT,
    region_evidence TEXT,
    client_log_excerpt TEXT,
    patch_key TEXT,
    attempted_at TEXT NOT NULL,
    evidence_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS errors (
    id INTEGER PRIMARY KEY,
    run_id INTEGER REFERENCES runs(id),
    match_id TEXT,
    stage TEXT NOT NULL,
    code TEXT NOT NULL,
    message TEXT NOT NULL,
    retryable INTEGER NOT NULL,
    http_status INTEGER,
    occurred_at TEXT NOT NULL,
    details_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_players_dataset_tier
    ON players(dataset_id, tier, league_points DESC);
CREATE INDEX IF NOT EXISTS idx_discoveries_match
    ON match_discoveries(dataset_id, match_id);
CREATE INDEX IF NOT EXISTS idx_matches_dataset_patch
    ON matches(dataset_id, queue_id, patch_key);
CREATE INDEX IF NOT EXISTS idx_matches_quality
    ON matches(dataset_id, challenger_count DESC, grandmaster_count DESC,
               known_apex_count DESC, master_count DESC, game_creation DESC);
CREATE INDEX IF NOT EXISTS idx_jobs_state
    ON replay_jobs(state, next_attempt_at);
CREATE INDEX IF NOT EXISTS idx_errors_run
    ON errors(run_id, occurred_at DESC);
"""


class Database:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path, timeout=30, isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute("PRAGMA busy_timeout=30000")
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=FULL")

    def close(self) -> None:
        self.connection.close()

    def migrate(self) -> None:
        self.connection.executescript(SCHEMA)

    @contextmanager
    def transaction(self, *, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        self.connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
        try:
            yield self.connection
        except BaseException:
            self.connection.rollback()
            raise
        else:
            self.connection.commit()

    def dataset(self, platform: str, patch: str, exact_realm_version: str) -> int:
        with self.transaction(immediate=True) as con:
            con.execute(
                """
                INSERT INTO datasets(platform, patch_key, exact_realm_version, created_at)
                VALUES(?,?,?,?)
                ON CONFLICT(platform, patch_key) DO UPDATE SET
                    exact_realm_version=excluded.exact_realm_version
                """,
                (platform, patch, exact_realm_version, utc_now()),
            )
            row = con.execute(
                "SELECT id FROM datasets WHERE platform=? AND patch_key=?",
                (platform, patch),
            ).fetchone()
        assert row is not None
        return int(row["id"])

    def latest_dataset(self, platform: str = "KR") -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM datasets WHERE platform=? ORDER BY id DESC LIMIT 1", (platform,)
        ).fetchone()

    def start_run(
        self, dataset_id: int | None, command: str, target_total: int | None = None
    ) -> int:
        cursor = self.connection.execute(
            """
            INSERT INTO runs(dataset_id,command,target_total,status,started_at)
            VALUES(?,?,?,?,?)
            """,
            (dataset_id, command, target_total, "RUNNING", utc_now()),
        )
        return int(cursor.lastrowid)

    def finish_run(
        self,
        run_id: int,
        status: str,
        *,
        capability: str | None = None,
        summary: dict[str, Any] | None = None,
    ) -> None:
        self.connection.execute(
            """
            UPDATE runs SET status=?, capability_status=?, finished_at=?, summary_json=?
            WHERE id=?
            """,
            (
                status,
                capability,
                utc_now(),
                json.dumps(summary or {}, sort_keys=True, separators=(",", ":")),
                run_id,
            ),
        )

    def interrupt_stale_runs(self, dataset_id: int) -> int:
        cursor = self.connection.execute(
            """
            UPDATE runs SET status='INTERRUPTED',finished_at=?,summary_json=?
            WHERE dataset_id=? AND status='RUNNING'
            """,
            (
                utc_now(),
                json.dumps(
                    {"reason": "PROCESS_INTERRUPTED_BEFORE_FINALIZATION"},
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                dataset_id,
            ),
        )
        return int(cursor.rowcount)

    def upsert_players(
        self, dataset_id: int, run_id: int, tier: str, entries: Sequence[dict[str, Any]]
    ) -> int:
        now = utc_now()
        rows = []
        for entry in entries:
            puuid = entry.get("puuid")
            if not puuid:
                continue
            rows.append(
                (
                    dataset_id,
                    puuid,
                    entry.get("summonerId"),
                    tier,
                    entry.get("rank"),
                    int(entry.get("leaguePoints", 0)),
                    int(entry.get("wins", 0)),
                    int(entry.get("losses", 0)),
                    now,
                    run_id,
                )
            )
        with self.transaction(immediate=True) as con:
            # League-V4 apex endpoints are full snapshots. Replace this tier's current
            # membership so promotions/demotions do not accumulate as phantom players.
            con.execute(
                "DELETE FROM players WHERE dataset_id=? AND tier=?", (dataset_id, tier)
            )
            con.executemany(
                """
                INSERT INTO players(
                    dataset_id,puuid,summoner_id,tier,rank,league_points,wins,losses,
                    retrieved_at,source_run_id
                ) VALUES(?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(dataset_id,puuid) DO UPDATE SET
                    summoner_id=excluded.summoner_id,
                    tier=excluded.tier,
                    rank=excluded.rank,
                    league_points=excluded.league_points,
                    wins=excluded.wins,
                    losses=excluded.losses,
                    retrieved_at=excluded.retrieved_at,
                    source_run_id=excluded.source_run_id
                """,
                rows,
            )
        return len(rows)

    def player_rows(self, dataset_id: int, tier: str | None = None) -> list[sqlite3.Row]:
        if tier:
            return list(
                self.connection.execute(
                    """
                    SELECT * FROM players WHERE dataset_id=? AND tier=?
                    ORDER BY league_points DESC, wins DESC, puuid
                    """,
                    (dataset_id, tier),
                )
            )
        return list(
            self.connection.execute(
                "SELECT * FROM players WHERE dataset_id=?", (dataset_id,)
            )
        )

    def player_tiers(self, dataset_id: int) -> dict[str, str]:
        return {
            str(row["puuid"]): str(row["tier"])
            for row in self.connection.execute(
                "SELECT puuid,tier FROM players WHERE dataset_id=?", (dataset_id,)
            )
        }

    def add_discoveries(
        self, dataset_id: int, run_id: int, puuid: str, match_ids: Iterable[str]
    ) -> int:
        now = utc_now()
        ids = list(dict.fromkeys(match_ids))
        with self.transaction(immediate=True) as con:
            con.executemany(
                """
                INSERT INTO match_discoveries(
                    dataset_id,match_id,puuid,first_run_id,first_seen_at,last_seen_at,observations
                ) VALUES(?,?,?,?,?,?,1)
                ON CONFLICT(dataset_id,match_id,puuid) DO UPDATE SET
                    last_seen_at=excluded.last_seen_at,
                    observations=match_discoveries.observations+1
                """,
                ((dataset_id, match_id, puuid, run_id, now, now) for match_id in ids),
            )
        return len(ids)

    def discovered_match_ids(self, dataset_id: int) -> list[str]:
        return [
            str(row[0])
            for row in self.connection.execute(
                "SELECT DISTINCT match_id FROM match_discoveries WHERE dataset_id=?",
                (dataset_id,),
            )
        ]

    def match_exists(self, match_id: str) -> bool:
        return (
            self.connection.execute(
                "SELECT 1 FROM matches WHERE match_id=?", (match_id,)
            ).fetchone()
            is not None
        )

    def store_match(self, dataset_id: int, record: MatchRecord, eligible: bool) -> None:
        now = utc_now()
        q = record.quality
        raw = json.dumps(record.raw, sort_keys=True, separators=(",", ":"))
        with self.transaction(immediate=True) as con:
            con.execute(
                """
                INSERT INTO matches(
                    match_id,dataset_id,platform,game_id,queue_id,game_version_exact,
                    patch_key,game_creation,game_duration,metadata_json,
                    challenger_count,grandmaster_count,master_count,known_apex_count,
                    highest_tier,first_seen_at,last_seen_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(match_id) DO UPDATE SET
                    metadata_json=excluded.metadata_json,
                    challenger_count=excluded.challenger_count,
                    grandmaster_count=excluded.grandmaster_count,
                    master_count=excluded.master_count,
                    known_apex_count=excluded.known_apex_count,
                    highest_tier=excluded.highest_tier,
                    last_seen_at=excluded.last_seen_at
                """,
                (
                    record.match_id,
                    dataset_id,
                    record.platform,
                    record.game_id,
                    record.queue_id,
                    record.game_version,
                    record.patch,
                    record.game_creation,
                    record.game_duration,
                    raw,
                    q.challenger_count,
                    q.grandmaster_count,
                    q.master_count,
                    q.known_apex_count,
                    q.highest_tier,
                    now,
                    now,
                ),
            )
            con.executemany(
                "INSERT OR IGNORE INTO match_participants(match_id,puuid) VALUES(?,?)",
                ((record.match_id, puuid) for puuid in set(record.participants)),
            )
            state = JobState.ELIGIBLE if eligible else JobState.INELIGIBLE
            reason = "CURRENT_PATCH_RANKED_SOLO_COMPLETED" if eligible else "FILTERED"
            con.execute(
                """
                INSERT INTO replay_jobs(
                    match_id,state,eligibility_reason,created_at,updated_at
                ) VALUES(?,?,?,?,?)
                ON CONFLICT(match_id) DO UPDATE SET
                    state=CASE
                        WHEN replay_jobs.state IN ('DISCOVERED','ELIGIBLE','INELIGIBLE')
                        THEN excluded.state ELSE replay_jobs.state END,
                    eligibility_reason=CASE
                        WHEN replay_jobs.state IN ('DISCOVERED','ELIGIBLE','INELIGIBLE')
                        THEN excluded.eligibility_reason ELSE replay_jobs.eligibility_reason END,
                    updated_at=excluded.updated_at
                """,
                (record.match_id, state.value, reason, now, now),
            )

    def update_quality(self, match_id: str, quality: Quality) -> None:
        self.connection.execute(
            """
            UPDATE matches SET challenger_count=?, grandmaster_count=?, master_count=?,
                known_apex_count=?, highest_tier=?, last_seen_at=?
            WHERE match_id=?
            """,
            (
                quality.challenger_count,
                quality.grandmaster_count,
                quality.master_count,
                quality.known_apex_count,
                quality.highest_tier,
                utc_now(),
                match_id,
            ),
        )

    def recompute_all_quality(self, dataset_id: int) -> None:
        tiers = self.player_tiers(dataset_id)
        matches = self.connection.execute(
            "SELECT match_id FROM matches WHERE dataset_id=?", (dataset_id,)
        ).fetchall()
        with self.transaction(immediate=True) as con:
            for row in matches:
                match_id = str(row["match_id"])
                puuids = [
                    str(item["puuid"])
                    for item in con.execute(
                        "SELECT puuid FROM match_participants WHERE match_id=?", (match_id,)
                    )
                ]
                quality = Quality.from_participants(puuids, tiers)
                con.execute(
                    """
                    UPDATE matches SET challenger_count=?,grandmaster_count=?,master_count=?,
                        known_apex_count=?,highest_tier=?,last_seen_at=? WHERE match_id=?
                    """,
                    (
                        quality.challenger_count,
                        quality.grandmaster_count,
                        quality.master_count,
                        quality.known_apex_count,
                        quality.highest_tier,
                        utc_now(),
                        match_id,
                    ),
                )

    def candidates(self, dataset_id: int, limit: int | None = None) -> list[sqlite3.Row]:
        sql = """
            SELECT m.*,j.state,j.attempts,j.provider
            FROM matches m JOIN replay_jobs j ON j.match_id=m.match_id
            WHERE m.dataset_id=? AND j.state IN ('ELIGIBLE','QUEUED','FAILED_RETRYABLE')
            ORDER BY m.challenger_count DESC,
                     CASE WHEN m.challenger_count > 0 THEN 1 ELSE 0 END DESC,
                     m.grandmaster_count DESC,
                     m.known_apex_count DESC,
                     m.master_count DESC,
                     m.game_creation DESC,
                     m.match_id ASC
        """
        params: tuple[Any, ...] = (dataset_id,)
        if limit is not None:
            sql += " LIMIT ?"
            params += (limit,)
        return list(self.connection.execute(sql, params))

    def match_row(self, match_id: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM matches WHERE match_id=?", (match_id,)
        ).fetchone()

    def transition_job(
        self,
        match_id: str,
        expected: JobState | Sequence[JobState],
        new_state: JobState,
        *,
        provider: str | None = None,
        resume_state: JobState | None = None,
        increment_attempts: bool = False,
        last_error_id: int | None = None,
    ) -> None:
        expected_values = (
            [expected.value]
            if isinstance(expected, JobState)
            else [state.value for state in expected]
        )
        placeholders = ",".join("?" for _ in expected_values)
        cursor = self.connection.execute(
            f"""
            UPDATE replay_jobs SET state=?,provider=COALESCE(?,provider),resume_state=?,
                attempts=attempts+?,last_error_id=COALESCE(?,last_error_id),updated_at=?
            WHERE match_id=? AND state IN ({placeholders})
            """,
            (
                new_state.value,
                provider,
                resume_state.value if resume_state else None,
                1 if increment_attempts else 0,
                last_error_id,
                utc_now(),
                match_id,
                *expected_values,
            ),
        )
        if cursor.rowcount != 1:
            actual = self.connection.execute(
                "SELECT state FROM replay_jobs WHERE match_id=?", (match_id,)
            ).fetchone()
            actual_value = actual["state"] if actual else "MISSING"
            raise ValueError(
                f"Illegal or concurrent replay job transition for {match_id}: "
                f"{actual_value} -> {new_state.value}"
            )

    def record_download(
        self,
        match_id: str,
        *,
        file_path: str,
        file_size: int,
        sha256: str,
        provider: str,
        verification: dict[str, Any],
    ) -> None:
        now = utc_now()
        with self.transaction(immediate=True) as con:
            con.execute(
                """
                INSERT INTO downloads(
                    match_id,file_path,file_size,sha256,provider,downloaded_at,verified_at,
                    verification_json
                ) VALUES(?,?,?,?,?,?,?,?)
                ON CONFLICT(match_id) DO UPDATE SET
                    file_path=excluded.file_path,file_size=excluded.file_size,
                    sha256=excluded.sha256,provider=excluded.provider,
                    downloaded_at=excluded.downloaded_at,verified_at=excluded.verified_at,
                    verification_json=excluded.verification_json
                """,
                (
                    match_id,
                    file_path,
                    file_size,
                    sha256,
                    provider,
                    now,
                    now,
                    json.dumps(verification, sort_keys=True, separators=(",", ":")),
                ),
            )
            con.execute(
                """
                UPDATE replay_jobs SET state='VERIFIED',resume_state=NULL,updated_at=?
                WHERE match_id=?
                """,
                (now, match_id),
            )

    def verified_count(self, dataset_id: int) -> int:
        return int(
            self.connection.execute(
                """
                SELECT count(*) FROM replay_jobs j JOIN matches m ON m.match_id=j.match_id
                WHERE m.dataset_id=? AND j.state='VERIFIED'
                """,
                (dataset_id,),
            ).fetchone()[0]
        )

    def recoverable_backlog_count(self, dataset_id: int) -> int:
        return int(
            self.connection.execute(
                """
                SELECT count(*) FROM replay_jobs j JOIN matches m ON m.match_id=j.match_id
                WHERE m.dataset_id=? AND j.state IN (
                    'ELIGIBLE','QUEUED','DOWNLOADING','DOWNLOADED','FAILED_RETRYABLE'
                )
                """,
                (dataset_id,),
            ).fetchone()[0]
        )

    def download_row(self, match_id: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM downloads WHERE match_id=?", (match_id,)
        ).fetchone()

    def record_probe(
        self,
        run_id: int | None,
        sample_match_id: str | None,
        patch: str | None,
        probe: ProbeEvidence,
    ) -> int:
        cursor = self.connection.execute(
            """
            INSERT INTO acquisition_probes(
                run_id,route,capability,sample_match_id,mechanism,http_status,auth_result,
                region_evidence,client_log_excerpt,patch_key,attempted_at,evidence_json
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                run_id,
                probe.route,
                probe.capability.value,
                sample_match_id,
                probe.mechanism,
                probe.http_status,
                probe.auth_result,
                probe.region_evidence,
                probe.client_log_excerpt,
                patch,
                utc_now(),
                json.dumps(probe.evidence or {}, sort_keys=True, separators=(",", ":")),
            ),
        )
        return int(cursor.lastrowid)

    def record_error(
        self,
        run_id: int | None,
        match_id: str | None,
        stage: str,
        code: str,
        message: str,
        *,
        retryable: bool,
        http_status: int | None = None,
        details: dict[str, Any] | None = None,
    ) -> int:
        cursor = self.connection.execute(
            """
            INSERT INTO errors(
                run_id,match_id,stage,code,message,retryable,http_status,occurred_at,details_json
            ) VALUES(?,?,?,?,?,?,?,?,?)
            """,
            (
                run_id,
                match_id,
                stage,
                code,
                message,
                int(retryable),
                http_status,
                utc_now(),
                json.dumps(details or {}, sort_keys=True, separators=(",", ":")),
            ),
        )
        return int(cursor.lastrowid)

    def manifest_rows(self, dataset_id: int) -> list[sqlite3.Row]:
        return list(
            self.connection.execute(
                """
                SELECT m.*,d.file_path,d.file_size,d.sha256,d.downloaded_at,d.verified_at,
                       d.verification_json,d.provider
                FROM matches m JOIN replay_jobs j ON j.match_id=m.match_id
                JOIN downloads d ON d.match_id=m.match_id
                WHERE m.dataset_id=? AND j.state='VERIFIED'
                ORDER BY m.match_id ASC
                """,
                (dataset_id,),
            )
        )

    def stats(self, dataset_id: int) -> dict[str, Any]:
        con = self.connection

        def scalar(sql: str, params: tuple[Any, ...] = ()) -> int:
            return int(con.execute(sql, params).fetchone()[0] or 0)

        stats: dict[str, Any] = {}
        stats["players_discovered"] = scalar(
            "SELECT count(*) FROM players WHERE dataset_id=?", (dataset_id,)
        )
        for tier, key in (
            ("CHALLENGER", "challenger_players"),
            ("GRANDMASTER", "grandmaster_players"),
            ("MASTER", "master_players"),
        ):
            stats[key] = scalar(
                "SELECT count(*) FROM players WHERE dataset_id=? AND tier=?", (dataset_id, tier)
            )
        stats["raw_match_discoveries"] = scalar(
            "SELECT sum(observations) FROM match_discoveries WHERE dataset_id=?", (dataset_id,)
        )
        stats["discovery_edges"] = scalar(
            "SELECT count(*) FROM match_discoveries WHERE dataset_id=?", (dataset_id,)
        )
        stats["unique_matches"] = scalar(
            "SELECT count(DISTINCT match_id) FROM match_discoveries WHERE dataset_id=?",
            (dataset_id,),
        )
        stats["current_patch_matches"] = scalar(
            """
            SELECT count(*) FROM matches m JOIN datasets d ON d.id=m.dataset_id
            WHERE m.dataset_id=? AND m.patch_key=d.patch_key
            """,
            (dataset_id,),
        )
        for state in JobState:
            stats[state.value.lower()] = scalar(
                """
                SELECT count(*) FROM replay_jobs j JOIN matches m ON m.match_id=j.match_id
                WHERE m.dataset_id=? AND j.state=?
                """,
                (dataset_id, state.value),
            )
        stats["total_dataset_size"] = scalar(
            """
            SELECT sum(d.file_size) FROM downloads d JOIN matches m ON m.match_id=d.match_id
            WHERE m.dataset_id=?
            """,
            (dataset_id,),
        )
        stats["challenger_heavy"] = scalar(
            """
            SELECT count(*) FROM matches m JOIN replay_jobs j ON j.match_id=m.match_id
            WHERE m.dataset_id=? AND j.state='VERIFIED' AND m.challenger_count>=5
            """,
            (dataset_id,),
        )
        stats["challenger_present"] = scalar(
            """
            SELECT count(*) FROM matches m JOIN replay_jobs j ON j.match_id=m.match_id
            WHERE m.dataset_id=? AND j.state='VERIFIED' AND m.challenger_count>0
            """,
            (dataset_id,),
        )
        stats["gm_heavy"] = scalar(
            """
            SELECT count(*) FROM matches m JOIN replay_jobs j ON j.match_id=m.match_id
            WHERE m.dataset_id=? AND j.state='VERIFIED' AND m.challenger_count=0
            AND m.grandmaster_count>=5
            """,
            (dataset_id,),
        )
        stats["master_heavy"] = scalar(
            """
            SELECT count(*) FROM matches m JOIN replay_jobs j ON j.match_id=m.match_id
            WHERE m.dataset_id=? AND j.state='VERIFIED' AND m.challenger_count=0
            AND m.grandmaster_count<5 AND m.master_count>0
            """,
            (dataset_id,),
        )
        latest = con.execute(
            "SELECT * FROM runs WHERE dataset_id=? ORDER BY id DESC LIMIT 1", (dataset_id,)
        ).fetchone()
        stats["last_run"] = dict(latest) if latest else None
        stats["latest_errors"] = [
            dict(row)
            for row in con.execute(
                "SELECT * FROM errors WHERE run_id IN (SELECT id FROM runs WHERE dataset_id=?) "
                "ORDER BY id DESC LIMIT 10",
                (dataset_id,),
            )
        ]
        stats["latest_probes"] = [
            dict(row)
            for row in con.execute(
                "SELECT * FROM acquisition_probes WHERE run_id IN "
                "(SELECT id FROM runs WHERE dataset_id=?) ORDER BY id DESC LIMIT 10",
                (dataset_id,),
            )
        ]
        return stats
