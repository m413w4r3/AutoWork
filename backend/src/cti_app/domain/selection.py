from __future__ import annotations

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID, uuid4


class SelectionAction(StrEnum):
    SELECT = "select"
    IGNORE = "ignore"


def _require_aware(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")


@dataclass(frozen=True, slots=True)
class SelectionDecision:
    edition_id: UUID
    discovery_subject_id: UUID
    snapshot_id: UUID
    snapshot_version: int
    action: SelectionAction
    subject_id: UUID | None
    actor_id: str
    correlation_id: str
    idempotency_key: str
    occurred_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    id: UUID = field(default_factory=uuid4)

    def __post_init__(self) -> None:
        if not self.actor_id.strip():
            raise ValueError("actor_id must not be blank")
        if not self.correlation_id.strip():
            raise ValueError("correlation_id must not be blank")
        if not self.idempotency_key.strip():
            raise ValueError("idempotency_key must not be blank")
        if self.snapshot_version <= 0:
            raise ValueError("snapshot_version must be positive")
        _require_aware(self.occurred_at, "occurred_at")
        if self.action is SelectionAction.SELECT and self.subject_id is None:
            raise ValueError("select decisions require a subject_id")
        if self.action is SelectionAction.IGNORE and self.subject_id is not None:
            raise ValueError("ignore decisions cannot have a subject_id")


@dataclass(frozen=True, slots=True)
class SubjectDiscoveryOrigin:
    subject_id: UUID
    edition_id: UUID
    discovery_subject_id: UUID
    selection_decision_id: UUID
    selected_snapshot_id: UUID
    selected_snapshot_version: int
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        if self.selected_snapshot_version <= 0:
            raise ValueError("selected_snapshot_version must be positive")
        _require_aware(self.created_at, "created_at")


def selection_request_fingerprint(
    *,
    snapshot_id: UUID,
    snapshot_version: int,
    decisions: Iterable[tuple[UUID, SelectionAction]],
) -> str:
    """Canonical fingerprint of a whole selection request.

    An ``Idempotency-Key`` identifies one HTTP request, not one decision:
    the fingerprint therefore covers the snapshot the operator read and the
    complete, order-independent set of decisions they confirmed. Replaying
    the same key with a subset, a superset or a different action yields a
    different fingerprint and must be rejected.
    """

    body = "|".join(sorted(f"{subject_id.hex}:{action.value}" for subject_id, action in decisions))
    material = f"{snapshot_id.hex}:{snapshot_version}:{body}"
    return hashlib.sha256(material.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class SelectionIdempotencyRecord:
    edition_id: UUID
    idempotency_key: str
    request_fingerprint: str
    actor_id: str
    correlation_id: str
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    id: UUID = field(default_factory=uuid4)

    def __post_init__(self) -> None:
        if not self.idempotency_key.strip():
            raise ValueError("idempotency_key must not be blank")
        if len(self.request_fingerprint) != 64:
            raise ValueError("request_fingerprint must be a sha256 hex digest")
        if not self.actor_id.strip():
            raise ValueError("actor_id must not be blank")
        if not self.correlation_id.strip():
            raise ValueError("correlation_id must not be blank")
        _require_aware(self.created_at, "created_at")
