"""Compile the v33 article-format fixtures with the pinned Typst renderer."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import xml.etree.ElementTree as ET
from itertools import pairwise
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
    fixture = json.loads((_FIXTURE_ROOT / name).read_text(encoding="utf-8"))
    if not isinstance(fixture, dict):
        raise ValueError(f"Fixture {name} must contain a JSON object")
    return fixture


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
        "edition-render-data.json" if entrypoint.endswith("edition.typ") else "render-data.json"
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


def _extract_bbox_lines(path: Path) -> list[list[dict[str, Any]]]:
    result = subprocess.run(
        ("pdftotext", "-bbox-layout", str(path), "-"),
        capture_output=True,
        check=True,
        text=True,
        timeout=10,
    )
    root = ET.fromstring(result.stdout)
    namespace = {"xhtml": "http://www.w3.org/1999/xhtml"}
    pages = []
    for page in root.findall(".//xhtml:page", namespace):
        lines = []
        for line in page.findall(".//xhtml:line", namespace):
            text = " ".join(word.text or "" for word in line.findall("xhtml:word", namespace))
            if text:
                lines.append(
                    {
                        "text": text,
                        "left": float(line.attrib["xMin"]),
                        "top": float(line.attrib["yMin"]),
                        "right": float(line.attrib["xMax"]),
                        "bottom": float(line.attrib["yMax"]),
                    }
                )
        pages.append(lines)
    return pages


def _save_example(pdf_bytes: bytes, filename: str) -> None:
    output = os.environ.get("M18_L1_ARTIFACT_DIR")
    if output:
        artifact = Path(output) / filename
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_bytes(pdf_bytes)


def _assert_reader_figure_references_resolve(text: str) -> None:
    caption_matches = list(re.finditer(r"\bfigure\s+(\d+)\s*:", text, re.IGNORECASE))
    caption_numbers = [int(match.group(1)) for match in caption_matches]
    assert caption_numbers == list(range(1, len(caption_numbers) + 1))

    text_without_captions = re.sub(
        r"\bfigure\s+\d+\s*:", "", text, flags=re.IGNORECASE
    )
    referenced_numbers = {
        int(match.group(1))
        for match in re.finditer(r"\bfigure\s+(\d+)\b", text_without_captions, re.IGNORECASE)
    }
    missing_targets = referenced_numbers.difference(caption_numbers)
    assert not missing_targets, (
        f"figure references have no rendered caption: {sorted(missing_targets)}"
    )
    assert "tableau récapitulatif des ioc du bulletin" not in text.casefold()


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
    pdf_path = tmp_path / "article-rich.pdf"
    text, reader = _extract_text(pdf_bytes, pdf_path)
    lowered_text = text.casefold()
    compact_text = "".join(text.split())
    _assert_reader_figure_references_resolve(text)

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
        "9 septembre 2026 : Legacy reference text must not replace the editor/title fields.",
        "Figure 1 : Chaîne d’exécution résumée",  # noqa: RUF001
        "Figure 2 : Séquence réseau résumée",
        "Composants documentés",
        "Correspondance des étapes",
        "Dernier paragraphe de synthèse pour vérifier l’ordre éditorial.",  # noqa: RUF001
        "IOC",
        "Adresses IP :",
        "URL :",
        "Noms de domaine :",
        "Fichiers :",
        "192.0.2.1",
        "hxxps://updates[.]example[.]test/api/collect/01",
        "node-01[.]example[.]test",
        "C:\\ProgramData\\Agent\\component-01.dll",
    ):
        assert expected in text, expected
    assert (
        text.index("Références")
        < text.index("Chronologie")
        < text.index("Vue d’ensemble")  # noqa: RUF001
        < text.index("Synthèse")
        < text.index("Dernier paragraphe de synthèse")
        < text.index("IOC")
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
        if "Legacy reference text must not replace" in line
    ]
    assert len(reference_lines) == 1
    assert reference_lines[0].startswith("• 9 septembre 2026 : ")
    assert "https://example.test/report" not in reference_lines[0]
    assert "https://example.test/report" in compact_text
    assert "Threat Desk" not in text
    assert any(
        str(annotation.get_object().get("/A", {}).get("/URI", "")) == "https://example.test/report"
        for page in reader.pages
        for annotation in page.get("/Annots", ())
    )
    assert sum(len(values) for values in fixture["article_format"]["ioc_categories"].values()) == 90
    compact_text = "".join(text.split())
    for values in fixture["article_format"]["ioc_categories"].values():
        for item in values:
            assert "".join(item["value"]["text"].split()) in compact_text

    bbox_pages = _extract_bbox_lines(pdf_path)
    bbox_lines = [
        (page_index, line_index, line)
        for page_index, lines in enumerate(bbox_pages)
        for line_index, line in enumerate(lines)
    ]

    def find_bbox_lines(fragment: str) -> list[tuple[int, int, dict[str, Any]]]:
        matches = [item for item in bbox_lines if fragment.casefold() in item[2]["text"].casefold()]
        assert matches, fragment
        return matches

    def find_bbox_line(fragment: str) -> tuple[int, int, dict[str, Any]]:
        matches = find_bbox_lines(fragment)
        assert len(matches) == 1, (fragment, [item[2]["text"] for item in matches])
        return matches[0]

    def find_exact_bbox_line(value: str) -> tuple[int, int, dict[str, Any]]:
        expected = "".join(value.split()).casefold()
        matches = [
            item for item in bbox_lines if "".join(item[2]["text"].split()).casefold() == expected
        ]
        assert len(matches) == 1, (value, [item[2]["text"] for item in matches])
        return matches[0]

    event_positions = []
    for event in fixture["article_format"]["timeline_key_events"]:
        date_line = find_exact_bbox_line(event["display_date_fr"])
        text_line = find_bbox_line(event["text"])
        assert date_line[0] == text_line[0], event
        assert date_line[1] < text_line[1], event
        assert date_line[2]["bottom"] <= text_line[2]["top"], event
        event_positions.append((date_line, text_line))
    timeline_dates = [
        event["date_iso"] for event in fixture["article_format"]["timeline_key_events"]
    ]
    assert timeline_dates == sorted(timeline_dates)
    for previous, following in pairwise(event_positions):
        previous_date, previous_text = previous
        following_date, _following_text = following
        assert (previous_date[0], previous_date[1]) < (following_date[0], following_date[1])
        if previous_text[0] == following_date[0]:
            assert previous_text[2]["bottom"] <= following_date[2]["top"]

    assert "Documenté, non observé" not in text
    assert "Source :" not in text
    assert "Attribution :" not in text

    categories = fixture["article_format"]["ioc_categories"]
    category_specs = (
        ("Adresses IP :", categories["ips"]),
        ("URL :", categories["urls"]),
        ("Noms de domaine :", categories["domains"]),
        ("Fichiers :", categories["files"]),
    )
    previous_value_line = None
    for heading, values in category_specs:
        heading_lines = find_bbox_lines(heading)
        heading_line = heading_lines[0]
        value_lines = [find_exact_bbox_line(item["value"]["text"]) for item in values]
        value_line = value_lines[-1]
        assert heading_line[1] < value_lines[0][1]
        if len(values) > 18:
            assert len(heading_lines) == 1
            assert heading_line[0] == value_lines[0][0]
            assert len({line[0] for line in value_lines}) >= 2
        else:
            assert len(heading_lines) == 1
            assert heading_line[0] == value_line[0]
            assert all(line[0] == heading_line[0] for line in value_lines)
        for previous_item, following_item in pairwise(value_lines):
            if previous_item[0] == following_item[0]:
                value_gap = following_item[2]["top"] - previous_item[2]["bottom"]
                assert value_gap >= 2, (heading, value_gap)
        if previous_value_line is not None and previous_value_line[0] == heading_line[0]:
            vertical_gap = heading_line[2]["top"] - previous_value_line[2]["bottom"]
            assert vertical_gap >= 10, (heading, vertical_gap)
        previous_value_line = value_line

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
    assert "Rapport conservé" in text
    assert "Legacy Lab" not in text
    assert "2026-08-02" not in text
    legacy_reference_line = next(line for line in text.splitlines() if "Rapport conservé" in line)
    assert "https://legacy.example.test/report" not in legacy_reference_line
    assert "https://legacy.example.test/report" in "".join(text.split())
    assert "IOC" in text and "IPv4" in text and "203.0.113.19" in text
    assert "Chronologie" not in text
    for forbidden in (*_BANNED_TEXT, "LEGACY CHRONOLOGY", "contextual indicator"):
        assert forbidden.casefold() not in lowered_text, forbidden


@pytest.mark.asyncio
async def test_article_with_one_projected_key_event_renders_it_without_threshold_logic(
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
    assert "Chronologie" in text
    assert "9 septembre 2026" in text
    assert "LEGACY EMPTY TEST" not in text
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
            "tlp": "TLP:CLEAR",
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
                "content_sections": article["content_sections"],
            },
            {
                "position": 2,
                "kind": "brief",
                "number": "02",
                "anchor": "brief-02",
                "title": brief["title"],
                "article_format": None,
                "content_sections": brief["content_sections"],
            },
        ],
    }
    pdf_bytes = await _compile(
        tmp_path=tmp_path,
        typst_binary=typst_binary,
        font_path=font_path,
        template_bundle=edition_template_bundle,
        entrypoint="RENDERER/edition.typ",
        render_data=edition,
    )
    pdf_path = tmp_path / "edition-article-brief.pdf"
    text, _reader = _extract_text(pdf_bytes, pdf_path)
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
    article_title = "[APT Exemple] Chaîne d’exploitation ciblée"  # noqa: RUF001
    article_page = next(
        page
        for page in _extract_bbox_lines(pdf_path)
        if any(article_title in line["text"] for line in page)
        and any(line["text"] == "Chronologie" for line in page)
    )
    title_line = next(line for line in article_page if article_title in line["text"])
    badge_line = next(line for line in article_page if line["text"] == "ARTICLE")
    number_line = next(line for line in article_page if line["text"] == "01")
    chronology_line = next(line for line in article_page if line["text"] == "Chronologie")
    assert title_line["bottom"] < min(badge_line["top"], number_line["top"])
    assert number_line["left"] > badge_line["right"]
    assert badge_line["top"] < number_line["bottom"]
    assert number_line["top"] < badge_line["bottom"]
    assert max(badge_line["bottom"], number_line["bottom"]) < chronology_line["top"]
    _save_example(pdf_bytes, "edition-article-brief.pdf")
