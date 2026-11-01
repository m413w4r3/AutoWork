from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest

from cti_app.application.investigation_acquisition_tools import (
    ARCHIVE_EXTRACT_MEMBER_SCHEMA,
    SAMPLE_ANALYZE_SCHEMA,
    VT_DOWNLOAD_SAMPLE_SCHEMA,
    VT_FILE_DOWNLOAD_CAPABILITY,
)
from cti_app.application.investigation_contracts import ConfirmedIOCContextV1
from cti_app.application.investigation_operator_view import build_investigation_operator_view
from cti_app.application.investigation_rule_tools import RULE_PROPOSE_SCHEMA
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
from cti_app.application.investigation_vt_tools import (
    BEHAVIOUR_SCHEMA,
    FILE_REPORT_SCHEMA,
    INTELLIGENCE_SEARCH_SCHEMA,
    OBJECT_REPORT_SCHEMA,
    RELATIONSHIPS_SCHEMA,
    VT_PASSIVE_RETRY_TRUSTED_PARAMETER,
)
from cti_app.application.operator_tool_invocation import (
    OperatorToolInvocationError,
    OperatorToolInvocationService,
)
from cti_app.application.research_context_builder import blocked_capabilities_from_history
from cti_app.domain.classification import TLP
from cti_app.domain.entities import SourceDocument
from cti_app.domain.external_effects import invocation_arguments_digest
from cti_app.domain.investigation import (
    BudgetDimension,
    EnvelopeAuthorityKind,
    EvidenceKind,
    EvidenceObservation,
    FindingAssessment,
    FindingKind,
    InvestigationBudget,
    InvestigationExecutionMode,
    InvestigationFinding,
    SampleAssociation,
    SampleOrigin,
    ToolInvocation,
    ToolInvocationStatus,
    tool_operation_key,
)
from tests.test_investigation_service import _Fixture


class _RecordingTool:
    def __init__(self, name: str) -> None:
        self.name = name
        self.calls: list[dict[str, Any]] = []
        self.status = ToolInvocationStatus.SUCCEEDED
        self.started: asyncio.Event | None = None
        self.release: asyncio.Event | None = None

    async def execute(
        self, arguments: Mapping[str, Any], context: ToolExecutionContext
    ) -> ToolResult:
        self.calls.append(dict(arguments))
        if self.started is not None:
            self.started.set()
        if self.release is not None:
            await self.release.wait()
        return ToolResult(
            status=self.status,
            output={"summary": f"{self.name} accepted the synthetic request."},
        )


def test_passive_retry_trust_is_outside_arguments_but_bound_at_consume() -> None:
    investigation_id = uuid4()
    arguments = {"file_hash": "d" * 64}
    key = tool_operation_key(
        investigation_id=investigation_id,
        tool_name="vt.file_report",
        schema_version="1",
        normalized_parameters=arguments,
        purpose="same caller request",
    )
    invocation = ToolInvocation(
        investigation_id=investigation_id,
        operation_key=key,
        tool_name="vt.file_report",
        schema_version="1",
        normalized_parameters=arguments,
        passive_read_retry_trusted=True,
        purpose="same caller request",
    )

    assert dict(invocation.normalized_parameters) == arguments
    assert VT_PASSIVE_RETRY_TRUSTED_PARAMETER not in invocation.normalized_parameters
    assert invocation.operation_key == key
    assert invocation_arguments_digest(
        arguments, passive_read_retry_trusted=True
    ) != invocation_arguments_digest(arguments, passive_read_retry_trusted=False)


class _SeededEvidenceSummaries:
    def __init__(self, fixture: _Fixture) -> None:
        self.fixture = fixture
        self.payloads: dict[str, Mapping[str, Any]] = {}
        self.samples: tuple[Mapping[str, Any], ...] = ()

    async def summary(self, observation: EvidenceObservation) -> Mapping[str, Any] | None:
        return self.payloads.get(str(observation.id))

    async def sample_inventory(self, investigation_id: UUID) -> tuple[Mapping[str, Any], ...]:
        assert investigation_id == self.fixture.investigation_id
        return self.samples


def _schema_tool(
    name: str,
    schema: Mapping[str, Any],
    effect: ToolEffect,
    capability: str,
) -> tuple[ToolDefinition, _RecordingTool]:
    tool = _RecordingTool(name)
    cost_model = {BudgetDimension.VT_UNITS: 1} if effect is ToolEffect.EXTERNAL_READ else {}
    return (
        ToolDefinition(
            name=name,
            schema_version="1",
            input_schema=schema,
            effect=effect,
            cost_model=cost_model,
            confidentiality=ToolConfidentiality.INTERNAL,
            limitations=("Synthetic offline availability oracle.",),
            capability_key=capability,
        ),
        tool,
    )


