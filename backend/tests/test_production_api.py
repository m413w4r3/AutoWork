"""API-level tests for the production endpoints.

These cover the "nothing started yet" entry point: the UI decides whether to
offer a start button based on a 404, so these endpoints must answer 404 — never
422 from a broken dependency wiring.
"""

from __future__ import annotations

import dataclasses
from collections.abc import AsyncIterator, Sequence
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from cti_app.api.production import router
from cti_app.domain.discovery import SourceRelationshipStatus
from cti_app.domain.editorial import (
    CandidateReference,
    EditorialGroup,
    EditorialGroupStatus,
    EditorialScore,
    EditorialType,
    GroupingConfidence,
    GroupingOutcome,
)
from cti_app.domain.production import (
    EditionProductionBatch,
    EditionProductionBatchItem,
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionArtifactStatus,
    ProductionProfile,
    SubjectProductionRun,
    SubjectProductionStage,
    SubjectProductionStatus,
)


def _score() -> EditorialScore:
    return EditorialScore(
        impact=3,
        novelty=3,
        technical_depth=3,
        hunting_potential=3,
        actionability=3,
        source_quality=3,
        justifications={},
    )


def _group(edition_id: UUID, title: str, subject_id: UUID) -> EditorialGroup:
    group = EditorialGroup(
        edition_id=edition_id,
        title=title,
        candidate_references=(CandidateReference(uuid4(), uuid4()),),
        outcome=GroupingOutcome.NEW_SUBJECT,
        score=_score(),
        source_relationship_status=SourceRelationshipStatus.PROVISIONAL,
        needs_source_verification=False,
        needs_source_expansion=False,
        grouping_confidence=GroupingConfidence.HIGH,
        grouping_justification="test",
    )
    group.select(EditorialType.BRIEF, subject_id)
    return group


class _Groups:
    def __init__(self, groups: list[EditorialGroup]) -> None:
        self._groups = groups

    async def list_for_edition(self, edition_id: UUID) -> Sequence[EditorialGroup]:
        return [g for g in self._groups if g.edition_id == edition_id]

    async def get_by_subject(self, subject_id: UUID) -> EditorialGroup | None:
        return next((g for g in self._groups if g.subject_id == subject_id), None)


class _Runs:
    def __init__(self) -> None:
        self.items: dict[UUID, SubjectProductionRun] = {}

    async def add(self, run: SubjectProductionRun) -> None:
        self.items[run.id] = run

    async def get(self, run_id: UUID) -> SubjectProductionRun | None:
        return self.items.get(run_id)

    async def get_for_update(self, run_id: UUID) -> SubjectProductionRun | None:
        # A real SQL repository reloads a distinct object on every fetch;
        # returning the exact same reference here would let a caller that
        # merely mutates in place look correct by accident. `replace` detaches
        # the returned object so only an explicit `save` makes a change stick.
        run = self.items.get(run_id)
        return dataclasses.replace(run) if run is not None else None

    async def save(self, run: SubjectProductionRun) -> None:
        self.items[run.id] = run

    async def get_current_for_subject(self, subject_id: UUID) -> SubjectProductionRun | None:
        # Mirrors the real repository: most recently created run wins, not
        # insertion order — a retry's new run must shadow the old one.
        matches = [r for r in self.items.values() if r.subject_id == subject_id]
        return max(matches, key=lambda r: r.created_at) if matches else None

    async def list_for_edition(self, edition_id: UUID) -> Sequence[SubjectProductionRun]:
        return [r for r in self.items.values() if r.edition_id == edition_id]


