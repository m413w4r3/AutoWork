"""PostgreSQL oracle for the generic typed enrichment revision route.

Exercises the real HTTP route, the real request-scoped service factory (with the
pinned-artifact loader wired from app state) and the real adoption writer for
caption, move and reorder. The only substitutes are the memory blob store and
the no-network model gateway used by the sibling editorial integration modules.
"""

from __future__ import annotations

from typing import Any, cast
from uuid import UUID, uuid4

import httpx
import pytest

from cti_app.api import editorial_workspace as editorial_workspace_api
from cti_app.domain.editorial_proposal import EditorialProposalState
from cti_app.domain.production import ProductionArtifactStage
from cti_app.domain.production_editorial_enrichment import editorial_enrichment_from_json
from tests.integration.editorial.test_enrichment_visual_revision_postgres import (
    _accept_proposal_via_route,
    _configure_app,
    _create_table_proposal,
    _enrichment_block,
    _lead_anchor_placement,
    _paragraph_block,
    _read_enrichment,
    _seed_ready_workspace_snapshot,
)
from tests.integration.editorial.test_table_insertion_postgres import (
    _ready_for_manual_table_route,
)

pytestmark = pytest.mark.integration


def _request(app: Any) -> Any:
    return type("_Request", (), {"app": app})()


async def _seed_and_snapshot(
    app: Any, uow_factory: Any, subject_id: UUID
) -> tuple[Any, dict[str, Any]]:
    workspace_service = editorial_workspace_api._service(_request(app))
    await _seed_ready_workspace_snapshot(
        workspace_service=workspace_service,
        uow_factory=uow_factory,
        artifact_store=app.state.production_artifact_store,
        subject_id=subject_id,
    )
    snapshot = await workspace_service.snapshot(subject_id)
    return workspace_service, cast(dict[str, Any], snapshot)


async def _post_revision(
    client: httpx.AsyncClient,
    *,
    subject_id: UUID,
    occurrence_id: UUID,
    body: dict[str, object],
) -> httpx.Response:
    return await client.post(
        f"/api/subjects/{subject_id}/editor/blocks/{occurrence_id}/enrichment-revisions",
        headers={"Idempotency-Key": f"visual-revision-{uuid4().hex}"},
        json=body,
    )


async def _accept(
    client: httpx.AsyncClient,
    *,
    subject_id: UUID,
    occurrence_id: UUID,
    proposal: dict[str, Any],
) -> httpx.Response:
    return await client.post(
        f"/api/subjects/{subject_id}/editor/threads/{occurrence_id}/"
        f"proposals/{proposal['id']}/accept",
        headers={"Idempotency-Key": f"accept-{uuid4().hex}"},
        json={
            "expected_state_version": 1,
            "patch_sha256": proposal["patch_sha256"],
        },
    )


async def _history_operations(
    client: httpx.AsyncClient, *, subject_id: UUID, occurrence_id: UUID
) -> list[str]:
    response = await client.get(
        f"/api/subjects/{subject_id}/editor/history",
        params={"block_id": str(occurrence_id)},
    )
    assert response.status_code == 200, response.text
    return [item["operation"] for item in response.json()["items"]]


