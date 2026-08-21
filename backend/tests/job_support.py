from __future__ import annotations

from collections.abc import Sequence
from copy import deepcopy
from datetime import datetime
from types import TracebackType
from uuid import UUID

from cti_app.application.persistence import JobEventRepository, JobRepository
from cti_app.domain.jobs import Job, JobEvent, JobOperationalMetrics, JobStatus


class InMemoryJobRepository:
    def __init__(self, jobs: dict[UUID, Job]) -> None:
        self._jobs = jobs

    async def add_if_absent(self, job: Job) -> bool:
        if any(item.idempotency_key == job.idempotency_key for item in self._jobs.values()):
            return False
        self._jobs[job.id] = deepcopy(job)
        return True

    async def get(self, job_id: UUID) -> Job | None:
        job = self._jobs.get(job_id)
        return deepcopy(job) if job else None

    async def get_for_update(self, job_id: UUID) -> Job | None:
        return await self.get(job_id)

    async def get_by_idempotency_key(self, idempotency_key: str) -> Job | None:
        for job in self._jobs.values():
            if job.idempotency_key == idempotency_key:
                return deepcopy(job)
        return None

    async def save(self, job: Job) -> None:
        if job.id not in self._jobs:
            raise LookupError(job.id)
        self._jobs[job.id] = deepcopy(job)

    async def list_abandoned(self, heartbeat_before: datetime) -> Sequence[Job]:
        return [
            deepcopy(job)
            for job in self._jobs.values()
            if job.status is JobStatus.RUNNING
            and job.heartbeat_at is not None
            and job.heartbeat_at < heartbeat_before
        ]

    async def list_for_aggregate(
        self, aggregate_type: str, aggregate_id: UUID, *, kind: str | None = None
    ) -> Sequence[Job]:
        return [
            deepcopy(job)
            for job in sorted(
                self._jobs.values(), key=lambda item: item.created_at, reverse=True
            )
            if job.aggregate_type == aggregate_type
            and job.aggregate_id == aggregate_id
            and (kind is None or job.kind == kind)
        ]

    async def operational_metrics(self) -> JobOperationalMetrics:
        counts = {
            status: sum(job.status is status for job in self._jobs.values()) for status in JobStatus
        }
        total = len(self._jobs)
        terminal = sum(
            counts[status]
            for status in (JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED)
        )
        return JobOperationalMetrics(
            total=total,
            counts_by_status=counts,
            retry_waiting=sum(
                job.status is JobStatus.QUEUED and job.next_retry_at is not None
                for job in self._jobs.values()
            ),
            average_duration_seconds=None,
            failure_rate=counts[JobStatus.FAILED] / terminal if terminal else 0.0,
        )


class InMemoryJobEventRepository:
    def __init__(self, events: list[JobEvent]) -> None:
        self._events = events

    async def append(self, event: JobEvent) -> None:
        self._events.append(deepcopy(event))

    async def list_for_job(self, job_id: UUID) -> Sequence[JobEvent]:
        return [deepcopy(event) for event in self._events if event.job_id == job_id]


class InMemoryJobUnitOfWork:
    jobs: JobRepository
    job_events: JobEventRepository

    def __init__(self, state: dict[UUID, Job], events: list[JobEvent]) -> None:
        self.jobs = InMemoryJobRepository(state)
        self.job_events = InMemoryJobEventRepository(events)

    async def __aenter__(self) -> InMemoryJobUnitOfWork:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None

    async def commit(self) -> None:
        return None

    async def rollback(self) -> None:
        return None


class InMemoryJobUnitOfWorkFactory:
    def __init__(self) -> None:
        self.state: dict[UUID, Job] = {}
        self.events: list[JobEvent] = []

    def __call__(self) -> InMemoryJobUnitOfWork:
        return InMemoryJobUnitOfWork(self.state, self.events)
