from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import date
from uuid import UUID

from cti_app.application.brief_publication_builder import wrap_publication_document_v7
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.application.publication_builder import (
    ARTICLE_FORMAT_POLICY_VERSION,
    ASSEMBLY_POLICY_VERSION,
)
from cti_app.application.typst_rendering import (
    _RENDER_DATA_SCHEMA_VERSION,
    project_publication_to_typst_model,
)
from cti_app.domain.discovery import SourceRole
from cti_app.domain.production import ProductionEvidenceBasis
from cti_app.domain.production_references import ProductionReferenceTier
from cti_app.domain.production_synthesis import (
    source_metadata_evidence_ref_v1,
)
from cti_app.domain.publication import (
    ArtifactType,
    PublicationEvidenceKind,
    PublicationEvidenceRefV1,
    PublicationIndicatorGroupV1,
    PublicationIndicatorV1,
    PublicationParagraphV1,
    PublicationReferenceEntryV1,
    PublicationSectionKind,
    PublicationSectionV1,
    PublicationTimelineEntryV1,
)
from cti_app.domain.publication_document import (
    ARTICLE_FORMAT_TIMELINE_MAX_EVENTS,
    ArticleFormatV1,
    IOCArticleCategoriesV1,
    PublicationDocumentV5,
    PublicationDocumentV8,
    PublicationIOCDetailV1,
    PublicationIOCDisposition,
    extract_article_overview,
    parse_publication_document,
    publication_document_text_anchors,
    publication_document_v5_sha256,
    select_article_timeline_key_events_v1,
    serialize_publication_document,
)
from cti_app.domain.publication_kind import PublicationKind
from cti_app.domain.semantic_annotation import (
    SEMANTIC_ANNOTATION_POLICY_VERSION,
    SEMANTIC_ANNOTATION_SCHEMA_VERSION,
    SemanticParagraphV1,
    SemanticRole,
    SemanticTextSpanV1,
    SemanticTextV1,
)
from tests.test_publication_v4 import _SECOND_SOURCE_ID, _SOURCE_ID, _document, _figure, _table

_THIRD_SOURCE_ID = UUID("00000000-0000-0000-0000-000000000003")
_ARTICLE_ID = UUID("00000000-0000-0000-0000-00000000000a")
_KNOWN_URL = "http://bad.example/path"
_KNOWN_DEFANGED_URL = "hxxp[:]//bad[dot]example/path"
_KNOWN_EMAIL = "analyst[@]bad[dot]example"


def _source(
    source_id: UUID,
    *,
    url: str,
    title: str | None,
    publisher: str | None,
    published_at: date | None,
):
    return replace(
        _document().sources[0],
        source_document_id=source_id,
        canonical_url=url,
        title=title,
        publisher=publisher,
        published_at=published_at,
    )


