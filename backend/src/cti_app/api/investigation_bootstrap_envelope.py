"""Human-only G2 envelope proposal/approval routes for the VT bootstrap (M24-03B).

The orchestrator owns ``api/main.py``; this module only exports ``router`` and
never registers itself.  The routes reuse the exact verified-human identity
gate and error mapping of ``api.investigation_bootstrap``; the shared
``OPERATOR_CLIENT`` token is refused here and by the global route allowlist.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, cast
from uuid import UUID

from fastapi import APIRouter, Header, Request
from pydantic import BaseModel, ConfigDict, Field

from cti_app.api.investigation import (
    _composition_factory,
    _decision_correlation,
    _http_error,
    _require_verified_human_operator,
    _uow_factory,
)
from cti_app.api.investigation_bootstrap import (
    _bootstrap_http_error,
    _service,
    _settings,
)
from cti_app.application.investigation_bootstrap import BootstrapError
from cti_app.application.investigation_bootstrap_envelope import (
    BootstrapEnvelopeApprovalV1,
    BootstrapEnvelopeProposalV1,
    BootstrapEnvelopeUnitOfWork,
    InvestigationBootstrapEnvelopeService,
)
from cti_app.application.investigation_governance import (
    EnvelopeDecisionConflictError,
    EnvelopeNotFoundError,
    EnvelopeVersionConflictError,
)

router = APIRouter(prefix="/api/investigations", tags=["investigation-bootstrap-envelope"])


class BootstrapEnvelopeApprovalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1, max_length=4_000)
    correlation_id: str = Field(min_length=1, max_length=80)
    proposal_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_version: int | None = Field(default=None, ge=1)


def _envelope_service(request: Request) -> InvestigationBootstrapEnvelopeService:
    composition = _composition_factory(request)
    settings = _settings(request)
    return InvestigationBootstrapEnvelopeService(
        uow_factory=cast(Callable[[], BootstrapEnvelopeUnitOfWork], _uow_factory(request)),
        bootstrap=_service(request),
        article_snapshots=composition.article_snapshots,
        settings=settings,
    )


@router.get(
    "/{investigation_id}/bootstrap/envelope-proposal",
    response_model=BootstrapEnvelopeProposalV1,
)
async def get_bootstrap_envelope_proposal(
    investigation_id: UUID, request: Request
) -> BootstrapEnvelopeProposalV1:
    await _require_verified_human_operator(request)
    try:
        return await _envelope_service(request).propose_for_bootstrap(investigation_id)
    except BootstrapError as exc:
        raise _bootstrap_http_error(exc) from exc


@router.post(
    "/{investigation_id}/bootstrap/envelope/approve",
    response_model=BootstrapEnvelopeApprovalV1,
)
async def approve_bootstrap_envelope(
    investigation_id: UUID,
    payload: BootstrapEnvelopeApprovalRequest,
    request: Request,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=255)],
) -> BootstrapEnvelopeApprovalV1:
    identity = await _require_verified_human_operator(request)
    try:
        return await _envelope_service(request).approve_for_bootstrap(
            investigation_id,
            actor_id=identity.actor_id,
            reason=payload.reason,
            correlation_id=_decision_correlation(payload.correlation_id, idempotency_key),
            expected_proposal_sha256=payload.proposal_sha256,
            expected_version=payload.expected_version,
        )
    except BootstrapError as exc:
        raise _bootstrap_http_error(exc) from exc
    except EnvelopeNotFoundError as exc:
        raise _http_error(404, "investigation_not_found") from exc
    except (EnvelopeVersionConflictError, EnvelopeDecisionConflictError) as exc:
        raise _http_error(409, "envelope_conflict", str(exc)) from exc
    except ValueError as exc:
        raise _http_error(422, "invalid_envelope", str(exc)) from exc


__all__ = ["router"]
