"""Compile a multi-article EditionDocumentV2 with the real pinned Typst binary."""

from __future__ import annotations

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
from tests.test_typst_chp_parity import (
    _FONT_BUNDLE_LOCK,
    _build_workspace,
    _embedded_visual_xobject_count,
    _normalized_pdf_text,
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
            "country": "France",
            "country_code": "FR",
            "period_start": "2026-08-01",
            "period_end": "2026-08-31",
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
        "France",
        "2026-08-01",
        "2026-08-31",
        "Alpha first article",
        "Zulu second article",
        "Edition command table",
        "Edition diagram",
        "Edition figure",
        "Display ip",
        "Primary source",
    ):
        assert expected in text
    # L5: the uncertainty list is no longer published inside the article body.
    assert "Attribution remains uncertain" not in text
    assert text.count("RÉFÉRENCES") >= 1
    assert text.index("RÉFÉRENCES") < text.index("SYNTHÈSE")
    assert text.index("Alpha first article") < text.index("Zulu second article")
    assert "France — 2026-08-01 / 2026-08-31" in text
    page_count = len(reader.pages)
    assert f"{page_count} / {page_count}" in text
    assert "Brèves" not in text
    assert "N/A" not in text
    assert _embedded_visual_xobject_count(reader) >= len(source.media_refs)
