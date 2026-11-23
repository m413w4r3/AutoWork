"""Trusted in-sandbox entrypoint: netns attestation and provider forwarder.

This file is started by ``bwrap`` inside the reasoning-session network
namespace (``--unshare-net``: only ``lo``, no route, see
``docs/design/investigation-workshop/BROKER-RELAY-EGRESS.md`` §5).  It is
trusted code: the model never supplies it, the supervisor mounts it read-only
and it is launched before the CLI.  The session profile (``process.py``) wraps
the adapter's CLI argv with ``forward-and-exec``; the attestation is run in a
separate fresh namespace before any spawn is released.

``attest`` proves, from inside the namespace and without emitting a packet:

* ``if_nameindex()`` names exactly ``lo`` and ``lo`` is UP|LOOPBACK;
* ``/proc/net/route`` has no data line and ``/proc/net/ipv6_route`` has no
  usable route other than ``::1/128`` (the kernel's two ``::/0`` reject
  entries carry ``RTF_REJECT|RTF_NONEXTHOP`` and forward nothing);
* loopback TCP bind/connect works for IPv4 and IPv6;
* connects to ``10.0.0.1``, ``172.16.0.1``, ``192.168.0.1``,
  ``169.254.169.254``, ``192.0.2.1``, ``fe80::1`` and ``fc00::1`` fail with
  ``ENETUNREACH``/``EHOSTUNREACH``/``EPERM``/``EACCES``;
* when the supervisor passes its host-loopback helper port, that port is not
  reachable (the host helper itself checks that it never accepted a
  connection).

The output is one short deterministic JSON object on stdout.

``forward-and-exec`` validates the single fixed upstream AF_UNIX socket
(absolute path, real socket, at most 107 bytes), forks a forwarder child that
sets ``PR_SET_PDEATHSIG`` so it dies with the CLI, waits for the listener, then
``exec``s the CLI.  The child listens on ``127.0.0.1:3128`` and byte-pipes each
TCP connection to the upstream socket: it parses no request, resolves no name,
does TLS nothing and knows no destination.  At most 8 concurrent tunnels, one
64 KiB buffer per direction, both sides closed when either direction reaches
EOF.  The CLI environment is normalised to exactly the closed proxy literal;
no other proxy variable survives.

Only this file may name an INET socket family inside ``sessions/`` (the
architecture guard allowlists it): it never runs on the host, only inside the
route-less namespace described above.

Normative source: ``docs/design/investigation-workshop/BROKER-RELAY-EGRESS.md``
§5 and §6.
"""

from __future__ import annotations

import ctypes
import json
import os
import signal
import socket
import stat
import sys
import threading
import time
from typing import NoReturn

FORWARDER_LISTEN_HOST = "127.0.0.1"
FORWARDER_PORT = 3128
PROXY_URL = f"http://{FORWARDER_LISTEN_HOST}:{FORWARDER_PORT}"
MAX_CONCURRENT_TUNNELS = 8
TUNNEL_BUFFER_BYTES = 64 * 1024
UPSTREAM_CONNECT_TIMEOUT_SECONDS = 10.0
FORWARDER_READY_TIMEOUT_SECONDS = 5.0
AF_UNIX_PATH_MAX_BYTES = 107  # sockaddr_un.sun_path is 108 bytes including the NUL

ATTEST_SCHEMA = "autowork-netns-attest.v1"
ATTEST_TIMEOUT_SECONDS = 0.5
REQUIRED_BLOCKED_ADDRESSES: tuple[str, ...] = (
    "10.0.0.1",
    "172.16.0.1",
    "192.168.0.1",
    "169.254.169.254",
    "192.0.2.1",
    "fe80::1",
    "fc00::1",
)
ALLOWED_BLOCKED_ERRNOS: frozenset[int] = frozenset(
    {
        1,  # EPERM
        13,  # EACCES
        101,  # ENETUNREACH
        113,  # EHOSTUNREACH
    }
)
_ERRNO_NAMES = {
    0: "CONNECTED",
    1: "EPERM",
    13: "EACCES",
    101: "ENETUNREACH",
    111: "ECONNREFUSED",
    113: "EHOSTUNREACH",
}
IFF_UP = 0x1
IFF_LOOPBACK = 0x8
IFF_RUNNING = 0x40
RTF_REJECT = 0x0200
SIOCGIFFLAGS = 0x8913
PR_SET_PDEATHSIG = 1
IPV4_ROUTE_PATH = "/proc/net/route"
IPV6_ROUTE_PATH = "/proc/net/ipv6_route"
LOOPBACK_HOST_PORT_FLAG = "--host-loopback-port"
UPSTREAM_FLAG = "--upstream"


