from __future__ import annotations

import copy
from dataclasses import replace
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest

from cti_app.api.investigation import _operator_status_tool_inventory
from cti_app.application.confined_analysis import ConfinedAnalysisFamilyStatus
from cti_app.application.investigation_composition import (
    InvestigationToolDependencies,
    build_investigation_composition,
)
from cti_app.application.investigation_contracts import ArticleContextV1
from cti_app.application.investigation_persistence import (
    InMemoryAnalystOpinionRepository,
    InMemoryEvidenceObservationRepository,
    InMemoryFindingRepository,
    InMemoryInvestigationEnvelopeRepository,
    InMemorySampleAssociationRepository,
    InMemoryToolInvocationRepository,
)
from cti_app.application.investigation_state import (
    InMemoryInvestigationStateRepository,
    InvestigationWorkingState,
)
from cti_app.application.investigation_tools import (
    InMemoryToolRegistry,
    RegistryToolDispatcher,
    ToolConfidentiality,
    ToolDefinition,
    ToolEffect,
    ToolExecutionContext,
    ToolResult,
    project_tool_availability,
)
from cti_app.application.local_analysis import LocalCommandCatalog, default_local_command_specs
from cti_app.application.research_context_builder import ArticleSnapshot
from cti_app.application.virustotal import (
    VirusTotalCapabilities,
    VirusTotalOperationRoute,
    VirusTotalRouteStep,
    VirusTotalRoutingPolicy,
)
from cti_app.config import Settings
from cti_app.domain.external_effects import (
    EvidenceReference,
    ExternalEffectAuthority,
    ExternalEffectAuthorityKind,
)
from cti_app.domain.investigation import (
    AgendaItem,
    BudgetDimension,
    DisclosureBounds,
    InvestigationAgenda,
    InvestigationBudget,
    InvestigationEnvelope,
    TLPLevel,
    ToolInvocationStatus,
)
from cti_app.domain.virustotal import (
    VirusTotalCapability,
    VirusTotalEndpointVariant,
    VirusTotalTransportKind,
)


class _ArticleSnapshots:
    def __init__(self, snapshot=None) -> None:
        self.snapshot = snapshot

    async def resolve(self, _synthesis_artifact_id):
        if self.snapshot is not None:
            return self.snapshot
        return SimpleNamespace(claims=())

    async def get_claims(self, _investigation_id, claim_ids=()):
        del claim_ids
        return ()


class _FeaturePacks:
    async def get_feature_pack(self, _sample_sha256):
        return None


class _Adapter:
    def __init__(self) -> None:
        self.calls = 0

    async def execute(self, _arguments, _context):
        self.calls += 1
        return ToolResult(ToolInvocationStatus.SUCCEEDED, output={"ok": True})


class _VirusTotalPort:
    async def object_report(self, *_args, **_kwargs):
        raise AssertionError("The test only inspects registration and prompt context")

    async def behaviour_pcap(self, *_args, **_kwargs):
        raise AssertionError("The test only inspects registration and prompt context")


class _VirusTotalObservations:
    async def store_behaviour_pcap(self, *_args, **_kwargs):
        raise AssertionError("The test only inspects registration and prompt context")


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
    )


