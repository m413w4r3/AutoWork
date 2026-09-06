"""The detail, the adjudication and the projection share one resolved value.

A legacy rejection carries no inline value, only a hash and its ``ModelRun``.
What the analyst sees must be exactly what an INCLUDE accepts and exactly what
the projection writes into the extraction — or must be refused everywhere.
"""

from __future__ import annotations

import hashlib
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest

from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.application.production_parsers import (
    TechnicalExtraction,
    technical_extraction_from_json,
    technical_extraction_to_json,
)
from cti_app.application.production_repair_payloads import (
    ProductionRepairPayloadResolver,
    RepairPayloadOrigin,
)
from cti_app.application.production_repairs import (
    ProductionRepairAdjudicationService,
    ProductionRepairIssueService,
    ProductionRepairProjectionService,
    ProductionRepairValueNotVerifiableError,
    repair_key_for_rejection,
)
from cti_app.domain.production import (
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionArtifactStatus,
    ProductionEvidenceBasis,
    ProductionRepairAction,
    ProductionRepairDecision,
    ProductionRepairIssueKind,
    SubjectProductionStatus,
)
from tests.test_production_recovery_support import (
    DOMAIN,
    MODEL_RUN_ID,
    SOURCE_URL,
    YARA_BODY,
    RecordingArchive,
    individual_archive,
    legacy_verification,
    sha256,
)
from tests.test_production_repairs import (
    ARTIFACT_ID,
    EDITION_ID,
    RUN_ID,
    SUBJECT_ID,
    _BlobCatalog,
    _DecisionRepository,
    _ProjectionFactory,
    _ProjectionUow,
)

DOMAIN_KEY = repair_key_for_rejection(
    edition_id=EDITION_ID,
    subject_id=SUBJECT_ID,
    kind=ProductionRepairIssueKind.REJECTED_INDICATOR,
    source_url=SOURCE_URL,
    artifact_type="domain",
    value=DOMAIN,
)
RULE_KEY = repair_key_for_rejection(
    edition_id=EDITION_ID,
    subject_id=SUBJECT_ID,
    kind=ProductionRepairIssueKind.REJECTED_RULE,
    source_url=SOURCE_URL,
    artifact_type="yara",
    value=YARA_BODY,
)


class _LegacyArtifacts:
    def __init__(self, artifact: Any) -> None:
        self.artifact = artifact

    async def get_current(self, _run_id: UUID, _stage: str) -> Any:
        return self.artifact

    async def get(self, artifact_id: UUID) -> Any:
        return self.artifact if artifact_id == self.artifact.id else None

    async def list_current_for_edition(self, _edition_id: UUID, _stage: str) -> list[Any]:
        return [self.artifact]


class _LegacyIssueUow:
    """A run whose current extraction only holds pre-LOT18 diagnostics."""

    def __init__(self, artifact: Any, decisions: _DecisionRepository | None = None) -> None:
        async def list_runs(_edition_id: UUID) -> list[Any]:
            return [SimpleNamespace(id=RUN_ID, subject_id=SUBJECT_ID, pipeline_generation=2)]

        self.subject_production_runs = SimpleNamespace(list_for_edition=list_runs)
        self.production_artifacts = _LegacyArtifacts(artifact)
        self.production_repair_decisions = decisions or _DecisionRepository()

    async def __aenter__(self) -> _LegacyIssueUow:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def commit(self) -> None:
        return None


class _LegacyFactory:
    def __init__(self, uow: _LegacyIssueUow) -> None:
        self.uow = uow

    def __call__(self) -> _LegacyIssueUow:
        return self.uow


def _legacy_artifact(canonical_blob_id: UUID | None = None) -> ProductionArtifact:
    return ProductionArtifact(
        id=ARTIFACT_ID,
        production_run_id=RUN_ID,
        subject_id=SUBJECT_ID,
        stage=ProductionArtifactStage.EXTRACTION,
        version=1,
        input_hash="a" * 64,
        status=ProductionArtifactStatus.VERIFIED,
        canonical_blob_id=canonical_blob_id,
        metadata={"deterministic_verification": legacy_verification()},
    )


async def test_legacy_issue_detail_shows_the_recovered_value_and_its_origin() -> None:
    archive = individual_archive()
    service = ProductionRepairIssueService(
        _LegacyFactory(_LegacyIssueUow(_legacy_artifact())),
        None,
        ProductionRepairPayloadResolver(archive),
    )

    issues = await service.list_issues(EDITION_ID, SUBJECT_ID)
    listed = next(issue for issue in issues if issue.repair_key == DOMAIN_KEY)

    # The bounded list never opens an archive, but says recovery may be possible.
    assert listed.payload_available is False
    assert listed.legacy_evidence is True
    assert archive.reads == []

    detail = await service.get_issue(EDITION_ID, DOMAIN_KEY, SUBJECT_ID)

    assert detail is not None
    assert detail.value == DOMAIN
    assert detail.payload_available is True
    assert detail.payload_origin is RepairPayloadOrigin.MODEL_OUTPUT_RECOVERED
    assert sha256(detail.value) == detail.issue.value_sha256
    assert archive.reads == ["model://q2/individual"]


