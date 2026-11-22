"""Shared helpers for the M24-05 H5 Claude profile tests.

All helpers are offline: a generated shell wrapper execs the deterministic
fixture CLI, and a generated ``bwrap`` double prints the reason it is mocked
before exec'ing the tail of the argv. Nothing here calls a provider, a model or
the network.
"""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import sys
from pathlib import Path

from sidecars.workspace_agent.sessions.state import (
    BudgetSpec,
    HandoffRef,
    SessionRecord,
    SessionState,
)
from sidecars.workspace_agent.sessions.supervisor import LaunchContext

FIXTURES = Path(__file__).resolve().parent / "fixtures"
FAKE_CLAUDE_SCRIPT = FIXTURES / "fake_claude.py"
CLAUDE_HELP_FIXTURE = FIXTURES / "claude_help_2_1_284.txt"
CLAUDE_VERSION_FIXTURE = FIXTURES / "claude_version_2_1_284.txt"
CLAUDE_MCP_HELP_FIXTURE = FIXTURES / "claude_mcp_help_2_1_284.txt"

SESSION_ID = "5d3f0a6e-7c2b-4b8f-9a1d-2e4c6b8a0f13"
WORKSPACE_ID = "ws-00112233445566778899aabbccddeeff"
INVESTIGATION_ID = "3f6f2c1a-8a1e-4b7c-9d2f-1a2b3c4d5e6f"
TASK_ID = "a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d"
HANDOFF_ID = "0f1e2d3c-4b5a-4968-8776-655443322110"

MOCKED_BWRAP_REASON = "autowork-test: bwrap mocked; reason=userns-unavailable-in-tests"
DEFAULT_FINAL_MESSAGE = "fake claude final message"


def write_fake_claude(directory: Path) -> Path:
    """A real executable that runs the deterministic fixture CLI."""
    directory.mkdir(parents=True, exist_ok=True)
    launcher = directory / "claude"
    launcher.write_text(
        "#!/bin/sh\n"
        f"exec {shlex.quote(sys.executable)} -B "
        f'{shlex.quote(str(FAKE_CLAUDE_SCRIPT))} "$@"\n',
        encoding="utf-8",
    )
    launcher.chmod(0o755)
    return launcher


def write_fake_bwrap(directory: Path) -> Path:
    """A ``bwrap`` double that says, on stderr, why the real profile is mocked.

    Nothing is silent: the session log must contain ``MOCKED_BWRAP_REASON`` and
    the test asserts it. The tail after the profile's ``--`` is exec'd in place,
    so process-group semantics stay real.
    """
    directory.mkdir(parents=True, exist_ok=True)
    bwrap = directory / "fake-bwrap"
    bwrap.write_text(
        "#!/bin/sh\n"
        f"printf '%s\\n' {shlex.quote(MOCKED_BWRAP_REASON)} >&2\n"
        'while [ "$#" -gt 0 ] && [ "$1" != "--" ]; do shift; done\n'
        'if [ "$#" -gt 0 ]; then shift; fi\n'
        'exec "$@"\n',
        encoding="utf-8",
    )
    bwrap.chmod(0o755)
    return bwrap


def claude_argv(plan_argv: tuple[str, ...] | list[str]) -> list[str]:
    """The CLI tail of a bwrap launch plan."""
    values = list(plan_argv)
    assert "--" in values, values[:4]
    return values[values.index("--") + 1 :]


def flag_value(argv: list[str], flag: str) -> str:
    index = argv.index(flag)
    assert index + 1 < len(argv), argv
    return argv[index + 1]


def claude_flags(argv: list[str]) -> set[str]:
    return {token for token in argv if token.startswith("--")}


def default_prompt(session_id: str = SESSION_ID) -> bytes:
    return json.dumps(
        {
            "schema": "autowork-session-brief.v1",
            "session_id": session_id,
            "adapter_kind": "claude",
            "task_id": TASK_ID,
            "skills": [],
            "tool_rules": "Only the generated MCP tool catalogue is callable.",
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def make_session_record(*, model: str | None = "claude-model", prompt: bytes) -> SessionRecord:
    return SessionRecord(
        session_id=SESSION_ID,
        adapter_kind="claude",
        workspace_id=WORKSPACE_ID,
        investigation_id=INVESTIGATION_ID,
        task_id=TASK_ID,
        handoff=HandoffRef(handoff_id=HANDOFF_ID, version=1, sha256="b" * 64),
        lease_fence=7,
        allowed_workspace_paths=("notes/humaines", "results"),
        skill_names=(),
        model=model,
        prompt_sha256=hashlib.sha256(prompt).hexdigest(),
        capability_sha256="0" * 64,
        confinement={},
        budgets=BudgetSpec(wall_seconds=30, max_output_bytes=1_048_576, max_instructions=8),
        state=SessionState.READY,
        state_version=1,
        generation=1,
    )


def make_launch_context(
    base: Path,
    *,
    name: str = "ctx",
    resume: bool = False,
    model: str | None = "claude-model",
    prompt: bytes | None = None,
    session_id: str = SESSION_ID,
) -> LaunchContext:
    """A prepared-session layout with the real directories the profile writes into."""
    prompt_bytes = default_prompt(session_id) if prompt is None else prompt
    session_directory = base / name / "sessions" / session_id
    scratch = session_directory / "scratch"
    work = scratch / "work"
    config = session_directory / "config"
    ipc = session_directory / "ipc"
    for directory, mode in (
        (session_directory, 0o750),
        (scratch, 0o700),
        (work, 0o700),
        (config, 0o700),
        (ipc, 0o700),
    ):
        directory.mkdir(parents=True, exist_ok=True)
        os.chmod(directory, mode)
    prompt_path = session_directory / "prompt.json"
    prompt_path.write_bytes(prompt_bytes)
    os.chmod(prompt_path, 0o600)
    record = make_session_record(model=model, prompt=prompt_bytes)
    record.session_id = session_id
    return LaunchContext(
        session_id=session_id,
        record=record,
        session_directory=session_directory,
        scratch_directory=scratch,
        work_directory=work,
        home_directory=session_directory / "home",
        config_directory=config,
        ipc_socket_path=ipc / "shim.sock",
        prompt_path=prompt_path,
        run=1,
        resume=resume,
        fake_scenario="success",
    )
