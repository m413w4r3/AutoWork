from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

import pytest

from cti_app.application.http_collection import (
    DownloadTooLargeError,
    DownloadTransientError,
    PinnedHttpRequest,
)
from cti_app.infrastructure.http import AsyncioPinnedHttpTransport


@asynccontextmanager
async def controlled_server(
    handler: Callable[[asyncio.StreamReader, asyncio.StreamWriter], Awaitable[None]],
) -> AsyncIterator[int]:
    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    port = int(server.sockets[0].getsockname()[1])
    try:
        yield port
    finally:
        server.close()
        await server.wait_closed()


def request(port: int, *, scheme: str = "http", timeout: float = 1.0) -> PinnedHttpRequest:
    return PinnedHttpRequest(
        url=f"{scheme}://transport.test:{port}/report",
        approved_ip="127.0.0.1",
        timeout_seconds=timeout,
        max_wire_bytes=1024,
        user_agent="transport-test",
    )


def responder(
    payload: bytes,
) -> Callable[[asyncio.StreamReader, asyncio.StreamWriter], Awaitable[None]]:
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readuntil(b"\r\n\r\n")
        writer.write(payload)
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    return handle


@pytest.mark.parametrize(
    ("content_length", "message"),
    [("invalid", "Invalid Content-Length"), ("-1", "Negative Content-Length")],
)
async def test_invalid_content_length_is_typed(content_length: str, message: str) -> None:
    payload = f"HTTP/1.1 200 OK\r\nContent-Length: {content_length}\r\n\r\n".encode()
    async with controlled_server(responder(payload)) as port:
        with pytest.raises(DownloadTransientError, match=message):
            await AsyncioPinnedHttpTransport().request(request(port))


async def test_truncated_content_length_is_typed() -> None:
    async with controlled_server(
        responder(b"HTTP/1.1 200 OK\r\nContent-Length: 20\r\n\r\nshort")
    ) as port:
        with pytest.raises(DownloadTransientError, match="shorter"):
            await AsyncioPinnedHttpTransport().request(request(port))


async def test_incomplete_chunk_is_typed() -> None:
    payload = b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n5\r\nabc"
    async with controlled_server(responder(payload)) as port:
        with pytest.raises(DownloadTransientError, match="truncated"):
            await AsyncioPinnedHttpTransport().request(request(port))


async def test_invalid_status_line_is_typed() -> None:
    async with controlled_server(responder(b"NOT-HTTP\r\n\r\n")) as port:
        with pytest.raises(DownloadTransientError, match="status line"):
            await AsyncioPinnedHttpTransport().request(request(port))


async def test_oversized_headers_are_typed() -> None:
    payload = b"HTTP/1.1 200 OK\r\nX-Large: " + b"a" * 70_000 + b"\r\n\r\n"
    async with controlled_server(responder(payload)) as port:
        with pytest.raises(DownloadTooLargeError, match="header"):
            await AsyncioPinnedHttpTransport().request(request(port))


async def test_tls_error_is_typed() -> None:
    async def plain_http(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        del reader
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n")
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    async with controlled_server(plain_http) as port:
        with pytest.raises(DownloadTransientError, match=r"TLS|connection"):
            await AsyncioPinnedHttpTransport().request(request(port, scheme="https"))


async def test_read_timeout_is_typed() -> None:
    async def stalled(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readuntil(b"\r\n\r\n")
        await asyncio.sleep(0.2)
        writer.close()
        await writer.wait_closed()

    async with controlled_server(stalled) as port:
        with pytest.raises(DownloadTransientError, match="timeout"):
            await AsyncioPinnedHttpTransport().request(request(port, timeout=0.01))
