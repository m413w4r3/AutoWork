from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from cti_app.application.acquisition_discovery import ThirdPartyExtractionStrategy
from cti_app.application.acquisition_fallbacks import (
    AcquisitionProbe,
    AcquisitionRequest,
    BrowserDomStrategy,
    BrowserNetworkStrategy,
    CapabilityPlaceholderStrategy,
    FeedStrategy,
    PublisherApiStrategy,
    StructuredDataStrategy,
    _PublisherPdfAdapter,
    default_acquisition_strategies,
    derived_collected_response,
)
from cti_app.application.browser_acquisition import BrowserObservation, CapturedJson
from cti_app.application.collection import SubjectCollectionService
from cti_app.application.http_collection import (
    CollectedResponse,
    CollectionPolicy,
    SafeHttpCollector,
    UnsafeAddressError,
    _detect_mime,
)
from cti_app.domain.acquisition import (
    TLP,
    AcquisitionCapabilityState,
    AcquisitionObservationKind,
    AcquisitionOutcome,
    AcquisitionPolicyContext,
    AcquisitionPolicyDecision,
    SourceEquivalenceClass,
)
from cti_app.domain.collection import CollectionState, DetectedMimeType, SourceAcquisitionMethod
from cti_app.infrastructure.blob_storage.filesystem import FilesystemBlobStore
from tests.collection_support import InMemoryCollectionUnitOfWorkFactory
from tests.test_acquisition_fallbacks import _ARTICLE, _BLOCKED, Resolver, Transport
from tests.test_article_extraction import PDF_FIXTURE
from tests.test_collection import selected_subject

ARTICLE_URL = "https://publisher.example/articles/synthetic-incident"
ARTICLE_TITLE = "Synthetic Incident Report"
ARTICLE_TEXT = (
    "The synthetic incident report describes a controlled investigation into a public test system. "
    "Analysts recorded the timeline, affected components, indicators, and defensive actions. "
    "The report includes reserved IOC values such as 203.0.113.77 and "
    "MiXeD.Example for validation. "
    "Every observation was produced from offline fixtures and no external system was contacted. "
) * 2


def _article_html(url: str = ARTICLE_URL, *, text: str = ARTICLE_TEXT) -> bytes:
    value = {
        "@context": "https://schema.org",
        "@type": "NewsArticle",
        "headline": ARTICLE_TITLE,
        "articleBody": text,
        "mainEntityOfPage": {"@id": url},
        "datePublished": "2026-03-01T10:00:00Z",
    }
    return (
        "<html><head><title>"
        + ARTICLE_TITLE
        + "</title><script type='application/ld+json'>"
        + json.dumps(value)
        + "</script></head><body><article><h1>"
        + ARTICLE_TITLE
        + "</h1><p>"
        + text
        + "</p></article></body></html>"
    ).encode()


def _response(
    url: str,
    body: bytes,
    *,
    content_type: str = "text/html",
    status: int = 200,
) -> CollectedResponse:
    digest = hashlib.sha256(body).hexdigest()
    return CollectedResponse(
        requested_url=url,
        final_url=url,
        redirect_chain=(),
        status=status,
        headers={"content-type": content_type},
        declared_content_type=content_type,
        detected_content_type=_detect_mime(body, allow_binary=True, allow_images=True),
        encoded_body=body,
        decoded_body=body,
        encoded_size=len(body),
        encoded_sha256=digest,
        decoded_size=len(body),
        decoded_sha256=digest,
        content_encoding="identity",
        acquired_at=datetime(2026, 3, 1, tzinfo=UTC),
    )


def _policy(
    *,
    external_llm_allowed: bool = True,
    tlp: TLP = TLP.CLEAR,
    allowed_domains: frozenset[str] = frozenset(),
) -> AcquisitionPolicyContext:
    return AcquisitionPolicyContext(
        allowed_domains=allowed_domains,
        blocked_domains=frozenset(),
        external_llm_allowed=external_llm_allowed,
        tlp=tlp,
        do_not_submit=False,
        expected_languages=("en",),
        allowed_syndication_domains=frozenset(),
        third_party_provider_allowed=False,
        provider_configured=False,
    )


