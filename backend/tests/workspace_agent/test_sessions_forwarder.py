"""NET-1 forwarder oracles: the in-sandbox byte pipe to the egress UDS.

The trusted ``entrypoint.py`` forwarder listens on ``127.0.0.1:3128`` inside the
``--unshare-net`` namespace and pipes each TCP connection to one fixed
``ipc/egress.sock``; it parses no request and knows no destination.  At most 8
tunnels are open at once, buffers are 64 KiB and both sides close when either
direction reaches EOF.

Simulated (S) oracles use the forwarder class and a real local AF_UNIX fake
upstream; the real (R) oracle runs the production ``spawn`` envelope inside
bwrap and reaches a fake host proxy socket.  No provider, LAN or external call
is ever made.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path

import pytest
from sidecars.workspace_agent.sessions import entrypoint, process

TEST_ENV = {
    "PATH": "/usr/bin:/bin",
    "HOME": "/tmp",
    "LANG": "C.UTF-8",
    "LC_ALL": "C.UTF-8",
}
PROXY_URL = "http://127.0.0.1:3128"
CLOSED_PROXY_KEYS = frozenset(
    {
        "HTTPS_PROXY",
        "HTTP_PROXY",
        "ALL_PROXY",
        "https_proxy",
        "http_proxy",
        "all_proxy",
        "NO_PROXY",
    }
)
MAX_TUNNELS = 8


@pytest.fixture
def short_dir() -> Path:
    """A /tmp directory short enough for an AF_UNIX pathname."""

    path = Path(tempfile.mkdtemp(prefix="fwd-", dir="/tmp"))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def _wait_until(predicate: Callable[[], bool], timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


class FakeUpstream:
    """A real AF_UNIX listener: counts connections and echoes every byte."""

    def __init__(self, path: Path) -> None:
        self._listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._listener.bind(str(path))
        self._listener.listen(MAX_TUNNELS * 4)
        self._listener.settimeout(0.2)
        self._lock = threading.Lock()
        self._connections: list[socket.socket] = []
        self._payload = bytearray()
        self.accepted = 0
        self._stop = threading.Event()
        threading.Thread(target=self._accept_loop, daemon=True).start()

    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            try:
                connection, _peer = self._listener.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            with self._lock:
                self.accepted += 1
                self._connections.append(connection)
            threading.Thread(target=self._echo_loop, args=(connection,), daemon=True).start()

    def _echo_loop(self, connection: socket.socket) -> None:
        connection.settimeout(0.2)
        while not self._stop.is_set():
            try:
                data = connection.recv(entrypoint.TUNNEL_BUFFER_BYTES)
            except TimeoutError:
                continue
            except OSError:
                return
            if not data:
                return
            with self._lock:
                self._payload.extend(data)
            try:
                connection.sendall(b"echo:" + data)
            except OSError:
                return

    def payload(self) -> bytes:
        with self._lock:
            return bytes(self._payload)

    def close(self) -> None:
        self._stop.set()
        try:
            self._listener.close()
        except OSError:
            pass
        with self._lock:
            connections = list(self._connections)
        for connection in connections:
            try:
                connection.close()
            except OSError:
                pass


def _connect(port: int) -> socket.socket:
    client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    client.settimeout(5.0)
    client.connect(("127.0.0.1", port))
    return client


def _recv_exactly(client: socket.socket, size: int) -> bytes:
    data = bytearray()
    while len(data) < size:
        chunk = client.recv(size - len(data))
        if not chunk:
            break
        data.extend(chunk)
    return bytes(data)


# ---------------------------------------------------------------------------
# Simulated oracles (S)
# ---------------------------------------------------------------------------


def test_forwarder_byte_pipes_both_directions_to_the_fixed_upstream(
    short_dir: Path,
) -> None:
    upstream_path = short_dir / "upstream.sock"
    upstream = FakeUpstream(upstream_path)
    forwarder = entrypoint.Forwarder(upstream_path=str(upstream_path), listen_port=0)
    forwarder.start()
    client = _connect(forwarder.port)
    try:
        client.sendall(b"ping")
        assert _recv_exactly(client, len(b"echo:ping")) == b"echo:ping"
        client.sendall(b"second")
        assert _recv_exactly(client, len(b"echo:second")) == b"echo:second"
        assert upstream.payload() == b"pingsecond"
        assert upstream.accepted == 1
    finally:
        client.close()
        forwarder.close()
        upstream.close()


def test_forwarder_caps_concurrent_tunnels_at_eight(short_dir: Path) -> None:
    upstream_path = short_dir / "upstream.sock"
    upstream = FakeUpstream(upstream_path)
    forwarder = entrypoint.Forwarder(upstream_path=str(upstream_path), listen_port=0)
    forwarder.start()
    clients: list[socket.socket] = []
    try:
        for index in range(MAX_TUNNELS):
            client = _connect(forwarder.port)
            client.sendall(f"tunnel-{index}".encode())
            expected = b"echo:tunnel-" + str(index).encode()
            assert _recv_exactly(client, len(expected)) == expected
            clients.append(client)
        assert _wait_until(lambda: upstream.accepted == MAX_TUNNELS)

        ninth = _connect(forwarder.port)
        try:
            ninth.settimeout(2.0)
            assert ninth.recv(1) == b""
        finally:
            ninth.close()
        time.sleep(0.2)
        assert upstream.accepted == MAX_TUNNELS
    finally:
        for client in clients:
            client.close()
        forwarder.close()
        upstream.close()


def test_forwarder_does_not_parse_the_client_request(short_dir: Path) -> None:
    """The client cannot choose a destination: bytes reach the one upstream."""

    upstream_path = short_dir / "upstream.sock"
    upstream = FakeUpstream(upstream_path)
    forwarder = entrypoint.Forwarder(upstream_path=str(upstream_path), listen_port=0)
    forwarder.start()
    client = _connect(forwarder.port)
    try:
        request = b"CONNECT forbidden.example:443\r\nX-Destination: 10.0.0.1\r\n\r\n"
        client.sendall(request)
        assert _recv_exactly(client, len(b"echo:") + len(request)) == b"echo:" + request
        assert upstream.payload() == request
        assert upstream.accepted == 1
    finally:
        client.close()
        forwarder.close()
        upstream.close()


def test_forwarder_validates_the_single_fixed_upstream(short_dir: Path) -> None:
    socket_path = short_dir / "upstream.sock"
    upstream = FakeUpstream(socket_path)
    try:
        assert entrypoint._validate_upstream(str(socket_path)) is None
    finally:
        upstream.close()
    assert entrypoint._validate_upstream("relative.sock") is not None
    assert entrypoint._validate_upstream("/nonexistent/egress.sock") is not None
    regular = short_dir / "regular"
    regular.write_bytes(b"x")
    assert entrypoint._validate_upstream(str(regular)) is not None
    too_long = "/tmp/" + "a" * 120 + ".sock"
    assert entrypoint._validate_upstream(too_long) == "the egress upstream path is too long"


def test_forwarder_closes_the_client_when_the_upstream_is_unusable(
    short_dir: Path,
) -> None:
    forwarder = entrypoint.Forwarder(upstream_path=str(short_dir / "missing.sock"), listen_port=0)
    forwarder.start()
    client = _connect(forwarder.port)
    try:
        client.settimeout(5.0)
        assert client.recv(1) == b""
    finally:
        client.close()
        forwarder.close()


def test_agent_env_cannot_bypass_netns_route() -> None:
    """The agent cannot smuggle an alternative proxy: the literal is closed.

    Real routing is proven separately in the netns (R); here the trusted
    entrypoint's environment normalisation is the simulated half of EG-07.
    """

    base = {
        "PATH": "/usr/bin:/bin",
        "HTTPS_PROXY": "http://provider-proxy:8080",
        "http_proxy": "http://other:3128",
        "NO_PROXY": "localhost,127.0.0.1",
        "no_proxy": "localhost",
        "FTP_PROXY": "socks5://elsewhere:1080",
        "PROXY_URL": "http://smuggled:1",
        "SOME_PROXY_TOKEN": "x",
        "KEEP_ME": "1",
    }
    environment = entrypoint._proxy_environment(base)
    assert {key for key in environment if "proxy" in key.lower()} == set(CLOSED_PROXY_KEYS)
    for name in ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY"):
        assert environment[name] == PROXY_URL
        assert environment[name.lower()] == PROXY_URL
    assert environment["NO_PROXY"] == ""
    assert environment["KEEP_ME"] == "1"
    assert environment["PATH"] == base["PATH"]


def test_forward_and_exec_cli_fails_closed_without_a_valid_upstream(
    short_dir: Path,
) -> None:
    script = str(entrypoint.__file__)
    missing_upstream = subprocess.run(
        [sys.executable, script, "forward-and-exec", "--", "/bin/true"],
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert missing_upstream.returncode == 2
    missing_socket = subprocess.run(
        [
            sys.executable,
            script,
            "forward-and-exec",
            "--upstream",
            str(short_dir / "missing.sock"),
            "--",
            "/bin/true",
        ],
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert missing_socket.returncode == 2
    assert b"missing" in missing_socket.stderr
    unknown = subprocess.run(
        [sys.executable, script, "unknown-command"], capture_output=True, timeout=30, check=False
    )
    assert unknown.returncode == 2


# ---------------------------------------------------------------------------
# Real oracle (R): the production spawn envelope inside a fresh netns
# ---------------------------------------------------------------------------


def _require_real_netns() -> None:
    attestation = process.network_isolation_attestation()
    if not attestation.ok:
        pytest.skip(f"real netns unavailable on this host: {attestation.reason}")


def test_real_netns_forwarder_reaches_a_fake_host_proxy_socket(short_dir: Path) -> None:
    _require_real_netns()
    ipc = short_dir / "ipc"
    scratch = short_dir / "scratch"
    config = short_dir / "config"
    work = scratch / "work"
    for directory in (ipc, scratch, config, work):
        directory.mkdir(parents=True, exist_ok=True)
    assert len(os.fsencode(str(ipc / "egress.sock"))) <= process.AF_UNIX_PATH_MAX_BYTES
    upstream = FakeUpstream(ipc / process.EGRESS_SOCKET_FILE_NAME)
    try:
        client_code = (
            "import json, socket\n"
            "probe = socket.socket(); probe.settimeout(5.0)\n"
            f"probe.connect(('127.0.0.1', {entrypoint.FORWARDER_PORT}))\n"
            "probe.sendall(b'hello-from-netns')\n"
            "data = probe.recv(256)\n"
            "probe.close()\n"
            "print(json.dumps({'reply': data.decode()}))\n"
        )
        profile = process.build_bwrap_argv(
            (process.PROBE_PYTHON, "-c", client_code),
            workspace_binds=(),
            ipc_directory=ipc,
            scratch_directory=scratch,
            config_directory=config,
            work_directory=work,
        )
        plan = process.LaunchPlan(
            argv=tuple(profile),
            env=dict(TEST_ENV),
            cwd=work,
            uses_bwrap=True,
            network_mode=process.NETWORK_MODE_ISOLATED_EGRESS,
        )
        managed = process.spawn(plan)
        completed = False
        try:
            stdout, stderr = managed.process.communicate(timeout=30)
            completed = True
        finally:
            process.stop_network(managed)
            process.close_pipes(managed.process)
            if not completed:
                process.terminate_group(managed.pgid, grace_seconds=1.0)
        assert managed.process.returncode == 0, stderr
        payload = json.loads(stdout.decode("utf-8"))
        assert payload["reply"] == "echo:hello-from-netns"
        assert upstream.payload() == b"hello-from-netns"
        assert upstream.accepted == 1
    finally:
        upstream.close()


def test_entrypoint_file_is_trusted_code() -> None:
    path = process.entrypoint_path()
    assert path.is_file()
    mode = stat.S_IMODE(path.stat().st_mode)
    # Repository checkouts carry the usual 0644/0755 bits; the production
    # install uses 0700.  Either way the file must never be world writable,
    # is trusted stdlib code and is mounted read-only in the netns.
    assert mode & 0o002 == 0
    assert process._trusted_entrypoint() == path
