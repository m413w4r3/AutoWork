import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from cti_app.application.diagnostics import DiagnosticsLog
from cti_app.application.discovery.jobs import DISCOVERY_JOB_KIND
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
from cti_app.application.production_jobs import stage_job_kind
from cti_app.domain.jobs import Job, JobStatus
from cti_app.domain.production import SubjectProductionStage
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


async def test_synchronous_dispatcher_retries_transient_bridge_timeout() -> None:
    factory = InMemoryJobUnitOfWorkFactory()
    registry = JobRegistry()
    calls = 0

    async def flaky_handler(parameters: JobParameters, context: JobExecutionContext) -> str:
        del parameters, context
        nonlocal calls
        calls += 1
        if calls < 3:
            raise JobHandlerError("bridge_timeout", "Le bridge a expiré.", transient=True)
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


async def test_a_crashed_job_leaves_its_traceback_in_the_diagnostics_trail(
    tmp_path: Path,
) -> None:
    """The public failure is deliberately vague, so the trail is the only lead.

    A user reporting "internal_error" plus a diagnostic id must be enough for us
    to find the stack that produced it, without the container logs.
    """
    factory = InMemoryJobUnitOfWorkFactory()
    registry = JobRegistry()

    async def unsafe_handler(parameters: JobParameters, context: JobExecutionContext) -> None:
        del parameters, context
        raise RuntimeError("boom-inside-the-chain")

    registry.register("test.unsafe", DemoJobParameters, unsafe_handler)
    service = JobService(factory, registry)
    diagnostics = DiagnosticsLog.from_env(tmp_path)
    dispatcher = SynchronousJobDispatcher(JobExecutor(factory, registry, diagnostics=diagnostics))
    job = await service.submit(
        kind="test.unsafe",
        aggregate_type="subject",
        aggregate_id=uuid4(),
        idempotency_key="crash",
        correlation_id="corr-crash",
        input_parameters={"steps": 1},
    )

    await dispatcher.dispatch(job.id)

    events = [
        json.loads(line)
        for line in (tmp_path / "events.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    crash = next(event for event in events if event["event"] == "job.crashed")
    assert crash["correlation_id"] == "corr-crash"
    assert crash["stage"] == "test.unsafe"
    assert crash["error_code"] == "internal_error"
    assert crash["error_type"] == "RuntimeError"
    traceback_text = (tmp_path / crash["payload_file"]).read_text(encoding="utf-8")
    assert "boom-inside-the-chain" in traceback_text
    assert "unsafe_handler" in traceback_text


async def test_a_controlled_job_failure_is_recorded_with_its_code(tmp_path: Path) -> None:
    factory = InMemoryJobUnitOfWorkFactory()
    registry = JobRegistry()

    async def refusing_handler(parameters: JobParameters, context: JobExecutionContext) -> None:
        del parameters, context
        raise JobHandlerError(
            "bridge_unavailable", "Le pont ChatGPT est injoignable.", transient=False
        )

    registry.register("test.refusing", DemoJobParameters, refusing_handler)
    service = JobService(factory, registry)
    diagnostics = DiagnosticsLog.from_env(tmp_path)
    dispatcher = SynchronousJobDispatcher(JobExecutor(factory, registry, diagnostics=diagnostics))
    job = await service.submit(
        kind="test.refusing",
        aggregate_type="subject",
        aggregate_id=uuid4(),
        idempotency_key="refused",
        correlation_id="corr-refused",
        input_parameters={"steps": 1},
    )

    await dispatcher.dispatch(job.id)

    events = [
        json.loads(line)
        for line in (tmp_path / "events.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    failure = next(event for event in events if event["event"] == "job.failed")
    assert failure["error_code"] == "bridge_unavailable"
    assert failure["job_kind"] == "test.refusing"
    assert failure["transient"] is False


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


async def test_long_handler_renews_job_lease() -> None:
    factory = InMemoryJobUnitOfWorkFactory()
    registry = JobRegistry()
    observed_renewal = False
    job_id = None

    async def slow_handler(parameters: JobParameters, context: JobExecutionContext) -> str:
        del parameters, context
        nonlocal observed_renewal
        assert job_id is not None
        initial = factory.state[job_id].heartbeat_at
        await asyncio.sleep(0.04)
        observed_renewal = factory.state[job_id].heartbeat_at != initial
        return "memory://slow-result"

    registry.register("test.slow", DemoJobParameters, slow_handler)
    service = JobService(factory, registry)
    executor = JobExecutor(factory, registry, heartbeat_interval_seconds=0.01)
    job = await service.submit(
        kind="test.slow",
        aggregate_type="subject",
        aggregate_id=uuid4(),
        idempotency_key="slow-heartbeat",
        correlation_id="heartbeat-test",
        input_parameters={"steps": 1},
    )
    job_id = job.id

    completed = await executor.execute(job.id)

    assert completed.status is JobStatus.SUCCEEDED
    assert observed_renewal is True


def _stale_discovery_job() -> Job:
    return Job(
        kind=DISCOVERY_JOB_KIND,
        aggregate_type="edition",
        aggregate_id=uuid4(),
        idempotency_key=f"discovery-{uuid4()}",
        correlation_id="test",
        input_parameters={},
        max_attempts=1,
    )


async def test_durable_kind_recovery_resumes_same_attempt_without_registry_lookup() -> None:
    factory = InMemoryJobUnitOfWorkFactory()
    # Le registry du process de recovery ne connaît pas les jobs métier.
    service = JobService(factory, create_job_registry())
    job = _stale_discovery_job()
    job.start(datetime.now(UTC) - timedelta(minutes=10))
    factory.state[job.id] = job

    recovered = await service.recover_abandoned(
        timedelta(seconds=120),
        resume_current_attempt_kinds=frozenset({DISCOVERY_JOB_KIND}),
    )

    assert [item.id for item in recovered] == [job.id]
    requeued = await service.get(job.id)
    assert requeued.status is JobStatus.QUEUED
    assert requeued.next_retry_at is not None
    assert requeued.attempt == 0
    assert requeued.error_code == "worker_interrupted"


async def test_production_recovery_resumes_the_same_business_attempt() -> None:
    factory = InMemoryJobUnitOfWorkFactory()
    service = JobService(factory, create_job_registry())
    kind = stage_job_kind(SubjectProductionStage.SOURCES)
    job = Job(
        kind=kind,
        aggregate_type="subject",
        aggregate_id=uuid4(),
        idempotency_key=f"production-{uuid4()}",
        correlation_id="test",
        input_parameters={
            "run_id": str(uuid4()),
            "expected_stage": SubjectProductionStage.SOURCES.value,
            "pipeline_generation": 0,
        },
        max_attempts=3,
    )
    job.start(datetime.now(UTC) - timedelta(minutes=10))
    factory.state[job.id] = job

    await service.recover_abandoned(
        timedelta(seconds=120),
        resume_current_attempt_kinds=frozenset({kind}),
    )

    recovered = await service.get(job.id)
    assert recovered.status is JobStatus.QUEUED
    assert recovered.attempt == 0
    assert recovered.error_code == "worker_interrupted"


async def test_durable_kind_recovery_cancels_a_job_whose_worker_died() -> None:
    factory = InMemoryJobUnitOfWorkFactory()
    service = JobService(factory, create_job_registry())
    job = _stale_discovery_job()
    job.start(datetime.now(UTC) - timedelta(minutes=10))
    job.request_cancellation()
    factory.state[job.id] = job

    await service.recover_abandoned(
        timedelta(seconds=120),
        resume_current_attempt_kinds=frozenset({DISCOVERY_JOB_KIND}),
    )

    cancelled = await service.get(job.id)
    assert cancelled.status is JobStatus.CANCELLED
    assert cancelled.user_message == "Tâche annulée"


async def test_recovery_with_declared_kinds_never_queries_an_incomplete_registry() -> None:
    factory = InMemoryJobUnitOfWorkFactory()
    service = JobService(factory, create_job_registry())
    unknown = Job(
        kind="collection.unknown-to-this-registry",
        aggregate_type="subject",
        aggregate_id=uuid4(),
        idempotency_key=f"unknown-{uuid4()}",
        correlation_id="test",
        input_parameters={},
    )
    unknown.start(datetime.now(UTC) - timedelta(minutes=10))
    factory.state[unknown.id] = unknown

    # Sans le paramètre, resumes_after_worker_loss lèverait UnknownJobKindError.
    recovered = await service.recover_abandoned(
        timedelta(seconds=120),
        resume_current_attempt_kinds=frozenset({DISCOVERY_JOB_KIND}),
    )

    assert [item.id for item in recovered] == [unknown.id]
    requeued = await service.get(unknown.id)
    assert requeued.status is JobStatus.QUEUED
    assert requeued.error_code == "heartbeat_expired"
