"""NET-1 oracles: the isolated CLI network, the netns attestation and fail-closed.

The production profile is a bwrap ``--unshare-net`` namespace with only ``lo``:
no slirp4netns, no ``tap0`` and no route.  ``spawn`` attests a fresh namespace
with the same bwrap profile before any CLI is released, then starts the trusted
``entrypoint.py forward-and-exec`` wrapper whose forwarder listens on
``127.0.0.1:3128`` inside the namespace and byte-pipes to the session's
``ipc/egress.sock``.  A missing bwrap, user namespace or proof is a typed
``confinement_unavailable`` refusal; there is no degraded fallback.

Simulated (S) oracles inject fake attestation output and a fake bwrap binary;
real (R) oracles run only where the production probe grants isolation and are
skipped with the measured refusal reason otherwise (the refusal oracle itself
always passes).  No provider, LAN or external call is ever made.
"""

from __future__ import annotations

import json
import re
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import cast

import pytest
from sidecars.workspace_agent.sessions import process

TEST_ENV = {
    "PATH": "/usr/bin:/bin",
    "HOME": "/tmp",
    "LANG": "C.UTF-8",
    "LC_ALL": "C.UTF-8",
}
PROXY_URL = "http://127.0.0.1:3128"
PROXY_ENV_KEYS = frozenset(
    {
        "HTTPS_PROXY",
        "HTTP_PROXY",
        "ALL_PROXY",
        "https_proxy",
        "http_proxy",
        "all_proxy",
        "NO_PROXY",
    }
)


@pytest.fixture(autouse=True)
def _fresh_attestation_cache() -> Iterator[None]:
    process.clear_network_attestation_cache()
    try:
        yield
    finally:
        process.clear_network_attestation_cache()


def _session_dirs(base: Path) -> dict[str, Path]:
    """Mirror the supervisor layout: the work directory lives under scratch."""

    paths = {}
    for name in ("ipc", "scratch", "config"):
        path = base / name
        path.mkdir(parents=True, exist_ok=True)
        paths[name] = path
    work = paths["scratch"] / "work"
    work.mkdir(parents=True, exist_ok=True)
    paths["work"] = work
    return paths


def _plant_egress_socket(path: Path) -> None:
    """A bound-then-closed AF_UNIX path is still a socket inode on disk."""

    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        listener.bind(str(path))
    finally:
        listener.close()


def _bwrap_profile(base: Path, cli_argv: tuple[str, ...]) -> list[str]:
    dirs = _session_dirs(base)
    return cast(
        list[str],
        process.build_bwrap_argv(
            cli_argv,
            workspace_binds=(),
            ipc_directory=dirs["ipc"],
            scratch_directory=dirs["scratch"],
            config_directory=dirs["config"],
            work_directory=dirs["work"],
        ),
    )


def _isolated_plan(
    base: Path,
    cli_argv: tuple[str, ...],
    *,
    egress: bool = True,
) -> process.LaunchPlan:
    dirs = _session_dirs(base)
    if egress:
        _plant_egress_socket(dirs["ipc"] / process.EGRESS_SOCKET_FILE_NAME)
    return process.LaunchPlan(
        argv=tuple(_bwrap_profile(base, cli_argv)),
        env={**TEST_ENV, "AUTOWORK_SESSION_SOCKET": str(dirs["ipc"] / "shim.sock")},
        cwd=dirs["work"],
        uses_bwrap=True,
        network_mode=process.NETWORK_MODE_ISOLATED_EGRESS,
    )


def _run_isolated(plan: process.LaunchPlan, *, timeout: float = 30.0) -> tuple[int, bytes, bytes]:
    managed = process.spawn(plan)
    completed = False
    try:
        stdout, stderr = managed.process.communicate(timeout=timeout)
        completed = True
    finally:
        process.stop_network(managed)
        process.close_pipes(managed.process)
        if not completed:
            process.terminate_group(managed.pgid, grace_seconds=1.0)
    return int(managed.process.returncode or 0), stdout, stderr


def _require_real_netns() -> process.NetworkIsolationAttestation:
    """Run the production probe; skip R tests with its measured refusal reason."""

    attestation = process.network_isolation_attestation()
    if not attestation.ok:
        pytest.skip(f"real netns unavailable on this host: {attestation.reason}")
    return attestation


