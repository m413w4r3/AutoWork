"""M24-05 H5 end-to-end: the Claude profile through the real H1 supervisor.

The generated ``claude`` wrapper runs the deterministic fixture CLI and the
generated ``bwrap`` double prints the reason it is mocked on stderr (asserted in
the session log), so confinement is never silently skipped. No model, provider,
network or login call is made.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest
from sidecars.workspace_agent.sessions import process as session_process
from sidecars.workspace_agent.sessions.adapters.claude import (
    DEFAULT_MAX_BUDGET_USD,
    ClaudeProfile,
    ClaudeStreamParser,
    ConfinementProbe,
)
from sidecars.workspace_agent.sessions.state import (
    ResumeUnavailableError,
    SessionState,
)
from sidecars.workspace_agent.sessions.supervisor import (
    CollectRequest,
    PrepareSessionRequest,
    ProfileRegistry,
    ReasonRequest,
    ResumeRequest,
    SessionSupervisor,
    SessionSupervisorConfig,
    StartSessionRequest,
    ToolManifestRequest,
)

from tests.workspace_agent.claude_profile_support import (
    MOCKED_BWRAP_REASON,
    SESSION_ID,
    write_fake_bwrap,
    write_fake_claude,
)
from tests.workspace_agent.sessions_support import prepare_payload, wait_until
from tests.workspace_agent.w3_support import ScanEnvironment, make_scan_environment

MANIFEST_TOOLS = [
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


@pytest.fixture(autouse=True)
def _bwrap_prepare_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    """Let H1's prepare gate accept the injected fake bwrap on any host.

    This is not a silent skip: the fake bwrap prints ``MOCKED_BWRAP_REASON`` on
    stderr (asserted in the session log by the lifecycle test) and the profile's
    injected confinement probe reports ``mocked=True`` with the same reason on
    the capability wire.  The network attestation is injected at the module
    seam (the stand-in creates no namespace) while the supervisor still wires
    the real relay and egress sockets.
    """
    monkeypatch.setattr(session_process, "bwrap_available", lambda: True)
    monkeypatch.setattr(
        session_process,
        "network_isolation_attestation",
        lambda **kwargs: session_process.NetworkIsolationAttestation(
            True, "test attestation", "isolated_egress", True
        ),
    )


def _egress_profile(session_base: Path) -> Path:
    """The real session's provider allowlist, narrowed to the Claude maximum."""
    path = session_base / "egress-profile.json"
    path.write_text(
        json.dumps(
            {
                "schema": "autowork-session-egress.v1",
                "profiles": {"claude": {"hosts": ["api.anthropic.com"]}},
            }
        ),
        encoding="utf-8",
    )
    os.chmod(path, 0o600)
    return path


def _build_supervisor(
    session_base: Path,
    environment: ScanEnvironment,
    *,
    resume_proven: bool = False,
) -> tuple[SessionSupervisor, SessionSupervisorConfig]:
    config = SessionSupervisorConfig(
        runtime_root=session_base / "run",
        workspace_root=environment.root,
        interrupt_grace_seconds=0.2,
        watchdog_poll_seconds=0.05,
        # Isolation is the only mode; the named fake bwrap cannot create
        # namespaces, so the host attestation is injected above and the real
        # relay/egress wiring still runs.
        cli_network_mode="isolated_egress",
        egress_profile_path=_egress_profile(session_base),
    )
    profile = ClaudeProfile(
        binary=write_fake_claude(session_base / "bin"),
        bwrap_binary=write_fake_bwrap(session_base / "bin"),
        resume_proven=resume_proven,
        network_mode="isolated_egress",
        confinement_probe=lambda: ConfinementProbe(
            ok=True,
            reason=MOCKED_BWRAP_REASON,
            mocked=True,
            network_mode="isolated_egress",
            loopback_isolated=True,
            netns=True,
        ),
    )
    supervisor = SessionSupervisor(
        config,
        workspaces=environment.manager,
        scans=environment.service(),
        profiles=ProfileRegistry((profile,)),
    )
    return supervisor, config


