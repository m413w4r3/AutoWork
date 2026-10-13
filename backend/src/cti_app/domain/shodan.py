from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class ShodanCapability(StrEnum):
    HOST = "host"
    SEARCH = "search"
    DOMAIN = "domain"
    COUNT = "count"


class ShodanRoute(StrEnum):
    HOST = "/shodan/host/{ip}"
    SEARCH = "/shodan/host/search"
    DOMAIN = "/dns/domain/{domain}"
    COUNT = "/shodan/host/count"
    API_INFO = "/api-info"


@dataclass(frozen=True, slots=True)
class ShodanCertificate:
    fingerprint_sha256: str | None = None
    issuer: str | None = None
    subject: str | None = None
    issued_at: datetime | None = None
    expires_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class ShodanService:
    port: int
    transport: str | None = None
    product: str | None = None
    banner: str | None = None
    banner_truncated: bool = False
    timestamp: datetime | None = None
    certificate: ShodanCertificate | None = None

    def __post_init__(self) -> None:
        if not 1 <= self.port <= 65535:
            raise ValueError("Shodan service port must be between 1 and 65535")
        if self.timestamp is not None and not _is_aware(self.timestamp):
            raise ValueError("Shodan service timestamp must be timezone-aware")


@dataclass(frozen=True, slots=True)
class ShodanServiceCapture:
    port: int
    transport: str | None
    timestamp: datetime | None
    age_seconds: float | None


@dataclass(frozen=True, slots=True)
class ShodanObservationMeta:
    captured_at: datetime
    service_captures: tuple[ShodanServiceCapture, ...]
    coverage_limits: tuple[str, ...]
    credits_consumed: int
    query_credits: int | None
    plan: str | None
    route: ShodanRoute

    def __post_init__(self) -> None:
        if not _is_aware(self.captured_at):
            raise ValueError("Shodan capture time must be timezone-aware")
        if self.credits_consumed < 0:
            raise ValueError("Shodan credits_consumed must be non-negative")
        if self.query_credits is not None and self.query_credits < 0:
            raise ValueError("Shodan query_credits must be non-negative")


@dataclass(frozen=True, slots=True)
class ShodanHostView:
    ip: str
    organization: str | None
    asn: str | None
    hostnames: tuple[str, ...]
    ports: tuple[int, ...]
    tags: tuple[str, ...]
    services: tuple[ShodanService, ...]
    meta: ShodanObservationMeta
    raw_response: bytes
    raw_sha256: str
    http_status: int

    def __post_init__(self) -> None:
        try:
            normalized = str(ipaddress.ip_address(self.ip))
        except ValueError as exc:
            raise ValueError("Shodan host view requires a valid IP address") from exc
        object.__setattr__(self, "ip", normalized)
        if not _is_sha256(self.raw_sha256):
            raise ValueError("Shodan raw_sha256 must be a lowercase SHA-256 digest")
        if not 200 <= self.http_status < 300 and self.http_status != 404:
            raise ValueError("Shodan host views require a successful or empty HTTP response")


@dataclass(frozen=True, slots=True)
class ShodanSearchResult:
    matches: tuple[ShodanHostView, ...]
    total: int
    page: int
    meta: ShodanObservationMeta
    raw_response: bytes
    raw_sha256: str
    http_status: int

    def __post_init__(self) -> None:
        if self.total < 0 or self.page < 1:
            raise ValueError("Shodan search total and page are out of range")
        if not _is_sha256(self.raw_sha256):
            raise ValueError("Shodan raw_sha256 must be a lowercase SHA-256 digest")
        if not 200 <= self.http_status < 300 and self.http_status != 404:
            raise ValueError("Shodan search results require a successful or empty response")


@dataclass(frozen=True, slots=True)
class ShodanDomainView:
    domain: str
    subdomains: tuple[str, ...]
    dns_records: tuple[dict[str, object], ...]
    meta: ShodanObservationMeta
    raw_response: bytes
    raw_sha256: str
    http_status: int

    def __post_init__(self) -> None:
        normalized = normalize_domain_value(self.domain)
        object.__setattr__(self, "domain", normalized)
        if not _is_sha256(self.raw_sha256):
            raise ValueError("Shodan raw_sha256 must be a lowercase SHA-256 digest")
        if not 200 <= self.http_status < 300 and self.http_status != 404:
            raise ValueError("Shodan domain views require a successful or empty response")


@dataclass(frozen=True, slots=True)
class ShodanCountResult:
    total: int
    query: str
    meta: ShodanObservationMeta
    raw_response: bytes
    raw_sha256: str
    http_status: int

    def __post_init__(self) -> None:
        if self.total < 0:
            raise ValueError("Shodan count total must be non-negative")
        if not _is_sha256(self.raw_sha256):
            raise ValueError("Shodan raw_sha256 must be a lowercase SHA-256 digest")
        if not 200 <= self.http_status < 300 and self.http_status != 404:
            raise ValueError("Shodan counts require a successful or empty response")


_DOMAIN_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def normalize_ip_value(value: str) -> str:
    try:
        return str(ipaddress.ip_address(value.strip()))
    except (AttributeError, ValueError) as exc:
        raise ValueError("A valid IP address is required") from exc


def normalize_domain_value(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("A valid domain name is required")
    candidate = value.strip().lower()
    if candidate.endswith("."):
        candidate = candidate[:-1]
    if not candidate or len(candidate) > 253:
        raise ValueError("A valid domain name is required")
    try:
        ascii_name = candidate.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise ValueError("A valid domain name is required") from exc
    if len(ascii_name) > 253:
        raise ValueError("A valid domain name is required")
    labels = ascii_name.split(".")
    if len(labels) < 2 or any(not _DOMAIN_LABEL.fullmatch(label) for label in labels):
        raise ValueError("A valid domain name is required")
    if all(character.isdigit() or character == "." for character in ascii_name):
        raise ValueError("A domain name is required, not an IP address")
    return ascii_name


def _is_aware(value: datetime) -> bool:
    return value.tzinfo is not None and value.utcoffset() is not None


def _is_sha256(value: str) -> bool:
    return bool(_SHA256.fullmatch(value))
