"""Cancelling a production preserves it; resuming continues where it stopped.

Cancellation writes a status on the run and cancels its jobs — it deletes no
archived source, no artifact and no Q2 checkpoint.  These tests pin the promise
that follows from that: a cancelled article resumes at its first stage without a
live artifact, reuses everything before it, and pays exactly the model calls the
remaining stages owe.  They cover the four states an operator can cancel in, and
the invariants a resume must never break — one run, one edition entry, no
duplicated or orphaned artifact.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4

import pytest

from cti_app.application.edition_review import review_item_can_resume, review_item_can_retry
from cti_app.application.edition_workspace import EditionProductionCheckpointService
from cti_app.application.jobs import JobRegistry
from cti_app.application.persistence import UnitOfWorkFactory
from cti_app.application.production_jobs import (
    ProductionStageChain,
    ProductionStageParameters,
    register_production_jobs,
    stage_job_kind,
)
from cti_app.application.production_resume import plan_production_resume
from cti_app.application.subject_production import SubjectProductionService
from cti_app.domain.classification import TLP
from cti_app.domain.editions import Edition, EditionStatus
from cti_app.domain.production import (
    EditionProductionBatch,
    EditionProductionBatchItem,
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionArtifactStatus,
    ProductionBatchPhase,
    ProductionBatchStatus,
    SubjectProductionRun,
    SubjectProductionStage,
    SubjectProductionStatus,
)

pytestmark = pytest.mark.asyncio

SOURCE_IDS = ("S1", "S2", "S3")


class _Runs:
    def __init__(self) -> None:
        self.items: dict[UUID, SubjectProductionRun] = {}
        self.saves = 0

    async def get(self, run_id: UUID) -> SubjectProductionRun | None:
        return self.items.get(run_id)

    async def get_for_update(self, run_id: UUID) -> SubjectProductionRun | None:
        return self.items.get(run_id)

    async def add(self, run: SubjectProductionRun) -> None:
        self.items[run.id] = run

    async def save(self, run: SubjectProductionRun) -> None:
        self.saves += 1
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
        matches = [batch for batch in self.items.values() if batch.edition_id == edition_id]
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
        return next((item for item in self.items if item.production_run_id == run_id), None)

    async def save(self, item: EditionProductionBatchItem) -> None:
        return None


class _Editions:
    def __init__(self, edition: Edition) -> None:
        self.edition = edition

    async def get(self, edition_id: UUID) -> Edition | None:
        return self.edition if edition_id == self.edition.id else None

    async def get_for_update(self, edition_id: UUID) -> Edition | None:
        return await self.get(edition_id)

    async def update(self, edition: Edition, expected_version: int) -> bool:
        self.edition = edition
        return True


class _Artifacts:
    """Real artifact rows: the resume plan is read from them."""

    def __init__(self) -> None:
        self.items: list[ProductionArtifact] = []
        self.staled: list[str] = []

    def add(self, run: SubjectProductionRun, stage: ProductionArtifactStage) -> ProductionArtifact:
        artifact = ProductionArtifact(
            production_run_id=run.id,
            subject_id=run.subject_id,
            stage=stage,
            version=1 + sum(1 for item in self.items if item.stage is stage),
            input_hash="a" * 64,
        )
        self.items.append(artifact)
        return artifact

    async def append(self, artifact: ProductionArtifact) -> None:
        self.items.append(artifact)

    async def list_for_run(self, run_id: UUID) -> Sequence[ProductionArtifact]:
        return [item for item in self.items if item.production_run_id == run_id]

    async def get_current(self, run_id: UUID, stage: str) -> ProductionArtifact | None:
        matches = [
            item
            for item in self.items
            if item.production_run_id == run_id
            and item.stage.value == stage
            and item.status is not ProductionArtifactStatus.STALE
        ]
        return max(matches, key=lambda item: item.version) if matches else None

    async def mark_from_stage_stale(self, run_id: UUID, stage: str) -> list[str]:
        self.staled.append(stage)
        return []


class _SourceCollections:
    def __init__(self, archived: int) -> None:
        self.archived = archived

    async def list_for_subject(self, subject_id: UUID) -> Sequence[Any]:
        del subject_id
        return [SimpleNamespace(state=SimpleNamespace(value="archived"))] * self.archived


class _Audit:
    def __init__(self) -> None:
        self.events: list[Any] = []

    async def append(self, event: Any) -> None:
        self.events.append(event)


class _ExecutionJobs:
    async def get(self, job_id: UUID) -> None:
        return None


class _ModelRuns:
    async def get(self, run_id: UUID) -> None:
        return None


class _Uow:
    def __init__(self, edition: Edition, *, archived_sources: int) -> None:
        self.subject_production_runs = _Runs()
        self.edition_production_batches = _Batches()
        self.edition_production_batch_items = _BatchItems()
        self.editions = _Editions(edition)
        self.edition_audit = _Audit()
        self.production_artifacts = _Artifacts()
        self.source_collections = _SourceCollections(archived_sources)
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
        return "cancel-resume"

    async def report_progress(self, current: int, total: int, message: str | None = None) -> None:
        return None

    async def check_cancelled(self) -> None:
        return None


class _Orchestrator:
    """A pipeline whose only observable cost is its model calls.

    Each stage persists the artifact it owes and spends what the real pipeline
    spends: one call for Q1 and Q4, one per source still missing a Q2 answer,
    none for the deterministic assembly.
    """

    def __init__(self, uow: _Uow) -> None:
        self.calls: list[SubjectProductionStage] = []
        self.model_calls: list[str] = []
        self._uow = uow

    async def execute_stage(
        self,
        run_id: UUID,
        expected_stage: SubjectProductionStage,
        context: object | None = None,
        correlation_id: str = "-",
    ) -> dict[str, Any]:
        self.calls.append(expected_stage)
        run = self._uow.subject_production_runs.items[run_id]
        artifacts = self._uow.production_artifacts
        if expected_stage is SubjectProductionStage.REFERENCES:
            self.model_calls.append("q1")
            artifacts.add(run, ProductionArtifactStage.REFERENCES)
        elif expected_stage is SubjectProductionStage.EXTRACTION:
            for entry in (run.extraction_progress or {}).get("sources", []):
                if entry["status"] not in {"cached", "succeeded"}:
                    self.model_calls.append(f"q2:{entry['source_id']}")
                    entry["status"] = "succeeded"
            artifacts.add(run, ProductionArtifactStage.EXTRACTION)
        elif expected_stage is SubjectProductionStage.SYNTHESIS:
            self.model_calls.append("q4")
            artifacts.add(run, ProductionArtifactStage.SYNTHESIS)
        elif expected_stage is SubjectProductionStage.ASSEMBLY:
            artifacts.add(run, ProductionArtifactStage.PUBLICATION)
            run.mark_ready()
        return {"stage": expected_stage.value, "status": "success"}


def _edition(status: EditionStatus = EditionStatus.PRODUCTION) -> Edition:
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


def _progress(*completed: str) -> dict[str, Any]:
    return {
        "total_sources": len(SOURCE_IDS),
        "sources": [
            {
                "source_id": source_id,
                "status": "succeeded" if source_id in completed else "pending",
            }
            for source_id in SOURCE_IDS
        ],
    }


class _World:
    """One edition batch, one article cancelled at a chosen point."""

    def __init__(
        self,
        *,
        stage: SubjectProductionStage,
        produced: Sequence[ProductionArtifactStage] = (),
        progress: dict[str, Any] | None = None,
        archived_sources: int = len(SOURCE_IDS),
        edition_status: EditionStatus = EditionStatus.PRODUCTION,
        batch_status: ProductionBatchStatus = ProductionBatchStatus.RUNNING,
        batch_phase: ProductionBatchPhase = ProductionBatchPhase.INITIAL,
        with_sibling: bool = False,
    ) -> None:
        self.edition = _edition(edition_status)
        self.uow = _Uow(self.edition, archived_sources=archived_sources)
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
            current_stage=stage,
            pipeline_generation=1,
            extraction_progress=progress,
        )
        self.run.start_running()
        self.uow.subject_production_runs.items[self.run.id] = self.run
        for artifact_stage in produced:
            self.uow.production_artifacts.add(self.run, artifact_stage)

        runs = [self.run]
        if with_sibling:
            self.sibling = SubjectProductionRun(
                subject_id=uuid4(),
                edition_id=self.edition.id,
                status=SubjectProductionStatus.READY,
                current_stage=SubjectProductionStage.ASSEMBLY,
            )
            self.uow.subject_production_runs.items[self.sibling.id] = self.sibling
            self.uow.production_artifacts.add(self.sibling, ProductionArtifactStage.PUBLICATION)
            runs.append(self.sibling)
        for position, run in enumerate(runs, start=1):
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

    def service(self) -> SubjectProductionService:
        return SubjectProductionService(cast(Any, self.factory))

    def artifact_identities(self) -> set[tuple[str, UUID]]:
        return {
            (artifact.stage.value, artifact.id)
            for artifact in self.uow.production_artifacts.items
            if artifact.status is not ProductionArtifactStatus.STALE
        }


def _register(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> tuple[JobRegistry, _Jobs, _Orchestrator]:
    orchestrator = _Orchestrator(world.uow)
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


async def _drain(registry: JobRegistry, jobs: _Jobs, *, limit: int = 12) -> None:
    index = 0
    while index < len(jobs.submitted) and index < limit:
        job = jobs.submitted[index]
        index += 1
        await registry.handler(job.kind)(
            ProductionStageParameters(**job.parameters), cast(Any, _Context())
        )


async def _resume_and_drain(
    world: _World,
    registry: JobRegistry,
    jobs: _Jobs,
) -> Any:
    """Resume, then dispatch the first stage exactly as the API endpoint does."""
    resumed = await world.service().resume_cancelled_run(world.run.id)
    await jobs.submit(
        kind=stage_job_kind(resumed.plan.resume_from_stage),
        idempotency_key=(
            f"production-{resumed.plan.resume_from_stage.value}-"
            f"{world.run.id}-g{resumed.run.pipeline_generation}"
        ),
        input_parameters={
            "run_id": str(world.run.id),
            "expected_stage": resumed.plan.resume_from_stage.value,
            "pipeline_generation": resumed.run.pipeline_generation,
        },
    )
    await _drain(registry, jobs)
    return resumed


# --- The domain transition -------------------------------------------------


def _cancelled_run(stage: SubjectProductionStage) -> SubjectProductionRun:
    run = SubjectProductionRun(subject_id=uuid4(), edition_id=uuid4(), current_stage=stage)
    run.start_running()
    run.mark_cancelled()
    return run


async def test_cancellation_only_writes_a_status() -> None:
    """Nothing the run produced is touched: cancellation is not a rollback."""
    world = _World(
        stage=SubjectProductionStage.EXTRACTION,
        produced=(ProductionArtifactStage.REFERENCES,),
        progress=_progress("S1"),
    )
    before = world.artifact_identities()

    await world.service().cancel_run_with_result(world.run.id)

    assert world.run.status is SubjectProductionStatus.CANCELLED
    assert world.artifact_identities() == before
    assert world.uow.production_artifacts.staled == []
    assert world.run.extraction_progress == _progress("S1")


async def test_resume_opens_a_new_generation_without_invalidating_anything() -> None:
    run = _cancelled_run(SubjectProductionStage.EXTRACTION)
    run.force_recompute_from_stage = SubjectProductionStage.EXTRACTION

    run.resume_after_cancellation(SubjectProductionStage.EXTRACTION)

    assert run.status is SubjectProductionStatus.RUNNING
    assert run.current_stage is SubjectProductionStage.EXTRACTION
    assert run.pipeline_generation == 1
    # A resume reuses; only a retry invalidates.
    assert run.force_recompute_from_stage is None
    assert run.finished_at is None


@pytest.mark.parametrize(
    "status",
    (
        SubjectProductionStatus.RUNNING,
        SubjectProductionStatus.READY,
        SubjectProductionStatus.FAILED,
        SubjectProductionStatus.NEEDS_REVIEW,
    ),
)
async def test_only_a_cancelled_run_is_resumable(status: SubjectProductionStatus) -> None:
    run = SubjectProductionRun(subject_id=uuid4(), edition_id=uuid4())
    run.start_running()
    if status is not SubjectProductionStatus.RUNNING:
        {
            SubjectProductionStatus.READY: lambda: run.mark_ready(),
            SubjectProductionStatus.FAILED: lambda: run.mark_failed(code="x", message="y"),
            SubjectProductionStatus.NEEDS_REVIEW: lambda: run.mark_needs_review(
                code="x", message="y"
            ),
        }[status]()

    with pytest.raises(ValueError, match="production_run_not_resumable"):
        run.resume_after_cancellation(SubjectProductionStage.SYNTHESIS)


# --- The four cancellation points ------------------------------------------


async def test_cancel_during_extraction_resumes_extraction_without_losing_sources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = _World(
        stage=SubjectProductionStage.EXTRACTION,
        produced=(ProductionArtifactStage.REFERENCES,),
        progress=_progress("S1"),
    )
    registry, jobs, orchestrator = _register(world, monkeypatch)
    await world.service().cancel_run_with_result(world.run.id)
    references = await world.uow.production_artifacts.get_current(world.run.id, "references")

    resumed = await _resume_and_drain(world, registry, jobs)

    plan = resumed.plan
    assert plan.previous_status is SubjectProductionStatus.CANCELLED
    assert plan.resume_from_stage is SubjectProductionStage.EXTRACTION
    assert plan.reused_artifacts == ("references",)
    # Two sources still owe a Q2 answer, and Q4 owes one call.
    assert plan.model_calls_expected == 3
    assert orchestrator.calls == [
        SubjectProductionStage.EXTRACTION,
        SubjectProductionStage.SYNTHESIS,
        SubjectProductionStage.ASSEMBLY,
    ]
    assert orchestrator.model_calls == ["q2:S2", "q2:S3", "q4"]
    # The archived sources and the reference report are the same rows as before.
    assert (
        await world.uow.production_artifacts.get_current(world.run.id, "references")
    ) is references
    assert world.uow.production_artifacts.staled == []
    assert world.run.status is SubjectProductionStatus.READY


async def test_cancel_after_extraction_never_replays_extraction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = _World(
        stage=SubjectProductionStage.SYNTHESIS,
        produced=(ProductionArtifactStage.REFERENCES, ProductionArtifactStage.EXTRACTION),
        progress=_progress(*SOURCE_IDS),
    )
    registry, jobs, orchestrator = _register(world, monkeypatch)
    await world.service().cancel_run_with_result(world.run.id)

    resumed = await _resume_and_drain(world, registry, jobs)

    assert resumed.plan.resume_from_stage is SubjectProductionStage.SYNTHESIS
    assert resumed.plan.reused_artifacts == ("references", "extraction")
    assert resumed.plan.model_calls_expected == 1
    assert orchestrator.calls == [
        SubjectProductionStage.SYNTHESIS,
        SubjectProductionStage.ASSEMBLY,
    ]
    assert orchestrator.model_calls == ["q4"]
    assert world.run.status is SubjectProductionStatus.READY


async def test_cancel_after_synthesis_only_assembles(monkeypatch: pytest.MonkeyPatch) -> None:
    world = _World(
        stage=SubjectProductionStage.ASSEMBLY,
        produced=(
            ProductionArtifactStage.REFERENCES,
            ProductionArtifactStage.EXTRACTION,
            ProductionArtifactStage.SYNTHESIS,
        ),
        progress=_progress(*SOURCE_IDS),
    )
    registry, jobs, orchestrator = _register(world, monkeypatch)
    await world.service().cancel_run_with_result(world.run.id)

    resumed = await _resume_and_drain(world, registry, jobs)

    assert resumed.plan.resume_from_stage is SubjectProductionStage.ASSEMBLY
    assert resumed.plan.reused_artifacts == ("references", "extraction", "synthesis")
    assert resumed.plan.model_calls_expected == 0
    assert orchestrator.calls == [SubjectProductionStage.ASSEMBLY]
    assert orchestrator.model_calls == []
    assert world.run.status is SubjectProductionStatus.READY


async def test_cancel_before_references_resumes_the_first_model_stage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = _World(stage=SubjectProductionStage.REFERENCES)
    registry, jobs, orchestrator = _register(world, monkeypatch)
    await world.service().cancel_run_with_result(world.run.id)

    resumed = await _resume_and_drain(world, registry, jobs)

    assert resumed.plan.resume_from_stage is SubjectProductionStage.REFERENCES
    assert resumed.plan.reused_artifacts == ()
    # Q1, then one Q2 call per archived source, then Q4.
    assert resumed.plan.model_calls_expected == 1 + len(SOURCE_IDS) + 1
    assert orchestrator.calls[0] is SubjectProductionStage.REFERENCES
    assert world.run.status is SubjectProductionStatus.READY


async def test_cancel_without_any_archived_source_resumes_collection() -> None:
    world = _World(stage=SubjectProductionStage.SOURCES, archived_sources=0)
    await world.service().cancel_run_with_result(world.run.id)

    resumed = await world.service().resume_cancelled_run(world.run.id)

    assert resumed.plan.resume_from_stage is SubjectProductionStage.SOURCES
    assert resumed.plan.reused_artifacts == ()


# --- Invariants a resume must never break ----------------------------------


async def test_resume_keeps_one_run_one_edition_entry_and_no_orphan_artifact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = _World(
        stage=SubjectProductionStage.EXTRACTION,
        produced=(ProductionArtifactStage.REFERENCES,),
        progress=_progress("S1"),
    )
    registry, jobs, _ = _register(world, monkeypatch)
    await world.service().cancel_run_with_result(world.run.id)
    run_ids = set(world.uow.subject_production_runs.items)
    kept = world.artifact_identities()

    resumed = await _resume_and_drain(world, registry, jobs)

    # The same run continues: no rival run, no second entry in the edition.
    assert resumed.run.id == world.run.id
    assert set(world.uow.subject_production_runs.items) == run_ids
    assert len(await world.uow.edition_production_batch_items.list_for_batch(world.batch.id)) == 1
    # Every artifact belongs to the resumed run, and each stage keeps exactly
    # one current row.
    artifacts = await world.uow.production_artifacts.list_for_run(world.run.id)
    assert len(artifacts) == len(world.uow.production_artifacts.items)
    current = [artifact.stage.value for artifact in artifacts]
    assert sorted(current) == ["extraction", "publication", "references", "synthesis"]
    # What existed before the resume is still there, untouched.
    assert kept <= world.artifact_identities()


async def test_a_cancelled_article_of_an_edition_resumes_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Case 4: one article of a finished batch, resumed from the Review."""
    world = _World(
        stage=SubjectProductionStage.SYNTHESIS,
        produced=(ProductionArtifactStage.REFERENCES, ProductionArtifactStage.EXTRACTION),
        progress=_progress(*SOURCE_IDS),
        edition_status=EditionStatus.REVIEW,
        batch_status=ProductionBatchStatus.COMPLETED_WITH_ISSUES,
        batch_phase=ProductionBatchPhase.REVIEW,
        with_sibling=True,
    )
    registry, jobs, orchestrator = _register(world, monkeypatch)
    await world.service().cancel_run_with_result(world.run.id)
    sibling_generation = world.sibling.pipeline_generation

    resumed = await _resume_and_drain(world, registry, jobs)

    assert resumed.batch_id == world.batch.id
    assert world.batch.phase is ProductionBatchPhase.REVIEW
    assert orchestrator.model_calls == ["q4"]
    assert world.run.status is SubjectProductionStatus.READY
    # The neighbour is untouched: resuming is an article-local gesture.
    assert world.sibling.status is SubjectProductionStatus.READY
    assert world.sibling.pipeline_generation == sibling_generation
    # Review-time recovery never reopens the whole edition in production.
    assert world.uow.editions.edition.status is EditionStatus.REVIEW


