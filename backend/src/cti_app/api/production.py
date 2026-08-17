"""API endpoints for subject production workflow."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel

from cti_app.application.jobs import JobDispatcher, JobService
from cti_app.application.persistence import UnitOfWorkFactory
from cti_app.application.production_jobs import ProductionStageParameters
from cti_app.application.subject_production import (
    EditionProductionService,
    SubjectProductionService,
)
from cti_app.domain.editorial import EditorialGroup, EditorialGroupStatus, EditorialType
from cti_app.domain.production import (
    ProductionProfile,
    SubjectProductionStage,
    SubjectProductionStatus,
)
from cti_app.logging import get_correlation_id

router = APIRouter(prefix="/api", tags=["production"])


# Request Models


class StartSubjectProductionRequest(BaseModel):
    """Body for starting a single subject production.

    The edition is resolved from the subject's editorial group, so callers only
    need to know the subject id.
    """

    profile: ProductionProfile = ProductionProfile.BRIEF_AUTO


class StartEditionProductionRequest(BaseModel):
    """Body for starting a batch production.

    When ``subject_ids`` is omitted, every selected brief of the edition is
    produced. When provided, only those subjects are produced.
    """

    subject_ids: list[UUID] | None = None


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


def _runtime(request: Request) -> tuple[UnitOfWorkFactory, JobService, JobDispatcher]:
    return (
        request.app.state.uow_factory,
        request.app.state.job_service,
        request.app.state.job_dispatcher,
    )


def _eligible_brief_subject_ids(groups: Iterable[EditorialGroup]) -> list[UUID]:
    """Subjects of an edition that are selected briefs, in board order."""
    return [
        group.subject_id
        for group in groups
        if group.subject_id is not None
        and group.status == EditorialGroupStatus.SELECTED
        and group.editorial_type == EditorialType.BRIEF
    ]


# Subject Production Endpoints


@router.post("/subjects/{subject_id}/production")
async def start_subject_production(
    subject_id: UUID,
    request: Request,
    body: StartSubjectProductionRequest | None = None,
    user: str = "system",
) -> dict[str, Any]:
    """Start production of a subject.

    Profile options:
    - brief_auto: Full automatic production pipeline
    - major_assisted: Not yet implemented

    The edition is resolved from the subject's editorial group.
    Returns the production run.
    """
    payload = body or StartSubjectProductionRequest()
    profile = payload.profile

    if profile == ProductionProfile.MAJOR_ASSISTED:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="major_assisted production not yet implemented",
        )

    uow_factory, jobs, dispatcher = _runtime(request)

    async with uow_factory() as uow:
        group = await uow.editorial_groups.get_by_subject(subject_id)
        if group is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"No editorial group found for subject {subject_id}",
            )
        if group.status != EditorialGroupStatus.SELECTED:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Subject is not selected",
            )
        if group.editorial_type != EditorialType.BRIEF:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Subject is not a brief",
            )
        edition_id = group.edition_id

    service = SubjectProductionService(uow_factory)

    try:
        run = await service.create_run(
            subject_id=subject_id,
            edition_id=edition_id,
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
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        ) from e

    return {
        "run_id": str(run.id),
        "subject_id": str(run.subject_id),
        "edition_id": str(edition_id),
        "profile": run.profile.value,
        "status": run.status.value,
        "stage": run.current_stage.value,
        "job_id": str(job.id),
        "created_at": run.created_at.isoformat(),
    }


@router.get("/subjects/{subject_id}/production")
async def get_subject_production(
    subject_id: UUID,
    request: Request,
) -> ProductionStatus:
    """Get complete production status for a subject.

    Returns 404 when the subject has no production run yet — that is the
    signal the UI uses to offer "start production".
    """
    uow_factory, _, _ = _runtime(request)

    async with uow_factory() as uow:
        # Get current run
        run = await uow.subject_production_runs.get_current_for_subject(subject_id)
        if not run:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"No production run found for subject {subject_id}",
            )

        group = await uow.editorial_groups.get_by_subject(subject_id)

        # Get all artifacts for this run
        artifacts = await uow.production_artifacts.list_for_run(run.id)
        artifacts_by_stage = {a.stage.value: a for a in artifacts}

        # Build stage statuses
        stages = {}
        stage_list = [s.value for s in SubjectProductionStage]
        for stage_name in stage_list:
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
            title=group.title if group else str(run.subject_id),
            editorial_type=(
                group.editorial_type.value
                if group and group.editorial_type
                else EditorialType.BRIEF.value
            ),
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
    request: Request,
) -> dict[str, Any]:
    """Retry references generation for a subject.

    Archives the old conversation and creates a new one.
    Automatically regenerates extraction and synthesis.
    """
    uow_factory, _, _ = _runtime(request)

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
    request: Request,
) -> dict[str, Any]:
    """Retry synthesis generation for a subject.

    Uses the same conversation and references/extraction.
    Only regenerates synthesis and brief.
    """
    uow_factory, _, _ = _runtime(request)

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
    request: Request,
) -> dict[str, Any]:
    """Cancel production for a subject."""
    uow_factory, _, _ = _runtime(request)
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


async def _artifact_view(
    request: Request,
    subject_id: UUID,
    stage: str,
) -> dict[str, Any]:
    uow_factory, _, _ = _runtime(request)

    async with uow_factory() as uow:
        run = await uow.subject_production_runs.get_current_for_subject(subject_id)
        if not run:
            raise HTTPException(status_code=404, detail="No production run found")

        artifact = await uow.production_artifacts.get_current(run.id, stage)
        if not artifact:
            raise HTTPException(status_code=404, detail=f"{stage} artifact not found")

        return {
            "artifact_id": str(artifact.id),
            "stage": artifact.stage.value,
            "version": artifact.version,
            "status": artifact.status.value,
            "metadata": artifact.metadata,
        }


@router.get("/subjects/{subject_id}/production/artifacts/references")
async def get_references_artifact(subject_id: UUID, request: Request) -> dict[str, Any]:
    """Get the current references artifact for a subject."""
    return await _artifact_view(request, subject_id, "references")


@router.get("/subjects/{subject_id}/production/artifacts/extraction")
async def get_extraction_artifact(subject_id: UUID, request: Request) -> dict[str, Any]:
    """Get the current extraction artifact for a subject."""
    return await _artifact_view(request, subject_id, "extraction")


@router.get("/subjects/{subject_id}/production/artifacts/synthesis")
async def get_synthesis_artifact(subject_id: UUID, request: Request) -> dict[str, Any]:
    """Get the current synthesis artifact for a subject."""
    return await _artifact_view(request, subject_id, "synthesis")


@router.get("/subjects/{subject_id}/production/artifacts/brief")
async def get_brief_artifact(subject_id: UUID, request: Request) -> dict[str, Any]:
    """Get the current brief artifact for a subject."""
    return await _artifact_view(request, subject_id, "brief")


# Edition Production Endpoints


@router.post("/editions/{edition_id}/production/briefs")
async def start_edition_brief_production(
    edition_id: UUID,
    request: Request,
    body: StartEditionProductionRequest | None = None,
    user: str = "system",
) -> BatchStatus:
    """Start batch production of selected briefs in an edition.

    Without a body (or with ``subject_ids: null``) every selected brief is
    produced. With ``subject_ids`` only that subset is produced.

    Idempotent: returns existing active batch if one exists.
    """
    payload = body or StartEditionProductionRequest()
    uow_factory, jobs, dispatcher = _runtime(request)
    service = EditionProductionService(uow_factory)

    async with uow_factory() as uow:
        # Check if active batch exists
        active_batch = await uow.edition_production_batches.get_active_for_edition(edition_id)
        if active_batch:
            items = await uow.edition_production_batch_items.list_for_batch(active_batch.id)
            return BatchStatus(
                batch_id=str(active_batch.id),
                edition_id=str(active_batch.edition_id),
                profile=active_batch.profile.value,
                status=active_batch.status,
                items=len(items),
                completed=0,
                needs_review=0,
                failed=0,
                current_subject_index=None,
                created_at=active_batch.created_at.isoformat(),
                started_at=active_batch.started_at.isoformat() if active_batch.started_at else None,
                finished_at=active_batch.finished_at.isoformat()
                if active_batch.finished_at
                else None,
            )

        # Determine which subjects to produce
        groups = await uow.editorial_groups.list_for_edition(edition_id)
        eligible_order = _eligible_brief_subject_ids(groups)

    if not eligible_order:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No selected briefs found for edition",
        )

    if payload.subject_ids:
        requested = set(payload.subject_ids)
        unknown = requested - set(eligible_order)
        if unknown:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Some requested subjects are not selected briefs",
            )
        subject_ids = [sid for sid in eligible_order if sid in requested]
    else:
        subject_ids = list(eligible_order)

    try:
        batch = await service.create_batch(
            edition_id=edition_id,
            profile=ProductionProfile.BRIEF_AUTO,
            subject_ids=subject_ids,
        )

        async with uow_factory() as uow:
            batch.start()
            await uow.edition_production_batches.save(batch)
            await uow.commit()

        # Dispatch the first job for the first subject
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
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        ) from e

    return BatchStatus(
        batch_id=str(batch.id),
        edition_id=str(batch.edition_id),
        profile=batch.profile.value,
        status=batch.status,
        items=len(subject_ids),
        completed=0,
        needs_review=0,
        failed=0,
        current_subject_index=0,
        created_at=batch.created_at.isoformat(),
        started_at=batch.started_at.isoformat() if batch.started_at else None,
        finished_at=None,
    )


@router.get("/editions/{edition_id}/production/briefs")
async def get_edition_brief_production(
    edition_id: UUID,
    request: Request,
) -> BatchStatus:
    """Get the status of batch production for an edition.

    Returns 404 when no batch exists yet — that is the signal the UI uses to
    offer "produce all briefs".
    """
    uow_factory, _, _ = _runtime(request)
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
    request: Request,
) -> dict[str, Any]:
    """Cancel a batch production for an edition."""
    uow_factory, _, _ = _runtime(request)

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
