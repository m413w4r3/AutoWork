"""F2-03b: typed in-place revision of an existing BAR chart.

Every numeric value is re-proven against the exact admitted FACT/EVENT record
that documents both the number and its unit. A revision replaces the chart in
place, keeps its key and placement, and never creates a second element.
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from uuid import UUID

import pytest

from cti_app.application.editorial_visual_revision import (
    VisualRevisionRejected,
    plan_visual_revision,
)
from cti_app.domain.editorial_enrichment_patch import (
    EditorialEnrichmentPatchError,
    ReviseChartPatch,
    visual_patch_from_json,
    visual_patch_to_json,
)
from cti_app.domain.production_editorial_enrichment import (
    ChartSpecV1,
)
from tests.editorial_visual_revision_support import (
    ALPHA_VALUE,
    BETA_VALUE,
    OTHER_ANCHOR,
    anchored,
    bar_chart,
    enrichment_with,
    quantitative_extraction,
    quantitative_synthesis,
    visual_context,
)


def _fixture(
    *,
    extra_facts: tuple[tuple[str, str], ...] = (),
    chart: ChartSpecV1 | None = None,
    admitted_refs=None,
    evidence_pack_required: bool = True,
):
    extraction, refs = quantitative_extraction(extra_facts=extra_facts)
    synthesis = quantitative_synthesis(extraction)
    base_chart = chart or bar_chart(
        refs=refs,
        compiled_asset_id=UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
    )
    enrichment = enrichment_with(extraction=extraction, synthesis=synthesis, charts=(base_chart,))
    context = visual_context(
        enrichment=enrichment,
        extraction=extraction,
        synthesis=synthesis,
        admitted_refs=admitted_refs,
        evidence_pack_required=evidence_pack_required,
    )
    return context, base_chart, refs


def _reject(code: str, plan) -> VisualRevisionRejected:
    with pytest.raises(VisualRevisionRejected) as exc_info:
        plan()
    assert exc_info.value.code == code
    return exc_info.value


def test_label_only_revision_is_accepted_without_new_evidence() -> None:
    context, base, _refs = _fixture()
    revised = replace(
        base,
        title="Documented endpoint counts, revised",
        caption="Revised synthetic caption.",
        category_axis_label="Documented category",
        value_axis_label="Observed endpoints",
        points=(
            replace(base.points[0], label="Alpha (revised)", series="Endpoints"),
            replace(base.points[1], label="Beta (revised)", series="Endpoints"),
        ),
    )
    plan = plan_visual_revision(context, ReviseChartPatch(element_key=base.key, chart=revised))

    assert plan.compile_chart is not None
    assert plan.compile_chart.compiled_asset_id is None
    assert plan.compile_chart.title == revised.title
    assert plan.materialize_diagram is None
    assert len(plan.revised_enrichment.charts) == 1
    stored = plan.revised_enrichment.charts[0]
    assert stored.key == base.key
    assert stored.placement == base.placement
    assert stored == plan.compile_chart
    assert plan.revised_enrichment.diagrams == context.base_enrichment.diagrams
    assert plan.preview["operation"] == "rewrite_chart"
    assert plan.preview["element_key"] == base.key


@pytest.mark.parametrize("field", ("category_axis_label", "value_axis_label"))
def test_axis_labels_follow_the_editorial_text_policy(field: str) -> None:
    context, base, _refs = _fixture()
    revised = replace(base, **{field: "<b>Documented count</b>"})
    error = _reject(
        "enrichment_revision_patch_invalid",
        lambda: plan_visual_revision(
            context, ReviseChartPatch(element_key=base.key, chart=revised)
        ),
    )
    assert error.diagnostics["reason_code"] == "bar_chart_text_policy_violation"
    assert error.diagnostics["field"] == field
    assert error.diagnostics["diagnostic_code"] == "plain_text_violation"


def test_unit_follows_the_editorial_text_policy_after_same_record_proof() -> None:
    unit = "http://example.invalid/endpoints"
    alpha_fact = f"Alpha: 12 {unit}"
    beta_fact = f"Beta: 20 {unit}"
    context, base, refs = _fixture(
        extra_facts=(
            (alpha_fact, f"The revised report records Alpha with 12 {unit}."),
            (beta_fact, f"The revised report records Beta with 20 {unit}."),
        )
    )
    revised = replace(
        base,
        unit=unit,
        points=(
            replace(
                base.points[0],
                value=Decimal("12"),
                evidence_refs=(refs[alpha_fact],),
            ),
            replace(
                base.points[1],
                value=Decimal("20"),
                evidence_refs=(refs[beta_fact],),
            ),
        ),
        purpose=replace(
            base.purpose,
            evidence_refs=(refs[alpha_fact], refs[beta_fact]),
        ),
    )

    error = _reject(
        "enrichment_revision_patch_invalid",
        lambda: plan_visual_revision(
            context, ReviseChartPatch(element_key=base.key, chart=revised)
        ),
    )

    assert error.diagnostics["reason_code"] == "bar_chart_text_policy_violation"
    assert error.diagnostics["field"] == "unit"


def test_value_change_proven_by_a_fresh_admitted_record_is_accepted() -> None:
    context, base, refs = _fixture(
        extra_facts=(("Beta: 20 endpoints", "The revised report records Beta with 20 endpoints."),)
    )
    revised_points = (
        base.points[0],
        replace(base.points[1], value=Decimal("20"), evidence_refs=(refs["Beta: 20 endpoints"],)),
    )
    revised = replace(
        base,
        points=revised_points,
        purpose=replace(
            base.purpose,
            evidence_refs=(refs[ALPHA_VALUE], refs["Beta: 20 endpoints"]),
        ),
    )
    plan = plan_visual_revision(context, ReviseChartPatch(element_key=base.key, chart=revised))

    assert plan.compile_chart is not None
    stored = plan.revised_enrichment.charts[0]
    assert stored.points[1].value == Decimal("20")
    assert stored.points[1].evidence_refs == (refs["Beta: 20 endpoints"],)


def test_value_change_without_fresh_proof_is_refused() -> None:
    context, base, _refs = _fixture()
    revised = replace(
        base,
        points=(base.points[0], replace(base.points[1], value=Decimal("21"))),
    )
    error = _reject(
        "bar_chart_value_not_evidenced",
        lambda: plan_visual_revision(
            context, ReviseChartPatch(element_key=base.key, chart=revised)
        ),
    )
    assert error.diagnostics["element_key"] == base.key


def test_unit_change_requires_the_same_record_to_document_value_and_unit() -> None:
    context, base, _refs = _fixture()
    revised = replace(base, unit="bytes")
    error = _reject(
        "bar_chart_value_not_evidenced",
        lambda: plan_visual_revision(
            context, ReviseChartPatch(element_key=base.key, chart=revised)
        ),
    )
    assert error.diagnostics["element_key"] == base.key


def test_value_and_unit_must_share_the_same_record() -> None:
    # One record states "12 endpoints"; a second states "Beta 18". Citing both
    # never proves a displayed point unless ONE of them holds number and unit.
    context, base, refs = _fixture()
    alpha_ref, beta_ref = refs[ALPHA_VALUE], refs[BETA_VALUE]
    revised = replace(
        base,
        points=(
            replace(base.points[0], evidence_refs=(alpha_ref, beta_ref)),
            replace(
                base.points[1],
                value=Decimal("12"),
                evidence_refs=(beta_ref, alpha_ref),
            ),
        ),
    )
    plan = plan_visual_revision(context, ReviseChartPatch(element_key=base.key, chart=revised))
    # Both points remain proven: the Alpha record holds exactly "12 endpoints".
    assert plan.revised_enrichment.charts[0].points[1].value == Decimal("12")

    # A record that states a number without the displayed unit can never prove
    # it, even when another cited record mentions the unit alone.
    extraction, refs = quantitative_extraction(
        extra_facts=(("Gamma count 7", "The report lists a Gamma count of 7."),)
    )
    synthesis = quantitative_synthesis(extraction)
    chart = bar_chart(
        refs=refs,
        values=(
            ("Alpha", "12", refs[ALPHA_VALUE]),
            ("Gamma", "7", refs["Gamma count 7"]),
        ),
    )
    gamma_context = visual_context(
        enrichment=enrichment_with(extraction=extraction, synthesis=synthesis, charts=(chart,)),
        extraction=extraction,
        synthesis=synthesis,
    )
    _reject(
        "bar_chart_value_not_evidenced",
        lambda: plan_visual_revision(
            gamma_context,
            ReviseChartPatch(element_key=chart.key, chart=replace(chart, title="Gamma retitled")),
        ),
    )


def test_unadmitted_evidence_is_refused() -> None:
    context, base, refs = _fixture(
        extra_facts=(("Beta: 20 endpoints", "The revised report records Beta with 20 endpoints."),),
        admitted_refs=None,
    )
    # Only the base chart's own records are admitted; the fresh Beta record is
    # present in the extraction but was never admitted for this artifact.
    from tests.editorial_visual_revision_support import evidence_pack

    context = replace(
        context,
        evidence_pack=evidence_pack((refs[ALPHA_VALUE], refs[BETA_VALUE])),
    )
    revised = replace(
        base,
        points=(
            base.points[0],
            replace(
                base.points[1],
                value=Decimal("20"),
                evidence_refs=(refs["Beta: 20 endpoints"],),
            ),
        ),
        purpose=replace(base.purpose, evidence_refs=(refs[ALPHA_VALUE],)),
        title="Retitled with an unadmitted value",
    )
    _reject(
        "bar_chart_insufficient_data",
        lambda: plan_visual_revision(
            context, ReviseChartPatch(element_key=base.key, chart=revised)
        ),
    )


def test_exact_decimal_values_are_not_rounded() -> None:
    context, _base, refs = _fixture(
        extra_facts=(
            (
                "Delta ratio: 0.10000000000000001 endpoints",
                "The report records a Delta ratio of 0.10000000000000001 endpoints in total.",
            ),
        )
    )
    exact = Decimal("0.10000000000000001")
    chart = bar_chart(
        refs=refs,
        values=(
            ("Alpha", "12", refs[ALPHA_VALUE]),
            ("Beta", "18", refs[BETA_VALUE]),
            ("Delta", format(exact, "f"), refs["Delta ratio: 0.10000000000000001 endpoints"]),
        ),
    )
    context = visual_context(
        enrichment=enrichment_with(
            extraction=context.extraction, synthesis=context.synthesis, charts=(chart,)
        ),
        extraction=context.extraction,
        synthesis=context.synthesis,
    )
    revised = replace(chart, title="Exact decimal revision")
    plan = plan_visual_revision(context, ReviseChartPatch(element_key=chart.key, chart=revised))
    stored = plan.revised_enrichment.charts[0].points[2].value
    assert stored == exact
    assert stored != Decimal("0.1")

    rounded_down = replace(
        chart,
        points=(
            chart.points[0],
            chart.points[1],
            replace(chart.points[2], value=Decimal("0.1")),
        ),
    )
    _reject(
        "bar_chart_value_not_evidenced",
        lambda: plan_visual_revision(
            context, ReviseChartPatch(element_key=chart.key, chart=rounded_down)
        ),
    )


def test_duplicate_analytic_question_is_refused_but_the_target_own_question_is_excluded() -> None:
    extraction, refs = quantitative_extraction()
    synthesis = quantitative_synthesis(extraction)
    first = bar_chart(refs=refs, key="chart_one")
    second = bar_chart(
        refs=refs,
        key="chart_two",
        question="Which other synthetic question is documented?",
    )
    context = visual_context(
        enrichment=enrichment_with(
            extraction=extraction, synthesis=synthesis, charts=(first, second)
        ),
        extraction=extraction,
        synthesis=synthesis,
    )
    # Reusing the target's own question is not a duplicate.
    same_question = replace(first, title="Retitled chart")
    plan_visual_revision(context, ReviseChartPatch(element_key=first.key, chart=same_question))

    colliding = replace(first, purpose=replace(first.purpose, question=second.purpose.question))
    _reject(
        "enrichment_revision_patch_invalid",
        lambda: plan_visual_revision(
            context, ReviseChartPatch(element_key=first.key, chart=colliding)
        ),
    )


def test_no_op_revision_is_refused() -> None:
    context, base, _refs = _fixture()
    unchanged = replace(base, compiled_asset_id=None)
    _reject(
        "enrichment_revision_no_change",
        lambda: plan_visual_revision(
            context, ReviseChartPatch(element_key=base.key, chart=unchanged)
        ),
    )


def test_compiled_asset_identity_does_not_create_a_change() -> None:
    context, base, _refs = _fixture()
    # A copied compiled asset id is re-derived at prepare/adoption; it must not
    # turn an otherwise identical spec into a revision.
    _reject(
        "enrichment_revision_no_change",
        lambda: plan_visual_revision(context, ReviseChartPatch(element_key=base.key, chart=base)),
    )


def test_placement_change_is_refused() -> None:
    context, base, _refs = _fixture()
    moved = replace(base, placement=anchored(OTHER_ANCHOR))
    _reject(
        "enrichment_placement_changed",
        lambda: plan_visual_revision(context, ReviseChartPatch(element_key=base.key, chart=moved)),
    )


def test_revision_rejects_a_non_bar_or_missing_target() -> None:
    from dataclasses import replace as dataclass_replace

    from tests.test_editorial_visual_revision import _table, _timeline_chart

    context, base, _refs = _fixture()
    timeline = replace(_timeline_chart(), key=base.key, placement=base.placement)
    _reject(
        "enrichment_element_kind_mismatch",
        lambda: plan_visual_revision(
            context, ReviseChartPatch(element_key=base.key, chart=timeline)
        ),
    )
    _reject(
        "enrichment_element_not_found",
        lambda: plan_visual_revision(
            context,
            ReviseChartPatch(
                element_key="missing_chart",
                chart=replace(base, key="missing_chart"),
            ),
        ),
    )
    with_table = dataclass_replace(
        context,
        base_enrichment=dataclass_replace(
            context.base_enrichment,
            tables=(_table("table_one", anchored(), "Which command is documented?"),),
        ),
    )
    _reject(
        "enrichment_element_kind_mismatch",
        lambda: plan_visual_revision(
            with_table,
            ReviseChartPatch(
                element_key="table_one",
                chart=replace(base, key="table_one"),
            ),
        ),
    )


def test_markup_and_semantic_policy_are_enforced() -> None:
    context, base, _refs = _fixture()
    with_url = replace(base, title="See https://example.test/report")
    _reject(
        "enrichment_revision_patch_invalid",
        lambda: plan_visual_revision(
            context, ReviseChartPatch(element_key=base.key, chart=with_url)
        ),
    )

    bad_caption = replace(base, caption="Contact mailto:user@example.test")
    _reject(
        "editorial_caption_text_violation",
        lambda: plan_visual_revision(
            context, ReviseChartPatch(element_key=base.key, chart=bad_caption)
        ),
    )

    # A point label must not smuggle an ungrounded technical literal.
    bad_label = replace(
        base,
        points=(
            replace(base.points[0], label="C2 at 203.0.113.9"),
            base.points[1],
        ),
    )
    _reject(
        "enrichment_revision_patch_invalid",
        lambda: plan_visual_revision(
            context, ReviseChartPatch(element_key=base.key, chart=bad_label)
        ),
    )


def test_missing_evidence_pack_is_refused_instead_of_trusting_the_patch() -> None:
    context, base, _refs = _fixture(evidence_pack_required=False)
    revised = replace(base, title="Retitled without admitted evidence")
    _reject(
        "enrichment_revision_patch_invalid",
        lambda: plan_visual_revision(
            context, ReviseChartPatch(element_key=base.key, chart=revised)
        ),
    )


def test_chart_patch_codec_round_trips_exact_values() -> None:
    _extraction, refs = quantitative_extraction()
    chart = bar_chart(refs=refs)
    patch = ReviseChartPatch(element_key=chart.key, chart=chart)

    payload = visual_patch_to_json(patch)
    assert payload["kind"] == "revise_chart"
    assert payload["element_key"] == chart.key
    assert payload["chart"]["points"][0]["value"] == "12"
    assert "compiled_asset_id" not in payload["chart"]
    assert visual_patch_from_json(payload) == patch

    with pytest.raises(EditorialEnrichmentPatchError):
        visual_patch_from_json({"kind": "revise_chart", "element_key": chart.key})
