from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import date
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from cti_app.api.production import router as production_router
from cti_app.application.production_references import production_reference_corpus_to_json
from cti_app.application.production_repairs import (
    ProductionRepairAdjudicationService,
    ProductionRepairAuditReasonRequiredError,
    ProductionRepairDecisionService,
    ProductionRepairIssueService,
    ProductionRepairSourceDiagnosticRequiredError,
    SourceExtractionFailureIssue,
    repair_issue_execution_state,
    repair_key_for_source_extraction_failure,
    repair_key_for_supplemental_source,
)
from cti_app.application.production_workflow import _source_loss_diagnostics
from cti_app.domain.collection import CollectionState
from cti_app.domain.discovery import SourceRole
from cti_app.domain.production import (
    ExtractionProfile,
    ProductionArtifactStage,
    ProductionArtifactStatus,
    ProductionRepairAction,
    ProductionRepairDecision,
    ProductionRepairIssueKind,
)
from cti_app.domain.production_extraction import (
    EXTRACTION_PROFILE_POLICY_VERSION,
    ExtractionReuseState,
    ProductionExtractionOmissionReason,
    ProductionExtractionOmissionV1,
    ProductionExtractionV1,
    ProductionSourceExtractionV1,
    production_extraction_to_json,
)
from cti_app.domain.production_references import (
    ProductionEditorialRole,
    ProductionReferenceCorpusV1,
    ProductionReferenceKind,
    ProductionReferenceResearchStatus,
    ProductionReferenceSourceV1,
    ProductionReferenceTier,
    ProductionSourceCriticality,
    ProductionSourceCriticalityReason,
    production_source_criticality,
)
from cti_app.infrastructure.database.repositories.production import (
    SqlAlchemyProductionRepairDecisionRepository,
    _production_repair_decision_from_row,
)


def _reference(
    *,
    url: str,
    title: str,
    tier: ProductionReferenceTier,
    role: SourceRole,
    editorial_role: ProductionEditorialRole,
    eligible: bool = True,
    content_sha256: str | None = None,
) -> ProductionReferenceSourceV1:
    document_id = uuid4() if eligible else None
    return ProductionReferenceSourceV1(
        canonical_url=url,
        tier=tier,
        kind=ProductionReferenceKind.PUBLICATION,
        role=role,
        editorial_role=editorial_role,
        title=title,
        publisher="Publisher",
        published_at=None,
        source_collection_id=document_id,
        source_document_id=document_id,
        discovery_candidate_ids=(),
        collection_state=(
            CollectionState.ARCHIVED if eligible else CollectionState.FAILED_RETRYABLE
        ),
        content_sha256=(
            content_sha256 or hashlib.sha256(url.encode("utf-8")).hexdigest() if eligible else None
        ),
        relevance_reason=(
            "Supports the subject" if tier is not ProductionReferenceTier.CORE else None
        ),
        proposed_by_model=tier is not ProductionReferenceTier.CORE,
        eligible_for_extraction=eligible,
    )


def test_criticality_requires_explicit_redundancy_evidence() -> None:
    core = _reference(
        url="https://example.test/core",
        title="Core",
        tier=ProductionReferenceTier.CORE,
        role=SourceRole.PRIMARY,
        editorial_role=ProductionEditorialRole.PRIMARY,
    )
    corroboration = _reference(
        url="https://example.test/corroboration",
        title="Corroboration",
        tier=ProductionReferenceTier.SUPPORTING,
        role=SourceRole.INDEPENDENT,
        editorial_role=ProductionEditorialRole.CORROBORATION,
        eligible=False,
    )
    counter_analysis = _reference(
        url="https://example.test/counter",
        title="Counter-analysis",
        tier=ProductionReferenceTier.SUPPORTING,
        role=SourceRole.INDEPENDENT,
        editorial_role=ProductionEditorialRole.COUNTER_ANALYSIS,
        eligible=False,
    )
    unknown_coverage = _reference(
        url="https://example.test/context",
        title="Context",
        tier=ProductionReferenceTier.SUPPORTING,
        role=SourceRole.UNKNOWN,
        editorial_role=ProductionEditorialRole.CONTEXT,
        eligible=False,
    )

    assert production_source_criticality(core, sources=(core, corroboration))[0] is (
        ProductionSourceCriticality.CRITICAL
    )
    assert production_source_criticality(corroboration, sources=(core, corroboration)) == (
        ProductionSourceCriticality.SECONDARY,
        ProductionSourceCriticalityReason.REDUNDANT_CORROBORATION,
    )
    assert production_source_criticality(counter_analysis, sources=(core, counter_analysis)) == (
        ProductionSourceCriticality.CRITICAL,
        ProductionSourceCriticalityReason.COUNTER_ANALYSIS,
    )
    assert production_source_criticality(unknown_coverage, sources=(core, unknown_coverage)) == (
        ProductionSourceCriticality.CRITICAL,
        ProductionSourceCriticalityReason.COVERAGE_UNKNOWN,
    )
    assert production_source_criticality(corroboration, sources=(corroboration,))[0] is (
        ProductionSourceCriticality.CRITICAL
    )
    duplicate = _reference(
        url="https://example.test/duplicate",
        title="Duplicate content",
        tier=ProductionReferenceTier.SUPPORTING,
        role=SourceRole.INDEPENDENT,
        editorial_role=ProductionEditorialRole.CONTEXT,
        content_sha256="d" * 64,
    )
    same_content = _reference(
        url="https://example.test/same-content",
        title="Same content",
        tier=ProductionReferenceTier.SUPPORTING,
        role=SourceRole.INDEPENDENT,
        editorial_role=ProductionEditorialRole.CONTEXT,
        content_sha256="d" * 64,
    )
    assert production_source_criticality(duplicate, sources=(duplicate, same_content)) == (
        ProductionSourceCriticality.SECONDARY,
        ProductionSourceCriticalityReason.CONTENT_DUPLICATE,
    )


