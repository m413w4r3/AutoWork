from __future__ import annotations

import asyncio
import ctypes
import errno
import json
import os
import resource
import select
import signal
import stat
import subprocess
import sys
import tempfile
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from cti_app.infrastructure.seccomp_network import install_network_seccomp_filter

_PR_SET_DUMPABLE = 4
_PR_GET_DUMPABLE = 3
_LIBC = ctypes.CDLL(None, use_errno=True) if sys.platform.startswith("linux") else None
_PRCTL = getattr(_LIBC, "prctl", None) if _LIBC is not None else None
if _PRCTL is not None:
    _PRCTL.argtypes = [
        ctypes.c_int,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_ulong,
    ]
    _PRCTL.restype = ctypes.c_int


def set_process_dumpable(dumpable: int) -> None:
    """Set Linux dumpability, which also gates same-UID access to procfs data."""
    if dumpable not in {0, 1}:
        raise ValueError("dumpable must be zero or one")
    if _PRCTL is None:
        raise OSError(errno.ENOSYS, "prctl(PR_SET_DUMPABLE) is unavailable")
    if _PRCTL(_PR_SET_DUMPABLE, dumpable, 0, 0, 0) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), "prctl(PR_SET_DUMPABLE)")


def get_process_dumpable() -> int:
    """Read the current process dumpability value for focused Linux tests."""
    if _PRCTL is None:
        raise OSError(errno.ENOSYS, "prctl(PR_GET_DUMPABLE) is unavailable")
    value = _PRCTL(_PR_GET_DUMPABLE, 0, 0, 0, 0)
    if value < 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), "prctl(PR_GET_DUMPABLE)")
    return int(value)


class AnalysisSubprocessStatus(StrEnum):
    SUCCEEDED = "SUCCEEDED"
    TIMED_OUT = "TIMED_OUT"
    OUTPUT_LIMIT = "OUTPUT_LIMIT"
    DIRECTORY_LIMIT = "DIRECTORY_LIMIT"
    PROCESS_LIMIT = "PROCESS_LIMIT"
    FAILED_TO_START = "FAILED_TO_START"
    NON_ZERO_EXIT = "NON_ZERO_EXIT"
    CONFINEMENT_VIOLATION = "CONFINEMENT_VIOLATION"


@dataclass(frozen=True, slots=True)
class AnalysisSubprocessResourceUsage:
    """Best-effort sampled usage of the child process group."""

    peak_rss_bytes: int | None = None
    peak_process_count: int | None = None
    cpu_seconds: float | None = None


@dataclass(frozen=True, slots=True)
class AnalysisSubprocessResult:
    status: AnalysisSubprocessStatus
    exit_code: int | None
    stdout: bytes
    stderr: bytes
    start_errno: int | None = None
    usage: AnalysisSubprocessResourceUsage | None = None
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    # Supervised executions (``DescendantSupervision``) additionally report
    # the per-execution identity and the descendant sweep proof.  A result
    # with ``sweep_complete=False`` or non-empty ``survivors`` is never a
    # success: the process-tree boundary could not be proven.
    execution_id: str | None = None
    confinement_error: str | None = None
    sweep_complete: bool = True
    survivors: tuple[int, ...] = ()
    descendants_killed: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class LandlockPaths:
    """Filesystem roots granted to a supervised script process."""

    read_directories: tuple[Path, ...]
    read_files: tuple[Path, ...]
    write_directories: tuple[Path, ...]
    write_files: tuple[Path, ...] = ()

    def __post_init__(self) -> None:
        if not self.write_directories:
            raise ValueError("landlock confinement requires a writable scratch directory")
        if not self.read_directories and not self.read_files:
            raise ValueError("landlock confinement requires readable runtime roots")


@dataclass(frozen=True, slots=True)
class DescendantSupervision:
    """Kernel-enforced process-tree supervision for one untrusted script.

    The runner starts a trusted subreaper process around the script.  The
    subreaper applies the rlimits, the Landlock domain (when configured) and
    the script seccomp filter in the child, then kills and reaps every
    descendant on all exit paths.  ``filesystem=None`` is the bubblewrap
    profile: the mount namespace, not Landlock, is the filesystem boundary.
    """

    wall_deadline_seconds: float
    process_limit: int
    nofile_limit: int = 256
    sweep_deadline_seconds: float = 5.0
    kill_grace_seconds: float = 5.0
    filesystem: LandlockPaths | None = None

    def __post_init__(self) -> None:
        if not 0 < self.wall_deadline_seconds <= 3600:
            raise ValueError("supervision wall deadline must be positive and bounded")
        if not 0 < self.process_limit <= 4096:
            raise ValueError("supervision process limit must be positive and bounded")
        if not 0 < self.nofile_limit <= 65536:
            raise ValueError("supervision nofile limit must be positive and bounded")
        if not 0 < self.sweep_deadline_seconds <= 60:
            raise ValueError("supervision sweep deadline must be positive and bounded")
        if not 0 < self.kill_grace_seconds <= 60:
            raise ValueError("supervision kill grace must be positive and bounded")


@dataclass(frozen=True, slots=True)
class ConfinementProbeResult:
    """Outcome of the executed, end-to-end script confinement probe."""

    ok: bool
    detail: str


def _process_group_kill(process: asyncio.subprocess.Process) -> None:
    """Kill the complete child process group, never only the direct child.

    ``start_new_session=True`` makes the child a session and process-group
    leader, so ``killpg`` reaches grandchildren a script forked. If the group
    is already gone, fall back to killing the direct child only when it still
    exists.
    """
    if process.returncode is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    except OSError:
        try:
            process.kill()
        except ProcessLookupError:
            pass


