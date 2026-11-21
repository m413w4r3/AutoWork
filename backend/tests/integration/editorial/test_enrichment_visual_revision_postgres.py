"""PostgreSQL oracles for MANUAL visual revisions of existing enrichments.

F2-03a covers caption, move and reorder end to end: preparation through the
shared planner, adoption through the single writer, CAS fences under the run
guard, and the absence of any model call. These tests run against the real
PostgreSQL schema with the memory blob store used by the sibling editorial
integration modules.
"""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import replace
from typing import Any, cast
from uuid import UUID, uuid4

import httpx
import pytest

from cti_app.api import editorial_workspace as editorial_workspace_api
from cti_app.api import main as api_main
from cti_app.application.editorial_proposal_adoption import (
    EditorialProposalAdoptionError,
    EditorialProposalAdoptionResult,
    EditorialProposalAdoptionService,
)
from cti_app.application.editorial_proposal_adoption_inputs import (
    ProductionEditorialProposalAdoptionInputs,
)
from cti_app.application.editorial_proposals import EditorialProposalService
from cti_app.application.editorial_visual_revision import VisualRevisionContextLoader
from cti_app.application.media_assets import MediaAssetStore
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.application.production_editorial_enrichment import (
    ProductionEditorialEnrichmentService,
)
from cti_app.application.production_enrichment_revision import (
    ProductionEditorialEnrichmentRevisionService,
)
from cti_app.application.production_stages import (
    EditorialEnrichmentService,
    SynthesisService,
)
from cti_app.application.production_synthesis import ProductionSynthesisService
from cti_app.config import Settings
from cti_app.domain.editorial_enrichment_patch import (
    MoveEnrichmentElementPatch,
    ReorderEnrichmentElementsPatch,
    UpdateVisualCaptionPatch,
)
from cti_app.domain.editorial_proposal import EditorialProposalState
from cti_app.domain.editorial_workspace import (
    EditorialBlockKind,
    bootstrap_block_registry,
)
from cti_app.domain.production import ProductionArtifactStage
from cti_app.domain.production_editorial_enrichment import (
    EditorialEnrichmentElementKind,
    EnrichmentElementRef,
    EnrichmentPlacementKind,
    EnrichmentPlacementV1,
    editorial_enrichment_from_json,
)
from cti_app.domain.production_synthesis import production_synthesis_to_json
from cti_app.domain.publication_kind import PublicationKind
from cti_app.infrastructure.analytic_chart_renderer import AnalyticChartRenderer
from cti_app.infrastructure.d2_diagram_compiler import D2DiagramCompiler
from tests.integration.editorial.test_proposal_adoption_postgres import _Seeded
from tests.integration.editorial.test_table_insertion_postgres import (
    _NoModelCalls,
    _ready_for_manual_table_route,
)

pytestmark = pytest.mark.integration


def _synthesis_entries(seeded: _Seeded):
    payload = production_synthesis_to_json(seeded.synthesis)
    canonical_sha256 = hashlib.sha256(
        ProductionArtifactStore.canonical_json_bytes(payload)
    ).hexdigest()
    return bootstrap_block_registry(
        artifact_id=seeded.base_artifact.id,
        canonical_sha256=canonical_sha256,
        source_stage="synthesis",
        canonical_content=payload,
    )


def _lead_anchor_placement(seeded: _Seeded) -> EnrichmentPlacementV1:
    lead = next(
        entry
        for entry in _synthesis_entries(seeded)
        if entry.kind is EditorialBlockKind.LEAD_PARAGRAPH
    )
    return EnrichmentPlacementV1(
        EnrichmentPlacementKind.AFTER_PARAGRAPH, anchor_block_id=lead.block_id
    )


