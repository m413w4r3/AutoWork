"""SQLAlchemy repositories for the canonical workspace binding and ingest rows."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from cti_app.application.workspace_agent import (
    WorkspaceBindingV1,
    WorkspaceFileEventV1,
    WorkspaceFileRevisionV1,
    WorkspaceSyncCursorV1,
)
from cti_app.infrastructure.database.models.workspace import (
    WorkspaceBindingRow,
    WorkspaceFileEventRow,
    WorkspaceFileRevisionRow,
    WorkspaceSyncCursorRow,
)


def _binding_from_row(row: WorkspaceBindingRow) -> WorkspaceBindingV1:
    return WorkspaceBindingV1(
        workspace_id=row.workspace_id,
        investigation_id=row.investigation_id,
        subject_id=row.subject_id,
        delivery_code=row.delivery_code,
        article_slug=row.article_slug,
        synthesis_sha256=row.synthesis_sha256,
        created_at=row.created_at,
    )


def _revision_from_row(row: WorkspaceFileRevisionRow) -> WorkspaceFileRevisionV1:
    return WorkspaceFileRevisionV1(
        workspace_id=row.workspace_id,
        revision_id=row.revision_id,
        path=row.path,
        sha256=row.sha256,
        size=row.size,
        mode=row.mode,
        mtime_ns=row.mtime_ns,
        observed_at=row.observed_at,
        origin=row.origin,
        task_id=row.task_id,
        supersedes_revision_id=row.supersedes_revision_id,
    )


def _event_from_row(row: WorkspaceFileEventRow) -> WorkspaceFileEventV1:
    return WorkspaceFileEventV1(
        workspace_id=row.workspace_id,
        event_id=row.event_id,
        seq=row.seq,
        generation=row.generation,
        kind=row.kind,
        path=row.path,
        previous_path=row.previous_path,
        revision_id=row.revision_id,
        previous_revision_id=row.previous_revision_id,
        sha256=row.sha256,
        size=row.size,
        origin=row.origin,
        task_id=row.task_id,
        observed_at=row.observed_at,
        policy_class=row.policy_class,
        import_status=row.import_status,
        imported_at=row.imported_at,
    )


def _cursor_from_row(row: WorkspaceSyncCursorRow) -> WorkspaceSyncCursorV1:
    return WorkspaceSyncCursorV1(
        workspace_id=row.workspace_id,
        generation=row.generation,
        cursor=row.cursor,
        last_scan_at=row.last_scan_at,
    )


class SqlAlchemyWorkspaceBindingRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, workspace_id: str) -> WorkspaceBindingV1 | None:
        row = await self._session.get(WorkspaceBindingRow, workspace_id)
        return _binding_from_row(row) if row is not None else None

    async def get_for_investigation(self, investigation_id: UUID) -> WorkspaceBindingV1 | None:
        row = await self._session.scalar(
            select(WorkspaceBindingRow).where(
                WorkspaceBindingRow.investigation_id == investigation_id
            )
        )
        return _binding_from_row(row) if row is not None else None

    async def add_if_absent(self, binding: WorkspaceBindingV1) -> tuple[WorkspaceBindingV1, bool]:
        existing_for_investigation = await self.get_for_investigation(binding.investigation_id)
        if existing_for_investigation is not None:
            if existing_for_investigation != binding:
                raise ValueError("An investigation can bind only one workspace identity")
            return existing_for_investigation, False
        statement = (
            insert(WorkspaceBindingRow)
            .values(
                workspace_id=binding.workspace_id,
                investigation_id=binding.investigation_id,
                subject_id=binding.subject_id,
                delivery_code=binding.delivery_code,
                article_slug=binding.article_slug,
                synthesis_sha256=binding.synthesis_sha256,
                created_at=binding.created_at,
            )
            .on_conflict_do_nothing()
        )
        result = await self._session.execute(statement)
        created = int(getattr(result, "rowcount", 0) or 0) == 1
        stored = await self.get(binding.workspace_id)
        if stored is None:
            stored = await self.get_for_investigation(binding.investigation_id)
        if stored is None:
            raise RuntimeError("workspace binding insert did not produce a row")
        if stored != binding:
            raise ValueError("A workspace id cannot identify a different investigation binding")
        return stored, created


class SqlAlchemyWorkspaceFileRevisionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, workspace_id: str, revision_id: str) -> WorkspaceFileRevisionV1 | None:
        row = await self._session.get(WorkspaceFileRevisionRow, (workspace_id, revision_id))
        return _revision_from_row(row) if row is not None else None

    async def list_for_workspace(self, workspace_id: str) -> tuple[WorkspaceFileRevisionV1, ...]:
        rows = await self._session.scalars(
            select(WorkspaceFileRevisionRow)
            .where(WorkspaceFileRevisionRow.workspace_id == workspace_id)
            .order_by(
                WorkspaceFileRevisionRow.path,
                WorkspaceFileRevisionRow.observed_at,
                WorkspaceFileRevisionRow.revision_id,
            )
        )
        return tuple(_revision_from_row(row) for row in rows)

    async def append_if_absent(
        self, revision: WorkspaceFileRevisionV1
    ) -> tuple[WorkspaceFileRevisionV1, bool]:
        statement = (
            insert(WorkspaceFileRevisionRow)
            .values(
                workspace_id=revision.workspace_id,
                revision_id=revision.revision_id,
                path=revision.path,
                sha256=revision.sha256,
                size=revision.size,
                mode=revision.mode,
                mtime_ns=revision.mtime_ns,
                observed_at=revision.observed_at,
                origin=revision.origin,
                task_id=revision.task_id,
                supersedes_revision_id=revision.supersedes_revision_id,
            )
            .on_conflict_do_nothing(
                index_elements=[
                    WorkspaceFileRevisionRow.workspace_id,
                    WorkspaceFileRevisionRow.revision_id,
                ]
            )
        )
        result = await self._session.execute(statement)
        created = int(getattr(result, "rowcount", 0) or 0) == 1
        stored = await self.get(revision.workspace_id, revision.revision_id)
        if stored is None:
            raise RuntimeError("workspace revision insert did not produce a row")
        if stored != revision:
            raise ValueError("A revision id cannot identify a different workspace file")
        return stored, created


class SqlAlchemyWorkspaceFileEventRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, workspace_id: str, event_id: str) -> WorkspaceFileEventV1 | None:
        row = await self._session.get(WorkspaceFileEventRow, (workspace_id, event_id))
        return _event_from_row(row) if row is not None else None

    async def list_for_workspace(self, workspace_id: str) -> tuple[WorkspaceFileEventV1, ...]:
        rows = await self._session.scalars(
            select(WorkspaceFileEventRow)
            .where(WorkspaceFileEventRow.workspace_id == workspace_id)
            .order_by(
                WorkspaceFileEventRow.generation,
                WorkspaceFileEventRow.seq,
                WorkspaceFileEventRow.event_id,
            )
        )
        return tuple(_event_from_row(row) for row in rows)

    async def append_if_absent(
        self, event: WorkspaceFileEventV1
    ) -> tuple[WorkspaceFileEventV1, bool]:
        statement = (
            insert(WorkspaceFileEventRow)
            .values(
                workspace_id=event.workspace_id,
                event_id=event.event_id,
                seq=event.seq,
                generation=event.generation,
                kind=event.kind,
                path=event.path,
                previous_path=event.previous_path,
                revision_id=event.revision_id,
                previous_revision_id=event.previous_revision_id,
                sha256=event.sha256,
                size=event.size,
                origin=event.origin,
                task_id=event.task_id,
                observed_at=event.observed_at,
                policy_class=event.policy_class,
                import_status=event.import_status,
                imported_at=event.imported_at,
            )
            .on_conflict_do_nothing()
        )
        result = await self._session.execute(statement)
        created = int(getattr(result, "rowcount", 0) or 0) == 1
        stored = await self.get(event.workspace_id, event.event_id)
        if stored is None:
            raise ValueError("An event sequence cannot identify a different event id")
        if stored != event:
            raise ValueError("An event id cannot identify different event content")
        return stored, created


class SqlAlchemyWorkspaceSyncCursorRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, workspace_id: str) -> WorkspaceSyncCursorV1 | None:
        row = await self._session.get(WorkspaceSyncCursorRow, workspace_id)
        return _cursor_from_row(row) if row is not None else None

    async def save(self, cursor: WorkspaceSyncCursorV1) -> WorkspaceSyncCursorV1:
        statement = insert(WorkspaceSyncCursorRow).values(
            workspace_id=cursor.workspace_id,
            generation=cursor.generation,
            cursor=cursor.cursor,
            last_scan_at=cursor.last_scan_at,
        )
        excluded = statement.excluded
        statement = statement.on_conflict_do_update(
            index_elements=[WorkspaceSyncCursorRow.workspace_id],
            set_={
                "generation": excluded.generation,
                "cursor": excluded.cursor,
                "last_scan_at": excluded.last_scan_at,
            },
            where=or_(
                WorkspaceSyncCursorRow.generation != excluded.generation,
                excluded.cursor > WorkspaceSyncCursorRow.cursor,
            ),
        )
        await self._session.execute(statement)
        stored = await self.get(cursor.workspace_id)
        if stored is None:
            raise RuntimeError("workspace cursor insert did not produce a row")
        return stored
