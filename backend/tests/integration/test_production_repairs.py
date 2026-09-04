"""PostgreSQL proof for production repair decision persistence."""

from __future__ import annotations

from datetime import UTC, date, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

from cti_app.application.persistence import UnitOfWorkFactory
from cti_app.application.production_repairs import repair_key_for_rejection
from cti_app.domain.classification import TLP
from cti_app.domain.editions import Edition, EditionStatus
from cti_app.domain.entities import Subject
from cti_app.domain.production import (
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionRepairAction,
    ProductionRepairDecision,
    ProductionRepairIssueKind,
    SubjectProductionRun,
)

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_production_repair_decisions_are_fk_backed_append_only_and_effective(
    uow_factory: UnitOfWorkFactory,
    migrated_postgres_url: str,
) -> None:
    from cti_app.infrastructure.database.session import (
        create_postgres_engine,
        create_session_factory,
    )

    edition = Edition(
        country="Repairland",
        country_code="RX",
        period_start=date(2098, 1, 1),
        period_end=date(2098, 1, 31),
        tlp=TLP.GREEN,
        languages=("fr",),
        target_articles=1,
        source_profile="test",
        status=EditionStatus.REVIEW,
    )
    subject = Subject(
        external_id=f"repair-{uuid4().hex}",
        slug=f"repair-{uuid4().hex}",
        tlp=TLP.GREEN,
    )
    run = SubjectProductionRun(subject_id=subject.id, edition_id=edition.id)
    artifact = ProductionArtifact(
        production_run_id=run.id,
        subject_id=subject.id,
        stage=ProductionArtifactStage.EXTRACTION,
        version=1,
        input_hash="a" * 64,
    )
    repair_key = repair_key_for_rejection(
        edition_id=edition.id,
        subject_id=subject.id,
        kind=ProductionRepairIssueKind.REJECTED_RULE,
        source_url="https://example.test/report",
        artifact_type="sigma",
        value="rule body",
    )
    now = datetime(2098, 1, 2, tzinfo=UTC)
    first = ProductionRepairDecision(
        id=UUID("00000000-0000-0000-0000-000000000001"),
        edition_id=edition.id,
        subject_id=subject.id,
        production_run_id=run.id,
        observed_artifact_id=artifact.id,
        observed_pipeline_generation=run.pipeline_generation,
        repair_key=repair_key,
        issue_kind=ProductionRepairIssueKind.REJECTED_RULE,
        action=ProductionRepairAction.EXCLUDE,
        actor_id="analyst",
        created_at=now,
    )
    second = ProductionRepairDecision(
        id=UUID("00000000-0000-0000-0000-000000000002"),
        edition_id=edition.id,
        subject_id=subject.id,
        production_run_id=run.id,
        observed_artifact_id=artifact.id,
        observed_pipeline_generation=run.pipeline_generation,
        repair_key=repair_key,
        issue_kind=ProductionRepairIssueKind.REJECTED_RULE,
        action=ProductionRepairAction.INCLUDE,
        actor_id="reviewer",
        created_at=now,
    )

    try:
        async with uow_factory() as uow:
            assert await uow.editions.add_if_absent(edition)
            await uow.subjects.add(subject)
            await uow.subject_production_runs.add(run)
            await uow.production_artifacts.append(artifact)
            await uow.production_repair_decisions.append(first)
            await uow.production_repair_decisions.append(second)
            await uow.commit()

        async with uow_factory() as uow:
            effective = await uow.production_repair_decisions.effective_decisions(edition.id)
            assert effective == (second,)

        engine = create_postgres_engine(migrated_postgres_url)
        session_factory = create_session_factory(engine)
        try:
            async with session_factory() as session:
                with pytest.raises(DBAPIError, match="append-only"):
                    await session.execute(
                        text(
                            "UPDATE production_repair_decisions SET reason = 'changed' "
                            "WHERE id = :id"
                        ),
                        {"id": first.id},
                    )
                await session.rollback()
                # Rewriting the arbitration itself is the dangerous edit: an
                # audit that can be flipped after the fact is not an audit.
                with pytest.raises(DBAPIError, match="append-only"):
                    await session.execute(
                        text(
                            "UPDATE production_repair_decisions SET action = 'exclude' "
                            "WHERE id = :id"
                        ),
                        {"id": second.id},
                    )
                await session.rollback()
                with pytest.raises(DBAPIError, match="append-only"):
                    await session.execute(
                        text("DELETE FROM production_repair_decisions WHERE id = :id"),
                        {"id": first.id},
                    )
                await session.rollback()
                with pytest.raises(DBAPIError, match="append-only"):
                    await session.execute(
                        text(
                            "DELETE FROM production_repair_decisions WHERE edition_id = :edition_id"
                        ),
                        {"edition_id": edition.id},
                    )
                await session.rollback()
                with pytest.raises(IntegrityError):
                    await session.execute(
                        text(
                            "INSERT INTO production_repair_decisions "
                            "(id, edition_id, subject_id, production_run_id, "
                            "observed_artifact_id, repair_key, issue_kind, action, "
                            "observed_pipeline_generation, actor_id, created_at) "
                            "VALUES (:id, :edition_id, :subject_id, :run_id, :artifact_id, "
                            ":repair_key, 'rejected_rule', 'include', 0, 'analyst', :created_at)"
                        ),
                        {
                            "id": uuid4(),
                            "edition_id": edition.id,
                            "subject_id": uuid4(),
                            "run_id": run.id,
                            "artifact_id": artifact.id,
                            "repair_key": repair_key,
                            "created_at": now,
                        },
                    )
        finally:
            await engine.dispose()
    finally:
        # The integration fixture drops the database; this inner finally only
        # keeps the test structure explicit around the engine lifecycle.
        pass
