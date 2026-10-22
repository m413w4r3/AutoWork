"""Safe, deterministic projections from protocol semantics to editorial D2 diagrams."""

from __future__ import annotations

import hashlib
import json
import re
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
    DiagramRelationDirection,
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
    ProtocolEvidenceLevelV1,
    ProtocolEvidenceRefV1,
    ProtocolRenderResultV1,
    ProtocolValidationStatus,
)

PROTOCOL_DIAGRAM_PROJECTION_VERSION = "protocol-diagram-projection-v3"
PROTOCOL_DIAGRAM_FIGURE_POLICY_VERSION = (
    f"{DIAGRAM_COMPILATION_POLICY_VERSION}+{PROTOCOL_DIAGRAM_PROJECTION_VERSION}"
)
_MAX_FIELD_EVIDENCE_REFS = 8
_MAX_FIELD_EVIDENCE_FRAMES = 12
_UUID_TEXT = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
    re.IGNORECASE,
)
_HASH_TEXT = re.compile(r"\b[0-9a-f]{32,64}\b", re.IGNORECASE)
_REFERENCE_TAIL = re.compile(
    r"\s*[·;,:—-]?\s*(?:sources?|observations?|findings?|evidence)\b.*$",
    re.IGNORECASE,
)
_DOCUMENTED_MARKERS = (
    "documented, not observed",
    "documenté, non observé",
    "article_documented",
)
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
                label="Aucun élément établi",
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

    _validate_projected_graph(spec, nodes, edges)
    return DiagramSpecV1(
        key=f"protocol-{spec.kind.value}-{spec.canonical_sha256[:16]}",
        kind=_KINDS[spec.kind],
        title=_display_title(spec.title),
        caption=protocol_diagram_caption(spec),
        direction=_DIRECTIONS[spec.kind],
        nodes=tuple(nodes),
        edges=tuple(edges),
        groups=(),
        placement=EnrichmentPlacementV1(EnrichmentPlacementKind.END),
        profile=profile,
    )


def protocol_diagram_caption(spec: ProtocolDiagramSpecV1) -> str:
    """Keep proof origin and the important uncertainty in one short caption."""
    limitations = tuple(
        _clean_display_text(item, limit=160) for item in spec.limitations if item.strip()
    )
    if _is_documented_view(spec):
        limit_line = "Aucun flux ni trame observés; offsets et valeurs d'octets inconnus."
    elif spec.status is ProtocolDiagramStatus.INSUFFICIENT_EVIDENCE:
        limit_line = "Trames, offsets et valeurs d'octets inconnus; aucune séquence n'est établie."
    else:
        limit_line = "Trames et offsets omis du dessin; les éléments non établis restent inconnus."
        if limitations:
            limit_line += " " + " ".join(limitations[:1])
    proof_levels = _protocol_spec_evidence_levels(spec)
    if ProtocolEvidenceLevelV1.ARTICLE_DOCUMENTED in proof_levels:
        other_levels = tuple(
            level
            for level in proof_levels
            if level is not ProtocolEvidenceLevelV1.ARTICLE_DOCUMENTED
        )
        proof_summary = "Niveau de preuve : ARTICLE_DOCUMENTED"
        if other_levels:
            proof_summary += ", " + ", ".join(level.value.upper() for level in other_levels)
        proof_summary += " — documenté, non observé."
    elif proof_levels:
        proof_summary = (
            "Niveau de preuve : " + ", ".join(level.value.upper() for level in proof_levels) + "."
        )
    else:
        proof_summary = "Niveau de preuve : INSUFFICIENT_EVIDENCE."
    return f"{proof_summary} {limit_line}"


def _display_title(value: str) -> str:
    title = _clean_display_text(value, limit=72)
    for marker in _DOCUMENTED_MARKERS:
        title = re.sub(re.escape(marker), "", title, flags=re.IGNORECASE)
    title = re.sub(r"\s*[·|—:-]\s*", " ", title).strip()
    return title or "Vue du protocole"


