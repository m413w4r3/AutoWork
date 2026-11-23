"""M24.1 LIFE-1 oracles: relay/egress/secret wiring and revocation across runs.

Every test is offline and rootless.  A real ``SessionSupervisor`` owns the
pathname ``ipc/broker.sock`` (broker relay) and ``ipc/egress.sock`` (provider
CONNECT proxy); a deterministic fake profile replaces the CLI so no model,
provider, DNS or network call is ever made.  The relay forwards to a loopback
fake backend in this process, and the proxy resolves/dials through injected
fakes that record every call.

Covered design rows (BROKER-RELAY-EGRESS.md §6):
NET-04/NET-09/EG-10 revocation => both sockets unlinked and CONNECTs refused;
restart => orphan sockets unlinked and active sessions lost; run end => sockets
torn down; next run => fresh listeners plus a re-armed one-shot secret; proxy
or attestation failure => ``confinement_unavailable`` with no spawn.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import shutil
import socket
import sys
import threading
from collections.abc import Callable, Iterator, Sequence
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TypedDict, cast

import pytest
from sidecars.workspace_agent.sessions import egress, process
from sidecars.workspace_agent.sessions import relay as relay_module
from sidecars.workspace_agent.sessions import supervisor as sessions_module
from sidecars.workspace_agent.sessions.state import ConfinementUnavailableError
from sidecars.workspace_agent.sessions.supervisor import (
    CapabilityProbe,
    InstructionRequest,
    LaunchContext,
    PrepareSessionRequest,
    ProbeFlags,
    ProfileRegistry,
    ReasonRequest,
    StartSessionRequest,
    ToolManifestRequest,
)

from tests.workspace_agent.sessions_support import (
    INVESTIGATION_ID,
    SECRET,
    SESSION_ID,
    TASK_ID,
    prepare_payload,
    wait_until,
)
from tests.workspace_agent.w3_support import ScanEnvironment, make_scan_environment

SESSIONS_PACKAGE = Path(__file__).resolve().parents[3] / "sidecars" / "workspace_agent" / "sessions"
MANIFEST_KEY = "m24-life-manifest-0001"
START_KEY = "m24-life-start-0001"
INTERRUPT_KEY = "m24-life-interrupt-0001"
CLOSE_KEY = "m24-life-close-0001"
HEARTBEAT_TOOL = {
    "mcp_name": "autowork_task_heartbeat",
    "broker_name": "task.heartbeat",
    "schema_version": "1",
    "input_schema": {"type": "object"},
    "effect": "read",
    "cost": "cheap",
    "capability": None,
}
HEARTBEAT_ARGUMENTS = {
    "investigation_id": INVESTIGATION_ID,
    "task_id": TASK_ID,
    "expected_status_version": 1,
    "lease_fence": 7,
    "nonce": "life-1-e2e-nonce",
}
TASK_COMMAND_PATH = f"/api/investigations/{INVESTIGATION_ID}/tasks/{TASK_ID}/commands"


@pytest.fixture(autouse=True)
def _fake_bwrap(monkeypatch: pytest.MonkeyPatch) -> None:
    """The host probe is a deployment fact; the tests inject it explicitly."""
    monkeypatch.setattr(process, "bwrap_available", lambda: True)


def _probe(
    adapter_kind: str, *, sandbox: bool, now: str = "2026-11-23T00:00:00Z"
) -> CapabilityProbe:
    digest = hashlib.sha256(f"{adapter_kind}-life".encode()).hexdigest()
    flags = ProbeFlags(
        non_interactive=True,
        json_events=True,
        structured_output=True,
        resume=True,
        streaming_stdin=True,
        mcp_per_invocation=True,
        os_sandbox=sandbox,
    )
    confinement: dict[str, object] = {
        "profile": "life-sandbox" if sandbox else "life-direct",
        "bwrap_ok": sandbox,
        "netns": sandbox,
    }
    if sandbox:
        confinement.update(
            {
                "network_mode": process.NETWORK_MODE_ISOLATED_EGRESS,
                "loopback_isolated": True,
                "network_reason": "injected_by_life_1_test",
            }
        )
    payload = json.dumps(confinement, sort_keys=True).encode()
    return CapabilityProbe(
        adapter_kind=adapter_kind,
        version="life-1",
        binary_sha256=digest,
        help_sha256=digest,
        flags=flags,
        confinement=confinement,
        probed_at=now,
        probe_sha256=hashlib.sha256(payload).hexdigest(),
    )


class _DirectProfile:
    """A closed test profile: direct python script, never bwrap, no provider."""

    adapter_kind = "fake"
    requires_sandbox = False
    requires_manifest = False

    def __init__(self, script: Path) -> None:
        self._script = script

    def probe(self, *, now: str) -> CapabilityProbe:
        return _probe(self.adapter_kind, sandbox=False, now=now)

    def build_launch_plan(self, context: LaunchContext) -> process.LaunchPlan:
        return process.LaunchPlan(
            argv=(sys.executable, "-B", str(self._script), str(context.work_directory)),
            env={
                "PATH": "/usr/bin:/bin",
                "HOME": str(context.home_directory),
                "TMPDIR": str(context.session_directory / "tmp"),
                "AUTOWORK_SESSION_ID": context.session_id,
                "AUTOWORK_SESSION_SOCKET": str(context.ipc_socket_path),
                "AUTOWORK_BROKER_SOCKET": str(context.ipc_socket_path.parent / "broker.sock"),
            },
            cwd=context.work_directory,
        )


class _SandboxProfile(_DirectProfile):
    """A requires-sandbox profile whose launch never creates a real namespace."""

    adapter_kind = "codex"
    requires_sandbox = True

    def probe(self, *, now: str) -> CapabilityProbe:
        return _probe(self.adapter_kind, sandbox=True, now=now)


class _FakeService:
    """A running supervisor plus the environment it serves."""

    def __init__(
        self,
        supervisor: sessions_module.SessionSupervisor,
        environment: ScanEnvironment,
        config: sessions_module.SessionSupervisorConfig,
    ) -> None:
        self.supervisor = supervisor
        self.environment = environment
        self.config = config

    def prepare(self, *, adapter_kind: str = "fake") -> dict[str, object]:
        body = prepare_payload(
            self.environment.workspace_id,
            adapter_kind=adapter_kind,
            network_mode="isolated_egress",
        )
        return cast(
            dict[str, object],
            self.supervisor.prepare(
                PrepareSessionRequest.from_payload(body),
                idempotency_key="m24-life-prepare-0001",
                body_sha256="a" * 64,
            ),
        )

    def accept_manifest(self) -> None:
        canonical = json.dumps(
            {"tools": [HEARTBEAT_TOOL]}, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        self.supervisor.tool_manifest(
            SESSION_ID,
            ToolManifestRequest.from_payload(
                {
                    "manifest_sha256": hashlib.sha256(canonical).hexdigest(),
                    "tools": [HEARTBEAT_TOOL],
                }
            ),
            idempotency_key=MANIFEST_KEY,
            body_sha256="d" * 64,
        )

    def start(self, *, key: str = START_KEY) -> dict[str, object]:
        return cast(
            dict[str, object],
            self.supervisor.start(
                SESSION_ID,
                StartSessionRequest(),
                idempotency_key=key,
                body_sha256="b" * 64,
            ),
        )

    def interrupt(self, *, key: str = INTERRUPT_KEY) -> dict[str, object]:
        return cast(
            dict[str, object],
            self.supervisor.interrupt(
                SESSION_ID,
                ReasonRequest.from_payload({"reason": "life-1 oracle"}),
                idempotency_key=key,
                body_sha256="f" * 64,
            ),
        )

    def state(self) -> str:
        return str(self.supervisor.status(SESSION_ID)["state"])

    def session_dir(self) -> Path:
        return cast(Path, self.config.session_root) / SESSION_ID

    def socket(self, name: str) -> Path:
        return self.session_dir() / "ipc" / name


@pytest.fixture
def services(session_base: Path) -> Iterator[Callable[..., _FakeService]]:
    environment = make_scan_environment(session_base)
    supervisors: list[sessions_module.SessionSupervisor] = []

    def build(
        profile: _DirectProfile,
        *,
        broker_backend_url: str = sessions_module.DEFAULT_BROKER_BACKEND_URL,
        egress_profile_path: Path | None = None,
        egress_resolver: egress.Resolver | None = None,
        egress_connector: egress.Connector | None = None,
    ) -> _FakeService:
        config = sessions_module.SessionSupervisorConfig(
            runtime_root=session_base / "run",
            workspace_root=environment.root,
            interrupt_grace_seconds=0.2,
            watchdog_poll_seconds=0.05,
            broker_backend_url=broker_backend_url,
            egress_profile_path=egress_profile_path,
            egress_resolver=egress_resolver,
            egress_connector=egress_connector,
            egress_local_addresses=lambda: frozenset(),
        )
        supervisor = sessions_module.SessionSupervisor(
            config,
            workspaces=environment.manager,
            scans=environment.service(),
            profiles=ProfileRegistry((profile,)),
        )
        supervisors.append(supervisor)
        return _FakeService(supervisor, environment, config)

    try:
        yield build
    finally:
        for supervisor in supervisors:
            supervisor.stop()


def _write_script(base: Path, body: str) -> Path:
    path = base / "life-cli.py"
    path.write_text(body, encoding="utf-8")
    return path


def _write_egress_profile(base: Path) -> Path:
    path = base / "egress-profile.json"
    path.write_text(
        json.dumps(
            {
                "schema": egress.EGRESS_PROFILE_SCHEMA,
                "profiles": {"codex": {"hosts": ["api.openai.com"]}},
            }
        ),
        encoding="utf-8",
    )
    os.chmod(path, 0o600)
    return path


def _raw_request(socket_path: Path, data: bytes) -> bytes:
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    connection.settimeout(5.0)
    try:
        connection.connect(str(socket_path))
        connection.sendall(data)
        chunks: list[bytes] = []
        while True:
            chunk = connection.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
    finally:
        connection.close()
    return b"".join(chunks)


def _refused_connect(socket_path: Path, authority: str) -> bytes:
    """Send one CONNECT and read the refusal until the proxy closes."""
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    connection.settimeout(5.0)
    try:
        connection.connect(str(socket_path))
        connection.sendall(f"CONNECT {authority} HTTP/1.1\r\n\r\n".encode("ascii"))
        chunks: list[bytes] = []
        while True:
            chunk = connection.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
    finally:
        connection.close()
    return b"".join(chunks)


def _open_tunnel(socket_path: Path, authority: str) -> tuple[socket.socket, bytes]:
    """Send one CONNECT, return the live socket and the established head."""
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    connection.settimeout(5.0)
    connection.connect(str(socket_path))
    connection.sendall(f"CONNECT {authority} HTTP/1.1\r\n\r\n".encode("ascii"))
    head = b""
    while b"\r\n\r\n" not in head:
        chunk = connection.recv(65536)
        if not chunk:
            break
        head += chunk
    return connection, head


class _FakeConnector:
    """One pinned public address dialled into an in-memory socketpair."""

    def __init__(self) -> None:
        self.dialed: list[egress.ResolvedAddress] = []
        self.peer: socket.socket | None = None

    def dial(self, address: egress.ResolvedAddress, *, timeout: float) -> egress.TunnelStream:
        self.dialed.append(address)
        client_end, peer = socket.socketpair()
        self.peer = peer
        return client_end


class _RecordedRequest(TypedDict):
    """One relay request as recorded by the fake backend."""

    path: str
    headers: dict[str, str]
    body: bytes


class _FakeBackend:
    """A loopback HTTP backend that records the relay's forwarded request."""

    def __init__(self) -> None:
        self.requests: list[_RecordedRequest] = []
        backend = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length)
                backend.requests.append(
                    {
                        "path": self.path,
                        "headers": {key: value for key, value in self.headers.items()},
                        "body": body,
                    }
                )
                payload = json.dumps({"ok": True, "source": "fake-backend"}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args: object) -> None:
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def url(self) -> str:
        host, port = cast(tuple[str, int], self._server.server_address[:2])
        return f"http://{host}:{port}"

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