def _visual_services(
    app: Any, uow_factory: Any
) -> tuple[Any, EditorialProposalService, EditorialProposalAdoptionService]:
    request = type("_Request", (), {"app": app})()
    workspace_service = editorial_workspace_api._service(request)
    artifact_store = app.state.production_artifact_store
    blob_store = app.state.blob_store
    model_gateway = _NoModelCalls()
    synthesis_writer = SynthesisService(uow_factory, artifact_store)
    enrichment_writer = EditorialEnrichmentService(uow_factory, artifact_store)
    collection_service = editorial_workspace_api._collection_service_for_request(request)
    enrichment_loader = ProductionEditorialEnrichmentService(
        uow_factory=uow_factory,
        artifact_store=artifact_store,
        model_gateway=model_gateway,
        editorial_enrichment_service=enrichment_writer,
        media_asset_store=MediaAssetStore(artifact_store, uow_factory),
        diagram_compiler=D2DiagramCompiler(),
        chart_compiler=AnalyticChartRenderer(),
        source_media_archiver=getattr(collection_service, "source_media_archiver", None),
        resource_search_enabled=False,
    )
    revision_service = ProductionEditorialEnrichmentRevisionService(
        enrichment_service=enrichment_loader,
        persistence_service=enrichment_writer,
        publication_render_service=app.state.publication_render_service,
    )
    proposal_service = EditorialProposalService(
        uow_factory=uow_factory,
        blob_store=blob_store,
        artifact_store=artifact_store,
        workspace_service=workspace_service,
        visual_revision_loader=VisualRevisionContextLoader(
            uow_factory=uow_factory,
            artifact_store=artifact_store,
            workspace_service=workspace_service,
            enrichment_loader=enrichment_loader,
            enrichment_revision_service=revision_service,
        ),
    )
    adoption_inputs = ProductionEditorialProposalAdoptionInputs(
        uow_factory=uow_factory,
        artifact_store=artifact_store,
        workspace_service=workspace_service,
        synthesis_loader=ProductionSynthesisService(
            uow_factory=uow_factory,
            artifact_store=artifact_store,
            model_gateway=model_gateway,
            synthesis_service=synthesis_writer,
        ),
        enrichment_loader=enrichment_loader,
        enrichment_revision_service=revision_service,
        resource_search_enabled=False,
    )
    adoption_service = EditorialProposalAdoptionService(
        uow_factory=uow_factory,
        blob_store=blob_store,
        artifact_store=artifact_store,
        workspace_service=workspace_service,
        inputs=adoption_inputs,
        synthesis_writer=synthesis_writer,
        enrichment_writer=enrichment_writer,
    )
    return workspace_service, proposal_service, adoption_service


def _enrichment_block(
    snapshot: dict[str, Any], *, kind: str, key: str | None = None
) -> dict[str, Any]:
    return cast(
        dict[str, Any],
        next(
            block
            for block in snapshot["enrichments"]
            if block["kind"] == kind and (key is None or block.get("element_key") == key)
        ),
    )


def _paragraph_block(snapshot: dict[str, Any], *, position: int = 0) -> dict[str, Any]:
    paragraphs = [
        block for block in snapshot["blocks"] if block["kind"] in {"paragraph", "lead_paragraph"}
    ]
    return cast(dict[str, Any], paragraphs[position])


async def _load_proposal(
    uow_factory: Any, subject_id: UUID, occurrence_id: UUID, proposal_id: UUID
) -> Any:
    async with uow_factory() as uow:
        proposal = await uow.editorial_proposals.get_for_occurrence(
            subject_id, occurrence_id, proposal_id
        )
    assert proposal is not None
    return proposal


async def _read_enrichment(app: Any, uow_factory: Any, run_id: UUID) -> tuple[Any, dict]:
    async with uow_factory() as uow:
        artifact = await uow.production_artifacts.get_current(
            run_id, ProductionArtifactStage.EDITORIAL_ENRICHMENT.value
        )
    assert artifact is not None and artifact.canonical_blob_id is not None
    payload = await app.state.production_artifact_store.read_json(artifact.canonical_blob_id)
    return artifact, payload


async def _adopt(
    service: EditorialProposalAdoptionService,
    proposal: Any,
    *,
    actor_id: UUID,
    key: str,
) -> EditorialProposalAdoptionResult:
    patch_sha256 = proposal.patch_sha256
    assert patch_sha256 is not None
    return await service.adopt(
        proposal.subject_id,
        proposal.occurrence_id,
        proposal.id,
        actor_id=actor_id,
        expected_state_version=proposal.state_version,
        patch_sha256=patch_sha256,
        idempotency_key=key,
    )


