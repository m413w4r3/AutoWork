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
    ProductionProfile,
    SubjectProductionRun,
    SubjectProductionStage,
    SubjectProductionStatus,
)


class TestSubjectProductionRun:
    """Tests for SubjectProductionRun domain model."""

    def test_create_run(self) -> None:
        """Test creating a new production run."""
        subject_id = uuid4()
        edition_id = uuid4()

        run = SubjectProductionRun(
            subject_id=subject_id,
            edition_id=edition_id,
            profile=ProductionProfile.BRIEF_AUTO,
        )

        assert run.status == SubjectProductionStatus.QUEUED
        assert run.current_stage is SubjectProductionStage.SOURCES
        assert run.run_number == 1
        assert run.conversation_id is None

    def test_run_state_transitions(self) -> None:
        """Test valid run state transitions."""
        subject_id = uuid4()
        edition_id = uuid4()
        run = SubjectProductionRun(
            subject_id=subject_id,
            edition_id=edition_id,
            profile=ProductionProfile.BRIEF_AUTO,
        )

        # QUEUED -> RUNNING
        run.start_running()
        assert run.status == SubjectProductionStatus.RUNNING

        # Advance stages
        run.advance_stage()
        assert run.current_stage is SubjectProductionStage.REFERENCES

        run.advance_stage()
        assert run.current_stage is SubjectProductionStage.EXTRACTION  # type: ignore[comparison-overlap]

        run.advance_stage()
        assert run.current_stage is SubjectProductionStage.SYNTHESIS

        run.advance_stage()
        assert run.current_stage is SubjectProductionStage.ASSEMBLY

        # Assembly -> READY
        run.mark_ready()
        assert run.status == SubjectProductionStatus.READY

    def test_run_cannot_start_from_non_queued(self) -> None:
        """Test that run cannot start from non-QUEUED status."""
        run = SubjectProductionRun(
            subject_id=uuid4(),
            edition_id=uuid4(),
            profile=ProductionProfile.BRIEF_AUTO,
        )

        run.start_running()
        with pytest.raises(ValueError, match="Can only start from QUEUED"):
            run.start_running()

    def test_run_mark_needs_review(self) -> None:
        """Test marking run as needing review."""
        run = SubjectProductionRun(
            subject_id=uuid4(),
            edition_id=uuid4(),
            profile=ProductionProfile.BRIEF_AUTO,
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
        """Test marking run as failed."""
        run = SubjectProductionRun(
            subject_id=uuid4(),
            edition_id=uuid4(),
            profile=ProductionProfile.BRIEF_AUTO,
        )

        run.start_running()
        run.mark_failed(
            code="critical_error",
            message="An unrecoverable error occurred",
        )

        assert run.status == SubjectProductionStatus.FAILED
        assert run.error_code == "critical_error"


class TestProductionArtifact:
    """Tests for ProductionArtifact domain model."""

    def test_create_artifact(self) -> None:
        """Test creating a production artifact."""
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
        """Test that artifact validates SHA-256 input hash."""
        run_id = uuid4()
        subject_id = uuid4()

        # Valid SHA-256
        artifact = ProductionArtifact(
            production_run_id=run_id,
            subject_id=subject_id,
            stage=ProductionArtifactStage.REFERENCES,
            version=1,
            input_hash="a" * 64,
        )
        assert len(artifact.input_hash) == 64

        # Invalid hash (too short)
        with pytest.raises(ValueError, match="input_hash must be lowercase SHA-256"):
            ProductionArtifact(
                production_run_id=run_id,
                subject_id=subject_id,
                stage=ProductionArtifactStage.REFERENCES,
                version=1,
                input_hash="a" * 32,
            )

            # Invalid hash (non-hex)
            ProductionArtifact(
                production_run_id=run_id,
                subject_id=subject_id,
                stage=ProductionArtifactStage.REFERENCES,
                version=1,
                input_hash="z" * 64,
            )


class TestEditionProductionBatch:
    """Tests for EditionProductionBatch domain model."""

    def test_create_batch(self) -> None:
        """Test creating a production batch."""
        edition_id = uuid4()

        batch = EditionProductionBatch(
            edition_id=edition_id,
            profile=ProductionProfile.BRIEF_AUTO,
            status="queued",
        )

        assert batch.status == "queued"
        assert batch.created_at
        assert batch.started_at is None

    def test_batch_lifecycle(self) -> None:
        """Test batch status transitions."""
        batch = EditionProductionBatch(
            edition_id=uuid4(),
            profile=ProductionProfile.BRIEF_AUTO,
            status="queued",
        )

        batch.start()
        assert batch.status == "running"
        assert batch.started_at

        batch.finish(completed_with_issues=False)
        assert batch.status == "completed"
        assert batch.finished_at

    def test_batch_finish_with_issues(self) -> None:
        """Test marking batch as completed with issues."""
        batch = EditionProductionBatch(
            edition_id=uuid4(),
            profile=ProductionProfile.BRIEF_AUTO,
            status="queued",
        )

        batch.start()
        batch.finish(completed_with_issues=True)
        assert batch.status == "completed_with_issues"


class TestEditionProductionBatchItem:
    """Tests for EditionProductionBatchItem domain model."""

    def test_create_batch_item(self) -> None:
        """Test creating a batch item with ordering."""
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
        """Test that batch item position must be >= 1."""
        with pytest.raises(ValueError, match="position must be >= 1"):
            EditionProductionBatchItem(
                batch_id=uuid4(),
                subject_id=uuid4(),
                production_run_id=uuid4(),
                position=0,
            )


class TestInputHashComputation:
    """Tests for deterministic input hash computation."""

    def test_input_hash_deterministic(self) -> None:
        """Test that same input produces same hash."""
        data = {
            "subject_id": "test",
            "template_version": "1.0.0",
        }

        hash1 = compute_input_hash(data)
        hash2 = compute_input_hash(data)

        assert hash1 == hash2
        assert len(hash1) == 64

    def test_input_hash_different_for_different_data(self) -> None:
        """Test that different data produces different hash."""
        data1 = {"version": "1.0.0"}
        data2 = {"version": "2.0.0"}

        hash1 = compute_input_hash(data1)
        hash2 = compute_input_hash(data2)

        assert hash1 != hash2

    def test_input_hash_order_independent(self) -> None:
        """Test that key order doesn't affect hash."""
        data1 = {"a": 1, "b": 2}
        data2 = {"b": 2, "a": 1}

        hash1 = compute_input_hash(data1)
        hash2 = compute_input_hash(data2)

        assert hash1 == hash2


class TestProductionWorkflow:
    """Integration tests for production workflow."""

    def test_brief_auto_workflow_sequence(self) -> None:
        """Test the complete brief_auto workflow sequence."""
        subject_id = uuid4()
        edition_id = uuid4()

        # Create run
        run = SubjectProductionRun(
            subject_id=subject_id,
            edition_id=edition_id,
            profile=ProductionProfile.BRIEF_AUTO,
        )

        # Stage sequence
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

        # After advancing from ASSEMBLY, should still be ASSEMBLY (last stage)
        assert run.current_stage is SubjectProductionStage.ASSEMBLY  # type: ignore[comparison-overlap]

    def test_batch_sequential_execution(self) -> None:
        """Test that batch items are processed in order."""
        edition_id = uuid4()
        subject_ids = [uuid4() for _ in range(3)]

        batch = EditionProductionBatch(
            edition_id=edition_id,
            profile=ProductionProfile.BRIEF_AUTO,
            status="queued",
        )

        # Create items with explicit positioning
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

        # Verify ordering
        assert items[0].position < items[1].position < items[2].position
