"""Shared helpers for the M24-05 H2 per-session tool-shim tests.

Every test is offline and rootless. The shim is a stdlib subprocess in the
same closed environment the H1 supervisor gives a CLI; it receives its session
secret once from the H1 ``SecretBroker`` over a tmpdir AF_UNIX socket and
signs HTTP requests with the ADR-0011 D7 scheme. The fake backend is a real
``ThreadingHTTPServer`` on an ephemeral loopback port that records every
request so a test can verify the signature independently. It is reached only
through the shim's test-fixture transport
(``AUTOWORK_SHIM_TEST_BACKEND_URL``), which no production component sets.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import BinaryIO, cast

from sidecars.workspace_agent.sessions.supervisor import SecretBroker

from tests.workspace_agent.conftest import REPOSITORY_ROOT

SESSION_ID = "5d3f0a6e-7c2b-4b8f-9a1d-2e4c6b8a0f13"
SECOND_SESSION_ID = "9a8b7c6d-5e4f-4a3b-8c2d-1e0f2a3b4c5d"
INVESTIGATION_ID = "3f6f2c1a-8a1e-4b7c-9d2f-1a2b3c4d5e6f"
TASK_ID = "a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d"
SECRET = hashlib.sha256(b"m24-h2 synthetic session secret").digest()
SECRET_B64 = base64.b64encode(SECRET).decode("ascii")
SECRET_HEX = SECRET.hex()
SHIM_PATH = REPOSITORY_ROOT / "sidecars" / "workspace_agent" / "sessions" / "shim.py"
FIXED_TIMESTAMP = 1_763_800_000


def reference_signature(
    secret: bytes,
    *,
    method: str,
    path: str,
    session_id: str,
    body: bytes,
    idempotency_key: str,
    timestamp: int,
) -> str:
    """The ADR-0011 D7 scheme, implemented independently of the shim."""
    message = b"\0".join(
        (
            method.encode("ascii"),
            path.encode("utf-8"),
            session_id.encode("utf-8"),
            hashlib.sha256(body).hexdigest().encode("ascii"),
            idempotency_key.encode("ascii"),
            str(timestamp).encode("ascii"),
        )
    )
    return "v1=" + hmac.new(secret, message, hashlib.sha256).hexdigest()


def canonical_json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def expected_client_key(session_id: str, broker_name: str, arguments: Mapping[str, object]) -> str:
    material = b"\0".join(
        (
            b"mcp.v1",
            session_id.encode("utf-8"),
            broker_name.encode("utf-8"),
            canonical_json(arguments),
        )
    )
    return hashlib.sha256(material).hexdigest()


@dataclass(frozen=True)
class RecordedRequest:
    method: str
    path: str
    headers: dict[str, str]
    body: bytes

    def header(self, name: str) -> str:
        return self.headers[name.lower()]

    def json_body(self) -> dict[str, object]:
        value = json.loads(self.body.decode("utf-8"))
        assert isinstance(value, dict)
        return cast(dict[str, object], value)


@dataclass(frozen=True)
class BackendReply:
    status: int = 200
    body: bytes = b'{"status":"ok"}'
    content_type: str = "application/json"


class _BackendServer(ThreadingHTTPServer):
    backend: FakeBackend


class FakeBackend:
    """A real loopback HTTP backend recording every request it receives."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._requests: list[RecordedRequest] = []
        self._replies: deque[BackendReply] = deque()
        self._server = _BackendServer(("127.0.0.1", 0), _FakeBackendHandler)
        self._server.daemon_threads = True
        self._server.backend = self
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="fake-backend", daemon=True
        )
        self._thread.start()

    @property
    def url(self) -> str:
        host, port = cast(tuple[str, int], self._server.server_address[:2])
        return f"http://{host}:{port}"

    def __enter__(self) -> FakeBackend:
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()

    @property
    def requests(self) -> list[RecordedRequest]:
        with self._lock:
            return list(self._requests)

    def enqueue(self, reply: BackendReply) -> None:
        with self._lock:
            self._replies.append(reply)

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=10)

    def _record(self, request: RecordedRequest) -> BackendReply:
        with self._lock:
            self._requests.append(request)
            reply = self._replies.popleft() if self._replies else BackendReply()
        return reply


