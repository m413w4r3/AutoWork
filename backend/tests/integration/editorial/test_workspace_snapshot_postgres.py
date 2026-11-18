from __future__ import annotations

from datetime import date
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.exc import DBAPIError, IntegrityError

from cti_app.domain.classification import TLP
from cti_app.domain.editions import Edition
from cti_app.domain.entities import Subject
from cti_app.domain.production import ProductionArtifact, ProductionArtifactStage, ProductionRun
from cti_app.infrastructure.database.models.editorial_workspace import (
    EditorialWorkspaceSnapshotRow,
)
from cti_app.infrastructure.database.session import create_postgres_engine, create_session_factory
from cti_app.infrastructure.database.uow import SqlAlchemyUnitOfWork

pytestmark = pytest.mark.integration


def _snapshot_row(
    *,
    subject_id: UUID,
    run_id: UUID,
    artifact_id: UUID,
    workspace_pin_hash: str,
) -> EditorialWorkspaceSnapshotRow:
    return EditorialWorkspaceSnapshotRow(
        snapshot_id=uuid4(),
        subject_id=subject_id,
        production_run_id=run_id,
        synthesis_artifact_id=artifact_id,
        synthesis_version=1,
        synthesis_input_hash="1" * 64,
        synthesis_canonical_sha256="2" * 64,
        enrichment_artifact_id=None,
        enrichment_version=None,
        enrichment_input_hash=None,
        enrichment_canonical_sha256=None,
        publication_artifact_id=None,
        publication_version=None,
        publication_input_hash=None,
        publication_canonical_sha256=None,
        pipeline_generation=0,
        routing_decision_id=None,
        routing_decision_hash=None,
        publication_kind=None,
        workspace_pin_hash=workspace_pin_hash,
        registry_hash="3" * 64,
        block_registry=[],
        retired_block_ids=[],
        parent_snapshot_id=None,
    )


async def test_snapshot_postgres_is_unique_append_only(
    migrated_postgres_url: str,
) -> None:
    engine = create_postgres_engine(migrated_postgres_url)
    session_factory = create_session_factory(engine)
    edition = Edition(
        country="France",
        country_code="FR",
        period_start=date(2026, 11, 1),
        period_end=date(2026, 11, 30),
        tlp=TLP.AMBER,
        languages=("fr",),
    )
    try:
        async with SqlAlchemyUnitOfWork(session_factory) as uow:
            await uow.editions.add_if_absent(edition)
            subject = Subject(
                edition_id=edition.id,
                title="Synthetic workspace subject",
                slug=f"workspace-{uuid4().hex[:10]}",
                tlp=TLP.AMBER,
            )
            await uow.subjects.add(subject)
            run = ProductionRun(subject_id=subject.id, edition_id=edition.id)
            await uow.production_runs.add(run)
            artifact = ProductionArtifact(
                production_run_id=run.id,
                subject_id=subject.id,
                stage=ProductionArtifactStage.SYNTHESIS,
                version=1,
                input_hash="4" * 64,
            )
            await uow.production_artifacts.append(artifact)
            await uow.commit()

        first = _snapshot_row(
            subject_id=subject.id,
            run_id=run.id,
            artifact_id=artifact.id,
            workspace_pin_hash="5" * 64,
        )
        async with session_factory() as session, session.begin():
            session.add(first)
        duplicate = _snapshot_row(
            subject_id=subject.id,
            run_id=run.id,
            artifact_id=artifact.id,
            workspace_pin_hash="5" * 64,
        )
        with pytest.raises(IntegrityError):
            async with session_factory() as session, session.begin():
                session.add(duplicate)
                await session.flush()

        with pytest.raises(DBAPIError):
            async with session_factory() as session, session.begin():
                await session.execute(
                    update(EditorialWorkspaceSnapshotRow)
                    .where(EditorialWorkspaceSnapshotRow.snapshot_id == first.snapshot_id)
                    .values(registry_hash="6" * 64)
                )
        with pytest.raises(DBAPIError):
            async with session_factory() as session, session.begin():
                await session.execute(
                    delete(EditorialWorkspaceSnapshotRow).where(
                        EditorialWorkspaceSnapshotRow.snapshot_id == first.snapshot_id
                    )
                )
        with pytest.raises(DBAPIError):
            async with session_factory() as session, session.begin():
                await session.execute(text("TRUNCATE TABLE editorial_workspace_snapshots"))

        rolled_back = _snapshot_row(
            subject_id=subject.id,
            run_id=run.id,
            artifact_id=artifact.id,
            workspace_pin_hash="7" * 64,
        )
        with pytest.raises(RuntimeError, match="synthetic rollback"):
            async with session_factory() as session, session.begin():
                session.add(rolled_back)
                await session.flush()
                raise RuntimeError("synthetic rollback")
        async with session_factory() as session:
            count = await session.scalar(
                select(func.count())
                .select_from(EditorialWorkspaceSnapshotRow)
                .where(EditorialWorkspaceSnapshotRow.snapshot_id == rolled_back.snapshot_id)
            )
        assert count == 0
    finally:
        await engine.dispose()