async def _seeded_fixture() -> tuple[
    _Fixture,
    Any,
    InMemoryToolRegistry,
    _SeededEvidenceSummaries,
    OperatorToolInvocationService,
    dict[str, _RecordingTool],
    dict[str, Any],
]:
    fixture = _Fixture(responses=[], tools=[("vt.file_report", "vt")])
    fixture.envelope = replace(
        fixture.envelope,
        budget=InvestigationBudget(
            limits={
                BudgetDimension.BRIDGE_TURNS: 8,
                BudgetDimension.VT_UNITS: 100,
                BudgetDimension.DOWNLOADS: 20,
                BudgetDimension.SHODAN_CREDITS: 100,
            }
        ),
    )
    state = await fixture.start(execution_mode=InvestigationExecutionMode.OPERATOR_LED)

    sample_rows = tuple(
        {
            "association_id": str(uuid4()),
            "sample_id": str(uuid4()),
            "sha256": "d" * 64 if index == 1 else f"{index:064x}",
            "byte_size": 4096 + index,
            "detected_type": "application/octet-stream",
            "provenance": "vt_download",
            "qualification": "candidate",
        }
        for index in range(1, 16)
    )
    iocs = (
        ("md5", "b" * 32, "file"),
        ("sha1", "c" * 40, "file"),
        ("sha256", "d" * 64, "file"),
        ("ipv4", "198.51.100.77", "ip_address"),
        ("domain", "pivot-synthetic.example", "domain"),
        ("url", "https://pivot-synthetic.example/path", "url"),
    )
    fixture.snapshot = replace(
        fixture.snapshot,
        confirmed_iocs=tuple(
            ConfirmedIOCContextV1(
                indicator_type=kind,
                value=value,
                extraction_artifact_id=uuid4(),
                extraction_artifact_version=1,
                source_document_ids=(),
            )
            for kind, value, _object_type in iocs
        ),
    )
    fixture.context_builder.article_snapshots.snapshot = fixture.snapshot

    summaries = _SeededEvidenceSummaries(fixture)
    summaries.samples = sample_rows
    linked_observations: dict[str, EvidenceObservation] = {}

    async def add_observation(
        *,
        source: str,
        kind: EvidenceKind,
        object_type: str,
        object_id: str,
        limitations: tuple[str, ...] = (),
    ) -> EvidenceObservation:
        payload_hash = hashlib.sha256(f"{source}:{object_type}:{object_id}".encode()).hexdigest()
        observation = EvidenceObservation(
            investigation_id=fixture.investigation_id,
            kind=kind,
            blob_id=uuid4(),
            sha256=payload_hash,
            source=source,
            source_version="synthetic.v1",
            observed_at=datetime.now(UTC),
            object_type=object_type,
            object_id=object_id,
            limitations=limitations,
        )
        await fixture.observations.append_if_absent(observation)
        return observation

    for _kind, value, object_type in iocs:
        linked_observations[value] = await add_observation(
            source="article_evidence",
            kind=EvidenceKind.OTHER,
            object_type=object_type,
            object_id=value,
        )

    relationship_rows = (
        ("file", "d" * 64, "contacted_ips", "ip_address", "203.0.113.44"),
        ("file", "d" * 64, "contacted_domains", "domain", "child-synthetic.example"),
        ("ip_address", "203.0.113.44", "resolutions", "domain", "resolved-synthetic.example"),
    )
    for object_type, object_id, relation, child_type, child_id in relationship_rows:
        observation = await add_observation(
            source="VirusTotal",
            kind=EvidenceKind.VT_RELATIONSHIP,
            object_type=object_type,
            object_id=object_id,
            limitations=(f"relation={relation}",),
        )
        summaries.payloads[str(observation.id)] = {
            "schema": "virustotal-investigation-summary.v1",
            "relation": relation,
            "descriptors": [{"type": child_type, "identifier": child_id}],
        }

    # Two of the fifteen canonical sample associations have completed static analysis.
    for row in sample_rows[:2]:
        sample_hash = str(row["sha256"])
        sample_observation = await add_observation(
            source="autowork.analysis_worker.static",
            kind=EvidenceKind.BINARY_ANALYSIS,
            object_type="sample",
            object_id=sample_hash,
        )
        await fixture.invocations.add_if_absent(
            ToolInvocation(
                investigation_id=fixture.investigation_id,
                operation_key=f"seeded-sample-analysis:{row['association_id']}",
                tool_name="sample.analyze",
                schema_version="1",
                normalized_parameters={"association_id": row["association_id"]},
                purpose="Seed completed synthetic static analysis.",
                status=ToolInvocationStatus.SUCCEEDED,
                response_refs=(sample_observation.id,),
            )
        )

    finding_observation = linked_observations["198.51.100.77"]
    await fixture.findings.append_version(
        InvestigationFinding(
            investigation_id=fixture.investigation_id,
            kind=FindingKind.NETWORK_BEHAVIOUR,
            assessment=FindingAssessment.SUPPORTED,
            statement="Synthetic observation supports the article network claim.",
            observation_refs=(finding_observation.id,),
            claim_refs=("C1",),
            scope="Synthetic offline fixture.",
        ),
        expected_previous_id=None,
    )

    associations = tuple(
        SampleAssociation(
            investigation_id=fixture.investigation_id,
            sample_sha256=str(row["sha256"]),
            origin=SampleOrigin.VT_PIVOT,
        )
        for row in sample_rows
    )

    class _Associations:
        async def list_for_investigation(self, investigation_id: UUID):
            assert investigation_id == fixture.investigation_id
            return associations

    class _Samples:
        async def list_by_subject_and_sha256(self, subject_id: UUID, sample_sha256: str):
            assert subject_id == fixture.subject_id
            return ()

    fixture.service._sample_associations = _Associations()
    fixture.service._samples = _Samples()

    capabilities = frozenset(
        {
            "vt.read",
            "investigation.sample_analysis",
            "investigation.archive_extract_member",
            "rule.workbench",
        }
    )
    registry = InMemoryToolRegistry()
    schemas = (
        ("vt.file_report", FILE_REPORT_SCHEMA, ToolEffect.EXTERNAL_READ, "vt.read"),
        ("vt.object_report", OBJECT_REPORT_SCHEMA, ToolEffect.EXTERNAL_READ, "vt.read"),
        ("vt.relationships", RELATIONSHIPS_SCHEMA, ToolEffect.EXTERNAL_READ, "vt.read"),
        (
            "vt.intelligence_search",
            INTELLIGENCE_SEARCH_SCHEMA,
            ToolEffect.EXTERNAL_READ,
            "vt.read",
        ),
        ("vt.behaviour", BEHAVIOUR_SCHEMA, ToolEffect.EXTERNAL_READ, "vt.read"),
        (
            "sample.analyze",
            SAMPLE_ANALYZE_SCHEMA,
            ToolEffect.LOCAL_COMPUTE,
            "investigation.sample_analysis",
        ),
        (
            "archive.extract_member",
            ARCHIVE_EXTRACT_MEMBER_SCHEMA,
            ToolEffect.LOCAL_COMPUTE,
            "investigation.archive_extract_member",
        ),
        ("rule.propose", RULE_PROPOSE_SCHEMA, ToolEffect.LOCAL_COMPUTE, "rule.workbench"),
    )
    tools: dict[str, _RecordingTool] = {}
    for name, schema, effect, capability in schemas:
        definition, adapter = _schema_tool(name, schema, effect, capability)
        registry.register(definition, adapter)
        tools[name] = adapter

    fixture.registry = registry
    fixture.available_capabilities = capabilities
    fixture.service._registry = registry
    fixture.service._dispatcher = RegistryToolDispatcher(registry)
    fixture.service._available_capabilities = capabilities
    fixture.context_builder._registry = registry
    fixture.context_builder._evidence_summaries = summaries
    invocation_service = OperatorToolInvocationService(
        investigation_id=fixture.investigation_id,
        investigations=fixture.investigations,
        states=fixture.states,
        envelopes=fixture.envelopes,
        invocations=fixture.invocations,
        context_builder=fixture.context_builder,
        execution=fixture.service,
        registry=registry,
        available_capabilities=capabilities,
        unavailable_tool_reasons={},
        observations=fixture.observations,
        evidence_summaries=summaries,
    )
    identifiers = {
        "linked_observations": linked_observations,
        "sample_rows": sample_rows,
    }
    return fixture, state, registry, summaries, invocation_service, tools, identifiers


async def _build_context(
    fixture: _Fixture,
    state: Any,
    registry: InMemoryToolRegistry,
    *,
    envelope: Any = ...,
    osint_policy_authorized: bool | None = None,
):
    active_envelope = (
        await fixture.envelopes.get(fixture.investigation_id) if envelope is ... else envelope
    )
    budget = (
        active_envelope.budget
        if active_envelope is not None
        else InvestigationBudget(limits={BudgetDimension.VT_UNITS: 0})
    )
    capabilities = fixture.available_capabilities
    tool_context = ToolExecutionContext(
        investigation_id=fixture.investigation_id,
        envelope=active_envelope,
        budget=budget,
        idempotency_key="available-equals-executable-context",
        available_capabilities=capabilities,
        osint_policy_authorized=osint_policy_authorized,
    )
    return await fixture.context_builder.build(
        investigation_id=fixture.investigation_id,
        synthesis_artifact_id=fixture.snapshot.synthesis_artifact_id,
        state=state,
        tool_context=tool_context,
        budget_remaining={
            dimension.value: amount
            for dimension, amount in budget.remaining.items()
            if amount is not None
        },
        registry=registry,
    )


