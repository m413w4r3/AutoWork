"""Resuming one article from Review, on the batch state that really exists.

A production that ends with issues leaves the batch terminal
(``completed_with_issues``, phase ``review``) and the edition in ``review``.
Every dispatch fence only lets a QUEUED or RUNNING batch move a subject
forward, so an article corrected from Review used to run its current stage and
then stop: the chained job for the next stage was fenced out and the run stayed
RUNNING forever.  These tests pin the explicit review-recovery transition that
fixes it, and the barriers that must survive it.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4

import pytest

from cti_app.application.edition_workspace import EditionProductionCheckpointService
from cti_app.application.jobs import JobRegistry
from cti_app.application.persistence import UnitOfWorkFactory
from cti_app.application.production_jobs import (
    ProductionReconciliationResumeParameters,
    ProductionStageChain,
    ProductionStageParameters,
    production_reconciliation_resume_idempotency_key,
    production_reconciliation_resume_job_kind,
    register_production_jobs,
    stage_job_kind,
)
from cti_app.application.production_review_recovery import (
    ReviewRecoveryConflictError,
    prepare_batch_for_recovery,
)
from cti_app.application.subject_production import SubjectProductionService
from cti_app.domain.classification import TLP
from cti_app.domain.editions import Edition, EditionStatus
from cti_app.domain.model_runs import ModelSubmissionState
from cti_app.domain.production import (
    PRODUCTION_RECONCILIATION_ERROR_CODE,
    EditionProductionBatch,
    EditionProductionBatchItem,
    ProductionBatchPhase,
    ProductionBatchStatus,
    ProductionSubmissionReconciliation,
    SubjectProductionRun,
    SubjectProductionStage,
    SubjectProductionStatus,
)

pytestmark = pytest.mark.asyncio


class _Runs:
    def __init__(self) -> None:
        self.items: dict[UUID, SubjectProductionRun] = {}

    async def get(self, run_id: UUID) -> SubjectProductionRun | None:
        return self.items.get(run_id)

    async def get_for_update(self, run_id: UUID) -> SubjectProductionRun | None:
        return self.items.get(run_id)

    async def save(self, run: SubjectProductionRun) -> None:
        self.items[run.id] = run


class _Batches:
    def __init__(self) -> None:
        self.items: dict[UUID, EditionProductionBatch] = {}
        self.order: list[UUID] = []

    async def get(self, batch_id: UUID) -> EditionProductionBatch | None:
        return self.items.get(batch_id)

    async def get_for_update(self, batch_id: UUID) -> EditionProductionBatch | None:
        return self.items.get(batch_id)

    async def save(self, batch: EditionProductionBatch) -> None:
        self.items[batch.id] = batch

    async def get_active_for_edition(self, edition_id: UUID) -> EditionProductionBatch | None:
        return next(
            (
                self.items[batch_id]
                for batch_id in self.order
                if self.items[batch_id].edition_id == edition_id
                and self.items[batch_id].status
                in {ProductionBatchStatus.QUEUED, ProductionBatchStatus.RUNNING}
            ),
            None,
        )

    async def get_latest_for_edition(self, edition_id: UUID) -> EditionProductionBatch | None:
        matches = [b for b in self.items.values() if b.edition_id == edition_id]
        return matches[-1] if matches else None

    def add(self, batch: EditionProductionBatch) -> EditionProductionBatch:
        self.items[batch.id] = batch
        self.order.append(batch.id)
        return batch


class _BatchItems:
    def __init__(self) -> None:
        self.items: list[EditionProductionBatchItem] = []

    async def list_for_batch(self, batch_id: UUID) -> list[EditionProductionBatchItem]:
        return [item for item in self.items if item.batch_id == batch_id]

    async def get_by_run(self, run_id: UUID) -> EditionProductionBatchItem | None:
        return next((i for i in self.items if i.production_run_id == run_id), None)

    async def save(self, item: EditionProductionBatchItem) -> None:
        return None


class _Editions:
    def __init__(self, edition: Edition) -> None:
        self.edition = edition
        self.updates = 0

    async def get(self, edition_id: UUID) -> Edition | None:
        return self.edition if edition_id == self.edition.id else None

    async def get_for_update(self, edition_id: UUID) -> Edition | None:
        return await self.get(edition_id)

    async def update(self, edition: Edition, expected_version: int) -> bool:
        self.edition = edition
        self.updates += 1
        return True


class _Audit:
    def __init__(self) -> None:
        self.events: list[Any] = []

    async def append(self, event: Any) -> None:
        self.events.append(event)


class _Artifacts:
    """Every prerequisite artifact of the pipeline is already verified."""

    def __init__(self) -> None:
        self.staled: list[str] = []
        self.missing: set[str] = set()

    async def get_current(self, run_id: UUID, stage: str) -> Any:
        return None if stage in self.missing else object()

    async def mark_from_stage_stale(self, run_id: UUID, stage: str) -> list[str]:
        self.staled.append(stage)
        return []


class _Manifests:
    def __init__(self) -> None:
        self.frozen = False

    async def get_latest_for_edition(self, edition_id: UUID) -> object | None:
        return object() if self.frozen else None


class _SourceCollections:
    async def list_for_subject(self, subject_id: UUID) -> Sequence[Any]:
        return []


class _ExecutionJobs:
    async def get(self, job_id: UUID) -> None:
        return None


class _ModelRuns:
    def __init__(self) -> None:
        self.items: dict[UUID, Any] = {}

    async def get(self, run_id: UUID) -> Any:
        return self.items.get(run_id)


class _Uow:
    def __init__(self, edition: Edition) -> None:
        self.subject_production_runs = _Runs()
        self.edition_production_batches = _Batches()
        self.edition_production_batch_items = _BatchItems()
        self.editions = _Editions(edition)
        self.edition_audit = _Audit()
        self.production_artifacts = _Artifacts()
        self.publication_manifests = _Manifests()
        self.source_collections = _SourceCollections()
        self.jobs = _ExecutionJobs()
        self.model_runs = _ModelRuns()

    async def __aenter__(self) -> _Uow:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def commit(self) -> None:
        return None

    async def rollback(self) -> None:
        return None


class _Job:
    def __init__(self, kind: str, idempotency_key: str, parameters: dict[str, Any]) -> None:
        self.id = uuid4()
        self.kind = kind
        self.idempotency_key = idempotency_key
        self.parameters = parameters


class _Jobs:
    """Records submissions and enforces the real idempotency-key contract."""

    def __init__(self) -> None:
        self.submitted: list[_Job] = []
        self.by_key: dict[str, _Job] = {}
        self.cancelled: list[UUID] = []

    async def submit(
        self,
        *,
        kind: str,
        idempotency_key: str,
        input_parameters: dict[str, Any],
        **options: Any,
    ) -> _Job:
        existing = self.by_key.get(idempotency_key)
        if existing is not None:
            return existing
        job = _Job(kind, idempotency_key, input_parameters)
        self.by_key[idempotency_key] = job
        self.submitted.append(job)
        return job

    async def cancel(self, job_id: UUID, *, actor_id: str = "system") -> None:
        self.cancelled.append(job_id)


class _Dispatcher:
    def __init__(self) -> None:
        self.dispatched: list[UUID] = []

    async def dispatch(self, job_id: UUID, *, delay_ms: int = 0) -> None:
        self.dispatched.append(job_id)


class _Context:
    def __init__(self) -> None:
        self.job_id = uuid4()

    async def correlation_id(self) -> str:
        return "review-recovery"

    async def report_progress(self, current: int, total: int, message: str | None = None) -> None:
        return None

    async def check_cancelled(self) -> None:
        return None


class _Orchestrator:
    """Every stage succeeds; only the dispatch backbone is under test."""

    def __init__(self, runs: _Runs) -> None:
        self.calls: list[SubjectProductionStage] = []
        self._runs = runs

    async def execute_stage(
        self,
        run_id: UUID,
        expected_stage: SubjectProductionStage,
        context: object | None = None,
        correlation_id: str = "-",
    ) -> dict[str, Any]:
        self.calls.append(expected_stage)
        if expected_stage is SubjectProductionStage.ASSEMBLY:
            # Assembly is the stage that ends the run, exactly as in production.
            run = self._runs.items[run_id]
            run.mark_ready()
        return {"stage": expected_stage.value, "status": "success"}


def _edition(status: EditionStatus = EditionStatus.REVIEW) -> Edition:
    return Edition(
        country="France",
        country_code="FR",
        period_start=date(2026, 8, 1),
        period_end=date(2026, 8, 31),
        tlp=TLP.GREEN,
        languages=("fr",),
        target_articles=2,
        source_profile="test",
        status=status,
    )


class _World:
    """The exact state reached after a batch that finished with issues."""

    def __init__(
        self,
        *,
        edition_status: EditionStatus = EditionStatus.REVIEW,
        batch_status: ProductionBatchStatus = ProductionBatchStatus.COMPLETED_WITH_ISSUES,
        batch_phase: ProductionBatchPhase = ProductionBatchPhase.REVIEW,
        stage: SubjectProductionStage = SubjectProductionStage.EXTRACTION,
        run_status: SubjectProductionStatus = SubjectProductionStatus.NEEDS_REVIEW,
    ) -> None:
        self.edition = _edition(edition_status)
        self.uow = _Uow(self.edition)
        self.batch = self.uow.edition_production_batches.add(
            EditionProductionBatch(
                edition_id=self.edition.id,
                status=batch_status,
                phase=batch_phase,
            )
        )
        self.run = SubjectProductionRun(
            subject_id=uuid4(),
            edition_id=self.edition.id,
            status=run_status,
            current_stage=stage,
            pipeline_generation=1,
            error_code="synthesis_error",
            error_message="stopped",
        )
        self.uow.subject_production_runs.items[self.run.id] = self.run
        self.sibling = SubjectProductionRun(
            subject_id=uuid4(),
            edition_id=self.edition.id,
            status=SubjectProductionStatus.READY,
            current_stage=SubjectProductionStage.ASSEMBLY,
        )
        self.uow.subject_production_runs.items[self.sibling.id] = self.sibling
        for position, run in enumerate((self.run, self.sibling), start=1):
            self.uow.edition_production_batch_items.items.append(
                EditionProductionBatchItem(
                    batch_id=self.batch.id,
                    subject_id=run.subject_id,
                    production_run_id=run.id,
                    position=position,
                )
            )

    def factory(self) -> _Uow:
        return self.uow


def _register(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> tuple[JobRegistry, _Jobs, _Orchestrator]:
    orchestrator = _Orchestrator(world.uow.subject_production_runs)
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
        cast(UnitOfWorkFactory, world.factory),
        chain=chain,
        checkpoint=cast(EditionProductionCheckpointService | None, None),
    )
    return registry, jobs, orchestrator


async def _drain(
    registry: JobRegistry, jobs: _Jobs, *, start: int = 0, limit: int = 12
) -> list[str]:
    """Run every submitted stage job, exactly as a worker pool would."""
    executed: list[str] = []
    index = start
    while index < len(jobs.submitted) and index < start + limit:
        job = jobs.submitted[index]
        index += 1
        executed.append(job.kind)
        model = (
            ProductionReconciliationResumeParameters
            if job.kind == production_reconciliation_resume_job_kind()
            else ProductionStageParameters
        )
        await handler_call(registry, job, model)
    return executed


async def handler_call(registry: JobRegistry, job: _Job, model: Any) -> None:
    await registry.handler(job.kind)(model(**job.parameters), cast(Any, _Context()))


async def _retry(world: _World, stage: SubjectProductionStage) -> Any:
    return await SubjectProductionService(cast(Any, world.factory)).retry_from_stage(
        world.run.id, stage
    )


async def test_review_retry_reopens_the_finished_batch_and_reaches_assembly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = _World()
    registry, jobs, orchestrator = _register(world, monkeypatch)

    result = await _retry(world, SubjectProductionStage.EXTRACTION)

    assert world.batch.status is ProductionBatchStatus.RUNNING
    assert world.batch.phase is ProductionBatchPhase.REVIEW
    assert result.run.status is SubjectProductionStatus.RUNNING
    assert result.run.pipeline_generation == 2

    # The API dispatches the first stage itself; the chain must carry the rest.
    await jobs.submit(
        kind=stage_job_kind(SubjectProductionStage.EXTRACTION),
        idempotency_key=f"production-extraction-{world.run.id}-g2",
        input_parameters={
            "run_id": str(world.run.id),
            "expected_stage": "extraction",
            "pipeline_generation": 2,
        },
    )
    await _drain(registry, jobs)

    assert orchestrator.calls == [
        SubjectProductionStage.EXTRACTION,
        SubjectProductionStage.SYNTHESIS,
        SubjectProductionStage.ASSEMBLY,
    ]
    assert jobs.cancelled == []
    assert world.run.status is SubjectProductionStatus.READY
    # The batch closes itself again once the corrected article is terminal —
    # cleanly this time, since every article of the batch is now ready.
    assert world.batch.status is ProductionBatchStatus.COMPLETED
    assert world.batch.phase is ProductionBatchPhase.REVIEW
    # Review-time recovery never reopens the whole edition in production.
    assert world.uow.editions.edition.status is EditionStatus.REVIEW


async def test_a_still_running_batch_keeps_its_own_phase_on_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = _World(
        edition_status=EditionStatus.PRODUCTION,
        batch_status=ProductionBatchStatus.RUNNING,
        batch_phase=ProductionBatchPhase.INITIAL,
    )
    _register(world, monkeypatch)

    await _retry(world, SubjectProductionStage.EXTRACTION)

    assert world.batch.status is ProductionBatchStatus.RUNNING
    assert world.batch.phase is ProductionBatchPhase.INITIAL


async def test_cancelled_batch_blocks_a_review_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    world = _World()
    _register(world, monkeypatch)
    world.batch.status = ProductionBatchStatus.CANCELLED

    with pytest.raises(ValueError) as error:
        await _retry(world, SubjectProductionStage.EXTRACTION)

    assert str(error.value) == "production_batch_cancelled"
    assert world.batch.status is ProductionBatchStatus.CANCELLED
    assert world.run.status is SubjectProductionStatus.NEEDS_REVIEW
    assert world.run.pipeline_generation == 1


async def test_publication_freeze_blocks_a_review_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    world = _World()
    _register(world, monkeypatch)
    world.uow.publication_manifests.frozen = True

    with pytest.raises(ValueError) as error:
        await _retry(world, SubjectProductionStage.EXTRACTION)

    assert str(error.value) == "edition_frozen_for_publication"
    assert world.batch.status is ProductionBatchStatus.COMPLETED_WITH_ISSUES


async def test_assembling_edition_blocks_a_review_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    world = _World(edition_status=EditionStatus.ASSEMBLING)
    _register(world, monkeypatch)

    with pytest.raises(ValueError) as error:
        await _retry(world, SubjectProductionStage.EXTRACTION)

    assert str(error.value) == "edition_frozen_for_publication"
    assert world.batch.status is ProductionBatchStatus.COMPLETED_WITH_ISSUES


async def test_a_running_sibling_forbids_reopening_a_finished_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = _World()
    _register(world, monkeypatch)
    world.sibling.status = SubjectProductionStatus.RUNNING

    with pytest.raises(ValueError) as error:
        await _retry(world, SubjectProductionStage.EXTRACTION)

    assert str(error.value) == "production_active_sibling"
    assert world.batch.status is ProductionBatchStatus.COMPLETED_WITH_ISSUES


async def test_a_second_retry_click_neither_restarts_nor_duplicates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = _World()
    _register(world, monkeypatch)

    first = await _retry(world, SubjectProductionStage.EXTRACTION)
    with pytest.raises(ValueError) as error:
        await _retry(world, SubjectProductionStage.EXTRACTION)

    assert str(error.value) == "retry_not_allowed_while_running"
    assert world.run.pipeline_generation == first.run.pipeline_generation == 2


async def test_a_cancelled_run_is_never_retried_from_review(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = _World(run_status=SubjectProductionStatus.CANCELLED)
    _register(world, monkeypatch)

    with pytest.raises(ValueError) as error:
        await _retry(world, SubjectProductionStage.EXTRACTION)

    assert str(error.value) == "production_run_cancelled"
    assert world.run.status is SubjectProductionStatus.CANCELLED
    assert world.batch.status is ProductionBatchStatus.COMPLETED_WITH_ISSUES


async def test_an_old_batch_is_never_reopened_behind_a_newer_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = _World()
    _register(world, monkeypatch)
    newer = world.uow.edition_production_batches.add(
        EditionProductionBatch(
            edition_id=world.edition.id,
            status=ProductionBatchStatus.RUNNING,
            phase=ProductionBatchPhase.INITIAL,
        )
    )

    with pytest.raises(ValueError) as error:
        await _retry(world, SubjectProductionStage.EXTRACTION)

    assert str(error.value) == "production_batch_superseded"
    assert world.batch.status is ProductionBatchStatus.COMPLETED_WITH_ISSUES
    assert newer.status is ProductionBatchStatus.RUNNING
    assert newer.phase is ProductionBatchPhase.INITIAL


async def test_the_validation_pass_never_mutates_the_batch() -> None:
    world = _World()

    async with world.uow as uow:
        batch = await prepare_batch_for_recovery(cast(Any, uow), world.run, reopen=False)

    assert batch is world.batch
    assert world.batch.status is ProductionBatchStatus.COMPLETED_WITH_ISSUES

    world.batch.status = ProductionBatchStatus.CANCELLED
    async with world.uow as uow:
        with pytest.raises(ReviewRecoveryConflictError) as error:
            await prepare_batch_for_recovery(cast(Any, uow), world.run, reopen=False)
    assert error.value.reason == "batch_cancelled"


async def test_reconciliation_resume_job_chains_to_assembly_on_a_reopened_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The adopted answer resumes the same generation and keeps going."""
    world = _World()
    registry, jobs, orchestrator = _register(world, monkeypatch)

    model_run_id = uuid4()
    output_sha256 = "c" * 64
    world.run.error_code = PRODUCTION_RECONCILIATION_ERROR_CODE
    world.run.reconciliation = ProductionSubmissionReconciliation(
        production_run_id=world.run.id,
        model_run_id=model_run_id,
        stage=SubjectProductionStage.EXTRACTION,
        bridge_response_id="bridge-1",
        submission_state=ModelSubmissionState.SUBMITTED_OR_UNKNOWN,
        phase="reconciliation",
        output_sha256=output_sha256,
        provenance="visible_recovery",
    )
    world.uow.model_runs.items[model_run_id] = SimpleNamespace(
        status=SimpleNamespace(value="succeeded"),
        raw_output_sha256=output_sha256,
    )
    # What the reconciliation service does under the Edition lock.
    async with world.uow as uow:
        await prepare_batch_for_recovery(cast(Any, uow), world.run, reopen=True)
    world.run.resume_reconciled(expected_stage=SubjectProductionStage.EXTRACTION)

    await jobs.submit(
        kind=production_reconciliation_resume_job_kind(),
        idempotency_key=production_reconciliation_resume_idempotency_key(
            world.run.id,
            SubjectProductionStage.EXTRACTION,
            world.run.pipeline_generation,
            model_run_id,
            output_sha256,
        ),
        input_parameters={
            "run_id": str(world.run.id),
            "expected_stage": "extraction",
            "pipeline_generation": world.run.pipeline_generation,
            "reconciliation_model_run_id": str(model_run_id),
            "reconciled_output_sha256": output_sha256,
        },
    )
    await _drain(registry, jobs)

    assert orchestrator.calls == [
        SubjectProductionStage.EXTRACTION,
        SubjectProductionStage.SYNTHESIS,
        SubjectProductionStage.ASSEMBLY,
    ]
    # Adoption never opens a new pipeline generation.
    assert world.run.pipeline_generation == 1
    assert world.run.status is SubjectProductionStatus.READY
    assert jobs.cancelled == []
