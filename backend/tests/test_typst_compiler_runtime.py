from __future__ import annotations

import json
import os
import shutil
import subprocess
from io import BytesIO
from pathlib import Path
from typing import Any, NoReturn

import pytest
from pypdf import PdfReader

from cti_app.application.typst_compilation import (
    TYPST_COMPILER_VERSION,
    TypstCompileRequest,
    load_font_bundle_snapshot,
    materialize_font_bundle,
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
    return {
        "schema_version": "typst-publication-model-v3-semantic-text",
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
                        "title": "Table after the lead",
                        "caption": "A compact local table.",
                        "columns": ["Indicator", "Value"],
                        "rows": [["IP", "192.0.2.10"], ["Domain", "example.test"]],
                    },
                    {"type": "paragraph", "text": "First section content."},
                    {
                        "type": "figure",
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
                        "rows": [["PDF", "valid"]],
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


def _build_runtime_workspace(repository_root: Path, workspace_root: Path) -> None:
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
    data = json.dumps(
        _runtime_render_data(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    (workspace_root / "RENDERER" / "render-data.json").write_text(data, encoding="utf-8")


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
