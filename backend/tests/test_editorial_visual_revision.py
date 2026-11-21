from __future__ import annotations

import hashlib
from dataclasses import replace
from uuid import UUID

import pytest

from cti_app.application.editorial_visual_revision import (
    VisualRevisionContext,
    VisualRevisionRejected,
    enrichment_order_group_hash,
    plan_visual_revision,
)
from cti_app.domain.editorial_enrichment_patch import (
    EditorialEnrichmentPatchError,
    MoveEnrichmentElementPatch,
    NotImplementedForSlice,
    RemoveEnrichmentElementPatch,
    ReorderEnrichmentElementsPatch,
    ReplaceSourceFigurePatch,
    ReviseChartPatch,
    ReviseDiagramPatch,
    UpdateVisualCaptionPatch,
    visual_patch_from_json,
    visual_patch_to_json,
)
from cti_app.domain.editorial_enrichment_patch import (
    enrichment_order_group_hash as domain_group_hash,
)
from cti_app.domain.production_editorial_enrichment import (
    EDITORIAL_ENRICHMENT_MAX_SCHEMA_VERSION,
    EDITORIAL_ENRICHMENT_V11_POLICY_VERSION,
    ChartKind,
    ChartPointV1,
    ChartSpecV1,
    DiagramEdgeV1,
    DiagramNodeV1,
    DiagramProfile,
    DiagramSpecV1,
    EditorialAnalyticPurposeV1,
    EditorialEnrichmentElementKind,
    EnrichmentDiagramDirection,
    EnrichmentDiagramKind,
    EnrichmentElementRef,
    EnrichmentPlacementKind,
    EnrichmentPlacementV1,
    EnrichmentTableKind,
    SourceFigureCandidateV1,
    SourceFigureInclusionStatus,
    SourceFigureLocatorV1,
    TableColumnV1,
    TableRowV1,
    TableSpecV1,
    editorial_enrichment_to_json,
    enrichment_element_order,
)
from cti_app.domain.production_synthesis import extraction_evidence_refs_v1
from tests.editorial_enrichment_support import build_empty_editorial_enrichment
from tests.test_production_editorial_enrichment import _extraction, _synthesis

ANCHOR_A = UUID("11111111-1111-4111-8111-111111111111")
ANCHOR_B = UUID("22222222-2222-4222-8222-222222222222")
ANCHOR_UNREGISTERED = UUID("33333333-3333-4333-8333-333333333333")


def _purpose(question: str) -> EditorialAnalyticPurposeV1:
    extraction = _extraction()
    ref = extraction_evidence_refs_v1(extraction)[0]
    return EditorialAnalyticPurposeV1(
        question=question,
        available_data="The synthetic extraction records the documented value.",
        comprehension_gain="The table pairs the literal with its documented role.",
        scope="Only the synthetic evidence above.",
        evidence_refs=(ref,),
        knowledge_limits="No further values are documented.",
        placement_reason="Place after the paragraph that introduces the value.",
    )


def _anchor(anchor_id: UUID = ANCHOR_A) -> EnrichmentPlacementV1:
    return EnrichmentPlacementV1(EnrichmentPlacementKind.AFTER_PARAGRAPH, anchor_block_id=anchor_id)


def _table(key: str, placement: EnrichmentPlacementV1, question: str) -> TableSpecV1:
    extraction = _extraction()
    ref = extraction_evidence_refs_v1(extraction)[0]
    return TableSpecV1(
        key=key,
        kind=EnrichmentTableKind.COMMANDS,
        title=f"Table {key}",
        caption=f"Caption {key}",
        columns=(TableColumnV1("command", "Command"), TableColumnV1("purpose", "Purpose")),
        rows=(TableRowV1(("-enc", "Execution"), (ref,)),),
        placement=placement,
        purpose=_purpose(question),
    )


def _timeline_chart(caption: str = "Chart caption") -> ChartSpecV1:
    extraction = _extraction()
    ref = extraction_evidence_refs_v1(extraction)[0]
    return ChartSpecV1(
        key="chart_one",
        kind=ChartKind.TIMELINE,
        title="Timeline chart",
        caption=caption,
        placement=EnrichmentPlacementV1(EnrichmentPlacementKind.AFTER_LEAD),
        purpose=_purpose("Which synthetic steps are ordered?"),
        points=(
            ChartPointV1(
                label="First observed",
                date="2026-01-01",
                series="Documented events",
                evidence_refs=(ref,),
            ),
        ),
    )


