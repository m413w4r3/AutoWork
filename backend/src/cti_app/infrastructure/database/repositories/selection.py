from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cti_app.domain.selection import SelectionAction, SelectionDecision, SubjectDiscoveryOrigin
from cti_app.infrastructure.database.models.selection import (
    SelectionDecisionRow,
    SubjectDiscoveryOriginRow,
)


class SqlAlchemySelectionDecisionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def append(self, decision: SelectionDecision) -> None:
        self._session.add(
            SelectionDecisionRow(
                id=decision.id,
                edition_id=decision.edition_id,
                discovery_subject_id=decision.discovery_subject_id,
                snapshot_id=decision.snapshot_id,
                snapshot_version=decision.snapshot_version,
                subject_id=decision.subject_id,
                action=decision.action.value,
                actor_id=decision.actor_id,
                correlation_id=decision.correlation_id,
                idempotency_key=decision.idempotency_key,
                occurred_at=decision.occurred_at,
            )
        )
        await self._session.flush()

    async def list_for_edition(self, edition_id: UUID) -> Sequence[SelectionDecision]:
        rows = await self._session.scalars(
            select(SelectionDecisionRow)
            .where(SelectionDecisionRow.edition_id == edition_id)
            .order_by(SelectionDecisionRow.occurred_at, SelectionDecisionRow.id)
        )
        return [_selection_decision_from_row(row) for row in rows]

    async def get_by_idempotency_key(
        self, edition_id: UUID, discovery_subject_id: UUID, idempotency_key: str
    ) -> SelectionDecision | None:
        row = await self._session.scalar(
            select(SelectionDecisionRow).where(
                SelectionDecisionRow.edition_id == edition_id,
                SelectionDecisionRow.discovery_subject_id == discovery_subject_id,
                SelectionDecisionRow.idempotency_key == idempotency_key,
            )
        )
        return _selection_decision_from_row(row) if row else None


class SqlAlchemySubjectDiscoveryOriginRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, origin: SubjectDiscoveryOrigin) -> None:
        self._session.add(
            SubjectDiscoveryOriginRow(
                subject_id=origin.subject_id,
                edition_id=origin.edition_id,
                discovery_subject_id=origin.discovery_subject_id,
                selection_decision_id=origin.selection_decision_id,
                selected_snapshot_id=origin.selected_snapshot_id,
                selected_snapshot_version=origin.selected_snapshot_version,
                created_at=origin.created_at,
            )
        )
        await self._session.flush()

    async def get_by_subject(self, subject_id: UUID) -> SubjectDiscoveryOrigin | None:
        row = await self._session.get(SubjectDiscoveryOriginRow, subject_id)
        return _subject_discovery_origin_from_row(row) if row else None

    async def list_for_edition(self, edition_id: UUID) -> Sequence[SubjectDiscoveryOrigin]:
        rows = await self._session.scalars(
            select(SubjectDiscoveryOriginRow)
            .where(SubjectDiscoveryOriginRow.edition_id == edition_id)
            .order_by(SubjectDiscoveryOriginRow.created_at, SubjectDiscoveryOriginRow.subject_id)
        )
        return [_subject_discovery_origin_from_row(row) for row in rows]


def _selection_decision_from_row(row: SelectionDecisionRow) -> SelectionDecision:
    return SelectionDecision(
        id=row.id,
        edition_id=row.edition_id,
        discovery_subject_id=row.discovery_subject_id,
        snapshot_id=row.snapshot_id,
        snapshot_version=row.snapshot_version,
        action=SelectionAction(row.action),
        subject_id=row.subject_id,
        actor_id=row.actor_id,
        correlation_id=row.correlation_id,
        idempotency_key=row.idempotency_key,
        occurred_at=row.occurred_at,
    )


def _subject_discovery_origin_from_row(row: SubjectDiscoveryOriginRow) -> SubjectDiscoveryOrigin:
    return SubjectDiscoveryOrigin(
        subject_id=row.subject_id,
        edition_id=row.edition_id,
        discovery_subject_id=row.discovery_subject_id,
        selection_decision_id=row.selection_decision_id,
        selected_snapshot_id=row.selected_snapshot_id,
        selected_snapshot_version=row.selected_snapshot_version,
        created_at=row.created_at,
    )
