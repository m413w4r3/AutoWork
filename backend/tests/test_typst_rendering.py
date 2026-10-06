from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import date
from pathlib import Path
from uuid import UUID

import pytest

from cti_app.application.semantic_annotation import EnglishTermDetector, SemanticAnnotator
from cti_app.application.typst_rendering import (
    TypstRenderer,
    TypstTemplateBundle,
    _display_date,
    _indicator_values,
    _table_column_weights,
    _timeline_source_urls,
    load_template_bundle,
    project_publication_to_typst_model,
)
from cti_app.domain.media_assets import MediaAssetKind
from cti_app.domain.production_editorial_enrichment import (
    ChartKind,
    EnrichmentPlacementKind,
    EnrichmentPlacementV1,
)
from cti_app.domain.production_synthesis import EvidenceKind, ExtractionEvidenceRefV1
from cti_app.domain.publication import (
    ArtifactType,
    PublicationEvidenceKind,
    PublicationEvidenceRefV1,
    PublicationIndicatorGroupV1,
    PublicationIndicatorV1,
    PublicationParagraphV1,
    PublicationSectionKind,
    PublicationSectionV1,
    PublicationTimelineEntryV1,
    PublicationUncertaintyV1,
)
from cti_app.domain.publication_document import (
    PublicationChartV1,
    PublicationDocumentV4,
    PublicationDocumentV5,
    publication_document_text_anchors,
)
from cti_app.domain.semantic_annotation import (
    SEMANTIC_ANNOTATION_POLICY_VERSION,
    SEMANTIC_ANNOTATION_SCHEMA_VERSION,
    SemanticAnnotationProposalV1,
    SemanticRole,
    SemanticTextV1,
)
from tests.test_publication_builder_v4 import _frontmatter_v6_case
from tests.test_publication_v4 import _SOURCE_ID, _diagram, _document, _figure, _source, _table

_SECOND_SOURCE_ID = UUID("00000000-0000-0000-0000-000000000002")
_THIRD_SOURCE_ID = UUID("00000000-0000-0000-0000-000000000003")
_SUBJECT_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
_INJECTION_TEXT = '#import "x"\n$math$ [] {} \\ "—été"'


def _renderer(tmp_path: Path) -> tuple[TypstRenderer, TypstTemplateBundle]:
    entrypoint = tmp_path / "RENDERER" / "publication.typ"
    entrypoint.parent.mkdir(parents=True)
    entrypoint.write_bytes(b'#let publication = json("render-data.json")\n')
    (tmp_path / "renderer-manifest.json").write_text(
        json.dumps({"template_version": "test-v1", "files": ["RENDERER/publication.typ"]}),
        encoding="utf-8",
    )
    return TypstRenderer(), load_template_bundle(tmp_path)


def _ref(source_document_id: UUID, token: str) -> PublicationEvidenceRefV1:
    return PublicationEvidenceRefV1(
        source_document_id=source_document_id,
        kind=PublicationEvidenceKind.FACT,
        evidence_key=token * 64,
    )


