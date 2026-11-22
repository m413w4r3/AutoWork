"""System sweep for expired investigation-task leases (M24-04B2, ADR-0011 D3).

The sweep is the only automatic transition: a task left ``running`` or
``awaiting_input`` past its lease is moved to ``blocked`` with
``blocked_reason='lease_expired'``, its lease is released (``lease_fence + 1``)
and the linked session is reported ``lost``.  Without this path an agent that
disappeared would leave a task occupied forever, so ``awaiting_input`` is swept
exactly like ``running`` (E6).

The transition goes through the same D3 primitives as a command: the
investigation advisory lock serialises sweeps with dependency work, the task
row is locked, the status write is a ``status_version`` CAS and the event is
appended through ``append_task_event`` (the sole ``event_sequence`` writer).
The event key is deterministic — ``sweep:<task_id>:<from_status>:<status_version>``
— so a retried sweep cannot apply twice, and a concurrent human command or
heartbeat that wins the row first leaves the sweep a clean no-op.

``now`` defaults to the process clock.  A scheduler should pass the database's
``statement_timestamp()`` so the expiry boundary is evaluated against the same
clock the heartbeat predicate uses.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from uuid import UUID

from cti_app.application.investigation_persistence import InvestigationTaskRepository
from cti_app.application.investigation_task_events import (
    TaskEventDraft,
    TaskEventType,
    append_task_event,
)
from cti_app.application.investigation_tasks import (
    LEASE_HELD_STATUSES,
    SYSTEM_TASK_ACTOR,
    TaskCommandResult,
    TaskSessionOutcome,
    TaskSessionPort,
)
from cti_app.domain.investigation_task import (
    BlockedReason,
    TaskStatus,
)


def _now(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(UTC)
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    return value


class InvestigationTaskSweeper:
    """Block tasks whose lease expired without a heartbeat."""

    def __init__(
        self,
        *,
        tasks: InvestigationTaskRepository,
        sessions: TaskSessionPort,
    ) -> None:
        self._tasks = tasks
        self._sessions = sessions

    async def sweep(
        self, investigation_ids: Iterable[UUID], *, now: datetime | None = None
    ) -> tuple[TaskCommandResult, ...]:
        """Sweep several investigations; each is serialised independently.

        A global enumeration of investigations belongs to the scheduler wiring;
        this service only needs the candidate ids, so it never invents a second
        query over the investigation aggregate.
        """
        timestamp = _now(now)
        swept: list[TaskCommandResult] = []
        for investigation_id in investigation_ids:
            swept.extend(await self.sweep_investigation(investigation_id, now=timestamp))
        return tuple(swept)

    async def sweep_investigation(
        self, investigation_id: UUID, *, now: datetime | None = None
    ) -> tuple[TaskCommandResult, ...]:
        timestamp = _now(now)
        swept: list[TaskCommandResult] = []
        await self._tasks.lock_investigation(investigation_id)
        for candidate in await self._tasks.list_for_investigation(investigation_id):
            if candidate.status not in LEASE_HELD_STATUSES:
                continue
            if candidate.lease_expires_at is None or candidate.lease_expires_at > timestamp:
                continue
            result = await self._expire(candidate.id, now=timestamp)
            if result is not None:
                swept.append(result)
        return tuple(swept)

    async def _expire(self, task_id: UUID, *, now: datetime) -> TaskCommandResult | None:
        task = await self._tasks.lock_task(task_id)
        if task is None:
            return None
        if (
            task.status not in LEASE_HELD_STATUSES
            or task.lease_expires_at is None
            or task.lease_expires_at > now
            or task.lease_session_id is None
        ):
            return None
        key = f"sweep:{task.id}:{task.status.value}:{task.status_version}"
        draft = TaskEventDraft(
            investigation_id=task.investigation_id,
            task_id=task.id,
            event_type=TaskEventType.LEASE_EXPIRED,
            actor=SYSTEM_TASK_ACTOR,
            idempotency_key=key,
            from_status=task.status,
            to_status=TaskStatus.BLOCKED,
            payload={
                "expected_status_version": task.status_version,
                "lease_expires_at": task.lease_expires_at.isoformat(),
            },
        )
        recorded = await self._tasks.get_event(task.id, key)
        if recorded is not None:
            if recorded.request_sha256 != draft.request_sha256:
                raise ValueError("a different sweep event already used this lease identity")
            return None
        released = task.with_released_lease(now=now)
        expired = released.with_status(
            TaskStatus.BLOCKED, now=now, blocked_reason=BlockedReason.LEASE_EXPIRED
        )
        written = await self._tasks.update_status(
            expired,
            expected_status_version=task.status_version,
            expected_status=task.status,
        )
        if written is None:
            return None
        event, _ = await append_task_event(self._tasks, draft, occurred_at=now)
        await self._sessions.close_session(
            session_id=task.lease_session_id,
            investigation_id=task.investigation_id,
            task_id=task.id,
            outcome=TaskSessionOutcome.LOST,
            reason="lease_expired",
        )
        current = await self._tasks.get(task.id)
        return TaskCommandResult(task=current if current is not None else written, event=event)


__all__ = ["InvestigationTaskSweeper"]
