"""R03 B1 oracles: the isolated CLI network and the closed CLI environment.

The isolated launch runs bwrap in its own network namespace and attaches a
slirp4netns gateway with the host loopback disabled.  These tests prove the
argv, the fail-closed spawn path, the credential deny list and the real host
attestation contract (when the host cannot isolate, the attestation says so and
the supervisor refuses; there is no silent host-network fallback).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sidecars.workspace_agent.sessions import process


def _session_dirs(base: Path) -> dict[str, Path]:
    paths = {}
    for name in ("ipc", "scratch", "config", "work"):
        path = base / name
        path.mkdir(parents=True, exist_ok=True)
        paths[name] = path
    return paths


def test_build_bwrap_argv_unshares_net_only_in_isolated_mode(tmp_path: Path) -> None:
    dirs = _session_dirs(tmp_path)
    common = {
        "workspace_binds": (),
        "ipc_directory": dirs["ipc"],
        "scratch_directory": dirs["scratch"],
        "config_directory": dirs["config"],
        "work_directory": dirs["work"],
        "bwrap": tmp_path / "bwrap",
    }
    isolated = process.build_bwrap_argv(
        ("/usr/bin/codex", "exec"),
        network_mode=process.NETWORK_MODE_ISOLATED_EGRESS,
        **common,
    )
    unsafe = process.build_bwrap_argv(
        ("/usr/bin/codex", "exec"),
        network_mode=process.NETWORK_MODE_HOST_NETWORK_UNSAFE,
        **common,
    )
    assert "--unshare-net" in isolated
    assert "--unshare-net" not in unsafe
    assert isolated[-3:] == ["--", "/usr/bin/codex", "exec"]
    assert "--chdir" in isolated


def test_build_bwrap_argv_rejects_an_unknown_network_mode(tmp_path: Path) -> None:
    dirs = _session_dirs(tmp_path)
    with pytest.raises(ValueError):
        process.build_bwrap_argv(
            ("/usr/bin/codex",),
            workspace_binds=(),
            ipc_directory=dirs["ipc"],
            scratch_directory=dirs["scratch"],
            config_directory=dirs["config"],
            work_directory=dirs["work"],
            network_mode="provider_only_magic",
        )


def test_cli_environment_deny_list_covers_autowork_credentials() -> None:
    for key in (
        "OPERATOR_API_TOKEN",
        "OPERATOR_CLIENT_API_TOKEN",
        "M24_AGENT_SESSION_KEY",
        "VIRUSTOTAL_API_KEY",
        "SHODAN_API_KEY",
        "DATABASE_URL",
        "MINIO_SECRET_KEY",
        "SOME_FUTURE_TOKEN",
        "SOME_FUTURE_PASSWORD",
        "SOME_FUTURE_API_KEY",
    ):
        assert process.cli_environment_violations({key: "value"}) == (key,), key
    clean = {
        "PATH": "/usr/bin:/bin",
        "HOME": "/tmp/home",
        "AUTOWORK_SESSION_ID": "s",
        "AUTOWORK_SESSION_SOCKET": "/tmp/socket",
        "AUTOWORK_BACKEND_URL": "http://127.0.0.1:8000",
    }
    assert process.cli_environment_violations(clean) == ()
    process.assert_cli_environment_safe(clean)
    with pytest.raises(process.CliEnvironmentError):
        process.assert_cli_environment_safe({**clean, "OPERATOR_API_TOKEN": "x"})


def test_isolated_spawn_fails_closed_when_bwrap_does_not_attest(tmp_path: Path) -> None:
    plan = process.LaunchPlan(
        argv=("/bin/true",),
        env={"PATH": "/usr/bin:/bin"},
        cwd=tmp_path,
        uses_bwrap=True,
        network_mode=process.NETWORK_MODE_ISOLATED_EGRESS,
    )
    with pytest.raises(process.NetworkIsolationUnavailable):
        process.spawn(plan)


def test_real_host_attestation_blocks_host_loopback_or_refuses() -> None:
    attestation = process.network_isolation_attestation()
    if attestation.ok:
        assert attestation.loopback_isolated is True
        assert attestation.network_mode == process.NETWORK_MODE_ISOLATED_EGRESS
        assert "loopback" in attestation.reason.lower()
    else:
        # No silent fallback: an unprovable host is a typed refusal.
        assert attestation.reason
        assert attestation.loopback_isolated is False
