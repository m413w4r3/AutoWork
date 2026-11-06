from __future__ import annotations

import asyncio
import base64
import hashlib
import inspect
import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import httpx
import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT))

from sidecars.playwright_acquirer.server import (  # noqa: E402
    MAX_CAPTURE_JSON_BODY_BYTES,
    _capture_json_responses,
    _route_http_only,
)
from sidecars.playwright_acquirer.server import (  # noqa: E402
    _credential_free_http_url as _renderer_credential_free_http_url,
)
from sidecars.playwright_acquirer.server import (  # noqa: E402
    _credential_free_url as _renderer_credential_free_url,
)

from cti_app.application.browser_acquisition import (  # noqa: E402
    BrowserControlReason,
    BrowserInvalidResponseError,
    BrowserSizeLimitError,
    BrowserTimeoutError,
    BrowserUnavailableError,
    BrowserUnconfiguredError,
    SidecarBrowserAdapter,
)


def _captured_json(
    body: bytes,
    url: str = "https://example.com/data?token=secret&kind=public",
) -> dict:
    return {
        "url": url,
        "content_type": "application/json; charset=utf-8",
        "status": 200,
        "body_base64": base64.b64encode(body).decode("ascii"),
        "sha256": hashlib.sha256(body).hexdigest(),
        "size": len(body),
    }


def _observation(**overrides) -> dict:
    value = {
        "rendered_html": "<html><body>rendered</body></html>",
        "final_url": "https://example.com/article?access_token=secret&section=public",
        "status": 200,
        "captured_json": [],
        "challenge_detected": False,
        "paywall_detected": False,
        "engine": "chromium",
        "duration_ms": 27,
    }
    value.update(overrides)
    return value


class FakeAsyncTransport(httpx.AsyncBaseTransport):
    def __init__(self, handler: Callable[[httpx.Request], Any]) -> None:
        self.handler = handler

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        response = self.handler(request)
        if inspect.isawaitable(response):
            response = await response
        response.request = request
        return cast(httpx.Response, response)


def _transport_for(handler) -> FakeAsyncTransport:
    return FakeAsyncTransport(handler)


@pytest.mark.asyncio
async def test_unconfigured_adapter_fails_before_network() -> None:
    adapter = SidecarBrowserAdapter(timeout_seconds=1)
    with pytest.raises(BrowserUnconfiguredError) as error:
        await adapter.navigate("https://example.com/article")
    assert error.value.reason_code is BrowserControlReason.UNCONFIGURED


@pytest.mark.asyncio
async def test_health_navigation_false_fails_closed() -> None:
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        return httpx.Response(
            200,
            json={
                "status": "ready",
                "browser": "chromium",
                "navigation": False,
                "policy_version": "browser-acquisition-v1",
            },
        )

    adapter = SidecarBrowserAdapter(
        service_url="http://fake-renderer",
        timeout_seconds=1,
        transport=_transport_for(handler),
    )
    try:
        with pytest.raises(BrowserUnconfiguredError):
            await adapter.navigate("https://example.com/article")
    finally:
        await adapter.aclose()
    assert requests == ["/health"]


