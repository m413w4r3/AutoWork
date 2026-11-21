from __future__ import annotations

import hashlib
from dataclasses import replace
from types import SimpleNamespace
from typing import cast
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from starlette.requests import Request

from cti_app.api import editorial_workspace as editorial_workspace_api
from cti_app.application.editorial_proposal_adoption import (
    EditorialProposalAdoptionError,
    _proposal_pins_match,
    _proposal_stage,
)
from cti_app.application.editorial_proposals import (
    EditorialProposalApplicationError,
    _editorial_patch_text_violation,
    _manual_table_evidence_handles,
)
from cti_app.application.editorial_proposals import (
    _proposal_pins_match as _proposal_inspection_pins_match,
)
from cti_app.application.editorial_workspace.registry import (
    WorkspaceOccurrenceResolution,
    WorkspaceSnapshotRecord,
    allowed_operations,
    project_blocks,
    revision_target_path,
    table_insertion_preparation_ready,
)
from cti_app.application.editorial_workspace.service import (
    EditorialRevisionTargetResolution,
    EditorialWorkspaceService,
)
from cti_app.domain.editorial_workspace import (
    BlockRegistryEntry,
    EditorialBlockKind,
    EditorialSourceStage,
    canonical_content_sha256,
)
from cti_app.domain.production import (
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionArtifactStatus,
)
from cti_app.domain.production_editorial_revision import (
    TargetedEditorialRevisionPlacement,
    TargetedEditorialRevisionPlacementKind,
)
from cti_app.domain.publication_kind import PublicationKind


def _paragraph_registry_entry() -> tuple[BlockRegistryEntry, dict[str, object]]:
    paragraph: dict[str, object] = {"text": "Synthetic paragraph with an admitted claim."}
    section: dict[str, object] = {
        "kind": "overview",
        "heading": "Synthetic observations",
        "paragraphs": [paragraph],
    }
    synthesis: dict[str, object] = {
        "title": "Synthetic report",
        "lead": [],
        "sections": [section],
        "timeline": [],
        "uncertainties": [],
        "warnings": [],
    }
    entry = BlockRegistryEntry(
        block_id=uuid4(),
        kind=EditorialBlockKind.PARAGRAPH,
        parent_block_id=None,
        position=0,
        content_sha256=canonical_content_sha256(paragraph),
        source_stage=EditorialSourceStage.SYNTHESIS,
        structural_pointer="/sections/0/paragraphs/0",
    )
    return entry, synthesis


def _workspace_record(
    *,
    subject_id: UUID,
    production_run_id: UUID,
    synthesis_artifact_id: UUID,
    synthesis_sha256: str,
    entries: tuple[BlockRegistryEntry, ...],
    enrichment_artifact: ProductionArtifact | None = None,
    enrichment_sha256: str | None = None,
) -> WorkspaceSnapshotRecord:
    return WorkspaceSnapshotRecord(
        snapshot_id=uuid4(),
        subject_id=subject_id,
        production_run_id=production_run_id,
        synthesis_artifact_id=synthesis_artifact_id,
        synthesis_version=1,
        synthesis_input_hash="a" * 64,
        synthesis_canonical_sha256=synthesis_sha256,
        enrichment_artifact_id=enrichment_artifact.id if enrichment_artifact else None,
        enrichment_version=enrichment_artifact.version if enrichment_artifact else None,
        enrichment_input_hash=enrichment_artifact.input_hash if enrichment_artifact else None,
        enrichment_canonical_sha256=enrichment_sha256,
        publication_artifact_id=None,
        publication_version=None,
        publication_input_hash=None,
        publication_canonical_sha256=None,
        pipeline_generation=1,
        routing_decision_id=None,
        routing_decision_hash=None,
        publication_kind=None,
        workspace_pin_hash="b" * 64,
        registry_hash="c" * 64,
        block_registry=tuple(entries),
    )


def _table_spec(anchor, evidence_refs):
    from cti_app.domain.production_editorial_enrichment import (
        EditorialAnalyticPurposeV1,
        EnrichmentPlacementKind,
        EnrichmentPlacementV1,
        EnrichmentTableKind,
        TableColumnV1,
        TableRowV1,
        TableSpecV1,
    )

    return TableSpecV1(
        key="synthetic_tool_inventory",
        kind=EnrichmentTableKind.CUSTOM,
        title="Synthetic tool inventory",
        caption="Every row cites admitted synthetic evidence.",
        columns=(TableColumnV1("tool", "Tool"), TableColumnV1("purpose", "Purpose")),
        rows=(TableRowV1(("synthetic-tool", "Analyst tooling"), tuple(evidence_refs)),),
        placement=EnrichmentPlacementV1(
            kind=EnrichmentPlacementKind.AFTER_PARAGRAPH,
            anchor_block_id=anchor,
        ),
        purpose=EditorialAnalyticPurposeV1(
            question="Which synthetic tools are documented?",
            available_data="Values in the cited synthetic row.",
            comprehension_gain="Compare documented values in one table.",
            scope="The cited synthetic row.",
            evidence_refs=tuple(evidence_refs),
            knowledge_limits="No uncited value is included.",
            placement_reason="After the selected paragraph.",
        ),
    )


