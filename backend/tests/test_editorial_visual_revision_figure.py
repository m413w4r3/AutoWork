"""Unit oracles for typed figure replacement and element removal (F2-03c).

The shared planner is pure: these tests build a fresh catalogue by hand and
prove that replacement resolves only an eligible, run-scoped archived figure
and that removal keeps every other element's list position and explicit order.
"""

from __future__ import annotations

import hashlib
from dataclasses import replace
from typing import Any, cast
from uuid import UUID

import pytest

from cti_app.application.editorial_figure_catalog import opaque_figure_id
from cti_app.application.editorial_visual_revision import (
    VisualRevisionContext,
    VisualRevisionRejected,
    plan_visual_revision,
)
from cti_app.application.production_editorial_enrichment import (
    EditorialFigureCatalogEntry,
)
from cti_app.application.source_figure_inventory import SourceFigureCatalogMetadata
from cti_app.domain.editorial_enrichment_patch import (
    EditorialEnrichmentPatchError,
    RemoveEnrichmentElementPatch,
    ReplaceSourceFigurePatch,
    visual_patch_from_json,
    visual_patch_to_json,
)
from cti_app.domain.production_editorial_enrichment import (
    EDITORIAL_ENRICHMENT_MAX_SCHEMA_VERSION,
    EDITORIAL_ENRICHMENT_V11_POLICY_VERSION,
    EditorialEnrichmentElementKind,
    EditorialFigureDecision,
    EditorialFigureDecisionActor,
    EditorialFigureDecisionTraceV1,
    EnrichmentElementRef,
    ResolvedSourceFigureV1,
    SourceFigureCandidateV1,
    SourceFigureDecision,
    SourceFigureInclusionStatus,
    SourceFigureLocatorV1,
    editorial_enrichment_to_json,
    source_figure_id,
)
from cti_app.domain.production_synthesis import extraction_evidence_refs_v1
from cti_app.domain.publication_kind import PublicationKind
from tests.editorial_enrichment_support import build_empty_editorial_enrichment
from tests.test_editorial_visual_revision import _anchor, _base_enrichment, _table
from tests.test_production_editorial_enrichment import _extraction, _synthesis

RUN_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
FIGURE_ONE = EnrichmentElementRef(EditorialEnrichmentElementKind.FIGURE, "figure_one")


def _resolved_figure(
    *,
    ordinal: int,
    decision: SourceFigureDecision = SourceFigureDecision.ACCEPTED,
) -> ResolvedSourceFigureV1:
    extraction = _extraction()
    source = extraction.sources[0]
    sha256 = hashlib.sha256(f"figure-{ordinal}".encode()).hexdigest()
    locator = SourceFigureLocatorV1(page=ordinal + 1)
    return ResolvedSourceFigureV1(
        figure_id=source_figure_id(
            source_document_id=source.source_document_id,
            sha256=sha256,
            source=source.canonical_url,
            locator=locator,
        ),
        blob_id=UUID(int=9100 + ordinal),
        sha256=sha256,
        mime_type="image/png",
        byte_size=128,
        source_document_id=source.source_document_id,
        source=source.canonical_url,
        provenance=f"Synthetic archived figure {ordinal}.",
        locator=locator,
        decision=decision,
        decision_reason="matched_archived_blob",
    )


def _catalog_entry(
    figure: ResolvedSourceFigureV1,
    *,
    handle: str,
    width: int | None = 8,
    height: int | None = 6,
) -> EditorialFigureCatalogEntry:
    return EditorialFigureCatalogEntry(
        handle=handle,
        source_role="primary",
        editorial_role="analysis",
        figure=figure,
        metadata=SourceFigureCatalogMetadata(width=width, height=height),
    )