def _full_document(
    *,
    tables: tuple = (),
    diagrams: tuple = (),
    figures: tuple = (),
) -> PublicationDocumentV4:
    source_a = replace(
        _source(_SOURCE_ID),
        canonical_url="https://example.test/one",
        title="Primary source",
        publisher="Example Lab",
        published_at=date(2025, 1, 2),
    )
    source_b = replace(
        _source(_SECOND_SOURCE_ID),
        canonical_url="https://example.test/two",
        title=None,
        publisher=None,
        published_at=None,
    )
    source_c = replace(
        _source(_THIRD_SOURCE_ID),
        canonical_url="https://example.test/one",
        title="Duplicate URL source",
        publisher=None,
        published_at=date(2025, 3, 4),
    )
    all_source_refs = (
        _ref(_SOURCE_ID, "1"),
        _ref(_SECOND_SOURCE_ID, "2"),
        _ref(_THIRD_SOURCE_ID, "3"),
    )
    indicators = tuple(
        PublicationIndicatorGroupV1(
            artifact_type,
            (
                PublicationIndicatorV1(
                    value=f"Display {artifact_type.value}",
                    normalized_value=f"normalized-{artifact_type.value}",
                    artifact_type=artifact_type,
                    source_document_ids=(_SOURCE_ID,),
                ),
            ),
        )
        for artifact_type in (
            ArtifactType.IP,
            ArtifactType.DOMAIN,
            ArtifactType.URL,
            ArtifactType.EMAIL,
            ArtifactType.HASH,
        )
    )
    return PublicationDocumentV4(
        schema_version="4",
        subject_id=_SUBJECT_ID,
        publication_language="fr",
        title="Intrusion report",
        lead=(
            PublicationParagraphV1(_INJECTION_TEXT, all_source_refs),
            PublicationParagraphV1("Second lead paragraph", (_ref(_SOURCE_ID, "4"),)),
        ),
        sections=(
            PublicationSectionV1(
                PublicationSectionKind.TECHNICAL,
                _INJECTION_TEXT,
                (PublicationParagraphV1("Section body", (_ref(_SECOND_SOURCE_ID, "5"),)),),
            ),
        ),
        timeline=(
            PublicationTimelineEntryV1(None, None, "No display date", (_ref(_SOURCE_ID, "a"),)),
            PublicationTimelineEntryV1(
                date(2025, 2, 3),
                None,
                "One source event",
                (_ref(_SECOND_SOURCE_ID, "6"),),
            ),
            PublicationTimelineEntryV1(
                None,
                "early 2025",
                "Several source event",
                (
                    _ref(_THIRD_SOURCE_ID, "7"),
                    _ref(_SOURCE_ID, "8"),
                    _ref(_SECOND_SOURCE_ID, "9"),
                ),
            ),
        ),
        indicators=indicators,
        sources=(source_a, source_b, source_c),
        uncertainties=(PublicationUncertaintyV1("Attribution remains uncertain", (_SOURCE_ID,)),),
        tables=tables,
        diagrams=diagrams,
        figures=figures,
    )


def _placement(kind: EnrichmentPlacementKind, section_index: int | None = None):
    return EnrichmentPlacementV1(kind=kind, section_index=section_index)


def _table_at(key: str, title: str, kind: EnrichmentPlacementKind, index: int | None = None):
    return replace(
        _table(key=key),
        title=title,
        caption=_INJECTION_TEXT if key == "table_after_timeline_z_first" else None,
        placement=_placement(kind, index),
    )


def _diagram_at(
    key: str,
    title: str,
    kind: EnrichmentPlacementKind,
    index: int | None = None,
    asset_id: UUID | None = None,
):
    return replace(
        _diagram(asset_id=asset_id or UUID(int=100 + len(key))),
        key=key,
        title=title,
        placement=_placement(kind, index),
    )


def _figure_at(
    key: str,
    caption: str,
    kind: EnrichmentPlacementKind,
    index: int | None = None,
    asset_id: UUID | None = None,
):
    return replace(
        _figure(
            key=key,
            asset_id=asset_id or UUID(int=200 + len(key)),
            caption=caption,
        ),
        placement=_placement(kind, index),
    )


