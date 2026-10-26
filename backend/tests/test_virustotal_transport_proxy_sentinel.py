from __future__ import annotations

import hashlib
import io
from collections.abc import Callable

import httpx
import pytest

from cti_app.application.virustotal import (
    VirusTotalCapabilities,
    VirusTotalHttpError,
    VirusTotalOperationRoute,
    VirusTotalRouteStep,
    VirusTotalRoutingPolicy,
)
from cti_app.config import Settings
from cti_app.domain.virustotal import (
    VirusTotalCapability,
    VirusTotalEndpointVariant,
    VirusTotalTransportKind,
)
from cti_app.infrastructure.investigation_runtime_ports import (
    configure_virustotal_proxy_http_client,
)
from cti_app.infrastructure.virustotal import VirusTotalHttpAdapter
from cti_app.infrastructure.virustotal_retrohunt_client import (
    VirusTotalRetrohuntHttpClient,
)
from tests.virustotal_external_effect_fakes import (
    authorized_vt_download,
    authorized_vt_read,
    configure_test_vt_adapter,
)

BASE = "https://www.virustotal.com/api/v3"
CDN_HOST = "vtsamples.commondatastorage.googleapis.com"
PROXY_SECRET = "0123456789abcdef" * 4
SHA256 = "a" * 64
PCAP_BYTES = b"\xd4\xc3\xb2\xa1" + (b"\x00" * 20)


def _strict_proxy(
    routes: dict[str, Callable[[httpx.Request, str], httpx.Response]],
) -> tuple[Callable[[httpx.Request], httpx.Response], list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        route = routes.get(request.url.path)
        if route is None:
            pytest.fail(f"unexpected proxy request: {request.method} {request.url.path}")
        raw_query = request.url.query.decode("ascii")
        # Match the deployed proxy's raw string rewrite: it appends '&apikey='
        # rather than building HTTP query parameters.
        rewritten_url = httpx.URL(f"{request.url}&apikey={PROXY_SECRET}")
        if not raw_query:
            # Without an existing '?', the appended fragment becomes part of
            # the path. Domain validation echoes that malformed identifier.
            if request.url.path.startswith("/api/v3/domains/"):
                malformed_domain = rewritten_url.path.rsplit("/", 1)[-1]
                return httpx.Response(
                    400,
                    json={
                        "error": {
                            "code": "InvalidArgumentError",
                            "message": f"Domain \"b'{malformed_domain}'\" is not valid",
                        }
                    },
                    request=request,
                )
            return httpx.Response(
                404,
                json={"error": {"code": "NotFoundError", "message": "not found"}},
                request=request,
            )
        rewritten_query = rewritten_url.query.decode("ascii")
        return route(request, rewritten_query)

    return handler, seen


def _file_report_response(request: httpx.Request, rewritten_query: str) -> httpx.Response:
    assert rewritten_query == "x=1&apikey=" + PROXY_SECRET
    return httpx.Response(
        200,
        json={
            "data": {
                "id": SHA256,
                "type": "file",
                "attributes": {"sha256": SHA256, "last_analysis_stats": {"malicious": 0}},
            }
        },
        request=request,
    )


@pytest.mark.asyncio
async def test_file_report_uses_sentinel_for_proxy_without_query_parameters() -> None:
    handler, seen = _strict_proxy({f"/api/v3/files/{SHA256}": _file_report_response})
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = VirusTotalHttpAdapter(
        client=client,
        base_url=BASE,
        capabilities=VirusTotalCapabilities(file_report=True),
        proxy_query_sentinel_enabled=True,
    )
    configure_test_vt_adapter(adapter)
    try:
        with authorized_vt_read():
            result = await adapter.file_report(SHA256)
    finally:
        await client.aclose()

    assert result.file.id == SHA256
    assert str(seen[0].url) == f"{BASE}/files/{SHA256}?x=1"
    assert "x-apikey" not in seen[0].headers


@pytest.mark.asyncio
async def test_object_report_retry_without_projection_still_has_proxy_query() -> None:
    seen_queries: list[str] = []

    def domain_route(request: httpx.Request, rewritten_query: str) -> httpx.Response:
        seen_queries.append(request.url.query.decode("ascii"))
        if "relationships=" in rewritten_query:
            return httpx.Response(
                403,
                json={
                    "error": {
                        "code": "ForbiddenError",
                        "message": (
                            f"Domain \"b'drivegoogel.cam&apikey={PROXY_SECRET}'\" is invalid"
                        ),
                    }
                },
                request=request,
            )
        assert rewritten_query == "x=1&apikey=" + PROXY_SECRET
        return httpx.Response(
            200,
            json={"data": {"id": "drivegoogel.cam", "type": "domain", "attributes": {}}},
            request=request,
        )

    handler, seen = _strict_proxy({"/api/v3/domains/drivegoogel.cam": domain_route})
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = VirusTotalHttpAdapter(
        client=client,
        base_url=BASE,
        capabilities=VirusTotalCapabilities(file_report=True),
        proxy_query_sentinel_enabled=True,
    )
    configure_test_vt_adapter(adapter)
    try:
        with authorized_vt_read():
            result = await adapter.object_report("domains", "drivegoogel.cam")
    finally:
        await client.aclose()

    assert result.object_id == "drivegoogel.cam"
    assert len(seen) == 2
    assert "relationships=" in seen_queries[0]
    assert seen_queries[1] == "x=1"


@pytest.mark.asyncio
async def test_file_download_302_uses_exact_cdn_without_proxy_credentials() -> None:
    sample = b"authorized synthetic sample"
    sample_hash = hashlib.sha256(sample).hexdigest()
    signed_url = f"https://{CDN_HOST}/object?GoogleAccessId=private&Signature=signed"
    handler, proxy_requests = _strict_proxy(
        {
            f"/api/v3/files/{sample_hash}/download": lambda request, rewritten: (
                httpx.Response(
                    302,
                    headers={"Location": signed_url},
                    request=request,
                )
                if rewritten == "x=1&apikey=" + PROXY_SECRET
                else pytest.fail(f"unexpected rewritten proxy query: {rewritten}")
            )
        }
    )
    cdn_requests: list[httpx.Request] = []

    def cdn_handler(request: httpx.Request) -> httpx.Response:
        cdn_requests.append(request)
        return httpx.Response(
            200,
            content=sample,
            headers={"Content-Type": "application/octet-stream"},
            request=request,
        )

    proxy_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = VirusTotalHttpAdapter(
        client=proxy_client,
        base_url=BASE,
        capabilities=VirusTotalCapabilities(file_download=True),
        file_download_enabled=True,
        proxy_query_sentinel_enabled=True,
        download_allowed_hosts=frozenset({CDN_HOST}),
        download_client_factory=lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(cdn_handler),
            headers={"x-apikey": PROXY_SECRET, "cookie": "session=private"},
            trust_env=False,
        ),
    )
    configure_test_vt_adapter(adapter)
    sink = io.BytesIO()
    try:
        with authorized_vt_download():
            result = await adapter.file_download(sample_hash, sink=sink)
    finally:
        await proxy_client.aclose()

    assert result.sha256 == sample_hash
    assert sink.getvalue() == sample
    assert len(proxy_requests) == 1
    assert proxy_requests[0].url.query == b"x=1"
    assert len(cdn_requests) == 1
    assert cdn_requests[0].url.scheme == "https"
    assert cdn_requests[0].url.host == CDN_HOST
    assert cdn_requests[0].url.port in {None, 443}
    assert "x-apikey" not in cdn_requests[0].headers
    assert "cookie" not in cdn_requests[0].headers
    assert PROXY_SECRET not in str(cdn_requests[0].url)


