from collections.abc import Sequence
from typing import Any, cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cti_app.domain.discovery import SourceRelationshipStatus
from cti_app.domain.editorial import (
    CandidateReference,
    EditorialGroup,
    EditorialGroupStatus,
    EditorialScore,
    EditorialType,
    GroupingConfidence,
    GroupingOutcome,
    HumanDecision,
    HumanDecisionType,
)
from cti_app.infrastructure.database.models.schema import (
    EditionRow,
    EditorialGroupRow,
    HumanDecisionRow,
)


class SqlAlchemyEditorialGroupRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, group: EditorialGroup) -> None:
        self._session.add(EditorialGroupRow(**_editorial_group_values(group)))
        await self._session.flush()

    async def get(self, group_id: UUID) -> EditorialGroup | None:
        row = await self._session.get(EditorialGroupRow, group_id)
        return _editorial_group_from_row(row) if row else None

    async def get_for_update(self, group_id: UUID) -> EditorialGroup | None:
        row = await self._session.scalar(
            select(EditorialGroupRow).where(EditorialGroupRow.id == group_id).with_for_update()
        )
        return _editorial_group_from_row(row) if row else None

    async def list_for_edition(self, edition_id: UUID) -> Sequence[EditorialGroup]:
        rows = await self._session.scalars(
            select(EditorialGroupRow)
            .where(EditorialGroupRow.edition_id == edition_id)
            .order_by(EditorialGroupRow.created_at, EditorialGroupRow.id)
        )
        return [_editorial_group_from_row(row) for row in rows]

    async def list_historical(self, edition_id: UUID) -> Sequence[EditorialGroup]:
        edition = await self._session.get(EditionRow, edition_id)
        if edition is None:
            return []
        rows = await self._session.scalars(
            select(EditorialGroupRow)
            .join(EditionRow, EditionRow.id == EditorialGroupRow.edition_id)
            .where(
                EditionRow.country_code == edition.country_code,
                EditionRow.period_start < edition.period_start,
                EditorialGroupRow.status == EditorialGroupStatus.SELECTED.value,
            )
            .order_by(EditionRow.period_start.desc(), EditorialGroupRow.created_at.desc())
        )
        return [_editorial_group_from_row(row) for row in rows]

    async def get_by_subject(self, subject_id: UUID) -> EditorialGroup | None:
        row = await self._session.scalar(
            select(EditorialGroupRow).where(EditorialGroupRow.subject_id == subject_id)
        )
        return _editorial_group_from_row(row) if row else None

    async def save(self, group: EditorialGroup) -> None:
        row = await self._session.get(EditorialGroupRow, group.id)
        if row is None:
            raise LookupError(f"Editorial group {group.id} does not exist")
        for field_name, value in _editorial_group_values(group).items():
            setattr(row, field_name, value)
        await self._session.flush()


class SqlAlchemyHumanDecisionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def append(self, decision: HumanDecision) -> None:
        self._session.add(
            HumanDecisionRow(
                id=decision.id,
                edition_id=decision.edition_id,
                decision_type=decision.decision_type.value,
                group_ids=[str(item) for item in decision.group_ids],
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
                group_ids=tuple(UUID(item) for item in row.group_ids),
                actor_id=row.actor_id,
                correlation_id=row.correlation_id,
                payload=row.payload,
                occurred_at=row.occurred_at,
            )
            for row in rows
        ]


def _editorial_group_values(group: EditorialGroup) -> dict[str, object]:
    return {
        "id": group.id,
        "edition_id": group.edition_id,
        "title": group.title,
        "outcome": group.outcome.value,
        "status": group.status.value,
        "source_relationship_status": group.source_relationship_status.value,
        "needs_source_verification": group.needs_source_verification,
        "needs_source_expansion": group.needs_source_expansion,
        "grouping_confidence": group.grouping_confidence.value,
        "grouping_justification": group.grouping_justification,
        "potential_historical_group_id": group.potential_historical_group_id,
        "editorial_type": group.editorial_type.value if group.editorial_type else None,
        "subject_id": group.subject_id,
        "discovery_subject_id": group.discovery_subject_id,
        "payload": {
            "candidate_references": [
                {"batch_id": str(item.batch_id), "candidate_id": str(item.candidate_id)}
                for item in group.candidate_references
            ],
            "score": {
                "impact": group.score.impact,
                "novelty": group.score.novelty,
                "technical_depth": group.score.technical_depth,
                "hunting_potential": group.score.hunting_potential,
                "actionability": group.score.actionability,
                "source_quality": group.score.source_quality,
                "justifications": group.score.justifications,
            },
        },
        "version": group.version,
        "created_at": group.created_at,
        "updated_at": group.updated_at,
    }


def _editorial_group_from_row(row: EditorialGroupRow) -> EditorialGroup:
    payload = row.payload
    score = cast(dict[str, Any], payload["score"])
    references = cast(list[dict[str, str]], payload["candidate_references"])
    return EditorialGroup(
        id=row.id,
        edition_id=row.edition_id,
        title=row.title,
        candidate_references=tuple(
            CandidateReference(UUID(item["batch_id"]), UUID(item["candidate_id"]))
            for item in references
        ),
        outcome=GroupingOutcome(row.outcome),
        status=EditorialGroupStatus(row.status),
        score=EditorialScore(
            impact=int(score["impact"]),
            novelty=int(score["novelty"]),
            technical_depth=int(score["technical_depth"]),
            hunting_potential=int(score["hunting_potential"]),
            actionability=int(score["actionability"]),
            source_quality=int(score["source_quality"]),
            justifications=cast(dict[str, str], score["justifications"]),
        ),
        source_relationship_status=SourceRelationshipStatus(row.source_relationship_status),
        needs_source_verification=row.needs_source_verification,
        needs_source_expansion=row.needs_source_expansion,
        grouping_confidence=GroupingConfidence(row.grouping_confidence),
        grouping_justification=row.grouping_justification,
        potential_historical_group_id=row.potential_historical_group_id,
        discovery_subject_id=row.discovery_subject_id,
        editorial_type=EditorialType(row.editorial_type) if row.editorial_type else None,
        subject_id=row.subject_id,
        version=row.version,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )
