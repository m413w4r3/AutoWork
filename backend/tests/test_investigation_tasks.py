"""Domain oracles for the M24 investigation task aggregate (ADR-0011 D3).

The transition matrix is the D3 table as data: every (status, status, actor)
triple is exercised against a literal copy of the ADR table, so an accidentally
widened or narrowed transition fails here before any engine exists.  The
verification helpers encode M5: ``pending_review`` only exists on
``awaiting_review``, ``accepted`` only on ``completed``, and every exit from
``awaiting_review`` writes ``rejected``.  Events are exercised through the
in-memory repository because ``append_task_event`` is the sole writer of
``event_sequence``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID, uuid4

import pytest

from cti_app.application.investigation_persistence import (
    AgentSessionRepository,
    InMemoryAgentSessionRepository,
    InMemoryInvestigationTaskRepository,
    InvestigationTaskRepository,
)
from cti_app.application.investigation_task_events import (
    TaskEventConflictError,
    TaskEventDraft,
    TaskEventType,
    TaskNotFoundError,
    append_task_event,
)
from cti_app.application.investigation_task_sweep import (
    InvestigationTaskSweeper,
    recover_orphan_agent_sessions,
    run_expired_lease_sweep,
)
from cti_app.application.investigation_tasks import (
    InvestigationTaskService,
    StaleTaskVersionError,
    TaskCommandError,
    TaskDependencyError,
    TaskIdempotencyConflictError,
    TaskKeyConflictError,
    TaskLeaseExpiredError,
    TaskLeaseFenceError,
    TaskLimitExceededError,
    TaskPermissionError,
    TaskSessionInactiveError,
    TaskSessionOutcome,
    TaskSessionRelease,
)
from cti_app.config import Settings
from cti_app.domain.agentic_operator import (
    AgentSessionState,
    AgentSessionV1,
    OperatorAdapterKind,
    OperatorCapabilitiesV1,
    OperatorProbeCommand,
)
from cti_app.domain.editorial import (
    AnalystDecision,
    AnalystDecisionTargetType,
    AnalystDecisionType,
)
from cti_app.domain.investigation_task import (
    TASK_LIMIT_DEFAULTS,
    BlockedReason,
    InvestigationTaskV1,
    ResultVerificationStatus,
    TaskActor,
    TaskActorKind,
    TaskLease,
    TaskLimits,
    TaskPriority,
    TaskStateError,
    TaskStatus,
    TaskTransitionContext,
    TaskTransitionError,
    result_verification_after_transition,
    validate_transition,
)
from cti_app.infrastructure.database.models.investigation_tasks import (
    TASK_BLOCKED_REASON_VALUES_SQL,
    TASK_EVENT_TYPE_VALUES_SQL,
    TASK_PRIORITY_VALUES_SQL,
    TASK_RESULT_VERIFICATION_VALUES_SQL,
    TASK_STATUS_VALUES_SQL,
)

NOW = datetime(2026, 11, 22, 12, 0, tzinfo=UTC)

# ADR-0011 D3, transcribed literally: (from, to) -> the actor kinds the table
# names.  Conditions are satisfied by ``_satisfied_context`` below so this
# oracle isolates actor legality; a separate test covers the conditions.
_D3_ALLOWED: dict[tuple[TaskStatus, TaskStatus], frozenset[TaskActorKind]] = {
    (TaskStatus.PROPOSED, TaskStatus.READY): frozenset({TaskActorKind.HUMAN, TaskActorKind.SYSTEM}),
    (TaskStatus.PROPOSED, TaskStatus.BLOCKED): frozenset({TaskActorKind.SYSTEM}),
    (TaskStatus.PROPOSED, TaskStatus.CANCELLED): frozenset({TaskActorKind.HUMAN}),
    (TaskStatus.READY, TaskStatus.ASSIGNED): frozenset({TaskActorKind.HUMAN}),
    (TaskStatus.READY, TaskStatus.CANCELLED): frozenset({TaskActorKind.HUMAN}),
    (TaskStatus.ASSIGNED, TaskStatus.READY): frozenset({TaskActorKind.HUMAN}),
    (TaskStatus.ASSIGNED, TaskStatus.RUNNING): frozenset(
        {TaskActorKind.OPERATOR, TaskActorKind.HUMAN}
    ),
    (TaskStatus.ASSIGNED, TaskStatus.CANCELLED): frozenset({TaskActorKind.HUMAN}),
    (TaskStatus.RUNNING, TaskStatus.AWAITING_INPUT): frozenset({TaskActorKind.OPERATOR}),
    (TaskStatus.RUNNING, TaskStatus.AWAITING_REVIEW): frozenset(
        {TaskActorKind.OPERATOR, TaskActorKind.HUMAN}
    ),
    (TaskStatus.RUNNING, TaskStatus.BLOCKED): frozenset(
        {TaskActorKind.OPERATOR, TaskActorKind.HUMAN, TaskActorKind.SYSTEM}
    ),
    (TaskStatus.RUNNING, TaskStatus.FAILED): frozenset(
        {TaskActorKind.OPERATOR, TaskActorKind.HUMAN, TaskActorKind.SYSTEM}
    ),
    (TaskStatus.RUNNING, TaskStatus.CANCELLED): frozenset({TaskActorKind.HUMAN}),
    (TaskStatus.AWAITING_INPUT, TaskStatus.RUNNING): frozenset({TaskActorKind.OPERATOR}),
    (TaskStatus.AWAITING_INPUT, TaskStatus.BLOCKED): frozenset(
        {TaskActorKind.OPERATOR, TaskActorKind.SYSTEM, TaskActorKind.HUMAN}
    ),
    (TaskStatus.AWAITING_INPUT, TaskStatus.FAILED): frozenset(
        {TaskActorKind.OPERATOR, TaskActorKind.SYSTEM}
    ),
    (TaskStatus.AWAITING_INPUT, TaskStatus.CANCELLED): frozenset({TaskActorKind.HUMAN}),
    (TaskStatus.AWAITING_REVIEW, TaskStatus.COMPLETED): frozenset({TaskActorKind.HUMAN}),
    (TaskStatus.AWAITING_REVIEW, TaskStatus.READY): frozenset({TaskActorKind.HUMAN}),
    (TaskStatus.AWAITING_REVIEW, TaskStatus.FAILED): frozenset({TaskActorKind.HUMAN}),
    (TaskStatus.AWAITING_REVIEW, TaskStatus.CANCELLED): frozenset({TaskActorKind.HUMAN}),
    (TaskStatus.BLOCKED, TaskStatus.READY): frozenset({TaskActorKind.HUMAN, TaskActorKind.SYSTEM}),
    (TaskStatus.BLOCKED, TaskStatus.ASSIGNED): frozenset({TaskActorKind.HUMAN}),
    (TaskStatus.BLOCKED, TaskStatus.CANCELLED): frozenset({TaskActorKind.HUMAN}),
    (TaskStatus.BLOCKED, TaskStatus.FAILED): frozenset({TaskActorKind.HUMAN, TaskActorKind.SYSTEM}),
}


def _satisfied_context(**overrides: object) -> TaskTransitionContext:
    values: dict[str, object] = {
        "current_assigned_actor_kind": TaskActorKind.HUMAN,
        "current_blocked_reason": BlockedReason.DEPENDENCY,
        "dependencies_completed": True,
        "target_blocked_reason": BlockedReason.DEPENDENCY,
    }
    values.update(overrides)
    return TaskTransitionContext(**values)


def _task(
    *,
    status: TaskStatus = TaskStatus.READY,
    verification: ResultVerificationStatus = ResultVerificationStatus.UNVERIFIED,
    assigned_actor_kind: TaskActorKind | None = None,
    assigned_actor_id: str | None = None,
    lease: TaskLease | None = None,
    blocked_reason: BlockedReason | None = None,
    parent_task_id: UUID | None = None,
    depth: int = 0,
    status_version: int = 1,
    **overrides: object,
) -> InvestigationTaskV1:
    values: dict[str, object] = {
        "investigation_id": uuid4(),
        "task_key": f"lead-{uuid4().hex[:12]}",
        "title": "Inspect the loader",
        "question": "Which gate contacts the C2?",
        "objective": "Identify the second-stage downloader.",
        "priority": TaskPriority.NORMAL,
        "status": status,
        "status_version": status_version,
        "result_verification_status": verification,
        "created_by": TaskActor(kind=TaskActorKind.SYSTEM, actor_id="plan_accept:test"),
        "created_at": NOW,
        "updated_at": NOW,
        "assigned_actor_kind": assigned_actor_kind,
        "assigned_actor_id": assigned_actor_id,
        "blocked_reason": blocked_reason,
        "parent_task_id": parent_task_id,
        "depth": depth,
    }
    if lease is not None:
        values.update(
            lease_id=lease.lease_id,
            lease_session_id=lease.lease_session_id,
            lease_fence=lease.lease_fence,
            lease_expires_at=lease.lease_expires_at,
            heartbeat_at=lease.heartbeat_at,
        )
    values.update(overrides)
    return InvestigationTaskV1(**values)


def _lease(*, fence: int = 1) -> TaskLease:
    return TaskLease(
        lease_id=uuid4(),
        lease_session_id=uuid4(),
        lease_fence=fence,
        lease_expires_at=NOW + timedelta(seconds=180),
        heartbeat_at=NOW,
    )


HUMAN = TaskActor(kind=TaskActorKind.HUMAN, actor_id="analyst:test")
OPERATOR = TaskActor(kind=TaskActorKind.OPERATOR, actor_id="operator:test")


class _SessionRecorder:
    """In-memory ``TaskSessionPort`` fake; sessions are 05B1's aggregate."""

    def __init__(self, *, active: tuple[UUID, ...] = ()) -> None:
        self.active = set(active)
        self.closed: list[tuple[UUID, UUID, TaskSessionOutcome, str]] = []

    async def is_active_session(
        self, *, session_id: UUID, investigation_id: UUID, task_id: UUID
    ) -> bool:
        return session_id in self.active

    async def close_session(
        self,
        *,
        session_id: UUID,
        investigation_id: UUID,
        task_id: UUID,
        outcome: TaskSessionOutcome,
        reason: str,
    ) -> None:
        self.closed.append((session_id, task_id, outcome, reason))
        self.active.discard(session_id)


