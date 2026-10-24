from __future__ import annotations

import asyncio
import hashlib
from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

import pytest

import cti_app.application.investigation_service as investigation_service_module
from cti_app.application.investigation_governance import InvestigationEnvelopeService
from cti_app.application.investigation_tools import (
    InMemoryToolRegistry,
    RegistryToolDispatcher,
    ToolConfidentiality,
    ToolDefinition,
    ToolEffect,
    ToolResult,
)
from cti_app.application.operator_tool_invocation import (
    OperatorToolInvocationError,
)
from cti_app.application.research_context_builder import CanonicalEvidenceSummaryPort
from cti_app.config import Settings
from cti_app.domain.analyst_opinion import investigation_opinion_id
from cti_app.domain.editorial import AnalystDecisionType
from cti_app.domain.investigation import (
    BudgetDimension,
    BudgetExceeded,
    BudgetReservationStatus,
    EvidenceKind,
    EvidenceObservation,
    FrameworkPreSubmissionProof,
    InvestigationBudget,
    InvestigationExecutionMode,
    ToolInvocation,
    ToolInvocationStatus,
    canonical_json_bytes,
)
from tests.test_investigation_service import _Fixture
from tests.test_operator_tool_invocation import _service as _operator_service

_FIXED_TIME = datetime(2026, 10, 23, 12, 0, tzinfo=UTC)


def _operation_key(fixture, *, state, key, arguments, purpose, tool_name="vt.file_report"):
    payload = canonical_json_bytes(
        {
            "tool_name": tool_name,
            "schema_version": "1",
            "arguments": arguments,
            "purpose": purpose,
            "expected_version": state.version,
        }
    )
    key_digest = hashlib.sha256(f"{fixture.investigation_id}\0{key}".encode()).hexdigest()
    return f"operator:{key_digest}:{hashlib.sha256(payload).hexdigest()}"


def _revoked(envelope):
    return replace(
        envelope,
        version=envelope.version + 1,
        allowed_providers=(),
        acquire_hits_allowed=False,
        budget=InvestigationBudget(limits={dimension: 0 for dimension in envelope.budget.limits}),
        disclosure=replace(
            envelope.disclosure,
            do_not_submit=True,
            external_llm_allowed=False,
        ),
    )


async def _external_fixture(*, cost=None):
    fixture = _Fixture(
        responses=[],
        tools=(
            (
                "vt.file_report",
                "vt",
                ToolEffect.EXTERNAL_READ,
                "vt.read",
                cost or {BudgetDimension.VT_UNITS: 1},
            ),
        ),
    )
    if cost is not None and BudgetDimension.TOOL_SECONDS in cost:
        fixture.envelope = replace(
            fixture.envelope,
            budget=InvestigationBudget(
                limits={
                    BudgetDimension.BRIDGE_TURNS: 8,
                    BudgetDimension.VT_UNITS: 20,
                    BudgetDimension.TOOL_SECONDS: 7_200,
                }
            ),
        )
    state = await fixture.start(execution_mode=InvestigationExecutionMode.OPERATOR_LED)
    return fixture, state, _operator_service(fixture)


def _audited_envelope_service(fixture):
    decisions = []

    class _DecisionRepository:
        async def list_for_investigation(self, _investigation_id):
            return tuple(decisions)

        async def append(self, decision):
            decisions.append(decision)

    class _Uow:
        investigation_envelopes = fixture.envelopes
        analyst_investigations = fixture.investigations
        analyst_decisions = _DecisionRepository()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc_info):
            return None

        async def commit(self):
            return None

    return (
        InvestigationEnvelopeService(lambda: _Uow(), settings=Settings(_env_file=None)),
        decisions,
    )


