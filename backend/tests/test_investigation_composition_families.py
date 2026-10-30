from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest

from cti_app.application.behavior_report_import import BehaviorReportImporter
from cti_app.application.investigation_composition import (
    OptionalInvestigationToolPorts,
    build_investigation_composition,
)
from cti_app.application.investigation_network_tools import network_tool_capabilities
from cti_app.application.investigation_persistence import (
    InMemoryAnalystOpinionRepository,
    InMemoryEvidenceObservationRepository,
    InMemoryFindingRepository,
    InMemoryInvestigationEnvelopeRepository,
    InMemorySampleAssociationRepository,
    InMemoryToolInvocationRepository,
)
from cti_app.application.investigation_protocol_tools import protocol_tool_capabilities
from cti_app.application.investigation_state import InMemoryInvestigationStateRepository
from cti_app.application.investigation_tools import ToolExecutionContext
from cti_app.application.local_analysis import LocalCommandCatalog
from cti_app.application.sample_similarity import SampleSimilarityService
from cti_app.application.virustotal import (
    VirusTotalCapabilities,
    VirusTotalOperationRoute,
    VirusTotalRouteStep,
    VirusTotalRoutingPolicy,
)
from cti_app.config import Settings
from cti_app.domain.investigation import InvestigationBudget, ToolInvocationStatus
from cti_app.domain.network_protocol import NetworkExtractionLimitsV1
from cti_app.domain.virustotal import (
    VirusTotalCapability,
    VirusTotalEndpointVariant,
    VirusTotalTransportKind,
)
from cti_app.infrastructure.tshark_adapter import TsharkAdapter, network_local_command_specs


class _ArticleSnapshots:
    async def resolve(self, _synthesis_artifact_id):
        return SimpleNamespace(claims=())

    async def get_claims(self, _investigation_id, claim_ids=()):
        del claim_ids
        return ()


class _FeaturePacks:
    async def get_feature_pack(self, _sample_sha256):
        return None


class _SimilarityService:
    async def compare(self, **_kwargs):
        raise AssertionError("composition tests do not execute similarity providers")


class _FlossPort:
    available = True
    version = "floss-test"

    async def extract(self, _sample_sha256: str, *, max_strings: int):
        del max_strings
        raise AssertionError("composition tests do not execute FLOSS")


class _ConfigurationPort:
    available = True

    def __init__(self) -> None:
        self.allowed_parsers = {"test.parser": "1.0"}

    async def extract(self, _sample_sha256: str, *, parser_id: str):
        del parser_id
        raise AssertionError("composition tests do not execute MWCP")


class _RetrohuntProvider:
    available = True

    async def submit_retrohunt(self, _request):
        raise AssertionError("composition tests do not contact a provider")

    async def retrohunt_status(self, _job_id: str):
        raise AssertionError("composition tests do not contact a provider")

    async def retrohunt_results(self, _job_id: str):
        raise AssertionError("composition tests do not contact a provider")


class _ConfinedReaRunner:
    available = True
    worker_confined = True
    dedicated_user = True
    network_disabled = True
    filesystem_confined = True
    no_host_privileges = True

    async def inspect(self, _request, *, timeout_seconds: float, max_response_bytes: int):
        del timeout_seconds, max_response_bytes
        raise AssertionError("composition tests do not execute native analysis")


class _Quarantine:
    async def read_quarantined_capture(self, _source_reference: str, *, max_bytes: int):
        del max_bytes
        raise AssertionError("composition tests do not read captures")


def _tshark(*, missing_binary: str | None = None, confined: bool = True) -> TsharkAdapter:
    limits = NetworkExtractionLimitsV1()
    catalog = LocalCommandCatalog(
        specs=network_local_command_specs(limits),
        which=lambda binary: None if binary == missing_binary else f"/fake/{binary}",
    )
    return TsharkAdapter(
        catalog=catalog,
        runner=cast(Any, SimpleNamespace(filesystem_confined=confined)),
        limits=limits,
    )


