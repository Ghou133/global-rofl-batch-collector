from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest

from global_rofl_collector.cli import build_parser
from global_rofl_collector.config import Config
from global_rofl_collector.db import Database
from global_rofl_collector.discovery import DiscoveryService
from global_rofl_collector.errors import ConfigurationError, RiotApiError
from global_rofl_collector.manifest import manifest_path
from global_rofl_collector.platforms import PLATFORM_ROUTES, platform_route
from global_rofl_collector.replay import replay_paths
from global_rofl_collector.riot import PatchResolver, RiotApi
from global_rofl_collector.service import CollectorService


@pytest.mark.parametrize(
    ("platform", "region", "realm"),
    [
        ("KR", "ASIA", "kr"),
        ("JP1", "ASIA", "jp"),
        ("NA1", "AMERICAS", "na"),
        ("BR1", "AMERICAS", "br"),
        ("LA1", "AMERICAS", "lan"),
        ("LA2", "AMERICAS", "las"),
        ("EUW1", "EUROPE", "euw"),
        ("EUN1", "EUROPE", "eune"),
        ("TR1", "EUROPE", "tr"),
        ("RU", "EUROPE", "ru"),
        ("ME1", "EUROPE", "me"),
        ("OC1", "SEA", "oce"),
        ("PH2", "SEA", "ph"),
        ("SG2", "SEA", "sg"),
        ("TH2", "SEA", "th"),
        ("TW2", "SEA", "tw"),
        ("VN2", "SEA", "vn"),
    ],
)
def test_platform_routing_table(platform: str, region: str, realm: str) -> None:
    route = platform_route(platform.lower())
    assert route.platform == platform
    assert route.match_region == region
    assert route.realm == realm
    assert route.league_base == f"https://{platform.lower()}.api.riotgames.com"
    assert route.match_base == f"https://{region.lower()}.api.riotgames.com"
    assert route.realm_url.endswith(f"/realms/{realm}.json")


def test_platform_selection_preserves_kr_default_and_rejects_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("COLLECTOR_PLATFORM", raising=False)
    assert Config.load(tmp_path).platform == "KR"
    assert Config.load(tmp_path, platform="oc1").platform == "OC1"
    monkeypatch.setenv("COLLECTOR_PLATFORM", "na1")
    assert Config.load(tmp_path).platform == "NA1"
    assert Config.load(tmp_path, platform="euw1").platform == "EUW1"
    with pytest.raises(ConfigurationError, match="PLATFORM_UNSUPPORTED"):
        Config.load(tmp_path, platform="CN")
    assert build_parser().parse_args(["--platform", "oc1", "status"]).platform == "OC1"
    assert set(PLATFORM_ROUTES) == set(build_parser()._option_string_actions["--platform"].choices)