def test_source_failure_decision_is_allowed_by_the_database_contract() -> None:
    from cti_app.infrastructure.database.models.production import ProductionRepairDecisionRow

    constraints = {
        constraint.name: str(constraint.sqltext)
        for constraint in ProductionRepairDecisionRow.__table__.constraints
        if constraint.name
        in {
            "ck_production_repair_issue_kind",
            "ck_production_repair_action_compatibility",
            "ck_production_repair_source_criticality",
            "ck_production_repair_criticality_reason",
            "ck_production_repair_source_criticality_pair",
            "ck_production_repair_criticality_policy_version",
            "ck_production_repair_source_extraction_diagnostic",
        }
    }

    assert "source_extraction_failed" in constraints["ck_production_repair_issue_kind"]
    assert "source_extraction_failed" in constraints["ck_production_repair_action_compatibility"]
    assert "continue_without_source" in constraints["ck_production_repair_action_compatibility"]
    assert "critical" in constraints["ck_production_repair_source_criticality"]
    assert "counter_analysis" in constraints["ck_production_repair_criticality_reason"]
    assert (
        "criticality_reason IS NULL" in constraints["ck_production_repair_source_criticality_pair"]
    )
    assert (
        "criticality_policy_version IS NOT NULL"
        in constraints["ck_production_repair_criticality_policy_version"]
    )
    assert (
        "failure_code IS NOT NULL"
        in constraints["ck_production_repair_source_extraction_diagnostic"]
    )


def test_failed_primary_is_not_counted_as_coverage_for_failed_corroboration() -> None:
    primary = SimpleNamespace(
        source_document_id=uuid4(),
        canonical_url="https://example.test/primary",
        title="Primary",
        tier=ProductionReferenceTier.SUPPORTING,
        role=SourceRole.PRIMARY,
        editorial_role=ProductionEditorialRole.PRIMARY,
        content_sha256=None,
    )
    corroboration = SimpleNamespace(
        source_document_id=uuid4(),
        canonical_url="https://example.test/corroboration",
        title="Corroboration",
        tier=ProductionReferenceTier.SUPPORTING,
        role=SourceRole.INDEPENDENT,
        editorial_role=ProductionEditorialRole.CORROBORATION,
        content_sha256=None,
    )
    context = _reference(
        url="https://example.test/context",
        title="Context",
        tier=ProductionReferenceTier.SUPPORTING,
        role=SourceRole.UNKNOWN,
        editorial_role=ProductionEditorialRole.CONTEXT,
    )
    subject_id = uuid4()
    plan = SimpleNamespace(sources=(primary, corroboration, context))
    successful_context = _source_extraction(context, subject_id=subject_id)
    extraction = ProductionExtractionV1(
        schema_version=1,
        subject_id=subject_id,
        production_input_hash="b" * 64,
        references_corpus_hash="c" * 64,
        profile_policy_version=EXTRACTION_PROFILE_POLICY_VERSION,
        sources=successful_context.sources,
        omitted_sources=tuple(
            ProductionExtractionOmissionV1(
                canonical_url=source.canonical_url,
                tier=source.tier,
                collection_state=CollectionState.ARCHIVED,
                reason=ProductionExtractionOmissionReason.SOURCE_EXTRACTION_FAILED,
                error_code="extraction_source_output_invalid",
            )
            for source in (primary, corroboration)
        ),
        warnings=(),
    )

    diagnostics = _source_loss_diagnostics(plan, extraction)
    corroboration_diagnostic = next(
        item for item in diagnostics if item["source_url"] == corroboration.canonical_url
    )

    assert corroboration_diagnostic["source_criticality"] == "critical"
    assert corroboration_diagnostic["criticality_reason"] == "corroboration_uncovered"


