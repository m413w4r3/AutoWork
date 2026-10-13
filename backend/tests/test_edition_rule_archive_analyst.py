from __future__ import annotations

import hashlib
import json
import zipfile
from datetime import UTC, datetime, timedelta
from io import BytesIO
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest

from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.domain.editorial import (
    AnalystDecision,
    AnalystDecisionTargetType,
    AnalystDecisionType,
)
from cti_app.domain.investigation import (
    DetectionCandidateStatus,
    DetectionCandidateVersion,
    EvidenceKind,
    EvidenceObservation,
)
from cti_app.domain.production import ProductionArtifact, ProductionArtifactStage
from tests.test_edition_rule_archive import (
    EDITION_ID,
    RUN_A,
    RUN_B,
    SUBJECT_A,
    SUBJECT_B,
    _Fixture,
)

SOURCE = 'rule AnalystMarker { strings: $a = "analyst-marker" condition: $a }'
SOURCE_SHA256 = hashlib.sha256(SOURCE.encode()).hexdigest()
REPORT_SHA256 = "9" * 64


class _Investigations:
    def __init__(self, by_run: dict[UUID, Any]) -> None:
        self.by_run = by_run

    async def get_for_run(self, run_id: UUID) -> Any:
        return self.by_run.get(run_id)


class _Decisions:
    def __init__(self, by_investigation: dict[UUID, tuple[AnalystDecision, ...]]) -> None:
        self.by_investigation = by_investigation

    async def list_for_investigation(self, investigation_id: UUID) -> tuple[AnalystDecision, ...]:
        return self.by_investigation.get(investigation_id, ())


class _Candidates:
    def __init__(self, versions: dict[UUID, DetectionCandidateVersion]) -> None:
        self.versions = versions

    async def get(self, version_id: UUID) -> DetectionCandidateVersion | None:
        return self.versions.get(version_id)


class _Observations:
    def __init__(self, by_investigation: dict[UUID, tuple[EvidenceObservation, ...]]) -> None:
        self.by_investigation = by_investigation

    async def list_for_investigation(
        self, investigation_id: UUID
    ) -> tuple[EvidenceObservation, ...]:
        return self.by_investigation.get(investigation_id, ())


def _report_payload(version: DetectionCandidateVersion) -> dict[str, Any]:
    return {
        "schema": "investigation-yara-test.v1",
        "operation": "revise",
        "candidate_id": str(version.candidate_id),
        "version_id": str(version.id),
        "proposal_source_sha256": version.source_sha256,
        "source_sha256": version.source_sha256,
        "execution_status": "succeeded",
        "engine_version": "yara 4.test",
        "candidate_status": "tested",
        "selection_bias": "synthetic corpus",
        "coverage_statement": "one positive, no goodware match",
        "corpus_sha256": REPORT_SHA256,
        "corpus_sha256s_by_role": {
            "confirmed_positive": ["1" * 64],
            "out_of_cluster_candidate": [],
            "goodware": ["2" * 64],
            "holdout": [],
        },
        "roles": [
            {
                "role": "confirmed_positive",
                "corpus_sha256s": ["1" * 64],
                "matches": ["1" * 64],
                "non_matches": [],
                "non_measurable": [],
                "errors": [],
            },
            {
                "role": "goodware",
                "corpus_sha256s": ["2" * 64],
                "matches": [],
                "non_matches": ["2" * 64],
                "non_measurable": [],
                "errors": [],
            },
        ],
        "limitations": ["synthetic corpus"],
    }


def _report_observation(
    fixture: _Fixture,
    version: DetectionCandidateVersion,
    investigation_id: UUID,
) -> EvidenceObservation:
    payload = _report_payload(version)
    blob_id = fixture.blobs.put(payload)
    return EvidenceObservation(
        id=uuid4(),
        investigation_id=investigation_id,
        kind=EvidenceKind.YARA_MATCH,
        blob_id=blob_id,
        sha256=hashlib.sha256(ProductionArtifactStore.canonical_json_bytes(payload)).hexdigest(),
        source="AutoWork RuleWorkbench",
        source_version="investigation-rule-tools.v1",
        observed_at=datetime(2026, 10, 12, tzinfo=UTC),
        object_type="detection_candidate_version",
        object_id=str(version.id),
    )


