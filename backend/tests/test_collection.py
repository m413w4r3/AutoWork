from __future__ import annotations

import asyncio
import calendar
import gzip
import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

import pytest

from cti_app.application.collection import (
    ManualArchiveReceipt,
    ManualContentAlreadyArchivedError,
    ManualContentEmptyError,
    ManualContentTooLargeError,
    ManualContentTypeError,
    ReferencedEvidence,
    SubjectCollectionService,
    SupplementalSource,
    collection_idempotency_key,
)
from cti_app.application.collection_review import CollectionReviewService
from cti_app.application.http_collection import (
    CollectionPolicy,
    DnsResolver,
    DownloadTransientError,
    HttpTransport,
    PinnedHttpRequest,
    RawHttpResponse,
    SafeHttpCollector,
)
from cti_app.application.jobs import JobCancelledError, JobExecutionContext, JobHandlerError
from cti_app.domain.classification import TLP
from cti_app.domain.collection import (
    CollectionAttempt,
    CollectionFailureReason,
    CollectionState,
    CollectionTransportClassification,
    SourceCollection,
    SourceOriginKind,
)
from cti_app.domain.discovery import (
    CandidateTopic,
    DiscoveryBatch,
    DiscoveryCandidate,
    DiscoveryCandidateEvidence,
    SourceCandidate,
    SourceRole,
)
from cti_app.domain.discovery_cumulative import (
    DiscoveryMemberReference,
    DiscoveryPlannerKind,
    DiscoverySnapshot,
    DiscoverySubject,
)
from cti_app.domain.editions import Edition
from cti_app.domain.entities import Subject
from cti_app.domain.selection import SubjectDiscoveryOrigin
from cti_app.infrastructure.blob_storage.filesystem import FilesystemBlobStore
from tests.collection_support import (
    InMemoryCollectionUnitOfWork,
    InMemoryCollectionUnitOfWorkFactory,
)

PUBLIC_IP = "93.184.216.34"
HTML = b"""<!doctype html><html><head><title>Report</title></head>
<body>ExampleRAT uses evil[.]example. English evidence summary.</body></html>"""


class Resolver:
    async def resolve(self, hostname: str) -> Sequence[str]:
        del hostname
        return (PUBLIC_IP,)


class Transport:
    def __init__(self, responses: list[RawHttpResponse]) -> None:
        self.responses = responses
        self.requests: list[PinnedHttpRequest] = []

    async def request(self, request: PinnedHttpRequest) -> RawHttpResponse:
        self.requests.append(request)
        return self.responses.pop(0)


class BlockingTransport(Transport):
    def __init__(self, item: RawHttpResponse) -> None:
        super().__init__([item])
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def request(self, request: PinnedHttpRequest) -> RawHttpResponse:
        self.requests.append(request)
        self.started.set()
        await self.release.wait()
        return self.responses.pop(0)


class NoopContext:
    def __init__(self, job_id: UUID) -> None:
        self.job_id = job_id
        self.progress: list[tuple[int, int]] = []
        self.messages: list[str | None] = []

    async def report_progress(self, current: int, total: int, message: str | None = None) -> None:
        self.progress.append((current, total))
        self.messages.append(message)

    async def heartbeat(self) -> None:
        return None

    async def check_cancelled(self) -> None:
        return None

    async def record_diagnostics(self, details: dict[str, object]) -> None:
        del details


class CancelBeforeArchiveContext(NoopContext):
    def __init__(self, job_id: UUID) -> None:
        super().__init__(job_id)
        self.checks = 0

    async def check_cancelled(self) -> None:
        self.checks += 1
        if self.checks >= 5:
            raise JobCancelledError


class CandidateRepository:
    def __init__(
        self,
        candidates: Sequence[DiscoveryCandidate],
        batches: Mapping[UUID, DiscoveryBatch],
    ) -> None:
        self.candidates = list(candidates)
        self.batches = batches

    async def list_for_batch(self, batch_id: UUID) -> list[DiscoveryCandidate]:
        return [item for item in self.candidates if item.discovery_batch_id == batch_id]

    async def list_for_edition(
        self, edition_id: UUID, *, include_replaced: bool = False
    ) -> list[DiscoveryCandidate]:
        del include_replaced
        return [
            item
            for item in self.candidates
            if self.batches[item.discovery_batch_id].edition_id == edition_id
        ]


class CandidateAwareCollectionFactory:
    def __init__(
        self,
        base: InMemoryCollectionUnitOfWorkFactory,
        candidates: Sequence[DiscoveryCandidate],
    ) -> None:
        self.base = base
        self.candidate_repository = CandidateRepository(candidates, base.batches)

    def __call__(self) -> object:
        unit_of_work = self.base()
        unit_of_work.discovery_candidates = self.candidate_repository
        return unit_of_work


def response(body: bytes = HTML, *, status: int = 200) -> RawHttpResponse:
    return RawHttpResponse(status, {"content-type": "text/html"}, body)


