from __future__ import annotations

import asyncio
import hashlib
import json
import os
from dataclasses import replace
from datetime import UTC, date, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from cti_app.application.acquisition_fallbacks import (
    AlternateRepresentationStrategy,
    ArchivePhStrategy,
    BridgeSourceDiscoveryStrategy,
    BridgeTranscriptionStrategy,
    BrowserRenderResult,
    BrowserSidecarUnavailable,
    CommonCrawlStrategy,
    MementoStrategy,
    PinnedHttpProfileStrategy,
    PlaywrightStrategy,
    SearchCacheStrategy,
    UnavailableTransportStrategy,
    WaybackStrategy,
    _CommandOcrEngine,
    article_rejection_reason,
    bridge_source_discovery_prompt,
    bridge_transcription_prompt,
    default_acquisition_strategies,
    parse_bridge_source_locations,
    parse_bridge_transcription,
)
from cti_app.application.collection import SubjectCollectionService
from cti_app.application.http_collection import (
    CollectedResponse,
    CollectionError,
    CollectionPolicy,
    RawHttpResponse,
    SafeHttpCollector,
)
from cti_app.application.production_extraction import (
    PlannedExtractionSource,
    build_canonical_source_extraction,
    build_extraction_plan,
    source_checkpoint_identity,
)
from cti_app.application.production_parsers import (
    Q2ArtifactProposal,
    Q2FactProposal,
    Q2SourceOutput,
)
from cti_app.application.production_references import observe_reference_collections
from cti_app.domain.collection import (
    CollectionFailureReason,
    CollectionState,
    DetectedMimeType,
    SourceAcquisitionMethod,
)
from cti_app.domain.discovery import SourceRole
from cti_app.domain.production import ExtractionProfile, ProductionEvidenceBasis
from cti_app.domain.production_extraction import (
    ExtractionIndicatorStatus,
    ExtractionProfileReasonCode,
    ExtractionReuseState,
    ProductionIndicatorEvidenceBasis,
    production_extraction_from_json,
    production_extraction_to_json,
)
from cti_app.domain.production_references import (
    ProductionEditorialRole,
    ProductionReferenceCorpusV1,
    ProductionReferenceKind,
    ProductionReferenceResearchStatus,
    ProductionReferenceSourceV1,
    ProductionReferenceTier,
)
from cti_app.infrastructure.blob_storage.filesystem import FilesystemBlobStore
from tests.collection_support import InMemoryCollectionUnitOfWorkFactory
from tests.test_collection import Resolver, Transport, selected_subject

_ARTICLE = (
    b"<!doctype html><html><head><title>Report 1: Campaign analysis</title></head><body>"
    b"The report describes ExampleRAT activity targeting organizations in the region. "
    b"Operators used the domain c2.example for command traffic and distributed a loader "
    b"through malicious documents. Investigators observed persistence, credential theft, "
    b"and staged collection of system information across several affected networks. "
    b"The publication provides technical context, timelines, and mitigation guidance. "
    b"The analysis identifies related infrastructure and explains the observed behavior."
    b"</body></html>"
)
_BLOCKED_WITH_AMP = (
    b"<!doctype html><html><head><title>Access denied</title>"
    b'<link rel="amphtml" href="/amp/"></head><body>Access denied</body></html>'
)
_BLOCKED = RawHttpResponse(403, {"content-type": "text/html"}, _BLOCKED_WITH_AMP)


def _collected(url: str, body: bytes, *, content_type: str = "text/html") -> CollectedResponse:
    digest = hashlib.sha256(body).hexdigest()
    return CollectedResponse(
        requested_url=url,
        final_url=url,
        redirect_chain=(),
        status=200,
        headers={"content-type": content_type},
        declared_content_type=content_type,
        detected_content_type=(
            DetectedMimeType.HTML if content_type == "text/html" else DetectedMimeType.JSON
        ),
        encoded_body=body,
        decoded_body=body,
        encoded_size=len(body),
        encoded_sha256=digest,
        decoded_size=len(body),
        decoded_sha256=digest,
        content_encoding="identity",
        acquired_at=datetime.now(UTC),
    )


def _request(fetch, *, sleep=None):
    async def no_sleep(seconds: float) -> None:
        del seconds

    return SimpleNamespace(
        requested_url="https://source.example/report",
        title="Report 1: Campaign analysis",
        published_at=date(2026, 1, 10),
        response_body=None,
        response_status=None,
        response_content_type=None,
        response_headers=None,
        link_header=None,
        fetch=fetch,
        sleep=sleep or no_sleep,
        bridge=None,
        attempted_strategy_urls=frozenset(),
        min_text_chars=200,
        expected_languages=("en",),
    )


class FakeGateway:
    def __init__(self, output: str) -> None:
        self.output = output
        self.requests: list[object] = []

    async def get_run(self, run_id):
        del run_id
        return None

    async def reconcile_bridge_submission(self, run_id):
        del run_id
        return "not_submitted"

    async def research(self, request):
        self.requests.append(request)
        raw_hash = hashlib.sha256(self.output.encode("utf-8")).hexdigest()
        run = SimpleNamespace(
            id=request.run_id,
            status=SimpleNamespace(value="succeeded"),
            bridge_request_id="bridge-consultation-1",
            response_id="bridge-response-1",
            raw_output_sha256=raw_hash,
        )
        return SimpleNamespace(run=run, output_text=self.output)


class FakeBrowserAdapter:
    def __init__(self, rendered_html: str | None = None, *, unavailable: bool = False) -> None:
        self.rendered_html = rendered_html
        self.unavailable = unavailable
        self.calls: list[tuple[str, str, float]] = []

    async def render(self, *, url: str, html: str, timeout_seconds: float):
        self.calls.append((url, html, timeout_seconds))
        if self.unavailable:
            raise BrowserSidecarUnavailable("offline fake")
        assert self.rendered_html is not None
        return BrowserRenderResult(rendered_html=self.rendered_html)


