from __future__ import annotations

import email.utils
import random
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC
from typing import Any
from urllib.parse import quote

import httpx

from .errors import RiotApiError
from .models import MatchRecord, Quality, patch_key

KR_PLATFORM_BASE = "https://kr.api.riotgames.com"
ASIA_REGIONAL_BASE = "https://asia.api.riotgames.com"
KR_REALM_URL = "https://ddragon.leagueoflegends.com/realms/kr.json"
RANKED_SOLO_QUEUE = "RANKED_SOLO_5x5"
RANKED_SOLO_QUEUE_ID = 420


def _parse_rate_pairs(value: str | None) -> list[tuple[int, float]]:
    pairs: list[tuple[int, float]] = []
    if not value:
        return pairs
    for item in value.split(","):
        try:
            first, second = item.strip().split(":", 1)
            pairs.append((int(first), float(second)))
        except (TypeError, ValueError):
            continue
    return pairs


def _retry_after(value: str | None, wall_clock: Callable[[], float]) -> float:
    if not value:
        return 1.0
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            target = email.utils.parsedate_to_datetime(value)
            if target.tzinfo is None:
                target = target.replace(tzinfo=UTC)
            return max(0.0, target.timestamp() - wall_clock())
        except (TypeError, ValueError, OverflowError):
            return 1.0


@dataclass(slots=True)
class RateLimiter:
    min_interval: float = 1.25
    monotonic: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep
    last_request: float | None = None
    blocked_until: float = 0.0

    def before_request(self) -> None:
        now = self.monotonic()
        delay = max(0.0, self.blocked_until - now)
        if self.last_request is not None:
            delay = max(delay, self.last_request + self.min_interval - now)
        if delay > 0:
            self.sleep(delay)
        self.last_request = self.monotonic()

    def observe(self, headers: Mapping[str, str]) -> None:
        now = self.monotonic()
        for limit_header, count_header in (
            ("X-App-Rate-Limit", "X-App-Rate-Limit-Count"),
            ("X-Method-Rate-Limit", "X-Method-Rate-Limit-Count"),
        ):
            limits = {
                window: count
                for count, window in _parse_rate_pairs(headers.get(limit_header))
            }
            counts = _parse_rate_pairs(headers.get(count_header))
            for count, window in counts:
                limit = limits.get(window)
                if limit is not None and count >= limit:
                    self.blocked_until = max(self.blocked_until, now + window)

    def penalize(self, seconds: float) -> None:
        self.blocked_until = max(self.blocked_until, self.monotonic() + max(0.0, seconds))