def _clean_display_text(value: str, *, limit: int = 72) -> str:
    text = _UUID_TEXT.sub("", value)
    text = _HASH_TEXT.sub("", text)
    text = _REFERENCE_TAIL.sub("", text)
    for marker in _DOCUMENTED_MARKERS:
        text = re.sub(re.escape(marker), "", text, flags=re.IGNORECASE)
    text = re.sub(
        r"\b(?:ARTICLE_DOCUMENTED|STATIC_INFERENCE|SANDBOX_CAPTURE_OBSERVED|"
        r"HYPOTHETICAL|VERIFIED_ON_HOLDOUT|OBSERVED|INFERRED|UNRESOLVED|"
        r"CONTRADICTED|DOCUMENTED_NOT_OBSERVED)\b",
        "",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"\s*[·|—:-]\s*", " ", text)
    text = re.sub(r"\s+", " ", text).strip(" .,:;—-")
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text


def _protocol_spec_evidence_levels(
    spec: ProtocolDiagramSpecV1,
) -> tuple[ProtocolEvidenceLevelV1, ...]:
    """Return the distinct evidence origins actually present in a protocol view."""
    evidence_levels = {
        _resolved_evidence_level(item.evidence_level, item.validation_status, item.evidence)
        for item in spec.nodes
    }
    evidence_levels.update(
        _resolved_evidence_level(item.evidence_level, item.validation_status, item.evidence)
        for item in spec.edges
    )
    evidence_levels.update(
        _resolved_evidence_level(item.evidence_level, item.validation_status, item.evidence)
        for item in spec.fields
    )
    if not evidence_levels and any(
        "article_documented" in item.casefold() for item in spec.limitations
    ):
        evidence_levels.add(ProtocolEvidenceLevelV1.ARTICLE_DOCUMENTED)
    return tuple(level for level in ProtocolEvidenceLevelV1 if level in evidence_levels)


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
            compiler_policy_version=PROTOCOL_DIAGRAM_FIGURE_POLICY_VERSION,
            caption=f"{item.diagram.title}\n{item.diagram.caption or item.spec.title}",
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


def _is_documented_view(spec: ProtocolDiagramSpecV1) -> bool:
    marker = "documenté, non observé"
    if (
        marker in spec.title.casefold()
        or "article_documented" in spec.title.casefold()
        or any(
            marker in item.casefold() or "article_documented" in item.casefold()
            for item in spec.limitations
        )
    ):
        return True
    has_elements = bool(spec.nodes or spec.edges or spec.fields)
    return has_elements and (
        all(
            _resolved_evidence_level(item.evidence_level, item.validation_status, item.evidence)
            is ProtocolEvidenceLevelV1.ARTICLE_DOCUMENTED
            for item in spec.nodes
        )
        and all(
            _resolved_evidence_level(item.evidence_level, item.validation_status, item.evidence)
            is ProtocolEvidenceLevelV1.ARTICLE_DOCUMENTED
            for item in spec.edges
        )
        and all(
            _resolved_evidence_level(item.evidence_level, item.validation_status, item.evidence)
            is ProtocolEvidenceLevelV1.ARTICLE_DOCUMENTED
            for item in spec.fields
        )
    )


def _resolved_evidence_level(
    level: ProtocolEvidenceLevelV1 | None,
    status: ProtocolValidationStatus,
    evidence: tuple[ProtocolEvidenceRefV1, ...],
) -> ProtocolEvidenceLevelV1:
    if level is not None:
        return level
    if status is ProtocolValidationStatus.DOCUMENTED_NOT_OBSERVED:
        return ProtocolEvidenceLevelV1.ARTICLE_DOCUMENTED
    if any(item.capture_sha256 is not None for item in evidence):
        return ProtocolEvidenceLevelV1.SANDBOX_CAPTURE_OBSERVED
    return ProtocolEvidenceLevelV1.STATIC_INFERENCE


