from __future__ import annotations

import base64
import os
import re
import subprocess
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Final, cast
from urllib.parse import urlencode

from lol_collector.models import JsonObject, JsonValue
from lol_collector.transport import (
    BinaryDownloadResult,
    BinaryHttpTransport,
    HttpResponse,
    HttpTransport,
)


_LCU_OPTION_PATTERN: Final[re.Pattern[str]] = re.compile(
    r'--(?P<name>app-port|remoting-auth-token|app-protocol|region|rso[_-]platform[_-]id)=(?P<value>"[^"]*"|\S+)',
    re.IGNORECASE,
)
_LCU_PROCESS_QUERY: Final[str] = (
    "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false); "
    "$client = Get-CimInstance Win32_Process -Filter \"Name = 'LeagueClientUx.exe'\" "
    "| Select-Object -First 1; "
    "if ($null -ne $client) { [Console]::Out.Write($client.CommandLine) }"
)


@dataclass(frozen=True, slots=True)
class LcuConnection:
    port: int
    password: str = field(repr=False)
    protocol: str = "https"
    region: str | None = None
    rso_platform_id: str | None = None
    rso_platform_flag: str | None = None

    @property
    def base_url(self) -> str:
        return f"{self.protocol}://127.0.0.1:{self.port}"

    @property
    def basic_auth(self) -> str:
        token = base64.b64encode(f"riot:{self.password}".encode()).decode()
        return f"Basic {token}"


@dataclass(frozen=True, slots=True)
class InvalidLcuLockfileError(ValueError):
    reason: str

    def __str__(self) -> str:
        return f"invalid League Client lockfile: {self.reason}"


@dataclass(frozen=True, slots=True)
class SgpSession:
    server_id: str
    entitlements_ready: bool
    league_session_ready: bool
    match_history_supported: bool = False
    common_supported: bool = False


@dataclass(frozen=True, slots=True)
class SgpEndpoints:
    match_history_base: str
    common_base: str
    region_path_param: str


@dataclass(frozen=True, slots=True)
class SgpCredentials:
    entitlements_token: str = field(repr=False)
    league_session_token: str = field(repr=False)


def parse_lockfile(text: str) -> LcuConnection:
    parts = text.strip().split(":")
    if len(parts) != 5:
        raise InvalidLcuLockfileError("unexpected field count")
    try:
        port = int(parts[2])
    except ValueError as error:
        raise InvalidLcuLockfileError("port is not an integer") from error
    return LcuConnection(port=port, password=parts[4], protocol=parts[3])


def discover_lcu() -> LcuConnection | None:
    command_line = _read_lcu_command_line()
    command_connection = (
        _parse_lcu_command_line(command_line) if command_line is not None else None
    )
    explicit = os.environ.get("LOL_LOCKFILE")
    candidates = [Path(explicit)] if explicit else []
    candidates.extend(
        Path(path)
        for path in (
            r"C:\Riot Games\League of Legends\lockfile",
            r"C:\Program Files\Riot Games\League of Legends\lockfile",
        )
    )
    if command_line is not None:
        process_lockfile = _process_lockfile_candidate(command_line)
        if process_lockfile is not None:
            candidates.insert(0, process_lockfile)

    for candidate in candidates:
        connection = _read_lcu_lockfile(candidate)
        if connection is not None:
            if command_connection is None:
                return connection
            return replace(
                connection,
                region=command_connection.region,
                rso_platform_id=command_connection.rso_platform_id,
                rso_platform_flag=command_connection.rso_platform_flag,
            )

    return command_connection


def _read_lcu_lockfile(lockfile: Path) -> LcuConnection | None:
    try:
        return parse_lockfile(lockfile.read_text(encoding="utf-8"))
    except (InvalidLcuLockfileError, OSError):
        return None


def _read_lcu_command_line() -> str | None:
    if os.name != "nt":
        return None
    try:
        result = subprocess.run(
            [
                "powershell.exe",
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                _LCU_PROCESS_QUERY,
            ],
            check=False,
            encoding="utf-8",
            errors="replace",
            stderr=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            text=True,
        )
    except OSError:
        return None
    command_line = result.stdout.strip()
    return command_line or None


def _process_lockfile_candidate(command_line: str) -> Path | None:
    executable = re.match(r'^\s*(?:"(?P<quoted>[^"]+)"|(?P<bare>\S+))', command_line)
    if executable is None:
        return None
    executable_path = executable.group("quoted") or executable.group("bare")
    if executable_path is None:
        return None
    return Path(executable_path).parent / "lockfile"


