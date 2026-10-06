from __future__ import annotations

import json
import os
import shutil
import subprocess
from io import BytesIO
from pathlib import Path
from typing import Any, NoReturn

import anyio
import pytest
from pypdf import PdfReader

from cti_app.application.typst_compilation import (
    TYPST_COMPILER_VERSION,
    TypstCompileRequest,
    load_font_bundle_snapshot,
    materialize_font_bundle,
)
from cti_app.application.typst_rendering import (
    _breakable_typst_display_text,
    _table_cell_typst_spans,
)
from cti_app.infrastructure.typst_compiler import TypstSubprocessCompiler

_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
_FONT_BUNDLE_LOCK = _REPOSITORY_ROOT / "infra" / "typst-fonts.lock"


@pytest.fixture(scope="session")
def font_bundle_root() -> Path:
    configured_root = os.environ.get("FONT_BUNDLE_ROOT")
    return Path(configured_root) if configured_root else _REPOSITORY_ROOT / "chpTypst"


def _skip_unless_ci(reason: str) -> NoReturn:
    """Mirror test_d2_diagram_compiler_runtime.py: fail hard in CI, skip locally."""
    if os.environ.get("CI", "").lower() == "true":
        pytest.fail(reason)
    pytest.skip(reason)


@pytest.fixture(scope="session")
def typst_binary() -> str:
    """Mirror the d2_binary fixture: use a pre-installed pinned binary only.

    CI installs Typst once via scripts/install-typst.sh and exposes it as
    TYPST_BINARY/PATH (see .github/workflows/ci.yml), matching the existing D2
    pattern. FONT_BUNDLE_ROOT selects the matching installed font bundle and
    falls back to the local ignored chpTypst fonts when available. Local runs
    without a pinned Typst on PATH skip instead of re-downloading per session.
    """
    binary = os.environ.get("TYPST_BINARY") or shutil.which("typst")
    if binary is None:
        _skip_unless_ci(
            "typst executable is not installed; run scripts/install-typst.sh or set TYPST_BINARY"
        )
    assert binary is not None
    try:
        probe = subprocess.run((binary, "--version"), capture_output=True, check=False, timeout=10)
    except (OSError, subprocess.TimeoutExpired) as exc:
        _skip_unless_ci(f"typst version check failed: {exc}")
    reported = probe.stdout.decode("utf-8", errors="replace").strip()
    reported_parts = reported.split()
    reported_version = (
        reported_parts[1] if len(reported_parts) >= 2 and reported_parts[0] == "typst" else ""
    )
    if probe.returncode != 0 or reported_version != TYPST_COMPILER_VERSION:
        _skip_unless_ci(f"typst {TYPST_COMPILER_VERSION} is required, found {reported!r}")
    return binary


def _runtime_render_data() -> dict[str, Any]:
    table_rows = [
        ["Contexte", "Deux observations consignées dans le rapport"],
        ["Hash SHA-256", "a" * 64],
        [
            "URL de téléchargement",
            "https://downloads.example.test/releases/2026/10/06/example-rat-loader-package?campaign=autumn-update&source=bulletin",
        ],
        ["Adresse IP", "198.51.100.42"],
        ["Domaine", "cdn.example.test"],
    ]
    return {
        "schema_version": "typst-publication-model-v8-unified-ioc-rendering",
        "language": "fr",
        "title": "AW-019 runtime publication fixture",
        "content_sections": [
            {
                "type": "references",
                "timeline": [
                    {
                        "display_date": "2026-09-30",
                        "text": "First observed event in the fixture timeline.",
                        "source_urls": ["https://example.test/timeline"],
                    },
                    {
                        "display_date": "2026-10-01",
                        "text": "A second timeline event.",
                        "source_urls": [],
                    },
                ],
                "blocks": [],
                "sources": [
                    {
                        "title": "Example runtime source",
                        "publisher": "AutoWork fixture",
                        "date": "2026-10-01",
                        "url": "https://example.test/source",
                    }
                ],
            },
            {
                "type": "synthesis",
                "blocks": [
                    {"type": "paragraph", "text": "This paragraph exercises the publication lead."},
                    {
                        "type": "table",
                        "key": "lead-table",
                        "title": "Observed technical values",
                        "caption": "Exact values from the cited source.",
                        "columns": ["Type", "Value"],
                        "rows": table_rows,
                        "semantic_cells": [
                            _table_cell_typst_spans(value) for row in table_rows for value in row
                        ],
                    },
                    {"type": "paragraph", "text": "First section content."},
                    {
                        "type": "figure",
                        "figure_number": 1,
                        "key": "runtime-figure",
                        "caption": "Vendored CHP image used as a figure.",
                        "provenance": "AW-019 runtime fixture",
                        "locator": "page 1, figure A",
                        "media_path": "media/figure.png",
                    },
                    {"type": "paragraph", "text": "Second section content."},
                    {
                        "type": "table",
                        "key": "end-table",
                        "title": "End placement table",
                        "caption": "A rich block after the final section.",
                        "columns": ["Check", "Result"],
                        "rows": [["PDF", "valid"], ["Page size", "A4"]],
                    },
                ],
            },
            {
                "type": "technical_annex",
                "indicators": {
                    "ips": ["192.0.2.10"],
                    "domains": ["example.test"],
                    "urls": ["https://example.test/path"],
                    "emails": ["analyst@example.test"],
                    "hashes": ["0123456789abcdef0123456789abcdef"],
                },
            },
        ],
    }