def selected_subject(
    factory: InMemoryCollectionUnitOfWorkFactory,
    urls: tuple[str, ...],
) -> Subject:
    edition = Edition(
        country="Iran",
        country_code="IR",
        period_start=date(2026, 7, 1),
        period_end=date(2026, 7, calendar.monthrange(2026, 7)[1]),
        tlp=TLP.AMBER,
        languages=("fr", "en"),
    )
    sources = [
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
    candidate = CandidateTopic(
        title="ExampleRAT campaign",
        summary="Technical source",
        novelty="new",
        technical_potential=4,
        uncertainties=(),
        relevance_reasons=("technical",),
        actors=(),
        campaigns=(),
        malware=("ExampleRAT",),
        cves=(),
        victims=(),
        sectors=(),
        countries=("Iran",),
        likely_artifacts=("ioc",),
        sources=sources,
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
        candidates=[candidate],
        discovery_run_id=UUID("00000000-0000-0000-0000-000000000001"),
        discovery_model_run_id=uuid4(),
        tlp=TLP.AMBER,
        sensitivity="internal",
        external_llm_allowed=False,
        parser_version="test-parser-v1",
    )
    subject = Subject(
        edition_id=edition.id,
        title="Test subject",
        slug=f"subject-{uuid4().hex}",
        tlp=TLP.AMBER,
    )
    factory.editions[edition.id] = edition
    factory.subjects[subject.id] = subject
    factory.batches[batch.id] = batch
    factory.candidates[candidate.id] = DiscoveryCandidate(
        discovery_run_id=batch.discovery_run_id,
        discovery_batch_id=batch.id,
        position=0,
        title=candidate.title,
        summary=candidate.summary,
        novelty=candidate.novelty,
        technical_potential=candidate.technical_potential,
        technical_potential_reason="fixture",
        event_date=candidate.event_date,
        actor_or_campaign=candidate.actor_or_campaign,
        context_only=candidate.context_only,
        tlp=candidate.tlp,
        sensitivity=candidate.sensitivity,
        external_llm_allowed=candidate.external_llm_allowed,
        evidence=DiscoveryCandidateEvidence(sources=candidate.sources),
        id=candidate.id,
    )
    discovery_subject_id = subject.id
    factory.discovery_canonical_ids[discovery_subject_id] = discovery_subject_id
    discovery_candidate = factory.candidates[candidate.id]
    factory.discovery_snapshots[edition.id] = DiscoverySnapshot(
        edition_id=edition.id,
        version=1,
        parent_snapshot_id=None,
        intake_id=None,
        merge_run_id=uuid4(),
        planner_kind=DiscoveryPlannerKind.DETERMINISTIC_BOOTSTRAP,
        subjects=(
            DiscoverySubject(
                subject_id=discovery_subject_id,
                candidate=candidate,
                member_references=(DiscoveryMemberReference(discovery_candidate.id),),
                created_at=discovery_candidate.created_at,
            ),
        ),
        snapshot_hash="a" * 64,
        is_active=True,
    )
    factory.subject_discovery_origins[subject.id] = SubjectDiscoveryOrigin(
        subject_id=subject.id,
        edition_id=edition.id,
        discovery_subject_id=discovery_subject_id,
        selection_decision_id=uuid4(),
        selected_snapshot_id=factory.discovery_snapshots[edition.id].id,
        selected_snapshot_version=1,
    )
    return subject


def service(
    factory: InMemoryCollectionUnitOfWorkFactory,
    transport: Transport,
    root: Path,
    *,
    with_claims: bool = False,
) -> SubjectCollectionService:
    del with_claims
    return SubjectCollectionService(
        factory,
        SafeHttpCollector(transport, Resolver()),
        FilesystemBlobStore(root),
    )


async def _run_one_attempt(
    url: str,
    transport: HttpTransport,
    root: Path,
    *,
    resolver: DnsResolver | None = None,
) -> tuple[
    InMemoryCollectionUnitOfWorkFactory,
    SubjectCollectionService,
    SourceCollection,
    CollectionState,
    CollectionAttempt,
]:
    factory = InMemoryCollectionUnitOfWorkFactory()
    subject = selected_subject(factory, (url,))
    app = SubjectCollectionService(
        factory,
        SafeHttpCollector(transport, resolver or Resolver()),
        FilesystemBlobStore(root),
    )
    source = (await app.initialize(subject.id))[0]
    state = await app.archive_one(source.id, uuid4())
    return factory, app, source, state, (await app.attempts(source.id))[-1]


async def test_collection_operations_use_subject_edition_when_group_differs(
    tmp_path: Path,
) -> None:
    factory = InMemoryCollectionUnitOfWorkFactory()
    subject = selected_subject(factory, ("https://discovery.example/report",))
    app = service(factory, Transport([]), tmp_path / "blobs")
    initialized = await app.initialize(subject.id)

    assert [item.requested_url for item in initialized] == ["https://discovery.example/report"]
    assert initialized[0].edition_id == subject.edition_id
    assert initialized[0].batch_id == next(iter(factory.batches))

    supplemental = await app.add_supplemental_sources(
        subject.id,
        [SupplementalSource(url="https://reference.example/report")],
    )

    assert len(supplemental) == 1
    assert supplemental[0].edition_id == subject.edition_id
    assert {item.edition_id for item in factory.collections.values()} == {subject.edition_id}


@pytest.mark.asyncio
async def test_source_context_projects_exact_persisted_discovery_candidate_id(
    tmp_path: Path,
) -> None:
    factory = InMemoryCollectionUnitOfWorkFactory()
    subject = selected_subject(factory, ("https://discovery.example/report",))
    batch = next(iter(factory.batches.values()))
    persisted = DiscoveryCandidate.from_candidate_topic(
        batch.candidates[0],
        discovery_run_id=batch.discovery_run_id,
        discovery_batch_id=batch.id,
        position=0,
        created_at=batch.created_at,
    )
    app = service(
        CandidateAwareCollectionFactory(factory, [persisted]),
        Transport([]),
        tmp_path / "blobs",
    )

    source = (await app.initialize(subject.id))[0]
    _candidate, _document, discovery_candidate_id = await app.source_context(source)

    assert discovery_candidate_id == persisted.id


@pytest.mark.asyncio
async def test_source_context_does_not_guess_discovery_candidate_from_url(
    tmp_path: Path,
) -> None:
    factory = InMemoryCollectionUnitOfWorkFactory()
    subject = selected_subject(factory, ("https://discovery.example/report",))
    batch = next(iter(factory.batches.values()))
    unrelated_source = replace(batch.candidates[0].sources[0], id=uuid4())
    persisted = DiscoveryCandidate.from_candidate_topic(
        replace(batch.candidates[0], sources=[unrelated_source]),
        discovery_run_id=batch.discovery_run_id,
        discovery_batch_id=batch.id,
        position=0,
        created_at=batch.created_at,
    )
    app = service(
        CandidateAwareCollectionFactory(factory, [persisted]),
        Transport([]),
        tmp_path / "blobs",
    )

    source = (await app.initialize(subject.id))[0]
    _candidate, _document, discovery_candidate_id = await app.source_context(source)

    assert source.requested_url == persisted.evidence.sources[0].canonical_url
    assert discovery_candidate_id == persisted.id


async def test_same_content_from_two_urls_reuses_blob_but_preserves_observations(
    tmp_path: Path,
) -> None:
    factory = InMemoryCollectionUnitOfWorkFactory()
    subject = selected_subject(
        factory,
        ("https://one.example/report", "https://two.example/report"),
    )
    app = service(factory, Transport([response(), response()]), tmp_path / "blobs")
    sources = await app.initialize(subject.id)

    for source in sources:
        await app.archive_one(source.id, uuid4())

    raw_blobs = [
        item for item in factory.blobs.values() if item.descriptor.logical_bucket == "source-raw"
    ]
    assert len(raw_blobs) == 1
    assert len(factory.documents) == 2
    assert {item.origin for item in factory.documents.values()} == {
        "https://one.example/report",
        "https://two.example/report",
    }


async def test_manual_content_archives_blocked_source_and_records_provenance(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    factory = InMemoryCollectionUnitOfWorkFactory()
    subject = selected_subject(factory, ("https://blocked.example/report",))
    app = service(factory, Transport([]), tmp_path / "blobs")
    source = (await app.initialize(subject.id))[0]
    factory.collections[source.id].state = CollectionState.BLOCKED
    content = b"<html><body>Analyst supplied evidence with ExampleRAT.</body></html>"

    with caplog.at_level("INFO", logger="cti_app.application.collection"):
        archived = await app.archive_manual_content(
            source.id,
            content=content,
            declared_mime_type="text/html",
            final_url="https://blocked.example/final-report",
            actor_id="analyst-1",
        )

    assert archived.state is CollectionState.ARCHIVED
    assert archived.origin_kind is SourceOriginKind.MANUAL
    assert archived.source_document_id is not None
    document = factory.documents[archived.source_document_id]
    assert document.decoded_size == len(content)
    assert document.detected_mime_type == "text/html"
    manual_events = [
        event for event in factory.provenance if event.event_type == "source.archived_manually"
    ]
    assert len(manual_events) == 1
    assert manual_events[0].actor_id == "analyst-1"
    # The manual audit carries the same evidence as a collector archive, plus
    # the analyst identity and the lease that authorized the operation.
    payload = manual_events[0].payload
    assert payload["actor_id"] == "analyst-1"
    assert payload["declared_mime_type"] == "text/html"
    assert payload["size"] == len(content)
    assert payload["decoded_sha256"] == hashlib.sha256(content).hexdigest()
    assert payload["encoded_sha256"] == hashlib.sha256(content).hexdigest()
    assert payload["source_document_id"] == str(archived.source_document_id)
    assert payload["decoded_blob_id"] == str(archived.decoded_blob_id)
    assert payload["requested_url"] == archived.canonical_url
    assert payload["original_url"] == archived.canonical_url
    assert payload["final_url"] == "https://blocked.example/final-report"
    assert manual_events[0].occurred_at.tzinfo is not None
    assert UUID(payload["manual_lease_id"])
    # No collector event: the analyst supplied the content, not the collector.
    assert not [event for event in factory.provenance if event.event_type == "source.archived"]
    attempts = await app.attempts(archived.id)
    assert [attempt.job_id for attempt in attempts] == [None]
    assert attempts[-1].manual_lease_id == UUID(payload["manual_lease_id"])
    completed = [
        record
        for record in caplog.records
        if getattr(record, "event", None) == "source.manual_archive.completed"
    ]
    assert len(completed) == 1
    assert completed[0].encoded_sha256 == hashlib.sha256(content).hexdigest()
    assert completed[0].decoded_sha256 == hashlib.sha256(content).hexdigest()
    assert completed[0].bytes == len(content)
    assert completed[0].raw_blob_id
    assert completed[0].decoded_blob_id == str(archived.decoded_blob_id)
    assert "ExampleRAT" not in completed[0].getMessage()


async def test_manual_archive_receipt_survives_a_reload_and_is_rebuilt_from_provenance(
    tmp_path: Path,
) -> None:
    """The receipt must be reconstructible by the backend, not held by the UI."""
    factory = InMemoryCollectionUnitOfWorkFactory()
    subject = selected_subject(factory, ("https://blocked.example/report",))
    app = service(factory, Transport([]), tmp_path / "blobs")
    source = (await app.initialize(subject.id))[0]
    factory.collections[source.id].state = CollectionState.BLOCKED
    content = b"<html><body>Analyst supplied evidence with ExampleRAT.</body></html>"

    archived, posted = cast(
        tuple[object, ManualArchiveReceipt],
        await app.archive_manual_content(
            source.id,
            content=content,
            declared_mime_type="text/html",
            actor_id="analyst-1",
            return_receipt=True,
        ),
    )

    # A brand new service instance: nothing is carried over in memory, exactly
    # like the first request after a browser refresh.
    reloaded_app = service(factory, Transport([]), tmp_path / "blobs")
    reloaded_source = next(
        item for item in await reloaded_app.list_sources(subject.id) if item.id == source.id
    )
    assert reloaded_source.state is CollectionState.ARCHIVED
    assert reloaded_source.origin_kind is SourceOriginKind.MANUAL

    receipt = await reloaded_app.manual_archive_receipt(reloaded_source)
    assert receipt is not None
    assert receipt.decoded_sha256 == hashlib.sha256(content).hexdigest()
    assert receipt.encoded_sha256 == posted.encoded_sha256
    assert receipt.bytes == len(content)
    assert receipt.declared_mime_type == "text/html"
    assert receipt.detected_mime_type == "text/html"
    assert receipt.actor_id == "analyst-1"
    assert receipt.source_document_id == posted.source_document_id
    assert receipt.decoded_blob_id == posted.decoded_blob_id
    assert receipt.raw_blob_id == posted.raw_blob_id
    assert receipt.collection_id == source.id
    assert receipt.subject_id == subject.id
    assert getattr(archived, "source_document_id", None) == posted.source_document_id

    # A collector-archived source never claims a manual receipt.
    other_factory = InMemoryCollectionUnitOfWorkFactory()
    other_subject = selected_subject(other_factory, ("https://one.example/report",))
    collector_app = service(other_factory, Transport([response()]), tmp_path / "collector-blobs")
    collected = (await collector_app.initialize(other_subject.id))[0]
    await collector_app.archive_one(collected.id, uuid4())
    stored = next(
        item
        for item in await collector_app.list_sources(other_subject.id)
        if item.id == collected.id
    )
    assert await collector_app.manual_archive_receipt(stored) is None


async def test_manual_content_rejects_empty_and_oversized_content(tmp_path: Path) -> None:
    factory = InMemoryCollectionUnitOfWorkFactory()
    subject = selected_subject(factory, ("https://blocked.example/report",))
    app = SubjectCollectionService(
        factory,
        SafeHttpCollector(Transport([]), Resolver(), CollectionPolicy(max_download_bytes=32)),
        FilesystemBlobStore(tmp_path / "blobs"),
    )
    source = (await app.initialize(subject.id))[0]

    with pytest.raises(ManualContentEmptyError):
        await app.archive_manual_content(
            source.id,
            content=b"",
            declared_mime_type="text/html",
            actor_id="analyst-1",
        )
    with pytest.raises(ManualContentTooLargeError):
        await app.archive_manual_content(
            source.id,
            content=b"<html>" + b"x" * 32,
            declared_mime_type="text/html",
            actor_id="analyst-1",
        )
    with pytest.raises(ManualContentTypeError):
        await app.archive_manual_content(
            source.id,
            content=b"\x00",
            declared_mime_type="application/octet-stream",
            actor_id="analyst-1",
        )


async def test_manual_content_refuses_an_already_archived_source(tmp_path: Path) -> None:
    factory = InMemoryCollectionUnitOfWorkFactory()
    subject = selected_subject(factory, ("https://one.example/report",))
    app = service(factory, Transport([]), tmp_path / "blobs")
    source = (await app.initialize(subject.id))[0]
    source.state = CollectionState.ARCHIVED
    factory.collections[source.id] = source

    with pytest.raises(ManualContentAlreadyArchivedError, match="source_already_archived"):
        await app.archive_manual_content(
            source.id,
            content=HTML,
            declared_mime_type="text/html",
            actor_id="analyst-1",
        )


async def test_completed_source_relaunch_is_idempotent(tmp_path: Path) -> None:
    factory = InMemoryCollectionUnitOfWorkFactory()
    subject = selected_subject(factory, ("https://one.example/report",))
    transport = Transport([response()])
    app = service(factory, transport, tmp_path / "blobs")
    source = (await app.initialize(subject.id))[0]

    first = await app.archive_one(source.id, uuid4())
    second = await app.archive_one(source.id, uuid4())

    assert first is second is CollectionState.ARCHIVED
    assert len(transport.requests) == 1
    assert len(factory.attempts) == 1
    assert len(factory.documents) == 1


async def test_transient_source_failure_is_resumable_without_duplicate_archive(
    tmp_path: Path,
) -> None:
    factory = InMemoryCollectionUnitOfWorkFactory()
    subject = selected_subject(factory, ("https://one.example/report",))
    transport = Transport([response(status=503), response()])
    app = service(factory, transport, tmp_path / "blobs")
    context = cast(JobExecutionContext, NoopContext(uuid4()))

    with pytest.raises(JobHandlerError) as transient:
        await app.collect_subject(subject.id, context.job_id, context)
    assert transient.value.code == "source_collection_no_success"
    assert transient.value.transient is True
    assert (await app.list_sources(subject.id))[0].state is CollectionState.FAILED_RETRYABLE

    failed_source = (await app.list_sources(subject.id))[0]
    await app.prepare_retry(failed_source.id)
    await app.collect_subject(subject.id, context.job_id, context, collection_id=failed_source.id)

    assert (await app.list_sources(subject.id))[0].state is CollectionState.ARCHIVED
    assert len(factory.attempts) == 2
    assert len(factory.documents) == 1


async def test_partial_batch_failure_keeps_candidate_and_completes_other_source(
    tmp_path: Path,
) -> None:
    factory = InMemoryCollectionUnitOfWorkFactory()
    subject = selected_subject(
        factory,
        ("https://one.example/report", "https://missing.example/report"),
    )
    app = service(factory, Transport([response(), response(status=404)]), tmp_path / "blobs")
    context = cast(JobExecutionContext, NoopContext(uuid4()))

    await app.collect_subject(subject.id, context.job_id, context)

    states = {item.requested_url: item.state for item in await app.list_sources(subject.id)}
    assert states["https://one.example/report"] is CollectionState.ARCHIVED
    assert states["https://missing.example/report"] is CollectionState.UNAVAILABLE
    assert len(factory.collections) == 2
    assert len(factory.attempts) == 2


async def test_selected_collection_does_not_create_evidence(tmp_path: Path) -> None:
    factory = InMemoryCollectionUnitOfWorkFactory()
    subject = selected_subject(factory, ("https://one.example/report",))
    app = service(
        factory,
        Transport([response()]),
        tmp_path / "blobs",
        with_claims=True,
    )
    source = (await app.initialize(subject.id))[0]

    assert await app.archive_one(source.id, uuid4()) is CollectionState.ARCHIVED
    review = CollectionReviewService(factory, FilesystemBlobStore(tmp_path / "blobs"))
    assert await review.list_evidence(subject.id) == ([], [])
    assert not factory.artifacts
    assert not factory.claims
    assert not factory.indicators


async def test_gzip_archives_encoded_and_decoded_representations(tmp_path: Path) -> None:
    factory = InMemoryCollectionUnitOfWorkFactory()
    subject = selected_subject(factory, ("https://one.example/report",))
    encoded = gzip.compress(HTML, mtime=1)
    transport = Transport(
        [RawHttpResponse(200, {"content-type": "text/html", "content-encoding": "gzip"}, encoded)]
    )
    blob_store = FilesystemBlobStore(tmp_path / "blobs")
    app = SubjectCollectionService(
        factory,
        SafeHttpCollector(transport, Resolver()),
        blob_store,
    )
    source = (await app.initialize(subject.id))[0]

    assert await app.archive_one(source.id, uuid4()) is CollectionState.ARCHIVED

    raw = next(
        item for item in factory.blobs.values() if item.descriptor.logical_bucket == "source-raw"
    )
    decoded = next(
        item
        for item in factory.blobs.values()
        if item.descriptor.logical_bucket == "source-decoded"
    )
    assert await blob_store.read(raw.descriptor, max_bytes=len(encoded)) == encoded
    assert await blob_store.read(decoded.descriptor, max_bytes=len(HTML)) == HTML
    attempt = factory.attempts[0]
    assert attempt.encoded_sha256 == hashlib.sha256(encoded).hexdigest()
    assert attempt.decoded_sha256 == hashlib.sha256(HTML).hexdigest()
    snapshot = factory.snapshots[attempt.policy_snapshot_id]
    assert snapshot.user_agent == app.policy_snapshot.user_agent
    assert snapshot.extraction_limits == {}
    assert not factory.artifacts


async def test_distinct_gzip_streams_share_decoded_blob(tmp_path: Path) -> None:
    factory = InMemoryCollectionUnitOfWorkFactory()
    subject = selected_subject(
        factory,
        ("https://one.example/report", "https://two.example/report"),
    )
    headers = {"content-type": "text/html", "content-encoding": "gzip"}
    transport = Transport(
        [
            RawHttpResponse(200, headers, gzip.compress(HTML, mtime=1)),
            RawHttpResponse(200, headers, gzip.compress(HTML, mtime=2)),
        ]
    )
    app = service(factory, transport, tmp_path / "blobs")

    for source in await app.initialize(subject.id):
        await app.archive_one(source.id, uuid4())

    assert (
        len(
            [
                item
                for item in factory.blobs.values()
                if item.descriptor.logical_bucket == "source-raw"
            ]
        )
        == 2
    )
    assert (
        len(
            [
                item
                for item in factory.blobs.values()
                if item.descriptor.logical_bucket == "source-decoded"
            ]
        )
        == 1
    )


async def test_expired_fetch_lease_is_recovered_with_interruption_attempt(tmp_path: Path) -> None:
    factory = InMemoryCollectionUnitOfWorkFactory()
    subject = selected_subject(factory, ("https://one.example/report",))
    app = service(factory, Transport([response()]), tmp_path / "blobs")
    source = (await app.initialize(subject.id))[0]
    source.claim_fetch(
        uuid4(),
        lease_duration=timedelta(seconds=1),
        policy_snapshot_id=app.policy_snapshot.id,
        now=datetime.now(UTC) - timedelta(minutes=5),
    )
    factory.collections[source.id] = source

    assert await app.archive_one(source.id, uuid4()) is CollectionState.ARCHIVED

    assert [item.outcome.value for item in factory.attempts] == ["interrupted", "succeeded"]


async def test_crash_after_download_before_archive_is_immediately_resumable(tmp_path: Path) -> None:
    factory = InMemoryCollectionUnitOfWorkFactory()
    subject = selected_subject(factory, ("https://one.example/report",))
    transport = Transport([response(), response()])
    app = service(factory, transport, tmp_path / "blobs")
    source = (await app.initialize(subject.id))[0]

    with pytest.raises(JobCancelledError):
        await app.archive_one(
            source.id,
            uuid4(),
            context=cast(JobExecutionContext, CancelBeforeArchiveContext(uuid4())),
        )
    assert factory.collections[source.id].state is CollectionState.FAILED_RETRYABLE

    assert await app.archive_one(source.id, uuid4()) is CollectionState.ARCHIVED
    assert len(transport.requests) == 2


async def test_archived_source_resumes_without_network(tmp_path: Path) -> None:
    factory = InMemoryCollectionUnitOfWorkFactory()
    subject = selected_subject(factory, ("https://one.example/report",))
    transport = Transport([response()])
    app = service(factory, transport, tmp_path / "blobs")
    source = (await app.initialize(subject.id))[0]
    await app.archive_one(source.id, uuid4())
    assert factory.collections[source.id].state is CollectionState.ARCHIVED

    assert await app.archive_one(source.id, uuid4()) is CollectionState.ARCHIVED
    assert len(transport.requests) == 1


async def test_two_workers_never_download_same_source_concurrently(tmp_path: Path) -> None:
    factory = InMemoryCollectionUnitOfWorkFactory()
    subject = selected_subject(factory, ("https://one.example/report",))
    transport = BlockingTransport(response())
    app = service(factory, transport, tmp_path / "blobs")
    source = (await app.initialize(subject.id))[0]

    first = asyncio.create_task(app.archive_one(source.id, uuid4()))
    await transport.started.wait()
    second_state = await app.archive_one(source.id, uuid4())
    transport.release.set()

    assert second_state is CollectionState.FETCHING
    assert await first is CollectionState.ARCHIVED
    assert len(transport.requests) == 1


async def test_collection_has_no_extraction_hook(tmp_path: Path) -> None:
    factory = InMemoryCollectionUnitOfWorkFactory()
    subject = selected_subject(factory, ("https://one.example/report",))
    transport = Transport([response()])
    app = SubjectCollectionService(
        factory,
        SafeHttpCollector(transport, Resolver()),
        FilesystemBlobStore(tmp_path / "blobs"),
    )
    source = (await app.initialize(subject.id))[0]

    assert not hasattr(app, "_extract")
    await app.archive_one(source.id, uuid4())
    assert factory.collections[source.id].state is CollectionState.ARCHIVED

    assert await app.archive_one(source.id, uuid4()) is CollectionState.ARCHIVED
    assert len(transport.requests) == 1


async def test_tenable_resume_processes_all_seven_sources_with_exact_summary(
    tmp_path: Path,
) -> None:
    factory = InMemoryCollectionUnitOfWorkFactory()
    subject = selected_subject(
        factory,
        (
            "https://tenable.example/report",
            "https://fbi.example/advisory",
            "https://three.example/report",
            "https://four.example/report",
            "https://five.example/report",
            "https://missing.example/report",
            "https://blocked.example/report",
        ),
    )
    transport = Transport(
        [
            response(),
            response(),
            response(),
            response(),
            response(),
            response(status=404),
        ]
    )
    app = SubjectCollectionService(
        factory,
        SafeHttpCollector(
            transport,
            Resolver(),
            CollectionPolicy(blocked_domains=frozenset({"blocked.example"})),
        ),
        FilesystemBlobStore(tmp_path / "blobs"),
    )
    sources = await app.initialize(subject.id)
    await app.archive_one(sources[0].id, uuid4())
    document_id = factory.collections[sources[0].id].source_document_id
    assert document_id is not None
    tenable_document = factory.documents[document_id]
    tenable_document.title = None
    tenable_document.logical_filename = tenable_document.original_name
    context_value = NoopContext(uuid4())
    context = cast(JobExecutionContext, context_value)

    output_reference = await app.collect_subject(subject.id, context.job_id, context)

    assert output_reference.startswith("provenance://events/")
    assert len(transport.requests) == 6
    assert context_value.progress[0] == (0, 7)
    completed_progress = [
        progress
        for progress, message in zip(context_value.progress, context_value.messages, strict=True)
        if message and message.startswith("Source ")
    ]
    assert completed_progress == [(index, 7) for index in range(1, 8)]
    summary = next(
        event.payload
        for event in factory.provenance
        if event.event_type == "source.collection_completed"
    )
    assert summary == {
        "total": 7,
        "already_archived": 1,
        "newly_archived": 4,
        "unavailable": 1,
        "blocked": 1,
        "failed_retryable": 0,
        "failed_terminal": 0,
    }
    assert factory.collections[sources[0].id].attempt_count == 1
    logical_filename = factory.documents[tenable_document.id].logical_filename
    assert logical_filename is not None
    assert logical_filename.startswith("date-inconnue_TLP AMBER_Report 1_Research team")


async def test_invalid_qwen_output_is_never_requested_during_collection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cti_app.application.extraction import EvidenceExtractionService

    invalid_output = {
        "dates": [],
        "cve": [],
        "campaigns": [{"kind": "campaigns"}],
        "malware": [{"kind": "malware"}],
        "tools": [{"kind": "tools"}],
    }

    async def forbidden_call(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise AssertionError(f"Qwen must not receive this output: {invalid_output}")

    monkeypatch.setattr(EvidenceExtractionService, "extract_claims", forbidden_call)
    factory = InMemoryCollectionUnitOfWorkFactory()
    subject = selected_subject(factory, ("https://one.example/report",))
    app = service(factory, Transport([response()]), tmp_path / "blobs")
    context = cast(JobExecutionContext, NoopContext(uuid4()))

    await app.collect_subject(subject.id, context.job_id, context)

    assert not factory.artifacts
    assert not factory.claims
    assert not factory.indicators


async def test_timeout_like_failure_does_not_stop_next_source(tmp_path: Path) -> None:
    factory = InMemoryCollectionUnitOfWorkFactory()
    subject = selected_subject(
        factory,
        ("https://timeout.example/report", "https://next.example/report"),
    )
    app = service(
        factory,
        Transport([response(status=503), response()]),
        tmp_path / "blobs",
    )
    context = cast(JobExecutionContext, NoopContext(uuid4()))

    await app.collect_subject(subject.id, context.job_id, context)

    states = {item.requested_url: item.state for item in await app.list_sources(subject.id)}
    assert states["https://timeout.example/report"] is CollectionState.FAILED_RETRYABLE
    assert states["https://next.example/report"] is CollectionState.ARCHIVED


async def test_404_is_persisted_as_an_obsolete_url_diagnostic(tmp_path: Path) -> None:
    url = "https://example.test/obsolete.pdf"
    factory, _app, source, state, attempt = await _run_one_attempt(
        url,
        Transport([RawHttpResponse(404, {"content-type": "text/html"}, b"not found")]),
        tmp_path / "blobs",
    )

    assert state is CollectionState.UNAVAILABLE
    assert (
        factory.collections[source.id].failure_reason_code
        is CollectionFailureReason.OBSOLETE_URL_404
    )
    assert attempt.http_status == 404
    assert attempt.final_url == url
    assert attempt.redirect_chain == ()
    assert attempt.declared_content_type == "text/html"
    assert attempt.detected_content_type == "text/plain"
    assert attempt.encoded_size == len(b"not found")
    assert attempt.reason_code is CollectionFailureReason.OBSOLETE_URL_404
    assert attempt.transport_classification is CollectionTransportClassification.HTTP


async def test_403_is_a_blocked_access_diagnostic(tmp_path: Path) -> None:
    url = "https://example.test/private.pdf"
    body = b"<!doctype html><html><body>Access denied</body></html>"
    factory, _app, source, state, attempt = await _run_one_attempt(
        url,
        Transport([RawHttpResponse(403, {"content-type": "text/html"}, body)]),
        tmp_path / "blobs",
    )

    assert state is CollectionState.BLOCKED
    assert (
        factory.collections[source.id].failure_reason_code is CollectionFailureReason.ACCESS_BLOCKED
    )
    assert attempt.http_status == 403
    assert attempt.detected_content_type == "text/html"
    assert attempt.encoded_size == len(body)
    assert attempt.reason_code is CollectionFailureReason.ACCESS_BLOCKED
    assert attempt.transport_classification is CollectionTransportClassification.BLOCKED


async def test_timeout_is_classified_separately_from_other_transport_failures(
    tmp_path: Path,
) -> None:
    class TimeoutTransport:
        async def request(self, request: PinnedHttpRequest) -> RawHttpResponse:
            del request
            raise DownloadTransientError(
                "Collection timeout exceeded",
                reason_code=CollectionFailureReason.TIMEOUT,
                transport_classification=CollectionTransportClassification.TIMEOUT,
            )

    _factory, _app, _source, state, attempt = await _run_one_attempt(
        "https://example.test/timeout.pdf", TimeoutTransport(), tmp_path / "blobs"
    )

    assert state is CollectionState.FAILED_RETRYABLE
    assert attempt.reason_code is CollectionFailureReason.TIMEOUT
    assert attempt.transport_classification is CollectionTransportClassification.TIMEOUT
    assert attempt.http_status is None
    assert attempt.final_url == "https://example.test/timeout.pdf"


async def test_dns_and_tls_failures_keep_distinct_transport_classifications(
    tmp_path: Path,
) -> None:
    class DnsFailureResolver:
        async def resolve(self, hostname: str) -> Sequence[str]:
            del hostname
            raise OSError("resolver unavailable")

    class TlsFailureTransport:
        async def request(self, request: PinnedHttpRequest) -> RawHttpResponse:
            del request
            raise DownloadTransientError(
                "TLS handshake or validation failed",
                reason_code=CollectionFailureReason.TLS_ERROR,
                transport_classification=CollectionTransportClassification.TLS,
            )

    _dns_factory, _dns_app, _dns_source, dns_state, dns_attempt = await _run_one_attempt(
        "https://dns.example/report.pdf",
        Transport([]),
        tmp_path / "dns-blobs",
        resolver=DnsFailureResolver(),
    )
    _tls_factory, _tls_app, _tls_source, tls_state, tls_attempt = await _run_one_attempt(
        "https://tls.example/report.pdf", TlsFailureTransport(), tmp_path / "tls-blobs"
    )

    assert dns_state is CollectionState.FAILED_RETRYABLE
    assert dns_attempt.reason_code is CollectionFailureReason.DNS_ERROR
    assert dns_attempt.transport_classification is CollectionTransportClassification.DNS
    assert tls_state is CollectionState.FAILED_RETRYABLE
    assert tls_attempt.reason_code is CollectionFailureReason.TLS_ERROR
    assert tls_attempt.transport_classification is CollectionTransportClassification.TLS


async def test_html_served_for_pdf_url_is_archived_with_non_pdf_reason(tmp_path: Path) -> None:
    url = "https://example.test/advisory.pdf"
    body = b"<!doctype html><html><body>Temporary page</body></html>"
    factory, _app, source, state, attempt = await _run_one_attempt(
        url,
        Transport([RawHttpResponse(200, {"content-type": "application/pdf"}, body)]),
        tmp_path / "blobs",
    )

    assert state is CollectionState.FAILED_TERMINAL
    assert factory.collections[source.id].source_document_id is not None
    assert (
        factory.collections[source.id].failure_reason_code
        is CollectionFailureReason.NON_PDF_CONTENT
    )
    assert attempt.outcome.value == "error"
    assert attempt.http_status == 200
    assert attempt.declared_content_type == "application/pdf"
    assert attempt.detected_content_type == "text/html"
    assert attempt.encoded_size == len(body)
    assert attempt.reason_code is CollectionFailureReason.NON_PDF_CONTENT
    assert (
        len(
            [
                blob
                for blob in factory.blobs.values()
                if blob.descriptor.logical_bucket == "source-raw"
            ]
        )
        == 1
    )


async def test_corrupt_pdf_is_archived_with_a_parse_failure_reason(tmp_path: Path) -> None:
    url = "https://example.test/corrupt.pdf"
    body = b"%PDF-1.4\nnot a valid PDF body\n"
    factory, _app, source, state, attempt = await _run_one_attempt(
        url,
        Transport([RawHttpResponse(200, {"content-type": "application/pdf"}, body)]),
        tmp_path / "blobs",
    )

    assert state is CollectionState.FAILED_TERMINAL
    assert factory.collections[source.id].source_document_id is not None
    assert (
        factory.collections[source.id].failure_reason_code
        is CollectionFailureReason.PDF_PARSE_FAILURE
    )
    assert attempt.reason_code is CollectionFailureReason.PDF_PARSE_FAILURE
    assert attempt.http_status == 200
    assert attempt.detected_content_type == "application/pdf"
    assert attempt.encoded_size == len(body)


async def test_redirect_chain_and_same_host_alternate_are_provenanced(tmp_path: Path) -> None:
    original = "https://cisa.gov/advisories/old-report.pdf"
    alternate = "https://www.cisa.gov/advisories/current-report.pdf"
    transport = Transport(
        [
            RawHttpResponse(302, {"location": alternate}, b""),
            RawHttpResponse(200, {"content-type": "text/html"}, HTML),
        ]
    )
    factory, app, source, state, attempt = await _run_one_attempt(
        original, transport, tmp_path / "blobs"
    )

    assert state is CollectionState.FAILED_TERMINAL
    assert source.requested_url == original
    assert attempt.final_url == alternate
    assert attempt.redirect_chain == (alternate,)
    assert attempt.candidate_resolution_url == alternate
    assert attempt.candidate_resolution_provenance.value == "same_site_http_redirect"
    archived_event = next(
        event for event in factory.provenance if event.event_type == "source.archived"
    )
    assert archived_event.payload["original_url"] == original
    assert archived_event.payload["final_url"] == alternate
    assert archived_event.payload["candidate_resolution_url"] == alternate
    assert (await app.list_sources(source.subject_id))[0].requested_url == original


async def test_ssrf_unsafe_redirect_remains_blocked(tmp_path: Path) -> None:
    private_target = "http://127.0.0.1/private.pdf"
    transport = Transport([RawHttpResponse(302, {"location": private_target}, b"")])
    _factory, _app, _source, state, attempt = await _run_one_attempt(
        "https://example.test/report.pdf", transport, tmp_path / "blobs"
    )

    assert state is CollectionState.BLOCKED
    assert len(transport.requests) == 1
    assert attempt.final_url == private_target
    assert attempt.redirect_chain == (private_target,)
    assert attempt.http_status == 302
    assert attempt.reason_code is CollectionFailureReason.UNSAFE_DESTINATION
    assert attempt.transport_classification is CollectionTransportClassification.BLOCKED


async def test_duplicate_bytes_share_blobs_and_keep_two_url_provenances(tmp_path: Path) -> None:
    factory = InMemoryCollectionUnitOfWorkFactory()
    subject = selected_subject(
        factory,
        ("https://one.example/report", "https://two.example/report"),
    )
    app = service(factory, Transport([response(HTML), response(HTML)]), tmp_path / "blobs")
    sources = await app.initialize(subject.id)

    for source in sources:
        assert await app.archive_one(source.id, uuid4()) is CollectionState.ARCHIVED

    events = [event for event in factory.provenance if event.event_type == "source.archived"]
    assert len(events) == 2
    assert {event.payload["original_url"] for event in events} == {
        "https://one.example/report",
        "https://two.example/report",
    }
    assert len({event.payload["raw_blob_id"] for event in events}) == 1
    assert len({event.payload["decoded_blob_id"] for event in events}) == 1
    assert len({event.payload["source_document_id"] for event in events}) == 2


async def test_no_success_stage_details_include_precise_collection_cause(
    tmp_path: Path,
) -> None:
    factory = InMemoryCollectionUnitOfWorkFactory()
    subject = selected_subject(factory, ("https://example.test/missing.pdf",))
    app = service(
        factory,
        Transport([RawHttpResponse(404, {"content-type": "text/html"}, b"not found")]),
        tmp_path / "blobs",
    )
    context = cast(JobExecutionContext, NoopContext(uuid4()))

    with pytest.raises(JobHandlerError) as failure:
        await app.collect_subject(subject.id, context.job_id, context)

    diagnostics = failure.value.details["collection_failures"]
    assert diagnostics[0]["reason_code"] == "obsolete_url_404"
    assert diagnostics[0]["http_status"] == 404
    assert diagnostics[0]["detected_content_type"] == "text/plain"
    assert diagnostics[0]["encoded_size"] == len(b"not found")


async def test_size_limit_does_not_stop_next_source(tmp_path: Path) -> None:
    factory = InMemoryCollectionUnitOfWorkFactory()
    subject = selected_subject(
        factory,
        ("https://large.example/report", "https://next.example/report"),
    )
    transport = Transport([response(b"<html>" + b"x" * 10_000), response()])
    app = SubjectCollectionService(
        factory,
        SafeHttpCollector(
            transport,
            Resolver(),
            CollectionPolicy(max_download_bytes=len(HTML) + 10),
        ),
        FilesystemBlobStore(tmp_path / "blobs"),
    )
    context = cast(JobExecutionContext, NoopContext(uuid4()))

    await app.collect_subject(subject.id, context.job_id, context)

    states = {item.requested_url: item.state for item in await app.list_sources(subject.id)}
    assert states["https://large.example/report"] is CollectionState.FAILED_TERMINAL
    assert states["https://next.example/report"] is CollectionState.ARCHIVED
    large = next(
        item
        for item in await app.list_sources(subject.id)
        if item.requested_url == "https://large.example/report"
    )
    assert (await app.attempts(large.id))[-1].reason_code is CollectionFailureReason.SIZE_LIMIT


async def test_blob_store_failure_remains_systemic(tmp_path: Path) -> None:
    class FailingBlobStore(FilesystemBlobStore):
        async def put(  # type: ignore[override]
            self, source: object, *, logical_bucket: str, mime_type: str
        ) -> object:
            del source, logical_bucket, mime_type
            raise RuntimeError("blob store unavailable")

    factory = InMemoryCollectionUnitOfWorkFactory()
    subject = selected_subject(factory, ("https://one.example/report",))
    app = SubjectCollectionService(
        factory,
        SafeHttpCollector(Transport([response()]), Resolver()),
        cast(FilesystemBlobStore, FailingBlobStore(tmp_path / "blobs")),
    )
    source = (await app.initialize(subject.id))[0]

    with pytest.raises(RuntimeError, match="blob store unavailable"):
        await app.archive_one(source.id, uuid4())


async def test_workspace_failure_does_not_fail_collection_job(tmp_path: Path) -> None:
    class FailingWorkspace:
        async def materialize(self, *args: object) -> None:
            del args
            raise OSError("workspace unavailable")

    factory = InMemoryCollectionUnitOfWorkFactory()
    subject = selected_subject(factory, ("https://one.example/report",))
    app = SubjectCollectionService(
        factory,
        SafeHttpCollector(Transport([response()]), Resolver()),
        FilesystemBlobStore(tmp_path / "blobs"),
        workspace_materializer=FailingWorkspace(),  # type: ignore[arg-type]
        workspace_root=tmp_path / "workspaces",
    )
    context = cast(JobExecutionContext, NoopContext(uuid4()))

    output_reference = await app.collect_subject(subject.id, context.job_id, context)

    assert output_reference.startswith("provenance://events/")
    assert any(event.event_type == "source.collection_completed" for event in factory.provenance)


async def test_postgresql_commit_failure_remains_systemic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    factory = InMemoryCollectionUnitOfWorkFactory()
    subject = selected_subject(factory, ("https://one.example/report",))
    app = service(factory, Transport([response()]), tmp_path / "blobs")
    source = (await app.initialize(subject.id))[0]

    async def failed_commit(self: InMemoryCollectionUnitOfWork) -> None:
        del self
        raise RuntimeError("PostgreSQL unavailable")

    monkeypatch.setattr(InMemoryCollectionUnitOfWork, "commit", failed_commit)

    with pytest.raises(RuntimeError, match="PostgreSQL unavailable"):
        await app.archive_one(source.id, uuid4())


async def test_new_contribution_does_not_recollect_an_already_known_url(
    tmp_path: Path,
) -> None:
    """§28 : une URL déjà rattachée au sujet n'est pas retéléchargée.

    Une contribution ultérieure réintroduit la même publication sous un
    SourceCandidate.id différent ; seule la nouvelle URL doit être collectée.
    """
    factory = InMemoryCollectionUnitOfWorkFactory()
    subject = selected_subject(factory, ("https://one.example/report",))
    app = service(factory, Transport([response(), response()]), tmp_path / "blobs")

    first = await app.initialize(subject.id)
    assert [collection.requested_url for collection in first] == ["https://one.example/report"]

    # Deuxième contribution : même publication (nouvel id) + une nouvelle URL.
    known_batch = next(iter(factory.batches.values()))
    known_candidate = known_batch.candidates[0]
    complement_candidate = CandidateTopic(
        title=known_candidate.title,
        summary=known_candidate.summary,
        novelty=known_candidate.novelty,
        technical_potential=4,
        uncertainties=(),
        relevance_reasons=("technical",),
        actors=(),
        campaigns=(),
        malware=("ExampleRAT",),
        cves=(),
        victims=(),
        sectors=(),
        countries=("Iran",),
        likely_artifacts=("ioc",),
        sources=[
            SourceCandidate(
                url="https://one.example/report?utm_source=newsletter",
                title="Report 1",
                publisher="Research team",
                role=SourceRole.PRIMARY,
                tlp=TLP.AMBER,
                sensitivity="internal",
                external_llm_allowed=False,
            ),
            SourceCandidate(
                url="https://three.example/report",
                title="Report 3",
                publisher="Research team",
                role=SourceRole.INDEPENDENT,
                tlp=TLP.AMBER,
                sensitivity="internal",
                external_llm_allowed=False,
            ),
        ],
        tlp=TLP.AMBER,
        sensitivity="internal",
        external_llm_allowed=False,
    )
    complement = DiscoveryBatch(
        edition_id=known_batch.edition_id,
        request_hash="b" * 64,
        complementary_axis="complement",
        queries=("query",),
        citations=(),
        candidates=[complement_candidate],
        discovery_run_id=UUID("00000000-0000-0000-0000-000000000002"),
        discovery_model_run_id=uuid4(),
        tlp=TLP.AMBER,
        sensitivity="internal",
        external_llm_allowed=False,
        parser_version="test-parser-v1",
    )
    factory.batches[complement.id] = complement
    complement_discovery_candidate = DiscoveryCandidate.from_candidate_topic(
        complement_candidate,
        discovery_run_id=complement.discovery_run_id,
        discovery_batch_id=complement.id,
        position=0,
    )
    factory.candidates[complement_discovery_candidate.id] = complement_discovery_candidate
    snapshot = factory.discovery_snapshots[known_batch.edition_id]
    subject_snapshot = snapshot.subjects[0]
    factory.discovery_snapshots[known_batch.edition_id] = replace(
        snapshot,
        subjects=(
            replace(
                subject_snapshot,
                member_references=(
                    *subject_snapshot.member_references,
                    DiscoveryMemberReference(complement_discovery_candidate.id),
                ),
            ),
        ),
    )
    factory.candidates[complement_candidate.id] = DiscoveryCandidate.from_candidate_topic(
        complement_candidate,
        discovery_run_id=complement.discovery_run_id,
        discovery_batch_id=complement.id,
        position=0,
    )

    collections = await app.initialize(subject.id)

    assert sorted(collection.requested_url for collection in collections) == [
        "https://one.example/report",
        "https://three.example/report",
    ]


def test_collection_idempotency_key_matches_historical_format() -> None:
    subject_id = uuid4()
    snapshot_id = "policy-snapshot-abc"

    key = collection_idempotency_key(subject_id, snapshot_id, 3)

    assert key == f"source.collect:{subject_id}:all:{snapshot_id}:3"


async def test_referenced_evidence_is_bounded_inherited_and_idempotent(
    tmp_path: Path,
) -> None:
    factory = InMemoryCollectionUnitOfWorkFactory()
    subject = selected_subject(
        factory,
        (
            "https://one.example/report",
            "https://two.example/report",
            "https://three.example/report",
        ),
    )
    app = service(factory, Transport([]), tmp_path / "blobs")
    parents = await app.initialize(subject.id)

    resources = tuple(
        ReferencedEvidence(
            parent_source_collection_id=parent.id,
            url=f"https://resources.example/{parent.id}/{number}.json",
            anchor_text=f"IOC {number}",
        )
        for parent in parents
        for number in range(8)
    )

    reused = await app.add_referenced_evidence(
        subject.id,
        (
            ReferencedEvidence(
                parent_source_collection_id=parents[0].id,
                url=parents[1].canonical_url,
                anchor_text="already a publication",
            ),
        ),
    )
    assert reused == []

    added = await app.add_referenced_evidence(subject.id, resources)

    assert len(added) == 20
    assert all(item.origin_kind is SourceOriginKind.REFERENCED_EVIDENCE for item in added)
    assert all(item.parent_source_collection_id is not None for item in added)
    assert all(item.source_tlp is TLP.AMBER for item in added)
    assert all(item.sensitivity == "internal" for item in added)
    assert all(not item.external_llm_allowed for item in added)
    assert all(not item.do_not_submit for item in added)
    assert [
        sum(item.parent_source_collection_id == parent.id for item in added) for parent in parents
    ] == [8, 8, 4]

    assert await app.add_referenced_evidence(subject.id, resources) == []
