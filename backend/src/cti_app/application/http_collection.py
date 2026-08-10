from __future__ import annotations

import hashlib
import ipaddress
import re
import socket
import time
import zlib
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol
from urllib.parse import SplitResult, urljoin, urlsplit

from cti_app.domain.collection import AttemptOutcome, DetectedMimeType


class CollectionError(RuntimeError):
    outcome = AttemptOutcome.ERROR
    retryable = False

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.final_url: str | None = None
        self.redirect_chain: tuple[str, ...] = ()
        self.http_status: int | None = None
        self.headers: dict[str, str] = {}
        self.size: int | None = None

    def with_context(
        self,
        *,
        final_url: str,
        redirect_chain: Sequence[str],
        http_status: int | None = None,
        headers: dict[str, str] | None = None,
        size: int | None = None,
    ) -> CollectionError:
        self.final_url = final_url
        self.redirect_chain = tuple(redirect_chain)
        self.http_status = http_status
        self.headers = _allowed_headers(headers or {})
        self.size = size
        return self


class UnsafeAddressError(CollectionError):
    outcome = AttemptOutcome.BLOCKED


class DownloadTooLargeError(CollectionError):
    outcome = AttemptOutcome.TOO_LARGE


class DownloadUnavailableError(CollectionError):
    outcome = AttemptOutcome.UNAVAILABLE


class DownloadTransientError(CollectionError):
    retryable = True


class UnsupportedContentError(CollectionError):
    pass