def _figure_candidate(
    key: str,
    figure: ResolvedSourceFigureV1,
    *,
    caption: str,
    inclusion_status: SourceFigureInclusionStatus = SourceFigureInclusionStatus.INCLUDED,
    placement=None,
    order: int | None = None,
    group_order: int | None = None,
) -> SourceFigureCandidateV1:
    return SourceFigureCandidateV1(
        key=key,
        source_document_id=figure.source_document_id,
        source_url=figure.source,
        caption=caption,
        provenance=figure.provenance,
        locator=figure.locator,
        inclusion_status=inclusion_status,
        placement=placement or _anchor(),
        resolved_figure=figure,
        width=8,
        height=6,
        order=order,
        group_order=group_order,
    )


def _decision_trace(figure: ResolvedSourceFigureV1) -> EditorialFigureDecisionTraceV1:
    extraction = _extraction()
    ref = next(
        ref
        for ref in extraction_evidence_refs_v1(extraction)
        if ref.source_document_id == figure.source_document_id
    )
    return EditorialFigureDecisionTraceV1(
        handle="F001",
        figure_id=figure.figure_id,
        source_document_id=figure.source_document_id,
        decision=EditorialFigureDecision.INCLUDED_BY_MODEL,
        actor=EditorialFigureDecisionActor.MODEL_PROPOSAL,
        reason_code="synthetic_model_selection",
        reason="The synthetic model selected this archived figure.",
        policy_version="synthetic-figure-policy-v1",
        prompt_version="synthetic-figure-prompt-v1",
        contract_version="synthetic-figure-contract-v1",
        parser_version="synthetic-figure-parser-v1",
        evidence_refs=(ref,),
    )


def _context(
    *,
    figures: tuple[SourceFigureCandidateV1, ...],
    catalog: tuple[EditorialFigureCatalogEntry, ...],
    figure_decisions: tuple[EditorialFigureDecisionTraceV1, ...] = (),
    schema_version: int = 10,
    publication_kind: PublicationKind | None = PublicationKind.ARTICLE,
    production_run_id: UUID | None = RUN_ID,
) -> VisualRevisionContext:
    extraction = _extraction()
    synthesis = _synthesis(extraction)
    base = build_empty_editorial_enrichment(extraction=extraction, synthesis=synthesis)
    base = replace(
        base,
        schema_version=schema_version,
        enrichment_policy_version=(
            EDITORIAL_ENRICHMENT_V11_POLICY_VERSION
            if schema_version >= EDITORIAL_ENRICHMENT_MAX_SCHEMA_VERSION
            else base.enrichment_policy_version
        ),
        source_figures=figures,
        figure_decisions=figure_decisions,
    )
    return VisualRevisionContext(
        base_enrichment=base,
        evidence_pack=None,
        extraction=extraction,
        synthesis=synthesis,
        figure_catalog=catalog,
        registered_anchor_ids=frozenset({_anchor().anchor_block_id}),
        production_run_id=production_run_id,
        publication_kind=publication_kind,
    )


def _opaque(figure: ResolvedSourceFigureV1) -> UUID:
    return UUID(opaque_figure_id(_extraction().subject_id, RUN_ID, figure.figure_id))


