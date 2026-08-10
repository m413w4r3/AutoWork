from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from cti_app.application.persistence import JobUnitOfWork, JobUnitOfWorkFactory
from cti_app.domain.jobs import Job, JobEvent, JobOperationalMetrics, JobStatus

INTERNAL_ERROR_CODE = "internal_error"
INTERNAL_ERROR_MESSAGE = "Une erreur interne est survenue pendant le traitement."


class JobNotFoundError(LookupError):
    pass


class DuplicateJobError(ValueError):
    def __init__(self, existing_job_id: UUID) -> None:
        super().__init__("A job already exists for this idempotency key")
        self.existing_job_id = existing_job_id


class UnknownJobKindError(ValueError):
    pass


class JobHandlerError(Exception):
    """Controlled handler failure whose public fields are safe to expose."""

    def __init__(self, code: str, public_message: str, *, transient: bool) -> None:
        super().__init__(code)
        self.code = code
        self.public_message = public_message
        self.transient = transient


class JobCancelledError(Exception):
    pass


class JobDispatcher(Protocol):
    async def dispatch(self, job_id: UUID, *, delay_ms: int = 0) -> None: ...


class JobParameters(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class DemoJobParameters(JobParameters):
    steps: int = Field(default=3, ge=1, le=100)
    label: str = Field(default="Démonstration", min_length=1, max_length=128)


@dataclass(frozen=True, slots=True)
class JobDefinition:
    parameter_model: type[JobParameters]
    handler: JobHandler


JobHandler = Callable[[JobParameters, "JobExecutionContext"], Awaitable[str | None]]


class JobRegistry:
    def __init__(self) -> None:
        self._definitions: dict[str, JobDefinition] = {}

    def register(
        self,
        kind: str,
        parameter_model: type[JobParameters],
        handler: JobHandler,
    ) -> None:
        if kind in self._definitions:
            raise ValueError(f"Job kind {kind!r} is already registered")
        self._definitions[kind] = JobDefinition(parameter_model, handler)

    def validate(self, kind: str, parameters: dict[str, Any]) -> JobParameters:
        definition = self._definition(kind)
        return definition.parameter_model.model_validate(parameters)

    def handler(self, kind: str) -> JobHandler:
        return self._definition(kind).handler

    def _definition(self, kind: str) -> JobDefinition:
        try:
            return self._definitions[kind]
        except KeyError as exc:
            raise UnknownJobKindError(f"Unknown job kind: {kind}") from exc


class JobExecutionContext:
    def __init__(self, job_id: UUID, uow_factory: JobUnitOfWorkFactory) -> None:
        self.job_id = job_id
        self._uow_factory = uow_factory

    async def report_progress(self, current: int, total: int, message: str | None = None) -> None:
        async with self._uow_factory() as uow:
            job = await uow.jobs.get_for_update(self.job_id)
            if job is None:
                raise JobNotFoundError(str(self.job_id))
            if job.cancellation_requested:
                previous_status = job.status
                job.mark_cancelled()
                await uow.jobs.save(job)
                await _append_job_event(
                    uow,
                    job,
                    previous_status,
                    "job.cancelled",
                    "system:worker",
                )
                await uow.commit()
                raise JobCancelledError
            job.report_progress(current, total, message)
            await uow.jobs.save(job)
            await uow.commit()

    async def heartbeat(self) -> None:
        async with self._uow_factory() as uow:
            job = await uow.jobs.get_for_update(self.job_id)
            if job is None:
                raise JobNotFoundError(str(self.job_id))
            if job.cancellation_requested:
                previous_status = job.status
                job.mark_cancelled()
                await uow.jobs.save(job)
                await _append_job_event(
                    uow,
                    job,
                    previous_status,
                    "job.cancelled",
                    "system:worker",
                )
                await uow.commit()
                raise JobCancelledError
            job.report_progress(job.progress_current, job.progress_total, job.user_message)
            await uow.jobs.save(job)
            await uow.commit()


class JobService:
    def __init__(self, uow_factory: JobUnitOfWorkFactory, registry: JobRegistry) -> None:
        self._uow_factory = uow_factory
        self._registry = registry

    async def submit(
        self,
        *,
        kind: str,
        aggregate_type: str,
        aggregate_id: UUID,
        idempotency_key: str,
        correlation_id: str,
        input_parameters: dict[str, Any],
        max_attempts: int = 3,
        actor_id: str = "system",
    ) -> Job:
        parameters = self._registry.validate(kind, input_parameters)
        job = Job(
            kind=kind,
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            idempotency_key=idempotency_key,
            correlation_id=correlation_id,
            input_parameters=parameters.model_dump(mode="json"),
            max_attempts=max_attempts,
            user_message="Tâche en attente",
        )
        async with self._uow_factory() as uow:
            inserted = await uow.jobs.add_if_absent(job)
            if not inserted:
                existing = await uow.jobs.get_by_idempotency_key(idempotency_key)
                if existing is None:
                    raise RuntimeError("Idempotency conflict without an existing job")
                raise DuplicateJobError(existing.id)
            await _append_job_event(uow, job, None, "job.submitted", actor_id)
            await uow.commit()
        return job

    async def get(self, job_id: UUID) -> Job:
        async with self._uow_factory() as uow:
            job = await uow.jobs.get(job_id)
            if job is None:
                raise JobNotFoundError(str(job_id))
            return job

    async def cancel(self, job_id: UUID, *, actor_id: str = "system") -> Job:
        async with self._uow_factory() as uow:
            job = await uow.jobs.get_for_update(job_id)
            if job is None:
                raise JobNotFoundError(str(job_id))
            previous_status = job.status
            job.request_cancellation()
            await uow.jobs.save(job)
            await _append_job_event(
                uow, job, previous_status, "job.cancellation_requested", actor_id
            )
            await uow.commit()
            return job

    async def retry(self, job_id: UUID, *, actor_id: str = "system") -> Job:
        async with self._uow_factory() as uow:
            job = await uow.jobs.get_for_update(job_id)
            if job is None:
                raise JobNotFoundError(str(job_id))
            previous_status = job.status
            job.retry_manually()
            await uow.jobs.save(job)
            await _append_job_event(uow, job, previous_status, "job.retry_requested", actor_id)
            await uow.commit()
            return job

    async def recover_abandoned(self, heartbeat_timeout: timedelta) -> list[Job]:
        cutoff = datetime.now(UTC) - heartbeat_timeout
        async with self._uow_factory() as uow:
            abandoned = list(await uow.jobs.list_abandoned(cutoff))
            for job in abandoned:
                previous_status = job.status
                job.recover_abandoned()
                await uow.jobs.save(job)
                await _append_job_event(
                    uow, job, previous_status, "job.heartbeat_recovered", "system:recovery"
                )
            await uow.commit()
            return abandoned

    async def history(self, job_id: UUID) -> list[JobEvent]:
        async with self._uow_factory() as uow:
            if await uow.jobs.get(job_id) is None:
                raise JobNotFoundError(str(job_id))
            return list(await uow.job_events.list_for_job(job_id))

    async def metrics(self) -> JobOperationalMetrics:
        async with self._uow_factory() as uow:
            return await uow.jobs.operational_metrics()


class JobExecutor:
    def __init__(
        self,
        uow_factory: JobUnitOfWorkFactory,
        registry: JobRegistry,
        *,
        retry_base_seconds: float = 1.0,
        retry_max_seconds: float = 300.0,
    ) -> None:
        self._uow_factory = uow_factory
        self._registry = registry
        self._retry_base_seconds = retry_base_seconds
        self._retry_max_seconds = retry_max_seconds

    async def execute(self, job_id: UUID, *, allow_early_retry: bool = False) -> Job:
        job, claimed = await self._start(job_id, allow_early_retry=allow_early_retry)
        if not claimed:
            return job

        context = JobExecutionContext(job_id, self._uow_factory)
        try:
            parameters = self._registry.validate(job.kind, job.input_parameters)
            output_reference = await self._registry.handler(job.kind)(parameters, context)
            return await self._succeed(job_id, output_reference)
        except JobCancelledError:
            return await self._get(job_id)
        except JobHandlerError as exc:
            return await self._handle_controlled_failure(job_id, exc)
        except Exception:
            return await self._fail(job_id, INTERNAL_ERROR_CODE, INTERNAL_ERROR_MESSAGE)

    async def _start(self, job_id: UUID, *, allow_early_retry: bool) -> tuple[Job, bool]:
        async with self._uow_factory() as uow:
            job = await uow.jobs.get_for_update(job_id)
            if job is None:
                raise JobNotFoundError(str(job_id))
            if job.is_terminal or job.status is not JobStatus.QUEUED:
                return job, False
            if (
                job.next_retry_at is not None
                and job.next_retry_at > datetime.now(UTC)
                and not allow_early_retry
            ):
                return job, False
            if job.cancellation_requested:
                previous_status = job.status
                job.mark_cancelled()
                claimed = False
            else:
                previous_status = job.status
                job.start()
                claimed = True
            await uow.jobs.save(job)
            await _append_job_event(
                uow,
                job,
                previous_status,
                "job.cancelled" if not claimed else "job.started",
                "system:worker",
            )
            await uow.commit()
            return job, claimed

    async def _succeed(self, job_id: UUID, output_reference: str | None) -> Job:
        async with self._uow_factory() as uow:
            job = await self._require_job(uow, job_id)
            previous_status = job.status
            if job.cancellation_requested:
                job.mark_cancelled()
            else:
                job.succeed(output_reference)
            await uow.jobs.save(job)
            await _append_job_event(
                uow,
                job,
                previous_status,
                "job.cancelled" if job.status is JobStatus.CANCELLED else "job.succeeded",
                "system:worker",
            )
            await uow.commit()
            return job

    async def _handle_controlled_failure(self, job_id: UUID, error: JobHandlerError) -> Job:
        async with self._uow_factory() as uow:
            job = await self._require_job(uow, job_id)
            previous_status = job.status
            if job.cancellation_requested:
                job.mark_cancelled()
            elif error.transient and job.attempt < job.max_attempts:
                delay_seconds = min(
                    self._retry_max_seconds,
                    self._retry_base_seconds * (2 ** (job.attempt - 1)),
                )
                job.schedule_retry(
                    error.code,
                    _clean_public_message(error.public_message),
                    timedelta(seconds=delay_seconds),
                )
            else:
                job.fail(error.code, _clean_public_message(error.public_message))
            await uow.jobs.save(job)
            event_type = (
                "job.cancelled"
                if job.status is JobStatus.CANCELLED
                else "job.retry_scheduled"
                if job.status is JobStatus.QUEUED
                else "job.failed"
            )
            await _append_job_event(uow, job, previous_status, event_type, "system:worker")
            await uow.commit()
            return job

    async def _fail(self, job_id: UUID, code: str, message: str) -> Job:
        async with self._uow_factory() as uow:
            job = await self._require_job(uow, job_id)
            previous_status = job.status
            if job.cancellation_requested:
                job.mark_cancelled()
            else:
                job.fail(code, message)
            await uow.jobs.save(job)
            await _append_job_event(
                uow,
                job,
                previous_status,
                "job.cancelled" if job.status is JobStatus.CANCELLED else "job.failed",
                "system:worker",
            )
            await uow.commit()
            return job

    async def _get(self, job_id: UUID) -> Job:
        async with self._uow_factory() as uow:
            return await self._require_job(uow, job_id)

    @staticmethod
    async def _require_job(uow: JobUnitOfWork, job_id: UUID) -> Job:
        job = await uow.jobs.get_for_update(job_id)
        if job is None:
            raise JobNotFoundError(str(job_id))
        return job


class SynchronousJobDispatcher:
    """Inline dispatcher used by tests; it never contacts Redis."""

    def __init__(self, executor: JobExecutor) -> None:
        self._executor = executor

    async def dispatch(self, job_id: UUID, *, delay_ms: int = 0) -> None:
        del delay_ms
        while True:
            job = await self._executor.execute(job_id, allow_early_retry=True)
            if job.status is not JobStatus.QUEUED or job.next_retry_at is None:
                return


async def demo_job_handler(parameters: JobParameters, context: JobExecutionContext) -> str:
    if not isinstance(parameters, DemoJobParameters):
        raise TypeError("Invalid demo job parameters")
    for step in range(1, parameters.steps + 1):
        await context.report_progress(
            step,
            parameters.steps,
            f"{parameters.label} : étape {step}/{parameters.steps}",
        )
    return f"demo://completed/{context.job_id}/{parameters.steps}"


def create_job_registry(
    model_gateway: object | None = None,
    discovery_service: object | None = None,
    collection_service: object | None = None,
) -> JobRegistry:
    registry = JobRegistry()
    registry.register("demo.deterministic", DemoJobParameters, demo_job_handler)
    if model_gateway is not None:
        from cti_app.application.model_gateway import ModelGateway
        from cti_app.application.model_jobs import register_model_jobs

        if not isinstance(model_gateway, ModelGateway):
            raise TypeError("model_gateway must be a ModelGateway")
        register_model_jobs(registry, model_gateway)
    if discovery_service is not None:
        from cti_app.application.discovery import DiscoveryService, register_discovery_jobs

        if not isinstance(discovery_service, DiscoveryService):
            raise TypeError("discovery_service must be a DiscoveryService")
        register_discovery_jobs(registry, discovery_service)
    if collection_service is not None:
        from cti_app.application.collection import (
            SubjectCollectionService,
            register_collection_jobs,
        )

        if not isinstance(collection_service, SubjectCollectionService):
            raise TypeError("collection_service must be a SubjectCollectionService")
        register_collection_jobs(registry, collection_service)
    return registry


def _clean_public_message(message: str) -> str:
    cleaned = " ".join(message.replace("\x00", "").split())
    return cleaned[:500] or "Le traitement a échoué."


async def _append_job_event(
    uow: JobUnitOfWork,
    job: Job,
    from_status: JobStatus | None,
    event_type: str,
    actor_id: str,
) -> None:
    await uow.job_events.append(
        JobEvent(
            job_id=job.id,
            event_type=event_type,
            from_status=from_status,
            to_status=job.status,
            actor_id=actor_id,
            correlation_id=job.correlation_id,
            payload={
                "attempt": job.attempt,
                "progress_current": job.progress_current,
                "progress_total": job.progress_total,
            },
        )
    )
