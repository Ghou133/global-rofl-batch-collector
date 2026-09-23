from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from lol_collector.adapters import LcuClientAdapter, parse_region
from lol_collector.artifacts import RawArtifactStore
from lol_collector.models import ArtifactType, CapabilityStatus, JsonObject
from lol_collector.probe_helpers import current_puuid
from lol_collector.transport import HttpResponse, HttpTransportError


@dataclass(frozen=True, slots=True)
class IdentityProbeResult:
    client: JsonObject
    region: str | None
    platform: str | None
    puuid: str | None


class LcuIdentityProbe:
    def __init__(self, output_dir: Path) -> None:
        self.artifacts = RawArtifactStore(output_dir / "probe-artifacts")
        self.named_artifacts = RawArtifactStore(output_dir)

    async def run(self, client: LcuClientAdapter) -> IdentityProbeResult:
        region_response, region_payload = await self._region(client)
        api_region, api_platform, _ = parse_region(region_payload)
        region = client.connection.region or api_region
        platform = client.connection.rso_platform_id or api_platform
        region_source = _source(client.connection.region, api_region)
        platform_source = _source(client.connection.rso_platform_id, api_platform)
        self._command_line_diagnostic(client)

        summoner_response, summoner_payload = await self._current_summoner(client)
        puuid, puuid_path = current_puuid(summoner_payload)
        identity_endpoint = "/lol-summoner/v1/current-summoner"
        if puuid is None:
            login_payload = await self._login_session(client)
            fallback_puuid, fallback_path = current_puuid(login_payload)
            if fallback_puuid is not None:
                puuid = fallback_puuid
                puuid_path = fallback_path
                identity_endpoint = "/lol-login/v1/session"

        connected = any(
            response is not None and response.status_code < 400
            for response in (region_response, summoner_response)
        )
        identity_status = (
            CapabilityStatus.SUPPORTED.value
            if puuid
            else CapabilityStatus.FAILED.value
            if summoner_response is None or summoner_response.status_code >= 400
            else CapabilityStatus.UNKNOWN.value
        )
        data: JsonObject = {
            "LCU_CONNECTED": connected,
            "LCU_CONNECTION_STATUS": (
                CapabilityStatus.SUPPORTED.value
                if connected
                else CapabilityStatus.FAILED.value
            ),
            "REGION": region,
            "REGION_SOURCE": region_source,
            "RSO_PLATFORM_ID": platform,
            "RSO_PLATFORM_ID_SOURCE": platform_source,
            "RSO_PLATFORM_ID_STATUS": (
                CapabilityStatus.SUPPORTED.value
                if platform
                else CapabilityStatus.UNKNOWN.value
            ),
            "CURRENT_PUUID_AVAILABLE": puuid is not None,
            "CURRENT_PUUID_STATUS": identity_status,
            "CURRENT_PUUID": puuid,
            "CURRENT_PUUID_PATH": puuid_path,
            "CURRENT_PUUID_ENDPOINT": identity_endpoint,
        }
        return IdentityProbeResult(data, region, platform, puuid)

    async def _region(
        self, client: LcuClientAdapter
    ) -> tuple[HttpResponse | None, JsonObject]:
        try:
            response, payload = await client.get_json("/riotclient/region-locale")
        except HttpTransportError:
            return None, {}
        if not isinstance(payload, dict):
            return response, {}
        self.artifacts.write_json(
            artifact_type=ArtifactType.CAPABILITY,
            owner_type="client",
            owner_id="region",
            endpoint_template="/riotclient/region-locale",
            http_status=response.status_code,
            payload=payload,
        )
        return response, payload

    async def _current_summoner(
        self, client: LcuClientAdapter
    ) -> tuple[HttpResponse | None, JsonObject | None]:
        try:
            response, payload = await client.get_json(
                "/lol-summoner/v1/current-summoner"
            )
        except HttpTransportError:
            return None, None
        if not isinstance(payload, dict):
            return response, None
        self.artifacts.write_json(
            artifact_type=ArtifactType.CAPABILITY,
            owner_type="client",
            owner_id="current-summoner",
            endpoint_template="/lol-summoner/v1/current-summoner",
            http_status=response.status_code,
            payload=payload,
        )
        self.named_artifacts.write_named_json("current-summoner.json", payload)
        return response, payload

    async def _login_session(self, client: LcuClientAdapter) -> JsonObject | None:
        try:
            response, payload = await client.get_json("/lol-login/v1/session")
        except HttpTransportError:
            return None
        if not isinstance(payload, dict):
            return None
        self.artifacts.write_json(
            artifact_type=ArtifactType.CAPABILITY,
            owner_type="client",
            owner_id="login-session-identity",
            endpoint_template="/lol-login/v1/session",
            http_status=response.status_code,
            payload=payload,
        )
        self.named_artifacts.write_named_json("login-session-identity.json", payload)
        return payload

    def _command_line_diagnostic(self, client: LcuClientAdapter) -> None:
        connection = client.connection
        self.named_artifacts.write_named_json(
            "lcu-command-line-diagnostic.json",
            {
                "process": "LeagueClientUx.exe",
                "command_line_metadata_status": (
                    CapabilityStatus.SUPPORTED.value
                    if connection.rso_platform_id
                    else CapabilityStatus.UNKNOWN.value
                ),
                "region_flag_available": connection.region is not None,
                "rso_platform_flag_available": connection.rso_platform_id is not None,
                "rso_platform_flag": connection.rso_platform_flag,
                "region": connection.region,
                "rsoPlatformId": connection.rso_platform_id,
                "raw_command_line_persisted": False,
                "auth_values_persisted": False,
            },
        )


def _source(command_line_value: str | None, api_value: str | None) -> str | None:
    if command_line_value:
        return "LEAGUECLIENTUX_COMMAND_LINE"
    return "LCU_API_RESPONSE" if api_value else None
