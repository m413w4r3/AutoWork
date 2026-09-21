from __future__ import annotations

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
