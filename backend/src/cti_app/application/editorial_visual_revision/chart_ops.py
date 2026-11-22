"""In-place BAR chart revision proven against exact admitted evidence.

A revision never creates a second element: it replaces one existing BAR chart
specification, keeps its key and placement, re-proves every displayed
(value, unit) against the same admitted FACT/EVENT record, and leaves the
compiled SVG to the deterministic renderer (prepare preview, adoption under the
lock). Labels, title, axis labels, series order and category order may change
without new evidence, but they still pass the editorial markup/semantic policy.
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
    bar_chart_record_documents_value_and_unit,
    editorial_technical_support,
    validate_editorial_enrichment,
)
from cti_app.application.production_synthesis import _all_evidence_entries
from cti_app.domain.editorial_enrichment_patch import ReviseChartPatch
from cti_app.domain.production_editorial_enrichment import (
    ChartKind,
    ChartSpecV1,
    DiagramSpecV1,
    EditorialEnrichmentElementKind,
    EnrichmentElementRef,
    TableSpecV1,
    normalize_analytic_question,
)
from cti_app.domain.production_synthesis import (
    EvidenceKind,
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


def _check_text_policy(
    chart: ChartSpecV1,
    *,
    entries: Mapping[ExtractionEvidenceRefV1, Mapping[str, Any]],
    technical_support: Mapping[tuple[str, str], set[ExtractionEvidenceRefV1]],
    purpose_refs: tuple[ExtractionEvidenceRefV1, ...],
    chart_refs: tuple[ExtractionEvidenceRefV1, ...],
) -> None:
    if chart.caption is not None and editorial_caption_text_violation(chart.caption):
        raise VisualRevisionRejected(
            "editorial_caption_text_violation",
            {"element_kind": EditorialEnrichmentElementKind.CHART.value, "element_key": chart.key},
        )
    grounded_text = (
        ("title", chart.title, chart_refs),
        ("caption", chart.caption or "", chart_refs),
        ("purpose.question", chart.purpose.question, purpose_refs),
        ("purpose.available_data", chart.purpose.available_data, purpose_refs),
        ("purpose.comprehension_gain", chart.purpose.comprehension_gain, purpose_refs),
        ("purpose.scope", chart.purpose.scope, purpose_refs),
        ("purpose.knowledge_limits", chart.purpose.knowledge_limits, purpose_refs),
        ("purpose.placement_reason", chart.purpose.placement_reason, purpose_refs),
    )
    for field_name, value, refs in grounded_text:
        try:
            _validate_grounded_editorial_text(
                value,
                refs,
                entries,
                technical_support,
                context=f"chart:{chart.key}:{field_name}",
            )
        except EditorialEnrichmentProposalControlError as error:
            raise patch_invalid(
                "bar_chart_text_policy_violation",
                element_key=chart.key,
                field=field_name,
                diagnostic_code=str(error.diagnostic_code or error.code.value),
            ) from None
    for index, point in enumerate(chart.points):
        for field_name, value in (("label", point.label), ("series", point.series)):
            try:
                _validate_ungrounded_editorial_text(
                    value, context=f"chart:{chart.key}:point.{index}.{field_name}"
                )
            except EditorialEnrichmentProposalControlError as error:
                raise patch_invalid(
                    "bar_chart_text_policy_violation",
                    element_key=chart.key,
                    field=f"point.{index}.{field_name}",
                    diagnostic_code=str(error.diagnostic_code or error.code.value),
                ) from None


def _check_evidence(
    chart: ChartSpecV1,
    *,
    entries: Mapping[ExtractionEvidenceRefV1, Mapping[str, Any]],
    admitted_refs: frozenset[ExtractionEvidenceRefV1],
) -> None:
    if not (chart.unit or "").strip():
        raise patch_invalid("bar_chart_unit_missing", element_key=chart.key)
    known_refs = set(entries)
    for index, point in enumerate(chart.points):
        if point.value is None or not point.evidence_refs:
            raise VisualRevisionRejected(
                "bar_chart_insufficient_data",
                {
                    "element_key": chart.key,
                    "point_index": index,
                    "reason_code": "bar_chart_value_missing",
                },
            )
        if any(ref not in admitted_refs or ref not in known_refs for ref in point.evidence_refs):
            raise VisualRevisionRejected(
                "bar_chart_insufficient_data",
                {
                    "element_key": chart.key,
                    "point_index": index,
                    "reason_code": "bar_chart_evidence_unadmitted",
                },
            )
        proven = any(
            bar_chart_record_documents_value_and_unit(
                entries[ref],
                value=point.value,
                unit=chart.unit,
            )
            for ref in point.evidence_refs
            if ref.kind in {EvidenceKind.FACT, EvidenceKind.EVENT}
        )
        if not proven:
            # Same-record proof: the displayed number and its unit must appear
            # in one admitted FACT/EVENT record, exactly (no rounding).
            raise VisualRevisionRejected(
                "bar_chart_value_not_evidenced",
                {"element_key": chart.key, "point_index": index},
            )


def _check_analytic_question(
    ctx: VisualRevisionContext,
    chart: ChartSpecV1,
    *,
    target_key: str,
) -> None:
    question = normalize_analytic_question(chart.purpose.question)
    elements: tuple[TableSpecV1 | ChartSpecV1 | DiagramSpecV1, ...] = (
        *ctx.base_enrichment.tables,
        *ctx.base_enrichment.charts,
        *ctx.base_enrichment.diagrams,
    )
    for element in elements:
        if isinstance(element, ChartSpecV1) and element.key == target_key:
            continue
        element_purpose = element.purpose
        if element_purpose is None:
            continue
        if normalize_analytic_question(element_purpose.question) == question:
            raise patch_invalid(
                "analytic_question_duplicate",
                element_key=target_key,
                conflicting_key=element.key,
            )


def plan_chart_revision(
    ctx: VisualRevisionContext,
    patch: ReviseChartPatch,
) -> VisualRevisionPlan:
    ref = EnrichmentElementRef(EditorialEnrichmentElementKind.CHART, patch.element_key)
    base = find_element(ctx.base_enrichment, ref)
    if not isinstance(base, ChartSpecV1):
        raise VisualRevisionRejected(
            "enrichment_element_kind_mismatch",
            {"element_kind": EditorialEnrichmentElementKind.CHART.value, "element_key": base.key},
        )
    chart = patch.chart
    if base.kind is not ChartKind.BAR or chart.kind is not ChartKind.BAR:
        # The slice revises quantitative BAR charts only; a timeline rewrite is
        # a representation change, not a typed correction of the same element.
        raise VisualRevisionRejected(
            "enrichment_element_kind_mismatch",
            {
                "element_kind": EditorialEnrichmentElementKind.CHART.value,
                "element_key": patch.element_key,
                "reason_code": "chart_kind_not_bar",
            },
        )
    if chart.placement != base.placement:
        raise VisualRevisionRejected(
            "enrichment_placement_changed",
            {
                "element_kind": EditorialEnrichmentElementKind.CHART.value,
                "element_key": patch.element_key,
            },
        )
    if chart.group_order is not None and chart.group_order != base.group_order:
        raise patch_invalid(
            "element_order_changed",
            element_kind=EditorialEnrichmentElementKind.CHART.value,
            element_key=patch.element_key,
        )
    normalized = replace(
        chart,
        placement=base.placement,
        group_order=base.group_order,
        compiled_asset_id=None,
    )
    if replace(normalized, compiled_asset_id=base.compiled_asset_id) == base:
        raise VisualRevisionRejected(
            "enrichment_revision_no_change",
            {
                "element_kind": EditorialEnrichmentElementKind.CHART.value,
                "element_key": patch.element_key,
            },
        )

    admitted_refs = admitted_evidence_refs(ctx)
    entries = _all_evidence_entries(ctx.extraction)
    technical_support = editorial_technical_support(entries)
    point_refs = {ref for point in normalized.points for ref in point.evidence_refs}
    purpose_refs = normalized.purpose.evidence_refs
    if not purpose_refs:
        raise patch_invalid("bar_chart_purpose_evidence_missing", element_key=patch.element_key)
    if any(ref not in admitted_refs for ref in purpose_refs):
        raise patch_invalid("bar_chart_purpose_evidence_unadmitted", element_key=patch.element_key)
    if not set(purpose_refs) <= point_refs:
        raise patch_invalid(
            "chart_purpose_evidence_not_used_by_points", element_key=patch.element_key
        )
    _check_analytic_question(ctx, normalized, target_key=patch.element_key)
    # Evidence first: every ref is then known to exist in the extraction before
    # the text-policy grounding walk indexes those records.
    _check_evidence(normalized, entries=entries, admitted_refs=admitted_refs)
    _check_text_policy(
        normalized,
        entries=entries,
        technical_support=technical_support,
        purpose_refs=tuple(sorted(purpose_refs, key=evidence_ref_sort_key)),
        chart_refs=tuple(sorted(point_refs, key=evidence_ref_sort_key)),
    )

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
                "element_kind": EditorialEnrichmentElementKind.CHART.value,
                "element_key": patch.element_key,
                "title": normalized.title,
                "caption": normalized.caption,
                "unit": normalized.unit,
                "point_count": len(normalized.points),
                "points": [
                    {
                        "label": point.label,
                        "series": point.series,
                        "value": format(point.value, "f") if point.value is not None else None,
                    }
                    for point in normalized.points
                ],
            },
        ),
        compile_chart=normalized,
    )


__all__ = ["plan_chart_revision"]
