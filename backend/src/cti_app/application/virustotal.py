from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol

from cti_app.domain.virustotal import (
    VirusTotalCapability,
    VirusTotalEndpointVariant,
    VirusTotalFallbackTrigger,
    VirusTotalTransportKind,
)


class VirusTotalError(RuntimeError):
    code = "virustotal_error"
    retryable = False
    status_code: int | None = None
    retry_after: float | None = None

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        retryable: bool | None = None,
        status_code: int | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code
        if retryable is not None:
            self.retryable = retryable
        self.status_code = status_code
        self.retry_after = retry_after


class VirusTotalConfigurationError(VirusTotalError):
    code = "virustotal_transport_not_configured"


class VirusTotalCapabilityDisabledError(VirusTotalError):
    code = "virustotal_capability_disabled"


class VirusTotalRouteUnavailableError(VirusTotalError):
    """The operation is authorized but no usable route is configured for it.

    Distinct from `VirusTotalCapabilityDisabledError`: a capability may be
    enabled while its route is direct-only and the direct transport is not
    wired (missing key, missing client, ...). This is a local configuration
    failure, raised before any network call.
    """

    code = "virustotal_route_unavailable"


class VirusTotalRelationNotAllowedError(VirusTotalError):
    code = "virustotal_relation_not_allowed"


class VirusTotalInvalidInputError(VirusTotalError):
    code = "virustotal_invalid_input"


class VirusTotalPayloadError(VirusTotalError):
    code = "virustotal_payload_invalid"


class VirusTotalJsonError(VirusTotalError):
    code = "virustotal_json_invalid"


class VirusTotalResponseTooLargeError(VirusTotalError):
    code = "virustotal_response_too_large"


class VirusTotalUnexpectedRedirectError(VirusTotalError):
    code = "virustotal_unexpected_redirect"


class VirusTotalHttpError(VirusTotalError):
    pass


class VirusTotalTransportError(VirusTotalError):
    pass


@dataclass(frozen=True, slots=True)
class VirusTotalCapabilities:
    """Authorization to request each operation. Says nothing about transport.

    `file_report=True` means the application may request a file report; it
    means neither "proxy is allowed" nor "direct is allowed". Which network
    path is used is decided separately by `VirusTotalRoutingPolicy`.
    """

    file_report: bool = False
    file_relationships: bool = False
    intelligence_search: bool = False
    file_download: bool = False
    submissions: bool = False
    behaviour_pcap: bool = False
    retrohunt: bool = False

    def is_enabled(self, capability: VirusTotalCapability) -> bool:
        return bool(getattr(self, capability.value))


@dataclass(frozen=True, slots=True)
class VirusTotalRouteStep:
    """One concrete hop a route may take: a transport plus a wire variant."""

    transport: VirusTotalTransportKind
    variant: VirusTotalEndpointVariant


@dataclass(frozen=True, slots=True)
class VirusTotalOperationRoute:
    """The explicit, ordered set of steps allowed for one operation.

    `primary` is always attempted first. A step in `fallbacks` is attempted
    only after the previous step failed with an outcome matching
    `fallback_trigger` (e.g. 404) — never on 403, 429, timeout, or 5xx unless
    the trigger says so. Nothing here is deduced from the presence of a proxy
    URL or an API key; both are wired separately and checked for
    availability when a step is actually reached.
    """

    primary: VirusTotalRouteStep
    fallbacks: tuple[VirusTotalRouteStep, ...] = ()
    fallback_trigger: VirusTotalFallbackTrigger = VirusTotalFallbackTrigger.NOT_FOUND

    def steps(self) -> tuple[VirusTotalRouteStep, ...]:
        return (self.primary, *self.fallbacks)

    def permits_fallback(self, error: VirusTotalError) -> bool:
        if self.fallback_trigger is VirusTotalFallbackTrigger.NOT_FOUND:
            return error.status_code == 404
        return False


