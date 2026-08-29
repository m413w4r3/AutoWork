"""Build the canonical publication document from verified production artifacts."""

from __future__ import annotations

import re
from collections import defaultdict

from cti_app.application.french_typography import apply_french_spacing
from cti_app.application.production_normalization import normalize_indicator_value
from cti_app.application.production_parsers import (
    ReferenceReport,
    SemanticType,
    TechnicalExtraction,
)
from cti_app.application.production_rendering import collect_indicators
from cti_app.application.semantic_annotation import SemanticAnnotator
from cti_app.domain.publication import (
    PUBLICATION_SCHEMA_VERSION,
    ArtifactType,
    Indicator,
    IndicatorGroup,
    PublicationDocumentV2,
    PublicationSource,
    RichSpan,
    RichSpanKind,
    RichText,
    TimelineEntry,
)

_VALID_TITLE = re.compile(r"^\[[^\]]+\]\s+.+")
_CITATION_SEPARATOR = re.compile(r"^[\s,;:.·]+$")


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
            content=(
                *annotate(event.text),
                RichSpan(
                    RichSpanKind.CITATION,
                    "",
                    tuple(dict.fromkeys(event.source_ids)),
                ),
            ),
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
