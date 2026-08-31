"""API-level tests for the production endpoints.

These cover the "nothing started yet" entry point: the UI decides whether to
offer a start button based on a 404, so these endpoints must answer 404 — never
422 from a broken dependency wiring.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from cti_app.api.production import router
from cti_app.application.identity import LocalIdentityProvider
from cti_app.application.production_parsers import technical_extraction_from_json
from cti_app.application.production_read_model import BatchStatusItem
from cti_app.application.production_state import (
    ProductionStateSnapshotV1,
    compute_production_state_checksum,
)
from cti_app.domain.collection import CollectionState
from cti_app.domain.discovery import SourceRelationshipStatus
from cti_app.domain.editorial import (
    CandidateReference,
    EditorialGroup,
    EditorialGroupStatus,
    EditorialScore,
    GroupingConfidence,
    GroupingOutcome,
)
from cti_app.domain.production import (
    AnalystInputPack,
    AnalystInvestigation,
    EditionProductionBatch,
    EditionProductionBatchItem,
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionArtifactStatus,
    ProductionBatchPhase,
    ProductionReuseInvalidation,
    SubjectProductionRun,
    SubjectProductionStage,
    SubjectProductionStatus,
)
from cti_app.logging import CorrelationIdMiddleware


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
    group.select(subject_id)
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


class _BatchStatusReadModel:
    """In-memory port double; one call represents one set-based read."""

    def __init__(self, uow: Any) -> None:
        self._uow = uow
        self.calls = 0
        self.snapshots: dict[UUID, SimpleNamespace] = {}

    async def list_for_batch(self, batch_id: UUID) -> Sequence[BatchStatusItem]:
        self.calls += 1
        items = [
            item
            for item in self._uow.edition_production_batch_items.items
            if item.batch_id == batch_id
        ]
        runs = self._uow.subject_production_runs.items
        groups = {group.subject_id: group for group in self._uow.editorial_groups._groups}
        result: list[BatchStatusItem] = []
        for item in items:
            run = runs.get(item.production_run_id)
            if run is None:
                continue
            group = groups.get(item.subject_id)
            snapshot = self.snapshots.get(run.id)
            result.append(
                BatchStatusItem(
                    position=item.position,
                    subject_id=item.subject_id,
                    title=(
                        snapshot.subject_title
                        if snapshot is not None
                        else group.title
                        if group is not None
                        else str(item.subject_id)
                    ),
                    run_id=run.id,
                    status=run.status,
                    current_stage=run.current_stage,
                    pipeline_generation=run.pipeline_generation,
                    auto_recovery_count=item.auto_recovery_count,
                    error_code=run.error_code,
                    error_message=run.error_message,
                )
            )
        return result


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
        stages = ["references", "extraction", "synthesis", "publication"]
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
            "assembly": "publication",
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
        return [SimpleNamespace(state=CollectionState.ARCHIVED)]


class _Investigations:
    def __init__(self) -> None:
        self.items: dict[UUID, AnalystInvestigation] = {}

    async def get(self, investigation_id: UUID) -> AnalystInvestigation | None:
        return self.items.get(investigation_id)

    async def get_for_run(self, run_id: UUID) -> AnalystInvestigation | None:
        return next(
            (item for item in self.items.values() if item.production_run_id == run_id), None
        )

    async def add(self, investigation: AnalystInvestigation) -> None:
        self.items[investigation.id] = investigation

    async def save(self, investigation: AnalystInvestigation) -> None:
        self.items[investigation.id] = investigation


class _InputPacks:
    def __init__(self) -> None:
        self.items: dict[UUID, AnalystInputPack] = {}

    async def get_for_investigation(self, investigation_id: UUID) -> AnalystInputPack | None:
        return next(
            (item for item in self.items.values() if item.investigation_id == investigation_id),
            None,
        )

    async def append(self, pack: AnalystInputPack) -> None:
        self.items[pack.id] = pack


class _ReuseInvalidations:
    def __init__(self) -> None:
        self.items: list[ProductionReuseInvalidation] = []

    async def add(self, invalidation: ProductionReuseInvalidation) -> None:
        self.items.append(invalidation)

    async def list_for_subject(
        self, edition_id: UUID, subject_id: UUID
    ) -> Sequence[ProductionReuseInvalidation]:
        return [
            item
            for item in self.items
            if item.edition_id == edition_id and item.subject_id == subject_id
        ]


class _Uow:
    """Single shared in-memory unit of work; commit is a no-op."""

    def __init__(self, groups: list[EditorialGroup]) -> None:
        self.editorial_groups = _Groups(groups)
        self.subject_production_runs = _Runs()
        self.edition_production_batches = _Batches()
        self.edition_production_batch_items = _BatchItems()
        self.production_artifacts = _Artifacts()
        self.batch_status_read_model = _BatchStatusReadModel(self)
        self.source_collections = _SourceCollections()
        self.analyst_investigations = _Investigations()
        self.analyst_input_packs = _InputPacks()
        self.production_reuse_invalidations = _ReuseInvalidations()

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


class _TrackedJob:
    def __init__(self, *, subject_id: UUID, run_id: UUID) -> None:
        self.id = uuid4()
        self.subject_id = subject_id
        self.input_parameters = {"run_id": str(run_id)}
        self.status = "running"

    @property
    def is_terminal(self) -> bool:
        return self.status == "cancelled"


class _CancelableJobs:
    def __init__(self) -> None:
        self.submitted: list[dict[str, Any]] = []
        self.jobs: list[_TrackedJob] = []
        self.cancelled: list[UUID] = []

    async def submit(self, **kwargs: Any) -> _TrackedJob:
        job = _TrackedJob(
            subject_id=kwargs["aggregate_id"],
            run_id=UUID(kwargs["input_parameters"]["run_id"]),
        )
        self.submitted.append(kwargs)
        self.jobs.append(job)
        return job

    async def list_for_aggregate(
        self, aggregate_type: str, aggregate_id: UUID
    ) -> list[_TrackedJob]:
        assert aggregate_type == "subject"
        return [job for job in self.jobs if job.subject_id == aggregate_id]

    async def cancel(self, job_id: UUID, *, actor_id: str = "system") -> _TrackedJob:
        del actor_id
        job = next(job for job in self.jobs if job.id == job_id)
        job.status = "cancelled"
        self.cancelled.append(job.id)
        return job


class _Dispatcher:
    def __init__(self) -> None:
        self.dispatched: list[UUID] = []
        self.delays: list[int] = []

    async def dispatch(self, job_id: UUID, *, delay_ms: int = 0) -> None:
        self.dispatched.append(job_id)
        self.delays.append(delay_ms)


class _FailingModel:
    """Import must never reach a model service or gateway."""

    def __getattr__(self, name: str) -> object:
        raise AssertionError(f"model must not be called during state import: {name}")


class _ArtifactStore:
    def __init__(self) -> None:
        self.payloads: dict[UUID, object] = {}

    async def store_stage_payloads(
        self,
        *,
        raw: str | None = None,
        canonical: dict[str, Any] | None = None,
        rendered: str | None = None,
    ) -> tuple[UUID | None, UUID | None, UUID | None]:
        async def save(value: object | None) -> UUID | None:
            if value is None:
                return None
            blob_id = uuid4()
            self.payloads[blob_id] = value
            return blob_id

        return await save(raw), await save(canonical), await save(rendered)

    async def read_json(self, blob_id: UUID) -> dict[str, Any]:
        value = self.payloads[blob_id]
        assert isinstance(value, dict)
        return value

    async def read_text(self, blob_id: UUID) -> str:
        value = self.payloads[blob_id]
        assert isinstance(value, str)
        return value

    async def put_canonical_json(self, payload: dict[str, Any], *, bucket: str) -> tuple[UUID, str]:
        del bucket
        encoded = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
        blob_id = uuid4()
        self.payloads[blob_id] = payload
        return blob_id, hashlib.sha256(encoded).hexdigest()


@pytest.fixture
def uow() -> _Uow:
    return _Uow([])


@pytest.fixture
def production_app(uow: _Uow) -> FastAPI:
    application = FastAPI()
    application.add_middleware(CorrelationIdMiddleware)
    application.include_router(router)
    application.state.uow_factory = lambda: uow
    application.state.job_service = _Jobs()
    application.state.job_dispatcher = _Dispatcher()
    application.state.identity_provider = LocalIdentityProvider()
    application.state.production_artifact_store = _ArtifactStore()
    application.state.model_service = _FailingModel()
    application.state.model_gateway = _FailingModel()
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


async def test_get_edition_production_without_batch_returns_404(api: AsyncClient) -> None:
    response = await api.get(f"/api/editions/{uuid4()}/production")

    assert response.status_code == 404
    assert response.status_code != 422


async def test_invalidate_reuse_without_run_returns_404(api: AsyncClient) -> None:
    response = await api.post(
        f"/api/subjects/{uuid4()}/production/reuse/invalidate",
        json={"from_stage": "references"},
    )

    assert response.status_code == 404


async def test_invalidate_reuse_rejects_active_run(api: AsyncClient, uow: _Uow) -> None:
    edition_id, subject_id = uuid4(), uuid4()
    uow.editorial_groups._groups.append(_group(edition_id, "Active", subject_id))
    await uow.subject_production_runs.add(
        SubjectProductionRun(subject_id=subject_id, edition_id=edition_id)
    )

    response = await api.post(
        f"/api/subjects/{subject_id}/production/reuse/invalidate",
        json={"from_stage": "references"},
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "reuse_invalidation_run_active"


@pytest.mark.parametrize("from_stage", ["sources", "assembly"])
async def test_invalidate_reuse_rejects_non_costly_stage(
    api: AsyncClient, uow: _Uow, from_stage: str
) -> None:
    edition_id, subject_id = uuid4(), uuid4()
    uow.editorial_groups._groups.append(_group(edition_id, "Rejected", subject_id))
    await uow.subject_production_runs.add(
        _terminal_run(edition_id, subject_id, status=SubjectProductionStatus.NEEDS_REVIEW)
    )

    response = await api.post(
        f"/api/subjects/{subject_id}/production/reuse/invalidate",
        json={"from_stage": from_stage},
    )

    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "reuse_invalidation_stage_not_allowed"
    assert uow.production_reuse_invalidations.items == []


@pytest.mark.parametrize("from_stage", ["references", "extraction", "synthesis"])
async def test_invalidate_reuse_persists_identity_from_provider(
    api: AsyncClient,
    uow: _Uow,
    production_app: FastAPI,
    from_stage: str,
) -> None:
    edition_id, subject_id = uuid4(), uuid4()
    uow.editorial_groups._groups.append(_group(edition_id, "Accepted", subject_id))
    run = _terminal_run(edition_id, subject_id, status=SubjectProductionStatus.NEEDS_REVIEW)
    await uow.subject_production_runs.add(run)
    production_app.state.identity_provider = LocalIdentityProvider("provider-user")

    response = await api.post(
        f"/api/subjects/{subject_id}/production/reuse/invalidate",
        json={"from_stage": from_stage, "actor_id": "spoofed-body-user"},
        headers={"X-Correlation-ID": "reuse-api-correlation"},
    )

    assert response.status_code == 200, response.text
    persisted = uow.production_reuse_invalidations.items
    assert len(persisted) == 1
    invalidation = persisted[0]
    assert invalidation.edition_id == edition_id
    assert invalidation.subject_id == subject_id
    assert invalidation.from_stage.value == from_stage
    assert invalidation.actor_id == "provider-user"
    assert invalidation.correlation_id == "reuse-api-correlation"
    assert invalidation.occurred_at.tzinfo is not None


async def test_start_subject_production_needs_no_edition_id(api: AsyncClient, uow: _Uow) -> None:
    """The subject page only knows the subject id; the edition is resolved server-side."""
    edition_id = uuid4()
    subject_id = uuid4()
    uow.editorial_groups._groups.append(_group(edition_id, "TAG-182", subject_id))

    response = await api.post(f"/api/subjects/{subject_id}/production", json={})

    assert response.status_code == 200, response.text
    assert response.json()["edition_id"] == str(edition_id)


async def test_start_subject_production_ignores_spoofed_user_query_parameter(
    api: AsyncClient, uow: _Uow, production_app: FastAPI
) -> None:
    edition_id = uuid4()
    subject_id = uuid4()
    uow.editorial_groups._groups.append(_group(edition_id, "Identity", subject_id))
    production_app.state.identity_provider = LocalIdentityProvider("real-user")

    response = await api.post(
        f"/api/subjects/{subject_id}/production?user=administrator",
        json={},
    )

    assert response.status_code == 200, response.text
    assert production_app.state.job_service.submitted[-1]["actor_id"] == "real-user"


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


async def test_start_edition_produces_every_selected_article(api: AsyncClient, uow: _Uow) -> None:
    edition_id = uuid4()
    subjects = [uuid4() for _ in range(3)]
    for name, subject_id in zip(("A", "B", "C"), subjects, strict=True):
        uow.editorial_groups._groups.append(_group(edition_id, name, subject_id))

    response = await api.post(f"/api/editions/{edition_id}/production", json={})

    assert response.status_code == 200, response.text
    assert response.json()["items"] == 3


async def test_start_edition_honours_subject_selection(api: AsyncClient, uow: _Uow) -> None:
    edition_id = uuid4()
    subjects = [uuid4() for _ in range(3)]
    for name, subject_id in zip(("A", "B", "C"), subjects, strict=True):
        uow.editorial_groups._groups.append(_group(edition_id, name, subject_id))

    response = await api.post(
        f"/api/editions/{edition_id}/production",
        json={"subject_ids": [str(subjects[0]), str(subjects[2])]},
    )

    assert response.status_code == 200, response.text
    assert response.json()["items"] == 2

    produced = {item.subject_id for item in uow.edition_production_batch_items.items}
    assert produced == {subjects[0], subjects[2]}


async def test_start_edition_with_more_eligible_than_selected_runs_only_the_chosen_subset(
    api: AsyncClient, uow: _Uow, production_app: FastAPI
) -> None:
    """Reproduces the real operator scenario: 4 editorially eligible articles
    (A/B/C/D), request only B and D. The batch must contain exactly B and D
    at positions 1 and 2; A and C must receive no run and no dispatched job."""
    edition_id = uuid4()
    subject_a, subject_b, subject_c, subject_d = (uuid4() for _ in range(4))
    for name, subject_id in zip(
        ("A", "B", "C", "D"),
        (subject_a, subject_b, subject_c, subject_d),
        strict=True,
    ):
        uow.editorial_groups._groups.append(_group(edition_id, name, subject_id))

    response = await api.post(
        f"/api/editions/{edition_id}/production",
        json={"subject_ids": [str(subject_b), str(subject_d)]},
    )

    assert response.status_code == 200, response.text
    assert response.json()["items"] == 2

    items = sorted(uow.edition_production_batch_items.items, key=lambda item: item.position)
    assert [item.subject_id for item in items] == [subject_b, subject_d]
    assert [item.position for item in items] == [1, 2]

    produced_subjects = {run.subject_id for run in uow.subject_production_runs.items.values()}
    assert produced_subjects == {subject_b, subject_d}
    assert subject_a not in produced_subjects
    assert subject_c not in produced_subjects

    jobs = production_app.state.job_service
    sources_jobs = [job for job in jobs.submitted if job["kind"] == "production.subject.sources"]
    assert len(sources_jobs) == 1
    assert sources_jobs[0]["aggregate_id"] == subject_b


async def test_start_edition_submits_sources_job_with_standard_retry_policy(
    api: AsyncClient, uow: _Uow, production_app: FastAPI
) -> None:
    edition_id = uuid4()
    subject_id = uuid4()
    uow.editorial_groups._groups.append(_group(edition_id, "TAG-182", subject_id))

    response = await api.post(f"/api/editions/{edition_id}/production", json={})

    assert response.status_code == 200, response.text
    jobs = production_app.state.job_service
    sources_jobs = [job for job in jobs.submitted if job["kind"] == "production.subject.sources"]
    assert len(sources_jobs) == 1
    assert sources_jobs[0]["max_attempts"] == 3


async def test_batch_status_exposes_phase_schedule_and_item_error_details(
    api: AsyncClient, uow: _Uow
) -> None:
    edition_id = uuid4()
    subject_id = uuid4()
    uow.editorial_groups._groups.append(_group(edition_id, "TAG-182", subject_id))
    started = await api.post(f"/api/editions/{edition_id}/production", json={})
    assert started.status_code == 200, started.text

    batch = next(iter(uow.edition_production_batches.items.values()))
    batch.phase = ProductionBatchPhase.RECOVERY
    batch.next_dispatch_at = datetime.now(UTC)
    item = uow.edition_production_batch_items.items[0]
    item.auto_recovery_count = 1
    run = uow.subject_production_runs.items[item.production_run_id]
    run.pipeline_generation = 2
    run.mark_failed(code="bridge_timeout", message="bridge stopped")

    response = await api.get(f"/api/editions/{edition_id}/production")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["phase"] == "recovery"
    assert body["next_dispatch_at"] is not None
    assert body["item_details"][0]["auto_recovery_count"] == 1
    assert body["item_details"][0]["pipeline_generation"] == 2
    assert body["item_details"][0]["error_code"] == "bridge_timeout"
    assert body["item_details"][0]["error_message"] == "bridge stopped"
    assert "error_details" not in body["item_details"][0]


async def test_batch_status_read_model_returns_set_based_rows_and_snapshot_titles(
    api: AsyncClient, uow: _Uow
) -> None:
    edition_id = uuid4()
    subjects = [uuid4() for _ in range(3)]
    for name, subject_id in zip(("A", "B", "C"), subjects, strict=True):
        uow.editorial_groups._groups.append(_group(edition_id, name, subject_id))

    started = await api.post(f"/api/editions/{edition_id}/production", json={})
    assert started.status_code == 200, started.text
    uow.batch_status_read_model.calls = 0
    batch_items = uow.edition_production_batch_items.items
    first_run = uow.subject_production_runs.items[batch_items[0].production_run_id]
    first_run.pipeline_generation = 3
    uow.batch_status_read_model.snapshots[first_run.id] = SimpleNamespace(
        subject_title="Snapshot title"
    )

    response = await api.get(f"/api/editions/{edition_id}/production")

    assert response.status_code == 200, response.text
    details = response.json()["item_details"]
    assert [detail["position"] for detail in details] == [1, 2, 3]
    assert details[0]["title"] == "Snapshot title"
    assert details[0]["pipeline_generation"] == 3
    assert uow.batch_status_read_model.calls == 1


async def test_start_edition_rejects_unselected_subject(api: AsyncClient, uow: _Uow) -> None:
    edition_id = uuid4()
    subject_id = uuid4()
    uow.editorial_groups._groups.append(_group(edition_id, "A", subject_id))

    response = await api.post(
        f"/api/editions/{edition_id}/production",
        json={"subject_ids": [str(uuid4())]},
    )

    assert response.status_code == 409


async def test_batch_creates_exactly_one_run_per_subject(api: AsyncClient, uow: _Uow) -> None:
    """create_batch already builds every run; the endpoint must not add another."""
    edition_id = uuid4()
    subjects = [uuid4() for _ in range(3)]
    for name, subject_id in zip(("A", "B", "C"), subjects, strict=True):
        uow.editorial_groups._groups.append(_group(edition_id, name, subject_id))

    response = await api.post(f"/api/editions/{edition_id}/production", json={})

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


def _ready_run(edition_id: UUID, subject_id: UUID, *, run_number: int = 1) -> SubjectProductionRun:
    run = SubjectProductionRun(
        subject_id=subject_id,
        edition_id=edition_id,
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


def _state_payload() -> dict[str, Any]:
    payload: dict[str, Any] = {
        "format": "autowork.production-state",
        "schema_version": 1,
        "exported_at": "2026-08-26T15:00:00Z",
        "origin": {
            "subject_title": "TAG-182",
            "editorial_type": "brief",
            "profile": "brief_auto",
            "research_date": "2026-08-26",
        },
        "artifacts": {
            "references": {
                "input_hash": "a" * 64,
                "canonical_content": {
                    "sources": [
                        {
                            "id": "S1",
                            "title": "Source",
                            "url": "https://example.test/source",
                            "canonical_url": "https://example.test/source",
                        }
                    ],
                    "events": [],
                },
            },
            "extraction": {
                "input_hash": "b" * 64,
                "canonical_content": {
                    "schema_version": "2",
                    "parser_version": "production-markdown-v2",
                    "items": [
                        {
                            "id": "I1",
                            "category": "infrastructure",
                            "value": "evil.example",
                            "context": "observed",
                            "artifact_type": "domain",
                            "semantic_type": "indicator",
                            "indicator_status": "confirmed_ioc",
                            "provenance": "source",
                            "display_policy": "ioc_section",
                            "normalized_value": "evil.example",
                            "evidence_quote": "evil.example",
                            "attack_id": None,
                            "reference_ids": [],
                            "source_ids": ["S1"],
                            "supported": True,
                        }
                    ],
                    "uncertainties": [],
                },
            },
            "synthesis": {"input_hash": "c" * 64, "rendered_content": "Fait [S1]"},
        },
        "content_sha256": "0" * 64,
    }
    snapshot = ProductionStateSnapshotV1.model_validate(payload)
    payload["content_sha256"] = compute_production_state_checksum(snapshot)
    return payload


async def _seed_exportable_run(
    uow: _Uow, store: _ArtifactStore, edition_id: UUID, subject_id: UUID
) -> SubjectProductionRun:
    run = _terminal_run(edition_id, subject_id, status=SubjectProductionStatus.NEEDS_REVIEW)
    run.current_stage = SubjectProductionStage.ASSEMBLY
    await uow.subject_production_runs.add(run)
    payload = _state_payload()
    artifacts = payload["artifacts"]
    assert isinstance(artifacts, dict)
    refs = artifacts["references"]
    extraction = artifacts["extraction"]
    synthesis = artifacts["synthesis"]
    assert isinstance(refs, dict) and isinstance(extraction, dict) and isinstance(synthesis, dict)
    _, refs_blob, _ = await store.store_stage_payloads(canonical=refs["canonical_content"])
    extraction_raw, extraction_blob, _ = await store.store_stage_payloads(
        raw="SECRET_RAW_MODEL_OUTPUT_SENTINEL", canonical=extraction["canonical_content"]
    )
    _, _, synthesis_blob = await store.store_stage_payloads(rendered=synthesis["rendered_content"])
    for stage, input_hash, canonical_blob_id, rendered_blob_id in (
        (ProductionArtifactStage.REFERENCES, refs["input_hash"], refs_blob, None),
        (ProductionArtifactStage.EXTRACTION, extraction["input_hash"], extraction_blob, None),
        (ProductionArtifactStage.SYNTHESIS, synthesis["input_hash"], None, synthesis_blob),
    ):
        await uow.production_artifacts.append(
            ProductionArtifact(
                production_run_id=run.id,
                subject_id=subject_id,
                stage=stage,
                version=1,
                input_hash=input_hash,
                canonical_blob_id=canonical_blob_id,
                rendered_blob_id=rendered_blob_id,
                raw_blob_id=extraction_raw if stage is ProductionArtifactStage.EXTRACTION else None,
            )
        )
    return run


async def test_production_state_export_import_is_transparent(
    api: AsyncClient, uow: _Uow, production_app: FastAPI
) -> None:
    edition_id, subject_id = uuid4(), uuid4()
    uow.editorial_groups._groups.append(_group(edition_id, "TAG-182", subject_id))
    store = production_app.state.production_artifact_store
    run = await _seed_exportable_run(uow, store, edition_id, subject_id)

    exported = await api.get(f"/api/subjects/{subject_id}/production/state/export")
    assert exported.status_code == 200, exported.text
    snapshot = exported.json()
    assert snapshot["format"] == "autowork.production-state"
    assert snapshot["schema_version"] == 2
    assert snapshot["content_sha256"]
    assert snapshot["artifacts"]["references"]["canonical_content"]["sources"][0]["id"] == "S1"
    assert (
        snapshot["artifacts"]["extraction"]["canonical_content"]["items"][0]["value"]
        == "evil.example"
    )
    assert snapshot["artifacts"]["synthesis"]["rendered_content"] == "Fait [S1]"

    imported_subject = uuid4()
    uow.editorial_groups._groups.append(_group(edition_id, "TAG-182", imported_subject))
    submitted = len(production_app.state.job_service.submitted)
    imported = await api.post(
        f"/api/subjects/{imported_subject}/production/state/import", json=snapshot
    )
    assert imported.status_code == 200, imported.text
    assert imported.json()["status"] == "needs_review"
    assert imported.json()["current_stage"] == "assembly"
    assert imported.json()["imported_stages"] == ["references", "extraction", "synthesis"]
    assert len(production_app.state.job_service.submitted) == submitted
    assert not uow.analyst_investigations.items
    assert not uow.analyst_input_packs.items

    production = await api.get(f"/api/subjects/{imported_subject}/production")
    assert production.json()["status"] == "needs_review"
    assert production.json()["current_stage"] == "assembly"
    assert production.json()["stages"]["references"]["status"] == "succeeded"
    assert production.json()["stages"]["extraction"]["status"] == "succeeded"
    assert production.json()["stages"]["synthesis"]["status"] == "succeeded"
    assert production.json()["stages"]["assembly"]["status"] == "needs_review"
    imported_artifacts: dict[str, dict[str, Any]] = {}
    for stage in ("references", "extraction", "synthesis"):
        artifact = await api.get(f"/api/subjects/{imported_subject}/production/artifacts/{stage}")
        assert artifact.json()["stage"] == stage
        assert artifact.json()["status"] == "verified"
        imported_artifacts[stage] = artifact.json()
    assert (
        imported_artifacts["references"]["canonical_content"]
        == snapshot["artifacts"]["references"]["canonical_content"]
    )
    assert (
        imported_artifacts["extraction"]["canonical_content"]
        == snapshot["artifacts"]["extraction"]["canonical_content"]
    )
    assert (
        imported_artifacts["synthesis"]["rendered_content"]
        == snapshot["artifacts"]["synthesis"]["rendered_content"]
    )
    assert run.id != UUID(imported.json()["run_id"])

    imported_run = await uow.subject_production_runs.get_current_for_subject(imported_subject)
    assert imported_run is not None
    extraction_artifact = await uow.production_artifacts.get_current(imported_run.id, "extraction")
    assert extraction_artifact is not None and extraction_artifact.canonical_blob_id is not None
    restored = await store.read_json(extraction_artifact.canonical_blob_id)
    extraction_document = technical_extraction_from_json(restored)
    assert [item.value for item in extraction_document.items] == ["evil.example"]
    assert extraction_document.items[0].supported is True


async def test_production_state_import_has_no_generation_side_effects(
    api: AsyncClient, uow: _Uow, production_app: FastAPI
) -> None:
    edition_id, source_id, target_id = uuid4(), uuid4(), uuid4()
    uow.editorial_groups._groups.extend(
        [_group(edition_id, "Source", source_id), _group(edition_id, "Target", target_id)]
    )
    await _seed_exportable_run(
        uow, production_app.state.production_artifact_store, edition_id, source_id
    )
    snapshot = (await api.get(f"/api/subjects/{source_id}/production/state/export")).json()
    jobs = production_app.state.job_service
    before_jobs = len(jobs.submitted)
    before_runs = len(uow.subject_production_runs.items)
    response = await api.post(f"/api/subjects/{target_id}/production/state/import", json=snapshot)
    assert response.status_code == 200
    assert len(jobs.submitted) == before_jobs
    assert len(uow.subject_production_runs.items) == before_runs + 1


async def test_production_state_export_import_export_preserves_business_content(
    api: AsyncClient, uow: _Uow, production_app: FastAPI
) -> None:
    edition_id, source_id, target_id = uuid4(), uuid4(), uuid4()
    uow.editorial_groups._groups.extend(
        [_group(edition_id, "Source", source_id), _group(edition_id, "Target", target_id)]
    )
    await _seed_exportable_run(
        uow, production_app.state.production_artifact_store, edition_id, source_id
    )
    first = (await api.get(f"/api/subjects/{source_id}/production/state/export")).json()
    assert (
        await api.post(f"/api/subjects/{target_id}/production/state/import", json=first)
    ).status_code == 200
    second = (await api.get(f"/api/subjects/{target_id}/production/state/export")).json()
    for stage, field in (
        ("references", "canonical_content"),
        ("extraction", "canonical_content"),
        ("synthesis", "rendered_content"),
    ):
        assert second["artifacts"][stage][field] == first["artifacts"][stage][field]


async def test_production_state_import_keeps_history_and_previous_artifacts(
    api: AsyncClient, uow: _Uow, production_app: FastAPI
) -> None:
    edition_id, subject_id = uuid4(), uuid4()
    uow.editorial_groups._groups.append(_group(edition_id, "Subject", subject_id))
    original = await _seed_exportable_run(
        uow, production_app.state.production_artifact_store, edition_id, subject_id
    )
    snapshot = (await api.get(f"/api/subjects/{subject_id}/production/state/export")).json()
    imported_ids: list[UUID] = []
    for _ in range(2):
        response = await api.post(
            f"/api/subjects/{subject_id}/production/state/import", json=snapshot
        )
        assert response.status_code == 200
        imported_ids.append(UUID(response.json()["run_id"]))
    assert len(uow.subject_production_runs.items) == 3
    assert (
        await uow.subject_production_runs.get_current_for_subject(subject_id)
        == uow.subject_production_runs.items[imported_ids[-1]]
    )
    for run_id in imported_ids:
        artifacts = await uow.production_artifacts.list_for_run(run_id)
        assert len(artifacts) == 3
        assert all(artifact.status is ProductionArtifactStatus.VERIFIED for artifact in artifacts)
    original_artifacts = await uow.production_artifacts.list_for_run(original.id)
    assert all(
        artifact.status is not ProductionArtifactStatus.STALE for artifact in original_artifacts
    )


async def test_production_state_export_excludes_foreign_ids_and_raw_output(
    api: AsyncClient, uow: _Uow, production_app: FastAPI
) -> None:
    edition_id, subject_id = uuid4(), uuid4()
    uow.editorial_groups._groups.append(_group(edition_id, "Subject", subject_id))
    run = await _seed_exportable_run(
        uow, production_app.state.production_artifact_store, edition_id, subject_id
    )
    extraction = next(
        a
        for a in uow.production_artifacts.items
        if a.production_run_id == run.id and a.stage is ProductionArtifactStage.EXTRACTION
    )
    extraction_content = production_app.state.production_artifact_store.payloads[
        extraction.canonical_blob_id
    ]
    assert isinstance(extraction_content, dict)
    extraction_content["items"][0]["model_run_ids"] = [str(uuid4())]
    snapshot = (await api.get(f"/api/subjects/{subject_id}/production/state/export")).json()
    serialized = json.dumps(snapshot)
    forbidden = [
        str(run.id),
        *(str(a.id) for a in uow.production_artifacts.items if a.production_run_id == run.id),
    ]
    forbidden.extend(
        str(value) for value in (extraction.canonical_blob_id, extraction.rendered_blob_id)
    )
    assert all(value not in serialized for value in forbidden if value != "None")
    assert "SECRET_RAW_MODEL_OUTPUT_SENTINEL" not in serialized
    assert "model_run_ids" not in serialized


@pytest.mark.parametrize(
    ("payload_change", "code"),
    [
        ({"content_sha256": "d" * 64}, "production_state_checksum_mismatch"),
        ({"schema_version": 2}, "production_state_version_unsupported"),
    ],
)
async def test_production_state_import_maps_validation_errors(
    api: AsyncClient, uow: _Uow, payload_change: dict[str, Any], code: str
) -> None:
    subject_id, edition_id = uuid4(), uuid4()
    uow.editorial_groups._groups.append(_group(edition_id, "TAG-182", subject_id))
    payload = _state_payload()
    payload.update(payload_change)
    response = await api.post(f"/api/subjects/{subject_id}/production/state/import", json=payload)
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == code


@pytest.mark.parametrize(
    ("initial_status", "stage", "expected_stale"),
    (
        (
            SubjectProductionStatus.READY,
            SubjectProductionStage.SOURCES,
            ["references", "extraction", "synthesis", "publication"],
        ),
        (
            SubjectProductionStatus.READY,
            SubjectProductionStage.REFERENCES,
            ["references", "extraction", "synthesis", "publication"],
        ),
        (
            SubjectProductionStatus.READY,
            SubjectProductionStage.EXTRACTION,
            ["extraction", "synthesis", "publication"],
        ),
        (
            SubjectProductionStatus.READY,
            SubjectProductionStage.SYNTHESIS,
            ["synthesis", "publication"],
        ),
        (SubjectProductionStatus.READY, SubjectProductionStage.ASSEMBLY, ["publication"]),
        (
            SubjectProductionStatus.FAILED,
            SubjectProductionStage.EXTRACTION,
            ["extraction", "synthesis", "publication"],
        ),
        (
            SubjectProductionStatus.NEEDS_REVIEW,
            SubjectProductionStage.EXTRACTION,
            ["extraction", "synthesis", "publication"],
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
    run = SubjectProductionRun(subject_id=subject_id, edition_id=edition_id)
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
    run = SubjectProductionRun(subject_id=uuid4(), edition_id=uuid4())
    if status is SubjectProductionStatus.RUNNING:
        run.start_running()
    await uow.subject_production_runs.add(run)

    response = await api.post(
        f"/api/subjects/{run.subject_id}/production/retry", json={"stage": "extraction"}
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "retry_not_allowed_while_running"


async def test_publication_artifact_by_run_does_not_follow_subject_current_run(
    api: AsyncClient,
    uow: _Uow,
) -> None:
    subject_id = uuid4()
    first = _terminal_run(uuid4(), subject_id, status=SubjectProductionStatus.FAILED)
    second = _terminal_run(first.edition_id, subject_id, status=SubjectProductionStatus.FAILED)
    await uow.subject_production_runs.add(first)
    await uow.subject_production_runs.add(second)
    first_artifact = _artifact(first, ProductionArtifactStage.PUBLICATION)
    second_artifact = _artifact(second, ProductionArtifactStage.PUBLICATION)
    await uow.production_artifacts.append(first_artifact)
    await uow.production_artifacts.append(second_artifact)

    by_run = await api.get(f"/api/production/runs/{first.id}/artifacts/publication")
    by_subject = await api.get(f"/api/subjects/{subject_id}/production/artifacts/publication")

    assert by_run.status_code == 200, by_run.text
    assert by_subject.status_code == 200, by_subject.text
    assert by_run.json()["artifact_id"] == str(first_artifact.id)
    assert by_subject.json()["artifact_id"] == str(second_artifact.id)


async def test_retry_by_run_changes_only_the_requested_run(
    api: AsyncClient,
    uow: _Uow,
    production_app: FastAPI,
) -> None:
    subject_id = uuid4()
    first = _terminal_run(uuid4(), subject_id, status=SubjectProductionStatus.FAILED)
    second = _terminal_run(first.edition_id, subject_id, status=SubjectProductionStatus.FAILED)
    await uow.subject_production_runs.add(first)
    await uow.subject_production_runs.add(second)
    await uow.production_artifacts.append(_artifact(first, ProductionArtifactStage.REFERENCES))

    response = await api.post(
        f"/api/production/runs/{first.id}/retry", json={"stage": "references"}
    )

    assert response.status_code == 200, response.text
    assert response.json()["run_id"] == str(first.id)
    assert uow.subject_production_runs.items[first.id].pipeline_generation == 1
    assert uow.subject_production_runs.items[second.id].pipeline_generation == 0
    assert production_app.state.job_service.submitted[-1]["input_parameters"]["run_id"] == str(
        first.id
    )


async def test_batch_cancel_marks_every_active_run_and_cancels_exact_jobs(
    api: AsyncClient,
    uow: _Uow,
    production_app: FastAPI,
) -> None:
    edition_id = uuid4()
    subjects = [uuid4(), uuid4()]
    for name, subject_id in zip(("A", "B"), subjects, strict=True):
        uow.editorial_groups._groups.append(_group(edition_id, name, subject_id))
    jobs = _CancelableJobs()
    production_app.state.job_service = jobs

    started = await api.post(f"/api/editions/{edition_id}/production", json={})
    assert started.status_code == 200, started.text
    batch = next(iter(uow.edition_production_batches.items.values()))
    first_run = uow.subject_production_runs.items[
        uow.edition_production_batch_items.items[0].production_run_id
    ]
    unrelated = _TrackedJob(subject_id=subjects[0], run_id=uuid4())
    jobs.jobs.append(unrelated)

    response = await api.post(f"/api/editions/{edition_id}/production/{batch.id}/cancel")
    repeated = await api.post(f"/api/editions/{edition_id}/production/{batch.id}/cancel")

    assert response.status_code == 200, response.text
    assert repeated.status_code == 200, repeated.text
    assert batch.status == "cancelled"
    assert all(
        run.status is SubjectProductionStatus.CANCELLED
        for run in uow.subject_production_runs.items.values()
    )
    exact_job = jobs.jobs[0]
    assert str(first_run.id) == exact_job.input_parameters["run_id"]
    assert exact_job.id in jobs.cancelled
    assert unrelated.id not in jobs.cancelled


async def test_subject_cancel_marks_run_and_cancels_its_exact_job(
    api: AsyncClient,
    uow: _Uow,
    production_app: FastAPI,
) -> None:
    edition_id = uuid4()
    subject_id = uuid4()
    uow.editorial_groups._groups.append(_group(edition_id, "A", subject_id))
    jobs = _CancelableJobs()
    production_app.state.job_service = jobs

    started = await api.post(f"/api/subjects/{subject_id}/production", json={})
    assert started.status_code == 200, started.text
    run_id = UUID(started.json()["run_id"])
    response = await api.post(f"/api/subjects/{subject_id}/production/cancel")

    assert response.status_code == 200, response.text
    assert uow.subject_production_runs.items[run_id].status is SubjectProductionStatus.CANCELLED
    assert jobs.cancelled == [jobs.jobs[0].id]
