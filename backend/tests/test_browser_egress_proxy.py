from __future__ import annotations

import asyncio
import base64
import sys
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT))

from sidecars.browser_egress_proxy.server import (  # noqa: E402
    AsyncForwardProxy,
    ProxyConfig,
    ProxyError,
    _parse_authority,
)


async def _start_proxy(
    *,
    config: ProxyConfig | None = None,
    resolver=None,
    connector=None,
) -> tuple[AsyncForwardProxy, int]:
    proxy = AsyncForwardProxy(
        config or ProxyConfig(listen_host="127.0.0.1", listen_port=0),
        resolver=resolver,
        connector=connector,
    )
    return proxy, await proxy.start()


async def _request(port: int, request: bytes, *, read_timeout: float = 2.0) -> bytes:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(request)
    await writer.drain()
    response = await asyncio.wait_for(reader.read(), read_timeout)
    writer.close()
    await writer.wait_closed()
    return response


def _request_bytes(target: str, *, proxy_authorization: str | None = None) -> bytes:
    headers = [f"GET {target} HTTP/1.1", "Host: example.com"]
    if proxy_authorization:
        headers.append(f"Proxy-Authorization: {proxy_authorization}")
    return ("\r\n".join(headers) + "\r\n\r\n").encode("ascii")


@pytest.mark.asyncio
async def test_destination_validation_rejects_private_and_mixed_dns_answers() -> None:
    proxy = AsyncForwardProxy(
        ProxyConfig(listen_host="127.0.0.1", listen_port=0),
        resolver=lambda _url: ["93.184.216.34", "10.0.0.4"],
    )
    with pytest.raises(ProxyError, match="non-public"):
        await proxy._resolve_and_validate("https://example.com/", "example.com", 443)

    private = AsyncForwardProxy(
        ProxyConfig(listen_host="127.0.0.1", listen_port=0),
        resolver=lambda _url: ["192.168.1.1"],
    )
    with pytest.raises(ProxyError, match="non-public"):
        await private._resolve_and_validate("http://example.com/", "example.com", 80)


@pytest.mark.asyncio
async def test_destination_validation_accepts_global_ipv4_and_ipv6() -> None:
    proxy = AsyncForwardProxy(
        ProxyConfig(listen_host="127.0.0.1", listen_port=0),
        resolver=lambda _url: ["93.184.216.34"],
    )
    assert await proxy._resolve_and_validate("https://example.com/", "example.com", 443) == (
        "93.184.216.34"
    )
    ipv6 = AsyncForwardProxy(
        ProxyConfig(listen_host="127.0.0.1", listen_port=0),
        resolver=lambda _url: ["2606:4700:4700::1111"],
    )
    assert await ipv6._resolve_and_validate("https://example.com/", "example.com", 443) == (
        "2606:4700:4700::1111"
    )


@pytest.mark.asyncio
async def test_proxy_never_connects_to_its_listener_port() -> None:
    proxy = AsyncForwardProxy(
        ProxyConfig(
            listen_host="127.0.0.1",
            listen_port=443,
            allowed_ports=frozenset({80, 443}),
        ),
        resolver=lambda _url: ["93.184.216.34"],
    )
    with pytest.raises(ProxyError, match="listener"):
        await proxy._resolve_and_validate("https://example.com/", "example.com", 443)


def test_domain_allow_block_and_docker_hostname_policy() -> None:
    proxy = AsyncForwardProxy(
        ProxyConfig(
            allowed_domains=frozenset({"example.com"}),
            blocked_domains=frozenset({"private.example.com"}),
        )
    )
    proxy._check_domain("example.com")
    proxy._check_domain("www.example.com")
    for hostname in (
        "example.net",
        "private.example.com",
        "x.private.example.com",
        "postgres",
        "metadata.google.internal",
        "notexample.com",
    ):
        with pytest.raises(ProxyError):
            proxy._check_domain(hostname)


def test_ports_userinfo_and_schemes_are_rejected() -> None:
    with pytest.raises(ProxyError, match="port"):
        _parse_authority("example.com:8443", frozenset({80, 443}))
    assert _parse_authority("example.com:8080", frozenset({8080})) == ("example.com", 8080)