class _DecisionRecorder:
    """In-memory ``AnalystDecisionRepository`` fake."""

    def __init__(self) -> None:
        self.decisions: list[AnalystDecision] = []

    async def append(self, decision: AnalystDecision) -> None:
        self.decisions.append(decision)

    async def list_for_investigation(self, investigation_id: UUID) -> list[AnalystDecision]:
        return [
            decision for decision in self.decisions if decision.investigation_id == investigation_id
        ]


def _task_service(
    repository: InMemoryInvestigationTaskRepository,
    *,
    sessions: _SessionRecorder | None = None,
    decisions: _DecisionRecorder | None = None,
    limits: TaskLimits | None = None,
) -> InvestigationTaskService:
    return InvestigationTaskService(
        tasks=repository,
        decisions=decisions if decisions is not None else _DecisionRecorder(),
        sessions=sessions if sessions is not None else _SessionRecorder(),
        limits=limits if limits is not None else TaskLimits(),
    )


async def _seeded(
    repository: InMemoryInvestigationTaskRepository,
    *,
    status: TaskStatus = TaskStatus.READY,
    investigation_id: UUID | None = None,
    **overrides: object,
) -> InvestigationTaskV1:
    task = _task(
        status=status,
        investigation_id=investigation_id if investigation_id is not None else uuid4(),
        **overrides,
    )
    return await repository.add(task)


async def _running_operator_task(
    repository: InMemoryInvestigationTaskRepository,
    *,
    session_id: UUID,
    now: datetime = NOW,
) -> InvestigationTaskV1:
    """Seed an assigned operator task and start it through the engine."""
    task = await _seeded(
        repository,
        status=TaskStatus.ASSIGNED,
        investigation_id=uuid4(),
        assigned_actor_kind=TaskActorKind.OPERATOR,
        assigned_actor_id=OPERATOR.actor_id,
    )
    service = _task_service(repository, sessions=_SessionRecorder(active=(session_id,)))
    started = await service.start(
        task_id=task.id,
        actor=OPERATOR,
        expected_status_version=task.status_version,
        idempotency_key=f"start:{task.id}",
        session_id=session_id,
        now=now,
    )
    return started.task


def test_transition_matrix_allows_only_declared_actors() -> None:
    context = _satisfied_context()
    for from_status in TaskStatus:
        for to_status in TaskStatus:
            expected_actors = _D3_ALLOWED.get((from_status, to_status), frozenset())
            for actor_kind in TaskActorKind:
                if actor_kind in expected_actors:
                    validate_transition(from_status, to_status, actor_kind, context)
                else:
                    with pytest.raises(TaskTransitionError):
                        validate_transition(from_status, to_status, actor_kind, context)


def test_terminal_statuses_have_no_outgoing_transition() -> None:
    context = _satisfied_context()
    for terminal in (TaskStatus.COMPLETED, TaskStatus.CANCELLED, TaskStatus.FAILED):
        for to_status in TaskStatus:
            for actor_kind in TaskActorKind:
                with pytest.raises(TaskTransitionError):
                    validate_transition(terminal, to_status, actor_kind, context)


def test_system_promotion_requires_satisfied_dependencies() -> None:
    dependent = _satisfied_context(dependencies_completed=False)
    with pytest.raises(TaskTransitionError):
        validate_transition(TaskStatus.PROPOSED, TaskStatus.READY, TaskActorKind.SYSTEM, dependent)
    with pytest.raises(TaskTransitionError):
        validate_transition(TaskStatus.PROPOSED, TaskStatus.READY, TaskActorKind.HUMAN, dependent)
    validate_transition(
        TaskStatus.PROPOSED,
        TaskStatus.READY,
        TaskActorKind.SYSTEM,
        _satisfied_context(dependencies_completed=True),
    )

    validate_transition(
        TaskStatus.BLOCKED,
        TaskStatus.READY,
        TaskActorKind.SYSTEM,
        _satisfied_context(current_blocked_reason=BlockedReason.DEPENDENCY),
    )
    with pytest.raises(TaskTransitionError):
        validate_transition(
            TaskStatus.BLOCKED,
            TaskStatus.READY,
            TaskActorKind.SYSTEM,
            _satisfied_context(current_blocked_reason=BlockedReason.CAPABILITY),
        )
    with pytest.raises(TaskTransitionError):
        validate_transition(
            TaskStatus.BLOCKED,
            TaskStatus.READY,
            TaskActorKind.SYSTEM,
            _satisfied_context(dependencies_completed=False),
        )
    # A human may still unblock a task whose block was not dependency-driven.
    validate_transition(
        TaskStatus.BLOCKED,
        TaskStatus.READY,
        TaskActorKind.HUMAN,
        _satisfied_context(current_blocked_reason=BlockedReason.CAPABILITY),
    )


def test_proposed_block_requires_dependency_reason() -> None:
    validate_transition(
        TaskStatus.PROPOSED, TaskStatus.BLOCKED, TaskActorKind.SYSTEM, _satisfied_context()
    )
    with pytest.raises(TaskTransitionError):
        validate_transition(
            TaskStatus.PROPOSED,
            TaskStatus.BLOCKED,
            TaskActorKind.SYSTEM,
            _satisfied_context(target_blocked_reason=BlockedReason.EXTERNAL),
        )


def test_human_operator_conditional_transitions() -> None:
    human = _satisfied_context(current_assigned_actor_kind=TaskActorKind.HUMAN)
    operator = _satisfied_context(current_assigned_actor_kind=TaskActorKind.OPERATOR)
    validate_transition(TaskStatus.ASSIGNED, TaskStatus.RUNNING, TaskActorKind.HUMAN, human)
    with pytest.raises(TaskTransitionError):
        validate_transition(TaskStatus.ASSIGNED, TaskStatus.RUNNING, TaskActorKind.HUMAN, operator)
    validate_transition(TaskStatus.ASSIGNED, TaskStatus.RUNNING, TaskActorKind.OPERATOR, operator)
    validate_transition(TaskStatus.RUNNING, TaskStatus.AWAITING_REVIEW, TaskActorKind.HUMAN, human)
    with pytest.raises(TaskTransitionError):
        validate_transition(
            TaskStatus.RUNNING, TaskStatus.AWAITING_REVIEW, TaskActorKind.HUMAN, operator
        )
    # An operator may never assign, accept a result or cancel a task.
    for from_status, to_status in (
        (TaskStatus.READY, TaskStatus.ASSIGNED),
        (TaskStatus.AWAITING_REVIEW, TaskStatus.COMPLETED),
        (TaskStatus.RUNNING, TaskStatus.CANCELLED),
        (TaskStatus.BLOCKED, TaskStatus.ASSIGNED),
    ):
        with pytest.raises(TaskTransitionError):
            validate_transition(from_status, to_status, TaskActorKind.OPERATOR, human)


