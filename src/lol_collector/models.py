from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from collections.abc import Mapping
from typing import Final, NewType, assert_never

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator

PlayerId = NewType("PlayerId", int)
MatchId = NewType("MatchId", int)
ArtifactId = NewType("ArtifactId", int)

JsonScalar = str | int | float | bool | None
JsonValue = JsonScalar | list["JsonValue"] | Mapping[str, "JsonValue"]
JsonObject = dict[str, JsonValue]

UTC: Final = timezone.utc
PATCH_RE: Final = re.compile(r"^(\d+\.\d+)")
SECRET_KEYS: Final = frozenset(
    {"authorization", "token", "access_token", "entitlements_token", "entitlementstoken", "leaguesession", "password"}
)


class RunStatus(StrEnum):
    CREATED = "CREATED"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    COMPLETE = "COMPLETE"
    EXHAUSTED = "EXHAUSTED"
    PATCH_ROLLOVER = "PATCH_ROLLOVER"
    WAITING_FOR_CLIENT = "WAITING_FOR_CLIENT"
    PAUSED_STORAGE_ERROR = "PAUSED_STORAGE_ERROR"


class PlayerQueueState(StrEnum):
    NEW = "NEW"
    RANK_CHECKED = "RANK_CHECKED"
    MASTER_PLUS = "MASTER_PLUS"
    NOT_MASTER_PLUS = "NOT_MASTER_PLUS"
    CRAWLING = "CRAWLING"
    CRAWLED = "CRAWLED"
    FAILED = "FAILED"


class RankStatus(StrEnum):
    UNKNOWN = "UNKNOWN"
    VERIFIED = "VERIFIED"
    FAILED = "FAILED"


class CapabilityStatus(StrEnum):
    SUPPORTED = "SUPPORTED"
    UNSUPPORTED_CONFIRMED = "UNSUPPORTED_CONFIRMED"
    FAILED = "FAILED"
    BLOCKED_BY_DEPENDENCY = "BLOCKED_BY_DEPENDENCY"
    NOT_TESTED = "NOT_TESTED"
    UNKNOWN = "UNKNOWN"


class PaginationStatus(StrEnum):
    COMPLETE = "COMPLETE"
    INCOMPLETE = "INCOMPLETE"
    UNKNOWN = "UNKNOWN"


class MatchQueueState(StrEnum):
    DISCOVERED = "DISCOVERED"
    SUMMARY_DOWNLOADED = "SUMMARY_DOWNLOADED"
    DETAILS_DOWNLOADED = "DETAILS_DOWNLOADED"
    PARSED = "PARSED"
    FAILED = "FAILED"


class ArtifactType(StrEnum):
    CAPABILITY = "CAPABILITY"
    QUEUE = "QUEUE"
    LEADERBOARD = "LEADERBOARD"
    RANK = "RANK"
    HISTORY = "HISTORY"
    SUMMARY = "SUMMARY"
    DETAILS = "DETAILS"
    REPLAY = "REPLAY"


class ReplayDownloadStatus(StrEnum):
    NOT_REQUESTED = "NOT_REQUESTED"
    QUEUED = "QUEUED"
    DOWNLOADING = "DOWNLOADING"
    DOWNLOADED = "DOWNLOADED"
    DOWNLOADED_BUT_INVALID = "DOWNLOADED_BUT_INVALID"
    VALIDATED = "VALIDATED"
    FAILED = "FAILED"
    UNAVAILABLE = "UNAVAILABLE"


class ReplayValidationStatus(StrEnum):
    NOT_VALIDATED = "NOT_VALIDATED"
    VALIDATED = "VALIDATED"
    INVALID = "INVALID"


class ReplaySourceDisposition(StrEnum):
    BASELINE = "BASELINE"
    QUEUED = "QUEUED"
    CAPTURED = "CAPTURED"
    FAILED = "FAILED"
    UNAVAILABLE = "UNAVAILABLE"


class CollectorConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    region: str = Field(min_length=1)
    target_tiers: tuple[str, ...] = ("MASTER", "GRANDMASTER", "CHALLENGER")
    target_patch: str = Field(min_length=3)
    window_start_at: datetime
    window_end_at: datetime
    target_valid_matches: int = Field(ge=1)
    master_plus_threshold: int = Field(default=8, ge=0, le=10)
    require_known_rank_count: int = Field(default=10, ge=0, le=10)
    seed: int = Field(default=0, ge=0)

    @field_validator("window_end_at")
    @classmethod
    def end_after_start(cls, value: datetime, info: ValidationInfo) -> datetime:
        start = info.data.get("window_start_at")
        if isinstance(start, datetime) and value <= start:
            raise ValueError("window_end_at must be after window_start_at")
        return value


@dataclass(frozen=True, slots=True)
class RankSnapshot:
    player_id: PlayerId
    observed_at: datetime
    source: str
    region: str
    queue_type: str
    tier: str
    division: str | None
    lp: int | None
    wins: int | None
    losses: int | None
    raw_artifact_id: ArtifactId | None

    @property
    def is_master_plus(self) -> bool:
        return self.tier in {"MASTER", "GRANDMASTER", "CHALLENGER"}


@dataclass(frozen=True, slots=True)
class MatchQuality:
    participant_count: int
    known_rank_count: int
    unknown_rank_count: int
    master_count: int
    grandmaster_count: int
    challenger_count: int
    below_master_count: int

    @property
    def master_plus_count(self) -> int:
        return self.master_count + self.grandmaster_count + self.challenger_count

    def qualifies(self, *, threshold: int, required_known: int) -> bool:
        return (
            self.participant_count == 10
            and self.known_rank_count >= required_known
            and self.master_plus_count >= threshold
        )


def utc_now() -> datetime:
    return datetime.now(UTC)


def normalize_patch(game_version: str) -> str | None:
    match = PATCH_RE.match(game_version.strip())
    return None if match is None else match.group(1)


def schema_fingerprint(value: JsonValue) -> str:
    def shape(item: JsonValue) -> JsonValue:
        match item:
            case dict() as mapping:
                return {key: shape(value) for key, value in sorted(mapping.items())}
            case list() as items:
                return [shape(items[0])] if items else []
            case str():
                return "<str>"
            case bool():
                return "<bool>"
            case int():
                return "<int>"
            case float():
                return "<float>"
            case None:
                return "<null>"
            case unreachable:
                assert_never(unreachable)

    encoded = json.dumps(shape(value), ensure_ascii=False, sort_keys=True).encode()
    return hashlib.sha256(encoded).hexdigest()


def sanitize_json(value: JsonValue) -> JsonValue:
    def clean(item: JsonValue) -> JsonValue:
        match item:
            case dict() as mapping:
                return {
                    key: "<redacted>" if key.lower() in SECRET_KEYS else clean(child)
                    for key, child in mapping.items()
                    if key.lower() != "bearer"
                }
            case list() as items:
                return [clean(child) for child in items]
            case str() | int() | float() | bool() | None:
                return item
            case unreachable:
                assert_never(unreachable)

    return clean(value)
