from __future__ import annotations

import hashlib
from types import SimpleNamespace
from uuid import uuid4

import pytest

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
from cti_app.application.editorial_workspace.registry import allowed_operations
from cti_app.domain.editorial_workspace import (
    BlockRegistryEntry,
    EditorialBlockKind,
    EditorialSourceStage,
    canonical_content_sha256,
)
from cti_app.domain.production_editorial_revision import (
    TargetedEditorialRevisionPlacement,
    TargetedEditorialRevisionPlacementKind,
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
    assert "create_table" in allowed_operations(entry, synthesis, allow_table_insertion=True)


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
