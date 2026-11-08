"""Compile the v33 article-format fixtures with the pinned Typst renderer."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
from pypdf import PdfReader

from cti_app.application.typst_compilation import TypstCompileRequest
from cti_app.application.typst_rendering import load_template_bundle
from cti_app.infrastructure.typst_compiler import TypstSubprocessCompiler

_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
_TEMPLATE_ROOT = _REPOSITORY_ROOT / "chpTypst"
_FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "typst" / "article_format"
_FONT_REFERENCE_ROOT = Path("/home/nill/perso/work/AutoWork/docs/references/chpTypst")
_BANNED_TEXT = (
    "PROVENANCE DE PUBLICATION",
    "Indicateur :",
    "Relation :",
    "contextual indicator",
    "Candidat non confirmé",
    "Pistes non confirmées",
    "IOC de l'article",
    "ANNEXE TECHNIQUE",
    "STATUT INTERNE À MASQUER",
)


@pytest.fixture(scope="session")
def typst_binary() -> str:
    binary = os.environ.get("TYPST_BINARY") or shutil.which("typst")
    if binary is None:
        pytest.fail("real Typst is required; set TYPST_BINARY to the pinned 0.15.1 binary")
    version = subprocess.run(
        (binary, "--version"), capture_output=True, check=True, text=True, timeout=10
    ).stdout.strip()
    assert version.startswith("typst 0.15.1"), version
    return binary


@pytest.fixture(scope="session")
def font_path() -> Path:
    configured_root = os.environ.get("FONT_BUNDLE_ROOT")
    root = Path(configured_root) if configured_root else _TEMPLATE_ROOT
    candidates = (root / "HankenGrotesk", _FONT_REFERENCE_ROOT / "HankenGrotesk", root)
    for candidate in candidates:
        if (candidate / "HankenGrotesk-Regular.ttf").is_file():
            return candidate
    pytest.fail(f"Hanken Grotesk font files are unavailable under {root}")


@pytest.fixture(scope="session")
def template_bundle():
    return load_template_bundle(_TEMPLATE_ROOT)


@pytest.fixture(scope="session")
def edition_template_bundle():
    return load_template_bundle(
        _TEMPLATE_ROOT,
        manifest_name="edition-renderer-manifest.json",
    )


def _read_fixture(name: str) -> dict[str, Any]:
    return json.loads((_FIXTURE_ROOT / name).read_text(encoding="utf-8"))


def _build_workspace(
    workspace: Path,
    *,
    template_bundle,
    entrypoint: str,
    render_data: dict[str, Any],
) -> None:
    for template_file in template_bundle.files:
        destination = workspace / template_file.relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(template_file.content)

    renderer_root = workspace / "RENDERER"
    renderer_root.mkdir(parents=True, exist_ok=True)
    data_name = (
        "edition-render-data.json"
        if entrypoint.endswith("edition.typ")
        else "render-data.json"
    )
    (renderer_root / data_name).write_text(
        json.dumps(render_data, ensure_ascii=False), encoding="utf-8"
    )
    media_root = renderer_root / "media"
    media_root.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(_FIXTURE_ROOT / "attack-chain.svg", media_root / "attack-chain.svg")


async def _compile(
    *,
    tmp_path: Path,
    typst_binary: str,
    font_path: Path,
    template_bundle,
    entrypoint: str,
    render_data: dict[str, Any],
) -> bytes:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _build_workspace(
        workspace,
        template_bundle=template_bundle,
        entrypoint=entrypoint,
        render_data=render_data,
    )
    result = await TypstSubprocessCompiler(binary=typst_binary).compile(
        TypstCompileRequest(
            workspace_root=workspace,
            entrypoint_relative_path=entrypoint,
            font_paths=(font_path,),
        )
    )
    assert result.media_type == "application/pdf"
    assert result.compiler_version == "0.15.1"
    return result.content


def _extract_text(pdf_bytes: bytes, path: Path) -> tuple[str, PdfReader]:
    path.write_bytes(pdf_bytes)
    result = subprocess.run(
        ("pdftotext", "-layout", str(path), "-"),
        capture_output=True,
        check=True,
        text=True,
        timeout=10,
    )
    return result.stdout, PdfReader(path, strict=True)


def _save_example(pdf_bytes: bytes, filename: str) -> None:
    output = os.environ.get("M18_L1_ARTIFACT_DIR")
    if output:
        artifact = Path(output) / filename
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_bytes(pdf_bytes)


@pytest.mark.asyncio
async def test_article_v33_renders_overview_timeline_references_figure_table_and_iocs(
    tmp_path: Path,
    typst_binary: str,
    font_path: Path,
    template_bundle,
) -> None:
    fixture = _read_fixture("article-rich.json")
    pdf_bytes = await _compile(
        tmp_path=tmp_path,
        typst_binary=typst_binary,
        font_path=font_path,
        template_bundle=template_bundle,
        entrypoint="RENDERER/publication.typ",
        render_data=fixture,
    )
    text, reader = _extract_text(pdf_bytes, tmp_path / "article-rich.pdf")
    lowered_text = text.casefold()
    compact_text = "".join(text.split())

    for expected in (
        "ARTICLE",
        "[APT Exemple] Chaîne d’exploitation ciblée",  # noqa: RUF001
        "Vue d’ensemble",  # noqa: RUF001
        "Victimologie",
        "Arsenal",
        "Objectif principal",
        "Chronologie",
        "9 septembre 2026",
        "21 septembre 2026",
        "Références",
        "Example Research",
        "Analyse de la chaîne observée",
        "2026-09-09",
        "Figure 1 : Chaîne d’exécution résumée",  # noqa: RUF001
        "Composants documentés",
        "IOC",
        "Adresses IP :",
        "URL :",
        "Noms de domaine :",
        "Fichiers :",
        "192.0.2.77",
        "hxxps://c2[.]example[.]test/api",
        "relay[.]example[.]test",
        "C:\\ProgramData\\agent.dll",
    ):
        assert expected in text, expected
    assert (
        text.index("Références")
        < text.index("Chronologie")
        < text.index("Vue d’ensemble")  # noqa: RUF001
        < text.index("Synthèse")
    )
    for forbidden in (
        *_BANNED_TEXT,
        "LEGACY REFERENCE TIMELINE",
        "LEGACY CHRONOLOGY",
        "MUST NOT RENDER",
        "198.51.100.66",
        "hxxps://candidate[.]invalid",
    ):
        assert forbidden.casefold() not in lowered_text, forbidden

    reference_lines = [
        line.strip()
        for line in text.splitlines()
        if line.strip().startswith("• Example Research")
    ]
    assert len(reference_lines) == 1
    assert reference_lines[0].startswith("•")
    assert "https://example.test/report" in compact_text
    assert "Threat Desk" in text
    assert "https://threat.example.test/analysis" in compact_text
    assert any(
        str(annotation.get_object().get("/A", {}).get("/URI", ""))
        == "https://example.test/report"
        for page in reader.pages
        for annotation in page.get("/Annots", ())
    )
    _save_example(pdf_bytes, "article-rich.pdf")


@pytest.mark.asyncio
async def test_legacy_article_fallback_suppresses_legacy_chronology_and_statuses(
    tmp_path: Path,
    typst_binary: str,
    font_path: Path,
    template_bundle,
) -> None:
    fixture = _read_fixture("article-legacy.json")
    assert "article_format" not in fixture
    pdf_bytes = await _compile(
        tmp_path=tmp_path,
        typst_binary=typst_binary,
        font_path=font_path,
        template_bundle=template_bundle,
        entrypoint="RENDERER/publication.typ",
        render_data=fixture,
    )
    text, _reader = _extract_text(pdf_bytes, tmp_path / "article-legacy.pdf")
    lowered_text = text.casefold()
    assert "Références" in text
    assert "•" in text and "Legacy Lab" in text
    assert "IOC" in text and "IPv4" in text and "203.0.113.19" in text
    assert "Chronologie" not in text
    for forbidden in (*_BANNED_TEXT, "LEGACY CHRONOLOGY", "contextual indicator"):
        assert forbidden.casefold() not in lowered_text, forbidden


@pytest.mark.asyncio
async def test_article_with_fewer_than_two_key_events_has_no_empty_timeline_or_ioc(
    tmp_path: Path,
    typst_binary: str,
    font_path: Path,
    template_bundle,
) -> None:
    fixture = _read_fixture("article-one-event.json")
    pdf_bytes = await _compile(
        tmp_path=tmp_path,
        typst_binary=typst_binary,
        font_path=font_path,
        template_bundle=template_bundle,
        entrypoint="RENDERER/publication.typ",
        render_data=fixture,
    )
    text, _reader = _extract_text(pdf_bytes, tmp_path / "article-one-event.pdf")
    assert "Chronologie" not in text
    assert "9 septembre 2026" not in text
    assert "IOC" not in text
    assert "Une brève séquence documentée." in text


@pytest.mark.asyncio
async def test_brief_keeps_legacy_timeline_and_uses_marker_free_ioc_fallback(
    tmp_path: Path,
    typst_binary: str,
    font_path: Path,
    template_bundle,
) -> None:
    fixture = _read_fixture("brief.json")
    assert fixture["article_format"] is None
    pdf_bytes = await _compile(
        tmp_path=tmp_path,
        typst_binary=typst_binary,
        font_path=font_path,
        template_bundle=template_bundle,
        entrypoint="RENDERER/publication.typ",
        render_data=fixture,
    )
    text, _reader = _extract_text(pdf_bytes, tmp_path / "brief.pdf")
    lowered_text = text.casefold()
    assert "Chronologie" in text
    assert "8 septembre 2026" in text
    assert "IOC" in text and "Domaines" in text and "notice[.]example[.]test" in text
    assert "Pistes non confirmées" not in text
    for forbidden in _BANNED_TEXT:
        assert forbidden.casefold() not in lowered_text, forbidden
    _save_example(pdf_bytes, "brief.pdf")


@pytest.mark.asyncio
async def test_edition_reuses_the_article_format_helpers_for_article_and_brief(
    tmp_path: Path,
    typst_binary: str,
    font_path: Path,
    edition_template_bundle,
) -> None:
    article = _read_fixture("article-rich.json")
    brief = _read_fixture("brief.json")
    edition = {
        "schema_version": "typst-edition-model-v24-test",
        "document_label": "edition-fixture",
        "document_header": "Bulletin n°3 / Septembre 2026",
        "edition": {
            "bulletin_country": "APT-CN2609",
            "bulletin_period": "Septembre 2026",
            "bulletin_title": "Bulletin de veille CTI",
            "country": "Chine",
            "country_code": "CN",
            "bulletin_number": "3",
            "bulletin_identifier": "APT-CN2609",
            "tlp": "TLP:CLEAR"
        },
        "file_ioc_count": 0,
        "publications": [
            {
                "position": 1,
                "kind": "article",
                "number": "01",
                "anchor": "article-01",
                "title": article["title"],
                "article_format": article["article_format"],
                "content_sections": article["content_sections"]
            },
            {
                "position": 2,
                "kind": "brief",
                "number": "02",
                "anchor": "brief-02",
                "title": brief["title"],
                "article_format": None,
                "content_sections": brief["content_sections"]
            }
        ]
    }
    pdf_bytes = await _compile(
        tmp_path=tmp_path,
        typst_binary=typst_binary,
        font_path=font_path,
        template_bundle=edition_template_bundle,
        entrypoint="RENDERER/edition.typ",
        render_data=edition,
    )
    text, _reader = _extract_text(pdf_bytes, tmp_path / "edition-article-brief.pdf")
    lowered_text = text.casefold()
    for expected in (
        "Bulletin n°3 / Septembre 2026",
        "Bulletin-APT-CN2609",
        "Actualité des codes et infrastructures Chine",
        "01",
        "02",
        "Vue d’ensemble",  # noqa: RUF001
        "Chronologie",
        "IOC",
        "Adresses IP :",
        "Une brève mise à jour",
        "notice[.]example[.]test",
    ):
        assert expected in text, expected
    for forbidden in _BANNED_TEXT:
        assert forbidden.casefold() not in lowered_text, forbidden
    _save_example(pdf_bytes, "edition-article-brief.pdf")
