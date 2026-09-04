from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from cti_app.api.production import router
from cti_app.application.identity import LocalIdentityProvider
from cti_app.domain.classification import TLP
from cti_app.domain.discovery import (
    CandidateTopic,
    DiscoveryBatch,
    DiscoverySourceMode,
    SourceCandidate,
    SourceRelationshipStatus,
    SourceRole,
)
from cti_app.domain.editions import EditionStatus
from cti_app.domain.editorial import (
    CandidateReference,
    EditorialGroup,
    EditorialScore,
    GroupingConfidence,
    GroupingOutcome,
)
from cti_app.domain.production import (
    EditionProductionBatchItem,
    SubjectProductionRun,
    SubjectProductionStage,
    SubjectProductionStatus,
)


class _Runs:
    def __init__(self, current: SubjectProductionRun) -> None:
        self.items = {current.id: current}

    async def lock_creation_for_subject(self, subject_id: UUID) -> None:
        del subject_id

    async def get_current_for_subject(self, subject_id: UUID) -> SubjectProductionRun | None:
        matches = [run for run in self.items.values() if run.subject_id == subject_id]
        return max(matches, key=lambda run: run.created_at) if matches else None

    async def allocate_next_run_number(self, subject_id: UUID) -> int:
        return 1 + max(
            (run.run_number for run in self.items.values() if run.subject_id == subject_id),
            default=0,
        )

    async def list_for_edition(self, edition_id: UUID) -> Sequence[SubjectProductionRun]:
        return [run for run in self.items.values() if run.edition_id == edition_id]

    async def add(self, run: SubjectProductionRun) -> None:
        self.items[run.id] = run

    async def get(self, run_id: UUID) -> SubjectProductionRun | None:
        return self.items.get(run_id)

    async def get_for_update(self, run_id: UUID) -> SubjectProductionRun | None:
        return self.items.get(run_id)

    async def save(self, run: SubjectProductionRun) -> None:
        self.items[run.id] = run


class _Snapshots:
    def __init__(self) -> None:
        self.items: dict[UUID, Any] = {}

    async def add(self, snapshot: Any) -> None:
        self.items[snapshot.production_run_id] = snapshot

    async def get_by_run(self, run_id: UUID) -> Any | None:
        return self.items.get(run_id)


class _Groups:
    def __init__(self, group: EditorialGroup) -> None:
        self.group = group

    async def get_by_subject(self, subject_id: UUID) -> EditorialGroup | None:
        return self.group if self.group.subject_id == subject_id else None


class _Editions:
    def __init__(self, edition_id: UUID) -> None:
        self.edition_id = edition_id

    async def get(self, edition_id: UUID) -> Any | None:
        if edition_id != self.edition_id:
            return None
        today = date(2026, 9, 4)
        return type(
            "Edition",
            (),
            {
                "id": edition_id,
                "status": EditionStatus.PRODUCTION,
                "period_start": today - timedelta(days=7),
                "period_end": today,
            },
        )()


class _Batches:
    def __init__(self, batches: list[DiscoveryBatch]) -> None:
        self.batches = batches

    async def list_for_edition(self, edition_id: UUID) -> list[DiscoveryBatch]:
        return [batch for batch in self.batches if batch.edition_id == edition_id]


class _BatchItems:
    def __init__(self, item: EditionProductionBatchItem) -> None:
        self.item = item

    async def get_by_run(self, run_id: UUID) -> EditionProductionBatchItem | None:
        return self.item if self.item.production_run_id == run_id else None

    async def save(self, item: EditionProductionBatchItem) -> None:
        self.item = item


class _Uow:
    def __init__(
        self,
        runs: _Runs,
        snapshots: _Snapshots,
        groups: _Groups,
        editions: _Editions,
        batches: _Batches,
        items: _BatchItems,
    ) -> None:
        self.subject_production_runs = runs
        self.production_input_snapshots = snapshots
        self.editorial_groups = groups
        self.editions = editions
        self.discovery_batches = batches
        self.edition_production_batch_items = items

    async def __aenter__(self) -> _Uow:
        return self

    async def __aexit__(self, *exc: object) -> None:
        del exc

    async def commit(self) -> None:
        return None


class _Jobs:
    def __init__(self) -> None:
        self.submitted: list[dict[str, Any]] = []
        self.ids: list[UUID] = []

    async def submit(self, **kwargs: Any) -> Any:
        self.submitted.append(kwargs)
        job_id = uuid4()
        self.ids.append(job_id)
        return type("Job", (), {"id": job_id})()


class _Dispatcher:
    def __init__(self) -> None:
        self.dispatched: list[UUID] = []

    async def dispatch(self, job_id: UUID, **kwargs: Any) -> None:
        del kwargs
        self.dispatched.append(job_id)


class _Factory:
    def __init__(self, uow: _Uow) -> None:
        self.uow = uow

    def __call__(self) -> _Uow:
        return self.uow


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


