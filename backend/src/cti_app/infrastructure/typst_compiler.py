"""Shell-free, bounded Typst 0.15.1 execution and PDF validation."""

from __future__ import annotations

import asyncio
import hashlib
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from io import BytesIO
from pathlib import PurePosixPath
from typing import Any, Protocol

from pypdf import PdfReader

from cti_app.application.typst_compilation import (
    TYPST_COMPILATION_TIMEOUT_SECONDS,
    TYPST_COMPILER,
    TYPST_COMPILER_VERSION,
    TYPST_MAX_PDF_BYTES,
    TYPST_MAX_STDERR_BYTES,
    TYPST_MAX_STDOUT_BYTES,
    CompiledTypstDocument,
    TypstCompileFailedError,
    TypstCompileRequest,
    TypstCompilerUnavailableError,
    TypstCompilerVersionMismatchError,
    TypstCompileTimeoutError,
    TypstOutputInvalidError,
    TypstOutputTooLargeError,
)

TYPST_PROCESS_LOCALE = "C.UTF-8"
TYPST_EXIT_REAP_TIMEOUT_SECONDS = 5.0
_READ_CHUNK_BYTES = 64 * 1024


class TypstProcessStatus(StrEnum):
    SUCCEEDED = "SUCCEEDED"
    FAILED_TO_START = "FAILED_TO_START"
    TIMED_OUT = "TIMED_OUT"
    NON_ZERO_EXIT = "NON_ZERO_EXIT"
    STDOUT_LIMIT = "STDOUT_LIMIT"
    STDERR_LIMIT = "STDERR_LIMIT"


@dataclass(frozen=True, slots=True)
class TypstProcessResult:
    status: TypstProcessStatus
    exit_code: int | None
    stdout: bytes
    stderr: bytes


class TypstProcessRunner(Protocol):
    async def run(
        self,
        argv: Sequence[str],
        *,
        environment: Mapping[str, str],
        timeout_seconds: float,
        stdout_limit: int,
        stderr_limit: int,
    ) -> TypstProcessResult: ...


def typst_process_environment() -> dict[str, str]:
    """Return PATH and fixed locale values, matching the D2 runner policy."""
    return {
        "PATH": os.environ.get("PATH", os.defpath),
        "LANG": TYPST_PROCESS_LOCALE,
        "LC_ALL": TYPST_PROCESS_LOCALE,
    }