def test_source_extraction_domain_requires_a_full_analyst_diagnostic() -> None:
    with pytest.raises(ValueError, match="analyst reason and diagnostic"):
        ProductionRepairDecision(
            edition_id=uuid4(),
            subject_id=uuid4(),
            production_run_id=uuid4(),
            observed_artifact_id=uuid4(),
            observed_pipeline_generation=0,
            repair_key="a" * 64,
            issue_kind=ProductionRepairIssueKind.SOURCE_EXTRACTION_FAILED,
            action=ProductionRepairAction.CONTINUE_WITHOUT_SOURCE,
            actor_id="analyst",
            reason="Reviewed the loss.",
            source_criticality=ProductionSourceCriticality.CRITICAL,
            criticality_reason=ProductionSourceCriticalityReason.COUNTER_ANALYSIS,
            criticality_policy_version="source-criticality-v1",
        )


def test_extraction_loss_decision_identity_tracks_artifact_failure_and_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    edition_id = uuid4()
    subject_id = uuid4()
    artifact_id = uuid4()

    def key(
        observed_artifact_id: UUID,
        failure_code: str,
        references_artifact_id: UUID | None = None,
    ) -> str:
        return repair_key_for_source_extraction_failure(
            edition_id=edition_id,
            subject_id=subject_id,
            source_url="https://example.test/report",
            observed_artifact_id=observed_artifact_id,
            observed_references_artifact_id=references_artifact_id,
            failure_code=failure_code,
        )

    first = key(artifact_id, "extraction_source_output_invalid")
    assert first == key(artifact_id, "extraction_source_output_invalid")
    assert first != repair_key_for_source_extraction_failure(
        edition_id=edition_id,
        subject_id=subject_id,
        source_url="https://example.test/report",
        observed_artifact_id=uuid4(),
        observed_references_artifact_id=None,
        failure_code="extraction_source_output_invalid",
    )
    assert first != key(artifact_id, "different_failure")
    assert first != key(artifact_id, "extraction_source_output_invalid", uuid4())
    monkeypatch.setattr(
        "cti_app.application.production_repairs.PRODUCTION_SOURCE_CRITICALITY_POLICY_VERSION",
        "source-criticality-v2",
    )
    assert first != key(artifact_id, "extraction_source_output_invalid")


def _source_extraction(
    source: ProductionReferenceSourceV1, *, subject_id: UUID | None = None
) -> ProductionExtractionV1:
    assert source.source_document_id is not None
    return ProductionExtractionV1(
        schema_version=1,
        subject_id=subject_id or uuid4(),
        production_input_hash="b" * 64,
        references_corpus_hash="c" * 64,
        profile_policy_version=EXTRACTION_PROFILE_POLICY_VERSION,
        sources=(
            ProductionSourceExtractionV1(
                source_document_id=source.source_document_id,
                canonical_url=source.canonical_url,
                content_sha256=source.content_sha256 or "a" * 64,
                tier=source.tier,
                kind=source.kind,
                role=source.role,
                profile=(
                    ExtractionProfile.FULL
                    if source.tier is ProductionReferenceTier.CORE
                    else ExtractionProfile.IOC_RULES
                ),
                checkpoint_id=uuid4(),
                reuse_state=ExtractionReuseState.FRESH,
                facts=(),
                events=(),
                indicators=(),
                rules=(),
                uncertainties=(),
            ),
        ),
        omitted_sources=(
            ProductionExtractionOmissionV1(
                canonical_url="https://example.test/ncsc",
                tier=ProductionReferenceTier.SUPPORTING,
                collection_state=CollectionState.ARCHIVED,
                reason=ProductionExtractionOmissionReason.SOURCE_EXTRACTION_FAILED,
                error_code="q2_no_payload",
            ),
        ),
        warnings=(
            "extraction_source_skipped:https://example.test/ncsc:q2_no_payload",
            "q2_parse_error:q2_no_payload",
        ),
    )


