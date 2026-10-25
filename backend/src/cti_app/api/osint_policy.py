"""Human-controlled API for instance and subject-set OSINT_OPEN policy."""

from __future__ import annotations

import hashlib
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Header, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field

from cti_app.api.investigation import (
    _composition_factory,
    _http_error,
    _require_verified_human_operator,
)
from cti_app.application.osint_open_policy import (
    OsintOpenPolicyService,
    OsintPolicyConflictError,
    OsintPolicyError,
    OsintPolicyNotFoundError,
)
from cti_app.domain.investigation import BudgetDimension, DisclosureBounds, TLPLevel
from cti_app.domain.osint_open import (
    OsintCapabilities,
    OsintPolicyVersion,
    OsintScopeKind,
)

router = APIRouter(prefix="/api/osint-open-policy", tags=["osint-open-policy"])


class PolicyScopeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: OsintScopeKind
    subject_ids: list[UUID] = Field(default_factory=list, max_length=100)


class PolicyDisclosureRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tlp: TLPLevel | None = None
    do_not_submit: bool | None = None
    external_llm_allowed: bool | None = None


class PolicyCapabilitiesRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    allowed_providers: list[str] | None = Field(default=None, max_length=8)
    budget_limits: dict[str, int] = Field(default_factory=dict, max_length=7)
    acquire_hits_allowed: bool | None = None
    disclosure: PolicyDisclosureRequest = Field(default_factory=PolicyDisclosureRequest)
    max_effect_claims_per_investigation: int | None = Field(default=None, ge=1)


class PolicyActivateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scope: PolicyScopeRequest
    capabilities: PolicyCapabilitiesRequest | None = None
    reason: str = Field(min_length=1, max_length=2000)
    correlation_id: str = Field(min_length=1, max_length=180)


class PolicyChangeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    policy_id: UUID
    expected_version: int = Field(ge=1)
    capabilities: PolicyCapabilitiesRequest | None = None
    reason: str = Field(min_length=1, max_length=2000)
    correlation_id: str = Field(min_length=1, max_length=180)


class PolicyView(BaseModel):
    policy_id: UUID
    version: int
    mode: str
    status: str
    scope_kind: str
    scope_ref: str
    capabilities: dict[str, Any]
    event_id: UUID
    actor_id: str
    reason: str
    correlation_id: str
    activation_correlation_id: str
    previous_version: int | None
    created_at: str


class CurrentPolicyView(BaseModel):
    mode: str
    policy: PolicyView | None


def _scope(kind: OsintScopeKind, subject_ids: list[UUID]) -> str:
    if kind is OsintScopeKind.INSTANCE:
        if subject_ids:
            raise _http_error(422, "instance_scope_has_no_subject_ids")
        return "instance"
    try:
        return OsintPolicyVersion.subject_set_ref(subject_ids)
    except ValueError as exc:
        raise _http_error(422, "subject_set_requires_subjects") from exc


def _effective_correlation(body_correlation: str, idempotency_key: str) -> str:
    digest = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()[:32]
    return f"{body_correlation.strip()}:idem:{digest}"


def _capabilities(
    service: OsintOpenPolicyService,
    request: PolicyCapabilitiesRequest | None,
) -> OsintCapabilities:
    if request is None:
        return service.default_capabilities()
    defaults = service.default_capabilities(
        tuple(request.allowed_providers) if request.allowed_providers is not None else None
    )
    limits = dict(defaults.budget_limits)
    for name, value in request.budget_limits.items():
        try:
            dimension = BudgetDimension(name)
        except ValueError as exc:
            raise _http_error(422, "invalid_policy_budget_dimension") from exc
        if isinstance(value, bool) or value < 0:
            raise _http_error(422, "invalid_policy_budget_limit")
        limits[dimension] = value
    disclosure_request = request.disclosure
    disclosure = DisclosureBounds(
        tlp=disclosure_request.tlp or defaults.disclosure.tlp,
        do_not_submit=(
            defaults.disclosure.do_not_submit
            if disclosure_request.do_not_submit is None
            else disclosure_request.do_not_submit
        ),
        external_llm_allowed=(
            defaults.disclosure.external_llm_allowed
            if disclosure_request.external_llm_allowed is None
            else disclosure_request.external_llm_allowed
        ),
    )
    try:
        capabilities = OsintCapabilities(
            allowed_providers=(
                tuple(request.allowed_providers)
                if request.allowed_providers is not None
                else defaults.allowed_providers
            ),
            budget_limits=limits,
            acquire_hits_allowed=(
                defaults.acquire_hits_allowed
                if request.acquire_hits_allowed is None
                else request.acquire_hits_allowed
            ),
            disclosure=disclosure,
            max_effect_claims_per_investigation=(
                request.max_effect_claims_per_investigation
                or defaults.max_effect_claims_per_investigation
            ),
        )
        service._validate_capabilities(capabilities)
    except (TypeError, ValueError) as exc:
        raise _http_error(422, "invalid_policy_capabilities") from exc
    return capabilities


