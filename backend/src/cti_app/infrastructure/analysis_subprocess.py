from __future__ import annotations

import asyncio
import os
import resource
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


class AnalysisSubprocessStatus(StrEnum):
    SUCCEEDED = "SUCCEEDED"
    TIMED_OUT = "TIMED_OUT"
    OUTPUT_LIMIT = "OUTPUT_LIMIT"
    FAILED_TO_START = "FAILED_TO_START"
    NON_ZERO_EXIT = "NON_ZERO_EXIT"


@dataclass(frozen=True, slots=True)
class AnalysisSubprocessResult:
    status: AnalysisSubprocessStatus
    exit_code: int | None
    stdout: bytes
    stderr: bytes


async def run_analysis_subprocess(
    argv: Sequence[str],
    *,
    timeout_seconds: float = 30,
    output_limit: int = 1_000_000,
    environment: dict[str, str] | None = None,
    memory_limit_bytes: int = 512 * 1024 * 1024,
) -> AnalysisSubprocessResult:
    env = {
        key: value
        for key, value in (environment or {}).items()
        if key in {"PATH", "LANG", "LC_ALL", "PYTHONPATH"}
    }

    def limit() -> None:
        resource.setrlimit(resource.RLIMIT_AS, (memory_limit_bytes, memory_limit_bytes))

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
        except OSError:
            return AnalysisSubprocessResult(
                AnalysisSubprocessStatus.FAILED_TO_START, None, b"", b""
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

        async def collect() -> tuple[bytes, bytes, int | None, bool]:
            assert process.stdout is not None
            assert process.stderr is not None
            stdout_task = asyncio.create_task(read_stream(process.stdout))
            stderr_task = asyncio.create_task(read_stream(process.stderr))
            tasks = {stdout_task, stderr_task}
            try:
                while tasks:
                    done, tasks = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                    if any(task.result()[1] for task in done):
                        await kill_and_wait((stdout_task, stderr_task))
                        output = [task.result()[0] for task in (stdout_task, stderr_task)]
                        return output[0], output[1], process.returncode, True
                exit_code = await process.wait()
                stdout, _ = stdout_task.result()
                stderr, _ = stderr_task.result()
                return stdout, stderr, exit_code, False
            except asyncio.CancelledError:
                await kill_and_wait((stdout_task, stderr_task))
                raise

        try:
            stdout, stderr, exit_code, output_limited = await asyncio.wait_for(
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