@pytest.mark.asyncio
async def test_download_ticket_has_sentinel_and_signed_cdn_has_no_authentication() -> None:
    sample = b"ticketed synthetic sample"
    sample_hash = hashlib.sha256(sample).hexdigest()
    signed_url = f"https://{CDN_HOST}/ticket-object?GoogleAccessId=private&Signature=signed"
    seen_queries: list[tuple[str, str]] = []

    def route(request: httpx.Request, rewritten_query: str) -> httpx.Response:
        seen_queries.append((request.url.path, request.url.query.decode("ascii")))
        if request.url.path.endswith("/download"):
            return httpx.Response(404, json={"error": {"code": "NotFoundError"}}, request=request)
        assert rewritten_query == "x=1&apikey=" + PROXY_SECRET
        return httpx.Response(200, json={"data": signed_url}, request=request)

    handler, _ = _strict_proxy(
        {
            f"/api/v3/files/{sample_hash}/download": route,
            f"/api/v3/files/{sample_hash}/download_url": route,
        }
    )
    cdn_requests: list[httpx.Request] = []

    def cdn_handler(request: httpx.Request) -> httpx.Response:
        cdn_requests.append(request)
        return httpx.Response(200, content=sample, request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = VirusTotalHttpAdapter(
        client=client,
        base_url=BASE,
        capabilities=VirusTotalCapabilities(file_download=True),
        file_download_enabled=True,
        proxy_query_sentinel_enabled=True,
        download_allowed_hosts=frozenset({CDN_HOST}),
        download_client_factory=lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(cdn_handler), trust_env=False
        ),
    )
    configure_test_vt_adapter(adapter)
    try:
        with authorized_vt_download():
            result = await adapter.file_download(sample_hash, sink=io.BytesIO())
    finally:
        await client.aclose()

    assert result.sha256 == sample_hash
    assert seen_queries == [
        (f"/api/v3/files/{sample_hash}/download", "x=1"),
        (f"/api/v3/files/{sample_hash}/download_url", "x=1"),
    ]
    assert len(cdn_requests) == 1
    assert "x-apikey" not in cdn_requests[0].headers
    assert "apikey" not in cdn_requests[0].url.params