def _diagram(caption: str = "Diagram caption") -> DiagramSpecV1:
    extraction = _extraction()
    ref = extraction_evidence_refs_v1(extraction)[0]
    return DiagramSpecV1(
        key="diagram_one",
        kind=EnrichmentDiagramKind.INFECTION_CHAIN,
        profile=DiagramProfile.FLOW,
        title="Infection chain",
        caption=caption,
        direction=EnrichmentDiagramDirection.LEFT_TO_RIGHT,
        nodes=(
            DiagramNodeV1("loader", "Loader", (ref,)),
            DiagramNodeV1("payload", "Payload", (ref,)),
        ),
        edges=(DiagramEdgeV1("loader", "payload", "loads", (ref,)),),
        groups=(),
        placement=EnrichmentPlacementV1(EnrichmentPlacementKind.END),
        purpose=_purpose("Which documented edge links loader and payload?"),
    )


def _figure(caption: str = "Source figure caption") -> SourceFigureCandidateV1:
    extraction = _extraction()
    source = extraction.sources[0]
    return SourceFigureCandidateV1(
        key="figure_one",
        source_document_id=source.source_document_id,
        source_url=source.canonical_url,
        caption=caption,
        provenance="Figure 1 from the synthetic report",
        locator=SourceFigureLocatorV1(page=1),
        inclusion_status=SourceFigureInclusionStatus.PROPOSED,
        placement=_anchor(ANCHOR_B),
    )


def _base_enrichment(
    *,
    schema_version: int = 10,
    tables: tuple[TableSpecV1, ...] | None = None,
    charts: tuple[ChartSpecV1, ...] | None = None,
    diagrams: tuple[DiagramSpecV1, ...] | None = None,
    figures: tuple[SourceFigureCandidateV1, ...] | None = None,
):
    extraction = _extraction()
    synthesis = _synthesis(extraction)
    base = build_empty_editorial_enrichment(extraction=extraction, synthesis=synthesis)
    return replace(
        base,
        schema_version=schema_version,
        enrichment_policy_version=(
            EDITORIAL_ENRICHMENT_V11_POLICY_VERSION
            if schema_version >= 11
            else base.enrichment_policy_version
        ),
        tables=(
            tables
            if tables is not None
            else (
                _table("table_one", _anchor(), "Which command is documented first?"),
                _table("table_two", _anchor(), "Which command is documented second?"),
            )
        ),
        charts=(charts if charts is not None else (_timeline_chart(),)),
        diagrams=(diagrams if diagrams is not None else (_diagram(),)),
        source_figures=(figures if figures is not None else (_figure(),)),
    )


def _context(
    enrichment=None,
    *,
    registered: frozenset[UUID] | None = frozenset({ANCHOR_A, ANCHOR_B}),
) -> VisualRevisionContext:
    extraction = _extraction()
    synthesis = _synthesis(extraction)
    return VisualRevisionContext(
        base_enrichment=enrichment if enrichment is not None else _base_enrichment(),
        evidence_pack=None,
        extraction=extraction,
        synthesis=synthesis,
        registered_anchor_ids=registered,
    )


def _reject(code: str, plan) -> None:
    with pytest.raises(VisualRevisionRejected) as exc_info:
        plan()
    assert exc_info.value.code == code


def _element(enrichment, kind: EditorialEnrichmentElementKind, key: str):
    family = {
        EditorialEnrichmentElementKind.TABLE: enrichment.tables,
        EditorialEnrichmentElementKind.CHART: enrichment.charts,
        EditorialEnrichmentElementKind.DIAGRAM: enrichment.diagrams,
        EditorialEnrichmentElementKind.FIGURE: enrichment.source_figures,
    }[kind]
    return next(item for item in family if item.key == key)


def test_caption_changes_only_the_caption_and_is_deterministic() -> None:
    ctx = _context()
    base_table = _element(ctx.base_enrichment, EditorialEnrichmentElementKind.TABLE, "table_one")
    patch = UpdateVisualCaptionPatch(
        element_kind=EditorialEnrichmentElementKind.TABLE,
        element_key="table_one",
        caption="Nouvelle légende synthétique",
    )

    first = plan_visual_revision(ctx, patch)
    second = plan_visual_revision(ctx, patch)

    assert first == second
    assert first.revised_enrichment.schema_version == 10
    assert first.changed_elements == (
        EnrichmentElementRef(EditorialEnrichmentElementKind.TABLE, "table_one"),
    )
    revised_table = _element(
        first.revised_enrichment, EditorialEnrichmentElementKind.TABLE, "table_one"
    )
    assert revised_table.caption == "Nouvelle légende synthétique"
    assert replace(revised_table, caption=base_table.caption) == base_table
    assert first.preview["operation"] == "update_visual_caption"
    assert first.preview["element_key"] == "table_one"


