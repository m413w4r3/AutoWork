"""Versioned outbox identity for work derived from an adopted proposal."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID


class EditorialProposalImpactState(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    SUPERSEDED = "superseded"


@dataclass(frozen=True, slots=True)
class EditorialProposalImpact:
    impact_id: UUID
    subject_id: UUID
    production_run_id: UUID
    proposal_id: UUID
    accepted_artifact_id: UUID
    accepted_snapshot_id: UUID
    base_pin_hash: str
    accepted_pin_hash: str
    sequence: int
    state: EditorialProposalImpactState = EditorialProposalImpactState.PENDING
    attempt_count: int = 0
    last_error_code: str | None = None
    available_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    claimed_at: datetime | None = None
    completed_at: datetime | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        if self.sequence < 1 or self.attempt_count < 0:
            raise ValueError("editorial_proposal_impact_counter_invalid")
        for value in (self.base_pin_hash, self.accepted_pin_hash):
            if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
                raise ValueError("editorial_proposal_impact_hash_invalid")
        if (
            self.state
            not in {
                EditorialProposalImpactState.PENDING,
                EditorialProposalImpactState.RUNNING,
            }
            and self.completed_at is None
        ):
            raise ValueError("editorial_proposal_impact_completion_missing")