async def test_resume_is_refused_when_a_sibling_is_still_running() -> None:
    world = _World(
        stage=SubjectProductionStage.SYNTHESIS,
        produced=(ProductionArtifactStage.REFERENCES, ProductionArtifactStage.EXTRACTION),
        with_sibling=True,
    )
    await world.service().cancel_run_with_result(world.run.id)
    world.sibling.status = SubjectProductionStatus.RUNNING

    with pytest.raises(ValueError, match="active_sibling"):
        await world.service().resume_cancelled_run(world.run.id)

    assert world.run.status is SubjectProductionStatus.CANCELLED


async def test_resume_is_refused_on_a_cancelled_batch() -> None:
    world = _World(stage=SubjectProductionStage.SYNTHESIS)
    await world.service().cancel_run_with_result(world.run.id)
    world.batch.status = ProductionBatchStatus.CANCELLED

    with pytest.raises(ValueError, match="batch_cancelled"):
        await world.service().resume_cancelled_run(world.run.id)

    assert world.run.status is SubjectProductionStatus.CANCELLED


async def test_resume_is_refused_once_the_edition_left_production() -> None:
    world = _World(stage=SubjectProductionStage.SYNTHESIS, edition_status=EditionStatus.ASSEMBLING)
    await world.service().cancel_run_with_result(world.run.id)

    with pytest.raises(ValueError, match="edition_frozen_for_publication"):
        await world.service().resume_cancelled_run(world.run.id)


