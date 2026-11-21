from __future__ import annotations

import json
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest

from cti_app.api import editorial_workspace as editorial_workspace_api
from cti_app.api import main as api_main
from cti_app.config import Settings
from cti_app.domain.editorial_proposal import EditorialProposalOrigin, EditorialProposalState
from cti_app.domain.production import ProductionArtifactStage
from cti_app.domain.production_editorial_enrichment import editorial_enrichment_from_json
from tests.integration.editorial.test_table_insertion_postgres import (
    _NoModelCalls,
    _ready_for_manual_table_route,
)

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
        assert current is not None and current.version == 2
        payload = await app.state.production_artifact_store.read_json(current.canonical_blob_id)
        enrichment = editorial_enrichment_from_json(payload)
        persisted_snapshot = await uow.editorial_workspace_snapshots.get_by_id(
            UUID(accepted.json()["snapshot_id"])
        )
    assert persisted_snapshot is not None
    inserted = [
        item
        for item in enrichment.source_figures
        if item.caption.startswith("Synthetic editorial caption ")
    ]
    assert len(inserted) == 2
    assert [item.order for item in inserted] == [0, 1]
    assert all(item.resolved_figure is not None for item in inserted)
    assert all(item.width is not None and item.height is not None for item in inserted)
    assert len(
        [
            entry
            for entry in persisted_snapshot.block_registry
            if entry.source_stage.value == "editorial_enrichment"
            and entry.kind.value == "figure"
        ]
    ) == 2
    updated_blocks = updated_snapshot["blocks"]
    updated_anchor_position = next(
        index for index, block in enumerate(updated_blocks) if block["block_id"] == str(anchor_id)
    )
    inserted_blocks = [block for block in updated_blocks if block["kind"] == "figure"]
    assert [block["figure"]["caption"] for block in inserted_blocks] == [
        "Synthetic editorial caption 1",
        "Synthetic editorial caption 2",
    ]
    assert [block["figure"]["order"] for block in inserted_blocks] == [0, 1]
    assert all(block["figure"]["anchor_block_id"] == str(anchor_id) for block in inserted_blocks)
    assert all(
        block["figure"]["width"] > 0 and block["figure"]["height"] > 0
        for block in inserted_blocks
    )
    assert all(
        block["position"] > updated_blocks[updated_anchor_position]["position"]
        for block in inserted_blocks
    )
    assert all("blob_id" not in json.dumps(block["figure"]) for block in inserted_blocks)