def _valid_evidence(**overrides: object) -> dict[str, object]:
    evidence: dict[str, object] = {
        "schema": process.ATTEST_SCHEMA,
        "ok": True,
        "ifaces": ["lo"],
        "lo_flags": 0x49,
        "ipv4_routes": 0,
        "ipv6_routes": ["::1/128"],
        "ipv6_reject_routes": 2,
        "loopback": {"v4_bind": True, "v4_connect": True, "v6_bind": True, "v6_connect": True},
        "blocked": {address: "ENETUNREACH" for address in process.REQUIRED_BLOCKED_ADDRESSES},
        "host_loopback": "blocked",
        "errors": [],
    }
    evidence.update(overrides)
    return evidence


def _fake_bwrap(base: Path, *, payload: dict[str, object] | None = None) -> Path:
    """A named bwrap stand-in that prints fixed attestation evidence."""

    path = base / "fake-bwrap"
    body = json.dumps(payload if payload is not None else _valid_evidence()).encode()
    path.write_bytes(b"#!/bin/sh\nprintf '%s\\n' '" + body + b"'\n")
    path.chmod(0o700)
    return path


class _HostLoopbackListener:
    """A host-loopback TCP listener that counts connections (EG-02)."""

    def __init__(self) -> None:
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(4)
        self._listener.settimeout(0.2)
        self.port = int(self._listener.getsockname()[1])
        self.accepted = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._thread.start()

    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            try:
                connection, _peer = self._listener.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            self.accepted += 1
            connection.close()

    def close(self) -> None:
        self._stop.set()
        try:
            self._listener.close()
        except OSError:
            pass
        self._thread.join(timeout=2)


# ---------------------------------------------------------------------------
# Simulated oracles (S)
# ---------------------------------------------------------------------------


def test_build_bwrap_argv_always_unshares_net(tmp_path: Path) -> None:
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
    assert "--unshare-net" in isolated
    # The adapter's CLI argv stays the bwrap tail; the entrypoint wrapper is
    # added by spawn (the launch layer), not by the profile builder.
    assert isolated[-3:] == ["--", "/usr/bin/codex", "exec"]
    assert "--chdir" in isolated
    assert all("entrypoint" not in token for token in isolated)


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
        "AUTOWORK_SHIM_TEST_BACKEND_URL": "http://127.0.0.1:8000",
    }
    assert process.cli_environment_violations(clean) == ()
    process.assert_cli_environment_safe(clean)
    with pytest.raises(process.CliEnvironmentError):
        process.assert_cli_environment_safe({**clean, "OPERATOR_API_TOKEN": "x"})


def test_isolated_entry_argv_wraps_the_cli_with_the_trusted_entrypoint(
    tmp_path: Path,
) -> None:
    dirs = _session_dirs(tmp_path)
    socket_path = dirs["ipc"] / process.EGRESS_SOCKET_FILE_NAME
    _plant_egress_socket(socket_path)
    profile = _bwrap_profile(tmp_path, ("/usr/bin/codex", "exec", "--json"))
    wrapped = process.isolated_entry_argv(profile, egress_socket_path=socket_path)
    entrypoint = str(process.entrypoint_path())
    separator = profile.index("--")

    # The bwrap profile (namespaces and binds) is untouched; the entrypoint
    # mount and wrapper replace the CLI tail.
    assert wrapped[:separator] == profile[:separator]
    assert wrapped[separator:] == [
        "--ro-bind",
        entrypoint,
        entrypoint,
        "--",
        process.PROBE_PYTHON,
        entrypoint,
        "forward-and-exec",
        "--upstream",
        str(socket_path),
        "--",
        "/usr/bin/codex",
        "exec",
        "--json",
    ]
    assert "--unshare-net" in wrapped


def test_isolated_entry_argv_requires_a_live_egress_socket(tmp_path: Path) -> None:
    dirs = _session_dirs(tmp_path)
    profile = _bwrap_profile(tmp_path, ("/usr/bin/codex", "exec"))
    with pytest.raises(process.NetworkIsolationUnavailable) as raised:
        process.isolated_entry_argv(
            profile, egress_socket_path=dirs["ipc"] / process.EGRESS_SOCKET_FILE_NAME
        )
    assert raised.value.code == "confinement_unavailable"


