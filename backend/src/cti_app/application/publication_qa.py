"""Canonical QA of a publication against the exact inputs it projects."""

from __future__ import annotations

import re
from typing import Any

from cti_app.application.publication_builder import build_publication_document_v4
from cti_app.application.typst_rendering import project_publication_to_typst_model
from cti_app.domain.production import ProductionInputSnapshot
from cti_app.domain.production_editorial_enrichment import EditorialEnrichmentV1
from cti_app.domain.production_extraction import ProductionExtractionV1
from cti_app.domain.production_references import ProductionReferenceCorpusV1
from cti_app.domain.production_relevance import RelevanceProjectionV1
from cti_app.domain.production_synthesis import ProductionSynthesisV1
from cti_app.domain.publication_document import (
    PublicationDocumentV4,
    serialize_publication_document,
)

_LEGACY_CITATION = re.compile(r"\[S\d+\]", re.IGNORECASE)
_INTERNAL_DIAGNOSTIC = re.compile(
    r"\b(?:extraction_source_skipped|synthesis_[a-z0-9]+(?:_[a-z0-9]+)+)\b"
    r"|\bdiagnostics?\s*[:=]",
    re.IGNORECASE,
)


def qa_publication_v4(
    *,
    snapshot: ProductionInputSnapshot,
    references: ProductionReferenceCorpusV1,
    extraction: ProductionExtractionV1,
    relevance_projection: RelevanceProjectionV1 | None = None,
    synthesis: ProductionSynthesisV1,
    editorial_enrichment: EditorialEnrichmentV1,
    publication: PublicationDocumentV4,
) -> dict[str, Any]:
    """Rebuild the pure projection and require byte-for-byte semantic equality."""
    checks: dict[str, bool] = {}
    errors: list[str] = []
    checks["subject_lineage"] = publication.subject_id == snapshot.subject_id
    if not checks["subject_lineage"]:
        errors.append("Publication subject differs from the frozen snapshot")
    checks["publication_language"] = (
        publication.publication_language == snapshot.publication_language
    )
    if not checks["publication_language"]:
        errors.append("Publication language differs from the frozen snapshot")
    try:
        expected = build_publication_document_v4(
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
    return {"passed": not errors, "checks": checks, "errors": errors, "warnings": []}


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
        publication: PublicationDocumentV4,
    ) -> dict[str, Any]:
        return qa_publication_v4(
            snapshot=snapshot,
            references=references,
            extraction=extraction,
            relevance_projection=relevance_projection,
            synthesis=synthesis,
            editorial_enrichment=editorial_enrichment,
            publication=publication,
        )
