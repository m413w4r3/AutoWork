"""Host-side provider-only egress proxy for one session (M24.1-F1-D, lot EGR-1).

The supervisor owns a per-session AF_UNIX *pathname* listener
``<session>/ipc/egress.sock`` (0600, no TCP listener at all).  A trusted
entrypoint inside the sandbox ``--unshare-net`` namespace starts a byte-pipe
forwarder on ``127.0.0.1:3128``; the CLI reaches it through the closed
``HTTPS_PROXY`` literal, and every tunnel is brokered here.

Protocol: HTTP/1.1 ``CONNECT host:443`` only, over one connection per tunnel.
The proxy never authenticates a peer (the UDS permissions are the boundary) and
never picks the allowlist from anything the sandbox sends: the profile was
chosen by the supervisor from ``record.adapter_kind``.  Validation is
fail-closed and ordered (design §5):

1. the authority is exactly ``host:port``, with no userinfo, path, query,
   escape, whitespace or control character, and port 443 only;
2. the name is lowercased/IDNA-normalised, with valid labels, at most 253
   characters; trailing dots, wildcards and every IP-literal form
   (dotted/octal/hex/integer IPv4, bracketed IPv6) are refused;
3. the name must match the session's *effective* allowlist exactly (the
   adapter's compiled maximum intersected with the on-disk profile; no
   wildcard);
4. the name is resolved once with ``getaddrinfo(AF_UNSPEC)`` under a 5 s
   timeout through an injectable resolver; an empty answer is refused;
5. **every** returned address is validated: loopback, RFC1918, ULA,
   link-local (including ``169.254.169.254`` and ``fe80::/10``), CGNAT,
   multicast, unspecified, reserved, documentation, benchmark, broadcast,
   6to4/Teredo, IPv4-mapped/compatible IPv6 (the embedded IPv4 is validated
   too and the mapped form is still refused), local-use NAT64, and every
   address assigned to a host interface (``/proc/net/fib_trie`` +
   ``/proc/net/ipv6_route`` through an injectable reader);
6. one forbidden address refuses the whole CONNECT; there is no fallback to a
   "good" one;
7. a validated address is connected with a 10 s timeout through an injectable
   connector and pinned: the name is never resolved again;
8. bytes are piped untouched until EOF in either direction with 64 KiB
   buffers; there is no TLS interception and no certificate substitution;
9. exactly one bounded JSONL line per decided CONNECT is appended to
   ``egress.log`` (0600, 2 MiB ring with a 1.25x high-water compaction).  It
   carries ``{at, session_id, adapter_kind, host, port, decision, reason,
   family}`` and never a URL, path, header, token, body or resolved-IP list.
   An authority that fails validation is logged with an empty ``host``.

At most ``MAX_CONCURRENT_CONNECTIONS`` client connections (being read,
validated or tunnelled) are handled at once; beyond that the proxy answers
503 and closes without starting a worker thread.  Resolution threads cannot be
cancelled, so at most ``MAX_CONCURRENT_RESOLVERS`` may be in flight and a
later CONNECT is refused with the stable ``resolver_busy`` code.

``revoke()`` refuses new CONNECTs and closes open tunnels immediately;
``close()`` also unlinks the socket.  The transport is thread-based: the
sessions architecture guard allowlists a closed stdlib import set that excludes
``asyncio``, and this module adds no dependency.

Normative source: ``docs/design/investigation-workshop/BROKER-RELAY-EGRESS.md``
§5.
"""

from __future__ import annotations

import json
import os
import re
import socket
import stat
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, cast

EGRESS_PROFILE_SCHEMA = "autowork-session-egress.v1"
EGRESS_PROFILE_MAX_BYTES = 64 * 1024
EGRESS_LOG_MAX_BYTES = 2 * 1024 * 1024
EGRESS_LOG_FILE_MODE = 0o600
EGRESS_SOCKET_MODE = 0o600
PRIVATE_DIRECTORY_MODE = 0o700
CONNECT_PORT = 443
CONNECT_REQUEST_TIMEOUT_SECONDS = 10.0
CONNECT_TIMEOUT_SECONDS = 10.0
RESOLVE_TIMEOUT_SECONDS = 5.0
TUNNEL_IDLE_TIMEOUT_SECONDS = 600.0
MAX_CONCURRENT_TUNNELS = 8
# Connections are counted from accept until the worker finishes, so this
# ceiling covers CONNECTs still being read or validated as well as tunnels.
MAX_CONCURRENT_CONNECTIONS = 16
# A timed-out resolver call cannot be killed; cap the abandoned threads.
MAX_CONCURRENT_RESOLVERS = MAX_CONCURRENT_TUNNELS
MAX_CONNECT_REQUEST_BYTES = 8 * 1024
TUNNEL_BUFFER_BYTES = 64 * 1024
MAX_HOSTNAME_LENGTH = 253
MAX_PROFILE_HOSTS = 64
AF_UNIX_PATH_MAX_BYTES = 107  # sockaddr_un.sun_path is 108 bytes including the NUL
CLOSE_JOIN_SECONDS = 5.0
FIB_TRIE_PATH = Path("/proc/net/fib_trie")
IPV6_ROUTE_PATH = Path("/proc/net/ipv6_route")

