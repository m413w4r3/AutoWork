from datetime import date, datetime
from uuid import UUID

import pytest

from cti_app.domain.discovery import SourceRole
from cti_app.domain.production_references import ProductionReferenceKind, ProductionReferenceTier
from cti_app.domain.production_synthesis import EvidenceKind, SynthesisSectionKind
from cti_app.domain.publication import (
    PUBLICATION_IOC_ARTIFACT_TYPES,
    ArtifactType,
    PublicationEvidenceKind,
    PublicationEvidenceRefV1,
    PublicationIndicatorGroupV1,
    PublicationIndicatorV1,
    PublicationParagraphV1,
    PublicationSectionKind,
    PublicationSectionV1,
    PublicationSourceV1,
    PublicationTimelineEntryV1,
    PublicationUncertaintyV1,
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


@pytest.mark.parametrize(
    "artifact_type",
    sorted(PUBLICATION_IOC_ARTIFACT_TYPES, key=lambda artifact_type: artifact_type.value),
)
def test_publication_indicator_accepts_all_publishable_ioc_types(
    artifact_type: ArtifactType,
) -> None:
    indicator = PublicationIndicatorV1(
        "indicator",
        "normalized",
        artifact_type,
        (UUID(int=1),),
    )
    assert indicator.artifact_type is artifact_type


@pytest.mark.parametrize(
    "artifact_type",
    (
        ArtifactType.YARA_RULE,
        ArtifactType.SIGMA_RULE,
        ArtifactType.SURICATA_RULE,
        ArtifactType.FILEPATH,
        ArtifactType.FILENAME,
        ArtifactType.CVE,
        ArtifactType.OTHER,
    ),
)
def test_publication_indicator_rejects_non_publication_types(artifact_type: ArtifactType) -> None:
    with pytest.raises(ValueError, match="not publishable"):
        PublicationIndicatorV1("indicator", "normalized", artifact_type, (UUID(int=1),))


def test_publication_indicator_requires_text_and_unique_uuid_provenance() -> None:
    first = UUID(int=1)
    second = UUID(int=2)
    indicator = PublicationIndicatorV1("value", "normalized", ArtifactType.IP, (second, first))
    assert indicator.source_document_ids == (first, second)

    for value, normalized_value, source_document_ids in (
        (" ", "normalized", (first,)),
        ("value", "\t", (first,)),
        ("value", "normalized", ()),
        ("value", "normalized", (first, first)),
        ("value", "normalized", ("not-a-uuid",)),
    ):
        with pytest.raises(ValueError):
            PublicationIndicatorV1(
                value,
                normalized_value,
                ArtifactType.IP,
                source_document_ids,  # type: ignore[arg-type]
            )


def test_publication_indicator_group_validates_and_sorts_indicators() -> None:
    source_id = UUID(int=1)
    zed = PublicationIndicatorV1("z", "z", ArtifactType.DOMAIN, (source_id,))
    alpha = PublicationIndicatorV1("a", "a", ArtifactType.DOMAIN, (source_id,))
    group = PublicationIndicatorGroupV1(ArtifactType.DOMAIN, (zed, alpha))
    assert group.indicators == (alpha, zed)

    with pytest.raises(ValueError, match="non-empty"):
        PublicationIndicatorGroupV1(ArtifactType.DOMAIN, ())
    with pytest.raises(ValueError, match="match"):
        PublicationIndicatorGroupV1(
            ArtifactType.DOMAIN,
            (PublicationIndicatorV1("1.2.3.4", "1.2.3.4", ArtifactType.IP, (source_id,)),),
        )
    with pytest.raises(ValueError, match="repeat normalized"):
        PublicationIndicatorGroupV1(
            ArtifactType.DOMAIN,
            (
                PublicationIndicatorV1("first", "same", ArtifactType.DOMAIN, (source_id,)),
                PublicationIndicatorV1("second", "same", ArtifactType.DOMAIN, (source_id,)),
            ),
        )


def _publication_source(**overrides: object) -> PublicationSourceV1:
    values: dict[str, object] = {
        "source_document_id": UUID(int=1),
        "canonical_url": "https://example.com/article?a=1&b=2",
        "title": "Article",
        "publisher": "Example",
        "published_at": date(2025, 1, 2),
        "tier": ProductionReferenceTier.CORE,
        "kind": ProductionReferenceKind.PUBLICATION,
        "role": SourceRole.PRIMARY,
    }
    values.update(overrides)
    return PublicationSourceV1(**values)  # type: ignore[arg-type]


def test_publication_source_keeps_canonical_corpus_metadata() -> None:
    source = _publication_source(title=None, publisher=None, published_at=None)
    assert source.source_document_id == UUID(int=1)
    assert source.title is None
    assert source.publisher is None
    assert source.published_at is None

    for overrides in (
        {"source_document_id": "not-a-uuid"},
        {"canonical_url": "HTTPS://EXAMPLE.COM/article/"},
        {"canonical_url": "https://example.com/article?b=2&a=1"},
        {"canonical_url": 123},
        {"title": 123},
        {"publisher": 123},
        {"published_at": datetime(2025, 1, 2)},
        {"tier": "core"},
        {"kind": "publication"},
        {"role": "primary"},
    ):
        with pytest.raises(ValueError):
            _publication_source(**overrides)


def test_publication_uncertainty_requires_unique_sorted_uuid_provenance() -> None:
    first = UUID(int=1)
    second = UUID(int=2)
    uncertainty = PublicationUncertaintyV1("Uncertain attribution", (second, first))
    assert uncertainty.source_document_ids == (first, second)

    for text, source_document_ids in (
        (" ", (first,)),
        ("Uncertain attribution", ()),
        ("Uncertain attribution", (first, first)),
        ("Uncertain attribution", ("not-a-uuid",)),
    ):
        with pytest.raises(ValueError):
            PublicationUncertaintyV1(text, source_document_ids)  # type: ignore[arg-type]
