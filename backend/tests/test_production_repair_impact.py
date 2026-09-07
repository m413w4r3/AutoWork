from __future__ import annotations

import hashlib
from datetime import date
from uuid import UUID

import pytest

from cti_app.application.production_parsers import (
    DisplayPolicy,
    ExtractionItem,
    IndicatorStatus,
    ParsedSource,
    ReferenceReport,
    SemanticType,
    TechnicalExtraction,
)
from cti_app.application.production_repairs import (
    ProductionRepairIssueView,
    SupplementalSourceRepairIssue,
    classify_repair_impact,
    extraction_item_contributes_to_synthesis,
    publication_projection_hash,
    rule_bundle_projection_hash,
    synthesis_projection_hash,
    synthesis_projection_payload,
)
from cti_app.application.production_workflow import ProductionWorkflowOrchestrator
from cti_app.domain.discovery import SourceRole
from cti_app.domain.production import (
    DetectionRule,
    DetectionRuleType,
    ProductionDerivedOutput,
    ProductionEvidenceBasis,
    ProductionRepairAction,
    ProductionRepairDecision,
    ProductionRepairImpactKind,
    ProductionRepairIssueKind,
    RepairDecisionApplicationState,
    SupplementalSourceRepairState,
)
from cti_app.domain.publication import ArtifactType, is_publication_ioc_artifact_type

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
def test_include_non_ioc_artifact_is_narrative(artifact_type: str) -> None:
    issue = _issue(artifact_type=artifact_type)

    impact = classify_repair_impact(issue, _decision(issue, ProductionRepairAction.INCLUDE))

    assert impact.kind is ProductionRepairImpactKind.NARRATIVE
    assert impact.model_call_required
    assert ProductionDerivedOutput.SYNTHESIS in impact.affected_outputs


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
            ProductionDerivedOutput.PUBLICATION,
            ProductionDerivedOutput.CHECKPOINT,
        }
    )


def _report() -> ReferenceReport:
    return ReferenceReport(
        sources=(
            ParsedSource(
                local_id="S1",
                title="Source",
                url="https://source.example/report",
                canonical_url="https://source.example/report",
                publisher="Publisher",
                published_at=date(2026, 1, 1),
                role=SourceRole.PRIMARY,
            ),
        ),
        events=(),
    )


def _item(
    value: str,
    artifact_type: ArtifactType,
    *,
    context: str = "",
    evidence_basis: ProductionEvidenceBasis = ProductionEvidenceBasis.SOURCE_VERIFIED,
) -> ExtractionItem:
    return ExtractionItem(
        local_id=value,
        category="network_artifacts"
        if is_publication_ioc_artifact_type(artifact_type)
        else "files",
        value=value,
        context=context,
        artifact_type=artifact_type,
        attack_id=None,
        reference_ids=(),
        source_ids=("S1",),
        supported=True,
        semantic_type=SemanticType.INDICATOR,
        indicator_status=IndicatorStatus.CONFIRMED_IOC,
        display_policy=DisplayPolicy.IOC_SECTION,
        evidence_basis=evidence_basis,
    )


def _rule(body: str, name: str = "Example") -> DetectionRule:
    return DetectionRule(
        rule_type=DetectionRuleType.YARA,
        name=name,
        body=body,
        source_ids=("S1",),
        context="rule context",
        evidence_quote="rule evidence",
        supported=True,
        model_run_ids=("model-run-must-not-hash",),
        sha256=hashlib.sha256(body.encode()).hexdigest(),
    )


def test_analyst_override_ioc_does_not_change_synthesis_projection() -> None:
    manual_ioc = _item(
        "override.example",
        ArtifactType.DOMAIN,
        evidence_basis=ProductionEvidenceBasis.ANALYST_OVERRIDE,
    )
    base = TechnicalExtraction(items=())
    with_override = TechnicalExtraction(items=(manual_ioc,))

    assert not extraction_item_contributes_to_synthesis(manual_ioc)
    assert synthesis_projection_hash(_report(), base, {}) == synthesis_projection_hash(
        _report(), with_override, {}
    )
    assert (
        ProductionWorkflowOrchestrator._build_synthesis_evidence_pack(_report(), with_override, {})[
            "technical_extraction"
        ]["items"]
        == []
    )


