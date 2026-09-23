from __future__ import annotations

import random
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RetryDecision:
    retryable: bool
    delay_seconds: float
    error_class: str
    refresh_session: bool = False
    pause_global: bool = False


def classify_error(status_code: int | None, error_name: str, attempt: int, retry_after: float | None = None, rng: random.Random | None = None) -> RetryDecision:
    source = rng or random.Random(0)
    if error_name == "client_disconnected":
        return RetryDecision(True, 0.0, "WAITING_FOR_CLIENT")
    if error_name == "storage_error":
        return RetryDecision(False, 0.0, "STORAGE_ERROR", pause_global=True)
    if status_code in {401, 403}:
        return RetryDecision(True, 0.0, "AUTH_EXPIRED", refresh_session=True)
    if status_code == 429:
        delay = retry_after if retry_after is not None else min(300.0, 2**attempt + source.random())
        return RetryDecision(True, delay, "RATE_LIMITED")
    if status_code == 404:
        return RetryDecision(attempt < 8, min(600.0, (2**attempt) * 30), "EVENTUAL_CONSISTENCY")
    if error_name in {"timeout", "dns", "connection_reset"} or status_code is None or status_code >= 500:
        return RetryDecision(attempt < 8, min(300.0, 2**attempt + source.random()), "TRANSIENT_NETWORK")
    return RetryDecision(False, 0.0, "PERMANENT")