def test_minimal_document_has_complete_empty_sections_and_is_deterministic(tmp_path: Path) -> None:
    renderer, bundle = _renderer(tmp_path)
    (tmp_path / "RENDERER" / "publication.typ").write_bytes(b"changed after bundle load")
    document = _document()

    first = renderer.render(document, bundle)
    second = renderer.render(document, bundle)
    data = json.loads(first.render_data_bytes)

    assert first.source_bytes == b'#let publication = json("render-data.json")\n'
    assert first.source_sha256 == hashlib.sha256(first.source_bytes).hexdigest()
    assert first.render_data_bytes == second.render_data_bytes
    assert first.source_bytes == second.source_bytes
    assert first.render_data_sha256 == second.render_data_sha256
    assert first.media_refs == ()
    assert data["schema_version"] == "typst-publication-model-v8-unified-ioc-rendering"
    references, synthesis = data["content_sections"]
    assert references["type"] == "references"
    assert references["timeline"] == []
    assert references["blocks"] == []
    assert references["sources"] == [
        {
            "title": "Source article",
            "publisher": "Example",
            "date": None,
            "url": "https://example.test/1",
        }
    ]
    assert synthesis == {
        "type": "synthesis",
        "blocks": [{"type": "paragraph", "text": "Initial assessment"}],
    }


def test_chart_projection_uses_svg_media_and_shared_figure_numbering() -> None:
    asset_id = UUID(int=4242)
    chart = PublicationChartV1(
        key="domain_registrations",
        kind=ChartKind.TIMELINE,
        title="Domain registrations",
        caption="Dates reported in the source.",
        placement=EnrichmentPlacementV1(EnrichmentPlacementKind.AFTER_LEAD),
        asset_id=asset_id,
        evidence_refs=(
            ExtractionEvidenceRefV1(
                UUID("00000000-0000-0000-0000-000000000001"),
                EvidenceKind.FACT,
                "a" * 64,
            ),
        ),
    )
    diagram = _diagram_at(
        "numbering-diagram", "Numbering diagram", EnrichmentPlacementKind.AFTER_LEAD
    )
    document = replace(_full_document(figures=(_figure(),), diagrams=(diagram,)), charts=(chart,))

    model = project_publication_to_typst_model(document)
    synthesis = next(item for item in model.content_sections if item["type"] == "synthesis")
    diagram_block = next(item for item in synthesis["blocks"] if item["type"] == "diagram")
    chart_block = next(item for item in synthesis["blocks"] if item["type"] == "chart")
    figure_block = next(item for item in synthesis["blocks"] if item["type"] == "figure")

    assert diagram_block["figure_number"] == 1
    assert chart_block["media_path"] == f"media/{asset_id}.svg"
    assert chart_block["figure_number"] == 2
    assert figure_block["figure_number"] == 3
    chart_media = next(media for media in model.media_refs if media.asset_id == asset_id)
    assert chart_media.expected_kind is MediaAssetKind.CHART_SVG


def test_french_publication_dates_use_first_day_typography() -> None:
    assert _display_date(None, date(2026, 2, 1)) == "1er février 2026"
    assert _display_date(None, date(2026, 2, 4)) == "4 février 2026"


def test_v6_render_model_uses_dated_source_references_and_both_ioc_groups(
    tmp_path: Path,
) -> None:
    publication, *_ = _frontmatter_v6_case()
    renderer, bundle = _renderer(tmp_path)

    model = json.loads(renderer.render(publication, bundle).render_data_bytes)

    assert model["title"] == "[Example actor] Décrit une activité documentée"
    assert "title_spans" not in model
    references, synthesis, annex = model["content_sections"]
    assert references["type"] == "references"
    assert references["timeline"][0]["display_date"] == "10 janvier 2026"
    assert references["timeline"][0]["source_urls"] == ["https://a.example/report"]
    assert references["timeline"][-1]["display_date"] == "Date de publication non précisée"
    assert references["timeline"][-1]["source_urls"] == ["https://missing.example/article"]
    assert synthesis["type"] == "synthesis"
    assert annex["type"] == "technical_annex"
    assert annex["indicators"]["domains"]
    assert set(annex["original_indicators"]["domains"]).issubset(
        set(annex["indicators"]["domains"])
    )
    assert len(annex["indicators"]["domains"]) == len(set(annex["indicators"]["domains"]))
    assert annex["original_indicators"]["domains"] == [
        "context.example",
        "original.example",
    ]


