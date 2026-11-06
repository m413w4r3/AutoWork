from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest

from cti_app.application.acquisition_discovery import (
    AuthorizedSyndicationStrategy,
    CapturedArtifact,
    ExactTitleSearchStrategy,
    OfficialNewsletterArchiveStrategy,
    PublisherAlternativeUrlStrategy,
    PublisherPdfStrategy,
    ThirdPartyExtractionStrategy,
    VisualOcrStrategy,
    classify_equivalence,
    external_payload_is_allowed,
    safe_external_url,
)
from cti_app.application.acquisition_fallbacks import AcquisitionRequest
from cti_app.application.http_collection import CollectedResponse, _detect_mime
from cti_app.domain.acquisition import (
    AcquisitionCapabilityState,
    AcquisitionOutcome,
    AcquisitionPolicyContext,
    EvidenceClass,
    SourceEquivalenceClass,
)
from cti_app.domain.collection import DetectedMimeType

URL = "https://publisher.example.test/articles/old-report"
TITLE = "Exact Incident Response Report"
MOVED = "https://www.publisher.example.test/articles/new-report"
ARTICLE_TEXT = (
    "The exact incident response report describes this security event, its timeline, "
    "affected systems, indicators, and remediation steps. " * 10
)
ARTICLE = (
    f"<html><head><title>{TITLE}</title></head><body><article>{ARTICLE_TEXT}</article></body></html>"
).encode()


def _policy(
    *,
    allowed_domains: frozenset[str] = frozenset(),
    allowed_syndication_domains: frozenset[str] = frozenset(),
    third_party_provider_allowed: bool = False,
    provider_configured: bool = False,
    do_not_submit: bool = False,
    tlp: str = "CLEAR",
) -> AcquisitionPolicyContext:
    return AcquisitionPolicyContext(
        allowed_domains=allowed_domains,
        blocked_domains=frozenset(),
        external_llm_allowed=True,
        tlp=tlp,
        do_not_submit=do_not_submit,
        expected_languages=(),
        allowed_syndication_domains=allowed_syndication_domains,
        third_party_provider_allowed=third_party_provider_allowed,
        provider_configured=provider_configured,
    )


def _response(
    url: str, body: bytes, *, content_type: str = "text/html", status: int = 200
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
        acquired_at=datetime(2025, 1, 1, tzinfo=UTC),
    )


def _request(
    fetch,
    *,
    body: bytes | None = None,
    requested_url: str = URL,
    title: str | None = TITLE,
    policy: AcquisitionPolicyContext | None = None,
) -> AcquisitionRequest:
    return AcquisitionRequest(
        requested_url=requested_url,
        title=title,
        published_at=None,
        response_body=body,
        response_status=200 if body else None,
        response_content_type="text/html" if body else None,
        response_headers={},
        link_header=None,
        fetch=fetch,
        min_text_chars=100,
        policy=policy or _policy(),
    )


@pytest.mark.asyncio
async def test_publisher_sitemap_detects_moved_article_by_exact_news_title() -> None:
    sitemap = (
        "<urlset xmlns='http://www.sitemaps.org/schemas/sitemap/0.9' "
        "xmlns:news='http://www.google.com/schemas/sitemap-news/0.9'>"
        f"<url><loc>{MOVED}</loc><news:news><news:title>{TITLE}</news:title></news:news></url>"
        "</urlset>"
    ).encode()
    calls: list[str] = []

    async def fetch(url, headers, timeout_seconds, allow_binary):
        calls.append(url)
        if url.endswith("sitemap.xml"):
            return _response(url, sitemap, content_type="application/xml")
        return _response(url, ARTICLE)

    probes = await PublisherAlternativeUrlStrategy().acquire(
        _request(fetch, body=b"<html><title>Old page</title></html>")
    )
    success = next(probe for probe in probes if probe.outcome is AcquisitionOutcome.SUCCEEDED)
    assert success.requested_url == MOVED
    assert success.equivalence_class is SourceEquivalenceClass.VERIFIED_PUBLISHER_EQUIVALENT
    assert calls[-1] == MOVED