def _uid_task_count() -> int:
    """Count tasks (threads) owned by this real UID, matching RLIMIT_NPROC."""
    uid = os.getuid()
    tasks = 0
    try:
        with os.scandir("/proc") as entries:
            for entry in entries:
                if not entry.name.isdigit():
                    continue
                try:
                    with open(f"/proc/{entry.name}/status", encoding="ascii") as handle:
                        owned = False
                        threads = 1
                        for line in handle:
                            if line.startswith("Uid:"):
                                owned = int(line.split()[1]) == uid
                            elif line.startswith("Threads:"):
                                threads = int(line.split()[1])
                                break
                        if owned:
                            tasks += threads
                except (OSError, ValueError):
                    continue
    except OSError:
        return 0
    return tasks


@dataclass(slots=True)
class _UsageState:
    peak_rss_bytes: int | None = None
    peak_process_count: int | None = None
    cpu_seconds: float | None = None


def _usage(state: _UsageState) -> AnalysisSubprocessResourceUsage:
    return AnalysisSubprocessResourceUsage(
        peak_rss_bytes=state.peak_rss_bytes,
        peak_process_count=state.peak_process_count,
        cpu_seconds=state.cpu_seconds,
    )


_CLOCK_TICKS = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
_PAGE_SIZE = os.sysconf("SC_PAGE_SIZE") if hasattr(os, "sysconf") else 4096


def _process_group_metrics(process_group: int) -> tuple[int, int, float] | None:
    """Return (process count, summed RSS bytes, summed CPU seconds) for a pgid."""
    count = 0
    rss_pages = 0
    cpu_ticks = 0
    try:
        with os.scandir("/proc") as entries:
            for entry in entries:
                if not entry.name.isdigit():
                    continue
                try:
                    raw = Path(f"/proc/{entry.name}/stat").read_text(encoding="ascii")
                except OSError:
                    continue
                fields = raw[raw.rfind(")") + 2 :].split()
                if len(fields) < 22:
                    continue
                try:
                    if int(fields[2]) != process_group:
                        continue
                    cpu_ticks += int(fields[11]) + int(fields[12])
                    rss_pages += int(fields[21])
                except ValueError:
                    continue
                count += 1
    except OSError:
        return None
    return count, rss_pages * _PAGE_SIZE, cpu_ticks / _CLOCK_TICKS


_ALLOWED_ENVIRONMENT_KEYS = frozenset(
    {
        "PATH",
        "LANG",
        "LC_ALL",
        "PYTHONPATH",
        "PYTHONDONTWRITEBYTECODE",
        "PYTHONUNBUFFERED",
        "PYTHONNOUSERSITE",
        "DOTNET_ROOT",
        "DOTNET_CLI_TELEMETRY_OPTOUT",
        "DOTNET_NOLOGO",
        "DOTNET_SKIP_FIRST_TIME_EXPERIENCE",
        "DOTNET_GCHeapHardLimit",
        "DOTNET_gcConcurrent",
        "NUGET_PACKAGES",
        "JAVA_HOME",
        "JAVA_OPTS",
        "HOME",
        "XDG_CONFIG_HOME",
        "XDG_CACHE_HOME",
        "XDG_DATA_HOME",
        "AUTOWORK_SCRIPT_INPUT",
        "AUTOWORK_SCRIPT_SCRATCH",
    }
)


def _allowed_environment(environment: dict[str, str] | None) -> dict[str, str]:
    return {
        key: value for key, value in (environment or {}).items() if key in _ALLOWED_ENVIRONMENT_KEYS
    }


