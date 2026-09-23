from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class JobState(StrEnum):
    DISCOVERED = "DISCOVERED"
    ELIGIBLE = "ELIGIBLE"
    INELIGIBLE = "INELIGIBLE"
    QUEUED = "QUEUED"
    DOWNLOADING = "DOWNLOADING"
    DOWNLOADED = "DOWNLOADED"
    VERIFIED = "VERIFIED"
    UNAVAILABLE = "UNAVAILABLE"
    FAILED_RETRYABLE = "FAILED_RETRYABLE"
    FAILED_PERMANENT = "FAILED_PERMANENT"


class Capability(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    UNKNOWN = "UNKNOWN"
    ACTION_REQUIRED = "ACTION_REQUIRED"


TIER_WEIGHT = {"CHALLENGER": 3, "GRANDMASTER": 2, "MASTER": 1}


def patch_key(version: str | None) -> str | None:
    """Return the major.minor research patch while preserving exact versions elsewhere."""
    if not version:
        return None
    match = re.match(r"^(\d+)\.(\d+)(?:\.|$)", version.strip())
    if not match:
        return None
    return f"{int(match.group(1))}.{int(match.group(2))}"


def safe_build_component(version: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", version).strip("._")
    return cleaned or "UNKNOWN"


@dataclass(frozen=True, slots=True)
class Quality:
    challenger_count: int
    grandmaster_count: int
    master_count: int
    known_apex_count: int
    highest_tier: str | None

    @classmethod
    def from_participants(cls, puuids: list[str], tiers: dict[str, str]) -> Quality:
        counts = {"CHALLENGER": 0, "GRANDMASTER": 0, "MASTER": 0}
        for puuid in set(puuids):
            tier = tiers.get(puuid)
            if tier in counts:
                counts[tier] += 1
        highest = next((tier for tier in TIER_WEIGHT if counts[tier]), None)
        return cls(
            challenger_count=counts["CHALLENGER"],
            grandmaster_count=counts["GRANDMASTER"],
            master_count=counts["MASTER"],
            known_apex_count=sum(counts.values()),
            highest_tier=highest,
        )


@dataclass(frozen=True, slots=True)
class MatchRecord:
    match_id: str
    game_id: str
    platform: str
    queue_id: int
    game_version: str
    patch: str
    game_creation: int
    game_duration: int
    participants: list[str]
    quality: Quality
    raw: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ProbeEvidence:
    route: str
    capability: Capability
    mechanism: str
    http_status: int | None = None
    auth_result: str | None = None
    region_evidence: str | None = None
    client_log_excerpt: str | None = None
    evidence: dict[str, Any] | None = None

