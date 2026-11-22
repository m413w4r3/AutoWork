"""``base_samples`` materialization and read-only access (design §8).

Samples arrive as raw bytes on the backend-initiated HTTP request; the agent
never fetches from the network and never executes a sample. Publication is
content-addressed:

* the body streams into ``.autowork/incoming/<uuid>.part`` (0600, exclusive);
* the SHA-256 is computed while streaming and must equal the path digest;
* the temporary is fsynced, chmod 0440 (an execute bit is never granted) and
  renamed to ``base_samples/<sha256>`` (no extension);
* the published entry is reopened ``O_NOFOLLOW`` and re-hashed; a mismatch
  unlinks it and fails with ``hash_mismatch``.

An existing ``base_samples/<sha256>`` is verified and never overwritten: equal
content returns ``already_present``; different content, a symbolic link or any
other non-regular entry is reported as a conflict. Every filesystem access uses
the dirfd helpers in :mod:`paths`; links are never followed and paths are never
resolved.
"""

from __future__ import annotations

import fcntl
import hashlib
import os
import re
import stat
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from . import paths, protocol, workspace
from .protocol import ErrorCode

SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
BASE_SAMPLES_DIRECTORY = "base_samples"
INCOMING_DIRECTORY = "incoming"
SAMPLE_FILE_MODE = 0o440
INCOMING_FILE_MODE = 0o600
PRIVATE_DIRECTORY_MODE = 0o700
STREAM_CHUNK_SIZE = 1024 * 1024
SAMPLE_RESPONSE_MODE = "0440"


class ByteSource(Protocol):
    """Minimal stream surface used by materialization (``rfile`` satisfies it)."""

    def read(self, size: int, /) -> bytes: ...


class SampleError(workspace.WorkspaceError):
    """A base-sample operation rejected with a protocol code and status."""


class InvalidSampleRequestError(SampleError):
    """The digest is malformed or the streamed body ended early."""

    code = ErrorCode.INVALID_REQUEST
    status = 400

    def __init__(self, reason: str) -> None:
        super().__init__("invalid base sample request", details={"reason": reason})


class SampleSizeLimitError(SampleError):
    """The declared body length exceeds the sample limit."""

    code = ErrorCode.SIZE_LIMIT
    status = 413

    def __init__(self) -> None:
        super().__init__(
            "base sample exceeds the size limit",
            details={"limit": protocol.SAMPLE_LIMIT},
        )


class SampleHashMismatchError(SampleError):
    """Streamed, published or pre-existing content does not match the digest."""

    code = ErrorCode.HASH_MISMATCH
    status = 422

    def __init__(self, reason: str, **details: str) -> None:
        super().__init__(
            "sample content does not match the requested sha256",
            details={"reason": reason, **details},
        )


class SampleConflictError(SampleError):
    """The destination entry is a symlink, a non-regular file or changed under us."""

    code = ErrorCode.CONCURRENT_CHANGE
    status = 409

    def __init__(self, reason: str) -> None:
        super().__init__("base sample destination is not trusted", details={"reason": reason})


class SampleNotFoundError(SampleError):
    """No materialized sample carries the requested digest."""

    code = ErrorCode.INVALID_REQUEST
    status = 404

    def __init__(self, sha256: str) -> None:
        super().__init__("base sample not found", details={"sha256": sha256})


class SampleStorageError(SampleError):
    """Local storage failed a write, rename or verification."""

    code = ErrorCode.INTERNAL_ERROR
    status = 500


@dataclass(frozen=True)
class SampleMaterialization:
    """``SampleMaterializationV1`` returned by a successful upload."""

    sha256: str
    path: str
    size: int
    mode: str
    already_present: bool

    def as_payload(self) -> dict[str, object]:
        return {
            "sha256": self.sha256,
            "path": self.path,
            "size": self.size,
            "mode": self.mode,
            "already_present": self.already_present,
        }


@dataclass(frozen=True)
class OpenSample:
    """An opened, verified regular file; the caller owns and closes ``fd``."""

    fd: int
    size: int


@dataclass(frozen=True)
class _Written:
    dev: int
    ino: int
    size: int


def materialize_sample(
    *,
    root: Path,
    descriptor: workspace.WorkspaceDescriptor,
    sha256: str,
    source: ByteSource,
    declared_length: int,
    analysis_gid: int | None = None,
) -> SampleMaterialization:
    """Stream ``declared_length`` bytes into ``base_samples/<sha256>``.

    ``analysis_gid`` is applied with ``chgrp`` only when configured and
    permitted; an unprivileged owner simply keeps its own group.
    """
    _require_sha256(sha256)
    if declared_length < 0:
        raise InvalidSampleRequestError("content_length")
    if declared_length > protocol.SAMPLE_LIMIT:
        raise SampleSizeLimitError()
    workspace_fd = _open_workspace_directory(root, descriptor)
    try:
        base_fd = _open_base_samples_directory(workspace_fd)
        try:
            incoming_fd = _open_incoming_directory(workspace_fd)
            try:
                return _stream_and_publish(
                    base_fd,
                    incoming_fd,
                    sha256=sha256,
                    source=source,
                    declared_length=declared_length,
                    analysis_gid=analysis_gid,
                )
            finally:
                os.close(incoming_fd)
        finally:
            os.close(base_fd)
    finally:
        os.close(workspace_fd)


