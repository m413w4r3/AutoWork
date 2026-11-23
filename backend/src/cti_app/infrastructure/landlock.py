"""Unprivileged Landlock filesystem domain for confined agent scripts.

Landlock lets an unprivileged process restrict its own future filesystem
access.  The confined script supervisor installs this domain in the child
before ``exec`` so the script (and every descendant) can read only the
interpreter/runtime trees, its single canonical input and its scratch
directory, and can write only the scratch directory.  ``/proc`` and every
store or source tree outside those roots are denied, so a same-UID script
cannot read another process's environment through procfs.

``LANDLOCK_ACCESS_FS_EXECUTE`` is deliberately *not* handled: the kernel also
checks that right for ``mmap(PROT_EXEC)`` mappings of the ELF interpreter and
shared libraries, so handling it would require granting execute on whole
runtime trees (and would not keep a shell out of them anyway).  The worker
still builds the only interpreter argv; any binary the script execs remains
inside the same Landlock, seccomp and rlimit domain.

Only the closed standard library is used: this module runs inside the
per-execution supervisor, before any backend import is needed.
"""

from __future__ import annotations

import ctypes
import errno
import os
import platform
import sys
from collections.abc import Sequence
from pathlib import Path

_LANDLOCK_CREATE_RULESET = 444
_LANDLOCK_ADD_RULE = 445
_LANDLOCK_RESTRICT_SELF = 446
_CREATE_RULESET_VERSION = 1
_RULE_PATH_BENEATH = 1
_PR_SET_NO_NEW_PRIVS = 38

_ACCESS_WRITE_FILE = 1 << 1
_ACCESS_READ_FILE = 1 << 2
_ACCESS_READ_DIR = 1 << 3
_ACCESS_REMOVE_DIR = 1 << 4
_ACCESS_REMOVE_FILE = 1 << 5
_ACCESS_MAKE_CHAR = 1 << 6
_ACCESS_MAKE_DIR = 1 << 7
_ACCESS_MAKE_REG = 1 << 8
_ACCESS_MAKE_SOCK = 1 << 9
_ACCESS_MAKE_FIFO = 1 << 10
_ACCESS_MAKE_BLOCK = 1 << 11
_ACCESS_MAKE_SYM = 1 << 12
_ACCESS_REFER = 1 << 13
_ACCESS_TRUNCATE = 1 << 14
_BASE_ACCESS = (
    _ACCESS_WRITE_FILE
    | _ACCESS_READ_FILE
    | _ACCESS_READ_DIR
    | _ACCESS_REMOVE_DIR
    | _ACCESS_REMOVE_FILE
    | _ACCESS_MAKE_CHAR
    | _ACCESS_MAKE_DIR
    | _ACCESS_MAKE_REG
    | _ACCESS_MAKE_SOCK
    | _ACCESS_MAKE_FIFO
    | _ACCESS_MAKE_BLOCK
    | _ACCESS_MAKE_SYM
)

_LIBC = ctypes.CDLL(None, use_errno=True) if sys.platform.startswith("linux") else None
if _LIBC is not None:
    _LIBC.syscall.restype = ctypes.c_long


class LandlockUnavailable(RuntimeError):
    """Landlock cannot enforce the requested domain on this host/kernel."""


class _RulesetAttr(ctypes.Structure):
    _fields_ = [("handled_access_fs", ctypes.c_uint64)]


class _PathBeneathAttr(ctypes.Structure):
    _pack_ = 1
    _fields_ = [("allowed_access", ctypes.c_uint64), ("parent_fd", ctypes.c_int32)]


def _supported_architecture() -> bool:
    machine = platform.machine().lower()
    return machine in {"x86_64", "amd64", "aarch64", "arm64"}


def _syscall(*arguments: object) -> int:
    assert _LIBC is not None
    return int(_LIBC.syscall(*arguments))


