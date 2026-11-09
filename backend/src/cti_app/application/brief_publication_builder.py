"""Build a publication brief directly from canonical production inputs."""

from __future__ import annotations

from uuid import UUID

from cti_app.application.production_brief_synthesis import (
    BRIEF_CHRONOLOGY_METADATA_PUBLICATION_FALLBACK_WARNING,
    brief_timeline_from_extraction,
)
from cti_app.application.production_synthesis import build_synthesis_timeline
from cti_app.application.publication_builder import (
    PublicationAssemblyValidationError,
    _project_publication_references,
    _project_publication_sources,
    _project_synthesis_publication,
    _validate_publication_lineage,
)
from cti_app.application.semantic_annotation import (
    SemanticAnnotator,
    semantic_entities_from_extraction,
)
from cti_app.domain.production import ProductionInputSnapshot
from cti_app.domain.production_extraction import ProductionExtractionV1
from cti_app.domain.production_references import ProductionReferenceCorpusV1
from cti_app.domain.production_relevance import RelevanceProjectionV1
from cti_app.domain.production_synthesis import (
    EvidenceKind,
    ExtractionEvidenceRefV1,
    ProductionSynthesisV1,
    synthesis_evidence_refs,
)
from cti_app.domain.publication import (
    PublicationAssemblyErrorCode,
    PublicationSectionKind,
    PublicationSectionV1,
    PublicationTimelineEntryV1,
)
from cti_app.domain.publication_document import (
    PUBLICATION_DOCUMENT_V4_SCHEMA_VERSION,
    PublicationDocumentV4,
    PublicationDocumentV5,
    PublicationDocumentV6,
    PublicationDocumentV6ProvenanceV1,
    PublicationDocumentV7,
    build_publication_date_events,
    order_publication_references,
    publication_document_text_anchors,
    publication_document_v5_sha256,
    select_publication_timeline_key_events_v2,
)
from cti_app.domain.publication_kind import PublicationKind
from cti_app.domain.semantic_annotation import (
    SEMANTIC_ANNOTATION_POLICY_VERSION,
    SEMANTIC_ANNOTATION_SCHEMA_VERSION,
    SemanticTextV1,
)

# M19 changes reader references and source-date timelines; this invalidates old brief assemblies.
BRIEF_ASSEMBLY_POLICY_VERSION = "brief-assembly-v2-m19-timeline-references"