@dataclass(frozen=True, slots=True)
class VirusTotalRoutingPolicy:
    """Deny-by-default map of capability -> allowed route.

    A capability absent from `routes` has no usable transport at all, even
    if enabled in `VirusTotalCapabilities` and even if a proxy or a direct
    key happens to be configured. Fully inspectable and testable without
    performing any request.
    """

    routes: Mapping[VirusTotalCapability, VirusTotalOperationRoute] = field(
        default_factory=dict
    )

    def route_for(self, capability: VirusTotalCapability) -> VirusTotalOperationRoute | None:
        return self.routes.get(capability)


class FileRelationship(StrEnum):
    CONTACTED_DOMAINS = "contacted_domains"
    CONTACTED_IPS = "contacted_ips"
    CONTACTED_URLS = "contacted_urls"
    DROPPED_FILES = "dropped_files"
    EXECUTION_PARENTS = "execution_parents"
    ITW_URLS = "itw_urls"
    EMBEDDED_URLS = "embedded_urls"
    SIMILAR_FILES = "similar_files"
    BUNDLED_FILES = "bundled_files"


@dataclass(frozen=True, slots=True)
class VirusTotalRawResponse:
    """The safe, httpx-independent shape of one successful upstream response.

    Carries only what the domain and persistence layers need to reproduce
    the transport's outcome: the exact body bytes and the HTTP status the
    server actually returned. Never carries headers, cookies, or anything
    that could leak credentials.
    """

    body: bytes
    status_code: int


@dataclass(frozen=True, slots=True)
class VirusTotalFile:
    id: str
    type: str
    lookup_value: str
    meaningful_name: str | None = None
    type_description: str | None = None
    size: int | None = None
    last_analysis_stats: dict[str, int] | None = None
    first_submission_date: int | None = None
    last_submission_date: int | None = None
    last_modification_date: int | None = None
    tags: tuple[str, ...] = ()
    vhash: str | None = None
    imphash: str | None = None
    ssdeep: str | None = None
    tlsh: str | None = None
    main_icon_dhash: str | None = None
    rich_header_hash: str | None = None


@dataclass(frozen=True, slots=True)
class VirusTotalFileReport:
    file: VirusTotalFile
    raw_json: bytes
    http_status: int
    transport: VirusTotalTransportKind
    api_generation: VirusTotalEndpointVariant


@dataclass(frozen=True, slots=True)
class VirusTotalPage:
    items: tuple[dict[str, Any], ...]
    next_cursor: str | None
    observed_count: int
    stopped_due_to_limit: bool
    exhaustive: bool
    raw_json_pages: tuple[bytes, ...]
    http_statuses: tuple[int, ...]
    limit_used: int
    transport: VirusTotalTransportKind
    api_generation: VirusTotalEndpointVariant


@dataclass(frozen=True, slots=True)
class VirusTotalSearchResult:
    items: tuple[dict[str, Any], ...]
    next_cursor: str | None
    observed_count: int
    stopped_due_to_limit: bool
    exhaustive: bool
    raw_json_pages: tuple[bytes, ...]
    http_statuses: tuple[int, ...]
    limit_used: int
    transport: VirusTotalTransportKind
    api_generation: VirusTotalEndpointVariant


class VirusTotalPort(Protocol):
    async def file_report(self, file_hash: str) -> VirusTotalFileReport: ...

    async def file_relationship(
        self,
        file_hash: str,
        relation: FileRelationship,
        *,
        limit: int | None = None,
        cursor: str | None = None,
        paginate: bool = False,
    ) -> VirusTotalPage: ...

    async def intelligence_search(
        self,
        query: str,
        *,
        limit: int | None = None,
        cursor: str | None = None,
        paginate: bool = False,
    ) -> VirusTotalSearchResult: ...


_HASH_RE = re.compile(r"^(?:[0-9a-f]{32}|[0-9a-f]{40}|[0-9a-f]{64})$", re.IGNORECASE)


def normalize_file_hash(value: str) -> str:
    candidate = value.strip().lower()
    if not _HASH_RE.fullmatch(candidate):
        raise VirusTotalInvalidInputError("Le hash fichier VirusTotal est invalide.")
    return candidate


def validate_search_query(value: str) -> str:
    candidate = value.strip()
    if not candidate:
        raise VirusTotalInvalidInputError("La requête Intelligence Search est vide.")
    return candidate
