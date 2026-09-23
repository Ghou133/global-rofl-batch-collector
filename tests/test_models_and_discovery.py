from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from global_rofl_collector.db import Database
from global_rofl_collector.discovery import DiscoveryService
from global_rofl_collector.errors import RiotApiError
from global_rofl_collector.models import MatchRecord, Quality, patch_key, safe_build_component
from global_rofl_collector.riot import parse_match


@pytest.mark.parametrize(
    ("version", "expected"),
    [
        ("26.18.704.1234", "26.18"),
        ("26.18", "26.18"),
        (" 026.018.1 ", "26.18"),
        ("26.18beta", None),
        ("26", None),
        ("not-a-version", None),
        ("", None),
        (None, None),
    ],
)
def test_patch_key_is_strict_and_normalized(version: str | None, expected: str | None) -> None:
    assert patch_key(version) == expected


def test_safe_build_component_preserves_build_identity_without_path_separators() -> None:
    assert safe_build_component("26.18.704+KR / build:7") == "26.18.704_KR_build_7"
    assert safe_build_component("../") == "UNKNOWN"


def test_quality_counts_unique_known_apex_players_and_uses_highest_tier() -> None:
    tiers = {
        "challenger": "CHALLENGER",
        "grandmaster": "GRANDMASTER",
        "master": "MASTER",
        "other": "DIAMOND",
    }

    quality = Quality.from_participants(
        ["master", "challenger", "challenger", "grandmaster", "other", "unknown"],
        tiers,
    )

    assert quality == Quality(
        challenger_count=1,
        grandmaster_count=1,
        master_count=1,
        known_apex_count=3,
        highest_tier="CHALLENGER",
    )


def test_parse_match_preserves_exact_version_and_derives_platform_from_match_id() -> None:
    raw = {
        "metadata": {"matchId": "KR_987654"},
        "info": {
            "gameId": 987654,
            "queueId": 420,
            "gameVersion": "26.18.704.9999",
            "gameCreation": 123,
            "gameDuration": 1_500,
            "participants": [{"puuid": "p1"}, {"puuid": "p2"}],
        },
    }

    record = parse_match(raw, {"p1": "GRANDMASTER"})

    assert record.match_id == "KR_987654"
    assert record.game_id == "987654"
    assert record.platform == "KR"
    assert record.patch == "26.18"
    assert record.game_version == "26.18.704.9999"
    assert record.quality == Quality(0, 1, 0, 1, "GRANDMASTER")


@pytest.mark.parametrize(
    "mutation",
    [
        lambda raw: raw["metadata"].pop("matchId"),
        lambda raw: raw["info"].pop("gameVersion"),
        lambda raw: raw["info"].__setitem__("gameVersion", "bad"),
        lambda raw: raw["info"].pop("gameId"),
    ],
)
def test_parse_match_rejects_missing_stable_identity(
    mutation: Callable[[dict], object],
) -> None:
    raw = {
        "metadata": {"matchId": "KR_1"},
        "info": {"gameId": 1, "gameVersion": "26.18.1", "participants": []},
    }
    mutation(raw)

    with pytest.raises(RiotApiError, match="API_SCHEMA_CHANGED"):
        parse_match(raw, {})


def test_current_patch_eligibility_requires_every_hard_filter(
    match_factory: Callable[..., MatchRecord],
) -> None:
    baseline = match_factory()
    assert DiscoveryService.eligible(baseline, "26.18")

    assert not DiscoveryService.eligible(match_factory(platform="NA1"), "26.18")
    assert not DiscoveryService.eligible(match_factory(queue_id=440), "26.18")
    assert not DiscoveryService.eligible(match_factory(patch="26.17"), "26.18")
    assert not DiscoveryService.eligible(match_factory(game_duration=0), "26.18")
    assert not DiscoveryService.eligible(match_factory(ended=False), "26.18")