def test_v6_render_model_keeps_original_ioc_metadata_separate_from_merged_values(
    tmp_path: Path,
) -> None:
    publication, *_ = _frontmatter_v6_case()
    original = publication.original_indicators[0].indicators[0]
    renderer, bundle = _renderer(tmp_path)

    model = json.loads(renderer.render(publication, bundle).render_data_bytes)
    annex = model["content_sections"][-1]

    displayed_domains = annex["indicators"]["domains"]
    assert original.value in displayed_domains
    assert len(displayed_domains) == len({value.casefold() for value in displayed_domains})
    assert publication.original_indicators[0].indicators[0] == original
    assert annex["original_indicators"]["domains"] == [
        "context.example",
        "original.example",
    ]


def test_render_ioc_projection_deduplicates_normalized_values_across_groups() -> None:
    publication, *_ = _frontmatter_v6_case()
    original_group = publication.original_indicators[0]

    displayed = _indicator_values((original_group, original_group))

    assert displayed["domains"] == [
        "context.example",
        "original.example",
    ]


def test_full_mapping_preserves_text_timeline_indicators_and_optional_sources(
    tmp_path: Path,
) -> None:
    renderer, bundle = _renderer(tmp_path)
    document = _full_document()
    data = json.loads(renderer.render(document, bundle).render_data_bytes)

    assert data["language"] == "fr"
    assert data["title"] == "Intrusion report"
    references, synthesis, technical_annex = data["content_sections"]
    assert references["type"] == "references"
    assert references["timeline"] == [
        {
            "display_date": "",
            "text": "No display date",
            "source_urls": ["https://example.test/one"],
        },
        {
            "display_date": "3 février 2025",
            "text": "One source event",
            "source_urls": ["https://example.test/two"],
        },
        {
            "display_date": "early 2025",
            "text": "Several source event",
            "source_urls": ["https://example.test/one", "https://example.test/two"],
        },
    ]
    assert technical_annex["type"] == "technical_annex"
    assert technical_annex["indicators"] == {
        "ips": ["Display ip"],
        "domains": ["Display domain"],
        "urls": ["Display url"],
        "emails": ["Display email"],
        "hashes": ["Display hash"],
    }
    assert "uncertainties" not in data
    assert references["sources"] == [
        {
            "title": "Primary source",
            "publisher": "Example Lab",
            "date": "2025-01-02",
            "url": "https://example.test/one",
        },
        {
            "title": None,
            "publisher": None,
            "date": None,
            "url": "https://example.test/two",
        },
        {
            "title": "Duplicate URL source",
            "publisher": None,
            "date": "2025-03-04",
            "url": "https://example.test/one",
        },
    ]
    assert synthesis["blocks"][:2] == [
        {"type": "paragraph", "text": _INJECTION_TEXT},
        {"type": "paragraph", "text": "Second lead paragraph"},
    ]
    assert synthesis["blocks"][2] == {"type": "paragraph", "text": "Section body"}
    assert all(
        block["type"] != "section_heading"
        for section in data["content_sections"]
        for block in section.get("blocks", [])
    )
    assert "—été".encode() in renderer.render(document, bundle).render_data_bytes


def test_internal_section_heading_is_not_projected(tmp_path: Path) -> None:
    renderer, bundle = _renderer(tmp_path)
    document = _full_document()
    document = replace(
        document,
        sections=(replace(document.sections[0], heading="Internal title"),),
    )

    data = json.loads(renderer.render(document, bundle).render_data_bytes)

    synthesis = data["content_sections"][1]
    assert {"type": "paragraph", "text": "Section body"} in synthesis["blocks"]
    assert all(block["type"] != "section_heading" for block in synthesis["blocks"])


