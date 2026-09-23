from __future__ import annotations

from collections.abc import Mapping

from lol_collector.models import (
    CapabilityStatus,
    JsonObject,
    JsonValue,
    PaginationStatus,
    schema_fingerprint,
    utc_now,
)
from lol_collector.transport import HttpResponse


def find_solo_queue(items: list[JsonValue]) -> int | None:
    for item in items:
        if not isinstance(item, dict):
            continue
        queue_id = item.get("id")
        if item.get("type") == "RANKED_SOLO_5x5" and isinstance(queue_id, int):
            return queue_id
    return None


def queue_entry(items: list[JsonValue], queue_id: int) -> JsonObject | None:
    for item in items:
        if isinstance(item, dict) and item.get("id") == queue_id:
            return item
    return None


def ranked_queue_types(payload: JsonValue | None) -> set[str]:
    found: set[str] = set()

    def visit(item: JsonValue) -> None:
        if isinstance(item, Mapping):
            for key, child in item.items():
                if key == "queueType" and isinstance(child, str):
                    found.add(child)
                else:
                    visit(child)
        elif isinstance(item, list):
            for child in item:
                visit(child)

    if payload is not None:
        visit(payload)
    return found


def current_puuid(payload: JsonValue | None) -> tuple[str | None, str | None]:
    if not isinstance(payload, Mapping):
        return None, None
    direct = payload.get("puuid")
    if isinstance(direct, str) and direct:
        return direct, "$.puuid"

    matches: list[tuple[str, str]] = []

    def visit(item: JsonValue, path: str) -> None:
        if isinstance(item, Mapping):
            for key, child in item.items():
                child_path = f"{path}.{key}"
                if key.lower() == "puuid" and isinstance(child, str) and child:
                    matches.append((child, child_path))
                else:
                    visit(child, child_path)
        elif isinstance(item, list):
            for index, child in enumerate(item):
                visit(child, f"{path}[{index}]")

    visit(payload, "$")
    return matches[0] if matches else (None, None)


def apex_info(response: HttpResponse, payload: JsonValue | None) -> JsonObject:
    entries: list[JsonValue] = []
    recognized = False
    if isinstance(payload, Mapping):
        direct_entries = payload.get("entries")
        divisions = payload.get("divisions")
        if isinstance(direct_entries, list):
            entries.extend(direct_entries)
            recognized = True
        elif isinstance(divisions, list):
            recognized = True
            for division in divisions:
                if isinstance(division, Mapping):
                    standings = division.get("standings")
                    if isinstance(standings, list):
                        entries.extend(standings)
    elif isinstance(payload, list):
        entries = payload
        recognized = True

    first = entries[0] if entries and isinstance(entries[0], Mapping) else {}
    keys = {str(key).lower() for key in first}
    success = response.status_code < 400
    capability = (
        CapabilityStatus.SUPPORTED
        if success and recognized
        else CapabilityStatus.UNKNOWN
        if success
        else CapabilityStatus.FAILED
    )
    pagination = PaginationStatus.UNKNOWN
    error_code = payload.get("errorCode") if isinstance(payload, Mapping) else None
    message = payload.get("message") if isinstance(payload, Mapping) else None
    return {
        "status": response.status_code,
        "success": success,
        "capability_status": capability.value,
        "entry_count": len(entries),
        "pagination_status": pagination.value,
        "APEX_PUUID": "puuid" in keys,
        "APEX_SUMMONER_ID": "summonerid" in keys,
        "APEX_RIOT_ID": "riotid" in keys,
        "tier": "tier" in keys,
        "lp": bool({"lp", "leaguepoints"} & keys),
        "errorCode": error_code if isinstance(error_code, str) else None,
        "message": message if isinstance(message, str) else None,
        "schema_fingerprint": schema_fingerprint(payload),
    }


def error_evidence(endpoint: str, response: HttpResponse) -> JsonObject:
    payload = response.payload
    error_code = payload.get("errorCode") if isinstance(payload, Mapping) else None
    message = payload.get("message") if isinstance(payload, Mapping) else None
    return {
        "http_status": response.status_code,
        "endpoint": endpoint,
        "timestamp": utc_now().isoformat(),
        "response_body": payload,
        "errorCode": error_code if isinstance(error_code, str) else None,
        "message": message if isinstance(message, str) else None,
        "schema_fingerprint": schema_fingerprint(payload),
    }


def game_id_from_history(payloads: list[JsonObject]) -> str | None:
    fallback: str | None = None
    for payload in payloads:
        games = payload.get("games")
        if not isinstance(games, list):
            continue
        for game in games:
            if not isinstance(game, Mapping):
                continue
            game_json = game.get("json")
            if not isinstance(game_json, Mapping):
                continue
            game_id = game_json.get("gameId")
            if isinstance(game_id, int | str) and str(game_id):
                candidate = str(game_id)
                if game_json.get("queueId") == 420:
                    return candidate
                fallback = fallback or candidate
    return fallback


def participant_puuids(payload: JsonValue | None) -> list[str]:
    if not isinstance(payload, Mapping):
        return []
    game_json = payload.get("json")
    if not isinstance(game_json, Mapping):
        return []
    participants = game_json.get("participants")
    if not isinstance(participants, list):
        return []
    result: list[str] = []
    for participant in participants:
        if not isinstance(participant, Mapping):
            continue
        puuid = participant.get("puuid")
        if isinstance(puuid, str) and puuid and puuid not in result:
            result.append(puuid)
    return result
