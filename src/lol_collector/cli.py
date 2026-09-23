from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import sqlite3
from pathlib import Path

import anyio
import typer
from rich.console import Console
from rich.table import Table

from lol_collector.models import CollectorConfig, RunStatus, normalize_patch
from lol_collector.audit import audit_valid_matches
from lol_collector.collection_runner import CollectionRunner
from lol_collector.collection_runtime import acquire_runtime
from lol_collector.probe import CapabilityProbe, ProbeReport
from lol_collector.replay_archive import default_replay_source_dirs
from lol_collector.replay_capture import ReplayCaptureCoordinator, ReplayCaptureResult
from lol_collector.repository import Repository
from lol_collector.scheduler import CollectionScheduler
from lol_collector.security import scan_tree
from lol_collector.transport import Httpx2Transport

app = typer.Typer(no_args_is_help=True)
console = Console()
CN_DATA_DIR = Path("./data/CN")
CN_REPLAY_DATA_DIR = CN_DATA_DIR / "replay-paired"


@app.command()
def probe(output_dir: Path = typer.Option(CN_DATA_DIR, "--output-dir")) -> None:
    repository = Repository(output_dir / "collector.sqlite3")

    async def run_probe() -> ProbeReport:
        transport = Httpx2Transport()
        try:
            return await CapabilityProbe(output_dir, repository=repository, transport=transport).run()
        finally:
            await transport.aclose()

    try:
        report = anyio.run(run_probe)
    finally:
        repository.close()
    console.print(f"能力探测完成: {output_dir / 'capability-report-v2.json'}")
    console.print(f"LCU_CONNECTED={report.client.get('LCU_CONNECTED')}")


@app.command("init-run")
def init_run(
    data_dir: Path = typer.Option(CN_DATA_DIR, "--data-dir"),
    region: str = typer.Option(...),
    target_patch: str = typer.Option(..., "--target-patch"),
    target_matches: int = typer.Option(100, min=1, max=1000000),
    lookback_days: int = typer.Option(7, min=1, max=365),
    threshold: int = typer.Option(8, min=0, max=10),
) -> None:
    end = datetime.now(timezone.utc)
    config = CollectorConfig(region=region, target_patch=target_patch, window_start_at=end - timedelta(days=lookback_days), window_end_at=end, target_valid_matches=target_matches, master_plus_threshold=threshold)
    repository = Repository(data_dir / "collector.sqlite3")
    try:
        run_id = repository.create_run(config)
    finally:
        repository.close()
    console.print(f"Collection run created: {run_id}")


@app.command()
def status(data_dir: Path = typer.Option(CN_DATA_DIR, "--data-dir"), run_id: int = typer.Option(..., "--run-id")) -> None:
    repository = Repository(data_dir / "collector.sqlite3")
    try:
        counts = repository.counts(run_id)
    finally:
        repository.close()
    table = Table(title=f"Collection run {run_id}")
    table.add_column("Metric")
    table.add_column("Value", justify="right")
    for name, value in (("seed players", counts.seed_players), ("queried players", counts.queried_players), ("discovered players", counts.discovered_players), ("verified Master+", counts.master_plus_players), ("pending players", counts.pending_players), ("discovered Game IDs", counts.discovered_games), ("unique Game IDs", counts.unique_games), ("duplicate Game IDs", counts.duplicate_games), ("SUMMARY success", counts.summary_success), ("DETAILS success", counts.details_success), ("Master+ 10/10", counts.rank_10_of_10), ("Master+ >=8/10", counts.master_plus_8), ("Master+ >=5/10", counts.master_plus_5), ("failed", counts.failed), ("waiting retry", counts.waiting_retry), ("target valid", counts.valid_matches)):
        table.add_row(name, str(value))
    console.print(table)


