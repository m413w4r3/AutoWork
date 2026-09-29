from datetime import date, datetime
from uuid import UUID

import pytest

from cti_app.domain.production_synthesis import EvidenceKind, SynthesisSectionKind
from cti_app.domain.publication import (
    PublicationEvidenceKind,
    PublicationEvidenceRefV1,
    PublicationParagraphV1,
    PublicationSectionKind,
    PublicationSectionV1,
    PublicationTimelineEntryV1,
)


def _ref(
    source_document_id: str,
    kind: PublicationEvidenceKind = PublicationEvidenceKind.FACT,
    evidence_key: str = "a" * 64,
) -> PublicationEvidenceRefV1:
    return PublicationEvidenceRefV1(UUID(source_document_id), kind, evidence_key)


def test_publication_enums_keep_synthesis_wire_values() -> None:
    assert tuple(kind.value for kind in PublicationEvidenceKind) == tuple(
        kind.value for kind in EvidenceKind
    )
    assert tuple(kind.value for kind in PublicationSectionKind) == tuple(
        kind.value for kind in SynthesisSectionKind
    )


def test_evidence_ref_requires_uuid_typed_kind_and_lowercase_sha256() -> None:
    assert _ref("00000000-0000-0000-0000-000000000001").evidence_key == "a" * 64

    for identity, kind, key in (
        ("not-a-uuid", PublicationEvidenceKind.FACT, "a" * 64),
        (UUID(int=1), "fact", "a" * 64),
        (UUID(int=1), PublicationEvidenceKind.FACT, "A" * 64),
        (UUID(int=1), PublicationEvidenceKind.FACT, "a" * 63),
        (UUID(int=1), PublicationEvidenceKind.FACT, None),
    ):
        with pytest.raises(ValueError):
            PublicationEvidenceRefV1(identity, kind, key)  # type: ignore[arg-type]


def test_evidence_refs_reject_duplicates_and_sort_deterministically() -> None:
    first = _ref("00000000-0000-0000-0000-000000000001", evidence_key="b" * 64)
    second = _ref("00000000-0000-0000-0000-000000000001", evidence_key="a" * 64)
    event = _ref(
        "00000000-0000-0000-0000-000000000001",
        PublicationEvidenceKind.EVENT,
        "f" * 64,
    )
    rule = _ref(
        "00000000-0000-0000-0000-000000000001",
        PublicationEvidenceKind.RULE,
        "0" * 64,
    )
    third = _ref("00000000-0000-0000-0000-000000000002")

    paragraph = PublicationParagraphV1("Finding", (third, rule, first, event, second))
    assert paragraph.evidence_refs == (event, second, first, rule, third)
    with pytest.raises(ValueError, match="repeat"):
        PublicationParagraphV1("Finding", (first, first))


def test_paragraph_and_section_require_content_and_preserve_paragraph_order() -> None:
    ref = _ref("00000000-0000-0000-0000-000000000001")
    with pytest.raises(ValueError):
        PublicationParagraphV1(" ", (ref,))
    with pytest.raises(ValueError):
        PublicationParagraphV1("Finding", ())
    with pytest.raises(ValueError):
        PublicationSectionV1(PublicationSectionKind.OVERVIEW, "Overview", ())

    later = PublicationParagraphV1("Second", (ref,))
    earlier = PublicationParagraphV1("First", (ref,))
    section = PublicationSectionV1(PublicationSectionKind.OVERVIEW, "Overview", (later, earlier))
    assert section.paragraphs == (later, earlier)


def test_timeline_validates_date_and_text_without_parsing_date_text() -> None:
    ref = _ref("00000000-0000-0000-0000-000000000001")
    entry = PublicationTimelineEntryV1(None, "circa early 2025", "Observed", (ref,))
    assert entry.event_date is None
    assert entry.date_text == "circa early 2025"
    assert entry.text == "Observed"

    dated = PublicationTimelineEntryV1(date(2025, 1, 2), None, "Observed", (ref,))
    assert dated.event_date == date(2025, 1, 2)
    for event_date, date_text, text in (
        (datetime(2025, 1, 2), None, "Observed"),
        (None, "  ", "Observed"),
        (None, None, "  "),
    ):
        with pytest.raises(ValueError):
            PublicationTimelineEntryV1(event_date, date_text, text, (ref,))  # type: ignore[arg-type]