def _publication(
    *,
    sources=None,
    timeline=(),
    lead_text: str = "La source signale un événement.",
    semantic_lead_spans: tuple[SemanticTextSpanV1, ...] | None = None,
    sections=None,
    indicators=(),
    tables=(),
    figures=(),
    reference_notes=None,
    ioc_details=(),
) -> PublicationDocumentV5:
    sources = tuple(
        sources
        or (
            _source(
                _SOURCE_ID,
                url="https://source.example/one",
                title="Source article",
                publisher="Example",
                published_at=None,
            ),
        )
    )
    source_ids = tuple(source.source_document_id for source in sources)
    lead_refs = tuple(
        PublicationEvidenceRefV1(source_id, PublicationEvidenceKind.FACT, f"{index:x}" * 64)
        for index, source_id in enumerate(source_ids, start=1)
    )
    base = replace(
        _document(
            sources=sources,
            tables=tuple(tables),
            figures=tuple(figures),
            lead_refs=lead_refs,
        ),
        lead=(PublicationParagraphV1(lead_text, lead_refs),),
        sections=tuple(sections)
        if sections is not None
        else (
            PublicationSectionV1(
                PublicationSectionKind.TECHNICAL,
                "Analyse",
                (PublicationParagraphV1("Les détails sont confirmés par les sources.", lead_refs),),
            ),
        ),
        timeline=tuple(timeline),
        indicators=tuple(indicators),
        additional_source_ids=frozenset(
            (
                *source_ids,
                *(source_id for detail in ioc_details for source_id in detail.source_document_ids),
            )
        ),
    )
    notes = reference_notes or {}
    references = tuple(
        PublicationReferenceEntryV1(
            source_document_id=source.source_document_id,
            text=notes.get(
                source.source_document_id,
                "Les chercheurs décrivent les éléments techniques pertinents de la campagne.",
            ),
            evidence_refs=(
                PublicationEvidenceRefV1(
                    source.source_document_id,
                    PublicationEvidenceKind.FACT,
                    f"{index + 5:x}" * 64,
                ),
            ),
        )
        for index, source in enumerate(sources)
    )
    sources_by_id = {source.source_document_id: source for source in sources}
    references = tuple(
        sorted(
            references,
            key=lambda item: (
                sources_by_id[item.source_document_id].published_at is None,
                sources_by_id[item.source_document_id].published_at or date.max,
                sources_by_id[item.source_document_id].tier is ProductionReferenceTier.CORE,
                sources_by_id[item.source_document_id].canonical_url,
            ),
        )
    )
    anchors = publication_document_text_anchors(base, references=references)
    semantic_paragraphs = []
    for anchor, text in anchors.items():
        spans = (
            semantic_lead_spans
            if anchor == "lead:0001" and semantic_lead_spans is not None
            else (SemanticTextSpanV1(SemanticRole.TEXT, text),)
        )
        semantic_paragraphs.append(SemanticParagraphV1(anchor, spans))
    return PublicationDocumentV5(
        document=base,
        semantic_text=SemanticTextV1(
            schema_version=SEMANTIC_ANNOTATION_SCHEMA_VERSION,
            policy_version=SEMANTIC_ANNOTATION_POLICY_VERSION,
            paragraphs=tuple(semantic_paragraphs),
        ),
        references=references,
        ioc_details=tuple(ioc_details),
    )


def _indicator(
    artifact_type: ArtifactType, value: str, normalized: str
) -> PublicationIndicatorGroupV1:
    return PublicationIndicatorGroupV1(
        artifact_type,
        (
            PublicationIndicatorV1(
                value=value,
                normalized_value=normalized,
                artifact_type=artifact_type,
                source_document_ids=(_SOURCE_ID,),
            ),
        ),
    )


def _file_detail(artifact_type: ArtifactType, value: str) -> PublicationIOCDetailV1:
    return PublicationIOCDetailV1(
        artifact_type=artifact_type,
        value=value,
        normalized_value=value,
        source_role=SourceRole.PRIMARY,
        context="File observed in source",
        evidence_quote=value,
        evidence_basis=ProductionEvidenceBasis.SOURCE_VERIFIED,
        source_document_ids=(_SOURCE_ID,),
        disposition=PublicationIOCDisposition.RETAINED,
        disposition_reason="Retained as canonical evidence",
    )


