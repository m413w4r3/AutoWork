"""M24.1 R2-FIX-A oracles: F2 capture secret refusal and F5 closed wire alias.

The host must never persist agent content that carries the session secret
(raw, hex, standard/urlsafe base64, or a ≥16-byte aligned fragment); the first
detection revokes the session.  The backend's ``OperatorAdapterKind`` wire
names are aliased to the host profile names exactly once, at the entry point.

Every test is offline and rootless: a real ``WorkspaceAgent`` serves a real
HTTP server on a tmpdir AF_UNIX socket, and the ``fake`` profile is registered
under its canonical name.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import threading
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest
from sidecars.workspace_agent import protocol
from sidecars.workspace_agent.http_server import (
    Route,
    RouteKind,
    WorkspaceAgentRequestHandler,
    _RequestRejected,
)
from sidecars.workspace_agent.sessions import supervisor as sessions_supervisor
from sidecars.workspace_agent.sessions.state import SessionState

from tests.workspace_agent.sessions_support import (
    COLLECT_KEY,
    PREPARE_KEY,
    SECRET,
    SESSION_ID,
    START_KEY,
    TASK_ID,
    fake_script,
    get_session,
    log_lines,
    post_session,
    prepare_payload,
    running_session_agent,
    session_events,
    wait_for_state,
    workspace_files_containing,
    write_fake_script,
)
from tests.workspace_agent.w3_support import make_scan_environment


class _RequestHeaders:
    def __init__(self, values: dict[str, str]) -> None:
        self.values = {name.lower(): value for name, value in values.items()}

    def get_all(self, name: str) -> list[str]:
        value = self.values.get(name.lower())
        return [] if value is None else [value]

    def get(self, name: str, default: str | None = None) -> str | None:
        return self.values.get(name.lower(), default)


def _dispatch_agent_file_write(
    tmp_path: Path, *, body: bytes, active: bool
) -> tuple[object, Path, SimpleNamespace]:
    environment = make_scan_environment(tmp_path)
    task_note = f"notes/agents/{TASK_ID}/note.md"
    record = SimpleNamespace(
        task_id=TASK_ID,
        workspace_id=environment.descriptor.workspace_id,
        state=SessionState.READY,
    )
    handle = SimpleNamespace(
        record=record,
        lock=threading.RLock(),
        secret_forms=sessions_supervisor._secret_content_forms(URLSAFE_SECRET),
        provider_credential_forms=(),
        directory=tmp_path,
    )
    supervisor = object.__new__(sessions_supervisor.SessionSupervisor)
    supervisor._sessions = {SESSION_ID: handle} if active else {}
    supervisor._lock = threading.RLock()
    supervisor._workspace_write_condition = threading.Condition()
    supervisor._workspace_write_gates = {}
    supervisor._stopped = threading.Event()
    supervisor._revoke_secret_exposure = lambda target, **_kwargs: setattr(
        target.record, "state", SessionState.FAILED
    )

    agent = SimpleNamespace(workspaces=environment.manager, scans=environment.service())
    request = object.__new__(WorkspaceAgentRequestHandler)
    request.server = SimpleNamespace(agent=agent, session_supervisor=supervisor)
    request._agent = lambda: agent
    request._sessions = lambda: supervisor
    request.path = (
        f"/v1/workspaces/{environment.descriptor.workspace_id}/files?"
        f"{urlencode({'path': task_note})}"
    )
    request.headers = _RequestHeaders(
        {
            "Content-Length": str(len(body)),
            "Idempotency-Key": "m24-r1b-agent-note-write-0001",
            "X-Autowork-Expected-Sha256": "new",
            "X-Autowork-Origin": "agent",
            "X-Autowork-Task-Id": TASK_ID,
        }
    )
    request.rfile = io.BytesIO(body)
    route = Route(
        RouteKind.WORKSPACE,
        "/v1/workspaces/{workspace_id}/files",
        protocol.EDITABLE_FILE_LIMIT,
    )
    try:
        response: object = request._dispatch_workspace(
            "PUT", f"/v1/workspaces/{environment.descriptor.workspace_id}/files", route
        )
    except _RequestRejected as exc:
        response = exc
    return response, Path(environment.descriptor.host_path) / task_note, handle


def _dispatch_non_ui_file_write(
    tmp_path: Path,
    *,
    body: bytes,
    origin: str,
    task_id: str | None,
    active: bool,
    destination_override: str | None = None,
    additional_session_secrets: tuple[bytes, ...] = (),
) -> tuple[object, Path, SimpleNamespace]:
    environment = make_scan_environment(tmp_path)
    generated = origin == "system"
    destination = destination_override or (
        "results/export-report.md" if generated else "notes/humaines/par-agent.md"
    )
    record = SimpleNamespace(
        task_id=TASK_ID,
        workspace_id=environment.descriptor.workspace_id,
        state=SessionState.READY,
    )
    handle = SimpleNamespace(
        record=record,
        lock=threading.RLock(),
        secret_forms=sessions_supervisor._secret_content_forms(URLSAFE_SECRET),
        provider_credential_forms=(),
        directory=tmp_path,
    )
    supervisor = object.__new__(sessions_supervisor.SessionSupervisor)
    supervisor._sessions = {SESSION_ID: handle} if active else {}
    if active:
        for index, secret in enumerate(additional_session_secrets):
            supervisor._sessions[f"session-other-{index}"] = SimpleNamespace(
                record=SimpleNamespace(
                    task_id=f"task-other-{index}",
                    workspace_id=environment.descriptor.workspace_id,
                    state=SessionState.READY,
                ),
                lock=threading.RLock(),
                secret_forms=sessions_supervisor._secret_content_forms(secret),
                provider_credential_forms=(),
                directory=tmp_path,
            )
    supervisor._lock = threading.RLock()
    supervisor._workspace_write_condition = threading.Condition()
    supervisor._workspace_write_gates = {}
    supervisor._stopped = threading.Event()
    supervisor._revoke_secret_exposure = lambda target, **_kwargs: setattr(
        target.record, "state", SessionState.FAILED
    )

    agent = SimpleNamespace(workspaces=environment.manager, scans=environment.service())
    request = object.__new__(WorkspaceAgentRequestHandler)
    request.server = SimpleNamespace(agent=agent, session_supervisor=supervisor)
    request._agent = lambda: agent
    request._sessions = lambda: supervisor
    route_template = (
        "/v1/workspaces/{workspace_id}/generated"
        if generated
        else "/v1/workspaces/{workspace_id}/files"
    )
    request.path = (
        f"/v1/workspaces/{environment.descriptor.workspace_id}/"
        f"{'generated' if generated else 'files'}?{urlencode({'path': destination})}"
    )
    headers = {
        "Content-Length": str(len(body)),
        "Idempotency-Key": "m24-r1c-non-ui-write-0001",
        "X-Autowork-Expected-Sha256": "new",
        "X-Autowork-Origin": origin,
    }
    if task_id is not None:
        headers["X-Autowork-Task-Id"] = task_id
    if generated:
        headers["X-Autowork-Generated-By"] = "autowork-backend"
        headers["X-Autowork-Source-Sha256"] = "a" * 64
    request.headers = _RequestHeaders(headers)
    request.rfile = io.BytesIO(body)
    route = Route(RouteKind.WORKSPACE, route_template, protocol.EDITABLE_FILE_LIMIT)
    try:
        response: object = request._dispatch_workspace(
            "PUT",
            f"/v1/workspaces/{environment.descriptor.workspace_id}/"
            f"{'generated' if generated else 'files'}",
            route,
        )
    except _RequestRejected as exc:
        response = exc
    return response, Path(environment.descriptor.host_path) / destination, handle


def _std_b64(value: bytes) -> bytes:
    return base64.b64encode(value)


def _url_b64(value: bytes) -> bytes:
    return base64.urlsafe_b64encode(value)


# A secret that really exercises the urlsafe alphabet (+/ vs -_).
URLSAFE_SECRET = bytes([0xFB, 0xFF, 0xBF]) + SECRET[3:]


def _encoded_forms(secret: bytes) -> dict[str, bytes]:
    standard = _std_b64(secret)
    urlsafe = _url_b64(secret)
    fragment = secret[8:24]
    return {
        "raw": secret,
        "hex": secret.hex().encode("ascii"),
        "hex_upper": secret.hex().upper().encode("ascii"),
        "b64": standard,
        "b64_unpadded": standard.rstrip(b"="),
        "b64url": urlsafe,
        "b64url_unpadded": urlsafe.rstrip(b"="),
        "fragment_raw": fragment,
        "fragment_hex": fragment.hex().encode("ascii"),
        "fragment_b64": _std_b64(fragment),
    }


def _prepare(client, running, workspace_id: str, *, secret: bytes = SECRET):
    return post_session(
        client,
        running,
        "/v1/sessions",
        prepare_payload(workspace_id, auth_secret=secret),
        PREPARE_KEY,
    )


def _drop_note(running, name: str, data: bytes) -> Path:
    """Drop a file where the sandbox's rw ``scratch/`` bind exposes it."""
    notes: Path = running.session_dir() / "scratch" / "notes"
    notes.mkdir(parents=True, exist_ok=True)
    path = notes / name
    path.write_bytes(data)
    return path


