from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class CollectorError(Exception):
    code: str
    message: str
    retryable: bool = False
    http_status: int | None = None
    retry_after: float | None = None

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"


class ConfigurationError(CollectorError):
    pass


class RiotApiError(CollectorError):
    pass


class LcuError(CollectorError):
    pass


class ReplayError(CollectorError):
    pass


class IntegrityError(ReplayError):
    pass