def _prepare(supervisor: SessionSupervisor, workspace_id: str) -> dict[str, object]:
    body = supervisor.prepare(
        PrepareSessionRequest.from_payload(
            prepare_payload(
                workspace_id,
                adapter_kind="claude",
                session_id=SESSION_ID,
                network_mode="isolated_egress",
            )
        ),
        idempotency_key="m24-h5-prepare-0001",
        body_sha256="a" * 64,
    )
    assert isinstance(body, dict)
    return {str(key): value for key, value in body.items()}


def _accept_manifest(supervisor: SessionSupervisor) -> None:
    canonical = json.dumps({"tools": MANIFEST_TOOLS}, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    supervisor.tool_manifest(
        SESSION_ID,
        ToolManifestRequest.from_payload(
            {
                "manifest_sha256": hashlib.sha256(canonical).hexdigest(),
                "tools": MANIFEST_TOOLS,
            }
        ),
        idempotency_key="m24-h5-manifest-0001",
        body_sha256="d" * 64,
    )


def _write_scenario(config: SessionSupervisorConfig, scenario: str) -> None:
    work = config.session_root / SESSION_ID / "scratch" / "work"
    work.mkdir(parents=True, exist_ok=True)
    path = work / "claude-scenario.json"
    path.write_text(json.dumps({"scenario": scenario}), encoding="utf-8")
    os.chmod(path, 0o600)


def _wait_state(
    supervisor: SessionSupervisor, *states: str, timeout: float = 20.0
) -> dict[str, object]:
    def _status() -> dict[str, object] | None:
        payload = supervisor.status(SESSION_ID)
        return payload if payload["state"] in states else None

    found = wait_until(_status, timeout=timeout)
    assert isinstance(found, dict)
    return found


def _observations(config: SessionSupervisorConfig) -> dict[str, object]:
    path = config.session_root / SESSION_ID / "scratch" / "work" / "claude-observations.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    assert isinstance(value, dict)
    return value


def _log_text(supervisor: SessionSupervisor) -> str:
    page = supervisor.log_page(SESSION_ID, after=0, limit=1000)
    lines = page["lines"]
    assert isinstance(lines, list)
    return "\n".join(str(line["text"]) for line in lines)


def test_claude_session_lifecycle_stream_events_and_collect(session_base: Path) -> None:
    environment = make_scan_environment(session_base)
    supervisor, config = _build_supervisor(session_base, environment)
    try:
        capabilities = supervisor.capabilities("claude")
        assert capabilities["adapter_kind"] == "claude"
        assert capabilities["flags"]["streaming_stdin"] is False
        assert capabilities["flags"]["resume"] is False
        assert capabilities["confinement"]["mocked"] is True
        assert capabilities["confinement"]["netns"] is True
        assert capabilities["confinement"]["network_mode"] == "isolated_egress"
        entries = {entry["name"]: entry for entry in capabilities["capabilities"]}
        assert entries["stream_json_live_instructions"]["proven"] is False
        assert capabilities["host_spike"]

        prepared = _prepare(supervisor, environment.workspace_id)
        assert prepared["state"] == "ready"
        _accept_manifest(supervisor)
        _write_scenario(config, "note")
        started = supervisor.start(
            SESSION_ID,
            StartSessionRequest(),
            idempotency_key="m24-h5-start-0001",
            body_sha256="b" * 64,
        )
        assert started["state"] == "running"
        _wait_state(supervisor, str(SessionState.STOPPED))

        observations = _observations(config)
        parse = observations["parse"]
        assert isinstance(parse, dict)
        if not parse:  # pragma: no cover - a missing observation is a real failure
            pytest.fail("the fake CLI never recorded its contract observations")
        assert parse["print_mode"] is True
        assert parse["output_format"] == "stream-json"
        assert parse["input_format"] is None
        assert parse["restricted"] is True
        assert parse["bare"] is True
        assert parse["strict_mcp_config"] is True
        assert parse["permission_mode"] == "dontAsk"
        assert parse["permission_prompts"] == "none"
        assert parse["no_session_persistence"] is True
        assert parse["max_budget_usd"] == DEFAULT_MAX_BUDGET_USD
        assert parse["session_id"] is None
        assert parse["resume"] is None
        assert "Bash" in str(parse["disallowed_tools"])
        assert parse["prompt_sha256"] == prepared["prompt_sha256"]
        assert parse["add_dirs"] == [str(config.session_root / SESSION_ID / "scratch")]
        system_prompt = parse["append_system_prompt"]
        assert isinstance(system_prompt, str)
        assert str(config.session_root / SESSION_ID / "scratch" / "result.md") in system_prompt
        assert "Authority limits" in system_prompt

        env = observations["env"]
        assert isinstance(env, dict)
        assert "CLAUDE_CONFIG_DIR" not in env
        assert not any(
            marker in key.upper()
            for key in env
            for marker in ("TOKEN", "SECRET", "PASSWORD", "APIKEY")
        ), sorted(env)
        assert env["HOME"].startswith(str(config.session_root / SESSION_ID / "config"))
        assert env["AUTOWORK_SESSION_SOCKET"] == str(
            config.session_root / SESSION_ID / "ipc" / "shim.sock"
        )

        mcp = observations["mcp"]
        assert isinstance(mcp, dict)
        assert mcp["present"] is True
        assert mcp["server_names"] == ["autowork"]
        assert mcp["mode"] == 0o600
        helper = mcp["helpers"]["autowork"]
        assert helper["shim"]["exists"] is True
        assert helper["shim"]["mode"] == 0o700
        assert helper["shim"]["symlink"] is False
        assert sorted(helper["env_keys"]) == [
            "AUTOWORK_BROKER_SOCKET",
            "AUTOWORK_SESSION_ID",
            "AUTOWORK_SESSION_SOCKET",
        ]
        assert "secret" not in json.dumps(mcp).lower()

        parser = ClaudeStreamParser()
        emitted = observations["emitted"]
        assert isinstance(emitted, list) and emitted
        for frame in emitted:
            parser.feed(json.dumps(frame).encode("utf-8"))
        assert parser.final_message == "fake claude final message"
        assert parser.outcome == "success"

        collected = supervisor.collect(
            SESSION_ID,
            CollectRequest.from_payload({"capture": True}),
            idempotency_key="m24-h5-collect-0001",
            body_sha256="e" * 64,
        )
        assert collected["outcome"] == "success"
        assert collected["exit"]["code"] == 0
        assert (
            collected["final_message_sha256"]
            == hashlib.sha256(b"fake claude final message").hexdigest()
        )
        files = collected["files"]
        assert isinstance(files, list)
        destinations = {item["path"] for item in files}
        task_id = prepare_payload(environment.workspace_id)["task_id"]
        assert f"notes/agents/{task_id}/finding.md" in destinations
        assert f"results/sessions/{SESSION_ID}/result.md" in destinations
        assert f"results/sessions/{SESSION_ID}/final.md" in destinations

        assert MOCKED_BWRAP_REASON in _log_text(supervisor)
        assert "claude-scenario" not in _log_text(supervisor)
    finally:
        supervisor.stop()


def test_claude_hang_is_interrupted_by_process_group(session_base: Path) -> None:
    environment = make_scan_environment(session_base)
    supervisor, config = _build_supervisor(session_base, environment)
    try:
        _prepare(supervisor, environment.workspace_id)
        _accept_manifest(supervisor)
        _write_scenario(config, "hang")
        supervisor.start(
            SESSION_ID,
            StartSessionRequest(),
            idempotency_key="m24-h5-start-0002",
            body_sha256="b" * 64,
        )
        pid = wait_until(lambda: _observations(config).get("pid"), timeout=20.0)
        assert isinstance(pid, int)
        supervisor.interrupt(
            SESSION_ID,
            ReasonRequest.from_payload({"reason": "m24-h5 interrupt oracle"}),
            idempotency_key="m24-h5-interrupt-0001",
            body_sha256="f" * 64,
        )
        status = _wait_state(supervisor, str(SessionState.STOPPED), str(SessionState.FAILED))
        assert status["state"] == "stopped"
        collected = supervisor.collect(
            SESSION_ID,
            CollectRequest.from_payload({"capture": False}),
            idempotency_key="m24-h5-collect-0002",
            body_sha256="e" * 64,
        )
        assert collected["outcome"] == "interrupted"
        assert len(status["runs"]) == 1
        wait_until(lambda: not _alive(pid), timeout=20.0)
    finally:
        supervisor.stop()


def _alive(pid: int) -> bool:
    try:
        text = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except OSError:
        return False
    remainder = text.rpartition(")")[2].split()
    return bool(remainder) and remainder[0] != "Z"


def test_claude_crash_is_recorded_failed_and_never_fabricated(session_base: Path) -> None:
    environment = make_scan_environment(session_base)
    supervisor, config = _build_supervisor(session_base, environment)
    try:
        _prepare(supervisor, environment.workspace_id)
        _accept_manifest(supervisor)
        _write_scenario(config, "crash")
        supervisor.start(
            SESSION_ID,
            StartSessionRequest(),
            idempotency_key="m24-h5-start-0005",
            body_sha256="b" * 64,
        )
        status = _wait_state(supervisor, str(SessionState.STOPPED), str(SessionState.FAILED))
        assert status["state"] == "failed"
        collected = supervisor.collect(
            SESSION_ID,
            CollectRequest.from_payload({"capture": False}),
            idempotency_key="m24-h5-collect-0003",
            body_sha256="e" * 64,
        )
        assert collected["outcome"] == "failed"
        assert collected["exit"]["code"] == 3
        assert collected["final_message_sha256"] is None
        assert collected["files"] == []
    finally:
        supervisor.stop()


def test_claude_resume_is_refused_until_the_probe_proves_it(session_base: Path) -> None:
    environment = make_scan_environment(session_base)
    supervisor, config = _build_supervisor(session_base, environment)
    try:
        _prepare(supervisor, environment.workspace_id)
        _accept_manifest(supervisor)
        _write_scenario(config, "success")
        supervisor.start(
            SESSION_ID,
            StartSessionRequest(),
            idempotency_key="m24-h5-start-0003",
            body_sha256="b" * 64,
        )
        _wait_state(supervisor, str(SessionState.STOPPED))
        with pytest.raises(ResumeUnavailableError):
            supervisor.resume(
                SESSION_ID,
                ResumeRequest.from_payload({"reason": "prove the gate"}),
                idempotency_key="m24-h5-resume-0001",
                body_sha256="c" * 64,
            )
        status = supervisor.status(SESSION_ID)
        assert len(status["runs"]) == 1
    finally:
        supervisor.stop()


def test_claude_resume_roundtrip_through_the_supervisor_when_proven(
    session_base: Path,
) -> None:
    environment = make_scan_environment(session_base)
    supervisor, config = _build_supervisor(session_base, environment, resume_proven=True)
    try:
        _prepare(supervisor, environment.workspace_id)
        _accept_manifest(supervisor)
        _write_scenario(config, "success")
        supervisor.start(
            SESSION_ID,
            StartSessionRequest(),
            idempotency_key="m24-h5-start-0004",
            body_sha256="b" * 64,
        )
        _wait_state(supervisor, str(SessionState.STOPPED))
        first = _observations(config)["parse"]
        assert isinstance(first, dict)
        assert first["session_id"] == SESSION_ID
        assert first["resume"] is None
        assert first["no_session_persistence"] is False

        resumed = supervisor.resume(
            SESSION_ID,
            ResumeRequest.from_payload({"reason": "probe proved resume"}),
            idempotency_key="m24-h5-resume-0002",
            body_sha256="c" * 64,
        )
        assert resumed["state"] == "running"
        assert resumed["run"] == 2
        _wait_state(supervisor, str(SessionState.STOPPED))
        second = _observations(config)["parse"]
        assert isinstance(second, dict)
        assert second["resume"] == SESSION_ID
        assert second["session_id"] is None
        status = supervisor.status(SESSION_ID)
        assert len(status["runs"]) == 2
    finally:
        supervisor.stop()
