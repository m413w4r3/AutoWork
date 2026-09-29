from __future__ import annotations

import hashlib
from datetime import date
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from cti_app.application.production_legacy_assembly import LegacyProductionQAService
from cti_app.application.production_parsers import (
    ParsedEvent,
    ParsedSource,
    ReferenceReport,
    TechnicalExtraction,
)
from cti_app.application.production_repairs import (
    EffectiveExtractionProjector,
    ProductionRepairIssueView,
    _impact_from_projection_hashes,
    _synthesis_evidence_hash,
    _synthesis_evidence_refs,
    classify_repair_impact,
)
from cti_app.application.production_synthesis import (
    ProductionSynthesisService,
    canonical_extraction_hash,
)
from cti_app.domain.classification import TLP
from cti_app.domain.discovery import SourceRole
from cti_app.domain.production import (
    ExtractionProfile,
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionArtifactStatus,
    ProductionDerivedOutput,
    ProductionEvidenceBasis,
    ProductionInputSnapshot,
    ProductionRepairAction,
    ProductionRepairDecision,
    ProductionRepairImpactKind,
    ProductionRepairIssueKind,
    RepairDecisionApplicationState,
    SupplementalSourceRepairState,
)
from cti_app.domain.production_extraction import (
    EXTRACTION_PROFILE_POLICY_VERSION,
    ExtractionEventV1,
    ExtractionIndicatorStatus,
    ExtractionIndicatorV1,
    ExtractionReuseState,
    ProductionExtractionV1,
    ProductionSourceExtractionV1,
    production_extraction_to_json,
)
from cti_app.domain.production_references import ProductionReferenceKind, ProductionReferenceTier
from cti_app.domain.production_synthesis import (
    PRODUCTION_SYNTHESIS_SCHEMA_VERSION,
    SYNTHESIS_POLICY_VERSION,
    ProductionSynthesisV1,
    SynthesisParagraphV1,
    extraction_evidence_refs_v1,
    production_synthesis_to_json,
)
from cti_app.domain.publication import ArtifactType

EDITION_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
SUBJECT_ID = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
RUN_ID = UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc")


def _issue(
    *,
    kind: ProductionRepairIssueKind = ProductionRepairIssueKind.REJECTED_INDICATOR,
    artifact_type: str = "domain",
    state: RepairDecisionApplicationState = RepairDecisionApplicationState.UNRESOLVED,
) -> ProductionRepairIssueView:
    return ProductionRepairIssueView(
        repair_key="a" * 64,
        kind=kind,
        artifact_type=artifact_type,
        source_id="S1",
        source_title="Source",
        is_publication_ioc=artifact_type in {"domain", "ip", "hash", "url", "email"},
        source_url="https://source.example/report",
        reason_code="rejected",
        value_sha256="b" * 64,
        preview="value",
        payload_available=True,
        production_run_id=RUN_ID,
        observed_artifact_id=uuid4(),
        observed_artifact_version=1,
        observed_pipeline_generation=1,
        application_state=state,
        subject_id=SUBJECT_ID,
    )


def _decision(
    issue: ProductionRepairIssueView, action: ProductionRepairAction
) -> ProductionRepairDecision:
    return ProductionRepairDecision(
        edition_id=EDITION_ID,
        subject_id=SUBJECT_ID,
        production_run_id=RUN_ID,
        observed_artifact_id=issue.observed_artifact_id or uuid4(),
        observed_pipeline_generation=1,
        repair_key=issue.repair_key,
        issue_kind=issue.kind,
        action=action,
        actor_id="analyst",
    )


