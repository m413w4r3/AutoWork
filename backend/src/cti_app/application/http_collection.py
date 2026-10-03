from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import socket
import time
import zlib
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol
from urllib.parse import SplitResult, urljoin, urlsplit

from cti_app.domain.collection import (
    AttemptOutcome,
    CollectionFailureReason,
    CollectionPolicySnapshot,
    CollectionTransportClassification,
    DetectedMimeType,
)

COLLECTOR_VERSION = "2.0.0"
CancellationCheck = Callable[[], Awaitable[None]]


class CollectionError(RuntimeError):
    outcome = AttemptOutcome.ERROR
    retryable = False
    default_reason_code = CollectionFailureReason.TRANSPORT_ERROR
    default_transport_classification: CollectionTransportClassification | None = None

    def __init__(
        self,
        message: str,
        *,
        reason_code: CollectionFailureReason | None = None,
        transport_classification: CollectionTransportClassification | None = None,
    ) -> None:
        super().__init__(message)
        self.reason_code = reason_code or self.default_reason_code
        self.transport_classification = (
            transport_classification
            if transport_classification is not None
            else self.default_transport_classification
        )
        self.final_url: str | None = None
        self.redirect_chain: tuple[str, ...] = ()
        self.http_status: int | None = None
        self.headers: dict[str, str] = {}
        self.encoded_size: int | None = None
        self.detected_content_type: str | None = None

    def with_context(
        self,
        *,
        final_url: str,
        redirect_chain: Sequence[str],
        http_status: int | None = None,
        headers: dict[str, str] | None = None,
        encoded_size: int | None = None,
        detected_content_type: str | None = None,
    ) -> CollectionError:
        self.final_url = final_url
        self.redirect_chain = tuple(redirect_chain)
        if http_status is not None:
            self.http_status = http_status
        if headers is not None:
            self.headers = _allowed_headers(headers)
        if encoded_size is not None:
            self.encoded_size = encoded_size
        if detected_content_type is not None:
            self.detected_content_type = detected_content_type
        return self


class UnsafeAddressError(CollectionError):
    outcome = AttemptOutcome.BLOCKED
    default_reason_code = CollectionFailureReason.UNSAFE_DESTINATION
    default_transport_classification = CollectionTransportClassification.BLOCKED


class AccessBlockedError(CollectionError):
    outcome = AttemptOutcome.BLOCKED
    default_reason_code = CollectionFailureReason.ACCESS_BLOCKED
    default_transport_classification = CollectionTransportClassification.BLOCKED


class ObsoleteUrlError(CollectionError):
    outcome = AttemptOutcome.UNAVAILABLE
    default_reason_code = CollectionFailureReason.OBSOLETE_URL_404
    default_transport_classification = CollectionTransportClassification.HTTP


class DownloadTooLargeError(CollectionError):
    outcome = AttemptOutcome.TOO_LARGE
    default_reason_code = CollectionFailureReason.SIZE_LIMIT


class DownloadUnavailableError(CollectionError):
    outcome = AttemptOutcome.UNAVAILABLE
    default_reason_code = CollectionFailureReason.HTTP_ERROR
    default_transport_classification = CollectionTransportClassification.HTTP


class DownloadTransientError(CollectionError):
    retryable = True
    default_reason_code = CollectionFailureReason.TRANSPORT_ERROR
    default_transport_classification = CollectionTransportClassification.TRANSPORT


