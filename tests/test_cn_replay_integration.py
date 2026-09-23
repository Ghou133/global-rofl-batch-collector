from __future__ import annotations

import hashlib
from pathlib import Path

import anyio
from typer.testing import CliRunner

from lol_collector.adapters import (
    LcuClientAdapter,
    LcuConnection,
    SgpClientAdapter,
    SgpCredentials,
    SgpEndpoints,
)
from lol_collector.cli import app
from lol_collector.collection_runtime import RuntimeSession
from lol_collector.replay_archive import archive_replay
from lol_collector.replay_capture import ReplayCaptureCoordinator
from lol_collector.repository import Repository
from lol_collector.transport import BinaryDownloadResult, HttpResponse


def _rofl() -> bytes:
    version = b"16.15.801.3452"
    return b"RIOT\x02\x00opaque00" + bytes([len(version)]) + version + b"\x01\x00\x00\x00body"


class _PairTransport:
    def __init__(self, *, replay_status: int = 200) -> None:
        self.replay_status = replay_status

    async def request(
        self, _method: str, url: str, _headers: dict[str, str] | None = None
    ) -> HttpResponse:
        if url.endswith("/SUMMARY"):
            return HttpResponse(
                200,
                {},
                {"json": {"gameId": "300", "gameVersion": "16.15.801.3452", "queueId": 420}},
            )
        if url.endswith("/DETAILS"):
            return HttpResponse(200, {}, {"json": {"gameId": "300", "frames": []}})
        raise AssertionError(f"Unexpected URL: {url}")

    async def download_to_file(
        self,
        _method: str,
        _url: str,
        target: Path,
        _headers: dict[str, str] | None = None,
    ) -> BinaryDownloadResult:
        if self.replay_status != 200:
            return BinaryDownloadResult(self.replay_status, {}, 0, None, None)
        content = _rofl()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        return BinaryDownloadResult(
            200, {}, len(content), hashlib.sha256(content).hexdigest(), target
        )


def _session(transport: _PairTransport) -> RuntimeSession:
    connection = LcuConnection(1234, "test-secret", region="TENCENT", rso_platform_id="HN1")
    return RuntimeSession(
        connection=connection,
        lcu=LcuClientAdapter(connection, transport),
        sgp=SgpClientAdapter(
            SgpEndpoints("https://history.example", "https://common.example", "HN1"),
            SgpCredentials("entitlement-secret", "league-secret"),
            transport,
        ),
        region="TENCENT",
        platform="HN1",
        server_id="TENCENT_HN1",
        patch="16.15",
        full_version="16.15.801.3452",
    )


def test_cn_replay_status_uses_isolated_default_database(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(app, ["replay", "--status"])
    assert result.exit_code == 0, result.output
    assert (tmp_path / "data" / "CN" / "replay-paired" / "collector.sqlite3").is_file()
    assert not (tmp_path / "data" / "collector.sqlite3").exists()


def test_cn_direct_capture_commits_verified_pair_without_credentials(tmp_path: Path) -> None:
    data_dir = tmp_path / "data" / "CN" / "replay-paired"
    repository = Repository(data_dir / "collector.sqlite3")
    try:
        capture = ReplayCaptureCoordinator(repository, data_dir, ())
        result = anyio.run(capture.capture_game_id, _session(_PairTransport()), "300")
        assert result.download_status == "VALIDATED"
        assert result.validation_status == "VALIDATED"
        assert capture.check_pair("300").valid_pair
        assert Path(result.rofl_path).read_bytes() == _rofl()
        assert Path(result.details_path).is_file()
        stored = repository.replays.get_record("300")
        assert stored["acquisition_method"] == "TENCENT_SGP_REPLAY_BINARY"
        assert not (data_dir / ".replay-downloads" / "300.download").exists()
    finally:
        repository.close()
    assert b"test-secret" not in (data_dir / "collector.sqlite3").read_bytes()
    assert b"entitlement-secret" not in (data_dir / "collector.sqlite3").read_bytes()


def test_cn_replay_404_remains_unavailable_without_partial_pair(tmp_path: Path) -> None:
    data_dir = tmp_path / "data" / "CN" / "replay-paired"
    repository = Repository(data_dir / "collector.sqlite3")
    try:
        capture = ReplayCaptureCoordinator(repository, data_dir, ())
        result = anyio.run(
            capture.capture_game_id, _session(_PairTransport(replay_status=404)), "300"
        )
        assert result.download_status == "UNAVAILABLE"
        assert result.details_exists is False
        assert repository.replays.pending_remote_games(10, cooldown_seconds=0) == ()
        assert not list(data_dir.rglob("*.rofl"))
    finally:
        repository.close()


def test_cn_archive_preserves_existing_invalid_file(tmp_path: Path) -> None:
    source = tmp_path / "incoming.rofl"
    source.write_bytes(_rofl())
    target = tmp_path / "archive" / "16.15" / "300.rofl"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"original damaged replay evidence")

    result = archive_replay(source, target)

    assert not result.success
    assert result.error_code == "ARCHIVE_CONFLICT"
    assert target.read_bytes() == b"original damaged replay evidence"