class AsyncioTypstProcessRunner:
    """Direct execution with concurrently drained, hard-bounded output pipes."""

    async def run(
        self,
        argv: Sequence[str],
        *,
        environment: Mapping[str, str],
        timeout_seconds: float,
        stdout_limit: int,
        stderr_limit: int,
    ) -> TypstProcessResult:
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=dict(environment),
            )
        except OSError:
            return TypstProcessResult(TypstProcessStatus.FAILED_TO_START, None, b"", b"")

        stdout_stream = process.stdout
        stderr_stream = process.stderr
        assert stdout_stream is not None
        assert stderr_stream is not None

        exit_code: int | None = None
        stdout_bytes = b""
        stderr_bytes = b""

        def close_transport() -> None:
            transport = getattr(process, "_transport", None)
            if transport is not None:
                transport.close()

        def settle(result: TypstProcessResult) -> TypstProcessResult:
            close_transport()
            return result

        def task_bytes(task: asyncio.Task[tuple[bytes, bool]]) -> bytes:
            if not task.done() or task.cancelled() or task.exception() is not None:
                return b""
            return task.result()[0]

        async def read_stream(stream: asyncio.StreamReader, *, limit: int) -> tuple[bytes, bool]:
            buffer = bytearray()
            while True:
                chunk = await stream.read(_READ_CHUNK_BYTES)
                if not chunk:
                    return bytes(buffer), False
                remaining = limit - len(buffer)
                if len(chunk) > remaining:
                    if remaining > 0:
                        buffer.extend(chunk[:remaining])
                    return bytes(buffer), True
                buffer.extend(chunk)

        async def drain_stream(stream: asyncio.StreamReader) -> None:
            while await stream.read(_READ_CHUNK_BYTES):
                pass

        async def kill_and_wait(*tasks: asyncio.Future[Any]) -> None:
            if process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
            await asyncio.gather(*tasks, return_exceptions=True)
            await asyncio.gather(
                drain_stream(stdout_stream),
                drain_stream(stderr_stream),
                return_exceptions=True,
            )
            try:
                await asyncio.wait_for(process.wait(), timeout=TYPST_EXIT_REAP_TIMEOUT_SECONDS)
            except TimeoutError:
                pass

        async def collect() -> TypstProcessStatus:
            nonlocal exit_code, stdout_bytes, stderr_bytes
            stdout_task = asyncio.create_task(read_stream(stdout_stream, limit=stdout_limit))
            stderr_task = asyncio.create_task(read_stream(stderr_stream, limit=stderr_limit))
            stream_tasks = {stdout_task, stderr_task}
            try:
                while stream_tasks:
                    done, stream_tasks = await asyncio.wait(
                        stream_tasks, return_when=asyncio.FIRST_COMPLETED
                    )
                    stdout_overflow = any(task is stdout_task and task.result()[1] for task in done)
                    stderr_overflow = any(task is stderr_task and task.result()[1] for task in done)
                    if stdout_overflow or stderr_overflow:
                        await kill_and_wait(stdout_task, stderr_task)
                        exit_code = process.returncode
                        stdout_bytes = task_bytes(stdout_task)
                        stderr_bytes = task_bytes(stderr_task)
                        return (
                            TypstProcessStatus.STDOUT_LIMIT
                            if stdout_overflow
                            else TypstProcessStatus.STDERR_LIMIT
                        )

                exit_code = await process.wait()
                stdout_bytes = stdout_task.result()[0]
                stderr_bytes = stderr_task.result()[0]
                return (
                    TypstProcessStatus.SUCCEEDED
                    if exit_code == 0
                    else TypstProcessStatus.NON_ZERO_EXIT
                )
            except asyncio.CancelledError:
                await kill_and_wait(stdout_task, stderr_task)
                exit_code = process.returncode
                stdout_bytes = task_bytes(stdout_task)
                stderr_bytes = task_bytes(stderr_task)
                raise
            except BaseException:
                await kill_and_wait(stdout_task, stderr_task)
                raise

        try:
            async with asyncio.timeout(timeout_seconds):
                status = await collect()
        except TimeoutError:
            return settle(
                TypstProcessResult(
                    TypstProcessStatus.TIMED_OUT, exit_code, stdout_bytes, stderr_bytes
                )
            )
        except asyncio.CancelledError:
            close_transport()
            raise
        return settle(TypstProcessResult(status, exit_code, stdout_bytes, stderr_bytes))


def _stderr_note(stderr: bytes) -> str:
    text = stderr.decode("utf-8", errors="replace").strip()
    if not text:
        return ""
    return f": {text[:512]}"


def _raise_process_failure(result: TypstProcessResult) -> None:
    if result.status is TypstProcessStatus.SUCCEEDED and result.exit_code == 0:
        return
    note = _stderr_note(result.stderr)
    if result.status is TypstProcessStatus.FAILED_TO_START:
        raise TypstCompilerUnavailableError(f"typst executable could not be started{note}")
    if result.status is TypstProcessStatus.TIMED_OUT:
        raise TypstCompileTimeoutError(
            f"typst exceeded the {TYPST_COMPILATION_TIMEOUT_SECONDS} second budget{note}"
        )
    if result.status in {
        TypstProcessStatus.STDOUT_LIMIT,
        TypstProcessStatus.STDERR_LIMIT,
    }:
        stream = "stdout" if result.status is TypstProcessStatus.STDOUT_LIMIT else "stderr"
        limit = TYPST_MAX_STDOUT_BYTES if stream == "stdout" else TYPST_MAX_STDERR_BYTES
        raise TypstOutputTooLargeError(
            f"typst {stream} exceeded {limit} bytes and the process was terminated{note}"
        )
    raise TypstCompileFailedError(f"typst exited with code {result.exit_code}{note}")


