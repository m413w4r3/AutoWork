"""Compile a multi-article EditionDocumentV2 with the real pinned Typst binary."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import xml.etree.ElementTree as ET
from dataclasses import replace
from io import BytesIO
from itertools import pairwise
from pathlib import Path
from uuid import UUID

import anyio
import pytest
from pypdf import PdfReader

from cti_app.application.edition_typst_rendering import EditionTypstRenderer
from cti_app.application.typst_compilation import (
    TypstCompileRequest,
    load_font_bundle_snapshot,
    materialize_font_bundle,
)
from cti_app.application.typst_rendering import (
    TypstDraftPreviewDocument,
    TypstRenderer,
    load_template_bundle,
)
from cti_app.domain.analyst_opinion import AnalystOpinionV1, OpinionBlock, OpinionBlockType
from cti_app.domain.edition_publication import EditionDocumentV2, EditionPublicationV2
from cti_app.domain.production_editorial_enrichment import EnrichmentPlacementKind
from cti_app.domain.publication_document import publication_document_v5_sha256
from cti_app.infrastructure.typst_compiler import TypstSubprocessCompiler
from tests.test_publication_builder_v4 import _frontmatter_v6_case
from tests.test_publication_semantic_text import _v5_document
from tests.test_typst_chp_parity import (
    _FONT_BUNDLE_LOCK,
    _assert_real_typst_keep_with_next_case,
    _build_workspace,
    _embedded_visual_xobject_count,
    _normalized_pdf_text,
)
from tests.test_typst_compiler_runtime import (
    _COPYABLE_VALUES,
    _COPYABLE_WINDOWS_PATH,
    _assert_exact_copyable_pdf_text,
    _copyable_ioc_document,
    _install_narrow_technical_table,
)
from tests.test_typst_rendering import _diagram_at, _figure_at, _full_document, _table_at

pytest_plugins = ("tests.test_typst_compiler_runtime",)

_CHP_TYPST_ROOT = Path(__file__).resolve().parents[2] / "chpTypst"
_EDITION_MANIFEST = "edition-renderer-manifest.json"
_EDITION_RENDER_LABEL = (
    "Édition publiée 00000000-0000-4000-8000-000000000020 "
    "— release 00000000-0000-4000-8000-000000000099"
)


def _edition_document() -> EditionDocumentV2:
    base = _full_document(
        tables=(
            _table_at("edition-table", "Edition command table", EnrichmentPlacementKind.AFTER_LEAD),
        ),
        diagrams=(_diagram_at("edition-diagram", "Edition diagram", EnrichmentPlacementKind.END),),
        figures=(_figure_at("edition-figure", "Edition figure", EnrichmentPlacementKind.END),),
    )
    publications = tuple(
        EditionPublicationV2(
            position=position,
            subject_id=publication.subject_id,
            document=publication,
        )
        for position, publication in (
            (2, replace(base, title="Zulu second article", subject_id=UUID(int=2))),
            (1, replace(base, title="Alpha first article", subject_id=UUID(int=1))),
        )
    )
    return EditionDocumentV2(
        edition={
            "id": "00000000-0000-4000-8000-000000000020",
            "country": "Iran",
            "country_code": "IR",
            "period_start": "2026-09-01",
            "period_end": "2026-09-30",
            "tlp": "GREEN",
            "languages": ["fr"],
            "version": 1,
        },
        publications=publications,
    )


@pytest.mark.asyncio
async def test_real_typst_compiles_multi_article_edition_in_one_pass(
    tmp_path: Path,
    typst_binary: str,
    font_bundle_root: Path,
) -> None:
    bundle = load_template_bundle(_CHP_TYPST_ROOT, manifest_name=_EDITION_MANIFEST)
    source = EditionTypstRenderer().render(
        _edition_document(), bundle, document_label=_EDITION_RENDER_LABEL
    )
    workspace_root = tmp_path / "workspace"
    _build_workspace(
        workspace_root,
        bundle_files=bundle.files,
        render_data_bytes=source.render_data_bytes,
        media_refs=source.media_refs,
    )
    # _build_workspace writes the publication data path; the edition entrypoint reads its own.
    (workspace_root / "RENDERER" / "render-data.json").unlink()
    destination = workspace_root / source.render_data_relative_path
    destination.write_bytes(source.render_data_bytes)

    font_snapshot = load_font_bundle_snapshot(font_bundle_root, _FONT_BUNDLE_LOCK)
    font_root = tmp_path / "font-snapshot"
    font_root.mkdir()
    compiled = await TypstSubprocessCompiler(binary=str(typst_binary)).compile(
        TypstCompileRequest(
            workspace_root=workspace_root,
            entrypoint_relative_path=source.entrypoint_relative_path,
            font_paths=materialize_font_bundle(font_snapshot, font_root),
        )
    )
    reader = PdfReader(BytesIO(compiled.content), strict=True)
    text, _ = _normalized_pdf_text(reader)

    assert len(reader.pages) >= 4
    for expected in (
        "Bulletin de veille CTI",
        "Iran",
        "IR",
        "septembre 2026",
        "Alpha first article",
        "Zulu second article",
        "Edition command table",
        "Edition diagram",
        "Edition figure",
        "Source :",
        "Display ip",
    ):
        assert expected in text


@pytest.mark.asyncio
async def test_real_typst_cover_has_internal_links_briefs_and_release_annexes(
    tmp_path: Path,
    typst_binary: str,
    font_bundle_root: Path,
) -> None:
    base = _full_document()
    brief = replace(
        base,
        subject_id=UUID(int=3),
        title="Short operational update",
        lead=(replace(base.lead[0], text="Brief body without titled sections."),),
        sections=(),
        timeline=(),
        indicators=(),
        uncertainties=(),
        tables=(),
        diagrams=(),
        figures=(),
        charts=(),
        additional_source_ids=frozenset(),
    )
    document = EditionDocumentV2(
        edition={
            "id": "00000000-0000-4000-8000-000000000030",
            "country": "Iran",
            "country_code": "IR",
            "period_start": "2026-09-01",
            "period_end": "2026-09-30",
            "tlp": "GREEN",
            "languages": ["fr"],
            "version": 5,
        },
        publications=(
            EditionPublicationV2(
                position=1,
                subject_id=base.subject_id,
                document=replace(base, title="Article with titled synthesis"),
            ),
            EditionPublicationV2(
                position=2,
                subject_id=brief.subject_id,
                document=brief,
            ),
        ),
    )
    bundle = load_template_bundle(_CHP_TYPST_ROOT, manifest_name=_EDITION_MANIFEST)
    source = EditionTypstRenderer().render(document, bundle, document_label=_EDITION_RENDER_LABEL)
    render_data = json.loads(source.render_data_bytes)
    workspace_root = tmp_path / "workspace"
    _build_workspace(
        workspace_root,
        bundle_files=bundle.files,
        render_data_bytes=source.render_data_bytes,
        media_refs=source.media_refs,
    )
    (workspace_root / "RENDERER" / "render-data.json").unlink()
    (workspace_root / source.render_data_relative_path).write_bytes(source.render_data_bytes)
    font_snapshot = load_font_bundle_snapshot(font_bundle_root, _FONT_BUNDLE_LOCK)
    font_root = tmp_path / "font-snapshot"
    font_root.mkdir()
    compiled = await TypstSubprocessCompiler(binary=typst_binary).compile(
        TypstCompileRequest(
            workspace_root=workspace_root,
            entrypoint_relative_path=source.entrypoint_relative_path,
            font_paths=materialize_font_bundle(font_snapshot, font_root),
        )
    )

    reader = PdfReader(BytesIO(compiled.content), strict=True)
    text, _ = _normalized_pdf_text(reader)
    links = [
        annotation.get_object()
        for annotation in (reader.pages[0].get("/Annots") or [])
        if annotation.get_object().get("/Subtype") == "/Link"
    ]
    internal_links = [
        annotation
        for annotation in links
        if annotation.get("/Dest") is not None or annotation.get("/A", {}).get("/S") == "/GoTo"
    ]

    assert [item["kind"] for item in render_data["publications"]] == ["article", "brief"]
    assert [item["number"] for item in render_data["publications"]] == ["01", "02"]
    assert len(internal_links) >= 2
    for expected in (
        "Résumé de l\u2019actualité",
        "Autres brèves relevées",
        "Short operational update",
        "Règles Suricata",
        "Échantillons relevés",
        "Article CSV",
        "STIX",
        "Excel",
    ):
        assert expected in text
    assert "Chronologie" not in text
    assert "Vue d'ensemble" not in text
    assert "Note de l'analyste" not in text
    # The reader sees a source note, never the internal provenance string.
    assert "Figure 1 from the source publication" not in text
    assert "Provenance :" not in text
    # L5: the uncertainty list is no longer published inside the article body.
    assert "Attribution remains uncertain" not in text
    assert text.count("RÉFÉRENCES") >= 1
    assert text.index("RÉFÉRENCES") < text.index("CHRONOLOGIE") < text.index("SYNTHÈSE")
    page_texts = [page.extract_text() or "" for page in reader.pages]
    first_article_page_text = next(page_text for page_text in page_texts if "SYNTHÈSE" in page_text)
    assert "SYNTHÈSE" in first_article_page_text
    if "CHRONOLOGIE" in first_article_page_text:
        assert first_article_page_text.index("CHRONOLOGIE") < first_article_page_text.index(
            "SYNTHÈSE"
        )
    assert text.index("Article with titled synthesis") < text.index("Short operational update")
    assert "Bulletin-Iran | Actualité des codes et infrastructures Iran | septembre 2026" in text
    for placeholder in (
        "Bulletin-CODE",
        "Bulletin n°XX",
        "infrastructures X",
        "XX",
        "CODE",
    ):
        assert placeholder not in text
    page_count = len(reader.pages)
    assert f"{page_count} / {page_count}" in text
    assert "Brèves" not in text
    assert "N/A" not in text
    assert "https://example.test/figure.png" not in text
    assert _embedded_visual_xobject_count(reader) >= len(source.media_refs)


@pytest.mark.asyncio
async def test_real_typst_compiles_legacy_and_current_semantic_policies_together(
    tmp_path: Path,
    typst_binary: str,
    font_bundle_root: Path,
) -> None:
    legacy = _v5_document()
    legacy = replace(
        legacy,
        semantic_text=replace(
            legacy.semantic_text,
            policy_version="semantic-annotation-policy-v4-technical-literal-globs",
        ),
    )
    current = _v5_document()
    template_document = _edition_document()
    edition_document = replace(
        template_document,
        publications=(
            EditionPublicationV2(
                position=1,
                subject_id=current.subject_id,
                document=current,
            ),
            EditionPublicationV2(
                position=2,
                subject_id=legacy.subject_id,
                document=legacy,
            ),
        ),
    )
    bundle = load_template_bundle(_CHP_TYPST_ROOT, manifest_name=_EDITION_MANIFEST)
    source = EditionTypstRenderer().render(
        edition_document, bundle, document_label=_EDITION_RENDER_LABEL
    )
    workspace_root = tmp_path / "mixed-policy-edition-workspace"
    _build_workspace(
        workspace_root,
        bundle_files=bundle.files,
        render_data_bytes=source.render_data_bytes,
        media_refs=source.media_refs,
    )
    (workspace_root / "RENDERER" / "render-data.json").unlink()
    (workspace_root / source.render_data_relative_path).write_bytes(source.render_data_bytes)

    font_snapshot = load_font_bundle_snapshot(font_bundle_root, _FONT_BUNDLE_LOCK)
    font_root = tmp_path / "mixed-policy-edition-fonts"
    font_root.mkdir()
    compiled = await TypstSubprocessCompiler(binary=typst_binary).compile(
        TypstCompileRequest(
            workspace_root=workspace_root,
            entrypoint_relative_path=source.entrypoint_relative_path,
            font_paths=materialize_font_bundle(font_snapshot, font_root),
        )
    )
    reader = PdfReader(BytesIO(compiled.content), strict=True)
    text, _ = _normalized_pdf_text(reader)

    assert text.count("Mandiant") >= 2
    assert "Mandiant published the report." in text


@pytest.mark.asyncio
async def test_real_typst_publication_layout_keeps_tables_notes_and_provenance_together(
    tmp_path: Path,
    typst_binary: str,
    font_bundle_root: Path,
) -> None:
    publication, *_ = _frontmatter_v6_case()
    finding_id = UUID(int=701)
    evidence_id = UUID(int=702)
    hash_value = "d" * 64
    document_label = f"synthetic-publication-provenance-reference-{hash_value}"
    table_lines = [
        "Synthetic table preface marker.",
        "| Component | State | Context | Marker |",
        "| --- | --- | --- | --- |",
    ]
    table_lines.extend(
        f"| Node {index:02d} | reachable | synthetic context {index:02d} | PIPE_ROW_{index:02d} |"
        for index in range(1, 46)
    )
    table_lines.append("Synthetic table note marker.")
    opinion = AnalystOpinionV1(
        investigation_id=UUID(int=700),
        synthesis_artifact_id=UUID(int=703),
        synthesis_sha256="a" * 64,
        base_publication_artifact_id=UUID(int=704),
        base_publication_sha256=publication_document_v5_sha256(publication),
        blocks=(
            OpinionBlock(
                OpinionBlockType.KEY_FINDINGS,
                "Run `token`",
                finding_refs=(finding_id,),
                evidence_refs=(evidence_id,),
                certainty=0.9,
            ),
            OpinionBlock(
                OpinionBlockType.NETWORK_AND_INFRASTRUCTURE,
                "\n".join(table_lines),
            ),
        ),
        author_id="fixture-analyst",
    )
    preview = TypstDraftPreviewDocument(base_publication=publication, analyst_opinion=opinion)
    template_bundle = load_template_bundle(_CHP_TYPST_ROOT)
    source = TypstRenderer().render(preview, template_bundle, document_label=document_label)
    render_data = json.loads(source.render_data_bytes)
    section_types = [item["type"] for item in render_data["content_sections"]]
    assert section_types[0] == "references"
    assert section_types.index("references") < section_types.index("synthesis")
    if "chronology" in section_types:
        assert section_types.index("references") < section_types.index("chronology")
        assert section_types.index("chronology") < section_types.index("synthesis")

    opinion_section = next(
        item for item in render_data["content_sections"] if item["type"] == "analyst_opinion"
    )
    projected_blocks = opinion_section["blocks"]
    code_block = next(item for item in projected_blocks if "`token`" in item["text"])
    table_block = next(item for item in projected_blocks if item["pipe_table"] is not None)
    assert code_block["text_spans"][1]["text"] == "token"
    assert code_block["text_spans"][1]["technical"] is True
    assert code_block["qualification_label"] == "Fait étayé"
    assert code_block["finding_refs"][0]["label"] == "[F1]"
    assert code_block["evidence_refs"][0]["label"] == "[E1]"
    assert "certainty_percent" not in code_block
    assert table_block["pipe_table"]["wide"] is True
    assert "Synthetic table note marker." in table_block["pipe_table"]["suffix"]

    annex = next(
        item for item in render_data["content_sections"] if item["type"] == "technical_annex"
    )
    annex["ioc_rows"] = [*annex["ioc_rows"], {"type": "domain"}]
    annex["ioc_row_groups"].extend(
        (
            {
                "heading": "Synthetic hash fixture",
                "rows": [
                    {
                        "type": "hash",
                        "type_label": "SHA-256",
                        "value": hash_value,
                        "status_label": "Confirmé",
                        "indicator_status_short_label": "Confirmé",
                        "relationship_status_short_label": "Lien établi",
                        "hash_context_summary": "HASH_CONTEXT_SYNTHETIC",
                        "context_summary": "HASH_CONTEXT_SYNTHETIC",
                        "source_refs": [{"number": 1}],
                        "draft_refs": [],
                    }
                ],
            },
            {
                "heading": "Synthetic network fixture",
                "rows": [
                    {
                        "type": "domain",
                        "type_label": "Domaine",
                        "value": f"netrow{index:03d}.fixture.invalid",
                        "value_spans": None,
                        "status_label": "Confirmé",
                        "indicator_status_label": "Confirmé",
                        "relationship_status_label": "Lien établi",
                        "context_summary": f"NET_CONTEXT_{index:03d}",
                        "source_refs": [{"number": 1}],
                        "draft_refs": [],
                    }
                    for index in range(1, 51)
                ],
            },
        )
    )
    modified_render_data = json.dumps(
        render_data,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")

    workspace_root = tmp_path / "m07-layout-workspace"
    _build_workspace(
        workspace_root,
        bundle_files=template_bundle.files,
        render_data_bytes=modified_render_data,
        media_refs=source.media_refs,
    )
    font_snapshot = load_font_bundle_snapshot(font_bundle_root, _FONT_BUNDLE_LOCK)
    font_root = tmp_path / "m07-layout-fonts"
    font_root.mkdir()
    compiled = await TypstSubprocessCompiler(binary=typst_binary).compile(
        TypstCompileRequest(
            workspace_root=workspace_root,
            entrypoint_relative_path=source.entrypoint_relative_path,
            font_paths=materialize_font_bundle(font_snapshot, font_root),
        )
    )
    pdf_path = tmp_path / "m07-layout.pdf"
    pdf_path.write_bytes(compiled.content)
    reader = PdfReader(BytesIO(compiled.content), strict=True)
    text, text_without_whitespace = _normalized_pdf_text(reader)
    page_texts = [page.extract_text() or "" for page in reader.pages]

    assert "SYNTHÈSE" in page_texts[0]
    assert re.search(r"Confiance\s*:\s*\d+\s*%", text, flags=re.IGNORECASE) is None
    assert "Fait étayé" in text
    assert "[F1]" in text and "[E1]" in text
    assert "Run token" in text
    assert "Run `token`" not in text
    assert hash_value in text_without_whitespace
    assert "PROVENANCE DE PUBLICATION" in page_texts[0]
    assert "NET_CONTEXT_050" in page_texts[-1]
    assert "netrow050.fixture.invalid" in page_texts[-1]
    assert len(page_texts) >= 5

    mono_fonts: list[str] = []
    context_font_sizes: list[float] = []
    for page in reader.pages:

        def collect_font_details(text_segment, _cm, _tm, font_dictionary, font_size) -> None:
            if "token" in text_segment:
                mono_fonts.append(str((font_dictionary or {}).get("/BaseFont", "")))
            if "NET_CONTEXT_001" in text_segment or "NET_CONTEXT_050" in text_segment:
                context_font_sizes.append(float(font_size))

        page.extract_text(visitor_text=collect_font_details)
    assert mono_fonts and all(
        "mono" in font_name.casefold() or "code" in font_name.casefold() for font_name in mono_fonts
    )
    assert context_font_sizes and min(context_font_sizes) >= 8.5

    pdftotext = shutil.which("pdftotext")
    assert pdftotext is not None, "pdftotext is required for page-box layout assertions"
    bbox_result = await anyio.run_process(
        (pdftotext, "-bbox", str(pdf_path), "-"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    bbox_root = ET.fromstring(bbox_result.stdout.decode("utf-8", errors="replace"))
    bbox_pages = []
    for page_index, page_element in enumerate(
        element for element in bbox_root.iter() if element.tag.rsplit("}", 1)[-1] == "page"
    ):
        bbox_pages.append(
            {
                "index": page_index,
                "height": float(page_element.attrib["height"]),
                "words": [
                    {
                        "text": word.text or "",
                        "x_min": float(word.attrib["xMin"]),
                        "y_min": float(word.attrib["yMin"]),
                        "x_max": float(word.attrib["xMax"]),
                        "y_max": float(word.attrib["yMax"]),
                    }
                    for word in page_element.iter()
                    if word.tag.rsplit("}", 1)[-1] == "word"
                ],
            }
        )

    def pages_containing(token: str) -> list[int]:
        return [
            page["index"]
            for page in bbox_pages
            if token in " ".join(word["text"] for word in page["words"])
        ]

    provenance_line_words = [
        word
        for word in bbox_pages[0]["words"]
        if word["text"] in {"Identité", "complète", ":"}
        or word["text"].startswith("synthetic-publication-provenance-")
    ]
    provenance_line_words.sort(key=lambda word: word["x_min"])
    provenance_gaps = [
        right["x_min"] - left["x_max"]
        for left, right in pairwise(provenance_line_words)
        if abs(right["y_min"] - left["y_min"]) < 2
    ]
    assert provenance_gaps and max(provenance_gaps) < 8

    network_page_indexes = pages_containing("NET_CONTEXT_")
    assert len(network_page_indexes) >= 2
    for page_index in network_page_indexes:
        page = bbox_pages[page_index]
        page_words = page["words"]
        page_text = " ".join(word["text"] for word in page_words)
        assert all(header in page_text for header in ("Type", "Valeur", "Statut", "Contexte"))
        footer_zone = [word for word in page_words if word["y_min"] >= page["height"] - 55]
        row_boxes = [
            word for word in page_words if word["text"].startswith(("NET_CONTEXT_", "netrow"))
        ]
        assert footer_zone and row_boxes
        footer_top = min(word["y_min"] for word in footer_zone)
        assert all(word["y_max"] < footer_top for word in row_boxes)

    annex_page = next(
        index for index, page_text in enumerate(page_texts) if "ANNEXE TECHNIQUE" in page_text
    )
    group_page = next(
        index
        for index, page_text in enumerate(page_texts)
        if "Synthetic network fixture" in page_text
    )
    first_network_row_page = pages_containing("NET_CONTEXT_001")[0]
    assert group_page in {annex_page, annex_page + 1}
    assert first_network_row_page in {group_page, group_page + 1}
    assert pages_containing("PIPE_ROW_45") == pages_containing("Synthetic table note marker")

    for page in bbox_pages:
        body_words = [
            word["text"]
            for word in page["words"]
            if word["text"]
            and not word["text"].startswith("Bulletin")
            and word["text"] not in {"Chaps", "Vision", "/"}
            and not word["text"].isdigit()
        ]
        assert body_words, f"PDF page {page['index'] + 1} contains only headers or footers"


@pytest.mark.asyncio
async def test_real_typst_edition_preserves_copyable_technical_literals(
    tmp_path: Path,
    typst_binary: str,
    font_bundle_root: Path,
) -> None:
    document = _copyable_ioc_document()
    edition_document = _edition_document()
    edition_document = replace(
        edition_document,
        publications=tuple(
            replace(publication, document=document) for publication in edition_document.publications
        ),
    )
    bundle = load_template_bundle(_CHP_TYPST_ROOT, manifest_name=_EDITION_MANIFEST)
    source = EditionTypstRenderer().render(
        edition_document, bundle, document_label=_EDITION_RENDER_LABEL
    )
    render_data = json.loads(source.render_data_bytes)
    _install_narrow_technical_table(render_data)
    render_data_bytes = json.dumps(
        render_data, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    workspace_root = tmp_path / "copyable-edition-workspace"
    _build_workspace(
        workspace_root,
        bundle_files=bundle.files,
        render_data_bytes=render_data_bytes,
        media_refs=source.media_refs,
    )
    (workspace_root / "RENDERER" / "render-data.json").unlink()
    (workspace_root / source.render_data_relative_path).write_bytes(render_data_bytes)

    font_snapshot = load_font_bundle_snapshot(font_bundle_root, _FONT_BUNDLE_LOCK)
    font_root = tmp_path / "copyable-edition-fonts"
    font_root.mkdir()
    compiled = await TypstSubprocessCompiler(binary=typst_binary).compile(
        TypstCompileRequest(
            workspace_root=workspace_root,
            entrypoint_relative_path=source.entrypoint_relative_path,
            font_paths=materialize_font_bundle(font_snapshot, font_root),
        )
    )
    reader = PdfReader(BytesIO(compiled.content), strict=True)
    extracted = _assert_exact_copyable_pdf_text(reader, _COPYABLE_VALUES)
    # The fixture classifies this Windows path as a URL; C8 omits invalid URLs
    # instead of publishing them as network indicators.
    assert _COPYABLE_WINDOWS_PATH not in extracted.replace("\n", "")
    assert extracted.count("IOC") >= len(edition_document.publications)
    assert "Registre Windows" in extracted
    assert len(reader.pages) >= 1


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ("table", "long-table", "ioc"))
async def test_real_typst_edition_keeps_titles_with_following_content(
    target: str,
    tmp_path: Path,
    typst_binary: str,
    font_bundle_root: Path,
) -> None:
    edition_document = _edition_document()
    tables = (
        (
            _table_at(
                "sticky-title",
                "Vulnérabilités cataloguées par système"
                if target == "table"
                else "Long table pagination probe",
                EnrichmentPlacementKind.AFTER_LEAD,
            ),
        )
        if target in {"table", "long-table"}
        else ()
    )
    publication = replace(edition_document.publications[0], document=_full_document(tables=tables))
    edition_document = replace(edition_document, publications=(publication,))
    template_bundle = load_template_bundle(_CHP_TYPST_ROOT, manifest_name=_EDITION_MANIFEST)
    render_source = EditionTypstRenderer().render(
        edition_document, template_bundle, document_label=_EDITION_RENDER_LABEL
    )
    await _assert_real_typst_keep_with_next_case(
        tmp_path=tmp_path,
        typst_binary=typst_binary,
        font_bundle_root=font_bundle_root,
        template_files=template_bundle.files,
        source=render_source,
        target=target,
        edition=True,
    )