def _collect(client, running, key: str = COLLECT_KEY):
    return post_session(
        client,
        running,
        f"/v1/sessions/{SESSION_ID}/collect",
        {"capture": True},
        key,
    )


def _status(client, running) -> dict[str, object]:
    response = get_session(client, running, f"/v1/sessions/{SESSION_ID}")
    assert response.status_code == 200, response.text
    body = response.json()
    assert isinstance(body, dict)
    return {str(key): value for key, value in body.items()}


def test_alias_is_closed_and_symmetric() -> None:
    from sidecars.workspace_agent.sessions.supervisor import (
        HOST_ADAPTER_WIRE_NAMES,
        WIRE_ADAPTER_ALIASES,
        canonical_adapter_kind,
        wire_adapter_kind,
    )

    assert canonical_adapter_kind("codex_cli") == "codex"
    assert canonical_adapter_kind("claude_code") == "claude"
    assert canonical_adapter_kind("fake") == "fake"
    assert canonical_adapter_kind("codex") == "codex"
    assert canonical_adapter_kind("codex_cli_v2") == "codex_cli_v2"
    assert canonical_adapter_kind(None) is None
    assert wire_adapter_kind("codex") == "codex_cli"
    assert wire_adapter_kind("claude") == "claude_code"
    assert wire_adapter_kind("fake") == "fake"
    assert set(WIRE_ADAPTER_ALIASES) == {"codex_cli", "claude_code"}
    assert set(HOST_ADAPTER_WIRE_NAMES) == {"codex", "claude"}