def test_egress_socket_is_derived_from_the_ro_bound_ipc_directory(tmp_path: Path) -> None:
    dirs = _session_dirs(tmp_path)
    profile = _bwrap_profile(tmp_path, ("/usr/bin/codex", "exec"))
    assert process.egress_socket_from_bwrap_argv(profile) == (
        dirs["ipc"] / process.EGRESS_SOCKET_FILE_NAME
    )
    assert process.egress_socket_from_bwrap_argv(["/usr/bin/bwrap", "--", "true"]) is None


def test_egress_socket_derivation_prefers_the_session_secret_directory(tmp_path: Path) -> None:
    dirs = _session_dirs(tmp_path)
    decoy = tmp_path / "workspace" / "ipc"
    decoy.mkdir(parents=True)
    profile = _bwrap_profile(tmp_path, ("/usr/bin/codex", "exec"))
    separator = profile.index("--")
    profile = [*profile[:separator], "--ro-bind", str(decoy), str(decoy), *profile[separator:]]
    plan = process.LaunchPlan(
        argv=tuple(profile),
        env={**TEST_ENV, "AUTOWORK_SESSION_SOCKET": str(dirs["ipc"] / "shim.sock")},
        cwd=dirs["work"],
        uses_bwrap=True,
    )
    assert process.egress_socket_for_plan(plan) == dirs["ipc"] / process.EGRESS_SOCKET_FILE_NAME
    explicit = process.LaunchPlan(
        argv=tuple(profile),
        env=dict(TEST_ENV),
        cwd=dirs["work"],
        uses_bwrap=True,
        egress_socket_path=tmp_path / "explicit-egress.sock",
    )
    assert process.egress_socket_for_plan(explicit) == tmp_path / "explicit-egress.sock"


def test_spawn_fails_closed_when_attestation_is_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = _isolated_plan(tmp_path, ("/usr/bin/codex", "exec"))
    monkeypatch.setattr(
        process,
        "network_isolation_attestation",
        lambda **kwargs: process.NetworkIsolationAttestation(
            False, "test refusal", process.NETWORK_MODE_ISOLATED_EGRESS, False
        ),
    )
    monkeypatch.setattr(
        process, "_spawn_direct", lambda plan: pytest.fail("degraded launch attempted")
    )
    monkeypatch.setattr(
        process.subprocess, "Popen", lambda *a, **k: pytest.fail("a process was spawned")
    )

    with pytest.raises(process.NetworkIsolationUnavailable) as raised:
        process.spawn(plan)
    assert raised.value.code == "confinement_unavailable"
    assert "attestation" in str(raised.value)


def test_spawn_refuses_a_forged_argv_without_the_netns_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # F9: the attestation probes the binary, never the argv about to launch.
    # An argv that lost ``--unshare-net`` must fail closed before any Popen.
    plan = _isolated_plan(tmp_path, ("/usr/bin/codex", "exec"))
    forged = tuple(token for token in plan.argv if token != "--unshare-net")
    assert "--unshare-net" not in forged
    forged_plan = process.LaunchPlan(
        argv=forged,
        env=plan.env,
        cwd=plan.cwd,
        uses_bwrap=True,
        network_mode=plan.network_mode,
    )
    monkeypatch.setattr(
        process,
        "network_isolation_attestation",
        lambda **kwargs: process.NetworkIsolationAttestation(
            True, "test attestation", process.NETWORK_MODE_ISOLATED_EGRESS, True
        ),
    )
    monkeypatch.setattr(
        process.subprocess, "Popen", lambda *a, **k: pytest.fail("a process was spawned")
    )

    with pytest.raises(process.NetworkIsolationUnavailable) as raised:
        process.spawn(forged_plan)
    assert raised.value.code == "confinement_unavailable"
    assert "sandbox profile" in str(raised.value)