@pytest.mark.asyncio
async def test_production_composition_builds_osint_policy_advisor_binding_offline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cti_app.application import bridge_investigator as bridge_module
    from cti_app.application.bridge_investigator import (
        TurnSubmission,
        TurnSubmissionStatus,
        submit_bridge_consultation_once,
    )
    from cti_app.application.model_gateway import BridgeExternalEffectBinding
    from cti_app.infrastructure.investigation_runtime_ports import (
        build_investigation_runtime_composition_factory,
    )
    from tests.test_investigation_runtime_ports import _runtime_inputs

    settings = Settings(
        _env_file=None,
        openai_bridge_base_url="https://bridge.test/v1",
    )
    runtime_inputs = _runtime_inputs(settings)
    runtime_inputs["external_effect_coordinator"] = cast(Any, object())
    runtime = build_investigation_runtime_composition_factory(**runtime_inputs)
    composition = runtime.composition_factory.build(_uow(), uuid4(), operator_mode=True)

    assert composition.advisor_tool is not None
    transport = composition.advisor_tool._transport
    submitted: dict[str, Any] = {}

    class NoWaitPacer:
        async def wait_for_next_submission(self):
            return True

    async def fake_external_submission(**kwargs: Any) -> TurnSubmission:
        submitted.update(kwargs)
        return TurnSubmission(TurnSubmissionStatus.SUCCEEDED, output_text="synthetic")

    monkeypatch.setattr(bridge_module, "_shared_bridge_pacer", lambda **_kwargs: NoWaitPacer())
    monkeypatch.setattr(transport, "submit", fake_external_submission)
    subject_id = uuid4()
    authority = ExternalEffectAuthority(
        investigation_id=uuid4(),
        invocation_id=uuid4(),
        operation_key="offline-advisor-binding",
        reservation_amounts={
            "bridge_turns": 1,
            "tool_seconds": 120,
            "context_bytes": 4096,
            "context_tokens": 1024,
        },
        expected_envelope_version=1,
        approval_decision_id=uuid4(),
        invocation_arguments_sha256="e" * 64,
        authority_kind=ExternalEffectAuthorityKind.OSINT_POLICY,
        policy_event_id=uuid4(),
        policy_version="offline-policy-v1",
        pinned_subject_id=subject_id,
    )
    evidence_refs = (
        EvidenceReference(
            kind="evidence_observation",
            id=str(uuid4()),
            version="offline-observation-v1",
            sha256="f" * 64,
        ),
    )

    result = await submit_bridge_consultation_once(
        transport,
        investigation_id=authority.investigation_id,
        subject_id=subject_id,
        turn_index=1,
        context_sha256="c" * 64,
        prompt="Synthetic advisor request.",
        external_llm_allowed=True,
        settings=settings,
        external_effect_binding_context={
            "authority_context": authority,
            "evidence_refs": evidence_refs,
            "invocation_arguments": {"consultation_type": "review_text"},
            "reservation_operation_key": authority.operation_key,
        },
    )

    binding = submitted.get("bridge_external_effect_binding")
    assert result.status is TurnSubmissionStatus.SUCCEEDED
    assert isinstance(binding, BridgeExternalEffectBinding)
    assert binding.authority is authority
    assert binding.family == "advisor.consultation.v1"
    assert binding.evidence_refs == evidence_refs


def test_composition_keeps_built_in_local_capability_without_optional_ports() -> None:
    investigation_id = uuid4()
    composition = build_investigation_composition(
        _uow(),
        uow_factory=lambda: None,
        artifact_store=SimpleNamespace(),
        model_conversations=SimpleNamespace(),
        settings=Settings.model_construct(),
        investigation_id=investigation_id,
        tool_dependencies=InvestigationToolDependencies(
            investigation_id=investigation_id,
            article_snapshots=_ArticleSnapshots(),
            observations=InMemoryEvidenceObservationRepository(),
            findings=InMemoryFindingRepository(),
            sample_associations=InMemorySampleAssociationRepository(),
            feature_packs=_FeaturePacks(),
            artifact_store=SimpleNamespace(),
            available_capabilities=frozenset(),
        ),
    )

    assert set(composition.available_tools) == {
        "article.get_claims",
        "evidence.get",
        "graph.neighbors",
        "network.protocol.document",
        "network.protocol.render",
        "sample.get_feature_pack",
        "sample.set_qualification",
    }
    assert "sample.analyze" in composition.unavailable_tools
    assert composition.available_capabilities == frozenset(
        {
            "investigation.local_read",
            "network.protocol.document",
            "network.protocol.render",
        }
    )
    assert all(
        not name.startswith(("vt.", "shodan.", "rule.")) for name in composition.available_tools
    )


