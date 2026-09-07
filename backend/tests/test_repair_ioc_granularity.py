"""An IOC arbitration is surgical: publication only, never a model call.

These tests are the executable statement of the granularity invariant. They
work on the real functional projections -- the Q4 evidence pack hash and the
publication input hash -- so they fail if a repair ever reaches the narrative
again, whatever the classifier happens to claim.
"""

from __future__ import annotations

import hashlib
from datetime import date
from uuid import UUID, uuid4

import pytest

from cti_app.application.production_parsers import (
    ParsedSource,
    ReferenceReport,
    TechnicalExtraction,
)
from cti_app.application.production_repairs import (
    EffectiveExtractionProjector,
    _impact_from_projection_hashes,
    classify_repair_impact,
    merge_repair_impacts,
    production_repair_correction_identity,
    publication_projection_hash,
    repair_application_diagnostic,
    rule_bundle_projection_hash,
    synthesis_projection_hash,
)
from cti_app.domain.discovery import SourceRole
from cti_app.domain.production import (
    ProductionDerivedOutput,
    ProductionEvidenceBasis,
    ProductionRepairAction,
    ProductionRepairCorrection,
    ProductionRepairDecision,
    ProductionRepairImpact,
    ProductionRepairImpactKind,
    ProductionRepairIssueKind,
    ProductionRepairVerificationState,
    RepairApplicationStage,
    RepairImpactInvariantError,
    RepairRemediation,
)

EDITION_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
SUBJECT_ID = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
RUN_ID = UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc")
ARTIFACT_ID = UUID("dddddddd-dddd-4ddd-8ddd-dddddddddddd")
SOURCE_ID = "S1"
SOURCE_URL = "https://source.example/report"
SYNTHESIS_TEXT = "Le groupe a exfiltré des données [S1]."

HASH_VALUE = "a" * 64
IP_VALUE = "203.0.113.7"
DOMAIN_VALUE = "evil.lot41-desk.com"


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _repair_key(seed: str) -> str:
    return _sha256(seed)


def _report() -> ReferenceReport:
    return ReferenceReport(
        sources=(
            ParsedSource(
                local_id=SOURCE_ID,
                title="Rapport",
                url=SOURCE_URL,
                canonical_url=SOURCE_URL,
                publisher="Publisher",
                published_at=date(2026, 8, 12),
                role=SourceRole.PRIMARY,
            ),
        ),
        events=(),
    )


def _entry(
    *,
    repair_key: str,
    artifact_type: str,
    value: str,
    kind: ProductionRepairIssueKind = ProductionRepairIssueKind.REJECTED_INDICATOR,
) -> dict[str, object]:
    return {
        "repair_key": repair_key,
        "kind": kind.value,
        "artifact_type": artifact_type,
        "source_id": SOURCE_ID,
        "source_url": SOURCE_URL,
        "value_sha256": _sha256(value),
        "reason_code": "source_evidence_missing",
    }


def _decision(
    repair_key: str,
    action: ProductionRepairAction,
    *,
    kind: ProductionRepairIssueKind = ProductionRepairIssueKind.REJECTED_INDICATOR,
    correction_id: UUID | None = None,
) -> ProductionRepairDecision:
    return ProductionRepairDecision(
        edition_id=EDITION_ID,
        subject_id=SUBJECT_ID,
        production_run_id=RUN_ID,
        observed_artifact_id=ARTIFACT_ID,
        observed_pipeline_generation=2,
        repair_key=repair_key,
        issue_kind=kind,
        action=action,
        actor_id="analyst",
        correction_id=correction_id,
    )