class _IfReq(ctypes.Structure):
    """The leading ``ifr_name``/``ifr_flags`` members of ``struct ifreq``."""

    _fields_ = [
        ("ifr_name", ctypes.c_char * 16),
        ("ifr_flags", ctypes.c_short),
        ("_padding", ctypes.c_byte * 14),
    ]


def _errno_name(code: int) -> str:
    return _ERRNO_NAMES.get(code, f"errno:{code}")


def interface_flags(name: str) -> int:
    """``SIOCGIFFLAGS`` for one interface, or ``-1`` when the ioctl fails."""

    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        request = _IfReq()
        request.ifr_name = name.encode("ascii", "replace")[:15]
        libc = ctypes.CDLL(None, use_errno=True)
        result = libc.ioctl(probe.fileno(), SIOCGIFFLAGS, ctypes.byref(request))
    except (OSError, AttributeError):
        return -1
    finally:
        probe.close()
    if result != 0:
        return -1
    return int(request.ifr_flags) & 0xFFFF


def _ipv4_route_lines() -> int:
    try:
        with open(IPV4_ROUTE_PATH, encoding="utf-8", errors="replace") as table:
            text = table.read()
    except OSError:
        return -1
    return sum(1 for line in text.splitlines() if line.strip() and not line.startswith("Iface"))


def _ipv6_routes() -> tuple[list[str], int, str | None]:
    """Return ``(usable routes, reject count, error)`` for ``/proc/net/ipv6_route``.

    A route line is ``dest prefix src srclen gateway metric refcnt use flags
    iface`` in hex.  The kernel always installs two ``::/0`` entries flagged
    ``RTF_REJECT|RTF_NONEXTHOP`` in a fresh namespace; they forward nothing and
    are counted, never treated as usable routes.
    """

    try:
        with open(IPV6_ROUTE_PATH, encoding="utf-8", errors="replace") as table:
            text = table.read()
    except OSError:
        return [], 0, "ipv6_route_unreadable"
    usable: list[str] = []
    rejects = 0
    for line in text.splitlines():
        fields = line.split()
        if not fields:
            continue
        if len(fields) < 10:
            return [], rejects, "ipv6_route_unparsable"
        try:
            prefix = int(fields[1], 16)
            flags = int(fields[8], 16)
        except ValueError:
            return [], rejects, "ipv6_route_unparsable"
        if flags & RTF_REJECT:
            rejects += 1
            continue
        if fields[0] == "0" * 31 + "1" and prefix == 128 and fields[9] == "lo":
            usable.append("::1/128")
            continue
        return [], rejects, "unexpected_ipv6_route"
    return sorted(usable), rejects, None


def _loopback_probe() -> tuple[dict[str, bool], list[str]]:
    results = {
        "v4_bind": False,
        "v4_connect": False,
        "v6_bind": False,
        "v6_connect": False,
    }
    errors: list[str] = []
    for family, address, prefix in (
        (socket.AF_INET, "127.0.0.1", "v4"),
        (socket.AF_INET6, "::1", "v6"),
    ):
        server = socket.socket(family, socket.SOCK_STREAM)
        client = socket.socket(family, socket.SOCK_STREAM)
        try:
            server.settimeout(2.0)
            server.bind((address, 0))
            server.listen(1)
            results[f"{prefix}_bind"] = True
            port = int(server.getsockname()[1])
            client.settimeout(2.0)
            if client.connect_ex((address, port)) != 0:
                errors.append(f"loopback_{prefix}_connect_failed")
                continue
            accepted, _peer = server.accept()
            accepted.close()
            results[f"{prefix}_connect"] = True
        except OSError:
            errors.append(f"loopback_{prefix}_probe_failed")
        finally:
            client.close()
            server.close()
    return results, errors


