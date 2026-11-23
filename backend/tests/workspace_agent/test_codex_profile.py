"""M24-05 H4 oracles: the Codex profile argv/env/config, resume gate, no invented flags.

Every test is offline and rootless. The fake ``codex`` fixture answers the real
preflight surface by replaying the captured ``codex-cli 0.163.0`` help text and
runs a deterministic scripted turn; the stub ``bwrap`` executes the inner argv
without creating namespaces. The stub is named and hashed inside the probe
record, so a mocked sandbox is explicit and never silent. No model, auth file,
login or network call is made.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import os
import shutil
import socket
import stat
import tomllib
from pathlib import Path

import pytest
from sidecars.workspace_agent.sessions import process
from sidecars.workspace_agent.sessions import supervisor as sessions
from sidecars.workspace_agent.sessions.adapters.claude import ClaudeProfile, ConfinementProbe
from sidecars.workspace_agent.sessions.adapters.codex import (
    MAX_EVENT_DETAIL_CHARS,
    CodexProfile,
    parse_event_line,
    read_final_message,
    supervisor_event_kind,
)
from sidecars.workspace_agent.sessions.adapters.preamble import SYSTEM_PREAMBLE
from sidecars.workspace_agent.sessions.logs import EventKind
from sidecars.workspace_agent.sessions.state import (
    BudgetSpec,
    CapabilityUnavailableError,
    ConfinementUnavailableError,
    HandoffRef,
    ResumeUnavailableError,
    SessionRecord,
    SessionState,
)

from cti_app.application.agentic_host_sessions import (
    HostCapabilityProbeV1,
    HostPreparedSessionV1,
    HostSessionStatusV1,
)
from tests.workspace_agent import claude_profile_support
from tests.workspace_agent.sessions_support import (
    HANDOFF_ID,
    INVESTIGATION_ID,
    PREPARE_KEY,
    SESSION_ID,
    START_KEY,
    TASK_ID,
    get_session,
    post_session,
    prepare_payload,
    proc_alive,
    running_session_agent,
    session_events,
    wait_for_process_gone,
    wait_for_state,
)
from tests.workspace_agent.w3_support import ARTICLE_SLUG, DELIVERY_CODE

FIXTURES = Path(__file__).with_name("fixtures")
CODEX_HELP_FIXTURE = FIXTURES / "codex_help_0_163_0.txt"
FAKE_CODEX_SOURCE = FIXTURES / "fake_codex.py"
SESSIONS_PACKAGE = Path(__file__).resolve().parents[3] / "sidecars" / "workspace_agent" / "sessions"
WORKSPACE_ID = "ws-" + "1" * 32
MANIFEST_KEY = "m24-h4-manifest-0001"
RESUME_KEY = "m24-h4-resume-0001"
INTERRUPT_KEY = "m24-h4-interrupt-0001"
COLLECT_KEY = "m24-h4-collect-0001"
TRANSPORT_ENV_KEYS = frozenset(
    {"AUTOWORK_SESSION_ID", "AUTOWORK_SESSION_SOCKET", "AUTOWORK_BROKER_SOCKET"}
)
EXPECTED_ENV_KEYS = frozenset(
    {
        "PATH",
        "HOME",
        "XDG_CONFIG_HOME",
        "XDG_CACHE_HOME",
        "TMPDIR",
        "LANG",
        "LC_ALL",
        "CODEX_HOME",
        *TRANSPORT_ENV_KEYS,
    }
)


@pytest.fixture(autouse=True)
def _attested_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the real host loopback attestation; its own oracle proves it separately.

    The profile tests run against a named bwrap stand-in that creates no
    namespaces, so the real bwrap netns probe cannot apply.  The attestation
    function is replaced at the module seam, never through a production
    environment flag.
    """
    monkeypatch.setattr(
        process,
        "network_isolation_attestation",
        lambda **kwargs: process.NetworkIsolationAttestation(
            True, "test attestation", "isolated_egress", True
        ),
    )


@pytest.fixture(autouse=True)
def _egress_profile(
    monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory
) -> Path:
    """A real session needs its provider allowlist profile (M24.1 LIFE-1)."""
    directory = tmp_path_factory.mktemp("egress-profile")
    path = directory / "egress-profile.json"
    path.write_text(
        json.dumps(
            {
                "schema": "autowork-session-egress.v1",
                "profiles": {
                    "codex": {"hosts": ["api.openai.com", "auth.openai.com", "chatgpt.com"]}
                },
            }
        ),
        encoding="utf-8",
    )
    os.chmod(path, 0o600)
    monkeypatch.setenv("AGENT_SESSIONS_EGRESS_PROFILE_FILE", str(path))
    return path


# ---------------------------------------------------------------------------
# Fixtures: the fake codex executable and the explicit bwrap stub
# ---------------------------------------------------------------------------


def _install_fake_codex(base: Path, *, omit_flags: tuple[str, ...] = ()) -> Path:
    """Install the deterministic ``codex`` stand-in with the real help fixture."""
    bin_dir = base / "fake-codex-bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    target = bin_dir / "codex"
    shutil.copyfile(FAKE_CODEX_SOURCE, target)
    shutil.copyfile(CODEX_HELP_FIXTURE, bin_dir / CODEX_HELP_FIXTURE.name)
    target.chmod(0o755)
    mode_path = bin_dir / "fake-codex-mode.json"
    if omit_flags:
        mode_path.write_text(json.dumps({"omit_flags": list(omit_flags)}), encoding="utf-8")
    elif mode_path.exists():
        mode_path.unlink()
    return target


