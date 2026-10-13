"""Canonical QA of a publication against the exact inputs it projects."""

from __future__ import annotations

import re
import unicodedata
from enum import StrEnum
from typing import Any

from cti_app.application.publication_builder import (
    build_publication_document_v4,
    build_publication_document_v5,
)
from cti_app.application.typst_rendering import project_publication_to_typst_model
from cti_app.domain.production import ProductionInputSnapshot
from cti_app.domain.production_editorial_enrichment import EditorialEnrichmentV1
from cti_app.domain.production_extraction import ProductionExtractionV1
from cti_app.domain.production_references import (
    ProductionReferenceCorpusV1,
    production_source_criticality,
)
from cti_app.domain.production_relevance import RelevanceProjectionV1
from cti_app.domain.production_synthesis import (
    ProductionSynthesisV1,
    is_valid_editorial_title,
    validate_synthesis_section_headings,
)
from cti_app.domain.publication_document import (
    CanonicalPublicationDocument,
    PublicationDocumentV5,
    PublicationDocumentV6,
    PublicationDocumentV6ValidationError,
    serialize_publication_document,
)
from cti_app.domain.semantic_annotation import (
    exact_word_occurrences,
    semantic_policy_requires_full_occurrence_coverage,
    semantic_ranges_cover_occurrence,
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


class PublicationQaErrorCode(StrEnum):
    DOCUMENT_NOT_V6 = "publication_qa_document_not_v6"
    V6_INVARIANT_INVALID = "publication_qa_v6_invariant_invalid"
    BASE_PROJECTION_MISMATCH = "publication_qa_v6_base_projection_mismatch"
    SECTION_ORDER_INVALID = "publication_qa_section_order_invalid"
    PUBLICATION_QA_FAILED = "publication_qa_failed"


class PublicationQaValidationError(ValueError):
    """Typed error for callers that invoke the V6-specific QA boundary incorrectly."""

    def __init__(self, code: PublicationQaErrorCode) -> None:
        self.code = code
        super().__init__(code.value)


def _fold_accents(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value.casefold())
    return "".join(character for character in decomposed if not unicodedata.combining(character))


def _semantic_annotation_has_full_occurrence_coverage(
    publication: PublicationDocumentV5,
) -> bool:
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
            semantic_ranges = tuple(ranges)
            for start, end in exact_word_occurrences(text, term):
                if not semantic_ranges_cover_occurrence(start, end, semantic_ranges):
                    return False
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
    v6 = publication if isinstance(publication, PublicationDocumentV6) else None
    base_publication = v6.base_publication if v6 is not None else publication
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
    checks["title_format"] = not isinstance(
        base_publication, PublicationDocumentV5
    ) or is_valid_editorial_title(base_publication.title)
    if not checks["title_format"]:
        errors.append("Publication title does not match the [Groupe] Titre format")
    if isinstance(base_publication, PublicationDocumentV5):
        if semantic_policy_requires_full_occurrence_coverage(
            base_publication.semantic_text.policy_version
        ):
            checks["semantic_annotation_coverage"] = (
                _semantic_annotation_has_full_occurrence_coverage(base_publication)
            )
            if not checks["semantic_annotation_coverage"]:
                errors.append("A semantic annotation is missing from another exact occurrence")
        else:
            checks["semantic_annotation_coverage"] = True
        semantic_paragraphs = base_publication.semantic_text.paragraphs
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
            if isinstance(base_publication, PublicationDocumentV5)
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
            base_publication
        ) == serialize_publication_document(expected)
        if not checks["exact_projection"]:
            errors.append("Publication differs from canonical Assembly projection")

    checks["source_provenance"] = True
    reference_sources_by_id: dict[Any, list[Any]] = {}
    for source in references.sources:
        if source.source_document_id is not None:
            reference_sources_by_id.setdefault(source.source_document_id, []).append(source)
    for publication_source in publication.sources:
        if publication_source.editorial_role is None:
            continue
        matches = reference_sources_by_id.get(publication_source.source_document_id, [])
        if not any(
            publication_source.editorial_role is source.editorial_role
            and publication_source.source_collection_id == source.source_collection_id
            and publication_source.content_sha256 == source.content_sha256
            and (
                publication_source.source_criticality,
                publication_source.criticality_reason,
            )
            == production_source_criticality(source, sources=references.sources)
            for source in matches
        ):
            checks["source_provenance"] = False
            errors.append(
                "Publication source role, criticality, or archive provenance differs "
                "from the canonical reference corpus"
            )

    v5 = base_publication if isinstance(base_publication, PublicationDocumentV5) else None
    reference_texts = tuple(item.text for item in v5.references) if v5 else ()
    original_ioc_texts = (
        tuple(
            text
            for group in v5.original_indicators
            for indicator in group.indicators
            for text in (indicator.value, indicator.normalized_value)
        )
        if v5
        else ()
    )
    editorial_text = (
        publication.title,
        *(item.text for item in publication.lead),
        *(section.heading for section in publication.sections),
        *(item.text for section in publication.sections for item in section.paragraphs),
        *(item.text for item in publication.timeline),
        *reference_texts,
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
        *original_ioc_texts,
        *(
            block.text
            for block in (v6.rendered_opinion.blocks if v6 and v6.rendered_opinion else ())
        ),
        *(v6.rendered_opinion.unresolved_points if v6 and v6.rendered_opinion else ()),
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

    try:
        validate_synthesis_section_headings(
            (
                section.heading,
                (paragraph.text for paragraph in section.paragraphs),
            )
            for section in publication.sections
        )
    except ValueError:
        checks["valid_synthesis_section_headings"] = False
    else:
        checks["valid_synthesis_section_headings"] = True
    if not checks["valid_synthesis_section_headings"]:
        errors.append("Publication synthesis contains an invalid section heading")

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
        if v6 is not None and v6.rendered_opinion is not None:
            expected_types.append("analyst_opinion")
        if publication.indicators or (v5 and v5.original_indicators):
            expected_types.append("technical_annex")
        checks["references_then_synthesis"] = section_types == expected_types
        annex = next(
            (
                section
                for section in render_model.content_sections
                if section["type"] == "technical_annex"
            ),
            None,
        )
        rendered_main = {
            str(item["text"]).casefold()
            for group in (annex or {}).get("indicators", {}).values()
            for item in group
        }
        rendered_original = {
            str(item["text"]).casefold()
            for group in (annex or {}).get("original_indicators", {}).values()
            for item in group
        }
        expected_main = {
            indicator.normalized_value.casefold()
            for group in publication.indicators
            for indicator in group.indicators
        }
        expected_original = {
            indicator.normalized_value.casefold()
            for group in (v5.original_indicators if v5 else ())
            for indicator in group.indicators
        }
        checks["ioc_groups_separated"] = (
            rendered_main == expected_main
            and rendered_original == expected_original
            and not rendered_main.intersection(rendered_original)
            and (
                not expected_original
                or "pas démontré" in str((annex or {}).get("original_indicator_note", ""))
            )
        )
        if not checks["ioc_groups_separated"]:
            errors.append("Subject IOCs and unlinked original IOCs are not clearly separated")
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
        errors.append(
            "Publication sections are not ordered references, synthesis, analyst opinion, "
            "then technical annex"
            if v6 is not None
            else "Publication sections are not ordered references then synthesis"
        )
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
    result: dict[str, Any] = {
        "passed": not errors,
        "checks": checks,
        "errors": errors,
        "warnings": warnings,
    }
    if v6 is not None:
        try:
            serialize_publication_document(v6)
        except PublicationDocumentV6ValidationError:
            checks["analyst_opinion_invariants"] = False
            errors.append("PublicationDocumentV6 opinion or provenance invariants are invalid")
        else:
            checks["analyst_opinion_invariants"] = True
        error_codes: list[str] = []
        if not checks["analyst_opinion_invariants"]:
            error_codes.append(PublicationQaErrorCode.V6_INVARIANT_INVALID.value)
        if not checks["exact_projection"]:
            error_codes.append(PublicationQaErrorCode.BASE_PROJECTION_MISMATCH.value)
        if not checks["references_then_synthesis"]:
            error_codes.append(PublicationQaErrorCode.SECTION_ORDER_INVALID.value)
        if errors and not error_codes:
            error_codes.append(PublicationQaErrorCode.PUBLICATION_QA_FAILED.value)
        result["passed"] = not errors
        result["error_codes"] = error_codes
    return result


def qa_publication_v6(**kwargs: Any) -> dict[str, Any]:
    publication = kwargs.get("publication")
    if not isinstance(publication, PublicationDocumentV6):
        raise PublicationQaValidationError(PublicationQaErrorCode.DOCUMENT_NOT_V6)
    return qa_publication_v5(**kwargs)


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
