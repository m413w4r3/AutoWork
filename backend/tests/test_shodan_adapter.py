from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable

import httpx
import pytest
from pydantic import SecretStr

from cti_app.application.shodan import (
    ShodanCapabilities,
    ShodanCapabilityDisabledError,
    ShodanHttpError,
    ShodanInvalidInputError,
    ShodanJsonError,
    ShodanNotConfiguredError,
    ShodanQuotaError,
    ShodanResponseTooLargeError,
    ShodanTransportError,
)
from cti_app.config import Settings
from cti_app.domain.shodan import ShodanRoute
from cti_app.infrastructure.shodan import (
    MAX_BANNER_CHARACTERS,
    ShodanHttpAdapter,
    create_shodan_http_adapter,
)
from tests.shodan_support import (
    API_INFO_RESPONSE,
    DOMAIN_JSON,
    DOMAIN_RESPONSE,
    HOST_RESPONSE,
    HTTP_RESPONSE_FIXTURES,
    SEARCH_RESPONSE,
    SHARED_CERT_SHA256,
)

API_KEY = "shodan-test-key-never-log"
TEST_API_KEY = SecretStr(API_KEY)
PASSIVE_PATHS = {
    "/shodan/host/203.0.113.10",
    "/shodan/host/search",
    "/shodan/host/count",
    "/dns/domain/example.test",
    "/api-info",
}


def _adapter(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    capabilities: ShodanCapabilities | None = None,
    api_key: SecretStr | None = TEST_API_KEY,
    max_response_bytes: int = 1_000_000,
    max_pages: int = 10,
    max_results: int = 1000,
) -> tuple[ShodanHttpAdapter, httpx.AsyncClient]:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True)
    adapter = ShodanHttpAdapter(
        client=client,
        capabilities=capabilities
        or ShodanCapabilities(host=True, search=True, domain=True, count=True),
        api_key=api_key,
        max_response_bytes=max_response_bytes,
        max_pages=max_pages,
        max_results=max_results,
    )
    return adapter, client


def _json_response(request: httpx.Request, payload: bytes, status: int = 200) -> httpx.Response:
    return httpx.Response(
        status, content=payload, request=request, headers={"content-type": "application/json"}
    )


def _check_passive_get(request: httpx.Request) -> None:
    assert request.method == "GET"
    assert request.url.path in PASSIVE_PATHS
    assert request.url.path not in {"/shodan/scan", "/shodan/scan/internet", "/shodan/alert"}
    assert request.url.params.get("key") == API_KEY


@pytest.mark.asyncio
async def test_host_maps_partial_and_present_fields_and_preserves_capture_times() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        _check_passive_get(request)
        requests.append(request)
        if request.url.path == "/shodan/host/203.0.113.10":
            return _json_response(request, HOST_RESPONSE)
        if request.url.path == "/api-info":
            return _json_response(request, API_INFO_RESPONSE)
        raise AssertionError(f"Unexpected passive path {request.url.path}")

    adapter, client = _adapter(handler)
    try:
        result = await adapter.host("203.0.113.10")
    finally:
        await client.aclose()

    assert result.ip == "203.0.113.10"
    assert result.organization == "Example Networks"
    assert result.asn == "AS64500"
    assert result.hostnames == ("edge.example.test",)
    assert result.ports == (443, 8443)
    assert result.tags == ("cdn", "cloud")
    assert len(result.services) == 2
    first, second = result.services
    assert first.port == 443 and first.transport == "tcp" and first.product == "nginx"
    assert (
        first.timestamp is not None and first.timestamp.isoformat() == "2026-10-12T08:30:00+00:00"
    )
    assert first.certificate is not None
    assert first.certificate.fingerprint_sha256 == SHARED_CERT_SHA256
    assert first.certificate.issuer == "CN=Example Test CA, O=Example"
    assert first.certificate.subject == "CN=edge.example.test"
    assert first.certificate.issued_at is not None and first.certificate.expires_at is not None
    assert second.certificate == first.certificate
    assert result.meta.service_captures[0].timestamp == first.timestamp
    assert result.meta.service_captures[0].age_seconds is not None
    assert result.meta.credits_consumed == 1
    assert result.meta.query_credits == 42
    assert result.meta.plan == "edu"
    assert result.meta.route is ShodanRoute.HOST
    assert result.raw_response == HOST_RESPONSE
    assert result.raw_sha256 == hashlib.sha256(HOST_RESPONSE).hexdigest()
    assert [request.url.path for request in requests] == ["/shodan/host/203.0.113.10", "/api-info"]


