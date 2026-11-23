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

The ``isolated_egress`` profile has no slirp4netns gateway and no ``tap0``: the
namespace keeps only ``lo`` (design §5).  ``spawn`` first runs ``entrypoint.py
attest`` in a fresh namespace with the same bwrap profile (cached per bwrap
binary hash for this process lifetime) and refuses with a typed
``confinement_unavailable`` error when the proof is missing; then it releases a
bwrap launch whose CLI argv is wrapped by ``entrypoint.py forward-and-exec``,
which listens on ``127.0.0.1:3128`` inside the namespace and byte-pipes to the
session's ``ipc/egress.sock``.  There is no degraded or host-network fallback.

Normative source:
``docs/design/investigation-workshop/BROKER-RELAY-EGRESS.md`` §5-§7 and
``docs/design/investigation-workshop/SESSION-RUNNER.md`` §3/§5.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import stat
import subprocess
import threading
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from .entrypoint import ATTEST_SCHEMA, REQUIRED_BLOCKED_ADDRESSES

BWRAP_BINARY = "/usr/bin/bwrap"
BWRAP_PROBE_PATHS: tuple[str, ...] = ("/usr/bin/bwrap", "/bin/bwrap")
PROC_STAT_START_TICKS_FIELD = 19  # zero-based index after "pid (comm) "
PROC_STAT_STATE_FIELD = 0
MIN_PGID = 2

NETWORK_MODE_ISOLATED_EGRESS = "isolated_egress"
# Isolation is the only mode (M24.1 LIFE-1).  The former
# ``host_network_unsafe`` development override is gone: no launch may share the
# host network, and an old environment value fails startup explicitly.
CLI_NETWORK_MODES = frozenset({NETWORK_MODE_ISOLATED_EGRESS})
PROBE_PYTHON = "/usr/bin/python3"
ENTRYPOINT_FILE_NAME = "entrypoint.py"
EGRESS_SOCKET_FILE_NAME = "egress.sock"
ATTESTATION_PROBE_TIMEOUT_SECONDS = 10.0
ATTESTATION_DIRECTORY_PREFIX = "autowork-netns-attest-"
AF_UNIX_PATH_MAX_BYTES = 107  # sockaddr_un.sun_path is 108 bytes including the NUL
ATTESTATION_ENV = {
    "PATH": "/usr/bin:/bin",
    "HOME": "/tmp",
    "LANG": "C.UTF-8",
    "LC_ALL": "C.UTF-8",
}
BLOCKED_ERRNO_NAMES = frozenset({"ENETUNREACH", "EHOSTUNREACH", "EPERM", "EACCES"})
ATTESTATION_EVIDENCE_KEYS = frozenset(
    {
        "schema",
        "ok",
        "ifaces",
        "lo_flags",
        "ipv4_routes",
        "ipv6_routes",
        "ipv6_reject_routes",
        "loopback",
        "blocked",
        "host_loopback",
        "errors",
    }
)
ATTESTATION_LOOPBACK_KEYS = frozenset({"v4_bind", "v4_connect", "v6_bind", "v6_connect"})
ATTESTATION_FAILURE_REASONS = frozenset(
    {
        "unexpected_interfaces",
        "lo_flags_unreadable",
        "lo_not_up_and_loopback",
        "unexpected_ipv4_route",
        "ipv6_route_unreadable",
        "ipv6_route_unparsable",
        "unexpected_ipv6_route",
        "loopback_v4_bind_failed",
        "loopback_v4_connect_failed",
        "loopback_v4_probe_failed",
        "loopback_v6_bind_failed",
        "loopback_v6_connect_failed",
        "loopback_v6_probe_failed",
        "host_loopback_reachable",
        "attestation_crashed",
    }
) | {f"reachable_address:{address}" for address in REQUIRED_BLOCKED_ADDRESSES}

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
    """The isolated CLI network could not be created; the run must not start.

    ``code`` is the stable fail-closed refusal the backend records
    (``confinement_unavailable``); there is no degraded or host-network
    fallback.
    """

    code = "confinement_unavailable"


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
    """The closed ``AGENT_SESSIONS_CLI_NETWORK_MODE`` value (always isolated)."""
    value = os.environ.get("AGENT_SESSIONS_CLI_NETWORK_MODE", "").strip()
    if not value:
        return NETWORK_MODE_ISOLATED_EGRESS
    if value == "host_network_unsafe":
        raise ValueError(
            "AGENT_SESSIONS_CLI_NETWORK_MODE=host_network_unsafe is no longer supported: "
            "agent CLI sessions must run in an isolated network namespace (isolated_egress)"
        )
    if value not in CLI_NETWORK_MODES:
        raise ValueError("AGENT_SESSIONS_CLI_NETWORK_MODE must be isolated_egress")
    return value


