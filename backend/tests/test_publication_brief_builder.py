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
from cti_app.domain.production_synthesis import (
    EvidenceKind,
    SynthesisParagraphV1,
    SynthesisTimelineEntryV1,
    extraction_evidence_refs_v1,
)
from cti_app.domain.publication_kind import PublicationKind
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


def test_brief_qa_rejects_claimed_enrichment_and_requires_relevance_projection() -> None:
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

    rejected_enrichment = qa_publication_v7(
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

    assert rejected_enrichment["passed"] is False
    assert rejected_enrichment["error_codes"] == ["publication_qa_editorial_enrichment_forbidden"]
    assert missing_relevance["passed"] is False
    assert missing_relevance["error_codes"] == ["publication_qa_kind_inputs_invalid"]


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
    )
    section_types = [item["type"] for item in render_model.content_sections]
    references_section = render_model.content_sections[0]

    assert publication.timeline == ()
    assert section_types == ["references", "synthesis"]
    assert render_model.article_format is None
    assert any(source_url in entry.get("urls", ()) for entry in references_section["entries"])
    assert publication.indicators == ()
    assert qa["passed"] is True


def test_brief_timeline_adds_distinct_archived_source_publication_dates() -> None:
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
    )

    assert {entry.event_date for entry in publication.timeline} == {
        date(2025, 1, 6),
        date(2025, 1, 10),
    }
    assert all(
        ref.kind.value == "source_metadata"
        for entry in publication.timeline
        for ref in entry.evidence_refs
    )
    assert [section["type"] for section in render_model.content_sections] == [
        "references",
        "chronology",
        "synthesis",
    ]
