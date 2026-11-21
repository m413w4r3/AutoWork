"""Caption, move and reorder validation/apply for visual revision patches."""

from __future__ import annotations

from dataclasses import replace
from typing import cast

from cti_app.application.editorial_text_policy import editorial_caption_text_violation
from cti_app.application.production_editorial_enrichment import validate_editorial_enrichment
from cti_app.domain.editorial_enrichment_patch import (
    MoveEnrichmentElementPatch,
    ReorderEnrichmentElementsPatch,
    UpdateVisualCaptionPatch,
    enrichment_order_group_hash,
)
from cti_app.domain.production_editorial_enrichment import (
    EDITORIAL_ENRICHMENT_MAX_SCHEMA_VERSION,
    EDITORIAL_ENRICHMENT_V11_POLICY_VERSION,
    ChartSpecV1,
    DiagramSpecV1,
    EditorialEnrichmentElementKind,
    EditorialEnrichmentV1,
    EnrichmentElementRef,
    EnrichmentPlacementKind,
    EnrichmentPlacementV1,
    SourceFigureCandidateV1,
    TableSpecV1,
    enrichment_element_order,
)

from .context import (
    VisualRevisionContext,
    VisualRevisionPlan,
    VisualRevisionRejected,
    visual_preview_payload,
)

FIGURE_CAPTION_MAX_CHARACTERS = 500
CHART_CAPTION_MAX_CHARACTERS = 2000

_Element = TableSpecV1 | DiagramSpecV1 | ChartSpecV1 | SourceFigureCandidateV1


def _family(
    enrichment: EditorialEnrichmentV1, kind: EditorialEnrichmentElementKind
) -> tuple[_Element, ...]:
    if kind is EditorialEnrichmentElementKind.TABLE:
        return enrichment.tables
    if kind is EditorialEnrichmentElementKind.DIAGRAM:
        return enrichment.diagrams
    if kind is EditorialEnrichmentElementKind.CHART:
        return enrichment.charts
    return enrichment.source_figures


def _all_elements(enrichment: EditorialEnrichmentV1) -> tuple[_Element, ...]:
    return (
        *enrichment.tables,
        *enrichment.diagrams,
        *enrichment.charts,
        *enrichment.source_figures,
    )


def find_element(
    enrichment: EditorialEnrichmentV1,
    ref: EnrichmentElementRef,
) -> _Element:
    """Resolve one element, distinguishing a missing key from a kind mismatch."""
    match = next((item for item in _family(enrichment, ref.kind) if item.key == ref.key), None)
    if match is not None:
        return match
    if any(item.key == ref.key for item in _all_elements(enrichment)):
        raise VisualRevisionRejected(
            "enrichment_element_kind_mismatch",
            {"element_kind": ref.kind.value, "element_key": ref.key},
        )
    raise VisualRevisionRejected(
        "enrichment_element_not_found",
        {"element_kind": ref.kind.value, "element_key": ref.key},
    )


def replace_element(
    enrichment: EditorialEnrichmentV1,
    ref: EnrichmentElementRef,
    replacement: _Element,
) -> EditorialEnrichmentV1:
    """Return a copy with exactly one element replaced, preserving list order."""
    if replacement.key != ref.key:
        raise ValueError("Visual revision replacement changed the element key")

    def replaced(items: tuple[_Element, ...]) -> tuple[_Element, ...]:
        if sum(item.key == ref.key for item in items) != 1:
            raise VisualRevisionRejected(
                "enrichment_element_not_found",
                {"element_kind": ref.kind.value, "element_key": ref.key},
            )
        return tuple(replacement if item.key == ref.key else item for item in items)

    if ref.kind is EditorialEnrichmentElementKind.TABLE:
        return replace(
            enrichment,
            tables=cast(tuple[TableSpecV1, ...], replaced(enrichment.tables)),
        )
    if ref.kind is EditorialEnrichmentElementKind.DIAGRAM:
        return replace(
            enrichment,
            diagrams=cast(tuple[DiagramSpecV1, ...], replaced(enrichment.diagrams)),
        )
    if ref.kind is EditorialEnrichmentElementKind.CHART:
        return replace(
            enrichment,
            charts=cast(tuple[ChartSpecV1, ...], replaced(enrichment.charts)),
        )
    return replace(
        enrichment,
        source_figures=cast(
            tuple[SourceFigureCandidateV1, ...], replaced(enrichment.source_figures)
        ),
    )


