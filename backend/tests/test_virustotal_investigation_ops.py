from __future__ import annotations

import hashlib
import io
from dataclasses import replace
from typing import Any, cast
from uuid import uuid4

import httpx
import pytest

from cti_app.application.virustotal import (
    VirusTotalCapabilities,
    VirusTotalCapabilityDisabledError,
    VirusTotalError,
    VirusTotalExternalEffectNeedsReviewError,
    VirusTotalHttpError,
    VirusTotalInvalidInputError,
    VirusTotalNotAuthorizedError,
    VirusTotalOperationRoute,
    VirusTotalQuotaExceededError,
    VirusTotalRelationNotAllowedError,
    VirusTotalRouteStep,
    VirusTotalRoutingPolicy,
    current_virustotal_external_effect_authority,
    virustotal_external_effect_scope,
)
from cti_app.config import Settings
from cti_app.domain.virustotal import (
    VirusTotalCapability,
    VirusTotalEndpointVariant,
    VirusTotalObjectType,
    VirusTotalOperation,
    VirusTotalTransportKind,
)
from cti_app.infrastructure import virustotal as virustotal_module
from cti_app.infrastructure.virustotal import VirusTotalHttpAdapter
from cti_app.workers.tasks import virustotal_capabilities_from_settings
from tests.virustotal_external_effect_fakes import (
    authorized_vt_read,
    configure_test_vt_adapter,
)

BASE = "https://www.virustotal.com/api/v3"
SHA256 = "a" * 64


@pytest.fixture(autouse=True)
def _external_effect_authority():
    with authorized_vt_read():
        yield


def _adapter(
    handler: Any,
    *,
    capabilities: VirusTotalCapabilities | None = None,
    **kwargs: Any,
) -> tuple[VirusTotalHttpAdapter, httpx.AsyncClient]:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    value = VirusTotalHttpAdapter(
        client=client,
        base_url=BASE,
        capabilities=capabilities
        or VirusTotalCapabilities(
            file_report=True,
            file_relationships=True,
            intelligence_search=True,
            behaviour_reports=True,
            file_download=True,
        ),
        **kwargs,
    )
    configure_test_vt_adapter(value)
    return value, client


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("object_type", "object_id", "relation", "expected_path"),
    [
        (
            VirusTotalObjectType.IP_ADDRESS,
            "8.8.8.8",
            "urls",
            "/api/v3/ip_addresses/8.8.8.8/urls",
        ),
        (
            VirusTotalObjectType.DOMAIN,
            "example.com",
            "subdomains",
            "/api/v3/domains/example.com/subdomains",
        ),
        (
            VirusTotalObjectType.URL,
            "https://example.com/a?x=1",
            "redirecting_urls",
            "/api/v3/urls/aHR0cHM6Ly9leGFtcGxlLmNvbS9hP3g9MQ/redirecting_urls",
        ),
        (
            VirusTotalObjectType.FILE,
            SHA256,
            "submissions",
            f"/api/v3/files/{SHA256}/submissions",
        ),
    ],
)
async def test_object_relationship_routes_are_allowlisted_and_encoded(
    object_type: VirusTotalObjectType,
    object_id: str,
    relation: str,
    expected_path: str,
) -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(200, json={"data": []}, request=request)

    value, client = _adapter(handler)
    try:
        page = await value.object_relationship(object_type, object_id, relation)
    finally:
        await client.aclose()
    assert seen == [expected_path]
    assert page.empty is True
    assert page.exhaustive is True
    claim = next(iter(cast(Any, value)._test_external_effect_store.claims.values()))
    assert claim.family == "vt.relationships"


@pytest.mark.asyncio
async def test_relationship_outside_the_typed_allowlist_is_rejected_before_http() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"data": []}, request=request)

    value, client = _adapter(handler)
    try:
        with pytest.raises(VirusTotalRelationNotAllowedError):
            await value.object_relationship(
                VirusTotalObjectType.DOMAIN, "example.com", "submissions"
            )
    finally:
        await client.aclose()
    assert calls == 0


