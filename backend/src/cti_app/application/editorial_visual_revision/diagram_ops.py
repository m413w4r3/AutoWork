"""In-place diagram revision recompiled by the deterministic D2 pipeline.

A revision may rename labels, remove an unproven association or reorder
presentation without new evidence. Adding a node, an edge or a relation is only
accepted when its evidence references were already admitted for that diagram;
the purpose evidence is preserved and must stay used by the graph. The planner
is pure: prepare compiles a PNG preview and adoption recompiles the canonical
SVG under the run lock. Arbitrary rendering code never runs.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from typing import Any

from cti_app.application.editorial_text_policy import editorial_caption_text_violation
from cti_app.application.production_editorial_enrichment import (
    EditorialEnrichmentProposalControlError,
    _validate_grounded_editorial_text,
    _validate_ungrounded_editorial_text,
    editorial_technical_support,
    validate_editorial_enrichment,
)
from cti_app.application.production_synthesis import _all_evidence_entries
from cti_app.domain.editorial_enrichment_patch import ReviseDiagramPatch
from cti_app.domain.production_editorial_enrichment import (
    DiagramSpecV1,
    EditorialEnrichmentElementKind,
    EnrichmentElementRef,
)
from cti_app.domain.production_synthesis import (
    ExtractionEvidenceRefV1,
    evidence_ref_sort_key,
)

from .context import (
    VisualRevisionContext,
    VisualRevisionPlan,
    VisualRevisionRejected,
    admitted_evidence_refs,
    patch_invalid,
    visual_preview_payload,
)
from .placement_ops import find_element, replace_element


def _diagram_evidence_refs(diagram: DiagramSpecV1) -> set[ExtractionEvidenceRefV1]:
    refs = {ref for node in diagram.nodes for ref in node.evidence_refs}
    refs.update(ref for edge in diagram.edges for ref in edge.evidence_refs)
    if diagram.purpose is not None:
        refs.update(diagram.purpose.evidence_refs)
    return refs


def _check_evidence_scope(
    base: DiagramSpecV1,
    revised: DiagramSpecV1,
    *,
    admitted_refs: frozenset[ExtractionEvidenceRefV1],
    known_refs: set[ExtractionEvidenceRefV1],
) -> None:
    """A revision never introduces evidence outside the diagram's admitted set."""
    previously_admitted = _diagram_evidence_refs(base)
    revised_refs = _diagram_evidence_refs(revised)
    if any(ref not in admitted_refs or ref not in known_refs for ref in revised_refs):
        raise patch_invalid(
            "diagram_evidence_unadmitted",
            element_kind=EditorialEnrichmentElementKind.DIAGRAM.value,
            element_key=revised.key,
        )
    for node in revised.nodes:
        if set(node.evidence_refs) - previously_admitted:
            raise patch_invalid(
                "diagram_evidence_not_previously_admitted",
                element_kind=EditorialEnrichmentElementKind.DIAGRAM.value,
                element_key=revised.key,
                item_kind="node",
                item_id=node.node_id,
            )
    for edge in revised.edges:
        if set(edge.evidence_refs) - previously_admitted:
            raise patch_invalid(
                "diagram_evidence_not_previously_admitted",
                element_kind=EditorialEnrichmentElementKind.DIAGRAM.value,
                element_key=revised.key,
                item_kind="edge",
                item_id=f"{edge.source_node_id}->{edge.target_node_id}",
            )


def _check_purpose(
    base: DiagramSpecV1,
    revised: DiagramSpecV1,
    *,
    admitted_refs: frozenset[ExtractionEvidenceRefV1],
) -> tuple[ExtractionEvidenceRefV1, ...]:
    if base.purpose is None:
        if revised.purpose is not None:
            raise patch_invalid("diagram_purpose_unsupported", element_key=revised.key)
        return ()
    if revised.purpose is None:
        raise patch_invalid("diagram_purpose_missing", element_key=revised.key)
    purpose_refs = set(revised.purpose.evidence_refs)
    if not purpose_refs or not purpose_refs <= set(base.purpose.evidence_refs):
        # Purpose evidence is preserved: a revision may not re-ground the
        # analytic question on records the base diagram never admitted.
        raise patch_invalid("diagram_purpose_evidence_changed", element_key=revised.key)
    if any(ref not in admitted_refs for ref in purpose_refs):
        raise patch_invalid("diagram_purpose_evidence_unadmitted", element_key=revised.key)
    graph_refs = {ref for node in revised.nodes for ref in node.evidence_refs}
    graph_refs.update(ref for edge in revised.edges for ref in edge.evidence_refs)
    if not purpose_refs <= graph_refs:
        raise patch_invalid("diagram_purpose_evidence_not_used_by_graph", element_key=revised.key)
    return tuple(sorted(purpose_refs, key=evidence_ref_sort_key))