@pytest.mark.asyncio
async def test_publisher_sitemap_wrong_title_match_is_rejected() -> None:
    sitemap = f"<urlset><url><loc>{MOVED}</loc></url></urlset>".encode()
    wrong = (
        b"<html><head><title>Weather forecast</title></head><body>"
        + b"Weather forecast text. " * 20
        + b"</body></html>"
    )

    async def fetch(url, headers, timeout_seconds, allow_binary):
        if url.endswith("sitemap.xml"):
            return _response(url, sitemap, content_type="application/xml")
        return _response(url, wrong)

    probes = await PublisherAlternativeUrlStrategy().acquire(
        _request(fetch, body=f'<link rel="canonical" href="{MOVED}">'.encode())
    )
    assert not any(probe.outcome is AcquisitionOutcome.SUCCEEDED for probe in probes)
    assert any(probe.reason_code in {"title_mismatch"} for probe in probes)


@pytest.mark.asyncio
async def test_publisher_alternative_rejects_cross_publisher_candidate() -> None:
    body = b'<link rel="canonical" href="https://copycat.other.test/articles/new-report">'
    calls: list[str] = []

    async def fetch(url, headers, timeout_seconds, allow_binary):
        calls.append(url)
        return _response(url, b"<urlset></urlset>", content_type="application/xml")

    probes = await PublisherAlternativeUrlStrategy().acquire(_request(fetch, body=body))
    assert not any("copycat.other.test" in url for url in calls)
    assert not any(probe.outcome is AcquisitionOutcome.SUCCEEDED for probe in probes)


@pytest.mark.asyncio
async def test_search_result_snippet_is_never_returned_as_article() -> None:
    result_url = "https://publisher.example.test/articles/old-report"
    calls: list[str] = []

    async def fetch(url, headers, timeout_seconds, allow_binary):
        calls.append(url)
        if "search.example.test" in url:
            return _response(
                url,
                json.dumps(
                    {"results": [{"url": result_url, "title": TITLE, "snippet": ARTICLE_TEXT[:90]}]}
                ).encode(),
                content_type="application/json",
            )
        return _response(url, ARTICLE)

    probes = await ExactTitleSearchStrategy("https://search.example.test/query").acquire(
        _request(fetch)
    )
    success = next(probe for probe in probes if probe.outcome is AcquisitionOutcome.SUCCEEDED)
    assert success.response is not None and success.response.decoded_body == ARTICLE
    assert result_url in calls


@pytest.mark.asyncio
async def test_search_wrong_title_false_positive_is_rejected_without_fetch() -> None:
    calls: list[str] = []

    async def fetch(url, headers, timeout_seconds, allow_binary):
        calls.append(url)
        return _response(
            url,
            json.dumps({"items": [{"url": MOVED, "title": "Sports results"}]}).encode(),
            content_type="application/json",
        )

    probes = await ExactTitleSearchStrategy("https://search.example.test/query").acquire(
        _request(fetch)
    )
    assert len(calls) == 1
    assert any(probe.reason_code == "search_result_title_mismatch" for probe in probes)
    assert not any(probe.outcome is AcquisitionOutcome.SUCCEEDED for probe in probes)


@pytest.mark.asyncio
async def test_search_disabled_without_explicit_endpoint() -> None:
    calls = 0

    async def fetch(url, headers, timeout_seconds, allow_binary):
        nonlocal calls
        calls += 1
        raise AssertionError("disabled search must not fetch")

    strategy = ExactTitleSearchStrategy()
    probe = (await strategy.acquire(_request(fetch)))[0]
    assert not strategy.enabled
    assert probe.outcome is AcquisitionOutcome.NOT_CONFIGURED
    assert probe.reason_code == "search_endpoint_not_configured"
    assert calls == 0


@pytest.mark.asyncio
async def test_syndication_is_authorized_only_when_candidate_points_to_original() -> None:
    syndication = "https://reprints.example.test/report"
    source_body = f'<a rel="alternate" href="{syndication}">reprint</a>'.encode()
    candidate = (
        f'<html><head><title>{TITLE}</title><link rel="canonical" href="{URL}"></head>'
        f"<body>{ARTICLE_TEXT}</body></html>"
    ).encode()

    async def fetch(url, headers, timeout_seconds, allow_binary):
        return _response(url, candidate)

    probes = await AuthorizedSyndicationStrategy().acquire(_request(fetch, body=source_body))
    success = next(probe for probe in probes if probe.outcome is AcquisitionOutcome.SUCCEEDED)
    assert success.equivalence_class is SourceEquivalenceClass.AUTHORIZED_REPUBLICATION
    assert success.evidence_class is EvidenceClass.AUTHORIZED_REPUBLICATION


