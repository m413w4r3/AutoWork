from __future__ import annotations

import asyncio
import base64
import binascii
import hmac
import inspect
import ipaddress
import json
import os
import re
import socket
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from urllib.parse import parse_qsl, unquote, urlsplit

POLICY_VERSION = "browser-egress-v1"
DEFAULT_ALLOWED_PORTS = frozenset({80, 443})
MAX_HEADER_BYTES = 64 * 1024
MAX_CONNECTION_BYTES = 16 * 1024 * 1024
MAX_CONNECTIONS = 16
MAX_TOTAL_CONNECTION_BYTES = 64 * 1024 * 1024
IDLE_TIMEOUT_SECONDS = 15.0
READ_CHUNK_BYTES = 64 * 1024
_METADATA_HOSTS = frozenset(
    {
        "metadata",
        "metadata.google.internal",
        "metadata.azure.internal",
        "metadata.azure.com",
        "instance-data.ec2.internal",
        "metadata.tencentyun.com",
        "metadata.oraclecloud.com",
    }
)
_DOMAIN_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_CREDENTIAL_URL_KEYS = frozenset(
    {
        "token",
        "secret",
        "signature",
        "sig",
        "auth",
        "authorization",
        "bearer",
        "apikey",
        "api_key",
        "client_secret",
        "cookie",
        "password",
        "credential",
        "credentials",
        "awsaccesskeyid",
        "x-amz-credential",
        "x-amz-signature",
        "x-amz-security-token",
        "x-goog-signature",
        "x-goog-credential",
        "jwt",
        "code",
        "id_token",
        "access_token",
        "refresh_token",
        "session",
        "sessionid",
        "key",
    }
)
_NORMALIZED_CREDENTIAL_URL_KEYS = frozenset(
    re.sub(r"[^a-z0-9]", "", key) for key in _CREDENTIAL_URL_KEYS
)

Resolver = Callable[[str], Sequence[str] | Awaitable[Sequence[str]]]
Connector = Callable[
    [str, int],
    tuple[asyncio.StreamReader, asyncio.StreamWriter]
    | Awaitable[tuple[asyncio.StreamReader, asyncio.StreamWriter]],
]


class ProxyError(Exception):
    def __init__(self, status: int, reason: str) -> None:
        super().__init__(reason)
        self.status = status


@dataclass(frozen=True, slots=True)
class ProxyConfig:
    allowed_domains: frozenset[str] = field(default_factory=frozenset)
    blocked_domains: frozenset[str] = field(default_factory=frozenset)
    allowed_ports: frozenset[int] = DEFAULT_ALLOWED_PORTS
    max_connections: int = MAX_CONNECTIONS
    max_connection_bytes: int = MAX_CONNECTION_BYTES
    max_total_connection_bytes: int = MAX_TOTAL_CONNECTION_BYTES
    max_header_bytes: int = MAX_HEADER_BYTES
    idle_timeout_seconds: float = IDLE_TIMEOUT_SECONDS
    listen_host: str = "0.0.0.0"
    listen_port: int = 8791
    auth_token: str | None = None

    def __post_init__(self) -> None:
        if self.max_connections < 1:
            raise ValueError("max_connections must be positive")
        if (
            self.max_connection_bytes < 1
            or self.max_total_connection_bytes < 1
            or self.max_header_bytes < 128
        ):
            raise ValueError("proxy byte limits must be positive")
        if self.idle_timeout_seconds <= 0:
            raise ValueError("idle_timeout_seconds must be positive")
        if not self.allowed_ports or any(
            not 1 <= port <= 65535 for port in self.allowed_ports
        ):
            raise ValueError("allowed_ports must contain valid TCP ports")


class _GlobalByteBudget:
    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.used = 0

    def add(self, size: int) -> None:
        if size < 0 or self.used + size > self.limit:
            raise ProxyError(413, "proxy aggregate active byte limit exceeded")
        self.used += size

    def release(self, size: int) -> None:
        self.used = max(0, self.used - size)


class _ByteBudget:
    def __init__(
        self, limit: int, global_budget: _GlobalByteBudget | None = None
    ) -> None:
        self.limit = limit
        self.global_budget = global_budget
        self.used = 0

    def add(self, size: int) -> None:
        if size < 0 or self.used + size > self.limit:
            raise ProxyError(413, "connection byte limit exceeded")
        if self.global_budget is not None:
            self.global_budget.add(size)
        self.used += size

    def release(self) -> None:
        if self.global_budget is not None:
            self.global_budget.release(self.used)
        self.used = 0


