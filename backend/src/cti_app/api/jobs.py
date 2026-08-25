import asyncio
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from cti_app.application.identity import IdentityProvider
from cti_app.application.jobs import (
    DuplicateJobError,
    JobDispatcher,
    JobNotFoundError,
    JobService,
    UnknownJobKindError,
)
from cti_app.domain.jobs import InvalidJobTransitionError, Job, JobEvent, JobStatus
from cti_app.logging import get_correlation_id

router = APIRouter(prefix="/api/jobs", tags=["jobs"])


class JobSubmission(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: str = Field(min_length=1, max_length=128)
    aggregate_type: str = Field(min_length=1, max_length=64)
    aggregate_id: UUID
    idempotency_key: str = Field(min_length=1, max_length=255)
    input_parameters: dict[str, Any] = Field(default_factory=dict)
    max_attempts: int = Field(default=3, ge=1, le=20)


class JobView(BaseModel):
    id: UUID
    kind: str
    aggregate_type: str
    aggregate_id: UUID
    status: JobStatus
    progress_current: int
    progress_total: int
    user_message: str | None
    idempotency_key: str
    attempt: int
    max_attempts: int
    next_retry_at: datetime | None
    started_at: datetime | None
    finished_at: datetime | None
    heartbeat_at: datetime | None
    error_code: str | None
    error_message: str | None
    error_details: dict[str, Any] | None
    correlation_id: str
    input_parameters: dict[str, Any]
    output_reference: str | None
    cancellation_requested: bool
    created_at: datetime
    updated_at: datetime


class JobEventView(BaseModel):
    id: UUID
    job_id: UUID
    event_type: str
    from_status: JobStatus | None
    to_status: JobStatus
    actor_id: str
    correlation_id: str
    payload: dict[str, Any]
    occurred_at: datetime


class JobMetricsView(BaseModel):
    total: int
    counts_by_status: dict[JobStatus, int]
    retry_waiting: int
    average_duration_seconds: float | None
    failure_rate: float


@router.post("", response_model=JobView, status_code=status.HTTP_202_ACCEPTED)
async def submit_job(payload: JobSubmission, request: Request) -> JobView:
    service, dispatcher = _runtime(request)
    actor_id = await _actor_id(request)
    try:
        job = await service.submit(
            kind=payload.kind,
            aggregate_type=payload.aggregate_type,
            aggregate_id=payload.aggregate_id,
            idempotency_key=payload.idempotency_key,
            correlation_id=get_correlation_id(),
            input_parameters=payload.input_parameters,
            max_attempts=payload.max_attempts,
            actor_id=actor_id,
        )
    except DuplicateJobError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "duplicate_job",
                "existing_job_id": str(exc.existing_job_id),
            },
        ) from exc
    except UnknownJobKindError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "unknown_job_kind", "message": str(exc)},
        ) from exc
    except ValidationError as exc:
        fields = [".".join(str(part) for part in error["loc"]) for error in exc.errors()]
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "invalid_job_parameters", "fields": fields},
        ) from exc
    await dispatcher.dispatch(job.id)
    return _job_view(await service.get(job.id))


@router.get("", response_model=list[JobView])
async def list_jobs(
    request: Request,
    aggregate_type: str = Query(min_length=1, max_length=64),
    aggregate_id: UUID = Query(),
    kind: str | None = Query(default=None, min_length=1, max_length=128),
) -> list[JobView]:
    # Lets the frontend find a background job (e.g. ChatGPT-backed merge reconciliation)
    # that isn't otherwise addressable by id — caller only knows the aggregate, not the job.
    service, _ = _runtime(request)
    jobs = await service.list_for_aggregate(aggregate_type, aggregate_id, kind=kind)
    return [_job_view(job) for job in jobs]


@router.get("/{job_id}", response_model=JobView)
async def get_job(job_id: UUID, request: Request) -> JobView:
    service, _ = _runtime(request)
    return _job_view(await _get_or_404(service, job_id))


