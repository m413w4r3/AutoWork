"""M24-05 H1 red oracles: supervisor lifecycle, idempotency, killpg, restart, logs.

Normative source: ``docs/design/investigation-workshop/SESSION-RUNNER.md`` §5-§6
and §11 (H1). Every test is offline and rootless; the only child process is the
deterministic fake CLI.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest
from sidecars.workspace_agent.sessions import fake_cli as fake_cli_module
from sidecars.workspace_agent.sessions.state import SecretUnavailableError
from sidecars.workspace_agent.sessions.supervisor import (
    AF_UNIX_SUN_PATH_MAX,
    IPC_DIRECTORY,
    SESSION_SOCKET_FILE_NAME,
    FakeProfile,
    PrepareSessionRequest,
    ProfileRegistry,
    ResumeRequest,
    SessionSupervisor,
    SessionSupervisorConfig,
    StartSessionRequest,
)

from tests.workspace_agent.conftest import REPOSITORY_ROOT, short_socket_base
from tests.workspace_agent.sessions_support import (
    CLOSE_KEY,
    FAKE_CLI_PATH,
    INTERRUPT_KEY,
    PREPARE_KEY,
    SECOND_SESSION_ID,
    SESSION_ID,
    START_KEY,
    fake_script,
    get_session,
    hang_script,
    log_events,
    log_lines,
    post_session,
    prepare_payload,
    proc_alive,
    running_session_agent,
    session_events,
    success_script,
    wait_for_log_event,
    wait_for_observation,
    wait_for_process_gone,
    wait_for_state,
    wait_until,
    write_fake_script,
)
from tests.workspace_agent.w3_support import make_scan_environment


def _prepare(client, running, workspace_id: str, **kwargs: object):
    payload = prepare_payload(workspace_id, **kwargs)
    return post_session(client, running, "/v1/sessions", payload, PREPARE_KEY)


def _start(client, running, body: dict[str, object] | None = None):
    return post_session(
        client, running, f"/v1/sessions/{SESSION_ID}/start", {} if body is None else body, START_KEY
    )


def test_start_once_and_replay_is_idempotent(session_base: Path) -> None:
    with running_session_agent(session_base) as running, running.client() as client:
        workspace_id = running.ensure_workspace(client)
        prepared = _prepare(client, running, workspace_id)
        assert prepared.status_code == 200, prepared.text
        write_fake_script(
            running,
            fake_script(
                [
                    {"op": "emit", "event": {"type": "started"}},
                    {"op": "write", "path": "notes/finding.md", "text": "note\n"},
                    {"op": "hang"},
                ]
            ),
        )

        started = _start(client, running)
        assert started.status_code == 200, started.text
        first = started.json()
        assert first["state"] == "running"
        assert first["run"] == 1
        assert isinstance(first["log_cursor"], int)

        replay = _start(client, running)
        assert replay.status_code == 200, replay.text
        assert replay.json() == first

        conflicting = _start(client, running, {"run_prompt": "different"})
        assert conflicting.status_code == 409, conflicting.text
        assert conflicting.json()["error"] == "idempotency_conflict"

        second_start = post_session(
            client,
            running,
            f"/v1/sessions/{SESSION_ID}/start",
            {},
            "m24-h1-start-0002",
        )
        assert second_start.status_code == 409, second_start.text
        assert second_start.json()["error"] in {"session_state_conflict", "session_busy"}

        interrupted = post_session(
            client,
            running,
            f"/v1/sessions/{SESSION_ID}/interrupt",
            {"reason": "test"},
            INTERRUPT_KEY,
        )
        assert interrupted.status_code == 200, interrupted.text
        wait_for_state(client, running, "stopped", "lost", "failed")
        events = session_events(client, running)
        assert sum(1 for event in events if event["kind"] == "process_started") == 1
        assert sum(1 for event in events if event["kind"] == "state_changed") >= 2
        status = get_session(client, running, f"/v1/sessions/{SESSION_ID}").json()
        assert len(status["runs"]) == 1


def test_prepare_rejects_unknown_adapter_and_arbitrary_binary(session_base: Path) -> None:
    with running_session_agent(session_base) as running, running.client() as client:
        workspace_id = running.ensure_workspace(client)

        unknown = post_session(
            client,
            running,
            "/v1/sessions",
            prepare_payload(workspace_id, adapter_kind="not_an_adapter"),
            "m24-h1-prepare-unknown-1",
        )
        assert unknown.status_code == 409, unknown.text
        assert unknown.json()["error"] == "capability_unavailable"

        unshipped = post_session(
            client,
            running,
            "/v1/sessions",
            prepare_payload(workspace_id, adapter_kind="codex_cli"),
            "m24-h1-prepare-unknown-2",
        )
        assert unshipped.status_code == 409, unshipped.text
        assert unshipped.json()["error"] == "capability_unavailable"

        for field_name, value in (("binary", "/bin/sh"), ("argv", ["/bin/sh", "-c", "id"])):
            rejected = post_session(
                client,
                running,
                "/v1/sessions",
                prepare_payload(workspace_id, **{field_name: value}),
                f"m24-h1-prepare-binary-{field_name}",
            )
            assert rejected.status_code == 400, rejected.text
            body = rejected.json()
            assert body["error"] == "invalid_request"
            assert body["details"]["field"] == field_name

        assert not running.session_dir().exists()


def test_interrupt_kills_process_group(session_base: Path) -> None:
    with running_session_agent(session_base) as running, running.client() as client:
        workspace_id = running.ensure_workspace(client)
        assert _prepare(client, running, workspace_id).status_code == 200
        write_fake_script(running, hang_script(child_seconds=120))

        assert _start(client, running).status_code == 200
        started = wait_for_log_event(client, running, "started")
        fake_pid = started["pid"]
        assert isinstance(fake_pid, int)
        child = wait_for_observation(running, "child_started")
        assert isinstance(child, dict)
        grandchild_pid = child["pid"]
        assert isinstance(grandchild_pid, int)
        assert proc_alive(fake_pid)
        assert proc_alive(grandchild_pid)

        interrupted = post_session(
            client,
            running,
            f"/v1/sessions/{SESSION_ID}/interrupt",
            {"reason": "test"},
            INTERRUPT_KEY,
        )
        assert interrupted.status_code == 200, interrupted.text
        assert interrupted.json() == {"state": "interrupt_requested", "signal_sent": True}

        wait_for_state(client, running, "stopped", "lost", "failed")
        wait_for_process_gone(grandchild_pid)
        wait_for_process_gone(fake_pid)

        events = log_events(client, running)
        assert any(event.get("type") == "started" for event in events)


def test_restart_marks_active_lost_and_never_relaunches(
    session_base: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AUTOWORK_SESSIONS_WATCHDOG_MS", "20")
    monkeypatch.setenv("AUTOWORK_SESSIONS_INTERRUPT_GRACE_MS", "100")

    with running_session_agent(session_base) as first:
        with first.client() as client:
            workspace_id = first.ensure_workspace(client)
            assert _prepare(client, first, workspace_id).status_code == 200
            write_fake_script(first, hang_script())
            assert _start(client, first).status_code == 200
            started = wait_for_log_event(client, first, "started")
            fake_pid = started["pid"]
            assert isinstance(fake_pid, int)
            before_state = json.loads(
                (first.session_dir() / "state.json").read_text(encoding="utf-8")
            )
            before_generation = before_state["generation"]
            assert isinstance(before_generation, int)

    # The first supervisor is gone; its child must not have been adopted.
    wait_for_process_gone(fake_pid)

    with running_session_agent(session_base) as second:
        with second.client() as client:
            status = get_session(client, second, f"/v1/sessions/{SESSION_ID}")
            assert status.status_code == 200, status.text
            body = status.json()
            assert body["state"] == "lost", body

            events = log_events(client, second)
            assert sum(1 for event in events if event.get("type") == "started") == 1
            stale = get_session(
                client,
                second,
                f"/v1/sessions/{SESSION_ID}/events",
                after=0,
                generation=before_generation,
            )
            assert stale.status_code == 409, stale.text
            assert stale.json()["error"] == "cursor_expired"
            assert stale.json()["details"]["resync_required"] is True
            session_events = get_session(
                client, second, f"/v1/sessions/{SESSION_ID}/events", after=0, limit=100
            )
            assert session_events.status_code in {200, 409}, session_events.text
            if session_events.status_code == 409:
                details = session_events.json()["details"]
                session_events = get_session(
                    client,
                    second,
                    f"/v1/sessions/{SESSION_ID}/events",
                    after=details["oldest_retained"] - 1,
                    limit=100,
                )
            assert session_events.status_code == 200, session_events.text
            page = session_events.json()
            assert page["generation"] == before_generation + 1
            kinds = [event["kind"] for event in page["events"]]
            assert "reconcile_completed" in kinds
            reconcile = next(
                event for event in page["events"] if event["kind"] == "reconcile_completed"
            )
            assert reconcile["payload"]["outcome"] in {
                "lost_with_handoff",
                "lost_unreconciled",
            }

            reconciled = post_session(
                client,
                second,
                f"/v1/sessions/{SESSION_ID}/reconcile",
                {},
                "m24-h1-reconcile-0001",
            )
            assert reconciled.status_code == 200, reconciled.text
            assert reconciled.json()["outcome"] in {
                "lost_with_handoff",
                "lost_unreconciled",
            }

            # Reconcile never relaunches: exactly the historical start line remains.
            assert (
                sum(1 for event in log_events(client, second) if event.get("type") == "started")
                == 1
            )


def test_log_ring_bounded_and_cursor_expiry_resync(
    session_base: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AUTOWORK_SESSIONS_LOG_RING_BYTES", "2048")
    monkeypatch.setenv("AUTOWORK_SESSIONS_EVENT_RING_ENTRIES", "3")

    flood = fake_script(
        [
            {"op": "emit", "event": {"type": "started"}},
            {"op": "log", "stream": "stdout", "count": 200, "bytes": 100},
            {"op": "exit", "code": 0},
        ]
    )
    with running_session_agent(session_base) as running, running.client() as client:
        workspace_id = running.ensure_workspace(client)
        assert _prepare(client, running, workspace_id).status_code == 200
        write_fake_script(running, flood)
        assert _start(client, running).status_code == 200
        wait_for_state(client, running, "stopped", "failed")

        expired = get_session(
            client, running, f"/v1/sessions/{SESSION_ID}/logs", after=0, limit=1000
        )
        assert expired.status_code == 409, expired.text
        envelope = expired.json()
        assert envelope["error"] == "cursor_expired"
        assert envelope["details"]["resync_required"] is True
        oldest = envelope["details"]["oldest_retained"]
        assert isinstance(oldest, int) and oldest > 1

        lines, next_cursor = log_lines(client, running)
        assert lines
        total = sum(len(str(line["text"]).encode()) for line in lines)
        assert total <= 2048, total
        assert next_cursor == max(
            seq for seq in (line["seq"] for line in lines) if isinstance(seq, int)
        )

        stale_generation = get_session(
            client, running, f"/v1/sessions/{SESSION_ID}/events", after=0, generation=999
        )
        assert stale_generation.status_code == 409, stale_generation.text
        assert stale_generation.json()["error"] == "cursor_expired"
        assert stale_generation.json()["details"]["resync_required"] is True

        expired_events = get_session(
            client, running, f"/v1/sessions/{SESSION_ID}/events", after=0, limit=100
        )
        assert expired_events.status_code == 409, expired_events.text
        assert expired_events.json()["error"] == "cursor_expired"
        details = expired_events.json()["details"]
        assert details["oldest_retained"] > 1
        retained = get_session(
            client,
            running,
            f"/v1/sessions/{SESSION_ID}/events",
            after=details["oldest_retained"] - 1,
            limit=100,
        )
        assert retained.status_code == 200, retained.text
        page = retained.json()
        assert len(page["events"]) <= 3
        assert page["resync_required"] is False


def test_session_runtime_never_in_workspace(session_base: Path) -> None:
    with running_session_agent(session_base) as running, running.client() as client:
        workspace_id = running.ensure_workspace(client)
        assert _prepare(client, running, workspace_id).status_code == 200

        session_dir = running.session_dir()
        assert session_dir.is_dir()
        assert session_dir.is_relative_to(running.runtime_dir)
        assert not session_dir.is_relative_to(running.root)
        assert not session_dir.is_relative_to(running.host_path)
        assert not str(session_dir).startswith(str(running.host_path))

        state_path = session_dir / "state.json"
        assert state_path.is_file()
        assert (state_path.stat().st_mode & 0o777) == 0o600
        assert (session_dir.stat().st_mode & 0o777) == 0o750

        found: list[str] = []
        for directory, _, names in os.walk(running.root):
            for name in names:
                found.append(str(Path(directory) / name))
        assert all("sessions" not in Path(path).parts for path in found), found


def test_capabilities_probe_is_deterministic_and_hash_checked(session_base: Path) -> None:
    with running_session_agent(session_base) as running, running.client() as client:
        first = get_session(client, running, "/v1/sessions/capabilities", adapter_kind="fake")
        assert first.status_code == 200, first.text
        probe = first.json()
        assert probe["protocol"] == "workspace-agent.sessions.v1"
        assert probe["adapter_kind"] == "fake"
        assert probe["confinement"]["netns"] is False
        digest = hashlib.sha256(FAKE_CLI_PATH.read_bytes()).hexdigest()
        assert probe["binary_sha256"] == digest
        assert isinstance(probe["probe_sha256"], str) and len(probe["probe_sha256"]) == 64

        unknown = get_session(client, running, "/v1/sessions/capabilities", adapter_kind="nope")
        assert unknown.status_code == 409
        assert unknown.json()["error"] == "capability_unavailable"


def test_resume_is_refused_until_the_profile_proves_it(session_base: Path) -> None:
    with running_session_agent(session_base) as running, running.client() as client:
        workspace_id = running.ensure_workspace(client)
        assert _prepare(client, running, workspace_id).status_code == 200
        write_fake_script(running, success_script())
        assert _start(client, running).status_code == 200
        wait_for_state(client, running, "stopped", "failed")

        resumed = post_session(
            client,
            running,
            f"/v1/sessions/{SESSION_ID}/resume",
            {"reason": "prove the gate"},
            "m24-h1-resume-0001",
        )
        assert resumed.status_code == 409, resumed.text
        assert resumed.json()["error"] == "resume_unavailable"
        status = get_session(client, running, f"/v1/sessions/{SESSION_ID}").json()
        assert len(status["runs"]) == 1


def test_resume_runs_only_when_the_profile_proved_it(session_base: Path) -> None:
    environment = make_scan_environment(session_base)
    config = SessionSupervisorConfig(
        runtime_root=session_base / "run", workspace_root=environment.root
    )
    profiles = ProfileRegistry((FakeProfile(supports_resume=True),))
    supervisor = SessionSupervisor(
        config,
        workspaces=environment.manager,
        scans=environment.service(),
        profiles=profiles,
    )
    try:
        request = PrepareSessionRequest.from_payload(prepare_payload(environment.workspace_id))
        supervisor.prepare(request, idempotency_key="m24-h1-prepare-resume-1", body_sha256="a" * 64)
        script_path = config.session_root / SESSION_ID / "fake-script.json"
        script_path.write_text(
            json.dumps(success_script(), separators=(",", ":")), encoding="utf-8"
        )
        started = supervisor.start(
            SESSION_ID,
            StartSessionRequest(),
            idempotency_key="m24-h1-start-resume-1",
            body_sha256="b" * 64,
        )
        assert started["run"] == 1
        wait_until(lambda: supervisor.status(SESSION_ID)["state"] == "stopped", timeout=15)
        resumed = supervisor.resume(
            SESSION_ID,
            ResumeRequest(reason="the probe proved resume"),
            idempotency_key="m24-h1-resume-proved-1",
            body_sha256="c" * 64,
        )
        assert resumed["state"] == "running"
        assert resumed["run"] == 2
        wait_until(lambda: supervisor.status(SESSION_ID)["state"] == "stopped", timeout=15)
        assert len(supervisor.status(SESSION_ID)["runs"]) == 2
    finally:
        supervisor.stop()


def test_tool_manifest_is_hash_checked_and_stored(session_base: Path) -> None:
    tools = [
        {
            "mcp_name": "autowork_task_heartbeat",
            "broker_name": "task.heartbeat",
            "schema_version": "1",
            "input_schema": {"type": "object"},
            "effect": "read",
            "cost": "cheap",
            "capability": None,
        }
    ]
    canonical = json.dumps({"tools": tools}, sort_keys=True, separators=(",", ":")).encode("utf-8")
    manifest_sha256 = hashlib.sha256(canonical).hexdigest()
    with running_session_agent(session_base) as running, running.client() as client:
        workspace_id = running.ensure_workspace(client)
        assert _prepare(client, running, workspace_id).status_code == 200

        accepted = post_session(
            client,
            running,
            f"/v1/sessions/{SESSION_ID}/tool-manifest",
            {"manifest_sha256": manifest_sha256, "tools": tools},
            "m24-h1-manifest-0001",
        )
        assert accepted.status_code == 200, accepted.text
        assert accepted.json() == {"accepted": True, "manifest_sha256": manifest_sha256}

        refused = post_session(
            client,
            running,
            f"/v1/sessions/{SESSION_ID}/tool-manifest",
            {"manifest_sha256": "0" * 64, "tools": tools},
            "m24-h1-manifest-0002",
        )
        assert refused.status_code == 400, refused.text
        assert refused.json()["error"] == "invalid_request"
        assert refused.json()["details"]["field"] == "manifest_sha256"


def test_close_deletes_secret_socket_scratch_and_keeps_state(session_base: Path) -> None:
    with running_session_agent(session_base) as running, running.client() as client:
        workspace_id = running.ensure_workspace(client)
        assert _prepare(client, running, workspace_id).status_code == 200
        session_dir = running.session_dir()
        # A prepared session already owns its one-shot secret listener and its
        # broker relay (M24.1 LIFE-1); both are pathname sockets in ipc/.
        assert (session_dir / "ipc" / "shim.sock").is_socket()
        assert (session_dir / "ipc" / "broker.sock").is_socket()

        closed = post_session(
            client, running, f"/v1/sessions/{SESSION_ID}/close", {"reason": "done"}, CLOSE_KEY
        )
        assert closed.status_code == 200, closed.text
        assert closed.json() == {"state": "closed"}
        assert (session_dir / "state.json").is_file()
        assert (session_dir / "events.jsonl").is_file()
        assert not (session_dir / "ipc" / "shim.sock").exists()
        assert not (session_dir / "ipc" / "broker.sock").exists()
        assert not (session_dir / "ipc" / "egress.sock").exists()
        assert not (session_dir / "scratch").exists()
        assert not (session_dir / "prompt.json").exists()

        status = get_session(client, running, f"/v1/sessions/{SESSION_ID}")
        assert status.status_code == 200
        assert status.json()["state"] == "closed"


def test_instruction_queue_delivers_live_and_replays(session_base: Path) -> None:
    instruction_id = "b3c4d5e6-f708-4a1b-9c2d-3e4f5a6b7c8d"
    text = "inspect the synthetic yara rule"
    sha256 = hashlib.sha256(text.encode("utf-8")).hexdigest()
    body = {"instruction_id": instruction_id, "text": text, "sha256": sha256}
    with running_session_agent(session_base) as running, running.client() as client:
        workspace_id = running.ensure_workspace(client)
        assert _prepare(client, running, workspace_id).status_code == 200
        write_fake_script(
            running,
            fake_script(
                [
                    {"op": "emit", "event": {"type": "started"}},
                    {"op": "wait_instruction", "timeout": 10},
                    {"op": "write", "path": "result.md", "text": "done\n"},
                    {"op": "exit", "code": 0},
                ]
            ),
        )
        assert _start(client, running).status_code == 200

        delivered = post_session(
            client,
            running,
            f"/v1/sessions/{SESSION_ID}/instructions",
            body,
            "m24-h1-instruction-0001",
        )
        assert delivered.status_code == 200, delivered.text
        payload = delivered.json()
        assert payload["accepted"] is True
        assert payload["delivered"] == "live"
        assert isinstance(payload["cursor"], int)

        replay = post_session(
            client,
            running,
            f"/v1/sessions/{SESSION_ID}/instructions",
            body,
            "m24-h1-instruction-0001",
        )
        assert replay.status_code == 200
        assert replay.json() == payload

        wait_for_state(client, running, "stopped", "failed")
        observation = wait_for_observation(running, "instruction")
        assert isinstance(observation, dict)
        assert observation["instruction_id"] == instruction_id
        assert observation["sha256"] == sha256
        assert observation["length"] == len(text)

        kinds = [event["kind"] for event in session_events(client, running)]
        assert "instruction_accepted" in kinds
        assert "instruction_delivered" in kinds


def test_prepare_embeds_only_allowlisted_skill_packs(
    session_base: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    skills_root = session_base / "skills"
    good = skills_root / "autowork-test"
    good.mkdir(parents=True)
    (good / "SKILL.md").write_text("# allowed skill\n", encoding="utf-8")
    linked = skills_root / "autowork-link"
    linked.symlink_to(good)
    monkeypatch.setenv("AUTOWORK_SESSIONS_SKILLS_ROOT", str(skills_root))

    with running_session_agent(session_base) as running, running.client() as client:
        workspace_id = running.ensure_workspace(client)
        accepted = post_session(
            client,
            running,
            "/v1/sessions",
            prepare_payload(workspace_id, skill_names=["autowork-test"]),
            "m24-h1-prepare-skill-1",
        )
        assert accepted.status_code == 200, accepted.text

        not_allowlisted = post_session(
            client,
            running,
            "/v1/sessions",
            prepare_payload(
                workspace_id,
                session_id=SECOND_SESSION_ID,
                skill_names=["not-an-autowork-skill"],
            ),
            "m24-h1-prepare-skill-2",
        )
        assert not_allowlisted.status_code == 400, not_allowlisted.text
        assert not_allowlisted.json()["error"] == "invalid_request"

        symlinked = post_session(
            client,
            running,
            "/v1/sessions",
            prepare_payload(
                workspace_id,
                session_id=SECOND_SESSION_ID,
                skill_names=["autowork-link"],
            ),
            "m24-h1-prepare-skill-3",
        )
        assert symlinked.status_code == 400, symlinked.text
        assert symlinked.json()["details"]["field"] == "skill_names"


def test_die_with_parent_semantics_kill_the_spawned_cli(tmp_path: Path) -> None:
    """The fake profile's child dies when its parent (the supervisor) dies."""
    script_path = tmp_path / "hang.json"
    script_path.write_text(
        json.dumps(fake_script([{"op": "hang"}]), separators=(",", ":")), encoding="utf-8"
    )
    output_path = tmp_path / "fake-output.json"
    env = {
        "PATH": "/usr/bin:/bin",
        "AUTOWORK_SESSION_ID": SESSION_ID,
        "AUTOWORK_FAKE_CLI_SCENARIO": "hang",
    }
    parent_script = tmp_path / "parent.py"
    parent_script.write_text(
        "import json, subprocess, sys, time\n"
        "argv = json.loads(sys.argv[1])\n"
        "env = json.loads(sys.argv[2])\n"
        "child = subprocess.Popen(argv, env=env, cwd=sys.argv[3], start_new_session=True,\n"
        "                          stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,\n"
        "                          stderr=subprocess.DEVNULL)\n"
        "print(child.pid, flush=True)\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    argv = [
        sys.executable,
        "-B",
        str(FAKE_CLI_PATH),
        "--session-id",
        SESSION_ID,
        "--run",
        "1",
        "--scenario",
        "hang",
        "--script",
        str(script_path),
        "--output-file",
        str(output_path),
    ]
    parent = subprocess.Popen(
        [sys.executable, str(parent_script), json.dumps(argv), json.dumps(env), str(tmp_path)],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert parent.stdout is not None
        child_pid = int(parent.stdout.readline().strip())
        assert proc_alive(child_pid)
        parent.kill()
        parent.wait(timeout=10)
        wait_for_process_gone(child_pid, timeout=15)
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.wait(timeout=5)


def test_fake_cli_module_is_standalone_and_deterministic() -> None:
    """The fake CLI never imports the package (it runs from a scratch cwd)."""
    source = Path(fake_cli_module.__file__).read_text(encoding="utf-8")
    assert "from sidecars" not in source
    assert "import sidecars" not in source
    assert str(REPOSITORY_ROOT) not in source
    started = time.monotonic()
    completed = subprocess.run(
        [sys.executable, "-B", str(FAKE_CLI_PATH), "--help"],
        capture_output=True,
        text=True,
        timeout=30,
        env={"PATH": "/usr/bin:/bin"},
    )
    assert completed.returncode == 0, completed.stderr
    assert time.monotonic() - started < 30


def test_short_socket_base_ignores_a_polluted_process_tempdir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A leaked ``tempfile.gettempdir()`` cache must never move a test socket."""
    polluted = tmp_path / ("nested-" * 12)
    polluted.mkdir()
    monkeypatch.setenv("TMPDIR", str(polluted))
    monkeypatch.setattr(tempfile, "tempdir", str(polluted))
    base = short_socket_base("m24-polluted-")
    try:
        assert not base.is_relative_to(polluted)
        socket_path = (
            base / "run" / "sessions" / SESSION_ID / IPC_DIRECTORY / SESSION_SOCKET_FILE_NAME
        )
        assert len(os.fsencode(socket_path)) <= AF_UNIX_SUN_PATH_MAX, socket_path
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_sidecar_prepares_a_session_under_a_polluted_process_tempdir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reproduce the CI leak: a long leaked TMPDIR must not break the sidecar."""
    polluted = tmp_path / ("nested-" * 12)
    polluted.mkdir()
    monkeypatch.setenv("TMPDIR", str(polluted))
    monkeypatch.setattr(tempfile, "tempdir", str(polluted))
    base = short_socket_base("m24-polluted-")
    try:
        with running_session_agent(base) as running, running.client() as client:
            workspace_id = running.ensure_workspace(client)
            prepared = post_session(
                client, running, "/v1/sessions", prepare_payload(workspace_id), PREPARE_KEY
            )
            assert prepared.status_code == 200, prepared.text
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_prepare_fails_closed_when_the_session_socket_path_is_too_long(
    session_base: Path,
) -> None:
    environment = make_scan_environment(session_base)
    config = SessionSupervisorConfig(
        runtime_root=session_base / ("d" * 120),
        workspace_root=environment.root,
    )
    supervisor = SessionSupervisor(
        config,
        workspaces=environment.manager,
        scans=environment.service(),
        profiles=ProfileRegistry((FakeProfile(),)),
    )
    try:
        request = PrepareSessionRequest.from_payload(prepare_payload(environment.workspace_id))
        with pytest.raises(SecretUnavailableError) as excinfo:
            supervisor.prepare(
                request, idempotency_key="m24-h1-prepare-long-1", body_sha256="a" * 64
            )
        assert excinfo.value.details == {"reason": "session_socket_path_too_long"}
        assert not (config.session_root / SESSION_ID).exists()
    finally:
        supervisor.stop()
