"""Single validation/apply authority for typed visual revisions of enrichments."""

from __future__ import annotations

from cti_app.domain.editorial_enrichment_patch import (
    MoveEnrichmentElementPatch,
    RemoveEnrichmentElementPatch,
    ReorderEnrichmentElementsPatch,
    ReplaceSourceFigurePatch,
    ReviseChartPatch,
    ReviseDiagramPatch,
    UpdateVisualCaptionPatch,
    VisualRevisionPatch,
)

from .chart_ops import plan_chart_revision
from .context import VisualRevisionContext, VisualRevisionPlan, VisualRevisionRejected
from .diagram_ops import plan_diagram_revision
from .placement_ops import plan_caption_update, plan_move, plan_reorder


def plan_visual_revision(
    ctx: VisualRevisionContext,
    patch: VisualRevisionPatch,
) -> VisualRevisionPlan:
    """Validate one typed revision and return its complete candidate enrichment.

    Pure and deterministic: no I/O, no model call, no canonical write. Prepare
    and adoption both call this function so the seven variants never grow
    separate validation paths. Variants owned by F2-03c reject with a typed
    ``enrichment_revision_unsupported_operation`` until their slice lands.
    """
    if not isinstance(ctx, VisualRevisionContext):
        raise ValueError("Expected a VisualRevisionContext")
    if isinstance(patch, UpdateVisualCaptionPatch):
        return plan_caption_update(ctx, patch)
    if isinstance(patch, MoveEnrichmentElementPatch):
        return plan_move(ctx, patch)
    if isinstance(patch, ReorderEnrichmentElementsPatch):
        return plan_reorder(ctx, patch)
    if isinstance(patch, ReviseChartPatch):
        return plan_chart_revision(ctx, patch)
    if isinstance(patch, ReviseDiagramPatch):
        return plan_diagram_revision(ctx, patch)
    if isinstance(patch, (ReplaceSourceFigurePatch, RemoveEnrichmentElementPatch)):
        raise VisualRevisionRejected(
            "enrichment_revision_unsupported_operation",
            {"variant": type(patch).__name__, "slice": "F2-03c"},
        )
    raise VisualRevisionRejected(
        "enrichment_revision_patch_invalid",
        {"variant": type(patch).__name__},
    )