@app.command("collect")
def collect(
    data_dir: Path = typer.Option(CN_DATA_DIR, "--data-dir"),
    run_id: int | None = typer.Option(None, "--run-id"),
    region: str = typer.Option("TENCENT", "--region"),
    target_patch: str | None = typer.Option(None, "--target-patch"),
    target_matches: int = typer.Option(100, "--target-matches", min=1, max=1000000),
    lookback_days: int = typer.Option(7, "--lookback-days", min=1, max=365),
    threshold: int = typer.Option(8, "--master-plus-threshold", min=0, max=10),
    poll_seconds: float = typer.Option(5.0, "--poll-seconds", min=0.2, max=300.0),
) -> None:
    repository = Repository(data_dir / "collector.sqlite3")
    selected_run = run_id
    try:
        detected_patch = _discover_patch(data_dir)
        resolved_patch = normalize_patch(target_patch or detected_patch or "")
        if resolved_patch is None:
            raise typer.BadParameter(
                "cannot determine the current client patch; start the League Client "
                + "or pass --target-patch"
            )
        if target_patch is not None and detected_patch is not None:
            current_patch = normalize_patch(detected_patch)
            if current_patch is not None and current_patch != resolved_patch:
                raise typer.BadParameter(
                    f"--target-patch {resolved_patch} does not match the current "
                    + f"client patch {current_patch}"
                )
        if detected_patch is not None:
            rolled_over = _mark_patch_rollovers(repository, region, resolved_patch)
            if rolled_over:
                console.print(
                    f"PATCH_ROLLOVER_RUNS={rolled_over} CURRENT_PATCH={resolved_patch}"
                )
        row = (
            _resumable_run(repository, region, target_matches, resolved_patch)
            if selected_run is None
            else repository.connection.execute(
                "SELECT * FROM collection_run WHERE id = ?", (selected_run,)
            ).fetchone()
        )
        if row is not None:
            selected_run = int(row["id"])
            config = _config_from_row(row)
            if config.target_patch != resolved_patch:
                raise typer.BadParameter(
                    f"run {selected_run} targets patch {config.target_patch}, but the "
                    + f"current client is {resolved_patch}; omit --run-id to create or "
                    + "resume a patch-isolated run"
                )
        else:
            now = datetime.now(timezone.utc)
            config = CollectorConfig(region=region, target_patch=resolved_patch, window_start_at=now - timedelta(days=lookback_days), window_end_at=now, target_valid_matches=target_matches, master_plus_threshold=threshold, require_known_rank_count=10)
            selected_run = repository.create_run(config)
        console.print(f"COLLECTION_RUN={selected_run}")

        async def execute() -> None:
            transport = Httpx2Transport()
            try:
                await CollectionRunner(repository, data_dir, selected_run, config, transport, poll_seconds, console).run()
            finally:
                await transport.aclose()

        try:
            anyio.run(execute)
        except KeyboardInterrupt:
            repository.set_run_status(selected_run, RunStatus.PAUSED)
            console.print(f"COLLECTION_INTERRUPTED run={selected_run}")
            raise typer.Exit(code=130) from None
    finally:
        repository.close()


def _resumable_run(
    repository: Repository,
    region: str,
    target_matches: int,
    target_patch: str,
) -> sqlite3.Row | None:
    return repository.connection.execute(
        "SELECT * FROM collection_run WHERE region = ? AND target_patch = ? "
        + "AND target_valid_matches = ? AND status NOT IN "
        + "('COMPLETE','EXHAUSTED','PATCH_ROLLOVER') ORDER BY id DESC LIMIT 1",
        (region, target_patch, target_matches),
    ).fetchone()


def _mark_patch_rollovers(
    repository: Repository,
    region: str,
    current_patch: str,
) -> int:
    with repository.lock, repository.connection:
        cursor = repository.connection.execute(
            "UPDATE collection_run SET status = ? WHERE region = ? "
            + "AND target_patch <> ? AND status NOT IN (?, ?, ?)",
            (
                RunStatus.PATCH_ROLLOVER.value,
                region,
                current_patch,
                RunStatus.COMPLETE.value,
                RunStatus.EXHAUSTED.value,
                RunStatus.PATCH_ROLLOVER.value,
            ),
        )
    return max(cursor.rowcount, 0)


def _config_from_row(row: sqlite3.Row) -> CollectorConfig:
    tiers = tuple(json.loads(str(row["selected_tiers"])))
    return CollectorConfig(region=str(row["region"]), target_tiers=tiers, target_patch=str(row["target_patch"]), window_start_at=datetime.fromisoformat(str(row["window_start_at"])), window_end_at=datetime.fromisoformat(str(row["window_end_at"])), target_valid_matches=int(row["target_valid_matches"]), master_plus_threshold=int(row["master_plus_threshold"]), require_known_rank_count=int(row["require_known_rank_count"]), seed=int(row["scheduler_rng_seed"]))


def _discover_patch(_data_dir: Path) -> str | None:
    async def resolve() -> str | None:
        transport = Httpx2Transport()
        try:
            session = await acquire_runtime(transport)
            return session.patch if session is not None else None
        finally:
            await transport.aclose()

    return anyio.run(resolve)


