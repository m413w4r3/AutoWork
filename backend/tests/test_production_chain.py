"""Tests for the production execution backbone.

These cover the two links that make the workflow autonomous:
stage N queues stage N+1, and a terminal subject hands the batch to the next
subject — including when it failed, so one bad subject cannot block the queue.
"""

from __future__ import annotations

from typing import Any, cast
from uuid import UUID, uuid4

import pytest

from cti_app.application.edition_workspace import EditionProductionCheckpointService
from cti_app.application.jobs import JobHandlerError, JobRegistry
from cti_app.application.persistence import UnitOfWorkFactory
from cti_app.application.production_jobs import (
    ProductionStageChain,
    ProductionStageParameters,
    register_production_jobs,
    stage_job_kind,
)
from cti_app.application.production_pacing import ProductionPacingPolicy
from cti_app.domain.production import (
    EditionProductionBatch,
    EditionProductionBatchItem,
    SubjectProductionRun,
    SubjectProductionStage,
    SubjectProductionStatus,
)


class _Runs:
    def __init__(self) -> None:
        self.items: dict[UUID, SubjectProductionRun] = {}

    async def add(self, run: SubjectProductionRun) -> None:
        self.items[run.id] = run

    async def get(self, run_id: UUID) -> SubjectProductionRun | None:
        return self.items.get(run_id)

    async def get_for_update(self, run_id: UUID) -> SubjectProductionRun | None:
        return self.items.get(run_id)

    async def save(self, run: SubjectProductionRun) -> None:
        self.items[run.id] = run


class _Batches:
    def __init__(self) -> None:
        self.items: dict[UUID, EditionProductionBatch] = {}

    async def get(self, batch_id: UUID) -> EditionProductionBatch | None:
        return self.items.get(batch_id)

    async def get_for_update(self, batch_id: UUID) -> EditionProductionBatch | None:
        return self.items.get(batch_id)

    async def save(self, batch: EditionProductionBatch) -> None:
        self.items[batch.id] = batch

    async def get_active_for_edition(self, edition_id: UUID) -> EditionProductionBatch | None:
        return next(
            (
                b
                for b in self.items.values()
                if b.edition_id == edition_id and b.status in ("queued", "running")
            ),
            None,
        )

    async def get_latest_for_edition(self, edition_id: UUID) -> EditionProductionBatch | None:
        matches = [b for b in self.items.values() if b.edition_id == edition_id]
        return matches[-1] if matches else None


class _BatchItems:
    def __init__(self) -> None:
        self.items: list[EditionProductionBatchItem] = []

    async def list_for_batch(self, batch_id: UUID) -> list[EditionProductionBatchItem]:
        return [i for i in self.items if i.batch_id == batch_id]

    async def get_by_run(self, run_id: UUID) -> EditionProductionBatchItem | None:
        return next((i for i in self.items if i.production_run_id == run_id), None)


class _Uow:
    def __init__(self) -> None:
        self.subject_production_runs = _Runs()
        self.edition_production_batches = _Batches()
        self.edition_production_batch_items = _BatchItems()
        self.jobs = _ExecutionJobs()

    async def __aenter__(self) -> _Uow:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def commit(self) -> None:
        return None

    async def rollback(self) -> None:
        return None


class _Job:
    def __init__(self, kind: str, idempotency_key: str) -> None:
        self.id = uuid4()
        self.kind = kind
        self.idempotency_key = idempotency_key


class _ExecutionJob:
    def __init__(self, *, attempt: int, max_attempts: int) -> None:
        self.attempt = attempt
        self.max_attempts = max_attempts


class _ExecutionJobs:
    def __init__(self) -> None:
        self.items: dict[UUID, _ExecutionJob] = {}

    async def get(self, job_id: UUID) -> _ExecutionJob | None:
        return self.items.get(job_id)


class _Jobs:
    def __init__(self) -> None:
        self.submitted: list[_Job] = []
        self.submission_options: list[dict[str, Any]] = []

    async def submit(self, *, kind: str, idempotency_key: str, **options: Any) -> _Job:
        job = _Job(kind, idempotency_key)
        self.submitted.append(job)
        self.submission_options.append(options)
        return job