def _build_runtime_workspace(
    repository_root: Path,
    workspace_root: Path,
    *,
    render_data: dict[str, Any] | None = None,
    media_files: dict[str, str] | None = None,
) -> None:
    chp_typst_root = repository_root / "chpTypst"
    shutil.copytree(chp_typst_root / "RENDERER", workspace_root / "RENDERER")
    shutil.copytree(chp_typst_root / "UTILS", workspace_root / "UTILS")
    media_root = workspace_root / "RENDERER" / "media"
    media_root.mkdir()
    (media_root / "diagram.svg").write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 240 72">'
        '<rect x="1" y="1" width="238" height="70" rx="8" fill="#eee" stroke="#555"/>'
        '<text x="12" y="43" font-size="18">local diagram</text></svg>',
        encoding="utf-8",
    )
    shutil.copy2(chp_typst_root / "UTILS" / "chap.png", media_root / "figure.png")
    for name, content in (media_files or {}).items():
        (media_root / name).write_text(content, encoding="utf-8")
    data = json.dumps(
        render_data if render_data is not None else _runtime_render_data(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    (workspace_root / "RENDERER" / "render-data.json").write_text(data, encoding="utf-8")


def _svg_fixture(width: int, height: int, label: str, fill: str) -> str:
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}">'
        f'<rect x="1" y="1" width="{width - 2}" height="{height - 2}" '
        f'rx="18" fill="{fill}" stroke="#35445a" stroke-width="3"/>'
        f'<text x="24" y="{height // 2}" font-family="sans-serif" font-size="32" '
        f'fill="#172033">{label}</text></svg>'
    )


def _timeline_chart_fixture() -> str:
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" width="1000" height="430" '
        'viewBox="0 0 1000 430">'
        '<rect width="1000" height="430" fill="#e3f2e8"/>'
        '<line x1="80" y1="350" x2="950" y2="350" stroke="#40505b" stroke-width="3"/>'
        '<line x1="80" y1="65" x2="80" y2="350" stroke="#40505b" stroke-width="3"/>'
        '<line x1="80" y1="275" x2="950" y2="275" stroke="#b5c6bb" stroke-width="2"/>'
        '<line x1="80" y1="195" x2="950" y2="195" stroke="#b5c6bb" stroke-width="2"/>'
        '<line x1="80" y1="115" x2="950" y2="115" stroke="#b5c6bb" stroke-width="2"/>'
        '<polyline points="130,290 370,240 610,165 860,95" fill="none" '
        'stroke="#2d7550" stroke-width="8"/>'
        '<circle cx="130" cy="290" r="12" fill="#2d7550"/>'
        '<circle cx="370" cy="240" r="12" fill="#2d7550"/>'
        '<circle cx="610" cy="165" r="12" fill="#2d7550"/>'
        '<circle cx="860" cy="95" r="12" fill="#2d7550"/>'
        '<text x="95" y="400" font-family="sans-serif" font-size="24">Jan.</text>'
        '<text x="335" y="400" font-family="sans-serif" font-size="24">Mar.</text>'
        '<text x="575" y="400" font-family="sans-serif" font-size="24">Mai</text>'
        '<text x="825" y="400" font-family="sans-serif" font-size="24">Juil.</text>'
        '<text x="105" y="55" font-family="sans-serif" font-size="24">Sources publiées</text>'
        "</svg>"
    )