def test_registry_resolves_a_profile_registered_under_the_wire_name() -> None:
    """M24's acceptance harness registers its fake profile under ``codex_cli``."""
    from sidecars.workspace_agent.sessions.supervisor import FakeProfile, ProfileRegistry

    # ``sidecars.*`` is an untyped boundary for mypy (pyproject override), so the
    # FakeProfile base is ``Any``; the subclass only binds the adapter kind.
    class _WireNamed(FakeProfile):  # type: ignore[misc]
        adapter_kind = "codex_cli"

    registry = ProfileRegistry((_WireNamed(),))
    assert registry.get("codex").adapter_kind == "codex_cli"
    assert registry.get("codex_cli").adapter_kind == "codex_cli"
    with pytest.raises(sessions_supervisor.CapabilityUnavailableError):
        registry.get("codex_cli_v2")


@pytest.mark.parametrize(
    "form",
    [
        "raw",
        "hex",
        "hex_upper",
        "b64",
        "b64_unpadded",
        "b64url",
        "b64url_unpadded",
        "fragment_raw",
        "fragment_hex",
        "fragment_b64",
    ],
)
def test_collect_refuses_a_note_containing_the_secret(session_base: Path, form: str) -> None:
    secret = URLSAFE_SECRET
    forms = _encoded_forms(secret)
    payload = forms[form]
    note = b"# synthetic note\n" + payload + b"\n"
    with running_session_agent(session_base) as running, running.client() as client:
        workspace_id = running.ensure_workspace(client)
        prepared = _prepare(client, running, workspace_id, secret=secret)
        assert prepared.status_code == 200, prepared.text
        # The fake profile arms the relay and the one-shot secret at prepare.
        ipc = running.session_dir() / "ipc"
        assert (ipc / "broker.sock").is_socket()
        assert (ipc / "shim.sock").is_socket()

        _drop_note(running, "leak.bin", note)
        collected = _collect(client, running)
        assert collected.status_code == 403, collected.text
        body = collected.json()
        assert body["error"] == "policy_denied"
        assert body["details"]["reason"] == "secret_exposure_detected"
        assert body["details"]["operation"] == "collect"

        status = _status(client, running)
        assert status["state"] == "failed"
        # Revocation destroys both listeners and the armed secret socket.
        assert not (ipc / "broker.sock").exists()
        assert not (ipc / "shim.sock").exists()

        assert workspace_files_containing(running, payload) == []
        assert not (running.host_path / "notes" / "agents" / TASK_ID / "leak.bin").exists()

        events = session_events(client, running)
        revoked = [
            event
            for event in events
            if event["kind"] == "state_changed"
            and event["payload"].get("reason") == "secret_exposure_detected"
        ]
        assert revoked, events
        assert revoked[-1]["payload"]["to"] == "failed"
        assert revoked[-1]["payload"]["operation"] == "collect"
        # No event carries the offending content in any tested form.
        serialized = json.dumps(events)
        for encoded in forms.values():
            assert encoded.decode("latin-1") not in serialized


