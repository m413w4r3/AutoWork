"""F2-03b: typed in-place revision of an existing diagram through the D2 compiler.

A revision may rename labels, drop associations or reorder presentation without
new evidence. Adding a node or an edge requires evidence already admitted for
that diagram; the purpose evidence is preserved. The canonical SVG is
recompiled at adoption, never rendered from arbitrary Bridge output.
"""

from __future__ import annotations

from dataclasses import replace
from uuid import UUID

import pytest

from cti_app.application.editorial_visual_revision import (
    VisualRevisionRejected,
    plan_visual_revision,
)
from cti_app.domain.editorial_enrichment_patch import (
    EditorialEnrichmentPatchError,
    ReviseDiagramPatch,
    visual_patch_from_json,
    visual_patch_to_json,
)
from cti_app.domain.production_editorial_enrichment import (
    DiagramEdgeV1,
    DiagramNodeV1,
    DiagramSpecV1,
    EnrichmentPlacementKind,
    EnrichmentPlacementV1,
)
from tests.editorial_visual_revision_support import (
    ALPHA_VALUE,
    BETA_VALUE,
    OTHER_ANCHOR,
    bar_chart,
    enrichment_with,
    flow_diagram,
    quantitative_extraction,
    quantitative_synthesis,
    visual_context,
)


def _fixture(*, diagram: DiagramSpecV1 | None = None, admitted_refs=None):
    extraction, refs = quantitative_extraction()
    synthesis = quantitative_synthesis(extraction)
    base_diagram = diagram or flow_diagram(
        refs=refs,
        compiled_asset_id=UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"),
    )
    enrichment = enrichment_with(
        extraction=extraction, synthesis=synthesis, diagrams=(base_diagram,)
    )
    context = visual_context(
        enrichment=enrichment,
        extraction=extraction,
        synthesis=synthesis,
        admitted_refs=admitted_refs,
    )
    return context, base_diagram, refs


def _reject(code: str, plan) -> VisualRevisionRejected:
    with pytest.raises(VisualRevisionRejected) as exc_info:
        plan()
    assert exc_info.value.code == code
    return exc_info.value


def test_label_only_diagram_revision_is_accepted_and_recompiled() -> None:
    context, base, _refs = _fixture()
    revised = replace(
        base,
        title="Documented flow, revised",
        caption="Revised synthetic flow caption.",
        nodes=(
            replace(base.nodes[0], label="Alpha loader"),
            replace(base.nodes[1], label="Beta payload"),
        ),
        edges=(replace(base.edges[0], label="transfers to"),),
    )
    plan = plan_visual_revision(context, ReviseDiagramPatch(element_key=base.key, diagram=revised))

    assert plan.materialize_diagram is not None
    assert plan.materialize_diagram.compiled_asset_id is None
    assert plan.compile_chart is None
    assert len(plan.revised_enrichment.diagrams) == 1
    stored = plan.revised_enrichment.diagrams[0]
    assert stored.key == base.key
    assert stored.placement == base.placement
    assert stored == plan.materialize_diagram
    assert plan.preview["operation"] == "rewrite_diagram"
    assert plan.preview["element_key"] == base.key


def test_removing_an_association_is_accepted_without_new_evidence() -> None:
    extraction, refs = quantitative_extraction()
    synthesis = quantitative_synthesis(extraction)
    base = flow_diagram(refs=refs)
    base = replace(
        base,
        nodes=(
            *base.nodes,
            DiagramNodeV1("relay", "Alpha relay", (refs[ALPHA_VALUE],)),
        ),
        edges=(
            *base.edges,
            DiagramEdgeV1("payload", "relay", "reaches", (refs[BETA_VALUE],)),
        ),
    )
    context = visual_context(
        enrichment=enrichment_with(extraction=extraction, synthesis=synthesis, diagrams=(base,)),
        extraction=extraction,
        synthesis=synthesis,
    )
    reduced = replace(base, edges=(base.edges[0],))
    plan = plan_visual_revision(context, ReviseDiagramPatch(element_key=base.key, diagram=reduced))
    assert plan.revised_enrichment.diagrams[0].edges == (base.edges[0],)


