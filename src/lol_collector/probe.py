from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from lol_collector.adapters import LcuClientAdapter, LcuConnection, discover_lcu
from lol_collector.artifacts import RawArtifactStore
from lol_collector.lcu_identity_probe import LcuIdentityProbe
from lol_collector.lcu_ranked_probe import LcuRankedProbe
from lol_collector.models import CapabilityStatus, JsonObject, utc_now
from lol_collector.phase0_diagnostics import render_phase0_diagnostics
from lol_collector.repository import Repository
from lol_collector.security import scan_tree
from lol_collector.sgp_probe import SgpCapabilityProbe
from lol_collector.transport import HttpTransport


@dataclass
class ProbeReport:
    client: JsonObject = field(default_factory=dict)
    queue: JsonObject = field(default_factory=dict)
    apex: dict[str, JsonObject] = field(default_factory=dict)
    lcu_rank: JsonObject = field(default_factory=dict)
    sgp: JsonObject = field(default_factory=dict)
    history: JsonObject = field(default_factory=dict)
    match: JsonObject = field(default_factory=dict)
    rank: JsonObject = field(default_factory=dict)
    replay: JsonObject = field(default_factory=dict)
    resilience: JsonObject = field(default_factory=dict)
    security: JsonObject = field(default_factory=dict)

    def to_json(self) -> JsonObject:
        return {
            "generated_at": utc_now().isoformat(),
            "CLIENT": self.client,
            "QUEUE": self.queue,
            "APEX": self.apex,
            "LCU_RANK": self.lcu_rank,
            "SGP": self.sgp,
            "SGP_HISTORY": self.history,
            "MATCH": self.match,
            "RANK": self.rank,
            "REPLAY": self.replay,
            "RESILIENCE": self.resilience,
            "SECURITY": self.security,
        }


class CapabilityProbe:
    def __init__(
        self,
        output_dir: Path,
        repository: Repository | None = None,
        transport: HttpTransport | None = None,
    ) -> None:
        self.output_dir = output_dir
        self.named_artifacts = RawArtifactStore(output_dir)
        self.repository = repository
        self.transport = transport
        self.identity_probe = LcuIdentityProbe(output_dir)
        self.ranked_probe = LcuRankedProbe(output_dir, repository)

    async def run(self, connection: LcuConnection | None = None) -> ProbeReport:
        report = ProbeReport()
        _set_phase0_defaults(report)
        connection = connection or discover_lcu()
        if connection is None or self.transport is None:
            _set_blocked(report)
            self._write_reports(report, "UNRESOLVED")
            return report

        client = LcuClientAdapter(connection, self.transport)
        region, platform, puuid = await self._probe_client(client, report)
        await self._probe_queue(client, report)
        await self._probe_rank_by_puuid(client, puuid, report)
        await self._probe_apex(client, report)

        sgp_result = await SgpCapabilityProbe(self.output_dir, self.transport).run(
            client, region, platform, puuid
        )
        report.sgp = sgp_result.sgp
        report.history = sgp_result.history
        report.match = sgp_result.match
        report.rank = sgp_result.rank
        self._write_reports(report, _seed_method(report.apex))
        return report

    async def _probe_client(
        self, client: LcuClientAdapter, report: ProbeReport
    ) -> tuple[str | None, str | None, str | None]:
        result = await self.identity_probe.run(client)
        report.client = result.client
        return result.region, result.platform, result.puuid

    async def _probe_queue(
        self, client: LcuClientAdapter, report: ProbeReport
    ) -> None:
        report.queue, report.lcu_rank = await self.ranked_probe.probe_queue(client)

    async def _probe_rank_by_puuid(
        self,
        client: LcuClientAdapter,
        puuid: str | None,
        report: ProbeReport,
    ) -> None:
        await self.ranked_probe.probe_rank_by_puuid(client, puuid, report.lcu_rank)

    async def _probe_apex(
        self, client: LcuClientAdapter, report: ProbeReport
    ) -> None:
        report.apex = await self.ranked_probe.probe_apex(client)

    def _write_reports(self, report: ProbeReport, seed_method: str) -> None:
        scan = scan_tree(self.output_dir)
        report.security = {
            "TOKEN_PERSISTENCE_SCAN": (
                CapabilityStatus.SUPPORTED.value
                if scan.passed
                else CapabilityStatus.FAILED.value
            ),
            "FINDING_COUNT": len(scan.findings),
            "TOKENS_MEMORY_ONLY": CapabilityStatus.SUPPORTED.value,
        }
        data = report.to_json()
        data["PRIMARY_SEED_METHOD"] = seed_method
        self.named_artifacts.write_named_json("capability-report-v2.json", data)
        self.named_artifacts.write_named_json("capability-report.json", data)
        self.named_artifacts.write_named_json(
            "schema-profile.json",
            {"fingerprints": {"queue": report.queue.get("QUEUE_SCHEMA_HASH")}},
        )
        self.named_artifacts.write_named_json("queue-profile.json", report.queue)
        (self.output_dir / "phase0-diagnostics.md").write_text(
            render_phase0_diagnostics(data), encoding="utf-8"
        )