async def test_resume_is_refused_on_a_run_that_was_not_cancelled() -> None:
    world = _World(stage=SubjectProductionStage.SYNTHESIS)

    with pytest.raises(ValueError, match="production_run_not_resumable"):
        await world.service().resume_cancelled_run(world.run.id)


# --- The plan itself --------------------------------------------------------


async def test_a_stale_artifact_is_not_evidence_of_a_complete_stage() -> None:
    world = _World(
        stage=SubjectProductionStage.SYNTHESIS,
        produced=(ProductionArtifactStage.REFERENCES, ProductionArtifactStage.EXTRACTION),
        progress=_progress(*SOURCE_IDS),
    )
    world.uow.production_artifacts.items[-1].status = ProductionArtifactStatus.STALE
    await world.service().cancel_run_with_result(world.run.id)

    resumed = await world.service().resume_cancelled_run(world.run.id)

    assert resumed.plan.resume_from_stage is SubjectProductionStage.EXTRACTION
    assert resumed.plan.reused_artifacts == ("references",)


async def test_a_fully_produced_run_replays_only_the_free_assembly() -> None:
    world = _World(
        stage=SubjectProductionStage.ASSEMBLY,
        produced=tuple(ProductionArtifactStage),
        progress=_progress(*SOURCE_IDS),
    )
    await world.service().cancel_run_with_result(world.run.id)

    plan = plan_production_resume(
        world.run,
        artifacts={
            stage.value: await world.uow.production_artifacts.get_current(
                world.run.id, stage.value
            )
            for stage in ProductionArtifactStage
        },
        archived_source_count=len(SOURCE_IDS),
    )

    assert plan.resume_from_stage is SubjectProductionStage.ASSEMBLY
    assert plan.model_calls_expected == 0
    assert plan.reused_artifacts == ("references", "extraction", "synthesis")