@pytest.mark.asyncio
async def test_host_tolerates_missing_optional_fields_and_truncates_banner() -> None:
    payload = {
        "ip_str": "203.0.113.10",
        "data": [{"port": 80, "data": "B" * (MAX_BANNER_CHARACTERS + 25)}],
    }
    raw = json.dumps(payload, separators=(",", ":")).encode()

    def handler(request: httpx.Request) -> httpx.Response:
        _check_passive_get(request)
        if request.url.path == "/api-info":
            return _json_response(request, b"{}")
        return _json_response(request, raw)

    adapter, client = _adapter(handler)
    try:
        result = await adapter.host("203.0.113.10")
    finally:
        await client.aclose()

    assert result.organization is None and result.asn is None
    assert result.hostnames == result.tags == ()
    assert result.services[0].transport is None
    assert result.services[0].timestamp is None
    assert result.services[0].certificate is None
    assert result.services[0].banner == "B" * MAX_BANNER_CHARACTERS
    assert result.services[0].banner_truncated
    assert result.meta.service_captures[0].age_seconds is None


@pytest.mark.asyncio
async def test_search_bounds_matches_and_domain_preserves_dns_records() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        _check_passive_get(request)
        requests.append(request)
        if request.url.path == "/shodan/host/search":
            return _json_response(request, SEARCH_RESPONSE)
        if request.url.path == "/dns/domain/example.test":
            return _json_response(request, DOMAIN_RESPONSE)
        if request.url.path == "/api-info":
            return _json_response(request, API_INFO_RESPONSE)
        raise AssertionError(f"Unexpected passive path {request.url.path}")

    adapter, client = _adapter(handler, max_results=2)
    try:
        search = await adapter.search("  port:443  ", page=2)
        domain = await adapter.domain("Example.Test.")
    finally:
        await client.aclose()

    assert search.page == 2 and search.total == 3 and len(search.matches) == 2
    search_request = next(request for request in requests if request.url.path.endswith("/search"))
    assert search_request.url.params.get("query") == "port:443"
    assert search_request.url.params.get("page") == "2"
    assert search.meta.query_credits == 42 and search.meta.plan == "edu"
    assert search.raw_response == SEARCH_RESPONSE
    assert domain.domain == "example.test"
    assert domain.subdomains == ("www", "api", "vpn")
    assert domain.dns_records == tuple(DOMAIN_JSON["data"])
    assert domain.meta.route is ShodanRoute.DOMAIN
    assert domain.raw_response == DOMAIN_RESPONSE


@pytest.mark.asyncio
async def test_404_no_information_returns_empty_typed_host_result() -> None:
    not_found_status, not_found_body = HTTP_RESPONSE_FIXTURES["not_found"]

    def handler(request: httpx.Request) -> httpx.Response:
        _check_passive_get(request)
        if request.url.path == "/api-info":
            return _json_response(request, API_INFO_RESPONSE)
        return _json_response(request, not_found_body, not_found_status)

    adapter, client = _adapter(handler)
    try:
        result = await adapter.host("203.0.113.10")
    finally:
        await client.aclose()

    assert result.http_status == 404
    assert result.ip == "203.0.113.10"
    assert result.services == ()
    assert result.raw_response == not_found_body
    assert result.raw_sha256 == hashlib.sha256(not_found_body).hexdigest()
    assert result.meta.query_credits == 42


@pytest.mark.asyncio
async def test_404_returns_empty_typed_results_for_search_domain_and_count() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        _check_passive_get(request)
        if request.url.path == "/api-info":
            return _json_response(request, API_INFO_RESPONSE)
        return httpx.Response(404, content=b"No information available", request=request)

    adapter, client = _adapter(handler)
    try:
        search = await adapter.search("port:443")
        domain = await adapter.domain("example.test")
        count = await adapter.count("port:443")
    finally:
        await client.aclose()

    assert search.http_status == 404 and search.total == 0 and search.matches == ()
    assert domain.http_status == 404 and domain.subdomains == () and domain.dns_records == ()
    assert count.http_status == 404 and count.total == 0


