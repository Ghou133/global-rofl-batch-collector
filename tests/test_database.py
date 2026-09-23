from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest

from global_rofl_collector.db import Database
from global_rofl_collector.models import JobState, MatchRecord, Quality


def _start_dataset(db: Database, patch: str = "26.18") -> tuple[int, int]:
    dataset_id = db.dataset("KR", patch, f"{patch}.704.1234")
    run_id = db.start_run(dataset_id, "test")
    return dataset_id, run_id


def test_discovery_edges_preserve_raw_observations_but_deduplicate_matches(db: Database) -> None:
    dataset_id, run_id = _start_dataset(db)

    assert db.add_discoveries(dataset_id, run_id, "p1", ["KR_1", "KR_1", "KR_2"]) == 2
    assert db.add_discoveries(dataset_id, run_id, "p2", ["KR_1", "KR_3"]) == 2
    assert db.add_discoveries(dataset_id, run_id, "p1", ["KR_1"]) == 1

    assert set(db.discovered_match_ids(dataset_id)) == {"KR_1", "KR_2", "KR_3"}
    stats = db.stats(dataset_id)
    assert stats["raw_match_discoveries"] == 5
    assert stats["discovery_edges"] == 4
    assert stats["unique_matches"] == 3


def test_player_upsert_uses_puuid_identity_and_refreshes_tier(db: Database) -> None:
    dataset_id, run_id = _start_dataset(db)
    first = {
        "puuid": "stable-puuid",
        "summonerId": "old-id",
        "rank": "I",
        "leaguePoints": 500,
        "wins": 10,
        "losses": 5,
    }
    refreshed = {
        **first,
        "summonerId": "new-id",
        "leaguePoints": 900,
        "wins": 20,
    }

    db.upsert_players(dataset_id, run_id, "MASTER", [first])
    db.upsert_players(dataset_id, run_id, "CHALLENGER", [refreshed])

    rows = db.player_rows(dataset_id)
    assert len(rows) == 1
    assert rows[0]["puuid"] == "stable-puuid"
    assert rows[0]["summoner_id"] == "new-id"
    assert rows[0]["tier"] == "CHALLENGER"
    assert rows[0]["league_points"] == 900


def test_candidate_order_is_objective_and_deterministic(
    db: Database,
    match_factory: Callable[..., MatchRecord],
) -> None:
    dataset_id, _ = _start_dataset(db)
    qualities = {
        "KR_10": Quality(0, 0, 10, 10, "MASTER"),
        "KR_20": Quality(0, 8, 1, 9, "GRANDMASTER"),
        "KR_30": Quality(1, 0, 9, 10, "CHALLENGER"),
        "KR_40": Quality(1, 2, 0, 3, "CHALLENGER"),
        "KR_50": Quality(2, 0, 0, 2, "CHALLENGER"),
    }
    for match_id, quality in qualities.items():
        db.store_match(dataset_id, match_factory(match_id, quality=quality), eligible=True)

    ordered = [row["match_id"] for row in db.candidates(dataset_id)]

    assert ordered == ["KR_50", "KR_40", "KR_30", "KR_20", "KR_10"]


def test_match_refresh_never_regresses_an_active_or_terminal_job_state(
    db: Database,
    match_factory: Callable[..., MatchRecord],
) -> None:
    dataset_id, _ = _start_dataset(db)
    record = match_factory()
    db.store_match(dataset_id, record, eligible=True)
    db.transition_job(record.match_id, JobState.ELIGIBLE, JobState.QUEUED)

    db.store_match(dataset_id, record, eligible=False)

    row = db.connection.execute(
        "SELECT state,eligibility_reason FROM replay_jobs WHERE match_id=?", (record.match_id,)
    ).fetchone()
    assert row["state"] == JobState.QUEUED
    assert row["eligibility_reason"] == "CURRENT_PATCH_RANKED_SOLO_COMPLETED"