@dataclass(frozen=True)
class LaunchPlan:
    """A fixed, supervisor-built launch; clients never supply argv or binary.

    ``stdin_data`` is a one-shot prompt for one-shot CLIs (Codex ``exec``): it is
    fed before the pipe is closed, so a live-instruction write fails and queues
    instead of corrupting the prompt stream. ``None`` keeps the pipe open.

    ``network_mode`` is the closed R03 B1 mode: ``isolated_egress`` runs the
    sandbox in its own network namespace with only ``lo`` and the trusted
    entrypoint's provider forwarder.  It is the only accepted value (M24.1
    LIFE-1); fake profiles never use bwrap and are not affected.

    ``egress_socket_path`` is the session's ``ipc/egress.sock``; it defaults to
    ``<ipc directory>/egress.sock`` as found in the bwrap profile, so existing
    profiles keep working.  LIFE-1 wires it explicitly.
    """

    argv: tuple[str, ...]
    env: dict[str, str]
    cwd: Path
    uses_bwrap: bool = False
    network_mode: str = NETWORK_MODE_ISOLATED_EGRESS
    stdin_data: bytes | None = None
    egress_socket_path: Path | None = None


@dataclass
class ManagedProcess:
    """One spawned run and its process-group identity."""

    process: subprocess.Popen[bytes]
    run: int
    pid: int
    pgid: int
    start_ticks: int
    argv: tuple[str, ...] = field(default_factory=tuple)


def spawn(plan: LaunchPlan) -> ManagedProcess:
    """Start a run in a new session with unbuffered pipes and a closed env."""
    assert_cli_environment_safe(plan.env)
    if plan.network_mode not in CLI_NETWORK_MODES:
        raise NetworkIsolationUnavailable("the launch plan carries an unknown network mode")
    if plan.uses_bwrap:
        return _spawn_isolated(plan)
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
    """Attest a fresh netns, then run the CLI behind the trusted entrypoint.

    The attestation gate runs before any CLI process is released.  A missing
    bwrap/user namespace, a missing entrypoint or a failed proof raises
    ``NetworkIsolationUnavailable`` (``confinement_unavailable``); there is no
    degraded or host-network launch.  The bwrap command then starts
    ``entrypoint.py forward-and-exec``, which owns the only network path in the
    namespace: ``127.0.0.1:3128`` byte-piped to the session egress UDS.
    """
    if not plan.argv:
        raise NetworkIsolationUnavailable("the isolated launch has no argv")
    attestation = network_isolation_attestation(bwrap=Path(plan.argv[0]))
    if not attestation.ok:
        raise NetworkIsolationUnavailable("network attestation refused: " + attestation.reason)
    egress_socket = egress_socket_for_plan(plan)
    if egress_socket is None:
        raise NetworkIsolationUnavailable("the isolated launch names no egress proxy socket")
    argv = isolated_entry_argv(plan.argv, egress_socket_path=egress_socket)
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
        )
    except OSError as exc:
        raise NetworkIsolationUnavailable("the sandbox could not be started") from exc
    if plan.stdin_data is not None:
        _feed_stdin(process, plan.stdin_data)
    return _managed(process, plan, run=0)