class _ArtifactStore:
    def __init__(self, payloads: dict[object, dict[str, object]]) -> None:
        self.payloads = payloads

    async def read_json(self, blob_id: object) -> dict[str, object]:
        return self.payloads[blob_id]


class _ArtifactRepository:
    def __init__(self, artifacts: tuple[object, ...]) -> None:
        self.artifacts = {artifact.stage.value: artifact for artifact in artifacts}

    async def get_current(self, _run_id: object, stage: str) -> object | None:
        return self.artifacts.get(stage)


class _IssueUow:
    def __init__(self, run: object, artifacts: tuple[object, ...]) -> None:
        self.production_runs = SimpleNamespace(
            list_for_edition=lambda _edition_id: _async_value([run])
        )
        self.production_artifacts = _ArtifactRepository(artifacts)
        self.production_repair_decisions = SimpleNamespace(
            effective_decisions=lambda _edition_id, _subject_id=None: _async_value(())
        )

    async def __aenter__(self) -> _IssueUow:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None


async def _async_value(value: object) -> object:
    return value


@pytest.mark.asyncio
async def test_q2_no_payload_is_a_durable_critical_source_loss_issue() -> None:
    subject_id = uuid4()
    core = _reference(
        url="https://example.test/core",
        title="Core report",
        tier=ProductionReferenceTier.CORE,
        role=SourceRole.PRIMARY,
        editorial_role=ProductionEditorialRole.PRIMARY,
    )
    ncsc = _reference(
        url="https://example.test/ncsc",
        title="NCSC counter-analysis",
        tier=ProductionReferenceTier.SUPPORTING,
        role=SourceRole.INDEPENDENT,
        editorial_role=ProductionEditorialRole.COUNTER_ANALYSIS,
    )
    corpus = ProductionReferenceCorpusV1(
        schema_version=1,
        subject_id=subject_id,
        research_date=date(2026, 10, 8),
        production_input_hash="b" * 64,
        research_status=ProductionReferenceResearchStatus.COMPLETED,
        sources=(core, ncsc),
        warnings=(),
    )
    extraction = _source_extraction(core)
    run_id = uuid4()
    references_blob = uuid4()
    extraction_blob = uuid4()
    references_artifact = SimpleNamespace(
        id=uuid4(),
        production_run_id=run_id,
        stage=ProductionArtifactStage.REFERENCES,
        version=1,
        status=ProductionArtifactStatus.VERIFIED,
        canonical_blob_id=references_blob,
    )
    extraction_artifact = SimpleNamespace(
        id=uuid4(),
        production_run_id=run_id,
        stage=ProductionArtifactStage.EXTRACTION,
        version=1,
        status=ProductionArtifactStatus.VERIFIED,
        canonical_blob_id=extraction_blob,
    )
    run = SimpleNamespace(id=run_id, subject_id=subject_id, pipeline_generation=3)
    uow = _IssueUow(run, (references_artifact, extraction_artifact))
    store = _ArtifactStore(
        {
            references_blob: production_reference_corpus_to_json(corpus),
            extraction_blob: production_extraction_to_json(extraction),
        }
    )

    issues = await ProductionRepairIssueService(
        lambda: uow, store
    ).list_source_extraction_failure_issues(uuid4(), subject_id)

    assert len(issues) == 1
    issue = issues[0]
    assert issue.failure_code == "q2_no_payload"
    assert "q2_parse_error:q2_no_payload" in issue.extraction_warnings
    assert issue.source_title == "NCSC counter-analysis"
    assert issue.source_criticality is ProductionSourceCriticality.CRITICAL
    assert issue.criticality_reason is ProductionSourceCriticalityReason.COUNTER_ANALYSIS
    assert repair_issue_execution_state(issue).resolved is False
    assert repair_issue_execution_state(issue).blocking is True