async def _service(
    tmp_path,
    *,
    transport: Transport,
    strategies,
    gateway: FakeGateway | None = None,
    external_llm_allowed: bool = False,
    source_url: str = "https://discovery.example/report",
    acquisition_max_attempts: int = 40,
):
    factory = InMemoryCollectionUnitOfWorkFactory()
    subject = selected_subject(factory, (source_url,))
    app = SubjectCollectionService(
        factory,
        SafeHttpCollector(transport, Resolver(), CollectionPolicy()),
        FilesystemBlobStore(tmp_path / "blobs"),
        model_gateway=gateway,  # type: ignore[arg-type]
        fallback_strategies=strategies,
        acquisition_max_attempts=acquisition_max_attempts,
    )
    source = (await app.initialize(subject.id))[0]
    stored = factory.collections[source.id]
    stored.source_candidate_id = None
    stored.external_llm_allowed = external_llm_allowed
    return factory, app, stored, gateway


@pytest.mark.asyncio
async def test_ordered_http_chain_archives_discovered_amp_variant(tmp_path):
    transport = Transport(
        [
            _BLOCKED,  # Direct collection
            _BLOCKED,  # Honest plain HTTP retry
            RawHttpResponse(200, {"content-type": "text/html"}, _ARTICLE),
        ]
    )
    strategies = (
        PinnedHttpProfileStrategy("http_plain"),
        AlternateRepresentationStrategy(),
    )
    factory, app, source, _gateway = await _service(
        tmp_path, transport=transport, strategies=strategies
    )

    result = await app.archive_one(source.id, uuid4())

    assert result is CollectionState.ARCHIVED
    assert [request.url for request in transport.requests] == [
        source.canonical_url,
        source.canonical_url,
        "https://discovery.example/amp",
    ]
    events = [
        event
        for event in factory.provenance
        if event.event_type == "source.acquisition_fallback_attempted"
    ]
    assert [event.payload["strategy_name"] for event in events] == [
        "http_plain",
        "representations",
    ]
    attempts = await app.attempts(source.id)
    assert [(item.strategy_name, item.strategy_version) for item in attempts[-2:]] == [
        ("http_plain", "1"),
        ("representations", "1"),
    ]
    assert attempts[-1].outcome.value == "succeeded"
    assert attempts[-1].duration_ms is not None
    assert attempts[-1].encoded_sha256 == hashlib.sha256(_ARTICLE).hexdigest()
    receipt = await app.archive_receipt(factory.collections[source.id])
    assert receipt is not None
    assert receipt.acquisition_method is SourceAcquisitionMethod.HTTP_VARIANT
    assert receipt.strategy_name == "representations"


@pytest.mark.asyncio
async def test_bridge_archive_has_non_primary_provenance_and_replay_is_idempotent(tmp_path):
    output = (
        "ARTICLE:\n"
        "Report 1 describes ExampleRAT activity targeting organizations in the region. "
        "Operators used c2.example for command traffic and distributed a loader through "
        "malicious documents. Investigators observed persistence, credential theft, and "
        "staged collection of system information across several affected networks. "
        "The publication provides technical context, timelines, and mitigation guidance. "
        "The analysis identifies related infrastructure and explains the observed behavior.\n"
        "IOCS:\n- domain: c2.example — listed as command infrastructure\n"
    )
    gateway = FakeGateway(output)
    factory, app, source, _gateway = await _service(
        tmp_path,
        transport=Transport([_BLOCKED]),
        strategies=(BridgeTranscriptionStrategy(),),
        gateway=gateway,
        external_llm_allowed=True,
    )

    result = await app.archive_one(source.id, uuid4())
    first_document_id = factory.collections[source.id].source_document_id
    replay = await app.archive_one(source.id, uuid4())

    assert result is CollectionState.ARCHIVED
    assert replay is CollectionState.ARCHIVED
    assert len(gateway.requests) == 1
    request = gateway.requests[0]
    assert request.external_llm_allowed is True
    assert "https://discovery.example/report" in request.text
    archived = next(
        event
        for event in reversed(factory.provenance)
        if event.event_type == "source.archived_fallback"
    )
    assert archived.payload["acquisition_method"] == "bridge_transcription"
    assert archived.payload["verification_status"] == "unverified_by_primary_source"
    assert archived.payload["strategy_name"] == "bridge_transcription"
    assert archived.payload["model_run_id"]
    assert archived.payload["prompt_sha256"]
    assert archived.payload["response_sha256"]
    receipt = await app.archive_receipt(factory.collections[source.id])
    assert receipt is not None
    assert receipt.acquisition_method is SourceAcquisitionMethod.BRIDGE_TRANSCRIPTION
    assert receipt.verification_status == "unverified_by_primary_source"
    assert factory.collections[source.id].source_document_id == first_document_id

    attempts = await app.attempts(source.id)
    bridge_attempt = attempts[-1]
    assert bridge_attempt.strategy_name == "bridge_transcription"
    assert bridge_attempt.encoded_sha256 == factory.documents[first_document_id].encoded_sha256