def entrypoint_path() -> Path:
    """The trusted in-sandbox entrypoint source (mounted read-only by bwrap)."""
    return Path(__file__).with_name(ENTRYPOINT_FILE_NAME).resolve()


def _trusted_entrypoint() -> Path:
    """The entrypoint must be trusted regular code, never world writable.

    The production install uses 0700; a checkout's group bit follows the
    repository umask, so only a world-writable file is refused here.  The
    mount is read-only either way.
    """

    entrypoint = entrypoint_path()
    try:
        info = entrypoint.stat()
    except OSError as exc:
        raise NetworkIsolationUnavailable("the session entrypoint is missing") from exc
    if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o002:
        raise NetworkIsolationUnavailable("the session entrypoint is not trusted code")
    return entrypoint


def _ro_bound_pairs(argv: Sequence[str]) -> list[tuple[Path, Path]]:
    values = list(argv)
    try:
        separator = values.index("--")
    except ValueError:
        return []
    options = values[:separator]
    pairs: list[tuple[Path, Path]] = []
    for index in range(len(options) - 2):
        if options[index] == "--ro-bind":
            pairs.append((Path(options[index + 1]), Path(options[index + 2])))
    return pairs


def egress_socket_from_bwrap_argv(argv: Sequence[str]) -> Path | None:
    """Find the ro-bound ``ipc`` directory and name ``<ipc>/egress.sock``.

    Profiles built by ``build_bwrap_argv`` always mount the session's ipc
    directory at its own absolute path; the egress proxy socket lives there.
    This is the fallback; ``egress_socket_for_plan`` prefers the directory of
    the session secret socket, which cannot be confused with a workspace path.
    """
    for source, target in _ro_bound_pairs(argv):
        if source == target and source.name == "ipc":
            return source / EGRESS_SOCKET_FILE_NAME
    return None


def egress_socket_for_plan(plan: LaunchPlan) -> Path | None:
    """The per-session egress socket: ``<ipc>/egress.sock`` next to shim.sock.

    The trusted adapter's closed environment names the session secret socket
    (``AUTOWORK_SESSION_SOCKET``); the egress proxy lives in the same ro-bound
    ipc directory.  The explicit ``LaunchPlan.egress_socket_path`` wins (LIFE-1
    wires it), and the profile scan is the last resort.
    """
    if plan.egress_socket_path is not None:
        return plan.egress_socket_path
    secret_socket = plan.env.get("AUTOWORK_SESSION_SOCKET", "").strip()
    if secret_socket:
        directory = Path(secret_socket).parent
        if any(source == target == directory for source, target in _ro_bound_pairs(plan.argv)):
            return directory / EGRESS_SOCKET_FILE_NAME
    return egress_socket_from_bwrap_argv(plan.argv)


def require_egress_socket(path: Path) -> None:
    """Fail closed unless the per-session egress proxy socket exists."""

    if not path.is_absolute():
        raise NetworkIsolationUnavailable("the egress proxy socket path is not absolute")
    if len(os.fsencode(str(path))) > AF_UNIX_PATH_MAX_BYTES:
        raise NetworkIsolationUnavailable("the egress proxy socket path is too long")
    try:
        info = path.lstat()
    except OSError as exc:
        raise NetworkIsolationUnavailable("the session egress proxy socket is missing") from exc
    if not stat.S_ISSOCK(info.st_mode):
        raise NetworkIsolationUnavailable("the session egress proxy is not a unix socket")


