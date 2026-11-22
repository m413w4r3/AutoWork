"""Timeline projection oracles (M24-04B3, ADR-0011 D8/P3)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from cti_app.application.investigation_persistence import (
    InMemoryEvidenceObservationRepository,
    InMemoryInvestigationPlanRepository,
    InMemoryInvestigationTaskRepository,
    InMemoryToolInvocationRepository,
)
from cti_app.application.investigation_task_events import (
    TaskEvent,
    TaskEventDraft,
    TaskEventType,
    append_task_event,
)
from cti_app.application.investigation_timeline import (
    InvestigationTimelineService,
    TimelineActorKind,
    TimelineItemType,
    TimelineItemV1,
    encode_position_cursor,
)
from cti_app.domain.editorial import (
    AnalystDecision,
    AnalystDecisionTargetType,
    AnalystDecisionType,
)
from cti_app.domain.investigation import (
    EvidenceKind,
    EvidenceObservation,
    ToolInvocation,
    ToolInvocationStatus,
)
from cti_app.domain.investigation_plan import (
    InvestigationLeadV1,
    InvestigationPlanV1,
    InvestigationPlanVersion,
    PlanParseStatus,
)
from cti_app.domain.investigation_task import (
    InvestigationTaskV1,
    ResultVerificationStatus,
    TaskActor,
    TaskActorKind,
    TaskPriority,
    TaskStatus,
)

INVESTIGATION_ID = UUID("00000000-0000-0000-0000-000000000901")
T0 = datetime(2026, 11, 22, 12, 0, tzinfo=UTC)
HUMAN = TaskActor(kind=TaskActorKind.HUMAN, actor_id="analyst:test")
OPERATOR = TaskActor(kind=TaskActorKind.OPERATOR, actor_id="operator:test")
SYSTEM = TaskActor(kind=TaskActorKind.SYSTEM, actor_id="plan_accept:test")


class _Decisions:
    def __init__(self) -> None:
        self.rows: list[AnalystDecision] = []

    async def append(self, decision: AnalystDecision) -> None:
        self.rows.append(decision)

    async def list_for_investigation(self, investigation_id: UUID) -> list[AnalystDecision]:
        return [decision for decision in self.rows if decision.investigation_id == investigation_id]


class _ExtraSource:
    def __init__(self, item: TimelineItemV1) -> None:
        self._item = item

    async def items(self, investigation_id: UUID) -> tuple[TimelineItemV1, ...]:
        assert investigation_id == INVESTIGATION_ID
        return (self._item,)


def _task(
    *, status: TaskStatus = TaskStatus.PROPOSED, task_key: str = "lead-1"
) -> InvestigationTaskV1:
    return InvestigationTaskV1(
        id=uuid4(),
        investigation_id=INVESTIGATION_ID,
        task_key=task_key,
        title="Inspect the loader",
        question="Which gate contacts the C2?",
        objective="Identify the second-stage downloader.",
        priority=TaskPriority.NORMAL,
        status=status,
        status_version=1,
        result_verification_status=ResultVerificationStatus.UNVERIFIED,
        created_by=SYSTEM,
        created_at=T0,
        updated_at=T0,
    )


async def _append_event(
    repository: InMemoryInvestigationTaskRepository,
    task: InvestigationTaskV1,
    *,
    event_type: TaskEventType = TaskEventType.TASK_CREATED,
    idempotency_key: str,
    actor: TaskActor = SYSTEM,
    occurred_at: datetime,
    to_status: TaskStatus | None = None,
    analyst_decision_id: UUID | None = None,
) -> TaskEvent:
    draft = TaskEventDraft(
        investigation_id=task.investigation_id,
        task_id=task.id,
        event_type=event_type,
        actor=actor,
        idempotency_key=idempotency_key,
        to_status=to_status,
        analyst_decision_id=analyst_decision_id,
    )
    event, _ = await append_task_event(repository, draft, occurred_at=occurred_at)
    return event


def _observation(
    *,
    source: str = "virustotal",
    object_type: str = "vt_file_report",
    observed_at: datetime,
    invocation_ref: UUID | None = None,
) -> EvidenceObservation:
    return EvidenceObservation(
        investigation_id=INVESTIGATION_ID,
        kind=EvidenceKind.OTHER,
        blob_id=uuid4(),
        sha256="a" * 64,
        source=source,
        source_version="test.v1",
        observed_at=observed_at,
        object_type=object_type,
        object_id="object-1",
        invocation_ref=invocation_ref,
    )


def _planner(
    *,
    tasks: InMemoryInvestigationTaskRepository | None = None,
    decisions: _Decisions | None = None,
    observations: InMemoryEvidenceObservationRepository | None = None,
    invocations: InMemoryToolInvocationRepository | None = None,
    plans: InMemoryInvestigationPlanRepository | None = None,
    extra_sources: tuple[_ExtraSource, ...] = (),
) -> InvestigationTimelineService:
    return InvestigationTimelineService(
        tasks=tasks or InMemoryInvestigationTaskRepository(),
        decisions=decisions or _Decisions(),
        observations=observations or InMemoryEvidenceObservationRepository(),
        invocations=invocations or InMemoryToolInvocationRepository(),
        plans=plans or InMemoryInvestigationPlanRepository(),
        extra_sources=extra_sources,
    )


def test_timeline_deduplicates_decision_and_event() -> None:
    async def scenario() -> None:
        tasks = InMemoryInvestigationTaskRepository()
        decisions = _Decisions()
        task = await tasks.add(_task())
        created = await _append_event(
            tasks,
            task,
            idempotency_key="created",
            occurred_at=T0,
            to_status=TaskStatus.PROPOSED,
        )
        decision = AnalystDecision(
            id=uuid4(),
            investigation_id=INVESTIGATION_ID,
            decision_type=AnalystDecisionType.TASK_RESULT_ACCEPT,
            target_type=AnalystDecisionTargetType.TASK,
            target_id=task.id,
            actor_id=HUMAN.actor_id,
            reason="result reviewed",
            correlation_id="task:review",
            occurred_at=T0 + timedelta(seconds=10),
        )
        await decisions.append(decision)
        verified = await _append_event(
            tasks,
            task,
            event_type=TaskEventType.VERIFICATION_RECORDED,
            idempotency_key="verified",
            actor=HUMAN,
            occurred_at=T0 + timedelta(seconds=10),
            analyst_decision_id=decision.id,
        )

        service = _planner(tasks=tasks, decisions=decisions)
        items = await service.items(INVESTIGATION_ID)

        decision_items = [item for item in items if item.item_type is TimelineItemType.DECISION]
        assert len(decision_items) == 1
        assert decision_items[0].object_id == decision.id
        assert decision_items[0].task_id == task.id
        assert decision_items[0].actor_kind is TimelineActorKind.HUMAN
        assert decision_items[0].linked_event_ids == (verified.id,)
        # The decision absorbs the event: one item, never a second journal row.
        assert all(item.object_id != verified.id for item in items)
        event_items = [item for item in items if item.item_type is TimelineItemType.TASK_EVENT]
        assert [item.object_id for item in event_items] == [created.id]

    asyncio.run(scenario())


def test_advisory_items_labelled_non_evidence() -> None:
    async def scenario() -> None:
        observations = InMemoryEvidenceObservationRepository()
        invocations = InMemoryToolInvocationRepository()
        plans = InMemoryInvestigationPlanRepository()
        canonical = _observation(observed_at=T0)
        advisory = _observation(
            source="advisor.consult.response",
            object_type="advisor_response_received",
            observed_at=T0 + timedelta(seconds=1),
        )
        await observations.append_if_absent(canonical)
        await observations.append_if_absent(advisory)
        invocation = ToolInvocation(
            investigation_id=INVESTIGATION_ID,
            operation_key="advisor-plan-1",
            tool_name="advisor.consult",
            schema_version="advisor-consult.v1",
            normalized_parameters={"consultation_type": "plan_investigation"},
            purpose="Ask Bridge for a plan",
            status=ToolInvocationStatus.SUCCEEDED,
            created_at=T0 + timedelta(seconds=2),
            updated_at=T0 + timedelta(seconds=2),
        )
        await invocations.add_if_absent(invocation)
        await plans.create_version(
            InvestigationPlanVersion(
                investigation_id=INVESTIGATION_ID,
                version=1,
                content=InvestigationPlanV1(
                    investigation_id=INVESTIGATION_ID,
                    version=1,
                    leads=(InvestigationLeadV1(title="Inspect the loader"),),
                ),
                parse_status=PlanParseStatus.PARSED,
                source_invocation_id=invocation.id,
                source_observation_id=advisory.id,
                created_at=T0 + timedelta(seconds=3),
            ),
            expected_current_version=None,
        )

        service = _planner(observations=observations, invocations=invocations, plans=plans)
        items = await service.items(INVESTIGATION_ID)

        by_id = {item.object_id: item for item in items}
        assert by_id[canonical.id].evidence is True
        assert by_id[advisory.id].evidence is False
        assert by_id[advisory.id].item_type is TimelineItemType.OBSERVATION
        plan_items = [item for item in items if item.item_type is TimelineItemType.PLAN_VERSION]
        assert plan_items and all(item.evidence is False for item in plan_items)
        consultation_items = [
            item for item in items if item.item_type is TimelineItemType.BRIDGE_CONSULTATION
        ]
        assert consultation_items and all(item.evidence is False for item in consultation_items)

    asyncio.run(scenario())


def test_timeline_union_items_have_typed_object_refs() -> None:
    async def scenario() -> None:
        tasks = InMemoryInvestigationTaskRepository()
        decisions = _Decisions()
        observations = InMemoryEvidenceObservationRepository()
        invocations = InMemoryToolInvocationRepository()
        plans = InMemoryInvestigationPlanRepository()
        task = await tasks.add(_task())
        await _append_event(
            tasks,
            task,
            idempotency_key="created",
            occurred_at=T0,
            to_status=TaskStatus.PROPOSED,
        )
        decision = AnalystDecision(
            investigation_id=INVESTIGATION_ID,
            decision_type=AnalystDecisionType.PLAN_ACCEPT,
            target_type=AnalystDecisionTargetType.PLAN_VERSION,
            target_id=uuid4(),
            actor_id=HUMAN.actor_id,
            reason="plan accepted",
            correlation_id="plan:accept",
            occurred_at=T0 + timedelta(seconds=1),
        )
        await decisions.append(decision)
        canonical = _observation(observed_at=T0 + timedelta(seconds=2))
        await observations.append_if_absent(canonical)
        invocation = ToolInvocation(
            investigation_id=INVESTIGATION_ID,
            operation_key="tool-1",
            tool_name="evidence.get",
            schema_version="evidence-get.v1",
            normalized_parameters={},
            purpose="Read evidence",
            status=ToolInvocationStatus.SUCCEEDED,
            created_at=T0 + timedelta(seconds=3),
            updated_at=T0 + timedelta(seconds=3),
        )
        await invocations.add_if_absent(invocation)

        service = _planner(
            tasks=tasks,
            decisions=decisions,
            observations=observations,
            invocations=invocations,
            plans=plans,
        )
        first = await service.page(INVESTIGATION_ID, limit=2)
        assert first.has_more is True
        assert first.next_cursor is not None
        second = await service.page(INVESTIGATION_ID, cursor=first.next_cursor, limit=10)
        assert second.has_more is False
        items = (*first.items, *second.items)

        assert len(items) == 4
        assert len({item.object_id for item in items}) == 4
        keys = [(item.occurred_at, item.object_id) for item in items]
        assert keys == sorted(keys)
        for item in items:
            assert item.item_type in TimelineItemType
            assert isinstance(item.object_id, UUID)
            assert item.actor_kind in TimelineActorKind
            assert item.actor_id
            assert 1 <= len(item.summary) <= 200
            assert item.refs
            assert all(isinstance(value, str) for value in item.refs.values())
        assert {item.item_type for item in items} == {
            TimelineItemType.TASK_EVENT,
            TimelineItemType.DECISION,
            TimelineItemType.OBSERVATION,
            TimelineItemType.TOOL_INVOCATION,
        }

        # A cursor is bound to its investigation.
        foreign = encode_position_cursor(investigation_id=uuid4(), occurred_at=T0, item_id=uuid4())
        with pytest.raises(ValueError):
            await service.page(INVESTIGATION_ID, cursor=foreign)

    asyncio.run(scenario())


def test_bridge_consultation_absorbs_its_two_observations() -> None:
    async def scenario() -> None:
        observations = InMemoryEvidenceObservationRepository()
        invocations = InMemoryToolInvocationRepository()
        invocation = ToolInvocation(
            investigation_id=INVESTIGATION_ID,
            operation_key="advisor-1",
            tool_name="advisor.consult",
            schema_version="advisor-consult.v1",
            normalized_parameters={"consultation_type": "interpretation"},
            purpose="Ask Bridge to interpret",
            status=ToolInvocationStatus.SUCCEEDED,
            created_at=T0,
            updated_at=T0,
        )
        call = _observation(
            source="advisor.consult",
            object_type="advisor_call",
            observed_at=T0,
            invocation_ref=invocation.id,
        )
        response = _observation(
            source="advisor.consult.response",
            object_type="advisor_response_received",
            observed_at=T0 + timedelta(seconds=1),
            invocation_ref=invocation.id,
        )
        invocation = ToolInvocation(
            investigation_id=invocation.investigation_id,
            operation_key=invocation.operation_key,
            tool_name=invocation.tool_name,
            schema_version=invocation.schema_version,
            normalized_parameters=invocation.normalized_parameters,
            purpose=invocation.purpose,
            status=invocation.status,
            response_refs=(response.id,),
            id=invocation.id,
            created_at=invocation.created_at,
            updated_at=invocation.updated_at,
        )
        await invocations.add_if_absent(invocation)
        await observations.append_if_absent(call)
        await observations.append_if_absent(response)

        items = await _planner(observations=observations, invocations=invocations).items(
            INVESTIGATION_ID
        )

        assert len(items) == 1
        consultation = items[0]
        assert consultation.item_type is TimelineItemType.BRIDGE_CONSULTATION
        assert consultation.object_id == invocation.id
        assert consultation.linked_observation_ids == (call.id, response.id)
        assert consultation.evidence is False

    asyncio.run(scenario())


def test_extra_sources_extend_the_union_without_new_tables() -> None:
    async def scenario() -> None:
        extra = TimelineItemV1(
            item_type=TimelineItemType.TASK_EVENT,
            object_id=uuid4(),
            occurred_at=T0,
            task_id=None,
            actor_kind=TimelineActorKind.SYSTEM,
            actor_id="future-source",
            summary="handoff_created",
            evidence=False,
            refs={"handoff_id": "future"},
        )
        items = await _planner(extra_sources=(_ExtraSource(extra),)).items(INVESTIGATION_ID)
        assert [item.object_id for item in items] == [extra.object_id]

    asyncio.run(scenario())
