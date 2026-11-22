"""Catalogue-resolved source figure replacement (slice F2-03c).

The replacement figure is identified by its run-scoped opaque catalogue handle
and re-resolved from the fresh catalogue built by the caller's input fence at
both preparation and adoption. No URL or caller-supplied archive fact is
trusted, and the replacement is applied in place: same key, placement, order
and explicit group order, one artifact version, with the prior version keeping
the old image reference.
"""

from __future__ import annotations

from dataclasses import replace
from uuid import UUID

from cti_app.application.editorial_figure_catalog import (
    EditorialFigureCatalogError,
    opaque_figure_id,
    validate_figure_insertion_limit,
)
from cti_app.application.production_editorial_enrichment import (
    EditorialFigureCatalogEntry,
    semantic_annotation_anchor_texts,
    validate_editorial_enrichment,
)
from cti_app.application.source_figure_inventory import MAX_SOURCE_FIGURES
from cti_app.domain.editorial_enrichment_patch import ReplaceSourceFigurePatch
from cti_app.domain.production_editorial_enrichment import (
    EditorialEnrichmentElementKind,
    EditorialEnrichmentV1,
    EnrichmentElementRef,
    SourceFigureCandidateV1,
    SourceFigureDecision,
    SourceFigureInclusionStatus,
)
from cti_app.domain.production_synthesis import ProductionSynthesisV1

from .context import (
    VisualRevisionContext,
    VisualRevisionPlan,
    VisualRevisionRejected,
    visual_preview_payload,
)
from .placement_ops import _normalized_caption, find_element, replace_element

_FIGURE = EditorialEnrichmentElementKind.FIGURE


def _catalog_by_public_id(
    ctx: VisualRevisionContext,
) -> dict[UUID, EditorialFigureCatalogEntry]:
    if ctx.production_run_id is None:
        raise VisualRevisionRejected(
            "figure_catalog_unavailable", {"reason_code": "run_identity_missing"}
        )
    return {
        UUID(
            opaque_figure_id(
                ctx.base_enrichment.subject_id,
                ctx.production_run_id,
                entry.figure.figure_id,
            )
        ): entry
        for entry in ctx.figure_catalog
    }


def _selected_entry(
    ctx: VisualRevisionContext,
    patch: ReplaceSourceFigurePatch,
) -> EditorialFigureCatalogEntry:
    entry = _catalog_by_public_id(ctx).get(patch.figure_id)
    if entry is None:
        raise VisualRevisionRejected(
            "editorial_figure_not_found", {"element_key": patch.element_key}
        )
    return entry


def _require_eligible(
    ctx: VisualRevisionContext,
    entry: EditorialFigureCatalogEntry,
) -> EditorialFigureCatalogEntry:
    """Re-check every archived fact the insertion path checks, from fresh data."""
    figure = entry.figure
    metadata = entry.metadata
    reason: str | None = None
    if figure.decision is not SourceFigureDecision.ACCEPTED:
        reason = "figure_not_accepted"
    elif any(
        value is None
        for value in (figure.blob_id, figure.sha256, figure.mime_type, figure.byte_size)
    ):
        reason = "archive_metadata_missing"
    elif (
        metadata.width is None
        or metadata.height is None
        or metadata.width < 1
        or metadata.height < 1
    ):
        reason = "archive_dimensions_missing"
    else:
        source = next(
            (
                source
                for source in ctx.extraction.sources
                if source.source_document_id == figure.source_document_id
            ),
            None,
        )
        if source is None or source.canonical_url != figure.source:
            reason = "source_not_admitted"
    if reason is not None:
        raise VisualRevisionRejected("editorial_figure_not_eligible", {"reason_code": reason})
    return entry


def _enforce_figure_limit(
    ctx: VisualRevisionContext,
    target: SourceFigureCandidateV1,
) -> None:
    """Keep the publication ceiling unchanged; replacement never adds a figure."""
    if ctx.publication_kind is None:
        return
    included = sum(
        figure.inclusion_status is SourceFigureInclusionStatus.INCLUDED
        for figure in ctx.base_enrichment.source_figures
    )
    replaced_included = target.inclusion_status is SourceFigureInclusionStatus.INCLUDED
    try:
        validate_figure_insertion_limit(
            publication_kind=ctx.publication_kind,
            integrated_count=included - (1 if replaced_included else 0),
            requested_count=1,
            inventory_limit=MAX_SOURCE_FIGURES,
        )
    except EditorialFigureCatalogError as error:
        raise VisualRevisionRejected(error.code, {"element_key": target.key}) from None


