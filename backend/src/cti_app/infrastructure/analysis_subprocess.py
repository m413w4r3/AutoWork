from __future__ import annotations

import asyncio
import ctypes
import errno
import os
import resource
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
    FAILED_TO_START = "FAILED_TO_START"
    NON_ZERO_EXIT = "NON_ZERO_EXIT"


@dataclass(frozen=True, slots=True)
class AnalysisSubprocessResult:
    status: AnalysisSubprocessStatus
    exit_code: int | None
    stdout: bytes
    stderr: bytes
    start_errno: int | None = None


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
) -> AnalysisSubprocessResult:
    if directory_limit_path is not None and (directory_max_bytes <= 0 or directory_max_files <= 0):
        raise ValueError("directory limits must be positive when a watched directory is set")
    if directory_limit_path is None and (directory_max_bytes or directory_max_files):
        raise ValueError("directory size limits require a watched directory")
    env = {
        key: value
        for key, value in (environment or {}).items()
        if key
        in {
            "PATH",
            "LANG",
            "LC_ALL",
            "PYTHONPATH",
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
        }
    }

    def limit() -> None:
        if sys.platform.startswith("linux"):
            set_process_dumpable(0)
        resource.setrlimit(resource.RLIMIT_AS, (memory_limit_bytes, memory_limit_bytes))
        if deny_network:
            install_network_seccomp_filter()

    with tempfile.TemporaryDirectory(prefix="cti-analysis-") as cwd:
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                cwd=Path(cwd),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                preexec_fn=limit if os.name == "posix" else None,
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

        async def drain_stream(stream: asyncio.StreamReader) -> None:
            while await stream.read(64 * 1024):
                pass

        async def kill_and_wait(
            reader_tasks: tuple[asyncio.Task[tuple[bytes, bool]], ...],
        ) -> None:
            if process.returncode is None:
                process.kill()
            await asyncio.gather(*reader_tasks, return_exceptions=True)
            assert process.stdout is not None
            assert process.stderr is not None
            await asyncio.gather(
                drain_stream(process.stdout),
                drain_stream(process.stderr),
            )
            await process.wait()

        def close_transport() -> None:
            transport = getattr(process, "_transport", None)
            if transport is not None:
                transport.close()

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

        async def collect() -> tuple[bytes, bytes, int | None, bool, bool]:
            assert process.stdout is not None
            assert process.stderr is not None
            stdout_task = asyncio.create_task(read_stream(process.stdout))
            stderr_task = asyncio.create_task(read_stream(process.stderr))
            directory_task = (
                asyncio.create_task(watch_directory()) if directory_limit_path is not None else None
            )
            try:
                while True:
                    watched: set[asyncio.Task[Any]] = {stdout_task, stderr_task}
                    if directory_task is not None:
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
                        await kill_and_wait((stdout_task, stderr_task))
                        output = [task.result()[0] for task in (stdout_task, stderr_task)]
                        if directory_task is not None and not directory_task.done():
                            directory_task.cancel()
                            await asyncio.gather(directory_task, return_exceptions=True)
                        return (
                            output[0],
                            output[1],
                            process.returncode,
                            output_limited,
                            directory_limited,
                        )
                    if stdout_task.done() and stderr_task.done():
                        exit_code = await process.wait()
                        stdout, _ = stdout_task.result()
                        stderr, _ = stderr_task.result()
                        if directory_task is not None:
                            directory_task.cancel()
                            await asyncio.gather(directory_task, return_exceptions=True)
                        return stdout, stderr, exit_code, False, False
            except asyncio.CancelledError:
                if directory_task is not None:
                    directory_task.cancel()
                await kill_and_wait((stdout_task, stderr_task))
                raise

        try:
            stdout, stderr, exit_code, output_limited, directory_limited = await asyncio.wait_for(
                collect(), timeout_seconds
            )
        except TimeoutError:
            close_transport()
            return AnalysisSubprocessResult(
                AnalysisSubprocessStatus.TIMED_OUT, process.returncode, b"", b""
            )
        if output_limited:
            close_transport()
            return AnalysisSubprocessResult(
                AnalysisSubprocessStatus.OUTPUT_LIMIT, exit_code, stdout, stderr
            )
    if directory_limited:
        close_transport()
        return AnalysisSubprocessResult(
            AnalysisSubprocessStatus.DIRECTORY_LIMIT, exit_code, stdout, stderr
        )
    if len(stdout) > output_limit or len(stderr) > output_limit:
        close_transport()
        return AnalysisSubprocessResult(
            AnalysisSubprocessStatus.OUTPUT_LIMIT,
            exit_code,
            stdout[:output_limit],
            stderr[:output_limit],
        )
    close_transport()
    return AnalysisSubprocessResult(
        AnalysisSubprocessStatus.SUCCEEDED
        if exit_code == 0
        else AnalysisSubprocessStatus.NON_ZERO_EXIT,
        exit_code,
        stdout,
        stderr,
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