@pytest.mark.asyncio
async def test_rejected_http_targets_never_connect_upstream() -> None:
    connections = 0

    async def connector(_ip: str, _port: int):
        nonlocal connections
        connections += 1
        raise AssertionError("rejected targets must not connect upstream")

    proxy, port = await _start_proxy(
        resolver=lambda _url: ["93.184.216.34"], connector=connector
    )
    try:
        responses = [
            await _request(port, _request_bytes("/origin-form")),
            await _request(port, _request_bytes("ftp://example.com/file")),
            await _request(port, _request_bytes("https://example.com/file")),
            await _request(port, _request_bytes("http://user:pass@example.com/")),
            await _request(port, _request_bytes("http://example.com:444/")),
        ]
    finally:
        await proxy.close()
    assert [response.split(b" ", 2)[1] for response in responses] == [
        b"400",
        b"400",
        b"400",
        b"400",
        b"403",
    ]
    assert connections == 0


@pytest.mark.asyncio
async def test_nonpublic_dns_answer_is_rejected_before_upstream_connect() -> None:
    connections = 0

    async def connector(_ip: str, _port: int):
        nonlocal connections
        connections += 1
        raise AssertionError("non-public DNS answers must not connect upstream")

    proxy, port = await _start_proxy(
        resolver=lambda _url: ["93.184.216.34", "169.254.169.254"],
        connector=connector,
    )
    try:
        response = await _request(port, _request_bytes("http://example.com/"))
    finally:
        await proxy.close()
    assert response.startswith(b"HTTP/1.1 403")
    assert connections == 0


@pytest.mark.asyncio
async def test_proxy_health_is_origin_form_and_reports_policy_version() -> None:
    proxy, port = await _start_proxy()
    try:
        response = await _request(
            port,
            b"GET /health HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n",
        )
    finally:
        await proxy.close()
    assert response.startswith(b"HTTP/1.1 200 OK")
    assert b'"policy_version":"browser-egress-v1"' in response


@pytest.mark.asyncio
async def test_connect_tunnels_to_injected_fake_upstream() -> None:
    async def echo(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        content = await reader.read(128)
        writer.write(content)
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    upstream = await asyncio.start_server(echo, "127.0.0.1", 0)
    upstream_port = int(upstream.sockets[0].getsockname()[1])
    connected: list[tuple[str, int]] = []

    async def connector(ip: str, port: int):
        connected.append((ip, port))
        return await asyncio.open_connection("127.0.0.1", upstream_port)

    proxy, port = await _start_proxy(
        resolver=lambda _url: ["93.184.216.34"], connector=connector
    )
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"CONNECT example.com:443 HTTP/1.1\r\nHost: example.com:443\r\n\r\n")
        await writer.drain()
        assert b"200 Connection Established" in await asyncio.wait_for(
            reader.readuntil(b"\r\n\r\n"), 1
        )
        writer.write(b"offline tunnel payload")
        await writer.drain()
        assert await asyncio.wait_for(reader.readexactly(22), 1) == b"offline tunnel payload"
        writer.close()
        await writer.wait_closed()
    finally:
        await proxy.close()
        upstream.close()
        await upstream.wait_closed()
    assert connected == [("93.184.216.34", 443)]


@pytest.mark.asyncio
async def test_absolute_http_request_is_forwarded_without_proxy_credentials() -> None:
    received: list[bytes] = []

    async def upstream_handler(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        received.append(await reader.readuntil(b"\r\n\r\n"))
        body = b"offline upstream"
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Length: 16\r\nConnection: close\r\n\r\n" + body
        )
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    upstream = await asyncio.start_server(upstream_handler, "127.0.0.1", 0)
    upstream_port = int(upstream.sockets[0].getsockname()[1])

    async def connector(_ip: str, _port: int):
        return await asyncio.open_connection("127.0.0.1", upstream_port)

    proxy, port = await _start_proxy(
        resolver=lambda _url: ["93.184.216.34"], connector=connector
    )
    try:
        response = await _request(
            port,
            b"GET http://example.com/article?q=one HTTP/1.1\r\n"
            b"Host: example.com\r\nProxy-Authorization: Bearer internal-token\r\n\r\n",
        )
    finally:
        await proxy.close()
        upstream.close()
        await upstream.wait_closed()
    assert response.endswith(b"offline upstream")
    assert received[0].startswith(b"GET /article?q=one HTTP/1.1\r\n")
    assert b"Host: example.com" in received[0]
    assert b"Proxy-Authorization" not in received[0]


