"""Unit tests for production workflow system (without database).

Tests domain models, state machines, and logic without database dependencies.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from cti_app.application.subject_production import SubjectProductionService
from cti_app.domain.production import (
    AnalystInvestigation,
    AnalystInvestigationStatus,
    EditionProductionBatch,
    EditionProductionBatchItem,
    LoopBudget,
    LoopBudgetCategory,
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionArtifactStatus,
    SubjectProductionRun,
    SubjectProductionStage,
    SubjectProductionStatus,
)


class TestSubjectProductionRunStates:
    def test_create_run_initial_state(self) -> None:
        run = SubjectProductionRun(
            subject_id=uuid4(),
            edition_id=uuid4(),
        )

        assert run.status == SubjectProductionStatus.QUEUED
        assert run.current_stage is SubjectProductionStage.SOURCES
        assert run.started_at is None
        assert run.finished_at is None

    def test_start_run_transitions_to_running(self) -> None:
        run = SubjectProductionRun(
            subject_id=uuid4(),
            edition_id=uuid4(),
        )

        run.start_running(now=datetime.now(UTC))

        assert run.status == SubjectProductionStatus.RUNNING
        assert run.started_at is not None

    def test_advance_stage_moves_through_pipeline(self) -> None:
        run = SubjectProductionRun(
            subject_id=uuid4(),
            edition_id=uuid4(),
        )

        assert run.current_stage is SubjectProductionStage.SOURCES

        run.advance_stage(now=datetime.now(UTC))
        assert run.current_stage is SubjectProductionStage.REFERENCES  # type: ignore[comparison-overlap]

        run.advance_stage(now=datetime.now(UTC))
        assert run.current_stage is SubjectProductionStage.EXTRACTION

        run.advance_stage(now=datetime.now(UTC))
        assert run.current_stage is SubjectProductionStage.SYNTHESIS

        run.advance_stage(now=datetime.now(UTC))
        assert run.current_stage is SubjectProductionStage.ASSEMBLY

    def test_mark_ready_terminates_run(self) -> None:
        run = SubjectProductionRun(
            subject_id=uuid4(),
            edition_id=uuid4(),
        )

        run.start_running(now=datetime.now(UTC))
        run.mark_ready(now=datetime.now(UTC))

        assert run.status == SubjectProductionStatus.READY
        assert run.finished_at is not None

    def test_mark_needs_review_allows_recovery(self) -> None:
        run = SubjectProductionRun(
            subject_id=uuid4(),
            edition_id=uuid4(),
        )

        run.start_running(now=datetime.now(UTC))
        run.mark_needs_review(
            code="qa_check_failed",
            message="QA validation failed",
            now=datetime.now(UTC),
        )

        assert run.status == SubjectProductionStatus.NEEDS_REVIEW
        assert run.error_code == "qa_check_failed"
        assert run.error_message is not None
        assert "QA validation failed" in run.error_message
        assert run.finished_at is not None

    def test_mark_failed_terminal_state(self) -> None:
        run = SubjectProductionRun(
            subject_id=uuid4(),
            edition_id=uuid4(),
        )

        run.start_running(now=datetime.now(UTC))
        run.mark_failed(
            code="conversation_error",
            message="Failed to create conversation",
            now=datetime.now(UTC),
        )

        assert run.status == SubjectProductionStatus.FAILED
        assert run.error_code == "conversation_error"
        assert run.finished_at is not None

    def test_mark_cancelled_by_user(self) -> None:
        run = SubjectProductionRun(
            subject_id=uuid4(),
            edition_id=uuid4(),
        )

        run.start_running(now=datetime.now(UTC))
        run.mark_cancelled(now=datetime.now(UTC))

        assert run.status == SubjectProductionStatus.CANCELLED
        assert run.finished_at is not None

    def test_mark_cancelled_is_idempotent_and_terminal(self) -> None:
        run = SubjectProductionRun(
            subject_id=uuid4(),
            edition_id=uuid4(),
        )
        run.start_running(now=datetime.now(UTC))
        run.mark_cancelled(now=datetime.now(UTC))
        finished_at = run.finished_at
        version = run.version

        run.mark_cancelled(now=datetime.now(UTC))

        assert run.finished_at == finished_at
        assert run.version == version
        with pytest.raises(ValueError, match="production_run_cancelled"):
            run.retry_from_stage(SubjectProductionStage.REFERENCES)
        with pytest.raises(ValueError, match="production_run_cancelled"):
            run.mark_ready()

    def test_cannot_transition_from_terminal_state(self) -> None:
        run = SubjectProductionRun(
            subject_id=uuid4(),
            edition_id=uuid4(),
        )

        run.start_running(now=datetime.now(UTC))
        run.mark_ready(now=datetime.now(UTC))

        with pytest.raises(ValueError):
            run.start_running(now=datetime.now(UTC))


class TestEditionProductionBatch:
    def test_create_batch_queued_status(self) -> None:
        edition_id = uuid4()

        batch = EditionProductionBatch(
            id=uuid4(),
            edition_id=edition_id,
            status="queued",
        )

        assert batch.edition_id == edition_id
        assert batch.status == "queued"
        assert batch.started_at is None

    def test_batch_start_transitions_to_running(self) -> None:
        edition_id = uuid4()

        batch = EditionProductionBatch(
            id=uuid4(),
            edition_id=edition_id,
            status="queued",
        )

        batch.start(now=datetime.now(UTC))

        assert batch.status == "running"
        assert batch.started_at is not None

    def test_batch_finish_marks_complete(self) -> None:
        batch = EditionProductionBatch(
            id=uuid4(),
            edition_id=uuid4(),
            status="running",
            started_at=datetime.now(UTC),
        )

        batch.finish(now=datetime.now(UTC))

        assert batch.status == "completed"
        assert batch.finished_at is not None

    def test_batch_finish_with_issues(self) -> None:
        batch = EditionProductionBatch(
            id=uuid4(),
            edition_id=uuid4(),
            status="running",
            started_at=datetime.now(UTC),
        )

        batch.finish(completed_with_issues=True, now=datetime.now(UTC))

        assert batch.status == "completed_with_issues"
        assert batch.finished_at is not None


class TestAnalystInvestigation:
    def test_requires_verified_synthesis_and_exhausts_without_members(self) -> None:
        artifact = ProductionArtifact(
            production_run_id=uuid4(),
            subject_id=uuid4(),
            stage=ProductionArtifactStage.SYNTHESIS,
            version=1,
            input_hash="a" * 64,
            status=ProductionArtifactStatus.VERIFIED,
        )
        investigation = AnalystInvestigation.from_verified_synthesis(
            synthesis=artifact,
            budget=LoopBudget(
                max_pivot_runs=1, max_hits_acquired=2, max_new_samples=3, max_vt_read_units=4
            ),
        )

        investigation.start(now=datetime.now(UTC))
        investigation.consume_budget(LoopBudgetCategory.PIVOT_RUNS, now=datetime.now(UTC))
        with pytest.raises(ValueError, match="Budget exceeded"):
            investigation.consume_budget(LoopBudgetCategory.PIVOT_RUNS, now=datetime.now(UTC))
        investigation.finish_cycle(validated_new_members=0, now=datetime.now(UTC))

        assert investigation.status is AnalystInvestigationStatus.EXHAUSTED


class TestProductionArtifactValidation:
    def test_artifact_version_must_be_positive(self) -> None:
        from cti_app.domain.production import ProductionArtifact

        with pytest.raises(ValueError, match="version must be >= 1"):
            ProductionArtifact(
                production_run_id=uuid4(),
                subject_id=uuid4(),
                stage=ProductionArtifactStage.REFERENCES,
                version=0,
                input_hash="a" * 64,
            )

    def test_artifact_input_hash_validation(self) -> None:
        from cti_app.domain.production import ProductionArtifact

        with pytest.raises(ValueError, match="input_hash must be lowercase SHA-256"):
            ProductionArtifact(
                production_run_id=uuid4(),
                subject_id=uuid4(),
                stage=ProductionArtifactStage.REFERENCES,
                version=1,
                input_hash="invalid",
            )

    def test_artifact_with_valid_sha256(self) -> None:
        from cti_app.domain.production import ProductionArtifact

        valid_hash = "a" * 64

        artifact = ProductionArtifact(
            production_run_id=uuid4(),
            subject_id=uuid4(),
            stage=ProductionArtifactStage.REFERENCES,
            version=1,
            input_hash=valid_hash,
        )

        assert artifact.input_hash == valid_hash


@pytest.mark.asyncio
async def test_retry_from_extraction_stales_downstream_artifacts_only() -> None:
    run = SubjectProductionRun(
        subject_id=uuid4(),
        edition_id=uuid4(),
        current_stage=SubjectProductionStage.ASSEMBLY,
    )
    run.start_running()
    run.mark_ready()
    artifacts = [
        ProductionArtifact(
            production_run_id=run.id,
            subject_id=run.subject_id,
            stage=stage,
            version=1,
            input_hash="a" * 64,
        )
        for stage in (
            ProductionArtifactStage.REFERENCES,
            ProductionArtifactStage.EXTRACTION,
            ProductionArtifactStage.SYNTHESIS,
            ProductionArtifactStage.PUBLICATION,
        )
    ]

    class Runs:
        async def get(self, run_id: object) -> SubjectProductionRun | None:
            return run if run_id == run.id else None

        async def get_for_update(self, run_id: object) -> SubjectProductionRun | None:
            return await self.get(run_id)

        async def save(self, saved: SubjectProductionRun) -> None:
            assert saved is run

    class Artifacts:
        async def get_current(self, run_id: object, stage: str) -> ProductionArtifact | None:
            if run_id != run.id:
                return None
            return next(
                (
                    artifact
                    for artifact in artifacts
                    if artifact.stage.value == stage
                    and artifact.status is ProductionArtifactStatus.VERIFIED
                ),
                None,
            )

        async def mark_from_stage_stale(self, run_id: object, stage: str) -> list[str]:
            assert run_id == run.id
            assert stage == SubjectProductionStage.EXTRACTION.value
            affected = ["extraction", "synthesis", "publication"]
            for artifact in artifacts:
                if artifact.stage.value in affected:
                    artifact.status = ProductionArtifactStatus.STALE
            return affected

    class Uow:
        subject_production_runs = Runs()
        production_artifacts = Artifacts()

        async def __aenter__(self) -> Uow:
            return self

        async def __aexit__(self, *args: object) -> None:
            del args

        async def commit(self) -> None:
            return None

    references = next(
        artifact for artifact in artifacts if artifact.stage is ProductionArtifactStage.REFERENCES
    )
    artifact_ids = {artifact.stage: artifact.id for artifact in artifacts}
    result = await SubjectProductionService(lambda: Uow()).retry_from_stage(
        run.id, SubjectProductionStage.EXTRACTION
    )

    assert result.staled_artifacts == ["extraction", "synthesis", "publication"]
    assert run.current_stage is SubjectProductionStage.EXTRACTION
    assert run.pipeline_generation == 1
    assert references.status is ProductionArtifactStatus.VERIFIED
    assert references.id == artifact_ids[ProductionArtifactStage.REFERENCES]
    assert {
        artifact.stage
        for artifact in artifacts
        if artifact.status is ProductionArtifactStatus.STALE
    } == {
        ProductionArtifactStage.EXTRACTION,
        ProductionArtifactStage.SYNTHESIS,
        ProductionArtifactStage.PUBLICATION,
    }
    assert {artifact.stage: artifact.id for artifact in artifacts} == artifact_ids


class TestBatchSequentialProcessing:
    def test_batch_item_creation(self) -> None:
        batch_id = uuid4()
        subject_ids = [uuid4() for _ in range(3)]

        items = [
            EditionProductionBatchItem(
                id=uuid4(),
                batch_id=batch_id,
                subject_id=sid,
                production_run_id=uuid4(),
                position=i + 1,
            )
            for i, sid in enumerate(subject_ids)
        ]

        assert len(items) == 3
        assert all(item.batch_id == batch_id for item in items)
        assert [item.position for item in items] == [1, 2, 3]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