def test_publication_date_events_require_metadata_proof_and_two_distinct_dates() -> None:
    import cti_app.domain.publication_document as publication_document

    build_events = getattr(publication_document, "build_publication_date_events", None)
    select_events = getattr(publication_document, "select_publication_timeline_key_events_v2", None)
    assert callable(build_events)
    assert callable(select_events)

    first = _source(
        _SOURCE_ID,
        url="https://a.example/source",
        title="First report",
        publisher="Example",
        published_at=date(2026, 10, 14),
    )
    second = _source(
        _SECOND_SOURCE_ID,
        url="https://b.example/source",
        title="Second report",
        publisher="Example",
        published_at=date(2026, 10, 15),
    )
    undated = _source(
        _THIRD_SOURCE_ID,
        url="https://c.example/source",
        title="Undated report",
        publisher="Example",
        published_at=None,
    )
    missing_metadata_proof = _source(
        _THIRD_SOURCE_ID,
        url="https://c.example/source",
        title=None,
        publisher="Example",
        published_at=date(2026, 10, 16),
    )

    first_events = build_events((first, undated, missing_metadata_proof))
    assert len(first_events) == 1
    assert first_events[0].event_date == date(2026, 10, 14)
    assert first_events[0].text == "Publication de la source."
    expected_ref = source_metadata_evidence_ref_v1(
        source_document_id=first.source_document_id,
        canonical_url=first.canonical_url,
        title=first.title,
        publisher=first.publisher,
        published_at=first.published_at,
    )
    assert first_events[0].evidence_refs == (
        PublicationEvidenceRefV1(
            first.source_document_id,
            PublicationEvidenceKind.SOURCE_METADATA,
            expected_ref.evidence_key,
        ),
    )
    assert select_events((), (first,)) == ()
    assert len(select_events((), (first, second))) == 2

    same_date_duplicate = _source(
        _THIRD_SOURCE_ID,
        url="https://c.example/source",
        title="First report",
        publisher="Example",
        published_at=date(2026, 10, 14),
    )
    deduplicated = build_events((first, same_date_duplicate))
    assert len(deduplicated) == 1
    assert len(deduplicated[0].evidence_refs) == 2
    assert select_events((), (first, same_date_duplicate)) == ()

    same_date_timeline = (
        PublicationTimelineEntryV1(date(2026, 10, 14), None, "Event A", (_ref(_SOURCE_ID, "a"),)),
        PublicationTimelineEntryV1(date(2026, 10, 14), None, "Event B", (_ref(_SOURCE_ID, "b"),)),
    )
    assert select_events(same_date_timeline, ()) == ()
    capped = select_events(
        (),
        tuple(
            _source(
                UUID(int=100 + index),
                url=f"https://source{index}.example/",
                title=f"Report {index}",
                publisher="Example",
                published_at=date(2026, 1, index + 1),
            )
            for index in range(10)
        ),
    )
    assert len(capped) == ARTICLE_FORMAT_TIMELINE_MAX_EVENTS