def test_v5_projection_maps_semantic_spans_to_closed_typst_helpers(tmp_path: Path) -> None:
    renderer, bundle = _renderer(tmp_path)
    base = _full_document(
        tables=(replace(_table(), caption="APT Étoile used -enc."),),
    )
    command = "`curl '$x' # marker ]`"
    lead_text = f"APT Étoile executed {command} over TCP port 443."
    base = replace(
        base,
        title="APT Étoile response",
        lead=(replace(base.lead[0], text=lead_text), *base.lead[1:]),
    )
    annotator = SemanticAnnotator(EnglishTermDetector(()))
    entities = (
        (SemanticRole.ACTOR, "APT Étoile"),
        (SemanticRole.COMMAND, "-enc"),
        (SemanticRole.TECHNICAL_LITERAL, "443"),
    )
    proposals = (
        SemanticAnnotationProposalV1(
            paragraph_anchor="lead:0001",
            role=SemanticRole.COMMAND,
            text=command,
        ),
    )
    semantic_text = SemanticTextV1(
        schema_version=SEMANTIC_ANNOTATION_SCHEMA_VERSION,
        policy_version=SEMANTIC_ANNOTATION_POLICY_VERSION,
        paragraphs=tuple(
            annotator.annotate_paragraph(
                anchor=anchor,
                text=text,
                entities=entities,
                proposals=proposals,
            )
            for anchor, text in publication_document_text_anchors(base).items()
        ),
    )
    document = PublicationDocumentV5(document=base, semantic_text=semantic_text)

    data = json.loads(renderer.render(document, bundle).render_data_bytes)
    synthesis = data["content_sections"][1]
    paragraph = synthesis["blocks"][0]
    table = next(block for block in synthesis["blocks"] if block["type"] == "table")
    styles = {span["style"] for span in paragraph["semantic_spans"]}
    semantic_cells = table["semantic_cells"]

    assert data["schema_version"] == "typst-publication-model-v8-unified-ioc-rendering"
    assert table["column_weights"] == _table_column_weights(base.tables[0])
    assert all(0.8 <= weight <= 2.4 for weight in table["column_weights"])
    assert paragraph["text"] == lead_text
    assert (
        "".join(span["text"] for span in paragraph["semantic_spans"]).replace("\u200b", "")
        == lead_text
    )
    assert {"semantic-actor", "semantic-command", "semantic-technical-literal"} <= styles
    assert len(semantic_cells) == sum(len(row) for row in table["rows"])
    assert all(isinstance(cell_spans, list) for cell_spans in semantic_cells)
    assert "semantic-table-command" in {span["style"] for span in semantic_cells[0]}
    assert "semantic-actor" in {span["style"] for span in table["semantic_caption"]}


def test_long_iocs_and_semantic_literals_get_render_only_break_opportunities(
    tmp_path: Path,
) -> None:
    renderer, bundle = _renderer(tmp_path)
    long_hash = "a" * 128
    long_path = "/opt/very-long-installation/path/to/a/critical/binary"
    long_command = "powershell.exe -ExecutionPolicy Bypass -EncodedCommand " + "B" * 48
    lead_text = f"Hash {long_hash}; path {long_path}; command {long_command}."
    base = _full_document()
    base = replace(
        base,
        lead=(replace(base.lead[0], text=lead_text), *base.lead[1:]),
        indicators=(
            PublicationIndicatorGroupV1(
                ArtifactType.HASH,
                (
                    PublicationIndicatorV1(
                        value=long_hash,
                        normalized_value=long_hash,
                        artifact_type=ArtifactType.HASH,
                        source_document_ids=(_SOURCE_ID,),
                    ),
                ),
            ),
        ),
    )
    proposals = tuple(
        SemanticAnnotationProposalV1(
            paragraph_anchor="lead:0001",
            role=role,
            text=value,
        )
        for role, value in (
            (SemanticRole.IOC, long_hash),
            (SemanticRole.PATH, long_path),
            (SemanticRole.COMMAND, long_command),
        )
    )
    annotator = SemanticAnnotator(EnglishTermDetector(()))
    semantic_text = SemanticTextV1(
        schema_version=SEMANTIC_ANNOTATION_SCHEMA_VERSION,
        policy_version=SEMANTIC_ANNOTATION_POLICY_VERSION,
        paragraphs=tuple(
            annotator.annotate_paragraph(
                anchor=anchor,
                text=text,
                entities=(),
                proposals=proposals,
            )
            for anchor, text in publication_document_text_anchors(base).items()
        ),
    )
    document = PublicationDocumentV5(document=base, semantic_text=semantic_text)

    data = json.loads(renderer.render(document, bundle).render_data_bytes)
    synthesis = data["content_sections"][1]
    lead = next(
        block
        for block in synthesis["blocks"]
        if block.get("type") == "paragraph" and block.get("text") == lead_text
    )
    spans = {span["style"]: span["text"] for span in lead["semantic_spans"]}

    def break_text(value: str) -> str:
        return "\u200b".join(value[index : index + 16] for index in range(0, len(value), 16))

    assert data["content_sections"][2]["indicators"]["hashes"] == [break_text(long_hash)]
    assert spans["semantic-ioc"] == break_text(long_hash)
    assert spans["semantic-path"] == break_text(long_path)
    assert spans["semantic-command"] == break_text(long_command)
    assert document.document.lead[0].text == lead_text
    assert document.document.indicators[0].indicators[0].value == long_hash
    assert "\u200b" not in lead_text