@pytest.mark.asyncio
async def test_unparseable_bridge_output_stays_blocked_and_replay_is_not_resubmitted(tmp_path):
    gateway = FakeGateway("The article is unavailable.")
    factory, app, source, _gateway = await _service(
        tmp_path,
        transport=Transport([_BLOCKED]),
        strategies=(BridgeTranscriptionStrategy(),),
        gateway=gateway,
        external_llm_allowed=True,
    )
    job_id = uuid4()

    result = await app.archive_one(source.id, job_id)
    replay = await app._acquire_fallback_chain(source.id, job_id, None)

    assert result is CollectionState.BLOCKED
    assert replay is CollectionState.BLOCKED
    assert len(gateway.requests) == 1
    events = [
        event
        for event in factory.provenance
        if event.event_type == "source.acquisition_fallback_attempted"
    ]
    assert [event.payload["outcome"] for event in events] == [
        "rejected",
        "skipped",
    ]
    assert events[0].payload["response_sha256"]
    attempts = await app.attempts(source.id)
    assert attempts[-2].reason_code.value == "bridge_transcription_failed"
    assert attempts[-1].outcome.value == "skipped"
    assert attempts[-1].strategy_name == "bridge_transcription"
    async with factory() as uow:
        reference_observation = await observe_reference_collections(
            uow, source.subject_id, (source.canonical_url,)
        )
    assert reference_observation[source.canonical_url].source_document_id is None
    assert [
        item["outcome"] for item in reference_observation[source.canonical_url].acquisition_attempts
    ] == [
        "rejected",
        "skipped",
    ]


@pytest.mark.asyncio
async def test_external_llm_disallowed_skips_bridge_with_durable_reason(tmp_path):
    gateway = FakeGateway("unused")
    factory, app, source, _gateway = await _service(
        tmp_path,
        transport=Transport([_BLOCKED]),
        strategies=(BridgeTranscriptionStrategy(),),
        gateway=gateway,
        external_llm_allowed=False,
    )

    result = await app.archive_one(source.id, uuid4())

    assert result is CollectionState.BLOCKED
    assert gateway.requests == []
    event = next(
        event
        for event in factory.provenance
        if event.event_type == "source.acquisition_fallback_attempted"
    )
    assert event.payload["outcome"] == "policy_skipped"
    assert event.payload["reason_code"] == "bridge_policy_denied:denied_external_llm"
    attempt = (await app.attempts(source.id))[-1]
    assert attempt.outcome.value == "skipped"
    assert attempt.strategy_name == "bridge_transcription"


@pytest.mark.asyncio
async def test_private_destination_block_does_not_enter_fallback_chain(tmp_path):
    gateway = FakeGateway("unused")
    transport = Transport([])
    factory, app, source, _gateway = await _service(
        tmp_path,
        transport=transport,
        strategies=(BridgeTranscriptionStrategy(),),
        gateway=gateway,
        external_llm_allowed=True,
        source_url="http://127.0.0.1/private-report",
    )

    result = await app.archive_one(source.id, uuid4())

    assert result is CollectionState.BLOCKED
    assert gateway.requests == []
    assert not any(
        event.event_type == "source.acquisition_fallback_attempted" for event in factory.provenance
    )
    assert transport.requests == []


@pytest.mark.asyncio
async def test_retry_after_is_honored_with_a_bounded_fake_delay():
    calls = 0
    delays: list[float] = []

    async def fetch(url, headers, timeout_seconds, allow_binary):
        del headers, timeout_seconds, allow_binary
        nonlocal calls
        calls += 1
        if calls == 1:
            error = CollectionError("rate limited", reason_code=CollectionFailureReason.HTTP_ERROR)
            error.http_status = 429
            error.headers = {"retry-after": "1.5"}
            raise error
        return _collected(url, _ARTICLE)

    async def sleep(seconds: float) -> None:
        delays.append(seconds)

    strategy = PinnedHttpProfileStrategy("http_retry_after", retries=2)
    probes = await strategy.acquire(_request(fetch, sleep=sleep))

    assert [probe.outcome for probe in probes] == ["failed", "succeeded"]
    assert delays == [1.5]


@pytest.mark.asyncio
async def test_playwright_strategy_uses_fake_sidecar_and_archives_rendered_dom():
    adapter = FakeBrowserAdapter(_ARTICLE.decode("utf-8"))
    request = _request(lambda *args: None)
    request.response_body = _BLOCKED_WITH_AMP
    request.response_status = 403
    request.response_content_type = "text/html"

    probes = await PlaywrightStrategy(adapter=adapter).acquire(request)

    assert probes[0].outcome == "succeeded"
    assert probes[0].method is SourceAcquisitionMethod.PLAYWRIGHT_RENDERED
    assert probes[0].http_status == 403
    assert probes[0].encoded_sha256 == hashlib.sha256(_ARTICLE).hexdigest()
    assert probes[0].response is not None
    assert probes[0].response.decoded_body == _ARTICLE
    assert adapter.calls == [("https://source.example/", _BLOCKED_WITH_AMP.decode("utf-8"), 45.0)]


@pytest.mark.asyncio
async def test_playwright_sidecar_unavailable_is_a_recordable_skip():
    adapter = FakeBrowserAdapter(unavailable=True)
    request = _request(lambda *args: None)
    request.response_body = _ARTICLE
    request.response_content_type = "text/html"

    probes = await PlaywrightStrategy(adapter=adapter).acquire(request)

    assert probes[0].outcome == "skipped"
    assert probes[0].reason_code == "playwright_sidecar_unavailable"