E2E_CLI = """
import json
import subprocess
import sys
import time
from pathlib import Path

shim, output, go = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])
deadline = time.monotonic() + 20.0
while not go.exists() and time.monotonic() < deadline:
    time.sleep(0.02)
if not go.exists():
    output.write_text(json.dumps({"error": "go_file_timeout"}))
    raise SystemExit(3)

proc = subprocess.Popen(
    [sys.executable, "-B", str(shim)],
    stdin=subprocess.PIPE,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
)
def send(frame):
    proc.stdin.write((json.dumps(frame) + "\\n").encode())
    proc.stdin.flush()

send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
      "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                 "clientInfo": {"name": "life-e2e", "version": "1"}}})
initialize = proc.stdout.readline()
arguments = json.loads(sys.argv[4])
send({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
      "params": {"name": "autowork_task_heartbeat", "arguments": arguments}})
call = proc.stdout.readline()
proc.stdin.close()
try:
    proc.wait(timeout=20)
except subprocess.TimeoutExpired:
    proc.kill()
    proc.wait()
output.write_text(json.dumps({
    "initialize": json.loads(initialize or b"{}"),
    "call": json.loads(call or b"{}"),
    "returncode": proc.returncode,
    "stderr": proc.stderr.read().decode("utf-8", "replace"),
}))
"""


