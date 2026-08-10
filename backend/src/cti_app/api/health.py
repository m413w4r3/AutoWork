from typing import Literal

from fastapi import APIRouter, Request, Response, status
from pydantic import BaseModel

from cti_app.application.health import DependencyStatus, evaluate_readiness

router = APIRouter(prefix="/api/health", tags=["health"])


class LiveResponse(BaseModel):
    status: Literal["ok"]


class DependencyResponse(BaseModel):
    status: Literal["ok", "unavailable"]
    detail: str | None = None


class ReadyResponse(BaseModel):
    status: Literal["ok", "unavailable"]
    dependencies: dict[str, DependencyResponse]


@router.get("/live", response_model=LiveResponse)
async def live() -> LiveResponse:
    return LiveResponse(status="ok")


@router.get("/ready", response_model=ReadyResponse)
async def ready(request: Request, response: Response) -> ReadyResponse:
    checks: dict[str, DependencyStatus] = await evaluate_readiness(request.app.state.readiness)
    is_ready = all(result.status == "ok" for result in checks.values())
    if not is_ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return ReadyResponse(
        status="ok" if is_ready else "unavailable",
        dependencies={
            name: DependencyResponse(status=result.status, detail=result.detail)
            for name, result in checks.items()
        },
    )
