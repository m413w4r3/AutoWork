from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import shutil
import struct
import subprocess
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from pypdf import PdfReader

from cti_app.application import typst_rendering as typst_rendering_module
from cti_app.application.opinion_note_proposals import (
    NoteContentBlockContextV1,
    note_article_element_facts,
    validate_note_content_blocks,
)
from cti_app.application.protocol_diagram_projection import PROTOCOL_DIAGRAM_FIGURE_POLICY_VERSION
from cti_app.application.publication_augmentation import (
    OpinionCitationContext,
    augment_publication_with_opinion,
)
from cti_app.application.publication_builder import (
    build_article_format_v1,
    build_publication_document_v5,
)
from cti_app.application.semantic_annotation import EnglishTermDetector, SemanticAnnotator
from cti_app.application.typst_compilation import (
    TypstCompileRequest,
    load_font_bundle_snapshot,
    materialize_font_bundle,
)
from cti_app.application.typst_rendering import (
    _FINDING_ASSESSMENT_LABELS,
    _IOC_DISPOSITION_LABELS,
    _IOC_EVIDENCE_BASIS_LABELS,
    _IOC_GROUP_STATUS_LABELS,
    _IOC_RELEVANCE_CLASSIFICATION_LABELS,
    _IOC_RELEVANCE_REASON_LABELS,
    _IOC_SOURCE_ROLE_LABELS,
    _NOVELTY_CLASS_LABELS,
    NoteBlockRenderContext,
    NoteContentBlockRenderError,
    TemplateBundleInvalidError,
    TypstDraftPreviewDocument,
    TypstRenderer,
    TypstTemplateBundle,
    _breakable_typst_display_text,
    _display_date,
    _draft_finding_ioc_qualification,
    _indicator_values,
    _opinion_pipe_table,
    _opinion_text_spans,
    _opinion_traceability_refs,
    _protocol_figure_brief,
    _table_column_weights,
    _timeline_source_urls,
    load_template_bundle,
    project_publication_to_typst_model,
)
from cti_app.domain.analyst_opinion import (
    AnalystObservationRelation,
    AnalystObservationState,
    AnalystObservationV1,
    AnalystOpinionStatus,
    AnalystOpinionV1,
    ArticleElementNoteBlockV1,
    BridgeDraftReferenceV1,
    CanonicalCodeNoteBlockV1,
    ImplantMaterialEditorialV1,
    ImplantMaterialType,
    IocTableNoteBlockV1,
    NoteArticleElementKind,
    OpinionBlock,
    OpinionBlockType,
    OpinionClaimQualification,
    OpinionProtocolFigureV1,
    ProtocolFigureNoteBlockV1,
    note_ioc_table_title,
    opinion_blocks_with_implant_materials,
    opinion_blocks_with_observations,
)
from cti_app.domain.discovery import SourceRole
from cti_app.domain.editorial_figure_keys import ADOPTED_SOURCE_FIGURE_KEY_PREFIX
from cti_app.domain.investigation import (
    EvidenceKind as ObservationEvidenceKind,
)
from cti_app.domain.investigation import (
    EvidenceObservation,
    FindingAssessment,
    FindingKind,
    NoveltyClass,
)
from cti_app.domain.media_assets import MediaAssetKind, media_asset_id
from cti_app.domain.production import ProductionEvidenceBasis
from cti_app.domain.production_editorial_enrichment import (
    ChartKind,
    EnrichmentPlacementKind,
    EnrichmentPlacementV1,
    ResolvedSourceFigureV1,
    SourceFigureCandidateV1,
    SourceFigureDecision,
    SourceFigureInclusionStatus,
    SourceFigureLocatorV1,
    source_figure_id,
)
from cti_app.domain.production_references import ProductionEditorialRole
from cti_app.domain.production_relevance import RelevanceClassification, RelevanceReasonCode
from cti_app.domain.production_synthesis import (
    EvidenceKind,
    ExtractionEvidenceRefV1,
    extraction_evidence_refs_v1,
)
from cti_app.domain.protocol_model import ProtocolDiagramKind
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
    PublicationDocumentV6,
    PublicationDocumentV7,
    PublicationDocumentV8,
    PublicationDocumentV9,
    PublicationIOCDetailV1,
    PublicationIOCDisposition,
    PublicationTableColumnV1,
    PublicationTableRowV1,
    ResolvedPublicationPlacementV1,
    publication_document_from_json,
    publication_document_text_anchors,
    publication_document_v5_sha256,
    publication_element_sha256,
    serialize_publication_document,
)
from cti_app.domain.publication_kind import PublicationKind
from cti_app.domain.semantic_annotation import (
    SEMANTIC_ANNOTATION_POLICY_VERSION,
    SEMANTIC_ANNOTATION_SCHEMA_VERSION,
    SemanticAnnotationProposalV1,
    SemanticRole,
    SemanticTextV1,
)
from cti_app.infrastructure.typst_compiler import TypstSubprocessCompiler
from tests.editorial_enrichment_support import build_empty_editorial_enrichment
from tests.test_publication_builder_v4 import (
    _canonical_inputs,
    _enrichment_with,
    _frontmatter_v6_case,
)
from tests.test_publication_builder_v4 import (
    _chart as _enrichment_chart,
)
from tests.test_publication_builder_v4 import (
    _table as _enrichment_table,
)
from tests.test_publication_document_v6 import _approved_opinion, _base_v5
from tests.test_publication_document_v7 import _v6
from tests.test_publication_v4 import (
    _SOURCE_ID,
    _diagram,
    _document,
    _figure,
    _source,
    _table,
)

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


def _render(renderer: TypstRenderer, document, bundle: TypstTemplateBundle):
    return renderer.render(document, bundle, document_label="Libellé de fixture")


def _ref(source_document_id: UUID, token: str) -> PublicationEvidenceRefV1:
    return PublicationEvidenceRefV1(
        source_document_id=source_document_id,
        kind=PublicationEvidenceKind.FACT,
        evidence_key=token * 64,
    )


def test_opinion_pipe_table_parser_handles_alignment_inline_code_and_safe_limits() -> None:
    hash_value = "a" * 64
    parsed = _opinion_pipe_table(
        "Context before the table.\n"
        "| Artifact | SHA-256 | Location |\n"
        "| :--- | ---: | :---: |\n"
        f"| `sample.bin` | {hash_value} | `dir/sample.bin` |\n"
        "Notes after the table."
    )

    assert parsed is not None
    assert parsed["prefix"] == "Context before the table.\n"
    assert parsed["suffix"] == "Notes after the table."
    assert parsed["alignment"] == ["left", "right", "center"]
    assert parsed["wide"] is True
    assert parsed["header"][0]["segments"][0]["text"] == "Artifact"
    assert parsed["rows"][0][0]["segments"][0]["code"] is True
    assert parsed["rows"][0][0]["segments"][0]["text"] == "sample.bin"
    assert parsed["rows"][0][1]["text"] == hash_value

    assert _opinion_pipe_table("| A | B |\n| --- |\n| x | y |") is None
    assert _opinion_pipe_table("| A |\n| --- |\n| unmatched `code |") is None
    too_many_columns = "|" + "|".join(f"C{index}" for index in range(9)) + "|\n"
    too_many_columns += "|" + "|".join("---" for _ in range(9)) + "|"
    assert _opinion_pipe_table(too_many_columns) is None
    too_many_rows = "| A |\n| --- |\n" + "| row |\n" * 201
    assert _opinion_pipe_table(too_many_rows) is None


def test_opinion_text_spans_projects_only_balanced_fences_and_preserves_literals() -> None:
    code_text = '#import "inert-fixture.typ" $x$ @name <tag> & `literal`\n#set page(margin: 0pt)'
    source = f"Before `inline`.\n```yara\n{code_text}\n```\nAfter."

    spans = _opinion_text_spans(source, parse_fenced_code=True)

    assert spans[0]["text"] == "Before "
    assert spans[1]["text"] == "inline"
    assert spans[1]["technical"] is True
    code_span = next(span for span in spans if span.get("code_block"))
    assert code_span["text"] == code_text + "\n"
    assert code_span["technical"] is True
    assert [line["text"] for line in code_span["code_lines"]] == [
        '#import "inert-fixture.typ" $x$ @name <tag> & `literal`',
        "#set page(margin: 0pt)",
        "",
    ]
    assert spans[-1]["text"] == "After."

    unclosed = 'Before `inline`.\n```yara\n#import "still inert.typ" $x$ @name'
    unclosed_spans = _opinion_text_spans(unclosed, parse_fenced_code=True)
    assert not any(span.get("code_block") for span in unclosed_spans)
    assert unclosed_spans[-1] == {
        "text": '```yara\n#import "still inert.typ" $x$ @name',
        "technical": False,
    }

    malformed = "Before.\n````\nnot a triple fence\n````"
    malformed_spans = _opinion_text_spans(malformed, parse_fenced_code=True)
    assert not any(span.get("code_block") for span in malformed_spans)
    assert "````" in "".join(span["text"] for span in malformed_spans)

    # Other renderers use the same parser for observations; fences remain plain text there.
    observation_spans = _opinion_text_spans(source)
    assert not any(span.get("code_block") for span in observation_spans)
    assert "```" in "".join(span["text"] for span in observation_spans)


def test_opinion_inline_code_keeps_neighboring_prose_spaces_outside_technical_span() -> None:
    spans = _opinion_text_spans("Before `--safe` after")

    assert [(span["text"], span["technical"]) for span in spans] == [
        ("Before ", False),
        ("--safe", True),
        (" after", False),
    ]


def test_opinion_text_spans_refang_known_iocs_outside_fenced_code() -> None:
    known_iocs = (
        (ArtifactType.IP, "203.0.113[.]94", "203.0.113.94"),
        (ArtifactType.DOMAIN, "updates.example[.]com", "updates.example.com"),
    )
    long_hex = "AA" * 48
    code = (
        'rule synthetic_endpoint { strings: $ip = "203.0.113[.]94" $hex = { '
        + long_hex
        + " } condition: $ip and $hex }"
    )
    source = (
        "Endpoint `203.0.113[.]94:80` and 203.0.113[.]94:80. "
        f"Domain `updates.example[.]com:443`.\n```yara\n{code}\n```"
    )

    spans = _opinion_text_spans(
        source,
        parse_fenced_code=True,
        known_iocs=known_iocs,
    )

    prose = "".join(span["text"] for span in spans if not span.get("code_block"))
    fenced = next(span for span in spans if span.get("code_block"))
    assert "203.0.113.94:80" in prose
    assert "updates.example.com:443" in prose
    assert fenced["text"] == code + "\n"
    assert fenced["code_lines"][0]["text"] == code
    assert "".join(fenced["code_lines"][0]["break_chunks"]) == code
    assert long_hex in "".join(fenced["code_lines"][0]["break_chunks"])
    assert "203.0.113[.]94" in source

    ordinary_text = "Ordinary `inline` prose without an IOC or fence."
    assert _opinion_text_spans(
        ordinary_text,
        parse_fenced_code=True,
        known_iocs=known_iocs,
    ) == _opinion_text_spans(ordinary_text)

    long_identifier = "C2-" + "A" * 96
    long_identifier_span = next(
        span
        for span in _opinion_text_spans("Endpoint " + long_identifier)
        if span["text"] == "Endpoint " + long_identifier
    )
    assert "".join(long_identifier_span["break_chunks"]) == "Endpoint " + long_identifier
    assert len(long_identifier_span["break_chunks"]) > 1


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


def _legacy_v9_source_figure_document() -> PublicationDocumentV9:
    snapshot, references, extraction, synthesis = _canonical_inputs()
    source = extraction.sources[0]
    content = b"synthetic archived legacy figure"
    sha256 = hashlib.sha256(content).hexdigest()
    locator = SourceFigureLocatorV1(page=1, section="Synthetic legacy section")
    figure_id = source_figure_id(
        source_document_id=source.source_document_id,
        sha256=sha256,
        source=source.canonical_url,
        locator=locator,
    )
    resolved = ResolvedSourceFigureV1(
        figure_id=figure_id,
        blob_id=UUID(int=7101),
        sha256=sha256,
        mime_type="image/png",
        byte_size=len(content),
        source_document_id=source.source_document_id,
        source=source.canonical_url,
        provenance="Synthetic archival provenance.",
        locator=locator,
        decision=SourceFigureDecision.ACCEPTED,
        decision_reason="archived_blob_verified",
    )
    candidate = SourceFigureCandidateV1(
        key="source_figure_historical_1",
        source_document_id=source.source_document_id,
        source_url=source.canonical_url,
        caption="Historical figure caption",
        provenance=resolved.provenance,
        locator=locator,
        inclusion_status=SourceFigureInclusionStatus.INCLUDED,
        placement=EnrichmentPlacementV1(kind=EnrichmentPlacementKind.AFTER_LEAD),
        resolved_figure=resolved,
        width=None,
        height=None,
        order=0,
    )
    enrichment = replace(
        build_empty_editorial_enrichment(extraction=extraction, synthesis=synthesis),
        source_figures=(candidate,),
    )
    v5 = build_publication_document_v5(
        snapshot=snapshot,
        references=references,
        extraction=extraction,
        synthesis=synthesis,
        editorial_enrichment=enrichment,
    )
    v7 = PublicationDocumentV7(
        base_publication=_v6(v5),
        publication_kind=PublicationKind.ARTICLE,
        routing_decision_hash="a" * 64,
        routing_policy_version="routing-policy-v1",
    )
    return PublicationDocumentV9(
        base_publication=v7,
        article_format=build_article_format_v1(v5, extraction),
        registry_hash="b" * 64,
        synthesis_canonical_sha256="c" * 64,
        enrichment_canonical_sha256="d" * 64,
        resolved_placements=(),
    )


def test_legacy_v9_figure_render_data_matches_frozen_historical_bytes(
    tmp_path: Path,
) -> None:
    renderer, bundle = _renderer(tmp_path)
    rendered = _render(renderer, _legacy_v9_source_figure_document(), bundle)
    historical_bytes = (
        Path(__file__).parent / "fixtures" / "legacy_v9_figure_render_data.json"
    ).read_bytes()

    assert rendered.render_data_bytes == historical_bytes
    render_data = json.loads(rendered.render_data_bytes)
    figure_blocks = [
        block
        for section in render_data["content_sections"]
        for block in section["blocks"]
        if block["type"] == "figure"
    ]
    assert render_data["schema_version"] == "typst-publication-model-v41-after-paragraph-anchors"
    assert len(figure_blocks) == 1
    assert not {"width", "height"}.intersection(figure_blocks[0])


def test_adopted_source_figure_render_block_includes_dimensions() -> None:
    figure = replace(
        _figure_at("adopted", "Adopted caption", EnrichmentPlacementKind.AFTER_LEAD),
        key=f"{ADOPTED_SOURCE_FIGURE_KEY_PREFIX}fixture",
        width=640,
        height=480,
    )
    block = typst_rendering_module._figure_block(
        figure,
        _source(_SOURCE_ID),
        lambda **_kwargs: SimpleNamespace(media_path="media/adopted.png"),
    )

    assert block["width"] == 640
    assert block["height"] == 480


