from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum

from lol_collector.artifacts import RawArtifactStore
from lol_collector.collection_rank import RankFetchStatus, fetch_rank
from lol_collector.collection_runtime import RuntimeSession
from lol_collector.collection_store import CollectionStore
from lol_collector.collection_support import MatchSummary, history_games, is_master_plus, parse_match_summary
from lol_collector.models import ArtifactType, CollectorConfig, PlayerQueueState, RankStatus, normalize_patch
from lol_collector.queue_store import Lease
from lol_collector.repository import Repository
from lol_collector.transport import HttpTransportError


class PlayerOutcome(StrEnum):
    COMPLETE = "COMPLETE"
    RETRY = "RETRY"
    RECONNECT = "RECONNECT"


class PlayerProcessor:
    def __init__(
        self,
        repository: Repository,
        store: CollectionStore,
        artifacts: RawArtifactStore,
        config: CollectorConfig,
        run_id: int,
        region: str,
        server_id: str,
        platform: str,
    ) -> None:
        self.repository = repository
        self.store = store
        self.artifacts = artifacts
        self.config = config
        self.run_id = run_id
        self.region = region
        self.server_id = server_id
        self.platform = platform

    async def process(self, session: RuntimeSession, lease: Lease) -> PlayerOutcome:
        puuid = self.store.player_puuid(lease.entity_id)
        if puuid is None:
            self.repository.release_player(self.run_id, lease.entity_id, PlayerQueueState.NOT_MASTER_PLUS)
            return PlayerOutcome.COMPLETE
        rank = self.store.rank_for_player(lease.entity_id) or self.store.rank_for_puuid(puuid)
        if rank is None:
            status, rank = await fetch_rank(session, self.repository, self.store, self.artifacts, self.run_id, self.region, puuid, lease.entity_id)
            if status is RankFetchStatus.RECONNECT:
                return PlayerOutcome.RECONNECT
        if rank is None or not is_master_plus(rank.snapshot.tier):
            self.repository.release_player(self.run_id, lease.entity_id, PlayerQueueState.NOT_MASTER_PLUS)
            self.repository.set_player_rank_status(self.run_id, lease.entity_id, PlayerQueueState.NOT_MASTER_PLUS, RankStatus.VERIFIED if rank else RankStatus.UNKNOWN)
            return PlayerOutcome.COMPLETE
        self.repository.set_player_rank_status(self.run_id, lease.entity_id, PlayerQueueState.MASTER_PLUS, RankStatus.VERIFIED)

        row = self.store.player_queue_row(self.run_id, lease.entity_id)
        start_index = int(row["history_start_index"] or 0) if row is not None else 0
        page_size = int(row["history_page_size"] or 20) if row is not None else 20
        try:
            response = await session.sgp.match_history(puuid, start_index, page_size)
        except HttpTransportError:
            return PlayerOutcome.RECONNECT
        if response.status_code in {401, 403}:
            return PlayerOutcome.RECONNECT
        if response.status_code >= 400 or not isinstance(response.payload, Mapping):
            self.store.record_error(self.run_id, "player", str(lease.entity_id), "HISTORY", "HTTP_ERROR", True, response.status_code, "history request failed")
            self.repository.retry_player(self.run_id, lease.entity_id, 5)
            return PlayerOutcome.RETRY
        artifact = self.artifacts.write_json(
            artifact_type=ArtifactType.HISTORY,
            owner_type="player",
            owner_id=f"{lease.entity_id}-{start_index}",
            endpoint_template="/match-history-query/v1/products/lol/player/{puuid}/SUMMARY",
            http_status=response.status_code,
            payload=response.payload,
        )
        self.repository.add_artifact(artifact)
        games = history_games(response.payload)
        oldest = None
        inserted = 0
        for game in games:
            summary = parse_match_summary({"json": game})
            if summary.game_start_at is not None and (oldest is None or summary.game_start_at < oldest):
                oldest = summary.game_start_at
            if not _is_target_history_game(summary, self.config):
                continue
            _, is_new = self.repository.discover_match(self.run_id, lease.entity_id, self.server_id, self.platform, summary.game_id, start_index // page_size)
            inserted += int(is_new)
        done = not games or len(games) < page_size or (oldest is not None and oldest < self.config.window_start_at)
        self.repository.release_player(
            self.run_id,
            lease.entity_id,
            PlayerQueueState.CRAWLED if done else PlayerQueueState.CRAWLING,
            start_index + page_size,
            int(row["pages_crawled"] or 0) + 1 if row is not None else 1,
        )
        return PlayerOutcome.COMPLETE


def _is_target_history_game(
    summary: MatchSummary,
    config: CollectorConfig,
) -> bool:
    if summary.queue_id != 420 or not summary.game_id:
        return False
    patch = normalize_patch(summary.game_version or "")
    if patch is not None and patch != config.target_patch:
        return False
    start = summary.game_start_at
    return start is None or config.window_start_at <= start <= config.window_end_at