@pytest.mark.parametrize(
    ("kind", "artifact_type", "action", "expected_impact", "model_call"),
    [
        (
            ProductionRepairIssueKind.REJECTED_RULE,
            "yara_rule",
            ProductionRepairAction.INCLUDE,
            ProductionRepairImpactKind.RULE_BUNDLE_ONLY,
            False,
        ),
        (
            ProductionRepairIssueKind.REJECTED_INDICATOR,
            "domain",
            ProductionRepairAction.INCLUDE,
            ProductionRepairImpactKind.PUBLICATION_ONLY,
            False,
        ),
        (
            ProductionRepairIssueKind.REJECTED_INDICATOR,
            "filename",
            ProductionRepairAction.INCLUDE,
            # A non-IOC value is projected without context: it renders in the
            # body from the existing synthesis, so it stays publication-only.
            ProductionRepairImpactKind.PUBLICATION_ONLY,
            False,
        ),
        (
            ProductionRepairIssueKind.REJECTED_INDICATOR,
            "domain",
            ProductionRepairAction.EXCLUDE,
            ProductionRepairImpactKind.NO_DELIVERABLE_CHANGE,
            False,
        ),
    ],
)
def test_lot33_minimum_matrix_plans_only_the_required_outputs(
    kind: ProductionRepairIssueKind,
    artifact_type: str,
    action: ProductionRepairAction,
    expected_impact: ProductionRepairImpactKind,
    model_call: bool,
) -> None:
    issue = _issue(kind=kind, artifact_type=artifact_type)

    impact = classify_repair_impact(issue, _decision(issue, action))

    assert impact.kind is expected_impact
    assert impact.model_call_required is model_call
    if expected_impact is ProductionRepairImpactKind.RULE_BUNDLE_ONLY:
        assert impact.affected_outputs == frozenset(
            {
                ProductionDerivedOutput.EXTRACTION,
                ProductionDerivedOutput.RULE_BUNDLE,
                ProductionDerivedOutput.CHECKPOINT,
            }
        )
    elif expected_impact is ProductionRepairImpactKind.PUBLICATION_ONLY:
        assert ProductionDerivedOutput.SYNTHESIS not in impact.affected_outputs


def test_lot33_include_then_exclude_projects_exactly_the_last_decision() -> None:
    value = "192.0.2.1"
    key = "a" * 64
    entry = {
        "repair_key": key,
        "kind": ProductionRepairIssueKind.REJECTED_INDICATOR.value,
        "artifact_type": "ip",
        "value_sha256": hashlib.sha256(value.encode()).hexdigest(),
        "source_id": "S1",
    }
    include = ProductionRepairDecision(
        edition_id=EDITION_ID,
        subject_id=SUBJECT_ID,
        production_run_id=RUN_ID,
        observed_artifact_id=uuid4(),
        observed_pipeline_generation=1,
        repair_key=key,
        issue_kind=ProductionRepairIssueKind.REJECTED_INDICATOR,
        action=ProductionRepairAction.INCLUDE,
        actor_id="analyst",
    )
    exclude = ProductionRepairDecision(
        edition_id=EDITION_ID,
        subject_id=SUBJECT_ID,
        production_run_id=RUN_ID,
        observed_artifact_id=uuid4(),
        observed_pipeline_generation=1,
        repair_key=key,
        issue_kind=ProductionRepairIssueKind.REJECTED_INDICATOR,
        action=ProductionRepairAction.EXCLUDE,
        actor_id="analyst",
    )
    base = TechnicalExtraction(items=())

    included = EffectiveExtractionProjector().project(
        base=base,
        repair_entries=[entry],
        effective_decisions=(include,),
        resolved_payloads={key: value},
    )
    final = EffectiveExtractionProjector().project(
        base=base,
        repair_entries=[entry],
        effective_decisions=(exclude,),
        resolved_payloads={},
    )

    assert len(included.extraction.items) == 1
    assert final.extraction == base
    assert final.excluded_repair_keys == (key,)


@pytest.mark.asyncio
async def test_lot33_qa_accepts_current_artifacts_with_independent_versions() -> None:
    source_url = "https://source.example/report"
    report = ReferenceReport(
        sources=(
            ParsedSource(
                local_id="S1",
                title="Source",
                url=source_url,
                canonical_url=source_url,
                publisher="Publisher",
                published_at=None,
                role=SourceRole.PRIMARY,
            ),
        ),
        events=(
            ParsedEvent(
                local_id="E1",
                event_date=date(2026, 1, 1),
                source_ids=("S1",),
                text="The event was reported.",
            ),
        ),
    )
    extraction = TechnicalExtraction(items=())

    def current(stage: ProductionArtifactStage, version: int) -> ProductionArtifact:
        return ProductionArtifact(
            production_run_id=RUN_ID,
            subject_id=SUBJECT_ID,
            stage=stage,
            version=version,
            input_hash=(f"{version:x}" * 64)[:64],
            status=ProductionArtifactStatus.VERIFIED,
        )

    result = await LegacyProductionQAService(lambda: None).run_qa(  # type: ignore[arg-type]
        run_id=RUN_ID,
        references_artifact=current(ProductionArtifactStage.REFERENCES, 3),
        extraction_artifact=current(ProductionArtifactStage.EXTRACTION, 7),
        synthesis_artifact=current(ProductionArtifactStage.SYNTHESIS, 4),
        publication_artifact=current(ProductionArtifactStage.PUBLICATION, 8),
        report=report,
        extraction=extraction,
        synthesis_text="Fait [S1]",
        publication_markdown="Fait",
        archived_urls={source_url},
        research_date=date(2026, 1, 2),
    )

    assert result["passed"] is True


