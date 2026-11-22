"""Human-only VT bootstrap routes for an ARTICLE investigation (M24-03A).

The orchestrator owns ``api/main.py``; this module only exports ``router`` and
never registers itself.  Every route reuses the canonical verified-human gate:
an ``OPERATOR_CLIENT``-only caller is refused here as well as by the global
route allowlist once the router is mounted.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, cast
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException, Request, status

from cti_app.api.investigation import (
    _composition_factory,
    _http_error,
    _require_verified_human_operator,
    _uow_factory,
)
from cti_app.application.external_effects import advisor_disclosure_policy
from cti_app.application.investigation_bootstrap import (
    BootstrapError,
    BootstrapPlanV1,
    BootstrapSnapshotExportV1,
    BootstrapSnapshotV1,
    BootstrapToolExecutor,
    BootstrapUnitOfWork,
    BootstrapVtCapabilities,
    InvestigationBootstrapService,
    IOCInventoryV1,
)
from cti_app.application.persistence import UnitOfWork
from cti_app.config import Settings, get_settings

router = APIRouter(prefix="/api/investigations", tags=["investigation-bootstrap"])


def _settings(request: Request) -> Settings:
    configured = getattr(request.app.state, "settings", None)
    return configured if isinstance(configured, Settings) else get_settings()


def _service(request: Request) -> InvestigationBootstrapService:
    factory = _composition_factory(request)
    uow_factory = cast(Callable[[], BootstrapUnitOfWork], _uow_factory(request))
    settings = _settings(request)
    return InvestigationBootstrapService(
        uow_factory=uow_factory,
        article_snapshots=factory.article_snapshots,
        artifact_store=factory.artifact_store,
        capabilities=BootstrapVtCapabilities.from_settings(settings),
        disclosure_policy=advisor_disclosure_policy(settings.advisor_disclosure_profile),
    )


def _bootstrap_http_error(exc: BootstrapError) -> HTTPException:
    return _http_error(exc.status_code, exc.code, str(exc))


@router.get(
    "/{investigation_id}/bootstrap/inventory",
    response_model=IOCInventoryV1,
)
async def get_bootstrap_inventory(investigation_id: UUID, request: Request) -> IOCInventoryV1:
    await _require_verified_human_operator(request)
    try:
        return await _service(request).inventory(investigation_id)
    except BootstrapError as exc:
        raise _bootstrap_http_error(exc) from exc


@router.get(
    "/{investigation_id}/bootstrap/plan",
    response_model=BootstrapPlanV1,
)
async def get_bootstrap_plan(investigation_id: UUID, request: Request) -> BootstrapPlanV1:
    await _require_verified_human_operator(request)
    try:
        return await _service(request).plan(investigation_id)
    except BootstrapError as exc:
        raise _bootstrap_http_error(exc) from exc


@router.get(
    "/{investigation_id}/bootstrap/snapshot",
    response_model=BootstrapSnapshotV1,
)
async def get_bootstrap_snapshot(investigation_id: UUID, request: Request) -> BootstrapSnapshotV1:
    await _require_verified_human_operator(request)
    try:
        snapshot = await _service(request).latest_snapshot(investigation_id)
    except BootstrapError as exc:
        raise _bootstrap_http_error(exc) from exc
    if snapshot is None:
        raise _http_error(status.HTTP_404_NOT_FOUND, "bootstrap_snapshot_not_found")
    return snapshot


@router.get(
    "/{investigation_id}/bootstrap/snapshot/export",
    response_model=BootstrapSnapshotExportV1,
)
async def get_bootstrap_snapshot_export(
    investigation_id: UUID, request: Request
) -> BootstrapSnapshotExportV1:
    await _require_verified_human_operator(request)
    service = _service(request)
    try:
        snapshot = await service.latest_snapshot(investigation_id)
        if snapshot is None:
            raise _http_error(status.HTTP_404_NOT_FOUND, "bootstrap_snapshot_not_found")
        return service.snapshot_export(snapshot)
    except BootstrapError as exc:
        raise _bootstrap_http_error(exc) from exc


@router.post(
    "/{investigation_id}/bootstrap/run",
    response_model=BootstrapSnapshotV1,
)
async def run_bootstrap_batch(
    investigation_id: UUID,
    request: Request,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=255)],
) -> BootstrapSnapshotV1:
    identity = await _require_verified_human_operator(request)
    factory = _composition_factory(request)
    service = _service(request)

    def executor_factory(uow: BootstrapUnitOfWork) -> BootstrapToolExecutor:
        composition = factory.build(cast(UnitOfWork, uow), investigation_id, operator_mode=False)
        return composition.service

    try:
        return await service.execute_batch(
            investigation_id,
            idempotency_key=idempotency_key,
            actor_id=identity.actor_id,
            executor_factory=executor_factory,
        )
    except BootstrapError as exc:
        raise _bootstrap_http_error(exc) from exc


__all__ = ["router"]