def _investigation(run_id: UUID, subject_id: UUID) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid4(),
        production_run_id=run_id,
        subject_id=subject_id,
        synthesis_artifact_id=uuid4(),
    )


def _decision(
    *,
    investigation_id: UUID,
    version: DetectionCandidateVersion,
    article_id: UUID,
    article_sha256: str,
    report: EvidenceObservation,
    decision_type: AnalystDecisionType,
    occurred_at: datetime,
) -> AnalystDecision:
    return AnalystDecision(
        investigation_id=investigation_id,
        decision_type=decision_type,
        target_type=AnalystDecisionTargetType.DETECTION,
        target_id=version.id,
        actor_id="human:reviewer",
        reason="Reviewed exact source and test report",
        correlation_id=f"review-{uuid4()}",
        occurred_at=occurred_at,
        details={
            "schema": "detection-review.v1",
            "source_sha256": version.source_sha256,
            "known_limitations": "Synthetic corpus; holdout unavailable.",
            "test_report": {
                "observation_id": str(report.id),
                "sha256": report.sha256,
                "corpus_sha256": REPORT_SHA256,
                "engine_version": "yara 4.test",
            },
            "base_publication": {"artifact_id": str(article_id), "sha256": article_sha256},
        },
    )


def _version(
    investigation_id: UUID, *, status: DetectionCandidateStatus
) -> DetectionCandidateVersion:
    return DetectionCandidateVersion(
        id=uuid4(),
        investigation_id=investigation_id,
        candidate_id=uuid4(),
        source=SOURCE,
        source_sha256=SOURCE_SHA256,
        motif_provenance=(),
        status=status,
    )


async def _attach_review_state(
    fixture: _Fixture,
    *,
    run_id: UUID,
    subject_id: UUID,
    status: DetectionCandidateStatus,
    decision_type: AnalystDecisionType | None,
    approved_at: datetime | None = None,
    v6_base: bool = False,
) -> tuple[DetectionCandidateVersion, AnalystDecision | None, UUID]:
    investigation = _investigation(run_id, subject_id)
    version = _version(investigation.id, status=status)
    report = _report_observation(fixture, version, investigation.id)
    article_entry = next(item for item in fixture.entries if item.subject_id == subject_id)
    article_id = article_entry.document_artifact_id
    article_artifact = fixture.uow.production_artifacts.artifacts[article_id]
    article_payload = await fixture.blobs.read_json(article_artifact.canonical_blob_id)  # type: ignore[arg-type]

    if v6_base:
        parent_id = article_id
        parent_payload = article_payload
        parent_sha256 = hashlib.sha256(
            ProductionArtifactStore.canonical_json_bytes(parent_payload)
        ).hexdigest()
        v6_payload = {
            "schema_version": "6",
            "base_publication": parent_payload,
            "provenance": {
                "base_publication_artifact_id": str(parent_id),
                "base_publication_sha256": parent_sha256,
            },
        }
        extraction_id = article_artifact.metadata["input_artifacts"]["extraction_artifact_id"]
        v6_id = uuid4()
        v6_artifact = ProductionArtifact(
            id=v6_id,
            production_run_id=run_id,
            subject_id=subject_id,
            stage=ProductionArtifactStage.PUBLICATION,
            version=2,
            input_hash=hashlib.sha256(
                ProductionArtifactStore.canonical_json_bytes(v6_payload)
            ).hexdigest(),
            canonical_blob_id=fixture.blobs.put(v6_payload),
            metadata={
                "previous_publication_artifact_id": str(parent_id),
                "publication_augmentation": {"investigation_id": str(investigation.id)},
                "input_artifacts": {"extraction_artifact_id": extraction_id},
            },
        )
        fixture.uow.production_artifacts.artifacts[v6_id] = v6_artifact
        article_id = v6_id
        article_payload = v6_payload

    article_sha256 = hashlib.sha256(
        ProductionArtifactStore.canonical_json_bytes(article_payload)
    ).hexdigest()
    decision = (
        _decision(
            investigation_id=investigation.id,
            version=version,
            article_id=article_id,
            article_sha256=article_sha256,
            report=report,
            decision_type=decision_type,
            occurred_at=approved_at or datetime(2026, 10, 13, tzinfo=UTC),
        )
        if decision_type is not None
        else None
    )
    existing_investigations = getattr(fixture.uow, "analyst_investigations", None)
    investigation_map = dict(existing_investigations.by_run) if existing_investigations else {}
    investigation_map.setdefault(RUN_A, _investigation(RUN_A, SUBJECT_A))
    investigation_map.setdefault(RUN_B, _investigation(RUN_B, SUBJECT_B))
    investigation_map[run_id] = investigation
    existing_decisions = getattr(fixture.uow, "analyst_decisions", None)
    decisions = dict(existing_decisions.by_investigation) if existing_decisions else {}
    decisions.setdefault(investigation.id, ())
    if decision is not None:
        decisions[investigation.id] = (*decisions[investigation.id], decision)
    existing_observations = getattr(fixture.uow, "evidence_observations", None)
    observations = dict(existing_observations.by_investigation) if existing_observations else {}
    observations.setdefault(investigation.id, ())
    observations[investigation.id] = (*observations[investigation.id], report)
    fixture.uow.analyst_investigations = _Investigations(investigation_map)
    fixture.uow.analyst_decisions = _Decisions(decisions)
    existing_candidates = getattr(fixture.uow, "detection_candidates", None)
    candidates = dict(existing_candidates.versions) if existing_candidates else {}
    candidates[version.id] = version
    fixture.uow.detection_candidates = _Candidates(candidates)
    fixture.uow.evidence_observations = _Observations(observations)
    return version, decision, article_id