def _flow_diagram_fixture() -> str:
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" width="1000" height="540" '
        'viewBox="0 0 1000 540">'
        '<rect width="1000" height="540" fill="#e9e4f5"/>'
        '<rect x="35" y="195" width="250" height="135" rx="18" fill="#fff" '
        'stroke="#59627a" stroke-width="4"/>'
        '<rect x="375" y="195" width="250" height="135" rx="18" fill="#fff" '
        'stroke="#59627a" stroke-width="4"/>'
        '<rect x="715" y="195" width="250" height="135" rx="18" fill="#fff" '
        'stroke="#59627a" stroke-width="4"/>'
        '<line x1="285" y1="262" x2="350" y2="262" stroke="#39465d" stroke-width="6"/>'
        '<polygon points="350,250 375,262 350,274" fill="#39465d"/>'
        '<line x1="625" y1="262" x2="690" y2="262" stroke="#39465d" stroke-width="6"/>'
        '<polygon points="690,250 715,262 690,274" fill="#39465d"/>'
        '<text x="70" y="250" font-family="sans-serif" font-size="26">Accès initial</text>'
        '<text x="82" y="292" font-family="sans-serif" font-size="22">Compte compromis</text>'
        '<text x="433" y="250" font-family="sans-serif" font-size="26">Exécution</text>'
        '<text x="413" y="292" font-family="sans-serif" font-size="22">Script malveillant</text>'
        '<text x="792" y="250" font-family="sans-serif" font-size="26">C2</text>'
        '<text x="755" y="292" font-family="sans-serif" font-size="22">Infrastructure</text>'
        "</svg>"
    )


def _relationship_diagram_fixture() -> str:
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" width="900" height="600" '
        'viewBox="0 0 900 600">'
        '<rect width="900" height="600" fill="#f5e4e4"/>'
        '<line x1="225" y1="285" x2="420" y2="190" stroke="#664a5b" stroke-width="5"/>'
        '<polygon points="410,178 438,182 422,204" fill="#664a5b"/>'
        '<line x1="480" y1="210" x2="680" y2="300" stroke="#664a5b" stroke-width="5"/>'
        '<polygon points="670,285 697,308 667,313" fill="#664a5b"/>'
        '<line x1="225" y1="325" x2="680" y2="325" stroke="#8b687c" stroke-width="4" '
        'stroke-dasharray="12 9"/>'
        '<ellipse cx="155" cy="305" rx="110" ry="72" fill="#fff" '
        'stroke="#664a5b" stroke-width="4"/>'
        '<rect x="390" y="120" width="180" height="140" rx="18" fill="#fff" '
        'stroke="#664a5b" stroke-width="4"/>'
        '<ellipse cx="745" cy="325" rx="120" ry="72" fill="#fff" '
        'stroke="#664a5b" stroke-width="4"/>'
        '<text x="93" y="315" font-family="sans-serif" font-size="27">APT31</text>'
        '<text x="420" y="185" font-family="sans-serif" font-size="25">Proxy</text>'
        '<text x="690" y="315" font-family="sans-serif" font-size="22">Victime</text>'
        '<text x="280" y="218" font-family="sans-serif" font-size="18">utilise</text>'
        '<text x="580" y="230" font-family="sans-serif" font-size="18">cible</text>'
        "</svg>"
    )


