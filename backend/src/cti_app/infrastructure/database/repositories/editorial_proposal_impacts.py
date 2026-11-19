"""PostgreSQL adapter for the mutable editorial impact outbox."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from cti_app.domain.editorial_proposal_impact import (
    EditorialProposalImpact,
    EditorialProposalImpactState,
)
from cti_app.infrastructure.database.models.editorial_proposal_impacts import (
    EditorialProposalImpactRow,
)


class SqlAlchemyEditorialProposalImpactRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, impact: EditorialProposalImpact) -> None:
        self._session.add(EditorialProposalImpactRow(**_row_values(impact)))
        await self._session.flush()

    async def get_by_proposal(self, proposal_id: UUID) -> EditorialProposalImpact | None:
        row = await self._session.scalar(
            select(EditorialProposalImpactRow).where(
                EditorialProposalImpactRow.proposal_id == proposal_id
            )
        )
        return _impact_from_row(row) if row is not None else None

    async def next_sequence_for_run(self, production_run_id: UUID) -> int:
        current = await self._session.scalar(
            select(func.max(EditorialProposalImpactRow.sequence)).where(
                EditorialProposalImpactRow.production_run_id == production_run_id
            )
        )
        return int(current or 0) + 1


def _row_values(impact: EditorialProposalImpact) -> dict[str, object]:
    return {
        "impact_id": impact.impact_id,
        "subject_id": impact.subject_id,
        "production_run_id": impact.production_run_id,
        "proposal_id": impact.proposal_id,
        "accepted_artifact_id": impact.accepted_artifact_id,
        "accepted_snapshot_id": impact.accepted_snapshot_id,
        "base_pin_hash": impact.base_pin_hash,
        "accepted_pin_hash": impact.accepted_pin_hash,
        "sequence": impact.sequence,
        "state": impact.state.value,
        "attempt_count": impact.attempt_count,
        "last_error_code": impact.last_error_code,
        "available_at": impact.available_at,
        "claimed_at": impact.claimed_at,
        "completed_at": impact.completed_at,
        "created_at": impact.created_at,
        "updated_at": impact.updated_at,
    }


def _impact_from_row(row: EditorialProposalImpactRow) -> EditorialProposalImpact:
    return EditorialProposalImpact(
        impact_id=row.impact_id,
        subject_id=row.subject_id,
        production_run_id=row.production_run_id,
        proposal_id=row.proposal_id,
        accepted_artifact_id=row.accepted_artifact_id,
        accepted_snapshot_id=row.accepted_snapshot_id,
        base_pin_hash=row.base_pin_hash,
        accepted_pin_hash=row.accepted_pin_hash,
        sequence=row.sequence,
        state=EditorialProposalImpactState(row.state),
        attempt_count=row.attempt_count,
        last_error_code=row.last_error_code,
        available_at=row.available_at,
        claimed_at=row.claimed_at,
        completed_at=row.completed_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )
