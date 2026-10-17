from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Protocol

from cti_app.domain.shodan import (
    ShodanCapability,
    ShodanCountResult,
    ShodanDomainView,
    ShodanHostView,
    ShodanSearchResult,
    normalize_domain_value,
    normalize_ip_value,
)


class ShodanError(RuntimeError):
    code = "shodan_error"
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
        upstream_message: str | None = None,
        plan: str | None = None,
        query_credits: int | None = None,
    ) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code
        if retryable is not None:
            self.retryable = retryable
        self.status_code = status_code
        self.retry_after = retry_after
        self.upstream_message = upstream_message
        self.plan = plan
        self.query_credits = query_credits


class ShodanCapabilityDisabledError(ShodanError):
    code = "shodan_capability_disabled"


class ShodanNotConfiguredError(ShodanError):
    code = "shodan_not_configured"


class ShodanQuotaError(ShodanError):
    code = "shodan_quota_exhausted"


class ShodanNotFoundError(ShodanError):
    code = "shodan_not_found"


class ShodanInvalidInputError(ShodanError):
    code = "shodan_invalid_input"


class ShodanHttpError(ShodanError):
    pass


class ShodanTransportError(ShodanError):
    pass


class ShodanResponseTooLargeError(ShodanError):
    code = "shodan_response_too_large"


class ShodanJsonError(ShodanError):
    code = "shodan_json_invalid"


class ShodanPayloadError(ShodanError):
    code = "shodan_payload_invalid"


@dataclass(frozen=True, slots=True)
class ShodanCapabilities:
    host: bool = False
    search: bool = False
    domain: bool = False
    count: bool = False

    def is_enabled(self, capability: ShodanCapability) -> bool:
        return bool(getattr(self, capability.value))


@dataclass(frozen=True, slots=True)
class ShodanBudgetEstimate:
    """Conservative query-credit reservations made before a passive call."""

    host: int = 1
    search: int = 1
    domain: int = 1
    count: int = 1

    def __post_init__(self) -> None:
        if any(value < 0 for value in (self.host, self.search, self.domain, self.count)):
            raise ValueError("Shodan credit estimates must be non-negative")

    def credits_for(self, capability: ShodanCapability) -> int:
        return int(getattr(self, capability.value))


class ShodanPort(Protocol):
    async def host(self, ip: str) -> ShodanHostView: ...

    async def search(self, query: str, page: int = 1) -> ShodanSearchResult: ...

    async def domain(self, domain: str) -> ShodanDomainView: ...


class ShodanCountPort(Protocol):
    """Optional count capability for ports that support query estimation."""

    async def count(self, query: str) -> ShodanCountResult: ...


MAX_SEARCH_QUERY_LENGTH = 500
MAX_SEARCH_PAGES = 100
_FILTER_TOKEN = re.compile(r"(?<![a-z0-9_-])([a-z][a-z0-9_-]*)\s*:", re.IGNORECASE)
_ACTIVE_SCAN_FILTERS = frozenset(
    {"active_scan", "on_demand_scan", "scan", "scan_id", "scan_result", "scan_status", "scan_time"}
)


def validate_shodan_ip(value: str) -> str:
    try:
        return normalize_ip_value(value)
    except (AttributeError, ValueError):
        raise ShodanInvalidInputError("Une adresse IP valide est requise.") from None


def validate_shodan_domain(value: str) -> str:
    try:
        return normalize_domain_value(value)
    except (AttributeError, ValueError):
        raise ShodanInvalidInputError("Un nom de domaine valide est requis.") from None


def validate_shodan_search_query(value: str) -> str:
    if not isinstance(value, str):
        raise ShodanInvalidInputError("La requête Shodan doit être du texte.")
    if any(unicodedata.category(character) == "Cc" for character in value):
        raise ShodanInvalidInputError("La requête Shodan contient un caractère de contrôle.")
    candidate = value.strip()
    if not candidate:
        raise ShodanInvalidInputError("La requête Shodan est vide.")
    if len(candidate) > MAX_SEARCH_QUERY_LENGTH:
        raise ShodanInvalidInputError("La requête Shodan dépasse 500 caractères.")
    for match in _FILTER_TOKEN.finditer(candidate):
        field = match.group(1).lower()
        if field in _ACTIVE_SCAN_FILTERS or field.startswith("scan_"):
            raise ShodanInvalidInputError(
                "Les filtres de scan actif ne sont pas autorisés dans Shodan search."
            )
    return candidate


def validate_shodan_page(page: int, *, max_pages: int = MAX_SEARCH_PAGES) -> int:
    if isinstance(page, bool) or not isinstance(page, int) or page < 1 or page > max_pages:
        raise ShodanInvalidInputError("La page Shodan est hors limites.")
    return page