@pytest.mark.asyncio
async def test_syndication_without_confirming_evidence_stays_unknown() -> None:
    syndication = "https://reprints.example.test/report"
    source_body = f'<a rel="alternate" href="{syndication}">reprint</a>'.encode()
    candidate = (
        f"<html><head><title>{TITLE}</title></head><body>{ARTICLE_TEXT}</body></html>".encode()
    )

    async def fetch(url, headers, timeout_seconds, allow_binary):
        return _response(url, candidate)

    probes = await AuthorizedSyndicationStrategy().acquire(_request(fetch, body=source_body))
    success = next(probe for probe in probes if probe.outcome is AcquisitionOutcome.SUCCEEDED)
    assert success.equivalence_class is SourceEquivalenceClass.UNKNOWN_MATCH
    assert success.evidence_class is EvidenceClass.UNKNOWN
    assert not success.primary_source_verified


def test_equivalence_keeps_different_publisher_as_independent_corroboration() -> None:
    result = classify_equivalence(
        URL,
        "https://another-publisher.example.org/report",
        same_publisher=False,
        title_similarity=0.98,
    )
    assert result is SourceEquivalenceClass.INDEPENDENT_CORROBORATION
    assert (
        classify_equivalence(URL, MOVED, same_publisher=True, title_similarity=1.0)
        is SourceEquivalenceClass.VERIFIED_PUBLISHER_EQUIVALENT
    )
    assert (
        classify_equivalence(URL, MOVED, same_publisher=True, title_similarity=0.2)
        is SourceEquivalenceClass.UNKNOWN_MATCH
    )
    assert (
        classify_equivalence(URL, URL + "?utm_source=x", title_similarity=1.0)
        is SourceEquivalenceClass.EXACT_ORIGINAL
    )


@pytest.mark.asyncio
async def test_publisher_pdf_preserves_observed_bytes_and_uses_injected_extractor() -> None:
    pdf = b"%PDF-1.7\nsynthetic PDF bytes\n%%EOF"
    page = b'<a href="/files/report.pdf">PDF</a>'
    seen_binary: list[bool] = []

    async def fetch(url, headers, timeout_seconds, allow_binary):
        seen_binary.append(allow_binary)
        return _response(url, pdf, content_type="application/pdf")

    async def extractor(raw: bytes) -> str:
        assert raw == pdf
        return TITLE + "\n" + ARTICLE_TEXT

    probes = await PublisherPdfStrategy(extractor=extractor).acquire(_request(fetch, body=page))
    success = next(probe for probe in probes if probe.outcome is AcquisitionOutcome.SUCCEEDED)
    assert seen_binary == [True]
    assert success.response is not None
    assert success.response.encoded_body == pdf
    assert success.response.encoded_sha256 == hashlib.sha256(pdf).hexdigest()
    assert success.response.decoded_body.decode().startswith(TITLE)
    assert success.response.detected_content_type is DetectedMimeType.TEXT


@pytest.mark.asyncio
async def test_publisher_pdf_rejects_bad_magic_before_extraction() -> None:
    calls = 0

    async def fetch(url, headers, timeout_seconds, allow_binary):
        return _response(url, b"not pdf", content_type="application/pdf")

    def extractor(raw: bytes) -> str:
        nonlocal calls
        calls += 1
        return ARTICLE_TEXT

    probes = await PublisherPdfStrategy(extractor=extractor).acquire(
        _request(fetch, body=b'<a href="/files/report.pdf">PDF</a>')
    )
    assert calls == 0
    assert probes[0].reason_code == "invalid_pdf_magic_or_trailer"


@pytest.mark.asyncio
async def test_publisher_pdf_without_extractor_is_dependency_unavailable() -> None:
    async def fetch(url, headers, timeout_seconds, allow_binary):
        raise AssertionError("no extractor means no artifact fetch")

    probe = (await PublisherPdfStrategy().acquire(_request(fetch)))[0]
    assert probe.outcome is AcquisitionOutcome.NOT_CONFIGURED
    assert probe.capability_state is AcquisitionCapabilityState.DEPENDENCY_UNAVAILABLE