def open_sample(
    *, root: Path, descriptor: workspace.WorkspaceDescriptor, sha256: str
) -> OpenSample:
    """Open one materialized sample read-only; the caller closes ``fd``."""
    _require_sha256(sha256)
    workspace_fd = _open_workspace_directory(root, descriptor)
    try:
        try:
            base_fd = paths.open_directory_chain(
                workspace_fd, (BASE_SAMPLES_DIRECTORY,), create=False
            )
        except paths.ConcurrentChangeError:
            raise
        except paths.WorkspacePathError as exc:
            raise SampleNotFoundError(sha256) from exc
        try:
            try:
                fd = os.open(sha256, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=base_fd)
            except FileNotFoundError as exc:
                raise SampleNotFoundError(sha256) from exc
            except OSError as exc:
                raise SampleConflictError("unreadable") from exc
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode):
                    raise SampleConflictError("not_regular")
                return OpenSample(fd=fd, size=info.st_size)
            except BaseException:
                os.close(fd)
                raise
        finally:
            os.close(base_fd)
    finally:
        os.close(workspace_fd)


def _stream_and_publish(
    base_fd: int,
    incoming_fd: int,
    *,
    sha256: str,
    source: ByteSource,
    declared_length: int,
    analysis_gid: int | None,
) -> SampleMaterialization:
    part_name = f"{uuid.uuid4().hex}.part"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
    try:
        part_fd = os.open(part_name, flags, INCOMING_FILE_MODE, dir_fd=incoming_fd)
    except OSError as exc:
        raise SampleStorageError("sample temporary file cannot be created") from exc
    published = False
    try:
        try:
            os.fchmod(part_fd, INCOMING_FILE_MODE)
            digest = _stream_to_fd(part_fd, source, declared_length)
            if digest != sha256:
                raise SampleHashMismatchError("content_mismatch", received_sha256=digest)
            os.fchmod(part_fd, SAMPLE_FILE_MODE)
            _chgrp_if_permitted(part_fd, analysis_gid)
            os.fsync(part_fd)
            info = os.fstat(part_fd)
            written = _Written(dev=info.st_dev, ino=info.st_ino, size=info.st_size)
        except OSError as exc:
            raise SampleStorageError("sample cannot be written") from exc
        finally:
            os.close(part_fd)
        result, published = _publish(base_fd, incoming_fd, part_name, written, sha256)
        return result
    finally:
        if not published:
            _unlink_quietly(incoming_fd, part_name)


def _publish(
    base_fd: int,
    incoming_fd: int,
    part_name: str,
    written: _Written,
    sha256: str,
) -> tuple[SampleMaterialization, bool]:
    """Publish the temporary atomically, or verify an existing entry."""
    try:
        fcntl.flock(base_fd, fcntl.LOCK_EX)
    except OSError as exc:
        raise SampleStorageError("base_samples lock cannot be acquired") from exc
    try:
        existing = _lstat_entry(base_fd, sha256)
        if existing is not None:
            size = _verify_existing(base_fd, existing, sha256)
            return _materialization(sha256, size, already_present=True), False
        try:
            os.rename(part_name, sha256, src_dir_fd=incoming_fd, dst_dir_fd=base_fd)
        except OSError as exc:
            raise SampleStorageError("sample cannot be published") from exc
        _fsync_quietly(base_fd)
        _fsync_quietly(incoming_fd)
        try:
            final = os.stat(sha256, dir_fd=base_fd, follow_symlinks=False)
        except OSError as exc:
            raise SampleStorageError("sample cannot be verified") from exc
        if not stat.S_ISREG(final.st_mode):
            raise SampleConflictError("replaced")
        if (final.st_dev, final.st_ino) != (written.dev, written.ino):
            # Another writer replaced our entry: never delete what is not ours.
            raise SampleConflictError("replaced")
        if final.st_size != written.size:
            # Same inode but a different length: the entry was modified in place.
            raise SampleConflictError("replaced")
        _verify_content(base_fd, sha256, sha256, unlink_on_mismatch=True)
        return _materialization(sha256, final.st_size, already_present=False), True
    finally:
        fcntl.flock(base_fd, fcntl.LOCK_UN)


def _verify_existing(base_fd: int, existing: os.stat_result, sha256: str) -> int:
    if stat.S_ISLNK(existing.st_mode):
        raise SampleConflictError("symlink")
    if not stat.S_ISREG(existing.st_mode):
        raise SampleConflictError("not_regular")
    _verify_content(base_fd, sha256, sha256, unlink_on_mismatch=False)
    return existing.st_size