def _archive_manifest(content: bytes) -> dict[str, Any]:
    with zipfile.ZipFile(BytesIO(content)) as archive:
        return json.loads(archive.read("manifest.json"))  # type: ignore[no-any-return]


@pytest.mark.asyncio
async def test_approved_rule_from_linked_v6_is_archived_with_its_provenance() -> None:
    fixture = _Fixture()
    version, decision, _v6_id = await _attach_review_state(
        fixture,
        run_id=RUN_A,
        subject_id=SUBJECT_A,
        status=DetectionCandidateStatus.TESTED,
        decision_type=AnalystDecisionType.DETECTION_APPROVE,
        approved_at=fixture.manifest.created_at - timedelta(seconds=1),
        v6_base=True,
    )

    archive = await fixture.service().build(EDITION_ID)

    with zipfile.ZipFile(BytesIO(archive.content)) as bundle:
        [path] = [name for name in bundle.namelist() if name.startswith("analyst/yara/")]
        assert bundle.read(path).decode() == SOURCE
    metadata = _archive_manifest(archive.content)["analyst_rules"][0]
    assert metadata["id"] == str(version.id)
    assert metadata["version"] == version.version
    assert metadata["source_sha256"] == SOURCE_SHA256
    assert metadata["article"]["artifact_id"] == str(fixture.entries[0].document_artifact_id)
    assert metadata["investigation_id"] == str(version.investigation_id)
    assert metadata["approver"] == "human:reviewer"
    assert metadata["approved_at"] == decision.occurred_at.isoformat()
    assert metadata["known_limitations"] == "Synthetic corpus; holdout unavailable."
    assert metadata["test_report"]["observation_id"]
    assert metadata["test_report"]["sha256"]
    assert archive.sha256 == hashlib.sha256(archive.content).hexdigest()