@pytest.mark.parametrize("widening", ["--share-net", "--dev-bind", "--cap-add"])
def test_spawn_refuses_an_argv_that_widens_the_sandbox(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, widening: str
) -> None:
    plan = _isolated_plan(tmp_path, ("/usr/bin/codex", "exec"))
    separator = plan.argv.index("--")
    forged = (*plan.argv[:separator], widening, *plan.argv[separator:])
    monkeypatch.setattr(
        process,
        "network_isolation_attestation",
        lambda **kwargs: process.NetworkIsolationAttestation(
            True, "test attestation", process.NETWORK_MODE_ISOLATED_EGRESS, True
        ),
    )
    monkeypatch.setattr(
        process.subprocess, "Popen", lambda *a, **k: pytest.fail("a process was spawned")
    )
    with pytest.raises(process.NetworkIsolationUnavailable) as raised:
        process.spawn(
            process.LaunchPlan(
                argv=forged,
                env=plan.env,
                cwd=plan.cwd,
                uses_bwrap=True,
                network_mode=plan.network_mode,
            )
        )
    assert raised.value.code == "confinement_unavailable"
    assert "widens the sandbox" in str(raised.value)


def test_spawn_fails_closed_when_the_egress_proxy_socket_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = _isolated_plan(tmp_path, ("/usr/bin/codex", "exec"), egress=False)
    monkeypatch.setattr(
        process,
        "network_isolation_attestation",
        lambda **kwargs: process.NetworkIsolationAttestation(
            True, "test attestation", process.NETWORK_MODE_ISOLATED_EGRESS, True
        ),
    )
    monkeypatch.setattr(
        process.subprocess, "Popen", lambda *a, **k: pytest.fail("a process was spawned")
    )

    with pytest.raises(process.NetworkIsolationUnavailable) as raised:
        process.spawn(plan)
    assert raised.value.code == "confinement_unavailable"


def test_world_writable_entrypoint_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entrypoint = tmp_path / "entrypoint.py"
    entrypoint.write_text("x = 1\n", encoding="utf-8")
    entrypoint.chmod(0o666)
    monkeypatch.setattr(process, "entrypoint_path", lambda: entrypoint)
    plan = _isolated_plan(tmp_path, ("/usr/bin/codex", "exec"))
    monkeypatch.setattr(
        process,
        "network_isolation_attestation",
        lambda **kwargs: process.NetworkIsolationAttestation(
            True, "test attestation", process.NETWORK_MODE_ISOLATED_EGRESS, True
        ),
    )
    with pytest.raises(process.NetworkIsolationUnavailable) as raised:
        process.spawn(plan)
    assert "trusted code" in str(raised.value)


def test_isolated_spawn_fails_closed_when_bwrap_does_not_attest(tmp_path: Path) -> None:
    # ``/bin/true`` is not bwrap: it prints no evidence, so the production
    # attestation refuses before any CLI process exists.
    plan = process.LaunchPlan(
        argv=("/bin/true",),
        env={"PATH": "/usr/bin:/bin"},
        cwd=tmp_path,
        uses_bwrap=True,
        network_mode=process.NETWORK_MODE_ISOLATED_EGRESS,
    )
    with pytest.raises(process.NetworkIsolationUnavailable) as raised:
        process.spawn(plan)
    assert raised.value.code == "confinement_unavailable"


