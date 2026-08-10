from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from cti_app.application.jobs import (
    DemoJobParameters,
    DuplicateJobError,
    JobExecutionContext,
    JobExecutor,
    JobHandlerError,
    JobParameters,
    JobRegistry,
    JobService,
    SynchronousJobDispatcher,
    create_job_registry,
)
from cti_app.domain.jobs import Job, JobStatus
from tests.job_support import InMemoryJobUnitOfWorkFactory


async def test_submission_is_idempotent() -> None:
    factory = InMemoryJobUnitOfWorkFactory()
    service = JobService(factory, create_job_registry())
    aggregate_id = uuid4()
    first = await service.submit(
        kind="demo.deterministic",
        aggregate_type="subject",
        aggregate_id=aggregate_id,
        idempotency_key="same-operation",
        correlation_id="test",
        input_parameters={"steps": 2},
    )

    with pytest.raises(DuplicateJobError) as duplicate:
        await service.submit(
            kind="demo.deterministic",
            aggregate_type="subject",
            aggregate_id=aggregate_id,
            idempotency_key="same-operation",
            correlation_id="test",
            input_parameters={"steps": 2},
        )
    assert duplicate.value.existing_job_id == first.id
    assert len(factory.state) == 1


async def test_synchronous_dispatcher_retries_only_transient_errors() -> None:
    factory = InMemoryJobUnitOfWorkFactory()
    registry = JobRegistry()
    calls = 0

    async def flaky_handler(parameters: JobParameters, context: JobExecutionContext) -> str:
        del parameters, context
        nonlocal calls
        calls += 1
        if calls < 3:
            raise JobHandlerError(
                "temporary_unavailable", "Service temporairement indisponible", transient=True
            )
        return "memory://result"

    registry.register("test.flaky", DemoJobParameters, flaky_handler)
    service = JobService(factory, registry)
    executor = JobExecutor(factory, registry, retry_base_seconds=1, retry_max_seconds=10)
    dispatcher = SynchronousJobDispatcher(executor)
    job = await service.submit(
        kind="test.flaky",
        aggregate_type="subject",
        aggregate_id=uuid4(),
        idempotency_key="flaky",
        correlation_id="test",
        input_parameters={"steps": 1},
        max_attempts=3,
    )

    await dispatcher.dispatch(job.id)
    completed = await service.get(job.id)

    assert completed.status is JobStatus.SUCCEEDED
    assert completed.attempt == 3
    assert calls == 3
    await dispatcher.dispatch(job.id)
    assert calls == 3


async def test_permanent_and_unexpected_errors_are_not_retried_or_leaked() -> None:
    factory = InMemoryJobUnitOfWorkFactory()
    registry = JobRegistry()

    async def unsafe_handler(parameters: JobParameters, context: JobExecutionContext) -> None:
        del parameters, context
        raise RuntimeError("customer-private-marker-must-not-leak")

    registry.register("test.unsafe", DemoJobParameters, unsafe_handler)
    service = JobService(factory, registry)
    dispatcher = SynchronousJobDispatcher(JobExecutor(factory, registry))
    job = await service.submit(
        kind="test.unsafe",
        aggregate_type="subject",
        aggregate_id=uuid4(),
        idempotency_key="unsafe",
        correlation_id="test",
        input_parameters={"steps": 1},
    )

    await dispatcher.dispatch(job.id)
    failed = await service.get(job.id)

    assert failed.status is JobStatus.FAILED
    assert failed.attempt == 1
    assert failed.error_code == "internal_error"
    assert "customer-private-marker-must-not-leak" not in (failed.error_message or "")

    history = await service.history(job.id)
    assert [event.event_type for event in history] == [
        "job.submitted",
        "job.started",
        "job.failed",
    ]
    assert all(
        "customer-private-marker-must-not-leak" not in str(event.payload) for event in history
    )
    metrics = await service.metrics()
    assert metrics.total == 1
    assert metrics.counts_by_status[JobStatus.FAILED] == 1
    assert metrics.failure_rate == 1.0


async def test_abandoned_running_job_is_requeued() -> None:
    factory = InMemoryJobUnitOfWorkFactory()
    service = JobService(factory, create_job_registry())
    stale = datetime.now(UTC) - timedelta(minutes=10)
    job = Job(
        kind="demo.deterministic",
        aggregate_type="subject",
        aggregate_id=uuid4(),
        idempotency_key="abandoned",
        correlation_id="test",
        input_parameters={"steps": 1, "label": "test"},
    )
    job.start(stale)
    factory.state[job.id] = job

    recovered = await service.recover_abandoned(timedelta(minutes=1))

    assert [item.id for item in recovered] == [job.id]
    assert (await service.get(job.id)).status is JobStatus.QUEUED
    assert (await service.get(job.id)).error_code == "heartbeat_expired"
