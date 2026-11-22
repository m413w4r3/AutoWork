"""The rebuilt file index projection and its atomic writer (design §5.3, §6).

``file-index.json`` is a local, rebuildable cache of the last stable
observation of every addressable file. It is never authoritative: deleting it
is safe, the next start mints a new generation and rescans. Projections are
published with the same tmp+fsync+rename idiom as the manifest so a crash
leaves either the previous or the next projection, never a torn one.
"""

from __future__ import annotations

import json
import os
import stat
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field

FILE_INDEX_FILE_NAME = "file-index.json"
INDEX_SCHEMA = "autowork-workspace-index.v1"
PROJECTION_MODE = 0o600
TEMP_FILE_SUFFIX = ".tmp"

POLICY_HUMAN_EDITABLE = "human_editable"
POLICY_AGENT_OWNED = "agent_owned"
POLICY_GENERATED_IMMUTABLE = "generated_immutable"
POLICY_READ_ONLY = "read_only"
POLICY_AGENT_INTERNAL = "agent_internal"
POLICY_CLASSES: frozenset[str] = frozenset(
    {
        POLICY_HUMAN_EDITABLE,
        POLICY_AGENT_OWNED,
        POLICY_GENERATED_IMMUTABLE,
        POLICY_READ_ONLY,
        POLICY_AGENT_INTERNAL,
    }
)

ORIGIN_FILESYSTEM = "filesystem"
ORIGINS: frozenset[str] = frozenset({ORIGIN_FILESYSTEM, "ui", "agent", "system"})

FILE_STATE_FIELDS: tuple[str, ...] = (
    "path",
    "revision_id",
    "sha256",
    "size",
    "mode",
    "mtime_ns",
    "origin",
    "observed_at",
    "policy_class",
    "task_id",
)
INDEX_FIELDS: frozenset[str] = frozenset({"schema", "workspace_id", "generation", "files"})

_GENERATED_PREFIXES: tuple[str, ...] = ("plans", "vt", "results", "pcaps")
_HUMAN_PREFIXES: tuple[str, ...] = ("scripts", "regles", "discover_sample")


class ProjectionError(ValueError):
    """A persisted projection violates a required invariant (rebuild it)."""


class ProjectionWriteError(RuntimeError):
    """A projection could not be published and verified atomically."""


def policy_class_for(path: str) -> str:
    """Map a workspace-relative path to its §5.3 ``policy_class``."""
    components = path.split("/")
    top = components[0]
    if top == "notes" and len(components) >= 2:
        if components[1] == "humaines":
            return POLICY_HUMAN_EDITABLE
        if components[1] == "bridge":
            return POLICY_GENERATED_IMMUTABLE
        if components[1] == "agents":
            return POLICY_AGENT_OWNED
        return POLICY_HUMAN_EDITABLE
    if top in _HUMAN_PREFIXES:
        return POLICY_HUMAN_EDITABLE
    if top in _GENERATED_PREFIXES:
        return POLICY_GENERATED_IMMUTABLE
    if top == "base_samples":
        return POLICY_READ_ONLY
    if top == ".autowork":
        return POLICY_AGENT_INTERNAL
    # Unknown top-level entries are not addressable through the API; treating
    # them as human-editable keeps the agent from ever calling them generated.
    return POLICY_HUMAN_EDITABLE


