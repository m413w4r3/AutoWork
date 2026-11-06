from __future__ import annotations

import asyncio
import base64
import hashlib
import inspect
import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from cti_app.application.browser_acquisition import (
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
        return response


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
async def test_credential_like_query_values_are_removed_from_returned_urls() -> None:
    body = b'{"items":[1]}'

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(
                200,
                json={"status": "ready", "browser": "chromium", "navigation": True},
            )
        return httpx.Response(
            200,
            json=_observation(captured_json=[_captured_json(body)]),
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
    assert observation.final_url == "https://example.com/article?section=public"
    assert observation.captured_json[0].url == "https://example.com/data?kind=public"
    assert "secret" not in observation.final_url
    assert "secret" not in observation.captured_json[0].url
    assert observation.captured_json[0].body == body
    assert observation.captured_json[0].sha256 == hashlib.sha256(body).hexdigest()


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

    async def fake_renderer(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
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
