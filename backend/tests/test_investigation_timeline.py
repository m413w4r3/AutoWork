"""Timeline projection oracles (M24-04B3, ADR-0011 D8/P3)."""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from cti_app.application.investigation_persistence import (
    HandoffRecord,
    InMemoryAgentSessionRepository,
    InMemoryEvidenceObservationRepository,
    InMemoryInvestigationHandoffRepository,
    InMemoryInvestigationPlanRepository,
    InMemoryInvestigationTaskRepository,
    InMemoryToolInvocationRepository,
    InMemoryWorkContributionRepository,
)
from cti_app.application.investigation_task_events import (
    TaskEvent,
    TaskEventDraft,
    TaskEventType,
    append_task_event,
)
from cti_app.application.investigation_timeline import (
    AgentSessionTimelineSource,
    ContributionTimelineSource,
    HandoffTimelineSource,
    InstructionTimelineSource,
    InvestigationTimelineService,
    TimelineActorKind,
    TimelineItemType,
    TimelineItemV1,
    TimelineSource,
    encode_position_cursor,
)
from cti_app.domain.agentic_operator import (
    AgentSessionInstructionV1,
    AgentSessionState,
    AgentSessionV1,
    InstructionDeliveryMode,
    InstructionState,
    OperatorAdapterKind,
    OperatorCapabilitiesV1,
    OperatorProbeCommand,
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
from cti_app.domain.investigation_handoff import (
    HandoffActor,
    HandoffActorKind,
    HandoffTaskSnapshot,
    InvestigationHandoffV1,
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
from cti_app.domain.work_contribution import (
    WorkContributionCategory,
    WorkContributionOrigin,
    WorkContributionV1,
)

INVESTIGATION_ID = UUID("00000000-0000-0000-0000-000000000901")
T0 = datetime(2026, 11, 22, 12, 0, tzinfo=UTC)
HUMAN = TaskActor(kind=TaskActorKind.HUMAN, actor_id="analyst:test")
OPERATOR = TaskActor(kind=TaskActorKind.OPERATOR, actor_id="operator:test")
SYSTEM = TaskActor(kind=TaskActorKind.SYSTEM, actor_id="plan_accept:test")
SESSION_SECRET_HASH = hashlib.sha256(b"session-auth-secret").hexdigest()
INSTRUCTION_TEXT = "Pull the strings blob and list the C2 gates."
FILE_SHA = "b" * 64


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


def _capabilities() -> OperatorCapabilitiesV1:
    return OperatorCapabilitiesV1(
        adapter_kind=OperatorAdapterKind.CODEX_CLI,
        executable="/usr/local/bin/codex",
        version="codex-cli 1.2.3",
        modes=("exec",),
        supports_non_interactive=True,
        probed_at=T0,
        probes=(
            OperatorProbeCommand(
                argv=("/usr/local/bin/codex", "--help"),
                exit_code=0,
                stdout_sha256="a" * 64,
                advertised_modes=("exec",),
            ),
        ),
    )


def _handoff(task: InvestigationTaskV1) -> HandoffRecord:
    handoff = InvestigationHandoffV1(
        handoff_id=str(uuid4()),
        investigation_id=str(INVESTIGATION_ID),
        task_id=str(task.id),
        version=2,
        created_at=T0 + timedelta(seconds=1),
        from_actor=HandoffActor(kind=HandoffActorKind.HUMAN, actor_id=HUMAN.actor_id),
        to_actor=HandoffActor(kind=HandoffActorKind.OPERATOR, actor_id=OPERATOR.actor_id),
        task=HandoffTaskSnapshot(
            status=task.status,
            status_version=task.status_version,
            question=task.question,
            objective=task.objective,
        ),
        next_action="Continue the analysis.",
    )
    return HandoffRecord(
        handoff=handoff,
        idempotency_key="handoff-0001",
        request_sha256="d" * 64,
        created_by=HandoffActor(kind=HandoffActorKind.HUMAN, actor_id=HUMAN.actor_id),
    )


def _contribution(task: InvestigationTaskV1) -> WorkContributionV1:
    return WorkContributionV1(
        investigation_id=INVESTIGATION_ID,
        workspace_id="ws-" + "1" * 32,
        workspace_revision_id="rev-" + "2" * 32,
        file_relpath="notes/analyste.md",
        file_sha256=FILE_SHA,
        observed_at=T0 + timedelta(seconds=2),
        origin=WorkContributionOrigin.HUMAN,
        origin_actor_id=HUMAN.actor_id,
        idempotency_key="contribution-0001",
        task_id=task.id,
        content_blob_id=uuid4(),
        content_sha256=FILE_SHA,
    )


def _session(task: InvestigationTaskV1, *, handoff_id: UUID | None = None) -> AgentSessionV1:
    capabilities = _capabilities()
    return AgentSessionV1(
        investigation_id=INVESTIGATION_ID,
        task_id=task.id,
        adapter_kind=OperatorAdapterKind.CODEX_CLI,
        state=AgentSessionState.RUNNING,
        state_version=3,
        capability_probe=capabilities.to_payload(),
        capability_sha256=capabilities.capability_sha256,
        probed_at=capabilities.probed_at,
        auth_secret_nonce=b"s" * 32,
        auth_secret_hash=SESSION_SECRET_HASH,
        auth_secret_issued_at=T0,
        opened_at=T0 + timedelta(seconds=3),
        idempotency_key="session-0001",
        created_at=T0,
        updated_at=T0 + timedelta(seconds=3),
        handoff_id=handoff_id,
    )


def _instruction(
    task: InvestigationTaskV1,
    session: AgentSessionV1,
    *,
    state: InstructionState = InstructionState.QUEUED,
    delivery_token: UUID | None = None,
) -> AgentSessionInstructionV1:
    queued_at = T0 + timedelta(seconds=4)
    values: dict[str, object] = {
        "investigation_id": INVESTIGATION_ID,
        "task_id": task.id,
        "session_id": session.id,
        "idempotency_key": f"instruction-{state.value}",
        "text": INSTRUCTION_TEXT,
        "text_sha256": hashlib.sha256(INSTRUCTION_TEXT.encode()).hexdigest(),
        "state": state,
        "created_by_kind": "human",
        "created_by_id": HUMAN.actor_id,
        "queued_at": queued_at,
        "created_at": queued_at,
        "updated_at": queued_at,
    }
    if state is InstructionState.DELIVERED:
        values.update(
            delivered_at=queued_at + timedelta(seconds=1),
            delivery_attempts=1,
            delivery_token=delivery_token,
            delivery_mode=InstructionDeliveryMode.LIVE,
            delivery_sha256="c" * 64,
        )
    return AgentSessionInstructionV1(**values)  # type: ignore[arg-type]


def _planner(
    *,
    tasks: InMemoryInvestigationTaskRepository | None = None,
    decisions: _Decisions | None = None,
    observations: InMemoryEvidenceObservationRepository | None = None,
    invocations: InMemoryToolInvocationRepository | None = None,
    plans: InMemoryInvestigationPlanRepository | None = None,
    extra_sources: tuple[TimelineSource, ...] = (),
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


def test_timeline_projects_handoff_contribution_session_instruction() -> None:
    """B7 oracle: the four new sources project their canonical rows."""

    async def scenario() -> None:
        tasks = InMemoryInvestigationTaskRepository()
        handoffs = InMemoryInvestigationHandoffRepository()
        contributions = InMemoryWorkContributionRepository()
        sessions = InMemoryAgentSessionRepository()
        observations = InMemoryEvidenceObservationRepository()
        task = await tasks.add(_task())

        record = _handoff(task)
        await handoffs.append_if_absent(record)
        contribution = _contribution(task)
        await contributions.append_if_absent(contribution)
        session = _session(task, handoff_id=UUID(record.handoff.handoff_id))
        await sessions.add(session)
        delivery_token = uuid4()
        delivered = _instruction(
            task,
            session,
            state=InstructionState.DELIVERED,
            delivery_token=delivery_token,
        )
        await sessions.add_instruction(delivered)
        queued = _instruction(task, session)
        await sessions.add_instruction(queued)

        service = _planner(
            tasks=tasks,
            observations=observations,
            extra_sources=(
                HandoffTimelineSource(handoffs),
                ContributionTimelineSource(contributions),
                AgentSessionTimelineSource(sessions),
                InstructionTimelineSource(sessions),
            ),
        )
        items = await service.items(INVESTIGATION_ID)
        by_id = {item.object_id: item for item in items}

        handoff_item = by_id[UUID(record.handoff.handoff_id)]
        assert handoff_item.item_type is TimelineItemType.HANDOFF
        assert handoff_item.task_id == task.id
        assert handoff_item.occurred_at == record.handoff.created_at
        assert handoff_item.actor_kind is TimelineActorKind.HUMAN
        assert handoff_item.actor_id == HUMAN.actor_id
        assert handoff_item.summary == "handoff:v2:human->operator"
        assert handoff_item.evidence is False
        assert handoff_item.refs["handoff_id"] == record.handoff.handoff_id
        assert handoff_item.refs["task_id"] == str(task.id)
        assert handoff_item.refs["version"] == "2"
        assert handoff_item.refs["sha256"] == record.handoff.content_sha256

        contribution_item = by_id[contribution.id]
        assert contribution_item.item_type is TimelineItemType.CONTRIBUTION
        assert contribution_item.task_id == task.id
        assert contribution_item.occurred_at == contribution.observed_at
        assert contribution_item.actor_kind is TimelineActorKind.HUMAN
        assert contribution_item.actor_id == HUMAN.actor_id
        assert contribution_item.summary == "contribution:work_contribution:imported"
        assert contribution_item.evidence is False
        assert contribution_item.refs["file_relpath"] == "notes/analyste.md"
        assert contribution_item.refs["file_sha256"] == FILE_SHA

        session_item = by_id[session.id]
        assert session_item.item_type is TimelineItemType.AGENT_SESSION
        assert session_item.task_id == task.id
        assert session_item.occurred_at == session.opened_at
        assert session_item.actor_kind is TimelineActorKind.SYSTEM
        assert session_item.actor_id == OperatorAdapterKind.CODEX_CLI.value
        assert session_item.summary == "agent_session:codex_cli:running"
        assert session_item.evidence is False
        assert session_item.refs["handoff_id"] == str(session.handoff_id)
        assert session_item.refs["state_version"] == "3"

        delivered_item = by_id[delivered.id]
        assert delivered_item.item_type is TimelineItemType.INSTRUCTION
        assert delivered_item.task_id == task.id
        assert delivered_item.occurred_at == delivered.queued_at
        assert delivered_item.actor_kind is TimelineActorKind.HUMAN
        assert delivered_item.actor_id == HUMAN.actor_id
        assert delivered_item.summary == "instruction:delivered"
        assert delivered_item.evidence is False
        assert delivered_item.refs["session_id"] == str(session.id)
        assert delivered_item.refs["text_sha256"] == delivered.text_sha256
        assert delivered_item.refs["delivery_mode"] == "live"
        assert delivered_item.refs["delivery_sha256"] == "c" * 64

        queued_item = by_id[queued.id]
        assert queued_item.item_type is TimelineItemType.INSTRUCTION
        assert queued_item.summary == "instruction:queued"
        assert queued_item.evidence is False
        assert (
            queued_item.refs["text_sha256"] == hashlib.sha256(INSTRUCTION_TEXT.encode()).hexdigest()
        )

        projected = [handoff_item, contribution_item, session_item, delivered_item, queued_item]
        for item in projected:
            assert 1 <= len(item.summary) <= 200
            assert all(isinstance(value, str) for value in item.refs.values())
        # Session authentication material and the delivery token never enter
        # the projection; the human instruction text stays on the B6 summary.
        serialized = json.dumps([item.model_dump(mode="json") for item in projected])
        assert str(delivery_token) not in serialized
        assert SESSION_SECRET_HASH not in serialized
        assert INSTRUCTION_TEXT not in serialized
        for forbidden in ("auth_secret", "auth_secret_hash", "auth_secret_nonce", "delivery_token"):
            assert forbidden not in serialized

    asyncio.run(scenario())


def test_new_items_are_non_evidence() -> None:
    """B7 oracle: the new sources never promote an item to evidence.

    An observation-category contribution points at its canonical observation:
    only that ``observation`` item keeps ``evidence: true``.
    """

    async def scenario() -> None:
        tasks = InMemoryInvestigationTaskRepository()
        handoffs = InMemoryInvestigationHandoffRepository()
        contributions = InMemoryWorkContributionRepository()
        sessions = InMemoryAgentSessionRepository()
        observations = InMemoryEvidenceObservationRepository()
        task = await tasks.add(_task())
        canonical = _observation(observed_at=T0 + timedelta(seconds=5))
        await observations.append_if_absent(canonical)

        await handoffs.append_if_absent(_handoff(task))
        imported_observation = WorkContributionV1(
            investigation_id=INVESTIGATION_ID,
            workspace_id="ws-" + "1" * 32,
            workspace_revision_id="rev-" + "2" * 32,
            file_relpath="results/strings.txt",
            file_sha256=FILE_SHA,
            observed_at=T0 + timedelta(seconds=2),
            origin=WorkContributionOrigin.HUMAN,
            origin_actor_id=HUMAN.actor_id,
            idempotency_key="contribution-observation-0001",
            category=WorkContributionCategory.OBSERVATION,
            task_id=task.id,
            content_blob_id=uuid4(),
            content_sha256=FILE_SHA,
            observation_id=canonical.id,
        )
        await contributions.append_if_absent(imported_observation)
        session = _session(task)
        await sessions.add(session)
        await sessions.add_instruction(_instruction(task, session))

        service = _planner(
            tasks=tasks,
            observations=observations,
            extra_sources=(
                HandoffTimelineSource(handoffs),
                ContributionTimelineSource(contributions),
                AgentSessionTimelineSource(sessions),
                InstructionTimelineSource(sessions),
            ),
        )
        items = await service.items(INVESTIGATION_ID)

        by_observation = {item.object_id: item for item in items}
        assert by_observation[canonical.id].item_type is TimelineItemType.OBSERVATION
        assert by_observation[canonical.id].evidence is True
        new_items = [
            item
            for item in items
            if item.item_type
            in {
                TimelineItemType.HANDOFF,
                TimelineItemType.CONTRIBUTION,
                TimelineItemType.AGENT_SESSION,
                TimelineItemType.INSTRUCTION,
            }
        ]
        assert {item.item_type for item in new_items} == {
            TimelineItemType.HANDOFF,
            TimelineItemType.CONTRIBUTION,
            TimelineItemType.AGENT_SESSION,
            TimelineItemType.INSTRUCTION,
        }
        assert all(item.evidence is False for item in new_items)
        # The imported observation is referenced, never duplicated as evidence.
        contribution_item = by_observation[imported_observation.id]
        assert contribution_item.refs["observation_id"] == str(canonical.id)
        assert contribution_item.evidence is False

    asyncio.run(scenario())


def test_existing_order_and_cursor_stay_stable_with_extra_sources() -> None:
    """Extra sources interleave but never perturb the existing keyset order."""

    async def scenario() -> None:
        tasks = InMemoryInvestigationTaskRepository()
        handoffs = InMemoryInvestigationHandoffRepository()
        contributions = InMemoryWorkContributionRepository()
        sessions = InMemoryAgentSessionRepository()
        observations = InMemoryEvidenceObservationRepository()
        task = await tasks.add(_task())
        await _append_event(
            tasks,
            task,
            idempotency_key="created",
            occurred_at=T0,
            to_status=TaskStatus.PROPOSED,
        )
        record = _handoff(task)
        await handoffs.append_if_absent(record)
        contribution = _contribution(task)
        await contributions.append_if_absent(contribution)
        session = _session(task)
        await sessions.add(session)
        queued = _instruction(task, session)
        await sessions.add_instruction(queued)
        canonical = _observation(observed_at=T0 + timedelta(seconds=5))
        await observations.append_if_absent(canonical)

        base_items = await _planner(tasks=tasks, observations=observations).items(INVESTIGATION_ID)
        service = _planner(
            tasks=tasks,
            observations=observations,
            extra_sources=(
                HandoffTimelineSource(handoffs),
                ContributionTimelineSource(contributions),
                AgentSessionTimelineSource(sessions),
                InstructionTimelineSource(sessions),
            ),
        )
        all_items = await service.items(INVESTIGATION_ID)
        existing_types = {TimelineItemType.TASK_EVENT, TimelineItemType.OBSERVATION}
        existing_order = [
            item.object_id
            for item in sorted(
                (item for item in all_items if item.item_type in existing_types),
                key=lambda item: (item.occurred_at, item.object_id),
            )
        ]
        assert existing_order == [
            item.object_id
            for item in sorted(base_items, key=lambda item: (item.occurred_at, item.object_id))
        ]

        pages: list[TimelineItemV1] = []
        cursor: str | None = None
        while True:
            page = await service.page(INVESTIGATION_ID, cursor=cursor, limit=2)
            pages.extend(page.items)
            if not page.has_more:
                assert page.next_cursor is None
                break
            assert page.next_cursor is not None
            cursor = page.next_cursor

        expected = sorted(all_items, key=lambda item: (item.occurred_at, item.object_id))
        assert len(pages) == 6
        assert [item.object_id for item in pages] == [item.object_id for item in expected]
        keys = [(item.occurred_at, item.object_id) for item in pages]
        assert keys == sorted(keys)
        assert len({item.object_id for item in pages}) == len(pages)

    asyncio.run(scenario())