def test_attestation_rejects_fake_or_tampered_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_bwrap = _fake_bwrap(tmp_path)
    monkeypatch.setattr(process, "PROBE_PYTHON", sys.executable)
    seen: list[list[str]] = []

    def run_with(payload: dict[str, object] | bytes) -> process.NetworkIsolationAttestation:
        def runner(argv: list[str], *, cwd: Path, env: dict[str, str]) -> tuple[int | None, bytes]:
            del cwd, env
            seen.append(list(argv))
            body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
            return 0, body

        return process.network_isolation_attestation(
            python_executable=sys.executable, bwrap=fake_bwrap, probe_runner=runner
        )

    valid = run_with(_valid_evidence())
    assert valid.ok is True
    assert "loopback" in valid.reason.lower()
    assert seen and "--unshare-net" in seen[0]
    command = seen[0]
    assert command[-6] == "--"
    assert command[-5] == sys.executable
    assert command[-4] == str(process.entrypoint_path())
    assert command[-3] == "attest"
    assert command[-2] == "--host-loopback-port"

    refusals = [
        _valid_evidence(ifaces=["lo", "eth0"]),
        _valid_evidence(lo_flags=0x40),
        _valid_evidence(ipv4_routes=1),
        _valid_evidence(ipv6_routes=["::1/128", "fe80::/64"]),
        _valid_evidence(loopback={"v4_bind": True, "v4_connect": True, "v6_bind": True}),
        _valid_evidence(
            blocked={
                **cast(dict[str, str], _valid_evidence()["blocked"]),
                "10.0.0.1": "CONNECTED",
            }
        ),
        _valid_evidence(host_loopback="reachable"),
        _valid_evidence(errors=["unexpected_interfaces"]),
        _valid_evidence(extra="unexpected"),
        _valid_evidence(ok=False),
        b"not json",
        b"",
    ]
    for payload in refusals:
        attestation = run_with(payload)
        assert attestation.ok is False, payload
        assert attestation.loopback_isolated is False, payload
        assert attestation.reason, payload

    def failing_runner(
        argv: list[str], *, cwd: Path, env: dict[str, str]
    ) -> tuple[int | None, bytes]:
        del argv, cwd, env
        return 1, json.dumps(
            _valid_evidence(ok=False, errors=["reachable_address:10.0.0.1"])
        ).encode()

    refused = process.network_isolation_attestation(
        python_executable=sys.executable, bwrap=fake_bwrap, probe_runner=failing_runner
    )
    assert refused.ok is False
    assert "reachable_address:10.0.0.1" in refused.reason


def test_attestation_is_cached_per_bwrap_hash_and_invalidated_on_change(tmp_path: Path) -> None:
    counter = tmp_path / "runs"
    counter.write_text("0", encoding="utf-8")
    fake_bwrap = tmp_path / "fake-bwrap"

    def install(body: bytes) -> Path:
        fake_bwrap.write_bytes(
            b"#!/bin/sh\n"
            b"n=$(cat " + str(counter).encode() + b")\n"
            b"echo $((n+1)) > " + str(counter).encode() + b"\n" + body
        )
        fake_bwrap.chmod(0o700)
        return fake_bwrap

    def runs() -> str:
        return counter.read_text(encoding="utf-8").strip()

    install(b"printf '%s\\n' '" + json.dumps(_valid_evidence()).encode() + b"'\n")
    first = process.network_isolation_attestation(
        python_executable=sys.executable, bwrap=fake_bwrap
    )
    second = process.network_isolation_attestation(
        python_executable=sys.executable, bwrap=fake_bwrap
    )
    assert first.ok is True and second is first
    assert runs() == "1"

    # A changed bwrap binary hash invalidates the cache and is probed again.
    install(b"exit 3\n")
    changed = process.network_isolation_attestation(
        python_executable=sys.executable, bwrap=fake_bwrap
    )
    assert changed.ok is False
    assert runs() == "2"


def test_host_netns_attestation_refuses_on_the_attesting_host() -> None:
    # The host has more than ``lo``: the in-sandbox attestation run directly on
    # the host must refuse (a negative control for the real oracle).
    completed = process.subprocess.run(
        [process.PROBE_PYTHON, str(process.entrypoint_path()), "attest"],
        env=dict(TEST_ENV),
        stdin=process.subprocess.DEVNULL,
        capture_output=True,
        timeout=30,
        check=False,
    )
    payload = json.loads(completed.stdout.decode("utf-8"))
    assert payload["schema"] == process.ATTEST_SCHEMA
    if payload["ok"] is True:
        pytest.skip("the test host itself runs in a loopback-only network namespace")
    assert payload["ok"] is False
    assert isinstance(payload["ifaces"], list)


# ---------------------------------------------------------------------------
# Real oracles (R): skipped with the measured probe refusal when unavailable
# ---------------------------------------------------------------------------


def test_real_host_attestation_blocks_host_loopback_or_refuses() -> None:
    attestation = process.network_isolation_attestation()
    if attestation.ok:
        assert attestation.loopback_isolated is True
        assert attestation.network_mode == process.NETWORK_MODE_ISOLATED_EGRESS
        assert "loopback" in attestation.reason.lower()
        cached = process.network_isolation_attestation()
        assert cached is attestation
    else:
        # No silent fallback: an unprovable host is a typed refusal.
        assert attestation.reason
        assert attestation.loopback_isolated is False


