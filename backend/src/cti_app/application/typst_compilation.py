"""Application port and result contract for compiling Typst workspaces."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Protocol

from cti_app.application.typst_bundle import hash_bundle_contents, resolve_bundle_files

TYPST_COMPILER = "typst"
TYPST_COMPILER_VERSION = "0.15.1"
TYPST_COMPILATION_TIMEOUT_SECONDS = 20
TYPST_MAX_STDOUT_BYTES = 64 * 1024
TYPST_MAX_STDERR_BYTES = 128 * 1024
TYPST_MAX_PDF_BYTES = 25 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class TypstCompileRequest:
    workspace_root: Path
    entrypoint_relative_path: str
    font_paths: tuple[Path, ...]


@dataclass(frozen=True, slots=True)
class CompiledTypstDocument:
    media_type: str
    content: bytes
    sha256: str
    byte_size: int
    compiler: str
    compiler_version: str


class TypstCompiler(Protocol):
    async def compile(self, request: TypstCompileRequest) -> CompiledTypstDocument: ...


class TypstCompilationError(Exception):
    """Base class for stable, typed application compilation failures."""

    code = "typst_compilation_error"


class TypstCompilerUnavailableError(TypstCompilationError):
    code = "typst_compiler_unavailable"


class TypstCompilerVersionMismatchError(TypstCompilationError):
    code = "typst_compiler_version_mismatch"


class TypstCompileTimeoutError(TypstCompilationError):
    code = "typst_compile_timeout"


class TypstCompileFailedError(TypstCompilationError):
    code = "typst_compile_failed"


class TypstOutputTooLargeError(TypstCompilationError):
    code = "typst_output_too_large"


class TypstOutputInvalidError(TypstCompilationError):
    code = "typst_output_invalid"


class FontBundleInvalidError(ValueError):
    """Raised when the font lock or one of its listed files is invalid."""


@dataclass(frozen=True, slots=True)
class FontBundleSnapshot:
    font_bundle_version: str
    files: tuple[tuple[str, bytes], ...]


def _load_font_bundle_files(
    font_bundle_root: Path, lock_path: Path
) -> tuple[Path, tuple[tuple[str, Path], ...]]:
    """Load and validate the font lock and all files beneath its bundle root."""
    try:
        root = font_bundle_root.resolve(strict=True)
    except OSError as exc:
        raise FontBundleInvalidError(
            f"Typst font bundle root is missing or unreadable: {font_bundle_root}"
        ) from exc
    if not root.is_dir():
        raise FontBundleInvalidError(f"Typst font bundle root is not a directory: {root}")

    try:
        manifest = json.loads(lock_path.read_bytes())
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise FontBundleInvalidError(f"Typst font lock is missing or invalid: {lock_path}") from exc
    if not isinstance(manifest, dict):
        raise FontBundleInvalidError("Typst font lock must be a JSON object")

    bundle_label = manifest.get("font_bundle_label")
    files = manifest.get("files")
    if not isinstance(bundle_label, str) or not bundle_label.strip():
        raise FontBundleInvalidError("Typst font lock requires a non-empty font_bundle_label")
    if not isinstance(files, list) or any(
        not isinstance(relative_path, str) or not relative_path.strip() for relative_path in files
    ):
        raise FontBundleInvalidError("Typst font lock files must be non-empty relative paths")
    if len(set(files)) != len(files):
        raise FontBundleInvalidError("Typst font lock contains duplicate file paths")

    return root, resolve_bundle_files(root, files, error=FontBundleInvalidError)


def load_font_bundle_snapshot(font_bundle_root: Path, lock_path: Path) -> FontBundleSnapshot:
    """Read, validate, and hash the exact lock-listed font bytes in one operation."""
    _, resolved_files = _load_font_bundle_files(font_bundle_root, lock_path)
    contents: list[tuple[str, bytes]] = []
    for relative_path, source_path in resolved_files:
        try:
            content = source_path.read_bytes()
        except OSError as exc:
            raise FontBundleInvalidError(
                f"Manifest-listed file cannot be read: {relative_path!r}"
            ) from exc
        contents.append((relative_path, content))
    files = tuple(contents)
    return FontBundleSnapshot(
        font_bundle_version=hash_bundle_contents(files),
        files=files,
    )


def font_bundle_search_paths(root: Path, relative_paths: tuple[str, ...]) -> tuple[Path, ...]:
    """Return minimal, sorted search roots for the given validated font paths."""
    directories = {root.joinpath(*PurePosixPath(path).parts).parent for path in relative_paths}
    search_roots = {
        directory
        for directory in directories
        if not any(other != directory and other in directory.parents for other in directories)
    }
    return tuple(
        sorted(
            search_roots,
            key=lambda path: (len(path.relative_to(root).parts), path.relative_to(root).as_posix()),
        )
    )


def materialize_font_bundle(
    snapshot: FontBundleSnapshot, destination_root: Path
) -> tuple[Path, ...]:
    """Write a snapshot to a private directory and return its Typst search roots."""
    for relative_path, content in snapshot.files:
        destination = destination_root.joinpath(*PurePosixPath(relative_path).parts)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)
    return font_bundle_search_paths(
        destination_root, tuple(relative_path for relative_path, _ in snapshot.files)
    )


def resolve_font_bundle_paths(font_bundle_root: Path, lock_path: Path) -> tuple[Path, ...]:
    """Return the sorted font search roots covering lock-listed files."""
    root, resolved_files = _load_font_bundle_files(font_bundle_root, lock_path)
    return font_bundle_search_paths(
        root, tuple(relative_path for relative_path, _ in resolved_files)
    )


def compute_font_bundle_version(font_bundle_root: Path, lock_path: Path) -> str:
    """Hash exactly the listed font files using the template bundle framing.

    Paths are sorted by their manifest strings. Each hash entry is framed as an
    8-byte big-endian UTF-8 path length, path bytes, an 8-byte big-endian file
    content length, and the file's exact bytes.
    """
    return load_font_bundle_snapshot(font_bundle_root, lock_path).font_bundle_version