def with_element_caption(
    element: _Element,
    caption: str | None,
) -> _Element:
    """Return one element with only its caption replaced (kind-checked)."""
    if isinstance(element, SourceFigureCandidateV1):
        if caption is None:
            raise VisualRevisionRejected(
                "enrichment_caption_invalid",
                {"element_kind": EditorialEnrichmentElementKind.FIGURE.value},
            )
        return replace(element, caption=caption)
    return replace(element, caption=caption)


def with_order_schema(enrichment: EditorialEnrichmentV1) -> EditorialEnrichmentV1:
    """Use schema V11 only when an element actually carries an explicit order."""
    has_group_order = any(item.group_order is not None for item in _all_elements(enrichment))
    if has_group_order and enrichment.schema_version < EDITORIAL_ENRICHMENT_MAX_SCHEMA_VERSION:
        return replace(
            enrichment,
            schema_version=EDITORIAL_ENRICHMENT_MAX_SCHEMA_VERSION,
            enrichment_policy_version=EDITORIAL_ENRICHMENT_V11_POLICY_VERSION,
        )
    return enrichment


def _normalized_caption(
    kind: EditorialEnrichmentElementKind,
    caption: str | None,
    *,
    schema_version: int,
) -> str | None:
    if caption is not None and editorial_caption_text_violation(caption):
        raise VisualRevisionRejected(
            "editorial_caption_text_violation",
            {"element_kind": kind.value},
        )
    if kind is EditorialEnrichmentElementKind.FIGURE:
        if caption is None or not caption.strip():
            raise VisualRevisionRejected(
                "enrichment_caption_invalid",
                {"element_kind": kind.value, "reason_code": "caption_required"},
            )
        if len(caption) > FIGURE_CAPTION_MAX_CHARACTERS:
            raise VisualRevisionRejected(
                "enrichment_caption_invalid",
                {"element_kind": kind.value, "reason_code": "caption_too_long"},
            )
        return caption
    if kind is EditorialEnrichmentElementKind.DIAGRAM:
        if caption is None or not caption.strip():
            if schema_version >= 8:
                raise VisualRevisionRejected(
                    "enrichment_caption_invalid",
                    {"element_kind": kind.value, "reason_code": "caption_required"},
                )
            return None
        return caption
    if caption is None or not caption.strip():
        return None
    if kind is EditorialEnrichmentElementKind.CHART and len(caption) > CHART_CAPTION_MAX_CHARACTERS:
        raise VisualRevisionRejected(
            "enrichment_caption_invalid",
            {"element_kind": kind.value, "reason_code": "caption_too_long"},
        )
    return caption


def plan_caption_update(
    ctx: VisualRevisionContext,
    patch: UpdateVisualCaptionPatch,
) -> VisualRevisionPlan:
    ref = EnrichmentElementRef(patch.element_kind, patch.element_key)
    element = find_element(ctx.base_enrichment, ref)
    caption = _normalized_caption(
        patch.element_kind,
        patch.caption,
        schema_version=ctx.base_enrichment.schema_version,
    )
    if caption == element.caption:
        raise VisualRevisionRejected(
            "enrichment_revision_no_change",
            {"element_kind": patch.element_kind.value, "element_key": patch.element_key},
        )
    revised = with_order_schema(
        replace_element(ctx.base_enrichment, ref, with_element_caption(element, caption))
    )
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
                "caption": caption,
                "previous_caption": element.caption,
            },
        ),
    )


def _placement_payload(placement: EnrichmentPlacementV1) -> dict[str, object]:
    return {
        "kind": placement.kind.value,
        "section_index": placement.section_index,
        "anchor_block_id": (
            str(placement.anchor_block_id) if placement.anchor_block_id is not None else None
        ),
    }


def plan_move(
    ctx: VisualRevisionContext,
    patch: MoveEnrichmentElementPatch,
) -> VisualRevisionPlan:
    ref = EnrichmentElementRef(patch.element_kind, patch.element_key)
    element = find_element(ctx.base_enrichment, ref)
    destination = EnrichmentPlacementV1(
        EnrichmentPlacementKind.AFTER_PARAGRAPH,
        anchor_block_id=patch.anchor_block_id,
    )
    if element.placement == destination:
        raise VisualRevisionRejected(
            "enrichment_revision_no_change",
            {
                "element_kind": patch.element_kind.value,
                "element_key": patch.element_key,
                "reason_code": "placement_unchanged",
            },
        )
    registered = ctx.registered_anchor_ids
    if registered is None or patch.anchor_block_id not in registered:
        # Never fall back to a section end: an unregistered or stale anchor
        # must fail so no content is moved without an editorial decision.
        raise VisualRevisionRejected(
            "editorial_anchor_stale",
            {"element_kind": patch.element_kind.value, "element_key": patch.element_key},
        )
    revised = with_order_schema(
        replace_element(
            ctx.base_enrichment,
            ref,
            replace(element, placement=destination, group_order=None),
        )
    )
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
                "from": _placement_payload(element.placement),
                "to": _placement_payload(destination),
            },
        ),
    )