@pytest.mark.asyncio
@pytest.mark.parametrize("malformed", [True, False])
async def test_malformed_or_oversized_captured_json_is_rejected(malformed: bool) -> None:
    if malformed:
        entry = {
            "url": "https://example.com/data",
            "content_type": "application/json",
            "body_base64": "%%%not-base64%%%",
            "sha256": "0" * 64,
            "size": 1,
        }
        expected_error = BrowserInvalidResponseError
    else:
        body = b"x" * (512 * 1024 + 1)
        entry = _captured_json(body)
        expected_error = BrowserSizeLimitError

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(
                200,
                json={"status": "ready", "browser": "chromium", "navigation": True},
            )
        return httpx.Response(200, json=_observation(captured_json=[entry]))

    adapter = SidecarBrowserAdapter(
        service_url="http://fake-renderer",
        timeout_seconds=2,
        transport=_transport_for(handler),
    )
    try:
        with pytest.raises(expected_error):
            await adapter.navigate("https://example.com/article")
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
async def test_non_json_captured_body_is_rejected() -> None:
    entry = _captured_json(b"not actually JSON")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(
                200,
                json={"status": "ready", "browser": "chromium", "navigation": True},
            )
        return httpx.Response(200, json=_observation(captured_json=[entry]))

    adapter = SidecarBrowserAdapter(
        service_url="http://fake-renderer",
        timeout_seconds=2,
        transport=_transport_for(handler),
    )
    try:
        with pytest.raises(BrowserInvalidResponseError):
            await adapter.navigate("https://example.com/article")
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
async def test_captured_json_total_body_budget_is_enforced_across_page() -> None:
    body = b'{"v":"' + b"a" * (450 * 1024 - 9) + b'"}'
    entries = [_captured_json(body, url=f"https://example.com/data/{index}") for index in range(5)]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(
                200,
                json={"status": "ready", "browser": "chromium", "navigation": True},
            )
        return httpx.Response(200, json=_observation(captured_json=entries))

    adapter = SidecarBrowserAdapter(
        service_url="http://fake-renderer",
        timeout_seconds=2,
        transport=_transport_for(handler),
    )
    try:
        with pytest.raises(BrowserSizeLimitError):
            await adapter.navigate("https://example.com/article")
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
async def test_credential_like_query_values_are_removed_from_returned_urls() -> None:
    body = b'{"items":[1]}'
    token_path_segment = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.signature"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(
                200,
                json={"status": "ready", "browser": "chromium", "navigation": True},
            )
        return httpx.Response(
            200,
            json=_observation(
                final_url=(
                    "https://example.com/article/"
                    f"{token_path_segment}?jwt=jwt-secret&code=code-secret"
                    "&access_token=access-secret&edition=monthly"
                ),
                captured_json=[
                    _captured_json(
                        body,
                        url=(
                            "https://example.com/data/"
                            f"{token_path_segment}?jwt=jwt-secret&code=code-secret"
                            "&access_token=access-secret&kind=public"
                        ),
                    )
                ],
            ),
        )

    adapter = SidecarBrowserAdapter(
        service_url="http://fake-renderer",
        timeout_seconds=2,
        transport=_transport_for(handler),
    )
    try:
        observation = await adapter.navigate(
            "https://example.com/article?token=request-secret&edition=monthly"
        )
    finally:
        await adapter.aclose()
    assert observation.final_url == "https://example.com/article/%5BREDACTED%5D"
    assert observation.captured_json[0].url == "https://example.com/data/%5BREDACTED%5D"
    assert all(
        secret not in observation.final_url
        for secret in ("jwt-secret", "code-secret", "access-secret", token_path_segment)
    )
    assert all(
        secret not in observation.captured_json[0].url
        for secret in ("jwt-secret", "code-secret", "access-secret", token_path_segment)
    )
    assert "?" not in observation.final_url
    assert "?" not in observation.captured_json[0].url
    assert observation.captured_json[0].body == body
    assert observation.captured_json[0].sha256 == hashlib.sha256(body).hexdigest()
    assert observation.captured_json[0].status == 200
    assert observation.captured_json[0].headers == {"content-type": "application/json"}


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/data?api_key=secret",
        "https://example.com/data?%61ccess_token=secret",
        "https://example.com/api_key/secret",
        "https://example.com/data?next=https%3A%2F%2Fother.example%2F%3Ftoken%3Dsecret",
        "https://user:pass@example.com/data",
    ],
)
def test_renderer_credential_url_gate_rejects_sensitive_query_and_path_keys(url: str) -> None:
    assert _renderer_credential_free_http_url(url) is False


def test_renderer_credential_url_gate_allows_public_url() -> None:
    assert _renderer_credential_free_http_url("https://example.com/article?section=public")


@pytest.mark.parametrize(
    "key",
    [
        "token",
        "secret",
        "signature",
        "sig",
        "auth",
        "apikey",
        "api_key",
        "password",
        "credential",
        "jwt",
        "code",
        "id_token",
        "access_token",
        "refresh_token",
        "session",
        "key",
    ],
)
def test_renderer_credential_url_gate_rejects_each_sensitive_query_key(key: str) -> None:
    assert _renderer_credential_free_http_url(f"https://example.com/data?{key}=secret") is False


def test_browser_route_aborts_credential_bearing_subrequest() -> None:
    class Route:
        request = type(
            "Request",
            (),
            {"url": "https://cdn.example.com/data?access_token=private-value"},
        )()

        def __init__(self) -> None:
            self.aborted = False
            self.continued = False

        def abort(self) -> None:
            self.aborted = True

        def continue_(self) -> None:
            self.continued = True

    route = Route()
    _route_http_only(route)

    assert route.aborted is True
    assert route.continued is False


def test_renderer_url_sanitization_removes_queries_and_token_path_segments() -> None:
    token_path_segment = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.signature"
    sanitized = _renderer_credential_free_url(
        "https://example.com/article/"
        f"{token_path_segment}?jwt=jwt-secret&code=code-secret"
        "&access_token=access-secret&edition=monthly"
    )
    assert sanitized == "https://example.com/article/%5BREDACTED%5D"
    assert all(
        secret not in sanitized
        for secret in ("jwt-secret", "code-secret", "access-secret", token_path_segment)
    )


def test_renderer_skips_response_with_oversized_content_length_before_body() -> None:
    class FakeRequest:
        resource_type = "xhr"

    class FakeResponse:
        def __init__(self) -> None:
            self.url = "https://example.com/data"
            self.status = 200
            self.headers = {
                "content-type": "application/json",
                "content-length": str(MAX_CAPTURE_JSON_BODY_BYTES + 1),
            }
            self.request = FakeRequest()
            self.body_called = False

        def body(self) -> bytes:
            self.body_called = True
            raise AssertionError("oversized response body must not be materialized")

    response = FakeResponse()
    assert _capture_json_responses([response]) == []
    assert response.body_called is False