async def run_analysis_subprocess(
    argv: Sequence[str],
    *,
    timeout_seconds: float = 30,
    output_limit: int = 1_000_000,
    environment: dict[str, str] | None = None,
    memory_limit_bytes: int = 512 * 1024 * 1024,
    deny_network: bool = False,
    directory_limit_path: Path | None = None,
    directory_max_bytes: int = 0,
    directory_max_files: int = 0,
    cpu_limit_seconds: int | None = None,
    file_size_limit_bytes: int | None = None,
    process_limit: int | None = None,
    pass_fds: Sequence[int] = (),
    cwd: Path | None = None,
    sample_usage: bool = False,
    supervision: DescendantSupervision | None = None,
) -> AnalysisSubprocessResult:
    if directory_limit_path is not None and (directory_max_bytes <= 0 or directory_max_files <= 0):
        raise ValueError("directory limits must be positive when a watched directory is set")
    if directory_limit_path is None and (directory_max_bytes or directory_max_files):
        raise ValueError("directory size limits require a watched directory")
    if process_limit is not None and process_limit <= 0:
        raise ValueError("process limit must be positive")
    if supervision is not None:
        if process_limit is not None and process_limit != supervision.process_limit:
            raise ValueError("supervised process limit must match the supervisor contract")
        if supervision.wall_deadline_seconds < timeout_seconds:
            raise ValueError("supervisor deadline must not precede the wall timeout")
        return await _run_supervised_subprocess(
            argv,
            timeout_seconds=timeout_seconds,
            output_limit=output_limit,
            environment=environment,
            memory_limit_bytes=memory_limit_bytes,
            deny_network=deny_network,
            directory_limit_path=directory_limit_path,
            directory_max_bytes=directory_max_bytes,
            directory_max_files=directory_max_files,
            cpu_limit_seconds=cpu_limit_seconds,
            file_size_limit_bytes=file_size_limit_bytes,
            pass_fds=pass_fds,
            cwd=cwd,
            supervision=supervision,
        )
    env = _allowed_environment(environment)

    # RLIMIT_NPROC counts every task (thread) owned by the real UID, not just
    # this process group. Add the UID's current task count so the ceiling
    # bounds this execution instead of being pre-exhausted by unrelated worker
    # threads. The group sampler below enforces the stricter process count.
    nproc_limit = _uid_task_count() + process_limit if process_limit is not None else None

    def limit() -> None:
        if sys.platform.startswith("linux"):
            set_process_dumpable(0)
        resource.setrlimit(resource.RLIMIT_AS, (memory_limit_bytes, memory_limit_bytes))
        if cpu_limit_seconds is not None:
            resource.setrlimit(resource.RLIMIT_CPU, (cpu_limit_seconds, cpu_limit_seconds + 1))
        if file_size_limit_bytes is not None:
            resource.setrlimit(
                resource.RLIMIT_FSIZE, (file_size_limit_bytes, file_size_limit_bytes)
            )
        if nproc_limit is not None:
            resource.setrlimit(resource.RLIMIT_NPROC, (nproc_limit, nproc_limit))
        if deny_network:
            install_network_seccomp_filter()

    with tempfile.TemporaryDirectory(prefix="cti-analysis-") as default_cwd:
        working_directory = cwd if cwd is not None else Path(default_cwd)
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                cwd=working_directory,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                preexec_fn=limit if os.name == "posix" else None,
                start_new_session=True,
                pass_fds=tuple(pass_fds),
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return AnalysisSubprocessResult(
                AnalysisSubprocessStatus.FAILED_TO_START,
                None,
                b"",
                b"",
                start_errno=getattr(exc, "errno", None),
            )

        async def read_stream(stream: asyncio.StreamReader) -> tuple[bytes, bool]:
            buffer = bytearray()
            while True:
                chunk = await stream.read(64 * 1024)
                if not chunk:
                    return bytes(buffer), False
                remaining = output_limit - len(buffer)
                if len(chunk) > remaining:
                    if remaining > 0:
                        buffer.extend(chunk[:remaining])
                    return bytes(buffer), True
                buffer.extend(chunk)

        def close_transport() -> None:
            transport = getattr(process, "_transport", None)
            if transport is not None:
                transport.close()

        async def kill_and_wait(
            reader_tasks: tuple[asyncio.Task[tuple[bytes, bool]], ...],
        ) -> None:
            _process_group_kill(process)
            try:
                await asyncio.wait_for(
                    asyncio.gather(*reader_tasks, return_exceptions=True), timeout=5
                )
            except TimeoutError:
                # A descendant escaped the process group and still holds the
                # pipes. Drop the transport instead of hanging the worker.
                close_transport()
                for task in reader_tasks:
                    task.cancel()
                await asyncio.gather(*reader_tasks, return_exceptions=True)
            if process.returncode is None:
                try:
                    await asyncio.wait_for(process.wait(), timeout=5)
                except TimeoutError:
                    close_transport()

        async def watch_directory() -> bool:
            assert directory_limit_path is not None
            while True:
                if _directory_exceeds_limits(
                    directory_limit_path,
                    max_bytes=directory_max_bytes,
                    max_files=directory_max_files,
                ):
                    return True
                await asyncio.sleep(0.05)

        usage_state = _UsageState()

        async def sample_group_usage() -> bool:
            try:
                while True:
                    metrics = _process_group_metrics(process.pid)
                    if metrics is not None:
                        count, rss_bytes, cpu_seconds = metrics
                        if (
                            usage_state.peak_process_count is None
                            or count > usage_state.peak_process_count
                        ):
                            usage_state.peak_process_count = count
                        if (
                            usage_state.peak_rss_bytes is None
                            or rss_bytes > usage_state.peak_rss_bytes
                        ):
                            usage_state.peak_rss_bytes = rss_bytes
                        if usage_state.cpu_seconds is None or cpu_seconds > usage_state.cpu_seconds:
                            usage_state.cpu_seconds = cpu_seconds
                        if process_limit is not None and count > process_limit:
                            return True
                    await asyncio.sleep(0.1)
            except Exception:
                return False

        def reader_output(task: asyncio.Task[tuple[bytes, bool]]) -> bytes:
            if task.cancelled() or task.exception() is not None:
                return b""
            return task.result()[0]

        def reader_limited(task: asyncio.Task[tuple[bytes, bool]]) -> bool:
            if task.cancelled() or task.exception() is not None:
                return False
            return task.result()[1]

        async def collect() -> tuple[bytes, bytes, int | None, bool, bool, bool, bool, bool]:
            assert process.stdout is not None
            assert process.stderr is not None
            stdout_task = asyncio.create_task(read_stream(process.stdout))
            stderr_task = asyncio.create_task(read_stream(process.stderr))
            directory_task = (
                asyncio.create_task(watch_directory()) if directory_limit_path is not None else None
            )
            sampler_task = (
                asyncio.create_task(sample_group_usage())
                if sample_usage or process_limit is not None
                else None
            )
            try:
                while True:
                    watched: set[asyncio.Task[Any]] = {stdout_task, stderr_task}
                    if directory_task is not None and not directory_task.done():
                        watched.add(directory_task)
                    if sampler_task is not None and not sampler_task.done():
                        watched.add(sampler_task)
                    done, _ = await asyncio.wait(watched, return_when=asyncio.FIRST_COMPLETED)
                    process_limited = (
                        sampler_task is not None
                        and sampler_task in done
                        and sampler_task.result() is True
                    )
                    directory_limited = (
                        directory_task is not None
                        and directory_task in done
                        and directory_task.result()
                    )
                    output_limited = any(
                        task in done and task.result()[1] for task in (stdout_task, stderr_task)
                    )
                    if process_limited or directory_limited or output_limited:
                        await kill_and_wait((stdout_task, stderr_task))
                        return (
                            reader_output(stdout_task),
                            reader_output(stderr_task),
                            process.returncode,
                            output_limited,
                            directory_limited,
                            process_limited,
                            reader_limited(stdout_task),
                            reader_limited(stderr_task),
                        )
                    if stdout_task.done() and stderr_task.done():
                        exit_code = await process.wait()
                        stdout, stdout_limited = stdout_task.result()
                        stderr, stderr_limited = stderr_task.result()
                        return (
                            stdout,
                            stderr,
                            exit_code,
                            False,
                            False,
                            False,
                            stdout_limited,
                            stderr_limited,
                        )
            except asyncio.CancelledError:
                await kill_and_wait((stdout_task, stderr_task))
                raise
            finally:
                pending = [
                    task
                    for task in (directory_task, sampler_task)
                    if task is not None and not task.done()
                ]
                for task in pending:
                    task.cancel()
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)

        try:
            (
                stdout,
                stderr,
                exit_code,
                output_limited,
                directory_limited,
                process_limited,
                stdout_truncated,
                stderr_truncated,
            ) = await asyncio.wait_for(collect(), timeout_seconds)
        except TimeoutError:
            close_transport()
            return AnalysisSubprocessResult(
                AnalysisSubprocessStatus.TIMED_OUT,
                process.returncode,
                b"",
                b"",
                usage=_usage(usage_state),
            )
        usage = _usage(usage_state)
        if process_limited:
            close_transport()
            return AnalysisSubprocessResult(
                AnalysisSubprocessStatus.PROCESS_LIMIT,
                exit_code,
                stdout,
                stderr,
                usage=usage,
                stdout_truncated=stdout_truncated,
                stderr_truncated=stderr_truncated,
            )
        if output_limited:
            close_transport()
            return AnalysisSubprocessResult(
                AnalysisSubprocessStatus.OUTPUT_LIMIT,
                exit_code,
                stdout,
                stderr,
                usage=usage,
                stdout_truncated=stdout_truncated,
                stderr_truncated=stderr_truncated,
            )
    if directory_limited:
        close_transport()
        return AnalysisSubprocessResult(
            AnalysisSubprocessStatus.DIRECTORY_LIMIT,
            exit_code,
            stdout,
            stderr,
            usage=usage,
            stdout_truncated=stdout_truncated,
            stderr_truncated=stderr_truncated,
        )
    if len(stdout) > output_limit or len(stderr) > output_limit:
        close_transport()
        return AnalysisSubprocessResult(
            AnalysisSubprocessStatus.OUTPUT_LIMIT,
            exit_code,
            stdout[:output_limit],
            stderr[:output_limit],
            usage=usage,
            stdout_truncated=len(stdout) > output_limit or stdout_truncated,
            stderr_truncated=len(stderr) > output_limit or stderr_truncated,
        )
    close_transport()
    return AnalysisSubprocessResult(
        AnalysisSubprocessStatus.SUCCEEDED
        if exit_code == 0
        else AnalysisSubprocessStatus.NON_ZERO_EXIT,
        exit_code,
        stdout,
        stderr,
        usage=usage,
        stdout_truncated=stdout_truncated,
        stderr_truncated=stderr_truncated,
    )