def test_table_insertion_is_offered_for_a_registered_paragraph() -> None:
    entry, synthesis = _paragraph_registry_entry()

    assert "create_table" not in allowed_operations(entry, synthesis)
    subject_id, run_id = uuid4(), uuid4()
    synthesis_artifact_id, enrichment_artifact_id = uuid4(), uuid4()
    synthesis_sha256, enrichment_sha256 = "d" * 64, "e" * 64
    synthesis_artifact = ProductionArtifact(
        production_run_id=run_id,
        subject_id=subject_id,
        stage=ProductionArtifactStage.SYNTHESIS,
        version=1,
        input_hash="a" * 64,
        canonical_blob_id=uuid4(),
        id=synthesis_artifact_id,
    )
    enrichment_artifact = ProductionArtifact(
        production_run_id=run_id,
        subject_id=subject_id,
        stage=ProductionArtifactStage.EDITORIAL_ENRICHMENT,
        version=1,
        input_hash="f" * 64,
        canonical_blob_id=uuid4(),
        id=enrichment_artifact_id,
    )
    record = _workspace_record(
        subject_id=subject_id,
        production_run_id=run_id,
        synthesis_artifact_id=synthesis_artifact_id,
        synthesis_sha256=synthesis_sha256,
        entries=(entry,),
        enrichment_artifact=enrichment_artifact,
        enrichment_sha256=enrichment_sha256,
    )

    def ready_to_prepare(
        *,
        table_artifact: ProductionArtifact = enrichment_artifact,
        table_read_sha256: str = enrichment_sha256,
        anchor_registered: bool = True,
    ) -> bool:
        return table_insertion_preparation_ready(
            record=record,
            entry=entry,
            synthesis_artifact=synthesis_artifact,
            synthesis_read_sha256=synthesis_sha256,
            enrichment_artifact=table_artifact,
            enrichment_read_sha256=table_read_sha256,
            anchor_registered=anchor_registered,
        )

    ready = ready_to_prepare()
    assert ready
    assert "create_table" in allowed_operations(entry, synthesis, allow_table_insertion=ready)
    assert "create_source_figure" not in allowed_operations(entry, synthesis)
    assert "create_source_figure" in allowed_operations(
        entry, synthesis, allow_figure_insertion=ready
    )

    def projected_operations(
        *,
        inventory_available: bool,
        pinned_enrichment: ProductionArtifact = enrichment_artifact,
    ) -> list[str]:
        blocks, _, _ = project_blocks(
            entries=(entry,),
            synthesis=synthesis,
            enrichment={},
            record=record,
            synthesis_artifact=synthesis_artifact,
            synthesis_read_sha256=synthesis_sha256,
            enrichment_artifact=pinned_enrichment,
            enrichment_read_sha256=enrichment_sha256,
            allow_figure_insertion=inventory_available,
        )
        return cast(list[str], blocks[0]["allowed_operations"])

    assert "create_source_figure" not in projected_operations(inventory_available=False)
    assert "create_source_figure" in projected_operations(inventory_available=True)
    assert "create_source_figure" not in projected_operations(
        inventory_available=True,
        pinned_enrichment=replace(enrichment_artifact, status=ProductionArtifactStatus.STALE),
    )

    for not_ready in (
        ready_to_prepare(
            table_artifact=replace(enrichment_artifact, status=ProductionArtifactStatus.STALE)
        ),
        ready_to_prepare(table_artifact=replace(enrichment_artifact, input_hash="0" * 64)),
        ready_to_prepare(table_read_sha256="0" * 64),
        ready_to_prepare(anchor_registered=False),
    ):
        assert not not_ready
        assert "create_table" not in allowed_operations(
            entry, synthesis, allow_table_insertion=not_ready
        )
        assert "create_source_figure" not in allowed_operations(
            entry, synthesis, allow_figure_insertion=not_ready
        )


def test_snapshot_orders_enrichments_by_position_then_element_key() -> None:
    subject_id, run_id, synthesis_artifact_id, enrichment_artifact_id = (uuid4() for _ in range(4))
    anchor_id = uuid4()
    paragraph = {"text": "Synthetic registered paragraph."}
    synthesis: dict[str, object] = {
        "title": "Synthetic report",
        "lead": [paragraph],
        "sections": [],
        "timeline": [],
        "uncertainties": [],
        "warnings": [],
    }
    synthesis_sha256 = canonical_content_sha256(synthesis)
    anchor_entry = BlockRegistryEntry(
        block_id=anchor_id,
        kind=EditorialBlockKind.LEAD_PARAGRAPH,
        parent_block_id=None,
        position=0,
        content_sha256=canonical_content_sha256(paragraph),
        source_stage=EditorialSourceStage.SYNTHESIS,
        structural_pointer="/lead/0",
    )
    placement = {"kind": "after_paragraph", "anchor_block_id": str(anchor_id)}
    table = {"key": "z_table", "title": "Z table", "placement": placement}
    charts = [
        {"key": "a_chart", "title": "A chart", "placement": placement},
        {"key": "z_chart", "title": "Z chart", "placement": placement},
    ]
    enrichment: dict[str, object] = {"tables": [table], "charts": charts}
    enrichment_sha256 = canonical_content_sha256(enrichment)
    enrichment_artifact = ProductionArtifact(
        production_run_id=run_id,
        subject_id=subject_id,
        stage=ProductionArtifactStage.EDITORIAL_ENRICHMENT,
        version=1,
        input_hash="f" * 64,
        canonical_blob_id=uuid4(),
        id=enrichment_artifact_id,
    )
    table_entry = BlockRegistryEntry(
        block_id=uuid4(),
        kind=EditorialBlockKind.TABLE,
        parent_block_id=None,
        position=0,
        content_sha256=canonical_content_sha256(table),
        source_stage=EditorialSourceStage.EDITORIAL_ENRICHMENT,
        structural_pointer="/tables/0",
    )
    chart_entries = tuple(
        BlockRegistryEntry(
            block_id=uuid4(),
            kind=EditorialBlockKind.CHART,
            parent_block_id=None,
            position=index,
            content_sha256=canonical_content_sha256(chart),
            source_stage=EditorialSourceStage.EDITORIAL_ENRICHMENT,
            structural_pointer=f"/charts/{index}",
        )
        for index, chart in enumerate(charts)
    )
    entries = (anchor_entry, table_entry, *chart_entries)
    record = _workspace_record(
        subject_id=subject_id,
        production_run_id=run_id,
        synthesis_artifact_id=synthesis_artifact_id,
        synthesis_sha256=synthesis_sha256,
        entries=entries,
        enrichment_artifact=enrichment_artifact,
        enrichment_sha256=enrichment_sha256,
    )

    blocks, _, _ = project_blocks(
        entries=entries,
        synthesis=synthesis,
        enrichment=enrichment,
        record=record,
        allow_operations=False,
    )

    anchor_index = next(
        index for index, block in enumerate(blocks) if block["block_id"] == str(anchor_id)
    )
    assert [block["block_id"] for block in blocks[anchor_index + 1 : anchor_index + 4]] == [
        str(chart_entries[0].block_id),
        str(table_entry.block_id),
        str(chart_entries[1].block_id),
    ]