def _osint_policy_envelope(envelope: Any, *, providers: tuple[str, ...]) -> Any:
    event_id = uuid4()
    return replace(
        envelope,
        allowed_providers=providers,
        authority_kind=EnvelopeAuthorityKind.OSINT_POLICY,
        inherited_policy_id=uuid4(),
        inherited_policy_version=1,
        inherited_policy_event_id=event_id,
        approval_decision_id=event_id,
    )


def _register_advisor(registry: InMemoryToolRegistry, fixture: _Fixture) -> None:
    definition, adapter = _schema_tool(
        "advisor.consult",
        {"type": "object", "properties": {}, "additionalProperties": False},
        ToolEffect.EXTERNAL_READ,
        "bridge.advisor",
    )
    registry.register(definition, adapter)
    fixture.available_capabilities = frozenset((*fixture.available_capabilities, "bridge.advisor"))


@pytest.mark.asyncio
async def test_active_osint_policy_projection_matches_dispatch_gates() -> None:
    (
        fixture,
        state,
        registry,
        _summaries,
        operator_service,
        _tools,
        _identifiers,
    ) = await _seeded_fixture()
    _register_advisor(registry, fixture)
    envelope = _osint_policy_envelope(fixture.envelope, providers=("bridge", "vt"))
    fixture.envelope = envelope
    fixture.envelopes._items[fixture.investigation_id] = envelope

    context = await _build_context(
        fixture,
        state,
        registry,
        osint_policy_authorized=True,
    )

    available_names = {item.name for item in context.available_tools}
    assert "advisor.consult" in available_names
    assert {name for name in available_names if name.startswith("vt.")}
    vt_pivots = [item for item in context.available_pivots if item.tool_name.startswith("vt.")]
    assert vt_pivots
    assert all(
        item.execution_state == "available" or item.blocked_reason == "source_references_required"
        for item in vt_pivots
    ), [
        (item.tool_name, item.execution_state, item.blocked_reason)
        for item in vt_pivots
        if item.execution_state != "available"
        and item.blocked_reason != "source_references_required"
    ]

    async def active_policy(_investigation_id):
        investigation = await fixture.investigations.get(fixture.investigation_id)
        inheritance = SimpleNamespace(
            investigation_id=fixture.investigation_id,
            subject_id=investigation.subject_id,
            policy_id=envelope.inherited_policy_id,
            policy_version=envelope.inherited_policy_version,
            policy_event_id=envelope.inherited_policy_event_id,
        )
        policy = SimpleNamespace(
            policy_id=envelope.inherited_policy_id,
            version=envelope.inherited_policy_version,
            event_id=envelope.inherited_policy_event_id,
            contains_subject=lambda subject_id: subject_id == investigation.subject_id,
        )
        return SimpleNamespace(authorized=True, inheritance=inheritance, policy=policy)

    operator_service._available_capabilities = fixture.available_capabilities
    operator_service._effective_policy_for = active_policy
    operator_context = await operator_service.get_context()
    operator_tool_names = {item.name for item in operator_context.context.available_tools}
    assert {"advisor.consult", "vt.file_report"} <= operator_tool_names


@pytest.mark.asyncio
async def test_operator_context_blocks_active_policy_with_mismatched_envelope_lineage() -> None:
    (
        fixture,
        _state,
        registry,
        _summaries,
        operator_service,
        _tools,
        _identifiers,
    ) = await _seeded_fixture()
    _register_advisor(registry, fixture)
    envelope = _osint_policy_envelope(fixture.envelope, providers=("bridge", "vt"))
    fixture.envelope = envelope
    fixture.envelopes._items[fixture.investigation_id] = envelope
    investigation = await fixture.investigations.get(fixture.investigation_id)

    async def mismatched_active_policy(_investigation_id):
        return SimpleNamespace(
            authorized=True,
            inheritance=SimpleNamespace(
                investigation_id=fixture.investigation_id,
                subject_id=investigation.subject_id,
                policy_id=uuid4(),
                policy_version=envelope.inherited_policy_version,
                policy_event_id=envelope.inherited_policy_event_id,
            ),
            policy=SimpleNamespace(
                policy_id=envelope.inherited_policy_id,
                version=envelope.inherited_policy_version,
                event_id=envelope.inherited_policy_event_id,
                contains_subject=lambda subject_id: subject_id == investigation.subject_id,
            ),
        )

    operator_service._effective_policy_for = mismatched_active_policy
    operator_service._available_capabilities = fixture.available_capabilities

    operator_context = await operator_service.get_context()
    tool_names = {item.name for item in operator_context.context.available_tools}
    assert "advisor.consult" not in tool_names
    assert "vt.file_report" not in tool_names
    unavailable = set(operator_context.context.unavailable_tools)
    assert "advisor.consult:policy_authorization_unverified" in unavailable
    assert "vt.file_report:policy_authorization_unverified" in unavailable


@pytest.mark.asyncio
async def test_inactive_osint_policy_blocks_external_tools_and_vt_pivots() -> None:
    fixture, state, registry, _summaries, _service, _tools, _identifiers = await _seeded_fixture()
    _register_advisor(registry, fixture)
    envelope = _osint_policy_envelope(fixture.envelope, providers=("bridge", "vt"))
    fixture.envelope = envelope
    fixture.envelopes._items[fixture.investigation_id] = envelope

    context = await _build_context(
        fixture,
        state,
        registry,
        osint_policy_authorized=False,
    )

    assert "advisor.consult" not in {item.name for item in context.available_tools}
    assert "advisor.consult:policy_authorization_unverified" in context.unavailable_tools
    vt_pivots = [item for item in context.available_pivots if item.tool_name.startswith("vt.")]
    assert vt_pivots
    assert {item.blocked_reason for item in vt_pivots} == {"policy_authorization_unverified"}


@pytest.mark.asyncio
async def test_osint_policy_does_not_authorize_advisor_when_bridge_is_not_allowed() -> None:
    fixture, state, registry, _summaries, _service, _tools, _identifiers = await _seeded_fixture()
    _register_advisor(registry, fixture)
    envelope = _osint_policy_envelope(fixture.envelope, providers=("vt",))
    fixture.envelope = envelope
    fixture.envelopes._items[fixture.investigation_id] = envelope

    context = await _build_context(
        fixture,
        state,
        registry,
        osint_policy_authorized=True,
    )

    assert "advisor.consult" not in {item.name for item in context.available_tools}
    assert "advisor.consult:provider_not_authorized" in context.unavailable_tools
    assert any(item.name.startswith("vt.") for item in context.available_tools)


