"""Polling reconcile scan, stable observations, projections and pages (§5.5-§7).

One ``scan(workspace)`` is the only change detector: it walks the tree with
dirfds (never ``Path.resolve``), skips ``.autowork/``, ``base_samples/`` content
and ``.*.tmp`` temporaries, reports symbolic links without following them and
emits a file event only once ``(dev,ino,size,mtime_ns)`` stayed unchanged across
two observations at least ``--stability-window-ms`` apart (plus an ``fstat``
re-check on the opened descriptor). Files above 32 MiB are indexed metadata-only
with ``sha256 = null``. Scan-detected changes always carry ``origin
filesystem``; authorship is never inferred from a folder name.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from . import events, index, paths, workspace
from .events import FileEvent, SyncState
from .protocol import ErrorCode, format_rfc3339_utc

METADATA_ONLY_THRESHOLD_BYTES = 32 * 1024 * 1024
HASH_CHUNK_BYTES = 1024 * 1024
MAX_PROJECTION_BYTES = 64 * 1024 * 1024
DEFAULT_PAGE_LIMIT = 100
MAX_PAGE_LIMIT = 500
EXECUTABLE_BITS = 0o111
TEMP_FILE_PREFIX = "."
TEMP_FILE_SUFFIX = ".tmp"
BASE_SAMPLES_DIRECTORY = "base_samples"
MAX_KNOWN_REVISIONS = 100
IDEMPOTENCY_RETENTION_NS = 24 * 60 * 60 * 1_000_000_000
IDEMPOTENCY_MAX_KEYS = 1000

POLICY_GENERATED: frozenset[str] = frozenset(
    {index.POLICY_GENERATED_IMMUTABLE, index.POLICY_READ_ONLY}
)
VIOLATION_SYMLINK = "symlink"
VIOLATION_EXECUTABLE = "executable_bit"
VIOLATION_GENERATED_MODIFIED = "generated_modified"

Signature = tuple[int, int, int, int]


class Clock(Protocol):
    """Injectable time source: monotonic seconds, aware wall clock and wall ns."""

    def monotonic(self) -> float: ...

    def now(self) -> datetime: ...

    def wall_ns(self) -> int: ...


class SystemClock:
    """Production clock; tests inject a deterministic ``FakeClock``."""

    def monotonic(self) -> float:
        return time.monotonic()

    def now(self) -> datetime:
        return datetime.now(UTC)

    def wall_ns(self) -> int:
        return time.time_ns()


class ScanRequestError(workspace.WorkspaceError):
    """A scan/read request violates the local contract (bad limit, cursor)."""

    code = ErrorCode.INVALID_REQUEST
    status = 400


class CursorExpiredError(workspace.WorkspaceError):
    """The client cursor predates the retained window or another generation."""

    code = ErrorCode.CURSOR_EXPIRED
    status = 409

    def __init__(
        self,
        *,
        generation: int,
        oldest_retained: int,
        last_seq: int,
        after: int,
    ) -> None:
        super().__init__(
            "event cursor expired; resynchronise from a snapshot",
            details={
                "resync_required": True,
                "generation": generation,
                "oldest_retained": oldest_retained,
                "last_seq": last_seq,
                "after": after,
            },
        )


@dataclass(frozen=True)
class ScanResult:
    """``ScanResultV1``: one scan outcome."""

    generation: int
    latest_cursor: int
    emitted: int
    policy_violations: int


@dataclass(frozen=True)
class SnapshotPage:
    """One ``SnapshotPageV1`` slice of the stable index."""

    generation: int
    files: tuple[index.FileState, ...]
    next_page_token: str | None


@dataclass(frozen=True)
class EventsPage:
    """One ``EventsPageV1`` slice of the retained event log."""

    generation: int
    events: tuple[FileEvent, ...]
    next_cursor: int
    oldest_retained: int
    resync_required: bool


@dataclass(frozen=True)
class FilesPage:
    """One ``GET /files`` page (prefix-filtered list view)."""

    generation: int
    files: tuple[index.FileState, ...]
    next_page_token: str | None


@dataclass(frozen=True)
class RevisionRecord:
    """``RevisionV1``: one revision the local projections know about (§4, §6)."""

    revision_id: str
    path: str
    sha256: str
    size: int
    observed_at: str
    origin: str
    task_id: str | None
    supersedes_revision_id: str | None

    def to_wire(self) -> dict[str, object]:
        return {
            "revision_id": self.revision_id,
            "path": self.path,
            "sha256": self.sha256,
            "size": self.size,
            "observed_at": self.observed_at,
            "origin": self.origin,
            "task_id": self.task_id,
            "supersedes_revision_id": self.supersedes_revision_id,
        }


@dataclass(frozen=True)
class _PendingObservation:
    signature: Signature
    started: float


@dataclass(frozen=True)
class _ObservedFile:
    path: str
    dev: int
    ino: int
    size: int
    mtime_ns: int
    mode: int
    policy_class: str
    sha256: str | None
    metadata_only: bool
    ready: bool
    unchanged: bool


@dataclass(frozen=True)
class _SymlinkObservation:
    path: str
    size: int
    mtime_ns: int
    mode: int
    policy_class: str


@dataclass(frozen=True)
class _ModeObservation:
    path: str
    size: int
    mtime_ns: int
    mode: int
    policy_class: str


@dataclass
class _WalkResult:
    files: dict[str, _ObservedFile] = field(default_factory=dict)
    symlinks: dict[str, _SymlinkObservation] = field(default_factory=dict)
    base_sample_violations: dict[str, _ModeObservation] = field(default_factory=dict)
    complete: bool = True


@dataclass(frozen=True)
class _Violation:
    path: str
    policy_class: str
    reason: str
    signature: str
    observed_at: str

    def to_payload(self) -> dict[str, object]:
        return {
            "path": self.path,
            "policy_class": self.policy_class,
            "reason": self.reason,
            "signature": self.signature,
            "observed_at": self.observed_at,
        }


@dataclass(frozen=True)
class _EventSpec:
    kind: events.EventKind
    path: str
    policy_class: str
    previous_path: str | None = None
    revision_id: str | None = None
    previous_revision_id: str | None = None
    sha256: str | None = None
    size: int | None = None

    def sort_key(self) -> tuple[str, str, str, str, str]:
        return (
            self.path,
            str(self.kind),
            self.previous_path or "",
            self.revision_id or "",
            self.previous_revision_id or "",
        )


class WorkspaceScanService:
    """Per-workspace scanners behind one lock; the agent starts the poll loop."""

    def __init__(
        self,
        workspaces: workspace.WorkspaceManager,
        *,
        poll_interval_ms: int = 1000,
        stability_window_ms: int = 400,
        clock: Clock | None = None,
        retained_event_limit: int = events.MAX_RETAINED_EVENTS,
    ) -> None:
        if poll_interval_ms < 1:
            raise ValueError("the polling interval must be positive")
        if stability_window_ms < 0:
            raise ValueError("the stability window must be non-negative")
        if retained_event_limit < 1:
            raise ValueError("the retained event limit must be positive")
        self._workspaces = workspaces
        self._clock: Clock = SystemClock() if clock is None else clock
        self._poll_interval_seconds = poll_interval_ms / 1000.0
        self._stability_window_seconds = stability_window_ms / 1000.0
        self._retained_event_limit = retained_event_limit
        self._lock = threading.Lock()
        self._scanners: dict[str, _WorkspaceScanner] = {}

    @property
    def poll_interval_seconds(self) -> float:
        return self._poll_interval_seconds

    @property
    def stability_window_seconds(self) -> float:
        return self._stability_window_seconds

    def scan(self, workspace_id: str, *, full: bool = False) -> ScanResult:
        return self._scanner(workspace_id).scan(full=full)

    def snapshot(
        self, workspace_id: str, *, page_token: str | None = None, limit: int | None = None
    ) -> SnapshotPage:
        return self._scanner(workspace_id).snapshot(page_token=page_token, limit=limit)

    def files(
        self,
        workspace_id: str,
        *,
        prefix: str | None = None,
        page_token: str | None = None,
        limit: int | None = None,
    ) -> FilesPage:
        return self._scanner(workspace_id).files(prefix=prefix, page_token=page_token, limit=limit)

    def events(
        self,
        workspace_id: str,
        *,
        after: int = 0,
        limit: int | None = None,
        generation: int | None = None,
    ) -> EventsPage:
        return self._scanner(workspace_id).events(after=after, limit=limit, generation=generation)

    def known_revisions(
        self,
        workspace_id: str,
        path: str,
        *,
        limit: int = MAX_KNOWN_REVISIONS,
    ) -> tuple[RevisionRecord, ...]:
        """Bounded revision history of one path from the local projections."""
        if limit < 1:
            raise ValueError("the revision limit must be positive")
        scanner = self._scanner(workspace_id)
        with scanner.lock:
            scanner._scan_if_unloaded_locked()
            return scanner._known_revisions_locked(path, limit=limit)

    def write_transaction(self, workspace_id: str) -> WorkspaceWriteTransaction:
        """Hold the per-workspace lock across a CAS decision and its projection update."""
        return WorkspaceWriteTransaction(self._scanner(workspace_id))

    def _scanner(self, workspace_id: str) -> _WorkspaceScanner:
        if workspace.WORKSPACE_ID_PATTERN.fullmatch(workspace_id) is None:
            raise workspace.WorkspaceNotFoundError(workspace_id)
        with self._lock:
            scanner = self._scanners.get(workspace_id)
            if scanner is None:
                scanner = _WorkspaceScanner(
                    workspace_id=workspace_id,
                    root=self._workspaces.root,
                    workspaces=self._workspaces,
                    clock=self._clock,
                    stability_window=self._stability_window_seconds,
                    retained_event_limit=self._retained_event_limit,
                )
                self._scanners[workspace_id] = scanner
            return scanner


class _WorkspaceScanner:
    """The scanning state of one workspace (index, log, stability bookkeeping)."""

    def __init__(
        self,
        *,
        workspace_id: str,
        root: Path,
        workspaces: workspace.WorkspaceManager,
        clock: Clock,
        stability_window: float,
        retained_event_limit: int,
    ) -> None:
        self.workspace_id = workspace_id
        self.lock = threading.Lock()
        self._root = root
        self._workspaces = workspaces
        self._clock = clock
        self._stability_window = stability_window
        self._retained_event_limit = retained_event_limit
        self._loaded = False
        self._dirty = False
        self._file_index: index.FileIndex | None = None
        self._sync: SyncState | None = None
        self._violations: dict[str, _Violation] = {}
        self._foreign_violations: list[dict[str, object]] = []
        self._pending: dict[str, _PendingObservation] = {}
        self._pending_deletions: dict[str, float] = {}
        self._inodes: dict[str, tuple[int, int]] = {}

    # -- public reads ---------------------------------------------------------

    def scan(self, *, full: bool = False) -> ScanResult:
        with self.lock:
            return self._scan_locked(full=full)

    def snapshot(self, *, page_token: str | None, limit: int | None) -> SnapshotPage:
        with self.lock:
            self._scan_if_unloaded_locked()
            page_limit = _page_limit(limit)
            token = _page_token(page_token)
            states = sorted(self._index.files.values(), key=lambda state: state.path)
            selected, next_token = _paginate(states, token, page_limit)
            return SnapshotPage(self._index.generation, selected, next_token)

    def files(self, *, prefix: str | None, page_token: str | None, limit: int | None) -> FilesPage:
        with self.lock:
            self._scan_if_unloaded_locked()
            page_limit = _page_limit(limit)
            token = _page_token(page_token)
            if prefix is not None and len(prefix) > paths.MAX_PATH_BYTES:
                raise ScanRequestError("prefix is too long", details={"parameter": "prefix"})
            states = [
                state
                for state in sorted(self._index.files.values(), key=lambda item: item.path)
                if prefix is None or state.path.startswith(prefix)
            ]
            selected, next_token = _paginate(states, token, page_limit)
            return FilesPage(self._index.generation, selected, next_token)

    def events(self, *, after: int, limit: int | None, generation: int | None) -> EventsPage:
        with self.lock:
            self._scan_if_unloaded_locked()
            page_limit = _page_limit(limit)
            if after < 0:
                raise ScanRequestError("after must be non-negative", details={"parameter": "after"})
            sync = self._sync_state
            if generation is not None and generation != sync.generation:
                raise CursorExpiredError(
                    generation=sync.generation,
                    oldest_retained=sync.log.oldest_retained,
                    last_seq=sync.log.last_seq,
                    after=after,
                )
            page = sync.log.page(after, page_limit)
            if page.resync_required:
                raise CursorExpiredError(
                    generation=sync.generation,
                    oldest_retained=sync.log.oldest_retained,
                    last_seq=sync.log.last_seq,
                    after=after,
                )
            return EventsPage(
                generation=sync.generation,
                events=page.events,
                next_cursor=page.next_cursor,
                oldest_retained=sync.log.oldest_retained,
                resync_required=False,
            )

    # -- API write transactions ---------------------------------------------

    def _begin_transaction_locked(self) -> None:
        self.lock.acquire()
        try:
            self._scan_if_unloaded_locked()
        except BaseException:
            self.lock.release()
            raise

    def _indexed_state_locked(self, path: str) -> index.FileState | None:
        return self._index.files.get(path)

    def _replay_idempotent_locked(
        self, key: str, *, now_ns: int
    ) -> tuple[int, dict[str, object]] | None:
        entry = self._sync_state.idempotency.get(key)
        if not isinstance(entry, dict):
            return None
        at_ns = entry.get("at_ns")
        status = entry.get("status")
        body = entry.get("body")
        if isinstance(at_ns, bool) or not isinstance(at_ns, int):
            return None
        if now_ns - at_ns > IDEMPOTENCY_RETENTION_NS:
            return None
        if isinstance(status, bool) or not isinstance(status, int):
            return None
        if not isinstance(body, dict):
            return None
        return status, {str(key_name): value for key_name, value in body.items()}

    def _remember_idempotent_locked(
        self, key: str, status: int, body: Mapping[str, object], *, now_ns: int
    ) -> None:
        retained: dict[str, object] = {}
        for existing_key, raw_entry in self._sync_state.idempotency.items():
            if not isinstance(raw_entry, dict):
                continue
            at_ns = raw_entry.get("at_ns")
            if isinstance(at_ns, bool) or not isinstance(at_ns, int):
                continue
            if now_ns - at_ns > IDEMPOTENCY_RETENTION_NS:
                continue
            retained[str(existing_key)] = raw_entry
        retained[key] = {"at_ns": now_ns, "status": status, "body": dict(body)}
        while len(retained) > IDEMPOTENCY_MAX_KEYS:
            expired = min(retained, key=lambda name: _idempotency_at_ns(retained[name]))
            del retained[expired]
        self._sync_state.idempotency = retained
        self._dirty = True

    def _record_write_locked(
        self,
        *,
        path: str,
        sha256: str,
        size: int,
        mtime_ns: int,
        mode: int,
        dev: int,
        ino: int,
        origin: str,
        task_id: str | None,
        previous_revision_id: str | None,
        created: bool,
        observed_at: str,
    ) -> RevisionRecord:
        revision_id = events.revision_id_for(self.workspace_id, path, sha256)
        state = index.FileState(
            path=path,
            revision_id=revision_id,
            sha256=sha256,
            size=size,
            mtime_ns=mtime_ns,
            mode=mode,
            origin=origin,
            observed_at=observed_at,
            policy_class=index.policy_class_for(path),
            task_id=task_id,
        )
        self._index.files[path] = state
        self._pending.pop(path, None)
        self._pending_deletions.pop(path, None)
        self._inodes[path] = (dev, ino)
        kind = events.EventKind.FILE_CREATED if created else events.EventKind.FILE_MODIFIED
        self._sync_state.log.append(
            event_id=events.event_id_for(
                self.workspace_id, path, previous_revision_id, revision_id, kind
            ),
            kind=kind,
            path=path,
            observed_at=observed_at,
            policy_class=state.policy_class,
            revision_id=revision_id,
            previous_revision_id=previous_revision_id,
            sha256=sha256,
            size=size,
            origin=origin,
            task_id=task_id,
        )
        self._dirty = True
        return RevisionRecord(
            revision_id=revision_id,
            path=path,
            sha256=sha256,
            size=size,
            observed_at=observed_at,
            origin=origin,
            task_id=task_id,
            supersedes_revision_id=previous_revision_id,
        )

    def _record_conflict_locked(
        self,
        *,
        path: str,
        proposed_sha256: str,
        proposed_size: int,
        current_sha256: str | None,
        conflict_id: str,
        conflict_path: str,
        origin: str,
        task_id: str | None,
        observed_at: str,
    ) -> None:
        state = self._index.files.get(path)
        if current_sha256 is not None:
            # The live bytes are authoritative for the transition, even when the
            # index has not observed a human edit yet.
            previous_revision_id = events.revision_id_for(self.workspace_id, path, current_sha256)
        elif state is not None:
            previous_revision_id = state.revision_id
        else:
            previous_revision_id = None
        revision_id = events.revision_id_for(self.workspace_id, path, proposed_sha256)
        event_id = events.event_id_for(
            self.workspace_id,
            path,
            previous_revision_id,
            revision_id,
            events.EventKind.CONFLICT_CREATED,
        )
        # A second key proposing the same bytes against the same current revision
        # reuses the conflict file; the event is appended once.
        if not any(existing.event_id == event_id for existing in self._sync_state.log.retained):
            self._sync_state.log.append(
                event_id=event_id,
                kind=events.EventKind.CONFLICT_CREATED,
                path=path,
                observed_at=observed_at,
                policy_class=index.policy_class_for(path),
                revision_id=revision_id,
                previous_revision_id=previous_revision_id,
                sha256=proposed_sha256,
                size=proposed_size,
                origin=origin,
                task_id=task_id,
            )
        entry: dict[str, object] = {
            "conflict_id": conflict_id,
            "path": path,
            "conflict_path": conflict_path,
            "current_sha256": current_sha256,
            "proposed_sha256": proposed_sha256,
            "origin": origin,
            "task_id": task_id,
            "observed_at": observed_at,
        }
        known = any(
            existing.get("conflict_id") == conflict_id for existing in self._sync_state.conflicts
        )
        if not known:
            self._sync_state.conflicts.append(entry)
        self._dirty = True

    def _known_revisions_locked(self, path: str, *, limit: int) -> tuple[RevisionRecord, ...]:
        seen: set[str] = set()
        records: list[RevisionRecord] = []
        for event in reversed(self._sync_state.log.retained):
            if event.path != path or event.revision_id is None:
                continue
            if event.sha256 is None or event.size is None:
                continue
            if event.revision_id in seen:
                continue
            seen.add(event.revision_id)
            records.append(
                RevisionRecord(
                    revision_id=event.revision_id,
                    path=event.path,
                    sha256=event.sha256,
                    size=event.size,
                    observed_at=event.observed_at,
                    origin=event.origin,
                    task_id=event.task_id,
                    supersedes_revision_id=event.previous_revision_id,
                )
            )
            if len(records) == limit:
                return tuple(records)
        state = self._index.files.get(path)
        if (
            state is not None
            and state.revision_id is not None
            and state.sha256 is not None
            and state.revision_id not in seen
        ):
            records.insert(
                0,
                RevisionRecord(
                    revision_id=state.revision_id,
                    path=state.path,
                    sha256=state.sha256,
                    size=state.size,
                    observed_at=state.observed_at,
                    origin=state.origin,
                    task_id=state.task_id,
                    supersedes_revision_id=None,
                ),
            )
        return tuple(records[:limit])

    def _flush_locked(self) -> None:
        workspace_fd = self._open_workspace_directory()
        try:
            autowork_fd = self._open_autowork_directory(workspace_fd)
            try:
                self._write_projections(autowork_fd)
            finally:
                os.close(autowork_fd)
        finally:
            os.close(workspace_fd)

    @property
    def _index(self) -> index.FileIndex:
        assert self._file_index is not None
        return self._file_index

    @property
    def _sync_state(self) -> SyncState:
        assert self._sync is not None
        return self._sync

    # -- scan ----------------------------------------------------------------

    def _scan_if_unloaded_locked(self) -> None:
        if not self._loaded:
            self._scan_locked(full=False)

    def _scan_locked(self, *, full: bool) -> ScanResult:
        workspace_fd = self._open_workspace_directory()
        try:
            self._refresh_layout(workspace_fd)
            autowork_fd = self._open_autowork_directory(workspace_fd)
            try:
                self._load_locked(autowork_fd)
                walk = self._walk(workspace_fd, full=full)
                observed_at = format_rfc3339_utc(self._clock.now())
                emitted = self._reconcile(walk, observed_at=observed_at)
                self._write_projections(autowork_fd)
            finally:
                os.close(autowork_fd)
        finally:
            os.close(workspace_fd)
        policy_violations = sum(
            1 for event in emitted if event.kind is events.EventKind.POLICY_VIOLATION
        )
        return ScanResult(
            generation=self._index.generation,
            latest_cursor=self._sync_state.log.last_seq,
            emitted=len(emitted),
            policy_violations=policy_violations,
        )

    def _open_workspace_directory(self) -> int:
        """Pin the root, then open ``<delivery>/<article>`` with dirfds only."""
        descriptor = self._workspaces.get_workspace(self.workspace_id)
        delivery_code = _manifest_text(descriptor.manifest, "delivery_code")
        article_slug = _manifest_text(descriptor.manifest, "article_slug")
        try:
            root_fd = paths.open_pinned_root(self._root)
        except paths.PathResolutionError as exc:
            raise workspace.WorkspaceNotFoundError(self.workspace_id) from exc
        try:
            return paths.open_directory_chain(root_fd, (delivery_code, article_slug))
        except paths.ConcurrentChangeError:
            raise
        except paths.PathResolutionError as exc:
            raise workspace.WorkspaceNotFoundError(self.workspace_id) from exc
        finally:
            os.close(root_fd)

    def _refresh_layout(self, workspace_fd: int) -> None:
        for components, mode in workspace.LAYOUT_DIRECTORIES:
            try:
                fd = paths.open_directory_chain(workspace_fd, components, create=True, mode=mode)
            except paths.ConcurrentChangeError:
                raise
            except paths.WorkspacePathError as exc:
                raise workspace.WorkspaceStorageError(
                    "workspace layout cannot be refreshed"
                ) from exc
            os.close(fd)

    @staticmethod
    def _open_autowork_directory(workspace_fd: int) -> int:
        try:
            return paths.open_directory_chain(
                workspace_fd,
                (paths.AGENT_INTERNAL_DIRECTORY,),
                create=True,
                mode=workspace.PRIVATE_DIRECTORY_MODE,
            )
        except paths.ConcurrentChangeError:
            raise
        except paths.WorkspacePathError as exc:
            raise workspace.WorkspaceStorageError(
                "agent metadata directory cannot be prepared"
            ) from exc

    # -- projections ---------------------------------------------------------

    def _load_locked(self, autowork_fd: int) -> None:
        if self._loaded:
            return
        index_payload = self._read_projection(autowork_fd, index.FILE_INDEX_FILE_NAME)
        sync_payload = self._read_projection(autowork_fd, events.SYNC_STATE_FILE_NAME)
        loaded_index: index.FileIndex | None = None
        loaded_sync: SyncState | None = None
        if index_payload is not None and sync_payload is not None:
            try:
                candidate_index = index.FileIndex.from_payload(
                    index_payload, workspace_id=self.workspace_id
                )
                candidate_sync = events.SyncState.from_payload(
                    sync_payload,
                    workspace_id=self.workspace_id,
                    limit=self._retained_event_limit,
                )
            except events.ProjectionError:
                pass
            else:
                if candidate_index.generation == candidate_sync.generation:
                    loaded_index, loaded_sync = candidate_index, candidate_sync
        if loaded_index is None or loaded_sync is None:
            generation = self._mint_generation(index_payload, sync_payload)
            loaded_index = index.FileIndex.empty(self.workspace_id, generation)
            loaded_sync = events.SyncState.fresh(
                self.workspace_id, generation, limit=self._retained_event_limit
            )
            self._dirty = True
        self._file_index = loaded_index
        self._sync = loaded_sync
        self._violations, self._foreign_violations = _parse_violations(
            loaded_sync.policy_violations
        )
        self._loaded = True

    def _mint_generation(
        self,
        index_payload: Mapping[str, object] | None,
        sync_payload: Mapping[str, object] | None,
    ) -> int:
        previous: list[int] = []
        for payload in (index_payload, sync_payload):
            if payload is None:
                continue
            value = payload.get("generation")
            if isinstance(value, int) and not isinstance(value, bool) and value >= 1:
                previous.append(value)
        return max(self._clock.wall_ns(), max(previous, default=0) + 1, 1)

    @staticmethod
    def _read_projection(autowork_fd: int, name: str) -> dict[str, object] | None:
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
        try:
            fd = os.open(name, flags, dir_fd=autowork_fd)
        except OSError:
            return None
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                return None
            raw = _read_bounded(fd, MAX_PROJECTION_BYTES)
        except OSError:
            return None
        finally:
            os.close(fd)
        if len(raw) > MAX_PROJECTION_BYTES:
            return None
        try:
            value = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        if not isinstance(value, dict):
            return None
        return {str(key): item for key, item in value.items()}

    def _write_projections(self, autowork_fd: int) -> None:
        if (
            not self._dirty
            and not _projection_missing(autowork_fd, index.FILE_INDEX_FILE_NAME)
            and not _projection_missing(autowork_fd, events.SYNC_STATE_FILE_NAME)
        ):
            return
        self._sync_state.policy_violations = [
            self._violations[path].to_payload() for path in sorted(self._violations)
        ] + [dict(entry) for entry in self._foreign_violations]
        try:
            index.write_json_projection(
                autowork_fd, index.FILE_INDEX_FILE_NAME, self._index.to_payload()
            )
            index.write_json_projection(
                autowork_fd, events.SYNC_STATE_FILE_NAME, self._sync_state.to_payload()
            )
        except index.ProjectionWriteError as exc:
            raise workspace.WorkspaceStorageError(
                "workspace projections cannot be published"
            ) from exc
        self._dirty = False

    # -- walking -------------------------------------------------------------

    def _walk(self, workspace_fd: int, *, full: bool) -> _WalkResult:
        result = _WalkResult()
        self._walk_directory(workspace_fd, (), result=result, base_samples=False, full=full)
        return result

    def _walk_directory(
        self,
        directory_fd: int,
        components: tuple[str, ...],
        *,
        result: _WalkResult,
        base_samples: bool,
        full: bool,
    ) -> None:
        try:
            names = sorted(os.listdir(directory_fd))
        except OSError:
            result.complete = False
            return
        for name in names:
            if name.startswith(TEMP_FILE_PREFIX) and name.endswith(TEMP_FILE_SUFFIX):
                continue
            if not components and name == paths.AGENT_INTERNAL_DIRECTORY:
                continue
            try:
                paths.validate_component(name)
            except paths.PathValidationError:
                continue  # unaddressable through the API; never indexed
            relative_path = f"{'/'.join(components)}/{name}" if components else name
            try:
                entry = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            except OSError:
                continue
            if stat.S_ISLNK(entry.st_mode):
                result.symlinks[relative_path] = _SymlinkObservation(
                    path=relative_path,
                    size=entry.st_size,
                    mtime_ns=entry.st_mtime_ns,
                    mode=stat.S_IMODE(entry.st_mode),
                    policy_class=index.policy_class_for(relative_path),
                )
                continue
            if stat.S_ISDIR(entry.st_mode):
                child_base_samples = base_samples or (
                    not components and name == BASE_SAMPLES_DIRECTORY
                )
                try:
                    child_fd = paths.open_directory_chain(directory_fd, (name,))
                except (paths.WorkspacePathError, OSError):
                    result.complete = False
                    continue
                try:
                    self._walk_directory(
                        child_fd,
                        (*components, name),
                        result=result,
                        base_samples=child_base_samples,
                        full=full,
                    )
                finally:
                    os.close(child_fd)
                continue
            if not stat.S_ISREG(entry.st_mode):
                continue
            policy_class = index.policy_class_for(relative_path)
            if base_samples:
                if entry.st_mode & EXECUTABLE_BITS:
                    result.base_sample_violations[relative_path] = _ModeObservation(
                        path=relative_path,
                        size=entry.st_size,
                        mtime_ns=entry.st_mtime_ns,
                        mode=stat.S_IMODE(entry.st_mode),
                        policy_class=policy_class,
                    )
                continue
            self._observe_file(
                directory_fd,
                name,
                relative_path,
                entry,
                policy_class,
                result=result,
                full=full,
            )

    def _observe_file(
        self,
        parent_fd: int,
        name: str,
        relative_path: str,
        entry: os.stat_result,
        policy_class: str,
        *,
        result: _WalkResult,
        full: bool,
    ) -> None:
        signature: Signature = (entry.st_dev, entry.st_ino, entry.st_size, entry.st_mtime_ns)
        metadata_only = entry.st_size > METADATA_ONLY_THRESHOLD_BYTES
        previous = self._index.files.get(relative_path)
        known_inode = self._inodes.get(relative_path)
        inode_changed = known_inode is not None and known_inode != (entry.st_dev, entry.st_ino)
        now = self._clock.monotonic()
        pending = self._pending.get(relative_path)
        pending_same = pending is not None and pending.signature == signature
        # A pending candidate already differs from the index; never shortcut it
        # back to "unchanged" through the cached content hash.
        content_reused = (
            not pending_same
            and not inode_changed
            and previous is not None
            and previous.size == entry.st_size
            and previous.mtime_ns == entry.st_mtime_ns
            and (metadata_only or (previous.sha256 is not None and not full))
        )
        if content_reused and previous is not None:
            self._pending.pop(relative_path, None)
            result.files[relative_path] = _ObservedFile(
                path=relative_path,
                dev=entry.st_dev,
                ino=entry.st_ino,
                size=entry.st_size,
                mtime_ns=entry.st_mtime_ns,
                mode=stat.S_IMODE(entry.st_mode),
                policy_class=policy_class,
                sha256=previous.sha256,
                metadata_only=metadata_only,
                ready=True,
                unchanged=True,
            )
            return

        if not pending_same:
            self._pending[relative_path] = _PendingObservation(signature, now)
            if self._stability_window > 0.0:
                result.files[relative_path] = _pending_observation(
                    entry, relative_path, policy_class, metadata_only
                )
                return
        elif pending is not None and now - pending.started < self._stability_window:
            result.files[relative_path] = _pending_observation(
                entry, relative_path, policy_class, metadata_only
            )
            return

        verified, sha256 = _stable_content(parent_fd, name, signature, metadata_only=metadata_only)
        if not verified:
            self._pending[relative_path] = _PendingObservation(signature, now)
            result.files[relative_path] = _pending_observation(
                entry, relative_path, policy_class, metadata_only
            )
            return
        self._pending.pop(relative_path, None)
        result.files[relative_path] = _ObservedFile(
            path=relative_path,
            dev=entry.st_dev,
            ino=entry.st_ino,
            size=entry.st_size,
            mtime_ns=entry.st_mtime_ns,
            mode=stat.S_IMODE(entry.st_mode),
            policy_class=policy_class,
            sha256=sha256,
            metadata_only=metadata_only,
            ready=True,
            unchanged=False,
        )

    # -- reconciliation ------------------------------------------------------

    def _reconcile(self, walk: _WalkResult, *, observed_at: str) -> list[FileEvent]:
        previous = dict(self._index.files)
        current = walk.files
        now = self._clock.monotonic()
        window = self._stability_window
        specs: list[_EventSpec] = []
        replacements: dict[str, index.FileState] = {}
        deleted: set[str] = set()
        active_violations: set[str] = set()
        changed = False

        removed = {path for path in previous if path not in current} if walk.complete else set()
        rename_pairs = self._plan_renames(previous, current, removed)
        renamed_targets = set(rename_pairs.values())
        renamed_sources = set(rename_pairs)
        target_to_source = {target: source for source, target in rename_pairs.items()}

        for path in sorted(current):
            observation = current[path]
            old = previous.get(path)
            if not observation.ready:
                continue
            if path in renamed_targets:
                old_path = target_to_source[path]
                old_entry = previous[old_path]
                revision_id = _revision_for(self.workspace_id, path, observation.sha256)
                specs.append(
                    _EventSpec(
                        kind=events.EventKind.FILE_RENAMED,
                        path=path,
                        previous_path=old_path,
                        revision_id=revision_id,
                        previous_revision_id=old_entry.revision_id,
                        sha256=observation.sha256,
                        size=observation.size,
                        policy_class=observation.policy_class,
                    )
                )
                replacements[path] = _state_for(self.workspace_id, observation, observed_at)
                changed = True
                continue

            generated_modified = (
                old is not None
                and observation.policy_class in POLICY_GENERATED
                and old.policy_class in POLICY_GENERATED
                and old.origin != index.ORIGIN_FILESYSTEM
                and not observation.unchanged
            )
            executable = (
                observation.policy_class in POLICY_GENERATED
                and (observation.mode & EXECUTABLE_BITS) != 0
            )
            violation_reason: str | None = None
            if generated_modified:
                violation_reason = VIOLATION_GENERATED_MODIFIED
            elif executable:
                violation_reason = VIOLATION_EXECUTABLE

            if old is None:
                revision_id = _revision_for(self.workspace_id, path, observation.sha256)
                replacements[path] = _state_for(self.workspace_id, observation, observed_at)
                changed = True
                if violation_reason is None:
                    specs.append(
                        _EventSpec(
                            kind=events.EventKind.FILE_CREATED,
                            path=path,
                            revision_id=revision_id,
                            sha256=observation.sha256,
                            size=observation.size,
                            policy_class=observation.policy_class,
                        )
                    )
                elif _record_violation(
                    self._violations,
                    path,
                    observation.policy_class,
                    violation_reason,
                    _signature_text(observation.mode, observation.size, observation.mtime_ns),
                    observed_at,
                ):
                    specs.append(
                        _EventSpec(
                            kind=events.EventKind.POLICY_VIOLATION,
                            path=path,
                            revision_id=revision_id,
                            sha256=observation.sha256,
                            size=observation.size,
                            policy_class=observation.policy_class,
                        )
                    )
            elif observation.unchanged:
                if observation.mode != old.mode:
                    replacements[path] = _replace_mode(old, observation.mode, observed_at)
                    changed = True
                if violation_reason is not None and _record_violation(
                    self._violations,
                    path,
                    observation.policy_class,
                    violation_reason,
                    _signature_text(observation.mode, observation.size, observation.mtime_ns),
                    observed_at,
                ):
                    specs.append(
                        _EventSpec(
                            kind=events.EventKind.POLICY_VIOLATION,
                            path=path,
                            revision_id=old.revision_id,
                            sha256=observation.sha256,
                            size=observation.size,
                            policy_class=observation.policy_class,
                        )
                    )
                    changed = True
            else:
                content_changed = observation.metadata_only or observation.sha256 != old.sha256
                revision_id = (
                    _revision_for(self.workspace_id, path, observation.sha256)
                    if content_changed
                    else old.revision_id
                )
                if content_changed:
                    replacements[path] = _state_for(self.workspace_id, observation, observed_at)
                elif observation.mode != old.mode:
                    replacements[path] = _replace_mode(old, observation.mode, observed_at)
                else:
                    replacements[path] = _refresh_metadata(old, observation, observed_at)
                changed = True
                if content_changed and violation_reason is None:
                    specs.append(
                        _EventSpec(
                            kind=events.EventKind.FILE_MODIFIED,
                            path=path,
                            revision_id=revision_id,
                            previous_revision_id=old.revision_id,
                            sha256=observation.sha256,
                            size=observation.size,
                            policy_class=observation.policy_class,
                        )
                    )
                elif violation_reason is not None and _record_violation(
                    self._violations,
                    path,
                    observation.policy_class,
                    violation_reason,
                    _signature_text(observation.mode, observation.size, observation.mtime_ns),
                    observed_at,
                ):
                    specs.append(
                        _EventSpec(
                            kind=events.EventKind.POLICY_VIOLATION,
                            path=path,
                            revision_id=revision_id,
                            previous_revision_id=old.revision_id if content_changed else None,
                            sha256=observation.sha256,
                            size=observation.size,
                            policy_class=observation.policy_class,
                        )
                    )
                    changed = True
            if violation_reason is not None:
                active_violations.add(path)

        # symbolic links: reported, never followed (and never indexed)
        for path in sorted(walk.symlinks):
            link = walk.symlinks[path]
            active_violations.add(path)
            if _record_violation(
                self._violations,
                path,
                link.policy_class,
                VIOLATION_SYMLINK,
                _signature_text(link.mode, link.size, link.mtime_ns),
                observed_at,
            ):
                existing = previous.get(path)
                specs.append(
                    _EventSpec(
                        kind=events.EventKind.POLICY_VIOLATION,
                        path=path,
                        previous_revision_id=existing.revision_id if existing is not None else None,
                        policy_class=link.policy_class,
                    )
                )
                changed = True

        # base_samples content is registered at materialization; only modes are policed
        for path in sorted(walk.base_sample_violations):
            sample = walk.base_sample_violations[path]
            active_violations.add(path)
            if _record_violation(
                self._violations,
                path,
                sample.policy_class,
                VIOLATION_EXECUTABLE,
                _signature_text(sample.mode, sample.size, sample.mtime_ns),
                observed_at,
            ):
                specs.append(
                    _EventSpec(
                        kind=events.EventKind.POLICY_VIOLATION,
                        path=path,
                        size=sample.size,
                        policy_class=sample.policy_class,
                    )
                )
                changed = True

        # deletions become visible only once the absence is old enough
        if walk.complete:
            for path in sorted(removed - renamed_sources):
                started = self._pending_deletions.get(path)
                if started is None:
                    self._pending_deletions[path] = now
                    continue
                if now - started < window:
                    continue
                old_entry = previous[path]
                specs.append(
                    _EventSpec(
                        kind=events.EventKind.FILE_DELETED,
                        path=path,
                        previous_revision_id=old_entry.revision_id,
                        policy_class=old_entry.policy_class,
                    )
                )
                deleted.add(path)
                self._pending_deletions.pop(path, None)
                changed = True

        if changed:
            files = dict(previous)
            for path in renamed_sources | deleted:
                files.pop(path, None)
            for path, state in replacements.items():
                files[path] = state
            self._index.files = files
            self._dirty = True

        # bookkeeping for the next scan; remember the inode of an indexed file
        # that just disappeared until its deletion or rename is decided
        next_inodes = {
            path: (observation.dev, observation.ino) for path, observation in current.items()
        }
        for path, inode in self._inodes.items():
            if path in self._index.files and path not in next_inodes:
                next_inodes[path] = inode
        self._inodes = next_inodes
        if walk.complete:
            self._pending = {
                path: pending for path, pending in self._pending.items() if path in current
            }
            self._pending_deletions = {
                path: started
                for path, started in self._pending_deletions.items()
                if path in removed and path not in renamed_sources and path not in deleted
            }
            stale_violations = set(self._violations) - active_violations
            for path in stale_violations:
                del self._violations[path]
            if stale_violations:
                self._dirty = True

        emitted: list[FileEvent] = []
        for spec in sorted(specs, key=lambda item: item.sort_key()):
            event_id = events.event_id_for(
                self.workspace_id,
                spec.path,
                spec.previous_revision_id,
                spec.revision_id,
                spec.kind,
            )
            emitted.append(
                self._sync_state.log.append(
                    event_id=event_id,
                    kind=spec.kind,
                    path=spec.path,
                    observed_at=observed_at,
                    policy_class=spec.policy_class,
                    previous_path=spec.previous_path,
                    revision_id=spec.revision_id,
                    previous_revision_id=spec.previous_revision_id,
                    sha256=spec.sha256,
                    size=spec.size,
                    origin=events.ORIGIN_FILESYSTEM,
                )
            )
            self._dirty = True
        return emitted

    def _plan_renames(
        self,
        previous: Mapping[str, index.FileState],
        current: Mapping[str, _ObservedFile],
        removed: set[str],
    ) -> dict[str, str]:
        """Best-effort ``old path -> new path`` pairing on the remembered inode."""
        inode_to_path: dict[tuple[int, int], str] = {}
        for path, observation in current.items():
            inode_to_path.setdefault((observation.dev, observation.ino), path)
        pairs: dict[str, str] = {}
        used_targets: set[str] = set()
        for old_path in sorted(removed):
            old_inode = self._inodes.get(old_path)
            if old_inode is None:
                continue
            target = inode_to_path.get(old_inode)
            if target is None or target in used_targets or target in previous:
                continue
            observation = current[target]
            if not observation.ready:
                continue
            used_targets.add(target)
            pairs[old_path] = target
        return pairs


class WorkspaceWriteTransaction:
    """One API write holding the per-workspace lock (design §6-§7).

    The caller performs its CAS decision and file publication while the lock is
    held, records the revision/conflict through this object, and flushes the
    projections before it responds. All ``*_locked`` scanner calls below assume
    the lock acquired by ``__enter__``.
    """

    def __init__(self, scanner: _WorkspaceScanner) -> None:
        self._scanner = scanner

    def __enter__(self) -> WorkspaceWriteTransaction:
        self._scanner._begin_transaction_locked()
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self._scanner.lock.release()

    def wall_ns(self) -> int:
        return self._scanner._clock.wall_ns()

    def observed_at(self) -> str:
        return format_rfc3339_utc(self._scanner._clock.now())

    def replay(self, key: str, *, now_ns: int) -> tuple[int, dict[str, object]] | None:
        return self._scanner._replay_idempotent_locked(key, now_ns=now_ns)

    def indexed_state(self, path: str) -> index.FileState | None:
        return self._scanner._indexed_state_locked(path)

    def record(
        self,
        *,
        path: str,
        sha256: str,
        size: int,
        mtime_ns: int,
        mode: int,
        dev: int,
        ino: int,
        origin: str,
        task_id: str | None,
        previous_revision_id: str | None,
        created: bool,
        observed_at: str,
    ) -> RevisionRecord:
        return self._scanner._record_write_locked(
            path=path,
            sha256=sha256,
            size=size,
            mtime_ns=mtime_ns,
            mode=mode,
            dev=dev,
            ino=ino,
            origin=origin,
            task_id=task_id,
            previous_revision_id=previous_revision_id,
            created=created,
            observed_at=observed_at,
        )

    def record_conflict(
        self,
        *,
        path: str,
        proposed_sha256: str,
        proposed_size: int,
        current_sha256: str | None,
        conflict_id: str,
        conflict_path: str,
        origin: str,
        task_id: str | None,
        observed_at: str,
    ) -> None:
        self._scanner._record_conflict_locked(
            path=path,
            proposed_sha256=proposed_sha256,
            proposed_size=proposed_size,
            current_sha256=current_sha256,
            conflict_id=conflict_id,
            conflict_path=conflict_path,
            origin=origin,
            task_id=task_id,
            observed_at=observed_at,
        )

    def remember(
        self,
        key: str,
        status: int,
        body: Mapping[str, object],
        *,
        now_ns: int,
    ) -> None:
        self._scanner._remember_idempotent_locked(key, status, body, now_ns=now_ns)

    def flush(self) -> None:
        self._scanner._flush_locked()


def _idempotency_at_ns(entry: object) -> int:
    if isinstance(entry, dict):
        value = entry.get("at_ns")
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return 0


def _manifest_text(manifest: Mapping[str, object], field_name: str) -> str:
    value = manifest.get(field_name)
    if not isinstance(value, str):
        raise workspace.WorkspaceStorageError("workspace manifest is incomplete")
    return value


def _projection_missing(autowork_fd: int, name: str) -> bool:
    try:
        os.stat(name, dir_fd=autowork_fd, follow_symlinks=False)
    except OSError:
        return True
    return False


def _pending_observation(
    entry: os.stat_result, relative_path: str, policy_class: str, metadata_only: bool
) -> _ObservedFile:
    return _ObservedFile(
        path=relative_path,
        dev=entry.st_dev,
        ino=entry.st_ino,
        size=entry.st_size,
        mtime_ns=entry.st_mtime_ns,
        mode=stat.S_IMODE(entry.st_mode),
        policy_class=policy_class,
        sha256=None,
        metadata_only=metadata_only,
        ready=False,
        unchanged=False,
    )


def _stable_content(
    parent_fd: int, name: str, signature: Signature, *, metadata_only: bool
) -> tuple[bool, str | None]:
    """Re-open, verify the signature, hash small files and re-check drift."""
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
    try:
        fd = os.open(name, flags, dir_fd=parent_fd)
    except OSError:
        return False, None
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            return False, None
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != signature:
            return False, None
        sha256: str | None = None
        if not metadata_only:
            digest = hashlib.sha256()
            while True:
                chunk = os.read(fd, HASH_CHUNK_BYTES)
                if not chunk:
                    break
                digest.update(chunk)
            after = os.fstat(fd)
            if (after.st_size, after.st_mtime_ns) != (before.st_size, before.st_mtime_ns):
                return False, None
            sha256 = digest.hexdigest()
    except OSError:
        return False, None
    finally:
        os.close(fd)
    try:
        entry = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except OSError:
        return False, None
    if (entry.st_dev, entry.st_ino) != (before.st_dev, before.st_ino):
        return False, None
    return True, sha256


def _revision_for(workspace_id: str, path: str, sha256: str | None) -> str | None:
    if sha256 is None:
        return None
    return events.revision_id_for(workspace_id, path, sha256)


def _state_for(workspace_id: str, observation: _ObservedFile, observed_at: str) -> index.FileState:
    return index.FileState(
        path=observation.path,
        revision_id=_revision_for(workspace_id, observation.path, observation.sha256),
        sha256=observation.sha256,
        size=observation.size,
        mtime_ns=observation.mtime_ns,
        mode=observation.mode,
        origin=index.ORIGIN_FILESYSTEM,
        observed_at=observed_at,
        policy_class=observation.policy_class,
        task_id=None,
    )


def _replace_mode(state: index.FileState, mode: int, observed_at: str) -> index.FileState:
    return index.FileState(
        path=state.path,
        revision_id=state.revision_id,
        sha256=state.sha256,
        size=state.size,
        mtime_ns=state.mtime_ns,
        mode=mode,
        origin=state.origin,
        observed_at=observed_at,
        policy_class=state.policy_class,
        task_id=state.task_id,
    )


def _refresh_metadata(
    state: index.FileState, observation: _ObservedFile, observed_at: str
) -> index.FileState:
    return index.FileState(
        path=state.path,
        revision_id=state.revision_id,
        sha256=state.sha256,
        size=observation.size,
        mtime_ns=observation.mtime_ns,
        mode=observation.mode,
        origin=state.origin,
        observed_at=observed_at,
        policy_class=observation.policy_class,
        task_id=state.task_id,
    )


def _signature_text(mode: int, size: int, mtime_ns: int) -> str:
    return f"{mode:04o}:{size}:{mtime_ns}"


def _record_violation(
    violations: dict[str, _Violation],
    path: str,
    policy_class: str,
    reason: str,
    signature: str,
    observed_at: str,
) -> bool:
    existing = violations.get(path)
    if (
        existing is not None
        and existing.reason == reason
        and existing.signature == signature
        and existing.policy_class == policy_class
    ):
        return False
    violations[path] = _Violation(
        path=path,
        policy_class=policy_class,
        reason=reason,
        signature=signature,
        observed_at=observed_at,
    )
    return True


def _parse_violations(
    payloads: list[dict[str, object]],
) -> tuple[dict[str, _Violation], list[dict[str, object]]]:
    violations: dict[str, _Violation] = {}
    foreign: list[dict[str, object]] = []
    for payload in payloads:
        parsed = _violation_from_payload(payload)
        if parsed is None or parsed.path in violations:
            foreign.append(payload)
        else:
            violations[parsed.path] = parsed
    return violations, foreign


def _violation_from_payload(payload: Mapping[str, object]) -> _Violation | None:
    values = (
        payload.get("path"),
        payload.get("policy_class"),
        payload.get("reason"),
        payload.get("signature"),
        payload.get("observed_at"),
    )
    path, policy_class, reason, signature, observed_at = values
    if not (
        isinstance(path, str)
        and isinstance(policy_class, str)
        and isinstance(reason, str)
        and isinstance(signature, str)
        and isinstance(observed_at, str)
    ):
        return None
    return _Violation(
        path=path,
        policy_class=policy_class,
        reason=reason,
        signature=signature,
        observed_at=observed_at,
    )


def _page_limit(limit: int | None) -> int:
    if limit is None:
        return DEFAULT_PAGE_LIMIT
    if limit < 1 or limit > MAX_PAGE_LIMIT:
        raise ScanRequestError("limit is out of range", details={"parameter": "limit"})
    return limit


def _page_token(page_token: str | None) -> str | None:
    """Decode the opaque (hex-encoded UTF-8 path) continuation token."""
    if page_token is None:
        return None
    if len(page_token) > 2 * paths.MAX_PATH_BYTES:
        raise ScanRequestError("page token is too long", details={"parameter": "page_token"})
    try:
        return bytes.fromhex(page_token).decode("utf-8")
    except (ValueError, UnicodeDecodeError) as exc:
        raise ScanRequestError(
            "page token is invalid", details={"parameter": "page_token"}
        ) from exc


def _paginate(
    states: list[index.FileState], token: str | None, limit: int
) -> tuple[tuple[index.FileState, ...], str | None]:
    remaining = [state for state in states if token is None or state.path > token]
    selected = remaining[:limit]
    next_token = (
        selected[-1].path.encode("utf-8").hex() if len(remaining) > limit and selected else None
    )
    return tuple(selected), next_token


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
