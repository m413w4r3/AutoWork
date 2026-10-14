from __future__ import annotations

import hashlib
import json
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
    VirusTotalRouteStep,
    VirusTotalRouteUnavailableError,
    VirusTotalRoutingPolicy,
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

VTUI_ORIGIN = "https://vtui.test"
ALLOWED_HOSTS = frozenset({"vtui.test"})
RULE = 'rule approved { strings: $a = "marker" condition: $a }'


def _route(*, direct: bool = False, fallback: bool = False) -> VirusTotalRoutingPolicy:
    primary = VirusTotalRouteStep(
        transport=(VirusTotalTransportKind.DIRECT if direct else VirusTotalTransportKind.PROXY),
        variant=VirusTotalEndpointVariant.V3,
    )
    fallbacks = (
        (
            VirusTotalRouteStep(
                transport=VirusTotalTransportKind.PROXY,
                variant=VirusTotalEndpointVariant.V3_FALLBACK,
            ),
        )
        if fallback
        else ()
    )
    return VirusTotalRoutingPolicy(
        routes={
            VirusTotalCapability.RETROHUNT: VirusTotalOperationRoute(
                primary=primary,
                fallbacks=fallbacks,
            )
        }
    )


def _client(
    handler,
    *,
    capabilities: VirusTotalCapabilities | None = None,
    routing_policy: VirusTotalRoutingPolicy | None = None,
    fallback_base_url: str | None = None,
    allowed_hosts: frozenset[str] = ALLOWED_HOSTS,
) -> tuple[VirusTotalRetrohuntHttpClient, httpx.AsyncClient]:
    transport = httpx.MockTransport(handler)
    http_client = httpx.AsyncClient(transport=transport, trust_env=False)
    client = VirusTotalRetrohuntHttpClient(
        client=http_client,
        base_url=VTUI_ORIGIN,
        allowed_hosts=allowed_hosts,
        capabilities=capabilities or VirusTotalCapabilities(retrohunt=True),
        routing_policy=routing_policy or _route(),
        fallback_base_url=fallback_base_url,
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
async def test_submit_uses_verified_vtui_route_and_payload_without_api_key() -> None:
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
    assert str(request.url) == f"{VTUI_ORIGIN}/api/tools/retrohunt/jobs"
    assert request.headers["accept"] == "application/json"
    assert "x-apikey" not in request.headers
    assert json.loads(request.content) == {
        "rules": RULE,
        "corpus": "main",
        "start": 1_700_000_000,
        "end": 1_700_001_000,
    }


@pytest.mark.asyncio
async def test_status_and_results_follow_vtui_post_routes_and_cursor_contract() -> None:
    sha_a = "a" * 64
    sha_b = "b" * 64
    requests: list[tuple[str, str, dict[str, object]]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append((request.method, request.url.path, body))
        if request.url.path.endswith("/jobs/get"):
            payload = _job_response(status="in-progress")
        elif body.get("cursor") == "next-page":
            payload = {"data": [{"id": sha_b, "type": "file"}]}
        else:
            payload = {"data": [{"id": sha_a, "type": "file"}], "meta": {"cursor": "next-page"}}
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
    assert requests == [
        ("POST", "/api/tools/retrohunt/jobs/get", {"job_id": "job-1"}),
        (
            "POST",
            "/api/tools/retrohunt/jobs/matching-files",
            {"job_id": "job-1", "limit": 100},
        ),
        (
            "POST",
            "/api/tools/retrohunt/jobs/matching-files",
            {"job_id": "job-1", "limit": 100, "cursor": "next-page"},
        ),
    ]


@pytest.mark.asyncio
async def test_fallback_is_proxy_only_and_only_after_policy_permitted_404() -> None:
    requests: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.host)
        if request.url.host == "vtui.test":
            return httpx.Response(404, json={"message": "route absent"}, request=request)
        return httpx.Response(200, json=_job_response(), request=request)

    fallback_origin = "https://vtui-fallback.test"
    client, http_client = _client(
        handler,
        routing_policy=_route(fallback=True),
        fallback_base_url=fallback_origin,
        allowed_hosts=frozenset({"vtui.test", "vtui-fallback.test"}),
    )
    try:
        job = await client.submit_retrohunt(_submit_request())
    finally:
        await http_client.aclose()

    assert job.job_id == "job-1"
    assert requests == ["vtui.test", "vtui-fallback.test"]

    blocked_requests: list[str] = []

    async def forbidden(request: httpx.Request) -> httpx.Response:
        blocked_requests.append(request.url.host)
        return httpx.Response(403, json={"message": "forbidden"}, request=request)

    client, http_client = _client(
        forbidden,
        routing_policy=_route(fallback=True),
        fallback_base_url=fallback_origin,
        allowed_hosts=frozenset({"vtui.test", "vtui-fallback.test"}),
    )
    try:
        with pytest.raises(VirusTotalNotAuthorizedError):
            await client.submit_retrohunt(_submit_request())
    finally:
        await http_client.aclose()
    assert blocked_requests == ["vtui.test"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("http_status", "expected_error"),
    [
        (401, VirusTotalNotAuthorizedError),
        (403, VirusTotalNotAuthorizedError),
        (404, VirusTotalHttpError),
        (429, VirusTotalQuotaExceededError),
    ],
)
async def test_http_errors_remain_typed_and_do_not_become_job_results(
    http_status: int, expected_error: type[Exception]
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            http_status,
            headers={"Retry-After": "7"} if http_status == 429 else {},
            json={"message": "provider response"},
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
    if http_status == 429:
        assert caught.value.retry_after == 7.0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("content", "expected_error"),
    [
        (b"not-json", VirusTotalJsonError),
        (b"[]", VirusTotalPayloadError),
        (b'{"data":{"id":"job-1","type":"retrohunt_job"}}', VirusTotalPayloadError),
    ],
)
async def test_malformed_or_partial_job_responses_are_typed_errors(
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
async def test_partial_matching_result_page_is_not_returned_as_a_hit_set() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"id": "not-a-sha256"}]}, request=request)

    client, http_client = _client(handler)
    try:
        with pytest.raises(VirusTotalPayloadError):
            await client.retrohunt_results("job-1")
    finally:
        await http_client.aclose()