def _figure_plan_context(
    *,
    current: ResolvedSourceFigureV1 | None = None,
    replacement: ResolvedSourceFigureV1 | None = None,
    extra_entries: tuple[EditorialFigureCatalogEntry, ...] = (),
    extra_figures: tuple[SourceFigureCandidateV1, ...] = (),
    figure_decisions: tuple[EditorialFigureDecisionTraceV1, ...] = (),
    schema_version: int = 10,
    publication_kind: PublicationKind | None = PublicationKind.ARTICLE,
    production_run_id: UUID | None = RUN_ID,
    replacement_width: int | None = 8,
    replacement_height: int | None = 6,
) -> tuple[VisualRevisionContext, ResolvedSourceFigureV1, ResolvedSourceFigureV1]:
    current = current or _resolved_figure(ordinal=0)
    replacement = replacement or _resolved_figure(ordinal=1)
    current_entry = _catalog_entry(current, handle="F001")
    replacement_entry = _catalog_entry(
        replacement,
        handle="F002",
        width=replacement_width,
        height=replacement_height,
    )
    figures = (
        _figure_candidate("figure_one", current, caption="Figure initiale.", order=0),
        *extra_figures,
    )
    return (
        _context(
            figures=figures,
            catalog=(current_entry, replacement_entry, *extra_entries),
            figure_decisions=figure_decisions,
            schema_version=schema_version,
            publication_kind=publication_kind,
            production_run_id=production_run_id,
        ),
        current,
        replacement,
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


def test_replace_source_figure_replaces_in_place_from_the_fresh_catalogue() -> None:
    current = _resolved_figure(ordinal=0)
    trace = _decision_trace(current)
    ctx, current, replacement = _figure_plan_context(figure_decisions=(trace,))
    patch = ReplaceSourceFigurePatch(element_key="figure_one", figure_id=_opaque(replacement))

    first = plan_visual_revision(ctx, patch)
    second = plan_visual_revision(ctx, patch)

    assert first == second
    assert first.changed_elements == (FIGURE_ONE,)
    revised = first.revised_enrichment
    assert len(revised.source_figures) == 1
    replaced = revised.source_figures[0]
    assert replaced.key == "figure_one"
    assert replaced.resolved_figure == replacement
    assert replaced.caption == "Figure initiale."
    assert replaced.placement == ctx.base_enrichment.source_figures[0].placement
    assert replaced.order == 0
    assert replaced.group_order is None
    assert replaced.inclusion_status is SourceFigureInclusionStatus.INCLUDED
    assert replaced.purpose is None
    # The manual replacement carries no model decision; keeping the old
    # INCLUDED_BY_MODEL trace would misattribute the choice and fail validation.
    assert revised.figure_decisions == ()
    assert ctx.base_enrichment.source_figures[0].resolved_figure == current
    assert first.preview["operation"] == "replace_source_figure"
    assert first.preview["element_key"] == "figure_one"
    assert first.preview["figure_id"] == str(_opaque(replacement))


def test_replace_preserves_placement_and_explicit_group_order() -> None:
    current = _resolved_figure(ordinal=0)
    replacement = _resolved_figure(ordinal=1)
    placed = _figure_candidate(
        "figure_one",
        current,
        caption="Figure placée.",
        placement=_anchor(),
        order=2,
        group_order=1,
    )
    ctx = _context(
        figures=(placed,),
        catalog=(
            _catalog_entry(current, handle="F001"),
            _catalog_entry(replacement, handle="F002"),
        ),
        schema_version=EDITORIAL_ENRICHMENT_MAX_SCHEMA_VERSION,
    )
    revised = plan_visual_revision(
        ctx,
        ReplaceSourceFigurePatch(element_key="figure_one", figure_id=_opaque(replacement)),
    ).revised_enrichment
    replaced = revised.source_figures[0]
    assert replaced.placement == placed.placement
    assert replaced.order == 2
    assert replaced.group_order == 1
    assert revised.schema_version == EDITORIAL_ENRICHMENT_MAX_SCHEMA_VERSION


def test_replace_rejects_an_ineligible_catalogue_figure() -> None:
    rejected = _resolved_figure(ordinal=1, decision=SourceFigureDecision.REJECTED)
    ctx, _current, replacement = _figure_plan_context(replacement=rejected)
    _reject(
        "editorial_figure_not_eligible",
        lambda: plan_visual_revision(
            ctx,
            ReplaceSourceFigurePatch(element_key="figure_one", figure_id=_opaque(replacement)),
        ),
    )


def test_replace_rejects_missing_archive_dimensions() -> None:
    ctx, _current, replacement = _figure_plan_context(
        replacement_width=None, replacement_height=None
    )
    _reject(
        "editorial_figure_not_eligible",
        lambda: plan_visual_revision(
            ctx,
            ReplaceSourceFigurePatch(element_key="figure_one", figure_id=_opaque(replacement)),
        ),
    )


def test_replace_rejects_a_figure_already_used_by_another_element() -> None:
    current = _resolved_figure(ordinal=0)
    used = _resolved_figure(ordinal=1)
    ctx = _context(
        figures=(
            _figure_candidate("figure_one", current, caption="Figure initiale."),
            _figure_candidate("figure_two", used, caption="Figure déjà utilisée."),
        ),
        catalog=(
            _catalog_entry(current, handle="F001"),
            _catalog_entry(used, handle="F002"),
        ),
    )
    _reject(
        "editorial_figure_not_eligible",
        lambda: plan_visual_revision(
            ctx,
            ReplaceSourceFigurePatch(element_key="figure_one", figure_id=_opaque(used)),
        ),
    )


def test_replace_with_the_same_figure_is_a_no_change() -> None:
    ctx, current, _replacement = _figure_plan_context()
    _reject(
        "editorial_figure_not_changed",
        lambda: plan_visual_revision(
            ctx,
            ReplaceSourceFigurePatch(element_key="figure_one", figure_id=_opaque(current)),
        ),
    )


def test_replace_rejects_an_unknown_opaque_figure_id() -> None:
    ctx, _current, _replacement = _figure_plan_context()
    _reject(
        "editorial_figure_not_found",
        lambda: plan_visual_revision(
            ctx,
            ReplaceSourceFigurePatch(
                element_key="figure_one",
                figure_id=UUID("44444444-4444-4444-8444-444444444444"),
            ),
        ),
    )


@pytest.mark.parametrize(
    ("publication_kind", "count", "expected"),
    (
        (PublicationKind.BRIEF, 3, None),
        (PublicationKind.BRIEF, 4, "brief_figure_limit_exceeded"),
    ),
)
def test_replace_keeps_the_brief_figure_cap(
    publication_kind: PublicationKind, count: int, expected: str | None
) -> None:
    resolved = tuple(_resolved_figure(ordinal=index) for index in range(count))
    figures = tuple(
        _figure_candidate(f"figure_{index}", figure, caption=f"Figure {index}.")
        for index, figure in enumerate(resolved)
    )
    replacement = _resolved_figure(ordinal=99)
    ctx = _context(
        figures=figures,
        catalog=(
            *(
                _catalog_entry(figure, handle=f"F{index:03d}")
                for index, figure in enumerate(resolved, start=1)
            ),
            _catalog_entry(replacement, handle="F100"),
        ),
        publication_kind=publication_kind,
    )
    patch = ReplaceSourceFigurePatch(element_key="figure_0", figure_id=_opaque(replacement))
    if expected is None:
        plan = plan_visual_revision(ctx, patch)
        assert len(plan.revised_enrichment.source_figures) == count
    else:
        _reject(expected, lambda: plan_visual_revision(ctx, patch))


def test_replace_caption_policy_matches_caption_updates() -> None:
    ctx, _current, replacement = _figure_plan_context()
    figure_id = _opaque(replacement)

    supplied = plan_visual_revision(
        ctx,
        ReplaceSourceFigurePatch(
            element_key="figure_one",
            figure_id=figure_id,
            caption="Nouvelle légende de figure.",
        ),
    )
    assert supplied.revised_enrichment.source_figures[0].caption == "Nouvelle légende de figure."

    kept = plan_visual_revision(
        ctx, ReplaceSourceFigurePatch(element_key="figure_one", figure_id=figure_id)
    )
    assert kept.revised_enrichment.source_figures[0].caption == "Figure initiale."

    _reject(
        "editorial_caption_text_violation",
        lambda: plan_visual_revision(
            ctx,
            ReplaceSourceFigurePatch(
                element_key="figure_one",
                figure_id=figure_id,
                caption="Voir https://example.test/figure.png",
            ),
        ),
    )
    _reject(
        "enrichment_caption_invalid",
        lambda: plan_visual_revision(
            ctx,
            ReplaceSourceFigurePatch(
                element_key="figure_one", figure_id=figure_id, caption="x" * 501
            ),
        ),
    )


def test_replace_requires_a_run_scoped_catalogue() -> None:
    ctx, _current, replacement = _figure_plan_context(production_run_id=None)
    _reject(
        "figure_catalog_unavailable",
        lambda: plan_visual_revision(
            ctx,
            ReplaceSourceFigurePatch(element_key="figure_one", figure_id=_opaque(replacement)),
        ),
    )


def test_replace_patch_never_accepts_a_url_instead_of_an_opaque_id() -> None:
    with pytest.raises(ValueError):
        ReplaceSourceFigurePatch(
            element_key="figure_one",
            figure_id=cast(Any, "https://example.test/f.png"),
        )
    with pytest.raises(EditorialEnrichmentPatchError):
        visual_patch_from_json(
            {
                "kind": "replace_source_figure",
                "element_key": "figure_one",
                "figure_id": "https://example.test/f.png",
                "caption": None,
            }
        )


def test_remove_rejects_a_missing_or_mismatched_element() -> None:
    ctx = _context(
        figures=(_figure_candidate("figure_one", _resolved_figure(ordinal=0), caption="x"),),
        catalog=(),
    )
    _reject(
        "enrichment_element_not_found",
        lambda: plan_visual_revision(
            ctx,
            RemoveEnrichmentElementPatch(
                element_kind=EditorialEnrichmentElementKind.TABLE, element_key="missing"
            ),
        ),
    )
    _reject(
        "enrichment_element_kind_mismatch",
        lambda: plan_visual_revision(
            ctx,
            RemoveEnrichmentElementPatch(
                element_kind=EditorialEnrichmentElementKind.TABLE,
                element_key="figure_one",
            ),
        ),
    )


@pytest.mark.parametrize(
    ("kind", "key"),
    (
        (EditorialEnrichmentElementKind.TABLE, "table_one"),
        (EditorialEnrichmentElementKind.CHART, "chart_one"),
        (EditorialEnrichmentElementKind.DIAGRAM, "diagram_one"),
        (EditorialEnrichmentElementKind.FIGURE, "figure_one"),
    ),
)
def test_remove_enrichment_element_for_every_kind(
    kind: EditorialEnrichmentElementKind, key: str
) -> None:
    base = _base_enrichment()
    extraction = _extraction()
    synthesis = _synthesis(extraction)
    ctx = VisualRevisionContext(
        base_enrichment=base,
        evidence_pack=None,
        extraction=extraction,
        synthesis=synthesis,
        registered_anchor_ids=frozenset({_anchor().anchor_block_id}),
        production_run_id=RUN_ID,
        publication_kind=PublicationKind.ARTICLE,
    )
    ref = EnrichmentElementRef(kind, key)

    plan = plan_visual_revision(ctx, RemoveEnrichmentElementPatch(kind, key))

    assert plan.changed_elements == (ref,)
    revised = plan.revised_enrichment
    assert revised.schema_version == base.schema_version
    assert all(
        item.key != key
        for item in {
            EditorialEnrichmentElementKind.TABLE: revised.tables,
            EditorialEnrichmentElementKind.CHART: revised.charts,
            EditorialEnrichmentElementKind.DIAGRAM: revised.diagrams,
            EditorialEnrichmentElementKind.FIGURE: revised.source_figures,
        }[kind]
    )
    for family_kind, field_name in (
        (EditorialEnrichmentElementKind.TABLE, "tables"),
        (EditorialEnrichmentElementKind.CHART, "charts"),
        (EditorialEnrichmentElementKind.DIAGRAM, "diagrams"),
        (EditorialEnrichmentElementKind.FIGURE, "source_figures"),
    ):
        base_family = getattr(base, field_name)
        revised_family = getattr(revised, field_name)
        if family_kind is kind:
            assert revised_family == tuple(item for item in base_family if item.key != key)
        else:
            assert revised_family == base_family
    assert plan.preview["operation"] == RemoveEnrichmentElementPatch(kind, key).operation.value
    assert plan.preview["element_kind"] == kind.value


def test_remove_keeps_other_elements_positions_and_group_order() -> None:
    base = replace(
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
            replace(
                _table("table_three", _anchor(), "Which command is documented third?"),
                group_order=2,
            ),
        ),
    )
    extraction = _extraction()
    synthesis = _synthesis(extraction)
    ctx = VisualRevisionContext(
        base_enrichment=base,
        evidence_pack=None,
        extraction=extraction,
        synthesis=synthesis,
        registered_anchor_ids=frozenset({_anchor().anchor_block_id}),
        production_run_id=RUN_ID,
        publication_kind=PublicationKind.ARTICLE,
    )
    revised = plan_visual_revision(
        ctx,
        RemoveEnrichmentElementPatch(EditorialEnrichmentElementKind.TABLE, "table_two"),
    ).revised_enrichment
    assert [table.key for table in revised.tables] == ["table_one", "table_three"]
    assert [table.group_order for table in revised.tables] == [0, 2]


