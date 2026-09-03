from __future__ import annotations

import random
from collections.abc import Mapping

import httpx
import pytest

from kr_rofl_collector.errors import RiotApiError
from kr_rofl_collector.riot import RateLimiter, RiotApi


class FakeClock:
    def __init__(self, start: float = 0.0):
        self.now = start
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def test_rate_limiter_honors_interval_and_observed_riot_window() -> None:
    clock = FakeClock(10.0)
    limiter = RateLimiter(
        min_interval=1.25,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )

    limiter.before_request()
    limiter.before_request()
    limiter.observe(
        {
            "X-App-Rate-Limit": "2:10,100:120",
            "X-App-Rate-Limit-Count": "2:10,1:120",
        }
    )
    limiter.before_request()

    assert clock.sleeps == pytest.approx([1.25, 10.0])
    assert clock.now == pytest.approx(21.25)


def test_rate_limiter_ignores_malformed_headers() -> None:
    clock = FakeClock()
    limiter = RateLimiter(min_interval=0, monotonic=clock.monotonic, sleep=clock.sleep)
    headers: Mapping[str, str] = {
        "X-App-Rate-Limit": "bad,20:nope,10:1",
        "X-App-Rate-Limit-Count": "not-a-pair,9:1",
    }

    limiter.observe(headers)
    limiter.before_request()

    assert clock.sleeps == []


def test_429_retry_after_blocks_then_retries_with_same_credential() -> None:
    clock = FakeClock()
    seen_tokens: list[str | None] = []
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        seen_tokens.append(request.headers.get("X-Riot-Token"))
        if calls == 1:
            return httpx.Response(429, headers={"Retry-After": "17"}, json={"status": {}})
        return httpx.Response(200, json={"entries": []})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    limiter = RateLimiter(min_interval=0, monotonic=clock.monotonic, sleep=clock.sleep)
    api = RiotApi(
        "test-key-not-a-secret",
        client=client,
        limiter=limiter,
        sleep=clock.sleep,
        rng=random.Random(0),
        max_retries=1,
    )
    try:
        assert api.ladder("CHALLENGER") == []
    finally:
        client.close()

    assert calls == 2
    assert seen_tokens == ["test-key-not-a-secret", "test-key-not-a-secret"]
    # Retry-After is deliberately much larger than exponential backoff plus jitter.
    assert clock.sleeps == pytest.approx([17.0])


def test_429_exhaustion_remains_retryable_and_distinct_from_auth_failure() -> None:
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(429, headers={"Retry-After": "0"})
        )
    )
    clock = FakeClock()
    api = RiotApi(
        "placeholder",
        client=client,
        limiter=RateLimiter(min_interval=0, monotonic=clock.monotonic, sleep=clock.sleep),
        sleep=clock.sleep,
        max_retries=0,
    )
    try:
        with pytest.raises(RiotApiError) as raised:
            api.ladder("CHALLENGER")
    finally:
        client.close()

    assert raised.value.code == "RATE_LIMITED"
    assert raised.value.retryable is True
    assert raised.value.http_status == 429


@pytest.mark.parametrize(
    ("status", "code"),
    [(401, "API_KEY_INVALID"), (403, "API_FORBIDDEN")],
)
def test_auth_failures_are_not_retried(status: int, code: str) -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(status)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    api = RiotApi("placeholder", client=client, min_interval=0, max_retries=3)
    try:
        with pytest.raises(RiotApiError) as raised:
            api.ladder("CHALLENGER")
    finally:
        client.close()

    assert raised.value.code == code
    assert raised.value.retryable is False
    assert calls == 1
