from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from enum import StrEnum
from typing import Protocol

from lol_collector.artifacts import RawArtifactStore
from lol_collector.collection_runtime import RuntimeSession
from lol_collector.collection_store import CollectionStore, StoredRank
from lol_collector.collection_rank import RankFetchStatus, fetch_rank
from lol_collector.collection_support import MatchSummary, parse_match_summary
from lol_collector.models import ArtifactType, CollectorConfig, JsonValue, normalize_patch
from lol_collector.queue_store import Lease
from lol_collector.repository import Repository
from lol_collector.transport import HttpResponse, HttpTransportError


class MatchOutcome(StrEnum):
    COMPLETE = "COMPLETE"
    RETRY = "RETRY"
    RECONNECT = "RECONNECT"


@dataclass(frozen=True, slots=True)
class ParticipantRank:
    puuid: str
    player_id: int
    rank: StoredRank | None


class MatchRequest(Protocol):
    async def __call__(self, game_id: str) -> HttpResponse: ...


class MatchProcessor:
    def __init__(
        self,
        repository: Repository,
        store: CollectionStore,
        artifacts: RawArtifactStore,
        config: CollectorConfig,
        run_id: int,
        region: str,
    ) -> None:
        self.repository = repository
        self.store = store
        self.artifacts = artifacts
        self.config = config
        self.run_id = run_id
        self.region = region

    async def process(self, session: RuntimeSession, lease: Lease) -> MatchOutcome:
        row = self.store.match_row(lease.entity_id)
        if row is None:
            return MatchOutcome.COMPLETE
        game_id = str(row["game_id"])
        summary_payload = self._read_artifact(row["summary_artifact_id"])
        if summary_payload is None:
            response = await self._request(session.sgp.summary, game_id)
            outcome, summary_payload = self._save_response(lease.entity_id, response, ArtifactType.SUMMARY, game_id, "summary")
            if outcome is not None:
                return outcome
        summary = parse_match_summary(summary_payload)
        if not summary.game_id:
            summary = MatchSummary(game_id, summary.queue_id, summary.map_id, summary.game_mode, summary.game_version, summary.game_start_at, summary.game_end_at, summary.participant_puuids)
        self.store.update_match_metadata(lease.entity_id, summary)

        details_payload = self._read_artifact(row["details_artifact_id"])
        if details_payload is None:
            response = await self._request(session.sgp.details, game_id)
            outcome, details_payload = self._save_response(lease.entity_id, response, ArtifactType.DETAILS, game_id, "details")
            if outcome is not None:
                return outcome

        participant_ids = _participant_ids(summary_payload)
        if len(participant_ids) != 10:
            participant_ids = _participant_ids(details_payload)
        ranks: list[ParticipantRank] = []
        for index, puuid in enumerate(participant_ids, start=1):
            player_id = self.repository.upsert_player(self.region, puuid)
            stored = self.store.rank_for_player(player_id) or self.store.rank_for_puuid(puuid)
            if stored is None:
                rank_status, stored = await fetch_rank(session, self.repository, self.store, self.artifacts, self.run_id, self.region, puuid, player_id)
                if rank_status is RankFetchStatus.RECONNECT:
                    return MatchOutcome.RECONNECT
            ranks.append(ParticipantRank(puuid, player_id, stored))
            self.store.save_participant(lease.entity_id, index, puuid, player_id, stored)
            self.store.enqueue_participant(self.run_id, player_id, stored.snapshot if stored else None)

        self._save_quality(lease.entity_id, summary, ranks)
        self.repository.mark_parsed(lease.entity_id)
        return MatchOutcome.COMPLETE

    async def _request(self, request: MatchRequest, game_id: str) -> HttpResponse:
        try:
            return await request(game_id)
        except HttpTransportError:
            raise

    def _save_response(
        self,
        match_id: int,
        response: HttpResponse,
        artifact_type: ArtifactType,
        game_id: str,
        step: str,
    ) -> tuple[MatchOutcome | None, JsonValue | None]:
        if response.status_code in {401, 403}:
            self.repository.retry_match(match_id, 0)
            return MatchOutcome.RECONNECT, None
        if response.status_code >= 400:
            self.store.record_error(self.run_id, "match", game_id, step.upper(), "HTTP_ERROR", True, response.status_code, _message(response.payload))
            self.repository.retry_match(match_id, 5)
            return MatchOutcome.RETRY, None
        if response.payload is None:
            self.store.record_error(self.run_id, "match", game_id, step.upper(), "EMPTY_BODY", True, response.status_code, "empty response body")
            self.repository.retry_match(match_id, 5)
            return MatchOutcome.RETRY, None
        artifact = self.artifacts.write_json(
            artifact_type=artifact_type,
            owner_type="match",
            owner_id=game_id,
            endpoint_template=f"/match-history-query/v1/products/lol/{{server}}_{game_id}/{step.upper()}",
            http_status=response.status_code,
            payload=response.payload,
        )
        artifact_id = self.repository.add_artifact(artifact)
        self.repository.attach_artifact(match_id, artifact_type, artifact_id)
        return None, response.payload

    def _read_artifact(self, artifact_id: int | None) -> JsonValue | None:
        if not isinstance(artifact_id, int):
            return None
        row = self.repository.connection.execute("SELECT filesystem_path FROM raw_artifact WHERE id = ?", (artifact_id,)).fetchone()
        if row is None:
            return None
        path = Path(str(row["filesystem_path"]))
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def _save_quality(self, match_id: int, summary: MatchSummary, ranks: list[ParticipantRank]) -> None:
        tiers = [item.rank.snapshot.tier for item in ranks if item.rank is not None]
        master = tiers.count("MASTER")
        grandmaster = tiers.count("GRANDMASTER")
        challenger = tiers.count("CHALLENGER")
        known = len(tiers)
        start = summary.game_start_at
        time_valid = start is not None and self.config.window_start_at <= start <= self.config.window_end_at
        patch_valid = normalize_patch(summary.game_version or "") == self.config.target_patch
        self.repository.update_quality(
            self.run_id,
            match_id,
            summary.queue_id == 420,
            patch_valid,
            time_valid,
            len(ranks),
            known,
            master,
            grandmaster,
            challenger,
            max(0, len(ranks) - known),
            self.config.require_known_rank_count,
            self.config.master_plus_threshold,
        )


def _participant_ids(payload: JsonValue | None) -> list[str]:
    body = payload.get("json") if isinstance(payload, Mapping) else None
    source = body if isinstance(body, Mapping) else payload
    participants = source.get("participants") if isinstance(source, Mapping) else None
    if not isinstance(participants, list):
        return []
    result: list[str] = []
    for item in participants:
        if not isinstance(item, Mapping):
            continue
        puuid = item.get("puuid")
        if isinstance(puuid, str) and puuid and puuid not in result:
            result.append(puuid)
    return result


def _message(payload: JsonValue | None) -> str:
    if isinstance(payload, Mapping):
        value = payload.get("message") or payload.get("errorCode")
        if isinstance(value, str):
            return value
    return "HTTP request failed"