def test_human_can_take_start_and_submit_task() -> None:
    task = _task()
    validate_transition(TaskStatus.READY, TaskStatus.ASSIGNED, TaskActorKind.HUMAN)
    task = task.with_assignment(
        TaskActor(kind=TaskActorKind.HUMAN, actor_id="analyst:test"), now=NOW
    )
    assert task.status is TaskStatus.ASSIGNED
    assert task.assigned_actor_kind is TaskActorKind.HUMAN
    assert task.lease is None

    validate_transition(
        TaskStatus.ASSIGNED,
        TaskStatus.RUNNING,
        TaskActorKind.HUMAN,
        TaskTransitionContext.for_task(task),
    )
    task = task.with_status(TaskStatus.RUNNING, now=NOW)
    assert task.status is TaskStatus.RUNNING
    assert task.status_version == 3

    validate_transition(
        TaskStatus.RUNNING,
        TaskStatus.AWAITING_REVIEW,
        TaskActorKind.HUMAN,
        TaskTransitionContext.for_task(task),
    )
    task = task.with_status(TaskStatus.AWAITING_REVIEW, now=NOW)
    assert task.result_verification_status is ResultVerificationStatus.PENDING_REVIEW

    validate_transition(TaskStatus.AWAITING_REVIEW, TaskStatus.COMPLETED, TaskActorKind.HUMAN)
    task = task.with_status(TaskStatus.COMPLETED, now=NOW)
    assert task.result_verification_status is ResultVerificationStatus.ACCEPTED
    assert task.is_terminal

    async def engine_scenario() -> None:
        repository = InMemoryInvestigationTaskRepository()
        decisions = _DecisionRecorder()
        seeded = _task(status=TaskStatus.READY, investigation_id=uuid4())
        await repository.add(seeded)
        service = _task_service(repository, decisions=decisions)

        assigned = await service.assign(
            task_id=seeded.id,
            actor=HUMAN,
            target=HUMAN,
            expected_status_version=1,
            idempotency_key="assign-1",
            now=NOW,
        )
        assert assigned.task.status is TaskStatus.ASSIGNED
        assert assigned.task.assigned_actor_id == HUMAN.actor_id
        assert assigned.task.lease is None

        started = await service.start(
            task_id=seeded.id,
            actor=HUMAN,
            expected_status_version=2,
            idempotency_key="start-1",
            now=NOW + timedelta(seconds=10),
        )
        assert started.task.status is TaskStatus.RUNNING
        assert started.task.lease is None

        submitted = await service.submit(
            task_id=seeded.id,
            actor=HUMAN,
            expected_status_version=3,
            idempotency_key="submit-1",
            now=NOW + timedelta(seconds=20),
        )
        assert submitted.task.status is TaskStatus.AWAITING_REVIEW
        assert submitted.task.result_verification_status is ResultVerificationStatus.PENDING_REVIEW

        accepted = await service.accept_result(
            task_id=seeded.id,
            actor=HUMAN,
            expected_status_version=4,
            idempotency_key="accept-1",
            reason="result verified against the observations",
            now=NOW + timedelta(seconds=30),
        )
        assert accepted.task.status is TaskStatus.COMPLETED
        assert accepted.task.result_verification_status is ResultVerificationStatus.ACCEPTED
        assert accepted.event is not None
        assert len(decisions.decisions) == 1
        decision = decisions.decisions[0]
        assert decision.decision_type is AnalystDecisionType.TASK_RESULT_ACCEPT
        assert decision.target_type is AnalystDecisionTargetType.TASK
        assert decision.target_id == seeded.id
        assert accepted.event.analyst_decision_id == decision.id

    asyncio.run(engine_scenario())


def test_awaiting_review_exit_forces_rejected() -> None:
    pending = ResultVerificationStatus.PENDING_REVIEW
    for target in (TaskStatus.READY, TaskStatus.FAILED, TaskStatus.CANCELLED):
        assert (
            result_verification_after_transition(
                TaskStatus.AWAITING_REVIEW, target, current=pending
            )
            is ResultVerificationStatus.REJECTED
        )
    assert (
        result_verification_after_transition(
            TaskStatus.RUNNING,
            TaskStatus.AWAITING_REVIEW,
            current=ResultVerificationStatus.UNVERIFIED,
        )
        is ResultVerificationStatus.PENDING_REVIEW
    )
    assert (
        result_verification_after_transition(
            TaskStatus.AWAITING_REVIEW, TaskStatus.COMPLETED, current=pending
        )
        is ResultVerificationStatus.ACCEPTED
    )
    assert (
        result_verification_after_transition(
            TaskStatus.READY, TaskStatus.ASSIGNED, current=ResultVerificationStatus.UNVERIFIED
        )
        is ResultVerificationStatus.UNVERIFIED
    )

    task = _task(
        status=TaskStatus.AWAITING_REVIEW,
        verification=ResultVerificationStatus.PENDING_REVIEW,
    )
    ready = task.with_status(TaskStatus.READY, now=NOW)
    assert ready.result_verification_status is ResultVerificationStatus.REJECTED
    assert ready.blocked_reason is None


def test_task_state_invariants_are_enforced() -> None:
    with pytest.raises(TaskStateError):
        _task(status=TaskStatus.BLOCKED)
    with pytest.raises(TaskStateError):
        _task(status=TaskStatus.READY, blocked_reason=BlockedReason.DEPENDENCY)
    with pytest.raises(TaskStateError):
        _task(status=TaskStatus.COMPLETED)
    with pytest.raises(TaskStateError):
        _task(
            status=TaskStatus.AWAITING_REVIEW,
            verification=ResultVerificationStatus.UNVERIFIED,
        )
    with pytest.raises(TaskStateError):
        _task(
            status=TaskStatus.COMPLETED,
            verification=ResultVerificationStatus.ACCEPTED,
            assigned_actor_kind=TaskActorKind.OPERATOR,
            assigned_actor_id="operator:test",
            lease=_lease(),
        )
    with pytest.raises(TaskStateError):
        _task(
            assigned_actor_kind=TaskActorKind.HUMAN,
            assigned_actor_id="analyst:test",
            lease=_lease(),
        )
    with pytest.raises(TaskStateError):
        _task(assigned_actor_kind=TaskActorKind.HUMAN)
    with pytest.raises(TaskStateError):
        _task(depth=1)
    with pytest.raises(TaskStateError):
        _task(parent_task_id=uuid4(), depth=0)

    operator_task = _task(
        status=TaskStatus.RUNNING,
        assigned_actor_kind=TaskActorKind.OPERATOR,
        assigned_actor_id="operator:test",
        lease=_lease(),
    )
    assert operator_task.lease is not None
    with pytest.raises(TaskStateError):
        operator_task.with_status(TaskStatus.AWAITING_REVIEW, now=NOW)
    with pytest.raises(TaskStateError):
        operator_task.with_status(
            TaskStatus.BLOCKED, now=NOW, blocked_reason=BlockedReason.CAPABILITY
        )
    renewed_at = NOW + timedelta(seconds=60)
    renewed = operator_task.with_heartbeat(
        heartbeat_at=renewed_at, lease_expires_at=renewed_at + timedelta(seconds=180)
    )
    assert renewed.status_version == operator_task.status_version
    released = renewed.with_released_lease(now=renewed_at)
    assert released.lease is None
    assert released.lease_fence == operator_task.lease_fence + 1
    awaiting_review = released.with_status(TaskStatus.AWAITING_REVIEW, now=renewed_at)
    assert awaiting_review.result_verification_status is ResultVerificationStatus.PENDING_REVIEW