async def test_the_log_payload_names_exactly_the_documented_fields() -> None:
    world = _World(
        stage=SubjectProductionStage.SYNTHESIS,
        produced=(ProductionArtifactStage.REFERENCES, ProductionArtifactStage.EXTRACTION),
        progress=_progress(*SOURCE_IDS),
    )
    await world.service().cancel_run_with_result(world.run.id)

    resumed = await world.service().resume_cancelled_run(world.run.id)

    assert resumed.plan.as_log_fields() == {
        "previous_status": "cancelled",
        "resume_from_stage": "synthesis",
        "reused_artifacts": ["references", "extraction"],
        "model_calls_expected": 1,
    }


# --- The Review policy ------------------------------------------------------


async def test_review_offers_resume_and_not_retry_on_a_cancelled_article() -> None:
    assert (
        review_item_can_resume(
            SubjectProductionStatus.CANCELLED,
            reconciliation_required=False,
        )
        is True
    )
    assert (
        review_item_can_retry(
            SubjectProductionStatus.CANCELLED,
            artifact_verified=False,
            reconciliation_required=False,
        )
        is False
    )


@pytest.mark.parametrize(
    "status",
    (
        SubjectProductionStatus.READY,
        SubjectProductionStatus.FAILED,
        SubjectProductionStatus.NEEDS_REVIEW,
        SubjectProductionStatus.RUNNING,
    ),
)
async def test_only_a_cancelled_article_is_offered_a_resume(
    status: SubjectProductionStatus,
) -> None:
    assert review_item_can_resume(status, reconciliation_required=False) is False