@pytest.mark.asyncio
async def test_vt_registry_operator_status_and_prompt_share_the_same_tool_names() -> None:
    investigation_id = uuid4()
    synthesis_id = uuid4()
    snapshot = ArticleSnapshot(
        subject_id=uuid4(),
        production_run_id=uuid4(),
        synthesis_artifact_id=synthesis_id,
        synthesis_sha256="a" * 64,
        article=ArticleContextV1(
            short_summary="A bounded investigation context.",
            claims=(),
            documented_samples=(),
            known_chain=(),
            open_questions=(),
        ),
        tlp=TLPLevel.GREEN.value,
        do_not_submit=False,
        external_llm_allowed=True,
    )
    artifact_store = SimpleNamespace(read_json=lambda _blob_id: {})
    route = VirusTotalOperationRoute(
        primary=VirusTotalRouteStep(
            transport=VirusTotalTransportKind.PROXY,
            variant=VirusTotalEndpointVariant.V3,
        )
    )
    capabilities = VirusTotalCapabilities(file_report=True, behaviour_pcap=True)
    worker_families = {
        name: ConfinedAnalysisFamilyStatus(name, True, f"{name}-test")
        for name in (
            "sample_analysis",
            "local_command",
            "yara",
            "floss",
            "mwcp",
            "suricata",
        )
    }
    analysis_worker = SimpleNamespace(
        available=True,
        available_reason=None,
        family_statuses=worker_families,
    )
    dependencies = InvestigationToolDependencies(
        investigation_id=investigation_id,
        article_snapshots=_ArticleSnapshots(snapshot),
        observations=InMemoryEvidenceObservationRepository(),
        findings=InMemoryFindingRepository(),
        sample_associations=InMemorySampleAssociationRepository(),
        feature_packs=_FeaturePacks(),
        artifact_store=artifact_store,
        virustotal=_VirusTotalPort(),
        virustotal_observations=_VirusTotalObservations(),
        vt_capabilities=capabilities,
        vt_routing_policy=VirusTotalRoutingPolicy(
            routes={
                VirusTotalCapability.FILE_REPORT: route,
                VirusTotalCapability.BEHAVIOUR_PCAP: route,
            }
        ),
        confined_analysis_execution=analysis_worker,
        local_commands=SimpleNamespace(
            estimate_budget=lambda _command_id: SimpleNamespace(tool_seconds=1)
        ),
        local_command_catalog=LocalCommandCatalog(
            specs=tuple(
                replace(spec, requires_filesystem_confinement=True)
                for spec in default_local_command_specs()
            ),
            which=lambda binary: f"/fake/{binary}",
        ),
        rule_workbench=SimpleNamespace(),
        rule_corpus_bytes=SimpleNamespace(),
        yara_engine=SimpleNamespace(available=True),
        network_quarantine=SimpleNamespace(read_quarantined_capture=lambda *_args, **_kwargs: None),
        tshark=SimpleNamespace(available=True, execution=analysis_worker),
    )
    settings = Settings.model_construct(
        virustotal_proxy_url="http://127.0.0.1:9000",
        virustotal_base_url="http://127.0.0.1:9000/api/v3",
        virustotal_file_report_enabled=True,
        virustotal_behaviour_pcap_enabled=True,
        analysis_worker_enabled=True,
        local_analysis_enabled=True,
        rule_workbench_enabled=True,
        ghidra_enabled=False,
    )
    uow = _uow()
    uow.detection_candidates = SimpleNamespace()
    composition = build_investigation_composition(
        uow,
        uow_factory=lambda: None,
        artifact_store=artifact_store,
        model_conversations=SimpleNamespace(),
        settings=settings,
        investigation_id=investigation_id,
        tool_dependencies=dependencies,
    )
    budget = InvestigationBudget(limits={})
    envelope = InvestigationEnvelope(
        investigation_id=investigation_id,
        allowed_providers=("vt",),
        budget=budget,
        acquire_hits_allowed=False,
        disclosure=DisclosureBounds(tlp=TLPLevel.GREEN, external_llm_allowed=True),
        approval_decision_id=uuid4(),
        approval_actor_id="analyst:test",
    )
    tool_context = ToolExecutionContext(
        investigation_id=investigation_id,
        envelope=envelope,
        budget=budget,
        idempotency_key="vt-capability-view-test",
        available_capabilities=composition.available_capabilities,
    )
    state = InvestigationWorkingState(
        investigation_id=investigation_id,
        agenda=InvestigationAgenda(
            items=(
                AgendaItem(question="Inspect network indicators.", potential=1.0, rank=1),
                AgendaItem(question="Review sandbox evidence.", potential=0.9, rank=2),
            )
        ),
    )
    prompt = await composition.service._context_builder.build(
        investigation_id=investigation_id,
        synthesis_artifact_id=synthesis_id,
        state=state,
        tool_context=tool_context,
        budget_remaining={},
        registry=composition.registry,
    )

    tool_availability = project_tool_availability(composition.registry, tool_context)
    operator_inventory = await _operator_status_tool_inventory(
        state=state,
        composition=composition,
        envelope=envelope,
        invocations=(),
        observations=(),
    )
    registry_names = {item.name for item in composition.registry.list_available(tool_context)}
    operator_status_names = set(tool_availability.available_names)
    prompt_names = {item.name for item in prompt.available_tools}
    composition_names = set(composition.available_tools)
    assert registry_names == operator_status_names == prompt_names == composition_names
    assert operator_inventory == (
        tuple(sorted(prompt_names)),
        prompt.unavailable_tools,
    )
    assert {
        "sample.analyze",
        "local.run_command",
        "rule.propose",
        "rule.test",
        "rule.revise",
        "rule.compare",
        "network.capture.inspect",
        "network.suricata_replay",
        "network.suricata_validate_rules",
        "vt.file_report",
        "vt.object_report",
        "vt.behaviour_pcap",
    } <= registry_names
    assert "vt.behaviour" in composition.unavailable_tools
    assert "vt.behaviour:virustotal_unavailable" in prompt.unavailable_tools
    assert "sample.extract_obfuscated_strings:floss_unavailable" in prompt.unavailable_tools
    assert "sample.extract_configuration:configuration_extraction_unavailable" in (
        prompt.unavailable_tools
    )

    denied = await composition.service._dispatcher.dispatch(
        tool_context,
        tool_name="vt.behaviour",
        schema_version="1",
        arguments={},
        purpose="Check an unavailable capability is rejected by the dispatcher.",
        operation_key="unavailable-vt-behaviour",
    )
    assert denied.status is ToolInvocationStatus.UNAVAILABLE
    assert "virustotal_unavailable" in denied.message