def _candidate(title: str, url: str) -> CandidateTopic:
    return CandidateTopic(
        title=title,
        summary="Summary.",
        novelty="Novel.",
        technical_potential=2,
        uncertainties=(),
        relevance_reasons=(),
        actors=(),
        campaigns=(),
        malware=(),
        cves=(),
        victims=(),
        sectors=(),
        countries=(),
        likely_artifacts=(),
        sources=[
            SourceCandidate(
                url=url,
                title=title,
                publisher="Research vendor",
                role=SourceRole.PRIMARY,
                tlp=TLP.AMBER,
                sensitivity="internal",
                external_llm_allowed=True,
            )
        ],
        tlp=TLP.AMBER,
        sensitivity="internal",
        external_llm_allowed=True,
    )


@pytest.mark.asyncio
async def test_restart_with_new_sources_captures_fresh_snapshot_and_repoints_batch() -> None:
    edition_id = uuid4()
    subject_id = uuid4()
    old_run = SubjectProductionRun(
        subject_id=subject_id,
        edition_id=edition_id,
        status=SubjectProductionStatus.NEEDS_REVIEW,
        current_stage=SubjectProductionStage.SOURCES,
        created_at=datetime(2026, 9, 3, 10, tzinfo=UTC),
        updated_at=datetime(2026, 9, 3, 10, tzinfo=UTC),
    )
    old_candidate = _candidate("Blocked report", "https://blocked.example/report")
    new_candidate = _candidate("Blocked report", "https://mirror.example/report")
    old_batch = DiscoveryBatch(
        edition_id=edition_id,
        request_hash="a" * 64,
        complementary_axis="research",
        queries=(),
        citations=(),
        discovery_model_run_id=uuid4(),
        tlp=TLP.AMBER,
        sensitivity="internal",
        external_llm_allowed=True,
        parser_version="test",
        candidates=[old_candidate],
        source_mode=DiscoverySourceMode.MANUAL_IMPORT,
        source_coverage_complete=False,
        source_coverage_incomplete_reason="test",
    )
    replacement_batch = DiscoveryBatch(
        edition_id=edition_id,
        request_hash="b" * 64,
        complementary_axis="manual-url-replace",
        queries=(),
        citations=(),
        discovery_model_run_id=uuid4(),
        tlp=TLP.AMBER,
        sensitivity="internal",
        external_llm_allowed=True,
        parser_version="manual-url-replace-v1",
        candidates=[new_candidate],
        source_mode=DiscoverySourceMode.MANUAL_IMPORT,
        source_coverage_complete=False,
        source_coverage_incomplete_reason="test",
    )
    group = EditorialGroup(
        edition_id=edition_id,
        title="Blocked report",
        candidate_references=(CandidateReference(replacement_batch.id, new_candidate.id),),
        outcome=GroupingOutcome.NEW_SUBJECT,
        score=_score(),
        source_relationship_status=SourceRelationshipStatus.PROVISIONAL,
        needs_source_verification=True,
        needs_source_expansion=True,
        grouping_confidence=GroupingConfidence.HIGH,
        grouping_justification="test",
    )
    group.select(subject_id)
    item = EditionProductionBatchItem(
        batch_id=uuid4(),
        subject_id=subject_id,
        production_run_id=old_run.id,
        position=1,
        auto_recovery_count=1,
    )
    runs = _Runs(old_run)
    snapshots = _Snapshots()
    uow = _Uow(
        runs,
        snapshots,
        _Groups(group),
        _Editions(edition_id),
        _Batches([old_batch, replacement_batch]),
        _BatchItems(item),
    )
    factory = _Factory(uow)
    jobs = _Jobs()
    dispatcher = _Dispatcher()
    application = FastAPI()
    application.include_router(router)
    application.state.uow_factory = factory
    application.state.job_service = jobs
    application.state.job_dispatcher = dispatcher
    application.state.identity_provider = LocalIdentityProvider()

    async with AsyncClient(
        transport=ASGITransport(app=application), base_url="http://test"
    ) as client:
        response = await client.post(
            f"/api/production/subjects/{subject_id}/production/restart-with-new-sources"
        )

    assert response.status_code == 200
    body = response.json()
    new_run_id = UUID(body["run_id"])
    assert body["replaced_run_id"] == str(old_run.id)
    snapshot = snapshots.items[new_run_id]
    assert [source.canonical_url for source in snapshot.core_sources] == [
        "https://mirror.example/report"
    ]
    assert "https://blocked.example/report" not in {
        source.canonical_url for source in snapshot.core_sources
    }
    assert item.production_run_id == new_run_id
    assert item.auto_recovery_count == 0
    assert jobs.submitted[0]["kind"] == "production.subject.sources"
    assert dispatcher.dispatched == jobs.ids