_SUPERVISOR_ENV = {
    "PATH": "/usr/bin:/bin:/usr/local/bin",
    "LANG": "C.UTF-8",
    "LC_ALL": "C.UTF-8",
}
_SUPERVISOR_STATUS_MAX_BYTES = 64 * 1024
# RLIMIT_NPROC counts every task of the real UID, including unrelated host or
# worker threads. The strict per-execution quota is the supervisor descendant
# count; NPROC only needs enough headroom to reach that quota reliably.
_SUPERVISED_NPROC_HEADROOM = 64
_SUPERVISOR_MODULE = "cti_app.infrastructure.confined_supervisor"


def runtime_read_directories() -> tuple[Path, ...]:
    """Read-only roots a confined script interpreter needs on this host."""
    candidates = [
        Path("/usr"),
        Path("/app"),
        Path("/lib"),
        Path("/lib64"),
        Path("/lib32"),
        Path(sys.base_prefix),
        Path(sys.prefix),
    ]
    selected: list[Path] = []
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if resolved == Path("/") or not resolved.is_dir():
            continue
        if any(resolved == existing or resolved.is_relative_to(existing) for existing in selected):
            continue
        selected.append(resolved)
    return tuple(selected)


def runtime_read_files(*extra: Path) -> tuple[Path, ...]:
    """Exact read-only files (loader cache, devices, script, input) for a script."""
    candidates = [
        Path("/etc/ld.so.cache"),
        Path("/etc/localtime"),
        Path("/dev/null"),
        Path("/dev/urandom"),
        Path("/dev/random"),
        *extra,
    ]
    selected: list[Path] = []
    for candidate in candidates:
        try:
            if candidate.exists() and not candidate.is_dir() and candidate not in selected:
                selected.append(candidate)
        except OSError:
            continue
    return tuple(selected)


def runtime_write_files() -> tuple[Path, ...]:
    """Device nodes a script may write to (``subprocess.DEVNULL``)."""
    return tuple(path for path in (Path("/dev/null"),) if path.exists() and not path.is_dir())