class UnsupportedContentError(CollectionError):
    default_reason_code = CollectionFailureReason.UNSUPPORTED_CONTENT


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

    def snapshot(
        self,
        extraction_limits: dict[str, int | float | str] | None = None,
    ) -> CollectionPolicySnapshot:
        values = {
            "max_redirects": self.max_redirects,
            "timeout_seconds": self.timeout_seconds,
            "max_download_bytes": self.max_download_bytes,
            "max_expanded_bytes": self.max_expanded_bytes,
            "max_decompression_ratio": self.max_decompression_ratio,
            "user_agent": self.user_agent,
            "allowed_domains": sorted(self.allowed_domains),
            "blocked_domains": sorted(self.blocked_domains),
            "collector_version": COLLECTOR_VERSION,
            "extraction_limits": extraction_limits or {},
        }
        canonical = json.dumps(values, sort_keys=True, separators=(",", ":"))
        return CollectionPolicySnapshot(
            id=hashlib.sha256(canonical.encode()).hexdigest(),
            max_redirects=self.max_redirects,
            timeout_seconds=self.timeout_seconds,
            max_download_bytes=self.max_download_bytes,
            max_expanded_bytes=self.max_expanded_bytes,
            max_decompression_ratio=self.max_decompression_ratio,
            user_agent=self.user_agent,
            allowed_domains=tuple(sorted(self.allowed_domains)),
            blocked_domains=tuple(sorted(self.blocked_domains)),
            collector_version=COLLECTOR_VERSION,
            extraction_limits=dict(extraction_limits or {}),
        )

    def snapshot_id(self) -> str:
        return self.snapshot().id


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
    encoded_body: bytes


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
    encoded_body: bytes
    decoded_body: bytes
    encoded_size: int
    encoded_sha256: str
    decoded_size: int
    decoded_sha256: str
    content_encoding: str
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

    async def fetch(
        self,
        requested_url: str,
        *,
        cancellation_check: CancellationCheck | None = None,
        allow_images: bool = False,
    ) -> CollectedResponse:
        current = requested_url.strip()
        redirects: list[str] = []
        last_redirect_response: RawHttpResponse | None = None
        deadline = time.monotonic() + self.policy.timeout_seconds
        for redirect_count in range(self.policy.max_redirects + 1):
            if cancellation_check is not None:
                await cancellation_check()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise DownloadTransientError(
                    "Total collection timeout exceeded",
                    reason_code=CollectionFailureReason.TIMEOUT,
                    transport_classification=CollectionTransportClassification.TIMEOUT,
                ).with_context(final_url=current, redirect_chain=redirects)
            try:
                parsed = _validate_url(current, self.policy)
                approved_ip = await self._resolve_and_pin(
                    parsed.hostname or "", remaining, cancellation_check
                )
            except CollectionError as exc:
                _attach_fetch_error_context(
                    exc, current, redirects, last_redirect_response, self.policy
                )
                raise
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise DownloadTransientError(
                    "Total collection timeout exceeded",
                    reason_code=CollectionFailureReason.TIMEOUT,
                    transport_classification=CollectionTransportClassification.TIMEOUT,
                ).with_context(final_url=current, redirect_chain=redirects)
            try:
                if cancellation_check is not None:
                    await cancellation_check()
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
                _attach_fetch_error_context(
                    exc, current, redirects, last_redirect_response, self.policy
                )
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
                        encoded_size=len(response.encoded_body),
                        detected_content_type=_sniff_response_type(response, self.policy),
                    )
                if redirect_count >= self.policy.max_redirects:
                    raise DownloadUnavailableError("Maximum redirect count exceeded").with_context(
                        final_url=current,
                        redirect_chain=redirects,
                        http_status=response.status,
                        headers=response.headers,
                        encoded_size=len(response.encoded_body),
                        detected_content_type=_sniff_response_type(response, self.policy),
                    )
                last_redirect_response = response
                current = urljoin(current, location)
                redirects.append(current)
                continue
            if response.status in {401, 403}:
                raise AccessBlockedError(
                    f"Remote server denied access with HTTP {response.status}"
                ).with_context(
                    final_url=current,
                    redirect_chain=redirects,
                    http_status=response.status,
                    headers=response.headers,
                    encoded_size=len(response.encoded_body),
                    detected_content_type=_sniff_response_type(response, self.policy),
                )
            if response.status in {404, 410}:
                raise ObsoleteUrlError(
                    f"Remote server returned HTTP {response.status}"
                ).with_context(
                    final_url=current,
                    redirect_chain=redirects,
                    http_status=response.status,
                    headers=response.headers,
                    encoded_size=len(response.encoded_body),
                    detected_content_type=_sniff_response_type(response, self.policy),
                )
            if response.status == 408:
                raise DownloadTransientError(
                    "Remote server timed out with HTTP 408",
                    reason_code=CollectionFailureReason.TIMEOUT,
                    transport_classification=CollectionTransportClassification.TIMEOUT,
                ).with_context(
                    final_url=current,
                    redirect_chain=redirects,
                    http_status=response.status,
                    headers=response.headers,
                    encoded_size=len(response.encoded_body),
                    detected_content_type=_sniff_response_type(response, self.policy),
                )
            if response.status in {425, 429} or response.status >= 500:
                raise DownloadTransientError(
                    f"Remote server returned HTTP {response.status}",
                    reason_code=CollectionFailureReason.HTTP_ERROR,
                    transport_classification=CollectionTransportClassification.HTTP,
                ).with_context(
                    final_url=current,
                    redirect_chain=redirects,
                    http_status=response.status,
                    headers=response.headers,
                    encoded_size=len(response.encoded_body),
                    detected_content_type=_sniff_response_type(response, self.policy),
                )
            if response.status < 200 or response.status >= 300:
                raise DownloadUnavailableError(
                    f"Remote server returned HTTP {response.status}"
                ).with_context(
                    final_url=current,
                    redirect_chain=redirects,
                    http_status=response.status,
                    headers=response.headers,
                    encoded_size=len(response.encoded_body),
                    detected_content_type=_sniff_response_type(response, self.policy),
                )
            try:
                decoded_body, content_encoding = _decode_body(
                    response.encoded_body, response.headers, self.policy
                )
                detected = _detect_mime(decoded_body, allow_images=allow_images)
            except CollectionError as exc:
                exc.with_context(
                    final_url=current,
                    redirect_chain=redirects,
                    http_status=response.status,
                    headers=response.headers,
                    encoded_size=len(response.encoded_body),
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
                encoded_body=response.encoded_body,
                decoded_body=decoded_body,
                encoded_size=len(response.encoded_body),
                encoded_sha256=hashlib.sha256(response.encoded_body).hexdigest(),
                decoded_size=len(decoded_body),
                decoded_sha256=hashlib.sha256(decoded_body).hexdigest(),
                content_encoding=content_encoding,
                acquired_at=datetime.now(UTC),
            )
        raise AssertionError("redirect loop terminates in the loop")

    async def _resolve_and_pin(
        self,
        hostname: str,
        timeout_seconds: float,
        cancellation_check: CancellationCheck | None,
    ) -> str:
        import asyncio

        try:
            async with asyncio.timeout(timeout_seconds):
                if cancellation_check is not None:
                    await cancellation_check()
                first = tuple(dict.fromkeys(await self._resolver.resolve(hostname)))
                if cancellation_check is not None:
                    await cancellation_check()
                second = tuple(dict.fromkeys(await self._resolver.resolve(hostname)))
        except TimeoutError as exc:
            raise DownloadTransientError(
                "DNS resolution exceeded the total timeout",
                reason_code=CollectionFailureReason.TIMEOUT,
                transport_classification=CollectionTransportClassification.TIMEOUT,
            ) from exc
        except OSError as exc:
            raise DownloadTransientError(
                "DNS resolution failed",
                reason_code=CollectionFailureReason.DNS_ERROR,
                transport_classification=CollectionTransportClassification.DNS,
            ) from exc
        if not first or not second:
            raise DownloadTransientError(
                "DNS resolution returned no address",
                reason_code=CollectionFailureReason.DNS_ERROR,
                transport_classification=CollectionTransportClassification.DNS,
            )
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