def _verify_content(
    base_fd: int, name: str, expected_sha256: str, *, unlink_on_mismatch: bool
) -> None:
    """Reopen ``name`` without following links and hash it completely."""
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=base_fd)
    except OSError as exc:
        raise SampleConflictError("unreadable") from exc
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise SampleConflictError("not_regular")
        digest = _hash_fd(fd)
    finally:
        os.close(fd)
    if digest != expected_sha256:
        if unlink_on_mismatch:
            _unlink_quietly(base_fd, name)
            _fsync_quietly(base_fd)
        raise SampleHashMismatchError(
            "post_publication_mismatch" if unlink_on_mismatch else "existing_content_mismatch",
            existing_sha256=digest,
        )


def _stream_to_fd(fd: int, source: ByteSource, declared_length: int) -> str:
    """Copy exactly ``declared_length`` bytes, hashing them on the way."""
    digest = hashlib.sha256()
    remaining = declared_length
    while remaining > 0:
        try:
            chunk = source.read(min(STREAM_CHUNK_SIZE, remaining))
        except OSError as exc:
            raise InvalidSampleRequestError("truncated_body") from exc
        if not chunk:
            raise InvalidSampleRequestError("truncated_body")
        digest.update(chunk)
        _write_all(fd, chunk)
        remaining -= len(chunk)
    return digest.hexdigest()


def _hash_fd(fd: int) -> str:
    digest = hashlib.sha256()
    os.lseek(fd, 0, os.SEEK_SET)
    while True:
        chunk = os.read(fd, STREAM_CHUNK_SIZE)
        if not chunk:
            return digest.hexdigest()
        digest.update(chunk)


def _write_all(fd: int, payload: bytes) -> None:
    remaining = memoryview(payload)
    while remaining:
        written = os.write(fd, remaining)
        remaining = remaining[written:]


def _open_workspace_directory(root: Path, descriptor: workspace.WorkspaceDescriptor) -> int:
    try:
        root_fd = paths.open_pinned_root(root)
    except paths.PathResolutionError as exc:
        raise workspace.WorkspaceNotFoundError(descriptor.workspace_id) from exc
    try:
        components = (
            _manifest_component(descriptor, "delivery_code"),
            _manifest_component(descriptor, "article_slug"),
        )
        return paths.open_directory_chain(root_fd, components, create=False)
    except paths.ConcurrentChangeError:
        raise
    except paths.WorkspacePathError as exc:
        raise workspace.WorkspaceNotFoundError(descriptor.workspace_id) from exc
    finally:
        os.close(root_fd)


def _open_base_samples_directory(workspace_fd: int) -> int:
    try:
        return paths.open_directory_chain(
            workspace_fd,
            (BASE_SAMPLES_DIRECTORY,),
            create=True,
            mode=workspace.BASE_SAMPLES_DIRECTORY_MODE,
        )
    except paths.ConcurrentChangeError:
        raise
    except paths.WorkspacePathError as exc:
        raise SampleStorageError("base_samples directory cannot be opened") from exc


def _open_incoming_directory(workspace_fd: int) -> int:
    try:
        metadata_fd = paths.open_directory_chain(
            workspace_fd, (paths.AGENT_INTERNAL_DIRECTORY,), create=False
        )
    except paths.ConcurrentChangeError:
        raise
    except paths.WorkspacePathError as exc:
        raise SampleStorageError("incoming directory cannot be opened") from exc
    try:
        try:
            return paths.open_directory_chain(
                metadata_fd,
                (INCOMING_DIRECTORY,),
                create=True,
                mode=PRIVATE_DIRECTORY_MODE,
            )
        except paths.ConcurrentChangeError:
            raise
        except paths.WorkspacePathError as exc:
            raise SampleStorageError("incoming directory cannot be opened") from exc
    finally:
        os.close(metadata_fd)


def _lstat_entry(parent_fd: int, name: str) -> os.stat_result | None:
    try:
        return os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise SampleStorageError("base_samples entry cannot be inspected") from exc


def _chgrp_if_permitted(fd: int, gid: int | None) -> None:
    if gid is None:
        return
    try:
        os.fchown(fd, -1, gid)
    except OSError:
        # Not permitted (or not a valid gid): the file keeps its owner group.
        return


def _unlink_quietly(parent_fd: int, name: str) -> None:
    try:
        os.unlink(name, dir_fd=parent_fd)
    except FileNotFoundError:
        return
    except OSError:
        return


def _fsync_quietly(fd: int) -> None:
    try:
        os.fsync(fd)
    except OSError:
        pass


def _materialization(sha256: str, size: int, *, already_present: bool) -> SampleMaterialization:
    return SampleMaterialization(
        sha256=sha256,
        path=f"{BASE_SAMPLES_DIRECTORY}/{sha256}",
        size=size,
        mode=SAMPLE_RESPONSE_MODE,
        already_present=already_present,
    )


def _require_sha256(sha256: str) -> None:
    if not isinstance(sha256, str) or SHA256_PATTERN.fullmatch(sha256) is None:
        raise InvalidSampleRequestError("sha256")


def _manifest_component(descriptor: workspace.WorkspaceDescriptor, field: str) -> str:
    value = descriptor.manifest.get(field)
    if not isinstance(value, str):
        raise SampleStorageError("workspace manifest is not usable")
    return value
