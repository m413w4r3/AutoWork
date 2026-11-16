"""Real-PDF regression oracles for the M19 ARTICLE and BRIEF Typst format."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import xml.etree.ElementTree as ET
from itertools import pairwise
from pathlib import Path
from statistics import median as statistics_median
from typing import Any

import pytest
from pypdf import PdfReader

from cti_app.application.typst_compilation import TypstCompileRequest
from cti_app.application.typst_rendering import load_template_bundle
from cti_app.infrastructure.typst_compiler import TypstSubprocessCompiler

_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
_TEMPLATE_ROOT = _REPOSITORY_ROOT / "chpTypst"
_FONT_BUNDLE_ROOT = Path(os.environ.get("FONT_BUNDLE_ROOT", str(_TEMPLATE_ROOT)))
_FONT_PATHS = (
    _FONT_BUNDLE_ROOT / "HankenGrotesk",
    _FONT_BUNDLE_ROOT / "CascadiaCode" / "ttf" / "static",
)
_MARGIN = "Synthetic paragraph text documents a fictional formatting example. "
_LONG_SENTENCE = (
    "The layout keeps this generic observation readable across a full line of prose, "
    "then continues with another neutral sentence so the rendered paragraph occupies "
    "at least two lines in the page text area. "
)
_REFERENCE_URL = "https://source.example.test/report"
_TIMELINE_URL = "https://source.example.test/event"
_SVG = """<svg xmlns="http://www.w3.org/2000/svg" width="180" height="90" viewBox="0 0 180 90">
<rect x="2" y="2" width="176" height="86" rx="6" fill="#e8eef3" stroke="#243b53"/>
<circle cx="45" cy="45" r="12" fill="#243b53"/>
<path d="M60 45h55" stroke="#243b53" stroke-width="3"/>
</svg>"""
_XML = {"xhtml": "http://www.w3.org/1999/xhtml"}


def _paragraphs(count: int) -> list[dict[str, Any]]:
    paragraphs: list[dict[str, Any]] = []
    for index in range(count):
        filler = _LONG_SENTENCE * 4
        if index == 0:
            text = f"SentinelTool uses --safe mode. {filler}"
            spans = [
                {"style": "semantic-tool", "text": "SentinelTool"},
                {"style": "semantic-plain", "text": " uses "},
                {"style": "semantic-technical-literal", "text": "--safe"},
                {"style": "semantic-plain", "text": f" mode. {filler}"},
            ]
        else:
            text = f"{_MARGIN}{filler}"
            spans = None
        block: dict[str, Any] = {"type": "paragraph", "text": text}
        if spans is not None:
            block["semantic_spans"] = spans
        paragraphs.append(block)
    return paragraphs


def _timeline_events(kind: str) -> list[dict[str, Any]]:
    events = (
        (
            "14 octobre 2026",
            "Synthetic first event is documented.",
            _TIMELINE_URL,
        ),
        ("16 octobre 2026", "Synthetic second event is documented.", None),
    )
    if kind == "article":
        return [
            {
                "date_iso": "2026-10-14" if index == 0 else "2026-10-16",
                "display_date_fr": display_date,
                "text": event_text,
                **({"source_urls": [url]} if url else {}),
            }
            for index, (display_date, event_text, url) in enumerate(events)
        ]
    return [
        {
            "display_date": display_date,
            "text": event_text,
            **({"source_urls": [url]} if url else {}),
        }
        for display_date, event_text, url in events
    ]


def _publication_fixture(
    kind: str,
    *,
    paragraph_count: int = 4,
    include_figures: bool = True,
    include_references: bool = True,
    events: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    reference_section: dict[str, Any] = {
        "type": "references",
        "show_heading": True,
        "entries": [
            {
                "display_date": "14 octobre 2026",
                "text": "Synthetic source summary sentence.",
                "title": "This title must not override the source summary.",
                "publisher": "Synthetic Publisher",
                "url": _REFERENCE_URL,
            },
            {
                "date": "2026-10-15",
                "title": "Fallback source title without an invented date.",
            },
        ],
        "sources": [],
        "blocks": [],
    }
    content_sections: list[dict[str, Any]] = []
    if include_references:
        content_sections.append(reference_section)
    projected_events = _timeline_events(kind) if events is None else events
    if kind == "brief":
        content_sections.append({"type": "chronology", "timeline": projected_events})
    body_blocks = _paragraphs(paragraph_count)
    if include_figures:
        body_blocks.extend(
            (
                {
                    "type": "figure",
                    "figure_number": 1,
                    "caption": "Archived figure caption",
                    "media_path": "media/m19-fixture.svg",
                    "classification": "Documenté, non observé — internal fixture data.",
                    "source_note": "Internal figure source note.",
                    "attribution": "Internal figure attribution.",
                },
                {
                    "type": "diagram",
                    "figure_number": 2,
                    "caption": "Diagram figure caption",
                    "media_path": "media/m19-fixture.svg",
                    "classification": "Documenté, non observé — diagram fixture data.",
                    "source_note": "Internal diagram source note.",
                    "attribution": "Internal diagram attribution.",
                },
            )
        )
    content_sections.append({"type": "synthesis", "blocks": body_blocks})

    result: dict[str, Any] = {
        "schema_version": "typst-publication-model-v40-m21-opinion-render",
        "document_label": f"m19-{kind}-format-fixture",
        "kind": kind,
        "title": f"Synthetic {kind} publication",
        "number": "01",
        "content_sections": content_sections,
        "article_format": None,
    }
    if kind == "article":
        result["article_format"] = {
            "overview": {
                "victimology": "A fictional audience.",
                "arsenal": "Neutral example components.",
                "main_objective": "Exercise the PDF layout.",
            },
            "timeline_key_events": projected_events,
            "ioc_categories": {"ips": [], "urls": [], "domains": [], "files": []},
        }
    return result


def _opinion_fixture() -> dict[str, Any]:
    source_url = "https://source.example.test/advisory"
    return {
        "schema_version": "typst-publication-model-v40-m21-opinion-render",
        "document_label": "m21-opinion-format-fixture",
        "kind": "article",
        "opinion_document": True,
        "title": "Synthetic analyst opinion",
        "number": "01",
        "article_format": None,
        "article_overview": {
            "victimology": {"text": "A fictional audience."},
            "arsenal": {"text": "Neutral example components."},
            "main_objective": {"text": "Exercise the opinion overview card."},
        },
        "content_sections": [
            {
                "type": "references",
                "show_heading": True,
                "entries": [
                    {
                        "number": 1,
                        "display_date": "14 octobre 2026",
                        "text": "Synthetic advisory source detail.",
                        "source_urls": [source_url],
                        "target_id": "publication-source-0001",
                    }
                ],
                "blocks": [],
            },
            {
                "type": "chronology",
                "timeline": [
                    {
                        "display_date": "14 octobre 2026",
                        "text": "Synthetic first advisory event.",
                        "source_refs": [{"number": 1, "target_id": "publication-source-0001"}],
                    },
                    {
                        "display_date": "16 octobre 2026",
                        "text": "Synthetic second event is documented.",
                    },
                ],
            },
            {
                "type": "synthesis",
                "blocks": [
                    {"type": "section_heading", "text": "Contexte"},
                    {"type": "paragraph", "text": "Synthetic context paragraph one."},
                    {"type": "paragraph", "text": "Synthetic context paragraph two."},
                ],
            },
            {
                "type": "analyst_opinion",
                "blocks": [
                    {
                        "number": "1.",
                        "heading": "Conclusion",
                        "qualification_label": "Étayé par les sources",
                        "text": "Synthetic opinion conclusion.",
                        "pipe_table": None,
                    }
                ],
                "unanchored_observations": [],
                "unresolved_points": [],
                "protocol_figures": [],
            },
        ],
    }


def test_m19_renderer_manifest_versions_are_explicitly_pinned() -> None:
    article = load_template_bundle(_TEMPLATE_ROOT)
    edition = load_template_bundle(
        _TEMPLATE_ROOT,
        manifest_name="edition-renderer-manifest.json",
    )
    assert article.template_version == (
        "chp-article-v47-m21-opinion-safe-links-m21-pdffix7-m21-format2"
    )
    assert edition.template_version == (
        "chp-edition-v36-m21-opinion-render-m21-pdffix7-m21-format2"
    )


def test_semantic_inline_technical_highlight_has_no_content_padding() -> None:
    helpers = (_TEMPLATE_ROOT / "UTILS" / "helpers.typ").read_text(encoding="utf-8")
    definition = helpers.split("#let semantic-inline-technical(body) = ", 1)[1].split(
        "\n#let semantic-technical-code-block", 1
    )[0]

    assert "highlight(fill: luma(190), radius: 2pt)[#text(" in definition
    assert re.search(r"hyphenate: false\)\[#body\]\]\s*$", definition)


def _typst_binary() -> str:
    binary = os.environ.get("TYPST_BINARY") or shutil.which("typst")
    if binary is None or not Path(binary).is_file():
        pytest.fail("real Typst is required; set TYPST_BINARY")
    return binary


def _build_workspace(
    workspace: Path,
    *,
    render_data: dict[str, Any],
    entrypoint: str,
    manifest_name: str,
    media_paths: tuple[str, ...] = (),
) -> None:
    bundle = load_template_bundle(_TEMPLATE_ROOT, manifest_name=manifest_name)
    for template_file in bundle.files:
        destination = workspace / template_file.relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(template_file.content)
    renderer_root = workspace / "RENDERER"
    renderer_root.mkdir(parents=True, exist_ok=True)
    data_name = (
        "edition-render-data.json" if entrypoint.endswith("edition.typ") else "render-data.json"
    )
    (renderer_root / data_name).write_text(
        json.dumps(render_data, ensure_ascii=False),
        encoding="utf-8",
    )
    media_root = renderer_root / "media"
    media_root.mkdir(parents=True, exist_ok=True)
    (media_root / "m19-fixture.svg").write_text(_SVG, encoding="utf-8")
    for media_path in media_paths:
        destination = renderer_root / media_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(_SVG, encoding="utf-8")


async def _compile_pdf(
    tmp_path: Path,
    render_data: dict[str, Any],
    *,
    entrypoint: str = "RENDERER/publication.typ",
    manifest_name: str = "renderer-manifest.json",
    media_paths: tuple[str, ...] = (),
) -> Path:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _build_workspace(
        workspace,
        render_data=render_data,
        entrypoint=entrypoint,
        manifest_name=manifest_name,
        media_paths=media_paths,
    )
    result = await TypstSubprocessCompiler(binary=_typst_binary()).compile(
        TypstCompileRequest(
            workspace_root=workspace,
            entrypoint_relative_path=entrypoint,
            font_paths=_FONT_PATHS,
        )
    )
    assert result.media_type == "application/pdf"
    path = tmp_path / "rendered.pdf"
    path.write_bytes(result.content)
    return path


def _extract_layout(path: Path) -> tuple[str, list[dict[str, Any]]]:
    text_result = subprocess.run(
        ("pdftotext", "-layout", str(path), "-"),
        capture_output=True,
        check=True,
        text=True,
        timeout=20,
    )
    result = subprocess.run(
        ("pdftotext", "-bbox-layout", str(path), "-"),
        capture_output=True,
        check=True,
        text=True,
        timeout=20,
    )
    root = ET.fromstring(result.stdout)
    pages: list[dict[str, Any]] = []
    for page_element in root.findall(".//xhtml:page", _XML):
        lines: list[dict[str, Any]] = []
        blocks: list[dict[str, Any]] = []
        for block_element in page_element.findall(".//xhtml:block", _XML):
            block_lines: list[dict[str, Any]] = []
            for line_element in block_element.findall("xhtml:line", _XML):
                words = line_element.findall("xhtml:word", _XML)
                line_text = " ".join(word.text or "" for word in words)
                if not line_text:
                    continue
                line = {
                    "text": line_text,
                    "left": float(line_element.attrib["xMin"]),
                    "top": float(line_element.attrib["yMin"]),
                    "right": float(line_element.attrib["xMax"]),
                    "bottom": float(line_element.attrib["yMax"]),
                }
                lines.append(line)
                block_lines.append(line)
            if block_lines:
                blocks.append(
                    {
                        "left": min(line["left"] for line in block_lines),
                        "top": min(line["top"] for line in block_lines),
                        "right": max(line["right"] for line in block_lines),
                        "bottom": max(line["bottom"] for line in block_lines),
                        "line_count": len(block_lines),
                    }
                )
        pages.append(
            {
                "width": float(page_element.attrib["width"]),
                "height": float(page_element.attrib["height"]),
                "lines": lines,
                "blocks": sorted(blocks, key=lambda block: block["top"]),
            }
        )
    return text_result.stdout, pages


def _paragraph_gap_measurements(pages: list[dict[str, Any]]) -> list[float]:
    gaps: list[float] = []
    for page in pages:
        blocks = page["blocks"]
        for previous, following in pairwise(blocks):
            if previous["line_count"] < 2 or following["line_count"] < 2:
                continue
            if previous["top"] < 100 or following["top"] > 730:
                continue
            previous_width = previous["right"] - previous["left"]
            following_width = following["right"] - following["left"]
            if previous_width < 250 or following_width < 250:
                continue
            overlap = max(
                0.0,
                min(previous["right"], following["right"])
                - max(previous["left"], following["left"]),
            )
            if overlap / min(previous_width, following_width) < 0.75:
                continue
            gap = following["top"] - previous["bottom"]
            if 8 <= gap <= 30:
                gaps.append(gap)
    return gaps


def _read_ppm(path: Path) -> tuple[int, int, bytes]:
    data = path.read_bytes()
    index = 0
    tokens: list[bytes] = []
    while len(tokens) < 4:
        while data[index : index + 1] in (b" ", b"\t", b"\r", b"\n"):
            index += 1
        if data[index : index + 1] == b"#":
            while data[index : index + 1] not in (b"", b"\n"):
                index += 1
            continue
        start = index
        while data[index : index + 1] and data[index : index + 1] not in (
            b" ",
            b"\t",
            b"\r",
            b"\n",
        ):
            index += 1
        tokens.append(data[start:index])
    if data[index : index + 2] == b"\r\n":
        index += 2
    else:
        index += 1
    assert tokens[0] == b"P6" and tokens[3] == b"255"
    return int(tokens[1]), int(tokens[2]), data[index:]


def _assert_timeline_rail(
    path: Path,
    pages: list[dict[str, Any]],
    tmp_path: Path,
) -> None:
    page_index = next(
        index
        for index, page in enumerate(pages)
        if any(line["text"] == "Chronologie" for line in page["lines"])
    )
    page = pages[page_index]
    heading = next(line for line in page["lines"] if line["text"] == "Chronologie")
    last_event = next(
        line for line in page["lines"] if line["text"] == "Synthetic second event is documented."
    )
    image_prefix = tmp_path / "timeline-page"
    subprocess.run(
        (
            "pdftoppm",
            "-r",
            "72",
            "-f",
            str(page_index + 1),
            "-l",
            str(page_index + 1),
            "-singlefile",
            str(path),
            str(image_prefix),
        ),
        capture_output=True,
        check=True,
        timeout=20,
    )
    width, height, pixels = _read_ppm(image_prefix.with_suffix(".ppm"))
    y_start = max(0, int(heading["top"]))
    y_end = min(height, int(last_event["bottom"]) + 1)
    accent = bytes((36, 59, 83))
    longest_run = 0
    for x in range(int(width * 0.04), int(width * 0.2)):
        current_run = 0
        for y in range(y_start, y_end):
            offset = (y * width + x) * 3
            if pixels[offset : offset + 3] == accent:
                current_run += 1
                longest_run = max(longest_run, current_run)
            else:
                current_run = 0
    assert longest_run >= 20, longest_run


def _assert_overview_box(path: Path, pages: list[dict[str, Any]], tmp_path: Path) -> None:
    page_index = next(
        index
        for index, page in enumerate(pages)
        if any(line["text"].replace("\u2019", "'") == "Vue d'ensemble" for line in page["lines"])
    )
    heading = next(
        line
        for line in pages[page_index]["lines"]
        if line["text"].replace("\u2019", "'") == "Vue d'ensemble"
    )
    image_prefix = tmp_path / "overview-page"
    subprocess.run(
        (
            "pdftoppm",
            "-r",
            "72",
            "-f",
            str(page_index + 1),
            "-l",
            str(page_index + 1),
            "-singlefile",
            str(path),
            str(image_prefix),
        ),
        capture_output=True,
        check=True,
        timeout=20,
    )
    width, height, pixels = _read_ppm(image_prefix.with_suffix(".ppm"))
    x = int(heading["left"]) - 5
    y = int((heading["top"] + heading["bottom"]) / 2)
    assert 0 <= x < width and 0 <= y < height
    offset = (y * width + x) * 3
    assert pixels[offset : offset + 3] == bytes((232, 240, 240))


def _assert_reference_date_is_purple(
    path: Path,
    line: dict[str, Any],
    page_index: int,
    tmp_path: Path,
) -> None:
    image_prefix = tmp_path / "reference-page"
    subprocess.run(
        (
            "pdftoppm",
            "-r",
            "72",
            "-f",
            str(page_index + 1),
            "-l",
            str(page_index + 1),
            "-singlefile",
            str(path),
            str(image_prefix),
        ),
        capture_output=True,
        check=True,
        timeout=20,
    )
    width, height, pixels = _read_ppm(image_prefix.with_suffix(".ppm"))
    x_start = max(0, int(line["left"]))
    x_end = min(width, int(line["left"]) + 140)
    y_start = max(0, int(line["top"]))
    y_end = min(height, int(line["bottom"]) + 1)
    purple_pixels = 0
    for y in range(y_start, y_end):
        for x in range(x_start, x_end):
            offset = (y * width + x) * 3
            red, green, blue = pixels[offset : offset + 3]
            if green - red >= 10 and blue - red >= 10:
                purple_pixels += 1
    assert purple_pixels > 0, "reference date was not rendered in the configured purple color"


def _pdf_font_list(path: Path) -> str:
    result = subprocess.run(
        ("pdffonts", str(path)),
        capture_output=True,
        check=True,
        text=True,
        timeout=20,
    )
    return result.stdout.casefold()


@pytest.mark.parametrize("kind", ("article", "brief"))
@pytest.mark.asyncio
async def test_m19_article_and_brief_render_v2_format_to_real_pdf(
    tmp_path: Path,
    kind: str,
) -> None:
    render_data = _publication_fixture(kind)
    if kind == "brief":
        render_data["article_overview"] = {
            "victimology": {"text": "A fictional brief audience."},
            "arsenal": {"text": "Neutral brief components."},
            "main_objective": {"text": "Exercise the brief overview card."},
        }
    path = await _compile_pdf(tmp_path, render_data)
    text, pages = _extract_layout(path)

    assert text.index("Références") < text.index("Chronologie") < text.index("Synthèse")
    assert text.index("Chronologie") < text.index("Vue d\u2019ensemble") < text.index("Synthèse")
    assert all(label in text for label in ("Victimologie", "Arsenal", "Objectif principal"))
    reference_line = next(
        line["text"]
        for page in pages
        for line in page["lines"]
        if "Synthetic source summary sentence." in line["text"]
    )
    assert reference_line.startswith("• 14 octobre 2026 : Synthetic source summary sentence.")
    assert _REFERENCE_URL not in reference_line
    assert "This title must not override the source summary." not in text
    assert "Synthetic Publisher" not in text
    assert "Fallback source title without an invented date." in text
    assert "2026-10-15" not in text
    assert _REFERENCE_URL in text and _TIMELINE_URL in text
    assert "Synthetic first event is documented." in text
    assert "Synthetic second event is documented." in text
    assert "SentinelTool" in text and "--safe" in text
    for absent in (
        "Documenté, non observé",
        "Internal figure source note",
        "Internal figure attribution",
        "Internal diagram source note",
        "Internal diagram attribution",
    ):
        assert absent not in text

    captions = (
        "Figure 1 : Archived figure caption",
        "Figure 2 : Diagram figure caption",
    )
    for caption in captions:
        line, page = next(
            (line, page) for page in pages for line in page["lines"] if caption in line["text"]
        )
        caption_center = (line["left"] + line["right"]) / 2
        # The text area has symmetric margins; 2 pt allows bbox glyph extents.
        assert abs(caption_center - page["width"] / 2) <= 2.0, (caption, caption_center)

    # Spec §1.5 uses adjacent pdftotext blocks at y≈100-730 pt, >=2 lines,
    # >=250 pt wide, >=75% horizontal overlap, and an ink gap above the
    # 15.33 pt CN/RU reference measurement (M21-FORMAT: more spaced on purpose).
    gaps = _paragraph_gap_measurements(pages)
    assert len(gaps) >= 1, "too few paragraph block pairs met the §1.5 filter"
    median = statistics_median(gaps)
    assert 15.5 <= median <= 26.0, (kind, median, gaps)

    font_names = _pdf_font_list(path)
    assert "hanken" in font_names
    assert "cascadia" in font_names
    reader = PdfReader(path, strict=True)
    linked_urls = {
        str(annotation.get_object().get("/A", {}).get("/URI", ""))
        for page in reader.pages
        for annotation in page.get("/Annots", ())
    }
    assert {_REFERENCE_URL, _TIMELINE_URL} <= linked_urls
    _assert_timeline_rail(path, pages, tmp_path)
    _assert_overview_box(path, pages, tmp_path)


@pytest.mark.parametrize("kind", ("article", "brief"))
@pytest.mark.asyncio
async def test_m19_reference_entries_have_separation_and_preserve_other_layout(
    tmp_path: Path,
    kind: str,
) -> None:
    render_data = _publication_fixture(kind, paragraph_count=1, include_figures=True)
    render_data["content_sections"][0]["entries"] = [
        {
            "display_date": "14 octobre 2026",
            "text": (
                "First source detail explains the complete analysis and its evidence. " * 5
            ).strip(),
            "url": _REFERENCE_URL,
        },
        {
            "display_date": "15 octobre 2026",
            "text": "Second source confirms the reported activity.",
            "url": _TIMELINE_URL,
        },
    ]
    path = await _compile_pdf(tmp_path, render_data)
    text, pages = _extract_layout(path)

    assert text.index("Références") < text.index("Chronologie") < text.index("Synthèse")
    reference_lines = sorted(
        [(page_index, line) for page_index, page in enumerate(pages) for line in page["lines"]],
        key=lambda item: (item[0], item[1]["top"]),
    )
    first_start = next(
        index
        for index, (_page_index, line) in enumerate(reference_lines)
        if line["text"].startswith("• 14 octobre 2026 :")
    )
    second_start = next(
        index
        for index, (_page_index, line) in enumerate(reference_lines)
        if line["text"].startswith("• 15 octobre 2026 :")
    )
    first_entry = reference_lines[first_start:second_start]
    second_entry = reference_lines[second_start]
    first_entry = [item for item in first_entry if item[1]["top"] <= second_entry[1]["top"]]
    assert len(first_entry) >= 2, [line["text"] for _p, line in first_entry]
    assert all(page_index == second_entry[0] for page_index, _line in first_entry)
    internal_gaps = [
        following["top"] - previous["bottom"]
        for (_page_a, previous), (_page_b, following) in pairwise(first_entry)
    ]
    inter_entry_gap = second_entry[1]["top"] - first_entry[-1][1]["bottom"]
    assert inter_entry_gap > max(internal_gaps) + 1.0, (inter_entry_gap, internal_gaps)

    caption = next(
        (line, page)
        for page in pages
        for line in page["lines"]
        if "Figure 1 : Archived figure caption" in line["text"]
    )
    caption_line, caption_page = caption
    caption_center = (caption_line["left"] + caption_line["right"]) / 2
    assert abs(caption_center - caption_page["width"] / 2) <= 2.0


@pytest.mark.asyncio
async def test_opinion_references_timeline_and_overview_use_reader_format(tmp_path: Path) -> None:
    path = await _compile_pdf(tmp_path, _opinion_fixture())
    text, pages = _extract_layout(path)

    reference_line = next(
        (page_index, line)
        for page_index, page in enumerate(pages)
        for line in page["lines"]
        if "Synthetic advisory source detail." in line["text"]
    )
    page_index, line = reference_line
    assert line["text"].startswith("• 14 octobre 2026 : Synthetic advisory source detail.")
    assert "1. 14 octobre 2026" not in text
    _assert_reference_date_is_purple(path, line, page_index, tmp_path)

    assert "Chronologie" in text
    assert "Synthetic first advisory event." in text
    assert "Synthetic second event is documented." in text
    first_event_line = next(
        line["text"]
        for page in pages
        for line in page["lines"]
        if "Synthetic first advisory event." in line["text"]
    )
    assert first_event_line.endswith("1"), first_event_line
    assert "Vue d'ensemble" in text.replace("\u2019", "'")
    assert all(label in text for label in ("Victimologie", "Arsenal", "Objectif principal"))
    assert "Contexte" in text and "Synthetic opinion conclusion." in text
    _assert_timeline_rail(path, pages, tmp_path)
    _assert_overview_box(path, pages, tmp_path)

    reader = PdfReader(path, strict=True)
    linked_urls = {
        str(annotation.get_object().get("/A", {}).get("/URI", ""))
        for page in reader.pages
        for annotation in page.get("/Annots", ())
    }
    assert "https://source.example.test/advisory" in linked_urls


@pytest.mark.parametrize("kind", ("article", "brief"))
@pytest.mark.parametrize("timeline_state", ("empty", "absent"))
@pytest.mark.asyncio
async def test_empty_article_or_brief_timeline_has_no_orphan_heading(
    tmp_path: Path,
    kind: str,
    timeline_state: str,
) -> None:
    fixture = _publication_fixture(kind, paragraph_count=1, include_figures=False, events=[])
    if timeline_state == "absent":
        if kind == "article":
            del fixture["article_format"]["timeline_key_events"]
        else:
            chronology = next(
                section
                for section in fixture["content_sections"]
                if section["type"] == "chronology"
            )
            chronology.pop("timeline")
    path = await _compile_pdf(tmp_path, fixture)
    text, _pages = _extract_layout(path)
    assert "Chronologie" not in text
    assert "Synthetic first event is documented." not in text


@pytest.mark.asyncio
async def test_edition_resets_footnote_numbers_per_publication_without_timeline_collision(
    tmp_path: Path,
) -> None:
    article = _publication_fixture("article", paragraph_count=1, include_figures=False)
    brief = _publication_fixture("brief", paragraph_count=1, include_figures=False)
    for position, publication in enumerate((article, brief), start=1):
        publication.update(
            {
                "position": position,
                "number": f"{position:02d}",
                "anchor": f"m19-publication-{position}",
            }
        )
    edition = {
        "schema_version": "typst-edition-model-v24-m19-fixture",
        "document_label": "m19-edition-footnote-fixture",
        "document_header": "Synthetic edition",
        "edition": {
            "bulletin_title": "Synthetic edition",
            "country": "Example",
            "country_code": "EX",
            "bulletin_period": "October 2026",
            "bulletin_number": "1",
            "bulletin_identifier": "M19",
            "tlp": "TLP:CLEAR",
        },
        "file_ioc_count": 0,
        "publications": [article, brief],
    }
    path = await _compile_pdf(
        tmp_path,
        edition,
        entrypoint="RENDERER/edition.typ",
        manifest_name="edition-renderer-manifest.json",
    )
    _xml, pages = _extract_layout(path)
    for title in ("Synthetic article publication", "Synthetic brief publication"):
        publication_page = next(
            page
            for page in pages
            if any(title in line["text"] for line in page["lines"])
            and any("Synthetic source summary sentence." in line["text"] for line in page["lines"])
        )
        reference_line = next(
            line["text"]
            for line in publication_page["lines"]
            if "Synthetic source summary sentence." in line["text"]
        )
        event_line = next(
            line["text"]
            for line in publication_page["lines"]
            if "Synthetic first event is documented." in line["text"]
        )
        assert reference_line.rstrip().endswith("1"), reference_line
        assert event_line.rstrip().endswith("2"), event_line


def test_detection_section_is_not_rendered_in_reader_pdfs() -> None:
    helpers = (_TEMPLATE_ROOT / "RENDERER" / "publication_helpers.typ").read_text(encoding="utf-8")
    branch = helpers.split('content_section.type == "detection" [', 1)[1].split(
        'content_section.type == "ioc" [', 1
    )[0]

    assert "#none" in branch
    for internal_label in ("Code source", "Règle YARA candidate", "Couverture", "Limite :"):
        assert internal_label not in branch
