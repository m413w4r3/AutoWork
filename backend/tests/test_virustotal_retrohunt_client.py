from __future__ import annotations

import hashlib
import json
import logging
from uuid import uuid4

import httpx
import pytest

from cti_app.application.virustotal import (
    VirusTotalCapabilities,
    VirusTotalError,
    VirusTotalHttpError,
    VirusTotalInvalidInputError,
    VirusTotalJsonError,
    VirusTotalNotAuthorizedError,
    VirusTotalOperationRoute,
    VirusTotalPayloadError,
    VirusTotalQuotaExceededError,
    VirusTotalResponseTooLargeError,
    VirusTotalRouteStep,
    VirusTotalRouteUnavailableError,
    VirusTotalRoutingPolicy,
    VirusTotalTransportError,
)
from cti_app.domain.retrohunt import (
    RetrohuntProviderJobState,
    RetrohuntSubmitRequest,
)
from cti_app.domain.virustotal import (
    VirusTotalCapability,
    VirusTotalEndpointVariant,
    VirusTotalTransportKind,
)
from cti_app.infrastructure.virustotal_retrohunt_client import (
    VirusTotalRetrohuntHttpClient,
)

VT_BASE_URL = "https://www.virustotal.com/api/v3"
PROXY_URL = "http://proxy.test:8080"
API_KEY = "vt-test-secret-do-not-log"
RULE = 'rule approved { strings: $a = "marker" condition: $a }'


def _route(
    *,
    transport: VirusTotalTransportKind = VirusTotalTransportKind.PROXY,
    variant: VirusTotalEndpointVariant = VirusTotalEndpointVariant.V3,
    fallback: bool = False,
) -> VirusTotalRoutingPolicy:
    steps = VirusTotalOperationRoute(
        primary=VirusTotalRouteStep(transport=transport, variant=variant),
        fallbacks=(
            (VirusTotalRouteStep(transport=transport, variant=variant),) if fallback else ()
        ),
    )
    return VirusTotalRoutingPolicy(routes={VirusTotalCapability.RETROHUNT: steps})


def _client(
    handler,
    *,
    capabilities: VirusTotalCapabilities | None = None,
    routing_policy: VirusTotalRoutingPolicy | None = None,
    base_url: str | None = VT_BASE_URL,
    api_key: str | None = API_KEY,
    proxy_url: str | None = PROXY_URL,
    max_response_bytes: int = 10 * 1024 * 1024,
    max_result_pages: int = 20,
    max_results: int = 1000,
) -> tuple[VirusTotalRetrohuntHttpClient, httpx.AsyncClient]:
    http_client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        trust_env=False,
    )
    client = VirusTotalRetrohuntHttpClient(
        client=http_client,
        base_url=base_url,
        api_key=api_key,
        proxy_url=proxy_url,
        capabilities=capabilities or VirusTotalCapabilities(retrohunt=True),
        routing_policy=routing_policy or _route(),
        max_response_bytes=max_response_bytes,
        max_result_pages=max_result_pages,
        max_results=max_results,
    )
    return client, http_client


def _submit_request(
    *, rule_source: str = RULE, parameters: dict[str, object] | None = None
) -> RetrohuntSubmitRequest:
    return RetrohuntSubmitRequest(
        subject_id=uuid4(),
        investigation_id=uuid4(),
        reservation_id=uuid4(),
        slot_number=1,
        rule_version_id=uuid4(),
        rule_source=rule_source,
        rule_source_sha256=hashlib.sha256(rule_source.encode()).hexdigest(),
        corpus_id="main",
        corpus_sha256="a" * 64,
        parameters=parameters or {},
    )


def _job_response(job_id: str = "job-1", status: str = "queued") -> dict[str, object]:
    return {
        "data": {
            "id": job_id,
            "type": "retrohunt_job",
            "attributes": {"status": status},
        }
    }


