"""PostgreSQL proof for publication review FKs and append-only protection."""

from __future__ import annotations

from datetime import UTC, date, datetime
from uuid import uuid4

import pytest
from sqlalchemy import event, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from cti_app.domain.classification import TLP
from cti_app.infrastructure.database.models.core import SubjectRow
from cti_app.infrastructure.database.models.editions import EditionRow
from cti_app.infrastructure.database.models.production import (
    EditionProductionBatchItemRow,
    EditionProductionBatchRow,
    ProductionArtifactRow,
    SubjectProductionRunRow,
)
from cti_app.infrastructure.database.models.publication_review import (
    PublicationReviewDecisionRow,
)
from cti_app.infrastructure.database.repositories.publication_review import (
    SqlAlchemyEditionReviewReadRepository,
)

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_publication_review_is_fk_backed_and_append_only(migrated_postgres_url: str) -> None:
    from cti_app.infrastructure.database.session import (
        create_postgres_engine,
        create_session_factory,
    )

    engine = create_postgres_engine(migrated_postgres_url)
    session_factory = create_session_factory(engine)
    now = datetime.now(UTC)
    edition_id = uuid4()
    subject_id = uuid4()
    run_id = uuid4()
    artifact_id = uuid4()
    decision_id = uuid4()
    try:
        async with session_factory() as session:
            session.add(
                EditionRow(
                    id=edition_id,
                    country="Reviewland",
                    country_code="ZZ",
                    period_start=date(2099, 12, 1),
                    period_end=date(2099, 12, 31),
                    tlp=TLP.GREEN.value,
                    languages=["fr"],
                    target_major_articles=0,
                    target_briefs=1,
                    source_profile="test",
                    status="review",
                    version=1,
                    created_at=now,
                    updated_at=now,
                )
            )
            session.add(
                SubjectRow(
                    id=subject_id,
                    external_id=f"review-{uuid4().hex}",
                    slug=f"review-{uuid4().hex}",
                    tlp=TLP.GREEN.value,
                    created_at=now,
                )
            )
            session.add(
                SubjectProductionRunRow(
                    id=run_id,
                    subject_id=subject_id,
                    edition_id=edition_id,
                    profile="brief_auto",
                    status="ready",
                    current_stage="assembly",
                    run_number=1,
                    pipeline_generation=2,
                    research_date=date(2099, 12, 1),
                    created_at=now,
                    updated_at=now,
                    version=1,
                )
            )
            await session.flush()
            session.add(
                ProductionArtifactRow(
                    id=artifact_id,
                    production_run_id=run_id,
                    subject_id=subject_id,
                    stage="brief",
                    version=1,
                    input_hash="a" * 64,
                    status="verified",
                    artifact_metadata={},
                    created_at=now,
                )
            )
            await session.flush()
            session.add(
                PublicationReviewDecisionRow(
                    id=decision_id,
                    edition_id=edition_id,
                    subject_id=subject_id,
                    production_run_id=run_id,
                    pipeline_generation=2,
                    document_artifact_id=artifact_id,
                    document_artifact_version=1,
                    document_input_hash="a" * 64,
                    decision="exclude",
                    actor_id="analyst",
                    reason="not publishable",
                    occurred_at=now,
                )
            )
            await session.commit()

            batch_id = uuid4()
            session.add(
                EditionProductionBatchRow(
                    id=batch_id,
                    edition_id=edition_id,
                    profile="brief_auto",
                    status="running",
                    phase="review",
                    version=1,
                    created_at=now,
                )
            )
            await session.flush()
            session.add(
                EditionProductionBatchItemRow(
                    id=uuid4(),
                    batch_id=batch_id,
                    subject_id=subject_id,
                    production_run_id=run_id,
                    position=1,
                    created_at=now,
                )
            )
            await session.commit()

            select_statements: list[str] = []

            def count_selects(
                _connection: object,
                _cursor: object,
                statement: str,
                _parameters: object,
                _context: object,
                _executemany: bool,
            ) -> None:
                if statement.lstrip().upper().startswith("SELECT"):
                    select_statements.append(statement)

            event.listen(engine.sync_engine, "before_cursor_execute", count_selects)
            try:
                review_rows = await SqlAlchemyEditionReviewReadRepository(session).list_for_edition(
                    edition_id
                )
            finally:
                event.remove(engine.sync_engine, "before_cursor_execute", count_selects)
            assert len(select_statements) == 1
            assert len(review_rows) == 1
            assert review_rows[0].effective_decision is not None
            assert review_rows[0].effective_decision.value == "exclude"
            assert review_rows[0].document_artifact_id == artifact_id

            with pytest.raises(DBAPIError, match="append-only"):
                await session.execute(
                    text(
                        "UPDATE publication_review_decisions "
                        "SET reason = 'changed' WHERE id = :id"
                    ),
                    {"id": decision_id},
                )
            await session.rollback()

            with pytest.raises(DBAPIError, match="append-only"):
                await session.execute(
                    text("DELETE FROM publication_review_decisions WHERE id = :id"),
                    {"id": decision_id},
                )
            await session.rollback()

            with pytest.raises(IntegrityError):
                await session.execute(
                    text(
                        "INSERT INTO publication_review_decisions "
                        "(id, edition_id, subject_id, production_run_id, pipeline_generation, "
                        "document_artifact_id, document_artifact_version, document_input_hash, "
                        "decision, actor_id, reason, occurred_at) "
                        "VALUES (:id, :edition_id, :subject_id, :run_id, 2, :artifact_id, 1, "
                        ":hash, 'include', 'analyst', NULL, :occurred_at)"
                    ),
                    {
                        "id": uuid4(),
                        "edition_id": edition_id,
                        "subject_id": subject_id,
                        "run_id": uuid4(),
                        "artifact_id": artifact_id,
                        "hash": "a" * 64,
                        "occurred_at": now,
                    },
                )
    finally:
        await engine.dispose()