def _configure_app(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        api_main,
        "get_settings",
        lambda: Settings(
            _env_file=None,
            app_env="development",
            api_bind_host="127.0.0.1",
        ),
    )
    monkeypatch.setattr(
        editorial_workspace_api,
        "get_settings",
        lambda: Settings(
            _env_file=None,
            editorial_proposal_accept_enabled=True,
            production_editorial_resource_search_enabled=False,
        ),
    )
    monkeypatch.setattr(editorial_workspace_api, "_proposal_impact_consumer_wired", lambda: True)


async def _create_table_proposal(
    client: httpx.AsyncClient,
    *,
    subject_id: UUID,
    snapshot: dict[str, Any],
    anchor_id: UUID,
    key: str,
    question: str,
    caption: str,
    evidence_ref: dict[str, str],
) -> dict[str, Any]:
    response = await client.post(
        f"/api/subjects/{subject_id}/editor/blocks/{anchor_id}/table-proposals",
        headers={"Idempotency-Key": f"manual-table-{uuid4().hex}"},
        json={
            "snapshot_id": snapshot["snapshot_id"],
            "anchor_block_id": str(anchor_id),
            "table": {
                "key": key,
                "kind": "custom",
                "title": f"Inserted {key}",
                "caption": caption,
                "columns": [
                    {"key": "finding", "label": "Finding"},
                    {"key": "source", "label": "Source detail"},
                ],
                "rows": [
                    {
                        "cells": ["Synthetic finding", "Synthetic test context."],
                        "evidence_refs": [evidence_ref],
                    }
                ],
                "purpose": {
                    "question": question,
                    "available_data": "The synthetic finding and its cited context.",
                    "comprehension_gain": "Compare the finding with its source context.",
                    "scope": "Only the synthetic finding and its source context.",
                    "evidence_refs": [evidence_ref],
                    "knowledge_limits": "No other source detail is represented.",
                    "placement_reason": "Keep the table after its anchor paragraph.",
                },
            },
        },
    )
    assert response.status_code == 200, response.text
    return cast(dict[str, Any], response.json())


async def _accept_proposal_via_route(
    client: httpx.AsyncClient, *, subject_id: UUID, occurrence_id: UUID, proposal: Any
) -> dict[str, Any]:
    response = await client.post(
        f"/api/subjects/{subject_id}/editor/threads/{occurrence_id}/"
        f"proposals/{proposal['id']}/accept",
        headers={"Idempotency-Key": f"accept-{uuid4().hex}"},
        json={
            "expected_state_version": 1,
            "patch_sha256": proposal["patch_sha256"],
        },
    )
    assert response.status_code == 200, response.text
    return cast(dict[str, Any], response.json())


async def _insert_source_figure(
    client: httpx.AsyncClient,
    *,
    subject_id: UUID,
    anchor_id: UUID,
    snapshot: dict[str, Any],
) -> UUID:
    catalog_response = await client.get(
        f"/api/subjects/{subject_id}/editor/figures?status=available"
    )
    assert catalog_response.status_code == 200, catalog_response.text
    selected = [item for item in catalog_response.json()["items"] if item.get("selectable")]
    assert selected
    proposal_response = await client.post(
        f"/api/subjects/{subject_id}/editor/blocks/{anchor_id}/figure-proposals",
        headers={"Idempotency-Key": f"manual-figures-{uuid4().hex}"},
        json={
            "snapshot_id": snapshot["snapshot_id"],
            "anchor_block_id": str(anchor_id),
            "figures": [
                {
                    "figure_id": selected[0]["figure_id"],
                    "caption": "Synthetic inserted figure caption",
                    "order": 0,
                }
            ],
        },
    )
    assert proposal_response.status_code == 200, proposal_response.text
    proposal = proposal_response.json()
    await _accept_proposal_via_route(
        client, subject_id=subject_id, occurrence_id=anchor_id, proposal=proposal
    )
    return UUID(proposal["id"])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("publication_kind", "expected"),
    ((PublicationKind.BRIEF, "brief"), (PublicationKind.ARTICLE, "article")),
)
async def test_caption_adoption_works_for_article_and_brief(
    uow_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
    publication_kind: PublicationKind,
    expected: str,
) -> None:
    _configure_app(monkeypatch)
    seeded, app, model_gateway, _content = await _ready_for_manual_table_route(
        uow_factory,
        base_table_placement_factory=_lead_anchor_placement,
        publication_kind=publication_kind,
    )
    workspace_service, proposal_service, adoption_service = _visual_services(app, uow_factory)
    snapshot = await workspace_service.snapshot(seeded.subject.id)
    assert snapshot["publication_kind"] == expected
    table = _enrichment_block(snapshot, kind="table")
    proposal = await proposal_service.prepare_visual_revision(
        subject_id=seeded.subject.id,
        snapshot_id=snapshot["snapshot_id"],
        occurrence_id=UUID(table["block_id"]),
        actor_id=seeded.actor_id,
        idempotency_key=f"visual-caption-{uuid4().hex}",
        patch=UpdateVisualCaptionPatch(
            element_kind=EditorialEnrichmentElementKind.TABLE,
            element_key=table["element_key"],
            caption="Synthetic caption revision",
        ),
    )
    result = await _adopt(
        adoption_service, proposal, actor_id=seeded.actor_id, key=f"adopt-{uuid4().hex}"
    )
    assert result.artifact.version == 2
    assert result.impact.state.value == "pending"
    assert model_gateway.calls == []
    artifact, payload = await _read_enrichment(app, uow_factory, seeded.run.id)
    enrichment = editorial_enrichment_from_json(payload)
    assert artifact.version == 2
    assert enrichment.tables[0].caption == "Synthetic caption revision"
    assert enrichment.tables[0].key == table["element_key"]


