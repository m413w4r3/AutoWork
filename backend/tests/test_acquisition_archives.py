from __future__ import annotations

import gzip
import hashlib
import json
from dataclasses import replace
from datetime import UTC, date, datetime
from urllib.parse import parse_qs, urlsplit

import pytest

from cti_app.application.acquisition_archives import (
    CommonCrawlWarcStrategy,
    MementoArchiveStrategy,
    WaybackSnapshotStrategy,
    existing_archive_probe,
)
from cti_app.application.acquisition_fallbacks import AcquisitionRequest
from cti_app.application.http_collection import CollectedResponse, _detect_mime
from cti_app.domain.acquisition import (
    AcquisitionOutcome,
    AcquisitionPolicyContext,
    EvidenceClass,
    SourceEquivalenceClass,
)
from cti_app.domain.collection import SourceAcquisitionMethod

URL = "https://news.example.test/news/original-report"
TITLE = "Original Incident Response Report"
ARTICLE = (
    "<html><head><title>Original Incident Response Report</title></head><body>"
    + "This is the complete original article with incident response details and indicators. " * 12
    + "</body></html>"
).encode()


def _policy() -> AcquisitionPolicyContext:
    return AcquisitionPolicyContext(
        allowed_domains=frozenset(),
        blocked_domains=frozenset(),
        external_llm_allowed=True,
        tlp="CLEAR",
        do_not_submit=False,
        expected_languages=(),
        allowed_syndication_domains=frozenset(),
        third_party_provider_allowed=False,
        provider_configured=False,
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
    published_at: date | None = date(2024, 5, 10),
    attempted=frozenset(),
    requested_url: str = URL,
) -> AcquisitionRequest:
    return AcquisitionRequest(
        requested_url=requested_url,
        title=TITLE,
        published_at=published_at,
        response_body=None,
        response_status=None,
        response_content_type=None,
        response_headers={},
        link_header=None,
        fetch=fetch,
        min_text_chars=100,
        policy=_policy(),
        attempted_strategy_urls=attempted,
    )


@pytest.mark.asyncio
async def test_wayback_selects_nearest_snapshot_and_preserves_timestamp_and_provenance() -> None:
    calls: list[str] = []
    replay = "https://web.archive.org/web/20240509000000id_/https://news.example.test/news/original-report"

    async def fetch(url, headers, timeout_seconds, allow_binary):
        calls.append(url)
        if "cdx/search/cdx" in url:
            params = parse_qs(urlsplit(url).query)
            assert params["filter"] == ["statuscode:200"]
            assert int(params["limit"][0]) <= 100
            return _response(
                url,
                (
                    b'["timestamp","original","statuscode"]\n'
                    b'["20240511000000","https://news.example.test/news/original-report","200"]\n'
                    b'["20240509000000","https://news.example.test/news/original-report","200"]\n'
                ),
                content_type="application/json",
            )
        assert url == replay
        return _response(url, ARTICLE)

    probes = await WaybackSnapshotStrategy().acquire(_request(fetch))
    success = next(probe for probe in probes if probe.outcome is AcquisitionOutcome.SUCCEEDED)
    assert calls[-1] == replay
    assert success.archive_snapshot_time == datetime(2024, 5, 9, tzinfo=UTC)
    assert success.equivalence_class is SourceEquivalenceClass.HISTORICAL_ORIGINAL
    assert success.evidence_class is EvidenceClass.HISTORICAL_PRIMARY
    assert success.quality is not None
    assert "/id_/" not in success.requested_url


@pytest.mark.asyncio
async def test_wayback_rejects_cdx_row_for_a_different_original_url() -> None:
    calls: list[str] = []

    async def fetch(url, headers, timeout_seconds, allow_binary):
        calls.append(url)
        return _response(
            url,
            (
                b'["timestamp","original","statuscode"]\n'
                b'["20240509000000","https://other.example.test/article","200"]\n'
            ),
            content_type="application/json",
        )

    probes = await WaybackSnapshotStrategy().acquire(_request(fetch))
    assert len(calls) == 1
    assert not any(probe.outcome is AcquisitionOutcome.SUCCEEDED for probe in probes)
    assert probes[-1].reason_code == "no_exact_matching_snapshot"


@pytest.mark.asyncio
async def test_wayback_rejects_injected_archive_toolbar() -> None:
    replay = "https://web.archive.org/web/20240509000000id_/https://news.example.test/news/original-report"

    async def fetch(url, headers, timeout_seconds, allow_binary):
        if "cdx/search/cdx" in url:
            return _response(
                url,
                (
                    b'["timestamp","original","statuscode"]\n'
                    b'["20240509000000","https://news.example.test/news/original-report","200"]\n'
                ),
                content_type="application/json",
            )
        assert url == replay
        wrapped = ARTICLE.replace(b"<body>", b'<body><div id="wm-ipp">Wayback navigation</div>')
        return _response(url, wrapped)

    probes = await WaybackSnapshotStrategy().acquire(_request(fetch))
    rejected = [probe for probe in probes if probe.outcome is AcquisitionOutcome.REJECTED]
    assert rejected[-1].reason_code == "archive_wrapper_detected"
    assert not any(probe.outcome is AcquisitionOutcome.SUCCEEDED for probe in probes)


