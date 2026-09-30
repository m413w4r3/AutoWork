"""PostgreSQL contract checks for the editorial enrichment stage."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, date, datetime
from uuid import uuid4

import pytest
from sqlalchemy import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from cti_app.application.persistence import UnitOfWorkFactory
from cti_app.application.production_editorial_enrichment import validate_editorial_enrichment
from cti_app.domain.classification import TLP
from cti_app.domain.editions import Edition
from cti_app.domain.entities import Subject
from cti_app.domain.production import (
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionArtifactStatus,
    ProductionRun,
    ProductionRunStatus,
    ProductionStage,
)
from cti_app.domain.production_editorial_enrichment import editorial_enrichment_from_json
from cti_app.domain.production_extraction import production_extraction_from_json
from cti_app.domain.production_synthesis import production_synthesis_from_json
from cti_app.infrastructure.database.models.production import ProductionArtifactRow
from tests.integration.production.support import ProductionScenario, grounded_editorial_proposal

from ..edition_codes import reserve_edition_code
from .test_pipeline_happy_path import _configured_scenario, _install_canonical_synthesis

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_editorial_enrichment_persists_grounded_structures_on_postgres(
    production_scenario_factory: Callable[[Mapping[str, Mapping[str, object]]], ProductionScenario],
) -> None:
    scenario = await _configured_scenario(production_scenario_factory)
    _install_canonical_synthesis(scenario)
    scenario.model.script.editorial_enrichment(grounded_editorial_proposal)
    await scenario.start()
    run = await scenario.run_until_terminal()
    assert run.status is ProductionRunStatus.READY

    async with scenario.uow_factory() as uow:
        artifacts = {
            item.stage: item for item in await uow.production_artifacts.list_for_run(run.id)
        }
    enrichment_artifact = artifacts[ProductionArtifactStage.EDITORIAL_ENRICHMENT]
    assert enrichment_artifact.model_run_id is not None
    assert enrichment_artifact.raw_blob_id is not None
    assert enrichment_artifact.canonical_blob_id is not None
    assert (
        artifacts[ProductionArtifactStage.PUBLICATION].status is ProductionArtifactStatus.VERIFIED
    )

    extraction = production_extraction_from_json(
        await scenario.artifact_store.read_json(
            artifacts[ProductionArtifactStage.EXTRACTION].canonical_blob_id  # type: ignore[arg-type]
        )
    )
    synthesis = production_synthesis_from_json(
        await scenario.artifact_store.read_json(
            artifacts[ProductionArtifactStage.SYNTHESIS].canonical_blob_id  # type: ignore[arg-type]
        )
    )
    enrichment = editorial_enrichment_from_json(
        await scenario.artifact_store.read_json(enrichment_artifact.canonical_blob_id)
    )
    assert len(enrichment.tables) == len(enrichment.diagrams) == 1
    assert enrichment.source_figures == ()
    validate_editorial_enrichment(enrichment, extraction=extraction, synthesis=synthesis)
    assert enrichment.tables[0].rows[0].evidence_refs
    assert all(node.evidence_refs for node in enrichment.diagrams[0].nodes)
    assert all(edge.evidence_refs for edge in enrichment.diagrams[0].edges)
    assert len([call for call in scenario.model.calls if call.stage == "editorial_enrichment"]) == 1


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