def test_reader_projection_refangs_only_known_iocs_and_keeps_canonical_data() -> None:
    sources = (
        _source(
            _SECOND_SOURCE_ID,
            url="https://z.example/article",
            title="Second source",
            publisher="Zeta",
            published_at=date(2026, 10, 14),
        ),
        _source(
            _SOURCE_ID,
            url="https://a.example/article",
            title="First source",
            publisher="Alpha",
            published_at=date(2026, 1, 2),
        ),
        _source(
            _THIRD_SOURCE_ID,
            url="https://undated.example/article",
            title="Undated source",
            publisher="Undated",
            published_at=None,
        ),
    )
    raw_note = (
        "Le rapport décrit le serveur connu. https://source.example/page "
        "www.other.example/report hxxps[:]//other[.]example/report. "
        "Le 14 octobre 2026. 14/10/2026."
    )
    lead = (
        "Alert from "
        + _KNOWN_DEFANGED_URL
        + "; contact "
        + _KNOWN_EMAIL
        + "; literal [.] without an IOC remains."
    )
    spans = (
        SemanticTextSpanV1(SemanticRole.TEXT, "Alert from "),
        SemanticTextSpanV1(SemanticRole.IOC, _KNOWN_DEFANGED_URL),
        SemanticTextSpanV1(SemanticRole.TEXT, "; contact "),
        SemanticTextSpanV1(SemanticRole.TECHNICAL, _KNOWN_EMAIL),
        SemanticTextSpanV1(SemanticRole.TEXT, "; literal [.] without an IOC remains."),
    )
    indicators = (
        _indicator(ArtifactType.URL, _KNOWN_DEFANGED_URL, _KNOWN_URL),
        _indicator(ArtifactType.EMAIL, _KNOWN_EMAIL, "analyst@bad.example"),
        _indicator(ArtifactType.HASH, "a" * 64, "a" * 64),
    )
    table = replace(
        _table(),
        caption=f"Observed at {_KNOWN_DEFANGED_URL}",
        rows=(replace(_table().rows[0], cells=(_KNOWN_DEFANGED_URL, "Observed")),),
    )
    figure = _figure(caption=f"Figure with {_KNOWN_DEFANGED_URL}")
    publication = _publication(
        sources=sources,
        lead_text=lead,
        semantic_lead_spans=spans,
        indicators=indicators,
        ioc_details=(
            _file_detail(ArtifactType.FILENAME, "sample[.]dll"),
            _file_detail(ArtifactType.FILEPATH, "C:\\temp\\sample[.]dll"),
        ),
        tables=(table,),
        figures=(figure,),
        reference_notes={_SOURCE_ID: raw_note},
    )
    original_sha = publication_document_v5_sha256(publication)
    model = project_publication_to_typst_model(publication)
    references = model.content_sections[0]
    assert references["type"] == "references"
    entries = references["entries"]
    assert [entry["url"] for entry in entries] == [
        "https://a.example/article",
        "https://z.example/article",
        "https://undated.example/article",
    ]
    assert entries[0]["display_date"] == "2 janvier 2026"
    assert "display_date" not in entries[-1]
    assert entries[0]["text"] == "Le rapport décrit le serveur connu."
    assert len(entries[0]["text"]) <= 260
    assert "https://" not in entries[0]["text"]
    assert "www.other.example" not in entries[0]["text"]
    assert "hxxps" not in entries[0]["text"]
    assert "14 octobre 2026" not in entries[0]["text"]
    assert "14/10/2026" not in entries[0]["text"]

    article_format = model.article_format
    assert article_format is not None
    assert [item["value"]["text"] for item in article_format["ioc_categories"]["urls"]] == [
        _KNOWN_URL
    ]
    assert [item["value"]["text"] for item in article_format["ioc_categories"]["files"]] == [
        "a" * 64
    ]
    synthesis = next(
        section for section in model.content_sections if section["type"] == "synthesis"
    )
    paragraph = synthesis["blocks"][0]
    expected_lead = (
        "Alert from "
        + _KNOWN_URL
        + "; contact analyst@bad.example; literal [.] without an IOC remains."
    )
    assert paragraph["text"] == expected_lead
    assert "".join(span["text"] for span in paragraph["semantic_spans"]) == expected_lead
    assert [span["text"] for span in paragraph["semantic_spans"]] == [
        "Alert from ",
        _KNOWN_URL,
        "; contact ",
        "analyst@bad.example",
        "; literal [.] without an IOC remains.",
    ]
    table_block = next(block for block in synthesis["blocks"] if block["type"] == "table")
    assert table_block["caption"] == f"Observed at {_KNOWN_URL}"
    assert table_block["rows"][0][0] == _KNOWN_URL
    figure_block = next(block for block in synthesis["blocks"] if block["type"] == "figure")
    assert figure_block["caption"] == f"Figure with {_KNOWN_URL}"
    assert "source_note" not in figure_block
    assert "attribution" not in figure_block
    assert "evidence_classification" not in figure_block
    assert figure_block["sha256"] == "c" * 64
    assert publication.document.figures[0].provenance == "Figure 1 from the source publication"
    assert tuple(item.artifact_type for item in publication.ioc_details) == (
        ArtifactType.FILENAME,
        ArtifactType.FILEPATH,
    )
    assert tuple(item.value for item in publication.ioc_details) == (
        "sample[.]dll",
        "C:\\temp\\sample[.]dll",
    )
    assert publication_document_v5_sha256(publication) == original_sha