@pytest.mark.asyncio
async def test_connection_byte_cap_closes_before_forwarding_large_response() -> None:
    async def upstream_handler(
        _reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        body = b"x" * 2000
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Length: 2000\r\nConnection: close\r\n\r\n" + body
        )
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    upstream = await asyncio.start_server(upstream_handler, "127.0.0.1", 0)
    upstream_port = int(upstream.sockets[0].getsockname()[1])

    async def connector(_ip: str, _port: int):
        return await asyncio.open_connection("127.0.0.1", upstream_port)

    config = ProxyConfig(
        listen_host="127.0.0.1",
        listen_port=0,
        max_connection_bytes=512,
    )
    proxy, port = await _start_proxy(
        config=config,
        resolver=lambda _url: ["93.184.216.34"],
        connector=connector,
    )
    try:
        response = await _request(port, _request_bytes("http://example.com/"))
    finally:
        await proxy.close()
        upstream.close()
        await upstream.wait_closed()
    assert response.startswith(b"HTTP/1.1 200 OK")
    assert b"x" * 512 not in response
    assert len(response) < 512


@pytest.mark.asyncio
async def test_idle_timeout_closes_connect_tunnel() -> None:
    async def idle_upstream(
        _reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            await asyncio.sleep(1)
        finally:
            writer.close()
            await writer.wait_closed()

    upstream = await asyncio.start_server(idle_upstream, "127.0.0.1", 0)
    upstream_port = int(upstream.sockets[0].getsockname()[1])

    async def connector(_ip: str, _port: int):
        return await asyncio.open_connection("127.0.0.1", upstream_port)

    config = ProxyConfig(
        listen_host="127.0.0.1",
        listen_port=0,
        idle_timeout_seconds=0.1,
    )
    proxy, port = await _start_proxy(
        config=config,
        resolver=lambda _url: ["93.184.216.34"],
        connector=connector,
    )
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"CONNECT example.com:443 HTTP/1.1\r\n\r\n")
        await writer.drain()
        assert b"200 Connection Established" in await asyncio.wait_for(
            reader.readuntil(b"\r\n\r\n"), 1
        )
        assert await asyncio.wait_for(reader.read(), 1) == b""
        writer.close()
        await writer.wait_closed()
    finally:
        await proxy.close()
        upstream.close()
        await upstream.wait_closed()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "authorization",
    [
        None,
        "Bearer wrong-token",
        "Basic " + base64.b64encode(b"autowork:wrong-token").decode("ascii"),
    ],
)
async def test_proxy_auth_rejects_missing_or_invalid_token(authorization: str | None) -> None:
    connections = 0

    async def connector(_ip: str, _port: int):
        nonlocal connections
        connections += 1
        raise AssertionError("unauthorized request reached upstream")

    proxy, port = await _start_proxy(
        config=ProxyConfig(
            listen_host="127.0.0.1",
            listen_port=0,
            auth_token="expected-token",
        ),
        resolver=lambda _url: ["93.184.216.34"],
        connector=connector,
    )
    try:
        response = await _request(
            port,
            _request_bytes("http://example.com/", proxy_authorization=authorization),
        )
    finally:
        await proxy.close()
    assert response.startswith(b"HTTP/1.1 407")
    assert connections == 0


@pytest.mark.asyncio
async def test_proxy_auth_accepts_basic_and_bearer_tokens() -> None:
    async def upstream_handler(
        _reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        writer.write(b"HTTP/1.1 204 No Content\r\nContent-Length: 0\r\n\r\n")
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    upstream = await asyncio.start_server(upstream_handler, "127.0.0.1", 0)
    upstream_port = int(upstream.sockets[0].getsockname()[1])

    async def connector(_ip: str, _port: int):
        return await asyncio.open_connection("127.0.0.1", upstream_port)

    proxy, port = await _start_proxy(
        config=ProxyConfig(
            listen_host="127.0.0.1",
            listen_port=0,
            auth_token="expected-token",
        ),
        resolver=lambda _url: ["93.184.216.34"],
        connector=connector,
    )
    try:
        for authorization in (
            "Bearer expected-token",
            "Basic " + base64.b64encode(b"autowork:expected-token").decode("ascii"),
        ):
            response = await _request(
                port,
                _request_bytes("http://example.com/", proxy_authorization=authorization),
            )
            assert response.startswith(b"HTTP/1.1 204")
    finally:
        await proxy.close()
        upstream.close()
        await upstream.wait_closed()
