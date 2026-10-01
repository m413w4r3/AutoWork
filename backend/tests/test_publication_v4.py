from __future__ import annotations

import json
from dataclasses import replace
from datetime import date, datetime
from uuid import UUID

import pytest

from cti_app.domain.discovery import SourceRole
from cti_app.domain.production_editorial_enrichment import (
    DiagramEdgeV1,
    DiagramGroupV1,
    DiagramNodeV1,
    EnrichmentDiagramDirection,
    EnrichmentDiagramKind,
    EnrichmentPlacementKind,
    EnrichmentPlacementV1,
    EnrichmentTableKind,
    SourceFigureLocatorV1,
)
from cti_app.domain.production_references import ProductionReferenceKind, ProductionReferenceTier
from cti_app.domain.production_synthesis import (
    EvidenceKind,
    ExtractionEvidenceRefV1,
    SynthesisSectionKind,
)
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
from cti_app.domain.publication_document import (
    PUBLICATION_DOCUMENT_V4_SCHEMA_VERSION,
    PublicationDiagramV1,
    PublicationDocumentV4,
    PublicationSourceFigureV1,
    PublicationTableColumnV1,
    PublicationTableRowV1,
    PublicationTableV1,
    publication_document_v4_from_json,
    publication_document_v4_to_json,
)

_SUBJECT_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
_SOURCE_ID = UUID("00000000-0000-0000-0000-000000000001")
_SECOND_SOURCE_ID = UUID("00000000-0000-0000-0000-000000000002")
_EVIDENCE_REF = ExtractionEvidenceRefV1(_SOURCE_ID, EvidenceKind.FACT, "a" * 64)
_SECOND_EVIDENCE_REF = ExtractionEvidenceRefV1(_SECOND_SOURCE_ID, EvidenceKind.EVENT, "b" * 64)
_ASSET_ID = UUID("10000000-0000-0000-0000-000000000001")


def _source(source_document_id: UUID = _SOURCE_ID) -> PublicationSourceV1:
    return PublicationSourceV1(
        source_document_id=source_document_id,
        canonical_url=f"https://example.test/{source_document_id.int}",
        title="Source article",
        publisher="Example",
        published_at=None,
        tier=ProductionReferenceTier.CORE,
        kind=ProductionReferenceKind.PUBLICATION,
        role=SourceRole.PRIMARY,
    )


def _table(
    *, key: str = "commands", evidence_ref: ExtractionEvidenceRefV1 = _EVIDENCE_REF
) -> PublicationTableV1:
    return PublicationTableV1(
        key=key,
        kind=EnrichmentTableKind.COMMANDS,
        title="Observed commands",
        caption=None,
        columns=(
            PublicationTableColumnV1("command", "Command"),
            PublicationTableColumnV1("purpose", "Purpose"),
        ),
        rows=(PublicationTableRowV1(("-enc", "Execution"), (evidence_ref,)),),
        placement=EnrichmentPlacementV1(EnrichmentPlacementKind.AFTER_LEAD),
    )


def _diagram(
    *,
    asset_id: UUID | None = _ASSET_ID,
    evidence_ref: ExtractionEvidenceRefV1 = _EVIDENCE_REF,
) -> PublicationDiagramV1:
    return PublicationDiagramV1(
        key="infection_chain",
        kind=EnrichmentDiagramKind.INFECTION_CHAIN,
        title="Infection chain",
        caption=None,
        direction=EnrichmentDiagramDirection.LEFT_TO_RIGHT,
        nodes=(
            DiagramNodeV1("loader", "Loader", (evidence_ref,)),
            DiagramNodeV1("payload", "Payload", (evidence_ref,)),
        ),
        edges=(DiagramEdgeV1("loader", "payload", "loads", (evidence_ref,)),),
        groups=(DiagramGroupV1("host", "Victim host", ("loader", "payload")),),
        placement=EnrichmentPlacementV1(EnrichmentPlacementKind.END),
        asset_id=asset_id,  # type: ignore[arg-type]
    )


def _figure(
    *, source_document_id: UUID = _SOURCE_ID, **overrides: object
) -> PublicationSourceFigureV1:
    values: dict[str, object] = {
        "key": "source_figure_01",
        "asset_id": UUID("20000000-0000-0000-0000-000000000001"),
        "sha256": "c" * 64,
        "mime_type": "image/png",
        "byte_size": 128,
        "source_document_id": source_document_id,
        "source_url": "https://example.test/figure.png",
        "caption": "Source architecture",
        "provenance": "Figure 1 from the source publication",
        "locator": SourceFigureLocatorV1(page=1),
        "placement": EnrichmentPlacementV1(EnrichmentPlacementKind.AFTER_LEAD),
    }
    values.update(overrides)
    return PublicationSourceFigureV1(**values)  # type: ignore[arg-type]


