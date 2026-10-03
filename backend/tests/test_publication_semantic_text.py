from __future__ import annotations

from uuid import uuid4

import pytest

from cti_app.domain.publication_document import (
    PUBLICATION_DOCUMENT_V4_SCHEMA_VERSION,
    PublicationDocumentV4,
    PublicationDocumentV5,
    parse_publication_document,
    publication_document_v4_to_json,
    serialize_publication_document,
)
from cti_app.domain.semantic_annotation import (
    SEMANTIC_ANNOTATION_POLICY_VERSION,
    SEMANTIC_ANNOTATION_SCHEMA_VERSION,
    SemanticParagraphV1,
    SemanticRole,
    SemanticTextSpanV1,
    SemanticTextV1,
)


def _base_document() -> PublicationDocumentV4:
    return PublicationDocumentV4(
        schema_version=PUBLICATION_DOCUMENT_V4_SCHEMA_VERSION,
        subject_id=uuid4(),
        publication_language="fr",
        title="Mandiant published the report.",
        lead=(),
        sections=(),
        timeline=(),
        indicators=(),
        sources=(),
        uncertainties=(),
        tables=(),
        diagrams=(),
        figures=(),
    )


def _v5_document() -> PublicationDocumentV5:
    base = _base_document()
    text = base.title
    first = "Mandiant"
    semantic = SemanticParagraphV1(
        anchor="title",
        spans=(
            SemanticTextSpanV1(SemanticRole.ACTOR, first),
            SemanticTextSpanV1(SemanticRole.TEXT, text[len(first) :]),
        ),
    )
    return PublicationDocumentV5(
        document=base,
        semantic_text=SemanticTextV1(
            schema_version=SEMANTIC_ANNOTATION_SCHEMA_VERSION,
            policy_version=SEMANTIC_ANNOTATION_POLICY_VERSION,
            paragraphs=(semantic,),
        ),
    )


def test_v5_persists_full_coverage_semantics_without_changing_text() -> None:
    document = _v5_document()
    payload = serialize_publication_document(document)
    parsed = parse_publication_document(payload)

    assert isinstance(parsed, PublicationDocumentV5)
    assert parsed == document
    title_span = parsed.semantic_text.paragraphs[0]
    assert title_span.text == parsed.document.title
    assert payload["schema_version"] == "5"
    assert payload["rich_text"]["policy_version"] == SEMANTIC_ANNOTATION_POLICY_VERSION
    assert "rich_text" not in publication_document_v4_to_json(_base_document())


def test_v5_rejects_spans_that_do_not_reconstruct_the_canonical_text() -> None:
    base = _base_document()
    bad_text = SemanticTextV1(
        schema_version=SEMANTIC_ANNOTATION_SCHEMA_VERSION,
        policy_version=SEMANTIC_ANNOTATION_POLICY_VERSION,
        paragraphs=(
            SemanticParagraphV1(
                anchor="title",
                spans=(SemanticTextSpanV1(SemanticRole.ACTOR, "different text"),),
            ),
        ),
    )

    with pytest.raises(ValueError, match="preserve every text field"):
        PublicationDocumentV5(document=base, semantic_text=bad_text)


def test_v4_remains_strict_and_rejects_v5_fields() -> None:
    payload = publication_document_v4_to_json(_base_document())

    with pytest.raises(ValueError, match="fields are invalid"):
        PublicationDocumentV4._from_json({**payload, "rich_text": {}})
