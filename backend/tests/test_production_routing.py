from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from cti_app.application.production_routing import _projection_has_incomplete_coverage
from cti_app.domain.collection import CollectionState, SourceAcquisitionMethod
from cti_app.domain.discovery import SourceRole
from cti_app.domain.legitimate_services import LegitimateServiceRegistry
from cti_app.domain.production import (
    ExtractionProfile,
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionArtifactStatus,
)
from cti_app.domain.production_extraction import (
    PRODUCTION_EXTRACTION_SCHEMA_VERSION,
    ExtractionFactV1,
    ExtractionIndicatorStatus,
    ExtractionIndicatorV1,
    ExtractionReuseState,
    ProductionEvidenceBasis,
    ProductionExtractionOmissionReason,
    ProductionExtractionOmissionV1,
    ProductionExtractionV1,
    ProductionSourceExtractionV1,
    production_extraction_hashes,
)
from cti_app.domain.production_references import (
    PRODUCTION_SOURCE_CRITICALITY_POLICY_VERSION,
    ProductionEditorialRole,
    ProductionReferenceKind,
    ProductionReferenceTier,
    ProductionSourceCriticality,
    ProductionSourceCriticalityReason,
)
from cti_app.domain.production_relevance import (
    RelevanceClassification,
    RelevanceClassificationItemV1,
    RelevanceDecisionProvenance,
    RelevanceProjectionV1,
    RelevanceReasonCode,
    relevance_projection_to_json,
)
from cti_app.domain.production_routing import (
    ProductionRoutingArtifactInputV1,
    RoutingCriticalSourceDiagnosticV1,
    RoutingReasonCode,
    RoutingStatus,
    decide_publication_routing,
)
from cti_app.domain.production_synthesis import (
    EvidenceKind,
    extraction_evidence_elements,
    extraction_evidence_refs_v1,
)
from cti_app.domain.publication import ArtifactType
from cti_app.domain.publication_kind import PublicationKind
from cti_app.domain.subject_ioc_eligibility import is_subject_ioc_eligible

RUN_ID = uuid4()
SUBJECT_ID = uuid4()
NOW = datetime(2026, 11, 5, 12, tzinfo=UTC)
_SERVICES = LegitimateServiceRegistry(())


def _indicator(
    value: str,
    artifact_type: ArtifactType = ArtifactType.DOMAIN,
    status: ExtractionIndicatorStatus = ExtractionIndicatorStatus.CONFIRMED_IOC,
    *,
    source_id=None,
    evidence_basis=ProductionEvidenceBasis.SOURCE_VERIFIED,
) -> ExtractionIndicatorV1:
    source_id = source_id or uuid4()
    return ExtractionIndicatorV1(
        value=value,
        artifact_type=artifact_type,
        indicator_status=status,
        context="Observed as command infrastructure.",
        evidence_quote=f"The report identifies {value}.",
        evidence_basis=evidence_basis,
        source_document_ids=(source_id,),
    )


def _source(
    *,
    source_id,
    indicators=(),
    facts=(),
    tier=ProductionReferenceTier.CORE,
    editorial_role=ProductionEditorialRole.PRIMARY,
) -> ProductionSourceExtractionV1:
    return ProductionSourceExtractionV1(
        source_document_id=source_id,
        canonical_url=f"https://source-{str(source_id)[:8]}.example/report",
        content_sha256="a" * 64,
        tier=tier,
        kind=ProductionReferenceKind.PUBLICATION,
        role=SourceRole.PRIMARY if tier is ProductionReferenceTier.CORE else SourceRole.INDEPENDENT,
        profile=ExtractionProfile.FULL,
        checkpoint_id=None,
        reuse_state=ExtractionReuseState.FRESH,
        facts=tuple(facts),
        events=(),
        indicators=tuple(indicators),
        rules=(),
        uncertainties=(),
        editorial_role=editorial_role,
    )