@pytest.mark.asyncio
@pytest.mark.parametrize("denial", ("tlp_red", "external_llm_disallowed"))
async def test_osint_policy_keeps_external_disclosure_denials(denial: str) -> None:
    fixture, state, registry, _summaries, _service, _tools, _identifiers = await _seeded_fixture()
    _register_advisor(registry, fixture)
    envelope = _osint_policy_envelope(fixture.envelope, providers=("bridge", "vt"))
    if denial == "external_llm_disallowed":
        fixture.snapshot = replace(fixture.snapshot, external_llm_allowed=False)
        fixture.context_builder.article_snapshots.snapshot = fixture.snapshot
    else:
        fixture.snapshot = replace(fixture.snapshot, tlp="RED")
        fixture.context_builder.article_snapshots.snapshot = fixture.snapshot
    fixture.envelope = envelope
    fixture.envelopes._items[fixture.investigation_id] = envelope

    context = await _build_context(
        fixture,
        state,
        registry,
        osint_policy_authorized=True,
    )

    assert "advisor.consult" not in {item.name for item in context.available_tools}
    assert "advisor.consult:external_disclosure_denied" in context.unavailable_tools
    assert any(
        item.tool_name.startswith("vt.")
        and item.execution_state == "blocked"
        and item.blocked_reason == "external_disclosure_denied"
        for item in context.available_pivots
    )


@pytest.mark.asyncio
async def test_available_pivots_and_exposed_families_execute_with_seeded_state() -> None:
    fixture, state, registry, _summaries, service, tools, identifiers = await _seeded_fixture()
    context = await _build_context(fixture, state, registry)

    assert len(context.investigation.sample_deltas) == 15
    assert (
        sum(item["analysis_status"] == "analyzed" for item in context.investigation.sample_deltas)
        == 2
    )
    assert context.article is not None and context.article.claims
    assert context.investigation.findings
    assert {item.tool_name for item in context.available_pivots} >= {
        "vt.file_report",
        "vt.relationships",
        "vt.intelligence_search",
    }

    version = state.version
    invoked_tools: set[str] = set()

    async def invoke(tool_name: str, arguments: Mapping[str, Any], key: str) -> None:
        nonlocal version
        result = await service.invoke(
            tool_name=tool_name,
            schema_version="1",
            arguments=arguments,
            purpose="Exercise an advertised synthetic investigation action.",
            expected_version=version,
            idempotency_key=key,
        )
        assert result.invocation.status in {
            ToolInvocationStatus.SUCCEEDED,
            ToolInvocationStatus.EMPTY,
        }, (tool_name, dict(arguments), result.error_code, result.invocation.status)
        version = result.working_state_version
        invoked_tools.add(tool_name)

    pivot_calls = 0
    for index, pivot in enumerate(context.available_pivots):
        if pivot.execution_state == "blocked":
            assert pivot.blocked_reason, (pivot.tool_name, pivot.arguments)
            continue
        assert pivot.execution_state == "available"
        await invoke(pivot.tool_name, pivot.arguments, f"seeded-pivot-{index}-{uuid4()}")
        pivot_calls += 1
    assert pivot_calls > 0

    # Exercise every exposed VT adapter once; pivots above already exercised the
    # exact argument cases for file reports, relationships, and intelligence search.
    hash_value = "d" * 64
    domain_value = "pivot-synthetic.example"
    for name in sorted(
        item.name for item in context.available_tools if item.name.startswith("vt.")
    ):
        if name in invoked_tools:
            continue
        arguments = {
            "vt.object_report": {"object_type": "domains", "object_id": domain_value},
            "vt.behaviour": {"file_hash": hash_value},
        }.get(name)
        assert arguments is not None, f"No state-backed argument oracle for {name}"
        await invoke(name, arguments, f"seeded-family-{name}-{uuid4()}")

    sample_row = identifiers["sample_rows"][2]
    sample_observation = next(iter(identifiers["linked_observations"].values()))
    family_arguments = {
        "sample.analyze": {
            "association_id": sample_row["association_id"],
            "purpose": "Analyze a seeded quarantined sample.",
            "evidence_refs": [str(sample_observation.id)],
        },
        "archive.extract_member": {
            "association_id": sample_row["association_id"],
            "member_path": "payload.bin",
            "reason": "Inspect a bounded member in the seeded archive fixture.",
        },
        "rule.propose": {"condition": "any"},
    }
    for name in sorted(
        item.name
        for item in context.available_tools
        if item.name.startswith(("sample.", "archive.", "rule."))
    ):
        assert name in family_arguments, f"No state-backed argument oracle for {name}"
        await invoke(name, family_arguments[name], f"seeded-family-{name}-{uuid4()}")

    assert invoked_tools >= {"sample.analyze", "archive.extract_member", "rule.propose"}
    assert all(tool.calls for tool in tools.values() if tool.name in invoked_tools)