def _enable_persisted_result_summaries(fixture, service):
    blobs = {}

    class _InMemoryArtifactStore:
        async def read_bytes(self, blob_id, *, max_bytes):
            payload = blobs[blob_id]
            if len(payload) > max_bytes:
                raise ValueError("test observation exceeds summary limit")
            return payload

    class _CanonicalWriter:
        async def archive(self, invocation, result):
            if not result.output and result.framework_pre_submission_proof is None:
                return ()
            payload = canonical_json_bytes(
                {
                    "schema": "investigation-tool-observation.v1",
                    "tool_name": invocation.tool_name,
                    "schema_version": invocation.schema_version,
                    "operation_key": invocation.operation_key,
                    "result": dict(result.output),
                }
            )
            blob_id = uuid4()
            blobs[blob_id] = payload
            observation = EvidenceObservation(
                investigation_id=invocation.investigation_id,
                kind=EvidenceKind.OTHER,
                blob_id=blob_id,
                sha256=hashlib.sha256(payload).hexdigest(),
                source=invocation.tool_name,
                source_version=invocation.schema_version,
                observed_at=_FIXED_TIME,
                object_type=(
                    result.framework_pre_submission_proof.value
                    if result.framework_pre_submission_proof is not None
                    else "tool_call"
                ),
                object_id=invocation.operation_key,
                invocation_ref=invocation.id,
            )
            return (await fixture.observations.append_if_absent(observation),)

    fixture.service._observation_writer = _CanonicalWriter()
    service._observations = fixture.observations
    service._evidence_summaries = CanonicalEvidenceSummaryPort(_InMemoryArtifactStore())


@pytest.mark.asyncio
async def test_service_and_dispatcher_reuse_the_persisted_cost_and_key() -> None:
    cost = {BudgetDimension.TOOL_SECONDS: 30}
    fixture, state, _ = await _external_fixture(cost=cost)
    envelope = await fixture.envelopes.get(fixture.investigation_id)
    budget = InvestigationBudget(limits={BudgetDimension.TOOL_SECONDS: 120})
    envelope = replace(envelope, version=envelope.version + 1, budget=budget)
    await fixture.envelopes.save_if_version(envelope, expected_version=envelope.version - 1)

    original_definition = fixture.registry.get("vt.file_report", "1")[0]
    dispatcher_reservations = []
    dispatcher_registry = InMemoryToolRegistry()
    dispatcher_registry.register(
        replace(
            original_definition,
            cost_model={BudgetDimension.TOOL_SECONDS: 99},
        ),
        fixture.tools["vt.file_report"],
    )
    changed_cost_dispatcher = RegistryToolDispatcher(dispatcher_registry)

    class _CostRecordingDispatcher:
        async def dispatch(self, context, **kwargs):
            dispatcher_reservations.append(
                (kwargs["operation_key"], dict(kwargs["reservation_amounts"]))
            )
            return await changed_cost_dispatcher.dispatch(context, **kwargs)

    fixture.service._dispatcher = _CostRecordingDispatcher()
    invocation, result, _refs, _uncertain = await fixture.service.execute_tool_call(
        investigation=await fixture.investigations.get(fixture.investigation_id),
        envelope=envelope,
        budget=envelope.budget,
        state=state,
        tool_name="vt.file_report",
        schema_version="1",
        arguments={"query": "fixed query"},
        purpose="check a report",
        operation_key="same-cost-operation",
    )

    latest = await fixture.envelopes.get(fixture.investigation_id)
    reservation = next(
        item for item in latest.budget.reservations if item.operation_key == "same-cost-operation"
    )
    assert result.status is ToolInvocationStatus.SUCCEEDED
    assert invocation.reserved_budget == cost
    assert dispatcher_reservations == [("same-cost-operation", cost)]
    assert reservation.status is BudgetReservationStatus.COMMITTED
    assert reservation.amounts == cost
    assert reservation.committed_amounts == cost
    assert len(fixture.tools["vt.file_report"].calls) == 1


@pytest.mark.asyncio
async def test_second_reservation_failure_releases_the_first_without_dispatch(monkeypatch) -> None:
    class _FailSecondReservation(InvestigationBudget):
        def __init__(self):
            super().__init__(limits={BudgetDimension.VT_UNITS: 3})
            self.calls = 0

        def reserve(self, operation_key, amounts):
            self.calls += 1
            if self.calls == 2:
                raise BudgetExceeded(BudgetDimension.VT_UNITS)
            return super().reserve(operation_key, amounts)

    fixture, state, _ = await _external_fixture()
    envelope = await fixture.envelopes.get(fixture.investigation_id)
    budget = _FailSecondReservation()
    envelope = replace(envelope, version=envelope.version + 1, budget=budget)
    await fixture.envelopes.save_if_version(envelope, expected_version=envelope.version - 1)
    monkeypatch.setattr(
        investigation_service_module, "_copy_investigation_budget", lambda value: value
    )

    invocation, result, _refs, _uncertain = await fixture.service.execute_tool_call(
        investigation=await fixture.investigations.get(fixture.investigation_id),
        envelope=envelope,
        budget=budget,
        state=state,
        tool_name="vt.file_report",
        schema_version="1",
        arguments={"query": "one query"},
        purpose="exercise the second admission check",
        operation_key="second-reservation-fails",
    )

    latest = await fixture.envelopes.get(fixture.investigation_id)
    reservation = next(
        item
        for item in latest.budget.reservations
        if item.operation_key == "second-reservation-fails"
    )
    assert budget.calls == 2
    assert invocation.reserved_budget == {BudgetDimension.VT_UNITS: 1}
    assert invocation.consumed_budget == {}
    assert result.status is ToolInvocationStatus.NEEDS_REVIEW
    assert reservation.status is BudgetReservationStatus.RELEASED
    assert latest.budget.reserved[BudgetDimension.VT_UNITS] == 0
    assert latest.budget.consumed[BudgetDimension.VT_UNITS] == 0
    assert fixture.tools["vt.file_report"].calls == []


