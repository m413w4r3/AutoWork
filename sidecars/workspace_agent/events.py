"""Deterministic revisions, file events and the bounded event log (design §6).

Revision and event identifiers are pure functions of their content, so a scan
that reloads the projections re-derives the same ids and never duplicates an
event. The per-workspace log is a ring buffer bounded to
``MAX_RETAINED_EVENTS``; the cursor exposed to the backend is ``seq`` and a
cursor older than the retained window forces a snapshot resynchronisation
(``cursor_expired`` with ``resync_required``).
"""

from __future__ import annotations

import hashlib
from collections import deque
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum

SYNC_STATE_FILE_NAME = "sync-state.json"
SYNC_STATE_SCHEMA = "autowork-workspace-sync.v1"
MAX_RETAINED_EVENTS = 10_000
EVENT_ID_PREFIX = "evt-"
REVISION_ID_PREFIX = "rev-"
ID_DIGEST_LENGTH = 32
NULL_COMPONENT = "null"
ORIGIN_FILESYSTEM = "filesystem"
ORIGINS: frozenset[str] = frozenset({ORIGIN_FILESYSTEM, "ui", "agent", "system"})


class ProjectionError(ValueError):
    """A persisted projection violates a required invariant (rebuild it)."""


class EventKind(StrEnum):
    """Closed event-kind set of the agent protocol (§4)."""

    FILE_CREATED = "file_created"
    FILE_MODIFIED = "file_modified"
    FILE_RENAMED = "file_renamed"
    FILE_DELETED = "file_deleted"
    CONFLICT_CREATED = "conflict_created"
    POLICY_VIOLATION = "policy_violation"
    SAMPLE_MATERIALIZED = "sample_materialized"
    SAMPLE_REJECTED = "sample_rejected"
    SCAN_COMPLETED = "scan_completed"


EVENT_FIELDS: tuple[str, ...] = (
    "event_id",
    "seq",
    "generation",
    "kind",
    "path",
    "previous_path",
    "revision_id",
    "previous_revision_id",
    "sha256",
    "size",
    "origin",
    "task_id",
    "observed_at",
    "policy_class",
)
SYNC_STATE_FIELDS: frozenset[str] = frozenset(
    {
        "schema",
        "workspace_id",
        "generation",
        "last_seq",
        "oldest_seq",
        "events",
        "idempotency",
        "conflicts",
        "policy_violations",
    }
)


def revision_id_for(workspace_id: str, path: str, sha256: str) -> str:
    """``rev-`` id of one ``(workspace, path, content)`` triple (§6)."""
    digest = hashlib.sha256(f"{workspace_id}\x00{path}\x00{sha256}".encode()).hexdigest()
    return REVISION_ID_PREFIX + digest[:ID_DIGEST_LENGTH]


def event_id_for(
    workspace_id: str,
    path: str,
    previous_revision_id: str | None,
    revision_id: str | None,
    kind: EventKind,
) -> str:
    """``evt-`` id of one observed transition; replays are byte-identical (§6)."""
    components = (
        workspace_id,
        path,
        previous_revision_id or NULL_COMPONENT,
        revision_id or NULL_COMPONENT,
        str(kind),
    )
    digest = hashlib.sha256("|".join(components).encode("utf-8")).hexdigest()
    return EVENT_ID_PREFIX + digest[:ID_DIGEST_LENGTH]


@dataclass(frozen=True)
class FileEvent:
    """``EventV1``: one immutable change observation of a workspace file."""

    event_id: str
    seq: int
    generation: int
    kind: EventKind
    path: str
    previous_path: str | None
    revision_id: str | None
    previous_revision_id: str | None
    sha256: str | None
    size: int | None
    origin: str
    task_id: str | None
    observed_at: str
    policy_class: str

    def to_wire(self) -> dict[str, object]:
        return {
            "event_id": self.event_id,
            "seq": self.seq,
            "generation": self.generation,
            "kind": str(self.kind),
            "path": self.path,
            "previous_path": self.previous_path,
            "revision_id": self.revision_id,
            "previous_revision_id": self.previous_revision_id,
            "sha256": self.sha256,
            "size": self.size,
            "origin": self.origin,
            "task_id": self.task_id,
            "observed_at": self.observed_at,
            "policy_class": self.policy_class,
        }


@dataclass(frozen=True)
class EventPage:
    """One ``EventsPageV1`` slice; ``resync_required`` forces a snapshot."""

    events: tuple[FileEvent, ...]
    next_cursor: int
    resync_required: bool