@pytest.mark.asyncio
async def test_document_backed_hash_read_and_download_are_available_executable() -> None:
    (
        fixture,
        state,
        registry,
        _summaries,
        operator_service,
        tools,
        _identifiers,
    ) = await _seeded_fixture()
    definition, adapter = _schema_tool(
        "vt.download_sample",
        VT_DOWNLOAD_SAMPLE_SCHEMA,
        ToolEffect.ACQUIRE,
        VT_FILE_DOWNLOAD_CAPABILITY,
    )
    definition = replace(
        definition,
        cost_model={BudgetDimension.VT_UNITS: 1, BudgetDimension.DOWNLOADS: 1},
    )
    registry.register(definition, adapter)
    tools["vt.download_sample"] = adapter
    capabilities = frozenset((*fixture.available_capabilities, VT_FILE_DOWNLOAD_CAPABILITY))
    fixture.available_capabilities = capabilities
    fixture.service._available_capabilities = capabilities
    operator_service._available_capabilities = capabilities

    envelope = await fixture.envelopes.get(fixture.investigation_id)
    assert envelope is not None
    envelope = replace(envelope, acquire_hits_allowed=True)
    fixture.envelope = envelope
    fixture.envelopes._items[fixture.investigation_id] = envelope

    sample_hash = "f" * 64
    document_id = uuid4()
    fixture.snapshot = replace(
        fixture.snapshot,
        confirmed_iocs=(
            *fixture.snapshot.confirmed_iocs,
            ConfirmedIOCContextV1(
                indicator_type="sha256",
                value=sample_hash.upper(),
                extraction_artifact_id=uuid4(),
                extraction_artifact_version=1,
                source_document_ids=(document_id,),
            ),
        ),
    )
    fixture.context_builder.article_snapshots.snapshot = fixture.snapshot

    class _SourceDocuments:
        async def get(self, requested_id: UUID):
            return source_document if requested_id == document_id else None

    source_document = SourceDocument(
        id=document_id,
        subject_id=fixture.subject_id,
        blob_id=uuid4(),
        original_name="synthetic-article.html",
        origin="synthetic-test",
        acquired_at=datetime.now(UTC),
        license_restriction=None,
        tlp=TLP.GREEN,
        do_not_submit=False,
        external_llm_allowed=True,
    )
    fixture.service._source_documents = _SourceDocuments()

    context = await _build_context(fixture, state, registry)
    assert "vt.download_sample" in {tool.name for tool in context.available_tools}
    downloaded = await operator_service.invoke(
        tool_name="vt.download_sample",
        schema_version="1",
        arguments={
            "sha256": sample_hash,
            "purpose": "Acquire a documented synthetic sample.",
            "evidence_refs": [],
        },
        purpose="Acquire a documented synthetic sample.",
        expected_version=state.version,
        idempotency_key="documented-vt-download",
    )
    assert downloaded.invocation.status is ToolInvocationStatus.SUCCEEDED
    assert tools["vt.download_sample"].calls == [
        {
            "sha256": sample_hash,
            "purpose": "Acquire a documented synthetic sample.",
            "evidence_refs": [],
        }
    ]

    documented_pivot = next(
        item
        for item in context.available_pivots
        if item.tool_name == "vt.file_report"
        and item.arguments.get("file_hash", "").casefold() == sample_hash
    )
    assert documented_pivot.execution_state == "available"
    assert not any(
        item.sample_sha256.casefold() == sample_hash
        for item in await fixture.service._sample_associations.list_for_investigation(
            fixture.investigation_id
        )
    )
    assert not any(
        item.object_id.casefold() == sample_hash
        for item in await fixture.observations.list_for_investigation(fixture.investigation_id)
    )
    reported = await operator_service.invoke(
        tool_name="vt.file_report",
        schema_version="1",
        arguments={"file_hash": sample_hash},
        purpose="Read a document-backed synthetic hash report.",
        expected_version=downloaded.working_state_version,
        idempotency_key="documented-vt-file-report",
    )
    assert reported.invocation.status is ToolInvocationStatus.SUCCEEDED
    source_refs = reported.invocation.normalized_parameters["_vt_external_effect_source_refs"]
    assert [reference["kind"] for reference in source_refs] == [
        "sample_hash",
        "source_document",
    ]
    assert source_refs[0]["id"] == sample_hash
    assert source_refs[1]["id"] == str(document_id)
    assert source_refs[1]["version"] == (
        f"{fixture.snapshot.synthesis_artifact_id}@{fixture.snapshot.synthesis_version or 1}"
    )
    assert source_refs[1]["sha256"] == fixture.snapshot.synthesis_sha256

    refused = await operator_service.invoke(
        tool_name="vt.download_sample",
        schema_version="1",
        arguments={
            "sha256": "9" * 64,
            "purpose": "Acquire an ungrounded synthetic sample.",
            "evidence_refs": [],
        },
        purpose="Acquire an ungrounded synthetic sample.",
        expected_version=reported.working_state_version,
        idempotency_key="ungrounded-vt-download",
    )
    assert refused.invocation.status is ToolInvocationStatus.POLICY_DENIED
    assert refused.error_code == "vt_hash_not_grounded"
    result_data = refused.output["data"]["result"]
    assert result_data["reason"] == "vt_hash_not_grounded"
    assert "vt.file_report" in result_data["correction"]
    assert refused.output["error"]["reason"] == result_data["correction"]
    assert len(tools["vt.download_sample"].calls) == 1


@pytest.mark.asyncio
async def test_vt_pivots_are_blocked_before_call_without_current_g2() -> None:
    fixture, state, registry, _summaries, service, tools, _identifiers = await _seeded_fixture()
    context = await _build_context(fixture, state, registry, envelope=None)

    vt_pivots = [item for item in context.available_pivots if item.tool_name.startswith("vt.")]
    assert vt_pivots
    assert all(item.execution_state == "blocked" for item in vt_pivots)
    assert {item.blocked_reason for item in vt_pivots} == {"external_disclosure_denied"}
    assert all(not item.name.startswith("vt.") for item in context.available_tools)
    investigation = await fixture.investigations.get(fixture.investigation_id)
    assert investigation is not None
    status = build_investigation_operator_view(
        investigation=investigation,
        state=state,
        synthesis_sha256=fixture.snapshot.synthesis_sha256,
        envelope=None,
        invocations=(),
        observations=(),
        opinion=None,
        jobs=(),
        available_tools=tuple(item.name for item in registry.list_registered()),
        unavailable_tools=(),
    ).payload
    assert "vt.file_report" not in status["tool_inventory"]["available"]
    assert "vt.file_report:external_disclosure_denied" in status["tool_inventory"]["unavailable"]

    fixture.envelopes._items[fixture.investigation_id] = None
    with pytest.raises(OperatorToolInvocationError) as error:
        await service.invoke(
            tool_name="vt.file_report",
            schema_version="1",
            arguments={"file_hash": "d" * 64},
            purpose="The absent envelope must be visible before provider dispatch.",
            expected_version=state.version,
            idempotency_key="missing-g2-vt-call",
        )
    assert error.value.code == "external_disclosure_denied"
    assert tools["vt.file_report"].calls == []


@pytest.mark.asyncio
async def test_untrusted_v1_review_has_no_retry_when_only_v2_is_trusted(monkeypatch) -> None:
    from cti_app.application import (
        investigation_service,
        investigation_vt_tools,
        operator_tool_invocation,
    )

    fixture, state, registry, _summaries, service, tools, _identifiers = await _seeded_fixture()
    file_definition, _v1_adapter = registry.get("vt.file_report", "1")
    v2_adapter = _RecordingTool("vt.file_report")
    registry.register(replace(file_definition, schema_version="2"), v2_adapter)
    monkeypatch.setattr(
        investigation_service,
        "is_trusted_vt_passive_read_adapter",
        lambda _name, adapter: adapter is v2_adapter,
    )
    monkeypatch.setattr(
        investigation_vt_tools,
        "is_trusted_vt_passive_read_adapter",
        lambda _name, adapter: adapter is v2_adapter,
    )
    monkeypatch.setattr(
        operator_tool_invocation,
        "is_trusted_vt_passive_read_adapter",
        lambda _name, adapter: adapter is v2_adapter,
    )
    tools["vt.file_report"].status = ToolInvocationStatus.NEEDS_REVIEW

    result = await service.invoke(
        tool_name="vt.file_report",
        schema_version="1",
        arguments={"file_hash": "d" * 64},
        purpose="Create a synthetic ambiguous v1 passive read.",
        expected_version=state.version,
        idempotency_key="untrusted-v1-needs-review",
    )
    assert result.invocation.status is ToolInvocationStatus.NEEDS_REVIEW
    assert result.invocation.passive_read_retry_trusted is False
    assert VT_PASSIVE_RETRY_TRUSTED_PARAMETER not in result.invocation.normalized_parameters

    current_state = await fixture.states.get(fixture.investigation_id)
    assert current_state is not None
    context = await _build_context(fixture, current_state, registry)
    matching_pivots = [
        item
        for item in context.available_pivots
        if item.tool_name == "vt.file_report" and item.arguments == {"file_hash": "d" * 64}
    ]
    assert {item.schema_version for item in matching_pivots} == {"1", "2"}
    assert all(
        item.execution_state == "blocked"
        and item.blocked_reason == "ambiguous_tool_invocation_requires_reconciliation"
        for item in matching_pivots
    )

    with pytest.raises(OperatorToolInvocationError) as retry_error:
        await service.invoke(
            tool_name="vt.file_report",
            schema_version="2",
            arguments={"file_hash": "d" * 64},
            purpose="A new schema must not bypass the unresolved untrusted v1 operation.",
            expected_version=current_state.version,
            idempotency_key="untrusted-v1-needs-review-v2-retry",
        )
    assert retry_error.value.code == "ambiguous_tool_invocation_requires_reconciliation"
    assert v2_adapter.calls == []

    investigation = await fixture.investigations.get(fixture.investigation_id)
    assert investigation is not None
    view = build_investigation_operator_view(
        investigation=investigation,
        state=state,
        synthesis_sha256=fixture.snapshot.synthesis_sha256,
        envelope=await fixture.envelopes.get(fixture.investigation_id),
        invocations=(result.invocation,),
        observations=(),
        opinion=None,
        jobs=(),
        available_tools=("vt.file_report",),
        unavailable_tools=(),
    ).payload
    item = view["invocations_requiring_reconciliation"][0]
    assert item["resubmission_allowed"] is False
    assert item["retry_action"] is None
    assert item["reconciliation_action"]["resubmission_allowed"] is False