@pytest.mark.asyncio
async def test_playwright_rendered_dom_archives_with_distinct_provenance(tmp_path):
    adapter = FakeBrowserAdapter(_ARTICLE.decode("utf-8"))
    factory, app, source, _gateway = await _service(
        tmp_path,
        transport=Transport([_BLOCKED]),
        strategies=(PlaywrightStrategy(adapter=adapter),),
    )

    result = await app.archive_one(source.id, uuid4())

    assert result is CollectionState.ARCHIVED
    receipt = await app.archive_receipt(factory.collections[source.id])
    assert receipt is not None
    assert receipt.acquisition_method is SourceAcquisitionMethod.PLAYWRIGHT_RENDERED
    assert receipt.strategy_name == "playwright"
    assert receipt.strategy_sha256 == hashlib.sha256(_ARTICLE).hexdigest()
    archived = next(
        event for event in factory.provenance if event.event_type == "source.archived_fallback"
    )
    assert archived.payload["acquisition_method"] == "playwright_rendered"
    assert archived.payload["verification_status"] == "archived_http_content"


def test_playwright_registry_requires_a_configured_adapter():
    strategies = default_acquisition_strategies(flags={"playwright_chromium_dom": True})

    browser = next(
        strategy for strategy in strategies if strategy.name == "playwright_chromium_dom"
    )
    assert browser.enabled is True
    assert browser.capability_state.value == "blocked_by_security"

    configured = default_acquisition_strategies(
        flags={"playwright_chromium_dom": True},
        browser_adapter=object(),
        browser_navigation_enabled=True,
    )
    names = [strategy.name for strategy in configured]
    assert (
        configured[names.index("playwright_chromium_dom")].capability_state.value == "implemented"
    )
    assert names.index("playwright_chromium_dom") < names.index("bridge_transcription")


@pytest.mark.asyncio
async def test_wayback_archive_ph_common_crawl_and_memento_are_separate_fakeable_strategies():
    source_url = "https://source.example/report"
    snapshots = []

    async def wayback_fetch(url, headers, timeout_seconds, allow_binary):
        del headers, timeout_seconds, allow_binary
        if "cdx/search/cdx" in url:
            return _collected(
                url,
                json.dumps(
                    [
                        ["timestamp", "original", "statuscode"],
                        ["20260101000000", source_url, "200"],
                        ["20260111000000", source_url, "200"],
                    ]
                ).encode(),
                content_type="application/json",
            )
        snapshots.append(url)
        return _collected(url, _ARTICLE)

    wayback = await WaybackStrategy().acquire(_request(wayback_fetch))
    assert snapshots == [
        "https://web.archive.org/web/20260111000000id_/https://source.example/report"
    ]
    assert wayback[-1].outcome == "succeeded"

    async def archive_fetch(url, headers, timeout_seconds, allow_binary):
        del headers, timeout_seconds, allow_binary
        return _collected(url, _ARTICLE)

    archive = await ArchivePhStrategy().acquire(_request(archive_fetch))
    assert archive[0].requested_url.startswith("https://archive.ph/")
    assert archive[0].outcome == "succeeded"

    warc_record = (
        b"WARC/1.0\r\nWARC-Type: response\r\n\r\n"
        b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\n\r\n" + _ARTICLE + b"\r\n\r\n"
    )

    async def common_crawl_fetch(url, headers, timeout_seconds, allow_binary):
        del headers, timeout_seconds, allow_binary
        if url.endswith("collinfo.json"):
            body = b'[{"id":"CC-MAIN-2026-20"}]'
            return _collected(url, body, content_type="application/json")
        if "-index?" in url:
            row = {
                "offset": 0,
                "length": len(warc_record),
                "filename": "crawl-data/CC-MAIN-2026-20/segments/1.warc.gz",
            }
            return _collected(url, json.dumps(row).encode(), content_type="application/json")
        return _collected(url, warc_record, content_type="application/octet-stream")

    common_crawl = await CommonCrawlStrategy().acquire(_request(common_crawl_fetch))
    assert common_crawl[-1].outcome == "succeeded"
    assert common_crawl[-1].strategy_name == "common_crawl"

    memento_snapshot = "https://web.archive.org/web/20260110120000id_/https://source.example/report"

    async def memento_fetch(url, headers, timeout_seconds, allow_binary):
        del headers, timeout_seconds, allow_binary
        if "timetravel.mementoweb.org" in url:
            return _collected(
                url,
                json.dumps(
                    {"mementos": {"list": [{"uri": memento_snapshot, "rel": "memento"}]}}
                ).encode(),
                content_type="application/json",
            )
        return _collected(url, _ARTICLE)

    memento = await MementoStrategy().acquire(_request(memento_fetch))
    assert memento[-1].requested_url == memento_snapshot
    assert memento[-1].outcome == "succeeded"


@pytest.mark.asyncio
async def test_http2_curl_and_search_cache_report_explicit_offline_skips():
    request = _request(lambda *_args: pytest.fail("unavailable strategies must not fetch"))
    http2 = await UnavailableTransportStrategy("http2").acquire(request)
    curl = await UnavailableTransportStrategy(
        "curl", reason="curl_adapter_not_available_with_same_dns_pinning"
    ).acquire(request)
    search = await SearchCacheStrategy().acquire(request)

    assert http2[0].reason_code == "transport_not_supported_by_pinned_adapter"
    assert curl[0].reason_code == "curl_adapter_not_available_with_same_dns_pinning"
    assert search[0].reason_code == "no_supported_public_search_cache_endpoint"
    registry_names = [strategy.name for strategy in default_acquisition_strategies()]
    assert registry_names.index("representations") < registry_names.index("wayback_snapshot")
    assert registry_names.index("memento_archive") < registry_names.index("bridge_transcription")


