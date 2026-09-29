import json
from dataclasses import replace
from datetime import date, datetime
from uuid import UUID

import pytest

from cti_app.domain.discovery import SourceRole
from cti_app.domain.production_references import ProductionReferenceKind, ProductionReferenceTier
from cti_app.domain.production_synthesis import EvidenceKind, SynthesisSectionKind
from cti_app.domain.publication import (
    PUBLICATION_DOCUMENT_V3_SCHEMA_VERSION,
    PUBLICATION_IOC_ARTIFACT_TYPES,
    PUBLICATION_SCHEMA_VERSION,
    ArtifactType,
    PublicationDocumentV2,
    PublicationDocumentV3,
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
    publication_document_from_json,
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


def _publication_v3_document() -> PublicationDocumentV3:
    first_id, second_id, third_id = UUID(int=1), UUID(int=2), UUID(int=3)
    return PublicationDocumentV3(
        schema_version=PUBLICATION_DOCUMENT_V3_SCHEMA_VERSION,
        subject_id=UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
        publication_language="en",
        title="Intrusion activity report",
        lead=(
            PublicationParagraphV1(
                "Initial assessment",
                (_ref(str(first_id), evidence_key="1" * 64),),
            ),
            PublicationParagraphV1(
                "Related activity",
                (_ref(str(second_id), evidence_key="2" * 64),),
            ),
        ),
        sections=(
            PublicationSectionV1(
                PublicationSectionKind.TECHNICAL,
                "Technical details",
                (
                    PublicationParagraphV1(
                        "The operator used a staged payload.",
                        (_ref(str(second_id), evidence_key="3" * 64),),
                    ),
                    PublicationParagraphV1(
                        "The payload contacted the infrastructure.",
                        (_ref(str(first_id), evidence_key="4" * 64),),
                    ),
                ),
            ),
            PublicationSectionV1(
                PublicationSectionKind.OVERVIEW,
                "Overview",
                (
                    PublicationParagraphV1(
                        "Activity affected several organizations.",
                        (_ref(str(third_id), evidence_key="5" * 64),),
                    ),
                ),
            ),
        ),
        timeline=(
            PublicationTimelineEntryV1(
                None,
                "early 2025",
                "Initial access was reported.",
                (_ref(str(second_id), evidence_key="6" * 64),),
            ),
            PublicationTimelineEntryV1(
                date(2025, 2, 3),
                None,
                "A payload was deployed.",
                (_ref(str(third_id), evidence_key="7" * 64),),
            ),
        ),
        indicators=(
            PublicationIndicatorGroupV1(
                ArtifactType.IP,
                (
                    PublicationIndicatorV1(
                        "192.0.2.10", "192.0.2.10", ArtifactType.IP, (second_id, first_id)
                    ),
                ),
            ),
            PublicationIndicatorGroupV1(
                ArtifactType.DOMAIN,
                (
                    PublicationIndicatorV1(
                        "example.net", "example.net", ArtifactType.DOMAIN, (third_id,)
                    ),
                ),
            ),
        ),
        sources=(
            _publication_source(
                source_document_id=third_id,
                canonical_url="https://example.com/article-three",
            ),
            _publication_source(
                source_document_id=first_id,
                canonical_url="https://example.com/article-one",
            ),
            _publication_source(
                source_document_id=second_id,
                canonical_url="https://example.com/article-two",
            ),
        ),
        uncertainties=(
            PublicationUncertaintyV1("Possible shared infrastructure", (third_id,)),
            PublicationUncertaintyV1("Attribution remains uncertain", (second_id, first_id)),
        ),
    )


def test_publication_document_v3_round_trips_exactly_and_has_canonical_contract() -> None:
    document = _publication_v3_document()
    payload = document.to_json()

    assert PublicationDocumentV3.from_json(payload) == document
    assert set(payload) == {
        "schema_version",
        "subject_id",
        "publication_language",
        "title",
        "lead",
        "sections",
        "timeline",
        "indicators",
        "sources",
        "uncertainties",
    }
    assert (
        not {
            "warnings",
            "renderer",
            "renderer_fields",
            "markdown",
            "synthesis",
            "local_id",
            "source_id",
        }
        & payload.keys()
    )
    assert payload["subject_id"] == "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    assert payload["timeline"][0]["event_date"] is None
    assert payload["timeline"][1]["event_date"] == "2025-02-03"


def test_publication_document_v3_sorts_non_editorial_collections_only() -> None:
    document = _publication_v3_document()
    reordered = replace(
        document,
        indicators=tuple(reversed(document.indicators)),
        sources=tuple(reversed(document.sources)),
        uncertainties=tuple(reversed(document.uncertainties)),
    )

    assert reordered.to_json() == document.to_json()
    assert json.dumps(reordered.to_json(), sort_keys=True) == json.dumps(
        document.to_json(), sort_keys=True
    )
    assert tuple(paragraph.text for paragraph in document.lead) == (
        "Initial assessment",
        "Related activity",
    )
    assert tuple(section.heading for section in document.sections) == (
        "Technical details",
        "Overview",
    )
    assert tuple(paragraph.text for paragraph in document.sections[0].paragraphs) == (
        "The operator used a staged payload.",
        "The payload contacted the infrastructure.",
    )
    assert tuple(entry.text for entry in document.timeline) == (
        "Initial access was reported.",
        "A payload was deployed.",
    )
    assert tuple(group.artifact_type for group in document.indicators) == (
        ArtifactType.DOMAIN,
        ArtifactType.IP,
    )
    assert tuple(source.source_document_id for source in document.sources) == (
        UUID(int=1),
        UUID(int=2),
        UUID(int=3),
    )
    assert tuple(item.text for item in document.uncertainties) == (
        "Attribution remains uncertain",
        "Possible shared infrastructure",
    )


def test_publication_document_v3_requires_exactly_the_used_source_identities() -> None:
    document = _publication_v3_document()
    unknown_id = UUID(int=99)
    unknown_paragraph = PublicationParagraphV1(
        "Unresolved evidence",
        (_ref(str(unknown_id), evidence_key="8" * 64),),
    )
    with pytest.raises(ValueError, match="exactly match"):
        replace(document, lead=(unknown_paragraph, *document.lead[1:]))

    with pytest.raises(ValueError, match="exactly match"):
        replace(
            document,
            sources=tuple(
                source for source in document.sources if source.source_document_id != UUID(int=3)
            ),
        )

    unused_source = _publication_source(
        source_document_id=unknown_id,
        canonical_url="https://example.com/article-unused",
    )
    with pytest.raises(ValueError, match="exactly match"):
        replace(document, sources=(*document.sources, unused_source))

    with pytest.raises(ValueError, match="repeat source_document_id"):
        replace(document, sources=(*document.sources, document.sources[0]))


def test_publication_document_v3_rejects_duplicate_indicator_groups_and_bad_schema() -> None:
    document = _publication_v3_document()
    with pytest.raises(ValueError, match="repeat artifact types"):
        replace(document, indicators=(*document.indicators, document.indicators[0]))

    with pytest.raises(ValueError, match="requires schema_version"):
        replace(document, schema_version="2")

    with pytest.raises(ValueError, match="invalid value type"):
        replace(document, lead=(None,))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="must be a tuple"):
        replace(document, sections=[])  # type: ignore[arg-type]