@pytest.mark.asyncio
async def test_wayback_rejects_replay_redirected_to_a_different_original_url() -> None:
    replay = "https://web.archive.org/web/20240509000000id_/https://news.example.test/news/original-report"

    async def fetch(url, headers, timeout_seconds, allow_binary):
        if "cdx/search/cdx" in url:
            return _response(
                url,
                (
                    b'["timestamp","original","statuscode"]\n'
                    b'["20240509000000","https://news.example.test/news/original-report","200"]\n'
                ),
                content_type="application/json",
            )
        assert url == replay
        return replace(
            _response(url, ARTICLE),
            final_url="https://web.archive.org/web/20240509000000id_/https://other.example.test/story",
        )

    probes = await WaybackSnapshotStrategy().acquire(_request(fetch))
    rejected = [probe for probe in probes if probe.outcome is AcquisitionOutcome.REJECTED]
    assert rejected[-1].reason_code == "replay_url_mismatch"


def _warc(
    target: str, *, body: bytes = ARTICLE, status: int = 200, target_override: str | None = None
) -> bytes:
    http = (
        f"HTTP/1.1 {status} OK\r\nContent-Type: text/html; charset=utf-8\r\n"
        f"Content-Length: {len(body)}\r\n\r\n"
    ).encode() + body
    warc_target = target_override or target
    header = (
        "WARC/1.1\r\nWARC-Type: response\r\n"
        f"WARC-Target-URI: {warc_target}\r\nWARC-Date: 2024-05-08T12:00:00Z\r\n"
        f"Content-Length: {len(http)}\r\n\r\n"
    ).encode()
    return header + http + b"\r\n\r\n"


async def _commoncrawl_request(warc_body: bytes):
    range_calls: list[tuple[str, dict[str, str], bool]] = []

    async def fetch(url, headers, timeout_seconds, allow_binary):
        if url == "https://index.commoncrawl.org/collinfo.json":
            return _response(url, b'[{"id":"CC-MAIN-2025-04"}]', content_type="application/json")
        if "-index?" in url:
            row = {
                "url": URL,
                "status": "200",
                "timestamp": "20240508120000",
                "offset": "0",
                "length": str(len(warc_body)),
                "filename": "crawl-data/CC-MAIN-2025-04/segments/1/warc/example.warc.gz",
            }
            return _response(
                url,
                (json.dumps(row) + "\n").encode(),
                content_type="application/x-ndjson",
            )
        range_calls.append((url, headers, allow_binary))
        return _response(url, warc_body, content_type="application/warc", status=206)

    return _request(fetch), range_calls


@pytest.mark.asyncio
async def test_commoncrawl_extracts_warc_article_and_timestamp() -> None:
    request, range_calls = await _commoncrawl_request(gzip.compress(_warc(URL)))
    probes = await CommonCrawlWarcStrategy().acquire(request)
    success = next(probe for probe in probes if probe.outcome is AcquisitionOutcome.SUCCEEDED)
    assert success.response is not None and success.response.decoded_body == ARTICLE
    assert success.archive_snapshot_time == datetime(2024, 5, 8, 12, tzinfo=UTC)
    assert success.equivalence_class is SourceEquivalenceClass.HISTORICAL_ORIGINAL
    assert range_calls[0][1]["range"].startswith("bytes=0-")
    assert range_calls[0][2] is True


@pytest.mark.asyncio
async def test_commoncrawl_rejects_corrupt_warc_without_success() -> None:
    request, _range_calls = await _commoncrawl_request(b"not a WARC record")
    probes = await CommonCrawlWarcStrategy().acquire(request)
    assert not any(probe.outcome is AcquisitionOutcome.SUCCEEDED for probe in probes)
    assert any(probe.reason_code == "invalid_or_mismatched_warc_record" for probe in probes)


@pytest.mark.asyncio
async def test_commoncrawl_rejects_warc_for_wrong_target() -> None:
    wrong = gzip.compress(_warc(URL, target_override="https://other.example.test/article"))
    request, _range_calls = await _commoncrawl_request(wrong)
    probes = await CommonCrawlWarcStrategy().acquire(request)
    assert not any(probe.outcome is AcquisitionOutcome.SUCCEEDED for probe in probes)


