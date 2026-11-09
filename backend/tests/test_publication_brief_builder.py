from __future__ import annotations

from dataclasses import replace
from datetime import date
from uuid import UUID

import pytest

from cti_app.application.brief_publication_builder import (
    build_brief_publication_document_v5,
    wrap_publication_document_v7,
)
from cti_app.application.production_brief_synthesis import (
    BRIEF_CHRONOLOGY_METADATA_PUBLICATION_FALLBACK_WARNING,
    brief_timeline_from_extraction,
)
from cti_app.application.production_extraction import references_corpus_hash
from cti_app.application.production_relevance import build_relevance_projection
from cti_app.application.production_synthesis import canonical_extraction_hash
from cti_app.application.publication_qa import qa_publication_v7
from cti_app.application.typst_rendering import project_publication_to_typst_model
from cti_app.domain.classification import TLP
from cti_app.domain.discovery import SourceRole
from cti_app.domain.production import ProductionInputSource
from cti_app.domain.production_editorial_enrichment import (
    EditorialAnalyticPurposeV1,
    EnrichmentPlacementKind,
    EnrichmentPlacementV1,
    EnrichmentTableKind,
    SourceFigureCandidateV1,
    SourceFigureInclusionStatus,
    SourceFigureLocatorV1,
    TableColumnV1,
    TableRowV1,
    TableSpecV1,
)
from cti_app.domain.production_synthesis import (
    EvidenceKind,
    SynthesisParagraphV1,
    SynthesisTimelineEntryV1,
    extraction_evidence_refs_v1,
)
from cti_app.domain.publication_kind import PublicationKind
from cti_app.domain.semantic_annotation import (
    SemanticAnnotationProposalV1,
    SemanticRole,
    lead_paragraph_anchor,
)
from tests.editorial_enrichment_support import build_empty_editorial_enrichment
from tests.test_publication_builder_v4 import (
    _canonical_inputs,
    _publication_case_with_content_variants,
)


@pytest.mark.parametrize("word_count", [80, 800])
def test_brief_assembly_stays_brief_across_narrative_lengths(word_count: int) -> None:
    snapshot, references, extraction, synthesis = _canonical_inputs()
    relevance = build_relevance_projection(snapshot, extraction)
    evidence_ref = extraction_evidence_refs_v1(extraction)[0]
    paragraph = SynthesisParagraphV1(" ".join(["reported"] * word_count), (evidence_ref,))
    timeline_entry = SynthesisTimelineEntryV1(
        event_date=date(2026, 10, 18),
        date_text="18 October 2026",
        text="The source records a dated activity.",
        evidence_refs=(evidence_ref,),
    )
    synthesis = replace(synthesis, lead=(paragraph,), timeline=(timeline_entry,))

    brief = build_brief_publication_document_v5(
        snapshot=snapshot,
        references=references,
        extraction=extraction,
        relevance_projection=relevance,
        synthesis=synthesis,
    )
    publication = wrap_publication_document_v7(
        brief,
        base_publication_artifact_id=UUID(int=1000 + word_count),
        publication_kind=PublicationKind.BRIEF,
        routing_decision_hash="c" * 64,
        routing_policy_version="routing-policy-v1",
    )
    qa = qa_publication_v7(
        snapshot=snapshot,
        references=references,
        extraction=extraction,
        relevance_projection=relevance,
        synthesis=synthesis,
        editorial_enrichment=None,
        publication=publication,
    )
    render_model = project_publication_to_typst_model(publication)

    assert publication.publication_kind is PublicationKind.BRIEF
    assert len(publication.lead[0].text.split()) == word_count
    assert publication.timeline == ()
    assert publication.indicators == ()
    assert brief.original_indicators == ()
    assert brief.ioc_details == ()
    assert not any(
        section.heading.casefold() == "vue d'ensemble" for section in publication.sections
    )
    assert all(section.paragraphs for section in publication.sections)
    assert [item["type"] for item in render_model.content_sections] == [
        "references",
        "synthesis",
    ]
    assert render_model.article_format is None
    assert qa["passed"] is True
    assert qa["checks"]["no_placeholder_markers"] is True
    assert qa["checks"]["no_ioc_annex"] is True