@pytest.mark.asyncio
async def test_caption_only_change_preserves_figure_archive_and_original_caption(
    uow_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_app(monkeypatch)
    seeded, app, model_gateway, _content = await _ready_for_manual_table_route(
        uow_factory,
        base_table_placement_factory=_lead_anchor_placement,
        source_figure_count=1,
    )
    workspace_service, proposal_service, adoption_service = _visual_services(app, uow_factory)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://editorial.test"
    ) as client:
        snapshot = await workspace_service.snapshot(seeded.subject.id)
        anchor = _paragraph_block(snapshot, position=1)
        await _insert_source_figure(
            client,
            subject_id=seeded.subject.id,
            anchor_id=UUID(anchor["block_id"]),
            snapshot=snapshot,
        )
        catalog_before = (
            await client.get(f"/api/subjects/{seeded.subject.id}/editor/figures")
        ).json()
        snapshot = await workspace_service.snapshot(seeded.subject.id)
        figure = _enrichment_block(snapshot, kind="figure")
        base_artifact, base_payload = await _read_enrichment(app, uow_factory, seeded.run.id)
        base_figure = next(
            item
            for item in editorial_enrichment_from_json(base_payload).source_figures
            if item.key == figure["element_key"]
        )
        assert base_figure.resolved_figure is not None
        archived_blob_id = base_figure.resolved_figure.blob_id
        archived_sha256 = base_figure.resolved_figure.sha256
        proposal = await proposal_service.prepare_visual_revision(
            subject_id=seeded.subject.id,
            snapshot_id=snapshot["snapshot_id"],
            occurrence_id=UUID(figure["block_id"]),
            actor_id=seeded.actor_id,
            idempotency_key=f"visual-figure-caption-{uuid4().hex}",
            patch=UpdateVisualCaptionPatch(
                element_kind=EditorialEnrichmentElementKind.FIGURE,
                element_key=figure["element_key"],
                caption="Editorial caption only",
            ),
        )
        result = await _adopt(
            adoption_service, proposal, actor_id=seeded.actor_id, key=f"adopt-{uuid4().hex}"
        )
        catalog_after = (
            await client.get(f"/api/subjects/{seeded.subject.id}/editor/figures")
        ).json()

    assert result.artifact.version == base_artifact.version + 1
    assert catalog_before == catalog_after
    _artifact, payload = await _read_enrichment(app, uow_factory, seeded.run.id)
    revised = editorial_enrichment_from_json(payload)
    revised_figure = next(
        item for item in revised.source_figures if item.key == figure["element_key"]
    )
    assert revised_figure.caption == "Editorial caption only"
    assert revised_figure.resolved_figure is not None
    assert revised_figure.resolved_figure.blob_id == archived_blob_id
    assert revised_figure.resolved_figure.sha256 == archived_sha256
    assert revised_figure.source_url == base_figure.source_url
    assert revised_figure.provenance == base_figure.provenance
    assert revised_figure.locator == base_figure.locator
    async with uow_factory() as uow:
        archived_blob = await uow.blobs.get(archived_blob_id)
        source_document = await uow.source_documents.get(revised_figure.source_document_id)
    assert archived_blob is not None
    assert archived_blob.descriptor.sha256 == archived_sha256
    assert source_document is not None
    assert model_gateway.calls == []
    assert result.proposal.model_run_id is None