def test_caption_on_a_figure_keeps_the_archived_reference_untouched() -> None:
    base = _base_enrichment()
    base_figure = _element(base, EditorialEnrichmentElementKind.FIGURE, "figure_one")
    assert base_figure.resolved_figure is None
    plan = plan_visual_revision(
        _context(base),
        UpdateVisualCaptionPatch(
            element_kind=EditorialEnrichmentElementKind.FIGURE,
            element_key="figure_one",
            caption="Légende de figure corrigée",
        ),
    )
    revised_figure = _element(
        plan.revised_enrichment, EditorialEnrichmentElementKind.FIGURE, "figure_one"
    )
    assert revised_figure.caption == "Légende de figure corrigée"
    assert replace(revised_figure, caption=base_figure.caption) == base_figure


@pytest.mark.parametrize(
    ("patch", "code"),
    (
        (
            UpdateVisualCaptionPatch(
                element_kind=EditorialEnrichmentElementKind.TABLE,
                element_key="missing",
                caption="x",
            ),
            "enrichment_element_not_found",
        ),
        (
            UpdateVisualCaptionPatch(
                element_kind=EditorialEnrichmentElementKind.DIAGRAM,
                element_key="table_one",
                caption="x",
            ),
            "enrichment_element_kind_mismatch",
        ),
        (
            UpdateVisualCaptionPatch(
                element_kind=EditorialEnrichmentElementKind.FIGURE,
                element_key="figure_one",
                caption="   ",
            ),
            "enrichment_caption_invalid",
        ),
        (
            UpdateVisualCaptionPatch(
                element_kind=EditorialEnrichmentElementKind.FIGURE,
                element_key="figure_one",
                caption="x" * 501,
            ),
            "enrichment_caption_invalid",
        ),
        (
            UpdateVisualCaptionPatch(
                element_kind=EditorialEnrichmentElementKind.DIAGRAM,
                element_key="diagram_one",
                caption=None,
            ),
            "enrichment_caption_invalid",
        ),
        (
            UpdateVisualCaptionPatch(
                element_kind=EditorialEnrichmentElementKind.TABLE,
                element_key="table_one",
                caption="Voir https://example.test/source",
            ),
            "editorial_caption_text_violation",
        ),
        (
            UpdateVisualCaptionPatch(
                element_kind=EditorialEnrichmentElementKind.TABLE,
                element_key="table_one",
                caption="Caption table_one",
            ),
            "enrichment_revision_no_change",
        ),
    ),
)
def test_caption_validation_codes(patch, code: str) -> None:
    _reject(code, lambda: plan_visual_revision(_context(), patch))


def test_move_changes_only_the_placement_and_clears_explicit_order() -> None:
    ordered = replace(
        _base_enrichment(schema_version=EDITORIAL_ENRICHMENT_MAX_SCHEMA_VERSION),
        tables=(
            replace(
                _table("table_one", _anchor(), "Which command is documented first?"),
                group_order=0,
            ),
            replace(
                _table("table_two", _anchor(), "Which command is documented second?"),
                group_order=1,
            ),
        ),
    )
    base_table = _element(ordered, EditorialEnrichmentElementKind.TABLE, "table_one")
    plan = plan_visual_revision(
        _context(ordered),
        MoveEnrichmentElementPatch(
            element_kind=EditorialEnrichmentElementKind.TABLE,
            element_key="table_one",
            anchor_block_id=ANCHOR_B,
        ),
    )
    moved = _element(plan.revised_enrichment, EditorialEnrichmentElementKind.TABLE, "table_one")
    assert moved.placement == _anchor(ANCHOR_B)
    assert moved.group_order is None
    assert replace(moved, placement=base_table.placement, group_order=base_table.group_order) == (
        base_table
    )
    other = _element(plan.revised_enrichment, EditorialEnrichmentElementKind.TABLE, "table_two")
    assert other.group_order == 1
    assert plan.revised_enrichment.schema_version == EDITORIAL_ENRICHMENT_MAX_SCHEMA_VERSION
    assert plan.preview["from"]["anchor_block_id"] == str(ANCHOR_A)
    assert plan.preview["to"]["anchor_block_id"] == str(ANCHOR_B)