def _document(
    *,
    sources: tuple[PublicationSourceV1, ...] = (_source(),),
    tables: tuple[PublicationTableV1, ...] = (),
    diagrams: tuple[PublicationDiagramV1, ...] = (),
    figures: tuple[PublicationSourceFigureV1, ...] = (),
    lead_refs: tuple[PublicationEvidenceRefV1, ...] = (
        PublicationEvidenceRefV1(_SOURCE_ID, PublicationEvidenceKind.FACT, "d" * 64),
    ),
) -> PublicationDocumentV4:
    return PublicationDocumentV4(
        schema_version=PUBLICATION_DOCUMENT_V4_SCHEMA_VERSION,
        subject_id=_SUBJECT_ID,
        publication_language="fr",
        title="Example report",
        lead=(PublicationParagraphV1("Initial assessment", lead_refs),),
        sections=(),
        timeline=(),
        indicators=(),
        sources=sources,
        uncertainties=(),
        tables=tables,
        diagrams=diagrams,
        figures=figures,
    )


def test_v4_minimal_document_without_enrichment_is_valid() -> None:
    document = _document()

    assert document.tables == document.diagrams == document.figures == ()
    assert document.schema_version == "4"


def test_v4_table_is_projected_and_keeps_row_evidence() -> None:
    document = _document(tables=(_table(),))
    payload = publication_document_v4_to_json(document)

    assert payload["tables"][0]["rows"][0]["evidence_refs"][0]["evidence_key"] == "a" * 64
    assert publication_document_v4_from_json(payload) == document


def test_v4_compiled_diagram_requires_and_keeps_asset_identity() -> None:
    diagram = _diagram()
    document = _document(diagrams=(diagram,))
    payload = publication_document_v4_to_json(document)

    assert payload["diagrams"][0]["asset_id"] == str(diagram.asset_id)
    assert publication_document_v4_from_json(payload) == document

    with pytest.raises(ValueError, match="asset"):
        _diagram(asset_id=None)


def test_v4_included_source_figure_is_valid_and_round_trips() -> None:
    document = _document(figures=(_figure(),))

    assert publication_document_v4_from_json(publication_document_v4_to_json(document)) == document


def test_v4_roundtrip_with_all_enrichment_is_exact() -> None:
    document = _document(tables=(_table(),), diagrams=(_diagram(),), figures=(_figure(),))

    payload = publication_document_v4_to_json(document)
    assert publication_document_v4_from_json(payload) == document
    assert publication_document_v4_to_json(publication_document_v4_from_json(payload)) == payload


def test_v4_rejects_non_v4_schema() -> None:
    with pytest.raises(ValueError, match="schema_version='4'"):
        replace(_document(), schema_version="3")

    payload = publication_document_v4_to_json(_document())
    payload["schema_version"] = "3"
    with pytest.raises(ValueError, match="schema_version='4'"):
        publication_document_v4_from_json(payload)


def test_v4_rejects_duplicate_rich_keys_across_collections() -> None:
    with pytest.raises(ValueError, match="globally unique"):
        _document(
            tables=(_table(key="same_key"),),
            diagrams=(replace(_diagram(), key="same_key"),),
            figures=(replace(_figure(), key="same_key"),),
        )


