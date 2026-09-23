from __future__ import annotations

from pathlib import Path

from lol_collector.adapters import LcuClientAdapter
from lol_collector.artifacts import ArtifactRecord, RawArtifactStore
from lol_collector.models import (
    ArtifactType,
    CapabilityStatus,
    JsonObject,
    JsonValue,
    schema_fingerprint,
)
from lol_collector.probe_helpers import (
    apex_info,
    error_evidence,
    find_solo_queue,
    queue_entry,
    ranked_queue_types,
)
from lol_collector.repository import Repository
from lol_collector.transport import HttpResponse, HttpTransportError


class LcuRankedProbe:
    def __init__(self, output_dir: Path, repository: Repository | None) -> None:
        self.artifacts = RawArtifactStore(output_dir / "probe-artifacts")
        self.named_artifacts = RawArtifactStore(output_dir)
        self.repository = repository

    async def probe_queue(
        self, client: LcuClientAdapter
    ) -> tuple[JsonObject, JsonObject]:
        try:
            response, payload = await client.get_json("/lol-game-queues/v1/queues")
        except HttpTransportError:
            return _unknown_queue(CapabilityStatus.FAILED), {}
        if response.status_code >= 400 or not isinstance(payload, list):
            status = (
                CapabilityStatus.FAILED
                if response.status_code >= 400
                else CapabilityStatus.UNKNOWN
            )
            return _unknown_queue(status), {}

        artifact = self._save(
            ArtifactType.QUEUE,
            "client",
            "queues",
            "/lol-game-queues/v1/queues",
            response,
            payload,
        )
        solo = queue_entry(payload, 420)
        flex = queue_entry(payload, 440)
        if solo is not None:
            self.named_artifacts.write_named_json("queue-420.json", solo)
        if flex is not None:
            self.named_artifacts.write_named_json("queue-440.json", flex)

        ranked_response, ranked_payload = await self._current_ranked(client)
        queue_types = ranked_queue_types(ranked_payload)
        queue_type_values: list[JsonValue] = []
        queue_type_values.extend(sorted(queue_types))
        confirmed = (
            find_solo_queue(payload) == 420
            and solo is not None
            and solo.get("type") == "RANKED_SOLO_5x5"
            and flex is not None
            and flex.get("type") == "RANKED_FLEX_SR"
            and "RANKED_SOLO_5x5" in queue_types
        )
        queue: JsonObject = {
            "QUEUE_METADATA": CapabilityStatus.SUPPORTED.value,
            "SOLO_RANKED_QUEUE_STATUS": (
                CapabilityStatus.SUPPORTED.value
                if confirmed
                else CapabilityStatus.UNKNOWN.value
            ),
            "SOLO_RANKED_QUEUE_CONFIRMED": confirmed,
            "SOLO_RANKED_QUEUE_ID": 420 if confirmed else None,
            "QUEUE_420_TYPE": solo.get("type") if solo else None,
            "QUEUE_440_TYPE": flex.get("type") if flex else None,
            "RANKED_STATS_QUEUE_TYPES": queue_type_values,
            "QUEUE_SCHEMA_HASH": schema_fingerprint(payload),
        }
        lcu_rank: JsonObject = {
            "LCU_RANK_CURRENT": (
                CapabilityStatus.SUPPORTED.value
                if ranked_response is not None
                and ranked_response.status_code < 400
                and ranked_payload is not None
                else CapabilityStatus.FAILED.value
                if ranked_response is None or ranked_response.status_code >= 400
                else CapabilityStatus.UNKNOWN.value
            ),
            "SOLO_RANK_SCHEMA_CONFIRMED": "RANKED_SOLO_5x5" in queue_types,
        }
        self._record_queue(response, artifact)
        return queue, lcu_rank

    async def probe_rank_by_puuid(
        self,
        client: LcuClientAdapter,
        puuid: str | None,
        lcu_rank: JsonObject,
    ) -> None:
        if not puuid:
            lcu_rank["LCU_RANK_BY_PUUID"] = CapabilityStatus.BLOCKED_BY_DEPENDENCY.value
            return
        try:
            response, payload = await client.get_json(f"/lol-ranked/v1/ranked-stats/{puuid}")
        except HttpTransportError:
            lcu_rank["LCU_RANK_BY_PUUID"] = CapabilityStatus.FAILED.value
            return
        lcu_rank["LCU_RANK_BY_PUUID"] = (
            CapabilityStatus.SUPPORTED.value
            if response.status_code < 400 and isinstance(payload, dict)
            else CapabilityStatus.FAILED.value
            if response.status_code >= 400
            else CapabilityStatus.UNKNOWN.value
        )
        if isinstance(payload, dict):
            self._save(
                ArtifactType.RANK,
                "client",
                "current-by-puuid",
                "/lol-ranked/v1/ranked-stats/{puuid}",
                response,
                payload,
            )

    async def probe_apex(
        self, client: LcuClientAdapter
    ) -> dict[str, JsonObject]:
        result: dict[str, JsonObject] = {}
        for tier in ("MASTER", "GRANDMASTER", "CHALLENGER"):
            invalid_path = f"/lol-ranked/v1/apex-leagues/SOLO5V5/{tier}"
            try:
                invalid_response, invalid_payload = await client.get_json(invalid_path)
            except HttpTransportError:
                result[tier] = _failed_apex()
                continue
            invalid_info = apex_info(invalid_response, invalid_payload)
            if invalid_response.status_code >= 400:
                self.named_artifacts.write_named_json(
                    f"apex-{tier.lower()}-error.json",
                    error_evidence(invalid_path, invalid_response),
                )
            if not _invalid_queue_enum(invalid_response, invalid_info):
                result[tier] = invalid_info
                continue

            corrected_path = f"/lol-ranked/v1/apex-leagues/RANKED_SOLO_5x5/{tier}"
            try:
                response, payload = await client.get_json(corrected_path)
            except HttpTransportError:
                failure = _failed_apex()
                failure["invalid_enum_diagnostic"] = invalid_info
                result[tier] = failure
                continue
            info = apex_info(response, payload)
            info["endpoint"] = corrected_path
            info["invalid_enum_diagnostic"] = invalid_info
            result[tier] = info
            if payload is not None:
                self._save(
                    ArtifactType.LEADERBOARD,
                    "apex",
                    tier.lower(),
                    corrected_path,
                    response,
                    payload,
                )
        return result

    async def _current_ranked(
        self, client: LcuClientAdapter
    ) -> tuple[HttpResponse | None, JsonObject | None]:
        try:
            response, payload = await client.get_json(
                "/lol-ranked/v1/current-ranked-stats"
            )
        except HttpTransportError:
            return None, None
        if not isinstance(payload, dict):
            return response, None
        self._save(
            ArtifactType.RANK,
            "client",
            "current",
            "/lol-ranked/v1/current-ranked-stats",
            response,
            payload,
        )
        self.named_artifacts.write_named_json("current-ranked-stats.json", payload)
        return response, payload

    def _save(
        self,
        artifact_type: ArtifactType,
        owner_type: str,
        owner_id: str,
        endpoint: str,
        response: HttpResponse,
        payload: JsonValue,
    ) -> ArtifactRecord:
        return self.artifacts.write_json(
            artifact_type=artifact_type,
            owner_type=owner_type,
            owner_id=owner_id,
            endpoint_template=endpoint,
            http_status=response.status_code,
            payload=payload,
        )

    def _record_queue(self, response: HttpResponse, artifact: ArtifactRecord) -> None:
        if self.repository is None:
            return
        artifact_id = self.repository.add_artifact(artifact)
        self.repository.record_probe(
            "/lol-game-queues/v1/queues",
            "QUEUE_METADATA",
            str(response.status_code),
            response.status_code < 400,
            artifact.schema_hash,
            None,
            artifact_id,
        )


def _unknown_queue(status: CapabilityStatus) -> JsonObject:
    return {
        "QUEUE_METADATA": status.value,
        "SOLO_RANKED_QUEUE_STATUS": CapabilityStatus.UNKNOWN.value,
        "SOLO_RANKED_QUEUE_CONFIRMED": None,
        "SOLO_RANKED_QUEUE_ID": None,
        "QUEUE_SCHEMA_HASH": None,
    }


def _failed_apex() -> JsonObject:
    return {
        "capability_status": CapabilityStatus.FAILED.value,
        "pagination_status": "UNKNOWN",
    }


def _invalid_queue_enum(response: HttpResponse, info: JsonObject) -> bool:
    message = info.get("message")
    normalized = message.lower() if isinstance(message, str) else ""
    return (
        response.status_code == 400
        and info.get("errorCode") == "RPC_ERROR"
        and ("invalid" in normalized or "not a valid" in normalized)
        and "queue" in normalized
    )
