"""Safe, deterministic projections from protocol semantics to editorial D2 diagrams."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from itertools import pairwise
from typing import Protocol, cast
from uuid import UUID

from cti_app.application.diagram_compilation import (
    DIAGRAM_COMPILATION_POLICY_VERSION,
    CompiledDiagram,
    CompiledDiagramPreview,
    DiagramCompiler,
)
from cti_app.application.media_assets import MediaAssetStore
from cti_app.domain.analyst_opinion import (
    OpinionProtocolFieldEvidenceV1,
    OpinionProtocolFigureV1,
)
from cti_app.domain.media_assets import MediaAssetManifest
from cti_app.domain.production_editorial_enrichment import (
    DiagramEdgeV1,
    DiagramNodeRole,
    DiagramNodeV1,
    DiagramProfile,
    DiagramRelationType,
    DiagramSpecV1,
    EnrichmentDiagramDirection,
    EnrichmentDiagramKind,
    EnrichmentPlacementKind,
    EnrichmentPlacementV1,
)
from cti_app.domain.protocol_model import (
    ProtocolDiagramEdgeV1,
    ProtocolDiagramFieldV1,
    ProtocolDiagramKind,
    ProtocolDiagramNodeV1,
    ProtocolDiagramSpecV1,
    ProtocolDiagramStatus,
    ProtocolEvidenceRefV1,
    ProtocolRenderResultV1,
    ProtocolValidationStatus,
)

PROTOCOL_DIAGRAM_PROJECTION_VERSION = "protocol-diagram-projection-v1"
_MAX_FIELD_EVIDENCE_REFS = 8
_MAX_FIELD_EVIDENCE_FRAMES = 12
_STATUS_LABELS = {
    ProtocolValidationStatus.OBSERVED: "OBSERVED",
    ProtocolValidationStatus.INFERRED: "INFERRED",
    ProtocolValidationStatus.UNRESOLVED: "UNRESOLVED",
    ProtocolValidationStatus.CONTRADICTED: "CONTRADICTED",
}
_PROFILES = {
    ProtocolDiagramKind.SEQUENCE: DiagramProfile.PROTOCOL_SEQUENCE,
    ProtocolDiagramKind.STATE_MACHINE: DiagramProfile.PROTOCOL_STATE_MACHINE,
    ProtocolDiagramKind.MESSAGE_FORMAT: DiagramProfile.PROTOCOL_MESSAGE_FORMAT,
}
_KINDS = {
    ProtocolDiagramKind.SEQUENCE: EnrichmentDiagramKind.NETWORK_FLOW,
    ProtocolDiagramKind.STATE_MACHINE: EnrichmentDiagramKind.EXECUTION_SEQUENCE,
    ProtocolDiagramKind.MESSAGE_FORMAT: EnrichmentDiagramKind.CUSTOM,
}
_DIRECTIONS = {
    ProtocolDiagramKind.SEQUENCE: EnrichmentDiagramDirection.TOP_TO_BOTTOM,
    ProtocolDiagramKind.STATE_MACHINE: EnrichmentDiagramDirection.LEFT_TO_RIGHT,
    ProtocolDiagramKind.MESSAGE_FORMAT: EnrichmentDiagramDirection.LEFT_TO_RIGHT,
}


class DiagramPreviewCompiler(Protocol):
    async def compile_preview(self, diagram: DiagramSpecV1) -> CompiledDiagramPreview: ...


class ProtocolDiagramProjectionError(ValueError):
    """Raised when a semantic protocol diagram cannot be safely projected."""


@dataclass(frozen=True, slots=True)
class ProtocolDiagramRenderV1:
    spec: ProtocolDiagramSpecV1
    diagram: DiagramSpecV1
    compiled: CompiledDiagram
    preview: CompiledDiagramPreview
    media_asset: MediaAssetManifest
    model_sha256: str
    spec_sha256: str
    pin_sha256: str


@dataclass(frozen=True, slots=True)
class ProtocolDiagramPreviewV1:
    """Transient SVG/PNG output for an HTTP preview; stores no media assets."""

    spec: ProtocolDiagramSpecV1
    diagram: DiagramSpecV1
    compiled: CompiledDiagram
    preview: CompiledDiagramPreview
    model_sha256: str
    spec_sha256: str


def project_protocol_diagram(spec: ProtocolDiagramSpecV1) -> DiagramSpecV1:
    """Convert one validated protocol view into the existing safe D2 input model."""
    if not isinstance(spec, ProtocolDiagramSpecV1):
        raise TypeError("Protocol diagram projection requires ProtocolDiagramSpecV1")
    profile = _PROFILES[spec.kind]
    nodes: list[DiagramNodeV1] = []
    edges: list[DiagramEdgeV1] = []

    if spec.status is ProtocolDiagramStatus.INSUFFICIENT_EVIDENCE:
        nodes.append(
            DiagramNodeV1(
                node_id="evidence-gap",
                label="INSUFFICIENT EVIDENCE · No protocol scenario established",
                evidence_refs=(),
                role=DiagramNodeRole.PROTOCOL_NOTE,
            )
        )
    elif spec.kind is ProtocolDiagramKind.SEQUENCE:
        nodes.extend(_project_participant(node) for node in spec.nodes)
        edges.extend(_project_message(edge) for edge in _ordered_edges(spec.edges))
    elif spec.kind is ProtocolDiagramKind.STATE_MACHINE:
        nodes.extend(_project_state(node) for node in spec.nodes)
        edges.extend(_project_transition(edge) for edge in spec.edges)
    else:
        if not spec.fields:
            raise ProtocolDiagramProjectionError(
                "A ready message-format spec must contain at least one field"
            )
        nodes.extend(_project_field(field, index) for index, field in enumerate(spec.fields, 1))
        edges.extend(_field_order_edges(spec.fields))
        nodes.append(
            DiagramNodeV1(
                node_id="derived-layout-example",
                label=(
                    "INFERRED · Derived layout example · metadata only · "
                    "encrypted payload bytes omitted"
                ),
                evidence_refs=(),
                role=DiagramNodeRole.PROTOCOL_NOTE,
            )
        )

    _validate_projected_graph(spec, nodes, edges)
    return DiagramSpecV1(
        key=f"protocol-{spec.kind.value}-{spec.canonical_sha256[:16]}",
        kind=_KINDS[spec.kind],
        title=spec.title,
        caption=protocol_diagram_caption(spec),
        direction=_DIRECTIONS[spec.kind],
        nodes=tuple(nodes),
        edges=tuple(edges),
        groups=(),
        placement=EnrichmentPlacementV1(EnrichmentPlacementKind.END),
        profile=profile,
    )


def protocol_diagram_caption(spec: ProtocolDiagramSpecV1) -> str:
    """Return the human-readable status legend and bounded semantic limitations."""
    legend = (
        "Legend: OBSERVED green; INFERRED orange and dashed; "
        "UNRESOLVED gray; CONTRADICTED red. Status is stated on each element."
    )
    if spec.status is ProtocolDiagramStatus.INSUFFICIENT_EVIDENCE:
        body = "Insufficient evidence; no generic sequence or schema was inserted."
    else:
        body = "Protocol view projected only from the supplied semantic evidence."
    limitations = tuple(item.strip() for item in spec.limitations if item.strip())
    if limitations:
        body = f"{body} Limits: {'; '.join(limitations)}"
    return f"{spec.title}. {body} {legend}"


def protocol_diagram_pin_sha256(
    spec: ProtocolDiagramSpecV1,
    *,
    compiler: str,
    compiler_version: str,
    compiler_policy_version: str = DIAGRAM_COMPILATION_POLICY_VERSION,
) -> str:
    """Hash the exact semantic spec together with the pinned D2 compiler identity."""
    payload = {
        "projection_version": PROTOCOL_DIAGRAM_PROJECTION_VERSION,
        "spec_sha256": spec.canonical_sha256,
        "compiler": compiler,
        "compiler_version": compiler_version,
        "compiler_policy_version": compiler_policy_version,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def opinion_protocol_figures(
    rendered: tuple[ProtocolDiagramRenderV1, ...],
) -> tuple[OpinionProtocolFigureV1, ...]:
    """Build immutable opinion attachments from all three compiled protocol views."""
    rendered_kinds = {item.spec.kind for item in rendered}
    if len(rendered) != len(ProtocolDiagramKind) or rendered_kinds != set(ProtocolDiagramKind):
        raise ProtocolDiagramProjectionError("Exactly three rendered protocol views are required")
    return tuple(
        OpinionProtocolFigureV1(
            kind=item.spec.kind,
            model_sha256=item.model_sha256,
            spec_sha256=item.spec_sha256,
            asset_id=item.media_asset.asset_id,
            asset_sha256=item.media_asset.sha256,
            preview_sha256=item.preview.media_sha256,
            pin_sha256=item.pin_sha256,
            compiler_name=item.compiled.compiler,
            compiler_version=item.compiled.compiler_version,
            compiler_policy_version=item.compiled.compiler_policy_version,
            caption=item.diagram.caption or item.spec.title,
            field_evidence=(
                tuple(
                    _field_evidence_metadata(field.field_id, field.evidence)
                    for field in item.spec.fields
                    if field.evidence
                )
                if item.spec.kind is ProtocolDiagramKind.MESSAGE_FORMAT
                else ()
            ),
        )
        for item in sorted(
            rendered, key=lambda item: tuple(ProtocolDiagramKind).index(item.spec.kind)
        )
    )


async def compile_protocol_render_result(
    result: ProtocolRenderResultV1,
    *,
    compiler: DiagramCompiler,
    media_asset_store: MediaAssetStore,
    production_run_id: UUID,
) -> tuple[ProtocolDiagramRenderV1, ...]:
    """Compile and content-address the three canonical views using the D2 pipeline."""
    if not isinstance(result, ProtocolRenderResultV1):
        raise TypeError("Protocol diagram rendering requires ProtocolRenderResultV1")
    specs = result.diagrams
    if len(specs) != len(ProtocolDiagramKind) or {item.kind for item in specs} != set(
        ProtocolDiagramKind
    ):
        raise ProtocolDiagramProjectionError("Exactly one spec for each protocol view is required")
    preview_compiler = cast(DiagramPreviewCompiler, compiler)
    if not callable(getattr(preview_compiler, "compile_preview", None)):
        raise ProtocolDiagramProjectionError(
            "The configured D2 compiler does not export PNG previews"
        )

    rendered: list[ProtocolDiagramRenderV1] = []
    for spec in sorted(specs, key=lambda item: tuple(ProtocolDiagramKind).index(item.kind)):
        diagram = project_protocol_diagram(spec)
        compiled = await compiler.compile(diagram)
        preview = await preview_compiler.compile_preview(diagram)
        if compiled.diagram_key != diagram.key or preview.diagram_key != diagram.key:
            raise ProtocolDiagramProjectionError("D2 returned output for a different protocol view")
        if compiled.source_sha256 != preview.source_sha256:
            raise ProtocolDiagramProjectionError(
                "D2 SVG and PNG previews use different source bytes"
            )
        if (
            compiled.compiler != preview.compiler
            or compiled.compiler_version != preview.compiler_version
        ):
            raise ProtocolDiagramProjectionError("D2 SVG and PNG compiler identities differ")
        spec_sha256 = spec.canonical_sha256
        pin_sha256 = protocol_diagram_pin_sha256(
            spec,
            compiler=compiled.compiler,
            compiler_version=compiled.compiler_version,
            compiler_policy_version=compiled.compiler_policy_version,
        )
        manifest = await media_asset_store.store_diagram(
            compiled,
            source=(
                f"production_run:{production_run_id}:protocol:{spec.kind.value}:"
                f"spec:{spec_sha256}:pin:{pin_sha256}"
            ),
        )
        rendered.append(
            ProtocolDiagramRenderV1(
                spec=spec,
                diagram=diagram,
                compiled=compiled,
                preview=preview,
                media_asset=manifest,
                model_sha256=result.model_sha256,
                spec_sha256=spec_sha256,
                pin_sha256=pin_sha256,
            )
        )
    return tuple(rendered)


async def compile_protocol_render_preview(
    result: ProtocolRenderResultV1,
    *,
    compiler: DiagramCompiler,
) -> tuple[ProtocolDiagramPreviewV1, ...]:
    """Compile all three safe views for a GET preview without persisting assets."""
    if not isinstance(result, ProtocolRenderResultV1):
        raise TypeError("Protocol diagram preview requires ProtocolRenderResultV1")
    specs = result.diagrams
    if len(specs) != len(ProtocolDiagramKind) or {item.kind for item in specs} != set(
        ProtocolDiagramKind
    ):
        raise ProtocolDiagramProjectionError("Exactly one spec for each protocol view is required")
    preview_compiler = cast(DiagramPreviewCompiler, compiler)
    if not callable(getattr(preview_compiler, "compile_preview", None)):
        raise ProtocolDiagramProjectionError(
            "The configured D2 compiler does not export PNG previews"
        )

    previews: list[ProtocolDiagramPreviewV1] = []
    for spec in sorted(specs, key=lambda item: tuple(ProtocolDiagramKind).index(item.kind)):
        diagram = project_protocol_diagram(spec)
        compiled = await compiler.compile(diagram)
        preview = await preview_compiler.compile_preview(diagram)
        if compiled.diagram_key != diagram.key or preview.diagram_key != diagram.key:
            raise ProtocolDiagramProjectionError("D2 returned output for a different protocol view")
        if compiled.source_sha256 != preview.source_sha256:
            raise ProtocolDiagramProjectionError(
                "D2 SVG and PNG previews use different source bytes"
            )
        if (
            compiled.compiler != preview.compiler
            or compiled.compiler_version != preview.compiler_version
        ):
            raise ProtocolDiagramProjectionError("D2 SVG and PNG compiler identities differ")
        previews.append(
            ProtocolDiagramPreviewV1(
                spec=spec,
                diagram=diagram,
                compiled=compiled,
                preview=preview,
                model_sha256=result.model_sha256,
                spec_sha256=spec.canonical_sha256,
            )
        )
    return tuple(previews)


def _status(status: ProtocolValidationStatus) -> str:
    return _STATUS_LABELS[status]


def _status_label(status: ProtocolValidationStatus, text: str) -> str:
    return f"{_status(status)} · {text.strip()}" if text.strip() else _status(status)


def _project_participant(node: ProtocolDiagramNodeV1) -> DiagramNodeV1:
    return DiagramNodeV1(
        node_id=node.node_id,
        label=_status_label(node.validation_status, node.label),
        evidence_refs=(),
        role=DiagramNodeRole.PROTOCOL_PARTICIPANT,
    )


def _project_state(node: ProtocolDiagramNodeV1) -> DiagramNodeV1:
    return DiagramNodeV1(
        node_id=node.node_id,
        label=_status_label(node.validation_status, node.label),
        evidence_refs=(),
        role=DiagramNodeRole.PROTOCOL_STATE,
    )


def _project_message(edge: ProtocolDiagramEdgeV1) -> DiagramEdgeV1:
    details = [edge.label.strip()]
    if edge.order is not None:
        details.insert(0, f"order {edge.order}")
    if edge.frame_numbers:
        details.append("frames " + ", ".join(str(frame) for frame in edge.frame_numbers))
    if edge.timing_ms is not None:
        details.append(f"timing {edge.timing_ms} ms")
    return DiagramEdgeV1(
        source_node_id=edge.source_id,
        target_node_id=edge.target_id,
        label=_status_label(edge.validation_status, " · ".join(item for item in details if item)),
        evidence_refs=(),
        relation_type=(
            DiagramRelationType.PROTOCOL_INFERENCE
            if edge.validation_status is ProtocolValidationStatus.INFERRED
            else DiagramRelationType.PROTOCOL_FACTUAL
        ),
    )


def _ordered_edges(
    edges: tuple[ProtocolDiagramEdgeV1, ...],
) -> tuple[ProtocolDiagramEdgeV1, ...]:
    indexed = tuple(enumerate(edges))
    return tuple(
        edge
        for _, edge in sorted(
            indexed,
            key=lambda item: (
                item[1].order is None,
                item[1].order if item[1].order is not None else item[0],
                item[0],
            ),
        )
    )


def _project_transition(edge: ProtocolDiagramEdgeV1) -> DiagramEdgeV1:
    details = [edge.label.strip()]
    if edge.order is not None:
        details.insert(0, f"order {edge.order}")
    if edge.frame_numbers:
        details.append("frames " + ", ".join(str(frame) for frame in edge.frame_numbers))
    if edge.timing_ms is not None:
        details.append(f"timing {edge.timing_ms} ms")
    return DiagramEdgeV1(
        source_node_id=edge.source_id,
        target_node_id=edge.target_id,
        label=_status_label(edge.validation_status, " · ".join(item for item in details if item)),
        evidence_refs=(),
        relation_type=(
            DiagramRelationType.PROTOCOL_INFERENCE
            if edge.validation_status is ProtocolValidationStatus.INFERRED
            else DiagramRelationType.PROTOCOL_FACTUAL
        ),
    )


def _project_field(field: ProtocolDiagramFieldV1, index: int) -> DiagramNodeV1:
    if field.encrypted_opaque:
        field_name = "Opaque encrypted payload · bytes omitted"
    else:
        field_name = field.name.strip() or f"Field {index}"
    details = [
        f"offset {_known(field.offset)}",
        f"length {_known(field.length)}",
        f"endianness {_known_text(field.endianness)}",
        f"encoding {_known_text(field.encoding)}",
    ]
    if field.condition:
        details.append(f"condition {field.condition.strip()}")
    evidence_label = _field_evidence_label(field.evidence)
    if evidence_label:
        details.append(evidence_label)
    label = _status_label(field.validation_status, field_name + " · " + " · ".join(details))
    return DiagramNodeV1(
        node_id=f"protocol-field-{index:04d}",
        label=label,
        evidence_refs=(),
        role=DiagramNodeRole.PROTOCOL_FIELD,
    )


def _bounded_field_evidence(
    evidence: tuple[ProtocolEvidenceRefV1, ...],
) -> tuple[tuple[ProtocolEvidenceRefV1, ...], bool]:
    unique = tuple(dict.fromkeys(evidence))
    truncated = len(unique) > _MAX_FIELD_EVIDENCE_REFS
    bounded: list[ProtocolEvidenceRefV1] = []
    for reference in unique[:_MAX_FIELD_EVIDENCE_REFS]:
        frames = reference.frame_numbers[:_MAX_FIELD_EVIDENCE_FRAMES]
        if len(frames) != len(reference.frame_numbers):
            truncated = True
            reference = replace(reference, frame_numbers=frames)
        bounded.append(reference)
    return tuple(bounded), truncated


def _field_evidence_metadata(
    field_id: str,
    evidence: tuple[ProtocolEvidenceRefV1, ...],
) -> OpinionProtocolFieldEvidenceV1:
    bounded, truncated = _bounded_field_evidence(evidence)
    return OpinionProtocolFieldEvidenceV1(
        field_id=field_id,
        evidence_refs=bounded,
        truncated=truncated,
    )


def _field_evidence_label(evidence: tuple[ProtocolEvidenceRefV1, ...]) -> str:
    bounded, truncated = _bounded_field_evidence(evidence)
    citations: list[str] = []
    for reference in bounded:
        citation = f"capture {reference.capture_sha256[:12]}"
        if reference.frame_numbers:
            citation += " frames " + ", ".join(str(frame) for frame in reference.frame_numbers)
        elif reference.start_offset is not None and reference.end_offset is not None:
            citation += f" bytes {reference.start_offset}-{reference.end_offset}"
        citations.append(citation)
    if not citations:
        return ""
    suffix = " · additional evidence omitted" if truncated else ""
    return "evidence " + "; ".join(citations) + suffix


def _field_order_edges(fields: tuple[ProtocolDiagramFieldV1, ...]) -> list[DiagramEdgeV1]:
    positioned = [
        (index, field)
        for index, field in enumerate(fields, 1)
        if field.offset is not None and field.length is not None
    ]
    positioned.sort(key=lambda item: (item[1].offset, item[0]))
    edges: list[DiagramEdgeV1] = []
    for (left_index, left), (right_index, right) in pairwise(positioned):
        if left.offset is None or left.length is None or right.offset is None:
            continue
        if left.offset + left.length > right.offset:
            continue
        status = (
            ProtocolValidationStatus.OBSERVED
            if left.validation_status
            is right.validation_status
            is ProtocolValidationStatus.OBSERVED
            else ProtocolValidationStatus.INFERRED
        )
        edges.append(
            DiagramEdgeV1(
                source_node_id=f"protocol-field-{left_index:04d}",
                target_node_id=f"protocol-field-{right_index:04d}",
                label=_status_label(status, "field order"),
                evidence_refs=(),
                relation_type=(
                    DiagramRelationType.PROTOCOL_FACTUAL
                    if status is ProtocolValidationStatus.OBSERVED
                    else DiagramRelationType.PROTOCOL_INFERENCE
                ),
            )
        )
    return edges


def _known(value: int | None) -> str:
    return str(value) if value is not None else "unknown"


def _known_text(value: str | None) -> str:
    return value.strip() if value and value.strip() else "unknown"


def _validate_projected_graph(
    spec: ProtocolDiagramSpecV1,
    nodes: list[DiagramNodeV1],
    edges: list[DiagramEdgeV1],
) -> None:
    node_ids = [node.node_id for node in nodes]
    if len(node_ids) != len(set(node_ids)):
        raise ProtocolDiagramProjectionError("Protocol diagram has duplicate node identifiers")
    known = set(node_ids)
    if spec.kind in {ProtocolDiagramKind.SEQUENCE, ProtocolDiagramKind.STATE_MACHINE}:
        source_ids = {node.node_id for node in spec.nodes}
        if len(source_ids) != len(spec.nodes):
            raise ProtocolDiagramProjectionError("Protocol diagram has duplicate node identifiers")
        if any(
            edge.source_id not in source_ids or edge.target_id not in source_ids
            for edge in spec.edges
        ):
            raise ProtocolDiagramProjectionError("Protocol diagram edge refers to an unknown node")
    if any(edge.source_node_id not in known or edge.target_node_id not in known for edge in edges):
        raise ProtocolDiagramProjectionError("Projected diagram edge refers to an unknown node")


__all__ = [
    "PROTOCOL_DIAGRAM_PROJECTION_VERSION",
    "ProtocolDiagramPreviewV1",
    "ProtocolDiagramProjectionError",
    "ProtocolDiagramRenderV1",
    "compile_protocol_render_preview",
    "compile_protocol_render_result",
    "opinion_protocol_figures",
    "project_protocol_diagram",
    "protocol_diagram_caption",
    "protocol_diagram_pin_sha256",
]
