"""Repositories for the Jobs bounded context (R10).

This module owns the SqlAlchemy repositories for Job and JobEvent aggregates,
along with their exclusive row/domain mappers and helper functions.
"""

from collections.abc import Sequence
from datetime import datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from cti_app.domain.jobs import Job, JobEvent, JobOperationalMetrics, JobStatus
from cti_app.infrastructure.database.models.schema import JobEventRow, JobRow


class SqlAlchemyJobRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add_if_absent(self, job: Job) -> bool:
        statement = (
            insert(JobRow)
            .values(**_job_values(job))
            .on_conflict_do_nothing(index_elements=[JobRow.idempotency_key])
            .returning(JobRow.id)
        )
        inserted_id = await self._session.scalar(statement)
        return inserted_id is not None

    async def get(self, job_id: UUID) -> Job | None:
        row = await self._session.get(JobRow, job_id)
        return _job_from_row(row) if row else None

    async def get_for_update(self, job_id: UUID) -> Job | None:
        row = await self._session.scalar(
            select(JobRow).where(JobRow.id == job_id).with_for_update()
        )
        return _job_from_row(row) if row else None

    async def get_by_idempotency_key(self, idempotency_key: str) -> Job | None:
        row = await self._session.scalar(
            select(JobRow).where(JobRow.idempotency_key == idempotency_key)
        )
        return _job_from_row(row) if row else None

    async def save(self, job: Job) -> None:
        row = await self._session.get(JobRow, job.id)
        if row is None:
            raise LookupError(f"Job {job.id} does not exist")
        for field_name, value in _job_values(job).items():
            setattr(row, field_name, value)
        await self._session.flush()

    async def list_abandoned(self, heartbeat_before: datetime) -> Sequence[Job]:
        rows = await self._session.scalars(
            select(JobRow)
            .where(
                JobRow.status == JobStatus.RUNNING.value,
                JobRow.heartbeat_at < heartbeat_before,
            )
            .order_by(JobRow.heartbeat_at, JobRow.id)
            .with_for_update(skip_locked=True)
        )
        return [_job_from_row(row) for row in rows]

    async def list_for_aggregate(
        self, aggregate_type: str, aggregate_id: UUID, *, kind: str | None = None
    ) -> Sequence[Job]:
        statement = (
            select(JobRow)
            .where(
                JobRow.aggregate_type == aggregate_type,
                JobRow.aggregate_id == aggregate_id,
            )
            .order_by(JobRow.created_at.desc())
        )
        if kind is not None:
            statement = statement.where(JobRow.kind == kind)
        rows = await self._session.scalars(statement)
        return [_job_from_row(row) for row in rows]

    async def operational_metrics(self) -> JobOperationalMetrics:
        status_rows = await self._session.execute(
            select(JobRow.status, func.count()).group_by(JobRow.status)
        )
        counts = {status: 0 for status in JobStatus}
        counts.update({JobStatus(status): int(count) for status, count in status_rows})
        total = sum(counts.values())
        retry_waiting = int(
            await self._session.scalar(
                select(func.count())
                .select_from(JobRow)
                .where(
                    JobRow.status == JobStatus.QUEUED.value,
                    JobRow.next_retry_at.is_not(None),
                )
            )
            or 0
        )
        average_duration = await self._session.scalar(
            select(func.avg(func.extract("epoch", JobRow.finished_at - JobRow.started_at))).where(
                JobRow.started_at.is_not(None), JobRow.finished_at.is_not(None)
            )
        )
        failed = counts.get(JobStatus.FAILED, 0)
        terminal = sum(
            counts.get(status, 0)
            for status in (JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED)
        )
        return JobOperationalMetrics(
            total=total,
            counts_by_status=counts,
            retry_waiting=retry_waiting,
            average_duration_seconds=float(average_duration) if average_duration else None,
            failure_rate=failed / terminal if terminal else 0.0,
        )


class SqlAlchemyJobEventRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def append(self, event: JobEvent) -> None:
        self._session.add(
            JobEventRow(
                id=event.id,
                job_id=event.job_id,
                event_type=event.event_type,
                from_status=event.from_status.value if event.from_status else None,
                to_status=event.to_status.value,
                actor_id=event.actor_id,
                correlation_id=event.correlation_id,
                payload=event.payload,
                occurred_at=event.occurred_at,
            )
        )
        await self._session.flush()

    async def list_for_job(self, job_id: UUID) -> Sequence[JobEvent]:
        rows = await self._session.scalars(
            select(JobEventRow)
            .where(JobEventRow.job_id == job_id)
            .order_by(JobEventRow.occurred_at, JobEventRow.id)
        )
        return [_job_event_from_row(row) for row in rows]


def _job_values(job: Job) -> dict[str, object]:
    return {
        "id": job.id,
        "kind": job.kind,
        "aggregate_type": job.aggregate_type,
        "aggregate_id": job.aggregate_id,
        "status": job.status.value,
        "progress_current": job.progress_current,
        "progress_total": job.progress_total,
        "user_message": job.user_message,
        "idempotency_key": job.idempotency_key,
        "attempt": job.attempt,
        "max_attempts": job.max_attempts,
        "next_retry_at": job.next_retry_at,
        "started_at": job.started_at,
        "finished_at": job.finished_at,
        "heartbeat_at": job.heartbeat_at,
        "error_code": job.error_code,
        "error_message": job.error_message,
        "error_details": job.error_details,
        "correlation_id": job.correlation_id,
        "input_parameters": job.input_parameters,
        "output_reference": job.output_reference,
        "cancellation_requested_at": job.cancellation_requested_at,
        "created_at": job.created_at,
        "updated_at": job.updated_at,
    }


def _job_from_row(row: JobRow) -> Job:
    return Job(
        id=row.id,
        kind=row.kind,
        aggregate_type=row.aggregate_type,
        aggregate_id=row.aggregate_id,
        status=JobStatus(row.status),
        progress_current=row.progress_current,
        progress_total=row.progress_total,
        user_message=row.user_message,
        idempotency_key=row.idempotency_key,
        attempt=row.attempt,
        max_attempts=row.max_attempts,
        next_retry_at=row.next_retry_at,
        started_at=row.started_at,
        finished_at=row.finished_at,
        heartbeat_at=row.heartbeat_at,
        error_code=row.error_code,
        error_message=row.error_message,
        error_details=row.error_details,
        correlation_id=row.correlation_id,
        input_parameters=row.input_parameters,
        output_reference=row.output_reference,
        cancellation_requested_at=row.cancellation_requested_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _job_event_from_row(row: JobEventRow) -> JobEvent:
    return JobEvent(
        id=row.id,
        job_id=row.job_id,
        event_type=row.event_type,
        from_status=JobStatus(row.from_status) if row.from_status else None,
        to_status=JobStatus(row.to_status),
        actor_id=row.actor_id,
        correlation_id=row.correlation_id,
        payload=row.payload,
        occurred_at=row.occurred_at,
    )