def _snapshot() -> ProductionInputSnapshot:
    return ProductionInputSnapshot(
        production_run_id=RUN_ID,
        edition_id=EDITION_ID,
        subject_id=SUBJECT_ID,
        subject_version=1,
        subject_title="Subject",
        subject_tlp=TLP.CLEAR,
        selection_decision_id=uuid4(),
        origin_discovery_subject_id=uuid4(),
        canonical_discovery_subject_id=uuid4(),
        discovery_snapshot_id=uuid4(),
        discovery_snapshot_version=1,
        member_candidate_ids=(),
        discovery_summary="",
        actor_or_campaign="",
        period_start=date(2026, 7, 1),
        period_end=date(2026, 7, 31),
        publication_language="fr",
        research_date=date(2026, 7, 31),
    )


def _canonical_extraction(
    events: tuple[ExtractionEventV1, ...],
    *,
    indicators: tuple[ExtractionIndicatorV1, ...] = (),
) -> ProductionExtractionV1:
    return ProductionExtractionV1(
        schema_version=1,
        subject_id=SUBJECT_ID,
        production_input_hash="a" * 64,
        references_corpus_hash="b" * 64,
        profile_policy_version=EXTRACTION_PROFILE_POLICY_VERSION,
        sources=(
            ProductionSourceExtractionV1(
                source_document_id=UUID("11111111-1111-1111-1111-111111111111"),
                canonical_url="https://source.example/report",
                content_sha256="c" * 64,
                tier=ProductionReferenceTier.CORE,
                kind=ProductionReferenceKind.PUBLICATION,
                role=SourceRole.PRIMARY,
                profile=ExtractionProfile.FULL,
                checkpoint_id=UUID("22222222-2222-2222-2222-222222222222"),
                reuse_state=ExtractionReuseState.FRESH,
                facts=(),
                events=events,
                indicators=indicators,
                rules=(),
                uncertainties=(),
            ),
        ),
        omitted_sources=(),
        warnings=(),
    )


def _event(day: int, text: str) -> ExtractionEventV1:
    return ExtractionEventV1(
        event_date=date(2026, 7, day),
        date_text=None,
        text=text,
        context="",
        evidence_quote=text,
        evidence_basis=ProductionEvidenceBasis.SOURCE_VERIFIED,
        source_document_ids=(UUID("11111111-1111-1111-1111-111111111111"),),
    )


def test_canonical_evidence_classifies_narrative_and_publication_repairs_selectively() -> None:
    snapshot = _snapshot()
    previous = _canonical_extraction((_event(2, "Initial access"),))
    updated_event = _canonical_extraction((_event(2, "Changed initial access"),))
    publication_indicator = ExtractionIndicatorV1(
        value="evil.example",
        artifact_type=ArtifactType.DOMAIN,
        indicator_status=ExtractionIndicatorStatus.CONFIRMED_IOC,
        context="",
        evidence_quote="",
        evidence_basis=ProductionEvidenceBasis.ANALYST_OVERRIDE,
        source_document_ids=(UUID("11111111-1111-1111-1111-111111111111"),),
    )
    publication_extraction = _canonical_extraction(
        previous.sources[0].events,
        indicators=(publication_indicator,),
    )
    previous_hash = _synthesis_evidence_hash(_synthesis_evidence_refs(snapshot, previous))
    event_hash = _synthesis_evidence_hash(_synthesis_evidence_refs(snapshot, updated_event))
    publication_hash = _synthesis_evidence_hash(
        _synthesis_evidence_refs(snapshot, publication_extraction)
    )

    assert publication_hash == previous_hash
    narrative_impact = _impact_from_projection_hashes(
        TechnicalExtraction(items=()),
        TechnicalExtraction(items=()),
        previous_synthesis_projection_hash=previous_hash,
        new_synthesis_projection_hash=event_hash,
        previous_publication_projection_hash="d" * 64,
        new_publication_projection_hash="d" * 64,
        previous_rule_bundle_hash="e" * 64,
        new_rule_bundle_hash="e" * 64,
    )
    publication_impact = _impact_from_projection_hashes(
        TechnicalExtraction(items=()),
        TechnicalExtraction(items=()),
        previous_synthesis_projection_hash=previous_hash,
        new_synthesis_projection_hash=publication_hash,
        previous_publication_projection_hash="f" * 64,
        new_publication_projection_hash="0" * 64,
        previous_rule_bundle_hash="e" * 64,
        new_rule_bundle_hash="e" * 64,
    )

    assert narrative_impact.kind is ProductionRepairImpactKind.NARRATIVE
    assert ProductionDerivedOutput.SYNTHESIS in narrative_impact.affected_outputs
    assert publication_impact.kind is ProductionRepairImpactKind.PUBLICATION_ONLY
    assert ProductionDerivedOutput.SYNTHESIS not in publication_impact.affected_outputs

    source_impact = classify_repair_impact(
        SimpleNamespace(
            kind=ProductionRepairIssueKind.SUPPLEMENTAL_SOURCE_UNARCHIVED,
            repair_state=SupplementalSourceRepairState.ARCHIVED_PENDING_REFERENCES,
        ),
        None,
    )
    assert source_impact.kind is ProductionRepairImpactKind.SOURCE_CORPUS
    assert ProductionDerivedOutput.SYNTHESIS in source_impact.affected_outputs


