from __future__ import annotations

import asyncio
import calendar
import gzip
import hashlib
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

import pytest

from cti_app.application.collection import SubjectCollectionService
from cti_app.application.http_collection import (
    CollectionPolicy,
    PinnedHttpRequest,
    RawHttpResponse,
    SafeHttpCollector,
)
from cti_app.application.jobs import JobCancelledError, JobExecutionContext, JobHandlerError
from cti_app.domain.classification import TLP
from cti_app.domain.collection import CollectionState
from cti_app.domain.discovery import CandidateTopic, DiscoveryBatch, SourceCandidate, SourceRole
from cti_app.domain.editions import Edition
from cti_app.domain.editorial import (
    CandidateReference,
    EditorialGroup,
    EditorialScore,
    EditorialType,
    GroupingConfidence,
    GroupingOutcome,
)
from cti_app.domain.entities import Subject
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
        target_major_articles=1,
        target_briefs=1,
        previous_edition_id=None,
        source_profile="default",
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
        discovery_model_run_id=uuid4(),
        structuring_model_run_id=uuid4(),
        tlp=TLP.AMBER,
        sensitivity="internal",
        external_llm_allowed=False,
    )
    subject = Subject(
        external_id=f"subject-{uuid4()}", slug=f"subject-{uuid4().hex}", tlp=TLP.AMBER
    )
    group = EditorialGroup(
        edition_id=edition.id,
        title=candidate.title,
        candidate_references=(CandidateReference(batch.id, candidate.id),),
        outcome=GroupingOutcome.NEW_SUBJECT,
        score=EditorialScore(2, 2, 2, 2, 2, 2, {"impact": "test"}),
        source_relationship_status=sources[0].relationship_status,
        needs_source_verification=True,
        needs_source_expansion=True,
        grouping_confidence=GroupingConfidence.HIGH,
        grouping_justification="test",
    )
    group.select(EditorialType.MAJOR, subject.id)
    factory.editions[edition.id] = edition
    factory.subjects[subject.id] = subject
    factory.batches[batch.id] = batch
    factory.groups[group.id] = group
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
    assert await app.list_evidence(subject.id) == ([], [])
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
        policy_snapshot_id=app.configuration_id,
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
    tenable_document = factory.documents[factory.collections[sources[0].id].source_document_id]
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
    assert factory.documents[tenable_document.id].logical_filename.startswith(
        "date-inconnue_TLP AMBER_Report 1_Research team"
    )


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


async def test_blob_store_failure_remains_systemic(tmp_path: Path) -> None:
    class FailingBlobStore(FilesystemBlobStore):
        async def put(self, source: object, *, logical_bucket: str, mime_type: str) -> object:
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
    group = next(iter(factory.groups.values()))
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
        discovery_model_run_id=uuid4(),
        structuring_model_run_id=uuid4(),
        tlp=TLP.AMBER,
        sensitivity="internal",
        external_llm_allowed=False,
    )
    factory.batches[complement.id] = complement
    group.add_candidates((CandidateReference(complement.id, complement_candidate.id),))

    collections = await app.initialize(subject.id)

    assert sorted(collection.requested_url for collection in collections) == [
        "https://one.example/report",
        "https://three.example/report",
    ]
