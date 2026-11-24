"""File reads, CAS-gated writes, generated publication and conflicts (design §4, §5.4-§7).

Every read and write happens through dirfds pinned to the workspace; links are
never followed and paths are never resolved. A write streams into
``.{name}.{uuid}.tmp`` in the destination directory while hashing, then decides
under the per-workspace write transaction:

* the body already equals the live content -> ``already_present`` (no write);
* the expected SHA does not match the live content -> the body is atomically
  published as ``<stem>.conflict-<sha8><suffix>`` (16 or 64 hex if a shorter
  name already holds other bytes), the live file is untouched and the response
  is 409 ``revision_conflict``;
* otherwise the live file is replaced atomically and the revision, event and
  projections are recorded before the response.

``Idempotency-Key`` replays return the stored status and body. The agent never
deletes or overwrites a file it was not CAS-authorized to replace.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from . import events, paths, policies, protocol, scan, workspace
from .protocol import ErrorCode

STREAM_CHUNK_SIZE = 1024 * 1024
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
TEMP_FILE_SUFFIX = ".tmp"
TEMP_FILE_MODE = 0o600
CONFLICT_INFIX = ".conflict-"
CONFLICT_HEX_LENGTHS: tuple[int, ...] = (8, 16, 64)
CONFLICT_ID_PREFIX = "cfl-"
MAX_COMPONENT_BYTES = 255
MAX_GENERATED_BY_BYTES = 128

EXPECTED_SHA256_HEADER = "X-Autowork-Expected-Sha256"
ORIGIN_HEADER = "X-Autowork-Origin"
TASK_ID_HEADER = "X-Autowork-Task-Id"
GENERATED_BY_HEADER = "X-Autowork-Generated-By"
SOURCE_SHA256_HEADER = "X-Autowork-Source-Sha256"

EDITABLE_ORIGINS: frozenset[str] = frozenset({"ui", "agent"})
GENERATED_ORIGINS: frozenset[str] = frozenset({"system"})


class ByteSource(Protocol):
    """Minimal stream surface used by writes (``rfile`` satisfies it)."""

    def read(self, size: int, /) -> bytes: ...


class FileError(workspace.WorkspaceError):
    """A file operation rejected with a protocol code and status."""


class FileRequestError(FileError):
    """A write header or streamed body violates the contract."""

    code = ErrorCode.INVALID_REQUEST
    status = 400

    def __init__(self, field: str, reason: str) -> None:
        super().__init__("invalid file request", details={"field": field, "reason": reason})


class WorkspaceFileNotFoundError(FileError):
    """No regular file carries the requested path."""

    code = ErrorCode.INVALID_REQUEST
    status = 404

    def __init__(self, path: str) -> None:
        super().__init__("file not found", details={"path": path})


class FileSizeLimitError(FileError):
    """The declared or actual size exceeds the route limit."""

    code = ErrorCode.SIZE_LIMIT
    status = 413

    def __init__(self, limit: int) -> None:
        super().__init__("file exceeds the size limit", details={"limit": limit})


class FileStorageError(FileError):
    """Local storage failed a read, write, rename or verification."""

    code = ErrorCode.INTERNAL_ERROR
    status = 500


@dataclass(frozen=True)
class ContentResponse:
    """One bounded content read, already verified against size/mtime drift."""

    body: bytes
    sha256: str
    size: int
    mtime_ns: int


@dataclass(frozen=True)
class WriteOutcome:
    """A ``WriteResultV1`` body (200/201) or a stored/fresh conflict envelope (409)."""

    status: int
    body: dict[str, object]


@dataclass(frozen=True)
class _Current:
    sha256: str
    size: int
    mtime_ns: int
    dev: int
    ino: int


@dataclass(frozen=True)
class _Written:
    dev: int
    ino: int
    size: int


@dataclass
class _Temporary:
    fd: int
    name: str
    written: _Written
    digest: str
    closed: bool = field(default=False)

    def seal(self, mode: int) -> None:
        """Apply the final mode, fsync and close the temporary exactly once."""
        if self.closed:
            return
        try:
            os.fchmod(self.fd, mode)
            os.fsync(self.fd)
        finally:
            os.close(self.fd)
            self.closed = True


@dataclass(frozen=True)
class _ConflictCopy:
    path: str
    conflict_id: str


def read_content(
    *, root: Path, descriptor: workspace.WorkspaceDescriptor, path: str
) -> ContentResponse:
    """Return the raw bytes of one regular file, bounded to 32 MiB."""
    paths.validate_relative_path(path)
    workspace_fd = _open_workspace_directory(root, descriptor)
    try:
        parent_fd, name, _ = paths.resolve_parent(workspace_fd, path)
        try:
            return _read_entry(parent_fd, name, path)
        finally:
            os.close(parent_fd)
    finally:
        os.close(workspace_fd)


def read_revisions(
    *,
    scans: scan.WorkspaceScanService,
    workspace_id: str,
    path: str,
    limit: int = scan.MAX_KNOWN_REVISIONS,
) -> tuple[scan.RevisionRecord, ...]:
    """Bounded, index-known revision history of one path."""
    paths.validate_relative_path(path)
    return scans.known_revisions(workspace_id, path, limit=limit)


def write_file(
    *,
    root: Path,
    descriptor: workspace.WorkspaceDescriptor,
    scans: scan.WorkspaceScanService,
    route: policies.WriteRoute,
    path: str,
    expected_sha256: str | None,
    origin: str | None,
    task_id: str | None,
    generated_by: str | None,
    source_sha256: str | None,
    source: ByteSource,
    declared_length: int,
    idempotency_key: str,
    agent_content_check: Callable[[str, str, Callable[[], bytes]], None] | None = None,
    hooks: paths.WalkHooks | None = None,
) -> WriteOutcome:
    """Publish one API write under its route policy and CAS preconditions.

    ``hooks`` is the deterministic §5.4 FS seam used by the race tests to swap a
    path component between the directory open and the inode re-check; production
    callers pass ``None``.
    """
    components = paths.validate_relative_path(path)
    policies.require_route_access(route, path)
    validated_origin = _require_origin(route, origin)
    expected = _parse_expected_sha256(expected_sha256)
    if task_id is not None and paths.TASK_ID_PATTERN.fullmatch(task_id) is None:
        raise FileRequestError("task_id", "invalid")
    policies.require_task_binding(path, task_id)
    if route is policies.WriteRoute.GENERATED:
        _require_generated_by(generated_by)
        _require_source_sha256(source_sha256)
    limit = policies.write_limit(route)
    if declared_length < 0:
        raise FileRequestError("content_length", "invalid")
    if declared_length > limit:
        raise FileSizeLimitError(limit)

    workspace_fd = _open_workspace_directory(root, descriptor)
    try:
        # Agent note capture uses one private subtree per task. Unlike the
        # static workspace layout, that subtree is created on demand; mirror
        # the session collector's safe dirfd creation so a fresh task can use
        # the same authorized file route without pre-seeding directories.
        create_agent_note_parent = (
            route is policies.WriteRoute.FILES
            and validated_origin == "agent"
            and policies.agent_task_id_for(path) is not None
        )
        parent_fd = _open_parent(
            workspace_fd,
            components,
            hooks=hooks,
            create=create_agent_note_parent,
            mode=workspace.PRIVATE_DIRECTORY_MODE,
        )
        try:
            temporary = _stream_to_temporary(parent_fd, components[-1], source, declared_length)
            try:
                if (
                    validated_origin == "agent"
                    and task_id is not None
                    and agent_content_check is not None
                ):
                    cached_content: bytes | None = None

                    def read_temporary() -> bytes:
                        nonlocal cached_content
                        if cached_content is None:
                            cached_content = _read_temporary_bytes(parent_fd, temporary)
                        return cached_content

                    agent_content_check(task_id, path, read_temporary)
                return _commit_write(
                    descriptor=descriptor,
                    scans=scans,
                    route=route,
                    path=path,
                    parent_fd=parent_fd,
                    name=components[-1],
                    expected=expected,
                    origin=validated_origin,
                    task_id=task_id,
                    temporary=temporary,
                    idempotency_key=idempotency_key,
                )
            finally:
                if not temporary.closed:
                    try:
                        os.close(temporary.fd)
                    except OSError:
                        pass
                _unlink_quietly(parent_fd, temporary.name)
        finally:
            os.close(parent_fd)
    finally:
        os.close(workspace_fd)


def _commit_write(
    *,
    descriptor: workspace.WorkspaceDescriptor,
    scans: scan.WorkspaceScanService,
    route: policies.WriteRoute,
    path: str,
    parent_fd: int,
    name: str,
    expected: str | None,
    origin: str,
    task_id: str | None,
    temporary: _Temporary,
    idempotency_key: str,
) -> WriteOutcome:
    workspace_id = descriptor.workspace_id
    with scans.write_transaction(workspace_id) as transaction:
        stored = transaction.replay(idempotency_key, now_ns=transaction.wall_ns())
        if stored is not None:
            status, body = stored
            return WriteOutcome(status=status, body=body)

        current = _inspect_current(parent_fd, name)
        if current is not None and current.sha256 == temporary.digest:
            revision = _already_present_revision(
                transaction,
                workspace_id=workspace_id,
                path=path,
                current=current,
                origin=origin,
                task_id=task_id,
            )
            body = _write_result(revision, created=False, already_present=True)
            transaction.remember(idempotency_key, 200, body, now_ns=transaction.wall_ns())
            transaction.flush()
            return WriteOutcome(status=200, body=body)

        expected_matches = (expected is None and current is None) or (
            expected is not None and current is not None and current.sha256 == expected
        )
        observed_at = transaction.observed_at()
        if not expected_matches:
            copy = _publish_conflict_copy(
                parent_fd,
                parent_path=_parent_relative(path),
                name=name,
                temporary=temporary,
                workspace_id=workspace_id,
                path=path,
            )
            current_sha256 = current.sha256 if current is not None else None
            transaction.record_conflict(
                path=path,
                proposed_sha256=temporary.digest,
                proposed_size=temporary.written.size,
                current_sha256=current_sha256,
                conflict_id=copy.conflict_id,
                conflict_path=copy.path,
                origin=origin,
                task_id=task_id,
                observed_at=observed_at,
            )
            body = protocol.error_envelope(
                ErrorCode.REVISION_CONFLICT,
                "the file changed since the expected revision",
                details={
                    "current": current_sha256,
                    "proposed_sha256": temporary.digest,
                    "conflict_path": copy.path,
                    "conflict_id": copy.conflict_id,
                },
            )
            transaction.remember(idempotency_key, 409, body, now_ns=transaction.wall_ns())
            transaction.flush()
            return WriteOutcome(status=409, body=body)

        final = _publish_live(parent_fd, name, temporary, mode=policies.final_mode(route))
        previous_revision_id = (
            events.revision_id_for(workspace_id, path, current.sha256)
            if current is not None
            else None
        )
        created = current is None
        revision = transaction.record(
            path=path,
            sha256=temporary.digest,
            size=temporary.written.size,
            mtime_ns=final.st_mtime_ns,
            mode=policies.final_mode(route),
            dev=final.st_dev,
            ino=final.st_ino,
            origin=origin,
            task_id=task_id,
            previous_revision_id=previous_revision_id,
            created=created,
            observed_at=observed_at,
        )
        status = 201 if created else 200
        body = _write_result(revision, created=created, already_present=False)
        transaction.remember(idempotency_key, status, body, now_ns=transaction.wall_ns())
        transaction.flush()
        return WriteOutcome(status=status, body=body)


def _read_entry(parent_fd: int, name: str, path: str) -> ContentResponse:
    for _ in range(2):
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent_fd)
        except FileNotFoundError as exc:
            raise WorkspaceFileNotFoundError(path) from exc
        except PermissionError as exc:
            raise FileStorageError("file cannot be read") from exc
        except OSError as exc:
            raise paths.ConcurrentChangeError() from exc
        try:
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode):
                raise WorkspaceFileNotFoundError(path)
            if before.st_size > protocol.CONTENT_READ_LIMIT:
                raise FileSizeLimitError(protocol.CONTENT_READ_LIMIT)
            body = _read_bounded(fd, protocol.CONTENT_READ_LIMIT)
            after = os.fstat(fd)
        finally:
            os.close(fd)
        if len(body) > protocol.CONTENT_READ_LIMIT:
            raise FileSizeLimitError(protocol.CONTENT_READ_LIMIT)
        try:
            entry = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except OSError:
            continue
        stable = (after.st_size, after.st_mtime_ns) == (before.st_size, before.st_mtime_ns)
        same_inode = (entry.st_dev, entry.st_ino) == (before.st_dev, before.st_ino)
        if stable and same_inode and len(body) == before.st_size:
            return ContentResponse(
                body=body,
                sha256=hashlib.sha256(body).hexdigest(),
                size=before.st_size,
                mtime_ns=before.st_mtime_ns,
            )
    raise paths.ConcurrentChangeError()


def _inspect_current(parent_fd: int, name: str) -> _Current | None:
    for _ in range(2):
        try:
            entry = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise FileStorageError("file cannot be inspected") from exc
        if stat.S_ISLNK(entry.st_mode) or not stat.S_ISREG(entry.st_mode):
            raise paths.ConcurrentChangeError()
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent_fd)
        except FileNotFoundError:
            return None
        except PermissionError as exc:
            raise FileStorageError("file cannot be inspected") from exc
        except OSError as exc:
            raise paths.ConcurrentChangeError() from exc
        try:
            before = os.fstat(fd)
            if (before.st_dev, before.st_ino) != (entry.st_dev, entry.st_ino):
                continue
            if not stat.S_ISREG(before.st_mode):
                raise paths.ConcurrentChangeError()
            digest = _hash_fd(fd)
            after = os.fstat(fd)
        finally:
            os.close(fd)
        if (after.st_size, after.st_mtime_ns) == (before.st_size, before.st_mtime_ns):
            return _Current(
                sha256=digest,
                size=before.st_size,
                mtime_ns=before.st_mtime_ns,
                dev=before.st_dev,
                ino=before.st_ino,
            )
    raise paths.ConcurrentChangeError()


def _stream_to_temporary(
    parent_fd: int, name: str, source: ByteSource, declared_length: int
) -> _Temporary:
    temporary_name = _temporary_name(name)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
    try:
        fd = os.open(temporary_name, flags, TEMP_FILE_MODE, dir_fd=parent_fd)
    except OSError as exc:
        raise FileStorageError("temporary file cannot be created") from exc
    try:
        digest = hashlib.sha256()
        remaining = declared_length
        while remaining > 0:
            try:
                chunk = source.read(min(STREAM_CHUNK_SIZE, remaining))
            except OSError as exc:
                raise FileRequestError("body", "unreadable") from exc
            if not chunk:
                raise FileRequestError("body", "truncated")
            digest.update(chunk)
            _write_all(fd, chunk)
            remaining -= len(chunk)
        info = os.fstat(fd)
        written = _Written(dev=info.st_dev, ino=info.st_ino, size=info.st_size)
        return _Temporary(fd=fd, name=temporary_name, written=written, digest=digest.hexdigest())
    except OSError as exc:
        _discard_temporary(parent_fd, fd, temporary_name)
        raise FileStorageError("file cannot be written") from exc
    except BaseException:
        _discard_temporary(parent_fd, fd, temporary_name)
        raise


def _read_temporary_bytes(parent_fd: int, temporary: _Temporary) -> bytes:
    """Read staged agent content only when an active session needs checking."""
    try:
        fd = os.open(
            temporary.name,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=parent_fd,
        )
    except OSError as exc:
        raise FileStorageError("temporary file cannot be verified") from exc
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or (info.st_dev, info.st_ino) != (temporary.written.dev, temporary.written.ino)
            or info.st_size != temporary.written.size
        ):
            raise FileStorageError("temporary file cannot be verified")
        data = _read_bounded(fd, temporary.written.size)
        if len(data) != temporary.written.size:
            raise FileStorageError("temporary file cannot be verified")
        return data
    finally:
        os.close(fd)


def _discard_temporary(parent_fd: int, fd: int, name: str) -> None:
    try:
        os.close(fd)
    finally:
        _unlink_quietly(parent_fd, name)


def _publish_live(parent_fd: int, name: str, temporary: _Temporary, *, mode: int) -> os.stat_result:
    try:
        temporary.seal(mode)
    except OSError as exc:
        raise FileStorageError("file cannot be published") from exc
    try:
        os.rename(
            temporary.name,
            name,
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
        )
        os.fsync(parent_fd)
    except OSError as exc:
        raise FileStorageError("file cannot be published") from exc
    try:
        final = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except OSError as exc:
        raise FileStorageError("file cannot be verified") from exc
    if not stat.S_ISREG(final.st_mode):
        raise FileStorageError("file cannot be verified")
    if (final.st_dev, final.st_ino) != (temporary.written.dev, temporary.written.ino):
        raise FileStorageError("file cannot be verified")
    return final


def _publish_conflict_copy(
    parent_fd: int,
    *,
    parent_path: str,
    name: str,
    temporary: _Temporary,
    workspace_id: str,
    path: str,
) -> _ConflictCopy:
    stem, suffix = _split_name(name)
    for length in CONFLICT_HEX_LENGTHS:
        candidate = f"{stem}{CONFLICT_INFIX}{temporary.digest[:length]}{suffix}"
        if len(candidate.encode("utf-8")) > MAX_COMPONENT_BYTES:
            continue
        relative = f"{parent_path}/{candidate}" if parent_path else candidate
        try:
            existing = os.stat(candidate, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            _publish_live(parent_fd, candidate, temporary, mode=policies.CONFLICT_FILE_MODE)
            return _ConflictCopy(
                path=relative, conflict_id=_conflict_id_for(workspace_id, path, temporary.digest)
            )
        except OSError as exc:
            raise FileStorageError("conflict copy cannot be inspected") from exc
        if not stat.S_ISREG(existing.st_mode):
            continue
        if _hash_entry(parent_fd, candidate) == temporary.digest:
            return _ConflictCopy(
                path=relative, conflict_id=_conflict_id_for(workspace_id, path, temporary.digest)
            )
    raise FileStorageError("conflict copy cannot be named")


def _already_present_revision(
    transaction: scan.WorkspaceWriteTransaction,
    *,
    workspace_id: str,
    path: str,
    current: _Current,
    origin: str,
    task_id: str | None,
) -> scan.RevisionRecord:
    state = transaction.indexed_state(path)
    if state is not None and state.revision_id is not None and state.sha256 == current.sha256:
        return scan.RevisionRecord(
            revision_id=state.revision_id,
            path=state.path,
            sha256=current.sha256,
            size=current.size,
            observed_at=state.observed_at,
            origin=state.origin,
            task_id=state.task_id,
            supersedes_revision_id=None,
        )
    return scan.RevisionRecord(
        revision_id=events.revision_id_for(workspace_id, path, current.sha256),
        path=path,
        sha256=current.sha256,
        size=current.size,
        observed_at=transaction.observed_at(),
        origin=origin,
        task_id=task_id,
        supersedes_revision_id=None,
    )


def _write_result(
    revision: scan.RevisionRecord, *, created: bool, already_present: bool
) -> dict[str, object]:
    return {
        "revision": revision.to_wire(),
        "created": created,
        "already_present": already_present,
        "conflict": False,
    }


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


def _open_parent(
    workspace_fd: int,
    components: tuple[str, ...],
    *,
    hooks: paths.WalkHooks | None = None,
    create: bool = False,
    mode: int = workspace.PRIVATE_DIRECTORY_MODE,
) -> int:
    return paths.open_directory_chain(
        workspace_fd, components[:-1], create=create, mode=mode, hooks=hooks
    )


def _manifest_component(descriptor: workspace.WorkspaceDescriptor, field: str) -> str:
    value = descriptor.manifest.get(field)
    if not isinstance(value, str):
        raise FileStorageError("workspace manifest is not usable")
    return value


def _parse_expected_sha256(value: str | None) -> str | None:
    if value is None:
        raise FileRequestError("expected_sha256", "missing")
    if value == "new":
        return None
    if SHA256_PATTERN.fullmatch(value) is None:
        raise FileRequestError("expected_sha256", "invalid")
    return value


def _require_origin(route: policies.WriteRoute, origin: str | None) -> str:
    allowed = EDITABLE_ORIGINS if route is policies.WriteRoute.FILES else GENERATED_ORIGINS
    if origin is None or origin not in allowed:
        raise FileRequestError("origin", "invalid")
    return origin


def _require_generated_by(value: str | None) -> str:
    if value is None or value == "":
        raise FileRequestError("generated_by", "missing")
    if len(value.encode("utf-8")) > MAX_GENERATED_BY_BYTES:
        raise FileRequestError("generated_by", "too_long")
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        raise FileRequestError("generated_by", "control")
    return value


def _require_source_sha256(value: str | None) -> str:
    if value is None:
        raise FileRequestError("source_sha256", "missing")
    if SHA256_PATTERN.fullmatch(value) is None:
        raise FileRequestError("source_sha256", "invalid")
    return value


def _temporary_name(name: str) -> str:
    candidate = f".{name}.{uuid.uuid4().hex}{TEMP_FILE_SUFFIX}"
    if len(candidate.encode("utf-8")) <= MAX_COMPONENT_BYTES:
        return candidate
    return f".{uuid.uuid4().hex}{TEMP_FILE_SUFFIX}"


def _split_name(name: str) -> tuple[str, str]:
    position = name.rfind(".")
    if position <= 0:
        return name, ""
    return name[:position], name[position:]


def _parent_relative(path: str) -> str:
    parent, _, _ = path.rpartition("/")
    return parent


def _conflict_id_for(workspace_id: str, path: str, digest: str) -> str:
    material = f"{workspace_id}\x00{path}\x00{digest}".encode()
    return CONFLICT_ID_PREFIX + hashlib.sha256(material).hexdigest()[:32]


def _hash_entry(parent_fd: int, name: str) -> str:
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent_fd)
    except OSError as exc:
        raise FileStorageError("conflict copy cannot be read") from exc
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise FileStorageError("conflict copy is not a regular file")
        return _hash_fd(fd)
    finally:
        os.close(fd)


def _hash_fd(fd: int) -> str:
    digest = hashlib.sha256()
    os.lseek(fd, 0, os.SEEK_SET)
    while True:
        chunk = os.read(fd, STREAM_CHUNK_SIZE)
        if not chunk:
            return digest.hexdigest()
        digest.update(chunk)


def _read_bounded(fd: int, limit: int) -> bytes:
    chunks: list[bytes] = []
    remaining = limit + 1
    while remaining > 0:
        chunk = os.read(fd, remaining)
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _write_all(fd: int, payload: bytes) -> None:
    remaining = memoryview(payload)
    while remaining:
        written = os.write(fd, remaining)
        remaining = remaining[written:]


def _unlink_quietly(parent_fd: int, name: str) -> None:
    try:
        os.unlink(name, dir_fd=parent_fd)
    except OSError:
        pass