def test_technical_table_cells_render_monospace_and_break_long_values(
    tmp_path: Path,
) -> None:
    renderer, bundle = _renderer(tmp_path)
    base_table = _table()
    sha256 = "a" * 64
    long_url = (
        "https://downloads.example.test/releases/2026/10/06/"
        "example-rat-loader-package?campaign=autumn-update&source=bulletin"
    )
    cells = (
        ("Contexte", "Deux observations consignées dans le rapport"),
        ("Hash SHA-256", sha256),
        ("URL", long_url),
        ("IP", "198.51.100.42"),
        ("Domaine", "cdn.example.test"),
        ("Chemin", "/opt/example-rat/payloads/loader.bin"),
    )
    table = replace(
        base_table,
        title="Valeurs techniques observées",
        caption="Les valeurs exactes documentées par la source.",
        rows=tuple(replace(base_table.rows[0], cells=row) for row in cells),
    )
    data = json.loads(renderer.render(_full_document(tables=(table,)), bundle).render_data_bytes)
    rendered_table = next(
        block for block in data["content_sections"][1]["blocks"] if block["type"] == "table"
    )
    styles = [
        {span["style"] for span in cell_spans} for cell_spans in rendered_table["semantic_cells"]
    ]
    display_values = [
        "".join(span["text"] for span in cell_spans).replace("\u200b", "")
        for cell_spans in rendered_table["semantic_cells"]
    ]

    assert styles[3] == {"semantic-table-technical"}
    assert styles[5] == {"semantic-table-ioc"}
    assert styles[7] == {"semantic-table-ioc"}
    assert styles[9] == {"semantic-table-ioc"}
    assert styles[11] == {"semantic-table-path"}
    assert display_values[3] == sha256
    assert display_values[5] == long_url
    assert "\u200b" in rendered_table["semantic_cells"][5][0]["text"]
    assert rendered_table["rows"][2][1] == long_url
    assert rendered_table["column_weights"][1] == 2.4
    assert rendered_table["caption"] == "Les valeurs exactes documentées par la source."


def test_lead_is_the_first_synthesis_paragraph_and_repeated_intro_is_suppressed(
    tmp_path: Path,
) -> None:
    renderer, bundle = _renderer(tmp_path)
    document = _full_document()
    document = replace(
        document,
        lead=(replace(document.lead[0], text="Opening assessment."), document.lead[1]),
        sections=(
            replace(
                document.sections[0],
                paragraphs=(
                    replace(
                        document.sections[0].paragraphs[0],
                        text="  OPENING   ASSESSMENT.  ",
                    ),
                ),
            ),
        ),
    )

    data = json.loads(renderer.render(document, bundle).render_data_bytes)
    references, synthesis, _annex = data["content_sections"]
    paragraphs = [block["text"] for block in synthesis["blocks"] if block["type"] == "paragraph"]

    assert [section["type"] for section in data["content_sections"]] == [
        "references",
        "synthesis",
        "technical_annex",
    ]
    assert paragraphs[0] == "Opening assessment."
    assert (
        sum(" ".join(text.casefold().split()) == "opening assessment." for text in paragraphs) == 1
    )
    assert references["type"] == "references"


