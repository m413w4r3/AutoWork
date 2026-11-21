from __future__ import annotations

import hashlib
from dataclasses import replace
from types import SimpleNamespace
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
from cti_app.application.editorial_workspace.registry import (
    WorkspaceSnapshotRecord,
    allowed_operations,
    project_blocks,
    table_insertion_preparation_ready,
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
from cti_app.domain.production_editorial_enrichment import EditorialEnrichmentElementKind
from cti_app.domain.production_editorial_revision import (
    TargetedEditorialRevisionPlacement,
    TargetedEditorialRevisionPlacementKind,
    TargetedEditorialRevisionTarget,
    TargetedEditorialRevisionTargetKind,
)


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


def test_table_patch_keeps_anchor_through_adoption() -> None:
    from cti_app.domain.production import ProductionArtifactStage
    from cti_app.domain.production_editorial_enrichment import EditorialEnrichmentElementKind

    anchor = uuid4()
    synthesis_artifact_id = uuid4()
    enrichment_artifact_id = uuid4()
    subject_id = uuid4()
    run_id = uuid4()
    snapshot_id = uuid4()
    entry = BlockRegistryEntry(
        block_id=anchor,
        kind=EditorialBlockKind.PARAGRAPH,
        parent_block_id=None,
        position=2,
        content_sha256="a" * 64,
        source_stage=EditorialSourceStage.SYNTHESIS,
        structural_pointer="/sections/0/paragraphs/0",
    )
    record = SimpleNamespace(
        subject_id=subject_id,
        production_run_id=run_id,
        snapshot_id=snapshot_id,
        workspace_pin_hash="b" * 64,
        routing_decision_hash="c" * 64,
        synthesis_artifact_id=synthesis_artifact_id,
        synthesis_version=3,
        synthesis_input_hash="d" * 64,
        synthesis_canonical_sha256="e" * 64,
        enrichment_artifact_id=enrichment_artifact_id,
        enrichment_version=5,
        enrichment_input_hash="f" * 64,
        enrichment_canonical_sha256="1" * 64,
    )
    proposal = SimpleNamespace(
        operation="create_table",
        occurrence_id=anchor,
        subject_id=subject_id,
        production_run_id=run_id,
        snapshot_id=snapshot_id,
        base_pin_hash=record.workspace_pin_hash,
        base_routing_hash=record.routing_decision_hash,
        base_artifact_id=enrichment_artifact_id,
        base_artifact_version=record.enrichment_version,
        base_input_hash=record.enrichment_input_hash,
        base_canonical_sha256=record.enrichment_canonical_sha256,
    )
    resolution = SimpleNamespace(
        entry=entry,
        binding_state="valid",
        record=record,
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

    patch["target"]["placement"]["anchor_block_id"] = str(uuid4())
    with pytest.raises(EditorialProposalAdoptionError, match="proposal_target_mismatch"):
        _proposal_stage(proposal, patch, resolution)


def test_manual_table_text_rejects_markup() -> None:
    assert _editorial_patch_text_violation("<b>forged cell</b>")
    assert _editorial_patch_text_violation('#raw("forged Typst")')


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
    entry = BlockRegistryEntry(
        block_id=anchor,
        kind=EditorialBlockKind.PARAGRAPH,
        parent_block_id=None,
        position=0,
        content_sha256="8" * 64,
        source_stage=EditorialSourceStage.SYNTHESIS,
        structural_pointer="/sections/0/paragraphs/0",
    )
    target = TargetedEditorialRevisionTarget(
        kind=TargetedEditorialRevisionTargetKind.ENRICHMENT_ELEMENT,
        element_kind=EditorialEnrichmentElementKind.TABLE,
        element_key=None,
        placement=TargetedEditorialRevisionPlacement(
            kind=TargetedEditorialRevisionPlacementKind.AFTER_PARAGRAPH,
            anchor_block_id=anchor,
        ),
    )

    class WorkspaceService:
        async def resolve_revision_target(self, **_kwargs):
            return SimpleNamespace(
                record=record,
                occurrence=SimpleNamespace(entry=entry, binding_state="valid"),
                target=target,
                target_id="target-id",
                before_hash=entry.content_sha256,
            )

    service = EditorialProposalService(
        uow_factory=UnitOfWork,
        blob_store=object(),
        artifact_store=object(),
        workspace_service=WorkspaceService(),
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
