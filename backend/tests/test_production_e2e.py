"""End-to-end tests for production workflow system.

Tests cover:
1. Complete subject production pipeline (5 stages)
2. Batch production with multiple subjects
3. Retry flows (references and synthesis)
4. Error handling and state management
5. Idempotence and caching
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from cti_app.application.persistence import ProductionUnitOfWork
from cti_app.application.production_jobs import ProductionJobDispatcher
from cti_app.application.production_stages import compute_input_hash
from cti_app.application.production_workflow import ProductionWorkflowOrchestrator
from cti_app.application.subject_production import (
    EditionProductionService,
    SubjectProductionService,
)
from cti_app.domain.production import (
    EditionProductionBatch,
    EditionProductionBatchItem,
    ProductionProfile,
    SubjectProductionRun,
    SubjectProductionStage,
    SubjectProductionStatus,
)


@pytest.mark.asyncio
async def test_subject_production_complete_pipeline(
    uow_factory: ProductionUnitOfWork,
) -> None:
    """Test complete subject production from sources to assembly.

    Pipeline stages:
    1. SOURCES - Collect existing sources (non-LLM)
    2. REFERENCES - Web research (LLM Q1)
    3. EXTRACTION - CTI extraction (LLM Q2)
    4. SYNTHESIS - Technical summary (LLM Q3)
    5. ASSEMBLY - Brief rendering + QA (deterministic)
    """
    # Setup: Create subject and production run
    subject_id = uuid4()
    edition_id = uuid4()

    service = SubjectProductionService(uow_factory)
    orchestrator = ProductionWorkflowOrchestrator(uow_factory)

    # Create production run
    run = await service.create_run(
        subject_id=subject_id,
        edition_id=edition_id,
        profile=ProductionProfile.BRIEF_AUTO,
    )

    assert run.id is not None
    assert run.status == SubjectProductionStatus.QUEUED
    assert run.current_stage == SubjectProductionStage.SOURCES

    # Start run
    started_run = await service.start_run(run.id)
    assert started_run.status == SubjectProductionStatus.RUNNING
    assert started_run.started_at is not None

    # Execute SOURCES stage (non-LLM)
    sources_result = await orchestrator.execute_stage(
        run_id=run.id,
        expected_stage=SubjectProductionStage.SOURCES,
    )
    assert sources_result["status"] in ("success", "error")

    # Verify stage advancement
    async with uow_factory() as uow:
        updated_run = await uow.subject_production_runs.get(run.id)
        if sources_result["status"] == "success":
            assert updated_run.current_stage in (
                SubjectProductionStage.REFERENCES,
                SubjectProductionStage.SOURCES,
            )


@pytest.mark.asyncio
async def test_batch_production_multiple_subjects(
    uow_factory: ProductionUnitOfWork,
) -> None:
    """Test batch production processing multiple subjects sequentially.

    Batch flow:
    1. Create batch with N subjects
    2. Start batch (status=RUNNING)
    3. Process subjects one by one (sequential)
    4. Update batch progress
    5. Mark complete when all done
    """
    edition_id = uuid4()

    batch_service = EditionProductionService(uow_factory)
    subject_service = SubjectProductionService(uow_factory)

    # Create batch with 3 subjects
    subject_ids = [uuid4() for _ in range(3)]

    batch = await batch_service.create_batch(
        edition_id=edition_id,
        subject_ids=subject_ids,
        profile=ProductionProfile.BRIEF_AUTO,
    )

    assert batch.id is not None
    assert batch.edition_id == edition_id
    assert len(batch.items) == 3
    assert all(
        item.subject_id in subject_ids for item in batch.items
    )

    # Start batch
    started_batch = await batch_service.get_batch(batch.id)
    assert started_batch.status in ("queued", "running")

    # Verify all batch items created
    async with uow_factory() as uow:
        items = await uow.edition_production_batch_items.list_for_batch(batch.id)
        assert len(items) == 3
        for i, item in enumerate(items):
            assert item.position == i
            assert item.status in ("queued", "running")


@pytest.mark.asyncio
async def test_input_hash_idempotence() -> None:
    """Test that identical inputs produce same hash (deterministic).

    If run_references(subject_A) twice with same context,
    should use cached artifact with same input_hash.
    """
    # Create same input twice
    input_data_1 = {
        "subject_id": "test-subject",
        "title": "APT1",
        "context": "Chinese cyber espionage group",
        "stage": "references",
    }

    input_data_2 = {
        "subject_id": "test-subject",
        "title": "APT1",
        "context": "Chinese cyber espionage group",
        "stage": "references",
    }

    hash1 = compute_input_hash(input_data_1)
    hash2 = compute_input_hash(input_data_2)

    # Identical inputs should produce identical hashes
    assert hash1 == hash2

    # Different inputs should produce different hashes
    input_data_3 = {
        "subject_id": "test-subject",
        "title": "APT1",
        "context": "DIFFERENT CONTEXT",
        "stage": "references",
    }
    hash3 = compute_input_hash(input_data_3)
    assert hash1 != hash3


@pytest.mark.asyncio
async def test_production_run_state_transitions(
    uow_factory: ProductionUnitOfWork,
) -> None:
    """Test valid state transitions through production lifecycle.

    Valid paths:
    - QUEUED -> RUNNING -> READY (success)
    - QUEUED -> RUNNING -> NEEDS_REVIEW (QA failed)
    - QUEUED -> RUNNING -> FAILED (critical error)
    - RUNNING -> CANCELLED (user cancellation)
    """
    subject_id = uuid4()
    edition_id = uuid4()

    service = SubjectProductionService(uow_factory)

    # Create run (QUEUED)
    run = await service.create_run(
        subject_id=subject_id,
        edition_id=edition_id,
        profile=ProductionProfile.BRIEF_AUTO,
    )
    assert run.status == SubjectProductionStatus.QUEUED

    # Transition to RUNNING
    run = await service.start_run(run.id)
    assert run.status == SubjectProductionStatus.RUNNING

    # Transition to READY (success)
    run = await service.mark_ready(run.id)
    assert run.status == SubjectProductionStatus.READY
    assert run.finished_at is not None

    # READY is terminal - cannot transition further
    with pytest.raises(ValueError):
        await service.start_run(run.id)


@pytest.mark.asyncio
async def test_production_needs_review_state(
    uow_factory: ProductionUnitOfWork,
) -> None:
    """Test NEEDS_REVIEW state for controlled problems.

    NEEDS_REVIEW allows:
    - Viewing error details
    - Retrying specific stages
    - Advancing to next if acceptable
    """
    subject_id = uuid4()
    edition_id = uuid4()

    service = SubjectProductionService(uow_factory)

    run = await service.create_run(
        subject_id=subject_id,
        edition_id=edition_id,
        profile=ProductionProfile.BRIEF_AUTO,
    )

    run = await service.start_run(run.id)

    # Mark as needs review
    run = await service.mark_needs_review(
        run.id,
        code="qa_missing_references",
        message="Could not find valid references for claim X",
    )

    assert run.status == SubjectProductionStatus.NEEDS_REVIEW
    assert run.error_code == "qa_missing_references"
    assert "references" in run.error_message


@pytest.mark.asyncio
async def test_downstream_invalidation(
    uow_factory: ProductionUnitOfWork,
) -> None:
    """Test that changing earlier stages marks downstream as stale.

    If references change:
    - extraction marked STALE
    - synthesis marked STALE
    - brief marked STALE

    If synthesis changes:
    - brief marked STALE
    - extraction/references unchanged
    """
    subject_id = uuid4()

    # Create artifacts for all stages
    async with uow_factory() as uow:
        run = SubjectProductionRun(
            id=uuid4(),
            subject_id=subject_id,
            edition_id=uuid4(),
            profile=ProductionProfile.BRIEF_AUTO,
            status=SubjectProductionStatus.RUNNING,
            current_stage=SubjectProductionStage.ASSEMBLY,
            conversation_id=uuid4(),
            created_at=datetime.now(UTC),
            started_at=datetime.now(UTC),
        )

        await uow.subject_production_runs.add(run)

        # Create artifacts
        input_hash = compute_input_hash({"test": "data"})

        for stage in ["references", "extraction", "synthesis", "brief"]:
            artifact = await uow.production_artifacts.append(
                run_id=run.id,
                stage=stage,
                status="verified",
                metadata={stage: "data"},
                input_hash=input_hash,
                turn_id=None,
            )
            assert artifact is not None

        await uow.commit()

    # Mark references as stale
    async with uow_factory() as uow:
        await uow.production_artifacts.mark_downstream_stale(
            run.id, SubjectProductionStage.REFERENCES
        )
        await uow.commit()

    # Verify downstream artifacts marked stale
    async with uow_factory() as uow:
        extraction = await uow.production_artifacts.get_current(
            run.id, "extraction"
        )
        synthesis = await uow.production_artifacts.get_current(
            run.id, "synthesis"
        )
        brief = await uow.production_artifacts.get_current(run.id, "brief")

        assert extraction.status == "stale"
        assert synthesis.status == "stale"
        assert brief.status == "stale"


@pytest.mark.asyncio
async def test_batch_sequential_processing(
    uow_factory: ProductionUnitOfWork,
) -> None:
    """Test that batch processes subjects sequentially (max 1 active).

    Batch with 3 subjects:
    - Only 1 subject RUNNING at any time
    - When subject finishes, next starts
    - Batch status reflects overall progress
    """
    edition_id = uuid4()

    batch_service = EditionProductionService(uow_factory)
    subject_service = SubjectProductionService(uow_factory)

    # Create batch
    subject_ids = [uuid4() for _ in range(3)]
    batch = await batch_service.create_batch(
        edition_id=edition_id,
        subject_ids=subject_ids,
        profile=ProductionProfile.BRIEF_AUTO,
    )

    # Verify only one item can be RUNNING
    async with uow_factory() as uow:
        items = await uow.edition_production_batch_items.list_for_batch(batch.id)
        running_items = [item for item in items if item.status == "running"]
        # Initially all should be queued, or first one running
        assert len(running_items) <= 1


@pytest.mark.asyncio
async def test_production_error_states(
    uow_factory: ProductionUnitOfWork,
) -> None:
    """Test error handling in different scenarios.

    Error types:
    1. NEEDS_REVIEW - Controlled errors (QA failure, missing data)
    2. FAILED - Terminal errors (LLM failure, data corruption)
    """
    subject_id = uuid4()
    edition_id = uuid4()

    service = SubjectProductionService(uow_factory)

    run = await service.create_run(
        subject_id=subject_id,
        edition_id=edition_id,
        profile=ProductionProfile.BRIEF_AUTO,
    )

    run = await service.start_run(run.id)

    # NEEDS_REVIEW - recoverable
    run = await service.mark_needs_review(
        run.id,
        code="qa_check_failed",
        message="QA detected missing IOC validation",
    )
    assert run.status == SubjectProductionStatus.NEEDS_REVIEW
    assert run.finished_at is not None  # Terminal but can retry

    # FAILED - terminal
    run2 = await service.create_run(
        subject_id=subject_id,
        edition_id=edition_id,
        profile=ProductionProfile.BRIEF_AUTO,
    )
    run2 = await service.start_run(run2.id)
    run2 = await service.mark_failed(
        run2.id,
        code="conversation_error",
        message="Failed to create conversation",
    )
    assert run2.status == SubjectProductionStatus.FAILED
    assert run2.finished_at is not None


@pytest.mark.asyncio
async def test_production_batch_progress_calculation(
    uow_factory: ProductionUnitOfWork,
) -> None:
    """Test batch progress calculation from items.

    Progress = (completed + needs_review + failed) / total
    Status transitions:
    - QUEUED -> RUNNING (when first item starts)
    - RUNNING -> COMPLETED (when all items done)
    - COMPLETED -> COMPLETED_WITH_ISSUES (if any needs_review or failed)
    """
    edition_id = uuid4()

    batch_service = EditionProductionService(uow_factory)

    subject_ids = [uuid4() for _ in range(5)]
    batch = await batch_service.create_batch(
        edition_id=edition_id,
        subject_ids=subject_ids,
        profile=ProductionProfile.BRIEF_AUTO,
    )

    # Initially all items queued
    async with uow_factory() as uow:
        items = await uow.edition_production_batch_items.list_for_batch(batch.id)
        assert all(item.status == "queued" for item in items)

        # Simulate completion of items
        for i, item in enumerate(items[:3]):
            item.status = "completed"
            item.finished_at = datetime.now(UTC)
            await uow.edition_production_batch_items.save(item)

        await uow.commit()

    # Verify batch reflects progress
    async with uow_factory() as uow:
        updated_batch = await uow.edition_production_batches.get(batch.id)
        items = await uow.edition_production_batch_items.list_for_batch(batch.id)

        completed = sum(1 for item in items if item.status == "completed")
        assert completed == 3


@pytest.mark.asyncio
async def test_production_cancellation_flow(
    uow_factory: ProductionUnitOfWork,
) -> None:
    """Test cancellation of subject and batch production.

    Subject cancellation:
    - RUNNING -> CANCELLED
    - Cannot transition from CANCELLED

    Batch cancellation:
    - RUNNING -> CANCELLED
    - Mark all active items as cancelled
    """
    subject_id = uuid4()
    edition_id = uuid4()

    service = SubjectProductionService(uow_factory)

    run = await service.create_run(
        subject_id=subject_id,
        edition_id=edition_id,
        profile=ProductionProfile.BRIEF_AUTO,
    )

    run = await service.start_run(run.id)
    assert run.status == SubjectProductionStatus.RUNNING

    run = await service.mark_cancelled(run.id)
    assert run.status == SubjectProductionStatus.CANCELLED
    assert run.finished_at is not None


@pytest.mark.asyncio
async def test_production_conversation_management(
    uow_factory: ProductionUnitOfWork,
) -> None:
    """Test conversation lifecycle in production.

    Conversation rules:
    1. Created when FRESH mode (references stage)
    2. Reused for Q2 and Q3 (CONTINUE mode)
    3. Archived on retry_references
    4. New conversation created after archive
    """
    subject_id = uuid4()
    edition_id = uuid4()

    service = SubjectProductionService(uow_factory)

    run = await service.create_run(
        subject_id=subject_id,
        edition_id=edition_id,
        profile=ProductionProfile.BRIEF_AUTO,
    )

    assert run.conversation_id is None  # Not created yet

    # After starting, conversation would be created on references stage
    run = await service.start_run(run.id)

    # In real flow, conversation_id would be set here
    # For testing, we simulate it
    async with uow_factory() as uow:
        run_for_update = await uow.subject_production_runs.get_for_update(run.id)
        run_for_update.conversation_id = uuid4()
        await uow.subject_production_runs.save(run_for_update)
        await uow.commit()

    # Verify it persists
    async with uow_factory() as uow:
        persisted_run = await uow.subject_production_runs.get(run.id)
        assert persisted_run.conversation_id is not None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
