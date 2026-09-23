from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from lol_collector.models import JsonObject, JsonValue, normalize_patch, schema_fingerprint


@dataclass(frozen=True, slots=True)
class LeaderboardEntry:
    puuid: str | None
    summoner_id: str | None
    riot_id: str | None
    tier: str
    lp: int | None
    position: int | None
    raw: JsonObject


@dataclass(frozen=True, slots=True)
class QueueMetadata:
    queue_id: int
    queue_type: str | None
    game_mode: str | None
    map_id: int | None
    ranked: bool
    players_per_team: int | None
    schema_hash: str


@dataclass(frozen=True, slots=True)
class Participant:
    participant_id: int
    puuid: str
    team_id: int | None


@dataclass(frozen=True, slots=True)
class MatchSummary:
    game_id: str
    queue_id: int | None
    game_mode: str | None
    map_id: int | None
    game_version_full: str
    patch_normalized: str | None
    game_start_at: datetime | None
    game_end_at: datetime | None
    participants: tuple[Participant, ...]


def parse_leaderboard(payload: JsonValue, tier: str) -> tuple[LeaderboardEntry, ...]:
    entries = _list(payload, "entries")
    result: list[LeaderboardEntry] = []
    for position, item in enumerate(entries, 1):
        if not isinstance(item, dict):
            continue
        result.append(LeaderboardEntry(_text(item, "puuid"), _text(item, "summonerId"), _text(item, "riotId"), _text(item, "tier") or tier, _integer(item, "lp"), _integer(item, "rank") or position, item))
    return tuple(result)


def parse_queue_metadata(payload: JsonValue) -> tuple[QueueMetadata, ...]:
    if not isinstance(payload, list):
        return ()
    result: list[QueueMetadata] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        queue_id = _integer(item, "id")
        if queue_id is None:
            continue
        result.append(QueueMetadata(queue_id, _text(item, "queueType"), _text(item, "gameMode"), _integer(item, "mapId"), item.get("isRanked") is True, _integer(item, "numPlayersPerTeam"), schema_fingerprint(item)))
    return tuple(result)


def parse_summary(payload: JsonValue) -> MatchSummary:
    if not isinstance(payload, dict):
        raise ValueError("summary payload must be an object")
    game_id = _text(payload, "gameId")
    game_version = _text(payload, "gameVersion")
    if game_id is None or game_version is None:
        raise ValueError("summary is missing gameId or gameVersion")
    participants: list[Participant] = []
    for index, item in enumerate(_list(payload, "participants")):
        if not isinstance(item, dict):
            continue
        puuid = _text(item, "puuid")
        if puuid is None:
            continue
        participants.append(Participant(_integer(item, "participantId") or index + 1, puuid, _integer(item, "teamId")))
    return MatchSummary(game_id, _integer(payload, "queueId"), _text(payload, "gameMode"), _integer(payload, "mapId"), game_version, normalize_patch(game_version), _time(payload, "gameStartTime"), _time(payload, "gameEndTime"), tuple(participants))


def details_game_id(payload: JsonValue) -> str:
    if not isinstance(payload, dict):
        raise ValueError("details payload must be an object")
    game_id = _text(payload, "gameId")
    if game_id is None:
        raise ValueError("details is missing gameId")
    return game_id


def _list(mapping: JsonValue, key: str) -> list[JsonValue]:
    if isinstance(mapping, dict) and isinstance(mapping.get(key), list):
        candidate = mapping[key]
        if isinstance(candidate, list):
            return candidate
    return []


def _text(mapping: JsonObject, key: str) -> str | None:
    value = mapping.get(key)
    return value if isinstance(value, str) else None


def _integer(mapping: JsonObject, key: str) -> int | None:
    value = mapping.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _time(mapping: JsonObject, key: str) -> datetime | None:
    value = mapping.get(key)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if isinstance(value, int):
        return datetime.fromtimestamp(value / 1000, tz=timezone.utc)
    return None
