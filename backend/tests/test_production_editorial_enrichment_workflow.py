"""The bootstrap enrichment stage consumes canonical inputs without a model."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

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
async def test_bootstrap_stage_persists_empty_canonical_enrichment_without_gateway() -> None:
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
    for stage, blob_id in (
        (ProductionArtifactStage.EXTRACTION, extraction_blob),
        (ProductionArtifactStage.SYNTHESIS, synthesis_blob),
    ):
        await uow.production_artifacts.append(
            ProductionArtifact(
                production_run_id=run_id,
                subject_id=subject_id,
                stage=stage,
                version=1,
                input_hash="a" * 64,
                canonical_blob_id=blob_id,
            )
        )
    run = ProductionRun(
        id=run_id,
        subject_id=subject_id,
        edition_id=uuid4(),
        current_stage=ProductionStage.EDITORIAL_ENRICHMENT,
    )
    workflow = ProductionWorkflowOrchestrator(
        uow_factory(uow), model_gateway=None, artifact_store=store
    )

    result = await workflow._execute_editorial_enrichment_stage(
        run,
        snapshot=SimpleNamespace(
            subject_id=subject_id, input_hash=extraction.production_input_hash
        ),
    )

    assert result["status"] == "success"
    assert result["tables"] == result["diagrams"] == result["source_figures"] == 0
    artifact = await uow.production_artifacts.get_current(
        run_id, ProductionArtifactStage.EDITORIAL_ENRICHMENT.value
    )
    assert artifact is not None and artifact.canonical_blob_id is not None
    assert artifact.model_run_id is None
    payload = await store.read_json(artifact.canonical_blob_id)
    assert payload["tables"] == payload["diagrams"] == payload["source_figures"] == []
