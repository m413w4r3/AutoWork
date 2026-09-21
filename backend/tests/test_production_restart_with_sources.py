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
    DiscoveryCandidate,
    DiscoverySourceMode,
    SourceCandidate,
    SourceRole,
)
from cti_app.domain.discovery_cumulative import (
    DiscoveryMemberReference,
    DiscoveryPlannerKind,
    DiscoverySnapshot,
    DiscoverySubject,
)
from cti_app.domain.editions import EditionStatus
from cti_app.domain.entities import Subject
from cti_app.domain.production import (
    EditionProductionBatchItem,
    ProductionRun,
    ProductionRunStatus,
    ProductionStage,
)
from cti_app.domain.selection import SubjectDiscoveryOrigin


class _Runs:
    def __init__(self, current: ProductionRun) -> None:
        self.items = {current.id: current}

    async def lock_creation_for_subject(self, subject_id: UUID) -> None:
        del subject_id

    async def get_current_for_subject(self, subject_id: UUID) -> ProductionRun | None:
        matches = [run for run in self.items.values() if run.subject_id == subject_id]
        return max(matches, key=lambda run: run.run_number) if matches else None

    async def get_latest_terminal_for_edition_subject(
        self, edition_id: UUID, subject_id: UUID
    ) -> ProductionRun | None:
        matches = [
            run
            for run in self.items.values()
            if run.edition_id == edition_id
            and run.subject_id == subject_id
            and run.status not in (ProductionRunStatus.QUEUED, ProductionRunStatus.RUNNING)
        ]
        return max(matches, key=lambda run: run.run_number) if matches else None

    async def allocate_next_run_number(self, subject_id: UUID) -> int:
        return 1 + max(
            (run.run_number for run in self.items.values() if run.subject_id == subject_id),
            default=0,
        )

    async def list_for_edition(self, edition_id: UUID) -> Sequence[ProductionRun]:
        return [run for run in self.items.values() if run.edition_id == edition_id]

    async def add(self, run: ProductionRun) -> None:
        self.items[run.id] = run

    async def get(self, run_id: UUID) -> ProductionRun | None:
        return self.items.get(run_id)

    async def get_for_update(self, run_id: UUID) -> ProductionRun | None:
        return self.items.get(run_id)

    async def save(self, run: ProductionRun) -> None:
        self.items[run.id] = run


class _Snapshots:
    def __init__(self) -> None:
        self.items: dict[UUID, Any] = {}

    async def add(self, snapshot: Any) -> None:
        self.items[snapshot.production_run_id] = snapshot

    async def get_by_run(self, run_id: UUID) -> Any | None:
        return self.items.get(run_id)


class _Subjects:
    def __init__(self, subject: Subject) -> None:
        self.subject = subject

    async def get(self, subject_id: UUID) -> Subject | None:
        return self.subject if self.subject.id == subject_id else None


class _Editions:
    def __init__(self, edition_id: UUID, state: EditionStatus = EditionStatus.OPEN) -> None:
        self.edition_id = edition_id
        self.state = state
        self.version = 1

    async def get(self, edition_id: UUID) -> Any | None:
        if edition_id != self.edition_id:
            return None
        today = date(2026, 9, 4)
        return type(
            "Edition",
            (),
            {
                "id": edition_id,
                "state": self.state,
                "version": self.version,
                "period_start": today - timedelta(days=7),
                "period_end": today,
            },
        )()

    async def get_for_update(self, edition_id: UUID) -> Any | None:
        return await self.get(edition_id)


class _Batches:
    def __init__(self, batches: list[DiscoveryBatch]) -> None:
        self.batches = batches

    async def list_for_edition(self, edition_id: UUID) -> list[DiscoveryBatch]:
        return [batch for batch in self.batches if batch.edition_id == edition_id]


class _BatchItems:
    def __init__(self, item: EditionProductionBatchItem) -> None:
        self.item = item


class _Origins:
    def __init__(self, origin: SubjectDiscoveryOrigin) -> None:
        self.origin = origin

    async def get_by_subject(self, subject_id: UUID) -> SubjectDiscoveryOrigin | None:
        return self.origin if self.origin.subject_id == subject_id else None


class _Identities:
    async def resolve_canonical_subject(self, subject_id: UUID) -> UUID:
        return subject_id


class _DiscoverySnapshots:
    def __init__(self, snapshot: DiscoverySnapshot) -> None:
        self.snapshot = snapshot

    async def get_active(self, edition_id: UUID) -> DiscoverySnapshot | None:
        return self.snapshot if self.snapshot.edition_id == edition_id else None


class _DiscoveryCandidates:
    def __init__(self, candidates: Sequence[DiscoveryCandidate]) -> None:
        self.candidates = tuple(candidates)

    async def list_for_edition(
        self, edition_id: UUID, *, include_replaced: bool = False
    ) -> Sequence[DiscoveryCandidate]:
        del edition_id, include_replaced
        return self.candidates

    async def get_by_run(self, run_id: UUID) -> EditionProductionBatchItem | None:
        return self.item if self.item.production_run_id == run_id else None

    async def save(self, item: EditionProductionBatchItem) -> None:
        self.item = item


