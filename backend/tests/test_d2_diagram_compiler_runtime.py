from __future__ import annotations

import asyncio
import hashlib
import html
import os
import re
import shutil
import subprocess
from dataclasses import replace
from typing import NoReturn
from uuid import UUID

import pytest

from cti_app.domain.production_editorial_enrichment import (
    DiagramEdgeV1,
    DiagramGroupV1,
    DiagramNodeV1,
    DiagramSpecV1,
    EnrichmentDiagramDirection,
    EnrichmentDiagramKind,
    EnrichmentPlacementKind,
    EnrichmentPlacementV1,
)
from cti_app.domain.production_synthesis import EvidenceKind, ExtractionEvidenceRefV1
from cti_app.infrastructure.d2_diagram_compiler import D2_COMPILER_VERSION, D2DiagramCompiler

_EVIDENCE = ExtractionEvidenceRefV1(
    source_document_id=UUID("00000000-0000-0000-0000-000000000001"),
    kind=EvidenceKind.FACT,
    evidence_key="a" * 64,
)


def _canonical_diagram() -> DiagramSpecV1:
    return DiagramSpecV1(
        key="runtime-check",
        kind=EnrichmentDiagramKind.CUSTOM,
        title="Runtime check",
        caption=None,
        direction=EnrichmentDiagramDirection.LEFT_TO_RIGHT,
        nodes=(
            DiagramNodeV1("start", "Start", (_EVIDENCE,)),
            DiagramNodeV1("finish", "Finish", (_EVIDENCE,)),
        ),
        edges=(DiagramEdgeV1("start", "finish", "connects", (_EVIDENCE,)),),
        groups=(),
        placement=EnrichmentPlacementV1(EnrichmentPlacementKind.END),
    )


@pytest.fixture
def d2_binary() -> str:
    binary = os.environ.get("D2_BINARY") or shutil.which("d2")
    if binary is None:
        _skip_unless_ci("d2 executable is not installed")
    assert binary is not None
    try:
        probe = subprocess.run((binary, "--version"), capture_output=True, check=False, timeout=10)
    except (OSError, subprocess.TimeoutExpired) as exc:
        _skip_unless_ci(f"d2 version check failed: {exc}")
    reported = probe.stdout.decode("utf-8", errors="replace")
    if probe.returncode != 0 or reported.strip().removeprefix("v") != D2_COMPILER_VERSION:
        _skip_unless_ci(f"d2 {D2_COMPILER_VERSION} is required, found {reported.strip()!r}")
    return binary


def _skip_unless_ci(reason: str) -> NoReturn:
    if os.environ.get("CI", "").lower() == "true":
        pytest.fail(reason)
    pytest.skip(reason)


def _svg_text(svg: bytes) -> list[str]:
    return [
        html.unescape(
            re.sub(
                r"<[^>]+>",
                "",
                re.sub(r"</tspan>\s*<tspan[^>]*>", " ", fragment),
            )
        )
        for fragment in re.findall(r"<text[^>]*>(.*?)</text>", svg.decode("utf-8"), re.DOTALL)
    ]


def test_real_d2_compilation_is_byte_deterministic(d2_binary: str) -> None:
    async def compile_twice() -> tuple:
        compiler = D2DiagramCompiler(binary=d2_binary)
        diagram = _canonical_diagram()
        return (
            await compiler.compile(diagram),
            await compiler.compile(diagram),
            await compiler.compile(replace(diagram, key="runtime-other")),
            await compiler.compile(
                replace(diagram, title="Updated legend", caption="Changed caption")
            ),
        )

    first, second, other_key, other_legend = asyncio.run(compile_twice())

    assert first.source_bytes == second.source_bytes
    assert first.source_bytes.startswith(b"direction: right\n")
    assert b"shape: oval" in first.source_bytes
    assert b'fill: "#F1F1EF"' in first.source_bytes
    assert first.source_bytes.endswith(b"\n") and not first.source_bytes.endswith(b"\n\n")
    assert first.source_sha256 == second.source_sha256
    assert first.media_bytes == second.media_bytes
    assert first.media_sha256 == second.media_sha256
    assert hashlib.sha256(first.media_bytes).hexdigest() == first.media_sha256
    assert other_key.source_bytes == other_legend.source_bytes == first.source_bytes
    assert other_key.media_bytes == other_legend.media_bytes == first.media_bytes
    assert other_key.media_sha256 == other_legend.media_sha256 == first.media_sha256


def test_real_d2_renders_hostile_labels_as_visible_text(d2_binary: str) -> None:
    labels = (
        'attacker\'s "loader" C:\\Temp\\x.exe',
        "${HOME} $PATH; a -> b: c {icon: https://example.test/i.png}",
        "# not a comment | not md | @import 'x.d2'",
        "[link](https://example.test) ...@spread",
    )
    diagram = DiagramSpecV1(
        key="runtime-hostile",
        kind=EnrichmentDiagramKind.CUSTOM,
        title="Hostile labels",
        caption=None,
        direction=EnrichmentDiagramDirection.TOP_TO_BOTTOM,
        nodes=(
            DiagramNodeV1("a", labels[0], (_EVIDENCE,)),
            DiagramNodeV1("b", labels[1], (_EVIDENCE,)),
        ),
        edges=(DiagramEdgeV1("a", "b", labels[2], (_EVIDENCE,)),),
        groups=(DiagramGroupV1("group", labels[3], ("a",)),),
        placement=EnrichmentPlacementV1(EnrichmentPlacementKind.END),
    )

    compiled = asyncio.run(D2DiagramCompiler(binary=d2_binary).compile(diagram))

    assert sorted(" ".join(label.split()) for label in _svg_text(compiled.media_bytes)) == sorted(
        " ".join(label.split()) for label in labels
    )