class _Dispatcher:
    def __init__(self) -> None:
        self.dispatched: list[UUID] = []
        self.delays: list[int] = []

    async def dispatch(self, job_id: UUID, *, delay_ms: int = 0) -> None:
        self.dispatched.append(job_id)
        self.delays.append(delay_ms)


class _Context:
    """Minimal JobExecutionContext stand-in."""

    def __init__(self) -> None:
        self.job_id = uuid4()
        self.progress: list[tuple[int, int]] = []

    async def correlation_id(self) -> str:
        return "test-correlation"

    async def report_progress(self, current: int, total: int, message: str | None = None) -> None:
        self.progress.append((current, total))

    async def check_cancelled(self) -> None:
        return None


class _Orchestrator:
    """Stands in for the real stage execution."""

    def __init__(self, result: dict[str, Any]) -> None:
        self.result = result
        self.calls: list[SubjectProductionStage] = []

    async def execute_stage(
        self,
        run_id: UUID,
        expected_stage: SubjectProductionStage,
        context: object | None = None,
        correlation_id: str = "-",
    ) -> dict[str, Any]:
        self.calls.append(expected_stage)
        return self.result


class _Checkpoint:
    def __init__(self) -> None:
        self.calls: list[UUID] = []

    async def checkpoint(self, run_id: UUID, *, correlation_id: str) -> None:
        self.calls.append(run_id)


@pytest.fixture
def uow() -> _Uow:
    return _Uow()


def _build(
    uow: _Uow,
    monkeypatch: pytest.MonkeyPatch,
    result: dict[str, Any],
    checkpoint: object | None = None,
) -> tuple[JobRegistry, _Jobs, _Orchestrator]:
    orchestrator = _Orchestrator(result)
    monkeypatch.setattr(
        "cti_app.application.production_jobs.ProductionWorkflowOrchestrator",
        lambda *a, **k: orchestrator,
    )
    registry = JobRegistry()
    jobs = _Jobs()
    chain = ProductionStageChain()
    chain.bind(jobs, _Dispatcher())  # type: ignore[arg-type]
    register_production_jobs(
        registry,
        cast(UnitOfWorkFactory, lambda: uow),
        chain=chain,
        checkpoint=cast(EditionProductionCheckpointService | None, checkpoint),
    )
    return registry, jobs, orchestrator


def _run(uow: _Uow, stage: SubjectProductionStage) -> SubjectProductionRun:
    run = SubjectProductionRun(
        subject_id=uuid4(),
        edition_id=uuid4(),
    )
    run.start_running()
    run.current_stage = stage
    uow.subject_production_runs.items[run.id] = run
    return run