# Each adapter declares its compiled maximum provider-host set.  Configuration
# may narrow this set, never widen it; the effective allowlist is the
# intersection.  Candidates come from CLI binary strings and stay INFERRED
# until the H0 spike proves the exact list.
ADAPTER_EGRESS_MAXIMUM: Mapping[str, frozenset[str]] = {
    "codex": frozenset({"api.openai.com", "auth.openai.com", "chatgpt.com"}),
    "claude": frozenset({"api.anthropic.com"}),
}

DECISION_ALLOW = "allow"
DECISION_DENY = "deny"

_HOST_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
_IPV4_TEXT = re.compile(r"\d{1,3}(?:\.\d{1,3}){3}")
_IPV6_GROUP = re.compile(r"[0-9A-Fa-f]{1,4}")
_FIB_TRIE_LOCAL = re.compile(r"\|-- (\d{1,3}(?:\.\d{1,3}){3})")
_STATUS_PHRASES = {
    400: "Bad Request",
    403: "Forbidden",
    405: "Method Not Allowed",
    408: "Request Timeout",
    431: "Request Header Fields Too Large",
    502: "Bad Gateway",
    503: "Service Unavailable",
}
_CONNECT_ESTABLISHED = b"HTTP/1.1 200 Connection Established\r\n\r\n"

SockAddr = tuple[str, int] | tuple[str, int, int, int]


class EgressConfigError(RuntimeError):
    """A fail-closed startup/profile refusal; ``code`` is safe to print."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class EgressRequestError(RuntimeError):
    """A per-CONNECT refusal with a short, non-echoing reason code."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class TunnelStream(Protocol):
    """The byte-stream surface shared by a real socket and an injected fake."""

    def recv(self, size: int, /) -> bytes: ...

    def sendall(self, data: bytes, /) -> None: ...

    def settimeout(self, value: float | None, /) -> None: ...

    def shutdown(self, how: int, /) -> None: ...

    def close(self) -> None: ...


class Resolver(Protocol):
    """Resolve a validated provider name once; tests inject a fake."""

    def __call__(self, host: str, port: int, /) -> Sequence[ResolvedAddress]: ...


class Connector(Protocol):
    """Dial one validated, pinned address; tests inject a fake."""

    def dial(self, address: ResolvedAddress, *, timeout: float) -> TunnelStream: ...


class LocalAddressReader(Protocol):
    """Return the textual addresses assigned to host interfaces."""

    def __call__(self, /) -> frozenset[str]: ...


@dataclass(frozen=True)
class ResolvedAddress:
    """One resolver answer: the socket family, textual IP and its sockaddr."""

    family: int
    ip: str
    sockaddr: SockAddr


@dataclass(frozen=True)
class EgressProfile:
    """The effective allowlist chosen by the supervisor for one session."""

    adapter_kind: str
    hosts: frozenset[str]


class _SocketConnector:
    """Production connector: a blocking socket with a bounded dial timeout."""

    def dial(self, address: ResolvedAddress, *, timeout: float) -> TunnelStream:
        connection = socket.socket(cast(socket.AddressFamily, address.family), socket.SOCK_STREAM)
        try:
            connection.settimeout(timeout)
            result = connection.connect_ex(address.sockaddr)
            if result != 0:
                raise OSError(result, os.strerror(result))
            connection.settimeout(None)
        except BaseException:
            connection.close()
            raise
        return connection


def system_resolver(host: str, port: int) -> list[ResolvedAddress]:
    """The production resolver: one bounded ``getaddrinfo(AF_UNSPEC)`` pass."""

    entries = socket.getaddrinfo(
        host,
        port,
        family=socket.AF_UNSPEC,
        type=socket.SOCK_STREAM,
        proto=socket.IPPROTO_TCP,
    )
    resolved: list[ResolvedAddress] = []
    seen: set[tuple[int, str]] = set()
    for family, _kind, _protocol, _canonical, sockaddr in entries:
        ip = str(sockaddr[0])
        key = (int(family), ip)
        if key in seen:
            continue
        seen.add(key)
        resolved.append(
            ResolvedAddress(family=int(family), ip=ip, sockaddr=cast(SockAddr, tuple(sockaddr)))
        )
    return resolved


def normalize_host(raw: str) -> str:
    """Normalise one provider name or raise ``EgressRequestError`` (design step 2)."""

    if not isinstance(raw, str) or not raw or len(raw) > 4 * MAX_HOSTNAME_LENGTH:
        raise EgressRequestError("invalid_authority")
    if raw.endswith("."):
        raise EgressRequestError("invalid_authority")
    try:
        ascii_name = raw.encode("idna").decode("ascii").lower()
    except ValueError as exc:
        raise EgressRequestError("invalid_authority") from exc
    if ascii_name.endswith(".") or len(ascii_name) > MAX_HOSTNAME_LENGTH:
        raise EgressRequestError("invalid_authority")
    if ":" in ascii_name or "[" in ascii_name or "]" in ascii_name:
        raise EgressRequestError("ip_literal")
    # ``inet_aton`` accepts every legacy IPv4 spelling: dotted decimal, octal,
    # hexadecimal, fewer parts and a single 32-bit integer.
    try:
        socket.inet_aton(ascii_name)
    except OSError:
        pass
    else:
        raise EgressRequestError("ip_literal")
    labels = ascii_name.split(".")
    if any(_HOST_LABEL.fullmatch(label) is None for label in labels):
        raise EgressRequestError("invalid_authority")
    if labels[-1].isdigit():
        raise EgressRequestError("ip_literal")
    return ascii_name


