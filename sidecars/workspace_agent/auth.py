"""Bearer-token lifecycle: rotation, hardened reads and constant-time comparison.

The token lives only in the runtime directory (``auth-token``, mode 0640), is
rotated on every start and is never written into the workspace or logged.
"""

from __future__ import annotations

import hmac
import os
import secrets
import stat
from pathlib import Path

TOKEN_FILE_NAME = "auth-token"
TOKEN_MODE = 0o640
MAX_TOKEN_FILE_BYTES = 128
_BEARER_SCHEME = "bearer"


class TokenFileError(RuntimeError):
    """The token file is missing, unsafe or not a regular file."""


def generate_token() -> str:
    """Return an opaque per-start token (43 URL-safe characters)."""
    return secrets.token_urlsafe(32)


def rotate_token_file(runtime_dir: Path, token: str, *, gid: int | None = None) -> Path:
    """Atomically replace ``runtime_dir/auth-token`` with ``token`` (mode 0640)."""
    if not token or not token.isascii() or token.strip() != token:
        raise ValueError("token must be non-empty printable ASCII without whitespace")
    path = runtime_dir / TOKEN_FILE_NAME
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise TokenFileError("cannot replace the existing token file") from exc
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
    try:
        fd = os.open(path, flags, TOKEN_MODE)
    except OSError as exc:
        raise TokenFileError("cannot create the token file") from exc
    try:
        os.fchmod(fd, TOKEN_MODE)
        if gid is not None:
            os.fchown(fd, -1, gid)
        remaining = memoryview(token.encode("ascii"))
        while remaining:
            written = os.write(fd, remaining)
            remaining = remaining[written:]
        os.fsync(fd)
    except OSError as exc:
        raise TokenFileError("cannot write the token file") from exc
    finally:
        os.close(fd)
    _fsync_directory(runtime_dir)
    return path


def read_token_file(path: Path) -> str:
    """Read a token without following symlinks; require a small regular file."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError as exc:
        raise TokenFileError("cannot open the token file") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise TokenFileError("token file is not a regular file")
        if not 0 < info.st_size <= MAX_TOKEN_FILE_BYTES:
            raise TokenFileError("token file has an unexpected size")
        raw = os.read(fd, MAX_TOKEN_FILE_BYTES + 1)
    finally:
        os.close(fd)
    try:
        token = raw.decode("ascii")
    except UnicodeDecodeError as exc:
        raise TokenFileError("token file is not ASCII") from exc
    if token.strip() != token or not token:
        raise TokenFileError("token file contains unexpected whitespace")
    return token


def verify_bearer(authorization: str | None, expected_token: str) -> bool:
    """Constant-time check of ``Authorization: Bearer <token>`` (scheme case-insensitive)."""
    if not authorization:
        return False
    scheme, separator, presented = authorization.partition(" ")
    if not separator or scheme.casefold() != _BEARER_SCHEME or not presented:
        return False
    return hmac.compare_digest(
        presented.encode("utf-8", "surrogatepass"),
        expected_token.encode("utf-8", "surrogatepass"),
    )


def _fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
