from __future__ import annotations

import asyncio
import ssl
from urllib.parse import urlsplit

from cti_app.application.http_collection import (
    DownloadTooLargeError,
    DownloadTransientError,
    PinnedHttpRequest,
    RawHttpResponse,
)


class AsyncioPinnedHttpTransport:
    """Small HTTP/1.1 client that connects to the exact approved DNS address."""

    async def request(self, request: PinnedHttpRequest) -> RawHttpResponse:
        parsed = urlsplit(request.url)
        host = parsed.hostname
        if host is None:
            raise ValueError("Request URL has no host")
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        ssl_context = ssl.create_default_context() if parsed.scheme == "https" else None
        server_hostname = host if ssl_context is not None else None
        try:
            async with asyncio.timeout(request.timeout_seconds):
                reader, writer = await asyncio.open_connection(
                    request.approved_ip,
                    port,
                    ssl=ssl_context,
                    server_hostname=server_hostname,
                )
                try:
                    target = parsed.path or "/"
                    if parsed.query:
                        target += f"?{parsed.query}"
                    host_header = host
                    if parsed.port and parsed.port not in {80, 443}:
                        host_header = f"{host}:{parsed.port}"
                    request_bytes = (
                        f"GET {target} HTTP/1.1\r\n"
                        f"Host: {host_header}\r\n"
                        f"User-Agent: {request.user_agent}\r\n"
                        "Accept: text/html, application/pdf;q=0.9\r\n"
                        "Accept-Encoding: gzip, deflate\r\n"
                        "Connection: close\r\n\r\n"
                    ).encode("ascii")
                    writer.write(request_bytes)
                    await writer.drain()
                    status_line = await reader.readline()
                    parts = status_line.decode("iso-8859-1").strip().split(" ", 2)
                    if len(parts) < 2 or not parts[1].isdigit():
                        raise DownloadTransientError("Invalid HTTP status line")
                    headers = await _read_headers(reader)
                    content_length = headers.get("content-length")
                    if content_length and int(content_length) > request.max_wire_bytes:
                        raise DownloadTooLargeError("Content-Length exceeds the download limit")
                    if headers.get("transfer-encoding", "").casefold() == "chunked":
                        body = await _read_chunked(reader, request.max_wire_bytes)
                    else:
                        body = await _read_bounded(reader, request.max_wire_bytes)
                    return RawHttpResponse(status=int(parts[1]), headers=headers, body=body)
                finally:
                    writer.close()
                    await writer.wait_closed()
        except TimeoutError as exc:
            raise DownloadTransientError("Collection timeout exceeded") from exc
        except OSError as exc:
            raise DownloadTransientError("Remote connection failed") from exc


async def _read_headers(reader: asyncio.StreamReader) -> dict[str, str]:
    headers: dict[str, str] = {}
    total = 0
    while True:
        line = await reader.readline()
        total += len(line)
        if total > 64 * 1024:
            raise DownloadTooLargeError("HTTP headers exceed the limit")
        if line in {b"\r\n", b"\n", b""}:
            return headers
        name, separator, value = line.decode("iso-8859-1").partition(":")
        if not separator:
            raise DownloadTransientError("Invalid HTTP header")
        headers[name.strip().casefold()] = value.strip()


async def _read_bounded(reader: asyncio.StreamReader, limit: int) -> bytes:
    result = bytearray()
    while chunk := await reader.read(min(64 * 1024, limit + 1 - len(result))):
        result.extend(chunk)
        if len(result) > limit:
            raise DownloadTooLargeError("Response exceeds the download limit")
    return bytes(result)


async def _read_chunked(reader: asyncio.StreamReader, limit: int) -> bytes:
    result = bytearray()
    while True:
        size_line = await reader.readline()
        try:
            size = int(size_line.split(b";", 1)[0].strip(), 16)
        except ValueError as exc:
            raise DownloadTransientError("Invalid chunk size") from exc
        if size == 0:
            await reader.readline()
            return bytes(result)
        if len(result) + size > limit:
            raise DownloadTooLargeError("Chunked response exceeds the download limit")
        result.extend(await reader.readexactly(size))
        if await reader.readexactly(2) != b"\r\n":
            raise DownloadTransientError("Invalid chunk terminator")