@pytest.mark.parametrize(
    "overrides",
    (
        {"sha256": "A" * 64},
        {"sha256": "not-a-sha256"},
        {"mime_type": "application/octet-stream"},
        {"byte_size": 0},
        {"byte_size": -1},
    ),
)
def test_v4_rejects_invalid_source_figures(overrides: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        _figure(**overrides)


def test_v4_rejects_source_figure_with_missing_document_source() -> None:
    with pytest.raises(ValueError, match="exactly match"):
        _document(figures=(_figure(source_document_id=_SECOND_SOURCE_ID),))


def test_v4_requires_exact_source_coverage_for_enrichment() -> None:
    with pytest.raises(ValueError, match="exactly match"):
        _document(
            sources=(_source(),),
            tables=(_table(evidence_ref=_SECOND_EVIDENCE_REF),),
        )

    with pytest.raises(ValueError, match="exactly match"):
        _document(sources=(_source(), _source(_SECOND_SOURCE_ID)))


@pytest.mark.parametrize(
    "enrichment",
    (
        lambda: {"tables": (_table(evidence_ref=_SECOND_EVIDENCE_REF),)},
        lambda: {"diagrams": (_diagram(evidence_ref=_SECOND_EVIDENCE_REF),)},
        lambda: {"figures": (_figure(source_document_id=_SECOND_SOURCE_ID),)},
    ),
)
def test_v4_accepts_sources_introduced_by_each_enrichment_kind(enrichment) -> None:
    document = _document(sources=(_source(), _source(_SECOND_SOURCE_ID)), **enrichment())

    assert _SECOND_SOURCE_ID in {source.source_document_id for source in document.sources}


def test_v4_rejects_table_width_mismatch() -> None:
    with pytest.raises(ValueError, match="row width"):
        replace(
            _table(),
            rows=(PublicationTableRowV1(("only one cell",), (_EVIDENCE_REF,)),),
        )


def test_v4_rejects_diagram_edge_to_unknown_node() -> None:
    with pytest.raises(ValueError, match="existing nodes"):
        replace(
            _diagram(),
            edges=(DiagramEdgeV1("loader", "missing", "loads", (_EVIDENCE_REF,)),),
        )


def test_v4_rejects_unknown_json_fields() -> None:
    payload = publication_document_v4_to_json(_document())
    payload["renderer"] = "forbidden"
    with pytest.raises(ValueError, match="fields are invalid"):
        publication_document_v4_from_json(payload)

    nested = publication_document_v4_to_json(_document(tables=(_table(),)))
    nested["tables"][0]["d2"] = "forbidden"
    with pytest.raises(ValueError, match="fields are invalid"):
        publication_document_v4_from_json(nested)

    missing = publication_document_v4_to_json(_document())
    del missing["title"]
    with pytest.raises(ValueError, match="fields are invalid"):
        publication_document_v4_from_json(missing)


def test_v4_serialization_is_deterministic_and_renderer_free() -> None:
    document = _document()
    equal_document = _document()
    reordered = replace(document, sources=tuple(reversed(document.sources)))

    assert document == equal_document
    assert publication_document_v4_to_json(equal_document) == publication_document_v4_to_json(
        document
    )
    assert publication_document_v4_to_json(reordered) == publication_document_v4_to_json(document)
    assert not {"d2", "svg", "typst", "markdown", "renderer"} & set(
        publication_document_v4_to_json(document)
    )


# Base publication contract validation migrated from the former version-specific suite.


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


def _publication_v4_document() -> PublicationDocumentV4:
    first_id, second_id, third_id = UUID(int=1), UUID(int=2), UUID(int=3)
    return PublicationDocumentV4(
        schema_version=PUBLICATION_DOCUMENT_V4_SCHEMA_VERSION,
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
        tables=(),
        diagrams=(),
        figures=(),
    )


def test_publication_document_v4_round_trips_exactly_and_has_canonical_contract() -> None:
    document = _publication_v4_document()
    payload = publication_document_v4_to_json(document)

    assert publication_document_v4_from_json(payload) == document
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
        "tables",
        "diagrams",
        "figures",
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


def test_publication_document_v4_sorts_non_editorial_collections_only() -> None:
    document = _publication_v4_document()
    reordered = replace(
        document,
        indicators=tuple(reversed(document.indicators)),
        sources=tuple(reversed(document.sources)),
        uncertainties=tuple(reversed(document.uncertainties)),
    )

    assert publication_document_v4_to_json(reordered) == publication_document_v4_to_json(document)
    assert json.dumps(publication_document_v4_to_json(reordered), sort_keys=True) == json.dumps(
        publication_document_v4_to_json(document), sort_keys=True
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


def test_publication_document_v4_requires_exactly_the_used_source_identities() -> None:
    document = _publication_v4_document()
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


def test_publication_document_v4_rejects_duplicate_indicator_groups_and_bad_schema() -> None:
    document = _publication_v4_document()
    with pytest.raises(ValueError, match="repeat artifact types"):
        replace(document, indicators=(*document.indicators, document.indicators[0]))

    with pytest.raises(ValueError, match="requires schema_version"):
        replace(document, schema_version="2")

    with pytest.raises(ValueError, match="invalid value type"):
        replace(document, lead=(None,))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="must be a tuple"):
        replace(document, sections=[])  # type: ignore[arg-type]


def test_publication_document_v4_parse_validates_nested_values_and_top_level_shape() -> None:
    payload = publication_document_v4_to_json(_publication_v4_document())
    payload["lead"][0]["text"] = " "
    with pytest.raises(ValueError, match="paragraph text"):
        publication_document_v4_from_json(payload)

    missing_field = publication_document_v4_to_json(_publication_v4_document())
    del missing_field["title"]
    with pytest.raises(ValueError, match="fields are invalid"):
        publication_document_v4_from_json(missing_field)

    upper_uuid = publication_document_v4_to_json(_publication_v4_document())
    upper_uuid["subject_id"] = upper_uuid["subject_id"].upper()
    with pytest.raises(ValueError, match="canonical lowercase UUID"):
        publication_document_v4_from_json(upper_uuid)

    compact_date = publication_document_v4_to_json(_publication_v4_document())
    compact_date["timeline"][1]["event_date"] = "20250203"
    with pytest.raises(ValueError, match="canonical ISO date"):
        publication_document_v4_from_json(compact_date)


def test_publication_document_v4_rejects_duplicate_uncertainties() -> None:
    document = _publication_v4_document()
    with pytest.raises(ValueError, match="uncertainties must not repeat"):
        replace(document, uncertainties=(*document.uncertainties, document.uncertainties[0]))


def test_explicit_v4_document_entrypoint_round_trips_and_rejects_removed_versions() -> None:
    document = _publication_v4_document()
    payload = publication_document_v4_to_json(document)

    assert publication_document_v4_from_json(payload) == document
    for schema_version in ("1", "2", "3"):
        with pytest.raises(ValueError, match="requires schema_version"):
            publication_document_v4_from_json({**payload, "schema_version": schema_version})
