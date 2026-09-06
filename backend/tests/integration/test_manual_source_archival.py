"""Analyst uploads archived against a real PostgreSQL schema.

The in-memory unit of work cannot see the foreign keys that make a manual
upload fail in production: ``source_collections.fetch_job_id`` and
``collection_attempts.job_id`` both reference ``jobs.id``.  These tests
therefore drive the real HTTP endpoint over the migrated schema.
"""

from __future__ import annotations

import calendar
import hashlib
from collections.abc import AsyncIterator, Iterator, Sequence
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from cti_app.api.collection import router
from cti_app.application.collection import (
    SubjectCollectionService,
    register_collection_jobs,
)
from cti_app.application.collection_review import CollectionReviewService
from cti_app.application.http_collection import (
    PinnedHttpRequest,
    RawHttpResponse,
    SafeHttpCollector,
)
from cti_app.application.identity import LocalIdentityProvider
from cti_app.application.jobs import (
    JobExecutor,
    JobRegistry,
    JobService,
    SynchronousJobDispatcher,
)
from cti_app.application.persistence import UnitOfWorkFactory
from cti_app.domain.classification import TLP
from cti_app.domain.collection import CollectionState, SourceCollection, SourceOriginKind
from cti_app.domain.discovery import (
    CandidateTopic,
    DiscoveryBatch,
    SourceCandidate,
    SourceRole,
)
from cti_app.domain.editions import Edition
from cti_app.domain.editorial import (
    CandidateReference,
    EditorialGroup,
    EditorialScore,
    GroupingConfidence,
    GroupingOutcome,
)
from cti_app.domain.entities import Subject
from cti_app.domain.jobs import Job
from cti_app.domain.model_runs import ModelProvider, ModelRole, ModelRun
from cti_app.infrastructure.blob_storage.filesystem import FilesystemBlobStore
from cti_app.infrastructure.database.session import (
    create_postgres_engine,
    create_session_factory,
)
from cti_app.infrastructure.database.uow import SqlAlchemyUnitOfWork

pytestmark = pytest.mark.integration

