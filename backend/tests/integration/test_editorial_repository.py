from datetime import date
from uuid import uuid4

import pytest
from sqlalchemy import update
from sqlalchemy.exc import DBAPIError

from cti_app.domain.classification import TLP
from cti_app.domain.discovery import SourceRelationshipStatus
from cti_app.domain.editions import Edition
from cti_app.domain.editorial import (
    CandidateReference,
    EditorialGroup,
    EditorialScore,
    GroupingConfidence,
    GroupingOutcome,
    HumanDecision,
    HumanDecisionType,
)
from cti_app.infrastructure.database.models.editorial import HumanDecisionRow
from cti_app.infrastructure.database.session import create_postgres_engine, create_session_factory
from cti_app.infrastructure.database.uow import SqlAlchemyUnitOfWork

pytestmark = pytest.mark.integration


async def test_editorial_group_round_trip_and_human_decision_is_append_only(
    migrated_postgres_url: str,
) -> None:
    engine = create_postgres_engine(migrated_postgres_url)
    session_factory = create_session_factory(engine)
    edition = Edition(
        country="Iran",
        country_code="IR",
        period_start=date(2026, 9, 1),
        period_end=date(2026, 9, 30),
        tlp=TLP.AMBER,
        languages=("fr", "en"),
        target_articles=8,
        source_profile="iran-default",
    )
    group = EditorialGroup(
        edition_id=edition.id,
        title="Campagne de test",
        candidate_references=(CandidateReference(uuid4(), uuid4()),),
        outcome=GroupingOutcome.AMBIGUOUS_REVIEW,
        score=EditorialScore(
            impact=3,
            novelty=2,
            technical_depth=4,
            hunting_potential=3,
            actionability=2,
            source_quality=1,
            justifications={"source_quality": "Relations provisoires"},
        ),
        source_relationship_status=SourceRelationshipStatus.PROVISIONAL,
        needs_source_verification=True,
        needs_source_expansion=True,
        grouping_confidence=GroupingConfidence.MEDIUM,
        grouping_justification="Correspondance à revoir",
    )
    decision = HumanDecision(
        edition_id=edition.id,
        decision_type=HumanDecisionType.REJECT,
        group_ids=(group.id,),
        actor_id="dev-analyst",
        correlation_id="editorial-repository-test",
        payload={"reason": "hors périmètre"},
    )
    try:
        async with SqlAlchemyUnitOfWork(session_factory) as uow:
            assert await uow.editions.add_if_absent(edition)
            await uow.editorial_groups.add(group)
            await uow.human_decisions.append(decision)
            await uow.commit()

        async with SqlAlchemyUnitOfWork(session_factory) as uow:
            persisted = await uow.editorial_groups.get(group.id)
            decisions = await uow.human_decisions.list_for_edition(edition.id)
        assert persisted == group
        assert list(decisions) == [decision]

        with pytest.raises(DBAPIError):
            async with engine.begin() as connection:
                await connection.execute(
                    update(HumanDecisionRow)
                    .where(HumanDecisionRow.id == decision.id)
                    .values(actor_id="tampered")
                )
    finally:
        await engine.dispose()