def _uow():
    return SimpleNamespace(
        analyst_investigations=SimpleNamespace(),
        investigation_states=InMemoryInvestigationStateRepository(),
        investigation_envelopes=InMemoryInvestigationEnvelopeRepository(),
        tool_invocations=InMemoryToolInvocationRepository(),
        evidence_observations=InMemoryEvidenceObservationRepository(),
        investigation_findings=InMemoryFindingRepository(),
        sample_associations=InMemorySampleAssociationRepository(),
        analyst_opinions=InMemoryAnalystOpinionRepository(),
        detection_candidates=SimpleNamespace(),
        retrohunt_reservations=SimpleNamespace(),
    )


def _proxy_routing_policy() -> VirusTotalRoutingPolicy:
    route = VirusTotalOperationRoute(
        primary=VirusTotalRouteStep(
            transport=VirusTotalTransportKind.PROXY,
            variant=VirusTotalEndpointVariant.V3,
        )
    )
    return VirusTotalRoutingPolicy(routes={VirusTotalCapability.RETROHUNT: route})


def _all_family_ports() -> OptionalInvestigationToolPorts:
    return OptionalInvestigationToolPorts(
        rule_workbench=cast(Any, SimpleNamespace()),
        rule_corpus_bytes=cast(Any, SimpleNamespace()),
        sample_similarity=cast(SampleSimilarityService, _SimilarityService()),
        floss_extraction=cast(Any, _FlossPort()),
        configuration_extraction=cast(Any, _ConfigurationPort()),
        behavior_report_importer=BehaviorReportImporter(),
        retrohunt_provider=cast(Any, _RetrohuntProvider()),
        vt_capabilities=VirusTotalCapabilities(retrohunt=True),
        vt_routing_policy=_proxy_routing_policy(),
        network_quarantine=cast(Any, _Quarantine()),
        tshark=_tshark(),
        confined_rea_runner=cast(Any, _ConfinedReaRunner()),
    )


def _settings(*, native_enabled: bool = True, provider: str = "rea") -> Settings:
    return Settings.model_construct(
        rule_workbench_enabled=True,
        local_analysis_enabled=True,
        native_analysis_enabled=native_enabled,
        native_analysis_provider=provider,
        native_analysis_rea_expected_version="rea-test",
        native_analysis_catalog_version="autowork-native-read.v1",
        native_analysis_timeout_seconds=30,
        native_analysis_max_response_bytes=64 * 1024,
        native_analysis_max_output_bytes=32 * 1024,
    )


def _build(
    ports: OptionalInvestigationToolPorts,
    *,
    settings: Settings | None = None,
    uow: Any | None = None,
    artifact_store: Any | None = None,
):
    investigation_id = uuid4()
    return build_investigation_composition(
        uow or _uow(),
        uow_factory=lambda: None,
        artifact_store=artifact_store or SimpleNamespace(),
        model_conversations=SimpleNamespace(),
        settings=settings or _settings(),
        investigation_id=investigation_id,
        optional_tool_ports=ports,
    )


FAMILY_TOOLS = frozenset(
    {
        "samples.compare",
        "features.common",
        "signatures.candidates",
        "features.explain",
        "rule.propose_from_candidates",
        "sample.extract_obfuscated_strings",
        "sample.extract_configuration",
        "sandbox.report.import",
        "vt.retrohunt.submit",
        "vt.retrohunt.status",
        "vt.retrohunt.results",
        "network.capture.inspect",
        "network.stream.reassemble",
        "network.messages.segment",
        "network.protocol.compare",
        "network.protocol.hypothesize",
        "network.protocol.verify",
        "network.protocol.render",
        "native.inspect",
    }
)
FAMILY_CAPABILITIES = frozenset(
    {
        "samples.compare",
        "features.common",
        "signatures.candidates",
        "features.explain",
        "rule.propose_from_candidates",
        "sample.extract_obfuscated_strings",
        "sample.extract_configuration",
        "sandbox.report.import",
        "vt.retrohunt",
        "network.capture.inspect",
        "network.stream.reassemble",
        "network.messages.segment",
        "network.protocol.compare",
        "network.protocol.hypothesize",
        "network.protocol.verify",
        "network.protocol.render",
        "native.inspect",
    }
)