def _extraction(
    *,
    indicators=(),
    facts=(),
    subject_id: UUID = SUBJECT_ID,
    tier=ProductionReferenceTier.CORE,
    editorial_role=ProductionEditorialRole.PRIMARY,
    omitted_sources=(),
) -> ProductionExtractionV1:
    source_id = uuid4()
    bound_indicators = tuple(
        ExtractionIndicatorV1(
            value=item.value,
            artifact_type=item.artifact_type,
            indicator_status=item.indicator_status,
            context=item.context,
            evidence_quote=item.evidence_quote,
            evidence_basis=item.evidence_basis,
            source_document_ids=(source_id,),
        )
        for item in indicators
    )
    bound_facts = tuple(
        ExtractionFactV1(
            category="malware",
            value=f"Malware {index}",
            attack_id=None,
            context="Observed in the reporting.",
            evidence_quote=f"The source names malware {index}.",
            evidence_basis=ProductionEvidenceBasis.SOURCE_VERIFIED,
            source_document_ids=(source_id,),
        )
        for index, _fact in enumerate(facts, start=1)
    )
    return ProductionExtractionV1(
        schema_version=PRODUCTION_EXTRACTION_SCHEMA_VERSION,
        subject_id=subject_id,
        production_input_hash="b" * 64,
        references_corpus_hash="c" * 64,
        profile_policy_version="test-routing-profile-v1",
        sources=(
            _source(
                source_id=source_id,
                indicators=bound_indicators,
                facts=bound_facts,
                tier=tier,
                editorial_role=editorial_role,
            ),
        ),
        omitted_sources=tuple(omitted_sources),
        warnings=(),
    )


def _projection(
    extraction: ProductionExtractionV1,
    *,
    decisions: dict[str, tuple[RelevanceClassification, RelevanceReasonCode]] | None = None,
) -> RelevanceProjectionV1:
    decisions = decisions or {}
    refs = extraction_evidence_refs_v1(extraction)
    payload_by_ref = dict(extraction_evidence_elements(extraction))
    classes = []
    for ref in refs:
        payload = payload_by_ref[ref]
        chosen = decisions.get(str(payload.get("value")))
        if chosen is None:
            chosen = (
                RelevanceClassification.DIRECT,
                RelevanceReasonCode.SUBJECT_MATCHED_PRIMARY,
            )
        classes.append(
            RelevanceClassificationItemV1(
                evidence_ref=ref,
                classification=chosen[0],
                reason_code=chosen[1],
                supporting_evidence_refs=(ref,),
                provenance=RelevanceDecisionProvenance.DETERMINISTIC_POLICY,
            )
        )
    return RelevanceProjectionV1(
        subject_id=extraction.subject_id,
        production_input_hash=extraction.production_input_hash,
        extraction_hash=production_extraction_hashes(extraction)[0],
        input_hash="d" * 64,
        extraction_evidence_refs=refs,
        classifications=tuple(classes),
    )


def _inputs(
    extraction: ProductionExtractionV1,
    projection: RelevanceProjectionV1,
    *,
    generation: int = 0,
    extraction_artifact: ProductionArtifact | None = None,
    projection_artifact: ProductionArtifact | None = None,
    projection_validation_error: RoutingReasonCode | None = None,
    diagnostics: tuple[RoutingCriticalSourceDiagnosticV1, ...] = (),
    now: datetime = NOW,
):
    extraction_artifact = extraction_artifact or ProductionArtifact(
        production_run_id=RUN_ID,
        subject_id=SUBJECT_ID,
        stage=ProductionArtifactStage.EXTRACTION,
        version=1,
        input_hash="e" * 64,
        status=ProductionArtifactStatus.VERIFIED,
    )
    projection_artifact = projection_artifact or ProductionArtifact(
        production_run_id=RUN_ID,
        subject_id=SUBJECT_ID,
        stage=ProductionArtifactStage.RELEVANCE_PROJECTION,
        version=1,
        input_hash="f" * 64,
        status=ProductionArtifactStatus.VERIFIED,
    )
    decision = decide_publication_routing(
        production_run_id=RUN_ID,
        subject_id=SUBJECT_ID,
        pipeline_generation=generation,
        extraction_input=ProductionRoutingArtifactInputV1(
            extraction_artifact,
            production_extraction_hashes(extraction)[0],
            extraction,
        ),
        relevance_input=ProductionRoutingArtifactInputV1(
            projection_artifact,
            projection.projection_hash,
            projection,
            projection_validation_error,
        ),
        legitimate_services=_SERVICES,
        decided_at=now,
        critical_source_diagnostics=diagnostics,
    )
    return decision


