"""M24.1 R2B-1 oracles: seeded provider credentials never outlive their session.

A CLI profile seeds the operator's provider credential into the disposable
session runtime (Claude ``config/home/.claude/.credentials.json``, Codex
``config/codex/auth.json``).  The design says the copy is deleted at close; the
host must also remove it when it stops supervising, when a run is interrupted
and when it reconciles a session left behind by a crashed supervisor.  A copy
the host cannot remove is never silent: the session becomes ``failed`` with a
typed reason and refuses any later start.

Every test is offline and rootless: a real ``SessionSupervisor`` owns the
runtime directory and the built-in ``fake`` profile spawns the repository's
real fake CLI.  The credential files are synthetic; no real auth file is read.
"""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest
from tests.workspace_agent.sessions_support import (
    SESSION_ID,
    TASK_ID,
    fake_script,
    hang_script,
    prepare_payload,
    wait_until,
)
from tests.workspace_agent.w3_support import ScanEnvironment, make_scan_environment

from sidecars.workspace_agent.sessions import process
from sidecars.workspace_agent.sessions import supervisor as sessions_module
from sidecars.workspace_agent.sessions.state import SessionState
from sidecars.workspace_agent.sessions.supervisor import (
    CollectRequest,
    FakeProfile,
    PrepareSessionRequest,
    ProfileRegistry,
    ReasonRequest,
    StartSessionRequest,
)

CLAUDE_CREDENTIAL = Path("config/home/.claude/.credentials.json")
CODEX_AUTH = Path("config/codex/auth.json")

CLAUDE_CREDENTIAL_BYTES = json.dumps(
    {
        "claudeAiOauth": {
            "accessToken": "synthetic-claude-access-token-0123456789abcdef",
            "refreshToken": "synthetic-claude-refresh-token-0123456789abcdef",
            "expiresAt": 1893456000000,
        }
    },
    indent=2,
).encode("utf-8")
CODEX_AUTH_BYTES = json.dumps(
    {
        "OPENAI_API_KEY": None,
        "tokens": {
            "access_token": "synthetic-codex-access-token-0123456789abcdef",
            "refresh_token": "synthetic-codex-refresh-token-0123456789abcdef",
            "id_token": "synthetic-codex-id-token-0123456789abcdef",
        },
    },
    indent=2,
).encode("utf-8")

CREDENTIAL_CLEANUP_REASON = "credential_cleanup_failed"


def _supervisor(
    session_base: Path, environment: ScanEnvironment
) -> sessions_module.SessionSupervisor:
    config = sessions_module.SessionSupervisorConfig(
        runtime_root=session_base / "run",
        workspace_root=environment.root,
        interrupt_grace_seconds=0.2,
        watchdog_poll_seconds=0.05,
    )
    return sessions_module.SessionSupervisor(
        config,
        workspaces=environment.manager,
        scans=environment.service(),
        profiles=ProfileRegistry((FakeProfile(),)),
    )


def _prepare(supervisor: sessions_module.SessionSupervisor, environment: ScanEnvironment) -> None:
    supervisor.prepare(
        PrepareSessionRequest.from_payload(prepare_payload(environment.workspace_id)),
        idempotency_key="m24-r2b1-prepare-0001",
        body_sha256="a" * 64,
    )


def _session_dir(session_base: Path) -> Path:
    return session_base / "run" / "sessions" / SESSION_ID


def _plant_credentials(session_base: Path) -> tuple[Path, Path]:
    """Place the two synthetic copies where the CLI profiles install them."""
    directory = _session_dir(session_base)
    paths: list[Path] = []
    for relative, data in (
        (CLAUDE_CREDENTIAL, CLAUDE_CREDENTIAL_BYTES),
        (CODEX_AUTH, CODEX_AUTH_BYTES),
    ):
        path = directory / relative
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.write_bytes(data)
        os.chmod(path, 0o600)
        paths.append(path)
    assert all(path.is_file() for path in paths)
    return paths[0], paths[1]


def _close(supervisor: sessions_module.SessionSupervisor, *, key: str = "m24-r2b1-close-0001"):
    return supervisor.close(
        SESSION_ID,
        ReasonRequest.from_payload({"reason": "r2b-1 oracle"}),
        idempotency_key=key,
        body_sha256="c" * 64,
    )