def _request(
    fetch,
    *,
    requested_url: str = ARTICLE_URL,
    title: str | None = ARTICLE_TITLE,
    response_body: bytes | None = None,
    response_content_type: str | None = None,
    response_status: int | None = None,
    response_headers: dict[str, str] | None = None,
    policy: AcquisitionPolicyContext | None = None,
    attempted: frozenset[str] = frozenset(),
) -> AcquisitionRequest:
    return AcquisitionRequest(
        requested_url=requested_url,
        title=title,
        published_at=None,
        response_body=response_body,
        response_status=response_status,
        response_content_type=response_content_type,
        response_headers=response_headers or {},
        link_header=None,
        fetch=fetch,
        attempted_strategy_urls=attempted,
        min_text_chars=200,
        expected_languages=("en",),
        policy=policy or _policy(),
    )


class FakeBrowser:
    def __init__(self, observation: BrowserObservation, *, error: bool = False) -> None:
        self.observation = observation
        self.error = error
        self.calls: list[str] = []

    async def navigate(self, url: str) -> BrowserObservation:
        self.calls.append(url)
        if self.error:
            raise RuntimeError("offline fake renderer")
        return self.observation


def test_registry_order_and_capability_states_are_explicit() -> None:
    strategies = default_acquisition_strategies()
    names = [strategy.name for strategy in strategies]

    assert names == [
        "http_plain",
        "http_standard_headers",
        "http_same_site_referer",
        "http_retry_after",
        "http2",
        "curl",
        "representations",
        "rss_atom_jsonfeed",
        "publisher_public_api",
        "embedded_structured_data",
        "playwright_chromium_dom",
        "playwright_network_response",
        "playwright_embedded_state",
        "playwright_firefox",
        "playwright_webkit",
        "wayback_snapshot",
        "commoncrawl_warc",
        "memento_archive",
        "archive_ph",
        "exact_title_search",
        "publisher_alternative_url",
        "authorized_syndication",
        "publisher_pdf",
        "visual_ocr",
        "official_newsletter_archive",
        "third_party_extraction",
        "bridge_source_discovery",
        "bridge_transcription",
    ]
    by_name = {strategy.name: strategy for strategy in strategies}
    assert (
        by_name["playwright_chromium_dom"].capability_state
        is AcquisitionCapabilityState.BLOCKED_BY_SECURITY
    )
    assert (
        by_name["playwright_firefox"].capability_state is AcquisitionCapabilityState.NOT_IMPLEMENTED
    )
    assert (
        by_name["playwright_webkit"].capability_state is AcquisitionCapabilityState.NOT_IMPLEMENTED
    )
    assert (
        by_name["visual_ocr"].capability_state is AcquisitionCapabilityState.DEPENDENCY_UNAVAILABLE
    )
    assert (
        by_name["official_newsletter_archive"].capability_state
        is AcquisitionCapabilityState.DISABLED_BY_CONFIGURATION
    )
    assert (
        by_name["third_party_extraction"].capability_state
        is AcquisitionCapabilityState.DISABLED_BY_CONFIGURATION
    )
    assert (
        by_name["exact_title_search"].capability_state
        is AcquisitionCapabilityState.DISABLED_BY_CONFIGURATION
    )


@pytest.mark.asyncio
async def test_structured_json_ld_preserves_iocs_and_raw_artifact() -> None:
    body = _article_html()

    async def fetch(url, headers, timeout_seconds, allow_binary):
        del headers, timeout_seconds, allow_binary
        return _response(url, body)

    probes = await StructuredDataStrategy().acquire(_request(fetch))
    winner = next(probe for probe in probes if probe.outcome is AcquisitionOutcome.SUCCEEDED)

    assert winner.response is not None
    assert winner.response.encoded_body == body
    assert winner.response.encoded_sha256 == hashlib.sha256(body).hexdigest()
    assert (
        winner.response.decoded_sha256 == hashlib.sha256(winner.response.decoded_body).hexdigest()
    )
    assert b"203.0.113.77" in winner.response.decoded_body
    assert b"MiXeD.Example" in winner.response.decoded_body
    assert winner.observation_kind is AcquisitionObservationKind.EMBEDDED_STRUCTURED_DATA
    assert winner.equivalence_class is SourceEquivalenceClass.EXACT_ORIGINAL