def _parse_lcu_command_line(command_line: str) -> LcuConnection | None:
    matches = list(_LCU_OPTION_PATTERN.finditer(command_line))
    options = {
        match.group("name").lower().replace("_", "-"): match.group("value").strip('"')
        for match in matches
    }
    port_text = options.get("app-port")
    password = options.get("remoting-auth-token")
    protocol = options.get("app-protocol", "https")
    if port_text is None or password is None or protocol not in {"http", "https"}:
        return None
    try:
        port = int(port_text)
    except ValueError:
        return None
    if not 1 <= port <= 65535:
        return None
    rso_match = next(
        (
            match
            for match in matches
            if match.group("name").lower().replace("_", "-") == "rso-platform-id"
        ),
        None,
    )
    return LcuConnection(
        port=port,
        password=password,
        protocol=protocol,
        region=options.get("region"),
        rso_platform_id=options.get("rso-platform-id"),
        rso_platform_flag=(
            f"--{rso_match.group('name')}" if rso_match is not None else None
        ),
    )


class LcuClientAdapter:
    def __init__(self, connection: LcuConnection, transport: HttpTransport) -> None:
        self.connection = connection
        self.transport = transport

    async def request(self, path: str) -> HttpResponse:
        return await self.transport.request("GET", f"{self.connection.base_url}{path}", {"Authorization": self.connection.basic_auth})

    async def get_json(self, path: str) -> tuple[HttpResponse, JsonObject | list[JsonValue] | None]:
        response = await self.request(path)
        payload = response.payload
        if isinstance(payload, dict):
            return response, payload
        if isinstance(payload, list):
            return response, payload
        return response, None


class SgpClientAdapter:
    def __init__(self, endpoints: SgpEndpoints, credentials: SgpCredentials, transport: HttpTransport) -> None:
        self.endpoints = endpoints
        self.credentials = credentials
        self.transport = transport

    async def match_history(self, puuid: str, start_index: int, count: int) -> HttpResponse:
        query = urlencode({"startIndex": start_index, "count": count})
        path = f"/match-history-query/v1/products/lol/player/{puuid}/SUMMARY?{query}"
        return await self._entitlements(path)

    async def summary(self, game_id: str) -> HttpResponse:
        return await self._entitlements(self._game_path(game_id, "SUMMARY"))

    async def details(self, game_id: str) -> HttpResponse:
        return await self._entitlements(self._game_path(game_id, "DETAILS"))

    async def ranked(self, puuid: str) -> HttpResponse:
        path = f"/leagues-ledge/v2/rankedStats/puuid/{puuid}"
        return await self._league_session(path)

    async def replay_metadata(self, game_id: str) -> HttpResponse:
        return await self._entitlements(self.replay_path(game_id))

    async def download_replay(
        self,
        game_id: str,
        target: Path,
    ) -> BinaryDownloadResult:
        transport = cast(BinaryHttpTransport, self.transport)
        url = f"{self.endpoints.match_history_base.rstrip('/')}{self.replay_path(game_id)}"
        return await transport.download_to_file(
            "GET",
            url,
            target,
            {"Authorization": f"Bearer {self.credentials.entitlements_token}"},
        )

    def replay_path(self, game_id: str) -> str:
        return f"/match-history-query/v3/product/lol/matchId/{self.endpoints.region_path_param}_{game_id}/infoType/replay"

    def _game_path(self, game_id: str, kind: str) -> str:
        return f"/match-history-query/v1/products/lol/{self.endpoints.region_path_param}_{game_id}/{kind}"

    async def _entitlements(self, path: str) -> HttpResponse:
        return await self._request(self.endpoints.match_history_base, path, self.credentials.entitlements_token)

    async def _league_session(self, path: str) -> HttpResponse:
        return await self._request(self.endpoints.common_base, path, self.credentials.league_session_token)

    async def _request(self, base_url: str, path: str, token: str) -> HttpResponse:
        return await self.transport.request("GET", f"{base_url.rstrip('/')}{path}", {"Authorization": f"Bearer {token}"})


def parse_region(payload: JsonObject) -> tuple[str | None, str | None, str | None]:
    region = _string(payload, "region") or _string(payload, "locale")
    platform = _string(payload, "rsoPlatformId") or _string(payload, "platformId")
    puuid = _string(payload, "puuid")
    return region, platform, puuid


def _string(mapping: JsonObject, key: str) -> str | None:
    value = mapping.get(key)
    return value if isinstance(value, str) else None
