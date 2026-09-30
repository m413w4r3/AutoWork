from __future__ import annotations

from dataclasses import fields, replace
from uuid import UUID

import pytest

from cti_app.application.diagram_compilation import UnsupportedDiagramStructureError
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
from cti_app.infrastructure.d2_diagram_compiler import (
    diagram_semantic_sha256,
    encode_d2_source,
)

_EVIDENCE = ExtractionEvidenceRefV1(
    source_document_id=UUID("00000000-0000-0000-0000-000000000001"),
    kind=EvidenceKind.FACT,
    evidence_key="a" * 64,
)


def _node(node_id: str, label: str | None = None) -> DiagramNodeV1:
    return DiagramNodeV1(node_id=node_id, label=label or node_id, evidence_refs=(_EVIDENCE,))


def _edge(source: str, target: str, label: str | None = None) -> DiagramEdgeV1:
    return DiagramEdgeV1(
        source_node_id=source,
        target_node_id=target,
        label=label,
        evidence_refs=(_EVIDENCE,),
    )


def _diagram(**overrides: object) -> DiagramSpecV1:
    fields: dict[str, object] = {
        "key": "diagram-main",
        "kind": EnrichmentDiagramKind.CUSTOM,
        "title": "Ignored title",
        "caption": "Ignored caption",
        "direction": EnrichmentDiagramDirection.LEFT_TO_RIGHT,
        "nodes": (_node("source"), _node("target")),
        "edges": (_edge("source", "target", "connects"),),
        "groups": (),
        "placement": EnrichmentPlacementV1(EnrichmentPlacementKind.END),
    }
    fields.update(overrides)
    return DiagramSpecV1(**fields)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("direction", "d2_direction"),
    (
        (EnrichmentDiagramDirection.LEFT_TO_RIGHT, "right"),
        (EnrichmentDiagramDirection.TOP_TO_BOTTOM, "down"),
    ),
)
def test_encodes_each_direction_and_ends_with_one_lf(
    direction: EnrichmentDiagramDirection, d2_direction: str
) -> None:
    source = encode_d2_source(_diagram(direction=direction))

    assert source.startswith(f"direction: {d2_direction}\n".encode())
    assert source.endswith(b"\n")
    assert not source.endswith(b"\n\n")


def test_preserves_tuple_order_and_qualifies_grouped_nodes() -> None:
    diagram = _diagram(
        nodes=(_node("third"), _node("first"), _node("second")),
        groups=(
            DiagramGroupV1("user-group-z", "Group Z", ("second",)),
            DiagramGroupV1("user-group-a", "Group A", ("third",)),
        ),
        edges=(
            _edge("third", "first", "first edge"),
            _edge("first", "second"),
            _edge("second", "third", "third edge"),
        ),
    )

    assert encode_d2_source(diagram).decode().splitlines() == [
        "direction: right",
        "g001: 'Group Z' {}",
        "g002: 'Group A' {}",
        "g002.n001: 'third'",
        "n002: 'first'",
        "g001.n003: 'second'",
        "g002.n001 -> n002: 'first edge'",
        "n002 -> g001.n003",
        "g001.n003 -> g002.n001: 'third edge'",
    ]


def test_encodes_two_directed_edges_in_tuple_order() -> None:
    diagram = _diagram(
        edges=(_edge("source", "target", "forward"), _edge("target", "source", "return")),
    )

    assert encode_d2_source(diagram).decode().splitlines()[-2:] == [
        "n001 -> n002: 'forward'",
        "n002 -> n001: 'return'",
    ]


def test_escapes_hostile_labels_as_single_quoted_content() -> None:
    node_label = "Unicode café 雪; quotes \" and ' backslash \\\nnext\t{ } : # -> <- | $ ${D2_VAR}"
    group_label = '@import("https://example.test") ![icon](image.png)'
    edge_label = "<- ${CONFIG}; | -> ; link: https://example.test 'end'"
    diagram = _diagram(
        nodes=(_node("model -> n001", node_label), _node("target")),
        edges=(_edge("model -> n001", "target", edge_label),),
        groups=(DiagramGroupV1("model-group", group_label, ("model -> n001",)),),
    )

    source = encode_d2_source(diagram).decode()
    assert f"g001: '{group_label}' {{}}" in source
    node_source_line = next(line for line in source.splitlines() if "café" in line)
    assert node_source_line.startswith("g001.n001: 'Unicode café 雪; quotes \" and \\'")
    assert node_source_line.endswith("{ } : # -> <- | $ ${D2_VAR}'")
    assert ("backslash " + "\\\\" * node_label.count("\\") + "\\nnext\\t") in node_source_line
    assert (
        "g001.n001 -> n002: '<- ${CONFIG}; | -> ; link: https://example.test \\'end\\''" in source
    )
    assert "\nimport " not in source
    assert "\nhttps://" not in source


def test_omits_only_none_edge_labels() -> None:
    diagram = _diagram(edges=(_edge("source", "target"),))

    assert encode_d2_source(diagram).decode().splitlines()[-1] == "n001 -> n002"


def test_encoding_and_semantic_hash_are_stable_and_key_sensitive() -> None:
    diagram = _diagram()

    assert encode_d2_source(diagram) == encode_d2_source(diagram)
    assert diagram_semantic_sha256(diagram) == diagram_semantic_sha256(diagram)
    assert diagram_semantic_sha256(diagram) != diagram_semantic_sha256(
        replace(diagram, key="diagram-other")
    )


@pytest.mark.parametrize(
    "invalid_groups",
    (
        (
            DiagramGroupV1("group-a", "A", ("source",)),
            DiagramGroupV1("group-b", "B", ("source",)),
        ),
        (),
    ),
)
def test_rejects_ambiguous_or_empty_group_membership(
    invalid_groups: tuple[DiagramGroupV1, ...],
) -> None:
    if not invalid_groups:
        empty_group = object.__new__(DiagramGroupV1)
        object.__setattr__(empty_group, "group_id", "empty")
        object.__setattr__(empty_group, "label", "Empty")
        object.__setattr__(empty_group, "node_ids", ())
        invalid_groups = (empty_group,)

    diagram = object.__new__(DiagramSpecV1)
    valid_diagram = _diagram()
    for field in fields(valid_diagram):
        object.__setattr__(diagram, field.name, getattr(valid_diagram, field.name))
    object.__setattr__(diagram, "groups", invalid_groups)

    with pytest.raises(UnsupportedDiagramStructureError):
        encode_d2_source(diagram)