def _project(
    entries: list[dict[str, object]],
    decisions: list[ProductionRepairDecision],
    payloads: dict[str, str],
    *,
    base: TechnicalExtraction | None = None,
) -> tuple[TechnicalExtraction, TechnicalExtraction, ProductionRepairImpact]:
    """Project the decisions and classify the real before/after delta."""
    previous = base if base is not None else TechnicalExtraction(items=())
    projected = (
        EffectiveExtractionProjector()
        .project(
            base=previous,
            repair_entries=entries,
            effective_decisions=decisions,
            resolved_payloads=payloads,
        )
        .extraction
    )
    report = _report()
    impact = _impact_from_projection_hashes(
        previous,
        projected,
        previous_synthesis_projection_hash=synthesis_projection_hash(report, previous, {}),
        new_synthesis_projection_hash=synthesis_projection_hash(report, projected, {}),
        previous_publication_projection_hash=publication_projection_hash(
            report, previous, SYNTHESIS_TEXT
        ),
        new_publication_projection_hash=publication_projection_hash(
            report, projected, SYNTHESIS_TEXT
        ),
        previous_rule_bundle_hash=rule_bundle_projection_hash(previous),
        new_rule_bundle_hash=rule_bundle_projection_hash(projected),
    )
    return previous, projected, impact


def _assert_synthesis_untouched(
    previous: TechnicalExtraction,
    projected: TechnicalExtraction,
    impact: ProductionRepairImpact,
) -> None:
    report = _report()
    assert synthesis_projection_hash(report, previous, {}) == synthesis_projection_hash(
        report, projected, {}
    ), "the Q4 evidence pack changed: the repair leaked into the narrative"
    assert not impact.model_call_required
    assert impact.kind is not ProductionRepairImpactKind.NARRATIVE
    assert ProductionDerivedOutput.SYNTHESIS not in impact.affected_outputs
    assert ProductionDerivedOutput.REFERENCES not in impact.affected_outputs


@pytest.mark.parametrize(
    ("artifact_type", "value"),
    [
        ("hash", HASH_VALUE),
        ("ip", IP_VALUE),
        ("domain", DOMAIN_VALUE),
        ("url", "https://evil.lot41-desk.com/payload"),
        ("email", "actor@evil.lot41-desk.com"),
    ],
)
def test_ioc_include_does_not_require_model(artifact_type: str, value: str) -> None:
    key = _repair_key(value)
    previous, projected, impact = _project(
        [_entry(repair_key=key, artifact_type=artifact_type, value=value)],
        [_decision(key, ProductionRepairAction.INCLUDE)],
        {key: value},
    )

    assert impact.kind is ProductionRepairImpactKind.PUBLICATION_ONLY
    assert [item.value for item in projected.items] == [value]
    _assert_synthesis_untouched(previous, projected, impact)


def test_ioc_include_only_rebuilds_publication() -> None:
    """The synthesis artifact is byte-identical; only the publication moves."""
    key = _repair_key(HASH_VALUE)
    report = _report()
    previous, projected, impact = _project(
        [_entry(repair_key=key, artifact_type="hash", value=HASH_VALUE)],
        [_decision(key, ProductionRepairAction.INCLUDE)],
        {key: HASH_VALUE},
    )

    assert impact.affected_outputs == frozenset(
        {ProductionDerivedOutput.EXTRACTION, ProductionDerivedOutput.CHECKPOINT}
    ) | {ProductionDerivedOutput.PUBLICATION}
    assert publication_projection_hash(
        report, previous, SYNTHESIS_TEXT
    ) != publication_projection_hash(report, projected, SYNTHESIS_TEXT)
    _assert_synthesis_untouched(previous, projected, impact)


def test_ioc_exclude_of_an_unprojected_value_changes_nothing() -> None:
    key = _repair_key(IP_VALUE)
    previous, projected, impact = _project(
        [_entry(repair_key=key, artifact_type="ip", value=IP_VALUE)],
        [_decision(key, ProductionRepairAction.EXCLUDE)],
        {},
    )

    assert projected.items == ()
    assert impact.kind is ProductionRepairImpactKind.NO_DELIVERABLE_CHANGE
    assert impact.affected_outputs == frozenset()
    _assert_synthesis_untouched(previous, projected, impact)


