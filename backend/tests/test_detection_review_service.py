from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from types import SimpleNamespace, TracebackType
from typing import Any
from uuid import UUID, uuid4

import pytest

from cti_app.application.detection_review_service import (
    DetectionReviewError,
    DetectionReviewErrorCode,
    DetectionReviewService,
)
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.domain.editorial import AnalystDecisionType
from cti_app.domain.investigation import (
    DetectionCandidateStatus,
    DetectionCandidateVersion,
    EvidenceKind,
    EvidenceObservation,
)
from cti_app.domain.production import (
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionArtifactStatus,
)
from cti_app.domain.publication_document import (
    PUBLICATION_DOCUMENT_V4_SCHEMA_VERSION,
    PublicationDocumentV4,
    serialize_publication_document,
)

INVESTIGATION_ID = UUID("a3bcefff-a850-4ecd-8d60-b2c9d90d64d0")
RUN_ID = UUID("f4d809d0-818a-4de0-9a1c-01c61089c4f0")
SUBJECT_ID = UUID("57d93f90-fc44-4abc-8a72-2bac4d899612")
ACTOR = "analyst:reviewer"
SOURCE = 'rule Sample { strings: $a = "safe-marker" condition: $a }'
SOURCE_SHA256 = hashlib.sha256(SOURCE.encode()).hexdigest()


class _BlobStore:
    def __init__(self) -> None:
        self.payloads: dict[UUID, dict[str, Any]] = {}

    def put(self, payload: dict[str, Any]) -> UUID:
        blob_id = uuid4()
        self.payloads[blob_id] = payload
        return blob_id

    async def read_json(self, blob_id: UUID) -> dict[str, Any]:
        return self.payloads[blob_id]


class _Candidates:
    def __init__(self, version: DetectionCandidateVersion) -> None:
        self.version = version

    async def get(self, version_id: UUID) -> DetectionCandidateVersion | None:
        return self.version if self.version.id == version_id else None

    async def list_for_investigation(
        self, investigation_id: UUID
    ) -> tuple[DetectionCandidateVersion, ...]:
        return (self.version,) if self.version.investigation_id == investigation_id else ()


class _Observations:
    def __init__(self, observations: tuple[EvidenceObservation, ...] = ()) -> None:
        self.items = list(observations)

    async def list_for_investigation(
        self, investigation_id: UUID
    ) -> tuple[EvidenceObservation, ...]:
        return tuple(item for item in self.items if item.investigation_id == investigation_id)


class _Decisions:
    def __init__(self) -> None:
        self.items = []

    async def list_for_investigation(self, investigation_id: UUID) -> tuple[Any, ...]:
        return tuple(item for item in self.items if item.investigation_id == investigation_id)

    async def append(self, decision: Any) -> None:
        self.items.append(decision)


class _Artifacts:
    def __init__(self, publication: ProductionArtifact, synthesis: ProductionArtifact) -> None:
        self.items = {publication.id: publication, synthesis.id: synthesis}

    async def get(self, artifact_id: UUID) -> ProductionArtifact | None:
        return self.items.get(artifact_id)

    async def get_current_for_revision(self, run_id: UUID, stage: str) -> ProductionArtifact:
        assert run_id == RUN_ID
        assert stage == ProductionArtifactStage.PUBLICATION.value
        return next(
            item
            for item in self.items.values()
            if item.stage is ProductionArtifactStage.PUBLICATION
        )


class _Investigations:
    def __init__(self) -> None:
        self.investigation = SimpleNamespace(
            id=INVESTIGATION_ID,
            production_run_id=RUN_ID,
            subject_id=SUBJECT_ID,
            synthesis_artifact_id=UUID("c844006f-b81c-4bc9-98dd-383caf7088dd"),
        )

    async def get(self, investigation_id: UUID) -> Any:
        return self.investigation if investigation_id == INVESTIGATION_ID else None


class _Uow:
    def __init__(self, fixture: _Fixture) -> None:
        self.detection_candidates = _Candidates(fixture.version)
        self.evidence_observations = _Observations(fixture.observations)
        self.analyst_decisions = _Decisions()
        self.analyst_investigations = _Investigations()
        self.production_artifacts = _Artifacts(fixture.publication, fixture.synthesis)
        self.commits = 0

    async def __aenter__(self) -> _Uow:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None

    async def commit(self) -> None:
        self.commits += 1


