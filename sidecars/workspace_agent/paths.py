"""Dirfd-based path validation and resolution (design §5.4).

A client path is validated as pure data first, then resolved with ``openat``
walks that pin the workspace root and never follow a symbolic link. After every
open the fd's ``(st_dev, st_ino)`` is compared with the directory entry; one
mismatch is retried, a second one is a concurrent change. Path resolution
helpers (``Path.resolve``) are never used for authorization (TOCTOU).
"""

from __future__ import annotations

import os
import re
import stat
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn

from .protocol import ErrorCode

MAX_COMPONENT_BYTES = 255
MAX_PATH_BYTES = 1024

AGENT_INTERNAL_DIRECTORY = ".autowork"
ALLOWED_TOP_LEVEL_DIRECTORIES: frozenset[str] = frozenset(
    {
        "base_samples",
        "pcaps",
        "scripts",
        "regles",
        "discover_sample",
        "plans",
        "results",
        "notes",
        "vt",
    }
)
NOTES_CHILD_DIRECTORIES: frozenset[str] = frozenset({"humaines", "bridge", "agents"})
TASK_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")

_DIRECTORY_OPEN_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


class WorkspacePathError(Exception):
    """A rejected path, carrying the protocol code and status for the HTTP layer."""

    code: ErrorCode = ErrorCode.INVALID_PATH
    status: int = 400

    def __init__(self, message: str, *, details: Mapping[str, object] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details: dict[str, object] = {} if details is None else dict(details)


class PathValidationError(WorkspacePathError, ValueError):
    """A client path violates the pure-data rules of §5.4.1-5.4.2."""

    def __init__(self, reason: str) -> None:
        super().__init__("invalid path", details={"reason": reason})
        self.reason = reason


class PathResolutionError(WorkspacePathError):
    """A component is missing, not a directory, or a symbolic link."""

    def __init__(self, message: str = "path cannot be resolved safely") -> None:
        super().__init__(message)


class ConcurrentChangeError(WorkspacePathError):
    """The opened inode no longer matches the directory entry, twice in a row."""

    code = ErrorCode.CONCURRENT_CHANGE
    status = 409

    def __init__(self) -> None:
        super().__init__("the path changed during resolution")


@dataclass(frozen=True)
class WalkHooks:
    """Test seam around directory opens; production passes ``None``."""

    before_open: Callable[[int, str], None] | None = None
    after_open: Callable[[int, int, str], None] | None = None


def _reject(reason: str) -> NoReturn:
    raise PathValidationError(reason)


def validate_component(name: str) -> str:
    """Validate one path component (NFC, controls, separators, byte length)."""
    if not isinstance(name, str):
        _reject("encoding")
    try:
        encoded = name.encode("utf-8")
    except UnicodeEncodeError:
        _reject("encoding")
    if unicodedata.normalize("NFC", name) != name:
        _reject("nfc")
    if "\x00" in name:
        _reject("nul")
    if any(unicodedata.category(character) == "Cc" for character in name):
        _reject("control")
    if "\\" in name:
        _reject("backslash")
    if name in {"", ".", ".."}:
        _reject("empty_component" if name == "" else "dot_component")
    if len(encoded) > MAX_COMPONENT_BYTES:
        _reject("component_too_long")
    return name


def validate_relative_path(path: str) -> tuple[str, ...]:
    """Validate a client-supplied workspace-relative path; return its components."""
    if not isinstance(path, str):
        _reject("encoding")
    try:
        encoded = path.encode("utf-8")
    except UnicodeEncodeError:
        _reject("encoding")
    if unicodedata.normalize("NFC", path) != path:
        _reject("nfc")
    if "\x00" in path:
        _reject("nul")
    if any(unicodedata.category(character) == "Cc" for character in path):
        _reject("control")
    if "\\" in path:
        _reject("backslash")
    if path == "":
        _reject("empty")
    if path.startswith("/"):
        _reject("absolute")
    if len(encoded) > MAX_PATH_BYTES:
        _reject("too_long")

    components = tuple(path.split("/"))
    for component in components:
        validate_component(component)

    first = components[0]
    if first == AGENT_INTERNAL_DIRECTORY:
        _reject("agent_internal")
    if len(components) < 2:
        _reject("top_level")
    if first not in ALLOWED_TOP_LEVEL_DIRECTORIES:
        _reject("top_level")
    if first == "notes":
        if components[1] not in NOTES_CHILD_DIRECTORIES:
            _reject("notes_child")
        if components[1] == "agents" and (
            len(components) < 3 or TASK_ID_PATTERN.fullmatch(components[2]) is None
        ):
            _reject("task_id")
    return components


def open_pinned_root(root: Path) -> int:
    """Pin the workspace root directory, refusing a symbolic link."""
    try:
        fd = os.open(root, _DIRECTORY_OPEN_FLAGS)
    except OSError as exc:
        raise PathResolutionError("workspace root is unavailable") from exc
    try:
        if not stat.S_ISDIR(os.fstat(fd).st_mode):
            raise PathResolutionError("workspace root is not a directory")
    except BaseException:
        os.close(fd)
        raise
    return fd


def open_directory_chain(
    root_fd: int,
    components: Sequence[str],
    *,
    create: bool = False,
    mode: int = 0o700,
    hooks: WalkHooks | None = None,
) -> int:
    """Open each component as a directory below ``root_fd``; return the last fd.

    Intermediate directories are created only when ``create`` is true
    (``EEXIST`` is accepted and re-opened with ``O_NOFOLLOW``). The caller owns
    the returned fd and closes ``root_fd``.
    """
    if not components:
        raise ValueError("at least one path component is required")
    parent_fd = root_fd
    opened_fd: int | None = None
    try:
        for raw_name in components:
            name = validate_component(raw_name)
            created = _make_directory(parent_fd, name, create=create, mode=mode)
            fd = _open_directory_entry(parent_fd, name, hooks)
            if created:
                try:
                    os.fchmod(fd, mode)
                except OSError:
                    os.close(fd)
                    raise
            if opened_fd is not None:
                os.close(opened_fd)
            opened_fd = fd
            parent_fd = fd
    except BaseException:
        if opened_fd is not None:
            os.close(opened_fd)
        raise
    assert opened_fd is not None
    return opened_fd


def resolve_parent(
    root_fd: int, path: str, *, hooks: WalkHooks | None = None
) -> tuple[int, str, tuple[str, ...]]:
    """Validate ``path`` and open its parent directory below the pinned root."""
    components = validate_relative_path(path)
    parent_fd = open_directory_chain(root_fd, components[:-1], hooks=hooks)
    return parent_fd, components[-1], components


def _make_directory(parent_fd: int, name: str, *, create: bool, mode: int) -> bool:
    if not create:
        return False
    try:
        os.mkdir(name, mode, dir_fd=parent_fd)
    except FileExistsError:
        return False
    except OSError as exc:
        raise PathResolutionError("path component cannot be created") from exc
    return True


def _open_directory_entry(parent_fd: int, name: str, hooks: WalkHooks | None) -> int:
    for attempt in (0, 1):
        if hooks is not None and hooks.before_open is not None:
            hooks.before_open(parent_fd, name)
        try:
            fd = os.open(name, _DIRECTORY_OPEN_FLAGS, dir_fd=parent_fd)
        except OSError as exc:
            raise PathResolutionError("path component is not a directory") from exc
        try:
            if hooks is not None and hooks.after_open is not None:
                hooks.after_open(fd, parent_fd, name)
            try:
                entry = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            except OSError:
                entry = None
            if entry is not None and stat.S_ISDIR(entry.st_mode):
                opened = os.fstat(fd)
                if (entry.st_dev, entry.st_ino) == (opened.st_dev, opened.st_ino):
                    return fd
        except BaseException:
            os.close(fd)
            raise
        os.close(fd)
        if attempt == 1:
            raise ConcurrentChangeError()
    raise AssertionError("unreachable")