def test_ioc_exclude_after_include_removes_it_from_the_publication_only() -> None:
    """Revising an applied INCLUDE is symmetric: it withdraws the value only.

    Both projections start from the same immutable Q2 extraction -- that is
    what makes the decision log append-only and the extraction immutable -- so
    the delta compared here is the projected INCLUDE against the projected
    EXCLUDE.
    """
    key = _repair_key(DOMAIN_VALUE)
    entry = _entry(repair_key=key, artifact_type="domain", value=DOMAIN_VALUE)
    base = TechnicalExtraction(items=())
    projector = EffectiveExtractionProjector()
    included = projector.project(
        base=base,
        repair_entries=[entry],
        effective_decisions=[_decision(key, ProductionRepairAction.INCLUDE)],
        resolved_payloads={key: DOMAIN_VALUE},
    ).extraction
    excluded = projector.project(
        base=base,
        repair_entries=[entry],
        effective_decisions=[_decision(key, ProductionRepairAction.EXCLUDE)],
        resolved_payloads={},
    ).extraction

    report = _report()
    impact = _impact_from_projection_hashes(
        included,
        excluded,
        previous_synthesis_projection_hash=synthesis_projection_hash(report, included, {}),
        new_synthesis_projection_hash=synthesis_projection_hash(report, excluded, {}),
        previous_publication_projection_hash=publication_projection_hash(
            report, included, SYNTHESIS_TEXT
        ),
        new_publication_projection_hash=publication_projection_hash(
            report, excluded, SYNTHESIS_TEXT
        ),
        previous_rule_bundle_hash=rule_bundle_projection_hash(included),
        new_rule_bundle_hash=rule_bundle_projection_hash(excluded),
    )

    assert [item.value for item in included.items] == [DOMAIN_VALUE]
    assert excluded.items == ()
    assert impact.kind is ProductionRepairImpactKind.PUBLICATION_ONLY
    _assert_synthesis_untouched(included, excluded, impact)


def _correction(original_key: str, corrected: str) -> ProductionRepairCorrection:
    state = ProductionRepairVerificationState.SOURCE_VERIFIED
    return ProductionRepairCorrection(
        id=production_repair_correction_identity(
            original_repair_key=original_key,
            artifact_type="hash",
            source_id=SOURCE_ID,
            source_url=SOURCE_URL,
            replacement_value_sha256=_sha256(corrected),
            verification_state=state,
        ),
        edition_id=EDITION_ID,
        subject_id=SUBJECT_ID,
        production_run_id=RUN_ID,
        original_repair_key=original_key,
        artifact_type="hash",
        source_id=SOURCE_ID,
        source_url=SOURCE_URL,
        replacement_value_sha256=_sha256(corrected),
        replacement_payload_blob_id=uuid4(),
        actor_id="analyst",
        verification_state=state,
    )


def test_ioc_replace_value_updates_publication() -> None:
    """A corrected value publishes the new hash and drops the old one."""
    original = "b" * 64
    corrected = "c" * 64
    key = _repair_key(original)
    correction = _correction(key, corrected)
    # The entry carries the corrected identity: the original extraction row is
    # never rewritten, the correction is a new append-only fact about it.
    entry = _entry(repair_key=key, artifact_type="hash", value=corrected)

    previous, projected, impact = _project(
        [entry],
        [_decision(key, ProductionRepairAction.REPLACE, correction_id=correction.id)],
        {key: corrected},
    )

    published = [item.value for item in projected.items]
    assert published == [corrected]
    assert original not in published
    assert correction.original_repair_key == key
    assert correction.replacement_value_sha256 == _sha256(corrected)
    assert impact.kind is ProductionRepairImpactKind.PUBLICATION_ONLY
    _assert_synthesis_untouched(previous, projected, impact)


def test_ioc_multiple_values_stay_publication_only() -> None:
    values = {
        "hash": HASH_VALUE,
        "ip": IP_VALUE,
        "domain": DOMAIN_VALUE,
        "email": "actor@evil.lot41-desk.com",
    }
    entries = []
    decisions = []
    payloads = {}
    for artifact_type, value in values.items():
        key = _repair_key(f"{artifact_type}:{value}")
        entries.append(_entry(repair_key=key, artifact_type=artifact_type, value=value))
        decisions.append(_decision(key, ProductionRepairAction.INCLUDE))
        payloads[key] = value

    previous, projected, impact = _project(entries, decisions, payloads)

    assert sorted(item.value for item in projected.items) == sorted(values.values())
    assert impact.kind is ProductionRepairImpactKind.PUBLICATION_ONLY
    _assert_synthesis_untouched(previous, projected, impact)