@pytest.mark.parametrize(
    ("extraction", "decisions", "expected_kind", "expected_reason"),
    [
        (_extraction(), {}, PublicationKind.BRIEF, RoutingReasonCode.NO_SUBJECT_IOC),
        (
            _extraction(facts=("a", "b", "c")),
            {},
            PublicationKind.BRIEF,
            RoutingReasonCode.NO_SUBJECT_IOC,
        ),
        (
            _extraction(indicators=(_indicator("CVE-2026-12345", ArtifactType.CVE),)),
            {},
            PublicationKind.BRIEF,
            RoutingReasonCode.NO_SUBJECT_IOC,
        ),
        (
            _extraction(
                indicators=(_indicator("https://source.example/report", ArtifactType.URL),)
            ),
            {
                "https://source.example/report": (
                    RelevanceClassification.CONTEXT,
                    RelevanceReasonCode.FOOTER_CONTACT,
                )
            },
            PublicationKind.BRIEF,
            RoutingReasonCode.NO_SUBJECT_IOC,
        ),
        (
            _extraction(indicators=(_indicator("c2.example"),)),
            {
                "c2.example": (
                    RelevanceClassification.DIRECT,
                    RelevanceReasonCode.MALICIOUS_SUBJECT_RELATION,
                )
            },
            PublicationKind.ARTICLE,
            RoutingReasonCode.SUBJECT_IOC_CONFIRMED,
        ),
        (
            _extraction(indicators=(_indicator("one.example"), _indicator("two.example"))),
            {
                "one.example": (
                    RelevanceClassification.DIRECT,
                    RelevanceReasonCode.MALICIOUS_SUBJECT_RELATION,
                ),
                "two.example": (
                    RelevanceClassification.DIRECT,
                    RelevanceReasonCode.MALICIOUS_SUBJECT_RELATION,
                ),
            },
            PublicationKind.ARTICLE,
            RoutingReasonCode.SUBJECT_IOC_CONFIRMED,
        ),
        (
            _extraction(
                indicators=(
                    _indicator("contextual.example", status=ExtractionIndicatorStatus.CONTEXTUAL),
                )
            ),
            {},
            PublicationKind.BRIEF,
            RoutingReasonCode.NO_SUBJECT_IOC,
        ),
        (
            _extraction(indicators=(_indicator("other-campaign.example"),)),
            {
                "other-campaign.example": (
                    RelevanceClassification.OUT_OF_SCOPE,
                    RelevanceReasonCode.INDICATOR_SECTION_OTHER_CASE,
                )
            },
            PublicationKind.BRIEF,
            RoutingReasonCode.NO_SUBJECT_IOC,
        ),
        (
            _extraction(
                indicators=(_indicator("secondary.example"),),
                tier=ProductionReferenceTier.SUPPORTING,
                editorial_role=ProductionEditorialRole.CORROBORATION,
            ),
            {
                "secondary.example": (
                    RelevanceClassification.CORROBORATION,
                    RelevanceReasonCode.MALICIOUS_SUBJECT_CORROBORATION,
                )
            },
            PublicationKind.ARTICLE,
            RoutingReasonCode.SUBJECT_IOC_CONFIRMED,
        ),
        (
            _extraction(indicators=(_indicator("original-only.example"),)),
            {
                "original-only.example": (
                    RelevanceClassification.INDETERMINATE,
                    RelevanceReasonCode.SUBJECT_LINK_NOT_DEMONSTRATED,
                )
            },
            PublicationKind.BRIEF,
            RoutingReasonCode.NO_SUBJECT_IOC,
        ),
    ],
)
def test_routing_article_brief_scenarios(extraction, decisions, expected_kind, expected_reason):
    decision = _inputs(extraction, _projection(extraction, decisions=decisions))
    assert decision.status is RoutingStatus.DECIDED
    assert decision.publication_kind is expected_kind
    assert decision.reason_code is expected_reason


def test_critical_source_omission_needs_review_with_explicit_diagnostic():
    omission = ProductionExtractionOmissionV1(
        canonical_url="https://critical.example/report",
        tier=ProductionReferenceTier.CORE,
        collection_state=CollectionState.ARCHIVED,
        reason=ProductionExtractionOmissionReason.REFERENCE_NOT_ELIGIBLE,
        error_code=None,
    )
    extraction = _extraction(omitted_sources=(omission,))
    decision = _inputs(extraction, _projection(extraction))
    assert decision.status is RoutingStatus.NEEDS_REVIEW
    assert decision.publication_kind is None
    assert decision.reason_code is RoutingReasonCode.CRITICAL_SOURCE_MISSING
    assert decision.critical_source_diagnostics[0].source_url == omission.canonical_url


