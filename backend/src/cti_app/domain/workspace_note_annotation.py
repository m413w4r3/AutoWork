"""Human note annotations over one workspace file revision (M24-WS2, D6).

A note annotation is a human-labelled relationship between a local workspace
note revision and a work object of the same investigation.  It is *not*
evidence: the row never creates an observation, a finding or a sample
qualification, and a note stays an unverified hypothesis until a canonical
observation is imported through the one import path of ADR-0011 D6.

The aggregate is append-only.  "Mark useful" and "unmark useful" are separate
rows, so the current state of a note is derived from the ordered rows instead
of mutating one.  An author is only ever the explicit attribution a verified
human recorded, never an inference from a folder, a path or an origin field.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID, uuid4

from cti_app.domain.work_contribution import is_relative_workspace_path

WORKSPACE_NOTE_ANNOTATION_SCHEMA_VERSION = "workspace-note-annotation.v1"
MAX_ANNOTATION_DETAIL_CHARS = 4_000
MAX_IOC_VALUE_CHARS = 2_048
MAX_IDEMPOTENCY_KEY_CHARS = 255

_REVISION_ID_PATTERN = re.compile(r"^rev-[0-9a-f]{32}$")
_WORKSPACE_ID_PATTERN = re.compile(r"^ws-[0-9a-f]{32}$")


class NoteAnnotationKind(StrEnum):
    """The closed set of human annotation actions (D6)."""

    LINK_TASK = "link_task"
    LINK_IOC = "link_ioc"
    LINK_SAMPLE = "link_sample"
    MARK_USEFUL = "mark_useful"
    UNMARK_USEFUL = "unmark_useful"
    ATTRIBUTE_AUTHOR = "attribute_author"
    DISMISS = "dismiss"


class NoteIocType(StrEnum):
    """The IOC kinds a note may be linked to (bootstrap inventory vocabulary)."""

    MD5 = "md5"
    SHA1 = "sha1"
    SHA256 = "sha256"
    IPV4 = "ipv4"
    DOMAIN = "domain"
    URL = "url"


class NoteAnnotationActorKind(StrEnum):
    """Only a verified human may annotate a note (D6/D8)."""

    HUMAN = "human"


_DETAIL_REQUIRED_KINDS = frozenset(
    {NoteAnnotationKind.ATTRIBUTE_AUTHOR, NoteAnnotationKind.DISMISS}
)


def is_note_path(value: str) -> bool:
    """A note lives under ``notes/``; the backend never reads it from disk."""
    return (
        isinstance(value, str)
        and value.startswith("notes/")
        and is_relative_workspace_path(value)
        and len(value) <= 4_096
    )


def _uuid(value: object, name: str) -> None:
    if not isinstance(value, UUID):
        raise TypeError(f"{name} must be a UUID")


def _bounded_detail(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    normalized = value.strip()
    if not normalized or "\x00" in normalized:
        raise ValueError(f"{name} must be non-empty and NUL-free")
    if len(normalized) > MAX_ANNOTATION_DETAIL_CHARS:
        raise ValueError(f"{name} exceeds {MAX_ANNOTATION_DETAIL_CHARS} characters")
    return normalized


@dataclass(frozen=True, slots=True, kw_only=True)
class WorkspaceNoteAnnotationV1:
    """One immutable human annotation of one per-file workspace revision."""

    investigation_id: UUID
    workspace_id: str
    revision_id: str
    file_relpath: str
    kind: NoteAnnotationKind
    actor_id: str
    idempotency_key: str
    target_task_id: UUID | None = None
    ioc_value: str | None = None
    ioc_type: NoteIocType | None = None
    target_sample_id: UUID | None = None
    detail: str | None = None
    actor_kind: NoteAnnotationActorKind = NoteAnnotationActorKind.HUMAN
    schema_version: str = WORKSPACE_NOTE_ANNOTATION_SCHEMA_VERSION
    id: UUID = field(default_factory=uuid4)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        _uuid(self.investigation_id, "investigation_id")
        _uuid(self.id, "id")
        if (
            not isinstance(self.workspace_id, str)
            or _WORKSPACE_ID_PATTERN.fullmatch(self.workspace_id) is None
        ):
            raise ValueError("workspace_id must match 'ws-<32 hex characters>'")
        if (
            not isinstance(self.revision_id, str)
            or _REVISION_ID_PATTERN.fullmatch(self.revision_id) is None
        ):
            raise ValueError("revision_id must match 'rev-<32 hex characters>'")
        if not is_note_path(self.file_relpath):
            raise ValueError("a note annotation targets a relative path under 'notes/'")
        if not isinstance(self.kind, NoteAnnotationKind):
            raise TypeError("kind must use NoteAnnotationKind")
        if self.actor_kind is not NoteAnnotationActorKind.HUMAN:
            raise ValueError("only a verified human may annotate a note")
        if not isinstance(self.actor_id, str) or not self.actor_id.strip():
            raise ValueError("actor_id must be a non-empty explicit human identity")
        if "\x00" in self.actor_id or len(self.actor_id) > 255:
            raise ValueError("actor_id must be NUL-free and bounded to 255 characters")
        if not isinstance(self.idempotency_key, str) or not self.idempotency_key.strip():
            raise ValueError("idempotency_key must be non-empty")
        if "\x00" in self.idempotency_key:
            raise ValueError("idempotency_key must be NUL-free")
        if len(self.idempotency_key) > MAX_IDEMPOTENCY_KEY_CHARS:
            raise ValueError("idempotency_key is bounded to 255 characters")
        if self.schema_version != WORKSPACE_NOTE_ANNOTATION_SCHEMA_VERSION:
            raise ValueError(f"schema_version must be {WORKSPACE_NOTE_ANNOTATION_SCHEMA_VERSION}")
        if (
            not isinstance(self.created_at, datetime)
            or self.created_at.tzinfo is None
            or self.created_at.utcoffset() is None
        ):
            raise ValueError("created_at must be a timezone-aware datetime")

        if self.target_task_id is not None:
            _uuid(self.target_task_id, "target_task_id")
        if self.target_sample_id is not None:
            _uuid(self.target_sample_id, "target_sample_id")
        if (self.ioc_value is None) != (self.ioc_type is None):
            raise ValueError("ioc_value and ioc_type are a pair")
        if self.ioc_value is not None:
            if not isinstance(self.ioc_value, str) or not self.ioc_value.strip():
                raise ValueError("ioc_value must be a non-empty string")
            if len(self.ioc_value) > MAX_IOC_VALUE_CHARS:
                raise ValueError("ioc_value is bounded to 2048 characters")
            if "\x00" in self.ioc_value:
                raise ValueError("ioc_value must be NUL-free")
            if not isinstance(self.ioc_type, NoteIocType):
                raise TypeError("ioc_type must use NoteIocType")
        if self.detail is not None:
            object.__setattr__(self, "detail", _bounded_detail(self.detail, "detail"))
        self._validate_targets()

    def _validate_targets(self) -> None:
        kind = self.kind
        if kind is NoteAnnotationKind.LINK_TASK:
            if self.target_task_id is None:
                raise ValueError("link_task requires a task identity")
            if self.ioc_value is not None or self.target_sample_id is not None:
                raise ValueError("link_task carries only a task identity")
            if self.detail is not None:
                raise ValueError("link_task carries no free-form detail")
            return
        if kind is NoteAnnotationKind.LINK_IOC:
            if self.ioc_value is None:
                raise ValueError("link_ioc requires an IOC value and type")
            if self.target_task_id is not None or self.target_sample_id is not None:
                raise ValueError("link_ioc carries only an IOC target")
            if self.detail is not None:
                raise ValueError("link_ioc carries no free-form detail")
            return
        if kind is NoteAnnotationKind.LINK_SAMPLE:
            if self.target_sample_id is None:
                raise ValueError("link_sample requires a sample identity")
            if self.ioc_value is not None or self.target_task_id is not None:
                raise ValueError("link_sample carries only a sample identity")
            if self.detail is not None:
                raise ValueError("link_sample carries no free-form detail")
            return
        if any(
            item is not None
            for item in (self.target_task_id, self.ioc_value, self.target_sample_id)
        ):
            raise ValueError(f"{kind.value} carries no target reference")
        if kind in _DETAIL_REQUIRED_KINDS:
            if self.detail is None:
                raise ValueError(f"{kind.value} requires an explicit human reason or author")
        elif self.detail is not None:
            raise ValueError(f"{kind.value} carries no free-form detail")

    def to_payload(self) -> dict[str, object]:
        return {
            "id": str(self.id),
            "investigation_id": str(self.investigation_id),
            "workspace_id": self.workspace_id,
            "revision_id": self.revision_id,
            "file_relpath": self.file_relpath,
            "kind": self.kind.value,
            "actor_kind": self.actor_kind.value,
            "actor_id": self.actor_id,
            "target_task_id": (
                str(self.target_task_id) if self.target_task_id is not None else None
            ),
            "ioc_value": self.ioc_value,
            "ioc_type": self.ioc_type.value if self.ioc_type is not None else None,
            "target_sample_id": (
                str(self.target_sample_id) if self.target_sample_id is not None else None
            ),
            "detail": self.detail,
            "schema_version": self.schema_version,
            "idempotency_key": self.idempotency_key,
            "created_at": self.created_at.isoformat(),
        }


__all__ = [
    "MAX_ANNOTATION_DETAIL_CHARS",
    "MAX_IOC_VALUE_CHARS",
    "WORKSPACE_NOTE_ANNOTATION_SCHEMA_VERSION",
    "NoteAnnotationActorKind",
    "NoteAnnotationKind",
    "NoteIocType",
    "WorkspaceNoteAnnotationV1",
    "is_note_path",
]
