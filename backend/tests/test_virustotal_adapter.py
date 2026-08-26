from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from typing import Any

import httpx
import pytest

from cti_app.application.virustotal import (
    FileRelationship,
    VirusTotalCapabilities,
    VirusTotalCapabilityDisabledError,
    VirusTotalError,
    VirusTotalInvalidInputError,
    VirusTotalRelationNotAllowedError,
    VirusTotalResponseTooLargeError,
)
from cti_app.config import Settings
from cti_app.infrastructure.virustotal import (
    VirusTotalHttpAdapter,
    create_virustotal_direct_http_client,
    create_virustotal_http_client,
)

BASE = "https://www.virustotal.com/api/v3"
SHA256 = "A" * 64


def adapter(handler: Any, **kwargs: Any) -> tuple[VirusTotalHttpAdapter, httpx.AsyncClient]:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    value = VirusTotalHttpAdapter(
        client=client,
        base_url=BASE,
        capabilities=VirusTotalCapabilities(
            file_report=True, file_relationships=True, intelligence_search=True
        ),
        **kwargs,
    )
    return value, client


@pytest.mark.asyncio
async def test_file_report_normalizes_hash_and_returns_exact_body() -> None:
    body = json.dumps(
        {
            "data": {
                "id": SHA256.lower(),
                "type": "file",
                "attributes": {
                    "sha256": SHA256.lower(),
                    "meaningful_name": "sample.exe",
                    "size": 12,
                    "last_analysis_stats": {"malicious": 2},
                    "tags": ["peexe"],
                },
            }
        },
        separators=(",", ":"),
    ).encode()
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=body, request=request)

    value, client = adapter(handler)
    try:
        result = await value.file_report(SHA256)
    finally:
        await client.aclose()
    assert result.raw_json == body
    assert result.file.lookup_value == SHA256.lower()
    assert str(seen[0].url) == f"{BASE}/files/{SHA256.lower()}"
    assert "x-apikey" not in seen[0].headers


@pytest.mark.asyncio
async def test_file_report_falls_back_to_v2_only_with_direct_key_client() -> None:
    proxy_calls: list[str] = []
    direct_calls: list[httpx.Request] = []

    def proxy_handler(request: httpx.Request) -> httpx.Response:
        proxy_calls.append(str(request.url))
        return httpx.Response(404, request=request)

    def direct_handler(request: httpx.Request) -> httpx.Response:
        direct_calls.append(request)
        return httpx.Response(
            200,
            json={
                "response_code": 1,
                "md5": "a" * 32,
                "sha256": SHA256.lower(),
                "positives": 3,
                "total": 70,
                "scans": {},
            },
            request=request,
        )

    proxy = httpx.AsyncClient(transport=httpx.MockTransport(proxy_handler))
    direct = httpx.AsyncClient(
        transport=httpx.MockTransport(direct_handler),
        headers={"accept": "application/json", "x-apikey": "direct-test-key"},
    )
    value = VirusTotalHttpAdapter(
        client=proxy,
        direct_client=direct,
        api_key="direct-test-key",
        base_url=BASE,
        legacy_base_url="http://www.virustotal.com/vtapi/v2",
        capabilities=VirusTotalCapabilities(file_report=True),
    )
    try:
        result = await value.file_report(SHA256)
    finally:
        await direct.aclose()
        await proxy.aclose()
    assert len(proxy_calls) == 1
    assert result.file.lookup_value == SHA256.lower()
    assert direct_calls[0].url.path == "/vtapi/v2/file/report"
    assert direct_calls[0].url.params["resource"] == SHA256.lower()
    assert direct_calls[0].url.params["apikey"] == "direct-test-key"
    assert direct_calls[0].headers["x-apikey"] == "direct-test-key"


@pytest.mark.asyncio
async def test_direct_client_requires_explicit_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings(_env_file=None)
    with pytest.raises(VirusTotalError) as caught:
        create_virustotal_direct_http_client(settings)
    assert caught.value.code == "virustotal_transport_not_configured"
    monkeypatch.setenv("VIRUSTOTAL_API_KEY", "direct-test-key")
    client = create_virustotal_direct_http_client(Settings(_env_file=None))
    try:
        assert client.headers["x-apikey"] == "direct-test-key"
    finally:
        await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("file_hash", ["A" * 32, "B" * 40, "C" * 64])
async def test_md5_sha1_sha256_are_accepted_and_normalized(file_hash: str) -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(
            200,
            json={"data": {"id": file_hash.lower(), "type": "file", "attributes": {}}},
            request=request,
        )

    value, client = adapter(handler)
    try:
        await value.file_report(file_hash)
    finally:
        await client.aclose()
    assert seen == [f"/api/v3/files/{file_hash.lower()}"]


@pytest.mark.asyncio
async def test_invalid_hash_is_rejected_without_http() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500, request=request)

    value, client = adapter(handler)
    try:
        with pytest.raises(VirusTotalInvalidInputError):
            await value.file_report("not-a-hash")
    finally:
        await client.aclose()
    assert calls == 0


@pytest.mark.asyncio
async def test_relationship_allowlist_and_submission_rejection() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"data": []}, request=request)

    value, client = adapter(handler)
    try:
        with pytest.raises(VirusTotalRelationNotAllowedError) as caught:
            await value.file_relationship(SHA256, "submissions")  # type: ignore[arg-type]
        assert caught.value.code == "virustotal_relation_not_allowed"
    finally:
        await client.aclose()
    assert calls == 0