def test_confirmed_subject_ioc_routes_article_despite_critical_source_omission():
    omission = ProductionExtractionOmissionV1(
        canonical_url="https://critical.example/report",
        tier=ProductionReferenceTier.CORE,
        collection_state=CollectionState.ARCHIVED,
        reason=ProductionExtractionOmissionReason.REFERENCE_NOT_ELIGIBLE,
        error_code=None,
    )
    extraction = _extraction(
        indicators=(_indicator("c2.example"),),
        omitted_sources=(omission,),
    )
    decision = _inputs(
        extraction,
        _projection(
            extraction,
            decisions={
                "c2.example": (
                    RelevanceClassification.DIRECT,
                    RelevanceReasonCode.MALICIOUS_SUBJECT_RELATION,
                )
            },
        ),
    )
    assert decision.status is RoutingStatus.DECIDED, (
        decision.reason_code,
        decision.confirmed_ioc_count,
        decision.critical_source_diagnostics,
    )
    assert decision.publication_kind is PublicationKind.ARTICLE
    assert decision.reason_code is RoutingReasonCode.SUBJECT_IOC_CONFIRMED
    assert decision.confirmed_ioc_count == 1
    assert len(decision.critical_source_diagnostics) == 1


def test_bridge_transcribed_core_source_keeps_ioc_contextual_and_needs_review():
    extraction = _extraction(
        indicators=(
            _indicator(
                "c2.example",
                status=ExtractionIndicatorStatus.UNVERIFIED_CONTEXTUAL,
                evidence_basis=ProductionEvidenceBasis.UNVERIFIED_MODEL_OUTPUT,
            ),
        )
    )
    bridge_source = replace(
        extraction.sources[0],
        acquisition_method=SourceAcquisitionMethod.BRIDGE_TRANSCRIPTION,
    )
    extraction = replace(extraction, sources=(bridge_source,))

    decision = _inputs(extraction, _projection(extraction))

    assert decision.status is RoutingStatus.NEEDS_REVIEW
    assert decision.publication_kind is None
    assert decision.reason_code is RoutingReasonCode.CRITICAL_SOURCE_MISSING
    assert decision.confirmed_ioc_count == 0
    assert decision.contextual_ioc_count == 1
    assert extraction.sources[0].indicators[0].evidence_basis is (
        ProductionEvidenceBasis.UNVERIFIED_MODEL_OUTPUT
    )


def test_bridge_core_source_blocks_article_promotion_from_another_confirmed_ioc():
    extraction = _extraction(indicators=(_indicator("first.example"),))
    bridge_source = replace(
        extraction.sources[0],
        acquisition_method=SourceAcquisitionMethod.BRIDGE_TRANSCRIPTION,
    )
    direct_source_id = uuid4()
    direct_source = _source(
        source_id=direct_source_id,
        indicators=(_indicator("second.example", source_id=direct_source_id),),
    )
    extraction = replace(extraction, sources=(bridge_source, direct_source))

    decision = _inputs(
        extraction,
        _projection(
            extraction,
            decisions={
                "first.example": (
                    RelevanceClassification.DIRECT,
                    RelevanceReasonCode.MALICIOUS_SUBJECT_RELATION,
                ),
                "second.example": (
                    RelevanceClassification.DIRECT,
                    RelevanceReasonCode.MALICIOUS_SUBJECT_RELATION,
                ),
            },
        ),
    )

    assert decision.status is RoutingStatus.NEEDS_REVIEW
    assert decision.reason_code is RoutingReasonCode.CRITICAL_SOURCE_MISSING
    assert decision.publication_kind is None
    assert decision.confirmed_ioc_count == 2


@pytest.mark.parametrize(
    "method",
    (
        SourceAcquisitionMethod.INDEPENDENT_CORROBORATION,
        SourceAcquisitionMethod.VISUAL_OCR,
        SourceAcquisitionMethod.THIRD_PARTY_EXTRACTION,
    ),
)
def test_derived_core_sources_force_review_even_with_confirmed_indicator(method):
    extraction = _extraction(indicators=(_indicator("derived.example"),))
    extraction = replace(
        extraction,
        sources=(replace(extraction.sources[0], acquisition_method=method),),
    )

    decision = _inputs(
        extraction,
        _projection(
            extraction,
            decisions={
                "derived.example": (
                    RelevanceClassification.DIRECT,
                    RelevanceReasonCode.MALICIOUS_SUBJECT_RELATION,
                )
            },
        ),
    )

    assert decision.status is RoutingStatus.NEEDS_REVIEW
    assert decision.reason_code is RoutingReasonCode.CRITICAL_SOURCE_MISSING
    assert decision.publication_kind is None
    assert decision.confirmed_ioc_count == 1