PUBLIC_IP = "93.184.216.34"
HTML = (
    b"<!doctype html><html><head><title>RedKitten</title></head>"
    b"<body>RedKitten uses malicious.example.com for staging.</body></html>"
)
TEXT = b"RedKitten staging host: malicious.example.com\n"
_LEASE = timedelta(minutes=2)
# Two-letter codes reserved for this module, so its editions never collide with
# another integration module sharing the same migrated database.
_COUNTRY_CODES = iter(f"Q{letter}" for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ")


class Resolver:
    async def resolve(self, hostname: str) -> Sequence[str]:
        del hostname
        return (PUBLIC_IP,)


class Transport:
    """A transport that fails loudly: manual archival must never fetch."""

    def __init__(self, responses: list[RawHttpResponse] | None = None) -> None:
        self.responses = responses or []
        self.requests: list[PinnedHttpRequest] = []

    async def request(self, request: PinnedHttpRequest) -> RawHttpResponse:
        self.requests.append(request)
        if not self.responses:
            raise AssertionError(f"Unexpected network call to {request.url}")
        return self.responses.pop(0)


class NoopContext:
    def __init__(self, job_id: UUID) -> None:
        self.job_id = job_id

    async def report_progress(self, current: int, total: int, message: str | None = None) -> None:
        del current, total, message

    async def heartbeat(self) -> None:
        return None

    async def check_cancelled(self) -> None:
        return None

    async def record_diagnostics(self, details: dict[str, object]) -> None:
        del details


@pytest.fixture
def postgres_engine(migrated_postgres_url: str) -> Iterator[AsyncEngine]:
    engine = create_postgres_engine(migrated_postgres_url)
    yield engine


@pytest.fixture
def postgres_uow_factory(postgres_engine: AsyncEngine) -> UnitOfWorkFactory:
    session_factory = create_session_factory(postgres_engine)

    def factory() -> SqlAlchemyUnitOfWork:
        return SqlAlchemyUnitOfWork(session_factory)

    return factory


async def _seed_subject(
    uow_factory: UnitOfWorkFactory,
    urls: tuple[str, ...],
    *,
    states: tuple[CollectionState, ...] | None = None,
) -> tuple[Subject, tuple[SourceCollection, ...]]:
    """Create one subject with its collections directly in PostgreSQL."""
    # Editions are unique on (country_code, period), and the migrated database
    # is shared with every other integration module: each seed takes its own
    # country code rather than competing for one calendar slot.
    country_code = next(_COUNTRY_CODES)
    edition = Edition(
        country=f"Manualland {country_code}",
        country_code=country_code,
        period_start=date(2026, 7, 1),
        period_end=date(2026, 7, calendar.monthrange(2026, 7)[1]),
        tlp=TLP.AMBER,
        languages=("fr", "en"),
        target_articles=2,
        previous_edition_id=None,
        source_profile="default",
    )
    candidates = [
        SourceCandidate(
            url=url,
            title=f"Report {index}",
            publisher="Research team",
            role=SourceRole.PRIMARY if index == 1 else SourceRole.INDEPENDENT,
            tlp=TLP.AMBER,
            sensitivity="internal",
            external_llm_allowed=False,
        )
        for index, url in enumerate(urls, start=1)
    ]
    topic = CandidateTopic(
        title="RedKitten campaign",
        summary="Technical source",
        novelty="new",
        technical_potential=4,
        uncertainties=(),
        relevance_reasons=("technical",),
        actors=(),
        campaigns=(),
        malware=("RedKitten",),
        cves=(),
        victims=(),
        sectors=(),
        countries=("Iran",),
        likely_artifacts=("ioc",),
        sources=candidates,
        tlp=TLP.AMBER,
        sensitivity="internal",
        external_llm_allowed=False,
    )
    batch = DiscoveryBatch(
        edition_id=edition.id,
        request_hash="a" * 64,
        complementary_axis="initial",
        queries=("query",),
        citations=(),
        candidates=[topic],
        discovery_model_run_id=uuid4(),
        tlp=TLP.AMBER,
        sensitivity="internal",
        external_llm_allowed=False,
        parser_version="test-parser-v1",
    )
    subject = Subject(
        external_id=f"subject-{uuid4()}", slug=f"subject-{uuid4().hex}", tlp=TLP.AMBER
    )
    group = EditorialGroup(
        edition_id=edition.id,
        title=topic.title,
        candidate_references=(CandidateReference(batch.id, topic.id),),
        outcome=GroupingOutcome.NEW_SUBJECT,
        score=EditorialScore(2, 2, 2, 2, 2, 2, {"impact": "test"}),
        source_relationship_status=candidates[0].relationship_status,
        needs_source_verification=True,
        needs_source_expansion=True,
        grouping_confidence=GroupingConfidence.HIGH,
        grouping_justification="test",
    )
    group.select(subject.id)
    discovery_run = ModelRun(
        id=batch.discovery_model_run_id,
        provider=ModelProvider.FAKE,
        model_role=ModelRole.RESEARCH,
        requested_model="fixture",
        prompt_template_id="fixture",
        prompt_template_version="1",
        authorized_input_hash="a" * 64,
        evidence_pack_hash="b" * 64,
        parameters={},
    )
    collections = tuple(
        SourceCollection(
            subject_id=subject.id,
            edition_id=edition.id,
            group_id=group.id,
            batch_id=batch.id,
            source_candidate_id=candidate.id,
            requested_url=candidate.canonical_url,
            canonical_url=candidate.canonical_url,
            proposed_role=candidate.role,
            title=candidate.title,
            publisher=candidate.publisher,
            source_tlp=candidate.tlp,
            sensitivity=candidate.sensitivity,
            external_llm_allowed=candidate.external_llm_allowed,
            state=state,
        )
        for candidate, state in zip(
            candidates,
            states or tuple(CollectionState.FAILED_RETRYABLE for _ in candidates),
            strict=True,
        )
    )
    async with uow_factory() as uow:
        assert await uow.editions.add_if_absent(edition)
        await uow.subjects.add(subject)
        await uow.model_runs.add(discovery_run)
        assert await uow.discovery_batches.add_if_absent(batch)
        await uow.editorial_groups.add(group)
        for collection in collections:
            assert await uow.source_collections.add_if_absent(collection)
        await uow.commit()
    return subject, collections


def _service(uow_factory: UnitOfWorkFactory, root: Path, transport: Transport | None = None):
    return SubjectCollectionService(
        uow_factory,
        SafeHttpCollector(transport or Transport(), Resolver()),
        FilesystemBlobStore(root),
    )


async def _client(
    uow_factory: UnitOfWorkFactory,
    service: SubjectCollectionService,
    root: Path,
) -> AsyncIterator[httpx.AsyncClient]:
    app = FastAPI()
    app.include_router(router)
    app.state.collection_service = service
    app.state.collection_review_service = CollectionReviewService(
        uow_factory, FilesystemBlobStore(root)
    )
    registry = JobRegistry()
    register_collection_jobs(registry, service)
    jobs = JobService(uow_factory, registry)
    app.state.job_service = jobs
    app.state.job_dispatcher = SynchronousJobDispatcher(JobExecutor(uow_factory, registry))
    app.state.identity_provider = LocalIdentityProvider("analyst-1")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client


async def _job_count(engine: AsyncEngine) -> int:
    async with engine.connect() as connection:
        result = await connection.execute(text("SELECT count(*) FROM jobs"))
        return int(result.scalar_one())


async def test_manual_html_upload_archives_without_creating_a_job(
    postgres_engine: AsyncEngine,
    postgres_uow_factory: UnitOfWorkFactory,
    tmp_path: Path,
) -> None:
    subject, collections = await _seed_subject(
        postgres_uow_factory, (f"https://redkitten-{uuid4().hex}.example/report",)
    )
    source = collections[0]
    service = _service(postgres_uow_factory, tmp_path / "blobs")
    jobs_before = await _job_count(postgres_engine)

    async for client in _client(postgres_uow_factory, service, tmp_path / "blobs"):
        response = await client.post(
            f"/api/subjects/{subject.id}/sources/{source.id}/content",
            json={"content": HTML.decode(), "declared_mime_type": "text/html"},
        )

    assert response.status_code == 200, response.text
    assert response.json()["state"] == "archived"

    async with postgres_uow_factory() as uow:
        stored = await uow.source_collections.get(source.id)
        assert stored is not None
        assert stored.state is CollectionState.ARCHIVED
        assert stored.fetch_job_id is None
        assert stored.manual_lease_id is None
        assert stored.origin_kind is SourceOriginKind.MANUAL
        assert stored.source_document_id is not None
        assert stored.decoded_blob_id is not None
        attempts = await uow.collection_attempts.list_for_collection(source.id)
        assert len(attempts) == 1
        assert attempts[0].job_id is None
        assert attempts[0].manual_lease_id is not None
        assert attempts[0].encoded_sha256 == hashlib.sha256(HTML).hexdigest()
        assert attempts[0].decoded_sha256 == hashlib.sha256(HTML).hexdigest()
        document = await uow.source_documents.get(stored.source_document_id)
        assert document is not None
        assert document.decoded_sha256 == hashlib.sha256(HTML).hexdigest()
        assert document.declared_mime_type == "text/html"
        assert document.detected_mime_type == "text/html"
        events = await uow.provenance.list_for_aggregate("source_collection", source.id)

    # The audit must name the analyst, never pretend a collector downloaded it.
    assert [event.event_type for event in events] == ["source.archived_manually"]
    assert events[0].actor_id == "analyst-1"
    assert await _job_count(postgres_engine) == jobs_before


async def test_manual_text_upload_and_file_upload_are_archived(
    postgres_uow_factory: UnitOfWorkFactory, tmp_path: Path
) -> None:
    subject, collections = await _seed_subject(
        postgres_uow_factory,
        (
            f"https://text-{uuid4().hex}.example/report",
            f"https://file-{uuid4().hex}.example/report",
        ),
    )
    service = _service(postgres_uow_factory, tmp_path / "blobs")

    async for client in _client(postgres_uow_factory, service, tmp_path / "blobs"):
        text_response = await client.post(
            f"/api/subjects/{subject.id}/sources/{collections[0].id}/content",
            json={"content": TEXT.decode(), "declared_mime_type": "text/plain"},
        )
        file_response = await client.post(
            f"/api/subjects/{subject.id}/sources/{collections[1].id}/content",
            files={"file": ("capture.html", HTML, "text/html")},
            data={"declared_mime_type": "text/html"},
        )

    assert text_response.status_code == 200, text_response.text
    assert file_response.status_code == 200, file_response.text

    async with postgres_uow_factory() as uow:
        for collection, payload, mime in (
            (collections[0], TEXT, "text/plain"),
            (collections[1], HTML, "text/html"),
        ):
            stored = await uow.source_collections.get(collection.id)
            assert stored is not None
            assert stored.state is CollectionState.ARCHIVED
            assert stored.fetch_job_id is None
            attempts = await uow.collection_attempts.list_for_collection(collection.id)
            assert [attempt.job_id for attempt in attempts] == [None]
            assert attempts[0].decoded_sha256 == hashlib.sha256(payload).hexdigest()
            assert attempts[0].detected_content_type == mime


async def test_sources_retry_reports_already_archived_after_a_manual_upload(
    postgres_uow_factory: UnitOfWorkFactory, tmp_path: Path
) -> None:
    """The RedKitten case: one unavailable source, one supplied by the analyst."""
    subject, collections = await _seed_subject(
        postgres_uow_factory,
        (
            f"https://retryable-{uuid4().hex}.example/report",
            f"https://unavailable-{uuid4().hex}.example/report",
        ),
        states=(CollectionState.FAILED_RETRYABLE, CollectionState.UNAVAILABLE),
    )
    retryable, unavailable = collections
    service = _service(postgres_uow_factory, tmp_path / "blobs")

    async for client in _client(postgres_uow_factory, service, tmp_path / "blobs"):
        archived = await client.post(
            f"/api/subjects/{subject.id}/sources/{retryable.id}/content",
            json={"content": HTML.decode(), "declared_mime_type": "text/html"},
        )
    assert archived.status_code == 200, archived.text

    job = Job(
        kind="source.collect",
        aggregate_type="subject",
        aggregate_id=subject.id,
        idempotency_key=f"source.collect:{subject.id}:{uuid4()}",
        correlation_id=str(uuid4()),
        input_parameters={"subject_id": str(subject.id)},
    )
    async with postgres_uow_factory() as uow:
        assert await uow.jobs.add_if_absent(job)
        await uow.commit()

    summary = await service.collect_subject(subject.id, job.id, NoopContext(job.id))

    assert summary.startswith("provenance://events/")
    async with postgres_uow_factory() as uow:
        events = await uow.provenance.list_for_aggregate("source_collection_job", job.id)
        stored_unavailable = await uow.source_collections.get(unavailable.id)
    completed = [event for event in events if event.event_type == "source.collection_completed"]
    assert completed
    payload = completed[-1].payload
    # No source_collection_no_success: the manual archive counts as a success.
    assert payload["already_archived"] == 1
    assert payload["already_archived"] + payload["newly_archived"] > 0
    assert stored_unavailable is not None
    assert stored_unavailable.state is CollectionState.UNAVAILABLE


async def test_manual_upload_cannot_steal_a_live_collector_lease(
    postgres_uow_factory: UnitOfWorkFactory, tmp_path: Path
) -> None:
    subject, collections = await _seed_subject(
        postgres_uow_factory, (f"https://leased-{uuid4().hex}.example/report",)
    )
    source = collections[0]
    service = _service(postgres_uow_factory, tmp_path / "blobs")
    job = Job(
        kind="source.collect",
        aggregate_type="source_collection",
        aggregate_id=source.id,
        idempotency_key=f"source.collect:{source.id}:{uuid4()}",
        correlation_id=str(uuid4()),
        input_parameters={},
    )
    async with postgres_uow_factory() as uow:
        assert await uow.jobs.add_if_absent(job)
        await uow.collection_policy_snapshots.add_if_absent(service.policy_snapshot)
        stored = await uow.source_collections.get_for_update(source.id)
        assert stored is not None
        assert stored.claim_fetch(
            job.id,
            lease_duration=_LEASE,
            policy_snapshot_id=service.policy_snapshot.id,
        )
        await uow.source_collections.save(stored)
        await uow.commit()

    async for client in _client(postgres_uow_factory, service, tmp_path / "blobs"):
        refused = await client.post(
            f"/api/subjects/{subject.id}/sources/{source.id}/content",
            json={"content": HTML.decode(), "declared_mime_type": "text/html"},
        )

    assert refused.status_code == 409
    async with postgres_uow_factory() as uow:
        after = await uow.source_collections.get(source.id)
    assert after is not None
    assert after.state is CollectionState.FETCHING
    assert after.fetch_job_id == job.id


async def test_a_stale_manual_lease_cannot_archive_over_a_newer_one(
    postgres_uow_factory: UnitOfWorkFactory, tmp_path: Path
) -> None:
    """Two concurrent uploads: the loser must never finish the winner's work."""
    subject, collections = await _seed_subject(
        postgres_uow_factory, (f"https://concurrent-{uuid4().hex}.example/report",)
    )
    source = collections[0]
    service = _service(postgres_uow_factory, tmp_path / "blobs")

    stale_lease = uuid4()
    async with postgres_uow_factory() as uow:
        await uow.collection_policy_snapshots.add_if_absent(service.policy_snapshot)
        stored = await uow.source_collections.get_for_update(source.id)
        assert stored is not None
        assert stored.claim_manual_upload(
            stale_lease,
            lease_duration=_LEASE,
            policy_snapshot_id=service.policy_snapshot.id,
            now=datetime(2026, 1, 1, tzinfo=UTC),
        )
        await uow.source_collections.save(stored)
        await uow.commit()

    # The expired lease lets a newer upload take over and archive the source.
    async for client in _client(postgres_uow_factory, service, tmp_path / "blobs"):
        winner = await client.post(
            f"/api/subjects/{subject.id}/sources/{source.id}/content",
            json={"content": HTML.decode(), "declared_mime_type": "text/html"},
        )
    assert winner.status_code == 200, winner.text

    async with postgres_uow_factory() as uow:
        stored = await uow.source_collections.get(source.id)
        assert stored is not None
        with pytest.raises(ValueError):
            stored.archive_manual(
                manual_lease_id=stale_lease,
                attempt_id=uuid4(),
                source_document_id=uuid4(),
                decoded_blob_id=uuid4(),
            )
