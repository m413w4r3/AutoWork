"""PostgreSQL proof for publication review FKs and append-only protection."""

from __future__ import annotations

from datetime import UTC, date, datetime
from uuid import uuid4

import pytest
from sqlalchemy import event, text, update
from sqlalchemy.exc import DBAPIError, IntegrityError

from cti_app.domain.classification import TLP
from cti_app.infrastructure.database.models.core import SubjectRow
from cti_app.infrastructure.database.models.edition_composition import (
    EditionCompositionEntryRow,
    EditionCompositionRow,
)
from cti_app.infrastructure.database.models.editions import EditionRow
from cti_app.infrastructure.database.models.production import (
    EditionProductionBatchItemRow,
    EditionProductionBatchRow,
    ProductionArtifactRow,
    ProductionRunRow,
)
from cti_app.infrastructure.database.models.publication_review import (
    PublicationReviewDecisionRow,
)
from cti_app.infrastructure.database.repositories.edition_composition import (
    SqlAlchemyEditionCompositionRepository,
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
    corrected_artifact_id = uuid4()
    p2_run_id = uuid4()
    p2_artifact_id = uuid4()
    p3_run_id = uuid4()
    p3_artifact_id = uuid4()
    extraction_artifact_id = uuid4()
    decision_id = uuid4()
    no_document_subject_id = uuid4()
    no_document_run_id = uuid4()
    no_document_decision_id = uuid4()
    unaddressed_subject_id = uuid4()
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
                    state="open",
                    version=1,
                    created_at=now,
                    updated_at=now,
                )
            )
            # subjects.edition_id is FK-backed: persist the Edition first.
            await session.flush()
            session.add(
                SubjectRow(
                    id=subject_id,
                    edition_id=edition_id,
                    title="Subject review title",
                    slug=f"review-{uuid4().hex}",
                    tlp=TLP.GREEN.value,
                    version=1,
                    created_at=now,
                    updated_at=now,
                )
            )
            session.add(
                ProductionRunRow(
                    id=run_id,
                    subject_id=subject_id,
                    edition_id=edition_id,
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
                    id=extraction_artifact_id,
                    production_run_id=run_id,
                    subject_id=subject_id,
                    stage="extraction",
                    version=1,
                    input_hash="b" * 64,
                    status="verified",
                    artifact_metadata={
                        "element_counts": {"rules": 3},
                        "deterministic_verification": {
                            "q2_rejected_rule_count": 2,
                            "q2_rejected_artifact_count": 7,
                        },
                    },
                    created_at=now,
                )
            )
            session.add(
                ProductionArtifactRow(
                    id=artifact_id,
                    production_run_id=run_id,
                    subject_id=subject_id,
                    stage="publication",
                    version=1,
                    input_hash="a" * 64,
                    status="verified",
                    artifact_metadata={},
                    created_at=now,
                )
            )
            session.add_all(
                [
                    ProductionArtifactRow(
                        id=corrected_artifact_id,
                        production_run_id=run_id,
                        subject_id=subject_id,
                        stage="publication",
                        version=2,
                        input_hash="c" * 64,
                        status="verified",
                        artifact_metadata={},
                        created_at=now,
                    ),
                    ProductionRunRow(
                        id=p2_run_id,
                        subject_id=subject_id,
                        edition_id=edition_id,
                        status="ready",
                        current_stage="assembly",
                        run_number=2,
                        pipeline_generation=1,
                        research_date=date(2099, 12, 1),
                        created_at=now,
                        updated_at=now,
                        version=1,
                    ),
                    ProductionRunRow(
                        id=p3_run_id,
                        subject_id=subject_id,
                        edition_id=edition_id,
                        status="ready",
                        current_stage="assembly",
                        run_number=3,
                        pipeline_generation=1,
                        research_date=date(2099, 12, 1),
                        created_at=now,
                        updated_at=now,
                        version=1,
                    ),
                ]
            )
            await session.flush()
            session.add_all(
                [
                    ProductionArtifactRow(
                        id=p2_artifact_id,
                        production_run_id=p2_run_id,
                        subject_id=subject_id,
                        stage="publication",
                        version=1,
                        input_hash="d" * 64,
                        status="verified",
                        artifact_metadata={},
                        created_at=now,
                    ),
                    ProductionArtifactRow(
                        id=p3_artifact_id,
                        production_run_id=p3_run_id,
                        subject_id=subject_id,
                        stage="publication",
                        version=1,
                        input_hash="e" * 64,
                        status="verified",
                        artifact_metadata={},
                        created_at=now,
                    ),
                ]
            )
            await session.flush()
            session.add(
                SubjectRow(
                    id=no_document_subject_id,
                    edition_id=edition_id,
                    title="Subject without document title",
                    slug=f"review-no-document-{uuid4().hex}",
                    tlp=TLP.GREEN.value,
                    version=1,
                    created_at=now,
                    updated_at=now,
                )
            )
            session.add(
                SubjectRow(
                    id=unaddressed_subject_id,
                    edition_id=edition_id,
                    title="Subject without a composition entry",
                    slug=f"review-unaddressed-{uuid4().hex}",
                    tlp=TLP.GREEN.value,
                    version=1,
                    created_at=now,
                    updated_at=now,
                )
            )
            session.add(
                ProductionRunRow(
                    id=no_document_run_id,
                    subject_id=no_document_subject_id,
                    edition_id=edition_id,
                    status="failed",
                    current_stage="synthesis",
                    run_number=1,
                    pipeline_generation=3,
                    research_date=date(2099, 12, 1),
                    created_at=now,
                    updated_at=now,
                    version=1,
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
                    decision="include",
                    actor_id="analyst",
                    reason="accepted exact artifact",
                    occurred_at=now,
                )
            )
            await session.commit()
            session.add(
                PublicationReviewDecisionRow(
                    id=no_document_decision_id,
                    edition_id=edition_id,
                    subject_id=no_document_subject_id,
                    production_run_id=no_document_run_id,
                    pipeline_generation=3,
                    document_artifact_id=None,
                    document_artifact_version=None,
                    document_input_hash=None,
                    decision="exclude",
                    actor_id="analyst",
                    reason="No publication was produced",
                    occurred_at=now,
                )
            )
            await session.commit()

            composition_id = uuid4()
            session.add(
                EditionCompositionRow(
                    id=composition_id,
                    edition_id=edition_id,
                    version=1,
                    preparation_status="unassessed",
                    actor_id="analyst",
                    created_at=now,
                    updated_at=now,
                )
            )
            await session.flush()
            session.add_all(
                [
                    EditionCompositionEntryRow(
                        id=uuid4(),
                        composition_id=composition_id,
                        subject_id=subject_id,
                        disposition="included",
                        position=1,
                        production_run_id=run_id,
                        pipeline_generation=2,
                        artifact_id=artifact_id,
                        artifact_version=1,
                        artifact_input_hash="a" * 64,
                        preparation_status="unassessed",
                    ),
                    EditionCompositionEntryRow(
                        id=uuid4(),
                        composition_id=composition_id,
                        subject_id=no_document_subject_id,
                        disposition="excluded",
                        review_decision_id=no_document_decision_id,
                        preparation_status="unassessed",
                    ),
                ]
            )
            await session.commit()

            batch_id = uuid4()
            session.add(
                EditionProductionBatchRow(
                    id=batch_id,
                    edition_id=edition_id,
                    status="completed_with_issues",
                    phase="review",
                    idempotency_key=f"fixture-{batch_id.hex}",
                    request_fingerprint="0" * 64,
                    actor_id="fixture",
                    correlation_id="fixture",
                    version=1,
                    created_at=now,
                )
            )
            await session.flush()
            session.add_all(
                [
                    EditionProductionBatchItemRow(
                        id=uuid4(),
                        batch_id=batch_id,
                        subject_id=subject_id,
                        production_run_id=run_id,
                        position=1,
                        created_at=now,
                    ),
                    EditionProductionBatchItemRow(
                        id=uuid4(),
                        batch_id=batch_id,
                        subject_id=no_document_subject_id,
                        production_run_id=no_document_run_id,
                        position=2,
                        created_at=now,
                    ),
                ]
            )
            await session.commit()

            later_batches = [uuid4(), uuid4()]
            session.add_all(
                [
                    EditionProductionBatchRow(
                        id=later_batch_id,
                        edition_id=edition_id,
                        status="completed",
                        phase="review",
                        idempotency_key=f"fixture-{later_batch_id.hex}",
                        request_fingerprint="1" * 64,
                        actor_id="fixture",
                        correlation_id="fixture",
                        version=1,
                        created_at=now,
                    )
                    for later_batch_id in later_batches
                ]
            )
            await session.flush()
            session.add_all(
                [
                    EditionProductionBatchItemRow(
                        id=uuid4(),
                        batch_id=later_batches[0],
                        subject_id=subject_id,
                        production_run_id=p2_run_id,
                        position=1,
                        created_at=now,
                    ),
                    EditionProductionBatchItemRow(
                        id=uuid4(),
                        batch_id=later_batches[1],
                        subject_id=subject_id,
                        production_run_id=p3_run_id,
                        position=1,
                        created_at=now,
                    ),
                ]
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
                if statement.lstrip().upper().startswith(("SELECT", "WITH")):
                    select_statements.append(statement)

            event.listen(engine.sync_engine, "before_cursor_execute", count_selects)
            try:
                review_rows = await SqlAlchemyEditionReviewReadRepository(session).list_for_edition(
                    edition_id
                )
            finally:
                event.remove(engine.sync_engine, "before_cursor_execute", count_selects)
            assert len(select_statements) == 1
            assert len(review_rows.items) == 1
            assert review_rows.composition_version == 1
            with_document = review_rows.items[0]
            assert with_document.effective_decision is not None
            assert with_document.effective_decision.value == "include"
            assert with_document.effective_decision_id == decision_id
            assert with_document.title == "Subject review title"
            assert with_document.document_artifact_id == artifact_id
            assert with_document.rejected_indicator_count == 7
            assert with_document.rejected_rule_count == 2
            assert with_document.published_rule_count == 3
            assert with_document.document_artifact_version == 1
            assert with_document.superseded_artifact_available is True
            assert with_document.latest_artifact_id == corrected_artifact_id
            exclusion = next(
                diagnostic
                for diagnostic in review_rows.diagnostics
                if diagnostic.subject_id == no_document_subject_id
            )
            assert exclusion.code.value == "subject_excluded"
            assert exclusion.run_id is None
            assert exclusion.details == {"title": "Subject without document title"}
            unaddressed = next(
                diagnostic
                for diagnostic in review_rows.diagnostics
                if diagnostic.subject_id == unaddressed_subject_id
            )
            assert unaddressed.code.value == "subject_unaddressed"
            assert unaddressed.details == {"title": "Subject without a composition entry"}

            await session.execute(
                update(EditionCompositionRow)
                .where(EditionCompositionRow.id == composition_id)
                .values(version=2)
            )
            await session.execute(
                update(EditionCompositionEntryRow)
                .where(
                    EditionCompositionEntryRow.composition_id == composition_id,
                    EditionCompositionEntryRow.subject_id == subject_id,
                )
                .values(
                    artifact_id=corrected_artifact_id,
                    artifact_version=2,
                    artifact_input_hash="c" * 64,
                )
            )
            await session.commit()
            corrected_scope = await SqlAlchemyEditionReviewReadRepository(session).list_for_edition(
                edition_id
            )
            corrected = corrected_scope.items[0]
            assert corrected_scope.composition_version == 2
            assert corrected.document_artifact_id == corrected_artifact_id
            assert corrected.document_artifact_version == 2
            assert corrected.effective_decision_id is None
            assert corrected.effective_decision is None
            assert corrected.superseded_artifact_available is False

            composition_repository = SqlAlchemyEditionCompositionRepository(session)
            composition = await composition_repository.get_for_edition(edition_id)
            assert composition is not None
            references = await composition_repository.validate_references(
                edition_id,
                composition.entries,
                lock=False,
            )
            pinned_state = references.entries[subject_id]
            assert pinned_state.latest_artifact_run_id == run_id
            assert pinned_state.latest_artifact_version == 2

            missing_edition_id = uuid4()
            session.add(
                EditionRow(
                    id=missing_edition_id,
                    country="Reviewland",
                    country_code="ZY",
                    period_start=date(2099, 12, 1),
                    period_end=date(2099, 12, 31),
                    tlp=TLP.GREEN.value,
                    languages=["fr"],
                    state="open",
                    version=1,
                    created_at=now,
                    updated_at=now,
                )
            )
            await session.commit()
            missing_scope = await SqlAlchemyEditionReviewReadRepository(session).list_for_edition(
                missing_edition_id
            )
            assert missing_scope.items == ()
            assert missing_scope.composition_version is None
            assert [diagnostic.code.value for diagnostic in missing_scope.diagnostics] == [
                "composition_missing"
            ]

            with pytest.raises(DBAPIError, match="append-only"):
                await session.execute(
                    text(
                        "UPDATE publication_review_decisions SET reason = 'changed' WHERE id = :id"
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
            await session.rollback()

            with pytest.raises(IntegrityError):
                await session.execute(
                    text(
                        "INSERT INTO publication_review_decisions "
                        "(id, edition_id, subject_id, production_run_id, pipeline_generation, "
                        "document_artifact_id, document_artifact_version, document_input_hash, "
                        "decision, actor_id, reason, occurred_at) "
                        "VALUES (:id, :edition_id, :subject_id, NULL, NULL, :artifact_id, 1, "
                        ":hash, 'include', 'analyst', NULL, :occurred_at)"
                    ),
                    {
                        "id": uuid4(),
                        "edition_id": edition_id,
                        "subject_id": subject_id,
                        "artifact_id": artifact_id,
                        "hash": "a" * 64,
                        "occurred_at": now,
                    },
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
                        "run_id": run_id,
                        "artifact_id": uuid4(),
                        "hash": "a" * 64,
                        "occurred_at": now,
                    },
                )
            await session.rollback()

            with pytest.raises(IntegrityError):
                await session.execute(
                    text(
                        "INSERT INTO publication_review_decisions "
                        "(id, edition_id, subject_id, production_run_id, pipeline_generation, "
                        "document_artifact_id, document_artifact_version, document_input_hash, "
                        "decision, actor_id, reason, occurred_at) "
                        "VALUES (:id, :edition_id, :subject_id, :run_id, 2, NULL, NULL, NULL, "
                        "'include', 'analyst', NULL, :occurred_at)"
                    ),
                    {
                        "id": uuid4(),
                        "edition_id": edition_id,
                        "subject_id": subject_id,
                        "run_id": run_id,
                        "occurred_at": now,
                    },
                )
    finally:
        await engine.dispose()
