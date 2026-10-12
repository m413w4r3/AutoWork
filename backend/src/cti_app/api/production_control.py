"""Read-only production control projection endpoints."""

from __future__ import annotations

from typing import Annotated, cast
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request, status

from cti_app.application.production_control_view import (
    InvalidControlCursorError,
    ProductionControlView,
    ProductionControlViewService,
)

router = APIRouter(prefix="/api", tags=["production-control"])


def _service(request: Request) -> ProductionControlViewService:
    configured = getattr(request.app.state, "production_control_view_service", None)
    if configured is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "production_control_view_unavailable"},
        )
    return cast(ProductionControlViewService, configured)


@router.get(
    "/editions/{edition_id}/production/control-view",
    response_model=ProductionControlView,
)
async def get_edition_production_control_view(
    edition_id: UUID,
    request: Request,
    cursor: Annotated[str | None, Query(max_length=128)] = None,
    limit: Annotated[int, Query(ge=1, le=25)] = 10,
) -> ProductionControlView:
    try:
        return await _service(request).edition_page(edition_id, cursor=cursor, limit=limit)
    except InvalidControlCursorError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "invalid_control_cursor"},
        ) from exc
    except LookupError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": str(exc)},
        ) from exc


@router.get(
    "/production/runs/{run_id}/control-view",
    response_model=ProductionControlView,
)
async def get_production_run_control_view(
    run_id: UUID,
    request: Request,
) -> ProductionControlView:
    try:
        return await _service(request).run_view(run_id)
    except LookupError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": str(exc)},
        ) from exc