@pytest.mark.asyncio
async def test_move_changes_only_placement_and_records_history_and_impact(
    uow_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_app(monkeypatch)
    seeded, app, model_gateway, _content = await _ready_for_manual_table_route(
        uow_factory, base_table_placement_factory=_lead_anchor_placement
    )
    workspace_service, proposal_service, adoption_service = _visual_services(app, uow_factory)
    snapshot = await workspace_service.snapshot(seeded.subject.id)
    table = _enrichment_block(snapshot, kind="table")
    destination = _paragraph_block(snapshot, position=1)
    base_artifact, base_payload = await _read_enrichment(app, uow_factory, seeded.run.id)
    base_table = editorial_enrichment_from_json(base_payload).tables[0]
    proposal = await proposal_service.prepare_visual_revision(
        subject_id=seeded.subject.id,
        snapshot_id=snapshot["snapshot_id"],
        occurrence_id=UUID(table["block_id"]),
        actor_id=seeded.actor_id,
        idempotency_key=f"visual-move-{uuid4().hex}",
        patch=MoveEnrichmentElementPatch(
            element_kind=EditorialEnrichmentElementKind.TABLE,
            element_key=table["element_key"],
            anchor_block_id=UUID(destination["block_id"]),
        ),
    )
    key = f"adopt-{uuid4().hex}"
    result = await _adopt(adoption_service, proposal, actor_id=seeded.actor_id, key=key)
    assert result.artifact.version == base_artifact.version + 1
    _artifact, payload = await _read_enrichment(app, uow_factory, seeded.run.id)
    revised = editorial_enrichment_from_json(payload)
    moved = revised.tables[0]
    assert moved.key == base_table.key
    assert moved.placement == EnrichmentPlacementV1(
        EnrichmentPlacementKind.AFTER_PARAGRAPH,
        anchor_block_id=UUID(destination["block_id"]),
    )
    assert replace(moved, placement=base_table.placement) == base_table
    async with uow_factory() as uow:
        stored = await uow.editorial_proposals.get_for_occurrence(
            seeded.subject.id, proposal.occurrence_id, proposal.id
        )
        accepted_event = await uow.editorial_proposals.get_event_by_idempotency(proposal.id, key)
        impact = await uow.editorial_proposal_impacts.get_by_proposal(proposal.id)
        artifacts = await uow.production_artifacts.list_for_run(seeded.run.id)
    assert stored is not None and stored.state is EditorialProposalState.ACCEPTED
    assert accepted_event is not None
    assert accepted_event.state is EditorialProposalState.ACCEPTED
    assert impact is not None and impact.state.value == "pending"
    assert impact.accepted_snapshot_id == result.snapshot.snapshot_id
    assert [
        artifact.version
        for artifact in artifacts
        if artifact.stage is ProductionArtifactStage.EDITORIAL_ENRICHMENT
    ] == [1, 2]
    assert model_gateway.calls == []
    assert result.proposal.model_run_id is None


@pytest.mark.asyncio
async def test_move_vs_concurrent_edit_has_exactly_one_winner(
    uow_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_app(monkeypatch)
    seeded, app, _model_gateway, _content = await _ready_for_manual_table_route(
        uow_factory, base_table_placement_factory=_lead_anchor_placement
    )
    workspace_service, proposal_service, adoption_service = _visual_services(app, uow_factory)
    snapshot = await workspace_service.snapshot(seeded.subject.id)
    table = _enrichment_block(snapshot, kind="table")
    destination = _paragraph_block(snapshot, position=1)
    move = await proposal_service.prepare_visual_revision(
        subject_id=seeded.subject.id,
        snapshot_id=snapshot["snapshot_id"],
        occurrence_id=UUID(table["block_id"]),
        actor_id=seeded.actor_id,
        idempotency_key=f"visual-move-race-{uuid4().hex}",
        patch=MoveEnrichmentElementPatch(
            element_kind=EditorialEnrichmentElementKind.TABLE,
            element_key=table["element_key"],
            anchor_block_id=UUID(destination["block_id"]),
        ),
    )
    caption = await proposal_service.prepare_visual_revision(
        subject_id=seeded.subject.id,
        snapshot_id=snapshot["snapshot_id"],
        occurrence_id=UUID(table["block_id"]),
        actor_id=seeded.actor_id,
        idempotency_key=f"visual-caption-race-{uuid4().hex}",
        patch=UpdateVisualCaptionPatch(
            element_kind=EditorialEnrichmentElementKind.TABLE,
            element_key=table["element_key"],
            caption="Concurrent caption",
        ),
    )

    results = await asyncio.gather(
        _adopt(adoption_service, move, actor_id=seeded.actor_id, key=f"adopt-{uuid4().hex}"),
        _adopt(adoption_service, caption, actor_id=seeded.actor_id, key=f"adopt-{uuid4().hex}"),
        return_exceptions=True,
    )
    successes = [item for item in results if isinstance(item, EditorialProposalAdoptionResult)]
    failures = [item for item in results if isinstance(item, Exception)]
    assert len(successes) == 1
    assert len(failures) == 1
    loss = failures[0]
    assert isinstance(loss, EditorialProposalAdoptionError)
    assert loss.code in {"proposal_stale", "proposal_base_stale"}
    async with uow_factory() as uow:
        artifacts = await uow.production_artifacts.list_for_run(seeded.run.id)
        proposals = [
            await uow.editorial_proposals.get_for_occurrence(
                seeded.subject.id, proposal.occurrence_id, proposal.id
            )
            for proposal in (move, caption)
        ]
    versions = [
        artifact.version
        for artifact in artifacts
        if artifact.stage is ProductionArtifactStage.EDITORIAL_ENRICHMENT
    ]
    assert versions == [1, 2]
    assert (
        sum(
            1
            for proposal in proposals
            if proposal and proposal.state is EditorialProposalState.ACCEPTED
        )
        == 1
    )
    assert all(
        proposal is None or proposal.state is not EditorialProposalState.PROPOSED
        for proposal in proposals
    )


@pytest.mark.asyncio
async def test_reorder_permutation_under_lock_and_reorder_vs_create_one_winner(
    uow_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_app(monkeypatch)
    seeded, app, _model_gateway, content = await _ready_for_manual_table_route(
        uow_factory, base_table_placement_factory=_lead_anchor_placement
    )
    evidence = content.base_enrichment.tables[0].rows[0].evidence_refs[0]
    evidence_ref = {
        "source_document_id": str(evidence.source_document_id),
        "kind": evidence.kind.value,
        "evidence_key": evidence.evidence_key,
    }
    workspace_service, proposal_service, adoption_service = _visual_services(app, uow_factory)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://editorial.test"
    ) as client:
        snapshot = await workspace_service.snapshot(seeded.subject.id)
        anchor = _paragraph_block(snapshot)
        anchor_id = UUID(anchor["block_id"])
        first = await _create_table_proposal(
            client,
            subject_id=seeded.subject.id,
            snapshot=snapshot,
            anchor_id=anchor_id,
            key="race_table_second",
            question="Which second synthetic command is documented?",
            caption="Second table caption.",
            evidence_ref=evidence_ref,
        )
        await _accept_proposal_via_route(
            client, subject_id=seeded.subject.id, occurrence_id=anchor_id, proposal=first
        )
        snapshot = await workspace_service.snapshot(seeded.subject.id)
        group = [
            block
            for block in snapshot["enrichments"]
            if block["kind"] == "table"
            and (block.get("table") or {}).get("anchor_block_id") == str(anchor_id)
        ]
        assert len(group) == 2
        assert [block["order"] for block in group] == [0, 1]
        for block in group:
            assert block["element_kind"] == "table"
            assert {
                "update_visual_caption",
                "move_enrichment_element",
                "reorder_enrichment_elements",
            } <= set(block["allowed_operations"])
        keys = [block["element_key"] for block in group]
        proposal = await proposal_service.prepare_visual_revision(
            subject_id=seeded.subject.id,
            snapshot_id=snapshot["snapshot_id"],
            occurrence_id=anchor_id,
            actor_id=seeded.actor_id,
            idempotency_key=f"visual-reorder-{uuid4().hex}",
            patch=ReorderEnrichmentElementsPatch(
                anchor_block_id=anchor_id,
                order=(
                    EnrichmentElementRef(EditorialEnrichmentElementKind.TABLE, keys[1]),
                    EnrichmentElementRef(EditorialEnrichmentElementKind.TABLE, keys[0]),
                ),
            ),
        )
        created_view = await _create_table_proposal(
            client,
            subject_id=seeded.subject.id,
            snapshot=snapshot,
            anchor_id=anchor_id,
            key="race_table_third",
            question="Which third synthetic command is documented?",
            caption="Third table caption.",
            evidence_ref=evidence_ref,
        )
        created = await _load_proposal(
            uow_factory, seeded.subject.id, anchor_id, UUID(created_view["id"])
        )
        results = await asyncio.gather(
            _adopt(
                adoption_service,
                proposal,
                actor_id=seeded.actor_id,
                key=f"adopt-{uuid4().hex}",
            ),
            _adopt(
                adoption_service,
                created,
                actor_id=seeded.actor_id,
                key=f"adopt-{uuid4().hex}",
            ),
            return_exceptions=True,
        )
        snapshot = await workspace_service.snapshot(seeded.subject.id)
    successes = [item for item in results if isinstance(item, EditorialProposalAdoptionResult)]
    failures = [item for item in results if isinstance(item, Exception)]
    assert len(successes) == 1
    assert len(failures) == 1
    loss = failures[0]
    assert isinstance(loss, EditorialProposalAdoptionError)
    assert loss.code in {"proposal_stale", "proposal_base_stale"}
    async with uow_factory() as uow:
        artifacts = await uow.production_artifacts.list_for_run(seeded.run.id)
    versions = [
        artifact.version
        for artifact in artifacts
        if artifact.stage is ProductionArtifactStage.EDITORIAL_ENRICHMENT
    ]
    assert versions == [1, 2, 3]
    enriched = editorial_enrichment_from_json(
        await app.state.production_artifact_store.read_json(
            next(
                artifact.canonical_blob_id
                for artifact in artifacts
                if artifact.stage is ProductionArtifactStage.EDITORIAL_ENRICHMENT
                and artifact.version == 3
                and artifact.canonical_blob_id is not None
            )
        )
    )
    reorder_won = successes[0].proposal.operation == "reorder_enrichment_elements"
    assert snapshot["snapshot_id"]
    if reorder_won:
        group_order = {table.key: table.group_order for table in enriched.tables}
        assert sorted(value for value in group_order.values() if value is not None) == [0, 1]
        assert "race_table_third" not in group_order
    else:
        assert {table.key for table in enriched.tables} == {
            "synthetic_table",
            "race_table_second",
            "race_table_third",
        }


@pytest.mark.asyncio
async def test_adoption_replay_is_idempotent(
    uow_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_app(monkeypatch)
    seeded, app, _model_gateway, _content = await _ready_for_manual_table_route(
        uow_factory, base_table_placement_factory=_lead_anchor_placement
    )
    workspace_service, proposal_service, adoption_service = _visual_services(app, uow_factory)
    snapshot = await workspace_service.snapshot(seeded.subject.id)
    table = _enrichment_block(snapshot, kind="table")
    proposal = await proposal_service.prepare_visual_revision(
        subject_id=seeded.subject.id,
        snapshot_id=snapshot["snapshot_id"],
        occurrence_id=UUID(table["block_id"]),
        actor_id=seeded.actor_id,
        idempotency_key=f"visual-replay-{uuid4().hex}",
        patch=UpdateVisualCaptionPatch(
            element_kind=EditorialEnrichmentElementKind.TABLE,
            element_key=table["element_key"],
            caption="Replay caption",
        ),
    )
    key = f"adopt-{uuid4().hex}"
    first = await _adopt(adoption_service, proposal, actor_id=seeded.actor_id, key=key)
    replay = await _adopt(adoption_service, proposal, actor_id=seeded.actor_id, key=key)
    assert replay.replayed is True
    assert replay.artifact.id == first.artifact.id
    assert replay.artifact.version == first.artifact.version == 2
    async with uow_factory() as uow:
        artifacts = await uow.production_artifacts.list_for_run(seeded.run.id)
    assert [
        artifact.version
        for artifact in artifacts
        if artifact.stage is ProductionArtifactStage.EDITORIAL_ENRICHMENT
    ] == [1, 2]


@pytest.mark.asyncio
async def test_stale_proposal_cannot_be_accepted_and_reject_leaves_canonical_unchanged(
    uow_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_app(monkeypatch)
    seeded, app, _model_gateway, _content = await _ready_for_manual_table_route(
        uow_factory, base_table_placement_factory=_lead_anchor_placement
    )
    workspace_service, proposal_service, adoption_service = _visual_services(app, uow_factory)
    snapshot = await workspace_service.snapshot(seeded.subject.id)
    table = _enrichment_block(snapshot, kind="table")
    destination = _paragraph_block(snapshot, position=1)
    base_version, base_payload = await _read_enrichment(app, uow_factory, seeded.run.id)
    rejectable = await proposal_service.prepare_visual_revision(
        subject_id=seeded.subject.id,
        snapshot_id=snapshot["snapshot_id"],
        occurrence_id=UUID(table["block_id"]),
        actor_id=seeded.actor_id,
        idempotency_key=f"visual-reject-caption-{uuid4().hex}",
        patch=UpdateVisualCaptionPatch(
            element_kind=EditorialEnrichmentElementKind.TABLE,
            element_key=table["element_key"],
            caption="Rejected caption",
        ),
    )
    rejected = await proposal_service.reject(
        seeded.subject.id,
        rejectable.occurrence_id,
        rejectable.id,
        actor_id=seeded.actor_id,
        expected_state_version=rejectable.state_version,
        patch_sha256=rejectable.patch_sha256,
        idempotency_key=f"reject-{uuid4().hex}",
        workspace_service=workspace_service,
    )
    assert rejected.state is EditorialProposalState.REJECTED
    version_after_reject, payload_after_reject = await _read_enrichment(
        app, uow_factory, seeded.run.id
    )
    assert version_after_reject.version == base_version.version
    assert payload_after_reject == base_payload

    caption = await proposal_service.prepare_visual_revision(
        subject_id=seeded.subject.id,
        snapshot_id=snapshot["snapshot_id"],
        occurrence_id=UUID(table["block_id"]),
        actor_id=seeded.actor_id,
        idempotency_key=f"visual-stale-caption-{uuid4().hex}",
        patch=UpdateVisualCaptionPatch(
            element_kind=EditorialEnrichmentElementKind.TABLE,
            element_key=table["element_key"],
            caption="Stale caption",
        ),
    )
    move = await proposal_service.prepare_visual_revision(
        subject_id=seeded.subject.id,
        snapshot_id=snapshot["snapshot_id"],
        occurrence_id=UUID(table["block_id"]),
        actor_id=seeded.actor_id,
        idempotency_key=f"visual-stale-move-{uuid4().hex}",
        patch=MoveEnrichmentElementPatch(
            element_kind=EditorialEnrichmentElementKind.TABLE,
            element_key=table["element_key"],
            anchor_block_id=UUID(destination["block_id"]),
        ),
    )
    await _adopt(adoption_service, move, actor_id=seeded.actor_id, key=f"adopt-{uuid4().hex}")
    with pytest.raises(EditorialProposalAdoptionError) as stale:
        await _adopt(
            adoption_service, caption, actor_id=seeded.actor_id, key=f"adopt-{uuid4().hex}"
        )
    assert stale.value.code in {"proposal_stale", "proposal_base_stale"}
    async with uow_factory() as uow:
        stored_caption = await uow.editorial_proposals.get_for_occurrence(
            seeded.subject.id, caption.occurrence_id, caption.id
        )
    assert stored_caption is not None
    assert stored_caption.state is EditorialProposalState.STALE
    version_after_stale, payload_after_stale = await _read_enrichment(
        app, uow_factory, seeded.run.id
    )
    assert version_after_stale.version == base_version.version + 1
    assert editorial_enrichment_from_json(payload_after_stale).tables[0].caption != (
        "Stale caption"
    )
