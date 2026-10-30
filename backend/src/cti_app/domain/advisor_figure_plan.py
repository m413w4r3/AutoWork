"""Strict semantic Bridge figure plans for deterministic local renderers."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class FigureEvidenceLevel(StrEnum):
    OBSERVED = "observed"
    DOCUMENTED = "documented"
    INFERRED = "inferred"
    HYPOTHESIS = "hypothesis"


class FigureDirection(StrEnum):
    A_TO_B = "a_to_b"
    B_TO_A = "b_to_a"
    BIDIRECTIONAL = "bidirectional"


@dataclass(frozen=True, slots=True)
class FigureEvidenceRefV1:
    kind: str
    id: str

    def __post_init__(self) -> None:
        if not isinstance(self.kind, str) or self.kind not in {
            "observation",
            "finding",
            "experiment",
            "article",
        }:
            raise ValueError("Figure evidence reference kind is invalid")
        if not isinstance(self.id, str) or not self.id:
            raise ValueError("Figure evidence reference ID is invalid")

    def to_payload(self) -> dict[str, str]:
        return {"kind": self.kind, "id": self.id}


@dataclass(frozen=True, slots=True)
class FigureNodeV1:
    id: str
    label: str
    refs: tuple[FigureEvidenceRefV1, ...]

    def __post_init__(self) -> None:
        if re.fullmatch(r"[a-z][a-z0-9_-]*", self.id) is None:
            raise ValueError("Figure node ID is invalid")
        if not isinstance(self.label, str) or not self.label.strip():
            raise ValueError("Figure node label is invalid")
        object.__setattr__(self, "refs", tuple(self.refs))
        if not self.refs or any(
            not isinstance(ref, FigureEvidenceRefV1) for ref in self.refs
        ):
            raise ValueError("Figure nodes require typed evidence references")

    def to_payload(self) -> dict[str, Any]:
        return {"id": self.id, "label": self.label, "refs": [ref.to_payload() for ref in self.refs]}


@dataclass(frozen=True, slots=True)
class FigureEdgeV1:
    id: str
    source: str
    target: str
    label: str
    refs: tuple[FigureEvidenceRefV1, ...]
    direction: FigureDirection | None = None
    direction_refs: tuple[FigureEvidenceRefV1, ...] = ()

    def __post_init__(self) -> None:
        if re.fullmatch(r"[a-z][a-z0-9_-]*", self.id) is None:
            raise ValueError("Figure edge ID is invalid")
        if any(
            re.fullmatch(r"[a-z][a-z0-9_-]*", item) is None
            for item in (self.source, self.target)
        ):
            raise ValueError("Figure edge endpoint is invalid")
        if not isinstance(self.label, str) or not self.label.strip():
            raise ValueError("Figure edge label is invalid")
        object.__setattr__(self, "refs", tuple(self.refs))
        object.__setattr__(self, "direction_refs", tuple(self.direction_refs))
        if not self.refs or any(
            not isinstance(ref, FigureEvidenceRefV1) for ref in self.refs
        ):
            raise ValueError("Figure edges require typed evidence references")
        if self.direction is not None and not isinstance(self.direction, FigureDirection):
            raise TypeError("Figure edge direction must use FigureDirection")
        if self.direction is None and self.direction_refs:
            raise ValueError("Undirected figure edges cannot carry direction references")
        if self.direction is not None and not self.direction_refs:
            raise ValueError("Directed figure edges require evidence for their direction")
        if any(not isinstance(ref, FigureEvidenceRefV1) for ref in self.direction_refs):
            raise ValueError("Figure edge direction references are invalid")

    def to_payload(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "source": self.source,
            "target": self.target,
            "label": self.label,
            "refs": [ref.to_payload() for ref in self.refs],
            "direction": self.direction.value if self.direction is not None else None,
            "direction_refs": [ref.to_payload() for ref in self.direction_refs],
        }


@dataclass(frozen=True, slots=True)
class FigureLegendV1:
    evidence_level: FigureEvidenceLevel
    text: str

    def __post_init__(self) -> None:
        if not isinstance(self.evidence_level, FigureEvidenceLevel):
            raise TypeError("Figure legend requires one evidence level")
        if not isinstance(self.text, str) or not self.text.strip():
            raise ValueError("Figure legend text is invalid")

    def to_payload(self) -> dict[str, str]:
        return {"evidence_level": self.evidence_level.value, "text": self.text}


@dataclass(frozen=True, slots=True)
class AdvisorFigurePlanV1:
    title: str
    legend: FigureLegendV1
    nodes: tuple[FigureNodeV1, ...]
    edges: tuple[FigureEdgeV1, ...]
    schema: str = "advisor-figure-plan.v1"

    def __post_init__(self) -> None:
        if self.schema != "advisor-figure-plan.v1":
            raise ValueError("Figure plan schema is invalid")
        if not isinstance(self.title, str) or not self.title.strip():
            raise ValueError("Figure plan title is invalid")
        if not isinstance(self.legend, FigureLegendV1):
            raise TypeError("Figure plan legend is invalid")
        object.__setattr__(self, "nodes", tuple(self.nodes))
        object.__setattr__(self, "edges", tuple(self.edges))
        if not self.nodes:
            raise ValueError("Figure plan requires at least one node")
        if any(not isinstance(item, FigureNodeV1) for item in self.nodes):
            raise TypeError("Figure plan nodes are invalid")
        if any(not isinstance(item, FigureEdgeV1) for item in self.edges):
            raise TypeError("Figure plan edges are invalid")
        node_ids = {item.id for item in self.nodes}
        if len(node_ids) != len(self.nodes):
            raise ValueError("Figure node IDs must be unique")
        edge_ids = {item.id for item in self.edges}
        if len(edge_ids) != len(self.edges):
            raise ValueError("Figure edge IDs must be unique")
        if any(item.source not in node_ids or item.target not in node_ids for item in self.edges):
            raise ValueError("Figure edge endpoints must name declared nodes")

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "title": self.title,
            "legend": self.legend.to_payload(),
            "nodes": [item.to_payload() for item in self.nodes],
            "edges": [item.to_payload() for item in self.edges],
        }

    @classmethod
    def from_payload(cls, value: object) -> AdvisorFigurePlanV1:
        if not isinstance(value, dict) or set(value) != {
            "schema",
            "title",
            "legend",
            "nodes",
            "edges",
        }:
            raise ValueError("Figure plan has missing or unknown fields")
        if not isinstance(value["title"], str) or not isinstance(value["legend"], dict):
            raise ValueError("Figure plan title or legend is invalid")
        legend = value["legend"]
        if set(legend) != {"evidence_level", "text"} or not isinstance(legend["text"], str):
            raise ValueError("Figure plan legend has missing or unknown fields")
        raw_nodes, raw_edges = value["nodes"], value["edges"]
        if not isinstance(raw_nodes, list) or not isinstance(raw_edges, list):
            raise ValueError("Figure plan nodes and edges must be lists")
        nodes = tuple(_node_from_payload(item) for item in raw_nodes)
        edges = tuple(_edge_from_payload(item) for item in raw_edges)
        return cls(
            schema=value["schema"],
            title=value["title"],
            legend=FigureLegendV1(FigureEvidenceLevel(legend["evidence_level"]), legend["text"]),
            nodes=nodes,
            edges=edges,
        )

    def require_transmitted_refs(self, allowed_refs: set[tuple[str, str]]) -> None:
        for ref in (
            *(ref for node in self.nodes for ref in node.refs),
            *(ref for edge in self.edges for ref in (*edge.refs, *edge.direction_refs)),
        ):
            if (ref.kind, ref.id) not in allowed_refs:
                raise ValueError("Figure plan cites a reference that was not transmitted")


def _refs_from_payload(value: object) -> tuple[FigureEvidenceRefV1, ...]:
    if not isinstance(value, list):
        raise ValueError("Figure evidence references must be a list")
    refs = []
    for item in value:
        if not isinstance(item, dict) or set(item) != {"kind", "id"}:
            raise ValueError("Figure evidence reference has missing or unknown fields")
        refs.append(FigureEvidenceRefV1(item["kind"], item["id"]))
    return tuple(refs)


def _node_from_payload(value: object) -> FigureNodeV1:
    if not isinstance(value, dict) or set(value) != {"id", "label", "refs"}:
        raise ValueError("Figure node has missing or unknown fields")
    return FigureNodeV1(value["id"], value["label"], _refs_from_payload(value["refs"]))


def _edge_from_payload(value: object) -> FigureEdgeV1:
    expected = {"id", "source", "target", "label", "refs", "direction", "direction_refs"}
    if not isinstance(value, dict) or set(value) != expected:
        raise ValueError("Figure edge has missing or unknown fields")
    direction = FigureDirection(value["direction"]) if value["direction"] is not None else None
    return FigureEdgeV1(
        id=value["id"],
        source=value["source"],
        target=value["target"],
        label=value["label"],
        refs=_refs_from_payload(value["refs"]),
        direction=direction,
        direction_refs=_refs_from_payload(value["direction_refs"]),
    )


_FIGURE_REF_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "kind": {"enum": ["observation", "finding", "experiment", "article"]},
        "id": {"type": "string", "minLength": 1},
    },
    "required": ["kind", "id"],
    "additionalProperties": False,
}
_FIGURE_NODE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "id": {"type": "string", "pattern": r"^[a-z][a-z0-9_-]*$"},
        "label": {"type": "string", "minLength": 1},
        "refs": {
            "type": "array",
            "minItems": 1,
            "items": _FIGURE_REF_SCHEMA,
        },
    },
    "required": ["id", "label", "refs"],
    "additionalProperties": False,
}
_FIGURE_EDGE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "id": {"type": "string", "pattern": r"^[a-z][a-z0-9_-]*$"},
        "source": {"type": "string", "pattern": r"^[a-z][a-z0-9_-]*$"},
        "target": {"type": "string", "pattern": r"^[a-z][a-z0-9_-]*$"},
        "label": {"type": "string", "minLength": 1},
        "refs": {
            "type": "array",
            "minItems": 1,
            "items": _FIGURE_REF_SCHEMA,
        },
        "direction": {
            "enum": [None, *(item.value for item in FigureDirection)],
        },
        "direction_refs": {
            "type": "array",
            "items": _FIGURE_REF_SCHEMA,
        },
    },
    "required": ["id", "source", "target", "label", "refs", "direction", "direction_refs"],
    "additionalProperties": False,
}
ADVISOR_FIGURE_PLAN_SCHEMA_V1: dict[str, Any] = {
    "type": "object",
    "properties": {
        "schema": {"const": "advisor-figure-plan.v1"},
        "title": {"type": "string", "minLength": 1},
        "legend": {
            "type": "object",
            "properties": {
                "evidence_level": {"enum": [item.value for item in FigureEvidenceLevel]},
                "text": {"type": "string", "minLength": 1},
            },
            "required": ["evidence_level", "text"],
            "additionalProperties": False,
        },
        "nodes": {
            "type": "array",
            "minItems": 1,
            "items": _FIGURE_NODE_SCHEMA,
        },
        "edges": {"type": "array", "items": _FIGURE_EDGE_SCHEMA},
    },
    "required": ["schema", "title", "legend", "nodes", "edges"],
    "additionalProperties": False,
}