@pytest.mark.asyncio
async def test_direct_v2_download_keeps_only_documented_params_without_sentinel() -> None:
    sample = b"private-v2 synthetic sample"
    sample_hash = hashlib.sha256(sample).hexdigest()

    def route_missing(request: httpx.Request, rewritten_query: str) -> httpx.Response:
        assert rewritten_query == "x=1&apikey=" + PROXY_SECRET
        return httpx.Response(
            404,
            headers={"Via": "1.1 configured-proxy"},
            json={"message": "route not found"},
            request=request,
        )

    proxy_handler, _ = _strict_proxy(
        {
            f"/api/v3/files/{sample_hash}/download": route_missing,
            f"/api/v3/files/{sample_hash}/download_url": route_missing,
        }
    )

    def direct_handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/vtapi/v2/file/download"
        assert set(request.url.params) == {"apikey", "hash"}
        assert request.url.params["apikey"] == PROXY_SECRET
        assert request.url.params["hash"] == sample_hash
        return httpx.Response(200, content=sample, request=request)

    proxy = httpx.AsyncClient(transport=httpx.MockTransport(proxy_handler))
    direct = httpx.AsyncClient(
        transport=httpx.MockTransport(direct_handler),
        headers={"x-apikey": PROXY_SECRET},
        trust_env=False,
    )
    adapter = VirusTotalHttpAdapter(
        client=proxy,
        direct_client=direct,
        api_key=PROXY_SECRET,
        base_url=BASE,
        legacy_base_url="https://www.virustotal.com/vtapi/v2",
        capabilities=VirusTotalCapabilities(file_download=True),
        file_download_enabled=True,
        file_download_legacy_fallback_enabled=True,
        proxy_query_sentinel_enabled=True,
        download_allowed_hosts=frozenset({CDN_HOST}),
    )
    configure_test_vt_adapter(adapter)
    try:
        with authorized_vt_download():
            result = await adapter.file_download(sample_hash, sink=io.BytesIO())
    finally:
        await direct.aclose()
        await proxy.aclose()

    assert result.sha256 == sample_hash


@pytest.mark.asyncio
async def test_behaviour_pcap_queryless_proxy_uses_sentinel() -> None:
    def pcap_route(request: httpx.Request, rewritten_query: str) -> httpx.Response:
        assert rewritten_query == "x=1&apikey=" + PROXY_SECRET
        return httpx.Response(200, content=PCAP_BYTES, request=request)

    handler, seen = _strict_proxy({"/api/v3/file_behaviours/sandbox-1/pcap": pcap_route})
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = VirusTotalHttpAdapter(
        client=client,
        base_url=BASE,
        capabilities=VirusTotalCapabilities(behaviour_pcap=True),
        behaviour_pcap_enabled=True,
        proxy_query_sentinel_enabled=True,
    )
    configure_test_vt_adapter(adapter)
    try:
        with authorized_vt_read():
            result = await adapter.behaviour_pcap("sandbox-1", sink=io.BytesIO())
    finally:
        await client.aclose()

    assert result.sha256 == hashlib.sha256(PCAP_BYTES).hexdigest()
    assert seen[0].url.query == b"x=1"


@pytest.mark.asyncio
async def test_relationship_and_behaviour_existing_queries_survive_proxy_rewrite() -> None:
    def collection_route(request: httpx.Request, rewritten_query: str) -> httpx.Response:
        assert "limit=" in rewritten_query
        assert "&apikey=" + PROXY_SECRET in rewritten_query
        return httpx.Response(200, json={"data": []}, request=request)

    handler, seen = _strict_proxy(
        {
            f"/api/v3/files/{SHA256}/contacted_domains": collection_route,
            f"/api/v3/files/{SHA256}/behaviours": collection_route,
        }
    )
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = VirusTotalHttpAdapter(
        client=client,
        base_url=BASE,
        capabilities=VirusTotalCapabilities(file_relationships=True, behaviour_reports=True),
        proxy_query_sentinel_enabled=True,
    )
    configure_test_vt_adapter(adapter)
    try:
        with authorized_vt_read():
            await adapter.file_relationship(SHA256, "contacted_domains")
            await adapter.behaviours(SHA256)
    finally:
        await client.aclose()

    assert len(seen) == 2
    assert all(request.url.params["limit"] for request in seen)
    assert all("x" not in request.url.params for request in seen)