def _project_participant(node: ProtocolDiagramNodeV1) -> DiagramNodeV1:
    return DiagramNodeV1(
        node_id=node.node_id,
        label=_clean_display_text(node.label),
        evidence_refs=(),
        role=DiagramNodeRole.PROTOCOL_PARTICIPANT,
    )


def _project_state(node: ProtocolDiagramNodeV1) -> DiagramNodeV1:
    return DiagramNodeV1(
        node_id=node.node_id,
        label=_clean_display_text(node.label),
        evidence_refs=(),
        role=DiagramNodeRole.PROTOCOL_STATE,
    )


def _project_message(edge: ProtocolDiagramEdgeV1) -> DiagramEdgeV1:
    details = [_clean_display_text(edge.label)]
    if edge.order is not None:
        details.insert(0, f"#{edge.order}")
    return DiagramEdgeV1(
        source_node_id=edge.source_id,
        target_node_id=edge.target_id,
        label=" · ".join(item for item in details if item),
        evidence_refs=(),
        direction=DiagramRelationDirection.DIRECTED,
        relation_type=(
            DiagramRelationType.PROTOCOL_FACTUAL
            if edge.validation_status
            in {
                ProtocolValidationStatus.OBSERVED,
                ProtocolValidationStatus.DOCUMENTED_NOT_OBSERVED,
            }
            else DiagramRelationType.PROTOCOL_INFERENCE
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
    details = [_clean_display_text(edge.label)]
    if edge.order is not None:
        details.insert(0, f"#{edge.order}")
    return DiagramEdgeV1(
        source_node_id=edge.source_id,
        target_node_id=edge.target_id,
        label=" · ".join(item for item in details if item),
        evidence_refs=(),
        direction=DiagramRelationDirection.DIRECTED,
        relation_type=(
            DiagramRelationType.PROTOCOL_FACTUAL
            if edge.validation_status
            in {
                ProtocolValidationStatus.OBSERVED,
                ProtocolValidationStatus.DOCUMENTED_NOT_OBSERVED,
            }
            else DiagramRelationType.PROTOCOL_INFERENCE
        ),
    )


def _project_field(field: ProtocolDiagramFieldV1, index: int) -> DiagramNodeV1:
    if field.encrypted_opaque:
        field_name = "Opaque encrypted payload · bytes omitted"
    else:
        field_name = field.name.strip() or f"Field {index}"
    label = _clean_display_text(field_name) or f"Champ {index}"
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
        left_level = _resolved_evidence_level(
            left.evidence_level, left.validation_status, left.evidence
        )
        right_level = _resolved_evidence_level(
            right.evidence_level, right.validation_status, right.evidence
        )
        edge_is_observed = (
            left.validation_status is ProtocolValidationStatus.OBSERVED
            and right.validation_status is ProtocolValidationStatus.OBSERVED
            and left_level is right_level
            and left_level
            in {
                ProtocolEvidenceLevelV1.SANDBOX_CAPTURE_OBSERVED,
                ProtocolEvidenceLevelV1.VERIFIED_ON_HOLDOUT,
            }
        )
        status = (
            ProtocolValidationStatus.OBSERVED
            if edge_is_observed
            else ProtocolValidationStatus.INFERRED
        )
        edges.append(
            DiagramEdgeV1(
                source_node_id=f"protocol-field-{left_index:04d}",
                target_node_id=f"protocol-field-{right_index:04d}",
                label="field order",
                evidence_refs=(),
                direction=DiagramRelationDirection.DIRECTED,
                relation_type=(
                    DiagramRelationType.PROTOCOL_FACTUAL
                    if status is ProtocolValidationStatus.OBSERVED
                    else DiagramRelationType.PROTOCOL_INFERENCE
                ),
            )
        )
    return edges


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
    "PROTOCOL_DIAGRAM_FIGURE_POLICY_VERSION",
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