def test_compare_and_set_transition_rejects_stale_or_illegal_expected_state(
    db: Database,
    match_factory: Callable[..., MatchRecord],
) -> None:
    dataset_id, _ = _start_dataset(db)
    record = match_factory()
    db.store_match(dataset_id, record, eligible=True)

    db.transition_job(record.match_id, JobState.ELIGIBLE, JobState.QUEUED)
    with pytest.raises(ValueError, match="QUEUED -> VERIFIED"):
        db.transition_job(record.match_id, JobState.ELIGIBLE, JobState.VERIFIED)

    actual = db.connection.execute(
        "SELECT state FROM replay_jobs WHERE match_id=?", (record.match_id,)
    ).fetchone()[0]
    assert actual == JobState.QUEUED


def test_patch_datasets_are_isolated_and_old_patch_is_preserved(db: Database) -> None:
    old_id = db.dataset("KR", "26.18", "26.18.704.1")
    new_id = db.dataset("KR", "26.19", "26.19.705.1")

    assert old_id != new_id
    assert db.connection.execute(
        "SELECT exact_realm_version FROM datasets WHERE id=?", (old_id,)
    ).fetchone()[0] == "26.18.704.1"
    assert db.latest_dataset("KR")["id"] == new_id


def test_migration_is_idempotent_and_queue_state_survives_database_restart(
    tmp_path: Path,
    match_factory: Callable[..., MatchRecord],
) -> None:
    path = tmp_path / "persistent.sqlite3"
    first = Database(path)
    first.migrate()
    dataset_id, _ = _start_dataset(first)
    record = match_factory()
    first.store_match(dataset_id, record, eligible=True)
    first.transition_job(record.match_id, JobState.ELIGIBLE, JobState.QUEUED)
    first.close()

    reopened = Database(path)
    try:
        reopened.migrate()
        reopened.migrate()
        dataset = reopened.latest_dataset("KR")
        job = reopened.connection.execute(
            "SELECT state FROM replay_jobs WHERE match_id=?", (record.match_id,)
        ).fetchone()

        assert dataset["id"] == dataset_id
        assert job["state"] == JobState.QUEUED
        assert reopened.connection.execute("SELECT count(*) FROM matches").fetchone()[0] == 1
        assert reopened.connection.execute("SELECT count(*) FROM schema_info").fetchone()[0] == 1
    finally:
        reopened.close()


def test_interrupt_stale_runs_is_scoped_idempotent_and_preserves_finished_runs(
    db: Database,
) -> None:
    dataset_id, stale_run = _start_dataset(db)
    finished_run = db.start_run(dataset_id, "probe")
    db.finish_run(finished_run, "PASS", summary={"completed": True})
    other_dataset = db.dataset("KR", "26.19", "26.19.705.1")
    other_running = db.start_run(other_dataset, "run", 100)

    assert db.interrupt_stale_runs(dataset_id) == 1
    assert db.interrupt_stale_runs(dataset_id) == 0

    rows = {
        row["id"]: row
        for row in db.connection.execute(
            "SELECT id,status,finished_at,summary_json FROM runs ORDER BY id"
        )
    }
    assert rows[stale_run]["status"] == "INTERRUPTED"
    assert rows[stale_run]["finished_at"] is not None
    assert json.loads(rows[stale_run]["summary_json"]) == {
        "reason": "PROCESS_INTERRUPTED_BEFORE_FINALIZATION"
    }
    assert rows[finished_run]["status"] == "PASS"
    assert rows[other_running]["status"] == "RUNNING"


def test_recoverable_backlog_includes_downloading_but_excludes_terminal_jobs(
    db: Database,
    match_factory: Callable[..., MatchRecord],
) -> None:
    dataset_id, _ = _start_dataset(db)
    for match_id in ("KR_11", "KR_12", "KR_13"):
        db.store_match(dataset_id, match_factory(match_id), eligible=True)
    db.transition_job("KR_11", JobState.ELIGIBLE, JobState.QUEUED)
    db.transition_job("KR_11", JobState.QUEUED, JobState.DOWNLOADING)
    db.transition_job("KR_13", JobState.ELIGIBLE, JobState.UNAVAILABLE)

    assert [row["match_id"] for row in db.candidates(dataset_id)] == ["KR_12"]
    assert db.recoverable_backlog_count(dataset_id) == 2
