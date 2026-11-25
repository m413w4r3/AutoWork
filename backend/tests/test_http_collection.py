from __future__ import annotations

import asyncio
import gzip
from collections.abc import Sequence

import pytest

from cti_app.application.http_collection import (
    AccessBlockedError,
    CollectionPolicy,
    DownloadTooLargeError,
    DownloadTransientError,
    DownloadUnavailableError,
    PinnedHttpRequest,
    RawHttpResponse,
    SafeHttpCollector,
    UnsafeAddressError,
    UnsupportedContentError,
    parse_domain_policy,
)
from cti_app.domain.collection import (
    AttemptOutcome,
    CollectionFailureReason,
    DetectedMimeType,
)

PUBLIC_IP = "93.184.216.34"


class StaticResolver:
    def __init__(
        self,
        values: Sequence[Sequence[str]] | None = None,
        *,
        fail_hostname: str | None = None,
    ) -> None:
        self.values = list(values or [(PUBLIC_IP,), (PUBLIC_IP,)])
        self.calls = 0
        self.fail_hostname = fail_hostname
        self.hostnames: list[str] = []

    async def resolve(self, hostname: str) -> Sequence[str]:
        self.hostnames.append(hostname)
        if hostname == self.fail_hostname:
            return ()
        value = self.values[min(self.calls, len(self.values) - 1)]
        self.calls += 1
        return value


class QueueTransport:
    def __init__(self, responses: list[RawHttpResponse]) -> None:
        self.responses = responses
        self.requests: list[PinnedHttpRequest] = []

    async def request(self, request: PinnedHttpRequest) -> RawHttpResponse:
        self.requests.append(request)
        return self.responses.pop(0)


class SlowResolver:
    async def resolve(self, hostname: str) -> Sequence[str]:
        del hostname
        await asyncio.sleep(0.02)
        return (PUBLIC_IP,)


def response(
    body: bytes = b"<!doctype html><html><body>ok</body></html>",
    *,
    status: int = 200,
    headers: dict[str, str] | None = None,
) -> RawHttpResponse:
    return RawHttpResponse(
        status=status,
        headers=headers or {"content-type": "text/html"},
        encoded_body=body,
    )


async def test_redirect_to_localhost_is_blocked_before_second_connection() -> None:
    transport = QueueTransport([response(status=302, headers={"location": "http://localhost/x"})])
    collector = SafeHttpCollector(transport, StaticResolver())

    with pytest.raises(UnsafeAddressError, match="Local"):
        await collector.fetch("https://public.example/report")

    assert len(transport.requests) == 1


async def test_redirect_to_private_address_is_blocked() -> None:
    transport = QueueTransport([response(status=302, headers={"location": "http://10.0.0.8/x"})])
    collector = SafeHttpCollector(transport, StaticResolver())

    with pytest.raises(UnsafeAddressError, match="Non-public"):
        await collector.fetch("https://public.example/report")

    assert len(transport.requests) == 1


@pytest.mark.parametrize(
    "url",
    [
        "http://[::1]/",
        "http://[fe80::1]/",
        "http://[fc00::1]/",
        "http://[ff02::1]/",
        "http://[2001:db8::1]/",
    ],
)
async def test_unsafe_ipv6_ranges_are_blocked(url: str) -> None:
    transport = QueueTransport([])

    with pytest.raises(UnsafeAddressError):
        await SafeHttpCollector(transport, StaticResolver()).fetch(url)

    assert transport.requests == []


async def test_dns_rebinding_is_blocked_when_answers_change_before_connection() -> None:
    resolver = StaticResolver([(PUBLIC_IP,), ("127.0.0.1",)])
    transport = QueueTransport([])

    with pytest.raises(UnsafeAddressError, match="Non-public"):
        await SafeHttpCollector(transport, resolver).fetch("https://public.example/report")

    assert transport.requests == []


async def test_mixed_public_and_private_answer_is_blocked() -> None:
    resolver = StaticResolver([(PUBLIC_IP, "10.0.0.8"), (PUBLIC_IP,)])
    transport = QueueTransport([])

    with pytest.raises(UnsafeAddressError, match="Non-public"):
        await SafeHttpCollector(transport, resolver).fetch("https://public.example/report")

    assert transport.requests == []


async def test_rotating_public_cdn_answers_are_accepted_and_pinned() -> None:
    resolver = StaticResolver([("2.22.250.138",), ("2.22.250.149",)])
    transport = QueueTransport([response()])

    await SafeHttpCollector(transport, resolver).fetch("https://public.example/report")

    assert [request.approved_ip for request in transport.requests] == ["2.22.250.138"]


async def test_total_timeout_includes_dns_resolution() -> None:
    collector = SafeHttpCollector(
        QueueTransport([]),
        SlowResolver(),
        CollectionPolicy(timeout_seconds=0.001),
    )

    with pytest.raises(DownloadTransientError, match="DNS resolution"):
        await collector.fetch("https://public.example/report")


