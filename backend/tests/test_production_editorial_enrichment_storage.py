"""Versioning and invalidation for the canonical enrichment artifact."""

from __future__ import annotations

from dataclasses import replace
from uuid import uuid4

import pytest

from cti_app.application.production_editorial_enrichment import (
    EDITORIAL_ENRICHMENT_GENERATOR_VERSION,
    compute_editorial_enrichment_input_hash,
)
from cti_app.application.production_stages import EditorialEnrichmentService
from cti_app.domain.production import ProductionArtifactStage
from tests.editorial_enrichment_support import build_empty_editorial_enrichment
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
    evidence_pack_hash = "c" * 64
    access_policy_hash = "d" * 64
    input_hash = compute_editorial_enrichment_input_hash(
        extraction=extraction,
        synthesis=synthesis,
        evidence_pack_hash=evidence_pack_hash,
        access_policy_hash=access_policy_hash,
    )
    uow, store = MemoryUow(), MemoryStore()
    service = EditorialEnrichmentService(uow_factory(uow), store)
    model_run_id = uuid4()
    arguments = {
        "run_id": run_id,
        "subject_id": subject_id,
        "input_hash": input_hash,
        "enrichment": enrichment,
        "extraction": extraction,
        "synthesis": synthesis,
        "raw_result": "NO USEFUL ENRICHMENT",
        "model_run_id": model_run_id,
        "evidence_pack_hash": evidence_pack_hash,
        "access_policy_hash": access_policy_hash,
        "model_policy_version": "editorial-enrichment-model-policy-v1",
        "routing_policy_version": "editorial-enrichment-routing-policy-v1",
    }

    first = await service.store_editorial_enrichment_result(**arguments)
    same = await service.store_editorial_enrichment_result(**arguments)
    assert same is first
    assert first.stage is ProductionArtifactStage.EDITORIAL_ENRICHMENT
    assert first.version == 1
    assert first.canonical_blob_id is not None
    assert first.raw_blob_id is not None
    assert first.rendered_blob_id is None
    assert first.model_run_id == model_run_id
    assert first.metadata["generator_version"] == EDITORIAL_ENRICHMENT_GENERATOR_VERSION
    assert first.metadata["validator_version"] == "editorial-enrichment-validator-v2"
    assert first.metadata["evidence_pack_hash"] == evidence_pack_hash
    assert first.metadata["access_policy_hash"] == access_policy_hash
    assert first.metadata["model_policy_version"] == "editorial-enrichment-model-policy-v1"
    assert first.metadata["routing_policy_version"] == "editorial-enrichment-routing-policy-v1"
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
