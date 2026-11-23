"""Trusted per-execution subreaper for confined agent scripts (M24.1 F2-A).

``analysis_subprocess.run_analysis_subprocess`` spawns one instance of this
module for every supervised script execution.  It exists so the process-tree
boundary is a process the untrusted script cannot outlive:

* it declares itself ``PR_SET_CHILD_SUBREAPER`` before spawning the script, so
  every orphaned descendant -- including a ``setsid``/double-fork daemon -- is
  reparented to it instead of init;
* it applies the kernel rlimits, the optional Landlock filesystem domain and
  the script seccomp filter in the child before ``exec``;
* it samples the complete descendant tree (``/proc/<pid>/.../children``, with
  a ``ppid`` scan fallback) for the process quota and resource usage;
* on every exit path (normal exit, wall deadline, process quota, parent
  SIGTERM) it SIGKILLs and reaps the whole tree, then reports whether the
  absence of survivors was *proven*.

Wire: one JSON config path plus the status fd in argv; the status fd receives
newline-delimited JSON, first ``{"event": "started", "pid": ...}`` and finally
``{"event": "result", ...}``.  The parent treats a missing or unproven result
as a confinement violation, never as success.

Only the standard library is used: this process must be importable and
runnable before the backend package (and its secrets) are involved.
"""

from __future__ import annotations

import ctypes
import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from typing import Any, NamedTuple

from cti_app.infrastructure.landlock import install_landlock_domain
from cti_app.infrastructure.seccomp_network import install_script_seccomp_filter

# Both helpers above are imported before the child bootstrap applies Landlock:
# importing a backend module after the filesystem domain is installed would be
# a read outside the granted runtime roots in a source-checkout test layout.

_PR_SET_DUMPABLE = 4
_PR_SET_CHILD_SUBREAPER = 36
_STATUS_MAX_BYTES = 64 * 1024
_CONFIG_MAX_BYTES = 64 * 1024
_MAX_TRACKED_PIDS = 256
_MAX_TREE_NODES = 4096
_CLOCK_TICKS = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
_PAGE_SIZE = os.sysconf("SC_PAGE_SIZE") if hasattr(os, "sysconf") else 4096

_LIBC = ctypes.CDLL(None, use_errno=True)


class _ConfigError(ValueError):
    """The parent supplied a malformed supervisor configuration."""


class _ProcessStat(NamedTuple):
    ppid: int
    pgrp: int
    state: str
    cpu_ticks: int
    rss_pages: int


class _Limits(NamedTuple):
    memory_bytes: int
    cpu_seconds: int
    file_size_bytes: int
    nofile: int
    nproc: int


class _Filesystem(NamedTuple):
    read_directories: tuple[str, ...]
    read_files: tuple[str, ...]
    write_directories: tuple[str, ...]
    write_files: tuple[str, ...]


