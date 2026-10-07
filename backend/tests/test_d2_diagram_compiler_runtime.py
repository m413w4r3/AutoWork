from __future__ import annotations

import asyncio
import hashlib
import html
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import replace
from importlib import util
from pathlib import Path
from typing import NoReturn
from uuid import UUID
from xml.etree import ElementTree

import pytest

from cti_app.domain.production_editorial_enrichment import (
    DiagramEdgeV1,
    DiagramGroupV1,
    DiagramNodeRole,
    DiagramNodeV1,
    DiagramProfile,
    DiagramRelationDirection,
    DiagramRelationType,
    DiagramSpecV1,
    EnrichmentDiagramDirection,
    EnrichmentDiagramKind,
    EnrichmentPlacementKind,
    EnrichmentPlacementV1,
)
from cti_app.domain.production_synthesis import EvidenceKind, ExtractionEvidenceRefV1
from cti_app.infrastructure.d2_diagram_compiler import (
    D2_COMPILER_VERSION,
    D2DiagramCompiler,
    encode_d2_source,
    validate_d2_svg,
)

_LAYOUT_HELPERS_SPEC = util.spec_from_file_location(
    "d2_svg_layout_helpers", Path(__file__).with_name("d2_svg_layout_helpers.py")
)
if _LAYOUT_HELPERS_SPEC is None or _LAYOUT_HELPERS_SPEC.loader is None:
    raise RuntimeError("Unable to load D2 SVG layout test helpers")
_LAYOUT_HELPERS = util.module_from_spec(_LAYOUT_HELPERS_SPEC)
sys.modules[_LAYOUT_HELPERS_SPEC.name] = _LAYOUT_HELPERS
_LAYOUT_HELPERS_SPEC.loader.exec_module(_LAYOUT_HELPERS)
estimate_printed_font_sizes = _LAYOUT_HELPERS.estimate_printed_font_sizes
measure_svg_overlaps = _LAYOUT_HELPERS.measure_svg_overlaps

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


def _cti_diagram(
    key: str,
    profile: DiagramProfile,
    kind: EnrichmentDiagramKind,
    nodes: tuple[DiagramNodeV1, ...],
    edges: tuple[DiagramEdgeV1, ...],
    *,
    direction: EnrichmentDiagramDirection = EnrichmentDiagramDirection.LEFT_TO_RIGHT,
    groups: tuple[DiagramGroupV1, ...] = (),
) -> DiagramSpecV1:
    return DiagramSpecV1(
        key=key,
        kind=kind,
        profile=profile,
        title=key.replace("_", " ").title(),
        caption=None,
        direction=direction,
        nodes=nodes,
        edges=edges,
        groups=groups,
        placement=EnrichmentPlacementV1(EnrichmentPlacementKind.END),
    )


def _cti_node(node_id: str, label: str, role: DiagramNodeRole) -> DiagramNodeV1:
    return DiagramNodeV1(node_id, label, (_EVIDENCE,), role)


