from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest

from cti_app.application.opinion_note_proposals import (
    NoteContentBlockContextV1,
    note_article_element_facts,
    validate_note_content_blocks,
)
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.application.publication_augmentation import (
    PublicationAugmentationArtifactService,
    PublicationAugmentationError,
    PublicationAugmentationErrorCode,
    augment_publication_with_opinion,
)
from cti_app.application.typst_rendering import (
    TypstDraftPreviewDocument,
    project_publication_to_typst_model,
)
from cti_app.domain.analyst_opinion import (
    AnalystObservationRelation,
    AnalystObservationState,
    AnalystObservationV1,
    AnalystOpinionStatus,
    AnalystOpinionV1,
    ArticleElementNoteBlockV1,
    NoteArticleElementKind,
    OpinionBlockType,
    OpinionClaimQualification,
    opinion_blocks_with_observations,
)
from cti_app.domain.production import (
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionArtifactStatus,
)
from cti_app.domain.publication_document import (
    PublicationDocumentV5,
    PublicationDocumentV9,
    PublicationDocumentV10,
    publication_document_from_json,
    publication_element_sha256,
    serialize_publication_document,
)
from tests.test_opinion_note_proposals import (
    LEAD_ANCHOR,
    _after_paragraph_table,
    _v9_publication_with_elements,
)
from tests.test_publication_document_v6 import _approved_opinion, _base_v5


def _reduced_article03_draft_preview(base: PublicationDocumentV5) -> TypstDraftPreviewDocument:
    opinion, _context = _approved_opinion(base)
    block_types = (
        OpinionBlockType.KEY_FINDINGS,
        OpinionBlockType.NETWORK_AND_INFRASTRUCTURE,
        OpinionBlockType.INFECTION_CHAIN_AND_CAPABILITIES,
        OpinionBlockType.PROPOSED_DETECTIONS,
        OpinionBlockType.CLAIM_VERIFICATION,
        OpinionBlockType.UNCERTAINTIES_AND_NEXT_LEADS,
    )
    finding_ref_counts = (10, 12, 11, 2, 8, 8)
    evidence_ref_counts = (10, 11, 12, 7, 8, 8)
    qualifications = (
        OpinionClaimQualification.INFERENCE,
        OpinionClaimQualification.FACT,
        OpinionClaimQualification.INFERENCE,
        OpinionClaimQualification.INFERENCE,
        OpinionClaimQualification.INFERENCE,
        OpinionClaimQualification.HYPOTHESIS,
    )
    blocks = []
    for index, block_type in enumerate(block_types):
        finding_start = 2_000 + sum(finding_ref_counts[:index])
        evidence_start = 3_000 + sum(evidence_ref_counts[:index])
        blocks.append(
            replace(
                opinion.blocks[0],
                block_type=block_type,
                text=f"Anonymized opinion block {index + 1}.",
                finding_refs=tuple(
                    UUID(int=finding_start + ref_index)
                    for ref_index in range(finding_ref_counts[index])
                ),
                evidence_refs=tuple(
                    UUID(int=evidence_start + ref_index)
                    for ref_index in range(evidence_ref_counts[index])
                ),
                qualification=qualifications[index],
                certainty=0.7,
                bridge_authored=False,
                bridge_draft_ref=None,
                bridge_review_ref=None,
            )
        )
    draft = replace(
        opinion,
        status=AnalystOpinionStatus.DRAFT,
        reviewer_id=None,
        blocks=tuple(blocks),
        unresolved_points=tuple(f"Anonymized unresolved point {index + 1}." for index in range(12)),
        content_sha256="",
    )
    return TypstDraftPreviewDocument(base, draft)