class _Uow:
    def __init__(
        self,
        runs: _Runs,
        snapshots: _Snapshots,
        subjects: _Subjects,
        editions: _Editions,
        batches: _Batches,
        items: _BatchItems,
    ) -> None:
        self.production_runs = runs
        self.production_input_snapshots = snapshots
        self.subjects = subjects
        self.editions = editions
        self.discovery_batches = batches
        self.edition_production_batch_items = items
        subject = subjects.subject
        source_batch = batches.batches[-1]
        candidate = DiscoveryCandidate.from_candidate_topic(
            source_batch.candidates[0],
            discovery_run_id=source_batch.discovery_run_id,
            discovery_batch_id=source_batch.id,
            position=0,
        )
        snapshot = DiscoverySnapshot(
            edition_id=subject.edition_id,
            version=1,
            parent_snapshot_id=None,
            intake_id=None,
            merge_run_id=uuid4(),
            planner_kind=DiscoveryPlannerKind.DETERMINISTIC_BOOTSTRAP,
            subjects=(
                DiscoverySubject(
                    subject_id=subject.id,
                    candidate=source_batch.candidates[0],
                    member_references=(DiscoveryMemberReference(candidate.id),),
                    created_at=candidate.created_at,
                ),
            ),
            snapshot_hash="a" * 64,
            is_active=True,
        )
        self.subject_discovery_origins = _Origins(
            SubjectDiscoveryOrigin(
                subject_id=subject.id,
                edition_id=subject.edition_id,
                discovery_subject_id=subject.id,
                selection_decision_id=uuid4(),
                selected_snapshot_id=snapshot.id,
                selected_snapshot_version=1,
            )
        )
        self.discovery_subject_identities = _Identities()
        self.discovery_snapshots = _DiscoverySnapshots(snapshot)
        self.discovery_candidates = _DiscoveryCandidates((candidate,))

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
@pytest.mark.parametrize(
    ("edition_state", "archive_before_repoint"),
    (
        (EditionStatus.OPEN, False),
        (EditionStatus.ARCHIVED, False),
        (EditionStatus.OPEN, True),
    ),
)
async def test_restart_with_new_sources_captures_fresh_snapshot_and_repoints_batch(
    edition_state: EditionStatus,
    archive_before_repoint: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    edition_id = uuid4()
    subject_id = uuid4()
    old_run = ProductionRun(
        subject_id=subject_id,
        edition_id=edition_id,
        status=ProductionRunStatus.NEEDS_REVIEW,
        current_stage=ProductionStage.SOURCES,
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
        discovery_run_id=uuid4(),
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
        discovery_run_id=uuid4(),
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
    item = EditionProductionBatchItem(
        batch_id=uuid4(),
        subject_id=subject_id,
        production_run_id=old_run.id,
        position=1,
        auto_recovery_count=1,
    )
    runs = _Runs(old_run)
    snapshots = _Snapshots()
    editions = _Editions(edition_id, state=edition_state)
    subject = Subject(
        id=subject_id,
        edition_id=edition_id,
        title="Blocked report",
        slug=f"blocked-report-{subject_id.hex[:8]}",
        tlp=TLP.AMBER,
    )
    uow = _Uow(
        runs,
        snapshots,
        _Subjects(subject),
        editions,
        _Batches([old_batch, replacement_batch]),
        _BatchItems(item),
    )
    if archive_before_repoint:
        original_get_for_update = editions.get_for_update
        calls = 0

        async def archive_on_repoint(locked_edition_id: UUID) -> Any | None:
            nonlocal calls
            calls += 1
            if calls == 2:
                editions.state = EditionStatus.ARCHIVED
            return await original_get_for_update(locked_edition_id)

        monkeypatch.setattr(editions, "get_for_update", archive_on_repoint)
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

    if edition_state is EditionStatus.ARCHIVED:
        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "production_edition_archived"
        assert len(runs.items) == 1
        assert item.production_run_id == old_run.id
        assert jobs.submitted == []
        assert dispatcher.dispatched == []
        return

    if archive_before_repoint:
        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "production_edition_archived"
        assert len(runs.items) == 2
        assert item.production_run_id == old_run.id
        assert jobs.submitted == []
        assert dispatcher.dispatched == []
        assert editions.state is EditionStatus.ARCHIVED
        return

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
    assert item.production_run_id == old_run.id
    assert item.auto_recovery_count == 1
    assert jobs.submitted[0]["kind"] == "production.subject.sources"
    assert dispatcher.dispatched == jobs.ids
    assert editions.state is EditionStatus.OPEN
    assert editions.version == 1
