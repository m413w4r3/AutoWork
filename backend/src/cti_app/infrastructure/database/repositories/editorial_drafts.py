from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from cti_app.domain.editorial_draft import EditorialDraft
from cti_app.infrastructure.database.models.editorial_drafts import EditorialDraftRow


class SqlAlchemyEditorialDraftRepository:
    """PostgreSQL adapter for mutable, occurrence-scoped server drafts."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def lock_for_key(self, subject_id: UUID, occurrence_id: UUID) -> None:
        lock_key = f"editorial-draft:{subject_id}:{occurrence_id}"
        await self._session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
            {"lock_key": lock_key},
        )

    async def get_for_update(self, subject_id: UUID, occurrence_id: UUID) -> EditorialDraft | None:
        row = await self._session.scalar(
            select(EditorialDraftRow)
            .where(
                EditorialDraftRow.subject_id == subject_id,
                EditorialDraftRow.occurrence_id == occurrence_id,
            )
            .with_for_update()
        )
        return _draft_from_row(row) if row is not None else None

    async def save(self, draft: EditorialDraft) -> EditorialDraft:
        row = await self._session.get(EditorialDraftRow, draft.id)
        values = _row_values(draft)
        if row is None:
            row = EditorialDraftRow(**values)
            self._session.add(row)
        else:
            for name, value in values.items():
                if name != "id":
                    setattr(row, name, value)
        await self._session.flush()
        return _draft_from_row(row)

    async def delete(self, draft_id: UUID) -> EditorialDraft | None:
        row = await self._session.get(EditorialDraftRow, draft_id)
        if row is None:
            return None
        draft = _draft_from_row(row)
        await self._session.delete(row)
        await self._session.flush()
        return draft

    async def delete_for_occurrence(
        self, subject_id: UUID, occurrence_id: UUID
    ) -> Sequence[EditorialDraft]:
        row = await self._session.scalar(
            select(EditorialDraftRow)
            .where(
                EditorialDraftRow.subject_id == subject_id,
                EditorialDraftRow.occurrence_id == occurrence_id,
            )
            .with_for_update()
        )
        if row is None:
            return ()
        draft = _draft_from_row(row)
        await self._session.delete(row)
        await self._session.flush()
        return (draft,)

    async def purge_expired(self, now: datetime, *, limit: int) -> int:
        rows = await self._session.scalars(
            select(EditorialDraftRow)
            .where(EditorialDraftRow.expires_at <= now)
            .order_by(EditorialDraftRow.expires_at, EditorialDraftRow.id)
            .with_for_update(skip_locked=True)
            .limit(limit)
        )
        expired = tuple(rows)
        for row in expired:
            await self._session.delete(row)
        if expired:
            await self._session.flush()
        return len(expired)


def _draft_from_row(row: EditorialDraftRow) -> EditorialDraft:
    return EditorialDraft(
        id=row.id,
        subject_id=row.subject_id,
        production_run_id=row.production_run_id,
        occurrence_id=row.occurrence_id,
        base_snapshot_id=row.base_snapshot_id,
        base_artifact_id=row.base_artifact_id,
        base_artifact_version=row.base_artifact_version,
        base_canonical_sha256=row.base_canonical_sha256,
        actor_id=row.actor_id,
        text_blob_ref=row.text_blob_ref,
        text_sha256=row.text_sha256,
        version=row.version,
        created_at=row.created_at,
        updated_at=row.updated_at,
        expires_at=row.expires_at,
    )


def _row_values(draft: EditorialDraft) -> dict[str, object]:
    return {
        "id": draft.id,
        "subject_id": draft.subject_id,
        "production_run_id": draft.production_run_id,
        "occurrence_id": draft.occurrence_id,
        "base_snapshot_id": draft.base_snapshot_id,
        "base_artifact_id": draft.base_artifact_id,
        "base_artifact_version": draft.base_artifact_version,
        "base_canonical_sha256": draft.base_canonical_sha256,
        "actor_id": draft.actor_id,
        "text_blob_ref": draft.text_blob_ref,
        "text_sha256": draft.text_sha256,
        "version": draft.version,
        "created_at": draft.created_at,
        "updated_at": draft.updated_at,
        "expires_at": draft.expires_at,
    }