@pytest.mark.asyncio
async def test_untrusted_vt_adapter_does_not_advertise_a_needs_review_retry() -> None:
    fixture, state, registry, _summaries, _service, _tools, identifiers = await _seeded_fixture()
    old = ToolInvocation(
        investigation_id=fixture.investigation_id,
        operation_key="untrusted-passive-read-needs-review",
        tool_name="vt.file_report",
        schema_version="1",
        normalized_parameters={
            "file_hash": "d" * 64,
            VT_PASSIVE_RETRY_TRUSTED_PARAMETER: False,
        },
        purpose="Untrusted adapter result requires reconciliation.",
        status=ToolInvocationStatus.NEEDS_REVIEW,
        response_refs=(next(iter(identifiers["linked_observations"].values())).id,),
    )
    await fixture.invocations.add_if_absent(old)
    context = await _build_context(fixture, state, registry)

    retry_pivots = [
        item
        for item in context.available_pivots
        if item.tool_name == "vt.file_report" and item.arguments == {"file_hash": "d" * 64}
    ]
    assert len(retry_pivots) == 1
    assert retry_pivots[0].execution_state == "blocked"
    assert retry_pivots[0].blocked_reason == "ambiguous_tool_invocation_requires_reconciliation"
    assert "vt.file_report" not in {item.name for item in context.available_tools}
    investigation = await fixture.investigations.get(fixture.investigation_id)
    assert investigation is not None
    envelope = await fixture.envelopes.get(fixture.investigation_id)
    status = build_investigation_operator_view(
        investigation=investigation,
        state=state,
        synthesis_sha256=fixture.snapshot.synthesis_sha256,
        envelope=envelope,
        invocations=(old,),
        observations=tuple(
            await fixture.observations.list_for_investigation(fixture.investigation_id)
        ),
        opinion=None,
        jobs=(),
        available_tools=tuple(item.name for item in context.available_tools),
        unavailable_tools=context.unavailable_tools,
    ).payload
    item = status["invocations_requiring_reconciliation"][0]
    assert item["resubmission_allowed"] is False
    assert item["retry_action"] is None
    assert item["reconciliation_action"]["resubmission_allowed"] is False


@pytest.mark.asyncio
async def test_passive_retry_readiness_is_bound_to_exact_schema_version(monkeypatch) -> None:
    from cti_app.application import investigation_vt_tools

    fixture, state, registry, _summaries, _service, _tools, identifiers = await _seeded_fixture()
    trusted_definition, trusted_adapter = registry.get("vt.behaviour", "1")
    registry.register(
        replace(trusted_definition, schema_version="2"),
        _RecordingTool("vt.behaviour"),
    )
    monkeypatch.setattr(
        investigation_vt_tools,
        "is_trusted_vt_passive_read_adapter",
        lambda name, adapter: name == "vt.behaviour" and adapter is trusted_adapter,
    )
    reviewed = ToolInvocation(
        investigation_id=fixture.investigation_id,
        operation_key="untrusted-vt-behaviour-v2-needs-review",
        tool_name="vt.behaviour",
        schema_version="2",
        normalized_parameters={"file_hash": "d" * 64},
        purpose="The matching v2 adapter is not in the trusted retry set.",
        status=ToolInvocationStatus.NEEDS_REVIEW,
        response_refs=(next(iter(identifiers["linked_observations"].values())).id,),
    )
    await fixture.invocations.add_if_absent(reviewed)

    context = await _build_context(fixture, state, registry)
    retries = [
        item
        for item in context.available_pivots
        if item.tool_name == "vt.behaviour" and item.arguments == {"file_hash": "d" * 64}
    ]
    assert len(retries) == 1
    assert retries[0].schema_version == "2"
    assert retries[0].execution_state == "blocked"
    assert retries[0].blocked_reason == "ambiguous_tool_invocation_requires_reconciliation"


