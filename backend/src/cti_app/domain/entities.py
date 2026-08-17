import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any
from uuid import UUID, uuid4

from cti_app.domain.blobs import utc_now
from cti_app.domain.classification import TLP, ensure_tlp_not_downgraded
from cti_app.domain.errors import DomainError

SLUG_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def _require_text(value: str, field_name: str) -> None:
    if not value.strip():
        raise DomainError(f"{field_name} cannot be empty")


def _require_aware(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise DomainError(f"{field_name} must be timezone-aware")


@dataclass(slots=True, kw_only=True)
class Subject:
    external_id: str
    slug: str
    tlp: TLP
    id: UUID = field(default_factory=uuid4)
    created_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        _require_text(self.external_id, "external_id")
        if not SLUG_PATTERN.fullmatch(self.slug):
            raise DomainError("slug must contain lowercase alphanumeric segments")
        _require_aware(self.created_at, "created_at")

    def restrict_tlp(self, requested: TLP) -> None:
        ensure_tlp_not_downgraded(self.tlp, requested)
        self.tlp = requested


@dataclass(slots=True, kw_only=True)
class SourceDocument:
    subject_id: UUID
    blob_id: UUID
    original_name: str
    origin: str
    acquired_at: datetime
    license_restriction: str | None
    tlp: TLP
    do_not_submit: bool
    external_llm_allowed: bool
    logical_filename: str | None = None
    source_collection_id: UUID | None = None
    source_candidate_id: UUID | None = None
    decoded_blob_id: UUID | None = None
    title: str | None = None
    publisher: str | None = None
    published_at: date | None = None
    final_url: str | None = None
    declared_mime_type: str | None = None
    detected_mime_type: str | None = None
    encoded_sha256: str | None = None
    decoded_sha256: str | None = None
    encoded_size: int | None = None
    decoded_size: int | None = None
    id: UUID = field(default_factory=uuid4)
    created_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        _require_text(self.original_name, "original_name")
        _require_text(self.origin, "origin")
        _require_aware(self.acquired_at, "acquired_at")
        _require_aware(self.created_at, "created_at")
        if self.logical_filename is not None:
            _require_text(self.logical_filename, "logical_filename")

    def restrict_tlp(self, requested: TLP) -> None:
        ensure_tlp_not_downgraded(self.tlp, requested)
        self.tlp = requested


@dataclass(slots=True, kw_only=True)
class Sample:
    subject_id: UUID
    blob_id: UUID
    original_name: str
    origin: str
    acquired_at: datetime
    license_restriction: str | None
    tlp: TLP
    do_not_submit: bool
    external_llm_allowed: bool
    id: UUID = field(default_factory=uuid4)
    created_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        _require_text(self.original_name, "original_name")
        _require_text(self.origin, "origin")
        _require_aware(self.acquired_at, "acquired_at")
        _require_aware(self.created_at, "created_at")

    def restrict_tlp(self, requested: TLP) -> None:
        ensure_tlp_not_downgraded(self.tlp, requested)
        self.tlp = requested


@dataclass(frozen=True, slots=True, kw_only=True)
class ProvenanceEvent:
    aggregate_type: str
    aggregate_id: UUID
    event_type: str
    payload: dict[str, Any]
    tlp: TLP
    subject_id: UUID | None = None
    actor_id: str | None = None
    id: UUID = field(default_factory=uuid4)
    occurred_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        _require_text(self.aggregate_type, "aggregate_type")
        _require_text(self.event_type, "event_type")
        _require_aware(self.occurred_at, "occurred_at")