def test_source_verified_ioc_with_context_contributes_to_q4() -> None:
    contextual_ioc = _item(
        "verified.example",
        ArtifactType.DOMAIN,
        context="serveur de commande et contrôle",
    )
    base = TechnicalExtraction(items=())
    with_ioc = TechnicalExtraction(items=(contextual_ioc,))

    assert extraction_item_contributes_to_synthesis(contextual_ioc)
    assert synthesis_projection_hash(_report(), base, {}) != synthesis_projection_hash(
        _report(), with_ioc, {}
    )


def test_synthesis_projection_never_contains_detection_rules() -> None:
    extraction = TechnicalExtraction(items=(), rules=(_rule("rule A"),))

    payload = synthesis_projection_payload(_report(), extraction, {})

    assert "rules" not in payload
    assert "detection_rules" not in payload["technical_extraction"]


def test_rule_body_changes_rule_bundle_hash_but_not_publication_hash() -> None:
    report = _report()
    extraction_a = TechnicalExtraction(
        items=(_item("x.example", ArtifactType.DOMAIN),), rules=(_rule("rule A"),)
    )
    extraction_b = TechnicalExtraction(items=extraction_a.items, rules=(_rule("rule B"),))

    assert rule_bundle_projection_hash(extraction_a) != rule_bundle_projection_hash(extraction_b)
    assert publication_projection_hash(
        report, extraction_a, "Texte [S1]."
    ) == publication_projection_hash(report, extraction_b, "Texte [S1].")


def test_publication_projection_changes_for_analyst_override_ioc() -> None:
    report = _report()
    base = TechnicalExtraction(items=(_item("source.example", ArtifactType.DOMAIN),))
    override = TechnicalExtraction(
        items=(
            *base.items,
            _item(
                "manual.example",
                ArtifactType.DOMAIN,
                evidence_basis=ProductionEvidenceBasis.ANALYST_OVERRIDE,
            ),
        )
    )

    assert publication_projection_hash(report, base, "Texte [S1].") != publication_projection_hash(
        report, override, "Texte [S1]."
    )


def test_rule_and_ioc_order_does_not_change_projection_hashes() -> None:
    report = _report()
    item_a = _item("a.example", ArtifactType.DOMAIN)
    item_b = _item("b.example", ArtifactType.IP)
    rule_a = _rule("rule A", "A")
    rule_b = _rule("rule B", "B")
    first = TechnicalExtraction(items=(item_a, item_b), rules=(rule_a, rule_b))
    reversed_order = TechnicalExtraction(items=(item_b, item_a), rules=(rule_b, rule_a))

    assert synthesis_projection_hash(report, first, {}) == synthesis_projection_hash(
        report, reversed_order, {}
    )
    assert publication_projection_hash(report, first, "Texte [S1].") == publication_projection_hash(
        report, reversed_order, "Texte [S1]."
    )
    assert rule_bundle_projection_hash(first) == rule_bundle_projection_hash(reversed_order)


def test_projection_hashes_are_stable_across_repeated_serialization() -> None:
    report = _report()
    extraction = TechnicalExtraction(
        items=(_item("stable.example", ArtifactType.DOMAIN),), rules=(_rule("stable"),)
    )

    first = (
        synthesis_projection_hash(report, extraction, {"https://source.example/report": "core"}),
        publication_projection_hash(report, extraction, "Texte [S1]."),
        rule_bundle_projection_hash(extraction),
    )
    second = (
        synthesis_projection_hash(report, extraction, {"https://source.example/report": "core"}),
        publication_projection_hash(report, extraction, "Texte [S1]."),
        rule_bundle_projection_hash(extraction),
    )

    assert first == second