def test_move_rejections() -> None:
    _reject(
        "editorial_anchor_stale",
        lambda: plan_visual_revision(
            _context(),
            MoveEnrichmentElementPatch(
                element_kind=EditorialEnrichmentElementKind.TABLE,
                element_key="table_one",
                anchor_block_id=ANCHOR_UNREGISTERED,
            ),
        ),
    )
    _reject(
        "editorial_anchor_stale",
        lambda: plan_visual_revision(
            _context(registered=None),
            MoveEnrichmentElementPatch(
                element_kind=EditorialEnrichmentElementKind.TABLE,
                element_key="table_one",
                anchor_block_id=ANCHOR_B,
            ),
        ),
    )
    _reject(
        "enrichment_revision_no_change",
        lambda: plan_visual_revision(
            _context(),
            MoveEnrichmentElementPatch(
                element_kind=EditorialEnrichmentElementKind.TABLE,
                element_key="table_one",
                anchor_block_id=ANCHOR_A,
            ),
        ),
    )


def test_reorder_writes_zero_based_group_order_and_bumps_schema_v11() -> None:
    ctx = _context()
    order = (
        EnrichmentElementRef(EditorialEnrichmentElementKind.TABLE, "table_two"),
        EnrichmentElementRef(EditorialEnrichmentElementKind.TABLE, "table_one"),
    )
    before_hash = enrichment_order_group_hash(ctx.base_enrichment, _anchor())
    plan = plan_visual_revision(
        ctx, ReorderEnrichmentElementsPatch(anchor_block_id=ANCHOR_A, order=order)
    )
    assert plan.revised_enrichment.schema_version == EDITORIAL_ENRICHMENT_MAX_SCHEMA_VERSION
    assert (
        plan.revised_enrichment.enrichment_policy_version == EDITORIAL_ENRICHMENT_V11_POLICY_VERSION
    )
    assert plan.changed_elements == order
    group = next(
        group
        for group in enrichment_element_order(plan.revised_enrichment)
        if group.placement == _anchor()
    )
    assert group.members == order
    assert (
        _element(
            plan.revised_enrichment, EditorialEnrichmentElementKind.TABLE, "table_two"
        ).group_order
        == 0
    )
    assert (
        _element(
            plan.revised_enrichment, EditorialEnrichmentElementKind.TABLE, "table_one"
        ).group_order
        == 1
    )
    after_hash = enrichment_order_group_hash(plan.revised_enrichment, _anchor())
    assert after_hash != before_hash
    assert enrichment_order_group_hash(ctx.base_enrichment, _anchor()) == before_hash
    assert plan == plan_visual_revision(
        ctx, ReorderEnrichmentElementsPatch(anchor_block_id=ANCHOR_A, order=order)
    )


def test_reorder_permutes_an_explicitly_ordered_group_atomically() -> None:
    ordered = replace(
        _base_enrichment(schema_version=EDITORIAL_ENRICHMENT_MAX_SCHEMA_VERSION),
        tables=(
            replace(
                _table("table_one", _anchor(), "Which command is documented first?"),
                group_order=0,
            ),
            replace(
                _table("table_two", _anchor(), "Which command is documented second?"),
                group_order=1,
            ),
        ),
    )
    order = (
        EnrichmentElementRef(EditorialEnrichmentElementKind.TABLE, "table_two"),
        EnrichmentElementRef(EditorialEnrichmentElementKind.TABLE, "table_one"),
    )
    plan = plan_visual_revision(
        _context(ordered),
        ReorderEnrichmentElementsPatch(anchor_block_id=ANCHOR_A, order=order),
    )
    assert (
        _element(
            plan.revised_enrichment, EditorialEnrichmentElementKind.TABLE, "table_two"
        ).group_order
        == 0
    )
    assert (
        _element(
            plan.revised_enrichment, EditorialEnrichmentElementKind.TABLE, "table_one"
        ).group_order
        == 1
    )
    group = next(
        group
        for group in enrichment_element_order(plan.revised_enrichment)
        if group.placement == _anchor()
    )
    assert group.members == order