@pytest.mark.asyncio
async def test_attested_worker_controls_analysis_tool_prompt_registry_and_operator_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cti_app.infrastructure import investigation_runtime_ports as runtime_ports
    from cti_app.infrastructure.investigation_runtime_ports import (
        build_investigation_runtime_composition_factory,
    )
    from tests.confined_worker_harness import build_confined_worker_harness
    from tests.test_investigation_runtime_ports import _runtime_inputs

    investigation_id = uuid4()
    synthesis_id = uuid4()
    settings = Settings(
        _env_file=None,
        analysis_worker_enabled=True,
        local_analysis_enabled=True,
        rule_workbench_enabled=True,
    )
    runtime_inputs = _runtime_inputs(settings)
    worker = build_confined_worker_harness(
        settings=settings,
        uow_factory=runtime_inputs["uow_factory"],  # type: ignore[arg-type]
        artifact_store=runtime_inputs["artifact_store"],  # type: ignore[arg-type]
    )
    runtime_inputs["confined_analysis_execution"] = worker.port

    async def read_capture(*_args: object, **_kwargs: object) -> None:
        return None

    runtime_inputs["network_quarantine"] = SimpleNamespace(read_quarantined_capture=read_capture)

    class _UnconfinedRunner:
        filesystem_confined = False
        namespace_reason = "bwrap_userns_refused_by_kernel"

    monkeypatch.setattr(runtime_ports, "LocalCommandRunner", lambda **_kwargs: _UnconfinedRunner())
    runtime = build_investigation_runtime_composition_factory(**runtime_inputs)
    uow = _uow()
    uow.detection_candidates = SimpleNamespace()
    composition = runtime.composition_factory.build(uow, investigation_id)
    assert runtime.ports.optional_tool_ports.confined_analysis_execution is worker.port
    runtime_families = {item.family: item for item in runtime.ports.statuses}
    assert runtime_families["local_filesystem_confinement"].state.value == "unavailable"
    assert runtime_families["local_command_execution"].execution_mode == "analysis-worker"

    snapshot = ArticleSnapshot(
        subject_id=uuid4(),
        production_run_id=uuid4(),
        synthesis_artifact_id=synthesis_id,
        synthesis_sha256="b" * 64,
        article=ArticleContextV1(
            short_summary="A bounded worker capability test.",
            claims=(),
            documented_samples=(),
            known_chain=(),
            open_questions=(),
        ),
    )
    composition.service._context_builder.article_snapshots = _ArticleSnapshots(snapshot)
    state = InvestigationWorkingState(
        investigation_id=investigation_id,
        agenda=InvestigationAgenda(
            items=(
                AgendaItem(question="Inspect sample features.", potential=1.0, rank=1),
                AgendaItem(question="Compare related evidence.", potential=0.5, rank=2),
            )
        ),
    )
    tool_context = ToolExecutionContext(
        investigation_id=investigation_id,
        envelope=None,
        budget=InvestigationBudget(limits={}),
        idempotency_key="attested-worker-tool-parity",
        available_capabilities=composition.available_capabilities,
    )
    prompt = await composition.service._context_builder.build(
        investigation_id=investigation_id,
        synthesis_artifact_id=synthesis_id,
        state=state,
        tool_context=tool_context,
        budget_remaining={},
        registry=composition.registry,
    )
    tool_availability = project_tool_availability(composition.registry, tool_context)
    operator_inventory = await _operator_status_tool_inventory(
        state=state,
        composition=composition,
        envelope=None,
        invocations=(),
        observations=(),
    )
    registry_names = {item.name for item in composition.registry.list_available(tool_context)}
    prompt_names = {item.name for item in prompt.available_tools}
    assert registry_names == set(tool_availability.available_names) == prompt_names
    assert operator_inventory == (tuple(sorted(prompt_names)), prompt.unavailable_tools)

    expected_families = {
        "sample.analyze": "sample_analysis",
        "sample.extract_obfuscated_strings": "floss",
        "local.run_command": "local_command",
        "rule.propose": "yara",
        "rule.test": "yara",
        "rule.revise": "yara",
        "rule.compare": "yara",
        "rule.propose_from_candidates": "yara",
        "network.capture.inspect": "tshark",
        "network.suricata_replay": "suricata",
        "network.suricata_validate_rules": "suricata",
    }
    assert set(expected_families) <= set(composition.available_tools) | set(
        composition.unavailable_tools
    )
    for name, family in expected_families.items():
        worker_status = worker.attestation.families.get(family)
        if worker_status is not None and worker_status.available:
            assert name in composition.available_tools
        else:
            assert name in composition.unavailable_tools
            assert composition.unavailable_tool_reasons[name] == (
                worker_status.available_reason
                if worker_status is not None and worker_status.available_reason
                else worker.attestation.available_reason or f"{family}_unavailable"
            )

    unavailable_families = {
        family: ConfinedAnalysisFamilyStatus(
            family,
            False,
            status.version,
            f"{family}_missing_from_worker_probe",
        )
        for family, status in worker.attestation.families.items()
    }
    unavailable_port = copy.copy(worker.port)
    unavailable_port._attestation = replace(
        worker.attestation,
        families=unavailable_families,
    )
    unavailable_inputs = dict(runtime_inputs)
    unavailable_inputs["confined_analysis_execution"] = unavailable_port
    unavailable_runtime = build_investigation_runtime_composition_factory(**unavailable_inputs)
    unavailable_uow = _uow()
    unavailable_uow.detection_candidates = SimpleNamespace()
    unavailable_composition = unavailable_runtime.composition_factory.build(
        unavailable_uow, investigation_id
    )
    assert set(expected_families) <= set(unavailable_composition.unavailable_tools)
    for name, family in expected_families.items():
        assert unavailable_composition.unavailable_tool_reasons[name] == (
            f"{family}_missing_from_worker_probe"
        )

    unavailable_composition.service._context_builder.article_snapshots = _ArticleSnapshots(snapshot)
    unavailable_context = ToolExecutionContext(
        investigation_id=investigation_id,
        envelope=None,
        budget=InvestigationBudget(limits={}),
        idempotency_key="unattested-worker-tool-parity",
        available_capabilities=unavailable_composition.available_capabilities,
    )
    unavailable_prompt = await unavailable_composition.service._context_builder.build(
        investigation_id=investigation_id,
        synthesis_artifact_id=synthesis_id,
        state=state,
        tool_context=unavailable_context,
        budget_remaining={},
        registry=unavailable_composition.registry,
    )
    unavailable_tool_availability = project_tool_availability(
        unavailable_composition.registry, unavailable_context
    )
    unavailable_operator_inventory = await _operator_status_tool_inventory(
        state=state,
        composition=unavailable_composition,
        envelope=None,
        invocations=(),
        observations=(),
    )
    unavailable_registry_names = {
        item.name for item in unavailable_composition.registry.list_available(unavailable_context)
    }
    assert (
        unavailable_registry_names
        == set(unavailable_tool_availability.available_names)
        == {item.name for item in unavailable_prompt.available_tools}
    )
    assert unavailable_operator_inventory == (
        tuple(sorted(unavailable_registry_names)),
        unavailable_prompt.unavailable_tools,
    )
    for name, family in expected_families.items():
        assert f"{name}:{family}_missing_from_worker_probe" in unavailable_prompt.unavailable_tools


