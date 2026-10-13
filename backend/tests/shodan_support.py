from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

from cti_app.application.shodan import ShodanPort
from cti_app.domain.shodan import (
    ShodanCertificate,
    ShodanCountResult,
    ShodanDomainView,
    ShodanHostView,
    ShodanObservationMeta,
    ShodanRoute,
    ShodanSearchResult,
    ShodanService,
    ShodanServiceCapture,
)

CAPTURED_AT = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)
SHARED_CERT_SHA256 = "a" * 64

HOST_JSON = {
    "ip_str": "203.0.113.10",
    "org": "Example Networks",
    "asn": "AS64500",
    "hostnames": ["edge.example.test"],
    "ports": [443, 8443],
    "tags": ["cdn", "cloud"],
    "last_update": "2026-10-12T09:00:00.000000",
    "data": [
        {
            "port": 443,
            "transport": "tcp",
            "product": "nginx",
            "timestamp": "2026-10-12T08:30:00.000000",
            "data": "HTTP/1.1 200 OK\r\nServer: nginx\r\n",
            "ssl": {
                "cert": {
                    "fingerprint": {"sha256": SHARED_CERT_SHA256},
                    "issuer": {"CN": "Example Test CA", "O": "Example"},
                    "subject": {"CN": "edge.example.test"},
                    "issued": "2025-01-01T00:00:00",
                    "expires": "2027-01-01T00:00:00",
                }
            },
        },
        {
            "port": 8443,
            "transport": "tcp",
            "product": "http-alt",
            "timestamp": "2026-10-11T20:00:00.000000",
            "data": "Alternate TLS listener",
            "ssl": {
                "cert": {
                    "fingerprint": {"sha256": SHARED_CERT_SHA256},
                    "issuer": {"CN": "Example Test CA", "O": "Example"},
                    "subject": {"CN": "edge.example.test"},
                    "issued": "2025-01-01T00:00:00",
                    "expires": "2027-01-01T00:00:00",
                }
            },
        },
    ],
}

SEARCH_JSON = {
    "total": 3,
    "matches": [
        {"ip_str": "203.0.113.10", "port": 443, "org": "Example Networks", "data": "one"},
        {"ip_str": "203.0.113.11", "port": 443, "org": "Example Networks", "data": "two"},
        {"ip_str": "203.0.113.12", "port": 8443, "data": "three"},
    ],
}

DOMAIN_JSON = {
    "domain": "example.test",
    "subdomains": ["www", "api", "vpn"],
    "data": [
        {"subdomain": "www", "type": "A", "value": "203.0.113.10"},
        {"subdomain": "api", "type": "AAAA", "value": "2001:db8::10"},
    ],
}

HOST_RESPONSE = json.dumps(HOST_JSON, separators=(",", ":")).encode()
SEARCH_RESPONSE = json.dumps(SEARCH_JSON, separators=(",", ":")).encode()
DOMAIN_RESPONSE = json.dumps(DOMAIN_JSON, separators=(",", ":")).encode()
API_INFO_RESPONSE = b'{"plan":"edu","query_credits":42,"scan_credits":3}'
HTTP_RESPONSE_FIXTURES = {
    "not_found": (404, b"No information available"),
    "unauthorized": (401, b"Invalid API key"),
    "quota": (402, b"Query credits limit reached"),
    "rate_limited": (429, b"Too many requests"),
}


def _meta(route: ShodanRoute, services: tuple[ShodanService, ...] = ()) -> ShodanObservationMeta:
    captures = tuple(
        ShodanServiceCapture(
            port=service.port,
            transport=service.transport,
            timestamp=service.timestamp,
            age_seconds=0.0 if service.timestamp else None,
        )
        for service in services
    )
    return ShodanObservationMeta(
        captured_at=CAPTURED_AT,
        service_captures=captures,
        coverage_limits=("fixture synthétique",),
        credits_consumed=1,
        query_credits=42,
        plan="edu",
        route=route,
    )


def _raw_hash(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _make_services() -> tuple[ShodanService, ...]:
    certificate = ShodanCertificate(
        fingerprint_sha256=SHARED_CERT_SHA256,
        issuer="CN=Example Test CA, O=Example",
        subject="CN=edge.example.test",
        issued_at=datetime(2025, 1, 1, tzinfo=UTC),
        expires_at=datetime(2027, 1, 1, tzinfo=UTC),
    )
    return (
        ShodanService(
            port=443,
            transport="tcp",
            product="nginx",
            banner="HTTP/1.1 200 OK\r\nServer: nginx\r\n",
            timestamp=datetime(2026, 10, 12, 8, 30, tzinfo=UTC),
            certificate=certificate,
        ),
        ShodanService(
            port=8443,
            transport="tcp",
            product="http-alt",
            banner="Alternate TLS listener",
            timestamp=datetime(2026, 10, 11, 20, 0, tzinfo=UTC),
            certificate=certificate,
        ),
    )


class FakeShodanPort(ShodanPort):
    """In-memory passive Shodan port for application-level tests."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[object, ...]]] = []
        self.services = _make_services()

    async def host(self, ip: str) -> ShodanHostView:
        self.calls.append(("host", (ip,)))
        return ShodanHostView(
            ip=ip,
            organization="Example Networks",
            asn="AS64500",
            hostnames=("edge.example.test",),
            ports=(443, 8443),
            tags=("cdn", "cloud"),
            services=self.services,
            meta=_meta(ShodanRoute.HOST, self.services),
            raw_response=HOST_RESPONSE,
            raw_sha256=_raw_hash(HOST_RESPONSE),
            http_status=200,
        )

    async def search(self, query: str, page: int = 1) -> ShodanSearchResult:
        self.calls.append(("search", (query, page)))
        matches = tuple(
            ShodanHostView(
                ip=f"203.0.113.{10 + index}",
                organization="Example Networks",
                asn="AS64500",
                hostnames=(),
                ports=(443,),
                tags=(),
                services=(),
                meta=_meta(ShodanRoute.SEARCH),
                raw_response=SEARCH_RESPONSE,
                raw_sha256=_raw_hash(SEARCH_RESPONSE),
                http_status=200,
            )
            for index in range(3)
        )
        return ShodanSearchResult(
            matches=matches,
            total=3,
            page=page,
            meta=_meta(ShodanRoute.SEARCH),
            raw_response=SEARCH_RESPONSE,
            raw_sha256=_raw_hash(SEARCH_RESPONSE),
            http_status=200,
        )

    async def domain(self, domain: str) -> ShodanDomainView:
        self.calls.append(("domain", (domain,)))
        return ShodanDomainView(
            domain=domain,
            subdomains=("www", "api", "vpn"),
            dns_records=tuple(dict(record) for record in DOMAIN_JSON["data"]),
            meta=_meta(ShodanRoute.DOMAIN),
            raw_response=DOMAIN_RESPONSE,
            raw_sha256=_raw_hash(DOMAIN_RESPONSE),
            http_status=200,
        )

    async def count(self, query: str) -> ShodanCountResult:
        self.calls.append(("count", (query,)))
        return ShodanCountResult(
            total=3,
            query=query,
            meta=_meta(ShodanRoute.COUNT),
            raw_response=SEARCH_RESPONSE,
            raw_sha256=_raw_hash(SEARCH_RESPONSE),
            http_status=200,
        )