def _e2e_profile(base: Path) -> _DirectProfile:
    """A profile whose fake CLI spawns the real shim over the relay socket."""
    source_cli = _write_script(base, E2E_CLI)

    class _E2EProfile(_DirectProfile):
        requires_manifest = True

        def build_launch_plan(self, context: LaunchContext) -> process.LaunchPlan:
            shim_dir = context.config_directory
            shutil.copyfile(SESSIONS_PACKAGE / "shim.py", shim_dir / "shim.py")
            shutil.copyfile(SESSIONS_PACKAGE / "catalog.py", shim_dir / "catalog.py")
            output = context.work_directory / "mcp-response.json"
            go_file = context.work_directory / "go"
            plan = super().build_launch_plan(context)
            return process.LaunchPlan(
                argv=(
                    sys.executable,
                    "-B",
                    str(source_cli),
                    str(shim_dir / "shim.py"),
                    str(output),
                    str(go_file),
                    json.dumps(HEARTBEAT_ARGUMENTS),
                ),
                env=plan.env,
                cwd=plan.cwd,
            )

    return _E2EProfile(source_cli)


def test_relay_refuses_while_ready_and_e2e_call_reaches_the_backend(
    session_base: Path, services
) -> None:
    backend = _FakeBackend()
    try:
        service = services(
            _e2e_profile(session_base),
            broker_backend_url=backend.url,
        )
        service.prepare()
        service.accept_manifest()
        broker = service.socket("broker.sock")
        assert broker.is_socket()
        assert service.socket("egress.sock").exists() is False  # fake: no proxy

        # READY is not an active run: the relay refuses before reading.
        refused = _raw_request(
            broker, b"POST /api/investigations/x HTTP/1.1\r\nContent-Length: 0\r\n\r\n"
        )
        assert b"409" in refused
        assert b"relay_session_inactive" in refused
        assert backend.requests == []

        started = service.start()
        assert started["state"] == "running"
        wait_until(lambda: service.state() == "running", timeout=10)
        (service.session_dir() / "scratch" / "work" / "go").write_text("go", encoding="utf-8")
        wait_until(
            lambda: service.state() in {"awaiting_instruction", "stopped", "failed"}, timeout=20
        )
        assert service.state() == "awaiting_instruction"

        response = json.loads(
            (service.session_dir() / "scratch" / "work" / "mcp-response.json").read_text(
                encoding="utf-8"
            )
        )
        assert response["stderr"] == "", response["stderr"]
        assert response["returncode"] == 0
        assert response["initialize"]["result"]["serverInfo"]["name"] == "autowork-session-shim"
        result = response["call"]["result"]
        assert result["isError"] is False
        envelope = json.loads(result["content"][0]["text"])
        assert envelope["status"] == 200, envelope

        assert len(backend.requests) == 1
        forwarded = backend.requests[0]
        assert forwarded["path"] == TASK_COMMAND_PATH
        headers = {
            str(key).lower(): str(value) for key, value in dict(forwarded["headers"]).items()
        }
        assert headers["x-autowork-agent-session"] == SESSION_ID
        body = bytes(forwarded["body"])
        assert json.loads(body)["command"] == "heartbeat"
        # The relay forwards byte-exact: the backend recomputes the D7 HMAC.
        message = b"\0".join(
            (
                b"POST",
                TASK_COMMAND_PATH.encode(),
                SESSION_ID.encode(),
                hashlib.sha256(body).hexdigest().encode(),
                headers["idempotency-key"].encode(),
                headers["x-autowork-agent-timestamp"].encode(),
            )
        )
        expected = "v1=" + hmac.new(SECRET, message, hashlib.sha256).hexdigest()
        assert headers["x-autowork-agent-signature"] == expected

        # Run end unlinks the relay socket; no request can be served after it.
        assert not broker.exists()
    finally:
        backend.close()


