from __future__ import annotations

import json
from dataclasses import replace
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest

from cti_app.api import editorial_workspace as editorial_workspace_api
from cti_app.api import main as api_main
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.application.production_extraction import references_corpus_hash
from cti_app.application.production_references import (
    ProductionReferenceCorpusV1,
    ProductionReferenceResearchStatus,
    ProductionReferenceSourceV1,
)
from cti_app.application.publication_assembly import _resolve_editorial_placements
from cti_app.config import Settings
from cti_app.domain.collection import CollectionState
from cti_app.domain.editorial_proposal import EditorialProposalOrigin, EditorialProposalState
from cti_app.domain.production import ProductionArtifactStage
from cti_app.domain.production_editorial_enrichment import editorial_enrichment_from_json
from cti_app.domain.publication_document import (
    PublicationDocumentV9,
    parse_publication_document,
)
from cti_app.domain.publication_kind import PublicationKind
from tests.integration.editorial.test_table_insertion_postgres import (
    _NoModelCalls,
    _ready_for_manual_table_route,
)
from tests.test_publication_assembly import _assemble
from tests.test_publication_assembly import _service as _publication_service

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_figure_adoption_makes_zero_gateway_calls(
    uow_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        api_main,
        "get_settings",
        lambda: Settings(_env_file=None, app_env="development", api_bind_host="127.0.0.1"),
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
    seeded, app, model_gateway, _enrichment_content = await _ready_for_manual_table_route(
        uow_factory,
        source_figure_count=2,
    )
    assert isinstance(model_gateway, _NoModelCalls)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://editorial.test"
    ) as client:
        snapshot_response = await client.get(f"/api/subjects/{seeded.subject.id}/editor/snapshot")
        assert snapshot_response.status_code == 200, snapshot_response.text
        snapshot = snapshot_response.json()
        anchor = next(
            block
            for block in snapshot["blocks"]
            if block["kind"] in {"paragraph", "lead_paragraph"}
        )
        anchor_id = UUID(anchor["block_id"])
        assert "create_source_figure" in anchor["allowed_operations"]

        catalog_response = await client.get(
            f"/api/subjects/{seeded.subject.id}/editor/figures?status=available"
        )
        assert catalog_response.status_code == 200, catalog_response.text
        catalog = catalog_response.json()
        selected = [item for item in catalog["items"] if item["selectable"]]
        assert len(selected) == 2
        selections = [
            {
                "figure_id": item["figure_id"],
                "caption": f"Synthetic editorial caption {index + 1}",
                "order": index,
            }
            for index, item in enumerate(selected)
        ]
        proposal_response = await client.post(
            f"/api/subjects/{seeded.subject.id}/editor/blocks/{anchor_id}/figure-proposals",
            headers={"Idempotency-Key": f"manual-figures-{uuid4().hex}"},
            json={
                "snapshot_id": snapshot["snapshot_id"],
                "anchor_block_id": str(anchor_id),
                "figures": selections,
            },
        )
        assert proposal_response.status_code == 200, proposal_response.text
        proposal_view = proposal_response.json()
        assert proposal_view["state"] == EditorialProposalState.PROPOSED.value
        assert proposal_view["operation"] == "create_source_figure"
        proposal_patch = json.dumps(proposal_view["patch"], sort_keys=True)
        assert "blob_id" not in proposal_patch
        proposal_id = UUID(proposal_view["id"])
        async with uow_factory() as uow:
            proposal = await uow.editorial_proposals.get_for_occurrence(
                seeded.subject.id, anchor_id, proposal_id
            )
        assert proposal is not None
        assert proposal.origin is EditorialProposalOrigin.MANUAL_PATCH
        assert proposal.model_run_id is None
        assert proposal.model_turn_id is None
        accepted = await client.post(
            f"/api/subjects/{seeded.subject.id}/editor/threads/{anchor_id}/"
            f"proposals/{proposal_id}/accept",
            headers={"Idempotency-Key": f"accept-figures-{uuid4().hex}"},
            json={
                "expected_state_version": proposal.state_version,
                "patch_sha256": proposal.patch_sha256,
            },
        )
        assert accepted.status_code == 200, accepted.text
        updated_snapshot_response = await client.get(
            f"/api/subjects/{seeded.subject.id}/editor/snapshot"
        )
        assert updated_snapshot_response.status_code == 200, updated_snapshot_response.text
        updated_snapshot = updated_snapshot_response.json()

    assert accepted.status_code == 200, accepted.text
    assert model_gateway.calls == []
    async with uow_factory() as uow:
        current = await uow.production_artifacts.get_current(
            seeded.run.id,
            ProductionArtifactStage.EDITORIAL_ENRICHMENT.value,
        )
        impact = await uow.editorial_proposal_impacts.get_by_proposal(proposal_id)
        assert current is not None and current.version == 2
        payload = await app.state.production_artifact_store.read_json(current.canonical_blob_id)
        enrichment = editorial_enrichment_from_json(payload)
        persisted_snapshot = await uow.editorial_workspace_snapshots.get_by_id(
            UUID(accepted.json()["snapshot_id"])
        )
    assert persisted_snapshot is not None
    assert impact is not None
    assert impact.proposal_id == proposal_id
    assert impact.state.value == "pending"
    assert impact.accepted_snapshot_id == persisted_snapshot.snapshot_id
    inserted = [
        item
        for item in enrichment.source_figures
        if item.caption.startswith("Synthetic editorial caption ")
    ]
    assert len(inserted) == 2
    assert [item.order for item in inserted] == [0, 1]
    assert all(item.resolved_figure is not None for item in inserted)
    assert all(item.width is not None and item.height is not None for item in inserted)
    figure_entries = [
        entry
        for entry in persisted_snapshot.block_registry
        if entry.source_stage.value == "editorial_enrichment" and entry.kind.value == "figure"
    ]
    assert len(figure_entries) == 2
    ordered_figure_entries = sorted(figure_entries, key=lambda item: item.position)
    assert [
        (entry.structural_pointer, entry.transition.value) for entry in ordered_figure_entries
    ] == [("/source_figures/0", "insert"), ("/source_figures/1", "insert")]
    updated_blocks = updated_snapshot["blocks"]
    updated_anchor_index = next(
        index for index, block in enumerate(updated_blocks) if block["block_id"] == str(anchor_id)
    )
    inserted_blocks = [
        block
        for block in updated_blocks
        if block["kind"] == "figure"
        and block["figure"]["caption"].startswith("Synthetic editorial caption ")
    ]
    assert [block["figure"]["caption"] for block in inserted_blocks] == [
        "Synthetic editorial caption 1",
        "Synthetic editorial caption 2",
    ]
    assert [block["figure"]["order"] for block in inserted_blocks] == [0, 1]
    assert all(block["figure"]["anchor_block_id"] == str(anchor_id) for block in inserted_blocks)
    assert all(
        block["figure"]["width"] > 0 and block["figure"]["height"] > 0 for block in inserted_blocks
    )
    assert [UUID(block["block_id"]) for block in inserted_blocks] == [
        entry.block_id for entry in sorted(figure_entries, key=lambda item: item.position)
    ]
    inserted_indices = [updated_blocks.index(block) for block in inserted_blocks]
    assert len(inserted_indices) == 2
    assert inserted_indices[0] < inserted_indices[1]
    same_anchor_blocks = [
        block
        for block in updated_blocks
        if any(
            isinstance(block.get(element_kind), dict)
            and block[element_kind].get("anchor_block_id") == str(anchor_id)
            for element_kind in ("figure", "table", "chart", "diagram")
        )
    ]
    assert all(index > updated_anchor_index for index in inserted_indices)
    # Other elements may share this anchor; their canonical order is position, then element key.
    canonical_anchor_blocks = sorted(
        same_anchor_blocks,
        key=lambda block: (block["position"], block[block["kind"]]["key"]),
    )
    assert [block["block_id"] for block in same_anchor_blocks] == [
        block["block_id"] for block in canonical_anchor_blocks
    ]
    last_figure_index = inserted_indices[-1]
    between_anchor_and_last_figure = updated_blocks[
        updated_anchor_index + 1 : last_figure_index + 1
    ]
    assert all(
        block["kind"] in {"figure", "table", "chart", "diagram"}
        and block[block["kind"]].get("anchor_block_id") == str(anchor_id)
        for block in between_anchor_and_last_figure
    )
    fixture_table = next(
        block
        for block in snapshot["blocks"]
        if block["kind"] == "table" and block["table"]["key"] == "synthetic_table"
    )
    assert fixture_table["table"]["anchor_block_id"] == str(anchor_id)
    fixture_table_index = next(
        index
        for index, block in enumerate(updated_blocks)
        if block["block_id"] == fixture_table["block_id"]
    )
    assert inserted_indices[0] < fixture_table_index < inserted_indices[-1]
    assert all("blob_id" not in json.dumps(block["figure"]) for block in inserted_blocks)

    anchor_entry = next(
        entry for entry in persisted_snapshot.block_registry if entry.block_id == anchor_id
    )
    placements = _resolve_editorial_placements(
        record=persisted_snapshot,
        run=seeded.run,
        synthesis=seeded.synthesis,
        enrichment=enrichment,
        enrichment_artifact_id=current.id,
    )
    figure_placements = [item for item in placements if item.element_kind == "figure"]
    assert [item.element_key for item in figure_placements] == [item.key for item in inserted]
    assert [item.element_position for item in figure_placements] == [0, 1]
    assert all(item.anchor_block_id == anchor_id for item in figure_placements)
    assert all(item.anchor_pointer == anchor_entry.structural_pointer for item in figure_placements)

    source = seeded.extraction.sources[0]
    references = ProductionReferenceCorpusV1(
        schema_version=1,
        subject_id=seeded.subject.id,
        research_date=seeded.production_input.research_date,
        production_input_hash=seeded.production_input.input_hash,
        research_status=ProductionReferenceResearchStatus.COMPLETED,
        sources=(
            ProductionReferenceSourceV1(
                canonical_url=source.canonical_url,
                tier=source.tier,
                kind=source.kind,
                role=source.role,
                title="Synthetic adoption source",
                publisher="Synthetic publisher",
                published_at=None,
                source_collection_id=None,
                source_document_id=source.source_document_id,
                discovery_candidate_ids=(),
                collection_state=CollectionState.EXTRACTED,
                content_sha256=source.content_sha256,
                relevance_reason=None,
                proposed_by_model=False,
                eligible_for_extraction=True,
                editorial_role=source.editorial_role,
            ),
        ),
        warnings=(),
    )
    references = replace(
        references,
        _parsed_payload_sha256=seeded.extraction.references_corpus_hash,
        _parsed_normalized_payload_sha256=references_corpus_hash(references),
    )
    publication_service, catalog, _ = _publication_service()
    assembly = await _assemble(
        publication_service,
        run=seeded.run,
        snapshot=seeded.production_input,
        references=references,
        extraction=seeded.extraction,
        synthesis=seeded.synthesis,
        editorial_enrichment=enrichment,
        publication_kind=PublicationKind.ARTICLE,
        editorial_enrichment_artifact_id=current.id,
        editorial_enrichment_artifact_input_hash=current.input_hash,
        workspace_snapshot_record=persisted_snapshot,
        routing_decision_hash="7" * 64,
        routing_policy_version="synthetic-routing-policy",
    )
    assert assembly.artifact.canonical_blob_id is not None
    publication = parse_publication_document(
        await ProductionArtifactStore(catalog).read_json(assembly.artifact.canonical_blob_id)
    )
    assert isinstance(publication, PublicationDocumentV9)
    inserted_figure_keys = {item.key for item in inserted}
    published_figures = [item for item in publication.figures if item.key in inserted_figure_keys]
    assert [(item.key, item.caption) for item in published_figures] == [
        (item.key, item.caption) for item in inserted
    ]
    published_placements = [
        item
        for item in publication.resolved_placements
        if item.element_kind == "figure" and item.element_key in inserted_figure_keys
    ]
    assert [item.element_key for item in published_placements] == [item.key for item in inserted]
    assert all(item.anchor_block_id == anchor_id for item in published_placements)
    assert all(
        item.anchor_pointer == anchor_entry.structural_pointer for item in published_placements
    )
    published_anchor_placements = [
        item for item in publication.resolved_placements if item.anchor_block_id == anchor_id
    ]
    assert [(item.element_kind, item.element_key) for item in published_anchor_placements] == [
        (block["kind"], block[block["kind"]]["key"]) for block in same_anchor_blocks
    ]