@pytest.mark.asyncio
async def test_unextracted_primary_does_not_make_a_failed_corroboration_secondary() -> None:
    subject_id = uuid4()
    primary = _reference(
        url="https://example.test/primary",
        title="Primary report",
        tier=ProductionReferenceTier.SUPPORTING,
        role=SourceRole.PRIMARY,
        editorial_role=ProductionEditorialRole.PRIMARY,
    )
    corroboration = _reference(
        url="https://example.test/reuters",
        title="Reuters corroboration",
        tier=ProductionReferenceTier.SUPPORTING,
        role=SourceRole.INDEPENDENT,
        editorial_role=ProductionEditorialRole.CORROBORATION,
    )
    context = _reference(
        url="https://example.test/context",
        title="Context source",
        tier=ProductionReferenceTier.SUPPORTING,
        role=SourceRole.UNKNOWN,
        editorial_role=ProductionEditorialRole.CONTEXT,
    )
    corpus = ProductionReferenceCorpusV1(
        schema_version=1,
        subject_id=subject_id,
        research_date=date(2026, 10, 8),
        production_input_hash="b" * 64,
        research_status=ProductionReferenceResearchStatus.COMPLETED,
        sources=(primary, corroboration, context),
        warnings=(),
    )
    successful_context = _source_extraction(context, subject_id=subject_id)
    extraction = ProductionExtractionV1(
        schema_version=1,
        subject_id=subject_id,
        production_input_hash="b" * 64,
        references_corpus_hash="c" * 64,
        profile_policy_version=EXTRACTION_PROFILE_POLICY_VERSION,
        sources=successful_context.sources,
        omitted_sources=tuple(
            ProductionExtractionOmissionV1(
                canonical_url=source.canonical_url,
                tier=source.tier,
                collection_state=CollectionState.ARCHIVED,
                reason=ProductionExtractionOmissionReason.SOURCE_EXTRACTION_FAILED,
                error_code="extraction_source_output_invalid",
            )
            for source in (primary, corroboration)
        ),
        warnings=(),
    )
    run_id = uuid4()
    references_blob = uuid4()
    extraction_blob = uuid4()
    references_artifact = SimpleNamespace(
        id=uuid4(),
        production_run_id=run_id,
        stage=ProductionArtifactStage.REFERENCES,
        version=1,
        status=ProductionArtifactStatus.VERIFIED,
        canonical_blob_id=references_blob,
    )
    extraction_artifact = SimpleNamespace(
        id=uuid4(),
        production_run_id=run_id,
        stage=ProductionArtifactStage.EXTRACTION,
        version=1,
        status=ProductionArtifactStatus.VERIFIED,
        canonical_blob_id=extraction_blob,
    )
    run = SimpleNamespace(id=run_id, subject_id=subject_id, pipeline_generation=1)
    uow = _IssueUow(run, (references_artifact, extraction_artifact))
    store = _ArtifactStore(
        {
            references_blob: production_reference_corpus_to_json(corpus),
            extraction_blob: production_extraction_to_json(extraction),
        }
    )

    issues = await ProductionRepairIssueService(
        lambda: uow, store
    ).list_source_extraction_failure_issues(uuid4(), subject_id)

    corroboration_issue = next(
        issue for issue in issues if issue.source_url == corroboration.canonical_url
    )
    assert corroboration_issue.source_criticality is ProductionSourceCriticality.CRITICAL
    assert (
        corroboration_issue.criticality_reason
        is ProductionSourceCriticalityReason.CORROBORATION_UNCOVERED
    )
    assert repair_issue_execution_state(corroboration_issue).blocking is True


@pytest.mark.asyncio
async def test_subject_repair_kind_filter_does_not_leak_source_failures() -> None:
    subject_id = uuid4()
    edition_id = uuid4()
    source_failure = SourceExtractionFailureIssue(
        repair_key="c" * 64,
        kind=ProductionRepairIssueKind.SOURCE_EXTRACTION_FAILED,
        source_id="doc-3",
        source_title="NCSC report",
        source_url="https://example.test/ncsc",
        publisher="NCSC",
        failure_code="extraction_source_output_invalid",
        extraction_warnings=("q2_parse_error:q2_no_payload",),
        source_criticality=ProductionSourceCriticality.CRITICAL,
        criticality_reason=ProductionSourceCriticalityReason.COUNTER_ANALYSIS,
        production_run_id=uuid4(),
        observed_artifact_id=uuid4(),
        observed_artifact_version=1,
        observed_pipeline_generation=2,
        subject_id=subject_id,
    )
    run = SimpleNamespace(id=uuid4(), edition_id=edition_id)

    class _Uow:
        production_runs = SimpleNamespace(
            get_current_for_subject=lambda _subject_id: _async_value(run)
        )

        async def __aenter__(self) -> _Uow:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

    class _IssueReader:
        async def list_supplemental_source_issues(
            self, _edition_id: object, _subject_id: object
        ) -> tuple[object, ...]:
            return ()

        async def list_source_extraction_failure_issues(
            self, _edition_id: object, _subject_id: object
        ) -> tuple[SourceExtractionFailureIssue, ...]:
            return (source_failure,)

        async def list_issue_views(
            self, _edition_id: object, _subject_id: object
        ) -> tuple[object, ...]:
            return ()

    application = FastAPI()
    application.include_router(production_router)
    application.state.uow_factory = lambda: _Uow()
    application.state.production_repair_issue_service = _IssueReader()

    async with AsyncClient(
        transport=ASGITransport(app=application), base_url="http://test"
    ) as client:
        rejected = await client.get(
            f"/api/subjects/{subject_id}/production/repairs",
            params={"kind": ProductionRepairIssueKind.REJECTED_INDICATOR.value},
        )
        source_losses = await client.get(
            f"/api/subjects/{subject_id}/production/repairs",
            params={"kind": ProductionRepairIssueKind.SOURCE_EXTRACTION_FAILED.value},
        )

    assert rejected.status_code == 200
    assert rejected.json()["issues"] == []
    assert source_losses.status_code == 200
    assert source_losses.json()["issues"][0]["source_criticality"] == "critical"
    assert source_losses.json()["issues"][0]["failure_code"] == "extraction_source_output_invalid"
    assert source_losses.json()["issues"][0]["extraction_warnings"] == [
        "q2_parse_error:q2_no_payload"
    ]
    assert source_losses.json()["issues"][0]["blocking"] is True
    assert source_losses.json()["issues"][0]["recommended_action"] == "review_source_loss"


