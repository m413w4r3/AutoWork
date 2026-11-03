from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Request, Response, status
from pydantic import BaseModel

from cti_app.application.goodware import GoodwareServiceStatus
from cti_app.application.health import DependencyStatus, evaluate_readiness

router = APIRouter(prefix="/api/health", tags=["health"])


class LiveResponse(BaseModel):
    status: Literal["ok"]


class DependencyResponse(BaseModel):
    status: Literal["ok", "unavailable"]
    detail: str | None = None


class GoodwareBaselineResponse(BaseModel):
    baseline_id: UUID
    baseline_fingerprint_sha256: str
    normalization_version: str
    pattern_version: str
    record_count: int
    occurrence_sum: int
    source_count: int
    schema_version: str | None
    key_version: str | None
    index_format_version: str | None


class GoodwareStatusResponse(BaseModel):
    status: Literal["loaded", "absent", "unavailable"]
    reason: str | None = None
    baselines: list[GoodwareBaselineResponse]


class ReadyResponse(BaseModel):
    status: Literal["ok", "unavailable"]
    dependencies: dict[str, DependencyResponse]
    goodware: GoodwareStatusResponse


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
    try:
        goodware_status: GoodwareServiceStatus = await request.app.state.goodware_service.status()
    except Exception:
        goodware = GoodwareStatusResponse(
            status="unavailable",
            reason="goodware_baseline_inventory_unavailable",
            baselines=[],
        )
    else:
        goodware = GoodwareStatusResponse(
            status=goodware_status.state.value,
            reason=goodware_status.reason,
            baselines=[
                GoodwareBaselineResponse(
                    baseline_id=item.baseline_id,
                    baseline_fingerprint_sha256=item.baseline_fingerprint_sha256,
                    normalization_version=item.normalization_version,
                    pattern_version=item.pattern_version,
                    record_count=item.record_count,
                    occurrence_sum=item.occurrence_sum,
                    source_count=item.source_count,
                    schema_version=item.schema_version,
                    key_version=item.key_version,
                    index_format_version=item.index_format_version,
                )
                for item in goodware_status.baselines
            ],
        )
    return ReadyResponse(
        status="ok" if is_ready else "unavailable",
        dependencies={
            name: DependencyResponse(status=result.status, detail=result.detail)
            for name, result in checks.items()
        },
        goodware=goodware,
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
