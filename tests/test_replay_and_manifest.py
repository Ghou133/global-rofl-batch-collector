from __future__ import annotations

import gzip
import hashlib
import json
import random
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from global_rofl_collector.config import Config
from global_rofl_collector.db import Database
from global_rofl_collector.errors import IntegrityError, ReplayError
from global_rofl_collector.manifest import manifest_path, write_manifest
from global_rofl_collector.models import JobState, MatchRecord, Quality
from global_rofl_collector.replay import (
    DownloadManager,
    ReplayBackendAcquirer,
    replay_paths,
    verify_rofl,
)


def _rofl_bytes(
    payload: bytes = b"chunk payload",
    *,
    version: str = "26.18.704.1234",
    stream_tag: int = 1,
) -> bytes:
    version_bytes = version.encode()
    header = bytearray(0x0F)
    header[:4] = b"RIOT"
    header[0x0E] = len(version_bytes)
    header.extend(version_bytes)

    chunk_header = bytearray(0x11)
    chunk_header[5:9] = (stream_tag << 24).to_bytes(4, "little")
    chunk_header[9:13] = len(payload).to_bytes(4, "little")
    metadata = json.dumps({"gameVersion": version}, separators=(",", ":")).encode()
    return (
        bytes(header)
        + bytes(chunk_header)
        + payload
        + bytes(0x100)
        + metadata
        + len(metadata).to_bytes(4, "little")
    )


class FakeAcquirer:
    provider = "test-replay-provider"

    def __init__(self, payload: bytes | None = None, *, reported_size_delta: int = 0):
        self.payload = payload or _rofl_bytes()
        self.reported_size_delta = reported_size_delta
        self.download_calls: list[tuple[str, Path]] = []

    def download_to(self, match_id: str, partial_path: Path) -> int:
        self.download_calls.append((match_id, partial_path))
        partial_path.write_bytes(self.payload)
        return len(self.payload) + self.reported_size_delta


def _seed_match(
    db: Database,
    match_factory: Callable[..., MatchRecord],
    *,
    match_id: str = "KR_1001",
    state: JobState = JobState.ELIGIBLE,
) -> tuple[int, int, Any]:
    dataset_id = db.dataset("KR", "26.18", "26.18.704.1234")
    run_id = db.start_run(dataset_id, "test")
    record = match_factory(
        match_id,
        quality=Quality(5, 3, 2, 10, "CHALLENGER"),
        participants=[f"p{index}" for index in range(10)],
    )
    db.store_match(dataset_id, record, eligible=True)
    if state is JobState.DOWNLOADING:
        db.transition_job(record.match_id, JobState.ELIGIBLE, JobState.QUEUED)
        db.transition_job(record.match_id, JobState.QUEUED, JobState.DOWNLOADING)
    elif state is not JobState.ELIGIBLE:
        db.transition_job(record.match_id, JobState.ELIGIBLE, state)
    row = db.connection.execute(
        "SELECT m.*,j.state,j.attempts,j.provider FROM matches m "
        "JOIN replay_jobs j ON j.match_id=m.match_id WHERE m.match_id=?",
        (record.match_id,),
    ).fetchone()
    return dataset_id, run_id, row


def test_verify_rofl_validates_riot_container_layout_and_hash(tmp_path: Path) -> None:
    payload = b"ReplayV2 binary payload" * 50
    content = _rofl_bytes(payload)
    path = tmp_path / "valid.rofl"
    path.write_bytes(content)

    result = verify_rofl(path, expected_size=len(content))

    assert result.file_size == len(content)
    assert result.sha256 == hashlib.sha256(content).hexdigest()
    assert result.container == "riot-replay-v2"
    assert result.uncompressed_size == len(content)
    assert bytes.fromhex(result.inner_prefix_hex) == content[:32]
    assert result.game_version == "26.18.704.1234"
    assert result.chunk_count == 1
    assert result.game_chunks == 1


@pytest.mark.parametrize(
    "body",
    [b"<html>error</html>", b"  <!DOCTYPE html>", b'{"error":1}', b" [1]"],
)
def test_verify_rofl_rejects_html_and_json_error_bodies(tmp_path: Path, body: bytes) -> None:
    path = tmp_path / "error.rofl"
    path.write_bytes(body)

    with pytest.raises(IntegrityError) as raised:
        verify_rofl(path)

    assert raised.value.code == "ROFL_ERROR_BODY"


def test_verify_rofl_rejects_content_length_mismatch_before_publish(tmp_path: Path) -> None:
    content = _rofl_bytes()
    path = tmp_path / "short.rofl.partial"
    path.write_bytes(content)

    with pytest.raises(IntegrityError) as raised:
        verify_rofl(path, expected_size=len(content) + 1)

    assert raised.value.code == "ROFL_TRUNCATED"
    assert raised.value.retryable is True