@pytest.mark.asyncio
async def test_266_seconds_refusal_is_only_a_request_and_has_no_external_output() -> None:
    requested = {BudgetDimension.TOOL_SECONDS: 3_600}
    fixture, state, service = await _external_fixture(cost=requested)
    _enable_persisted_result_summaries(fixture, service)
    governance, decisions = _audited_envelope_service(fixture)
    envelope = await fixture.envelopes.get(fixture.investigation_id)
    budget = InvestigationBudget(limits=dict(envelope.budget.limits))
    for reservation in envelope.budget.reservations:
        budget.restore_reservation(reservation)
    budget.reserve("already-consumed", {BudgetDimension.TOOL_SECONDS: 5_344})
    budget.commit("already-consumed")
    budget.reserve("other-active-operation", {BudgetDimension.TOOL_SECONDS: 1_590})
    await fixture.envelopes.save_if_version(
        replace(envelope, version=envelope.version + 1, budget=budget),
        expected_version=envelope.version,
    )

    result = await service.invoke(
        tool_name="vt.file_report",
        schema_version="1",
        arguments={"query": "budget boundary"},
        purpose="check the remaining tool time",
        expected_version=state.version,
        idempotency_key="tool-seconds-266",
    )

    latest = await fixture.envelopes.get(fixture.investigation_id)
    operation_reservation = next(
        item
        for item in latest.budget.reservations
        if item.operation_key == result.invocation.operation_key
    )
    assert latest.budget.remaining[BudgetDimension.TOOL_SECONDS] == 266
    assert result.invocation.reserved_budget == requested  # requested, not admitted
    assert result.invocation.consumed_budget == {}
    assert result.invocation.status is ToolInvocationStatus.NEEDS_REVIEW
    assert operation_reservation.status is BudgetReservationStatus.RELEASED
    assert latest.budget.reserved[BudgetDimension.TOOL_SECONDS] == 1_590
    assert fixture.tools["vt.file_report"].calls == []
    assert fixture.bridge_transport.submission_count == 0
    proof_observation = await fixture.observations.get(result.invocation.response_refs[0])
    assert proof_observation.object_type == FrameworkPreSubmissionProof.BUDGET_REJECTED.value
    assert proof_observation.invocation_ref == result.invocation.id
    assert proof_observation.object_id == result.invocation.operation_key

    # The refusal is a safe, persisted pre-submission outcome. A fresh key may
    # retry after a versioned budget extension while the old key remains replay-only.
    extended = await governance.extend(
        fixture.investigation_id,
        expected_version=latest.version,
        allowed_providers=latest.allowed_providers,
        budget=InvestigationBudget(limits={BudgetDimension.TOOL_SECONDS: 10_800}),
        acquire_hits_allowed=latest.acquire_hits_allowed,
        disclosure=latest.disclosure,
        actor_id="analyst:alice",
        reason="Increase the approved tool-time budget after review.",
        correlation_id="g2:tool-seconds-approved-retry",
    )
    assert extended.version == latest.version + 1
    assert len(decisions) == 1
    assert decisions[-1].decision_type is AnalystDecisionType.PIVOT_APPROVE
    assert decisions[-1].actor_id == "analyst:alice"
    assert decisions[-1].reason == "Increase the approved tool-time budget after review."
    assert decisions[-1].correlation_id == "g2:tool-seconds-approved-retry"
    latest_state = await fixture.states.get(fixture.investigation_id)
    retried = await service.invoke(
        tool_name="vt.file_report",
        schema_version="1",
        arguments={"query": "budget boundary"},
        purpose="check the remaining tool time",
        expected_version=latest_state.version,
        idempotency_key="tool-seconds-after-approved-extension",
    )

    previous = await fixture.invocations.get_by_operation_key(result.invocation.operation_key)
    assert previous.status is ToolInvocationStatus.FAILED
    assert retried.invocation.status is ToolInvocationStatus.SUCCEEDED
    assert len(fixture.tools["vt.file_report"].calls) == 1