def test_renderer_prechecks_captured_json_total_before_materializing_body(monkeypatch) -> None:
    class FakeRequest:
        resource_type = "xhr"

    class FakeResponse:
        def __init__(self, index: int, body: bytes) -> None:
            self.url = f"https://example.com/data/{index}"
            self.status = 200
            self.headers = {
                "content-type": "application/json",
                "content-length": str(len(body)),
            }
            self.request = FakeRequest()
            self._body = body
            self.body_called = False

        def body(self) -> bytes:
            self.body_called = True
            return self._body

    def padded_json(size: int) -> bytes:
        prefix, suffix = b'{"v":"', b'"}'
        return prefix + b"a" * (size - len(prefix) - len(suffix)) + suffix

    monkeypatch.setattr(
        "sidecars.playwright_acquirer.server.MAX_CAPTURE_JSON_TOTAL_BYTES", 1024 * 1024
    )
    bodies = [padded_json(300 * 1024) for _ in range(3)]
    bodies.extend([padded_json(200 * 1024), padded_json(50 * 1024)])
    responses = [FakeResponse(index, body) for index, body in enumerate(bodies)]

    captured = _capture_json_responses(responses)

    assert len(captured) == 4
    assert all(response.body_called for response in responses[:3])
    assert responses[3].body_called is False
    assert responses[4].body_called is True


@pytest.mark.asyncio
async def test_timeout_is_mapped_and_exception_does_not_include_body_or_url() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            await asyncio.sleep(0.5)
            return httpx.Response(
                200,
                json={"status": "ready", "browser": "chromium", "navigation": True},
            )
        return httpx.Response(200, json={})

    adapter = SidecarBrowserAdapter(
        service_url="http://fake-renderer",
        timeout_seconds=0.2,
        transport=_transport_for(handler),
    )
    try:
        with pytest.raises(BrowserTimeoutError) as error:
            await adapter.navigate("https://example.com/article?token=private-token")
    finally:
        await adapter.aclose()
    assert error.value.reason_code is BrowserControlReason.TIMEOUT
    assert "private-token" not in str(error.value)
    assert "body" not in str(error.value).casefold()


@pytest.mark.asyncio
async def test_renderer_error_body_is_never_in_exception_message() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(
                200,
                json={"status": "ready", "browser": "chromium", "navigation": True},
            )
        return httpx.Response(
            503,
            content=b'{"error":"renderer down","secret":"provider-token-value"}',
        )

    adapter = SidecarBrowserAdapter(
        service_url="http://fake-renderer",
        timeout_seconds=2,
        transport=_transport_for(handler),
    )
    try:
        with pytest.raises(BrowserUnavailableError) as error:
            await adapter.navigate("https://example.com/article")
    finally:
        await adapter.aclose()
    assert "provider-token-value" not in str(error.value)
    assert "renderer down" not in str(error.value)


@pytest.mark.asyncio
async def test_unix_socket_control_uses_local_fake_server(tmp_path) -> None:
    socket_path = str(tmp_path / "renderer.sock")
    received_paths: list[str] = []
    render_payloads: list[dict] = []

    async def fake_renderer(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        request_line = await reader.readline()
        path = request_line.decode("ascii").split(" ")[1]
        received_paths.append(path)
        headers: dict[str, str] = {}
        while (line := await reader.readline()) != b"\r\n":
            name, value = line.decode("latin-1").split(":", 1)
            headers[name.casefold()] = value.strip()
        body = await reader.readexactly(int(headers.get("content-length", "0")))
        if path == "/health":
            payload = {
                "status": "ready",
                "browser": "chromium",
                "navigation": True,
            }
        else:
            render_payloads.append(json.loads(body))
            payload = _observation()
        encoded = json.dumps(payload).encode("utf-8")
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
            + f"Content-Length: {len(encoded)}\r\nConnection: close\r\n\r\n".encode("ascii")
            + encoded
        )
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_unix_server(fake_renderer, path=socket_path)
    adapter = SidecarBrowserAdapter(socket_path=socket_path, timeout_seconds=2)
    try:
        observation = await adapter.navigate("https://example.com/article")
    finally:
        await adapter.aclose()
        server.close()
        await server.wait_closed()
    assert observation.engine == "chromium"
    assert received_paths == ["/health", "/render"]
    assert render_payloads[0]["capture_json"] is True


@pytest.mark.asyncio
async def test_offline_render_does_not_require_navigation_health() -> None:
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        return httpx.Response(200, json=_observation(status=None, captured_json=[]))

    adapter = SidecarBrowserAdapter(
        service_url="http://fake-renderer",
        timeout_seconds=1,
        transport=_transport_for(handler),
    )
    try:
        observation = await adapter.render_offline(
            "<html><body>offline</body></html>", "https://example.com/offline"
        )
    finally:
        await adapter.aclose()
    assert observation.status is None
    assert requests == ["/render"]