@pytest.mark.asyncio
async def test_count_reads_total_and_preserves_raw_body_and_metadata() -> None:
    raw = b'{"total":31,"matches":[]}'

    def handler(request: httpx.Request) -> httpx.Response:
        _check_passive_get(request)
        if request.url.path == "/shodan/host/count":
            return _json_response(request, raw)
        if request.url.path == "/api-info":
            return _json_response(request, API_INFO_RESPONSE)
        raise AssertionError(f"Unexpected passive path {request.url.path}")

    adapter, client = _adapter(handler)
    try:
        result = await adapter.count("port:443")
    finally:
        await client.aclose()

    assert result.total == 31 and result.query == "port:443"
    assert result.raw_response == raw
    assert result.raw_sha256 == hashlib.sha256(raw).hexdigest()
    assert result.meta.credits_consumed == 1
    assert result.meta.query_credits == 42 and result.meta.plan == "edu"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("fixture_name", "error_type", "status", "error_code"),
    [
        ("unauthorized", ShodanCapabilityDisabledError, 401, "shodan_upstream_unauthorized"),
        ("quota", ShodanQuotaError, 402, "shodan_quota_exhausted"),
        ("rate_limited", ShodanHttpError, 429, "shodan_rate_limited"),
    ],
)
async def test_provider_auth_quota_and_rate_limit_errors_are_typed_and_bounded(
    fixture_name: str, error_type: type[Exception], status: int, error_code: str
) -> None:
    calls = 0
    response_status, response_body = HTTP_RESPONSE_FIXTURES[fixture_name]

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        _check_passive_get(request)
        calls += 1
        headers = {"Retry-After": "7200"} if status == 429 else {}
        return httpx.Response(status, content=response_body, headers=headers, request=request)

    adapter, client = _adapter(handler)
    try:
        with pytest.raises(error_type) as caught:
            await adapter.host("203.0.113.10")
    finally:
        await client.aclose()

    assert calls == 1
    assert caught.value.status_code == response_status  # type: ignore[attr-defined]
    assert caught.value.code == error_code  # type: ignore[attr-defined]
    if status == 429:
        assert caught.value.retryable  # type: ignore[attr-defined]
        assert caught.value.retry_after == 300.0  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_forbidden_status_also_marks_capability_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        _check_passive_get(request)
        return httpx.Response(403, request=request)

    adapter, client = _adapter(handler)
    try:
        with pytest.raises(ShodanCapabilityDisabledError) as caught:
            await adapter.host("203.0.113.10")
    finally:
        await client.aclose()
    assert caught.value.status_code == 403
    assert caught.value.code == "shodan_upstream_forbidden"


@pytest.mark.asyncio
async def test_forbidden_error_preserves_safe_upstream_message_and_account_details() -> None:
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        if request.url.path == "/api-info":
            return _json_response(request, b'{"plan":"oss","query_credits":0}')
        return _json_response(
            request,
            b'{"error":"Requires membership or higher to access"}',
            status=403,
        )

    adapter, client = _adapter(handler)
    try:
        with pytest.raises(ShodanCapabilityDisabledError) as caught:
            await adapter.host("203.0.113.10")
    finally:
        await client.aclose()

    assert caught.value.upstream_message == "Requires membership or higher to access"
    assert "Requires membership or higher to access" in str(caught.value)
    assert caught.value.plan == "oss"
    assert caught.value.query_credits == 0
    assert requests == ["/shodan/host/203.0.113.10", "/api-info"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        b'{"error":"line one\\nline two"}',
        b"raw error " + b"x" * 8_000,
        (b'{"error":"' + API_KEY.encode() + b'"}'),
    ],
)
async def test_forbidden_upstream_message_is_sanitized_and_bounded(body: bytes) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api-info":
            return httpx.Response(403, content=b"not-json", request=request)
        return httpx.Response(403, content=body, request=request)

    adapter, client = _adapter(handler)
    try:
        with pytest.raises(ShodanCapabilityDisabledError) as caught:
            await adapter.host("203.0.113.10")
    finally:
        await client.aclose()

    assert caught.value.upstream_message is not None
    assert len(caught.value.upstream_message) <= 200
    assert "\\n" not in caught.value.upstream_message
    assert API_KEY not in caught.value.upstream_message


@pytest.mark.asyncio
async def test_disabled_or_unconfigured_capability_fails_before_http() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise AssertionError(f"Unexpected HTTP request: {request.url.path}")

    disabled, client = _adapter(handler, capabilities=ShodanCapabilities())
    try:
        with pytest.raises(ShodanCapabilityDisabledError):
            await disabled.host("203.0.113.10")
    finally:
        await client.aclose()

    unconfigured, client = _adapter(handler, api_key=None)
    try:
        with pytest.raises(ShodanNotConfiguredError):
            await unconfigured.host("203.0.113.10")
    finally:
        await client.aclose()
    assert calls == 0