async def _run_supervised_subprocess(
    argv: Sequence[str],
    *,
    timeout_seconds: float,
    output_limit: int,
    environment: dict[str, str] | None,
    memory_limit_bytes: int,
    deny_network: bool,
    directory_limit_path: Path | None,
    directory_max_bytes: int,
    directory_max_files: int,
    cpu_limit_seconds: int | None,
    file_size_limit_bytes: int | None,
    pass_fds: Sequence[int],
    cwd: Path | None,
    supervision: DescendantSupervision,
) -> AnalysisSubprocessResult:
    """Run one untrusted script behind the trusted subreaper supervisor."""
    env = _allowed_environment(environment)
    execution_id = uuid.uuid4().hex
    filesystem = supervision.filesystem
    config = {
        "execution_id": execution_id,
        "argv": list(argv),
        "cwd": str(cwd) if cwd is not None else None,
        "env": env,
        "pass_fds": list(pass_fds),
        "deny_network": deny_network,
        "limits": {
            "memory_bytes": int(memory_limit_bytes),
            "cpu_seconds": int(
                cpu_limit_seconds
                if cpu_limit_seconds is not None
                else max(1, int(timeout_seconds) + 1)
            ),
            "file_size_bytes": int(
                file_size_limit_bytes if file_size_limit_bytes is not None else 16 * 1024 * 1024
            ),
            "nofile": supervision.nofile_limit,
            "nproc": _uid_task_count() + supervision.process_limit + _SUPERVISED_NPROC_HEADROOM,
        },
        "filesystem": (
            {
                "read_directories": [str(path) for path in filesystem.read_directories],
                "read_files": [str(path) for path in filesystem.read_files],
                "write_directories": [str(path) for path in filesystem.write_directories],
                "write_files": [str(path) for path in filesystem.write_files],
            }
            if filesystem is not None
            else None
        ),
        "wall_deadline_seconds": supervision.wall_deadline_seconds,
        "process_limit": supervision.process_limit,
        "sweep_deadline_seconds": supervision.sweep_deadline_seconds,
    }
    with tempfile.TemporaryDirectory(prefix="cti-supervisor-") as root_name:
        root = Path(root_name)
        config_path = root / "supervisor.json"
        config_path.write_text(
            json.dumps(config, sort_keys=True, separators=(",", ":")), encoding="ascii"
        )
        config_path.chmod(0o600)
        status_read, status_write = os.pipe()
        try:
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-I",
                "-m",
                _SUPERVISOR_MODULE,
                str(config_path),
                str(status_write),
                cwd=root,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=dict(_SUPERVISOR_ENV),
                pass_fds=(status_write, *tuple(pass_fds)),
                start_new_session=True,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            os.close(status_read)
            os.close(status_write)
            return AnalysisSubprocessResult(
                AnalysisSubprocessStatus.FAILED_TO_START,
                None,
                b"",
                b"",
                start_errno=getattr(exc, "errno", None),
                execution_id=execution_id,
                confinement_error="supervisor_spawn_failed",
            )
        os.close(status_write)
        try:
            result = await _collect_supervised_result(
                process,
                status_read=status_read,
                supervision=supervision,
                timeout_seconds=timeout_seconds,
                output_limit=output_limit,
                directory_limit_path=directory_limit_path,
                directory_max_bytes=directory_max_bytes,
                directory_max_files=directory_max_files,
                execution_id=execution_id,
            )
        finally:
            transport = getattr(process, "_transport", None)
            if transport is not None:
                transport.close()
            os.close(status_read)
        return result


