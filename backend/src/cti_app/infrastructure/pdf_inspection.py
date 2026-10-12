from __future__ import annotations

import asyncio
import os
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cti_app.application.pdf_inspection import (
    MAX_WARNING_COUNT,
    MAX_WARNING_LENGTH,
    PREVIEW_DPI,
    PREVIEW_MAX_EDGE_PX,
    THUMBNAIL_MAX_EDGE_PX,
    PdfInspectionResult,
)
from cti_app.domain.pdf_inspection import PdfInspectionProfile

try:
    import resource
except ImportError:  # pragma: no cover - Linux backend images provide resource
    resource = None  # type: ignore[assignment]

_POPPLER_SEMAPHORE = asyncio.Semaphore(2)
_PAGE_SIZE = re.compile(r"(?:Page\s+\d+\s+size|Page size):\s*([\d.]+)\s+x\s+([\d.]+)\s+pts")
_PAGES = re.compile(r"^Pages:\s*(\d+)\s*$", re.MULTILINE)
_ENCRYPTED = re.compile(r"^Encrypted:\s*(yes|no)(?:\s+.*)?$", re.MULTILINE | re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class _CommandOutput:
    stdout: bytes
    stderr: bytes
    returncode: int


class PopplerCommandError(RuntimeError):
    def __init__(self, message: str, warnings: tuple[str, ...] = ()) -> None:
        super().__init__(message)
        self.warnings = warnings


class PopplerSubprocess:
    """Bounded, deterministic Poppler process adapter; never invokes a shell."""

    def __init__(self, *, temp_root: Path | None = None) -> None:
        self._temp_root = (
            temp_root or Path(tempfile.gettempdir()) / "cti-app-blobs" / "pdf-inspections"
        )
        self._temp_root.mkdir(parents=True, exist_ok=True)
        self._pdfinfo = shutil.which("pdfinfo")
        self._pdffonts = shutil.which("pdffonts")
        self._pdftotext = shutil.which("pdftotext")
        self._pdftoppm = shutil.which("pdftoppm")
        self._prlimit = shutil.which("prlimit")
        if not all((self._pdfinfo, self._pdffonts, self._pdftotext, self._pdftoppm)):
            raise RuntimeError("Poppler commands are not installed")
        version_result = self._version()
        self.build = f"poppler-{version_result}"

    def _version(self) -> str:
        assert self._pdftoppm is not None
        result = subprocess.run(
            [self._pdftoppm, "-v"],
            check=False,
            capture_output=True,
            timeout=5,
            env={"LC_ALL": "C", "LANG": "C", "TZ": "UTC", "PATH": os.defpath},
        )
        text = (result.stderr or result.stdout).decode("utf-8", errors="replace")[:512]
        match = re.search(r"version\s+([0-9][\w.+-]*)", text, re.IGNORECASE)
        return match.group(1) if match else "unknown"

    async def inspect(self, content: bytes) -> PdfInspectionResult:
        warnings: list[str] = []
        with tempfile.TemporaryDirectory(prefix="pdf-inspect-", dir=self._temp_root) as directory:
            root = Path(directory)
            pdf_path = root / "input.pdf"
            pdf_path.write_bytes(content)
            info = await self._run(
                [self._pdfinfo or "pdfinfo", "-box", str(pdf_path)],
                root,
                timeout_seconds=15,
                allow_nonzero=True,
            )
            if info.returncode != 0 and not info.stderr:
                warnings.append(f"pdfinfo exited with status {info.returncode}")
            warnings.extend(self._warnings(info.stderr))
            info_text = info.stdout.decode("utf-8", errors="replace")
            page_count_match = _PAGES.search(info_text)
            size_match = _PAGE_SIZE.search(info_text)
            encrypted_match = _ENCRYPTED.search(info_text)
            if page_count_match is None or size_match is None:
                return PdfInspectionResult(
                    readable=False,
                    page_count=int(page_count_match.group(1)) if page_count_match else 0,
                    page_width_points=float(size_match.group(1)) if size_match else None,
                    page_height_points=float(size_match.group(2)) if size_match else None,
                    encrypted=bool(encrypted_match and encrypted_match.group(1).lower() == "yes"),
                    parser_warnings=self._bounded_warnings(warnings),
                    non_embedded_fonts=(),
                    page_occupancies=(),
                    page_word_counts=(),
                )
            page_count = int(page_count_match.group(1))
            width, height = float(size_match.group(1)), float(size_match.group(2))
            encrypted = bool(encrypted_match and encrypted_match.group(1).lower() == "yes")
            if page_count > 200 or encrypted:
                return PdfInspectionResult(
                    readable=True,
                    page_count=page_count,
                    page_width_points=width,
                    page_height_points=height,
                    encrypted=encrypted,
                    parser_warnings=self._bounded_warnings(warnings),
                    non_embedded_fonts=(),
                    page_occupancies=(),
                    page_word_counts=(),
                )

            fonts = await self._run(
                [self._pdffonts or "pdffonts", str(pdf_path)],
                root,
                timeout_seconds=15,
                allow_nonzero=True,
            )
            if fonts.returncode != 0 and not fonts.stderr:
                warnings.append(f"pdffonts exited with status {fonts.returncode}")
            warnings.extend(self._warnings(fonts.stderr))
            font_text = fonts.stdout.decode("utf-8", errors="replace")
            if "name" not in font_text or "emb" not in font_text or "object ID" not in font_text:
                warnings.append("pdffonts returned invalid output")
                non_embedded_fonts: tuple[str, ...] = ()
            else:
                non_embedded_fonts = self._non_embedded_fonts(font_text)

            text_result = await self._run(
                [self._pdftotext or "pdftotext", "-bbox-layout", str(pdf_path), "-"],
                root,
                timeout_seconds=15,
                max_output=8 * 1024 * 1024,
                allow_nonzero=True,
            )
            if text_result.returncode != 0 and not text_result.stderr:
                warnings.append(f"pdftotext exited with status {text_result.returncode}")
            warnings.extend(self._warnings(text_result.stderr))
            try:
                page_word_counts, page_occupancies = self._occupancies(
                    text_result.stdout, page_count
                )
            except (ET.ParseError, OSError, ValueError) as exc:
                warnings.append(f"pdftotext returned invalid bbox output: {exc}")
                page_word_counts, page_occupancies = (), ()
            return PdfInspectionResult(
                readable=True,
                page_count=page_count,
                page_width_points=width,
                page_height_points=height,
                encrypted=encrypted,
                parser_warnings=self._bounded_warnings(warnings),
                non_embedded_fonts=non_embedded_fonts,
                page_occupancies=page_occupancies,
                page_word_counts=page_word_counts,
            )

    async def page_size(self, content: bytes, page: int) -> tuple[float, float] | None:
        with tempfile.TemporaryDirectory(prefix="pdf-page-info-", dir=self._temp_root) as directory:
            root = Path(directory)
            pdf_path = root / "input.pdf"
            pdf_path.write_bytes(content)
            result = await self._run(
                [
                    self._pdfinfo or "pdfinfo",
                    "-f",
                    str(page),
                    "-l",
                    str(page),
                    "-box",
                    str(pdf_path),
                ],
                root,
                timeout_seconds=15,
            )
            text = result.stdout.decode("utf-8", errors="replace")
            match = _PAGE_SIZE.search(text)
            return (float(match.group(1)), float(match.group(2))) if match else None

    async def rasterize(self, content: bytes, page: int, profile: PdfInspectionProfile) -> bytes:
        with tempfile.TemporaryDirectory(prefix="pdf-raster-", dir=self._temp_root) as directory:
            root = Path(directory)
            pdf_path = root / "input.pdf"
            output_prefix = root / "page"
            pdf_path.write_bytes(content)
            width, height = await self.page_size(content, page) or (0, 0)
            if width <= 0 or height <= 0:
                raise PopplerCommandError("pdfinfo did not report the requested page size")
            if profile is PdfInspectionProfile.PREVIEW:
                natural_edge = max(width, height) / 72 * PREVIEW_DPI
                params = (
                    ["-scale-to", str(PREVIEW_MAX_EDGE_PX)]
                    if natural_edge > PREVIEW_MAX_EDGE_PX
                    else ["-r", str(PREVIEW_DPI)]
                )
            else:
                params = ["-scale-to", str(THUMBNAIL_MAX_EDGE_PX)]
            await self._run(
                [
                    self._pdftoppm or "pdftoppm",
                    "-f",
                    str(page),
                    "-l",
                    str(page),
                    *params,
                    "-png",
                    "-singlefile",
                    str(pdf_path),
                    str(output_prefix),
                ],
                root,
                timeout_seconds=15,
            )
            output = root / "page.png"
            if not output.is_file() or output.stat().st_size > 25 * 1024 * 1024:
                raise PopplerCommandError("pdftoppm returned no bounded PNG")
            return output.read_bytes()

    async def _run(
        self,
        command: list[str],
        directory: Path,
        *,
        timeout_seconds: float,
        max_output: int = 1024 * 1024,
        allow_nonzero: bool = False,
    ) -> _CommandOutput:
        env = {"LC_ALL": "C", "LANG": "C", "TZ": "UTC", "PATH": os.defpath}
        preexec_fn: Callable[[], None] | None = _limit_process_resources
        if self._prlimit is not None:
            memory_limit = 512 * 1024 * 1024
            command = [
                self._prlimit,
                f"--as={memory_limit}:{memory_limit}",
                "--cpu=14:15",
                "--fsize=16777216:16777216",
                "--",
                *command,
            ]
            preexec_fn = None
        stdout_path = directory / "stdout.bin"
        stderr_path = directory / "stderr.bin"
        async with _POPPLER_SEMAPHORE:
            with stdout_path.open("wb") as stdout_file, stderr_path.open("wb") as stderr_file:
                # Popen.poll is non-blocking; process startup is the only synchronous operation.
                process = subprocess.Popen(  # noqa: ASYNC220
                    command,
                    cwd=directory,
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=stdout_file,
                    stderr=stderr_file,
                    start_new_session=True,
                    preexec_fn=preexec_fn,
                )
                deadline = asyncio.get_running_loop().time() + timeout_seconds
                try:
                    while process.poll() is None:
                        if asyncio.get_running_loop().time() >= deadline:
                            raise TimeoutError("Poppler command timed out")
                        await asyncio.sleep(0.02)
                except BaseException:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    process.wait()
                    raise
            if stdout_path.stat().st_size > max_output or stderr_path.stat().st_size > 64 * 1024:
                raise PopplerCommandError("Poppler returned oversized output")
            stdout = stdout_path.read_bytes()
            stderr = stderr_path.read_bytes()
            if process.returncode != 0 and not allow_nonzero:
                warnings = self._warnings(stderr)
                raise PopplerCommandError(
                    f"Poppler command failed with status {process.returncode}", tuple(warnings)
                )
            return _CommandOutput(
                stdout=stdout,
                stderr=stderr,
                returncode=process.returncode or 0,
            )

    @staticmethod
    def _warnings(stderr: bytes) -> list[str]:
        return [
            line.strip()
            for line in stderr.decode("utf-8", errors="replace").splitlines()
            if line.strip()
        ]

    @staticmethod
    def _bounded_warnings(warnings: list[str] | tuple[str, ...]) -> tuple[str, ...]:
        return tuple(item[:MAX_WARNING_LENGTH] for item in warnings[:MAX_WARNING_COUNT])

    @staticmethod
    def _non_embedded_fonts(output: str) -> tuple[str, ...]:
        lines = output.splitlines()
        result: list[str] = []
        for line in lines[2:]:
            parts = line.split()
            if len(parts) >= 8 and parts[-5].lower() in {"no", "n"}:
                result.append(parts[0][:256])
        return tuple(result)

    @staticmethod
    def _occupancies(
        output: bytes, page_count: int
    ) -> tuple[tuple[int, ...], tuple[float | None, ...]]:
        root = ET.fromstring(output)
        pages = [item for item in root.iter() if item.tag.rsplit("}", 1)[-1] == "page"]
        counts: list[int] = []
        ratios: list[float | None] = []
        for page in pages[:page_count]:
            width = float(page.attrib["width"])
            height = float(page.attrib["height"])
            words = [item for item in page.iter() if item.tag.rsplit("}", 1)[-1] == "word"]
            area = 0.0
            for word in words:
                x0 = float(word.attrib["xMin"])
                y0 = float(word.attrib["yMin"])
                x1 = float(word.attrib["xMax"])
                y1 = float(word.attrib["yMax"])
                area += max(0, x1 - x0) * max(0, y1 - y0)
            counts.append(len(words))
            ratios.append(min(1.0, area / (width * height)) if width > 0 and height > 0 else None)
        while len(counts) < page_count:
            counts.append(0)
            ratios.append(None)
        return tuple(counts), tuple(ratios)


class PdfInspectionCache:
    """Ephemeral disk caches with atomic writes, TTLs, and separate PDF/raster quotas."""

    def __init__(
        self,
        root: Path | None = None,
        *,
        derived_max_bytes: int = 512 * 1024 * 1024,
        derived_ttl_seconds: int = 24 * 60 * 60,
        preview_max_bytes: int = 100 * 1024 * 1024,
        preview_ttl_seconds: int = 10 * 60,
    ) -> None:
        base = root or Path(tempfile.gettempdir()) / "cti-app-blobs" / "pdf-inspections"
        self._derived = base / "derived"
        self._previews = base / "previews"
        self._derived.mkdir(parents=True, exist_ok=True)
        self._previews.mkdir(parents=True, exist_ok=True)
        self._derived_max_bytes = derived_max_bytes
        self._derived_ttl_seconds = derived_ttl_seconds
        self._preview_max_bytes = preview_max_bytes
        self._preview_ttl_seconds = preview_ttl_seconds
        self._lock = threading.RLock()
        self._preview_entries: dict[str, _PreviewEntry] = {}
        self._expired_hashes: dict[str, float] = {}

    def get_preview(self, pdf_sha256: str) -> tuple[bytes, tuple[dict[str, Any], ...], bool] | None:
        with self._lock:
            self._prune_previews(time.time())
            entry = self._preview_entries.get(pdf_sha256)
            if entry is None:
                if (
                    pdf_sha256 in self._expired_hashes
                    or (self._previews / f"{pdf_sha256}.pdf").exists()
                ):
                    self._expired_hashes[pdf_sha256] = time.time()
                    (self._previews / f"{pdf_sha256}.pdf").unlink(missing_ok=True)
                    return b"", (), True
                return None
            path = self._previews / f"{pdf_sha256}.pdf"
            if not path.is_file() or path.stat().st_size != entry.size:
                self._preview_entries.pop(pdf_sha256, None)
                self._expired_hashes[pdf_sha256] = time.time()
                return b"", (), True
            entry.last_access = time.time()
            os.utime(path, None)
            return path.read_bytes(), tuple(dict(item) for item in entry.provenances), False

    def put_preview(self, pdf_sha256: str, content: bytes, provenance: dict[str, Any]) -> None:
        if len(content) > self._preview_max_bytes:
            raise ValueError("preview PDF cache quota exceeded")
        now = time.time()
        with self._lock:
            self._prune_previews(now)
            path = self._previews / f"{pdf_sha256}.pdf"
            current = self._preview_entries.get(pdf_sha256)
            if current is None:
                self._atomic_write(path, content)
                current = _PreviewEntry(len(content), now, now, [])
                self._preview_entries[pdf_sha256] = current
            elif path.is_file() and path.stat().st_size == len(content):
                self._atomic_write(path, content)
                current.last_access = now
                current.created_at = now
            else:
                self._atomic_write(path, content)
                current.size = len(content)
                current.created_at = now
                current.last_access = now
                current.provenances.clear()
            if provenance not in current.provenances:
                current.provenances.append(dict(provenance))
            current.last_access = now
            self._expired_hashes.pop(pdf_sha256, None)
            self._evict_previews()

    def get_derived(self, key: str) -> bytes | None:
        path = self._derived_path(key)
        with self._lock:
            now = time.time()
            self._prune_derived(now)
            if not path.is_file():
                return None
            os.utime(path, None)
            return path.read_bytes()

    def put_derived(self, key: str, content: bytes) -> None:
        path = self._derived_path(key)
        with self._lock:
            self._atomic_write(path, content)
            os.utime(path, None)
            self._evict_derived()

    def delete_derived(self, key: str) -> None:
        with self._lock:
            self._derived_path(key).unlink(missing_ok=True)

    def _derived_path(self, key: str) -> Path:
        import hashlib

        safe = hashlib.sha256(key.encode()).hexdigest()
        return self._derived / f"{safe}.cache"

    @staticmethod
    def _atomic_write(path: Path, content: bytes) -> None:
        fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, path)
        finally:
            Path(name).unlink(missing_ok=True)

    def _prune_derived(self, now: float) -> None:
        for path in self._derived.iterdir():
            try:
                if now - path.stat().st_mtime > self._derived_ttl_seconds:
                    path.unlink(missing_ok=True)
            except FileNotFoundError:
                continue

    def _evict_derived(self) -> None:
        entries = sorted(
            (
                (path.stat().st_mtime, path.stat().st_size, path)
                for path in self._derived.iterdir()
                if path.is_file()
            ),
            key=lambda item: item[0],
        )
        total = sum(size for _, size, _ in entries)
        for _, size, path in entries:
            if total <= self._derived_max_bytes:
                break
            path.unlink(missing_ok=True)
            total -= size

    def _prune_previews(self, now: float) -> None:
        for pdf_sha256, entry in list(self._preview_entries.items()):
            if now - entry.created_at > self._preview_ttl_seconds:
                (self._previews / f"{pdf_sha256}.pdf").unlink(missing_ok=True)
                self._preview_entries.pop(pdf_sha256, None)
                self._expired_hashes[pdf_sha256] = now
        for pdf_sha256, expired_at in list(self._expired_hashes.items()):
            if now - expired_at > 24 * 60 * 60:
                self._expired_hashes.pop(pdf_sha256, None)

    def _evict_previews(self) -> None:
        entries = sorted(self._preview_entries.items(), key=lambda item: item[1].last_access)
        total = sum(entry.size for _, entry in entries)
        for pdf_sha256, entry in entries:
            if total <= self._preview_max_bytes:
                break
            (self._previews / f"{pdf_sha256}.pdf").unlink(missing_ok=True)
            self._preview_entries.pop(pdf_sha256, None)
            self._expired_hashes[pdf_sha256] = time.time()
            total -= entry.size


@dataclass(slots=True)
class _PreviewEntry:
    size: int
    created_at: float
    last_access: float
    provenances: list[dict[str, Any]]


def _limit_process_resources() -> None:
    if resource is None:
        return
    memory_limit = 512 * 1024 * 1024
    resource.setrlimit(resource.RLIMIT_AS, (memory_limit, memory_limit))
    resource.setrlimit(resource.RLIMIT_CPU, (14, 15))
    resource.setrlimit(resource.RLIMIT_FSIZE, (16 * 1024 * 1024, 16 * 1024 * 1024))