@app.command()
def run(data_dir: Path = typer.Option(CN_DATA_DIR, "--data-dir"), run_id: int = typer.Option(..., "--run-id"), region: str = typer.Option("", help="Required only when creating a run"), target_patch: str = typer.Option("", "--target-patch")) -> None:
    repository = Repository(data_dir / "collector.sqlite3")
    try:
        row = repository.connection.execute("SELECT * FROM collection_run WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            raise typer.BadParameter("run_id does not exist")
        config = CollectorConfig(region=str(row["region"] or region), target_patch=str(row["target_patch"] or target_patch), window_start_at=datetime.fromisoformat(row["window_start_at"]), window_end_at=datetime.fromisoformat(row["window_end_at"]), target_valid_matches=int(row["target_valid_matches"]), master_plus_threshold=int(row["master_plus_threshold"]), require_known_rank_count=int(row["require_known_rank_count"]), seed=int(row["scheduler_rng_seed"]))
        decision = CollectionScheduler(repository, config, run_id).decide()
    finally:
        repository.close()
    console.print(f"status={decision.status} valid={decision.valid_matches}/{decision.target_matches}")


@app.command()
def audit(data_dir: Path = typer.Option(CN_DATA_DIR, "--data-dir"), run_id: int = typer.Option(..., "--run-id"), seed: int = typer.Option(0), limit: int = typer.Option(20, min=1, max=20)) -> None:
    repository = Repository(data_dir / "collector.sqlite3")
    try:
        result = audit_valid_matches(repository.connection, run_id, seed, limit)
    finally:
        repository.close()
    console.print(f"audit={result.passed}/{result.checked} seed={result.seed}")
    for failure in result.failures:
        console.print(failure)


@app.command("security-scan")
def security_scan(data_dir: Path = typer.Option(CN_DATA_DIR, "--data-dir")) -> None:
    result = scan_tree(data_dir)
    console.print(f"TOKEN_PERSISTENCE_SCAN={'PASS' if result.passed else 'FAIL'}")
    for finding in result.findings:
        console.print(finding)


@app.command("replay")
def replay_capture(
    data_dir: Path = typer.Option(
        CN_REPLAY_DATA_DIR,
        "--data-dir",
        help="Isolated Replay+DETAILS paired data root",
    ),
    source_dir: list[Path] | None = typer.Option(
        None,
        "--source-dir",
        help="Official client Replay source directory; repeatable",
    ),
    game_id: list[str] | None = typer.Option(
        None,
        "--game-id",
        help="Download this gameId directly from Tencent SGP; repeatable",
    ),
    limit: int = typer.Option(
        10,
        "--limit",
        min=1,
        max=100,
        help="Maximum capture attempts; with --status, only rows displayed",
    ),
    status_only: bool = typer.Option(
        False,
        "--status",
        help="Read-only archive status; never downloads Replay files",
    ),
    watch: bool = typer.Option(False, "--watch", help="Keep watching for new Replay files"),
    include_existing: bool = typer.Option(
        False,
        "--include-existing",
        help="On a brand-new data root, process existing Replay files instead of baselining them",
    ),
    poll_seconds: float = typer.Option(
        5.0,
        "--poll-seconds",
        min=1.0,
        max=300.0,
    ),
) -> None:
    if status_only and watch:
        raise typer.BadParameter("--status and --watch cannot be used together")
    if game_id and watch:
        raise typer.BadParameter("--game-id and --watch cannot be used together")
    sources = tuple(source_dir) if source_dir else default_replay_source_dirs()
    repository = Repository(data_dir / "collector.sqlite3")
    coordinator = ReplayCaptureCoordinator(repository, data_dir, sources)
    try:
        if status_only:
            _render_replay_status(repository, limit)
            return

        async def execute_game_ids(game_ids: tuple[str, ...]) -> tuple[ReplayCaptureResult, ...]:
            transport = Httpx2Transport()
            try:
                session = await acquire_runtime(transport)
                return await coordinator.capture_game_ids(session, game_ids)
            finally:
                await transport.aclose()

        if game_id:
            selected = tuple(dict.fromkeys(game_id))[:limit]
            if any(not value.isdecimal() for value in selected):
                raise typer.BadParameter("every --game-id must contain decimal digits only")
            results = anyio.run(execute_game_ids, selected)
            _render_replay_results(results)
            return

        registration = coordinator.scan(include_existing=include_existing)
        if registration.initialized_now and not include_existing:
            console.print(
                f"REPLAY_BASELINE_INITIALIZED={registration.baseline_count} OLD_REPLAYS_IGNORED=YES"
            )
        elif registration.queued_count:
            console.print(f"NEW_REPLAY_QUEUED={registration.queued_count}")

        async def execute_once() -> tuple[ReplayCaptureResult, ...]:
            transport = Httpx2Transport()
            try:
                session = await acquire_runtime(transport)
                remote = await coordinator.capture_remote_pending(session, limit)
                local = await coordinator.capture_pending(session, limit - len(remote))
                return remote + local
            finally:
                await transport.aclose()

        async def execute_watch() -> None:
            while True:
                coordinator.scan(include_existing=False)
                results = await execute_once()
                if results:
                    _render_replay_results(results)
                await anyio.sleep(poll_seconds)

        if watch:
            console.print(f"REPLAY_WATCHING={', '.join(str(path) for path in sources)}")
            try:
                anyio.run(execute_watch)
            except KeyboardInterrupt:
                console.print("REPLAY_WATCH_STOPPED")
        else:
            results = anyio.run(execute_once)
            _render_replay_results(results)
            if not results:
                console.print("NO_NEW_REPLAY_PENDING")
    finally:
        repository.close()


def _render_replay_results(results: tuple[ReplayCaptureResult, ...]) -> None:
    table = Table(title="Replay + DETAILS capture")
    table.add_column("gameId", no_wrap=True)
    table.add_column("Replay", no_wrap=True)
    table.add_column("DETAILS", no_wrap=True)
    table.add_column("validation", no_wrap=True)
    table.add_column("bytes", justify="right")
    table.add_column("error")
    for result in results:
        table.add_row(
            result.game_id,
            result.download_status,
            "YES" if result.details_exists else "NO",
            result.validation_status,
            str(result.file_size or "-"),
            result.error_code or "-",
        )
    console.print(table)
    for result in results:
        console.print(
            f"gameId={result.game_id} sha256={result.sha256 or '-'} rofl={result.rofl_path or '-'} details={result.details_path or '-'}"
        )


def _render_replay_status(
    repository: Repository,
    limit: int,
) -> None:
    metrics = repository.replays.metrics()
    console.print(
        "REPLAY_STATUS_MODE=READ_ONLY DOWNLOADS_ATTEMPTED=0 "
        + f"RECENT_ROW_LIMIT={limit}"
    )
    table = Table(title="Replay paired archive status")
    table.add_column("Metric")
    table.add_column("Value", justify="right")
    for name, value in (
        ("source files", metrics.sources_total),
        ("old baseline (ignored)", metrics.sources_baseline),
        ("source queued", metrics.sources_queued),
        ("paired records", metrics.replays_total),
        ("queued", metrics.queued),
        ("downloading", metrics.downloading),
        ("validated pairs", metrics.validated),
        ("failed", metrics.failed),
        ("unavailable", metrics.unavailable),
        ("invalid", metrics.invalid),
    ):
        table.add_row(name, str(value))
    console.print(table)
    patch_table = Table(title="Replay records by patch")
    patch_table.add_column("Patch", no_wrap=True)
    patch_table.add_column("Records", justify="right")
    patch_table.add_column("Validated", justify="right")
    for patch_metrics in repository.replays.patch_metrics():
        patch_table.add_row(
            patch_metrics.patch or "<pending/unknown>",
            str(patch_metrics.total),
            str(patch_metrics.validated),
        )
    console.print(patch_table)
    records = repository.replays.list_records(limit)
    if metrics.replays_total > len(records):
        console.print(
            f"REPLAY_STATUS_SHOWING={len(records)} TOTAL={metrics.replays_total} (increase --limit to show more)"
        )
    if records:
        _render_replay_results(
            tuple(
                ReplayCaptureResult(
                    game_id=str(row["game_id"]),
                    details_exists=(
                        row["details_path"] is not None
                        and Path(str(row["details_path"])).is_file()
                    ),
                    download_status=str(row["download_status"]),
                    validation_status=str(row["validation_status"]),
                    rofl_path=(
                        str(row["rofl_path"])
                        if row["rofl_path"] is not None
                        else None
                    ),
                    details_path=(
                        str(row["details_path"])
                        if row["details_path"] is not None
                        else None
                    ),
                    file_size=(
                        int(row["file_size"])
                        if row["file_size"] is not None
                        else None
                    ),
                    sha256=(
                        str(row["sha256"]) if row["sha256"] is not None else None
                    ),
                    error_code=(
                        str(row["error_code"])
                        if row["error_code"] is not None
                        else None
                    ),
                    error_message=(
                        str(row["error_message"])
                        if row["error_message"] is not None
                        else None
                    ),
                )
                for row in records
            )
        )


if __name__ == "__main__":
    app()