def test_article_acceptance_rejects_language_mismatch_and_interstitial_pages():
    assert (
        article_rejection_reason(
            body=_ARTICLE,
            content_type="text/html",
            requested_url="https://source.example/report",
            expected_title="Report 1: Campaign analysis",
            headers={"content-language": "de"},
            expected_languages=("en", "fr"),
        )
        == "content_language_mismatch"
    )
    captcha = (
        b"<html><head><title>Checking your browser</title></head><body>"
        + b"Verify you are human. " * 30
        + b"</body></html>"
    )
    assert (
        article_rejection_reason(
            body=captcha,
            content_type="text/html",
            requested_url="https://source.example/report",
            expected_title="Report 1: Campaign analysis",
            expected_languages=("en",),
        )
        == "challenge_or_interstitial_page"
    )


def test_bridge_transcription_parser_keeps_ioc_candidates_out_of_article_text():
    raw = (
        "Bridge note: the following is transcribed text.\r\n"
        "```markdown\r\n"
        "<<<ARTICLE_TRANSCRIPTION>>>\r\n"
        "TITLE: Campaña técnica — informe\r\n"
        "SOURCE_URL: https://source.example/report\r\n"
        "PUBLISHER: Publisher\r\n"
        "CONTENT:\r\n"
        "The report describes a campaign with exact observed infrastructure.\r\n"
        "The domain [C2].Example and URL https://C2[.]example/Path, were visible.\r\n"
        "<<<END_ARTICLE_TRANSCRIPTION>>>\r\n"
        "<<<IOC_CANDIDATES>>>\r\n"
        "type | value | source_excerpt\r\n"
        "domain | [C2].Example | the domain [C2].Example was visible\r\n"
        "<<<END_IOC_CANDIDATES>>>\r\n"
        "```\r\n"
        "Additional explanation after the fenced format."
    )

    parsed = parse_bridge_transcription(raw)

    assert parsed.parsed is True
    assert parsed.reason is None
    assert parsed.title == "Campaña técnica — informe"
    assert parsed.publisher == "Publisher"
    assert parsed.article_text.endswith("https://C2[.]example/Path, were visible.")
    assert "source_excerpt" not in parsed.article_text
    assert len(parsed.ioc_candidates) == 1
    assert parsed.ioc_candidates[0].value == "[C2].Example"
    assert parsed.ioc_candidates[0].excerpt == "the domain [C2].Example was visible"
    assert parsed.raw_text == raw


@pytest.mark.parametrize(
    ("raw", "reason"),
    (
        ("", "missing_article_label"),
        ("ARTICLE: UNAVAILABLE", "article_unavailable"),
        ("ARTICLE: I cannot access the report.", "article_unavailable"),
        (
            "<<<ARTICLE_TRANSCRIPTION>>>\nCONTENT:\ntext",
            "malformed_article_block",
        ),
        (
            "<<<ARTICLE_TRANSCRIPTION>>>\nTITLE: title\n<<<END_ARTICLE_TRANSCRIPTION>>>",
            "missing_content_field",
        ),
    ),
)
def test_bridge_transcription_parser_rejects_unavailable_refusal_and_malformed(raw, reason):
    parsed = parse_bridge_transcription(raw)
    assert parsed.parsed is False
    assert parsed.reason == reason


def test_bridge_prompts_request_exact_transcription_and_locations_without_source_bytes():
    transcription = bridge_transcription_prompt(
        url="https://source.example/report", title="Report 1"
    )
    discovery = bridge_source_discovery_prompt(
        url="https://source.example/report",
        title="Report 1",
        publisher="Publisher",
        published_at=date(2026, 1, 10),
    )

    assert "do not invent, infer, or reconstruct" in transcription.lower()
    assert "<<<ARTICLE_TRANSCRIPTION>>>" in transcription
    assert "type | value | source_excerpt" in transcription
    assert "potentially verifiable public locations" in discovery
    assert "do not quote, summarize, or reproduce article/source bytes" in discovery
    assert "2026-01-10" in discovery and "Publisher" in discovery


@pytest.mark.asyncio
async def test_bridge_source_discovery_fetches_and_archives_only_verified_content(tmp_path):
    discovery_output = (
        "<<<PUBLIC_ARTICLE_LOCATIONS>>>\n"
        "title | publisher | date | URL\n"
        "Report 1 | Research team | UNAVAILABLE | "
        "https://discovery.example/copy\n"
        "<<<END_PUBLIC_ARTICLE_LOCATIONS>>>"
    )
    gateway = FakeGateway(discovery_output)
    transport = Transport(
        [
            _BLOCKED,
            RawHttpResponse(200, {"content-type": "text/html"}, _ARTICLE),
        ]
    )
    factory, app, source, _gateway = await _service(
        tmp_path,
        transport=transport,
        strategies=(BridgeSourceDiscoveryStrategy(), BridgeTranscriptionStrategy()),
        gateway=gateway,
        external_llm_allowed=True,
    )

    result = await app.archive_one(source.id, uuid4())

    assert result is CollectionState.ARCHIVED
    assert [request.url for request in transport.requests] == [
        source.canonical_url,
        "https://discovery.example/copy",
    ]
    assert len(gateway.requests) == 1
    assert "article/source bytes" in gateway.requests[0].text
    receipt = await app.archive_receipt(factory.collections[source.id])
    assert receipt is not None
    assert receipt.acquisition_method is SourceAcquisitionMethod.DIRECT_HTTP
    archived = next(
        event for event in factory.provenance if event.event_type == "source.archived_fallback"
    )
    assert archived.payload["source_equivalence_class"] == "verified_publisher_equivalent"
    assert archived.payload["observation_kind"] == "http_response"
    assert archived.payload["primary_source_verified"] is True
    assert archived.payload["model_run_id"]
    assert archived.payload["prompt_sha256"]
    assert archived.payload["response_sha256"]