def parse_authority(authority: str) -> tuple[str, int]:
    """Parse exactly ``host:443``; any other spelling is refused (design step 1)."""

    if not isinstance(authority, str) or not authority:
        raise EgressRequestError("invalid_authority")
    if any(ord(character) < 0x21 or ord(character) > 0x7E for character in authority):
        raise EgressRequestError("invalid_authority")
    if "[" in authority or "]" in authority:
        raise EgressRequestError("ip_literal")
    if authority.count(":") != 1:
        raise EgressRequestError("invalid_authority")
    raw_host, raw_port = authority.split(":", 1)
    if raw_port != str(CONNECT_PORT):
        raise EgressRequestError("port_not_allowed")
    return normalize_host(raw_host), CONNECT_PORT


def load_egress_profile(path: str | Path, *, adapter_kind: str) -> EgressProfile:
    """Load ``autowork-session-egress.v1`` and intersect it with the maximum.

    A missing, linked, non-0600, oversized or malformed document, an unknown
    adapter or an empty effective set raises ``egress_profile_unavailable``:
    there is no fallback profile.
    """

    maximum = ADAPTER_EGRESS_MAXIMUM.get(adapter_kind)
    if maximum is None:
        raise EgressConfigError("egress_profile_unavailable")
    payload = _read_profile_document(Path(path))
    if not isinstance(payload, dict) or set(payload) != {"schema", "profiles"}:
        raise EgressConfigError("egress_profile_unavailable")
    if payload.get("schema") != EGRESS_PROFILE_SCHEMA:
        raise EgressConfigError("egress_profile_unavailable")
    profiles = payload.get("profiles")
    if not isinstance(profiles, dict) or not set(profiles) <= set(ADAPTER_EGRESS_MAXIMUM):
        raise EgressConfigError("egress_profile_unavailable")
    entry = profiles.get(adapter_kind)
    if not isinstance(entry, dict) or set(entry) != {"hosts"}:
        raise EgressConfigError("egress_profile_unavailable")
    hosts = entry["hosts"]
    if not isinstance(hosts, list) or len(hosts) > MAX_PROFILE_HOSTS:
        raise EgressConfigError("egress_profile_unavailable")
    normalized: set[str] = set()
    for host in hosts:
        if not isinstance(host, str):
            raise EgressConfigError("egress_profile_unavailable")
        try:
            normalized.add(normalize_host(host))
        except EgressRequestError as exc:
            raise EgressConfigError("egress_profile_unavailable") from exc
    effective = frozenset(normalized) & maximum
    if not effective:
        raise EgressConfigError("egress_profile_unavailable")
    return EgressProfile(adapter_kind=adapter_kind, hosts=effective)


def read_local_addresses() -> frozenset[str]:
    """Read the host's assigned addresses from the kernel procfs tables.

    IPv4 addresses come from the ``Local:`` table of ``/proc/net/fib_trie``;
    IPv6 addresses are the ``/128`` destinations of ``/proc/net/ipv6_route``.
    An unreadable file only removes this *extra* refusal: the private,
    loopback and link-local ranges are always refused statically.
    """

    try:
        trie = FIB_TRIE_PATH.read_text(encoding="utf-8", errors="replace")
    except OSError:
        trie = ""
    try:
        routes = IPV6_ROUTE_PATH.read_text(encoding="utf-8", errors="replace")
    except OSError:
        routes = ""
    return _local_addresses_from_tables(trie, routes)


def _local_addresses_from_tables(trie: str, routes: str) -> frozenset[str]:
    found: set[str] = set()
    in_local_table = False
    for line in trie.splitlines():
        stripped = line.strip()
        if stripped == "Local:":
            in_local_table = True
            continue
        if in_local_table and not line[:1].isspace() and stripped.endswith(":"):
            in_local_table = False
            continue
        if not in_local_table:
            continue
        match = _FIB_TRIE_LOCAL.search(stripped)
        if match is not None:
            address = match.group(1)
            if _parse_ipv4(address) is not None:
                found.add(address)
    for line in routes.splitlines():
        fields = line.split()
        if len(fields) < 2 or fields[1].lower() != "80" or len(fields[0]) != 32:
            continue
        try:
            raw = bytes.fromhex(fields[0])
        except ValueError:
            continue
        if len(raw) != 16 or raw == bytes(16):
            continue
        found.add(_format_ipv6(raw))
    return frozenset(found)