def test_revocation_unlinks_both_sockets(session_base: Path, services) -> None:
    connector = _FakeConnector()
    resolver_calls: list[tuple[str, int]] = []

    def resolver(host: str, port: int) -> Sequence[egress.ResolvedAddress]:
        resolver_calls.append((host, port))
        return [egress.ResolvedAddress(socket.AF_INET, "93.184.216.34", ("93.184.216.34", 443))]

    service = services(
        _SandboxProfile(
            _write_script(
                session_base,
                "import time\nwhile True:\n    time.sleep(1)\n",
            )
        ),
        egress_profile_path=_write_egress_profile(session_base),
        egress_resolver=resolver,
        egress_connector=connector,
    )
    service.prepare(adapter_kind="codex")
    egress_socket = service.socket("egress.sock")
    broker_socket = service.socket("broker.sock")
    assert egress_socket.is_socket()
    assert broker_socket.is_socket()

    # READY: an allowlisted CONNECT is refused before any resolution/dial.
    denied = _refused_connect(egress_socket, "api.openai.com:443")
    assert b"502" in denied
    assert denied.endswith(b"session_inactive\n")
    assert resolver_calls == []
    assert connector.dialed == []

    service.start()
    wait_until(lambda: service.state() == "running", timeout=10)

    # RUNNING: the same CONNECT tunnels through the injected resolver/connector.
    tunnel, established = _open_tunnel(egress_socket, "api.openai.com:443")
    try:
        assert b"200 Connection Established" in established
        assert resolver_calls == [("api.openai.com", 443)]
        assert len(connector.dialed) == 1
        assert connector.peer is not None
        tunnel.sendall(b"ping")
        assert connector.peer.recv(4) == b"ping"
        connector.peer.sendall(b"pong")
        assert tunnel.recv(4) == b"pong"

        # Interrupt aborts the open provider tunnel and unlinks both sockets.
        interrupted = service.interrupt()
        assert interrupted["signal_sent"] is True
        wait_until(lambda: service.state() in {"stopped", "failed"}, timeout=15)
        assert service.state() == "stopped"
        assert not egress_socket.exists()
        assert not broker_socket.exists()
        connector.peer.settimeout(5.0)
        assert connector.peer.recv(4) == b""
        # The revoked proxy has no socket left to accept a new CONNECT.
        with pytest.raises(OSError):
            _refused_connect(egress_socket, "api.openai.com:443")
    finally:
        tunnel.close()

    # A new run re-creates both listeners (fresh objects, same paths).
    service.start(key="m24-life-start-0002")
    wait_until(lambda: service.state() == "running", timeout=10)
    assert egress_socket.is_socket()
    assert broker_socket.is_socket()
    service.interrupt(key="m24-life-interrupt-0002")
    wait_until(lambda: service.state() in {"stopped", "failed"}, timeout=15)
    assert not egress_socket.exists()
    assert not broker_socket.exists()

    closed = service.supervisor.close(
        SESSION_ID,
        ReasonRequest.from_payload({"reason": "done"}),
        idempotency_key=CLOSE_KEY,
        body_sha256="c" * 64,
    )
    assert closed == {"state": "closed"}
    assert not egress_socket.exists()
    assert not broker_socket.exists()