def test_verify_rofl_rejects_a_chunk_whose_declared_body_is_truncated(tmp_path: Path) -> None:
    path = tmp_path / "truncated.rofl.partial"
    content = bytearray(_rofl_bytes(b"short body"))
    chunk_start = 0x0F + content[0x0E]
    content[chunk_start + 9 : chunk_start + 13] = (10_000).to_bytes(4, "little")
    path.write_bytes(content)

    with pytest.raises(IntegrityError) as raised:
        verify_rofl(path)

    assert raised.value.code == "ROFL_CHUNK_TRUNCATED"


def test_verify_rofl_rejects_an_unremoved_http_gzip_wrapper(tmp_path: Path) -> None:
    path = tmp_path / "wrapped.rofl"
    path.write_bytes(gzip.compress(_rofl_bytes(), mtime=0))

    with pytest.raises(IntegrityError) as raised:
        verify_rofl(path)

    assert raised.value.code == "ROFL_HTTP_GZIP_WRAPPER"


def test_replay_backend_decodes_http_gzip_and_retries_429(tmp_path: Path) -> None:
    content = _rofl_bytes(b"decoded RIOT container bytes")
    wire_content = gzip.compress(content, mtime=0)
    calls = 0

    class StubLcu:
        def discover_edge_base(self) -> str:
            return "https://edge.example.test"

        def edge_headers(self) -> dict[str, str]:
            return {"Authorization": "Bearer opaque"}

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, headers={"Retry-After": "19"})
        return httpx.Response(
            200,
            content=wire_content,
            headers={
                "Content-Encoding": "gzip",
                "Content-Length": str(len(wire_content)),
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    sleeps: list[float] = []
    config = Config(
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        db_path=tmp_path / "collector.sqlite3",
        logs_dir=tmp_path / "logs",
        riot_api_key=None,
        league_install_dir=tmp_path / "League",
        replay_edge_base_url=None,
        request_timeout=1,
        api_min_interval=0,
        api_max_retries=0,
        history_count=20,
    )
    acquirer = ReplayBackendAcquirer(
        config,
        lcu=StubLcu(),  # type: ignore[arg-type]
        edge_client=client,
        download_retries=2,
        sleep=sleeps.append,
        rng=random.Random(0),
    )
    target = tmp_path / "KR_1.rofl.partial"
    try:
        written = acquirer.download_to("KR_1", target)
    finally:
        acquirer.close()
        client.close()

    assert calls == 2
    assert sleeps == [19.0]
    assert written == len(content)
    assert target.read_bytes() == content
    verify_rofl(target, expected_size=len(content))


def test_replay_backend_retries_http_gzip_decoding_error(
    tmp_path: Path,
    config: Config,
) -> None:
    content = _rofl_bytes(b"valid body after a broken transfer")
    calls = 0

    class StubLcu:
        def edge_headers(self) -> dict[str, str]:
            return {}

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        wire = b"not-a-gzip-stream" if calls == 1 else gzip.compress(content, mtime=0)
        return httpx.Response(200, content=wire, headers={"Content-Encoding": "gzip"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    sleeps: list[float] = []
    acquirer = ReplayBackendAcquirer(
        config,
        lcu=StubLcu(),  # type: ignore[arg-type]
        edge_client=client,
        download_retries=2,
        sleep=sleeps.append,
        rng=random.Random(0),
    )
    target = tmp_path / "decoded.rofl.partial"
    try:
        written = acquirer.download_to("KR_1", target)
    finally:
        acquirer.close()
        client.close()

    assert calls == 2
    assert len(sleeps) == 1
    assert written == len(content)
    assert target.read_bytes() == content


def test_replay_backend_does_not_retry_permanent_unavailable(tmp_path: Path) -> None:
    calls = 0

    class StubLcu:
        def discover_edge_base(self) -> str:
            return "https://edge.example.test"

        def edge_headers(self) -> dict[str, str]:
            return {}

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(404)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    config = Config(
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        db_path=tmp_path / "collector.sqlite3",
        logs_dir=tmp_path / "logs",
        riot_api_key=None,
        league_install_dir=tmp_path / "League",
        replay_edge_base_url=None,
        request_timeout=1,
        api_min_interval=0,
        api_max_retries=0,
        history_count=20,
    )
    acquirer = ReplayBackendAcquirer(
        config,
        lcu=StubLcu(),  # type: ignore[arg-type]
        edge_client=client,
        download_retries=4,
        sleep=lambda _seconds: None,
    )
    try:
        with pytest.raises(ReplayError) as raised:
            acquirer.download_to("KR_missing", tmp_path / "missing.rofl.partial")
    finally:
        acquirer.close()
        client.close()

    assert raised.value.code == "REPLAY_UNAVAILABLE"
    assert raised.value.retryable is False
    assert calls == 1


def test_download_is_published_by_rename_only_after_verification(
    db: Database,
    config: Config,
    match_factory: Callable[..., MatchRecord],
) -> None:
    _, run_id, row = _seed_match(db, match_factory)
    acquirer = FakeAcquirer()
    manager = DownloadManager(db, config, acquirer)  # type: ignore[arg-type]

    verification = manager.download(row, run_id)
    final, partial = replay_paths(config.data_dir, row)

    assert acquirer.download_calls == [("KR_1001", partial)]
    assert final.read_bytes() == acquirer.payload
    assert not partial.exists()
    assert verification.sha256 == hashlib.sha256(acquirer.payload).hexdigest()
    job = db.connection.execute(
        "SELECT state,attempts FROM replay_jobs WHERE match_id='KR_1001'"
    ).fetchone()
    assert tuple(job) == (JobState.VERIFIED, 1)
    download = db.download_row("KR_1001")
    assert download["file_path"] == final.relative_to(config.data_dir).as_posix()


def test_failed_integrity_keeps_partial_and_becomes_retryable(
    db: Database,
    config: Config,
    match_factory: Callable[..., MatchRecord],
) -> None:
    _, run_id, row = _seed_match(db, match_factory)
    acquirer = FakeAcquirer(reported_size_delta=1)
    manager = DownloadManager(db, config, acquirer)  # type: ignore[arg-type]

    with pytest.raises(IntegrityError, match="ROFL_TRUNCATED"):
        manager.download(row, run_id)

    final, partial = replay_paths(config.data_dir, row)
    assert not final.exists()
    assert partial.is_file()
    job = db.connection.execute(
        "SELECT state,resume_state,attempts FROM replay_jobs WHERE match_id='KR_1001'"
    ).fetchone()
    assert tuple(job) == (JobState.FAILED_RETRYABLE, JobState.QUEUED, 1)


def test_download_rejects_replay_from_another_build_and_preserves_partial(
    db: Database,
    config: Config,
    match_factory: Callable[..., MatchRecord],
) -> None:
    _, run_id, row = _seed_match(db, match_factory)
    acquirer = FakeAcquirer(_rofl_bytes(version="26.18.999.1"))
    manager = DownloadManager(db, config, acquirer)  # type: ignore[arg-type]

    with pytest.raises(IntegrityError, match="ROFL_BUILD_MISMATCH"):
        manager.download(row, run_id)

    final, partial = replay_paths(config.data_dir, row)
    assert not final.exists()
    assert partial.read_bytes() == acquirer.payload
    assert db.download_row("KR_1001") is None
    assert db.connection.execute(
        "SELECT state FROM replay_jobs WHERE match_id='KR_1001'"
    ).fetchone()["state"] == JobState.FAILED_PERMANENT


def test_reconcile_adopts_an_existing_final_without_redownload(
    db: Database,
    config: Config,
    match_factory: Callable[..., MatchRecord],
) -> None:
    dataset_id, _, row = _seed_match(db, match_factory)
    final, _ = replay_paths(config.data_dir, row)
    final.parent.mkdir(parents=True)
    final.write_bytes(_rofl_bytes())
    acquirer = FakeAcquirer()

    adopted = DownloadManager(db, config, acquirer).reconcile(dataset_id)  # type: ignore[arg-type]

    assert adopted == 1
    assert acquirer.download_calls == []
    assert db.verified_count(dataset_id) == 1
    verification = json.loads(db.download_row("KR_1001")["verification_json"])
    assert verification["adopted_existing"] is True


def test_reconcile_preserves_corrupt_final_and_continues_adopting_other_assets(
    db: Database,
    config: Config,
    match_factory: Callable[..., MatchRecord],
) -> None:
    dataset_id, run_id, corrupt_row = _seed_match(db, match_factory, match_id="KR_1001")
    _, _, valid_row = _seed_match(db, match_factory, match_id="KR_1002")
    corrupt_final, _ = replay_paths(config.data_dir, corrupt_row)
    valid_final, _ = replay_paths(config.data_dir, valid_row)
    corrupt_final.parent.mkdir(parents=True, exist_ok=True)
    corrupt_final.write_bytes(b"preserve this corrupt research asset")
    valid_final.write_bytes(_rofl_bytes())

    adopted = DownloadManager(db, config, FakeAcquirer()).reconcile(dataset_id, run_id)  # type: ignore[arg-type]

    assert adopted == 1
    assert corrupt_final.read_bytes() == b"preserve this corrupt research asset"
    states = {
        row["match_id"]: row["state"]
        for row in db.connection.execute(
            "SELECT match_id,state FROM replay_jobs WHERE match_id IN ('KR_1001','KR_1002')"
        )
    }
    assert states == {
        "KR_1001": JobState.FAILED_PERMANENT,
        "KR_1002": JobState.VERIFIED,
    }
    error = db.connection.execute(
        "SELECT * FROM errors WHERE match_id='KR_1001' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert error["run_id"] == run_id
    assert error["stage"] == "replay_reconcile"
    assert error["code"] == "ROFL_MALFORMED"
    assert error["retryable"] == 0
    assert json.loads(error["details_json"])["preserved_path"] == str(corrupt_final)


def test_reconcile_promotes_a_complete_partial_after_interrupted_download(
    db: Database,
    config: Config,
    match_factory: Callable[..., MatchRecord],
) -> None:
    dataset_id, _, row = _seed_match(db, match_factory, state=JobState.DOWNLOADING)
    final, partial = replay_paths(config.data_dir, row)
    partial.parent.mkdir(parents=True)
    partial.write_bytes(_rofl_bytes())

    adopted = DownloadManager(db, config, FakeAcquirer()).reconcile(dataset_id)  # type: ignore[arg-type]

    assert adopted == 1
    assert final.is_file()
    assert not partial.exists()
    job = db.connection.execute(
        "SELECT state,resume_state FROM replay_jobs WHERE match_id='KR_1001'"
    ).fetchone()
    assert tuple(job) == (JobState.VERIFIED, None)


def test_reconcile_marks_interrupted_download_retryable_without_deleting_bad_partial(
    db: Database,
    config: Config,
    match_factory: Callable[..., MatchRecord],
) -> None:
    dataset_id, _, row = _seed_match(db, match_factory, state=JobState.DOWNLOADING)
    final, partial = replay_paths(config.data_dir, row)
    partial.parent.mkdir(parents=True)
    partial.write_bytes(b"not a replay")

    adopted = DownloadManager(db, config, FakeAcquirer()).reconcile(dataset_id)  # type: ignore[arg-type]

    assert adopted == 0
    assert not final.exists()
    assert partial.read_bytes() == b"not a replay"
    job = db.connection.execute(
        "SELECT state,resume_state FROM replay_jobs WHERE match_id='KR_1001'"
    ).fetchone()
    assert tuple(job) == (JobState.FAILED_RETRYABLE, JobState.QUEUED)


def test_manifest_is_stable_traceable_and_atomically_replaced(
    db: Database,
    config: Config,
    match_factory: Callable[..., MatchRecord],
) -> None:
    dataset_id, _, row = _seed_match(db, match_factory)
    final, _ = replay_paths(config.data_dir, row)
    final.parent.mkdir(parents=True)
    content = _rofl_bytes()
    final.write_bytes(content)
    check = verify_rofl(final)
    db.record_download(
        "KR_1001",
        file_path=final.relative_to(config.data_dir).as_posix(),
        file_size=check.file_size,
        sha256=check.sha256,
        provider="test-provider",
        verification=check.as_dict(),
    )
    target = manifest_path(config.data_dir, "26.18")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("old manifest\n", encoding="utf-8")

    result = write_manifest(db, dataset_id, config.data_dir, "26.18")

    assert result == target
    assert not target.with_suffix(".jsonl.tmp").exists()
    lines = target.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    item = json.loads(lines[0])
    assert item["match_id"] == "KR_1001"
    assert item["game_version"] == "26.18.704.1234"
    assert item["source_quality"] == "CHALLENGER_HEAVY"
    assert item["file"] == final.relative_to(config.data_dir).as_posix()
    assert item["size"] == len(content)
    assert item["sha256"] == hashlib.sha256(content).hexdigest()


def test_manifest_validation_failure_preserves_previous_manifest(
    db: Database,
    config: Config,
    match_factory: Callable[..., MatchRecord],
) -> None:
    dataset_id, _, row = _seed_match(db, match_factory)
    final, _ = replay_paths(config.data_dir, row)
    db.record_download(
        "KR_1001",
        file_path=final.relative_to(config.data_dir).as_posix(),
        file_size=123,
        sha256="a" * 64,
        provider="test-provider",
        verification={"gzip_crc": "PASS"},
    )
    target = manifest_path(config.data_dir, "26.18")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("known-good-old-manifest\n", encoding="utf-8")

    with pytest.raises(ValueError, match="asset missing or size mismatch"):
        write_manifest(db, dataset_id, config.data_dir, "26.18")

    assert target.read_text(encoding="utf-8") == "known-good-old-manifest\n"
