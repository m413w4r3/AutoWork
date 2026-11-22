"""Command engine for durable investigation tasks (M24-04B2, ADR-0011 D3).

Every business status write goes through one of the commands below.  A command
validates the D3 transition table, checks the lease and the CAS token, applies
the business state and appends exactly one ``investigation_task_events`` row in
the caller's unit of work.  ``investigation_tasks.status``/``status_version``
and ``lease_*``/``lease_fence`` stay the only authorities: this module adds no
second journal, budget or evidence path.

Lease rules.  An operator command that writes under a lease requires the
current ``lease_fence`` and a lease that has not expired; an expired or
superseded lease refuses every such write with a typed ``lease_expired`` /
``stale_fence`` error while reads remain allowed.  A human command uses only
``status_version`` (a human-assigned task never holds a lease) and may revoke a
running operator lease.  The heartbeat is a single conditional UPDATE through
``renew_lease`` (``status IN ('running','awaiting_input') AND
status_version = :expected AND lease_fence = :fence AND lease_expires_at >
:now``); zero rows is a typed 409 and a late heartbeat never resurrects a
lease.  ``append_task_event`` remains the sole writer of ``event_sequence``:
a status command advances ``status_version`` and then ``event_sequence``, a
non-status event advances only ``event_sequence`` and carries the current
``status_version``.

Idempotency is scoped by ``(task_id, Idempotency-Key)`` and compared through
the event's canonical ``request_sha256``.  A replay returns the recorded event
and the current task without a second effect; the same key with a different
request body is ``idempotency_conflict``.  The variable part of a status
command is the *from* status, which a retry cannot know, so a replay rebuilds
the body identity from the recorded event's ``from_status`` while keeping the
caller's requested to-status.

Dependencies are serialised per investigation by the advisory lock exposed as
``lock_investigation`` from 04B1: cycle check, dependency/children caps and the
system promotion of dependents all run under that lock.  A dependency is only
satisfied by ``completed``; ``cancelled``/``failed`` never satisfy it and make
a still-``proposed`` dependent ``blocked`` with ``blocked_reason='dependency'``.

Session persistence belongs to lot 05B1.  The engine only depends on the
narrow :class:`TaskSessionPort` seam: an operator ``start`` requires an active
session bound to the task, and releasing a lease closes the linked session
(``closed`` on a clean exit, ``lost`` on expiry/failure).
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol
from uuid import UUID, uuid4, uuid5

from cti_app.application.investigation_persistence import (
    InvestigationTaskRepository,
    VersionConflictError,
)
from cti_app.application.investigation_task_events import (
    TaskEvent,
    TaskEventDraft,
    TaskEventType,
    append_task_event,
)
from cti_app.application.persistence import AnalystDecisionRepository
from cti_app.domain.editorial import (
    AnalystDecision,
    AnalystDecisionTargetType,
    AnalystDecisionType,
)
from cti_app.domain.investigation import thaw_json
from cti_app.domain.investigation_task import (
    TASK_LIMIT_DEFAULTS,
    BlockedReason,
    InvestigationTaskV1,
    ResultVerificationStatus,
    TaskActor,
    TaskActorKind,
    TaskLimits,
    TaskPriority,
    TaskStatus,
    TaskTransitionContext,
    TaskTransitionError,
    validate_transition,
)

# "Active" bounds concurrent work, not the backlog: proposed/ready/blocked
# tasks wait without occupying an operator or a review slot.
ACTIVE_TASK_STATUSES = frozenset(
    {
        TaskStatus.ASSIGNED,
        TaskStatus.RUNNING,
        TaskStatus.AWAITING_INPUT,
        TaskStatus.AWAITING_REVIEW,
    }
)
LEASE_HELD_STATUSES = frozenset({TaskStatus.RUNNING, TaskStatus.AWAITING_INPUT})
TERMINAL_STATUSES = frozenset({TaskStatus.COMPLETED, TaskStatus.CANCELLED, TaskStatus.FAILED})
UNSATISFIED_TERMINAL_STATUSES = frozenset({TaskStatus.CANCELLED, TaskStatus.FAILED})

SYSTEM_TASK_ACTOR = TaskActor(kind=TaskActorKind.SYSTEM, actor_id="task_engine")
_TASK_DECISION_NAMESPACE = UUID("b0c2f0f4-4c3d-4b57-9a51-5d3d9f9a6c21")

_ASSIGNABLE_ACTOR_KINDS = frozenset({TaskActorKind.HUMAN, TaskActorKind.OPERATOR})
_ALL_TASK_CREATOR_KINDS = frozenset(
    {TaskActorKind.HUMAN, TaskActorKind.OPERATOR, TaskActorKind.SYSTEM}
)
_EXECUTION_ACTOR_KINDS = frozenset({TaskActorKind.HUMAN, TaskActorKind.OPERATOR})


class TaskCommandError(ValueError):
    """Base refusal for a task command; ``code`` is the API-facing reason."""

    code = "task_command_invalid"
    status_code = 400


class TaskNotFoundCommandError(TaskCommandError):
    code = "task_not_found"
    status_code = 404


class TaskPermissionError(TaskCommandError):
    code = "task_actor_not_allowed"
    status_code = 403


class StaleTaskVersionError(TaskCommandError):
    code = "stale_task_version"
    status_code = 409


class TaskIdempotencyConflictError(TaskCommandError):
    code = "idempotency_conflict"
    status_code = 409


class TaskLeaseExpiredError(TaskCommandError):
    code = "lease_expired"
    status_code = 409


class TaskLeaseFenceError(TaskCommandError):
    code = "stale_fence"
    status_code = 409


class TaskTransitionNotAllowedError(TaskCommandError):
    code = "task_transition_not_allowed"
    status_code = 409


class TaskSessionInactiveError(TaskCommandError):
    code = "agent_session_inactive"
    status_code = 409


class TaskLimitExceededError(TaskCommandError):
    code = "task_limit_exceeded"
    status_code = 409


class TaskDependencyError(TaskCommandError):
    code = "task_dependency_invalid"
    status_code = 422


class TaskKeyConflictError(TaskCommandError):
    code = "task_key_conflict"
    status_code = 409


class TaskSessionOutcome(StrEnum):
    """How a released task lease leaves its bound agent session."""

    CLOSED = "closed"
    LOST = "lost"


class TaskSessionPort(Protocol):
    """Narrow seam over ``agent_sessions`` (repository owned by lot 05B1).

    The engine never persists or inspects a session itself: it only asks
    whether a session is active and bound to the task before an operator
    ``start``, and reports the outcome when a lease is released.
    """

    async def is_active_session(
        self, *, session_id: UUID, investigation_id: UUID, task_id: UUID
    ) -> bool: ...

    async def close_session(
        self,
        *,
        session_id: UUID,
        investigation_id: UUID,
        task_id: UUID,
        outcome: TaskSessionOutcome,
        reason: str,
    ) -> None: ...


@dataclass(frozen=True, slots=True, kw_only=True)
class TaskCommandResult:
    """The task state after a command plus the event that changed it.

    ``event`` is ``None`` only when a creation is replayed through the task-key
    identity and the existing task carries no ``task_created`` event (for
    example a task materialised by another lot's writer).
    """

    task: InvestigationTaskV1
    event: TaskEvent | None
    replayed: bool = False


@dataclass(frozen=True, slots=True, kw_only=True)
class TaskDependencyResult:
    task: InvestigationTaskV1
    depends_on_task_id: UUID
    created: bool


def _aware(value: object, name: str) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError(f"{name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value


def _now(value: datetime | None) -> datetime:
    return datetime.now(UTC) if value is None else _aware(value, "now")


def _idempotency_key(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TaskCommandError("Idempotency-Key must be a non-empty string")
    return value.strip()


def _uuid(value: object, name: str) -> UUID:
    if not isinstance(value, UUID):
        raise TypeError(f"{name} must be a UUID")
    return value


def _optional_id(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, UUID):
        raise TypeError("value must be a UUID or None")
    return str(value)


def _normalized_text(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    normalized = value.strip()
    if not normalized:
        raise TaskCommandError(f"{name} must not be empty")
    return normalized


def _normalized_refs(value: object, name: str) -> tuple[str, ...]:
    """Mirror ``InvestigationTaskV1``'s reference normalization for identity."""
    if not isinstance(value, (tuple, list)):
        raise TypeError(f"{name} must be a sequence of references")
    normalized: list[str] = []
    seen: set[str] = set()
    for item in value:
        reference = _normalized_text(item, f"{name} entry")
        if reference in seen:
            continue
        seen.add(reference)
        normalized.append(reference)
    return tuple(normalized)


def _require_actor(actor: TaskActor, kinds: frozenset[TaskActorKind], refusal: str) -> None:
    if not isinstance(actor, TaskActor):
        raise TypeError("actor must use TaskActor")
    if actor.kind not in kinds:
        raise TaskPermissionError(refusal)


def _require_human(actor: TaskActor) -> None:
    _require_actor(actor, frozenset({TaskActorKind.HUMAN}), "this command requires a human actor")


def _require_operator(actor: TaskActor) -> None:
    _require_actor(
        actor, frozenset({TaskActorKind.OPERATOR}), "this command requires an operator actor"
    )


def _require_assignable(target: TaskActor) -> None:
    if not isinstance(target, TaskActor):
        raise TypeError("target must use TaskActor")
    if target.kind not in _ASSIGNABLE_ACTOR_KINDS:
        raise TaskPermissionError("a task is only assigned to a human or an operator")


def _validate_workspace_path(path: object) -> str:
    """Reject host-absolute and traversal paths before they reach a task row.

    A single trailing slash denotes a directory grant and is normalized away;
    no other empty, ``.`` or ``..`` component is accepted.
    """
    if not isinstance(path, str) or not path.strip():
        raise TaskCommandError("allowed workspace paths must be non-empty strings")
    candidate = path.strip()
    if candidate.startswith("/") or "\\" in candidate:
        raise TaskCommandError("allowed workspace paths must be relative POSIX paths")
    candidate = candidate.rstrip("/")
    if not candidate:
        raise TaskCommandError("allowed workspace paths must not be the workspace root")
    parts = candidate.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise TaskCommandError("allowed workspace paths must not contain '.', '..' or empty parts")
    return candidate


def _path_within(candidate: str, granted: str) -> bool:
    return candidate == granted or candidate.startswith(granted.rstrip("/") + "/")


def _normalized_paths(value: object) -> tuple[str, ...]:
    """Validate and dedupe path grants the way the task aggregate does."""
    if not isinstance(value, (tuple, list)):
        raise TypeError("allowed_workspace_paths must be a sequence of paths")
    normalized: list[str] = []
    for item in value:
        path = _validate_workspace_path(item)
        if path not in normalized:
            normalized.append(path)
    return tuple(normalized)


def _path_subset_refusal(requested: Sequence[str], granted: Sequence[str]) -> str | None:
    for path in requested:
        if not any(_path_within(path, allowed) for allowed in granted):
            return path
    return None


def _creation_identity(
    *,
    investigation_id: UUID,
    task_key: str,
    plan_version_id: UUID | None,
    parent_task_id: UUID | None,
    depth: int,
    title: str,
    question: str,
    objective: str,
    priority: TaskPriority,
    claim_refs: Sequence[str],
    evidence_refs: Sequence[str],
    sample_refs: Sequence[str],
    required_capabilities: Sequence[str],
    allowed_workspace_paths: Sequence[str],
    execution_policy: Mapping[str, Any] | None,
    expected_outputs: Sequence[str],
    actor: TaskActor,
) -> dict[str, Any]:
    return {
        "investigation_id": str(investigation_id),
        "task_key": task_key,
        "plan_version_id": _optional_id(plan_version_id),
        "parent_task_id": _optional_id(parent_task_id),
        "depth": depth,
        "title": title,
        "question": question,
        "objective": objective,
        "priority": priority.value,
        "claim_refs": list(claim_refs),
        "evidence_refs": list(evidence_refs),
        "sample_refs": list(sample_refs),
        "required_capabilities": list(required_capabilities),
        "allowed_workspace_paths": list(allowed_workspace_paths),
        "execution_policy": thaw_json(execution_policy if execution_policy is not None else {}),
        "expected_outputs": list(expected_outputs),
        "created_by_actor_kind": actor.kind.value,
        "created_by_actor_id": actor.actor_id,
    }


def _creation_identity_from_task(task: InvestigationTaskV1) -> dict[str, Any]:
    return _creation_identity(
        investigation_id=task.investigation_id,
        task_key=task.task_key,
        plan_version_id=task.plan_version_id,
        parent_task_id=task.parent_task_id,
        depth=task.depth,
        title=task.title,
        question=task.question,
        objective=task.objective,
        priority=task.priority,
        claim_refs=task.claim_refs,
        evidence_refs=task.evidence_refs,
        sample_refs=task.sample_refs,
        required_capabilities=task.required_capabilities,
        allowed_workspace_paths=task.allowed_workspace_paths,
        execution_policy=task.execution_policy,
        expected_outputs=task.expected_outputs,
        actor=task.created_by,
    )


def _same_identity(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    return dict(left) == dict(right)


class InvestigationTaskService:
    """The D3 state machine, lease and idempotency engine for one UoW."""

    def __init__(
        self,
        *,
        tasks: InvestigationTaskRepository,
        decisions: AnalystDecisionRepository,
        sessions: TaskSessionPort,
        limits: TaskLimits = TASK_LIMIT_DEFAULTS,
    ) -> None:
        self._tasks = tasks
        self._decisions = decisions
        self._sessions = sessions
        self._limits = limits

    # ------------------------------------------------------------------ create

    async def create_task(
        self,
        *,
        investigation_id: UUID,
        actor: TaskActor,
        idempotency_key: str,
        task_key: str,
        title: str,
        question: str = "",
        objective: str = "",
        priority: TaskPriority = TaskPriority.NORMAL,
        parent_task_id: UUID | None = None,
        plan_version_id: UUID | None = None,
        claim_refs: Sequence[str] = (),
        evidence_refs: Sequence[str] = (),
        sample_refs: Sequence[str] = (),
        required_capabilities: Sequence[str] = (),
        allowed_workspace_paths: Sequence[str] = (),
        execution_policy: Mapping[str, Any] | None = None,
        expected_outputs: Sequence[str] = (),
        task_id: UUID | None = None,
        now: datetime | None = None,
    ) -> TaskCommandResult:
        """Create one ``proposed`` task; a subtask adds no authorization.

        Operator creation is limited to a subtask of a task assigned to that
        operator.  A subtask is bounded by its parent's depth, capabilities and
        allowed workspace paths; it never widens them.
        """
        _uuid(investigation_id, "investigation_id")
        _require_actor(actor, _ALL_TASK_CREATOR_KINDS, "this actor may not create tasks")
        key = _idempotency_key(idempotency_key)
        timestamp = _now(now)
        normalized_key = _normalized_text(task_key, "task_key")
        normalized_title = _normalized_text(title, "title")
        if not isinstance(question, str) or not isinstance(objective, str):
            raise TypeError("question and objective must be strings")
        if not isinstance(priority, TaskPriority):
            raise TypeError("priority must use TaskPriority")
        if task_id is not None:
            _uuid(task_id, "task_id")
        if parent_task_id is not None:
            _uuid(parent_task_id, "parent_task_id")
        if plan_version_id is not None:
            _uuid(plan_version_id, "plan_version_id")
        if actor.kind is TaskActorKind.OPERATOR and parent_task_id is None:
            raise TaskPermissionError("an operator may only create a subtask of its own task")
        capabilities = _normalized_refs(required_capabilities, "required_capabilities")
        paths = _normalized_paths(allowed_workspace_paths)
        claim_refs = _normalized_refs(claim_refs, "claim_refs")
        evidence_refs = _normalized_refs(evidence_refs, "evidence_refs")
        sample_refs = _normalized_refs(sample_refs, "sample_refs")
        expected_outputs = _normalized_refs(expected_outputs, "expected_outputs")

        existing = await self._tasks.get_by_task_key(investigation_id, normalized_key)
        if existing is not None:
            return await self._replay_creation(
                existing,
                actor=actor,
                idempotency_key=key,
                investigation_id=investigation_id,
                task_key=normalized_key,
                title=normalized_title,
                question=question.strip(),
                objective=objective.strip(),
                priority=priority,
                parent_task_id=parent_task_id,
                plan_version_id=plan_version_id,
                claim_refs=claim_refs,
                evidence_refs=evidence_refs,
                sample_refs=sample_refs,
                required_capabilities=capabilities,
                allowed_workspace_paths=paths,
                execution_policy=execution_policy,
                expected_outputs=expected_outputs,
            )

        await self._tasks.lock_investigation(investigation_id)
        existing = await self._tasks.get_by_task_key(investigation_id, normalized_key)
        if existing is not None:
            return await self._replay_creation(
                existing,
                actor=actor,
                idempotency_key=key,
                investigation_id=investigation_id,
                task_key=normalized_key,
                title=normalized_title,
                question=question.strip(),
                objective=objective.strip(),
                priority=priority,
                parent_task_id=parent_task_id,
                plan_version_id=plan_version_id,
                claim_refs=claim_refs,
                evidence_refs=evidence_refs,
                sample_refs=sample_refs,
                required_capabilities=capabilities,
                allowed_workspace_paths=paths,
                execution_policy=execution_policy,
                expected_outputs=expected_outputs,
            )

        depth = 0
        effective_plan_version_id = plan_version_id
        if parent_task_id is not None:
            parent = await self._tasks.get(parent_task_id)
            if parent is None or parent.investigation_id != investigation_id:
                raise TaskDependencyError("the parent task belongs to another investigation")
            if parent.status in TERMINAL_STATUSES:
                raise TaskTransitionNotAllowedError("a terminal task cannot receive subtasks")
            if actor.kind is TaskActorKind.OPERATOR and (
                parent.assigned_actor_kind is not TaskActorKind.OPERATOR
                or parent.assigned_actor_id != actor.actor_id
            ):
                raise TaskPermissionError("an operator may only create a subtask of its own task")
            depth = parent.depth + 1
            if depth > self._limits.max_depth:
                raise TaskLimitExceededError("maximum subtask depth reached")
            # Authorization is checked before resource caps so an escalation is
            # always reported as such, even when the parent is already full.
            escalated = _path_subset_refusal(paths, parent.allowed_workspace_paths)
            if escalated is not None:
                raise TaskPermissionError(
                    f"subtask path {escalated!r} is outside the parent's allowed paths"
                )
            if not set(capabilities) <= set(parent.required_capabilities):
                raise TaskPermissionError(
                    "a subtask cannot require capabilities its parent does not hold"
                )
            children = await self._tasks.list_children(parent_task_id)
            if len(children) >= self._limits.max_children:
                raise TaskLimitExceededError("maximum children reached")
            if effective_plan_version_id is None:
                effective_plan_version_id = parent.plan_version_id

        task = InvestigationTaskV1(
            id=task_id if task_id is not None else uuid4(),
            investigation_id=investigation_id,
            task_key=normalized_key,
            plan_version_id=effective_plan_version_id,
            parent_task_id=parent_task_id,
            depth=depth,
            title=normalized_title,
            question=question.strip(),
            objective=objective.strip(),
            priority=priority,
            claim_refs=claim_refs,
            evidence_refs=evidence_refs,
            sample_refs=sample_refs,
            required_capabilities=capabilities,
            allowed_workspace_paths=paths,
            execution_policy=execution_policy if execution_policy is not None else {},
            expected_outputs=expected_outputs,
            status=TaskStatus.PROPOSED,
            status_version=1,
            result_verification_status=ResultVerificationStatus.UNVERIFIED,
            created_by=actor,
            created_at=timestamp,
            updated_at=timestamp,
        )
        identity = _creation_identity_from_task(task)
        try:
            created = await self._tasks.add(task)
        except VersionConflictError as exc:
            raise TaskKeyConflictError(str(exc)) from exc
        draft = TaskEventDraft(
            investigation_id=created.investigation_id,
            task_id=created.id,
            event_type=TaskEventType.TASK_CREATED,
            actor=actor,
            idempotency_key=key,
            to_status=TaskStatus.PROPOSED,
            payload=identity,
        )
        event, _ = await append_task_event(self._tasks, draft, occurred_at=timestamp)
        return TaskCommandResult(task=await self._current_task(created.id, created), event=event)

    async def _replay_creation(
        self,
        existing: InvestigationTaskV1,
        *,
        actor: TaskActor,
        idempotency_key: str,
        investigation_id: UUID,
        task_key: str,
        title: str,
        question: str,
        objective: str,
        priority: TaskPriority,
        parent_task_id: UUID | None,
        plan_version_id: UUID | None,
        claim_refs: Sequence[str],
        evidence_refs: Sequence[str],
        sample_refs: Sequence[str],
        required_capabilities: Sequence[str],
        allowed_workspace_paths: Sequence[str],
        execution_policy: Mapping[str, Any] | None,
        expected_outputs: Sequence[str],
    ) -> TaskCommandResult:
        depth = existing.depth
        effective_plan_version_id = plan_version_id
        if parent_task_id is not None:
            parent = await self._tasks.get(parent_task_id)
            if parent is not None:
                depth = parent.depth + 1
                if effective_plan_version_id is None:
                    effective_plan_version_id = parent.plan_version_id
        requested = _creation_identity(
            investigation_id=investigation_id,
            task_key=task_key,
            plan_version_id=effective_plan_version_id,
            parent_task_id=parent_task_id,
            depth=depth,
            title=title,
            question=question,
            objective=objective,
            priority=priority,
            claim_refs=claim_refs,
            evidence_refs=evidence_refs,
            sample_refs=sample_refs,
            required_capabilities=required_capabilities,
            allowed_workspace_paths=allowed_workspace_paths,
            execution_policy=execution_policy,
            expected_outputs=expected_outputs,
            actor=actor,
        )
        recorded = await self._tasks.get_event(existing.id, idempotency_key)
        if recorded is not None:
            draft = TaskEventDraft(
                investigation_id=existing.investigation_id,
                task_id=existing.id,
                event_type=TaskEventType.TASK_CREATED,
                actor=actor,
                idempotency_key=idempotency_key,
                from_status=recorded.from_status,
                to_status=TaskStatus.PROPOSED,
                payload=requested,
            )
            if draft.request_sha256 != recorded.request_sha256:
                raise TaskIdempotencyConflictError(
                    "Idempotency-Key already records a different task creation"
                )
            return TaskCommandResult(task=existing, event=recorded, replayed=True)
        if not _same_identity(requested, _creation_identity_from_task(existing)):
            raise TaskKeyConflictError(
                "task_key already materialised a task with a different definition"
            )
        events = await self._tasks.list_events(existing.id)
        created = next(
            (event for event in events if event.event_type is TaskEventType.TASK_CREATED),
            None,
        )
        return TaskCommandResult(task=existing, event=created, replayed=True)

    # ------------------------------------------------------------- assignment

    async def assign(
        self,
        *,
        task_id: UUID,
        actor: TaskActor,
        target: TaskActor,
        expected_status_version: int,
        idempotency_key: str,
        now: datetime | None = None,
    ) -> TaskCommandResult:
        """``ready -> assigned`` by a human; no lease is created."""
        _require_human(actor)
        _require_assignable(target)
        key = _idempotency_key(idempotency_key)
        timestamp = _now(now)
        await self._lock_investigation_for(task_id)
        task = await self._locked_task(task_id)
        payload = {
            "expected_status_version": expected_status_version,
            "assigned_actor_kind": target.kind.value,
            "assigned_actor_id": target.actor_id,
        }
        replayed = await self._replayed(
            task,
            idempotency_key=key,
            event_type=TaskEventType.ASSIGNED,
            actor=actor,
            payload=payload,
            to_status=TaskStatus.ASSIGNED,
        )
        if replayed is not None:
            return replayed
        self._require_expected_version(task, expected_status_version)
        self._require_transition(task, TaskStatus.ASSIGNED, actor)
        await self._require_active_capacity(task, exclude_task_id=task.id)
        updated = task.with_assignment(target, now=timestamp)
        return await self._commit_status(
            task=task,
            updated=updated,
            actor=actor,
            event_type=TaskEventType.ASSIGNED,
            idempotency_key=key,
            payload=payload,
            now=timestamp,
        )

    async def release(
        self,
        *,
        task_id: UUID,
        actor: TaskActor,
        expected_status_version: int,
        idempotency_key: str,
        now: datetime | None = None,
    ) -> TaskCommandResult:
        """``assigned -> ready``: the human gives the task back."""
        _require_human(actor)
        key = _idempotency_key(idempotency_key)
        timestamp = _now(now)
        task = await self._locked_task(task_id)
        payload = {"expected_status_version": expected_status_version}
        replayed = await self._replayed(
            task,
            idempotency_key=key,
            event_type=TaskEventType.STATUS_CHANGED,
            actor=actor,
            payload=payload,
            to_status=TaskStatus.READY,
        )
        if replayed is not None:
            return replayed
        self._require_expected_version(task, expected_status_version)
        self._require_transition(task, TaskStatus.READY, actor)
        cleared = replace(task, assigned_actor_kind=None, assigned_actor_id=None)
        updated = cleared.with_status(TaskStatus.READY, now=timestamp)
        return await self._commit_status(
            task=task,
            updated=updated,
            actor=actor,
            event_type=TaskEventType.STATUS_CHANGED,
            idempotency_key=key,
            payload=payload,
            now=timestamp,
        )

    async def reassign(
        self,
        *,
        task_id: UUID,
        actor: TaskActor,
        target: TaskActor,
        expected_status_version: int,
        idempotency_key: str,
        now: datetime | None = None,
    ) -> TaskCommandResult:
        """``blocked -> assigned`` by a human; the old session is already closed."""
        _require_human(actor)
        _require_assignable(target)
        key = _idempotency_key(idempotency_key)
        timestamp = _now(now)
        await self._lock_investigation_for(task_id)
        task = await self._locked_task(task_id)
        payload = {
            "expected_status_version": expected_status_version,
            "assigned_actor_kind": target.kind.value,
            "assigned_actor_id": target.actor_id,
        }
        replayed = await self._replayed(
            task,
            idempotency_key=key,
            event_type=TaskEventType.ASSIGNED,
            actor=actor,
            payload=payload,
            to_status=TaskStatus.ASSIGNED,
        )
        if replayed is not None:
            return replayed
        self._require_expected_version(task, expected_status_version)
        self._require_transition(task, TaskStatus.ASSIGNED, actor)
        await self._require_active_capacity(task, exclude_task_id=task.id)
        # A blocked task carries a reason; clearing it and assigning are one
        # state change, validated by the aggregate as a whole.
        updated = replace(
            task,
            blocked_reason=None,
            assigned_actor_kind=target.kind,
            assigned_actor_id=target.actor_id,
            status=TaskStatus.ASSIGNED,
            status_version=task.status_version + 1,
            updated_at=timestamp,
        )
        return await self._commit_status(
            task=task,
            updated=updated,
            actor=actor,
            event_type=TaskEventType.ASSIGNED,
            idempotency_key=key,
            payload=payload,
            now=timestamp,
        )

    # ------------------------------------------------------------------ start

    async def start(
        self,
        *,
        task_id: UUID,
        actor: TaskActor,
        expected_status_version: int,
        idempotency_key: str,
        session_id: UUID | None = None,
        now: datetime | None = None,
    ) -> TaskCommandResult:
        """``assigned -> running``.

        An operator start acquires the lease (``fence + 1``) and requires an
        active session bound to the task; a human start is only legal on the
        task assigned to that human and never creates a lease.
        """
        _require_actor(actor, _EXECUTION_ACTOR_KINDS, "only a human or an operator starts a task")
        key = _idempotency_key(idempotency_key)
        timestamp = _now(now)
        if session_id is not None:
            _uuid(session_id, "session_id")
        if actor.kind is TaskActorKind.OPERATOR and session_id is None:
            raise TaskSessionInactiveError("an operator start requires a bound session")
        await self._lock_investigation_for(task_id)
        task = await self._locked_task(task_id)
        payload: dict[str, Any] = {"expected_status_version": expected_status_version}
        if session_id is not None:
            payload["session_id"] = str(session_id)
        replayed = await self._replayed(
            task,
            idempotency_key=key,
            event_type=(
                TaskEventType.LEASE_ACQUIRED
                if actor.kind is TaskActorKind.OPERATOR
                else TaskEventType.STATUS_CHANGED
            ),
            actor=actor,
            payload=payload,
            to_status=TaskStatus.RUNNING,
        )
        if replayed is not None:
            return replayed
        self._require_expected_version(task, expected_status_version)
        if actor.kind is TaskActorKind.HUMAN:
            if (
                task.assigned_actor_kind is not TaskActorKind.HUMAN
                or task.assigned_actor_id != actor.actor_id
            ):
                raise TaskPermissionError("a human only starts the task assigned to them")
            self._require_transition(task, TaskStatus.RUNNING, actor)
            updated = task.with_status(TaskStatus.RUNNING, now=timestamp)
            return await self._commit_status(
                task=task,
                updated=updated,
                actor=actor,
                event_type=TaskEventType.STATUS_CHANGED,
                idempotency_key=key,
                payload=payload,
                now=timestamp,
            )

        if (
            task.assigned_actor_kind is not TaskActorKind.OPERATOR
            or task.assigned_actor_id != actor.actor_id
        ):
            raise TaskPermissionError("an operator only starts the task assigned to it")
        if session_id is None:
            raise TaskSessionInactiveError("an operator start requires a bound session")
        if not await self._sessions.is_active_session(
            session_id=session_id,
            investigation_id=task.investigation_id,
            task_id=task.id,
        ):
            raise TaskSessionInactiveError("the session is not active for this task")
        await self._require_active_capacity(task, exclude_task_id=task.id)
        await self._require_session_running_capacity(task, session_id=session_id)
        self._require_transition(task, TaskStatus.RUNNING, actor)
        running = await self._tasks.acquire_lease(
            task.id,
            expected_status_version=task.status_version,
            lease_id=uuid4(),
            lease_session_id=session_id,
            now=timestamp,
            ttl_seconds=self._limits.lease_seconds,
        )
        if running is None:
            raise StaleTaskVersionError("the task changed before the lease was acquired")
        draft = self._event_draft(
            task=task,
            event_type=TaskEventType.LEASE_ACQUIRED,
            actor=actor,
            idempotency_key=key,
            from_status=task.status,
            to_status=TaskStatus.RUNNING,
            payload=payload,
        )
        event, _ = await append_task_event(self._tasks, draft, occurred_at=timestamp)
        return TaskCommandResult(task=await self._current_task(running.id, running), event=event)

    # -------------------------------------------------------------- heartbeat

    async def heartbeat(
        self,
        *,
        task_id: UUID,
        actor: TaskActor,
        expected_status_version: int,
        lease_fence: int,
        idempotency_key: str,
        now: datetime | None = None,
    ) -> TaskCommandResult:
        """Renew a ``running``/``awaiting_input`` lease without resurrecting it.

        The renewal is the D3 conditional UPDATE; zero rows is a typed 409.
        The command never changes ``status_version`` and appends one
        ``lease_renewed`` event through the sole ``event_sequence`` writer.
        """
        _require_operator(actor)
        key = _idempotency_key(idempotency_key)
        timestamp = _now(now)
        task = await self._locked_task(task_id)
        payload: dict[str, Any] = {
            "expected_status_version": expected_status_version,
            "lease_fence": lease_fence,
            "ttl_seconds": self._limits.lease_seconds,
        }
        replayed = await self._replayed(
            task,
            idempotency_key=key,
            event_type=TaskEventType.LEASE_RENEWED,
            actor=actor,
            payload=payload,
            to_status=None,
        )
        if replayed is not None:
            return replayed
        self._require_expected_version(task, expected_status_version)
        await self._require_current_lease(task, actor, lease_fence=lease_fence, now=timestamp)
        renewed = await self._tasks.renew_lease(
            task.id,
            expected_status_version=task.status_version,
            expected_lease_fence=lease_fence,
            now=timestamp,
            ttl_seconds=self._limits.lease_seconds,
        )
        if renewed is None:
            raise TaskLeaseExpiredError(
                "the lease expired or was superseded before the heartbeat was applied"
            )
        draft = self._event_draft(
            task=task,
            event_type=TaskEventType.LEASE_RENEWED,
            actor=actor,
            idempotency_key=key,
            from_status=None,
            to_status=None,
            payload=payload,
        )
        event, _ = await append_task_event(self._tasks, draft, occurred_at=timestamp)
        return TaskCommandResult(task=await self._current_task(renewed.id, renewed), event=event)

    # ---------------------------------------------------------------- running

    async def request_input(
        self,
        *,
        task_id: UUID,
        actor: TaskActor,
        expected_status_version: int,
        lease_fence: int,
        idempotency_key: str,
        now: datetime | None = None,
    ) -> TaskCommandResult:
        """``running -> awaiting_input``; the lease is kept and renewable."""
        _require_operator(actor)
        key = _idempotency_key(idempotency_key)
        timestamp = _now(now)
        task = await self._locked_task(task_id)
        payload: dict[str, Any] = {
            "expected_status_version": expected_status_version,
            "lease_fence": lease_fence,
        }
        replayed = await self._replayed(
            task,
            idempotency_key=key,
            event_type=TaskEventType.STATUS_CHANGED,
            actor=actor,
            payload=payload,
            to_status=TaskStatus.AWAITING_INPUT,
        )
        if replayed is not None:
            return replayed
        self._require_expected_version(task, expected_status_version)
        await self._require_current_lease(task, actor, lease_fence=lease_fence, now=timestamp)
        self._require_transition(task, TaskStatus.AWAITING_INPUT, actor)
        updated = task.with_status(TaskStatus.AWAITING_INPUT, now=timestamp)
        return await self._commit_status(
            task=task,
            updated=updated,
            actor=actor,
            event_type=TaskEventType.STATUS_CHANGED,
            idempotency_key=key,
            payload=payload,
            now=timestamp,
        )

    async def resume(
        self,
        *,
        task_id: UUID,
        actor: TaskActor,
        expected_status_version: int,
        lease_fence: int,
        idempotency_key: str,
        now: datetime | None = None,
    ) -> TaskCommandResult:
        """``awaiting_input -> running`` once the instruction arrived."""
        _require_operator(actor)
        key = _idempotency_key(idempotency_key)
        timestamp = _now(now)
        task = await self._locked_task(task_id)
        payload: dict[str, Any] = {
            "expected_status_version": expected_status_version,
            "lease_fence": lease_fence,
        }
        replayed = await self._replayed(
            task,
            idempotency_key=key,
            event_type=TaskEventType.STATUS_CHANGED,
            actor=actor,
            payload=payload,
            to_status=TaskStatus.RUNNING,
        )
        if replayed is not None:
            return replayed
        self._require_expected_version(task, expected_status_version)
        await self._require_current_lease(task, actor, lease_fence=lease_fence, now=timestamp)
        self._require_transition(task, TaskStatus.RUNNING, actor)
        updated = task.with_status(TaskStatus.RUNNING, now=timestamp)
        return await self._commit_status(
            task=task,
            updated=updated,
            actor=actor,
            event_type=TaskEventType.STATUS_CHANGED,
            idempotency_key=key,
            payload=payload,
            now=timestamp,
        )

    async def submit(
        self,
        *,
        task_id: UUID,
        actor: TaskActor,
        expected_status_version: int,
        idempotency_key: str,
        result_refs: Sequence[Mapping[str, Any]] = (),
        lease_fence: int | None = None,
        now: datetime | None = None,
    ) -> TaskCommandResult:
        """``running -> awaiting_review`` with the result references."""
        _require_actor(
            actor, _EXECUTION_ACTOR_KINDS, "only a human or an operator submits a task result"
        )
        key = _idempotency_key(idempotency_key)
        timestamp = _now(now)
        task = await self._locked_task(task_id)
        payload: dict[str, Any] = {
            "expected_status_version": expected_status_version,
            "result_refs": [thaw_json(item) for item in result_refs],
        }
        if actor.kind is TaskActorKind.OPERATOR:
            if lease_fence is None:
                raise TaskLeaseFenceError("an operator submit requires the current lease fence")
            payload["lease_fence"] = lease_fence
        replayed = await self._replayed(
            task,
            idempotency_key=key,
            event_type=TaskEventType.RESULT_SUBMITTED,
            actor=actor,
            payload=payload,
            to_status=TaskStatus.AWAITING_REVIEW,
        )
        if replayed is not None:
            return replayed
        self._require_expected_version(task, expected_status_version)
        lease_session_id: UUID | None = None
        if actor.kind is TaskActorKind.OPERATOR:
            if lease_fence is None:
                raise TaskLeaseFenceError("an operator submit requires the current lease fence")
            lease_session_id = await self._require_current_lease(
                task, actor, lease_fence=lease_fence, now=timestamp
            )
            released = task.with_released_lease(now=timestamp)
            base = replace(released, result_refs=tuple(result_refs))
        else:
            if (
                task.assigned_actor_kind is not TaskActorKind.HUMAN
                or task.assigned_actor_id != actor.actor_id
            ):
                raise TaskPermissionError("a human only submits the task assigned to them")
            base = replace(task, result_refs=tuple(result_refs))
        self._require_transition(task, TaskStatus.AWAITING_REVIEW, actor)
        updated = base.with_status(TaskStatus.AWAITING_REVIEW, now=timestamp)
        result = await self._commit_status(
            task=task,
            updated=updated,
            actor=actor,
            event_type=TaskEventType.RESULT_SUBMITTED,
            idempotency_key=key,
            payload=payload,
            now=timestamp,
        )
        if lease_session_id is not None:
            await self._close_session(
                task, lease_session_id, TaskSessionOutcome.CLOSED, "submitted"
            )
        return result

    # ------------------------------------------------------------- verification

    async def accept_result(
        self,
        *,
        task_id: UUID,
        actor: TaskActor,
        expected_status_version: int,
        idempotency_key: str,
        reason: str,
        now: datetime | None = None,
    ) -> TaskCommandResult:
        """``awaiting_review -> completed`` plus the human acceptance decision."""
        return await self._decide_result(
            task_id=task_id,
            actor=actor,
            expected_status_version=expected_status_version,
            idempotency_key=idempotency_key,
            reason=reason,
            decision_type=AnalystDecisionType.TASK_RESULT_ACCEPT,
            target=TaskStatus.COMPLETED,
            now=now,
        )

    async def request_changes(
        self,
        *,
        task_id: UUID,
        actor: TaskActor,
        expected_status_version: int,
        idempotency_key: str,
        reason: str,
        now: datetime | None = None,
    ) -> TaskCommandResult:
        """``awaiting_review -> ready``: the human rejects the submitted result."""
        return await self._decide_result(
            task_id=task_id,
            actor=actor,
            expected_status_version=expected_status_version,
            idempotency_key=idempotency_key,
            reason=reason,
            decision_type=AnalystDecisionType.TASK_RESULT_CHANGES_REQUESTED,
            target=TaskStatus.READY,
            now=now,
        )

    async def _decide_result(
        self,
        *,
        task_id: UUID,
        actor: TaskActor,
        expected_status_version: int,
        idempotency_key: str,
        reason: str,
        decision_type: AnalystDecisionType,
        target: TaskStatus,
        now: datetime | None,
    ) -> TaskCommandResult:
        _require_human(actor)
        if not isinstance(reason, str) or not reason.strip():
            raise TaskCommandError("a result decision requires a reason")
        key = _idempotency_key(idempotency_key)
        timestamp = _now(now)
        await self._lock_investigation_for(task_id)
        task = await self._locked_task(task_id)
        decision_id = uuid5(
            _TASK_DECISION_NAMESPACE,
            f"{task.investigation_id}:{task.id}:{decision_type.value}:{key}",
        )
        payload: dict[str, Any] = {
            "expected_status_version": expected_status_version,
            "analyst_decision_id": str(decision_id),
            "reason": reason.strip(),
        }
        replayed = await self._replayed(
            task,
            idempotency_key=key,
            event_type=TaskEventType.VERIFICATION_RECORDED,
            actor=actor,
            payload=payload,
            to_status=target,
            analyst_decision_id=decision_id,
        )
        if replayed is not None:
            return replayed
        self._require_expected_version(task, expected_status_version)
        self._require_transition(task, target, actor)
        clear_assignment = decision_type is AnalystDecisionType.TASK_RESULT_CHANGES_REQUESTED
        base = (
            replace(task, assigned_actor_kind=None, assigned_actor_id=None)
            if clear_assignment
            else task
        )
        updated = base.with_status(target, now=timestamp)
        # The decision is the verification authority; it must exist before the
        # event that references it (the FK is checked at statement time).
        await self._decisions.append(
            AnalystDecision(
                id=decision_id,
                investigation_id=task.investigation_id,
                decision_type=decision_type,
                target_type=AnalystDecisionTargetType.TASK,
                target_id=task.id,
                actor_id=actor.actor_id,
                reason=reason.strip(),
                correlation_id=self._decision_correlation_id(task, decision_type, key),
            )
        )
        result = await self._commit_status(
            task=task,
            updated=updated,
            actor=actor,
            event_type=TaskEventType.VERIFICATION_RECORDED,
            idempotency_key=key,
            payload=payload,
            now=timestamp,
            analyst_decision_id=decision_id,
        )
        if decision_type is AnalystDecisionType.TASK_RESULT_ACCEPT:
            await self._reevaluate_dependents(task, now=timestamp)
        return result

    @staticmethod
    def _decision_correlation_id(
        task: InvestigationTaskV1, decision_type: AnalystDecisionType, key: str
    ) -> str:
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
        return f"task:{task.id}:{decision_type.value}:{digest}"

    # ------------------------------------------------------------------ block

    async def block(
        self,
        *,
        task_id: UUID,
        actor: TaskActor,
        expected_status_version: int,
        idempotency_key: str,
        blocked_reason: BlockedReason,
        lease_fence: int | None = None,
        now: datetime | None = None,
    ) -> TaskCommandResult:
        """Move a running/awaiting-input task to ``blocked`` and release its lease."""
        _require_actor(
            actor,
            frozenset({TaskActorKind.HUMAN, TaskActorKind.SYSTEM, TaskActorKind.OPERATOR}),
            "this actor may not block a task",
        )
        if not isinstance(blocked_reason, BlockedReason):
            raise TypeError("blocked_reason must use BlockedReason")
        key = _idempotency_key(idempotency_key)
        timestamp = _now(now)
        task = await self._locked_task(task_id)
        payload: dict[str, Any] = {
            "expected_status_version": expected_status_version,
            "blocked_reason": blocked_reason.value,
        }
        if actor.kind is TaskActorKind.OPERATOR:
            if lease_fence is None:
                raise TaskLeaseFenceError("an operator block requires the current lease fence")
            payload["lease_fence"] = lease_fence
        replayed = await self._replayed(
            task,
            idempotency_key=key,
            event_type=TaskEventType.STATUS_CHANGED,
            actor=actor,
            payload=payload,
            to_status=TaskStatus.BLOCKED,
        )
        if replayed is not None:
            return replayed
        self._require_expected_version(task, expected_status_version)
        lease_session_id: UUID | None = None
        if actor.kind is TaskActorKind.OPERATOR:
            if lease_fence is None:
                raise TaskLeaseFenceError("an operator block requires the current lease fence")
            lease_session_id = await self._require_current_lease(
                task, actor, lease_fence=lease_fence, now=timestamp
            )
        base = task
        if task.lease_id is not None:
            lease_session_id = task.lease_session_id
            base = task.with_released_lease(now=timestamp)
        self._require_transition(
            task,
            TaskStatus.BLOCKED,
            actor,
            target_blocked_reason=blocked_reason,
        )
        updated = base.with_status(TaskStatus.BLOCKED, now=timestamp, blocked_reason=blocked_reason)
        result = await self._commit_status(
            task=task,
            updated=updated,
            actor=actor,
            event_type=TaskEventType.STATUS_CHANGED,
            idempotency_key=key,
            payload=payload,
            now=timestamp,
        )
        if lease_session_id is not None:
            await self._close_session(task, lease_session_id, TaskSessionOutcome.CLOSED, "blocked")
        return result

    async def unblock(
        self,
        *,
        task_id: UUID,
        actor: TaskActor,
        expected_status_version: int,
        idempotency_key: str,
        now: datetime | None = None,
    ) -> TaskCommandResult:
        """``blocked -> ready``; the system only lifts a dependency block."""
        _require_actor(
            actor,
            frozenset({TaskActorKind.HUMAN, TaskActorKind.SYSTEM}),
            "this actor may not unblock a task",
        )
        key = _idempotency_key(idempotency_key)
        timestamp = _now(now)
        task = await self._locked_task(task_id)
        payload = {"expected_status_version": expected_status_version}
        replayed = await self._replayed(
            task,
            idempotency_key=key,
            event_type=TaskEventType.STATUS_CHANGED,
            actor=actor,
            payload=payload,
            to_status=TaskStatus.READY,
        )
        if replayed is not None:
            return replayed
        self._require_expected_version(task, expected_status_version)
        dependencies_completed = await self._dependencies_all_completed(task.id)
        self._require_transition(
            task,
            TaskStatus.READY,
            actor,
            dependencies_completed=dependencies_completed,
        )
        updated = task.with_status(TaskStatus.READY, now=timestamp)
        return await self._commit_status(
            task=task,
            updated=updated,
            actor=actor,
            event_type=TaskEventType.STATUS_CHANGED,
            idempotency_key=key,
            payload=payload,
            now=timestamp,
        )

    async def promote(
        self,
        *,
        task_id: UUID,
        actor: TaskActor,
        expected_status_version: int,
        idempotency_key: str,
        now: datetime | None = None,
    ) -> TaskCommandResult:
        """``proposed -> ready`` once every dependency is ``completed``."""
        _require_actor(
            actor,
            frozenset({TaskActorKind.HUMAN, TaskActorKind.SYSTEM}),
            "this actor may not promote a task",
        )
        key = _idempotency_key(idempotency_key)
        timestamp = _now(now)
        task = await self._locked_task(task_id)
        payload = {"expected_status_version": expected_status_version}
        replayed = await self._replayed(
            task,
            idempotency_key=key,
            event_type=TaskEventType.STATUS_CHANGED,
            actor=actor,
            payload=payload,
            to_status=TaskStatus.READY,
        )
        if replayed is not None:
            return replayed
        self._require_expected_version(task, expected_status_version)
        dependencies_completed = await self._dependencies_all_completed(task.id)
        self._require_transition(
            task,
            TaskStatus.READY,
            actor,
            dependencies_completed=dependencies_completed,
        )
        updated = task.with_status(TaskStatus.READY, now=timestamp)
        return await self._commit_status(
            task=task,
            updated=updated,
            actor=actor,
            event_type=TaskEventType.STATUS_CHANGED,
            idempotency_key=key,
            payload=payload,
            now=timestamp,
        )

    # --------------------------------------------------------------- terminal

    async def cancel(
        self,
        *,
        task_id: UUID,
        actor: TaskActor,
        expected_status_version: int,
        idempotency_key: str,
        reason: str,
        now: datetime | None = None,
    ) -> TaskCommandResult:
        """Human cancellation; revokes the lease and blocks unsatisfiable dependents."""
        _require_human(actor)
        if not isinstance(reason, str) or not reason.strip():
            raise TaskCommandError("a cancellation requires a reason")
        key = _idempotency_key(idempotency_key)
        timestamp = _now(now)
        await self._lock_investigation_for(task_id)
        task = await self._locked_task(task_id)
        payload: dict[str, Any] = {
            "expected_status_version": expected_status_version,
            "reason": reason.strip(),
        }
        replayed = await self._replayed(
            task,
            idempotency_key=key,
            event_type=TaskEventType.STATUS_CHANGED,
            actor=actor,
            payload=payload,
            to_status=TaskStatus.CANCELLED,
        )
        if replayed is not None:
            return replayed
        self._require_expected_version(task, expected_status_version)
        self._require_transition(task, TaskStatus.CANCELLED, actor)
        lease_session_id = task.lease_session_id
        base = task.with_released_lease(now=timestamp) if task.lease_id is not None else task
        updated = base.with_status(TaskStatus.CANCELLED, now=timestamp)
        result = await self._commit_status(
            task=task,
            updated=updated,
            actor=actor,
            event_type=TaskEventType.STATUS_CHANGED,
            idempotency_key=key,
            payload=payload,
            now=timestamp,
        )
        if lease_session_id is not None:
            await self._close_session(
                task, lease_session_id, TaskSessionOutcome.CLOSED, "cancelled"
            )
        await self._block_unsatisfiable_dependents(task, now=timestamp)
        return result

    async def fail(
        self,
        *,
        task_id: UUID,
        actor: TaskActor,
        expected_status_version: int,
        idempotency_key: str,
        reason: str,
        lease_fence: int | None = None,
        now: datetime | None = None,
    ) -> TaskCommandResult:
        """Terminal failure; the linked session is ``lost`` and dependents blocked."""
        _require_actor(
            actor,
            frozenset({TaskActorKind.HUMAN, TaskActorKind.SYSTEM, TaskActorKind.OPERATOR}),
            "this actor may not fail a task",
        )
        if not isinstance(reason, str) or not reason.strip():
            raise TaskCommandError("a failure requires a reason")
        key = _idempotency_key(idempotency_key)
        timestamp = _now(now)
        await self._lock_investigation_for(task_id)
        task = await self._locked_task(task_id)
        payload: dict[str, Any] = {
            "expected_status_version": expected_status_version,
            "reason": reason.strip(),
        }
        if actor.kind is TaskActorKind.OPERATOR:
            if lease_fence is None:
                raise TaskLeaseFenceError("an operator failure requires the current lease fence")
            payload["lease_fence"] = lease_fence
        replayed = await self._replayed(
            task,
            idempotency_key=key,
            event_type=TaskEventType.STATUS_CHANGED,
            actor=actor,
            payload=payload,
            to_status=TaskStatus.FAILED,
        )
        if replayed is not None:
            return replayed
        self._require_expected_version(task, expected_status_version)
        if actor.kind is TaskActorKind.OPERATOR:
            if lease_fence is None:
                raise TaskLeaseFenceError("an operator failure requires the current lease fence")
            await self._require_current_lease(task, actor, lease_fence=lease_fence, now=timestamp)
        lease_session_id = task.lease_session_id
        base = task.with_released_lease(now=timestamp) if task.lease_id is not None else task
        self._require_transition(task, TaskStatus.FAILED, actor)
        updated = base.with_status(TaskStatus.FAILED, now=timestamp)
        result = await self._commit_status(
            task=task,
            updated=updated,
            actor=actor,
            event_type=TaskEventType.STATUS_CHANGED,
            idempotency_key=key,
            payload=payload,
            now=timestamp,
        )
        if lease_session_id is not None:
            await self._close_session(task, lease_session_id, TaskSessionOutcome.LOST, "failed")
        await self._block_unsatisfiable_dependents(task, now=timestamp)
        return result

    # ------------------------------------------------------------ dependencies

    async def add_dependency(
        self,
        *,
        task_id: UUID,
        depends_on_task_id: UUID,
        actor: TaskActor,
    ) -> TaskDependencyResult:
        """Append one ``task depends on`` edge under the investigation lock.

        Dependencies are the authority in their own append-only table; the
        closed D2 event vocabulary has no dependency event, so this command
        writes no task event.  It is idempotent by the edge primary key.
        """
        _require_actor(
            actor,
            frozenset({TaskActorKind.HUMAN, TaskActorKind.SYSTEM}),
            "only a human or the system can add a dependency",
        )
        _uuid(task_id, "task_id")
        _uuid(depends_on_task_id, "depends_on_task_id")
        if task_id == depends_on_task_id:
            raise TaskDependencyError("a task cannot depend on itself")
        await self._lock_investigation_for(task_id)
        task = await self._locked_task(task_id)
        dependency = await self._locked_task(depends_on_task_id)
        if dependency.investigation_id != task.investigation_id:
            raise TaskDependencyError("a dependency must join two tasks of one investigation")
        if task.status is not TaskStatus.PROPOSED:
            raise TaskDependencyError("dependencies are declared while the task is proposed")
        existing = await self._tasks.list_dependencies(task.id)
        if depends_on_task_id in existing:
            return TaskDependencyResult(
                task=task, depends_on_task_id=depends_on_task_id, created=False
            )
        if len(existing) >= self._limits.max_dependencies:
            raise TaskLimitExceededError("maximum dependencies reached")
        await self._require_no_dependency_cycle(task.id, depends_on_task_id)
        await self._tasks.add_dependency(
            investigation_id=task.investigation_id,
            task_id=task.id,
            depends_on_task_id=depends_on_task_id,
        )
        return TaskDependencyResult(task=task, depends_on_task_id=depends_on_task_id, created=True)

    async def _require_no_dependency_cycle(self, task_id: UUID, depends_on_task_id: UUID) -> None:
        """Refuse an edge that would close a cycle; caller holds the advisory lock."""
        pending: list[UUID] = [depends_on_task_id]
        visited: set[UUID] = set()
        while pending:
            current = pending.pop()
            if current == task_id:
                raise TaskDependencyError("the dependency edge would create a cycle")
            if current in visited:
                continue
            visited.add(current)
            pending.extend(await self._tasks.list_dependencies(current))

    async def _dependencies_all_completed(self, task_id: UUID) -> bool:
        for dependency_id in await self._tasks.list_dependencies(task_id):
            dependency = await self._tasks.get(dependency_id)
            if dependency is None or dependency.status is not TaskStatus.COMPLETED:
                return False
        return True

    async def _dependencies_unsatisfiable(self, task_id: UUID) -> bool:
        for dependency_id in await self._tasks.list_dependencies(task_id):
            dependency = await self._tasks.get(dependency_id)
            if dependency is not None and dependency.status in UNSATISFIED_TERMINAL_STATUSES:
                return True
        return False

    async def _reevaluate_dependents(
        self, completed: InvestigationTaskV1, *, now: datetime
    ) -> None:
        """System promotion/block after a dependency completes."""
        for dependent_id in await self._tasks.list_dependents(completed.id):
            dependent = await self._tasks.lock_task(dependent_id)
            if dependent is None:
                continue
            if dependent.status is TaskStatus.PROPOSED:
                if await self._dependencies_all_completed(dependent.id):
                    await self._system_transition(
                        dependent,
                        TaskStatus.READY,
                        source_task_id=completed.id,
                        now=now,
                    )
                elif await self._dependencies_unsatisfiable(dependent.id):
                    await self._system_transition(
                        dependent,
                        TaskStatus.BLOCKED,
                        source_task_id=completed.id,
                        now=now,
                        blocked_reason=BlockedReason.DEPENDENCY,
                    )
            elif (
                dependent.status is TaskStatus.BLOCKED
                and dependent.blocked_reason is BlockedReason.DEPENDENCY
                and await self._dependencies_all_completed(dependent.id)
            ):
                await self._system_transition(
                    dependent,
                    TaskStatus.READY,
                    source_task_id=completed.id,
                    now=now,
                )

    async def _block_unsatisfiable_dependents(
        self, closed: InvestigationTaskV1, *, now: datetime
    ) -> None:
        """``cancelled``/``failed`` never satisfy a dependency (E6)."""
        for dependent_id in await self._tasks.list_dependents(closed.id):
            dependent = await self._tasks.lock_task(dependent_id)
            if dependent is None or dependent.status is not TaskStatus.PROPOSED:
                continue
            if await self._dependencies_unsatisfiable(dependent.id):
                await self._system_transition(
                    dependent,
                    TaskStatus.BLOCKED,
                    source_task_id=closed.id,
                    now=now,
                    blocked_reason=BlockedReason.DEPENDENCY,
                )

    async def _system_transition(
        self,
        dependent: InvestigationTaskV1,
        target: TaskStatus,
        *,
        source_task_id: UUID,
        now: datetime,
        blocked_reason: BlockedReason | None = None,
    ) -> None:
        dependencies_completed = await self._dependencies_all_completed(dependent.id)
        self._require_transition(
            dependent,
            target,
            SYSTEM_TASK_ACTOR,
            dependencies_completed=dependencies_completed,
            target_blocked_reason=blocked_reason,
        )
        updated = dependent.with_status(target, now=now, blocked_reason=blocked_reason)
        key = f"dep:{source_task_id}:{dependent.id}:{dependent.status_version}"
        draft = TaskEventDraft(
            investigation_id=dependent.investigation_id,
            task_id=dependent.id,
            event_type=TaskEventType.STATUS_CHANGED,
            actor=SYSTEM_TASK_ACTOR,
            idempotency_key=key,
            from_status=dependent.status,
            to_status=target,
            payload={
                "expected_status_version": dependent.status_version,
                "source_task_id": str(source_task_id),
            },
        )
        recorded = await self._tasks.get_event(dependent.id, key)
        if recorded is not None:
            if recorded.request_sha256 != draft.request_sha256:
                raise TaskIdempotencyConflictError(
                    "a different system transition already used this dependency key"
                )
            return
        written = await self._tasks.update_status(
            updated,
            expected_status_version=dependent.status_version,
            expected_status=dependent.status,
        )
        if written is None:
            raise StaleTaskVersionError("the dependent task changed during system promotion")
        await append_task_event(self._tasks, draft, occurred_at=now)

    # ----------------------------------------------------------------- helpers

    async def _lock_investigation_for(self, task_id: UUID) -> None:
        """Take the advisory lock before any row lock (deadlock-free order)."""
        probe = await self._tasks.get(task_id)
        if probe is None:
            raise TaskNotFoundCommandError(f"task {task_id} does not exist")
        await self._tasks.lock_investigation(probe.investigation_id)

    async def _locked_task(self, task_id: UUID) -> InvestigationTaskV1:
        task = await self._tasks.lock_task(task_id)
        if task is None:
            raise TaskNotFoundCommandError(f"task {task_id} does not exist")
        return task

    async def _current_task(
        self, task_id: UUID, fallback: InvestigationTaskV1
    ) -> InvestigationTaskV1:
        """Re-read after an event append so the result carries ``event_sequence``."""
        current = await self._tasks.get(task_id)
        return current if current is not None else fallback

    @staticmethod
    def _require_expected_version(task: InvestigationTaskV1, expected: int) -> None:
        if not isinstance(expected, int) or isinstance(expected, bool) or expected < 1:
            raise TaskCommandError("expected_status_version must be a positive integer")
        if task.status_version != expected:
            raise StaleTaskVersionError(
                f"expected status_version {expected}, current is {task.status_version}"
            )

    @staticmethod
    def _require_transition(
        task: InvestigationTaskV1,
        target: TaskStatus,
        actor: TaskActor,
        *,
        dependencies_completed: bool = False,
        target_blocked_reason: BlockedReason | None = None,
    ) -> None:
        context = TaskTransitionContext.for_task(
            task,
            dependencies_completed=dependencies_completed,
            target_blocked_reason=target_blocked_reason,
        )
        try:
            validate_transition(task.status, target, actor.kind, context)
        except TaskTransitionError as exc:
            raise TaskTransitionNotAllowedError(str(exc)) from exc

    @staticmethod
    async def _require_current_lease(
        task: InvestigationTaskV1, actor: TaskActor, *, lease_fence: int, now: datetime
    ) -> UUID:
        if (
            task.assigned_actor_kind is not TaskActorKind.OPERATOR
            or task.assigned_actor_id != actor.actor_id
        ):
            raise TaskPermissionError("an operator only writes its own assigned task")
        if task.lease_id is None or task.lease_session_id is None or task.lease_expires_at is None:
            raise TaskLeaseExpiredError("the task holds no lease")
        if task.lease_fence != lease_fence:
            raise TaskLeaseFenceError(
                f"expected lease_fence {lease_fence}, current is {task.lease_fence}"
            )
        if task.lease_expires_at <= now:
            raise TaskLeaseExpiredError("the lease has expired")
        return task.lease_session_id

    async def _require_active_capacity(
        self, task: InvestigationTaskV1, *, exclude_task_id: UUID
    ) -> None:
        tasks = await self._tasks.list_for_investigation(task.investigation_id)
        active = [
            item
            for item in tasks
            if item.id != exclude_task_id and item.status in ACTIVE_TASK_STATUSES
        ]
        if len(active) >= self._limits.max_active_per_investigation:
            raise TaskLimitExceededError("maximum active tasks for this investigation reached")

    async def _require_session_running_capacity(
        self, task: InvestigationTaskV1, *, session_id: UUID
    ) -> None:
        tasks = await self._tasks.list_for_investigation(task.investigation_id)
        running = [
            item
            for item in tasks
            if item.id != task.id
            and item.lease_session_id == session_id
            and item.status in LEASE_HELD_STATUSES
        ]
        if len(running) >= self._limits.max_running_per_session:
            raise TaskLimitExceededError("this session already runs the maximum number of tasks")

    async def _replayed(
        self,
        task: InvestigationTaskV1,
        *,
        idempotency_key: str,
        event_type: TaskEventType,
        actor: TaskActor,
        payload: Mapping[str, Any],
        to_status: TaskStatus | None,
        analyst_decision_id: UUID | None = None,
    ) -> TaskCommandResult | None:
        recorded = await self._tasks.get_event(task.id, idempotency_key)
        if recorded is None:
            return None
        # A retry cannot know the *from* status it originally observed, so the
        # body identity is rebuilt from the recorded event; the requested
        # to-status stays the caller's, so reusing a key for another command
        # is a conflict rather than a replay.
        draft = TaskEventDraft(
            investigation_id=task.investigation_id,
            task_id=task.id,
            event_type=event_type,
            actor=actor,
            idempotency_key=idempotency_key,
            from_status=recorded.from_status,
            to_status=to_status,
            analyst_decision_id=analyst_decision_id,
            payload=payload,
        )
        if draft.request_sha256 != recorded.request_sha256:
            raise TaskIdempotencyConflictError(
                "Idempotency-Key already records a different task command"
            )
        return TaskCommandResult(task=task, event=recorded, replayed=True)

    def _event_draft(
        self,
        *,
        task: InvestigationTaskV1,
        event_type: TaskEventType,
        actor: TaskActor,
        idempotency_key: str,
        from_status: TaskStatus | None,
        to_status: TaskStatus | None,
        payload: Mapping[str, Any],
        analyst_decision_id: UUID | None = None,
    ) -> TaskEventDraft:
        return TaskEventDraft(
            investigation_id=task.investigation_id,
            task_id=task.id,
            event_type=event_type,
            actor=actor,
            idempotency_key=idempotency_key,
            from_status=from_status,
            to_status=to_status,
            analyst_decision_id=analyst_decision_id,
            payload=payload,
        )

    async def _commit_status(
        self,
        *,
        task: InvestigationTaskV1,
        updated: InvestigationTaskV1,
        actor: TaskActor,
        event_type: TaskEventType,
        idempotency_key: str,
        payload: Mapping[str, Any],
        now: datetime,
        analyst_decision_id: UUID | None = None,
    ) -> TaskCommandResult:
        written = await self._tasks.update_status(
            updated,
            expected_status_version=task.status_version,
            expected_status=task.status,
        )
        if written is None:
            raise StaleTaskVersionError("the task changed before the command was applied")
        draft = self._event_draft(
            task=task,
            event_type=event_type,
            actor=actor,
            idempotency_key=idempotency_key,
            from_status=task.status,
            to_status=updated.status,
            payload=payload,
            analyst_decision_id=analyst_decision_id,
        )
        event, _ = await append_task_event(self._tasks, draft, occurred_at=now)
        return TaskCommandResult(task=await self._current_task(written.id, written), event=event)

    async def _close_session(
        self,
        task: InvestigationTaskV1,
        session_id: UUID,
        outcome: TaskSessionOutcome,
        reason: str,
    ) -> None:
        await self._sessions.close_session(
            session_id=session_id,
            investigation_id=task.investigation_id,
            task_id=task.id,
            outcome=outcome,
            reason=reason,
        )


__all__ = [
    "ACTIVE_TASK_STATUSES",
    "SYSTEM_TASK_ACTOR",
    "InvestigationTaskService",
    "StaleTaskVersionError",
    "TaskCommandError",
    "TaskCommandResult",
    "TaskDependencyError",
    "TaskDependencyResult",
    "TaskIdempotencyConflictError",
    "TaskKeyConflictError",
    "TaskLeaseExpiredError",
    "TaskLeaseFenceError",
    "TaskLimitExceededError",
    "TaskNotFoundCommandError",
    "TaskPermissionError",
    "TaskSessionInactiveError",
    "TaskSessionOutcome",
    "TaskSessionPort",
    "TaskTransitionNotAllowedError",
]
