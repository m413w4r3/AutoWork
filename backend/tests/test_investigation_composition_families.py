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
from cti_app.application.investigation_persistence import (
    InMemoryAnalystOpinionRepository,
    InMemoryEvidenceObservationRepository,
    InMemoryFindingRepository,
    InMemoryInvestigationEnvelopeRepository,
    InMemorySampleAssociationRepository,
    InMemoryToolInvocationRepository,
)
from cti_app.application.investigation_state import InMemoryInvestigationStateRepository
from cti_app.application.investigation_tools import ToolExecutionContext
from cti_app.application.sample_similarity import SampleSimilarityService
from cti_app.application.virustotal import (
    VirusTotalCapabilities,
    VirusTotalOperationRoute,
    VirusTotalRouteStep,
    VirusTotalRoutingPolicy,
)
from cti_app.config import Settings
from cti_app.domain.investigation import InvestigationBudget, ToolInvocationStatus
from cti_app.domain.native_analysis import NativeAnalysisProvider
from cti_app.domain.virustotal import (
    VirusTotalCapability,
    VirusTotalEndpointVariant,
    VirusTotalTransportKind,
)


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


class _NativePort:
    provider = NativeAnalysisProvider.REA
    available = True

    async def inspect(self, _request):
        raise AssertionError("composition tests do not execute native analysis")


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
        native_analysis_rea=cast(Any, _NativePort()),
    )


def _settings(*, native_enabled: bool = True, provider: str = "rea") -> Settings:
    return Settings.model_construct(
        rule_workbench_enabled=True,
        local_analysis_enabled=True,
        native_analysis_enabled=native_enabled,
        native_analysis_provider=provider,
    )


def _build(
    ports: OptionalInvestigationToolPorts,
    *,
    settings: Settings | None = None,
    uow: Any | None = None,
):
    investigation_id = uuid4()
    return build_investigation_composition(
        uow or _uow(),
        uow_factory=lambda: None,
        artifact_store=SimpleNamespace(),
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


def test_missing_rea_provider_does_not_disable_other_tool_families() -> None:
    composition = _build(replace(_all_family_ports(), native_analysis_rea=None))

    assert "native.inspect" not in composition.available_tools
    assert {
        "samples.compare",
        "rule.propose_from_candidates",
        "sample.extract_obfuscated_strings",
        "vt.retrohunt.status",
    } <= set(composition.available_tools)
    assert "native.inspect" not in composition.available_capabilities


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


def test_native_analysis_flag_is_off_by_default_even_with_rea_port() -> None:
    settings = Settings.model_construct(
        local_analysis_enabled=True,
        native_analysis_provider="rea",
    )
    assert settings.native_analysis_enabled is False

    ports = replace(_all_family_ports(), native_analysis_ghidra_mcp=None)
    composition = _build(ports, settings=settings)

    assert "native.inspect" not in composition.available_tools
    assert "samples.compare" in composition.available_tools
    assert "sample.extract_configuration" in composition.available_tools
