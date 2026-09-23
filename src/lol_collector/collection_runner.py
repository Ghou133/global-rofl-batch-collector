from __future__ import annotations

from pathlib import Path

import anyio
from rich.console import Console

from lol_collector.artifacts import RawArtifactStore
from lol_collector.collection_match import MatchOutcome, MatchProcessor
from lol_collector.collection_player import PlayerOutcome, PlayerProcessor
from lol_collector.collection_runtime import RuntimeSession, acquire_runtime
from lol_collector.collection_store import CollectionStore
from lol_collector.models import ArtifactType, CollectorConfig, RunStatus, normalize_patch
from lol_collector.queue_store import Lease, QueueCounts
from lol_collector.replay_capture import ReplayCaptureCoordinator
from lol_collector.repository import Repository
from lol_collector.scheduler import CollectionScheduler
from lol_collector.transport import HttpTransport, HttpTransportError


class CollectionRunner:
    def __init__(
        self,
        repository: Repository,
        output_dir: Path,
        run_id: int,
        config: CollectorConfig,
        transport: HttpTransport,
        poll_seconds: float = 5.0,
        console: Console | None = None,
    ) -> None:
        self.repository = repository
        self.output_dir = output_dir
        self.run_id = run_id
        self.config = config
        self.transport = transport
        self.poll_seconds = poll_seconds
        self.console = console or Console()
        self.store = CollectionStore(repository, config.region)
        self.artifacts = RawArtifactStore(output_dir / "collection-artifacts")
        self.owner = CollectionScheduler(repository, config, run_id).owner()
        self.session: RuntimeSession | None = None

    async def run(self) -> None:
        self.repository.recover_leases(self.run_id)
        self.repository.set_run_status(self.run_id, RunStatus.RUNNING)
        seeded = self._seed_ready()
        while True:
            counts = self.repository.counts(self.run_id)
            self._emit(counts)
            if counts.valid_matches >= self.config.target_valid_matches:
                self.repository.set_run_status(self.run_id, RunStatus.COMPLETE)
                self._emit(self.repository.counts(self.run_id))
                return
            if self.session is None:
                self.session = await acquire_runtime(self.transport, self.config.target_patch)
                if self.session is None:
                    self.repository.set_run_status(self.run_id, RunStatus.WAITING_FOR_CLIENT)
                    self.console.print("CLIENT_RECONNECT=WAITING_FOR_CLIENT", end="\n")
                    await anyio.sleep(self.poll_seconds)
                    continue
                if not self._apply_runtime_context(self.session):
                    self.session = None
                    return
                self.repository.set_run_status(self.run_id, RunStatus.RUNNING)
            if not seeded:
                seeded = await self._seed(self.session)
                if not seeded:
                    self.session = None
                    await anyio.sleep(self.poll_seconds)
                    continue
            match_lease = self.repository.lease_match(self.owner, run_id=self.run_id)
            if match_lease is not None:
                await self._process_match(match_lease)
                continue
            player_lease = self.repository.lease_player(self.run_id, self.owner)
            if player_lease is not None:
                await self._process_player(player_lease)
                continue
            counts = self.repository.counts(self.run_id)
            if counts.pending_players == 0:
                self.repository.set_run_status(self.run_id, RunStatus.EXHAUSTED)
                self._emit(counts)
                return
            await anyio.sleep(0.2)

    async def _seed(self, session: RuntimeSession) -> bool:
        for tier in self.config.target_tiers:
            if self.store.snapshot_exists(self.run_id, tier):
                continue
            endpoint = f"/lol-ranked/v1/apex-leagues/RANKED_SOLO_5x5/{tier}"
            try:
                response = await session.lcu.request(endpoint)
            except HttpTransportError:
                self.store.record_error(self.run_id, "leaderboard", tier, "SEED", "CLIENT_DISCONNECTED", True, None, "client unavailable")
                return False
            if response.status_code >= 400 or not isinstance(response.payload, dict):
                self.store.record_error(self.run_id, "leaderboard", tier, "SEED", "HTTP_ERROR", True, response.status_code, "leaderboard request failed")
                return False
            artifact = self.artifacts.write_json(
                artifact_type=ArtifactType.LEADERBOARD,
                owner_type="run",
                owner_id=f"{self.run_id}-{tier}",
                endpoint_template=endpoint,
                http_status=response.status_code,
                payload=response.payload,
            )
            self.store.save_leaderboard(self.run_id, tier, response.payload, artifact)
            self._emit(self.repository.counts(self.run_id))
        return self._seed_ready()

    async def _process_player(self, lease: Lease) -> None:
        if self.session is None:
            return
        processor = PlayerProcessor(
            self.repository,
            self.store,
            self.artifacts,
            self.config,
            self.run_id,
            self.session.region,
            self.session.server_id,
            self.session.platform,
        )
        try:
            outcome = await processor.process(self.session, lease)
        except HttpTransportError:
            self.repository.retry_player(self.run_id, lease.entity_id, 0)
            self.session = None
            self.console.print("CLIENT_RECONNECT=WAITING_FOR_CLIENT")
            return
        if outcome is PlayerOutcome.RECONNECT:
            self.repository.retry_player(self.run_id, lease.entity_id, 0)
            self.session = None
            self.console.print("CLIENT_RECONNECT=WAITING_FOR_CLIENT")

    async def _process_match(self, lease: Lease) -> None:
        if self.session is None:
            return
        processor = MatchProcessor(
            self.repository,
            self.store,
            self.artifacts,
            self.config,
            self.run_id,
            self.session.region,
        )
        try:
            outcome = await processor.process(self.session, lease)
        except HttpTransportError:
            self.repository.retry_match(lease.entity_id, 0)
            self.session = None
            self.console.print("CLIENT_RECONNECT=WAITING_FOR_CLIENT")
            return
        if outcome is MatchOutcome.RECONNECT:
            self.repository.retry_match(lease.entity_id, 0)
            self.session = None
            self.console.print("CLIENT_RECONNECT=WAITING_FOR_CLIENT")
        elif outcome is MatchOutcome.COMPLETE:
            row = self.store.match_row(lease.entity_id)
            if row is not None and normalize_patch(
                str(row["game_version_full"] or "")
            ) == self.config.target_patch:
                await self._capture_replay_pair(str(row["game_id"]))

    async def _capture_replay_pair(self, game_id: str) -> None:
        if self.session is None:
            return
        paired_root = self.output_dir / "replay-paired"
        paired_repository = Repository(paired_root / "collector.sqlite3")
        try:
            coordinator = ReplayCaptureCoordinator(
                paired_repository,
                paired_root,
                (),
            )
            result = await coordinator.capture_game_id(self.session, game_id)
            self.console.print(
                f"REPLAY_PAIR gameId={game_id} status={result.download_status} validation={result.validation_status}"
            )
        except Exception as error:
            self.console.print(
                f"REPLAY_PAIR gameId={game_id} status=FAILED error={type(error).__name__}"
            )
        finally:
            paired_repository.close()

    def _seed_ready(self) -> bool:
        return all(self.store.snapshot_exists(self.run_id, tier) for tier in self.config.target_tiers)

    def _apply_runtime_context(self, session: RuntimeSession) -> bool:
        if session.patch != self.config.target_patch:
            self.repository.set_run_status(self.run_id, RunStatus.PATCH_ROLLOVER)
            self.console.print(
                f"PATCH_ROLLOVER run={self.run_id} "
                + f"target={self.config.target_patch} current={session.patch}"
            )
            return False
        self.store.region = session.region
        self.store.save_run_context(self.run_id, session.region, session.platform, session.server_id, self.config.target_patch, session.full_version)
        return True

    def _emit(self, counts: QueueCounts) -> None:
        message = f"Seed Players: {counts.seed_players} | 已查询玩家: {counts.queried_players} | Master+ 玩家: {counts.master_plus_players} | "
        message += f"发现比赛: {counts.discovered_games} | 重复比赛: {counts.duplicate_games} | 唯一比赛: {counts.unique_games} | "
        message += f"SUMMARY 成功: {counts.summary_success} | DETAILS 成功: {counts.details_success} | "
        message += f"Master+ 10/10: {counts.rank_10_of_10} | Master+ >=8/10: {counts.master_plus_8} | Master+ >=5/10: {counts.master_plus_5} | "
        message += f"Valid Matches: {counts.valid_matches} / {self.config.target_valid_matches}"
        self.console.print(message)
