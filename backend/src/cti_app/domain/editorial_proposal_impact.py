"""Versioned outbox identity for work derived from an adopted proposal."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID

from cti_app.domain.publication_render import PublicationPreviewStatus


class EditorialProposalImpactState(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    SUPERSEDED = "superseded"


class EditorialWorkspaceImpactStatus(StrEnum):
    NONE = "none"
    PENDING = "pending"
    RUNNING = "running"
    SUPERSEDED = "superseded"
    UP_TO_DATE = "up_to_date"
    FAILED = "failed"


class EditorialPdfStatus(StrEnum):
    CURRENT = "CURRENT"
    STALE = "STALE"


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


def project_editorial_impact_status(
    impact: EditorialProposalImpact | None,
    *,
    render_status: PublicationPreviewStatus | str,
    publication_stale: bool,
) -> tuple[EditorialWorkspaceImpactStatus, PublicationPreviewStatus, EditorialPdfStatus]:
    """Combine the durable impact state with the real publication render state."""
    if impact is None:
        impact_status = EditorialWorkspaceImpactStatus.NONE
    elif impact.state is EditorialProposalImpactState.DONE:
        impact_status = EditorialWorkspaceImpactStatus.UP_TO_DATE
    else:
        impact_status = EditorialWorkspaceImpactStatus(impact.state.value)

    preview_status = PublicationPreviewStatus(render_status)
    pdf_is_stale = publication_stale or preview_status is PublicationPreviewStatus.STALE
    if pdf_is_stale and preview_status is PublicationPreviewStatus.READY:
        preview_status = PublicationPreviewStatus.STALE

    pdf_status = EditorialPdfStatus.STALE if pdf_is_stale else EditorialPdfStatus.CURRENT
    return impact_status, preview_status, pdf_status