@pytest.mark.asyncio
async def test_analyst_rule_order_and_archive_hash_are_deterministic() -> None:
    fixture = _Fixture()
    await _attach_review_state(
        fixture,
        run_id=RUN_A,
        subject_id=SUBJECT_A,
        status=DetectionCandidateStatus.TESTED,
        decision_type=AnalystDecisionType.DETECTION_APPROVE,
        approved_at=fixture.manifest.created_at - timedelta(seconds=2),
        v6_base=True,
    )
    await _attach_review_state(
        fixture,
        run_id=RUN_B,
        subject_id=SUBJECT_B,
        status=DetectionCandidateStatus.TESTED,
        decision_type=AnalystDecisionType.DETECTION_APPROVE,
        approved_at=fixture.manifest.created_at - timedelta(seconds=1),
    )

    first = await fixture.service().build(EDITION_ID)
    second = await fixture.service().build(EDITION_ID)
    manifest = _archive_manifest(first.content)

    assert first.content == second.content
    assert first.sha256 == second.sha256
    assert [item["article"]["position"] for item in manifest["analyst_rules"]] == [1, 2]


@pytest.mark.asyncio
async def test_candidate_rejected_or_not_reviewed_never_enters_archive() -> None:
    fixture = _Fixture()
    await _attach_review_state(
        fixture,
        run_id=RUN_A,
        subject_id=SUBJECT_A,
        status=DetectionCandidateStatus.CANDIDATE,
        decision_type=None,
    )
    await _attach_review_state(
        fixture,
        run_id=RUN_B,
        subject_id=SUBJECT_B,
        status=DetectionCandidateStatus.TESTED,
        decision_type=AnalystDecisionType.DETECTION_REJECT,
        approved_at=fixture.manifest.created_at - timedelta(seconds=1),
    )

    archive = await fixture.service().build(EDITION_ID)

    assert not any(name.startswith("analyst/yara/") for name in _names(archive.content))
    assert "analyst_rules" not in _archive_manifest(archive.content)


@pytest.mark.asyncio
async def test_rule_for_an_excluded_article_is_not_archived() -> None:
    fixture = _Fixture()
    _version_b, decision_b, _base_b = await _attach_review_state(
        fixture,
        run_id=RUN_B,
        subject_id=SUBJECT_B,
        status=DetectionCandidateStatus.TESTED,
        decision_type=AnalystDecisionType.DETECTION_APPROVE,
        approved_at=fixture.manifest.created_at - timedelta(seconds=1),
    )
    assert decision_b is not None
    from cti_app.domain.edition_publication import (
        PublicationManifestExclusionV1,
        PublicationManifestV1,
    )

    fixture.manifest = PublicationManifestV1.create(
        id=uuid4(),
        edition_id=EDITION_ID,
        edition_version=fixture.manifest.edition_version,
        batch_id=fixture.manifest.batch_id,
        created_by=fixture.manifest.created_by,
        entries=(fixture.entries[0],),
        exclusions=(
            PublicationManifestExclusionV1(subject_id=SUBJECT_B, review_decision_id=uuid4()),
        ),
        created_at=fixture.manifest.created_at + timedelta(seconds=1),
    )
    fixture.uow.publication_manifests.manifest = fixture.manifest

    archive = await fixture.service().build(EDITION_ID)

    assert not any(name.startswith("analyst/yara/") for name in _names(archive.content))


@pytest.mark.asyncio
async def test_approval_after_a_manifest_does_not_change_that_manifest_archive() -> None:
    fixture = _Fixture()
    before = await fixture.service().build(EDITION_ID)
    _version_a, _decision, _base_a = await _attach_review_state(
        fixture,
        run_id=RUN_A,
        subject_id=SUBJECT_A,
        status=DetectionCandidateStatus.TESTED,
        decision_type=AnalystDecisionType.DETECTION_APPROVE,
        approved_at=fixture.manifest.created_at + timedelta(seconds=1),
    )

    after = await fixture.service().build(EDITION_ID)

    assert after.content == before.content
    assert (
        fixture.uow.publication_manifests.manifest.content_sha256 == fixture.manifest.content_sha256
    )


def _names(content: bytes) -> list[str]:
    with zipfile.ZipFile(BytesIO(content)) as archive:
        return archive.namelist()