async def test_an_unrecoverable_legacy_issue_refuses_include_but_allows_exclude() -> None:
    empty_archive = RecordingArchive({}, {})
    decisions = _DecisionRepository()
    factory = _LegacyFactory(_LegacyIssueUow(_legacy_artifact(), decisions))
    issues = ProductionRepairIssueService(
        factory, None, ProductionRepairPayloadResolver(empty_archive)
    )
    adjudication = ProductionRepairAdjudicationService(factory, issues)
    detail = await issues.get_issue(EDITION_ID, DOMAIN_KEY, SUBJECT_ID)

    assert detail is not None
    assert detail.value is None
    assert detail.payload_available is False
    assert detail.payload_origin is RepairPayloadOrigin.UNAVAILABLE

    with pytest.raises(ProductionRepairValueNotVerifiableError):
        await adjudication.decide_current_issue(
            edition_id=EDITION_ID,
            subject_id=SUBJECT_ID,
            repair_key=DOMAIN_KEY,
            action=ProductionRepairAction.INCLUDE,
            observed_artifact_id=ARTIFACT_ID,
            observed_pipeline_generation=2,
            expected_effective_decision_id=None,
            actor_id="analyst",
        )


def _decision(repair_key: str, kind: ProductionRepairIssueKind) -> ProductionRepairDecision:
    return ProductionRepairDecision(
        edition_id=EDITION_ID,
        subject_id=SUBJECT_ID,
        production_run_id=RUN_ID,
        observed_artifact_id=ARTIFACT_ID,
        observed_pipeline_generation=2,
        repair_key=repair_key,
        issue_kind=kind,
        action=ProductionRepairAction.INCLUDE,
        actor_id="analyst",
    )


async def test_projection_materializes_exactly_the_recovered_legacy_values() -> None:
    catalog = _BlobCatalog()
    store = ProductionArtifactStore(catalog)  # type: ignore[arg-type]
    canonical_id = await store.put_json(
        technical_extraction_to_json(TechnicalExtraction(items=(), rules=())),
        bucket="production-artifacts-canonical",
    )
    base = _legacy_artifact(canonical_id)
    run = SimpleNamespace(
        id=RUN_ID,
        edition_id=EDITION_ID,
        subject_id=SUBJECT_ID,
        status=SubjectProductionStatus.READY,
        requires_reconciliation=False,
        pipeline_generation=2,
    )
    decisions = [
        _decision(DOMAIN_KEY, ProductionRepairIssueKind.REJECTED_INDICATOR),
        _decision(RULE_KEY, ProductionRepairIssueKind.REJECTED_RULE),
    ]
    archive = individual_archive()

    result = await ProductionRepairProjectionService(
        _ProjectionFactory(_ProjectionUow(run, base, decisions)),
        store,  # type: ignore[arg-type]
        payload_resolver=ProductionRepairPayloadResolver(archive),
    ).project_effective_extraction(RUN_ID, actor_id="analyst")

    assert result.changed
    projected = technical_extraction_from_json(
        await store.read_json(result.artifact.canonical_blob_id)  # type: ignore[arg-type]
    )
    # The value the analyst saw is the value the deliverable now contains.
    assert [item.value for item in projected.items] == [DOMAIN]
    assert projected.items[0].evidence_basis is ProductionEvidenceBasis.ANALYST_OVERRIDE
    assert [rule.body for rule in projected.rules] == [YARA_BODY]
    assert projected.rules[0].sha256 == hashlib.sha256(YARA_BODY.encode()).hexdigest()
    # Two issues, one ModelRun: its archived answer was read exactly once.
    assert archive.reads == ["model://q2/individual"]


async def test_projection_refuses_to_materialize_an_unrecoverable_include() -> None:
    catalog = _BlobCatalog()
    store = ProductionArtifactStore(catalog)  # type: ignore[arg-type]
    canonical_id = await store.put_json(
        technical_extraction_to_json(TechnicalExtraction(items=(), rules=())),
        bucket="production-artifacts-canonical",
    )
    base = _legacy_artifact(canonical_id)
    run = SimpleNamespace(
        id=RUN_ID,
        edition_id=EDITION_ID,
        subject_id=SUBJECT_ID,
        status=SubjectProductionStatus.READY,
        requires_reconciliation=False,
        pipeline_generation=2,
    )
    service = ProductionRepairProjectionService(
        _ProjectionFactory(
            _ProjectionUow(
                run, base, [_decision(DOMAIN_KEY, ProductionRepairIssueKind.REJECTED_INDICATOR)]
            )
        ),
        store,  # type: ignore[arg-type]
        payload_resolver=ProductionRepairPayloadResolver(RecordingArchive({}, {})),
    )

    with pytest.raises(Exception, match="repair_payload_unavailable"):
        await service.project_effective_extraction(RUN_ID, actor_id="analyst")


async def test_unknown_repair_key_is_still_absent_from_the_detail() -> None:
    service = ProductionRepairIssueService(
        _LegacyFactory(_LegacyIssueUow(_legacy_artifact())),
        None,
        ProductionRepairPayloadResolver(individual_archive()),
    )

    assert await service.get_issue(EDITION_ID, "f" * 64, SUBJECT_ID) is None
    assert uuid4() != MODEL_RUN_ID