class _Batches:
    def __init__(self) -> None:
        self.items: dict[UUID, EditionProductionBatch] = {}

    async def add(self, batch: EditionProductionBatch) -> None:
        self.items[batch.id] = batch

    async def get(self, batch_id: UUID) -> EditionProductionBatch | None:
        return self.items.get(batch_id)

    async def get_for_update(self, batch_id: UUID) -> EditionProductionBatch | None:
        return self.items.get(batch_id)

    async def save(self, batch: EditionProductionBatch) -> None:
        self.items[batch.id] = batch

    async def get_latest_for_edition(self, edition_id: UUID) -> EditionProductionBatch | None:
        matches = [b for b in self.items.values() if b.edition_id == edition_id]
        return matches[-1] if matches else None

    async def get_active_for_edition(self, edition_id: UUID) -> EditionProductionBatch | None:
        return next(
            (
                b
                for b in self.items.values()
                if b.edition_id == edition_id and b.status in ("queued", "running")
            ),
            None,
        )


class _BatchItems:
    def __init__(self) -> None:
        self.items: list[EditionProductionBatchItem] = []

    async def append_many(self, items: Sequence[EditionProductionBatchItem]) -> None:
        self.items.extend(items)

    async def list_for_batch(self, batch_id: UUID) -> Sequence[EditionProductionBatchItem]:
        return [i for i in self.items if i.batch_id == batch_id]


class _Artifacts:
    def __init__(self) -> None:
        self.items: list[ProductionArtifact] = []

    async def append(self, artifact: ProductionArtifact) -> None:
        self.items.append(artifact)

    async def list_for_run(self, run_id: UUID) -> Sequence[Any]:
        return [artifact for artifact in self.items if artifact.production_run_id == run_id]

    async def get_current(self, run_id: UUID, stage: str) -> ProductionArtifact | None:
        matches = [
            artifact
            for artifact in self.items
            if artifact.production_run_id == run_id
            and artifact.stage.value == stage
            and artifact.status is not ProductionArtifactStatus.STALE
        ]
        return max(matches, key=lambda artifact: artifact.version) if matches else None

    async def mark_downstream_stale(self, run_id: UUID, stage: str) -> None:
        stages = ["references", "extraction", "synthesis", "brief"]
        if stage not in stages:
            return
        downstream = set(stages[stages.index(stage) + 1 :])
        for artifact in self.items:
            if artifact.production_run_id == run_id and artifact.stage.value in downstream:
                artifact.status = ProductionArtifactStatus.STALE

    async def mark_from_stage_stale(self, run_id: UUID, stage: str) -> list[str]:
        """Mirror the SQL repository's selected-stage-plus-downstream semantics."""
        pipeline = ["sources", "references", "extraction", "synthesis", "assembly"]
        artifact_stages = {
            "references": "references",
            "extraction": "extraction",
            "synthesis": "synthesis",
            "assembly": "brief",
        }
        if stage not in pipeline:
            return []
        affected = [
            artifact_stages[item]
            for item in pipeline[pipeline.index(stage) :]
            if item in artifact_stages
        ]
        for artifact in self.items:
            if (
                artifact.production_run_id == run_id
                and artifact.stage.value in affected
                and artifact.status is not ProductionArtifactStatus.STALE
            ):
                artifact.status = ProductionArtifactStatus.STALE
        return affected


class _SourceCollections:
    async def list_for_subject(self, subject_id: UUID) -> Sequence[SimpleNamespace]:
        del subject_id
        return [SimpleNamespace(state=SimpleNamespace(value="archived"))]


class _Uow:
    """Single shared in-memory unit of work; commit is a no-op."""

    def __init__(self, groups: list[EditorialGroup]) -> None:
        self.editorial_groups = _Groups(groups)
        self.subject_production_runs = _Runs()
        self.edition_production_batches = _Batches()
        self.edition_production_batch_items = _BatchItems()
        self.production_artifacts = _Artifacts()
        self.source_collections = _SourceCollections()

    async def __aenter__(self) -> _Uow:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def commit(self) -> None:
        return None

    async def rollback(self) -> None:
        return None


class _Job:
    def __init__(self) -> None:
        self.id = uuid4()