def test_v9_renders_after_the_registered_paragraph_and_v8_keeps_v40(tmp_path: Path) -> None:
    renderer, bundle = _renderer(tmp_path)
    snapshot, references, extraction, synthesis = _canonical_inputs()
    anchor_id = UUID(int=777)
    table = replace(
        _enrichment_table(
            extraction_evidence_refs_v1(extraction)[0], key="after-registered-paragraph"
        ),
        placement=EnrichmentPlacementV1(
            EnrichmentPlacementKind.AFTER_PARAGRAPH,
            anchor_block_id=anchor_id,
        ),
    )
    anchored_enrichment = replace(
        build_empty_editorial_enrichment(extraction=extraction, synthesis=synthesis),
        tables=(table,),
    )
    anchored_v5 = build_publication_document_v5(
        snapshot=snapshot,
        references=references,
        extraction=extraction,
        synthesis=synthesis,
        editorial_enrichment=anchored_enrichment,
    )
    v7 = PublicationDocumentV7(
        base_publication=_v6(anchored_v5),
        publication_kind=PublicationKind.ARTICLE,
        routing_decision_hash="a" * 64,
        routing_policy_version="routing-policy-v1",
    )
    v9 = PublicationDocumentV9(
        base_publication=v7,
        article_format=build_article_format_v1(anchored_v5, extraction),
        registry_hash="b" * 64,
        synthesis_canonical_sha256="c" * 64,
        enrichment_canonical_sha256="d" * 64,
        resolved_placements=(
            ResolvedPublicationPlacementV1(
                element_kind="table",
                element_key=table.key,
                element_position=0,
                anchor_block_id=anchor_id,
                anchor_pointer="/lead/0",
                anchor_canonical_position=0,
                paragraph_kind="lead",
                section_index=None,
                paragraph_index=0,
            ),
        ),
    )
    v9_render = json.loads(_render(renderer, v9, bundle).render_data_bytes)
    legacy_table = replace(
        table,
        placement=EnrichmentPlacementV1(EnrichmentPlacementKind.AFTER_LEAD),
    )
    legacy_enrichment = replace(anchored_enrichment, tables=(legacy_table,))
    legacy_v5 = build_publication_document_v5(
        snapshot=snapshot,
        references=references,
        extraction=extraction,
        synthesis=synthesis,
        editorial_enrichment=legacy_enrichment,
    )
    v8 = PublicationDocumentV8(
        base_publication=PublicationDocumentV7(
            base_publication=_v6(legacy_v5),
            publication_kind=PublicationKind.ARTICLE,
            routing_decision_hash="a" * 64,
            routing_policy_version="routing-policy-v1",
        ),
        article_format=build_article_format_v1(legacy_v5, extraction),
    )
    v8_render = json.loads(_render(renderer, v8, bundle).render_data_bytes)

    assert v9_render["schema_version"] == "typst-publication-model-v41-after-paragraph-anchors"
    v9_blocks = next(item for item in v9_render["content_sections"] if item["type"] == "synthesis")[
        "blocks"
    ]
    assert [item.get("key", item["type"]) for item in v9_blocks[:2]] == [
        "paragraph",
        "after-registered-paragraph",
    ]
    assert v8_render["schema_version"] == "typst-publication-model-v40-m21-opinion-render"


def test_real_typst_multi_figure_pdf_uses_local_assets_after_anchor(
    tmp_path: Path,
) -> None:
    binary = os.environ.get("TYPST_BINARY") or str(
        Path.home() / ".local" / "share" / "autowork-tools" / "typst"
    )
    if shutil.which(binary) is None and not Path(binary).is_file():
        pytest.skip("pinned Typst 0.15.1 is unavailable")
    version = subprocess.run(
        (binary, "--version"), capture_output=True, check=True, text=True, timeout=10
    ).stdout.strip()
    assert version.startswith("typst 0.15.1"), version

    repository_root = Path(__file__).resolve().parents[2]
    template_root = repository_root / "chpTypst"
    image_paths = (
        template_root / "UTILS" / "chap.png",
        repository_root / "docs" / "mission01" / "typst-captures" / "current" / "page-1.png",
    )
    image_bytes = tuple(path.read_bytes() for path in image_paths)
    snapshot, references, extraction, synthesis = _canonical_inputs()
    source = extraction.sources[0]
    anchor_id = UUID(int=777)
    placement = EnrichmentPlacementV1(
        kind=EnrichmentPlacementKind.AFTER_PARAGRAPH,
        anchor_block_id=anchor_id,
    )
    figure_candidates: list[SourceFigureCandidateV1] = []
    asset_bytes_by_id: dict[UUID, bytes] = {}
    for position, content in enumerate(image_bytes):
        assert content.startswith(b"\x89PNG\r\n\x1a\n")
        width, height = struct.unpack(">II", content[16:24])
        content_sha256 = hashlib.sha256(content).hexdigest()
        locator = SourceFigureLocatorV1(
            page=position + 1,
            section="Asset local de démonstration",
            figure_label=f"Figure locale {position + 1}",
        )
        figure_id = source_figure_id(
            source_document_id=source.source_document_id,
            sha256=content_sha256,
            source=source.canonical_url,
            locator=locator,
        )
        asset_id = media_asset_id(content_sha256, "image/png")
        provenance = (
            "Asset du dépôt local utilisé comme substitut de rendu; "
            "ce document ne constitue pas une preuve CTI."
        )
        resolved = ResolvedSourceFigureV1(
            figure_id=figure_id,
            blob_id=UUID(int=7101 + position),
            sha256=content_sha256,
            mime_type="image/png",
            byte_size=len(content),
            source_document_id=source.source_document_id,
            source=source.canonical_url,
            provenance=provenance,
            locator=locator,
            decision=SourceFigureDecision.ACCEPTED,
            decision_reason="archived_blob_verified",
        )
        figure_candidates.append(
            SourceFigureCandidateV1(
                key=f"{ADOPTED_SOURCE_FIGURE_KEY_PREFIX}demo_{position + 1}",
                source_document_id=source.source_document_id,
                source_url=source.canonical_url,
                caption=f"Figure de démonstration locale {position + 1}",
                provenance=provenance,
                locator=locator,
                inclusion_status=SourceFigureInclusionStatus.INCLUDED,
                placement=placement,
                resolved_figure=resolved,
                width=width,
                height=height,
                order=position,
            )
        )
        asset_bytes_by_id[asset_id] = content

    anchored_enrichment = replace(
        build_empty_editorial_enrichment(extraction=extraction, synthesis=synthesis),
        source_figures=tuple(figure_candidates),
    )
    anchored_v5 = build_publication_document_v5(
        snapshot=snapshot,
        references=references,
        extraction=extraction,
        synthesis=synthesis,
        editorial_enrichment=anchored_enrichment,
    )
    v7 = PublicationDocumentV7(
        base_publication=_v6(anchored_v5),
        publication_kind=PublicationKind.ARTICLE,
        routing_decision_hash="a" * 64,
        routing_policy_version="routing-policy-v1",
    )
    figures = anchored_v5.document.figures
    article_format = build_article_format_v1(anchored_v5, extraction)
    v9 = PublicationDocumentV9(
        base_publication=v7,
        article_format=article_format,
        registry_hash="b" * 64,
        synthesis_canonical_sha256="c" * 64,
        enrichment_canonical_sha256="d" * 64,
        resolved_placements=tuple(
            ResolvedPublicationPlacementV1(
                element_kind="figure",
                element_key=figure.key,
                element_position=position,
                anchor_block_id=anchor_id,
                anchor_pointer="/lead/0",
                anchor_canonical_position=0,
                paragraph_kind="lead",
                section_index=None,
                paragraph_index=0,
            )
            for position, figure in enumerate(figures)
        ),
    )
    bundle = load_template_bundle(template_root)
    rendered = TypstRenderer().render(
        v9,
        bundle,
        document_label="Démonstration locale multi-figures",
    )
    render_data = json.loads(rendered.render_data_bytes)
    assert render_data["schema_version"] == (
        "typst-publication-model-v43-adopted-source-figure-notes"
    )
    synthesis_section = next(
        section for section in render_data["content_sections"] if section["type"] == "synthesis"
    )
    blocks = synthesis_section["blocks"]
    anchor_position = next(
        index
        for index, block in enumerate(blocks)
        if block["type"] == "paragraph" and block.get("anchor_target") is not None
    )
    figure_positions = [index for index, block in enumerate(blocks) if block["type"] == "figure"]
    assert figure_positions == [anchor_position + 1, anchor_position + 2]
    figure_blocks = [block for block in blocks if block["type"] == "figure"]
    assert [(block["width"], block["height"]) for block in figure_blocks] == [
        (figure.width, figure.height) for figure in figures
    ]
    assert len(rendered.media_refs) == 2
    assert all(ref.media_path.startswith("media/") for ref in rendered.media_refs)
    assert all(
        "Source : example.com" in block["caption"] for block in blocks if block["type"] == "figure"
    )

    legacy_candidates = tuple(
        replace(candidate, key=f"source_figure_demo_{position + 1}")
        for position, candidate in enumerate(figure_candidates)
    )
    legacy_v5 = build_publication_document_v5(
        snapshot=snapshot,
        references=references,
        extraction=extraction,
        synthesis=synthesis,
        editorial_enrichment=replace(anchored_enrichment, source_figures=legacy_candidates),
    )
    legacy_v7 = PublicationDocumentV7(
        base_publication=_v6(legacy_v5),
        publication_kind=PublicationKind.ARTICLE,
        routing_decision_hash="a" * 64,
        routing_policy_version="routing-policy-v1",
    )
    legacy_figures = legacy_v5.document.figures
    legacy_v9 = PublicationDocumentV9(
        base_publication=legacy_v7,
        article_format=build_article_format_v1(legacy_v5, extraction),
        registry_hash="b" * 64,
        synthesis_canonical_sha256="c" * 64,
        enrichment_canonical_sha256="d" * 64,
        resolved_placements=tuple(
            ResolvedPublicationPlacementV1(
                element_kind="figure",
                element_key=figure.key,
                element_position=position,
                anchor_block_id=anchor_id,
                anchor_pointer="/lead/0",
                anchor_canonical_position=0,
                paragraph_kind="lead",
                section_index=None,
                paragraph_index=0,
            )
            for position, figure in enumerate(legacy_figures)
        ),
    )
    legacy_render_data = json.loads(
        TypstRenderer()
        .render(
            legacy_v9,
            bundle,
            document_label="Legacy source figure projection",
        )
        .render_data_bytes
    )
    legacy_blocks = next(
        section
        for section in legacy_render_data["content_sections"]
        if section["type"] == "synthesis"
    )["blocks"]
    assert legacy_render_data["schema_version"] == (
        "typst-publication-model-v41-after-paragraph-anchors"
    )
    assert all(
        "Source :" not in block["caption"] for block in legacy_blocks if block["type"] == "figure"
    )

    workspace = tmp_path / "workspace"
    for template_file in bundle.files:
        destination = workspace / template_file.relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(template_file.content)
    render_data_path = workspace / rendered.render_data_relative_path
    render_data_path.parent.mkdir(parents=True, exist_ok=True)
    render_data_path.write_bytes(rendered.render_data_bytes)
    for media_ref in rendered.media_refs:
        media_path = workspace / "RENDERER" / media_ref.media_path
        media_path.parent.mkdir(parents=True, exist_ok=True)
        media_path.write_bytes(asset_bytes_by_id[media_ref.asset_id])

    font_root = Path(os.environ.get("FONT_BUNDLE_ROOT", str(template_root)))
    fonts_lock = Path(
        os.environ.get(
            "TYPST_FONTS_LOCK_FILE",
            str(repository_root / "infra" / "typst-fonts.lock"),
        )
    )
    font_snapshot = load_font_bundle_snapshot(font_root, fonts_lock)
    font_paths = materialize_font_bundle(font_snapshot, tmp_path / "fonts")
    compiled = asyncio.run(
        TypstSubprocessCompiler(binary=binary).compile(
            TypstCompileRequest(
                workspace_root=workspace,
                entrypoint_relative_path=rendered.entrypoint_relative_path,
                font_paths=font_paths,
            )
        )
    )
    demo_output = os.environ.get("AUTOWORK_T_M6_25_DEMO_PDF")
    if demo_output is not None:
        output_path = Path(demo_output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(compiled.content)
    assert compiled.media_type == "application/pdf"
    pdf = PdfReader(io.BytesIO(compiled.content), strict=True)
    assert pdf.pages
    pdf_text = " ".join((page.extract_text() or "") for page in pdf.pages)
    assert "Figure de démonstration locale 1" in pdf_text
    assert "Figure de démonstration locale 2" in pdf_text
    assert "Source : example.com" in pdf_text


def test_minimal_document_has_complete_empty_sections_and_is_deterministic(tmp_path: Path) -> None:
    renderer, bundle = _renderer(tmp_path)
    (tmp_path / "RENDERER" / "publication.typ").write_bytes(b"changed after bundle load")
    document = _document()

    first = _render(renderer, document, bundle)
    second = _render(renderer, document, bundle)
    data = json.loads(first.render_data_bytes)

    assert first.source_bytes == b'#let publication = json("render-data.json")\n'
    assert first.source_sha256 == hashlib.sha256(first.source_bytes).hexdigest()
    assert first.render_data_bytes == second.render_data_bytes
    assert first.source_bytes == second.source_bytes
    assert first.render_data_sha256 == second.render_data_sha256
    assert first.media_refs == ()
    assert data["schema_version"] == "typst-publication-model-v40-m21-opinion-render"
    assert data["kind"] == "brief"
    assert data["article_format"] is None
    assert data["document_label"] == "Libellé de fixture"
    assert data["document_header"] == "Publication"
    assert data["overview"] == {"status_label": "Publication", "source_count": 1}
    assert data["brand_logo_available"] is False
    assert "classification_label" not in data
    references, synthesis = data["content_sections"]
    assert references["type"] == "references"
    assert len(references["entries"]) == 1
    assert references["entries"][0]["source_urls"] == ["https://example.test/1"]
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
        "blocks": [
            {
                "type": "paragraph",
                "text": "Initial assessment",
                "anchor_target": "publication-paragraph-1c4decbc8d241b506463",
            }
        ],
    }


def test_renderer_preserves_sample_cryptographic_material_in_clear(tmp_path: Path) -> None:
    renderer, bundle = _renderer(tmp_path)
    document = _document()
    sample_value = "RC4 passphrase = synthetic-render-passphrase-021"
    document = replace(
        document,
        lead=(replace(document.lead[0], text=sample_value), *document.lead[1:]),
    )

    rendered = _render(renderer, document, bundle)
    assert sample_value in rendered.render_data_bytes.decode("utf-8")


def test_standalone_render_input_uses_explicit_v7_publication_kind(tmp_path: Path) -> None:
    from cti_app.domain.publication_document import PublicationDocumentV7
    from cti_app.domain.publication_kind import PublicationKind
    from tests.test_publication_document_v6 import _base_v5
    from tests.test_publication_document_v7 import _v6

    renderer, bundle = _renderer(tmp_path)
    base = _base_v5()
    document = PublicationDocumentV7(
        base_publication=_v6(base),
        publication_kind=PublicationKind.BRIEF,
        routing_decision_hash="a" * 64,
        routing_policy_version="routing-policy-v1",
    )

    data = json.loads(_render(renderer, document, bundle).render_data_bytes)

    assert data["kind"] == "brief"