def test_collect_refuses_the_secret_in_the_result_file(session_base: Path) -> None:
    with running_session_agent(session_base) as running, running.client() as client:
        workspace_id = running.ensure_workspace(client)
        assert _prepare(client, running, workspace_id).status_code == 200
        result = running.session_dir() / "scratch" / "result.md"
        result.write_bytes(b"result body\n" + _std_b64(SECRET) + b"\n")
        collected = _collect(client, running)
        assert collected.status_code == 403, collected.text
        assert collected.json()["details"]["reason"] == "secret_exposure_detected"
        assert _status(client, running)["state"] == "failed"
        assert not (running.host_path / "results" / "sessions" / SESSION_ID / "result.md").exists()


def test_collect_refuses_a_session_secret_in_the_destination_path(session_base: Path) -> None:
    revoked: list[object] = []
    supervisor = object.__new__(sessions_supervisor.SessionSupervisor)
    supervisor._revoke_secret_exposure = lambda handle, **_kwargs: revoked.append(handle)
    handle = SimpleNamespace(
        secret_forms=sessions_supervisor._secret_content_forms(SECRET),
        provider_credential_forms=(),
        directory=session_base,
    )
    destination = f"notes/agents/{TASK_ID}/{SECRET.hex()}.md"

    with pytest.raises(sessions_supervisor.SecretExposureError):
        supervisor._refuse_secret_exposure(handle, ((destination, "notes", b"clean note"),))

    assert revoked == [handle]


def test_agent_file_route_refuses_active_task_secret_before_persist(tmp_path: Path) -> None:
    body = b"# synthetic note\n" + URLSAFE_SECRET[4:24] + b"\n"

    response, destination, handle = _dispatch_agent_file_write(tmp_path, body=body, active=True)

    assert isinstance(response, _RequestRejected)
    assert response.status == 403
    assert response.code == protocol.ErrorCode.POLICY_DENIED
    assert handle.record.state is SessionState.FAILED
    assert not destination.exists()


def test_agent_file_route_allows_clean_content_for_active_task(tmp_path: Path) -> None:
    body = b"# synthetic clean note\n"

    response, destination, handle = _dispatch_agent_file_write(tmp_path, body=body, active=True)

    assert isinstance(response, tuple)
    assert response[0] == 201
    assert handle.record.state is SessionState.READY
    assert destination.read_bytes() == body


