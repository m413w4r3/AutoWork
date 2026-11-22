"""Typed removal of one existing enrichment element (slice F2-03c).

Removal deletes the element from its family list only. Other elements keep
their list position and explicit ``group_order``; archived media, inventory and
source evidence are never touched, and the canonical write stays a single new
artifact version. The impact outbox invalidates derived renders after adoption.
"""

from __future__ import annotations

from dataclasses import replace

from cti_app.application.production_editorial_enrichment import (
    validate_editorial_enrichment,
)
from cti_app.domain.editorial_enrichment_patch import RemoveEnrichmentElementPatch
from cti_app.domain.production_editorial_enrichment import (
    EditorialEnrichmentElementKind,
    EnrichmentElementRef,
    SourceFigureCandidateV1,
    remove_enrichment_element,
)

from .context import (
    VisualRevisionContext,
    VisualRevisionPlan,
    visual_preview_payload,
)
from .placement_ops import find_element


def plan_remove_enrichment_element(
    ctx: VisualRevisionContext,
    patch: RemoveEnrichmentElementPatch,
) -> VisualRevisionPlan:
    ref = EnrichmentElementRef(patch.element_kind, patch.element_key)
    element = find_element(ctx.base_enrichment, ref)
    revised = remove_enrichment_element(ctx.base_enrichment, ref)
    if patch.element_kind is EditorialEnrichmentElementKind.FIGURE and isinstance(
        element, SourceFigureCandidateV1
    ):
        removed_id = (
            element.resolved_figure.figure_id if element.resolved_figure is not None else None
        )
        # Current behaviour (kept and documented): the removed figure's decision
        # trace is dropped; earlier artifact versions retain the history.
        if removed_id is not None:
            revised = replace(
                revised,
                figure_decisions=tuple(
                    decision
                    for decision in revised.figure_decisions
                    if decision.figure_id != removed_id
                ),
            )
    anchor_prefix = f"{patch.element_kind.value}:{patch.element_key}:"
    annotations = tuple(
        annotation
        for annotation in revised.annotations
        if not annotation.paragraph_anchor.startswith(anchor_prefix)
    )
    if annotations != revised.annotations:
        revised = replace(revised, annotations=annotations)
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
                "element_kind": patch.element_kind.value,
                "element_key": patch.element_key,
            },
        ),
    )


__all__ = ["plan_remove_enrichment_element"]