def _group_members(
    enrichment: EditorialEnrichmentV1,
    placement: EnrichmentPlacementV1,
) -> tuple[EnrichmentElementRef, ...]:
    group = next(
        (
            candidate
            for candidate in enrichment_element_order(enrichment)
            if candidate.placement == placement
        ),
        None,
    )
    return group.members if group is not None else ()


def plan_reorder(
    ctx: VisualRevisionContext,
    patch: ReorderEnrichmentElementsPatch,
) -> VisualRevisionPlan:
    placement = EnrichmentPlacementV1(
        EnrichmentPlacementKind.AFTER_PARAGRAPH,
        anchor_block_id=patch.anchor_block_id,
    )
    registered = ctx.registered_anchor_ids
    if registered is None or patch.anchor_block_id not in registered:
        raise VisualRevisionRejected("editorial_anchor_stale", {"anchor": "unregistered"})
    members = _group_members(ctx.base_enrichment, placement)
    if len(members) < 2:
        raise VisualRevisionRejected(
            "enrichment_order_group_unsupported",
            {"anchor_block_id": str(patch.anchor_block_id), "member_count": len(members)},
        )
    requested = patch.order
    if len(requested) != len(members) or len(set(requested)) != len(requested):
        raise VisualRevisionRejected(
            "enrichment_order_invalid",
            {"anchor_block_id": str(patch.anchor_block_id), "reason_code": "not_a_permutation"},
        )
    if set(requested) != set(members):
        raise VisualRevisionRejected(
            "enrichment_order_invalid",
            {"anchor_block_id": str(patch.anchor_block_id), "reason_code": "member_mismatch"},
        )
    if requested == members:
        raise VisualRevisionRejected(
            "enrichment_revision_no_change",
            {"anchor_block_id": str(patch.anchor_block_id)},
        )
    # Assign every member in one constructor call: per-element updates would
    # pass through an intermediate state with duplicate group orders, which the
    # V11 invariant rejects when an explicit group is merely permuted.
    order_by_ref = {ref: index for index, ref in enumerate(requested)}

    def ordered(
        items: tuple[_Element, ...], kind: EditorialEnrichmentElementKind
    ) -> tuple[_Element, ...]:
        return tuple(
            replace(
                item,
                group_order=order_by_ref.get(
                    EnrichmentElementRef(kind, item.key), item.group_order
                ),
            )
            for item in items
        )

    revised = replace(
        ctx.base_enrichment,
        schema_version=EDITORIAL_ENRICHMENT_MAX_SCHEMA_VERSION,
        enrichment_policy_version=EDITORIAL_ENRICHMENT_V11_POLICY_VERSION,
        tables=cast(
            tuple[TableSpecV1, ...],
            ordered(ctx.base_enrichment.tables, EditorialEnrichmentElementKind.TABLE),
        ),
        diagrams=cast(
            tuple[DiagramSpecV1, ...],
            ordered(ctx.base_enrichment.diagrams, EditorialEnrichmentElementKind.DIAGRAM),
        ),
        charts=cast(
            tuple[ChartSpecV1, ...],
            ordered(ctx.base_enrichment.charts, EditorialEnrichmentElementKind.CHART),
        ),
        source_figures=cast(
            tuple[SourceFigureCandidateV1, ...],
            ordered(ctx.base_enrichment.source_figures, EditorialEnrichmentElementKind.FIGURE),
        ),
    )
    validate_editorial_enrichment(
        revised,
        extraction=ctx.extraction,
        synthesis=ctx.synthesis,
    )
    return VisualRevisionPlan(
        revised_enrichment=revised,
        changed_elements=requested,
        preview=visual_preview_payload(
            operation=patch.operation.value,
            changed_elements=requested,
            schema_version=revised.schema_version,
            details={
                "anchor_block_id": str(patch.anchor_block_id),
                "order": [{"kind": item.kind.value, "key": item.key} for item in requested],
            },
        ),
    )


__all__ = [
    "CHART_CAPTION_MAX_CHARACTERS",
    "FIGURE_CAPTION_MAX_CHARACTERS",
    "enrichment_order_group_hash",
    "find_element",
    "plan_caption_update",
    "plan_move",
    "plan_reorder",
    "replace_element",
    "with_order_schema",
]