def _blocked_connects() -> tuple[dict[str, str], list[str]]:
    blocked: dict[str, str] = {}
    errors: list[str] = []
    try:
        lo_index = socket.if_nametoindex("lo")
    except OSError:
        lo_index = 0
    for address in REQUIRED_BLOCKED_ADDRESSES:
        if ":" in address:
            family = socket.AF_INET6
            sockaddr: tuple[object, ...] = (address, 443, 0, lo_index)
        else:
            family = socket.AF_INET
            sockaddr = (address, 443)
        probe = socket.socket(family, socket.SOCK_STREAM)
        try:
            probe.settimeout(ATTEST_TIMEOUT_SECONDS)
            code = probe.connect_ex(sockaddr)
        except OSError:
            code = -1
        finally:
            probe.close()
        blocked[address] = _errno_name(code)
        if code not in ALLOWED_BLOCKED_ERRNOS:
            errors.append(f"reachable_address:{address}")
    return blocked, errors


def _host_loopback_probe(port: int) -> tuple[str, str | None]:
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.settimeout(ATTEST_TIMEOUT_SECONDS)
        code = probe.connect_ex(("127.0.0.1", port))
    except OSError:
        code = -1
    finally:
        probe.close()
    if code == 0:
        return "reachable", "host_loopback_reachable"
    return "blocked", None


def attestation_evidence(host_loopback_port: int | None = None) -> dict[str, object]:
    """Collect the in-namespace evidence as a plain deterministic mapping."""

    errors: list[str] = []
    ifaces = sorted(name for _index, name in socket.if_nameindex())
    if ifaces != ["lo"]:
        errors.append("unexpected_interfaces")
    flags = interface_flags("lo")
    if flags < 0:
        errors.append("lo_flags_unreadable")
    elif flags & (IFF_UP | IFF_LOOPBACK) != (IFF_UP | IFF_LOOPBACK):
        errors.append("lo_not_up_and_loopback")
    ipv4_routes = _ipv4_route_lines()
    if ipv4_routes != 0:
        errors.append("unexpected_ipv4_route")
    ipv6_routes, ipv6_rejects, ipv6_error = _ipv6_routes()
    if ipv6_error is not None:
        errors.append(ipv6_error)
    elif ipv6_routes != ["::1/128"]:
        errors.append("unexpected_ipv6_route")
    loopback, loopback_errors = _loopback_probe()
    errors.extend(loopback_errors)
    blocked, blocked_errors = _blocked_connects()
    errors.extend(blocked_errors)
    host_loopback = "not_checked"
    if host_loopback_port is not None:
        host_loopback, host_loopback_error = _host_loopback_probe(host_loopback_port)
        if host_loopback_error is not None:
            errors.append(host_loopback_error)
    return {
        "schema": ATTEST_SCHEMA,
        "ok": not errors,
        "ifaces": ifaces,
        "lo_flags": flags,
        "ipv4_routes": ipv4_routes,
        "ipv6_routes": ipv6_routes,
        "ipv6_reject_routes": ipv6_rejects,
        "loopback": loopback,
        "blocked": blocked,
        "host_loopback": host_loopback,
        "errors": sorted(errors),
    }


def _attest(arguments: list[str]) -> int:
    port: int | None = None
    index = 0
    while index < len(arguments):
        if arguments[index] != LOOPBACK_HOST_PORT_FLAG or index + 1 >= len(arguments):
            _fail("usage: entrypoint.py attest [--host-loopback-port PORT]")
        try:
            port = int(arguments[index + 1], 10)
        except ValueError:
            _fail("usage: entrypoint.py attest [--host-loopback-port PORT]")
        if not 0 < port < 65536:
            _fail("usage: entrypoint.py attest [--host-loopback-port PORT]")
        index += 2
    try:
        evidence = attestation_evidence(port)
    except BaseException:
        evidence = {
            "schema": ATTEST_SCHEMA,
            "ok": False,
            "errors": ["attestation_crashed"],
        }
    sys.stdout.write(json.dumps(evidence, sort_keys=True, separators=(",", ":")) + "\n")
    sys.stdout.flush()
    return 0 if evidence["ok"] is True else 1


