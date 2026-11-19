from __future__ import annotations

import hashlib
from dataclasses import replace
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest

from cti_app.application.editorial_enrichment_candidate import (
    CandidatePayloadBlobs,
    build_candidate_from_validated_patch,
    persist_candidate,
)
from cti_app.application.editorial_enrichment_revision import (
    validate_editorial_enrichment_patch,
)
from cti_app.application.production_editorial_enrichment import (
    EDITORIAL_ENRICHMENT_MODEL_POLICY_VERSION,
    build_editorial_figure_catalog,
    parse_editorial_enrichment_proposal_wire,
)
from cti_app.application.production_enrichment_revision import (
    ProductionEditorialEnrichmentRevisionService,
)
from cti_app.application.production_stages import EditorialEnrichmentService
from cti_app.domain.production_editorial_enrichment import (
    EditorialEnrichmentElementKind,
    EditorialEnrichmentRevisionAction,
    editorial_enrichment_from_json,
)
from tests.test_production_editorial_enrichment_application import (
    _RecordingGateway,
    _revision_args,
    _revision_harness,
    _RevisionArtifactRepository,
    _RevisionUow,
    _succeeded_text,
    _table_revision_wire,
)


async def _validated_table_patch(harness: SimpleNamespace) -> Any:
    figure_catalog = build_editorial_figure_catalog(
        harness.world.extraction, harness.inventory_state.value
    )
    parsed = parse_editorial_enrichment_proposal_wire(
        _table_revision_wire(), harness.evidence_pack, figure_catalog=figure_catalog
    )
    return validate_editorial_enrichment_patch(
        parsed,
        element_kind=EditorialEnrichmentElementKind.TABLE,
        element_key="tools_table",
        evidence_pack=harness.evidence_pack,
        extraction=harness.world.extraction,
        synthesis=harness.world.synthesis,
        figure_catalog=figure_catalog,
    )


async def _build_table_candidate(harness: SimpleNamespace, *, artifact_id: Any) -> Any:
    return await build_candidate_from_validated_patch(
        base_artifact=harness.base,
        base_payload=harness.base_payload,
        base_canonical_bytes=await harness.world.store.read_bytes(harness.base.canonical_blob_id),
        base_canonical_sha256=harness.base_sha256,
        base_enrichment=editorial_enrichment_from_json(harness.base_payload),
        validated_patch=await _validated_table_patch(harness),
        extraction=harness.world.extraction,
        synthesis=harness.world.synthesis,
        element_kind=EditorialEnrichmentElementKind.TABLE,
        element_key="tools_table",
        action=EditorialEnrichmentRevisionAction.IMPROVE_TABLE,
        input_hash=harness.input_hash,
        artifact_id=artifact_id,
    )


@pytest.mark.asyncio
async def test_candidate_core_matches_legacy_revise_candidate_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = await _revision_harness(
        monkeypatch,
        _RecordingGateway(lambda request: _succeeded_text(request, _table_revision_wire())),
    )
    assert isinstance(harness.service, ProductionEditorialEnrichmentRevisionService)

    legacy_result = await harness.service.revise(**_revision_args(harness))
    candidate = await _build_table_candidate(harness, artifact_id=legacy_result.artifact.id)

    assert candidate.canonical_bytes == await harness.world.store.read_bytes(
        legacy_result.artifact.canonical_blob_id
    )
    assert candidate.canonical_sha256 == hashlib.sha256(candidate.canonical_bytes).hexdigest()


@pytest.mark.asyncio
async def test_candidate_core_makes_no_model_call_and_no_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = await _revision_harness(
        monkeypatch,
        _RecordingGateway(lambda request: _succeeded_text(request, _table_revision_wire())),
    )
    stored_blob_ids = set(harness.world.store.payloads)
    stored_artifacts = tuple(harness.artifacts.artifacts)

    await _build_table_candidate(harness, artifact_id=uuid4())

    assert harness.world.gateway.calls == []
    assert set(harness.world.store.payloads) == stored_blob_ids
    assert tuple(harness.artifacts.artifacts) == stored_artifacts