def _entrypoint_wrapped_argv(
    argv: Sequence[str],
    *,
    python_executable: str,
    command: Sequence[str],
) -> list[str]:
    """Mount the trusted entrypoint read-only and run it in the bwrap profile."""

    values = list(argv)
    try:
        separator = values.index("--")
    except ValueError as exc:
        raise NetworkIsolationUnavailable("the isolated launch has no -- separator") from exc
    entrypoint = _trusted_entrypoint()
    if not Path(python_executable).is_file():
        raise NetworkIsolationUnavailable("the sandbox interpreter is missing")
    return [
        *values[:separator],
        "--ro-bind",
        str(entrypoint),
        str(entrypoint),
        "--",
        python_executable,
        str(entrypoint),
        *command,
    ]


def isolated_entry_argv(
    argv: Sequence[str],
    *,
    egress_socket_path: Path,
    python_executable: str = PROBE_PYTHON,
) -> list[str]:
    """Wrap the adapter's CLI argv with the trusted entrypoint (design §5).

    Adapters keep building their CLI argv unchanged; only the launch layer
    inserts the read-only entrypoint mount and the ``forward-and-exec``
    wrapper.  The wrapper runs in the same bwrap profile and ``exec``s the CLI,
    so the recorded process group and ``killpg`` semantics are unchanged.
    """
    values = list(argv)
    try:
        separator = values.index("--")
    except ValueError as exc:
        raise NetworkIsolationUnavailable("the isolated launch has no -- separator") from exc
    cli_command = values[separator + 1 :]
    if not cli_command:
        raise NetworkIsolationUnavailable("the isolated launch has no argv")
    require_egress_socket(egress_socket_path)
    return _entrypoint_wrapped_argv(
        values,
        python_executable=python_executable,
        command=(
            "forward-and-exec",
            "--upstream",
            str(egress_socket_path),
            "--",
            *cli_command,
        ),
    )


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


