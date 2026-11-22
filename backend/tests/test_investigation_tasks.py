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
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from cti_app.application.investigation_persistence import InMemoryInvestigationTaskRepository
from cti_app.application.investigation_task_events import (
    TaskEventConflictError,
    TaskEventDraft,
    TaskEventType,
    TaskNotFoundError,
    append_task_event,
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