@dataclass(frozen=True, slots=True)
class CollectionPolicy:
    max_redirects: int = 5
    timeout_seconds: float = 30.0
    max_download_bytes: int = 10 * 1024 * 1024
    max_expanded_bytes: int = 25 * 1024 * 1024
    max_decompression_ratio: float = 20.0
    user_agent: str = "CTI-Bulletin-Collector/1.0 (+internal-evidence-archiver)"
    allowed_domains: frozenset[str] = field(default_factory=frozenset)
    blocked_domains: frozenset[str] = field(default_factory=frozenset)

    def snapshot_id(self) -> str:
        value = "|".join(
            (
                str(self.max_redirects),
                str(self.timeout_seconds),
                str(self.max_download_bytes),
                str(self.max_expanded_bytes),
                str(self.max_decompression_ratio),
                self.user_agent,
                ",".join(sorted(self.allowed_domains)),
                ",".join(sorted(self.blocked_domains)),
            )
        )
        return hashlib.sha256(value.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class PinnedHttpRequest:
    url: str
    approved_ip: str
    timeout_seconds: float
    max_wire_bytes: int
    user_agent: str


@dataclass(frozen=True, slots=True)
class RawHttpResponse:
    status: int
    headers: dict[str, str]
    body: bytes


class HttpTransport(Protocol):
    async def request(self, request: PinnedHttpRequest) -> RawHttpResponse: ...


class DnsResolver(Protocol):
    async def resolve(self, hostname: str) -> Sequence[str]: ...


class SystemDnsResolver:
    async def resolve(self, hostname: str) -> Sequence[str]:
        import asyncio

        loop = asyncio.get_running_loop()
        records = await loop.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
        return tuple(dict.fromkeys(record[4][0] for record in records))


@dataclass(frozen=True, slots=True)
class CollectedResponse:
    requested_url: str
    final_url: str
    redirect_chain: tuple[str, ...]
    status: int
    headers: dict[str, str]
    declared_content_type: str | None
    detected_content_type: DetectedMimeType
    body: bytes
    sha256: str
    acquired_at: datetime


class SafeHttpCollector:
    def __init__(
        self,
        transport: HttpTransport,
        resolver: DnsResolver,
        policy: CollectionPolicy | None = None,
    ) -> None:
        self._transport = transport
        self._resolver = resolver
        self.policy = policy or CollectionPolicy()

    async def fetch(self, requested_url: str) -> CollectedResponse:
        current = requested_url.strip()
        redirects: list[str] = []
        deadline = time.monotonic() + self.policy.timeout_seconds
        for redirect_count in range(self.policy.max_redirects + 1):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise DownloadTransientError("Total collection timeout exceeded")
            try:
                parsed = _validate_url(current, self.policy)
                approved_ip = await self._resolve_and_pin(parsed.hostname or "", remaining)
            except CollectionError as exc:
                exc.with_context(final_url=current, redirect_chain=redirects)
                raise
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise DownloadTransientError("Total collection timeout exceeded")
            try:
                response = await self._transport.request(
                    PinnedHttpRequest(
                        url=current,
                        approved_ip=approved_ip,
                        timeout_seconds=remaining,
                        max_wire_bytes=self.policy.max_download_bytes,
                        user_agent=self.policy.user_agent,
                    )
                )
            except CollectionError as exc:
                exc.with_context(final_url=current, redirect_chain=redirects)
                raise
            if response.status in {301, 302, 303, 307, 308}:
                location = response.headers.get("location")
                if not location:
                    raise DownloadUnavailableError(
                        "Redirect response omitted Location"
                    ).with_context(
                        final_url=current,
                        redirect_chain=redirects,
                        http_status=response.status,
                        headers=response.headers,
                        size=len(response.body),
                    )
                if redirect_count >= self.policy.max_redirects:
                    raise DownloadUnavailableError("Maximum redirect count exceeded").with_context(
                        final_url=current,
                        redirect_chain=redirects,
                        http_status=response.status,
                        headers=response.headers,
                        size=len(response.body),
                    )
                current = urljoin(current, location)
                redirects.append(current)
                continue
            if response.status in {408, 425, 429} or response.status >= 500:
                raise DownloadTransientError(
                    f"Remote server returned HTTP {response.status}"
                ).with_context(
                    final_url=current,
                    redirect_chain=redirects,
                    http_status=response.status,
                    headers=response.headers,
                    size=len(response.body),
                )
            if response.status < 200 or response.status >= 300:
                raise DownloadUnavailableError(
                    f"Remote server returned HTTP {response.status}"
                ).with_context(
                    final_url=current,
                    redirect_chain=redirects,
                    http_status=response.status,
                    headers=response.headers,
                    size=len(response.body),
                )
            try:
                body = _decode_body(response.body, response.headers, self.policy)
                detected = _detect_mime(body)
            except CollectionError as exc:
                exc.with_context(
                    final_url=current,
                    redirect_chain=redirects,
                    http_status=response.status,
                    headers=response.headers,
                    size=len(response.body),
                )
                raise
            declared = _content_type(response.headers.get("content-type"))
            allowed_headers = _allowed_headers(response.headers)
            return CollectedResponse(
                requested_url=requested_url,
                final_url=current,
                redirect_chain=tuple(redirects),
                status=response.status,
                headers=allowed_headers,
                declared_content_type=declared,
                detected_content_type=detected,
                body=body,
                sha256=hashlib.sha256(body).hexdigest(),
                acquired_at=datetime.now(UTC),
            )
        raise AssertionError("redirect loop terminates in the loop")

    async def _resolve_and_pin(self, hostname: str, timeout_seconds: float) -> str:
        import asyncio

        try:
            async with asyncio.timeout(timeout_seconds):
                first = tuple(dict.fromkeys(await self._resolver.resolve(hostname)))
                second = tuple(dict.fromkeys(await self._resolver.resolve(hostname)))
        except TimeoutError as exc:
            raise DownloadTransientError("DNS resolution exceeded the total timeout") from exc
        if not first or not second:
            raise DownloadTransientError("DNS resolution returned no address")
        if set(first) != set(second):
            raise UnsafeAddressError("DNS answers changed before connection")
        for value in first:
            _validate_ip(value)
        return first[0]


def parse_domain_policy(value: str) -> frozenset[str]:
    domains = frozenset(
        item.strip().rstrip(".").casefold() for item in value.split(",") if item.strip()
    )
    if any(
        len(domain) > 253
        or any(
            not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
            for label in domain.split(".")
        )
        for domain in domains
    ):
        raise ValueError("Collection domain policy contains an invalid hostname")
    return domains


def _validate_url(url: str, policy: CollectionPolicy) -> SplitResult:
    parsed = urlsplit(url)
    if parsed.scheme.lower() not in {"http", "https"}:
        raise UnsafeAddressError("Only HTTP and HTTPS URLs are allowed")
    if not parsed.hostname or parsed.username or parsed.password:
        raise UnsafeAddressError("URL host is missing or contains credentials")
    hostname = parsed.hostname.rstrip(".").casefold()
    if hostname in {"localhost", "metadata.google.internal"} or hostname.endswith(".localhost"):
        raise UnsafeAddressError("Local and cloud metadata hosts are blocked")
    if hostname in policy.blocked_domains or any(
        hostname.endswith(f".{domain}") for domain in policy.blocked_domains
    ):
        raise UnsafeAddressError("Domain is blocked by collection policy")
    if policy.allowed_domains and not (
        hostname in policy.allowed_domains
        or any(hostname.endswith(f".{domain}") for domain in policy.allowed_domains)
    ):
        raise UnsafeAddressError("Domain is outside the collection allow-list")
    try:
        _validate_ip(hostname)
    except ValueError:
        pass
    return parsed


def _validate_ip(value: str) -> None:
    address = ipaddress.ip_address(value.split("%", 1)[0])
    if not address.is_global:
        raise UnsafeAddressError(f"Non-public address is blocked: {address.compressed}")
    if address.is_loopback or address.is_link_local or address.is_multicast:
        raise UnsafeAddressError(f"Unsafe address is blocked: {address.compressed}")


def _decode_body(body: bytes, headers: dict[str, str], policy: CollectionPolicy) -> bytes:
    if len(body) > policy.max_download_bytes:
        raise DownloadTooLargeError("Compressed response exceeds the download limit")
    encoding = headers.get("content-encoding", "identity").casefold().strip()
    try:
        if encoding in {"", "identity"}:
            expanded = body
        elif encoding == "gzip":
            expanded = _bounded_decompress(body, policy.max_expanded_bytes, 16 + zlib.MAX_WBITS)
        elif encoding == "deflate":
            expanded = _bounded_decompress(body, policy.max_expanded_bytes, zlib.MAX_WBITS)
        else:
            raise UnsupportedContentError(f"Unsupported Content-Encoding: {encoding}")
    except zlib.error as exc:
        raise UnsupportedContentError("Invalid compressed response") from exc
    if len(expanded) > policy.max_expanded_bytes:
        raise DownloadTooLargeError("Expanded response exceeds the size limit")
    if body and len(expanded) / len(body) > policy.max_decompression_ratio:
        raise DownloadTooLargeError("Response exceeds the decompression ratio limit")
    return expanded


def _bounded_decompress(body: bytes, limit: int, window_bits: int) -> bytes:
    decompressor = zlib.decompressobj(window_bits)
    expanded = decompressor.decompress(body, limit + 1)
    if decompressor.unconsumed_tail or len(expanded) > limit:
        raise DownloadTooLargeError("Expanded response exceeds the size limit")
    expanded += decompressor.flush(max(1, limit + 1 - len(expanded)))
    if len(expanded) > limit:
        raise DownloadTooLargeError("Expanded response exceeds the size limit")
    if not decompressor.eof:
        raise zlib.error("Compressed stream ended prematurely")
    return expanded


def _detect_mime(body: bytes) -> DetectedMimeType:
    prefix = body[:1024]
    if prefix.startswith(b"\xef\xbb\xbf"):
        prefix = prefix[3:]
    prefix = prefix.lstrip().lower()
    if prefix.startswith(b"%pdf-"):
        return DetectedMimeType.PDF
    if prefix.startswith((b"<!doctype html", b"<html")) or b"<html" in prefix:
        return DetectedMimeType.HTML
    raise UnsupportedContentError("Only detected HTML and PDF content is supported")


def _content_type(value: str | None) -> str | None:
    return value.split(";", 1)[0].strip().casefold() if value else None


def _allowed_headers(headers: dict[str, str]) -> dict[str, str]:
    allowed = {
        "content-type",
        "content-length",
        "content-language",
        "content-disposition",
        "last-modified",
        "etag",
        "cache-control",
    }
    return {key.casefold(): value for key, value in headers.items() if key.casefold() in allowed}