@pytest.mark.asyncio
async def test_secondary_collection_loss_keeps_diagnostic_and_can_be_audited() -> None:
    subject_id = uuid4()
    edition_id = uuid4()
    core = _reference(
        url="https://example.test/core",
        title="Core report",
        tier=ProductionReferenceTier.CORE,
        role=SourceRole.PRIMARY,
        editorial_role=ProductionEditorialRole.PRIMARY,
    )
    secondary = _reference(
        url="https://example.test/reuters",
        title="Independent corroboration",
        tier=ProductionReferenceTier.SUPPORTING,
        role=SourceRole.INDEPENDENT,
        editorial_role=ProductionEditorialRole.CORROBORATION,
        eligible=False,
    )
    corpus = ProductionReferenceCorpusV1(
        schema_version=1,
        subject_id=subject_id,
        research_date=date(2026, 10, 8),
        production_input_hash="b" * 64,
        research_status=ProductionReferenceResearchStatus.COMPLETED,
        sources=(core, secondary),
        warnings=(),
    )
    run_id = uuid4()
    references_blob = uuid4()
    extraction_blob = uuid4()
    references_artifact = SimpleNamespace(
        id=uuid4(),
        production_run_id=run_id,
        stage=ProductionArtifactStage.REFERENCES,
        version=1,
        status=ProductionArtifactStatus.VERIFIED,
        canonical_blob_id=references_blob,
        raw_blob_id=None,
    )
    extraction_artifact = SimpleNamespace(
        id=uuid4(),
        production_run_id=run_id,
        stage=ProductionArtifactStage.EXTRACTION,
        version=1,
        status=ProductionArtifactStatus.VERIFIED,
        canonical_blob_id=extraction_blob,
    )
    run = SimpleNamespace(
        id=run_id,
        subject_id=subject_id,
        pipeline_generation=2,
        research_date=date(2026, 10, 8),
    )
    collection = SimpleNamespace(
        id=uuid4(),
        canonical_url=secondary.canonical_url,
        state=CollectionState.FAILED_RETRYABLE,
        error_reason="HTTP 403 from publisher",
        failure_reason_code="access_blocked",
        attempt_count=1,
    )

    class _ReferenceUow:
        production_runs = SimpleNamespace(list_for_edition=lambda _edition_id: _async_value([run]))
        production_artifacts = _ArtifactRepository((references_artifact, extraction_artifact))
        source_collections = SimpleNamespace(
            list_for_subject=lambda _subject_id: _async_value([collection])
        )
        production_repair_decisions = SimpleNamespace(
            effective_decisions=lambda _edition_id, _subject_id=None: _async_value(())
        )

        async def __aenter__(self) -> _ReferenceUow:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

    store = _ArtifactStore(
        {
            references_blob: production_reference_corpus_to_json(corpus),
            extraction_blob: production_extraction_to_json(
                _source_extraction(core, subject_id=subject_id)
            ),
        }
    )
    issue = (
        await ProductionRepairIssueService(
            lambda: _ReferenceUow(), store
        ).list_supplemental_source_issues(edition_id, subject_id)
    )[0]

    assert issue.failure_code == "access_blocked"
    assert issue.source_criticality is ProductionSourceCriticality.SECONDARY
    assert issue.criticality_reason is ProductionSourceCriticalityReason.REDUNDANT_CORROBORATION
    assert issue.criticality_policy_version == "source-criticality-v1"
    assert issue.repair_state.value == "continued_with_diagnostic"
    assert issue.recommended_action == "continue_without_source"
    assert repair_issue_execution_state(issue).resolved is True
    assert repair_issue_execution_state(issue).blocking is False
    assert repair_issue_execution_state(issue).application_state.value == "already_effective"
    assert repair_issue_execution_state(issue).impact.ready_to_apply is True
    missing_diagnostic = replace(issue, failure_code=None)
    assert repair_issue_execution_state(missing_diagnostic).blocking is True
    repair_key = repair_key_for_supplemental_source(
        edition_id=edition_id, subject_id=subject_id, source_url=secondary.canonical_url
    )
    decision = ProductionRepairDecision(
        edition_id=edition_id,
        subject_id=subject_id,
        production_run_id=run_id,
        observed_artifact_id=references_artifact.id,
        observed_pipeline_generation=run.pipeline_generation,
        repair_key=repair_key,
        issue_kind=ProductionRepairIssueKind.SUPPLEMENTAL_SOURCE_UNARCHIVED,
        action=ProductionRepairAction.CONTINUE_WITHOUT_SOURCE,
        actor_id="analyst-11",
        reason="The core report covers the claim; the loss is recorded for review.",
        source_criticality=issue.source_criticality,
        criticality_reason=issue.criticality_reason,
        failure_code=issue.failure_code,
        criticality_policy_version=issue.criticality_policy_version,
    )
    state = repair_issue_execution_state(issue, decision)
    assert state.resolved is True
    assert state.blocking is False
    assert issue.failure_code == "access_blocked"

    class _Session:
        row: object | None = None

        def add(self, row: object) -> None:
            self.row = row

        async def flush(self) -> None:
            return None

    session = _Session()
    await SqlAlchemyProductionRepairDecisionRepository(session).append(decision)  # type: ignore[arg-type]
    assert session.row is not None
    restored = _production_repair_decision_from_row(session.row)  # type: ignore[arg-type]
    assert restored.source_criticality is ProductionSourceCriticality.SECONDARY
    assert restored.criticality_reason is ProductionSourceCriticalityReason.REDUNDANT_CORROBORATION
    assert restored.failure_code == "access_blocked"
    assert restored.criticality_policy_version == "source-criticality-v1"