def _check_text_policy(
    diagram: DiagramSpecV1,
    *,
    entries: Mapping[ExtractionEvidenceRefV1, Mapping[str, Any]],
    technical_support: Mapping[tuple[str, str], set[ExtractionEvidenceRefV1]],
    purpose_refs: tuple[ExtractionEvidenceRefV1, ...],
) -> None:
    if diagram.caption is not None and editorial_caption_text_violation(diagram.caption):
        raise VisualRevisionRejected(
            "editorial_caption_text_violation",
            {
                "element_kind": EditorialEnrichmentElementKind.DIAGRAM.value,
                "element_key": diagram.key,
            },
        )
    diagram_refs = tuple(
        sorted(
            {ref for node in diagram.nodes for ref in node.evidence_refs}
            | {ref for edge in diagram.edges for ref in edge.evidence_refs},
            key=evidence_ref_sort_key,
        )
    )
    grounded_text: list[tuple[str, str, tuple[ExtractionEvidenceRefV1, ...]]] = [
        ("title", diagram.title, diagram_refs),
        ("caption", diagram.caption or "", diagram_refs),
    ]
    for node in diagram.nodes:
        grounded_text.append((f"node.{node.node_id}.label", node.label, node.evidence_refs))
    for edge in diagram.edges:
        if edge.label is not None:
            grounded_text.append(
                (
                    f"edge.{edge.source_node_id}.{edge.target_node_id}.label",
                    edge.label,
                    edge.evidence_refs,
                )
            )
    if diagram.purpose is not None:
        for field_name in (
            "question",
            "available_data",
            "comprehension_gain",
            "scope",
            "knowledge_limits",
            "placement_reason",
        ):
            grounded_text.append(
                (
                    f"purpose.{field_name}",
                    str(getattr(diagram.purpose, field_name)),
                    purpose_refs,
                )
            )
    for field_name, value, refs in grounded_text:
        try:
            _validate_grounded_editorial_text(
                value,
                refs,
                entries,
                technical_support,
                context=f"diagram:{diagram.key}:{field_name}",
            )
        except EditorialEnrichmentProposalControlError as error:
            raise patch_invalid(
                "diagram_text_policy_violation",
                element_key=diagram.key,
                field=field_name,
                diagnostic_code=str(error.diagnostic_code or error.code.value),
            ) from None
    for group in diagram.groups:
        try:
            _validate_ungrounded_editorial_text(
                group.label, context=f"diagram:{diagram.key}:group.{group.group_id}.label"
            )
        except EditorialEnrichmentProposalControlError as error:
            raise patch_invalid(
                "diagram_text_policy_violation",
                element_key=diagram.key,
                field=f"group.{group.group_id}.label",
                diagnostic_code=str(error.diagnostic_code or error.code.value),
            ) from None


def plan_diagram_revision(
    ctx: VisualRevisionContext,
    patch: ReviseDiagramPatch,
) -> VisualRevisionPlan:
    ref = EnrichmentElementRef(EditorialEnrichmentElementKind.DIAGRAM, patch.element_key)
    base = find_element(ctx.base_enrichment, ref)
    if not isinstance(base, DiagramSpecV1):
        raise VisualRevisionRejected(
            "enrichment_element_kind_mismatch",
            {
                "element_kind": EditorialEnrichmentElementKind.DIAGRAM.value,
                "element_key": patch.element_key,
            },
        )
    diagram = patch.diagram
    if diagram.placement != base.placement:
        raise VisualRevisionRejected(
            "enrichment_placement_changed",
            {
                "element_kind": EditorialEnrichmentElementKind.DIAGRAM.value,
                "element_key": patch.element_key,
            },
        )
    if diagram.group_order is not None and diagram.group_order != base.group_order:
        raise patch_invalid(
            "element_order_changed",
            element_kind=EditorialEnrichmentElementKind.DIAGRAM.value,
            element_key=patch.element_key,
        )
    normalized = replace(
        diagram,
        placement=base.placement,
        group_order=base.group_order,
        compiled_asset_id=None,
    )
    if replace(normalized, compiled_asset_id=base.compiled_asset_id) == base:
        raise VisualRevisionRejected(
            "enrichment_revision_no_change",
            {
                "element_kind": EditorialEnrichmentElementKind.DIAGRAM.value,
                "element_key": patch.element_key,
            },
        )

    admitted_refs = admitted_evidence_refs(ctx)
    entries = _all_evidence_entries(ctx.extraction)
    technical_support = editorial_technical_support(entries)
    _check_evidence_scope(
        base,
        normalized,
        admitted_refs=admitted_refs,
        known_refs=set(entries),
    )
    purpose_refs = _check_purpose(base, normalized, admitted_refs=admitted_refs)
    _check_text_policy(
        normalized,
        entries=entries,
        technical_support=technical_support,
        purpose_refs=purpose_refs,
    )
    if ctx.base_enrichment.schema_version >= 8 and (
        normalized.profile is None or normalized.caption is None
    ):
        # Mirror the canonical V8 invariant before constructing the revised
        # artifact so a structurally impossible patch stays a typed refusal.
        raise patch_invalid("diagram_profile_and_caption_required", element_key=patch.element_key)

    revised = replace_element(ctx.base_enrichment, ref, normalized)
    validate_editorial_enrichment(
        revised,
        extraction=ctx.extraction,
        synthesis=ctx.synthesis,
    )
    return VisualRevisionPlan(
        revised_enrichment=revised,
        changed_elements=(ref,),
        preview=visual_preview_payload(
            operation=patch.operation.value,
            changed_elements=(ref,),
            schema_version=revised.schema_version,
            details={
                "element_kind": EditorialEnrichmentElementKind.DIAGRAM.value,
                "element_key": patch.element_key,
                "title": normalized.title,
                "caption": normalized.caption,
                "kind": normalized.kind.value,
                "profile": normalized.profile.value if normalized.profile is not None else None,
                "direction": normalized.direction.value,
                "nodes": [
                    {"node_id": node.node_id, "label": node.label} for node in normalized.nodes
                ],
                "edges": [
                    {
                        "source": edge.source_node_id,
                        "target": edge.target_node_id,
                        "label": edge.label,
                    }
                    for edge in normalized.edges
                ],
                "groups": [
                    {"group_id": group.group_id, "label": group.label}
                    for group in normalized.groups
                ],
            },
        ),
        materialize_diagram=normalized,
    )


__all__ = ["plan_diagram_revision"]
