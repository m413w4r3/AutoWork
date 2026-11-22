from __future__ import annotations

import asyncio
import ctypes
import errno
import os
import resource
import signal
import stat
import subprocess
import sys
import tempfile
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
) -> AnalysisSubprocessResult:
    if directory_limit_path is not None and (directory_max_bytes <= 0 or directory_max_files <= 0):
        raise ValueError("directory limits must be positive when a watched directory is set")
    if directory_limit_path is None and (directory_max_bytes or directory_max_files):
        raise ValueError("directory size limits require a watched directory")
    if process_limit is not None and process_limit <= 0:
        raise ValueError("process limit must be positive")
    env = {
        key: value
        for key, value in (environment or {}).items()
        if key
        in {
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
    }

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
