"""SQLAlchemy repository for investigation tasks, dependencies and events.

ADR-0011 D3 places every SQL primitive here so the task engine (lot 04B2)
composes them without owning schema code:

* ``lock_investigation`` takes the transaction-scoped advisory lock that
  serialises cycle checks and subtask insertion per investigation (M2).
* ``lock_task`` is the ``SELECT ... FOR UPDATE`` that makes ``append_task_event``
  the sole, gap-free writer of ``event_sequence`` (M4).
* ``update_status`` is the compare-and-swap status write
  (``status_version + 1`` where the expected version and status still match).
* ``acquire_lease`` and ``renew_lease`` are the D3 lease SQL: acquisition bumps
  the fence atomically, the heartbeat is conditional and never resurrects an
  expired lease (M1).
* Dependencies and events are append-only by database trigger.

The heartbeat boundary is the *database* clock: the expiry predicate and the
new ``heartbeat_at``/``lease_expires_at`` values are evaluated with
``statement_timestamp()`` inside the UPDATE, so a process clock that drifts
from PostgreSQL cannot renew a dead lease or refuse a live one.
``database_now`` exposes that same clock to the system sweep.

``update_status`` never writes ``event_sequence``: only
``advance_event_sequence`` does, and only inside ``append_task_event``.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from cti_app.application.investigation_persistence import VersionConflictError
from cti_app.application.investigation_task_events import TaskEvent, TaskEventType
from cti_app.domain.investigation import thaw_json
from cti_app.domain.investigation_task import (
    BlockedReason,
    InvestigationTaskV1,
    ResultVerificationStatus,
    TaskActor,
    TaskActorKind,
    TaskPriority,
    TaskStatus,
)
from cti_app.infrastructure.database.models.investigation_tasks import (
    InvestigationTaskDependencyRow,
    InvestigationTaskEventRow,
    InvestigationTaskRow,
)


def _task_from_row(row: InvestigationTaskRow) -> InvestigationTaskV1:
    return InvestigationTaskV1(
        id=row.id,
        investigation_id=row.investigation_id,
        task_key=row.task_key,
        plan_version_id=row.plan_version_id,
        parent_task_id=row.parent_task_id,
        depth=row.depth,
        title=row.title,
        question=row.question,
        objective=row.objective,
        priority=TaskPriority(row.priority),
        claim_refs=tuple(row.claim_refs),
        evidence_refs=tuple(row.evidence_refs),
        sample_refs=tuple(row.sample_refs),
        required_capabilities=tuple(row.required_capabilities),
        allowed_workspace_paths=tuple(row.allowed_workspace_paths),
        execution_policy=row.execution_policy,
        expected_outputs=tuple(row.expected_outputs),
        status=TaskStatus(row.status),
        status_version=row.status_version,
        result_verification_status=ResultVerificationStatus(row.result_verification_status),
        blocked_reason=BlockedReason(row.blocked_reason) if row.blocked_reason else None,
        assigned_actor_kind=(
            TaskActorKind(row.assigned_actor_kind) if row.assigned_actor_kind else None
        ),
        assigned_actor_id=row.assigned_actor_id,
        lease_id=row.lease_id,
        lease_session_id=row.lease_session_id,
        lease_fence=row.lease_fence,
        lease_expires_at=row.lease_expires_at,
        heartbeat_at=row.heartbeat_at,
        handoff_ref=row.handoff_ref,
        result_refs=tuple(row.result_refs),
        event_sequence=row.event_sequence,
        created_by=TaskActor(
            kind=TaskActorKind(row.created_by_actor_kind),
            actor_id=row.created_by_actor_id,
        ),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _task_values(task: InvestigationTaskV1) -> dict[str, Any]:
    return {
        "id": task.id,
        "investigation_id": task.investigation_id,
        "task_key": task.task_key,
        "plan_version_id": task.plan_version_id,
        "parent_task_id": task.parent_task_id,
        "depth": task.depth,
        "title": task.title,
        "question": task.question,
        "objective": task.objective,
        "priority": task.priority.value,
        "claim_refs": list(task.claim_refs),
        "evidence_refs": list(task.evidence_refs),
        "sample_refs": list(task.sample_refs),
        "required_capabilities": list(task.required_capabilities),
        "allowed_workspace_paths": list(task.allowed_workspace_paths),
        "execution_policy": thaw_json(task.execution_policy),
        "expected_outputs": list(task.expected_outputs),
        "status": task.status.value,
        "status_version": task.status_version,
        "result_verification_status": task.result_verification_status.value,
        "blocked_reason": task.blocked_reason.value if task.blocked_reason else None,
        "assigned_actor_kind": (
            task.assigned_actor_kind.value if task.assigned_actor_kind else None
        ),
        "assigned_actor_id": task.assigned_actor_id,
        "lease_id": task.lease_id,
        "lease_session_id": task.lease_session_id,
        "lease_fence": task.lease_fence,
        "lease_expires_at": task.lease_expires_at,
        "heartbeat_at": task.heartbeat_at,
        "handoff_ref": task.handoff_ref,
        "result_refs": [thaw_json(item) for item in task.result_refs],
        "event_sequence": task.event_sequence,
        "created_by_actor_kind": task.created_by.kind.value,
        "created_by_actor_id": task.created_by.actor_id,
        "created_at": task.created_at,
        "updated_at": task.updated_at,
    }


def _event_from_row(row: InvestigationTaskEventRow) -> TaskEvent:
    return TaskEvent(
        id=row.id,
        investigation_id=row.investigation_id,
        task_id=row.task_id,
        sequence=row.sequence,
        event_type=TaskEventType(row.event_type),
        from_status=TaskStatus(row.from_status) if row.from_status else None,
        to_status=TaskStatus(row.to_status) if row.to_status else None,
        status_version=row.status_version,
        lease_fence=row.lease_fence,
        actor=TaskActor(kind=TaskActorKind(row.actor_kind), actor_id=row.actor_id),
        idempotency_key=row.idempotency_key,
        request_sha256=row.request_sha256,
        analyst_decision_id=row.analyst_decision_id,
        payload=row.payload,
        occurred_at=row.occurred_at,
    )


def _event_values(event: TaskEvent) -> dict[str, Any]:
    return {
        "id": event.id,
        "investigation_id": event.investigation_id,
        "task_id": event.task_id,
        "sequence": event.sequence,
        "event_type": event.event_type.value,
        "from_status": event.from_status.value if event.from_status else None,
        "to_status": event.to_status.value if event.to_status else None,
        "status_version": event.status_version,
        "lease_fence": event.lease_fence,
        "actor_kind": event.actor.kind.value,
        "actor_id": event.actor.actor_id,
        "idempotency_key": event.idempotency_key,
        "request_sha256": event.request_sha256,
        "analyst_decision_id": event.analyst_decision_id,
        "payload": thaw_json(event.payload),
        "occurred_at": event.occurred_at,
    }


class SqlAlchemyInvestigationTaskRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def lock_investigation(self, investigation_id: UUID) -> None:
        """Serialise dependency/limit checks for one investigation (D3 M2)."""
        await self._session.execute(
            text(
                "SELECT pg_advisory_xact_lock(hashtextextended(CAST(:investigation_id AS text), 0))"
            ),
            {"investigation_id": str(investigation_id)},
        )

    async def get(self, task_id: UUID) -> InvestigationTaskV1 | None:
        row = await self._session.scalar(
            select(InvestigationTaskRow)
            .where(InvestigationTaskRow.id == task_id)
            .execution_options(populate_existing=True)
        )
        return _task_from_row(row) if row is not None else None

    async def get_by_task_key(
        self, investigation_id: UUID, task_key: str
    ) -> InvestigationTaskV1 | None:
        row = await self._session.scalar(
            select(InvestigationTaskRow)
            .where(
                InvestigationTaskRow.investigation_id == investigation_id,
                InvestigationTaskRow.task_key == task_key,
            )
            .execution_options(populate_existing=True)
        )
        return _task_from_row(row) if row is not None else None

    async def list_for_investigation(
        self, investigation_id: UUID
    ) -> tuple[InvestigationTaskV1, ...]:
        rows = await self._session.scalars(
            select(InvestigationTaskRow)
            .where(InvestigationTaskRow.investigation_id == investigation_id)
            .order_by(InvestigationTaskRow.created_at, InvestigationTaskRow.id)
            .execution_options(populate_existing=True)
        )
        return tuple(_task_from_row(row) for row in rows)

    async def list_children(self, parent_task_id: UUID) -> tuple[InvestigationTaskV1, ...]:
        rows = await self._session.scalars(
            select(InvestigationTaskRow)
            .where(InvestigationTaskRow.parent_task_id == parent_task_id)
            .order_by(InvestigationTaskRow.created_at, InvestigationTaskRow.id)
            .execution_options(populate_existing=True)
        )
        return tuple(_task_from_row(row) for row in rows)

    async def list_dependencies(self, task_id: UUID) -> tuple[UUID, ...]:
        rows = await self._session.scalars(
            select(InvestigationTaskDependencyRow.depends_on_task_id)
            .where(InvestigationTaskDependencyRow.task_id == task_id)
            .order_by(InvestigationTaskDependencyRow.depends_on_task_id)
        )
        return tuple(rows)

    async def list_dependents(self, depends_on_task_id: UUID) -> tuple[UUID, ...]:
        rows = await self._session.scalars(
            select(InvestigationTaskDependencyRow.task_id)
            .where(InvestigationTaskDependencyRow.depends_on_task_id == depends_on_task_id)
            .order_by(InvestigationTaskDependencyRow.task_id)
        )
        return tuple(rows)

    async def get_event(self, task_id: UUID, idempotency_key: str) -> TaskEvent | None:
        row = await self._session.scalar(
            select(InvestigationTaskEventRow).where(
                InvestigationTaskEventRow.task_id == task_id,
                InvestigationTaskEventRow.idempotency_key == idempotency_key,
            )
        )
        return _event_from_row(row) if row is not None else None

    async def list_events(self, task_id: UUID) -> tuple[TaskEvent, ...]:
        rows = await self._session.scalars(
            select(InvestigationTaskEventRow)
            .where(InvestigationTaskEventRow.task_id == task_id)
            .order_by(InvestigationTaskEventRow.sequence)
        )
        return tuple(_event_from_row(row) for row in rows)

    async def lock_task(self, task_id: UUID) -> InvestigationTaskV1 | None:
        row = await self._session.scalar(
            select(InvestigationTaskRow)
            .where(InvestigationTaskRow.id == task_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return _task_from_row(row) if row is not None else None

    async def add(self, task: InvestigationTaskV1) -> InvestigationTaskV1:
        existing = await self.get_by_task_key(task.investigation_id, task.task_key)
        if existing is not None:
            if existing != task:
                raise VersionConflictError("Task key already materialised a different task")
            return existing
        result = await self._session.execute(
            insert(InvestigationTaskRow)
            .values(**_task_values(task))
            .on_conflict_do_nothing()
            .returning(InvestigationTaskRow.id)
        )
        if result.scalar_one_or_none() is None:
            existing = await self.get_by_task_key(task.investigation_id, task.task_key)
            if existing is None:
                raise VersionConflictError("Task id is already used")
            if existing != task:
                raise VersionConflictError("Task key already materialised a different task")
            return existing
        return task

    async def add_dependency(
        self,
        *,
        investigation_id: UUID,
        task_id: UUID,
        depends_on_task_id: UUID,
    ) -> None:
        if task_id == depends_on_task_id:
            raise ValueError("A task cannot depend on itself")
        await self._session.execute(
            insert(InvestigationTaskDependencyRow)
            .values(
                task_id=task_id,
                depends_on_task_id=depends_on_task_id,
                investigation_id=investigation_id,
            )
            .on_conflict_do_nothing()
        )

    async def update_status(
        self,
        task: InvestigationTaskV1,
        *,
        expected_status_version: int,
        expected_status: TaskStatus,
    ) -> InvestigationTaskV1 | None:
        """Compare-and-swap the mutable state; never writes ``event_sequence``."""
        if task.status_version != expected_status_version + 1:
            raise ValueError("A status change increments status_version exactly once")
        if task.status is expected_status:
            raise ValueError("A status change must change the status")
        result = await self._session.execute(
            update(InvestigationTaskRow)
            .where(
                InvestigationTaskRow.id == task.id,
                InvestigationTaskRow.status_version == expected_status_version,
                InvestigationTaskRow.status == expected_status.value,
            )
            .values(
                status=task.status.value,
                status_version=task.status_version,
                result_verification_status=task.result_verification_status.value,
                blocked_reason=(task.blocked_reason.value if task.blocked_reason else None),
                assigned_actor_kind=(
                    task.assigned_actor_kind.value if task.assigned_actor_kind else None
                ),
                assigned_actor_id=task.assigned_actor_id,
                lease_id=task.lease_id,
                lease_session_id=task.lease_session_id,
                lease_fence=task.lease_fence,
                lease_expires_at=task.lease_expires_at,
                heartbeat_at=task.heartbeat_at,
                result_refs=[thaw_json(item) for item in task.result_refs],
                updated_at=task.updated_at,
            )
            .returning(InvestigationTaskRow)
            .execution_options(populate_existing=True, synchronize_session=False)
        )
        row = result.scalar_one_or_none()
        return _task_from_row(row) if row is not None else None

    async def acquire_lease(
        self,
        task_id: UUID,
        *,
        expected_status_version: int,
        lease_id: UUID,
        lease_session_id: UUID,
        now: datetime,
        ttl_seconds: int,
    ) -> InvestigationTaskV1 | None:
        """Assigned -> running for an operator, bumping the fence atomically."""
        if ttl_seconds < 1:
            raise ValueError("ttl_seconds must be >= 1")
        result = await self._session.execute(
            update(InvestigationTaskRow)
            .where(
                InvestigationTaskRow.id == task_id,
                InvestigationTaskRow.status_version == expected_status_version,
                InvestigationTaskRow.status == TaskStatus.ASSIGNED.value,
            )
            .values(
                status=TaskStatus.RUNNING.value,
                status_version=InvestigationTaskRow.status_version + 1,
                lease_id=lease_id,
                lease_session_id=lease_session_id,
                lease_fence=InvestigationTaskRow.lease_fence + 1,
                lease_expires_at=now + timedelta(seconds=ttl_seconds),
                heartbeat_at=now,
                updated_at=now,
            )
            .returning(InvestigationTaskRow)
            .execution_options(populate_existing=True, synchronize_session=False)
        )
        row = result.scalar_one_or_none()
        return _task_from_row(row) if row is not None else None

    async def renew_lease(
        self,
        task_id: UUID,
        *,
        expected_status_version: int,
        expected_lease_fence: int,
        ttl_seconds: int,
    ) -> InvestigationTaskV1 | None:
        """Conditional heartbeat for ``running``/``awaiting_input`` (D3 M1).

        Every time value comes from ``statement_timestamp()``: the expiry
        predicate, ``heartbeat_at``, ``lease_expires_at`` and ``updated_at``.
        No caller clock participates, so a late heartbeat cannot resurrect a
        lease PostgreSQL already considers expired and a skewed application
        clock cannot resurrect one either.
        """
        if ttl_seconds < 1:
            raise ValueError("ttl_seconds must be >= 1")
        database_now = func.statement_timestamp()
        result = await self._session.execute(
            update(InvestigationTaskRow)
            .where(
                InvestigationTaskRow.id == task_id,
                InvestigationTaskRow.status.in_(
                    (TaskStatus.RUNNING.value, TaskStatus.AWAITING_INPUT.value)
                ),
                InvestigationTaskRow.status_version == expected_status_version,
                InvestigationTaskRow.lease_fence == expected_lease_fence,
                InvestigationTaskRow.lease_expires_at > database_now,
            )
            .values(
                heartbeat_at=database_now,
                lease_expires_at=database_now + timedelta(seconds=ttl_seconds),
                updated_at=database_now,
            )
            .returning(InvestigationTaskRow)
            .execution_options(populate_existing=True, synchronize_session=False)
        )
        row = result.scalar_one_or_none()
        return _task_from_row(row) if row is not None else None

    async def database_now(self) -> datetime:
        """The database clock shared by the lease predicates (D3 M1)."""
        value = await self._session.scalar(select(func.statement_timestamp()))
        if not isinstance(value, datetime):
            raise RuntimeError("PostgreSQL did not return a statement timestamp")
        return value

    async def advance_event_sequence(self, task_id: UUID) -> int:
        """The only writer of ``event_sequence`` (D3 M4)."""
        sequence = await self._session.scalar(
            update(InvestigationTaskRow)
            .where(InvestigationTaskRow.id == task_id)
            .values(event_sequence=InvestigationTaskRow.event_sequence + 1)
            .returning(InvestigationTaskRow.event_sequence)
        )
        if sequence is None:
            raise VersionConflictError("Task disappeared before event append")
        return int(sequence)

    async def add_event(self, event: TaskEvent) -> TaskEvent:
        result = await self._session.execute(
            insert(InvestigationTaskEventRow)
            .values(**_event_values(event))
            .on_conflict_do_nothing()
            .returning(InvestigationTaskEventRow.id)
        )
        if result.scalar_one_or_none() is None:
            raise VersionConflictError("Task event sequence or idempotency key is already used")
        return event