@pytest.mark.asyncio
async def test_ocr_unconfigured_reports_dependency_and_reason() -> None:
    async def fetch(url, headers, timeout_seconds, allow_binary):
        raise AssertionError("OCR never performs live fetch")

    probe = (await VisualOcrStrategy().acquire(_request(fetch)))[0]
    assert probe.outcome is AcquisitionOutcome.NOT_CONFIGURED
    assert probe.reason_code == "ocr_not_configured"
    assert probe.capability_state is AcquisitionCapabilityState.DEPENDENCY_UNAVAILABLE


@pytest.mark.asyncio
async def test_ocr_preserves_artifact_hash_and_marks_ocr_provenance() -> None:
    artifact_bytes = b"%PDF-1.7 already captured"

    class FakeOcr:
        async def recognize(self, artifact):
            assert artifact.encoded_body == artifact_bytes
            return TITLE + "\n" + ARTICLE_TEXT

    async def fetch(url, headers, timeout_seconds, allow_binary):
        raise AssertionError("OCR does not fetch")

    artifact = CapturedArtifact(
        artifact_bytes, "application/pdf", URL, hashlib.sha256(artifact_bytes).hexdigest()
    )
    probes = await VisualOcrStrategy(artifact=artifact, engine=FakeOcr()).acquire(_request(fetch))
    success = probes[0]
    assert success.outcome is AcquisitionOutcome.SUCCEEDED
    assert success.observation_kind.value == "ocr_text"
    assert success.evidence_class is EvidenceClass.OCR_DERIVED
    assert success.response is not None
    assert success.response.encoded_body == artifact_bytes
    assert success.response.encoded_sha256 == artifact.sha256


def test_safe_external_url_drops_credentials_and_query_values() -> None:
    safe = safe_external_url(
        "https://user:pass@provider.example.test/path?token=secret&x=123#fragment"
    )
    assert safe == "https://provider.example.test/path"
    assert "secret" not in safe and "pass" not in safe and "123" not in safe


def test_external_provider_payload_policy_gate() -> None:
    allowed = _policy(third_party_provider_allowed=True, provider_configured=True)
    denied_submit = _policy(
        third_party_provider_allowed=True, provider_configured=True, do_not_submit=True
    )
    red = _policy(third_party_provider_allowed=True, provider_configured=True, tlp="RED")
    assert external_payload_is_allowed(allowed, URL)
    assert not external_payload_is_allowed(denied_submit, URL)
    assert not external_payload_is_allowed(red, URL)
    assert not external_payload_is_allowed(allowed, "https://user:pass@publisher.example.test/path")


@pytest.mark.asyncio
async def test_provider_disabled_by_default_and_no_request_sent() -> None:
    calls = 0

    async def fetch(url, headers, timeout_seconds, allow_binary):
        nonlocal calls
        calls += 1
        raise AssertionError("provider is disabled")

    probe = (await ThirdPartyExtractionStrategy().acquire(_request(fetch)))[0]
    assert probe.outcome is AcquisitionOutcome.NOT_CONFIGURED
    assert not ThirdPartyExtractionStrategy().enabled
    assert calls == 0


@pytest.mark.asyncio
async def test_provider_policy_denial_sends_no_external_disclosure() -> None:
    calls = 0

    async def fetch(url, headers, timeout_seconds, allow_binary):
        nonlocal calls
        calls += 1
        raise AssertionError("policy denied provider request")

    strategy = ThirdPartyExtractionStrategy(
        provider_url="https://provider.example.test/extract", provider_token="synthetic-token"
    )
    request = _request(
        fetch,
        policy=_policy(
            third_party_provider_allowed=True, provider_configured=True, do_not_submit=True
        ),
    )
    probe = (await strategy.acquire(request))[0]
    assert probe.outcome is AcquisitionOutcome.POLICY_SKIPPED
    assert probe.policy_decision.value == "denied_provider_disclosure"
    assert calls == 0


@pytest.mark.asyncio
async def test_provider_timeout_is_typed_and_does_not_retry() -> None:
    calls = 0

    async def fetch(url, headers, timeout_seconds, allow_binary):
        nonlocal calls
        calls += 1
        assert headers["authorization"] == "Bearer synthetic-token"
        raise TimeoutError

    strategy = ThirdPartyExtractionStrategy(
        provider_url="https://provider.example.test/extract", provider_token="synthetic-token"
    )
    request = _request(
        fetch, policy=_policy(third_party_provider_allowed=True, provider_configured=True)
    )
    probe = (await strategy.acquire(request))[0]
    assert probe.outcome is AcquisitionOutcome.FAILED
    assert probe.reason_code == "provider_timeout"
    assert calls == 1
    assert "synthetic-token" not in probe.requested_url