def test_reorder_rejections() -> None:
    _reject(
        "editorial_anchor_stale",
        lambda: plan_visual_revision(
            _context(),
            ReorderEnrichmentElementsPatch(
                anchor_block_id=ANCHOR_UNREGISTERED,
                order=(
                    EnrichmentElementRef(EditorialEnrichmentElementKind.TABLE, "table_two"),
                    EnrichmentElementRef(EditorialEnrichmentElementKind.TABLE, "table_one"),
                ),
            ),
        ),
    )
    _reject(
        "enrichment_order_invalid",
        lambda: plan_visual_revision(
            _context(),
            ReorderEnrichmentElementsPatch(
                anchor_block_id=ANCHOR_A,
                order=(
                    EnrichmentElementRef(EditorialEnrichmentElementKind.TABLE, "table_one"),
                    EnrichmentElementRef(EditorialEnrichmentElementKind.TABLE, "table_one"),
                ),
            ),
        ),
    )
    _reject(
        "enrichment_order_invalid",
        lambda: plan_visual_revision(
            _context(),
            ReorderEnrichmentElementsPatch(
                anchor_block_id=ANCHOR_A,
                order=(
                    EnrichmentElementRef(EditorialEnrichmentElementKind.TABLE, "table_two"),
                    EnrichmentElementRef(EditorialEnrichmentElementKind.TABLE, "table_one"),
                    EnrichmentElementRef(EditorialEnrichmentElementKind.TABLE, "table_extra"),
                ),
            ),
        ),
    )
    _reject(
        "enrichment_revision_no_change",
        lambda: plan_visual_revision(
            _context(),
            ReorderEnrichmentElementsPatch(
                anchor_block_id=ANCHOR_A,
                order=(
                    EnrichmentElementRef(EditorialEnrichmentElementKind.TABLE, "table_one"),
                    EnrichmentElementRef(EditorialEnrichmentElementKind.TABLE, "table_two"),
                ),
            ),
        ),
    )
    single = replace(
        _base_enrichment(),
        tables=(_table("table_one", _anchor(), "Which single command is documented?"),),
    )
    _reject(
        "enrichment_order_group_unsupported",
        lambda: plan_visual_revision(
            _context(single),
            ReorderEnrichmentElementsPatch(
                anchor_block_id=ANCHOR_A,
                order=(EnrichmentElementRef(EditorialEnrichmentElementKind.TABLE, "table_one"),),
            ),
        ),
    )


def test_unimplemented_slice_variants_reject_explicitly() -> None:
    ctx = _context()
    chart = _timeline_chart()
    diagram = _diagram()
    variants = (
        ReviseChartPatch(element_key="chart_one", chart=replace(chart, key="chart_one")),
        ReviseDiagramPatch(element_key="diagram_one", diagram=replace(diagram, key="diagram_one")),
        ReplaceSourceFigurePatch(
            element_key="figure_one", figure_id=UUID("44444444-4444-4444-8444-444444444444")
        ),
        RemoveEnrichmentElementPatch(
            element_kind=EditorialEnrichmentElementKind.TABLE, element_key="table_one"
        ),
    )
    for variant in variants:
        with pytest.raises(VisualRevisionRejected) as exc_info:
            plan_visual_revision(ctx, variant)
        assert exc_info.value.code == "enrichment_revision_unsupported_operation"


def test_patch_codec_round_trips_implemented_variants_and_rejects_the_rest() -> None:
    caption = UpdateVisualCaptionPatch(
        element_kind=EditorialEnrichmentElementKind.CHART,
        element_key="chart_one",
        caption="Nouvelle légende",
    )
    move = MoveEnrichmentElementPatch(
        element_kind=EditorialEnrichmentElementKind.TABLE,
        element_key="table_one",
        anchor_block_id=ANCHOR_B,
    )
    reorder = ReorderEnrichmentElementsPatch(
        anchor_block_id=ANCHOR_A,
        order=(
            EnrichmentElementRef(EditorialEnrichmentElementKind.TABLE, "table_two"),
            EnrichmentElementRef(EditorialEnrichmentElementKind.TABLE, "table_one"),
        ),
    )
    for patch in (caption, move, reorder):
        assert visual_patch_from_json(visual_patch_to_json(patch)) == patch
    with pytest.raises(NotImplementedForSlice):
        visual_patch_to_json(ReviseChartPatch(element_key="chart_one", chart=_timeline_chart()))
    with pytest.raises(NotImplementedForSlice):
        visual_patch_from_json({"kind": "revise_chart"})
    with pytest.raises(EditorialEnrichmentPatchError):
        visual_patch_from_json({"kind": "update_visual_caption"})