def test_remove_figure_drops_only_its_stale_decision_trace() -> None:
    figure = _resolved_figure(ordinal=0)
    other = _resolved_figure(ordinal=1)
    extraction = _extraction()
    synthesis = _synthesis(extraction)
    trace = _decision_trace(figure)
    other_ref = next(
        ref
        for ref in extraction_evidence_refs_v1(extraction)
        if ref.source_document_id == other.source_document_id
    )
    other_trace = replace(
        trace,
        handle="F002",
        figure_id=other.figure_id,
        decision=EditorialFigureDecision.NOT_SELECTED_BY_MODEL,
        evidence_refs=(other_ref,),
    )
    base = replace(
        build_empty_editorial_enrichment(extraction=extraction, synthesis=synthesis),
        source_figures=(_figure_candidate("figure_one", figure, caption="Figure supprimée."),),
        figure_decisions=(trace, other_trace),
    )
    ctx = VisualRevisionContext(
        base_enrichment=base,
        evidence_pack=None,
        extraction=extraction,
        synthesis=synthesis,
        registered_anchor_ids=frozenset({_anchor().anchor_block_id}),
        production_run_id=RUN_ID,
        publication_kind=PublicationKind.ARTICLE,
    )
    revised = plan_visual_revision(
        ctx,
        RemoveEnrichmentElementPatch(EditorialEnrichmentElementKind.FIGURE, "figure_one"),
    ).revised_enrichment
    assert revised.source_figures == ()
    # Current behaviour (documented): the removed figure's decision trace is
    # dropped; older artifact versions retain the history.
    assert revised.figure_decisions == (other_trace,)


