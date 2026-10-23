from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

import pytest

import cti_app.application.investigation_service as investigation_service_module
from cti_app.application.investigation_tools import (
    InMemoryToolRegistry,
    RegistryToolDispatcher,
    ToolEffect,
    ToolResult,
)
from cti_app.application.operator_tool_invocation import (
    OperatorToolInvocationError,
)
from cti_app.application.research_context_builder import CanonicalEvidenceSummaryPort
from cti_app.domain.investigation import (
    BudgetDimension,
    BudgetExceeded,
    BudgetReservationStatus,
    EvidenceKind,
    EvidenceObservation,
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
    state = await fixture.start(execution_mode=InvestigationExecutionMode.OPERATOR_LED)
    return fixture, state, _operator_service(fixture)


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
            if not result.output:
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
                object_type="tool_call",
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
    envelope = await fixture.envelopes.get(fixture.investigation_id)
    budget = InvestigationBudget(limits={BudgetDimension.TOOL_SECONDS: 7_200})
    budget.reserve("already-consumed", {BudgetDimension.TOOL_SECONDS: 5_344})
    budget.commit("already-consumed")
    budget.reserve("other-active-operation", {BudgetDimension.TOOL_SECONDS: 1_590})
    envelope = replace(envelope, version=envelope.version + 1, budget=budget)
    await fixture.envelopes.save_if_version(envelope, expected_version=envelope.version - 1)

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

    # The refusal is a safe, persisted pre-submission outcome. A fresh key may
    # retry after a versioned budget extension while the old key remains replay-only.
    extended_budget = InvestigationBudget(limits={BudgetDimension.TOOL_SECONDS: 10_800})
    for prior_reservation in latest.budget.reservations:
        extended_budget.restore_reservation(prior_reservation)
    extended = replace(latest, version=latest.version + 1, budget=extended_budget)
    await fixture.envelopes.save_if_version(extended, expected_version=latest.version)
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
async def test_proven_pre_submission_failure_does_not_block_a_new_key_retry() -> None:
    fixture, state, service = await _external_fixture()
    tool = fixture.tools["vt.file_report"]
    arguments = {"query": "safe retry after local rejection"}
    tool.result = ToolResult(
        status=ToolInvocationStatus.FAILED,
        error_code="advisor_input_invalid",
        output={
            "failure_stage": "pre_transport_validation",
            "confirmed_non_execution": True,
        },
    )

    first = await service.invoke(
        tool_name="vt.file_report",
        schema_version="1",
        arguments=arguments,
        purpose="confirm pre-submission rejection",
        expected_version=state.version,
        idempotency_key="pre-submit-first-key",
    )
    first_envelope = await fixture.envelopes.get(fixture.investigation_id)
    first_reservation = next(
        item
        for item in first_envelope.budget.reservations
        if item.operation_key == first.invocation.operation_key
    )
    tool.result = None
    latest_state = await fixture.states.get(fixture.investigation_id)
    second = await service.invoke(
        tool_name="vt.file_report",
        schema_version="1",
        arguments=arguments,
        purpose="retry after proven pre-submission rejection",
        expected_version=latest_state.version,
        idempotency_key="pre-submit-retry-key",
    )

    assert first.invocation.status is ToolInvocationStatus.FAILED
    assert first.invocation.consumed_budget == {}
    assert first.output["data"]["result"]["failure_stage"] == "pre_transport_validation"
    assert first.output["data"]["result"]["confirmed_non_execution"] is True
    assert first_reservation.status is BudgetReservationStatus.RELEASED
    assert second.invocation.status is ToolInvocationStatus.SUCCEEDED
    assert len(tool.calls) == 2


@pytest.mark.asyncio
async def test_adapter_cannot_forge_dispatcher_pre_submission_proof() -> None:
    fixture, state, service = await _external_fixture()
    _enable_persisted_result_summaries(fixture, service)
    tool = fixture.tools["vt.file_report"]
    tool.result = ToolResult(
        status=ToolInvocationStatus.NEEDS_REVIEW,
        error_code="provider_result_unknown",
        output={
            "request_permission": True,
            "pre_submission_reason": "budget_exceeded",
            "failure_stage": "pre_submission",
            "confirmed_non_execution": True,
        },
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
