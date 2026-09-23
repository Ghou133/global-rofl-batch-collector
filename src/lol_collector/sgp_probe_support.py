from __future__ import annotations

from collections.abc import Awaitable, Callable

from lol_collector.adapters import LcuClientAdapter
from lol_collector.artifacts import RawArtifactStore
from lol_collector.models import CapabilityStatus
from lol_collector.probe_helpers import error_evidence
from lol_collector.transport import HttpResponse, HttpTransportError


async def lcu_request(
    client: LcuClientAdapter, path: str
) -> HttpResponse | None:
    try:
        return await client.request(path)
    except HttpTransportError:
        return None


async def sgp_request(
    operation: Callable[..., Awaitable[HttpResponse]], *args: object
) -> HttpResponse | None:
    try:
        return await operation(*args)
    except HttpTransportError:
        return None


def record_response(
    store: RawArtifactStore,
    success_name: str,
    error_name: str,
    response: HttpResponse | None,
) -> bool:
    if response is None:
        return False
    if response.status_code >= 400:
        store.write_named_json(error_name, error_evidence("SGP endpoint", response))
        return False
    if not isinstance(response.payload, dict):
        return False
    store.write_named_json(success_name, response.payload)
    return True


def token_status(response: HttpResponse | None, parsed: bool) -> str:
    if response is None or response.status_code >= 400:
        return CapabilityStatus.FAILED.value
    return CapabilityStatus.SUPPORTED.value if parsed else CapabilityStatus.UNKNOWN.value


def response_status(response: HttpResponse | None) -> str:
    if response is None or response.status_code >= 400:
        return CapabilityStatus.FAILED.value
    return CapabilityStatus.UNKNOWN.value


def sgp_server_id(region: str | None, platform: str | None) -> str | None:
    normalized_region = (region or "").upper()
    normalized_platform = (platform or "").upper()
    if not normalized_region:
        return None
    if normalized_region == "TENCENT":
        return f"TENCENT_{normalized_platform}" if normalized_platform else None
    return normalized_region