def test_augmentation_projects_ordered_blocks_and_keeps_v5_bytes_unchanged() -> None:
    base = _base_v5()
    opinion, context = _approved_opinion(base)
    base_payload = serialize_publication_document(base)
    base_bytes = json.dumps(
        base_payload,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")

    augmented = augment_publication_with_opinion(base, opinion, context)
    model = project_publication_to_typst_model(augmented)

    assert augmented.base_publication is base
    assert serialize_publication_document(base) == base_payload
    base_bytes_after = json.dumps(
        serialize_publication_document(base),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    opinion_section = next(
        section for section in model.content_sections if section["type"] == "analyst_opinion"
    )
    assert opinion_section["blocks"][0]["number"] == "1.1"
    assert opinion_section["blocks"][0]["qualification_label"] == "Fait étayé"
    assert model.content_sections[-1]["type"] == "analyst_opinion"
    assert model.article_format is None
    assert not {
        "chronology",
        "technical_annex",
        "provenance",
    }.intersection(section["type"] for section in model.content_sections)
    assert serialize_publication_document(augmented)["base_publication"] == base_payload
    assert base_bytes_after == base_bytes


def test_opinion_reference_projection_assigns_short_labels_and_preserves_reference_ids() -> None:
    base = _base_v5()
    opinion, context = _approved_opinion(base)

    augmented = augment_publication_with_opinion(base, opinion, context)
    model = project_publication_to_typst_model(augmented)
    opinion_section = next(
        section for section in model.content_sections if section["type"] == "analyst_opinion"
    )
    block = opinion_section["blocks"][0]

    assert [reference["label"] for reference in block["finding_refs"]] == ["[F1]"]
    assert [reference["label"] for reference in block["evidence_refs"]] == ["[E1]"]
    assert block["finding_refs"][0]["reference_id"] == str(context.findings[0].id)
    assert block["evidence_refs"][0]["reference_id"] == str(context.observations[0].id)
    assert [
        reference["note_number"] for reference in block["finding_refs"] + block["evidence_refs"]
    ] == [1, 2]


def test_augmentation_rejects_an_unverifiable_citation() -> None:
    base = _base_v5()
    opinion, context = _approved_opinion(base)
    missing_id = UUID(int=908)
    changed_block = replace(opinion.blocks[0], finding_refs=(missing_id,))
    changed_opinion = replace(opinion, blocks=(changed_block,), content_sha256="")

    with pytest.raises(PublicationAugmentationError) as caught:
        augment_publication_with_opinion(base, changed_opinion, context)

    assert caught.value.code is PublicationAugmentationErrorCode.CITATIONS_INVALID
    assert any(item.reference_id == missing_id for item in caught.value.citation_violations)

    foreign_finding = replace(
        context.findings[0],
        investigation_id=UUID(int=999),
    )
    with pytest.raises(PublicationAugmentationError) as foreign_error:
        augment_publication_with_opinion(
            base,
            opinion,
            replace(context, findings=(foreign_finding,)),
        )
    assert foreign_error.value.code is PublicationAugmentationErrorCode.CITATIONS_INVALID


def test_augmentation_rejects_draft_and_declared_disclosure_violations() -> None:
    base = _base_v5()
    opinion, context = _approved_opinion(base)
    draft = replace(
        opinion,
        status=AnalystOpinionStatus.DRAFT,
        reviewer_id=None,
        content_sha256="",
    )
    with pytest.raises(PublicationAugmentationError) as draft_error:
        augment_publication_with_opinion(base, draft, context)
    assert draft_error.value.code is PublicationAugmentationErrorCode.OPINION_NOT_APPROVED

    with pytest.raises(PublicationAugmentationError) as tlp_error:
        augment_publication_with_opinion(base, opinion, replace(context, tlp_violation=True))
    assert tlp_error.value.code is PublicationAugmentationErrorCode.TLP_VIOLATION

    with pytest.raises(PublicationAugmentationError) as submit_error:
        augment_publication_with_opinion(base, opinion, replace(context, do_not_submit=True))
    assert submit_error.value.code is PublicationAugmentationErrorCode.DO_NOT_SUBMIT


def test_empty_opinions_do_not_create_an_empty_render_section() -> None:
    base = _base_v5()
    opinion, context = _approved_opinion(base)
    empty = replace(
        opinion,
        blocks=(replace(opinion.blocks[0], text=" "),),
        content_sha256="",
    )
    with pytest.raises(PublicationAugmentationError) as caught:
        augment_publication_with_opinion(base, empty, context)
    assert caught.value.code is PublicationAugmentationErrorCode.OPINION_EMPTY

    assert isinstance(base, PublicationDocumentV5)
    v5_model = project_publication_to_typst_model(base)
    assert all(section["type"] != "analyst_opinion" for section in v5_model.content_sections)


def test_reduced_article03_draft_projection_preserves_reference_and_unresolved_counts() -> None:
    preview = _reduced_article03_draft_preview(_base_v5())

    model = project_publication_to_typst_model(preview)
    section = next(item for item in model.content_sections if item["type"] == "analyst_opinion")

    assert preview.analyst_opinion.implant_materials == ()
    assert preview.analyst_opinion.implant_materials_section is None
    assert len(section["blocks"]) == 6
    assert sum(len(item["finding_refs"]) for item in section["blocks"]) == 51
    assert sum(len(item["evidence_refs"]) for item in section["blocks"]) == 56
    assert len(section["unresolved_points"]) == 12
    assert len(section["unresolved_point_render_data"]) == 12
    assert all(item["text"].startswith("Anonymized opinion block") for item in section["blocks"])


class _ArtifactRepository:
    def __init__(self, artifacts: tuple[ProductionArtifact, ...]) -> None:
        self.items = list(artifacts)

    async def get_current_for_revision(self, run_id: UUID, stage: str) -> ProductionArtifact | None:
        matches = [
            item
            for item in self.items
            if item.production_run_id == run_id
            and item.stage.value == stage
            and item.status is not ProductionArtifactStatus.STALE
        ]
        return max(matches, key=lambda item: item.version) if matches else None

    async def list_for_run(self, run_id: UUID) -> list[ProductionArtifact]:
        return [item for item in self.items if item.production_run_id == run_id]

    async def get(self, artifact_id: UUID) -> ProductionArtifact | None:
        return next((item for item in self.items if item.id == artifact_id), None)

    async def append(self, artifact: ProductionArtifact) -> None:
        self.items.append(artifact)


class _InvestigationRepository:
    def __init__(self, investigation: object) -> None:
        self.investigation = investigation

    async def get(self, investigation_id: UUID) -> object:
        del investigation_id
        return self.investigation


class _AugmentationUow:
    def __init__(self, artifacts: _ArtifactRepository, investigation: object) -> None:
        self.production_artifacts = artifacts
        self.analyst_investigations = _InvestigationRepository(investigation)
        self.committed = False

    async def __aenter__(self) -> _AugmentationUow:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def commit(self) -> None:
        self.committed = True


class _AugmentationStore:
    def __init__(self, blobs: dict[UUID, bytes]) -> None:
        self.blobs = blobs

    async def read_bytes(self, blob_id: UUID) -> bytes:
        return self.blobs[blob_id]

    async def store_stage_payloads(
        self, *, canonical: dict[str, object]
    ) -> tuple[None, UUID, None]:
        blob_id = UUID(int=len(self.blobs) + 1000)
        self.blobs[blob_id] = ProductionArtifactStore.canonical_json_bytes(canonical)
        return None, blob_id, None


class _PreviewRenderer:
    def __init__(self) -> None:
        self.artifact_ids: list[UUID] = []

    async def render_preview(self, publication_artifact_id: UUID) -> None:
        self.artifact_ids.append(publication_artifact_id)


@pytest.mark.asyncio
async def test_artifact_service_appends_a_linked_version_without_changing_the_v5_blob() -> None:
    base = _base_v5()
    opinion, context = _approved_opinion(base)
    synthesis_bytes = b"verified canonical synthesis"
    opinion = replace(
        opinion,
        synthesis_sha256=hashlib.sha256(synthesis_bytes).hexdigest(),
        bridge_drafting_gap_acknowledged=True,
        content_sha256="",
    )
    run_id = UUID(int=911)
    base_blob_id = UUID(int=912)
    synthesis_blob_id = UUID(int=913)
    base_payload = serialize_publication_document(base)
    base_bytes = ProductionArtifactStore.canonical_json_bytes(base_payload)
    assert hashlib.sha256(base_bytes).hexdigest() == context.base_publication_sha256
    blobs = {base_blob_id: base_bytes, synthesis_blob_id: synthesis_bytes}
    base_artifact = ProductionArtifact(
        id=context.base_publication_artifact_id,
        production_run_id=run_id,
        subject_id=base.subject_id,
        stage=ProductionArtifactStage.PUBLICATION,
        version=4,
        input_hash="c" * 64,
        canonical_blob_id=base_blob_id,
    )
    synthesis_artifact = ProductionArtifact(
        id=opinion.synthesis_artifact_id,
        production_run_id=run_id,
        subject_id=base.subject_id,
        stage=ProductionArtifactStage.SYNTHESIS,
        version=3,
        input_hash="d" * 64,
        canonical_blob_id=synthesis_blob_id,
    )
    enrichment_artifact = ProductionArtifact(
        production_run_id=run_id,
        subject_id=base.subject_id,
        stage=ProductionArtifactStage.EDITORIAL_ENRICHMENT,
        version=2,
        input_hash="e" * 64,
        canonical_blob_id=UUID(int=914),
    )
    repository = _ArtifactRepository((base_artifact, synthesis_artifact, enrichment_artifact))
    editorial_identity_before = {
        stage: tuple(
            (item.id, item.version, item.input_hash)
            for item in repository.items
            if item.stage is stage
        )
        for stage in (
            ProductionArtifactStage.SYNTHESIS,
            ProductionArtifactStage.EDITORIAL_ENRICHMENT,
        )
    }
    investigation = SimpleNamespace(
        production_run_id=run_id,
        subject_id=base.subject_id,
        synthesis_artifact_id=opinion.synthesis_artifact_id,
    )
    uow = _AugmentationUow(repository, investigation)
    store = _AugmentationStore(blobs)
    preview = _PreviewRenderer()
    service = PublicationAugmentationArtifactService(
        uow_factory=lambda: uow,
        artifact_store=store,  # type: ignore[arg-type]
        preview_renderer=preview,
    )

    publication = await service.publish(
        base_publication_artifact_id=base_artifact.id,
        opinion=opinion,
        citation_context=context,
    )

    assert publication.stage is ProductionArtifactStage.PUBLICATION
    assert publication.version == 5
    assert publication.status is ProductionArtifactStatus.VERIFIED
    assert (
        publication.metadata["publication_augmentation"]["bridge_drafting_gap_acknowledged"] is True
    )

    legacy_metadata = dict(publication.metadata)
    legacy_augmentation = dict(legacy_metadata["publication_augmentation"])
    del legacy_augmentation["bridge_drafting_gap_acknowledged"]
    legacy_metadata["publication_augmentation"] = legacy_augmentation
    legacy_publication = replace(publication, metadata=legacy_metadata)
    repository.items[-1] = legacy_publication
    replay = await service.publish(
        base_publication_artifact_id=base_artifact.id,
        opinion=opinion,
        citation_context=context,
    )

    assert replay is legacy_publication
    assert "bridge_drafting_gap_acknowledged" not in replay.metadata["publication_augmentation"]
    assert publication.metadata["previous_publication_artifact_id"] == str(base_artifact.id)
    assert publication.canonical_blob_id != base_blob_id
    assert blobs[base_blob_id] == base_bytes
    assert uow.committed is True
    assert preview.artifact_ids == [publication.id]
    assert {
        stage: tuple(
            (item.id, item.version, item.input_hash)
            for item in repository.items
            if item.stage is stage
        )
        for stage in (
            ProductionArtifactStage.SYNTHESIS,
            ProductionArtifactStage.EDITORIAL_ENRICHMENT,
        )
    } == editorial_identity_before


# ---------------------------------------------------------------------------
# R3-F1 — a resolved V9/V10 base stays resolved through opinion augmentation
# ---------------------------------------------------------------------------


def _envelope_sha256(document: Any) -> str:
    return hashlib.sha256(
        ProductionArtifactStore.canonical_json_bytes(serialize_publication_document(document))
    ).hexdigest()


def _resolved_envelope(
    document_class: type[PublicationDocumentV9] | type[PublicationDocumentV10],
) -> tuple[PublicationDocumentV9 | PublicationDocumentV10, Any]:
    v9, base = _v9_publication_with_elements(tables=(_after_paragraph_table(),))
    if document_class is PublicationDocumentV9:
        return v9, base
    return (
        PublicationDocumentV10(
            base_publication=v9.base_publication,
            article_format=v9.article_format,
            registry_hash=v9.registry_hash,
            synthesis_canonical_sha256=v9.synthesis_canonical_sha256,
            enrichment_canonical_sha256=v9.enrichment_canonical_sha256,
            resolved_placements=v9.resolved_placements,
        ),
        base,
    )


def _article_element_block(base: Any) -> ArticleElementNoteBlockV1:
    published = base.document.tables[0]
    return ArticleElementNoteBlockV1(
        kind=NoteArticleElementKind.TABLE,
        element_key=published.key,
        element_sha256=publication_element_sha256(base.document, kind="table", key=published.key),
        anchor=LEAD_ANCHOR,
    )


def _opinion_pinned_to(
    base: Any,
    document: Any,
    block: ArticleElementNoteBlockV1,
) -> tuple[AnalystOpinionV1, Any]:
    """Build an approved opinion carrying one article_element note on a resolved base."""
    opinion, citation_context = _approved_opinion(base)
    note = AnalystObservationV1(
        id=UUID(int=9301),
        anchor=LEAD_ANCHOR,
        text="NOTE-RESOLVED-BASE",
        state=AnalystObservationState.UNRESOLVED,
        relation=AnalystObservationRelation.UNRESOLVED,
        content_blocks=(block,),
    )
    base_sha256 = _envelope_sha256(document)
    return (
        replace(
            opinion,
            base_publication_sha256=base_sha256,
            blocks=opinion_blocks_with_observations(opinion.blocks, (note,)),
            content_sha256="",
        ),
        replace(citation_context, base_publication_sha256=base_sha256),
    )


@pytest.mark.parametrize("document_class", (PublicationDocumentV9, PublicationDocumentV10))
def test_augmentation_preserves_the_resolved_envelope(document_class: Any) -> None:
    """A V9/V10 base keeps its pins and placements in the augmented publication."""
    document, base = _resolved_envelope(document_class)
    opinion, citation_context = _opinion_pinned_to(base, document, _article_element_block(base))

    augmented = augment_publication_with_opinion(document, opinion, citation_context)

    assert type(augmented) is document_class
    assert augmented.registry_hash == document.registry_hash
    assert augmented.synthesis_canonical_sha256 == document.synthesis_canonical_sha256
    assert augmented.enrichment_canonical_sha256 == document.enrichment_canonical_sha256
    assert augmented.resolved_placements == document.resolved_placements
    assert augmented.article_format == document.article_format
    assert augmented.base_publication.base_publication.base_publication == base
    parsed = publication_document_from_json(serialize_publication_document(augmented))
    assert type(parsed) is document_class
    assert parsed.resolved_placements == document.resolved_placements
    # The resolved augmentation is renderable; the plain-V8 downcast was not.
    project_publication_to_typst_model(augmented)


def test_note_article_element_accepted_on_v9_stays_valid_after_augmentation() -> None:
    """The accepted note block still resolves against the augmented publication."""
    v9, base = _resolved_envelope(PublicationDocumentV9)
    block = _article_element_block(base)
    opinion, citation_context = _opinion_pinned_to(base, v9, block)

    resolved_context = NoteContentBlockContextV1(
        investigation_id=opinion.investigation_id,
        anchor=LEAD_ANCHOR,
        elements=note_article_element_facts(v9),
        observations={},
        blobs={},
        protocol_figures=(),
    )
    (resolved,) = validate_note_content_blocks((block,), resolved_context)
    assert resolved.anchor == LEAD_ANCHOR
    assert resolved.element_sha256 == block.element_sha256

    augmented = augment_publication_with_opinion(v9, opinion, citation_context)

    assert type(augmented) is PublicationDocumentV9
    augmented_context = replace(resolved_context, elements=note_article_element_facts(augmented))
    assert [(fact.kind, fact.key, fact.anchor) for fact in augmented_context.elements] == [
        ("table", block.element_key, LEAD_ANCHOR)
    ]
    (resolved_after,) = validate_note_content_blocks((block,), augmented_context)
    assert resolved_after == resolved


@pytest.mark.asyncio
async def test_artifact_service_persists_a_resolved_v9_augmentation() -> None:
    """The publication service stores the augmented V9, not a bare V8."""
    document, base = _resolved_envelope(PublicationDocumentV9)
    opinion, citation_context = _opinion_pinned_to(base, document, _article_element_block(base))
    synthesis_bytes = b"verified canonical synthesis"
    opinion = replace(
        opinion,
        synthesis_sha256=hashlib.sha256(synthesis_bytes).hexdigest(),
        bridge_drafting_gap_acknowledged=True,
        content_sha256="",
    )
    run_id = UUID(int=9311)
    base_blob_id = UUID(int=9312)
    synthesis_blob_id = UUID(int=9313)
    base_payload = serialize_publication_document(document)
    base_bytes = ProductionArtifactStore.canonical_json_bytes(base_payload)
    assert hashlib.sha256(base_bytes).hexdigest() == citation_context.base_publication_sha256
    blobs = {base_blob_id: base_bytes, synthesis_blob_id: synthesis_bytes}
    base_artifact = ProductionArtifact(
        id=citation_context.base_publication_artifact_id,
        production_run_id=run_id,
        subject_id=base.subject_id,
        stage=ProductionArtifactStage.PUBLICATION,
        version=1,
        input_hash=hashlib.sha256(base_bytes).hexdigest(),
        canonical_blob_id=base_blob_id,
    )
    synthesis_artifact = ProductionArtifact(
        id=opinion.synthesis_artifact_id,
        production_run_id=run_id,
        subject_id=base.subject_id,
        stage=ProductionArtifactStage.SYNTHESIS,
        version=1,
        input_hash="d" * 64,
        canonical_blob_id=synthesis_blob_id,
    )
    repository = _ArtifactRepository((base_artifact, synthesis_artifact))
    investigation = SimpleNamespace(
        production_run_id=run_id,
        subject_id=base.subject_id,
        synthesis_artifact_id=opinion.synthesis_artifact_id,
    )
    uow = _AugmentationUow(repository, investigation)
    store = _AugmentationStore(blobs)
    preview = _PreviewRenderer()
    service = PublicationAugmentationArtifactService(
        uow_factory=lambda: uow,
        artifact_store=store,  # type: ignore[arg-type]
        preview_renderer=preview,
    )

    publication = await service.publish(
        base_publication_artifact_id=base_artifact.id,
        opinion=opinion,
        citation_context=citation_context,
    )

    stored = publication_document_from_json(json.loads(blobs[publication.canonical_blob_id]))
    assert type(stored) is PublicationDocumentV9
    assert stored.resolved_placements == document.resolved_placements
    assert stored.registry_hash == document.registry_hash
    assert stored.synthesis_canonical_sha256 == document.synthesis_canonical_sha256
    assert stored.enrichment_canonical_sha256 == document.enrichment_canonical_sha256
    assert stored.base_publication.base_publication.analyst_opinion is not None
    assert uow.committed is True
    assert preview.artifact_ids == [publication.id]
