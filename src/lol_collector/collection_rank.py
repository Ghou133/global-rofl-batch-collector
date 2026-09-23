from __future__ import annotations

import hashlib
from enum import StrEnum

from lol_collector.artifacts import RawArtifactStore
from lol_collector.collection_runtime import RuntimeSession
from lol_collector.collection_store import CollectionStore, StoredRank
from lol_collector.collection_support import parse_rank_snapshot
from lol_collector.models import ArtifactType, PlayerId
from lol_collector.repository import Repository
from lol_collector.transport import HttpTransportError


class RankFetchStatus(StrEnum):
    OK = "OK"
    RECONNECT = "RECONNECT"
    UNKNOWN = "UNKNOWN"


async def fetch_rank(
    session: RuntimeSession,
    repository: Repository,
    store: CollectionStore,
    artifacts: RawArtifactStore,
    run_id: int,
    region: str,
    puuid: str,
    player_id: int,
) -> tuple[RankFetchStatus, StoredRank | None]:
    try:
        response = await session.sgp.ranked(puuid)
    except HttpTransportError:
        return RankFetchStatus.RECONNECT, None
    if response.status_code in {401, 403}:
        return RankFetchStatus.RECONNECT, None
    if response.status_code >= 400 or response.payload is None:
        store.record_error(run_id, "player", str(player_id), "RANK", "HTTP_ERROR", True, response.status_code, "rank request failed")
        return RankFetchStatus.UNKNOWN, None
    owner = hashlib.sha256(puuid.encode()).hexdigest()[:20]
    artifact = artifacts.write_json(
        artifact_type=ArtifactType.RANK,
        owner_type="player",
        owner_id=owner,
        endpoint_template="/leagues-ledge/v2/rankedStats/puuid/{puuid}",
        http_status=response.status_code,
        payload=response.payload,
    )
    artifact_id = repository.add_artifact(artifact)
    snapshot = parse_rank_snapshot(puuid, response.payload, region, PlayerId(player_id), artifact_id)
    if snapshot is None:
        return RankFetchStatus.UNKNOWN, None
    return RankFetchStatus.OK, store.save_rank(snapshot)