def _cti_edge(
    source: str,
    target: str,
    label: str,
    relation_type: DiagramRelationType = DiagramRelationType.FACTUAL,
    direction: DiagramRelationDirection = DiagramRelationDirection.DIRECTED,
) -> DiagramEdgeV1:
    return DiagramEdgeV1(
        source,
        target,
        label,
        (_EVIDENCE,),
        relation_type,
        direction,
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


def _fixture_evidence_refs(raw_refs: list[dict[str, str]]) -> tuple[ExtractionEvidenceRefV1, ...]:
    return tuple(
        ExtractionEvidenceRefV1(
            source_document_id=UUID(raw["source_document_id"]),
            kind=EvidenceKind(raw["kind"]),
            evidence_key=raw["evidence_key"],
        )
        for raw in raw_refs
    )


def _published_diagram_corpus() -> tuple[tuple[str, DiagramSpecV1], ...]:
    fixture_root = Path(__file__).parent / "fixtures" / "d2_diagrams"
    results: list[tuple[str, DiagramSpecV1]] = []
    for fixture_path in sorted(fixture_root.glob("*.json")):
        fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
        raw = fixture["diagram"]
        nodes = tuple(
            DiagramNodeV1(
                node_id=node["node_id"],
                label=node["label"],
                evidence_refs=_fixture_evidence_refs(node["evidence_refs"]),
                role=DiagramNodeRole(node["role"]),
            )
            for node in raw["nodes"]
        )
        edges = tuple(
            DiagramEdgeV1(
                source_node_id=edge["source_node_id"],
                target_node_id=edge["target_node_id"],
                label=edge["label"],
                evidence_refs=_fixture_evidence_refs(edge["evidence_refs"]),
                relation_type=DiagramRelationType(
                    edge.get("relation_type", DiagramRelationType.FACTUAL.value)
                ),
                direction=(
                    DiagramRelationDirection(edge["direction"]) if "direction" in edge else None
                ),
            )
            for edge in raw["edges"]
        )
        groups = tuple(
            DiagramGroupV1(group["group_id"], group["label"], tuple(group["node_ids"]))
            for group in raw["groups"]
        )
        placement = raw["placement"]
        diagram = DiagramSpecV1(
            key=raw["key"],
            kind=EnrichmentDiagramKind(raw["kind"]),
            title=raw["title"],
            caption=raw["caption"],
            direction=EnrichmentDiagramDirection(raw["direction"]),
            nodes=nodes,
            edges=edges,
            groups=groups,
            placement=EnrichmentPlacementV1(
                EnrichmentPlacementKind(placement["kind"]), placement.get("section_index")
            ),
        )
        results.append((fixture["subject_id"], diagram))
    assert len(results) == 4, "The published D2 corpus must contain all four subject fixtures"
    return tuple(results)


_LAYOUT_CANDIDATES = (
    ("dagre-default", "dagre", ()),
    ("dagre-spaced", "dagre", ("--dagre-nodesep=120", "--dagre-edgesep=40")),
    ("elk-default", "elk", ()),
    ("elk-compact", "elk", ("--pad=8", "--elk-nodeNodeBetweenLayers=15")),
)


def _render_layout_candidate(
    d2_binary: str,
    diagram: DiagramSpecV1,
    *,
    layout: str,
    layout_flags: tuple[str, ...],
) -> bytes:
    source = encode_d2_source(diagram)
    argv = (
        d2_binary,
        f"--layout={layout}",
        *layout_flags,
        "--timeout=10",
        "--omit-version",
        f"--salt={hashlib.sha256(source).hexdigest()}",
        "--stdout-format=svg",
        "-",
        "-",
    )
    result = subprocess.run(
        argv,
        input=source,
        capture_output=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
    validate_d2_svg(result.stdout)
    return result.stdout


def _directed_arrowhead_count(svg_bytes: bytes) -> int:
    root = ElementTree.fromstring(svg_bytes)
    return sum(
        1
        for element in root.iter()
        if element.tag.rsplit("}", 1)[-1] == "path"
        and "connection" in element.attrib.get("class", "").split()
        and "marker-end" in element.attrib
    )


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
    assert b"shape: rectangle" in first.source_bytes
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


def test_real_d2_compiles_cti_profiles_and_printable_relationships(d2_binary: str) -> None:
    chain = _cti_diagram(
        "malware_chain",
        DiagramProfile.FLOW,
        EnrichmentDiagramKind.INFECTION_CHAIN,
        (
            _cti_node("victim", "Poste compromis", DiagramNodeRole.VICTIM),
            _cti_node("loader", "Loader", DiagramNodeRole.DATA_ARTIFACT),
            _cti_node("malware", "Malware", DiagramNodeRole.MALWARE_TOOL),
            _cti_node("c2", "C2 operator", DiagramNodeRole.INFRASTRUCTURE),
        ),
        (
            _cti_edge("victim", "loader", "exécute"),
            _cti_edge("loader", "malware", "charge"),
            _cti_edge("malware", "c2", "contacte"),
        ),
        direction=EnrichmentDiagramDirection.TOP_TO_BOTTOM,
    )
    c2 = _cti_diagram(
        "c2_flow",
        DiagramProfile.FLOW,
        EnrichmentDiagramKind.NETWORK_FLOW,
        (
            _cti_node("implant", "ExampleRAT", DiagramNodeRole.MALWARE_TOOL),
            _cti_node("domain", "c2.example.test", DiagramNodeRole.INFRASTRUCTURE),
            _cti_node("ip", "203.0.113.8", DiagramNodeRole.INFRASTRUCTURE),
        ),
        (
            _cti_edge("implant", "domain", "résout"),
            _cti_edge("domain", "ip", "pointe vers"),
        ),
        direction=EnrichmentDiagramDirection.TOP_TO_BOTTOM,
    )
    architecture = _cti_diagram(
        "architecture",
        DiagramProfile.ARCHITECTURE,
        EnrichmentDiagramKind.INFRASTRUCTURE,
        (
            _cti_node("victim", "Victime", DiagramNodeRole.VICTIM),
            _cti_node("proxy", "Nginx proxy", DiagramNodeRole.INFRASTRUCTURE),
            _cti_node("catcher", "Catcher", DiagramNodeRole.TECHNIQUE_STEP),
            _cti_node("storage", "Stockage", DiagramNodeRole.DATA_ARTIFACT),
            _cti_node("gardener", "Gardener", DiagramNodeRole.TECHNIQUE_STEP),
        ),
        (
            _cti_edge("victim", "proxy", "HTTPS"),
            _cti_edge("proxy", "catcher", "transmet"),
            _cti_edge("catcher", "storage", "archive"),
            _cti_edge(
                "catcher",
                "gardener",
                "probablement déclenche",
                DiagramRelationType.INFERENCE,
            ),
        ),
        direction=EnrichmentDiagramDirection.TOP_TO_BOTTOM,
        groups=(DiagramGroupV1("backend", "Backend", ("catcher", "storage", "gardener")),),
    )
    relationship = _cti_diagram(
        "relationship_graph",
        DiagramProfile.RELATIONSHIP,
        EnrichmentDiagramKind.COMPONENT_RELATIONSHIP,
        (
            _cti_node("document", "Rapport Word", DiagramNodeRole.DATA_ARTIFACT),
            _cti_node("xml", "Fichier XML", DiagramNodeRole.DATA_ARTIFACT),
            _cti_node("dll", "sample.dll", DiagramNodeRole.DATA_ARTIFACT),
            _cti_node("url", "https://drop.example/a", DiagramNodeRole.INFRASTRUCTURE),
            _cti_node("domain", "drop.example", DiagramNodeRole.INFRASTRUCTURE),
            _cti_node("ip", "198.51.100.27", DiagramNodeRole.INFRASTRUCTURE),
            _cti_node(
                "hash",
                "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
                DiagramNodeRole.DATA_ARTIFACT,
            ),
        ),
        (
            _cti_edge("document", "xml", "contient"),
            _cti_edge("document", "dll", "dépose"),
            _cti_edge("document", "url", "référence"),
            _cti_edge("domain", "ip", "résout vers"),
            _cti_edge(
                "dll",
                "hash",
                "correspond à",
                DiagramRelationType.INFERENCE,
                DiagramRelationDirection.UNDIRECTED,
            ),
        ),
        direction=EnrichmentDiagramDirection.TOP_TO_BOTTOM,
        groups=(
            DiagramGroupV1("operator-infrastructure", "Infrastructure opérateur", ("domain", "ip")),
        ),
    )
    branching = _cti_diagram(
        "branching_flow",
        DiagramProfile.FLOW,
        EnrichmentDiagramKind.EXECUTION_SEQUENCE,
        (
            _cti_node("lure", "Document leurre", DiagramNodeRole.DATA_ARTIFACT),
            _cti_node("xml_branch", "XML", DiagramNodeRole.DATA_ARTIFACT),
            _cti_node("dll_branch", "DLL", DiagramNodeRole.MALWARE_TOOL),
            _cti_node("url_branch", "URL de dépôt", DiagramNodeRole.INFRASTRUCTURE),
        ),
        (
            _cti_edge("lure", "xml_branch", "extrait"),
            _cti_edge("lure", "dll_branch", "dépose"),
            _cti_edge("lure", "url_branch", "ouvre"),
        ),
        direction=EnrichmentDiagramDirection.TOP_TO_BOTTOM,
    )
    # The model validator limits generated diagrams to eight nodes. The compiler
    # remains valid for larger canonical graphs and keeps the shared vertical rule.
    ten_nodes = tuple(
        _cti_node(f"stage_{index}", f"Étape {index}", DiagramNodeRole.TECHNIQUE_STEP)
        for index in range(1, 11)
    )
    ten_node_chain = _cti_diagram(
        "ten_node_chain",
        DiagramProfile.FLOW,
        EnrichmentDiagramKind.EXECUTION_SEQUENCE,
        ten_nodes,
        tuple(
            _cti_edge(f"stage_{index}", f"stage_{index + 1}", "enchaîne") for index in range(1, 10)
        ),
    )

    fixtures = (chain, c2, architecture, relationship, branching, ten_node_chain)

    async def compile_all() -> tuple:
        compiler = D2DiagramCompiler(binary=d2_binary)
        return tuple([await compiler.compile(diagram) for diagram in fixtures])

    compiled = asyncio.run(compile_all())

    for diagram, output in zip(fixtures, compiled, strict=True):
        assert output.compiler_version == D2_COMPILER_VERSION
        assert output.media_bytes.startswith(b"<svg") or b"<svg" in output.media_bytes[:256]
        rendered_text = " ".join(_svg_text(output.media_bytes))
        for node in diagram.nodes:
            assert node.label in rendered_text
        assert output.source_bytes.endswith(b"\n")
    assert compiled[0].source_bytes.startswith(b"direction: down\n")
    assert b"stroke-dash: 5" in compiled[2].source_bytes
    assert b"operator-infrastructure" not in compiled[3].source_bytes
    assert '"Infrastructure opérateur"'.encode() in compiled[3].source_bytes
    assert b"--" in compiled[3].source_bytes
    assert compiled[-1].source_bytes.startswith(b"direction: down\n")


def test_real_d2_layout_candidates_on_published_diagram_corpus(d2_binary: str) -> None:
    corpus = _published_diagram_corpus()
    metrics: dict[str, dict[str, tuple[int, int]]] = {
        candidate: {} for candidate, _, _ in _LAYOUT_CANDIDATES
    }
    for subject_id, diagram in corpus:
        for candidate, layout, flags in _LAYOUT_CANDIDATES:
            svg = _render_layout_candidate(
                d2_binary,
                diagram,
                layout=layout,
                layout_flags=flags,
            )
            report = measure_svg_overlaps(svg)
            metrics[candidate][subject_id] = (
                report.text_overlap_count,
                report.edge_node_overlap_count,
            )
            if candidate == "elk-compact":
                node_pt, edge_pt = estimate_printed_font_sizes(svg)
                assert node_pt >= 9.0, f"{subject_id} node labels print at only {node_pt:.2f}pt"
                assert edge_pt >= 8.0, f"{subject_id} edge labels print at only {edge_pt:.2f}pt"

    if os.environ.get("D2_LAYOUT_METRICS") == "1":
        print(json.dumps(metrics, ensure_ascii=False, sort_keys=True))

    elk_issues = {
        subject_id: counts
        for subject_id, counts in metrics["elk-compact"].items()
        if counts != (0, 0)
    }
    assert not elk_issues, (
        f"Compact ELK overlaps published diagram labels or node shapes: {elk_issues}"
    )
    s7_metrics = metrics["dagre-default"]["30fe8e86-f22d-436c-a3df-f5837e1661e2"]
    assert s7_metrics[0] > 0, "The published Arman overlap must remain detectable under dagre"


def test_real_d2_published_diagrams_are_clear_and_deterministic(d2_binary: str) -> None:
    async def compile_twice() -> tuple[tuple[str, DiagramSpecV1, bytes, bytes], ...]:
        compiler = D2DiagramCompiler(binary=d2_binary)
        outputs = []
        for subject_id, diagram in _published_diagram_corpus():
            first = await compiler.compile(diagram)
            second = await compiler.compile(diagram)
            outputs.append((subject_id, diagram, first.media_bytes, second.media_bytes))
        return tuple(outputs)

    for subject_id, diagram, first_svg, second_svg in asyncio.run(compile_twice()):
        assert first_svg == second_svg
        report = measure_svg_overlaps(first_svg)
        assert report.text_overlap_count == 0, (
            f"{subject_id}/{diagram.key} has overlapping text boxes: {report.text_overlap_pairs}"
        )
        assert report.edge_node_overlap_count == 0, (
            f"{subject_id}/{diagram.key} has edge labels over node shapes: "
            f"{report.edge_node_overlap_pairs}"
        )
        node_pt, edge_pt = estimate_printed_font_sizes(first_svg)
        assert node_pt >= 9.0, f"{subject_id}/{diagram.key} node labels print at {node_pt:.2f}pt"
        assert edge_pt >= 8.0, f"{subject_id}/{diagram.key} edge labels print at {edge_pt:.2f}pt"
        expected_arrowheads = sum(
            edge.direction is DiagramRelationDirection.DIRECTED for edge in diagram.edges
        )
        assert _directed_arrowhead_count(first_svg) == expected_arrowheads