def test_unaccounted_source_extraction_failure_needs_review():
    omission = ProductionExtractionOmissionV1(
        canonical_url="https://secondary.example/report",
        tier=ProductionReferenceTier.SUPPORTING,
        collection_state=CollectionState.ARCHIVED,
        reason=ProductionExtractionOmissionReason.SOURCE_EXTRACTION_FAILED,
        error_code="source_failed",
    )
    extraction = _extraction(omitted_sources=(omission,))
    decision = _inputs(extraction, _projection(extraction))
    assert decision.status is RoutingStatus.NEEDS_REVIEW
    assert decision.reason_code is RoutingReasonCode.INCOMPLETE_EXTRACTION
    assert decision.publication_kind is None


def test_incomplete_projection_needs_review():
    extraction = _extraction(indicators=(_indicator("c2.example"),))
    projection = _projection(extraction)
    decision = _inputs(
        extraction,
        projection,
        projection_validation_error=RoutingReasonCode.INCOMPLETE_PROJECTION,
    )
    assert decision.status is RoutingStatus.NEEDS_REVIEW
    assert decision.reason_code is RoutingReasonCode.INCOMPLETE_PROJECTION
    assert decision.publication_kind is None


def test_application_recognizes_incomplete_projection_coverage():
    extraction = _extraction(indicators=(_indicator("c2.example"),))
    payload = relevance_projection_to_json(_projection(extraction))
    payload["classifications"].pop()
    assert _projection_has_incomplete_coverage(payload)


def test_projection_with_foreign_extraction_provenance_needs_review():
    extraction = _extraction(indicators=(_indicator("c2.example"),))
    foreign_extraction = _extraction(indicators=(_indicator("other.example"),))
    decision = _inputs(extraction, _projection(foreign_extraction))
    assert decision.status is RoutingStatus.NEEDS_REVIEW
    assert decision.reason_code is RoutingReasonCode.PROVENANCE_MISMATCH


def test_decision_hash_and_id_are_deterministic_across_replays():
    extraction = _extraction(indicators=(_indicator("c2.example"),))
    projection = _projection(
        extraction,
        decisions={
            "c2.example": (
                RelevanceClassification.DIRECT,
                RelevanceReasonCode.MALICIOUS_SUBJECT_RELATION,
            )
        },
    )
    extraction_artifact = ProductionArtifact(
        production_run_id=RUN_ID,
        subject_id=SUBJECT_ID,
        stage=ProductionArtifactStage.EXTRACTION,
        version=1,
        input_hash="e" * 64,
        status=ProductionArtifactStatus.VERIFIED,
    )
    projection_artifact = ProductionArtifact(
        production_run_id=RUN_ID,
        subject_id=SUBJECT_ID,
        stage=ProductionArtifactStage.RELEVANCE_PROJECTION,
        version=1,
        input_hash="f" * 64,
        status=ProductionArtifactStatus.VERIFIED,
    )
    first = _inputs(
        extraction,
        projection,
        extraction_artifact=extraction_artifact,
        projection_artifact=projection_artifact,
        now=NOW,
    )
    replay = _inputs(
        extraction,
        projection,
        extraction_artifact=extraction_artifact,
        projection_artifact=projection_artifact,
        now=NOW + timedelta(minutes=10),
    )
    assert replay.input_hash == first.input_hash
    assert replay.decision_hash == first.decision_hash
    assert replay.decision_id == first.decision_id
    assert replay.decided_at != first.decided_at


def test_requalification_brief_article_brief_keeps_distinct_history_inputs():
    brief_extraction = _extraction(facts=("one",))
    brief_one = _inputs(brief_extraction, _projection(brief_extraction))
    article_extraction = _extraction(indicators=(_indicator("c2.example"),))
    article_projection = _projection(
        article_extraction,
        decisions={
            "c2.example": (
                RelevanceClassification.DIRECT,
                RelevanceReasonCode.MALICIOUS_SUBJECT_RELATION,
            )
        },
    )
    article = _inputs(article_extraction, article_projection)
    brief_again_extraction = _extraction(facts=("two",))
    brief_two = _inputs(brief_again_extraction, _projection(brief_again_extraction))
    assert [brief_one.publication_kind, article.publication_kind, brief_two.publication_kind] == [
        PublicationKind.BRIEF,
        PublicationKind.ARTICLE,
        PublicationKind.BRIEF,
    ]
    assert len({brief_one.input_hash, article.input_hash, brief_two.input_hash}) == 3
    assert len({brief_one.decision_id, article.decision_id, brief_two.decision_id}) == 3


