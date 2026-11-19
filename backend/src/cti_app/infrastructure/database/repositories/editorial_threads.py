from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from cti_app.domain.editorial_thread import (
    EditorialBridgeStatus,
    EditorialThread,
    EditorialThreadBinding,
    EditorialThreadError,
    EditorialThreadEvent,
    EditorialThreadEventKind,
    EditorialThreadRecord,
)
from cti_app.infrastructure.database.models.core import SubjectRow
from cti_app.infrastructure.database.models.editorial_threads import (
    EditorialThreadEventRow,
    EditorialThreadRow,
)
from cti_app.infrastructure.database.models.model_execution import (
    ModelConversationRow,
    ModelConversationTurnRow,
)


class SqlAlchemyEditorialThreadRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_current_by_occurrence(
        self, subject_id: UUID, production_run_id: UUID, occurrence_id: UUID
    ) -> EditorialThreadRecord | None:
        row = await self._session.scalar(
            select(EditorialThreadRow)
            .where(
                EditorialThreadRow.subject_id == subject_id,
                EditorialThreadRow.production_run_id == production_run_id,
                EditorialThreadRow.occurrence_id == occurrence_id,
            )
            .order_by(EditorialThreadRow.session_number.desc())
            .limit(1)
            .with_for_update(read=True)
        )
        return await self._record(row) if row is not None else None

    async def get(self, thread_id: UUID) -> EditorialThreadRecord | None:
        row = await self._session.get(EditorialThreadRow, thread_id)
        return await self._record(row) if row is not None else None

    async def create_if_absent(self, thread: EditorialThread) -> EditorialThread:
        origin = thread.origin
        await self._session.scalar(
            select(SubjectRow.id).where(SubjectRow.id == origin.subject_id).with_for_update()
        )
        existing = await self._session.scalar(
            select(EditorialThreadRow).where(
                EditorialThreadRow.subject_id == origin.subject_id,
                EditorialThreadRow.occurrence_id == origin.occurrence_id,
                EditorialThreadRow.session_number == thread.session_number,
            )
        )
        if existing is not None:
            if (
                existing.id == thread.id
                and existing.production_run_id == origin.production_run_id
                and existing.origin_artifact_id == origin.artifact_id
                and existing.origin_artifact_version == origin.artifact_version
                and existing.origin_canonical_sha256 == origin.canonical_sha256
            ):
                return _thread_from_row(existing)
            raise EditorialThreadError("editorial_thread_session_conflict")

        latest = await self._session.scalar(
            select(EditorialThreadRow)
            .where(
                EditorialThreadRow.subject_id == origin.subject_id,
                EditorialThreadRow.occurrence_id == origin.occurrence_id,
            )
            .order_by(EditorialThreadRow.session_number.desc())
            .limit(1)
            .with_for_update()
        )
        if thread.session_number == 1:
            if latest is not None:
                raise EditorialThreadError("editorial_thread_session_conflict")
        elif (
            latest is None
            or latest.session_number + 1 != thread.session_number
            or latest.generation_state != EditorialBridgeStatus.SESSION_LOST.value
            or latest.subject_id != origin.subject_id
            or latest.production_run_id != origin.production_run_id
            or latest.occurrence_id != origin.occurrence_id
        ):
            raise EditorialThreadError("editorial_thread_session_not_lost")
        statement = (
            insert(EditorialThreadRow)
            .values(
                id=thread.id,
                subject_id=origin.subject_id,
                production_run_id=origin.production_run_id,
                occurrence_id=origin.occurrence_id,
                session_number=thread.session_number,
                origin_artifact_id=origin.artifact_id,
                origin_artifact_version=origin.artifact_version,
                origin_canonical_sha256=origin.canonical_sha256,
                model_conversation_id=thread.model_conversation_id,
                generation_state=thread.bridge_status.value,
                next_sequence=thread.next_sequence,
                version=thread.version,
                created_at=thread.created_at,
                updated_at=thread.updated_at,
            )
            .on_conflict_do_nothing(constraint="uq_editorial_threads_subject_occurrence_session")
        )
        await self._session.execute(statement)
        row = await self._session.scalar(
            select(EditorialThreadRow).where(
                EditorialThreadRow.subject_id == origin.subject_id,
                EditorialThreadRow.occurrence_id == origin.occurrence_id,
                EditorialThreadRow.session_number == thread.session_number,
            )
        )
        if row is None:
            raise EditorialThreadError("editorial_thread_create_failed")
        return _thread_from_row(row)

    async def append_event(
        self,
        *,
        thread_id: UUID,
        kind: EditorialThreadEventKind,
        actor_id: UUID | None,
        idempotency_key: str,
        request_sha256: str,
        payload_blob_ref: UUID | None = None,
        payload_sha256: str | None = None,
        proposal_id: UUID | None = None,
        model_turn_id: UUID | None = None,
    ) -> EditorialThreadEvent:
        row = await self._session.scalar(
            select(EditorialThreadRow).where(EditorialThreadRow.id == thread_id).with_for_update()
        )
        if row is None:
            raise EditorialThreadError("thread_not_found")
        existing_row = await self._session.scalar(
            select(EditorialThreadEventRow).where(
                EditorialThreadEventRow.thread_id == thread_id,
                EditorialThreadEventRow.idempotency_key == idempotency_key,
            )
        )
        thread = _thread_from_row(row)
        existing = _event_from_row(existing_row) if existing_row is not None else None
        if model_turn_id is not None:
            turn_binding = await self._session.execute(
                select(
                    ModelConversationTurnRow.conversation_id,
                    ModelConversationRow.subject_id,
                )
                .join(
                    ModelConversationRow,
                    ModelConversationRow.id == ModelConversationTurnRow.conversation_id,
                )
                .where(ModelConversationTurnRow.id == model_turn_id)
            )
            turn_owner = turn_binding.one_or_none()
            if (
                turn_owner is None
                or thread.model_conversation_id is None
                or turn_owner[0] != thread.model_conversation_id
                or turn_owner[1] != thread.origin.subject_id
            ):
                raise EditorialThreadError("model_turn_thread_mismatch")
        updated, event = thread.append_event(
            kind=kind,
            actor_id=actor_id,
            idempotency_key=idempotency_key,
            request_sha256=request_sha256,
            payload_blob_ref=payload_blob_ref,
            payload_sha256=payload_sha256,
            proposal_id=proposal_id,
            model_turn_id=model_turn_id,
            existing_event=existing,
        )
        if existing is not None:
            return existing

        self._session.add(
            EditorialThreadEventRow(
                id=event.id,
                thread_id=event.thread_id,
                sequence=event.sequence,
                event_kind=event.kind.value,
                actor_id=event.actor_id,
                idempotency_key=event.idempotency_key,
                request_sha256=event.request_sha256,
                payload_blob_ref=event.payload_blob_ref,
                payload_sha256=event.payload_sha256,
                proposal_id=event.proposal_id,
                model_turn_id=event.model_turn_id,
                created_at=event.created_at,
            )
        )
        row.next_sequence = updated.next_sequence
        row.version = updated.version
        row.updated_at = updated.updated_at
        await self._session.flush()
        return event

    async def _record(self, row: EditorialThreadRow) -> EditorialThreadRecord:
        events = await self._session.scalars(
            select(EditorialThreadEventRow)
            .where(EditorialThreadEventRow.thread_id == row.id)
            .order_by(EditorialThreadEventRow.sequence)
        )
        return EditorialThreadRecord(
            thread=_thread_from_row(row),
            events=tuple(_event_from_row(event) for event in events),
        )