def test_task_limits_defaults_match_the_adr() -> None:
    limits = TaskLimits()
    assert limits.max_depth == 3
    assert limits.max_children == 20
    assert limits.max_dependencies == 16
    assert limits.max_active_per_investigation == 8
    assert limits.max_running_per_session == 1
    assert limits.lease_seconds == 180
    assert limits.max_contribution_bytes == 16 * 1024 * 1024
    assert TASK_LIMIT_DEFAULTS == limits
    with pytest.raises(ValueError):
        TaskLimits(lease_seconds=29)
    with pytest.raises(ValueError):
        TaskLimits(lease_seconds=3601)


def test_task_control_enums_match_sql_value_sets() -> None:
    import re

    def values(sql: str) -> set[str]:
        return set(re.findall(r"'([a-z_]+)'", sql))

    assert values(TASK_STATUS_VALUES_SQL) == {item.value for item in TaskStatus}
    assert values(TASK_PRIORITY_VALUES_SQL) == {item.value for item in TaskPriority}
    assert values(TASK_RESULT_VERIFICATION_VALUES_SQL) == {
        item.value for item in ResultVerificationStatus
    }
    assert values(TASK_BLOCKED_REASON_VALUES_SQL) == {item.value for item in BlockedReason}
    assert values(TASK_EVENT_TYPE_VALUES_SQL) == {item.value for item in TaskEventType}


def test_task_event_idempotency_is_scoped_per_task() -> None:
    async def scenario() -> None:
        repository = InMemoryInvestigationTaskRepository()
        first = _task(status=TaskStatus.PROPOSED)
        second = _task(status=TaskStatus.PROPOSED, investigation_id=first.investigation_id)
        await repository.add(first)
        await repository.add(second)

        draft = TaskEventDraft(
            investigation_id=first.investigation_id,
            task_id=first.id,
            event_type=TaskEventType.TASK_CREATED,
            actor=first.created_by,
            idempotency_key="plan:version:task",
            to_status=TaskStatus.PROPOSED,
        )
        event, created = await append_task_event(repository, draft, occurred_at=NOW)
        assert created is True
        assert event.sequence == 1
        assert event.status_version == first.status_version
        assert event.lease_fence == first.lease_fence
        stored = await repository.get(first.id)
        assert stored is not None and stored.event_sequence == 1

        replayed, created_again = await append_task_event(
            repository, draft, occurred_at=NOW + timedelta(seconds=5)
        )
        assert created_again is False
        assert replayed == event
        stored = await repository.get(first.id)
        assert stored is not None and stored.event_sequence == 1

        with pytest.raises(TaskEventConflictError):
            await append_task_event(
                repository,
                TaskEventDraft(
                    investigation_id=first.investigation_id,
                    task_id=first.id,
                    event_type=TaskEventType.TASK_CREATED,
                    actor=first.created_by,
                    idempotency_key="plan:version:task",
                    to_status=TaskStatus.READY,
                ),
                occurred_at=NOW,
            )

        other, created_other = await append_task_event(
            repository,
            TaskEventDraft(
                investigation_id=second.investigation_id,
                task_id=second.id,
                event_type=TaskEventType.TASK_CREATED,
                actor=second.created_by,
                idempotency_key="plan:version:task",
                to_status=TaskStatus.PROPOSED,
            ),
            occurred_at=NOW,
        )
        assert created_other is True
        assert other.sequence == 1

        with pytest.raises(TaskNotFoundError):
            await append_task_event(
                repository,
                TaskEventDraft(
                    investigation_id=uuid4(),
                    task_id=uuid4(),
                    event_type=TaskEventType.TASK_CREATED,
                    actor=first.created_by,
                    idempotency_key="missing",
                ),
                occurred_at=NOW,
            )

    asyncio.run(scenario())


async def _complete_human_task(
    service: InvestigationTaskService,
    task: InvestigationTaskV1,
    *,
    prefix: str,
    now: datetime = NOW,
) -> InvestigationTaskV1:
    version = task.status_version
    await service.assign(
        task_id=task.id,
        actor=HUMAN,
        target=HUMAN,
        expected_status_version=version,
        idempotency_key=f"{prefix}:assign",
        now=now,
    )
    await service.start(
        task_id=task.id,
        actor=HUMAN,
        expected_status_version=version + 1,
        idempotency_key=f"{prefix}:start",
        now=now,
    )
    await service.submit(
        task_id=task.id,
        actor=HUMAN,
        expected_status_version=version + 2,
        idempotency_key=f"{prefix}:submit",
        now=now,
    )
    accepted = await service.accept_result(
        task_id=task.id,
        actor=HUMAN,
        expected_status_version=version + 3,
        idempotency_key=f"{prefix}:accept",
        reason=f"{prefix} result verified",
        now=now,
    )
    return accepted.task


def test_cas_conflict_returns_stale_task_version() -> None:
    async def scenario() -> None:
        repository = InMemoryInvestigationTaskRepository()
        task = await _seeded(repository)
        service = _task_service(repository)

        assigned = await service.assign(
            task_id=task.id,
            actor=HUMAN,
            target=HUMAN,
            expected_status_version=1,
            idempotency_key="assign",
            now=NOW,
        )
        assert assigned.task.status_version == 2

        with pytest.raises(StaleTaskVersionError) as stale:
            await service.release(
                task_id=task.id,
                actor=HUMAN,
                expected_status_version=1,
                idempotency_key="release-stale",
                now=NOW,
            )
        assert stale.value.code == "stale_task_version"
        stored = await repository.get(task.id)
        assert stored is not None
        assert stored.status is TaskStatus.ASSIGNED
        assert stored.status_version == 2
        assert stored.event_sequence == 1

        released = await service.release(
            task_id=task.id,
            actor=HUMAN,
            expected_status_version=2,
            idempotency_key="release",
            now=NOW,
        )
        assert released.task.status is TaskStatus.READY
        assert released.task.assigned_actor_id is None

    asyncio.run(scenario())


def test_heartbeat_at_expiry_cannot_resurrect_lease() -> None:
    async def scenario() -> None:
        repository = InMemoryInvestigationTaskRepository()
        session_id = uuid4()
        task = await _running_operator_task(repository, session_id=session_id)
        assert task.status is TaskStatus.RUNNING
        assert task.lease_fence == 1
        assert task.lease_expires_at == NOW + timedelta(seconds=180)
        assert task.event_sequence == 1
        service = _task_service(repository)

        with pytest.raises(TaskLeaseExpiredError) as expired:
            await service.heartbeat(
                task_id=task.id,
                actor=OPERATOR,
                expected_status_version=task.status_version,
                lease_fence=1,
                idempotency_key="late-heartbeat",
                now=NOW + timedelta(seconds=180),
            )
        assert expired.value.code == "lease_expired"
        stored = await repository.get(task.id)
        assert stored is not None
        assert stored.status is TaskStatus.RUNNING
        assert stored.status_version == task.status_version
        assert stored.event_sequence == 1
        assert stored.lease_expires_at == task.lease_expires_at

        with pytest.raises(TaskLeaseFenceError) as stale_fence:
            await service.heartbeat(
                task_id=task.id,
                actor=OPERATOR,
                expected_status_version=task.status_version,
                lease_fence=2,
                idempotency_key="wrong-fence",
                now=NOW,
            )
        assert stale_fence.value.code == "stale_fence"

    asyncio.run(scenario())


def test_heartbeat_appends_event_single_sequence_writer() -> None:
    async def scenario() -> None:
        repository = InMemoryInvestigationTaskRepository(clock=lambda: NOW)
        session_id = uuid4()
        started = await _running_operator_task(repository, session_id=session_id)
        service = _task_service(repository)

        first = await service.heartbeat(
            task_id=started.id,
            actor=OPERATOR,
            expected_status_version=started.status_version,
            lease_fence=started.lease_fence,
            idempotency_key="heartbeat-1",
            now=NOW + timedelta(seconds=30),
        )
        assert first.task.status_version == started.status_version
        assert first.task.event_sequence == 2
        assert first.event is not None
        assert first.event.event_type is TaskEventType.LEASE_RENEWED
        assert first.event.status_version == started.status_version

        second = await service.heartbeat(
            task_id=started.id,
            actor=OPERATOR,
            expected_status_version=started.status_version,
            lease_fence=started.lease_fence,
            idempotency_key="heartbeat-2",
            now=NOW + timedelta(seconds=60),
        )
        assert second.task.event_sequence == 3

        replayed = await service.heartbeat(
            task_id=started.id,
            actor=OPERATOR,
            expected_status_version=started.status_version,
            lease_fence=started.lease_fence,
            idempotency_key="heartbeat-1",
            now=NOW + timedelta(seconds=90),
        )
        assert replayed.replayed is True
        assert replayed.event is not None
        assert replayed.event.id == first.event.id

        stored = await repository.get(started.id)
        assert stored is not None
        assert stored.event_sequence == 3
        assert stored.status_version == started.status_version
        events = await repository.list_events(started.id)
        assert [event.sequence for event in events] == [1, 2, 3]
        assert [event.event_type for event in events] == [
            TaskEventType.LEASE_ACQUIRED,
            TaskEventType.LEASE_RENEWED,
            TaskEventType.LEASE_RENEWED,
        ]

    asyncio.run(scenario())