class _Jobs:
    def __init__(self) -> None:
        self.submitted: list[dict[str, Any]] = []

    async def submit(self, **kwargs: Any) -> _Job:
        self.submitted.append(kwargs)
        return _Job()


class _Dispatcher:
    def __init__(self) -> None:
        self.dispatched: list[UUID] = []

    async def dispatch(self, job_id: UUID) -> None:
        self.dispatched.append(job_id)


@pytest.fixture
def uow() -> _Uow:
    return _Uow([])


@pytest.fixture
def production_app(uow: _Uow) -> FastAPI:
    application = FastAPI()
    application.include_router(router)
    application.state.uow_factory = lambda: uow
    application.state.job_service = _Jobs()
    application.state.job_dispatcher = _Dispatcher()
    return application


@pytest.fixture
async def api(production_app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(
        transport=ASGITransport(app=production_app), base_url="http://test"
    ) as client:
        yield client


async def test_get_subject_production_without_run_returns_404(api: AsyncClient) -> None:
    """The UI keys "offer a start button" off this 404 — 422 would break it."""
    response = await api.get(f"/api/subjects/{uuid4()}/production")

    assert response.status_code == 404
    assert response.status_code != 422


async def test_get_edition_briefs_without_batch_returns_404(api: AsyncClient) -> None:
    response = await api.get(f"/api/editions/{uuid4()}/production/briefs")

    assert response.status_code == 404
    assert response.status_code != 422


async def test_start_subject_production_needs_no_edition_id(api: AsyncClient, uow: _Uow) -> None:
    """The subject page only knows the subject id; the edition is resolved server-side."""
    edition_id = uuid4()
    subject_id = uuid4()
    uow.editorial_groups._groups.append(_group(edition_id, "TAG-182", subject_id))

    response = await api.post(f"/api/subjects/{subject_id}/production", json={})

    assert response.status_code == 200, response.text
    assert response.json()["edition_id"] == str(edition_id)


async def test_start_subject_production_returns_the_run_actually_started(
    api: AsyncClient, uow: _Uow, production_app: FastAPI
) -> None:
    """The response must reflect the run start_run() persisted, not the
    QUEUED object create_run() handed back before it was started -- the fake
    repository detaches objects on every fetch, like the real SQL one does,
    so this only passes if the API keeps the object start_run() returns."""
    edition_id = uuid4()
    subject_id = uuid4()
    uow.editorial_groups._groups.append(_group(edition_id, "TAG-182", subject_id))

    response = await api.post(f"/api/subjects/{subject_id}/production", json={})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "running"
    assert body["stage"] == "sources"

    jobs = production_app.state.job_service
    sources_jobs = [job for job in jobs.submitted if job["kind"] == "production.subject.sources"]
    assert len(sources_jobs) == 1
    assert sources_jobs[0]["max_attempts"] == 3


async def test_start_subject_production_rejects_non_selected_subject(
    api: AsyncClient, uow: _Uow
) -> None:
    edition_id = uuid4()
    subject_id = uuid4()
    group = _group(edition_id, "Cavern", subject_id)
    group.status = EditorialGroupStatus.PROPOSED
    uow.editorial_groups._groups.append(group)

    response = await api.post(f"/api/subjects/{subject_id}/production", json={})

    assert response.status_code == 409


async def test_start_edition_briefs_produces_every_selected_brief(
    api: AsyncClient, uow: _Uow
) -> None:
    edition_id = uuid4()
    subjects = [uuid4() for _ in range(3)]
    for name, subject_id in zip(("A", "B", "C"), subjects, strict=True):
        uow.editorial_groups._groups.append(_group(edition_id, name, subject_id))

    response = await api.post(f"/api/editions/{edition_id}/production/briefs", json={})

    assert response.status_code == 200, response.text
    assert response.json()["items"] == 3


async def test_start_edition_briefs_honours_subject_selection(api: AsyncClient, uow: _Uow) -> None:
    edition_id = uuid4()
    subjects = [uuid4() for _ in range(3)]
    for name, subject_id in zip(("A", "B", "C"), subjects, strict=True):
        uow.editorial_groups._groups.append(_group(edition_id, name, subject_id))

    response = await api.post(
        f"/api/editions/{edition_id}/production/briefs",
        json={"subject_ids": [str(subjects[0]), str(subjects[2])]},
    )

    assert response.status_code == 200, response.text
    assert response.json()["items"] == 2

    produced = {item.subject_id for item in uow.edition_production_batch_items.items}
    assert produced == {subjects[0], subjects[2]}


async def test_start_edition_briefs_submits_sources_job_with_standard_retry_policy(
    api: AsyncClient, uow: _Uow, production_app: FastAPI
) -> None:
    edition_id = uuid4()
    subject_id = uuid4()
    uow.editorial_groups._groups.append(_group(edition_id, "TAG-182", subject_id))

    response = await api.post(f"/api/editions/{edition_id}/production/briefs", json={})

    assert response.status_code == 200, response.text
    jobs = production_app.state.job_service
    sources_jobs = [job for job in jobs.submitted if job["kind"] == "production.subject.sources"]
    assert len(sources_jobs) == 1
    assert sources_jobs[0]["max_attempts"] == 3


async def test_start_edition_briefs_rejects_unselected_subject(api: AsyncClient, uow: _Uow) -> None:
    edition_id = uuid4()
    subject_id = uuid4()
    uow.editorial_groups._groups.append(_group(edition_id, "A", subject_id))

    response = await api.post(
        f"/api/editions/{edition_id}/production/briefs",
        json={"subject_ids": [str(uuid4())]},
    )

    assert response.status_code == 409


async def test_batch_creates_exactly_one_run_per_subject(api: AsyncClient, uow: _Uow) -> None:
    """create_batch already builds every run; the endpoint must not add another."""
    edition_id = uuid4()
    subjects = [uuid4() for _ in range(3)]
    for name, subject_id in zip(("A", "B", "C"), subjects, strict=True):
        uow.editorial_groups._groups.append(_group(edition_id, name, subject_id))

    response = await api.post(f"/api/editions/{edition_id}/production/briefs", json={})

    assert response.status_code == 200, response.text
    assert len(uow.subject_production_runs.items) == 3

    # Every batch item must point at a run that actually exists.
    linked = {item.production_run_id for item in uow.edition_production_batch_items.items}
    assert linked == set(uow.subject_production_runs.items)

    running = [
        run
        for run in uow.subject_production_runs.items.values()
        if run.status is SubjectProductionStatus.RUNNING
    ]
    assert len(running) == 1


async def test_second_start_while_running_does_not_reprompt(
    api: AsyncClient, uow: _Uow, production_app: FastAPI
) -> None:
    """A duplicate POST must not start the run again nor submit a second job."""
    edition_id = uuid4()
    subject_id = uuid4()
    uow.editorial_groups._groups.append(_group(edition_id, "TAG-182", subject_id))

    first = await api.post(f"/api/subjects/{subject_id}/production", json={})
    assert first.status_code == 200, first.text

    jobs = production_app.state.job_service
    submitted_after_first = len(jobs.submitted)

    second = await api.post(f"/api/subjects/{subject_id}/production", json={})

    assert second.status_code == 200, second.text
    assert second.json()["run_id"] == first.json()["run_id"]
    assert second.json()["job_id"] is None
    assert len(jobs.submitted) == submitted_after_first
    assert len(uow.subject_production_runs.items) == 1


def _terminal_run(
    edition_id: UUID,
    subject_id: UUID,
    *,
    status: SubjectProductionStatus,
    run_number: int = 1,
) -> SubjectProductionRun:
    run = SubjectProductionRun(
        subject_id=subject_id,
        edition_id=edition_id,
        profile=ProductionProfile.BRIEF_AUTO,
        run_number=run_number,
    )
    run.start_running()
    if status is SubjectProductionStatus.FAILED:
        run.mark_failed(code="model_gateway_error", message="Model run needs reconciliation")
    elif status is SubjectProductionStatus.NEEDS_REVIEW:
        run.mark_needs_review(code="no_model_response", message="No response from model")
    else:  # pragma: no cover - guard against a bad call in a future edit
        raise AssertionError(f"Unsupported terminal status for this helper: {status}")
    return run


@pytest.mark.parametrize(
    "status", (SubjectProductionStatus.FAILED, SubjectProductionStatus.NEEDS_REVIEW)
)
async def test_start_production_after_failure_creates_a_new_run(
    api: AsyncClient,
    uow: _Uow,
    production_app: FastAPI,
    status: SubjectProductionStatus,
) -> None:
    """P23.6 part F: POST /subjects/{id}/production is the retry path for a
    FAILED or NEEDS_REVIEW run -- it must create a brand-new run (new run_id,
    new run_number, its own idempotency keys/conversations) rather than
    reanimate the terminal one, and dispatch a real SOURCES job."""
    edition_id = uuid4()
    subject_id = uuid4()
    uow.editorial_groups._groups.append(_group(edition_id, "TAG-182", subject_id))
    previous = _terminal_run(edition_id, subject_id, status=status)
    await uow.subject_production_runs.add(previous)

    response = await api.post(f"/api/subjects/{subject_id}/production", json={})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["run_id"] != str(previous.id)
    assert body["status"] == "running"
    assert body["stage"] == "sources"

    # The old run is untouched, immutable history.
    assert uow.subject_production_runs.items[previous.id].status is status
    assert uow.subject_production_runs.items[previous.id].id == previous.id

    new_run = uow.subject_production_runs.items[UUID(body["run_id"])]
    assert new_run.run_number == previous.run_number + 1
    assert new_run.status is SubjectProductionStatus.RUNNING
    assert new_run.current_stage is SubjectProductionStage.SOURCES

    jobs = production_app.state.job_service
    sources_jobs = [job for job in jobs.submitted if job["kind"] == "production.subject.sources"]
    assert len(sources_jobs) == 1
    assert sources_jobs[0]["idempotency_key"] == f"production-sources-{new_run.id}-g0"

    dispatcher = production_app.state.job_dispatcher
    assert len(dispatcher.dispatched) == 1


async def test_save_brief_draft_appends_artifact_versions(api: AsyncClient, uow: _Uow) -> None:
    edition_id = uuid4()
    subject_id = uuid4()
    run = SubjectProductionRun(
        subject_id=subject_id,
        edition_id=edition_id,
        profile=ProductionProfile.BRIEF_AUTO,
    )
    await uow.subject_production_runs.add(run)

    first = await api.post(
        f"/api/subjects/{subject_id}/production/brief/draft",
        json={"content": "première version"},
    )
    second = await api.post(
        f"/api/subjects/{subject_id}/production/brief/draft",
        json={"content": "seconde version"},
    )

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert [artifact.version for artifact in uow.production_artifacts.items] == [1, 2]
    assert second.json()["draft_version"] == 2


def _ready_run(edition_id: UUID, subject_id: UUID, *, run_number: int = 1) -> SubjectProductionRun:
    run = SubjectProductionRun(
        subject_id=subject_id,
        edition_id=edition_id,
        profile=ProductionProfile.BRIEF_AUTO,
        run_number=run_number,
    )
    run.start_running()
    run.mark_ready()
    return run


def _artifact(run: SubjectProductionRun, stage: ProductionArtifactStage) -> ProductionArtifact:
    return ProductionArtifact(
        production_run_id=run.id,
        subject_id=run.subject_id,
        stage=stage,
        version=1,
        input_hash="a" * 64,
    )


@pytest.mark.parametrize(
    ("initial_status", "stage", "expected_stale"),
    (
        (
            SubjectProductionStatus.READY,
            SubjectProductionStage.SOURCES,
            ["references", "extraction", "synthesis", "brief"],
        ),
        (
            SubjectProductionStatus.READY,
            SubjectProductionStage.REFERENCES,
            ["references", "extraction", "synthesis", "brief"],
        ),
        (
            SubjectProductionStatus.READY,
            SubjectProductionStage.EXTRACTION,
            ["extraction", "synthesis", "brief"],
        ),
        (SubjectProductionStatus.READY, SubjectProductionStage.SYNTHESIS, ["synthesis", "brief"]),
        (SubjectProductionStatus.READY, SubjectProductionStage.ASSEMBLY, ["brief"]),
        (
            SubjectProductionStatus.FAILED,
            SubjectProductionStage.EXTRACTION,
            ["extraction", "synthesis", "brief"],
        ),
        (
            SubjectProductionStatus.NEEDS_REVIEW,
            SubjectProductionStage.EXTRACTION,
            ["extraction", "synthesis", "brief"],
        ),
    ),
)
async def test_retry_stage_reuses_run_and_stales_selected_stage_and_downstream(
    api: AsyncClient,
    uow: _Uow,
    production_app: FastAPI,
    initial_status: SubjectProductionStatus,
    stage: SubjectProductionStage,
    expected_stale: list[str],
) -> None:
    edition_id, subject_id = uuid4(), uuid4()
    run = SubjectProductionRun(
        subject_id=subject_id, edition_id=edition_id, profile=ProductionProfile.BRIEF_AUTO
    )
    run.start_running()
    run.current_stage = SubjectProductionStage.ASSEMBLY
    if initial_status is SubjectProductionStatus.READY:
        run.mark_ready()
    elif initial_status is SubjectProductionStatus.FAILED:
        run.mark_failed(code="extraction_failed", message="failed")
    else:
        run.mark_needs_review(code="extraction_review", message="review")
    await uow.subject_production_runs.add(run)
    for artifact_stage in ProductionArtifactStage:
        await uow.production_artifacts.append(_artifact(run, artifact_stage))

    response = await api.post(
        f"/api/subjects/{subject_id}/production/retry", json={"stage": stage.value}
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["run_id"] == str(run.id)
    assert body["pipeline_generation"] == 1
    persisted = uow.subject_production_runs.items[run.id]
    assert persisted.current_stage is stage
    assert persisted.status is SubjectProductionStatus.RUNNING
    assert body["staled_artifacts"] == expected_stale
    stale = {
        item.stage.value
        for item in uow.production_artifacts.items
        if item.status is ProductionArtifactStatus.STALE
    }
    assert stale == set(expected_stale)
    assert all(
        item.status is ProductionArtifactStatus.VERIFIED
        for item in uow.production_artifacts.items
        if item.stage.value not in expected_stale
    )
    job = production_app.state.job_service.submitted[-1]
    expected_kind = (
        "production.subject.assemble"
        if stage is SubjectProductionStage.ASSEMBLY
        else f"production.subject.{stage.value}"
    )
    assert job["kind"] == expected_kind
    assert job["idempotency_key"] == f"production-{stage.value}-{run.id}-g1"
    assert job["max_attempts"] == 3


@pytest.mark.parametrize(
    "status", (SubjectProductionStatus.QUEUED, SubjectProductionStatus.RUNNING)
)
async def test_retry_stage_rejects_queued_or_running_run(
    api: AsyncClient, uow: _Uow, status: SubjectProductionStatus
) -> None:
    run = SubjectProductionRun(
        subject_id=uuid4(), edition_id=uuid4(), profile=ProductionProfile.BRIEF_AUTO
    )
    if status is SubjectProductionStatus.RUNNING:
        run.start_running()
    await uow.subject_production_runs.add(run)

    response = await api.post(
        f"/api/subjects/{run.subject_id}/production/retry", json={"stage": "extraction"}
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "retry_not_allowed_while_running"
