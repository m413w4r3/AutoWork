from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from cti_app.application import production_routing as production_routing_app
from cti_app.application.production_routing import (
    _projection_has_incomplete_coverage,
    compute_brief_routing_feasibility,
)
from cti_app.domain.classification import TLP
from cti_app.domain.collection import CollectionState, SourceAcquisitionMethod
from cti_app.domain.discovery import SourceRole
from cti_app.domain.legitimate_services import LegitimateServiceRegistry
from cti_app.domain.production import (
    ExtractionProfile,
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionArtifactStatus,
    ProductionInputSnapshot,
    ProductionInputSource,
)
from cti_app.domain.production_extraction import (
    PRODUCTION_EXTRACTION_SCHEMA_VERSION,
    ExtractionEventV1,
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
    ROUTING_POLICY_VERSION,
    BriefRoutingFeasibilityV1,
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
    events=(),
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
        events=tuple(events),
        indicators=tuple(indicators),
        rules=(),
        uncertainties=(),
        editorial_role=editorial_role,
    )


def _extraction(
    *,
    indicators=(),
    facts=(),
    events=(),
    subject_id: UUID = SUBJECT_ID,
    tier=ProductionReferenceTier.CORE,
    editorial_role=ProductionEditorialRole.PRIMARY,
    omitted_sources=(),
    production_input_hash: str = "b" * 64,
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
    bound_events = tuple(replace(event, source_document_ids=(source_id,)) for event in events)
    return ProductionExtractionV1(
        schema_version=PRODUCTION_EXTRACTION_SCHEMA_VERSION,
        subject_id=subject_id,
        production_input_hash=production_input_hash,
        references_corpus_hash="c" * 64,
        profile_policy_version="test-routing-profile-v1",
        sources=(
            _source(
                source_id=source_id,
                indicators=bound_indicators,
                facts=bound_facts,
                events=bound_events,
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
    brief_feasibility: BriefRoutingFeasibilityV1 | None = None,
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
        brief_feasibility=brief_feasibility,
        critical_source_diagnostics=diagnostics,
    )
    return decision


def _dated_event(event_date: date = date(2026, 11, 1)) -> ExtractionEventV1:
    iso_date = event_date.isoformat()
    return ExtractionEventV1(
        event_date=event_date,
        date_text=iso_date,
        text="The source reported this event.",
        context="A source-verified dated event.",
        evidence_quote=f"The report published on {iso_date} describes this event.",
        evidence_basis=ProductionEvidenceBasis.SOURCE_VERIFIED,
        source_document_ids=(uuid4(),),
    )


def _snapshot_for_extraction(
    extraction: ProductionExtractionV1,
    *,
    published_at: date | None,
) -> ProductionInputSnapshot:
    member_id = uuid4()
    source = ProductionInputSource(
        discovery_candidate_id=member_id,
        source_candidate_id=uuid4(),
        canonical_url=extraction.sources[0].canonical_url,
        role=SourceRole.PRIMARY,
        title="Frozen source title",
        publisher="Frozen publisher",
        published_at=published_at,
        tlp=TLP.CLEAR,
        sensitivity="public",
        external_llm_allowed=True,
    )
    return ProductionInputSnapshot(
        production_run_id=RUN_ID,
        edition_id=uuid4(),
        subject_id=extraction.subject_id,
        subject_version=1,
        subject_title="Frozen subject title",
        subject_tlp=TLP.CLEAR,
        selection_decision_id=uuid4(),
        origin_discovery_subject_id=uuid4(),
        canonical_discovery_subject_id=uuid4(),
        discovery_snapshot_id=uuid4(),
        discovery_snapshot_version=1,
        member_candidate_ids=(member_id,),
        discovery_summary="Frozen discovery summary.",
        actor_or_campaign="Example actor",
        period_start=date(2026, 10, 1),
        period_end=date(2026, 11, 5),
        publication_language="fr",
        research_date=date(2026, 11, 5),
        core_sources=(source,),
    )


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


def test_no_subject_ioc_without_dated_brief_feasibility_needs_review():
    extraction = _extraction()
    projection = _projection(extraction)
    feasibility = compute_brief_routing_feasibility(
        extraction=extraction,
        projection=projection,
        snapshot=None,
    )

    decision = _inputs(
        extraction,
        projection,
        brief_feasibility=feasibility,
    )

    assert feasibility.dated_entry_count == 0
    assert feasibility.missing_source_urls == (extraction.sources[0].canonical_url,)
    assert feasibility.needs == ("dated source acquisition or an eligible publication date",)
    assert decision.status is RoutingStatus.NEEDS_REVIEW
    assert decision.publication_kind is None
    assert decision.reason_code is RoutingReasonCode.BRIEF_EVIDENCE_CHRONOLOGY_MISSING
    diagnostic = decision.critical_source_diagnostics[0]
    assert diagnostic.source_url == extraction.sources[0].canonical_url
    assert diagnostic.tier == ProductionReferenceTier.CORE.value
    assert diagnostic.editorial_role is ProductionEditorialRole.PRIMARY
    assert "source_scope=CORE/PRIMARY" in diagnostic.failure_code
    assert "dated_entry_count=0" in diagnostic.failure_code
    assert "dated source acquisition or an eligible publication date" in diagnostic.failure_code


def test_no_subject_ioc_with_source_verified_dated_event_is_a_brief():
    extraction = _extraction(events=(_dated_event(),))
    projection = _projection(extraction)
    feasibility = compute_brief_routing_feasibility(
        extraction=extraction,
        projection=projection,
        snapshot=None,
    )

    decision = _inputs(extraction, projection, brief_feasibility=feasibility)

    assert feasibility.dated_entry_count == 1
    assert decision.status is RoutingStatus.DECIDED
    assert decision.publication_kind is PublicationKind.BRIEF
    assert decision.reason_code is RoutingReasonCode.NO_SUBJECT_IOC


def test_no_subject_ioc_with_eligible_snapshot_publication_date_is_a_brief():
    extraction = _extraction()
    snapshot = _snapshot_for_extraction(extraction, published_at=date(2026, 10, 20))
    extraction = replace(extraction, production_input_hash=snapshot.input_hash)
    projection = _projection(extraction)
    feasibility = compute_brief_routing_feasibility(
        extraction=extraction,
        projection=projection,
        snapshot=snapshot,
    )

    decision = _inputs(extraction, projection, brief_feasibility=feasibility)

    assert feasibility.dated_entry_count == 1
    assert decision.status is RoutingStatus.DECIDED
    assert decision.publication_kind is PublicationKind.BRIEF
    assert decision.reason_code is RoutingReasonCode.NO_SUBJECT_IOC


@pytest.mark.asyncio
async def test_application_routing_loads_the_run_snapshot_for_brief_feasibility(monkeypatch):
    extraction = _extraction()
    snapshot = _snapshot_for_extraction(extraction, published_at=date(2026, 10, 20))
    extraction = replace(extraction, production_input_hash=snapshot.input_hash)
    projection = _projection(extraction)
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

    async def load_extraction_input(**_kwargs):
        return (
            ProductionRoutingArtifactInputV1(
                extraction_artifact,
                production_extraction_hashes(extraction)[0],
                extraction,
            ),
            (),
        )

    async def load_relevance_input(**_kwargs):
        return ProductionRoutingArtifactInputV1(
            projection_artifact,
            projection.projection_hash,
            projection,
        )

    monkeypatch.setattr(production_routing_app, "_load_extraction_input", load_extraction_input)
    monkeypatch.setattr(production_routing_app, "_load_relevance_input", load_relevance_input)
    monkeypatch.setattr(production_routing_app, "load_legitimate_services", lambda: _SERVICES)

    snapshot_reads: list[UUID] = []
    adopted = []

    async def get_run(run_id):
        assert run_id == RUN_ID
        return SimpleNamespace(id=RUN_ID, subject_id=SUBJECT_ID, pipeline_generation=0)

    async def get_snapshot(run_id):
        snapshot_reads.append(run_id)
        return snapshot

    async def append_and_adopt(decision, *, expected_version):
        assert expected_version == 4
        adopted.append(decision)
        return decision

    class FakeUow:
        production_runs = SimpleNamespace(get=get_run)
        production_input_snapshots = SimpleNamespace(get_by_run=get_snapshot)
        production_routing_decisions = SimpleNamespace(append_and_adopt=append_and_adopt)

        async def commit(self):
            return None

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

    decision = await production_routing_app.decide_and_adopt_production_routing(
        uow_factory=FakeUow,
        artifact_store=object(),
        production_run_id=RUN_ID,
        expected_version=4,
        decided_at=NOW,
    )

    assert snapshot_reads == [RUN_ID]
    assert adopted == [decision]
    assert decision.status is RoutingStatus.DECIDED
    assert decision.publication_kind is PublicationKind.BRIEF


def test_confirmed_subject_ioc_routes_to_article_when_brief_is_infeasible():
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
    feasibility = compute_brief_routing_feasibility(
        extraction=extraction,
        projection=projection,
        snapshot=None,
    )

    decision = _inputs(extraction, projection, brief_feasibility=feasibility)

    assert feasibility.dated_entry_count == 0
    assert decision.status is RoutingStatus.DECIDED
    assert decision.publication_kind is PublicationKind.ARTICLE
    assert decision.reason_code is RoutingReasonCode.SUBJECT_IOC_CONFIRMED


def test_adding_a_source_verified_date_changes_routing_hash_and_replays_stably():
    extraction_without_date = _extraction()
    projection_without_date = _projection(extraction_without_date)
    old_extraction_artifact = ProductionArtifact(
        production_run_id=RUN_ID,
        subject_id=SUBJECT_ID,
        stage=ProductionArtifactStage.EXTRACTION,
        version=1,
        input_hash="e" * 64,
        status=ProductionArtifactStatus.VERIFIED,
    )
    old_projection_artifact = ProductionArtifact(
        production_run_id=RUN_ID,
        subject_id=SUBJECT_ID,
        stage=ProductionArtifactStage.RELEVANCE_PROJECTION,
        version=1,
        input_hash="f" * 64,
        status=ProductionArtifactStatus.VERIFIED,
    )
    infeasible = compute_brief_routing_feasibility(
        extraction=extraction_without_date,
        projection=projection_without_date,
        snapshot=None,
    )
    first = _inputs(
        extraction_without_date,
        projection_without_date,
        extraction_artifact=old_extraction_artifact,
        projection_artifact=old_projection_artifact,
        brief_feasibility=infeasible,
    )

    source = extraction_without_date.sources[0]
    dated_source = replace(
        source,
        events=(
            replace(
                _dated_event(),
                source_document_ids=(source.source_document_id,),
            ),
        ),
    )
    extraction_with_date = replace(extraction_without_date, sources=(dated_source,))
    projection_with_date = _projection(extraction_with_date)
    new_extraction_artifact = ProductionArtifact(
        production_run_id=RUN_ID,
        subject_id=SUBJECT_ID,
        stage=ProductionArtifactStage.EXTRACTION,
        version=2,
        input_hash="a" * 64,
        status=ProductionArtifactStatus.VERIFIED,
    )
    new_projection_artifact = ProductionArtifact(
        production_run_id=RUN_ID,
        subject_id=SUBJECT_ID,
        stage=ProductionArtifactStage.RELEVANCE_PROJECTION,
        version=2,
        input_hash="b" * 64,
        status=ProductionArtifactStatus.VERIFIED,
    )
    feasible = compute_brief_routing_feasibility(
        extraction=extraction_with_date,
        projection=projection_with_date,
        snapshot=None,
    )
    recomputed = _inputs(
        extraction_with_date,
        projection_with_date,
        extraction_artifact=new_extraction_artifact,
        projection_artifact=new_projection_artifact,
        brief_feasibility=feasible,
    )
    replay = _inputs(
        extraction_with_date,
        projection_with_date,
        extraction_artifact=new_extraction_artifact,
        projection_artifact=new_projection_artifact,
        brief_feasibility=feasible,
    )

    assert first.reason_code is RoutingReasonCode.BRIEF_EVIDENCE_CHRONOLOGY_MISSING
    assert recomputed.publication_kind is PublicationKind.BRIEF
    assert recomputed.input_hash != first.input_hash
    assert recomputed.decision_hash != first.decision_hash
    assert recomputed.policy_version == "subject-publication-routing-v4-brief-feasibility-v1"
    assert replay.input_hash == recomputed.input_hash
    assert replay.decision_hash == recomputed.decision_hash
    assert replay.decision_id == recomputed.decision_id


def test_core_archived_direct_source_does_not_require_review():
    extraction = _extraction()
    direct_source = replace(
        extraction.sources[0],
        acquisition_method=SourceAcquisitionMethod.DIRECT_HTTP,
    )
    extraction = replace(extraction, sources=(direct_source,))

    decision = _inputs(extraction, _projection(extraction))

    assert decision.status is RoutingStatus.DECIDED
    assert decision.publication_kind is PublicationKind.BRIEF
    assert decision.reason_code is RoutingReasonCode.NO_SUBJECT_IOC
    assert decision.critical_source_diagnostics == ()


@pytest.mark.parametrize(
    "method",
    (
        SourceAcquisitionMethod.INDEPENDENT_CORROBORATION,
        SourceAcquisitionMethod.BRIDGE_TRANSCRIPTION,
        SourceAcquisitionMethod.VISUAL_OCR,
        SourceAcquisitionMethod.THIRD_PARTY_EXTRACTION,
    ),
)
@pytest.mark.parametrize("confirmed_ioc", (False, True))
def test_alternative_core_provenance_has_distinct_reason_and_diagnostic(method, confirmed_ioc):
    indicators = (_indicator("derived.example"),) if confirmed_ioc else ()
    extraction = _extraction(indicators=indicators)
    source = replace(extraction.sources[0], acquisition_method=method)
    extraction = replace(extraction, sources=(source,))
    decisions = (
        {
            "derived.example": (
                RelevanceClassification.DIRECT,
                RelevanceReasonCode.MALICIOUS_SUBJECT_RELATION,
            )
        }
        if confirmed_ioc
        else None
    )

    decision = _inputs(extraction, _projection(extraction, decisions=decisions))

    assert decision.status is RoutingStatus.NEEDS_REVIEW
    assert decision.publication_kind is None
    assert decision.reason_code is RoutingReasonCode.CRITICAL_SOURCE_ALTERNATIVE_PROVENANCE
    assert decision.confirmed_ioc_count == int(confirmed_ioc)
    diagnostic = decision.critical_source_diagnostics[0]
    assert diagnostic.source_id == str(source.source_document_id)
    assert diagnostic.source_url == source.canonical_url
    assert diagnostic.tier == ProductionReferenceTier.CORE.value
    assert diagnostic.criticality is ProductionSourceCriticality.CRITICAL
    assert diagnostic.criticality_reason == ProductionSourceCriticalityReason.CORE_TIER.value
    assert diagnostic.editorial_role is source.editorial_role
    assert diagnostic.acquisition_method is method
    assert diagnostic.omission_collection_state is None
    assert diagnostic.omission_error_code is None
    assert diagnostic.to_json()["collection_state"] is None
    assert diagnostic.to_json()["error_code"] is None
    assert diagnostic.to_json()["remaining_control_code"] == "human_review_required"
    assert diagnostic.to_json()["human_waiver_path"] == "not_consumed_by_routing"
    assert decision.policy_version == "subject-publication-routing-v4-brief-feasibility-v1"


def test_critical_source_omission_needs_review_with_explicit_diagnostic():
    omission = ProductionExtractionOmissionV1(
        canonical_url="https://critical.example/report",
        tier=ProductionReferenceTier.CORE,
        collection_state=CollectionState.BLOCKED,
        reason=ProductionExtractionOmissionReason.REFERENCE_NOT_ELIGIBLE,
        error_code=None,
    )
    extraction = _extraction(omitted_sources=(omission,))
    decision = _inputs(extraction, _projection(extraction))
    assert decision.status is RoutingStatus.NEEDS_REVIEW
    assert decision.publication_kind is None
    assert decision.reason_code is RoutingReasonCode.CRITICAL_SOURCE_INACCESSIBLE
    diagnostic = decision.critical_source_diagnostics[0]
    assert diagnostic.source_url == omission.canonical_url
    assert diagnostic.omission_collection_state is CollectionState.BLOCKED
    assert diagnostic.omission_error_code is None
    assert diagnostic.to_json()["collection_state"] == CollectionState.BLOCKED.value
    assert diagnostic.to_json()["error_code"] is None
    assert diagnostic.to_json()["remaining_control_code"] == "human_review_required"
    assert diagnostic.to_json()["human_waiver_path"] == "not_consumed_by_routing"


def test_supporting_critical_missing_or_unreadable_has_specific_reason_and_omission_details():
    cases = (
        (
            ProductionExtractionOmissionReason.REFERENCE_NOT_ELIGIBLE,
            CollectionState.BLOCKED,
            None,
            RoutingReasonCode.CRITICAL_SOURCE_INACCESSIBLE,
        ),
        (
            ProductionExtractionOmissionReason.SOURCE_EXTRACTION_FAILED,
            CollectionState.ARCHIVED,
            "extraction_source_text_unreadable",
            RoutingReasonCode.CRITICAL_SOURCE_UNREADABLE,
        ),
    )
    for index, (omission_reason, state, error_code, expected_reason) in enumerate(cases):
        url = f"https://supporting-critical-{index}.example/report"
        omission = ProductionExtractionOmissionV1(
            canonical_url=url,
            tier=ProductionReferenceTier.SUPPORTING,
            collection_state=state,
            reason=omission_reason,
            error_code=error_code,
        )
        extraction = _extraction(omitted_sources=(omission,))
        diagnostic = RoutingCriticalSourceDiagnosticV1(
            source_id=f"supporting-{index}",
            source_url=url,
            failure_code=error_code or omission_reason.value,
            tier=ProductionReferenceTier.SUPPORTING.value,
            criticality=ProductionSourceCriticality.CRITICAL,
            criticality_reason=ProductionSourceCriticalityReason.COVERAGE_UNKNOWN.value,
            policy_version=PRODUCTION_SOURCE_CRITICALITY_POLICY_VERSION,
        )

        decision = _inputs(
            extraction,
            _projection(extraction),
            diagnostics=(diagnostic,),
        )

        assert decision.status is RoutingStatus.NEEDS_REVIEW
        assert decision.reason_code is expected_reason
        persisted = next(
            item for item in decision.critical_source_diagnostics if item.source_url == url
        )
        assert persisted.omission_collection_state is state
        assert persisted.omission_error_code == error_code
        assert persisted.to_json()["collection_state"] == state.value
        assert persisted.to_json()["error_code"] == error_code


def test_supporting_secondary_missing_source_does_not_block_routing():
    omission = ProductionExtractionOmissionV1(
        canonical_url="https://secondary.example/missing",
        tier=ProductionReferenceTier.SUPPORTING,
        collection_state=CollectionState.BLOCKED,
        reason=ProductionExtractionOmissionReason.REFERENCE_NOT_ELIGIBLE,
        error_code=None,
    )
    extraction = _extraction(omitted_sources=(omission,))
    diagnostic = RoutingCriticalSourceDiagnosticV1(
        source_id="secondary-id",
        source_url=omission.canonical_url,
        failure_code=omission.reason.value,
        tier=ProductionReferenceTier.SUPPORTING.value,
        criticality=ProductionSourceCriticality.SECONDARY,
        criticality_reason=ProductionSourceCriticalityReason.REDUNDANT_CORROBORATION.value,
        policy_version=PRODUCTION_SOURCE_CRITICALITY_POLICY_VERSION,
    )

    decision = _inputs(extraction, _projection(extraction), diagnostics=(diagnostic,))

    assert decision.status is RoutingStatus.DECIDED
    assert decision.publication_kind is PublicationKind.BRIEF
    assert decision.reason_code is RoutingReasonCode.NO_SUBJECT_IOC
    persisted = decision.critical_source_diagnostics[0]
    assert persisted.to_json()["remaining_control_code"] == "no_routing_block"


def test_historical_critical_source_missing_reason_still_deserializes():
    assert RoutingReasonCode("critical_source_missing") is RoutingReasonCode.CRITICAL_SOURCE_MISSING
    assert ROUTING_POLICY_VERSION == "subject-publication-routing-v4-brief-feasibility-v1"


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
    assert decision.reason_code is RoutingReasonCode.CRITICAL_SOURCE_ALTERNATIVE_PROVENANCE
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
    assert decision.reason_code is RoutingReasonCode.CRITICAL_SOURCE_ALTERNATIVE_PROVENANCE
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
    assert decision.reason_code is RoutingReasonCode.CRITICAL_SOURCE_ALTERNATIVE_PROVENANCE
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


def test_decision_hash_is_deterministic_across_source_order():
    extraction = _extraction(facts=("first",))
    other_source = _extraction(facts=("second",)).sources[0]
    extraction = replace(extraction, sources=(*extraction.sources, other_source))
    reordered = replace(extraction, sources=tuple(reversed(extraction.sources)))
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
        _projection(extraction),
        extraction_artifact=extraction_artifact,
        projection_artifact=projection_artifact,
    )
    replay = _inputs(
        reordered,
        _projection(reordered),
        extraction_artifact=extraction_artifact,
        projection_artifact=projection_artifact,
    )

    assert replay.input_hash == first.input_hash
    assert replay.decision_hash == first.decision_hash
    assert replay.decision_id == first.decision_id


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
    assert decision.reason_code is RoutingReasonCode.CRITICAL_SOURCE_UNREADABLE


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