def test_adding_a_node_with_evidence_already_admitted_for_the_diagram_is_accepted() -> None:
    context, base, refs = _fixture()
    extended = replace(
        base,
        nodes=(*base.nodes, DiagramNodeV1("relay", "Alpha relay", (refs[ALPHA_VALUE],))),
        edges=(
            *base.edges,
            DiagramEdgeV1("payload", "relay", "reaches", (refs[BETA_VALUE],)),
        ),
    )
    plan = plan_visual_revision(context, ReviseDiagramPatch(element_key=base.key, diagram=extended))
    assert [node.node_id for node in plan.revised_enrichment.diagrams[0].nodes] == [
        "loader",
        "payload",
        "relay",
    ]


def test_adding_a_node_with_unadmitted_evidence_is_refused() -> None:
    # The current pack admits Gamma, but the base diagram never cited it: a
    # revision may not introduce a new association for a foreign record.
    extraction, refs = quantitative_extraction(
        extra_facts=(("Gamma: 7 endpoints", "The report records Gamma with 7 endpoints."),)
    )
    synthesis = quantitative_synthesis(extraction)
    base = flow_diagram(refs=refs)
    context = visual_context(
        enrichment=enrichment_with(extraction=extraction, synthesis=synthesis, diagrams=(base,)),
        extraction=extraction,
        synthesis=synthesis,
    )
    extended = replace(
        base,
        nodes=(
            *base.nodes,
            DiagramNodeV1("gamma", "Gamma", (refs["Gamma: 7 endpoints"],)),
        ),
        edges=(
            *base.edges,
            DiagramEdgeV1("payload", "gamma", "reaches", (refs["Gamma: 7 endpoints"],)),
        ),
    )
    error = _reject(
        "enrichment_revision_patch_invalid",
        lambda: plan_visual_revision(
            context, ReviseDiagramPatch(element_key=base.key, diagram=extended)
        ),
    )
    assert error.diagnostics["reason_code"] == "diagram_evidence_not_previously_admitted"


def test_purpose_evidence_must_be_preserved_and_used_by_the_graph() -> None:
    # Profiled diagrams always carry their purpose; the typed patch refuses an
    # incoherent profile-without-purpose spec before the planner.
    context, base, refs = _fixture()
    with pytest.raises(ValueError, match="analytic purpose"):
        ReviseDiagramPatch(element_key=base.key, diagram=replace(base, purpose=None))

    # Dropping both the purpose and the profile is a settable typed patch; the
    # planner still refuses the lost analytic grounding before any canonical
    # write.
    _reject(
        "enrichment_revision_patch_invalid",
        lambda: plan_visual_revision(
            context,
            ReviseDiagramPatch(
                element_key=base.key,
                diagram=replace(base, purpose=None, profile=None),
            ),
        ),
    )

    new_purpose = replace(
        base,
        purpose=replace(base.purpose, evidence_refs=(refs[BETA_VALUE],)),
    )
    _reject(
        "enrichment_revision_patch_invalid",
        lambda: plan_visual_revision(
            context, ReviseDiagramPatch(element_key=base.key, diagram=new_purpose)
        ),
    )

    # Keeping the base purpose evidence while the graph still cites it is the
    # accepted case (covered by the label-only revision test): the same refs
    # are used by the loader node.
    kept = replace(base, title="Retitled while keeping the purpose evidence")
    plan = plan_visual_revision(context, ReviseDiagramPatch(element_key=base.key, diagram=kept))
    assert plan.revised_enrichment.diagrams[0].purpose.evidence_refs == (base.purpose.evidence_refs)


def test_v8_diagram_revision_requires_a_profile_and_a_caption() -> None:
    context, base, _refs = _fixture()
    _reject(
        "enrichment_revision_patch_invalid",
        lambda: plan_visual_revision(
            context,
            ReviseDiagramPatch(
                element_key=base.key,
                diagram=replace(base, caption=None, title="Retitled without caption"),
            ),
        ),
    )


