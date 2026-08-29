"""Tests for production workflow domain and services."""

from uuid import uuid4

import pytest

from cti_app.application.production_stages import compute_input_hash
from cti_app.domain.production import (
    EditionProductionBatch,
    EditionProductionBatchItem,
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionArtifactStatus,
    SubjectProductionRun,
    SubjectProductionStage,
    SubjectProductionStatus,
)


class TestSubjectProductionRun:
    def test_create_run(self) -> None:
        subject_id = uuid4()
        edition_id = uuid4()

        run = SubjectProductionRun(
            subject_id=subject_id,
            edition_id=edition_id,
        )

        assert run.status == SubjectProductionStatus.QUEUED
        assert run.current_stage is SubjectProductionStage.SOURCES
        assert run.run_number == 1
        assert run.references_conversation_id is None
        assert run.synthesis_conversation_id is None

    def test_run_state_transitions(self) -> None:
        subject_id = uuid4()
        edition_id = uuid4()
        run = SubjectProductionRun(
            subject_id=subject_id,
            edition_id=edition_id,
        )

        run.start_running()
        assert run.status == SubjectProductionStatus.RUNNING

        run.advance_stage()
        assert run.current_stage is SubjectProductionStage.REFERENCES

        run.advance_stage()
        assert run.current_stage is SubjectProductionStage.EXTRACTION  # type: ignore[comparison-overlap]

        run.advance_stage()
        assert run.current_stage is SubjectProductionStage.SYNTHESIS

        run.advance_stage()
        assert run.current_stage is SubjectProductionStage.ASSEMBLY

        run.mark_ready()
        assert run.status == SubjectProductionStatus.READY

    def test_run_cannot_start_from_non_queued(self) -> None:
        run = SubjectProductionRun(
            subject_id=uuid4(),
            edition_id=uuid4(),
        )

        run.start_running()
        with pytest.raises(ValueError, match="Can only start from QUEUED"):
            run.start_running()

    def test_run_mark_needs_review(self) -> None:
        run = SubjectProductionRun(
            subject_id=uuid4(),
            edition_id=uuid4(),
        )

        run.start_running()
        run.mark_needs_review(
            code="qa_failed",
            message="QA checks did not pass",
        )

        assert run.status == SubjectProductionStatus.NEEDS_REVIEW
        assert run.error_code == "qa_failed"
        assert run.error_message is not None
        assert "QA" in run.error_message

    def test_run_mark_failed(self) -> None:
        run = SubjectProductionRun(
            subject_id=uuid4(),
            edition_id=uuid4(),
        )

        run.start_running()
        run.mark_failed(
            code="critical_error",
            message="An unrecoverable error occurred",
        )

        assert run.status == SubjectProductionStatus.FAILED
        assert run.error_code == "critical_error"


class TestProductionArtifact:
    def test_create_artifact(self) -> None:
        run_id = uuid4()
        subject_id = uuid4()
        test_hash = "a" * 64

        artifact = ProductionArtifact(
            production_run_id=run_id,
            subject_id=subject_id,
            stage=ProductionArtifactStage.REFERENCES,
            version=1,
            input_hash=test_hash,
        )

        assert artifact.status == ProductionArtifactStatus.VERIFIED
        assert artifact.canonical_blob_id is None
        assert artifact.created_at

    def test_artifact_input_hash_validation(self) -> None:
        run_id = uuid4()
        subject_id = uuid4()

        artifact = ProductionArtifact(
            production_run_id=run_id,
            subject_id=subject_id,
            stage=ProductionArtifactStage.REFERENCES,
            version=1,
            input_hash="a" * 64,
        )
        assert len(artifact.input_hash) == 64

        with pytest.raises(ValueError, match="input_hash must be lowercase SHA-256"):
            ProductionArtifact(
                production_run_id=run_id,
                subject_id=subject_id,
                stage=ProductionArtifactStage.REFERENCES,
                version=1,
                input_hash="a" * 32,
            )

            ProductionArtifact(
                production_run_id=run_id,
                subject_id=subject_id,
                stage=ProductionArtifactStage.REFERENCES,
                version=1,
                input_hash="z" * 64,
            )