class _Config(NamedTuple):
    execution_id: str
    argv: tuple[str, ...]
    cwd: str | None
    environment: dict[str, str]
    pass_fds: tuple[int, ...]
    deny_network: bool
    limits: _Limits
    filesystem: _Filesystem | None
    wall_deadline_seconds: float
    process_limit: int
    sweep_deadline_seconds: float


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv if argv is None else argv
    if len(arguments) != 3:
        return 2
    config_path, status_fd_text = arguments[1], arguments[2]
    try:
        status_fd = int(status_fd_text)
    except ValueError:
        return 2
    try:
        config = _read_config(config_path)
    except _ConfigError as exc:
        _report(status_fd, {"event": "result", "confinement_error": str(exc)})
        return 70
    except OSError:
        _report(status_fd, {"event": "result", "confinement_error": "supervisor_config_missing"})
        return 70

    if sys.platform.startswith("linux"):
        try:
            _prctl(_PR_SET_CHILD_SUBREAPER, 1)
            _prctl(_PR_SET_DUMPABLE, 0)
        except OSError as exc:
            label = str(exc.args[1]) if len(exc.args) > 1 else "prctl"
            error = "subreaper_unavailable" if label == "subreaper" else "dumpable_unavailable"
            _report(status_fd, {"event": "result", "confinement_error": error})
            return 70
    else:
        _report(status_fd, {"event": "result", "confinement_error": "supervisor_requires_linux"})
        return 70

    terminate_requested = False

    def request_termination(_signum: int, _frame: object) -> None:
        nonlocal terminate_requested
        terminate_requested = True

    signal.signal(signal.SIGTERM, request_termination)
    signal.signal(signal.SIGINT, request_termination)

    started = time.monotonic()
    try:
        child = subprocess.Popen(
            list(config.argv),
            cwd=config.cwd,
            env=config.environment,
            stdin=subprocess.DEVNULL,
            close_fds=True,
            pass_fds=config.pass_fds,
            start_new_session=True,
            preexec_fn=_child_bootstrap(config),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        detail = str(exc).strip()[:200]
        _report(
            status_fd,
            {
                "event": "result",
                "execution_id": config.execution_id,
                "confinement_error": "script_spawn_failed",
                "confinement_detail": detail or None,
                "spawn_errno": getattr(exc, "errno", None),
                "duration_seconds": round(time.monotonic() - started, 6),
            },
        )
        return 70

    _report(status_fd, {"event": "started", "execution_id": config.execution_id, "pid": child.pid})

    main_status: list[int] = []
    usage = {"peak_rss_bytes": 0, "peak_process_count": 0, "cpu_seconds": 0.0}
    reason = "exited"
    children_supported = _children_files_supported()
    deadline = started + config.wall_deadline_seconds
    try:
        while True:
            _reap_children(main_pid=child.pid, main_status=main_status)
            descendants = _descendants(os.getpid(), children_supported)
            _sample_usage(descendants, usage)
            alive = _alive_descendants(descendants)
            if main_status:
                reason = "exited"
                break
            if len(alive) > config.process_limit:
                reason = "process_limit"
                break
            if terminate_requested:
                reason = "terminated_by_parent"
                break
            if time.monotonic() >= deadline:
                reason = "deadline"
                break
            time.sleep(0.1)
    except Exception:
        reason = "supervisor_error"
    sweep_complete, survivors, killed = _sweep(
        main_pid=child.pid,
        main_status=main_status,
        sweep_deadline=config.sweep_deadline_seconds,
        children_supported=children_supported,
    )
    exit_code, exit_signal = _decode_status(main_status[0] if main_status else None)
    record: dict[str, Any] = {
        "event": "result",
        "execution_id": config.execution_id,
        "reason": reason,
        "exit_code": exit_code,
        "signal": exit_signal,
        "sweep_complete": sweep_complete,
        "survivors": survivors[:_MAX_TRACKED_PIDS],
        "survivors_truncated": len(survivors) > _MAX_TRACKED_PIDS,
        "descendants_killed": killed[:_MAX_TRACKED_PIDS],
        "descendants_killed_truncated": len(killed) > _MAX_TRACKED_PIDS,
        "usage": usage,
        "duration_seconds": round(time.monotonic() - started, 6),
    }
    _report(status_fd, record)
    return 0 if sweep_complete else 72


def _prctl(option: int, value: int) -> None:
    if _LIBC.prctl(option, value, 0, 0, 0) != 0:
        error = ctypes.get_errno()
        label = "subreaper" if option == _PR_SET_CHILD_SUBREAPER else "dumpable"
        raise OSError(error, label)


def _child_bootstrap(config: _Config) -> Callable[[], None]:
    limits = config.limits

    def bootstrap() -> None:
        if sys.platform.startswith("linux"):
            _prctl(_PR_SET_DUMPABLE, 0)
        import resource

        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        resource.setrlimit(resource.RLIMIT_AS, (limits.memory_bytes, limits.memory_bytes))
        resource.setrlimit(resource.RLIMIT_CPU, (limits.cpu_seconds, limits.cpu_seconds + 1))
        resource.setrlimit(resource.RLIMIT_FSIZE, (limits.file_size_bytes, limits.file_size_bytes))
        resource.setrlimit(resource.RLIMIT_NOFILE, (limits.nofile, limits.nofile))
        resource.setrlimit(resource.RLIMIT_NPROC, (limits.nproc, limits.nproc))
        if config.filesystem is not None:
            install_landlock_domain(
                read_directories=config.filesystem.read_directories,
                read_files=config.filesystem.read_files,
                write_directories=config.filesystem.write_directories,
                write_files=config.filesystem.write_files,
            )
        if config.deny_network:
            install_script_seccomp_filter()

    return bootstrap


def _read_config(path: str) -> _Config:
    with open(path, "rb") as handle:
        raw = handle.read(_CONFIG_MAX_BYTES + 1)
    try:
        os.unlink(path)
    except OSError:
        pass
    if len(raw) > _CONFIG_MAX_BYTES:
        raise _ConfigError("supervisor_config_too_large")
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _ConfigError("supervisor_config_invalid") from exc
    if not isinstance(payload, dict):
        raise _ConfigError("supervisor_config_invalid")
    try:
        argv = tuple(_string_list(payload["argv"], "argv", maximum=256, item_max=4096))
        cwd = payload.get("cwd")
        if cwd is not None and not isinstance(cwd, str):
            raise _ConfigError("supervisor_config_invalid")
        environment = payload["env"]
        if not isinstance(environment, dict) or len(environment) > 64:
            raise _ConfigError("supervisor_config_invalid")
        env = {
            str(key): str(value)
            for key, value in environment.items()
            if isinstance(key, str) and isinstance(value, str)
        }
        pass_fds = tuple(
            int(item) for item in _string_list(payload["pass_fds"], "pass_fds", maximum=8)
        )
        limits = payload["limits"]
        if not isinstance(limits, dict):
            raise _ConfigError("supervisor_config_invalid")
        filesystem_payload = payload.get("filesystem")
        filesystem = None
        if filesystem_payload is not None:
            if not isinstance(filesystem_payload, dict):
                raise _ConfigError("supervisor_config_invalid")
            filesystem = _Filesystem(
                read_directories=tuple(
                    _string_list(filesystem_payload["read_directories"], "read_directories")
                ),
                read_files=tuple(_string_list(filesystem_payload["read_files"], "read_files")),
                write_directories=tuple(
                    _string_list(filesystem_payload["write_directories"], "write_directories")
                ),
                write_files=tuple(
                    _string_list(filesystem_payload.get("write_files", []), "write_files")
                ),
            )
        return _Config(
            execution_id=_bounded_string(payload["execution_id"], 128),
            argv=argv,
            cwd=cwd,
            environment=env,
            pass_fds=pass_fds,
            deny_network=bool(payload.get("deny_network", False)),
            limits=_Limits(
                memory_bytes=_positive_int(limits["memory_bytes"]),
                cpu_seconds=_positive_int(limits["cpu_seconds"]),
                file_size_bytes=_positive_int(limits["file_size_bytes"]),
                nofile=_positive_int(limits["nofile"]),
                nproc=_positive_int(limits["nproc"]),
            ),
            filesystem=filesystem,
            wall_deadline_seconds=_positive_float(payload["wall_deadline_seconds"]),
            process_limit=_positive_int(payload["process_limit"]),
            sweep_deadline_seconds=_positive_float(payload["sweep_deadline_seconds"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, _ConfigError):
            raise
        raise _ConfigError("supervisor_config_invalid") from exc


def _string_list(
    value: object, label: str, *, maximum: int = 256, item_max: int = 4096
) -> list[str]:
    del label
    if not isinstance(value, list) or len(value) > maximum:
        raise _ConfigError("supervisor_config_invalid")
    items: list[str] = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, int | str):
            raise _ConfigError("supervisor_config_invalid")
        text = str(item)
        if not text or len(text) > item_max or "\x00" in text:
            raise _ConfigError("supervisor_config_invalid")
        items.append(text)
    return items


def _bounded_string(value: object, maximum: int) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum or "\x00" in value:
        raise _ConfigError("supervisor_config_invalid")
    return value


def _positive_int(value: object) -> int:
    if type(value) is not int or value <= 0:
        raise _ConfigError("supervisor_config_invalid")
    return value


def _positive_float(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise _ConfigError("supervisor_config_invalid")
    number = float(value)
    if not 0 < number <= 3600:
        raise _ConfigError("supervisor_config_invalid")
    return number


def _report(status_fd: int, record: dict[str, Any]) -> None:
    payload = json.dumps(record, sort_keys=True, separators=(",", ":")).encode("ascii")
    if len(payload) > _STATUS_MAX_BYTES:
        payload = json.dumps(
            {"event": record.get("event", "result"), "confinement_error": "status_too_large"},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
    try:
        view = memoryview(payload + b"\n")
        while view:
            written = os.write(status_fd, view)
            view = view[written:]
    except OSError:
        pass


def _children_files_supported() -> bool:
    path = f"/proc/{os.getpid()}/task/{os.getpid()}/children"
    try:
        with open(path, "rb") as handle:
            handle.read(1)
    except OSError:
        return False
    return True


def _child_pids(pid: int) -> list[int]:
    children: list[int] = []
    task_directory = f"/proc/{pid}/task"
    try:
        tasks = os.listdir(task_directory)
    except OSError:
        return children
    for task in tasks:
        try:
            with open(f"{task_directory}/{task}/children", "rb") as handle:
                raw = handle.read(65536)
        except OSError:
            continue
        for token in raw.split():
            try:
                children.append(int(token))
            except ValueError:
                continue
    return children


def _descendants(root_pid: int, children_supported: bool) -> list[int]:
    if children_supported:
        found: list[int] = []
        seen = {root_pid}
        pending = [root_pid]
        while pending and len(found) < _MAX_TREE_NODES:
            current = pending.pop()
            for child in _child_pids(current):
                if child in seen:
                    continue
                seen.add(child)
                found.append(child)
                pending.append(child)
        return found
    return _descendants_by_ppid(root_pid)


def _descendants_by_ppid(root_pid: int) -> list[int]:
    children_by_parent: dict[int, list[int]] = {}
    try:
        with os.scandir("/proc") as entries:
            for entry in entries:
                if not entry.name.isdigit():
                    continue
                try:
                    pid = int(entry.name)
                except ValueError:
                    continue
                stat = _proc_stat(pid)
                if stat is None:
                    continue
                children_by_parent.setdefault(stat.ppid, []).append(pid)
    except OSError:
        return []
    found: list[int] = []
    seen = {root_pid}
    pending = [root_pid]
    while pending and len(found) < _MAX_TREE_NODES:
        current = pending.pop()
        for child in children_by_parent.get(current, ()):
            if child in seen:
                continue
            seen.add(child)
            found.append(child)
            pending.append(child)
    return found


def _proc_stat(pid: int) -> _ProcessStat | None:
    try:
        with open(f"/proc/{pid}/stat", "rb") as handle:
            raw = handle.read(4096).decode("ascii", "replace")
    except OSError:
        return None
    separator = raw.rfind(")")
    if separator < 0:
        return None
    fields = raw[separator + 2 :].split()
    if len(fields) < 22:
        return None
    try:
        return _ProcessStat(
            ppid=int(fields[1]),
            pgrp=int(fields[2]),
            state=fields[0],
            cpu_ticks=int(fields[11]) + int(fields[12]),
            rss_pages=int(fields[21]),
        )
    except ValueError:
        return None


def _sample_usage(descendants: list[int], usage: dict[str, Any]) -> None:
    count = 0
    rss_pages = 0
    cpu_ticks = 0
    for pid in descendants:
        stat = _proc_stat(pid)
        if stat is None or stat.state == "Z":
            continue
        count += 1
        rss_pages += stat.rss_pages
        cpu_ticks += stat.cpu_ticks
    if count > usage["peak_process_count"]:
        usage["peak_process_count"] = count
    rss_bytes = rss_pages * _PAGE_SIZE
    if rss_bytes > usage["peak_rss_bytes"]:
        usage["peak_rss_bytes"] = rss_bytes
    cpu_seconds = cpu_ticks / _CLOCK_TICKS
    if cpu_seconds > usage["cpu_seconds"]:
        usage["cpu_seconds"] = cpu_seconds


def _alive_descendants(descendants: list[int]) -> list[int]:
    alive: list[int] = []
    for pid in descendants:
        stat = _proc_stat(pid)
        if stat is None or stat.state == "Z":
            continue
        alive.append(pid)
    return alive


def _reap_children(*, main_pid: int, main_status: list[int]) -> None:
    while True:
        try:
            pid, status = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return
        if pid == 0:
            return
        if pid == main_pid and not main_status:
            main_status.append(status)


def _sweep(
    *,
    main_pid: int,
    main_status: list[int],
    sweep_deadline: float,
    children_supported: bool,
) -> tuple[bool, list[int], list[int]]:
    """Kill and reap every descendant; prove or report the absence of survivors."""
    killed: set[int] = set()
    for pid in _alive_descendants(_descendants(os.getpid(), children_supported)):
        killed.add(pid)
    try:
        os.killpg(main_pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    deadline = time.monotonic() + sweep_deadline
    while True:
        _reap_children(main_pid=main_pid, main_status=main_status)
        alive = _alive_descendants(_descendants(os.getpid(), children_supported))
        if not alive:
            return True, [], sorted(killed)
        if time.monotonic() >= deadline:
            return False, sorted(alive), sorted(killed)
        for pid in alive:
            killed.add(pid)
            try:
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                continue
        time.sleep(0.01)


def _decode_status(status: int | None) -> tuple[int | None, int | None]:
    if status is None:
        return None, None
    try:
        code = os.waitstatus_to_exitcode(status)
    except ValueError:
        return None, None
    if code < 0:
        return None, -code
    return code, None


if __name__ == "__main__":
    raise SystemExit(main())
