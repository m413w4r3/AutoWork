"""Build the canonical publication document from verified production artifacts."""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from uuid import UUID

from cti_app.application.french_typography import apply_french_spacing
from cti_app.application.production_extraction import references_corpus_hash
from cti_app.application.production_normalization import normalize_indicator_value
from cti_app.application.production_parsers import (
    ReferenceReport,
    SemanticType,
    TechnicalExtraction,
)
from cti_app.application.production_rendering import collect_indicators
from cti_app.application.production_synthesis import canonical_extraction_hash
from cti_app.application.semantic_annotation import SemanticAnnotator
from cti_app.domain.production import ProductionInputSnapshot
from cti_app.domain.production_extraction import (
    ExtractionIndicatorStatus,
    ProductionExtractionV1,
)
from cti_app.domain.production_references import (
    ProductionReferenceCorpusV1,
    ProductionReferenceSourceV1,
)
from cti_app.domain.production_synthesis import (
    ExtractionEvidenceRefV1,
    ProductionSynthesisV1,
    SynthesisParagraphV1,
    extraction_evidence_refs_v1,
    synthesis_evidence_refs,
)
from cti_app.domain.publication import (
    PUBLICATION_IOC_ARTIFACT_TYPES,
    PUBLICATION_SCHEMA_VERSION,
    ArtifactType,
    Indicator,
    IndicatorGroup,
    PublicationDocumentV2,
    PublicationDocumentV3,
    PublicationEvidenceKind,
    PublicationEvidenceRefV1,
    PublicationIndicatorGroupV1,
    PublicationIndicatorV1,
    PublicationParagraphV1,
    PublicationSectionKind,
    PublicationSectionV1,
    PublicationSource,
    PublicationSourceV1,
    PublicationTimelineEntryV1,
    PublicationUncertaintyV1,
    RichSpan,
    RichSpanKind,
    RichText,
    TimelineEntry,
)

_VALID_TITLE = re.compile(r"^\[[^\]]+\]\s+.+")
_CITATION_SEPARATOR = re.compile(r"^[\s,;:.·]+$")


@dataclass(frozen=True, slots=True)
class _PublicationNarrativeProjection:
    lead: tuple[PublicationParagraphV1, ...]
    sections: tuple[PublicationSectionV1, ...]
    timeline: tuple[PublicationTimelineEntryV1, ...]
    uncertainties: tuple[PublicationUncertaintyV1, ...]
    used_source_document_ids: frozenset[UUID]


@dataclass(frozen=True, slots=True)
class _PublicationContentProjection:
    lead: tuple[PublicationParagraphV1, ...]
    sections: tuple[PublicationSectionV1, ...]
    timeline: tuple[PublicationTimelineEntryV1, ...]
    uncertainties: tuple[PublicationUncertaintyV1, ...]
    indicators: tuple[PublicationIndicatorGroupV1, ...]
    used_source_document_ids: frozenset[UUID]


