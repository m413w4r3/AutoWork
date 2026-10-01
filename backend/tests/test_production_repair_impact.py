from __future__ import annotations

from uuid import UUID

import pytest

from cti_app.application.production_repairs import (
    ProductionRepairIssueView,
    SupplementalSourceRepairIssue,
    classify_repair_impact,
    merge_repair_impacts,
)
from cti_app.domain.production import (
    ProductionDerivedOutput,
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


def _issue(
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


def _decision(
    issue: ProductionRepairIssueView, action: ProductionRepairAction
) -> ProductionRepairDecision:
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


@pytest.mark.parametrize("artifact_type", ["domain", "ip", "hash", "url", "email"])
def test_include_publication_ioc_is_publication_only(artifact_type: str) -> None:
    issue = _issue(artifact_type=artifact_type)

    impact = classify_repair_impact(issue, _decision(issue, ProductionRepairAction.INCLUDE))

    assert impact.kind is ProductionRepairImpactKind.PUBLICATION_ONLY
    assert impact.affected_outputs == frozenset(
        {
            ProductionDerivedOutput.EXTRACTION,
            ProductionDerivedOutput.EDITORIAL_ENRICHMENT,
            ProductionDerivedOutput.PUBLICATION,
            ProductionDerivedOutput.CHECKPOINT,
        }
    )
    assert not impact.model_call_required


def test_include_yara_is_rule_bundle_only_without_model_call() -> None:
    issue = _issue(
        kind=ProductionRepairIssueKind.REJECTED_RULE,
        artifact_type="yara_rule",
    )

    impact = classify_repair_impact(issue, _decision(issue, ProductionRepairAction.INCLUDE))

    assert impact.kind is ProductionRepairImpactKind.RULE_BUNDLE_ONLY
    assert impact.affected_outputs == frozenset(
        {
            ProductionDerivedOutput.EXTRACTION,
            ProductionDerivedOutput.EDITORIAL_ENRICHMENT,
            ProductionDerivedOutput.RULE_BUNDLE,
            ProductionDerivedOutput.CHECKPOINT,
        }
    )
    assert not impact.model_call_required


@pytest.mark.parametrize(
    ("kind", "artifact_type"),
    [
        (ProductionRepairIssueKind.REJECTED_RULE, "yara_rule"),
        (ProductionRepairIssueKind.REJECTED_INDICATOR, "domain"),
    ],
)
def test_exclude_previously_projected_content_keeps_its_impact(
    kind: ProductionRepairIssueKind, artifact_type: str
) -> None:
    issue = _issue(
        kind=kind,
        artifact_type=artifact_type,
        application_state=RepairDecisionApplicationState.PROJECTION_REQUIRED,
    )

    impact = classify_repair_impact(issue, _decision(issue, ProductionRepairAction.EXCLUDE))

    expected = (
        ProductionRepairImpactKind.RULE_BUNDLE_ONLY
        if kind is ProductionRepairIssueKind.REJECTED_RULE
        else ProductionRepairImpactKind.PUBLICATION_ONLY
    )
    assert impact.kind is expected
    assert not impact.model_call_required


def test_first_exclude_of_already_rejected_ioc_has_no_deliverable_change() -> None:
    issue = _issue(application_state=RepairDecisionApplicationState.ALREADY_EFFECTIVE)

    impact = classify_repair_impact(issue, _decision(issue, ProductionRepairAction.EXCLUDE))

    assert impact.kind is ProductionRepairImpactKind.NO_DELIVERABLE_CHANGE
    assert impact.affected_outputs == frozenset()


def test_include_of_already_materialized_value_has_no_deliverable_change() -> None:
    issue = _issue(application_state=RepairDecisionApplicationState.ALREADY_EFFECTIVE)

    impact = classify_repair_impact(issue, _decision(issue, ProductionRepairAction.INCLUDE))

    assert impact.kind is ProductionRepairImpactKind.NO_DELIVERABLE_CHANGE
    assert impact.affected_outputs == frozenset()


@pytest.mark.parametrize("artifact_type", ["filename", "filepath", "cve"])
def test_include_non_ioc_artifact_is_publication_only(artifact_type: str) -> None:
    """A non-IOC extracted value reaches the body, never the Q4 evidence pack.

    The projection admits it without context and without an evidence quote, so
    the publication can render it deterministically from the existing
    synthesis. Charging it a narrative rebuild was the defect this pins.
    """
    issue = _issue(artifact_type=artifact_type)

    impact = classify_repair_impact(issue, _decision(issue, ProductionRepairAction.INCLUDE))

    assert impact.kind is ProductionRepairImpactKind.PUBLICATION_ONLY
    assert not impact.model_call_required
    assert ProductionDerivedOutput.SYNTHESIS not in impact.affected_outputs


@pytest.mark.parametrize("action", [ProductionRepairAction.INCLUDE, ProductionRepairAction.EXCLUDE])
@pytest.mark.parametrize(
    "artifact_type", ["hash", "ip", "domain", "url", "email", "filename", "filepath", "cve"]
)
def test_no_extraction_value_repair_is_ever_narrative(
    artifact_type: str, action: ProductionRepairAction
) -> None:
    """The granularity invariant, stated once over the whole action matrix."""
    for state in RepairDecisionApplicationState:
        issue = _issue(artifact_type=artifact_type, application_state=state)

        impact = classify_repair_impact(issue, _decision(issue, action))

        assert impact.kind is not ProductionRepairImpactKind.NARRATIVE
        assert not impact.model_call_required
        assert ProductionDerivedOutput.SYNTHESIS not in impact.affected_outputs
        assert ProductionDerivedOutput.REFERENCES not in impact.affected_outputs


def _source_issue(
    state: SupplementalSourceRepairState,
) -> SupplementalSourceRepairIssue:
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
        subject_id=SUBJECT_ID,
    )


def test_continue_without_source_has_no_deliverable_change() -> None:
    issue = _source_issue(SupplementalSourceRepairState.UNARCHIVED)
    decision = ProductionRepairDecision(
        edition_id=EDITION_ID,
        subject_id=SUBJECT_ID,
        production_run_id=RUN_ID,
        observed_artifact_id=ARTIFACT_ID,
        observed_pipeline_generation=1,
        repair_key=issue.repair_key,
        issue_kind=issue.kind,
        action=ProductionRepairAction.CONTINUE_WITHOUT_SOURCE,
        actor_id="analyst",
    )

    impact = classify_repair_impact(issue, decision)

    assert impact.kind is ProductionRepairImpactKind.NO_DELIVERABLE_CHANGE
    assert impact.affected_outputs == frozenset()


def test_archived_source_pending_references_invalidates_full_chain() -> None:
    impact = classify_repair_impact(
        _source_issue(SupplementalSourceRepairState.ARCHIVED_PENDING_REFERENCES), None
    )

    assert impact.kind is ProductionRepairImpactKind.SOURCE_CORPUS
    assert impact.model_call_required
    assert impact.affected_outputs == frozenset(
        {
            ProductionDerivedOutput.REFERENCES,
            ProductionDerivedOutput.EXTRACTION,
            ProductionDerivedOutput.SYNTHESIS,
            ProductionDerivedOutput.EDITORIAL_ENRICHMENT,
            ProductionDerivedOutput.PUBLICATION,
            ProductionDerivedOutput.CHECKPOINT,
        }
    )


def test_merge_rule_and_ioc_uses_publication_dominance_and_unions_outputs() -> None:
    rule = _issue(
        kind=ProductionRepairIssueKind.REJECTED_RULE,
        artifact_type="yara_rule",
    )
    ioc = _issue(artifact_type="domain")

    merged = merge_repair_impacts(
        (
            classify_repair_impact(rule, _decision(rule, ProductionRepairAction.INCLUDE)),
            classify_repair_impact(ioc, _decision(ioc, ProductionRepairAction.INCLUDE)),
        )
    )

    assert merged.kind is ProductionRepairImpactKind.PUBLICATION_ONLY
    assert merged.affected_outputs == frozenset(
        {
            ProductionDerivedOutput.EXTRACTION,
            ProductionDerivedOutput.EDITORIAL_ENRICHMENT,
            ProductionDerivedOutput.PUBLICATION,
            ProductionDerivedOutput.RULE_BUNDLE,
            ProductionDerivedOutput.CHECKPOINT,
        }
    )
    assert not merged.model_call_required
    assert merged.ready_to_apply
    assert "Mise à jour des fichiers YARA/Sigma" in merged.deterministic_steps
    assert "Enrichissement éditorial" in merged.deterministic_steps
    assert "Rendu Publication" in merged.deterministic_steps


def test_merge_source_dominates_narrative_and_preserves_model_steps() -> None:
    source = classify_repair_impact(
        _source_issue(SupplementalSourceRepairState.ARCHIVED_PENDING_REFERENCES), None
    )
    narrative_issue = _issue(artifact_type="filename")
    narrative = classify_repair_impact(
        narrative_issue,
        _decision(narrative_issue, ProductionRepairAction.INCLUDE),
    )

    merged = merge_repair_impacts((narrative, source))

    assert merged.kind is ProductionRepairImpactKind.SOURCE_CORPUS
    assert ProductionDerivedOutput.REFERENCES in merged.affected_outputs
    assert ProductionDerivedOutput.SYNTHESIS in merged.affected_outputs
    assert merged.model_call_required
    assert merged.provider_steps == (
        "Nouvelle extraction possible",
        "Nouvelle synthèse possible",
    )