def test_agent_file_route_keeps_writes_without_active_session(tmp_path: Path) -> None:
    body = b"# synthetic note without a linked session\n"

    response, destination, _handle = _dispatch_agent_file_write(tmp_path, body=body, active=False)

    assert isinstance(response, tuple)
    assert response[0] == 201
    assert destination.read_bytes() == body


@pytest.mark.parametrize(
    ("origin", "task_id"),
    [("system", None), ("agent", None)],
)
def test_non_ui_file_routes_refuse_active_session_secret_before_persist(
    tmp_path: Path, origin: str, task_id: str | None
) -> None:
    body = b"# synthetic write\n" + URLSAFE_SECRET[4:24] + b"\n"

    response, destination, handle = _dispatch_non_ui_file_write(
        tmp_path, body=body, origin=origin, task_id=task_id, active=True
    )

    assert isinstance(response, _RequestRejected)
    assert response.status == 403
    assert response.code == protocol.ErrorCode.POLICY_DENIED
    assert handle.record.state is SessionState.FAILED
    assert not destination.exists()


@pytest.mark.parametrize(
    ("origin", "task_id"),
    [("system", None), ("agent", None)],
)
def test_non_ui_file_routes_allow_clean_content_for_active_session(
    tmp_path: Path, origin: str, task_id: str | None
) -> None:
    body = b"# synthetic clean write\n"

    response, destination, handle = _dispatch_non_ui_file_write(
        tmp_path, body=body, origin=origin, task_id=task_id, active=True
    )

    assert isinstance(response, tuple)
    assert response[0] == 201
    assert handle.record.state is SessionState.READY
    assert destination.read_bytes() == body


@pytest.mark.parametrize("origin", ["system", "agent"])
def test_non_ui_file_route_does_not_refuse_a_clean_credential_named_destination(
    tmp_path: Path, origin: str
) -> None:
    destination_path = "results/auth.json" if origin == "system" else "notes/humaines/auth.json"
    body = b"# synthetic clean content\n"

    response, destination, handle = _dispatch_non_ui_file_write(
        tmp_path,
        body=body,
        origin=origin,
        task_id=None,
        active=True,
        destination_override=destination_path,
    )

    assert isinstance(response, tuple)
    assert response[0] == 201
    assert handle.record.state is SessionState.READY
    assert destination.read_bytes() == body


def test_non_ui_file_route_refuses_secret_fragment_in_destination_path(
    tmp_path: Path,
) -> None:
    body = b"# synthetic clean content\n"
    path_fragment = URLSAFE_SECRET[8:24].hex()

    response, destination, handle = _dispatch_non_ui_file_write(
        tmp_path,
        body=body,
        origin="system",
        task_id=None,
        active=True,
        destination_override=f"results/export-{path_fragment}.md",
    )

    assert isinstance(response, _RequestRejected)
    assert response.status == 403
    assert response.code == protocol.ErrorCode.POLICY_DENIED
    assert handle.record.state is SessionState.FAILED
    assert not destination.exists()


def test_non_ui_file_route_checks_all_active_sessions_despite_task_id(
    tmp_path: Path,
) -> None:
    other_secret = b"independent-session-secret-value-92715"
    body = b"# synthetic write\n" + other_secret[8:24] + b"\n"

    response, destination, _handle = _dispatch_non_ui_file_write(
        tmp_path,
        body=body,
        origin="system",
        task_id=TASK_ID,
        active=True,
        additional_session_secrets=(other_secret,),
    )

    assert isinstance(response, _RequestRejected)
    assert response.status == 403
    assert response.code == protocol.ErrorCode.POLICY_DENIED
    assert not destination.exists()