def _project_synthesis_publication(
    *, extraction: ProductionExtractionV1, synthesis: ProductionSynthesisV1
) -> _PublicationNarrativeProjection:
    """Project validated canonical synthesis narrative without changing editorial order."""
    _validate_synthesis_evidence_refs(extraction=extraction, synthesis=synthesis)
    used_source_document_ids: set[UUID] = set()

    def evidence_refs(
        refs: tuple[ExtractionEvidenceRefV1, ...],
    ) -> tuple[PublicationEvidenceRefV1, ...]:
        projected = tuple(
            PublicationEvidenceRefV1(
                source_document_id=ref.source_document_id,
                kind=PublicationEvidenceKind(ref.kind.value),
                evidence_key=ref.evidence_key,
            )
            for ref in refs
        )
        used_source_document_ids.update(ref.source_document_id for ref in projected)
        return projected

    def paragraph(value: SynthesisParagraphV1) -> PublicationParagraphV1:
        return PublicationParagraphV1(
            text=value.text,
            evidence_refs=evidence_refs(value.evidence_refs),
        )

    lead = tuple(paragraph(value) for value in synthesis.lead)
    sections = tuple(
        PublicationSectionV1(
            kind=PublicationSectionKind(section.kind.value),
            heading=section.heading,
            paragraphs=tuple(paragraph(value) for value in section.paragraphs),
        )
        for section in synthesis.sections
    )
    timeline = tuple(
        PublicationTimelineEntryV1(
            event_date=entry.event_date,
            date_text=entry.date_text,
            text=entry.text,
            evidence_refs=evidence_refs(entry.evidence_refs),
        )
        for entry in synthesis.timeline
    )
    uncertainties = tuple(
        PublicationUncertaintyV1(
            text=uncertainty.text,
            source_document_ids=uncertainty.source_document_ids,
        )
        for uncertainty in synthesis.uncertainties
    )
    used_source_document_ids.update(
        source_document_id
        for uncertainty in uncertainties
        for source_document_id in uncertainty.source_document_ids
    )
    return _PublicationNarrativeProjection(
        lead=lead,
        sections=sections,
        timeline=timeline,
        uncertainties=uncertainties,
        used_source_document_ids=frozenset(used_source_document_ids),
    )


def _project_publication_iocs(
    *, extraction: ProductionExtractionV1, narrative: _PublicationNarrativeProjection
) -> _PublicationContentProjection:
    """Add confirmed canonical IOCs to the narrative projection."""
    occurrences: dict[tuple[ArtifactType, str], tuple[set[str], set[UUID]]] = {}
    for source in extraction.sources:
        for item in source.indicators:
            if (
                item.indicator_status is not ExtractionIndicatorStatus.CONFIRMED_IOC
                or item.artifact_type not in PUBLICATION_IOC_ARTIFACT_TYPES
            ):
                continue
            try:
                normalized_value = normalize_indicator_value(item.value, item.artifact_type)
            except ValueError:
                continue
            identity = (item.artifact_type, normalized_value)
            values, source_document_ids = occurrences.setdefault(identity, (set(), set()))
            values.add(item.value)
            source_document_ids.update(item.source_document_ids)

    grouped: dict[ArtifactType, list[PublicationIndicatorV1]] = defaultdict(list)
    used_source_document_ids = set(narrative.used_source_document_ids)
    for (artifact_type, normalized_value), (values, source_document_ids) in sorted(
        occurrences.items(),
        key=lambda item: (item[0][0].value, item[0][1]),
    ):
        ordered_source_ids = tuple(sorted(source_document_ids, key=str))
        used_source_document_ids.update(ordered_source_ids)
        grouped[artifact_type].append(
            PublicationIndicatorV1(
                value=min(values),
                normalized_value=normalized_value,
                artifact_type=artifact_type,
                source_document_ids=ordered_source_ids,
            )
        )

    indicators = tuple(
        PublicationIndicatorGroupV1(artifact_type, tuple(grouped[artifact_type]))
        for artifact_type in sorted(grouped, key=lambda value: value.value)
    )
    return _PublicationContentProjection(
        lead=narrative.lead,
        sections=narrative.sections,
        timeline=narrative.timeline,
        uncertainties=narrative.uncertainties,
        indicators=indicators,
        used_source_document_ids=frozenset(used_source_document_ids),
    )


def _validate_publication_v3_lineage(
    *,
    snapshot: ProductionInputSnapshot,
    references: ProductionReferenceCorpusV1,
    extraction: ProductionExtractionV1,
    synthesis: ProductionSynthesisV1,
) -> None:
    """Require all canonical artifacts to belong to the same frozen inputs."""
    if (
        len(
            {
                snapshot.subject_id,
                references.subject_id,
                extraction.subject_id,
                synthesis.subject_id,
            }
        )
        != 1
    ):
        raise ValueError("Publication artifacts must share the same subject identity")
    if extraction.production_input_hash != snapshot.input_hash:
        raise ValueError("Extraction does not match the production input snapshot")
    if references_corpus_hash(references) != extraction.references_corpus_hash:
        raise ValueError("Extraction does not match the canonical references corpus")
    if synthesis.production_input_hash != snapshot.input_hash:
        raise ValueError("Synthesis does not match the production input snapshot")
    if synthesis.extraction_hash != canonical_extraction_hash(extraction):
        raise ValueError("Synthesis does not match the canonical extraction")


