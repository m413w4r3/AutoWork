from __future__ import annotations

import base64
import hashlib
import logging
import re
import traceback
from collections.abc import Callable
from io import BytesIO
from typing import Any, cast

import httpx
import pytest

from cti_app.application.sample_acquisition import (
    SampleAcquisitionCacheScope,
    SampleAcquisitionResponseOrigin,
)
from cti_app.application.virustotal import (
    VirusTotalCapabilities,
    VirusTotalDownloadHashMismatchError,
    VirusTotalDownloadHostNotAllowedError,
    VirusTotalDownloadTooLargeError,
    VirusTotalError,
    VirusTotalExternalEffectRefusedError,
    VirusTotalHttpError,
    VirusTotalOperationRoute,
    VirusTotalPayloadError,
    VirusTotalRouteStep,
    VirusTotalRoutingPolicy,
    VirusTotalSignedUrlExpiredError,
    VirusTotalUnexpectedRedirectError,
    virustotal_external_effect_scope,
)
from cti_app.domain.virustotal import (
    VirusTotalCapability,
    VirusTotalEndpointVariant,
    VirusTotalTransportKind,
)
from cti_app.infrastructure.virustotal import VirusTotalHttpAdapter
from tests.virustotal_external_effect_fakes import (
    authorized_vt_read,
    configure_test_vt_adapter,
)

BASE = "https://www.virustotal.com/api/v3"
LEGACY_BASE = "https://www.virustotal.com/vtapi/v2"
SHA256 = hashlib.sha256(b"sample-bytes").hexdigest()
API_KEY = "fake-vt-api-key-must-never-leak"
SIGNED_URL = "https://cdn.example.test/object.bin?signature=do-not-log-this"


@pytest.fixture(autouse=True)
def _external_effect_authority():
    with authorized_vt_read():
        yield


def _route(capability: VirusTotalCapability) -> VirusTotalRoutingPolicy:
    return VirusTotalRoutingPolicy(
        routes={
            capability: VirusTotalOperationRoute(
                primary=VirusTotalRouteStep(
                    VirusTotalTransportKind.PROXY,
                    VirusTotalEndpointVariant.V3,
                )
            )
        }
    )


def _operation_route(capability: VirusTotalCapability) -> VirusTotalOperationRoute:
    route = _route(capability).route_for(capability)
    assert route is not None
    return route


def _unexpected_request(request: httpx.Request) -> httpx.Response:
    pytest.fail(f"unexpected request: {request.url!s}")


def _make_adapter(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    cdn_handler: Callable[[httpx.Request], httpx.Response] | None = None,
    direct_handler: Callable[[httpx.Request], httpx.Response] | None = None,
    download_allowed_hosts: frozenset[str] = frozenset({"cdn.example.test"}),
    download_max_redirects: int = 3,
    **kwargs: Any,
) -> tuple[VirusTotalHttpAdapter, list[httpx.AsyncClient]]:
    clients: list[httpx.AsyncClient] = []

    def client(handler_: Callable[[httpx.Request], httpx.Response], **client_kwargs: Any):
        result = httpx.AsyncClient(
            transport=httpx.MockTransport(handler_),
            trust_env=False,
            **client_kwargs,
        )
        clients.append(result)
        return result

    proxy = client(handler, headers={"x-apikey": API_KEY, "cookie": "vt=private"})
    direct = client(direct_handler) if direct_handler is not None else None
    download_factory = (lambda: client(cdn_handler)) if cdn_handler is not None else None
    adapter = VirusTotalHttpAdapter(
        client=proxy,
        direct_client=direct,
        api_key=API_KEY if direct is not None else None,
        base_url=BASE,
        legacy_base_url=LEGACY_BASE,
        capabilities=VirusTotalCapabilities(
            file_report=True,
            file_download=True,
            file_relationships=True,
            behaviour_pcap=True,
        ),
        routing_policy=VirusTotalRoutingPolicy(
            routes={
                VirusTotalCapability.FILE_DOWNLOAD: _operation_route(
                    VirusTotalCapability.FILE_DOWNLOAD
                ),
                VirusTotalCapability.FILE_REPORT: _operation_route(
                    VirusTotalCapability.FILE_REPORT
                ),
                VirusTotalCapability.FILE_RELATIONSHIPS: _operation_route(
                    VirusTotalCapability.FILE_RELATIONSHIPS
                ),
                VirusTotalCapability.BEHAVIOUR_PCAP: _operation_route(
                    VirusTotalCapability.BEHAVIOUR_PCAP
                ),
            }
        ),
        file_download_enabled=True,
        proxy_query_sentinel_enabled=False,
        behaviour_pcap_enabled=True,
        download_allowed_hosts=download_allowed_hosts,
        download_allowed_ports=frozenset({443}),
        download_max_redirects=download_max_redirects,
        download_client_factory=download_factory,
        **kwargs,
    )
    configure_test_vt_adapter(adapter)
    return adapter, clients