def test_diagram_no_op_is_refused() -> None:
    context, base, _refs = _fixture()
    _reject(
        "enrichment_revision_no_change",
        lambda: plan_visual_revision(
            context,
            ReviseDiagramPatch(element_key=base.key, diagram=replace(base, compiled_asset_id=None)),
        ),
    )
    _reject(
        "enrichment_revision_no_change",
        lambda: plan_visual_revision(
            context, ReviseDiagramPatch(element_key=base.key, diagram=base)
        ),
    )


def test_placement_or_order_change_is_refused() -> None:
    context, base, _refs = _fixture()
    moved = replace(
        base,
        placement=EnrichmentPlacementV1(
            EnrichmentPlacementKind.AFTER_PARAGRAPH, anchor_block_id=OTHER_ANCHOR
        ),
    )
    _reject(
        "enrichment_placement_changed",
        lambda: plan_visual_revision(
            context, ReviseDiagramPatch(element_key=base.key, diagram=moved)
        ),
    )
    reordered = replace(base, group_order=2, title="Retitled")
    _reject(
        "enrichment_revision_patch_invalid",
        lambda: plan_visual_revision(
            context, ReviseDiagramPatch(element_key=base.key, diagram=reordered)
        ),
    )


def test_revision_rejects_a_missing_or_mismatched_target() -> None:
    context, base, refs = _fixture()
    _reject(
        "enrichment_element_not_found",
        lambda: plan_visual_revision(
            context,
            ReviseDiagramPatch(
                element_key="missing_diagram",
                diagram=replace(base, key="missing_diagram"),
            ),
        ),
    )
    chart = bar_chart(refs=refs)
    with_chart = replace(
        context,
        base_enrichment=enrichment_with(
            extraction=context.extraction,
            synthesis=context.synthesis,
            charts=(chart,),
            diagrams=(base,),
        ),
    )
    _reject(
        "enrichment_element_kind_mismatch",
        lambda: plan_visual_revision(
            with_chart,
            ReviseDiagramPatch(
                element_key=chart.key,
                diagram=replace(base, key=chart.key),
            ),
        ),
    )


def test_diagram_text_policy_is_enforced() -> None:
    context, base, _refs = _fixture()
    with_url = replace(base, title="See https://example.test/report")
    _reject(
        "enrichment_revision_patch_invalid",
        lambda: plan_visual_revision(
            context, ReviseDiagramPatch(element_key=base.key, diagram=with_url)
        ),
    )
    bad_caption = replace(base, caption="Contact mailto:user@example.test")
    _reject(
        "editorial_caption_text_violation",
        lambda: plan_visual_revision(
            context, ReviseDiagramPatch(element_key=base.key, diagram=bad_caption)
        ),
    )
    bad_label = replace(
        base,
        nodes=(
            replace(base.nodes[0], label="C2 at 203.0.113.9"),
            base.nodes[1],
        ),
    )
    _reject(
        "enrichment_revision_patch_invalid",
        lambda: plan_visual_revision(
            context, ReviseDiagramPatch(element_key=base.key, diagram=bad_label)
        ),
    )


def test_diagram_patch_codec_round_trips() -> None:
    _extraction, refs = quantitative_extraction()
    diagram = flow_diagram(refs=refs)
    patch = ReviseDiagramPatch(element_key=diagram.key, diagram=diagram)

    payload = visual_patch_to_json(patch)
    assert payload["kind"] == "revise_diagram"
    assert payload["element_key"] == diagram.key
    assert payload["diagram"]["purpose"]["evidence_refs"]
    assert "compiled_asset_id" not in payload["diagram"]
    assert visual_patch_from_json(payload) == patch

    with pytest.raises(EditorialEnrichmentPatchError):
        visual_patch_from_json({"kind": "revise_diagram", "element_key": diagram.key})