@pytest.mark.asyncio
async def test_persisted_running_reservation_replays_to_review_without_transport() -> None:
    fixture, state, service = await _external_fixture()
    key = "crash-after-admission"
    arguments = {"query": "ambiguous operation"}
    purpose = "resume a persisted admission"
    operation_key = _operation_key(
        fixture, state=state, key=key, arguments=arguments, purpose=purpose
    )
    envelope = await fixture.envelopes.get(fixture.investigation_id)
    envelope.budget.reserve(operation_key, {BudgetDimension.VT_UNITS: 1})
    await fixture.envelopes.save_if_version(
        replace(envelope, version=envelope.version + 1), expected_version=envelope.version
    )
    invocation = ToolInvocation(
        investigation_id=fixture.investigation_id,
        operation_key=operation_key,
        tool_name="vt.file_report",
        schema_version="1",
        normalized_parameters=arguments,
        purpose=purpose,
        reserved_budget={BudgetDimension.VT_UNITS: 1},
        created_at=_FIXED_TIME,
        updated_at=_FIXED_TIME,
    ).transition(ToolInvocationStatus.RUNNING, now=_FIXED_TIME)
    await fixture.invocations.add_if_absent(invocation)

    replay = await service.invoke(
        tool_name="vt.file_report",
        schema_version="1",
        arguments=arguments,
        purpose=purpose,
        expected_version=state.version,
        idempotency_key=key,
    )

    latest = await fixture.envelopes.get(fixture.investigation_id)
    reservation = next(
        item for item in latest.budget.reservations if item.operation_key == operation_key
    )
    assert replay.idempotent_replay is True
    assert replay.invocation.id == invocation.id
    assert replay.invocation.status is ToolInvocationStatus.NEEDS_REVIEW
    assert reservation.status is BudgetReservationStatus.RESERVED
    assert latest.budget.consumed[BudgetDimension.VT_UNITS] == 0
    assert fixture.tools["vt.file_report"].calls == []
    assert fixture.bridge_transport.submission_count == 0


