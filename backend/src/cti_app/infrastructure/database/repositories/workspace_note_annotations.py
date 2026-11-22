"""SQLAlchemy repository for append-only human note annotations (M24-WS2)."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from cti_app.application.investigation_persistence import (
    VersionConflictError,
    same_workspace_note_annotation_request,
)
from cti_app.domain.workspace_note_annotation import (
    NoteAnnotationActorKind,
    NoteAnnotationKind,
    NoteIocType,
    WorkspaceNoteAnnotationV1,
)
from cti_app.infrastructure.database.models.workspace_note_annotations import (
    WorkspaceNoteAnnotationRow,
)


def _from_row(row: WorkspaceNoteAnnotationRow) -> WorkspaceNoteAnnotationV1:
    return WorkspaceNoteAnnotationV1(
        id=row.id,
        investigation_id=row.investigation_id,
        workspace_id=row.workspace_id,
        revision_id=row.revision_id,
        file_relpath=row.file_relpath,
        kind=NoteAnnotationKind(row.kind),
        actor_kind=NoteAnnotationActorKind(row.actor_kind),
        actor_id=row.actor_id,
        target_task_id=row.target_task_id,
        ioc_value=row.ioc_value,
        ioc_type=NoteIocType(row.ioc_type) if row.ioc_type is not None else None,
        target_sample_id=row.target_sample_id,
        detail=row.detail,
        idempotency_key=row.idempotency_key,
        created_at=row.created_at,
    )


def _values(annotation: WorkspaceNoteAnnotationV1) -> dict[str, object]:
    return {
        "id": annotation.id,
        "investigation_id": annotation.investigation_id,
        "workspace_id": annotation.workspace_id,
        "revision_id": annotation.revision_id,
        "file_relpath": annotation.file_relpath,
        "kind": annotation.kind.value,
        "actor_kind": annotation.actor_kind.value,
        "actor_id": annotation.actor_id,
        "target_task_id": annotation.target_task_id,
        "ioc_value": annotation.ioc_value,
        "ioc_type": annotation.ioc_type.value if annotation.ioc_type is not None else None,
        "target_sample_id": annotation.target_sample_id,
        "detail": annotation.detail,
        "idempotency_key": annotation.idempotency_key,
        "created_at": annotation.created_at,
    }


class SqlAlchemyWorkspaceNoteAnnotationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, annotation_id: UUID) -> WorkspaceNoteAnnotationV1 | None:
        row = await self._session.get(WorkspaceNoteAnnotationRow, annotation_id)
        return _from_row(row) if row is not None else None

    async def get_by_idempotency(
        self, investigation_id: UUID, idempotency_key: str
    ) -> WorkspaceNoteAnnotationV1 | None:
        row = await self._session.scalar(
            select(WorkspaceNoteAnnotationRow).where(
                WorkspaceNoteAnnotationRow.investigation_id == investigation_id,
                WorkspaceNoteAnnotationRow.idempotency_key == idempotency_key,
            )
        )
        return _from_row(row) if row is not None else None

    async def list_for_path(
        self, investigation_id: UUID, file_relpath: str
    ) -> tuple[WorkspaceNoteAnnotationV1, ...]:
        rows = await self._session.scalars(
            select(WorkspaceNoteAnnotationRow)
            .where(
                WorkspaceNoteAnnotationRow.investigation_id == investigation_id,
                WorkspaceNoteAnnotationRow.file_relpath == file_relpath,
            )
            .order_by(
                WorkspaceNoteAnnotationRow.created_at,
                WorkspaceNoteAnnotationRow.id,
            )
        )
        return tuple(_from_row(row) for row in rows)

    async def list_for_revision(
        self, workspace_id: str, revision_id: str
    ) -> tuple[WorkspaceNoteAnnotationV1, ...]:
        rows = await self._session.scalars(
            select(WorkspaceNoteAnnotationRow)
            .where(
                WorkspaceNoteAnnotationRow.workspace_id == workspace_id,
                WorkspaceNoteAnnotationRow.revision_id == revision_id,
            )
            .order_by(
                WorkspaceNoteAnnotationRow.created_at,
                WorkspaceNoteAnnotationRow.id,
            )
        )
        return tuple(_from_row(row) for row in rows)

    async def append_if_absent(
        self, annotation: WorkspaceNoteAnnotationV1
    ) -> tuple[WorkspaceNoteAnnotationV1, bool]:
        result = await self._session.execute(
            insert(WorkspaceNoteAnnotationRow)
            .values(**_values(annotation))
            .on_conflict_do_nothing()
        )
        created = int(getattr(result, "rowcount", 0) or 0) == 1
        stored = await self.get(annotation.id)
        if created:
            if stored is None or stored != annotation:
                raise RuntimeError("note annotation insert did not produce its row")
            return stored, True
        replay = await self.get_by_idempotency(
            annotation.investigation_id, annotation.idempotency_key
        )
        if replay is not None:
            if not same_workspace_note_annotation_request(replay, annotation):
                raise VersionConflictError(
                    "Note annotation idempotency key records a different action"
                )
            return replay, False
        raise VersionConflictError("note annotation conflicts with a different row")