@pytest.mark.asyncio
async def test_intelligence_uses_params_and_forces_descriptors_only() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v3/intelligence/search"
        assert request.url.params["query"] == 'name:"a b"'
        assert request.url.params["limit"] == "2"
        assert request.url.params["descriptors_only"] == "true"
        assert "x-apikey" not in request.headers
        return httpx.Response(200, json={"data": [{"id": "x"}]}, request=request)

    value, client = adapter(handler)
    try:
        result = await value.intelligence_search('name:"a b"', limit=2)
    finally:
        await client.aclose()
    assert result.items == ({"id": "x"},)
    assert result.exhaustive is True


@pytest.mark.asyncio
async def test_pagination_cursor_and_max_pages() -> None:
    cursors: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        cursors.append(request.url.params.get("cursor"))
        cursor = request.url.params.get("cursor")
        next_cursor = "page-2" if cursor is None else "page-3"
        return httpx.Response(
            200,
            json={"data": [{"id": cursor or "page-1"}], "meta": {"cursor": next_cursor}},
            request=request,
        )

    value, client = adapter(handler, max_pages=2)
    try:
        result = await value.file_relationship(
            SHA256, FileRelationship.DROPPED_FILES, paginate=True
        )
    finally:
        await client.aclose()
    assert cursors == [None, "page-2"]
    assert result.observed_count == 2
    assert result.stopped_due_to_limit is True
    assert result.exhaustive is False
    assert result.next_cursor == "page-3"


@pytest.mark.asyncio
async def test_max_results_marks_result_non_exhaustive() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"data": [{"id": "a"}, {"id": "b"}], "meta": {"cursor": "next"}},
            request=request,
        )

    value, client = adapter(handler, max_results=1)
    try:
        result = await value.file_relationship(
            SHA256, FileRelationship.DROPPED_FILES, paginate=True
        )
    finally:
        await client.aclose()
    assert result.observed_count == 1
    assert result.stopped_due_to_limit is True
    assert result.exhaustive is False


@pytest.mark.asyncio
async def test_capability_disabled_and_missing_proxy_are_local() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    value = VirusTotalHttpAdapter(
        client=client, base_url=BASE, capabilities=VirusTotalCapabilities()
    )
    try:
        with pytest.raises(VirusTotalCapabilityDisabledError) as caught:
            await value.file_report(SHA256)
        assert caught.value.code == "virustotal_capability_disabled"
        with pytest.raises(VirusTotalError):
            create_virustotal_http_client(Settings(_env_file=None))
    finally:
        await client.aclose()
    assert calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "code", "retryable"),
    [
        (401, "virustotal_upstream_unauthorized", False),
        (403, "virustotal_upstream_forbidden", False),
        (404, "virustotal_not_found", False),
        (407, "virustotal_proxy_auth_required", False),
        (429, "virustotal_rate_limited", True),
        (503, "virustotal_upstream_error", True),
    ],
)
async def test_http_statuses_are_normalized(status: int, code: str, retryable: bool) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        headers = {"Retry-After": "7"} if status == 429 else {}
        return httpx.Response(status, headers=headers, request=request)

    value, client = adapter(handler)
    try:
        with pytest.raises(VirusTotalError) as caught:
            await value.file_report(SHA256)
    finally:
        await client.aclose()
    assert caught.value.code == code
    assert caught.value.retryable is retryable
    if status == 429:
        assert caught.value.retry_after == 7


@pytest.mark.asyncio
async def test_retry_after_http_date_and_invalid_json_and_size_limit() -> None:
    date = format_datetime(datetime.now(UTC) + timedelta(seconds=30), usegmt=True)

    def date_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": date}, request=request)

    value, client = adapter(date_handler)
    try:
        with pytest.raises(VirusTotalError) as caught:
            await value.file_report(SHA256)
    finally:
        await client.aclose()
    assert caught.value.retry_after is not None and caught.value.retry_after > 0

    def invalid_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not-json", request=request)

    value, client = adapter(invalid_handler)
    try:
        with pytest.raises(VirusTotalError) as caught:
            await value.file_report(SHA256)
    finally:
        await client.aclose()
    assert caught.value.code == "virustotal_json_invalid"

    def large_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * 11, request=request)

    value, client = adapter(large_handler, max_response_bytes=10)
    try:
        with pytest.raises(VirusTotalResponseTooLargeError):
            await value.file_report(SHA256)
    finally:
        await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("transport_error", "code", "retryable"),
    [
        (httpx.ConnectTimeout("connect"), "virustotal_connect_timeout", True),
        (httpx.ReadTimeout("read"), "virustotal_read_timeout", True),
        (httpx.ConnectError("connect"), "virustotal_connection_error", True),
        (httpx.ProxyError("proxy"), "virustotal_proxy_error", True),
    ],
)
async def test_transport_errors_are_normalized(
    transport_error: Exception, code: str, retryable: bool
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise transport_error

    value, client = adapter(handler)
    try:
        with pytest.raises(VirusTotalError) as caught:
            await value.file_report(SHA256)
    finally:
        await client.aclose()
    assert caught.value.code == code
    assert caught.value.retryable is retryable
