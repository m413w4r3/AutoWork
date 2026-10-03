"""PostgreSQL coverage for the portable V5 checkpoint and assembly resume."""

from collections.abc import Callable, Mapping
from typing import cast
from uuid import UUID

import pytest
from sqlalchemy import select

from cti_app.application.production_jobs import (
    PRODUCTION_STAGE_MAX_ATTEMPTS,
    ProductionStageParameters,
    production_stage_idempotency_key,
    stage_job_kind,
)
from cti_app.application.production_state import ProductionStateService
from cti_app.application.subject_production import SubjectProductionService
from cti_app.domain.production import (
    ProductionArtifactStage,
    ProductionArtifactStatus,
    ProductionRunStatus,
    ProductionStage,
)
from cti_app.infrastructure.database.models.model_execution import ModelRunRow
from cti_app.infrastructure.database.uow import SqlAlchemyUnitOfWork
from tests.integration.production.support import ProductionScenario, grounded_editorial_proposal

from .test_pipeline_happy_path import _configured_scenario, _install_canonical_synthesis

pytestmark = pytest.mark.integration


async def _model_run_ids(scenario: ProductionScenario) -> set[UUID]:
    async with scenario.uow_factory() as uow:
        sql_uow = cast(SqlAlchemyUnitOfWork, uow)
        assert sql_uow._session is not None
        rows = await sql_uow._session.scalars(select(ModelRunRow.id))
        return set(rows.all())


@pytest.mark.asyncio
async def test_imported_v5_state_is_directly_assemblable(
    production_scenario_factory: Callable[[Mapping[str, Mapping[str, object]]], ProductionScenario],
) -> None:
    scenario = await _configured_scenario(production_scenario_factory)
    scenario.model.script.editorial_enrichment(grounded_editorial_proposal)
    _install_canonical_synthesis(scenario)
    initial = await scenario.start()
    assert initial.status is ProductionRunStatus.RUNNING

    completed = await scenario.run_until_terminal()
    assert completed.status is ProductionRunStatus.READY
    assert completed.current_stage is ProductionStage.ASSEMBLY

    model_run_ids_before = await _model_run_ids(scenario)
    model_call_count_before = len(scenario.model.calls)
    service = ProductionStateService(scenario.uow_factory, scenario.artifact_store)
    exported = await service.export_state(subject_id=completed.subject_id)
    assert exported.schema_version == 5
    editorial_content = exported.artifacts.editorial_enrichment.canonical_content
    assert len(editorial_content["tables"]) == 1
    assert len(editorial_content["diagrams"]) == 1
    assert set(exported.artifacts.model_fields_set) == {
        "references",
        "extraction",
        "synthesis",
        "editorial_enrichment",
    }

    imported = await service.import_state(
        subject_id=completed.subject_id,
        edition_id=completed.edition_id,
        payload=exported.model_dump(mode="json"),
    )
    assert imported.status == "needs_review"
    assert imported.current_stage == "assembly"
    assert imported.schema_version == 5
    assert imported.imported_stages == (
        "references",
        "extraction",
        "synthesis",
        "editorial_enrichment",
    )

    async with scenario.uow_factory() as uow:
        imported_run = await uow.production_runs.get(imported.run_id)
        assert imported_run is not None
        assert imported_run.status is ProductionRunStatus.NEEDS_REVIEW
        assert imported_run.current_stage is ProductionStage.ASSEMBLY
        assert imported_run.error_code == "imported_production_state"
        imported_artifacts = await uow.production_artifacts.list_for_run(imported.run_id)
        assert len(imported_artifacts) == 5
        imported_by_stage = {artifact.stage: artifact for artifact in imported_artifacts}
        # The relevance projection is rebuilt deterministically on import.
        assert set(imported_by_stage) == {
            ProductionArtifactStage.REFERENCES,
            ProductionArtifactStage.EXTRACTION,
            ProductionArtifactStage.RELEVANCE_PROJECTION,
            ProductionArtifactStage.SYNTHESIS,
            ProductionArtifactStage.EDITORIAL_ENRICHMENT,
        }
        assert all(
            artifact.status is ProductionArtifactStatus.VERIFIED
            and artifact.canonical_blob_id is not None
            for artifact in imported_artifacts
        )
        imported_editorial = imported_by_stage[ProductionArtifactStage.EDITORIAL_ENRICHMENT]
        imported_content = await scenario.artifact_store.read_json(
            imported_editorial.canonical_blob_id  # type: ignore[arg-type]
        )
        assert imported_content == editorial_content
        assert await uow.analyst_investigations.get_for_run(imported.run_id) is None

    upstream_artifact_ids = {stage: imported_by_stage[stage].id for stage in imported_by_stage}
    retry = await SubjectProductionService(scenario.uow_factory).retry_from_stage(
        imported.run_id, ProductionStage.ASSEMBLY
    )
    assert retry.run.current_stage is ProductionStage.ASSEMBLY
    parameters = ProductionStageParameters(
        run_id=retry.run.id,
        expected_stage=ProductionStage.ASSEMBLY.value,
        pipeline_generation=retry.run.pipeline_generation,
    )
    job = await scenario.jobs.submit(
        kind=stage_job_kind(ProductionStage.ASSEMBLY),
        aggregate_type="subject",
        aggregate_id=retry.run.subject_id,
        idempotency_key=production_stage_idempotency_key(retry.run, ProductionStage.ASSEMBLY),
        correlation_id="production-state-v5-integration",
        input_parameters=parameters.model_dump(mode="json"),
        max_attempts=PRODUCTION_STAGE_MAX_ATTEMPTS,
        actor_id="production-state-v5-integration",
    )
    await scenario.runner.dispatch(job.id)
    await scenario.runner.run_until_idle()

    async with scenario.uow_factory() as uow:
        ready = await uow.production_runs.get(imported.run_id)
        assert ready is not None
        artifacts_after_assembly = await uow.production_artifacts.list_for_run(imported.run_id)
        assert len(artifacts_after_assembly) == 6
        by_stage_after_assembly = {
            artifact.stage: artifact for artifact in artifacts_after_assembly
        }
        completed_job = await uow.jobs.get(job.id)
    assert ready.status is ProductionRunStatus.READY
    assert ready.current_stage is ProductionStage.ASSEMBLY
    assert ready.error_code is None
    # Assembly marks READY only after ProductionQAService returns passed=True.
    assert completed_job is not None
    assert completed_job.output_reference == (f"production-stage://{imported.run_id}/assembly")
    assert set(by_stage_after_assembly) == {
        ProductionArtifactStage.REFERENCES,
        ProductionArtifactStage.EXTRACTION,
        ProductionArtifactStage.RELEVANCE_PROJECTION,
        ProductionArtifactStage.SYNTHESIS,
        ProductionArtifactStage.EDITORIAL_ENRICHMENT,
        ProductionArtifactStage.PUBLICATION,
    }
    assert all(
        by_stage_after_assembly[stage].id == artifact_id
        for stage, artifact_id in upstream_artifact_ids.items()
    )
    publication = by_stage_after_assembly[ProductionArtifactStage.PUBLICATION]
    assert publication.status is ProductionArtifactStatus.VERIFIED
    assert publication.canonical_blob_id is not None
    assert await _model_run_ids(scenario) == model_run_ids_before
    assert len(scenario.model.calls) == model_call_count_before
