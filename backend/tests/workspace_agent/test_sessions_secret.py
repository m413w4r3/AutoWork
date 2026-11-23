"""M24-05 H1 secret oracles: the session secret never leaks and is delivered once.

The secret transits the AF_UNIX wire once at ``prepare``, lives only in
supervisor memory, and is handed once to the child over the per-session
``ipc/shim.sock``. It must never appear in argv, env, state, logs, prompts,
workspace files or HTTP response bodies.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from tests.workspace_agent.sessions_support import (
    COLLECT_KEY,
    PREPARE_KEY,
    SECRET,
    SECRET_B64,
    SECRET_HEX,
    SECRET_SHA256,
    SESSION_ID,
    START_KEY,
    fake_script,
    get_session,
    log_events,
    post_session,
    prepare_payload,
    running_session_agent,
    runtime_files_containing,
    wait_for_observation,
    wait_for_state,
    workspace_files_containing,
    write_fake_script,
)


def _prepare(client, running, workspace_id: str):
    return post_session(client, running, "/v1/sessions", prepare_payload(workspace_id), PREPARE_KEY)


def _secret_script() -> dict[str, object]:
    return fake_script(
        [
            {"op": "emit", "event": {"type": "started"}},
            {"op": "probe"},
            {"op": "receive_secret", "attempts": 2},
            {"op": "write", "path": "notes/secret.md", "text": "captured note\n"},
            {"op": "write", "path": "result.md", "text": "result\n"},
            {"op": "exit", "code": 0},
        ],
        final_message="secret scenario final",
    )


def test_secret_never_in_state_logs_argv_env_or_responses(session_base: Path) -> None:
    with running_session_agent(session_base) as running, running.client() as client:
        workspace_id = running.ensure_workspace(client)
        assert _prepare(client, running, workspace_id).status_code == 200
        write_fake_script(running, _secret_script())
        started = post_session(client, running, f"/v1/sessions/{SESSION_ID}/start", {}, START_KEY)
        assert started.status_code == 200, started.text
        wait_for_state(client, running, "awaiting_instruction", "stopped", "failed")

        status = get_session(client, running, f"/v1/sessions/{SESSION_ID}")
        events = get_session(
            client, running, f"/v1/sessions/{SESSION_ID}/events", after=0, limit=1000
        )
        logs = get_session(client, running, f"/v1/sessions/{SESSION_ID}/logs", after=0, limit=1000)
        collected = post_session(
            client,
            running,
            f"/v1/sessions/{SESSION_ID}/collect",
            {"capture": True},
            COLLECT_KEY,
        )
        assert collected.status_code == 200, collected.text
        bodies = " ".join(
            response.text
            for response in (status, events, logs, collected)
            if response.status_code == 200
        )
        assert SECRET_B64 not in bodies
        assert SECRET_HEX not in bodies
        assert SECRET_HEX[:32] not in bodies

        runtime_hits = runtime_files_containing(running, SECRET_B64.encode())
        runtime_hits += runtime_files_containing(running, SECRET_HEX.encode())
        runtime_hits += runtime_files_containing(running, SECRET)
        assert runtime_hits == []
        workspace_hits = workspace_files_containing(running, SECRET_B64.encode())
        workspace_hits += workspace_files_containing(running, SECRET_HEX.encode())
        workspace_hits += workspace_files_containing(running, SECRET)
        assert workspace_hits == []

        observations = wait_for_observation(running, "probe")
        assert isinstance(observations, dict)
        probe = observations.get("argv"), observations.get("env")
        serialized = json.dumps(probe)
        assert SECRET_B64 not in serialized
        assert SECRET_HEX not in serialized
        env = observations.get("env")
        assert isinstance(env, dict)
        assert not any("secret" in str(key).lower() for key in env)

        received = wait_for_observation(running, "secret_received")
        assert isinstance(received, dict)
        assert received["sha256"] == SECRET_SHA256
        assert received["length"] == 32


def test_secret_handoff_is_single_use(session_base: Path) -> None:
    with running_session_agent(session_base) as running, running.client() as client:
        workspace_id = running.ensure_workspace(client)
        assert _prepare(client, running, workspace_id).status_code == 200
        write_fake_script(running, _secret_script())
        assert (
            post_session(
                client, running, f"/v1/sessions/{SESSION_ID}/start", {}, START_KEY
            ).status_code
            == 200
        )
        wait_for_state(client, running, "awaiting_instruction", "stopped", "failed")

        received = wait_for_observation(running, "secret_received")
        assert isinstance(received, dict)
        assert received["sha256"] == SECRET_SHA256
        second = wait_for_observation(running, "secret_second_attempt")
        assert isinstance(second, dict)
        assert second["delivered"] is False
        socket_path = running.session_dir() / "ipc" / "shim.sock"
        assert not socket_path.exists()


def test_secret_is_rearmed_for_each_run(session_base: Path) -> None:
    """BL3: a second run of the same session gets a fresh one-shot handoff.

    The secret stays only in supervisor memory; each run gets its own
    single-use socket, and consuming it never prevents a later run from
    receiving the same secret again.
    """
    script = fake_script(
        [
            {"op": "emit", "event": {"type": "started"}},
            {"op": "receive_secret", "attempts": 2},
            {"op": "exit", "code": 0},
        ]
    )
    with running_session_agent(session_base) as running, running.client() as client:
        workspace_id = running.ensure_workspace(client)
        assert _prepare(client, running, workspace_id).status_code == 200
        write_fake_script(running, script)
        first = post_session(client, running, f"/v1/sessions/{SESSION_ID}/start", {}, START_KEY)
        assert first.status_code == 200, first.text
        wait_for_state(client, running, "awaiting_instruction", "stopped", "failed")
        first_received = wait_for_observation(running, "secret_received")
        assert isinstance(first_received, dict)
        assert first_received["sha256"] == SECRET_SHA256
        socket_path = running.session_dir() / "ipc" / "shim.sock"
        assert not socket_path.exists()  # the run consumed the one-shot socket

        # A second run started by the instruction primitive (D4) must find a
        # newly armed socket even though the first one was consumed.
        text = "continue the synthetic review"
        instruction = {
            "instruction_id": "b3c4d5e6-f708-4a1b-9c2d-3e4f5a6b7c8d",
            "text": text,
            "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        }
        second = post_session(
            client,
            running,
            f"/v1/sessions/{SESSION_ID}/instructions",
            instruction,
            "m24-h1-instruction-rearm-0001",
        )
        assert second.status_code == 200, second.text
        assert second.json()["delivered"] == "queued"
        wait_for_state(client, running, "awaiting_instruction", "stopped", "failed")
        second_received = wait_for_observation(running, "secret_received")
        assert isinstance(second_received, dict)
        assert second_received["sha256"] == SECRET_SHA256
        assert not socket_path.exists()
        status = get_session(client, running, f"/v1/sessions/{SESSION_ID}").json()
        assert len(status["runs"]) == 2

        delivered = [
            event for event in log_events(client, running) if event.get("type") == "secret_received"
        ]
        assert len(delivered) == 2
        hits = runtime_files_containing(running, SECRET_B64.encode())
        hits += runtime_files_containing(running, SECRET_HEX.encode())
        hits += runtime_files_containing(running, SECRET)
        assert hits == []


def test_responses_never_include_host_absolute_paths(session_base: Path) -> None:
    with running_session_agent(session_base) as running, running.client() as client:
        workspace_id = running.ensure_workspace(client)
        prepared = _prepare(client, running, workspace_id)
        write_fake_script(running, _secret_script())
        started = post_session(client, running, f"/v1/sessions/{SESSION_ID}/start", {}, START_KEY)
        wait_for_state(client, running, "awaiting_instruction", "stopped", "failed")
        collected = post_session(
            client,
            running,
            f"/v1/sessions/{SESSION_ID}/collect",
            {"capture": True},
            COLLECT_KEY,
        )
        responses = [prepared, started, collected]
        responses.extend(
            [
                get_session(client, running, f"/v1/sessions/{SESSION_ID}"),
                get_session(
                    client, running, f"/v1/sessions/{SESSION_ID}/events", after=0, limit=1000
                ),
                get_session(
                    client, running, f"/v1/sessions/{SESSION_ID}/logs", after=0, limit=1000
                ),
                get_session(client, running, "/v1/sessions/capabilities", adapter_kind="fake"),
            ]
        )
        forbidden = [str(running.runtime_dir), str(running.host_path), str(running.root)]
        for response in responses:
            for value in forbidden:
                assert value not in response.text, (response.request.url, value)
        session_events = log_events(client, running)
        assert any(event.get("type") == "started" for event in session_events)