class _Fixture:
    def __init__(self, *, with_report: bool = True, noisy: bool = False) -> None:
        self.blobs = _BlobStore()
        self.version = DetectionCandidateVersion(
            id=UUID("b23b570e-894d-4d42-827a-f50a1a9721ae"),
            investigation_id=INVESTIGATION_ID,
            candidate_id=UUID("7ee8f978-184f-4d00-a469-50d413e6f2bb"),
            source=SOURCE,
            source_sha256=SOURCE_SHA256,
            motif_provenance=(),
            status=DetectionCandidateStatus.TESTED,
        )
        publication_id = UUID("5ce6e7fa-8b8a-4685-91fb-88dfb2821fa8")
        base_document = PublicationDocumentV4(
            schema_version=PUBLICATION_DOCUMENT_V4_SCHEMA_VERSION,
            subject_id=SUBJECT_ID,
            publication_language="fr",
            title="Article de test",
            lead=(),
            sections=(),
            timeline=(),
            indicators=(),
            sources=(),
            uncertainties=(),
            tables=(),
            diagrams=(),
            figures=(),
        )
        publication_payload = serialize_publication_document(base_document)
        publication_blob_id = self.blobs.put(publication_payload)
        self.publication = ProductionArtifact(
            id=publication_id,
            production_run_id=RUN_ID,
            subject_id=SUBJECT_ID,
            stage=ProductionArtifactStage.PUBLICATION,
            version=1,
            input_hash="a" * 64,
            status=ProductionArtifactStatus.VERIFIED,
            canonical_blob_id=publication_blob_id,
        )
        self.synthesis = ProductionArtifact(
            id=UUID("c844006f-b81c-4bc9-98dd-383caf7088dd"),
            production_run_id=RUN_ID,
            subject_id=SUBJECT_ID,
            stage=ProductionArtifactStage.SYNTHESIS,
            version=1,
            input_hash="b" * 64,
            status=ProductionArtifactStatus.VERIFIED,
            canonical_blob_id=uuid4(),
        )
        self.observations: tuple[EvidenceObservation, ...] = (
            (self._report(noisy=noisy),) if with_report else ()
        )
        self.uow = _Uow(self)
        self.service = DetectionReviewService(lambda: self.uow, self.blobs)  # type: ignore[arg-type]

    def _report(self, *, noisy: bool) -> EvidenceObservation:
        hashes = ["1" * 64, "2" * 64]
        goodware_matches = [hashes[1]] if noisy else []
        payload = {
            "schema": "investigation-yara-test.v1",
            "operation": "revise",
            "candidate_id": str(self.version.candidate_id),
            "version_id": str(self.version.id),
            "proposal_source_sha256": SOURCE_SHA256,
            "source_sha256": SOURCE_SHA256,
            "execution_status": "succeeded",
            "validation_status": "real_partial",
            "engine_version": "yara 4.test",
            "candidate_status": "tested",
            "selection_bias": "synthetic test corpus",
            "coverage_statement": "two synthetic cases",
            "corpus_sha256": "3" * 64,
            "provenance_counts_by_role": {
                "confirmed_positive": {"real": 0, "synthetic": 1},
                "goodware": {"real": 1, "synthetic": 0},
            },
            "false_positive_count": len(goodware_matches),
            "false_positive_sha256s": goodware_matches,
            "false_negative_count": 0,
            "false_negative_sha256s": [],
            "corpus_sha256s_by_role": {
                "confirmed_positive": [hashes[0]],
                "out_of_cluster_candidate": [],
                "goodware": [hashes[1]],
                "holdout": [],
            },
            "roles": [
                {
                    "role": "confirmed_positive",
                    "corpus_sha256s": [hashes[0]],
                    "provenance_counts": {"real": 0, "synthetic": 1},
                    "test_cases": [
                        {"sha256": hashes[0], "provenance": "synthetic", "outcome": "match"}
                    ],
                    "matches": [hashes[0]],
                    "non_matches": [],
                    "non_measurable": [],
                    "errors": [],
                },
                {
                    "role": "goodware",
                    "corpus_sha256s": [hashes[1]],
                    "provenance_counts": {"real": 1, "synthetic": 0},
                    "test_cases": [
                        {
                            "sha256": hashes[1],
                            "provenance": "real",
                            "outcome": "match" if noisy else "non_match",
                        }
                    ],
                    "matches": goodware_matches,
                    "non_matches": [] if noisy else [hashes[1]],
                    "non_measurable": [],
                    "errors": [],
                },
            ],
            "limitations": ["synthetic fixture"],
        }
        blob_id = self.blobs.put(payload)
        return EvidenceObservation(
            id=UUID("9a43b1c5-3484-4d8c-a08b-135f00f0c0da"),
            investigation_id=INVESTIGATION_ID,
            kind=EvidenceKind.YARA_MATCH,
            blob_id=blob_id,
            sha256=hashlib.sha256(
                ProductionArtifactStore.canonical_json_bytes(payload)
            ).hexdigest(),
            source="AutoWork RuleWorkbench",
            source_version="investigation-rule-tools.v1",
            observed_at=datetime(2026, 10, 13, tzinfo=UTC),
            object_type="detection_candidate_version",
            object_id=str(self.version.id),
        )


@pytest.mark.asyncio
async def test_approval_requires_an_exact_test_report_and_article_base() -> None:
    fixture = _Fixture(with_report=False)

    with pytest.raises(DetectionReviewError) as error:
        await fixture.service.approve(
            fixture.version.id,
            SOURCE_SHA256,
            ACTOR,
            "Reviewed source and corpus",
            "No known false positives in the available goodware corpus.",
            "approve-no-report",
        )

    assert error.value.code is DetectionReviewErrorCode.TEST_REPORT_NOT_FOUND
    assert fixture.uow.commits == 0


