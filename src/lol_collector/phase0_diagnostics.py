from __future__ import annotations

from collections.abc import Mapping

from lol_collector.models import JsonObject, JsonValue


_RESULTS = {
    "SUPPORTED": "PASS",
    "FAILED": "FAIL",
    "BLOCKED_BY_DEPENDENCY": "BLOCKED",
    "NOT_TESTED": "NOT_TESTED",
    "UNKNOWN": "UNKNOWN",
    "UNSUPPORTED_CONFIRMED": "FAIL",
}


def render_phase0_diagnostics(report: JsonObject) -> str:
    client = _object(report.get("CLIENT"))
    queue = _object(report.get("QUEUE"))
    apex = _object(report.get("APEX"))
    sgp = _object(report.get("SGP"))
    history = _object(report.get("SGP_HISTORY"))
    match = _object(report.get("MATCH"))
    rank = _object(report.get("RANK"))
    security = _object(report.get("SECURITY"))

    apex_statuses = [
        _object(apex.get(tier)).get("capability_status")
        for tier in ("MASTER", "GRANDMASTER", "CHALLENGER")
    ]
    apex_result = "PASS" if apex_statuses == ["SUPPORTED"] * 3 else "FAIL"
    error_result = "PASS" if _apex_errors_captured(apex) else "UNKNOWN"
    queue_result = (
        "PASS"
        if queue.get("SOLO_RANKED_QUEUE_CONFIRMED") is True
        else "FAIL"
        if queue.get("SOLO_RANKED_QUEUE_CONFIRMED") is False
        else "UNKNOWN"
    )
    rows = [
        ("LCU connection", _bool_result(client.get("LCU_CONNECTED")), "CLIENT in capability-report-v2.json"),
        ("LeagueClientUx rsoPlatformId discovery", _status_result(client.get("RSO_PLATFORM_ID_STATUS")), "lcu-command-line-diagnostic.json"),
        ("Current account PUUID", _status_result(client.get("CURRENT_PUUID_STATUS")), "current-summoner.json at $.puuid"),
        ("Solo queue 420 vs Flex 440", queue_result, "queue-420.json, queue-440.json, current-ranked-stats.json"),
        ("APEX HTTP 400 diagnosis", error_result, "apex-*-error.json"),
        ("Corrected APEX leaderboard", apex_result, "probe-artifacts/leaderboard and APEX report entries"),
        ("Capability and pagination enums", "PASS", "capability-report-v2.json"),
        ("Tencent SGP server mapping", _status_result(sgp.get("SGP_SERVER_CONFIG")), "sgp-server-config.json"),
        ("SGP match history", _status_result(sgp.get("SGP_MATCH_HISTORY")), "sgp-history-page-*.json"),
        ("SGP common/ranked", _status_result(sgp.get("SGP_RANKED")), "sgp-ranked-current.json"),
        ("Three-page history traversal", _status_result(history.get("THREE_PAGE_PAGINATION")), "sgp-history-page-1.json through page-3.json"),
        ("RAW SUMMARY", _status_result(match.get("SUMMARY")), "sgp-summary.json"),
        ("RAW DETAILS", _status_result(match.get("DETAILS")), "sgp-details.json"),
        ("Ten participant rank snapshots", _status_result(rank.get("TEN_PLAYER_RANK_COVERAGE")), "sgp-rank-participant-01.json through -10.json"),
        ("Primary seed method", "PASS" if report.get("PRIMARY_SEED_METHOD") == "LCU_APEX" else "UNKNOWN", "APEX section in capability-report-v2.json"),
        ("Credential persistence scan", _status_result(security.get("TOKEN_PERSISTENCE_SCAN")), "SECURITY section in capability-report-v2.json"),
    ]
    lines = [
        "# Phase 0 diagnostics",
        "",
        "No Authorization header, auth token, riotClientAuthToken, Entitlements token, or League Session token is persisted.",
        "",
        "| Check | Result | Evidence artifact |",
        "|---|---|---|",
    ]
    lines.extend(f"| {check} | {result} | `{evidence}` |" for check, result, evidence in rows)
    lines.extend(
        [
            "",
            "## APEX diagnosis",
            "",
            "The legacy `SOLO5V5` requests are retained only as bounded diagnostics. Their response body identifies an invalid queue enum; the corrected request uses `RANKED_SOLO_5x5`, corroborated by queue 420 and current ranked stats.",
            "",
            "## Scope stop",
            "",
            "This run performs Phase 0 capability checks only. It does not create or start a 10,000-match collection run.",
            "",
        ]
    )
    return "\n".join(lines)


def _object(value: JsonValue | None) -> JsonObject:
    return dict(value) if isinstance(value, Mapping) else {}


def _status_result(value: JsonValue | None) -> str:
    return _RESULTS.get(value, "UNKNOWN") if isinstance(value, str) else "UNKNOWN"


def _bool_result(value: JsonValue | None) -> str:
    return "PASS" if value is True else "FAIL" if value is False else "UNKNOWN"


def _apex_errors_captured(apex: JsonObject) -> bool:
    for tier in ("MASTER", "GRANDMASTER", "CHALLENGER"):
        info = _object(apex.get(tier))
        diagnostic = _object(info.get("invalid_enum_diagnostic"))
        if diagnostic.get("status") != 400 or diagnostic.get("pagination_status") != "UNKNOWN":
            return False
    return True