def _view(policy: OsintPolicyVersion) -> PolicyView:
    capabilities = policy.capabilities
    return PolicyView(
        policy_id=policy.policy_id,
        version=policy.version,
        mode=policy.mode.value,
        status=policy.status.value,
        scope_kind=policy.scope_kind.value,
        scope_ref=policy.scope_ref,
        capabilities={
            "allowed_providers": list(capabilities.allowed_providers),
            "budget_limits": {
                dimension.value: value for dimension, value in capabilities.budget_limits.items()
            },
            "acquire_hits_allowed": capabilities.acquire_hits_allowed,
            "disclosure": {
                "tlp": (
                    capabilities.disclosure.tlp.value
                    if capabilities.disclosure.tlp is not None
                    else None
                ),
                "do_not_submit": capabilities.disclosure.do_not_submit,
                "external_llm_allowed": capabilities.disclosure.external_llm_allowed,
            },
            "max_effect_claims_per_investigation": capabilities.max_effect_claims_per_investigation,
        },
        event_id=policy.event_id,
        actor_id=policy.actor_id,
        reason=policy.reason,
        correlation_id=policy.correlation_id,
        activation_correlation_id=policy.activation_correlation_id,
        previous_version=policy.previous_version,
        created_at=policy.created_at.isoformat(),
    )


def _service(request: Request) -> OsintOpenPolicyService:
    factory = _composition_factory(request)
    return factory.osint_open_policy_service


def _raise_policy_http(exc: OsintPolicyError) -> None:
    if isinstance(exc, OsintPolicyNotFoundError):
        raise _http_error(404, "policy_not_found") from exc
    if isinstance(exc, OsintPolicyConflictError):
        raise _http_error(409, "policy_conflict", str(exc)) from exc
    raise _http_error(422, "invalid_policy", str(exc)) from exc


@router.get("/current", response_model=CurrentPolicyView)
async def get_current_policy(
    request: Request,
    scope_kind: OsintScopeKind = Query(default=OsintScopeKind.INSTANCE),
    scope_ref: str = Query(default="instance", max_length=4096),
) -> CurrentPolicyView:
    service = _service(request)
    try:
        policy = await service.current(scope_kind, scope_ref)
    except OsintPolicyError as exc:
        _raise_policy_http(exc)
    return CurrentPolicyView(
        mode=policy.mode.value if policy else "restricted",
        policy=_view(policy) if policy else None,
    )


@router.post("/activate", response_model=PolicyView, status_code=status.HTTP_201_CREATED)
async def activate_policy(
    payload: PolicyActivateRequest,
    request: Request,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=255)],
) -> PolicyView:
    identity = await _require_verified_human_operator(request)
    service = _service(request)
    try:
        policy = await service.activate(
            scope_kind=payload.scope.kind,
            scope_ref=_scope(payload.scope.kind, payload.scope.subject_ids),
            capabilities=_capabilities(service, payload.capabilities),
            actor_id=identity.actor_id,
            reason=payload.reason,
            correlation_id=_effective_correlation(payload.correlation_id, idempotency_key),
        )
    except OsintPolicyError as exc:
        _raise_policy_http(exc)
    return _view(policy)


@router.post("/extend", response_model=PolicyView)
async def extend_policy(
    payload: PolicyChangeRequest,
    request: Request,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=255)],
) -> PolicyView:
    identity = await _require_verified_human_operator(request)
    service = _service(request)
    if payload.capabilities is None:
        raise _http_error(422, "policy_capabilities_required")
    try:
        policy = await service.extend(
            policy_id=payload.policy_id,
            expected_version=payload.expected_version,
            capabilities=_capabilities(service, payload.capabilities),
            actor_id=identity.actor_id,
            reason=payload.reason,
            correlation_id=_effective_correlation(payload.correlation_id, idempotency_key),
        )
    except OsintPolicyError as exc:
        _raise_policy_http(exc)
    return _view(policy)


@router.post("/revoke", response_model=PolicyView)
async def revoke_policy(
    payload: PolicyChangeRequest,
    request: Request,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=255)],
) -> PolicyView:
    identity = await _require_verified_human_operator(request)
    service = _service(request)
    try:
        policy = await service.revoke(
            policy_id=payload.policy_id,
            expected_version=payload.expected_version,
            actor_id=identity.actor_id,
            reason=payload.reason,
            correlation_id=_effective_correlation(payload.correlation_id, idempotency_key),
        )
    except OsintPolicyError as exc:
        _raise_policy_http(exc)
    return _view(policy)


__all__ = ["router"]
