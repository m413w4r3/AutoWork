"""SQLAlchemy repository for immutable investigation handoffs (M24-05A).

Migration 0022 installs append-only triggers, so this module only inserts and
reads.  Version allocation and idempotent replay are decided by
:class:`~cti_app.application.investigation_handoffs.InvestigationHandoffService`
while it holds the task row lock; the unique constraints here are the
last-resort race guard.
"""

from __future__ import annotations

import hashlib
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from cti_app.application.investigation_persistence import (
    HandoffRecord,
    VersionConflictError,
)
from cti_app.domain.investigation import canonical_json_bytes
from cti_app.domain.investigation_handoff import (
    HandoffActor,
    HandoffActorKind,
    InvestigationHandoffV1,
)
from cti_app.infrastructure.database.models.investigation_handoffs import (
    InvestigationHandoffRow,
)


def _handoff_from_row(row: InvestigationHandoffRow) -> HandoffRecord:
    handoff = InvestigationHandoffV1.model_validate(row.content)
    sha256 = hashlib.sha256(canonical_json_bytes(handoff.to_payload())).hexdigest()
    if sha256 != row.content_sha256 or handoff.content_sha256 != row.content_sha256:
        raise VersionConflictError("Stored handoff content does not match its SHA-256")
    if (
        handoff.handoff_id != str(row.id)
        or handoff.investigation_id != str(row.investigation_id)
        or handoff.task_id != str(row.task_id)
        or handoff.version != row.version
    ):
        raise VersionConflictError("Stored handoff identity disagrees with its row")
    return HandoffRecord(
        handoff=handoff,
        idempotency_key=row.idempotency_key,
        request_sha256=row.request_sha256,
        created_by=HandoffActor(
            kind=HandoffActorKind(row.created_by_actor_kind),
            actor_id=row.created_by_actor_id,
        ),
        predecessor_id=str(row.predecessor_id) if row.predecessor_id is not None else None,
    )


def _record_values(record: HandoffRecord) -> dict[str, Any]:
    handoff = record.handoff
    return {
        "id": UUID(handoff.handoff_id),
        "investigation_id": UUID(handoff.investigation_id),
        "task_id": UUID(handoff.task_id),
        "version": handoff.version,
        "schema_version": handoff.schema_version,
        "content": handoff.to_payload(),
        "content_sha256": handoff.content_sha256,
        "request_sha256": record.request_sha256,
        "from_actor_kind": handoff.from_actor.kind.value,
        "from_actor_id": handoff.from_actor.actor_id,
        "to_actor_kind": handoff.to_actor.kind.value,
        "to_actor_id": handoff.to_actor.actor_id,
        "predecessor_id": (
            UUID(record.predecessor_id) if record.predecessor_id is not None else None
        ),
        "created_by_actor_kind": record.created_by.kind.value,
        "created_by_actor_id": record.created_by.actor_id,
        "idempotency_key": record.idempotency_key,
        "created_at": handoff.created_at,
    }


class SqlAlchemyInvestigationHandoffRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, handoff_id: UUID) -> HandoffRecord | None:
        row = await self._session.get(InvestigationHandoffRow, handoff_id)
        return _handoff_from_row(row) if row is not None else None

    async def get_by_idempotency(self, task_id: UUID, idempotency_key: str) -> HandoffRecord | None:
        row = await self._session.scalar(
            select(InvestigationHandoffRow).where(
                InvestigationHandoffRow.task_id == task_id,
                InvestigationHandoffRow.idempotency_key == idempotency_key,
            )
        )
        return _handoff_from_row(row) if row is not None else None

    async def get_by_version(self, task_id: UUID, version: int) -> HandoffRecord | None:
        row = await self._session.scalar(
            select(InvestigationHandoffRow).where(
                InvestigationHandoffRow.task_id == task_id,
                InvestigationHandoffRow.version == version,
            )
        )
        return _handoff_from_row(row) if row is not None else None

    async def latest_for_task(self, task_id: UUID) -> HandoffRecord | None:
        row = await self._session.scalar(
            select(InvestigationHandoffRow)
            .where(InvestigationHandoffRow.task_id == task_id)
            .order_by(InvestigationHandoffRow.version.desc())
            .limit(1)
        )
        return _handoff_from_row(row) if row is not None else None

    async def list_for_investigation(
        self, investigation_id: UUID, *, task_id: UUID | None = None
    ) -> tuple[HandoffRecord, ...]:
        statement = select(InvestigationHandoffRow).where(
            InvestigationHandoffRow.investigation_id == investigation_id
        )
        if task_id is not None:
            statement = statement.where(InvestigationHandoffRow.task_id == task_id)
        rows = await self._session.scalars(
            statement.order_by(
                InvestigationHandoffRow.created_at,
                InvestigationHandoffRow.version,
                InvestigationHandoffRow.id,
            )
        )
        return tuple(_handoff_from_row(row) for row in rows)

    async def append_if_absent(self, record: HandoffRecord) -> tuple[HandoffRecord, bool]:
        result = await self._session.execute(
            insert(InvestigationHandoffRow)
            .values(**_record_values(record))
            .on_conflict_do_nothing()
            .returning(InvestigationHandoffRow.id)
        )
        if result.scalar_one_or_none() is not None:
            return record, True
        existing = await self.get_by_idempotency(
            UUID(record.handoff.task_id), record.idempotency_key
        )
        if existing is not None and existing == record:
            return existing, False
        raise VersionConflictError("Handoff task version or idempotency key is already used")