@pytest.mark.asyncio
async def test_same_host_amp_variant_is_validated_against_its_fetched_url() -> None:
    amp_url = "https://publisher.example/articles/synthetic-incident/amp"
    discovery_body = (
        b"<html><head><link rel='amphtml' href='/articles/synthetic-incident/amp'>"
        b"</head><body>blocked</body></html>"
    )

    async def fetch(url, headers, timeout_seconds, allow_binary):
        del headers, timeout_seconds, allow_binary
        if url == ARTICLE_URL:
            return _response(url, discovery_body, status=403)
        assert url == amp_url
        return _response(url, _article_html(amp_url))

    probes = await StructuredDataStrategy().acquire(
        _request(fetch, response_body=discovery_body, response_content_type="text/html")
    )
    winner = next(probe for probe in probes if probe.outcome is AcquisitionOutcome.SUCCEEDED)

    assert winner.response is not None
    assert winner.response.requested_url == ARTICLE_URL
    assert winner.response.final_url == amp_url
    assert winner.equivalence_class is SourceEquivalenceClass.VERIFIED_PUBLISHER_EQUIVALENT


@pytest.mark.asyncio
async def test_embedded_state_is_extracted_as_structured_content() -> None:
    state = {
        "props": {
            "pageProps": {
                "article": {
                    "title": ARTICLE_TITLE,
                    "url": ARTICLE_URL,
                    "content": ARTICLE_TEXT,
                }
            }
        }
    }
    body = (
        f"<html><head><title>{ARTICLE_TITLE}</title></head>"
        f"<body><script id='__NEXT_DATA__'>{json.dumps(state)}</script></body></html>"
    ).encode()

    async def fetch(url, headers, timeout_seconds, allow_binary):
        del headers, timeout_seconds, allow_binary
        return _response(url, body)

    probes = await StructuredDataStrategy().acquire(_request(fetch))
    winner = next(probe for probe in probes if probe.outcome is AcquisitionOutcome.SUCCEEDED)

    assert winner.quality is not None
    assert winner.response is not None
    assert winner.response.decoded_body.decode() == ARTICLE_TEXT.strip()


@pytest.mark.asyncio
async def test_rss_full_text_entry_is_selected_and_derived() -> None:
    body = (
        "<rss version='2.0'><channel><title>Synthetic Publisher</title><item>"
        f"<title>{ARTICLE_TITLE}</title><link>{ARTICLE_URL}</link>"
        f"<description>{ARTICLE_TEXT}</description></item></channel></rss>"
    ).encode()

    async def fetch(url, headers, timeout_seconds, allow_binary):
        del headers, timeout_seconds, allow_binary
        if url.startswith("https://publisher.example/feed"):
            return _response(url, body, content_type="application/rss+xml")
        return _response(url, b"not a feed", status=404)

    request = _request(
        fetch,
        response_body=(
            b"<html><link rel='alternate' type='application/rss+xml' href='/feed/'></html>"
        ),
    )
    probes = await FeedStrategy(max_candidates=1).acquire(request)
    winner = next(probe for probe in probes if probe.outcome is AcquisitionOutcome.SUCCEEDED)

    assert winner.response is not None
    assert winner.response.encoded_body == body
    assert winner.response.decoded_body == ARTICLE_TEXT.strip().encode()
    assert winner.observation_kind is AcquisitionObservationKind.HTTP_RESPONSE
    assert winner.equivalence_class is SourceEquivalenceClass.VERIFIED_PUBLISHER_EQUIVALENT


@pytest.mark.asyncio
async def test_cms_public_api_candidate_is_accepted() -> None:
    api_url = "https://publisher.example/wp-json/wp/v2/posts/7"
    body = json.dumps(
        {
            "date": "2026-03-01T10:00:00",
            "link": api_url,
            "title": {"rendered": ARTICLE_TITLE},
            "content": {"rendered": f"<p>{ARTICLE_TEXT}</p>"},
        }
    ).encode()

    async def fetch(url, headers, timeout_seconds, allow_binary):
        del headers, timeout_seconds, allow_binary
        return _response(url, body, content_type="application/json")

    probes = await PublisherApiStrategy().acquire(_request(fetch, requested_url=api_url))
    winner = next(probe for probe in probes if probe.outcome is AcquisitionOutcome.SUCCEEDED)

    assert winner.response is not None
    assert winner.response.encoded_body == body
    assert winner.response.decoded_body.decode() == ARTICLE_TEXT.strip()
    assert winner.publication_date is not None