class _FakeBackendHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: object) -> None:
        return

    def do_POST(self) -> None:
        self._reply()

    def do_GET(self) -> None:
        self._reply()

    def _reply(self) -> None:
        backend = cast(_BackendServer, self.server).backend
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length) if length else b""
        request = RecordedRequest(
            method=self.command,
            path=self.path,
            headers={key.lower(): value for key, value in self.headers.items()},
            body=body,
        )
        reply = backend._record(request)
        self.send_response(reply.status)
        self.send_header("Content-Type", reply.content_type)
        self.send_header("Content-Length", str(len(reply.body)))
        self.end_headers()
        self.wfile.write(reply.body)


@dataclass
class ShimProcess:
    """A live shim subprocess plus its one-shot H1 secret broker."""

    process: subprocess.Popen[bytes]
    broker: SecretBroker | None
    base: Path
    environment: dict[str, str]
    _stdout: queue.Queue[bytes | None] = field(default_factory=queue.Queue)
    _stderr: queue.Queue[bytes | None] = field(default_factory=queue.Queue)
    _next_id: int = 1
    _stdout_lines: list[bytes] = field(default_factory=list)
    _stderr_lines: list[bytes] = field(default_factory=list)

    def initialize(self) -> dict[str, object]:
        response = self.request(
            "initialize",
            {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test"}},
        )
        assert "result" in response, response
        self.notify("notifications/initialized")
        return response

    def notify(self, method: str) -> None:
        self._send({"jsonrpc": "2.0", "method": method})

    def request(self, method: str, params: Mapping[str, object] | None = None) -> dict[str, object]:
        request_id = self._next_id
        self._next_id += 1
        message: dict[str, object] = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            message["params"] = dict(params)
        self._send(message)
        line = self._stdout.get(timeout=20.0)
        assert line is not None, f"shim exited: {self.stderr_text()}"
        self._stdout_lines.append(line)
        response = json.loads(line.decode("utf-8"))
        assert isinstance(response, dict), response
        assert response.get("id") == request_id, response
        return cast(dict[str, object], response)

    def call_tool(self, name: str, arguments: Mapping[str, object]) -> dict[str, object]:
        return self.request("tools/call", {"name": name, "arguments": dict(arguments)})

    def _send(self, message: Mapping[str, object]) -> None:
        stdin = self.process.stdin
        assert stdin is not None
        stdin.write(canonical_json(message) + b"\n")
        stdin.flush()

    def close(self) -> int:
        stdin = self.process.stdin
        if stdin is not None:
            try:
                stdin.close()
            except OSError:
                pass
        try:
            return self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.kill()
            return self.process.wait(timeout=10)

    def stderr_text(self) -> str:
        return b"".join(self._drain(self._stderr)).decode("utf-8", errors="replace")

    def stdout_text(self) -> str:
        return b"".join([*self._stdout_lines, *self._drain(self._stdout)]).decode(
            "utf-8", errors="replace"
        )

    def _drain(self, channel: queue.Queue[bytes | None]) -> list[bytes]:
        lines: list[bytes] = []
        while True:
            try:
                line = channel.get_nowait()
            except queue.Empty:
                return lines
            if line is None:
                return lines
            lines.append(line)


def shim_environment(
    *,
    base: Path,
    socket_path: Path,
    backend_url: str,
    session_id: str = SESSION_ID,
) -> dict[str, str]:
    """The H1 supervisor's closed environment, with no secret anywhere."""
    home = base / "home"
    tmp = base / "tmp"
    home.mkdir(parents=True, exist_ok=True)
    tmp.mkdir(parents=True, exist_ok=True)
    return {
        "PATH": "/usr/bin:/bin",
        "HOME": str(home),
        "TMPDIR": str(tmp),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "AUTOWORK_SESSION_ID": session_id,
        "AUTOWORK_SESSION_SOCKET": str(socket_path),
        "AUTOWORK_SHIM_TEST_BACKEND_URL": backend_url,
    }


def _pump(channel: BinaryIO, target: queue.Queue[bytes | None]) -> None:
    try:
        for line in channel:
            target.put(line)
    finally:
        target.put(None)


class ShimHarness:
    """Context manager owning a tmpdir, the secret broker and the shim process."""

    def __init__(
        self,
        *,
        backend_url: str = "http://127.0.0.1:1",
        secret: bytes = SECRET,
        session_id: str = SESSION_ID,
        environment: dict[str, str] | None = None,
        start_broker: bool = True,
    ) -> None:
        self.base = Path(tempfile.mkdtemp(prefix="m24h2-", dir="/tmp"))
        self.socket_path = self.base / "ipc" / "shim.sock"
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(self.socket_path.parent, 0o700)
        self.secret = secret
        self.session_id = session_id
        self.backend_url = backend_url
        self.environment = environment
        self.start_broker = start_broker
        self.broker: SecretBroker | None = None
        self.shim: ShimProcess | None = None

    def __enter__(self) -> ShimProcess:
        if self.start_broker:
            self.broker = SecretBroker(
                self.socket_path,
                self.session_id,
                self.secret,
                log=logging.getLogger("m24-h2-test"),
            )
            self.broker.start()
        env = shim_environment(
            base=self.base,
            socket_path=self.socket_path,
            backend_url=self.backend_url,
            session_id=self.session_id,
        )
        if self.environment is not None:
            env = dict(self.environment)
        process = subprocess.Popen(
            [sys.executable, "-B", str(SHIM_PATH)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(self.base),
            env=env,
            start_new_session=True,
            close_fds=True,
        )
        assert process.stdout is not None
        assert process.stderr is not None
        shim = ShimProcess(process=process, broker=self.broker, base=self.base, environment=env)
        threading.Thread(
            target=_pump, args=(process.stdout, shim._stdout), name="shim-stdout", daemon=True
        ).start()
        threading.Thread(
            target=_pump, args=(process.stderr, shim._stderr), name="shim-stderr", daemon=True
        ).start()
        self.shim = shim
        return shim

    def __exit__(self, *exc: object) -> None:
        if self.shim is not None:
            self.shim.close()
        if self.broker is not None:
            self.broker.stop()
        shutil.rmtree(self.base, ignore_errors=True)


def run_shim_once(
    environment: Mapping[str, str], *, timeout: float = 10.0
) -> subprocess.CompletedProcess[bytes]:
    """Run the shim with a closed environment and no stdin; return its exit."""
    return subprocess.run(
        [sys.executable, "-B", str(SHIM_PATH)],
        input=b"",
        capture_output=True,
        env=dict(environment),
        timeout=timeout,
        check=False,
        start_new_session=True,
    )


def proc_identity(pid: int) -> tuple[str, str | None]:
    """Return argv and the environment when the process still exposes it.

    The shim sets ``PR_SET_DUMPABLE=0`` after receiving the secret, which
    seals ``/proc/<pid>/environ`` against same-uid readers; ``None`` is the
    hardened, expected outcome on Linux.
    """
    cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
    try:
        environ = Path(f"/proc/{pid}/environ").read_bytes()
    except PermissionError:
        return cmdline.decode("utf-8", errors="replace"), None
    return cmdline.decode("utf-8", errors="replace"), environ.decode("utf-8", errors="replace")


def wait_until(predicate: Callable[[], object], *, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("condition not met")


def free_uds_path(base: Path, name: str = "shim.sock") -> Path:
    path = base / "ipc" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    return path


__all__ = [
    "FIXED_TIMESTAMP",
    "INVESTIGATION_ID",
    "SECOND_SESSION_ID",
    "SECRET",
    "SECRET_B64",
    "SECRET_HEX",
    "SESSION_ID",
    "SHIM_PATH",
    "TASK_ID",
    "BackendReply",
    "FakeBackend",
    "RecordedRequest",
    "ShimHarness",
    "ShimProcess",
    "canonical_json",
    "expected_client_key",
    "free_uds_path",
    "proc_identity",
    "reference_signature",
    "run_shim_once",
    "shim_environment",
    "wait_until",
]
