from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from cti_app.application.production_parsers import (
    ParsedEvent,
    ParsedSource,
    ReferenceReport,
    TechnicalExtraction,
)
from cti_app.application.production_repairs import (
    EffectiveExtractionProjector,
    ProductionRepairIssueView,
    classify_repair_impact,
    synthesis_projection_hash,
)
from cti_app.application.production_stages import ProductionQAService
from cti_app.application.production_workflow import ProductionWorkflowOrchestrator
from cti_app.domain.discovery import SourceRole
from cti_app.domain.production import (
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionArtifactStatus,
    ProductionDerivedOutput,
    ProductionRepairAction,
    ProductionRepairDecision,
    ProductionRepairImpactKind,
    ProductionRepairIssueKind,
    RepairDecisionApplicationState,
)

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
            ProductionRepairImpactKind.NARRATIVE,
            True,
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

    result = await ProductionQAService(lambda: None).run_qa(  # type: ignore[arg-type]
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


class _HistoricalStore:
    def __init__(self, extraction_id: UUID, synthesis_id: UUID) -> None:
        self.extraction_id = extraction_id
        self.synthesis_id = synthesis_id

    async def read_json(self, blob_id: UUID) -> dict[str, object]:
        assert blob_id == self.extraction_id
        return {"items": [], "rules": [], "uncertainties": []}

    async def read_text(self, blob_id: UUID) -> str:
        assert blob_id == self.synthesis_id
        return "Fait [S1]"


class _HistoricalArtifacts:
    def __init__(self, artifacts: tuple[ProductionArtifact, ...]) -> None:
        self.artifacts = artifacts

    async def list_for_run(self, _run_id: UUID) -> tuple[ProductionArtifact, ...]:
        return self.artifacts


@pytest.mark.asyncio
async def test_lot33_legacy_synthesis_hash_is_reused_when_semantic_projection_is_unchanged() -> (
    None
):
    now = datetime.now(UTC)
    extraction_blob = uuid4()
    synthesis_blob = uuid4()
    old_extraction = ProductionArtifact(
        production_run_id=RUN_ID,
        subject_id=SUBJECT_ID,
        stage=ProductionArtifactStage.EXTRACTION,
        version=1,
        input_hash="b" * 64,
        status=ProductionArtifactStatus.STALE,
        canonical_blob_id=extraction_blob,
        created_at=now - timedelta(minutes=2),
    )
    historical_synthesis = ProductionArtifact(
        production_run_id=RUN_ID,
        subject_id=SUBJECT_ID,
        stage=ProductionArtifactStage.SYNTHESIS,
        version=1,
        input_hash="c" * 64,
        status=ProductionArtifactStatus.STALE,
        rendered_blob_id=synthesis_blob,
        created_at=now - timedelta(minutes=1),
    )
    report = ReferenceReport(
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
    extraction = TechnicalExtraction(items=())
    semantic_hash = synthesis_projection_hash(report, extraction, {})
    run = SimpleNamespace(id=RUN_ID, subject_id=SUBJECT_ID)
    uow = SimpleNamespace(
        production_artifacts=_HistoricalArtifacts((old_extraction, historical_synthesis))
    )
    orchestrator = ProductionWorkflowOrchestrator(
        lambda: None,  # type: ignore[arg-type]
        artifact_store=_HistoricalStore(extraction_blob, synthesis_blob),  # type: ignore[arg-type]
    )

    found = await orchestrator._find_compatible_historical_synthesis(
        uow=uow,
        run=run,  # type: ignore[arg-type]
        report=report,
        extraction_payload=extraction,
        source_tiers_by_url={},
        semantic_projection_hash=semantic_hash,
    )

    assert found is historical_synthesis
    assert historical_synthesis.input_hash == "c" * 64