@pytest.mark.asyncio
async def test_plain_text_api_representation_is_accepted() -> None:
    body = f"{ARTICLE_TITLE}\n{ARTICLE_TEXT}".encode()

    async def fetch(url, headers, timeout_seconds, allow_binary):
        del headers, timeout_seconds, allow_binary
        return _response(url, body, content_type="text/plain")

    probes = await PublisherApiStrategy().acquire(_request(fetch))
    winner = next(probe for probe in probes if probe.outcome is AcquisitionOutcome.SUCCEEDED)

    assert winner.response is not None
    assert winner.response.decoded_body.decode() == " ".join(body.decode().split())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("external_llm_allowed", "tlp", "provider_allowed", "expected"),
    [
        (False, TLP.CLEAR, True, AcquisitionPolicyDecision.DENIED_EXTERNAL_LLM),
        (True, TLP.RED, True, AcquisitionPolicyDecision.DENIED_TLP),
        (True, TLP.CLEAR, False, AcquisitionPolicyDecision.DENIED_PROVIDER_DISCLOSURE),
    ],
)
async def test_third_party_policy_denial_makes_no_call(
    external_llm_allowed, tlp, provider_allowed, expected
) -> None:
    async def fetch(*_args):
        pytest.fail("third-party provider policy must deny before transport")

    policy = replace(
        _policy(external_llm_allowed=external_llm_allowed, tlp=tlp),
        third_party_provider_allowed=provider_allowed,
        provider_configured=True,
    )
    strategy = ThirdPartyExtractionStrategy(
        provider_url="https://provider.example/extract",
        provider_token="fixture-token",
        auth_transport_supported=True,
    )

    probes = await strategy.acquire(_request(fetch, policy=policy))

    assert probes[0].outcome is AcquisitionOutcome.POLICY_SKIPPED
    assert probes[0].policy_decision is expected


@pytest.mark.asyncio
async def test_browser_dom_success_keeps_initial_bytes_and_browser_observation_kind() -> None:
    initial = b"<html><body>blocked response</body></html>"
    observation = BrowserObservation(
        rendered_html=_article_html().decode(),
        final_url=ARTICLE_URL,
        status=200,
        captured_json=(),
        challenge_detected=False,
        paywall_detected=False,
        engine="chromium",
        duration_ms=2,
    )
    adapter = FakeBrowser(observation)
    strategy = BrowserDomStrategy(adapter=adapter, navigation_enabled=True)
    request = _request(
        lambda *_args: pytest.fail("browser uses only the configured sidecar"),
        response_body=initial,
        response_content_type="text/html",
    )

    probes = await strategy.acquire(request)
    winner = next(probe for probe in probes if probe.outcome is AcquisitionOutcome.SUCCEEDED)

    assert adapter.calls == [ARTICLE_URL]
    assert winner.response is not None
    assert winner.response.encoded_body == initial
    assert ARTICLE_TEXT.strip().encode() in winner.response.decoded_body
    assert winner.observation_kind is AcquisitionObservationKind.BROWSER_DOM


@pytest.mark.asyncio
async def test_browser_network_json_succeeds_after_dom_candidate_rejection() -> None:
    captured_body = _article_html()
    captured = CapturedJson(
        url=ARTICLE_URL,
        content_type="text/html",
        body=captured_body,
        sha256=hashlib.sha256(captured_body).hexdigest(),
        size=len(captured_body),
    )
    observation = BrowserObservation(
        rendered_html=(
            "<html><head><title>Access denied</title></head><body>Access denied</body></html>"
        ),
        final_url=ARTICLE_URL,
        status=200,
        captured_json=(captured,),
        challenge_detected=False,
        paywall_detected=False,
        engine="chromium",
        duration_ms=3,
    )
    adapter = FakeBrowser(observation)
    request = _request(
        lambda *_args: pytest.fail("browser uses only the configured sidecar"),
        response_body=b"<html>blocked</html>",
        response_content_type="text/html",
    )
    dom = await BrowserDomStrategy(adapter=adapter, navigation_enabled=True).acquire(request)
    network = await BrowserNetworkStrategy(adapter=adapter, navigation_enabled=True).acquire(
        request
    )

    assert dom[0].outcome is AcquisitionOutcome.REJECTED
    winner = next(probe for probe in network if probe.outcome is AcquisitionOutcome.SUCCEEDED)
    assert winner.response is not None
    assert winner.observation_kind is AcquisitionObservationKind.BROWSER_NETWORK_JSON


