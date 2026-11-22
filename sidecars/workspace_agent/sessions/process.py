"""Run process control: closed launch plans, setsid, process-group signals, exits.

Each run is started in its own session (``start_new_session=True``), so the
recorded pgid equals the child pid and ``killpg`` reaches every descendant.
Interrupt is SIGTERM to the group, a grace period, then SIGKILL. Liveness uses
``/proc/<pid>/stat`` start ticks so a recycled pid is never mistaken for the
recorded run.

Die-with-parent semantics come from the sandboxed profile's bwrap
``--die-with-parent``; the direct ``fake`` profile's CLI sets
``PR_SET_PDEATHSIG`` on itself, and the supervisor additionally kills every
recorded group on restart before marking the session ``lost`` (no adoption).

Normative source: ``docs/design/investigation-workshop/SESSION-RUNNER.md`` §3/§5.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

BWRAP_BINARY = "/usr/bin/bwrap"
BWRAP_PROBE_PATHS: tuple[str, ...] = ("/usr/bin/bwrap", "/bin/bwrap")
SLIRP_BINARY = "/usr/bin/slirp4netns"
SLIRP_PROBE_PATHS: tuple[str, ...] = ("/usr/bin/slirp4netns", "/bin/slirp4netns")
PROC_STAT_START_TICKS_FIELD = 19  # zero-based index after "pid (comm) "
PROC_STAT_STATE_FIELD = 0
MIN_PGID = 2

NETWORK_MODE_ISOLATED_EGRESS = "isolated_egress"
NETWORK_MODE_HOST_NETWORK_UNSAFE = "host_network_unsafe"
CLI_NETWORK_MODES = frozenset({NETWORK_MODE_ISOLATED_EGRESS, NETWORK_MODE_HOST_NETWORK_UNSAFE})
INFO_FD_TIMEOUT_SECONDS = 10.0
ISOLATED_SPAWN_ATTEMPTS = 3
SLIRP_READY_TIMEOUT_SECONDS = 5.0
PROBE_LOOPBACK_TIMEOUT_SECONDS = 4.0
PROBE_PYTHON = "/usr/bin/python3"

# R03 B1/env: an AutoWork operational credential must never enter the CLI
# environment.  The launch env is already a closed literal; this deny list is
# the defense in depth that fails the spawn if a future change copies os.environ.
FORBIDDEN_CLI_ENV_KEYS = frozenset(
    {
        "OPERATOR_API_TOKEN",
        "OPERATOR_CLIENT_API_TOKEN",
        "M24_AGENT_SESSION_KEY",
        "VIRUSTOTAL_API_KEY",
        "VT_API_KEY",
        "SHODAN_API_KEY",
        "BRIDGE_API_KEY",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "DATABASE_URL",
        "REDIS_URL",
        "MINIO_ACCESS_KEY",
        "MINIO_SECRET_KEY",
        "MINIO_ROOT_USER",
        "MINIO_ROOT_PASSWORD",
    }
)
FORBIDDEN_CLI_ENV_SUFFIXES = ("_TOKEN", "_SECRET", "_PASSWORD", "_API_KEY")


class CliEnvironmentError(RuntimeError):
    """The CLI launch environment carries an AutoWork credential key."""


class NetworkIsolationUnavailable(RuntimeError):
    """The isolated CLI network could not be created; the run must not start."""


def cli_environment_violations(env: dict[str, str]) -> tuple[str, ...]:
    """Return the offending key names only; never a value."""
    violations: list[str] = []
    for key in env:
        normalized = key.strip().upper()
        if normalized in FORBIDDEN_CLI_ENV_KEYS or normalized.endswith(FORBIDDEN_CLI_ENV_SUFFIXES):
            violations.append(key)
    return tuple(sorted(violations))


def assert_cli_environment_safe(env: dict[str, str]) -> None:
    violations = cli_environment_violations(env)
    if violations:
        raise CliEnvironmentError(
            "the CLI environment would expose AutoWork credential keys: " + ", ".join(violations)
        )


def network_mode_from_environment() -> str:
    """The closed ``AGENT_SESSIONS_CLI_NETWORK_MODE`` value (default isolated)."""
    value = os.environ.get("AGENT_SESSIONS_CLI_NETWORK_MODE", "").strip()
    if not value:
        return NETWORK_MODE_ISOLATED_EGRESS
    if value not in CLI_NETWORK_MODES:
        raise ValueError("AGENT_SESSIONS_CLI_NETWORK_MODE must be a closed network mode")
    return value


@dataclass(frozen=True)
class LaunchPlan:
    """A fixed, supervisor-built launch; clients never supply argv or binary.

    ``stdin_data`` is a one-shot prompt for one-shot CLIs (Codex ``exec``): it is
    fed before the pipe is closed, so a live-instruction write fails and queues
    instead of corrupting the prompt stream. ``None`` keeps the pipe open.

    ``network_mode`` is the closed R03 B1 mode: ``isolated_egress`` runs the
    sandbox in its own network namespace behind a slirp4netns gateway with the
    host loopback disabled; ``host_network_unsafe`` is the explicit development
    override (same host network).  Fake profiles never use bwrap and are not
    affected.
    """

    argv: tuple[str, ...]
    env: dict[str, str]
    cwd: Path
    uses_bwrap: bool = False
    network_mode: str = NETWORK_MODE_ISOLATED_EGRESS
    stdin_data: bytes | None = None


@dataclass
class ManagedProcess:
    """One spawned run and its process-group identity."""

    process: subprocess.Popen[bytes]
    run: int
    pid: int
    pgid: int
    start_ticks: int
    argv: tuple[str, ...] = field(default_factory=tuple)
    slirp: subprocess.Popen[bytes] | None = None
    network_exit_fd: int | None = None


def spawn(plan: LaunchPlan) -> ManagedProcess:
    """Start a run in a new session with unbuffered pipes and a closed env."""
    assert_cli_environment_safe(plan.env)
    if plan.network_mode not in CLI_NETWORK_MODES:
        raise NetworkIsolationUnavailable("the launch plan carries an unknown network mode")
    if plan.uses_bwrap and plan.network_mode == NETWORK_MODE_ISOLATED_EGRESS:
        # bwrap's unprivileged user namespace is briefly not joinable by
        # slirp4netns (setns EPERM) on some hosts.  A failed attempt never
        # started the CLI and leaves no side effect, so a fresh sandbox may be
        # retried; the caller still fails closed after the bound.
        last_error: NetworkIsolationUnavailable | None = None
        for _attempt in range(ISOLATED_SPAWN_ATTEMPTS):
            try:
                return _spawn_isolated(plan)
            except NetworkIsolationUnavailable as exc:
                last_error = exc
        assert last_error is not None
        raise last_error
    return _spawn_direct(plan)


def _spawn_direct(plan: LaunchPlan) -> ManagedProcess:
    process = subprocess.Popen(
        plan.argv,
        cwd=plan.cwd,
        env=plan.env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
        close_fds=True,
        bufsize=0,
    )
    if plan.stdin_data is not None:
        _feed_stdin(process, plan.stdin_data)
    return _managed(process, plan, run=0)


def _spawn_isolated(plan: LaunchPlan) -> ManagedProcess:
    """Launch bwrap in its own netns, attach slirp4netns, then let it exec.

    ``bwrap --block-fd`` holds the command before ``exec`` while the supervisor
    reads ``--info-fd`` (host pid in the new net namespace), starts
    ``slirp4netns --disable-host-loopback`` and waits for it to be ready.  A
    failure kills the sandbox and refuses the run; there is no unisolated
    fallback.
    """
    info_r, info_w = os.pipe()
    block_r, block_w = os.pipe()
    argv = list(plan.argv)
    if not argv:
        raise NetworkIsolationUnavailable("the isolated launch has no argv")
    argv[1:1] = ["--info-fd", str(info_w), "--block-fd", str(block_r)]
    process: subprocess.Popen[bytes] | None = None
    slirp: subprocess.Popen[bytes] | None = None
    network_exit_fd: int | None = None
    try:
        process = subprocess.Popen(
            argv,
            cwd=plan.cwd,
            env=plan.env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            close_fds=True,
            bufsize=0,
            pass_fds=(info_w, block_r),
        )
        os.close(info_w)
        info_w = -1
        os.close(block_r)
        block_r = -1
        child_pid = _read_child_pid(process, info_r)
        _close_quietly(info_r)
        info_r = -1
        slirp, ready_r, network_exit_fd = _start_slirp(child_pid)
        _wait_slirp_ready(slirp, ready_r)
        os.write(block_w, b"1")
        os.close(block_w)
        block_w = -1
    except BaseException:
        for fd in (info_r, info_w, block_r, block_w):
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass
        if slirp is not None:
            _stop_aux(slirp, network_exit_fd)
        if process is not None:
            try:
                os.killpg(os.getpgid(process.pid), signal.SIGKILL)
            except OSError:
                pass
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            close_pipes(process)
        raise
    if plan.stdin_data is not None and process.stdin is not None:
        _feed_stdin(process, plan.stdin_data)
    managed = _managed(process, plan, run=0)
    managed.slirp = slirp
    managed.network_exit_fd = network_exit_fd
    return managed


def _managed(process: subprocess.Popen[bytes], plan: LaunchPlan, *, run: int) -> ManagedProcess:
    pid = process.pid
    pgid = os.getpgid(pid)
    ticks = proc_start_ticks(pid)
    if ticks is None:
        # The child is alive (we just started it); a missing /proc entry means
        # the platform cannot prove identity. Refuse to manage it blindly.
        terminate_group(pgid, grace_seconds=0.0)
        raise OSError("cannot read the child process start ticks")
    return ManagedProcess(
        process=process,
        run=run,
        pid=pid,
        pgid=pgid,
        start_ticks=ticks,
        argv=plan.argv,
    )


def _read_child_pid(process: subprocess.Popen[bytes], info_r: int) -> int:
    """Read bwrap's ``--info-fd`` JSON (bounded) before it is allowed to exec."""
    deadline = time.monotonic() + INFO_FD_TIMEOUT_SECONDS
    os.set_blocking(info_r, False)
    data = bytearray()
    while time.monotonic() < deadline:
        try:
            chunk = os.read(info_r, 65536)
        except BlockingIOError:
            chunk = None
        if chunk:
            data.extend(chunk)
            if b"}" in data and data.rstrip().endswith(b"}"):
                break
        elif chunk == b"":
            break
        elif process.poll() is not None:
            break
        else:
            time.sleep(0.001)
    try:
        payload = json.loads(data.decode("utf-8"))
        child_pid = payload["child-pid"]
        if isinstance(child_pid, bool) or not isinstance(child_pid, int) or child_pid < 2:
            raise ValueError("child-pid is not a usable pid")
        return child_pid
    except (ValueError, KeyError, OSError) as exc:
        raise NetworkIsolationUnavailable(
            "the sandbox did not report its network-namespace child"
        ) from exc