def test_snapshot_projects_pinned_table_after_its_registered_anchor() -> None:
    anchor_entry, synthesis = _paragraph_registry_entry()
    anchor_id = anchor_entry.block_id
    evidence_ref = {
        "source_document_id": str(uuid4()),
        "kind": "fact",
        "evidence_key": hashlib.sha256(b"snapshot-table-evidence").hexdigest(),
    }
    table = {
        "key": "snapshot_table",
        "kind": "custom",
        "title": "Pinned synthetic table",
        "caption": "Read from the pinned enrichment.",
        "columns": [
            {"key": "finding", "label": "Finding"},
            {"key": "source", "label": "Source"},
        ],
        "rows": [
            {
                "cells": ["Synthetic finding", "Synthetic source"],
                "evidence_refs": [evidence_ref],
            }
        ],
        "placement": {"kind": "after_paragraph", "anchor_block_id": str(anchor_id)},
        "purpose": {"evidence_refs": [evidence_ref]},
    }
    table_entry = BlockRegistryEntry(
        block_id=uuid4(),
        kind=EditorialBlockKind.TABLE,
        parent_block_id=None,
        position=0,
        content_sha256=canonical_content_sha256(table),
        source_stage=EditorialSourceStage.EDITORIAL_ENRICHMENT,
        structural_pointer="/tables/0",
    )
    synthesis_artifact_id, enrichment_artifact_id = uuid4(), uuid4()
    subject_id, run_id = uuid4(), uuid4()
    record = _workspace_record(
        subject_id=subject_id,
        production_run_id=run_id,
        synthesis_artifact_id=synthesis_artifact_id,
        synthesis_sha256="d" * 64,
        entries=(anchor_entry, table_entry),
        enrichment_artifact=ProductionArtifact(
            production_run_id=run_id,
            subject_id=subject_id,
            stage=ProductionArtifactStage.EDITORIAL_ENRICHMENT,
            version=1,
            input_hash="f" * 64,
            canonical_blob_id=uuid4(),
            id=enrichment_artifact_id,
        ),
        enrichment_sha256="e" * 64,
    )
    blocks, _, _ = project_blocks(
        entries=(anchor_entry, table_entry),
        synthesis=synthesis,
        enrichment={"tables": [table]},
        record=record,
        allow_operations=False,
    )

    anchor_index = next(
        index for index, block in enumerate(blocks) if block["block_id"] == str(anchor_id)
    )
    anchor_block = blocks[anchor_index]
    table_block = next(block for block in blocks if block["block_id"] == str(table_entry.block_id))
    assert blocks[anchor_index + 1] is table_block
    projected_anchor = editorial_workspace_api.EditorialWorkspaceBlock.model_validate(anchor_block)
    projected_block = editorial_workspace_api.EditorialWorkspaceBlock.model_validate(table_block)
    assert projected_anchor.table is None
    projected = projected_block.table
    assert projected is not None
    assert projected.title == "Pinned synthetic table"
    assert projected.caption == "Read from the pinned enrichment."
    assert [(column.key, column.label) for column in projected.columns] == [
        ("finding", "Finding"),
        ("source", "Source"),
    ]
    assert projected.rows[0].cells == ["Synthetic finding", "Synthetic source"]
    assert projected.rows[0].evidence_refs == [evidence_ref]
    assert projected.evidence_refs == [evidence_ref]
    assert projected.anchor_block_id == anchor_id
    assert projected.placement == "after_paragraph"


def test_table_insertion_request_uses_the_exact_registered_anchor() -> None:
    from cti_app.domain.editorial_proposal import ManualPatch, ManualTablePatch
    from cti_app.domain.production_synthesis import EvidenceKind, ExtractionEvidenceRefV1

    anchor = uuid4()
    reference = ExtractionEvidenceRefV1(
        uuid4(), EvidenceKind.FACT, hashlib.sha256(b"fact-1").hexdigest()
    )
    table = _table_spec(anchor, (reference,))
    typed = ManualTablePatch(
        anchor_block_id=anchor,
        table=table,
    )
    patch = ManualPatch(typed_patch=typed)

    assert patch.typed_patch.anchor_block_id == anchor
    assert typed.operation.value == "create_table"
    assert TargetedEditorialRevisionPlacement(
        kind=TargetedEditorialRevisionPlacementKind.AFTER_PARAGRAPH,
        anchor_block_id=anchor,
    ).to_json() == {
        "kind": "after_paragraph",
        "section_id": None,
        "anchor_block_id": str(anchor),
    }


def test_manual_table_rejects_row_evidence_outside_the_admitted_pack() -> None:
    from cti_app.domain.production_synthesis import EvidenceKind, ExtractionEvidenceRefV1

    reference = ExtractionEvidenceRefV1(
        uuid4(), EvidenceKind.FACT, hashlib.sha256(b"not-admitted").hexdigest()
    )
    table = _table_spec(uuid4(), (reference,))

    with pytest.raises(EditorialProposalApplicationError) as error:
        _manual_table_evidence_handles(table, handles_by_ref={})

    assert error.value.code == "manual_edit_new_fact_unadmitted"


@pytest.mark.asyncio
async def test_table_patch_keeps_anchor_through_adoption() -> None:
    from cti_app.domain.editorial_proposal import (
        EditorialProposal,
        EditorialProposalOrigin,
        EditorialProposalState,
    )
    from cti_app.domain.production import ProductionArtifactStage
    from cti_app.domain.production_editorial_enrichment import EditorialEnrichmentElementKind
    from cti_app.domain.production_editorial_revision import TargetedEditorialRevisionOperation

    anchor = uuid4()
    synthesis_artifact_id = uuid4()
    enrichment_artifact_id = uuid4()
    subject_id = uuid4()
    run_id = uuid4()
    entry = BlockRegistryEntry(
        block_id=anchor,
        kind=EditorialBlockKind.PARAGRAPH,
        parent_block_id=None,
        position=2,
        content_sha256="a" * 64,
        source_stage=EditorialSourceStage.SYNTHESIS,
        structural_pointer="/sections/0/paragraphs/0",
    )
    enrichment_artifact = ProductionArtifact(
        id=enrichment_artifact_id,
        production_run_id=run_id,
        subject_id=subject_id,
        stage=ProductionArtifactStage.EDITORIAL_ENRICHMENT,
        version=5,
        input_hash="f" * 64,
        status=ProductionArtifactStatus.VERIFIED,
        canonical_blob_id=uuid4(),
    )
    record = _workspace_record(
        subject_id=subject_id,
        production_run_id=run_id,
        synthesis_artifact_id=synthesis_artifact_id,
        synthesis_sha256="e" * 64,
        entries=(entry,),
        enrichment_artifact=enrichment_artifact,
        enrichment_sha256="1" * 64,
    )
    proposal = EditorialProposal(
        id=uuid4(),
        subject_id=subject_id,
        production_run_id=run_id,
        origin=EditorialProposalOrigin.MANUAL_PATCH,
        snapshot_id=record.snapshot_id,
        occurrence_id=anchor,
        base_artifact_id=enrichment_artifact_id,
        base_artifact_version=record.enrichment_version or 0,
        base_input_hash=record.enrichment_input_hash or "",
        base_canonical_sha256=record.enrichment_canonical_sha256 or "",
        base_routing_hash=record.routing_decision_hash,
        base_pin_hash=record.workspace_pin_hash,
        target_id="synthetic-table-target",
        operation=TargetedEditorialRevisionOperation.CREATE_TABLE.value,
        before_hash=entry.content_sha256,
        patch_blob_ref=uuid4(),
        patch_sha256="2" * 64,
        evidence_refs=(),
        diagnostics={},
        thread_id=None,
        variant_group_id=uuid4(),
        model_run_id=None,
        model_turn_id=None,
        state=EditorialProposalState.PROPOSED,
        state_version=1,
        actor_id=uuid4(),
        idempotency_key="table-proposal-00000001",
        request_sha256="3" * 64,
    )
    resolution = WorkspaceOccurrenceResolution(
        record=record,
        entry=entry,
        binding_state="valid",
        artifact_id=synthesis_artifact_id,
        artifact_version=record.synthesis_version,
        canonical_sha256=record.synthesis_canonical_sha256,
    )
    patch = {
        "target": {
            "kind": "enrichment_element",
            "element_kind": EditorialEnrichmentElementKind.TABLE.value,
            "element_key": None,
            "placement": {
                "kind": "after_paragraph",
                "anchor_block_id": str(anchor),
            },
        }
    }

    assert (
        _proposal_stage(proposal, patch, resolution) is ProductionArtifactStage.EDITORIAL_ENRICHMENT
    )
    assert _proposal_pins_match(proposal, resolution)
    assert _proposal_inspection_pins_match(proposal, resolution)
    stale_resolution = replace(resolution, record=replace(record, enrichment_version=6))
    assert not _proposal_pins_match(proposal, stale_resolution)
    assert not _proposal_inspection_pins_match(proposal, stale_resolution)

    from cti_app.application.editorial_proposal_adoption_inputs import (
        ProductionEditorialProposalAdoptionInputs,
    )

    inputs = ProductionEditorialProposalAdoptionInputs(
        uow_factory=object(),
        artifact_store=object(),
        workspace_service=object(),
        synthesis_loader=object(),
        enrichment_loader=object(),
        enrichment_revision_service=object(),
    )
    await inputs._revalidate_thread(None, proposal, resolution)

    patch["target"]["placement"]["anchor_block_id"] = str(uuid4())
    with pytest.raises(EditorialProposalAdoptionError, match="proposal_target_mismatch"):
        _proposal_stage(proposal, patch, resolution)