@pytest.mark.asyncio
async def test_submit_uses_only_the_v3_job_route_and_header_authentication() -> None:
    observed: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        observed.append(request)
        return httpx.Response(200, json=_job_response(), request=request)

    client, http_client = _client(handler)
    try:
        job = await client.submit_retrohunt(
            _submit_request(parameters={"start": 1_700_000_000, "end": 1_700_001_000})
        )
    finally:
        await http_client.aclose()

    assert client.available
    assert job.job_id == "job-1"
    assert job.state is RetrohuntProviderJobState.QUEUED
    assert len(observed) == 1
    request = observed[0]
    assert request.method == "POST"
    assert str(request.url) == f"{VT_BASE_URL}/intelligence/retrohunt_jobs"
    assert request.headers["accept"] == "application/json"
    assert request.headers["x-apikey"] == API_KEY
    assert API_KEY not in str(request.url)
    assert json.loads(request.content) == {
        "data": {
            "type": "retrohunt_job",
            "attributes": {
                "rules": RULE,
                "corpus": "main",
                "start_date": "2023-11-14T22:13:20+00:00",
                "end_date": "2023-11-14T22:30:00+00:00",
            },
        }
    }


@pytest.mark.asyncio
async def test_status_and_results_use_get_routes_and_bounded_cursor_pagination() -> None:
    sha_a = "a" * 64
    sha_b = "b" * 64
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/matching_files"):
            if request.url.params.get("cursor") == "next-page":
                payload: dict[str, object] = {"data": [{"id": sha_b, "type": "file"}]}
            else:
                payload = {
                    "data": [{"id": sha_a, "type": "file"}],
                    "meta": {"cursor": "next-page"},
                }
        else:
            payload = _job_response(status="in-progress")
        return httpx.Response(200, json=payload, request=request)

    client, http_client = _client(handler)
    try:
        status = await client.retrohunt_status("job-1")
        results = await client.retrohunt_results("job-1")
    finally:
        await http_client.aclose()

    assert status.state is RetrohuntProviderJobState.RUNNING
    assert results.job_id == "job-1"
    assert [hit.sha256 for hit in results.hits] == [sha_a, sha_b]
    assert [(item.method, item.url.path) for item in requests] == [
        ("GET", "/api/v3/intelligence/retrohunt_jobs/job-1"),
        ("GET", "/api/v3/intelligence/retrohunt_jobs/job-1/matching_files"),
        ("GET", "/api/v3/intelligence/retrohunt_jobs/job-1/matching_files"),
    ]
    assert [item.url.params.get("cursor") for item in requests] == [None, None, "next-page"]
    assert all(item.headers["x-apikey"] == API_KEY for item in requests)
    assert all(API_KEY not in str(item.url) for item in requests)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("http_status", "expected_error", "expected_code"),
    [
        (401, VirusTotalNotAuthorizedError, "vt_unauthorized"),
        (403, VirusTotalNotAuthorizedError, "vt_forbidden"),
        (404, VirusTotalHttpError, "vt_http_404"),
        (429, VirusTotalQuotaExceededError, "vt_rate_limited"),
        (503, VirusTotalHttpError, "vt_http_503"),
    ],
)
async def test_http_errors_are_typed_and_never_disclose_the_api_key(
    http_status: int,
    expected_error: type[Exception],
    expected_code: str,
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            http_status,
            headers={"Retry-After": "7"} if http_status == 429 else {},
            json={"message": API_KEY},
            request=request,
        )

    client, http_client = _client(handler)
    try:
        with pytest.raises(expected_error) as caught:
            await client.submit_retrohunt(_submit_request())
    finally:
        await http_client.aclose()

    assert isinstance(caught.value, VirusTotalError)
    assert caught.value.status_code == http_status
    assert caught.value.code == expected_code
    assert API_KEY not in str(caught.value)
    if http_status == 429:
        assert caught.value.retry_after == 7.0


@pytest.mark.asyncio
async def test_timeout_and_transport_errors_are_sanitized(caplog: pytest.LogCaptureFixture) -> None:
    async def timeout(_request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout(API_KEY)

    caplog.set_level(logging.DEBUG)
    client, http_client = _client(timeout)
    try:
        with pytest.raises(VirusTotalTransportError) as caught:
            await client.submit_retrohunt(_submit_request())
    finally:
        await http_client.aclose()

    assert caught.value.code == "vt_timeout"
    assert API_KEY not in str(caught.value)
    assert API_KEY not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("content", "expected_error"),
    [
        (b"not-json", VirusTotalJsonError),
        (b"[]", VirusTotalPayloadError),
        (b'{"data":{"id":"job-1","type":"retrohunt_job"}}', VirusTotalPayloadError),
    ],
)
async def test_malformed_or_partial_v3_job_responses_are_typed_errors(
    content: bytes, expected_error: type[Exception]
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=content, request=request)

    client, http_client = _client(handler)
    try:
        with pytest.raises(expected_error):
            await client.submit_retrohunt(_submit_request())
    finally:
        await http_client.aclose()