class EventLog:
    """Per-workspace monotonic event sequence with a bounded retained window."""

    def __init__(
        self,
        generation: int,
        *,
        limit: int = MAX_RETAINED_EVENTS,
        last_seq: int = 0,
        retained: Iterable[FileEvent] = (),
    ) -> None:
        if limit < 1:
            raise ValueError("the event log limit must be positive")
        if last_seq < 0:
            raise ValueError("the event sequence must be non-negative")
        self._generation = generation
        self._limit = limit
        self._events: deque[FileEvent] = deque(retained, maxlen=limit)
        self._last_seq = last_seq

    @property
    def generation(self) -> int:
        return self._generation

    @property
    def last_seq(self) -> int:
        return self._last_seq

    @property
    def oldest_retained(self) -> int:
        return self._events[0].seq if self._events else self._last_seq + 1

    @property
    def retained(self) -> tuple[FileEvent, ...]:
        return tuple(self._events)

    def append(
        self,
        *,
        event_id: str,
        kind: EventKind,
        path: str,
        observed_at: str,
        policy_class: str,
        previous_path: str | None = None,
        revision_id: str | None = None,
        previous_revision_id: str | None = None,
        sha256: str | None = None,
        size: int | None = None,
        origin: str = ORIGIN_FILESYSTEM,
        task_id: str | None = None,
    ) -> FileEvent:
        """Append one event, assigning the next per-workspace ``seq``."""
        if origin not in ORIGINS:
            raise ValueError("unknown event origin")
        self._last_seq += 1
        event = FileEvent(
            event_id=event_id,
            seq=self._last_seq,
            generation=self._generation,
            kind=kind,
            path=path,
            previous_path=previous_path,
            revision_id=revision_id,
            previous_revision_id=previous_revision_id,
            sha256=sha256,
            size=size,
            origin=origin,
            task_id=task_id,
            observed_at=observed_at,
            policy_class=policy_class,
        )
        self._events.append(event)
        return event

    def page(self, after: int, limit: int) -> EventPage:
        """Return events with ``seq > after``; expired cursors ask for a resync."""
        if after < 0:
            raise ValueError("the cursor must be non-negative")
        if limit < 1:
            raise ValueError("the page limit must be positive")
        if after > self._last_seq or after < self.oldest_retained - 1:
            return EventPage(events=(), next_cursor=after, resync_required=True)
        selected: list[FileEvent] = []
        for event in self._events:
            if event.seq > after:
                selected.append(event)
                if len(selected) == limit:
                    break
        next_cursor = selected[-1].seq if selected else after
        return EventPage(events=tuple(selected), next_cursor=next_cursor, resync_required=False)


