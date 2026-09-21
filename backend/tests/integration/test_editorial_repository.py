from datetime import date
from uuid import uuid4

import pytest
from sqlalchemy import update
from sqlalchemy.exc import DBAPIError

from cti_app.domain.classification import TLP
from cti_app.domain.editions import Edition
from cti_app.domain.editorial import (
    HumanDecision,
    HumanDecisionType,
)
from cti_app.infrastructure.database.models.editorial import HumanDecisionRow
from cti_app.infrastructure.database.session import create_postgres_engine, create_session_factory
from cti_app.infrastructure.database.uow import SqlAlchemyUnitOfWork

pytestmark = pytest.mark.integration


async def test_human_decision_round_trip_is_append_only(
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
    )
    subject_id = uuid4()
    decision = HumanDecision(
        edition_id=edition.id,
        # Selection decisions no longer live here (AW-008): use a decision type
        # still emitted by application/collection_review.py.
        decision_type=HumanDecisionType.CLAIM_REJECT,
        subject_ids=(subject_id,),
        actor_id="dev-analyst",
        correlation_id="editorial-repository-test",
        payload={"claim_id": str(uuid4()), "reason": "affirmation non étayée"},
    )
    try:
        async with SqlAlchemyUnitOfWork(session_factory) as uow:
            assert await uow.editions.add_if_absent(edition)
            await uow.human_decisions.append(decision)
            await uow.commit()

        async with SqlAlchemyUnitOfWork(session_factory) as uow:
            decisions = await uow.human_decisions.list_for_edition(edition.id)
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
