"""PostgreSQL contract checks for the AW-015 editorial enrichment stage."""

from __future__ import annotations

from datetime import UTC, date, datetime
from uuid import uuid4

import pytest
from sqlalchemy import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from cti_app.application.persistence import UnitOfWorkFactory
from cti_app.domain.classification import TLP
from cti_app.domain.editions import Edition
from cti_app.domain.entities import Subject
from cti_app.domain.production import (
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionArtifactStatus,
    ProductionRun,
    ProductionStage,
)
from cti_app.infrastructure.database.models.production import ProductionArtifactRow

from ..edition_codes import reserve_edition_code

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_editorial_enrichment_pipeline_contract_on_postgres(
    uow_factory: UnitOfWorkFactory,
    migrated_postgres_url: str,
) -> None:
    """A fresh migrated database accepts the stage and enforces its artifact keys."""
    edition = Edition(
        country="Editorial enrichment contract",
        country_code=reserve_edition_code(),
        period_start=date(2026, 10, 1),
        period_end=date(2026, 10, 31),
        tlp=TLP.AMBER,
        languages=("fr",),
    )
    subjects = tuple(
        Subject(
            edition_id=edition.id,
            title=f"Enrichment subject {index}",
            slug=f"editorial-enrichment-{uuid4().hex}",
            tlp=TLP.AMBER,
        )
        for index in range(2)
    )
    runs = tuple(
        ProductionRun(
            subject_id=subject.id,
            edition_id=edition.id,
            current_stage=ProductionStage.EDITORIAL_ENRICHMENT,
        )
        for subject in subjects
    )

    async with uow_factory() as uow:
        await uow.editions.add_if_absent(edition)
        for subject, run in zip(subjects, runs, strict=True):
            await uow.subjects.add(subject)
            await uow.production_runs.add(run)
            for stage in ProductionArtifactStage:
                await uow.production_artifacts.append(
                    ProductionArtifact(
                        production_run_id=run.id,
                        subject_id=subject.id,
                        stage=stage,
                        version=1,
                        input_hash="a" * 64,
                    )
                )
        await uow.commit()

    async with uow_factory() as uow:
        await uow.production_artifacts.mark_downstream_stale(
            runs[0].id, ProductionArtifactStage.SYNTHESIS.value
        )
        await uow.production_artifacts.mark_downstream_stale(
            runs[1].id, ProductionArtifactStage.EDITORIAL_ENRICHMENT.value
        )
        await uow.commit()

    async with uow_factory() as uow:
        synthesis_downstream = await uow.production_artifacts.list_for_run(runs[0].id)
        enrichment_downstream = await uow.production_artifacts.list_for_run(runs[1].id)
    assert {
        artifact.stage.value
        for artifact in synthesis_downstream
        if artifact.status is ProductionArtifactStatus.STALE
    } == {"editorial_enrichment", "publication"}
    assert {
        artifact.stage.value
        for artifact in enrichment_downstream
        if artifact.status is ProductionArtifactStatus.STALE
    } == {"publication"}

    engine = create_async_engine(migrated_postgres_url)
    try:
        async with engine.connect() as connection:
            for stage, run_index, version in (
                ("unknown_stage", 0, 99),
                (ProductionArtifactStage.EDITORIAL_ENRICHMENT.value, 1, 1),
            ):
                transaction = await connection.begin()
                with pytest.raises(IntegrityError):
                    await connection.execute(
                        insert(ProductionArtifactRow).values(
                            id=uuid4(),
                            production_run_id=runs[run_index].id,
                            subject_id=subjects[run_index].id,
                            stage=stage,
                            version=version,
                            input_hash="b" * 64,
                            status=ProductionArtifactStatus.VERIFIED.value,
                            artifact_metadata={},
                            created_at=datetime.now(UTC),
                        )
                    )
                await transaction.rollback()
    finally:
        await engine.dispose()
