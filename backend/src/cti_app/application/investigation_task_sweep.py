"""System sweep for expired investigation-task leases (M24-04B2, ADR-0011 D3).

The sweep is the only automatic transition: a task left ``running`` or
``awaiting_input`` past its lease is moved to ``blocked`` with
``blocked_reason='lease_expired'``, its lease is released (``lease_fence + 1``)
and the linked session is reported ``lost``.  Without this path an agent that
disappeared would leave a task occupied forever, so ``awaiting_input`` is swept
exactly like ``running`` (E6).

The expiry boundary is the *database* clock: ``database_now()`` is the same
``statement_timestamp()`` the heartbeat predicate uses, read again after the
task row is locked so the authoritative comparison cannot be decided by a
drifting process clock.  The in-memory fake supplies that clock itself.

The transition goes through the same D3 primitives as a command: the
investigation advisory lock serialises sweeps with dependency work, the task
row is locked, the status write is a ``status_version`` CAS and the event is
appended through ``append_task_event`` (the sole ``event_sequence`` writer).
The event key is deterministic — ``sweep:<task_id>:<from_status>:<status_version>``
— so a retried sweep cannot apply twice, and a concurrent human command or
heartbeat that wins the row first leaves the sweep a clean no-op.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable, Sequence
from types import TracebackType
from typing import Protocol, Self
from uuid import UUID

from cti_app.application.investigation_persistence import (
    AgentSessionRepository,
    InvestigationTaskRepository,
)
from cti_app.application.investigation_task_events import (
    TaskEventDraft,
    TaskEventType,
    append_task_event,
)
from cti_app.application.investigation_tasks import (
    LEASE_HELD_STATUSES,
    SYSTEM_TASK_ACTOR,
    RepositoryTaskSessionPort,
    TaskCommandResult,
    TaskSessionOutcome,
    TaskSessionPort,
    TaskSessionRelease,
)
from cti_app.domain.investigation_task import (
    BlockedReason,
    TaskStatus,
)


class ExpiredLeaseSource(Protocol):
    """The one global read the scheduler needs; no second task aggregate."""

    async def list_expired_lease_investigation_ids(self, *, limit: int) -> tuple[UUID, ...]: ...


class SweepUnitOfWork(Protocol):
    """The sweep's narrow UoW seam (task + session repositories, one commit)."""

    investigation_tasks: InvestigationTaskRepository
    agent_sessions: AgentSessionRepository

    async def __aenter__(self) -> Self: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...

    async def commit(self) -> None: ...


async def run_expired_lease_sweep(
    *,
    source: ExpiredLeaseSource,
    uow_factory: Callable[[], SweepUnitOfWork],
    flush_releases: Callable[[Sequence[TaskSessionRelease]], Awaitable[None]],
    batch_size: int,
) -> int:
    """Sweep every candidate investigation, one short transaction each.

    Each investigation commits its status/event/session-row changes before the
    host release is flushed, so the host socket is never used while the
    advisory or task-row lock is held (B2/B3).  A candidate that a concurrent
    heartbeat revived is a clean no-op and is not counted.
    """
    candidates = await source.list_expired_lease_investigation_ids(limit=batch_size)
    swept = 0
    for investigation_id in candidates:
        releases: list[TaskSessionRelease] = []
        async with uow_factory() as uow:
            sweeper = InvestigationTaskSweeper(
                tasks=uow.investigation_tasks,
                sessions=RepositoryTaskSessionPort(uow.agent_sessions, releases),
            )
            results = await sweeper.sweep_investigation(investigation_id)
            await uow.commit()
        swept += len(results)
        await flush_releases(releases)
    return swept


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

    async def sweep(self, investigation_ids: Iterable[UUID]) -> tuple[TaskCommandResult, ...]:
        """Sweep several investigations; each is serialised independently.

        A global enumeration of investigations belongs to the scheduler wiring;
        this service only needs the candidate ids, so it never invents a second
        query over the investigation aggregate.
        """
        swept: list[TaskCommandResult] = []
        for investigation_id in investigation_ids:
            swept.extend(await self.sweep_investigation(investigation_id))
        return tuple(swept)

    async def sweep_investigation(self, investigation_id: UUID) -> tuple[TaskCommandResult, ...]:
        swept: list[TaskCommandResult] = []
        await self._tasks.lock_investigation(investigation_id)
        candidates = await self._tasks.list_for_investigation(investigation_id)
        if not any(candidate.status in LEASE_HELD_STATUSES for candidate in candidates):
            return ()
        timestamp = await self._tasks.database_now()
        for candidate in candidates:
            if candidate.status not in LEASE_HELD_STATUSES:
                continue
            if candidate.lease_expires_at is None or candidate.lease_expires_at > timestamp:
                continue
            result = await self._expire(candidate.id)
            if result is not None:
                swept.append(result)
        return tuple(swept)

    async def _expire(self, task_id: UUID) -> TaskCommandResult | None:
        task = await self._tasks.lock_task(task_id)
        if task is None:
            return None
        if (
            task.status not in LEASE_HELD_STATUSES
            or task.lease_expires_at is None
            or task.lease_session_id is None
        ):
            return None
        # The row is locked now; this is the authoritative boundary read.
        now = await self._tasks.database_now()
        if task.lease_expires_at > now:
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


__all__ = [
    "ExpiredLeaseSource",
    "InvestigationTaskSweeper",
    "SweepUnitOfWork",
    "run_expired_lease_sweep",
]