def _validate_synthesis_evidence_refs(
    *, extraction: ProductionExtractionV1, synthesis: ProductionSynthesisV1
) -> None:
    """Require each synthesis citation to identify current extraction evidence."""
    current_identities = {
        (ref.source_document_id, ref.kind.value, ref.evidence_key)
        for ref in extraction_evidence_refs_v1(extraction)
    }
    for ref in synthesis_evidence_refs(synthesis):
        identity = (ref.source_document_id, ref.kind.value, ref.evidence_key)
        if identity not in current_identities:
            raise ValueError("Synthesis evidence reference is absent from the current extraction")


def _normalize_title(title: str, extraction: TechnicalExtraction) -> str:
    cleaned = " ".join(title.split())
    if _VALID_TITLE.fullmatch(cleaned):
        return cleaned
    actor = next(
        (
            item.value
            for item in extraction.items
            if item.supported and item.semantic_type is SemanticType.ACTOR
        ),
        "Publication",
    )
    return f"[{actor}] {cleaned}"


def _merge_citations(spans: RichText) -> RichText:
    """Merge adjacent citation markers while retaining first-use source order."""
    output: list[RichSpan] = []
    index = 0
    while index < len(spans):
        span = spans[index]
        if span.kind is not RichSpanKind.CITATION:
            output.append(span)
            index += 1
            continue
        source_ids = list(span.source_ids)
        cursor = index + 1
        while cursor < len(spans):
            candidate = spans[cursor]
            separator: RichSpan | None = None
            if (
                candidate.kind is RichSpanKind.TEXT
                and _CITATION_SEPARATOR.fullmatch(candidate.text)
                and cursor + 1 < len(spans)
                and spans[cursor + 1].kind is RichSpanKind.CITATION
            ):
                separator = candidate
                candidate = spans[cursor + 1]
            if candidate.kind is not RichSpanKind.CITATION:
                break
            source_ids.extend(candidate.source_ids)
            cursor += 2 if separator is not None else 1
        if output and output[-1].kind is RichSpanKind.TEXT:
            previous = output[-1]
            output[-1] = RichSpan(previous.kind, previous.text.rstrip(), previous.source_ids)
        output.append(RichSpan(RichSpanKind.CITATION, "", tuple(dict.fromkeys(source_ids))))
        index = cursor
    return tuple(output)


def _with_event_citation(spans: RichText, source_ids: tuple[str, ...]) -> RichText:
    """Fold the event-level sources into the paragraph's last citation marker.

    A timeline paragraph must never emit two adjacent footnotes, so the event
    sources join the existing citation instead of adding a second one.
    """
    merged = list(spans)
    for index in range(len(merged) - 1, -1, -1):
        if merged[index].kind is RichSpanKind.CITATION:
            combined = (*merged[index].source_ids, *source_ids)
            merged[index] = RichSpan(RichSpanKind.CITATION, "", tuple(dict.fromkeys(combined)))
            return tuple(merged)
    merged.append(RichSpan(RichSpanKind.CITATION, "", tuple(dict.fromkeys(source_ids))))
    return tuple(merged)


