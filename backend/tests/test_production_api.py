"""API-level tests for the production endpoints.

These cover the "nothing started yet" entry point: the UI decides whether to
offer a start button based on a 404, so these endpoints must answer 404 — never
422 from a broken dependency wiring.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from cti_app.api.production import router
from cti_app.domain.editorial import (
    CandidateReference,
    EditorialGroup,
    EditorialGroupStatus,
    EditorialScore,
    EditorialType,
    GroupingConfidence,
    GroupingOutcome,
    SourceRelationshipStatus,
)
from cti_app.domain.production import (
    EditionProductionBatch,
    EditionProductionBatchItem,
    SubjectProductionRun,
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
        return self.items.get(run_id)

    async def save(self, run: SubjectProductionRun) -> None:
        self.items[run.id] = run

    async def get_current_for_subject(self, subject_id: UUID) -> SubjectProductionRun | None:
        return next((r for r in self.items.values() if r.subject_id == subject_id), None)

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
    async def list_for_run(self, run_id: UUID) -> Sequence[Any]:
        return []

    async def get_current(self, run_id: UUID, stage: str) -> Any:
        return None


class _Uow:
    """Single shared in-memory unit of work; commit is a no-op."""

    def __init__(self, groups: list[EditorialGroup]) -> None:
        self.editorial_groups = _Groups(groups)
        self.subject_production_runs = _Runs()
        self.edition_production_batches = _Batches()
        self.edition_production_batch_items = _BatchItems()
        self.production_artifacts = _Artifacts()

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


async def test_start_edition_briefs_rejects_unselected_subject(api: AsyncClient, uow: _Uow) -> None:
    edition_id = uuid4()
    subject_id = uuid4()
    uow.editorial_groups._groups.append(_group(edition_id, "A", subject_id))

    response = await api.post(
        f"/api/editions/{edition_id}/production/briefs",
        json={"subject_ids": [str(uuid4())]},
    )

    assert response.status_code == 409
