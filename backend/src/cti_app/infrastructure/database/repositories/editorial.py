from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cti_app.domain.editorial import HumanDecision, HumanDecisionType
from cti_app.infrastructure.database.models.editorial import HumanDecisionRow


class SqlAlchemyHumanDecisionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def append(self, decision: HumanDecision) -> None:
        self._session.add(
            HumanDecisionRow(
                id=decision.id,
                edition_id=decision.edition_id,
                decision_type=decision.decision_type.value,
                subject_ids=[str(item) for item in decision.subject_ids],
                actor_id=decision.actor_id,
                correlation_id=decision.correlation_id,
                payload=decision.payload,
                occurred_at=decision.occurred_at,
            )
        )
        await self._session.flush()

    async def list_for_edition(self, edition_id: UUID) -> Sequence[HumanDecision]:
        rows = await self._session.scalars(
            select(HumanDecisionRow)
            .where(HumanDecisionRow.edition_id == edition_id)
            .order_by(HumanDecisionRow.occurred_at, HumanDecisionRow.id)
        )
        return [
            HumanDecision(
                id=row.id,
                edition_id=row.edition_id,
                decision_type=HumanDecisionType(row.decision_type),
                subject_ids=tuple(UUID(item) for item in row.subject_ids),
                actor_id=row.actor_id,
                correlation_id=row.correlation_id,
                payload=row.payload,
                occurred_at=row.occurred_at,
            )
            for row in rows
        ]