def test_revision_effect_conflict_has_closed_reason_and_origin_frame() -> None:
    from cti_app.application.editorial_revision_effects import EditorialRevisionEffectsError

    with pytest.raises(EditorialRevisionEffectsError) as caught:
        raise EditorialRevisionEffectsError("proposal_state_conflict")

    assert caught.value.reason == "stale_transition_cas_conflict"
    assert caught.value.frame is not None
    assert caught.value.frame.startswith("test_editorial_table_insertion.py:")
    assert caught.value.frame.endswith(
        ":test_revision_effect_conflict_has_closed_reason_and_origin_frame"
    )
    with pytest.raises(ValueError, match="closed reason"):
        EditorialRevisionEffectsError("proposal_state_conflict", reason="untrusted source text")


@pytest.mark.asyncio
async def test_accept_route_logs_closed_rejection_reason_and_source_frame(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from cti_app.application.editorial_revision_effects import EditorialConflictReason

    subject_id, occurrence_id, proposal_id, actor_id = (uuid4() for _ in range(4))

    class AdoptionService:
        async def adopt(self, *_args, **_kwargs):
            raise EditorialProposalAdoptionError(
                "proposal_state_conflict",
                reason=EditorialConflictReason.EXPECTED_STATE_VERSION_MISMATCH,
            )

    async def actor_id_for_request(_request: Request) -> UUID:
        return actor_id

    monkeypatch.setattr(editorial_workspace_api, "_proposal_impact_consumer_wired", lambda: True)
    monkeypatch.setattr(
        editorial_workspace_api, "_proposal_adoption_service", lambda _request: AdoptionService()
    )
    monkeypatch.setattr(editorial_workspace_api, "_thread_actor_id", actor_id_for_request)
    app = FastAPI()
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/accept",
            "headers": [],
            "query_string": b"",
            "server": ("editorial.test", 80),
            "client": ("test", 1),
            "app": app,
        }
    )
    payload = editorial_workspace_api.EditorialProposalAcceptRequest(
        expected_state_version=1,
        patch_sha256="a" * 64,
    )

    with caplog.at_level("WARNING"):
        with pytest.raises(HTTPException) as caught:
            await editorial_workspace_api.accept_editorial_proposal(
                subject_id,
                occurrence_id,
                proposal_id,
                request,
                payload,
                "accept-table-decision-0001",
            )

    assert caught.value.status_code == 409
    assert caught.value.detail["code"] == "proposal_state_conflict"
    assert "proposal_decision_rejected code=proposal_state_conflict" in caplog.text
    assert "reason=expected_state_version_mismatch" in caplog.text
    assert "exc_type=EditorialProposalAdoptionError" in caplog.text
    assert "proposal_id=" + str(proposal_id) in caplog.text
    assert "frame=test_editorial_table_insertion.py:" in caplog.text
    assert ":adopt" in caplog.text
    assert "Synthetic" not in caplog.text


def test_proposal_state_conflict_rejects_non_vocabulary_reason() -> None:
    with pytest.raises(ValueError, match="closed reason"):
        EditorialProposalAdoptionError(
            "proposal_state_conflict",
            reason="proposal state did not match user prose",
        )


def test_manual_table_text_rejects_markup() -> None:
    assert _editorial_patch_text_violation("<b>forged cell</b>")
    assert _editorial_patch_text_violation('#raw("forged Typst")')
    assert _editorial_patch_text_violation("https://example.test/report")
    assert _editorial_patch_text_violation("www.example.test/report")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field_name", "unsafe_text"),
    [
        ("cell", '#raw(read("x"))'),
        ("title", '#include("x")'),
        ("caption", '#link("https://example.test", "link")'),
        ("caption", "ftp://example.test/a"),
        ("caption", "mailto:user@example.test"),
        ("cell", "<b>forged cell</b>"),
        ("purpose", "This [E001] citation must stay in its structured field."),
    ],
)
async def test_manual_table_input_rejects_unsafe_text_with_stable_code(
    field_name: str, unsafe_text: str
) -> None:
    from cti_app.application.editorial_proposals import EditorialProposalService
    from cti_app.domain.editorial_proposal import ManualTablePatch
    from cti_app.domain.production_synthesis import EvidenceKind, ExtractionEvidenceRefV1

    anchor_id = uuid4()
    evidence_ref = ExtractionEvidenceRefV1(
        uuid4(), EvidenceKind.FACT, hashlib.sha256(b"manual table evidence").hexdigest()
    )
    table = _table_spec(anchor_id, (evidence_ref,))
    if field_name == "cell":
        row = replace(table.rows[0], cells=(unsafe_text, table.rows[0].cells[1]))
        table = replace(table, rows=(row,))
    elif field_name in {"title", "caption"}:
        table = replace(table, **{field_name: unsafe_text})
    elif field_name == "purpose":
        assert table.purpose is not None
        table = replace(table, purpose=replace(table.purpose, question=unsafe_text))
    else:
        raise AssertionError(f"Unhandled test field: {field_name}")
    service = EditorialProposalService(
        uow_factory=object(),
        artifact_store=object(),
        workspace_service=object(),
        discussion_context_builder=object(),
    )

    with pytest.raises(EditorialProposalApplicationError) as caught:
        await service._prepare_manual_table_patch(
            subject_id=uuid4(),
            snapshot_id=uuid4(),
            occurrence_id=anchor_id,
            actor_id=uuid4(),
            idempotency_key="manual-table-markup-test-0001",
            typed_patch=ManualTablePatch(anchor_block_id=anchor_id, table=table),
        )

    assert caught.value.code == "manual_edit_patch_invalid"
    assert caught.value.reason == "manual_table:markup_rejected"


