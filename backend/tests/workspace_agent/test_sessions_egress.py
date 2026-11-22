"""EGR-1 oracles: the host CONNECT proxy refuses by policy and tunnels otherwise.

Every test is simulated (S in the design oracle map): the resolver and the
connector are injected, no DNS query is made and no packet leaves the host.
The fake upstream is one end of a local ``socketpair``; the proxy treats it as
an opaque byte stream.  Production policy is never relaxed for tests: there is
no "allow loopback" switch, and reaching a fake upstream only works because the
connector was injected.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import stat
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from datetime import datetime
from pathlib import Path

import pytest
from sidecars.workspace_agent.sessions import egress

PUBLIC_V4 = "93.184.216.34"
PUBLIC_V4_ALT = "1.1.1.1"
PUBLIC_V6 = "2606:2800:220:1:248:1893:25c8:1946"
SESSION_ID = "7f1e3e5e-0b5a-4a8e-9e57-6a1f4f9f6b21"
ALLOWED_AUTHORITY = "api.openai.com:443"
DEFAULT_PROFILE = egress.EgressProfile(adapter_kind="codex", hosts=frozenset({"api.openai.com"}))
LOG_FIELDS = {
    "at",
    "session_id",
    "adapter_kind",
    "host",
    "port",
    "decision",
    "reason",
    "family",
}


@pytest.fixture
def short_dir() -> Iterator[Path]:
    """A /tmp directory short enough for an AF_UNIX pathname (108-byte limit)."""

    path = Path(tempfile.mkdtemp(prefix="egress-", dir="/tmp"))
    assert len(os.fsencode(str(path / "egress.sock"))) < 100
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def resolved(ip: str, port: int = 443) -> egress.ResolvedAddress:
    if ":" in ip:
        return egress.ResolvedAddress(family=socket.AF_INET6, ip=ip, sockaddr=(ip, port, 0, 0))
    return egress.ResolvedAddress(family=socket.AF_INET, ip=ip, sockaddr=(ip, port))


def static_resolver(*ips: str) -> Callable[[str, int], list[egress.ResolvedAddress]]:
    def resolve(host: str, port: int) -> list[egress.ResolvedAddress]:
        del host
        return [resolved(ip, port) for ip in ips]

    return resolve


class FakeConnector:
    """Return a fabricated stream pair; optionally refuse the first dials."""

    def __init__(self, *, failures: int = 0) -> None:
        self.dialed: list[egress.ResolvedAddress] = []
        self.upstreams: list[socket.socket] = []
        self._failures = failures
        self._lock = threading.Lock()

    def dial(self, address: egress.ResolvedAddress, *, timeout: float) -> socket.socket:
        del timeout
        with self._lock:
            self.dialed.append(address)
            if self._failures > 0:
                self._failures -= 1
                raise OSError("fake upstream refused the dial")
        client, upstream = socket.socketpair()
        with self._lock:
            self.upstreams.append(upstream)
        return client


@pytest.fixture
def proxy_factory(short_dir: Path) -> Iterator[Callable[..., egress.EgressProxy]]:
    created: list[egress.EgressProxy] = []

    def factory(
        *,
        profile: egress.EgressProfile = DEFAULT_PROFILE,
        resolver: Callable[[str, int], list[egress.ResolvedAddress]] | None = None,
        connector: object | None = None,
        local_addresses: Callable[[], frozenset[str]] | None = None,
        **options: object,
    ) -> egress.EgressProxy:
        index = len(created)
        timeouts: dict[str, object] = {
            "request_timeout_seconds": 2.0,
            "resolve_timeout_seconds": 2.0,
            "connect_timeout_seconds": 2.0,
            "idle_timeout_seconds": 2.0,
        }
        timeouts.update(options)
        proxy = egress.EgressProxy(
            socket_path=short_dir / f"egress-{index}.sock",
            log_path=short_dir / f"egress-{index}.log",
            session_id=SESSION_ID,
            profile=profile,
            resolver=resolver if resolver is not None else static_resolver(PUBLIC_V4),
            connector=connector if connector is not None else FakeConnector(),
            local_addresses=(
                local_addresses if local_addresses is not None else (lambda: frozenset())
            ),
            **timeouts,  # type: ignore[arg-type]
        )
        proxy.start()
        created.append(proxy)
        return proxy

    yield factory
    for proxy in created:
        proxy.close()


def open_client(proxy: egress.EgressProxy, *, timeout: float = 5.0) -> socket.socket:
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    connection.settimeout(timeout)
    connection.connect(str(proxy.socket_path))
    return connection


def connect_request(authority: str = ALLOWED_AUTHORITY, *, headers: bytes = b"") -> bytes:
    encoded = authority.encode("utf-8")
    return b"CONNECT " + encoded + b" HTTP/1.1\r\nHost: " + encoded + b"\r\n" + headers + b"\r\n"


def read_response(connection: socket.socket) -> tuple[int, bytes, bytes]:
    """Read one HTTP response; the leftover bytes are tunnel data (if any)."""

    buffer = bytearray()
    while b"\r\n\r\n" not in buffer:
        chunk = connection.recv(65536)
        if not chunk:
            raise AssertionError("the proxy closed without a response")
        buffer.extend(chunk)
    head, _, rest = bytes(buffer).partition(b"\r\n\r\n")
    status = int(head.split(b" ", 2)[1])
    length = 0
    for line in head.split(b"\r\n")[1:]:
        name, _, value = line.partition(b":")
        if name.strip().lower() == b"content-length":
            length = int(value.strip())
    body = rest
    while len(body) < length:
        chunk = connection.recv(65536)
        if not chunk:
            break
        body += chunk
    return status, body[:length], body[length:]


def recv_exact(connection: socket.socket, size: int, *, timeout: float = 5.0) -> bytes:
    connection.settimeout(timeout)
    data = bytearray()
    while len(data) < size:
        chunk = connection.recv(size - len(data))
        if not chunk:
            raise AssertionError(f"unexpected EOF after {len(data)}/{size} bytes")
        data.extend(chunk)
    return bytes(data)


def expect_eof(connection: socket.socket, *, timeout: float = 5.0) -> None:
    connection.settimeout(timeout)
    try:
        while connection.recv(65536):
            pass
    except ConnectionResetError:
        return


def read_log(short_dir: Path, index: int = 0) -> list[dict[str, object]]:
    path = short_dir / f"egress-{index}.log"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line]


def wait_for_log(
    short_dir: Path, *, index: int = 0, count: int = 1, timeout: float = 2.0
) -> list[dict[str, object]]:
    """The proxy answers before it appends its decision line; wait for it."""

    deadline = time.monotonic() + timeout
    while True:
        lines = read_log(short_dir, index)
        if len(lines) >= count or time.monotonic() >= deadline:
            return lines
        time.sleep(0.01)


def exchange(
    factory: Callable[..., egress.EgressProxy], request: bytes, **options: object
) -> tuple[int, bytes]:
    proxy = factory(**options)
    with open_client(proxy) as client:
        client.sendall(request)
        status, body, _leftover = read_response(client)
    return status, body


def write_profile(path: Path, payload: object, *, mode: int = 0o600) -> Path:
    path.write_text(json.dumps(payload))
    os.chmod(path, mode)
    return path


def profile_document(
    hosts: list[str] | tuple[str, ...] = ("api.openai.com",), *, adapter: str = "codex"
) -> dict[str, object]:
    return {
        "schema": egress.EGRESS_PROFILE_SCHEMA,
        "profiles": {adapter: {"hosts": list(hosts)}},
    }


# -- EG-01: an allowlisted CONNECT is tunnelled byte-exact --------------------


def test_allowlisted_connect_tunnels_to_fake_upstream(
    proxy_factory: Callable[..., egress.EgressProxy], short_dir: Path
) -> None:
    connector = FakeConnector()
    proxy = proxy_factory(connector=connector)
    with open_client(proxy) as client:
        client.sendall(connect_request())
        status, _body, _leftover = read_response(client)
        assert status == 200
        upstream = connector.upstreams[0]
        client.sendall(b"client-to-upstream")
        assert recv_exact(upstream, len(b"client-to-upstream")) == b"client-to-upstream"
        upstream.sendall(b"upstream-to-client")
        assert recv_exact(client, len(b"upstream-to-client")) == b"upstream-to-client"
    assert [address.ip for address in connector.dialed] == [PUBLIC_V4]
    lines = wait_for_log(short_dir)
    assert len(lines) == 1
    line = lines[0]
    assert {key: value for key, value in line.items() if key != "at"} == {
        "session_id": SESSION_ID,
        "adapter_kind": "codex",
        "host": "api.openai.com",
        "port": 443,
        "decision": "allow",
        "reason": "connected",
        "family": "ipv4",
    }
    datetime.fromisoformat(str(line["at"]))


def test_early_bytes_after_connect_reach_upstream(
    proxy_factory: Callable[..., egress.EgressProxy],
) -> None:
    connector = FakeConnector()
    proxy = proxy_factory(connector=connector)
    early = b"early-tls-client-hello"
    with open_client(proxy) as client:
        client.sendall(connect_request() + early)
        status, _body, _leftover = read_response(client)
        assert status == 200
        assert recv_exact(connector.upstreams[0], len(early)) == early


def test_public_ipv6_resolution_tunnels(
    proxy_factory: Callable[..., egress.EgressProxy], short_dir: Path
) -> None:
    connector = FakeConnector()
    proxy = proxy_factory(resolver=static_resolver(PUBLIC_V6), connector=connector)
    with open_client(proxy) as client:
        client.sendall(connect_request())
        status, _body, _leftover = read_response(client)
        assert status == 200
        client.sendall(b"over-ipv6")
        assert recv_exact(connector.upstreams[0], len(b"over-ipv6")) == b"over-ipv6"
    assert connector.dialed[0].ip == PUBLIC_V6
    assert wait_for_log(short_dir)[0]["family"] == "ipv6"


# -- EG-03/EG-04: private, LAN, Docker and host-local addresses -------------


@pytest.mark.parametrize(
    "ip",
    [
        "127.0.0.1",
        "127.10.20.30",
        "0.0.0.0",
        "10.0.0.1",
        "10.10.51.4",
        "172.16.0.1",
        "172.17.0.1",
        "172.18.0.1",
        "172.31.255.254",
        "192.168.1.1",
        "169.254.169.254",
        "169.254.0.1",
        "100.64.0.1",
        "100.119.62.63",
    ],
)
def test_private_and_lan_resolutions_are_refused(
    proxy_factory: Callable[..., egress.EgressProxy], short_dir: Path, ip: str
) -> None:
    connector = FakeConnector()
    status, body = exchange(
        proxy_factory,
        connect_request(),
        resolver=static_resolver(ip),
        connector=connector,
    )
    assert status == 403
    assert body.strip() == b"address_forbidden"
    assert connector.dialed == []
    lines = wait_for_log(short_dir)
    assert [(line["decision"], line["reason"]) for line in lines] == [("deny", "address_forbidden")]
    assert ip not in (short_dir / "egress-0.log").read_text()


@pytest.mark.parametrize(
    "ip",
    [
        "192.0.2.5",
        "198.51.100.7",
        "203.0.113.9",
        "198.18.0.1",
        "198.19.255.254",
        "192.0.0.8",
        "192.88.99.1",
        "224.0.0.1",
        "239.255.255.255",
        "240.0.0.1",
        "255.255.255.255",
    ],
)
def test_documentation_benchmark_multicast_and_reserved_are_refused(
    proxy_factory: Callable[..., egress.EgressProxy], ip: str
) -> None:
    status, body = exchange(proxy_factory, connect_request(), resolver=static_resolver(ip))
    assert status == 403
    assert body.strip() == b"address_forbidden"


def test_host_local_addresses_are_refused_even_when_public(
    proxy_factory: Callable[..., egress.EgressProxy],
) -> None:
    connector = FakeConnector()
    status, body = exchange(
        proxy_factory,
        connect_request(),
        resolver=static_resolver(PUBLIC_V4),
        connector=connector,
        local_addresses=lambda: frozenset({PUBLIC_V4}),
    )
    assert status == 403
    assert body.strip() == b"address_forbidden"
    assert connector.dialed == []


def test_unreadable_local_tables_do_not_open_private_ranges(
    proxy_factory: Callable[..., egress.EgressProxy],
) -> None:
    def unreadable() -> frozenset[str]:
        raise OSError("procfs unavailable")

    status, body = exchange(
        proxy_factory,
        connect_request(),
        resolver=static_resolver("10.0.0.1"),
        local_addresses=unreadable,
    )
    assert status == 403
    assert body.strip() == b"address_forbidden"


def test_local_address_parser_reads_only_assigned_addresses() -> None:
    trie = (
        "Id 52:\n"
        "Main:\n"
        "  +-- 0.0.0.0/0 6 2 41\n"
        "     |-- 5.196.91.129\n"
        "        /32 universe UNICAST\n"
        "Local:\n"
        "  +-- 0.0.0.0/0 4 0 11\n"
        "     |-- 10.10.31.19\n"
        "        /32 host LOCAL\n"
        "     |-- 127.0.0.1\n"
        "        /32 host LOCAL\n"
    )
    routes = (
        "00000000000000000000000000000000 00 "
        "00000000000000000000000000000000 00 "
        "00000000000000000000000000000000 ffffffff 00000001 00000000 00200200 lo\n"
        "fd7a115ca1e000000000000000000053 80 "
        "00000000000000000000000000000000 00 "
        "00000000000000000000000000000000 00000400 00000015 00000000 00000001 tailscale0\n"
        "fe800000000000000000000000000000 40 "
        "00000000000000000000000000000000 00 "
        "00000000000000000000000000000000 00000100 00000002 00000000 00000001 tailscale0\n"
    )
    addresses = egress._local_addresses_from_tables(trie, routes)
    assert addresses == frozenset({"10.10.31.19", "127.0.0.1", "fd7a:115c:a1e0:0:0:0:0:53"})


# -- EG-05: non-allowlisted names, IP literals and adversarial authorities --


@pytest.mark.parametrize(
    ("authority", "code"),
    [
        ("api.openai.com.evil.com:443", "host_not_allowed"),
        ("evil.example:443", "host_not_allowed"),
        ("chatgpt.com:443", "host_not_allowed"),
        ("api.openai.com@evil.com:443", "invalid_authority"),
        ("127.0.0.1:443", "ip_literal"),
        ("0x7f.1:443", "ip_literal"),
        ("2130706433:443", "ip_literal"),
        ("0177.0.0.1:443", "ip_literal"),
        ("1.2.3:443", "ip_literal"),
        ("[::1]:443", "ip_literal"),
        ("::1:443", "invalid_authority"),
        ("api.openai.com:80", "port_not_allowed"),
        ("api.openai.com:0443", "port_not_allowed"),
        ("API.OPENAI.COM.:443", "invalid_authority"),
        ("api.openai.com%2eevil.com:443", "invalid_authority"),
        ("api.openai.com:443/extra", "port_not_allowed"),
        ("api.openai.com:443?query=1", "port_not_allowed"),
    ],
)
def test_adversarial_authorities_are_refused(
    proxy_factory: Callable[..., egress.EgressProxy],
    short_dir: Path,
    authority: str,
    code: str,
) -> None:
    connector = FakeConnector()
    status, body = exchange(proxy_factory, connect_request(authority), connector=connector)
    assert status in (400, 403)
    assert body.strip() == code.encode()
    assert connector.dialed == []
    lines = wait_for_log(short_dir)
    assert lines[0]["reason"] == code
    assert lines[0]["decision"] == "deny"


def test_unicode_homoglyph_cannot_impersonate_an_allowlisted_name() -> None:
    homoglyph = chr(0x430) + "pi.openai.com"  # Cyrillic "a" homoglyph
    normalized = egress.normalize_host(homoglyph)
    assert normalized != "api.openai.com"
    assert normalized not in egress.ADAPTER_EGRESS_MAXIMUM["codex"]


def test_non_ascii_authority_is_refused_at_the_wire(
    proxy_factory: Callable[..., egress.EgressProxy],
) -> None:
    proxy = proxy_factory()
    with open_client(proxy) as client:
        client.sendall(b"CONNECT \xd0\xb0pi.openai.com:443 HTTP/1.1\r\n\r\n")
        status, body, _leftover = read_response(client)
    assert status == 400
    assert body.strip() == b"invalid_request"


def test_non_connect_method_is_refused(
    proxy_factory: Callable[..., egress.EgressProxy],
) -> None:
    proxy = proxy_factory()
    with open_client(proxy) as client:
        client.sendall(b"GET / HTTP/1.1\r\nHost: api.openai.com\r\n\r\n")
        status, body, _leftover = read_response(client)
    assert status == 405
    assert body.strip() == b"method_not_allowed"


# -- EG-06: IPv6 special ranges and every-address validation ----------------


@pytest.mark.parametrize(
    "ip",
    [
        "::1",
        "::",
        "fd00::1",
        "fc00::1",
        "fe80::1",
        "fec0::1",
        "ff02::1",
        "::ffff:10.0.0.1",
        "::ffff:93.184.216.34",
        "::10.0.0.1",
        "2001:db8::1",
        "2001::1",
        "2002:0a00:0001::1",
        "64:ff9b::1.2.3.4",
        "64:ff9b:1::1",
        "3fff::1",
        "5f00::1",
        "100::1",
    ],
)
def test_ipv6_special_addresses_are_refused(
    proxy_factory: Callable[..., egress.EgressProxy], ip: str
) -> None:
    connector = FakeConnector()
    status, body = exchange(
        proxy_factory, connect_request(), resolver=static_resolver(ip), connector=connector
    )
    assert status == 403
    assert body.strip() == b"address_forbidden"
    assert connector.dialed == []


@pytest.mark.parametrize("addresses", [(PUBLIC_V4, "fd00::1"), ("fd00::1", PUBLIC_V4)])
def test_every_resolved_address_must_validate(
    proxy_factory: Callable[..., egress.EgressProxy],
    short_dir: Path,
    addresses: tuple[str, ...],
) -> None:
    connector = FakeConnector()
    status, body = exchange(
        proxy_factory,
        connect_request(),
        resolver=static_resolver(*addresses),
        connector=connector,
    )
    assert status == 403
    assert body.strip() == b"address_forbidden"
    assert connector.dialed == []
    assert wait_for_log(short_dir)[0]["reason"] == "address_forbidden"


def test_aaa_only_forbidden_resolution_is_refused(
    proxy_factory: Callable[..., egress.EgressProxy],
) -> None:
    connector = FakeConnector()
    status, body = exchange(
        proxy_factory,
        connect_request(),
        resolver=static_resolver("fd00::53"),
        connector=connector,
    )
    assert status == 403
    assert body.strip() == b"address_forbidden"
    assert connector.dialed == []


# -- resolver lifecycle: single resolution, pinning, fallback ---------------


def test_resolution_happens_once_and_the_ip_is_pinned(
    proxy_factory: Callable[..., egress.EgressProxy],
) -> None:
    calls: list[str] = []
    answers = [[resolved(PUBLIC_V4)], [resolved("10.0.0.1")]]

    def resolver(host: str, port: int) -> list[egress.ResolvedAddress]:
        del port
        calls.append(host)
        return answers[len(calls) - 1]

    connector = FakeConnector()
    proxy = proxy_factory(resolver=resolver, connector=connector)
    with open_client(proxy) as client:
        client.sendall(connect_request())
        status, _body, _leftover = read_response(client)
        assert status == 200
        client.sendall(b"more-bytes")
        assert recv_exact(connector.upstreams[0], len(b"more-bytes")) == b"more-bytes"
    assert calls == ["api.openai.com"]
    assert [address.ip for address in connector.dialed] == [PUBLIC_V4]


def test_connect_falls_back_to_the_next_validated_address(
    proxy_factory: Callable[..., egress.EgressProxy],
) -> None:
    connector = FakeConnector(failures=1)
    status, _body = exchange(
        proxy_factory,
        connect_request(),
        resolver=static_resolver(PUBLIC_V4, PUBLIC_V4_ALT),
        connector=connector,
    )
    assert status == 200
    assert [address.ip for address in connector.dialed] == [PUBLIC_V4, PUBLIC_V4_ALT]


def test_resolver_timeout_is_refused(
    proxy_factory: Callable[..., egress.EgressProxy],
) -> None:
    def slow(host: str, port: int) -> list[egress.ResolvedAddress]:
        del host
        time.sleep(0.5)
        return [resolved(PUBLIC_V4, port)]

    status, body = exchange(
        proxy_factory, connect_request(), resolver=slow, resolve_timeout_seconds=0.1
    )
    assert status == 502
    assert body.strip() == b"resolution_timeout"


def test_empty_resolution_is_refused(
    proxy_factory: Callable[..., egress.EgressProxy],
) -> None:
    status, body = exchange(proxy_factory, connect_request(), resolver=lambda host, port: [])
    assert status == 502
    assert body.strip() == b"resolution_failed"


def test_resolution_failure_is_refused(
    proxy_factory: Callable[..., egress.EgressProxy],
) -> None:
    def failing(host: str, port: int) -> list[egress.ResolvedAddress]:
        del host, port
        raise socket.gaierror("fake resolver failure")

    status, body = exchange(proxy_factory, connect_request(), resolver=failing)
    assert status == 502
    assert body.strip() == b"resolution_failed"


# -- EG-08: the profile is mandatory and can only narrow --------------------


def test_missing_profile_is_refused(short_dir: Path) -> None:
    with pytest.raises(egress.EgressConfigError) as caught:
        egress.load_egress_profile(short_dir / "absent.json", adapter_kind="codex")
    assert caught.value.code == "egress_profile_unavailable"


@pytest.mark.parametrize(
    ("payload", "mode"),
    [
        (b"", 0o600),
        (b"not json", 0o600),
        (json.dumps({"schema": "wrong", "profiles": {}}).encode(), 0o600),
        (json.dumps({"schema": egress.EGRESS_PROFILE_SCHEMA}).encode(), 0o600),
        (json.dumps(profile_document()).encode(), 0o644),
        (json.dumps(profile_document(hosts=())).encode(), 0o600),
        (json.dumps(profile_document(adapter="other")).encode(), 0o600),
        (
            json.dumps(profile_document(hosts=("api.anthropic.com",))).encode(),
            0o600,
        ),
        (b" " * (egress.EGRESS_PROFILE_MAX_BYTES + 1), 0o600),
    ],
)
def test_invalid_profiles_are_refused(short_dir: Path, payload: bytes, mode: int) -> None:
    path = short_dir / "profile.json"
    path.write_bytes(payload)
    os.chmod(path, mode)
    with pytest.raises(egress.EgressConfigError) as caught:
        egress.load_egress_profile(path, adapter_kind="codex")
    assert caught.value.code == "egress_profile_unavailable"


def test_symlinked_profile_is_refused(short_dir: Path) -> None:
    target = write_profile(short_dir / "real.json", profile_document())
    link = short_dir / "link.json"
    link.symlink_to(target)
    with pytest.raises(egress.EgressConfigError) as caught:
        egress.load_egress_profile(link, adapter_kind="codex")
    assert caught.value.code == "egress_profile_unavailable"


def test_unknown_adapter_profile_is_refused(short_dir: Path) -> None:
    path = write_profile(short_dir / "profile.json", profile_document())
    with pytest.raises(egress.EgressConfigError) as caught:
        egress.load_egress_profile(path, adapter_kind="unknown")
    assert caught.value.code == "egress_profile_unavailable"


def test_profile_configuration_can_only_narrow_the_compiled_maximum(
    short_dir: Path,
) -> None:
    payload = {
        "schema": egress.EGRESS_PROFILE_SCHEMA,
        "profiles": {
            "codex": {"hosts": ["API.OPENAI.COM", "auth.openai.com", "evil.example"]},
            "claude": {"hosts": ["api.anthropic.com"]},
        },
    }
    path = write_profile(short_dir / "profile.json", payload)
    codex = egress.load_egress_profile(path, adapter_kind="codex")
    assert codex.hosts == frozenset({"api.openai.com", "auth.openai.com"})
    claude = egress.load_egress_profile(path, adapter_kind="claude")
    assert claude.hosts == frozenset({"api.anthropic.com"})


def test_proxy_refuses_a_profile_wider_than_the_compiled_maximum(
    short_dir: Path,
) -> None:
    wider = egress.EgressProfile(adapter_kind="codex", hosts=frozenset({"evil.example"}))
    with pytest.raises(egress.EgressConfigError) as caught:
        egress.EgressProxy(
            socket_path=short_dir / "egress.sock",
            log_path=short_dir / "egress.log",
            session_id=SESSION_ID,
            profile=wider,
        )
    assert caught.value.code == "egress_profile_unavailable"


def test_proxy_refuses_an_empty_profile(short_dir: Path) -> None:
    empty = egress.EgressProfile(adapter_kind="codex", hosts=frozenset())
    with pytest.raises(egress.EgressConfigError) as caught:
        egress.EgressProxy(
            socket_path=short_dir / "egress.sock",
            log_path=short_dir / "egress.log",
            session_id=SESSION_ID,
            profile=empty,
        )
    assert caught.value.code == "egress_profile_unavailable"


# -- EG-10: revocation and close -------------------------------------------


def test_revocation_refuses_new_connects_and_closes_tunnels(
    proxy_factory: Callable[..., egress.EgressProxy],
) -> None:
    connector = FakeConnector()
    proxy = proxy_factory(connector=connector)
    client = open_client(proxy)
    try:
        client.sendall(connect_request())
        status, _body, _leftover = read_response(client)
        assert status == 200
        upstream = connector.upstreams[0]
        proxy.revoke()
        expect_eof(client)
        expect_eof(upstream)
        with open_client(proxy) as refused:
            refused.sendall(connect_request())
            status, body, _leftover = read_response(refused)
        assert status == 403
        assert body.strip() == b"egress_revoked"
    finally:
        client.close()


def test_close_unlinks_the_socket(
    proxy_factory: Callable[..., egress.EgressProxy],
) -> None:
    proxy = proxy_factory()
    path = proxy.socket_path
    assert path.exists()
    proxy.close()
    assert not path.exists()


# -- bounds: request size, request timeout, idle, wall budget, 8 tunnels ----


def test_production_bounds_are_the_designed_ones() -> None:
    assert egress.CONNECT_REQUEST_TIMEOUT_SECONDS == 10.0
    assert egress.CONNECT_TIMEOUT_SECONDS == 10.0
    assert egress.RESOLVE_TIMEOUT_SECONDS == 5.0
    assert egress.TUNNEL_IDLE_TIMEOUT_SECONDS == 600.0
    assert egress.MAX_CONCURRENT_TUNNELS == 8
    assert egress.MAX_CONNECT_REQUEST_BYTES == 8 * 1024
    assert egress.TUNNEL_BUFFER_BYTES == 64 * 1024
    assert egress.EGRESS_LOG_MAX_BYTES == 2 * 1024 * 1024
    assert egress.CONNECT_PORT == 443


def test_oversized_connect_request_is_refused(
    proxy_factory: Callable[..., egress.EgressProxy], short_dir: Path
) -> None:
    proxy = proxy_factory()
    with open_client(proxy) as client:
        client.sendall(b"CONNECT " + b"a" * (egress.MAX_CONNECT_REQUEST_BYTES + 100))
        status, body, _leftover = read_response(client)
    assert status == 431
    assert body.strip() == b"request_too_large"
    assert wait_for_log(short_dir)[0]["reason"] == "request_too_large"


def test_connect_request_receive_timeout_is_refused(
    proxy_factory: Callable[..., egress.EgressProxy],
) -> None:
    proxy = proxy_factory(request_timeout_seconds=0.2)
    with open_client(proxy) as client:
        status, body, _leftover = read_response(client)
    assert status == 408
    assert body.strip() == b"request_timeout"


def test_tunnel_idle_timeout_closes_both_sides(
    proxy_factory: Callable[..., egress.EgressProxy],
) -> None:
    connector = FakeConnector()
    proxy = proxy_factory(connector=connector, idle_timeout_seconds=0.2)
    client = open_client(proxy)
    try:
        client.sendall(connect_request())
        status, _body, _leftover = read_response(client)
        assert status == 200
        expect_eof(client)
        expect_eof(connector.upstreams[0])
    finally:
        client.close()


def test_wall_budget_closes_tunnels(
    proxy_factory: Callable[..., egress.EgressProxy],
) -> None:
    connector = FakeConnector()
    proxy = proxy_factory(connector=connector, idle_timeout_seconds=5.0, budget_wall_seconds=0.2)
    client = open_client(proxy)
    try:
        client.sendall(connect_request())
        status, _body, _leftover = read_response(client)
        assert status == 200
        expect_eof(client)
    finally:
        client.close()


def test_injected_clock_enforces_the_wall_budget_mid_tunnel(
    proxy_factory: Callable[..., egress.EgressProxy],
) -> None:
    now = [1000.0]
    connector = FakeConnector()
    proxy = proxy_factory(
        connector=connector,
        clock=lambda: now[0],
        idle_timeout_seconds=30.0,
        budget_wall_seconds=5.0,
    )
    client = open_client(proxy)
    try:
        client.sendall(connect_request())
        status, _body, _leftover = read_response(client)
        assert status == 200
        now[0] += 10.0  # past the injected deadline
        client.sendall(b"wake-the-pump")
        expect_eof(client)
        expect_eof(connector.upstreams[0])
    finally:
        client.close()


def test_concurrent_tunnel_limit_is_eight(
    proxy_factory: Callable[..., egress.EgressProxy], short_dir: Path
) -> None:
    connector = FakeConnector()
    proxy = proxy_factory(connector=connector, idle_timeout_seconds=30.0)
    clients: list[socket.socket] = []
    try:
        for _ in range(egress.MAX_CONCURRENT_TUNNELS):
            client = open_client(proxy)
            clients.append(client)
            client.sendall(connect_request())
            status, _body, _leftover = read_response(client)
            assert status == 200
        with open_client(proxy) as ninth:
            ninth.sendall(connect_request())
            status, body, _leftover = read_response(ninth)
        assert status == 503
        assert body.strip() == b"too_many_tunnels"
        lines = wait_for_log(short_dir, count=egress.MAX_CONCURRENT_TUNNELS + 1)
        assert lines[-1]["reason"] == "too_many_tunnels"
    finally:
        for client in clients:
            client.close()


# -- socket and log hygiene -------------------------------------------------


def test_listener_socket_is_a_0600_pathname_socket(
    proxy_factory: Callable[..., egress.EgressProxy],
) -> None:
    proxy = proxy_factory()
    info = os.lstat(proxy.socket_path)
    assert stat.S_ISSOCK(info.st_mode)
    assert stat.S_IMODE(info.st_mode) == 0o600


def test_socket_path_too_long_is_refused(short_dir: Path) -> None:
    long_path = short_dir / ("e" * 100)
    assert len(os.fsencode(str(long_path))) > egress.AF_UNIX_PATH_MAX_BYTES
    proxy = egress.EgressProxy(
        socket_path=long_path,
        log_path=short_dir / "egress.log",
        session_id=SESSION_ID,
        profile=DEFAULT_PROFILE,
    )
    with pytest.raises(egress.EgressConfigError) as caught:
        proxy.start()
    assert caught.value.code == "egress_socket_path_too_long"


def test_egress_log_never_carries_secrets_ips_or_extra_fields(
    proxy_factory: Callable[..., egress.EgressProxy], short_dir: Path
) -> None:
    secret = "Bearer sk-live-SECRET-TOKEN"
    connector = FakeConnector()
    proxy = proxy_factory(connector=connector)
    with open_client(proxy) as client:
        client.sendall(
            connect_request(headers=b"Proxy-Authorization: " + secret.encode() + b"\r\n")
        )
        status, _body, _leftover = read_response(client)
        assert status == 200
    proxy_with_private_answer = proxy_factory(resolver=static_resolver("10.0.0.1"))
    with open_client(proxy_with_private_answer) as client:
        client.sendall(connect_request())
        status, _body, _leftover = read_response(client)
        assert status == 403
    with open_client(proxy) as client:
        client.sendall(b"CONNECT api.openai.com\x01@evil.com:443 HTTP/1.1\r\n\r\n")
        status, body, _leftover = read_response(client)
        assert status == 400
        assert body.strip() == b"invalid_authority"
    lines = wait_for_log(short_dir, count=2) + wait_for_log(short_dir, index=1)
    text = (short_dir / "egress-0.log").read_text() + (short_dir / "egress-1.log").read_text()
    assert secret not in text
    assert "SECRET-TOKEN" not in text
    assert "10.0.0.1" not in text
    assert PUBLIC_V4 not in text
    assert "\x01" not in text
    assert stat.S_IMODE(os.lstat(short_dir / "egress-0.log").st_mode) == 0o600
    assert stat.S_IMODE(os.lstat(short_dir / "egress-1.log").st_mode) == 0o600
    assert lines
    for line in lines:
        assert set(line) == LOG_FIELDS
        assert line["session_id"] == SESSION_ID
        assert line["adapter_kind"] == "codex"
        datetime.fromisoformat(str(line["at"]))
    malformed = [line for line in lines if line["reason"] == "invalid_authority"]
    assert malformed
    assert malformed[0]["host"] == "api.openai.com@evil.com:443"


def test_log_ring_prunes_whole_lines_and_stays_0600(short_dir: Path) -> None:
    log = egress._EgressLog(short_dir / "ring.log", max_bytes=40)
    for index in range(10):
        log.append({"n": index})
    path = short_dir / "ring.log"
    lines = path.read_text().splitlines()
    assert 0 < len(lines) < 10
    assert json.loads(lines[-1])["n"] == 9
    assert sum(len(line) + 1 for line in lines) <= 40
    assert stat.S_IMODE(os.lstat(path).st_mode) == 0o600