def test_ioc_mixed_with_a_non_ioc_value_never_becomes_narrative() -> None:
    """The batch case that produced the defect: one accept, many decisions.

    A repair is applied for the whole article, so an IOC accepted beside other
    arbitrations used to inherit their narrative cost. No arbitration of an
    extracted value may charge a synthesis rebuild, whatever it is batched
    with.
    """
    ioc_key = _repair_key(HASH_VALUE)
    cve_value = "CVE-2026-1234"
    cve_key = _repair_key(cve_value)

    previous, projected, impact = _project(
        [
            _entry(repair_key=ioc_key, artifact_type="hash", value=HASH_VALUE),
            _entry(repair_key=cve_key, artifact_type="cve", value=cve_value),
        ],
        [
            _decision(ioc_key, ProductionRepairAction.INCLUDE),
            _decision(cve_key, ProductionRepairAction.INCLUDE),
        ],
        {ioc_key: HASH_VALUE, cve_key: cve_value},
    )

    assert sorted(item.value for item in projected.items) == sorted([HASH_VALUE, cve_value])
    assert impact.kind is ProductionRepairImpactKind.PUBLICATION_ONLY
    _assert_synthesis_untouched(previous, projected, impact)


def test_merging_an_ioc_with_a_rule_repair_costs_no_model_call() -> None:
    ioc = classify_repair_impact(
        _issue_view("hash"), _decision(_repair_key(HASH_VALUE), ProductionRepairAction.INCLUDE)
    )
    rule = classify_repair_impact(
        _issue_view("yara_rule", kind=ProductionRepairIssueKind.REJECTED_RULE),
        _decision(
            _repair_key("rule"),
            ProductionRepairAction.INCLUDE,
            kind=ProductionRepairIssueKind.REJECTED_RULE,
        ),
    )

    merged = merge_repair_impacts([ioc, rule])

    assert merged.kind is ProductionRepairImpactKind.PUBLICATION_ONLY
    assert not merged.model_call_required
    assert merged.provider_steps == ()
    assert ProductionDerivedOutput.SYNTHESIS not in merged.affected_outputs
    assert ProductionDerivedOutput.RULE_BUNDLE in merged.affected_outputs


def _issue_view(
    artifact_type: str,
    *,
    kind: ProductionRepairIssueKind = ProductionRepairIssueKind.REJECTED_INDICATOR,
) -> object:
    from cti_app.application.production_repairs import ProductionRepairIssueView
    from cti_app.domain.production import RepairDecisionApplicationState

    return ProductionRepairIssueView(
        repair_key="e" * 64,
        kind=kind,
        artifact_type=artifact_type,
        source_id=SOURCE_ID,
        source_title="Source",
        is_publication_ioc=artifact_type not in {"yara_rule", "sigma_rule"},
        source_url=SOURCE_URL,
        reason_code="source_evidence_missing",
        value_sha256="f" * 64,
        preview="value",
        payload_available=True,
        production_run_id=RUN_ID,
        observed_artifact_id=ARTIFACT_ID,
        observed_artifact_version=1,
        observed_pipeline_generation=2,
        application_state=RepairDecisionApplicationState.PROJECTION_REQUIRED,
        subject_id=SUBJECT_ID,
    )


