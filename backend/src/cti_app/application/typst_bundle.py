"""Shared file-bundle resolution and hashing for Typst template/font manifests.

Both the chpTypst renderer manifest (AW-019.3) and the Typst font lock
(AW-019.4) are "exactly these files, nothing else" bundles that need the same
two properties: no path may escape the bundle root, and the resulting hash
must be stable across machines. This module holds that one algorithm so the
two call sites can never drift apart.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from pathlib import Path, PurePosixPath, PureWindowsPath


def resolve_bundle_files(
    root: Path, files: Sequence[str], *, error: type[Exception]
) -> tuple[tuple[str, Path], ...]:
    """Validate and resolve each manifest-relative path against `root`.

    Returns `(relative_path, absolute_path)` pairs sorted by `relative_path`.
    Raises `error` for an absolute path, a `..`/backslash escape, or a
    manifest entry that doesn't resolve to a file inside `root`.
    """
    resolved_files: list[tuple[str, Path]] = []
    for relative_path in sorted(files):
        posix_path = PurePosixPath(relative_path)
        windows_path = PureWindowsPath(relative_path)
        if (
            posix_path.is_absolute()
            or windows_path.is_absolute()
            or windows_path.drive
            or "\\" in relative_path
        ):
            raise error(f"Manifest path must stay inside the bundle: {relative_path!r}")
        candidate = root.joinpath(*posix_path.parts)
        try:
            resolved = candidate.resolve()
            resolved.relative_to(root)
        except (OSError, RuntimeError, ValueError) as exc:
            raise error(f"Manifest path escapes the bundle: {relative_path!r}") from exc
        if not resolved.is_file():
            raise error(f"Manifest-listed file is missing: {relative_path!r}")
        resolved_files.append((relative_path, resolved))
    return tuple(resolved_files)


def hash_bundle_files(resolved_files: Sequence[tuple[str, Path]], *, error: type[Exception]) -> str:
    """Hash resolved `(relative_path, absolute_path)` pairs deterministically.

    Each entry is framed as an 8-byte big-endian UTF-8 path length, the path
    bytes, an 8-byte big-endian file content length, and the file's exact
    bytes. Length-prefixing both fields makes path/content and adjacent-entry
    boundaries unambiguous. Callers must pass `resolved_files` already sorted
    by relative path (as returned by `resolve_bundle_files`).
    """
    contents: list[tuple[str, bytes]] = []
    for relative_path, absolute_path in resolved_files:
        try:
            file_bytes = absolute_path.read_bytes()
        except OSError as exc:
            raise error(f"Manifest-listed file cannot be read: {relative_path!r}") from exc
        contents.append((relative_path, file_bytes))
    return hash_bundle_contents(contents)


def hash_bundle_contents(files: Sequence[tuple[str, bytes]]) -> str:
    """Hash in-memory `(relative_path, content)` pairs with the bundle framing."""
    digest = hashlib.sha256()
    for relative_path, file_bytes in sorted(files, key=lambda item: item[0]):
        path_bytes = relative_path.encode("utf-8")
        digest.update(len(path_bytes).to_bytes(8, "big"))
        digest.update(path_bytes)
        digest.update(len(file_bytes).to_bytes(8, "big"))
        digest.update(file_bytes)
    return digest.hexdigest()
