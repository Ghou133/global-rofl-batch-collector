from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone

from lol_collector.models import JsonObject, JsonValue, PlayerId, RankSnapshot, normalize_patch, utc_now


@dataclass(frozen=True, slots=True)
class MatchSummary:
    """Parsed summary fields used by the V1 quality gate."""

    game_id: str
    queue_id: int | None
    map_id: int | None
    game_mode: str | None
    game_version: str | None
    game_start_at: datetime | None
    game_end_at: datetime | None
    participant_puuids: tuple[str, ...]


def leaderboard_entries(payload: JsonValue | None) -> list[JsonObject]:
    """Flatten the current LCU apex response into immutable seed entries."""
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, Mapping):
        return []
    direct = payload.get("entries")
    if isinstance(direct, list):
        return [item for item in direct if isinstance(item, dict)]
    result: list[JsonObject] = []
    divisions = payload.get("divisions")
    if not isinstance(divisions, list):
        return result
    for division in divisions:
        if not isinstance(division, Mapping):
            continue
        standings = division.get("standings")
        if isinstance(standings, list):
            result.extend(item for item in standings if isinstance(item, dict))
    return result


def extract_current_patch(payload: JsonValue | None) -> str | None:
    """Extract a normalized major/minor patch from known LCU response shapes."""
    candidates: list[str] = []

    def visit(value: JsonValue, key: str | None = None) -> None:
        if isinstance(value, str):
            if key is None or key.lower() in {"version", "gameversion", "patch", "currentpatch"}:
                candidates.append(value)
            return
        if isinstance(value, Mapping):
            for child_key, child in value.items():
                visit(child, child_key)
            return
        if isinstance(value, list):
            for child in value:
                visit(child, key)

    if payload is not None:
        visit(payload)
    for candidate in candidates:
        normalized = normalize_patch(candidate)
        if normalized is not None:
            return normalized
    return None


def parse_match_summary(payload: JsonValue | None) -> MatchSummary:
    """Parse an SGP summary while tolerating the metadata envelope."""
    body = payload.get("json") if isinstance(payload, Mapping) else None
    source = body if isinstance(body, Mapping) else payload
    mapping = source if isinstance(source, Mapping) else {}
    game_id = mapping.get("gameId")
    participants = mapping.get("participants")
    puuids: list[str] = []
    if isinstance(participants, list):
        for participant in participants:
            if not isinstance(participant, Mapping):
                continue
            puuid = participant.get("puuid")
            if isinstance(puuid, str) and puuid and puuid not in puuids:
                puuids.append(puuid)
    return MatchSummary(
        game_id=str(game_id) if isinstance(game_id, int | str) else "",
        queue_id=_int_value(mapping.get("queueId")),
        map_id=_int_value(mapping.get("mapId")),
        game_mode=_str_value(mapping.get("gameMode")),
        game_version=_str_value(mapping.get("gameVersion")),
        game_start_at=_timestamp(mapping.get("gameStartTimestamp")),
        game_end_at=_timestamp(mapping.get("gameEndTimestamp")),
        participant_puuids=tuple(puuids),
    )


def parse_rank_snapshot(
    puuid: str,
    payload: JsonValue | None,
    region: str,
    player_id: PlayerId = PlayerId(0),
    raw_artifact_id: int | None = None,
) -> RankSnapshot | None:
    """Select the exact Solo queue from an SGP ranked-stats response."""
    queues = payload.get("queues") if isinstance(payload, Mapping) else None
    if not isinstance(queues, list):
        return None
    solo: Mapping[str, JsonValue] | None = None
    for item in queues:
        if isinstance(item, Mapping) and item.get("queueType") == "RANKED_SOLO_5x5":
            solo = item
            break
    if solo is None or not isinstance(solo.get("tier"), str):
        return None
    if not puuid:
        return None
    return RankSnapshot(
        player_id=player_id,
        observed_at=utc_now(),
        source="SGP_COMMON",
        region=region,
        queue_type="RANKED_SOLO_5x5",
        tier=str(solo["tier"]).upper(),
        division=_str_value(solo.get("rank")),
        lp=_int_value(solo.get("leaguePoints")),
        wins=_int_value(solo.get("wins")),
        losses=_int_value(solo.get("losses")),
        raw_artifact_id=raw_artifact_id,
    )


def history_games(payload: JsonValue | None) -> list[JsonObject]:
    """Return the JSON game objects from one SGP history page."""
    games = payload.get("games") if isinstance(payload, Mapping) else None
    if not isinstance(games, list):
        return []
    result: list[JsonObject] = []
    for game in games:
        if not isinstance(game, Mapping):
            continue
        body = game.get("json")
        if isinstance(body, dict):
            result.append(body)
    return result


def is_master_plus(tier: str | None) -> bool:
    """Return whether a Solo rank tier meets the seed expansion boundary."""
    return tier in {"MASTER", "GRANDMASTER", "CHALLENGER"}


def _timestamp(value: JsonValue) -> datetime | None:
    if isinstance(value, (int, float)):
        number = float(value)
        if number > 100_000_000_000:
            number /= 1000
        return datetime.fromtimestamp(number, tz=timezone.utc)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)
    return None


def _int_value(value: JsonValue) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _str_value(value: JsonValue) -> str | None:
    return value if isinstance(value, str) else None