@pytest.mark.asyncio
async def test_behaviours_page_with_cursor_and_typed_summary() -> None:
    requests: list[tuple[str | None, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        cursor = request.url.params.get("cursor")
        requests.append((cursor, request.url.params["limit"]))
        if cursor is None:
            payload = {
                "data": [
                    {
                        "id": "sandbox-1",
                        "attributes": {
                            "sandbox_name": "Cape",
                            "analysis_date": 1781300000,
                            "tags": ["win32", "network"],
                            "has_pcap": True,
                            "has_html_report": False,
                            "dns_lookups": [{"hostname": "example.test"}],
                        },
                    }
                ],
                "meta": {"cursor": "next-page"},
            }
        else:
            payload = {"data": [{"id": "sandbox-2", "attributes": {"processes_tree": []}}]}
        return httpx.Response(200, json=payload, request=request)

    value, client = _adapter(handler, max_pages=3)
    try:
        page = await value.behaviours(SHA256, paginate=True)
    finally:
        await client.aclose()
    assert requests == [(None, "10"), ("next-page", "10")]
    assert page.observed_count == 2
    assert page.exhaustive is True
    assert page.behaviour_summaries[0].sandbox == "Cape"
    assert page.behaviour_summaries[0].date == 1781300000
    assert page.behaviour_summaries[0].tags == ("win32", "network")
    assert page.behaviour_summaries[0].has_pcap is True
    assert page.behaviour_summaries[0].has_html_report is False
    assert page.behaviour_summaries[0].available_sections == ("dns_lookups",)
    assert "raw_json" not in repr(page.behaviour_summaries)
    claims = tuple(cast(Any, value)._test_external_effect_store.claims.values())
    assert len(claims) == 2
    assert {claim.family for claim in claims} == {"vt.behaviour"}


@pytest.mark.asyncio
async def test_search_exposes_descriptors_only_and_order() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["descriptors_only"] == "false"
        assert request.url.params["order"] == "last_submission_date-"
        return httpx.Response(200, json={"data": [{"id": "file-id"}]}, request=request)

    value, client = _adapter(handler)
    try:
        result = await value.intelligence_search(
            'name:"sample file"', descriptors_only=False, order="last_submission_date-"
        )
    finally:
        await client.aclose()
    assert result.descriptors_only is False
    assert result.order == "last_submission_date-"
    claim = next(iter(cast(Any, value)._test_external_effect_store.claims.values()))
    assert claim.family == "vt.intelligence_search"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "query", ['content:"abc"', "content:{AABBCC}", "x" * 2001, "name:a\nname:b"]
)
async def test_search_rejects_short_content_long_or_controlled_queries_without_http(
    query: str,
) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"data": []}, request=request)

    value, client = _adapter(handler)
    try:
        with pytest.raises(VirusTotalInvalidInputError):
            await value.intelligence_search(query)
    finally:
        await client.aclose()
    assert calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "body", "error_type"),
    [
        (
            429,
            {"error": {"code": "TooManyRequests", "message": "try later"}},
            VirusTotalQuotaExceededError,
        ),
        (
            400,
            {"error": {"code": "QuotaExceeded", "message": "daily quota exhausted"}},
            VirusTotalQuotaExceededError,
        ),
        (
            403,
            {"error": {"code": "NotAuthorized", "message": "plan does not allow this"}},
            VirusTotalNotAuthorizedError,
        ),
        (
            401,
            {"error": {"code": "Unauthorized", "message": "authentication required"}},
            VirusTotalNotAuthorizedError,
        ),
    ],
)
async def test_quota_and_authorization_errors_keep_journal_metadata(
    status: int, body: dict[str, Any], error_type: type[VirusTotalError]
) -> None:
    calls: list[str] = []

    def proxy_handler(request: httpx.Request) -> httpx.Response:
        calls.append("proxy")
        return httpx.Response(
            status,
            headers={"Retry-After": "11"} if status == 429 else {},
            json=body,
            request=request,
        )

    def direct_handler(request: httpx.Request) -> httpx.Response:
        calls.append("direct")
        return httpx.Response(200, json={"response_code": 1, "scans": {}}, request=request)

    proxy = httpx.AsyncClient(transport=httpx.MockTransport(proxy_handler))
    direct = httpx.AsyncClient(transport=httpx.MockTransport(direct_handler))
    route = VirusTotalOperationRoute(
        primary=VirusTotalRouteStep(VirusTotalTransportKind.PROXY, VirusTotalEndpointVariant.V3),
        fallbacks=(
            VirusTotalRouteStep(
                VirusTotalTransportKind.DIRECT, VirusTotalEndpointVariant.LEGACY_V2
            ),
        ),
    )
    value = VirusTotalHttpAdapter(
        client=proxy,
        direct_client=direct,
        api_key="mock-key",
        base_url=BASE,
        legacy_base_url="http://www.virustotal.com/vtapi/v2",
        capabilities=VirusTotalCapabilities(file_report=True),
        routing_policy=VirusTotalRoutingPolicy(routes={VirusTotalCapability.FILE_REPORT: route}),
    )
    configure_test_vt_adapter(value)
    try:
        with pytest.raises(error_type) as caught:
            await value.file_report(SHA256)
    finally:
        await direct.aclose()
        await proxy.aclose()
    error = caught.value
    assert calls == ["proxy"]
    assert isinstance(error, VirusTotalHttpError)
    assert error.status_code == status
    assert error.operation is VirusTotalOperation.FILE_REPORT
    assert error.route == "proxy:v3"
    if status == 429:
        assert error.retry_after == 11
    assert error.error_code == body["error"]["code"]
    assert error.error_message == body["error"]["message"]


