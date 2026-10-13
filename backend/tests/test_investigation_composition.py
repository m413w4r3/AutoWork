from __future__ import annotations

from types import SimpleNamespace
from typing import cast
from uuid import UUID, uuid4

import pytest

from cti_app.application.bridge_investigator import TurnSubmission, TurnSubmissionStatus
from cti_app.application.investigation_composition import (
    OPTIONAL_CAPABILITIES,
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
    ToolConfidentiality,
    ToolDefinition,
    ToolEffect,
)
from cti_app.application.model_conversations import ModelConversationService
from cti_app.application.persistence import UnitOfWorkFactory
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.application.rule_workbench import RuleWorkbench
from cti_app.config import Settings
from cti_app.domain.investigation import BudgetDimension


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
        "sample.get_feature_pack",
        "sample.analyze",
        "sample.set_qualification",
    }
    assert composition.available_capabilities == frozenset({"investigation.local_read"})
    assert composition.service._no_progress_limit == 7
    assert composition.tool_dependencies.virustotal is None
    assert composition.tool_dependencies.shodan is None
    assert composition.tool_dependencies.ghidra is None
    assert composition.tool_dependencies.acquisition is None
    assert composition.tool_dependencies.rule_workbench is None
    assert {"rule.propose", "rule.test", "rule.get"}.issubset(set(composition.unavailable_tools))


def test_composition_injects_code_features_and_goodware_into_sample_analysis() -> None:
    investigation_id = uuid4()
    code_features = SimpleNamespace()
    goodware = SimpleNamespace()
    composition = build_investigation_composition(
        _unit_of_work(),
        uow_factory=cast(UnitOfWorkFactory, lambda: None),
        artifact_store=cast(ProductionArtifactStore, SimpleNamespace()),
        model_conversations=cast(ModelConversationService, SimpleNamespace()),
        settings=Settings.model_construct(
            local_analysis_enabled=True,
            virustotal_file_download_enabled=True,
            investigation_max_hits_acquired=1,
        ),
        investigation_id=investigation_id,
        optional_tool_ports=OptionalInvestigationToolPorts(
            acquisition=SimpleNamespace(),
            sample_static_analysis=SimpleNamespace(),
            sample_code_features=code_features,
            sample_goodware=goodware,
        ),
    )

    registered = composition.registry.get("sample.analyze", "1")
    assert registered is not None
    _definition, adapter = registered
    assert adapter._analyzer is not None
    assert adapter._analyzer._code_features is code_features
    assert adapter._analyzer._goodware is goodware


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
