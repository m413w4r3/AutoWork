"""Build a publication brief directly from canonical production inputs."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from uuid import UUID

from cti_app.application.production_brief_synthesis import (
    BRIEF_CHRONOLOGY_METADATA_PUBLICATION_FALLBACK_WARNING,
    brief_timeline_from_extraction,
)
from cti_app.application.production_editorial_enrichment import (
    MAX_BRIEF_ENRICHMENT_FIGURE_PROPOSALS,
)
from cti_app.application.production_synthesis import build_synthesis_timeline
from cti_app.application.publication_builder import (
    PublicationAssemblyValidationError,
    _project_publication_charts,
    _project_publication_diagrams,
    _project_publication_figures,
    _project_publication_references,
    _project_publication_sources,
    _project_publication_tables,
    _project_synthesis_publication,
    _validate_publication_enrichment,
    _validate_publication_lineage,
)
from cti_app.application.semantic_annotation import (
    SemanticAnnotator,
    semantic_entities_from_extraction,
)
from cti_app.domain.production import ProductionInputSnapshot
from cti_app.domain.production_editorial_enrichment import (
    EditorialEnrichmentV1,
    editorial_enrichment_evidence_refs,
)
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
    PublicationParagraphV1,
    PublicationSectionV1,
    PublicationTimelineEntryV1,
    is_overview_section,
)
from cti_app.domain.publication_document import (
    PUBLICATION_DOCUMENT_V4_SCHEMA_VERSION,
    PublicationDocumentV4,
    PublicationDocumentV5,
    PublicationDocumentV6,
    PublicationDocumentV6ProvenanceV1,
    PublicationDocumentV7,
    order_publication_references,
    publication_document_text_anchors,
    publication_document_v5_sha256,
    select_publication_timeline_key_events_v3,
)
from cti_app.domain.publication_kind import PublicationKind
from cti_app.domain.semantic_annotation import (
    SEMANTIC_ANNOTATION_POLICY_VERSION,
    SEMANTIC_ANNOTATION_SCHEMA_VERSION,
    SemanticTextV1,
)

# M19 changes reader references and source-date timelines; this invalidates old brief assemblies.
BRIEF_ASSEMBLY_POLICY_VERSION = "brief-assembly-v4-m19-publication-events"


@dataclass(frozen=True, slots=True)
class BriefPublicationAnchorMap:
    """Where each synthesis paragraph lands in the merged brief publication body.

    Values are ``(paragraph_kind, section_index, paragraph_index)`` in the
    publication document. Lead paragraphs keep their identity and are not
    stored. A missing section-paragraph key means the merge collapsed the
    paragraph into an ambiguous duplicate; callers must refuse that anchor
    rather than guess.
    """

    section_paragraphs: Mapping[tuple[int, int], tuple[str, int | None, int]]
    synthesis_lead_count: int

    def resolve(
        self,
        *,
        paragraph_kind: str,
        section_index: int | None,
        paragraph_index: int,
    ) -> tuple[str, int | None, int] | None:
        """Map one synthesis paragraph identity into the publication body."""
        if paragraph_kind == "lead":
            if 0 <= paragraph_index < self.synthesis_lead_count:
                return ("lead", None, paragraph_index)
            return None
        if paragraph_kind != "section" or section_index is None:
            return None
        return self.section_paragraphs.get((section_index, paragraph_index))


def _brief_paragraph_fingerprint(text: str) -> str:
    return " ".join(text.casefold().split())


def merge_brief_narrative_sections(
    *,
    lead: tuple[PublicationParagraphV1, ...],
    sections: tuple[PublicationSectionV1, ...],
) -> tuple[
    tuple[PublicationParagraphV1, ...],
    tuple[PublicationSectionV1, ...],
    BriefPublicationAnchorMap,
]:
    """Merge overview sections into the lead exactly once, with their anchor map.

    This is the single authority for the BRIEF projection: the publication body
    and the placement resolver both consume its output, so a synthesis paragraph
    can never be rendered in a body position that resolution does not know.
    """
    merged_lead = list(lead)
    lead_fingerprints = {_brief_paragraph_fingerprint(item.text) for item in merged_lead}
    lead_indices_by_fingerprint: dict[str, list[int]] = {}
    for index, item in enumerate(merged_lead):
        lead_indices_by_fingerprint.setdefault(_brief_paragraph_fingerprint(item.text), []).append(
            index
        )
    anchor_map: dict[tuple[int, int], tuple[str, int | None, int]] = {}
    merged_sections: list[PublicationSectionV1] = []
    for section_index, section in enumerate(sections):
        if is_overview_section(section.kind, section.heading):
            for paragraph_index, paragraph in enumerate(section.paragraphs):
                fingerprint = _brief_paragraph_fingerprint(paragraph.text)
                target: tuple[str, int | None, int] | None
                if fingerprint not in lead_fingerprints:
                    merged_lead.append(paragraph)
                    lead_fingerprints.add(fingerprint)
                    lead_indices_by_fingerprint.setdefault(fingerprint, []).append(
                        len(merged_lead) - 1
                    )
                    target = ("lead", None, len(merged_lead) - 1)
                else:
                    indices = lead_indices_by_fingerprint.get(fingerprint, [])
                    target = ("lead", None, indices[0]) if len(indices) == 1 else None
                if target is not None:
                    anchor_map[(section_index, paragraph_index)] = target
            continue
        kept: list[PublicationParagraphV1] = []
        kept_source_indices: list[int] = []
        for paragraph_index, paragraph in enumerate(section.paragraphs):
            fingerprint = _brief_paragraph_fingerprint(paragraph.text)
            if fingerprint in lead_fingerprints:
                indices = lead_indices_by_fingerprint.get(fingerprint, [])
                if len(indices) == 1:
                    anchor_map[(section_index, paragraph_index)] = ("lead", None, indices[0])
                continue
            kept.append(paragraph)
            kept_source_indices.append(paragraph_index)
        if kept:
            publication_section_index = len(merged_sections)
            merged_sections.append(
                PublicationSectionV1(
                    kind=section.kind,
                    heading=section.heading,
                    paragraphs=tuple(kept),
                )
            )
            for publication_paragraph_index, source_paragraph_index in enumerate(
                kept_source_indices
            ):
                anchor_map[(section_index, source_paragraph_index)] = (
                    "section",
                    publication_section_index,
                    publication_paragraph_index,
                )
    return (
        tuple(merged_lead),
        tuple(merged_sections),
        BriefPublicationAnchorMap(
            section_paragraphs=MappingProxyType(dict(anchor_map)),
            synthesis_lead_count=len(lead),
        ),
    )


def build_brief_publication_document_v5(
    *,
    snapshot: ProductionInputSnapshot,
    references: ProductionReferenceCorpusV1,
    extraction: ProductionExtractionV1,
    relevance_projection: RelevanceProjectionV1 | None,
    synthesis: ProductionSynthesisV1,
    editorial_enrichment: EditorialEnrichmentV1 | None = None,
) -> PublicationDocumentV5:
    """Project a brief with optional, evidence-validated editorial enrichment."""
    document, _anchor_map = build_brief_publication_document_v5_with_anchor_map(
        snapshot=snapshot,
        references=references,
        extraction=extraction,
        relevance_projection=relevance_projection,
        synthesis=synthesis,
        editorial_enrichment=editorial_enrichment,
    )
    return document


def build_brief_publication_document_v5_with_anchor_map(
    *,
    snapshot: ProductionInputSnapshot,
    references: ProductionReferenceCorpusV1,
    extraction: ProductionExtractionV1,
    relevance_projection: RelevanceProjectionV1 | None,
    synthesis: ProductionSynthesisV1,
    editorial_enrichment: EditorialEnrichmentV1 | None = None,
) -> tuple[PublicationDocumentV5, BriefPublicationAnchorMap]:
    """Project a brief and return the synthesis-to-publication anchor map."""
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
    if editorial_enrichment is not None:
        if len(editorial_enrichment.source_figures) > MAX_BRIEF_ENRICHMENT_FIGURE_PROPOSALS:
            raise PublicationAssemblyValidationError(
                PublicationAssemblyErrorCode.VALIDATION_FAILED,
                "Brief editorial enrichment exceeds the archived figure limit",
            )
        _validate_publication_enrichment(
            editorial_enrichment=editorial_enrichment,
            extraction=extraction,
            synthesis=synthesis,
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

    tables = (
        _project_publication_tables(editorial_enrichment.tables)
        if editorial_enrichment is not None
        else ()
    )
    diagrams = (
        _project_publication_diagrams(editorial_enrichment.diagrams)
        if editorial_enrichment is not None
        else ()
    )
    figures = (
        _project_publication_figures(editorial_enrichment.source_figures)
        if editorial_enrichment is not None
        else ()
    )
    charts = (
        _project_publication_charts(editorial_enrichment.charts)
        if editorial_enrichment is not None
        else ()
    )

    lead, sections, anchor_map = merge_brief_narrative_sections(
        lead=narrative.lead,
        sections=narrative.sections,
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
    if editorial_enrichment is not None:
        additional_source_ids.update(
            ref.source_document_id
            for ref in editorial_enrichment_evidence_refs(editorial_enrichment)
        )
    additional_source_ids.update(figure.source_document_id for figure in figures)
    used_source_document_ids = set(narrative.used_source_document_ids)
    used_source_document_ids.update(
        ref.source_document_id for entry in source_timeline for ref in entry.evidence_refs
    )
    used_source_document_ids.update(additional_source_ids)
    sources = _project_publication_sources(
        references=references,
        used_source_document_ids=used_source_document_ids,
    )
    selected_timeline = select_publication_timeline_key_events_v3(source_timeline, sources)
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
        tables=tables,
        diagrams=diagrams,
        figures=figures,
        additional_source_ids=frozenset(additional_source_ids),
        charts=charts,
    )
    anchors = publication_document_text_anchors(document, references=ordered_references)
    semantic_text = SemanticTextV1(
        schema_version=SEMANTIC_ANNOTATION_SCHEMA_VERSION,
        policy_version=SEMANTIC_ANNOTATION_POLICY_VERSION,
        paragraphs=SemanticAnnotator().annotate_paragraphs(
            tuple(anchors.items()),
            entities=semantic_entities_from_extraction(extraction),
            proposals=(
                editorial_enrichment.annotations if editorial_enrichment is not None else ()
            ),
        ),
    )
    return (
        PublicationDocumentV5(
            document=document,
            semantic_text=semantic_text,
            references=ordered_references,
        ),
        anchor_map,
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