def test_composition_registers_all_optional_families_with_fake_ports() -> None:
    composition = _build(_all_family_ports())

    assert FAMILY_TOOLS <= set(composition.available_tools)
    assert FAMILY_CAPABILITIES <= composition.available_capabilities
    native_registration = composition.registry.get("native.inspect", "1")
    assert native_registration is not None
    native_definition, _native_adapter = native_registration
    assert len(native_definition.input_schema["properties"]["operation"]["enum"]) == 13
    assert native_definition.limitations
    assert composition.registry.get("samples.compare", "1") is not None
    assert composition.registry.get("rule.propose_from_candidates", "1") is not None
    assert composition.registry.get("sample.extract_configuration", "1") is not None


def test_rule_get_is_registered_without_the_rule_workbench_when_read_ports_exist() -> None:
    class _ReadableArtifacts:
        async def read_json(self, _blob_id):
            return {}

    ports = replace(_all_family_ports(), rule_workbench=None, rule_corpus_bytes=None)
    settings = _settings()
    settings.rule_workbench_enabled = False

    composition = _build(ports, settings=settings, artifact_store=_ReadableArtifacts())

    assert "rule.get" in composition.available_tools
    assert "rule.propose" not in composition.available_tools
    assert "rule.test" not in composition.available_tools
    assert composition.registry.get("rule.propose", "1") is None


def test_default_rule_workbench_fails_closed_without_engine_and_candidate_ports() -> None:
    class _ReadableArtifacts:
        async def read_json(self, _blob_id):
            return {}

    ports = replace(_all_family_ports(), rule_workbench=None, rule_corpus_bytes=None)
    settings = _settings()
    assert settings.rule_workbench_enabled is True

    composition = _build(ports, settings=settings, artifact_store=_ReadableArtifacts())

    assert "rule.get" in composition.available_tools
    assert "rule.propose" not in composition.available_tools
    assert "rule.test" not in composition.available_tools
    assert composition.registry.get("rule.propose", "1") is None


@pytest.mark.asyncio
async def test_missing_rea_runner_does_not_disable_other_tool_families() -> None:
    composition = _build(replace(_all_family_ports(), confined_rea_runner=None))

    assert "native.inspect" not in composition.available_tools
    assert {
        "samples.compare",
        "rule.propose_from_candidates",
        "sample.extract_obfuscated_strings",
        "vt.retrohunt.status",
    } <= set(composition.available_tools)
    assert NETWORK_TOOL_NAMES | PROTOCOL_TOOL_NAMES <= set(composition.available_tools)
    assert "native.inspect" not in composition.available_capabilities
    registration = composition.registry.get("native.inspect", "1")
    assert registration is not None
    _definition, adapter = registration
    result = await adapter.execute(
        {},
        ToolExecutionContext(
            investigation_id=uuid4(),
            envelope=None,
            budget=InvestigationBudget(),
            idempotency_key="missing-rea-runner",
        ),
    )
    assert result.status is ToolInvocationStatus.UNAVAILABLE
    assert result.message == "rea_runner_not_configured"


@pytest.mark.asyncio
async def test_retrohunt_without_proxy_route_returns_unavailable_only_for_retrohunt() -> None:
    ports = replace(_all_family_ports(), vt_routing_policy=VirusTotalRoutingPolicy())
    composition = _build(ports)

    assert not {"vt.retrohunt.submit", "vt.retrohunt.status", "vt.retrohunt.results"}.intersection(
        composition.available_tools
    )
    assert "samples.compare" in composition.available_tools
    assert "native.inspect" in composition.available_tools
    registration = composition.registry.get("vt.retrohunt.status", "1")
    assert registration is not None
    _definition, adapter = registration
    result = await adapter.execute(
        {"reservation_id": str(uuid4())},
        ToolExecutionContext(
            investigation_id=uuid4(),
            envelope=None,
            budget=InvestigationBudget(),
            idempotency_key="composition-retrohunt-unavailable",
            available_capabilities=composition.available_capabilities,
        ),
    )
    assert result.status is ToolInvocationStatus.UNAVAILABLE