async def _close(clients: list[httpx.AsyncClient]) -> None:
    await asyncio_gather(*(client.aclose() for client in clients))


async def asyncio_gather(*awaitables: Any) -> None:
    import asyncio

    await asyncio.gather(*awaitables)


@pytest.mark.asyncio
async def test_v3_signed_redirect_is_manual_and_cdn_client_has_no_credentials(
    caplog: pytest.LogCaptureFixture,
) -> None:
    cdn_requests: list[httpx.Request] = []

    def upstream(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == f"{BASE}/files/{SHA256}/download"
        assert request.headers["x-apikey"] == API_KEY
        return httpx.Response(302, headers={"Location": SIGNED_URL}, request=request)

    def cdn(request: httpx.Request) -> httpx.Response:
        cdn_requests.append(request)
        assert str(request.url) == SIGNED_URL
        return httpx.Response(
            200,
            content=b"sample-bytes",
            headers={"Content-Type": "application/octet-stream", "Content-Length": "12"},
            request=request,
        )

    adapter, clients = _make_adapter(upstream, cdn_handler=cdn)
    caplog.set_level(logging.INFO, logger="cti_app.infrastructure.virustotal")
    try:
        sink = BytesIO()
        result = await adapter.file_download(SHA256, sink=sink)
    finally:
        await _close(clients)

    assert sink.getvalue() == b"sample-bytes"
    assert result.sha256 == SHA256
    assert len(cdn_requests) == 1
    assert "x-apikey" not in cdn_requests[0].headers
    assert "cookie" not in cdn_requests[0].headers
    assert clients[1]._trust_env is False
    assert "signature=do-not-log-this" not in caplog.text
    assert API_KEY not in caplog.text
    assert "vt_http_exchange" in caplog.text
    assert adapter.acquisition_route_fingerprint
    assert isinstance(adapter.sample_acquisition_cache_scope, SampleAcquisitionCacheScope)
    assert adapter.sample_acquisition_cache_scope.route_fingerprint
    assert "?" not in adapter.sample_acquisition_cache_scope.route_fingerprint


@pytest.mark.asyncio
async def test_sample_acquisition_cache_scope_contains_only_non_secret_values() -> None:
    adapter, clients = _make_adapter(_unexpected_request, direct_handler=_unexpected_request)
    try:
        scope = adapter.sample_acquisition_cache_scope
    finally:
        await _close(clients)

    assert isinstance(scope, SampleAcquisitionCacheScope)
    assert scope.provider == "virustotal"
    assert scope.transport == "proxy-v3"
    assert "?" not in scope.route_fingerprint
    assert API_KEY not in repr(scope)
    assert SIGNED_URL not in repr(scope)
    assert all(
        isinstance(value, str) and value
        for value in (
            scope.provider,
            scope.account_capability_fingerprint,
            scope.route_fingerprint,
            scope.transport,
            scope.configuration_fingerprint,
        )
    )


@pytest.mark.asyncio
async def test_redirect_to_virustotal_host_requires_and_obeys_explicit_allowlist() -> None:
    signed_url = "https://www.virustotal.com/signed.bin?signature=secret"

    def upstream(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == f"{BASE}/files/{SHA256}/download"
        return httpx.Response(302, headers={"Location": signed_url}, request=request)

    def cdn(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == signed_url
        assert "x-apikey" not in request.headers
        assert "cookie" not in request.headers
        return httpx.Response(200, content=b"sample-bytes", request=request)

    adapter, clients = _make_adapter(
        upstream,
        cdn_handler=cdn,
        download_allowed_hosts=frozenset({"www.virustotal.com"}),
    )
    try:
        result = await adapter.file_download(SHA256, sink=BytesIO())
    finally:
        await _close(clients)
    assert result.sha256 == SHA256


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("location", "expected_code"),
    [
        (
            "http://cdn.example.test/object.bin?signature=secret",
            "virustotal_download_host_not_allowed",
        ),
        (
            "https://evil.example.test/object.bin?signature=secret",
            "virustotal_download_host_not_allowed",
        ),
        (
            "https://cdn.example.test:8443/object.bin?signature=secret",
            "virustotal_download_host_not_allowed",
        ),
    ],
)
async def test_each_redirect_target_requires_https_exact_host_and_approved_port(
    location: str,
    expected_code: str,
) -> None:
    def upstream(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == f"{BASE}/files/{SHA256}/download"
        return httpx.Response(307, headers={"Location": location}, request=request)

    adapter, clients = _make_adapter(upstream, cdn_handler=_unexpected_request)
    try:
        with pytest.raises(VirusTotalDownloadHostNotAllowedError) as caught:
            await adapter.file_download(SHA256, sink=BytesIO())
    finally:
        await _close(clients)
    assert caught.value.code == expected_code
    assert caught.value.response_origin is SampleAcquisitionResponseOrigin.UNKNOWN
    assert location not in str(caught.value)
    assert "signature=secret" not in repr(caught.value)


@pytest.mark.asyncio
async def test_download_404_with_proxy_marker_has_proxy_response_origin() -> None:
    def upstream(request: httpx.Request) -> httpx.Response:
        if str(request.url) == f"{BASE}/files/{SHA256}/download":
            return httpx.Response(404, text="not found", request=request)
        assert str(request.url) == f"{BASE}/files/{SHA256}/download_url"
        return httpx.Response(
            404,
            text="route not found",
            headers={"Via": "1.1 vt-proxy", "Content-Type": "text/plain"},
            request=request,
        )

    adapter, clients = _make_adapter(upstream)
    try:
        with pytest.raises(VirusTotalHttpError) as caught:
            await adapter.file_download(SHA256, sink=BytesIO())
    finally:
        await _close(clients)

    assert caught.value.status_code == 404
    assert caught.value.response_origin is SampleAcquisitionResponseOrigin.PROXY


@pytest.mark.asyncio
async def test_download_vt_json_error_has_provider_response_origin() -> None:
    def upstream(request: httpx.Request) -> httpx.Response:
        if str(request.url) == f"{BASE}/files/{SHA256}/download":
            return httpx.Response(404, text="not found", request=request)
        assert str(request.url) == f"{BASE}/files/{SHA256}/download_url"
        return httpx.Response(
            404,
            json={"error": {"code": "NotFoundError", "message": "File not found"}},
            headers={"Content-Type": "application/json"},
            request=request,
        )

    adapter, clients = _make_adapter(upstream)
    try:
        with pytest.raises(VirusTotalHttpError) as caught:
            await adapter.file_download(SHA256, sink=BytesIO())
    finally:
        await _close(clients)

    assert caught.value.response_origin is SampleAcquisitionResponseOrigin.PROVIDER


@pytest.mark.asyncio
async def test_download_ambiguous_404_has_unknown_response_origin() -> None:
    def upstream(request: httpx.Request) -> httpx.Response:
        if str(request.url) == f"{BASE}/files/{SHA256}/download":
            return httpx.Response(404, text="not found", request=request)
        assert str(request.url) == f"{BASE}/files/{SHA256}/download_url"
        return httpx.Response(
            404,
            text="not found",
            headers={"Content-Type": "text/plain"},
            request=request,
        )

    adapter, clients = _make_adapter(upstream)
    try:
        with pytest.raises(VirusTotalHttpError) as caught:
            await adapter.file_download(SHA256, sink=BytesIO())
    finally:
        await _close(clients)

    assert caught.value.response_origin is SampleAcquisitionResponseOrigin.UNKNOWN


@pytest.mark.asyncio
async def test_signed_cdn_error_has_signed_cdn_response_origin() -> None:
    def upstream(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == f"{BASE}/files/{SHA256}/download"
        return httpx.Response(302, headers={"Location": SIGNED_URL}, request=request)

    def cdn(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == SIGNED_URL
        return httpx.Response(403, text="expired", request=request)

    adapter, clients = _make_adapter(upstream, cdn_handler=cdn)
    try:
        with pytest.raises(VirusTotalSignedUrlExpiredError) as caught:
            await adapter.file_download(SHA256, sink=BytesIO())
    finally:
        await _close(clients)

    assert caught.value.response_origin is SampleAcquisitionResponseOrigin.SIGNED_CDN
    assert SIGNED_URL not in repr(caught.value)


@pytest.mark.asyncio
async def test_signed_redirect_limit_is_enforced_before_the_next_hop() -> None:
    first = "https://cdn.example.test/step-1?signature=secret"
    second = "https://cdn.example.test/step-2?signature=secret"
    seen: list[str] = []

    def upstream(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == f"{BASE}/files/{SHA256}/download"
        return httpx.Response(302, headers={"Location": first}, request=request)

    def cdn(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if str(request.url) == first:
            return httpx.Response(307, headers={"Location": second}, request=request)
        pytest.fail(f"unexpected request after redirect limit: {request.url!s}")

    adapter, clients = _make_adapter(upstream, cdn_handler=cdn, download_max_redirects=1)
    try:
        with pytest.raises(VirusTotalError) as caught:
            await adapter.file_download(SHA256, sink=BytesIO())
    finally:
        await _close(clients)
    assert caught.value.code == "virustotal_download_redirect_limit"
    assert second not in repr(caught.value)
    assert seen == [first]


@pytest.mark.asyncio
async def test_signed_cdn_403_is_an_expired_ticket() -> None:
    def upstream(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == f"{BASE}/files/{SHA256}/download"
        return httpx.Response(302, headers={"Location": SIGNED_URL}, request=request)

    def cdn(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == SIGNED_URL
        return httpx.Response(403, text="expired", request=request)

    adapter, clients = _make_adapter(upstream, cdn_handler=cdn)
    try:
        with pytest.raises(VirusTotalError) as caught:
            await adapter.file_download(SHA256, sink=BytesIO())
    finally:
        await _close(clients)
    assert caught.value.code == "virustotal_signed_url_expired"
    assert caught.value.transport == "signed-cdn"


@pytest.mark.asyncio
async def test_pcap_redirect_is_archived_and_not_followed_outside_coordinator() -> None:
    upstream_requests: list[httpx.Request] = []
    signed_url = "https://cdn.example.test/pcap?signature=hidden"

    def upstream(request: httpx.Request) -> httpx.Response:
        upstream_requests.append(request)
        assert str(request.url) == f"{BASE}/file_behaviours/sandbox-1/pcap"
        return httpx.Response(
            302,
            headers={"Location": signed_url},
            request=request,
        )

    def cdn(request: httpx.Request) -> httpx.Response:
        pytest.fail(f"uncoordinated signed redirect was followed: {request.url!s}")

    adapter, clients = _make_adapter(upstream, cdn_handler=cdn)
    try:
        with pytest.raises(VirusTotalUnexpectedRedirectError):
            await adapter.behaviour_pcap("sandbox-1", sink=BytesIO())
    finally:
        await _close(clients)
    assert len(upstream_requests) == 1
    assert len(adapter._test_external_effect_store.claims) == 1
    assert all(
        signed_url not in repr(claim)
        for claim in adapter._test_external_effect_store.claims.values()
    )


@pytest.mark.asyncio
async def test_download_url_ticket_uses_same_headerless_signed_transport() -> None:
    seen: list[str] = []

    def upstream(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if str(request.url) == f"{BASE}/files/{SHA256}/download":
            return httpx.Response(404, request=request)
        assert str(request.url) == f"{BASE}/files/{SHA256}/download_url"
        return httpx.Response(200, json={"data": SIGNED_URL}, request=request)

    def cdn(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == SIGNED_URL
        assert "x-apikey" not in request.headers
        assert "cookie" not in request.headers
        return httpx.Response(200, content=b"sample-bytes", request=request)

    adapter, clients = _make_adapter(upstream, cdn_handler=cdn)
    try:
        result = await adapter.file_download(SHA256, sink=BytesIO())
    finally:
        await _close(clients)
    assert seen == [
        f"{BASE}/files/{SHA256}/download",
        f"{BASE}/files/{SHA256}/download_url",
    ]
    assert result.sha256 == SHA256


@pytest.mark.asyncio
async def test_unattributed_404_is_diagnostic_and_never_claims_file_absent() -> None:
    seen: list[str] = []

    def upstream(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        assert str(request.url) in {
            f"{BASE}/files/{SHA256}/download",
            f"{BASE}/files/{SHA256}/download_url",
        }
        return httpx.Response(
            404,
            headers={"Content-Type": "text/html"},
            text="not found",
            request=request,
        )

    adapter, clients = _make_adapter(upstream)
    try:
        with pytest.raises(VirusTotalHttpError) as caught:
            await adapter.file_download(SHA256, sink=BytesIO())
    finally:
        await _close(clients)

    error = caught.value
    assert error.transport == "proxy-v3"
    assert error.route == "proxy:v3"
    assert error.route_path == "/files/{hash}/download_url"
    assert error.status_code == 404
    assert error.content_type_class == "html"
    assert error.issuer == "unknown"
    assert error.bytes_read == len(b"not found")
    assert error.correlation_id
    assert error.fingerprint == adapter.acquisition_route_fingerprint
    assert error.code == "virustotal_http_404_unattributed"
    assert seen == [
        f"{BASE}/files/{SHA256}/download",
        f"{BASE}/files/{SHA256}/download_url",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "headers", "body", "error_type"),
    [
        (204, {}, b"", "virustotal_download_empty"),
        (
            200,
            {"Content-Type": "text/html"},
            b"<html>nope</html>",
            "virustotal_download_not_binary",
        ),
        (
            200,
            {"Content-Type": "application/json"},
            b'{"data":{}}',
            "virustotal_download_not_binary",
        ),
        (200, {"Content-Length": "20"}, b"sample-bytes", "virustotal_download_truncated"),
        (200, {"Content-Length": "11"}, b"sample-bytes", "virustotal_download_length_mismatch"),
    ],
)
async def test_download_rejects_empty_html_json_and_misleading_lengths(
    status: int,
    headers: dict[str, str],
    body: bytes,
    error_type: str,
) -> None:
    def upstream(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == f"{BASE}/files/{SHA256}/download"
        return httpx.Response(status, headers=headers, content=body, request=request)

    adapter, clients = _make_adapter(upstream)
    try:
        with pytest.raises(VirusTotalError) as caught:
            await adapter.file_download(SHA256, sink=BytesIO())
    finally:
        await _close(clients)
    assert caught.value.code == error_type


@pytest.mark.asyncio
async def test_download_limit_uses_streamed_bytes_and_hash_mismatch_is_typed() -> None:
    data = b"sample-bytes"

    def upstream(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == f"{BASE}/files/{SHA256}/download"
        return httpx.Response(200, content=data, headers={"Content-Length": "1"}, request=request)

    adapter, clients = _make_adapter(upstream, download_max_bytes=8)
    try:
        with pytest.raises(VirusTotalDownloadTooLargeError) as caught:
            await adapter.file_download(SHA256, sink=BytesIO())
    finally:
        await _close(clients)
    assert caught.value.code == "virustotal_download_too_large"

    adapter, clients = _make_adapter(
        lambda request: (
            httpx.Response(200, content=b"different", request=request)
            if str(request.url) == f"{BASE}/files/{SHA256}/download"
            else pytest.fail(f"unexpected request: {request.url!s}")
        )
    )
    try:
        with pytest.raises(VirusTotalDownloadHashMismatchError) as caught:
            await adapter.file_download(SHA256, sink=BytesIO())
    finally:
        await _close(clients)
    assert caught.value.code == "virustotal_download_hash_mismatch"


@pytest.mark.asyncio
async def test_stream_read_error_keeps_actual_bytes_and_is_typed_as_truncation() -> None:
    class BrokenStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"partial"
            raise httpx.ReadError("simulated truncated stream")

        async def aclose(self) -> None:
            return None

    def upstream(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == f"{BASE}/files/{SHA256}/download"
        return httpx.Response(
            200,
            headers={"Content-Length": "20"},
            stream=BrokenStream(),
            request=request,
        )

    adapter, clients = _make_adapter(upstream)
    try:
        with pytest.raises(VirusTotalError) as caught:
            await adapter.file_download(SHA256, sink=BytesIO())
    finally:
        await _close(clients)
    assert caught.value.code == "virustotal_download_truncated"
    assert caught.value.bytes_read == len(b"partial")


@pytest.mark.asyncio
async def test_opt_in_v2_fallback_requires_two_identified_route_missing_404s() -> None:
    requests: list[httpx.Request] = []

    def proxy(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert str(request.url) in {
            f"{BASE}/files/{SHA256}/download",
            f"{BASE}/files/{SHA256}/download_url",
        }
        return httpx.Response(
            404,
            headers={"Via": "1.1 configured-proxy", "Content-Type": "application/json"},
            json={"message": "route not found"},
            request=request,
        )

    def direct(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert str(request.url.copy_with(query=None)) == f"{LEGACY_BASE}/file/download"
        assert request.url.params["apikey"] == API_KEY
        assert request.url.params["hash"] == SHA256
        assert "x" not in request.url.params
        return httpx.Response(200, content=b"sample-bytes", request=request)

    adapter, clients = _make_adapter(
        proxy,
        direct_handler=direct,
        file_download_legacy_fallback_enabled=True,
    )
    try:
        result = await adapter.file_download(SHA256, sink=BytesIO())
    finally:
        await _close(clients)
    assert [request.url.path for request in requests] == [
        f"/api/v3/files/{SHA256}/download",
        f"/api/v3/files/{SHA256}/download_url",
        "/vtapi/v2/file/download",
    ]
    assert result.sha256 == SHA256


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403, 429])
async def test_v3_policy_or_quota_errors_never_trigger_v2_fallback(status: int) -> None:
    def proxy(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == f"{BASE}/files/{SHA256}/download"
        return httpx.Response(status, json={"error": {"code": "RequestDenied"}}, request=request)

    def unexpected_direct(request: httpx.Request) -> httpx.Response:
        pytest.fail(f"v2 fallback must not run after v3 status {status}: {request.url!s}")

    adapter, clients = _make_adapter(
        proxy,
        direct_handler=unexpected_direct,
        file_download_legacy_fallback_enabled=True,
    )
    try:
        with pytest.raises(VirusTotalHttpError) as caught:
            await adapter.file_download(SHA256, sink=BytesIO())
    finally:
        await _close(clients)
    assert caught.value.status_code == status


@pytest.mark.asyncio
async def test_v2_secret_is_removed_from_exception_context_repr_and_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def proxy(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            404,
            headers={"Via": "1.1 configured-proxy"},
            json={"message": "route not found"},
            request=request,
        )

    def direct(request: httpx.Request) -> httpx.Response:
        assert request.url.params["apikey"] == API_KEY
        return httpx.Response(403, text=f"denied {API_KEY}", request=request)

    adapter, clients = _make_adapter(
        proxy,
        direct_handler=direct,
        file_download_legacy_fallback_enabled=True,
    )
    caplog.set_level(logging.INFO, logger="cti_app.infrastructure.virustotal")
    try:
        with pytest.raises(VirusTotalHttpError) as caught:
            await adapter.file_download(SHA256, sink=BytesIO())
    finally:
        await _close(clients)
    error = caught.value
    rendered = "".join(traceback.format_exception(error))
    assert error.transport == "direct-v2"
    assert API_KEY not in str(error)
    assert API_KEY not in repr(error)
    assert API_KEY not in rendered
    assert API_KEY not in caplog.text
    assert re.search(r"apikey=(?!\[REDACTED\])", caplog.text) is None


@pytest.mark.asyncio
async def test_behaviour_pcap_is_bounded_hashed_and_magic_checked() -> None:
    valid_pcap = b"\xd4\xc3\xb2\xa1" + b"pcap-payload"
    requests: list[httpx.Request] = []

    def pcap(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert str(request.url) == f"{BASE}/file_behaviours/sandbox-1/pcap"
        return httpx.Response(200, content=valid_pcap, request=request)

    adapter, clients = _make_adapter(pcap, download_max_bytes=1024)
    try:
        sink = BytesIO()
        result = await adapter.behaviour_pcap("sandbox-1", sink=sink)
        replay_sink = BytesIO()
        replay = await adapter.behaviour_pcap("sandbox-1", sink=replay_sink)
    finally:
        await _close(clients)
    assert sink.getvalue() == valid_pcap
    assert replay_sink.getvalue() == valid_pcap
    assert result.sha256 == hashlib.sha256(valid_pcap).hexdigest()
    assert result.size == len(valid_pcap)
    assert replay == result
    assert len(requests) == 1
    assert len(adapter._test_external_effect_store.claims) == 1
    assert adapter._test_external_effect_store.reuse_count == 1
    claim = next(iter(adapter._test_external_effect_store.claims.values()))
    archived = adapter._test_vt_response_archive.items[claim.result_ref]
    assert archived.reference.sha256 == hashlib.sha256(valid_pcap).hexdigest()

    oversized, oversized_clients = _make_adapter(
        lambda request: httpx.Response(200, content=valid_pcap, request=request)
    )
    try:
        oversized_sink = BytesIO()
        with pytest.raises(VirusTotalDownloadTooLargeError):
            await oversized.behaviour_pcap("sandbox-1", sink=oversized_sink, max_bytes=4)
        assert oversized_sink.getvalue() == b""
        assert len(oversized._test_external_effect_store.claims) == 1
    finally:
        await _close(oversized_clients)

    adapter, clients = _make_adapter(
        lambda request: (
            httpx.Response(200, content=b"not-pcap", request=request)
            if str(request.url) == f"{BASE}/file_behaviours/sandbox-1/pcap"
            else pytest.fail(f"unexpected request: {request.url!s}")
        )
    )
    try:
        with pytest.raises(VirusTotalPayloadError) as caught:
            await adapter.behaviour_pcap("sandbox-1", sink=BytesIO())
    finally:
        await _close(clients)
    assert caught.value.code == "virustotal_pcap_magic_invalid"


@pytest.mark.asyncio
async def test_behaviour_pcap_without_committed_authority_sends_nothing() -> None:
    requests: list[httpx.Request] = []

    def pcap(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, content=b"\xd4\xc3\xb2\xa1", request=request)

    adapter, clients = _make_adapter(pcap)
    try:
        with virustotal_external_effect_scope(None):
            with pytest.raises(VirusTotalExternalEffectRefusedError):
                await adapter.behaviour_pcap("sandbox-1", sink=BytesIO())
    finally:
        await _close(clients)
    assert requests == []


@pytest.mark.asyncio
async def test_object_report_url_id_retry_and_schema_validation() -> None:
    url_value = "https://example.test/a?b=c"
    url_id = base64.urlsafe_b64encode(url_value.encode()).decode().rstrip("=")
    calls: list[httpx.Request] = []

    def upstream(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert request.url.path == f"/api/v3/urls/{url_id}"
        if request.url.params:
            assert str(request.url) == (
                f"{BASE}/urls/{url_id}?relationships=comments%2Ccommunicating_files%2C"
                "downloaded_files%2Cgraphs%2Crelated_comments%2Crelated_references%2C"
                "related_threat_actors"
            )
            return httpx.Response(403, json={"error": {"code": "Forbidden"}}, request=request)
        assert str(request.url) == f"{BASE}/urls/{url_id}"
        return httpx.Response(
            200,
            json={"data": {"type": "url", "id": url_id, "attributes": {"url": url_value}}},
            request=request,
        )

    adapter, clients = _make_adapter(upstream)
    try:
        report = await adapter.object_report("urls", url_value)
    finally:
        await _close(clients)
    assert report.object_id == url_id
    assert report.kind == "urls"
    assert len(calls) == 2
    assert calls[-1].url.params.multi_items() == []
    claims = tuple(cast(Any, adapter)._test_external_effect_store.claims.values())
    assert len(claims) == 2
    assert {claim.family for claim in claims} == {"vt.object_report"}

    adapter, clients = _make_adapter(
        lambda request: (
            httpx.Response(200, json={"data": []}, request=request)
            if str(request.url)
            == (
                f"{BASE}/domains/example.test?relationships=caa_records%2Ccname_records%2C"
                "comments%2Ccommunicating_files%2Cdownloaded_files%2Cgraphs%2C"
                "historical_ssl_certificates%2Chistorical_whois%2Cimmediate_parent%2C"
                "mx_records%2Cns_records%2Cparent%2Creferrer_files%2Crelated_comments%2C"
                "related_references%2Crelated_threat_actors%2Cresolutions%2Csoa_records%2C"
                "siblings%2Csubdomains%2Curls%2Cuser_votes"
            )
            else pytest.fail(f"unexpected request: {request.url!s}")
        )
    )
    try:
        with pytest.raises(VirusTotalPayloadError):
            await adapter.object_report("domains", "example.test")
    finally:
        await _close(clients)