async def _collect_supervised_result(
    process: asyncio.subprocess.Process,
    *,
    status_read: int,
    supervision: DescendantSupervision,
    timeout_seconds: float,
    output_limit: int,
    directory_limit_path: Path | None,
    directory_max_bytes: int,
    directory_max_files: int,
    execution_id: str,
) -> AnalysisSubprocessResult:
    stdout_stream = process.stdout
    stderr_stream = process.stderr
    assert stdout_stream is not None
    assert stderr_stream is not None

    def close_transport() -> None:
        transport = getattr(process, "_transport", None)
        if transport is not None:
            transport.close()

    async def read_stream(stream: asyncio.StreamReader) -> tuple[bytes, bool]:
        buffer = bytearray()
        while True:
            chunk = await stream.read(64 * 1024)
            if not chunk:
                return bytes(buffer), False
            remaining = output_limit - len(buffer)
            if len(chunk) > remaining:
                if remaining > 0:
                    buffer.extend(chunk[:remaining])
                return bytes(buffer), True
            buffer.extend(chunk)

    async def stop_supervisor() -> None:
        if process.returncode is not None:
            return
        try:
            process.send_signal(signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(process.wait(), timeout=supervision.sweep_deadline_seconds + 2.0)
        except TimeoutError:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except (ProcessLookupError, OSError):
                pass
            try:
                await asyncio.wait_for(process.wait(), timeout=2.0)
            except TimeoutError:
                close_transport()

    async def drain(readers: tuple[asyncio.Task[tuple[bytes, bool]], ...]) -> None:
        try:
            await asyncio.wait_for(asyncio.gather(*readers, return_exceptions=True), timeout=5)
        except TimeoutError:
            close_transport()
            for reader in readers:
                reader.cancel()
            await asyncio.gather(*readers, return_exceptions=True)

    def reader_output(task: asyncio.Task[tuple[bytes, bool]]) -> bytes:
        if task.cancelled() or task.exception() is not None:
            return b""
        return task.result()[0]

    def reader_limited(task: asyncio.Task[tuple[bytes, bool]]) -> bool:
        if task.cancelled() or task.exception() is not None:
            return False
        return task.result()[1]

    async def watch_directory() -> bool:
        assert directory_limit_path is not None
        while True:
            if _directory_exceeds_limits(
                directory_limit_path,
                max_bytes=directory_max_bytes,
                max_files=directory_max_files,
            ):
                return True
            await asyncio.sleep(0.05)

    captured: dict[str, Any] = {}

    async def collect() -> tuple[bytes, bytes, bool, bool, bool]:
        stdout_task = asyncio.create_task(read_stream(stdout_stream))
        stderr_task = asyncio.create_task(read_stream(stderr_stream))
        directory_task = (
            asyncio.create_task(watch_directory()) if directory_limit_path is not None else None
        )
        try:
            while True:
                watched: set[asyncio.Task[Any]] = {stdout_task, stderr_task}
                if directory_task is not None and not directory_task.done():
                    watched.add(directory_task)
                done, _ = await asyncio.wait(watched, return_when=asyncio.FIRST_COMPLETED)
                directory_limited = (
                    directory_task is not None
                    and directory_task in done
                    and directory_task.result()
                )
                output_limited = any(
                    task in done and task.result()[1] for task in (stdout_task, stderr_task)
                )
                if directory_limited or output_limited:
                    await stop_supervisor()
                    await drain((stdout_task, stderr_task))
                    return (
                        reader_output(stdout_task),
                        reader_output(stderr_task),
                        output_limited,
                        directory_limited,
                        False,
                    )
                if stdout_task.done() and stderr_task.done():
                    await process.wait()
                    return (
                        reader_output(stdout_task),
                        reader_output(stderr_task),
                        False,
                        False,
                        True,
                    )
        except asyncio.CancelledError:
            await stop_supervisor()
            await drain((stdout_task, stderr_task))
            captured["stdout"] = reader_output(stdout_task)
            captured["stderr"] = reader_output(stderr_task)
            captured["stdout_limited"] = reader_limited(stdout_task)
            captured["stderr_limited"] = reader_limited(stderr_task)
            raise
        finally:
            if directory_task is not None and not directory_task.done():
                directory_task.cancel()
                await asyncio.gather(directory_task, return_exceptions=True)

    timed_out = False
    stdout_limited = False
    stderr_limited = False
    try:
        (
            stdout,
            stderr,
            output_limited,
            directory_limited,
            _normal_exit,
        ) = await asyncio.wait_for(collect(), timeout_seconds)
    except TimeoutError:
        timed_out = True
        stdout = captured.get("stdout", b"")
        stderr = captured.get("stderr", b"")
        stdout_limited = bool(captured.get("stdout_limited", False))
        stderr_limited = bool(captured.get("stderr_limited", False))
        output_limited = False
        directory_limited = False

    records = await asyncio.to_thread(_read_supervisor_records, status_read)
    started_record = next((record for record in records if record.get("event") == "started"), None)
    result_record = next((record for record in records if record.get("event") == "result"), None)
    usage = _supervisor_usage(result_record)
    survivors = _supervisor_pids(result_record, "survivors")
    descendants_killed = _supervisor_pids(result_record, "descendants_killed")
    common: dict[str, Any] = {
        "usage": usage,
        "execution_id": execution_id,
        "survivors": survivors,
        "descendants_killed": descendants_killed,
    }
    if result_record is None:
        if started_record is not None:
            script_pid = started_record.get("pid")
            if type(script_pid) is int and script_pid > 1:
                try:
                    os.killpg(script_pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
        # A supervisor that exited (even successfully) without its status is
        # an infrastructure refusal, not evidence of an escape; one killed by
        # a signal could have been killed by the script, so its tree proof is
        # missing and the execution is a confinement violation.
        exited_normally = process.returncode is not None and process.returncode >= 0
        return AnalysisSubprocessResult(
            (
                AnalysisSubprocessStatus.FAILED_TO_START
                if exited_normally
                else AnalysisSubprocessStatus.CONFINEMENT_VIOLATION
            ),
            None,
            stdout,
            stderr,
            stdout_truncated=stdout_limited,
            stderr_truncated=stderr_limited,
            confinement_error="supervisor_status_missing",
            sweep_complete=False,
            **common,
        )
    confinement_error = result_record.get("confinement_error")
    if isinstance(confinement_error, str) and confinement_error:
        detail = result_record.get("confinement_detail")
        if isinstance(detail, str) and detail:
            confinement_error = f"{confinement_error}:{detail[:160]}"
        return AnalysisSubprocessResult(
            AnalysisSubprocessStatus.FAILED_TO_START,
            None,
            stdout,
            stderr,
            stdout_truncated=stdout_limited,
            stderr_truncated=stderr_limited,
            confinement_error=confinement_error,
            **common,
        )
    exit_code = result_record.get("exit_code")
    script_exit_code = exit_code if type(exit_code) is int else None
    sweep_complete = result_record.get("sweep_complete") is True
    if not sweep_complete or survivors:
        return AnalysisSubprocessResult(
            AnalysisSubprocessStatus.CONFINEMENT_VIOLATION,
            script_exit_code,
            stdout,
            stderr,
            stdout_truncated=stdout_limited,
            stderr_truncated=stderr_limited,
            sweep_complete=False,
            **common,
        )
    detailed_reason = result_record.get("reason")
    if timed_out or detailed_reason == "deadline":
        status = AnalysisSubprocessStatus.TIMED_OUT
    elif output_limited:
        status = AnalysisSubprocessStatus.OUTPUT_LIMIT
    elif directory_limited:
        status = AnalysisSubprocessStatus.DIRECTORY_LIMIT
    elif detailed_reason == "process_limit":
        status = AnalysisSubprocessStatus.PROCESS_LIMIT
    elif detailed_reason == "supervisor_error":
        return AnalysisSubprocessResult(
            AnalysisSubprocessStatus.CONFINEMENT_VIOLATION,
            script_exit_code,
            stdout,
            stderr,
            stdout_truncated=stdout_limited,
            stderr_truncated=stderr_limited,
            confinement_error="supervisor_error",
            **common,
        )
    elif script_exit_code == 0:
        status = AnalysisSubprocessStatus.SUCCEEDED
    else:
        status = AnalysisSubprocessStatus.NON_ZERO_EXIT
    return AnalysisSubprocessResult(
        status,
        script_exit_code,
        stdout,
        stderr,
        stdout_truncated=stdout_limited or output_limited,
        stderr_truncated=stderr_limited or output_limited,
        **common,
    )


def _read_supervisor_records(status_fd: int) -> list[dict[str, Any]]:
    data = bytearray()
    while len(data) < _SUPERVISOR_STATUS_MAX_BYTES:
        try:
            ready, _, _ = select.select([status_fd], [], [], 2.0)
        except (OSError, ValueError):
            break
        if not ready:
            break
        try:
            chunk = os.read(status_fd, 4096)
        except OSError:
            break
        if not chunk:
            break
        data.extend(chunk)
    records: list[dict[str, Any]] = []
    for line in bytes(data).splitlines():
        try:
            record = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def _supervisor_usage(
    record: dict[str, Any] | None,
) -> AnalysisSubprocessResourceUsage | None:
    if record is None:
        return None
    usage = record.get("usage")
    if not isinstance(usage, dict):
        return None

    def number(name: str) -> int | float | None:
        value = usage.get(name)
        if isinstance(value, bool) or not isinstance(value, int | float):
            return None
        return value

    rss = number("peak_rss_bytes")
    processes = number("peak_process_count")
    cpu = number("cpu_seconds")
    return AnalysisSubprocessResourceUsage(
        peak_rss_bytes=int(rss) if rss is not None and rss > 0 else None,
        peak_process_count=int(processes) if processes is not None and processes > 0 else None,
        cpu_seconds=float(cpu) if cpu is not None else None,
    )


def _supervisor_pids(record: dict[str, Any] | None, name: str) -> tuple[int, ...]:
    if record is None:
        return ()
    values = record.get(name)
    if not isinstance(values, list):
        return ()
    return tuple(value for value in values if type(value) is int and value > 1)


_PROBE_SCRIPT_TEMPLATE = """\
import errno
import json
import os
import socket
import sys
import threading
import time

DENIED = {denied!r}
SCRATCH = os.environ["AUTOWORK_SCRIPT_SCRATCH"]
INPUT = os.environ["AUTOWORK_SCRIPT_INPUT"]
report = {{}}


def attempt(name, call, expected):
    try:
        call()
    except OSError as exc:
        code = int(exc.errno)
        report[name] = -code if code in expected else code
    else:
        report[name] = 0


def read_denied():
    with open(DENIED, "rb") as handle:
        handle.read(64)


def read_proc():
    with open("/proc/self/status", "rb") as handle:
        handle.read(64)


def write_outside():
    with open(os.path.join(os.path.dirname(DENIED), "probe-escape.txt"), "wb") as handle:
        handle.write(b"x")


def open_socket():
    socket.socket()


def signal_self():
    os.kill(os.getpid(), 0)


def scratch_io():
    path = os.path.join(SCRATCH, "probe.txt")
    with open(path, "wb") as handle:
        handle.write(b"ok")
    with open(path, "rb") as handle:
        assert handle.read() == b"ok"


def input_read():
    with open(INPUT, "rb") as handle:
        assert handle.read()


def setsid_child():
    pid = os.fork()
    if pid == 0:
        try:
            os.setsid()
        except OSError as exc:
            os._exit(0 if exc.errno == errno.EPERM else 9)
        os._exit(8)
    _, status = os.waitpid(pid, 0)
    code = os.waitstatus_to_exitcode(status)
    if code == 0:
        raise OSError(errno.EPERM, "setsid blocked")
    if code == 8:
        raise OSError(errno.EIO, "setsid allowed")
    raise OSError(errno.EIO, "setsid unexpected")


def devnull_write():
    with open(os.devnull, "wb") as handle:
        handle.write(b"x")


def detached_child():
    pid = os.fork()
    if pid == 0:
        time.sleep(300)
        os._exit(0)
    print("DETACHED=%d" % pid, flush=True)


denied_errors = {{errno.EACCES, errno.ENOENT, errno.EPERM}}
attempt("denied_read", read_denied, denied_errors)
attempt("read_proc", read_proc, denied_errors)
attempt("write_outside", write_outside, denied_errors)
attempt("socket", open_socket, {{errno.EPERM}})
attempt("signal_self", signal_self, {{errno.EPERM}})
attempt("setsid_child", setsid_child, {{errno.EPERM}})
attempt("scratch_io", scratch_io, set())
attempt("input_read", input_read, set())
attempt("devnull_write", devnull_write, set())
thread = threading.Thread(target=lambda: None)
thread.start()
thread.join()
detached_child()
print("PROBE=" + json.dumps(report, sort_keys=True), flush=True)
expected = {{"denied_read": "blocked", "read_proc": "blocked", "write_outside": "blocked",
           "socket": "blocked", "signal_self": "blocked", "setsid_child": "blocked",
           "scratch_io": "ok", "input_read": "ok", "devnull_write": "ok"}}
observed = {{
    name: ("ok" if report.get(name) == 0 else "blocked" if (report.get(name) or 0) < 0 else "other")
    for name in expected
}}
sys.exit(0 if observed == expected else 3)
"""


def probe_confined_script_execution(
    interpreter: str,
    *,
    timeout_seconds: float = 15.0,
    memory_limit_bytes: int = 256 * 1024 * 1024,
) -> ConfinementProbeResult:
    """Execute the full supervisor stack and prove each confinement property.

    The probe script is our own harmless code: it checks that Landlock denies
    reads/writes outside the allowlist and ``/proc``, that seccomp denies
    sockets, signal sending and ``setsid``, that the interpreter still runs
    threads and its input/scratch, and that a leftover child is killed by the
    subreaper sweep before the status is reported.
    """
    if not sys.platform.startswith("linux"):
        return ConfinementProbeResult(False, "probe_requires_linux")
    with tempfile.TemporaryDirectory(prefix="cti-script-probe-") as root_name:
        root = Path(root_name)
        scratch = root / "work"
        scratch.mkdir(mode=0o700)
        input_path = root / "input.bin"
        input_path.write_bytes(b"probe-input")
        input_path.chmod(0o400)
        denied_path = root / "denied.txt"
        denied_path.write_bytes(b"denied")
        script_path = root / "probe.py"
        script_path.write_text(
            _PROBE_SCRIPT_TEMPLATE.format(denied=str(denied_path)), encoding="utf-8"
        )
        script_path.chmod(0o400)
        supervision = DescendantSupervision(
            wall_deadline_seconds=timeout_seconds + 5.0,
            process_limit=8,
            sweep_deadline_seconds=3.0,
            filesystem=LandlockPaths(
                read_directories=runtime_read_directories(),
                read_files=runtime_read_files(script_path, input_path),
                write_directories=(scratch,),
                write_files=runtime_write_files(),
            ),
        )
        filesystem = supervision.filesystem
        assert filesystem is not None
        config = {
            "execution_id": uuid.uuid4().hex,
            "argv": [interpreter, "-I", "-u", str(script_path)],
            "cwd": str(scratch),
            "env": {
                "PATH": "/usr/bin:/bin:/usr/local/bin",
                "LANG": "C.UTF-8",
                "LC_ALL": "C.UTF-8",
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONUNBUFFERED": "1",
                "AUTOWORK_SCRIPT_INPUT": str(input_path),
                "AUTOWORK_SCRIPT_SCRATCH": str(scratch),
            },
            "pass_fds": [],
            "deny_network": True,
            "limits": {
                "memory_bytes": memory_limit_bytes,
                "cpu_seconds": max(1, int(timeout_seconds)),
                "file_size_bytes": 16 * 1024 * 1024,
                "nofile": supervision.nofile_limit,
                "nproc": _uid_task_count() + supervision.process_limit + _SUPERVISED_NPROC_HEADROOM,
            },
            "filesystem": {
                "read_directories": [str(path) for path in filesystem.read_directories],
                "read_files": [str(path) for path in filesystem.read_files],
                "write_directories": [str(path) for path in filesystem.write_directories],
                "write_files": [str(path) for path in filesystem.write_files],
            },
            "wall_deadline_seconds": supervision.wall_deadline_seconds,
            "process_limit": supervision.process_limit,
            "sweep_deadline_seconds": supervision.sweep_deadline_seconds,
        }
        config_path = root / "supervisor.json"
        config_path.write_text(
            json.dumps(config, sort_keys=True, separators=(",", ":")), encoding="ascii"
        )
        config_path.chmod(0o600)
        status_read, status_write = os.pipe()
        try:
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-I",
                    "-m",
                    _SUPERVISOR_MODULE,
                    str(config_path),
                    str(status_write),
                ],
                cwd=root,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=dict(_SUPERVISOR_ENV),
                pass_fds=(status_write,),
                start_new_session=True,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            os.close(status_read)
            os.close(status_write)
            return ConfinementProbeResult(False, f"probe_supervisor_spawn_failed:{exc}")
        os.close(status_write)
        try:
            try:
                stdout_bytes, stderr_bytes = process.communicate(timeout=timeout_seconds + 12.0)
            except subprocess.TimeoutExpired:
                process.kill()
                stdout_bytes, stderr_bytes = process.communicate()
                records = _read_supervisor_records(status_read)
                script_pid = next(
                    (record.get("pid") for record in records if record.get("event") == "started"),
                    None,
                )
                if type(script_pid) is int and script_pid > 1:
                    try:
                        os.killpg(script_pid, signal.SIGKILL)
                    except (ProcessLookupError, PermissionError):
                        pass
                return ConfinementProbeResult(False, "probe_timeout")
            records = _read_supervisor_records(status_read)
        finally:
            os.close(status_read)
    result_record = next((record for record in records if record.get("event") == "result"), None)
    if result_record is None:
        return ConfinementProbeResult(False, "probe_supervisor_status_missing")
    confinement_error = result_record.get("confinement_error")
    if isinstance(confinement_error, str) and confinement_error:
        return ConfinementProbeResult(False, confinement_error)
    output = stdout_bytes.decode("utf-8", "replace")
    detached_pids = [
        int(line.split("=", 1)[1])
        for line in output.splitlines()
        if line.startswith("DETACHED=") and line.split("=", 1)[1].isdigit()
    ]
    if result_record.get("exit_code") != 0 or process.returncode != 0:
        detail = (
            f"probe_script_failed:supervisor={process.returncode}"
            f",script={result_record.get('exit_code')}"
        )
        report_line = next(
            (line[6:160] for line in output.splitlines() if line.startswith("PROBE=")), None
        )
        if report_line:
            detail = f"{detail}:{report_line}"
        stderr_text = stderr_bytes.decode("utf-8", "replace").strip()
        if stderr_text:
            detail = f"{detail}:stderr={stderr_text[-240:]}"
        return ConfinementProbeResult(False, detail)
    if result_record.get("sweep_complete") is not True or result_record.get("survivors"):
        return ConfinementProbeResult(False, "probe_sweep_unproven")
    killed = {value for value in result_record.get("descendants_killed", []) if type(value) is int}
    for pid in detached_pids:
        if _pid_is_alive(pid):
            return ConfinementProbeResult(False, f"probe_detached_survivor:{pid}")
        if pid not in killed:
            return ConfinementProbeResult(False, f"probe_detached_not_swept:{pid}")
    if not detached_pids:
        return ConfinementProbeResult(False, "probe_detached_child_not_observed")
    return ConfinementProbeResult(True, "landlock_seccomp_subreaper_verified")


def _pid_is_alive(pid: int) -> bool:
    try:
        with open(f"/proc/{pid}/stat", "rb") as handle:
            raw = handle.read(4096).decode("ascii", "replace")
    except OSError:
        return False
    separator = raw.rfind(")")
    if separator < 0:
        return False
    fields = raw[separator + 2 :].split()
    return bool(fields) and fields[0] != "Z"


def _directory_exceeds_limits(path: Path, *, max_bytes: int, max_files: int) -> bool:
    """Measure a tool output tree without following links or traversing outside it."""
    try:
        if path.is_symlink():
            return True
        if not path.exists():
            return False
        if not path.is_dir():
            return True
        pending = [path]
        entry_count = 0
        total_bytes = 0
        while pending:
            directory = pending.pop()
            with os.scandir(directory) as entries:
                for entry in entries:
                    entry_count += 1
                    if entry_count > max_files:
                        return True
                    if entry.is_symlink():
                        return True
                    if entry.is_dir(follow_symlinks=False):
                        pending.append(Path(entry.path))
                    elif entry.is_file(follow_symlinks=False):
                        total_bytes += entry.stat(follow_symlinks=False).st_size
                        if total_bytes > max_bytes:
                            return True
                    else:
                        mode = entry.stat(follow_symlinks=False).st_mode
                        if not stat.S_ISREG(mode) and not stat.S_ISDIR(mode):
                            return True
        return False
    except OSError:
        return True