async def test_successful_stage_queues_the_next_one(
    uow: _Uow, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry, jobs, _ = _build(uow, monkeypatch, {"stage": "sources", "status": "success"})
    run = _run(uow, SubjectProductionStage.SOURCES)

    handler = registry.handler(stage_job_kind(SubjectProductionStage.SOURCES))
    await handler(
        ProductionStageParameters(
            run_id=run.id, expected_stage=SubjectProductionStage.SOURCES.value
        ),
        _Context(),  # type: ignore[arg-type]
    )

    assert [job.kind for job in jobs.submitted] == [
        stage_job_kind(SubjectProductionStage.REFERENCES)
    ]
    assert uow.subject_production_runs.items[run.id].current_stage is (
        SubjectProductionStage.REFERENCES
    )


async def test_recovered_old_stage_job_resubmits_the_current_stage_without_failing_run(
    uow: _Uow, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry, jobs, orchestrator = _build(
        uow, monkeypatch, {"stage": "sources", "status": "success"}
    )
    run = _run(uow, SubjectProductionStage.REFERENCES)

    handler = registry.handler(stage_job_kind(SubjectProductionStage.SOURCES))
    await handler(
        ProductionStageParameters(
            run_id=run.id,
            expected_stage=SubjectProductionStage.SOURCES.value,
            pipeline_generation=run.pipeline_generation,
        ),
        _Context(),  # type: ignore[arg-type]
    )

    assert orchestrator.calls == []
    assert [job.kind for job in jobs.submitted] == [
        stage_job_kind(SubjectProductionStage.REFERENCES)
    ]
    assert uow.subject_production_runs.items[run.id].status is SubjectProductionStatus.RUNNING


async def test_next_stage_job_is_idempotent_per_run_and_stage(
    uow: _Uow, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A retried handler must never re-send the same prompt."""
    registry, jobs, _ = _build(uow, monkeypatch, {"stage": "sources", "status": "success"})
    run = _run(uow, SubjectProductionStage.SOURCES)

    handler = registry.handler(stage_job_kind(SubjectProductionStage.SOURCES))
    await handler(
        ProductionStageParameters(
            run_id=run.id, expected_stage=SubjectProductionStage.SOURCES.value
        ),
        _Context(),  # type: ignore[arg-type]
    )

    assert jobs.submitted[0].idempotency_key == f"production-references-{run.id}-g0"
    assert jobs.submission_options[0]["max_attempts"] == 3


async def test_assembly_stage_queues_no_further_stage(
    uow: _Uow, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry, jobs, _ = _build(uow, monkeypatch, {"stage": "assembly", "status": "success"})
    run = _run(uow, SubjectProductionStage.ASSEMBLY)

    handler = registry.handler(stage_job_kind(SubjectProductionStage.ASSEMBLY))
    await handler(
        ProductionStageParameters(
            run_id=run.id, expected_stage=SubjectProductionStage.ASSEMBLY.value
        ),
        _Context(),  # type: ignore[arg-type]
    )

    assert jobs.submitted == []


def _batch_of(uow: _Uow, first: SubjectProductionRun) -> SubjectProductionRun:
    """A two-subject batch whose second subject is still queued."""
    batch = EditionProductionBatch(
        edition_id=first.edition_id,
        status="running",
    )
    uow.edition_production_batches.items[batch.id] = batch

    second = SubjectProductionRun(
        subject_id=uuid4(),
        edition_id=first.edition_id,
    )
    uow.subject_production_runs.items[second.id] = second

    for position, run in enumerate((first, second), start=1):
        uow.edition_production_batch_items.items.append(
            EditionProductionBatchItem(
                batch_id=batch.id,
                subject_id=run.subject_id,
                production_run_id=run.id,
                position=position,
            )
        )
    return second


async def test_finished_subject_starts_the_next_one_in_the_batch(
    uow: _Uow, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry, jobs, _ = _build(uow, monkeypatch, {"stage": "assembly", "status": "success"})
    first = _run(uow, SubjectProductionStage.ASSEMBLY)
    first.mark_ready()
    second = _batch_of(uow, first)

    handler = registry.handler(stage_job_kind(SubjectProductionStage.ASSEMBLY))
    await handler(
        ProductionStageParameters(
            run_id=first.id, expected_stage=SubjectProductionStage.ASSEMBLY.value
        ),
        _Context(),  # type: ignore[arg-type]
    )

    assert [job.kind for job in jobs.submitted] == [stage_job_kind(SubjectProductionStage.SOURCES)]
    assert uow.subject_production_runs.items[second.id].status is (SubjectProductionStatus.RUNNING)


async def test_failed_subject_does_not_block_the_batch(
    uow: _Uow, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A subject that ends in error must hand over to the next one."""
    registry, jobs, _ = _build(
        uow, monkeypatch, {"stage": "references", "status": "error", "error": "boom"}
    )
    first = _run(uow, SubjectProductionStage.REFERENCES)
    second = _batch_of(uow, first)

    handler = registry.handler(stage_job_kind(SubjectProductionStage.REFERENCES))
    with pytest.raises(JobHandlerError):
        await handler(
            ProductionStageParameters(
                run_id=first.id, expected_stage=SubjectProductionStage.REFERENCES.value
            ),
            _Context(),  # type: ignore[arg-type]
        )

    assert uow.subject_production_runs.items[first.id].status is SubjectProductionStatus.FAILED
    assert [job.kind for job in jobs.submitted] == [stage_job_kind(SubjectProductionStage.SOURCES)]
    assert uow.subject_production_runs.items[second.id].status is (SubjectProductionStatus.RUNNING)


async def test_terminalization_calls_the_checkpoint_once(
    uow: _Uow, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkpoint = _Checkpoint()
    registry, _, _ = _build(
        uow,
        monkeypatch,
        {"stage": "references", "status": "error", "error": "boom"},
        checkpoint=checkpoint,
    )
    first = _run(uow, SubjectProductionStage.REFERENCES)
    _batch_of(uow, first)

    handler = registry.handler(stage_job_kind(SubjectProductionStage.REFERENCES))
    with pytest.raises(JobHandlerError):
        await handler(
            ProductionStageParameters(
                run_id=first.id, expected_stage=SubjectProductionStage.REFERENCES.value
            ),
            _Context(),  # type: ignore[arg-type]
        )

    assert checkpoint.calls == [first.id]


async def test_needs_review_is_a_business_outcome_not_a_crash(
    uow: _Uow, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A format problem must park the subject for review, not fail the job."""
    registry, jobs, _ = _build(
        uow,
        monkeypatch,
        {"stage": "references", "status": "needs_review", "error_code": "format_unusable"},
    )
    first = _run(uow, SubjectProductionStage.REFERENCES)
    second = _batch_of(uow, first)

    handler = registry.handler(stage_job_kind(SubjectProductionStage.REFERENCES))
    await handler(
        ProductionStageParameters(
            run_id=first.id, expected_stage=SubjectProductionStage.REFERENCES.value
        ),
        _Context(),  # type: ignore[arg-type]
    )

    parked = uow.subject_production_runs.items[first.id]
    assert parked.status is SubjectProductionStatus.NEEDS_REVIEW
    assert parked.error_code == "format_unusable"
    # The queue keeps going.
    assert [job.kind for job in jobs.submitted] == [stage_job_kind(SubjectProductionStage.SOURCES)]
    assert uow.subject_production_runs.items[second.id].status is SubjectProductionStatus.RUNNING


async def test_transient_error_stays_retryable_and_keeps_the_run_alive(
    uow: _Uow, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry, jobs, _ = _build(
        uow,
        monkeypatch,
        {
            "stage": "references",
            "status": "transient_error",
            "error_code": "bridge_timeout",
            "details": {"failure_class": "global_transient_pre_submission"},
        },
    )
    run = _run(uow, SubjectProductionStage.REFERENCES)
    context = _Context()
    uow.jobs.items[context.job_id] = _ExecutionJob(attempt=1, max_attempts=3)

    handler = registry.handler(stage_job_kind(SubjectProductionStage.REFERENCES))
    with pytest.raises(JobHandlerError) as excinfo:
        await handler(
            ProductionStageParameters(
                run_id=run.id, expected_stage=SubjectProductionStage.REFERENCES.value
            ),
            context,  # type: ignore[arg-type]
        )

    assert excinfo.value.transient is True
    assert excinfo.value.details == {"failure_class": "global_transient_pre_submission"}
    # The run must not be terminated: the job will be retried.
    assert uow.subject_production_runs.items[run.id].status is SubjectProductionStatus.RUNNING
    assert jobs.submitted == []


async def test_transient_extraction_failure_holds_the_batch_slot_until_retry_exhaustion(
    uow: _Uow, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry, jobs, _ = _build(
        uow,
        monkeypatch,
        {"stage": "extraction", "status": "transient_error", "error_code": "bridge_ui_timeout"},
    )
    first = _run(uow, SubjectProductionStage.EXTRACTION)
    second = _batch_of(uow, first)
    context = _Context()
    uow.jobs.items[context.job_id] = _ExecutionJob(attempt=1, max_attempts=3)

    handler = registry.handler(stage_job_kind(SubjectProductionStage.EXTRACTION))
    with pytest.raises(JobHandlerError) as excinfo:
        await handler(
            ProductionStageParameters(
                run_id=first.id, expected_stage=SubjectProductionStage.EXTRACTION.value
            ),
            context,  # type: ignore[arg-type]
        )

    assert excinfo.value.transient is True
    assert uow.subject_production_runs.items[first.id].status is SubjectProductionStatus.RUNNING
    assert uow.subject_production_runs.items[second.id].status is SubjectProductionStatus.QUEUED
    assert jobs.submitted == []


@pytest.mark.parametrize(
    ("recovery_stage", "expected_delay"),
    (
        (SubjectProductionStage.SOURCES, 7000),
        (SubjectProductionStage.EXTRACTION, 7000),
        (SubjectProductionStage.REFERENCES, 18000),
        (SubjectProductionStage.SYNTHESIS, 18000),
    ),
)
async def test_recovery_dispatch_combines_subject_and_model_pacing(
    uow: _Uow,
    monkeypatch: pytest.MonkeyPatch,
    recovery_stage: SubjectProductionStage,
    expected_delay: int,
) -> None:
    first = _run(uow, SubjectProductionStage.SOURCES)
    next_run = _run(uow, recovery_stage)
    next_run.status = SubjectProductionStatus.RUNNING
    batch = EditionProductionBatch(
        edition_id=first.edition_id,
        status="running",
    )
    uow.edition_production_batches.items[batch.id] = batch
    uow.edition_production_batch_items.items.append(
        EditionProductionBatchItem(
            batch_id=batch.id,
            subject_id=first.subject_id,
            production_run_id=first.id,
            position=1,
        )
    )

    class RecoveryBatchService:
        def __init__(self, *args: object, **kwargs: object) -> None:
            del args, kwargs

        async def clear_next_dispatch(self, run_id: UUID) -> None:
            del run_id

        async def on_subject_terminal(
            self, batch_id: UUID, run_id: UUID, *, correlation_id: str
        ) -> SubjectProductionRun:
            del batch_id, run_id, correlation_id
            return next_run

        async def next_dispatch_delay_ms(self, batch_id: UUID) -> int:
            del batch_id
            return 7000

    monkeypatch.setattr(
        "cti_app.application.production_jobs.EditionProductionService", RecoveryBatchService
    )
    monkeypatch.setattr(
        "cti_app.application.production_jobs.ProductionWorkflowOrchestrator",
        lambda *args, **kwargs: _Orchestrator(
            {"stage": "sources", "status": "error", "error": "failed"}
        ),
    )
    registry = JobRegistry()
    dispatcher = _Dispatcher()
    chain = ProductionStageChain(
        ProductionPacingPolicy(
            subject_jitter_min_seconds=7,
            subject_jitter_max_seconds=7,
            model_jitter_min_seconds=11,
            model_jitter_max_seconds=11,
        )
    )
    jobs = _Jobs()
    chain.bind(jobs, dispatcher)  # type: ignore[arg-type]
    register_production_jobs(registry, lambda: uow, chain=chain)  # type: ignore[arg-type]

    handler = registry.handler(stage_job_kind(SubjectProductionStage.SOURCES))
    with pytest.raises(JobHandlerError):
        await handler(
            ProductionStageParameters(
                run_id=first.id, expected_stage=SubjectProductionStage.SOURCES.value
            ),
            _Context(),  # type: ignore[arg-type]
        )

    assert dispatcher.delays == [expected_delay]


def test_stage_parameters_survive_the_json_round_trip() -> None:
    """Parameters reach the worker as JSON: the UUID comes back as a string.

    With strict validation this raised `Input should be an instance of UUID`
    and every production job died before running.
    """
    run_id = uuid4()
    encoded = ProductionStageParameters(
        run_id=run_id, expected_stage=SubjectProductionStage.SOURCES.value
    ).model_dump(mode="json")

    assert isinstance(encoded["run_id"], str)

    decoded = ProductionStageParameters.model_validate(encoded)

    assert decoded.run_id == run_id
    assert decoded.expected_stage == SubjectProductionStage.SOURCES.value


def test_registry_validates_the_parameters_it_receives_from_the_queue() -> None:
    """The registry is what re-parses them worker-side."""
    registry = JobRegistry()
    register_production_jobs(registry, lambda: None, chain=ProductionStageChain())  # type: ignore[arg-type]
    run_id = uuid4()

    validated = registry.validate(
        stage_job_kind(SubjectProductionStage.SOURCES),
        {"run_id": str(run_id), "expected_stage": "sources"},
    )

    assert isinstance(validated, ProductionStageParameters)
    assert validated.run_id == run_id