@pytest.mark.asyncio
async def test_needs_review_is_listed_with_passive_retry_or_advisor_reconciliation() -> None:
    fixture, state, registry, _summaries, _service, _tools, identifiers = await _seeded_fixture()
    now = datetime.now(UTC)
    passive = ToolInvocation(
        investigation_id=fixture.investigation_id,
        operation_key="needs-review-passive-read",
        tool_name="vt.file_report",
        schema_version="1",
        normalized_parameters={
            "file_hash": "d" * 64,
            "_vt_external_effect_source_refs": [{"kind": "old-internal-provenance"}],
            VT_PASSIVE_RETRY_TRUSTED_PARAMETER: True,
        },
        purpose="Uncertain passive VT read.",
        status=ToolInvocationStatus.NEEDS_REVIEW,
        response_refs=(next(iter(identifiers["linked_observations"].values())).id,),
        created_at=now,
    )
    advisor = ToolInvocation(
        investigation_id=fixture.investigation_id,
        operation_key="needs-review-advisor",
        tool_name="advisor.consult",
        schema_version="advisor-consult.v1",
        normalized_parameters={"consultation_type": "review_text"},
        purpose="Ambiguous advisor handoff.",
        status=ToolInvocationStatus.NEEDS_REVIEW,
        response_refs=(uuid4(),),
        created_at=now + timedelta(seconds=1),
    )
    unsupported_schema_passive = ToolInvocation(
        investigation_id=fixture.investigation_id,
        operation_key="needs-review-passive-read-v2",
        tool_name="vt.file_report",
        schema_version="2",
        normalized_parameters={
            "file_hash": "d" * 64,
            VT_PASSIVE_RETRY_TRUSTED_PARAMETER: False,
        },
        purpose="A passive adapter version without an operator retry contract.",
        status=ToolInvocationStatus.NEEDS_REVIEW,
        response_refs=(next(iter(identifiers["linked_observations"].values())).id,),
        created_at=now + timedelta(seconds=2),
    )
    passive_without_result = ToolInvocation(
        investigation_id=fixture.investigation_id,
        operation_key="needs-review-passive-read-without-result",
        tool_name="vt.file_report",
        schema_version="1",
        normalized_parameters={"file_hash": "e" * 64},
        purpose="Uncertain passive VT read without a stored result.",
        status=ToolInvocationStatus.NEEDS_REVIEW,
        created_at=now + timedelta(seconds=3),
    )
    investigation = await fixture.investigations.get(fixture.investigation_id)
    assert investigation is not None
    envelope = await fixture.envelopes.get(fixture.investigation_id)
    view = build_investigation_operator_view(
        investigation=investigation,
        state=state,
        synthesis_sha256=fixture.snapshot.synthesis_sha256,
        envelope=envelope,
        invocations=(passive, advisor, unsupported_schema_passive, passive_without_result),
        observations=tuple(
            await fixture.observations.list_for_investigation(fixture.investigation_id)
        ),
        opinion=None,
        jobs=(),
        available_tools=("vt.file_report", "advisor.consult"),
        unavailable_tools=("vt.file_report@1:passive_retry_ready",),
        operator_reconciliation_vt_tool_versions=frozenset(
            (definition.name, definition.schema_version)
            for definition in registry.list_registered()
            if definition.effect is ToolEffect.EXTERNAL_READ
        ),
    ).payload

    todo = view["invocations_requiring_reconciliation"]
    assert {item["id"] for item in todo} == {
        passive.id,
        advisor.id,
        unsupported_schema_passive.id,
        passive_without_result.id,
    }
    passive_view = next(item for item in todo if item["id"] == passive.id)
    advisor_view = next(item for item in todo if item["id"] == advisor.id)
    assert passive_view["response_refs"]
    assert passive_view["resubmission_allowed"] is True
    assert passive_view["retry_action"]["method"] == "POST"
    assert passive_view["retry_action"]["path"].endswith("/operator/tools/vt.file_report")
    assert passive_view["retry_action"]["arguments"] == {"file_hash": "d" * 64}
    assert passive_view["retry_action"]["idempotency_key"] != passive.operation_key
    assert passive_view["reconciliation_action"]["path"].endswith(
        f"/operator/invocations/{passive.id}/reconcile"
    )
    assert passive_view["reconciliation_action"]["accepted_outcomes"] == [
        "executed_with_result",
        "abandon",
    ]
    assert passive_view["reconciliation_action"]["requires_verified_human_operator"] is False
    assert passive_view["reconciliation_action"]["requires_idempotency_key"] is True
    assert passive_view["reconciliation_action"]["response_refs_required"] is True
    assert advisor_view["response_refs"]
    assert advisor_view["resubmission_allowed"] is False
    assert advisor_view["retry_action"] is None
    assert advisor_view["reconciliation_action"]["resubmission_allowed"] is False
    assert advisor_view["reconciliation_action"]["path"].endswith(
        f"/operator/invocations/{advisor.id}/reconcile"
    )
    unsupported_view = next(item for item in todo if item["id"] == unsupported_schema_passive.id)
    assert unsupported_view["response_refs"]
    assert unsupported_view["resubmission_allowed"] is False
    assert unsupported_view["retry_action"] is None
    assert unsupported_view["reconciliation_action"]["resubmission_allowed"] is False
    assert unsupported_view["reconciliation_action"]["requires_verified_human_operator"] is True
    no_result_view = next(item for item in todo if item["id"] == passive_without_result.id)
    assert no_result_view["reconciliation_action"]["accepted_outcomes"] == [
        "not_executed",
        "abandon",
    ]
    unverified_registry_view = build_investigation_operator_view(
        investigation=investigation,
        state=state,
        synthesis_sha256=fixture.snapshot.synthesis_sha256,
        envelope=envelope,
        invocations=(passive,),
        observations=tuple(
            await fixture.observations.list_for_investigation(fixture.investigation_id)
        ),
        opinion=None,
        jobs=(),
        available_tools=("vt.file_report",),
        unavailable_tools=(),
    ).payload
    unverified_reconciliation = unverified_registry_view["invocations_requiring_reconciliation"][0][
        "reconciliation_action"
    ]
    assert unverified_reconciliation["path"].endswith(f"/invocations/{passive.id}/reconcile")
    assert unverified_reconciliation["requires_verified_human_operator"] is True
    assert unverified_registry_view["next_idempotent_action"]["path"].endswith(
        f"/invocations/{passive.id}/reconcile"
    )
    assert (
        unverified_registry_view["next_idempotent_action"]["requires_verified_human_operator"]
        is True
    )
    assert view["next_idempotent_action"]["path"].endswith(
        f"/operator/invocations/{passive.id}/reconcile"
    )
    assert view["next_idempotent_action"]["retry_action"]["path"].endswith(
        "/operator/tools/vt.file_report"
    )
    assert "vt.file_report@1:passive_retry_ready" not in view["tool_inventory"]["unavailable"]


