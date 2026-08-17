"""Tests for the production execution backbone.

These cover the two links that make the workflow autonomous:
stage N queues stage N+1, and a terminal subject hands the batch to the next
subject — including when it failed, so one bad subject cannot block the queue.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import pytest

from cti_app.application.jobs import JobHandlerError, JobRegistry
from cti_app.application.production_jobs import (
    ProductionStageChain,
    ProductionStageParameters,
    register_production_jobs,
    stage_job_kind,
)
from cti_app.domain.production import (
    EditionProductionBatch,
    EditionProductionBatchItem,
    ProductionProfile,
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


class _Jobs:
    def __init__(self) -> None:
        self.submitted: list[_Job] = []

    async def submit(self, *, kind: str, idempotency_key: str, **_: Any) -> _Job:
        job = _Job(kind, idempotency_key)
        self.submitted.append(job)
        return job


class _Dispatcher:
    def __init__(self) -> None:
        self.dispatched: list[UUID] = []

    async def dispatch(self, job_id: UUID) -> None:
        self.dispatched.append(job_id)


class _Context:
    """Minimal JobExecutionContext stand-in."""

    def __init__(self) -> None:
        self.job_id = uuid4()
        self.progress: list[tuple[int, int]] = []

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
    ) -> dict[str, Any]:
        self.calls.append(expected_stage)
        return self.result


@pytest.fixture
def uow() -> _Uow:
    return _Uow()


def _build(
    uow: _Uow,
    monkeypatch: pytest.MonkeyPatch,
    result: dict[str, Any],
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
    register_production_jobs(registry, lambda: uow, chain=chain)  # type: ignore[arg-type]
    return registry, jobs, orchestrator


def _run(uow: _Uow, stage: SubjectProductionStage) -> SubjectProductionRun:
    run = SubjectProductionRun(
        subject_id=uuid4(),
        edition_id=uuid4(),
        profile=ProductionProfile.BRIEF_AUTO,
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

    assert jobs.submitted[0].idempotency_key == f"production-references-{run.id}"


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
        profile=ProductionProfile.BRIEF_AUTO,
        status="running",
    )
    uow.edition_production_batches.items[batch.id] = batch

    second = SubjectProductionRun(
        subject_id=uuid4(),
        edition_id=first.edition_id,
        profile=ProductionProfile.BRIEF_AUTO,
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