@pytest.mark.asyncio
async def test_new_idempotency_key_is_blocked_by_ambiguous_semantic_match() -> None:
    fixture, state, service = await _external_fixture()
    arguments = {"query": "same normalized parameters"}
    old_operation_key = "operator:old-key:old-payload"
    envelope = await fixture.envelopes.get(fixture.investigation_id)
    envelope.budget.reserve(old_operation_key, {BudgetDimension.VT_UNITS: 1})
    await fixture.envelopes.save_if_version(
        replace(envelope, version=envelope.version + 1), expected_version=envelope.version
    )
    ambiguous = (
        ToolInvocation(
            investigation_id=fixture.investigation_id,
            operation_key=old_operation_key,
            tool_name="vt.file_report",
            schema_version="1",
            normalized_parameters=arguments,
            purpose="an earlier attempt",
            reserved_budget={BudgetDimension.VT_UNITS: 1},
            created_at=_FIXED_TIME,
            updated_at=_FIXED_TIME,
        )
        .transition(ToolInvocationStatus.RUNNING, now=_FIXED_TIME)
        .transition(ToolInvocationStatus.NEEDS_REVIEW, now=_FIXED_TIME)
    )
    await fixture.invocations.add_if_absent(ambiguous)

    with pytest.raises(OperatorToolInvocationError) as error:
        await service.invoke(
            tool_name="vt.file_report",
            schema_version="1",
            arguments=arguments,
            purpose="try again with a new key",
            expected_version=state.version,
            idempotency_key="fresh-idempotency-key",
        )

    latest = await fixture.envelopes.get(fixture.investigation_id)
    assert error.value.code == "ambiguous_tool_invocation_requires_reconciliation"
    assert (await fixture.states.get(fixture.investigation_id)).version == state.version
    assert fixture.tools["vt.file_report"].calls == []
    assert fixture.bridge_transport.submission_count == 0
    assert latest.budget.reserved[BudgetDimension.VT_UNITS] == 1
    assert latest.budget.consumed[BudgetDimension.VT_UNITS] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("old_arguments", "new_arguments", "old_schema_version"),
    [
        (
            {"consultation_type": "plan_investigation", "question": "plan pivot"},
            {"consultation_type": "plan_investigation", "question": " plan pivot "},
            "advisor-consult.v1",
        ),
        (
            {"consultation_type": "plan_investigation"},
            {"consultation_type": "plan_investigation", "question": ""},
            "advisor-consult.v0",
        ),
        (
            {"consultation_type": "plan_investigation", "question": "pivot"},
            {
                "consultation_type": "plan_investigation",
                "question": "pivot",
                "evidence_refs": [],
            },
            "advisor-consult.v1",
        ),
    ],
)
async def test_advisor_semantic_normalization_blocks_equivalent_new_key(
    old_arguments, new_arguments, old_schema_version
) -> None:
    fixture, state, service = await _external_fixture()

    class _NeverDispatchedAdapter:
        def __init__(self):
            self.calls = []

        async def execute(self, arguments, _context):
            self.calls.append(dict(arguments))
            return ToolResult(ToolInvocationStatus.SUCCEEDED)

    adapter = _NeverDispatchedAdapter()
    fixture.registry.register(
        ToolDefinition(
            name="advisor.consult",
            schema_version="advisor-consult.v1",
            input_schema={
                "type": "object",
                "properties": {
                    "consultation_type": {"type": "string"},
                    "question": {"type": "string"},
                },
                "required": ["consultation_type"],
                "additionalProperties": False,
            },
            effect=ToolEffect.EXTERNAL_READ,
            cost_model={BudgetDimension.TOOL_SECONDS: 1},
            confidentiality=ToolConfidentiality.INTERNAL,
            limitations=("Offline semantic-identity oracle",),
            capability_key="vt.advisor",
        ),
        adapter,
    )
    fixture.service._available_capabilities |= {"bridge.advisor", "vt.advisor"}
    service._available_capabilities |= {"bridge.advisor", "vt.advisor"}
    old_operation_key = "operator:advisor-old-key:old-payload"
    ambiguous = (
        ToolInvocation(
            investigation_id=fixture.investigation_id,
            operation_key=old_operation_key,
            tool_name="advisor.consult",
            schema_version=old_schema_version,
            normalized_parameters=old_arguments,
            purpose="earlier ambiguous advisor request",
            reserved_budget={BudgetDimension.TOOL_SECONDS: 1},
            created_at=_FIXED_TIME,
            updated_at=_FIXED_TIME,
        )
        .transition(ToolInvocationStatus.RUNNING, now=_FIXED_TIME)
        .transition(ToolInvocationStatus.NEEDS_REVIEW, now=_FIXED_TIME)
    )
    await fixture.invocations.add_if_absent(ambiguous)

    with pytest.raises(OperatorToolInvocationError) as error:
        await service.invoke(
            tool_name="advisor.consult",
            schema_version="advisor-consult.v1",
            arguments=new_arguments,
            purpose="retry an equivalent advisor request",
            expected_version=state.version,
            idempotency_key="advisor-fresh-equivalent-key",
        )

    assert error.value.code == "ambiguous_tool_invocation_requires_reconciliation"
    assert adapter.calls == []
    assert fixture.bridge_transport.submission_count == 0
    assert (await fixture.states.get(fixture.investigation_id)).version == state.version


class _NeverDispatchedAdapter:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def execute(self, arguments, _context):
        self.calls.append(dict(arguments))
        return ToolResult(ToolInvocationStatus.SUCCEEDED)


