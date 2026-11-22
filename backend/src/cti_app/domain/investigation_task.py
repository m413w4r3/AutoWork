"""Strict, immutable investigation task aggregate and its state machine.

ADR-0011 D1 makes ``investigation_tasks.status`` + ``status_version`` the sole
authority for a task's business status; ``result_verification_status`` plus an
``analyst_decisions`` row is the sole authority for result verification, and
``lease_*``/``lease_fence`` are the sole lease authority.  This module owns the
D3 transition table as data, the pure transition validator, and the state
invariants every persisted row must satisfy.  It performs no I/O: comments,
locks, sessions and events belong to the application lots.

Actor vocabulary is deliberately split.  Tasks accept ``human``, ``operator``
and ``system`` (the system only promotes or blocks dependencies and sweeps
leases); a *current assignment* may only name a human or an operator, and a
tool invocation records only the two execution kinds (``human``/``operator``,
ADR-0011 E4).  Bridge is never an actor: it influences the workshop only
through persisted consultations and server-materialised plans.

The D3 conditions that are pure are encoded in :class:`TaskTransitionContext`:
dependency completion, the human assignee required by human ``start``/``submit``,
the dependency reason required for a system block, and the system promotion of
a dependency-blocked task.  Lease currency, CAS and session liveness are
runtime checks performed by the engine in the same UoW; they are not facts a
pure function can decide.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import StrEnum
from typing import Any, cast
from uuid import UUID, uuid4

from cti_app.domain.errors import DomainError
from cti_app.domain.investigation import freeze_json


class TaskError(DomainError):
    """Base refusal for an investigation task operation."""


class TaskTransitionError(TaskError):
    """The (from, to, actor) triple is absent from the ADR D3 table."""


class TaskStateError(TaskError):
    """A persisted task state violates an ADR D2/D3 invariant."""


class TaskStatus(StrEnum):
    PROPOSED = "proposed"
    READY = "ready"
    ASSIGNED = "assigned"
    RUNNING = "running"
    AWAITING_INPUT = "awaiting_input"
    AWAITING_REVIEW = "awaiting_review"
    BLOCKED = "blocked"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"


TERMINAL_TASK_STATUSES = frozenset({TaskStatus.COMPLETED, TaskStatus.CANCELLED, TaskStatus.FAILED})


class ResultVerificationStatus(StrEnum):
    UNVERIFIED = "unverified"
    PENDING_REVIEW = "pending_review"
    ACCEPTED = "accepted"
    REJECTED = "rejected"


class BlockedReason(StrEnum):
    DEPENDENCY = "dependency"
    CAPABILITY = "capability"
    APPROVAL = "approval"
    SESSION_LOST = "session_lost"
    LEASE_EXPIRED = "lease_expired"
    INPUT_UNAVAILABLE = "input_unavailable"
    EXTERNAL = "external"


class TaskActorKind(StrEnum):
    HUMAN = "human"
    OPERATOR = "operator"
    SYSTEM = "system"


class TaskPriority(StrEnum):
    CRITICAL = "critical"
    HIGH = "high"
    NORMAL = "normal"
    LOW = "low"


_ASSIGNABLE_ACTOR_KINDS = frozenset({TaskActorKind.HUMAN, TaskActorKind.OPERATOR})


@dataclass(frozen=True, slots=True, kw_only=True)
class TaskActor:
    """One acting identity with its kind and stable caller-provided id."""

    kind: TaskActorKind
    actor_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.kind, TaskActorKind):
            raise TypeError("actor kind must use TaskActorKind")
        if not isinstance(self.actor_id, str) or not self.actor_id.strip():
            raise ValueError("actor_id must not be empty")


@dataclass(frozen=True, slots=True, kw_only=True)
class TaskLease:
    """A complete operator lease: id, session, fence and heartbeat window."""

    lease_id: UUID
    lease_session_id: UUID
    lease_fence: int
    lease_expires_at: datetime
    heartbeat_at: datetime

    def __post_init__(self) -> None:
        for name in ("lease_id", "lease_session_id"):
            if not isinstance(getattr(self, name), UUID):
                raise TypeError(f"{name} must be a UUID")
        if isinstance(self.lease_fence, bool) or not isinstance(self.lease_fence, int):
            raise TypeError("lease_fence must be an integer")
        if self.lease_fence < 1:
            raise ValueError("a complete lease requires lease_fence >= 1")
        _aware(self.heartbeat_at, "heartbeat_at")
        _aware(self.lease_expires_at, "lease_expires_at")
        if self.lease_expires_at <= self.heartbeat_at:
            raise ValueError("lease_expires_at must follow heartbeat_at")


@dataclass(frozen=True, slots=True, kw_only=True)
class TaskLimits:
    """ADR-0011 D3 limits, in the wording of their environment variables.

    Enforcement (depth/children/dependencies/active work) belongs to the task
    engine; this object keeps the ADR defaults and the lease bounds in one
    place so the engine and the API cannot drift apart.
    """

    max_depth: int = 3
    max_children: int = 20
    max_dependencies: int = 16
    max_active_per_investigation: int = 8
    max_running_per_session: int = 1
    lease_seconds: int = 180
    max_contribution_bytes: int = 16 * 1024 * 1024

    def __post_init__(self) -> None:
        for name in (
            "max_depth",
            "max_children",
            "max_dependencies",
            "max_active_per_investigation",
            "max_running_per_session",
            "lease_seconds",
            "max_contribution_bytes",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"{name} must be an integer")
            if value < 0:
                raise ValueError(f"{name} must be non-negative")
        if self.max_active_per_investigation < 1:
            raise ValueError("max_active_per_investigation must be >= 1")
        if self.max_running_per_session < 1:
            raise ValueError("max_running_per_session must be >= 1")
        if not 30 <= self.lease_seconds <= 3600:
            raise ValueError("lease_seconds must be between 30 and 3600")


TASK_LIMIT_DEFAULTS = TaskLimits()


class TransitionCondition(StrEnum):
    """The pure D3 conditions not already expressed by the actor column."""

    DEPENDENCIES_COMPLETED = "dependencies_completed"
    HUMAN_ASSIGNEE = "human_assignee"
    SYSTEM_DEPENDENCY_PROMOTION = "system_dependency_promotion"
    BLOCKED_BY_DEPENDENCY = "blocked_by_dependency"


@dataclass(frozen=True, slots=True, kw_only=True)
class TaskTransitionContext:
    """Facts the caller must establish before asking for a transition.

    ``dependencies_completed`` is fail-closed: the default ``False`` makes a
    promotion refuse until the engine has checked every dependency.  ``None``
    is accepted for the assigned-kind/reason fields only where the rule does
    not need them.
    """

    current_assigned_actor_kind: TaskActorKind | None = None
    current_blocked_reason: BlockedReason | None = None
    dependencies_completed: bool = False
    target_blocked_reason: BlockedReason | None = None

    def __post_init__(self) -> None:
        if self.current_assigned_actor_kind is not None and (
            self.current_assigned_actor_kind not in _ASSIGNABLE_ACTOR_KINDS
        ):
            raise TypeError("a task is only assigned to a human or an operator")
        if self.current_blocked_reason is not None and not isinstance(
            self.current_blocked_reason, BlockedReason
        ):
            raise TypeError("current_blocked_reason must use BlockedReason")
        if self.target_blocked_reason is not None and not isinstance(
            self.target_blocked_reason, BlockedReason
        ):
            raise TypeError("target_blocked_reason must use BlockedReason")
        if not isinstance(self.dependencies_completed, bool):
            raise TypeError("dependencies_completed must be a boolean")

    @classmethod
    def for_task(
        cls,
        task: InvestigationTaskV1,
        *,
        dependencies_completed: bool = False,
        target_blocked_reason: BlockedReason | None = None,
    ) -> TaskTransitionContext:
        return cls(
            current_assigned_actor_kind=task.assigned_actor_kind,
            current_blocked_reason=task.blocked_reason,
            dependencies_completed=dependencies_completed,
            target_blocked_reason=target_blocked_reason,
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class TaskTransitionRule:
    """One row fragment of the D3 table: actors admitted by one condition."""

    actor_kinds: frozenset[TaskActorKind]
    condition: TransitionCondition | None = None

    def __post_init__(self) -> None:
        if not self.actor_kinds or not self.actor_kinds <= frozenset(TaskActorKind):
            raise TypeError("actor_kinds must be a non-empty TaskActorKind set")
        if self.condition is not None and not isinstance(self.condition, TransitionCondition):
            raise TypeError("condition must use TransitionCondition")


_HUMAN = frozenset({TaskActorKind.HUMAN})
_OPERATOR = frozenset({TaskActorKind.OPERATOR})
_SYSTEM = frozenset({TaskActorKind.SYSTEM})
_OPERATOR_SYSTEM = frozenset({TaskActorKind.OPERATOR, TaskActorKind.SYSTEM})
_HUMAN_SYSTEM = frozenset({TaskActorKind.HUMAN, TaskActorKind.SYSTEM})
_ALL_ACTORS = frozenset(TaskActorKind)

# ADR-0011 D3, row by row.  A (from, to) pair may list several rules when the
# table admits different actors under different conditions (human start/submit
# versus operator start/submit; human versus system unblock).
TRANSITION_RULES: Mapping[tuple[TaskStatus, TaskStatus], tuple[TaskTransitionRule, ...]] = {
    (TaskStatus.PROPOSED, TaskStatus.READY): (
        TaskTransitionRule(
            actor_kinds=_HUMAN_SYSTEM,
            condition=TransitionCondition.DEPENDENCIES_COMPLETED,
        ),
    ),
    (TaskStatus.PROPOSED, TaskStatus.BLOCKED): (
        TaskTransitionRule(
            actor_kinds=_SYSTEM,
            condition=TransitionCondition.BLOCKED_BY_DEPENDENCY,
        ),
    ),
    (TaskStatus.PROPOSED, TaskStatus.CANCELLED): (TaskTransitionRule(actor_kinds=_HUMAN),),
    (TaskStatus.READY, TaskStatus.ASSIGNED): (TaskTransitionRule(actor_kinds=_HUMAN),),
    (TaskStatus.READY, TaskStatus.CANCELLED): (TaskTransitionRule(actor_kinds=_HUMAN),),
    (TaskStatus.ASSIGNED, TaskStatus.READY): (TaskTransitionRule(actor_kinds=_HUMAN),),
    (TaskStatus.ASSIGNED, TaskStatus.RUNNING): (
        TaskTransitionRule(actor_kinds=_OPERATOR),
        TaskTransitionRule(actor_kinds=_HUMAN, condition=TransitionCondition.HUMAN_ASSIGNEE),
    ),
    (TaskStatus.ASSIGNED, TaskStatus.CANCELLED): (TaskTransitionRule(actor_kinds=_HUMAN),),
    (TaskStatus.RUNNING, TaskStatus.AWAITING_INPUT): (TaskTransitionRule(actor_kinds=_OPERATOR),),
    (TaskStatus.RUNNING, TaskStatus.AWAITING_REVIEW): (
        TaskTransitionRule(actor_kinds=_OPERATOR),
        TaskTransitionRule(actor_kinds=_HUMAN, condition=TransitionCondition.HUMAN_ASSIGNEE),
    ),
    (TaskStatus.RUNNING, TaskStatus.BLOCKED): (TaskTransitionRule(actor_kinds=_ALL_ACTORS),),
    (TaskStatus.RUNNING, TaskStatus.FAILED): (TaskTransitionRule(actor_kinds=_ALL_ACTORS),),
    (TaskStatus.RUNNING, TaskStatus.CANCELLED): (TaskTransitionRule(actor_kinds=_HUMAN),),
    (TaskStatus.AWAITING_INPUT, TaskStatus.RUNNING): (TaskTransitionRule(actor_kinds=_OPERATOR),),
    (TaskStatus.AWAITING_INPUT, TaskStatus.BLOCKED): (TaskTransitionRule(actor_kinds=_ALL_ACTORS),),
    (TaskStatus.AWAITING_INPUT, TaskStatus.FAILED): (
        TaskTransitionRule(actor_kinds=_OPERATOR_SYSTEM),
    ),
    (TaskStatus.AWAITING_INPUT, TaskStatus.CANCELLED): (TaskTransitionRule(actor_kinds=_HUMAN),),
    (TaskStatus.AWAITING_REVIEW, TaskStatus.COMPLETED): (TaskTransitionRule(actor_kinds=_HUMAN),),
    (TaskStatus.AWAITING_REVIEW, TaskStatus.READY): (TaskTransitionRule(actor_kinds=_HUMAN),),
    (TaskStatus.AWAITING_REVIEW, TaskStatus.FAILED): (TaskTransitionRule(actor_kinds=_HUMAN),),
    (TaskStatus.AWAITING_REVIEW, TaskStatus.CANCELLED): (TaskTransitionRule(actor_kinds=_HUMAN),),
    (TaskStatus.BLOCKED, TaskStatus.READY): (
        TaskTransitionRule(actor_kinds=_HUMAN),
        TaskTransitionRule(
            actor_kinds=_SYSTEM,
            condition=TransitionCondition.SYSTEM_DEPENDENCY_PROMOTION,
        ),
    ),
    (TaskStatus.BLOCKED, TaskStatus.ASSIGNED): (TaskTransitionRule(actor_kinds=_HUMAN),),
    (TaskStatus.BLOCKED, TaskStatus.CANCELLED): (TaskTransitionRule(actor_kinds=_HUMAN),),
    (TaskStatus.BLOCKED, TaskStatus.FAILED): (TaskTransitionRule(actor_kinds=_HUMAN_SYSTEM),),
}


def _condition_holds(condition: TransitionCondition, context: TaskTransitionContext) -> bool:
    if condition is TransitionCondition.DEPENDENCIES_COMPLETED:
        return context.dependencies_completed
    if condition is TransitionCondition.HUMAN_ASSIGNEE:
        return context.current_assigned_actor_kind is TaskActorKind.HUMAN
    if condition is TransitionCondition.SYSTEM_DEPENDENCY_PROMOTION:
        return (
            context.current_blocked_reason is BlockedReason.DEPENDENCY
            and context.dependencies_completed
        )
    if condition is TransitionCondition.BLOCKED_BY_DEPENDENCY:
        return context.target_blocked_reason is BlockedReason.DEPENDENCY
    raise TaskTransitionError(f"unknown transition condition {condition}")


def validate_transition(
    from_status: TaskStatus,
    to_status: TaskStatus,
    actor_kind: TaskActorKind,
    context: TaskTransitionContext | None = None,
) -> None:
    """Refuse anything the ADR D3 table does not name for this actor.

    Raises :class:`TaskTransitionError`.  The engine still owns CAS, lease
    currency, session liveness and result payload checks; this function only
    decides whether the business transition is legal for the acting kind.
    """
    if not isinstance(from_status, TaskStatus):
        raise TypeError("from_status must use TaskStatus")
    if not isinstance(to_status, TaskStatus):
        raise TypeError("to_status must use TaskStatus")
    if not isinstance(actor_kind, TaskActorKind):
        raise TypeError("actor_kind must use TaskActorKind")
    rules = TRANSITION_RULES.get((from_status, to_status))
    if not rules:
        raise TaskTransitionError(
            f"{from_status.value} -> {to_status.value} is not an ADR D3 transition"
        )
    effective = context if context is not None else TaskTransitionContext()
    admitted = [rule for rule in rules if actor_kind in rule.actor_kinds]
    if not admitted:
        raise TaskTransitionError(
            f"{actor_kind.value} may not move a task from {from_status.value} to {to_status.value}"
        )
    if any(
        rule.condition is None or _condition_holds(rule.condition, effective) for rule in admitted
    ):
        return
    conditions = ", ".join(
        sorted(rule.condition.value for rule in admitted if rule.condition is not None)
    )
    raise TaskTransitionError(
        f"{from_status.value} -> {to_status.value} by {actor_kind.value} requires "
        f"unsatisfied condition(s): {conditions}"
    )


def result_verification_after_transition(
    from_status: TaskStatus,
    to_status: TaskStatus,
    *,
    current: ResultVerificationStatus,
) -> ResultVerificationStatus:
    """The M5 verification rule for one business transition.

    ``awaiting_review`` is the only state that may hold ``pending_review`` and
    only ``completed`` may hold ``accepted``.  Entering ``awaiting_review``
    writes ``pending_review``; accepting writes ``accepted``; every other exit
    from ``awaiting_review`` writes ``rejected``.  Unrelated transitions keep
    the current value.
    """
    if not isinstance(from_status, TaskStatus):
        raise TypeError("from_status must use TaskStatus")
    if not isinstance(to_status, TaskStatus):
        raise TypeError("to_status must use TaskStatus")
    if not isinstance(current, ResultVerificationStatus):
        raise TypeError("current must use ResultVerificationStatus")
    if to_status is TaskStatus.AWAITING_REVIEW:
        return ResultVerificationStatus.PENDING_REVIEW
    if to_status is TaskStatus.COMPLETED:
        return ResultVerificationStatus.ACCEPTED
    if from_status is TaskStatus.AWAITING_REVIEW:
        return ResultVerificationStatus.REJECTED
    if current in {
        ResultVerificationStatus.ACCEPTED,
        ResultVerificationStatus.PENDING_REVIEW,
    }:
        raise TaskStateError(
            f"accepted/pending_review verification cannot persist on {to_status.value}"
        )
    return current


def _aware(value: object, name: str) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError(f"{name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value


def _text(value: object, name: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    normalized = value.strip()
    if not normalized and not allow_empty:
        raise ValueError(f"{name} must not be empty")
    return normalized


def _references(value: object, name: str) -> tuple[str, ...]:
    if not isinstance(value, (tuple, list)):
        raise TypeError(f"{name} must be a sequence of references")
    normalized: list[str] = []
    seen: set[str] = set()
    for item in value:
        reference = _text(item, f"{name} entry")
        if reference in seen:
            continue
        seen.add(reference)
        normalized.append(reference)
    return tuple(normalized)


def _json_refs(value: object, name: str) -> tuple[Mapping[str, Any], ...]:
    if not isinstance(value, (tuple, list)):
        raise TypeError(f"{name} must be a sequence of JSON objects")
    normalized: list[Mapping[str, Any]] = []
    for item in value:
        if not isinstance(item, Mapping):
            raise TypeError(f"{name} entries must be JSON objects")
        frozen = freeze_json(item)
        if not isinstance(frozen, Mapping):
            raise TypeError(f"{name} entries must be JSON objects")
        normalized.append(cast(Mapping[str, Any], frozen))
    return tuple(normalized)


def _json_object(value: object, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{name} must be a JSON object")
    frozen = freeze_json(value)
    if not isinstance(frozen, Mapping):
        raise TypeError(f"{name} must be a JSON object")
    return cast(Mapping[str, Any], frozen)


@dataclass(frozen=True, slots=True, kw_only=True)
class InvestigationTaskV1:
    """One durable investigation task, exactly as ADR-0011 D2 sketches it.

    The post-init checks mirror the PostgreSQL CHECK constraints one for one,
    so an object the engine accepts can always be persisted and a row the
    database accepts can always be hydrated.
    """

    investigation_id: UUID
    task_key: str
    title: str
    question: str
    objective: str
    priority: TaskPriority
    status: TaskStatus
    status_version: int
    result_verification_status: ResultVerificationStatus
    created_by: TaskActor
    created_at: datetime
    updated_at: datetime
    id: UUID = field(default_factory=uuid4)
    plan_version_id: UUID | None = None
    parent_task_id: UUID | None = None
    depth: int = 0
    claim_refs: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    sample_refs: tuple[str, ...] = ()
    required_capabilities: tuple[str, ...] = ()
    allowed_workspace_paths: tuple[str, ...] = ()
    execution_policy: Mapping[str, Any] = field(default_factory=dict)
    expected_outputs: tuple[str, ...] = ()
    blocked_reason: BlockedReason | None = None
    assigned_actor_kind: TaskActorKind | None = None
    assigned_actor_id: str | None = None
    lease_id: UUID | None = None
    lease_session_id: UUID | None = None
    lease_fence: int = 0
    lease_expires_at: datetime | None = None
    heartbeat_at: datetime | None = None
    handoff_ref: UUID | None = None
    result_refs: tuple[Mapping[str, Any], ...] = ()
    event_sequence: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.id, UUID):
            raise TypeError("id must be a UUID")
        if not isinstance(self.investigation_id, UUID):
            raise TypeError("investigation_id must be a UUID")
        object.__setattr__(self, "task_key", _text(self.task_key, "task_key"))
        object.__setattr__(self, "title", _text(self.title, "title"))
        object.__setattr__(self, "question", _text(self.question, "question", allow_empty=True))
        object.__setattr__(self, "objective", _text(self.objective, "objective", allow_empty=True))
        if not isinstance(self.priority, TaskPriority):
            raise TypeError("priority must use TaskPriority")
        if not isinstance(self.status, TaskStatus):
            raise TypeError("status must use TaskStatus")
        if not isinstance(self.result_verification_status, ResultVerificationStatus):
            raise TypeError("result_verification_status must use ResultVerificationStatus")
        if not isinstance(self.created_by, TaskActor):
            raise TypeError("created_by must use TaskActor")
        if self.blocked_reason is not None and not isinstance(self.blocked_reason, BlockedReason):
            raise TypeError("blocked_reason must use BlockedReason")
        for name in ("status_version", "event_sequence", "depth", "lease_fence"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"{name} must be an integer")
        if self.status_version < 1:
            raise ValueError("status_version must be >= 1")
        if self.event_sequence < 0:
            raise ValueError("event_sequence must be >= 0")
        if self.depth < 0:
            raise ValueError("depth must be >= 0")
        if self.lease_fence < 0:
            raise ValueError("lease_fence must be >= 0")
        for name in ("plan_version_id", "parent_task_id", "handoff_ref"):
            value = getattr(self, name)
            if value is not None and not isinstance(value, UUID):
                raise TypeError(f"{name} must be a UUID or None")
        if self.parent_task_id == self.id:
            raise ValueError("A task cannot be its own parent")
        if (self.parent_task_id is None) != (self.depth == 0):
            raise TaskStateError("parent_task_id must be set exactly when depth > 0")
        for name in ("claim_refs", "evidence_refs", "sample_refs", "required_capabilities"):
            object.__setattr__(self, name, _references(getattr(self, name), name))
        object.__setattr__(
            self,
            "allowed_workspace_paths",
            _references(self.allowed_workspace_paths, "allowed_workspace_paths"),
        )
        object.__setattr__(
            self, "expected_outputs", _references(self.expected_outputs, "expected_outputs")
        )
        object.__setattr__(
            self, "execution_policy", _json_object(self.execution_policy, "execution_policy")
        )
        object.__setattr__(self, "result_refs", _json_refs(self.result_refs, "result_refs"))
        _aware(self.created_at, "created_at")
        _aware(self.updated_at, "updated_at")
        if self.updated_at < self.created_at:
            raise ValueError("updated_at cannot precede created_at")

        if (self.assigned_actor_kind is None) != (self.assigned_actor_id is None):
            raise TaskStateError("assignment requires both an actor kind and an actor id")
        if self.assigned_actor_kind is not None:
            if self.assigned_actor_kind not in _ASSIGNABLE_ACTOR_KINDS:
                raise TaskStateError("a task is only assigned to a human or an operator")
            object.__setattr__(
                self, "assigned_actor_id", _text(self.assigned_actor_id, "assigned_actor_id")
            )

        lease_fields = (
            self.lease_id,
            self.lease_session_id,
            self.lease_expires_at,
            self.heartbeat_at,
        )
        has_any_lease = any(value is not None for value in lease_fields)
        if has_any_lease:
            if any(value is None for value in lease_fields):
                raise TaskStateError("an operator lease is either entirely null or complete")
            lease_id = cast(UUID, self.lease_id)
            lease_session_id = cast(UUID, self.lease_session_id)
            lease_expires_at = cast(datetime, self.lease_expires_at)
            heartbeat_at = cast(datetime, self.heartbeat_at)
            TaskLease(
                lease_id=lease_id,
                lease_session_id=lease_session_id,
                lease_fence=self.lease_fence,
                heartbeat_at=heartbeat_at,
                lease_expires_at=lease_expires_at,
            )
            if self.assigned_actor_kind is TaskActorKind.HUMAN:
                raise TaskStateError("a human-assigned task never holds a lease")
        if self.status in TERMINAL_TASK_STATUSES and self.lease_id is not None:
            raise TaskStateError("a terminal task cannot hold a lease")

        if (self.status is TaskStatus.COMPLETED) != (
            self.result_verification_status is ResultVerificationStatus.ACCEPTED
        ):
            raise TaskStateError("only completed holds accepted; accepted requires completed")
        if (self.status is TaskStatus.AWAITING_REVIEW) != (
            self.result_verification_status is ResultVerificationStatus.PENDING_REVIEW
        ):
            raise TaskStateError(
                "awaiting_review holds pending_review and pending_review requires awaiting_review"
            )
        if (self.blocked_reason is not None) != (self.status is TaskStatus.BLOCKED):
            raise TaskStateError("blocked_reason is set exactly when the task is blocked")

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_TASK_STATUSES

    @property
    def lease(self) -> TaskLease | None:
        if self.lease_id is None:
            return None
        if (
            self.lease_session_id is None
            or self.lease_expires_at is None
            or self.heartbeat_at is None
        ):
            raise TaskStateError("an operator lease is either entirely null or complete")
        return TaskLease(
            lease_id=self.lease_id,
            lease_session_id=self.lease_session_id,
            lease_fence=self.lease_fence,
            heartbeat_at=self.heartbeat_at,
            lease_expires_at=self.lease_expires_at,
        )

    @property
    def next_lease_fence(self) -> int:
        """The fence a new acquisition or lease release must use."""
        return self.lease_fence + 1

    def with_assignment(self, actor: TaskActor, *, now: datetime) -> InvestigationTaskV1:
        if not isinstance(actor, TaskActor):
            raise TypeError("actor must use TaskActor")
        if actor.kind not in _ASSIGNABLE_ACTOR_KINDS:
            raise TaskStateError("only a human or an operator can be assigned")
        return replace(
            self,
            assigned_actor_kind=actor.kind,
            assigned_actor_id=actor.actor_id,
            status=TaskStatus.ASSIGNED,
            status_version=self.status_version + 1,
            updated_at=_aware(now, "now"),
        )

    def with_lease(self, lease: TaskLease, *, now: datetime) -> InvestigationTaskV1:
        if not isinstance(lease, TaskLease):
            raise TypeError("lease must use TaskLease")
        if self.assigned_actor_kind is not TaskActorKind.OPERATOR:
            raise TaskStateError("only an operator-assigned task acquires a lease")
        return replace(
            self,
            lease_id=lease.lease_id,
            lease_session_id=lease.lease_session_id,
            lease_fence=lease.lease_fence,
            lease_expires_at=lease.lease_expires_at,
            heartbeat_at=lease.heartbeat_at,
            status=TaskStatus.RUNNING,
            status_version=self.status_version + 1,
            updated_at=_aware(now, "now"),
        )

    def with_released_lease(self, *, now: datetime) -> InvestigationTaskV1:
        return replace(
            self,
            lease_id=None,
            lease_session_id=None,
            lease_expires_at=None,
            heartbeat_at=None,
            lease_fence=self.lease_fence + 1,
            updated_at=_aware(now, "now"),
        )

    def with_heartbeat(
        self, *, heartbeat_at: datetime, lease_expires_at: datetime
    ) -> InvestigationTaskV1:
        if self.lease_id is None:
            raise TaskStateError("a task without a lease cannot renew one")
        return replace(
            self,
            heartbeat_at=_aware(heartbeat_at, "heartbeat_at"),
            lease_expires_at=_aware(lease_expires_at, "lease_expires_at"),
            updated_at=heartbeat_at,
        )

    def with_status(
        self,
        status: TaskStatus,
        *,
        now: datetime,
        blocked_reason: BlockedReason | None = None,
    ) -> InvestigationTaskV1:
        """Apply one business status change and the M5 verification rule.

        The caller must already have validated the transition and must release
        the lease first: D3 releases it on ``awaiting_review``, ``blocked`` and
        every terminal status, so moving into one of them while a lease is
        still present is refused instead of silently dropping the fence.
        """
        if not isinstance(status, TaskStatus):
            raise TypeError("status must use TaskStatus")
        if (blocked_reason is not None) != (status is TaskStatus.BLOCKED):
            raise TaskStateError("blocked_reason must be set exactly for blocked")
        if self.lease_id is not None and (
            status in TERMINAL_TASK_STATUSES
            or status in (TaskStatus.AWAITING_REVIEW, TaskStatus.BLOCKED)
        ):
            raise TaskStateError(
                "this transition releases the lease; release it before changing status"
            )
        verification = result_verification_after_transition(
            self.status, status, current=self.result_verification_status
        )
        return replace(
            self,
            status=status,
            status_version=self.status_version + 1,
            result_verification_status=verification,
            blocked_reason=blocked_reason,
            updated_at=_aware(now, "now"),
        )