def test_sweep_covers_awaiting_input() -> None:
    async def scenario() -> None:
        database_clock = [NOW]
        repository = InMemoryInvestigationTaskRepository(clock=lambda: database_clock[0])
        session_id = uuid4()
        sessions = _SessionRecorder(active=(session_id,))
        seeded = await _seeded(
            repository,
            status=TaskStatus.ASSIGNED,
            assigned_actor_kind=TaskActorKind.OPERATOR,
            assigned_actor_id=OPERATOR.actor_id,
        )
        service = _task_service(repository, sessions=sessions)
        started = await service.start(
            task_id=seeded.id,
            actor=OPERATOR,
            expected_status_version=1,
            idempotency_key="start",
            session_id=session_id,
            now=NOW,
        )
        awaiting = await service.request_input(
            task_id=seeded.id,
            actor=OPERATOR,
            expected_status_version=started.task.status_version,
            lease_fence=started.task.lease_fence,
            idempotency_key="request-input",
            now=NOW + timedelta(seconds=10),
        )
        assert awaiting.task.status is TaskStatus.AWAITING_INPUT
        assert awaiting.task.lease_fence == 1
        assert awaiting.task.lease_expires_at == NOW + timedelta(seconds=180)

        database_clock[0] = NOW + timedelta(seconds=181)
        sweeper = InvestigationTaskSweeper(tasks=repository, sessions=sessions)
        swept = await sweeper.sweep_investigation(seeded.investigation_id)
        assert len(swept) == 1
        blocked = swept[0].task
        assert blocked.status is TaskStatus.BLOCKED
        assert blocked.blocked_reason is BlockedReason.LEASE_EXPIRED
        assert blocked.lease is None
        assert blocked.lease_fence == 2
        assert swept[0].event is not None
        assert swept[0].event.event_type is TaskEventType.LEASE_EXPIRED
        assert swept[0].event.idempotency_key == (
            f"sweep:{seeded.id}:awaiting_input:{awaiting.task.status_version}"
        )
        assert sessions.closed == [
            (session_id, seeded.id, TaskSessionOutcome.LOST, "lease_expired")
        ]

        again = await sweeper.sweep_investigation(seeded.investigation_id)
        assert again == ()
        stored = await repository.get(seeded.id)
        assert stored is not None
        assert stored.event_sequence == 3

    asyncio.run(scenario())


def test_sweep_runner_commits_before_flushing_the_host_release() -> None:
    """B1/B2: the scheduler runner sweeps, commits, then asks the host to stop."""

    async def scenario() -> None:
        database_clock = [NOW]
        repository = InMemoryInvestigationTaskRepository(clock=lambda: database_clock[0])
        session_id = uuid4()
        seeded = await _seeded(
            repository,
            status=TaskStatus.ASSIGNED,
            assigned_actor_kind=TaskActorKind.OPERATOR,
            assigned_actor_id=OPERATOR.actor_id,
        )
        service = _task_service(repository, sessions=_SessionRecorder(active=(session_id,)))
        started = await service.start(
            task_id=seeded.id,
            actor=OPERATOR,
            expected_status_version=1,
            idempotency_key="start",
            session_id=session_id,
            now=NOW,
        )
        await service.request_input(
            task_id=seeded.id,
            actor=OPERATOR,
            expected_status_version=started.task.status_version,
            lease_fence=started.task.lease_fence,
            idempotency_key="request-input",
            now=NOW + timedelta(seconds=10),
        )

        class _AgentSessions:
            def __init__(self) -> None:
                self.closed: list[dict[str, object]] = []

            async def close_session(self, **values: object) -> None:
                self.closed.append(values)

        class _Uow:
            def __init__(self) -> None:
                self.investigation_tasks: InvestigationTaskRepository = repository
                self.agent_sessions = cast(AgentSessionRepository, agent_sessions)
                self.committed = False

            async def __aenter__(self) -> _Uow:
                return self

            async def __aexit__(self, *_args: object) -> None:
                return None

            async def commit(self) -> None:
                self.committed = True

        class _Source:
            async def list_expired_lease_investigation_ids(self, *, limit: int) -> tuple[UUID, ...]:
                assert limit == 5
                return (seeded.investigation_id,)

        agent_sessions = _AgentSessions()
        releases: list[TaskSessionRelease] = []
        uow = _Uow()

        async def flush(items: Sequence[TaskSessionRelease]) -> None:
            assert uow.committed is True, "the DB release must commit before the host call"
            assert agent_sessions.closed, "the session row must be released before teardown"
            releases.extend(items)

        database_clock[0] = NOW + timedelta(seconds=181)
        swept = await run_expired_lease_sweep(
            source=_Source(),
            uow_factory=lambda: uow,
            flush_releases=flush,
            batch_size=5,
        )
        assert swept == 1
        assert len(releases) == 1
        assert releases[0].session_id == session_id
        assert releases[0].outcome is TaskSessionOutcome.LOST
        assert releases[0].reason == "lease_expired"
        stored = await repository.get(seeded.id)
        assert stored is not None
        assert stored.status is TaskStatus.BLOCKED
        assert stored.blocked_reason is BlockedReason.LEASE_EXPIRED

    asyncio.run(scenario())


def _agent_session_row(
    task: InvestigationTaskV1,
    *,
    state: AgentSessionState,
    created_at: datetime,
    session_id: UUID | None = None,
    closed_at: datetime | None = None,
    close_reason: str | None = None,
) -> AgentSessionV1:
    session_id = session_id or uuid4()
    capabilities = OperatorCapabilitiesV1(
        adapter_kind=OperatorAdapterKind.CODEX_CLI,
        executable="/usr/local/bin/codex",
        version="codex-cli 1.2.3",
        modes=("exec",),
        supports_resume=True,
        supports_structured_output=True,
        supports_non_interactive=True,
        supports_stop=True,
        permission_modes=("workspace-write",),
        probed_at=NOW,
        probes=(
            OperatorProbeCommand(
                argv=("/usr/local/bin/codex", "--help"),
                exit_code=0,
                stdout_sha256="a" * 64,
                advertised_modes=("exec", "resume", "workspace-write"),
            ),
        ),
    )
    return AgentSessionV1(
        id=session_id,
        investigation_id=task.investigation_id,
        task_id=task.id,
        adapter_kind=OperatorAdapterKind.CODEX_CLI,
        state=state,
        state_version=2,
        capability_probe=capabilities.to_payload(),
        capability_sha256=capabilities.capability_sha256,
        probed_at=NOW,
        auth_secret_nonce=b"s" * 32,
        auth_secret_hash="a" * 64,
        auth_secret_issued_at=NOW,
        opened_at=created_at,
        idempotency_key=f"orphan-{session_id}",
        created_at=created_at,
        updated_at=created_at,
        host_session_ref=f"host-{session_id}",
        closed_at=closed_at,
        close_reason=close_reason,
    )