class _Tunnel:
    """Two pump threads over one client and one upstream byte stream."""

    def __init__(self, client: socket.socket, upstream: socket.socket) -> None:
        self._client = client
        self._upstream = upstream
        self._lock = threading.Lock()
        self._closed = False

    def run(self) -> None:
        pumps = [
            threading.Thread(target=self._pump, args=(self._client, self._upstream)),
            threading.Thread(target=self._pump, args=(self._upstream, self._client)),
        ]
        for pump in pumps:
            pump.start()
        for pump in pumps:
            pump.join()

    def _pump(self, source: socket.socket, sink: socket.socket) -> None:
        try:
            while True:
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


class Forwarder:
    """The 127.0.0.1:3128 TCP listener that byte-pipes to one fixed UDS."""

    def __init__(self, *, upstream_path: str, listen_port: int = FORWARDER_PORT) -> None:
        self._upstream_path = upstream_path
        self._listen_port = listen_port
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._listener: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._tunnels: set[_Tunnel] = set()
        self._active = 0

    @property
    def port(self) -> int:
        listener = self._listener
        if listener is None:
            return 0
        return int(listener.getsockname()[1])

    def start(self) -> None:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind((FORWARDER_LISTEN_HOST, self._listen_port))
            listener.listen(MAX_CONCURRENT_TUNNELS * 2)
            listener.settimeout(0.2)
        except OSError:
            listener.close()
            raise
        self._listener = listener
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._thread.start()

    def serve_forever(self) -> None:
        self._stop.wait()

    def close(self) -> None:
        self._stop.set()
        with self._lock:
            listener, self._listener = self._listener, None
            tunnels = tuple(self._tunnels)
            self._tunnels.clear()
        if listener is not None:
            try:
                listener.close()
            except OSError:
                pass
        for tunnel in tunnels:
            tunnel.close()

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
            if not self._reserve():
                connection.close()
                continue
            threading.Thread(target=self._handle, args=(connection,), daemon=True).start()

    def _reserve(self) -> bool:
        with self._lock:
            if self._stop.is_set() or self._active >= MAX_CONCURRENT_TUNNELS:
                return False
            self._active += 1
            return True

    def _release(self) -> None:
        with self._lock:
            self._active = max(0, self._active - 1)

    def _handle(self, connection: socket.socket) -> None:
        tunnel: _Tunnel | None = None
        try:
            if self._stop.is_set():
                return
            upstream = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                upstream.settimeout(UPSTREAM_CONNECT_TIMEOUT_SECONDS)
                upstream.connect(self._upstream_path)
                upstream.settimeout(None)
            except OSError:
                upstream.close()
                return
            connection.settimeout(None)
            tunnel = _Tunnel(connection, upstream)
            with self._lock:
                if self._stop.is_set():
                    tunnel.close()
                    return
                self._tunnels.add(tunnel)
            tunnel.run()
        finally:
            if tunnel is not None:
                with self._lock:
                    self._tunnels.discard(tunnel)
                tunnel.close()
            else:
                try:
                    connection.close()
                except OSError:
                    pass
            self._release()


def _set_parent_death_signal(signum: int) -> bool:
    """``prctl(PR_SET_PDEATHSIG)``; the forwarder must die with the CLI."""

    try:
        libc = ctypes.CDLL(None, use_errno=True)
        return bool(libc.prctl(PR_SET_PDEATHSIG, int(signum), 0, 0, 0) == 0)
    except (OSError, AttributeError):
        return False