@pytest.mark.asyncio
async def test_detection_inventory_includes_candidate_without_test_report() -> None:
    fixture = _Fixture(with_report=False)

    versions = await fixture.service.list_for_investigation(INVESTIGATION_ID)

    assert len(versions) == 1
    assert versions[0].version.id == fixture.version.id
    assert versions[0].test_reports == ()


@pytest.mark.asyncio
async def test_approval_rejects_a_different_source_hash() -> None:
    fixture = _Fixture()

    with pytest.raises(DetectionReviewError) as error:
        await fixture.service.approve(
            fixture.version.id,
            "f" * 64,
            ACTOR,
            "Reviewed source and corpus",
            "No known false positives in the available goodware corpus.",
            "approve-wrong-hash",
        )

    assert error.value.code is DetectionReviewErrorCode.SOURCE_SHA256_MISMATCH


@pytest.mark.asyncio
async def test_approval_requires_identified_actor_reason_and_limitations() -> None:
    fixture = _Fixture()
    with pytest.raises(DetectionReviewError) as actor_error:
        await fixture.service.approve(
            fixture.version.id,
            SOURCE_SHA256,
            " ",
            "Reviewed source",
            "No known limitations.",
            "missing-actor",
        )
    with pytest.raises(DetectionReviewError) as reason_error:
        await fixture.service.approve(
            fixture.version.id,
            SOURCE_SHA256,
            ACTOR,
            " ",
            "No known limitations.",
            "missing-reason",
        )
    with pytest.raises(DetectionReviewError) as limitations_error:
        await fixture.service.approve(
            fixture.version.id,
            SOURCE_SHA256,
            ACTOR,
            "Reviewed source",
            " ",
            "missing-limitations",
        )

    assert actor_error.value.code is DetectionReviewErrorCode.INVALID_ACTOR
    assert reason_error.value.code is DetectionReviewErrorCode.INVALID_REASON
    assert limitations_error.value.code is DetectionReviewErrorCode.LIMITATIONS_REQUIRED


@pytest.mark.asyncio
async def test_noisy_rule_requires_explicit_false_positive_risk_acknowledgement() -> None:
    fixture = _Fixture(noisy=True)

    with pytest.raises(DetectionReviewError) as error:
        await fixture.service.approve(
            fixture.version.id,
            SOURCE_SHA256,
            ACTOR,
            "Reviewed source and corpus",
            "The rule should work well.",
            "approve-noisy-without-risk",
        )

    assert error.value.code is DetectionReviewErrorCode.FALSE_POSITIVE_NOT_ACKNOWLEDGED


@pytest.mark.asyncio
async def test_approval_is_append_only_audited_and_idempotent_by_correlation_id() -> None:
    fixture = _Fixture(noisy=True)
    limitations = "Known goodware false positive on " + "2" * 64
    first = await fixture.service.approve(
        fixture.version.id,
        SOURCE_SHA256,
        ACTOR,
        "Reviewed exact source and test report",
        limitations,
        "approve-once",
    )
    replay = await fixture.service.approve(
        fixture.version.id,
        SOURCE_SHA256,
        ACTOR,
        "Reviewed exact source and test report",
        limitations,
        "approve-once",
    )

    assert first.decision_type is AnalystDecisionType.DETECTION_APPROVE
    assert first is replay
    assert first.details["source_sha256"] == SOURCE_SHA256
    assert first.details["known_limitations"] == limitations
    assert first.details["test_report"]["observation_id"] == str(fixture.observations[0].id)
    assert first.details["base_publication"]["artifact_id"] == str(fixture.publication.id)
    assert len(fixture.uow.analyst_decisions.items) == 1
    assert fixture.version.status is DetectionCandidateStatus.TESTED


@pytest.mark.asyncio
async def test_rejected_test_version_cannot_be_approved() -> None:
    fixture = _Fixture()
    fixture.version = DetectionCandidateVersion(
        id=fixture.version.id,
        investigation_id=INVESTIGATION_ID,
        candidate_id=fixture.version.candidate_id,
        source=SOURCE,
        source_sha256=SOURCE_SHA256,
        motif_provenance=(),
        status=DetectionCandidateStatus.REJECTED,
    )
    fixture.uow.detection_candidates.version = fixture.version
    fixture.observations = (fixture._report(noisy=False),)
    fixture.uow.evidence_observations.items = list(fixture.observations)

    with pytest.raises(DetectionReviewError) as error:
        await fixture.service.approve(
            fixture.version.id,
            SOURCE_SHA256,
            ACTOR,
            "Reviewed rejected version",
            "Known limitations reviewed.",
            "approve-rejected",
        )

    assert error.value.code is DetectionReviewErrorCode.VERSION_REJECTED_BY_TEST