GATED_CLI = """
import sys
import time
from pathlib import Path

work = Path(sys.argv[1])
deadline = time.monotonic() + 30.0
while not (work / "go").exists() and time.monotonic() < deadline:
    time.sleep(0.02)
"""


def _gated_script(base: Path) -> Path:
    """A run that lasts until the test writes ``<work>/go``."""
    return _write_script(base, GATED_CLI)


def test_run_end_unlinks_sockets_and_next_run_recreates_them(session_base: Path, services) -> None:
    service = services(
        _SandboxProfile(_gated_script(session_base)),
        egress_profile_path=_write_egress_profile(session_base),
        egress_resolver=lambda host, port: [],
    )
    work = service.session_dir() / "scratch" / "work"
    service.prepare(adapter_kind="codex")
    service.start()
    wait_until(lambda: service.state() == "running", timeout=10)
    assert service.socket("egress.sock").is_socket()
    assert service.socket("broker.sock").is_socket()
    (work / "go").write_text("go", encoding="utf-8")
    wait_until(lambda: service.state() == "awaiting_instruction", timeout=15)
    assert not service.socket("egress.sock").exists()
    assert not service.socket("broker.sock").exists()

    # D4: the next run is started by the instruction primitive at the idle
    # receive point; the complete launch path re-creates both listeners and
    # re-arms the one-shot secret before the new process exists.
    (work / "go").unlink()
    text = "continue the synthetic relay run"
    service.supervisor.instruction(
        SESSION_ID,
        InstructionRequest.from_payload(
            {
                "instruction_id": "b3c4d5e6-f708-4a1b-9c2d-3e4f5a6b7c8d",
                "text": text,
                "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            }
        ),
        idempotency_key="m24-life-instruction-0001",
        body_sha256="e" * 64,
    )
    wait_until(lambda: service.state() == "running", timeout=10)
    assert service.socket("egress.sock").is_socket()
    assert service.socket("broker.sock").is_socket()
    (work / "go").write_text("go", encoding="utf-8")
    wait_until(lambda: service.state() == "awaiting_instruction", timeout=15)
    assert len(service.supervisor.status(SESSION_ID)["runs"]) == 2


def test_missing_egress_profile_refuses_the_real_session_before_layout(
    session_base: Path, services
) -> None:
    service = services(_SandboxProfile(_write_script(session_base, "pass\n")))
    with pytest.raises(ConfinementUnavailableError) as raised:
        service.prepare(adapter_kind="codex")
    assert raised.value.details["reason"] == "egress_profile_unavailable"
    assert not service.session_dir().exists()


def test_missing_egress_socket_at_start_is_confinement_unavailable_without_spawn(
    session_base: Path, services
) -> None:
    service = services(
        _SandboxProfile(_write_script(session_base, "import time\ntime.sleep(30)\n")),
        egress_profile_path=_write_egress_profile(session_base),
    )
    service.prepare(adapter_kind="codex")
    egress_socket = service.socket("egress.sock")
    assert egress_socket.is_socket()
    egress_socket.unlink()

    with pytest.raises(ConfinementUnavailableError):
        service.start()
    assert service.state() == "ready"
    assert service.supervisor.status(SESSION_ID)["runs"] == []
    assert not service.socket("broker.sock").exists()  # torn down, no run


def test_attestation_failure_is_confinement_unavailable_without_spawn(
    session_base: Path, services, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _BwrapProfile(_SandboxProfile):
        def build_launch_plan(self, context: LaunchContext) -> process.LaunchPlan:
            plan = super().build_launch_plan(context)
            return process.LaunchPlan(
                argv=("/usr/bin/bwrap", *plan.argv),
                env=plan.env,
                cwd=plan.cwd,
                uses_bwrap=True,
                network_mode=plan.network_mode,
            )

    service = services(
        _BwrapProfile(_write_script(session_base, "import time\ntime.sleep(30)\n")),
        egress_profile_path=_write_egress_profile(session_base),
    )
    service.prepare(adapter_kind="codex")
    monkeypatch.setattr(
        process,
        "network_isolation_attestation",
        lambda **kwargs: process.NetworkIsolationAttestation(
            False, "injected attestation refusal", "isolated_egress", False
        ),
    )
    with pytest.raises(ConfinementUnavailableError) as raised:
        service.start()
    assert "attestation" in str(raised.value.details["reason"])
    assert service.state() == "ready"
    assert service.supervisor.status(SESSION_ID)["runs"] == []


def test_relay_bind_failure_refuses_prepare_with_confinement_unavailable(
    session_base: Path, services, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _FailingRelay:
        def __init__(self, **kwargs: object) -> None:
            raise relay_module.RelayConfigError("relay_socket_unavailable")

    monkeypatch.setattr(relay_module, "BrokerRelay", _FailingRelay)
    service = services(_DirectProfile(_write_script(session_base, "pass\n")))
    with pytest.raises(ConfinementUnavailableError) as raised:
        service.prepare()
    assert raised.value.details["reason"] == "relay_socket_unavailable"
    assert not service.session_dir().exists()


def test_restart_unlinks_orphan_sockets_and_never_adopts(
    session_base: Path,
) -> None:
    environment = make_scan_environment(session_base)
    config = sessions_module.SessionSupervisorConfig(
        runtime_root=session_base / "run", workspace_root=environment.root
    )
    first = sessions_module.SessionSupervisor(
        config,
        workspaces=environment.manager,
        scans=environment.service(),
        profiles=ProfileRegistry((_DirectProfile(_write_script(session_base, "pass\n")),)),
    )
    try:
        first.prepare(
            PrepareSessionRequest.from_payload(
                prepare_payload(environment.workspace_id, adapter_kind="fake")
            ),
            idempotency_key="m24-life-prepare-0002",
            body_sha256="a" * 64,
        )
    finally:
        first.stop()

    ipc = config.session_root / SESSION_ID / "ipc"
    orphans: list[Path] = []
    for name in ("shim.sock", "broker.sock", "egress.sock"):
        path = ipc / name
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            listener.bind(str(path))
        finally:
            listener.close()
        orphans.append(path)
    assert all(path.is_socket() for path in orphans)

    second = sessions_module.SessionSupervisor(
        config,
        workspaces=environment.manager,
        scans=environment.service(),
        profiles=ProfileRegistry((_DirectProfile(_write_script(session_base, "pass\n")),)),
    )
    try:
        status = second.status(SESSION_ID)
        assert status["state"] == "lost"
        assert all(not path.exists() for path in orphans)
    finally:
        second.stop()


def test_removed_host_network_mode_fails_supervisor_startup(
    session_base: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    environment = make_scan_environment(session_base)
    monkeypatch.setenv("AGENT_SESSIONS_CLI_NETWORK_MODE", "host_network_unsafe")
    with pytest.raises(sessions_module.SessionConfigError, match="host_network_unsafe"):
        sessions_module._env_network_mode()
    config = sessions_module.SessionSupervisorConfig(
        runtime_root=session_base / "run",
        workspace_root=environment.root,
        cli_network_mode="host_network_unsafe",
    )
    with pytest.raises(sessions_module.SessionConfigError, match="host_network_unsafe"):
        sessions_module.SessionSupervisor(
            config,
            workspaces=environment.manager,
            scans=environment.service(),
            profiles=ProfileRegistry((_DirectProfile(_write_script(session_base, "pass\n")),)),
        )
