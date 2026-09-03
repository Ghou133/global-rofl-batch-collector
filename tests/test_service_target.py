from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

import kr_rofl_collector.service as service_module
from kr_rofl_collector.config import Config
from kr_rofl_collector.errors import ConfigurationError
from kr_rofl_collector.locking import CollectorLock
from kr_rofl_collector.models import MatchRecord
from kr_rofl_collector.replay import replay_paths, verify_rofl
from kr_rofl_collector.service import CollectorService


def _rofl_bytes(version: str = "26.18.704.1234") -> bytes:
    version_bytes = version.encode()
    header = bytearray(0x0F)
    header[:4] = b"RIOT"
    header[0x0E] = len(version_bytes)
    header.extend(version_bytes)
    payload = b"chunk"
    chunk_header = bytearray(0x11)
    chunk_header[5:9] = (1 << 24).to_bytes(4, "little")
    chunk_header[9:13] = len(payload).to_bytes(4, "little")
    metadata = json.dumps({"gameVersion": version}).encode()
    return (
        bytes(header)
        + bytes(chunk_header)
        + payload
        + bytes(0x100)
        + metadata
        + len(metadata).to_bytes(4, "little")
    )


def _seed_verified(
    service: CollectorService,
    config: Config,
    match_factory: Callable[..., MatchRecord],
) -> int:
    dataset_id = service.db.dataset("KR", "26.18", "26.18.704.1234")
    record = match_factory()
    service.db.store_match(dataset_id, record, eligible=True)
    row = service.db.match_row(record.match_id)
    final, _ = replay_paths(config.data_dir, row)
    final.parent.mkdir(parents=True)
    final.write_bytes(_rofl_bytes())
    check = verify_rofl(final)
    service.db.record_download(
        record.match_id,
        file_path=final.relative_to(config.data_dir).as_posix(),
        file_size=check.file_size,
        sha256=check.sha256,
        provider="test-provider",
        verification=check.as_dict(),
    )
    return dataset_id


def test_target_is_dataset_total_and_already_verified_files_need_no_api_key(
    config: Config,
    match_factory: Callable[..., MatchRecord],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with CollectorService(config) as service:
        dataset_id = _seed_verified(service, config, match_factory)
        stale_run = service.db.start_run(dataset_id, "run", 999)
        monkeypatch.setattr(service, "_patch", lambda: ("26.18", "26.18.704.1234"))

        result = service.run(1)

        assert result["status"] == "TARGET_ALREADY_MET"
        assert result["verified_before"] == 1
        assert result["verified"] == 1
        assert Path(result["manifest"]).is_file()
        assert service.db.connection.execute(
            "SELECT status FROM runs WHERE id=?", (stale_run,)
        ).fetchone()["status"] == "INTERRUPTED"


def test_probe_enters_collector_lock_before_running_probe_body(
    config: Config,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[tuple[str, Path] | str] = []

    class SpyLock:
        def __init__(self, path: Path):
            self.path = path

        def __enter__(self) -> None:
            events.append(("enter", self.path))

        def __exit__(self, *_args: object) -> None:
            events.append("exit")

    monkeypatch.setattr(service_module, "CollectorLock", SpyLock)
    with CollectorService(config) as service:
        def probe_body() -> dict[str, bool]:
            assert events == [("enter", config.data_dir / ".collector.lock")]
            events.append("body")
            return {"ok": True}

        monkeypatch.setattr(service, "_probe_locked", probe_body)
        assert service.probe() == {"ok": True}

    assert events == [
        ("enter", config.data_dir / ".collector.lock"),
        "body",
        "exit",
    ]


def test_probe_is_rejected_while_collector_lock_is_already_held(config: Config) -> None:
    lock_path = config.data_dir / ".collector.lock"
    with (
        CollectorService(config) as service,
        CollectorLock(lock_path),
        pytest.raises(ConfigurationError) as raised,
    ):
        service.probe()

    assert raised.value.code == "COLLECTOR_ALREADY_RUNNING"
    assert raised.value.message == "Another collector process is already running"


def test_probe_interrupts_stale_run_before_missing_key_failure(
    config: Config,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with CollectorService(config) as service:
        dataset_id = service.db.dataset("KR", "26.18", "26.18.704.1234")
        stale_run = service.db.start_run(dataset_id, "run", 100)
        monkeypatch.setattr(service, "_patch", lambda: ("26.18", "26.18.704.1234"))

        with pytest.raises(ConfigurationError, match="API_KEY_MISSING"):
            service.probe()

        runs = list(
            service.db.connection.execute(
                "SELECT id,status FROM runs WHERE dataset_id=? ORDER BY id", (dataset_id,)
            )
        )
        assert [(row["id"], row["status"]) for row in runs] == [
            (stale_run, "INTERRUPTED"),
            (stale_run + 1, "FAILED"),
        ]


def test_discovery_backlog_is_based_on_missing_total_not_full_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = Config(
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        db_path=tmp_path / "data" / "collector.sqlite3",
        logs_dir=tmp_path / "logs",
        riot_api_key="placeholder",
        league_install_dir=tmp_path / "League",
        replay_edge_base_url=None,
        request_timeout=1,
        api_min_interval=0,
        api_max_retries=0,
        history_count=20,
    )
    captured: dict[str, int] = {}

    class DummyApi:
        def __init__(self, *_args: Any, **_kwargs: Any):
            pass

        def close(self) -> None:
            pass

    class StopAfterCapture:
        def __init__(self, *_args: Any, **_kwargs: Any):
            pass

        def discover(
            self,
            _dataset_id: int,
            _run_id: int,
            _patch: str,
            wanted_candidates: int,
        ) -> None:
            captured["wanted_candidates"] = wanted_candidates
            raise RuntimeError("stop after target calculation")

    monkeypatch.setattr(service_module, "RiotApi", DummyApi)
    monkeypatch.setattr(service_module, "DiscoveryService", StopAfterCapture)

    with CollectorService(config) as service:
        monkeypatch.setattr(service, "_patch", lambda: ("26.18", "26.18.704.1234"))
        monkeypatch.setattr(service.db, "verified_count", lambda _dataset_id: 80)
        with pytest.raises(RuntimeError, match="stop after target calculation"):
            service.run(100)

    # Missing is 20; the safety backlog is 20 + max(20, ceil(20 * .25)) = 40.
    assert captured["wanted_candidates"] == 40


@pytest.mark.parametrize("target", [0, -1])
def test_target_must_be_positive_without_touching_external_services(
    config: Config,
    target: int,
) -> None:
    with (
        CollectorService(config) as service,
        pytest.raises(ConfigurationError, match="INVALID_TARGET"),
    ):
        service.run(target)
