from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from lol_collector.adapters import (
    LcuClientAdapter,
    SgpClientAdapter,
    SgpCredentials,
    SgpEndpoints,
)
from lol_collector.artifacts import RawArtifactStore
from lol_collector.models import (
    CapabilityStatus,
    JsonObject,
    PaginationStatus,
)
from lol_collector.probe_helpers import (
    game_id_from_history,
    participant_puuids,
)
from lol_collector.sgp_probe_support import (
    lcu_request,
    record_response,
    response_status,
    sgp_request,
    sgp_server_id,
    token_status,
)
from lol_collector.transport import HttpTransport


_AKARI_SHA: Final[str] = "959dd03a78de6915a8abf35dbdd45642ad7c1b13"
_HN1_ENDPOINTS: Final[SgpEndpoints] = SgpEndpoints(
    match_history_base="https://hn1-k8s-sgp.lol.qq.com:21019",
    common_base="https://hn1-k8s-sgp.lol.qq.com:21019",
    region_path_param="HN1",
)


@dataclass(frozen=True, slots=True)
class SgpProbeResult:
    sgp: JsonObject
    history: JsonObject
    match: JsonObject
    rank: JsonObject


class SgpCapabilityProbe:
    def __init__(self, output_dir: Path, transport: HttpTransport) -> None:
        self.named_artifacts = RawArtifactStore(output_dir)
        self.transport = transport

    async def run(
        self,
        client: LcuClientAdapter,
        region: str | None,
        platform: str | None,
        puuid: str | None,
    ) -> SgpProbeResult:
        blocked = CapabilityStatus.BLOCKED_BY_DEPENDENCY.value
        not_tested = CapabilityStatus.NOT_TESTED.value
        sgp: JsonObject = {
            "SGP_SERVER_ID": None,
            "SGP_SERVER_CONFIG": blocked,
            "SGP_MATCH_HISTORY": blocked,
            "SGP_COMMON": blocked,
            "SGP_RANKED": blocked,
            "ENTITLEMENTS_READY": not_tested,
            "LEAGUE_SESSION_READY": not_tested,
            "CONFIG_SOURCE": f"LeagueAkari@{_AKARI_SHA}",
        }
        history: JsonObject = {
            "THREE_PAGE_PAGINATION": blocked,
            "PAGINATION_STATUS": PaginationStatus.UNKNOWN.value,
            "PAGES_REQUESTED": 0,
            "PAGES_SUCCEEDED": 0,
        }
        match: JsonObject = {"GAME_ID_AVAILABLE": blocked, "SUMMARY": blocked, "DETAILS": blocked}
        rank: JsonObject = {
            "TEN_PLAYER_RANK_COVERAGE": blocked,
            "TEN_PLAYER_RANK_COVERAGE_COUNT": 0,
            "PARTICIPANT_PUUID_COUNT": 0,
            "PRIMARY_RANK_ADAPTER": "UNRESOLVED",
            "FALLBACK_RANK_ADAPTER": "LCU",
        }

        server_id = sgp_server_id(region, platform)
        sgp["SGP_SERVER_ID"] = server_id
        endpoints = _HN1_ENDPOINTS if server_id == "TENCENT_HN1" else None
        if endpoints is None:
            sgp["SGP_SERVER_CONFIG"] = CapabilityStatus.UNKNOWN.value
            return SgpProbeResult(sgp, history, match, rank)

        sgp["SGP_SERVER_CONFIG"] = CapabilityStatus.SUPPORTED.value
        self.named_artifacts.write_named_json(
            "sgp-server-config.json",
            {
                "server_id": server_id,
                "matchHistory": endpoints.match_history_base,
                "common": endpoints.common_base,
                "regionPathParam": endpoints.region_path_param,
                "source": f"LeagueAkari@{_AKARI_SHA}",
            },
        )
        credentials = await self._credentials(client, sgp)
        if credentials is None or puuid is None:
            return SgpProbeResult(sgp, history, match, rank)

        adapter = SgpClientAdapter(endpoints, credentials, self.transport)
        await self._probe_common(adapter, puuid, sgp, rank)
        pages = await self._probe_history(adapter, puuid, sgp, history)
        if history["THREE_PAGE_PAGINATION"] != CapabilityStatus.SUPPORTED.value:
            return SgpProbeResult(sgp, history, match, rank)

        game_id = game_id_from_history(pages)
        match["GAME_ID_AVAILABLE"] = (
            CapabilityStatus.SUPPORTED.value if game_id else CapabilityStatus.UNKNOWN.value
        )
        if game_id is None:
            return SgpProbeResult(sgp, history, match, rank)
        match["GAME_ID"] = game_id

        summary = await sgp_request(adapter.summary, game_id)
        if not record_response(
            self.named_artifacts, "sgp-summary.json", "sgp-summary-error.json", summary
        ):
            match["SUMMARY"] = response_status(summary)
            return SgpProbeResult(sgp, history, match, rank)
        match["SUMMARY"] = CapabilityStatus.SUPPORTED.value

        details = await sgp_request(adapter.details, game_id)
        if details is None or not record_response(
            self.named_artifacts,
            "sgp-details.json",
            "sgp-details-error.json",
            details,
        ):
            match["DETAILS"] = response_status(details)
            return SgpProbeResult(sgp, history, match, rank)
        match["DETAILS"] = CapabilityStatus.SUPPORTED.value

        participant_ids = participant_puuids(details.payload)
        rank["PARTICIPANT_PUUID_COUNT"] = len(participant_ids)
        if len(participant_ids) != 10:
            rank["TEN_PLAYER_RANK_COVERAGE"] = CapabilityStatus.UNKNOWN.value
            return SgpProbeResult(sgp, history, match, rank)

        successes = 0
        for index, participant_puuid in enumerate(participant_ids, start=1):
            response = await sgp_request(adapter.ranked, participant_puuid)
            success_name = f"sgp-rank-participant-{index:02d}.json"
            error_name = f"sgp-rank-participant-{index:02d}-error.json"
            if record_response(
                self.named_artifacts, success_name, error_name, response
            ):
                successes += 1
        rank["TEN_PLAYER_RANK_COVERAGE_COUNT"] = successes
        rank["TEN_PLAYER_RANK_COVERAGE"] = (
            CapabilityStatus.SUPPORTED.value
            if successes == 10
            else CapabilityStatus.FAILED.value
        )
        return SgpProbeResult(sgp, history, match, rank)

    async def _credentials(
        self, client: LcuClientAdapter, sgp: JsonObject
    ) -> SgpCredentials | None:
        entitlements = await lcu_request(client, "/entitlements/v1/token")
        league_session = await lcu_request(
            client, "/lol-league-session/v1/league-session-token"
        )
        access_token: str | None = None
        if entitlements is not None and isinstance(entitlements.payload, Mapping):
            candidate = entitlements.payload.get("accessToken")
            if isinstance(candidate, str) and candidate:
                access_token = candidate
        session_token = (
            league_session.payload
            if league_session is not None
            and league_session.status_code < 400
            and isinstance(league_session.payload, str)
            and league_session.payload
            else None
        )
        sgp["ENTITLEMENTS_READY"] = token_status(entitlements, access_token is not None)
        sgp["LEAGUE_SESSION_READY"] = token_status(
            league_session, session_token is not None
        )
        if access_token is None or session_token is None:
            return None
        return SgpCredentials(access_token, session_token)

    async def _probe_common(
        self,
        adapter: SgpClientAdapter,
        puuid: str,
        sgp: JsonObject,
        rank: JsonObject,
    ) -> None:
        response = await sgp_request(adapter.ranked, puuid)
        if record_response(
            self.named_artifacts,
            "sgp-ranked-current.json",
            "sgp-ranked-current-error.json",
            response,
        ):
            sgp["SGP_COMMON"] = CapabilityStatus.SUPPORTED.value
            sgp["SGP_RANKED"] = CapabilityStatus.SUPPORTED.value
            rank["PRIMARY_RANK_ADAPTER"] = "SGP_COMMON"
            return
        status = response_status(response)
        sgp["SGP_COMMON"] = status
        sgp["SGP_RANKED"] = status

    async def _probe_history(
        self,
        adapter: SgpClientAdapter,
        puuid: str,
        sgp: JsonObject,
        history: JsonObject,
    ) -> list[JsonObject]:
        page_size = 10
        pages: list[JsonObject] = []
        lengths: list[int] = []
        history["PAGES_REQUESTED"] = 3
        for page_number in range(1, 4):
            start_index = (page_number - 1) * page_size
            response = await sgp_request(
                adapter.match_history, puuid, start_index, page_size
            )
            if response is None or not record_response(
                self.named_artifacts,
                f"sgp-history-page-{page_number}.json",
                f"sgp-history-page-{page_number}-error.json",
                response,
            ):
                sgp["SGP_MATCH_HISTORY"] = response_status(response)
                history["THREE_PAGE_PAGINATION"] = response_status(response)
                return pages
            if not isinstance(response.payload, dict):
                sgp["SGP_MATCH_HISTORY"] = CapabilityStatus.UNKNOWN.value
                history["THREE_PAGE_PAGINATION"] = CapabilityStatus.UNKNOWN.value
                return pages
            games = response.payload.get("games")
            if not isinstance(games, list):
                sgp["SGP_MATCH_HISTORY"] = CapabilityStatus.UNKNOWN.value
                history["THREE_PAGE_PAGINATION"] = CapabilityStatus.UNKNOWN.value
                return pages
            pages.append(response.payload)
            lengths.append(len(games))
            history["PAGES_SUCCEEDED"] = page_number

        sgp["SGP_MATCH_HISTORY"] = CapabilityStatus.SUPPORTED.value
        history["THREE_PAGE_PAGINATION"] = CapabilityStatus.SUPPORTED.value
        history["PAGE_ITEM_COUNTS"] = lengths
        history["PAGINATION_STATUS"] = (
            PaginationStatus.COMPLETE.value
            if any(length < page_size for length in lengths)
            else PaginationStatus.INCOMPLETE.value
        )
        return pages