def test_subject_ioc_eligibility_shared_helper_rejects_non_subject_sources():
    extraction = _extraction(indicators=(_indicator("example.test"),))
    projection = _projection(
        extraction,
        decisions={
            "example.test": (
                RelevanceClassification.CONTEXT,
                RelevanceReasonCode.SUBJECT_LINK_NOT_DEMONSTRATED,
            )
        },
    )
    indicator = extraction.sources[0].indicators[0]
    ref = next(
        ref
        for ref, payload in extraction_evidence_elements(extraction)
        if ref.kind is EvidenceKind.INDICATOR and payload["value"] == indicator.value
    )
    assert not is_subject_ioc_eligible(
        indicator_status=indicator.indicator_status,
        artifact_type=indicator.artifact_type,
        value=indicator.value,
        relevance=projection.classification_for(ref),
        legitimate_services=_SERVICES,
    )


def test_secondary_critical_source_diagnostic_blocks_routing():
    omission = ProductionExtractionOmissionV1(
        canonical_url="https://secondary.example/report",
        tier=ProductionReferenceTier.SUPPORTING,
        collection_state=CollectionState.ARCHIVED,
        reason=ProductionExtractionOmissionReason.SOURCE_EXTRACTION_FAILED,
        error_code="source_failed",
    )
    extraction = _extraction(omitted_sources=(omission,))
    projection = _projection(extraction)
    diagnostic = RoutingCriticalSourceDiagnosticV1(
        source_id="secondary-id",
        source_url="https://secondary.example/report",
        failure_code="source_failed",
        tier=ProductionReferenceTier.SUPPORTING.value,
        criticality=ProductionSourceCriticality.CRITICAL,
        criticality_reason=ProductionSourceCriticalityReason.COVERAGE_UNKNOWN.value,
        policy_version=PRODUCTION_SOURCE_CRITICALITY_POLICY_VERSION,
    )
    decision = _inputs(extraction, projection, diagnostics=(diagnostic,))
    assert decision.status is RoutingStatus.NEEDS_REVIEW
    assert decision.reason_code is RoutingReasonCode.CRITICAL_SOURCE_MISSING


def test_diagnostic_order_does_not_change_canonical_input_or_decision_hash():
    omissions = tuple(
        ProductionExtractionOmissionV1(
            canonical_url=f"https://secondary-{name}.example/report",
            tier=ProductionReferenceTier.SUPPORTING,
            collection_state=CollectionState.ARCHIVED,
            reason=ProductionExtractionOmissionReason.SOURCE_EXTRACTION_FAILED,
            error_code=f"failed-{name}",
        )
        for name in ("a", "b")
    )
    extraction = _extraction(omitted_sources=omissions)
    projection = _projection(extraction)
    diagnostics = tuple(
        RoutingCriticalSourceDiagnosticV1(
            source_id=f"source-{name}",
            source_url=f"https://secondary-{name}.example/report",
            failure_code=f"failed-{name}",
            tier=ProductionReferenceTier.SUPPORTING.value,
            criticality=ProductionSourceCriticality.CRITICAL,
            criticality_reason=ProductionSourceCriticalityReason.COVERAGE_UNKNOWN.value,
            policy_version=PRODUCTION_SOURCE_CRITICALITY_POLICY_VERSION,
        )
        for name in ("a", "b")
    )
    extraction_artifact = ProductionArtifact(
        production_run_id=RUN_ID,
        subject_id=SUBJECT_ID,
        stage=ProductionArtifactStage.EXTRACTION,
        version=1,
        input_hash="e" * 64,
        status=ProductionArtifactStatus.VERIFIED,
    )
    projection_artifact = ProductionArtifact(
        production_run_id=RUN_ID,
        subject_id=SUBJECT_ID,
        stage=ProductionArtifactStage.RELEVANCE_PROJECTION,
        version=1,
        input_hash="f" * 64,
        status=ProductionArtifactStatus.VERIFIED,
    )
    first = _inputs(
        extraction,
        projection,
        extraction_artifact=extraction_artifact,
        projection_artifact=projection_artifact,
        diagnostics=diagnostics,
    )
    replay = _inputs(
        extraction,
        projection,
        extraction_artifact=extraction_artifact,
        projection_artifact=projection_artifact,
        diagnostics=tuple(reversed(diagnostics)),
    )
    assert replay.input_hash == first.input_hash
    assert replay.decision_hash == first.decision_hash