def _register_dummy_advisor(fixture, service) -> _NeverDispatchedAdapter:
    adapter = _NeverDispatchedAdapter()
    fixture.registry.register(
        ToolDefinition(
            name="advisor.consult",
            schema_version="advisor-consult.v1",
            input_schema={
                "type": "object",
                "properties": {
                    "consultation_type": {"type": "string"},
                    "question": {"type": "string"},
                    "finding_refs": {"type": "array"},
                    "opinion_id": {"type": "string"},
                    "opinion_version": {"type": "integer"},
                    "form": {"type": "string"},
                },
                "required": ["consultation_type"],
                "additionalProperties": True,
            },
            effect=ToolEffect.EXTERNAL_READ,
            cost_model={BudgetDimension.TOOL_SECONDS: 1},
            confidentiality=ToolConfidentiality.INTERNAL,
            limitations=("Offline semantic-identity oracle",),
            capability_key="vt.advisor",
        ),
        adapter,
    )
    fixture.service._available_capabilities |= {"bridge.advisor", "vt.advisor"}
    service._available_capabilities |= {"bridge.advisor", "vt.advisor"}
    return adapter


async def _store_ambiguous_invocation(
    fixture, *, tool_name: str, schema_version: str, arguments
) -> None:
    ambiguous = (
        ToolInvocation(
            investigation_id=fixture.investigation_id,
            operation_key=f"operator:{tool_name}-old-key:old-payload",
            tool_name=tool_name,
            schema_version=schema_version,
            normalized_parameters=arguments,
            purpose="earlier ambiguous request",
            reserved_budget={BudgetDimension.TOOL_SECONDS: 1},
            created_at=_FIXED_TIME,
            updated_at=_FIXED_TIME,
        )
        .transition(ToolInvocationStatus.RUNNING, now=_FIXED_TIME)
        .transition(ToolInvocationStatus.NEEDS_REVIEW, now=_FIXED_TIME)
    )
    await fixture.invocations.add_if_absent(ambiguous)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_name", "schema_version", "old_arguments", "new_arguments", "needs_advisor"),
    [
        (
            "vt.file_report",
            "1",
            {"file_hash": "A" * 64},
            {"file_hash": "a" * 64},
            False,
        ),
        (
            "advisor.consult",
            "advisor-consult.v1",
            {
                "consultation_type": "figure_plan",
                "finding_refs": ["ABCDEF01-2345-4678-9ABC-DEF012345678"],
            },
            {
                "consultation_type": "figure_plan",
                "finding_refs": ["abcdef01-2345-4678-9abc-def012345678"],
            },
            True,
        ),
    ],
)
async def test_effect_equivalent_representations_block_a_fresh_key(
    tool_name, schema_version, old_arguments, new_arguments, needs_advisor
) -> None:
    fixture, state, service = await _external_fixture()
    adapter = _register_dummy_advisor(fixture, service) if needs_advisor else None
    await _store_ambiguous_invocation(
        fixture,
        tool_name=tool_name,
        schema_version=schema_version,
        arguments=old_arguments,
    )

    with pytest.raises(OperatorToolInvocationError) as error:
        await service.invoke(
            tool_name=tool_name,
            schema_version=schema_version,
            arguments=new_arguments,
            purpose="retry the same effect under a fresh key",
            expected_version=state.version,
            idempotency_key="effect-equivalent-fresh-key",
        )

    assert error.value.code == "ambiguous_tool_invocation_requires_reconciliation"
    if adapter is not None:
        assert adapter.calls == []
    assert fixture.bridge_transport.submission_count == 0


@pytest.mark.asyncio
async def test_review_text_omitted_defaults_match_explicit_equivalent_new_key() -> None:
    fixture, state, service = await _external_fixture()
    adapter = _register_dummy_advisor(fixture, service)
    await _store_ambiguous_invocation(
        fixture,
        tool_name="advisor.consult",
        schema_version="advisor-consult.v1",
        arguments={"consultation_type": "review_text"},
    )
    default_opinion_id = str(investigation_opinion_id(fixture.investigation_id))

    with pytest.raises(OperatorToolInvocationError) as error:
        await service.invoke(
            tool_name="advisor.consult",
            schema_version="advisor-consult.v1",
            arguments={
                "consultation_type": "review_text",
                "opinion_id": default_opinion_id,
                "opinion_version": 3,
                "form": "body",
            },
            purpose="retry review_text with explicit default values",
            expected_version=state.version,
            idempotency_key="review-text-explicit-defaults-key",
        )

    assert error.value.code == "ambiguous_tool_invocation_requires_reconciliation"
    assert adapter.calls == []
    assert fixture.bridge_transport.submission_count == 0