@pytest.mark.asyncio
async def test_matching_pages_stop_at_configured_result_and_page_limits() -> None:
    sha_a = "a" * 64
    sha_b = "b" * 64

    async def two_pages(request: httpx.Request) -> httpx.Response:
        payload = (
            {"data": [{"id": sha_b, "type": "file"}]}
            if request.url.params.get("cursor")
            else {"data": [{"id": sha_a, "type": "file"}], "meta": {"cursor": "next"}}
        )
        return httpx.Response(200, json=payload, request=request)

    client, http_client = _client(two_pages, max_result_pages=1)
    try:
        with pytest.raises(VirusTotalResponseTooLargeError):
            await client.retrohunt_results("job-1")
    finally:
        await http_client.aclose()

    client, http_client = _client(two_pages, max_results=1)
    try:
        with pytest.raises(VirusTotalResponseTooLargeError):
            await client.retrohunt_results("job-1")
    finally:
        await http_client.aclose()


@pytest.mark.asyncio
async def test_response_size_duplicate_hits_and_cursor_cycles_fail_closed() -> None:
    async def large_response(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b" " * 64, request=request)

    client, http_client = _client(large_response, max_response_bytes=32)
    try:
        with pytest.raises(VirusTotalResponseTooLargeError):
            await client.submit_retrohunt(_submit_request())
    finally:
        await http_client.aclose()

    sha = "a" * 64

    async def duplicate(request: httpx.Request) -> httpx.Response:
        payload = {
            "data": [{"id": sha, "type": "file"}],
            "meta": {"cursor": "again"},
        }
        return httpx.Response(200, json=payload, request=request)

    client, http_client = _client(duplicate)
    try:
        with pytest.raises(VirusTotalPayloadError):
            await client.retrohunt_results("job-1")
    finally:
        await http_client.aclose()


@pytest.mark.asyncio
async def test_client_requires_proxy_key_capability_and_a_single_v3_proxy_route() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("unavailable Retrohunt must not make a request")

    disabled_route, client_a = _client(handler, routing_policy=VirusTotalRoutingPolicy())
    direct_route, client_b = _client(
        handler,
        routing_policy=_route(transport=VirusTotalTransportKind.DIRECT),
    )
    no_key, client_c = _client(handler, api_key=None)
    no_proxy, client_d = _client(handler, proxy_url=None)
    try:
        assert not disabled_route.available
        assert not direct_route.available
        assert not no_key.available
        assert not no_proxy.available
        with pytest.raises(VirusTotalRouteUnavailableError):
            await no_key.retrohunt_status("job-1")
    finally:
        for item in (client_a, client_b, client_c, client_d):
            await item.aclose()


@pytest.mark.asyncio
async def test_submit_rejects_bad_rule_parameters_and_unknown_corpus_without_http() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("invalid submission must not make a request")

    client, http_client = _client(handler)
    try:
        with pytest.raises(VirusTotalInvalidInputError):
            await client.submit_retrohunt(_submit_request(rule_source=f" {RULE}"))
        with pytest.raises(VirusTotalInvalidInputError):
            await client.submit_retrohunt(_submit_request(parameters={"window_days": 30}))
        with pytest.raises(VirusTotalInvalidInputError):
            await client.retrohunt_status(f"{API_KEY}/job")
    finally:
        await http_client.aclose()


def test_base_url_must_be_the_official_https_v3_origin() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("invalid URL must not make a request")

    transport = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(VirusTotalRouteUnavailableError):
            VirusTotalRetrohuntHttpClient(
                client=transport,
                base_url="https://unapproved.test/api/v3",
                api_key=API_KEY,
                proxy_url=PROXY_URL,
                capabilities=VirusTotalCapabilities(retrohunt=True),
                routing_policy=_route(),
            )
        with pytest.raises(VirusTotalRouteUnavailableError):
            VirusTotalRetrohuntHttpClient(
                client=transport,
                base_url="https://www.virustotal.com/api/tools",
                api_key=API_KEY,
                proxy_url=PROXY_URL,
                capabilities=VirusTotalCapabilities(retrohunt=True),
                routing_policy=_route(),
            )
    finally:
        import asyncio

        asyncio.run(transport.aclose())
