from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from global_rofl_collector.config import Config
from global_rofl_collector.db import Database
from global_rofl_collector.models import MatchRecord, Quality


@pytest.fixture
def db(tmp_path: Path) -> Iterator[Database]:
    database = Database(tmp_path / "collector.sqlite3")
    database.migrate()
    try:
        yield database
    finally:
        database.close()


@pytest.fixture
def config(tmp_path: Path) -> Config:
    return Config(
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        db_path=tmp_path / "data" / "collector.sqlite3",
        logs_dir=tmp_path / "logs",
        riot_api_key=None,
        league_install_dir=tmp_path / "League of Legends",
        replay_edge_base_url="https://player-platform.example.test",
        request_timeout=1.0,
        api_min_interval=0.0,
        api_max_retries=2,
        history_count=20,
    )


@pytest.fixture
def match_factory() -> Callable[..., MatchRecord]:
    def make_match(
        match_id: str = "KR_1001",
        *,
        game_id: str | None = None,
        platform: str = "KR",
        queue_id: int = 420,
        game_version: str = "26.18.704.1234",
        patch: str = "26.18",
        game_creation: int = 1_700_000_000_000,
        game_duration: int = 1_800,
        participants: list[str] | None = None,
        quality: Quality | None = None,
        ended: bool = True,
        raw_overrides: dict[str, Any] | None = None,
    ) -> MatchRecord:
        participant_ids = list(participants or [])
        raw: dict[str, Any] = {
            "metadata": {"matchId": match_id},
            "info": {
                "gameId": int(game_id or match_id.rsplit("_", 1)[-1]),
                "platformId": platform,
                "queueId": queue_id,
                "gameVersion": game_version,
                "gameCreation": game_creation,
                "gameDuration": game_duration,
                "gameEndTimestamp": game_creation + game_duration * 1_000 if ended else None,
                "participants": [{"puuid": puuid} for puuid in participant_ids],
            },
        }
        if raw_overrides:
            raw.update(raw_overrides)
        return MatchRecord(
            match_id=match_id,
            game_id=game_id or match_id.rsplit("_", 1)[-1],
            platform=platform,
            queue_id=queue_id,
            game_version=game_version,
            patch=patch,
            game_creation=game_creation,
            game_duration=game_duration,
            participants=participant_ids,
            quality=quality or Quality(0, 0, 0, 0, None),
            raw=raw,
        )

    return make_match