def test_all_placements_preserve_collection_order_and_type_priority(tmp_path: Path) -> None:
    renderer, bundle = _renderer(tmp_path)
    placement_specs = (
        ("after_timeline", EnrichmentPlacementKind.AFTER_TIMELINE, None),
        ("after_lead", EnrichmentPlacementKind.AFTER_LEAD, None),
        ("after_section", EnrichmentPlacementKind.AFTER_SECTION, 0),
        ("end", EnrichmentPlacementKind.END, None),
    )
    tables = tuple(
        _table_at(f"table_{name}_{suffix}", f"{suffix} table {name}", kind, index)
        for name, kind, index in placement_specs
        for suffix in ("z_first", "a_second")
    )
    diagrams = tuple(
        _diagram_at(
            f"diagram_{name}_{suffix}",
            f"{suffix} diagram {name}",
            kind,
            index,
            UUID(int=1000 + len(name) * 10 + position),
        )
        for name, kind, index in placement_specs
        for position, suffix in enumerate(("z_first", "a_second"))
    )
    figures = tuple(
        _figure_at(
            f"figure_{name}_{suffix}",
            f"{suffix} figure {name}",
            kind,
            index,
            UUID(int=2000 + len(name) * 10 + position),
        )
        for name, kind, index in placement_specs
        for position, suffix in enumerate(("z_first", "a_second"))
    )
    document = _full_document(tables=tables, diagrams=diagrams, figures=figures)
    data = json.loads(renderer.render(document, bundle).render_data_bytes)
    reference_blocks = data["content_sections"][0]["blocks"]
    blocks = data["content_sections"][1]["blocks"]

    expected_by_placement = {
        name: [
            *[f"table_{name}_{suffix}" for suffix in ("z_first", "a_second")],
            *[f"diagram_{name}_{suffix}" for suffix in ("z_first", "a_second")],
            *[f"figure_{name}_{suffix}" for suffix in ("z_first", "a_second")],
        ]
        for name, _, _ in placement_specs
    }
    observed_after_timeline = [block["key"] for block in reference_blocks]
    observed_after_lead = [block["key"] for block in blocks[2:8]]
    section_rich_start = 9
    observed_after_section = [
        block["key"] for block in blocks[section_rich_start : section_rich_start + 6]
    ]
    observed_end = [block["key"] for block in blocks[-6:]]

    assert observed_after_timeline == expected_by_placement["after_timeline"]
    assert observed_after_lead == expected_by_placement["after_lead"]
    assert observed_after_section == expected_by_placement["after_section"]
    assert observed_end == expected_by_placement["end"]
    assert [block["type"] for block in reference_blocks] == [
        "table",
        "table",
        "diagram",
        "diagram",
        "figure",
        "figure",
    ]
    caption_block = next(
        block for block in reference_blocks if block.get("key") == "table_after_timeline_z_first"
    )
    assert caption_block["caption"] == _INJECTION_TEXT
    assert caption_block["columns"] == ["Command", "Purpose"]
    assert caption_block["rows"] == [["-enc", "Execution"]]

    diagram_ref = next(
        ref
        for ref in renderer.render(document, bundle).media_refs
        if ref.asset_id == diagrams[0].asset_id
    )
    assert diagram_ref.expected_kind is MediaAssetKind.DIAGRAM_SVG
    assert diagram_ref.expected_mime_type == "image/svg+xml"
    assert diagram_ref.expected_sha256 is None
    assert diagram_ref.expected_byte_size is None
    assert diagram_ref.media_path.endswith(".svg")