def test_absent_floss_and_mwcp_do_not_disable_report_import_or_other_families() -> None:
    ports = replace(
        _all_family_ports(),
        floss_extraction=None,
        configuration_extraction=None,
    )
    composition = _build(ports)

    assert "sample.extract_obfuscated_strings" not in composition.available_tools
    assert "sample.extract_configuration" not in composition.available_tools
    assert "sandbox.report.import" in composition.available_tools
    assert "samples.compare" in composition.available_tools
    assert "vt.retrohunt.status" in composition.available_tools
    assert "native.inspect" in composition.available_tools


def test_native_analysis_flag_is_off_by_default_even_with_confined_runner() -> None:
    settings = Settings.model_construct(
        local_analysis_enabled=True,
        native_analysis_provider="rea",
    )
    assert settings.native_analysis_enabled is False

    composition = _build(_all_family_ports(), settings=settings)

    assert "native.inspect" not in composition.available_tools
    assert "samples.compare" in composition.available_tools
    assert "sample.extract_configuration" in composition.available_tools


NETWORK_TOOL_NAMES = frozenset(
    {
        "network.capture.inspect",
        "network.stream.reassemble",
        "network.messages.segment",
    }
)
PROTOCOL_TOOL_NAMES = frozenset(
    {
        "network.protocol.compare",
        "network.protocol.hypothesize",
        "network.protocol.verify",
        "network.protocol.render",
        "network.protocol.document",
    }
)


def test_network_and_protocol_capabilities_are_exact() -> None:
    assert network_tool_capabilities(_tshark()) == NETWORK_TOOL_NAMES
    assert network_tool_capabilities(_tshark(missing_binary="tshark")) == {
        "network.stream.reassemble",
        "network.messages.segment",
    }
    assert network_tool_capabilities(_tshark(missing_binary="capinfos")) == {
        "network.stream.reassemble",
        "network.messages.segment",
    }
    assert network_tool_capabilities(_tshark(confined=False)) == {
        "network.stream.reassemble",
        "network.messages.segment",
    }
    assert protocol_tool_capabilities() == PROTOCOL_TOOL_NAMES


@pytest.mark.parametrize("missing_binary", ["tshark", "capinfos"])
def test_missing_network_binary_disables_only_capture_inspection(
    missing_binary: str,
) -> None:
    composition = _build(
        replace(_all_family_ports(), tshark=_tshark(missing_binary=missing_binary))
    )

    assert "network.capture.inspect" not in composition.available_tools
    assert {
        "network.stream.reassemble",
        "network.messages.segment",
        *PROTOCOL_TOOL_NAMES,
    } <= set(composition.available_tools)
    assert "samples.compare" in composition.available_tools
    assert "native.inspect" in composition.available_tools


def test_missing_filesystem_confinement_disables_only_capture_inspection() -> None:
    composition = _build(replace(_all_family_ports(), tshark=_tshark(confined=False)))

    assert "network.capture.inspect" not in composition.available_tools
    assert {
        "network.stream.reassemble",
        "network.messages.segment",
        *PROTOCOL_TOOL_NAMES,
    } <= set(composition.available_tools)
    assert "samples.compare" in composition.available_tools
    assert "native.inspect" in composition.available_tools


def test_protocol_family_is_absent_without_a_network_workbench() -> None:
    composition = _build(replace(_all_family_ports(), network_quarantine=None))

    assert not (NETWORK_TOOL_NAMES | PROTOCOL_TOOL_NAMES).intersection(
        composition.available_tools
    ) - {"network.protocol.document", "network.protocol.render"}
    assert {"network.protocol.document", "network.protocol.render"} <= set(
        composition.available_tools
    )
    assert "samples.compare" in composition.available_tools
    assert "native.inspect" in composition.available_tools