@pytest.mark.asyncio
async def test_client_is_unavailable_without_explicit_proxy_route_or_with_direct_route() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("unavailable Retrohunt must not make a request")

    no_route, no_route_client = _client(handler, routing_policy=VirusTotalRoutingPolicy())
    direct_route, direct_client = _client(handler, routing_policy=_route(direct=True))
    no_capability, no_capability_client = _client(
        handler,
        capabilities=VirusTotalCapabilities(),
    )
    try:
        assert not no_route.available
        assert "route" in (no_route.available_reason or "")
        assert not direct_route.available
        assert "proxy" in (direct_route.available_reason or "")
        assert not no_capability.available
        assert "désactivée" in (no_capability.available_reason or "")
        with pytest.raises(VirusTotalRouteUnavailableError):
            await no_route.retrohunt_status("job-1")
    finally:
        await no_route_client.aclose()
        await direct_client.aclose()
        await no_capability_client.aclose()


@pytest.mark.asyncio
async def test_vtui_origin_requires_explicit_host_allowlist() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("invalid origin must not make a request")

    transport = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with pytest.raises(VirusTotalRouteUnavailableError):
        VirusTotalRetrohuntHttpClient(
            client=transport,
            base_url="https://unapproved.test",
            allowed_hosts=ALLOWED_HOSTS,
            capabilities=VirusTotalCapabilities(retrohunt=True),
            routing_policy=_route(),
        )
    with pytest.raises(VirusTotalRouteUnavailableError):
        VirusTotalRetrohuntHttpClient(
            client=transport,
            base_url="https://www.virustotal.com",
            allowed_hosts=frozenset({"www.virustotal.com"}),
            capabilities=VirusTotalCapabilities(retrohunt=True),
            routing_policy=_route(),
        )
    await transport.aclose()


@pytest.mark.asyncio
async def test_submit_rejects_modified_rule_and_unmapped_parameters_before_http() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("invalid submission must not make a request")

    client, http_client = _client(handler)
    try:
        modified = _submit_request(rule_source=f" {RULE}")
        with pytest.raises(VirusTotalInvalidInputError):
            await client.submit_retrohunt(modified)
        with pytest.raises(VirusTotalInvalidInputError):
            await client.submit_retrohunt(_submit_request(parameters={"window_days": 30}))
    finally:
        await http_client.aclose()
