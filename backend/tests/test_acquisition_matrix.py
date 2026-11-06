"""Offline acquisition regression matrix.

Cases 14 and 18 are partial: the acquisition contract exposes TLP and
``external_llm_allowed`` but no explicit G2/G3 authority state, and the in-memory
unit of work cannot model PostgreSQL row locks. Their tests cover the available
TLP/external-permission and service lease gates, respectively.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from cti_app.application.acquisition_fallbacks import (
    AcquisitionRequest,
    AlternateRepresentationStrategy,
    BridgeSourceDiscoveryStrategy,
    BridgeTranscriptionStrategy,
    BrowserDomStrategy,
    BrowserNetworkStrategy,
    CommonCrawlStrategy,
    FeedStrategy,
    PinnedHttpProfileStrategy,
    WaybackStrategy,
    _PublisherPdfAdapter,
    _VisualOcrAdapter,
)
from cti_app.application.acquisition_orchestrator import ArticleAcquisitionOrchestrator
from cti_app.application.browser_acquisition import BrowserObservation, CapturedJson
from cti_app.application.collection import SubjectCollectionService
from cti_app.application.http_collection import (
    CollectedResponse,
    CollectionError,
    CollectionPolicy,
    RawHttpResponse,
    SafeHttpCollector,
)
from cti_app.domain.acquisition import (
    AcquisitionObservationKind,
    AcquisitionOutcome,
    AcquisitionPolicyContext,
    AcquisitionPolicyDecision,
    EvidenceClass,
    SourceEquivalenceClass,
)
from cti_app.domain.classification import TLP
from cti_app.domain.collection import (
    CollectionFailureReason,
    CollectionState,
    DetectedMimeType,
    SourceAcquisitionMethod,
)
from cti_app.domain.production_references import ProductionReferenceTier
from cti_app.domain.production_relevance import RelevanceClassification, RelevanceReasonCode
from cti_app.domain.production_routing import RoutingReasonCode, RoutingStatus
from cti_app.infrastructure.blob_storage.filesystem import FilesystemBlobStore
from tests.collection_support import InMemoryCollectionUnitOfWorkFactory
from tests.test_collection import BlockingTransport, Resolver, Transport, selected_subject
from tests.test_production_routing import (
    _extraction,
    _indicator,
    _inputs,
    _projection,
    _source,
)

_URL = "https://source.example/report"
_TITLE = "Report 1"
_ARTICLE_TEXT = (
    "The report describes ExampleRAT activity targeting organizations in the region. "
    "Operators used c2.example for command traffic and distributed a loader through "
    "malicious documents. Investigators observed persistence, credential theft, and "
    "staged collection of system information across several affected networks. "
    "The publication provides technical context, timelines, and mitigation guidance. "
    "The analysis identifies related infrastructure and explains the observed behavior. "
) * 2
_ARTICLE = (
    "<!doctype html><html><head><title>Report 1</title></head><body><article>"
    f"<h1>{_TITLE}</h1><p>{_ARTICLE_TEXT}</p></article></body></html>"
).encode()
_BLOCKED_BODY = (
    b"<!doctype html><html><head><title>Access denied</title>"
    b'<link rel="amphtml" href="/amp/"></head><body>Access denied</body></html>'
)
_BLOCKED = RawHttpResponse(403, {"content-type": "text/html"}, _BLOCKED_BODY)


def _collected(
    url: str,
    body: bytes,
    *,
    content_type: str = "text/html",
    status: int = 200,
) -> CollectedResponse:
    digest = hashlib.sha256(body).hexdigest()
    mime = {
        "application/json": DetectedMimeType.JSON,
        "application/pdf": DetectedMimeType.PDF,
        "text/plain": DetectedMimeType.TEXT,
        "image/png": DetectedMimeType.PNG,
        "image/jpeg": DetectedMimeType.JPEG,
    }.get(content_type.split(";", 1)[0], DetectedMimeType.HTML)
    return CollectedResponse(
        requested_url=url,
        final_url=url,
        redirect_chain=(),
        status=status,
        headers={"content-type": content_type},
        declared_content_type=content_type,
        detected_content_type=mime,
        encoded_body=body,
        decoded_body=body,
        encoded_size=len(body),
        encoded_sha256=digest,
        decoded_size=len(body),
        decoded_sha256=digest,
        content_encoding="identity",
        acquired_at=datetime.now(UTC),
    )


def _request(
    fetch,
    *,
    url: str = _URL,
    title: str = _TITLE,
    response_body: bytes | None = None,
    content_type: str | None = None,
    captured_json: tuple[CapturedJson, ...] = (),
) -> AcquisitionRequest:
    policy = AcquisitionPolicyContext(
        allowed_domains=frozenset(),
        blocked_domains=frozenset(),
        external_llm_allowed=True,
        tlp=TLP.CLEAR,
        do_not_submit=True,
        expected_languages=("en",),
        allowed_syndication_domains=frozenset(),
        third_party_provider_allowed=False,
        provider_configured=False,
    )
    return AcquisitionRequest(
        requested_url=url,
        title=title,
        published_at=None,
        response_body=response_body,
        response_status=403 if response_body else None,
        response_content_type=content_type,
        response_headers={"content-type": content_type} if content_type else {},
        link_header=None,
        fetch=fetch,
        policy=policy,
    )


class _Gateway:
    def __init__(self, output: str) -> None:
        self.output = output
        self.requests: list[object] = []
        self.run: SimpleNamespace | None = None

    async def get_run(self, run_id):
        del run_id
        return self.run

    async def reconcile_bridge_submission(self, run_id):
        del run_id
        return "not_submitted"

    async def research(self, request):
        self.requests.append(request)
        raw_hash = hashlib.sha256(self.output.encode("utf-8")).hexdigest()
        self.run = SimpleNamespace(
            id=request.run_id,
            status=SimpleNamespace(value="succeeded"),
            submission_state=SimpleNamespace(value="captured_output"),
            bridge_request_id="bridge-request-1",
            response_id="bridge-response-1",
            raw_output_sha256=raw_hash,
            duration_ms=1,
        )
        return SimpleNamespace(run=self.run, output_text=self.output)


class _Navigator:
    def __init__(self, observation: BrowserObservation | None = None, *, fail: bool = False):
        self.observation = observation
        self.fail = fail
        self.calls: list[str] = []

    async def navigate(self, url: str) -> BrowserObservation:
        self.calls.append(url)
        if self.fail:
            raise RuntimeError("offline browser failure")
        assert self.observation is not None
        return self.observation


async def _service(
    tmp_path,
    *,
    responses: list[RawHttpResponse],
    strategies,
    gateway=None,
    external_llm_allowed: bool = False,
    source_url: str = _URL,
    syndication_domains: str = "",
    tlp: TLP | None = None,
):
    factory = InMemoryCollectionUnitOfWorkFactory()
    subject = selected_subject(factory, (source_url,))
    transport = Transport(responses)
    service = SubjectCollectionService(
        factory,
        SafeHttpCollector(transport, Resolver(), CollectionPolicy()),
        FilesystemBlobStore(tmp_path / "blobs"),
        model_gateway=gateway,
        fallback_strategies=strategies,
        syndication_domains=syndication_domains,
    )
    source = (await service.initialize(subject.id))[0]
    stored = factory.collections[source.id]
    stored.external_llm_allowed = external_llm_allowed
    stored.title = _TITLE
    stored.publisher = "Research team"
    if tlp is not None:
        stored.source_tlp = tlp
    return factory, service, stored, transport


def _location_output(url: str) -> str:
    return (
        "<<<PUBLIC_ARTICLE_LOCATIONS>>>\n"
        "title | publisher | date | URL\n"
        f"{_TITLE} | Research team | UNAVAILABLE | {url}\n"
        "<<<END_PUBLIC_ARTICLE_LOCATIONS>>>"
    )


def _transcription_output() -> str:
    return (
        "<<<ARTICLE_TRANSCRIPTION>>>\n"
        f"TITLE: {_TITLE}\nSOURCE_URL: {_URL}\nPUBLISHER: Research team\n"
        f"CONTENT:\n{_ARTICLE_TEXT}\n<<<END_ARTICLE_TRANSCRIPTION>>>\n"
        "<<<IOC_CANDIDATES>>>\n"
        "domain | c2.example | command traffic\n"
        "<<<END_IOC_CANDIDATES>>>"
    )


@pytest.mark.asyncio
async def test_case_01_direct_http_success_does_not_enter_fallbacks(tmp_path):
    factory, app, source, transport = await _service(
        tmp_path,
        responses=[RawHttpResponse(200, {"content-type": "text/html"}, _ARTICLE)],
        strategies=(),
    )
    state = await app.archive_one(source.id, uuid4())
    assert state is CollectionState.ARCHIVED
    assert len(transport.requests) == 1
    assert not any(
        event.event_type == "source.acquisition_fallback_attempted" for event in factory.provenance
    )


@pytest.mark.asyncio
async def test_case_02_amp_representation_recovers_direct_failure(tmp_path):
    factory, app, source, transport = await _service(
        tmp_path,
        responses=[
            _BLOCKED,
            RawHttpResponse(200, {"content-type": "text/html"}, _ARTICLE),
        ],
        strategies=(AlternateRepresentationStrategy(),),
    )
    state = await app.archive_one(source.id, uuid4())
    receipt = await app.archive_receipt(factory.collections[source.id])
    assert state is CollectionState.ARCHIVED
    assert len(transport.requests) == 2
    assert (
        receipt is not None and receipt.acquisition_method is SourceAcquisitionMethod.HTTP_VARIANT
    )
    assert transport.requests[-1].url.endswith("/amp")


@pytest.mark.asyncio
async def test_case_03_javascript_only_page_uses_fake_secure_browser_dom(tmp_path):
    navigator = _Navigator(
        BrowserObservation(_ARTICLE.decode(), _URL, 200, (), False, False, "chromium", 1)
    )
    factory, app, source, _transport = await _service(
        tmp_path,
        responses=[_BLOCKED],
        strategies=(BrowserDomStrategy(navigator, navigation_enabled=True),),
    )
    state = await app.archive_one(source.id, uuid4())
    receipt = await app.archive_receipt(factory.collections[source.id])
    assert state is CollectionState.ARCHIVED
    assert navigator.calls == [_URL]
    assert (
        receipt is not None
        and receipt.acquisition_method is SourceAcquisitionMethod.PLAYWRIGHT_CHROMIUM_DOM
    )


@pytest.mark.asyncio
async def test_case_04_empty_dom_uses_captured_public_json():
    body = json.dumps({"title": _TITLE, "url": _URL, "articleBody": _ARTICLE_TEXT}).encode()
    captured = CapturedJson(
        url=_URL,
        content_type="application/json",
        status=200,
        body=body,
        sha256=hashlib.sha256(body).hexdigest(),
        size=len(body),
    )
    observation = BrowserObservation(
        "<html><body></body></html>", _URL, 200, (captured,), False, False, "chromium", 1
    )
    navigator = _Navigator(observation)

    async def no_fetch(*_args):
        pytest.fail("browser network capture is supplied by the offline adapter")

    probes = await BrowserNetworkStrategy(navigator, navigation_enabled=True).acquire(
        _request(no_fetch, response_body=b"<html>empty</html>", content_type="text/html")
    )
    winner = next(probe for probe in probes if probe.outcome is AcquisitionOutcome.SUCCEEDED)
    assert winner.response is not None
    assert winner.observation_kind is AcquisitionObservationKind.BROWSER_NETWORK_JSON
    assert winner.response.decoded_body.decode() == _ARTICLE_TEXT.strip()


@pytest.mark.asyncio
async def test_case_05_official_rss_full_text_recovers_publisher_failure():
    feed = (
        "<rss><channel><item><title>Report 1</title>"
        f"<link>{_URL}</link><description>{_ARTICLE_TEXT}</description>"
        "</item></channel></rss>"
    ).encode()

    async def fetch(url, headers, timeout_seconds, allow_binary):
        del headers, timeout_seconds
        if url.endswith("/feed"):
            return _collected(url, feed, content_type="application/rss+xml")
        return _collected(url, b"not a feed", content_type="text/plain", status=404)

    probes = await FeedStrategy(max_candidates=2).acquire(_request(fetch))
    winner = next(probe for probe in probes if probe.outcome is AcquisitionOutcome.SUCCEEDED)
    assert winner.response is not None
    assert winner.method is SourceAcquisitionMethod.RSS_ATOM_JSONFEED
    assert winner.observation_kind is AcquisitionObservationKind.HTTP_RESPONSE
    assert winner.response.decoded_body.decode() == _ARTICLE_TEXT.strip()


@pytest.mark.asyncio
async def test_case_06_obsolete_source_recovers_from_wayback_snapshot():
    snapshot_url = "https://web.archive.org/web/20260111000000id_/https://source.example/report"

    async def fetch(url, headers, timeout_seconds, allow_binary):
        del headers, timeout_seconds, allow_binary
        if "cdx/search/cdx" in url:
            body = json.dumps(
                [["timestamp", "original", "statuscode"], ["20260111000000", _URL, "200"]]
            ).encode()
            return _collected(url, body, content_type="application/json")
        return _collected(url, _ARTICLE)

    probes = await WaybackStrategy().acquire(_request(fetch))
    winner = next(probe for probe in probes if probe.outcome is AcquisitionOutcome.SUCCEEDED)
    assert winner.requested_url == snapshot_url
    assert winner.method is SourceAcquisitionMethod.WAYBACK


@pytest.mark.asyncio
async def test_case_07_wayback_unavailable_recovers_from_common_crawl_warc():
    warc = (
        b"WARC/1.0\r\nWARC-Type: response\r\n\r\n"
        b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\n\r\n" + _ARTICLE + b"\r\n\r\n"
    )

    async def fetch(url, headers, timeout_seconds, allow_binary):
        del headers, timeout_seconds
        if "cdx/search/cdx" in url:
            raise CollectionError(
                "Wayback index unavailable",
                reason_code=CollectionFailureReason.HTTP_ERROR,
            )
        if url.endswith("collinfo.json"):
            return _collected(url, b'[{"id":"CC-MAIN-2026-20"}]', content_type="application/json")
        if "-index?" in url:
            row = {
                "offset": 0,
                "length": len(warc),
                "filename": "crawl-data/CC-MAIN-2026-20/segments/1.warc.gz",
            }
            return _collected(url, json.dumps(row).encode(), content_type="application/json")
        assert allow_binary
        return _collected(url, warc, content_type="application/octet-stream")

    result = await ArticleAcquisitionOrchestrator(
        (WaybackStrategy(), CommonCrawlStrategy()),
        deadline_seconds=10,
        max_attempts=20,
        per_domain_concurrency=2,
    ).run(_request(fetch))
    winner = result.winner
    assert winner is not None
    assert any(
        probe.method is SourceAcquisitionMethod.WAYBACK
        and probe.outcome is AcquisitionOutcome.FAILED
        for probe in result.probes
    )
    assert winner.method is SourceAcquisitionMethod.COMMON_CRAWL
    assert winner.observation_kind is AcquisitionObservationKind.HTTP_RESPONSE
    assert winner.response is not None and b"ExampleRAT" in winner.response.decoded_body


@pytest.mark.asyncio
async def test_case_08_moved_publisher_url_is_verified_as_same_publisher(tmp_path):
    gateway = _Gateway(_location_output("https://source.example/news/report-final"))
    factory, app, source, transport = await _service(
        tmp_path,
        responses=[_BLOCKED, RawHttpResponse(200, {"content-type": "text/html"}, _ARTICLE)],
        strategies=(BridgeSourceDiscoveryStrategy(), BridgeTranscriptionStrategy()),
        gateway=gateway,
        external_llm_allowed=True,
    )
    state = await app.archive_one(source.id, uuid4())
    receipt = await app.archive_receipt(factory.collections[source.id])
    assert state is CollectionState.ARCHIVED
    assert len(gateway.requests) == 1 and len(transport.requests) == 2
    assert receipt is not None
    assert receipt.acquisition_method is SourceAcquisitionMethod.DIRECT_HTTP
    assert receipt.source_equivalence_class is SourceEquivalenceClass.VERIFIED_PUBLISHER_EQUIVALENT
    assert receipt.primary_source_verified is True


@pytest.mark.asyncio
async def test_case_09_independent_copy_remains_unverified_as_primary(tmp_path):
    gateway = _Gateway(_location_output("https://copy.example/report"))
    factory, app, source, _transport = await _service(
        tmp_path,
        responses=[_BLOCKED, RawHttpResponse(200, {"content-type": "text/html"}, _ARTICLE)],
        strategies=(BridgeSourceDiscoveryStrategy(), BridgeTranscriptionStrategy()),
        gateway=gateway,
        external_llm_allowed=True,
        syndication_domains="copy.example",
    )
    assert await app.archive_one(source.id, uuid4()) is CollectionState.ARCHIVED
    receipt = await app.archive_receipt(factory.collections[source.id])
    assert receipt is not None
    assert receipt.source_equivalence_class is SourceEquivalenceClass.INDEPENDENT_CORROBORATION
    assert receipt.primary_source_verified is False


@pytest.mark.asyncio
async def test_case_10_pdf_and_image_ocr_archive_derived_text_with_provenance(tmp_path):
    from tests.test_acquisition_integration import ARTICLE_TITLE
    from tests.test_article_extraction import PDF_FIXTURE

    class FakeOcr:
        async def recognize(self, artifact):
            assert artifact.content_type == "image/png"
            return f"{_TITLE}\n{_ARTICLE_TEXT}"

    image = b"\x89PNG\r\n\x1a\nfixture image bytes"
    ocr_factory, ocr_app, ocr_source, _ = await _service(
        tmp_path / "ocr",
        responses=[RawHttpResponse(403, {"content-type": "image/png"}, image)],
        strategies=(_VisualOcrAdapter(engine=FakeOcr()),),
    )
    assert await ocr_app.archive_one(ocr_source.id, uuid4()) is CollectionState.ARCHIVED
    ocr_receipt = await ocr_app.archive_receipt(ocr_factory.collections[ocr_source.id])
    assert ocr_receipt is not None
    assert ocr_receipt.acquisition_method is SourceAcquisitionMethod.VISUAL_OCR
    assert ocr_receipt.observation_kind is AcquisitionObservationKind.OCR_TEXT
    assert ocr_receipt.evidence_class is EvidenceClass.OCR_DERIVED
    assert ocr_receipt.encoded_sha256 == hashlib.sha256(image).hexdigest()

    pdf_url = "https://source.example/files/report.pdf"
    page = f'<html><body><a href="{pdf_url}">Download PDF</a></body></html>'.encode()
    pdf_factory, pdf_app, pdf_source, _ = await _service(
        tmp_path / "pdf",
        responses=[
            RawHttpResponse(403, {"content-type": "text/html"}, page),
            RawHttpResponse(200, {"content-type": "application/pdf"}, PDF_FIXTURE),
        ],
        strategies=(_PublisherPdfAdapter(),),
    )
    pdf_source.title = ARTICLE_TITLE
    pdf_source.publisher = "Synthetic Publisher"
    assert await pdf_app.archive_one(pdf_source.id, uuid4()) is CollectionState.ARCHIVED
    pdf_receipt = await pdf_app.archive_receipt(pdf_factory.collections[pdf_source.id])
    assert pdf_receipt is not None
    assert pdf_receipt.acquisition_method is SourceAcquisitionMethod.PUBLISHER_PDF
    assert pdf_receipt.observation_kind is AcquisitionObservationKind.PUBLISHER_PDF
    assert pdf_receipt.encoded_sha256 == hashlib.sha256(PDF_FIXTURE).hexdigest()
    assert pdf_receipt.decoded_sha256 != pdf_receipt.encoded_sha256


@pytest.mark.asyncio
async def test_case_11_bridge_transcription_archives_only_unverified_model_text(tmp_path):
    gateway = _Gateway(_transcription_output())
    factory, app, source, _transport = await _service(
        tmp_path,
        responses=[_BLOCKED],
        strategies=(BridgeTranscriptionStrategy(),),
        gateway=gateway,
        external_llm_allowed=True,
    )
    assert await app.archive_one(source.id, uuid4()) is CollectionState.ARCHIVED
    receipt = await app.archive_receipt(factory.collections[source.id])
    assert receipt is not None
    assert receipt.acquisition_method is SourceAcquisitionMethod.BRIDGE_TRANSCRIPTION
    assert receipt.evidence_class is EvidenceClass.MODEL_CONTEXTUAL_UNVERIFIED
    assert receipt.observation_kind is AcquisitionObservationKind.MODEL_TRANSCRIPTION
    assert receipt.model_generated is True and receipt.primary_source_verified is False
    assert receipt.ioc_primary_verified is False


@pytest.mark.asyncio
async def test_case_12_malformed_bridge_output_stays_unresolved_with_diagnostic(tmp_path):
    gateway = _Gateway("The article is unavailable; I cannot access it.")
    factory, app, source, _transport = await _service(
        tmp_path,
        responses=[_BLOCKED],
        strategies=(BridgeTranscriptionStrategy(),),
        gateway=gateway,
        external_llm_allowed=True,
    )
    assert await app.archive_one(source.id, uuid4()) is CollectionState.BLOCKED
    assert factory.collections[source.id].source_document_id is None
    bridge_events = [
        event
        for event in factory.provenance
        if event.event_type == "source.acquisition_fallback_attempted"
        and event.payload.get("acquisition_method") == "bridge_transcription"
    ]
    assert bridge_events and bridge_events[-1].payload["outcome"] == "rejected"
    assert bridge_events[-1].payload["reason_code"] == "missing_article_label"
    bridge_attempt = (await app.attempts(source.id))[-1]
    assert bridge_attempt.reason_code is CollectionFailureReason.BRIDGE_TRANSCRIPTION_FAILED


@pytest.mark.asyncio
async def test_case_13_external_llm_disallowed_skips_without_bridge_submission(tmp_path):
    gateway = _Gateway(_transcription_output())
    factory, app, source, _transport = await _service(
        tmp_path,
        responses=[_BLOCKED],
        strategies=(BridgeTranscriptionStrategy(),),
        gateway=gateway,
        external_llm_allowed=False,
    )
    assert await app.archive_one(source.id, uuid4()) is CollectionState.BLOCKED
    assert gateway.requests == []
    bridge_attempt = next(
        event
        for event in factory.provenance
        if event.event_type == "source.acquisition_fallback_attempted"
        and event.payload.get("acquisition_method") == "bridge_transcription"
    )
    assert bridge_attempt.payload["reason_code"] == (
        "bridge_policy_denied:" + AcquisitionPolicyDecision.DENIED_EXTERNAL_LLM.value
    )


@pytest.mark.asyncio
async def test_case_14_red_tlp_blocks_bridge_disclosure_before_submission(tmp_path):
    """Partial: RED is covered; the acquisition contract has no G2/G3 state."""
    gateway = _Gateway(_transcription_output())
    factory, app, source, _transport = await _service(
        tmp_path,
        responses=[_BLOCKED],
        strategies=(BridgeTranscriptionStrategy(),),
        gateway=gateway,
        external_llm_allowed=True,
        tlp=TLP.RED,
    )
    assert await app.archive_one(source.id, uuid4()) is CollectionState.BLOCKED
    assert gateway.requests == []
    blocked = next(
        event
        for event in factory.provenance
        if event.event_type == "source.acquisition_fallback_attempted"
        and event.payload.get("acquisition_method") == "bridge_transcription"
    )
    assert blocked.payload["reason_code"] == (
        "bridge_policy_denied:" + AcquisitionPolicyDecision.DENIED_TLP.value
    )


@pytest.mark.asyncio
async def test_case_15_existing_manual_archive_cannot_be_replaced_by_fallback(tmp_path):
    gateway = _Gateway(_transcription_output())
    factory, app, source, _transport = await _service(
        tmp_path,
        responses=[],
        strategies=(BridgeTranscriptionStrategy(),),
        gateway=gateway,
        external_llm_allowed=True,
    )
    manual = await app.archive_manual_content(
        source.id,
        content=b"analyst archive content" * 30,
        declared_mime_type="text/plain",
        actor_id="analyst:test",
    )
    state = await app.archive_one(source.id, uuid4())
    assert state is CollectionState.ARCHIVED
    assert factory.collections[source.id].source_document_id == manual.source_document_id
    assert gateway.requests == []


@pytest.mark.asyncio
async def test_case_16_later_human_archive_supersedes_fallback_immutably(tmp_path):
    gateway = _Gateway(_transcription_output())
    factory, app, source, _transport = await _service(
        tmp_path,
        responses=[_BLOCKED],
        strategies=(BridgeTranscriptionStrategy(),),
        gateway=gateway,
        external_llm_allowed=True,
    )
    assert await app.archive_one(source.id, uuid4()) is CollectionState.ARCHIVED
    fallback_id = factory.collections[source.id].source_document_id
    manual = await app.archive_manual_content(
        source.id,
        content=b"human-reviewed article version" * 24,
        declared_mime_type="text/plain",
        actor_id="analyst:test",
    )
    assert fallback_id is not None and manual.source_document_id != fallback_id
    assert factory.collections[source.id].source_document_id == manual.source_document_id
    assert len(gateway.requests) == 1


@pytest.mark.asyncio
async def test_case_17_same_job_replay_has_no_duplicate_archive_or_bridge_submission(tmp_path):
    gateway = _Gateway(_transcription_output())
    factory, app, source, _transport = await _service(
        tmp_path,
        responses=[_BLOCKED],
        strategies=(BridgeTranscriptionStrategy(),),
        gateway=gateway,
        external_llm_allowed=True,
    )
    job_id = uuid4()
    assert await app.archive_one(source.id, job_id) is CollectionState.ARCHIVED
    source_document_id = factory.collections[source.id].source_document_id
    assert await app.archive_one(source.id, job_id) is CollectionState.ARCHIVED
    archives = [
        event for event in factory.provenance if event.event_type == "source.archived_fallback"
    ]
    assert len(gateway.requests) == 1 and len(archives) == 1
    assert factory.collections[source.id].source_document_id == source_document_id


@pytest.mark.asyncio
async def test_case_18_two_workers_observe_one_active_lease_and_one_archive(tmp_path):
    """Partial: verifies the service lease gate using the in-memory UOW, not PG row locks."""
    factory = InMemoryCollectionUnitOfWorkFactory()
    subject = selected_subject(factory, (_URL,))
    transport = BlockingTransport(RawHttpResponse(200, {"content-type": "text/html"}, _ARTICLE))
    app = SubjectCollectionService(
        factory,
        SafeHttpCollector(transport, Resolver(), CollectionPolicy()),
        FilesystemBlobStore(tmp_path / "blobs"),
        fallback_strategies=(),
    )
    source = (await app.initialize(subject.id))[0]
    first = asyncio.create_task(app.archive_one(source.id, uuid4()))
    await transport.started.wait()
    second = await app.archive_one(source.id, uuid4())
    transport.release.set()
    first_result = await first
    archives = [event for event in factory.provenance if event.event_type == "source.archived"]
    assert second is CollectionState.FETCHING
    assert first_result is CollectionState.ARCHIVED
    assert len(transport.requests) == 1 and len(archives) == 1


def test_case_19_core_bridge_source_forces_review_without_article_promotion():
    extraction = _extraction(indicators=(_indicator("c2.example"),))
    bridge_source = replace(
        extraction.sources[0], acquisition_method=SourceAcquisitionMethod.BRIDGE_TRANSCRIPTION
    )
    extraction = replace(extraction, sources=(bridge_source,))
    decision = _inputs(extraction, _projection(extraction))
    assert decision.status is RoutingStatus.NEEDS_REVIEW
    assert decision.reason_code is RoutingReasonCode.CRITICAL_SOURCE_MISSING
    assert decision.publication_kind is None


def test_case_20_independent_archival_evidence_remains_usable_with_core_source():
    extraction = _extraction(indicators=(_indicator("c2.example"),))
    independent = _source(
        source_id=uuid4(),
        tier=ProductionReferenceTier.SUPPORTING,
    )
    extraction = replace(extraction, sources=(*extraction.sources, independent))
    decision = _inputs(
        extraction,
        _projection(
            extraction,
            decisions={
                "c2.example": (
                    RelevanceClassification.DIRECT,
                    RelevanceReasonCode.MALICIOUS_SUBJECT_RELATION,
                )
            },
        ),
    )
    assert decision.status is RoutingStatus.DECIDED
    assert decision.reason_code is not RoutingReasonCode.CRITICAL_SOURCE_MISSING
    assert decision.confirmed_ioc_count == 1


@pytest.mark.asyncio
async def test_case_21_ssrf_destination_has_no_alternative_transport_bypass(tmp_path):
    gateway = _Gateway(_transcription_output())
    factory, app, source, transport = await _service(
        tmp_path,
        responses=[],
        strategies=(BridgeSourceDiscoveryStrategy(), BridgeTranscriptionStrategy()),
        gateway=gateway,
        external_llm_allowed=True,
        source_url="http://127.0.0.1/private",
    )
    assert await app.archive_one(source.id, uuid4()) is CollectionState.BLOCKED
    assert transport.requests == [] and gateway.requests == []
    assert not any(
        event.event_type == "source.acquisition_fallback_attempted" for event in factory.provenance
    )


@pytest.mark.asyncio
async def test_case_22_browser_egress_failure_fails_closed(tmp_path):
    navigator = _Navigator(fail=True)
    factory, app, source, _transport = await _service(
        tmp_path,
        responses=[_BLOCKED],
        strategies=(BrowserDomStrategy(navigator, navigation_enabled=True),),
    )
    assert await app.archive_one(source.id, uuid4()) is CollectionState.BLOCKED
    assert navigator.calls == [_URL]
    failed = next(
        event
        for event in factory.provenance
        if event.event_type == "source.acquisition_fallback_attempted"
        and event.payload.get("strategy_name") == "playwright_chromium_dom"
    )
    assert failed.payload["outcome"] == "failed"
    assert failed.payload["reason_code"] == "browser_unavailable"


@pytest.mark.asyncio
async def test_case_23_exhaustion_returns_blocked_with_each_strategy_diagnostic(tmp_path):
    class RecordingContext:
        def __init__(self):
            self.diagnostics: list[dict[str, object]] = []

        async def check_cancelled(self):
            return None

        async def record_diagnostics(self, value):
            self.diagnostics.append(value)

    context = RecordingContext()
    factory, app, source, _transport = await _service(
        tmp_path,
        responses=[_BLOCKED, _BLOCKED],
        strategies=(
            PinnedHttpProfileStrategy("http_plain"),
            BrowserDomStrategy(_Navigator(fail=True), navigation_enabled=True),
        ),
    )
    state = await app.archive_one(source.id, uuid4(), context=context)
    assert state is CollectionState.BLOCKED
    assert factory.collections[source.id].source_document_id is None
    assert len(context.diagnostics) == 2
    assert {item["strategy_name"] for item in context.diagnostics} == {
        "http_plain",
        "playwright_chromium_dom",
    }
    assert all(item["reason_code"] for item in context.diagnostics)
    attempts = await app.attempts(source.id)
    assert any(item.reason_code is CollectionFailureReason.FALLBACK_FAILED for item in attempts)


@pytest.mark.asyncio
async def test_case_24_ambiguous_submission_reconciles_before_any_resubmission(tmp_path):
    class ReconciliationRequired(RuntimeError):
        code = "model_submission_reconciliation_required"
        model_run_id = None

    class AmbiguousGateway(_Gateway):
        def __init__(self):
            super().__init__(_transcription_output())
            self.submissions = 0
            self.reconciliations = 0

        async def research(self, request):
            self.submissions += 1
            self.run = SimpleNamespace(
                id=request.run_id,
                status=SimpleNamespace(value="needs_review"),
                submission_state=SimpleNamespace(value="submitted"),
                bridge_request_id="submitted-but-no-response",
                response_id=None,
                raw_output_sha256=None,
            )
            raise ReconciliationRequired("simulated crash after provider submission")

        async def reconcile_bridge_submission(self, run_id):
            del run_id
            self.reconciliations += 1
            return "submission_pending"

    gateway = AmbiguousGateway()
    _factory, app, source, _transport = await _service(
        tmp_path,
        responses=[_BLOCKED],
        strategies=(BridgeTranscriptionStrategy(),),
        gateway=gateway,
        external_llm_allowed=True,
    )
    job_id = uuid4()
    assert await app.archive_one(source.id, job_id) is CollectionState.BLOCKED
    assert await app._acquire_fallback_chain(source.id, job_id, None) is CollectionState.BLOCKED
    assert gateway.submissions == 1
    assert gateway.reconciliations == 1