def _set_phase0_defaults(report: ProbeReport) -> None:
    report.replay = {
        "REPLAY_METADATA": CapabilityStatus.NOT_TESTED.value,
        "REPLAY_STREAM_AVAILABLE": CapabilityStatus.NOT_TESTED.value,
    }
    report.resilience = {
        "CLIENT_RECONNECT": CapabilityStatus.NOT_TESTED.value,
        "SESSION_REFRESH": CapabilityStatus.NOT_TESTED.value,
        "TASK_RESUME": CapabilityStatus.NOT_TESTED.value,
    }


def _set_blocked(report: ProbeReport) -> None:
    blocked = CapabilityStatus.BLOCKED_BY_DEPENDENCY.value
    not_tested = CapabilityStatus.NOT_TESTED.value
    report.client = {
        "LCU_CONNECTED": False,
        "LCU_CONNECTION_STATUS": CapabilityStatus.FAILED.value,
        "REGION": None,
        "RSO_PLATFORM_ID": None,
        "RSO_PLATFORM_ID_STATUS": blocked,
        "CURRENT_PUUID_AVAILABLE": None,
        "CURRENT_PUUID_STATUS": blocked,
    }
    report.queue = {
        "QUEUE_METADATA": blocked,
        "SOLO_RANKED_QUEUE_STATUS": blocked,
        "SOLO_RANKED_QUEUE_CONFIRMED": None,
        "SOLO_RANKED_QUEUE_ID": None,
        "QUEUE_SCHEMA_HASH": None,
    }
    report.apex = {
        tier: {"capability_status": blocked, "pagination_status": "UNKNOWN"}
        for tier in ("MASTER", "GRANDMASTER", "CHALLENGER")
    }
    report.lcu_rank = {"LCU_RANK_CURRENT": blocked, "LCU_RANK_BY_PUUID": blocked}
    report.sgp = {
        "SGP_SERVER_ID": None,
        "SGP_SERVER_CONFIG": blocked,
        "SGP_MATCH_HISTORY": blocked,
        "SGP_COMMON": blocked,
        "SGP_RANKED": blocked,
        "ENTITLEMENTS_READY": not_tested,
        "LEAGUE_SESSION_READY": not_tested,
    }
    report.history = {"THREE_PAGE_PAGINATION": blocked, "PAGINATION_STATUS": "UNKNOWN"}
    report.match = {"SUMMARY": blocked, "DETAILS": blocked}
    report.rank = {"TEN_PLAYER_RANK_COVERAGE": blocked, "TEN_PLAYER_RANK_COVERAGE_COUNT": 0}


def _seed_method(apex: dict[str, JsonObject]) -> str:
    supported = all(
        apex.get(tier, {}).get("capability_status")
        == CapabilityStatus.SUPPORTED.value
        for tier in ("MASTER", "GRANDMASTER", "CHALLENGER")
    )
    return "LCU_APEX" if supported else "UNRESOLVED"