class EgressProxy:
    """The per-session CONNECT listener, policy engine, tunnel and log ring."""

    def __init__(
        self,
        *,
        socket_path: str | Path,
        log_path: str | Path,
        session_id: str,
        profile: EgressProfile,
        resolver: Resolver | None = None,
        connector: Connector | None = None,
        local_addresses: LocalAddressReader | None = None,
        clock: Callable[[], float] | None = None,
        request_timeout_seconds: float = CONNECT_REQUEST_TIMEOUT_SECONDS,
        resolve_timeout_seconds: float = RESOLVE_TIMEOUT_SECONDS,
        connect_timeout_seconds: float = CONNECT_TIMEOUT_SECONDS,
        idle_timeout_seconds: float = TUNNEL_IDLE_TIMEOUT_SECONDS,
        budget_wall_seconds: float | None = None,
    ) -> None:
        maximum = ADAPTER_EGRESS_MAXIMUM.get(profile.adapter_kind)
        if maximum is None or not profile.hosts or not profile.hosts <= maximum:
            raise EgressConfigError("egress_profile_unavailable")
        for host in profile.hosts:
            try:
                if normalize_host(host) != host:
                    raise EgressConfigError("egress_profile_unavailable")
            except EgressRequestError as exc:
                raise EgressConfigError("egress_profile_unavailable") from exc
        if min(
            request_timeout_seconds,
            resolve_timeout_seconds,
            connect_timeout_seconds,
            idle_timeout_seconds,
        ) <= 0 or (budget_wall_seconds is not None and budget_wall_seconds <= 0):
            raise ValueError("egress timeouts must be positive")
        if not session_id:
            raise ValueError("session_id is required")
        self._socket_path = Path(socket_path)
        self._log_path = Path(log_path)
        self._session_id = session_id
        self._profile = profile
        self._resolver = resolver if resolver is not None else system_resolver
        self._connector = connector if connector is not None else _SocketConnector()
        self._local_addresses = (
            local_addresses if local_addresses is not None else read_local_addresses
        )
        self._clock = clock if clock is not None else time.monotonic
        self._request_timeout_seconds = request_timeout_seconds
        self._resolve_timeout_seconds = resolve_timeout_seconds
        self._connect_timeout_seconds = connect_timeout_seconds
        self._idle_timeout_seconds = idle_timeout_seconds
        self._budget_wall_seconds = budget_wall_seconds
        self._budget_deadline: float | None = None
        self._log = _EgressLog(self._log_path)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._listener: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._client_threads: list[threading.Thread] = []
        self._client_slots = threading.BoundedSemaphore(MAX_CONCURRENT_CONNECTIONS)
        self._resolver_slots = threading.BoundedSemaphore(MAX_CONCURRENT_RESOLVERS)
        self._active_clients = 0
        self._tunnels: set[_Tunnel] = set()
        self._active_tunnels = 0
        self._revoked = False
        self._closed = False

    @property
    def socket_path(self) -> Path:
        return self._socket_path

    @property
    def profile(self) -> EgressProfile:
        return self._profile

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def active_connections(self) -> int:
        with self._lock:
            return self._active_clients

    def start(self) -> None:
        """Bind the 0600 pathname listener and accept CONNECTs in a thread."""

        with self._lock:
            if self._closed:
                raise EgressConfigError("egress_closed")
            if self._listener is not None:
                return
            if not self._socket_path.is_absolute():
                raise EgressConfigError("egress_socket_path_invalid")
            if len(os.fsencode(str(self._socket_path))) > AF_UNIX_PATH_MAX_BYTES:
                raise EgressConfigError("egress_socket_path_too_long")
            self._socket_path.parent.mkdir(mode=PRIVATE_DIRECTORY_MODE, parents=True, exist_ok=True)
            try:
                info = self._socket_path.lstat()
            except FileNotFoundError:
                info = None
            if info is not None:
                if not stat.S_ISSOCK(info.st_mode):
                    raise EgressConfigError("egress_socket_exists")
                self._socket_path.unlink()
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                listener.bind(str(self._socket_path))
                os.chmod(self._socket_path, EGRESS_SOCKET_MODE)
                listener.listen(MAX_CONCURRENT_CONNECTIONS * 2)
                listener.settimeout(0.2)
            except OSError as exc:
                listener.close()
                raise EgressConfigError("egress_socket_unavailable") from exc
            self._listener = listener
            if self._budget_wall_seconds is not None:
                self._budget_deadline = self._clock() + self._budget_wall_seconds
            thread = threading.Thread(target=self._accept_loop, name="session-egress", daemon=True)
            self._thread = thread
            thread.start()

    def revoke(self) -> None:
        """Refuse every new CONNECT and close the open tunnels immediately."""

        with self._lock:
            if self._revoked:
                return
            self._revoked = True
            tunnels = tuple(self._tunnels)
            self._tunnels.clear()
        for tunnel in tunnels:
            tunnel.close()

    def close(self) -> None:
        """Revoke, stop accepting, unlink the socket and join the workers."""

        with self._lock:
            if self._closed:
                return
            self._closed = True
        self.revoke()
        self._stop.set()
        with self._lock:
            listener, self._listener = self._listener, None
            thread, self._thread = self._thread, None
            pending = list(self._client_threads)
        if listener is not None:
            try:
                listener.close()
            except OSError:
                pass
        deadline = time.monotonic() + CLOSE_JOIN_SECONDS
        if thread is not None:
            thread.join(timeout=max(0.0, deadline - time.monotonic()))
        for entry in pending:
            entry.join(timeout=max(0.0, deadline - time.monotonic()))
        try:
            info = self._socket_path.lstat()
        except FileNotFoundError:
            return
        if stat.S_ISSOCK(info.st_mode):
            try:
                self._socket_path.unlink()
            except OSError:
                pass

    # -- listener ----------------------------------------------------------

    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            listener = self._listener
            if listener is None:
                return
            try:
                connection, _address = listener.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            if not self._client_slots.acquire(blocking=False):
                # Over the simultaneous-connection ceiling: refuse without
                # starting a worker thread, so idle CONNECTs cannot exhaust the
                # host process the way one thread per connection would.
                try:
                    connection.settimeout(0.1)
                except OSError:
                    pass
                _respond(connection, 503, "too_many_connections")
                self._log_event(DECISION_DENY, "too_many_connections", host="", port=0)
                try:
                    connection.close()
                except OSError:
                    pass
                continue
            with self._lock:
                if self._closed or self._stop.is_set():
                    self._client_slots.release()
                    try:
                        connection.close()
                    except OSError:
                        pass
                    return
                self._client_threads = [entry for entry in self._client_threads if entry.is_alive()]
                thread = threading.Thread(
                    target=self._handle_client,
                    args=(connection,),
                    name="session-egress-client",
                    daemon=True,
                )
                self._client_threads.append(thread)
            try:
                thread.start()
            except BaseException:
                self._client_slots.release()
                try:
                    connection.close()
                except OSError:
                    pass
                raise

    # -- one CONNECT -------------------------------------------------------

    def _handle_client(self, connection: socket.socket) -> None:
        with self._lock:
            self._active_clients += 1
        tunnel: _Tunnel | None = None
        reserved = False
        try:
            if self._revoked:
                _respond(connection, 403, "egress_revoked")
                self._log_event(DECISION_DENY, "egress_revoked", host="", port=0)
                return
            if self._budget_expired():
                _respond(connection, 503, "egress_budget_exhausted")
                self._log_event(DECISION_DENY, "egress_budget_exhausted", host="", port=0)
                return
            try:
                request = _read_connect_request(
                    connection, self._request_timeout_seconds, self._clock
                )
            except EgressRequestError as exc:
                _respond(connection, 431 if exc.code == "request_too_large" else 408, exc.code)
                self._log_event(DECISION_DENY, exc.code, host="", port=0)
                return
            if request is None:
                return
            head, leftover = request
            try:
                request_line = head.split(b"\r\n", 1)[0].decode("ascii")
            except UnicodeDecodeError:
                request_line = ""
            parts = request_line.split(" ")
            if len(parts) != 3 or not all(parts):
                _respond(connection, 400, "invalid_request")
                self._log_event(DECISION_DENY, "invalid_request", host="", port=0)
                return
            method, authority, version = parts
            if method != "CONNECT":
                _respond(connection, 405, "method_not_allowed")
                self._log_event(DECISION_DENY, "method_not_allowed", host="", port=0)
                return
            if version != "HTTP/1.1":
                _respond(connection, 400, "invalid_request")
                self._log_event(DECISION_DENY, "invalid_request", host="", port=0)
                return
            try:
                host, port = parse_authority(authority)
            except EgressRequestError as exc:
                _respond(connection, 403 if exc.code == "ip_literal" else 400, exc.code)
                # Never echo a path, query or token from an unvalidated
                # authority into the log (design §5).
                self._log_event(DECISION_DENY, exc.code, host="", port=0)
                return
            if host not in self._profile.hosts:
                _respond(connection, 403, "host_not_allowed")
                self._log_event(DECISION_DENY, "host_not_allowed", host=host, port=port)
                return
            if self._revoked:
                _respond(connection, 403, "egress_revoked")
                self._log_event(DECISION_DENY, "egress_revoked", host=host, port=port)
                return
            if self._budget_expired():
                _respond(connection, 503, "egress_budget_exhausted")
                self._log_event(DECISION_DENY, "egress_budget_exhausted", host=host, port=port)
                return
            try:
                addresses = _call_with_timeout(
                    lambda: self._resolver(host, port),
                    self._resolve_timeout_seconds,
                    slots=self._resolver_slots,
                )
            except EgressRequestError as exc:
                status = 503 if exc.code == "resolver_busy" else 502
                _respond(connection, status, exc.code)
                self._log_event(DECISION_DENY, exc.code, host=host, port=port)
                return
            except Exception:
                _respond(connection, 502, "resolution_failed")
                self._log_event(DECISION_DENY, "resolution_failed", host=host, port=port)
                return
            if not addresses:
                _respond(connection, 502, "resolution_failed")
                self._log_event(DECISION_DENY, "resolution_failed", host=host, port=port)
                return
            local = self._local_address_set()
            for address in addresses:
                if _address_forbidden(address, local) is not None:
                    _respond(connection, 403, "address_forbidden")
                    self._log_event(DECISION_DENY, "address_forbidden", host=host, port=port)
                    return
            if self._revoked:
                _respond(connection, 403, "egress_revoked")
                self._log_event(DECISION_DENY, "egress_revoked", host=host, port=port)
                return
            if not self._reserve_tunnel():
                if self._revoked:
                    _respond(connection, 403, "egress_revoked")
                    self._log_event(DECISION_DENY, "egress_revoked", host=host, port=port)
                else:
                    _respond(connection, 503, "too_many_tunnels")
                    self._log_event(DECISION_DENY, "too_many_tunnels", host=host, port=port)
                return
            reserved = True
            pinned: ResolvedAddress | None = None
            upstream: TunnelStream | None = None
            for address in addresses:
                if self._revoked:
                    break
                try:
                    upstream = self._connector.dial(address, timeout=self._connect_timeout_seconds)
                except Exception:
                    upstream = None
                    continue
                pinned = address
                break
            if upstream is None or pinned is None:
                if self._revoked:
                    _respond(connection, 403, "egress_revoked")
                    self._log_event(DECISION_DENY, "egress_revoked", host=host, port=port)
                else:
                    _respond(connection, 502, "connect_failed")
                    self._log_event(DECISION_DENY, "connect_failed", host=host, port=port)
                return
            tunnel = _Tunnel(
                connection,
                upstream,
                idle_timeout_seconds=self._idle_timeout_seconds,
                deadline=self._budget_deadline,
                clock=self._clock,
            )
            if not self._register_tunnel(tunnel):
                tunnel.close()
                _respond(connection, 403, "egress_revoked")
                self._log_event(DECISION_DENY, "egress_revoked", host=host, port=port)
                return
            try:
                connection.sendall(_CONNECT_ESTABLISHED)
            except OSError:
                tunnel.close()
                self._log_event(DECISION_DENY, "client_closed", host=host, port=port)
                return
            self._log_event(
                DECISION_ALLOW,
                "connected",
                host=host,
                port=port,
                family=_family_of(pinned),
            )
            tunnel.run(leftover)
        finally:
            if tunnel is not None:
                self._discard_tunnel(tunnel)
            if reserved:
                self._release_tunnel()
            if tunnel is None:
                try:
                    connection.close()
                except OSError:
                    pass
            # Release the slot before dropping the counter so a test (or the
            # accept loop) observing zero active clients also sees a free slot.
            self._client_slots.release()
            with self._lock:
                self._active_clients -= 1

    # -- policy helpers ----------------------------------------------------

    def _budget_expired(self) -> bool:
        deadline = self._budget_deadline
        return deadline is not None and self._clock() >= deadline

    def _local_address_set(self) -> frozenset[bytes]:
        try:
            values = self._local_addresses()
        except Exception:
            return frozenset()
        parsed: set[bytes] = set()
        for value in values:
            result = _parse_ip(value)
            if result is not None:
                parsed.add(result[1])
        return frozenset(parsed)

    def _reserve_tunnel(self) -> bool:
        with self._lock:
            if self._revoked or self._active_tunnels >= MAX_CONCURRENT_TUNNELS:
                return False
            self._active_tunnels += 1
            return True

    def _release_tunnel(self) -> None:
        with self._lock:
            self._active_tunnels = max(0, self._active_tunnels - 1)

    def _register_tunnel(self, tunnel: _Tunnel) -> bool:
        with self._lock:
            if self._revoked:
                return False
            self._tunnels.add(tunnel)
            return True

    def _discard_tunnel(self, tunnel: _Tunnel) -> None:
        with self._lock:
            self._tunnels.discard(tunnel)
        tunnel.close()

    def _log_event(
        self,
        decision: str,
        reason: str,
        *,
        host: str,
        port: int,
        family: str | None = None,
    ) -> None:
        self._log.append(
            {
                "at": _utc_timestamp(),
                "session_id": self._session_id,
                "adapter_kind": self._profile.adapter_kind,
                "host": host,
                "port": port,
                "decision": decision,
                "reason": reason,
                "family": family,
            }
        )