def test_file_write_rechecks_secret_after_exposure_revocation(tmp_path: Path) -> None:
    forms = sessions_supervisor._secret_content_forms(URLSAFE_SECRET)
    record = SimpleNamespace(
        task_id=TASK_ID,
        state=SessionState.READY,
        state_version=1,
        outcome=None,
        updated_at=None,
        last_activity_at=None,
    )

    class _Events:
        def __init__(self) -> None:
            self.payloads: list[dict[str, object]] = []

        def append_event(self, *, payload: dict[str, object], **_kwargs: object) -> None:
            self.payloads.append(payload)

    events = _Events()
    persisted: list[None] = []
    handle = SimpleNamespace(
        record=record,
        lock=threading.RLock(),
        events=events,
        secret_forms=forms,
        provider_credential_forms=(),
        directory=tmp_path,
    )
    supervisor = object.__new__(sessions_supervisor.SessionSupervisor)
    supervisor._sessions = {SESSION_ID: handle}
    supervisor._lock = threading.RLock()
    supervisor._now = lambda: "synthetic-time"

    def stop_channels(target, *, wipe_secret, clear_forms=False) -> None:
        if clear_forms:
            target.secret_forms = ()

    supervisor._stop_channels = stop_channels
    supervisor._persist = lambda *_args, **_kwargs: persisted.append(None)

    with pytest.raises(sessions_supervisor.SecretExposureError):
        supervisor.check_non_ui_file_write(TASK_ID, "results/first.md", lambda: URLSAFE_SECRET)

    assert record.state is SessionState.FAILED
    assert record.outcome == sessions_supervisor.SECRET_EXPOSURE_REASON
    assert handle.secret_forms == forms
    revoked_version = record.state_version
    event_count = len(events.payloads)
    persist_count = len(persisted)
    with pytest.raises(sessions_supervisor.SecretExposureError):
        supervisor.check_non_ui_file_write(
            TASK_ID, "results/second.md", lambda: URLSAFE_SECRET[8:24]
        )
    assert record.state_version == revoked_version
    assert len(events.payloads) == event_count
    assert len(persisted) == persist_count


@pytest.mark.parametrize(
    ("origin", "task_id"),
    [("system", None), ("agent", None)],
)
def test_non_ui_file_routes_without_active_session_do_not_read_temporary_content(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    origin: str,
    task_id: str | None,
) -> None:
    def unexpected_read(*_args: object) -> bytes:
        pytest.fail("temporary content read without an active session")

    monkeypatch.setattr(sessions_supervisor.files, "_read_temporary_bytes", unexpected_read)
    body = b"# synthetic write carrying a session secret\n" + URLSAFE_SECRET[4:24]

    response, destination, handle = _dispatch_non_ui_file_write(
        tmp_path, body=body, origin=origin, task_id=task_id, active=False
    )

    assert isinstance(response, tuple)
    assert response[0] == 201
    assert handle.record.state is SessionState.READY
    assert destination.read_bytes() == body


def test_ui_file_route_keeps_secret_content_unchanged(tmp_path: Path) -> None:
    body = b"# human-authored note\n" + URLSAFE_SECRET[4:24]

    response, destination, handle = _dispatch_non_ui_file_write(
        tmp_path, body=body, origin="ui", task_id=None, active=True
    )

    assert isinstance(response, tuple)
    assert response[0] == 201
    assert handle.record.state is SessionState.READY
    assert destination.read_bytes() == body


def test_file_write_secret_revocation_event_records_file_write_operation(
    tmp_path: Path,
) -> None:
    recorded: list[dict[str, object]] = []
    record = SimpleNamespace(
        state=SessionState.READY,
        state_version=1,
        outcome=None,
        updated_at=None,
        last_activity_at=None,
    )

    class _Events:
        def append_event(self, *, payload: dict[str, object], **_kwargs: object) -> None:
            recorded.append(payload)

    handle = SimpleNamespace(
        record=record,
        events=_Events(),
        secret_forms=sessions_supervisor._secret_content_forms(URLSAFE_SECRET),
        provider_credential_forms=(),
        directory=tmp_path,
    )
    supervisor = object.__new__(sessions_supervisor.SessionSupervisor)
    supervisor._now = lambda: "synthetic-time"
    supervisor._stop_channels = lambda *_args, **_kwargs: None
    supervisor._persist = lambda *_args, **_kwargs: None

    with pytest.raises(sessions_supervisor.SecretExposureError):
        supervisor._refuse_secret_exposure(
            handle,
            (("results/export-report.md", "file", URLSAFE_SECRET),),
            operation="file_write",
        )

    assert recorded[-1]["operation"] == "file_write"