@pytest.mark.asyncio
async def test_source_failure_continue_requires_an_audited_analyst_reason() -> None:
    critical = SourceExtractionFailureIssue(
        repair_key="d" * 64,
        kind=ProductionRepairIssueKind.SOURCE_EXTRACTION_FAILED,
        source_id="doc-1",
        source_title="Counter-analysis",
        source_url="https://example.test/counter",
        publisher="NCSC",
        failure_code="q2_no_payload",
        extraction_warnings=("q2_parse_error:q2_no_payload",),
        source_criticality=ProductionSourceCriticality.CRITICAL,
        criticality_reason=ProductionSourceCriticalityReason.COUNTER_ANALYSIS,
        production_run_id=uuid4(),
        observed_artifact_id=uuid4(),
        observed_artifact_version=2,
        observed_pipeline_generation=4,
        subject_id=uuid4(),
    )

    class _Issues:
        async def get_supplemental_source_issue(self, *_args: object) -> None:
            return None

        async def get_source_extraction_failure_issue(
            self, *_args: object
        ) -> SourceExtractionFailureIssue:
            return critical

    class _Decisions:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        async def decide(self, **kwargs: object) -> dict[str, object]:
            self.calls.append(kwargs)
            return kwargs

    recorder = _Decisions()
    service = ProductionRepairAdjudicationService(
        lambda: None,
        issue_service=_Issues(),  # type: ignore[arg-type]
        decision_service=recorder,  # type: ignore[arg-type]
    )
    request_args = {
        "edition_id": uuid4(),
        "subject_id": critical.subject_id,
        "repair_key": critical.repair_key,
        "action": ProductionRepairAction.CONTINUE_WITHOUT_SOURCE,
        "observed_artifact_id": critical.observed_artifact_id,
        "observed_pipeline_generation": critical.observed_pipeline_generation,
        "expected_effective_decision_id": None,
        "actor_id": "analyst-7",
    }

    with pytest.raises(ProductionRepairAuditReasonRequiredError):
        await service.decide_current_issue(**request_args)

    recorded = await service.decide_current_issue(
        **request_args,
        reason="The alternate official source covers the affected finding.",
    )
    assert recorded["actor_id"] == "analyst-7"
    assert recorded["reason"] == "The alternate official source covers the affected finding."
    assert recorded["source_criticality"] is ProductionSourceCriticality.CRITICAL
    assert recorded["criticality_reason"] is ProductionSourceCriticalityReason.COUNTER_ANALYSIS
    assert recorded["failure_code"] == "q2_no_payload"
    assert recorded["criticality_policy_version"] == "source-criticality-v1"