def _validate_upstream(raw: str) -> str | None:
    """The single fixed upstream: absolute, an existing socket, <= 107 bytes."""

    if not raw or not os.path.isabs(raw):
        return "the egress upstream must be an absolute path"
    if len(os.fsencode(raw)) > AF_UNIX_PATH_MAX_BYTES:
        return "the egress upstream path is too long"
    try:
        info = os.lstat(raw)
    except OSError:
        return "the egress upstream socket is missing"
    if not stat.S_ISSOCK(info.st_mode):
        return "the egress upstream is not a socket"
    return None


def _proxy_environment(environ: dict[str, str]) -> dict[str, str]:
    """Exactly the closed proxy literal: six variables plus an empty NO_PROXY.

    Every key that mentions ``proxy`` is dropped first (an agent cannot smuggle
    an alternative proxy under another spelling), then the six canonical names
    and the empty ``NO_PROXY`` are set.
    """

    cleaned = {key: value for key, value in environ.items() if "proxy" not in key.lower()}
    for name in (
        "HTTPS_PROXY",
        "HTTP_PROXY",
        "ALL_PROXY",
        "https_proxy",
        "http_proxy",
        "all_proxy",
    ):
        cleaned[name] = PROXY_URL
    cleaned["NO_PROXY"] = ""
    return cleaned


def _wait_ready(ready_fd: int, timeout_seconds: float) -> bool:
    deadline = time.monotonic() + timeout_seconds
    os.set_blocking(ready_fd, False)
    while time.monotonic() < deadline:
        try:
            marker = os.read(ready_fd, 1)
        except BlockingIOError:
            marker = None
        if marker == b"1":
            return True
        if marker == b"":
            return False
        time.sleep(0.001)
    return False


def _forward_and_exec(arguments: list[str]) -> int:
    if len(arguments) < 4 or arguments[0] != UPSTREAM_FLAG or arguments[2] != "--":
        _fail("usage: entrypoint.py forward-and-exec --upstream PATH -- ARGV...")
    upstream_error = _validate_upstream(arguments[1])
    if upstream_error is not None:
        _fail(upstream_error)
    command = arguments[3:]
    if not command:
        _fail("usage: entrypoint.py forward-and-exec --upstream PATH -- ARGV...")
    upstream_path = arguments[1]
    ready_r, ready_w = os.pipe()
    child = os.fork()
    if child == 0:
        os.close(ready_r)
        parent = os.getppid()
        if not _set_parent_death_signal(signal.SIGKILL) or os.getppid() != parent:
            os._exit(1)
        try:
            devnull = os.open(os.devnull, os.O_RDWR)
            for fd in (0, 1, 2):
                os.dup2(devnull, fd)
            os.close(devnull)
            forwarder = Forwarder(upstream_path=upstream_path)
            forwarder.start()
            os.write(ready_w, b"1")
            os.close(ready_w)
            forwarder.serve_forever()
        except BaseException:
            os._exit(1)
        os._exit(0)
    os.close(ready_w)
    try:
        ready = _wait_ready(ready_r, FORWARDER_READY_TIMEOUT_SECONDS)
    finally:
        os.close(ready_r)
    if not ready:
        try:
            os.kill(child, signal.SIGKILL)
        except OSError:
            pass
        _fail("the provider forwarder did not start")
    try:
        os.execvpe(command[0], command, _proxy_environment(dict(os.environ)))
    except OSError:
        try:
            os.kill(child, signal.SIGKILL)
        except OSError:
            pass
        _fail("the sandboxed command could not be executed")
    return 1  # unreachable: execvpe either replaces the process or raises


def _fail(message: str) -> NoReturn:
    sys.stderr.write(message + "\n")
    sys.stderr.flush()
    raise SystemExit(2)


def main(argv: list[str]) -> int:
    if not argv:
        _fail("usage: entrypoint.py {attest|forward-and-exec} ...")
    command, arguments = argv[0], argv[1:]
    if command == "attest":
        return _attest(arguments)
    if command == "forward-and-exec":
        return _forward_and_exec(arguments)
    _fail("usage: entrypoint.py {attest|forward-and-exec} ...")
    return 2  # unreachable


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
