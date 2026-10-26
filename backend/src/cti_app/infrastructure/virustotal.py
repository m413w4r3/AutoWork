from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import re
import ssl
import struct
import tempfile
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from enum import StrEnum
from typing import Any, BinaryIO, Literal, cast
from urllib.parse import quote, urljoin, urlsplit
from uuid import uuid4

import httpx

from cti_app.application.external_effects import (
    Ambiguous,
    Completed,
    DefinitelyNotSubmitted,
    ExternalEffectCoordinator,
    HandoffGateRefused,
    PermitAwareTransport,
    PhysicalSendCapability,
    Refused,
    Reused,
    TransportResponse,
)
from cti_app.application.sample_acquisition import (
    SampleAcquisitionCacheScope,
    SampleAcquisitionResponseOrigin,
)
from cti_app.application.virustotal import (
    VT_DOWNLOAD_CREDENTIAL_PARAMETER_NAMES,
    FileRelationship,
    VirusTotalCapabilities,
    VirusTotalCapabilityDisabledError,
    VirusTotalConfigurationError,
    VirusTotalDownloadEmptyError,
    VirusTotalDownloadHashMismatchError,
    VirusTotalDownloadHostNotAllowedError,
    VirusTotalDownloadLengthMismatchError,
    VirusTotalDownloadNotBinaryError,
    VirusTotalDownloadRedirectLimitError,
    VirusTotalDownloadResult,
    VirusTotalDownloadTooLargeError,
    VirusTotalDownloadTruncatedError,
    VirusTotalError,
    VirusTotalExternalEffectNeedsReviewError,
    VirusTotalExternalEffectRefusedError,
    VirusTotalFile,
    VirusTotalFileReport,
    VirusTotalHttpError,
    VirusTotalInvalidInputError,
    VirusTotalJsonError,
    VirusTotalNotAuthorizedError,
    VirusTotalObjectReport,
    VirusTotalOperationRoute,
    VirusTotalPage,
    VirusTotalPayloadError,
    VirusTotalPort,
    VirusTotalQuotaExceededError,
    VirusTotalRawResponse,
    VirusTotalRelationNotAllowedError,
    VirusTotalResponseTooLargeError,
    VirusTotalRetrohuntPort,
    VirusTotalRouteStep,
    VirusTotalRouteUnavailableError,
    VirusTotalRoutingPolicy,
    VirusTotalSearchResult,
    VirusTotalSignedUrlExpiredError,
    VirusTotalTransportError,
    VirusTotalUnexpectedRedirectError,
    current_virustotal_external_effect_authority,
    current_virustotal_external_effect_evidence_refs,
    file_hash_family,
    normalize_file_hash,
    record_virustotal_external_effect,
    validate_search_query,
)
from cti_app.application.virustotal_persistence import (
    ArchivedVirusTotalResponse,
    VirusTotalObservationService,
)
from cti_app.config import VIRUSTOTAL_DOWNLOAD_REQUIRED_CDN_HOST, Settings
from cti_app.domain.external_effects import (
    EvidenceReference,
    FrozenWireRequest,
    PreparedExternalRequest,
    normalize_credential_parameter_name,
)
from cti_app.domain.retrohunt import (
    RetrohuntProviderJob,
    RetrohuntProviderResults,
    RetrohuntSubmitRequest,
)
from cti_app.domain.virustotal import (
    VIRUSTOTAL_RELATIONSHIPS_BY_OBJECT_TYPE,
    VirusTotalBehaviourSummary,
    VirusTotalCapability,
    VirusTotalEndpointVariant,
    VirusTotalObjectType,
    VirusTotalOperation,
    VirusTotalRelationship,
    VirusTotalTransportKind,
)

logger = logging.getLogger(__name__)


_MAX_ERROR_BODY_BYTES = 64 * 1024
_VT_RESULT_MAGIC = b"VT-READ-RESULT\x01\0"
_VT_READ_NORMALIZER_VERSION = "vt-read.v1"
_VT_DOWNLOAD_NORMALIZER_VERSION = "vt-download.v1"
_DOWNLOAD_FINGERPRINT_CONFIG_VERSION = "vt-download-transport-v2"
VIRUSTOTAL_PROXY_QUERY_SENTINEL_NAME = "x"
VIRUSTOTAL_PROXY_QUERY_SENTINEL_VALUE = "1"
_PROXY_QUERY_SENTINEL_HOOK_MARKER = "_cti_vt_proxy_query_sentinel_hook"
_PCAP_MAGIC = (
    b"\xd4\xc3\xb2\xa1",
    b"\xa1\xb2\xc3\xd4",
    b"\x4d\x3c\xb2\xa1",
    b"\xa1\xb2\x3c\x4d",
    b"\x0a\x0d\x0d\x0a",
)

VIRUSTOTAL_ADAPTER_SETTINGS_BY_ARGUMENT = {
    "base_url": "virustotal_base_url",
    "fallback_base_url": "virustotal_fallback_base_url",
    "legacy_base_url": "virustotal_legacy_base_url",
    "file_report_proxy_fallback_enabled": "virustotal_file_report_proxy_fallback_enabled",
    "file_report_legacy_fallback_enabled": "virustotal_file_report_legacy_fallback_enabled",
    "max_response_bytes": "virustotal_max_response_bytes",
    "default_page_size": "virustotal_default_page_size",
    "max_page_size": "virustotal_max_page_size",
    "max_pages": "virustotal_max_pages",
    "max_results": "virustotal_max_results",
    "file_download_enabled": "virustotal_file_download_enabled",
    "file_download_legacy_fallback_enabled": "virustotal_file_download_legacy_fallback_enabled",
    "proxy_query_sentinel_enabled": "virustotal_proxy_query_sentinel_enabled",
    "behaviour_pcap_enabled": "virustotal_behaviour_pcap_enabled",
    "download_allowed_hosts": "virustotal_download_allowed_hosts",
    "download_allowed_ports": "virustotal_download_allowed_ports",
    "download_max_redirects": "virustotal_download_max_redirects",
    "download_max_bytes": "virustotal_download_max_bytes",
    "download_timeout_seconds": "virustotal_download_timeout_seconds",
}


def create_virustotal_http_client(settings: Settings) -> httpx.AsyncClient:
    """Create the proxy-only client; its caller owns and closes its lifecycle."""
    proxy, verify, timeout = _virustotal_proxy_options(settings)
    client = httpx.AsyncClient(
        proxy=proxy,
        verify=verify,
        timeout=timeout,
        follow_redirects=False,
        headers={"accept": "application/json"},
        trust_env=False,
    )
    return _configure_proxy_query_sentinel(client, settings.virustotal_proxy_query_sentinel_enabled)


def configure_virustotal_proxy_http_client(
    client: httpx.AsyncClient, settings: Settings
) -> httpx.AsyncClient:
    """Bind a fresh lifecycle-owned HTTPX client to the configured VT proxy.

    The API and worker roots already own a placeholder client for Retrohunt.
    Replacing its unused default transport here preserves their existing close
    lifecycle while giving it exactly the same proxy, TLS, and timeout policy
    as ordinary VirusTotal reads. Injected transports (notably MockTransport)
    remain intact for deterministic tests.
    """
    proxy, verify, timeout = _virustotal_proxy_options(settings)
    if isinstance(client._transport, httpx.AsyncHTTPTransport):
        client._transport = httpx.AsyncHTTPTransport(
            proxy=proxy,
            verify=verify,
            trust_env=False,
        )
    client.timeout = timeout
    client.follow_redirects = False
    client.headers["accept"] = "application/json"
    _configure_proxy_query_sentinel(client, settings.virustotal_proxy_query_sentinel_enabled)
    return client


def _configure_proxy_query_sentinel(client: httpx.AsyncClient, enabled: bool) -> httpx.AsyncClient:
    """Install the proxy-only queryless-request workaround on a shared client."""
    hooks = client.event_hooks.setdefault("request", [])
    hooks[:] = [
        hook for hook in hooks if not getattr(hook, _PROXY_QUERY_SENTINEL_HOOK_MARKER, False)
    ]
    if enabled:

        async def add_proxy_query_sentinel(request: httpx.Request) -> None:
            if not request.url.query:
                request.url = request.url.copy_merge_params(
                    {VIRUSTOTAL_PROXY_QUERY_SENTINEL_NAME: VIRUSTOTAL_PROXY_QUERY_SENTINEL_VALUE}
                )

        setattr(add_proxy_query_sentinel, _PROXY_QUERY_SENTINEL_HOOK_MARKER, True)
        hooks.append(add_proxy_query_sentinel)
    return client


def _virustotal_proxy_options(
    settings: Settings,
) -> tuple[str | httpx.Proxy, bool, httpx.Timeout]:
    proxy_url = settings.virustotal_proxy_url
    if not proxy_url:
        raise VirusTotalConfigurationError("Le proxy VirusTotal n'est pas configuré.")
    proxy: str | httpx.Proxy = proxy_url
    if settings.virustotal_proxy_insecure and proxy_url.startswith("https://"):
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        proxy = httpx.Proxy(url=proxy_url, ssl_context=context)
    timeout = httpx.Timeout(
        settings.virustotal_read_timeout_seconds,
        connect=settings.virustotal_connect_timeout_seconds,
    )
    return proxy, not settings.virustotal_proxy_insecure, timeout


def create_virustotal_direct_http_client(settings: Settings) -> httpx.AsyncClient:
    """Create the optional direct client used only by explicitly authorized fallbacks.

    Built only when a key exists AND at least one direct route is actually
    enabled — the key's mere presence never authorizes building this client.
    """
    if settings.virustotal_api_key is None:
        raise VirusTotalConfigurationError("La clé API directe VirusTotal n'est pas configurée.")
    if not (
        settings.virustotal_file_report_legacy_fallback_enabled
        or settings.virustotal_file_download_legacy_fallback_enabled
    ):
        raise VirusTotalConfigurationError(
            "Aucune route directe VirusTotal n'est activée explicitement."
        )
    timeout = httpx.Timeout(
        settings.virustotal_read_timeout_seconds,
        connect=settings.virustotal_connect_timeout_seconds,
    )
    return httpx.AsyncClient(
        headers={
            "accept": "application/json",
            "x-apikey": settings.virustotal_api_key.get_secret_value(),
        },
        timeout=timeout,
        follow_redirects=False,
        trust_env=False,
    )


@dataclass(frozen=True, slots=True, repr=False)
class _TransientVirusTotalResponse:
    status_code: int
    content_type_class: str
    location_host: str | None
    retry_after: float | None
    issuer: str
    oversized: bool
    body: bytes


class _VirusTotalPermitAwareTransport(PermitAwareTransport):
    """The only gated-read owner of a raw VT HTTPX request/client call."""

    __slots__ = ("__archive", "__client", "__frozen_request", "__max_bytes", "__request")

    def __init__(
        self,
        *,
        client: httpx.AsyncClient,
        request: httpx.Request,
        frozen_request: FrozenWireRequest,
        archive: VirusTotalObservationService,
        max_response_bytes: int,
    ) -> None:
        self.__client = client
        self.__request = request
        self.__frozen_request = frozen_request
        self.__archive = archive
        self.__max_bytes = max_response_bytes

    async def submit_once(
        self, request: FrozenWireRequest, capability: PhysicalSendCapability
    ) -> TransportResponse:
        if request is not self.__frozen_request or _freeze_httpx_request(self.__request) != request:
            raise HandoffGateRefused("prepared VT request changed before physical send")
        capability(request)
        response = await self.__client.send(
            self.__request,
            follow_redirects=False,
            stream=True,
        )
        try:
            status_code = response.status_code
            content_type_class = _content_type_class(response.headers.get("Content-Type"))
            location_host = _url_host(response.headers.get("Location"))
            retry_after = _retry_after(response.headers.get("Retry-After"))
            limit = self.__max_bytes + 1 if status_code < 300 else _MAX_ERROR_BODY_BYTES
            body = await _read_response_prefix(response, limit)
            oversized = status_code < 300 and len(body) > self.__max_bytes
            issuer = _response_issuer(response.headers, body)
            transient = _TransientVirusTotalResponse(
                status_code=status_code,
                content_type_class=content_type_class,
                location_host=location_host,
                retry_after=retry_after,
                issuer=issuer,
                oversized=oversized,
                body=body,
            )
            reference = await self.__archive.archive_external_response(
                body,
                status_code=status_code,
                content_type_class=content_type_class,
                location_host=location_host,
                retry_after=retry_after,
                issuer=issuer,
                oversized=oversized,
            )
            return TransportResponse(
                result_ref=reference.id,
                evidence_refs=(reference,),
                payload=_encode_vt_transient_response(transient),
            )
        finally:
            await response.aclose()


class _SpooledResponseStream(httpx.AsyncByteStream):
    def __init__(self, spool: BinaryIO) -> None:
        self._spool = spool

    async def __aiter__(self) -> AsyncIterator[bytes]:
        while True:
            chunk = await asyncio.to_thread(self._spool.read, 64 * 1024)
            if not chunk:
                break
            yield chunk

    async def aclose(self) -> None:
        self._spool.close()


class _VirusTotalDownloadPermitAwareTransport(PermitAwareTransport):
    """Single physical download request with transient, non-archived response bytes."""

    __slots__ = (
        "__client",
        "__frozen_request",
        "__max_bytes",
        "__pending",
        "__request",
    )

    def __init__(
        self,
        *,
        client: httpx.AsyncClient,
        request: httpx.Request,
        frozen_request: FrozenWireRequest,
        max_response_bytes: int,
    ) -> None:
        self.__client = client
        self.__request = request
        self.__frozen_request = frozen_request
        self.__max_bytes = max_response_bytes
        self.__pending: dict[str, tuple[BinaryIO, int, dict[str, str]]] = {}

    async def submit_once(
        self, request: FrozenWireRequest, capability: PhysicalSendCapability
    ) -> TransportResponse:
        if request is not self.__frozen_request or _freeze_httpx_request(self.__request) != request:
            raise HandoffGateRefused("prepared VT download request changed before physical send")
        capability(request)
        response = await self.__client.send(
            self.__request,
            follow_redirects=False,
            stream=True,
        )
        spool = tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024, mode="w+b")
        try:
            status = response.status_code
            limit = (
                self.__max_bytes + 1
                if 200 <= status < 300
                else _MAX_ERROR_BODY_BYTES
                if status >= 400
                else 0
            )
            size = 0
            if limit:
                async for chunk in response.aiter_bytes():
                    remaining = limit - size
                    if remaining <= 0:
                        break
                    piece = chunk[:remaining]
                    spool.write(piece)
                    size += len(piece)
                    if len(piece) < len(chunk):
                        break
            spool.seek(0)
            safe_headers = {
                name: response.headers[name]
                for name in (
                    "content-type",
                    "content-length",
                    "location",
                    "retry-after",
                    "server",
                    "via",
                    "x-proxy-error",
                )
                if name in response.headers
            }
            token = uuid4().hex
            self.__pending[token] = (cast(BinaryIO, spool), status, safe_headers)
            return TransportResponse(payload=token.encode("ascii"))
        except BaseException:
            spool.close()
            raise
        finally:
            await response.aclose()

    def take(self, token: bytes) -> httpx.Response | None:
        try:
            key = token.decode("ascii")
        except UnicodeDecodeError:
            return None
        item = self.__pending.pop(key, None)
        if item is None:
            return None
        spool, status, headers = item
        return httpx.Response(status, headers=headers, stream=_SpooledResponseStream(spool))

    def close_pending(self) -> None:
        for spool, _status, _headers in self.__pending.values():
            spool.close()
        self.__pending.clear()


def _freeze_httpx_request(request: httpx.Request) -> FrozenWireRequest:
    return FrozenWireRequest(
        method=request.method.encode("ascii"),
        target=request.url.raw_path,
        headers=tuple((bytes(name), bytes(value)) for name, value in request.headers.raw),
        body=request.content,
    )


def _credential_free_query(request: httpx.Request) -> list[list[str]]:
    safe: list[list[str]] = []
    for name, value in request.url.params.multi_items():
        # VT v2 names this credential parameter exactly `apikey`. Keep other
        # query names and values in canonical identity rather than applying a
        # heuristic that could make distinct application requests collide.
        if name.casefold() == "apikey":
            continue
        safe.append([name, value])
    return safe