@pytest.mark.asyncio
async def test_bridge_source_discovery_rejects_private_urls_without_fetch(tmp_path):
    gateway = FakeGateway(
        "<<<PUBLIC_ARTICLE_LOCATIONS>>>\n"
        "title | publisher | date | URL\n"
        "Report 1: Campaign analysis | Publisher | 2026-01-10 | "
        "http://127.0.0.1/private\n"
        "<<<END_PUBLIC_ARTICLE_LOCATIONS>>>"
    )
    transport = Transport([_BLOCKED])
    factory, app, source, _gateway = await _service(
        tmp_path,
        transport=transport,
        strategies=(BridgeSourceDiscoveryStrategy(),),
        gateway=gateway,
        external_llm_allowed=True,
    )

    await app.archive_one(source.id, uuid4())

    assert [request.url for request in transport.requests] == [source.canonical_url]
    events = [
        event
        for event in factory.provenance
        if event.event_type == "source.acquisition_fallback_attempted"
        and event.payload.get("acquisition_method") == "bridge_source_discovery"
    ]
    assert any(event.payload.get("reason_code") == "denied_private_destination" for event in events)


@pytest.mark.asyncio
async def test_bridge_source_discovery_respects_remaining_attempt_budget(tmp_path):
    gateway = FakeGateway(
        "<<<PUBLIC_ARTICLE_LOCATIONS>>>\n"
        "title | publisher | date | URL\n"
        "Report 1 | Research team | UNAVAILABLE | https://discovery.example/first\n"
        "Report 1 | Research team | UNAVAILABLE | https://discovery.example/second\n"
        "<<<END_PUBLIC_ARTICLE_LOCATIONS>>>"
    )
    transport = Transport(
        [
            _BLOCKED,
            RawHttpResponse(503, {"content-type": "text/plain"}, b"unavailable"),
        ]
    )
    _factory, app, source, _gateway = await _service(
        tmp_path,
        transport=transport,
        strategies=(BridgeSourceDiscoveryStrategy(), BridgeTranscriptionStrategy()),
        gateway=gateway,
        external_llm_allowed=True,
        acquisition_max_attempts=1,
    )

    await app.archive_one(source.id, uuid4())

    assert [request.url for request in transport.requests] == [
        source.canonical_url,
        "https://discovery.example/first",
    ]


def test_bridge_discovery_registry_is_real_and_precedes_transcription():
    strategies = default_acquisition_strategies(
        flags={"bridge_source_discovery": True, "bridge_transcription": True}
    )
    discovery = next(item for item in strategies if item.name == "bridge_source_discovery")
    names = [item.name for item in strategies]

    assert isinstance(discovery, BridgeSourceDiscoveryStrategy)
    assert discovery.capability_state.value == "implemented"
    assert names.index("bridge_source_discovery") < names.index("bridge_transcription")
    parsed = parse_bridge_source_locations(
        "Locations:\n- https://source.example/copy.\nhttps://source.example/copy\n"
    )
    assert parsed[0].url == "https://source.example/copy"


def test_bridge_extraction_is_unverified_and_checkpoint_scoped():
    planned = PlannedExtractionSource(
        source_document_id=uuid4(),
        canonical_url="https://source.example/report",
        content_sha256="a" * 64,
        tier=ProductionReferenceTier.CORE,
        kind=ProductionReferenceKind.PUBLICATION,
        role=SourceRole.PRIMARY,
        editorial_role=ProductionEditorialRole.PRIMARY,
        profile=ExtractionProfile.FULL,
        profile_reason_code=ExtractionProfileReasonCode.CORE_PRIMARY_SOURCE,
        profile_policy_version="production-reference-tier-v1:bridge_transcription",
        title="Report",
        collection_state=CollectionState.ARCHIVED,
        acquisition_method=SourceAcquisitionMethod.BRIDGE_TRANSCRIPTION,
    )
    output = Q2SourceOutput(
        facts=[
            Q2FactProposal(
                category="malware",
                value="ExampleRAT",
                context="Named in the transcription.",
                evidence_quote="The article describes ExampleRAT.",
            )
        ],
        artifacts=[
            Q2ArtifactProposal(
                value="c2.example",
                artifact_type="domain",
                indicator_status="confirmed_ioc",
                context="Listed in the transcription.",
                evidence_quote="The article identifies c2.example.",
            )
        ],
    )

    extraction = build_canonical_source_extraction(
        planned=planned,
        gated=output,
        checkpoint_id=None,
        reuse_state=ExtractionReuseState.FRESH,
    )
    direct_identity = source_checkpoint_identity(
        effective_evidence_sha256="b" * 64,
        profile=ExtractionProfile.FULL,
        prompt_version="full-v1",
        profile_policy_version="production-reference-tier-v1",
    )
    bridge_identity = source_checkpoint_identity(
        effective_evidence_sha256="b" * 64,
        profile=ExtractionProfile.FULL,
        prompt_version="full-v1",
        profile_policy_version="production-reference-tier-v1:bridge_transcription",
    )

    assert extraction.facts[0].evidence_basis is ProductionEvidenceBasis.UNVERIFIED_MODEL_OUTPUT
    assert (
        extraction.indicators[0].evidence_basis is ProductionEvidenceBasis.UNVERIFIED_MODEL_OUTPUT
    )
    assert extraction.indicators[0].indicator_status.value == "unverified_contextual"
    assert direct_identity != bridge_identity


