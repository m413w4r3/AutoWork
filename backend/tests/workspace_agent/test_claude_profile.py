"""M24-05 H5 red oracles: the Claude Code profile (argv, probe, parser, resume).

Normative source: ``docs/design/investigation-workshop/SESSION-RUNNER.md`` §3,
§7, §8 and §11 (H5). Every flag asserted here must appear in the recorded real
``claude --help`` fixture (2.1.284); capabilities the help text cannot prove
are asserted ``proven=False`` with their conservative branch.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path

import pytest
from sidecars.workspace_agent.sessions import logs, process
from sidecars.workspace_agent.sessions.adapters.claude import (
    CLAUDE_HOST_SPIKE_CHECKS,
    DEFAULT_MAX_BUDGET_USD,
    MAX_FINAL_MESSAGE_BYTES,
    MAX_PROMPT_ARGV_BYTES,
    MAX_STREAM_LINE_BYTES,
    REQUIRED_HELP_FLAGS,
    ClaudeCapabilityProbe,
    ClaudeProfile,
    ClaudePromptUnavailableError,
    ClaudeStreamParser,
    ConfinementProbe,
    parse_help_flags,
)
from sidecars.workspace_agent.sessions.state import (
    CapabilityUnavailableError,
    ConfinementUnavailableError,
    ResumeUnavailableError,
)

from tests.workspace_agent.claude_profile_support import (
    CLAUDE_HELP_FIXTURE,
    CLAUDE_MCP_HELP_FIXTURE,
    CLAUDE_VERSION_FIXTURE,
    DEFAULT_FINAL_MESSAGE,
    claude_argv,
    claude_flags,
    default_prompt,
    flag_value,
    make_launch_context,
    write_fake_bwrap,
    write_fake_claude,
)

CLAUDE_HELP_SHA256 = "f12192d0e60545166396125ff47163f89bcfe4ed0a976771e7fb9bbf7e1ca486"
CLAUDE_VERSION_SHA256 = "3a5495706ad266f8675071284ae47afcf75aec014b4d93ad164de16cf4d01c42"
CLAUDE_MCP_HELP_SHA256 = "7119050de7984dc9d616f6b632fa3ee7fef92667eb5298ba57c73051e59cad46"
PROBED_AT = "2026-11-22T00:00:00Z"
SESSIONS_PACKAGE = Path(__file__).resolve().parents[3] / "sidecars" / "workspace_agent" / "sessions"


def _profile(
    tmp_path: Path,
    *,
    resume_proven: bool = False,
    bwrap_binary: Path | None = None,
    mcp_python: Path | None = None,
) -> tuple[ClaudeProfile, Path]:
    binary = write_fake_claude(tmp_path / "bin")
    bwrap = write_fake_bwrap(tmp_path / "bin") if bwrap_binary is None else bwrap_binary
    profile = ClaudeProfile(
        binary=binary,
        bwrap_binary=bwrap,
        resume_proven=resume_proven,
        mcp_python=mcp_python,
        confinement_probe=lambda: ConfinementProbe(
            ok=True,
            reason="mocked: userns unavailable in unit tests",
            mocked=True,
            network_mode="isolated_egress",
            loopback_isolated=True,
            netns=True,
        ),
    )
    return profile, binary


def test_claude_help_fixture_is_the_recorded_real_help_2_1_284() -> None:
    assert hashlib.sha256(CLAUDE_HELP_FIXTURE.read_bytes()).hexdigest() == CLAUDE_HELP_SHA256
    assert hashlib.sha256(CLAUDE_VERSION_FIXTURE.read_bytes()).hexdigest() == (
        CLAUDE_VERSION_SHA256
    )
    assert hashlib.sha256(CLAUDE_MCP_HELP_FIXTURE.read_bytes()).hexdigest() == (
        CLAUDE_MCP_HELP_SHA256
    )
    assert CLAUDE_VERSION_FIXTURE.read_text(encoding="utf-8").strip() == "2.1.284 (Claude Code)"
    help_text = CLAUDE_HELP_FIXTURE.read_text(encoding="utf-8")
    flags = parse_help_flags(help_text)
    assert set(REQUIRED_HELP_FLAGS) <= flags
    assert "--max-turns" not in flags  # verified absence: budgets stay external
    for value in ("stream-json", "dontAsk", "acceptEdits", 'choices: "host", "none"'):
        assert value in help_text, value
    mcp_help = CLAUDE_MCP_HELP_FIXTURE.read_text(encoding="utf-8")
    assert "add-json" in mcp_help and "stdio" in mcp_help


def test_claude_argv_uses_probed_modes_only(tmp_path: Path) -> None:
    profile, binary = _profile(tmp_path)
    context = make_launch_context(tmp_path)
    plan = profile.build_launch_plan(context)
    assert plan.uses_bwrap is True
    assert plan.cwd == context.work_directory
    argv = claude_argv(plan.argv)
    assert argv[0] == str(binary.resolve())
    flags = claude_flags(argv)
    probed = parse_help_flags(CLAUDE_HELP_FIXTURE.read_text(encoding="utf-8"))
    assert flags <= probed, sorted(flags - probed)

    assert "--print" in flags
    assert flag_value(argv, "--output-format") == "stream-json"
    assert flag_value(argv, "--max-budget-usd") == DEFAULT_MAX_BUDGET_USD
    assert flag_value(argv, "--model") == "claude-model"
    assert "--strict-mcp-config" in flags
    assert flag_value(argv, "--mcp-config").startswith(str(context.session_directory))
    assert argv[-1] == default_prompt().decode("utf-8")
    assert "--append-system-prompt" in flags
    assert "--no-session-persistence" in flags  # resume is unproven
    # §5 captures from the scratch root, so it must be an allowed file root.
    assert flag_value(argv, "--add-dir") == str(context.scratch_directory)
    assert context.scratch_directory == context.work_directory.parent

    # Modes the profile deliberately does not use (UNPROVEN or unnecessary).
    for absent in (
        "--input-format",
        "--resume",
        "--session-id",
        "--fork-session",
        "--json-schema",
        "--tools",
        "--allowedTools",
        "--settings",
        "--setting-sources",
        "--safe-mode",
        "--dangerously-skip-permissions",
        "--allow-dangerously-skip-permissions",
    ):
        assert absent not in flags, absent

    probe = profile.probe(now=PROBED_AT)
    assert probe.flags.non_interactive is True
    assert probe.flags.json_events is True
    assert probe.flags.streaming_stdin is False
    assert probe.flags.resume is False
    assert probe.flags.mcp_per_invocation is True
    assert probe.flags.os_sandbox is False

    assert plan.env == {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": str(context.config_directory / "home"),
        "XDG_CONFIG_HOME": str(context.config_directory / "home" / ".config"),
        "XDG_CACHE_HOME": str(context.config_directory / "cache"),
        "TMPDIR": "/tmp",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "DISABLE_AUTOUPDATER": "1",
        "AUTOWORK_SESSION_ID": context.session_id,
        "AUTOWORK_SESSION_SOCKET": str(context.ipc_socket_path),
        "AUTOWORK_BROKER_SOCKET": str(context.ipc_socket_path.parent / "broker.sock"),
    }
    for key in plan.env:
        assert not any(
            marker in key.upper() for marker in ("TOKEN", "SECRET", "PASSWORD", "APIKEY", "AUTH")
        ), key
    assert "CLAUDE_CONFIG_DIR" not in plan.env


def test_claude_restricted_tools_only(tmp_path: Path) -> None:
    profile, binary = _profile(tmp_path)
    context = make_launch_context(tmp_path)
    argv = claude_argv(profile.build_launch_plan(context).argv)
    flags = claude_flags(argv)

    assert "--restricted" in flags
    assert "--bare" in flags
    assert flag_value(argv, "--permission-mode") == "dontAsk"
    assert flag_value(argv, "--permission-prompts") == "none"
    denied = {item.strip() for item in flag_value(argv, "--disallowedTools").split(",")}
    assert {"Bash", "PowerShell", "REPL", "WebFetch"} <= denied
    assert "--allowedTools" not in flags  # --restricted's removal must stand
    assert "--tools" not in flags
    assert "--dangerously-skip-permissions" not in flags
    assert flag_value(argv, "--add-dir") == str(context.scratch_directory)
    assert not context.scratch_directory.is_relative_to(context.work_directory)

    config_path = Path(flag_value(argv, "--mcp-config"))
    assert config_path.is_relative_to(context.session_directory)
    assert not config_path.is_relative_to(context.work_directory)
    assert (config_path.stat().st_mode & 0o777) == 0o600
    document = json.loads(config_path.read_text(encoding="utf-8"))
    servers = document["mcpServers"]
    assert sorted(servers) == ["autowork"]  # strict-mcp-config: only the H2 shim
    server = servers["autowork"]
    assert server["command"].startswith("/")
    shim_copy = Path(server["args"][0])
    assert shim_copy.is_relative_to(context.config_directory)
    assert (shim_copy.stat().st_mode & 0o777) == 0o700
    assert shim_copy.read_bytes() == (SESSIONS_PACKAGE / "shim.py").read_bytes()
    assert sorted(server["env"]) == [
        "AUTOWORK_BROKER_SOCKET",
        "AUTOWORK_SESSION_ID",
        "AUTOWORK_SESSION_SOCKET",
    ]
    assert server["env"]["AUTOWORK_SESSION_SOCKET"] == str(context.ipc_socket_path)
    assert server["env"]["AUTOWORK_BROKER_SOCKET"] == str(
        context.ipc_socket_path.parent / "broker.sock"
    )
    serialized = config_path.read_text(encoding="utf-8")
    assert "secret" not in serialized.lower()
    assert str(binary) not in serialized
    assert (context.config_directory.stat().st_mode & 0o777) == 0o700
    assert (context.config_directory / "home").is_dir()
    assert (context.config_directory / "cache").is_dir()


def test_claude_uses_relay_socket_without_backend_url(tmp_path: Path) -> None:
    """NET-10: the shim reaches the broker over the ro-bound ipc socket only."""
    profile, _ = _profile(tmp_path)
    context = make_launch_context(tmp_path)
    plan = profile.build_launch_plan(context)

    broker_socket = context.ipc_socket_path.parent / "broker.sock"
    assert plan.env["AUTOWORK_BROKER_SOCKET"] == str(broker_socket)
    assert plan.env["AUTOWORK_SESSION_SOCKET"] == str(context.ipc_socket_path)
    # The provider proxy literal belongs to the trusted entrypoint, never to the
    # adapter: no proxy variable is set here, under any spelling.
    assert not any("proxy" in key.lower() for key in plan.env)
    assert not any("proxy" in value.lower() for value in plan.env.values())
    # The non-essential-traffic switches are the only Claude-specific addition.
    assert plan.env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] == "1"
    assert plan.env["DISABLE_AUTOUPDATER"] == "1"

    binds = [
        (plan.argv[index + 1], plan.argv[index + 2])
        for index in range(len(plan.argv) - 2)
        if plan.argv[index] == "--ro-bind"
    ]
    ipc = str(context.ipc_socket_path.parent)
    assert (ipc, ipc) in binds

    config_path = Path(flag_value(claude_argv(plan.argv), "--mcp-config"))
    serialized = config_path.read_text(encoding="utf-8")
    server = json.loads(serialized)["mcpServers"]["autowork"]
    assert sorted(server["env"]) == [
        "AUTOWORK_BROKER_SOCKET",
        "AUTOWORK_SESSION_ID",
        "AUTOWORK_SESSION_SOCKET",
    ]
    assert server["env"]["AUTOWORK_BROKER_SOCKET"] == str(broker_socket)

    produced = "\0".join((*plan.argv, *plan.env.keys(), *plan.env.values(), serialized))
    assert "AUTOWORK_BACKEND_URL" not in produced
    assert "http://" not in produced and "https://" not in produced
    assert "X-AutoWork-Agent-Signature" not in produced
    process.assert_cli_environment_safe(plan.env)


def test_claude_resume_session_id_roundtrip(tmp_path: Path) -> None:
    profile, _ = _profile(tmp_path, resume_proven=True)
    first_context = make_launch_context(tmp_path, name="first")
    first = claude_argv(profile.build_launch_plan(first_context).argv)
    assert flag_value(first, "--session-id") == first_context.session_id
    assert "--resume" not in first
    assert "--no-session-persistence" not in first

    resume_context = make_launch_context(tmp_path, name="second", resume=True)
    resumed = claude_argv(profile.build_launch_plan(resume_context).argv)
    assert flag_value(resumed, "--resume") == resume_context.session_id
    assert "--session-id" not in resumed
    assert "--no-session-persistence" not in resumed

    probed = parse_help_flags(CLAUDE_HELP_FIXTURE.read_text(encoding="utf-8"))
    assert {"--session-id", "--resume"} <= probed
    assert claude_flags(first) <= probed
    assert claude_flags(resumed) <= probed
    assert profile.probe(now=PROBED_AT).flags.resume is True

    # Without the explicit host-spike gate the resume branch fails closed.
    unproven, _ = _profile(tmp_path, resume_proven=False)
    with pytest.raises(ResumeUnavailableError):
        unproven.build_launch_plan(make_launch_context(tmp_path, name="gate", resume=True))
    assert unproven.probe(now=PROBED_AT).flags.resume is False


def test_claude_probe_is_real_hash_checked_and_bounded(tmp_path: Path) -> None:
    profile, binary = _profile(tmp_path)
    probe = profile.probe(now=PROBED_AT)
    assert isinstance(probe, ClaudeCapabilityProbe)
    wire = probe.to_wire()
    assert wire["protocol"] == "workspace-agent.sessions.v1"
    assert wire["adapter_kind"] == "claude"
    assert wire["version"] == "2.1.284 (Claude Code)"
    # Only a real execution of the fake CLI can produce these digests: the
    # profile never reads the help fixture itself.
    assert wire["help_sha256"] == CLAUDE_HELP_SHA256
    assert wire["binary_sha256"] == hashlib.sha256(binary.read_bytes()).hexdigest()
    assert wire["flags"] == {
        "non_interactive": True,
        "json_events": True,
        "structured_output": False,
        "resume": False,
        "streaming_stdin": False,
        "mcp_per_invocation": True,
        "os_sandbox": False,
        # WIRE-1: a fresh print-mode run carries the instruction block.
        "next_run_instruction": True,
    }
    assert wire["confinement"] == {
        "profile": "bwrap-claude",
        "bwrap_ok": True,
        "netns": True,
        "network_mode": "isolated_egress",
        "loopback_isolated": True,
        "network_reason": None,
        "reason": "mocked: userns unavailable in unit tests",
        "mocked": True,
    }
    capabilities = {entry["name"]: entry for entry in wire["capabilities"]}
    assert capabilities["print_mode"]["proven"] is True
    assert capabilities["stream_json_output"]["proven"] is True
    assert capabilities["max_budget_flag"]["proven"] is True
    for unproven in (
        "stream_json_live_instructions",
        "resume_session_id_roundtrip",
        "mcp_strict_shim_registration",
        "restricted_keeps_file_tools",
        "private_config_dir_env",
        "bare_provider_auth",
        "bwrap_namespaces",
        "stream_json_verbose_requirement",
    ):
        assert capabilities[unproven]["proven"] is False, unproven
        assert capabilities[unproven]["conservative_behaviour"], unproven
    assert wire["host_spike"] == list(CLAUDE_HOST_SPIKE_CHECKS)
    assert all(isinstance(check, str) and check for check in wire["host_spike"])
    assert len(wire["probe_sha256"]) == 64
    assert profile.probe(now=PROBED_AT).to_wire() == wire

    with pytest.raises(CapabilityUnavailableError):
        ClaudeProfile(binary=tmp_path / "missing-claude").probe(now=PROBED_AT)

    hang = tmp_path / "hang-claude"
    hang.write_text("#!/bin/sh\nsleep 30\n", encoding="utf-8")
    hang.chmod(0o755)
    with pytest.raises(CapabilityUnavailableError):
        ClaudeProfile(binary=hang, probe_timeout_seconds=0.2).probe(now=PROBED_AT)


def test_claude_bwrap_fails_closed_without_the_binary(tmp_path: Path) -> None:
    binary = write_fake_claude(tmp_path / "bin")
    profile = ClaudeProfile(binary=binary, bwrap_binary=tmp_path / "missing-bwrap")
    probe = profile.probe(now=PROBED_AT)
    assert probe.confinement["bwrap_ok"] is False
    # The profile still requests its own network namespace; without bwrap it
    # cannot attest the loopback isolation, so the backend fails closed.
    assert probe.confinement["netns"] is True
    assert probe.confinement["network_mode"] == "isolated_egress"
    assert probe.confinement["loopback_isolated"] is False
    assert probe.confinement["reason"] == "bwrap binary missing"
    assert str(tmp_path) not in str(probe.confinement["reason"])  # no host path on the wire
    with pytest.raises(ConfinementUnavailableError):
        profile.build_launch_plan(make_launch_context(tmp_path))
    # The real H1 binary probe is consulted when no bwrap path is injected.
    assert isinstance(process.bwrap_available(), bool)


def test_claude_prompt_larger_than_argv_budget_fails_closed(tmp_path: Path) -> None:
    profile, _ = _profile(tmp_path)
    oversized = b"{" + b"x" * MAX_PROMPT_ARGV_BYTES + b"}"
    with pytest.raises(ClaudePromptUnavailableError):
        profile.build_launch_plan(make_launch_context(tmp_path, prompt=oversized))


def test_claude_stream_parser_maps_typed_events_and_bounds_output() -> None:
    parser = ClaudeStreamParser()
    drafts = [
        parser.feed(line)
        for line in (
            b'{"type":"system","subtype":"init","session_id":"s"}',
            b'{"type":"assistant","message":{"role":"assistant","content":['
            b'{"type":"text","text":"working"},'
            b'{"type":"tool_use","id":"toolu_1","name":"autowork_task_heartbeat",'
            b'"input":{"note":"synthetic"}}]}}',
            b'{"type":"user","message":{"role":"user","content":['
            b'{"type":"tool_result","tool_use_id":"toolu_1","is_error":false,'
            b'"content":"ok"}]}}',
            json.dumps(
                {
                    "type": "result",
                    "subtype": "success",
                    "is_error": False,
                    "result": DEFAULT_FINAL_MESSAGE,
                }
            ).encode("utf-8"),
        )
    ]
    kept = [draft for draft in drafts if draft is not None]
    kinds = [draft.kind for draft in kept]
    assert logs.EventKind.TOOL_CALL_REQUESTED in kinds
    assert logs.EventKind.TOOL_CALL_COMPLETED in kinds
    requested = kept[kinds.index(logs.EventKind.TOOL_CALL_REQUESTED)]
    assert requested.payload["tool"] == "autowork_task_heartbeat"
    assert requested.payload["input_sha256"] == hashlib.sha256(b'{"note":"synthetic"}').hexdigest()
    assert "note" not in requested.payload  # tool prose never enters the event ring
    assert requested.payload["input_bytes"] > 0
    assert parser.final_message == DEFAULT_FINAL_MESSAGE
    assert parser.outcome == "success"
    assert parser.final_message_truncated is False

    assert parser.feed(b"{not json") is None
    assert parser.feed(b"[" * 10) is None
    assert parser.feed(b"x" * (MAX_STREAM_LINE_BYTES + 1)) is None
    assert parser.truncated_lines >= 1
    assert parser.feed(b'{"type":"assistant","message":{"content":[{"type":"tool_use"}]}}') is None

    oversized = json.dumps(
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": "y" * (MAX_FINAL_MESSAGE_BYTES + 1024),
        }
    ).encode("utf-8")
    parser.feed(oversized)
    assert parser.final_message is not None
    assert len(parser.final_message.encode("utf-8")) <= MAX_FINAL_MESSAGE_BYTES
    assert parser.final_message_truncated is True

    failed = ClaudeStreamParser()
    failed.feed(b'{"type":"result","subtype":"error_max_turns","is_error":true,"result":"no"}')
    assert failed.outcome == "failed"
    assert failed.final_message == "no"

    assert stat.S_ISDIR(SESSIONS_PACKAGE.stat().st_mode)
    assert os.access(CLAUDE_HELP_FIXTURE, os.R_OK)