def test_opinion_github_tokens_and_typed_rc4_material_reach_publication_render_data(
    tmp_path: Path,
) -> None:
    renderer, bundle = _renderer(tmp_path)
    publication, *_ = _frontmatter_v6_case()
    rc4_key = bytes(range(16)).hex()
    ghp_token = "ghp_" + "a" * 36
    fine_grained_token = "github_pat_" + "b" * 82
    sample_sha256 = "c" * 64
    observation_id = uuid4()
    materials = (
        ImplantMaterialEditorialV1(
            type=ImplantMaterialType.RC4_KEY,
            value=rc4_key,
            role="Déchiffrement des données de configuration",
            sample_location="0x120-0x12f",
            sample_sha256=sample_sha256,
            evidence_observation_id=observation_id,
        ),
        ImplantMaterialEditorialV1(
            type=ImplantMaterialType.ATTACKER_GITHUB_C2_TOKEN,
            value=fine_grained_token,
            role="Canal C2 via l'API GitHub",
            sample_location="0x500-0x55f",
            sample_sha256=sample_sha256,
            evidence_observation_id=observation_id,
        ),
    )
    opinion = AnalystOpinionV1(
        investigation_id=uuid4(),
        synthesis_artifact_id=uuid4(),
        synthesis_sha256="a" * 64,
        base_publication_artifact_id=uuid4(),
        base_publication_sha256=publication_document_v5_sha256(publication),
        blocks=opinion_blocks_with_implant_materials(
            (
                OpinionBlock(
                    OpinionBlockType.KEY_FINDINGS,
                    f"L'implant contient les jetons {ghp_token} et {fine_grained_token} "
                    "pour son canal C2.",
                ),
                OpinionBlock(
                    OpinionBlockType.NETWORK_AND_INFRASTRUCTURE,
                    "Run `token`.",
                    finding_refs=(uuid4(),),
                    evidence_refs=(observation_id,),
                    certainty=0.9,
                ),
            ),
            materials,
        ),
        author_id="fixture-analyst",
    )
    preview = TypstDraftPreviewDocument(base_publication=publication, analyst_opinion=opinion)

    rendered = _render(renderer, preview, bundle)
    data = json.loads(rendered.render_data_bytes)
    sections = {item["type"]: item for item in data["content_sections"]}
    opinion_text = sections["analyst_opinion"]["blocks"][0]["text"]
    code_block = next(
        item for item in sections["analyst_opinion"]["blocks"] if item["text"] == "Run `token`."
    )
    material_items = sections["technical_materials"]["items"]

    assert ghp_token in opinion_text
    assert fine_grained_token in opinion_text
    assert code_block["text_spans"] == [
        {"text": "Run ", "technical": False},
        {"text": "token", "break_chunks": [], "technical": True},
        {"text": ".", "technical": False},
    ]
    assert code_block["qualification_label"] == "Fait étayé"
    assert code_block["finding_refs"][0]["label"] == "[F1]"
    assert code_block["evidence_refs"][0]["label"] == "[E1]"
    assert "certainty_percent" not in code_block
    assert material_items[0]["value"] == rc4_key
    assert material_items[0]["type_label"] == "Clé RC4"
    assert material_items[0]["role"] == "Déchiffrement des données de configuration"
    assert material_items[0]["sample_location"] == "0x120-0x12f"
    assert "sample_sha256" not in material_items[0]
    assert material_items[0]["evidence_ref"]["label"] == "[E1]"
    assert "technical_annex" not in sections
    assert any(item["value"] == fine_grained_token for item in material_items)


def test_typst_opinion_text_keeps_reservations_and_excludes_bridge_audit_metadata(
    tmp_path: Path,
) -> None:
    renderer, bundle = _renderer(tmp_path)
    publication, *_ = _frontmatter_v6_case()
    text = "Réserves : attribution non déterminée; protocole non testé."
    invocation_id = str(uuid4())
    opinion = AnalystOpinionV1(
        investigation_id=uuid4(),
        synthesis_artifact_id=uuid4(),
        synthesis_sha256="a" * 64,
        base_publication_artifact_id=uuid4(),
        base_publication_sha256=publication_document_v5_sha256(publication),
        blocks=(
            OpinionBlock(
                block_type=OpinionBlockType.UNCERTAINTIES_AND_NEXT_LEADS,
                text=text,
                bridge_authored=True,
                bridge_draft_ref=BridgeDraftReferenceV1(
                    invocation_id,
                    hashlib.sha256(text.encode()).hexdigest(),
                    source_text_sha256=hashlib.sha256(text.encode()).hexdigest(),
                    source_observation_id=UUID(int=707),
                ),
            ),
        ),
        author_id="fixture-analyst",
    )

    rendered = _render(
        renderer,
        TypstDraftPreviewDocument(base_publication=publication, analyst_opinion=opinion),
        bundle,
    )
    data_text = rendered.render_data_bytes.decode("utf-8")
    data = json.loads(data_text)
    opinion_section = next(
        item for item in data["content_sections"] if item["type"] == "analyst_opinion"
    )
    opinion_text = opinion_section["blocks"][0]["text"]

    assert "non déterminée" in opinion_text
    assert "non testé" in opinion_text
    assert "bridge" not in opinion_text.casefold()
    assert invocation_id not in opinion_text
    assert str(UUID(int=707)) not in opinion_text
    assert "bridge_authored" not in data_text
    assert invocation_id not in data_text
    assert str(UUID(int=707)) not in data_text


def test_opinion_projection_refangs_blocks_and_notes_only_for_display(tmp_path: Path) -> None:
    renderer, bundle = _renderer(tmp_path)
    publication, *_ = _frontmatter_v6_case()
    block_text = "C2 indiqué : `203.0.113[.]94:80` et 203.0.113[.]94:80."
    note_text = (
        "Note: `203.0.113[.]94:80`.\n```yara\n"
        'rule synthetic_endpoint { strings: $ip = "203.0.113[.]94" condition: $ip }\n```'
    )
    publication_anchors = publication_document_text_anchors(
        publication.document,
        references=publication.references,
    )
    note = AnalystObservationV1(
        id=UUID(int=710),
        anchor=next(
            anchor for anchor in publication_anchors if anchor.startswith(("lead:", "section:"))
        ),
        text=note_text,
        state=AnalystObservationState.UNRESOLVED,
        relation=AnalystObservationRelation.UNRESOLVED,
    )
    opinion = AnalystOpinionV1(
        investigation_id=uuid4(),
        synthesis_artifact_id=uuid4(),
        synthesis_sha256="a" * 64,
        base_publication_artifact_id=uuid4(),
        base_publication_sha256=publication_document_v5_sha256(publication),
        blocks=opinion_blocks_with_observations(
            (OpinionBlock(OpinionBlockType.NETWORK_AND_INFRASTRUCTURE, block_text),),
            (note,),
        ),
        author_id="fixture-analyst",
    )
    canonical_hash = opinion.content_sha256

    rendered = _render(
        renderer,
        TypstDraftPreviewDocument(base_publication=publication, analyst_opinion=opinion),
        bundle,
    )
    data = json.loads(rendered.render_data_bytes)
    opinion_section = next(
        item for item in data["content_sections"] if item["type"] == "analyst_opinion"
    )
    projected_block = opinion_section["blocks"][0]
    synthesis_section = next(
        item for item in data["content_sections"] if item["type"] == "synthesis"
    )
    projected_note = next(
        item for item in synthesis_section["blocks"] if item["type"] == "analyst_observation"
    )
    inline_block_code = next(
        span
        for span in projected_block["text_spans"]
        if span.get("technical") and span["text"].endswith(":80")
    )
    inline_note_code = next(
        span
        for span in projected_note["text_spans"]
        if span.get("technical") and not span.get("code_block")
    )
    fenced_note_code = next(span for span in projected_note["text_spans"] if span.get("code_block"))

    assert projected_block["text"] == block_text
    assert inline_block_code["text"] == "203.0.113.94:80"
    assert projected_note["text"] == note_text
    assert inline_note_code["text"] == "203.0.113.94:80"
    assert "203.0.113[.]94" in fenced_note_code["text"]
    assert opinion.content_sha256 == canonical_hash


def test_opinion_traceability_lists_only_logical_findings_cited_by_opinion() -> None:
    cited_findings = {UUID(int=index) for index in range(720, 743)}
    unrelated_version = UUID(int=743)
    evidence_id = UUID(int=744)
    references = [
        *(
            {"kind_label": "Finding", "reference_id": str(reference_id)}
            for reference_id in sorted(cited_findings, key=str)
        ),
        {"kind_label": "Finding", "reference_id": str(min(cited_findings, key=str))},
        {"kind_label": "Finding", "reference_id": str(unrelated_version)},
        {"kind_label": "Évidence", "reference_id": str(evidence_id)},
    ]

    projected = _opinion_traceability_refs(references, cited_findings)

    projected_finding_ids = {
        UUID(reference["reference_id"])
        for reference in projected
        if reference["kind_label"] == "Finding"
    }
    assert projected_finding_ids == cited_findings
    assert len(projected_finding_ids) == 23
    assert sum(item["kind_label"] == "Finding" for item in projected) == 23
    assert projected[-1] == references[-1]


def test_opinion_block_headings_are_reader_facing(tmp_path: Path) -> None:
    renderer, bundle = _renderer(tmp_path)
    publication, *_ = _frontmatter_v6_case()
    blocks = tuple(
        OpinionBlock(block_type=block_type, text=f"Texte {block_type.value}.")
        for block_type in (
            OpinionBlockType.KEY_FINDINGS,
            OpinionBlockType.ADDITIONAL_SAMPLES,
            OpinionBlockType.INFECTION_CHAIN_AND_CAPABILITIES,
            OpinionBlockType.NETWORK_AND_INFRASTRUCTURE,
            OpinionBlockType.CLAIM_VERIFICATION,
            OpinionBlockType.PROPOSED_DETECTIONS,
            OpinionBlockType.UNCERTAINTIES_AND_NEXT_LEADS,
        )
    )
    opinion = AnalystOpinionV1(
        investigation_id=UUID(int=780),
        synthesis_artifact_id=UUID(int=781),
        synthesis_sha256="a" * 64,
        base_publication_artifact_id=UUID(int=782),
        base_publication_sha256=publication_document_v5_sha256(publication),
        blocks=blocks,
        author_id="fixture-analyst",
    )

    data = json.loads(
        _render(
            renderer,
            TypstDraftPreviewDocument(publication, opinion),
            bundle,
        ).render_data_bytes
    )
    section = next(item for item in data["content_sections"] if item["type"] == "analyst_opinion")
    headings = [item["heading"] for item in section["blocks"]]

    assert headings == [
        "Apports de l'analyse",
        "Échantillons complémentaires",
        "Chaîne d'infection et capacités",
        "Activité réseau et infrastructure",
        "Vérification des affirmations",
        "Détections proposées",
        "Incertitudes et prochaines pistes",
    ]
    assert all("enquête" not in heading.casefold() for heading in headings)


def test_opinion_traceability_maps_superseded_finding_versions_to_current_ids(
    tmp_path: Path,
) -> None:
    renderer, bundle = _renderer(tmp_path)
    base = _base_v5()
    opinion, citation_context = _approved_opinion(base)
    original = citation_context.findings[0]
    first_version = replace(original, id=UUID(int=909), version=1, supersedes=None)
    cited_version = replace(first_version, id=UUID(int=910), version=2, supersedes=first_version.id)
    current = replace(original, version=3, supersedes=cited_version.id)
    opinion = replace(
        opinion,
        blocks=(
            replace(
                opinion.blocks[0],
                finding_refs=(cited_version.id, current.id, cited_version.id),
            ),
        ),
        content_sha256="",
    )
    citation_context = replace(
        citation_context,
        findings=(first_version, cited_version, current),
    )

    augmented = augment_publication_with_opinion(base, opinion, citation_context)
    article = PublicationDocumentV7(
        base_publication=augmented,
        publication_kind=PublicationKind.ARTICLE,
        routing_decision_hash="c" * 64,
        routing_policy_version="fixture-routing-v1",
    )
    source = _render(renderer, article, bundle)
    data = json.loads(source.render_data_bytes)
    annex = next(item for item in data["content_sections"] if item["type"] == "technical_annex")
    finding_refs = [item for item in annex["opinion_refs"] if item["kind_label"] == "Constat"]
    opinion_section = next(
        item for item in data["content_sections"] if item["type"] == "analyst_opinion"
    )
    rendered_block_refs = opinion_section["blocks"][0]["finding_refs"]

    assert [item["reference_id"] for item in finding_refs] == [str(current.id)]
    # Repeated citations of the same logical finding are rendered once.
    assert [item["reference_id"] for item in rendered_block_refs] == [str(current.id)]
    assert augmented.provenance.finding_version_logical_ids == ((cited_version.id, current.id),)
    assert opinion.content_sha256 == augmented.provenance.opinion_content_sha256
    assert opinion.blocks[0].finding_refs == (
        cited_version.id,
        current.id,
        cited_version.id,
    )
    round_tripped = publication_document_from_json(serialize_publication_document(augmented))
    assert isinstance(round_tripped, PublicationDocumentV6)
    assert round_tripped.provenance.finding_version_logical_ids == ((cited_version.id, current.id),)


