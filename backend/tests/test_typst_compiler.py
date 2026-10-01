from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Mapping, Sequence
from io import BytesIO
from pathlib import Path

import pytest
from pypdf import PdfWriter

from cti_app.application.typst_compilation import (
    TYPST_COMPILER_VERSION,
    TYPST_MAX_PDF_BYTES,
    CompiledTypstDocument,
    FontBundleInvalidError,
    TypstCompileFailedError,
    TypstCompileRequest,
    TypstCompilerUnavailableError,
    TypstCompilerVersionMismatchError,
    TypstCompileTimeoutError,
    TypstOutputInvalidError,
    TypstOutputTooLargeError,
    compute_font_bundle_version,
)
from cti_app.infrastructure.typst_compiler import (
    AsyncioTypstProcessRunner,
    TypstProcessResult,
    TypstProcessStatus,
    TypstSubprocessCompiler,
)


def _valid_pdf() -> bytes:
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


class FakeTypstProcessRunner:
    def __init__(
        self,
        *,
        version_stdout: bytes = b"typst 0.15.1 (test-build)\n",
        version_result: TypstProcessResult | None = None,
        compile_result: TypstProcessResult | None = None,
        pdf_bytes: bytes | None = None,
        pdf_size: int | None = None,
    ) -> None:
        self.version_stdout = version_stdout
        self.version_result = version_result
        self.compile_result = compile_result or TypstProcessResult(
            TypstProcessStatus.SUCCEEDED, 0, b"", b""
        )
        self.pdf_bytes = pdf_bytes
        self.pdf_size = pdf_size
        self.calls: list[tuple[str, ...]] = []

    async def run(
        self,
        argv: Sequence[str],
        *,
        environment: Mapping[str, str],
        timeout_seconds: float,
        stdout_limit: int,
        stderr_limit: int,
    ) -> TypstProcessResult:
        del environment, timeout_seconds, stdout_limit, stderr_limit
        command = tuple(argv)
        self.calls.append(command)
        if command[-1] == "--version":
            if self.version_result is not None:
                return self.version_result
            return TypstProcessResult(TypstProcessStatus.SUCCEEDED, 0, self.version_stdout, b"")

        if self.compile_result.status is TypstProcessStatus.SUCCEEDED:
            output_path = Path(command[-1])
            _write_output_file(output_path, self.pdf_bytes, self.pdf_size)
        return self.compile_result


def _write_output_file(output_path: Path, pdf_bytes: bytes | None, pdf_size: int | None) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if pdf_size is not None:
        with output_path.open("wb") as output:
            output.truncate(pdf_size)
    elif pdf_bytes is not None:
        output_path.write_bytes(pdf_bytes)


def _request(tmp_path: Path, *, font_paths: tuple[Path, ...] = ()) -> TypstCompileRequest:
    workspace = tmp_path / "workspace"
    entrypoint = workspace / "RENDERER" / "publication.typ"
    entrypoint.parent.mkdir(parents=True)
    entrypoint.write_text("Hello from the self-contained compiler fixture.\n", encoding="utf-8")
    return TypstCompileRequest(
        workspace_root=workspace,
        entrypoint_relative_path="RENDERER/publication.typ",
        font_paths=font_paths,
    )


@pytest.mark.asyncio
async def test_valid_pdf_compiles_and_checks_version_only_once(tmp_path: Path) -> None:
    pdf_bytes = _valid_pdf()
    runner = FakeTypstProcessRunner(pdf_bytes=pdf_bytes)
    request = _request(tmp_path, font_paths=(tmp_path / "fonts-a", tmp_path / "fonts-b"))
    compiler = TypstSubprocessCompiler(runner=runner)

    first = await compiler.compile(request)
    second = await compiler.compile(request)

    assert isinstance(first, CompiledTypstDocument)
    assert first.media_type == "application/pdf"
    assert first.content == pdf_bytes
    assert first.sha256 == hashlib.sha256(pdf_bytes).hexdigest()
    assert first.byte_size == len(pdf_bytes)
    assert first.compiler == "typst"
    assert first.compiler_version == TYPST_COMPILER_VERSION
    assert second.sha256 == first.sha256
    assert runner.calls[0] == ("typst", "--version")
    assert sum(call[-1] == "--version" for call in runner.calls) == 1
    compile_argv = runner.calls[1]
    assert compile_argv == (
        "typst",
        "compile",
        "--root",
        str(request.workspace_root.resolve()),
        "--font-path",
        str(request.font_paths[0]),
        "--font-path",
        str(request.font_paths[1]),
        "--ignore-system-fonts",
        "--creation-timestamp",
        "0",
        str(request.workspace_root.resolve() / request.entrypoint_relative_path),
        str(request.workspace_root.resolve() / "output.pdf"),
    )