def test_replace_and_remove_patch_codecs_round_trip() -> None:
    replace_patch = ReplaceSourceFigurePatch(
        element_key="figure_one",
        figure_id=UUID("44444444-4444-4444-8444-444444444444"),
        caption="Nouvelle légende.",
    )
    remove_patch = RemoveEnrichmentElementPatch(
        element_kind=EditorialEnrichmentElementKind.DIAGRAM, element_key="diagram_one"
    )
    for patch in (replace_patch, remove_patch):
        payload = visual_patch_to_json(patch)
        assert visual_patch_from_json(payload) == patch
    assert replace_patch.operation.value == "replace_source_figure"
    assert remove_patch.operation.value == "delete_diagram"
    assert replace_patch.element_kind is EditorialEnrichmentElementKind.FIGURE


def test_replace_rejects_a_catalogue_entry_from_another_run() -> None:
    # The opaque id is run-scoped; an id minted for another run resolves to no
    # entry in this run's catalogue even when the canonical figure is identical.
    ctx, _current, replacement = _figure_plan_context()
    other_run_id = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
    foreign_id = UUID(
        opaque_figure_id(_extraction().subject_id, other_run_id, replacement.figure_id)
    )
    _reject(
        "editorial_figure_not_found",
        lambda: plan_visual_revision(
            ctx, ReplaceSourceFigurePatch(element_key="figure_one", figure_id=foreign_id)
        ),
    )


def test_replacement_payload_is_the_planned_enrichment_round_trip() -> None:
    ctx, _current, replacement = _figure_plan_context()
    plan = plan_visual_revision(
        ctx,
        ReplaceSourceFigurePatch(element_key="figure_one", figure_id=_opaque(replacement)),
    )
    payload = editorial_enrichment_to_json(plan.revised_enrichment)
    assert payload["source_figures"][0]["resolved_figure"]["figure_id"] == str(
        replacement.figure_id
    )
    assert payload["source_figures"][0]["key"] == "figure_one"
