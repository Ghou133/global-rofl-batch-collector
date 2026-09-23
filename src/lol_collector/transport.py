from __future__ import annotations

import hashlib
import os
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import httpx2

from lol_collector.models import JsonValue


_LIMITS = httpx2.Limits(max_connections=200, max_keepalive_connections=40, keepalive_expiry=30.0)
_TIMEOUT = httpx2.Timeout(connect=5.0, read=30.0, write=10.0, pool=10.0)
_SOCKET_OPTIONS = ((socket.IPPROTO_TCP, socket.TCP_NODELAY, 1),)
_MAX_BINARY_DOWNLOAD_BYTES = 512 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class HttpResponse:
    status_code: int
    headers: dict[str, str]
    payload: JsonValue | None


class HttpTransport(Protocol):
    async def request(self, method: str, url: str, headers: dict[str, str] | None = None) -> HttpResponse: ...


@dataclass(frozen=True, slots=True)
class BinaryDownloadResult:
    status_code: int
    headers: dict[str, str]
    file_size: int
    sha256: str | None
    path: Path | None


class BinaryHttpTransport(HttpTransport, Protocol):
    async def download_to_file(
        self,
        method: str,
        url: str,
        target: Path,
        headers: dict[str, str] | None = None,
    ) -> BinaryDownloadResult: ...


class HttpTransportError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class Httpx2Transport:
    def __init__(self) -> None:
        self._client = _create_async_client(verify=True, http2=True)
        self._lcu_client = _create_async_client(verify=False, http2=False)

    async def request(self, method: str, url: str, headers: dict[str, str] | None = None) -> HttpResponse:
        try:
            client = self._lcu_client if url.startswith("https://127.0.0.1:") else self._client
            response = await client.request(method, url, headers=headers or {})
            payload: JsonValue | None = None
            content_type = response.headers.get("content-type", "").lower()
            if response.content and "json" in content_type:
                parsed = response.json()
                if isinstance(parsed, (dict, list, str, int, float, bool)) or parsed is None:
                    payload = parsed
            return HttpResponse(response.status_code, {key.lower(): value for key, value in response.headers.items()}, payload)
        except (httpx2.HTTPError, OSError, ValueError) as error:
            raise HttpTransportError(type(error).__name__) from error

    async def download_to_file(
        self,
        method: str,
        url: str,
        target: Path,
        headers: dict[str, str] | None = None,
    ) -> BinaryDownloadResult:
        client = self._lcu_client if url.startswith("https://127.0.0.1:") else self._client
        partial = target.with_suffix(f"{target.suffix}.partial")
        try:
            async with client.stream(
                method,
                url,
                headers=headers or {},
                follow_redirects=False,
            ) as response:
                response_headers = {
                    key.lower(): value for key, value in response.headers.items()
                }
                if not 200 <= response.status_code < 300:
                    partial.unlink(missing_ok=True)
                    return BinaryDownloadResult(
                        response.status_code,
                        response_headers,
                        0,
                        None,
                        None,
                    )
                target.parent.mkdir(parents=True, exist_ok=True)
                partial.unlink(missing_ok=True)
                digest = hashlib.sha256()
                size = 0
                with partial.open("xb") as handle:
                    async for chunk in response.aiter_bytes():
                        if not chunk:
                            continue
                        handle.write(chunk)
                        digest.update(chunk)
                        size += len(chunk)
                        if size > _MAX_BINARY_DOWNLOAD_BYTES:
                            raise ValueError("binary download exceeds safe size limit")
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(partial, target)
                return BinaryDownloadResult(
                    response.status_code,
                    response_headers,
                    size,
                    digest.hexdigest(),
                    target,
                )
        except BaseException as error:
            partial.unlink(missing_ok=True)
            if isinstance(error, (httpx2.HTTPError, OSError, ValueError)):
                raise HttpTransportError(type(error).__name__) from error
            raise

    async def aclose(self) -> None:
        await self._client.aclose()
        await self._lcu_client.aclose()


def _create_async_client(*, verify: bool, http2: bool) -> httpx2.AsyncClient:
    transport = httpx2.AsyncHTTPTransport(
        verify=verify,
        http2=http2,
        retries=3,
        limits=_LIMITS,
        socket_options=_SOCKET_OPTIONS,
    )
    return httpx2.AsyncClient(
        transport=transport,
        timeout=_TIMEOUT,
        follow_redirects=True,
    )