class _RevisionStore:
    def __init__(self, blobs: dict[UUID, dict[str, object]]) -> None:
        self.blobs = blobs

    async def read_json(self, blob_id: UUID) -> dict[str, object]:
        return self.blobs[blob_id]


class _RevisionArtifacts:
    def __init__(self, artifacts: tuple[ProductionArtifact, ...]) -> None:
        self.artifacts = artifacts

    async def list_for_run(self, _run_id: UUID) -> tuple[ProductionArtifact, ...]:
        return self.artifacts


@pytest.mark.asyncio
async def test_next_canonical_revision_uses_current_evidence_delta() -> None:
    previous = _canonical_extraction((_event(2, "A"), _event(3, "B"), _event(4, "C")))
    current = _canonical_extraction((_event(2, "A"), _event(4, "C"), _event(5, "D")))
    previous_refs = extraction_evidence_refs_v1(previous)
    prior_synthesis = ProductionSynthesisV1(
        schema_version=PRODUCTION_SYNTHESIS_SCHEMA_VERSION,
        subject_id=SUBJECT_ID,
        production_input_hash="a" * 64,
        extraction_hash=canonical_extraction_hash(previous),
        publication_language="fr",
        synthesis_policy_version=SYNTHESIS_POLICY_VERSION,
        title="Subject",
        lead=(SynthesisParagraphV1(text="Previous summary", evidence_refs=previous_refs),),
        sections=(),
        timeline=(),
        uncertainties=(),
        warnings=(),
    )
    extraction_blob = uuid4()
    synthesis_blob = uuid4()
    extraction_artifact = ProductionArtifact(
        production_run_id=RUN_ID,
        subject_id=SUBJECT_ID,
        stage=ProductionArtifactStage.EXTRACTION,
        version=1,
        input_hash="b" * 64,
        canonical_blob_id=extraction_blob,
    )
    synthesis_artifact = ProductionArtifact(
        production_run_id=RUN_ID,
        subject_id=SUBJECT_ID,
        stage=ProductionArtifactStage.SYNTHESIS,
        version=1,
        input_hash="c" * 64,
        canonical_blob_id=synthesis_blob,
    )
    store = _RevisionStore(
        {
            extraction_blob: production_extraction_to_json(previous),
            synthesis_blob: production_synthesis_to_json(prior_synthesis),
        }
    )
    service = ProductionSynthesisService(
        uow_factory=lambda: None,  # type: ignore[arg-type]
        artifact_store=store,  # type: ignore[arg-type]
        model_gateway=object(),  # type: ignore[arg-type]
        synthesis_service=object(),  # type: ignore[arg-type]
    )
    revision = await service._decode_revision_candidate(
        SimpleNamespace(production_artifacts=_RevisionArtifacts((extraction_artifact,))),
        synthesis_artifact,
        current,
        "d" * 64,
    )

    assert revision is not None
    removed_ref = extraction_evidence_refs_v1(_canonical_extraction((_event(3, "B"),)))[0]
    added_ref = extraction_evidence_refs_v1(_canonical_extraction((_event(5, "D"),)))[0]
    assert revision.delta.removed_evidence == (removed_ref,)
    assert revision.delta.added_evidence == (added_ref,)
    assert len(revision.delta.unchanged_evidence) == 2
