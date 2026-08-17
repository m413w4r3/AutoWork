"""API endpoints for subject production."""
from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status

from cti_app.api.auth import get_current_user
from cti_app.application.persistence import ProductionUnitOfWorkFactory
from cti_app.application.subject_production import (
    EditionProductionService,
    SubjectProductionService,
)
from cti_app.domain.production import ProductionProfile, SubjectProductionStatus

router = APIRouter(prefix="/api", tags=["production"])


class SubjectProductionResponse:
    """Response model for subject production status."""

    subject_id: UUID
    title: str
    editorial_type: str
    status: str
    current_stage: str
    progress_current: int
    progress_total: int
    stages: dict[str, Any]


@router.post("/subjects/{subject_id}/production")
async def start_subject_production(
    subject_id: UUID,
    body: dict[str, str],
    user: str = Depends(get_current_user),
    uow_factory: ProductionUnitOfWorkFactory = Depends(),
) -> dict[str, Any]:
    """Start production of a subject.

    Profile options:
    - brief_auto: Full automatic production pipeline
    - major_assisted: Not yet implemented

    Returns the production run with status and duplicate flag.
    """
    profile_str = body.get("profile", "brief_auto")

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

    try:
        run = await service.create_run(
            subject_id=subject_id,
            edition_id=UUID(body.get("edition_id")),  # Would be provided by client
            profile=profile,
        )
        return {
            "run_id": str(run.id),
            "status": run.status.value,
            "duplicate": False,  # Would check if this is a retry
        }
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )


@router.get("/subjects/{subject_id}/production")
async def get_subject_production(
    subject_id: UUID,
    user: str = Depends(get_current_user),
    uow_factory: ProductionUnitOfWorkFactory = Depends(),
) -> dict[str, Any]:
    """Get complete production status for a subject."""
    # Implementation would retrieve and format production data
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail="Endpoint not yet fully implemented",
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
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail="Endpoint not yet fully implemented",
    )


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
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail="Endpoint not yet fully implemented",
    )


@router.post("/subjects/{subject_id}/production/cancel")
async def cancel_production(
    subject_id: UUID,
    user: str = Depends(get_current_user),
    uow_factory: ProductionUnitOfWorkFactory = Depends(),
) -> dict[str, Any]:
    """Cancel production for a subject."""
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail="Endpoint not yet fully implemented",
    )


@router.get("/subjects/{subject_id}/production/artifacts/references")
async def get_references_artifact(
    subject_id: UUID,
    user: str = Depends(get_current_user),
    uow_factory: ProductionUnitOfWorkFactory = Depends(),
) -> dict[str, Any]:
    """Get the current references artifact (canonical JSON) for a subject."""
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail="Endpoint not yet fully implemented",
    )


@router.get("/subjects/{subject_id}/production/artifacts/extraction")
async def get_extraction_artifact(
    subject_id: UUID,
    user: str = Depends(get_current_user),
    uow_factory: ProductionUnitOfWorkFactory = Depends(),
) -> dict[str, Any]:
    """Get the current extraction artifact (canonical JSON) for a subject."""
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail="Endpoint not yet fully implemented",
    )


@router.get("/subjects/{subject_id}/production/artifacts/synthesis")
async def get_synthesis_artifact(
    subject_id: UUID,
    user: str = Depends(get_current_user),
    uow_factory: ProductionUnitOfWorkFactory = Depends(),
) -> dict[str, Any]:
    """Get the current synthesis artifact (Markdown) for a subject."""
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail="Endpoint not yet fully implemented",
    )


@router.get("/subjects/{subject_id}/production/artifacts/brief")
async def get_brief_artifact(
    subject_id: UUID,
    user: str = Depends(get_current_user),
    uow_factory: ProductionUnitOfWorkFactory = Depends(),
) -> dict[str, Any]:
    """Get the current brief artifact (Markdown) for a subject."""
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail="Endpoint not yet fully implemented",
    )


# Edition production endpoints

@router.post("/editions/{edition_id}/production/briefs")
async def start_edition_brief_production(
    edition_id: UUID,
    user: str = Depends(get_current_user),
    uow_factory: ProductionUnitOfWorkFactory = Depends(),
) -> dict[str, Any]:
    """Start batch production of all selected briefs in an edition.

    Idempotent: returns existing active batch if one exists.
    """
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail="Endpoint not yet fully implemented",
    )


@router.get("/editions/{edition_id}/production/briefs")
async def get_edition_brief_production(
    edition_id: UUID,
    user: str = Depends(get_current_user),
    uow_factory: ProductionUnitOfWorkFactory = Depends(),
) -> dict[str, Any]:
    """Get the status of batch production for an edition."""
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail="Endpoint not yet fully implemented",
    )


@router.post("/editions/{edition_id}/production/briefs/{batch_id}/cancel")
async def cancel_edition_batch(
    edition_id: UUID,
    batch_id: UUID,
    user: str = Depends(get_current_user),
    uow_factory: ProductionUnitOfWorkFactory = Depends(),
) -> dict[str, Any]:
    """Cancel a batch production for an edition."""
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail="Endpoint not yet fully implemented",
    )