def test_discovery_expansion_does_not_skip_unqueried_players_recent_matches(
    db: Database,
    match_factory: Callable[..., MatchRecord],
) -> None:
    dataset_id = db.dataset("KR", "26.18", "26.18.704.1234")
    run_id = db.start_run(dataset_id, "test")
    calls: list[tuple[str, int]] = []
    matches = {
        "KR_1": match_factory("KR_1", patch="26.17", game_version="26.17.1").raw,
        "KR_2": match_factory("KR_2", patch="26.17", game_version="26.17.1").raw,
        "KR_3": match_factory("KR_3").raw,
    }

    class FakeApi:
        def ladder(self, tier: str) -> list[dict[str, Any]]:
            if tier != "CHALLENGER":
                return []
            return [
                {
                    "puuid": puuid,
                    "summonerId": f"sum-{puuid}",
                    "rank": "I",
                    "leaguePoints": points,
                    "wins": 1,
                    "losses": 1,
                }
                for puuid, points in (("p1", 1_000), ("p2", 900))
            ]

        def match_ids(self, puuid: str, *, count: int, start: int = 0) -> list[str]:
            assert count == 20
            calls.append((puuid, start))
            if (puuid, start) == ("p1", 0):
                return ["KR_1", "KR_2"]
            if (puuid, start) == ("p2", 0):
                return ["KR_3"]
            return []

        def match(self, match_id: str) -> dict[str, Any]:
            return matches[match_id]

    result = DiscoveryService(db, FakeApi(), history_count=20).discover(  # type: ignore[arg-type]
        dataset_id,
        run_id,
        "26.18",
        wanted_candidates=2,
    )

    assert calls == [("p1", 0), ("p1", 20), ("p2", 0)]
    assert result.histories_queried == 3
    assert result.details_queried == 3
    assert result.unique_discovered == 3
    assert result.eligible_candidates == 1


def test_discovery_uses_cached_backlog_without_requerying_history(
    db: Database,
    match_factory: Callable[..., MatchRecord],
) -> None:
    dataset_id = db.dataset("KR", "26.18", "26.18.704.1234")
    run_id = db.start_run(dataset_id, "seed")
    db.add_discoveries(dataset_id, run_id, "p1", ["KR_1"])
    db.store_match(dataset_id, match_factory("KR_1"), eligible=True)

    class CachedApi:
        def ladder(self, _tier: str) -> list[dict[str, Any]]:
            return []

        def match_ids(self, *_args: Any, **_kwargs: Any) -> list[str]:
            raise AssertionError("cached candidate should avoid Match-V5 history calls")

        def match(self, _match_id: str) -> dict[str, Any]:
            raise AssertionError("cached candidate should avoid Match-V5 detail calls")

    result = DiscoveryService(db, CachedApi()).discover(  # type: ignore[arg-type]
        dataset_id,
        run_id,
        "26.18",
        wanted_candidates=1,
    )

    assert result.histories_queried == 0
    assert result.details_queried == 0
    assert result.eligible_candidates == 1


def test_detail_ingest_makes_zero_calls_when_eligible_target_is_already_met(
    db: Database,
    match_factory: Callable[..., MatchRecord],
) -> None:
    dataset_id = db.dataset("KR", "26.18", "26.18.704.1234")
    run_id = db.start_run(dataset_id, "seed")
    db.add_discoveries(dataset_id, run_id, "p1", ["KR_cached", "KR_unknown"])
    db.store_match(dataset_id, match_factory("KR_cached", game_id="1001"), eligible=True)

    class NoDetailApi:
        def ladder(self, _tier: str) -> list[dict[str, Any]]:
            return []

        def match_ids(self, *_args: Any, **_kwargs: Any) -> list[str]:
            raise AssertionError("existing discovery pool should avoid history calls")

        def match(self, _match_id: str) -> dict[str, Any]:
            raise AssertionError("met eligible target must not fetch unknown match details")

    result = DiscoveryService(db, NoDetailApi()).discover(  # type: ignore[arg-type]
        dataset_id,
        run_id,
        "26.18",
        wanted_candidates=1,
    )

    assert result.histories_queried == 0
    assert result.details_queried == 0
    assert result.unique_discovered == 2
    assert result.eligible_candidates == 1
    assert not db.match_exists("KR_unknown")
