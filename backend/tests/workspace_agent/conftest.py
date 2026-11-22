"""Shared fixtures for the host workspace agent tests.

The sidecar is a standalone stdlib service outside the backend package; tests
import it through the repository-root ``sys.path`` insert precedent
(``backend/tests/test_browser_acquisition.py:16-19``).

Every test runs offline and rootless: a real ``ThreadingHTTPServer`` listens on
an AF_UNIX socket inside a pytest tmpdir, or the agent runs as a subprocess.
"""

from __future__ import annotations

import json
import os
import socket
import sys
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from sidecars.workspace_agent.agent import (  # noqa: E402
    SOCKET_FILE_NAME,
    AgentConfig,
    WorkspaceAgent,
)
from sidecars.workspace_agent.auth import TOKEN_FILE_NAME, read_token_file  # noqa: E402

from tests.workspace_agent.w3_support import (  # noqa: E402
    RunningScanAgent,
    ScanEnvironment,
    make_scan_environment,
    running_scan_agent,
)

BASE_URL = "http://workspace-agent"


@dataclass(frozen=True)
class RunningAgent:
    """A live in-process agent bound to a tmpdir runtime directory."""

    agent: WorkspaceAgent
    root: Path
    runtime_dir: Path

    @property
    def socket_path(self) -> Path:
        path: Path = self.runtime_dir / SOCKET_FILE_NAME
        return path

    @property
    def token(self) -> str:
        token: str | None = self.agent.token
        assert token is not None, "agent is not started"
        return token

    @property
    def auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}


@pytest.fixture
def agent_server(tmp_path: Path) -> Iterator[RunningAgent]:
    root = tmp_path / "workspaces"
    root.mkdir()
    runtime_dir = tmp_path / "run"
    agent = WorkspaceAgent(
        AgentConfig(
            root=root,
            socket_dir=runtime_dir,
            group=os.getgid(),
            poll_interval_ms=1000,
            stability_window_ms=400,
            log_level="WARNING",
            allow_foreign_runtime_dir=False,
        )
    )
    stop = threading.Event()
    thread = threading.Thread(target=agent.serve, args=(stop,), name="workspace-agent", daemon=True)
    thread.start()
    try:
        if not agent.wait_ready(15):
            pytest.fail("workspace agent did not become ready")
        assert read_token_file(runtime_dir / TOKEN_FILE_NAME) == agent.token
        yield RunningAgent(agent=agent, root=root, runtime_dir=runtime_dir)
    finally:
        stop.set()
        thread.join(timeout=15)
        assert not thread.is_alive()


@pytest.fixture
def uds_client(agent_server: RunningAgent) -> Iterator[httpx.Client]:
    transport = httpx.HTTPTransport(uds=str(agent_server.socket_path), retries=0)
    with httpx.Client(
        base_url=BASE_URL, transport=transport, timeout=10.0, trust_env=False
    ) as client:
        yield client


def raw_http_request(socket_path: Path, payload: bytes, *, timeout: float = 10.0) -> bytes:
    """Send raw bytes over AF_UNIX and read until the server closes."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(timeout)
        connection.connect(str(socket_path))
        connection.sendall(payload)
        chunks: list[bytes] = []
        while True:
            chunk = connection.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
    return b"".join(chunks)


def response_status(raw: bytes) -> int:
    status_line = raw.split(b"\r\n", 1)[0]
    return int(status_line.split(b" ", 2)[1])


def response_json(raw: bytes) -> dict[str, object]:
    _, separator, body = raw.partition(b"\r\n\r\n")
    assert separator, raw[:200]
    value = json.loads(body)
    assert isinstance(value, dict)
    return value


def wait_for_health(socket_path: Path, *, timeout: float = 20.0) -> None:
    request = b"GET /health HTTP/1.1\r\nHost: workspace-agent\r\n\r\n"
    deadline = time.monotonic() + timeout
    last_error: OSError | None = None
    while time.monotonic() < deadline:
        try:
            raw = raw_http_request(socket_path, request, timeout=2.0)
        except OSError as exc:
            last_error = exc
        else:
            try:
                if response_status(raw) == 200:
                    return
            except (IndexError, ValueError):
                pass
        time.sleep(0.05)
    pytest.fail(f"workspace agent socket never became healthy: {last_error}")


def agent_environment() -> dict[str, str]:
    env = dict(os.environ)
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = str(REPOSITORY_ROOT) + (os.pathsep + existing if existing else "")
    return env


def agent_argv(root: Path, runtime_dir: Path) -> list[str]:
    return [
        sys.executable,
        "-m",
        "sidecars.workspace_agent",
        "--root",
        str(root),
        "--socket-dir",
        str(runtime_dir),
        "--group",
        str(os.getgid()),
        "--log-level",
        "WARNING",
    ]


@pytest.fixture
def scan_environment(tmp_path: Path) -> ScanEnvironment:
    """A real ensured workspace plus an injected deterministic clock."""
    return make_scan_environment(tmp_path)


@pytest.fixture
def scanning_agent(tmp_path: Path) -> Iterator[RunningScanAgent]:
    """A live agent whose zero stability window imports files on the first scan."""
    with running_scan_agent(tmp_path) as running:
        yield running


@pytest.fixture
def scanning_client(scanning_agent: RunningScanAgent) -> Iterator[httpx.Client]:
    transport = httpx.HTTPTransport(uds=str(scanning_agent.socket_path), retries=0)
    with httpx.Client(
        base_url=BASE_URL, transport=transport, timeout=30.0, trust_env=False
    ) as client:
        yield client
