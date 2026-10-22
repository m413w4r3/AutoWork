from __future__ import annotations

import pytest

from cti_app.application.publication_augmentation import augment_publication_with_opinion
from cti_app.application.publication_builder import build_publication_document_v5
from cti_app.application.publication_qa import (
    PublicationQaErrorCode,
    PublicationQaValidationError,
    qa_publication_v5,
    qa_publication_v6,
)
from cti_app.application.typst_rendering import project_publication_to_typst_model
from tests.test_publication_builder_v4 import _canonical_inputs, _enrichment_with
from tests.test_publication_document_v6 import _approved_opinion, _base_v5


def _inputs() -> dict[str, object]:
    snapshot, references, extraction, synthesis = _canonical_inputs()
    enrichment = _enrichment_with(extraction=extraction, synthesis=synthesis)
    return {
        "snapshot": snapshot,
        "references": references,
        "extraction": extraction,
        "synthesis": synthesis,
        "editorial_enrichment": enrichment,
    }


def test_qa_v6_keeps_v5_projection_and_checks_conditional_section_order() -> None:
    inputs = _inputs()
    base = build_publication_document_v5(**inputs)  # type: ignore[arg-type]
    opinion, context = _approved_opinion(base)
    publication = augment_publication_with_opinion(base, opinion, context)

    result = qa_publication_v6(publication=publication, **inputs)
    content_sections = project_publication_to_typst_model(publication).content_sections

    assert result["passed"] is True
    assert result["checks"]["exact_projection"] is True
    assert result["checks"]["analyst_opinion_invariants"] is True
    assert result["error_codes"] == []
    assert [section["type"] for section in content_sections] == [
        "references",
        "synthesis",
        "analyst_opinion",
        "technical_annex",
    ]


def test_qa_v5_result_shape_and_projection_remain_unchanged() -> None:
    inputs = _inputs()
    base = _base_v5()

    result = qa_publication_v5(publication=base, **inputs)  # type: ignore[arg-type]

    assert result["passed"] is True
    assert "error_codes" not in result
    assert [
        section["type"] for section in project_publication_to_typst_model(base).content_sections
    ] == ["references", "synthesis"]


def test_v6_specific_qa_rejects_a_v5_document_with_typed_code() -> None:
    with pytest.raises(PublicationQaValidationError) as caught:
        qa_publication_v6(publication=_base_v5())
    assert caught.value.code is PublicationQaErrorCode.DOCUMENT_NOT_V6
