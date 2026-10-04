"""Canonical QA of a publication against the exact inputs it projects."""

from __future__ import annotations

import re
import unicodedata
from typing import Any

from cti_app.application.publication_builder import (
    build_publication_document_v4,
    build_publication_document_v5,
)
from cti_app.application.typst_rendering import project_publication_to_typst_model
from cti_app.domain.production import ProductionInputSnapshot
from cti_app.domain.production_editorial_enrichment import EditorialEnrichmentV1
from cti_app.domain.production_extraction import ProductionExtractionV1
from cti_app.domain.production_references import ProductionReferenceCorpusV1
from cti_app.domain.production_relevance import RelevanceProjectionV1
from cti_app.domain.production_synthesis import ProductionSynthesisV1
from cti_app.domain.publication_document import (
    CanonicalPublicationDocument,
    PublicationDocumentV5,
    serialize_publication_document,
)

_LEGACY_CITATION = re.compile(r"\[S\d+\]", re.IGNORECASE)
_INTERNAL_DIAGNOSTIC = re.compile(
    r"\b(?:extraction_source_skipped|synthesis_[a-z0-9]+(?:_[a-z0-9]+)+)\b"
    r"|\bdiagnostics?\s*[:=]",
    re.IGNORECASE,
)
_PIPELINE_VOCABULARY_TERMS = (
    "pack fourni",
    "packs fournis",
    "éléments fournis",
    "base du pack",
    "base des packs",
    "pack de preuves",
    "packs de preuves",
    "evidence pack",
    "evidence packs",
    "éléments fournis par le pack",
    "éléments fournis par les packs",
    "evidence handle",
    "evidence handles",
)


def _fold_accents(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value.casefold())
    return "".join(character for character in decomposed if not unicodedata.combining(character))


def _semantic_annotation_has_full_occurrence_coverage(publication: PublicationDocumentV5) -> bool:
    """Every exact term styled once must remain styled at all exact occurrences."""
    paragraphs = publication.semantic_text.paragraphs
    terms = {
        span.text
        for paragraph in paragraphs
        for span in paragraph.spans
        if span.role.value != "text" and span.text
    }
    for term in terms:
        for paragraph in paragraphs:
            text = paragraph.text
            ranges: list[tuple[int, int]] = []
            cursor = 0
            for span in paragraph.spans:
                end = cursor + len(span.text)
                if span.role.value != "text":
                    ranges.append((cursor, end))
                cursor = end
            start = 0
            left_word = term[0].isalnum() or term[0] == "_"
            right_word = term[-1].isalnum() or term[-1] == "_"
            while True:
                start = text.find(term, start)
                if start < 0:
                    break
                end = start + len(term)
                if (
                    left_word
                    and start > 0
                    and (text[start - 1].isalnum() or text[start - 1] == "_")
                ) or (right_word and end < len(text) and (text[end].isalnum() or text[end] == "_")):
                    start += 1
                    continue
                covered_until = start
                for range_start, range_end in ranges:
                    if range_start <= covered_until < range_end:
                        covered_until = range_end
                        if covered_until >= end:
                            break
                if covered_until < end:
                    return False
                start += 1
    return True


_PIPELINE_VOCABULARY = re.compile(
    r"(?<!\w)(?:"
    + "|".join(re.escape(_fold_accents(term)) for term in _PIPELINE_VOCABULARY_TERMS)
    + r"|e[0-9]{3})(?!\w)",
    re.IGNORECASE,
)


