"""Compile a multi-article EditionDocumentV2 with the real pinned Typst binary."""

from __future__ import annotations

import json
from dataclasses import replace
from io import BytesIO
from pathlib import Path
from uuid import UUID

import pytest
from pypdf import PdfReader

from cti_app.application.edition_typst_rendering import EditionTypstRenderer
from cti_app.application.typst_compilation import (
    TypstCompileRequest,
    load_font_bundle_snapshot,
    materialize_font_bundle,
)
from cti_app.application.typst_rendering import load_template_bundle
from cti_app.domain.edition_publication import EditionDocumentV2, EditionPublicationV2
from cti_app.domain.production_editorial_enrichment import EnrichmentPlacementKind
from cti_app.infrastructure.typst_compiler import TypstSubprocessCompiler
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
    _assert_exact_copyable_pdf_text,
    _copyable_ioc_document,
    _install_narrow_technical_table,
)
from tests.test_typst_rendering import _diagram_at, _figure_at, _full_document, _table_at

pytest_plugins = ("tests.test_typst_compiler_runtime",)

_CHP_TYPST_ROOT = Path(__file__).resolve().parents[2] / "chpTypst"
_EDITION_MANIFEST = "edition-renderer-manifest.json"


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
    source = EditionTypstRenderer().render(_edition_document(), bundle)
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
    # The reader sees a source note, never the internal provenance string.
    assert "Figure 1 from the source publication" not in text
    assert "Provenance :" not in text
    # L5: the uncertainty list is no longer published inside the article body.
    assert "Attribution remains uncertain" not in text
    assert text.count("RÉFÉRENCES") >= 1
    assert text.index("RÉFÉRENCES") < text.index("SYNTHÈSE")
    assert text.index("Alpha first article") < text.index("Zulu second article")
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
    source = EditionTypstRenderer().render(edition_document, bundle)
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
    source = EditionTypstRenderer().render(edition_document, bundle)
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
    render_source = EditionTypstRenderer().render(edition_document, template_bundle)
    await _assert_real_typst_keep_with_next_case(
        tmp_path=tmp_path,
        typst_binary=typst_binary,
        font_bundle_root=font_bundle_root,
        template_files=template_bundle.files,
        source=render_source,
        target=target,
        edition=True,
    )