class TypstSubprocessCompiler:
    """Compile a prepared workspace with pinned Typst and validate its PDF.

    Implements the `TypstCompiler` port (`application/typst_compilation.py`) —
    named distinctly here so the port and this implementation never collide
    when both are imported in the same module, mirroring how `D2DiagramCompiler`
    is named apart from the `DiagramCompiler` port it implements.
    """

    def __init__(
        self,
        runner: TypstProcessRunner | None = None,
        binary: str = TYPST_COMPILER,
    ) -> None:
        self._runner = runner if runner is not None else AsyncioTypstProcessRunner()
        self._binary = binary
        self._version_verified = False
        self._version_lock = asyncio.Lock()

    async def compile(self, request: TypstCompileRequest) -> CompiledTypstDocument:
        workspace_root = request.workspace_root.resolve(strict=True)
        if not workspace_root.is_dir():
            raise TypstCompileFailedError(f"Typst workspace is not a directory: {workspace_root}")
        relative_entrypoint = PurePosixPath(request.entrypoint_relative_path)
        if (
            relative_entrypoint.is_absolute()
            or "\\" in request.entrypoint_relative_path
            or ".." in relative_entrypoint.parts
        ):
            raise TypstCompileFailedError("Typst entrypoint must be a path inside its workspace")
        entrypoint = workspace_root.joinpath(*relative_entrypoint.parts)
        output_path = workspace_root / "output.pdf"

        await self._verify_version()
        try:
            output_path.unlink(missing_ok=True)
        except OSError as exc:
            raise TypstCompileFailedError(
                f"Unable to prepare Typst output path: {output_path}"
            ) from exc

        argv: list[str] = [self._binary, "compile", "--root", str(workspace_root)]
        for font_path in request.font_paths:
            argv.extend(("--font-path", str(font_path)))
        argv.extend(
            (
                "--ignore-system-fonts",
                # Typst embeds the wall-clock compile time in the PDF's
                # CreationDate/ModDate and derives the /ID trailer from it,
                # so two compiles of the same input would otherwise never
                # hash identically. Pin it to make output reproducible.
                "--creation-timestamp",
                "0",
                str(entrypoint),
                str(output_path),
            )
        )
        result = await self._run(argv)
        _raise_process_failure(result)

        try:
            if not output_path.is_file():
                raise TypstOutputInvalidError("Typst did not produce an output PDF")
            output_size = os.stat(output_path).st_size
        except OSError as exc:
            raise TypstOutputInvalidError("Typst output PDF is missing or unreadable") from exc
        if output_size > TYPST_MAX_PDF_BYTES:
            raise TypstOutputTooLargeError(f"Typst PDF exceeded {TYPST_MAX_PDF_BYTES} bytes")
        try:
            pdf_bytes = output_path.read_bytes()
        except OSError as exc:
            raise TypstOutputInvalidError("Typst output PDF is missing or unreadable") from exc
        _validate_pdf(pdf_bytes)
        return CompiledTypstDocument(
            media_type="application/pdf",
            content=pdf_bytes,
            sha256=hashlib.sha256(pdf_bytes).hexdigest(),
            byte_size=len(pdf_bytes),
            compiler=TYPST_COMPILER,
            compiler_version=TYPST_COMPILER_VERSION,
        )

    async def _run(self, argv: Sequence[str]) -> TypstProcessResult:
        try:
            return await self._runner.run(
                argv,
                environment=typst_process_environment(),
                timeout_seconds=float(TYPST_COMPILATION_TIMEOUT_SECONDS),
                stdout_limit=TYPST_MAX_STDOUT_BYTES,
                stderr_limit=TYPST_MAX_STDERR_BYTES,
            )
        except OSError as exc:
            raise TypstCompilerUnavailableError("typst executable could not be started") from exc

    async def _verify_version(self) -> None:
        if self._version_verified:
            return
        async with self._version_lock:
            if self._version_verified:
                return
            result = await self._run((self._binary, "--version"))
            _raise_process_failure(result)
            reported = result.stdout.decode("utf-8", errors="replace").strip().split()
            reported_version = reported[1] if len(reported) >= 2 and reported[0] == "typst" else ""
            if reported_version != TYPST_COMPILER_VERSION:
                report = result.stdout.decode("utf-8", errors="replace").strip()
                raise TypstCompilerVersionMismatchError(
                    f"required Typst version {TYPST_COMPILER_VERSION}, reported {report[:64]!r}"
                )
            self._version_verified = True


def _validate_pdf(pdf_bytes: bytes) -> None:
    if not pdf_bytes:
        raise TypstOutputInvalidError("Typst PDF output is empty")
    if not pdf_bytes.startswith(b"%PDF-"):
        raise TypstOutputInvalidError("Typst output does not start with a PDF header")
    try:
        reader = PdfReader(BytesIO(pdf_bytes), strict=True)
        is_encrypted = reader.is_encrypted
        page_count = len(reader.pages)
    except Exception as exc:
        raise TypstOutputInvalidError("Typst output is not a parseable PDF") from exc
    if page_count < 1:
        raise TypstOutputInvalidError("Typst PDF output has no pages")
    if is_encrypted:
        raise TypstOutputInvalidError("Typst PDF output is encrypted")