@pytest.mark.asyncio
async def test_browser_navigation_disabled_or_unavailable_fails_closed() -> None:
    observation = BrowserObservation("", ARTICLE_URL, 200, (), False, False, "chromium", 1)
    adapter = FakeBrowser(observation, error=True)
    request = _request(lambda *_args: pytest.fail("unexpected network fetch"))

    disabled = await BrowserDomStrategy(adapter=None, navigation_enabled=False).acquire(request)
    unavailable = await BrowserDomStrategy(adapter=adapter, navigation_enabled=True).acquire(
        request
    )

    assert disabled[0].outcome is AcquisitionOutcome.SKIPPED
    assert disabled[0].reason_code == "browser_navigation_disabled"
    assert disabled[0].capability_state is AcquisitionCapabilityState.BLOCKED_BY_SECURITY
    assert unavailable[0].outcome is AcquisitionOutcome.FAILED
    assert adapter.calls == [ARTICLE_URL]


@pytest.mark.asyncio
async def test_publisher_pdf_keeps_pdf_bytes_and_extracted_text() -> None:
    pdf_url = "https://publisher.example/articles/synthetic-incident.pdf"
    page = f"<html><body><a href='{pdf_url}'>Download PDF</a></body></html>".encode()

    async def fetch(url, headers, timeout_seconds, allow_binary):
        del headers, timeout_seconds
        assert allow_binary
        return _response(url, PDF_FIXTURE, content_type="application/pdf")

    request = _request(
        fetch,
        response_body=page,
        response_content_type="text/html",
    )
    probe = await _PublisherPdfAdapter().acquire(request)
    winner = next((item for item in probe if item.outcome is AcquisitionOutcome.SUCCEEDED), None)
    assert winner is not None, [(item.outcome, item.reason_code) for item in probe]

    assert winner.response is not None
    assert winner.response.encoded_body == PDF_FIXTURE
    assert winner.response.encoded_sha256 == hashlib.sha256(PDF_FIXTURE).hexdigest()
    assert winner.response.declared_content_type == "application/pdf"
    assert winner.response.detected_content_type is DetectedMimeType.TEXT
    assert b"Synthetic incident report" in winner.response.decoded_body
    assert winner.observation_kind is AcquisitionObservationKind.PUBLISHER_PDF


@pytest.mark.asyncio
async def test_disabled_and_not_configured_capabilities_do_not_use_fetch() -> None:
    async def fetch(*_args):
        pytest.fail("a disabled or unconfigured strategy must not fetch")

    request = _request(fetch)
    strategies = default_acquisition_strategies(
        flags={"third_party_extraction": True},
        third_party_provider_enabled=False,
    )
    selected = {
        strategy.name: strategy
        for strategy in strategies
        if strategy.name in {"third_party_extraction", "visual_ocr", "exact_title_search"}
    }

    for strategy in selected.values():
        assert strategy.capability_state in {
            AcquisitionCapabilityState.DISABLED_BY_CONFIGURATION,
            AcquisitionCapabilityState.DEPENDENCY_UNAVAILABLE,
        }
        probes = await strategy.acquire(request)
        assert probes
        assert probes[0].outcome in {
            AcquisitionOutcome.SKIPPED,
            AcquisitionOutcome.NOT_CONFIGURED,
        }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("external_llm_allowed", "tlp"),
    [(False, TLP.CLEAR), (True, TLP.RED)],
)
async def test_search_policy_denials_make_no_external_call(external_llm_allowed, tlp) -> None:
    async def fetch(*_args):
        pytest.fail("search payload must be withheld by policy")

    strategy = next(
        item
        for item in default_acquisition_strategies(
            exact_title_search_endpoint="https://search.example/query"
        )
        if item.name == "exact_title_search"
    )
    probe = await strategy.acquire(
        _request(
            fetch,
            policy=_policy(external_llm_allowed=external_llm_allowed, tlp=tlp),
        )
    )

    assert probe[0].outcome is AcquisitionOutcome.POLICY_SKIPPED
    assert probe[0].policy_decision in {
        AcquisitionPolicyDecision.DENIED_EXTERNAL_LLM,
        AcquisitionPolicyDecision.DENIED_TLP,
    }