def landlock_abi_version() -> int:
    """Return the kernel Landlock ABI version, or raise ``LandlockUnavailable``."""
    if not sys.platform.startswith("linux") or _LIBC is None:
        raise LandlockUnavailable("landlock_requires_linux")
    if not _supported_architecture():
        raise LandlockUnavailable("landlock_unsupported_architecture")
    version = _syscall(_LANDLOCK_CREATE_RULESET, None, 0, _CREATE_RULESET_VERSION)
    if version < 0:
        error = ctypes.get_errno()
        if error in {errno.ENOSYS, errno.EOPNOTSUPP, errno.EPERM, errno.EINVAL}:
            raise LandlockUnavailable("landlock_unavailable")
        raise LandlockUnavailable(f"landlock_unavailable_{os.strerror(error).replace(' ', '_')}")
    return version


def install_landlock_domain(
    *,
    read_directories: Sequence[Path | str],
    read_files: Sequence[Path | str],
    write_directories: Sequence[Path | str],
    write_files: Sequence[Path | str] = (),
) -> int:
    """Restrict the calling thread to the given filesystem hierarchy.

    Directories in ``write_directories`` receive every handled right;
    ``read_directories`` and ``read_files`` receive read rights only, and
    ``write_files`` (device nodes such as ``/dev/null``) receive read and
    write.  A path that does not exist is skipped.  Returns the ABI version.
    """
    abi = landlock_abi_version()
    handled = _BASE_ACCESS
    if abi >= 2:
        handled |= _ACCESS_REFER
    if abi >= 3:
        handled |= _ACCESS_TRUNCATE
    rules: dict[Path, int] = {}
    for directory in read_directories:
        path = Path(directory)
        if path.is_dir():
            _merge_rule(rules, path, _ACCESS_READ_FILE | _ACCESS_READ_DIR)
    for file_path in read_files:
        path = Path(file_path)
        if path.exists() and not path.is_dir():
            _merge_rule(rules, path, _ACCESS_READ_FILE)
    for file_path in write_files:
        path = Path(file_path)
        if path.exists() and not path.is_dir():
            _merge_rule(rules, path, _ACCESS_READ_FILE | _ACCESS_WRITE_FILE)
    for directory in write_directories:
        _merge_rule(rules, Path(directory), handled)
    assert _LIBC is not None
    ruleset_attr = _RulesetAttr(handled)
    ruleset_fd = _syscall(
        _LANDLOCK_CREATE_RULESET,
        ctypes.byref(ruleset_attr),
        ctypes.sizeof(ruleset_attr),
        0,
    )
    if ruleset_fd < 0:
        error = ctypes.get_errno()
        raise LandlockUnavailable(f"landlock_create_ruleset_failed_{error}")
    try:
        os.set_inheritable(ruleset_fd, False)
        for path, allowed_access in rules.items():
            _add_rule(ruleset_fd, path, allowed_access)
        if _LIBC.prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
            error = ctypes.get_errno()
            raise LandlockUnavailable(f"landlock_no_new_privs_failed_{error}")
        if _syscall(_LANDLOCK_RESTRICT_SELF, ruleset_fd, 0) != 0:
            error = ctypes.get_errno()
            raise LandlockUnavailable(f"landlock_restrict_self_failed_{error}")
    finally:
        os.close(ruleset_fd)
    return abi


def _merge_rule(rules: dict[Path, int], path: Path, allowed_access: int) -> None:
    resolved = Path(os.path.realpath(path))
    rules[resolved] = rules.get(resolved, 0) | allowed_access


def _add_rule(ruleset_fd: int, path: Path, allowed_access: int) -> None:
    assert _LIBC is not None
    try:
        path_fd = os.open(path, os.O_PATH | os.O_CLOEXEC)
    except OSError as exc:
        raise LandlockUnavailable(f"landlock_rule_open_failed_{exc.errno}") from exc
    try:
        rule = _PathBeneathAttr(allowed_access, path_fd)
        result = _syscall(
            _LANDLOCK_ADD_RULE,
            ruleset_fd,
            _RULE_PATH_BENEATH,
            ctypes.byref(rule),
            0,
        )
        if result != 0:
            error = ctypes.get_errno()
            raise LandlockUnavailable(f"landlock_add_rule_failed_{error}")
    finally:
        os.close(path_fd)


__all__ = [
    "LandlockUnavailable",
    "install_landlock_domain",
    "landlock_abi_version",
]