def test_draft_ioc_provenance_maps_finding_versions_to_logical_ids(tmp_path: Path) -> None:
    renderer, bundle = _renderer(tmp_path)
    base = _base_v5()
    article_section = PublicationSectionV1(
        kind=PublicationSectionKind.TECHNICAL,
        heading="Contexte de l'article",
        paragraphs=(
            PublicationParagraphV1(
                "Contexte documenté pour un rendu d'article.",
                base.document.lead[0].evidence_refs,
            ),
        ),
    )
    article_document = replace(base.document, sections=(article_section,))
    article_semantic_text = SemanticTextV1(
        schema_version=base.semantic_text.schema_version,
        policy_version=base.semantic_text.policy_version,
        paragraphs=tuple(
            SemanticAnnotator(EnglishTermDetector(())).annotate_paragraph(
                anchor=anchor,
                text=text,
                entities=(),
            )
            for anchor, text in publication_document_text_anchors(
                article_document,
                references=base.references,
            ).items()
        ),
    )
    base = replace(
        base,
        document=article_document,
        semantic_text=article_semantic_text,
    )
    opinion, citation_context = _approved_opinion(base)
    original = citation_context.findings[0]
    original_observation = citation_context.observations[0]
    sample_hash = "d" * 64
    observation = replace(
        original_observation,
        object_type="file",
        object_id=sample_hash,
    )
    first_version = replace(
        original,
        id=UUID(int=920),
        kind=FindingKind.NEW_SAMPLE,
        assessment=FindingAssessment.SUPPORTED,
        novelty=NoveltyClass.NEW_TO_ARTICLE,
        scope=f"Subject reference: {sample_hash}",
        version=1,
        supersedes=None,
    )
    cited_version = replace(
        first_version,
        id=UUID(int=921),
        version=2,
        supersedes=first_version.id,
    )
    current = replace(
        original,
        id=UUID(int=922),
        kind=FindingKind.NEW_SAMPLE,
        assessment=FindingAssessment.SUPPORTED,
        novelty=NoveltyClass.NEW_TO_ARTICLE,
        scope=f"Subject reference: {sample_hash}",
        version=3,
        supersedes=cited_version.id,
    )
    draft_opinion = replace(
        opinion,
        status=AnalystOpinionStatus.DRAFT,
        reviewer_id=None,
        blocks=(replace(opinion.blocks[0], finding_refs=(cited_version.id,)),),
        content_sha256="",
    )
    preview = TypstDraftPreviewDocument(
        base,
        draft_opinion,
        findings=(current, cited_version, first_version),
        evidence_observations=(observation,),
    )

    data = json.loads(_render(renderer, preview, bundle).render_data_bytes)
    annex = next(item for item in data["content_sections"] if item["type"] == "technical_annex")
    ioc_section = next(item for item in data["content_sections"] if item["type"] == "ioc")
    finding_refs = [item for item in annex["opinion_refs"] if item["kind_label"] == "Constat"]
    ioc_rows = [row for group in annex["ioc_row_groups"] for row in group["rows"]]

    assert [item["reference_id"] for item in finding_refs] == [str(current.id)]
    assert len(ioc_rows) == 1
    assert ioc_rows[0]["value"] == sample_hash
    assert ioc_rows[0]["draft_refs"][0]["reference_id"] == str(current.id)
    assert (
        ioc_section["original_indicators"][0]["items"][0]["indicator_status_label"]
        == "Indicateur découvert dans cette analyse"
    )
    assert (
        "Constat portant sur un élément nouveau pour l'article"
        in ioc_section["original_indicators"][0]["items"][0]["qualification"]
    )
    reader_ioc = json.dumps(ioc_section, ensure_ascii=False).casefold()
    reader_annex = json.dumps(annex, ensure_ascii=False).casefold()
    assert "new_to_article" not in reader_ioc
    assert "finding new_to_article" not in reader_ioc
    assert "ioc original du run" not in reader_ioc
    assert "constat" in reader_annex
    assert "étayé par les observations citées" in reader_ioc
    assert str(current.id) in reader_annex
    assert str(observation.id) in reader_annex
    assert draft_opinion.blocks[0].finding_refs == (cited_version.id,)


def test_opinion_ioc_appendix_localizes_provenance_enums_for_display(tmp_path: Path) -> None:
    renderer, bundle = _renderer(tmp_path)
    base = _base_v5()
    source = base.document.sources[0]
    independent_id = UUID(int=783)
    independent_source = replace(
        source,
        source_document_id=independent_id,
        canonical_url="https://independent.example/report",
        role=SourceRole.INDEPENDENT,
    )
    article = replace(
        base.document,
        sources=(*base.document.sources, independent_source),
        additional_source_ids=base.document.additional_source_ids | {independent_id},
    )
    details = (
        PublicationIOCDetailV1(
            artifact_type=ArtifactType.DOMAIN,
            value="alpha.example",
            normalized_value="alpha.example",
            source_role=SourceRole.PRIMARY,
            context="Domaine cité dans la source primaire.",
            evidence_quote="La source mentionne alpha.example.",
            evidence_basis=ProductionEvidenceBasis.SOURCE_VERIFIED,
            source_document_ids=(source.source_document_id,),
            disposition=PublicationIOCDisposition.RETAINED,
            disposition_reason="contextual indicator",
            relevance_classification=RelevanceClassification.DIRECT,
            relevance_reason_code=RelevanceReasonCode.MALICIOUS_SUBJECT_RELATION,
        ),
        PublicationIOCDetailV1(
            artifact_type=ArtifactType.DOMAIN,
            value="beta.example",
            normalized_value="beta.example",
            source_role=SourceRole.INDEPENDENT,
            context="Domaine cité par une source indépendante.",
            evidence_quote="La source mentionne beta.example.",
            evidence_basis=ProductionEvidenceBasis.SOURCE_VERIFIED,
            source_document_ids=(independent_id,),
            disposition=PublicationIOCDisposition.UNCERTAIN,
            disposition_reason="unsupported_artifact_type",
            relevance_classification=RelevanceClassification.CONTEXT,
            relevance_reason_code=RelevanceReasonCode.MALICIOUS_ROLE_NOT_DEMONSTRATED,
        ),
        PublicationIOCDetailV1(
            artifact_type=ArtifactType.DOMAIN,
            value="gamma.example",
            normalized_value="gamma.example",
            source_role=SourceRole.PRIMARY,
            context="Domaine avec un motif de statut inconnu.",
            evidence_quote="La source mentionne gamma.example.",
            evidence_basis=ProductionEvidenceBasis.SOURCE_VERIFIED,
            source_document_ids=(source.source_document_id,),
            disposition=PublicationIOCDisposition.UNCERTAIN,
            disposition_reason="provider_internal_reason",
            first_observed=date(2026, 1, 2),
            last_observed=date(2026, 1, 3),
        ),
    )
    publication = replace(base, document=article, ioc_details=details)
    opinion, citation_context = _approved_opinion(publication)
    augmented = augment_publication_with_opinion(publication, opinion, citation_context)
    routed_article = PublicationDocumentV7(
        base_publication=augmented,
        publication_kind=PublicationKind.ARTICLE,
        routing_decision_hash="d" * 64,
        routing_policy_version="fixture-routing-v1",
    )

    data = json.loads(_render(renderer, routed_article, bundle).render_data_bytes)
    annex = next(item for item in data["content_sections"] if item["type"] == "technical_annex")
    rows = [row for group in annex["ioc_row_groups"] for row in group["rows"]]
    display_text = " ".join(row["context_summary"] for row in rows)

    assert "vérifiée par la source" in display_text.casefold()
    assert "source primaire" in display_text.casefold()
    assert "source indépendante" in display_text.casefold()
    assert "direct — relation malveillante au sujet" in display_text.casefold()
    assert "contexte — rôle malveillant non démontré" in display_text.casefold()
    assert "indicateur contextuel" in display_text.casefold()
    assert "contextual indicator" not in display_text.casefold()
    assert "type non publié" in display_text.casefold()
    assert "motif non précisé" in display_text.casefold()
    assert "période d'observation : 2026-01-02-2026-01-03" in display_text.casefold()
    assert "obs.:" not in display_text.casefold()
    assert "provider_internal_reason" not in display_text.casefold()
    assert "tlp non précisé" in display_text.casefold()
    assert "source_verified" not in display_text
    assert "primary" not in display_text
    assert "independent" not in display_text
    assert details[0].evidence_basis.value == "source_verified"
    assert details[0].source_role.value == "primary"
    assert details[0].relevance_reason_code.value == "malicious_subject_relation"


def test_short_monospace_host_has_no_internal_page_break_opportunities() -> None:
    host = "www.xionamiconm.com"

    rendered = _breakable_typst_display_text(
        host,
        interval=8,
        break_at_technical_separators=True,
    )

    assert rendered["text"] == host
    assert rendered["break_chunks"] == []


def test_ioc_reader_label_maps_cover_their_domain_enums() -> None:
    assert set(_IOC_DISPOSITION_LABELS) == set(PublicationIOCDisposition)
    assert set(_IOC_SOURCE_ROLE_LABELS) == {item.value for item in SourceRole}
    assert set(_IOC_EVIDENCE_BASIS_LABELS) == {item.value for item in ProductionEvidenceBasis}
    assert set(_IOC_RELEVANCE_CLASSIFICATION_LABELS) == {
        item.value for item in RelevanceClassification
    }
    assert set(_IOC_RELEVANCE_REASON_LABELS) == {item.value for item in RelevanceReasonCode}
    assert set(_FINDING_ASSESSMENT_LABELS) == set(FindingAssessment)
    assert set(_NOVELTY_CLASS_LABELS) == set(NoveltyClass)
    assert set(_IOC_GROUP_STATUS_LABELS) == {"retained", "originals", "candidates"}


def test_unconfirmed_finding_ioc_qualification_uses_neutral_french_labels() -> None:
    finding = SimpleNamespace(
        novelty=NoveltyClass.NEW_TO_ARTICLE,
        assessment=FindingAssessment.INCONCLUSIVE,
    )

    qualification = _draft_finding_ioc_qualification(finding)

    assert qualification == (
        "Piste non confirmée : constat non tranché (nouveau pour l'article) "
        "au vu des observations citées."
    )
    assert "finding" not in qualification.casefold()
    assert "inconclusive" not in qualification.casefold()


def test_stale_protocol_preview_caption_keeps_only_localized_proof_reservation() -> None:
    figure = cast(
        OpinionProtocolFigureV1,
        SimpleNamespace(
            kind=SimpleNamespace(value="sequence"),
            caption=(
                "Séquence du protocole\n"
                "Niveau de preuve : ARTICLE_DOCUMENTED — documenté, non observé. "
                "Aucun flux ni trame observés; offsets et valeurs d'octets inconnus."
            ),
            compiler_policy_version="diagram-d2-svg-v9-legacy",
        ),
    )

    title, caption, status_label = _protocol_figure_brief(figure, is_draft_preview=True)

    assert title == "Séquence réseau du protocole"
    assert caption == (
        "Niveau de preuve : documenté, non observé. "
        "Aucun flux ni trame observés; offsets et valeurs d'octets inconnus."
    )
    assert status_label is None
    assert "Niveau de preuve non précisé" not in caption


def test_current_protocol_figure_caption_keeps_proof_without_repeating_title() -> None:
    figure = cast(
        OpinionProtocolFigureV1,
        SimpleNamespace(
            kind=SimpleNamespace(value="sequence"),
            caption=(
                "Séquence réseau du protocole\n"
                "Niveau de preuve : ARTICLE_DOCUMENTED — documenté, non observé. "
                "Aucun flux ni trame observés; offsets et valeurs d'octets inconnus."
            ),
            compiler_policy_version=PROTOCOL_DIAGRAM_FIGURE_POLICY_VERSION,
        ),
    )

    title, caption, status_label = _protocol_figure_brief(figure, is_draft_preview=False)

    assert title == "Séquence réseau du protocole"
    assert caption == (
        "Niveau de preuve : documenté, non observé. "
        "Aucun flux ni trame observés; offsets et valeurs d'octets inconnus."
    )
    assert status_label is None


def test_publication_document_label_is_required_and_kept_verbatim(tmp_path: Path) -> None:
    renderer, bundle = _renderer(tmp_path)
    document = _document()

    with pytest.raises(TemplateBundleInvalidError, match="document_label"):
        renderer.render(document, bundle)
    with pytest.raises(TemplateBundleInvalidError, match="document_label"):
        renderer.render(document, bundle, document_label="  ")

    label = "Publication enrichie — avis approuvé v17"
    data = json.loads(
        renderer.render(document, bundle, document_label=f"  {label}  ").render_data_bytes
    )
    assert data["document_label"] == label


def test_wide_component_table_paths_wrap_between_components_only(tmp_path: Path) -> None:
    renderer, bundle = _renderer(tmp_path)
    table = _table_at("component-inventory", "Composants", EnrichmentPlacementKind.AFTER_LEAD)
    path_a = r"C:\Program Files\Vendor\PsProxy.dll"
    path_b = r"C:\ProgramData\Windows\Blackwood.dll"
    table = replace(
        table,
        columns=(
            PublicationTableColumnV1("component", "Composant"),
            PublicationTableColumnV1("path", "Chemin Windows"),
            PublicationTableColumnV1("role", "Rôle"),
            PublicationTableColumnV1("evidence", "Contexte de preuve"),
        ),
        rows=(
            PublicationTableRowV1(
                ("PsProxy.dll", path_a, "Proxy", "Composant décrit."),
                table.rows[0].evidence_refs,
            ),
            PublicationTableRowV1(
                ("Blackwood.dll", path_b, "Loader", "Rôle documenté."),
                table.rows[0].evidence_refs,
            ),
        ),
    )

    data = json.loads(_render(renderer, _full_document(tables=(table,)), bundle).render_data_bytes)
    rendered_table = next(
        block
        for section in data["content_sections"]
        for block in section["blocks"]
        if block["type"] == "table"
    )

    assert rendered_table["rows"][0][0] == "PsProxy.dll"
    assert rendered_table["rows"][1][0] == "Blackwood.dll"
    assert rendered_table["card_display_cells"][0][1]["break_chunks"][-1] == "PsProxy.dll"
    assert rendered_table["card_display_cells"][1][1]["break_chunks"][-1] == "Blackwood.dll"
    assert all(
        "PsProxy.dll" not in chunk and "Blackwood.dll" not in chunk
        for row in rendered_table["card_display_cells"]
        for cell in row
        if cell is not None
        for chunk in cell["break_chunks"][:-1]
    )


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