async def test_redirect_loop_is_bounded() -> None:
    transport = QueueTransport(
        [response(status=302, headers={"location": "/loop"}) for _ in range(3)]
    )
    resolver = StaticResolver([(PUBLIC_IP,)] * 6)
    collector = SafeHttpCollector(
        transport,
        resolver,
        CollectionPolicy(max_redirects=2),
    )

    with pytest.raises(DownloadUnavailableError, match="Maximum redirect"):
        await collector.fetch("https://public.example/loop")

    assert len(transport.requests) == 3


@pytest.mark.parametrize("status", [202, 204, 205])
async def test_non_content_success_status_is_a_deterministic_block(status: int) -> None:
    destination = "https://www.example.test/document?signature=secret#section"
    transport = QueueTransport(
        [
            response(
                status=302,
                headers={"location": destination},
            ),
            response(
                b"<!doctype html><html><body>Accepted challenge</body></html>",
                status=status,
                headers={"content-type": "text/html"},
            ),
        ]
    )

    with pytest.raises(AccessBlockedError) as captured:
        await SafeHttpCollector(transport, StaticResolver()).fetch("http://www.example.test/")

    error = captured.value
    assert error.retryable is False
    assert error.outcome is AttemptOutcome.BLOCKED
    assert error.reason_code is CollectionFailureReason.ACCESS_BLOCKED
    assert error.final_url == destination
    assert error.redirect_chain == (destination,)
    assert error.http_status == status
    assert error.headers == {"content-type": "text/html"}
    assert error.response_body is None


@pytest.mark.parametrize("status", [200, 203, 206])
async def test_document_success_statuses_still_accept_content(status: int) -> None:
    result = await SafeHttpCollector(
        QueueTransport([response(b"%PDF-1.7\ncontent", status=status)]),
        StaticResolver(),
    ).fetch("https://public.example/report.pdf")

    assert result.status == status
    assert result.detected_content_type is DetectedMimeType.PDF


async def test_dns_failure_after_www_redirect_keeps_remote_location_unchanged() -> None:
    target = "https://www.www.example.test/"
    transport = QueueTransport(
        [response(status=301, headers={"location": target + "?signature=secret#section"})]
    )
    resolver = StaticResolver(fail_hostname="www.www.example.test")

    with pytest.raises(DownloadTransientError) as captured:
        await SafeHttpCollector(transport, resolver).fetch("http://www.example.test/")

    error = captured.value
    assert error.reason_code is CollectionFailureReason.DNS_ERROR
    assert error.final_url == target + "?signature=secret#section"
    assert error.redirect_chain == (target + "?signature=secret#section",)
    assert error.headers == {"location": target}
    assert resolver.hostnames == [
        "www.example.test",
        "www.example.test",
        "www.www.example.test",
        "www.www.example.test",
    ]
    assert [request.url for request in transport.requests] == ["http://www.example.test/"]


async def test_response_over_wire_limit_is_rejected() -> None:
    collector = SafeHttpCollector(
        QueueTransport([response(b"<html>" + b"a" * 100 + b"</html>")]),
        StaticResolver(),
        CollectionPolicy(max_download_bytes=32),
    )

    with pytest.raises(DownloadTooLargeError, match="Compressed"):
        await collector.fetch("https://public.example/large")


async def test_excessive_decompression_is_rejected() -> None:
    compressed = gzip.compress(b"<html>" + b"A" * 10_000 + b"</html>")
    collector = SafeHttpCollector(
        QueueTransport(
            [
                response(
                    compressed,
                    headers={
                        "content-type": "text/html",
                        "content-encoding": "gzip",
                    },
                )
            ]
        ),
        StaticResolver(),
        CollectionPolicy(max_decompression_ratio=2),
    )

    with pytest.raises(DownloadTooLargeError, match="decompression ratio"):
        await collector.fetch("https://public.example/bomb")


async def test_detected_mime_wins_over_misleading_content_type() -> None:
    collector = SafeHttpCollector(
        QueueTransport([response(b"%PDF-1.7\n% test", headers={"content-type": "text/html"})]),
        StaticResolver(),
    )

    result = await collector.fetch("https://public.example/report")

    assert result.declared_content_type == "text/html"
    assert result.detected_content_type is DetectedMimeType.PDF


async def test_unsupported_content_encoding_is_typed() -> None:
    collector = SafeHttpCollector(
        QueueTransport([response(headers={"content-type": "text/html", "content-encoding": "br"})]),
        StaticResolver(),
    )

    with pytest.raises(UnsupportedContentError, match="Content-Encoding"):
        await collector.fetch("https://public.example/report")


async def test_configured_domain_restrictions_are_enforced() -> None:
    policy = CollectionPolicy(
        allowed_domains=parse_domain_policy("research.example"),
        blocked_domains=parse_domain_policy("blocked.research.example"),
    )
    collector = SafeHttpCollector(QueueTransport([]), StaticResolver(), policy)

    with pytest.raises(UnsafeAddressError, match="allow-list"):
        await collector.fetch("https://outside.example/report")
    with pytest.raises(UnsafeAddressError, match="blocked"):
        await collector.fetch("https://blocked.research.example/report")


def test_invalid_domain_policy_is_rejected() -> None:
    with pytest.raises(ValueError, match="invalid hostname"):
        parse_domain_policy("valid.example,bad..example")