def build_brief_publication_document_v5(
    *,
    snapshot: ProductionInputSnapshot,
    references: ProductionReferenceCorpusV1,
    extraction: ProductionExtractionV1,
    relevance_projection: RelevanceProjectionV1 | None,
    synthesis: ProductionSynthesisV1,
) -> PublicationDocumentV5:
    """Project a brief without creating or requiring editorial enrichment."""
    if relevance_projection is None:
        raise PublicationAssemblyValidationError(
            PublicationAssemblyErrorCode.INPUTS_MISMATCH,
            "A brief requires a canonical relevance projection",
        )
    _validate_publication_lineage(
        snapshot=snapshot,
        references=references,
        extraction=extraction,
        synthesis=synthesis,
        relevance_projection=relevance_projection,
    )
    metadata_refs = frozenset(
        ref
        for ref in synthesis_evidence_refs(synthesis)
        if ref.kind is EvidenceKind.SOURCE_METADATA
    )
    allowed_metadata_refs: frozenset[ExtractionEvidenceRefV1] = frozenset()
    if metadata_refs:
        expected_timeline = brief_timeline_from_extraction(
            extraction,
            build_synthesis_timeline(extraction),
            snapshot=snapshot,
            projection=relevance_projection,
            cited_evidence_refs=(
                ref for paragraph in synthesis.lead for ref in paragraph.evidence_refs
            ),
        )
        expected_metadata_refs = frozenset(
            ref
            for entry in expected_timeline or ()
            for ref in entry.evidence_refs
            if ref.kind is EvidenceKind.SOURCE_METADATA
        )
        non_timeline_metadata_refs = {
            ref
            for paragraph in (
                *synthesis.lead,
                *(paragraph for section in synthesis.sections for paragraph in section.paragraphs),
            )
            for ref in paragraph.evidence_refs
            if ref.kind is EvidenceKind.SOURCE_METADATA
        } | {
            ref
            for note in synthesis.source_notes
            for ref in note.evidence_refs
            if ref.kind is EvidenceKind.SOURCE_METADATA
        }
        if (
            not expected_metadata_refs
            or metadata_refs != expected_metadata_refs
            or non_timeline_metadata_refs
            or synthesis.timeline != expected_timeline
            or BRIEF_CHRONOLOGY_METADATA_PUBLICATION_FALLBACK_WARNING not in synthesis.warnings
        ):
            raise PublicationAssemblyValidationError(
                PublicationAssemblyErrorCode.EVIDENCE_MISSING,
                "Brief source metadata citation does not match its deterministic fallback",
            )
        allowed_metadata_refs = expected_metadata_refs
    narrative = _project_synthesis_publication(
        extraction=extraction,
        synthesis=synthesis,
        include_timeline=False,
        allowed_source_metadata_refs=allowed_metadata_refs,
    )
    reference_entries = _project_publication_references(
        references=references,
        extraction=extraction,
        synthesis=synthesis,
    )

    lead = list(narrative.lead)
    sections: list[PublicationSectionV1] = []
    lead_fingerprints = {" ".join(item.text.casefold().split()) for item in lead}
    for section in narrative.sections:
        heading_key = " ".join(section.heading.casefold().replace("\u2019", "'").split())
        paragraphs = tuple(
            paragraph
            for paragraph in section.paragraphs
            if " ".join(paragraph.text.casefold().split()) not in lead_fingerprints
        )
        if heading_key in {"vue d'ensemble", "overview"} or (
            section.kind is PublicationSectionKind.OVERVIEW
        ):
            for paragraph in paragraphs:
                fingerprint = " ".join(paragraph.text.casefold().split())
                if fingerprint not in lead_fingerprints:
                    lead.append(paragraph)
                    lead_fingerprints.add(fingerprint)
            continue
        if paragraphs:
            sections.append(
                PublicationSectionV1(
                    kind=section.kind,
                    heading=section.heading,
                    paragraphs=paragraphs,
                )
            )

    full_narrative = _project_synthesis_publication(
        extraction=extraction,
        synthesis=synthesis,
        include_timeline=True,
        allowed_source_metadata_refs=allowed_metadata_refs,
    )
    source_timeline = tuple(
        PublicationTimelineEntryV1(
            event_date=entry.event_date,
            date_text=entry.date_text,
            text=entry.text,
            evidence_refs=entry.evidence_refs,
        )
        for entry in full_narrative.timeline
        if entry.event_date is not None
    )
    additional_source_ids = {item.source_document_id for item in reference_entries}
    additional_source_ids.update(
        ref.source_document_id for item in reference_entries for ref in item.evidence_refs
    )
    used_source_document_ids = set(narrative.used_source_document_ids)
    used_source_document_ids.update(
        ref.source_document_id for entry in source_timeline for ref in entry.evidence_refs
    )
    used_source_document_ids.update(additional_source_ids)
    sources = _project_publication_sources(
        references=references,
        used_source_document_ids=used_source_document_ids,
    )
    publication_date_events = tuple(
        PublicationTimelineEntryV1(
            event_date=event.event_date,
            date_text=None,
            text=event.text,
            evidence_refs=event.evidence_refs,
        )
        for event in build_publication_date_events(sources)
    )
    selected_timeline = select_publication_timeline_key_events_v2(
        (*source_timeline, *publication_date_events)
    )
    # Apply the two-distinct-date gate after merging cited events and archived
    # source publication dates. A lone source date remains visible in Références.
    timeline = tuple(
        PublicationTimelineEntryV1(
            event_date=event.event_date,
            date_text=None,
            text=event.text,
            evidence_refs=event.evidence_refs,
        )
        for event in selected_timeline
    )
    ordered_references = order_publication_references(reference_entries, sources)
    document = PublicationDocumentV4(
        schema_version=PUBLICATION_DOCUMENT_V4_SCHEMA_VERSION,
        subject_id=snapshot.subject_id,
        publication_language=synthesis.publication_language,
        title=synthesis.title,
        lead=tuple(lead),
        sections=tuple(sections),
        timeline=timeline,
        indicators=(),
        sources=sources,
        uncertainties=(),
        tables=(),
        diagrams=(),
        figures=(),
        additional_source_ids=frozenset(additional_source_ids),
    )
    anchors = publication_document_text_anchors(document, references=ordered_references)
    semantic_text = SemanticTextV1(
        schema_version=SEMANTIC_ANNOTATION_SCHEMA_VERSION,
        policy_version=SEMANTIC_ANNOTATION_POLICY_VERSION,
        paragraphs=SemanticAnnotator().annotate_paragraphs(
            tuple(anchors.items()),
            entities=semantic_entities_from_extraction(extraction),
        ),
    )
    return PublicationDocumentV5(
        document=document,
        semantic_text=semantic_text,
        references=ordered_references,
    )


def wrap_publication_document_v7(
    publication: PublicationDocumentV5,
    *,
    base_publication_artifact_id: UUID,
    publication_kind: PublicationKind,
    routing_decision_hash: str,
    routing_policy_version: str,
) -> PublicationDocumentV7:
    """Wrap the exact V5 content in the V6 and V7 versioned contracts."""
    v6 = PublicationDocumentV6(
        base_publication=publication,
        provenance=PublicationDocumentV6ProvenanceV1(
            base_publication_artifact_id=base_publication_artifact_id,
            base_publication_sha256=publication_document_v5_sha256(publication),
        ),
    )
    return PublicationDocumentV7(
        base_publication=v6,
        publication_kind=publication_kind,
        routing_decision_hash=routing_decision_hash,
        routing_policy_version=routing_policy_version,
    )
