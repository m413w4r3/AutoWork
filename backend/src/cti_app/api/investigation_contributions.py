"""Workspace contribution routes (M24-07, ADR-0011 D6/D8).

The orchestrator owns ``api/main.py``; this module only exports ``router`` and
never registers itself.  Every route reuses the canonical verified-human gate.
An ``agent`` origin is refused here with ``operator_session_required``: a
session-bound write must arrive through lot 05B2, which verifies the D7 HMAC
and passes a validated task/lease authority to the same service.  ``host``
imports never claim lease authority and are never attributed to an operator.

A semantic refusal (closed mapping, forbidden file type, advisory observation
reference, stale revision content) is a **persisted** terminal row with a
typed ``validation_error_code``; the route returns it so the UI can display
the rejection and retry with a new attempt key.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Annotated, Any, cast
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from cti_app.api.investigation import (
    _composition_factory,
    _http_error,
    _require_verified_human_operator,
    _uow_factory,
)
from cti_app.application.work_contributions import (
    ContributionAuthority,
    OperatorSessionRequiredError,
    WorkContributionError,
    WorkContributionService,
    WorkContributionUnitOfWork,
)
from cti_app.application.workspace_agent import WorkspaceAgentPort
from cti_app.config import Settings, get_settings
from cti_app.domain.investigation import FindingAssessment, FindingKind, NoveltyClass
from cti_app.domain.work_contribution import (
    ContributionDeclarationV1,
    ContributionValidationError,
    FindingDeclarationV1,
    ObservationDeclarationV1,
    WorkContributionCategory,
    WorkContributionImportStatus,
    WorkContributionMethod,
    WorkContributionObjectType,
    WorkContributionOrigin,
)
from cti_app.infrastructure.workspace_agent import HttpWorkspaceAgentClient

router = APIRouter(prefix="/api/investigations", tags=["investigation-contributions"])


class ObservationDeclarationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    object_type: WorkContributionObjectType
    method: WorkContributionMethod
    object_id: str = Field(min_length=1, max_length=512)
    tool: str = Field(min_length=1, max_length=128)
    tool_version: str = Field(min_length=1, max_length=64)
    parameters: dict[str, JsonValue] = Field(default_factory=dict)
    limitations: tuple[str, ...] = ()
    observed_at: datetime | None = None


class FindingDeclarationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: FindingKind
    assessment: FindingAssessment
    statement: str = Field(min_length=1)
    observation_refs: tuple[str, ...] = ()
    claim_refs: tuple[str, ...] = ()
    novelty: NoveltyClass | None = None
    scope: str = ""
    limitations: tuple[str, ...] = ()
    proposed_sample_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class ImportContributionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workspace_id: str = Field(pattern=r"^ws-[0-9a-f]{32}$")
    workspace_revision_id: str = Field(pattern=r"^rev-[0-9a-f]{32}$")
    category: WorkContributionCategory = WorkContributionCategory.WORK_CONTRIBUTION
    origin: WorkContributionOrigin = WorkContributionOrigin.HOST
    author_actor_id: str | None = Field(default=None, max_length=256)
    task_id: str | None = None
    lease_fence: int | None = Field(default=None, ge=1)
    observation: ObservationDeclarationRequest | None = None
    finding: FindingDeclarationRequest | None = None

    @model_validator(mode="after")
    def _validate_category_shape(self) -> ImportContributionRequest:
        if self.category is WorkContributionCategory.WORK_CONTRIBUTION:
            if self.observation is not None or self.finding is not None:
                raise ValueError("a work contribution declares no observation or finding")
            return self
        if self.category is WorkContributionCategory.OBSERVATION:
            if self.observation is None or self.finding is not None:
                raise ValueError("an observation import requires exactly one observation")
            return self
        if self.finding is None or self.observation is not None:
            raise ValueError("a finding import requires exactly one finding")
        return self


def _settings(request: Request) -> Settings:
    configured = getattr(request.app.state, "settings", None)
    return configured if isinstance(configured, Settings) else get_settings()


def _workspace_agent(request: Request) -> tuple[WorkspaceAgentPort, bool]:
    injected = getattr(request.app.state, "workspace_agent_port", None)
    if injected is not None:
        return cast(WorkspaceAgentPort, injected), False
    settings = _settings(request)
    return (
        HttpWorkspaceAgentClient(
            socket_path=settings.workspace_agent_socket or None,
            token_file=settings.workspace_agent_token_file or None,
        ),
        True,
    )


def _service(request: Request, workspace_agent: WorkspaceAgentPort) -> WorkContributionService:
    injected = getattr(request.app.state, "work_contribution_service", None)
    if injected is not None:
        return cast(WorkContributionService, injected)
    factory = _composition_factory(request)
    return WorkContributionService(
        uow_factory=cast(Callable[[], WorkContributionUnitOfWork], _uow_factory(request)),
        workspace_agent=workspace_agent,
        artifact_store=factory.artifact_store,
        article_snapshots=factory.article_snapshots,
        max_contribution_bytes=_settings(request).task_limits().max_contribution_bytes,
    )


def _declaration(body: ImportContributionRequest) -> ContributionDeclarationV1:
    observation = (
        ObservationDeclarationV1(
            object_type=body.observation.object_type,
            method=body.observation.method,
            object_id=body.observation.object_id,
            tool=body.observation.tool,
            tool_version=body.observation.tool_version,
            parameters=body.observation.parameters,
            limitations=body.observation.limitations,
            observed_at=body.observation.observed_at,
        )
        if body.observation is not None
        else None
    )
    finding = (
        FindingDeclarationV1(
            kind=body.finding.kind,
            assessment=body.finding.assessment,
            statement=body.finding.statement,
            observation_refs=tuple(
                _uuid(ref, "observation_refs") for ref in body.finding.observation_refs
            ),
            claim_refs=body.finding.claim_refs,
            novelty=body.finding.novelty,
            scope=body.finding.scope,
            limitations=body.finding.limitations,
            proposed_sample_sha256=body.finding.proposed_sample_sha256,
        )
        if body.finding is not None
        else None
    )
    return ContributionDeclarationV1(
        category=body.category, observation=observation, finding=finding
    )


def _uuid(value: str, field_name: str) -> UUID:
    try:
        return UUID(value)
    except ValueError as exc:
        raise _http_error(422, "invalid_uuid", f"{field_name} contains an invalid UUID") from exc


def _authority(body: ImportContributionRequest) -> ContributionAuthority:
    if body.origin is WorkContributionOrigin.AGENT:
        raise OperatorSessionRequiredError(
            "agent contributions require a verified session-bound call (lot 05B2)"
        )
    if body.origin is WorkContributionOrigin.HOST:
        # A host import never claims lease authority, even if the client sends one.
        if body.lease_fence is not None:
            raise _http_error(422, "invalid_request", "host imports never claim a lease fence")
        return ContributionAuthority(
            origin=body.origin,
            actor_id=body.author_actor_id,
            task_id=None,
            lease_fence=None,
        )
    task_id = _uuid(body.task_id, "task_id") if body.task_id is not None else None
    if body.lease_fence is not None:
        raise _http_error(422, "invalid_request", "human imports never claim a lease fence")
    return ContributionAuthority(
        origin=body.origin,
        actor_id=body.author_actor_id,
        task_id=task_id,
        lease_fence=None,
    )


def _service_error(exc: WorkContributionError) -> HTTPException:
    if isinstance(exc, OperatorSessionRequiredError):
        return _http_error(403, exc.code, str(exc), details=exc.details or None)
    return _http_error(exc.status_code, exc.code, str(exc), details=exc.details or None)


def _domain_error(exc: ContributionValidationError) -> HTTPException:
    return _http_error(422, exc.code.value, str(exc))


@router.get("/{investigation_id}/contributions")
async def list_contributions(
    investigation_id: UUID,
    request: Request,
    category: WorkContributionCategory | None = None,
    import_status: WorkContributionImportStatus | None = None,
    workspace_id: str | None = None,
) -> list[dict[str, Any]]:
    await _require_verified_human_operator(request)
    agent, should_close = _workspace_agent(request)
    try:
        contributions = await _service(request, agent).list(
            investigation_id,
            category=category,
            import_status=import_status,
            workspace_id=workspace_id,
        )
    except WorkContributionError as exc:
        raise _service_error(exc) from exc
    finally:
        if should_close:
            await agent.aclose()
    return [contribution.to_payload() for contribution in contributions]


@router.post("/{investigation_id}/contributions")
async def import_contribution(
    investigation_id: UUID,
    body: ImportContributionRequest,
    request: Request,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=255)],
) -> dict[str, Any]:
    await _require_verified_human_operator(request)
    try:
        declaration = _declaration(body)
        authority = _authority(body)
    except ContributionValidationError as exc:
        raise _domain_error(exc) from exc
    except WorkContributionError as exc:
        raise _service_error(exc) from exc
    agent, should_close = _workspace_agent(request)
    try:
        contribution = await _service(request, agent).import_revision(
            investigation_id,
            workspace_id=body.workspace_id,
            workspace_revision_id=body.workspace_revision_id,
            declaration=declaration,
            authority=authority,
            idempotency_key=idempotency_key,
        )
    except ContributionValidationError as exc:
        raise _domain_error(exc) from exc
    except WorkContributionError as exc:
        raise _service_error(exc) from exc
    finally:
        if should_close:
            await agent.aclose()
    return contribution.to_payload()


@router.get("/{investigation_id}/contributions/{contribution_id}")
async def get_contribution(
    investigation_id: UUID, contribution_id: UUID, request: Request
) -> dict[str, Any]:
    await _require_verified_human_operator(request)
    agent, should_close = _workspace_agent(request)
    try:
        contribution = await _service(request, agent).get(investigation_id, contribution_id)
    except WorkContributionError as exc:
        raise _service_error(exc) from exc
    finally:
        if should_close:
            await agent.aclose()
    return contribution.to_payload()


__all__ = ["router"]
