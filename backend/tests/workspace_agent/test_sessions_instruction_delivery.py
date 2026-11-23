"""M24.1 ID-4/ID-5 oracles: host-side instruction delivery.

Normative source: ``docs/design/investigation-workshop/INSTRUCTION-DELIVERY.md``
§1.3, §3.1 (D2, D4), §3.4, §3.5 and §5 (lots ID-4/ID-5).

Every test is offline and rootless: the only child process is the deterministic
fake CLI.  A delivered instruction is the *next run's prompt*: the supervisor
re-arms the one-shot secret, re-binds the relay and provider proxy, attests the
network inside ``process.spawn`` and only then answers ``delivered:"queued"``.
There is no volatile queue and no live stdin instruction path.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest
from sidecars.workspace_agent.sessions import process
from sidecars.workspace_agent.sessions.adapters.claude import (
    ClaudeProfile,
    ConfinementProbe,
)
from sidecars.workspace_agent.sessions.adapters.codex import CodexProfile
from sidecars.workspace_agent.sessions.adapters.preamble import INSTRUCTION_BLOCK_PREAMBLE
from sidecars.workspace_agent.sessions.supervisor import (
    MAX_INSTRUCTION_BLOCK_BYTES,
    InstructionRequest,
    _compose_instruction_block,
)

from tests.workspace_agent.claude_profile_support import (
    claude_argv,
    make_launch_context,
    write_fake_bwrap,
    write_fake_claude,
)
from tests.workspace_agent.sessions_support import (
    CLOSE_KEY,
    COLLECT_KEY,
    INTERRUPT_KEY,
    PREPARE_KEY,
    SECRET_B64,
    SECRET_HEX,
    SESSION_ID,
    START_KEY,
    fake_script,
    get_session,
    log_events,
    log_lines,
    post_session,
    prepare_payload,
    proc_alive,
    read_observations,
    running_session_agent,
    runtime_files_containing,
    session_events,
    wait_until,
    write_fake_script,
)
from tests.workspace_agent.test_codex_profile import _install_fake_codex, _install_stub_bwrap
from tests.workspace_agent.test_codex_profile import _make_context as _make_codex_context

INSTRUCTION_ID = "b3c4d5e6-f708-4a1b-9c2d-3e4f5a6b7c8d"
SECOND_INSTRUCTION_ID = "c4d5e6f7-0819-4b2c-8d3e-4f5a6b7c8d9e"
INSTRUCTION_KEY = "m24-id4-instruction-0001"
REPLAY_KEY = "m24-id4-instruction-0002"
CONFLICT_KEY = "m24-id4-instruction-0003"
RETRY_KEY = "m24-id4-instruction-0004"
RETRY_ATTEMPT_KEY = "m24-id4-instruction-0005"

RUN_SCRIPT = fake_script(
    [
        {"op": "emit", "event": {"type": "started"}},
        {"op": "probe"},
        {"op": "receive_secret", "attempts": 1},
        {"op": "write", "path": "notes/finding.md", "text": "# fake finding\n"},
        {"op": "write", "path": "result.md", "text": "fake result\n"},
        {"op": "exit", "code": 0},
    ]
)


def _prepare(client, running, workspace_id: str):
    return post_session(client, running, "/v1/sessions", prepare_payload(workspace_id), PREPARE_KEY)


def _start(client, running):
    return post_session(client, running, f"/v1/sessions/{SESSION_ID}/start", {}, START_KEY)


def _instruction_body(
    text: str = "inspect the new synthetic note", instruction_id: str = INSTRUCTION_ID
) -> dict[str, object]:
    return {
        "instruction_id": instruction_id,
        "text": text,
        "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
    }


def _post_instruction(client, running, body: dict[str, object], key: str):
    return post_session(client, running, f"/v1/sessions/{SESSION_ID}/instructions", body, key)


def _run_to_awaiting(client, running, *, runs: int = 1) -> dict[str, object]:
    """Wait for ``runs`` recorded runs and the idle receive point after them."""

    def _ready() -> dict[str, object] | None:
        status = get_session(client, running, f"/v1/sessions/{SESSION_ID}").json()
        if status["state"] == "awaiting_instruction" and len(status["runs"]) == runs:
            return status
        return None

    found = wait_until(_ready, timeout=20)
    assert isinstance(found, dict)
    return found


def test_clean_run_end_reaches_awaiting_instruction(session_base: Path) -> None:
    with running_session_agent(session_base) as running, running.client() as client:
        workspace_id = running.ensure_workspace(client)
        assert _prepare(client, running, workspace_id).status_code == 200
        write_fake_script(running, RUN_SCRIPT)
        assert _start(client, running).status_code == 200
        status = _run_to_awaiting(client, running)

        assert status["exit"] == {"code": 0, "signal": None}
        assert len(status["runs"]) == 1
        pid = status["runs"][0]["pid"]
        assert isinstance(pid, int)
        wait_until(lambda: not proc_alive(pid), timeout=15)

        # Run end tears the relay, the proxy and the one-shot secret down.
        assert not (running.session_dir() / "ipc" / "shim.sock").exists()
        assert not (running.session_dir() / "ipc" / "broker.sock").exists()

        transition = [
            event
            for event in session_events(client, running)
            if event["kind"] == "state_changed"
            and event["payload"].get("to") == "awaiting_instruction"
        ]
        assert len(transition) == 1, transition
        assert transition[0]["payload"]["reason"] == "run_exit"


def test_instruction_at_receive_point_starts_next_run_with_prompt_sha(
    session_base: Path,
) -> None:
    text = "inspect the new synthetic note"
    body = _instruction_body(text)
    with running_session_agent(session_base) as running, running.client() as client:
        workspace_id = running.ensure_workspace(client)
        assert _prepare(client, running, workspace_id).status_code == 200
        write_fake_script(running, RUN_SCRIPT)
        assert _start(client, running).status_code == 200
        _run_to_awaiting(client, running)

        delivered = _post_instruction(client, running, body, INSTRUCTION_KEY)
        assert delivered.status_code == 200, delivered.text
        receipt = delivered.json()
        # The backend's HostInstructionResultV1 is strict: exactly these keys.
        assert set(receipt) == {"accepted", "delivered", "cursor"}
        assert receipt["accepted"] is True
        assert receipt["delivered"] == "queued"
        assert isinstance(receipt["cursor"], int)

        # The answer is only sent after a successful spawn: the second run is
        # already recorded when the receipt arrives.
        immediate = get_session(client, running, f"/v1/sessions/{SESSION_ID}").json()
        assert len(immediate["runs"]) == 2
        status = _run_to_awaiting(client, running, runs=2)
        assert status["exit"] == {"code": 0, "signal": None}

        # The fake CLI saw the exact bounded block: id, declared SHA and the
        # SHA-256 of the text body (another run cannot fabricate it).
        observations = read_observations(running)
        seen = observations["instruction"]
        assert isinstance(seen, dict)
        assert seen["instruction_id"] == INSTRUCTION_ID
        assert seen["sha256"] == body["sha256"]
        assert seen["text_sha256"] == body["sha256"]
        assert seen["length"] > 0

        block_path = running.session_dir() / "instruction-block.txt"
        block = block_path.read_text(encoding="utf-8")
        assert f"instruction_id={INSTRUCTION_ID}" in block
        assert f"sha256={body['sha256']}" in block
        assert str(running.session_dir()) not in block
        assert SECRET_B64 not in block and SECRET_HEX not in block

        events = session_events(client, running)
        delivered_events = [event for event in events if event["kind"] == "instruction_delivered"]
        assert len(delivered_events) == 1, delivered_events
        assert delivered_events[0]["payload"] == {
            "instruction_id": INSTRUCTION_ID,
            "sha256": body["sha256"],
            "mode": "next_run",
            "run": 2,
        }
        assert sum(1 for event in events if event["kind"] == "instruction_accepted") == 1

        # The secret was re-armed for the next run, then never persisted.
        secret_events = [
            event for event in log_events(client, running) if event.get("type") == "secret_received"
        ]
        assert len(secret_events) == 2, secret_events
        assert {event["run"] for event in secret_events} == {1, 2}
        probe = next(
            observation
            for observation in observations["observations"]
            if observation["kind"] == "probe"
        )
        serialized = json.dumps({"argv": probe["argv"], "env": probe["env"]})
        assert SECRET_B64 not in serialized and SECRET_HEX not in serialized
        lines, _ = log_lines(client, running)
        log_text = "\n".join(str(line["text"]) for line in lines)
        assert SECRET_B64 not in log_text and SECRET_HEX not in log_text
        hits = runtime_files_containing(running, SECRET_B64.encode())
        hits += runtime_files_containing(running, SECRET_HEX.encode())
        assert hits == []

        # Replaying the same instruction id and text returns the same receipt
        # and never starts a third run, even under a different host key.
        replay = _post_instruction(client, running, body, REPLAY_KEY)
        assert replay.status_code == 200, replay.text
        assert replay.json() == receipt
        status = get_session(client, running, f"/v1/sessions/{SESSION_ID}").json()
        assert len(status["runs"]) == 2
        assert (
            sum(
                1
                for event in session_events(client, running)
                if event["kind"] == "instruction_delivered"
            )
            == 1
        )


def test_same_instruction_id_with_another_sha_is_a_conflict(session_base: Path) -> None:
    first = _instruction_body("first delivered text")
    with running_session_agent(session_base) as running, running.client() as client:
        workspace_id = running.ensure_workspace(client)
        assert _prepare(client, running, workspace_id).status_code == 200
        write_fake_script(running, RUN_SCRIPT)
        assert _start(client, running).status_code == 200
        _run_to_awaiting(client, running)
        assert _post_instruction(client, running, first, INSTRUCTION_KEY).status_code == 200
        _run_to_awaiting(client, running, runs=2)

        changed = _instruction_body("second, never delivered text")
        conflict = _post_instruction(client, running, changed, CONFLICT_KEY)
        assert conflict.status_code == 409, conflict.text
        assert conflict.json()["error"] == "idempotency_conflict"
        status = get_session(client, running, f"/v1/sessions/{SESSION_ID}").json()
        assert len(status["runs"]) == 2


def test_attestation_failure_refuses_before_spawn_and_new_sha_retry_is_allowed(
    session_base: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    text = "first attempt, never delivered"
    first = _instruction_body(text)
    second_text = "second attempt after the refusal"
    second = _instruction_body(second_text)
    real_spawn = process.spawn
    attempts: list[str] = []

    with running_session_agent(session_base) as running, running.client() as client:
        running_sessions = running.session_dir()

        def refusing_spawn(plan: process.LaunchPlan) -> process.ManagedProcess:
            attempts.append("spawn")
            # The complete path ran before the attestation: the one-shot secret
            # is armed and the instruction block is already on disk.
            assert (running_sessions / "ipc" / "shim.sock").is_socket()
            assert (running_sessions / "instruction-block.txt").is_file()
            raise process.NetworkIsolationUnavailable("simulated attestation failure")

        workspace_id = running.ensure_workspace(client)
        assert _prepare(client, running, workspace_id).status_code == 200
        write_fake_script(running, RUN_SCRIPT)
        assert _start(client, running).status_code == 200
        _run_to_awaiting(client, running)

        monkeypatch.setattr(process, "spawn", refusing_spawn)
        refused = _post_instruction(client, running, first, RETRY_KEY)
        assert refused.status_code == 503, refused.text
        assert refused.json()["error"] == "confinement_unavailable"
        assert attempts == ["spawn"]
        status = get_session(client, running, f"/v1/sessions/{SESSION_ID}").json()
        assert status["state"] == "awaiting_instruction"
        assert len(status["runs"]) == 1
        kinds = [event["kind"] for event in session_events(client, running)]
        assert "instruction_delivered" not in kinds

        monkeypatch.setattr(process, "spawn", real_spawn)
        retried = _post_instruction(client, running, second, RETRY_ATTEMPT_KEY)
        assert retried.status_code == 200, retried.text
        status = _run_to_awaiting(client, running, runs=2)
        assert len(status["runs"]) == 2
        delivered = [
            event
            for event in session_events(client, running)
            if event["kind"] == "instruction_delivered"
        ]
        assert len(delivered) == 1
        assert delivered[0]["payload"]["sha256"] == second["sha256"]


def test_supervisor_restart_marks_awaiting_instruction_lost_and_refuses(
    session_base: Path,
) -> None:
    body = _instruction_body("must never run after the restart")
    with running_session_agent(session_base) as first, first.client() as client:
        workspace_id = first.ensure_workspace(client)
        assert _prepare(client, first, workspace_id).status_code == 200
        write_fake_script(first, RUN_SCRIPT)
        assert _start(client, first).status_code == 200
        _run_to_awaiting(client, first)

    with running_session_agent(session_base) as second, second.client() as client:
        status = get_session(client, second, f"/v1/sessions/{SESSION_ID}")
        assert status.status_code == 200, status.text
        assert status.json()["state"] == "lost"
        assert len(status.json()["runs"]) == 1

        refused = _post_instruction(client, second, body, INSTRUCTION_KEY)
        assert refused.status_code == 409, refused.text
        assert refused.json()["error"] == "session_state_conflict"
        assert refused.json()["details"]["reason"] == "session_lost"

        after = get_session(client, second, f"/v1/sessions/{SESSION_ID}").json()
        assert len(after["runs"]) == 1
        assert (
            sum(
                1
                for event in session_events(client, second)
                if event["kind"] == "instruction_delivered"
            )
            == 0
        )


def test_interrupt_in_awaiting_instruction_stops_directly(session_base: Path) -> None:
    with running_session_agent(session_base) as running, running.client() as client:
        workspace_id = running.ensure_workspace(client)
        assert _prepare(client, running, workspace_id).status_code == 200
        write_fake_script(running, RUN_SCRIPT)
        assert _start(client, running).status_code == 200
        _run_to_awaiting(client, running)

        interrupted = post_session(
            client,
            running,
            f"/v1/sessions/{SESSION_ID}/interrupt",
            {"reason": "stop the idle session"},
            INTERRUPT_KEY,
        )
        assert interrupted.status_code == 200, interrupted.text
        # The receipt literal stays the SESSION-RUNNER §6 one; the mirror state
        # transitions directly to stopped without a phantom interrupt request.
        assert interrupted.json() == {"state": "interrupt_requested", "signal_sent": False}
        status = get_session(client, running, f"/v1/sessions/{SESSION_ID}").json()
        assert status["state"] == "stopped"

        collected = post_session(
            client,
            running,
            f"/v1/sessions/{SESSION_ID}/collect",
            {"capture": False},
            COLLECT_KEY,
        )
        assert collected.status_code == 200, collected.text
        assert collected.json()["outcome"] == "interrupted"


def test_collect_and_close_accept_awaiting_instruction(session_base: Path) -> None:
    body = _instruction_body("capture the run after this instruction")
    with running_session_agent(session_base) as running, running.client() as client:
        workspace_id = running.ensure_workspace(client)
        assert _prepare(client, running, workspace_id).status_code == 200
        write_fake_script(running, RUN_SCRIPT)
        assert _start(client, running).status_code == 200
        _run_to_awaiting(client, running)
        assert _post_instruction(client, running, body, INSTRUCTION_KEY).status_code == 200
        _run_to_awaiting(client, running, runs=2)

        collected = post_session(
            client, running, f"/v1/sessions/{SESSION_ID}/collect", {"capture": True}, COLLECT_KEY
        )
        assert collected.status_code == 200, collected.text
        result = collected.json()
        assert result["outcome"] == "success"
        assert result["exit"] == {"code": 0, "signal": None}
        assert any(path.endswith("/finding.md") for path in result["captured_notes"])
        assert f"results/sessions/{SESSION_ID}/result.md" in {
            item["path"] for item in result["files"]
        }
        assert (
            get_session(client, running, f"/v1/sessions/{SESSION_ID}").json()["state"]
            == "awaiting_instruction"
        )

        session_dir = running.session_dir()
        assert (session_dir / "instruction-block.txt").is_file()
        closed = post_session(
            client,
            running,
            f"/v1/sessions/{SESSION_ID}/close",
            {"reason": "done"},
            CLOSE_KEY,
        )
        assert closed.status_code == 200, closed.text
        assert closed.json() == {"state": "closed"}
        assert not (session_dir / "instruction-block.txt").exists()
        assert not (session_dir / "prompt.json").exists()
        assert not (session_dir / "scratch").exists()
        assert not (session_dir / "ipc" / "shim.sock").exists()
        assert not (session_dir / "ipc" / "broker.sock").exists()


def test_instruction_block_is_bounded_and_truncated_explicitly() -> None:
    text = "note text"
    request = InstructionRequest(
        instruction_id=INSTRUCTION_ID,
        text=text,
        sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
    )
    block = _compose_instruction_block(request)
    assert f"instruction_id={INSTRUCTION_ID}" in block
    assert f"sha256={request.sha256}" in block
    assert block.endswith(text)

    oversized = "x" * (32 * 1024)
    long_request = InstructionRequest(
        instruction_id=SECOND_INSTRUCTION_ID,
        text=oversized,
        sha256=hashlib.sha256(oversized.encode("utf-8")).hexdigest(),
    )
    long_block = _compose_instruction_block(long_request)
    assert len(long_block.encode("utf-8")) <= MAX_INSTRUCTION_BLOCK_BYTES
    assert "instruction truncated:" in long_block
    assert "bytes omitted" in long_block
    assert f"instruction_id={SECOND_INSTRUCTION_ID}" in long_block


def _codex_prompt(tmp_path: Path) -> str:
    fake = _install_fake_codex(tmp_path)
    stub = _install_stub_bwrap(tmp_path)
    profile = CodexProfile(binary=fake, bwrap=stub)
    context = replace(_make_codex_context(tmp_path), instruction_block=_BLOCK)
    plan = profile.build_launch_plan(context)
    assert plan.stdin_data is not None
    return plan.stdin_data.decode("utf-8")


def _claude_prompt(tmp_path: Path) -> str:
    binary = write_fake_claude(tmp_path / "bin")
    bwrap = write_fake_bwrap(tmp_path / "bin")
    profile = ClaudeProfile(
        binary=binary,
        bwrap_binary=bwrap,
        confinement_probe=lambda: ConfinementProbe(
            ok=True,
            reason="mocked: userns unavailable in unit tests",
            mocked=True,
            network_mode="isolated_egress",
            loopback_isolated=True,
            netns=True,
        ),
    )
    context = replace(make_launch_context(tmp_path), instruction_block=_BLOCK)
    plan = profile.build_launch_plan(context)
    return claude_argv(plan.argv)[-1]


_BLOCK_TEXT = "apply the note revision 3d2c and close the pivot"
_BLOCK_SHA = hashlib.sha256(_BLOCK_TEXT.encode("utf-8")).hexdigest()
_BLOCK = _compose_instruction_block(
    InstructionRequest(
        instruction_id=INSTRUCTION_ID,
        text=_BLOCK_TEXT,
        sha256=_BLOCK_SHA,
    )
)


@pytest.mark.parametrize("factory", [_codex_prompt, _claude_prompt], ids=["codex", "claude"])
def test_instruction_block_reaches_both_adapters_as_untrusted_data(
    tmp_path: Path, factory: Callable[[Path], str]
) -> None:
    prompt = factory(tmp_path)
    assert INSTRUCTION_BLOCK_PREAMBLE in prompt
    assert _BLOCK in prompt
    assert f"instruction_id={INSTRUCTION_ID}" in prompt
    assert f"sha256={_BLOCK_SHA}" in prompt
    assert _BLOCK_TEXT in prompt
    # The adapter adds no host path and no secret to the untrusted section.
    assert str(tmp_path) not in _BLOCK
    assert "/home/" not in _BLOCK
    assert SECRET_B64 not in prompt and SECRET_HEX not in prompt
