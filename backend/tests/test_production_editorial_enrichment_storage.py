"""Versioning and invalidation for the canonical enrichment artifact."""

from __future__ import annotations

from dataclasses import replace
from uuid import uuid4

import pytest

from cti_app.application.production_editorial_enrichment import (
    build_empty_editorial_enrichment,
    compute_editorial_enrichment_input_hash,
)
from cti_app.application.production_stages import EditorialEnrichmentService
from cti_app.domain.production import ProductionArtifactStage
from tests.test_production_synthesis_storage import (
    MemoryStore,
    MemoryUow,
    canonical_pair,
    uow_factory,
)


@pytest.mark.asyncio
async def test_enrichment_storage_is_idempotent_and_stales_only_publication() -> None:
    subject_id, run_id = uuid4(), uuid4()
    synthesis, extraction = canonical_pair(subject_id)
    enrichment = build_empty_editorial_enrichment(extraction=extraction, synthesis=synthesis)
    input_hash = compute_editorial_enrichment_input_hash(extraction=extraction, synthesis=synthesis)
    uow, store = MemoryUow(), MemoryStore()
    service = EditorialEnrichmentService(uow_factory(uow), store)
    arguments = {
        "run_id": run_id,
        "subject_id": subject_id,
        "input_hash": input_hash,
        "enrichment": enrichment,
        "extraction": extraction,
        "synthesis": synthesis,
    }

    first = await service.store_editorial_enrichment_result(**arguments)
    same = await service.store_editorial_enrichment_result(**arguments)
    assert same is first
    assert first.stage is ProductionArtifactStage.EDITORIAL_ENRICHMENT
    assert first.version == 1
    assert first.canonical_blob_id is not None
    assert first.raw_blob_id is None
    assert first.rendered_blob_id is None
    assert first.model_run_id is None
    assert first.metadata["generator_version"] == "bootstrap-empty-v1"
    assert first.metadata["table_count"] == 0
    assert len(uow.production_artifacts.items) == 1

    changed = replace(enrichment, warnings=("Editorial warning",))
    second = await service.store_editorial_enrichment_result(**{**arguments, "enrichment": changed})
    assert second.version == 2
    assert second.canonical_blob_id != first.canonical_blob_id
    assert len(uow.production_artifacts.items) == 2
    assert uow.production_artifacts.staled == [
        (run_id, ProductionArtifactStage.EDITORIAL_ENRICHMENT.value),
        (run_id, ProductionArtifactStage.EDITORIAL_ENRICHMENT.value),
    ]