def _encode_vt_transient_response(response: _TransientVirusTotalResponse) -> bytes:
    metadata = json.dumps(
        {
            "content_type_class": response.content_type_class,
            "issuer": response.issuer,
            "location_host": response.location_host,
            "oversized": response.oversized,
            "retry_after": response.retry_after,
            "status_code": response.status_code,
        },
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    return _VT_RESULT_MAGIC + struct.pack(">I", len(metadata)) + metadata + response.body


def _decode_vt_transient_response(payload: bytes | None) -> _TransientVirusTotalResponse | None:
    if not isinstance(payload, bytes) or not payload.startswith(_VT_RESULT_MAGIC):
        return None
    offset = len(_VT_RESULT_MAGIC)
    if len(payload) < offset + 4:
        return None
    metadata_length = struct.unpack(">I", payload[offset : offset + 4])[0]
    start = offset + 4
    end = start + metadata_length
    if end > len(payload):
        return None
    try:
        metadata = json.loads(payload[start:end].decode("ascii"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(metadata, dict):
        return None
    status_code = metadata.get("status_code")
    content_type_class = metadata.get("content_type_class")
    issuer = metadata.get("issuer")
    location_host = metadata.get("location_host")
    retry_after = metadata.get("retry_after")
    oversized = metadata.get("oversized")
    if (
        isinstance(status_code, bool)
        or not isinstance(status_code, int)
        or not 100 <= status_code <= 599
        or content_type_class not in {"missing", "html", "json", "binary", "text", "other"}
        or issuer not in {"unknown", "proxy-likely", "upstream-vt-likely"}
        or (location_host is not None and not isinstance(location_host, str))
        or (
            retry_after is not None
            and (isinstance(retry_after, bool) or not isinstance(retry_after, int | float))
        )
        or not isinstance(oversized, bool)
    ):
        return None
    return _TransientVirusTotalResponse(
        status_code=status_code,
        content_type_class=content_type_class,
        location_host=location_host,
        retry_after=float(retry_after) if retry_after is not None else None,
        issuer=issuer,
        oversized=oversized,
        body=payload[end:],
    )


def _one_raw_response_reference(
    references: tuple[EvidenceReference, ...],
) -> EvidenceReference | None:
    matches = tuple(reference for reference in references if reference.kind == "vt.raw_response")
    return matches[0] if len(matches) == 1 else None


async def _read_vt_archive(
    archive: VirusTotalObservationService,
    reference: EvidenceReference,
    maximum: int,
) -> ArchivedVirusTotalResponse:
    try:
        return await archive.read_external_response(reference, max_bytes=maximum)
    except Exception:
        raise VirusTotalExternalEffectNeedsReviewError(
            "The VirusTotal response archive is missing or unverifiable."
        ) from None


def _transient_from_archive(archived: ArchivedVirusTotalResponse) -> _TransientVirusTotalResponse:
    return _TransientVirusTotalResponse(
        status_code=archived.status_code,
        content_type_class=archived.content_type_class,
        location_host=archived.location_host,
        retry_after=archived.retry_after,
        issuer=archived.issuer,
        oversized=archived.oversized,
        body=archived.body,
    )


def _httpx_response_from_transient(
    request: httpx.Request, transient: _TransientVirusTotalResponse
) -> httpx.Response:
    content_types = {
        "html": "text/html",
        "json": "application/json",
        "binary": "application/octet-stream",
        "text": "text/plain",
        "other": "application/octet-stream",
    }
    headers: dict[str, str] = {}
    content_type = content_types.get(transient.content_type_class)
    if content_type is not None:
        headers["Content-Type"] = content_type
    if transient.location_host is not None:
        headers["Location"] = f"https://{transient.location_host}/"
    if transient.retry_after is not None:
        headers["Retry-After"] = str(transient.retry_after)
    if transient.issuer == "proxy-likely":
        headers["Via"] = "gated-proxy"
    elif transient.issuer == "upstream-vt-likely":
        headers["Server"] = "VirusTotal"
    return httpx.Response(
        transient.status_code,
        headers=headers,
        content=transient.body,
        request=request,
    )


async def _read_response_prefix(response: httpx.Response, maximum: int) -> bytes:
    chunks: list[bytes] = []
    size = 0
    async for chunk in response.aiter_bytes():
        remaining = maximum - size
        if remaining <= 0:
            break
        chunks.append(chunk[:remaining])
        size += min(len(chunk), remaining)
        if len(chunk) > remaining:
            break
    return b"".join(chunks)


def _virustotal_external_effect_family(
    operation: VirusTotalOperation | None, capability: str | None
) -> str | None:
    if operation is VirusTotalOperation.FILE_DOWNLOAD:
        return None
    if capability == "object_report":
        return "vt.object_report"
    if capability == VirusTotalCapability.BEHAVIOUR_PCAP.value:
        return "vt.behaviour_pcap"
    if operation is None:
        return None
    families = {
        VirusTotalOperation.FILE_REPORT: "vt.file_report",
        VirusTotalOperation.FILE_RELATIONSHIP: "vt.relationships",
        VirusTotalOperation.OBJECT_RELATIONSHIP: "vt.relationships",
        VirusTotalOperation.INTELLIGENCE_SEARCH: "vt.intelligence_search",
        VirusTotalOperation.BEHAVIOURS: "vt.behaviour",
    }
    return families.get(operation)


class VirusTotalHttpAdapter(VirusTotalPort):
    def __init__(
        self,
        *,
        client: httpx.AsyncClient,
        base_url: str,
        capabilities: VirusTotalCapabilities,
        fallback_base_url: str | None = None,
        legacy_base_url: str | None = None,
        direct_client: httpx.AsyncClient | None = None,
        api_key: str | None = None,
        file_report_proxy_fallback_enabled: bool = False,
        file_report_legacy_fallback_enabled: bool = False,
        routing_policy: VirusTotalRoutingPolicy | None = None,
        max_response_bytes: int = 10 * 1024 * 1024,
        default_page_size: int = 40,
        max_page_size: int = 100,
        max_pages: int = 10,
        max_results: int = 1000,
        file_download_enabled: bool = False,
        file_download_legacy_fallback_enabled: bool = False,
        proxy_query_sentinel_enabled: bool = True,
        behaviour_pcap_enabled: bool = False,
        download_allowed_hosts: frozenset[str] = frozenset(),
        download_allowed_ports: frozenset[int] = frozenset({443}),
        download_max_redirects: int = 3,
        download_max_bytes: int = 200 * 1024 * 1024,
        download_timeout_seconds: float = 120.0,
        download_client_factory: Callable[[], httpx.AsyncClient] | None = None,
        offline_probe_mode: bool = False,
    ) -> None:
        self._client = client
        self._external_effect_coordinator: ExternalEffectCoordinator | None = None
        self._raw_response_archive: VirusTotalObservationService | None = None
        self._offline_probe_mode = offline_probe_mode
        self._base_url = _validate_base_url(base_url)
        self._fallback_base_url = (
            _validate_base_url(fallback_base_url) if fallback_base_url else None
        )
        self._legacy_base_url = (
            _validate_legacy_base_url(legacy_base_url) if legacy_base_url else None
        )
        self._direct_client = direct_client
        self._api_key = api_key
        if (direct_client is None) != (api_key is None):
            raise ValueError("direct_client et api_key doivent être fournis ensemble")
        self._capabilities = capabilities
        self._file_download_enabled = file_download_enabled
        self._proxy_query_sentinel_enabled = proxy_query_sentinel_enabled
        self._behaviour_pcap_enabled = behaviour_pcap_enabled
        if routing_policy is None:
            self._routing_policy = _default_routing_policy(
                base_url=self._base_url,
                fallback_base_url=self._fallback_base_url,
                legacy_base_url=self._legacy_base_url,
                direct_client=self._direct_client,
                api_key=self._api_key,
                proxy_fallback_enabled=file_report_proxy_fallback_enabled,
                legacy_fallback_enabled=file_report_legacy_fallback_enabled,
                file_download_enabled=file_download_enabled,
                behaviour_pcap_enabled=behaviour_pcap_enabled,
            )
        elif file_download_enabled:
            self._routing_policy = routing_policy
        else:
            routes = dict(routing_policy.routes)
            routes.pop(VirusTotalCapability.FILE_DOWNLOAD, None)
            if not behaviour_pcap_enabled:
                routes.pop(VirusTotalCapability.BEHAVIOUR_PCAP, None)
            self._routing_policy = VirusTotalRoutingPolicy(routes=routes)
        if routing_policy is not None and file_download_enabled and not behaviour_pcap_enabled:
            routes = dict(self._routing_policy.routes)
            routes.pop(VirusTotalCapability.BEHAVIOUR_PCAP, None)
            self._routing_policy = VirusTotalRoutingPolicy(routes=routes)
        self._max_response_bytes = _positive(max_response_bytes, "max_response_bytes")
        self._default_page_size = _bounded(default_page_size, 1, max_page_size, "default_page_size")
        self._max_page_size = _positive(max_page_size, "max_page_size")
        self._max_pages = _positive(max_pages, "max_pages")
        self._max_results = _positive(max_results, "max_results")
        self._download_allowed_hosts = frozenset(host.lower() for host in download_allowed_hosts)
        self._download_allowed_ports = frozenset(download_allowed_ports)
        self._download_max_redirects = _bounded(
            download_max_redirects, 0, 10, "download_max_redirects"
        )
        self._download_max_bytes = _positive(download_max_bytes, "download_max_bytes")
        self._download_timeout_seconds = _positive_float(
            download_timeout_seconds, "download_timeout_seconds"
        )
        # Tests inject a mock-transport factory here; production never
        # overrides it, so the real download always uses a bare,
        # headerless, non-redirecting client — never the proxy or direct
        # client, which may carry credentials.
        self._download_client_factory = download_client_factory
        self._file_download_legacy_fallback_enabled = file_download_legacy_fallback_enabled
        self.acquisition_route_fingerprint = _download_route_fingerprint(
            base_url=self._base_url,
            routing_policy=self._routing_policy,
            file_download_enabled=file_download_enabled,
            file_download_legacy_fallback_enabled=file_download_legacy_fallback_enabled,
            allowed_hosts=download_allowed_hosts,
            allowed_ports=download_allowed_ports,
            max_redirects=self._download_max_redirects,
            max_bytes=self._download_max_bytes,
            timeout_seconds=self._download_timeout_seconds,
            proxy_query_sentinel_enabled=self._proxy_query_sentinel_enabled,
        )
        download_route = self._routing_policy.route_for(VirusTotalCapability.FILE_DOWNLOAD)
        route_steps = (
            []
            if download_route is None
            else [
                {"transport": step.transport.value, "variant": step.variant.value}
                for step in download_route.steps()
            ]
        )
        self._sample_acquisition_route_fingerprint = _fingerprint_json(
            {
                "route": route_steps,
                "path": "/files/{hash}/download",
                "proxy_query_sentinel_enabled": self._proxy_query_sentinel_enabled,
            }
        )
        self._sample_acquisition_configuration_fingerprint = _fingerprint_json(
            {
                "version": _DOWNLOAD_FINGERPRINT_CONFIG_VERSION,
                "base_host": (urlsplit(self._base_url).hostname or "").lower(),
                "fallback_base_host": (
                    (urlsplit(self._fallback_base_url).hostname or "").lower()
                    if self._fallback_base_url
                    else None
                ),
                "legacy_base_host": (
                    (urlsplit(self._legacy_base_url).hostname or "").lower()
                    if self._legacy_base_url
                    else None
                ),
                "enabled": file_download_enabled,
                "legacy_fallback_enabled": file_download_legacy_fallback_enabled,
                "proxy_query_sentinel_enabled": self._proxy_query_sentinel_enabled,
                "allowed_hosts": sorted(self._download_allowed_hosts),
                "allowed_ports": sorted(self._download_allowed_ports),
                "max_redirects": self._download_max_redirects,
                "max_bytes": self._download_max_bytes,
                "timeout_seconds": self._download_timeout_seconds,
            }
        )
        account_identity = (
            hashlib.sha256(self._api_key.encode("utf-8")).hexdigest()
            if self._api_key
            else "proxy-account-unattributed"
        )
        self._sample_acquisition_account_capability_fingerprint = _fingerprint_json(
            {
                "account_identity": account_identity,
                "capabilities": sorted(
                    capability.value
                    for capability in VirusTotalCapability
                    if self._capabilities.is_enabled(capability)
                ),
            }
        )
        self._sample_acquisition_transport = (
            "+".join(
                sorted(
                    {
                        f"{step.transport.value}-{step.variant.value}"
                        for step in download_route.steps()
                    }
                )
            )
            if download_route is not None
            else "unconfigured"
        )

    def configure_external_effects(
        self,
        coordinator: ExternalEffectCoordinator,
        raw_response_archive: VirusTotalObservationService,
    ) -> None:
        """Bind the investigation-only coordinator and canonical raw response archive."""
        self._external_effect_coordinator = coordinator
        self._raw_response_archive = raw_response_archive

    def maximum_file_report_requests(self) -> int:
        """Return the maximum physical sends allowed by the configured file route."""
        route = self._routing_policy.route_for(VirusTotalCapability.FILE_REPORT)
        return len(route.steps()) if route is not None else 1

    @property
    def sample_acquisition_cache_scope(self) -> SampleAcquisitionCacheScope:
        """Stable, non-secret refusal-cache scope for the configured download route.

        The provider account component is a one-way credential fingerprint when
        a direct key exists; proxy-owned account identity is explicitly marked
        unattributed. Route and configuration are separate opaque fingerprints.
        Neither this scope nor ``acquisition_route_fingerprint`` contains query
        strings, credentials, or signed URLs.
        """
        return SampleAcquisitionCacheScope(
            provider="virustotal",
            account_capability_fingerprint=(
                self._sample_acquisition_account_capability_fingerprint
            ),
            route_fingerprint=self._sample_acquisition_route_fingerprint,
            transport=self._sample_acquisition_transport,
            configuration_fingerprint=self._sample_acquisition_configuration_fingerprint,
        )

    async def file_report(self, file_hash: str) -> VirusTotalFileReport:
        self._require(VirusTotalCapability.FILE_REPORT)
        normalized = normalize_file_hash(file_hash)
        route = self._resolve_route(VirusTotalCapability.FILE_REPORT)
        response, step = await self._execute_route(
            route, lambda step: self._file_report_request(step, normalized)
        )
        payload = _json_object(response.body)
        file = (
            _parse_v2_file(payload, normalized)
            if _is_v2_file_report(payload)
            else _parse_file(payload)
        )
        return VirusTotalFileReport(
            file=file,
            raw_json=response.body,
            http_status=response.status_code,
            transport=step.transport,
            api_generation=step.variant,
        )

    async def file_download(self, file_hash: str, *, sink: BinaryIO) -> VirusTotalDownloadResult:
        """Download and verify bytes using the allowlisted route contract.

        Redirects are followed manually with a fresh credential-free client and
        the public ``acquisition_route_fingerprint`` records the normalized
        route/configuration scope (config version, base host, route, limits,
        allowlists, and fallback flags). ``sample_acquisition_cache_scope``
        exposes separate non-secret account/capability, route, transport, and
        configuration fingerprints for durable refusal scoping. Download
        errors carry ``response_origin`` as Lot C's
        ``SampleAcquisitionResponseOrigin``: PROVIDER/PROXY are heuristic
        attributions based on VT JSON/error and proxy headers, SIGNED_CDN marks
        that separate hop, and UNKNOWN is not cacheable as a durable refusal.
        The issuer heuristic is diagnostic and does not prove authorship. Error
        metadata also carries a correlation id, the historical route label, and
        a normalized ``route_path`` without query parameters. A 404
        is diagnostic metadata, never evidence
        that the file is absent; v2 is considered only after explicit proxy
        route-missing evidence for both v3 endpoints and the opt-in flag.
        """
        correlation_id = uuid4().hex
        fingerprint = self.acquisition_route_fingerprint
        if (
            self._file_download_enabled
            and self._routing_policy.route_for(VirusTotalCapability.FILE_DOWNLOAD) is None
        ):
            raise VirusTotalRouteUnavailableError(
                "La route VT v3 de téléchargement n'est pas configurée.",
                route="proxy:v3",
                route_path="/files/{hash}/download",
                transport="proxy-v3",
                correlation_id=correlation_id,
                fingerprint=fingerprint,
                response_origin=SampleAcquisitionResponseOrigin.UNKNOWN,
            )
        try:
            self._require(VirusTotalCapability.FILE_DOWNLOAD)
            normalized = normalize_file_hash(file_hash)
            family = file_hash_family(normalized)
            route = self._resolve_route(VirusTotalCapability.FILE_DOWNLOAD)
        except VirusTotalError as caught_error:
            _annotate_error(
                caught_error,
                transport="proxy-v3",
                route="proxy:v3",
                route_path="/files/{hash}/download",
                correlation_id=correlation_id,
                fingerprint=fingerprint,
                response_origin=SampleAcquisitionResponseOrigin.UNKNOWN,
            )
            raise

        async def direct_download(step: VirusTotalRouteStep) -> VirusTotalDownloadResult:
            client, base_url = self._resolve_transport(step)
            return await _stream_route_download(
                client,
                f"{base_url}/files/{normalized}/download",
                sink=sink,
                expected_hash=normalized,
                expected_family=family,
                max_bytes=self._download_max_bytes,
                allowed_hosts=self._download_allowed_hosts,
                allowed_ports=self._download_allowed_ports,
                max_redirects=self._download_max_redirects,
                download_client_factory=self._download_client,
                proxy_query_sentinel_enabled=self._proxy_query_sentinel_enabled,
                operation=VirusTotalOperation.FILE_DOWNLOAD,
                route=_route_name(step),
                capability=VirusTotalCapability.FILE_DOWNLOAD.value,
                correlation_id=correlation_id,
                fingerprint=fingerprint,
                fallback_decision_on_404="request_download_url",
                secret_values=(self._api_key,) if self._api_key else (),
                coordinated_request=self._coordinated_download_response,
                acquisition_hash=normalized,
            )

        direct_error: VirusTotalHttpError | None = None
        try:
            async with asyncio.timeout(self._download_timeout_seconds):
                try:
                    direct_result, _ = await self._execute_route(route, direct_download)
                    return direct_result
                except VirusTotalHttpError as direct_caught_error:
                    if direct_caught_error.status_code != 404:
                        raise
                    direct_error = direct_caught_error

                async def download_ticket(step: VirusTotalRouteStep) -> VirusTotalRawResponse:
                    client, base_url = self._resolve_transport(step)
                    return await self._get(
                        f"/files/{normalized}/download_url",
                        client=client,
                        base_url=base_url,
                        operation=VirusTotalOperation.FILE_DOWNLOAD,
                        route=_route_name(step),
                        capability=VirusTotalCapability.FILE_DOWNLOAD.value,
                        correlation_id=correlation_id,
                        fingerprint=fingerprint,
                        fallback_decision="direct-v2-if-both-route-missing-404s",
                        secret_values=(self._api_key,) if self._api_key else (),
                        coordinated_download=True,
                        acquisition_hash=normalized,
                    )

                try:
                    ticket, step = await self._execute_route(route, download_ticket)
                except VirusTotalHttpError as ticket_caught_error:
                    if (
                        ticket_caught_error.status_code == 404
                        and direct_error is not None
                        and direct_error.route_missing
                        and ticket_caught_error.route_missing
                        and self._file_download_legacy_fallback_enabled
                    ):
                        if (
                            self._direct_client is None
                            or self._api_key is None
                            or not self._legacy_base_url
                        ):
                            ticket_caught_error.fallback_decision = (
                                "direct-v2-unavailable-local-configuration"
                            )
                            _log_vt_exchange(
                                correlation_id=correlation_id,
                                capability=VirusTotalCapability.FILE_DOWNLOAD.value,
                                route="direct:legacy_v2",
                                transport="direct-v2",
                                status_code=None,
                                content_type_class=None,
                                location_host=None,
                                bytes_read=0,
                                error_code="virustotal_route_unavailable",
                                fallback_decision=ticket_caught_error.fallback_decision,
                                fingerprint=fingerprint,
                            )
                            raise VirusTotalRouteUnavailableError(
                                "Le repli v2 est activé mais sa route directe est incomplète.",
                                route="direct:legacy_v2",
                                route_path="/file/download",
                                status_code=404,
                                transport="direct-v2",
                                bytes_read=0,
                                correlation_id=correlation_id,
                                fingerprint=fingerprint,
                                fallback_decision=ticket_caught_error.fallback_decision,
                            ) from None
                        _log_vt_exchange(
                            correlation_id=correlation_id,
                            capability=VirusTotalCapability.FILE_DOWNLOAD.value,
                            route="/file/download",
                            transport="direct-v2",
                            status_code=None,
                            content_type_class=None,
                            location_host=None,
                            bytes_read=0,
                            error_code=None,
                            fallback_decision="attempt_direct_v2_after_two_route_missing_404s",
                            fingerprint=fingerprint,
                        )
                        return await _stream_route_download(
                            self._direct_client,
                            f"{self._legacy_base_url}/file/download",
                            params={"apikey": self._api_key, "hash": normalized},
                            sink=sink,
                            expected_hash=normalized,
                            expected_family=family,
                            max_bytes=self._download_max_bytes,
                            allowed_hosts=self._download_allowed_hosts,
                            allowed_ports=self._download_allowed_ports,
                            max_redirects=self._download_max_redirects,
                            download_client_factory=self._download_client,
                            proxy_query_sentinel_enabled=False,
                            operation=VirusTotalOperation.FILE_DOWNLOAD,
                            route="direct:legacy_v2",
                            capability=VirusTotalCapability.FILE_DOWNLOAD.value,
                            correlation_id=correlation_id,
                            fingerprint=fingerprint,
                            fallback_decision_on_404="none",
                            secret_values=(self._api_key,),
                            coordinated_request=self._coordinated_download_response,
                            acquisition_hash=normalized,
                        )
                    ticket_caught_error.fallback_decision = (
                        "no_v2_fallback_unattributed_or_not_route_missing"
                    )
                    raise

                try:
                    payload = _json_object(ticket.body)
                except VirusTotalError as caught_error:
                    _annotate_error(
                        caught_error,
                        transport=_transport_name(step),
                        route=_route_name(step),
                        route_path="/files/{hash}/download_url",
                        correlation_id=correlation_id,
                        fingerprint=fingerprint,
                        bytes_read=len(ticket.body),
                        content_type_class="json",
                    )
                    raise
                signed_url = payload.get("data")
                if not isinstance(signed_url, str) or not signed_url:
                    error = VirusTotalPayloadError(
                        "VirusTotal n'a pas renvoyé d'URL de téléchargement signée."
                    )
                    _annotate_error(
                        error,
                        transport=_transport_name(step),
                        route=_route_name(step),
                        route_path="/files/{hash}/download_url",
                        correlation_id=correlation_id,
                        fingerprint=fingerprint,
                        bytes_read=len(ticket.body),
                        content_type_class="json",
                    )
                    _log_vt_exchange(
                        correlation_id=correlation_id,
                        capability=VirusTotalCapability.FILE_DOWNLOAD.value,
                        route="/files/{hash}/download_url",
                        transport=_transport_name(step),
                        status_code=ticket.status_code,
                        content_type_class="json",
                        location_host=None,
                        bytes_read=len(ticket.body),
                        error_code=error.code,
                        fallback_decision="signed_ticket_invalid",
                        fingerprint=fingerprint,
                    )
                    raise error
                try:
                    _validate_signed_download_url(
                        signed_url,
                        self._download_allowed_hosts,
                        self._download_allowed_ports,
                    )
                except VirusTotalError as caught_error:
                    _annotate_error(
                        caught_error,
                        transport="signed-cdn",
                        route=_route_name(step),
                        route_path="/files/{hash}/download",
                        location_host=_url_host(signed_url),
                        correlation_id=correlation_id,
                        fingerprint=fingerprint,
                        bytes_read=0,
                    )
                    _log_vt_exchange(
                        correlation_id=correlation_id,
                        capability=VirusTotalCapability.FILE_DOWNLOAD.value,
                        route="/files/{hash}/download",
                        transport="signed-cdn",
                        status_code=None,
                        content_type_class=None,
                        location_host=_url_host(signed_url),
                        bytes_read=0,
                        error_code=caught_error.code,
                        fallback_decision="signed_ticket_host_rejected",
                        fingerprint=fingerprint,
                    )
                    raise
                return await _stream_signed_download(
                    signed_url,
                    sink=sink,
                    expected_hash=normalized,
                    expected_family=family,
                    max_bytes=self._download_max_bytes,
                    allowed_hosts=self._download_allowed_hosts,
                    allowed_ports=self._download_allowed_ports,
                    max_redirects=self._download_max_redirects,
                    client_factory=self._download_client,
                    operation=VirusTotalOperation.FILE_DOWNLOAD,
                    route="signed-cdn",
                    capability=VirusTotalCapability.FILE_DOWNLOAD.value,
                    correlation_id=correlation_id,
                    fingerprint=fingerprint,
                    secret_values=(self._api_key,) if self._api_key else (),
                    coordinated_request=self._coordinated_download_response,
                    acquisition_hash=normalized,
                )
        except TimeoutError:
            timeout_error = VirusTotalTransportError(
                "Le téléchargement VirusTotal a expiré.",
                code="virustotal_download_timeout",
                retryable=True,
                operation=VirusTotalOperation.FILE_DOWNLOAD,
                route="/files/{hash}/download",
                transport="proxy-v3",
                correlation_id=correlation_id,
                fingerprint=fingerprint,
                response_origin=SampleAcquisitionResponseOrigin.UNKNOWN,
            )
            _log_vt_exchange(
                correlation_id=correlation_id,
                capability=VirusTotalCapability.FILE_DOWNLOAD.value,
                route="/files/{hash}/download",
                transport="proxy-v3",
                status_code=None,
                content_type_class=None,
                location_host=None,
                bytes_read=0,
                error_code=timeout_error.code,
                fallback_decision="none",
                fingerprint=fingerprint,
            )
            raise timeout_error from None
        except VirusTotalError as caught_error:
            _annotate_error(
                caught_error,
                correlation_id=correlation_id,
                fingerprint=fingerprint,
                route=caught_error.route or "proxy:v3",
                route_path=caught_error.route_path or "/files/{hash}/download",
                response_origin=SampleAcquisitionResponseOrigin.UNKNOWN,
            )
            raise

    async def behaviour_pcap(
        self, sandbox_id: str, *, sink: BinaryIO, max_bytes: int | None = None
    ) -> VirusTotalDownloadResult:
        correlation_id = uuid4().hex
        fingerprint = self.acquisition_route_fingerprint
        if (
            self._behaviour_pcap_enabled
            and self._routing_policy.route_for(VirusTotalCapability.BEHAVIOUR_PCAP) is None
        ):
            raise VirusTotalRouteUnavailableError(
                "La route VT v3 de PCAP sandbox n'est pas configurée.",
                route="proxy:v3",
                route_path="/file_behaviours/{sandbox_id}/pcap",
                transport="proxy-v3",
                correlation_id=correlation_id,
                fingerprint=fingerprint,
                response_origin=SampleAcquisitionResponseOrigin.UNKNOWN,
            )
        try:
            self._require(VirusTotalCapability.BEHAVIOUR_PCAP)
            if not isinstance(sandbox_id, str) or not sandbox_id.strip():
                raise VirusTotalInvalidInputError("L'identifiant sandbox VirusTotal est vide.")
            route = self._resolve_route(VirusTotalCapability.BEHAVIOUR_PCAP)
        except VirusTotalError as caught_error:
            _annotate_error(
                caught_error,
                transport="proxy-v3",
                route="proxy:v3",
                route_path="/file_behaviours/{sandbox_id}/pcap",
                correlation_id=correlation_id,
                fingerprint=fingerprint,
                response_origin=SampleAcquisitionResponseOrigin.UNKNOWN,
            )
            raise
        step = route.primary
        client, base_url = self._resolve_transport(step)
        normalized_id = quote(sandbox_id.strip(), safe="")
        capture_limit = (
            self._download_max_bytes
            if max_bytes is None
            else min(self._download_max_bytes, _positive(max_bytes, "max_bytes"))
        )
        try:
            async with asyncio.timeout(self._download_timeout_seconds):
                response = await self._get(
                    f"/file_behaviours/{normalized_id}/pcap",
                    client=client,
                    base_url=base_url,
                    route=_route_name(step),
                    capability=VirusTotalCapability.BEHAVIOUR_PCAP.value,
                    correlation_id=correlation_id,
                    fingerprint=fingerprint,
                    secret_values=(self._api_key,) if self._api_key else (),
                    response_max_bytes=capture_limit,
                )
        except VirusTotalResponseTooLargeError as caught_error:
            raise VirusTotalDownloadTooLargeError(
                "Le PCAP VirusTotal dépasse la limite configurée.",
                route=_route_name(step),
                route_path="/file_behaviours/{sandbox_id}/pcap",
                transport=_transport_name(step),
                bytes_read=capture_limit + 1,
                correlation_id=correlation_id,
                fingerprint=fingerprint,
            ) from caught_error
        except TimeoutError:
            raise VirusTotalTransportError(
                "Le téléchargement PCAP VirusTotal a expiré.",
                code="virustotal_download_timeout",
                retryable=True,
                route=_route_name(step),
                route_path="/file_behaviours/{sandbox_id}/pcap",
                transport=_transport_name(step),
                correlation_id=correlation_id,
                fingerprint=fingerprint,
                response_origin=SampleAcquisitionResponseOrigin.UNKNOWN,
            ) from None
        except VirusTotalError as caught_error:
            _annotate_error(
                caught_error,
                correlation_id=correlation_id,
                fingerprint=fingerprint,
                route=caught_error.route or "proxy:v3",
                route_path=(caught_error.route_path or "/file_behaviours/{sandbox_id}/pcap"),
                response_origin=SampleAcquisitionResponseOrigin.UNKNOWN,
            )
            raise

        if response.status_code == 204 or not response.body:
            raise VirusTotalDownloadEmptyError(
                "La route de téléchargement VirusTotal a renvoyé un contenu vide.",
                status_code=response.status_code,
                route=_route_name(step),
                transport=_transport_name(step),
                content_type_class=response.content_type_class,
                correlation_id=correlation_id,
                fingerprint=fingerprint,
            )
        content_type_class = response.content_type_class or "missing"
        if _sniff_binary_error(response.body[:16], content_type_class) is not None:
            raise VirusTotalDownloadNotBinaryError(
                "La route PCAP VirusTotal a renvoyé une page HTML ou JSON.",
                status_code=response.status_code,
                route=_route_name(step),
                transport=_transport_name(step),
                content_type_class=content_type_class,
                bytes_read=len(response.body),
                correlation_id=correlation_id,
                fingerprint=fingerprint,
            )
        if not any(response.body.startswith(magic) for magic in _PCAP_MAGIC):
            raise VirusTotalPayloadError(
                "Le contenu reçu n'a pas une signature PCAP ou PCAPNG reconnue.",
                code="virustotal_pcap_magic_invalid",
                status_code=response.status_code,
                route=_route_name(step),
                transport=_transport_name(step),
                content_type_class=content_type_class,
                bytes_read=len(response.body),
                correlation_id=correlation_id,
                fingerprint=fingerprint,
            )
        content = response.body
        sink.write(content)
        return VirusTotalDownloadResult(
            md5=hashlib.md5(content, usedforsecurity=False).hexdigest(),
            sha1=hashlib.sha1(content, usedforsecurity=False).hexdigest(),
            sha256=hashlib.sha256(content).hexdigest(),
            size=len(content),
        )

    async def object_report(
        self,
        kind: Literal["ip_addresses", "domains", "urls"] | str,
        object_id: str,
    ) -> VirusTotalObjectReport:
        if kind not in {"ip_addresses", "domains", "urls"}:
            raise VirusTotalInvalidInputError("Le type de rapport VirusTotal est invalide.")
        if not isinstance(object_id, str) or not object_id.strip():
            raise VirusTotalInvalidInputError("L'identifiant du rapport VirusTotal est vide.")
        self._require(VirusTotalCapability.FILE_REPORT)
        configured_route = self._resolve_route(VirusTotalCapability.FILE_REPORT)
        if configured_route.primary.transport is not VirusTotalTransportKind.PROXY or (
            configured_route.primary.variant is not VirusTotalEndpointVariant.V3
        ):
            raise VirusTotalRouteUnavailableError(
                "Les rapports d'objet exigent une route proxy VT v3.",
                route=f"/{kind}/{{id}}",
            )
        if kind == "urls":
            normalized_id = (
                base64.urlsafe_b64encode(object_id.strip().encode("utf-8"))
                .decode("ascii")
                .rstrip("=")
            )
        else:
            normalized_id = object_id.strip()
        safe_id = quote(normalized_id, safe="")
        path = f"/{kind}/{safe_id}"
        route_name = _route_name(configured_route.primary)
        correlation_id = uuid4().hex
        fingerprint = self.acquisition_route_fingerprint
        projections = {
            "ip_addresses": (
                "comments,communicating_files,downloaded_files,graphs,historical_ssl_certificates,"
                "historical_whois,related_comments,related_references,related_threat_actors,"
                "referrer_files,resolutions,urls"
            ),
            "domains": (
                "caa_records,cname_records,comments,communicating_files,downloaded_files,graphs,"
                "historical_ssl_certificates,historical_whois,immediate_parent,mx_records,ns_records,"
                "parent,referrer_files,related_comments,related_references,related_threat_actors,"
                "resolutions,soa_records,siblings,subdomains,urls,user_votes"
            ),
            "urls": (
                "comments,communicating_files,downloaded_files,graphs,related_comments,"
                "related_references,related_threat_actors"
            ),
        }
        projection = projections.get(kind)
        client, base_url = self._resolve_transport(configured_route.primary)
        try:
            try:
                response = await self._get(
                    path,
                    params={"relationships": projection} if projection else None,
                    client=client,
                    base_url=base_url,
                    route=route_name,
                    capability="object_report",
                    correlation_id=correlation_id,
                    fingerprint=fingerprint,
                    fallback_decision="retry_without_relationships_on_400_403",
                )
            except VirusTotalHttpError as error:
                if projection is None or error.status_code not in {400, 403}:
                    raise
                response = await self._get(
                    path,
                    client=client,
                    base_url=base_url,
                    route=route_name,
                    capability="object_report",
                    correlation_id=correlation_id,
                    fingerprint=fingerprint,
                    fallback_decision="retry_without_relationships",
                )
            payload = _json_object(response.body)
            data = payload.get("data")
            expected_type = {"ip_addresses": "ip_address", "domains": "domain", "urls": "url"}[kind]
            if (
                not isinstance(data, dict)
                or data.get("type") != expected_type
                or not isinstance(data.get("id"), str)
                or not isinstance(data.get("attributes"), dict)
            ):
                raise VirusTotalPayloadError("Le schéma du rapport objet VirusTotal est invalide.")
            return VirusTotalObjectReport(
                kind=kind,  # type: ignore[arg-type]
                object_id=data["id"],
                raw_json=response.body,
                http_status=response.status_code,
                transport=configured_route.primary.transport,
                api_generation=configured_route.primary.variant,
            )
        except VirusTotalError as caught_error:
            _annotate_error(
                caught_error,
                transport="proxy-v3",
                route=_normalize_route(path),
                correlation_id=correlation_id,
                fingerprint=fingerprint,
            )
            raise

    def _download_client(self) -> httpx.AsyncClient:
        if self._download_client_factory is not None:
            return self._download_client_factory()
        timeout = httpx.Timeout(
            self._download_timeout_seconds, connect=self._download_timeout_seconds
        )
        return httpx.AsyncClient(follow_redirects=False, timeout=timeout, trust_env=False)

    async def _file_report_request(
        self, step: VirusTotalRouteStep, file_hash: str
    ) -> VirusTotalRawResponse:
        client, base_url = self._resolve_transport(step)
        if step.variant is VirusTotalEndpointVariant.LEGACY_V2:
            # VT v2 defines `apikey` as a query argument; this compatibility route
            # cannot move it to the v3 x-apikey header. configure_logging redacts it.
            return await self._get(
                "/file/report",
                client=client,
                base_url=base_url,
                params={"resource": file_hash, "apikey": self._api_key},
                operation=VirusTotalOperation.FILE_REPORT,
                route=_route_name(step),
            )
        return await self._get(
            f"/files/{file_hash}",
            client=client,
            base_url=base_url,
            operation=VirusTotalOperation.FILE_REPORT,
            route=_route_name(step),
        )

    async def _execute_route[ResultT](
        self,
        route: VirusTotalOperationRoute,
        request: Callable[[VirusTotalRouteStep], Awaitable[ResultT]],
    ) -> tuple[ResultT, VirusTotalRouteStep]:
        steps = route.steps()
        for index, step in enumerate(steps):
            is_last = index == len(steps) - 1
            try:
                return await request(step), step
            except VirusTotalError as error:
                if is_last or not route.permits_fallback(error):
                    raise
        raise VirusTotalRouteUnavailableError(
            "Aucune route VirusTotal utilisable n'est configurée."
        )

    def _resolve_transport(self, step: VirusTotalRouteStep) -> tuple[httpx.AsyncClient, str]:
        if step.transport is VirusTotalTransportKind.PROXY:
            if step.variant is VirusTotalEndpointVariant.V3_FALLBACK:
                if self._fallback_base_url is None:
                    raise VirusTotalRouteUnavailableError(
                        "La route proxy de secours VirusTotal n'est pas configurée."
                    )
                return self._client, self._fallback_base_url
            return self._client, self._base_url
        if self._direct_client is None or self._legacy_base_url is None or self._api_key is None:
            raise VirusTotalRouteUnavailableError(
                "La route directe VirusTotal n'est pas configurée."
            )
        return self._direct_client, self._legacy_base_url

    def _resolve_route(self, capability: VirusTotalCapability) -> VirusTotalOperationRoute:
        route = self._routing_policy.route_for(capability)
        if route is None:
            raise VirusTotalRouteUnavailableError(
                "Aucune route VirusTotal n'est configurée pour cette opération."
            )
        if capability is not VirusTotalCapability.FILE_REPORT and any(
            step.transport is VirusTotalTransportKind.DIRECT for step in route.steps()
        ):
            raise VirusTotalRouteUnavailableError(
                "Les opérations VirusTotal hors rapport fichier doivent utiliser le proxy."
            )
        return route

    async def file_relationship(
        self,
        file_hash: str,
        relation: FileRelationship,
        *,
        limit: int | None = None,
        cursor: str | None = None,
        paginate: bool = False,
    ) -> VirusTotalPage:
        self._require(VirusTotalCapability.FILE_RELATIONSHIPS)
        self._resolve_route(VirusTotalCapability.FILE_RELATIONSHIPS)
        normalized = normalize_file_hash(file_hash)
        try:
            resolved_relation = FileRelationship(relation)
        except (TypeError, ValueError) as exc:
            raise VirusTotalRelationNotAllowedError(
                "La relation fichier n'est pas autorisée."
            ) from exc
        return await self._paginate(
            f"/files/{normalized}/{resolved_relation.value}",
            limit=limit,
            cursor=cursor,
            paginate=paginate,
            operation=VirusTotalOperation.FILE_RELATIONSHIP,
        )

    async def object_relationship(
        self,
        object_type: VirusTotalObjectType,
        object_id: str,
        relation: VirusTotalRelationship | str,
        *,
        limit: int | None = None,
        cursor: str | None = None,
        paginate: bool = False,
    ) -> VirusTotalPage:
        self._require(VirusTotalCapability.FILE_RELATIONSHIPS)
        self._resolve_route(VirusTotalCapability.FILE_RELATIONSHIPS)
        try:
            resolved_type = VirusTotalObjectType(object_type)
        except (TypeError, ValueError) as exc:
            raise VirusTotalInvalidInputError("Le type d'objet VirusTotal est invalide.") from exc
        relation_value = relation.value if isinstance(relation, StrEnum) else relation
        if (
            not isinstance(relation_value, str)
            or relation_value not in VIRUSTOTAL_RELATIONSHIPS_BY_OBJECT_TYPE[resolved_type]
        ):
            raise VirusTotalRelationNotAllowedError(
                "La relation demandée n'est pas autorisée pour ce type d'objet."
            )
        if not isinstance(object_id, str) or not object_id.strip():
            raise VirusTotalInvalidInputError("L'identifiant d'objet VirusTotal est vide.")
        if resolved_type is VirusTotalObjectType.FILE:
            normalized_id = normalize_file_hash(object_id)
        elif resolved_type is VirusTotalObjectType.URL:
            normalized_id = base64.urlsafe_b64encode(object_id.encode("utf-8")).decode("ascii")
            normalized_id = normalized_id.rstrip("=")
        else:
            normalized_id = object_id.strip()
        collection = {
            VirusTotalObjectType.FILE: "files",
            VirusTotalObjectType.IP_ADDRESS: "ip_addresses",
            VirusTotalObjectType.DOMAIN: "domains",
            VirusTotalObjectType.URL: "urls",
        }[resolved_type]
        safe_id = (
            normalized_id
            if resolved_type is VirusTotalObjectType.URL
            else quote(normalized_id, safe="")
        )
        return await self._paginate(
            f"/{collection}/{safe_id}/{relation_value}",
            limit=limit,
            cursor=cursor,
            paginate=paginate,
            operation=VirusTotalOperation.OBJECT_RELATIONSHIP,
        )

    async def behaviours(
        self,
        file_hash: str,
        *,
        limit: int | None = None,
        cursor: str | None = None,
        paginate: bool = False,
    ) -> VirusTotalPage:
        self._require(VirusTotalCapability.BEHAVIOUR_REPORTS)
        self._resolve_route(VirusTotalCapability.BEHAVIOUR_REPORTS)
        normalized = normalize_file_hash(file_hash)
        page = await self._paginate(
            f"/files/{normalized}/behaviours",
            limit=10 if limit is None else limit,
            cursor=cursor,
            paginate=paginate,
            operation=VirusTotalOperation.BEHAVIOURS,
        )
        return VirusTotalPage(
            items=page.items,
            next_cursor=page.next_cursor,
            observed_count=page.observed_count,
            stopped_due_to_limit=page.stopped_due_to_limit,
            exhaustive=page.exhaustive,
            raw_json_pages=page.raw_json_pages,
            http_statuses=page.http_statuses,
            limit_used=page.limit_used,
            transport=page.transport,
            api_generation=page.api_generation,
            empty=page.empty,
            behaviour_summaries=tuple(_behaviour_summary(item) for item in page.items),
        )

    async def intelligence_search(
        self,
        query: str,
        *,
        limit: int | None = None,
        cursor: str | None = None,
        paginate: bool = False,
        descriptors_only: bool = True,
        order: str | None = None,
    ) -> VirusTotalSearchResult:
        self._require(VirusTotalCapability.INTELLIGENCE_SEARCH)
        self._resolve_route(VirusTotalCapability.INTELLIGENCE_SEARCH)
        query = validate_search_query(query)
        page = await self._paginate(
            "/intelligence/search",
            limit=limit,
            cursor=cursor,
            paginate=paginate,
            extra_params={
                "query": query,
                "descriptors_only": str(descriptors_only).lower(),
                **({"order": order} if order is not None else {}),
            },
            operation=VirusTotalOperation.INTELLIGENCE_SEARCH,
        )
        return VirusTotalSearchResult(
            items=page.items,
            next_cursor=page.next_cursor,
            observed_count=page.observed_count,
            stopped_due_to_limit=page.stopped_due_to_limit,
            exhaustive=page.exhaustive,
            raw_json_pages=page.raw_json_pages,
            http_statuses=page.http_statuses,
            limit_used=page.limit_used,
            transport=page.transport,
            api_generation=page.api_generation,
            empty=page.empty,
            descriptors_only=descriptors_only,
            order=order,
        )

    async def _paginate(
        self,
        path: str,
        *,
        limit: int | None,
        cursor: str | None,
        paginate: bool,
        operation: VirusTotalOperation,
        extra_params: Mapping[str, str] | None = None,
    ) -> VirusTotalPage:
        page_limit = self._page_limit(limit)
        pages: list[bytes] = []
        statuses: list[int] = []
        items: list[dict[str, Any]] = []
        current_cursor = cursor
        stopped = False
        for _ in range(self._max_pages if paginate else 1):
            params: dict[str, Any] = dict(extra_params or {})
            params["limit"] = page_limit
            if current_cursor:
                params["cursor"] = current_cursor
            response = await self._get(
                path,
                params=params,
                operation=operation,
                route="proxy:v3",
            )
            payload = _json_object(response.body)
            page_items = _parse_items(payload)
            pages.append(response.body)
            statuses.append(response.status_code)
            remaining = self._max_results - len(items)
            items.extend(page_items[:remaining])
            next_cursor = _next_cursor(payload)
            if len(items) >= self._max_results and (len(page_items) > remaining or next_cursor):
                stopped = True
                next_cursor = next_cursor
                break
            if not paginate or not next_cursor:
                current_cursor = next_cursor
                break
            current_cursor = next_cursor
        else:
            stopped = bool(current_cursor)
            next_cursor = current_cursor
        exhaustive = not stopped and next_cursor is None
        return VirusTotalPage(
            items=tuple(items),
            next_cursor=next_cursor,
            observed_count=len(items),
            stopped_due_to_limit=stopped,
            exhaustive=exhaustive,
            raw_json_pages=tuple(pages),
            http_statuses=tuple(statuses),
            limit_used=page_limit,
            transport=VirusTotalTransportKind.PROXY,
            api_generation=VirusTotalEndpointVariant.V3,
            empty=not items and exhaustive,
        )

    async def _get(
        self,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        client: httpx.AsyncClient | None = None,
        base_url: str | None = None,
        operation: VirusTotalOperation | None = None,
        route: str | None = "proxy:v3",
        capability: str | None = None,
        correlation_id: str | None = None,
        fingerprint: str | None = None,
        fallback_decision: str | None = None,
        secret_values: tuple[str | None, ...] = (),
        response_max_bytes: int | None = None,
        coordinated_download: bool = False,
        acquisition_hash: str | None = None,
    ) -> VirusTotalRawResponse:
        correlation_id = correlation_id or uuid4().hex
        normalized_route = _normalize_route(path)
        route_label = route or "proxy:v3"
        transport = _transport_name_from_label(route_label)
        max_response_bytes = (
            self._max_response_bytes
            if response_max_bytes is None
            else _positive(response_max_bytes, "response_max_bytes")
        )
        response: httpx.Response | None = None
        content_type_class: str | None = None
        location_host: str | None = None
        bytes_read = 0
        try:
            request_params = _proxy_query_params(
                params,
                transport=transport,
                sentinel_enabled=self._proxy_query_sentinel_enabled,
            )
            family = _virustotal_external_effect_family(operation, capability)
            if coordinated_download:
                response = await self._coordinated_download_response(
                    client or self._client,
                    f"{base_url or self._base_url}{path}",
                    params=request_params,
                    headers={"accept-encoding": "identity"},
                    route=route_label,
                    transport=transport,
                    hop_index=0,
                    signed=False,
                    acquisition_hash=acquisition_hash,
                    max_response_bytes=max_response_bytes,
                )
                await response.aread()
            elif family is None:
                response = await (client or self._client).get(
                    f"{base_url or self._base_url}{path}",
                    params=request_params,
                    follow_redirects=False,
                )
            else:
                response = await self._coordinated_read(
                    client=client or self._client,
                    url=f"{base_url or self._base_url}{path}",
                    params=request_params,
                    family=family,
                    route=route_label,
                    max_response_bytes=max_response_bytes,
                )
            content_type_class = _content_type_class(response.headers.get("Content-Type"))
            location_host = _url_host(response.headers.get("Location"))
            if 300 <= response.status_code < 400:
                raise VirusTotalUnexpectedRedirectError(
                    "VirusTotal a renvoyé une redirection inattendue.",
                    status_code=response.status_code,
                    operation=operation,
                    transport=transport,
                    route=route_label,
                    route_path=normalized_route,
                    content_type_class=content_type_class,
                    location_host=location_host,
                    correlation_id=correlation_id,
                    fingerprint=fingerprint or self.acquisition_route_fingerprint,
                    fallback_decision=fallback_decision,
                )
            if response.status_code >= 400:
                retry_after = _retry_after(response.headers.get("Retry-After"))
                body = response.content[:_MAX_ERROR_BODY_BYTES]
                bytes_read = len(body)
                raise _http_error(
                    response.status_code,
                    retry_after,
                    body=body,
                    operation=operation,
                    route=route_label,
                    route_path=normalized_route,
                    transport=transport,
                    content_type_class=content_type_class,
                    location_host=location_host,
                    bytes_read=bytes_read,
                    correlation_id=correlation_id,
                    fingerprint=fingerprint or self.acquisition_route_fingerprint,
                    issuer=_response_issuer(response.headers, body),
                    fallback_decision=fallback_decision,
                    secret_values=secret_values + ((self._api_key,) if self._api_key else ()),
                )
            body = await _read_bounded(response, max_response_bytes)
            bytes_read = len(body)
            _log_vt_exchange(
                correlation_id=correlation_id,
                capability=capability or _operation_capability(operation),
                route=normalized_route,
                transport=transport,
                status_code=response.status_code,
                content_type_class=content_type_class,
                location_host=location_host,
                bytes_read=bytes_read,
                error_code=None,
                fallback_decision=fallback_decision or "none",
                fingerprint=fingerprint or self.acquisition_route_fingerprint,
            )
            return VirusTotalRawResponse(
                body=body,
                status_code=response.status_code,
                content_type_class=content_type_class,
            )
        except VirusTotalError as caught_error:
            _set_error_route(caught_error, route_label, normalized_route)
            _annotate_error(
                caught_error,
                transport=transport,
                content_type_class=content_type_class,
                location_host=location_host,
                bytes_read=bytes_read,
                correlation_id=correlation_id,
                fingerprint=fingerprint or self.acquisition_route_fingerprint,
                fallback_decision=fallback_decision,
            )
            _log_vt_exchange(
                correlation_id=correlation_id,
                capability=capability or _operation_capability(operation),
                route=normalized_route,
                transport=transport,
                status_code=(
                    response.status_code if response is not None else caught_error.status_code
                ),
                content_type_class=content_type_class,
                location_host=location_host,
                bytes_read=bytes_read,
                error_code=caught_error.code,
                fallback_decision=fallback_decision or "none",
                fingerprint=fingerprint or self.acquisition_route_fingerprint,
            )
            raise
        except httpx.ConnectTimeout:
            error = VirusTotalTransportError(
                "Le proxy VirusTotal a expiré pendant la connexion.",
                code="virustotal_connect_timeout",
                retryable=True,
                operation=operation,
            )
        except httpx.ReadTimeout:
            error = VirusTotalTransportError(
                "VirusTotal a expiré pendant la lecture.",
                code="virustotal_read_timeout",
                retryable=True,
                operation=operation,
            )
        except httpx.ProxyError:
            error = VirusTotalTransportError(
                "La connexion au proxy VirusTotal a échoué.",
                code="virustotal_proxy_error",
                retryable=True,
                operation=operation,
            )
        except httpx.ConnectError as exc:
            if _is_tls_error(exc):
                error = VirusTotalTransportError(
                    "La vérification TLS de VirusTotal a échoué.",
                    code="virustotal_tls_error",
                    retryable=False,
                    operation=operation,
                )
            else:
                error = VirusTotalTransportError(
                    "La connexion au proxy VirusTotal a échoué.",
                    code="virustotal_connection_error",
                    retryable=True,
                    operation=operation,
                )
        except httpx.TimeoutException:
            error = VirusTotalTransportError(
                "La requête VirusTotal a expiré.",
                code="virustotal_timeout",
                retryable=True,
                operation=operation,
            )
        except httpx.TransportError:
            error = VirusTotalTransportError(
                "Le transport VirusTotal a échoué.",
                code="virustotal_transport_error",
                retryable=True,
                operation=operation,
            )
        _set_error_route(error, route_label, normalized_route)
        _annotate_error(
            error,
            transport=transport,
            content_type_class=content_type_class,
            location_host=location_host,
            bytes_read=bytes_read,
            correlation_id=correlation_id,
            fingerprint=fingerprint or self.acquisition_route_fingerprint,
            fallback_decision=fallback_decision,
        )
        _log_vt_exchange(
            correlation_id=correlation_id,
            capability=capability or _operation_capability(operation),
            route=normalized_route,
            transport=transport,
            status_code=None,
            content_type_class=content_type_class,
            location_host=location_host,
            bytes_read=bytes_read,
            error_code=error.code,
            fallback_decision=fallback_decision or "none",
            fingerprint=fingerprint or self.acquisition_route_fingerprint,
        )
        raise error from None

    async def _coordinated_read(
        self,
        *,
        client: httpx.AsyncClient,
        url: str,
        params: Mapping[str, Any] | None,
        family: str,
        route: str,
        max_response_bytes: int | None = None,
    ) -> httpx.Response:
        authority = current_virustotal_external_effect_authority()
        coordinator = self._external_effect_coordinator
        archive = self._raw_response_archive
        if (
            self._offline_probe_mode
            and authority is None
            and coordinator is None
            and archive is None
        ):
            # This mode is used only by the local MockTransport preflight. Runtime
            # adapter construction leaves it disabled, so investigation reads
            # always require the coordinator and committed authority below.
            return await client.get(url, params=params, follow_redirects=False)
        if authority is None or coordinator is None or archive is None:
            raise VirusTotalExternalEffectRefusedError(
                "The VirusTotal read has no committed investigation authority."
            )

        request = client.build_request("GET", url, params=params)
        frozen_request = _freeze_httpx_request(request)
        canonical_request = {
            "schema": "vt-read-request.v1",
            "family": family,
            "route": route,
            "method": "GET",
            "path": request.url.path,
            "query": _credential_free_query(request),
        }
        prepared = PreparedExternalRequest.build(
            family=family,
            provider="vt",
            provider_scope="default",
            normalizer_version=_VT_READ_NORMALIZER_VERSION,
            canonical_request=canonical_request,
            application_request=frozen_request,
            evidence_refs=current_virustotal_external_effect_evidence_refs(),
            credential_parameter_names=("apikey",),
        )
        transport = _VirusTotalPermitAwareTransport(
            client=client,
            request=request,
            frozen_request=frozen_request,
            archive=archive,
            max_response_bytes=(
                self._max_response_bytes if max_response_bytes is None else max_response_bytes
            ),
        )
        try:
            outcome = await coordinator.send_one(authority, prepared, transport)
        except Exception:
            raise VirusTotalExternalEffectNeedsReviewError(
                "The VirusTotal effect outcome could not be confirmed; it will not be resent."
            ) from None

        if isinstance(outcome, Completed):
            envelope = outcome.payload
            decoded = (
                _decode_vt_transient_response(envelope) if isinstance(envelope, bytes) else None
            )
            reference = _one_raw_response_reference(outcome.evidence_refs)
            if reference is None or outcome.result_ref != reference.id:
                raise VirusTotalExternalEffectNeedsReviewError(
                    "The completed VirusTotal response has no raw archive reference."
                )
            archived = await _read_vt_archive(
                archive,
                reference,
                max(
                    (self._max_response_bytes if max_response_bytes is None else max_response_bytes)
                    + 1,
                    _MAX_ERROR_BODY_BYTES,
                ),
            )
            archived_transient = _transient_from_archive(archived)
            if decoded is None:
                decoded = archived_transient
            elif decoded != archived_transient:
                raise VirusTotalExternalEffectNeedsReviewError(
                    "The completed VirusTotal response does not match its raw archive."
                )
            record_virustotal_external_effect(reused=outcome.payload is None)
            return _httpx_response_from_transient(request, decoded)

        if isinstance(outcome, Reused):
            reference = _one_raw_response_reference(outcome.observation_refs)
            if reference is None:
                raise VirusTotalExternalEffectNeedsReviewError(
                    "The reused VirusTotal effect has no raw response archive."
                )
            archived = await _read_vt_archive(
                archive,
                reference,
                max(
                    (self._max_response_bytes if max_response_bytes is None else max_response_bytes)
                    + 1,
                    _MAX_ERROR_BODY_BYTES,
                ),
            )
            record_virustotal_external_effect(reused=True)
            return _httpx_response_from_transient(request, _transient_from_archive(archived))

        if isinstance(outcome, Ambiguous):
            raise VirusTotalExternalEffectNeedsReviewError(
                "The VirusTotal request outcome is ambiguous; it will not be resent."
            )
        if isinstance(outcome, (Refused, DefinitelyNotSubmitted)):
            refusal_code = (
                f"virustotal_external_effect_refused_{outcome.reason}"
                if isinstance(outcome, Refused)
                else "virustotal_external_effect_not_submitted"
            )
            raise VirusTotalExternalEffectRefusedError(
                "The coordinator refused the VirusTotal request before a new send.",
                code=refusal_code,
            )
        raise VirusTotalExternalEffectNeedsReviewError(
            "The coordinator returned an unknown VirusTotal outcome."
        )

    async def _coordinated_download_response(
        self,
        client: httpx.AsyncClient,
        url: str,
        *,
        params: Mapping[str, str] | None,
        headers: Mapping[str, str],
        route: str,
        transport: str,
        hop_index: int,
        signed: bool,
        acquisition_hash: str | None,
        max_response_bytes: int,
    ) -> httpx.Response:
        authority = current_virustotal_external_effect_authority()
        coordinator = self._external_effect_coordinator
        if self._offline_probe_mode and authority is None and coordinator is None:
            request = client.build_request(
                "GET",
                url,
                params=params,
                headers=headers,
            )
            return await client.send(request, follow_redirects=False, stream=True)
        if authority is None or coordinator is None or acquisition_hash is None:
            raise VirusTotalExternalEffectRefusedError(
                "The VirusTotal download has no committed investigation authority."
            )

        request = client.build_request("GET", url, params=params, headers=headers)
        normalized_query_names = {
            normalize_credential_parameter_name(name)
            for name, _value in request.url.params.multi_items()
        }
        declared_names = {
            normalize_credential_parameter_name(name)
            for name in VT_DOWNLOAD_CREDENTIAL_PARAMETER_NAMES
        }
        if not normalized_query_names.issubset(declared_names):
            raise VirusTotalExternalEffectRefusedError(
                "The VirusTotal download URL uses an undeclared query parameter.",
                code="virustotal_download_query_not_allowlisted",
            )
        if signed:
            _validate_signed_download_url(
                str(request.url), self._download_allowed_hosts, self._download_allowed_ports
            )
        if not re.fullmatch(r"[0-9a-f]{64}", acquisition_hash):
            raise VirusTotalExternalEffectRefusedError(
                "The VirusTotal download request has no canonical SHA-256 source."
            )
        frozen_request = _freeze_httpx_request(request)
        canonical_request = {
            "schema": "vt-download-request.v1",
            "family": "vt.download_sample",
            "route": route,
            "transport": transport,
            "method": "GET",
            "host": request.url.host.casefold(),
            "path": "signed-cdn-object" if signed else _normalize_route(request.url.path),
            "hop": hop_index,
            "sha256": acquisition_hash,
        }
        prepared = PreparedExternalRequest.build(
            family="vt.download_sample",
            provider="vt",
            provider_scope="default",
            normalizer_version=_VT_DOWNLOAD_NORMALIZER_VERSION,
            canonical_request=canonical_request,
            application_request=frozen_request,
            evidence_refs=current_virustotal_external_effect_evidence_refs(),
            credential_parameter_names=tuple(sorted(VT_DOWNLOAD_CREDENTIAL_PARAMETER_NAMES)),
        )
        transport_adapter = _VirusTotalDownloadPermitAwareTransport(
            client=client,
            request=request,
            frozen_request=frozen_request,
            max_response_bytes=max_response_bytes,
        )
        try:
            try:
                outcome = await coordinator.send_one(authority, prepared, transport_adapter)
            except Exception:
                raise VirusTotalExternalEffectNeedsReviewError(
                    "The VirusTotal download outcome could not be confirmed; it will not be resent."
                ) from None
            if isinstance(outcome, Completed) and isinstance(outcome.payload, bytes):
                response = transport_adapter.take(outcome.payload)
                if response is None:
                    raise VirusTotalExternalEffectNeedsReviewError(
                        "The completed VirusTotal download has no transient response stream."
                    )
                record_virustotal_external_effect(reused=False)
                return response
            if isinstance(outcome, (Refused, DefinitelyNotSubmitted)):
                code = (
                    f"virustotal_external_effect_refused_{outcome.reason}"
                    if isinstance(outcome, Refused)
                    else "virustotal_external_effect_not_submitted"
                )
                raise VirusTotalExternalEffectRefusedError(
                    "The coordinator refused the VirusTotal download before a new send.",
                    code=code,
                )
            if isinstance(outcome, (Ambiguous, Reused, Completed)):
                raise VirusTotalExternalEffectNeedsReviewError(
                    "The VirusTotal download claim cannot authorize another physical send."
                )
            raise VirusTotalExternalEffectNeedsReviewError(
                "The coordinator returned an unknown VirusTotal download outcome."
            )
        finally:
            transport_adapter.close_pending()

    def _page_limit(self, value: int | None) -> int:
        if value is None:
            return self._default_page_size
        if value < 1 or value > self._max_page_size:
            raise VirusTotalInvalidInputError("La taille de page VirusTotal est hors limites.")
        return value

    def _require(self, capability: VirusTotalCapability) -> None:
        if not self._capabilities.is_enabled(capability):
            raise VirusTotalCapabilityDisabledError("La capability VirusTotal est désactivée.")


def create_virustotal_http_adapter(
    *,
    client: httpx.AsyncClient,
    settings: Settings,
    capabilities: VirusTotalCapabilities,
    direct_client: httpx.AsyncClient | None = None,
    api_key: str | None = None,
    routing_policy: VirusTotalRoutingPolicy | None = None,
    offline_probe_mode: bool = False,
) -> VirusTotalHttpAdapter:
    """Bind adapter policy and limits to the validated runtime settings."""
    arguments = {
        argument: getattr(settings, field_name)
        for argument, field_name in VIRUSTOTAL_ADAPTER_SETTINGS_BY_ARGUMENT.items()
    }
    arguments["download_allowed_hosts"] = frozenset(
        host for host in settings.virustotal_download_allowed_hosts.split(",") if host
    )
    arguments["download_allowed_ports"] = frozenset(
        int(port) for port in settings.virustotal_download_allowed_ports.split(",") if port
    )
    effective_routing_policy = routing_policy or _default_routing_policy(
        base_url=settings.virustotal_base_url,
        fallback_base_url=settings.virustotal_fallback_base_url,
        legacy_base_url=settings.virustotal_legacy_base_url,
        direct_client=direct_client,
        api_key=(
            api_key
            if api_key is not None
            else settings.virustotal_api_key.get_secret_value()
            if settings.virustotal_api_key is not None and direct_client is not None
            else None
        ),
        proxy_fallback_enabled=settings.virustotal_file_report_proxy_fallback_enabled,
        legacy_fallback_enabled=settings.virustotal_file_report_legacy_fallback_enabled,
        file_download_enabled=settings.virustotal_file_download_enabled,
        behaviour_pcap_enabled=settings.virustotal_behaviour_pcap_enabled,
    )
    proxy_ready = bool(settings.virustotal_proxy_url and settings.virustotal_base_url)
    direct_ready = bool(
        direct_client is not None
        and (api_key is not None or settings.virustotal_api_key is not None)
        and settings.virustotal_legacy_base_url
    )
    route_opt_ins = {
        VirusTotalCapability.FILE_REPORT: settings.virustotal_file_report_enabled,
        VirusTotalCapability.FILE_RELATIONSHIPS: settings.virustotal_object_relationships_enabled,
        VirusTotalCapability.INTELLIGENCE_SEARCH: settings.virustotal_intelligence_search_enabled,
        VirusTotalCapability.FILE_DOWNLOAD: settings.virustotal_file_download_enabled,
        VirusTotalCapability.BEHAVIOUR_REPORTS: settings.virustotal_behaviour_reports_enabled,
        VirusTotalCapability.BEHAVIOUR_PCAP: settings.virustotal_behaviour_pcap_enabled,
    }
    configured_routes = {}
    for capability, route in effective_routing_policy.routes.items():
        if capability in route_opt_ins and not route_opt_ins[capability]:
            continue
        transport_ready = (
            proxy_ready
            if route.primary.transport is VirusTotalTransportKind.PROXY
            else direct_ready
        )
        if not transport_ready:
            continue
        if capability is VirusTotalCapability.FILE_DOWNLOAD and (
            VIRUSTOTAL_DOWNLOAD_REQUIRED_CDN_HOST not in arguments["download_allowed_hosts"]
            or not arguments["download_allowed_ports"]
        ):
            continue
        configured_routes[capability] = route
    effective_routing_policy = VirusTotalRoutingPolicy(routes=configured_routes)

    def primary_route_ready(capability: VirusTotalCapability) -> bool:
        route = effective_routing_policy.route_for(capability)
        if route is None:
            return False
        return (
            proxy_ready
            if route.primary.transport is VirusTotalTransportKind.PROXY
            else direct_ready
        )

    effective_capabilities = VirusTotalCapabilities(
        file_report=(
            capabilities.file_report
            and settings.virustotal_file_report_enabled
            and primary_route_ready(VirusTotalCapability.FILE_REPORT)
        ),
        file_relationships=(
            capabilities.file_relationships
            and settings.virustotal_object_relationships_enabled
            and primary_route_ready(VirusTotalCapability.FILE_RELATIONSHIPS)
        ),
        intelligence_search=(
            capabilities.intelligence_search
            and settings.virustotal_intelligence_search_enabled
            and primary_route_ready(VirusTotalCapability.INTELLIGENCE_SEARCH)
        ),
        file_download=(
            capabilities.file_download
            and settings.virustotal_file_download_enabled
            and VIRUSTOTAL_DOWNLOAD_REQUIRED_CDN_HOST in arguments["download_allowed_hosts"]
            and bool(arguments["download_allowed_ports"])
            and primary_route_ready(VirusTotalCapability.FILE_DOWNLOAD)
        ),
        submissions=capabilities.submissions,
        behaviour_reports=(
            capabilities.behaviour_reports
            and settings.virustotal_behaviour_reports_enabled
            and primary_route_ready(VirusTotalCapability.BEHAVIOUR_REPORTS)
        ),
        behaviour_pcap=(
            settings.virustotal_behaviour_pcap_enabled
            and primary_route_ready(VirusTotalCapability.BEHAVIOUR_PCAP)
        ),
        retrohunt=capabilities.retrohunt,
    )
    resolved_api_key = api_key
    if direct_client is None:
        resolved_api_key = None
    elif resolved_api_key is None and settings.virustotal_api_key is not None:
        resolved_api_key = settings.virustotal_api_key.get_secret_value()
    return VirusTotalHttpAdapter(
        client=client,
        capabilities=effective_capabilities,
        direct_client=direct_client,
        api_key=resolved_api_key,
        routing_policy=effective_routing_policy,
        offline_probe_mode=offline_probe_mode,
        **arguments,
    )


def _default_routing_policy(
    *,
    base_url: str,
    fallback_base_url: str | None,
    legacy_base_url: str | None,
    direct_client: httpx.AsyncClient | None,
    api_key: str | None,
    proxy_fallback_enabled: bool,
    legacy_fallback_enabled: bool,
    file_download_enabled: bool = False,
    behaviour_pcap_enabled: bool = False,
) -> VirusTotalRoutingPolicy:
    """Build the deny-by-default policy this adapter ships with.

    Every step below requires its own explicit enable flag: neither a
    configured fallback base URL nor a present API key is, by itself,
    sufficient to add a step. Only `file_report` carries fallbacks; other
    operations use the proxy primary route only, matching what this adapter
    actually implements for them.
    """
    file_report_steps = [
        VirusTotalRouteStep(VirusTotalTransportKind.PROXY, VirusTotalEndpointVariant.V3)
    ]
    if proxy_fallback_enabled and fallback_base_url is not None and fallback_base_url != base_url:
        file_report_steps.append(
            VirusTotalRouteStep(
                VirusTotalTransportKind.PROXY, VirusTotalEndpointVariant.V3_FALLBACK
            )
        )
    if (
        legacy_fallback_enabled
        and direct_client is not None
        and api_key is not None
        and legacy_base_url is not None
    ):
        file_report_steps.append(
            VirusTotalRouteStep(VirusTotalTransportKind.DIRECT, VirusTotalEndpointVariant.LEGACY_V2)
        )
    file_report_primary, *file_report_fallbacks = file_report_steps
    proxy_only = VirusTotalOperationRoute(
        primary=VirusTotalRouteStep(VirusTotalTransportKind.PROXY, VirusTotalEndpointVariant.V3)
    )
    routes = {
        VirusTotalCapability.FILE_REPORT: VirusTotalOperationRoute(
            primary=file_report_primary, fallbacks=tuple(file_report_fallbacks)
        ),
        VirusTotalCapability.FILE_RELATIONSHIPS: proxy_only,
        VirusTotalCapability.INTELLIGENCE_SEARCH: proxy_only,
        VirusTotalCapability.BEHAVIOUR_REPORTS: proxy_only,
    }
    if file_download_enabled:
        routes[VirusTotalCapability.FILE_DOWNLOAD] = proxy_only
    if behaviour_pcap_enabled:
        routes[VirusTotalCapability.BEHAVIOUR_PCAP] = proxy_only
    return VirusTotalRoutingPolicy(routes=routes)


def _route_name(step: VirusTotalRouteStep) -> str:
    return f"{step.transport.value}:{step.variant.value}"


def _transport_name(step: VirusTotalRouteStep) -> str:
    if step.transport is VirusTotalTransportKind.PROXY:
        return "proxy-v3"
    if step.variant is VirusTotalEndpointVariant.LEGACY_V2:
        return "direct-v2"
    return "direct-v3"


def _transport_name_from_label(label: str | None) -> str:
    normalized = (label or "proxy:v3").lower()
    if normalized in {"signed-cdn", "signed:cdn"}:
        return "signed-cdn"
    if normalized.startswith("direct:") and "legacy" in normalized:
        return "direct-v2"
    if normalized.startswith("direct:"):
        return "direct-v3"
    return "proxy-v3"


def _proxy_query_params(
    params: Mapping[str, Any] | None,
    *,
    transport: str,
    sentinel_enabled: bool,
) -> Mapping[str, Any] | None:
    if transport == "proxy-v3" and sentinel_enabled and not params:
        return {VIRUSTOTAL_PROXY_QUERY_SENTINEL_NAME: VIRUSTOTAL_PROXY_QUERY_SENTINEL_VALUE}
    return params


def _normalize_route(value: str) -> str:
    """Return a route template without query strings or object identifiers."""
    path = urlsplit(value).path
    for prefix in ("/api/v3", "/vtapi/v2"):
        if path == prefix:
            path = "/"
            break
        if path.startswith(f"{prefix}/"):
            path = path[len(prefix) :]
            break
    parts = path.split("/")
    collections = {
        "files": "{hash}",
        "file_behaviours": "{sandbox_id}",
        "ip_addresses": "{ip}",
        "domains": "{domain}",
        "urls": "{url_id}",
        "users": "{user_id}",
        "retrohunt_jobs": "{job_id}",
    }
    for index, part in enumerate(parts[:-1]):
        replacement = collections.get(part)
        if replacement is not None and parts[index + 1]:
            parts[index + 1] = replacement
    return "/".join(parts) or "/"


def _content_type_class(value: str | None) -> str:
    if not value:
        return "missing"
    media_type = value.partition(";")[0].strip().lower()
    if media_type == "text/html" or media_type.endswith("/html"):
        return "html"
    if media_type == "application/json" or media_type.endswith("+json"):
        return "json"
    if media_type in {
        "application/octet-stream",
        "application/pcap",
        "application/vnd.tcpdump.pcap",
    }:
        return "binary"
    if media_type.startswith("text/"):
        return "text"
    return "other" if media_type else "missing"


def _sniff_binary_error(prefix: bytes, content_type_class: str) -> str | None:
    candidate = prefix.lstrip(b"\xef\xbb\xbf\x00\t\r\n ").lower()
    if content_type_class == "html" or candidate.startswith(
        (b"<!doctype html", b"<html", b"<head", b"<body")
    ):
        return "html"
    if content_type_class == "json" or candidate.startswith((b"{", b"[")):
        return "json"
    return None


def _content_length(value: str | None) -> int | object | None:
    if value is None:
        return None
    if not value.isascii() or not value.isdecimal():
        return _INVALID_CONTENT_LENGTH
    return int(value)


def _url_host(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return urlsplit(value).hostname
    except ValueError:
        return None


def _response_issuer(headers: Mapping[str, str], body: bytes) -> str:
    """Heuristic only: an intervening proxy can rewrite provider responses."""
    lowered = {name.lower(): value.lower() for name, value in headers.items()}
    if "via" in lowered or "x-proxy-error" in lowered:
        return "proxy-likely"
    server = lowered.get("server", "")
    if any(token in server for token in ("nginx", "envoy", "haproxy", "varnish", "squid")):
        return "proxy-likely"
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        payload = None
    if isinstance(payload, dict) and isinstance(payload.get("error"), dict):
        return "upstream-vt-likely"
    if "virustotal" in server:
        return "upstream-vt-likely"
    return "unknown"


def _is_route_missing_error(error: VirusTotalHttpError) -> bool:
    if error.status_code != 404 or error.issuer != "proxy-likely":
        return False
    marker = f"{error.error_code or ''} {error.error_message or ''}".lower().replace("_", " ")
    return any(
        text in marker
        for text in (
            "route not found",
            "route missing",
            "no route",
            "endpoint not found",
            "unknown route",
            "cannot get",
        )
    )


def _strip_cdn_client_credentials(client: httpx.AsyncClient) -> None:
    """Strip credentials and cookies before an ephemeral signed-host request."""
    if getattr(client, "_trust_env", True) and isinstance(client._transport, httpx.MockTransport):
        # An injected MockTransport has no proxy mounts or network path to inherit.
        client._trust_env = False
    if getattr(client, "_trust_env", True) is not False:
        raise VirusTotalConfigurationError(
            "Le client CDN signé doit être créé avec trust_env=False."
        )
    client.headers.pop("x-apikey", None)
    client.headers.pop("authorization", None)
    client.headers.pop("cookie", None)
    client.cookies.clear()
    client._auth = None
    client.params = httpx.QueryParams()


def _annotate_error(error: VirusTotalError, **values: Any) -> None:
    for name, value in values.items():
        current = getattr(error, name, None)
        if value is not None and (
            current is None or (name == "response_origin" and current == "unknown")
        ):
            setattr(error, name, value)


def _set_error_route(error: VirusTotalError, route_label: str, route_path: str) -> None:
    """Keep the historical route label and attach the normalized wire path."""
    error.route = route_label
    error.route_path = route_path


def _operation_capability(operation: VirusTotalOperation | None) -> str:
    if operation is None:
        return "unknown"
    return {
        VirusTotalOperation.FILE_REPORT: VirusTotalCapability.FILE_REPORT.value,
        VirusTotalOperation.FILE_DOWNLOAD: VirusTotalCapability.FILE_DOWNLOAD.value,
        VirusTotalOperation.FILE_RELATIONSHIP: VirusTotalCapability.FILE_RELATIONSHIPS.value,
        VirusTotalOperation.OBJECT_RELATIONSHIP: VirusTotalCapability.FILE_RELATIONSHIPS.value,
        VirusTotalOperation.BEHAVIOURS: VirusTotalCapability.BEHAVIOUR_REPORTS.value,
        VirusTotalOperation.INTELLIGENCE_SEARCH: VirusTotalCapability.INTELLIGENCE_SEARCH.value,
    }.get(operation, "unknown")


def _log_vt_exchange(
    *,
    correlation_id: str,
    capability: str,
    route: str,
    transport: str,
    status_code: int | None,
    content_type_class: str | None,
    location_host: str | None,
    bytes_read: int,
    error_code: str | None,
    fallback_decision: str,
    fingerprint: str | None,
) -> None:
    logger.info(
        "vt_http_exchange",
        extra={
            "event": "vt_http_exchange",
            "correlation_id": correlation_id,
            "provider": "virustotal",
            "capability": capability,
            "route": route,
            "transport": transport,
            "status_code": status_code,
            "content_type_class": content_type_class,
            "location_host": location_host,
            "bytes_read": bytes_read,
            "error_code": error_code,
            "fallback_decision": fallback_decision,
            "fingerprint": fingerprint,
        },
    )


def _download_route_fingerprint(
    *,
    base_url: str,
    routing_policy: VirusTotalRoutingPolicy,
    file_download_enabled: bool,
    file_download_legacy_fallback_enabled: bool,
    allowed_hosts: frozenset[str],
    allowed_ports: frozenset[int],
    max_redirects: int,
    max_bytes: int,
    timeout_seconds: float,
    proxy_query_sentinel_enabled: bool,
) -> str:
    base_host = (urlsplit(base_url).hostname or "").lower()
    route = routing_policy.route_for(VirusTotalCapability.FILE_DOWNLOAD)
    steps = (
        []
        if route is None
        else [
            {"transport": step.transport.value, "variant": step.variant.value}
            for step in route.steps()
        ]
    )
    contract = {
        "version": _DOWNLOAD_FINGERPRINT_CONFIG_VERSION,
        "base_host": base_host,
        "route": steps,
        "file_download_enabled": file_download_enabled,
        "file_download_legacy_fallback_enabled": file_download_legacy_fallback_enabled,
        "proxy_query_sentinel_enabled": proxy_query_sentinel_enabled,
        "allowed_hosts": sorted(allowed_hosts),
        "allowed_ports": sorted(allowed_ports),
        "max_redirects": max_redirects,
        "max_bytes": max_bytes,
        "timeout_seconds": timeout_seconds,
    }
    return _fingerprint_json(contract)


def _fingerprint_json(contract: Mapping[str, Any]) -> str:
    material = json.dumps(contract, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def _sample_acquisition_response_origin(
    transport: str | None, issuer: str | None
) -> SampleAcquisitionResponseOrigin:
    """Map diagnostic transport heuristics to Lot C's cache safety boundary.

    Provider/proxy attribution is heuristic (JSON VT error shape, server/via
    headers, and explicit proxy markers); it does not prove who authored a
    response. Signed CDN responses are attributed only to that separate hop.
    """
    if transport == "signed-cdn":
        return SampleAcquisitionResponseOrigin.SIGNED_CDN
    if issuer == "proxy-likely":
        return SampleAcquisitionResponseOrigin.PROXY
    if issuer == "upstream-vt-likely":
        return SampleAcquisitionResponseOrigin.PROVIDER
    return SampleAcquisitionResponseOrigin.UNKNOWN


_INVALID_CONTENT_LENGTH = object()


def _validate_base_url(value: str) -> str:
    parsed = urlsplit(value.strip())
    if parsed.scheme not in {"http", "https"} or parsed.hostname not in {
        "virustotal.com",
        "www.virustotal.com",
    }:
        raise VirusTotalConfigurationError("La base VirusTotal doit cibler l'API VirusTotal v3.")
    if parsed.query or parsed.fragment or not parsed.path.rstrip("/").endswith("/api/v3"):
        raise VirusTotalConfigurationError(
            "La base VirusTotal doit être un préfixe API v3 sans querystring."
        )
    return value.rstrip("/")


def _validate_legacy_base_url(value: str) -> str:
    parsed = urlsplit(value.strip())
    if parsed.scheme not in {"http", "https"} or parsed.hostname not in {
        "virustotal.com",
        "www.virustotal.com",
    }:
        raise VirusTotalConfigurationError("La base legacy VirusTotal est invalide.")
    if parsed.query or parsed.fragment or not parsed.path.rstrip("/").endswith("/vtapi/v2"):
        raise VirusTotalConfigurationError("La base legacy doit cibler /vtapi/v2.")
    return value.rstrip("/")


def _positive(value: int, name: str) -> int:
    if value < 1:
        raise ValueError(f"{name} doit être positif")
    return value


def _positive_float(value: float, name: str) -> float:
    if value <= 0:
        raise ValueError(f"{name} doit être positif")
    return value


def _validate_signed_download_url(
    url: str,
    allowed_hosts: frozenset[str],
    allowed_ports: frozenset[int],
) -> None:
    try:
        parsed = urlsplit(url)
        port = parsed.port if parsed.port is not None else 443
    except ValueError:
        raise VirusTotalDownloadHostNotAllowedError(
            "L'URL de téléchargement signée est invalide."
        ) from None
    if parsed.scheme.lower() != "https":
        raise VirusTotalDownloadHostNotAllowedError(
            "L'URL de téléchargement signée doit être HTTPS."
        )
    if parsed.username or parsed.password:
        raise VirusTotalDownloadHostNotAllowedError(
            "L'URL de téléchargement signée ne doit pas contenir d'identifiants."
        )
    hostname = parsed.hostname
    if not hostname or hostname.lower() not in allowed_hosts:
        raise VirusTotalDownloadHostNotAllowedError(
            "L'hôte de l'URL de téléchargement signée n'est pas autorisé."
        )
    if port not in allowed_ports:
        raise VirusTotalDownloadHostNotAllowedError(
            "Le port de l'URL de téléchargement signée n'est pas autorisé."
        )


@asynccontextmanager
async def _download_response_stream(
    client: httpx.AsyncClient,
    url: str,
    *,
    params: Mapping[str, str] | None,
    headers: Mapping[str, str],
    coordinated_request: Callable[..., Awaitable[httpx.Response]] | None,
    route: str,
    transport: str,
    hop_index: int,
    signed: bool,
    acquisition_hash: str | None,
    max_response_bytes: int,
) -> AsyncIterator[httpx.Response]:
    if coordinated_request is None:
        async with client.stream(
            "GET",
            url,
            params=params,
            headers=headers,
            follow_redirects=False,
        ) as response:
            yield response
        return
    response = await coordinated_request(
        client,
        url,
        params=params,
        headers=headers,
        route=route,
        transport=transport,
        hop_index=hop_index,
        signed=signed,
        acquisition_hash=acquisition_hash,
        max_response_bytes=max_response_bytes,
    )
    try:
        yield response
    finally:
        await response.aclose()


async def _stream_signed_download(
    url: str,
    *,
    sink: BinaryIO,
    expected_hash: str,
    expected_family: str,
    max_bytes: int,
    allowed_hosts: frozenset[str],
    allowed_ports: frozenset[int],
    max_redirects: int,
    client_factory: Callable[[], httpx.AsyncClient],
    operation: VirusTotalOperation | None,
    route: str,
    capability: str,
    correlation_id: str,
    fingerprint: str,
    secret_values: tuple[str | None, ...] = (),
    coordinated_request: Callable[..., Awaitable[httpx.Response]] | None = None,
    acquisition_hash: str | None = None,
) -> VirusTotalDownloadResult:
    return await _stream_route_download(
        None,
        url,
        sink=sink,
        expected_hash=expected_hash,
        expected_family=expected_family,
        max_bytes=max_bytes,
        allowed_hosts=allowed_hosts,
        allowed_ports=allowed_ports,
        max_redirects=max_redirects,
        download_client_factory=client_factory,
        operation=operation,
        route=route,
        capability=capability,
        correlation_id=correlation_id,
        fingerprint=fingerprint,
        initial_signed=True,
        secret_values=secret_values,
        coordinated_request=coordinated_request,
        acquisition_hash=acquisition_hash,
    )


async def _stream_route_download(
    client: httpx.AsyncClient | None,
    url: str,
    *,
    sink: BinaryIO,
    expected_hash: str | None,
    expected_family: str | None,
    expected_magic: tuple[bytes, ...] | None = None,
    max_bytes: int,
    allowed_hosts: frozenset[str],
    allowed_ports: frozenset[int],
    max_redirects: int,
    download_client_factory: Callable[[], httpx.AsyncClient],
    params: Mapping[str, str] | None = None,
    proxy_query_sentinel_enabled: bool = False,
    follow_redirects: bool = True,
    operation: VirusTotalOperation | None,
    route: str,
    capability: str,
    correlation_id: str,
    fingerprint: str,
    fallback_decision_on_404: str = "none",
    initial_signed: bool = False,
    secret_values: tuple[str | None, ...] = (),
    coordinated_request: Callable[..., Awaitable[httpx.Response]] | None = None,
    acquisition_hash: str | None = None,
) -> VirusTotalDownloadResult:
    current_url = url
    redirects_followed = 0
    current_is_signed = initial_signed
    base_transport = _transport_name_from_label(route)
    normalized_route = _normalize_route(urlsplit(url).path)

    while True:
        ephemeral_client: httpx.AsyncClient | None = None
        active_client = client
        current_transport = "signed-cdn" if current_is_signed else base_transport
        if current_is_signed or redirects_followed > 0:
            ephemeral_client = download_client_factory()
            _strip_cdn_client_credentials(ephemeral_client)
            active_client = ephemeral_client
        if active_client is None:
            raise VirusTotalRouteUnavailableError(
                "Le transport de téléchargement VirusTotal n'est pas configuré.",
                route=route,
                route_path=normalized_route,
                transport=current_transport,
                correlation_id=correlation_id,
                fingerprint=fingerprint,
            )

        next_url: str | None = None
        location_host: str | None = None
        response: httpx.Response | None = None
        try:
            request_params = params if redirects_followed == 0 and not initial_signed else None
            request_params = _proxy_query_params(
                request_params,
                transport=current_transport,
                sentinel_enabled=proxy_query_sentinel_enabled,
            )
            async with _download_response_stream(
                active_client,
                current_url,
                params=request_params,
                headers={"accept-encoding": "identity"},
                coordinated_request=coordinated_request,
                route=route,
                transport=current_transport,
                hop_index=redirects_followed,
                signed=current_is_signed,
                acquisition_hash=acquisition_hash,
                max_response_bytes=max_bytes,
            ) as response:
                location_host = _url_host(response.headers.get("Location"))
                content_type_class = _content_type_class(response.headers.get("Content-Type"))
                if 300 <= response.status_code < 400:
                    if not follow_redirects:
                        redirect_error = VirusTotalUnexpectedRedirectError(
                            "La route VT ne peut pas rediriger cet artefact.",
                            status_code=response.status_code,
                            operation=operation,
                            transport=current_transport,
                            route=normalized_route,
                            content_type_class=content_type_class,
                            location_host=location_host,
                            bytes_read=0,
                            correlation_id=correlation_id,
                            fingerprint=fingerprint,
                        )
                        _log_vt_exchange(
                            correlation_id=correlation_id,
                            capability=capability,
                            route=normalized_route,
                            transport=current_transport,
                            status_code=response.status_code,
                            content_type_class=content_type_class,
                            location_host=location_host,
                            bytes_read=0,
                            error_code=redirect_error.code,
                            fallback_decision="redirect_rejected",
                            fingerprint=fingerprint,
                        )
                        raise redirect_error
                    if redirects_followed >= max_redirects:
                        limit_error = VirusTotalDownloadRedirectLimitError(
                            "Le téléchargement VirusTotal a dépassé la limite de redirections.",
                            status_code=response.status_code,
                            operation=operation,
                            transport=current_transport,
                            route=normalized_route,
                            content_type_class=content_type_class,
                            location_host=location_host,
                            bytes_read=0,
                            correlation_id=correlation_id,
                            fingerprint=fingerprint,
                        )
                        _log_vt_exchange(
                            correlation_id=correlation_id,
                            capability=capability,
                            route=normalized_route,
                            transport=current_transport,
                            status_code=response.status_code,
                            content_type_class=content_type_class,
                            location_host=location_host,
                            bytes_read=0,
                            error_code=limit_error.code,
                            fallback_decision="redirect_limit_reached",
                            fingerprint=fingerprint,
                        )
                        raise limit_error
                    location = response.headers.get("Location")
                    if not location:
                        missing_location_error = VirusTotalUnexpectedRedirectError(
                            "La redirection VirusTotal ne contient pas de destination.",
                            status_code=response.status_code,
                            operation=operation,
                            transport=current_transport,
                            route=normalized_route,
                            content_type_class=content_type_class,
                            location_host=None,
                            bytes_read=0,
                            correlation_id=correlation_id,
                            fingerprint=fingerprint,
                        )
                        _log_vt_exchange(
                            correlation_id=correlation_id,
                            capability=capability,
                            route=normalized_route,
                            transport=current_transport,
                            status_code=response.status_code,
                            content_type_class=content_type_class,
                            location_host=None,
                            bytes_read=0,
                            error_code=missing_location_error.code,
                            fallback_decision="redirect_missing_location",
                            fingerprint=fingerprint,
                        )
                        raise missing_location_error
                    try:
                        next_url = urljoin(current_url, location)
                        _validate_signed_download_url(next_url, allowed_hosts, allowed_ports)
                    except (ValueError, VirusTotalError) as destination_error:
                        if isinstance(destination_error, VirusTotalError):
                            _annotate_error(
                                destination_error,
                                operation=operation,
                                transport=current_transport,
                                route=normalized_route,
                                status_code=response.status_code,
                                content_type_class=content_type_class,
                                location_host=_url_host(location),
                                bytes_read=0,
                                correlation_id=correlation_id,
                                fingerprint=fingerprint,
                            )
                            _log_vt_exchange(
                                correlation_id=correlation_id,
                                capability=capability,
                                route=normalized_route,
                                transport=current_transport,
                                status_code=response.status_code,
                                content_type_class=content_type_class,
                                location_host=_url_host(location),
                                bytes_read=0,
                                error_code=destination_error.code,
                                fallback_decision="redirect_destination_rejected",
                                fingerprint=fingerprint,
                            )
                            raise
                        host_error = VirusTotalDownloadHostNotAllowedError(
                            "La destination de redirection VirusTotal est invalide.",
                            status_code=response.status_code,
                        )
                        _annotate_error(
                            host_error,
                            operation=operation,
                            transport=current_transport,
                            route=normalized_route,
                            content_type_class=content_type_class,
                            location_host=_url_host(location),
                            correlation_id=correlation_id,
                            fingerprint=fingerprint,
                        )
                        raise host_error from None
                    _log_vt_exchange(
                        correlation_id=correlation_id,
                        capability=capability,
                        route=normalized_route,
                        transport=current_transport,
                        status_code=response.status_code,
                        content_type_class=content_type_class,
                        location_host=_url_host(next_url),
                        bytes_read=0,
                        error_code=None,
                        fallback_decision="follow_signed_redirect",
                        fingerprint=fingerprint,
                    )
                else:
                    try:
                        result = await _stream_verified_response(
                            response,
                            sink=sink,
                            expected_hash=expected_hash,
                            expected_family=expected_family,
                            expected_magic=expected_magic,
                            max_bytes=max_bytes,
                            operation=operation,
                            route=normalized_route,
                            transport=current_transport,
                            capability=capability,
                            correlation_id=correlation_id,
                            fingerprint=fingerprint,
                            fallback_decision=(
                                fallback_decision_on_404 if response.status_code == 404 else "none"
                            ),
                            signed=current_transport == "signed-cdn",
                            secret_values=secret_values,
                        )
                        return result
                    except VirusTotalHttpError as caught_error:
                        caught_error.route_missing = _is_route_missing_error(caught_error)
                        raise
        except VirusTotalError as caught_error:
            _set_error_route(caught_error, route, normalized_route)
            raise
        except httpx.ReadError:
            read_error = VirusTotalDownloadTruncatedError(
                "Le flux VirusTotal s'est interrompu avant sa fin.",
                operation=operation,
                transport=current_transport,
                route=route,
                route_path=normalized_route,
                correlation_id=correlation_id,
                fingerprint=fingerprint,
            )
            _log_vt_exchange(
                correlation_id=correlation_id,
                capability=capability,
                route=normalized_route,
                transport=current_transport,
                status_code=response.status_code if response is not None else None,
                content_type_class=(
                    _content_type_class(response.headers.get("Content-Type"))
                    if response is not None
                    else None
                ),
                location_host=location_host,
                bytes_read=0,
                error_code=read_error.code,
                fallback_decision="none",
                fingerprint=fingerprint,
            )
            raise read_error from None
        except httpx.TimeoutException:
            timeout_error = VirusTotalTransportError(
                "Le téléchargement VirusTotal a expiré.",
                code="virustotal_download_timeout",
                retryable=True,
                operation=operation,
                transport=current_transport,
                route=route,
                route_path=normalized_route,
                correlation_id=correlation_id,
                fingerprint=fingerprint,
            )
            _log_vt_exchange(
                correlation_id=correlation_id,
                capability=capability,
                route=normalized_route,
                transport=current_transport,
                status_code=response.status_code if response is not None else None,
                content_type_class=(
                    _content_type_class(response.headers.get("Content-Type"))
                    if response is not None
                    else None
                ),
                location_host=location_host,
                bytes_read=0,
                error_code=timeout_error.code,
                fallback_decision="none",
                fingerprint=fingerprint,
            )
            raise timeout_error from None
        except httpx.TransportError:
            transport_error = VirusTotalTransportError(
                "Le transport du téléchargement VirusTotal a échoué.",
                code="virustotal_download_transport_error",
                retryable=True,
                operation=operation,
                transport=current_transport,
                route=route,
                route_path=normalized_route,
                correlation_id=correlation_id,
                fingerprint=fingerprint,
            )
            _log_vt_exchange(
                correlation_id=correlation_id,
                capability=capability,
                route=normalized_route,
                transport=current_transport,
                status_code=response.status_code if response is not None else None,
                content_type_class=(
                    _content_type_class(response.headers.get("Content-Type"))
                    if response is not None
                    else None
                ),
                location_host=location_host,
                bytes_read=0,
                error_code=transport_error.code,
                fallback_decision="none",
                fingerprint=fingerprint,
            )
            raise transport_error from None
        finally:
            if ephemeral_client is not None:
                await ephemeral_client.aclose()

        if next_url is not None:
            current_url = next_url
            redirects_followed += 1
            current_is_signed = True
            continue


async def _stream_verified_response(
    response: httpx.Response,
    *,
    sink: BinaryIO,
    expected_hash: str | None,
    expected_family: str | None,
    expected_magic: tuple[bytes, ...] | None = None,
    max_bytes: int,
    operation: VirusTotalOperation | None,
    route: str,
    transport: str,
    capability: str,
    correlation_id: str,
    fingerprint: str,
    fallback_decision: str,
    signed: bool = False,
    secret_values: tuple[str | None, ...] = (),
) -> VirusTotalDownloadResult:
    status_code = response.status_code
    content_type_class = _content_type_class(response.headers.get("Content-Type"))
    location_host = _url_host(response.headers.get("Location"))
    issuer = "unknown"
    error: VirusTotalError
    if 300 <= response.status_code < 400:
        error = VirusTotalUnexpectedRedirectError(
            "Le téléchargement VirusTotal a été redirigé.",
            status_code=response.status_code,
            operation=operation,
            transport=transport,
            route=route,
            content_type_class=content_type_class,
            location_host=location_host,
            correlation_id=correlation_id,
            fingerprint=fingerprint,
        )
        _log_vt_exchange(
            correlation_id=correlation_id,
            capability=capability,
            route=route,
            transport=transport,
            status_code=status_code,
            content_type_class=content_type_class,
            location_host=location_host,
            bytes_read=0,
            error_code=error.code,
            fallback_decision=fallback_decision,
            fingerprint=fingerprint,
        )
        raise error
    if response.status_code >= 400:
        body = await _read_error_prefix(response)
        issuer = _response_issuer(response.headers, body)
        if signed and response.status_code in {401, 403, 404}:
            error = VirusTotalSignedUrlExpiredError(
                "L'URL signée de téléchargement a expiré ou a été refusée.",
                status_code=response.status_code,
                operation=operation,
                transport=transport,
                route=route,
                content_type_class=content_type_class,
                location_host=location_host,
                bytes_read=len(body),
                correlation_id=correlation_id,
                issuer=issuer,
                response_origin=SampleAcquisitionResponseOrigin.SIGNED_CDN,
                fingerprint=fingerprint,
            )
        else:
            error = _http_error(
                response.status_code,
                _retry_after(response.headers.get("Retry-After")),
                body=body,
                operation=operation,
                route=route,
                transport=transport,
                content_type_class=content_type_class,
                location_host=location_host,
                bytes_read=len(body),
                correlation_id=correlation_id,
                issuer=issuer,
                fingerprint=fingerprint,
                fallback_decision=fallback_decision,
                secret_values=secret_values,
            )
        _log_vt_exchange(
            correlation_id=correlation_id,
            capability=capability,
            route=route,
            transport=transport,
            status_code=status_code,
            content_type_class=content_type_class,
            location_host=location_host,
            bytes_read=len(body),
            error_code=error.code,
            fallback_decision=fallback_decision,
            fingerprint=fingerprint,
        )
        raise error
    if status_code == 204:
        error = VirusTotalDownloadEmptyError(
            "La route de téléchargement VirusTotal a renvoyé un contenu vide.",
            status_code=status_code,
            operation=operation,
            transport=transport,
            route=route,
            content_type_class=content_type_class,
            bytes_read=0,
            correlation_id=correlation_id,
            fingerprint=fingerprint,
        )
        _log_vt_exchange(
            correlation_id=correlation_id,
            capability=capability,
            route=route,
            transport=transport,
            status_code=status_code,
            content_type_class=content_type_class,
            location_host=location_host,
            bytes_read=0,
            error_code=error.code,
            fallback_decision=fallback_decision,
            fingerprint=fingerprint,
        )
        raise error
    if status_code < 200 or status_code >= 300:
        error = VirusTotalHttpError(
            "La route de téléchargement VirusTotal a renvoyé un statut inattendu.",
            code="virustotal_download_http_status_unexpected",
            status_code=status_code,
            operation=operation,
            transport=transport,
            route=route,
            content_type_class=content_type_class,
            location_host=location_host,
            bytes_read=0,
            correlation_id=correlation_id,
            fingerprint=fingerprint,
        )
        _log_vt_exchange(
            correlation_id=correlation_id,
            capability=capability,
            route=route,
            transport=transport,
            status_code=status_code,
            content_type_class=content_type_class,
            location_host=location_host,
            bytes_read=0,
            error_code=error.code,
            fallback_decision=fallback_decision,
            fingerprint=fingerprint,
        )
        raise error
    if content_type_class in {"html", "json"}:
        error = VirusTotalDownloadNotBinaryError(
            "La route de téléchargement VirusTotal a renvoyé une page HTML ou JSON.",
            status_code=status_code,
            operation=operation,
            transport=transport,
            route=route,
            content_type_class=content_type_class,
            bytes_read=0,
            correlation_id=correlation_id,
            fingerprint=fingerprint,
        )
        _log_vt_exchange(
            correlation_id=correlation_id,
            capability=capability,
            route=route,
            transport=transport,
            status_code=status_code,
            content_type_class=content_type_class,
            location_host=location_host,
            bytes_read=0,
            error_code=error.code,
            fallback_decision=fallback_decision,
            fingerprint=fingerprint,
        )
        raise error
    md5 = hashlib.md5()
    sha1 = hashlib.sha1()
    sha256 = hashlib.sha256()
    size = 0
    content_length = _content_length(response.headers.get("Content-Length"))
    prefix = bytearray()
    prefix_flushed = False
    sniff_length = 16
    async for chunk in _iter_verified_download_chunks(
        response,
        operation=operation,
        route=route,
        transport=transport,
        capability=capability,
        correlation_id=correlation_id,
        fingerprint=fingerprint,
        fallback_decision=fallback_decision,
    ):
        size += len(chunk)
        if size > max_bytes:
            error = VirusTotalDownloadTooLargeError(
                "Le téléchargement VirusTotal dépasse la limite configurée.",
                status_code=status_code,
                operation=operation,
                route=route,
                transport=transport,
                content_type_class=content_type_class,
                bytes_read=size,
                correlation_id=correlation_id,
                fingerprint=fingerprint,
            )
            _log_vt_exchange(
                correlation_id=correlation_id,
                capability=capability,
                route=route,
                transport=transport,
                status_code=status_code,
                content_type_class=content_type_class,
                location_host=location_host,
                bytes_read=size,
                error_code=error.code,
                fallback_decision=fallback_decision,
                fingerprint=fingerprint,
            )
            raise error
        md5.update(chunk)
        sha1.update(chunk)
        sha256.update(chunk)
        if not prefix_flushed:
            needed = max(0, sniff_length - len(prefix))
            prefix.extend(chunk[:needed])
            remainder = chunk[needed:]
            if len(prefix) >= sniff_length:
                error_class = _sniff_binary_error(bytes(prefix), content_type_class)
                if error_class is not None:
                    error = VirusTotalDownloadNotBinaryError(
                        "La réponse VirusTotal contient du JSON ou du HTML, "
                        "pas des octets binaires.",
                        status_code=status_code,
                        operation=operation,
                        route=route,
                        transport=transport,
                        content_type_class=error_class,
                        bytes_read=size,
                        correlation_id=correlation_id,
                        fingerprint=fingerprint,
                    )
                    _log_vt_exchange(
                        correlation_id=correlation_id,
                        capability=capability,
                        route=route,
                        transport=transport,
                        status_code=status_code,
                        content_type_class=error_class,
                        location_host=location_host,
                        bytes_read=size,
                        error_code=error.code,
                        fallback_decision=fallback_decision,
                        fingerprint=fingerprint,
                    )
                    raise error
                if expected_magic is not None and not any(
                    bytes(prefix).startswith(magic) for magic in expected_magic
                ):
                    error = VirusTotalPayloadError(
                        "Le contenu reçu n'a pas une signature PCAP ou PCAPNG reconnue.",
                        code="virustotal_pcap_magic_invalid",
                        status_code=status_code,
                        operation=operation,
                        route=route,
                        transport=transport,
                        content_type_class=content_type_class,
                        bytes_read=size,
                        correlation_id=correlation_id,
                        fingerprint=fingerprint,
                    )
                    _log_vt_exchange(
                        correlation_id=correlation_id,
                        capability=capability,
                        route=route,
                        transport=transport,
                        status_code=status_code,
                        content_type_class=content_type_class,
                        location_host=location_host,
                        bytes_read=size,
                        error_code=error.code,
                        fallback_decision=fallback_decision,
                        fingerprint=fingerprint,
                    )
                    raise error
                sink.write(prefix)
                sink.write(remainder)
                prefix_flushed = True
            elif remainder:
                # The initial prefix is kept in memory until enough bytes exist
                # to distinguish JSON/HTML and validate a PCAP magic number.
                prefix.extend(remainder)
        else:
            sink.write(chunk)

    if not prefix_flushed:
        prefix_bytes = bytes(prefix)
        error_class = _sniff_binary_error(prefix_bytes, content_type_class)
        if error_class is not None:
            error = VirusTotalDownloadNotBinaryError(
                "La réponse VirusTotal contient du JSON ou du HTML, pas des octets de fichier.",
                status_code=status_code,
                operation=operation,
                route=route,
                transport=transport,
                content_type_class=error_class,
                bytes_read=size,
                correlation_id=correlation_id,
                fingerprint=fingerprint,
            )
            _log_vt_exchange(
                correlation_id=correlation_id,
                capability=capability,
                route=route,
                transport=transport,
                status_code=status_code,
                content_type_class=error_class,
                location_host=location_host,
                bytes_read=size,
                error_code=error.code,
                fallback_decision=fallback_decision,
                fingerprint=fingerprint,
            )
            raise error
        if expected_magic is not None and not any(
            prefix_bytes.startswith(magic) for magic in expected_magic
        ):
            error = VirusTotalPayloadError(
                "Le contenu reçu n'a pas une signature PCAP ou PCAPNG reconnue.",
                code="virustotal_pcap_magic_invalid",
                status_code=status_code,
                operation=operation,
                route=route,
                transport=transport,
                content_type_class=content_type_class,
                bytes_read=size,
                correlation_id=correlation_id,
                fingerprint=fingerprint,
            )
            _log_vt_exchange(
                correlation_id=correlation_id,
                capability=capability,
                route=route,
                transport=transport,
                status_code=status_code,
                content_type_class=content_type_class,
                location_host=location_host,
                bytes_read=size,
                error_code=error.code,
                fallback_decision=fallback_decision,
                fingerprint=fingerprint,
            )
            raise error
        if prefix:
            sink.write(prefix)

    if size == 0:
        error_type: type[VirusTotalError] = (
            VirusTotalDownloadTruncatedError
            if content_length not in {None, 0}
            else VirusTotalDownloadEmptyError
        )
        error = error_type(
            "Le téléchargement VirusTotal n'a renvoyé aucun octet exploitable.",
            status_code=status_code,
            operation=operation,
            route=route,
            transport=transport,
            content_type_class=content_type_class,
            bytes_read=0,
            correlation_id=correlation_id,
            fingerprint=fingerprint,
        )
        _log_vt_exchange(
            correlation_id=correlation_id,
            capability=capability,
            route=route,
            transport=transport,
            status_code=status_code,
            content_type_class=content_type_class,
            location_host=location_host,
            bytes_read=0,
            error_code=error.code,
            fallback_decision=fallback_decision,
            fingerprint=fingerprint,
        )
        raise error
    if content_length is _INVALID_CONTENT_LENGTH or (
        content_length is not None and content_length != size
    ):
        error_type = (
            VirusTotalDownloadTruncatedError
            if isinstance(content_length, int) and content_length > size
            else VirusTotalDownloadLengthMismatchError
        )
        error = error_type(
            "La longueur annoncée par la réponse VirusTotal ne correspond pas aux octets lus.",
            status_code=status_code,
            operation=operation,
            route=route,
            transport=transport,
            content_type_class=content_type_class,
            bytes_read=size,
            correlation_id=correlation_id,
            fingerprint=fingerprint,
        )
        _log_vt_exchange(
            correlation_id=correlation_id,
            capability=capability,
            route=route,
            transport=transport,
            status_code=status_code,
            content_type_class=content_type_class,
            location_host=location_host,
            bytes_read=size,
            error_code=error.code,
            fallback_decision=fallback_decision,
            fingerprint=fingerprint,
        )
        raise error
    computed = {"md5": md5.hexdigest(), "sha1": sha1.hexdigest(), "sha256": sha256.hexdigest()}
    if expected_hash is not None and expected_family is not None:
        if computed[expected_family] != expected_hash:
            error = VirusTotalDownloadHashMismatchError(
                "Les octets téléchargés ne correspondent pas au hash demandé.",
                status_code=status_code,
                operation=operation,
                route=route,
                transport=transport,
                content_type_class=content_type_class,
                bytes_read=size,
                correlation_id=correlation_id,
                fingerprint=fingerprint,
            )
            _log_vt_exchange(
                correlation_id=correlation_id,
                capability=capability,
                route=route,
                transport=transport,
                status_code=status_code,
                content_type_class=content_type_class,
                location_host=location_host,
                bytes_read=size,
                error_code=error.code,
                fallback_decision=fallback_decision,
                fingerprint=fingerprint,
            )
            raise error
    _log_vt_exchange(
        correlation_id=correlation_id,
        capability=capability,
        route=route,
        transport=transport,
        status_code=status_code,
        content_type_class=content_type_class,
        location_host=location_host,
        bytes_read=size,
        error_code=None,
        fallback_decision=fallback_decision,
        fingerprint=fingerprint,
    )
    return VirusTotalDownloadResult(
        md5=computed["md5"], sha1=computed["sha1"], sha256=computed["sha256"], size=size
    )


async def _iter_verified_download_chunks(
    response: httpx.Response,
    *,
    operation: VirusTotalOperation | None,
    route: str,
    transport: str,
    capability: str,
    correlation_id: str,
    fingerprint: str,
    fallback_decision: str,
) -> AsyncIterator[bytes]:
    """Yield payload chunks and retain the actual byte count on stream errors."""
    bytes_read = 0
    try:
        async for chunk in response.aiter_bytes():
            bytes_read += len(chunk)
            yield chunk
    except httpx.ReadError:
        error: VirusTotalError = VirusTotalDownloadTruncatedError(
            "Le flux VirusTotal s'est interrompu avant sa fin.",
            status_code=response.status_code,
            operation=operation,
            transport=transport,
            route=route,
            content_type_class=_content_type_class(response.headers.get("Content-Type")),
            location_host=_url_host(response.headers.get("Location")),
            bytes_read=bytes_read,
            correlation_id=correlation_id,
            fingerprint=fingerprint,
        )
        decision = "stream_interrupted"
    except httpx.TimeoutException:
        error = VirusTotalTransportError(
            "La lecture du flux VirusTotal a expiré.",
            code="virustotal_download_timeout",
            retryable=True,
            status_code=response.status_code,
            operation=operation,
            transport=transport,
            route=route,
            content_type_class=_content_type_class(response.headers.get("Content-Type")),
            location_host=_url_host(response.headers.get("Location")),
            bytes_read=bytes_read,
            correlation_id=correlation_id,
            fingerprint=fingerprint,
        )
        decision = "stream_timeout"
    except httpx.TransportError:
        error = VirusTotalTransportError(
            "Le transport du flux VirusTotal a échoué.",
            code="virustotal_download_transport_error",
            retryable=True,
            status_code=response.status_code,
            operation=operation,
            transport=transport,
            route=route,
            content_type_class=_content_type_class(response.headers.get("Content-Type")),
            location_host=_url_host(response.headers.get("Location")),
            bytes_read=bytes_read,
            correlation_id=correlation_id,
            fingerprint=fingerprint,
        )
        decision = "stream_transport_error"
    else:
        return

    _log_vt_exchange(
        correlation_id=correlation_id,
        capability=capability,
        route=route,
        transport=transport,
        status_code=response.status_code,
        content_type_class=_content_type_class(response.headers.get("Content-Type")),
        location_host=_url_host(response.headers.get("Location")),
        bytes_read=bytes_read,
        error_code=error.code,
        fallback_decision=decision or fallback_decision,
        fingerprint=fingerprint,
    )
    raise error from None


def _bounded(value: int, lower: int, upper: int, name: str) -> int:
    if value < lower or value > upper:
        raise ValueError(f"{name} est hors limites")
    return value


async def _read_bounded(response: httpx.Response, maximum: int) -> bytes:
    chunks: list[bytes] = []
    size = 0
    async for chunk in response.aiter_bytes():
        size += len(chunk)
        if size > maximum:
            raise VirusTotalResponseTooLargeError(
                "La réponse VirusTotal dépasse la limite configurée."
            )
        chunks.append(chunk)
    return b"".join(chunks)


async def _read_error_prefix(response: httpx.Response) -> bytes:
    chunks: list[bytes] = []
    remaining = _MAX_ERROR_BODY_BYTES
    async for chunk in response.aiter_bytes():
        chunks.append(chunk[:remaining])
        remaining -= min(len(chunk), remaining)
        if remaining == 0:
            break
    return b"".join(chunks)


def _json_object(body: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise VirusTotalJsonError("VirusTotal a renvoyé un JSON invalide.") from exc
    if not isinstance(payload, dict):
        raise VirusTotalPayloadError("La réponse VirusTotal n'est pas un objet JSON.")
    return payload


def _parse_file(payload: dict[str, Any]) -> VirusTotalFile:
    data_raw = payload.get("data")
    if (
        not isinstance(data_raw, dict)
        or not isinstance(data_raw.get("id"), str)
        or not isinstance(data_raw.get("type"), str)
    ):
        raise VirusTotalPayloadError("La réponse VirusTotal n'identifie pas un fichier.")
    data: dict[str, Any] = data_raw
    attrs_raw = data.get("attributes")
    attrs: dict[str, Any] = attrs_raw if isinstance(attrs_raw, dict) else {}
    stats = attrs.get("last_analysis_stats")
    if stats is not None and (
        not isinstance(stats, dict)
        or not all(isinstance(k, str) and isinstance(v, int) for k, v in stats.items())
    ):
        raise VirusTotalPayloadError("Les statistiques d'analyse VirusTotal sont invalides.")
    tags = attrs.get("tags", [])
    if not isinstance(tags, list) or not all(isinstance(tag, str) for tag in tags):
        raise VirusTotalPayloadError("Les tags VirusTotal sont invalides.")
    lookup = attrs.get("sha256") or attrs.get("sha1") or attrs.get("md5") or data["id"]
    if not isinstance(lookup, str):
        raise VirusTotalPayloadError("Le fichier VirusTotal n'a pas de valeur d'identification.")
    return VirusTotalFile(
        id=data["id"],
        type=data["type"],
        lookup_value=lookup,
        meaningful_name=attrs.get("meaningful_name")
        if isinstance(attrs.get("meaningful_name"), str)
        else None,
        type_description=attrs.get("type_description")
        if isinstance(attrs.get("type_description"), str)
        else None,
        size=attrs.get("size") if isinstance(attrs.get("size"), int) else None,
        last_analysis_stats=stats,
        first_submission_date=attrs.get("first_submission_date")
        if isinstance(attrs.get("first_submission_date"), int)
        else None,
        last_submission_date=attrs.get("last_submission_date")
        if isinstance(attrs.get("last_submission_date"), int)
        else None,
        last_modification_date=attrs.get("last_modification_date")
        if isinstance(attrs.get("last_modification_date"), int)
        else None,
        tags=tuple(tags),
        vhash=_string_value(attrs.get("vhash")),
        imphash=_string_value(attrs.get("imphash")),
        ssdeep=_string_value(attrs.get("ssdeep")),
        tlsh=_string_value(attrs.get("tlsh")),
        main_icon_dhash=_nested_string(attrs.get("main_icon"), "dhash"),
        rich_header_hash=_nested_string(attrs.get("pe_info"), "rich_header_hash"),
    )


def _string_value(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _nested_string(value: object, key: str) -> str | None:
    return value.get(key) if isinstance(value, dict) and isinstance(value.get(key), str) else None


def _is_v2_file_report(payload: dict[str, Any]) -> bool:
    return isinstance(payload.get("response_code"), int) and "scans" in payload


def _parse_v2_file(payload: dict[str, Any], lookup: str) -> VirusTotalFile:
    if payload.get("response_code") != 1:
        raise VirusTotalPayloadError("Le rapport fichier v2 VirusTotal est introuvable.")
    values = {
        key: payload.get(key) for key in ("md5", "sha1", "sha256", "verbose_msg", "scan_date")
    }
    stats = {"malicious": payload.get("positives", 0), "total": payload.get("total", 0)}
    if not all(isinstance(stats[key], int) for key in stats):
        raise VirusTotalPayloadError("Les statistiques du rapport v2 sont invalides.")
    identifier = next(
        (values[key] for key in ("sha256", "sha1", "md5") if isinstance(values[key], str)), lookup
    )
    if not isinstance(identifier, str):
        raise VirusTotalPayloadError("Le rapport v2 n'a pas d'identifiant fichier.")
    return VirusTotalFile(
        id=identifier,
        type="file",
        lookup_value=identifier,
        meaningful_name=None,
        type_description=None,
        last_analysis_stats=stats,
    )


def _parse_items(payload: dict[str, Any]) -> list[dict[str, Any]]:
    data = payload.get("data")
    if not isinstance(data, list) or not all(isinstance(item, dict) for item in data):
        raise VirusTotalPayloadError("La réponse paginée VirusTotal est incompatible.")
    return data


def _behaviour_summary(item: dict[str, Any]) -> VirusTotalBehaviourSummary:
    raw_attributes = item.get("attributes")
    attributes = raw_attributes if isinstance(raw_attributes, dict) else {}
    sandbox_value = attributes.get("sandbox_name", attributes.get("sandbox"))
    sandbox = sandbox_value if isinstance(sandbox_value, str) else None
    date_value = attributes.get("analysis_date", attributes.get("date"))
    date = date_value if isinstance(date_value, str) or type(date_value) is int else None
    raw_tags = attributes.get("tags")
    tags = (
        tuple(tag for tag in raw_tags if isinstance(tag, str)) if isinstance(raw_tags, list) else ()
    )
    metadata_keys = {
        "sandbox_name",
        "sandbox",
        "analysis_date",
        "date",
        "tags",
        "has_pcap",
        "has_html_report",
        "behash",
    }
    available_sections = tuple(
        key
        for key, value in attributes.items()
        if key not in metadata_keys and value not in (None, [], {})
    )
    return VirusTotalBehaviourSummary(
        sandbox=sandbox,
        date=date,
        tags=tags,
        has_pcap=attributes.get("has_pcap") is True,
        has_html_report=attributes.get("has_html_report") is True,
        available_sections=available_sections,
    )


def _next_cursor(payload: dict[str, Any]) -> str | None:
    meta = payload.get("meta")
    if isinstance(meta, dict) and isinstance(meta.get("cursor"), str) and meta["cursor"]:
        return str(meta["cursor"])
    links = payload.get("links")
    if isinstance(links, dict) and isinstance(links.get("next"), str):
        # A next URL is not a cursor and must never be reconstructed or followed.
        return None
    return None


def _http_error(
    status: int,
    retry_after: float | None,
    *,
    body: bytes = b"",
    operation: VirusTotalOperation | None = None,
    route: str | None = None,
    route_path: str | None = None,
    transport: str | None = None,
    content_type_class: str | None = None,
    location_host: str | None = None,
    bytes_read: int | None = None,
    correlation_id: str | None = None,
    fingerprint: str | None = None,
    issuer: str | None = None,
    fallback_decision: str | None = None,
    secret_values: tuple[str | None, ...] = (),
) -> VirusTotalHttpError:
    raw_code: str | None = None
    raw_message: str | None = None
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        payload = None
    if isinstance(payload, dict):
        raw_error = payload.get("error")
        if isinstance(raw_error, dict):
            code_value = raw_error.get("code")
            message_value = raw_error.get("message")
            raw_code = (
                _safe_error_detail(code_value, 160, secret_values)
                if isinstance(code_value, str)
                else None
            )
            raw_message = (
                _safe_error_detail(message_value, 1024, secret_values)
                if isinstance(message_value, str)
                else None
            )
        if raw_message is None:
            fallback_message = payload.get("message", payload.get("detail"))
            if isinstance(fallback_message, str):
                raw_message = _safe_error_detail(fallback_message, 1024, secret_values)
    normalized = f"{raw_code or ''} {raw_message or ''}".lower().replace("_", " ").replace("-", " ")
    issuer = issuer or _response_issuer({}, body)
    route_missing = (
        status == 404
        and issuer == "proxy-likely"
        and any(
            marker in normalized
            for marker in (
                "route not found",
                "route missing",
                "no route",
                "endpoint not found",
                "unknown route",
                "cannot get",
            )
        )
    )
    metadata: dict[str, Any] = {
        "status_code": status,
        "retry_after": retry_after,
        "error_code": raw_code,
        "error_message": raw_message,
        "operation": operation,
        "route": route,
        "route_path": route_path or route,
        "transport": transport,
        "content_type_class": content_type_class,
        "location_host": location_host,
        "bytes_read": bytes_read,
        "correlation_id": correlation_id,
        "issuer": issuer,
        "fingerprint": fingerprint,
        "route_missing": route_missing,
        "fallback_decision": fallback_decision,
        "response_origin": _sample_acquisition_response_origin(transport, issuer),
    }
    if status == 429 or "quota" in normalized or "rate limit" in normalized:
        return VirusTotalQuotaExceededError(
            "VirusTotal a signalé une limite de quota ou de débit.",
            retryable=status == 429,
            **metadata,
        )
    if status == 403 or any(
        phrase in normalized
        for phrase in ("not authorized", "notauthorized", "unauthorized", "plan")
    ):
        return VirusTotalNotAuthorizedError(
            "VirusTotal n'autorise pas cette opération avec le plan actuel.",
            **metadata,
        )
    mapping = {
        401: ("virustotal_upstream_unauthorized", False),
        403: ("virustotal_upstream_forbidden", False),
        404: (
            "virustotal_proxy_route_not_found"
            if route_missing
            else "virustotal_http_404_unattributed"
            if operation is VirusTotalOperation.FILE_DOWNLOAD and issuer != "upstream-vt-likely"
            else "virustotal_upstream_not_found"
            if operation is VirusTotalOperation.FILE_DOWNLOAD
            else "virustotal_not_found",
            False,
        ),
        407: ("virustotal_proxy_auth_required", False),
        429: ("virustotal_rate_limited", True),
    }
    code, retryable = mapping.get(status, ("virustotal_upstream_error", status >= 500))
    return VirusTotalHttpError(
        "VirusTotal ou le proxy a refusé la requête.",
        code=code,
        retryable=retryable,
        **metadata,
    )


def _safe_error_detail(value: str, maximum: int, secret_values: tuple[str | None, ...] = ()) -> str:
    bounded = value[:maximum]
    bounded = re.sub(r"https?://[^\s\"'<>]+", "[redacted-url]", bounded, flags=re.IGNORECASE)
    bounded = re.sub(
        r"(?i)(\b(?:x-apikey|apikey|api[_ -]key)(?:\s*[:=]\s*|\s+))[^\s,;&\"'<>]+",
        r"\1[REDACTED]",
        bounded,
    )
    bounded = re.sub(
        r"(?i)(\b(?:x-)?apikey\b(?:\s*[:=]\s*|\s+))[a-f0-9]{32,}",
        r"\1[REDACTED]",
        bounded,
    )
    for secret in secret_values:
        if secret:
            bounded = re.sub(re.escape(secret), "[redacted-key]", bounded, flags=re.IGNORECASE)
    return bounded


def _retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(value)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            return max(0.0, (parsed - datetime.now(UTC)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return None


def _is_tls_error(error: BaseException) -> bool:
    current: BaseException | None = error
    for _ in range(3):
        if current is None:
            return False
        if isinstance(current, ssl.SSLError):
            return True
        current = current.__cause__ or current.__context__
    return False


class VirusTotalRetrohuntProxyAdapter:
    """Capability-gated adapter for a separately verified VirusTotal v3 client.

    This adapter deliberately has no HTTP endpoint or payload assumptions.
    A provider-specific client can be injected only after its proxy contract
    has been verified. Until then, an absent capability, proxy route, or
    delegate makes Retrohunt unavailable without affecting other VT tools.
    """

    def __init__(
        self,
        *,
        capabilities: VirusTotalCapabilities,
        routing_policy: VirusTotalRoutingPolicy,
        proxy_client: VirusTotalRetrohuntPort | None = None,
    ) -> None:
        self._capabilities = capabilities
        self._routing_policy = routing_policy
        self._proxy_client = proxy_client

    @property
    def available(self) -> bool:
        route = self._routing_policy.route_for(VirusTotalCapability.RETROHUNT)
        return bool(
            self._capabilities.retrohunt
            and self._proxy_client is not None
            and self._proxy_client.available
            and route is not None
            and route.primary.transport is VirusTotalTransportKind.PROXY
            and all(step.transport is VirusTotalTransportKind.PROXY for step in route.steps())
        )

    async def submit_retrohunt(self, request: RetrohuntSubmitRequest) -> RetrohuntProviderJob:
        return await self._require_proxy().submit_retrohunt(request)

    async def retrohunt_status(self, job_id: str) -> RetrohuntProviderJob:
        return await self._require_proxy().retrohunt_status(job_id)

    async def retrohunt_results(self, job_id: str) -> RetrohuntProviderResults:
        return await self._require_proxy().retrohunt_results(job_id)

    def _require_proxy(self) -> VirusTotalRetrohuntPort:
        if not self._capabilities.retrohunt:
            raise VirusTotalCapabilityDisabledError(
                "La capability Retrohunt VirusTotal est désactivée."
            )
        if not self.available:
            raise VirusTotalRouteUnavailableError(
                "Aucune route proxy VirusTotal v3 vérifiée n'expose Retrohunt."
            )
        assert self._proxy_client is not None
        return self._proxy_client
