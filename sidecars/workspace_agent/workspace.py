"""Workspace identity, manifest and layout lifecycle (design §5.1-5.2).

The workspace directory is the agent's own aggregate: its only authoritative
local state is ``.autowork/manifest.json``, written atomically by ``ensure``
under a per-workspace ``flock``. ``ensure`` is idempotent, refreshes missing
layout directories, and never rewrites an existing manifest, deletes content or
repairs a corrupt one.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import stat
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from . import paths
from .protocol import ErrorCode, format_rfc3339_utc

MANIFEST_SCHEMA = "autowork-workspace-manifest.v1"
MANIFEST_FILE_NAME = "manifest.json"
SYNC_STATE_FILE_NAME = "sync-state.json"
CREATED_BY = "autowork-workspace-agent"
LAYOUT_VERSION = 1
AGENT_INTERNAL_DIRECTORY = paths.AGENT_INTERNAL_DIRECTORY

DELIVERY_CODE_PATTERN = re.compile(r"^[A-Z0-9][A-Z0-9._-]{0,63}$")
# Same semantics as cti_app.domain.entities.SLUG_PATTERN, re-implemented here
# because this sidecar is stdlib-only and never imports the backend package.
ARTICLE_SLUG_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
ARTICLE_SLUG_MAX_LENGTH = 80
SYNTHESIS_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
WORKSPACE_ID_PATTERN = re.compile(r"^ws-[0-9a-f]{32}$")

MAX_MANIFEST_BYTES = 64 * 1024
PRIVATE_DIRECTORY_MODE = 0o700
BASE_SAMPLES_DIRECTORY_MODE = 0o750
MANIFEST_MODE = 0o600

REQUIRED_FIELDS: tuple[str, ...] = (
    "delivery_code",
    "article_slug",
    "subject_id",
    "investigation_id",
    "synthesis_sha256",
)
MANIFEST_FIELDS: frozenset[str] = frozenset(
    {
        "schema",
        "workspace_id",
        "delivery_code",
        "article_slug",
        "subject_id",
        "investigation_id",
        "synthesis_sha256",
        "created_at",
        "created_by",
        "layout_version",
        "identity_sha256",
    }
)

LAYOUT_DIRECTORIES: tuple[tuple[tuple[str, ...], int], ...] = (
    ((".autowork",), PRIVATE_DIRECTORY_MODE),
    ((".autowork", "incoming"), PRIVATE_DIRECTORY_MODE),
    (("base_samples",), BASE_SAMPLES_DIRECTORY_MODE),
    (("pcaps",), PRIVATE_DIRECTORY_MODE),
    (("scripts",), PRIVATE_DIRECTORY_MODE),
    (("regles",), PRIVATE_DIRECTORY_MODE),
    (("discover_sample",), PRIVATE_DIRECTORY_MODE),
    (("plans",), PRIVATE_DIRECTORY_MODE),
    (("results",), PRIVATE_DIRECTORY_MODE),
    (("notes",), PRIVATE_DIRECTORY_MODE),
    (("notes", "humaines"), PRIVATE_DIRECTORY_MODE),
    (("notes", "bridge"), PRIVATE_DIRECTORY_MODE),
    (("notes", "agents"), PRIVATE_DIRECTORY_MODE),
    (("vt",), PRIVATE_DIRECTORY_MODE),
    (("vt", "files"), PRIVATE_DIRECTORY_MODE),
    (("vt", "ips"), PRIVATE_DIRECTORY_MODE),
    (("vt", "domains"), PRIVATE_DIRECTORY_MODE),
    (("vt", "urls"), PRIVATE_DIRECTORY_MODE),
    (("vt", "behaviours"), PRIVATE_DIRECTORY_MODE),
    (("vt", "relationships"), PRIVATE_DIRECTORY_MODE),
)


class WorkspaceError(Exception):
    """A workspace operation rejected with a protocol error code and status."""

    code: ErrorCode = ErrorCode.INTERNAL_ERROR
    status: int = 500

    def __init__(self, message: str, *, details: Mapping[str, object] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details: dict[str, object] = {} if details is None else dict(details)


class InvalidWorkspaceRequestError(WorkspaceError):
    """An ``EnsureWorkspaceV1`` payload violates the identity contract."""

    code = ErrorCode.INVALID_REQUEST
    status = 400

    def __init__(self, field: str, reason: str) -> None:
        super().__init__(
            "invalid workspace ensure request", details={"field": field, "reason": reason}
        )


class WorkspaceIdentityConflictError(WorkspaceError):
    """The workspace path already holds a different immutable identity."""

    code = ErrorCode.WORKSPACE_IDENTITY_CONFLICT
    status = 409

    def __init__(self, workspace_id: str) -> None:
        super().__init__(
            "workspace path already holds another identity",
            details={"workspace_id": workspace_id},
        )


class WorkspaceUnmanagedDirectoryError(WorkspaceError):
    """Human content or an unreadable manifest occupies the workspace path."""

    code = ErrorCode.WORKSPACE_UNMANAGED_DIRECTORY
    status = 409

    def __init__(self, *, reason: str) -> None:
        super().__init__(
            "workspace directory is not managed by this agent", details={"reason": reason}
        )


class WorkspaceNotFoundError(WorkspaceError):
    """No live manifest carries the requested workspace id."""

    code = ErrorCode.WORKSPACE_NOT_FOUND
    status = 404

    def __init__(self, workspace_id: str) -> None:
        super().__init__("workspace not found", details={"workspace_id": workspace_id})


class WorkspaceStorageError(WorkspaceError):
    """Local storage is missing, unsafe or failed a write verification."""

    code = ErrorCode.INTERNAL_ERROR
    status = 500


@dataclass(frozen=True)
class EnsureWorkspaceRequest:
    """Validated ``EnsureWorkspaceV1`` fields (identity is immutable)."""

    delivery_code: str
    article_slug: str
    subject_id: str
    investigation_id: str
    synthesis_sha256: str

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> EnsureWorkspaceRequest:
        for field in REQUIRED_FIELDS:
            if field not in payload:
                raise InvalidWorkspaceRequestError(field, "missing")
        for field in payload:
            if field not in REQUIRED_FIELDS:
                raise InvalidWorkspaceRequestError(str(field), "unknown_field")
        return cls(
            delivery_code=_require_delivery_code(payload["delivery_code"]),
            article_slug=_require_article_slug(payload["article_slug"]),
            subject_id=_require_uuid("subject_id", payload["subject_id"]),
            investigation_id=_require_uuid("investigation_id", payload["investigation_id"]),
            synthesis_sha256=_require_synthesis_sha256(payload["synthesis_sha256"]),
        )

    @property
    def workspace_id(self) -> str:
        return workspace_id_for(self.delivery_code, self.article_slug)

    @property
    def identity_sha256(self) -> str:
        return compute_identity_sha256(
            workspace_id=self.workspace_id,
            delivery_code=self.delivery_code,
            article_slug=self.article_slug,
            subject_id=self.subject_id,
            investigation_id=self.investigation_id,
            synthesis_sha256=self.synthesis_sha256,
        )


@dataclass(frozen=True)
class WorkspaceDescriptor:
    """``WorkspaceV1``: identity, host path and the authoritative manifest."""

    workspace_id: str
    host_path: str
    created: bool
    manifest: Mapping[str, object]


@dataclass(frozen=True)
class WorkspaceSummary:
    """One ``GET /v1/workspaces`` manifest-discovery entry."""

    workspace_id: str
    delivery_code: str
    article_slug: str
    host_path: str
    generation: int


def workspace_id_for(delivery_code: str, article_slug: str) -> str:
    """Return the deterministic ``ws-`` id of an identity pair."""
    digest = hashlib.sha256(f"{delivery_code}\x00{article_slug}".encode()).hexdigest()
    return "ws-" + digest[:32]


def compute_identity_sha256(
    *,
    workspace_id: str,
    delivery_code: str,
    article_slug: str,
    subject_id: str,
    investigation_id: str,
    synthesis_sha256: str,
) -> str:
    """Hash the canonical identity object stored in ``identity_sha256``."""
    fields = {
        "schema": MANIFEST_SCHEMA,
        "workspace_id": workspace_id,
        "delivery_code": delivery_code,
        "article_slug": article_slug,
        "subject_id": subject_id,
        "investigation_id": investigation_id,
        "synthesis_sha256": synthesis_sha256,
    }
    canonical = json.dumps(fields, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_manifest(request: EnsureWorkspaceRequest, created_at: str) -> dict[str, object]:
    """Build the normative manifest object (keys and values of §5.1)."""
    return {
        "schema": MANIFEST_SCHEMA,
        "workspace_id": request.workspace_id,
        "delivery_code": request.delivery_code,
        "article_slug": request.article_slug,
        "subject_id": request.subject_id,
        "investigation_id": request.investigation_id,
        "synthesis_sha256": request.synthesis_sha256,
        "created_at": created_at,
        "created_by": CREATED_BY,
        "layout_version": LAYOUT_VERSION,
        "identity_sha256": request.identity_sha256,
    }


class WorkspaceManager:
    """Manifest discovery, idempotent ``ensure`` and layout refresh under one root."""

    def __init__(self, root: Path) -> None:
        path = Path(root)
        if not path.is_absolute():
            raise ValueError("workspace root must be an absolute path")
        self._root = path

    @property
    def root(self) -> Path:
        return self._root

    def ensure(self, request: EnsureWorkspaceRequest) -> WorkspaceDescriptor:
        """Create or refresh the workspace for ``request`` under its locks."""
        self._prepare_root()
        try:
            root_fd = paths.open_pinned_root(self._root)
        except paths.PathResolutionError as exc:
            raise WorkspaceStorageError("workspace root cannot be opened") from exc
        try:
            delivery_fd = self._open_layout_directory(root_fd, (request.delivery_code,))
            try:
                workspace_fd = self._open_layout_directory(delivery_fd, (request.article_slug,))
                try:
                    return self._ensure_workspace(workspace_fd, request)
                finally:
                    os.close(workspace_fd)
            finally:
                os.close(delivery_fd)
        finally:
            os.close(root_fd)

    def list_workspaces(self) -> list[WorkspaceSummary]:
        """Discover ``<root>/*/*/.autowork/manifest.json`` read-only."""
        summaries = [
            WorkspaceSummary(
                workspace_id=_manifest_str(manifest, "workspace_id"),
                delivery_code=_manifest_str(manifest, "delivery_code"),
                article_slug=_manifest_str(manifest, "article_slug"),
                host_path=str(workspace_dir),
                # The generation is minted by the scan projections (W3); a
                # workspace that was never scanned has none.
                generation=0,
            )
            for workspace_dir, manifest in self._scan_manifests()
        ]
        summaries.sort(key=lambda item: (item.delivery_code, item.article_slug))
        return summaries

    def get_workspace(self, workspace_id: str) -> WorkspaceDescriptor:
        """Return the live descriptor of one workspace or ``workspace_not_found``."""
        if WORKSPACE_ID_PATTERN.fullmatch(workspace_id) is None:
            raise WorkspaceNotFoundError(workspace_id)
        for workspace_dir, manifest in self._scan_manifests():
            if manifest.get("workspace_id") == workspace_id:
                return WorkspaceDescriptor(
                    workspace_id=workspace_id,
                    host_path=str(workspace_dir),
                    created=False,
                    manifest=manifest,
                )
        raise WorkspaceNotFoundError(workspace_id)

    def _prepare_root(self) -> None:
        try:
            os.makedirs(self._root, mode=PRIVATE_DIRECTORY_MODE, exist_ok=True)
            info = os.lstat(self._root)
        except OSError as exc:
            raise WorkspaceStorageError("workspace root cannot be created") from exc
        if not stat.S_ISDIR(info.st_mode):
            raise WorkspaceStorageError("workspace root is not a directory")

    def _open_layout_directory(self, parent_fd: int, components: tuple[str, ...]) -> int:
        try:
            return paths.open_directory_chain(
                parent_fd, components, create=True, mode=PRIVATE_DIRECTORY_MODE
            )
        except paths.ConcurrentChangeError:
            raise
        except paths.WorkspacePathError as exc:
            raise WorkspaceStorageError("workspace directory cannot be prepared") from exc

    def _ensure_workspace(
        self, workspace_fd: int, request: EnsureWorkspaceRequest
    ) -> WorkspaceDescriptor:
        delivery_code = request.delivery_code
        article_slug = request.article_slug
        # The workspace directory fd is the per-workspace lock: it exists before
        # any state is created, so concurrent ensures serialise even on an
        # unmanaged directory (which must not receive an .autowork marker).
        try:
            fcntl.flock(workspace_fd, fcntl.LOCK_EX)
        except OSError as exc:
            raise WorkspaceStorageError("workspace lock cannot be acquired") from exc
        manifest = self._load_manifest(
            workspace_fd, delivery_code=delivery_code, article_slug=article_slug
        )
        if manifest is None and self._has_foreign_entries(workspace_fd):
            raise WorkspaceUnmanagedDirectoryError(reason="unmanaged_directory")

        autowork_fd = self._open_autowork_directory(workspace_fd)
        try:
            host_path = str(self._root / delivery_code / article_slug)
            if manifest is not None:
                if manifest["identity_sha256"] != request.identity_sha256:
                    raise WorkspaceIdentityConflictError(request.workspace_id)
                self._create_layout(workspace_fd)
                return WorkspaceDescriptor(
                    workspace_id=request.workspace_id,
                    host_path=host_path,
                    created=False,
                    manifest=manifest,
                )
            self._create_layout(workspace_fd)
            manifest = self._write_manifest(
                autowork_fd, build_manifest(request, format_rfc3339_utc(datetime.now(UTC)))
            )
            return WorkspaceDescriptor(
                workspace_id=request.workspace_id,
                host_path=host_path,
                created=True,
                manifest=manifest,
            )
        finally:
            os.close(autowork_fd)

    def _open_autowork_directory(self, workspace_fd: int) -> int:
        try:
            return paths.open_directory_chain(
                workspace_fd, (AGENT_INTERNAL_DIRECTORY,), create=True, mode=PRIVATE_DIRECTORY_MODE
            )
        except paths.ConcurrentChangeError:
            raise
        except paths.WorkspacePathError as exc:
            raise WorkspaceUnmanagedDirectoryError(reason="metadata_corrupt") from exc

    def _has_foreign_entries(self, workspace_fd: int) -> bool:
        try:
            names = os.listdir(workspace_fd)
        except OSError as exc:
            raise WorkspaceStorageError("workspace directory cannot be inspected") from exc
        return any(name != AGENT_INTERNAL_DIRECTORY for name in names)

    def _create_layout(self, workspace_fd: int) -> None:
        for components, mode in LAYOUT_DIRECTORIES:
            try:
                fd = paths.open_directory_chain(workspace_fd, components, create=True, mode=mode)
            except paths.ConcurrentChangeError:
                raise
            except paths.WorkspacePathError as exc:
                raise WorkspaceStorageError("workspace layout cannot be prepared") from exc
            os.close(fd)

    def _load_manifest(
        self, workspace_fd: int, *, delivery_code: str, article_slug: str
    ) -> dict[str, object] | None:
        try:
            entry = os.stat(AGENT_INTERNAL_DIRECTORY, dir_fd=workspace_fd, follow_symlinks=False)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise WorkspaceUnmanagedDirectoryError(reason="metadata_corrupt") from exc
        if not stat.S_ISDIR(entry.st_mode):
            raise WorkspaceUnmanagedDirectoryError(reason="metadata_corrupt")
        try:
            autowork_fd = paths.open_directory_chain(
                workspace_fd, (AGENT_INTERNAL_DIRECTORY,), create=False
            )
        except paths.ConcurrentChangeError:
            raise
        except paths.WorkspacePathError as exc:
            raise WorkspaceUnmanagedDirectoryError(reason="metadata_corrupt") from exc
        try:
            try:
                manifest_fd = os.open(
                    MANIFEST_FILE_NAME,
                    os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
                    dir_fd=autowork_fd,
                )
            except FileNotFoundError:
                return None
            except OSError as exc:
                raise WorkspaceUnmanagedDirectoryError(reason="manifest_corrupt") from exc
            try:
                if not stat.S_ISREG(os.fstat(manifest_fd).st_mode):
                    raise WorkspaceUnmanagedDirectoryError(reason="manifest_corrupt")
                raw = _read_bounded(manifest_fd, MAX_MANIFEST_BYTES)
            finally:
                os.close(manifest_fd)
        finally:
            os.close(autowork_fd)
        if len(raw) > MAX_MANIFEST_BYTES:
            raise WorkspaceUnmanagedDirectoryError(reason="manifest_corrupt")
        return _parse_manifest(raw, delivery_code=delivery_code, article_slug=article_slug)

    def _write_manifest(self, autowork_fd: int, manifest: dict[str, object]) -> dict[str, object]:
        payload = (
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8")
        temporary = f".{MANIFEST_FILE_NAME}.{uuid.uuid4().hex}.tmp"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
        try:
            fd = os.open(temporary, flags, MANIFEST_MODE, dir_fd=autowork_fd)
        except OSError as exc:
            raise WorkspaceStorageError("manifest temporary file cannot be created") from exc
        try:
            os.fchmod(fd, MANIFEST_MODE)
            _write_all(fd, payload)
            os.fsync(fd)
            written = os.fstat(fd)
        except OSError as exc:
            raise WorkspaceStorageError("manifest cannot be written") from exc
        finally:
            os.close(fd)
        try:
            os.rename(
                temporary,
                MANIFEST_FILE_NAME,
                src_dir_fd=autowork_fd,
                dst_dir_fd=autowork_fd,
            )
            os.fsync(autowork_fd)
        except OSError as exc:
            try:
                os.unlink(temporary, dir_fd=autowork_fd)
            except OSError:
                pass  # the temporary may already be gone; the error below wins
            raise WorkspaceStorageError("manifest cannot be published") from exc
        try:
            final = os.stat(MANIFEST_FILE_NAME, dir_fd=autowork_fd, follow_symlinks=False)
        except OSError as exc:
            raise WorkspaceStorageError("manifest cannot be verified") from exc
        if not stat.S_ISREG(final.st_mode) or (final.st_dev, final.st_ino) != (
            written.st_dev,
            written.st_ino,
        ):
            raise WorkspaceStorageError("manifest write could not be verified")
        return manifest

    def _scan_manifests(self) -> list[tuple[Path, dict[str, object]]]:
        try:
            root_fd = paths.open_pinned_root(self._root)
        except paths.PathResolutionError:
            return []
        found: list[tuple[Path, dict[str, object]]] = []
        try:
            try:
                delivery_names = sorted(os.listdir(root_fd))
            except OSError:
                return []
            for delivery_name in delivery_names:
                if DELIVERY_CODE_PATTERN.fullmatch(delivery_name) is None:
                    continue
                try:
                    delivery_fd = paths.open_directory_chain(root_fd, (delivery_name,))
                except paths.WorkspacePathError:
                    continue
                try:
                    try:
                        article_names = sorted(os.listdir(delivery_fd))
                    except OSError:
                        continue
                    for article_name in article_names:
                        if ARTICLE_SLUG_PATTERN.fullmatch(article_name) is None:
                            continue
                        try:
                            workspace_fd = paths.open_directory_chain(delivery_fd, (article_name,))
                        except paths.WorkspacePathError:
                            continue
                        try:
                            try:
                                manifest = self._load_manifest(
                                    workspace_fd,
                                    delivery_code=delivery_name,
                                    article_slug=article_name,
                                )
                            except (WorkspaceError, paths.WorkspacePathError):
                                manifest = None
                        finally:
                            os.close(workspace_fd)
                        if manifest is not None:
                            found.append((self._root / delivery_name / article_name, manifest))
                finally:
                    os.close(delivery_fd)
        finally:
            os.close(root_fd)
        return found


def _manifest_str(manifest: Mapping[str, object], field: str) -> str:
    value = manifest[field]
    assert isinstance(value, str)
    return value


def _parse_manifest(raw: bytes, *, delivery_code: str, article_slug: str) -> dict[str, object]:
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WorkspaceUnmanagedDirectoryError(reason="manifest_corrupt") from exc
    if not isinstance(value, dict):
        raise WorkspaceUnmanagedDirectoryError(reason="manifest_corrupt")
    manifest = {str(key): item for key, item in value.items()}
    if set(manifest) != MANIFEST_FIELDS:
        raise WorkspaceUnmanagedDirectoryError(reason="manifest_corrupt")
    for field in (
        "schema",
        "workspace_id",
        "delivery_code",
        "article_slug",
        "subject_id",
        "investigation_id",
        "synthesis_sha256",
        "created_at",
        "created_by",
        "identity_sha256",
    ):
        if not isinstance(manifest[field], str):
            raise WorkspaceUnmanagedDirectoryError(reason="manifest_corrupt")
    layout_version = manifest["layout_version"]
    if isinstance(layout_version, bool) or layout_version != LAYOUT_VERSION:
        raise WorkspaceUnmanagedDirectoryError(reason="manifest_corrupt")
    if manifest["schema"] != MANIFEST_SCHEMA:
        raise WorkspaceUnmanagedDirectoryError(reason="manifest_corrupt")
    if manifest["delivery_code"] != delivery_code or manifest["article_slug"] != article_slug:
        raise WorkspaceUnmanagedDirectoryError(reason="manifest_corrupt")
    if manifest["workspace_id"] != workspace_id_for(delivery_code, article_slug):
        raise WorkspaceUnmanagedDirectoryError(reason="manifest_corrupt")
    if SYNTHESIS_SHA256_PATTERN.fullmatch(_manifest_str(manifest, "synthesis_sha256")) is None:
        raise WorkspaceUnmanagedDirectoryError(reason="manifest_corrupt")
    for field in ("subject_id", "investigation_id"):
        text = _manifest_str(manifest, field)
        try:
            parsed = uuid.UUID(text)
        except (ValueError, AttributeError) as exc:
            raise WorkspaceUnmanagedDirectoryError(reason="manifest_corrupt") from exc
        if str(parsed) != text:
            raise WorkspaceUnmanagedDirectoryError(reason="manifest_corrupt")
    identity = compute_identity_sha256(
        workspace_id=_manifest_str(manifest, "workspace_id"),
        delivery_code=_manifest_str(manifest, "delivery_code"),
        article_slug=_manifest_str(manifest, "article_slug"),
        subject_id=_manifest_str(manifest, "subject_id"),
        investigation_id=_manifest_str(manifest, "investigation_id"),
        synthesis_sha256=_manifest_str(manifest, "synthesis_sha256"),
    )
    if identity != manifest["identity_sha256"]:
        raise WorkspaceUnmanagedDirectoryError(reason="manifest_corrupt")
    return manifest


def _require_str(field: str, value: object) -> str:
    if not isinstance(value, str):
        raise InvalidWorkspaceRequestError(field, "invalid")
    return value


def _require_delivery_code(value: object) -> str:
    text = _require_str("delivery_code", value)
    if DELIVERY_CODE_PATTERN.fullmatch(text) is None:
        raise InvalidWorkspaceRequestError("delivery_code", "invalid")
    return text


def _require_article_slug(value: object) -> str:
    text = _require_str("article_slug", value)
    if len(text) > ARTICLE_SLUG_MAX_LENGTH or ARTICLE_SLUG_PATTERN.fullmatch(text) is None:
        raise InvalidWorkspaceRequestError("article_slug", "invalid")
    return text


def _require_uuid(field: str, value: object) -> str:
    text = _require_str(field, value)
    try:
        parsed = uuid.UUID(text)
    except (ValueError, AttributeError) as exc:
        raise InvalidWorkspaceRequestError(field, "invalid") from exc
    return str(parsed)


def _require_synthesis_sha256(value: object) -> str:
    text = _require_str("synthesis_sha256", value)
    if SYNTHESIS_SHA256_PATTERN.fullmatch(text) is None:
        raise InvalidWorkspaceRequestError("synthesis_sha256", "invalid")
    return text


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