def _events(supervisor: sessions_module.SessionSupervisor) -> list[dict[str, object]]:
    page = supervisor.events(SESSION_ID, after=0, generation=None, limit=1000)
    events = page["events"]
    assert isinstance(events, list)
    return [event for event in events if isinstance(event, dict)]


def test_session_etc_creation_replaces_stale_hardlinks_without_truncating_targets(
    tmp_path: Path,
) -> None:
    runtime = tmp_path / "runtime-session"
    runtime.mkdir(mode=0o700)
    stale = runtime / process.SESSION_ETC_DIRECTORY_NAME
    stale.mkdir(mode=0o700)
    preserved = runtime / "synthetic-state"
    preserved.write_bytes(b"synthetic state that must remain intact")
    os.link(preserved, stale / "hosts")

    created = process.create_session_etc_directory(runtime)

    assert created == stale
    assert preserved.read_bytes() == b"synthetic state that must remain intact"
    for name, expected in process.SESSION_ETC_FILES:
        info = (created / name).lstat()
        assert info.st_nlink == 1
        assert info.st_mode & 0o777 == 0o600
        assert (created / name).read_bytes() == expected


def test_session_etc_creation_unlinks_a_stale_directory_symlink(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime-session"
    runtime.mkdir(mode=0o700)
    external = tmp_path / "synthetic-external"
    external.mkdir(mode=0o700)
    preserved = external / "hosts"
    preserved.write_bytes(b"synthetic external data")
    stale = runtime / process.SESSION_ETC_DIRECTORY_NAME
    stale.symlink_to(external, target_is_directory=True)

    created = process.create_session_etc_directory(runtime)

    assert created == stale
    assert not stale.is_symlink()
    assert preserved.read_bytes() == b"synthetic external data"


@pytest.fixture
def scripted_supervisors(session_base: Path) -> Iterator[list[sessions_module.SessionSupervisor]]:
    """Track every supervisor a test builds so no fake process outlives it."""
    supervisors: list[sessions_module.SessionSupervisor] = []
    try:
        yield supervisors
    finally:
        for supervisor in supervisors:
            supervisor.stop()


def test_close_removes_the_seeded_credential_copies(
    session_base: Path, scripted_supervisors: list[sessions_module.SessionSupervisor]
) -> None:
    environment = make_scan_environment(session_base)
    supervisor = _supervisor(session_base, environment)
    scripted_supervisors.append(supervisor)
    _prepare(supervisor, environment)
    planted = _plant_credentials(session_base)
    session_etc = process.create_session_etc_directory(_session_dir(session_base))

    body = _close(supervisor)

    assert body["state"] == "closed"
    assert all(not path.exists() for path in planted)
    assert not session_etc.exists()
    assert supervisor.status(SESSION_ID)["state"] == "closed"


def test_stop_removes_the_seeded_credential_copies(
    session_base: Path, scripted_supervisors: list[sessions_module.SessionSupervisor]
) -> None:
    environment = make_scan_environment(session_base)
    supervisor = _supervisor(session_base, environment)
    scripted_supervisors.append(supervisor)
    _prepare(supervisor, environment)
    planted = _plant_credentials(session_base)
    session_etc = process.create_session_etc_directory(_session_dir(session_base))

    supervisor.stop()

    assert all(not path.exists() for path in planted)
    assert not session_etc.exists()


def test_interrupt_removes_the_seeded_credential_copies(
    session_base: Path, scripted_supervisors: list[sessions_module.SessionSupervisor]
) -> None:
    environment = make_scan_environment(session_base)
    supervisor = _supervisor(session_base, environment)
    scripted_supervisors.append(supervisor)
    _prepare(supervisor, environment)
    script = _session_dir(session_base) / "fake-script.json"
    script.write_text(json.dumps(hang_script()), encoding="utf-8")
    os.chmod(script, 0o600)
    planted = _plant_credentials(session_base)
    session_etc = process.create_session_etc_directory(_session_dir(session_base))
    started = supervisor.start(
        SESSION_ID,
        StartSessionRequest(),
        idempotency_key="m24-r2b1-start-0001",
        body_sha256="b" * 64,
    )
    assert started["state"] == "running"
    wait_until(lambda: supervisor.status(SESSION_ID)["state"] == "running")

    supervisor.interrupt(
        SESSION_ID,
        ReasonRequest.from_payload({"reason": "r2b-1 interrupt oracle"}),
        idempotency_key="m24-r2b1-interrupt-0001",
        body_sha256="e" * 64,
    )
    wait_until(lambda: supervisor.status(SESSION_ID)["state"] in {"stopped", "failed"})

    assert all(not path.exists() for path in planted)
    assert not session_etc.exists()


def test_inactive_interrupt_removes_session_etc(tmp_path: Path) -> None:
    session_etc = process.create_session_etc_directory(tmp_path)
    handle = SimpleNamespace(
        lock=threading.RLock(),
        record=SimpleNamespace(
            session_id=SESSION_ID,
            workspace_id="ws-inactive-interrupt",
            state=SessionState.AWAITING_INSTRUCTION,
            state_version=0,
            outcome=None,
            updated_at=None,
            last_activity_at=None,
            stopped_at=None,
        ),
        managed=None,
        directory=tmp_path,
        events=SimpleNamespace(append_event=lambda *_args, **_kwargs: None),
    )
    supervisor = object.__new__(sessions_module.SessionSupervisor)
    supervisor._workspace_write_condition = threading.Condition()
    supervisor._workspace_write_gates = {}
    supervisor._stopped = threading.Event()
    supervisor._handle = lambda _session_id: handle
    supervisor._idempotency = SimpleNamespace(
        replay=lambda *_args: None,
        remember=lambda *_args: None,
    )
    supervisor._now = lambda: "2026-11-24T00:00:00Z"
    supervisor._stop_channels = lambda _handle, *, wipe_secret: None
    supervisor._purge_provider_credentials = lambda _handle, *, operation: None
    supervisor._persist = lambda _handle: None

    supervisor.interrupt(
        SESSION_ID,
        ReasonRequest.from_payload({"reason": "inactive interrupt cleanup oracle"}),
        idempotency_key="m24-r2b1-inactive-interrupt",
        body_sha256="f" * 64,
    )

    assert not session_etc.exists()


def test_inactive_interrupt_removes_session_etc_when_credential_purge_raises(
    tmp_path: Path,
) -> None:
    session_etc = process.create_session_etc_directory(tmp_path)
    handle = SimpleNamespace(
        lock=threading.RLock(),
        record=SimpleNamespace(
            session_id=SESSION_ID,
            workspace_id="ws-inactive-interrupt",
            state=SessionState.AWAITING_INSTRUCTION,
            state_version=0,
            outcome=None,
            updated_at=None,
            last_activity_at=None,
            stopped_at=None,
        ),
        managed=None,
        directory=tmp_path,
        events=SimpleNamespace(append_event=lambda *_args, **_kwargs: None),
    )
    supervisor = object.__new__(sessions_module.SessionSupervisor)
    supervisor._workspace_write_condition = threading.Condition()
    supervisor._workspace_write_gates = {}
    supervisor._stopped = threading.Event()
    supervisor._handle = lambda _session_id: handle
    supervisor._idempotency = SimpleNamespace(
        replay=lambda *_args: None,
        remember=lambda *_args: None,
    )
    supervisor._now = lambda: "2026-11-24T00:00:00Z"
    supervisor._stop_channels = lambda _handle, *, wipe_secret: None

    def _raise_on_purge(_handle, *, operation: str) -> None:
        raise RuntimeError(f"synthetic purge failure: {operation}")

    supervisor._purge_provider_credentials = _raise_on_purge
    supervisor._persist = lambda _handle: None

    with pytest.raises(RuntimeError, match="synthetic purge failure: interrupt"):
        supervisor.interrupt(
            SESSION_ID,
            ReasonRequest.from_payload({"reason": "inactive interrupt purge failure"}),
            idempotency_key="m24-r2b1-inactive-interrupt-purge-failure",
            body_sha256="f" * 64,
        )

    assert not session_etc.exists()


def test_failed_run_end_removes_a_surviving_credential_copy(
    session_base: Path, scripted_supervisors: list[sessions_module.SessionSupervisor]
) -> None:
    """R4-F9: finalizing a terminal run purges copies left after shutdown.

    An interrupted or failed CLI can rewrite its credential file while it
    stops, after the interrupt-time purge.  Once the process is gone the
    supervisor purges again for the outcomes that never reach the reuse
    point, so no copy outlives a terminal run.
    """
    environment = make_scan_environment(session_base)
    supervisor = _supervisor(session_base, environment)
    scripted_supervisors.append(supervisor)
    _prepare(supervisor, environment)
    script = _session_dir(session_base) / "fake-script.json"
    script.write_text(json.dumps(fake_script([{"op": "crash", "code": 1}])), encoding="utf-8")
    os.chmod(script, 0o600)
    planted = _plant_credentials(session_base)
    supervisor.start(
        SESSION_ID,
        StartSessionRequest(),
        idempotency_key="m24-r2b1-start-0002",
        body_sha256="b" * 64,
    )

    wait_until(lambda: supervisor.status(SESSION_ID)["state"] == "failed")

    assert all(not path.exists() for path in planted)


def test_restart_reconciliation_removes_an_orphan_session_credential_copy(
    session_base: Path, scripted_supervisors: list[sessions_module.SessionSupervisor]
) -> None:
    environment = make_scan_environment(session_base)
    crashed = _supervisor(session_base, environment)
    _prepare(crashed, environment)
    planted = _plant_credentials(session_base)
    session_etc = process.create_session_etc_directory(_session_dir(session_base))
    # No close/stop: this supervisor is the "crash" the restart must clean up.

    restarted = _supervisor(session_base, environment)
    scripted_supervisors.append(restarted)

    assert all(not path.exists() for path in planted)
    assert not session_etc.exists()
    status = restarted.status(SESSION_ID)
    assert status["state"] == "lost"
    events = _events(restarted)
    assert any(event["kind"] == "reconcile_completed" for event in events)


def test_collect_replay_after_supervisor_restart_keeps_one_import_and_provenance(
    session_base: Path,
    scripted_supervisors: list[sessions_module.SessionSupervisor],
) -> None:
    environment = make_scan_environment(session_base)
    crashed = _supervisor(session_base, environment)
    scripted_supervisors.append(crashed)
    _prepare(crashed, environment)
    script = _session_dir(session_base) / "fake-script.json"
    script.write_text(
        json.dumps(
            fake_script(
                [
                    {"op": "write", "path": "notes/allowed.md", "text": "# collected note\n"},
                    {"op": "write", "path": "result.md", "text": "collected result\n"},
                    {"op": "exit", "code": 0},
                ]
            )
        ),
        encoding="utf-8",
    )
    os.chmod(script, 0o600)
    crashed.start(
        SESSION_ID,
        StartSessionRequest(),
        idempotency_key="m24-r2b1-start-replay",
        body_sha256="b" * 64,
    )
    wait_until(lambda: crashed.status(SESSION_ID)["state"] == "awaiting_instruction")

    collect_request = CollectRequest(capture=True)
    first = crashed.collect(
        SESSION_ID,
        collect_request,
        idempotency_key="m24-e3-collect-restart",
        body_sha256="d" * 64,
    )
    planted = _plant_credentials(session_base)

    # This is a supervisor process crash: the second instance reloads durable
    # state and must remove the seeded copies before it accepts any request.
    restarted = _supervisor(session_base, environment)
    scripted_supervisors.append(restarted)
    assert all(not path.exists() for path in planted)
    assert restarted.status(SESSION_ID)["state"] == "lost"
    replay = restarted.collect(
        SESSION_ID,
        collect_request,
        idempotency_key="m24-e3-collect-restart",
        body_sha256="d" * 64,
    )

    assert (
        replay["captured_notes"]
        == first["captured_notes"]
        == [f"notes/agents/{TASK_ID}/allowed.md"]
    )
    assert {entry["path"]: entry["sha256"] for entry in replay["files"]} == {
        entry["path"]: entry["sha256"] for entry in first["files"]
    }
    scan_service = environment.service(stability_window_ms=0)
    scan_service.scan(environment.workspace_id, full=True)
    snapshot = scan_service.snapshot(environment.workspace_id)
    note_path = f"notes/agents/{TASK_ID}/allowed.md"
    result_path = f"results/sessions/{SESSION_ID}/result.md"
    note = next(item for item in snapshot.files if item.path == note_path)
    result = next(item for item in snapshot.files if item.path == result_path)
    assert note.origin == "agent"
    assert note.task_id == TASK_ID
    assert note.policy_class == "agent_owned"
    assert result.origin == "system"
    assert result.policy_class == "generated_immutable"
    assert len(scan_service.known_revisions(environment.workspace_id, note_path)) == 1
    assert len(scan_service.known_revisions(environment.workspace_id, result_path)) == 1


def test_close_fails_closed_when_a_credential_copy_cannot_be_removed(
    session_base: Path, scripted_supervisors: list[sessions_module.SessionSupervisor]
) -> None:
    environment = make_scan_environment(session_base)
    supervisor = _supervisor(session_base, environment)
    scripted_supervisors.append(supervisor)
    _prepare(supervisor, environment)
    claude, codex = _plant_credentials(session_base)
    os.chmod(claude.parent, 0o500)  # unlink needs write permission on the parent
    try:
        with pytest.raises(sessions_module.SessionError) as raised:
            _close(supervisor)
    finally:
        os.chmod(claude.parent, 0o700)

    assert raised.value.details["reason"] == CREDENTIAL_CLEANUP_REASON
    assert raised.value.details["operation"] == "close"
    # The host never claims a clean close it cannot guarantee: the session is
    # failed and refuses any later start.
    assert supervisor.status(SESSION_ID)["state"] == "failed"
    with pytest.raises(sessions_module.SessionStateConflictError):
        supervisor.start(
            SESSION_ID,
            StartSessionRequest(),
            idempotency_key="m24-r2b1-start-0002",
            body_sha256="b" * 64,
        )
    revoked = [
        event
        for event in _events(supervisor)
        if event["payload"].get("reason") == CREDENTIAL_CLEANUP_REASON
    ]
    assert revoked, _events(supervisor)
    assert revoked[-1]["payload"]["to"] == "failed"
    # The copy survives its failed unlink, but the best-effort overwrite already
    # shredded it and neither the event nor the state echoes any credential.
    assert claude.is_file()
    assert CLAUDE_CREDENTIAL_BYTES not in claude.read_bytes()
    state_text = json.dumps(_events(supervisor))
    for data in (CLAUDE_CREDENTIAL_BYTES, CODEX_AUTH_BYTES):
        assert data.decode("utf-8") not in state_text
    # Once the host can delete it, a retried close completes and removes both.
    assert _close(supervisor, key="m24-r2b1-close-0002")["state"] == "closed"
    assert not claude.exists()
    assert not codex.exists()


def test_reconciliation_fails_closed_when_a_credential_copy_cannot_be_removed(
    session_base: Path, scripted_supervisors: list[sessions_module.SessionSupervisor]
) -> None:
    environment = make_scan_environment(session_base)
    crashed = _supervisor(session_base, environment)
    _prepare(crashed, environment)
    claude, _codex = _plant_credentials(session_base)
    os.chmod(claude.parent, 0o500)
    try:
        restarted = _supervisor(session_base, environment)
    finally:
        os.chmod(claude.parent, 0o700)
    scripted_supervisors.append(restarted)

    assert restarted.status(SESSION_ID)["state"] == "failed"
    with pytest.raises(sessions_module.SessionStateConflictError):
        restarted.start(
            SESSION_ID,
            StartSessionRequest(),
            idempotency_key="m24-r2b1-start-0003",
            body_sha256="b" * 64,
        )
    events = _events(restarted)
    reconcile = [event for event in events if event["kind"] == "reconcile_completed"]
    assert reconcile and reconcile[-1]["payload"].get("credential_cleanup_failed") is True
    assert claude.exists()

    # A third start (permissions restored) purges the leftover copy even
    # though the record is already terminal.
    third = _supervisor(session_base, environment)
    scripted_supervisors.append(third)
    assert not claude.exists()
    assert third.status(SESSION_ID)["state"] == "failed"
