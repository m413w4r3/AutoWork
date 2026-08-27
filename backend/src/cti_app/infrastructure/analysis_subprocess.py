from __future__ import annotations

import asyncio
import os
import resource
import tempfile
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Sequence


class AnalysisSubprocessStatus(StrEnum):
    SUCCEEDED = "SUCCEEDED"; TIMED_OUT = "TIMED_OUT"; OUTPUT_LIMIT = "OUTPUT_LIMIT"; FAILED_TO_START = "FAILED_TO_START"; NON_ZERO_EXIT = "NON_ZERO_EXIT"


@dataclass(frozen=True, slots=True)
class AnalysisSubprocessResult:
    status: AnalysisSubprocessStatus
    exit_code: int | None
    stdout: bytes
    stderr: bytes


async def run_analysis_subprocess(argv: Sequence[str], *, timeout_seconds: float = 30, output_limit: int = 1_000_000, environment: dict[str, str] | None = None, memory_limit_bytes: int = 512 * 1024 * 1024) -> AnalysisSubprocessResult:
    env = {key: value for key, value in (environment or {}).items() if key in {"PATH", "LANG", "LC_ALL", "PYTHONPATH"}}
    def limit() -> None:
        resource.setrlimit(resource.RLIMIT_AS, (memory_limit_bytes, memory_limit_bytes))
    with tempfile.TemporaryDirectory(prefix="cti-analysis-") as cwd:
        try:
            process = await asyncio.create_subprocess_exec(*argv, cwd=Path(cwd), stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env, preexec_fn=limit if os.name == "posix" else None)
        except OSError:
            return AnalysisSubprocessResult(AnalysisSubprocessStatus.FAILED_TO_START, None, b"", b"")
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout_seconds)
        except TimeoutError:
            process.kill(); await process.wait()
            return AnalysisSubprocessResult(AnalysisSubprocessStatus.TIMED_OUT, process.returncode, b"", b"")
    if len(stdout) > output_limit or len(stderr) > output_limit:
        return AnalysisSubprocessResult(AnalysisSubprocessStatus.OUTPUT_LIMIT, process.returncode, stdout[:output_limit], stderr[:output_limit])
    return AnalysisSubprocessResult(AnalysisSubprocessStatus.SUCCEEDED if process.returncode == 0 else AnalysisSubprocessStatus.NON_ZERO_EXIT, process.returncode, stdout, stderr)
