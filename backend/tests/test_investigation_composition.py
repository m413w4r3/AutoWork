from __future__ import annotations

from types import SimpleNamespace
from typing import cast
from uuid import UUID, uuid4

import pytest

from cti_app.application.bridge_investigator import (
    BridgeInvestigator,
    TurnSubmission,
    TurnSubmissionStatus,
)
from cti_app.application.investigation_composition import (
    OPTIONAL_CAPABILITIES,
    InvestigationCompositionFactory,
    InvestigationToolDependencies,
    OptionalInvestigationToolPorts,
    _PermissionAwareInvestigationTransport,
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
from cti_app.application.investigation_tools import (
    RegistryToolDispatcher,
    ToolConfidentiality,
    ToolDefinition,
    ToolEffect,
    ToolExecutionContext,
    ToolInvocationStatus,
    project_tool_availability,
)
from cti_app.application.model_conversations import ModelConversationService
from cti_app.application.persistence import UnitOfWorkFactory
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.application.rule_workbench import RuleWorkbench
from cti_app.config import Settings
from cti_app.domain.investigation import BudgetDimension, InvestigationBudget


class _ArticleSnapshots:
    async def resolve(self, _synthesis_artifact_id):
        raise AssertionError("the test only inspects the composed tool catalogue")

    async def get_claims(self, _investigation_id, claim_ids=()):
        del claim_ids
        return ()


class _FeaturePackPort:
    async def get_feature_pack(self, _sample_sha256):
        return None


class _RuleWorkbench:
    pass


class _OptionalAdapter:
    async def execute(self, _arguments, _context):
        raise AssertionError("the test does not execute registered tools")


class _BlockedTransport:
    async def submit(
        self,
        *,
        investigation_id: UUID,
        subject_id: UUID,
        turn_index: int,
        context_sha256: str,
        prompt: str,
        external_llm_allowed: bool,
    ) -> TurnSubmission:
        del investigation_id, subject_id, turn_index, context_sha256, prompt, external_llm_allowed
        return TurnSubmission(
            status=TurnSubmissionStatus.BLOCKED,
            idempotency_key="blocked-at-approval-gate",
        )


@pytest.mark.asyncio
async def test_permission_aware_transport_forwards_orphan_reconciliation() -> None:
    class _OrphanAwareTransport(_BlockedTransport):
        def __init__(self) -> None:
            self.reconciliation_calls: list[dict[str, object]] = []

        async def reconcile_orphaned_investigation_turn(self, **kwargs: object) -> str:
            self.reconciliation_calls.append(kwargs)
            return "orphaned_running_turn"

    underlying = _OrphanAwareTransport()
    bridge = BridgeInvestigator(_PermissionAwareInvestigationTransport(underlying))
    investigation_id = uuid4()
    disposition = await bridge.reconcile_orphaned_turn(
        investigation_id=investigation_id,
        turn_index=20,
        context_sha256="a" * 64,
        attempt=0,
        owner_job_terminal=True,
    )

    assert disposition == "orphaned_running_turn"
    assert underlying.reconciliation_calls == [
        {
            "investigation_id": investigation_id,
            "turn_index": 20,
            "context_sha256": "a" * 64,
            "attempt": 0,
            "owner_job_terminal": True,
        }
    ]


def _unit_of_work():
    observations = InMemoryEvidenceObservationRepository()
    findings = InMemoryFindingRepository()
    associations = InMemorySampleAssociationRepository()
    return SimpleNamespace(
        analyst_investigations=SimpleNamespace(),
        investigation_states=InMemoryInvestigationStateRepository(),
        investigation_envelopes=InMemoryInvestigationEnvelopeRepository(),
        tool_invocations=InMemoryToolInvocationRepository(),
        evidence_observations=observations,
        investigation_findings=findings,
        sample_associations=associations,
        analyst_opinions=InMemoryAnalystOpinionRepository(),
    )


def test_composition_announces_local_tools_and_only_configured_capabilities() -> None:
    investigation_id = uuid4()
    uow = _unit_of_work()
    settings = Settings.model_construct(investigation_no_progress_limit=7)
    optional_port = _RuleWorkbench()

    composition = build_investigation_composition(
        uow,
        uow_factory=cast(UnitOfWorkFactory, lambda: None),
        artifact_store=cast(ProductionArtifactStore, SimpleNamespace()),
        model_conversations=cast(ModelConversationService, SimpleNamespace()),
        settings=settings,
        investigation_id=investigation_id,
        tool_dependencies=cast(
            InvestigationToolDependencies,
            SimpleNamespace(
                investigation_id=investigation_id,
                article_snapshots=_ArticleSnapshots(),
                feature_packs=_FeaturePackPort(),
                virustotal=SimpleNamespace(),
                shodan=SimpleNamespace(),
                rule_workbench=optional_port,
                local_commands=None,
                ghidra=SimpleNamespace(),
                acquisition=SimpleNamespace(),
                available_capabilities=frozenset(),
            ),
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
    assert composition.available_capabilities == frozenset(
        {
            "investigation.local_read",
            "network.protocol.document",
            "network.protocol.render",
        }
    )
    assert composition.service._no_progress_limit == 7
    assert composition.tool_dependencies.virustotal is None
    assert composition.tool_dependencies.shodan is None
    assert composition.tool_dependencies.ghidra is None
    assert composition.tool_dependencies.acquisition is None
    assert composition.tool_dependencies.rule_workbench is None
    assert {"rule.propose", "rule.test", "rule.get"}.issubset(set(composition.unavailable_tools))


def test_composition_routes_sample_analysis_to_worker_when_local_analysis_is_disabled() -> None:
    investigation_id = uuid4()
    worker = SimpleNamespace(
        available=True,
        family_statuses={"sample_analysis": SimpleNamespace(available=True)},
    )
    composition = build_investigation_composition(
        _unit_of_work(),
        uow_factory=cast(UnitOfWorkFactory, lambda: None),
        artifact_store=cast(ProductionArtifactStore, SimpleNamespace()),
        model_conversations=cast(ModelConversationService, SimpleNamespace()),
        settings=Settings.model_construct(
            local_analysis_enabled=False,
            analysis_worker_enabled=True,
            virustotal_file_download_enabled=True,
            investigation_max_hits_acquired=1,
        ),
        investigation_id=investigation_id,
        optional_tool_ports=OptionalInvestigationToolPorts(
            acquisition=SimpleNamespace(),
            confined_analysis_execution=worker,
        ),
    )

    registered = composition.registry.get("sample.analyze", "1")
    assert registered is not None
    _definition, adapter = registered
    assert adapter._analyzer is not None
    assert adapter._analyzer._confined_analysis is worker
    assert adapter._analyzer._static_analysis is None


def test_tool_registrar_receives_dependencies_and_can_extend_registry() -> None:
    investigation_id = uuid4()
    uow = _unit_of_work()
    settings = Settings.model_construct(rule_workbench_enabled=True)
    seen = []
    definition = ToolDefinition(
        name="rule_workbench.inspect",
        schema_version="1",
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        effect=ToolEffect.LOCAL_COMPUTE,
        cost_model={BudgetDimension.TOOL_SECONDS: 1},
        confidentiality=ToolConfidentiality.INTERNAL,
        limitations=("Local rule analysis.",),
        capability_key=OPTIONAL_CAPABILITIES["rule_workbench"],
    )

    def register(registry, dependencies):
        seen.append(dependencies)
        registry.register(definition, _OptionalAdapter())

    composition = build_investigation_composition(
        uow,
        uow_factory=cast(UnitOfWorkFactory, lambda: None),
        artifact_store=cast(ProductionArtifactStore, SimpleNamespace()),
        model_conversations=cast(ModelConversationService, SimpleNamespace()),
        settings=settings,
        investigation_id=investigation_id,
        optional_tool_ports=OptionalInvestigationToolPorts(
            rule_workbench=cast(RuleWorkbench, _RuleWorkbench()),
            available_capabilities=frozenset({OPTIONAL_CAPABILITIES["rule_workbench"]}),
        ),
        tool_registrar=register,
    )

    assert len(seen) == 1
    assert seen[0] is composition.tool_dependencies
    assert composition.tool_dependencies.rule_workbench is not None
    assert "rule_workbench.inspect" in composition.available_tools
    assert OPTIONAL_CAPABILITIES["rule_workbench"] in composition.available_capabilities


@pytest.mark.asyncio
async def test_operator_inventory_capabilities_match_the_dispatcher() -> None:
    investigation_id = uuid4()
    composition = build_investigation_composition(
        _unit_of_work(),
        uow_factory=cast(UnitOfWorkFactory, lambda: None),
        artifact_store=cast(ProductionArtifactStore, SimpleNamespace()),
        model_conversations=cast(ModelConversationService, SimpleNamespace()),
        settings=Settings.model_construct(),
        investigation_id=investigation_id,
        turn_transport=_BlockedTransport(),
        operator_mode=True,
    )
    context = ToolExecutionContext(
        investigation_id=investigation_id,
        envelope=None,
        budget=InvestigationBudget(limits={}),
        idempotency_key="operator-tool-parity",
        available_capabilities=composition.available_capabilities,
    )
    inventory = project_tool_availability(composition.registry, context)
    expected = {
        "finding.propose",
        "finding.revise",
        "opinion.compose",
        "opinion.revise",
        "opinion.note",
        "advisor.consult",
    }
    assert expected.issubset(set(inventory.available_names))
    assert composition.service.available_capabilities == composition.available_capabilities

    dispatcher = RegistryToolDispatcher(composition.registry)
    definitions = {
        definition.name: definition for definition in composition.registry.list_registered()
    }
    for name in expected:
        definition = definitions[name]
        assert definition.capability_key in composition.service.available_capabilities
        result = await dispatcher.dispatch(
            context,
            tool_name=name,
            schema_version=definition.schema_version,
            arguments={},
            purpose="availability parity check",
            operation_key=f"operator-tool-parity:{name}",
        )
        assert result.status is not ToolInvocationStatus.UNAVAILABLE


@pytest.mark.asyncio
async def test_advisor_inventory_and_dispatch_explain_missing_bridge_or_disclosure() -> None:
    investigation_id = uuid4()

    def compose(*, transport=None):
        return build_investigation_composition(
            _unit_of_work(),
            uow_factory=cast(UnitOfWorkFactory, lambda: None),
            artifact_store=cast(ProductionArtifactStore, SimpleNamespace()),
            model_conversations=cast(ModelConversationService, SimpleNamespace()),
            settings=Settings.model_construct(),
            investigation_id=investigation_id,
            turn_transport=transport,
            operator_mode=True,
        )

    missing_bridge = compose()
    context = ToolExecutionContext(
        investigation_id=investigation_id,
        envelope=None,
        budget=InvestigationBudget(limits={}),
        idempotency_key="advisor-no-bridge",
        available_capabilities=missing_bridge.available_capabilities,
    )
    missing_bridge_inventory = project_tool_availability(missing_bridge.registry, context)
    assert "advisor.consult" not in missing_bridge_inventory.available_names
    assert "advisor.consult:bridge_transport_unavailable" in (
        missing_bridge_inventory.unavailable_diagnostics
    )

    disclosed_transport = compose(transport=_BlockedTransport())
    blocked_context = ToolExecutionContext(
        investigation_id=investigation_id,
        envelope=None,
        budget=InvestigationBudget(limits={}),
        idempotency_key="advisor-disclosure-denied",
        available_capabilities=disclosed_transport.available_capabilities,
        blocked_capabilities=("bridge.advisor:external_llm_disclosure_denied",),
    )
    blocked_inventory = project_tool_availability(disclosed_transport.registry, blocked_context)
    assert "advisor.consult" not in blocked_inventory.available_names
    assert "advisor.consult:external_llm_disclosure_denied" in (
        blocked_inventory.unavailable_diagnostics
    )
    definition = next(
        item
        for item in disclosed_transport.registry.list_registered()
        if item.name == "advisor.consult"
    )
    result = await RegistryToolDispatcher(disclosed_transport.registry).dispatch(
        blocked_context,
        tool_name=definition.name,
        schema_version=definition.schema_version,
        arguments={},
        purpose="advisor availability parity",
        operation_key="advisor-disclosure-denied",
    )
    assert result.status is ToolInvocationStatus.UNAVAILABLE


@pytest.mark.asyncio
async def test_blocked_model_turn_becomes_a_persistable_review_outcome() -> None:
    transport = _PermissionAwareInvestigationTransport(_BlockedTransport())

    result = await transport.submit(
        investigation_id=uuid4(),
        subject_id=uuid4(),
        turn_index=0,
        context_sha256="a" * 64,
        prompt="Safe test prompt.",
        external_llm_allowed=False,
    )

    assert result.status is TurnSubmissionStatus.NEEDS_REVIEW
    assert result.idempotency_key == "blocked-at-approval-gate"


@pytest.mark.asyncio
async def test_api_factory_operator_writes_execute_through_live_composition_services() -> None:
    import hashlib
    from datetime import UTC, datetime

    from cti_app.application.investigation_persistence import (
        InMemoryEvidenceObservationRepository,
        InMemoryFindingRepository,
    )
    from cti_app.domain.investigation import (
        AgendaItem,
        EvidenceKind,
        EvidenceObservation,
        FindingAssessment,
        FindingKind,
        InvestigationAgenda,
        InvestigationExecutionMode,
    )
    from cti_app.domain.investigation_state import InvestigationWorkingState
    from cti_app.domain.publication_document import publication_document_text_anchors
    from tests.test_analyst_opinion_service import _base_v5, make_opinion_harness

    harness = make_opinion_harness()
    investigation_id = harness.investigation.id
    observations = InMemoryEvidenceObservationRepository()
    findings = InMemoryFindingRepository()
    observation = EvidenceObservation(
        investigation_id=investigation_id,
        kind=EvidenceKind.BINARY_ANALYSIS,
        blob_id=uuid4(),
        sha256=hashlib.sha256(b"measured operator fixture").hexdigest(),
        source="fixture-analysis",
        source_version="1",
        observed_at=datetime.now(UTC),
        object_type="sample",
        object_id="sample-a",
    )
    await observations.append_if_absent(observation)
    harness.uow.evidence_observations = observations
    harness.uow.investigation_findings = findings
    harness.uow._state.observations = observations
    harness.uow._state.findings = findings

    class SnapshotPort:
        def __init__(self) -> None:
            base = _base_v5()
            self.anchors = tuple(
                publication_document_text_anchors(
                    base.document,
                    references=base.references,
                )
            )

        async def resolve(self, _synthesis_artifact_id):
            return SimpleNamespace(
                subject_id=harness.investigation.subject_id,
                production_run_id=harness.investigation.production_run_id,
                synthesis_artifact_id=harness.investigation.synthesis_artifact_id,
                synthesis_sha256="b" * 64,
                article=SimpleNamespace(
                    publication_anchors=tuple(
                        SimpleNamespace(anchor=anchor) for anchor in self.anchors
                    ),
                    claims=(),
                ),
                tlp=None,
                do_not_submit=True,
                external_llm_allowed=False,
            )

        async def get_claims(self, _investigation_id, claim_ids=()):
            del claim_ids
            return ()

    snapshot = SnapshotPort()
    state_repository = InMemoryInvestigationStateRepository()
    invocation_repository = InMemoryToolInvocationRepository()
    associations = InMemorySampleAssociationRepository()
    no_envelope = SimpleNamespace(get=async_get_none)

    def api_uow_factory():
        unit = harness.uow_factory()
        unit.evidence_observations = observations
        unit.investigation_findings = findings
        unit.investigation_states = state_repository
        unit.tool_invocations = invocation_repository
        unit.sample_associations = associations
        unit.investigation_envelopes = no_envelope
        return unit

    harness.uow.investigation_states = state_repository
    harness.uow.tool_invocations = invocation_repository
    harness.uow.sample_associations = associations
    harness.uow.investigation_envelopes = no_envelope
    state = InvestigationWorkingState(
        investigation_id=investigation_id,
        agenda=InvestigationAgenda(
            items=(
                AgendaItem("Validate the measured capability", 0.9, 1),
                AgendaItem("Identify remaining gaps", 0.8, 2),
            )
        ),
        execution_mode=InvestigationExecutionMode.OPERATOR_LED,
    )
    await state_repository.save_if_version(state, expected_version=None)
    dependencies = InvestigationToolDependencies(
        investigation_id=investigation_id,
        article_snapshots=snapshot,
        observations=observations,
        findings=findings,
        sample_associations=associations,
        feature_packs=SimpleNamespace(get_feature_pack=async_get_none),
        artifact_store=harness.store,
    )
    factory = InvestigationCompositionFactory(
        uow_factory=api_uow_factory,
        artifact_store=harness.store,
        model_conversations=SimpleNamespace(),
        settings=Settings.model_construct(investigation_no_progress_limit=7),
        tool_dependencies=dependencies,
    )
    api_composition = factory.build(harness.uow, investigation_id, operator_mode=True)
    api_composition.operator_tool_invocations._observations = None
    api_composition.operator_tool_invocations._evidence_summaries = None
    assert {"finding.propose", "opinion.compose", "opinion.note"}.issubset(
        set(api_composition.available_tools)
    )

    proposed = await api_composition.operator_tool_invocations.invoke(
        tool_name="finding.propose",
        schema_version="1",
        arguments={
            "action": "propose",
            "kind": FindingKind.CAPABILITY.value,
            "assessment": FindingAssessment.SUPPORTED.value,
            "statement": "The measured sample exposes capability A.",
            "subject_ref": "sample-a",
            "observation_refs": [str(observation.id)],
            "claim_refs": [],
            "novelty": None,
            "scope": "static analysis only",
            "limitations": [],
        },
        purpose="validate operator write composition",
        expected_version=1,
        idempotency_key="factory-finding-propose",
    )
    assert proposed.invocation.status is ToolInvocationStatus.SUCCEEDED
    assert proposed.invocation.schema_version == "operator-finding.v1"
    finding_id = proposed.output["data"]["result"]["finding_id"]

    composed = await api_composition.operator_tool_invocations.invoke(
        tool_name="opinion.compose",
        schema_version="1",
        arguments={
            "action": "compose",
            "selection": {
                "finding_ids": [finding_id],
                "claim_ids": [],
                "block_types": ["key_findings"],
            },
        },
        purpose="compose an operator draft",
        expected_version=proposed.working_state_version,
        idempotency_key="factory-opinion-compose",
    )
    assert composed.invocation.status is ToolInvocationStatus.SUCCEEDED
    assert composed.invocation.schema_version == "operator-opinion.v1"
    opinion = composed.output["data"]["result"]

    noted = await api_composition.operator_tool_invocations.invoke(
        tool_name="opinion.note",
        schema_version="1",
        arguments={
            "action": "note",
            "anchor": next(
                anchor for anchor in snapshot.anchors if anchor.startswith(("lead:", "section:"))
            ),
            "state": "inferred",
            "relation": "qualifies",
            "text": "The measured result is limited to static analysis.",
            "expected_opinion_version": opinion["opinion_version"],
            "expected_opinion_sha256": opinion["content_sha256"],
            "finding_refs": [finding_id],
            "evidence_refs": [str(observation.id)],
        },
        purpose="add a cited operator note",
        expected_version=composed.working_state_version,
        idempotency_key="factory-opinion-note",
    )
    assert noted.invocation.status is ToolInvocationStatus.SUCCEEDED
    assert noted.invocation.schema_version == "operator-opinion.v1"


async def async_get_none(*_args, **_kwargs):
    return None