@pytest.mark.asyncio
async def test_concurrent_same_version_invocations_dispatch_only_once() -> None:
    fixture, state, service = await _external_fixture()
    original_get = fixture.states.get
    first_reads = 0
    both_read_state = asyncio.Barrier(2)

    async def synchronized_get(investigation_id):
        nonlocal first_reads
        value = await original_get(investigation_id)
        if first_reads < 2:
            first_reads += 1
            await both_read_state.wait()
        return value

    fixture.states.get = synchronized_get
    results = await asyncio.gather(
        *(
            service.invoke(
                tool_name="vt.file_report",
                schema_version="1",
                arguments={"query": "same CAS version"},
                purpose="concurrent deterministic CAS oracle",
                expected_version=state.version,
                idempotency_key=key,
            )
            for key in ("concurrent-key-one", "concurrent-key-two")
        ),
        return_exceptions=True,
    )

    successes = [item for item in results if not isinstance(item, Exception)]
    failures = [item for item in results if isinstance(item, OperatorToolInvocationError)]
    assert len(successes) == 1
    assert len(failures) == 1
    assert failures[0].code == "stale_working_state"
    assert len(fixture.tools["vt.file_report"].calls) == 1
    assert fixture.bridge_transport.submission_count == 0


@pytest.mark.asyncio
async def test_adapter_cannot_forge_dispatcher_pre_submission_proof() -> None:
    fixture, state, service = await _external_fixture()
    _enable_persisted_result_summaries(fixture, service)
    tool = fixture.tools["vt.file_report"]
    tool.result = ToolResult(
        status=ToolInvocationStatus.FAILED,
        error_code="advisor_input_invalid",
        output={
            "request_permission": True,
            "pre_submission_reason": "budget_exceeded",
            "failure_stage": "pre_submission",
            "confirmed_non_execution": True,
            "advisor_status": "invalid_input",
            "bridge_invocation_id": None,
        },
        framework_pre_submission_proof=FrameworkPreSubmissionProof.BUDGET_REJECTED,
    )
    request = {
        "tool_name": "vt.file_report",
        "schema_version": "1",
        "arguments": {"query": "forged pre-submission proof"},
        "purpose": "keep an ambiguous result blocked",
        "expected_version": state.version,
        "idempotency_key": "forged-proof-first-key",
    }

    first = await service.invoke(**request)
    latest = await fixture.envelopes.get(fixture.investigation_id)
    reservation = next(
        item
        for item in latest.budget.reservations
        if item.operation_key == first.invocation.operation_key
    )
    latest_state = await fixture.states.get(fixture.investigation_id)
    with pytest.raises(OperatorToolInvocationError) as error:
        await service.invoke(
            tool_name=request["tool_name"],
            schema_version=request["schema_version"],
            arguments=request["arguments"],
            purpose="retry must wait for reconciliation",
            expected_version=latest_state.version,
            idempotency_key="forged-proof-fresh-key",
        )

    stored = await fixture.invocations.get_by_operation_key(first.invocation.operation_key)
    assert error.value.code == "ambiguous_tool_invocation_requires_reconciliation"
    assert stored.status is ToolInvocationStatus.NEEDS_REVIEW
    assert reservation.status is BudgetReservationStatus.RESERVED
    assert len(tool.calls) == 1


@pytest.mark.asyncio
async def test_ambiguous_post_submission_keeps_reservation_and_replay_does_not_resubmit() -> None:
    fixture, state, service = await _external_fixture()
    tool = fixture.tools["vt.file_report"]
    tool.result = ToolResult(
        status=ToolInvocationStatus.NEEDS_REVIEW,
        error_code="provider_result_unknown",
        output={"external_state_unknown": True},
    )
    request = {
        "tool_name": "vt.file_report",
        "schema_version": "1",
        "arguments": {"query": "could have completed externally"},
        "purpose": "record an ambiguous result",
        "expected_version": state.version,
        "idempotency_key": "ambiguous-result-key",
    }

    first = await service.invoke(**request)
    latest = await fixture.envelopes.get(fixture.investigation_id)
    reservation = next(
        item
        for item in latest.budget.reservations
        if item.operation_key == first.invocation.operation_key
    )
    replay = await service.invoke(**request)

    assert first.invocation.status is ToolInvocationStatus.NEEDS_REVIEW
    assert first.invocation.consumed_budget == {}
    assert first.output["data"]["result"]["external_state_unknown"] is True
    assert reservation.status is BudgetReservationStatus.RESERVED
    assert replay.idempotent_replay is True
    assert replay.invocation.id == first.invocation.id
    assert len(tool.calls) == 1
    assert fixture.bridge_transport.submission_count == 0