@pytest.mark.parametrize(
    ("method", "role", "tier", "editorial_role", "basis"),
    (
        (
            SourceAcquisitionMethod.VISUAL_OCR,
            SourceRole.PRIMARY,
            ProductionReferenceTier.CORE,
            ProductionEditorialRole.PRIMARY,
            ProductionEvidenceBasis.SOURCE_VERIFIED,
        ),
        (
            SourceAcquisitionMethod.THIRD_PARTY_EXTRACTION,
            SourceRole.PRIMARY,
            ProductionReferenceTier.CORE,
            ProductionEditorialRole.PRIMARY,
            ProductionEvidenceBasis.SOURCE_VERIFIED,
        ),
        (
            SourceAcquisitionMethod.DIRECT_HTTP,
            SourceRole.INDEPENDENT,
            ProductionReferenceTier.SUPPORTING,
            ProductionEditorialRole.CORROBORATION,
            ProductionIndicatorEvidenceBasis.INDEPENDENT_CORROBORATION,
        ),
    ),
)
def test_context_only_extraction_methods_cannot_confirm_iocs(
    method, role, tier, editorial_role, basis
):
    planned = PlannedExtractionSource(
        source_document_id=uuid4(),
        canonical_url="https://source.example/report",
        content_sha256="a" * 64,
        tier=tier,
        kind=ProductionReferenceKind.PUBLICATION,
        role=role,
        editorial_role=editorial_role,
        profile=ExtractionProfile.FULL,
        profile_reason_code=ExtractionProfileReasonCode.CORE_PRIMARY_SOURCE,
        profile_policy_version=f"production-reference-tier-v1:{method.value}",
        title="Report",
        collection_state=CollectionState.ARCHIVED,
        acquisition_method=method,
    )
    extraction = build_canonical_source_extraction(
        planned=planned,
        gated=Q2SourceOutput(
            artifacts=[
                Q2ArtifactProposal(
                    value="c2.example",
                    artifact_type="domain",
                    indicator_status="confirmed_ioc",
                    context="Reported infrastructure.",
                    evidence_quote="The source identifies c2.example.",
                )
            ]
        ),
        checkpoint_id=None,
        reuse_state=ExtractionReuseState.FRESH,
    )

    assert extraction.indicators[0].indicator_status.value == "unverified_contextual"
    if basis is ProductionIndicatorEvidenceBasis.INDEPENDENT_CORROBORATION:
        assert extraction.indicators[0].evidence_basis is ProductionEvidenceBasis.SOURCE_VERIFIED
        assert extraction.indicators[0].corroboration_basis is basis
        from tests.test_production_routing import _extraction, _indicator

        canonical = _extraction(
            indicators=(
                _indicator(
                    "c2.example",
                    status=ExtractionIndicatorStatus.UNVERIFIED_CONTEXTUAL,
                ),
            )
        )
        source = replace(
            canonical.sources[0],
            indicators=(
                replace(
                    canonical.sources[0].indicators[0],
                    corroboration_basis=basis,
                ),
            ),
        )
        canonical = replace(canonical, sources=(source,))
        roundtrip = production_extraction_from_json(production_extraction_to_json(canonical))
        assert roundtrip.sources[0].indicators[0].corroboration_basis is basis
    else:
        assert extraction.indicators[0].evidence_basis is basis
        assert extraction.indicators[0].corroboration_basis is None


@pytest.mark.parametrize(
    ("method", "tier", "role", "editorial_role", "suffix"),
    (
        (
            SourceAcquisitionMethod.VISUAL_OCR,
            ProductionReferenceTier.CORE,
            SourceRole.PRIMARY,
            ProductionEditorialRole.PRIMARY,
            "visual_ocr",
        ),
        (
            SourceAcquisitionMethod.THIRD_PARTY_EXTRACTION,
            ProductionReferenceTier.CORE,
            SourceRole.PRIMARY,
            ProductionEditorialRole.PRIMARY,
            "third_party_extraction",
        ),
        (
            SourceAcquisitionMethod.DIRECT_HTTP,
            ProductionReferenceTier.SUPPORTING,
            SourceRole.INDEPENDENT,
            ProductionEditorialRole.CORROBORATION,
            "independent_corroboration",
        ),
    ),
)
def test_contextual_source_methods_are_in_extraction_cache_identity(
    method, tier, role, editorial_role, suffix
):
    def reference(
        url: str,
        *,
        tier: ProductionReferenceTier,
        role: SourceRole,
        editorial_role: ProductionEditorialRole,
        method: SourceAcquisitionMethod,
    ):
        source_id = uuid4()
        return ProductionReferenceSourceV1(
            canonical_url=url,
            tier=tier,
            kind=ProductionReferenceKind.PUBLICATION,
            role=role,
            title="Report",
            publisher="Publisher",
            published_at=date(2026, 1, 10),
            source_collection_id=None,
            source_document_id=source_id,
            discovery_candidate_ids=(),
            collection_state=CollectionState.ARCHIVED,
            content_sha256="b" * 64,
            relevance_reason=None,
            proposed_by_model=False,
            eligible_for_extraction=True,
            editorial_role=editorial_role,
            acquisition_method=method,
        )

    core = reference(
        "https://source.example/report",
        tier=ProductionReferenceTier.CORE,
        role=SourceRole.PRIMARY,
        editorial_role=ProductionEditorialRole.PRIMARY,
        method=(
            SourceAcquisitionMethod.DIRECT_HTTP
            if tier is ProductionReferenceTier.SUPPORTING
            else method
        ),
    )
    target = (
        reference(
            "https://independent.example/copy",
            tier=tier,
            role=role,
            editorial_role=editorial_role,
            method=method,
        )
        if tier is ProductionReferenceTier.SUPPORTING
        else None
    )
    sources = (core, target) if target is not None else (core,)
    corpus = ProductionReferenceCorpusV1(
        schema_version=1,
        subject_id=uuid4(),
        research_date=date(2026, 1, 11),
        production_input_hash="a" * 64,
        research_status=ProductionReferenceResearchStatus.COMPLETED,
        sources=sources,
        warnings=(),
    )

    plan = build_extraction_plan(corpus)
    planned = (
        next(item for item in plan.sources if item.canonical_url.endswith("copy"))
        if target
        else plan.sources[0]
    )

    assert planned.profile_policy_version.endswith(suffix)