def test_hidden_timeline_figure_does_not_consume_reader_figure_number(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publication, *_ = _frontmatter_v6_case()
    opinion, _citation_context = _approved_opinion(publication)
    protocol_figures = tuple(
        OpinionProtocolFigureV1(
            kind=kind,
            model_sha256="a" * 64,
            spec_sha256="b" * 64,
            asset_id=UUID(int=4243 + index),
            asset_sha256="c" * 64,
            preview_sha256="d" * 64,
            pin_sha256="e" * 64,
            compiler_name="d2",
            compiler_version="0.6.9",
            compiler_policy_version=PROTOCOL_DIAGRAM_FIGURE_POLICY_VERSION,
            caption="Reader-visible protocol diagram.",
        )
        for index, kind in enumerate(
            (
                ProtocolDiagramKind.SEQUENCE,
                ProtocolDiagramKind.STATE_MACHINE,
                ProtocolDiagramKind.MESSAGE_FORMAT,
            )
        )
    )
    opinion = replace(
        opinion,
        status=AnalystOpinionStatus.DRAFT,
        reviewer_id=None,
        protocol_figures=protocol_figures,
        content_sha256="",
    )
    monkeypatch.setattr(
        typst_rendering_module,
        "build_publication_timeline_figure",
        lambda _publication: {
            "figure_id": "hidden-timeline-figure",
            "publication_sha256": "f" * 64,
            "events": [],
        },
    )

    model = project_publication_to_typst_model(
        TypstDraftPreviewDocument(base_publication=publication, analyst_opinion=opinion)
    )
    opinion_section = next(
        section for section in model.content_sections if section["type"] == "analyst_opinion"
    )

    assert [f["figure_number"] for f in opinion_section["protocol_figures"]] == [1, 2, 3]


def test_french_publication_dates_use_first_day_typography() -> None:
    assert _display_date(None, date(2026, 2, 1)) == "1er février 2026"
    assert _display_date(None, date(2026, 2, 4)) == "4 février 2026"


def test_v6_render_model_uses_dated_source_references_and_both_ioc_groups(
    tmp_path: Path,
) -> None:
    publication, *_ = _frontmatter_v6_case()
    renderer, bundle = _renderer(tmp_path)

    model = json.loads(_render(renderer, publication, bundle).render_data_bytes)

    assert model["title"] == "[Example actor] Décrit une activité documentée"
    assert "title_spans" not in model
    sections = {item["type"]: item for item in model["content_sections"]}
    references = sections["references"]
    synthesis = sections["synthesis"]
    ioc = sections["ioc"]
    domain_group = next(
        group for group in ioc["source_indicators"] if group["heading"] == "Domaines"
    )
    displayed_domain_texts = [item["value"]["text"] for item in domain_group["items"]]
    assert references["type"] == "references"
    assert model["article_format"] is None
    dated_reference = next(
        item
        for item in references["entries"]
        if item["source_urls"] == ["https://a.example/report"]
    )
    assert dated_reference["display_date"] == "10 janvier 2026"
    undated_reference = next(
        item
        for item in references["entries"]
        if item["source_urls"] == ["https://missing.example/article"]
    )
    assert undated_reference["date"] is None
    assert "display_date" not in undated_reference
    assert synthesis["type"] == "synthesis"
    assert len(displayed_domain_texts) == len(set(displayed_domain_texts))
    assert {"context.example", "original.example"} <= set(displayed_domain_texts)
    assert all("type_label" not in item for item in domain_group["items"])


def test_v6_render_model_keeps_original_ioc_values_separate_from_subject_values(
    tmp_path: Path,
) -> None:
    publication, *_ = _frontmatter_v6_case()
    original = publication.original_indicators[0].indicators[0]
    renderer, bundle = _renderer(tmp_path)

    model = json.loads(_render(renderer, publication, bundle).render_data_bytes)
    ioc = model["content_sections"][-1]
    domain_group = next(
        group for group in ioc["source_indicators"] if group["heading"] == "Domaines"
    )
    displayed_texts = [item["value"]["text"] for item in domain_group["items"]]
    assert original.normalized_value in displayed_texts
    assert len(displayed_texts) == len({value.casefold() for value in displayed_texts})
    assert publication.original_indicators[0].indicators[0] == original
    assert all(
        "status_label" not in item and "type_label" not in item for item in domain_group["items"]
    )


def test_render_ioc_projection_deduplicates_normalized_values_across_groups() -> None:
    publication, *_ = _frontmatter_v6_case()
    original_group = publication.original_indicators[0]

    displayed = _indicator_values((original_group, original_group))

    assert [item["text"] for item in displayed["domains"]] == [
        "context.example",
        "original.example",
    ]


def test_ioc_projection_refangs_values_in_type_specific_lists() -> None:
    values = (
        (ArtifactType.IP, "198.51.100[.]10", "198.51.100.10"),
        (ArtifactType.DOMAIN, "core[.]example", "core.example"),
        (
            ArtifactType.URL,
            "hxxps://cdn[.]example[.]test/path",
            "https://cdn.example.test/path",
        ),
        (ArtifactType.HASH, "a" * 64, "a" * 64),
    )
    document = replace(
        _full_document(),
        indicators=tuple(
            PublicationIndicatorGroupV1(
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
            for artifact_type, value, normalized in values
        ),
    )

    model = project_publication_to_typst_model(document)
    assert model.article_format is not None
    indicators = model.article_format["ioc_categories"]

    assert [item["value"]["text"] for item in indicators["ips"]] == ["198.51.100.10"]
    assert [item["value"]["text"] for item in indicators["domains"]] == ["core.example"]
    assert [item["value"]["text"] for item in indicators["urls"]] == [
        "https://cdn.example.test/path"
    ]
    assert [item["value"]["text"] for item in indicators["files"]] == ["a" * 64]
    assert {
        (group.artifact_type, indicator.value, indicator.normalized_value)
        for group in document.indicators
        for indicator in group.indicators
    } == {(artifact_type, value, normalized) for artifact_type, value, normalized in values}


def test_full_mapping_preserves_text_timeline_indicators_and_optional_sources(
    tmp_path: Path,
) -> None:
    renderer, bundle = _renderer(tmp_path)
    document = _full_document()
    document = replace(
        document,
        indicators=tuple(
            replace(
                group,
                indicators=tuple(
                    replace(indicator, normalized_value="https://normalized.example/path")
                    for indicator in group.indicators
                ),
            )
            if group.artifact_type is ArtifactType.URL
            else group
            for group in document.indicators
        ),
    )
    contextual_source = replace(document.sources[1], editorial_role=ProductionEditorialRole.CONTEXT)
    document = replace(
        document,
        sources=(document.sources[0], contextual_source, document.sources[2]),
    )
    data = json.loads(_render(renderer, document, bundle).render_data_bytes)

    assert data["language"] == "fr"
    assert data["title"] == "Intrusion report"
    sections = {item["type"]: item for item in data["content_sections"]}
    references = sections["references"]
    synthesis = sections["synthesis"]
    article_format = data["article_format"]
    assert references["type"] == "references"
    assert {item["source_urls"][0] for item in references["entries"]} == {
        "https://example.test/one",
        "https://example.test/two",
    }
    assert article_format["timeline_key_events"] == []
    assert set(article_format["ioc_categories"]) == {"ips", "urls", "domains", "files"}
    ioc_categories = article_format["ioc_categories"]
    assert [item["value"]["text"] for item in ioc_categories["ips"]] == ["Display ip"]
    assert [item["value"]["text"] for item in ioc_categories["domains"]] == ["normalized-domain"]
    assert [item["value"]["text"] for item in ioc_categories["urls"]] == [
        "https://normalized.example/path"
    ]
    assert [item["value"]["text"] for item in ioc_categories["files"]] == ["normalized-hash"]
    assert not {"chronology", "technical_annex", "provenance"}.intersection(sections)
    assert all(
        {"publisher", "title", "date", "url", "urls"} <= set(item) for item in references["entries"]
    )
    assert "uncertainties" not in data
    assert references["sources"] == [
        {
            "title": "Primary source",
            "publisher": "Example Lab",
            "date": "2025-01-02",
            "url": "https://example.test/one",
        },
        {
            "title": "Duplicate URL source",
            "publisher": None,
            "date": "2025-03-04",
            "url": "https://example.test/one",
        },
        {
            "title": None,
            "publisher": "Contexte · example.test",
            "date": None,
            "url": "https://example.test/two",
        },
    ]
    assert [(item["type"], item["text"]) for item in synthesis["blocks"][:2]] == [
        ("paragraph", _INJECTION_TEXT),
        ("paragraph", "Second lead paragraph"),
    ]
    assert all(
        item["anchor_target"].startswith("publication-paragraph-")
        for item in synthesis["blocks"][:2]
    )
    assert synthesis["blocks"][2] == {"type": "section_heading", "text": _INJECTION_TEXT}
    assert synthesis["blocks"][3]["type"] == "paragraph"
    assert synthesis["blocks"][3]["text"] == "Section body"
    assert synthesis["blocks"][3]["anchor_target"].startswith("publication-paragraph-")
    assert "—été".encode() in _render(renderer, document, bundle).render_data_bytes


def test_synthesis_section_heading_is_projected_before_its_paragraph(tmp_path: Path) -> None:
    renderer, bundle = _renderer(tmp_path)
    document = _full_document()
    document = replace(
        document,
        sections=(
            replace(document.sections[0], heading="Accès initial"),
            replace(
                document.sections[0],
                heading="Persistance",
                paragraphs=(
                    replace(document.sections[0].paragraphs[0], text="A task is created."),
                ),
            ),
        ),
    )

    data = json.loads(_render(renderer, document, bundle).render_data_bytes)

    synthesis = next(item for item in data["content_sections"] if item["type"] == "synthesis")
    assert [(item["type"], item["text"]) for item in synthesis["blocks"][2:]] == [
        ("section_heading", "Accès initial"),
        ("paragraph", "Section body"),
        ("section_heading", "Persistance"),
        ("paragraph", "A task is created."),
    ]
    assert synthesis["blocks"][3]["anchor_target"].startswith("publication-paragraph-")
    assert synthesis["blocks"][5]["anchor_target"].startswith("publication-paragraph-")


def test_brief_without_synthesis_sections_has_no_section_headings(tmp_path: Path) -> None:
    renderer, bundle = _renderer(tmp_path)
    document = replace(_full_document(), sections=())

    data = json.loads(_render(renderer, document, bundle).render_data_bytes)
    synthesis = next(item for item in data["content_sections"] if item["type"] == "synthesis")

    assert synthesis["blocks"]
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

    data = json.loads(_render(renderer, document, bundle).render_data_bytes)
    synthesis = next(item for item in data["content_sections"] if item["type"] == "synthesis")
    paragraph = synthesis["blocks"][0]
    table = next(block for block in synthesis["blocks"] if block["type"] == "table")
    styles = {span["style"] for span in paragraph["semantic_spans"]}
    semantic_cells = table["semantic_cells"]

    assert data["schema_version"] == "typst-publication-model-v40-m21-opinion-render"
    assert data["kind"] == "article"
    assert table["column_weights"] == _table_column_weights(base.tables[0])
    assert all(0.8 <= weight <= 2.4 for weight in table["column_weights"])
    assert paragraph["text"] == lead_text
    assert "".join(span["text"] for span in paragraph["semantic_spans"]) == lead_text
    assert {"semantic-actor", "semantic-command", "semantic-technical-literal"} <= styles
    assert len(semantic_cells) == sum(len(row) for row in table["rows"])
    assert all(isinstance(cell_spans, list) for cell_spans in semantic_cells)
    assert "semantic-table-command" in {span["style"] for span in semantic_cells[0]}
    assert "semantic-actor" in {span["style"] for span in table["semantic_caption"]}


def test_v5_typst_projection_keeps_extracted_xml_parameter_as_literal_text(
    tmp_path: Path,
) -> None:
    renderer, bundle = _renderer(tmp_path)
    base = _full_document()
    literal = '<etwEnable enabled="false"/>'
    paragraph_text = f"La configuration contient {literal}."
    base = replace(
        base,
        lead=(replace(base.lead[0], text=paragraph_text), *base.lead[1:]),
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
            )
            for anchor, text in publication_document_text_anchors(base).items()
        ),
    )
    document = PublicationDocumentV5(document=base, semantic_text=semantic_text)

    render_data_bytes = _render(renderer, document, bundle).render_data_bytes
    render_data = json.loads(render_data_bytes)
    synthesis = next(
        item for item in render_data["content_sections"] if item["type"] == "synthesis"
    )
    paragraph = synthesis["blocks"][0]

    assert paragraph["text"] == paragraph_text
    assert "".join(span["text"] for span in paragraph["semantic_spans"]) == paragraph_text
    assert json.dumps(literal, ensure_ascii=False).encode("utf-8")[1:-1] in render_data_bytes


def test_long_iocs_and_semantic_literals_get_render_only_break_opportunities(
    tmp_path: Path,
) -> None:
    renderer, bundle = _renderer(tmp_path)
    long_hash = "a" * 128
    long_path = "/opt/very-long-installation/path/to/a/critical/binary"
    long_command = "powershell.exe -ExecutionPolicy Bypass -EncodedCommand " + "B" * 48
    registry_path = r"HKCU\Software\Microsoft\Windows\CurrentVersion\Run"
    lead_text = " ".join(
        (
            f"Hash {long_hash};",
            f"path {long_path};",
            f"registry {registry_path};",
            f"command {long_command}.",
        )
    )
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
            (SemanticRole.TECHNICAL_LITERAL, registry_path),
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
    base = replace(
        base,
        additional_source_ids=base.additional_source_ids | frozenset({_SOURCE_ID}),
    )
    ioc_detail = PublicationIOCDetailV1(
        artifact_type=ArtifactType.HASH,
        value=long_hash,
        normalized_value=long_hash,
        source_role=SourceRole.PRIMARY,
        context="Hash file observé dans la source.",
        evidence_quote=f"SHA-256 : {long_hash}",
        evidence_basis=ProductionEvidenceBasis.SOURCE_VERIFIED,
        source_document_ids=(_SOURCE_ID,),
        disposition=PublicationIOCDisposition.RETAINED,
        disposition_reason="IOC retenu dans le document publié.",
    )
    document = PublicationDocumentV5(
        document=base,
        semantic_text=semantic_text,
        ioc_details=(ioc_detail,),
    )

    data = json.loads(_render(renderer, document, bundle).render_data_bytes)
    synthesis = next(item for item in data["content_sections"] if item["type"] == "synthesis")
    lead = next(
        block
        for block in synthesis["blocks"]
        if block.get("type") == "paragraph" and block.get("text") == lead_text
    )
    spans = {span["style"]: span for span in lead["semantic_spans"]}

    files = data["article_format"]["ioc_categories"]["files"]
    ioc_value_span = next(item["value"] for item in files if item["value"]["text"] == long_hash)
    assert ioc_value_span["text"] == long_hash
    assert "".join(ioc_value_span["break_chunks"]) == long_hash
    assert ioc_value_span["break_after"] == [True] * (len(ioc_value_span["break_chunks"]) - 1)
    for style, value in (
        ("semantic-ioc", long_hash),
        ("semantic-path", long_path),
        ("semantic-technical-literal", registry_path),
        ("semantic-command", long_command),
    ):
        assert spans[style]["text"] == value
        assert "".join(spans[style]["break_chunks"]) == value
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
        ("Registre", r"HKCU\Software\Microsoft\Windows\CurrentVersion\Run"),
    )
    table = replace(
        base_table,
        title="Valeurs techniques observées",
        caption="Les valeurs exactes documentées par la source.",
        rows=tuple(replace(base_table.rows[0], cells=row) for row in cells),
    )
    data = json.loads(_render(renderer, _full_document(tables=(table,)), bundle).render_data_bytes)
    rendered_table = next(
        block
        for section in data["content_sections"]
        if section["type"] == "synthesis"
        for block in section["blocks"]
        if block["type"] == "table"
    )
    styles = [
        {span["style"] for span in cell_spans} for cell_spans in rendered_table["semantic_cells"]
    ]
    display_values = [
        "".join(span["text"] for span in cell_spans)
        for cell_spans in rendered_table["semantic_cells"]
    ]

    assert styles[3] == {"semantic-table-technical"}
    assert styles[5] == {"semantic-table-ioc"}
    assert styles[7] == {"semantic-table-ioc"}
    assert styles[9] == {"semantic-table-ioc"}
    assert styles[11] == {"semantic-table-path"}
    assert styles[13] == {"semantic-table-path"}
    assert display_values[3] == sha256
    assert display_values[5] == long_url
    assert rendered_table["semantic_cells"][5][0]["text"] == long_url
    assert "".join(rendered_table["semantic_cells"][5][0]["break_chunks"]) == long_url
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

    data = json.loads(_render(renderer, document, bundle).render_data_bytes)
    sections = {item["type"]: item for item in data["content_sections"]}
    references = sections["references"]
    synthesis = sections["synthesis"]
    paragraphs = [block["text"] for block in synthesis["blocks"] if block["type"] == "paragraph"]

    assert [section["type"] for section in data["content_sections"]] == ["references", "synthesis"]
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
    data = json.loads(_render(renderer, document, bundle).render_data_bytes)
    sections = {item["type"]: item for item in data["content_sections"]}
    reference_blocks = sections["references"]["blocks"]
    blocks = sections["synthesis"]["blocks"]

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
    section_rich_start = 10
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
        for ref in _render(renderer, document, bundle).media_refs
        if ref.asset_id == diagrams[0].asset_id
    )
    assert diagram_ref.expected_kind is MediaAssetKind.DIAGRAM_SVG
    assert diagram_ref.expected_mime_type == "image/svg+xml"
    assert diagram_ref.expected_sha256 is None
    assert diagram_ref.expected_byte_size is None
    assert diagram_ref.media_path.endswith(".svg")