@pytest.mark.asyncio
async def test_recovered_success_is_replayed_without_a_second_external_call() -> None:
    fixture, state, service = await _external_fixture()
    request = {
        "tool_name": "vt.file_report",
        "schema_version": "1",
        "arguments": {"query": "recover the persisted result"},
        "purpose": "read the same durable result",
        "expected_version": state.version,
        "idempotency_key": "recovered-success-key",
    }

    first = await service.invoke(**request)
    replay = await service.invoke(**request)
    latest = await fixture.envelopes.get(fixture.investigation_id)
    reservation = next(
        item
        for item in latest.budget.reservations
        if item.operation_key == first.invocation.operation_key
    )

    assert first.invocation.status is ToolInvocationStatus.SUCCEEDED
    assert replay.idempotent_replay is True
    assert replay.invocation.id == first.invocation.id
    assert reservation.status is BudgetReservationStatus.COMMITTED
    assert reservation.committed_amounts == {BudgetDimension.VT_UNITS: 1}
    assert len(fixture.tools["vt.file_report"].calls) == 1


@pytest.mark.asyncio
async def test_stale_envelope_cas_cannot_overwrite_a_revocation() -> None:
    fixture, state, service = await _external_fixture()
    base = fixture.envelopes

    class _RevokeBeforeAdmissionSave:
        triggered = False

        async def get(self, investigation_id):
            return await base.get(investigation_id)

        async def save_if_version(self, envelope, *, expected_version):
            if not self.triggered:
                self.triggered = True
                current = await base.get(envelope.investigation_id)
                await base.save_if_version(_revoked(current), expected_version=current.version)
            return await base.save_if_version(envelope, expected_version=expected_version)

    fixture.service._envelopes = _RevokeBeforeAdmissionSave()

    result = await service.invoke(
        tool_name="vt.file_report",
        schema_version="1",
        arguments={"query": "stale envelope admission"},
        purpose="attempt with a stale envelope version",
        expected_version=state.version,
        idempotency_key="stale-envelope-key",
    )

    latest = await base.get(fixture.investigation_id)
    assert result.invocation.status is ToolInvocationStatus.FAILED
    assert result.output["data"]["result"]["reason"] == "envelope_admission_conflict"
    assert latest.allowed_providers == ()
    assert latest.disclosure.do_not_submit is True
    assert latest.disclosure.external_llm_allowed is False
    assert latest.budget.reserved[BudgetDimension.VT_UNITS] == 0
    assert fixture.tools["vt.file_report"].calls == []
    assert fixture.bridge_transport.submission_count == 0


@pytest.mark.asyncio
async def test_revocation_after_admission_is_rechecked_before_dispatch() -> None:
    fixture, state, service = await _external_fixture()
    original_commit = fixture.service._commit_uow
    revoked = False

    async def commit_then_revoke():
        nonlocal revoked
        await original_commit()
        if revoked:
            return
        current = await fixture.envelopes.get(fixture.investigation_id)
        reservation = next(
            (
                item
                for item in current.budget.reservations
                if item.status is BudgetReservationStatus.RESERVED
            ),
            None,
        )
        if reservation is None:
            return
        revoked = True
        await fixture.envelopes.save_if_version(_revoked(current), expected_version=current.version)

    fixture.service._commit_uow = commit_then_revoke
    result = await service.invoke(
        tool_name="vt.file_report",
        schema_version="1",
        arguments={"query": "revoke before external submit"},
        purpose="exercise the final G2 check",
        expected_version=state.version,
        idempotency_key="revoke-between-admission-and-dispatch",
    )

    latest = await fixture.envelopes.get(fixture.investigation_id)
    reservation = next(
        item
        for item in latest.budget.reservations
        if item.operation_key == result.invocation.operation_key
    )
    assert revoked is True
    assert result.invocation.status is ToolInvocationStatus.POLICY_DENIED
    assert result.error_code == "tool_policy_denied"
    assert result.output["data"]["result"]["reason"] == "envelope_revoked_before_dispatch"
    assert reservation.status is BudgetReservationStatus.RELEASED
    assert latest.allowed_providers == ()
    assert latest.disclosure.external_llm_allowed is False
    assert fixture.tools["vt.file_report"].calls == []
    assert fixture.bridge_transport.submission_count == 0