class RiotApi:
    def __init__(
        self,
        api_key: str,
        *,
        timeout: float = 30.0,
        min_interval: float = 1.25,
        max_retries: int = 6,
        client: httpx.Client | None = None,
        limiter: RateLimiter | None = None,
        sleep: Callable[[float], None] = time.sleep,
        rng: random.Random | None = None,
    ):
        self._key = api_key
        self._client = client or httpx.Client(timeout=timeout, follow_redirects=True)
        self._owns_client = client is None
        self._limiter = limiter or RateLimiter(min_interval=min_interval, sleep=sleep)
        self._sleep = sleep
        self._rng = rng or random.Random()
        self._max_retries = max_retries

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> RiotApi:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _request_json(self, url: str) -> Any:
        last_error: RiotApiError | None = None
        for attempt in range(self._max_retries + 1):
            self._limiter.before_request()
            try:
                response = self._client.get(url, headers={"X-Riot-Token": self._key})
            except httpx.TransportError as exc:
                last_error = RiotApiError(
                    "API_NETWORK_ERROR", type(exc).__name__, retryable=True
                )
                if attempt >= self._max_retries:
                    raise last_error from exc
                self._sleep(min(30.0, 0.5 * (2**attempt)) + self._rng.uniform(0, 0.25))
                continue

            self._limiter.observe(response.headers)
            status = response.status_code
            if 200 <= status < 300:
                try:
                    return response.json()
                except ValueError as exc:
                    raise RiotApiError(
                        "API_INVALID_RESPONSE",
                        "Riot API returned non-JSON success content",
                        retryable=True,
                        http_status=status,
                    ) from exc

            if status == 429:
                retry = _retry_after(response.headers.get("Retry-After"), time.time)
                self._limiter.penalize(retry)
                last_error = RiotApiError(
                    "RATE_LIMITED",
                    f"Riot API rate limited the request; retry after {retry:.1f}s",
                    retryable=True,
                    http_status=status,
                )
                if attempt >= self._max_retries:
                    raise last_error
                continue

            if status >= 500:
                last_error = RiotApiError(
                    "API_SERVER_ERROR",
                    f"Riot API returned HTTP {status}",
                    retryable=True,
                    http_status=status,
                )
                if attempt >= self._max_retries:
                    raise last_error
                self._sleep(min(30.0, 0.5 * (2**attempt)) + self._rng.uniform(0, 0.25))
                continue

            if status == 401:
                raise RiotApiError(
                    "API_KEY_INVALID",
                    "Riot rejected the API credential (HTTP 401)",
                    http_status=status,
                )
            if status == 403:
                raise RiotApiError(
                    "API_FORBIDDEN",
                    "Riot rejected or forbade the API key; development keys may have expired",
                    http_status=status,
                )
            if status == 404:
                raise RiotApiError(
                    "API_NOT_FOUND", "Riot resource not found", http_status=status
                )
            raise RiotApiError(
                "API_HTTP_ERROR", f"Riot API returned HTTP {status}", http_status=status
            )
        assert last_error is not None
        raise last_error

    def ladder(self, tier: str) -> list[dict[str, Any]]:
        endpoint = {
            "CHALLENGER": "challengerleagues",
            "GRANDMASTER": "grandmasterleagues",
            "MASTER": "masterleagues",
        }.get(tier.upper())
        if not endpoint:
            raise ValueError(f"Unsupported apex tier: {tier}")
        data = self._request_json(
            f"{KR_PLATFORM_BASE}/lol/league/v4/{endpoint}/by-queue/{RANKED_SOLO_QUEUE}"
        )
        if not isinstance(data, dict) or not isinstance(data.get("entries"), list):
            raise RiotApiError(
                "API_SCHEMA_CHANGED", f"Unexpected League-V4 response for {tier}"
            )
        return list(data["entries"])

    def match_ids(self, puuid: str, *, count: int = 20, start: int = 0) -> list[str]:
        encoded = quote(puuid, safe="")
        data = self._request_json(
            f"{ASIA_REGIONAL_BASE}/lol/match/v5/matches/by-puuid/{encoded}/ids"
            f"?queue={RANKED_SOLO_QUEUE_ID}&start={start}&count={max(1, min(100, count))}"
        )
        if not isinstance(data, list) or not all(isinstance(item, str) for item in data):
            raise RiotApiError("API_SCHEMA_CHANGED", "Unexpected Match-V5 ID response")
        return list(data)

    def match(self, match_id: str) -> dict[str, Any]:
        data = self._request_json(
            f"{ASIA_REGIONAL_BASE}/lol/match/v5/matches/{quote(match_id, safe='')}"
        )
        if not isinstance(data, dict) or "metadata" not in data or "info" not in data:
            raise RiotApiError("API_SCHEMA_CHANGED", "Unexpected Match-V5 match response")
        return data


class PatchResolver:
    def __init__(self, *, timeout: float = 30.0, client: httpx.Client | None = None):
        self._client = client or httpx.Client(timeout=timeout, follow_redirects=True)
        self._owns_client = client is None

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def resolve(self) -> tuple[str, str]:
        try:
            response = self._client.get(KR_REALM_URL)
            response.raise_for_status()
            data = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise RiotApiError(
                "PATCH_RESOLUTION_FAILED", "Could not read the official KR realm", retryable=True
            ) from exc
        exact = data.get("v") if isinstance(data, dict) else None
        patch = patch_key(exact)
        if not isinstance(exact, str) or patch is None:
            raise RiotApiError("PATCH_SCHEMA_CHANGED", "KR realm did not contain a usable version")
        return patch, exact


def parse_match(data: dict[str, Any], tiers: dict[str, str]) -> MatchRecord:
    metadata = data.get("metadata") or {}
    info = data.get("info") or {}
    match_id = metadata.get("matchId")
    version = info.get("gameVersion")
    patch = patch_key(version)
    participants_data = info.get("participants") or []
    participants = [
        participant.get("puuid")
        for participant in participants_data
        if isinstance(participant, dict) and participant.get("puuid")
    ]
    platform = info.get("platformId") or (match_id.split("_", 1)[0] if match_id else None)
    game_id = info.get("gameId")
    if (
        not match_id
        or not isinstance(version, str)
        or patch is None
        or not platform
        or game_id is None
    ):
        raise RiotApiError("API_SCHEMA_CHANGED", "Match-V5 response lacks stable identity/version")
    return MatchRecord(
        match_id=str(match_id),
        game_id=str(game_id),
        platform=str(platform).upper(),
        queue_id=int(info.get("queueId", -1)),
        game_version=version,
        patch=patch,
        game_creation=int(info.get("gameCreation", 0)),
        game_duration=int(info.get("gameDuration", 0)),
        participants=participants,
        quality=Quality.from_participants(participants, tiers),
        raw=data,
    )