@pytest.mark.parametrize(
    ("platform", "region", "realm"),
    [("KR", "ASIA", "kr"), ("NA1", "AMERICAS", "na"), ("OC1", "SEA", "oce")],
)
def test_riot_requests_use_platform_and_regional_hosts(
    platform: str, region: str, realm: str
) -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if "/lol/league/v4/" in request.url.path:
            return httpx.Response(200, json={"entries": []})
        if request.url.path.endswith("/ids"):
            return httpx.Response(200, json=[])
        if "/lol/match/v5/matches/" in request.url.path:
            return httpx.Response(200, json={"metadata": {}, "info": {}})
        return httpx.Response(200, json={"v": "26.18.1"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    try:
        api = RiotApi("placeholder", platform=platform, client=client, min_interval=0)
        api.ladder("CHALLENGER")
        api.match_ids("player", count=1)
        api.match(f"{platform}_1")
        assert PatchResolver(platform=platform, client=client).resolve() == ("26.18", "26.18.1")
    finally:
        client.close()

    assert seen[0].startswith(f"https://{platform.lower()}.api.riotgames.com/lol/league/v4/")
    assert seen[1].startswith(f"https://{region.lower()}.api.riotgames.com/lol/match/v5/")
    assert seen[2].startswith(f"https://{region.lower()}.api.riotgames.com/lol/match/v5/")
    assert seen[3] == f"https://ddragon.leagueoflegends.com/realms/{realm}.json"


def test_platform_paths_and_status_are_isolated(config: Config, tmp_path: Path) -> None:
    row = {
        "platform": "NA1",
        "patch_key": "26.18",
        "game_version_exact": "26.18.704.1234",
        "match_id": "NA1_123",
    }
    final, partial = replay_paths(config.data_dir, row)
    assert final == config.data_dir / "NA1/26.18/builds/26.18.704.1234/rofl/NA1_123.rofl"
    assert partial == final.with_suffix(".rofl.partial")
    assert manifest_path(config.data_dir, "26.18", "NA1") == (
        config.data_dir / "NA1/26.18/manifests/dataset_manifest.jsonl"
    )

    selected = replace(config, platform="NA1")
    with CollectorService(selected) as service:
        service.db.dataset("KR", "26.17", "26.17.1")
        assert service.status()["current_patch"] == "UNKNOWN"
        service.db.dataset("NA1", "26.18", "26.18.1")
        status = service.status()
        assert status["platform"] == "NA1"
        assert status["current_patch"] == "26.18"
        assert status["manifest"] == str(manifest_path(tmp_path / "data", "26.18", "NA1"))


def test_discovery_eligibility_uses_selected_platform(match_factory: Any) -> None:
    match = match_factory(match_id="NA1_123", platform="NA1")
    assert DiscoveryService.eligible(match, "26.18", "NA1")
    assert not DiscoveryService.eligible(match, "26.18", "KR")


def test_discovery_does_not_ingest_cross_platform_history(
    db: Database, match_factory: Any
) -> None:
    dataset_id = db.dataset("NA1", "26.18", "26.18.1")
    run_id = db.start_run(dataset_id, "test")
    db.add_discoveries(dataset_id, run_id, "p1", ["KR_old"])
    details_requested: list[str] = []

    class MixedHistoryApi:
        def ladder(self, tier: str) -> list[dict[str, Any]]:
            if tier != "CHALLENGER":
                return []
            return [
                {
                    "puuid": "p1",
                    "summonerId": "sum-p1",
                    "rank": "I",
                    "leaguePoints": 1_000,
                    "wins": 1,
                    "losses": 1,
                }
            ]

        def match_ids(self, _puuid: str, *, count: int, start: int = 0) -> list[str]:
            assert count == 20
            return ["KR_new", "NA1_1"] if start == 0 else ["JP1_new", "NA1_2"]

        def match(self, match_id: str) -> dict[str, Any]:
            details_requested.append(match_id)
            return match_factory(match_id=match_id, platform="NA1").raw

    result = DiscoveryService(db, MixedHistoryApi(), platform="NA1").discover(  # type: ignore[arg-type]
        dataset_id, run_id, "26.18", wanted_candidates=2
    )

    assert result.unique_discovered == 2
    assert result.details_queried == 2
    assert result.eligible_candidates == 2
    assert details_requested == ["NA1_1", "NA1_2"]
    assert set(db.discovered_match_ids(dataset_id)) == {"KR_old", "NA1_1", "NA1_2"}
    assert all(db.match_row(match_id) is None for match_id in ("KR_old", "KR_new", "JP1_new"))


def test_discovery_rejects_detail_with_another_platform_identity(
    db: Database, match_factory: Any
) -> None:
    dataset_id = db.dataset("NA1", "26.18", "26.18.1")
    run_id = db.start_run(dataset_id, "test")

    class MismatchedDetailApi:
        def ladder(self, tier: str) -> list[dict[str, Any]]:
            if tier != "CHALLENGER":
                return []
            return [
                {
                    "puuid": "p1",
                    "summonerId": "sum-p1",
                    "rank": "I",
                    "leaguePoints": 1_000,
                    "wins": 1,
                    "losses": 1,
                }
            ]

        def match_ids(self, _puuid: str, *, count: int, start: int = 0) -> list[str]:
            return ["NA1_3"] if start == 0 else []

        def match(self, _match_id: str) -> dict[str, Any]:
            return match_factory(match_id="NA1_3", platform="KR").raw

    with pytest.raises(RiotApiError, match="API_SCHEMA_CHANGED"):
        DiscoveryService(db, MismatchedDetailApi(), platform="NA1").discover(  # type: ignore[arg-type]
            dataset_id, run_id, "26.18", wanted_candidates=1
        )
    assert db.match_row("NA1_3") is None
