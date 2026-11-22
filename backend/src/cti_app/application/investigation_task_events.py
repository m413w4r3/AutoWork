"""The single append path for the investigation task business-event journal.

ADR-0011 D3 makes ``investigation_task_events`` the append-only journal of a
task's business status and makes one function, :func:`append_task_event`, the
sole writer of ``investigation_tasks.event_sequence``.  A status command first
advances ``status_version`` through its CAS update and then appends one event;
a non-status event (``lease_renewed``, ``session_state_changed``,
``contribution_imported``) appends without touching ``status_version``.  No
other UPDATE may increment ``event_sequence``.

Idempotency is scoped per task: the unique key is ``(task_id,
idempotency_key)``, never the investigation.  Replaying the same key with the
same request body returns the recorded event without a second effect; the same
key with a different body is a conflict, detected through a canonical
``request_sha256`` that excludes the event's own id, sequence and timestamp.
Every append locks the task row first, so two writers of one task are
serialised and an event can never observe a half-applied command.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any, cast
from uuid import UUID, uuid4

from cti_app.domain.investigation import canonical_json_bytes, freeze_json, thaw_json
from cti_app.domain.investigation_task import TaskActor, TaskStatus

if TYPE_CHECKING:
    from cti_app.application.investigation_persistence import InvestigationTaskRepository

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class TaskEventType(StrEnum):
    """The closed event vocabulary of ADR-0011 D2."""

    TASK_CREATED = "task_created"
    STATUS_CHANGED = "status_changed"
    ASSIGNED = "assigned"
    LEASE_ACQUIRED = "lease_acquired"
    LEASE_RENEWED = "lease_renewed"
    LEASE_RELEASED = "lease_released"
    LEASE_EXPIRED = "lease_expired"
    RESULT_SUBMITTED = "result_submitted"
    VERIFICATION_RECORDED = "verification_recorded"
    SESSION_STARTED = "session_started"
    SESSION_STATE_CHANGED = "session_state_changed"
    HANDOFF_CREATED = "handoff_created"
    CONTRIBUTION_IMPORTED = "contribution_imported"


class TaskEventError(ValueError):
    """Base refusal for an event that cannot be appended."""

    code = "task_event_invalid"


class TaskNotFoundError(TaskEventError):
    """The target task does not exist."""

    code = "task_not_found"


class TaskEventConflictError(TaskEventError):
    """An idempotency key was replayed with a different request body."""

    code = "idempotency_conflict"


def _aware(value: object, name: str) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError(f"{name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value


def _payload(value: object) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError("payload must be a JSON object")
    frozen = freeze_json(value)
    if not isinstance(frozen, Mapping):
        raise TypeError("payload must be a JSON object")
    return cast(Mapping[str, Any], frozen)


@dataclass(frozen=True, slots=True, kw_only=True)
class TaskEventDraft:
    """The caller's request to append one event, before locking the task.

    The draft deliberately omits ``sequence``, ``status_version`` and
    ``lease_fence``: those are read from the locked task row so a retry can
    never invent a different ordering than the one the database recorded.
    """

    investigation_id: UUID
    task_id: UUID
    event_type: TaskEventType
    actor: TaskActor
    idempotency_key: str
    from_status: TaskStatus | None = None
    to_status: TaskStatus | None = None
    analyst_decision_id: UUID | None = None
    payload: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("investigation_id", "task_id"):
            if not isinstance(getattr(self, name), UUID):
                raise TypeError(f"{name} must be a UUID")
        if not isinstance(self.event_type, TaskEventType):
            raise TypeError("event_type must use TaskEventType")
        if not isinstance(self.actor, TaskActor):
            raise TypeError("actor must use TaskActor")
        if not isinstance(self.idempotency_key, str) or not self.idempotency_key.strip():
            raise ValueError("idempotency_key must not be empty")
        for name in ("from_status", "to_status"):
            value = getattr(self, name)
            if value is not None and not isinstance(value, TaskStatus):
                raise TypeError(f"{name} must use TaskStatus")
        if self.analyst_decision_id is not None and not isinstance(self.analyst_decision_id, UUID):
            raise TypeError("analyst_decision_id must be a UUID or None")
        object.__setattr__(self, "payload", _payload(self.payload))

    @property
    def request_sha256(self) -> str:
        """Canonical identity of this request body, stable across retries."""
        identity = {
            "investigation_id": str(self.investigation_id),
            "task_id": str(self.task_id),
            "event_type": self.event_type.value,
            "from_status": self.from_status.value if self.from_status is not None else None,
            "to_status": self.to_status.value if self.to_status is not None else None,
            "actor_kind": self.actor.kind.value,
            "actor_id": self.actor.actor_id,
            "idempotency_key": self.idempotency_key,
            "analyst_decision_id": (
                str(self.analyst_decision_id) if self.analyst_decision_id is not None else None
            ),
            "payload": thaw_json(self.payload),
        }
        return hashlib.sha256(canonical_json_bytes(identity)).hexdigest()


@dataclass(frozen=True, slots=True, kw_only=True)
class TaskEvent:
    """One immutable row of ``investigation_task_events``."""

    investigation_id: UUID
    task_id: UUID
    sequence: int
    event_type: TaskEventType
    status_version: int
    actor: TaskActor
    idempotency_key: str
    request_sha256: str
    occurred_at: datetime
    from_status: TaskStatus | None = None
    to_status: TaskStatus | None = None
    lease_fence: int | None = None
    analyst_decision_id: UUID | None = None
    payload: Mapping[str, Any] = field(default_factory=dict)
    id: UUID = field(default_factory=uuid4)

    def __post_init__(self) -> None:
        for name in ("investigation_id", "task_id", "id"):
            if not isinstance(getattr(self, name), UUID):
                raise TypeError(f"{name} must be a UUID")
        if not isinstance(self.event_type, TaskEventType):
            raise TypeError("event_type must use TaskEventType")
        if not isinstance(self.actor, TaskActor):
            raise TypeError("actor must use TaskActor")
        for name in ("sequence", "status_version"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"{name} must be an integer")
            if value < 1:
                raise ValueError(f"{name} must be >= 1")
        if self.lease_fence is not None:
            if isinstance(self.lease_fence, bool) or not isinstance(self.lease_fence, int):
                raise TypeError("lease_fence must be an integer or None")
            if self.lease_fence < 0:
                raise ValueError("lease_fence must be >= 0")
        if not isinstance(self.idempotency_key, str) or not self.idempotency_key.strip():
            raise ValueError("idempotency_key must not be empty")
        if not isinstance(self.request_sha256, str) or not _SHA256_RE.fullmatch(
            self.request_sha256
        ):
            raise ValueError("request_sha256 must be a lowercase SHA-256")
        for name in ("from_status", "to_status"):
            value = getattr(self, name)
            if value is not None and not isinstance(value, TaskStatus):
                raise TypeError(f"{name} must use TaskStatus")
        if self.analyst_decision_id is not None and not isinstance(self.analyst_decision_id, UUID):
            raise TypeError("analyst_decision_id must be a UUID or None")
        object.__setattr__(self, "payload", _payload(self.payload))
        _aware(self.occurred_at, "occurred_at")


async def append_task_event(
    repository: InvestigationTaskRepository,
    draft: TaskEventDraft,
    *,
    occurred_at: datetime | None = None,
) -> tuple[TaskEvent, bool]:
    """Append one task event inside the caller's unit of work.

    Returns ``(event, created)``.  ``created`` is ``False`` for an idempotent
    replay of the same ``(task_id, idempotency_key)`` and identical body; a
    different body with the same key raises :class:`TaskEventConflictError`.
    The task row is locked before the key is examined, so concurrent writers of
    one task serialise and the sequence has no gaps from replays.
    """
    if not isinstance(draft, TaskEventDraft):
        raise TypeError("draft must use TaskEventDraft")
    timestamp = occurred_at if occurred_at is not None else datetime.now(UTC)
    _aware(timestamp, "occurred_at")

    task = await repository.lock_task(draft.task_id)
    if task is None:
        raise TaskNotFoundError(f"task {draft.task_id} does not exist")
    if task.investigation_id != draft.investigation_id:
        raise TaskEventError("event belongs to another investigation than its task")

    existing = await repository.get_event(draft.task_id, draft.idempotency_key)
    if existing is not None:
        if existing.request_sha256 != draft.request_sha256:
            raise TaskEventConflictError(
                "Idempotency-Key already records a different task event request"
            )
        return existing, False

    if draft.to_status is not None and draft.to_status is not task.status:
        raise TaskEventError("a status event must record the status the locked task now holds")

    sequence = await repository.advance_event_sequence(draft.task_id)
    event = TaskEvent(
        investigation_id=draft.investigation_id,
        task_id=draft.task_id,
        sequence=sequence,
        event_type=draft.event_type,
        status_version=task.status_version,
        actor=draft.actor,
        idempotency_key=draft.idempotency_key,
        request_sha256=draft.request_sha256,
        occurred_at=timestamp,
        from_status=draft.from_status,
        to_status=draft.to_status,
        lease_fence=task.lease_fence,
        analyst_decision_id=draft.analyst_decision_id,
        payload=draft.payload,
    )
    return await repository.add_event(event), True