def test_figure_reader_source_hides_pipeline_locators_and_media_refs_deduplicate(
    tmp_path: Path,
) -> None:
    renderer, bundle = _renderer(tmp_path)
    shared_asset_id = UUID("30000000-0000-4000-8000-000000000001")
    technical_provenance = (
        "archived source document 65d67fad-2db8-4e50-a6a0-6fd09e790000; "
        "/html[1]/body[1]/div[3]/img[3]; anchor: https://secret.example/image.png"
    )
    first = replace(
        _figure_at(
            "figure_one",
            "Caption one",
            EnrichmentPlacementKind.AFTER_LEAD,
            asset_id=shared_asset_id,
        ),
        source_url="https://secret.example/image.png",
        provenance=technical_provenance,
        locator=replace(
            _figure_at(
                "locator_template",
                "Caption",
                EnrichmentPlacementKind.AFTER_LEAD,
            ).locator,
            page=1,
            section="Bitcoin wallet recovery",
            figure_label="HTML image 6",
        ),
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
            section="HTML image 6",
            figure_label="HTML image 6",
            original_asset_url="https://secret.example/source-image.png",
        ),
    )
    third = replace(
        second,
        key="figure_three",
        source_url="about:blank",
        locator=replace(second.locator, section=None, figure_label="HTML image 7"),
    )
    document = _full_document(figures=(first, second, third))
    rendered = _render(renderer, document, bundle)
    data = json.loads(rendered.render_data_bytes)
    figure_blocks = [
        block
        for section in data["content_sections"]
        if section["type"] == "synthesis"
        for block in section["blocks"]
        if block["type"] == "figure"
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
        f"media/{shared_asset_id}.png",
    ]
    assert all(
        not {"classification", "source_note", "attribution"}.intersection(block)
        for block in figure_blocks
    )
    assert figure_blocks[0]["caption"] == "Caption one"
    assert figure_blocks[0]["sha256"] == first.sha256
    assert figure_blocks[0]["byte_size"] == first.byte_size
    render_data = rendered.render_data_bytes.decode("utf-8")
    assert "provenance" not in render_data
    assert "locator" not in render_data
    assert all("source_url" not in block for block in figure_blocks)
    assert all("provenance" not in block for block in figure_blocks)
    assert "https://secret.example/image.png" not in render_data
    assert "secret.example/image.png" not in render_data
    assert "65d67fad-2db8-4e50-a6a0-6fd09e790000" not in render_data
    assert "/html[1]" not in render_data
    assert "HTML image" not in render_data
    assert document.figures[0].provenance == technical_provenance


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
    rendered = _render(renderer, _document(figures=(figure,)), bundle)

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


# ---------------------------------------------------------------------------
# F4-D B5b — Typst rendering of note content blocks
# ---------------------------------------------------------------------------

_NOTE_BLOCK_ANCHOR_ID = UUID(int=8801)
_NOTE_BLOCK_ANCHOR = "lead:0001"


def _note_block_v9(
    *,
    tables: tuple = (),
    charts: tuple = (),
) -> tuple[PublicationDocumentV9, PublicationDocumentV5, object]:
    """Build a V9 article whose after-paragraph elements resolve to lead:0001."""
    snapshot, references, extraction, synthesis = _canonical_inputs()
    enrichment = _enrichment_with(
        extraction=extraction,
        synthesis=synthesis,
        tables=tables,
        charts=charts,
    )
    base = build_publication_document_v5(
        snapshot=snapshot,
        references=references,
        extraction=extraction,
        synthesis=synthesis,
        editorial_enrichment=enrichment,
    )
    placements: list[ResolvedPublicationPlacementV1] = []
    position = 0
    for kind, elements in (
        ("table", base.document.tables),
        ("chart", base.document.charts),
        ("diagram", base.document.diagrams),
        ("figure", base.document.figures),
    ):
        for element in elements:
            if element.placement.kind is EnrichmentPlacementKind.AFTER_PARAGRAPH:
                placements.append(
                    ResolvedPublicationPlacementV1(
                        element_kind=kind,
                        element_key=element.key,
                        element_position=position,
                        anchor_block_id=_NOTE_BLOCK_ANCHOR_ID,
                        anchor_pointer="/lead/0",
                        anchor_canonical_position=0,
                        paragraph_kind="lead",
                        section_index=None,
                        paragraph_index=0,
                    )
                )
            position += 1
    v9 = PublicationDocumentV9(
        base_publication=PublicationDocumentV7(
            base_publication=_v6(base),
            publication_kind=PublicationKind.ARTICLE,
            routing_decision_hash="a" * 64,
            routing_policy_version="routing-policy-v1",
        ),
        article_format=build_article_format_v1(base, extraction),
        registry_hash="b" * 64,
        synthesis_canonical_sha256="c" * 64,
        enrichment_canonical_sha256="d" * 64,
        resolved_placements=tuple(placements),
    )
    return v9, base, extraction


def _after_paragraph_table(key: str = "note-linked-table"):
    _, _, extraction, _ = _canonical_inputs()
    return replace(
        _enrichment_table(extraction_evidence_refs_v1(extraction)[0], key=key),
        placement=EnrichmentPlacementV1(
            EnrichmentPlacementKind.AFTER_PARAGRAPH,
            anchor_block_id=_NOTE_BLOCK_ANCHOR_ID,
        ),
    )


def _after_paragraph_chart(key: str = "note-linked-chart"):
    _, _, extraction, _ = _canonical_inputs()
    return replace(
        _enrichment_chart(extraction_evidence_refs_v1(extraction)[0]),
        key=key,
        placement=EnrichmentPlacementV1(
            EnrichmentPlacementKind.AFTER_PARAGRAPH,
            anchor_block_id=_NOTE_BLOCK_ANCHOR_ID,
        ),
    )