@pytest.mark.parametrize("origin_value", ["bridge_draft", "manual_patch"])
@pytest.mark.parametrize(
    ("row_count", "invalid_field", "invalid_text", "expected_state", "expected_validator_code"),
    [
        (2, None, None, "proposed", None),
        (1, None, None, "needs_review", "editorial_enrichment_output_invalid"),
        (
            2,
            "cell",
            "203.0.113.99",
            "needs_review",
            "editorial_enrichment_unknown_technical_value",
        ),
        (2, "cell", '#raw(read("x"))', "needs_review", "revision_wire_markup_forbidden"),
        (2, "key", "table [E001] key", "needs_review", "revision_wire_markup_forbidden"),
        (2, "title", '#include("x")', "needs_review", "revision_wire_markup_forbidden"),
        (
            2,
            "caption",
            '#link("https://example.test", "link")',
            "needs_review",
            "revision_wire_markup_forbidden",
        ),
        (
            2,
            "title",
            "https://example.test/report",
            "needs_review",
            "revision_wire_markup_forbidden",
        ),
        (
            2,
            "caption",
            "www.example.test/report",
            "needs_review",
            "revision_wire_markup_forbidden",
        ),
        (
            2,
            "column_label",
            "https://example.test/source",
            "needs_review",
            "revision_wire_markup_forbidden",
        ),
        (
            2,
            "cell",
            "www.example.test/value",
            "needs_review",
            "revision_wire_markup_forbidden",
        ),
        (2, "cell", "<b>forged cell</b>", "needs_review", "revision_wire_markup_forbidden"),
        (
            2,
            "column_key",
            "column [E001] key",
            "needs_review",
            "revision_wire_markup_forbidden",
        ),
        (
            2,
            "column_label",
            "column [E001] label",
            "needs_review",
            "revision_wire_markup_forbidden",
        ),
        (
            2,
            "purpose_question",
            "This [E001] citation must stay in its structured field.",
            "needs_review",
            "revision_wire_markup_forbidden",
        ),
        (
            2,
            "purpose_available_data",
            "Available data [E001] remains prose, not a handle.",
            "needs_review",
            "revision_wire_markup_forbidden",
        ),
        (
            2,
            "purpose_comprehension_gain",
            "Compare [E001] with source text.",
            "needs_review",
            "revision_wire_markup_forbidden",
        ),
        (
            2,
            "purpose_scope",
            "Scope [E001] stays plain text.",
            "needs_review",
            "revision_wire_markup_forbidden",
        ),
        (
            2,
            "purpose_knowledge_limits",
            "Limits [E001] stay plain text.",
            "needs_review",
            "revision_wire_markup_forbidden",
        ),
        (
            2,
            "purpose_placement_reason",
            "Placement [E001] stays plain text.",
            "needs_review",
            "revision_wire_markup_forbidden",
        ),
    ],
)
@pytest.mark.asyncio
async def test_typed_table_wire_uses_real_validator_for_registered_anchor_payload(
    row_count: int,
    invalid_field: str | None,
    invalid_text: str | None,
    expected_state: str,
    expected_validator_code: str | None,
    origin_value: str,
) -> None:
    import json

    from cti_app.application.editorial_proposals import (
        EditorialProposalPreparation,
        EditorialProposalService,
        _manual_table_failure_from_diagnostics,
    )
    from cti_app.application.production_editorial_enrichment import (
        EditorialEnrichmentEvidencePackV1,
    )
    from cti_app.application.production_synthesis import canonical_extraction_hash
    from cti_app.domain.discovery import SourceRole
    from cti_app.domain.editorial_proposal import (
        EditorialProposalEventKind,
        EditorialProposalOrigin,
        EditorialProposalState,
    )
    from cti_app.domain.production import ExtractionProfile, ProductionEvidenceBasis
    from cti_app.domain.production_editorial_enrichment import EditorialEnrichmentElementKind
    from cti_app.domain.production_editorial_revision import (
        TargetedEditorialRevisionOperation,
        TargetedEditorialRevisionPlacement,
        TargetedEditorialRevisionPlacementKind,
        TargetedEditorialRevisionTarget,
        TargetedEditorialRevisionTargetKind,
    )
    from cti_app.domain.production_extraction import (
        EXTRACTION_PROFILE_POLICY_VERSION,
        ExtractionFactV1,
        ExtractionReuseState,
        ProductionExtractionV1,
        ProductionSourceExtractionV1,
    )
    from cti_app.domain.production_references import (
        ProductionReferenceKind,
        ProductionReferenceTier,
    )
    from cti_app.domain.production_synthesis import (
        PRODUCTION_SYNTHESIS_SCHEMA_VERSION,
        SYNTHESIS_POLICY_VERSION,
        ProductionSynthesisV1,
        SynthesisParagraphV1,
        extraction_evidence_refs_v1,
    )

    subject_id, source_document_id, anchor_id = uuid4(), uuid4(), uuid4()
    extraction = ProductionExtractionV1(
        schema_version=1,
        subject_id=subject_id,
        production_input_hash="a" * 64,
        references_corpus_hash="b" * 64,
        profile_policy_version=EXTRACTION_PROFILE_POLICY_VERSION,
        sources=(
            ProductionSourceExtractionV1(
                source_document_id=source_document_id,
                canonical_url="https://example.test/synthetic-report",
                content_sha256="c" * 64,
                tier=ProductionReferenceTier.CORE,
                kind=ProductionReferenceKind.PUBLICATION,
                role=SourceRole.PRIMARY,
                profile=ExtractionProfile.FULL,
                checkpoint_id=None,
                reuse_state=ExtractionReuseState.FRESH,
                facts=(
                    ExtractionFactV1(
                        category="other_technical",
                        value="Synthetic finding",
                        attack_id=None,
                        context="Synthetic test context.",
                        evidence_quote="The synthetic report records this finding.",
                        evidence_basis=ProductionEvidenceBasis.SOURCE_VERIFIED,
                        source_document_ids=(source_document_id,),
                    ),
                ),
                events=(),
                indicators=(),
                rules=(),
                uncertainties=(),
            ),
        ),
        omitted_sources=(),
        warnings=(),
    )
    evidence_ref = extraction_evidence_refs_v1(extraction)[0]
    synthesis = ProductionSynthesisV1(
        schema_version=PRODUCTION_SYNTHESIS_SCHEMA_VERSION,
        subject_id=subject_id,
        production_input_hash=extraction.production_input_hash,
        extraction_hash=canonical_extraction_hash(extraction),
        publication_language="fr",
        synthesis_policy_version=SYNTHESIS_POLICY_VERSION,
        title="Synthetic report",
        lead=(SynthesisParagraphV1("Synthetic lead claim.", (evidence_ref,)),),
        sections=(),
        timeline=(),
        uncertainties=(),
        warnings=(),
    )
    evidence_pack = EditorialEnrichmentEvidencePackV1(
        publication_language="fr",
        current_synthesis={"lead": [{"block_id": str(anchor_id)}], "sections": []},
        narrative_evidence=(),
        technical_evidence=(),
        reserve_evidence=(),
        source_pair_relations=(),
        registered_anchor_ids=frozenset({anchor_id}),
        _handle_to_ref={"E001": evidence_ref},
    )
    anchor_entry = BlockRegistryEntry(
        block_id=anchor_id,
        kind=EditorialBlockKind.PARAGRAPH,
        parent_block_id=None,
        position=0,
        content_sha256="d" * 64,
        source_stage=EditorialSourceStage.SYNTHESIS,
        structural_pointer="/lead/0",
    )
    target_path = revision_target_path(anchor_entry)
    rows = [
        {
            "cells": ["Synthetic finding", "Synthetic test context."],
            "evidence_handles": ["E001"],
        },
        {
            "cells": ["Synthetic finding", "The synthetic report records this finding."],
            "evidence_handles": ["E001"],
        },
    ][:row_count]
    table_payload = {
        "key": "table_inserted_after_registered_paragraph",
        "kind": "custom",
        "title": "Inserted synthetic findings",
        "caption": "Synthetic findings placed after the selected paragraph.",
        "columns": [
            {"key": "finding", "label": "Finding"},
            {"key": "source", "label": "Source detail"},
        ],
        "rows": rows,
        "placement": {
            "kind": "after_paragraph",
            "section_index": None,
            "anchor_block_id": str(anchor_id),
        },
        "purpose": {
            "question": "Which source passage supports this finding?",
            "available_data": "The cited synthetic finding and its source context.",
            "comprehension_gain": "Compare source context beside the finding.",
            "scope": "Only the synthetic finding and its cited source context.",
            "evidence_handles": ["E001"],
            "knowledge_limits": "No other source detail is represented.",
            "placement_reason": "Keep the table after its anchor paragraph.",
        },
    }
    if invalid_text is not None:
        if invalid_field == "cell":
            rows[0]["cells"][0] = invalid_text
        elif invalid_field in {"key", "title", "caption"}:
            table_payload[invalid_field] = invalid_text
        elif invalid_field in {"column_key", "column_label"}:
            column_field = "key" if invalid_field == "column_key" else "label"
            table_payload["columns"][0][column_field] = invalid_text
        elif invalid_field.startswith("purpose_"):
            table_payload["purpose"][invalid_field.removeprefix("purpose_")] = invalid_text
        else:
            raise AssertionError(f"Unhandled test field: {invalid_field}")
    patch_after = json.dumps(
        {
            "tables": [table_payload],
            "charts": [],
            "diagrams": [],
            "annotations": [],
            "figures": [],
            "resource_needs": [],
        },
        ensure_ascii=True,
        separators=(",", ":"),
    )
    assert _editorial_patch_text_violation(patch_after)
    target = TargetedEditorialRevisionTarget(
        kind=TargetedEditorialRevisionTargetKind.ENRICHMENT_ELEMENT,
        element_kind=EditorialEnrichmentElementKind.TABLE,
        placement=TargetedEditorialRevisionPlacement(
            kind=TargetedEditorialRevisionPlacementKind.AFTER_PARAGRAPH,
            anchor_block_id=anchor_id,
        ),
    )
    before_hash = "e" * 64
    raw_response = "\n".join(
        (
            "@@ TARGET @@",
            target_path,
            "@@ ACTION @@",
            TargetedEditorialRevisionOperation.CREATE_TABLE.value,
            "@@ BASE_ARTIFACT_HASH @@",
            "f" * 64,
            "@@ BEFORE_HASH @@",
            before_hash,
            "@@ AFTER @@",
            patch_after,
            "@@ RATIONALE @@",
            "Modification manuelle validée par l’analyste.",  # noqa: RUF001
            "@@ END @@",
        )
    )
    preparation = EditorialProposalPreparation(
        subject_id=subject_id,
        production_run_id=uuid4(),
        snapshot_id=uuid4(),
        occurrence_id=anchor_id,
        thread_id=uuid4() if origin_value == "bridge_draft" else None,
        base_artifact_id=uuid4(),
        base_artifact_version=1,
        base_input_hash=extraction.production_input_hash,
        base_canonical_sha256="f" * 64,
        base_pin_hash="1" * 64,
        routing_decision_hash=None,
        target_id="registered-table-target",
        target=target,
        operation=TargetedEditorialRevisionOperation.CREATE_TABLE,
        before_hash=before_hash,
        base_content={"tables": []},
        raw_response=raw_response,
        model_run_id=uuid4() if origin_value == "bridge_draft" else None,
        model_turn_id=uuid4() if origin_value == "bridge_draft" else None,
        actor_id=uuid4(),
        idempotency_key="manual-table-unit-wire-0001",
        request_sha256="2" * 64,
        enrichment_context={
            "target_path": target_path,
            "evidence_pack": evidence_pack,
            "extraction": extraction,
            "synthesis": synthesis,
            "figure_catalog": (),
        },
        strict_wire=True,
        origin=EditorialProposalOrigin(origin_value),
    )
    service = EditorialProposalService(uow_factory=object())

    patch_payload, state, event_kind, diagnostics = await service._validate_result(preparation)

    assert state.value == expected_state
    if expected_state == "proposed":
        assert event_kind is EditorialProposalEventKind.PREPARED
        assert patch_payload is not None
        table_payload = patch_payload["enrichment_patch"]["tables"][0]
        assert table_payload["title"] == "Inserted synthetic findings"
        assert table_payload["caption"] == (
            "Synthetic findings placed after the selected paragraph."
        )
        assert [row["cells"] for row in table_payload["rows"]] == [
            ["Synthetic finding", "Synthetic test context."],
            ["Synthetic finding", "The synthetic report records this finding."],
        ]
        assert table_payload["placement"]["anchor_block_id"] == str(anchor_id)
        expected_ref = {
            "source_document_id": str(evidence_ref.source_document_id),
            "kind": evidence_ref.kind.value,
            "evidence_key": evidence_ref.evidence_key,
        }
        assert patch_payload["evidence_refs"] == [expected_ref, expected_ref, expected_ref]
        assert diagnostics == {}
    else:
        assert state is EditorialProposalState.NEEDS_REVIEW
        assert event_kind is EditorialProposalEventKind.NEEDS_REVIEW
        assert expected_validator_code is not None
        if origin_value == "bridge_draft":
            if expected_validator_code == "revision_wire_markup_forbidden":
                assert diagnostics["code"] == expected_validator_code
            else:
                assert diagnostics
        elif expected_validator_code != "revision_wire_markup_forbidden":
            assert diagnostics["validator_code"] == expected_validator_code
            assert diagnostics["reason"] == "manual_table:validator_rejected"
            rejected = _manual_table_failure_from_diagnostics(
                preparation,
                "manual_edit_patch_invalid",
                diagnostics,
            )
            assert rejected.validator_code == expected_validator_code
            assert editorial_workspace_api._manual_table_validator_log_code(rejected) == (
                expected_validator_code
            )
        else:
            assert diagnostics["code"] == expected_validator_code
            assert diagnostics["validator_code"] == expected_validator_code