def _thread_from_row(row: EditorialThreadRow) -> EditorialThread:
    return EditorialThread(
        id=row.id,
        origin=EditorialThreadBinding(
            subject_id=row.subject_id,
            production_run_id=row.production_run_id,
            occurrence_id=row.occurrence_id,
            artifact_id=row.origin_artifact_id,
            artifact_version=row.origin_artifact_version,
            canonical_sha256=row.origin_canonical_sha256,
        ),
        model_conversation_id=row.model_conversation_id,
        bridge_status=EditorialBridgeStatus(row.generation_state),
        next_sequence=row.next_sequence,
        version=row.version,
        session_number=row.session_number,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _event_from_row(row: EditorialThreadEventRow) -> EditorialThreadEvent:
    return EditorialThreadEvent(
        id=row.id,
        thread_id=row.thread_id,
        sequence=row.sequence,
        kind=EditorialThreadEventKind(row.event_kind),
        actor_id=row.actor_id,
        idempotency_key=row.idempotency_key,
        request_sha256=row.request_sha256,
        payload_blob_ref=row.payload_blob_ref,
        payload_sha256=row.payload_sha256,
        proposal_id=row.proposal_id,
        model_turn_id=row.model_turn_id,
        created_at=row.created_at or datetime.now(UTC),
    )