def _lot6_composition_render_data() -> tuple[dict[str, Any], tuple[str, ...]]:
    ips = [f"198.51.100.{index}" for index in range(1, 21)]
    domains = [f"host-{index:03d}.long-indicator.example.test" for index in range(1, 21)]
    raw_urls = [
        "https://downloads.example.test/releases/2026/10/06/"
        + f"package-{index:03d}/"
        + "segment/" * 10
        + f"artifact-{index:03d}.bin?"
        + "campaign=autumn-update&source=publication-fixture&signature="
        + "a" * 80
        for index in range(1, 21)
    ]
    emails = [f"analyst-{index:03d}@mail.example.test" for index in range(1, 21)]
    hashes = [f"{index:064x}" for index in range(1, 21)]
    values = tuple(
        _breakable_typst_display_text(value)
        for group in (ips, domains, raw_urls, emails, hashes)
        for value in group
    )
    assert len(values) == 100
    rendered_values = {
        key: [_breakable_typst_display_text(value) for value in group]
        for key, group in (
            ("ips", ips),
            ("domains", domains),
            ("urls", raw_urls),
            ("emails", emails),
            ("hashes", hashes),
        )
    }
    original_values = {key: group[:1] for key, group in rendered_values.items()}
    for group in original_values.values():
        for index, value in enumerate(group):
            group[index] = _breakable_typst_display_text(value.replace("\u200b", ""))

    return (
        {
            "schema_version": "typst-publication-model-v8-unified-ioc-rendering",
            "language": "fr",
            "title": "[LOT 6] Fixture de composition A4",
            "content_sections": [
                {
                    "type": "references",
                    "timeline": [
                        {
                            "display_date": "5 octobre 2026",
                            "text": "La source décrit l'activité après sa publication.",
                            "source_urls": [],
                        }
                    ],
                    "blocks": [
                        {
                            "type": "figure",
                            "figure_number": 1,
                            "key": "source-wide",
                            "caption": "Figure source wide, repère A.",
                            "provenance": "Média source archivé",
                            "locator": "page 2, figure A",
                            "media_path": "media/source-wide.svg",
                        }
                    ],
                    "sources": [],
                },
                {
                    "type": "synthesis",
                    "blocks": [
                        {
                            "type": "paragraph",
                            "text": "Composition complète de référence pour le rendu final.",
                        },
                        {
                            "type": "figure",
                            "figure_number": 2,
                            "key": "source-portrait",
                            "caption": "Figure source portrait, repère B.",
                            "provenance": "Média source archivé",
                            "locator": "page 3, figure B",
                            "media_path": "media/source-portrait.svg",
                        },
                        {
                            "type": "chart",
                            "figure_number": 3,
                            "key": "timeline-chart",
                            "kind": "timeline",
                            "title": "Titre chart qui ne doit pas apparaître au-dessus",
                            "caption": "Événements datés selon leur source.",
                            "media_path": "media/timeline-chart.svg",
                        },
                        {
                            "type": "diagram",
                            "figure_number": 4,
                            "key": "flow-diagram",
                            "title": "Titre FLOW qui ne doit pas apparaître au-dessus",
                            "caption": "Flux de compromission observé.",
                            "media_path": "media/flow-diagram.svg",
                        },
                        {
                            "type": "table",
                            "key": "runtime-table",
                            "title": "Tableau des artefacts observés",
                            "caption": "Valeurs représentatives de la source.",
                            "columns": ["Type", "Valeur"],
                            "rows": [["Domaine", "host-001.long-indicator.example.test"]],
                        },
                        {
                            "type": "diagram",
                            "figure_number": 5,
                            "key": "relationship-diagram",
                            "title": "Titre RELATIONSHIP qui ne doit pas apparaître au-dessus",
                            "caption": "Relations entre opérateur et infrastructure.",
                            "media_path": "media/relationship-diagram.svg",
                        },
                    ],
                },
                {
                    "type": "technical_annex",
                    "indicators": rendered_values,
                    "original_indicators": original_values,
                    "original_indicator_note": "Classification originale conservée en interne.",
                },
            ],
        },
        values,
    )


@pytest.mark.asyncio
async def test_real_typst_compiles_representative_workspace_deterministically(
    tmp_path: Path, typst_binary: str, font_bundle_root: Path
) -> None:
    workspace_root = tmp_path / "workspace"
    _build_runtime_workspace(_REPOSITORY_ROOT, workspace_root)
    font_snapshot = load_font_bundle_snapshot(font_bundle_root, _FONT_BUNDLE_LOCK)
    font_root = tmp_path / "font-snapshot"
    font_root.mkdir()
    request = TypstCompileRequest(
        workspace_root=workspace_root,
        entrypoint_relative_path="RENDERER/publication.typ",
        font_paths=materialize_font_bundle(font_snapshot, font_root),
    )
    compiler = TypstSubprocessCompiler(binary=str(typst_binary))

    first = await compiler.compile(request)
    first_reader = PdfReader(BytesIO(first.content), strict=True)
    second = await compiler.compile(request)
    second_reader = PdfReader(BytesIO(second.content), strict=True)

    assert first.compiler_version == "0.15.1"
    assert len(first_reader.pages) >= 1
    assert not first_reader.is_encrypted
    assert len(second_reader.pages) >= 1
    assert not second_reader.is_encrypted
    assert second.sha256 == first.sha256, (
        "identical Typst workspace produced different PDF SHA-256 values: "
        f"{first.sha256} != {second.sha256}"
    )