@pytest.mark.asyncio
async def test_manual_archive_supersedes_fallback_but_replay_does_not_overwrite(tmp_path):
    transcript = (
        "ARTICLE:\nReport 1 describes ExampleRAT activity targeting organizations in the region. "
        "Operators used c2.example for command traffic and distributed a loader through "
        "malicious documents. Investigators observed persistence, credential theft, and "
        "staged collection of system information across several affected networks. "
        "The publication provides technical context, timelines, and mitigation guidance. "
        "The analysis identifies related infrastructure and explains the observed behavior."
    )
    gateway = FakeGateway(transcript)
    factory, app, source, _gateway = await _service(
        tmp_path,
        transport=Transport([_BLOCKED]),
        strategies=(BridgeTranscriptionStrategy(),),
        gateway=gateway,
        external_llm_allowed=True,
    )
    assert await app.archive_one(source.id, uuid4()) is CollectionState.ARCHIVED
    fallback_id = factory.collections[source.id].source_document_id
    assert fallback_id is not None

    await app.archive_one(source.id, uuid4())
    assert factory.collections[source.id].source_document_id == fallback_id
    assert len(gateway.requests) == 1

    human_text = ("Human-verified publication text with confirmed context. " * 12).encode()
    manual = await app.archive_manual_content(
        source.id,
        content=human_text,
        declared_mime_type="text/plain",
        actor_id="analyst:unit-test",
    )

    assert manual.source_document_id != fallback_id
    assert factory.collections[source.id].source_document_id == manual.source_document_id
    assert factory.collections[source.id].origin_kind.value == "manual"
    assert any(
        event.event_type == "source.archived_manually"
        and event.payload["source_document_id"] == str(manual.source_document_id)
        for event in factory.provenance
    )


class _FakeOcrStdout:
    def __init__(self, chunks: list[bytes], *, blocked: asyncio.Event | None = None) -> None:
        self.chunks = list(chunks)
        self.blocked = blocked
        self.read_count = 0

    async def read(self, size: int) -> bytes:
        self.read_count += 1
        if self.blocked is not None:
            await self.blocked.wait()
            return b""
        if not self.chunks:
            return b""
        chunk = self.chunks.pop(0)
        if len(chunk) > size:
            self.chunks.insert(0, chunk[size:])
            return chunk[:size]
        return chunk


class _FakeOcrStdin:
    def __init__(self) -> None:
        self.body = bytearray()
        self.closed = False

    def write(self, body: bytes) -> None:
        self.body.extend(body)

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return None


class _FakeOcrProcess:
    def __init__(self, chunks: list[bytes], *, blocked: asyncio.Event | None = None) -> None:
        self.stdout = _FakeOcrStdout(chunks, blocked=blocked)
        self.stdin = _FakeOcrStdin()
        self.returncode = 0
        self.killed = False
        self.waited = False

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    async def wait(self) -> int:
        self.waited = True
        return self.returncode


@pytest.mark.asyncio
async def test_command_ocr_uses_minimal_environment(monkeypatch) -> None:
    process = _FakeOcrProcess([b"recognized article text"])
    captured: dict[str, object] = {}

    async def fake_create_subprocess_exec(*command, **kwargs):
        captured["command"] = command
        captured["env"] = kwargs.get("env")
        return process

    monkeypatch.setenv("AUTOWORK_PROVIDER_SECRET", "not-for-child")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    result = await _CommandOcrEngine(("fake-ocr",)).recognize(
        SimpleNamespace(encoded_body=b"image bytes")
    )

    env = captured["env"]
    assert result == "recognized article text"
    assert captured["command"] == ("fake-ocr",)
    assert isinstance(env, dict)
    assert set(env) == {"PATH", "LANG", "HOME"}
    assert env["PATH"] == os.environ["PATH"]
    assert env["HOME"] == "/tmp"
    assert "AUTOWORK_PROVIDER_SECRET" not in env
    assert process.stdin.closed is True


@pytest.mark.asyncio
async def test_command_ocr_kills_child_when_streamed_output_exceeds_cap(monkeypatch) -> None:
    process = _FakeOcrProcess([b"12345678", b"9", b"unused output"])
    monkeypatch.setattr("cti_app.application.acquisition_fallbacks._MAX_DERIVED_BODY_BYTES", 8)
    monkeypatch.setattr(
        asyncio, "create_subprocess_exec", lambda *a, **kw: _return_process(process)
    )

    with pytest.raises(RuntimeError, match="ocr_output_exceeds_limit"):
        await _CommandOcrEngine(("fake-ocr",)).recognize(
            SimpleNamespace(encoded_body=b"image bytes")
        )

    assert process.killed is True
    assert process.waited is True
    assert process.stdout.chunks == [b"unused output"]


async def _return_process(process: _FakeOcrProcess) -> _FakeOcrProcess:
    return process


@pytest.mark.asyncio
async def test_command_ocr_kills_and_waits_for_child_on_cancellation(monkeypatch) -> None:
    process = _FakeOcrProcess([], blocked=asyncio.Event())
    monkeypatch.setattr(
        asyncio, "create_subprocess_exec", lambda *a, **kw: _return_process(process)
    )
    task = asyncio.create_task(
        _CommandOcrEngine(("fake-ocr",), timeout_seconds=30).recognize(
            SimpleNamespace(encoded_body=b"image bytes")
        )
    )
    await asyncio.sleep(0)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert process.killed is True
    assert process.waited is True