class TestEditionProductionBatch:
    def test_create_batch(self) -> None:
        edition_id = uuid4()

        batch = EditionProductionBatch(
            edition_id=edition_id,
            status="queued",
        )

        assert batch.status == "queued"
        assert batch.created_at
        assert batch.started_at is None

    def test_batch_lifecycle(self) -> None:
        batch = EditionProductionBatch(
            edition_id=uuid4(),
            status="queued",
        )

        batch.start()
        assert batch.status == "running"
        assert batch.started_at

        batch.finish(completed_with_issues=False)
        assert batch.status == "completed"
        assert batch.finished_at

    def test_batch_finish_with_issues(self) -> None:
        batch = EditionProductionBatch(
            edition_id=uuid4(),
            status="queued",
        )

        batch.start()
        batch.finish(completed_with_issues=True)
        assert batch.status == "completed_with_issues"


class TestEditionProductionBatchItem:
    def test_create_batch_item(self) -> None:
        batch_id = uuid4()
        subject_id = uuid4()
        run_id = uuid4()

        item = EditionProductionBatchItem(
            batch_id=batch_id,
            subject_id=subject_id,
            production_run_id=run_id,
            position=1,
        )

        assert item.position == 1
        assert item.batch_id == batch_id

    def test_batch_item_position_must_be_positive(self) -> None:
        with pytest.raises(ValueError, match="position must be >= 1"):
            EditionProductionBatchItem(
                batch_id=uuid4(),
                subject_id=uuid4(),
                production_run_id=uuid4(),
                position=0,
            )


class TestInputHashComputation:
    def test_input_hash_deterministic(self) -> None:
        data = {
            "subject_id": "test",
            "template_version": "1.0.0",
        }

        hash1 = compute_input_hash(data)
        hash2 = compute_input_hash(data)

        assert hash1 == hash2
        assert len(hash1) == 64

    def test_input_hash_different_for_different_data(self) -> None:
        data1 = {"version": "1.0.0"}
        data2 = {"version": "2.0.0"}

        hash1 = compute_input_hash(data1)
        hash2 = compute_input_hash(data2)

        assert hash1 != hash2

    def test_input_hash_order_independent(self) -> None:
        data1 = {"a": 1, "b": 2}
        data2 = {"b": 2, "a": 1}

        hash1 = compute_input_hash(data1)
        hash2 = compute_input_hash(data2)

        assert hash1 == hash2


class TestProductionWorkflow:
    def test_article_workflow_sequence(self) -> None:
        subject_id = uuid4()
        edition_id = uuid4()

        run = SubjectProductionRun(
            subject_id=subject_id,
            edition_id=edition_id,
        )

        assert run.current_stage is SubjectProductionStage.SOURCES

        stages = [
            SubjectProductionStage.SOURCES,
            SubjectProductionStage.REFERENCES,
            SubjectProductionStage.EXTRACTION,
            SubjectProductionStage.SYNTHESIS,
            SubjectProductionStage.ASSEMBLY,
        ]

        for stage in stages:
            assert run.current_stage == stage
            run.advance_stage()

        # advance_stage() is a no-op past the last stage
        assert run.current_stage is SubjectProductionStage.ASSEMBLY  # type: ignore[comparison-overlap]

    def test_batch_sequential_execution(self) -> None:
        edition_id = uuid4()
        subject_ids = [uuid4() for _ in range(3)]

        batch = EditionProductionBatch(
            edition_id=edition_id,
            status="queued",
        )

        items = [
            EditionProductionBatchItem(
                batch_id=batch.id,
                subject_id=subject_ids[0],
                production_run_id=uuid4(),
                position=1,
            ),
            EditionProductionBatchItem(
                batch_id=batch.id,
                subject_id=subject_ids[1],
                production_run_id=uuid4(),
                position=2,
            ),
            EditionProductionBatchItem(
                batch_id=batch.id,
                subject_id=subject_ids[2],
                production_run_id=uuid4(),
                position=3,
            ),
        ]

        assert items[0].position < items[1].position < items[2].position
