from __future__ import annotations

from dataclasses import replace
from datetime import date
from uuid import UUID

import pytest

from cti_app.application.brief_publication_builder import (
    build_brief_publication_document_v5,
    wrap_publication_document_v7,
)
from cti_app.application.production_relevance import build_relevance_projection
from cti_app.application.publication_qa import qa_publication_v7
from cti_app.application.typst_rendering import project_publication_to_typst_model
from cti_app.domain.production_synthesis import (
    SynthesisParagraphV1,
    SynthesisTimelineEntryV1,
    extraction_evidence_refs_v1,
)
from cti_app.domain.publication_kind import PublicationKind
from tests.editorial_enrichment_support import build_empty_editorial_enrichment
from tests.test_publication_builder_v4 import _canonical_inputs


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
    assert [entry.event_date for entry in publication.timeline] == [date(2026, 10, 18)]
    assert (
        publication.timeline[0].evidence_refs[0].source_document_id
        == evidence_ref.source_document_id
    )
    assert publication.indicators == ()
    assert brief.original_indicators == ()
    assert brief.ioc_details == ()
    assert not any(
        section.heading.casefold() == "vue d'ensemble" for section in publication.sections
    )
    assert all(section.paragraphs for section in publication.sections)
    assert [item["type"] for item in render_model.content_sections] == [
        "references",
        "chronology",
        "synthesis",
    ]
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