def test_orphan_session_recovery_fails_the_row_and_flushes_the_host_release() -> None:
    """BLOCKER 1(b): a crash-orphaned session never blocks its task forever."""

    async def scenario() -> None:
        clock_now = NOW + timedelta(minutes=30)
        repository = InMemoryInvestigationTaskRepository(clock=lambda: clock_now)
        task = await _seeded(
            repository,
            status=TaskStatus.ASSIGNED,
            investigation_id=uuid4(),
            assigned_actor_kind=TaskActorKind.OPERATOR,
            assigned_actor_id=OPERATOR.actor_id,
        )
        sessions = InMemoryAgentSessionRepository()
        orphan = _agent_session_row(task, state=AgentSessionState.PROVISIONING, created_at=NOW)
        await sessions.add(orphan)
        terminal = _agent_session_row(
            task,
            state=AgentSessionState.CLOSED,
            created_at=NOW,
            closed_at=NOW,
            close_reason="submitted",
        )
        await sessions.add(terminal)

        class _Uow:
            def __init__(self) -> None:
                self.investigation_tasks: InvestigationTaskRepository = repository
                self.agent_sessions = cast(AgentSessionRepository, sessions)

            async def __aenter__(self) -> _Uow:
                return self

            async def __aexit__(self, *_args: object) -> None:
                return None

            async def commit(self) -> None:
                return None

        class _Source:
            async def list_orphan_sessions(
                self, *, grace_seconds: int, limit: int
            ) -> tuple[AgentSessionV1, ...]:
                rows = await sessions.list_for_task(task.id)
                return tuple(
                    row
                    for row in rows
                    if row.state in (AgentSessionState.PROVISIONING, AgentSessionState.READY)
                    and row.task_lease_id is None
                )

            async def list_pending_host_release_sessions(
                self, *, grace_seconds: int, limit: int
            ) -> tuple[AgentSessionV1, ...]:
                rows = await sessions.list_for_task(task.id)
                return tuple(row for row in rows if row.is_terminal)

        flushed: list[Sequence[TaskSessionRelease]] = []

        async def flush(items: Sequence[TaskSessionRelease]) -> None:
            flushed.append(tuple(items))

        recovered, pending = await recover_orphan_agent_sessions(
            source=_Source(),
            uow_factory=lambda: _Uow(),
            flush_releases=flush,
            grace_seconds=60,
            batch_size=10,
        )
        assert recovered == 1
        stored = await sessions.get(orphan.id)
        assert stored is not None
        assert stored.state is AgentSessionState.FAILED
        assert stored.close_reason == "orphaned_no_lease"
        assert flushed[0][0].session_id == orphan.id
        assert flushed[0][0].outcome is TaskSessionOutcome.LOST
        assert flushed[0][0].reason == "orphaned_no_lease"
        # The recorder writes no confirmation event, so the just-failed row is
        # still "pending" for the second step (at-least-once until confirmed).
        assert {release.session_id for release in flushed[1]} == {orphan.id, terminal.id}
        assert pending == 2

        # Idempotent: the failed row is never failed again.
        again = await recover_orphan_agent_sessions(
            source=_Source(),
            uow_factory=lambda: _Uow(),
            flush_releases=flush,
            grace_seconds=60,
            batch_size=10,
        )
        assert again == (0, 2)
        still = await sessions.get(orphan.id)
        assert still is not None and still.state is AgentSessionState.FAILED

    asyncio.run(scenario())


def test_orphan_recovery_respects_the_grace_period() -> None:
    async def scenario() -> None:
        clock_now = NOW + timedelta(minutes=1)
        repository = InMemoryInvestigationTaskRepository(clock=lambda: clock_now)
        task = await _seeded(
            repository,
            status=TaskStatus.ASSIGNED,
            investigation_id=uuid4(),
            assigned_actor_kind=TaskActorKind.OPERATOR,
            assigned_actor_id=OPERATOR.actor_id,
        )
        sessions = InMemoryAgentSessionRepository()
        young = _agent_session_row(task, state=AgentSessionState.READY, created_at=NOW)
        await sessions.add(young)

        class _Uow:
            def __init__(self) -> None:
                self.investigation_tasks: InvestigationTaskRepository = repository
                self.agent_sessions = cast(AgentSessionRepository, sessions)

            async def __aenter__(self) -> _Uow:
                return self

            async def __aexit__(self, *_args: object) -> None:
                return None

            async def commit(self) -> None:
                return None

        class _Source:
            async def list_orphan_sessions(
                self, *, grace_seconds: int, limit: int
            ) -> tuple[AgentSessionV1, ...]:
                return (young,)

            async def list_pending_host_release_sessions(
                self, *, grace_seconds: int, limit: int
            ) -> tuple[AgentSessionV1, ...]:
                return ()

        async def flush(_items: Sequence[TaskSessionRelease]) -> None:
            raise AssertionError("an in-flight session younger than the grace is not touched")

        assert await recover_orphan_agent_sessions(
            source=_Source(),
            uow_factory=lambda: _Uow(),
            flush_releases=flush,
            grace_seconds=3600,
            batch_size=10,
        ) == (0, 0)
        stored = await sessions.get(young.id)
        assert stored is not None and stored.state is AgentSessionState.READY

    asyncio.run(scenario())


def test_awaiting_review_to_failed_sets_rejected() -> None:
    async def scenario() -> None:
        repository = InMemoryInvestigationTaskRepository()
        task = await _seeded(repository)
        service = _task_service(repository)
        await service.assign(
            task_id=task.id,
            actor=HUMAN,
            target=HUMAN,
            expected_status_version=1,
            idempotency_key="assign",
            now=NOW,
        )
        await service.start(
            task_id=task.id,
            actor=HUMAN,
            expected_status_version=2,
            idempotency_key="start",
            now=NOW,
        )
        submitted = await service.submit(
            task_id=task.id,
            actor=HUMAN,
            expected_status_version=3,
            idempotency_key="submit",
            now=NOW,
        )
        assert submitted.task.result_verification_status is ResultVerificationStatus.PENDING_REVIEW

        failed = await service.fail(
            task_id=task.id,
            actor=HUMAN,
            expected_status_version=4,
            idempotency_key="fail",
            reason="the result cannot be verified",
            now=NOW,
        )
        assert failed.task.status is TaskStatus.FAILED
        assert failed.task.result_verification_status is ResultVerificationStatus.REJECTED
        assert failed.task.is_terminal
        assert failed.task.lease is None

    asyncio.run(scenario())


def test_task_idempotency_scoped_per_task() -> None:
    async def scenario() -> None:
        repository = InMemoryInvestigationTaskRepository()
        service = _task_service(repository)
        investigation_id = uuid4()

        created = await service.create_task(
            investigation_id=investigation_id,
            actor=HUMAN,
            idempotency_key="create-1",
            task_key="lead-a",
            title="Inspect the loader",
            now=NOW,
        )
        assert created.event is not None
        replay_created = await service.create_task(
            investigation_id=investigation_id,
            actor=HUMAN,
            idempotency_key="create-1",
            task_key="lead-a",
            title="Inspect the loader",
            now=NOW + timedelta(seconds=5),
        )
        assert replay_created.replayed is True
        assert replay_created.event is not None
        assert replay_created.event.id == created.event.id
        stored = await repository.get(created.task.id)
        assert stored is not None
        assert stored.event_sequence == 1

        with pytest.raises(TaskIdempotencyConflictError):
            await service.create_task(
                investigation_id=investigation_id,
                actor=HUMAN,
                idempotency_key="create-1",
                task_key="lead-a",
                title="A different body",
                now=NOW,
            )
        with pytest.raises(TaskKeyConflictError):
            await service.create_task(
                investigation_id=investigation_id,
                actor=HUMAN,
                idempotency_key="create-2",
                task_key="lead-a",
                title="A different body",
                now=NOW,
            )
        replay_by_key = await service.create_task(
            investigation_id=investigation_id,
            actor=HUMAN,
            idempotency_key="create-3",
            task_key="lead-a",
            title="Inspect the loader",
            now=NOW,
        )
        assert replay_by_key.task.id == created.task.id
        assert replay_by_key.event is not None
        assert replay_by_key.event.id == created.event.id

        first = await _seeded(repository, investigation_id=investigation_id)
        second = await _seeded(repository, investigation_id=investigation_id)
        assigned_first = await service.assign(
            task_id=first.id,
            actor=HUMAN,
            target=HUMAN,
            expected_status_version=1,
            idempotency_key="shared-key",
            now=NOW,
        )
        assigned_second = await service.assign(
            task_id=second.id,
            actor=HUMAN,
            target=HUMAN,
            expected_status_version=1,
            idempotency_key="shared-key",
            now=NOW,
        )
        assert assigned_first.event is not None
        assert assigned_second.event is not None
        assert assigned_first.event.id != assigned_second.event.id

        replayed = await service.assign(
            task_id=first.id,
            actor=HUMAN,
            target=HUMAN,
            expected_status_version=1,
            idempotency_key="shared-key",
            now=NOW + timedelta(seconds=5),
        )
        assert replayed.replayed is True
        assert replayed.event is not None
        assert replayed.event.id == assigned_first.event.id
        stored_first = await repository.get(first.id)
        assert stored_first is not None
        assert stored_first.status_version == 2
        assert stored_first.event_sequence == 1

        with pytest.raises(TaskIdempotencyConflictError):
            await service.assign(
                task_id=first.id,
                actor=HUMAN,
                target=OPERATOR,
                expected_status_version=1,
                idempotency_key="shared-key",
                now=NOW,
            )

    asyncio.run(scenario())