@pytest.mark.asyncio
async def test_retrohunt_proxy_client_gets_sentinel_from_shared_client_configuration() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v3/intelligence/retrohunt_jobs/job-1"
        assert request.url.query == b"x=1"
        assert request.headers["x-apikey"] == PROXY_SECRET
        return httpx.Response(
            200,
            json={
                "data": {
                    "id": "job-1",
                    "type": "retrohunt_job",
                    "attributes": {"status": "queued"},
                }
            },
            request=request,
        )

    settings = Settings(
        _env_file=None,
        virustotal_proxy_url="http://proxy.test:8080",
        virustotal_api_key=PROXY_SECRET,
    )
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), trust_env=False)
    configure_virustotal_proxy_http_client(client, settings)
    route = VirusTotalOperationRoute(
        primary=VirusTotalRouteStep(
            transport=VirusTotalTransportKind.PROXY,
            variant=VirusTotalEndpointVariant.V3,
        )
    )
    retrohunt = VirusTotalRetrohuntHttpClient(
        client=client,
        base_url=BASE,
        api_key=PROXY_SECRET,
        proxy_url=settings.virustotal_proxy_url,
        capabilities=VirusTotalCapabilities(retrohunt=True),
        routing_policy=VirusTotalRoutingPolicy(routes={VirusTotalCapability.RETROHUNT: route}),
    )
    try:
        result = await retrohunt.retrohunt_status("job-1")
    finally:
        await client.aclose()

    assert result.job_id == "job-1"


@pytest.mark.asyncio
async def test_shared_proxy_configuration_adds_sentinel_to_queryless_usage_route() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v3/users/me"
        assert request.url.query == b"x=1"
        return httpx.Response(200, json={"data": {}}, request=request)

    settings = Settings(_env_file=None, virustotal_proxy_url="http://proxy.test:8080")
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), trust_env=False)
    configure_virustotal_proxy_http_client(client, settings)
    try:
        response = await client.get(f"{BASE}/users/me")
    finally:
        await client.aclose()

    assert response.status_code == 200


def test_proxy_sentinel_setting_changes_download_cache_fingerprints() -> None:
    common = {
        "client": httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200))),
        "base_url": BASE,
        "capabilities": VirusTotalCapabilities(file_download=True),
        "file_download_enabled": True,
        "download_allowed_hosts": frozenset({CDN_HOST}),
    }
    enabled_client = common["client"]
    disabled_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda req: httpx.Response(200))
    )
    try:
        enabled = VirusTotalHttpAdapter(**common, proxy_query_sentinel_enabled=True)
        disabled = VirusTotalHttpAdapter(
            **{**common, "client": disabled_client}, proxy_query_sentinel_enabled=False
        )
        assert enabled.sample_acquisition_cache_scope.route_fingerprint != (
            disabled.sample_acquisition_cache_scope.route_fingerprint
        )
        assert enabled.sample_acquisition_cache_scope.configuration_fingerprint != (
            disabled.sample_acquisition_cache_scope.configuration_fingerprint
        )
    finally:
        import asyncio

        asyncio.run(enabled_client.aclose())
        asyncio.run(disabled_client.aclose())


@pytest.mark.asyncio
async def test_proxy_error_body_echo_is_redacted_from_exception_and_logs(caplog) -> None:
    def error_route(request: httpx.Request, rewritten_query: str) -> httpx.Response:
        assert rewritten_query == "x=1&apikey=" + PROXY_SECRET
        return httpx.Response(
            400,
            json={
                "error": {
                    "code": "InvalidArgumentError",
                    "message": f"Domain \"b'drivegoogel.cam&apikey={PROXY_SECRET}'\" is invalid",
                }
            },
            request=request,
        )

    handler, _ = _strict_proxy({f"/api/v3/files/{SHA256}": error_route})
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = VirusTotalHttpAdapter(
        client=client,
        base_url=BASE,
        capabilities=VirusTotalCapabilities(file_report=True),
        proxy_query_sentinel_enabled=True,
    )
    configure_test_vt_adapter(adapter)
    try:
        with authorized_vt_read():
            with pytest.raises(VirusTotalHttpError) as caught:
                await adapter.file_report(SHA256)
    finally:
        await client.aclose()

    assert PROXY_SECRET not in str(caught.value)
    assert PROXY_SECRET not in repr(caught.value)
    assert PROXY_SECRET not in (caught.value.error_message or "")
    assert "apikey=[REDACTED]" in (caught.value.error_message or "")
    assert PROXY_SECRET not in caplog.text