@pytest.mark.asyncio
async def test_local_tools_run_without_envelope_and_denied_external_tool_requests_permission() -> (
    None
):
    registry = InMemoryToolRegistry()
    local_adapter = _Adapter()
    external_adapter = _Adapter()
    common_schema = {"type": "object", "properties": {}, "additionalProperties": False}
    registry.register(
        ToolDefinition(
            name="article.get_claims",
            schema_version="1",
            input_schema=common_schema,
            effect=ToolEffect.LOCAL_READ,
            cost_model={},
            confidentiality=ToolConfidentiality.INTERNAL,
            limitations=("Persisted article context only.",),
            capability_key="investigation.local_read",
        ),
        local_adapter,
    )
    registry.register(
        ToolDefinition(
            name="vt.file_report",
            schema_version="1",
            input_schema=common_schema,
            effect=ToolEffect.EXTERNAL_READ,
            cost_model={BudgetDimension.VT_UNITS: 1},
            confidentiality=ToolConfidentiality.INTERNAL,
            limitations=("Provider response is bounded.",),
            capability_key="vt.file_report",
        ),
        external_adapter,
    )
    context = ToolExecutionContext(
        investigation_id=uuid4(),
        envelope=None,
        budget=InvestigationBudget(limits={BudgetDimension.VT_UNITS: 4}),
        idempotency_key="turn-1",
        available_capabilities=frozenset({"investigation.local_read", "vt.file_report"}),
    )
    dispatcher = RegistryToolDispatcher(registry)

    local = await dispatcher.dispatch(
        context,
        tool_name="article.get_claims",
        schema_version="1",
        arguments={},
        purpose="Read the verified article claim.",
        operation_key="local-read",
    )
    denied = await dispatcher.dispatch(
        context,
        tool_name="vt.file_report",
        schema_version="1",
        arguments={},
        purpose="Request an external provider report.",
        operation_key="external-report",
    )

    assert local.status is ToolInvocationStatus.SUCCEEDED
    assert local_adapter.calls == 1
    assert denied.status is ToolInvocationStatus.POLICY_DENIED
    assert denied.output["request_permission"] is True
    assert "article.get_claims" in denied.suggested_alternatives
    assert external_adapter.calls == 0