@router.get("/metrics/operational", response_model=JobMetricsView)
async def job_metrics(request: Request) -> JobMetricsView:
    service, _ = _runtime(request)
    metrics = await service.metrics()
    return JobMetricsView(
        total=metrics.total,
        counts_by_status=metrics.counts_by_status,
        retry_waiting=metrics.retry_waiting,
        average_duration_seconds=metrics.average_duration_seconds,
        failure_rate=metrics.failure_rate,
    )


@router.get("/{job_id}/history", response_model=list[JobEventView])
async def job_history(job_id: UUID, request: Request) -> list[JobEventView]:
    service, _ = _runtime(request)
    try:
        return [_job_event_view(event) for event in await service.history(job_id)]
    except JobNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Job not found") from exc


@router.post("/{job_id}/retry", response_model=JobView, status_code=status.HTTP_202_ACCEPTED)
async def retry_job(job_id: UUID, request: Request) -> JobView:
    service, dispatcher = _runtime(request)
    try:
        job = await service.retry(job_id, actor_id=await _actor_id(request))
    except JobNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found") from exc
    except InvalidJobTransitionError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "invalid_job_transition", "message": str(exc)},
        ) from exc
    await dispatcher.dispatch(job.id)
    return _job_view(await service.get(job.id))


@router.post("/{job_id}/cancel", response_model=JobView)
async def cancel_job(job_id: UUID, request: Request) -> JobView:
    service, _ = _runtime(request)
    try:
        return _job_view(await service.cancel(job_id, actor_id=await _actor_id(request)))
    except JobNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found") from exc
    except InvalidJobTransitionError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "invalid_job_transition", "message": str(exc)},
        ) from exc


@router.get("/{job_id}/events")
async def job_events(job_id: UUID, request: Request) -> StreamingResponse:
    service, _ = _runtime(request)
    await _get_or_404(service, job_id)

    async def stream() -> AsyncIterator[str]:
        last_version: tuple[datetime, JobStatus] | None = None
        while True:
            if await request.is_disconnected():
                return
            job = await service.get(job_id)
            version = (job.updated_at, job.status)
            if version != last_version:
                payload = _job_view(job).model_dump_json()
                yield f"event: job\ndata: {payload}\n\n"
                last_version = version
            if job.is_terminal:
                return
            yield ": keep-alive\n\n"
            await asyncio.sleep(1)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


def _runtime(request: Request) -> tuple[JobService, JobDispatcher]:
    return request.app.state.job_service, request.app.state.job_dispatcher


async def _actor_id(request: Request) -> str:
    provider: IdentityProvider = request.app.state.identity_provider
    return (await provider.current()).actor_id


async def _get_or_404(service: JobService, job_id: UUID) -> Job:
    try:
        return await service.get(job_id)
    except JobNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found") from exc


def _job_view(job: Job) -> JobView:
    return JobView(
        id=job.id,
        kind=job.kind,
        aggregate_type=job.aggregate_type,
        aggregate_id=job.aggregate_id,
        status=job.status,
        progress_current=job.progress_current,
        progress_total=job.progress_total,
        user_message=job.user_message,
        idempotency_key=job.idempotency_key,
        attempt=job.attempt,
        max_attempts=job.max_attempts,
        next_retry_at=job.next_retry_at,
        started_at=job.started_at,
        finished_at=job.finished_at,
        heartbeat_at=job.heartbeat_at,
        error_code=job.error_code,
        error_message=job.error_message,
        error_details=job.error_details,
        correlation_id=job.correlation_id,
        input_parameters=job.input_parameters,
        output_reference=job.output_reference,
        cancellation_requested=job.cancellation_requested,
        created_at=job.created_at,
        updated_at=job.updated_at,
    )


def _job_event_view(event: JobEvent) -> JobEventView:
    return JobEventView(
        id=event.id,
        job_id=event.job_id,
        event_type=event.event_type,
        from_status=event.from_status,
        to_status=event.to_status,
        actor_id=event.actor_id,
        correlation_id=event.correlation_id,
        payload=event.payload,
        occurred_at=event.occurred_at,
    )