def test_project_blocks_exposes_element_fields_order_and_reorder_operations() -> None:
    from cti_app.application.editorial_workspace.registry import (
        WorkspaceSnapshotRecord,
        project_blocks,
        registry_hash,
        resolve_enrichment_element_target,
        resolve_enrichment_order_target,
    )
    from cti_app.application.production_artifact_store import ProductionArtifactStore
    from cti_app.domain.editorial_workspace import bootstrap_block_registry
    from cti_app.domain.production_synthesis import production_synthesis_to_json

    extraction = _extraction()
    synthesis = _synthesis(extraction)
    synthesis_payload = production_synthesis_to_json(synthesis)
    synthesis_sha256 = hashlib.sha256(
        ProductionArtifactStore.canonical_json_bytes(synthesis_payload)
    ).hexdigest()
    synthesis_artifact_id = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
    synthesis_entries = bootstrap_block_registry(
        artifact_id=synthesis_artifact_id,
        canonical_sha256=synthesis_sha256,
        source_stage="synthesis",
        canonical_content=synthesis_payload,
    )
    anchor_entry = next(entry for entry in synthesis_entries if entry.kind == "paragraph")
    enrichment = replace(
        _base_enrichment(),
        tables=(
            _table(
                "table_one",
                _anchor(anchor_entry.block_id),
                "Which first command is documented?",
            ),
            _table(
                "table_two",
                _anchor(anchor_entry.block_id),
                "Which second command is documented?",
            ),
        ),
        charts=(),
        diagrams=(),
        source_figures=(),
    )
    enrichment_payload = editorial_enrichment_to_json(enrichment)
    enrichment_artifact_id = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
    enrichment_entries = bootstrap_block_registry(
        artifact_id=enrichment_artifact_id,
        canonical_sha256="c" * 64,
        source_stage="editorial_enrichment",
        canonical_content=enrichment_payload,
    )
    entries = (*synthesis_entries, *enrichment_entries)
    record = WorkspaceSnapshotRecord(
        snapshot_id=UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc"),
        subject_id=extraction.subject_id,
        production_run_id=UUID("dddddddd-dddd-4ddd-8ddd-dddddddddddd"),
        synthesis_artifact_id=synthesis_artifact_id,
        synthesis_version=1,
        synthesis_input_hash=extraction.production_input_hash,
        synthesis_canonical_sha256=synthesis_sha256,
        enrichment_artifact_id=enrichment_artifact_id,
        enrichment_version=1,
        enrichment_input_hash="e" * 64,
        enrichment_canonical_sha256="c" * 64,
        publication_artifact_id=None,
        publication_version=None,
        publication_input_hash=None,
        publication_canonical_sha256=None,
        pipeline_generation=0,
        routing_decision_id=None,
        routing_decision_hash=None,
        publication_kind=None,
        workspace_pin_hash="f" * 64,
        registry_hash=registry_hash(entries),
        block_registry=entries,
    )

    _blocks, enrichments, _outline = project_blocks(
        entries=entries,
        synthesis=synthesis_payload,
        enrichment=enrichment_payload,
        record=record,
    )

    assert [block["element_key"] for block in enrichments] == ["table_one", "table_two"]
    assert [block["order"] for block in enrichments] == [0, 1]
    assert all(block["element_kind"] == "table" for block in enrichments)
    assert [block["caption"] for block in enrichments] == ["Caption table_one", "Caption table_two"]
    for block in enrichments:
        allowed = block["allowed_operations"]
        assert isinstance(allowed, list)
        assert {
            "update_visual_caption",
            "move_enrichment_element",
            "reorder_enrichment_elements",
        } <= set(allowed)
    table_entry = next(
        entry for entry in enrichment_entries if entry.structural_pointer == "/tables/0"
    )
    resolved = resolve_enrichment_element_target(
        entry=table_entry, enrichment=enrichment_payload, operation="update_visual_caption"
    )
    assert resolved.target.element_key == "table_one"
    assert resolved.before_hash == table_entry.content_sha256
    order_target = resolve_enrichment_order_target(
        entry=anchor_entry,
        enrichment=enrichment_payload,
        operation="reorder_enrichment_elements",
    )
    assert order_target.before_hash == enrichment_order_group_hash(
        enrichment, _anchor(anchor_entry.block_id)
    )


def test_group_hash_is_shared_with_the_domain_helper() -> None:
    enrichment = _base_enrichment()
    assert enrichment_order_group_hash(enrichment, _anchor()) == domain_group_hash(
        enrichment, _anchor()
    )
    payload = editorial_enrichment_to_json(enrichment)
    assert payload["schema_version"] == 10
    assert "group_order" not in payload["tables"][0]