@pytest.mark.asyncio
async def test_wrong_reported_version_is_rejected(tmp_path: Path) -> None:
    runner = FakeTypstProcessRunner(version_stdout=b"typst 0.15.0 (old-build)\n")

    with pytest.raises(TypstCompilerVersionMismatchError) as error:
        await TypstSubprocessCompiler(runner=runner).compile(_request(tmp_path))

    assert error.value.code == "typst_compiler_version_mismatch"


@pytest.mark.asyncio
async def test_missing_binary_maps_spawn_oserror_to_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fail_to_spawn(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise FileNotFoundError("typst")

    monkeypatch.setattr(
        "cti_app.infrastructure.typst_compiler.asyncio.create_subprocess_exec", fail_to_spawn
    )

    compiler = TypstSubprocessCompiler(runner=AsyncioTypstProcessRunner(), binary="missing-typst")
    with pytest.raises(TypstCompilerUnavailableError) as error:
        await compiler.compile(_request(tmp_path))

    assert error.value.code == "typst_compiler_unavailable"


@pytest.mark.asyncio
async def test_timeout_maps_to_stable_error(tmp_path: Path) -> None:
    runner = FakeTypstProcessRunner(
        compile_result=TypstProcessResult(TypstProcessStatus.TIMED_OUT, None, b"", b"slow")
    )

    with pytest.raises(TypstCompileTimeoutError) as error:
        await TypstSubprocessCompiler(runner=runner).compile(_request(tmp_path))

    assert error.value.code == "typst_compile_timeout"


@pytest.mark.asyncio
async def test_nonzero_exit_maps_to_compile_failed_with_stderr(tmp_path: Path) -> None:
    runner = FakeTypstProcessRunner(
        compile_result=TypstProcessResult(
            TypstProcessStatus.NON_ZERO_EXIT, 7, b"", b"template error"
        )
    )

    with pytest.raises(TypstCompileFailedError, match="template error") as error:
        await TypstSubprocessCompiler(runner=runner).compile(_request(tmp_path))

    assert error.value.code == "typst_compile_failed"


@pytest.mark.asyncio
async def test_stream_overflow_maps_to_output_too_large(tmp_path: Path) -> None:
    for status in (TypstProcessStatus.STDOUT_LIMIT, TypstProcessStatus.STDERR_LIMIT):
        runner = FakeTypstProcessRunner(compile_result=TypstProcessResult(status, -9, b"", b""))
        with pytest.raises(TypstOutputTooLargeError) as error:
            await TypstSubprocessCompiler(runner=runner).compile(_request(tmp_path / status.value))
        assert error.value.code == "typst_output_too_large"


@pytest.mark.asyncio
async def test_oversized_pdf_is_rejected_before_reading_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = FakeTypstProcessRunner(pdf_size=TYPST_MAX_PDF_BYTES + 1)
    request = _request(tmp_path)
    output_path = request.workspace_root / "output.pdf"
    original_read_bytes = Path.read_bytes
    read_attempts: list[Path] = []

    def guarded_read_bytes(path: Path) -> bytes:
        read_attempts.append(path)
        if path == output_path:
            raise AssertionError("oversized PDF was read before its size was checked")
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", guarded_read_bytes)

    with pytest.raises(TypstOutputTooLargeError) as error:
        await TypstSubprocessCompiler(runner=runner).compile(request)

    assert error.value.code == "typst_output_too_large"
    assert output_path not in read_attempts


@pytest.mark.asyncio
@pytest.mark.parametrize("pdf_bytes", (b"not a PDF", b"%PDF-1.7\ncorrupt body"))
async def test_invalid_pdf_is_rejected(tmp_path: Path, pdf_bytes: bytes) -> None:
    runner = FakeTypstProcessRunner(pdf_bytes=pdf_bytes)

    with pytest.raises(TypstOutputInvalidError) as error:
        await TypstSubprocessCompiler(runner=runner).compile(_request(tmp_path))

    assert error.value.code == "typst_output_invalid"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stdout_data", "stderr_data", "expected_status"),
    (
        (b"12345", b"", TypstProcessStatus.STDOUT_LIMIT),
        (b"", b"12345", TypstProcessStatus.STDERR_LIMIT),
    ),
)
async def test_process_runner_kills_and_reaps_on_either_stream_limit(
    monkeypatch: pytest.MonkeyPatch,
    stdout_data: bytes,
    stderr_data: bytes,
    expected_status: TypstProcessStatus,
) -> None:
    stdout_stream = asyncio.StreamReader()
    stderr_stream = asyncio.StreamReader()
    stdout_stream.feed_data(stdout_data)
    stdout_stream.feed_eof()
    stderr_stream.feed_data(stderr_data)
    stderr_stream.feed_eof()

    class FakeProcess:
        stdout = stdout_stream
        stderr = stderr_stream
        returncode: int | None = None

        def __init__(self) -> None:
            self.killed = False
            self.wait_called = False

        def kill(self) -> None:
            self.killed = True
            self.returncode = -9

        async def wait(self) -> int:
            self.wait_called = True
            assert self.returncode is not None
            return self.returncode

    process = FakeProcess()

    async def create_process(*args: object, **kwargs: object) -> FakeProcess:
        del args, kwargs
        return process

    monkeypatch.setattr(
        "cti_app.infrastructure.typst_compiler.asyncio.create_subprocess_exec", create_process
    )

    result = await AsyncioTypstProcessRunner().run(
        ("typst", "compile"),
        environment={"PATH": "/bin"},
        timeout_seconds=1.0,
        stdout_limit=4,
        stderr_limit=4,
    )

    assert result.status is expected_status
    assert process.killed
    assert process.wait_called
    assert process.returncode == -9


