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
PROC_STAT_START_TICKS_FIELD = 19  # zero-based index after "pid (comm) "
PROC_STAT_STATE_FIELD = 0
MIN_PGID = 2


@dataclass(frozen=True)
class LaunchPlan:
    """A fixed, supervisor-built launch; clients never supply argv or binary.

    ``stdin_data`` is a one-shot prompt for one-shot CLIs (Codex ``exec``): it is
    fed before the pipe is closed, so a live-instruction write fails and queues
    instead of corrupting the prompt stream. ``None`` keeps the pipe open.
    """

    argv: tuple[str, ...]
    env: dict[str, str]
    cwd: Path
    uses_bwrap: bool = False
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


def spawn(plan: LaunchPlan) -> ManagedProcess:
    """Start a run in a new session with unbuffered pipes and a closed env."""
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
    pid = process.pid
    pgid = os.getpgid(pid)
    ticks = proc_start_ticks(pid)
    if ticks is None:
        # The child is alive (we just started it); a missing /proc entry means
        # the platform cannot prove identity. Refuse to manage it blindly.
        terminate_group(pgid, grace_seconds=0.0)
        raise OSError("cannot read the child process start ticks")
    return ManagedProcess(process=process, run=0, pid=pid, pgid=pgid, start_ticks=ticks)


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
) -> list[str]:
    """The reasoning-session bwrap profile (§3): namespaces, no netns, ro binds.

    This builder is the registration seam for the Codex/Claude profiles (H4/H5);
    the ``fake`` profile launches directly and never uses it. ``extra_ro_binds``
    and ``extra_rw_binds`` are ``(source, target)`` pairs for the per-session
    skills exposure and private HOME, so the closed profile stays the only
    place that decides what the model may see.
    """
    executable = str(bwrap if bwrap is not None else default_bwrap_binary())
    command = [
        executable,
        "--unshare-pid",
        "--unshare-ipc",
        "--unshare-uts",
        "--die-with-parent",
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
    ]
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