def _install_stub_bwrap(base: Path) -> Path:
    """A named stand-in that records its argv and execs the inner argv; it creates no namespaces."""
    path = base / "fake-bwrap.sh"
    path.write_text(
        "#!/bin/sh\n"
        "# Deterministic bwrap stand-in: record the mount options, exec the inner argv.\n"
        'log="$(dirname "$0")/fake-bwrap-invocations.log"\n'
        '{ printf \'%s\\n\' "$@" >> "$log"; printf -- \'---\\n\' >> "$log"; }\n'
        'while [ "$#" -gt 0 ]; do\n'
        '  if [ "$1" = "--" ]; then shift; exec "$@"; fi\n'
        "  shift\n"
        "done\n"
        "exit 64\n",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def _bwrap_invocations(base: Path) -> list[list[str]]:
    log = base / "fake-bwrap-invocations.log"
    if not log.exists():
        return []
    entries: list[list[str]] = []
    current: list[str] = []
    for line in log.read_text(encoding="utf-8").splitlines():
        if line == "---":
            entries.append(current)
            current = []
        else:
            current.append(line)
    return entries


def _fixture_section(command: str) -> str:
    """Return the captured ``$ codex <command>`` help section verbatim."""
    marker = f"$ codex {command}\n"
    text = CODEX_HELP_FIXTURE.read_text(encoding="utf-8")
    start = text.index(marker) + len(marker)
    following = text.find("\n$ codex ", start)
    return text[start:] if following < 0 else text[start : following + 1]


def _flags_in(text: str) -> set[str]:
    """Extract flag tokens from one captured ``--help`` section."""
    tokens: set[str] = set()
    for word in text.replace(",", " ").split():
        candidate = word.rstrip(".,")
        if candidate.startswith("--") and candidate[2:3].isalpha():
            tokens.add(candidate)
        elif len(candidate) == 2 and candidate.startswith("-") and candidate[1:2].isalpha():
            tokens.add(candidate)
    return tokens


def _session_record(*, model: str | None = "fake-model") -> SessionRecord:
    return SessionRecord(
        session_id=SESSION_ID,
        adapter_kind="codex",
        workspace_id=WORKSPACE_ID,
        investigation_id=INVESTIGATION_ID,
        task_id=TASK_ID,
        handoff=HandoffRef(handoff_id=HANDOFF_ID, version=3, sha256="b" * 64),
        lease_fence=7,
        allowed_workspace_paths=("notes/humaines", "results"),
        skill_names=(),
        model=model,
        prompt_sha256="a" * 64,
        capability_sha256="c" * 64,
        confinement={},
        budgets=BudgetSpec(wall_seconds=30, max_output_bytes=1_048_576, max_instructions=8),
        state=SessionState.READY,
        state_version=1,
        generation=1,
    )


def _make_context(base: Path, *, resume: bool = False) -> sessions.LaunchContext:
    session_directory = base / "run" / "sessions" / SESSION_ID
    for name in ("ipc", "scratch", "scratch/work", "home", "config", "cache", "tmp"):
        (session_directory / name).mkdir(parents=True, exist_ok=True)
    brief = {
        "schema": "autowork-session-brief.v1",
        "session_id": SESSION_ID,
        "task_id": TASK_ID,
        "handoff": {"handoff_id": HANDOFF_ID, "version": 3, "sha256": "b" * 64},
        "output_conventions": "notes/ and result.md",
    }
    prompt_path = session_directory / "prompt.json"
    prompt_path.write_text(json.dumps(brief, separators=(",", ":")), encoding="utf-8")
    (session_directory / "config" / "shim.py").write_text("# shim placeholder\n", encoding="utf-8")
    workspace_directory = base / "workspaces" / DELIVERY_CODE / ARTICLE_SLUG
    (workspace_directory / "notes" / "humaines").mkdir(parents=True, exist_ok=True)
    (workspace_directory / "results").mkdir(parents=True, exist_ok=True)
    return sessions.LaunchContext(
        session_id=SESSION_ID,
        record=_session_record(),
        session_directory=session_directory,
        scratch_directory=session_directory / "scratch",
        work_directory=session_directory / "scratch" / "work",
        home_directory=session_directory / "home",
        config_directory=session_directory / "config",
        ipc_socket_path=session_directory / "ipc" / "shim.sock",
        prompt_path=prompt_path,
        run=1,
        resume=resume,
        fake_scenario="success",
        workspace_directory=workspace_directory,
        skills_directory=None,
    )


def _after_bwrap_separator(argv: tuple[str, ...]) -> list[str]:
    return list(argv[argv.index("--") + 1 :])


def _argv_flags(argv: list[str]) -> set[str]:
    return {token for token in argv if token.startswith("-") and token != "-"}


# ---------------------------------------------------------------------------
# Red oracles named by the H4 lot
# ---------------------------------------------------------------------------


def test_codex_argv_uses_probed_modes_only(tmp_path: Path) -> None:
    fake = _install_fake_codex(tmp_path)
    stub = _install_stub_bwrap(tmp_path)
    profile = CodexProfile(binary=fake, bwrap=stub)
    probe = profile.probe(now="2026-11-22T00:00:00Z")
    assert probe.adapter_kind == "codex"
    assert probe.version == "0.163.0"
    assert probe.flags.non_interactive is True
    assert probe.flags.json_events is True
    assert probe.flags.resume is False  # resume re-arming is UNPROVEN by default
    assert probe.flags.streaming_stdin is False
    assert probe.flags.mcp_per_invocation is False
    # WIRE-1: the profile composes the instruction block into a fresh run, so
    # the host advertises the next-run delivery capability to the backend.
    assert probe.flags.next_run_instruction is True

    context = _make_context(tmp_path)
    plan = profile.build_launch_plan(context)
    assert plan.uses_bwrap is True
    cli = _after_bwrap_separator(plan.argv)
    assert cli[0] == str(fake.resolve())
    assert cli[:2] == [str(fake.resolve()), "exec"]
    assert "resume" not in cli
    assert "--json" in cli
    assert "--ephemeral" in cli  # the conservative no-persistence default
    assert "-C" in cli
    assert "--add-dir" not in cli
    assert "-s" not in cli
    assert cli[-1] == "-"  # prompt is read from the supervisor-fed stdin
    exec_flags = _flags_in(_fixture_section("exec --help"))
    assert _argv_flags(cli) <= exec_flags, sorted(_argv_flags(cli) - exec_flags)

    # A binary that does not print every required flag is refused before argv exists.
    reduced = _install_fake_codex(tmp_path / "reduced", omit_flags=("--json",))
    with pytest.raises(CapabilityUnavailableError):
        CodexProfile(binary=reduced, bwrap=stub).probe(now="2026-11-22T00:00:00Z")


def test_codex_resume_never_relaunches_without_probe(
    session_base: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _install_fake_codex(session_base)
    stub = _install_stub_bwrap(session_base)
    profile = CodexProfile(binary=fake, bwrap=stub)
    context = _make_context(tmp_path, resume=True)
    with pytest.raises(ResumeUnavailableError):
        profile.build_launch_plan(context)

    monkeypatch.setenv("AUTOWORK_CODEX_BINARY", str(fake))
    monkeypatch.setenv("AUTOWORK_CODEX_BWRAP", str(stub))
    monkeypatch.delenv("AUTOWORK_CODEX_RESUME_PROVEN", raising=False)
    with running_session_agent(session_base) as running, running.client() as client:
        workspace_id = running.ensure_workspace(client)
        prepared = _prepare_codex(client, running, workspace_id)
        assert prepared.status_code == 200, prepared.text
        assert _post_manifest(client, running).status_code == 200
        _write_scenario(running, {"outcome": "success", "final_message": "first turn"})
        started = post_session(client, running, f"/v1/sessions/{SESSION_ID}/start", {}, START_KEY)
        assert started.status_code == 200, started.text
        wait_for_state(client, running, "awaiting_instruction")

        response = post_session(
            client,
            running,
            f"/v1/sessions/{SESSION_ID}/resume",
            {"reason": "no probe"},
            RESUME_KEY,
        )
        assert response.status_code == 409, response.text
        assert response.json()["error"] == "resume_unavailable"
        status = get_session(client, running, f"/v1/sessions/{SESSION_ID}").json()
        assert status["state"] == "awaiting_instruction"
        assert len(status["runs"]) == 1


def test_resume_unprints_no_invented_flags(tmp_path: Path) -> None:
    fake = _install_fake_codex(tmp_path)
    stub = _install_stub_bwrap(tmp_path)
    profile = CodexProfile(binary=fake, bwrap=stub, resume_proven=True)
    probe = profile.probe(now="2026-11-22T00:00:00Z")
    assert probe.flags.resume is True

    exec_plan = profile.build_launch_plan(_make_context(tmp_path))
    resume_plan = profile.build_launch_plan(_make_context(tmp_path, resume=True))
    exec_cli = _after_bwrap_separator(exec_plan.argv)
    resume_cli = _after_bwrap_separator(resume_plan.argv)
    assert "--ephemeral" not in exec_cli  # a resumable session must persist
    assert resume_cli[:3] == [str(fake.resolve()), "exec", "resume"]
    assert "--last" in resume_cli
    assert "-C" not in resume_cli  # `exec resume --help` does not print -C/--cd
    assert resume_cli[-1] == "-"
    exec_flags = _flags_in(_fixture_section("exec --help"))
    resume_flags = _flags_in(_fixture_section("exec resume --help"))
    assert _argv_flags(exec_cli) <= exec_flags, sorted(_argv_flags(exec_cli) - exec_flags)
    assert _argv_flags(resume_cli) <= resume_flags, sorted(_argv_flags(resume_cli) - resume_flags)


# ---------------------------------------------------------------------------
# Profile contract details
# ---------------------------------------------------------------------------


def test_codex_probe_records_unproven_capabilities(tmp_path: Path) -> None:
    fake = _install_fake_codex(tmp_path)
    stub = _install_stub_bwrap(tmp_path)
    profile = CodexProfile(binary=fake, bwrap=stub)
    probe = profile.probe(now="2026-11-22T00:00:00Z")
    capabilities = probe.confinement["capabilities"]
    assert isinstance(capabilities, dict)
    assert capabilities["output_last_message_flag"]["proven"] is True
    for name in (
        "mcp_config_toml_schema",
        "mcp_exec_tool_exposure",
        "jsonl_event_schema",
        "final_message_content",
        "provider_credentials",
        "resume_rearming",
        "live_instruction",
        "cli_interrupt",
    ):
        assert capabilities[name]["proven"] is False, name
    assert probe.confinement["bwrap_ok"] is True
    # R03 B1: the profile requests its own network namespace and the host must
    # attest the loopback isolation; the stand-in cannot, so the test seam above
    # supplies the attestation.
    assert probe.confinement["netns"] is True
    assert probe.confinement["network_mode"] == "isolated_egress"
    assert probe.confinement["loopback_isolated"] is True
    # The stub is named by hash: the mocked sandbox is never silent.
    assert probe.confinement["bwrap_sha256"] == hashlib.sha256(stub.read_bytes()).hexdigest()
    # probed_at varies but the capability digest is stable for the same facts,
    # which the backend compares between its probe and the host prepare.
    later = profile.probe(now="2026-11-22T01:02:03Z")
    assert later.probe_sha256 == probe.probe_sha256


def test_codex_prepare_fails_closed_without_bwrap(
    session_base: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _install_fake_codex(session_base)
    monkeypatch.setenv("AUTOWORK_CODEX_BINARY", str(fake))
    monkeypatch.setenv("AUTOWORK_CODEX_BWRAP", str(session_base / "missing-bwrap"))
    with running_session_agent(session_base) as running, running.client() as client:
        workspace_id = running.ensure_workspace(client)
        response = _prepare_codex(client, running, workspace_id)
        assert response.status_code == 503, response.text
        payload = response.json()
        assert payload["error"] == "confinement_unavailable"
        assert payload["details"]["reason"]
        assert not running.session_dir().exists()


def test_codex_env_is_closed_and_credentials_stay_private(tmp_path: Path) -> None:
    fake = _install_fake_codex(tmp_path)
    stub = _install_stub_bwrap(tmp_path)
    auth_source = tmp_path / "auth-source.json"
    auth_source.write_text('{"token": "synthetic-provider-token"}\n', encoding="utf-8")
    profile = CodexProfile(binary=fake, bwrap=stub, auth_file=auth_source)
    context = _make_context(tmp_path)
    plan = profile.build_launch_plan(context)

    assert set(plan.env) == EXPECTED_ENV_KEYS
    assert plan.env["CODEX_HOME"] == str(context.config_directory / "codex")
    assert plan.env["HOME"] == str(context.home_directory)
    joined = "\0".join(plan.argv) + "\0" + "\0".join(plan.env.values())
    assert plan.stdin_data is not None
    joined += "\0" + plan.stdin_data.decode("utf-8")
    assert "synthetic-provider-token" not in joined

    auth_copy = context.config_directory / "codex" / "auth.json"
    assert auth_copy.read_bytes() == auth_source.read_bytes()
    assert stat.S_IMODE(auth_copy.stat().st_mode) == 0o600
    config_toml = (context.config_directory / "codex" / "config.toml").read_text(encoding="utf-8")
    assert "[mcp_servers.autowork]" in config_toml
    assert str(context.config_directory / "shim.py") in config_toml
    assert "AUTOWORK_SESSION_SOCKET" in config_toml
    assert "AUTOWORK_BROKER_SOCKET" in config_toml
    assert "--ignore-user-config" not in _after_bwrap_separator(plan.argv)


def test_codex_uses_relay_socket_without_backend_url(tmp_path: Path) -> None:
    """NET-10: the shim reaches the broker over the ro-bound ipc socket only."""
    fake = _install_fake_codex(tmp_path)
    stub = _install_stub_bwrap(tmp_path)
    profile = CodexProfile(binary=fake, bwrap=stub)
    context = _make_context(tmp_path)
    plan = profile.build_launch_plan(context)

    broker_socket = context.ipc_socket_path.parent / "broker.sock"
    assert plan.env["AUTOWORK_BROKER_SOCKET"] == str(broker_socket)
    assert plan.env["AUTOWORK_SESSION_SOCKET"] == str(context.ipc_socket_path)

    # build_bwrap_argv mounts the ipc directory at its own absolute path, so the
    # path the supervisor binds is also the path the sandboxed shim sees.
    binds = [
        (plan.argv[index + 1], plan.argv[index + 2])
        for index in range(len(plan.argv) - 2)
        if plan.argv[index] == "--ro-bind"
    ]
    ipc = str(context.ipc_socket_path.parent)
    assert (ipc, ipc) in binds

    config_toml = (context.config_directory / "codex" / "config.toml").read_text(encoding="utf-8")
    server = tomllib.loads(config_toml)["mcp_servers"]["autowork"]
    assert sorted(server["env"]) == sorted(TRANSPORT_ENV_KEYS)
    assert server["env"]["AUTOWORK_BROKER_SOCKET"] == str(broker_socket)

    assert plan.stdin_data is not None
    produced = "\0".join(
        (*plan.argv, *plan.env.keys(), *plan.env.values(), config_toml, plan.stdin_data.decode())
    )
    assert "AUTOWORK_BACKEND_URL" not in produced
    assert "AUTOWORK_SHIM_TEST_BACKEND_URL" not in produced
    assert "http://" not in produced and "https://" not in produced
    assert not any("proxy" in key.lower() for key in plan.env)
    process.assert_cli_environment_safe(plan.env)


@pytest.mark.parametrize("adapter_kind", ["codex", "claude"])
def test_real_adapters_share_the_relay_transport_contract(
    adapter_kind: str, tmp_path: Path
) -> None:
    """NET-10: same socket variable and same shim catalogue, different flags."""
    if adapter_kind == "codex":
        fake = _install_fake_codex(tmp_path)
        stub = _install_stub_bwrap(tmp_path)
        profile: CodexProfile | ClaudeProfile = CodexProfile(binary=fake, bwrap=stub)
        context = _make_context(tmp_path)
        plan = profile.build_launch_plan(context)
        document = tomllib.loads(
            (context.config_directory / "codex" / "config.toml").read_text(encoding="utf-8")
        )
        server = document["mcp_servers"]["autowork"]
    else:
        profile = ClaudeProfile(
            binary=claude_profile_support.write_fake_claude(tmp_path / "bin"),
            bwrap_binary=claude_profile_support.write_fake_bwrap(tmp_path / "bin"),
            confinement_probe=lambda: ConfinementProbe(
                ok=True,
                reason="contract test",
                mocked=True,
                network_mode="isolated_egress",
                loopback_isolated=True,
                netns=True,
            ),
        )
        context = claude_profile_support.make_launch_context(tmp_path / "claude")
        plan = profile.build_launch_plan(context)
        document = json.loads(
            (context.config_directory / "claude-mcp.json").read_text(encoding="utf-8")
        )
        server = document["mcpServers"]["autowork"]

    assert TRANSPORT_ENV_KEYS <= set(plan.env)
    assert sorted(server["env"]) == sorted(TRANSPORT_ENV_KEYS)
    for key in TRANSPORT_ENV_KEYS:
        assert server["env"][key] == plan.env[key], key
    assert Path(plan.env["AUTOWORK_BROKER_SOCKET"]).parent == context.ipc_socket_path.parent
    assert Path(plan.env["AUTOWORK_BROKER_SOCKET"]).name == "broker.sock"
    assert Path(server["args"][0]) == context.config_directory / "shim.py"
    assert Path(server["command"]).is_absolute()
    # Both adapters register the same shim module, hence the same closed
    # `catalog.CATALOGUE`; neither invents a second MCP server.
    assert Path(server["args"][0]).read_bytes() == (SESSIONS_PACKAGE / "shim.py").read_bytes()
    # H0: the shim imports its catalogue as a sibling; a lone shim.py fails at
    # CLI startup, so the adapter installs both files it names.
    catalog_copy = context.config_directory / "catalog.py"
    assert catalog_copy.read_bytes() == (SESSIONS_PACKAGE / "catalog.py").read_bytes()
    assert catalog_copy.stat().st_mode & 0o777 == 0o700

    synthetic_host_etc = tmp_path / "synthetic-host" / "etc"
    synthetic_host_etc.mkdir(parents=True)
    sentinel = "tailnet.r2b4.invalid"
    (synthetic_host_etc / "hosts").write_text(
        f"127.0.0.1 localhost {sentinel}\n", encoding="utf-8"
    )
    (synthetic_host_etc / "resolv.conf").write_text(
        f"search {sentinel}\nnameserver 192.0.2.53\n", encoding="utf-8"
    )
    session_etc = context.session_directory / process.SESSION_ETC_DIRECTORY_NAME
    assert (session_etc / "hosts").read_bytes() == b"127.0.0.1 localhost\n::1 localhost\n"
    assert (session_etc / "resolv.conf").read_bytes() == b""
    assert (session_etc / "nsswitch.conf").read_bytes() == b"hosts: files\n"
    assert stat.S_IMODE(session_etc.stat().st_mode) == 0o700
    for name, _contents in process.SESSION_ETC_FILES:
        assert stat.S_IMODE((session_etc / name).stat().st_mode) == 0o600
    binds = [
        (Path(plan.argv[index + 1]), Path(plan.argv[index + 2]))
        for index in range(len(plan.argv) - 2)
        if plan.argv[index] == "--ro-bind"
    ]
    assert all(pair in binds for pair in process.session_etc_ro_binds(session_etc))
    assert all(pair in binds for pair in process.tls_trust_anchor_binds())
    assert not any(
        target in {
            Path("/etc/ssl"),
            Path("/etc/ca-certificates"),
            Path("/etc/pki"),
            Path("/etc/localtime"),
            Path("/etc/host.conf"),
        }
        for _source, target in binds
    )
    assert (session_etc, session_etc) not in binds
    assert not any(
        source in {synthetic_host_etc / "hosts", synthetic_host_etc / "resolv.conf"}
        for source, _target in binds
    )
    assert sentinel not in "\0".join(plan.argv)

    egress_socket = context.ipc_socket_path.parent / process.EGRESS_SOCKET_FILE_NAME
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        listener.bind(str(egress_socket))
        wrapped = process.isolated_entry_argv(plan.argv, egress_socket_path=egress_socket)
    finally:
        listener.close()
    forwarder_index = wrapped.index("forward-and-exec")
    assert wrapped[forwarder_index + 1 : forwarder_index + 3] == [
        "--upstream",
        str(egress_socket),
    ]
    assert "--unshare-net" in wrapped
    assert "AUTOWORK_BACKEND_URL" not in plan.env
    assert "AUTOWORK_BACKEND_URL" not in server["env"]
    assert "AUTOWORK_SHIM_TEST_BACKEND_URL" not in plan.env
    assert "AUTOWORK_SHIM_TEST_BACKEND_URL" not in server["env"]
    assert not any("proxy" in key.lower() for key in plan.env)
    process.assert_cli_environment_safe(plan.env)
    # No constructor knob can redirect the transport or widen the catalogue.
    parameters = inspect.signature(type(profile)).parameters
    assert "broker_socket" not in parameters
    assert "backend_url" not in parameters


def test_codex_installs_the_exact_shim_and_catalogue(tmp_path: Path) -> None:
    """H0: the generated config names files that must exist at CLI startup.

    The shim imports ``catalog`` as a sibling; a config that names only a
    non-installed (or placeholder) ``shim.py`` leaves ``codex exec`` with a
    dead MCP server.  The adapter installs both exact modules, 0700.
    """
    fake = _install_fake_codex(tmp_path)
    stub = _install_stub_bwrap(tmp_path)
    profile = CodexProfile(binary=fake, bwrap=stub)
    context = _make_context(tmp_path)
    assert (context.config_directory / "shim.py").read_text(encoding="utf-8") == (
        "# shim placeholder\n"
    )
    profile.build_launch_plan(context)
    for name in ("shim.py", "catalog.py"):
        copied = context.config_directory / name
        assert copied.is_file()
        assert copied.read_bytes() == (SESSIONS_PACKAGE / name).read_bytes()
        assert copied.stat().st_mode & 0o777 == 0o700
    document = tomllib.loads(
        (context.config_directory / "codex" / "config.toml").read_text(encoding="utf-8")
    )
    assert Path(document["mcp_servers"]["autowork"]["args"][0]) == (
        context.config_directory / "shim.py"
    )


def test_codex_binds_only_certificate_anchors_and_private_etc_files(tmp_path: Path) -> None:
    """Keep the provider CA store while replacing host resolver configuration."""
    fake = _install_fake_codex(tmp_path)
    stub = _install_stub_bwrap(tmp_path)
    profile = CodexProfile(binary=fake, bwrap=stub)
    context = _make_context(tmp_path)
    plan = profile.build_launch_plan(context)
    argv = list(plan.argv)
    binds = [
        (Path(argv[index + 1]), Path(argv[index + 2]))
        for index in range(len(argv) - 2)
        if argv[index] == "--ro-bind"
    ]
    tls_binds = process.tls_trust_anchor_binds()
    assert tls_binds
    assert all(pair in binds for pair in tls_binds)
    assert not any(
        target in {Path("/etc/ssl"), Path("/etc/pki"), Path("/etc/ca-certificates")}
        for _source, target in binds
    )
    assert not any(
        target in {Path("/etc/hosts"), Path("/etc/resolv.conf")} and source == target
        for source, target in binds
    )
    session_etc = context.session_directory / process.SESSION_ETC_DIRECTORY_NAME
    assert all(pair in binds for pair in process.session_etc_ro_binds(session_etc))
    assert not any(target == Path("/etc/host.conf") for _source, target in binds)


def test_codex_prompt_carries_preamble_and_handoff_reference(tmp_path: Path) -> None:
    fake = _install_fake_codex(tmp_path)
    stub = _install_stub_bwrap(tmp_path)
    profile = CodexProfile(binary=fake, bwrap=stub)
    context = _make_context(tmp_path)
    plan = profile.build_launch_plan(context)
    assert plan.stdin_data is not None
    prompt = plan.stdin_data.decode("utf-8")
    assert prompt.startswith(SYSTEM_PREAMBLE)
    assert HANDOFF_ID in prompt
    assert "version=3" in prompt
    assert "G2/G3" in prompt
    assert "untrusted" in prompt.lower()

    handoff = context.config_directory / "handoff.json"
    content = b'{"objective": "synthetic"}\n'
    handoff.write_bytes(content)
    context.record.handoff = HandoffRef(
        handoff_id=HANDOFF_ID, version=3, sha256=hashlib.sha256(content).hexdigest()
    )
    with_handoff = profile.build_launch_plan(context)
    assert with_handoff.stdin_data is not None
    received = with_handoff.stdin_data.decode("utf-8")
    assert "config/handoff.json" in received
    assert hashlib.sha256(content).hexdigest() in received

    # F10: ``config/`` is writable from the sandbox.  A file that does not
    # match the versioned handoff sha256 is omitted and never quoted.
    handoff.write_bytes(b'{"objective": "tampered by a previous run"}\n')
    mismatched = profile.build_launch_plan(context)
    assert mismatched.stdin_data is not None
    received = mismatched.stdin_data.decode("utf-8")
    assert "handoff content mismatch, omitted" in received
    assert "tampered by a previous run" not in received
    assert "The versioned handoff content is provided read-only" not in received


def test_codex_fails_closed_when_bwrap_is_unavailable(tmp_path: Path) -> None:
    fake = _install_fake_codex(tmp_path)
    missing = tmp_path / "missing-bwrap"
    profile = CodexProfile(binary=fake, bwrap=missing)
    probe = profile.probe(now="2026-11-22T00:00:00Z")
    assert probe.confinement["bwrap_ok"] is False
    assert isinstance(probe.confinement["bwrap_reason"], str)
    with pytest.raises(ConfinementUnavailableError) as raised:
        profile.build_launch_plan(_make_context(tmp_path))
    assert raised.value.details.get("reason")


def test_codex_event_parser_is_tolerant_and_bounded() -> None:
    event = parse_event_line(b'{"type": "turn.completed", "usage": {"input_tokens": 3}}\n')
    assert event is not None
    assert event.kind == "turn.completed"
    assert supervisor_event_kind(event) == EventKind.OUTPUT_CHUNK
    assert parse_event_line(b"not json at all\n") is None
    assert parse_event_line(b"[1, 2, 3]\n") is None
    assert parse_event_line(b"   \n") is None

    oversized = json.dumps({"type": "item.completed", "text": "x" * 200_000}).encode()
    bounded = parse_event_line(oversized)
    assert bounded is not None
    assert len(bounded.detail) <= MAX_EVENT_DETAIL_CHARS


def test_read_final_message_refuses_links_and_oversize(tmp_path: Path) -> None:
    final = tmp_path / "final-message.txt"
    final.write_text("codex final message", encoding="utf-8")
    assert read_final_message(final) == "codex final message"
    link = tmp_path / "link.txt"
    link.symlink_to(final)
    assert read_final_message(link) is None
    assert read_final_message(tmp_path / "absent.txt") is None
    final.write_text("x" * 32, encoding="utf-8")
    assert read_final_message(final, limit=8) is None


# ---------------------------------------------------------------------------
# End-to-end through the H1 supervisor (fake codex + explicit bwrap stub)
# ---------------------------------------------------------------------------


def test_codex_profile_lifecycle_through_supervisor(
    session_base: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _install_fake_codex(session_base)
    stub = _install_stub_bwrap(session_base)
    monkeypatch.setenv("AUTOWORK_CODEX_BINARY", str(fake))
    monkeypatch.setenv("AUTOWORK_CODEX_BWRAP", str(stub))
    monkeypatch.delenv("AUTOWORK_CODEX_AUTH_FILE", raising=False)
    monkeypatch.delenv("AUTOWORK_CODEX_RESUME_PROVEN", raising=False)
    with running_session_agent(session_base) as running, running.client() as client:
        workspace_id = running.ensure_workspace(client)
        prepared = _prepare_codex(client, running, workspace_id)
        assert prepared.status_code == 200, prepared.text
        body = prepared.json()
        confinement = body["confinement"]
        assert confinement["profile"] == "bwrap-reasoning-session"
        assert confinement["bwrap_ok"] is True
        assert confinement["bwrap_sha256"] == hashlib.sha256(stub.read_bytes()).hexdigest()
        assert _post_manifest(client, running).status_code == 200
        _write_scenario(
            running,
            {
                "outcome": "success",
                "final_message": "codex fake final message",
                "note_text": "# agent note\n\nsynthetic\n",
            },
        )

        started = post_session(client, running, f"/v1/sessions/{SESSION_ID}/start", {}, START_KEY)
        assert started.status_code == 200, started.text
        status = wait_for_state(client, running, "awaiting_instruction")
        assert status["exit"] == {"code": 0, "signal": None}
        assert len(status["runs"]) == 1

        observation = _last_observation(running)
        argv = observation["argv"]
        assert isinstance(argv, list)
        # The stub execs the inner argv, so the CLI sees exactly the profile argv.
        cli = [str(item) for item in argv]
        assert cli[:2] == [str(fake.resolve()), "exec"]
        assert "--ephemeral" in cli
        stdin_bytes = observation["stdin_bytes"]
        assert isinstance(stdin_bytes, int) and stdin_bytes >= len(SYSTEM_PREAMBLE)
        assert observation["mode"] == "exec"
        invocations = _bwrap_invocations(session_base)
        launched = [entry for entry in invocations if "exec" in entry and "--json" in entry]
        assert len(launched) == 1, invocations
        assert "--unshare-pid" in launched[0]
        # Isolation is the only mode (M24.1 LIFE-1); the named bwrap stand-in
        # execs the trusted entrypoint wrapper without creating the namespace,
        # and the host attestation is injected above.
        assert "--unshare-net" in launched[0]
        assert any("entrypoint.py" in token for token in launched[0])
        assert "--dangerously-bypass-approvals-and-sandbox" in launched[0]

        final_path = running.session_dir() / "scratch" / "work" / "final-message.txt"
        assert read_final_message(final_path) == "codex fake final message"

        collected = post_session(
            client,
            running,
            f"/v1/sessions/{SESSION_ID}/collect",
            {"capture": True},
            COLLECT_KEY,
        )
        assert collected.status_code == 200, collected.text
        result = collected.json()
        assert f"notes/agents/{TASK_ID}/agent-note.md" in result["captured_notes"]
        paths = {item["path"] for item in result["files"]}
        assert f"results/sessions/{SESSION_ID}/result.md" in paths
        kinds = [event["kind"] for event in session_events(client, running)]
        assert "process_started" in kinds
        assert "process_exited" in kinds


def test_codex_cli_wire_kind_selects_the_codex_profile_and_egress(
    session_base: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F5: the backend's exact ``OperatorAdapterKind`` value reaches the profile.

    The host resolves ``codex_cli`` once at the entry point, records/selects
    the canonical ``codex`` profile and its egress allowlist, and echoes the
    wire name in the probe/status responses the backend validates.
    """
    fake = _install_fake_codex(session_base)
    stub = _install_stub_bwrap(session_base)
    monkeypatch.setenv("AUTOWORK_CODEX_BINARY", str(fake))
    monkeypatch.setenv("AUTOWORK_CODEX_BWRAP", str(stub))
    monkeypatch.delenv("AUTOWORK_CODEX_AUTH_FILE", raising=False)
    monkeypatch.delenv("AUTOWORK_CODEX_RESUME_PROVEN", raising=False)
    with running_session_agent(session_base) as running, running.client() as client:
        workspace_id = running.ensure_workspace(client)
        probe = get_session(client, running, "/v1/sessions/capabilities", adapter_kind="codex_cli")
        assert probe.status_code == 200, probe.text
        wire = probe.json()
        assert wire["adapter_kind"] == "codex_cli"
        HostCapabilityProbeV1.model_validate_json(probe.content)

        prepared = post_session(
            client,
            running,
            "/v1/sessions",
            prepare_payload(workspace_id, adapter_kind="codex_cli", network_mode="isolated_egress"),
            "m24-r2fixa-prepare-codex-1",
        )
        assert prepared.status_code == 200, prepared.text
        HostPreparedSessionV1.model_validate_json(prepared.content)
        assert prepared.json()["confinement"]["profile"] == "bwrap-reasoning-session"

        status = get_session(client, running, f"/v1/sessions/{SESSION_ID}")
        assert status.status_code == 200, status.text
        body = status.json()
        assert body["adapter_kind"] == "codex_cli"
        HostSessionStatusV1.model_validate_json(status.content)

        # Record, profile and egress allowlist are canonical: the fixture
        # egress profile declares only "codex", so a wire-named load would
        # have refused prepare with confinement_unavailable.
        persisted = json.loads((running.session_dir() / "state.json").read_text(encoding="utf-8"))
        assert persisted["adapter_kind"] == "codex"

        # Only the two closed aliases exist; a canonical name still works for
        # direct host callers, and anything else stays refused.
        canonical = get_session(client, running, "/v1/sessions/capabilities", adapter_kind="codex")
        assert canonical.status_code == 200
        assert canonical.json()["adapter_kind"] == "codex"
        unknown = get_session(
            client, running, "/v1/sessions/capabilities", adapter_kind="codex_cli_v2"
        )
        assert unknown.status_code == 409
        assert unknown.json()["error"] == "capability_unavailable"


def test_codex_profile_interrupt_kills_process_group(
    session_base: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _install_fake_codex(session_base)
    stub = _install_stub_bwrap(session_base)
    monkeypatch.setenv("AUTOWORK_CODEX_BINARY", str(fake))
    monkeypatch.setenv("AUTOWORK_CODEX_BWRAP", str(stub))
    with running_session_agent(session_base) as running, running.client() as client:
        workspace_id = running.ensure_workspace(client)
        assert _prepare_codex(client, running, workspace_id).status_code == 200
        assert _post_manifest(client, running).status_code == 200
        _write_scenario(running, {"outcome": "hang"})
        started = post_session(client, running, f"/v1/sessions/{SESSION_ID}/start", {}, START_KEY)
        assert started.status_code == 200, started.text
        running_status = wait_for_state(client, running, "running")
        pid = running_status["runs"][0]["pid"]
        assert isinstance(pid, int) and proc_alive(pid)

        interrupted = post_session(
            client,
            running,
            f"/v1/sessions/{SESSION_ID}/interrupt",
            {"reason": "qualification"},
            INTERRUPT_KEY,
        )
        assert interrupted.status_code == 200, interrupted.text
        status = wait_for_state(client, running, "stopped")
        assert status["exit"]["signal"] in {15, 9}
        wait_for_process_gone(pid)


def test_codex_profile_resume_runs_only_when_proven(
    session_base: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _install_fake_codex(session_base)
    stub = _install_stub_bwrap(session_base)
    monkeypatch.setenv("AUTOWORK_CODEX_BINARY", str(fake))
    monkeypatch.setenv("AUTOWORK_CODEX_BWRAP", str(stub))
    monkeypatch.setenv("AUTOWORK_CODEX_RESUME_PROVEN", "1")
    with running_session_agent(session_base) as running, running.client() as client:
        workspace_id = running.ensure_workspace(client)
        assert _prepare_codex(client, running, workspace_id).status_code == 200
        assert _post_manifest(client, running).status_code == 200
        _write_scenario(running, {"outcome": "success", "final_message": "turn"})
        started = post_session(client, running, f"/v1/sessions/{SESSION_ID}/start", {}, START_KEY)
        assert started.status_code == 200, started.text
        wait_for_state(client, running, "awaiting_instruction")

        resumed = post_session(
            client,
            running,
            f"/v1/sessions/{SESSION_ID}/resume",
            {"reason": "proven"},
            RESUME_KEY,
        )
        assert resumed.status_code == 200, resumed.text
        assert resumed.json()["run"] == 2
        status = wait_for_state(client, running, "awaiting_instruction")
        assert len(status["runs"]) == 2
        observations = _all_observations(running)
        assert len(observations) == 2
        first, second = observations
        first_argv = first["argv"]
        assert isinstance(first_argv, list)
        assert "--ephemeral" not in first_argv
        second_argv = second["argv"]
        assert isinstance(second_argv, list)
        resume_cli = [str(item) for item in second_argv]
        assert resume_cli[:3] == [str(fake.resolve()), "exec", "resume"]
        assert "--last" in resume_cli


# ---------------------------------------------------------------------------
# HTTP helpers for the Codex adapter kind
# ---------------------------------------------------------------------------


def _prepare_codex(client, running, workspace_id: str):
    network_mode = (
        os.environ.get("AGENT_SESSIONS_CLI_NETWORK_MODE", "isolated_egress").strip()
        or "isolated_egress"
    )
    payload = prepare_payload(workspace_id, adapter_kind="codex", network_mode=network_mode)
    return post_session(client, running, "/v1/sessions", payload, PREPARE_KEY)


def _post_manifest(client, running):
    tool = {
        "mcp_name": "autowork_task_heartbeat",
        "broker_name": "task.heartbeat",
        "schema_version": "1",
        "input_schema": {"type": "object"},
        "effect": "read",
        "cost": "0",
        "capability": None,
    }
    digest = hashlib.sha256(
        json.dumps({"tools": [tool]}, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return post_session(
        client,
        running,
        f"/v1/sessions/{SESSION_ID}/tool-manifest",
        {"manifest_sha256": digest, "tools": [tool]},
        MANIFEST_KEY,
    )


def _write_scenario(running, scenario: dict[str, object], *, session_id: str = SESSION_ID) -> Path:
    path = Path(running.session_dir(session_id) / "scratch" / "work" / "fake-codex-scenario.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(scenario, separators=(",", ":")), encoding="utf-8")
    return path


def _all_observations(running, *, session_id: str = SESSION_ID) -> list[dict[str, object]]:
    path = running.session_dir(session_id) / "scratch" / "work" / "fake-codex-observations.jsonl"
    if not path.exists():
        return []
    entries: list[dict[str, object]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        value = json.loads(line)
        if isinstance(value, dict):
            entries.append({str(key): item for key, item in value.items()})
    return entries


def _last_observation(running, *, session_id: str = SESSION_ID) -> dict[str, object]:
    entries = _all_observations(running, session_id=session_id)
    assert entries, "the fake codex wrote no observation"
    return entries[-1]