def test_reasonless_legacy_waiver_stays_readable_but_unresolved() -> None:
    issue = SourceExtractionFailureIssue(
        repair_key="e" * 64,
        kind=ProductionRepairIssueKind.SOURCE_EXTRACTION_FAILED,
        source_id="doc-2",
        source_title="Report",
        source_url="https://example.test/report",
        publisher=None,
        failure_code="q2_no_payload",
        extraction_warnings=("q2_parse_error:q2_no_payload",),
        source_criticality=ProductionSourceCriticality.CRITICAL,
        criticality_reason=ProductionSourceCriticalityReason.COUNTER_ANALYSIS,
        production_run_id=uuid4(),
        observed_artifact_id=uuid4(),
        observed_artifact_version=1,
        observed_pipeline_generation=1,
        subject_id=uuid4(),
    )
    legacy = ProductionRepairDecision(
        edition_id=uuid4(),
        subject_id=issue.subject_id or uuid4(),
        production_run_id=issue.production_run_id,
        observed_artifact_id=issue.observed_artifact_id,
        observed_pipeline_generation=issue.observed_pipeline_generation,
        repair_key=issue.repair_key,
        issue_kind=ProductionRepairIssueKind.SUPPLEMENTAL_SOURCE_UNARCHIVED,
        action=ProductionRepairAction.CONTINUE_WITHOUT_SOURCE,
        actor_id="legacy-analyst",
        reason=None,
    )

    state = repair_issue_execution_state(issue, legacy)
    assert state.resolved is False
    assert state.blocking is True

    legacy_row = SimpleNamespace(
        id=uuid4(),
        edition_id=uuid4(),
        subject_id=issue.subject_id,
        production_run_id=issue.production_run_id,
        observed_artifact_id=issue.observed_artifact_id,
        observed_pipeline_generation=issue.observed_pipeline_generation,
        repair_key=issue.repair_key,
        issue_kind=ProductionRepairIssueKind.SUPPLEMENTAL_SOURCE_UNARCHIVED.value,
        action=ProductionRepairAction.CONTINUE_WITHOUT_SOURCE.value,
        correction_id=None,
        actor_id="legacy-analyst",
        reason=None,
        source_criticality=None,
        criticality_reason=None,
        failure_code=None,
        criticality_policy_version=None,
        created_at=legacy.created_at,
    )
    restored = _production_repair_decision_from_row(legacy_row)
    assert restored.reason is None
    assert restored.source_criticality is None
    assert restored.criticality_reason is None
    assert restored.criticality_policy_version is None


@pytest.mark.asyncio
async def test_low_level_continue_decision_cannot_bypass_reason_requirement() -> None:
    with pytest.raises(ProductionRepairAuditReasonRequiredError):
        await ProductionRepairDecisionService(lambda: None).decide(
            edition_id=uuid4(),
            subject_id=uuid4(),
            production_run_id=uuid4(),
            observed_artifact_id=uuid4(),
            observed_pipeline_generation=0,
            repair_key="f" * 64,
            issue_kind=ProductionRepairIssueKind.SOURCE_EXTRACTION_FAILED,
            action=ProductionRepairAction.CONTINUE_WITHOUT_SOURCE,
            actor_id="analyst-9",
        )
    with pytest.raises(ProductionRepairSourceDiagnosticRequiredError):
        await ProductionRepairDecisionService(lambda: None).decide(
            edition_id=uuid4(),
            subject_id=uuid4(),
            production_run_id=uuid4(),
            observed_artifact_id=uuid4(),
            observed_pipeline_generation=0,
            repair_key="f" * 64,
            issue_kind=ProductionRepairIssueKind.SOURCE_EXTRACTION_FAILED,
            action=ProductionRepairAction.CONTINUE_WITHOUT_SOURCE,
            actor_id="analyst-9",
            reason="The loss is explicitly reviewed.",
        )