def test_article_format_overview_and_timeline_refang_known_iocs() -> None:
    sources = (
        _source(
            _SOURCE_ID,
            url="https://one.example/article",
            title="One source",
            publisher="Example",
            published_at=date(2026, 1, 2),
        ),
        _source(
            _SECOND_SOURCE_ID,
            url="https://two.example/article",
            title="Two source",
            publisher="Example",
            published_at=date(2026, 1, 3),
        ),
    )
    refs = (_ref(_SOURCE_ID, "d"),)
    sections = (
        PublicationSectionV1(
            PublicationSectionKind.TECHNICAL,
            "Vue d'ensemble",
            (
                PublicationParagraphV1(
                    f"Victimologie: Les opérateurs ciblent {_KNOWN_DEFANGED_URL}.", refs
                ),
                PublicationParagraphV1("Arsenal: Un outil de collecte.", refs),
                PublicationParagraphV1("Objectif principal: Vol de données.", refs),
            ),
        ),
    )
    event_text = f"Premier accès observé via {_KNOWN_DEFANGED_URL}."
    publication = _publication(
        sources=sources,
        sections=sections,
        timeline=(
            PublicationTimelineEntryV1(date(2026, 1, 4), None, event_text, refs),
            PublicationTimelineEntryV1(
                date(2026, 1, 5), "début janvier 2026", "Month-only event.", refs
            ),
        ),
        indicators=(
            PublicationIndicatorGroupV1(
                ArtifactType.URL,
                (
                    PublicationIndicatorV1(
                        value=_KNOWN_DEFANGED_URL,
                        normalized_value=_KNOWN_URL,
                        artifact_type=ArtifactType.URL,
                        source_document_ids=(_SOURCE_ID,),
                    ),
                    PublicationIndicatorV1(
                        value=r"C:\ProgramData\malicious.exe",
                        normalized_value=r"C:\ProgramData\malicious.exe",
                        artifact_type=ArtifactType.URL,
                        source_document_ids=(_SOURCE_ID,),
                    ),
                ),
            ),
        ),
    )

    model = project_publication_to_typst_model(publication)
    assert model.article_format is not None
    assert model.article_format["overview"]["victimology"]["text"] == (
        f"Les opérateurs ciblent {_KNOWN_URL}."
    )
    assert any(
        event["text"] == f"Premier accès observé via {_KNOWN_URL}."
        for event in model.article_format["timeline_key_events"]
    )
    month_only_event = next(
        event
        for event in model.article_format["timeline_key_events"]
        if event["text"] == "Month-only event."
    )
    assert month_only_event["display_date_fr"] == "début janvier 2026"


def test_reference_fallback_is_deterministic_bounded_and_contains_no_url() -> None:
    source = _source(
        _SOURCE_ID,
        url="https://source.example/a-report",
        title=None,
        publisher="Example publisher",
        published_at=None,
    )
    publication = _publication(
        sources=(source,),
        reference_notes={source.source_document_id: source.canonical_url},
    )

    entries = project_publication_to_typst_model(publication).content_sections[0]["entries"]
    fallback = entries[0]["text"]
    assert fallback == "Publication par Example publisher."
    assert len(fallback) <= 260
    assert "http" not in fallback
    assert "source.example" not in fallback


