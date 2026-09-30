"""Canonical QA of a publication against the exact inputs it projects."""

from __future__ import annotations

import re
from typing import Any

from cti_app.application.publication_builder import build_publication_document_v3
from cti_app.domain.production import ProductionInputSnapshot
from cti_app.domain.production_extraction import ProductionExtractionV1
from cti_app.domain.production_references import ProductionReferenceCorpusV1
from cti_app.domain.production_synthesis import ProductionSynthesisV1
from cti_app.domain.publication import PublicationDocumentV3
from cti_app.domain.publication_document import serialize_publication_document

_LEGACY_CITATION = re.compile(r"\[S\d+\]", re.IGNORECASE)


def qa_publication_v3(
    *,
    snapshot: ProductionInputSnapshot,
    references: ProductionReferenceCorpusV1,
    extraction: ProductionExtractionV1,
    synthesis: ProductionSynthesisV1,
    publication: PublicationDocumentV3,
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
        expected = build_publication_document_v3(
            snapshot=snapshot,
            references=references,
            extraction=extraction,
            synthesis=synthesis,
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
    )
    checks["no_legacy_citation"] = not any(_LEGACY_CITATION.search(text) for text in editorial_text)
    if not checks["no_legacy_citation"]:
        errors.append("Publication contains a legacy source marker")
    return {"passed": not errors, "checks": checks, "errors": errors, "warnings": []}


class ProductionQAService:
    """QA boundary for the current canonical Production publication."""

    async def run_qa(
        self,
        *,
        snapshot: ProductionInputSnapshot,
        references: ProductionReferenceCorpusV1,
        extraction: ProductionExtractionV1,
        synthesis: ProductionSynthesisV1,
        publication: PublicationDocumentV3,
    ) -> dict[str, Any]:
        return qa_publication_v3(
            snapshot=snapshot,
            references=references,
            extraction=extraction,
            synthesis=synthesis,
            publication=publication,
        )