@pytest.mark.asyncio
async def test_manual_table_patch_rejects_duplicate_normalized_analytic_question() -> None:
    from cti_app.application.editorial_proposals import EditorialProposalService
    from cti_app.domain.editorial_proposal import ManualPatch, ManualTablePatch
    from cti_app.domain.production_synthesis import EvidenceKind, ExtractionEvidenceRefV1

    anchor = uuid4()
    reference = ExtractionEvidenceRefV1(
        uuid4(), EvidenceKind.FACT, hashlib.sha256(b"admitted-table-evidence").hexdigest()
    )
    table = _table_spec(anchor, (reference,))
    snapshot_id = uuid4()
    base_content = {"tables": [{"purpose": {"question": "WHICH synthetic tools are documented!"}}]}

    class ProposalRepository:
        async def get_by_idempotency_key(self, *_args):
            return None

    class UnitOfWork:
        editorial_proposals = ProposalRepository()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

    class ContextBuilder:
        async def build_manual(self, *_args):
            return SimpleNamespace(
                proposal_snapshot_id=snapshot_id,
                proposal_operations=("create_table",),
                proposal_enrichment_context={
                    "anchor_block_id": str(anchor),
                    "base_artifact_id": str(record.enrichment_artifact_id),
                    "base_artifact_version": record.enrichment_version,
                    "base_input_hash": record.enrichment_input_hash,
                    "base_canonical_sha256": record.enrichment_canonical_sha256,
                    "evidence_pack": SimpleNamespace(_handle_to_ref={"ev-1": reference}),
                    "base_content": base_content,
                },
            )

    record = SimpleNamespace(
        production_run_id=uuid4(),
        workspace_pin_hash="3" * 64,
        routing_decision_hash="4" * 64,
        synthesis_artifact_id=uuid4(),
        synthesis_version=1,
        synthesis_canonical_sha256="5" * 64,
        enrichment_artifact_id=uuid4(),
        enrichment_version=1,
        enrichment_input_hash="6" * 64,
        enrichment_canonical_sha256="7" * 64,
    )
    workspace_service = EditorialWorkspaceService(
        uow_factory=UnitOfWork,
        artifact_store=object(),
    )

    service = EditorialProposalService(
        uow_factory=UnitOfWork,
        blob_store=object(),
        artifact_store=object(),
        workspace_service=workspace_service,
        discussion_context_builder=ContextBuilder(),
    )

    async def should_not_prepare(*_args, **_kwargs):
        pytest.fail("A colliding analytic question must be rejected before proposal storage")

    service._prepare_result = should_not_prepare

    with pytest.raises(EditorialProposalApplicationError) as caught:
        await service.prepare_manual_patch(
            subject_id=uuid4(),
            snapshot_id=snapshot_id,
            occurrence_id=anchor,
            actor_id=uuid4(),
            idempotency_key="manual-table-question-0001",
            patch=ManualPatch(typed_patch=ManualTablePatch(anchor_block_id=anchor, table=table)),
        )

    assert caught.value.code == "manual_table_analytic_question_duplicate"
    assert caught.value.reason == "manual_table:duplicate_question"
    assert caught.value.frame is not None
    assert caught.value.frame.startswith("editorial_proposals.py:")
    assert caught.value.frame.endswith(":_prepare_manual_table_patch")