def qa_publication_v5(
    *,
    snapshot: ProductionInputSnapshot,
    references: ProductionReferenceCorpusV1,
    extraction: ProductionExtractionV1,
    relevance_projection: RelevanceProjectionV1 | None = None,
    synthesis: ProductionSynthesisV1,
    editorial_enrichment: EditorialEnrichmentV1,
    publication: CanonicalPublicationDocument,
) -> dict[str, Any]:
    """Rebuild the pure projection and require byte-for-byte semantic equality."""
    checks: dict[str, bool] = {}
    errors: list[str] = []
    warnings: list[str] = []
    checks["subject_lineage"] = publication.subject_id == snapshot.subject_id
    if not checks["subject_lineage"]:
        errors.append("Publication subject differs from the frozen snapshot")
    checks["publication_language"] = (
        publication.publication_language == snapshot.publication_language
    )
    if not checks["publication_language"]:
        errors.append("Publication language differs from the frozen snapshot")
    if isinstance(publication, PublicationDocumentV5):
        checks["semantic_annotation_coverage"] = _semantic_annotation_has_full_occurrence_coverage(
            publication
        )
        if not checks["semantic_annotation_coverage"]:
            errors.append("A semantic annotation is missing from another exact occurrence")
        semantic_paragraphs = publication.semantic_text.paragraphs
        annotated_spans = sum(
            span.role.value != "text"
            for paragraph in semantic_paragraphs
            for span in paragraph.spans
        )
        document_characters = sum(len(paragraph.text) for paragraph in semantic_paragraphs)
        empty_long_paragraph = any(
            len(paragraph.text) >= 300
            and not any(span.role.value != "text" for span in paragraph.spans)
            for paragraph in semantic_paragraphs
        )
        if empty_long_paragraph or (
            document_characters > 0 and annotated_spans * 600 < document_characters
        ):
            warnings.append("semantic_annotation_sparse")
    else:
        checks["semantic_annotation_coverage"] = True
    try:
        expected_builder = (
            build_publication_document_v5
            if isinstance(publication, PublicationDocumentV5)
            else build_publication_document_v4
        )
        expected = expected_builder(
            snapshot=snapshot,
            references=references,
            extraction=extraction,
            relevance_projection=relevance_projection,
            synthesis=synthesis,
            editorial_enrichment=editorial_enrichment,
        )
    except ValueError as exc:
        checks["canonical_inputs_valid"] = False
        errors.append(str(exc))
    else:
        checks["canonical_inputs_valid"] = True
        checks["exact_projection"] = serialize_publication_document(
            publication
        ) == serialize_publication_document(expected)
        if not checks["exact_projection"]:
            errors.append("Publication differs from canonical Assembly projection")

    editorial_text = (
        publication.title,
        *(item.text for item in publication.lead),
        *(section.heading for section in publication.sections),
        *(item.text for section in publication.sections for item in section.paragraphs),
        *(item.text for item in publication.timeline),
        *(item.text for item in publication.uncertainties),
        *(
            text
            for source in publication.sources
            for text in (
                source.canonical_url,
                source.title,
                source.publisher,
                source.published_at.isoformat() if source.published_at is not None else None,
            )
            if text is not None
        ),
        *(
            text
            for group in publication.indicators
            for indicator in group.indicators
            for text in (indicator.value, indicator.normalized_value)
        ),
        *(
            text
            for table in publication.tables
            for text in (
                table.title,
                table.caption,
                *(column.label for column in table.columns),
                *(cell for row in table.rows for cell in row.cells),
            )
            if text is not None
        ),
        *(
            text
            for diagram in publication.diagrams
            for text in (
                diagram.title,
                diagram.caption,
                *(node.label for node in diagram.nodes),
                *(edge.label for edge in diagram.edges),
                *(group.label for group in diagram.groups),
            )
            if text is not None
        ),
        *(
            figure_text
            for figure in publication.figures
            for figure_text in (
                figure.caption,
                figure.provenance,
                figure.locator.section,
                figure.locator.figure_label,
                str(figure.locator.page) if figure.locator.page is not None else None,
            )
            if figure_text is not None
        ),
    )
    checks["no_legacy_citation"] = not any(_LEGACY_CITATION.search(text) for text in editorial_text)
    if not checks["no_legacy_citation"]:
        errors.append("Publication contains a legacy source marker")

    checks["no_internal_synthesis_headings"] = all(
        not section.heading.strip() for section in publication.sections
    )
    if not checks["no_internal_synthesis_headings"]:
        errors.append("Publication synthesis contains an internal section heading")

    checks["no_uncertainty_list"] = not publication.uncertainties
    if not checks["no_uncertainty_list"]:
        errors.append("Publication contains a separate uncertainty list")

    try:
        render_model = project_publication_to_typst_model(publication)
    except ValueError:
        checks["references_then_synthesis"] = False
    else:
        section_types = [section["type"] for section in render_model.content_sections]
        expected_types = ["references", "synthesis"]
        if any(group.indicators for group in publication.indicators):
            expected_types.append("technical_annex")
        checks["references_then_synthesis"] = section_types == expected_types
        synthesis_section = next(
            (
                section
                for section in render_model.content_sections
                if section["type"] == "synthesis"
            ),
            None,
        )
        synthesis_paragraphs = (
            [block["text"] for block in synthesis_section["blocks"] if block["type"] == "paragraph"]
            if synthesis_section is not None
            else []
        )
        lead_texts = [item.text for item in publication.lead]
        lead_fingerprints = [" ".join(text.casefold().split()) for text in lead_texts]
        paragraph_fingerprints = [
            " ".join(text.casefold().split()) for text in synthesis_paragraphs
        ]
        checks["lead_is_first_and_not_duplicated"] = paragraph_fingerprints[
            : len(lead_fingerprints)
        ] == lead_fingerprints and all(
            paragraph_fingerprints.count(text) == 1 for text in lead_fingerprints
        )
    if not checks["references_then_synthesis"]:
        errors.append("Publication sections are not ordered references then synthesis")
    if not checks.get("lead_is_first_and_not_duplicated", False):
        errors.append("Publication lead is not the first unique synthesis paragraph")

    checks["no_diagnostic_code_leaks"] = not any(
        _INTERNAL_DIAGNOSTIC.search(text) for text in editorial_text
    )
    if not checks["no_diagnostic_code_leaks"]:
        errors.append("Publication contains an internal diagnostic code")

    checks["no_pipeline_vocabulary_leaks"] = not any(
        _PIPELINE_VOCABULARY.search(_fold_accents(text)) for text in editorial_text
    )
    if not checks["no_pipeline_vocabulary_leaks"]:
        errors.append(
            "Publication failed no_pipeline_vocabulary_leaks: internal production "
            "vocabulary is present"
        )
    return {"passed": not errors, "checks": checks, "errors": errors, "warnings": warnings}


# Keep the old consumer name for frozen V4 documents. The validation function
# selects the matching strict contract from the supplied canonical document.
qa_publication_v4 = qa_publication_v5


class ProductionQAService:
    """QA boundary for the current canonical Production publication."""

    async def run_qa(
        self,
        *,
        snapshot: ProductionInputSnapshot,
        references: ProductionReferenceCorpusV1,
        extraction: ProductionExtractionV1,
        relevance_projection: RelevanceProjectionV1 | None = None,
        synthesis: ProductionSynthesisV1,
        editorial_enrichment: EditorialEnrichmentV1,
        publication: CanonicalPublicationDocument,
    ) -> dict[str, Any]:
        return qa_publication_v5(
            snapshot=snapshot,
            references=references,
            extraction=extraction,
            relevance_projection=relevance_projection,
            synthesis=synthesis,
            editorial_enrichment=editorial_enrichment,
            publication=publication,
        )
