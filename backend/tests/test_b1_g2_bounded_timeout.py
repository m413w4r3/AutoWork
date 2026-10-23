from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

import cti_app.application.investigation_advisor_tools as advisor_tools
from cti_app.application.investigation_advisor_tools import (
    ADVISOR_SCHEMA_VERSION,
    ADVISOR_TOOL_NAME,
    register_investigation_advisor_tool,
)
from cti_app.application.investigation_tools import (
    InMemoryToolRegistry,
    RegistryToolDispatcher,
    ToolExecutionContext,
)
from cti_app.domain.investigation import (
    BudgetDimension,
    DisclosureBounds,
    InvestigationBudget,
    InvestigationEnvelope,
    TLPLevel,
    ToolInvocationStatus,
)
from tests.test_investigation_advisor_tools import (
    Transport,
    advisor_response,
    make_observation,
    make_tool,
)


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _context(investigation_id, *, key: str, tool_seconds: int) -> ToolExecutionContext:
    budget = InvestigationBudget(limits={})
    budget.reserve(
        key,
        {
            BudgetDimension.BRIDGE_TURNS: 1,
            BudgetDimension.CONTEXT_BYTES: 4_000_000,
            BudgetDimension.CONTEXT_TOKENS: 1_000_000,
            BudgetDimension.TOOL_SECONDS: tool_seconds,
        },
    )
    return ToolExecutionContext(
        investigation_id=investigation_id,
        envelope=None,
        budget=budget,
        idempotency_key=key,
    )


def _valid_submission(clock: FakeClock, elapsed: float, observation_id) -> Transport:
    def submit(_kwargs):
        clock.advance(elapsed)
        return advisor_tools.TurnSubmission(
            advisor_tools.TurnSubmissionStatus.SUCCEEDED,
            output_text=advisor_response("Réponse liée aux mesures.", [str(observation_id)]),
        )

    return Transport(submit)


def _evidence_payload(sample_sha256: str) -> dict[str, object]:
    return {
        "schema": "sample-obfuscated-strings.v1",
        "sample_sha256": sample_sha256,
        "strings": [
            {"value": "Blackwood measured beacon marker", "offset": 128, "encoding": "ascii"}
        ],
    }


@pytest.mark.asyncio
async def test_3600_second_reservation_bounds_7200_role_and_late_response_is_ambiguous(
    monkeypatch,
) -> None:
    clock = FakeClock()
    monkeypatch.setattr(advisor_tools.time, "monotonic", clock)
    investigation_id = uuid4()
    observation, raw = make_observation(investigation_id, _evidence_payload("a" * 64))
    transport = _valid_submission(clock, 3_601, observation.id)
    tool, _ = make_tool(
        investigation_id=investigation_id,
        transport=transport,
        observation=observation,
        raw=raw,
        role_timeout_seconds=7_200,
    )
    assert await tool._measurement_payload(observation) is not None

    context = _context(investigation_id, key="bounded-3600-late", tool_seconds=3_600)
    result = await tool.execute(
        {"consultation_type": "pivot_strategy", "evidence_refs": [str(observation.id)]},
        context,
    )

    assert transport.calls, dict(result.output)
    assert [call["timeout_seconds"] for call in transport.calls] == [3_600]
    assert len(transport.calls) == 1
    assert result.status is ToolInvocationStatus.NEEDS_REVIEW
    assert result.output["advisor_status"] == "needs_review"
    assert result.budget_consumed[BudgetDimension.TOOL_SECONDS] <= 3_600
    assert "blocks" not in result.output
    reservation = context.budget.commit(context.idempotency_key, result.budget_consumed)
    assert (
        reservation.committed_amounts[BudgetDimension.TOOL_SECONDS]
        <= reservation.amounts[BudgetDimension.TOOL_SECONDS]
    )


@pytest.mark.asyncio
async def test_9000_second_reservation_is_bounded_by_7200_role_timeout(monkeypatch) -> None:
    clock = FakeClock()
    monkeypatch.setattr(advisor_tools.time, "monotonic", clock)
    investigation_id = uuid4()
    observation, raw = make_observation(investigation_id, _evidence_payload("b" * 64))
    transport = _valid_submission(clock, 0, observation.id)
    tool, _ = make_tool(
        investigation_id=investigation_id,
        transport=transport,
        observation=observation,
        raw=raw,
        role_timeout_seconds=7_200,
    )

    result = await tool.execute(
        {"consultation_type": "pivot_strategy", "evidence_refs": [str(observation.id)]},
        _context(investigation_id, key="bounded-9000", tool_seconds=9_000),
    )

    assert [call["timeout_seconds"] for call in transport.calls] == [7_200]
    assert len(transport.calls) == 1
    assert result.status is ToolInvocationStatus.SUCCEEDED