@pytest.mark.asyncio
async def test_manual_table_patch_uses_registered_path_from_real_workspace_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from typing import cast

    from cti_app.application.editorial_proposals import (
        EditorialProposalPreparation,
        EditorialProposalService,
    )
    from cti_app.domain.editorial_proposal import (
        EditorialProposal,
        ManualPatch,
        ManualTablePatch,
    )
    from cti_app.domain.production_synthesis import EvidenceKind, ExtractionEvidenceRefV1

    anchor_entry, synthesis = _paragraph_registry_entry()
    subject_id, production_run_id, snapshot_id = uuid4(), uuid4(), uuid4()
    enrichment_artifact = ProductionArtifact(
        production_run_id=production_run_id,
        subject_id=subject_id,
        stage=ProductionArtifactStage.EDITORIAL_ENRICHMENT,
        version=1,
        input_hash="6" * 64,
        status=ProductionArtifactStatus.VERIFIED,
        canonical_blob_id=uuid4(),
    )
    record = _workspace_record(
        subject_id=subject_id,
        production_run_id=production_run_id,
        synthesis_artifact_id=uuid4(),
        synthesis_sha256=canonical_content_sha256(synthesis),
        entries=(anchor_entry,),
        enrichment_artifact=enrichment_artifact,
        enrichment_sha256="7" * 64,
    )
    record = replace(
        record,
        snapshot_id=snapshot_id,
        publication_kind=PublicationKind.ARTICLE,
    )

    class ProposalRepository:
        async def get_by_idempotency_key(self, *_args):
            return None

    class UnitOfWork:
        editorial_proposals = ProposalRepository()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

    reference = ExtractionEvidenceRefV1(
        uuid4(), EvidenceKind.FACT, hashlib.sha256(b"registered table evidence").hexdigest()
    )
    table = _table_spec(anchor_entry.block_id, (reference,))
    enrichment_context = {
        "anchor_block_id": str(anchor_entry.block_id),
        "target_path": revision_target_path(anchor_entry),
        "base_artifact_id": str(enrichment_artifact.id),
        "base_artifact_version": enrichment_artifact.version,
        "base_input_hash": enrichment_artifact.input_hash,
        "base_canonical_sha256": "7" * 64,
        "evidence_pack": SimpleNamespace(_handle_to_ref={"ev-1": reference}),
        "base_content": {"tables": []},
    }

    class ContextBuilder:
        async def build_manual(self, *_args):
            return SimpleNamespace(
                proposal_snapshot_id=snapshot_id,
                proposal_operations=("create_table",),
                proposal_enrichment_context=enrichment_context,
            )

    workspace_service = EditorialWorkspaceService(
        uow_factory=UnitOfWork,
        artifact_store=object(),
    )

    async def load_current(*_args, **_kwargs):
        return {}, record, synthesis

    monkeypatch.setattr(workspace_service, "_load_current", load_current)
    resolutions: list[EditorialRevisionTargetResolution] = []
    resolve_revision_target = workspace_service.resolve_revision_target

    async def record_resolution(**kwargs):
        resolved = await resolve_revision_target(**kwargs)
        resolutions.append(resolved)
        return resolved

    monkeypatch.setattr(workspace_service, "resolve_revision_target", record_resolution)
    service = EditorialProposalService(
        uow_factory=UnitOfWork,
        blob_store=object(),
        artifact_store=object(),
        workspace_service=workspace_service,
        discussion_context_builder=ContextBuilder(),
    )
    preparations: list[EditorialProposalPreparation] = []

    async def capture_preparation(
        preparation: EditorialProposalPreparation, *, reject_invalid_manual: bool
    ) -> EditorialProposal:
        assert reject_invalid_manual
        preparations.append(preparation)
        return cast(EditorialProposal, preparation)

    monkeypatch.setattr(service, "_prepare_result", capture_preparation)

    await service.prepare_manual_patch(
        subject_id=subject_id,
        snapshot_id=snapshot_id,
        occurrence_id=anchor_entry.block_id,
        actor_id=uuid4(),
        idempotency_key="manual-table-registered-path-0001",
        patch=ManualPatch(
            typed_patch=ManualTablePatch(anchor_block_id=anchor_entry.block_id, table=table)
        ),
    )

    assert len(resolutions) == 1
    assert isinstance(resolutions[0], EditorialRevisionTargetResolution)
    assert resolutions[0].occurrence.entry is anchor_entry
    assert len(preparations) == 1
    assert f"@@ TARGET @@\n{revision_target_path(anchor_entry)}\n" in preparations[0].raw_response
    assert preparations[0].target == resolutions[0].target