@pytest.mark.asyncio
async def test_provider_prior_attempt_prevents_duplicate_disclosure() -> None:
    calls = 0

    async def fetch(url, headers, timeout_seconds, allow_binary):
        nonlocal calls
        calls += 1
        raise AssertionError("attempted provider request must not be sent again")

    strategy = ThirdPartyExtractionStrategy(
        provider_url="https://provider.example.test/extract", provider_token="synthetic-token"
    )
    request = _request(
        fetch,
        policy=_policy(third_party_provider_allowed=True, provider_configured=True),
    )
    target = "https://provider.example.test/extract?" + urlencode({"url": URL, "title": TITLE})
    attempt_key = hashlib.sha256(f"third_party_extraction\0{target}".encode()).hexdigest()
    request = replace(request, attempted_strategy_urls=frozenset({attempt_key}))
    probe = (await strategy.acquire(request))[0]
    assert probe.outcome is AcquisitionOutcome.SKIPPED
    assert probe.reason_code == "already_attempted_for_strategy"
    assert calls == 0


@pytest.mark.asyncio
async def test_provider_ambiguous_response_is_typed_failure() -> None:
    async def fetch(url, headers, timeout_seconds, allow_binary):
        return _response(url, b"not-json", content_type="application/json")

    strategy = ThirdPartyExtractionStrategy(
        provider_url="https://provider.example.test/extract", provider_token="synthetic-token"
    )
    request = _request(
        fetch, policy=_policy(third_party_provider_allowed=True, provider_configured=True)
    )
    probe = (await strategy.acquire(request))[0]
    assert probe.outcome is AcquisitionOutcome.FAILED
    assert probe.reason_code == "provider_ambiguous_failure"


@pytest.mark.asyncio
async def test_provider_sends_only_url_and_title_and_keeps_evidence_unknown() -> None:
    calls: list[tuple[str, dict[str, str]]] = []
    payload = {
        "url": URL,
        "title": TITLE,
        "text": TITLE + "\n" + ARTICLE_TEXT,
        "published_at": "2024-04-01",
        "publisher": "Publisher",
    }

    async def fetch(url, headers, timeout_seconds, allow_binary):
        calls.append((url, headers))
        assert set(parse_qs(urlsplit(url).query)) == {"url", "title"}
        assert ARTICLE_TEXT not in url
        return _response(url, json.dumps(payload).encode(), content_type="application/json")

    strategy = ThirdPartyExtractionStrategy(
        provider_url="https://provider.example.test/extract", provider_token="synthetic-token"
    )
    request = _request(
        fetch, policy=_policy(third_party_provider_allowed=True, provider_configured=True)
    )
    success = (await strategy.acquire(request))[0]
    assert success.outcome is AcquisitionOutcome.SUCCEEDED
    assert success.evidence_class is EvidenceClass.UNKNOWN
    assert success.quality is not None
    assert "Bearer synthetic-token" == calls[0][1]["authorization"]
    assert "synthetic-token" not in success.requested_url
    assert success.response is not None
    assert success.response.decoded_body.decode().startswith(TITLE)


@pytest.mark.asyncio
async def test_official_newsletter_archive_is_not_configured_by_default() -> None:
    calls = 0

    async def fetch(url, headers, timeout_seconds, allow_binary):
        nonlocal calls
        calls += 1
        raise AssertionError("newsletter archive is not configured")

    probe = (await OfficialNewsletterArchiveStrategy().acquire(_request(fetch)))[0]
    assert probe.outcome is AcquisitionOutcome.NOT_CONFIGURED
    assert calls == 0


@pytest.mark.asyncio
async def test_explicit_official_newsletter_archive_fetches_and_validates_page() -> None:
    calls: list[str] = []

    async def fetch(url, headers, timeout_seconds, allow_binary):
        calls.append(url)
        return _response(url, ARTICLE)

    strategy = OfficialNewsletterArchiveStrategy(
        archive_endpoint="https://newsletter.publisher.example.test/archive",
        official_domains=frozenset({"newsletter.publisher.example.test"}),
    )
    probes = await strategy.acquire(_request(fetch))
    success = probes[0]
    assert calls and "url=" in calls[0]
    assert success.outcome is AcquisitionOutcome.SUCCEEDED
    assert success.quality is not None