@pytest.mark.asyncio
async def test_route_caption_prepare_accept_end_to_end(
    uow_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_app(monkeypatch)
    seeded, app, model_gateway, _content = await _ready_for_manual_table_route(
        uow_factory, base_table_placement_factory=_lead_anchor_placement
    )
    _workspace_service, snapshot = await _seed_and_snapshot(app, uow_factory, seeded.subject.id)
    table = _enrichment_block(snapshot, kind="table")
    previous_caption = table["caption"]

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://editorial.test"
    ) as client:
        response = await _post_revision(
            client,
            subject_id=seeded.subject.id,
            occurrence_id=UUID(table["block_id"]),
            body={
                "snapshot_id": snapshot["snapshot_id"],
                "operation": "update_visual_caption",
                "element_kind": "table",
                "element_key": table["element_key"],
                "caption": "Synthetic route caption",
            },
        )
        assert response.status_code == 200, response.text
        proposal = response.json()
        assert proposal["operation"] == "update_visual_caption"
        assert proposal["origin"] == "manual_patch"
        assert proposal["state"] == "proposed"
        assert proposal["patch"]["visual_patch"] == {
            "kind": "update_visual_caption",
            "element_kind": "table",
            "element_key": table["element_key"],
            "caption": "Synthetic route caption",
        }
        accepted = await _accept(
            client,
            subject_id=seeded.subject.id,
            occurrence_id=UUID(table["block_id"]),
            proposal=proposal,
        )
        assert accepted.status_code == 200, accepted.text
        accepted_payload = accepted.json()
        assert accepted_payload["state"] == "accepted"
        assert accepted_payload["impact_state"] == "pending"
        operations = await _history_operations(
            client, subject_id=seeded.subject.id, occurrence_id=UUID(table["block_id"])
        )

    artifact, payload = await _read_enrichment(app, uow_factory, seeded.run.id)
    enrichment = editorial_enrichment_from_json(payload)
    revised = next(item for item in enrichment.tables if item.key == table["element_key"])
    assert artifact.version == 2
    assert revised.caption == "Synthetic route caption"
    assert previous_caption != revised.caption
    assert operations == ["update_visual_caption"]
    assert model_gateway.calls == []
    async with uow_factory() as uow:
        stored = await uow.editorial_proposals.get_for_occurrence(
            seeded.subject.id, UUID(table["block_id"]), UUID(proposal["id"])
        )
        impact = await uow.editorial_proposal_impacts.get_by_proposal(UUID(proposal["id"]))
    assert stored is not None and stored.state is EditorialProposalState.ACCEPTED
    assert stored.model_run_id is None
    assert impact is not None and impact.state.value == "pending"


@pytest.mark.asyncio
async def test_route_move_prepare_accept_end_to_end(
    uow_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_app(monkeypatch)
    seeded, app, model_gateway, _content = await _ready_for_manual_table_route(
        uow_factory, base_table_placement_factory=_lead_anchor_placement
    )
    _workspace_service, snapshot = await _seed_and_snapshot(app, uow_factory, seeded.subject.id)
    table = _enrichment_block(snapshot, kind="table")
    destination = _paragraph_block(snapshot, position=1)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://editorial.test"
    ) as client:
        response = await _post_revision(
            client,
            subject_id=seeded.subject.id,
            occurrence_id=UUID(table["block_id"]),
            body={
                "snapshot_id": snapshot["snapshot_id"],
                "operation": "move_enrichment_element",
                "element_kind": "table",
                "element_key": table["element_key"],
                "anchor_block_id": destination["block_id"],
            },
        )
        assert response.status_code == 200, response.text
        proposal = response.json()
        assert proposal["operation"] == "move_enrichment_element"
        assert proposal["patch"]["target"]["placement"] == {
            "kind": "after_paragraph",
            "anchor_block_id": destination["block_id"],
            "section_id": None,
        }
        accepted = await _accept(
            client,
            subject_id=seeded.subject.id,
            occurrence_id=UUID(table["block_id"]),
            proposal=proposal,
        )
        assert accepted.status_code == 200, accepted.text
        assert accepted.json()["impact_state"] == "pending"
        operations = await _history_operations(
            client, subject_id=seeded.subject.id, occurrence_id=UUID(table["block_id"])
        )

    _artifact, payload = await _read_enrichment(app, uow_factory, seeded.run.id)
    moved = editorial_enrichment_from_json(payload).tables[0]
    assert moved.placement.anchor_block_id == UUID(destination["block_id"])
    assert moved.key == table["element_key"]
    assert operations == ["move_enrichment_element"]
    assert model_gateway.calls == []
    async with uow_factory() as uow:
        stored = await uow.editorial_proposals.get_for_occurrence(
            seeded.subject.id, UUID(table["block_id"]), UUID(proposal["id"])
        )
        artifacts = await uow.production_artifacts.list_for_run(seeded.run.id)
    assert stored is not None and stored.model_run_id is None
    assert [
        artifact.version
        for artifact in artifacts
        if artifact.stage is ProductionArtifactStage.EDITORIAL_ENRICHMENT
    ] == [1, 2]