@pytest.mark.asyncio
async def test_persist_candidate_writes_same_rows_as_legacy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = await _revision_harness(
        monkeypatch,
        _RecordingGateway(lambda request: _succeeded_text(request, _table_revision_wire())),
    )
    initial_rows = [
        replace(row, metadata=dict(row.metadata)) for row in harness.artifacts.artifacts
    ]
    legacy_result = await harness.service.revise(**_revision_args(harness))
    expected = legacy_result.artifact
    assert all(row.id != expected.id for row in initial_rows)

    candidate = await _build_table_candidate(harness, artifact_id=expected.id)
    replay_artifacts = _RevisionArtifactRepository(initial_rows)
    initial_row_count = len(initial_rows)
    uow = _RevisionUow(
        run=harness.world.run,
        snapshot=harness.world.snapshot,
        source_documents=harness.service._enrichment._uow_factory().source_documents,  # type: ignore[attr-defined]
        artifacts=replay_artifacts,
        source_collections=harness.service._enrichment._uow_factory().source_collections,  # type: ignore[attr-defined]
    )
    persistence = EditorialEnrichmentService(lambda: cast(Any, uow), harness.world.store)
    raw_text = _table_revision_wire()

    persisted = await persist_candidate(
        uow,
        artifact_store=harness.world.store,
        persistence_service=persistence,
        candidate=candidate,
        payload_blobs=CandidatePayloadBlobs(
            raw_blob_id=expected.raw_blob_id,
            canonical_blob_id=expected.canonical_blob_id,
            raw_sha256=hashlib.sha256(raw_text.encode("utf-8")).hexdigest(),
            canonical_sha256=candidate.canonical_sha256,
        ),
        raw_result=raw_text,
        model_run_id=expected.model_run_id,
        evidence_pack_hash=str(harness.base.metadata["evidence_pack_hash"]),
        access_policy_hash=str(harness.base.metadata["access_policy_hash"]),
        model_policy_version=EDITORIAL_ENRICHMENT_MODEL_POLICY_VERSION,
        routing_policy_version=str(harness.base.metadata["routing_policy_version"]),
        source_figure_inventory_hash=str(harness.base.metadata["source_figure_inventory_hash"]),
        metadata_extra=expected.metadata,
    )
    replayed = await persist_candidate(
        uow,
        artifact_store=harness.world.store,
        persistence_service=persistence,
        candidate=candidate,
        payload_blobs=CandidatePayloadBlobs(
            raw_blob_id=expected.raw_blob_id,
            canonical_blob_id=expected.canonical_blob_id,
            raw_sha256=hashlib.sha256(raw_text.encode("utf-8")).hexdigest(),
            canonical_sha256=candidate.canonical_sha256,
        ),
        raw_result=raw_text,
        model_run_id=expected.model_run_id,
        evidence_pack_hash=str(harness.base.metadata["evidence_pack_hash"]),
        access_policy_hash=str(harness.base.metadata["access_policy_hash"]),
        model_policy_version=EDITORIAL_ENRICHMENT_MODEL_POLICY_VERSION,
        routing_policy_version=str(harness.base.metadata["routing_policy_version"]),
        source_figure_inventory_hash=str(harness.base.metadata["source_figure_inventory_hash"]),
        metadata_extra=expected.metadata,
    )

    assert replace(persisted, created_at=expected.created_at) == expected
    assert replayed is persisted
    assert any(row.id == expected.id for row in replay_artifacts.artifacts)
    assert len(replay_artifacts.artifacts) == initial_row_count + 1
    replayed_publication = next(
        row for row in replay_artifacts.artifacts if row.stage.value == "publication"
    )
    expected_publication = next(
        row for row in harness.artifacts.artifacts if row.stage.value == "publication"
    )
    assert replayed_publication.status is expected_publication.status
