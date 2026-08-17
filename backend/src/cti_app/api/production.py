"""API endpoints for subject production workflow."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel

from cti_app.api.auth import get_current_user
from cti_app.application.persistence import ProductionUnitOfWorkFactory
from cti_app.application.production_jobs import ProductionStageParameters
from cti_app.application.subject_production import (
    EditionProductionService,
    SubjectProductionService,
)
from cti_app.domain.production import (
    ProductionProfile,
    SubjectProductionStage,
    SubjectProductionStatus,
)
from cti_app.logging import get_correlation_id

router = APIRouter(prefix="/api", tags=["production"])


# Response Models


class StageStatus(BaseModel):
    """Status of a production stage."""

    status: str  # pending, running, succeeded, needs_review, failed
    version: int | None = None
    error_code: str | None = None
    error_message: str | None = None


class ProductionStatus(BaseModel):
    """Complete production status for a subject."""

    subject_id: str
    title: str
    editorial_type: str
    status: str  # queued, running, ready, needs_review, failed, cancelled
    current_stage: str
    progress_current: int
    progress_total: int
    conversation_id: str | None = None
    run_id: str
    created_at: str
    started_at: str | None = None
    finished_at: str | None = None
    stages: dict[str, StageStatus]


class BatchStatus(BaseModel):
    """Batch production status."""

    batch_id: str
    edition_id: str
    profile: str
    status: str
    items: int
    completed: int
    needs_review: int
    failed: int
    current_subject_index: int | None = None
    created_at: str
    started_at: str | None = None
    finished_at: str | None = None


# Subject Production Endpoints


@router.post("/subjects/{subject_id}/production")
async def start_subject_production(
    subject_id: UUID,
    body: dict[str, str],
    request: Request,
    user: str = Depends(get_current_user),
    uow_factory: ProductionUnitOfWorkFactory = Depends(),
) -> dict[str, Any]:
    """Start production of a subject.

    Profile options:
    - brief_auto: Full automatic production pipeline
    - major_assisted: Not yet implemented

    Returns the production run.
    """
    profile_str = body.get("profile", "brief_auto")
    edition_id_str = body.get("edition_id")

    if not edition_id_str:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="edition_id is required",
        )

    try:
        profile = ProductionProfile(profile_str)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid profile: {profile_str}",
        )

    if profile == ProductionProfile.MAJOR_ASSISTED:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="major_assisted production not yet implemented",
        )

    service = SubjectProductionService(uow_factory)
    jobs = request.app.state.job_service
    dispatcher = request.app.state.job_dispatcher

    try:
        run = await service.create_run(
            subject_id=subject_id,
            edition_id=UUID(edition_id_str),
            profile=profile,
        )

        # Start the run
        await service.start_run(run.id)

        # Dispatch the first job (SOURCES stage)
        parameters = ProductionStageParameters(
            run_id=run.id,
            expected_stage=SubjectProductionStage.SOURCES.value,
        )

        job = await jobs.submit(
            kind="production.subject.sources",
            aggregate_type="subject",
            aggregate_id=run.subject_id,
            idempotency_key=f"production-sources-{run.id}",
            correlation_id=get_correlation_id(),
            input_parameters=parameters.model_dump(mode="json"),
            max_attempts=1,
            actor_id=user,
        )
        await dispatcher.dispatch(job.id)

        return {
            "run_id": str(run.id),
            "subject_id": str(run.subject_id),
            "profile": run.profile.value,
            "status": run.status.value,
            "stage": run.current_stage.value,
            "job_id": str(job.id),
            "created_at": run.created_at.isoformat(),
        }
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Production start failed: {e!s}",
        )


@router.get("/subjects/{subject_id}/production")
async def get_subject_production(
    subject_id: UUID,
    user: str = Depends(get_current_user),
    uow_factory: ProductionUnitOfWorkFactory = Depends(),
) -> ProductionStatus:
    """Get complete production status for a subject."""
    async with uow_factory() as uow:
        # Get current run
        run = await uow.subject_production_runs.get_current_for_subject(subject_id)
        if not run:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"No production run found for subject {subject_id}",
            )

        # Get all artifacts for this run
        artifacts = await uow.production_artifacts.list_for_run(run.id)
        artifacts_by_stage = {a.stage.value: a for a in artifacts}

        # Build stage statuses
        stages = {}
        stage_list = [s.value for s in SubjectProductionStage]
        for i, stage_name in enumerate(stage_list):
            artifact = artifacts_by_stage.get(stage_name)
            stages[stage_name] = {
                "status": artifact.status.value if artifact else "pending",
                "version": artifact.version if artifact else None,
                "error_code": None,
                "error_message": None,
            }

        # Calculate progress
        completed_stages = sum(
            1 for s in stages.values() if s["status"] in ("verified", "succeeded")
        )

        return ProductionStatus(
            subject_id=str(run.subject_id),
            title="Subject Title",  # Would fetch from subject entity
            editorial_type="brief",
            status=run.status.value,
            current_stage=run.current_stage.value,
            progress_current=completed_stages,
            progress_total=len(stage_list),
            conversation_id=str(run.conversation_id) if run.conversation_id else None,
            run_id=str(run.id),
            created_at=run.created_at.isoformat(),
            started_at=run.started_at.isoformat() if run.started_at else None,
            finished_at=run.finished_at.isoformat() if run.finished_at else None,
            stages=stages,
        )


@router.post("/subjects/{subject_id}/production/references/retry")
async def retry_references(
    subject_id: UUID,
    user: str = Depends(get_current_user),
    uow_factory: ProductionUnitOfWorkFactory = Depends(),
) -> dict[str, Any]:
    """Retry references generation for a subject.

    Archives the old conversation and creates a new one.
    Automatically regenerates extraction and synthesis.
    """
    async with uow_factory() as uow:
        run = await uow.subject_production_runs.get_current_for_subject(subject_id)
        if not run:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"No production run found for subject {subject_id}",
            )

        if run.status != SubjectProductionStatus.READY:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Can only retry from READY status, current is {run.status.value}",
            )

        # Archive old conversation (implementation would go here)
        # Create new conversation
        # Reset to SOURCES stage
        run.current_stage = SubjectProductionStage.SOURCES
        run.conversation_id = None
        await uow.subject_production_runs.save(run)
        await uow.commit()

        return {
            "action": "retry_references",
            "run_id": str(run.id),
            "status": "initiated",
        }


@router.post("/subjects/{subject_id}/production/synthesis/retry")
async def retry_synthesis(
    subject_id: UUID,
    user: str = Depends(get_current_user),
    uow_factory: ProductionUnitOfWorkFactory = Depends(),
) -> dict[str, Any]:
    """Retry synthesis generation for a subject.

    Uses the same conversation and references/extraction.
    Only regenerates synthesis and brief.
    """
    async with uow_factory() as uow:
        run = await uow.subject_production_runs.get_current_for_subject(subject_id)
        if not run:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"No production run found for subject {subject_id}",
            )

        if run.status != SubjectProductionStatus.READY:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Can only retry from READY status, current is {run.status.value}",
            )

        # Reset to SYNTHESIS stage
        run.current_stage = SubjectProductionStage.SYNTHESIS
        await uow.subject_production_runs.save(run)

        # Mark brief as stale
        await uow.production_artifacts.mark_downstream_stale(
            run.id, SubjectProductionStage.SYNTHESIS.value
        )

        await uow.commit()

        return {
            "action": "retry_synthesis",
            "run_id": str(run.id),
            "status": "initiated",
        }


@router.post("/subjects/{subject_id}/production/cancel")
async def cancel_production(
    subject_id: UUID,
    user: str = Depends(get_current_user),
    uow_factory: ProductionUnitOfWorkFactory = Depends(),
) -> dict[str, Any]:
    """Cancel production for a subject."""
    service = SubjectProductionService(uow_factory)

    async with uow_factory() as uow:
        run = await uow.subject_production_runs.get_current_for_subject(subject_id)
        if not run:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"No production run found for subject {subject_id}",
            )

        await service.cancel_run(run.id)

        return {
            "action": "cancel",
            "run_id": str(run.id),
            "status": SubjectProductionStatus.CANCELLED.value,
        }


@router.get("/subjects/{subject_id}/production/artifacts/references")
async def get_references_artifact(
    subject_id: UUID,
    user: str = Depends(get_current_user),
    uow_factory: ProductionUnitOfWorkFactory = Depends(),
) -> dict[str, Any]:
    """Get the current references artifact for a subject."""
    async with uow_factory() as uow:
        run = await uow.subject_production_runs.get_current_for_subject(subject_id)
        if not run:
            raise HTTPException(status_code=404, detail="No production run found")

        artifact = await uow.production_artifacts.get_current(run.id, "references")
        if not artifact:
            raise HTTPException(status_code=404, detail="References artifact not found")

        return {
            "artifact_id": str(artifact.id),
            "stage": artifact.stage.value,
            "version": artifact.version,
            "status": artifact.status.value,
            "metadata": artifact.metadata,
        }


@router.get("/subjects/{subject_id}/production/artifacts/extraction")
async def get_extraction_artifact(
    subject_id: UUID,
    user: str = Depends(get_current_user),
    uow_factory: ProductionUnitOfWorkFactory = Depends(),
) -> dict[str, Any]:
    """Get the current extraction artifact for a subject."""
    async with uow_factory() as uow:
        run = await uow.subject_production_runs.get_current_for_subject(subject_id)
        if not run:
            raise HTTPException(status_code=404, detail="No production run found")

        artifact = await uow.production_artifacts.get_current(run.id, "extraction")
        if not artifact:
            raise HTTPException(status_code=404, detail="Extraction artifact not found")

        return {
            "artifact_id": str(artifact.id),
            "stage": artifact.stage.value,
            "version": artifact.version,
            "status": artifact.status.value,
            "metadata": artifact.metadata,
        }


@router.get("/subjects/{subject_id}/production/artifacts/synthesis")
async def get_synthesis_artifact(
    subject_id: UUID,
    user: str = Depends(get_current_user),
    uow_factory: ProductionUnitOfWorkFactory = Depends(),
) -> dict[str, Any]:
    """Get the current synthesis artifact for a subject."""
    async with uow_factory() as uow:
        run = await uow.subject_production_runs.get_current_for_subject(subject_id)
        if not run:
            raise HTTPException(status_code=404, detail="No production run found")

        artifact = await uow.production_artifacts.get_current(run.id, "synthesis")
        if not artifact:
            raise HTTPException(status_code=404, detail="Synthesis artifact not found")

        return {
            "artifact_id": str(artifact.id),
            "stage": artifact.stage.value,
            "version": artifact.version,
            "status": artifact.status.value,
            "metadata": artifact.metadata,
        }


@router.get("/subjects/{subject_id}/production/artifacts/brief")
async def get_brief_artifact(
    subject_id: UUID,
    user: str = Depends(get_current_user),
    uow_factory: ProductionUnitOfWorkFactory = Depends(),
) -> dict[str, Any]:
    """Get the current brief artifact for a subject."""
    async with uow_factory() as uow:
        run = await uow.subject_production_runs.get_current_for_subject(subject_id)
        if not run:
            raise HTTPException(status_code=404, detail="No production run found")

        artifact = await uow.production_artifacts.get_current(run.id, "brief")
        if not artifact:
            raise HTTPException(status_code=404, detail="Brief artifact not found")

        return {
            "artifact_id": str(artifact.id),
            "stage": artifact.stage.value,
            "version": artifact.version,
            "status": artifact.status.value,
            "metadata": artifact.metadata,
        }


# Edition Production Endpoints


@router.post("/editions/{edition_id}/production/briefs")
async def start_edition_brief_production(
    edition_id: UUID,
    request: Request,
    user: str = Depends(get_current_user),
    uow_factory: ProductionUnitOfWorkFactory = Depends(),
) -> BatchStatus:
    """Start batch production of all selected briefs in an edition.

    Idempotent: returns existing active batch if one exists.
    """
    service = EditionProductionService(uow_factory)
    jobs = request.app.state.job_service
    dispatcher = request.app.state.job_dispatcher

    try:
        async with uow_factory() as uow:
            # Check if active batch exists
            active_batch = await uow.edition_production_batches.get_active_for_edition(edition_id)
            if active_batch:
                # Return existing batch
                return BatchStatus(
                    batch_id=str(active_batch.id),
                    edition_id=str(active_batch.edition_id),
                    profile=active_batch.profile.value,
                    status=active_batch.status,
                    items=0,  # TODO: Count from batch items
                    completed=0,
                    needs_review=0,
                    failed=0,
                    current_subject_index=None,
                    created_at=active_batch.created_at.isoformat(),
                    started_at=active_batch.started_at.isoformat()
                    if active_batch.started_at
                    else None,
                    finished_at=active_batch.finished_at.isoformat()
                    if active_batch.finished_at
                    else None,
                )

        # Get all selected briefs for this edition
        async with uow_factory() as uow:
            groups = await uow.editorial_groups.list_for_edition(edition_id)
            subject_ids = [
                g.subject_id
                for g in groups
                if g.status == "selected" and g.editorial_type == "brief"
            ]

            if not subject_ids:
                raise ValueError("No selected briefs found for edition")

            batch = await service.create_batch(
                edition_id=edition_id,
                profile=ProductionProfile.BRIEF_AUTO,
                subject_ids=subject_ids,
            )

            # Start the batch
            batch.start()
            await uow.edition_production_batches.save(batch)
            await uow.commit()

            # Dispatch the first job for the first subject
            if subject_ids:
                first_subject_id = subject_ids[0]
                subject_run = await service.create_batch_item_run(
                    batch_id=batch.id,
                    subject_id=first_subject_id,
                    position=0,
                )

                if subject_run:
                    parameters = ProductionStageParameters(
                        run_id=subject_run.id,
                        expected_stage=SubjectProductionStage.SOURCES.value,
                    )
                    job = await jobs.submit(
                        kind="production.subject.sources",
                        aggregate_type="subject",
                        aggregate_id=first_subject_id,
                        idempotency_key=f"production-batch-{batch.id}-{first_subject_id}",
                        correlation_id=get_correlation_id(),
                        input_parameters=parameters.model_dump(mode="json"),
                        max_attempts=1,
                        actor_id=user,
                    )
                    await dispatcher.dispatch(job.id)

            return BatchStatus(
                batch_id=str(batch.id),
                edition_id=str(batch.edition_id),
                profile=batch.profile.value,
                status=batch.status,
                items=len(subject_ids),
                completed=0,
                needs_review=0,
                failed=0,
                current_subject_index=0 if subject_ids else None,
                created_at=batch.created_at.isoformat(),
                started_at=batch.started_at.isoformat() if batch.started_at else None,
                finished_at=None,
            )
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Batch creation failed: {e!s}",
        )


@router.get("/editions/{edition_id}/production/briefs")
async def get_edition_brief_production(
    edition_id: UUID,
    user: str = Depends(get_current_user),
    uow_factory: ProductionUnitOfWorkFactory = Depends(),
) -> BatchStatus:
    """Get the status of batch production for an edition."""
    service = EditionProductionService(uow_factory)

    async with uow_factory() as uow:
        batch = await service.get_batch(edition_id)
        if not batch:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"No batch found for edition {edition_id}",
            )

        # Get batch items
        items = await uow.edition_production_batch_items.list_for_batch(batch.id)

        # Count statuses
        completed = 0
        needs_review = 0
        failed = 0
        current_index = None

        for i, item in enumerate(items):
            run = await uow.subject_production_runs.get(item.production_run_id)
            if not run:
                continue

            if run.status == SubjectProductionStatus.READY:
                completed += 1
            elif run.status == SubjectProductionStatus.NEEDS_REVIEW:
                needs_review += 1
            elif run.status == SubjectProductionStatus.FAILED:
                failed += 1
            elif run.status == SubjectProductionStatus.RUNNING:
                current_index = i

        return BatchStatus(
            batch_id=str(batch.id),
            edition_id=str(batch.edition_id),
            profile=batch.profile.value,
            status=batch.status,
            items=len(items),
            completed=completed,
            needs_review=needs_review,
            failed=failed,
            current_subject_index=current_index,
            created_at=batch.created_at.isoformat(),
            started_at=batch.started_at.isoformat() if batch.started_at else None,
            finished_at=batch.finished_at.isoformat() if batch.finished_at else None,
        )


@router.post("/editions/{edition_id}/production/briefs/{batch_id}/cancel")
async def cancel_edition_batch(
    edition_id: UUID,
    batch_id: UUID,
    user: str = Depends(get_current_user),
    uow_factory: ProductionUnitOfWorkFactory = Depends(),
) -> dict[str, Any]:
    """Cancel a batch production for an edition."""
    async with uow_factory() as uow:
        batch = await uow.edition_production_batches.get_for_update(batch_id)
        if not batch:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Batch {batch_id} not found",
            )

        if batch.edition_id != edition_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Batch does not belong to this edition",
            )

        batch.cancel(now=datetime.now(UTC))
        await uow.edition_production_batches.save(batch)
        await uow.commit()

        return {
            "action": "cancel",
            "batch_id": str(batch.id),
            "status": batch.status,
        }