@pytest.mark.asyncio
async def test_route_reorder_prepare_accept_end_to_end(
    uow_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_app(monkeypatch)
    seeded, app, model_gateway, content = await _ready_for_manual_table_route(
        uow_factory, base_table_placement_factory=_lead_anchor_placement
    )
    _workspace_service, snapshot = await _seed_and_snapshot(app, uow_factory, seeded.subject.id)
    anchor = _paragraph_block(snapshot, position=0)
    anchor_id = UUID(anchor["block_id"])
    evidence = content.base_enrichment.tables[0].rows[0].evidence_refs[0]
    evidence_ref = {
        "source_document_id": str(evidence.source_document_id),
        "kind": evidence.kind.value,
        "evidence_key": evidence.evidence_key,
    }

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://editorial.test"
    ) as client:
        created = await _create_table_proposal(
            client,
            subject_id=seeded.subject.id,
            snapshot=snapshot,
            anchor_id=anchor_id,
            key="route_reorder_second",
            question="Which second synthetic command is documented by the route?",
            caption="Second route table caption.",
            evidence_ref=evidence_ref,
        )
        await _accept_proposal_via_route(
            client, subject_id=seeded.subject.id, occurrence_id=anchor_id, proposal=created
        )
        snapshot = await _workspace_service.snapshot(seeded.subject.id)
        group = [
            block
            for block in snapshot["enrichments"]
            if block["element_kind"] == "table"
            and (block.get("table") or {}).get("anchor_block_id") == str(anchor_id)
        ]
        assert len(group) == 2
        assert [block["order"] for block in group] == [0, 1]
        keys = [block["element_key"] for block in group]
        response = await _post_revision(
            client,
            subject_id=seeded.subject.id,
            occurrence_id=anchor_id,
            body={
                "snapshot_id": snapshot["snapshot_id"],
                "operation": "reorder_enrichment_elements",
                "anchor_block_id": str(anchor_id),
                "order": [
                    {"kind": "table", "key": keys[1]},
                    {"kind": "table", "key": keys[0]},
                ],
            },
        )
        assert response.status_code == 200, response.text
        proposal = response.json()
        assert proposal["operation"] == "reorder_enrichment_elements"
        assert proposal["patch"]["target"]["kind"] == "enrichment_order"
        accepted = await _accept(
            client,
            subject_id=seeded.subject.id,
            occurrence_id=anchor_id,
            proposal=proposal,
        )
        assert accepted.status_code == 200, accepted.text
        assert accepted.json()["impact_state"] == "pending"
        operations = await _history_operations(
            client, subject_id=seeded.subject.id, occurrence_id=anchor_id
        )
        snapshot_after = await _workspace_service.snapshot(seeded.subject.id)

    _artifact, payload = await _read_enrichment(app, uow_factory, seeded.run.id)
    enrichment = editorial_enrichment_from_json(payload)
    assert enrichment.schema_version == 11
    ordered = [
        block
        for block in snapshot_after["enrichments"]
        if block["element_kind"] == "table"
        and (block.get("table") or {}).get("anchor_block_id") == str(anchor_id)
    ]
    assert [block["element_key"] for block in ordered] == [keys[1], keys[0]]
    assert [block["order"] for block in ordered] == [0, 1]
    group_order = {table.key: table.group_order for table in enrichment.tables}
    assert group_order[keys[0]] == 1
    assert group_order[keys[1]] == 0
    assert operations == ["reorder_enrichment_elements", "create_table"]
    assert model_gateway.calls == []
    async with uow_factory() as uow:
        stored = await uow.editorial_proposals.get_for_occurrence(
            seeded.subject.id, anchor_id, UUID(proposal["id"])
        )
        artifacts = await uow.production_artifacts.list_for_run(seeded.run.id)
    assert stored is not None and stored.model_run_id is None
    assert [
        artifact.version
        for artifact in artifacts
        if artifact.stage is ProductionArtifactStage.EDITORIAL_ENRICHMENT
    ] == [1, 2, 3]