@pytest.mark.asyncio
async def test_unimplemented_capability_does_not_consume_collection_attempt_budget(
    tmp_path,
) -> None:
    class WinnerStrategy:
        name = "offline_winner"
        version = "1"
        method = SourceAcquisitionMethod.EMBEDDED_STRUCTURED_DATA
        enabled = True
        timeout_seconds = 1.0

        async def acquire(self, request):
            response = _response(request.requested_url, _ARTICLE)
            return (
                AcquisitionProbe(
                    strategy_name=self.name,
                    strategy_version=self.version,
                    method=self.method,
                    requested_url=request.requested_url,
                    outcome=AcquisitionOutcome.SUCCEEDED,
                    reason_code=None,
                    response=response,
                    http_status=response.status,
                    encoded_size=response.encoded_size,
                    encoded_sha256=response.encoded_sha256,
                    duration_ms=0,
                ),
            )

    factory = InMemoryCollectionUnitOfWorkFactory()
    subject = selected_subject(factory, (ARTICLE_URL,))
    app = SubjectCollectionService(
        factory,
        SafeHttpCollector(Transport([_BLOCKED]), Resolver(), CollectionPolicy()),
        FilesystemBlobStore(tmp_path / "blobs"),
        fallback_strategies=(
            CapabilityPlaceholderStrategy(
                name="future_engine",
                method=SourceAcquisitionMethod.PLAYWRIGHT_FIREFOX,
                capability=AcquisitionCapabilityState.NOT_IMPLEMENTED,
                reason="not_implemented",
            ),
            WinnerStrategy(),
        ),
        acquisition_max_attempts=1,
    )
    source = (await app.initialize(subject.id))[0]
    factory.collections[source.id].source_candidate_id = None

    result = await app.archive_one(source.id, uuid4())

    assert result is CollectionState.ARCHIVED
    attempts = [
        event.payload["strategy_name"]
        for event in factory.provenance
        if event.event_type == "source.acquisition_fallback_attempted"
    ]
    assert "future_engine" in attempts
    assert "offline_winner" in attempts


def test_derived_collected_response_uses_raw_and_derived_hashes_and_utc() -> None:
    raw = b"<html>observed</html>"
    derived = ARTICLE_TEXT.encode()
    response = derived_collected_response(
        requested_url=ARTICLE_URL,
        final_url=ARTICLE_URL,
        raw_body=raw,
        raw_content_type="text/html; charset=utf-8",
        derived_body=derived,
        derived_content_type="text/plain",
        status=200,
        headers={"content-type": "text/html"},
        acquired_at=datetime(2026, 3, 1),
    )

    assert response.encoded_body == raw
    assert response.encoded_sha256 == hashlib.sha256(raw).hexdigest()
    assert response.decoded_body == derived
    assert response.decoded_sha256 == hashlib.sha256(derived).hexdigest()
    assert response.declared_content_type == "text/html; charset=utf-8"
    assert response.detected_content_type is DetectedMimeType.TEXT
    assert response.content_encoding == "identity"
    assert response.acquired_at.tzinfo is UTC


@pytest.mark.asyncio
async def test_credential_like_direct_failure_persists_only_safe_origin(tmp_path) -> None:
    requested_url = "https://publisher.example/story?api_key=secret"

    class CredentialRejectingCollector:
        policy = CollectionPolicy()

        async def fetch(self, url, *, cancellation_check=None):
            del cancellation_check
            raise UnsafeAddressError(f"credential-like URL rejected: {url}")

    factory = InMemoryCollectionUnitOfWorkFactory()
    subject = selected_subject(factory, (requested_url,))
    app = SubjectCollectionService(
        factory,
        CredentialRejectingCollector(),
        FilesystemBlobStore(tmp_path / "blobs"),
        fallback_strategies=(),
    )
    source = (await app.initialize(subject.id))[0]

    state = await app.archive_one(source.id, uuid4())
    attempt = (await app.attempts(source.id))[-1]
    diagnostics = await app._failure_diagnostics([source])
    persisted_audit = repr((attempt, diagnostics, factory.provenance))

    assert state is CollectionState.BLOCKED
    assert attempt.requested_url == "https://publisher.example/"
    assert attempt.failure_reason == "credential-like URL rejected"
    assert diagnostics[0]["requested_url"] == "https://publisher.example/"
    assert "secret" not in persisted_audit
    assert "api_key" not in persisted_audit