def _decode_body(
    body: bytes, headers: dict[str, str], policy: CollectionPolicy
) -> tuple[bytes, str]:
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
    return expanded, encoding or "identity"


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


def _detect_mime(body: bytes, *, allow_images: bool = False) -> DetectedMimeType:
    if allow_images:
        image_mime = _sniff_image_mime(body)
        if image_mime is not None:
            return image_mime
    prefix = body[:1024]
    if prefix.startswith(b"\xef\xbb\xbf"):
        prefix = prefix[3:]
    prefix = prefix.lstrip().lower()
    if prefix.startswith(b"%pdf-"):
        return DetectedMimeType.PDF
    if prefix.startswith((b"<!doctype html", b"<html")) or b"<html" in prefix:
        return DetectedMimeType.HTML
    try:
        decoded = body.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise UnsupportedContentError("Detected content type is not supported") from exc
    try:
        parsed = json.loads(decoded)
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, (dict, list)):
        return DetectedMimeType.JSON
    if "\x00" not in decoded:
        return DetectedMimeType.TEXT
    raise UnsupportedContentError("Detected content type is not supported")


def _sniff_image_mime(body: bytes) -> DetectedMimeType | None:
    if body.startswith(b"\x89PNG\r\n\x1a\n"):
        return DetectedMimeType.PNG
    if body.startswith(b"\xff\xd8\xff"):
        return DetectedMimeType.JPEG
    if body.startswith((b"GIF87a", b"GIF89a")):
        return DetectedMimeType.GIF
    if len(body) >= 12 and body[:4] == b"RIFF" and body[8:12] == b"WEBP":
        return DetectedMimeType.WEBP
    prefix = body[:1024].lstrip(b"\xef\xbb\xbf\x00\t\r\n ")
    if re.match(rb"(?:<\?xml[^>]*\?>\s*)?<svg(?:\s|>)", prefix, re.IGNORECASE):
        return DetectedMimeType.SVG
    return None


def _content_type(value: str | None) -> str | None:
    return value.split(";", 1)[0].strip().casefold() if value else None


def _sniff_response_type(response: RawHttpResponse, policy: CollectionPolicy) -> str | None:
    """Best-effort content sniffing for error responses, within collector limits."""
    try:
        decoded, _encoding = _decode_body(response.encoded_body, response.headers, policy)
        return _detect_mime(decoded).value
    except CollectionError:
        return None


def _attach_fetch_error_context(
    error: CollectionError,
    current_url: str,
    redirect_chain: Sequence[str],
    last_redirect_response: RawHttpResponse | None,
    policy: CollectionPolicy,
) -> None:
    if last_redirect_response is None:
        error.with_context(final_url=current_url, redirect_chain=redirect_chain)
        return
    error.with_context(
        final_url=current_url,
        redirect_chain=redirect_chain,
        http_status=last_redirect_response.status,
        headers=last_redirect_response.headers,
        encoded_size=len(last_redirect_response.encoded_body),
        detected_content_type=_sniff_response_type(last_redirect_response, policy),
    )


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