class _Tunnel:
    """Two pump threads over the client and pinned-upstream streams."""

    def __init__(
        self,
        client: TunnelStream,
        upstream: TunnelStream,
        *,
        idle_timeout_seconds: float,
        deadline: float | None,
        clock: Callable[[], float],
    ) -> None:
        self._client = client
        self._upstream = upstream
        self._idle_timeout_seconds = idle_timeout_seconds
        self._deadline = deadline
        self._clock = clock
        self._lock = threading.Lock()
        self._closed = False

    def run(self, initial: bytes) -> None:
        try:
            if initial:
                self._upstream.sendall(initial)
            self._client.settimeout(self._idle_timeout_seconds)
            self._upstream.settimeout(self._idle_timeout_seconds)
        except OSError:
            self.close()
            return
        pumps = [
            threading.Thread(
                target=self._pump,
                args=(self._client, self._upstream),
                name="session-egress-up",
                daemon=True,
            ),
            threading.Thread(
                target=self._pump,
                args=(self._upstream, self._client),
                name="session-egress-down",
                daemon=True,
            ),
        ]
        for pump in pumps:
            pump.start()
        for pump in pumps:
            pump.join()

    def _pump(self, source: TunnelStream, sink: TunnelStream) -> None:
        try:
            while True:
                timeout = self._idle_timeout_seconds
                if self._deadline is not None:
                    remaining = self._deadline - self._clock()
                    if remaining <= 0:
                        break
                    timeout = min(timeout, remaining)
                source.settimeout(timeout)
                chunk = source.recv(TUNNEL_BUFFER_BYTES)
                if not chunk:
                    break
                sink.sendall(chunk)
        except OSError:
            pass
        finally:
            self.close()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        for stream in (self._client, self._upstream):
            try:
                stream.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        for stream in (self._client, self._upstream):
            try:
                stream.close()
            except OSError:
                pass


