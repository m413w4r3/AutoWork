"""Deterministic D2 source encoding for canonical diagrams."""

from __future__ import annotations

import hashlib
import json
import unicodedata
from typing import Any

from cti_app.application.diagram_compilation import UnsupportedDiagramStructureError
from cti_app.domain.production_editorial_enrichment import (
    DiagramSpecV1,
    EnrichmentDiagramDirection,
)

D2_SOURCE_FORMAT = "d2"
D2_SOURCE_ENCODING = "utf-8"
D2_COMPILER_VERSION = "0.9.0"
D2_COMPILER_POLICY_VERSION = "diagram-d2-svg-v1"

_D2_DIRECTION_BY_V1 = {
    EnrichmentDiagramDirection.LEFT_TO_RIGHT: "right",
    EnrichmentDiagramDirection.TOP_TO_BOTTOM: "down",
}
_LABEL_ESCAPES = {
    "\\": "\\\\",
    "'": "\\'",
    "\n": "\\n",
    "\r": "\\r",
    "\t": "\\t",
    "\b": "\\b",
    "\f": "\\f",
}


def _ref_projection(ref: Any) -> dict[str, str]:
    return {
        "source_document_id": str(ref.source_document_id),
        "kind": ref.kind.value,
        "evidence_key": ref.evidence_key,
    }


def _semantic_projection(diagram: DiagramSpecV1) -> dict[str, Any]:
    return {
        "key": diagram.key,
        "kind": diagram.kind.value,
        "title": diagram.title,
        "caption": diagram.caption,
        "direction": diagram.direction.value,
        "nodes": [
            {
                "node_id": node.node_id,
                "label": node.label,
                "evidence_refs": [_ref_projection(ref) for ref in node.evidence_refs],
            }
            for node in diagram.nodes
        ],
        "edges": [
            {
                "source_node_id": edge.source_node_id,
                "target_node_id": edge.target_node_id,
                "label": edge.label,
                "evidence_refs": [_ref_projection(ref) for ref in edge.evidence_refs],
            }
            for edge in diagram.edges
        ],
        "groups": [
            {"group_id": group.group_id, "label": group.label, "node_ids": list(group.node_ids)}
            for group in diagram.groups
        ],
        "placement": {
            "kind": diagram.placement.kind.value,
            "section_index": diagram.placement.section_index,
        },
    }


def diagram_semantic_sha256(diagram: DiagramSpecV1) -> str:
    """Hash every canonical diagram field as deterministic JSON."""
    canonical_json = json.dumps(
        _semantic_projection(diagram), sort_keys=True, separators=(",", ":")
    ).encode(D2_SOURCE_ENCODING)
    return hashlib.sha256(canonical_json).hexdigest()


def _escape_d2_label(value: str) -> str:
    escaped: list[str] = []
    for char in value:
        replacement = _LABEL_ESCAPES.get(char)
        if replacement is not None:
            escaped.append(replacement)
        elif unicodedata.category(char) in {"Cc", "Cs", "Zl", "Zp"}:
            escaped.append(f"\\u{ord(char):04x}")
        else:
            escaped.append(char)
    return "'" + "".join(escaped) + "'"


def _node_references(diagram: DiagramSpecV1) -> dict[str, str]:
    node_ids = {node.node_id for node in diagram.nodes}
    group_ids: dict[str, str] = {}
    memberships: dict[str, str] = {}
    for group_index, group in enumerate(diagram.groups, 1):
        if not group.node_ids:
            raise UnsupportedDiagramStructureError("diagram groups must contain nodes")
        if group.group_id in group_ids:
            raise UnsupportedDiagramStructureError("diagram group identifiers must be unique")
        group_ids[group.group_id] = f"g{group_index:03d}"
        for node_id in group.node_ids:
            if node_id not in node_ids:
                raise UnsupportedDiagramStructureError(
                    "diagram groups must reference existing nodes"
                )
            if node_id in memberships:
                raise UnsupportedDiagramStructureError(
                    "diagram nodes cannot belong to multiple groups"
                )
            memberships[node_id] = group_ids[group.group_id]

    references: dict[str, str] = {}
    for node_index, node in enumerate(diagram.nodes, 1):
        node_reference = f"n{node_index:03d}"
        group_reference = memberships.get(node.node_id)
        references[node.node_id] = (
            f"{group_reference}.{node_reference}" if group_reference else node_reference
        )
    return references


def encode_d2_source(diagram: DiagramSpecV1) -> bytes:
    """Encode the canonical graph using only synthetic D2 identifiers."""
    node_references = _node_references(diagram)
    lines = [f"direction: {_D2_DIRECTION_BY_V1[diagram.direction]}"]

    for group_index, group in enumerate(diagram.groups, 1):
        lines.append(f"g{group_index:03d}: {_escape_d2_label(group.label)} {{}}")

    for node in diagram.nodes:
        lines.append(f"{node_references[node.node_id]}: {_escape_d2_label(node.label)}")

    for edge in diagram.edges:
        line = f"{node_references[edge.source_node_id]} -> {node_references[edge.target_node_id]}"
        if edge.label is not None:
            line += f": {_escape_d2_label(edge.label)}"
        lines.append(line)

    return ("\n".join(lines) + "\n").encode(D2_SOURCE_ENCODING)
