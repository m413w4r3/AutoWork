"""Shared deterministic helpers for the W3 scan/index/events tests.

Every test runs offline and rootless against a real tmpdir workspace; time is
injected through ``FakeClock`` and the HTTP tests drive a real
``ThreadingHTTPServer`` on an AF_UNIX socket inside the pytest tmpdir.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from sidecars.workspace_agent.agent import SOCKET_FILE_NAME, AgentConfig, WorkspaceAgent
from sidecars.workspace_agent.scan import ScanResult, WorkspaceScanService
from sidecars.workspace_agent.workspace import (
    EnsureWorkspaceRequest,
    WorkspaceDescriptor,
    WorkspaceManager,
)

DELIVERY_CODE = "IR2609"
ARTICLE_SLUG = "bluemoon-cn"
SUBJECT_ID = "3f6f2c1a-8a1e-4b7c-9d2f-1a2b3c4d5e6f"
INVESTIGATION_ID = "9b8a7c6d-5e4f-4a3b-8c2d-1e0f2a3b4c5d"
SYNTHESIS_SHA256 = "c" * 64
AUTH_IDEMPOTENCY_KEY = "w3-ensure-0001"
SCAN_IDEMPOTENCY_KEY = "w3-scan-0001"


class FakeClock:
    """Deterministic ``monotonic``/wall clock; ``advance`` moves both together."""

    def __init__(self, *, monotonic: float = 0.0, wall: datetime | None = None) -> None:
        self._monotonic = monotonic
        self._wall = datetime(2026, 1, 1, tzinfo=UTC) if wall is None else wall

    def advance(self, seconds: float) -> None:
        self._monotonic += seconds

    def monotonic(self) -> float:
        return self._monotonic

    def now(self) -> datetime:
        return self._wall + timedelta(seconds=self._monotonic)

    def wall_ns(self) -> int:
        return int(self.now().timestamp() * 1_000_000_000)


@dataclass(frozen=True)
class ScanEnvironment:
    """A real ensured workspace plus the injected clock and scan manager."""

    manager: WorkspaceManager
    descriptor: WorkspaceDescriptor
    clock: FakeClock
    root: Path
    host_path: Path

    @property
    def workspace_id(self) -> str:
        workspace_id = self.descriptor.workspace_id
        assert isinstance(workspace_id, str)
        return workspace_id

    def service(
        self,
        *,
        stability_window_ms: int = 400,
        retained_event_limit: int = 10_000,
        poll_interval_ms: int = 1000,
    ) -> WorkspaceScanService:
        return WorkspaceScanService(
            self.manager,
            poll_interval_ms=poll_interval_ms,
            stability_window_ms=stability_window_ms,
            clock=self.clock,
            retained_event_limit=retained_event_limit,
        )

    def stable_scan(
        self,
        service: WorkspaceScanService,
        *,
        advance_seconds: float = 1.0,
        full: bool = False,
    ) -> ScanResult:
        """Move past the stability window, then run one scan."""
        self.clock.advance(advance_seconds)
        return service.scan(self.workspace_id, full=full)

    def write(self, relative_path: str, content: bytes, *, mode: int = 0o640) -> Path:
        return write_workspace_file(self.host_path, relative_path, content, mode=mode)


def make_scan_environment(tmp_path: Path) -> ScanEnvironment:
    root = tmp_path / "workspaces"
    root.mkdir()
    manager = WorkspaceManager(root)
    request = EnsureWorkspaceRequest(
        delivery_code=DELIVERY_CODE,
        article_slug=ARTICLE_SLUG,
        subject_id=SUBJECT_ID,
        investigation_id=INVESTIGATION_ID,
        synthesis_sha256=SYNTHESIS_SHA256,
    )
    descriptor = manager.ensure(request)
    return ScanEnvironment(
        manager=manager,
        descriptor=descriptor,
        clock=FakeClock(),
        root=root,
        host_path=Path(descriptor.host_path),
    )


def write_workspace_file(
    base: Path, relative_path: str, content: bytes, *, mode: int = 0o640
) -> Path:
    path = base / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    os.chmod(path, mode)
    return path


@dataclass(frozen=True)
class RunningScanAgent:
    """A live in-process agent with a zero stability window (routes tests)."""

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

    @property
    def host_path(self) -> Path:
        return self.root / DELIVERY_CODE / ARTICLE_SLUG

    def write(self, relative_path: str, content: bytes, *, mode: int = 0o640) -> Path:
        return write_workspace_file(self.host_path, relative_path, content, mode=mode)


@contextmanager
def running_scan_agent(tmp_path: Path, runtime_dir: Path) -> Iterator[RunningScanAgent]:
    """Start a scanning agent whose socket lives in the caller's short runtime dir.

    ``tmp_path`` may be arbitrarily long (pytest basetemp, caller TMPDIR); only
    the AF_UNIX runtime directory has to stay short, so the caller provides it.
    """
    root = tmp_path / "workspaces"
    root.mkdir()
    agent = WorkspaceAgent(
        AgentConfig(
            root=root,
            socket_dir=runtime_dir,
            group=os.getgid(),
            poll_interval_ms=1000,
            stability_window_ms=0,
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
        yield RunningScanAgent(agent=agent, root=root, runtime_dir=runtime_dir)
    finally:
        stop.set()
        thread.join(timeout=15)
        assert not thread.is_alive()


def ensure_workspace_via_http(client: httpx.Client, running: RunningScanAgent) -> str:
    response = client.post(
        "/v1/workspaces/ensure",
        json={
            "delivery_code": DELIVERY_CODE,
            "article_slug": ARTICLE_SLUG,
            "subject_id": SUBJECT_ID,
            "investigation_id": INVESTIGATION_ID,
            "synthesis_sha256": SYNTHESIS_SHA256,
        },
        headers={**running.auth_headers, "Idempotency-Key": AUTH_IDEMPOTENCY_KEY},
    )
    assert response.status_code == 201, response.text
    payload = response.json()
    assert isinstance(payload, dict)
    workspace_id = payload["workspace_id"]
    assert isinstance(workspace_id, str)
    return workspace_id


def scan_via_http(
    client: httpx.Client, running: RunningScanAgent, workspace_id: str, *, full: bool = False
) -> dict[str, object]:
    response = client.post(
        f"/v1/workspaces/{workspace_id}/scan",
        json={"full": full},
        headers={**running.auth_headers, "Idempotency-Key": SCAN_IDEMPOTENCY_KEY},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert isinstance(payload, dict)
    return payload


def json_object(response: httpx.Response) -> dict[str, object]:
    payload = response.json()
    assert isinstance(payload, dict), response.text
    return payload
