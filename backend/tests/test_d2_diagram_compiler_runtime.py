from __future__ import annotations

import asyncio
import hashlib
import shutil
import subprocess
from uuid import UUID

import pytest

from cti_app.domain.production_editorial_enrichment import (
    DiagramEdgeV1,
    DiagramNodeV1,
    DiagramSpecV1,
    EnrichmentDiagramDirection,
    EnrichmentDiagramKind,
    EnrichmentPlacementKind,
    EnrichmentPlacementV1,
)
from cti_app.domain.production_synthesis import EvidenceKind, ExtractionEvidenceRefV1
from cti_app.infrastructure.d2_diagram_compiler import D2_COMPILER_VERSION, D2DiagramCompiler


def _canonical_diagram() -> DiagramSpecV1:
    evidence = ExtractionEvidenceRefV1(
        source_document_id=UUID("00000000-0000-0000-0000-000000000001"),
        kind=EvidenceKind.FACT,
        evidence_key="a" * 64,
    )
    return DiagramSpecV1(
        key="runtime-check",
        kind=EnrichmentDiagramKind.CUSTOM,
        title="Runtime check",
        caption=None,
        direction=EnrichmentDiagramDirection.LEFT_TO_RIGHT,
        nodes=(
            DiagramNodeV1("start", "Start", (evidence,)),
            DiagramNodeV1("finish", "Finish", (evidence,)),
        ),
        edges=(DiagramEdgeV1("start", "finish", "connects", (evidence,)),),
        groups=(),
        placement=EnrichmentPlacementV1(EnrichmentPlacementKind.END),
    )


async def _compile_twice(compiler: D2DiagramCompiler, diagram: DiagramSpecV1):
    return await compiler.compile(diagram), await compiler.compile(diagram)


def test_real_d2_compilation_is_byte_deterministic() -> None:
    binary = shutil.which("d2")
    if binary is None:
        pytest.skip("d2 executable is not installed")
    probe = subprocess.run((binary, "--version"), capture_output=True, check=False, timeout=10)
    reported = probe.stdout.decode("utf-8", errors="replace")
    if reported.strip().removeprefix("v") != D2_COMPILER_VERSION:
        pytest.skip(f"d2 {D2_COMPILER_VERSION} is not installed")

    compiler = D2DiagramCompiler(binary=binary)
    first, second = asyncio.run(_compile_twice(compiler, _canonical_diagram()))

    expected_source = b"direction: right\nn001: 'Start'\nn002: 'Finish'\nn001 -> n002: 'connects'\n"
    expected_source_sha256 = "c1a6c458be4c5923564e42b818ebcbd65084a712f58bf1b582ee5126dff362e8"
    assert first.source_bytes == second.source_bytes == expected_source
    assert first.source_sha256 == second.source_sha256 == expected_source_sha256
    assert first.media_bytes == second.media_bytes
    assert first.media_sha256 == second.media_sha256
    assert hashlib.sha256(first.media_bytes).hexdigest() == first.media_sha256