@pytest.mark.asyncio
async def test_operator_led_needs_review_keeps_analytic_action_separate() -> None:
    fixture, state, _registry, _summaries, _service, _tools, _identifiers = await _seeded_fixture()
    now = datetime.now(UTC)
    reviewed = ToolInvocation(
        investigation_id=fixture.investigation_id,
        operation_key="needs-review-local-analysis",
        tool_name="sample.analyze",
        schema_version="1",
        normalized_parameters={"association_id": str(uuid4())},
        purpose="Synthetic ambiguous local analysis.",
        status=ToolInvocationStatus.NEEDS_REVIEW,
        response_refs=(uuid4(),),
        created_at=now,
    )
    succeeded = tuple(
        ToolInvocation(
            investigation_id=fixture.investigation_id,
            operation_key=f"completed-local-analysis-{index}",
            tool_name="sample.analyze",
            schema_version="1",
            normalized_parameters={"association_id": str(uuid4())},
            purpose="Synthetic completed local analysis.",
            status=ToolInvocationStatus.SUCCEEDED,
            created_at=now + timedelta(seconds=index + 1),
        )
        for index in range(2)
    )
    investigation = await fixture.investigations.get(fixture.investigation_id)
    assert investigation is not None
    envelope = await fixture.envelopes.get(fixture.investigation_id)
    observations = tuple(
        await fixture.observations.list_for_investigation(fixture.investigation_id)
    )

    def build_view(
        execution_state: Any,
        invocations: tuple[ToolInvocation, ...],
        *,
        advisor_guidance: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return build_investigation_operator_view(
            investigation=investigation,
            state=execution_state,
            synthesis_sha256=fixture.snapshot.synthesis_sha256,
            envelope=envelope,
            invocations=invocations,
            observations=observations,
            opinion=None,
            jobs=(),
            available_tools=("sample.analyze",),
            unavailable_tools=(),
            advisor_guidance=advisor_guidance,
        ).payload

    operator_view = build_view(state, (reviewed, *succeeded))
    task = operator_view["reconciliation_tasks"][0]
    assert len(operator_view["reconciliation_tasks"]) == 1
    assert task["invocation_id"] == reviewed.id
    assert task["tool_name"] == "sample.analyze"
    assert task["status"] == "needs_review"
    assert task["requires_verified_human_operator"] is True
    assert task["response_persisted"] is True
    assert operator_view["next_analytic_action"] == {
        "code": "operator_action_required",
        "method": "GET",
        "path": f"/api/investigations/{fixture.investigation_id}/operator/context",
        "expected_version": state.version,
    }
    assert operator_view["next_idempotent_action"] == {
        "code": "reconcile_tool_invocation",
        "invocation_id": reviewed.id,
        "method": "POST",
        "path": (
            f"/api/investigations/{fixture.investigation_id}/invocations/{reviewed.id}/reconcile"
        ),
        "expected_version": reviewed.version,
        "accepted_outcomes": ["executed_with_result", "not_executed", "abandon"],
        "reason_required": True,
        "response_refs_allowed_for": "executed_with_result",
        "idempotency_key_header": "Idempotency-Key",
        "requires_verified_human_operator": True,
        "resubmission_allowed": False,
        "blocks_other_tools": False,
        "analytic_action_available": True,
    }

    due_guidance = {
        "checkpoint_due": True,
        "suggested_consultation_type": "review_text",
        "reason": "synthetic checkpoint",
    }
    advisor_view = build_view(state, (reviewed,), advisor_guidance=due_guidance)
    assert advisor_view["next_analytic_action"]["code"] == "advisor_checkpoint_due"
    assert advisor_view["next_idempotent_action"]["code"] == "reconcile_tool_invocation"

    running = ToolInvocation(
        investigation_id=fixture.investigation_id,
        operation_key="running-local-analysis",
        tool_name="sample.analyze",
        schema_version="1",
        normalized_parameters={"association_id": str(uuid4())},
        purpose="Synthetic in-flight local analysis.",
        status=ToolInvocationStatus.RUNNING,
    )
    running_view = build_view(state, (running,))
    assert running_view["next_idempotent_action"]["code"] == "wait_for_worker"
    assert running_view["next_idempotent_action"]["invocation_id"] == running.id
    assert running_view["next_idempotent_action"]["path"].endswith("/operator-status")
    assert running_view["next_analytic_action"]["code"] == "operator_action_required"
    (running_task,) = running_view["reconciliation_tasks"]
    assert running_task["status"] == "running"
    assert running_task["reconciliation_action"] is None
    assert running_task["requires_verified_human_operator"] is False

    bridge_state = replace(state, execution_mode=InvestigationExecutionMode.BRIDGE_LED)
    bridge_view = build_view(bridge_state, (reviewed,))
    assert bridge_view["next_idempotent_action"]["code"] == "reconcile_tool_invocation"
    assert "blocks_other_tools" not in bridge_view["next_idempotent_action"]
    assert bridge_view["next_analytic_action"] == bridge_view["next_idempotent_action"]


@pytest.mark.asyncio
async def test_passive_retry_uses_current_trusted_schema_after_version_change(monkeypatch) -> None:
    from cti_app.application import investigation_vt_tools

    fixture, state, registry, _summaries, _service, _tools, identifiers = await _seeded_fixture()
    old_definition, _ = registry.get("vt.file_report", "1")
    registry.unregister("vt.file_report", "1")
    current_adapter = _RecordingTool("vt.file_report")
    registry.register(replace(old_definition, schema_version="2"), current_adapter)
    monkeypatch.setattr(
        investigation_vt_tools,
        "is_trusted_vt_passive_read_adapter",
        lambda name, adapter: name == "vt.file_report" and adapter is current_adapter,
    )
    stale = ToolInvocation(
        investigation_id=fixture.investigation_id,
        operation_key="needs-review-passive-read-old-schema",
        tool_name="vt.file_report",
        schema_version="1",
        normalized_parameters={
            "file_hash": "d" * 64,
            VT_PASSIVE_RETRY_TRUSTED_PARAMETER: True,
        },
        purpose="The original trusted passive adapter schema is no longer registered.",
        status=ToolInvocationStatus.NEEDS_REVIEW,
        response_refs=(next(iter(identifiers["linked_observations"].values())).id,),
    )
    investigation = await fixture.investigations.get(fixture.investigation_id)
    assert investigation is not None
    envelope = await fixture.envelopes.get(fixture.investigation_id)
    assert envelope is not None
    retry_hints = await blocked_capabilities_from_history(
        investigation_id=fixture.investigation_id,
        invocations=(stale,),
        observations=(),
    )
    availability = project_tool_availability(
        registry,
        ToolExecutionContext(
            investigation_id=fixture.investigation_id,
            envelope=envelope,
            budget=envelope.budget,
            idempotency_key="stale-vt-schema-availability",
            available_capabilities=fixture.available_capabilities,
            blocked_capabilities=retry_hints,
        ),
    )
    assert "vt.file_report@2:passive_retry_ready" in availability.unavailable_diagnostics
    view = build_investigation_operator_view(
        investigation=investigation,
        state=state,
        synthesis_sha256=fixture.snapshot.synthesis_sha256,
        envelope=envelope,
        invocations=(stale,),
        observations=(),
        opinion=None,
        jobs=(),
        available_tools=availability.available_names,
        unavailable_tools=availability.unavailable_diagnostics,
    ).payload

    item = view["invocations_requiring_reconciliation"][0]
    assert item["resubmission_allowed"] is True
    assert item["retry_action"]["schema_version"] == "2"


@pytest.mark.asyncio
async def test_live_running_passive_read_cannot_start_a_second_occurrence(monkeypatch) -> None:
    from cti_app.application import operator_tool_invocation

    fixture, state, _registry, _summaries, service, tools, _identifiers = await _seeded_fixture()
    tool = tools["vt.file_report"]
    tool.started = asyncio.Event()
    tool.release = asyncio.Event()
    monkeypatch.setattr(
        operator_tool_invocation,
        "is_trusted_vt_passive_read_adapter",
        lambda _name, _adapter: True,
    )
    arguments = {"file_hash": "d" * 64}

    first_task = asyncio.create_task(
        service.invoke(
            tool_name="vt.file_report",
            schema_version="1",
            arguments=arguments,
            purpose="A synthetic passive read held in flight.",
            expected_version=state.version,
            idempotency_key="running-passive-first",
        )
    )
    await asyncio.wait_for(tool.started.wait(), timeout=2)
    running_state = await fixture.states.get(fixture.investigation_id)
    assert running_state is not None
    running_invocations = await fixture.invocations.list_for_investigation(fixture.investigation_id)
    running = next(
        item for item in running_invocations if item.status is ToolInvocationStatus.RUNNING
    )

    with pytest.raises(OperatorToolInvocationError) as error:
        await service.invoke(
            tool_name="vt.file_report",
            schema_version="1",
            arguments=arguments,
            purpose="A second key must not overlap a live passive read.",
            expected_version=running_state.version,
            idempotency_key="running-passive-second",
        )

    assert error.value.code == "ambiguous_tool_invocation_requires_reconciliation"
    assert len(tool.calls) == 1
    current_running = await fixture.invocations.get(running.id)
    assert current_running is not None and current_running.status is ToolInvocationStatus.RUNNING
    tool.release.set()
    first_result = await first_task
    assert first_result.invocation.status is ToolInvocationStatus.SUCCEEDED
    assert len(tool.calls) == 1