def test_publication_document_v3_from_json_validates_nested_values_and_top_level_shape() -> None:
    payload = _publication_v3_document().to_json()
    payload["lead"][0]["text"] = " "
    with pytest.raises(ValueError, match="paragraph text"):
        PublicationDocumentV3.from_json(payload)

    missing_field = _publication_v3_document().to_json()
    del missing_field["title"]
    with pytest.raises(ValueError, match="fields are invalid"):
        PublicationDocumentV3.from_json(missing_field)

    upper_uuid = _publication_v3_document().to_json()
    upper_uuid["subject_id"] = upper_uuid["subject_id"].upper()
    with pytest.raises(ValueError, match="canonical lowercase UUID"):
        PublicationDocumentV3.from_json(upper_uuid)

    compact_date = _publication_v3_document().to_json()
    compact_date["timeline"][1]["event_date"] = "20250203"
    with pytest.raises(ValueError, match="canonical ISO date"):
        PublicationDocumentV3.from_json(compact_date)


def test_publication_document_v3_rejects_duplicate_uncertainties() -> None:
    document = _publication_v3_document()
    with pytest.raises(ValueError, match="uncertainties must not repeat"):
        replace(document, uncertainties=(*document.uncertainties, document.uncertainties[0]))


def test_v3_addition_preserves_v2_schema_construction_serialization_and_reader() -> None:
    document = PublicationDocumentV2(
        schema_version=PUBLICATION_SCHEMA_VERSION,
        title="Historical V2 document",
        timeline=(),
        synthesis=(),
        indicators=(),
        sources=(),
        uncertainties=(),
    )
    payload = document.to_json()

    assert PUBLICATION_SCHEMA_VERSION == "2"
    assert payload == {
        "schema_version": "2",
        "title": "Historical V2 document",
        "timeline": (),
        "synthesis": (),
        "indicators": (),
        "sources": (),
        "uncertainties": (),
        "analyst_note": None,
        "original_indicators": (),
    }
    assert isinstance(publication_document_from_json(payload), PublicationDocumentV2)
    with pytest.raises(ValueError, match="unsupported publication document"):
        publication_document_from_json(_publication_v3_document().to_json())