@pytest.mark.asyncio
async def test_process_runner_kills_and_reaps_on_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stdout_stream = asyncio.StreamReader()
    stderr_stream = asyncio.StreamReader()

    class HangingFakeProcess:
        stdout = stdout_stream
        stderr = stderr_stream
        returncode: int | None = None

        def __init__(self) -> None:
            self.killed = False
            self.wait_called = False

        def kill(self) -> None:
            self.killed = True
            self.returncode = -9
            stdout_stream.feed_eof()
            stderr_stream.feed_eof()

        async def wait(self) -> int:
            self.wait_called = True
            assert self.returncode is not None
            return self.returncode

    process = HangingFakeProcess()

    async def create_process(*args: object, **kwargs: object) -> HangingFakeProcess:
        del args, kwargs
        return process

    monkeypatch.setattr(
        "cti_app.infrastructure.typst_compiler.asyncio.create_subprocess_exec", create_process
    )

    result = await AsyncioTypstProcessRunner().run(
        ("typst", "compile"),
        environment={"PATH": "/bin"},
        timeout_seconds=0.01,
        stdout_limit=4,
        stderr_limit=4,
    )

    assert result.status is TypstProcessStatus.TIMED_OUT
    assert process.killed
    assert process.wait_called
    assert process.returncode == -9


def test_font_bundle_hash_uses_the_template_framing_scheme(tmp_path: Path) -> None:
    root = tmp_path / "chpTypst"
    (root / "fonts").mkdir(parents=True)
    file_a = b"font-a-bytes"
    file_b = b"font-b-bytes"
    (root / "fonts" / "a.ttf").write_bytes(file_a)
    (root / "fonts" / "z.ttf").write_bytes(file_b)
    lock_path = tmp_path / "typst-fonts.lock"
    lock_path.write_text(
        json.dumps(
            {
                "font_bundle_label": "test-fonts-v1",
                "files": ["fonts/z.ttf", "fonts/a.ttf"],
            }
        ),
        encoding="utf-8",
    )

    expected = hashlib.sha256()
    for relative_path, contents in (("fonts/a.ttf", file_a), ("fonts/z.ttf", file_b)):
        path_bytes = relative_path.encode("utf-8")
        expected.update(len(path_bytes).to_bytes(8, "big"))
        expected.update(path_bytes)
        expected.update(len(contents).to_bytes(8, "big"))
        expected.update(contents)

    assert compute_font_bundle_version(root, lock_path) == expected.hexdigest()


@pytest.mark.parametrize("listed_path", ("../outside.ttf", "/outside.ttf"))
def test_font_bundle_hash_rejects_paths_outside_root(tmp_path: Path, listed_path: str) -> None:
    root = tmp_path / "chpTypst"
    root.mkdir()
    lock_path = tmp_path / "typst-fonts.lock"
    lock_path.write_text(
        json.dumps({"font_bundle_label": "test-fonts-v1", "files": [listed_path]}),
        encoding="utf-8",
    )

    with pytest.raises(FontBundleInvalidError, match=r"stay inside|escapes"):
        compute_font_bundle_version(root, lock_path)


def test_font_bundle_hash_rejects_missing_listed_file(tmp_path: Path) -> None:
    root = tmp_path / "chpTypst"
    root.mkdir()
    lock_path = tmp_path / "typst-fonts.lock"
    lock_path.write_text(
        json.dumps({"font_bundle_label": "test-fonts-v1", "files": ["fonts/missing.ttf"]}),
        encoding="utf-8",
    )

    with pytest.raises(FontBundleInvalidError, match="missing"):
        compute_font_bundle_version(root, lock_path)
