"""Validate semantic figure plans and project them to deterministic D2 input."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Protocol

from cti_app.domain.production_editorial_enrichment import (
    DiagramEdgeV1,
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
from cti_app.domain.production_synthesis import ExtractionEvidenceRefV1

FIGURE_PLAN_SCHEMA_VERSION = "figure_plan.v1"
FIGURE_PLAN_PROJECTION_VERSION = "figure-plan-projection.v1"
_NODE_ID = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_UUID_OR_HASH = re.compile(
    r"(?:\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b"
    r"|\b[0-9a-f]{32,64}\b)",
    re.IGNORECASE,
)
_PROOF_MARKER = re.compile(
    r"\b(?:ARTICLE_DOCUMENTED|STATIC_INFERENCE|SANDBOX_CAPTURE_OBSERVED|"
    r"VERIFIED_ON_HOLDOUT|DOCUMENTED_NOT_OBSERVED|DOCUMENTED\s*,\s*NOT OBSERVED|"
    r"DOCUMENTÉ\s*,\s*NON OBSERVÉ)\b",
    re.IGNORECASE,
)
_FRAME_TERM = re.compile(r"\b(?:frames?|trames?)\b", re.IGNORECASE)
_OFFSET_TERM = re.compile(r"\boffsets?\b", re.IGNORECASE)
_UNCERTAINTY_TERM = re.compile(
    r"\b(?:unknown|inconnu\w*|incertain\w*|not observed|non observé\w*|"
    r"omitted|omise?\w*|absent\w*)\b",
    re.IGNORECASE,
)


class FigurePlanValidationError(ValueError):
    """A semantic figure plan is malformed or cites an unverified reference."""


class FigurePlanReferenceResolver(Protocol):
    """Resolve only references already verified against canonical investigation state."""

    def resolve_verified(self, reference: str) -> ExtractionEvidenceRefV1 | None: ...


@dataclass(frozen=True, slots=True)
class FigurePlanNodeV1:
    node_id: str
    label: str
    role: DiagramNodeRole
    refs: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.node_id, str) or _NODE_ID.fullmatch(self.node_id) is None:
            raise FigurePlanValidationError("figure_plan_node_id_invalid")
        _semantic_label(self.label, field="node label", maximum=56, proof_allowed=False)
        if not isinstance(self.role, DiagramNodeRole):
            raise FigurePlanValidationError("figure_plan_node_role_invalid")
        _reference_list(
            self.refs,
            field="node refs",
            required=self.role is not DiagramNodeRole.PROTOCOL_NOTE,
        )


@dataclass(frozen=True, slots=True)
class FigurePlanEdgeV1:
    source_node_id: str
    target_node_id: str
    label: str
    relation_type: DiagramRelationType
    refs: tuple[str, ...]
    direction: DiagramRelationDirection | None = None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.source_node_id, str)
            or _NODE_ID.fullmatch(self.source_node_id) is None
            or not isinstance(self.target_node_id, str)
            or _NODE_ID.fullmatch(self.target_node_id) is None
        ):
            raise FigurePlanValidationError("figure_plan_edge_endpoint_invalid")
        if self.source_node_id == self.target_node_id:
            raise FigurePlanValidationError("figure_plan_self_edge_invalid")
        _semantic_label(self.label, field="edge label", maximum=64, proof_allowed=False)
        if not isinstance(self.relation_type, DiagramRelationType):
            raise FigurePlanValidationError("figure_plan_relation_type_invalid")
        if self.direction is not None and not isinstance(self.direction, DiagramRelationDirection):
            raise FigurePlanValidationError("figure_plan_direction_invalid")
        if (
            self.relation_type is DiagramRelationType.COMPARISON
            and self.direction is DiagramRelationDirection.DIRECTED
        ):
            raise FigurePlanValidationError("figure_plan_comparison_must_be_undirected")
        _reference_list(self.refs, field="edge refs", required=True)


@dataclass(frozen=True, slots=True)
class FigurePlanV1:
    source: str
    source_ref: str
    title: str
    legend: str
    kind: EnrichmentDiagramKind
    nodes: tuple[FigurePlanNodeV1, ...]
    edges: tuple[FigurePlanEdgeV1, ...]
    refs: tuple[str, ...]
    direction: EnrichmentDiagramDirection = EnrichmentDiagramDirection.TOP_TO_BOTTOM
    schema_version: str = FIGURE_PLAN_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != FIGURE_PLAN_SCHEMA_VERSION:
            raise FigurePlanValidationError("figure_plan_schema_version_unsupported")
        if self.source != "bridge" or not _short_reference(self.source_ref):
            raise FigurePlanValidationError("figure_plan_bridge_provenance_invalid")
        _semantic_label(self.title, field="title", maximum=72, proof_allowed=False)
        _semantic_label(self.legend, field="legend", maximum=480, proof_allowed=True)
        proof_markers = tuple(
            re.sub(r"\s+", " ", marker).casefold() for marker in _PROOF_MARKER.findall(self.legend)
        )
        if (
            self.legend.casefold().count("niveau de preuve") != 1
            or not proof_markers
            or len(proof_markers) != len(set(proof_markers))
        ):
            raise FigurePlanValidationError("figure_plan_proof_legend_invalid")
        if (
            _FRAME_TERM.search(self.legend) is None
            or _OFFSET_TERM.search(self.legend) is None
            or _UNCERTAINTY_TERM.search(self.legend) is None
        ):
            raise FigurePlanValidationError("figure_plan_legend_uncertainty_missing")
        if not isinstance(self.kind, EnrichmentDiagramKind):
            raise FigurePlanValidationError("figure_plan_kind_invalid")
        if not isinstance(self.direction, EnrichmentDiagramDirection):
            raise FigurePlanValidationError("figure_plan_layout_direction_invalid")
        if not 1 <= len(self.nodes) <= 16 or len(self.edges) > 24:
            raise FigurePlanValidationError("figure_plan_size_out_of_bounds")
        if not all(isinstance(node, FigurePlanNodeV1) for node in self.nodes):
            raise FigurePlanValidationError("figure_plan_node_invalid")
        if not all(isinstance(edge, FigurePlanEdgeV1) for edge in self.edges):
            raise FigurePlanValidationError("figure_plan_edge_invalid")
        if len({node.node_id for node in self.nodes}) != len(self.nodes):
            raise FigurePlanValidationError("figure_plan_duplicate_node_id")
        node_ids = {node.node_id for node in self.nodes}
        if any(
            edge.source_node_id not in node_ids or edge.target_node_id not in node_ids
            for edge in self.edges
        ):
            raise FigurePlanValidationError("figure_plan_edge_endpoint_missing")
        _reference_list(self.refs, field="plan refs", required=True)
        referenced = {reference for node in self.nodes for reference in node.refs}
        referenced.update(reference for edge in self.edges for reference in edge.refs)
        if not referenced.issubset(set(self.refs)):
            raise FigurePlanValidationError("figure_plan_refs_incomplete")


def project_figure_plan(
    plan: FigurePlanV1,
    *,
    evidence_resolver: FigurePlanReferenceResolver,
) -> DiagramSpecV1:
    """Resolve verified refs and convert the semantic plan into deterministic D2 input.

    Missing edge direction is deliberately rendered as an undirected relation. A Bridge
    proposal cannot create an arrow unless it supplies a direction and verified evidence.
    """
    if not isinstance(plan, FigurePlanV1):
        raise TypeError("Figure plan projection requires FigurePlanV1")
    verified: dict[str, ExtractionEvidenceRefV1] = {}
    for reference in plan.refs:
        resolved = evidence_resolver.resolve_verified(reference)
        if not isinstance(resolved, ExtractionEvidenceRefV1):
            raise FigurePlanValidationError("figure_plan_reference_unverified")
        verified[reference] = resolved

    nodes = tuple(
        DiagramNodeV1(
            node_id=node.node_id,
            label=node.label,
            evidence_refs=tuple(verified[reference] for reference in node.refs),
            role=node.role,
        )
        for node in plan.nodes
    )
    edges = tuple(
        DiagramEdgeV1(
            source_node_id=edge.source_node_id,
            target_node_id=edge.target_node_id,
            label=edge.label,
            evidence_refs=tuple(verified[reference] for reference in edge.refs),
            relation_type=edge.relation_type,
            direction=edge.direction or DiagramRelationDirection.UNDIRECTED,
        )
        for edge in plan.edges
    )
    identity = {
        "schema_version": plan.schema_version,
        "projection_version": FIGURE_PLAN_PROJECTION_VERSION,
        "source": plan.source,
        "source_ref": plan.source_ref,
        "title": plan.title,
        "legend": plan.legend,
        "kind": plan.kind.value,
        "direction": plan.direction.value,
        "nodes": [
            {"id": node.node_id, "label": node.label, "role": node.role.value, "refs": node.refs}
            for node in plan.nodes
        ],
        "edges": [
            {
                "source": edge.source_node_id,
                "target": edge.target_node_id,
                "label": edge.label,
                "relation_type": edge.relation_type.value,
                "direction": edge.direction.value if edge.direction else None,
                "refs": edge.refs,
            }
            for edge in plan.edges
        ],
        "refs": sorted(plan.refs),
    }
    digest = hashlib.sha256(
        json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()
    return DiagramSpecV1(
        key=f"figure-plan-v1-{digest[:24]}",
        kind=plan.kind,
        title=plan.title,
        caption=plan.legend,
        direction=plan.direction,
        nodes=nodes,
        edges=edges,
        groups=(),
        placement=EnrichmentPlacementV1(EnrichmentPlacementKind.END),
        profile=(
            DiagramProfile.ARCHITECTURE
            if plan.kind is EnrichmentDiagramKind.INFRASTRUCTURE
            else DiagramProfile.FLOW
        ),
    )


def _reference_list(refs: tuple[str, ...], *, field: str, required: bool) -> None:
    if not isinstance(refs, tuple) or (required and not refs) or len(refs) > 16:
        raise FigurePlanValidationError(f"figure_plan_{field.replace(' ', '_')}_invalid")
    if any(not _short_reference(item) for item in refs) or len(refs) != len(set(refs)):
        raise FigurePlanValidationError(f"figure_plan_{field.replace(' ', '_')}_invalid")


def _short_reference(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip()) and len(value) <= 160


def _semantic_label(
    value: object,
    *,
    field: str,
    maximum: int,
    proof_allowed: bool,
) -> None:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise FigurePlanValidationError(f"figure_plan_{field.replace(' ', '_')}_invalid")
    if any(ord(char) < 32 and char not in "\n\t" for char in value):
        raise FigurePlanValidationError(f"figure_plan_{field.replace(' ', '_')}_invalid")
    if _UUID_OR_HASH.search(value):
        raise FigurePlanValidationError(
            f"figure_plan_{field.replace(' ', '_')}_contains_identifier"
        )
    if not proof_allowed and _PROOF_MARKER.search(value):
        raise FigurePlanValidationError(
            f"figure_plan_{field.replace(' ', '_')}_repeats_proof_level"
        )