@pytest.mark.asyncio
async def test_not_found_is_not_empty_and_successful_empty_page_is_terminal() -> None:
    def missing(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": {"code": "NotFound"}}, request=request)

    value, client = _adapter(missing)
    try:
        with pytest.raises(VirusTotalHttpError) as caught:
            await value.file_report(SHA256)
    finally:
        await client.aclose()
    assert caught.value.status_code == 404
    assert caught.value.code == "virustotal_not_found"

    def empty(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": []}, request=request)

    value, client = _adapter(empty)
    try:
        page = await value.object_relationship(VirusTotalObjectType.IP_ADDRESS, "8.8.8.8", "urls")
    finally:
        await client.aclose()
    assert page.empty is True and page.exhaustive is True


@pytest.mark.asyncio
async def test_download_uses_proxy_route_before_ticket_and_hash_checks_bytes() -> None:
    content = b"direct-v3-sample"
    digest = hashlib.sha256(content).hexdigest()
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        assert "x-apikey" not in request.headers
        if request.url.path.endswith("/download"):
            return httpx.Response(200, content=content, request=request)
        pytest.fail("the ticket route must not run after a successful direct download")

    value, client = _adapter(
        handler,
        capabilities=VirusTotalCapabilities(file_download=True),
        file_download_enabled=True,
    )
    sink = io.BytesIO()
    try:
        result = await value.file_download(digest, sink=sink)
    finally:
        await client.aclose()
    assert paths == [f"/api/v3/files/{digest}/download"]
    assert sink.getvalue() == content
    assert result.sha256 == digest


@pytest.mark.asyncio
async def test_download_uses_ticket_only_after_direct_404_and_signed_host_allowlist() -> None:
    content = b"ticket-sample"
    digest = hashlib.sha256(content).hexdigest()
    routes: list[str] = []
    signed_headers: list[httpx.Headers] = []

    def handler(request: httpx.Request) -> httpx.Response:
        routes.append(request.url.path)
        if request.url.path.endswith("/download"):
            return httpx.Response(404, request=request)
        return httpx.Response(
            200,
            json={"data": "https://signed.example.test/blob"},
            request=request,
        )

    def signed_handler(request: httpx.Request) -> httpx.Response:
        signed_headers.append(request.headers)
        return httpx.Response(200, content=content, request=request)

    value, client = _adapter(
        handler,
        capabilities=VirusTotalCapabilities(file_download=True),
        file_download_enabled=True,
        download_allowed_hosts=frozenset({"signed.example.test"}),
        download_client_factory=lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(signed_handler), follow_redirects=False
        ),
    )
    sink = io.BytesIO()
    try:
        result = await value.file_download(digest, sink=sink)
    finally:
        await client.aclose()
    assert routes == [
        f"/api/v3/files/{digest}/download",
        f"/api/v3/files/{digest}/download_url",
    ]
    assert result.sha256 == digest
    assert sink.getvalue() == content
    assert "x-apikey" not in signed_headers[0]
    assert "authorization" not in signed_headers[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [403, 429])
async def test_download_does_not_use_ticket_after_non_404(status: int) -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(status, json={"error": {"code": "RequestDenied"}}, request=request)

    value, client = _adapter(
        handler,
        capabilities=VirusTotalCapabilities(file_download=True),
        file_download_enabled=True,
    )
    try:
        with pytest.raises(VirusTotalError):
            await value.file_download(SHA256, sink=io.BytesIO())
    finally:
        await client.aclose()
    assert paths == [f"/api/v3/files/{SHA256}/download"]


@pytest.mark.asyncio
async def test_download_429_retry_uses_new_dispatch_occurrence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    content = b"direct-v3-sample"
    digest = hashlib.sha256(content).hexdigest()
    requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        if requests == 1:
            return httpx.Response(
                429,
                json={"error": {"code": "QuotaExceeded"}},
                headers={"Retry-After": "1"},
                request=request,
            )
        return httpx.Response(200, content=content, request=request)

    value, client = _adapter(
        handler,
        capabilities=VirusTotalCapabilities(file_download=True),
        file_download_enabled=True,
    )
    authority = current_virustotal_external_effect_authority()
    assert authority is not None
    first_attempt = replace(authority, acquisition_dispatch_attempt_id=uuid4())
    retry_attempt = replace(authority, acquisition_dispatch_attempt_id=uuid4())
    result_marker = object()

    async def run_coordinated_hop(
        client_arg: httpx.AsyncClient | None, url: str, **kwargs: Any
    ) -> Any:
        assert client_arg is not None
        assert kwargs["occurrence_id"] is not None
        response = await kwargs["coordinated_request"](
            client_arg,
            url,
            params=kwargs.get("params"),
            headers={"accept-encoding": "identity"},
            route=kwargs["route"],
            transport="proxy-v3",
            hop_index=0,
            signed=False,
            acquisition_hash=kwargs["acquisition_hash"],
            max_response_bytes=kwargs["max_bytes"],
            occurrence_id=kwargs["occurrence_id"],
        )
        status_code = response.status_code
        await response.aclose()
        if status_code >= 400:
            raise VirusTotalHttpError("synthetic quota response", status_code=status_code)
        return result_marker

    monkeypatch.setattr(virustotal_module, "_stream_route_download", run_coordinated_hop)

    async def send(attempt: Any) -> Any:
        with virustotal_external_effect_scope(attempt):
            return await value.file_download(digest, sink=io.BytesIO())

    try:
        with pytest.raises(VirusTotalHttpError) as caught:
            await send(first_attempt)
        assert caught.value.status_code == 429
        assert await send(retry_attempt) is result_marker
    finally:
        await client.aclose()

    claims = tuple(cast(Any, value)._test_external_effect_store.claims.values())
    assert requests == 2
    assert len(claims) == 2
    assert len({claim.effect_key for claim in claims}) == 2
    assert all(claim.state.name == "COMPLETED" for claim in claims)


@pytest.mark.asyncio
async def test_ambiguous_download_replay_keeps_dispatch_occurrence_and_does_not_send() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ReadError("synthetic interruption after handoff", request=request)

    value, client = _adapter(
        handler,
        capabilities=VirusTotalCapabilities(file_download=True),
        file_download_enabled=True,
    )
    authority = current_virustotal_external_effect_authority()
    assert authority is not None
    attempt = replace(authority, acquisition_dispatch_attempt_id=uuid4())
    try:
        with virustotal_external_effect_scope(attempt):
            for _ in range(2):
                with pytest.raises(VirusTotalExternalEffectNeedsReviewError):
                    await value.file_download(SHA256, sink=io.BytesIO())
    finally:
        await client.aclose()

    claims = tuple(cast(Any, value)._test_external_effect_store.claims.values())
    assert calls == 1
    assert len(claims) == 1
    assert claims[0].state.name == "NEEDS_REVIEW"


@pytest.mark.asyncio
async def test_download_route_stays_absent_until_the_route_flag_is_enabled() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, content=b"", request=request)

    value, client = _adapter(handler, capabilities=VirusTotalCapabilities(file_download=True))
    try:
        with pytest.raises(VirusTotalError) as caught:
            await value.file_download(SHA256, sink=io.BytesIO())
    finally:
        await client.aclose()
    assert caught.value.code == "virustotal_route_unavailable"
    assert calls == 0


@pytest.mark.asyncio
async def test_disabled_capability_makes_no_request() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"data": []}, request=request)

    value, client = _adapter(handler, capabilities=VirusTotalCapabilities())
    try:
        with pytest.raises(VirusTotalCapabilityDisabledError):
            await value.behaviours(SHA256)
    finally:
        await client.aclose()
    assert calls == 0


def test_settings_capability_composition_is_explicit_and_default_off() -> None:
    defaults = virustotal_capabilities_from_settings(Settings(_env_file=None))
    assert defaults == VirusTotalCapabilities()

    configured = virustotal_capabilities_from_settings(
        Settings(
            _env_file=None,
            virustotal_proxy_url="http://proxy.test:8080",
            virustotal_file_report_enabled=True,
            virustotal_object_relationships_enabled=True,
            virustotal_intelligence_search_enabled=True,
            virustotal_behaviour_reports_enabled=True,
            virustotal_file_download_enabled=True,
            virustotal_download_allowed_hosts="vtsamples.commondatastorage.googleapis.com",
        )
    )
    assert configured == VirusTotalCapabilities(
        file_report=True,
        file_relationships=True,
        intelligence_search=True,
        behaviour_reports=True,
        file_download=True,
    )