class AsyncForwardProxy:
    """Small forward proxy with DNS pinning and an injectable network seam."""

    def __init__(
        self,
        config: ProxyConfig | None = None,
        *,
        resolver: Resolver | None = None,
        connector: Connector | None = None,
    ) -> None:
        self.config = config or ProxyConfig()
        self._resolver = resolver or self._resolve
        self._connector = connector or self._connect
        self._server: asyncio.AbstractServer | None = None
        self._bound_ports = {self.config.listen_port}
        self._active = 0
        self._active_lock = asyncio.Lock()
        self._total_byte_budget = _GlobalByteBudget(
            self.config.max_total_connection_bytes
        )

    async def start(self) -> int:
        if self._server is not None:
            raise RuntimeError("proxy is already running")
        self._server = await asyncio.start_server(
            self._handle_client,
            self.config.listen_host,
            self.config.listen_port,
            limit=self.config.max_header_bytes + 4,
        )
        sockets = self._server.sockets or []
        if sockets:
            port = int(sockets[0].getsockname()[1])
            self._bound_ports.add(port)
            return port
        return self.config.listen_port

    async def serve_forever(self) -> None:
        if self._server is None:
            await self.start()
        assert self._server is not None
        async with self._server:
            await self._server.serve_forever()

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        async with self._active_lock:
            if self._active >= self.config.max_connections:
                await self._write_error(writer, 503, "proxy busy")
                writer.close()
                await writer.wait_closed()
                return
            self._active += 1
        budget = _ByteBudget(self.config.max_connection_bytes, self._total_byte_budget)
        try:
            raw_header = await self._read_header(reader)
            budget.add(len(raw_header))
            request_line, headers = _parse_headers(raw_header)
            method, target, version = _parse_request_line(request_line)
            if method == "GET" and target == "/health":
                await self._write_health(writer, budget)
                return
            if not self._authorized(headers):
                raise ProxyError(407, "proxy authentication required")
            if method == "CONNECT":
                await self._handle_connect(target, reader, writer, headers, budget)
                return
            await self._handle_http(
                method, target, version, reader, writer, headers, budget
            )
        except ProxyError as exc:
            if not writer.is_closing():
                await self._write_error(writer, exc.status, str(exc), budget=budget)
        except TimeoutError:
            if not writer.is_closing():
                await self._write_error(
                    writer, 504, "proxy idle timeout", budget=budget
                )
        except (ConnectionError, OSError, asyncio.IncompleteReadError, ValueError):
            if not writer.is_closing():
                await self._write_error(
                    writer, 400, "invalid proxy request", budget=budget
                )
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass
            budget.release()
            async with self._active_lock:
                self._active -= 1

    async def _read_header(self, reader: asyncio.StreamReader) -> bytes:
        try:
            header = await asyncio.wait_for(
                reader.readuntil(b"\r\n\r\n"), self.config.idle_timeout_seconds
            )
        except asyncio.LimitOverrunError as exc:
            raise ProxyError(431, "request header too large") from exc
        if len(header) > self.config.max_header_bytes:
            raise ProxyError(431, "request header too large")
        return header

    def _authorized(self, headers: list[tuple[str, str]]) -> bool:
        token = self.config.auth_token
        if not token:
            return True
        values = [
            value for name, value in headers if name.casefold() == "proxy-authorization"
        ]
        if len(values) != 1:
            return False
        scheme, separator, credential = values[0].partition(" ")
        if not separator:
            return False
        scheme = scheme.casefold()
        if scheme == "bearer":
            return hmac.compare_digest(credential.strip(), token)
        if scheme != "basic":
            return False
        try:
            decoded = base64.b64decode(credential.strip(), validate=True).decode(
                "utf-8"
            )
        except (binascii.Error, UnicodeDecodeError):
            return False
        username, colon, password = decoded.partition(":")
        return bool(
            colon and username == "autowork" and hmac.compare_digest(password, token)
        )

    async def _handle_connect(
        self,
        target: str,
        client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
        headers: list[tuple[str, str]],
        budget: _ByteBudget,
    ) -> None:
        del headers
        host, port = _parse_authority(target, self.config.allowed_ports)
        url = _authority_url(host, port, "https" if port == 443 else "http")
        ip = await self._resolve_and_validate(url, host, port)
        upstream_reader, upstream_writer = await self._open_upstream(ip, port)
        try:
            response = (
                b"HTTP/1.1 200 Connection Established\r\nProxy-Agent: AutoWork\r\n\r\n"
            )
            budget.add(len(response))
            client_writer.write(response)
            await asyncio.wait_for(
                client_writer.drain(), self.config.idle_timeout_seconds
            )
            await self._relay(
                client_reader, client_writer, upstream_reader, upstream_writer, budget
            )
        finally:
            upstream_writer.close()
            try:
                await upstream_writer.wait_closed()
            except OSError:
                pass

    async def _handle_http(
        self,
        method: str,
        target: str,
        version: str,
        client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
        headers: list[tuple[str, str]],
        budget: _ByteBudget,
    ) -> None:
        try:
            parsed = urlsplit(target)
            port = parsed.port or (80 if parsed.scheme.casefold() == "http" else 443)
        except ValueError as exc:
            raise ProxyError(400, "invalid absolute request target") from exc
        if (
            parsed.scheme.casefold() != "http"
            or not parsed.netloc
            or parsed.hostname is None
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise ProxyError(400, "absolute HTTP request target required")
        if not _credential_free_http_url(target):
            raise ProxyError(403, "credential-like destination URL is blocked")
        host = parsed.hostname
        if port not in self.config.allowed_ports:
            raise ProxyError(403, "destination port is not allowed")
        self._check_domain(host)
        supplied_hosts = [value for name, value in headers if name.casefold() == "host"]
        if len(supplied_hosts) > 1 or (
            supplied_hosts and not _host_header_matches(supplied_hosts[0], host, port)
        ):
            raise ProxyError(400, "Host header does not match request target")
        if any(name.casefold() == "expect" for name, _value in headers):
            raise ProxyError(417, "Expect request headers are not supported")
        if any(name.casefold() == "upgrade" for name, _value in headers):
            raise ProxyError(400, "HTTP upgrades are not supported")
        address = await self._resolve_and_validate(target, host, port)
        upstream_reader, upstream_writer = await self._open_upstream(address, port)
        try:
            path = parsed.path or "/"
            if parsed.query:
                path += "?" + parsed.query
            outgoing_headers = [
                (name, value)
                for name, value in headers
                if name.casefold()
                not in {
                    "proxy-authorization",
                    "proxy-connection",
                    "connection",
                    "keep-alive",
                }
            ]
            if not supplied_hosts:
                outgoing_headers.append(("Host", _format_authority(host, port)))
            outgoing_headers.append(("Connection", "close"))
            request_head = (
                f"{method} {path} {version}\r\n"
                + "".join(f"{name}: {value}\r\n" for name, value in outgoing_headers)
                + "\r\n"
            ).encode("latin-1")
            budget.add(len(request_head))
            upstream_writer.write(request_head)
            await asyncio.wait_for(
                upstream_writer.drain(), self.config.idle_timeout_seconds
            )
            await self._copy_request_body(
                client_reader, upstream_writer, headers, budget
            )
            await asyncio.wait_for(
                upstream_writer.drain(), self.config.idle_timeout_seconds
            )
            await self._forward_response(upstream_reader, client_writer, budget)
        finally:
            upstream_writer.close()
            try:
                await upstream_writer.wait_closed()
            except OSError:
                pass

    async def _copy_request_body(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        headers: list[tuple[str, str]],
        budget: _ByteBudget,
    ) -> None:
        lengths = [
            value.strip()
            for name, value in headers
            if name.casefold() == "content-length"
        ]
        transfer_encodings = [
            value.strip().casefold()
            for name, value in headers
            if name.casefold() == "transfer-encoding"
        ]
        if len(lengths) > 1:
            raise ProxyError(400, "conflicting Content-Length headers")
        if lengths and transfer_encodings:
            raise ProxyError(400, "ambiguous request body framing")
        if transfer_encodings:
            if len(transfer_encodings) != 1 or transfer_encodings[0] != "chunked":
                raise ProxyError(400, "unsupported Transfer-Encoding")
            await self._copy_chunked_body(reader, writer, budget)
        elif lengths:
            try:
                if not re.fullmatch(r"[0-9]+", lengths[0]):
                    raise ValueError
                remaining = int(lengths[0], 10)
            except ValueError as exc:
                raise ProxyError(400, "invalid Content-Length") from exc
            if remaining < 0:
                raise ProxyError(400, "invalid Content-Length")
            await self._copy_exact(reader, writer, remaining, budget)

    async def _copy_chunked_body(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        budget: _ByteBudget,
    ) -> None:
        trailer_bytes = 0
        while True:
            line = await asyncio.wait_for(
                reader.readline(), self.config.idle_timeout_seconds
            )
            if not line or len(line) > self.config.max_header_bytes:
                raise ProxyError(400, "invalid chunked request body")
            budget.add(len(line))
            trailer_bytes += len(line)
            writer.write(line)
            try:
                chunk_size = int(line.split(b";", 1)[0].strip(), 16)
            except ValueError as exc:
                raise ProxyError(400, "invalid chunked request body") from exc
            if chunk_size < 0:
                raise ProxyError(400, "invalid chunked request body")
            if chunk_size == 0:
                while True:
                    trailer = await asyncio.wait_for(
                        reader.readline(), self.config.idle_timeout_seconds
                    )
                    trailer_bytes += len(trailer)
                    if not trailer or trailer_bytes > self.config.max_header_bytes:
                        raise ProxyError(400, "invalid chunked request trailers")
                    budget.add(len(trailer))
                    writer.write(trailer)
                    if trailer == b"\r\n":
                        return
            await self._copy_exact(reader, writer, chunk_size + 2, budget)

    async def _copy_exact(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        remaining: int,
        budget: _ByteBudget,
    ) -> None:
        while remaining:
            chunk = await asyncio.wait_for(
                reader.read(min(READ_CHUNK_BYTES, remaining)),
                self.config.idle_timeout_seconds,
            )
            if not chunk:
                raise ProxyError(400, "incomplete request body")
            budget.add(len(chunk))
            writer.write(chunk)
            remaining -= len(chunk)

    async def _forward_response(
        self,
        upstream_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
        budget: _ByteBudget,
    ) -> None:
        while True:
            try:
                response_head = await asyncio.wait_for(
                    upstream_reader.readuntil(b"\r\n\r\n"),
                    self.config.idle_timeout_seconds,
                )
            except asyncio.LimitOverrunError as exc:
                raise ProxyError(502, "upstream response header too large") from exc
            if len(response_head) > self.config.max_header_bytes:
                raise ProxyError(502, "upstream response header too large")
            status = _response_status(response_head)
            budget.add(len(response_head))
            client_writer.write(response_head)
            await asyncio.wait_for(
                client_writer.drain(), self.config.idle_timeout_seconds
            )
            if status < 100 or status >= 200 or status == 101:
                break
        while True:
            chunk = await asyncio.wait_for(
                upstream_reader.read(READ_CHUNK_BYTES), self.config.idle_timeout_seconds
            )
            if not chunk:
                return
            budget.add(len(chunk))
            client_writer.write(chunk)
            await asyncio.wait_for(
                client_writer.drain(), self.config.idle_timeout_seconds
            )

    async def _relay(
        self,
        client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
        upstream_reader: asyncio.StreamReader,
        upstream_writer: asyncio.StreamWriter,
        budget: _ByteBudget,
    ) -> None:
        last_activity = time.monotonic()

        async def pump(
            source: asyncio.StreamReader, destination: asyncio.StreamWriter
        ) -> None:
            nonlocal last_activity
            while True:
                try:
                    chunk = await asyncio.wait_for(
                        source.read(READ_CHUNK_BYTES), self.config.idle_timeout_seconds
                    )
                except TimeoutError:
                    if (
                        time.monotonic() - last_activity
                        < self.config.idle_timeout_seconds
                    ):
                        continue
                    return
                if not chunk:
                    return
                last_activity = time.monotonic()
                budget.add(len(chunk))
                destination.write(chunk)
                await asyncio.wait_for(
                    destination.drain(), self.config.idle_timeout_seconds
                )

        pumps = {
            asyncio.create_task(pump(client_reader, upstream_writer)),
            asyncio.create_task(pump(upstream_reader, client_writer)),
        }
        try:
            done, pending = await asyncio.wait(
                pumps, return_when=asyncio.FIRST_COMPLETED
            )
            for task in done:
                task.result()
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
        finally:
            upstream_writer.close()
            try:
                await upstream_writer.wait_closed()
            except OSError:
                pass

    async def _resolve_and_validate(self, url: str, host: str, port: int) -> str:
        self._check_domain(host)
        if port not in self.config.allowed_ports:
            raise ProxyError(403, "destination port is not allowed")
        if port in self._bound_ports:
            raise ProxyError(403, "proxy listener destination is blocked")
        try:
            result = self._resolver(url)
            addresses = (
                await asyncio.wait_for(result, self.config.idle_timeout_seconds)
                if inspect.isawaitable(result)
                else result
            )
        except (OSError, TimeoutError) as exc:
            raise ProxyError(502, "destination resolution failed") from exc
        if len(addresses) > 64:
            raise ProxyError(403, "destination returned too many addresses")
        unique: list[str] = []
        for value in addresses:
            if not isinstance(value, str) or not value or "%" in value:
                raise ProxyError(403, "invalid destination address")
            try:
                address = ipaddress.ip_address(value)
            except ValueError as exc:
                raise ProxyError(403, "invalid destination address") from exc
            if not address.is_global or address.is_loopback or address.is_link_local:
                raise ProxyError(403, "non-public destination address")
            if address.is_multicast or address.is_unspecified or address.is_reserved:
                raise ProxyError(403, "non-public destination address")
            if port == self.config.listen_port and self._matches_listener(
                address, host
            ):
                raise ProxyError(403, "proxy listener destination is blocked")
            normalized = address.compressed
            if normalized not in unique:
                unique.append(normalized)
        if not unique:
            raise ProxyError(403, "destination has no addresses")
        # All DNS answers must be safe; pin the connection to the first checked address.
        return unique[0]

    def _check_domain(self, host: str) -> None:
        normalized = _normalize_hostname(host)
        if _is_forbidden_hostname(normalized):
            raise ProxyError(403, "destination hostname is blocked")
        if any(
            _matches_domain(normalized, domain)
            for domain in self.config.blocked_domains
        ):
            raise ProxyError(403, "destination domain is blocked")
        if self.config.allowed_domains and not any(
            _matches_domain(normalized, domain)
            for domain in self.config.allowed_domains
        ):
            raise ProxyError(403, "destination domain is outside the allow-list")

    def _matches_listener(
        self,
        address: ipaddress.IPv4Address | ipaddress.IPv6Address,
        host: str,
    ) -> bool:
        configured = self.config.listen_host
        if configured not in {"0.0.0.0", "::", ""}:
            try:
                if ipaddress.ip_address(configured) == address:
                    return True
            except ValueError:
                if _normalize_hostname(configured) == _normalize_hostname(host):
                    return True
        return _normalize_hostname(host) == _normalize_hostname(configured)

    async def _open_upstream(
        self, address: str, port: int
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        try:
            result = self._connector(address, port)
            pair = (
                await asyncio.wait_for(result, self.config.idle_timeout_seconds)
                if inspect.isawaitable(result)
                else result
            )
            return pair
        except (OSError, TimeoutError) as exc:
            raise ProxyError(502, "upstream connection failed") from exc

    @staticmethod
    async def _resolve(url: str) -> Sequence[str]:
        parsed = urlsplit(url)
        if parsed.hostname is None:
            return ()
        try:
            return (ipaddress.ip_address(parsed.hostname).compressed,)
        except ValueError:
            loop = asyncio.get_running_loop()
            results = await loop.getaddrinfo(
                parsed.hostname, parsed.port, type=socket.SOCK_STREAM
            )
            return tuple(dict.fromkeys(item[4][0] for item in results))

    @staticmethod
    async def _connect(
        address: str, port: int
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        return await asyncio.open_connection(address, port)

    async def _write_health(
        self, writer: asyncio.StreamWriter, budget: _ByteBudget
    ) -> None:
        body = json.dumps(
            {"status": "ok", "policy_version": POLICY_VERSION}, separators=(",", ":")
        ).encode("utf-8")
        await self._write_response(writer, 200, "OK", body, budget=budget)

    async def _write_error(
        self,
        writer: asyncio.StreamWriter,
        status: int,
        message: str,
        *,
        budget: _ByteBudget | None = None,
    ) -> None:
        body = json.dumps({"error": message}, separators=(",", ":")).encode("utf-8")
        try:
            await self._write_response(
                writer, status, _reason(status), body, budget=budget
            )
        except (ConnectionError, OSError, TimeoutError, ProxyError):
            pass

    async def _write_response(
        self,
        writer: asyncio.StreamWriter,
        status: int,
        reason: str,
        body: bytes,
        *,
        budget: _ByteBudget | None,
    ) -> None:
        response = (
            f"HTTP/1.1 {status} {reason}\r\n"
            f"Content-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\n"
            "Connection: close\r\n\r\n"
        ).encode("ascii") + body
        if budget is not None:
            budget.add(len(response))
        writer.write(response)
        await asyncio.wait_for(writer.drain(), self.config.idle_timeout_seconds)


def _parse_headers(raw: bytes) -> tuple[str, list[tuple[str, str]]]:
    try:
        lines = raw[:-4].decode("latin-1").split("\r\n")
    except UnicodeDecodeError as exc:
        raise ProxyError(400, "invalid request header") from exc
    if not lines or not lines[0]:
        raise ProxyError(400, "empty request")
    headers: list[tuple[str, str]] = []
    for line in lines[1:]:
        if not line or line[0] in " \t" or ":" not in line:
            raise ProxyError(400, "invalid request header")
        name, value = line.split(":", 1)
        if not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", name):
            raise ProxyError(400, "invalid request header")
        if "\r" in value or "\n" in value:
            raise ProxyError(400, "invalid request header")
        headers.append((name, value.strip()))
    return lines[0], headers


def _parse_request_line(value: str) -> tuple[str, str, str]:
    pieces = value.split(" ")
    if (
        len(pieces) != 3
        or not pieces[0].isalpha()
        or pieces[2] not in {"HTTP/1.0", "HTTP/1.1"}
    ):
        raise ProxyError(400, "invalid request line")
    return pieces[0].upper(), pieces[1], pieces[2]


def _parse_authority(value: str, allowed_ports: frozenset[int]) -> tuple[str, int]:
    if not value or "/" in value or "@" in value:
        raise ProxyError(400, "invalid CONNECT authority")
    try:
        parsed = urlsplit("//" + value)
        host = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise ProxyError(400, "invalid CONNECT authority") from exc
    if (
        not host
        or parsed.username is not None
        or parsed.password is not None
        or port is None
    ):
        raise ProxyError(400, "CONNECT requires host and port")
    if port not in allowed_ports:
        raise ProxyError(403, "destination port is not allowed")
    return host, port


def _normalize_hostname(host: str) -> str:
    normalized = host.rstrip(".").casefold()
    if not normalized or len(normalized) > 253:
        raise ProxyError(400, "invalid destination hostname")
    try:
        ipaddress.ip_address(normalized)
        return normalized
    except ValueError:
        pass
    labels = normalized.split(".")
    if len(labels) < 2 or any(not _DOMAIN_LABEL.fullmatch(label) for label in labels):
        raise ProxyError(403, "private hostname is blocked")
    return normalized


def _credential_free_http_url(value: str, *, depth: int = 0) -> bool:
    """Mirror the renderer gate for absolute-form HTTP URLs before forwarding."""
    if depth > 3:
        return False
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme.casefold() not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port in {0}
        ):
            return False
        for key, query_value in parse_qsl(parsed.query, keep_blank_values=True):
            normalized_key = re.sub(r"[^a-z0-9]", "", unquote(key).casefold())
            cloud_signed_key = normalized_key.startswith(("xamz", "xgoog")) and (
                normalized_key.endswith(("credential", "signature", "securitytoken"))
            )
            if normalized_key in _NORMALIZED_CREDENTIAL_URL_KEYS or cloud_signed_key:
                return False
            nested_value = unquote(query_value)
            if nested_value.casefold().startswith(("http://", "https://")) and not (
                _credential_free_http_url(nested_value, depth=depth + 1)
            ):
                return False
        for segment in parsed.path.split("/"):
            decoded = segment
            for _ in range(3):
                decoded = unquote(decoded)
            key = re.split(r"[=/:]", decoded, maxsplit=1)[0]
            normalized_key = re.sub(r"[^a-z0-9]", "", key.casefold())
            if normalized_key in _NORMALIZED_CREDENTIAL_URL_KEYS:
                return False
        return True
    except ValueError:
        return False


def _is_forbidden_hostname(host: str) -> bool:
    if host in _METADATA_HOSTS:
        return True
    if host.endswith(
        (
            ".localhost",
            ".local",
            ".internal",
            ".docker",
            ".docker.internal",
            ".test",
            ".default",
            ".svc",
            ".cluster.local",
        )
    ):
        return True
    return any(host.endswith("." + forbidden) for forbidden in _METADATA_HOSTS)


def _matches_domain(host: str, domain: str) -> bool:
    return host == domain or host.endswith("." + domain)


def _host_header_matches(value: str, host: str, port: int) -> bool:
    try:
        parsed = urlsplit("//" + value)
        return (
            parsed.hostname is not None
            and _normalize_hostname(parsed.hostname) == _normalize_hostname(host)
            and (parsed.port or 80) == port
            and parsed.username is None
            and parsed.password is None
        )
    except (ProxyError, ValueError):
        return False


def _format_authority(host: str, port: int) -> str:
    formatted = f"[{host}]" if ":" in host else host
    return formatted if port == 80 else f"{formatted}:{port}"


def _authority_url(host: str, port: int, scheme: str) -> str:
    return f"{scheme}://{_format_authority(host, port)}/"


def _response_status(raw: bytes) -> int:
    try:
        status_line = raw.split(b"\r\n", 1)[0].decode("ascii")
        pieces = status_line.split(" ", 2)
        if len(pieces) < 2 or pieces[0] not in {"HTTP/1.0", "HTTP/1.1"}:
            raise ValueError
        status = int(pieces[1])
        if not 100 <= status <= 599:
            raise ValueError
        return status
    except (UnicodeDecodeError, ValueError) as exc:
        raise ProxyError(502, "invalid upstream response") from exc


def _reason(status: int) -> str:
    return {
        200: "OK",
        400: "Bad Request",
        403: "Forbidden",
        407: "Proxy Authentication Required",
        417: "Expectation Failed",
        431: "Request Header Fields Too Large",
        413: "Content Too Large",
        502: "Bad Gateway",
        503: "Service Unavailable",
        504: "Gateway Timeout",
    }.get(status, "Proxy Error")


def _domains_from_env(name: str) -> frozenset[str]:
    raw = os.environ.get(name, "")
    domains = frozenset(
        item.strip().rstrip(".").casefold() for item in raw.split(",") if item.strip()
    )
    for domain in domains:
        labels = domain.split(".")
        if len(domain) > 253 or any(
            not _DOMAIN_LABEL.fullmatch(label) for label in labels
        ):
            raise ValueError(f"invalid domain policy in {name}")
    return domains


def config_from_env() -> ProxyConfig:
    return ProxyConfig(
        allowed_domains=_domains_from_env("BROWSER_EGRESS_ALLOWED_DOMAINS"),
        blocked_domains=_domains_from_env("BROWSER_EGRESS_BLOCKED_DOMAINS"),
        max_connections=int(
            os.environ.get("BROWSER_EGRESS_MAX_CONNECTIONS", str(MAX_CONNECTIONS))
        ),
        max_connection_bytes=int(
            os.environ.get(
                "BROWSER_EGRESS_MAX_CONNECTION_BYTES", str(MAX_CONNECTION_BYTES)
            )
        ),
        max_header_bytes=int(
            os.environ.get("BROWSER_EGRESS_MAX_HEADER_BYTES", str(MAX_HEADER_BYTES))
        ),
        idle_timeout_seconds=float(
            os.environ.get(
                "BROWSER_EGRESS_IDLE_TIMEOUT_SECONDS", str(IDLE_TIMEOUT_SECONDS)
            )
        ),
        listen_host=os.environ.get("BROWSER_EGRESS_BIND", "0.0.0.0"),
        listen_port=int(os.environ.get("BROWSER_EGRESS_PORT", "8791")),
        auth_token=os.environ.get("BROWSER_EGRESS_PROXY_TOKEN") or None,
    )


async def _run() -> None:
    proxy = AsyncForwardProxy(config_from_env())
    await proxy.serve_forever()


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
