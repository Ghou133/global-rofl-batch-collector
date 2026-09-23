from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from lol_collector.adapters import (
    LcuClientAdapter,
    LcuConnection,
    SgpClientAdapter,
    SgpCredentials,
    SgpEndpoints,
    discover_lcu,
)
from lol_collector.collection_support import extract_current_patch
from lol_collector.models import JsonValue
from lol_collector.transport import HttpResponse, HttpTransport, HttpTransportError


@dataclass(frozen=True, slots=True)
class RuntimeSession:
    connection: LcuConnection
    lcu: LcuClientAdapter
    sgp: SgpClientAdapter
    region: str
    platform: str
    server_id: str
    patch: str
    full_version: str | None


def endpoints_for(region: str, platform: str) -> SgpEndpoints | None:
    if region.upper() != "TENCENT" or not platform:
        return None
    platform_id = platform.upper()
    if platform_id != "HN1":
        return None
    host = "https://hn1-k8s-sgp.lol.qq.com:21019"
    return SgpEndpoints(host, host, platform_id)


async def acquire_runtime(
    transport: HttpTransport,
    target_patch: str | None = None,
) -> RuntimeSession | None:
    connection = discover_lcu()
    if connection is None:
        return None
    lcu = LcuClientAdapter(connection, transport)
    try:
        region_response, region_payload = await lcu.get_json("/riotclient/region-locale")
        if region_response.status_code >= 400:
            return None
        region = connection.region or _region(region_payload)
        platform = connection.rso_platform_id or _platform(region_payload)
        if region is None or platform is None:
            return None
        endpoints = endpoints_for(region, platform)
        if endpoints is None:
            return None
        patch_response = await lcu.request("/lol-patch/v1/game-version")
        full_version = _version(patch_response.payload)
        patch = extract_current_patch(patch_response.payload) or target_patch
        if patch is None:
            return None
        credentials = await _credentials(lcu)
        if credentials is None:
            return None
    except HttpTransportError:
        return None
    return RuntimeSession(
        connection,
        lcu,
        SgpClientAdapter(endpoints, credentials, transport),
        region,
        platform.upper(),
        f"{region.upper()}_{platform.upper()}",
        patch,
        full_version,
    )


async def lcu_request(client: LcuClientAdapter, path: str) -> HttpResponse | None:
    try:
        return await client.request(path)
    except HttpTransportError:
        return None


async def _credentials(client: LcuClientAdapter) -> SgpCredentials | None:
    entitlements = await client.request("/entitlements/v1/token")
    session = await client.request("/lol-league-session/v1/league-session-token")
    access = entitlements.payload.get("accessToken") if isinstance(entitlements.payload, Mapping) else None
    session_token = session.payload if isinstance(session.payload, str) else None
    if not isinstance(access, str) or not access or not isinstance(session_token, str) or not session_token:
        return None
    return SgpCredentials(access, session_token)


def _region(payload: JsonValue | None) -> str | None:
    if not isinstance(payload, Mapping):
        return None
    value = payload.get("region") or payload.get("locale")
    return value if isinstance(value, str) else None


def _platform(payload: JsonValue | None) -> str | None:
    if not isinstance(payload, Mapping):
        return None
    value = payload.get("rsoPlatformId") or payload.get("platformId")
    return value if isinstance(value, str) else None


def _version(payload: JsonValue | None) -> str | None:
    if isinstance(payload, str):
        return payload
    if isinstance(payload, Mapping):
        for key in ("version", "gameVersion", "patch"):
            value = payload.get(key)
            if isinstance(value, str):
                return value
    return None