def _without_stale_annotations(
    synthesis: ProductionSynthesisV1,
    enrichment: EditorialEnrichmentV1,
) -> EditorialEnrichmentV1:
    anchor_texts = semantic_annotation_anchor_texts(synthesis, enrichment)
    retained = tuple(
        annotation
        for annotation in enrichment.annotations
        if (text := anchor_texts.get(annotation.paragraph_anchor)) is not None
        and annotation.text in text
    )
    if retained == enrichment.annotations:
        return enrichment
    return replace(enrichment, annotations=retained)


def _public_figure_id(ctx: VisualRevisionContext, figure_id: UUID | None) -> str | None:
    if figure_id is None or ctx.production_run_id is None:
        return None
    return opaque_figure_id(ctx.base_enrichment.subject_id, ctx.production_run_id, figure_id)


def plan_replace_source_figure(
    ctx: VisualRevisionContext,
    patch: ReplaceSourceFigurePatch,
) -> VisualRevisionPlan:
    ref = EnrichmentElementRef(_FIGURE, patch.element_key)
    target = find_element(ctx.base_enrichment, ref)
    if not isinstance(target, SourceFigureCandidateV1):
        raise VisualRevisionRejected(
            "enrichment_element_kind_mismatch",
            {"element_kind": _FIGURE.value, "element_key": patch.element_key},
        )
    entry = _require_eligible(ctx, _selected_entry(ctx, patch))
    figure = entry.figure
    metadata = entry.metadata
    previous_id = target.resolved_figure.figure_id if target.resolved_figure is not None else None
    if previous_id == figure.figure_id:
        raise VisualRevisionRejected(
            "editorial_figure_not_changed",
            {"element_kind": _FIGURE.value, "element_key": patch.element_key},
        )
    if any(
        other.resolved_figure is not None and other.resolved_figure.figure_id == figure.figure_id
        for other in ctx.base_enrichment.source_figures
        if other.key != ref.key
    ):
        raise VisualRevisionRejected(
            "editorial_figure_not_eligible",
            {"reason_code": "already_used", "element_key": patch.element_key},
        )
    _enforce_figure_limit(ctx, target)
    caption = target.caption
    if patch.caption is not None:
        normalized = _normalized_caption(
            _FIGURE, patch.caption, schema_version=ctx.base_enrichment.schema_version
        )
        # Figure captions are always required by _normalized_caption.
        assert normalized is not None
        caption = normalized
    replacement = SourceFigureCandidateV1(
        key=target.key,
        source_document_id=figure.source_document_id,
        source_url=figure.source,
        caption=caption,
        provenance=figure.provenance,
        locator=figure.locator,
        inclusion_status=SourceFigureInclusionStatus.INCLUDED,
        placement=target.placement,
        resolved_figure=figure,
        purpose=None,
        width=metadata.width,
        height=metadata.height,
        order=target.order,
        group_order=target.group_order,
    )
    replaced_ids = {
        figure_id for figure_id in (previous_id, figure.figure_id) if figure_id is not None
    }
    # A manual replacement has no model decision for the new figure; the old
    # trace would misattribute the choice (and an assembled figure may not
    # carry a non-included decision). Prior artifact versions retain history.
    revised = replace(
        replace_element(ctx.base_enrichment, ref, replacement),
        figure_decisions=tuple(
            decision
            for decision in ctx.base_enrichment.figure_decisions
            if decision.figure_id not in replaced_ids
        ),
    )
    revised = _without_stale_annotations(ctx.synthesis, revised)
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
                "element_kind": _FIGURE.value,
                "element_key": patch.element_key,
                "figure_id": str(patch.figure_id),
                "figure_handle": entry.handle,
                "previous_figure_id": _public_figure_id(ctx, previous_id),
            },
        ),
    )


__all__ = ["plan_replace_source_figure"]