def test_brief_qa_accepts_empty_enrichment_and_requires_relevance_projection() -> None:
    snapshot, references, extraction, synthesis = _canonical_inputs()
    relevance = build_relevance_projection(snapshot, extraction)
    brief = build_brief_publication_document_v5(
        snapshot=snapshot,
        references=references,
        extraction=extraction,
        relevance_projection=relevance,
        synthesis=synthesis,
    )
    publication = wrap_publication_document_v7(
        brief,
        base_publication_artifact_id=UUID(int=1100),
        publication_kind=PublicationKind.BRIEF,
        routing_decision_hash="d" * 64,
        routing_policy_version="routing-policy-v1",
    )
    enrichment = build_empty_editorial_enrichment(extraction=extraction, synthesis=synthesis)

    accepted_empty_enrichment = qa_publication_v7(
        snapshot=snapshot,
        references=references,
        extraction=extraction,
        relevance_projection=relevance,
        synthesis=synthesis,
        editorial_enrichment=enrichment,
        publication=publication,
    )
    missing_relevance = qa_publication_v7(
        snapshot=snapshot,
        references=references,
        extraction=extraction,
        relevance_projection=None,
        synthesis=synthesis,
        editorial_enrichment=None,
        publication=publication,
    )

    assert accepted_empty_enrichment["passed"] is True
    assert accepted_empty_enrichment["checks"]["exact_projection"] is True
    assert missing_relevance["passed"] is False
    assert missing_relevance["error_codes"] == ["publication_qa_kind_inputs_invalid"]


def test_brief_enrichment_rejects_more_than_three_source_figures() -> None:
    snapshot, references, extraction, synthesis = _canonical_inputs()
    relevance = build_relevance_projection(snapshot, extraction)
    source = extraction.sources[0]
    enrichment = replace(
        build_empty_editorial_enrichment(extraction=extraction, synthesis=synthesis),
        source_figures=tuple(
            SourceFigureCandidateV1(
                key=f"archived_figure_{index}",
                source_document_id=source.source_document_id,
                source_url=source.canonical_url,
                caption=f"Archived figure {index}",
                provenance="Archived figure from the source",
                locator=SourceFigureLocatorV1(page=index + 1),
                inclusion_status=SourceFigureInclusionStatus.PROPOSED,
                placement=EnrichmentPlacementV1(EnrichmentPlacementKind.END),
            )
            for index in range(4)
        ),
    )

    with pytest.raises(ValueError, match="exceeds the archived figure limit"):
        build_brief_publication_document_v5(
            snapshot=snapshot,
            references=references,
            extraction=extraction,
            relevance_projection=relevance,
            synthesis=synthesis,
            editorial_enrichment=enrichment,
        )


def test_brief_enrichment_projects_evidence_table_and_exact_semantic_annotation() -> None:
    snapshot, references, extraction, synthesis = _canonical_inputs()
    evidence_ref = extraction_evidence_refs_v1(extraction)[0]
    synthesis = replace(
        synthesis,
        lead=(replace(synthesis.lead[0], text="Example actor is documented by Example Lab."),),
    )
    enrichment = replace(
        build_empty_editorial_enrichment(extraction=extraction, synthesis=synthesis),
        tables=(
            TableSpecV1(
                key="observations",
                kind=EnrichmentTableKind.CUSTOM,
                title="Observed actor",
                caption="The source names the actor.",
                columns=(
                    TableColumnV1("entity", "Entity"),
                    TableColumnV1("role", "Role"),
                ),
                rows=(TableRowV1(("Example actor", "Reported actor"), (evidence_ref,)),),
                placement=EnrichmentPlacementV1(EnrichmentPlacementKind.AFTER_LEAD),
                purpose=EditorialAnalyticPurposeV1(
                    question="Which actor is named?",
                    available_data="The source names Example actor.",
                    comprehension_gain="The row pairs the name and its reported role.",
                    scope="The actor named by this source.",
                    evidence_refs=(evidence_ref,),
                    knowledge_limits="No additional identity is asserted.",
                    placement_reason="Place beside the lead that names the actor.",
                ),
            ),
        ),
        annotations=(
            SemanticAnnotationProposalV1(
                lead_paragraph_anchor(1), SemanticRole.PROPER_NAME, "Example Lab"
            ),
        ),
    )

    brief = build_brief_publication_document_v5(
        snapshot=snapshot,
        references=references,
        extraction=extraction,
        relevance_projection=build_relevance_projection(snapshot, extraction),
        synthesis=synthesis,
        editorial_enrichment=enrichment,
    )

    assert brief.document.tables[0].rows[0].evidence_refs == (evidence_ref,)
    lead = next(item for item in brief.semantic_text.paragraphs if item.anchor == "lead:0001")
    assert any(
        span.role is SemanticRole.PROPER_NAME and span.text == "Example Lab" for span in lead.spans
    )