def _close_quietly(fd: int) -> None:
    try:
        os.close(fd)
    except OSError:
        pass


def _start_slirp(child_pid: int) -> tuple[subprocess.Popen[bytes], int, int]:
    slirp_binary = default_slirp_binary()
    if not Path(slirp_binary).is_file():
        raise NetworkIsolationUnavailable("the slirp4netns binary is missing")
    ready_r, ready_w = os.pipe()
    exit_r, exit_w = os.pipe()
    try:
        slirp = subprocess.Popen(
            [
                slirp_binary,
                "--configure",
                "--disable-host-loopback",
                "--mtu=65520",
                "--ready-fd",
                str(ready_w),
                "--exit-fd",
                str(exit_r),
                str(child_pid),
                "tap0",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            pass_fds=(ready_w, exit_r),
            close_fds=True,
        )
    except OSError as exc:
        _close_quietly(ready_r)
        _close_quietly(exit_w)
        raise NetworkIsolationUnavailable("slirp4netns could not be started") from exc
    finally:
        _close_quietly(ready_w)
        _close_quietly(exit_r)
    return slirp, ready_r, exit_w


def _wait_slirp_ready(slirp: subprocess.Popen[bytes], ready_r: int) -> None:
    deadline = time.monotonic() + SLIRP_READY_TIMEOUT_SECONDS
    os.set_blocking(ready_r, False)
    try:
        while time.monotonic() < deadline:
            try:
                marker = os.read(ready_r, 16)
            except BlockingIOError:
                marker = None
            if marker:
                return
            if marker == b"" or slirp.poll() is not None:
                break
            time.sleep(0.001)
        raise NetworkIsolationUnavailable("slirp4netns did not attest a ready isolated network")
    finally:
        try:
            os.close(ready_r)
        except OSError:
            pass


def _stop_aux(slirp: subprocess.Popen[bytes], network_exit_fd: int | None) -> None:
    """Terminate the gateway: closing the exit fd makes slirp4netns leave."""
    if network_exit_fd is not None:
        try:
            os.close(network_exit_fd)
        except OSError:
            pass
    if slirp.poll() is None:
        try:
            slirp.wait(timeout=2)
        except subprocess.TimeoutExpired:
            slirp.kill()
            try:
                slirp.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
    for stream in (slirp.stderr,):
        if stream is not None:
            try:
                stream.close()
            except OSError:
                pass


def stop_network(managed: ManagedProcess) -> None:
    """Release the per-run slirp4netns gateway (idempotent)."""
    slirp = managed.slirp
    exit_fd = managed.network_exit_fd
    managed.slirp = None
    managed.network_exit_fd = None
    if slirp is not None:
        _stop_aux(slirp, exit_fd)
    elif exit_fd is not None:
        try:
            os.close(exit_fd)
        except OSError:
            pass


def _feed_stdin(process: subprocess.Popen[bytes], data: bytes) -> None:
    """Feed a one-shot prompt off-thread (a large prompt must not block spawn)."""

    def _write() -> None:
        stream = process.stdin
        if stream is None:
            return
        try:
            stream.write(data)
            stream.flush()
        except (OSError, ValueError):
            pass
        finally:
            try:
                stream.close()
            except OSError:
                pass

    threading.Thread(target=_write, name="session-stdin", daemon=True).start()


def close_pipes(process: subprocess.Popen[bytes]) -> None:
    """Close the parent's pipe ends (idempotent)."""
    for stream in (process.stdin, process.stdout, process.stderr):
        if stream is None:
            continue
        try:
            stream.close()
        except OSError:
            pass


def proc_start_ticks(pid: int) -> int | None:
    """Field 22 of ``/proc/<pid>/stat`` (clock ticks since boot), or ``None``."""
    text = _read_stat(pid)
    if text is None:
        return None
    fields = _stat_fields(text)
    if len(fields) <= PROC_STAT_START_TICKS_FIELD:
        return None
    try:
        return int(fields[PROC_STAT_START_TICKS_FIELD])
    except ValueError:
        return None


def proc_state(pid: int) -> str | None:
    text = _read_stat(pid)
    if text is None:
        return None
    fields = _stat_fields(text)
    return fields[PROC_STAT_STATE_FIELD] if fields else None


def process_alive(pid: int, start_ticks: int | None) -> bool:
    """True when ``pid`` exists, is not a zombie, and matches ``start_ticks``."""
    if pid < 1:
        return False
    state = proc_state(pid)
    if state is None or state == "Z":
        return False
    if start_ticks is not None:
        current = proc_start_ticks(pid)
        if current is None or current != start_ticks:
            return False
    return True


def group_exists(pgid: int) -> bool:
    if pgid < MIN_PGID:
        return False
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def signal_group(pgid: int, signum: int) -> bool:
    """Signal a process group; refuse pgid 0/1 and report whether it exists."""
    if pgid < MIN_PGID:
        return False
    try:
        os.killpg(pgid, signum)
    except ProcessLookupError:
        return False
    except PermissionError:
        return False
    return True


def terminate_group(pgid: int, *, grace_seconds: float) -> bool:
    """SIGTERM the group, wait up to ``grace_seconds``, then SIGKILL it."""
    if not signal_group(pgid, signal.SIGTERM):
        return False
    deadline = time.monotonic() + max(0.0, grace_seconds)
    while time.monotonic() < deadline:
        if not group_exists(pgid):
            return True
        time.sleep(0.01)
    signal_group(pgid, signal.SIGKILL)
    return True


def build_bwrap_argv(
    argv: tuple[str, ...],
    *,
    workspace_binds: tuple[Path, ...],
    ipc_directory: Path,
    scratch_directory: Path,
    config_directory: Path,
    work_directory: Path,
    bwrap: Path | None = None,
    extra_ro_binds: tuple[tuple[Path, Path], ...] = (),
    extra_rw_binds: tuple[tuple[Path, Path], ...] = (),
    network_mode: str = NETWORK_MODE_ISOLATED_EGRESS,
) -> list[str]:
    """The reasoning-session bwrap profile (§3): namespaces, ro binds.

    In ``isolated_egress`` the profile adds ``--unshare-net``; the supervisor's
    ``spawn`` then attaches the slirp4netns gateway (host loopback disabled)
    before the command may exec (R03 B1). ``host_network_unsafe`` keeps the
    host network and is the explicit development override.  The ``fake``
    profile launches directly and never uses this builder. ``extra_ro_binds``
    and ``extra_rw_binds`` are ``(source, target)`` pairs for the per-session
    skills exposure and private HOME, so the closed profile stays the only
    place that decides what the model may see.
    """
    if network_mode not in CLI_NETWORK_MODES:
        raise ValueError("network_mode must be isolated_egress or host_network_unsafe")
    executable = str(bwrap if bwrap is not None else default_bwrap_binary())
    command = [
        executable,
        "--unshare-pid",
        "--unshare-ipc",
        "--unshare-uts",
        "--die-with-parent",
    ]
    if network_mode == NETWORK_MODE_ISOLATED_EGRESS:
        command.append("--unshare-net")
    command.extend(
        (
            # The new tmpfs root comes first: session paths may live under /tmp,
            # and a later --tmpfs /tmp would hide mounts created before it.
            "--tmpfs",
            "/tmp",
            "--proc",
            "/proc",
            "--dev",
            "/dev",
            "--ro-bind",
            "/usr",
            "/usr",
        )
    )
    for system_path in ("/lib", "/lib64"):
        if Path(system_path).exists():
            command.extend(("--ro-bind", system_path, system_path))
    for path in workspace_binds:
        command.extend(("--ro-bind", str(path), str(path)))
    command.extend(("--ro-bind", str(ipc_directory), str(ipc_directory)))
    for source, target in extra_ro_binds:
        command.extend(("--ro-bind", str(source), str(target)))
    for source, target in extra_rw_binds:
        command.extend(("--bind", str(source), str(target)))
    command.extend(("--bind", str(scratch_directory), str(scratch_directory)))
    command.extend(("--bind", str(config_directory), str(config_directory)))
    command.extend(("--chdir", str(work_directory), "--", *argv))
    return command


def default_bwrap_binary() -> str:
    for candidate in BWRAP_PROBE_PATHS:
        if Path(candidate).is_file():
            return candidate
    located = shutil.which("bwrap")
    return located if located is not None else BWRAP_BINARY


def bwrap_available() -> bool:
    return Path(default_bwrap_binary()).is_file()


def default_slirp_binary() -> str:
    for candidate in SLIRP_PROBE_PATHS:
        if Path(candidate).is_file():
            return candidate
    located = shutil.which("slirp4netns")
    return located if located is not None else SLIRP_BINARY


def slirp_available() -> bool:
    return Path(default_slirp_binary()).is_file()


@dataclass(frozen=True)
class NetworkIsolationAttestation:
    """What the host could prove about the isolated CLI network (R03 B1)."""

    ok: bool
    reason: str
    network_mode: str
    loopback_isolated: bool


_LOOPBACK_HELPER_CODE = (
    "import http.server, sys, threading\n"
    "seen = [False]\n"
    "class Handler(http.server.BaseHTTPRequestHandler):\n"
    "    def do_GET(self):\n"
    "        seen[0] = True\n"
    "        self.send_response(200)\n"
    "        self.end_headers()\n"
    "        self.wfile.write(b'ok')\n"
    "    def log_message(self, *args):\n"
    "        pass\n"
    "server = http.server.HTTPServer(('127.0.0.1', 0), Handler)\n"
    "threading.Thread(target=server.handle_request, daemon=True).start()\n"
    "print(server.server_port, flush=True)\n"
    "sys.stdin.readline()\n"
    "print('ACCEPTED' if seen[0] else 'NO_REQUEST', flush=True)\n"
)


def _attestation(ok: bool, reason: str) -> NetworkIsolationAttestation:
    return NetworkIsolationAttestation(ok, reason, NETWORK_MODE_ISOLATED_EGRESS, ok)


def _stop_loopback_helper(helper: subprocess.Popen[bytes]) -> str:
    """Release the host listener only after the sandbox attempt finished."""
    stream, helper.stdin = helper.stdin, None
    if stream is not None:
        try:
            stream.write(b"stop\n")
            stream.flush()
            stream.close()
        except (OSError, ValueError):
            pass
    output = b""
    try:
        helper.wait(timeout=5)
        if helper.stdout is not None:
            output = helper.stdout.read() or b""
    except subprocess.TimeoutExpired:
        helper.kill()
        helper.wait()
        return "TIMEOUT"
    return output.decode("utf-8", "replace")


def network_isolation_attestation(
    *, python_executable: str = PROBE_PYTHON
) -> NetworkIsolationAttestation:
    """Prove on this host that the sandbox cannot reach the host loopback.

    A short-lived host helper (a separate ``python3`` process, so the supervisor
    itself never opens an INET socket) binds the host loopback and stays
    listening until the sandbox attempt returns; the sandbox runs the same
    bwrap + slirp4netns launch the CLI will use and tries to reach it.  Success
    requires a refused connection, no accepted connection and a real tap
    interface: no provider or external traffic is involved.  Any failure is a
    refusal, never an unconfined fallback.
    """
    if not bwrap_available():
        return _attestation(False, "bwrap binary missing")
    if not slirp_available():
        return _attestation(False, "slirp4netns binary missing")
    if not Path(python_executable).is_file():
        return _attestation(False, "probe interpreter missing")
    helper = subprocess.Popen(
        [python_executable, "-c", _LOOPBACK_HELPER_CODE],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )
    helper_output: str | None = None
    try:
        assert helper.stdout is not None
        port_line = helper.stdout.readline(32).decode("utf-8", "replace").strip()
        if not port_line.isdigit():
            return _attestation(False, "the loopback host helper did not start")
        port = int(port_line)
        if not 0 < port < 65536:
            return _attestation(False, "the loopback host helper reported no port")
        code = (
            "import urllib.request\n"
            "try:\n"
            f"    urllib.request.urlopen('http://127.0.0.1:{port}/', timeout=2).close()\n"
            "    print('LOOPBACK_REACHABLE')\n"
            "except Exception:\n"
            "    print('LOOPBACK_BLOCKED')\n"
            "import socket\n"
            "print('IFACES ' + ','.join(name for _, name in socket.if_nameindex()))\n"
        )
        try:
            managed = spawn(
                LaunchPlan(
                    argv=tuple(_probe_bwrap_argv(python_executable, code)),
                    env={
                        "PATH": "/usr/bin:/bin",
                        "HOME": "/tmp",
                        "LANG": "C.UTF-8",
                        "LC_ALL": "C.UTF-8",
                    },
                    cwd=Path("/tmp"),
                    uses_bwrap=True,
                    network_mode=NETWORK_MODE_ISOLATED_EGRESS,
                )
            )
        except (NetworkIsolationUnavailable, CliEnvironmentError, OSError) as exc:
            return _attestation(
                False, f"the isolated network could not be created ({type(exc).__name__})"
            )
        probe_timeout = PROBE_LOOPBACK_TIMEOUT_SECONDS + 6
        probe_completed = False
        try:
            stdout, _stderr = managed.process.communicate(timeout=probe_timeout)
            probe_completed = True
        finally:
            stop_network(managed)
            close_pipes(managed.process)
            if not probe_completed:
                terminate_group(managed.pgid, grace_seconds=1.0)
            helper_output = _stop_loopback_helper(helper)
        if not probe_completed:
            return _attestation(False, "the network probe did not finish")
        if helper_output == "TIMEOUT":
            return _attestation(False, "the loopback host listener did not stop cleanly")
        text = stdout.decode("utf-8", "replace")
        if "ACCEPTED" in helper_output:
            return _attestation(False, "the sandbox reached the host loopback")
        if "LOOPBACK_BLOCKED" not in text:
            return _attestation(False, "the sandbox did not prove the loopback refusal")
        if "tap0" not in text:
            return _attestation(False, "the sandbox has no egress interface")
        return _attestation(True, "host loopback blocked; provider egress through slirp4netns")
    finally:
        if helper.poll() is None:
            helper.kill()
            try:
                helper.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
        if helper.stdout is not None:
            try:
                helper.stdout.close()
            except OSError:
                pass


def _probe_bwrap_argv(python_executable: str, code: str) -> list[str]:
    argv = [
        default_bwrap_binary(),
        "--unshare-pid",
        "--unshare-ipc",
        "--unshare-uts",
        "--die-with-parent",
        "--unshare-net",
        "--tmpfs",
        "/tmp",
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        "--ro-bind",
        "/usr",
        "/usr",
    ]
    for system_path in ("/lib", "/lib64"):
        if Path(system_path).exists():
            argv.extend(("--ro-bind", system_path, system_path))
    argv.extend(("--chdir", "/tmp", "--", python_executable, "-c", code))
    return argv


def _read_stat(pid: int) -> str | None:
    if pid < 1:
        return None
    try:
        return Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _stat_fields(text: str) -> list[str]:
    closing = text.rfind(")")
    if closing < 0:
        return []
    return text[closing + 2 :].split()