@dataclass
class SyncState:
    """``sync-state.json``: the retained events plus the sections W4 owns."""

    workspace_id: str
    generation: int
    log: EventLog
    idempotency: dict[str, object] = field(default_factory=dict)
    conflicts: list[dict[str, object]] = field(default_factory=list)
    policy_violations: list[dict[str, object]] = field(default_factory=list)

    @classmethod
    def fresh(
        cls, workspace_id: str, generation: int, *, limit: int = MAX_RETAINED_EVENTS
    ) -> SyncState:
        return cls(
            workspace_id=workspace_id,
            generation=generation,
            log=EventLog(generation, limit=limit),
        )

    def to_payload(self) -> dict[str, object]:
        return {
            "schema": SYNC_STATE_SCHEMA,
            "workspace_id": self.workspace_id,
            "generation": self.generation,
            "last_seq": self.log.last_seq,
            "oldest_seq": self.log.oldest_retained,
            "events": [event.to_wire() for event in self.log.retained],
            "idempotency": dict(self.idempotency),
            "conflicts": [dict(entry) for entry in self.conflicts],
            "policy_violations": [dict(entry) for entry in self.policy_violations],
        }

    @classmethod
    def from_payload(
        cls,
        payload: Mapping[str, object],
        *,
        workspace_id: str,
        limit: int = MAX_RETAINED_EVENTS,
    ) -> SyncState:
        """Rebuild one projection; any violated invariant raises ``ProjectionError``."""
        if set(payload) != SYNC_STATE_FIELDS:
            raise ProjectionError("unexpected sync-state fields")
        if payload["schema"] != SYNC_STATE_SCHEMA:
            raise ProjectionError("unexpected sync-state schema")
        stored_workspace = payload["workspace_id"]
        if not isinstance(stored_workspace, str) or stored_workspace != workspace_id:
            raise ProjectionError("sync-state belongs to another workspace")
        generation = _require_int(payload["generation"], "generation", minimum=1)
        last_seq = _require_int(payload["last_seq"], "last_seq", minimum=0)
        oldest_seq = _require_int(payload["oldest_seq"], "oldest_seq", minimum=1)

        raw_events = payload["events"]
        if not isinstance(raw_events, list) or len(raw_events) > limit:
            raise ProjectionError("sync-state event list is invalid")
        retained = tuple(_event_from_payload(entry, generation=generation) for entry in raw_events)
        for position in range(1, len(retained)):
            if retained[position].seq <= retained[position - 1].seq:
                raise ProjectionError("event sequences are not strictly increasing")
        expected_last = retained[-1].seq if retained else 0
        if last_seq != expected_last:
            raise ProjectionError("last_seq does not match the retained events")
        expected_oldest = retained[0].seq if retained else last_seq + 1
        if oldest_seq != expected_oldest:
            raise ProjectionError("oldest_seq does not match the retained events")

        idempotency = payload["idempotency"]
        if not isinstance(idempotency, dict):
            raise ProjectionError("sync-state idempotency section is invalid")
        conflicts = _require_object_list(payload["conflicts"], "conflicts")
        policies = _require_object_list(payload["policy_violations"], "policy_violations")
        return cls(
            workspace_id=workspace_id,
            generation=generation,
            log=EventLog(generation, limit=limit, last_seq=last_seq, retained=retained),
            idempotency={str(key): value for key, value in idempotency.items()},
            conflicts=conflicts,
            policy_violations=policies,
        )


def _event_from_payload(payload: object, *, generation: int) -> FileEvent:
    if not isinstance(payload, dict):
        raise ProjectionError("an event entry is not an object")
    entry = {str(key): value for key, value in payload.items()}
    if set(entry) != set(EVENT_FIELDS):
        raise ProjectionError("unexpected event fields")
    raw_kind = entry["kind"]
    if not isinstance(raw_kind, str):
        raise ProjectionError("event kind is not a string")
    try:
        kind = EventKind(raw_kind)
    except ValueError as exc:
        raise ProjectionError("unknown event kind") from exc
    event_generation = _require_int(entry["generation"], "generation", minimum=1)
    if event_generation != generation:
        raise ProjectionError("event generation does not match the log")
    origin = entry["origin"]
    if not isinstance(origin, str) or origin not in ORIGINS:
        raise ProjectionError("unknown event origin")
    return FileEvent(
        event_id=_require_str(entry["event_id"], "event_id"),
        seq=_require_int(entry["seq"], "seq", minimum=1),
        generation=event_generation,
        kind=kind,
        path=_require_str(entry["path"], "path"),
        previous_path=_optional_str(entry["previous_path"], "previous_path"),
        revision_id=_optional_str(entry["revision_id"], "revision_id"),
        previous_revision_id=_optional_str(entry["previous_revision_id"], "previous_revision_id"),
        sha256=_optional_str(entry["sha256"], "sha256"),
        size=_optional_int(entry["size"], "size"),
        origin=origin,
        task_id=_optional_str(entry["task_id"], "task_id"),
        observed_at=_require_str(entry["observed_at"], "observed_at"),
        policy_class=_require_str(entry["policy_class"], "policy_class"),
    )


def _require_str(value: object, field_name: str) -> str:
    if not isinstance(value, str):
        raise ProjectionError(f"{field_name} is not a string")
    return value


def _optional_str(value: object, field_name: str) -> str | None:
    if value is None:
        return None
    return _require_str(value, field_name)


def _require_int(value: object, field_name: str, *, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ProjectionError(f"{field_name} is not a valid integer")
    return value


def _optional_int(value: object, field_name: str) -> int | None:
    if value is None:
        return None
    return _require_int(value, field_name, minimum=0)


def _require_object_list(value: object, field_name: str) -> list[dict[str, object]]:
    if not isinstance(value, list):
        raise ProjectionError(f"{field_name} is not a list")
    entries: list[dict[str, object]] = []
    for item in value:
        if not isinstance(item, dict):
            raise ProjectionError(f"{field_name} contains a non-object entry")
        entries.append({str(key): item_value for key, item_value in item.items()})
    return entries