@pytest.mark.asyncio
async def test_invalid_ip_query_and_page_are_rejected_before_http() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise AssertionError(f"Unexpected HTTP request: {request.url.path}")

    adapter, client = _adapter(handler, max_pages=2)
    try:
        with pytest.raises(ShodanInvalidInputError):
            await adapter.host("not-an-ip")
        with pytest.raises(ShodanInvalidInputError):
            await adapter.search("scan:203.0.113.10")
        with pytest.raises(ShodanInvalidInputError):
            await adapter.search("port:443", page=3)
    finally:
        await client.aclose()
    assert calls == 0


@pytest.mark.asyncio
async def test_private_request_guard_refuses_active_or_unknown_shodan_routes() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise AssertionError(f"Unexpected HTTP request: {request.url.path}")

    adapter, client = _adapter(handler)
    try:
        for path in ("/shodan/scan", "/shodan/scan/internet", "/shodan/alert", "/unknown"):
            with pytest.raises(ShodanInvalidInputError):
                await adapter._get(path)
    finally:
        await client.aclose()
    assert calls == 0


@pytest.mark.asyncio
async def test_response_size_limit_and_invalid_json_are_rejected() -> None:
    def large_handler(request: httpx.Request) -> httpx.Response:
        _check_passive_get(request)
        return _json_response(request, b"{" + b" " * 100 + b"}")

    adapter, client = _adapter(large_handler, max_response_bytes=20)
    try:
        with pytest.raises(ShodanResponseTooLargeError):
            await adapter.host("203.0.113.10")
    finally:
        await client.aclose()

    def invalid_handler(request: httpx.Request) -> httpx.Response:
        _check_passive_get(request)
        if request.url.path == "/api-info":
            return _json_response(request, b"{}")
        return _json_response(request, b"not-json")

    adapter, client = _adapter(invalid_handler)
    try:
        with pytest.raises(ShodanJsonError):
            await adapter.host("203.0.113.10")
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_redirect_is_refused_even_if_injected_client_follows_redirects() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        _check_passive_get(request)
        calls += 1
        return httpx.Response(302, headers={"Location": "/api-info"}, request=request)

    adapter, client = _adapter(handler)
    try:
        with pytest.raises(ShodanHttpError) as caught:
            await adapter.host("203.0.113.10")
    finally:
        await client.aclose()
    assert caught.value.code == "shodan_unexpected_redirect"
    assert calls == 1


@pytest.mark.asyncio
async def test_key_is_not_in_transport_error_or_logs(caplog: pytest.LogCaptureFixture) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        _check_passive_get(request)
        raise httpx.ConnectError(f"connection failed at {request.url}", request=request)

    adapter, client = _adapter(handler)
    try:
        with caplog.at_level(logging.DEBUG):
            with pytest.raises(ShodanTransportError) as caught:
                await adapter.host("203.0.113.10")
    finally:
        await client.aclose()

    assert API_KEY not in str(caught.value)
    assert API_KEY not in repr(caught.value)
    assert API_KEY not in caplog.text
    assert caught.value.__suppress_context__


@pytest.mark.asyncio
async def test_http_client_request_logs_redact_the_query_key(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        _check_passive_get(request)
        if request.url.path == "/api-info":
            return _json_response(request, API_INFO_RESPONSE)
        return _json_response(request, HOST_RESPONSE)

    adapter, client = _adapter(handler)
    try:
        with caplog.at_level(logging.INFO, logger="httpx"):
            await adapter.host("203.0.113.10")
    finally:
        await client.aclose()

    assert any(record.name == "httpx" for record in caplog.records)
    assert "[REDACTED]" in caplog.text
    assert API_KEY not in caplog.text


@pytest.mark.asyncio
async def test_timeout_is_mapped_to_retryable_transport_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        _check_passive_get(request)
        raise httpx.ReadTimeout("timeout", request=request)

    adapter, client = _adapter(handler)
    try:
        with pytest.raises(ShodanTransportError) as caught:
            await adapter.host("203.0.113.10")
    finally:
        await client.aclose()
    assert caught.value.retryable
    assert caught.value.code == "shodan_timeout"


def test_adapter_exposes_only_passive_read_operations() -> None:
    public_methods = {
        name
        for name, value in ShodanHttpAdapter.__dict__.items()
        if not name.startswith("_") and callable(value)
    }
    assert public_methods == {"host", "search", "domain", "count"}
    assert not any("scan" in name or "alert" in name for name in public_methods)


@pytest.mark.asyncio
async def test_settings_builder_keeps_shodan_disabled_by_default() -> None:
    settings = Settings(_env_file=None)
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200)))
    adapter = create_shodan_http_adapter(client=client, settings=settings)
    assert adapter._capabilities == ShodanCapabilities()
    assert adapter._api_key is None
    assert adapter._base_url == "https://api.shodan.io"
    await client.aclose()