@pytest.mark.asyncio
async def test_manual_table_route_returns_stable_question_collision_error(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    subject_id, anchor = uuid4(), uuid4()

    class IdentityProvider:
        async def current(self):
            return SimpleNamespace(actor_id="dev-analyst")

    class ProposalService:
        async def prepare_manual_patch(self, **_kwargs):
            raise EditorialProposalApplicationError("manual_table_analytic_question_duplicate")

    monkeypatch.setattr(editorial_workspace_api, "_proposal_impact_consumer_wired", lambda: True)
    monkeypatch.setattr(
        editorial_workspace_api, "_manual_proposal_service", lambda _request: ProposalService()
    )
    app = FastAPI()
    app.state.identity_provider = IdentityProvider()
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/table-proposals",
            "headers": [],
            "query_string": b"",
            "server": ("editorial.test", 80),
            "client": ("test", 1),
            "app": app,
        }
    )
    evidence = editorial_workspace_api.EditorialTableEvidenceReferenceRequest(
        source_document_id=uuid4(),
        kind="fact",
        evidence_key=hashlib.sha256(b"admitted-table-evidence").hexdigest(),
    )
    payload = editorial_workspace_api.EditorialManualTableProposalRequest(
        snapshot_id=uuid4(),
        anchor_block_id=anchor,
        table=editorial_workspace_api.EditorialTableDraftRequest(
            key="manual-question-collision",
            kind="custom",
            title="Synthetic table",
            columns=[
                editorial_workspace_api.EditorialTableColumnRequest(key="finding", label="Finding"),
                editorial_workspace_api.EditorialTableColumnRequest(key="source", label="Source"),
            ],
            rows=[
                editorial_workspace_api.EditorialTableRowRequest(
                    cells=["Synthetic finding", "Synthetic source"], evidence_refs=[evidence]
                )
            ],
            purpose=editorial_workspace_api.EditorialTablePurposeRequest(
                question="Which source supports the finding?",
                available_data="Synthetic source details.",
                comprehension_gain="Compare the source details.",
                scope="The synthetic source.",
                evidence_refs=[evidence],
                knowledge_limits="No other source is represented.",
                placement_reason="After the anchor paragraph.",
            ),
        ),
    )

    with pytest.raises(HTTPException) as caught:
        await editorial_workspace_api.prepare_manual_editorial_table_proposal(
            subject_id,
            anchor,
            request,
            payload,
            "manual-table-question-0002",
        )

    assert caught.value.status_code == 422
    assert caught.value.detail["code"] == "manual_table_analytic_question_duplicate"
    assert caught.value.detail["message"] == (
        "La question analytique existe déjà dans l'enrichissement."
    )
    assert "manual_table_proposal_rejected code=manual_table_analytic_question_duplicate" in (
        caplog.text
    )
    assert "reason=manual_table:validator_rejected" in caplog.text
    assert "validator_code=none" in caplog.text
    assert "frame=test_editorial_table_insertion.py:" in caplog.text


@pytest.mark.asyncio
async def test_manual_table_route_returns_stable_row_width_validation_error(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(
        editorial_workspace_api,
        "get_settings",
        lambda: SimpleNamespace(editorial_proposal_accept_enabled=True),
    )
    app = FastAPI()
    app.include_router(editorial_workspace_api.router)
    subject_id, anchor_id = uuid4(), uuid4()
    evidence_ref = {
        "source_document_id": str(uuid4()),
        "kind": "fact",
        "evidence_key": hashlib.sha256(b"request-validation-evidence").hexdigest(),
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://editorial.test"
    ) as client:
        response = await client.post(
            f"/api/subjects/{subject_id}/editor/blocks/{anchor_id}/table-proposals",
            headers={"Idempotency-Key": "manual-table-invalid-width-0001"},
            json={
                "snapshot_id": str(uuid4()),
                "anchor_block_id": str(anchor_id),
                "table": {
                    "key": "invalid_width_table",
                    "kind": "custom",
                    "title": "Synthetic table",
                    "columns": [
                        {"key": "finding", "label": "Finding"},
                        {"key": "source", "label": "Source"},
                    ],
                    "rows": [
                        {
                            "cells": ["one", "two", "three"],
                            "evidence_refs": [evidence_ref],
                        }
                    ],
                    "purpose": {
                        "question": "Which synthetic source supports this finding?",
                        "available_data": "One synthetic source record.",
                        "comprehension_gain": "Compare both synthetic values.",
                        "scope": "This one synthetic row.",
                        "evidence_refs": [evidence_ref],
                        "knowledge_limits": "No other values are included.",
                        "placement_reason": "After the selected anchor paragraph.",
                    },
                },
            },
        )

    assert response.status_code == 422
    assert response.json()["detail"] == {
        "code": "manual_table_request_invalid",
        "message": "La requête de tableau est invalide.",
    }
    assert "manual_table_proposal_rejected code=manual_table_request_invalid" in caplog.text
    assert "reason=manual_table:row_width_mismatch" in caplog.text
    assert "frame=editorial_workspace.py:" in caplog.text