def test_brief_projection_filters_filename_iocs_and_applies_publication_date_threshold() -> None:
    one_source = _source(
        _SOURCE_ID,
        url="https://one.example/article",
        title="One source",
        publisher="Example",
        published_at=date(2026, 4, 1),
    )
    brief_one = wrap_publication_document_v7(
        _publication(
            sources=(one_source,),
            indicators=(_indicator(ArtifactType.HASH, "b" * 64, "b" * 64),),
            ioc_details=(_file_detail(ArtifactType.FILENAME, "only-name.exe"),),
        ),
        base_publication_artifact_id=UUID(int=500),
        publication_kind=PublicationKind.BRIEF,
        routing_decision_hash="1" * 64,
        routing_policy_version="routing-v1",
    )
    model_one = project_publication_to_typst_model(brief_one, include_brief_chronology=True)
    assert not any(section["type"] == "chronology" for section in model_one.content_sections)
    ioc_section = next(
        section for section in model_one.content_sections if section["type"] == "ioc"
    )
    assert [
        item["value"]["text"]
        for group in ioc_section["source_indicators"]
        for item in group["items"]
    ] == ["b" * 64]

    two_sources = (
        one_source,
        _source(
            _SECOND_SOURCE_ID,
            url="https://two.example/article",
            title="Second source",
            publisher="Example",
            published_at=date(2026, 4, 2),
        ),
    )
    brief_two = wrap_publication_document_v7(
        _publication(sources=two_sources),
        base_publication_artifact_id=UUID(int=501),
        publication_kind=PublicationKind.BRIEF,
        routing_decision_hash="2" * 64,
        routing_policy_version="routing-v1",
    )
    model_two = project_publication_to_typst_model(brief_two, include_brief_chronology=True)
    chronology = next(
        section for section in model_two.content_sections if section["type"] == "chronology"
    )
    assert [row["display_date"] for row in chronology["timeline"]] == [
        "1er avril 2026",
        "2 avril 2026",
    ]


def test_v8_policy_v1_timeline_payload_roundtrips_with_identical_canonical_hash() -> None:
    source = _source(
        _SOURCE_ID,
        url="https://source.example/article",
        title="Source article",
        publisher="Example",
        published_at=None,
    )
    old_timeline = (
        PublicationTimelineEntryV1(date(2026, 4, 1), None, "First event", (_ref(_SOURCE_ID, "a"),)),
        PublicationTimelineEntryV1(
            date(2026, 4, 1), None, "Second event", (_ref(_SOURCE_ID, "b"),)
        ),
    )
    publication = _publication(sources=(source,), timeline=old_timeline)
    historical_events = select_article_timeline_key_events_v1(old_timeline)
    assert len(historical_events) == 2
    empty_iocs = IOCArticleCategoriesV1(ips=(), urls=(), domains=(), files=())
    from cti_app.application.brief_publication_builder import wrap_publication_document_v7

    base_v7 = wrap_publication_document_v7(
        publication,
        base_publication_artifact_id=_ARTICLE_ID,
        publication_kind=PublicationKind.ARTICLE,
        routing_decision_hash="3" * 64,
        routing_policy_version="routing-v1",
    )
    historical = PublicationDocumentV8(
        base_publication=base_v7,
        article_format=ArticleFormatV1(
            overview=extract_article_overview(publication.document.sections),
            timeline_key_events=historical_events,
            ioc_categories=empty_iocs,
        ),
    )
    payload = serialize_publication_document(historical)
    original_bytes = ProductionArtifactStore.canonical_json_bytes(payload)
    original_sha = hashlib.sha256(original_bytes).hexdigest()
    parsed = parse_publication_document(payload)
    replayed_payload = serialize_publication_document(parsed)
    replayed_bytes = ProductionArtifactStore.canonical_json_bytes(replayed_payload)
    assert replayed_bytes == original_bytes
    assert hashlib.sha256(replayed_bytes).hexdigest() == original_sha


def test_m19_contract_versions_are_bumped_and_bounded() -> None:
    assert ARTICLE_FORMAT_POLICY_VERSION.startswith("2-")
    assert ASSEMBLY_POLICY_VERSION.startswith("17-")
    assert _RENDER_DATA_SCHEMA_VERSION == "typst-publication-model-v34-m19-format"
    assert all(
        len(value) <= 64
        for value in (
            ARTICLE_FORMAT_POLICY_VERSION,
            ASSEMBLY_POLICY_VERSION,
            _RENDER_DATA_SCHEMA_VERSION,
        )
    )


def _ref(source_id: UUID, token: str) -> PublicationEvidenceRefV1:
    return PublicationEvidenceRefV1(
        source_id,
        PublicationEvidenceKind.EVENT,
        token * 64,
    )
