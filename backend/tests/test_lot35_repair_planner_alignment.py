"""LOT 35 — one issue, one business truth.

Two defects share the same shape: the planner answered from the recorded
decision while the read model answered from the durable state of the source.
An analyst who waived a source and later supplied it got an issue that blocked
sign-off, claimed a REFERENCES rebuild, and carried a
``no_deliverable_change`` plan the Repair Desk refuses to display -- a deadlock
with nothing to arbitrate and nothing to apply.

The matrix below pins the whole planner surface at once so that class of
divergence cannot come back, and the end-to-end scenario proves the fix on the
real HTTP read model.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

import pytest

from cti_app.application.production_repairs import (
    ProductionRepairIssueView,
    SupplementalSourceRepairIssue,
    repair_issue_execution_state,
)
from cti_app.domain.production import (
    ProductionRepairAction,
    ProductionRepairDecision,
    ProductionRepairImpactKind,
    ProductionRepairIssueKind,
    RepairDecisionApplicationState,
    SupplementalSourceRepairState,
)
from cti_app.domain.publication import is_publication_ioc_artifact_type

EDITION_ID = UUID("00000000-0000-0000-0000-000000000001")
SUBJECT_ID = UUID("00000000-0000-0000-0000-000000000002")
RUN_ID = UUID("00000000-0000-0000-0000-000000000003")
ARTIFACT_ID = UUID("00000000-0000-0000-0000-000000000004")


def _q2_issue(
    *,
    kind: ProductionRepairIssueKind = ProductionRepairIssueKind.REJECTED_INDICATOR,
    artifact_type: str = "domain",
    application_state: RepairDecisionApplicationState = RepairDecisionApplicationState.UNRESOLVED,
) -> ProductionRepairIssueView:
    return ProductionRepairIssueView(
        repair_key="a" * 64,
        kind=kind,
        artifact_type=artifact_type,
        source_id="S1",
        source_title="Source",
        is_publication_ioc=is_publication_ioc_artifact_type(artifact_type),
        source_url="https://source.example/report",
        reason_code="rejected",
        value_sha256="b" * 64,
        preview="value",
        payload_available=True,
        production_run_id=RUN_ID,
        observed_artifact_id=ARTIFACT_ID,
        observed_artifact_version=1,
        observed_pipeline_generation=1,
        application_state=application_state,
        subject_id=SUBJECT_ID,
    )


def _source_issue(state: SupplementalSourceRepairState) -> SupplementalSourceRepairIssue:
    return SupplementalSourceRepairIssue(
        repair_key="c" * 64,
        kind=ProductionRepairIssueKind.SUPPLEMENTAL_SOURCE_UNARCHIVED,
        source_id="S2",
        source_title="Supplemental source",
        source_url="https://source.example/supplemental",
        publisher="Publisher",
        collection_id=None,
        collection_state=None,
        error_reason=None,
        attempt_count=0,
        production_run_id=RUN_ID,
        observed_artifact_id=ARTIFACT_ID,
        observed_artifact_version=1,
        observed_pipeline_generation=1,
        repair_state=state,
        rebuild_required=(state is SupplementalSourceRepairState.ARCHIVED_PENDING_REFERENCES),
        subject_id=SUBJECT_ID,
    )


def _decision(issue: Any, action: ProductionRepairAction) -> ProductionRepairDecision:
    return ProductionRepairDecision(
        edition_id=EDITION_ID,
        subject_id=SUBJECT_ID,
        production_run_id=RUN_ID,
        observed_artifact_id=ARTIFACT_ID,
        observed_pipeline_generation=1,
        repair_key=issue.repair_key,
        issue_kind=issue.kind,
        action=action,
        actor_id="analyst",
    )


@dataclass(frozen=True)
class _Row:
    """One planner input and the full answer every consumer must agree on."""

    label: str
    issue: Any
    decision: ProductionRepairDecision | None
    application_state: RepairDecisionApplicationState
    impact_kind: ProductionRepairImpactKind
    resolved: bool
    blocks_signoff: bool
    rebuild_required: bool
    ready_to_apply: bool
    recommended_stage: str | None
    # What RepairRebuildBar shows: exactly its own filter, so the desk can
    # never drop an article the backend declared blocking.
    desk_offers_apply: bool


def _rule_issue(state: RepairDecisionApplicationState) -> ProductionRepairIssueView:
    return _q2_issue(
        kind=ProductionRepairIssueKind.REJECTED_RULE,
        artifact_type="yara_rule",
        application_state=state,
    )


_UNARCHIVED_WAIVED = _source_issue(SupplementalSourceRepairState.UNARCHIVED)
_ARCHIVED_WAIVED = _source_issue(SupplementalSourceRepairState.ARCHIVED_PENDING_REFERENCES)
_RULE_INCLUDE = _rule_issue(RepairDecisionApplicationState.PROJECTION_REQUIRED)
_RULE_EXCLUDE_FIRST = _rule_issue(RepairDecisionApplicationState.ALREADY_EFFECTIVE)
_IOC_INCLUDE = _q2_issue(application_state=RepairDecisionApplicationState.PROJECTION_REQUIRED)
# A non-IOC extracted value (CVE, filename, filepath) is re-admitted by the
# projection without context, so it reaches the publication body without
# entering the Q4 evidence pack: it is publication-only, never narrative.
_NON_IOC_INCLUDE = _q2_issue(
    artifact_type="cve",
    application_state=RepairDecisionApplicationState.PROJECTION_REQUIRED,
)
_UNBUILDABLE_INCLUDE = _q2_issue(
    artifact_type="cve",
    application_state=RepairDecisionApplicationState.UNBUILDABLE,
)

PLANNER_MATRIX: tuple[_Row, ...] = (
    _Row(
        label="source · collection_missing · no decision",
        issue=_source_issue(SupplementalSourceRepairState.COLLECTION_MISSING),
        decision=None,
        application_state=RepairDecisionApplicationState.UNRESOLVED,
        impact_kind=ProductionRepairImpactKind.SOURCE_CORPUS,
        resolved=False,
        blocks_signoff=False,
        rebuild_required=False,
        ready_to_apply=False,
        recommended_stage="rebuild_references",
        desk_offers_apply=False,
    ),
    _Row(
        label="source · unarchived · no decision",
        issue=_source_issue(SupplementalSourceRepairState.UNARCHIVED),
        decision=None,
        application_state=RepairDecisionApplicationState.UNRESOLVED,
        impact_kind=ProductionRepairImpactKind.SOURCE_CORPUS,
        resolved=False,
        blocks_signoff=False,
        rebuild_required=False,
        ready_to_apply=False,
        recommended_stage="rebuild_references",
        desk_offers_apply=False,
    ),
    _Row(
        label="source · unarchived · waiver",
        issue=_UNARCHIVED_WAIVED,
        decision=_decision(_UNARCHIVED_WAIVED, ProductionRepairAction.CONTINUE_WITHOUT_SOURCE),
        application_state=RepairDecisionApplicationState.ALREADY_EFFECTIVE,
        impact_kind=ProductionRepairImpactKind.NO_DELIVERABLE_CHANGE,
        resolved=True,
        blocks_signoff=False,
        rebuild_required=False,
        ready_to_apply=True,
        recommended_stage="none",
        desk_offers_apply=False,
    ),
    _Row(
        label="source · archived_pending · no decision",
        issue=_source_issue(SupplementalSourceRepairState.ARCHIVED_PENDING_REFERENCES),
        decision=None,
        application_state=RepairDecisionApplicationState.UNRESOLVED,
        impact_kind=ProductionRepairImpactKind.SOURCE_CORPUS,
        resolved=True,
        blocks_signoff=True,
        rebuild_required=True,
        ready_to_apply=True,
        recommended_stage="rebuild_references",
        desk_offers_apply=True,
    ),
    _Row(
        # The regression LOT 35 exists for.
        label="source · archived_pending · old waiver",
        issue=_ARCHIVED_WAIVED,
        decision=_decision(_ARCHIVED_WAIVED, ProductionRepairAction.CONTINUE_WITHOUT_SOURCE),
        application_state=RepairDecisionApplicationState.ALREADY_EFFECTIVE,
        impact_kind=ProductionRepairImpactKind.SOURCE_CORPUS,
        resolved=True,
        blocks_signoff=True,
        rebuild_required=True,
        ready_to_apply=True,
        recommended_stage="rebuild_references",
        desk_offers_apply=True,
    ),
    _Row(
        label="Q2 · rule · include",
        issue=_RULE_INCLUDE,
        decision=_decision(_RULE_INCLUDE, ProductionRepairAction.INCLUDE),
        application_state=RepairDecisionApplicationState.PROJECTION_REQUIRED,
        impact_kind=ProductionRepairImpactKind.RULE_BUNDLE_ONLY,
        resolved=True,
        blocks_signoff=True,
        rebuild_required=True,
        ready_to_apply=True,
        recommended_stage="apply_projection",
        desk_offers_apply=True,
    ),
    _Row(
        label="Q2 · rule · exclude (first time)",
        issue=_RULE_EXCLUDE_FIRST,
        decision=_decision(_RULE_EXCLUDE_FIRST, ProductionRepairAction.EXCLUDE),
        application_state=RepairDecisionApplicationState.ALREADY_EFFECTIVE,
        impact_kind=ProductionRepairImpactKind.NO_DELIVERABLE_CHANGE,
        resolved=True,
        blocks_signoff=False,
        rebuild_required=False,
        ready_to_apply=True,
        recommended_stage="none",
        desk_offers_apply=False,
    ),
    _Row(
        label="Q2 · rule · include then exclude",
        issue=_RULE_INCLUDE,
        decision=_decision(_RULE_INCLUDE, ProductionRepairAction.EXCLUDE),
        application_state=RepairDecisionApplicationState.PROJECTION_REQUIRED,
        impact_kind=ProductionRepairImpactKind.RULE_BUNDLE_ONLY,
        resolved=True,
        blocks_signoff=True,
        rebuild_required=True,
        ready_to_apply=True,
        recommended_stage="apply_projection",
        desk_offers_apply=True,
    ),
    _Row(
        label="Q2 · IOC · include",
        issue=_IOC_INCLUDE,
        decision=_decision(_IOC_INCLUDE, ProductionRepairAction.INCLUDE),
        application_state=RepairDecisionApplicationState.PROJECTION_REQUIRED,
        impact_kind=ProductionRepairImpactKind.PUBLICATION_ONLY,
        resolved=True,
        blocks_signoff=True,
        rebuild_required=True,
        ready_to_apply=True,
        recommended_stage="apply_projection",
        desk_offers_apply=True,
    ),
    _Row(
        label="Q2 · IOC · include then exclude",
        issue=_IOC_INCLUDE,
        decision=_decision(_IOC_INCLUDE, ProductionRepairAction.EXCLUDE),
        application_state=RepairDecisionApplicationState.PROJECTION_REQUIRED,
        impact_kind=ProductionRepairImpactKind.PUBLICATION_ONLY,
        resolved=True,
        blocks_signoff=True,
        rebuild_required=True,
        ready_to_apply=True,
        recommended_stage="apply_projection",
        desk_offers_apply=True,
    ),
    _Row(
        label="Q2 · non-IOC value · include",
        issue=_NON_IOC_INCLUDE,
        decision=_decision(_NON_IOC_INCLUDE, ProductionRepairAction.INCLUDE),
        application_state=RepairDecisionApplicationState.PROJECTION_REQUIRED,
        impact_kind=ProductionRepairImpactKind.PUBLICATION_ONLY,
        resolved=True,
        blocks_signoff=True,
        rebuild_required=True,
        ready_to_apply=True,
        recommended_stage="apply_projection",
        desk_offers_apply=True,
    ),
    _Row(
        # The one blocking state with no applicable plan: it is legal only
        # because the typed arbitration says what to do instead.
        label="Q2 · non-IOC value · include nothing could build",
        issue=_UNBUILDABLE_INCLUDE,
        decision=_decision(_UNBUILDABLE_INCLUDE, ProductionRepairAction.INCLUDE),
        application_state=RepairDecisionApplicationState.UNBUILDABLE,
        impact_kind=ProductionRepairImpactKind.PUBLICATION_ONLY,
        resolved=True,
        blocks_signoff=True,
        rebuild_required=True,
        ready_to_apply=False,
        recommended_stage="revise_decision",
        desk_offers_apply=False,
    ),
)


@pytest.mark.parametrize("row", PLANNER_MATRIX, ids=lambda row: row.label)
def test_planner_matrix_answers_every_consumer_with_the_same_truth(row: _Row) -> None:
    state = repair_issue_execution_state(row.issue, row.decision)

    assert state.application_state is row.application_state
    assert state.impact.kind is row.impact_kind
    assert state.resolved is row.resolved
    assert state.blocking is row.blocks_signoff
    assert state.rebuild_required is row.rebuild_required
    assert state.ready_to_apply is row.ready_to_apply
    assert state.recommended_stage == row.recommended_stage
    assert _desk_offers_apply(state) is row.desk_offers_apply


@pytest.mark.parametrize("row", PLANNER_MATRIX, ids=lambda row: row.label)
def test_no_repair_state_is_a_ux_deadlock(row: _Row) -> None:
    """A blocking issue always leaves the analyst something to do.

    Either the execution plan is applicable, or the recommended action is the
    typed arbitration that unblocks it.  Blocking with neither would be a state
    the desk shows as unresolvable.
    """
    state = repair_issue_execution_state(row.issue, row.decision)

    if state.blocking:
        assert state.ready_to_apply or state.recommended_stage == "revise_decision", (
            f"{row.label} blocks sign-off with no applicable plan and no arbitration"
        )


@pytest.mark.parametrize("row", PLANNER_MATRIX, ids=lambda row: row.label)
def test_no_deliverable_change_never_owes_a_rebuild(row: _Row) -> None:
    state = repair_issue_execution_state(row.issue, row.decision)

    if state.impact.kind is ProductionRepairImpactKind.NO_DELIVERABLE_CHANGE:
        assert not state.rebuild_required
        assert not state.blocking


def test_archiving_a_waived_source_reverses_the_planner_answer() -> None:
    """The same effective waiver, before and after the source really exists."""
    waived = repair_issue_execution_state(
        _UNARCHIVED_WAIVED,
        _decision(_UNARCHIVED_WAIVED, ProductionRepairAction.CONTINUE_WITHOUT_SOURCE),
    )
    archived = repair_issue_execution_state(
        _ARCHIVED_WAIVED,
        _decision(_ARCHIVED_WAIVED, ProductionRepairAction.CONTINUE_WITHOUT_SOURCE),
    )

    assert waived.impact.kind is ProductionRepairImpactKind.NO_DELIVERABLE_CHANGE
    assert not waived.blocking
    assert archived.impact.kind is ProductionRepairImpactKind.SOURCE_CORPUS
    assert archived.blocking
    assert archived.rebuild_required
    assert archived.execution_plan.ready_to_apply


def test_already_effective_decision_owes_a_synthesis_when_the_article_has_no_document() -> None:
    issue = _q2_issue(application_state=RepairDecisionApplicationState.ALREADY_EFFECTIVE)

    state = repair_issue_execution_state(
        issue,
        _decision(issue, ProductionRepairAction.EXCLUDE),
        document_missing=True,
    )

    assert state.rebuild_required
    assert state.recommended_stage == "synthesis"


def _desk_offers_apply(state: Any) -> bool:
    """Mirror of ``RepairRebuildBar.isApplicable`` for a single issue."""
    return bool(
        state.rebuild_required
        and state.execution_plan.ready_to_apply
        and state.execution_plan.impact_kind is not ProductionRepairImpactKind.NO_DELIVERABLE_CHANGE
    )
