from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from cti_app.application.investigation_composition import (
    InvestigationToolDependencies,
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
    InMemoryToolRegistry,
    RegistryToolDispatcher,
    ToolConfidentiality,
    ToolDefinition,
    ToolEffect,
    ToolExecutionContext,
    ToolResult,
)
from cti_app.config import Settings
from cti_app.domain.investigation import (
    BudgetDimension,
    InvestigationBudget,
    ToolInvocationStatus,
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


class _Adapter:
    def __init__(self) -> None:
        self.calls = 0

    async def execute(self, _arguments, _context):
        self.calls += 1
        return ToolResult(ToolInvocationStatus.SUCCEEDED, output={"ok": True})


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
        "sample.get_feature_pack",
        "sample.analyze",
        "sample.set_qualification",
    }
    assert composition.available_capabilities == frozenset({"investigation.local_read"})
    assert all(
        not name.startswith(("vt.", "shodan.", "rule.")) for name in composition.available_tools
    )


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