def test_real_netns_cannot_reach_a_host_loopback_listener(tmp_path: Path) -> None:
    _require_real_netns()
    listener = _HostLoopbackListener()
    try:
        code = (
            "import socket\n"
            "probe = socket.socket()\n"
            "probe.settimeout(2.0)\n"
            f"rc = probe.connect_ex(('127.0.0.1', {listener.port}))\n"
            "probe.close()\n"
            "print('NETNS_CONNECT_RC', rc)\n"
        )
        plan = _isolated_plan(tmp_path, (process.PROBE_PYTHON, "-c", code))
        returncode, stdout, _stderr = _run_isolated(plan)
        assert returncode == 0, stdout
        match = re.search(rb"NETNS_CONNECT_RC (\d+)", stdout)
        assert match is not None, stdout
        assert int(match.group(1)) != 0
        time.sleep(0.2)
        assert listener.accepted == 0
    finally:
        listener.close()


def test_real_bwrap_sees_private_resolver_files_only(tmp_path: Path) -> None:
    """Read the actual /etc mounts in bwrap when this host can create netns."""
    _require_real_netns()
    runtime = tmp_path / "runtime-session"
    runtime.mkdir(mode=0o700)
    dirs = _session_dirs(runtime)
    session_etc = process.create_session_etc_directory(runtime)
    host_etc = tmp_path / "synthetic-host" / "etc"
    host_etc.mkdir(parents=True)
    sentinel = "tailnet.r2b4.invalid"
    (host_etc / "hosts").write_text(f"127.0.0.1 localhost {sentinel}\n", encoding="utf-8")
    (host_etc / "resolv.conf").write_text(
        f"search {sentinel}\nnameserver 192.0.2.53\n", encoding="utf-8"
    )
    code = (
        "import json, pathlib; "
        "print(json.dumps({name: pathlib.Path('/etc', name).read_text() "
        "for name in ('hosts', 'resolv.conf', 'nsswitch.conf')}))"
    )
    argv = process.build_bwrap_argv(
        (process.PROBE_PYTHON, "-c", code),
        workspace_binds=(),
        ipc_directory=dirs["ipc"],
        scratch_directory=dirs["scratch"],
        config_directory=dirs["config"],
        work_directory=dirs["work"],
        session_etc_directory=session_etc,
    )
    completed = subprocess.run(
        argv,
        cwd=dirs["work"],
        env=TEST_ENV,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=15,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr.decode("utf-8", errors="replace")
    observed = json.loads(completed.stdout.decode("utf-8"))
    assert observed == {
        "hosts": "127.0.0.1 localhost\n::1 localhost\n",
        "resolv.conf": "",
        "nsswitch.conf": "hosts: files\n",
    }
    assert sentinel not in completed.stdout.decode("utf-8")
    ro_binds = [
        (Path(argv[index + 1]), Path(argv[index + 2]))
        for index in range(len(argv) - 2)
        if argv[index] == "--ro-bind"
    ]
    assert all(pair in ro_binds for pair in process.session_etc_ro_binds(session_etc))


def test_real_spawn_survives_a_short_lived_calling_thread(tmp_path: Path) -> None:
    """H0 regression: bwrap's death signal follows the *spawning thread*.

    The supervisor serves ``POST /start`` from a per-request thread.  A direct
    ``Popen`` there left ``PR_SET_PDEATHSIG`` pointing at a thread that dies
    when the response is written, and the real sandbox was SIGKILLed seconds
    into every run with no caller-side signal.  ``spawn`` anchors the child to
    a thread that lives as long as it; only supervisor death tears it down.
    """
    _require_real_netns()
    plan = _isolated_plan(tmp_path, (process.PROBE_PYTHON, "-c", "import time; time.sleep(10)"))
    captured: dict[str, process.ManagedProcess] = {}

    def caller() -> None:
        captured["managed"] = process.spawn(plan)
        # bwrap has set its death signal well before this; the thread exit is
        # exactly what used to kill the sandbox.
        time.sleep(0.5)

    thread = threading.Thread(target=caller)
    thread.start()
    thread.join()
    managed = captured["managed"]
    try:
        time.sleep(0.5)
        assert managed.process.poll() is None, (
            "the sandbox was killed when its calling thread exited"
        )
    finally:
        process.terminate_group(managed.pgid, grace_seconds=1.0)
        managed.process.wait(timeout=10)
        process.close_pipes(managed.process)


def test_real_netns_route_is_independent_of_the_agent_environment(tmp_path: Path) -> None:
    _require_real_netns()
    code = (
        "import json, os, socket\n"
        "before = {k: v for k, v in os.environ.items() if k.lower().endswith('_proxy')}\n"
        "for key in [k for k in os.environ if k.lower().endswith('_proxy')]:\n"
        "    del os.environ[key]\n"
        "first = socket.socket(); first.settimeout(1.0)\n"
        "rc_unset = first.connect_ex(('10.0.0.1', 443)); first.close()\n"
        "os.environ['HTTPS_PROXY'] = 'http://127.0.0.1:9'\n"
        "os.environ['ALL_PROXY'] = 'socks5://127.0.0.1:9'\n"
        "second = socket.socket(); second.settimeout(1.0)\n"
        "rc_override = second.connect_ex(('10.0.0.1', 443)); second.close()\n"
        "print(json.dumps({'before': before, 'rc_unset': rc_unset,\n"
        "                  'rc_override': rc_override}, sort_keys=True))\n"
    )
    plan = _isolated_plan(tmp_path, (process.PROBE_PYTHON, "-c", code))
    returncode, stdout, stderr = _run_isolated(plan)
    assert returncode == 0, stderr
    payload = json.loads(stdout.decode("utf-8"))
    assert set(payload["before"]) == PROXY_ENV_KEYS
    for name in (
        "HTTPS_PROXY",
        "HTTP_PROXY",
        "ALL_PROXY",
        "https_proxy",
        "http_proxy",
        "all_proxy",
    ):
        assert payload["before"][name] == PROXY_URL
    assert payload["before"]["NO_PROXY"] == ""
    # Changing or unsetting the proxy environment cannot create a kernel route.
    assert payload["rc_unset"] in (101, 113)  # ENETUNREACH / EHOSTUNREACH
    assert payload["rc_override"] in (101, 113)


def test_real_netns_attestation_evidence_is_deterministic(tmp_path: Path) -> None:
    _require_real_netns()
    root = process._attestation_workspace()
    try:
        directories = process._prepare_attestation_directories(root)
        profile = process.build_bwrap_argv(
            (process.PROBE_PYTHON,),
            workspace_binds=(),
            ipc_directory=directories["ipc"],
            scratch_directory=directories["scratch"],
            config_directory=directories["config"],
            work_directory=directories["work"],
        )
        command = process._entrypoint_wrapped_argv(
            profile, python_executable=process.PROBE_PYTHON, command=("attest",)
        )
        env = dict(process.ATTESTATION_ENV)
        first = process.subprocess.run(
            command,
            cwd=root,
            env=env,
            stdin=process.subprocess.DEVNULL,
            capture_output=True,
            timeout=30,
            check=False,
        )
        second = process.subprocess.run(
            command,
            cwd=root,
            env=env,
            stdin=process.subprocess.DEVNULL,
            capture_output=True,
            timeout=30,
            check=False,
        )
    finally:
        process.shutil.rmtree(root, ignore_errors=True)
    assert first.returncode == 0, first.stderr
    assert first.stdout == second.stdout
    payload = json.loads(first.stdout.decode("utf-8"))
    assert payload["ok"] is True
    assert payload["ifaces"] == ["lo"]
    assert payload["ipv4_routes"] == 0
    assert payload["ipv6_routes"] == ["::1/128"]
    assert payload["host_loopback"] == "not_checked"
    assert set(payload["blocked"]) == set(process.REQUIRED_BLOCKED_ADDRESSES)
    assert set(payload["blocked"].values()) <= {
        "ENETUNREACH",
        "EHOSTUNREACH",
        "EPERM",
        "EACCES",
    }