@pytest.mark.asyncio
async def test_response_at_3599_seconds_succeeds_within_3600_reservation(monkeypatch) -> None:
    clock = FakeClock()
    monkeypatch.setattr(advisor_tools.time, "monotonic", clock)
    investigation_id = uuid4()
    observation, raw = make_observation(investigation_id, _evidence_payload("c" * 64))
    transport = _valid_submission(clock, 3_599, observation.id)
    tool, _ = make_tool(
        investigation_id=investigation_id,
        transport=transport,
        observation=observation,
        raw=raw,
        role_timeout_seconds=7_200,
    )

    result = await tool.execute(
        {"consultation_type": "pivot_strategy", "evidence_refs": [str(observation.id)]},
        _context(investigation_id, key="bounded-3599", tool_seconds=3_600),
    )

    assert [call["timeout_seconds"] for call in transport.calls] == [3_600]
    assert len(transport.calls) == 1
    assert result.status is ToolInvocationStatus.SUCCEEDED
    assert result.budget_consumed[BudgetDimension.TOOL_SECONDS] == 3_599


@pytest.mark.asyncio
async def test_role_timeout_below_reservation_is_propagated(monkeypatch) -> None:
    clock = FakeClock()
    monkeypatch.setattr(advisor_tools.time, "monotonic", clock)
    investigation_id = uuid4()
    observation, raw = make_observation(investigation_id, _evidence_payload("d" * 64))
    transport = _valid_submission(clock, 1_799, observation.id)
    tool, _ = make_tool(
        investigation_id=investigation_id,
        transport=transport,
        observation=observation,
        raw=raw,
        role_timeout_seconds=1_800,
    )

    result = await tool.execute(
        {"consultation_type": "pivot_strategy", "evidence_refs": [str(observation.id)]},
        _context(investigation_id, key="role-1800", tool_seconds=3_600),
    )

    assert [call["timeout_seconds"] for call in transport.calls] == [1_800]
    assert len(transport.calls) == 1
    assert result.status is ToolInvocationStatus.SUCCEEDED
    assert result.budget_consumed[BudgetDimension.TOOL_SECONDS] == 1_799


@pytest.mark.asyncio
async def test_remaining_capacity_below_minimum_is_refused_before_reservation_or_transport() -> (
    None
):
    investigation_id = uuid4()
    transport = Transport(
        advisor_tools.TurnSubmission(
            advisor_tools.TurnSubmissionStatus.SUCCEEDED,
            output_text="never reached",
        )
    )
    tool, _ = make_tool(
        investigation_id=investigation_id,
        transport=transport,
        role_timeout_seconds=7_200,
    )
    budget = InvestigationBudget(limits={BudgetDimension.TOOL_SECONDS: 3_659})
    budget.reserve("previous-operation", {BudgetDimension.TOOL_SECONDS: 3_600})
    budget.commit("previous-operation")
    envelope = InvestigationEnvelope(
        investigation_id=investigation_id,
        allowed_providers=("bridge",),
        budget=budget,
        acquire_hits_allowed=False,
        disclosure=DisclosureBounds(tlp=TLPLevel.CLEAR, external_llm_allowed=True),
        approval_decision_id=uuid4(),
        approval_actor_id="analyst:test",
        approved_at=datetime.now(UTC),
    )
    context = ToolExecutionContext(
        investigation_id=investigation_id,
        envelope=envelope,
        budget=budget,
        idempotency_key="operation-under-minimum",
        available_capabilities=frozenset({"bridge.advisor"}),
    )
    registry = InMemoryToolRegistry()
    register_investigation_advisor_tool(registry, adapter=tool)
    dispatcher = RegistryToolDispatcher(registry)

    result = await dispatcher.dispatch(
        context,
        tool_name=ADVISOR_TOOL_NAME,
        schema_version=ADVISOR_SCHEMA_VERSION,
        arguments={"consultation_type": "plan_investigation"},
        purpose="test bounded advisor admission",
        operation_key=context.idempotency_key,
    )

    assert result.status is ToolInvocationStatus.NEEDS_REVIEW
    assert result.output["pre_submission_reason"] == "budget_exceeded"
    assert "tool_seconds" in result.message
    assert len(transport.calls) == 0
    assert all(
        reservation.operation_key != context.idempotency_key for reservation in budget.reservations
    )