@pytest.mark.asyncio
async def test_commoncrawl_rejects_traversal_filename_without_range_fetch() -> None:
    calls: list[str] = []

    async def fetch(url, headers, timeout_seconds, allow_binary):
        calls.append(url)
        if url.endswith("collinfo.json"):
            return _response(url, b'[{"id":"CC-MAIN-2025-04"}]', content_type="application/json")
        return _response(
            url,
            (
                b'{"url":"https://news.example.test/news/original-report","status":"200",'
                b'"timestamp":"20240508120000","offset":0,"length":20,'
                b'"filename":"crawl-data/../private.warc.gz"}\n'
            ),
            content_type="application/x-ndjson",
        )

    probes = await CommonCrawlWarcStrategy().acquire(_request(fetch))
    assert not any("data.commoncrawl.org" in url for url in calls)
    assert not any(probe.outcome is AcquisitionOutcome.SUCCEEDED for probe in probes)


@pytest.mark.asyncio
async def test_memento_uses_only_memento_relation_and_keeps_timestamp() -> None:
    memento_url = "https://archive.example.test/20240508/news/original-report"
    calls: list[str] = []

    async def fetch(url, headers, timeout_seconds, allow_binary):
        calls.append(url)
        if "/api/json/" in url:
            return _response(
                url,
                (
                    '{"links":[{"rel":"timemap","uri":"https://bad.example.test/wrong"},'
                    f'{{"rel":"memento","uri":"{memento_url}","datetime":"2024-05-08T10:00:00Z"}}]}}'
                ).encode(),
                content_type="application/json",
            )
        assert url == memento_url
        return _response(url, ARTICLE)

    probes = await MementoArchiveStrategy().acquire(_request(fetch))
    success = next(probe for probe in probes if probe.outcome is AcquisitionOutcome.SUCCEEDED)
    assert calls[-1] == memento_url
    assert success.archive_snapshot_time == datetime(2024, 5, 8, 10, tzinfo=UTC)
    assert success.evidence_class is EvidenceClass.HISTORICAL_PRIMARY


@pytest.mark.asyncio
async def test_memento_rejects_invalid_snapshot_url() -> None:
    calls: list[str] = []

    async def fetch(url, headers, timeout_seconds, allow_binary):
        calls.append(url)
        return _response(
            url, b'{"rel":"memento","uri":"javascript:alert(1)"}', content_type="application/json"
        )

    probes = await MementoArchiveStrategy().acquire(_request(fetch))
    assert len(calls) == 1
    assert not any(probe.outcome is AcquisitionOutcome.SUCCEEDED for probe in probes)


def test_existing_archive_probe_succeeds_without_network_or_fabricated_response() -> None:
    class Collection:
        requested_url = URL
        source_document_id = "document-1"

    probe = existing_archive_probe(Collection())
    assert probe.method is SourceAcquisitionMethod.EXISTING_ARCHIVE
    assert probe.outcome is AcquisitionOutcome.SUCCEEDED
    assert probe.response is None
    assert probe.quality is not None


@pytest.mark.asyncio
async def test_wayback_honors_prior_attempt_key_without_fetching_again() -> None:
    import hashlib

    calls = 0

    async def fetch(url, headers, timeout_seconds, allow_binary):
        nonlocal calls
        calls += 1
        raise AssertionError("prior attempt should prevent duplicate fetch")

    # The strategy's first URL is deterministic; build it once with a no-op capture.
    async def capture(url, headers, timeout_seconds, allow_binary):
        return _response(
            url, b'["timestamp","original","statuscode"]', content_type="application/json"
        )

    await WaybackSnapshotStrategy().acquire(_request(capture))
    # Use the public canonical URL and CDX query shape to derive its URL hash.
    from urllib.parse import urlencode

    full_lookup = "https://web.archive.org/cdx/search/cdx?" + urlencode(
        {
            "url": URL,
            "output": "json",
            "filter": "statuscode:200",
            "fl": "timestamp,original,statuscode",
            "limit": "20",
        }
    )
    key = hashlib.sha256(f"wayback_snapshot\0{full_lookup}".encode()).hexdigest()
    probes = await WaybackSnapshotStrategy().acquire(_request(fetch, attempted=frozenset({key})))
    assert calls == 0
    assert probes[0].reason_code == "already_attempted_for_strategy"


@pytest.mark.asyncio
async def test_archives_do_not_submit_private_original_urls_to_archive_services() -> None:
    calls = 0

    async def fetch(url, headers, timeout_seconds, allow_binary):
        nonlocal calls
        calls += 1
        raise AssertionError("private origin must be rejected before archive lookup")

    probes = await WaybackSnapshotStrategy().acquire(
        _request(fetch, requested_url="http://127.0.0.1/internal")
    )
    assert calls == 0
    assert probes[0].outcome is AcquisitionOutcome.POLICY_SKIPPED
    assert probes[0].reason_code == "original_url_policy_denied"
