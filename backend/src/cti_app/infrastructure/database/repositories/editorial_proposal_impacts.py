"""PostgreSQL adapter for the mutable editorial impact outbox."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from uuid import UUID

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

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

    async def get_by_id(self, impact_id: UUID) -> EditorialProposalImpact | None:
        row = await self._session.get(EditorialProposalImpactRow, impact_id)
        return _impact_from_row(row) if row is not None else None

    async def latest_for_run(self, production_run_id: UUID) -> EditorialProposalImpact | None:
        row = await self._session.scalar(
            select(EditorialProposalImpactRow)
            .where(EditorialProposalImpactRow.production_run_id == production_run_id)
            .order_by(EditorialProposalImpactRow.sequence.desc())
            .limit(1)
        )
        return _impact_from_row(row) if row is not None else None

    async def next_sequence_for_run(self, production_run_id: UUID) -> int:
        current = await self._session.scalar(
            select(func.max(EditorialProposalImpactRow.sequence)).where(
                EditorialProposalImpactRow.production_run_id == production_run_id
            )
        )
        return int(current or 0) + 1

    async def claim(
        self,
        impact_id: UUID,
        *,
        now: datetime,
        stale_running_before: datetime,
    ) -> EditorialProposalImpact | None:
        row = await self._session.scalar(
            select(EditorialProposalImpactRow)
            .where(EditorialProposalImpactRow.impact_id == impact_id)
            .with_for_update()
        )
        if row is None:
            return None
        dispatchable = (row.state in {"pending", "failed"} and row.available_at <= now) or (
            row.state == "running"
            and row.claimed_at is not None
            and row.claimed_at <= stale_running_before
        )
        if not dispatchable:
            return None
        row.state = EditorialProposalImpactState.RUNNING.value
        row.attempt_count += 1
        row.last_error_code = None
        row.claimed_at = now
        row.completed_at = None
        row.updated_at = now
        await self._session.flush()
        return _impact_from_row(row)

    async def list_dispatchable(
        self,
        *,
        now: datetime,
        stale_running_before: datetime,
        dispatch_before: datetime,
        limit: int,
    ) -> Sequence[UUID]:
        statement = (
            select(EditorialProposalImpactRow.impact_id)
            .where(
                EditorialProposalImpactRow.updated_at <= dispatch_before,
                _dispatchable_condition(now=now, stale_running_before=stale_running_before),
            )
            .order_by(
                EditorialProposalImpactRow.production_run_id,
                EditorialProposalImpactRow.sequence,
            )
            .limit(limit)
        )
        result = await self._session.scalars(statement)
        return tuple(result.all())

    async def reserve_dispatch(
        self,
        impact_id: UUID,
        *,
        now: datetime,
        stale_running_before: datetime,
        dispatch_before: datetime,
    ) -> bool:
        result = await self._session.execute(
            update(EditorialProposalImpactRow)
            .where(
                EditorialProposalImpactRow.impact_id == impact_id,
                EditorialProposalImpactRow.updated_at <= dispatch_before,
                _dispatchable_condition(now=now, stale_running_before=stale_running_before),
            )
            .values(updated_at=now)
            .returning(EditorialProposalImpactRow.impact_id)
        )
        return result.scalar_one_or_none() is not None

    async def set_state(
        self,
        impact_id: UUID,
        *,
        state: EditorialProposalImpactState,
        expected_claimed_at: datetime,
        now: datetime,
        available_at: datetime,
        last_error_code: str | None,
    ) -> EditorialProposalImpact | None:
        row = await self._session.scalar(
            select(EditorialProposalImpactRow)
            .where(EditorialProposalImpactRow.impact_id == impact_id)
            .with_for_update()
        )
        if (
            row is None
            or row.state != EditorialProposalImpactState.RUNNING.value
            or row.claimed_at != expected_claimed_at
        ):
            return None
        row.state = state.value
        row.last_error_code = last_error_code
        row.available_at = available_at
        row.completed_at = (
            now
            if state
            in {
                EditorialProposalImpactState.DONE,
                EditorialProposalImpactState.FAILED,
                EditorialProposalImpactState.SUPERSEDED,
            }
            else None
        )
        row.updated_at = now
        await self._session.flush()
        return _impact_from_row(row)


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


def _dispatchable_condition(
    *, now: datetime, stale_running_before: datetime
) -> ColumnElement[bool]:
    return or_(
        and_(
            EditorialProposalImpactRow.state.in_(
                [
                    EditorialProposalImpactState.PENDING.value,
                    EditorialProposalImpactState.FAILED.value,
                ]
            ),
            EditorialProposalImpactRow.available_at <= now,
        ),
        and_(
            EditorialProposalImpactRow.state == EditorialProposalImpactState.RUNNING.value,
            EditorialProposalImpactRow.claimed_at.is_not(None),
            EditorialProposalImpactRow.claimed_at <= stale_running_before,
        ),
    )


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