def test_collect_refuses_secret_split_across_three_notes(
    session_base: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = bytes(range(ord("A"), ord("A") + 45))
    session_directory = session_base / "session"
    scratch = session_directory / "scratch"
    notes = scratch / "notes"
    notes.mkdir(parents=True)
    for index in range(3):
        (notes / f"part-{index}.md").write_bytes(secret[index * 15 : (index + 1) * 15])
    handle = SimpleNamespace(
        record=SimpleNamespace(
            workspace_id="ws-test",
            task_id="task-test",
            session_id="sess-test",
            current_run=1,
            captured={},
        ),
        scratch=scratch,
        directory=session_directory,
        secret_forms=sessions_supervisor._secret_content_forms(secret),
        provider_credential_forms=(),
    )
    revoked: list[object] = []
    supervisor = object.__new__(sessions_supervisor.SessionSupervisor)
    supervisor._workspaces = SimpleNamespace(get_workspace=lambda _workspace_id: object())
    supervisor._config = SimpleNamespace(workspace_root=session_base)
    supervisor._scans = object()
    supervisor._revoke_secret_exposure = lambda target, **_kwargs: revoked.append(target)
    supervisor._ensure_capture_parent = lambda *_args: None
    monkeypatch.setattr(
        sessions_supervisor.files,
        "write_file",
        lambda **_kwargs: sessions_supervisor.files.WriteOutcome(
            status=201, body={"revision": {"sha256": "a" * 64}}
        ),
    )

    with pytest.raises(sessions_supervisor.SecretExposureError):
        supervisor._capture(handle)

    assert revoked == [handle]


def test_collect_ignores_an_oversized_note_outside_the_manifest(session_base: Path) -> None:
    session_directory = session_base / "session"
    scratch = session_directory / "scratch"
    notes = scratch / "notes"
    notes.mkdir(parents=True)
    oversized = b"x" * (sessions_supervisor.MAX_CAPTURE_NOTE_BYTES + 1)
    for index in range(5):
        (notes / f"outside-manifest-{index}.json").write_bytes(oversized)
    handle = SimpleNamespace(
        record=SimpleNamespace(workspace_id="ws-test", task_id="task-test", session_id="sess-test"),
        scratch=scratch,
        directory=session_directory,
    )
    supervisor = object.__new__(sessions_supervisor.SessionSupervisor)
    supervisor._workspaces = SimpleNamespace(get_workspace=lambda _workspace_id: object())
    supervisor._refuse_secret_exposure = lambda _handle, _entries, **_kwargs: None

    captured, captured_notes = supervisor._capture(handle)

    assert captured == []
    assert captured_notes == []


def test_collect_budget_ignores_files_outside_the_manifest(session_base: Path) -> None:
    """Large logs the manifest ignores must not consume the importable-note budget."""
    session_directory = session_base / "session"
    scratch = session_directory / "scratch"
    notes = scratch / "notes"
    notes.mkdir(parents=True)
    # Each file is below the per-file bound; together they exceed the total budget.
    near_limit = sessions_supervisor.MAX_CAPTURE_NOTE_BYTES - 1
    count = sessions_supervisor.MAX_CAPTURE_TOTAL_BYTES // near_limit + 2
    for index in range(count):
        (notes / f"log-{index}.json").write_bytes(b"x" * near_limit)
    assert count * near_limit > sessions_supervisor.MAX_CAPTURE_TOTAL_BYTES
    handle = SimpleNamespace(
        record=SimpleNamespace(workspace_id="ws-test", task_id="task-test", session_id="sess-test"),
        scratch=scratch,
        directory=session_directory,
    )
    supervisor = object.__new__(sessions_supervisor.SessionSupervisor)
    supervisor._workspaces = SimpleNamespace(get_workspace=lambda _workspace_id: object())
    supervisor._refuse_secret_exposure = lambda _handle, _entries, **_kwargs: None

    captured, captured_notes = supervisor._capture(handle)

    assert captured == []
    assert captured_notes == []


def test_collect_copies_a_legitimate_near_miss_note(session_base: Path) -> None:
    # Flipping bytes 0 and 16 breaks every 16-byte aligned window, so the file
    # is close to the secret but shares no detectable fragment with it.
    altered = bytearray(SECRET)
    altered[0] ^= 0xFF
    altered[16] ^= 0xFF
    content = b"# synthetic near miss\n" + bytes(altered).hex().encode("ascii") + b"\n"
    with running_session_agent(session_base) as running, running.client() as client:
        workspace_id = running.ensure_workspace(client)
        assert _prepare(client, running, workspace_id).status_code == 200
        _drop_note(running, "near-miss.md", content)
        collected = _collect(client, running)
        assert collected.status_code == 200, collected.text
        body = collected.json()
        destination = f"notes/agents/{TASK_ID}/near-miss.md"
        assert destination in body["captured_notes"]
        captured = running.host_path / "notes" / "agents" / TASK_ID / "near-miss.md"
        assert captured.read_bytes() == content
        assert _status(client, running)["state"] == "ready"
        assert workspace_files_containing(running, SECRET) == []


def test_a_failed_capture_does_not_copy_the_other_notes(session_base: Path) -> None:
    with running_session_agent(session_base) as running, running.client() as client:
        workspace_id = running.ensure_workspace(client)
        assert _prepare(client, running, workspace_id).status_code == 200
        _drop_note(running, "a-clean.md", b"# clean note\n")
        _drop_note(running, "b-leak.md", b"leak\n" + SECRET.hex().encode("ascii") + b"\n")
        collected = _collect(client, running)
        assert collected.status_code == 403, collected.text
        assert not (running.host_path / "notes" / "agents" / TASK_ID / "a-clean.md").exists()
        assert workspace_files_containing(running, SECRET.hex().encode("ascii")) == []


def test_legitimate_capture_is_unchanged(session_base: Path) -> None:
    with running_session_agent(session_base) as running, running.client() as client:
        workspace_id = running.ensure_workspace(client)
        assert _prepare(client, running, workspace_id).status_code == 200
        _drop_note(running, "plain.md", b"# plain note\n")
        collected = _collect(client, running)
        assert collected.status_code == 200, collected.text
        assert f"notes/agents/{TASK_ID}/plain.md" in collected.json()["captured_notes"]


def test_cli_logs_never_persist_the_raw_secret(session_base: Path) -> None:
    """F2 defense in depth: a printable secret in a CLI log line is redacted."""
    printable = b"ABCDEFGHIJKLMNOPQRSTUVWX01234567"
    assert len(printable) == 32
    script = fake_script(
        [
            {"op": "emit", "event": {"type": printable.decode("ascii")}},
            {"op": "exit", "code": 0},
        ]
    )
    with running_session_agent(session_base) as running, running.client() as client:
        workspace_id = running.ensure_workspace(client)
        assert _prepare(client, running, workspace_id, secret=printable).status_code == 200
        write_fake_script(running, script)
        started = post_session(client, running, f"/v1/sessions/{SESSION_ID}/start", {}, START_KEY)
        assert started.status_code == 200, started.text
        # A clean run end is the D2 instruction receive point (ID-4).
        wait_for_state(client, running, "awaiting_instruction", "stopped", "failed")
        lines, _cursor = log_lines(client, running)
        text = "\n".join(str(line["text"]) for line in lines)
        assert printable.decode("ascii") not in text
        assert "<secret-redacted>" in text


def test_secret_forms_never_match_random_content() -> None:
    """A sanity bound: the derived forms are specific, not short byte strings."""
    forms = sessions_supervisor._secret_content_forms(SECRET)
    assert forms
    assert all(len(form) >= 16 for form in forms)
    random_bytes = hashlib.sha256(b"unrelated content").digest()
    assert not any(form in random_bytes for form in forms)
