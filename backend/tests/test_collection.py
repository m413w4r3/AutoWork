from __future__ import annotations

import calendar
from collections.abc import Sequence
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from uuid import UUID, uuid4

import pytest

from cti_app.application.collection import SubjectCollectionService
from cti_app.application.extraction import EvidenceExtractionService, QwenEvidenceOutput
from cti_app.application.http_collection import (
    PinnedHttpRequest,
    RawHttpResponse,
    SafeHttpCollector,
)
from cti_app.application.jobs import JobExecutionContext, JobHandlerError
from cti_app.application.model_gateway import StructuredExtractionModel
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
    HumanDecisionType,
)
from cti_app.domain.entities import Subject
from cti_app.infrastructure.blob_storage.filesystem import FilesystemBlobStore
from tests.collection_support import InMemoryCollectionUnitOfWorkFactory

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


class NoopContext:
    def __init__(self, job_id: UUID) -> None:
        self.job_id = job_id
        self.progress: list[tuple[int, int]] = []

    async def report_progress(self, current: int, total: int, message: str | None = None) -> None:
        del message
        self.progress.append((current, total))

    async def heartbeat(self) -> None:
        return None


class ClaimModel:
    async def extract(self, request: object, output_schema: object) -> object:
        del request, output_schema
        return SimpleNamespace(
            structured_output=QwenEvidenceOutput.model_validate(
                {
                    "actors": [
                        {
                            "kind": "name",
                            "value": "ExampleRAT",
                            "exact_quote": "ExampleRAT uses evil[.]example",
                            "confidence": "high",
                            "uncertainty": None,
                        }
                    ]
                }
            )
        )


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
    model = cast(StructuredExtractionModel, ClaimModel()) if with_claims else None
    return SubjectCollectionService(
        factory,
        SafeHttpCollector(transport, Resolver()),
        FilesystemBlobStore(root),
        EvidenceExtractionService(model),
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
        await app.collect_one(source.id, uuid4())

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

    first = await app.collect_one(source.id, uuid4())
    second = await app.collect_one(source.id, uuid4())

    assert first is second is CollectionState.COMPLETED
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
    assert transient.value.transient is True
    assert (await app.list_sources(subject.id))[0].state is CollectionState.FAILED_RETRYABLE

    await app.collect_subject(subject.id, context.job_id, context)

    assert (await app.list_sources(subject.id))[0].state is CollectionState.COMPLETED
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
    assert states["https://one.example/report"] is CollectionState.COMPLETED
    assert states["https://missing.example/report"] is CollectionState.UNAVAILABLE
    assert len(factory.collections) == 2
    assert len(factory.attempts) == 2


async def test_selected_collect_extract_validate_scenario(tmp_path: Path) -> None:
    factory = InMemoryCollectionUnitOfWorkFactory()
    subject = selected_subject(factory, ("https://one.example/report",))
    app = service(
        factory,
        Transport([response()]),
        tmp_path / "blobs",
        with_claims=True,
    )
    source = (await app.initialize(subject.id))[0]

    assert await app.collect_one(source.id, uuid4()) is CollectionState.COMPLETED
    claims, indicators = await app.list_evidence(subject.id)
    assert claims[0].span.passage(await app.extracted_text(claims[0].derived_artifact_id)) == (
        "ExampleRAT uses evil[.]example"
    )
    assert any(item.normalized_value == "evil.example" for item in indicators)

    decision = await app.decide_claim(
        claims[0].id,
        HumanDecisionType.CLAIM_VALIDATE,
        actor_id="dev-analyst",
        correlation_id="scenario",
    )
    assert decision.payload["original_value"] == "ExampleRAT"
    assert factory.claims[claims[0].id].value == "ExampleRAT"