def _note_v9_with_opinion(
    v9: PublicationDocumentV9,
    *,
    notes: tuple[AnalystObservationV1, ...],
    observations: tuple[EvidenceObservation, ...],
    investigation_id: UUID,
) -> PublicationDocumentV9:
    base_sha256 = hashlib.sha256(
        json.dumps(
            serialize_publication_document(v9),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    artifact_id = uuid4()
    opinion = AnalystOpinionV1(
        investigation_id=investigation_id,
        synthesis_artifact_id=uuid4(),
        synthesis_sha256="1" * 64,
        base_publication_artifact_id=artifact_id,
        base_publication_sha256=base_sha256,
        blocks=opinion_blocks_with_observations(
            (
                OpinionBlock(
                    OpinionBlockType.KEY_FINDINGS,
                    "Constat de démonstration.",
                    qualification=OpinionClaimQualification.HYPOTHESIS,
                ),
            ),
            notes,
        ),
        author_id="fixture-analyst",
        reviewer_id="fixture-reviewer",
        status=AnalystOpinionStatus.APPROVED,
    )
    augmented = augment_publication_with_opinion(
        v9,
        opinion,
        OpinionCitationContext(
            base_publication_artifact_id=artifact_id,
            base_publication_sha256=base_sha256,
            investigation_id=investigation_id,
            approver_id="fixture-approver",
            findings=(),
            observations=observations,
            tlp_violation=False,
            do_not_submit=False,
        ),
    )
    # The augmentation itself must preserve the resolved V9 envelope; rebuilding
    # one here would hide the production defect instead of proving the path.
    assert type(augmented) is PublicationDocumentV9
    return augmented


def test_note_content_blocks_render_with_provenance() -> None:
    """B5b: refs resolve in notes_by_anchor; elements are referenced, never copied."""
    table = _after_paragraph_table()
    v9, base, _extraction = _note_block_v9(tables=(table,))
    investigation_id = uuid4()
    published_table = base.document.tables[0]
    element_sha256 = publication_element_sha256(
        base.document, kind="table", key=published_table.key
    )
    evidence = EvidenceObservation(
        investigation_id=investigation_id,
        kind=ObservationEvidenceKind.OTHER,
        blob_id=uuid4(),
        sha256="e" * 64,
        source="vt",
        source_version="fixture.v1",
        observed_at=datetime.now(UTC),
        object_type="file",
        object_id="note-sample.bin",
    )
    note = AnalystObservationV1(
        id=UUID(int=8811),
        anchor=_NOTE_BLOCK_ANCHOR,
        text="NOTE-TABLE-CROSS-REFERENCE",
        state=AnalystObservationState.UNRESOLVED,
        relation=AnalystObservationRelation.UNRESOLVED,
        content_blocks=(
            ArticleElementNoteBlockV1(
                kind=NoteArticleElementKind.TABLE,
                element_key=published_table.key,
                element_sha256=element_sha256,
                anchor=_NOTE_BLOCK_ANCHOR,
            ),
            IocTableNoteBlockV1(
                observation_ids=(evidence.id,),
                title=note_ioc_table_title(1),
            ),
        ),
    )
    document = _note_v9_with_opinion(
        v9,
        notes=(note,),
        observations=(evidence,),
        investigation_id=investigation_id,
    )

    model = project_publication_to_typst_model(
        document,
        note_block_context=NoteBlockRenderContext(observations=(evidence,)),
    )
    synthesis = next(item for item in model.content_sections if item["type"] == "synthesis")
    blocks = synthesis["blocks"]
    note_block = next(
        item for item in blocks if item.get("type") == "analyst_observation" and item.get("blocks")
    )
    assert note_block["text"] == "NOTE-TABLE-CROSS-REFERENCE"
    rendered_blocks = note_block["blocks"]
    assert [block["block_type"] for block in rendered_blocks] == [
        "article_element",
        "ioc_table",
    ]

    reference = rendered_blocks[0]
    assert reference["kind"] == "table"
    assert reference["element_key"] == published_table.key
    assert reference["reference_label"] == f"Tableau — {published_table.title}"
    reference_provenance = "".join(part["text"] for part in reference["provenance"])
    assert element_sha256 in reference_provenance
    assert _NOTE_BLOCK_ANCHOR in reference_provenance

    ioc = rendered_blocks[1]
    assert ioc["title"] == note_ioc_table_title(1)
    assert [cell["text"] for cell in ioc["pipe_table"]["header"]] == [
        "Observation",
        "Type",
        "Valeur",
        "Source",
        "Observé le",
        "Empreinte",
    ]
    (row,) = ioc["pipe_table"]["rows"]
    assert [cell["text"] for cell in row] == [
        str(evidence.id),
        "file",
        "note-sample.bin",
        "vt",
        evidence.observed_at.isoformat(),
        evidence.sha256,
    ]
    ioc_provenance = "".join(part["text"] for part in ioc["provenance"])
    assert "Observations canoniques de cette enquête : 1" in ioc_provenance

    # The element is rendered at its article placement only; the note references it.
    assert (
        sum(
            1
            for item in blocks
            if item.get("type") == "table" and item.get("key") == published_table.key
        )
        == 1
    )
    table_position = next(
        index
        for index, item in enumerate(blocks)
        if item.get("type") == "table" and item.get("key") == published_table.key
    )
    note_position = blocks.index(note_block)
    assert table_position < note_position
    facts = note_article_element_facts(document)
    assert [(fact.kind, fact.key, fact.anchor) for fact in facts] == [
        ("table", published_table.key, _NOTE_BLOCK_ANCHOR)
    ]


def test_note_chart_without_evidence_refused() -> None:
    """B5b: a chart whose points carry no evidence is refused before compilation."""
    chart = _after_paragraph_chart()
    v9, base, _extraction = _note_block_v9(charts=(chart,))
    investigation_id = uuid4()
    published_chart = base.document.charts[0]
    facts = note_article_element_facts(v9)
    (chart_fact,) = facts
    assert chart_fact.kind == "chart"
    unevidenced_fact = replace(chart_fact, evidence_ref_count=0)
    note = AnalystObservationV1(
        id=UUID(int=8812),
        anchor=_NOTE_BLOCK_ANCHOR,
        text="NOTE-CHART-REFERENCE",
        state=AnalystObservationState.UNRESOLVED,
        relation=AnalystObservationRelation.UNRESOLVED,
        content_blocks=(
            ArticleElementNoteBlockV1(
                kind=NoteArticleElementKind.CHART,
                element_key=published_chart.key,
                element_sha256=chart_fact.element_sha256,
                anchor=_NOTE_BLOCK_ANCHOR,
            ),
        ),
    )
    document = _note_v9_with_opinion(
        v9,
        notes=(note,),
        observations=(),
        investigation_id=investigation_id,
    )

    with pytest.raises(NoteContentBlockRenderError) as failure:
        project_publication_to_typst_model(
            document,
            note_block_context=NoteBlockRenderContext(element_facts=(unevidenced_fact,)),
        )
    assert failure.value.code == "note_chart_without_evidence"

    # The same block renders its deterministic cross-reference when evidenced.
    model = project_publication_to_typst_model(
        document,
        note_block_context=NoteBlockRenderContext(element_facts=(chart_fact,)),
    )
    synthesis = next(item for item in model.content_sections if item["type"] == "synthesis")
    note_block = next(
        item
        for item in synthesis["blocks"]
        if item.get("type") == "analyst_observation" and item.get("blocks")
    )
    assert note_block["blocks"][0]["reference_label"] == (f"Figure 1 — {published_chart.title}")


def test_load_note_block_render_context_reads_only_cited_observations() -> None:
    """The published render path reads exactly the observations its blocks cite."""
    from cti_app.application.publication_rendering import load_note_block_render_context

    raw = b"cited bytes"
    publication = _base_v5()
    cited_code = EvidenceObservation(
        investigation_id=uuid4(),
        kind=ObservationEvidenceKind.BINARY_ANALYSIS,
        blob_id=uuid4(),
        sha256=hashlib.sha256(raw).hexdigest(),
        source="native_analysis",
        source_version="fixture.v1",
        observed_at=datetime.now(UTC),
        object_type="native_analysis",
        object_id="cited.bin",
    )
    cited_ioc = EvidenceObservation(
        investigation_id=cited_code.investigation_id,
        kind=ObservationEvidenceKind.OTHER,
        blob_id=uuid4(),
        sha256="6" * 64,
        source="vt",
        source_version="fixture.v1",
        observed_at=datetime.now(UTC),
        object_type="ip",
        object_id="192.0.2.44",
    )
    uncited = EvidenceObservation(
        investigation_id=cited_code.investigation_id,
        kind=ObservationEvidenceKind.OTHER,
        blob_id=uuid4(),
        sha256="7" * 64,
        source="shodan",
        source_version="fixture.v1",
        observed_at=datetime.now(UTC),
        object_type="ip",
        object_id="192.0.2.45",
    )
    code_block = CanonicalCodeNoteBlockV1(
        observation_id=cited_code.id,
        blob_sha256=cited_code.sha256,
        offset=0,
        length=len(raw),
        excerpt_sha256=hashlib.sha256(raw).hexdigest(),
        language="text",
    )
    ioc_block = IocTableNoteBlockV1(
        observation_ids=(cited_ioc.id,),
        title=note_ioc_table_title(1),
    )
    note = AnalystObservationV1(
        id=UUID(int=8822),
        anchor=_NOTE_BLOCK_ANCHOR,
        text="NOTE-CONTEXT-FIXTURE",
        state=AnalystObservationState.UNRESOLVED,
        relation=AnalystObservationRelation.UNRESOLVED,
        content_blocks=(code_block, ioc_block),
    )
    opinion = AnalystOpinionV1(
        investigation_id=cited_code.investigation_id,
        synthesis_artifact_id=uuid4(),
        synthesis_sha256="5" * 64,
        base_publication_artifact_id=uuid4(),
        base_publication_sha256=publication_document_v5_sha256(publication),
        blocks=opinion_blocks_with_observations(
            (
                OpinionBlock(
                    OpinionBlockType.KEY_FINDINGS,
                    "Constat de démonstration.",
                    qualification=OpinionClaimQualification.HYPOTHESIS,
                ),
            ),
            (note,),
        ),
        author_id="fixture-analyst",
    )

    class _Uow:
        def __init__(self) -> None:
            self.requested: list[UUID] = []
            self.evidence_observations = SimpleNamespace(get=self._get)

        async def _get(self, observation_id: UUID) -> EvidenceObservation | None:
            self.requested.append(observation_id)
            return {
                cited_code.id: cited_code,
                cited_ioc.id: cited_ioc,
                uncited.id: uncited,
            }.get(observation_id)

        async def __aenter__(self) -> _Uow:
            return self

        async def __aexit__(self, *exc: object) -> None:
            return None

    class _Store:
        def __init__(self) -> None:
            self.reads: list[UUID] = []

        async def read_bytes(self, blob_id: UUID, *, max_bytes: int | None = None) -> bytes:
            self.reads.append(blob_id)
            return raw

    uow = _Uow()
    store = _Store()
    context = asyncio.run(
        load_note_block_render_context(
            uow_factory=cast(Any, lambda: uow),
            artifact_store=cast(Any, store),
            opinion=opinion,
            base_document=publication,
        )
    )

    assert context is not None
    assert {item.id for item in context.observations} == {cited_code.id, cited_ioc.id}
    assert context.blobs == {cited_code.id: raw}
    assert set(uow.requested) == {cited_code.id, cited_ioc.id}
    assert uncited.id not in uow.requested
    assert store.reads == [cited_code.blob_id]
    blockedless = replace(
        opinion, blocks=opinion_blocks_with_observations((), ()), content_sha256=""
    )
    unbounded_uow = _Uow()
    assert (
        asyncio.run(
            load_note_block_render_context(
                uow_factory=cast(Any, lambda: unbounded_uow),
                artifact_store=cast(Any, store),
                opinion=blockedless,
                base_document=publication,
            )
        )
        is None
    )
    assert unbounded_uow.requested == []


def test_note_stale_block_ref_refused() -> None:
    """B5b: an element hash that no longer matches the rendered article is stale."""
    table = _after_paragraph_table()
    v9, base, _extraction = _note_block_v9(tables=(table,))
    investigation_id = uuid4()
    published_table = base.document.tables[0]
    note = AnalystObservationV1(
        id=UUID(int=8818),
        anchor=_NOTE_BLOCK_ANCHOR,
        text="NOTE-STALE-REFERENCE",
        state=AnalystObservationState.UNRESOLVED,
        relation=AnalystObservationRelation.UNRESOLVED,
        content_blocks=(
            ArticleElementNoteBlockV1(
                kind=NoteArticleElementKind.TABLE,
                element_key=published_table.key,
                element_sha256="0" * 64,
                anchor=_NOTE_BLOCK_ANCHOR,
            ),
        ),
    )
    document = _note_v9_with_opinion(
        v9,
        notes=(note,),
        observations=(),
        investigation_id=investigation_id,
    )

    with pytest.raises(NoteContentBlockRenderError) as failure:
        project_publication_to_typst_model(document)
    assert failure.value.code == "note_block_ref_stale"


def test_real_typst_note_article_element_cross_reference_without_duplication(
    tmp_path: Path,
) -> None:
    """End to end: a V9 article_element note is accepted, augmented and compiled.

    The note references the element rendered at its article placement; the PDF
    must carry the deterministic cross-reference and only one copy of the
    element itself.
    """
    binary = os.environ.get("TYPST_BINARY") or str(
        Path.home() / ".local" / "share" / "autowork-tools" / "typst"
    )
    if shutil.which(binary) is None and not Path(binary).is_file():
        pytest.skip("pinned Typst 0.15.1 is unavailable")
    version = subprocess.run(
        (binary, "--version"), capture_output=True, check=True, text=True, timeout=10
    ).stdout.strip()
    assert version.startswith("typst 0.15.1"), version

    table = _after_paragraph_table()
    v9, base, _extraction = _note_block_v9(tables=(table,))
    investigation_id = uuid4()
    published_table = base.document.tables[0]
    block = ArticleElementNoteBlockV1(
        kind=NoteArticleElementKind.TABLE,
        element_key=published_table.key,
        element_sha256=publication_element_sha256(
            base.document, kind="table", key=published_table.key
        ),
        anchor=_NOTE_BLOCK_ANCHOR,
    )
    # The acceptance path validates the block against the pinned V9 facts.
    context = NoteContentBlockContextV1(
        investigation_id=investigation_id,
        anchor=_NOTE_BLOCK_ANCHOR,
        elements=note_article_element_facts(v9),
        observations={},
        blobs={},
        protocol_figures=(),
    )
    (resolved,) = validate_note_content_blocks((block,), context)
    assert resolved.anchor == _NOTE_BLOCK_ANCHOR

    note = AnalystObservationV1(
        id=UUID(int=8831),
        anchor=_NOTE_BLOCK_ANCHOR,
        text="NOTE-TABLE-CROSS-REFERENCE",
        state=AnalystObservationState.UNRESOLVED,
        relation=AnalystObservationRelation.UNRESOLVED,
        content_blocks=(block,),
    )
    document = _note_v9_with_opinion(
        v9,
        notes=(note,),
        observations=(),
        investigation_id=investigation_id,
    )
    # The publication is the augmented artifact itself, never a rebuilt V9.
    assert type(document) is PublicationDocumentV9
    assert document.resolved_placements == v9.resolved_placements

    bundle = load_template_bundle(Path(__file__).resolve().parents[2] / "chpTypst")
    render_context = NoteBlockRenderContext(element_facts=note_article_element_facts(document))
    rendered = TypstRenderer().render(
        document,
        bundle,
        document_label="Note article element — publication V9",
        note_block_context=render_context,
    )
    workspace = tmp_path / "workspace"
    for template_file in bundle.files:
        destination = workspace / template_file.relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(template_file.content)
    render_data_path = workspace / rendered.render_data_relative_path
    render_data_path.parent.mkdir(parents=True, exist_ok=True)
    render_data_path.write_bytes(rendered.render_data_bytes)

    repository_root = Path(__file__).resolve().parents[2]
    font_root = Path(os.environ.get("FONT_BUNDLE_ROOT", str(repository_root / "chpTypst")))
    font_snapshot = load_font_bundle_snapshot(
        font_root, repository_root / "infra" / "typst-fonts.lock"
    )
    font_paths = materialize_font_bundle(font_snapshot, tmp_path / "fonts")
    compiled = asyncio.run(
        TypstSubprocessCompiler(binary=binary).compile(
            TypstCompileRequest(
                workspace_root=workspace,
                entrypoint_relative_path=rendered.entrypoint_relative_path,
                font_paths=font_paths,
            )
        )
    )

    pdf = PdfReader(io.BytesIO(compiled.content), strict=True)
    assert pdf.pages
    compact_text = "".join("".join((page.extract_text() or "").split()) for page in pdf.pages)
    assert "NOTE-TABLE-CROSS-REFERENCE" in compact_text
    # The deterministic cross-reference names the element at its placement.
    assert "".join(f"Tableau — {published_table.title}".split()) in compact_text
    # The note references the element; it never duplicates its row data.
    assert compact_text.count("-enc") == 1


def _draft_preview_opinion(
    base: PublicationDocumentV5,
    *,
    investigation_id: UUID,
    notes: tuple[AnalystObservationV1, ...] = (),
) -> AnalystOpinionV1:
    return AnalystOpinionV1(
        id=UUID(int=8850),
        investigation_id=investigation_id,
        synthesis_artifact_id=UUID(int=8851),
        synthesis_sha256="1" * 64,
        base_publication_artifact_id=UUID(int=8852),
        base_publication_sha256=publication_document_v5_sha256(base),
        blocks=opinion_blocks_with_observations(
            (
                OpinionBlock(
                    OpinionBlockType.KEY_FINDINGS,
                    "Constat de démonstration.",
                    qualification=OpinionClaimQualification.HYPOTHESIS,
                ),
            ),
            notes,
        ),
        author_id="fixture-analyst",
        status=AnalystOpinionStatus.DRAFT,
    )


def test_draft_preview_v9_renders_exactly_like_the_publication() -> None:
    """R6-F1: a preview pinned on a V9 base keeps the resolved placements and notes.

    The preview projects the exact V5 body of the pinned V9 envelope, so its
    lead paragraph, after-paragraph element and anchored note blocks must equal
    the ones the augmented publication renders, instead of refusing the
    placement as ``editorial_anchor_stale``.
    """
    table = _after_paragraph_table()
    v9, base, _extraction = _note_block_v9(tables=(table,))
    investigation_id = uuid4()
    published_table = base.document.tables[0]
    evidence = EvidenceObservation(
        investigation_id=investigation_id,
        kind=ObservationEvidenceKind.OTHER,
        blob_id=uuid4(),
        sha256="e" * 64,
        source="vt",
        source_version="fixture.v1",
        observed_at=datetime.now(UTC),
        object_type="file",
        object_id="preview-sample.bin",
    )
    (element_fact,) = note_article_element_facts(v9)
    note = AnalystObservationV1(
        id=UUID(int=8853),
        anchor=_NOTE_BLOCK_ANCHOR,
        text="NOTE-PREVIEW-V9",
        state=AnalystObservationState.UNRESOLVED,
        relation=AnalystObservationRelation.UNRESOLVED,
        content_blocks=(
            ArticleElementNoteBlockV1(
                kind=NoteArticleElementKind.TABLE,
                element_key=published_table.key,
                element_sha256=element_fact.element_sha256,
                anchor=_NOTE_BLOCK_ANCHOR,
            ),
            IocTableNoteBlockV1(
                observation_ids=(evidence.id,),
                title=note_ioc_table_title(1),
            ),
        ),
    )
    opinion = _draft_preview_opinion(base, investigation_id=investigation_id, notes=(note,))
    preview = TypstDraftPreviewDocument(
        base_publication=base,
        analyst_opinion=opinion,
        publication_v9=v9,
    )
    context = NoteBlockRenderContext(
        observations=(evidence,),
        element_facts=note_article_element_facts(v9),
    )

    model = project_publication_to_typst_model(preview, note_block_context=context)
    synthesis = next(item for item in model.content_sections if item["type"] == "synthesis")
    blocks = synthesis["blocks"]
    assert [block.get("type") for block in blocks] == [
        "paragraph",
        "table",
        "analyst_observation",
    ]
    assert blocks[1]["key"] == published_table.key
    assert blocks[2]["text"] == "NOTE-PREVIEW-V9"
    assert [item["block_type"] for item in blocks[2]["blocks"]] == [
        "article_element",
        "ioc_table",
    ]

    # The exact same body blocks as the augmented publication for this V9 base.
    published = _note_v9_with_opinion(
        v9,
        notes=(note,),
        observations=(evidence,),
        investigation_id=investigation_id,
    )
    publication_model = project_publication_to_typst_model(
        published,
        note_block_context=NoteBlockRenderContext(
            observations=(evidence,),
            element_facts=note_article_element_facts(published),
        ),
    )
    publication_synthesis = next(
        item for item in publication_model.content_sections if item["type"] == "synthesis"
    )
    assert publication_synthesis["blocks"] == blocks


def test_draft_preview_v9_real_typst_pdf_places_element_and_note(tmp_path: Path) -> None:
    """R6-F1: the real preview PDF carries the element once, then its anchored note."""
    binary = os.environ.get("TYPST_BINARY") or str(
        Path.home() / ".local" / "share" / "autowork-tools" / "typst"
    )
    if shutil.which(binary) is None and not Path(binary).is_file():
        pytest.skip("pinned Typst 0.15.1 is unavailable")
    version = subprocess.run(
        (binary, "--version"), capture_output=True, check=True, text=True, timeout=10
    ).stdout.strip()
    assert version.startswith("typst 0.15.1"), version

    table = _after_paragraph_table()
    v9, base, _extraction = _note_block_v9(tables=(table,))
    investigation_id = uuid4()
    published_table = base.document.tables[0]
    evidence = EvidenceObservation(
        investigation_id=investigation_id,
        kind=ObservationEvidenceKind.OTHER,
        blob_id=uuid4(),
        sha256="e" * 64,
        source="vt",
        source_version="fixture.v1",
        observed_at=datetime.now(UTC),
        object_type="file",
        object_id="preview-sample.bin",
    )
    (element_fact,) = note_article_element_facts(v9)
    note = AnalystObservationV1(
        id=UUID(int=8854),
        anchor=_NOTE_BLOCK_ANCHOR,
        text="NOTE-PREVIEW-V9",
        state=AnalystObservationState.UNRESOLVED,
        relation=AnalystObservationRelation.UNRESOLVED,
        content_blocks=(
            ArticleElementNoteBlockV1(
                kind=NoteArticleElementKind.TABLE,
                element_key=published_table.key,
                element_sha256=element_fact.element_sha256,
                anchor=_NOTE_BLOCK_ANCHOR,
            ),
            IocTableNoteBlockV1(
                observation_ids=(evidence.id,),
                title=note_ioc_table_title(1),
            ),
        ),
    )
    opinion = _draft_preview_opinion(base, investigation_id=investigation_id, notes=(note,))
    preview = TypstDraftPreviewDocument(
        base_publication=base,
        analyst_opinion=opinion,
        publication_v9=v9,
    )
    bundle = load_template_bundle(Path(__file__).resolve().parents[2] / "chpTypst")
    rendered = TypstRenderer().render(
        preview,
        bundle,
        document_label="Aperçu d'avis V9 — placement résolu",
        note_block_context=NoteBlockRenderContext(
            observations=(evidence,),
            element_facts=note_article_element_facts(v9),
        ),
    )
    workspace = tmp_path / "workspace"
    for template_file in bundle.files:
        destination = workspace / template_file.relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(template_file.content)
    render_data_path = workspace / rendered.render_data_relative_path
    render_data_path.parent.mkdir(parents=True, exist_ok=True)
    render_data_path.write_bytes(rendered.render_data_bytes)

    repository_root = Path(__file__).resolve().parents[2]
    font_root = Path(os.environ.get("FONT_BUNDLE_ROOT", str(repository_root / "chpTypst")))
    font_snapshot = load_font_bundle_snapshot(
        font_root, repository_root / "infra" / "typst-fonts.lock"
    )
    font_paths = materialize_font_bundle(font_snapshot, tmp_path / "fonts")
    compiled = asyncio.run(
        TypstSubprocessCompiler(binary=binary).compile(
            TypstCompileRequest(
                workspace_root=workspace,
                entrypoint_relative_path=rendered.entrypoint_relative_path,
                font_paths=font_paths,
            )
        )
    )

    pdf = PdfReader(io.BytesIO(compiled.content), strict=True)
    assert pdf.pages
    compact_text = "".join("".join((page.extract_text() or "").split()) for page in pdf.pages)
    lead_text = "".join(base.document.lead[0].text.split())
    table_title = "".join(published_table.title.split())
    note_text = "NOTE-PREVIEW-V9"
    cross_reference = "".join(f"Tableau — {published_table.title}".split())
    assert note_text in compact_text
    assert cross_reference in compact_text
    assert compact_text.index(lead_text) < compact_text.index(table_title)
    assert compact_text.index(table_title) < compact_text.index(note_text)
    # The element is rendered once at its placement; the note only references it.
    assert compact_text.count("-enc") == 1


def test_draft_preview_v9_refuses_stale_envelope_anchor_and_element_hash() -> None:
    """R6-F1: the preview keeps the fail-closed refusals for stale canonical state."""
    table = _after_paragraph_table()
    v9, base, _extraction = _note_block_v9(tables=(table,))
    other_v9, _other_base, _other_extraction = _note_block_v9()
    investigation_id = uuid4()
    note = AnalystObservationV1(
        id=UUID(int=8855),
        anchor=_NOTE_BLOCK_ANCHOR,
        text="NOTE-PREVIEW-STALE",
        state=AnalystObservationState.UNRESOLVED,
        relation=AnalystObservationRelation.UNRESOLVED,
    )
    opinion = _draft_preview_opinion(base, investigation_id=investigation_id, notes=(note,))

    # An envelope wrapping another V5 body must never supply its placements.
    with pytest.raises(ValueError, match="does not wrap the projected V5 body"):
        TypstDraftPreviewDocument(
            base_publication=base,
            analyst_opinion=opinion,
            publication_v9=other_v9,
        )

    stale_anchor_opinion = _draft_preview_opinion(
        base,
        investigation_id=investigation_id,
        notes=(replace(note, anchor="lead:9999"),),
    )
    with pytest.raises(ValueError, match="anchor is stale or unsupported"):
        TypstDraftPreviewDocument(
            base_publication=base,
            analyst_opinion=stale_anchor_opinion,
            publication_v9=v9,
        )

    published_table = base.document.tables[0]
    stale_hash_note = AnalystObservationV1(
        id=UUID(int=8856),
        anchor=_NOTE_BLOCK_ANCHOR,
        text="NOTE-PREVIEW-STALE-HASH",
        state=AnalystObservationState.UNRESOLVED,
        relation=AnalystObservationRelation.UNRESOLVED,
        content_blocks=(
            ArticleElementNoteBlockV1(
                kind=NoteArticleElementKind.TABLE,
                element_key=published_table.key,
                element_sha256="0" * 64,
                anchor=_NOTE_BLOCK_ANCHOR,
            ),
        ),
    )
    stale_hash_preview = TypstDraftPreviewDocument(
        base_publication=base,
        analyst_opinion=_draft_preview_opinion(
            base,
            investigation_id=investigation_id,
            notes=(stale_hash_note,),
        ),
        publication_v9=v9,
    )
    with pytest.raises(NoteContentBlockRenderError) as failure:
        project_publication_to_typst_model(
            stale_hash_preview,
            note_block_context=NoteBlockRenderContext(
                element_facts=note_article_element_facts(v9),
            ),
        )
    assert failure.value.code == "note_block_ref_stale"


def test_note_protocol_figure_references_the_opinion_annex() -> None:
    """B5b: protocol figures stay in the opinion annex; the note cross-references one."""
    protocol_figures = tuple(
        OpinionProtocolFigureV1(
            kind=kind,
            model_sha256="a" * 64,
            spec_sha256="b" * 64,
            asset_id=UUID(int=8820 + index),
            asset_sha256="c" * 64,
            preview_sha256="d" * 64,
            pin_sha256="e" * 64,
            compiler_name="d2",
            compiler_version="0.6.9",
            compiler_policy_version=PROTOCOL_DIAGRAM_FIGURE_POLICY_VERSION,
            caption="Vue de protocole de démonstration.",
        )
        for index, kind in enumerate(
            (
                ProtocolDiagramKind.SEQUENCE,
                ProtocolDiagramKind.STATE_MACHINE,
                ProtocolDiagramKind.MESSAGE_FORMAT,
            )
        )
    )
    publication = _base_v5()
    note = AnalystObservationV1(
        id=UUID(int=8821),
        anchor=_NOTE_BLOCK_ANCHOR,
        text="NOTE-PROTOCOL-REFERENCE",
        state=AnalystObservationState.UNRESOLVED,
        relation=AnalystObservationRelation.UNRESOLVED,
        content_blocks=(
            ProtocolFigureNoteBlockV1(
                kind=ProtocolDiagramKind.SEQUENCE,
                asset_sha256="c" * 64,
            ),
        ),
    )
    opinion = AnalystOpinionV1(
        investigation_id=uuid4(),
        synthesis_artifact_id=uuid4(),
        synthesis_sha256="4" * 64,
        base_publication_artifact_id=uuid4(),
        base_publication_sha256=publication_document_v5_sha256(publication),
        blocks=opinion_blocks_with_observations(
            (
                OpinionBlock(
                    OpinionBlockType.KEY_FINDINGS,
                    "Constat de démonstration.",
                    qualification=OpinionClaimQualification.HYPOTHESIS,
                ),
            ),
            (note,),
        ),
        author_id="fixture-analyst",
        protocol_figures=protocol_figures,
    )
    model = project_publication_to_typst_model(
        TypstDraftPreviewDocument(base_publication=publication, analyst_opinion=opinion)
    )
    synthesis = next(item for item in model.content_sections if item["type"] == "synthesis")
    note_block = next(
        item
        for item in synthesis["blocks"]
        if item.get("type") == "analyst_observation" and item.get("blocks")
    )
    (reference,) = note_block["blocks"]
    assert reference["block_type"] == "protocol_figure"
    assert reference["reference_label"] == ("Figure 1 — Séquence réseau du protocole, en annexe")
    provenance = "".join(part["text"] for part in reference["provenance"])
    assert "c" * 64 in provenance
    opinion_section = next(
        item for item in model.content_sections if item["type"] == "analyst_opinion"
    )
    assert [figure["key"] for figure in opinion_section["protocol_figures"]] == [
        "protocol-sequence",
        "protocol-state_machine",
        "protocol-message_format",
    ]


def test_note_without_content_blocks_renders_byte_identically(tmp_path: Path) -> None:
    """B5b: a v1 note without blocks keeps the exact pre-B5b render-data bytes."""
    publication = _base_v5()
    note = AnalystObservationV1(
        id=UUID(int=8814),
        anchor=_NOTE_BLOCK_ANCHOR,
        text="NOTE-SANS-BLOCS",
        state=AnalystObservationState.UNRESOLVED,
        relation=AnalystObservationRelation.UNRESOLVED,
    )
    opinion = AnalystOpinionV1(
        investigation_id=UUID(int=8815),
        synthesis_artifact_id=UUID(int=8816),
        synthesis_sha256="3" * 64,
        base_publication_artifact_id=UUID(int=8817),
        base_publication_sha256=publication_document_v5_sha256(publication),
        blocks=opinion_blocks_with_observations(
            (
                OpinionBlock(
                    OpinionBlockType.KEY_FINDINGS,
                    "Constat de démonstration.",
                    qualification=OpinionClaimQualification.HYPOTHESIS,
                ),
            ),
            (note,),
        ),
        author_id="fixture-analyst",
    )
    preview = TypstDraftPreviewDocument(base_publication=publication, analyst_opinion=opinion)
    renderer, bundle = _renderer(tmp_path)

    rendered = _render(renderer, preview, bundle)
    data = json.loads(rendered.render_data_bytes)
    synthesis = next(item for item in data["content_sections"] if item["type"] == "synthesis")
    projected_note = next(
        item for item in synthesis["blocks"] if item["type"] == "analyst_observation"
    )

    # No key, no empty list: the v1 projection stays byte-identical to 6821f826.
    assert "blocks" not in projected_note
    assert hashlib.sha256(rendered.render_data_bytes).hexdigest() == (
        "15ad059ddd079267dfd5aa4cc6ec5f6cc6da2b2f6c0c9a13818448aa79344ca3"
    )


def test_note_code_ref_exact_bytes(tmp_path: Path) -> None:
    """B5b: code bytes are sliced from the observation blob, never supplied text."""
    raw = b"xxCreateFileWyy"
    excerpt = raw[2:13]
    publication = _base_v5()
    observation = EvidenceObservation(
        investigation_id=uuid4(),
        kind=ObservationEvidenceKind.BINARY_ANALYSIS,
        blob_id=uuid4(),
        sha256=hashlib.sha256(raw).hexdigest(),
        source="native_analysis",
        source_version="fixture.v1",
        observed_at=datetime.now(UTC),
        object_type="native_analysis",
        object_id="note-code-sample.bin",
    )
    block = CanonicalCodeNoteBlockV1(
        observation_id=observation.id,
        blob_sha256=observation.sha256,
        offset=2,
        length=len(excerpt),
        excerpt_sha256=hashlib.sha256(excerpt).hexdigest(),
        language="c",
    )
    ioc_observation = EvidenceObservation(
        investigation_id=observation.investigation_id,
        kind=ObservationEvidenceKind.SHODAN_OBSERVATION,
        blob_id=uuid4(),
        sha256="f" * 64,
        source="shodan",
        source_version="fixture.v1",
        observed_at=datetime.now(UTC),
        object_type="ip",
        object_id="192.0.2.10",
    )
    ioc_block = IocTableNoteBlockV1(
        observation_ids=(ioc_observation.id,),
        title=note_ioc_table_title(1),
    )
    note = AnalystObservationV1(
        id=UUID(int=8813),
        anchor=_NOTE_BLOCK_ANCHOR,
        text="NOTE-CODE-FIXTURE-ALPHA",
        state=AnalystObservationState.UNRESOLVED,
        relation=AnalystObservationRelation.UNRESOLVED,
        content_blocks=(block, ioc_block),
    )
    opinion = AnalystOpinionV1(
        investigation_id=observation.investigation_id,
        synthesis_artifact_id=uuid4(),
        synthesis_sha256="2" * 64,
        base_publication_artifact_id=uuid4(),
        base_publication_sha256=publication_document_v5_sha256(publication),
        blocks=opinion_blocks_with_observations(
            (
                OpinionBlock(
                    OpinionBlockType.KEY_FINDINGS,
                    "Constat de démonstration.",
                    qualification=OpinionClaimQualification.HYPOTHESIS,
                ),
            ),
            (note,),
        ),
        author_id="fixture-analyst",
    )
    preview = TypstDraftPreviewDocument(
        base_publication=publication,
        analyst_opinion=opinion,
    )
    context = NoteBlockRenderContext(
        observations=(observation, ioc_observation),
        blobs={observation.id: raw},
    )
    model = project_publication_to_typst_model(preview, note_block_context=context)
    synthesis = next(item for item in model.content_sections if item["type"] == "synthesis")
    note_block = next(
        item
        for item in synthesis["blocks"]
        if item.get("type") == "analyst_observation" and item.get("blocks")
    )
    assert [item["block_type"] for item in note_block["blocks"]] == [
        "canonical_code",
        "ioc_table",
    ]
    code_block = note_block["blocks"][0]
    assert code_block["excerpt"] == "CreateFileW"
    assert "".join(line["text"] for line in code_block["code_lines"]) == "CreateFileW"
    provenance = "".join(part["text"] for part in code_block["provenance"])
    assert observation.sha256 in provenance
    assert hashlib.sha256(excerpt).hexdigest() in provenance
    assert "octets 2-13" in provenance
    assert "native_analysis" in provenance

    # A missing or divergent blob refuses the whole note, never a partial render.
    for stale_blobs in ({}, {observation.id: b"tampered bytes"}):
        with pytest.raises(NoteContentBlockRenderError) as failure:
            project_publication_to_typst_model(
                preview,
                note_block_context=NoteBlockRenderContext(
                    observations=(observation,), blobs=stale_blobs
                ),
            )
        assert failure.value.code == "note_code_ref_stale"

    binary = os.environ.get("TYPST_BINARY") or str(
        Path.home() / ".local" / "share" / "autowork-tools" / "typst"
    )
    if shutil.which(binary) is None and not Path(binary).is_file():
        pytest.skip("pinned Typst 0.15.1 is unavailable")
    version = subprocess.run(
        (binary, "--version"), capture_output=True, check=True, text=True, timeout=10
    ).stdout.strip()
    assert version.startswith("typst 0.15.1"), version

    repository_root = Path(__file__).resolve().parents[2]
    template_root = repository_root / "chpTypst"
    bundle = load_template_bundle(template_root)
    rendered = TypstRenderer().render(
        preview,
        bundle,
        document_label="Note code fixture",
        note_block_context=context,
    )
    workspace = tmp_path / "workspace"
    for template_file in bundle.files:
        destination = workspace / template_file.relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(template_file.content)
    render_data_path = workspace / rendered.render_data_relative_path
    render_data_path.parent.mkdir(parents=True, exist_ok=True)
    render_data_path.write_bytes(rendered.render_data_bytes)
    font_root = Path(os.environ.get("FONT_BUNDLE_ROOT", str(template_root)))
    fonts_lock = Path(
        os.environ.get(
            "TYPST_FONTS_LOCK_FILE",
            str(repository_root / "infra" / "typst-fonts.lock"),
        )
    )
    font_snapshot = load_font_bundle_snapshot(font_root, fonts_lock)
    font_paths = materialize_font_bundle(font_snapshot, tmp_path / "fonts")
    compiled = asyncio.run(
        TypstSubprocessCompiler(binary=binary).compile(
            TypstCompileRequest(
                workspace_root=workspace,
                entrypoint_relative_path=rendered.entrypoint_relative_path,
                font_paths=font_paths,
            )
        )
    )
    assert compiled.media_type == "application/pdf"
    pdf = PdfReader(io.BytesIO(compiled.content), strict=True)
    assert pdf.pages
    pdf_text = " ".join((page.extract_text() or "") for page in pdf.pages)
    compact = "".join(pdf_text.split())
    anchor_text = "".join(publication.document.lead[0].text.split())
    assert "NOTE-CODE-FIXTURE-ALPHA" in compact
    assert "CreateFileW" in compact
    assert "TableauIOC(1)" in compact
    assert "192.0.2.10" in compact
    assert observation.sha256[:32] in compact
    assert compact.index(anchor_text) < compact.index("NOTE-CODE-FIXTURE-ALPHA")
    assert compact.index("NOTE-CODE-FIXTURE-ALPHA") < compact.index("CreateFileW")
    assert compact.index("CreateFileW") < compact.index("TableauIOC(1)")