@dataclass(frozen=True)
class FileState:
    """``FileStateV1`` plus the local observation of one stable file."""

    path: str
    revision_id: str | None
    sha256: str | None
    size: int
    mtime_ns: int
    mode: int
    origin: str
    observed_at: str
    policy_class: str
    task_id: str | None = None

    def to_wire(self) -> dict[str, object]:
        return {
            "path": self.path,
            "revision_id": self.revision_id,
            "sha256": self.sha256,
            "size": self.size,
            "mode": f"{self.mode:04o}",
            "mtime_ns": self.mtime_ns,
            "origin": self.origin,
            "observed_at": self.observed_at,
            "policy_class": self.policy_class,
            "task_id": self.task_id,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> FileState:
        if set(payload) != set(FILE_STATE_FIELDS):
            raise ProjectionError("unexpected file-state fields")
        origin = payload["origin"]
        if not isinstance(origin, str) or origin not in ORIGINS:
            raise ProjectionError("unknown file-state origin")
        policy_class = payload["policy_class"]
        if not isinstance(policy_class, str) or policy_class not in POLICY_CLASSES:
            raise ProjectionError("unknown file-state policy class")
        mode_text = payload["mode"]
        if not isinstance(mode_text, str):
            raise ProjectionError("file-state mode is not a string")
        try:
            mode = int(mode_text, 8)
        except ValueError as exc:
            raise ProjectionError("file-state mode is not octal") from exc
        if not 0 <= mode <= 0o7777:
            raise ProjectionError("file-state mode is out of range")
        path = payload["path"]
        if not isinstance(path, str):
            raise ProjectionError("file-state path is not a string")
        return cls(
            path=path,
            revision_id=_optional_str(payload["revision_id"], "revision_id"),
            sha256=_optional_str(payload["sha256"], "sha256"),
            size=_require_int(payload["size"], "size"),
            mtime_ns=_require_int(payload["mtime_ns"], "mtime_ns"),
            mode=mode,
            origin=origin,
            observed_at=_require_str(payload["observed_at"], "observed_at"),
            policy_class=policy_class,
            task_id=_optional_str(payload["task_id"], "task_id"),
        )


@dataclass
class FileIndex:
    """The path-keyed file index of one workspace generation."""

    workspace_id: str
    generation: int
    files: dict[str, FileState] = field(default_factory=dict)

    @classmethod
    def empty(cls, workspace_id: str, generation: int) -> FileIndex:
        return cls(workspace_id=workspace_id, generation=generation)

    def to_payload(self) -> dict[str, object]:
        return {
            "schema": INDEX_SCHEMA,
            "workspace_id": self.workspace_id,
            "generation": self.generation,
            "files": {path: state.to_wire() for path, state in self.files.items()},
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, object], *, workspace_id: str) -> FileIndex:
        if set(payload) != INDEX_FIELDS:
            raise ProjectionError("unexpected file-index fields")
        if payload["schema"] != INDEX_SCHEMA:
            raise ProjectionError("unexpected file-index schema")
        stored_workspace = payload["workspace_id"]
        if not isinstance(stored_workspace, str) or stored_workspace != workspace_id:
            raise ProjectionError("file-index belongs to another workspace")
        generation = _require_int(payload["generation"], "generation")
        if generation < 1:
            raise ProjectionError("file-index generation is not positive")
        raw_files = payload["files"]
        if not isinstance(raw_files, dict):
            raise ProjectionError("file-index files section is not an object")
        files: dict[str, FileState] = {}
        for raw_path, raw_state in raw_files.items():
            if not isinstance(raw_path, str) or not isinstance(raw_state, dict):
                raise ProjectionError("file-index entry is invalid")
            state = FileState.from_payload({str(key): value for key, value in raw_state.items()})
            if state.path != raw_path:
                raise ProjectionError("file-index key does not match its entry")
            files[raw_path] = state
        return cls(workspace_id=workspace_id, generation=generation, files=files)


def write_json_projection(directory_fd: int, name: str, payload: Mapping[str, object]) -> None:
    """Publish one projection atomically below ``directory_fd`` (§5.5 idiom)."""
    raw = (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    temporary = f".{name}.{uuid.uuid4().hex}{TEMP_FILE_SUFFIX}"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
    try:
        fd = os.open(temporary, flags, PROJECTION_MODE, dir_fd=directory_fd)
    except OSError as exc:
        raise ProjectionWriteError("projection temporary file cannot be created") from exc
    try:
        os.fchmod(fd, PROJECTION_MODE)
        _write_all(fd, raw)
        os.fsync(fd)
        written = os.fstat(fd)
    except OSError as exc:
        raise ProjectionWriteError("projection cannot be written") from exc
    finally:
        os.close(fd)
    try:
        os.rename(temporary, name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
        os.fsync(directory_fd)
    except OSError as exc:
        try:
            os.unlink(temporary, dir_fd=directory_fd)
        except OSError:
            pass  # the temporary may already be gone; the error below wins
        raise ProjectionWriteError("projection cannot be published") from exc
    try:
        final = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except OSError as exc:
        raise ProjectionWriteError("projection cannot be verified") from exc
    if not stat.S_ISREG(final.st_mode) or (final.st_dev, final.st_ino) != (
        written.st_dev,
        written.st_ino,
    ):
        raise ProjectionWriteError("projection write could not be verified")


def _require_str(value: object, field_name: str) -> str:
    if not isinstance(value, str):
        raise ProjectionError(f"{field_name} is not a string")
    return value


def _optional_str(value: object, field_name: str) -> str | None:
    if value is None:
        return None
    return _require_str(value, field_name)


def _require_int(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ProjectionError(f"{field_name} is not a valid integer")
    return value


def _write_all(fd: int, payload: bytes) -> None:
    remaining = memoryview(payload)
    while remaining:
        written = os.write(fd, remaining)
        remaining = remaining[written:]