@pytest.mark.parametrize(
    "kind",
    [
        ProductionRepairImpactKind.NO_DELIVERABLE_CHANGE,
        ProductionRepairImpactKind.RULE_BUNDLE_ONLY,
        ProductionRepairImpactKind.PUBLICATION_ONLY,
    ],
)
def test_a_non_narrative_impact_cannot_declare_narrative_work(
    kind: ProductionRepairImpactKind,
) -> None:
    """The invariant is structural: an illegal plan cannot be constructed."""
    with pytest.raises(RepairImpactInvariantError, match="model call"):
        ProductionRepairImpact(
            kind=kind,
            affected_outputs=frozenset({ProductionDerivedOutput.PUBLICATION}),
            model_call_required=True,
            reason="illegal",
        )
    with pytest.raises(RepairImpactInvariantError, match="synthesis"):
        ProductionRepairImpact(
            kind=kind,
            affected_outputs=frozenset({ProductionDerivedOutput.SYNTHESIS}),
            model_call_required=False,
            reason="illegal",
        )
    with pytest.raises(RepairImpactInvariantError, match="references"):
        ProductionRepairImpact(
            kind=kind,
            affected_outputs=frozenset({ProductionDerivedOutput.REFERENCES}),
            model_call_required=False,
            reason="illegal",
        )
    with pytest.raises(RepairImpactInvariantError, match="provider steps"):
        ProductionRepairImpact(
            kind=kind,
            affected_outputs=frozenset(),
            model_call_required=False,
            reason="illegal",
            provider_steps=("Nouvelle synthèse",),
        )


def test_a_narrative_impact_may_still_declare_a_model_call() -> None:
    impact = ProductionRepairImpact(
        kind=ProductionRepairImpactKind.SOURCE_CORPUS,
        affected_outputs=frozenset(
            {ProductionDerivedOutput.REFERENCES, ProductionDerivedOutput.SYNTHESIS}
        ),
        model_call_required=True,
        reason="A supplemental source changes the corpus.",
    )

    assert impact.model_call_required


def test_a_failed_application_names_its_stage_and_remediation() -> None:
    diagnostic = repair_application_diagnostic(
        "repair_payload_unavailable",
        "repair_payload_unavailable",
        repair_id=str(SUBJECT_ID),
    )

    assert diagnostic == {
        "repair_id": str(SUBJECT_ID),
        "stage": RepairApplicationStage.PAYLOAD.value,
        "error_code": "repair_payload_unavailable",
        "message": "repair_payload_unavailable",
        "remediation": RepairRemediation.RERUN_EXTRACTION.value,
    }


def test_the_rebuild_endpoint_returns_the_diagnostic_not_an_opaque_conflict() -> None:
    """The HTTP detail keeps ``code``/``message`` and adds what to do next."""
    from cti_app.api.publication import _rebuild_error
    from cti_app.application.production_repairs import ProductionRepairProjectionError

    error = _rebuild_error(
        ProductionRepairProjectionError("production_repair_qa_failed"),
        repair_id=str(SUBJECT_ID),
    )

    assert error.status_code == 409
    assert error.detail == {
        "code": "production_repair_qa_failed",
        "message": "production_repair_qa_failed",
        "repair_id": str(SUBJECT_ID),
        "stage": RepairApplicationStage.QA.value,
        "error_code": "production_repair_qa_failed",
        "remediation": RepairRemediation.OPEN_QA_REPORT.value,
    }


def test_an_unmapped_failure_still_tells_the_analyst_what_to_do() -> None:
    diagnostic = repair_application_diagnostic("brand_new_code", "", repair_id="R1")

    assert diagnostic["stage"] == RepairApplicationStage.PROJECTION.value
    assert diagnostic["remediation"] == RepairRemediation.RELOAD_REPAIR_QUEUE.value
    assert diagnostic["message"] == "brand_new_code"


def test_the_evidence_basis_of_a_repair_never_reopens_the_narrative() -> None:
    """A SOURCE_VERIFIED correction is still context-free, so still Q4-free."""
    key = _repair_key(HASH_VALUE)
    entry = _entry(repair_key=key, artifact_type="hash", value=HASH_VALUE)
    entry["evidence_basis"] = ProductionEvidenceBasis.SOURCE_VERIFIED.value

    previous, projected, impact = _project(
        [entry], [_decision(key, ProductionRepairAction.INCLUDE)], {key: HASH_VALUE}
    )

    assert projected.items[0].evidence_basis is ProductionEvidenceBasis.SOURCE_VERIFIED
    _assert_synthesis_untouched(previous, projected, impact)
