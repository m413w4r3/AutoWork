"""Canonical, versioned editorial choices for an edition."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

_HEX_RE = re.compile(r"^[0-9a-f]+$")


class CompositionDisposition(StrEnum):
    INCLUDED = "included"
    DEFERRED = "deferred"
    EXCLUDED = "excluded"


class CompositionPreparationStatus(StrEnum):
    UNASSESSED = "unassessed"
    READY = "ready"
    BLOCKED = "blocked"


class CompositionMutationKind(StrEnum):
    REPLACE_ENTRIES = "replace_entries"
    INCLUDE = "include"
    DEFER = "defer"
    EXCLUDE = "exclude"
    REORDER = "reorder"


class CompositionInvariantError(ValueError):
    """An entry violates the canonical composition invariants."""

    code = "composition_invariant_violation"
    status_code = 422


@dataclass(frozen=True, slots=True, kw_only=True)
class EditionCompositionEntry:
    subject_id: UUID
    disposition: CompositionDisposition
    position: int | None = None
    production_run_id: UUID | None = None
    pipeline_generation: int | None = None
    artifact_id: UUID | None = None
    artifact_version: int | None = None
    artifact_input_hash: str | None = None
    provenance_batch_id: UUID | None = None
    review_decision_id: UUID | None = None
    document_sha256: str | None = None
    preparation_status: CompositionPreparationStatus = CompositionPreparationStatus.UNASSESSED
    id: UUID = field(default_factory=uuid4)

    def __post_init__(self) -> None:
        if not isinstance(self.disposition, CompositionDisposition):
            raise TypeError("disposition must be a CompositionDisposition")
        if not isinstance(self.preparation_status, CompositionPreparationStatus):
            raise TypeError("preparation_status must be a CompositionPreparationStatus")
        if self.position is not None and self.position < 1:
            raise CompositionInvariantError("positions must be >= 1")
        if self.disposition is CompositionDisposition.INCLUDED and self.position is None:
            raise CompositionInvariantError("included entries require a position")

        document_identity = (
            self.artifact_id,
            self.artifact_version,
            self.artifact_input_hash,
        )
        if any(value is None for value in document_identity) and any(
            value is not None for value in document_identity
        ):
            raise CompositionInvariantError("document identity must be complete or empty")
        if self.production_run_id is None:
            if self.pipeline_generation is not None or any(
                value is not None for value in document_identity
            ):
                raise CompositionInvariantError(
                    "runless entries cannot carry generation or document"
                )
        elif self.pipeline_generation is None:
            raise CompositionInvariantError("production_run_id requires pipeline_generation")
        if any(value is not None for value in document_identity) and self.production_run_id is None:
            raise CompositionInvariantError("document identity requires a production run")
        if self.disposition is CompositionDisposition.INCLUDED and (
            self.production_run_id is None
            or self.pipeline_generation is None
            or any(value is None for value in document_identity)
        ):
            raise CompositionInvariantError("included entries require a complete artifact pin")
        if self.disposition is CompositionDisposition.DEFERRED and (
            self.production_run_id is not None
            or self.pipeline_generation is not None
            or any(value is not None for value in document_identity)
        ):
            raise CompositionInvariantError("deferred entries cannot carry artifacts")
        if self.disposition is CompositionDisposition.EXCLUDED and self.review_decision_id is None:
            raise CompositionInvariantError("excluded entries require a review decision")
        if self.pipeline_generation is not None and self.pipeline_generation < 0:
            raise CompositionInvariantError("pipeline_generation must be >= 0")
        if self.artifact_version is not None and self.artifact_version < 1:
            raise CompositionInvariantError("artifact_version must be >= 1")
        if self.artifact_input_hash is not None and (
            len(self.artifact_input_hash) < 64 or not _HEX_RE.fullmatch(self.artifact_input_hash)
        ):
            raise CompositionInvariantError("artifact_input_hash must be at least 64 hex digits")
        if self.document_sha256 is not None and (
            len(self.document_sha256) != 64 or not _HEX_RE.fullmatch(self.document_sha256)
        ):
            raise CompositionInvariantError("document_sha256 must be a lowercase SHA-256")


@dataclass(frozen=True, slots=True, kw_only=True)
class EditionComposition:
    edition_id: UUID
    version: int
    entries: tuple[EditionCompositionEntry, ...]
    actor_id: str
    preparation_status: CompositionPreparationStatus = CompositionPreparationStatus.UNASSESSED
    id: UUID = field(default_factory=uuid4)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        if self.version < 1:
            raise CompositionInvariantError("composition version must be >= 1")
        if not self.actor_id.strip():
            raise CompositionInvariantError("actor_id must not be empty")
        if not isinstance(self.preparation_status, CompositionPreparationStatus):
            raise TypeError("preparation_status must be a CompositionPreparationStatus")
        if any(not isinstance(entry, EditionCompositionEntry) for entry in self.entries):
            raise TypeError("entries must contain EditionCompositionEntry values")
        subjects = [entry.subject_id for entry in self.entries]
        if len(set(subjects)) != len(subjects):
            raise CompositionInvariantError("each subject may appear only once")
        included_positions = [
            entry.position
            for entry in self.entries
            if entry.disposition is CompositionDisposition.INCLUDED
        ]
        if len(set(included_positions)) != len(included_positions):
            raise CompositionInvariantError("included entry positions must be unique")
        for field_name, value in (("created_at", self.created_at), ("updated_at", self.updated_at)):
            if value.tzinfo is None or value.utcoffset() is None:
                raise CompositionInvariantError(f"{field_name} must be timezone-aware")
        object.__setattr__(self, "created_at", self.created_at.astimezone(UTC))
        object.__setattr__(self, "updated_at", self.updated_at.astimezone(UTC))
        object.__setattr__(
            self,
            "entries",
            tuple(sorted(self.entries, key=lambda item: (item.position or 2**31, str(item.id)))),
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class EditionCompositionEvent:
    composition_id: UUID
    edition_id: UUID
    kind: CompositionMutationKind
    expected_version: int
    result_version: int
    idempotency_key: str
    request_fingerprint: str
    actor_id: str
    correlation_id: str
    payload: dict[str, Any]
    result_snapshot: dict[str, Any]
    id: UUID = field(default_factory=uuid4)
    occurred_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        if not isinstance(self.kind, CompositionMutationKind):
            raise TypeError("kind must be a CompositionMutationKind")
        if self.expected_version < 0 or self.result_version != self.expected_version + 1:
            raise CompositionInvariantError("event versions must advance exactly once")
        if not self.idempotency_key.strip() or len(self.idempotency_key) > 255:
            raise CompositionInvariantError("idempotency_key must be non-empty and <= 255 chars")
        if not self.actor_id.strip() or len(self.actor_id) > 255:
            raise CompositionInvariantError("actor_id must be non-empty and <= 255 chars")
        if not self.correlation_id.strip() or len(self.correlation_id) > 128:
            raise CompositionInvariantError("correlation_id must be non-empty and <= 128 chars")
        if len(self.request_fingerprint) != 64 or not _HEX_RE.fullmatch(self.request_fingerprint):
            raise CompositionInvariantError("request_fingerprint must be lowercase SHA-256")
        if self.occurred_at.tzinfo is None or self.occurred_at.utcoffset() is None:
            raise CompositionInvariantError("occurred_at must be timezone-aware")
        object.__setattr__(self, "occurred_at", self.occurred_at.astimezone(UTC))


def composition_request_fingerprint(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def composition_snapshot(composition: EditionComposition) -> dict[str, Any]:
    return {
        "id": str(composition.id),
        "edition_id": str(composition.edition_id),
        "version": composition.version,
        "actor_id": composition.actor_id,
        "preparation_status": composition.preparation_status.value,
        "created_at": composition.created_at.isoformat(),
        "updated_at": composition.updated_at.isoformat(),
        "entries": [composition_entry_snapshot(entry) for entry in composition.entries],
    }


def composition_entry_snapshot(entry: EditionCompositionEntry) -> dict[str, Any]:
    return {
        "id": str(entry.id),
        "subject_id": str(entry.subject_id),
        "disposition": entry.disposition.value,
        "position": entry.position,
        "production_run_id": str(entry.production_run_id) if entry.production_run_id else None,
        "pipeline_generation": entry.pipeline_generation,
        "artifact_id": str(entry.artifact_id) if entry.artifact_id else None,
        "artifact_version": entry.artifact_version,
        "artifact_input_hash": entry.artifact_input_hash,
        "provenance_batch_id": (
            str(entry.provenance_batch_id) if entry.provenance_batch_id else None
        ),
        "review_decision_id": str(entry.review_decision_id) if entry.review_decision_id else None,
        "document_sha256": entry.document_sha256,
        "preparation_status": entry.preparation_status.value,
    }


def composition_from_snapshot(payload: dict[str, Any]) -> EditionComposition:
    return EditionComposition(
        id=UUID(payload["id"]),
        edition_id=UUID(payload["edition_id"]),
        version=int(payload["version"]),
        actor_id=str(payload["actor_id"]),
        preparation_status=CompositionPreparationStatus(payload["preparation_status"]),
        created_at=datetime.fromisoformat(payload["created_at"]),
        updated_at=datetime.fromisoformat(payload["updated_at"]),
        entries=tuple(
            EditionCompositionEntry(
                id=UUID(item["id"]),
                subject_id=UUID(item["subject_id"]),
                disposition=CompositionDisposition(item["disposition"]),
                position=item["position"],
                production_run_id=UUID(item["production_run_id"])
                if item["production_run_id"]
                else None,
                pipeline_generation=item["pipeline_generation"],
                artifact_id=UUID(item["artifact_id"]) if item["artifact_id"] else None,
                artifact_version=item["artifact_version"],
                artifact_input_hash=item["artifact_input_hash"],
                provenance_batch_id=UUID(item["provenance_batch_id"])
                if item["provenance_batch_id"]
                else None,
                review_decision_id=UUID(item["review_decision_id"])
                if item["review_decision_id"]
                else None,
                document_sha256=item["document_sha256"],
                preparation_status=CompositionPreparationStatus(item["preparation_status"]),
            )
            for item in payload["entries"]
        ),
    )