class _EgressLog:
    """A bounded append-only JSONL ring: 0600, whole-line pruning.

    Appends stay O(1) until the buffer reaches a 1.25x high-water mark; only
    then is the whole file compacted back to ``max_bytes`` (never more than
    one rewrite per quarter of the ring), so a fast-denied CONNECT loop cannot
    turn every event into a full-file rewrite.
    """

    def __init__(self, path: Path, *, max_bytes: int = EGRESS_LOG_MAX_BYTES) -> None:
        self._path = path
        self._max_bytes = max_bytes
        self._high_bytes = max_bytes + max_bytes // 4
        self._lock = threading.Lock()
        self._lines: deque[bytes] = deque()
        self._total_bytes = 0

    def append(self, payload: Mapping[str, object]) -> None:
        line = json.dumps(dict(payload), separators=(",", ":"), ensure_ascii=True).encode() + b"\n"
        with self._lock:
            self._lines.append(line)
            self._total_bytes += len(line)
            try:
                if self._total_bytes <= self._high_bytes:
                    self._append_line(line)
                    return
                while self._total_bytes > self._max_bytes and len(self._lines) > 1:
                    self._total_bytes -= len(self._lines.popleft())
                self._rewrite()
            except OSError:
                # Losing an observability line never changes a policy decision.
                pass

    def _append_line(self, line: bytes) -> None:
        parent_fd = os.open(
            self._path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        )
        try:
            fd = os.open(
                self._path.name,
                os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC,
                EGRESS_LOG_FILE_MODE,
                dir_fd=parent_fd,
            )
            try:
                os.fchmod(fd, EGRESS_LOG_FILE_MODE)
                os.write(fd, line)
            finally:
                os.close(fd)
        finally:
            os.close(parent_fd)

    def _rewrite(self) -> None:
        payload = b"".join(self._lines)
        parent_fd = os.open(
            self._path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        )
        temporary = f".{self._path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
        try:
            fd = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW | os.O_CLOEXEC,
                EGRESS_LOG_FILE_MODE,
                dir_fd=parent_fd,
            )
            try:
                os.fchmod(fd, EGRESS_LOG_FILE_MODE)
                view = memoryview(payload)
                while view:
                    written = os.write(fd, view)
                    view = view[written:]
                os.fsync(fd)
            finally:
                os.close(fd)
            os.rename(temporary, self._path.name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
            os.fsync(parent_fd)
        except BaseException:
            try:
                os.unlink(temporary, dir_fd=parent_fd)
            except OSError:
                pass
            raise
        finally:
            os.close(parent_fd)


def _read_profile_document(path: Path) -> object:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError as exc:
        raise EgressConfigError("egress_profile_unavailable") from exc
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_size > EGRESS_PROFILE_MAX_BYTES
        ):
            raise EgressConfigError("egress_profile_unavailable")
        chunks: list[bytes] = []
        remaining = EGRESS_PROFILE_MAX_BYTES + 1
        while remaining > 0:
            chunk = os.read(fd, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
    finally:
        os.close(fd)
    data = b"".join(chunks)
    if not data or len(data) > EGRESS_PROFILE_MAX_BYTES:
        raise EgressConfigError("egress_profile_unavailable")
    try:
        return json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EgressConfigError("egress_profile_unavailable") from exc


def _read_connect_request(
    connection: socket.socket, timeout: float, clock: Callable[[], float]
) -> tuple[bytes, bytes] | None:
    """Read the bounded CONNECT head; return ``(head, early tunnel bytes)``."""

    deadline = clock() + timeout
    buffer = bytearray()
    while True:
        end = buffer.find(b"\r\n\r\n")
        if end >= 0:
            if end + 4 > MAX_CONNECT_REQUEST_BYTES:
                raise EgressRequestError("request_too_large")
            return bytes(buffer[: end + 4]), bytes(buffer[end + 4 :])
        if len(buffer) > MAX_CONNECT_REQUEST_BYTES:
            raise EgressRequestError("request_too_large")
        remaining = deadline - clock()
        if remaining <= 0:
            raise EgressRequestError("request_timeout")
        connection.settimeout(remaining)
        try:
            chunk = connection.recv(TUNNEL_BUFFER_BYTES)
        except TimeoutError as exc:
            raise EgressRequestError("request_timeout") from exc
        except OSError:
            return None
        if not chunk:
            return None
        buffer.extend(chunk)


def _respond(connection: socket.socket, status: int, code: str) -> None:
    body = (code + "\n").encode("ascii", "ignore")
    head = (
        f"HTTP/1.1 {status} {_STATUS_PHRASES[status]}\r\n"
        "Content-Type: text/plain; charset=utf-8\r\n"
        f"Content-Length: {len(body)}\r\n"
        "Connection: close\r\n\r\n"
    ).encode("ascii")
    try:
        connection.sendall(head + body)
    except OSError:
        pass


def _utc_timestamp() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _call_with_timeout[T](
    callback: Callable[[], T],
    timeout: float,
    *,
    slots: threading.BoundedSemaphore | None = None,
) -> T:
    """Run one blocking resolver call under a wall-clock ceiling.

    A timed-out thread cannot be cancelled; ``slots`` keeps the number of
    abandoned resolver threads bounded and caps a slot until the call really
    returns.  With no free slot the CONNECT is refused with ``resolver_busy``.
    """

    if slots is not None and not slots.acquire(blocking=False):
        raise EgressRequestError("resolver_busy")

    outcome: list[T] = []
    failure: list[BaseException] = []
    done = threading.Event()

    def run() -> None:
        try:
            outcome.append(callback())
        except BaseException as exc:  # re-raised in the caller, never swallowed
            failure.append(exc)
        finally:
            done.set()
            if slots is not None:
                slots.release()

    try:
        worker = threading.Thread(target=run, name="session-egress-resolve", daemon=True)
        worker.start()
    except BaseException:
        if slots is not None:
            slots.release()
        raise
    if not done.wait(timeout):
        raise EgressRequestError("resolution_timeout")
    if failure:
        raise failure[0]
    return outcome[0]


def _parse_ipv4(text: str) -> bytes | None:
    """Strict dotted-quad only: no leading zeros, no short or integer forms."""

    if _IPV4_TEXT.fullmatch(text) is None:
        return None
    parts = text.split(".")
    if any(len(part) > 1 and part.startswith("0") for part in parts):
        return None
    values = [int(part) for part in parts]
    if any(value > 255 for value in values):
        return None
    return bytes(values)


def _parse_ipv6(text: str) -> bytes | None:
    if not text or "%" in text or text.count("::") > 1:
        return None
    if "::" in text:
        head_text, tail_text = text.split("::", 1)
        head_groups = head_text.split(":") if head_text else []
        tail_groups = tail_text.split(":") if tail_text else []
        if any(group == "" for group in head_groups + tail_groups):
            return None
        head_words = _ipv6_groups_to_words(head_groups)
        tail_words = _ipv6_groups_to_words(tail_groups)
        if head_words is None or tail_words is None:
            return None
        total = len(head_words) + len(tail_words)
        if total >= 8:
            return None
        words = head_words + [0] * (8 - total) + tail_words
    else:
        groups = text.split(":")
        if any(group == "" for group in groups):
            return None
        parsed = _ipv6_groups_to_words(groups)
        if parsed is None or len(parsed) != 8:
            return None
        words = parsed
    return b"".join(word.to_bytes(2, "big") for word in words)


def _ipv6_groups_to_words(groups: Sequence[str]) -> list[int] | None:
    words: list[int] = []
    for index, group in enumerate(groups):
        if "." in group:
            if index != len(groups) - 1:
                return None
            ipv4 = _parse_ipv4(group)
            if ipv4 is None:
                return None
            words.extend((int.from_bytes(ipv4[:2], "big"), int.from_bytes(ipv4[2:], "big")))
            continue
        if _IPV6_GROUP.fullmatch(group) is None:
            return None
        words.append(int(group, 16))
    return words


def _format_ipv6(raw: bytes) -> str:
    return ":".join(
        f"{int.from_bytes(raw[index : index + 2], 'big'):x}" for index in range(0, 16, 2)
    )


def _parse_ip(text: str) -> tuple[str, bytes] | None:
    if ":" in text:
        raw = _parse_ipv6(text)
        return None if raw is None else ("ipv6", raw)
    raw = _parse_ipv4(text)
    return None if raw is None else ("ipv4", raw)


def _ipv4_forbidden_reason(raw: bytes) -> str | None:
    first, second, third, _fourth = raw
    if first == 0:
        return "reserved"  # 0.0.0.0/8
    if first == 10:
        return "private"
    if first == 127:
        return "loopback"
    if first == 100 and 64 <= second <= 127:
        return "cgnat"  # 100.64.0.0/10
    if first == 169 and second == 254:
        return "link_local"  # includes the 169.254.169.254 metadata address
    if first == 172 and 16 <= second <= 31:
        return "private"
    if first == 192 and second == 168:
        return "private"
    if first == 192 and second == 0 and third == 0:
        return "reserved"  # 192.0.0.0/24, including NAT64 discovery
    if first == 192 and second == 0 and third == 2:
        return "documentation"
    if first == 192 and second == 88 and third == 99:
        return "reserved"  # deprecated 6to4 relay anycast
    if first == 198 and second in (18, 19):
        return "benchmark"  # 198.18.0.0/15
    if first == 198 and second == 51 and third == 100:
        return "documentation"
    if first == 203 and second == 0 and third == 113:
        return "documentation"
    if first >= 224:
        return "reserved"  # 224/4 multicast and 240/4 reserved/broadcast
    return None


def _ipv6_forbidden_reason(raw: bytes) -> str | None:
    if raw == bytes(16):
        return "unspecified"
    if raw == bytes(15) + b"\x01":
        return "loopback"
    if raw[:10] == bytes(10) and raw[10] == 0xFF and raw[11] == 0xFF:
        _ipv4_forbidden_reason(raw[12:16])  # validate the embedded IPv4 as well
        return "ipv4_mapped"
    if raw[:12] == bytes(12):
        return "ipv4_compatible"
    first = raw[0]
    if first & 0xFE == 0xFC:
        return "unique_local"  # fc00::/7
    if first == 0xFE and raw[1] & 0xC0 == 0x80:
        return "link_local"  # fe80::/10
    if first == 0xFE and raw[1] & 0xC0 == 0xC0:
        return "site_local"  # fec0::/10, deprecated
    if first == 0xFF:
        return "multicast"
    if raw[:2] == b"\x20\x01" and raw[2] <= 0x01:
        return "reserved"  # 2001::/23, including Teredo and benchmarking
    if raw[:4] == b"\x20\x01\x0d\xb8":
        return "documentation"  # 2001:db8::/32
    if raw[:2] == b"\x20\x02":
        return "6to4"  # 2002::/16
    if raw[:2] == b"\x3f\xff" and raw[2] & 0xF0 == 0:
        return "documentation"  # 3fff::/20
    if raw[:2] == b"\x5f\x00":
        return "reserved"  # 5f00::/16, SRv6 SIDs
    if raw[:8] == b"\x01\x00" + bytes(6):
        return "reserved"  # 100::/64 discard-only
    if raw[:12] == b"\x00\x64\xff\x9b" + bytes(8):
        return "nat64"  # 64:ff9b::/96
    if raw[:6] == b"\x00\x64\xff\x9b\x00\x01":
        return "nat64_local"  # 64:ff9b:1::/48
    return None


def _address_forbidden(address: ResolvedAddress, local: frozenset[bytes]) -> str | None:
    parsed = _parse_ip(address.ip)
    if parsed is None:
        return "invalid_address"
    family, raw = parsed
    reason = _ipv4_forbidden_reason(raw) if family == "ipv4" else _ipv6_forbidden_reason(raw)
    if reason is not None:
        return reason
    if raw in local:
        return "local_interface"
    return None


def _family_of(address: ResolvedAddress) -> str:
    return "ipv6" if ":" in address.ip else "ipv4"