def test_dependency_cycle_serialized_by_advisory_lock() -> None:
    async def scenario() -> None:
        repository = InMemoryInvestigationTaskRepository()
        service = _task_service(repository)
        investigation_id = uuid4()
        first = await _seeded(
            repository, status=TaskStatus.PROPOSED, investigation_id=investigation_id
        )
        second = await _seeded(
            repository, status=TaskStatus.PROPOSED, investigation_id=investigation_id
        )
        third = await _seeded(
            repository, status=TaskStatus.PROPOSED, investigation_id=investigation_id
        )

        created = await service.add_dependency(
            task_id=first.id, depends_on_task_id=second.id, actor=HUMAN
        )
        assert created.created is True
        repeated = await service.add_dependency(
            task_id=first.id, depends_on_task_id=second.id, actor=HUMAN
        )
        assert repeated.created is False
        await service.add_dependency(task_id=second.id, depends_on_task_id=third.id, actor=HUMAN)

        with pytest.raises(TaskDependencyError) as cycle:
            await service.add_dependency(task_id=third.id, depends_on_task_id=first.id, actor=HUMAN)
        assert cycle.value.code == "task_dependency_invalid"
        with pytest.raises(TaskDependencyError):
            await service.add_dependency(task_id=first.id, depends_on_task_id=first.id, actor=HUMAN)
        assert await repository.list_dependencies(first.id) == (second.id,)
        assert await repository.list_dependencies(third.id) == ()

    asyncio.run(scenario())


def test_proposed_task_promoted_when_dependency_completes() -> None:
    async def scenario() -> None:
        repository = InMemoryInvestigationTaskRepository()
        service = _task_service(repository)
        investigation_id = uuid4()
        dependent = await _seeded(
            repository, status=TaskStatus.PROPOSED, investigation_id=investigation_id
        )
        first = await _seeded(repository, investigation_id=investigation_id)
        last = await _seeded(repository, investigation_id=investigation_id)
        await service.add_dependency(task_id=dependent.id, depends_on_task_id=first.id, actor=HUMAN)
        await service.add_dependency(task_id=dependent.id, depends_on_task_id=last.id, actor=HUMAN)

        await _complete_human_task(service, first, prefix="first")
        stored = await repository.get(dependent.id)
        assert stored is not None
        assert stored.status is TaskStatus.PROPOSED
        assert await repository.list_events(dependent.id) == ()

        await _complete_human_task(service, last, prefix="last")
        stored = await repository.get(dependent.id)
        assert stored is not None
        assert stored.status is TaskStatus.READY
        assert stored.blocked_reason is None
        events = await repository.list_events(dependent.id)
        assert len(events) == 1
        promotion = events[0]
        assert promotion.event_type is TaskEventType.STATUS_CHANGED
        assert promotion.actor.kind is TaskActorKind.SYSTEM
        assert promotion.from_status is TaskStatus.PROPOSED
        assert promotion.to_status is TaskStatus.READY
        assert promotion.idempotency_key == f"dep:{last.id}:{dependent.id}:1"

    asyncio.run(scenario())


def test_dependency_unsatisfiable_blocks_dependent() -> None:
    async def scenario() -> None:
        repository = InMemoryInvestigationTaskRepository()
        service = _task_service(repository)
        investigation_id = uuid4()
        dependent = await _seeded(
            repository, status=TaskStatus.PROPOSED, investigation_id=investigation_id
        )
        prerequisite = await _seeded(
            repository, status=TaskStatus.READY, investigation_id=investigation_id
        )
        await service.add_dependency(
            task_id=dependent.id, depends_on_task_id=prerequisite.id, actor=HUMAN
        )

        cancelled = await service.cancel(
            task_id=prerequisite.id,
            actor=HUMAN,
            expected_status_version=1,
            idempotency_key="cancel-prerequisite",
            reason="the lead is obsolete",
            now=NOW,
        )
        assert cancelled.task.status is TaskStatus.CANCELLED

        stored = await repository.get(dependent.id)
        assert stored is not None
        assert stored.status is TaskStatus.BLOCKED
        assert stored.blocked_reason is BlockedReason.DEPENDENCY
        events = await repository.list_events(dependent.id)
        assert len(events) == 1
        assert events[0].actor.kind is TaskActorKind.SYSTEM
        assert events[0].to_status is TaskStatus.BLOCKED
        assert events[0].idempotency_key == f"dep:{prerequisite.id}:{dependent.id}:1"

        promoted = await service.promote(
            task_id=dependent.id,
            actor=HUMAN,
            expected_status_version=2,
            idempotency_key="manual-unblock",
            now=NOW,
        )
        assert promoted.task.status is TaskStatus.READY

    asyncio.run(scenario())


def test_subtask_depth_capability_and_path_subset_enforced() -> None:
    async def scenario() -> None:
        repository = InMemoryInvestigationTaskRepository()
        service = _task_service(repository, limits=TaskLimits(max_children=1))
        investigation_id = uuid4()
        parent = await service.create_task(
            investigation_id=investigation_id,
            actor=HUMAN,
            idempotency_key="parent",
            task_key="parent",
            title="Parent lead",
            required_capabilities=("vt", "bridge"),
            allowed_workspace_paths=("notes/agents/", "samples/"),
            now=NOW,
        )
        assert parent.task.depth == 0

        child = await service.create_task(
            investigation_id=investigation_id,
            actor=HUMAN,
            idempotency_key="child",
            task_key="child",
            title="Child lead",
            parent_task_id=parent.task.id,
            required_capabilities=("vt",),
            allowed_workspace_paths=("notes/agents/child/",),
            now=NOW,
        )
        assert child.task.status is TaskStatus.PROPOSED
        assert child.task.depth == 1
        assert child.task.parent_task_id == parent.task.id

        with pytest.raises(TaskPermissionError):
            await service.create_task(
                investigation_id=investigation_id,
                actor=HUMAN,
                idempotency_key="capability-escalation",
                task_key="capability-escalation",
                title="Escalated",
                parent_task_id=parent.task.id,
                required_capabilities=("vt", "publish"),
                now=NOW,
            )
        with pytest.raises(TaskPermissionError):
            await service.create_task(
                investigation_id=investigation_id,
                actor=HUMAN,
                idempotency_key="path-escalation",
                task_key="path-escalation",
                title="Escalated",
                parent_task_id=parent.task.id,
                allowed_workspace_paths=("secrets/",),
                now=NOW,
            )
        with pytest.raises(TaskCommandError):
            await service.create_task(
                investigation_id=investigation_id,
                actor=HUMAN,
                idempotency_key="path-traversal",
                task_key="path-traversal",
                title="Traversal",
                parent_task_id=parent.task.id,
                allowed_workspace_paths=("../escape/",),
                now=NOW,
            )
        with pytest.raises(TaskLimitExceededError):
            await service.create_task(
                investigation_id=investigation_id,
                actor=HUMAN,
                idempotency_key="second-child",
                task_key="second-child",
                title="Second child",
                parent_task_id=parent.task.id,
                now=NOW,
            )

        second = await service.create_task(
            investigation_id=investigation_id,
            actor=HUMAN,
            idempotency_key="depth-2",
            task_key="depth-2",
            title="Depth two",
            parent_task_id=child.task.id,
            required_capabilities=("vt",),
            allowed_workspace_paths=("notes/agents/child/next/",),
            now=NOW,
        )
        third = await service.create_task(
            investigation_id=investigation_id,
            actor=HUMAN,
            idempotency_key="depth-3",
            task_key="depth-3",
            title="Depth three",
            parent_task_id=second.task.id,
            required_capabilities=("vt",),
            allowed_workspace_paths=("notes/agents/child/next/leaf/",),
            now=NOW,
        )
        assert third.task.depth == 3
        with pytest.raises(TaskLimitExceededError):
            await service.create_task(
                investigation_id=investigation_id,
                actor=HUMAN,
                idempotency_key="depth-4",
                task_key="depth-4",
                title="Depth four",
                parent_task_id=third.task.id,
                required_capabilities=("vt",),
                allowed_workspace_paths=("notes/agents/child/next/leaf/more/",),
                now=NOW,
            )

        human_parent = await _seeded(
            repository,
            status=TaskStatus.ASSIGNED,
            investigation_id=investigation_id,
            assigned_actor_kind=TaskActorKind.HUMAN,
            assigned_actor_id=HUMAN.actor_id,
        )
        with pytest.raises(TaskPermissionError):
            await service.create_task(
                investigation_id=investigation_id,
                actor=OPERATOR,
                idempotency_key="operator-subtask",
                task_key="operator-subtask",
                title="Operator subtask",
                parent_task_id=human_parent.id,
                now=NOW,
            )
        with pytest.raises(TaskPermissionError):
            await service.create_task(
                investigation_id=investigation_id,
                actor=OPERATOR,
                idempotency_key="operator-top-level",
                task_key="operator-top-level",
                title="Operator top level",
                now=NOW,
            )

    asyncio.run(scenario())


