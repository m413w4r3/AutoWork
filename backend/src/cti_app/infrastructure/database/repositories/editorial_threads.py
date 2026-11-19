from __future__ import annotations

from datetime import UTC, datetime
from typing import cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from cti_app.domain.editorial_thread import (
    EditorialBridgeStatus,
    EditorialThread,
    EditorialThreadActiveJobConflict,
    EditorialThreadBinding,
    EditorialThreadError,
    EditorialThreadEvent,
    EditorialThreadEventKind,
    EditorialThreadJob,
    EditorialThreadJobState,
    EditorialThreadLockTimeout,
    EditorialThreadRecord,
)
from cti_app.infrastructure.database.models.core import SubjectRow
from cti_app.infrastructure.database.models.editorial_threads import (
    EditorialThreadEventRow,
    EditorialThreadJobRow,
    EditorialThreadRow,
)
from cti_app.infrastructure.database.models.model_execution import (
    ModelConversationRow,
    ModelConversationTurnRow,
)

_ACTIVE_THREAD_JOB_INDEX = "uq_editorial_thread_jobs_active_thread"
_POSTGRES_BUSY_SQLSTATES = frozenset({"55P03", "40P01"})


class SqlAlchemyEditorialThreadRepository:
    # Every operation that locks both rows takes the thread row before the job row.
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def _lock_thread_for_job(self, job_id: UUID) -> EditorialThreadRow | None:
        thread_id = await self._session.scalar(
            select(EditorialThreadJobRow.thread_id).where(EditorialThreadJobRow.id == job_id)
        )
        if thread_id is None:
            return None
        return cast(
            EditorialThreadRow | None,
            await self._session.scalar(
                select(EditorialThreadRow)
                .where(EditorialThreadRow.id == thread_id)
                .with_for_update()
            ),
        )

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

    async def enqueue_message(
        self,
        *,
        job_id: UUID,
        thread_id: UUID,
        actor_id: UUID,
        idempotency_key: str,
        request_sha256: str,
        payload_blob_ref: UUID,
        payload_sha256: str,
    ) -> tuple[EditorialThreadEvent, EditorialThreadJob, bool]:
        try:
            return await self._enqueue_message(
                job_id=job_id,
                thread_id=thread_id,
                actor_id=actor_id,
                idempotency_key=idempotency_key,
                request_sha256=request_sha256,
                payload_blob_ref=payload_blob_ref,
                payload_sha256=payload_sha256,
            )
        except (IntegrityError, DBAPIError) as error:
            translated = translate_enqueue_database_error(error)
            if translated is not None:
                raise translated from error
            raise

    async def _enqueue_message(
        self,
        *,
        job_id: UUID,
        thread_id: UUID,
        actor_id: UUID,
        idempotency_key: str,
        request_sha256: str,
        payload_blob_ref: UUID,
        payload_sha256: str,
    ) -> tuple[EditorialThreadEvent, EditorialThreadJob, bool]:
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
        if existing_row is not None:
            event = _event_from_row(existing_row)
            if event.request_sha256 != request_sha256:
                raise EditorialThreadError("idempotency_conflict")
            job_row = await self._session.scalar(
                select(EditorialThreadJobRow).where(EditorialThreadJobRow.send_event_id == event.id)
            )
            if job_row is None:
                raise EditorialThreadError("editorial_thread_job_missing")
            return event, _job_from_row(job_row), True

        active = await self._session.scalar(
            select(EditorialThreadJobRow)
            .where(
                EditorialThreadJobRow.thread_id == thread_id,
                EditorialThreadJobRow.state.in_(
                    (
                        EditorialThreadJobState.QUEUED.value,
                        EditorialThreadJobState.WAITING_FOR_CAPACITY.value,
                        EditorialThreadJobState.RUNNING.value,
                        EditorialThreadJobState.RECONCILIATION_REQUIRED.value,
                    )
                ),
            )
            .with_for_update()
        )
        if active is not None:
            if active.state == EditorialThreadJobState.RECONCILIATION_REQUIRED.value:
                raise EditorialThreadError("reconciliation_required")
            raise EditorialThreadError("editorial_thread_busy")

        thread = _thread_from_row(row)
        updated, event = thread.append_event(
            kind=EditorialThreadEventKind.USER_MESSAGE,
            actor_id=actor_id,
            idempotency_key=idempotency_key,
            request_sha256=request_sha256,
            payload_blob_ref=payload_blob_ref,
            payload_sha256=payload_sha256,
        )
        event_row = EditorialThreadEventRow(
            id=event.id,
            thread_id=event.thread_id,
            sequence=event.sequence,
            event_kind=event.kind.value,
            actor_id=event.actor_id,
            idempotency_key=event.idempotency_key,
            request_sha256=event.request_sha256,
            payload_blob_ref=event.payload_blob_ref,
            payload_sha256=event.payload_sha256,
            created_at=event.created_at,
        )
        job_row = EditorialThreadJobRow(
            id=job_id,
            thread_id=thread_id,
            send_event_id=event.id,
            state=EditorialThreadJobState.QUEUED.value,
            attempt_count=0,
        )
        self._session.add_all((event_row, job_row))
        row.next_sequence = updated.next_sequence
        row.version = updated.version
        row.generation_state = EditorialBridgeStatus.GENERATING.value
        row.updated_at = updated.updated_at
        await self._session.flush()
        return event, _job_from_row(job_row), False

    async def get_job(self, job_id: UUID) -> EditorialThreadJob | None:
        row = await self._session.get(EditorialThreadJobRow, job_id)
        return _job_from_row(row) if row is not None else None

    async def get_job_for_event(self, event_id: UUID) -> EditorialThreadJob | None:
        row = await self._session.scalar(
            select(EditorialThreadJobRow).where(EditorialThreadJobRow.send_event_id == event_id)
        )
        return _job_from_row(row) if row is not None else None

    async def latest_job_for_thread(self, thread_id: UUID) -> EditorialThreadJob | None:
        row = await self._session.scalar(
            select(EditorialThreadJobRow)
            .where(EditorialThreadJobRow.thread_id == thread_id)
            .order_by(EditorialThreadJobRow.created_at.desc())
            .limit(1)
        )
        return _job_from_row(row) if row is not None else None

    async def claim_job(
        self, job_id: UUID, *, lease_token: UUID, lease_until: datetime, now: datetime
    ) -> EditorialThreadJob | None:
        if await self._lock_thread_for_job(job_id) is None:
            return None
        row = await self._session.scalar(
            select(EditorialThreadJobRow)
            .where(EditorialThreadJobRow.id == job_id)
            .with_for_update(skip_locked=True)
        )
        if row is None:
            return None
        state = EditorialThreadJobState(row.state)
        runnable = state in {
            EditorialThreadJobState.QUEUED,
        } or (
            state
            in {
                EditorialThreadJobState.WAITING_FOR_CAPACITY,
                EditorialThreadJobState.RUNNING,
            }
            and row.lease_until is not None
            and row.lease_until <= now
        )
        if state is EditorialThreadJobState.WAITING_FOR_CAPACITY and row.lease_until is None:
            runnable = True
        if not runnable:
            return None
        row.state = EditorialThreadJobState.RUNNING.value
        row.lease_token = lease_token
        row.lease_until = lease_until
        row.attempt_count += 1
        row.updated_at = now
        await self._session.flush()
        return _job_from_row(row)

    async def set_job_state(
        self,
        job_id: UUID,
        state: EditorialThreadJobState,
        *,
        lease_token: UUID | None = None,
        lease_until: datetime | None = None,
    ) -> None:
        if await self._lock_thread_for_job(job_id) is None:
            raise EditorialThreadError("editorial_thread_job_not_found")
        row = await self._session.scalar(
            select(EditorialThreadJobRow)
            .where(EditorialThreadJobRow.id == job_id)
            .with_for_update()
        )
        if row is None:
            raise EditorialThreadError("editorial_thread_job_not_found")
        if lease_token is not None and row.lease_token != lease_token:
            raise EditorialThreadError("editorial_thread_job_lease_lost")
        row.state = state.value
        row.lease_token = None
        row.lease_until = lease_until
        row.updated_at = datetime.now(UTC)
        await self._session.flush()

    async def set_bridge_status(self, thread_id: UUID, status: EditorialBridgeStatus) -> None:
        row = await self._session.scalar(
            select(EditorialThreadRow).where(EditorialThreadRow.id == thread_id).with_for_update()
        )
        if row is None:
            raise EditorialThreadError("thread_not_found")
        row.generation_state = status.value
        row.version += 1
        row.updated_at = datetime.now(UTC)
        await self._session.flush()

    async def list_dispatchable_jobs(self, *, now: datetime, limit: int = 100) -> list[UUID]:
        rows = await self._session.scalars(
            select(EditorialThreadJobRow.id)
            .where(
                (EditorialThreadJobRow.state.in_((EditorialThreadJobState.QUEUED.value,)))
                | (
                    (
                        EditorialThreadJobRow.state
                        == EditorialThreadJobState.WAITING_FOR_CAPACITY.value
                    )
                    & (
                        EditorialThreadJobRow.lease_until.is_(None)
                        | (EditorialThreadJobRow.lease_until <= now)
                    )
                )
                | (
                    (EditorialThreadJobRow.state == EditorialThreadJobState.RUNNING.value)
                    & (EditorialThreadJobRow.lease_until <= now)
                )
            )
            .order_by(EditorialThreadJobRow.created_at)
            .limit(limit)
        )
        return list(rows)

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


def _job_from_row(row: EditorialThreadJobRow) -> EditorialThreadJob:
    return EditorialThreadJob(
        id=row.id,
        thread_id=row.thread_id,
        send_event_id=row.send_event_id,
        state=EditorialThreadJobState(row.state),
        lease_until=row.lease_until,
        lease_token=row.lease_token,
        attempt_count=row.attempt_count,
    )


def translate_enqueue_database_error(error: Exception) -> EditorialThreadError | None:
    """Translate only known active-job races and postgres lock failures."""
    if isinstance(error, IntegrityError):
        original = error.orig
        diagnostic = getattr(original, "diag", None)
        constraint_name = getattr(original, "constraint_name", None) or getattr(
            diagnostic, "constraint_name", None
        )
        if constraint_name == _ACTIVE_THREAD_JOB_INDEX:
            return EditorialThreadActiveJobConflict()
    if isinstance(error, DBAPIError):
        original = error.orig
        sqlstate = getattr(original, "sqlstate", None) or getattr(original, "pgcode", None)
        if sqlstate in _POSTGRES_BUSY_SQLSTATES:
            return EditorialThreadLockTimeout()
    return None