@pytest.mark.asyncio
async def test_real_typst_lot6_a4_composition_fixture(
    tmp_path: Path,
    typst_binary: str,
    font_bundle_root: Path,
) -> None:
    render_data, expected_iocs = _lot6_composition_render_data()
    media_files = {
        "source-wide.svg": _svg_fixture(1200, 420, "SOURCE WIDE MEDIA", "#e7eef8"),
        "source-portrait.svg": _svg_fixture(480, 1500, "SOURCE PORTRAIT MEDIA", "#f6eadb"),
        "timeline-chart.svg": _timeline_chart_fixture(),
        "flow-diagram.svg": _flow_diagram_fixture(),
        "relationship-diagram.svg": _relationship_diagram_fixture(),
    }
    workspace_root = tmp_path / "lot6-a4-workspace"
    _build_runtime_workspace(
        _REPOSITORY_ROOT,
        workspace_root,
        render_data=render_data,
        media_files=media_files,
    )
    font_snapshot = load_font_bundle_snapshot(font_bundle_root, _FONT_BUNDLE_LOCK)
    font_root = tmp_path / "lot6-a4-font-snapshot"
    font_root.mkdir()
    compiled = await TypstSubprocessCompiler(binary=str(typst_binary)).compile(
        TypstCompileRequest(
            workspace_root=workspace_root,
            entrypoint_relative_path="RENDERER/publication.typ",
            font_paths=materialize_font_bundle(font_snapshot, font_root),
        )
    )

    review_directory = anyio.Path(
        os.environ.get("AUTOWORK_REVIEW_ARTIFACT_DIR", tmp_path / "lot6-a4-review")
    )
    await review_directory.mkdir(parents=True, exist_ok=True)
    pdf_path = review_directory / "lot6-composition-a4.pdf"
    await pdf_path.write_bytes(compiled.content)
    rasterizer = shutil.which("pdftoppm")
    assert rasterizer is not None, "pdftoppm is required to inspect every fixture page"
    png_prefix = review_directory / "lot6-composition-a4"
    with anyio.fail_after(90):
        rasterized = await anyio.run_process(
            (rasterizer, "-png", "-r", "120", str(pdf_path), str(png_prefix))
        )
    assert rasterized.returncode == 0, rasterized.stderr.decode("utf-8", errors="replace")
    png_pages = [path async for path in review_directory.glob("lot6-composition-a4-*.png")]

    reader = PdfReader(BytesIO(compiled.content), strict=True)
    page_text = [page.extract_text() or "" for page in reader.pages]
    text = " ".join(" ".join(value.split()) for value in page_text)
    compact_text = "".join(text.split()).replace("\u200b", "")
    assert len(reader.pages) >= 4
    assert all(
        abs(float(page.mediabox.width) - 595.28) < 0.2
        and abs(float(page.mediabox.height) - 841.89) < 0.2
        for page in reader.pages
    )
    assert "5 octobre 2026 : La source décrit l'activité après sa publication." in text
    for number, caption in enumerate(
        (
            "Figure source wide, repère A.",
            "Figure source portrait, repère B.",
            "Événements datés selon leur source.",
            "Flux de compromission observé.",
            "Relations entre opérateur et infrastructure.",
        ),
        start=1,
    ):
        assert f"Figure {number} : {caption}" in text
    for hidden_title in (
        "Titre chart qui ne doit pas apparaître au-dessus",
        "Titre FLOW qui ne doit pas apparaître au-dessus",
        "Titre RELATIONSHIP qui ne doit pas apparaître au-dessus",
    ):
        assert hidden_title not in text
    assert "Tableau des artefacts observés" in text
    assert text.count("IOC") == 1
    assert "IOC originaux" not in text
    assert "Classification originale conservée en interne" not in text
    missing_iocs = tuple(
        value.replace("\u200b", "")
        for value in expected_iocs
        if value.replace("\u200b", "") not in compact_text
    )
    assert not missing_iocs, f"IOC values missing from extracted PDF text: {missing_iocs[:8]!r}"
    assert len(png_pages) == len(reader.pages)
    png_sizes = [(await path.stat()).st_size for path in png_pages]
    assert all(size > 0 for size in png_sizes)