def test_settings_task_limits_read_adr_env_names() -> None:
    defaults = Settings(_env_file=None)
    assert defaults.task_limits() == TASK_LIMIT_DEFAULTS
    overridden = Settings(
        _env_file=None,
        m24_task_lease_seconds=45,
        m24_task_max_deps=4,
        m24_task_max_depth=2,
    )
    limits = overridden.task_limits()
    assert limits.lease_seconds == 45
    assert limits.max_dependencies == 4
    assert limits.max_depth == 2


def test_operator_lease_lifecycle_keeps_and_closes_its_session() -> None:
    async def scenario() -> None:
        # The lease deadline is read on the repository (database) clock: pin it
        # to the scenario clock instead of the wall clock that has moved on.
        repository = InMemoryInvestigationTaskRepository(clock=lambda: NOW)
        sessions = _SessionRecorder()
        seeded = await _seeded(
            repository,
            status=TaskStatus.ASSIGNED,
            assigned_actor_kind=TaskActorKind.OPERATOR,
            assigned_actor_id=OPERATOR.actor_id,
        )
        service = _task_service(repository, sessions=sessions)

        with pytest.raises(TaskSessionInactiveError):
            await service.start(
                task_id=seeded.id,
                actor=OPERATOR,
                expected_status_version=1,
                idempotency_key="start-without-session",
                session_id=uuid4(),
                now=NOW,
            )

        session_id = uuid4()
        sessions.active.add(session_id)
        started = await service.start(
            task_id=seeded.id,
            actor=OPERATOR,
            expected_status_version=1,
            idempotency_key="start",
            session_id=session_id,
            now=NOW,
        )
        assert started.task.lease is not None
        awaiting = await service.request_input(
            task_id=seeded.id,
            actor=OPERATOR,
            expected_status_version=2,
            lease_fence=1,
            idempotency_key="request-input",
            now=NOW + timedelta(seconds=10),
        )
        assert awaiting.task.status is TaskStatus.AWAITING_INPUT
        assert awaiting.task.lease is not None

        resumed = await service.resume(
            task_id=seeded.id,
            actor=OPERATOR,
            expected_status_version=3,
            lease_fence=1,
            idempotency_key="resume",
            now=NOW + timedelta(seconds=20),
        )
        assert resumed.task.status is TaskStatus.RUNNING
        assert resumed.task.lease_fence == 1
        assert resumed.task.lease is not None

        submitted = await service.submit(
            task_id=seeded.id,
            actor=OPERATOR,
            expected_status_version=4,
            lease_fence=1,
            idempotency_key="submit",
            result_refs=({"kind": "observation", "id": str(uuid4())},),
            now=NOW + timedelta(seconds=30),
        )
        assert submitted.task.status is TaskStatus.AWAITING_REVIEW
        assert submitted.task.result_verification_status is ResultVerificationStatus.PENDING_REVIEW
        assert submitted.task.lease is None
        assert submitted.task.lease_fence == 2
        assert submitted.task.result_refs[0]["kind"] == "observation"
        assert sessions.closed == [(session_id, seeded.id, TaskSessionOutcome.CLOSED, "submitted")]

    asyncio.run(scenario())


def test_request_changes_rejects_verification_and_clears_assignment() -> None:
    async def scenario() -> None:
        repository = InMemoryInvestigationTaskRepository()
        decisions = _DecisionRecorder()
        seeded = await _seeded(repository)
        service = _task_service(repository, decisions=decisions)
        submitted = await service.assign(
            task_id=seeded.id,
            actor=HUMAN,
            target=HUMAN,
            expected_status_version=1,
            idempotency_key="assign",
            now=NOW,
        )
        await service.start(
            task_id=seeded.id,
            actor=HUMAN,
            expected_status_version=submitted.task.status_version,
            idempotency_key="start",
            now=NOW,
        )
        awaiting = await service.submit(
            task_id=seeded.id,
            actor=HUMAN,
            expected_status_version=3,
            idempotency_key="submit",
            now=NOW,
        )
        assert awaiting.task.assigned_actor_id == HUMAN.actor_id

        changed = await service.request_changes(
            task_id=seeded.id,
            actor=HUMAN,
            expected_status_version=4,
            idempotency_key="changes",
            reason="the second stage is not evidenced",
            now=NOW,
        )
        assert changed.task.status is TaskStatus.READY
        assert changed.task.result_verification_status is ResultVerificationStatus.REJECTED
        assert changed.task.assigned_actor_kind is None
        assert changed.task.assigned_actor_id is None
        assert len(decisions.decisions) == 1
        decision = decisions.decisions[0]
        assert decision.decision_type is AnalystDecisionType.TASK_RESULT_CHANGES_REQUESTED
        assert changed.event is not None
        assert changed.event.analyst_decision_id == decision.id

    asyncio.run(scenario())


def test_human_unblock_and_reassign_paths() -> None:
    async def scenario() -> None:
        repository = InMemoryInvestigationTaskRepository()
        service = _task_service(repository)
        investigation_id = uuid4()
        blocked = await _seeded(
            repository,
            status=TaskStatus.BLOCKED,
            investigation_id=investigation_id,
            blocked_reason=BlockedReason.CAPABILITY,
        )
        unblocked = await service.unblock(
            task_id=blocked.id,
            actor=HUMAN,
            expected_status_version=1,
            idempotency_key="unblock",
            now=NOW,
        )
        assert unblocked.task.status is TaskStatus.READY
        assert unblocked.task.blocked_reason is None

        dependency_blocked = await _seeded(
            repository,
            status=TaskStatus.BLOCKED,
            investigation_id=investigation_id,
            blocked_reason=BlockedReason.LEASE_EXPIRED,
            lease_fence=2,
        )
        reassigned = await service.reassign(
            task_id=dependency_blocked.id,
            actor=HUMAN,
            target=OPERATOR,
            expected_status_version=1,
            idempotency_key="reassign",
            now=NOW,
        )
        assert reassigned.task.status is TaskStatus.ASSIGNED
        assert reassigned.task.assigned_actor_kind is TaskActorKind.OPERATOR
        assert reassigned.task.assigned_actor_id == OPERATOR.actor_id
        assert reassigned.task.blocked_reason is None
        assert reassigned.task.lease is None

        session_id = uuid4()
        sessions = _SessionRecorder(active=(session_id,))
        restarted = await _task_service(repository, sessions=sessions).start(
            task_id=dependency_blocked.id,
            actor=OPERATOR,
            expected_status_version=2,
            idempotency_key="restart",
            session_id=session_id,
            now=NOW,
        )
        assert restarted.task.status is TaskStatus.RUNNING
        assert restarted.task.lease_fence == 3

    asyncio.run(scenario())