def build_publication_document_v3(
    *,
    snapshot: ProductionInputSnapshot,
    references: ProductionReferenceCorpusV1,
    extraction: ProductionExtractionV1,
    synthesis: ProductionSynthesisV1,
) -> PublicationDocumentV3:
    """Build a renderer-independent publication from canonical production inputs."""
    _validate_publication_v3_lineage(
        snapshot=snapshot,
        references=references,
        extraction=extraction,
        synthesis=synthesis,
    )
    narrative = _project_synthesis_publication(extraction=extraction, synthesis=synthesis)
    projection = _project_publication_iocs(extraction=extraction, narrative=narrative)

    sources_by_id: dict[UUID, ProductionReferenceSourceV1] = {}
    for source in references.sources:
        source_document_id = source.source_document_id
        if source_document_id is None:
            continue
        if source_document_id in sources_by_id:
            raise ValueError(
                f"Canonical reference corpus repeats source_document_id {source_document_id}"
            )
        sources_by_id[source_document_id] = source

    publication_sources: list[PublicationSourceV1] = []
    for source_document_id in sorted(projection.used_source_document_ids, key=str):
        resolved_source = sources_by_id.get(source_document_id)
        if resolved_source is None:
            raise ValueError(
                "Used publication source is absent from the canonical reference corpus: "
                f"{source_document_id}"
            )
        publication_sources.append(
            PublicationSourceV1(
                source_document_id=source_document_id,
                canonical_url=resolved_source.canonical_url,
                title=resolved_source.title,
                publisher=resolved_source.publisher,
                published_at=resolved_source.published_at,
                tier=resolved_source.tier,
                kind=resolved_source.kind,
                role=resolved_source.role,
            )
        )

    return PublicationDocumentV3(
        schema_version="3",
        subject_id=snapshot.subject_id,
        publication_language=synthesis.publication_language,
        title=synthesis.title,
        lead=projection.lead,
        sections=projection.sections,
        timeline=projection.timeline,
        indicators=projection.indicators,
        sources=tuple(publication_sources),
        uncertainties=projection.uncertainties,
    )


def build_publication_document(
    *,
    subject_title: str,
    report: ReferenceReport,
    extraction: TechnicalExtraction,
    synthesis_text: str,
    annotator: SemanticAnnotator | None = None,
) -> PublicationDocumentV2:
    """Build a deterministic, fully serializable V2 publication document."""
    annotator = annotator or SemanticAnnotator()
    known_sources = report.source_ids()

    def annotate(text: str) -> RichText:
        spans = _merge_citations(annotator.annotate(apply_french_spacing(text), extraction))
        unknown = {
            source_id
            for span in spans
            if span.kind is RichSpanKind.CITATION
            for source_id in span.source_ids
            if source_id not in known_sources
        }
        if unknown:
            raise ValueError(f"Unknown publication source: {','.join(sorted(unknown))}")
        return spans

    timeline = tuple(
        TimelineEntry(
            date=event.event_date,
            content=_with_event_citation(annotate(event.text), event.source_ids),
            source_ids=event.source_ids,
        )
        for event in report.events
    )
    paragraphs = tuple(
        annotate(paragraph.strip())
        for paragraph in re.split(r"\n\s*\n", synthesis_text.strip())
        if paragraph.strip()
    )

    grouped: dict[ArtifactType, list[Indicator]] = defaultdict(list)
    for item in collect_indicators(extraction):
        assert item.artifact_type is not None
        artifact_type = (
            item.artifact_type
            if isinstance(item.artifact_type, ArtifactType)
            else ArtifactType(item.artifact_type)
        )
        try:
            normalized = item.normalized_value or normalize_indicator_value(
                item.value, artifact_type
            )
        except ValueError:
            continue
        grouped[artifact_type].append(
            Indicator(
                value=item.value,
                normalized_value=normalized,
                artifact_type=artifact_type,
                source_ids=item.source_ids,
            )
        )

    title = report.editorial_title or subject_title
    return PublicationDocumentV2(
        schema_version=PUBLICATION_SCHEMA_VERSION,
        title=_normalize_title(title, extraction),
        timeline=timeline,
        synthesis=paragraphs,
        indicators=tuple(
            IndicatorGroup(artifact_type=artifact_type, values=tuple(values))
            for artifact_type, values in grouped.items()
        ),
        sources=tuple(
            PublicationSource(source.local_id, source.canonical_url) for source in report.sources
        ),
        uncertainties=tuple(report.uncertainties) + tuple(extraction.uncertainties),
    )
