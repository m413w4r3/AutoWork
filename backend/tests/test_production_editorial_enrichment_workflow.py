"""The workflow adapts canonical enrichment service outcomes to stage results."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from cti_app.application.production_editorial_enrichment import (
    EditorialEnrichmentExecutionStatus,
)
from cti_app.application.production_workflow import ProductionWorkflowOrchestrator
from cti_app.domain.production import (
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionRun,
    ProductionStage,
)
from cti_app.domain.production_extraction import production_extraction_to_json
from cti_app.domain.production_synthesis import production_synthesis_to_json
from tests.test_production_synthesis_storage import (
    MemoryStore,
    MemoryUow,
    canonical_pair,
    uow_factory,
)


@pytest.mark.asyncio
async def test_enrichment_without_canonical_service_is_blocked_without_empty_artifact() -> None:
    subject_id, run_id = uuid4(), uuid4()
    run = ProductionRun(
        id=run_id,
        subject_id=subject_id,
        edition_id=uuid4(),
        current_stage=ProductionStage.EDITORIAL_ENRICHMENT,
    )
    uow, store = MemoryUow(), MemoryStore()
    workflow = ProductionWorkflowOrchestrator(
        uow_factory(uow), model_gateway=None, artifact_store=store
    )

    result = await workflow._execute_editorial_enrichment_stage(
        run,
        snapshot=SimpleNamespace(subject_id=subject_id, input_hash="a" * 64),
    )

    assert result["status"] == "terminal_error"
    assert result["error_code"] == "editorial_enrichment_service_unavailable"
    assert (
        await uow.production_artifacts.get_current(
            run_id, ProductionArtifactStage.EDITORIAL_ENRICHMENT.value
        )
        is None
    )


@pytest.mark.asyncio
async def test_enrichment_workflow_maps_canonical_service_result() -> None:
    subject_id, run_id = uuid4(), uuid4()
    synthesis, extraction = canonical_pair(subject_id)
    store, uow = MemoryStore(), MemoryUow()
    _, extraction_blob, _ = await store.store_stage_payloads(
        canonical=production_extraction_to_json(extraction)
    )
    _, synthesis_blob, _ = await store.store_stage_payloads(
        canonical=production_synthesis_to_json(synthesis)
    )
    assert extraction_blob is not None and synthesis_blob is not None
    upstream = {}
    for stage, blob_id in (
        (ProductionArtifactStage.EXTRACTION, extraction_blob),
        (ProductionArtifactStage.SYNTHESIS, synthesis_blob),
    ):
        artifact = ProductionArtifact(
            production_run_id=run_id,
            subject_id=subject_id,
            stage=stage,
            version=1,
            input_hash="a" * 64,
            canonical_blob_id=blob_id,
        )
        upstream[stage] = artifact
        await uow.production_artifacts.append(artifact)
    projection_artifact = ProductionArtifact(
        production_run_id=run_id,
        subject_id=subject_id,
        stage=ProductionArtifactStage.RELEVANCE_PROJECTION,
        version=1,
        input_hash="c" * 64,
    )
    upstream[ProductionArtifactStage.RELEVANCE_PROJECTION] = projection_artifact
    await uow.production_artifacts.append(projection_artifact)

    run = ProductionRun(
        id=run_id,
        subject_id=subject_id,
        edition_id=uuid4(),
        current_stage=ProductionStage.EDITORIAL_ENRICHMENT,
    )
    snapshot = SimpleNamespace(subject_id=subject_id, input_hash=extraction.production_input_hash)
    artifact = ProductionArtifact(
        production_run_id=run_id,
        subject_id=subject_id,
        stage=ProductionArtifactStage.EDITORIAL_ENRICHMENT,
        version=1,
        input_hash="b" * 64,
    )
    model_run_id = uuid4()
    execution = SimpleNamespace(
        status=EditorialEnrichmentExecutionStatus.SUCCEEDED,
        input_hash="b" * 64,
        artifact=artifact,
        model_run_id=model_run_id,
        model_calls=1,
        table_count=2,
        diagram_count=1,
        source_figure_count=0,
        details={},
    )
    calls = []

    class Service:
        async def execute(self, *args):
            calls.append(args)
            return execution

    workflow = ProductionWorkflowOrchestrator(
        uow_factory(uow), model_gateway=None, artifact_store=store
    )
    workflow._canonical_editorial_enrichment = Service()

    result = await workflow._execute_editorial_enrichment_stage(run, snapshot=snapshot)

    assert calls == [
        (
            run,
            snapshot,
            upstream[ProductionArtifactStage.EXTRACTION],
            upstream[ProductionArtifactStage.SYNTHESIS],
            upstream[ProductionArtifactStage.RELEVANCE_PROJECTION],
        )
    ]
    assert result == {
        "stage": "editorial_enrichment",
        "input_hash": "b" * 64,
        "tables": 2,
        "diagrams": 1,
        "source_figures": 0,
        "model_calls": 1,
        "model_run_id": str(model_run_id),
        "status": "success",
        "artifact_id": str(artifact.id),
        "reused": False,
    }


def test_editorial_enrichment_cross_run_reuse_is_exposed_as_success() -> None:
    run_id, subject_id = uuid4(), uuid4()
    source_artifact_id = uuid4()
    artifact = ProductionArtifact(
        production_run_id=run_id,
        subject_id=subject_id,
        stage=ProductionArtifactStage.EDITORIAL_ENRICHMENT,
        version=1,
        input_hash="c" * 64,
        reused_from_artifact_id=source_artifact_id,
    )
    execution = SimpleNamespace(
        status=EditorialEnrichmentExecutionStatus.REUSED,
        input_hash="c" * 64,
        artifact=artifact,
        model_run_id=None,
        model_calls=0,
        table_count=1,
        diagram_count=2,
        source_figure_count=0,
        details={"reused": True},
    )

    result = ProductionWorkflowOrchestrator._editorial_enrichment_execution_result(execution)

    assert result["status"] == "success"
    assert result["reused"] is True
    assert result["model_calls"] == 0
    assert result["reused_from_artifact_id"] == str(source_artifact_id)