def test_figure_locator_does_not_leak_original_url_and_media_refs_deduplicate(
    tmp_path: Path,
) -> None:
    renderer, bundle = _renderer(tmp_path)
    shared_asset_id = UUID("30000000-0000-4000-8000-000000000001")
    first = _figure_at(
        "figure_one",
        "Caption one",
        EnrichmentPlacementKind.AFTER_LEAD,
        asset_id=shared_asset_id,
    )
    second = replace(
        _figure_at(
            "figure_two",
            "Caption two",
            EnrichmentPlacementKind.END,
            asset_id=shared_asset_id,
        ),
        locator=replace(
            first.locator,
            page=None,
            section=None,
            figure_label=None,
            original_asset_url="https://secret.example/source-image.png",
        ),
    )
    document = _full_document(figures=(first, second))
    rendered = renderer.render(document, bundle)
    data = json.loads(rendered.render_data_bytes)
    figure_blocks = [
        block for block in data["content_sections"][1]["blocks"] if block["type"] == "figure"
    ]

    assert len(rendered.media_refs) == 1
    assert rendered.media_refs[0].asset_id == shared_asset_id
    assert rendered.media_refs[0].expected_kind is MediaAssetKind.SOURCE_FIGURE
    assert rendered.media_refs[0].expected_mime_type == "image/png"
    assert rendered.media_refs[0].expected_sha256 == first.sha256
    assert rendered.media_refs[0].expected_byte_size == first.byte_size
    assert [block["media_path"] for block in figure_blocks] == [
        f"media/{shared_asset_id}.png",
        f"media/{shared_asset_id}.png",
    ]
    assert figure_blocks[0]["locator"] == "page 1"
    assert figure_blocks[1]["locator"] is None
    assert "secret.example" not in rendered.render_data_bytes.decode("utf-8")


def test_empty_evidence_reference_list_maps_to_zero_source_urls() -> None:
    # PublicationTimelineEntryV1 itself requires evidence, but the projection
    # keeps the empty case well-defined for its input boundary.
    assert _timeline_source_urls((), {_SOURCE_ID: _source(_SOURCE_ID)}) == []


@pytest.mark.parametrize(
    ("mime_type", "extension"),
    (
        ("image/svg+xml", ".svg"),
        ("image/png", ".png"),
        ("image/jpeg", ".jpg"),
        ("image/webp", ".webp"),
        ("image/gif", ".gif"),
    ),
)
def test_figure_media_extension_matches_mime_type(
    tmp_path: Path, mime_type: str, extension: str
) -> None:
    renderer, bundle = _renderer(tmp_path)
    figure = replace(
        _figure(key="figure_ext"),
        asset_id=UUID(int=500 + len(extension)),
        mime_type=mime_type,
    )
    rendered = renderer.render(_document(figures=(figure,)), bundle)

    assert rendered.media_refs[0].media_path.endswith(extension)


def test_table_column_weights_are_deterministic_bounded_and_content_sensitive() -> None:
    table = _table(key="layout")
    narrow = replace(
        table,
        columns=(replace(table.columns[0], label="ID"), replace(table.columns[1], label="Note")),
        rows=(replace(table.rows[0], cells=("A1", "Short")),),
    )
    wide = replace(
        narrow,
        rows=(
            replace(
                narrow.rows[0],
                cells=(
                    "A1",
                    "A substantially longer evidence-grounded explanation that should wrap in its "
                    "own wider column rather than forcing equal widths.",
                ),
            ),
        ),
    )

    narrow_weights = _table_column_weights(narrow)
    wide_weights = _table_column_weights(wide)

    assert narrow_weights == _table_column_weights(narrow)
    assert all(0.8 <= weight <= 2.4 for weight in wide_weights)
    assert wide_weights[1] > wide_weights[0]
