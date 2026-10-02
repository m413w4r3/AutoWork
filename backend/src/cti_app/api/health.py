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


class ModelRoutingStatusResponse(BaseModel):
    active_routing: dict[str, str]
    forced_backend: str | None
    configured_backends: dict[str, bool]
    code_version: str
    worker_code_version: str


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


@router.get("/models", response_model=ModelRoutingStatusResponse)
async def model_routing_status(request: Request) -> ModelRoutingStatusResponse:
    from cti_app.config import get_settings

    settings = get_settings()
    gateway = request.app.state.model_gateway
    routing = gateway.routing_status()
    return ModelRoutingStatusResponse(
        **routing,
        code_version=settings.code_version,
        worker_code_version=settings.worker_code_version or settings.code_version,
    )