def test_single_metadata_derived_brief_date_stays_in_references_without_chronology() -> None:
    snapshot, references, extraction, synthesis = _canonical_inputs()
    published_at = date(2025, 1, 10)
    source_url = extraction.sources[0].canonical_url
    discovery_candidate_id = UUID(int=5100)
    input_source = ProductionInputSource(
        discovery_candidate_id=discovery_candidate_id,
        source_candidate_id=UUID(int=5101),
        canonical_url=source_url,
        role=SourceRole.PRIMARY,
        title="Example report",
        publisher="Example publisher",
        published_at=published_at,
        tlp=TLP.CLEAR,
        sensitivity="clear",
        external_llm_allowed=True,
    )
    snapshot = replace(
        snapshot,
        member_candidate_ids=(discovery_candidate_id,),
        core_sources=(input_source,),
        input_hash="",
        reuse_basis_hash="",
    )
    references = replace(
        references,
        production_input_hash=snapshot.input_hash,
        sources=(
            replace(
                references.sources[0],
                title=input_source.title,
                publisher=input_source.publisher,
                published_at=published_at,
            ),
        ),
    )
    extraction = replace(
        extraction,
        production_input_hash=snapshot.input_hash,
        references_corpus_hash=references_corpus_hash(references),
        sources=(replace(extraction.sources[0], events=()),),
    )
    expected_timeline = brief_timeline_from_extraction(
        extraction,
        (),
        snapshot=snapshot,
    )
    assert expected_timeline is not None
    assert expected_timeline[0].evidence_refs[0].kind is EvidenceKind.SOURCE_METADATA
    synthesis = replace(
        synthesis,
        production_input_hash=snapshot.input_hash,
        extraction_hash=canonical_extraction_hash(extraction),
        timeline=expected_timeline,
        warnings=(BRIEF_CHRONOLOGY_METADATA_PUBLICATION_FALLBACK_WARNING,),
    )
    relevance = build_relevance_projection(snapshot, extraction)

    brief = build_brief_publication_document_v5(
        snapshot=snapshot,
        references=references,
        extraction=extraction,
        relevance_projection=relevance,
        synthesis=synthesis,
    )
    publication = wrap_publication_document_v7(
        brief,
        base_publication_artifact_id=UUID(int=5102),
        publication_kind=PublicationKind.BRIEF,
        routing_decision_hash="e" * 64,
        routing_policy_version="routing-policy-v1",
    )
    qa = qa_publication_v7(
        snapshot=snapshot,
        references=references,
        extraction=extraction,
        relevance_projection=relevance,
        synthesis=synthesis,
        editorial_enrichment=None,
        publication=publication,
    )
    render_model = project_publication_to_typst_model(
        publication,
        include_brief_chronology=True,
        brief_timeline_policy_version="brief-assembly-v4-m19-publication-events",
    )
    section_types = [item["type"] for item in render_model.content_sections]
    references_section = render_model.content_sections[0]

    assert publication.timeline == ()
    assert section_types == ["references", "synthesis"]
    assert render_model.article_format is None
    assert any(source_url in entry.get("urls", ()) for entry in references_section["entries"])
    assert publication.indicators == ()
    assert qa["passed"] is True


def test_brief_timeline_excludes_context_source_and_hides_single_publication_date() -> None:
    snapshot, references, extraction, synthesis = _publication_case_with_content_variants(
        ("d" * 64,)
    )
    relevance = build_relevance_projection(snapshot, extraction)

    brief = build_brief_publication_document_v5(
        snapshot=snapshot,
        references=references,
        extraction=extraction,
        relevance_projection=relevance,
        synthesis=synthesis,
    )
    publication = wrap_publication_document_v7(
        brief,
        base_publication_artifact_id=UUID(int=5200),
        publication_kind=PublicationKind.BRIEF,
        routing_decision_hash="f" * 64,
        routing_policy_version="routing-policy-v1",
    )
    render_model = project_publication_to_typst_model(
        publication,
        include_brief_chronology=True,
        brief_timeline_policy_version="brief-assembly-v4-m19-publication-events",
    )

    # The second archived source is editorial context, so only the core
    # publication date remains; C1 suppresses a one-date chronology.
    assert publication.timeline == ()
    assert [section["type"] for section in render_model.content_sections] == [
        "references",
        "synthesis",
    ]
