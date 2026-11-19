from __future__ import annotations

import hashlib
from dataclasses import replace
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest

from cti_app.application.editorial_enrichment_candidate import (
    EnrichmentCandidateBlobReference,
    build_candidate_from_validated_patch,
    materialize_candidate_diagrams,
    persist_candidate,
)
from cti_app.application.editorial_enrichment_revision import (
    validate_editorial_enrichment_patch,
)
from cti_app.application.production_editorial_enrichment import (
    EDITORIAL_ENRICHMENT_MODEL_POLICY_VERSION,
    EditorialEnrichmentRevisionConflictError,
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


async def _build_diagram_candidate(harness: SimpleNamespace, *, artifact_id: Any) -> Any:
    enrichment = editorial_enrichment_from_json(harness.base_payload)
    diagram = enrichment.diagrams[0]
    return await build_candidate_from_validated_patch(
        base_artifact=harness.base,
        base_payload=harness.base_payload,
        base_canonical_bytes=await harness.world.store.read_bytes(harness.base.canonical_blob_id),
        base_canonical_sha256=harness.base_sha256,
        base_enrichment=enrichment,
        validated_patch=replace(enrichment, diagrams=(diagram,)),
        extraction=harness.world.extraction,
        synthesis=harness.world.synthesis,
        element_kind=EditorialEnrichmentElementKind.DIAGRAM,
        element_key=diagram.key,
        action=EditorialEnrichmentRevisionAction.DETAIL_DIAGRAM,
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
async def test_candidate_preparation_returns_diagram_materialization_without_media_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = await _revision_harness(
        monkeypatch,
        _RecordingGateway(lambda request: _succeeded_text(request, _table_revision_wire())),
    )
    stored_blobs = set(harness.world.store.payloads)
    materialized: list[Any] = []

    candidate = await _build_diagram_candidate(harness, artifact_id=uuid4())

    assert candidate.diagram_materialization is not None
    assert materialized == []
    assert set(harness.world.store.payloads) == stored_blobs

    async def materializer(diagram: Any) -> Any:
        materialized.append(diagram)
        return replace(diagram, compiled_asset_id=uuid4())

    finalized = await materialize_candidate_diagrams(candidate, materializer=materializer)

    assert materialized == [candidate.diagram_materialization]
    assert finalized.diagram_materialization is None
    assert finalized.enrichment.diagrams[0].compiled_asset_id is not None


@pytest.mark.asyncio
async def test_remove_element_with_resource_need_keeps_legacy_need_outcome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    needs_wire = (
        "NEEDS N009\nKIND: TECHNICAL_ANALYSIS\n"
        "REASON: A protocol detail is absent from the admitted evidence.\n"
        "QUERY_HINT: ExampleRAT protocol detail\nEND NEEDS"
    )
    harness = await _revision_harness(
        monkeypatch,
        _RecordingGateway(lambda request: _succeeded_text(request, needs_wire)),
    )
    args = _revision_args(harness)
    args["action"] = EditorialEnrichmentRevisionAction.REMOVE_ELEMENT

    result = await harness.service.revise(**args)

    assert result.outcome.value == "needs_new_evidence"
    assert result.revision["resource_need"]["kind"] == "TECHNICAL_ANALYSIS"  # type: ignore[index]
    stored = await harness.world.store.read_json(result.artifact.canonical_blob_id)
    assert stored["tables"] == harness.base_payload["tables"]  # type: ignore[index]
    assert len(stored["resource_needs"]) == 1  # type: ignore[arg-type]


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
    revision_metadata = {
        "outcome": candidate.outcome.value,
        "base_artifact_id": str(candidate.base_artifact.id),
        "base_canonical_sha256": candidate.base_canonical_sha256,
        "result_artifact_id": str(candidate.artifact_id),
    }

    persisted = await persist_candidate(
        uow,
        artifact_store=harness.world.store,
        persistence_service=persistence,
        candidate=candidate,
        raw_result=raw_text,
        model_run_id=expected.model_run_id,
        evidence_pack_hash=str(harness.base.metadata["evidence_pack_hash"]),
        access_policy_hash=str(harness.base.metadata["access_policy_hash"]),
        model_policy_version=EDITORIAL_ENRICHMENT_MODEL_POLICY_VERSION,
        routing_policy_version=str(harness.base.metadata["routing_policy_version"]),
        source_figure_inventory_hash=str(harness.base.metadata["source_figure_inventory_hash"]),
        metadata_extra={"revision": revision_metadata},
    )

    assert persisted.id == expected.id
    assert persisted.version == expected.version
    assert persisted.input_hash == expected.input_hash
    assert persisted.metadata["revision"] == revision_metadata
    assert persisted.metadata["schema_version"] == candidate.enrichment.schema_version
    assert persisted.metadata["table_count"] == len(candidate.enrichment.tables)
    assert persisted.metadata["diagram_count"] == len(candidate.enrichment.diagrams)
    assert any(row.id == expected.id for row in replay_artifacts.artifacts)
    assert len(replay_artifacts.artifacts) == initial_row_count + 1
    replayed_publication = next(
        row for row in replay_artifacts.artifacts if row.stage.value == "publication"
    )
    expected_publication = next(
        row for row in harness.artifacts.artifacts if row.stage.value == "publication"
    )
    assert replayed_publication.status is expected_publication.status


@pytest.mark.asyncio
async def test_preuploaded_candidate_replay_does_not_read_candidate_blob_in_uow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = await _revision_harness(
        monkeypatch,
        _RecordingGateway(lambda request: _succeeded_text(request, _table_revision_wire())),
    )
    initial_rows = [
        replace(row, metadata=dict(row.metadata)) for row in harness.artifacts.artifacts
    ]
    accepted = await harness.service.revise(**_revision_args(harness))
    candidate = await _build_table_candidate(harness, artifact_id=accepted.artifact.id)
    canonical_blob_id = accepted.artifact.canonical_blob_id
    assert canonical_blob_id is not None
    replay_artifacts = _RevisionArtifactRepository([*initial_rows, accepted.artifact])
    uow = _RevisionUow(
        run=harness.world.run,
        snapshot=harness.world.snapshot,
        source_documents=harness.service._enrichment._uow_factory().source_documents,  # type: ignore[attr-defined]
        artifacts=replay_artifacts,
        source_collections=harness.service._enrichment._uow_factory().source_collections,  # type: ignore[attr-defined]
    )
    persistence = EditorialEnrichmentService(lambda: cast(Any, uow), harness.world.store)

    class CandidateReadGuard:
        async def read_bytes(self, blob_id: Any, *, max_bytes: int | None = None) -> bytes:
            assert blob_id != canonical_blob_id
            return cast(bytes, await harness.world.store.read_bytes(blob_id, max_bytes=max_bytes))

    with pytest.raises(EditorialEnrichmentRevisionConflictError):
        await persist_candidate(
            uow,
            artifact_store=CandidateReadGuard(),
            persistence_service=persistence,
            candidate=candidate,
            raw_result=None,
            model_run_id=accepted.artifact.model_run_id,
            evidence_pack_hash=str(harness.base.metadata["evidence_pack_hash"]),
            access_policy_hash=str(harness.base.metadata["access_policy_hash"]),
            model_policy_version=EDITORIAL_ENRICHMENT_MODEL_POLICY_VERSION,
            routing_policy_version=str(harness.base.metadata["routing_policy_version"]),
            source_figure_inventory_hash=str(harness.base.metadata["source_figure_inventory_hash"]),
            candidate_blob=EnrichmentCandidateBlobReference(
                blob_id=canonical_blob_id,
                sha256=candidate.canonical_sha256,
                size=len(candidate.canonical_bytes),
            ),
        )


@pytest.mark.asyncio
async def test_late_candidate_replay_rejects_stale_current_base(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = await _revision_harness(
        monkeypatch,
        _RecordingGateway(lambda request: _succeeded_text(request, _table_revision_wire())),
    )
    initial_rows = [
        replace(row, metadata=dict(row.metadata)) for row in harness.artifacts.artifacts
    ]
    result = await harness.service.revise(**_revision_args(harness))
    candidate = await _build_table_candidate(harness, artifact_id=result.artifact.id)
    replay_artifacts = _RevisionArtifactRepository([*initial_rows, result.artifact])
    uow = _RevisionUow(
        run=harness.world.run,
        snapshot=harness.world.snapshot,
        source_documents=harness.service._enrichment._uow_factory().source_documents,  # type: ignore[attr-defined]
        artifacts=replay_artifacts,
        source_collections=harness.service._enrichment._uow_factory().source_collections,  # type: ignore[attr-defined]
    )
    persistence = EditorialEnrichmentService(lambda: cast(Any, uow), harness.world.store)

    with pytest.raises(EditorialEnrichmentRevisionConflictError):
        await persist_candidate(
            uow,
            artifact_store=harness.world.store,
            persistence_service=persistence,
            candidate=candidate,
            raw_result=_table_revision_wire(),
            model_run_id=result.artifact.model_run_id,
            evidence_pack_hash=str(harness.base.metadata["evidence_pack_hash"]),
            access_policy_hash=str(harness.base.metadata["access_policy_hash"]),
            model_policy_version=EDITORIAL_ENRICHMENT_MODEL_POLICY_VERSION,
            routing_policy_version=str(harness.base.metadata["routing_policy_version"]),
            source_figure_inventory_hash=str(harness.base.metadata["source_figure_inventory_hash"]),
        )


@pytest.mark.asyncio
async def test_stale_uow_fence_after_preflight_does_not_orphan_payloads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = await _revision_harness(
        monkeypatch,
        _RecordingGateway(lambda request: _succeeded_text(request, _table_revision_wire())),
    )
    candidate = await _build_table_candidate(harness, artifact_id=uuid4())
    replay_artifacts = _RevisionArtifactRepository(
        [replace(row, metadata=dict(row.metadata)) for row in harness.artifacts.artifacts]
    )
    uow = _RevisionUow(
        run=harness.world.run,
        snapshot=harness.world.snapshot,
        source_documents=harness.service._enrichment._uow_factory().source_documents,  # type: ignore[attr-defined]
        artifacts=replay_artifacts,
        source_collections=harness.service._enrichment._uow_factory().source_collections,  # type: ignore[attr-defined]
    )
    persistence = EditorialEnrichmentService(lambda: cast(Any, uow), harness.world.store)
    await persistence.assert_editorial_enrichment_revision_fence(
        run_id=harness.base.production_run_id,
        expected_current_artifact_id=harness.base.id,
        expected_current_input_hash=harness.base.input_hash,
        expected_current_canonical_sha256=candidate.base_canonical_sha256,
    )
    current = await replay_artifacts.get_current_for_revision(
        harness.base.production_run_id,
        harness.base.stage.value,
    )
    assert current is not None
    replay_artifacts.artifacts.append(
        replace(
            current,
            id=uuid4(),
            version=current.version + 1,
            metadata=dict(current.metadata),
        )
    )
    stored_blobs = set(harness.world.store.payloads)

    with pytest.raises(EditorialEnrichmentRevisionConflictError):
        await persist_candidate(
            uow,
            artifact_store=harness.world.store,
            persistence_service=persistence,
            candidate=candidate,
            raw_result=_table_revision_wire(),
            model_run_id=None,
            evidence_pack_hash=str(harness.base.metadata["evidence_pack_hash"]),
            access_policy_hash=str(harness.base.metadata["access_policy_hash"]),
            model_policy_version=EDITORIAL_ENRICHMENT_MODEL_POLICY_VERSION,
            routing_policy_version=str(harness.base.metadata["routing_policy_version"]),
            source_figure_inventory_hash=str(harness.base.metadata["source_figure_inventory_hash"]),
        )

    assert set(harness.world.store.payloads) == stored_blobs