def stop_network(managed: ManagedProcess) -> None:
    """Compatibility no-op: the isolated profile has no auxiliary gateway.

    The per-run network state is the bwrap process group itself, which
    ``killpg`` already owns; the trusted entrypoint's forwarder dies with it
    (``PR_SET_PDEATHSIG``).  The supervisor's channel teardown owns the egress
    proxy socket instead.
    """
    del managed


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

    The profile always adds ``--unshare-net`` and leaves the namespace with only
    ``lo``; ``spawn`` attests the namespace and wraps the CLI with the trusted
    ``entrypoint.py`` (design §5).  There is no slirp4netns gateway, no ``tap0``
    and no host-network fallback.  The ``fake`` profile launches directly and
    never uses this builder. ``extra_ro_binds`` and ``extra_rw_binds`` are
    ``(source, target)`` pairs for the per-session skills exposure and private
    HOME, so the closed profile stays the only place that decides what the model
    may see.
    """
    if network_mode not in CLI_NETWORK_MODES:
        raise ValueError("network_mode must be isolated_egress")
    executable = str(bwrap if bwrap is not None else default_bwrap_binary())
    command = [
        executable,
        "--unshare-pid",
        "--unshare-ipc",
        "--unshare-uts",
        "--die-with-parent",
        "--unshare-net",
    ]
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


AttestationProbeRunner = Callable[..., tuple[int | None, bytes]]

_ATTESTATION_LOCK = threading.Lock()
_ATTESTATION_CACHE: dict[tuple[str, ...], NetworkIsolationAttestation] = {}


def clear_network_attestation_cache() -> None:
    """Drop the per-process attestation cache (tests and bwrap upgrades)."""
    with _ATTESTATION_LOCK:
        _ATTESTATION_CACHE.clear()


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _attestation_cache_key(bwrap_path: Path, python_executable: str) -> tuple[str, ...]:
    return (
        str(bwrap_path.resolve()),
        _file_digest(bwrap_path),
        str(Path(python_executable).resolve()),
        _file_digest(entrypoint_path()),
    )


def _run_attestation_probe(
    argv: list[str], *, cwd: Path, env: dict[str, str]
) -> tuple[int | None, bytes]:
    """Run the bwrap attestation command; ``None`` means it could not run."""
    try:
        completed = subprocess.run(
            argv,
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=ATTESTATION_PROBE_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None, b""
    return completed.returncode, completed.stdout


def _attestation_workspace() -> Path:
    root = Path("/tmp") / f"{ATTESTATION_DIRECTORY_PREFIX}{os.getpid()}-{uuid.uuid4().hex}"
    root.mkdir(mode=0o700)
    return root


def _prepare_attestation_directories(root: Path) -> dict[str, Path]:
    """Mirror the session layout: the work directory lives under scratch."""

    directories: dict[str, Path] = {}
    for name in ("ipc", "scratch", "config"):
        path = root / name
        path.mkdir(mode=0o700)
        directories[name] = path
    work = directories["scratch"] / "work"
    work.mkdir(mode=0o700)
    directories["work"] = work
    return directories


def _attestation_refusal_reason(stdout: bytes) -> str:
    """A fixed-vocabulary reason; the sandbox output is never echoed raw."""
    try:
        payload = json.loads(stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return "the netns attestation returned no evidence"
    if isinstance(payload, dict):
        errors = payload.get("errors")
        if isinstance(errors, list):
            for error in errors:
                if isinstance(error, str) and error in ATTESTATION_FAILURE_REASONS:
                    return f"the netns attestation failed ({error})"
    return "the netns attestation failed"


def _validate_attestation_evidence(stdout: bytes) -> NetworkIsolationAttestation:
    """Strictly validate the entrypoint's in-namespace evidence (design §6)."""
    try:
        payload = json.loads(stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return _attestation(False, "the netns attestation returned no evidence")
    if not isinstance(payload, dict) or set(payload) != ATTESTATION_EVIDENCE_KEYS:
        return _attestation(False, "the netns attestation evidence is malformed")
    if payload.get("schema") != ATTEST_SCHEMA or payload.get("ok") is not True:
        return _attestation(False, "the netns attestation did not pass")
    if payload.get("ifaces") != ["lo"]:
        return _attestation(False, "the netns has an unexpected interface")
    flags = payload.get("lo_flags")
    if isinstance(flags, bool) or not isinstance(flags, int) or flags & 0x9 != 0x9:
        return _attestation(False, "the loopback interface is not up")
    if payload.get("ipv4_routes") != 0:
        return _attestation(False, "the netns carries an IPv4 route")
    if payload.get("ipv6_routes") != ["::1/128"]:
        return _attestation(False, "the netns carries a non-loopback IPv6 route")
    rejects = payload.get("ipv6_reject_routes")
    if isinstance(rejects, bool) or not isinstance(rejects, int) or rejects < 0:
        return _attestation(False, "the netns attestation evidence is malformed")
    loopback = payload.get("loopback")
    if (
        not isinstance(loopback, dict)
        or set(loopback) != ATTESTATION_LOOPBACK_KEYS
        or any(value is not True for value in loopback.values())
    ):
        return _attestation(False, "the netns loopback is not usable")
    blocked = payload.get("blocked")
    if (
        not isinstance(blocked, dict)
        or set(blocked) != set(REQUIRED_BLOCKED_ADDRESSES)
        or any(
            blocked.get(address) not in BLOCKED_ERRNO_NAMES
            for address in REQUIRED_BLOCKED_ADDRESSES
        )
    ):
        return _attestation(False, "a non-loopback destination is reachable")
    if payload.get("host_loopback") != "blocked":
        return _attestation(False, "the sandbox reached the host loopback")
    if payload.get("errors") != []:
        return _attestation(False, "the netns attestation reported errors")
    return _attestation(
        True,
        "network namespace attested: only the loopback interface, no route, "
        "loopback usable; host loopback unreachable",
    )


def _probe_network_isolation(
    *,
    bwrap_path: Path,
    python_executable: str,
    runner: AttestationProbeRunner,
) -> NetworkIsolationAttestation:
    helper = subprocess.Popen(
        [python_executable, "-c", _LOOPBACK_HELPER_CODE],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )
    try:
        assert helper.stdout is not None
        port_line = helper.stdout.readline(32).decode("utf-8", "replace").strip()
        if not port_line.isdigit():
            return _attestation(False, "the loopback host helper did not start")
        port = int(port_line)
        if not 0 < port < 65536:
            return _attestation(False, "the loopback host helper reported no port")
        root = _attestation_workspace()
        try:
            directories = _prepare_attestation_directories(root)
            profile = build_bwrap_argv(
                (python_executable,),
                workspace_binds=(),
                ipc_directory=directories["ipc"],
                scratch_directory=directories["scratch"],
                config_directory=directories["config"],
                work_directory=directories["work"],
                bwrap=bwrap_path,
            )
            command = _entrypoint_wrapped_argv(
                profile,
                python_executable=python_executable,
                command=("attest", "--host-loopback-port", str(port)),
            )
            returncode, stdout = runner(command, cwd=root, env=dict(ATTESTATION_ENV))
        finally:
            shutil.rmtree(root, ignore_errors=True)
        helper_output = _stop_loopback_helper(helper)
        if helper_output == "TIMEOUT":
            return _attestation(False, "the loopback host listener did not stop cleanly")
        if "ACCEPTED" in helper_output:
            return _attestation(False, "the sandbox reached the host loopback")
        if "NO_REQUEST" not in helper_output:
            return _attestation(False, "the loopback host listener did not report")
        if returncode != 0:
            return _attestation(False, _attestation_refusal_reason(stdout))
        return _validate_attestation_evidence(stdout)
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


def network_isolation_attestation(
    *,
    python_executable: str = PROBE_PYTHON,
    bwrap: Path | None = None,
    probe_runner: AttestationProbeRunner | None = None,
) -> NetworkIsolationAttestation:
    """Prove this host's isolated CLI network with the production bwrap profile.

    A short-lived host helper (a separate ``python3`` process, so the
    supervisor itself never opens an INET socket) binds the host loopback and
    stays listening; ``entrypoint.py attest`` runs in a fresh bwrap
    ``--unshare-net`` namespace built by the same ``build_bwrap_argv`` profile
    and proves only ``lo``, no route, usable loopback and refused non-loopback
    connects.  Success additionally requires that the host helper accepted
    nothing: no provider, LAN or external traffic is involved.  Results are
    cached per bwrap binary hash (and entrypoint hash) for this process
    lifetime; any failure is a refusal, never an unconfined fallback.
    ``probe_runner`` is a test seam: injected evidence is never cached.
    """
    bwrap_path = Path(bwrap) if bwrap is not None else Path(default_bwrap_binary())
    if not bwrap_path.is_file():
        return _attestation(False, "bwrap binary missing")
    try:
        _trusted_entrypoint()
    except NetworkIsolationUnavailable as exc:
        return _attestation(False, str(exc))
    if not Path(python_executable).is_file():
        return _attestation(False, "probe interpreter missing")
    try:
        cache_key = _attestation_cache_key(bwrap_path, python_executable)
    except OSError:
        return _attestation(False, "the sandbox binaries cannot be read")
    runner = probe_runner if probe_runner is not None else _run_attestation_probe
    with _ATTESTATION_LOCK:
        if probe_runner is None:
            cached = _ATTESTATION_CACHE.get(cache_key)
            if cached is not None:
                return cached
        try:
            attestation = _probe_network_isolation(
                bwrap_path=bwrap_path,
                python_executable=python_executable,
                runner=runner,
            )
        except OSError:
            # A probe workspace or helper that cannot be created is a refusal,
            # never an unconfined fallback.
            attestation = _attestation(False, "the netns attestation could not run")
        if probe_runner is None and attestation.ok:
            _ATTESTATION_CACHE[cache_key] = attestation
        return attestation


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
