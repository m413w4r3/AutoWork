"""Shared helpers for the M24-05 H1 host session supervisor tests.

Every test is offline and rootless: a real ``WorkspaceAgent`` serves a real
``ThreadingHTTPServer`` on a tmpdir AF_UNIX socket, and the ``fake`` profile
spawns the real :mod:`sidecars.workspace_agent.sessions.fake_cli` process.
The fake CLI records its observations in ``scratch/work/fake-output.json`` so a
test never has to read a redacted log line to learn a PID or a SHA-256.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import threading
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
from sidecars.workspace_agent.agent import SOCKET_FILE_NAME, AgentConfig, WorkspaceAgent

from tests.workspace_agent.conftest import BASE_URL, REPOSITORY_ROOT
from tests.workspace_agent.w3_support import (
    ARTICLE_SLUG,
    DELIVERY_CODE,
    INVESTIGATION_ID,
    SYNTHESIS_SHA256,
    json_object,
)

SESSION_ID = "5d3f0a6e-7c2b-4b8f-9a1d-2e4c6b8a0f13"
SECOND_SESSION_ID = "9a8b7c6d-5e4f-4a3b-8c2d-1e0f2a3b4c5d"
TASK_ID = "a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d"
HANDOFF_ID = "0f1e2d3c-4b5a-4968-8776-655443322110"
SECRET = hashlib.sha256(b"m24-h1 fake session secret").digest()
SECRET_B64 = base64.b64encode(SECRET).decode("ascii")
SECRET_HEX = SECRET.hex()
SECRET_SHA256 = hashlib.sha256(SECRET).hexdigest()
FAKE_CLI_PATH = REPOSITORY_ROOT / "sidecars" / "workspace_agent" / "sessions" / "fake_cli.py"

PREPARE_KEY = "m24-h1-prepare-0001"
START_KEY = "m24-h1-start-0001"
INTERRUPT_KEY = "m24-h1-interrupt-0001"
COLLECT_KEY = "m24-h1-collect-0001"
CLOSE_KEY = "m24-h1-close-0001"


def prepare_payload(
    workspace_id: str,
    *,
    session_id: str = SESSION_ID,
    adapter_kind: str = "fake",
    task_id: str = TASK_ID,
    auth_secret: bytes | None = SECRET,
    skill_names: list[str] | None = None,
    budgets: Mapping[str, int] | None = None,
    **extra: object,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "session_id": session_id,
        "adapter_kind": adapter_kind,
        "workspace_id": workspace_id,
        "investigation_id": INVESTIGATION_ID,
        "task_id": task_id,
        "handoff": {"handoff_id": HANDOFF_ID, "version": 1, "sha256": "b" * 64},
        "lease_fence": 7,
        "allowed_workspace_paths": ["notes/humaines", "results"],
        "skill_names": [] if skill_names is None else skill_names,
        "budgets": (
            {"wall_seconds": 30, "max_output_bytes": 1_048_576, "max_instructions": 8}
            if budgets is None
            else dict(budgets)
        ),
        "model": "fake-model",
    }
    if auth_secret is not None:
        payload["auth_secret"] = base64.b64encode(auth_secret).decode("ascii")
    payload.update(extra)
    return payload


def fake_script(
    steps: list[dict[str, object]],
    *,
    final_message: str = "fake final message",
    outcome: str = "success",
) -> dict[str, object]:
    return {"steps": steps, "final_message": final_message, "outcome": outcome}


def success_script(*, note_text: str = "# agent note\n\nsynthetic\n") -> dict[str, object]:
    return fake_script(
        [
            {"op": "emit", "event": {"type": "started"}},
            {"op": "write", "path": "notes/finding.md", "text": note_text},
            {"op": "write", "path": "result.md", "text": "result body\n"},
            {"op": "write", "path": "final.md", "text": "final body\n"},
            {"op": "exit", "code": 0},
        ]
    )


def hang_script(*, child_seconds: int | None = None) -> dict[str, object]:
    steps: list[dict[str, object]] = [{"op": "emit", "event": {"type": "started"}}]
    if child_seconds is not None:
        steps.extend(
            [
                {"op": "spawn_child", "seconds": child_seconds},
                {"op": "emit", "event": {"type": "waiting"}},
            ]
        )
    steps.append({"op": "hang"})
    return fake_script(steps)


@contextmanager
def running_session_agent(base: Path) -> Iterator[RunningSessionAgent]:
    """Start a real agent; the same ``base`` can be reused after a restart."""
    root = base / "workspaces"
    root.mkdir(parents=True, exist_ok=True)
    runtime_dir = base / "run"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(runtime_dir, 0o750)
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
        yield RunningSessionAgent(agent=agent, root=root, runtime_dir=runtime_dir)
    finally:
        stop.set()
        thread.join(timeout=15)
        assert not thread.is_alive()


class RunningSessionAgent:
    """A live in-process agent plus the UDS client helpers H1 tests need."""

    def __init__(self, *, agent: WorkspaceAgent, root: Path, runtime_dir: Path) -> None:
        self.agent = agent
        self.root = root
        self.runtime_dir = runtime_dir

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

    @property
    def sessions_root(self) -> Path:
        return self.runtime_dir / "sessions"

    def session_dir(self, session_id: str = SESSION_ID) -> Path:
        return self.sessions_root / session_id

    def client(self) -> httpx.Client:
        transport = httpx.HTTPTransport(uds=str(self.socket_path), retries=0)
        return httpx.Client(base_url=BASE_URL, transport=transport, timeout=30.0, trust_env=False)

    def ensure_workspace(self, client: httpx.Client) -> str:
        response = client.post(
            "/v1/workspaces/ensure",
            json=self.ensure_payload(),
            headers={**self.auth_headers, "Idempotency-Key": "m24-h1-ensure-0001"},
        )
        assert response.status_code == 201, response.text
        workspace_id = json_object(response)["workspace_id"]
        assert isinstance(workspace_id, str)
        return workspace_id

    @staticmethod
    def ensure_payload() -> dict[str, object]:
        return {
            "delivery_code": DELIVERY_CODE,
            "article_slug": ARTICLE_SLUG,
            "subject_id": "3f6f2c1a-8a1e-4b7c-9d2f-1a2b3c4d5e6f",
            "investigation_id": INVESTIGATION_ID,
            "synthesis_sha256": SYNTHESIS_SHA256,
        }


def write_fake_script(
    running: RunningSessionAgent,
    script: Mapping[str, object],
    *,
    session_id: str = SESSION_ID,
) -> Path:
    session_dir = running.session_dir(session_id)
    session_dir.mkdir(parents=True, exist_ok=True)
    path = session_dir / "fake-script.json"
    path.write_text(json.dumps(script, separators=(",", ":")), encoding="utf-8")
    os.chmod(path, 0o600)
    return path


def post_session(
    client: httpx.Client,
    running: RunningSessionAgent,
    path: str,
    body: Mapping[str, object],
    key: str,
) -> httpx.Response:
    return client.post(
        path,
        json=dict(body),
        headers={**running.auth_headers, "Idempotency-Key": key},
    )


def get_session(
    client: httpx.Client, running: RunningSessionAgent, path: str, **params: object
) -> httpx.Response:
    return client.get(path, params=params, headers=running.auth_headers)


def wait_until(predicate: Any, *, timeout: float = 15.0, interval: float = 0.02) -> Any:
    deadline = time.monotonic() + timeout
    last: Any = None
    while time.monotonic() < deadline:
        last = predicate()
        if last:
            return last
        time.sleep(interval)
    pytest.fail(f"condition not met within {timeout}s: {last!r}")


def wait_for_state(
    client: httpx.Client,
    running: RunningSessionAgent,
    *states: str,
    session_id: str = SESSION_ID,
    timeout: float = 15.0,
) -> dict[str, object]:
    def _status() -> dict[str, object] | None:
        response = get_session(client, running, f"/v1/sessions/{session_id}")
        assert response.status_code == 200, response.text
        payload = json_object(response)
        return payload if payload["state"] in states else None

    found = wait_until(_status, timeout=timeout)
    assert isinstance(found, dict)
    return found


def session_events(
    client: httpx.Client,
    running: RunningSessionAgent,
    *,
    session_id: str = SESSION_ID,
    after: int = 0,
    generation: int | None = None,
    limit: int = 1000,
) -> list[dict[str, object]]:
    """Return the retained session events, resynchronising once on cursor expiry."""
    response = get_session(
        client,
        running,
        f"/v1/sessions/{session_id}/events",
        after=after,
        limit=limit,
        **({} if generation is None else {"generation": generation}),
    )
    if response.status_code == 409:
        payload = json_object(response)
        assert payload["error"] == "cursor_expired", response.text
        details = payload["details"]
        assert isinstance(details, dict)
        oldest = details["oldest_retained"]
        assert isinstance(oldest, int)
        response = get_session(
            client,
            running,
            f"/v1/sessions/{session_id}/events",
            after=oldest - 1,
            limit=limit,
            **({} if generation is None else {"generation": generation}),
        )
    assert response.status_code == 200, response.text
    events = json_object(response)["events"]
    assert isinstance(events, list)
    return [event for event in events if isinstance(event, dict)]


def log_lines(
    client: httpx.Client,
    running: RunningSessionAgent,
    *,
    session_id: str = SESSION_ID,
    after: int = 0,
    limit: int = 1000,
) -> tuple[list[dict[str, object]], int]:
    response = get_session(
        client, running, f"/v1/sessions/{session_id}/logs", after=after, limit=limit
    )
    if response.status_code == 409:
        payload = json_object(response)
        assert payload["error"] == "cursor_expired", response.text
        details = payload["details"]
        assert isinstance(details, dict)
        oldest = details["oldest_retained"]
        assert isinstance(oldest, int)
        response = get_session(
            client, running, f"/v1/sessions/{session_id}/logs", after=oldest - 1, limit=limit
        )
    assert response.status_code == 200, response.text
    payload = json_object(response)
    lines = payload["lines"]
    assert isinstance(lines, list)
    next_cursor = payload["next_cursor"]
    assert isinstance(next_cursor, int)
    return [line for line in lines if isinstance(line, dict)], next_cursor


def log_events(
    client: httpx.Client, running: RunningSessionAgent, *, session_id: str = SESSION_ID
) -> list[dict[str, object]]:
    lines, _ = log_lines(client, running, session_id=session_id)
    events: list[dict[str, object]] = []
    for line in lines:
        text = line["text"]
        if not isinstance(text, str):
            continue
        try:
            value = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            events.append({str(key): item for key, item in value.items()})
    return events


def wait_for_log_event(
    client: httpx.Client,
    running: RunningSessionAgent,
    event_type: str,
    *,
    session_id: str = SESSION_ID,
    timeout: float = 15.0,
) -> dict[str, object]:
    def _find() -> dict[str, object] | None:
        for event in log_events(client, running, session_id=session_id):
            if event.get("type") == event_type:
                return event
        return None

    found = wait_until(_find, timeout=timeout)
    assert isinstance(found, dict)
    return found


def observation_file(running: RunningSessionAgent, session_id: str = SESSION_ID) -> Path:
    return running.session_dir(session_id) / "scratch" / "work" / "fake-output.json"


def read_observations(
    running: RunningSessionAgent, session_id: str = SESSION_ID
) -> dict[str, object]:
    path = observation_file(running, session_id)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    assert isinstance(payload, dict), path
    return {str(key): value for key, value in payload.items()}


def wait_for_observation(
    running: RunningSessionAgent,
    key: str,
    *,
    session_id: str = SESSION_ID,
    timeout: float = 15.0,
) -> object:
    def _find() -> object | None:
        observations = read_observations(running, session_id)
        entries = observations.get("observations")
        if not isinstance(entries, list):
            return None
        for entry in entries:
            if isinstance(entry, dict) and entry.get("kind") == key:
                return entry
        return None

    return wait_until(_find, timeout=timeout)


def proc_alive(pid: int) -> bool:
    """A process exists and is not a zombie (Linux ``/proc`` oracle)."""
    try:
        text = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except OSError:
        return False
    remainder = text.rpartition(")")[2].split()
    return bool(remainder) and remainder[0] != "Z"


def wait_for_process_gone(pid: int, *, timeout: float = 15.0) -> None:
    wait_until(lambda: not proc_alive(pid), timeout=timeout)


def runtime_files_containing(running: RunningSessionAgent, needle: bytes) -> list[str]:
    """Relative names of runtime session files whose bytes contain ``needle``."""
    hits: list[str] = []
    root = running.session_dir()
    for directory, _, names in os.walk(root):
        for name in names:
            path = Path(directory) / name
            try:
                if path.stat().st_size > 4 * 1024 * 1024:
                    continue
                data = path.read_bytes()
            except OSError:
                continue
            if needle in data:
                hits.append(str(path.relative_to(root)))
    return sorted(hits)


def workspace_files_containing(running: RunningSessionAgent, needle: bytes) -> list[str]:
    hits: list[str] = []
    for directory, _, names in os.walk(running.host_path):
        for name in names:
            path = Path(directory) / name
            try:
                if path.stat().st_size > 4 * 1024 * 1024:
                    continue
                data = path.read_bytes()
            except OSError:
                continue
            if needle in data:
                hits.append(str(path.relative_to(running.host_path)))
    return sorted(hits)


def uds_connect(socket_path: Path, *, timeout: float = 1.0) -> socket.socket:
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    connection.settimeout(timeout)
    connection.connect(str(socket_path))
    return connection
