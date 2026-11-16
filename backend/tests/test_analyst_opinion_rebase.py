from __future__ import annotations

import hashlib
from dataclasses import replace
from uuid import UUID, uuid4

from cti_app.domain.analyst_opinion import (
    AnalystObservationRelation,
    AnalystObservationState,
    AnalystObservationV1,
    AnalystOpinionStatus,
    AnalystOpinionV1,
    BridgeDraftReferenceV1,
    BridgeReviewReferenceV1,
    ImplantMaterialEditorialV1,
    ImplantMaterialType,
    OpinionBlock,
    OpinionBlockType,
    OpinionClaimQualification,
    OpinionProtocolFigureV1,
    opinion_blocks_with_implant_materials,
    opinion_blocks_with_observations,
    validate_analyst_opinion_rebase,
    verify_opinion_citations,
)
from cti_app.domain.protocol_model import ProtocolDiagramKind


def test_rebase_preserves_notes_materials_figures_citations_and_bridge_hashes() -> None:
    finding_id = UUID(int=101)
    evidence_id = UUID(int=102)
    base_text = "Bridge-authored finding."
    base_text_sha = hashlib.sha256(base_text.encode("utf-8")).hexdigest()
    source_text_sha = hashlib.sha256(b"original Bridge source").hexdigest()
    block_draft_ref = BridgeDraftReferenceV1(
        str(uuid4()), base_text_sha, source_text_sha
    )
    block_review_ref = BridgeReviewReferenceV1(
        str(uuid4()), hashlib.sha256(b"review response").hexdigest(), base_text_sha
    )
    body = OpinionBlock(
        block_type=OpinionBlockType.KEY_FINDINGS,
        text=base_text,
        finding_refs=(finding_id,),
        evidence_refs=(evidence_id,),
        qualification=OpinionClaimQualification.INFERENCE,
        certainty=0.8,
        bridge_draft_ref=block_draft_ref,
        bridge_review_ref=block_review_ref,
    )
    note_text = "Bridge-authored analyst note."
    note_text_sha = hashlib.sha256(note_text.encode("utf-8")).hexdigest()
    note_source_sha = hashlib.sha256(b"original Bridge note source").hexdigest()
    note = AnalystObservationV1(
        id=UUID(int=103),
        anchor="section:0:paragraph:0001",
        text=note_text,
        state=AnalystObservationState.INFERRED,
        relation=AnalystObservationRelation.QUALIFIES,
        finding_refs=(finding_id,),
        evidence_refs=(evidence_id,),
        bridge_block_type=OpinionBlockType.KEY_FINDINGS,
        bridge_draft_ref=BridgeDraftReferenceV1(
            str(uuid4()), note_text_sha, note_source_sha, UUID(int=104)
        ),
        bridge_review_ref=BridgeReviewReferenceV1(
            str(uuid4()), hashlib.sha256(b"note review response").hexdigest(), note_text_sha
        ),
    )
    materials_section = OpinionBlock(
        block_type=OpinionBlockType.IMPLANT_MATERIALS,
        text="Bridge-authored materials context.",
        bridge_draft_ref=BridgeDraftReferenceV1(
            str(uuid4()),
            hashlib.sha256(b"Bridge-authored materials context.").hexdigest(),
            hashlib.sha256(b"original materials source").hexdigest(),
        ),
        bridge_review_ref=BridgeReviewReferenceV1(
            str(uuid4()),
            hashlib.sha256(b"materials review response").hexdigest(),
            hashlib.sha256(b"Bridge-authored materials context.").hexdigest(),
        ),
    )
    materials = (
        ImplantMaterialEditorialV1(
            type=ImplantMaterialType.AES_KEY,
            value="0011223344556677",
            role="fixture key",
            sample_location="config/key",
            sample_sha256="a" * 64,
            evidence_observation_id=evidence_id,
        ),
    )
    blocks = opinion_blocks_with_observations((body, materials_section), (note,))
    blocks = opinion_blocks_with_implant_materials(
        blocks, materials, section=materials_section
    )
    figures = tuple(
        OpinionProtocolFigureV1(
            kind=kind,
            model_sha256="b" * 64,
            spec_sha256="c" * 64,
            asset_id=UUID(int=110 + index),
            asset_sha256="d" * 64,
            preview_sha256="e" * 64,
            pin_sha256="f" * 64,
            compiler_name="d2",
            compiler_version="1.0.0",
            compiler_policy_version="fixture-policy-v1",
            caption=f"Fixture {kind.value}.",
        )
        for index, kind in enumerate(ProtocolDiagramKind)
    )
    approved = AnalystOpinionV1(
        id=UUID(int=120),
        version=4,
        investigation_id=UUID(int=121),
        synthesis_artifact_id=UUID(int=122),
        synthesis_sha256="1" * 64,
        base_publication_artifact_id=UUID(int=123),
        base_publication_sha256="2" * 64,
        blocks=blocks,
        unresolved_points=("Review the changed publication paragraph.",),
        protocol_figures=figures,
        author_id="analyst:author",
        reviewer_id="editor:approver",
        status=AnalystOpinionStatus.APPROVED,
        bridge_drafting_gap_acknowledged=True,
    )
    rebased = replace(
        approved,
        version=approved.version + 1,
        base_publication_artifact_id=UUID(int=124),
        base_publication_sha256="3" * 64,
        author_id="editor:rebaser",
        reviewer_id=None,
        status=AnalystOpinionStatus.DRAFT,
        content_sha256="",
    )

    validate_analyst_opinion_rebase(approved, rebased)

    assert rebased.blocks == approved.blocks
    assert rebased.observations == approved.observations
    assert rebased.implant_materials == approved.implant_materials
    assert rebased.implant_materials_section == approved.implant_materials_section
    assert rebased.protocol_figures == approved.protocol_figures
    assert rebased.unresolved_points == approved.unresolved_points
    assert rebased.bridge_drafting_gap_acknowledged is True
    assert rebased.content_sha256 != approved.content_sha256
    assert verify_opinion_citations(rebased, (), ()) == verify_opinion_citations(approved, (), ())
    assert rebased.blocks[0].bridge_draft_ref == block_draft_ref
    assert rebased.blocks[0].bridge_review_ref == block_review_ref
    assert rebased.observations[0].bridge_draft_ref.source_text_sha256 == note_source_sha
    assert (
        rebased.implant_materials_section.bridge_draft_ref.source_text_sha256
        == materials_section.bridge_draft_ref.source_text_sha256
    )
