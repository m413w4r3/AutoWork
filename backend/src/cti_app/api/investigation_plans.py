"""Plan, acceptance and consultation-ledger routes for an investigation (M24-04A2).

The orchestrator owns ``api/main.py``: this module only exports ``router``.
``POST /plans/request`` reuses the existing advisor tool path
(:class:`OperatorToolInvocationService`) with a human requester attribution and
the bootstrap snapshot as an explicit evidence reference: the dossier is built
once by the advisor tool, never a second time here.  ``POST /plans`` only
materialises an already persisted ``plan_investigation`` response, and
``POST /plans/{id}/accept`` writes the human ``plan_accept`` decision and the
tasks in one unit of work.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Annotated, Any, cast
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from cti_app.api.investigation import (
    _composition_factory,
    _http_error,
    _identity,
    _require_verified_human_operator,
    _uow_factory,
)
from cti_app.api.investigation_task_consultations import assemble_task_context
from cti_app.application.identity import IdentityScope
from cti_app.application.investigation_advisor_tools import ADVISOR_SCHEMA_VERSION
from cti_app.application.investigation_bootstrap import BOOTSTRAP_SNAPSHOT_OBJECT_TYPE
from cti_app.application.investigation_consultations import (
    InvestigationConsultationLedger,
    InvestigationConsultationV1,
    extract_advisor_consultation,
)
from cti_app.application.investigation_persistence import (
    EvidenceObservationRepository,
    ToolInvocationRepository,
    VersionConflictError,
)
from cti_app.application.investigation_plan_materialization import (
    InvestigationPlanMaterializationService,
    PlanMaterializationError,
    plan_accept_decision_id,
)
from cti_app.application.investigation_plan_parser import (
    PlanTextParseStatus,
    parse_investigation_plan_text,
)
from cti_app.application.investigation_plans import (
    PLAN_CONSULTATION_TYPE,
    InvestigationPlanService,
)
from cti_app.application.investigation_state import InvestigationStateVersionConflict
from cti_app.application.operator_tool_invocation import OperatorToolInvocationError
from cti_app.domain.editorial import (
    AnalystDecision,
    AnalystDecisionTargetType,
    AnalystDecisionType,
)
from cti_app.domain.investigation import (
    EvidenceObservation,
    ToolInvocation,
    ToolInvocationActorKind,
    ToolInvocationStatus,
)
from cti_app.domain.investigation_plan import InvestigationPlanVersion, PlanParseStatus
from cti_app.domain.investigation_task import InvestigationTaskV1

router = APIRouter(prefix="/api/investigations", tags=["investigation-plans"])

_ADVISOR_TOOL_NAME = "advisor.consult"
_ACCEPTED_INVOCATION_STATUSES = frozenset(
    {ToolInvocationStatus.SUCCEEDED, ToolInvocationStatus.PARTIAL}
)
_DEFAULT_PLAN_QUESTION = (
    "Établis un plan d'investigation à partir du contexte fourni : synthèse de "
    "l'article, pack d'entrée analyste, snapshot de bootstrap VT, IOC, "
    "observations et findings. Réponds en texte libre ou Markdown (le JSON "
    "n'est pas obligatoire). Décris chaque piste avec : titre, objectif, "
    "justification CTI, références de claims, preuves de départ, échantillons, "
    "outils, préconditions, intérêt, critères de conclusion, limites et "
    "dépendances éventuelles. Donne un identifiant de piste stable si possible. "
    "Ne demande aucune exécution de malware et n'élargis aucune autorisation."
)
_PLAN_REQUEST_PURPOSE = (
    "Demande humaine d'un plan d'investigation Bridge (M24), à partir du "
    "contexte déjà autorisé de l'enquête."
)


class PlanVersionSummaryV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan_version_id: UUID
    investigation_id: UUID
    version: int
    schema_version: str
    content_sha256: str
    parse_status: str
    source_invocation_id: UUID
    source_observation_id: UUID
    supersedes_id: UUID | None = None
    created_at: datetime
    lead_count: int
    current: bool


class PlanVersionDetailV1(PlanVersionSummaryV1):
    content: dict[str, object]


class PlanListV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    current_version: int | None = None
    plans: tuple[PlanVersionSummaryV1, ...]


class PlanRequestV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(ge=1)
    expected_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    question: str | None = Field(default=None, max_length=8_000)
    context_task_id: UUID | None = None


class PlanRequestResponseV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    investigation_id: UUID
    invocation_id: UUID
    invocation_status: str
    idempotent_replay: bool
    advisor_parse_status: str
    plan_parse_status: str
    plan_version_id: UUID | None = None
    plan_version: int | None = None
    missing_fields: tuple[str, ...] = ()
    advice_only: bool


class PlanMaterializeV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_invocation_id: UUID


class PlanMaterializeResponseV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    investigation_id: UUID
    plan_version_id: UUID
    plan_version: int
    parse_status: str
    lead_count: int
    created: bool


class PlanAcceptV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    reason: str = Field(default="Accept the investigation plan", min_length=1, max_length=4_000)


class PlanAcceptTaskV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: UUID
    task_key: str
    title: str
    status: str
    created: bool


class PlanAcceptResponseV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    investigation_id: UUID
    plan_version_id: UUID
    plan_version: int
    decision_id: UUID
    tasks: tuple[PlanAcceptTaskV1, ...]
    idempotent_replay: bool


class ConsultationLedgerV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    investigation_id: UUID
    consultations: tuple[InvestigationConsultationV1, ...]


async def _require_plan_reader(request: Request) -> Any:
    """Humans read with the verified gate; operator clients may read plans."""
    identity = await _identity(request)
    if identity.has_scope(IdentityScope.OPERATOR_CLIENT):
        return identity
    if identity.has_scope(IdentityScope.HUMAN_OPERATOR):
        return await _require_verified_human_operator(request)
    raise _http_error(403, "verified_human_operator_required")


def _plan_service(uow: Any) -> InvestigationPlanService:
    return InvestigationPlanService(
        plans=uow.investigation_plans,
        invocations=uow.tool_invocations,
        observations=uow.evidence_observations,
        investigations=uow.analyst_investigations,
    )


def _summary(
    version: InvestigationPlanVersion, current_version: int | None
) -> PlanVersionSummaryV1:
    return PlanVersionSummaryV1(
        plan_version_id=version.id,
        investigation_id=version.investigation_id,
        version=version.version,
        schema_version=version.schema_version,
        content_sha256=version.content_sha256,
        parse_status=version.parse_status.value,
        source_invocation_id=version.source_invocation_id,
        source_observation_id=version.source_observation_id,
        supersedes_id=version.supersedes_id,
        created_at=version.created_at,
        lead_count=len(version.content.leads),
        current=current_version == version.version,
    )


def _latest_bootstrap_snapshot(
    observations: Sequence[EvidenceObservation],
) -> EvidenceObservation | None:
    snapshots = [
        observation
        for observation in observations
        if observation.object_type == BOOTSTRAP_SNAPSHOT_OBJECT_TYPE
    ]
    if not snapshots:
        return None
    return max(snapshots, key=lambda observation: (observation.observed_at, str(observation.id)))


def _missing_fields(result_missing: Sequence[tuple[str | None, str]]) -> tuple[str, ...]:
    return tuple(f"{lead or 'lead'}:{field}" for lead, field in result_missing)


def _materialization_error(exc: PlanMaterializationError) -> HTTPException:
    return _http_error(422, exc.code, str(exc))


async def _plan_from_invocation(
    *,
    uow: Any,
    invocation: ToolInvocation,
    observations: Sequence[EvidenceObservation],
    artifact_store: Any,
) -> tuple[InvestigationPlanVersion | None, str, tuple[str, ...], str]:
    """Parse one persisted advisor answer; return ``(version, plan_status, missing, advisor)``."""
    extract = await extract_advisor_consultation(invocation, observations, artifact_store)
    existing_versions = await uow.investigation_plans.list_for_investigation(
        invocation.investigation_id
    )
    existing = next(
        (version for version in existing_versions if version.source_invocation_id == invocation.id),
        None,
    )
    if existing is not None:
        return existing, existing.parse_status.value, (), extract.parse_status.value
    if not extract.readable or extract.advisory_text is None:
        return None, PlanTextParseStatus.UNPARSED.value, (), extract.parse_status.value
    parsed = parse_investigation_plan_text(extract.advisory_text)
    missing = _missing_fields(
        [
            (status.lead_id or status.title, field)
            for status in parsed.lead_statuses
            for field in status.missing_fields
        ]
    )
    if parsed.parse_status is PlanTextParseStatus.UNPARSED:
        return None, parsed.parse_status.value, missing, extract.parse_status.value
    if extract.response_observation_id is None:
        return None, parsed.parse_status.value, missing, extract.parse_status.value
    existing = await uow.investigation_plans.get_by_source(
        invocation.id, extract.response_observation_id
    )
    if existing is not None:
        return existing, existing.parse_status.value, missing, extract.parse_status.value
    head = await uow.investigation_plans.get_head(invocation.investigation_id)
    version_number = 1 if head is None else head.current_version + 1
    plan = parsed.to_plan(
        investigation_id=invocation.investigation_id,
        version=version_number,
    )
    if plan is None:
        return None, parsed.parse_status.value, missing, extract.parse_status.value
    stored = await _plan_service(uow).create_version(
        plan,
        parse_status=_domain_parse_status(parsed.parse_status),
        source_invocation_id=invocation.id,
        source_observation_id=extract.response_observation_id,
    )
    return stored, stored.parse_status.value, missing, extract.parse_status.value


def _domain_parse_status(status: PlanTextParseStatus) -> PlanParseStatus:
    return PlanParseStatus(status.value)


def _accepted_invocation(
    invocation: ToolInvocation | None, investigation_id: UUID
) -> ToolInvocation:
    if invocation is None or invocation.investigation_id != investigation_id:
        raise _http_error(404, "plan_source_invocation_not_found")
    if invocation.tool_name != _ADVISOR_TOOL_NAME:
        raise _http_error(422, "plan_source_invalid", "The source is not an advisor consultation")
    if invocation.normalized_parameters.get("consultation_type") != PLAN_CONSULTATION_TYPE:
        raise _http_error(
            422, "plan_source_invalid", "The source is not a plan_investigation consultation"
        )
    if invocation.status not in _ACCEPTED_INVOCATION_STATUSES:
        raise _http_error(
            422, "plan_source_invalid", "The source consultation has no usable advisor output"
        )
    return invocation


@router.get(
    "/{investigation_id}/plans",
    response_model=PlanListV1,
)
async def list_plans(investigation_id: UUID, request: Request) -> PlanListV1:
    await _require_plan_reader(request)
    async with _uow_factory(request)() as uow:
        investigation = await uow.analyst_investigations.get(investigation_id)
        if investigation is None:
            raise _http_error(404, "investigation_not_found")
        head = await uow.investigation_plans.get_head(investigation_id)
        current_version = head.current_version if head is not None else None
        versions = await uow.investigation_plans.list_for_investigation(investigation_id)
        return PlanListV1(
            current_version=current_version,
            plans=tuple(_summary(version, current_version) for version in versions),
        )


@router.get(
    "/{investigation_id}/plans/{plan_version_id}",
    response_model=PlanVersionDetailV1,
)
async def get_plan(
    investigation_id: UUID, plan_version_id: UUID, request: Request
) -> PlanVersionDetailV1:
    await _require_plan_reader(request)
    async with _uow_factory(request)() as uow:
        version = await uow.investigation_plans.get_version(plan_version_id)
        if version is None or version.investigation_id != investigation_id:
            raise _http_error(404, "plan_not_found")
        head = await uow.investigation_plans.get_head(investigation_id)
        summary = _summary(version, head.current_version if head is not None else None)
        return PlanVersionDetailV1(
            **summary.model_dump(),
            content=version.content.to_payload(),
        )


@router.get(
    "/{investigation_id}/consultations",
    response_model=ConsultationLedgerV1,
)
async def list_consultations(
    investigation_id: UUID, request: Request, task_id: UUID | None = None
) -> ConsultationLedgerV1:
    await _require_plan_reader(request)
    factory = _composition_factory(request)
    async with _uow_factory(request)() as uow:
        investigation = await uow.analyst_investigations.get(investigation_id)
        if investigation is None:
            raise _http_error(404, "investigation_not_found")
        ledger = InvestigationConsultationLedger(
            invocations=cast(ToolInvocationRepository, uow.tool_invocations),
            observations=cast(EvidenceObservationRepository, uow.evidence_observations),
            artifact_store=factory.artifact_store,
            decisions=uow.analyst_decisions,
        )
        consultations = await ledger.list_for_investigation(
            investigation_id, context_task_id=task_id
        )
        return ConsultationLedgerV1(
            investigation_id=investigation_id,
            consultations=consultations,
        )


@router.post(
    "/{investigation_id}/plans/request",
    response_model=PlanRequestResponseV1,
)
async def request_plan(
    investigation_id: UUID,
    payload: PlanRequestV1,
    request: Request,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=255)],
) -> PlanRequestResponseV1:
    identity = await _require_verified_human_operator(request)
    factory = _composition_factory(request)
    async with _uow_factory(request)() as uow:
        investigation = await uow.analyst_investigations.get(investigation_id)
        if investigation is None:
            raise _http_error(404, "investigation_not_found")
        observations = await uow.evidence_observations.list_for_investigation(investigation_id)
        snapshot = _latest_bootstrap_snapshot(observations)
        context_task: InvestigationTaskV1 | None = None
        if payload.context_task_id is not None:
            context_task = await uow.investigation_tasks.get(payload.context_task_id)
            if context_task is None or context_task.investigation_id != investigation_id:
                raise _http_error(404, "task_not_found")
        arguments: dict[str, Any] = {
            "consultation_type": PLAN_CONSULTATION_TYPE,
            "question": (payload.question or "").strip() or _DEFAULT_PLAN_QUESTION,
        }
        if snapshot is not None:
            arguments["evidence_refs"] = [str(snapshot.id)]
        if context_task is not None:
            arguments["task_context"] = await assemble_task_context(
                uow=uow,
                task=context_task,
                factory=factory,
                investigation=investigation,
            )
        composition = factory.build(uow, investigation_id, operator_mode=True)
        try:
            invocation_result = await composition.operator_tool_invocations.invoke(
                tool_name=_ADVISOR_TOOL_NAME,
                schema_version=ADVISOR_SCHEMA_VERSION,
                arguments=arguments,
                purpose=_PLAN_REQUEST_PURPOSE,
                expected_version=payload.expected_version,
                expected_sha256=payload.expected_sha256,
                idempotency_key=idempotency_key,
                actor_id=identity.actor_id,
                requester_actor_kind=ToolInvocationActorKind.HUMAN,
                context_task_id=context_task.id if context_task is not None else None,
            )
        except OperatorToolInvocationError as exc:
            raise _http_error(exc.status_code, exc.code, details=exc.details) from exc
        except (VersionConflictError, InvestigationStateVersionConflict) as exc:
            raise _http_error(409, "stale_working_state") from exc
        observations = await uow.evidence_observations.list_for_investigation(investigation_id)
        try:
            version, plan_status, missing, advisor_status = await _plan_from_invocation(
                uow=uow,
                invocation=invocation_result.invocation,
                observations=observations,
                artifact_store=factory.artifact_store,
            )
        except VersionConflictError as exc:
            raise _http_error(409, "plan_version_conflict") from exc
        await uow.commit()
        return PlanRequestResponseV1(
            investigation_id=investigation_id,
            invocation_id=invocation_result.invocation.id,
            invocation_status=invocation_result.invocation.status.value,
            idempotent_replay=invocation_result.idempotent_replay,
            advisor_parse_status=advisor_status,
            plan_parse_status=plan_status,
            plan_version_id=version.id if version is not None else None,
            plan_version=version.version if version is not None else None,
            missing_fields=missing,
            advice_only=version is None,
        )


@router.post(
    "/{investigation_id}/plans",
    response_model=PlanMaterializeResponseV1,
)
async def materialize_plan(
    investigation_id: UUID,
    payload: PlanMaterializeV1,
    request: Request,
) -> PlanMaterializeResponseV1:
    await _require_verified_human_operator(request)
    factory = _composition_factory(request)
    async with _uow_factory(request)() as uow:
        investigation = await uow.analyst_investigations.get(investigation_id)
        if investigation is None:
            raise _http_error(404, "investigation_not_found")
        invocation = _accepted_invocation(
            await uow.tool_invocations.get(payload.source_invocation_id), investigation_id
        )
        existing_versions = await uow.investigation_plans.list_for_investigation(investigation_id)
        for existing in existing_versions:
            if existing.source_invocation_id == invocation.id:
                return PlanMaterializeResponseV1(
                    investigation_id=investigation_id,
                    plan_version_id=existing.id,
                    plan_version=existing.version,
                    parse_status=existing.parse_status.value,
                    lead_count=len(existing.content.leads),
                    created=False,
                )
        observations = await uow.evidence_observations.list_for_investigation(investigation_id)
        version, _plan_status, _missing, _advisor = await _plan_from_invocation(
            uow=uow,
            invocation=invocation,
            observations=observations,
            artifact_store=factory.artifact_store,
        )
        if version is None:
            raise _http_error(
                422,
                "plan_unparsed",
                "The persisted advisor answer contains no usable plan",
            )
        await uow.commit()
        return PlanMaterializeResponseV1(
            investigation_id=investigation_id,
            plan_version_id=version.id,
            plan_version=version.version,
            parse_status=version.parse_status.value,
            lead_count=len(version.content.leads),
            created=True,
        )


@router.post(
    "/{investigation_id}/plans/{plan_version_id}/accept",
    response_model=PlanAcceptResponseV1,
)
async def accept_plan(
    investigation_id: UUID,
    plan_version_id: UUID,
    payload: PlanAcceptV1,
    request: Request,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=255)],
) -> PlanAcceptResponseV1:
    identity = await _require_verified_human_operator(request)
    async with _uow_factory(request)() as uow:
        investigation = await uow.analyst_investigations.get(investigation_id)
        if investigation is None:
            raise _http_error(404, "investigation_not_found")
        version = await uow.investigation_plans.get_version(plan_version_id)
        if version is None or version.investigation_id != investigation_id:
            raise _http_error(404, "plan_not_found")
        decision_id = plan_accept_decision_id(investigation_id, idempotency_key)
        decisions = await uow.analyst_decisions.list_for_investigation(investigation_id)
        prior = next((decision for decision in decisions if decision.id == decision_id), None)
        if prior is not None:
            if (
                prior.decision_type is not AnalystDecisionType.PLAN_ACCEPT
                or prior.target_id != version.id
            ):
                raise _http_error(409, "idempotency_conflict")
            task_keys = tuple(str(key) for key in prior.details.get("task_keys", ()))
            tasks = await uow.investigation_tasks.list_for_investigation(investigation_id)
            selected = {task.task_key: task for task in tasks if task.task_key in set(task_keys)}
            return _accept_response(
                investigation_id,
                version,
                decision_id,
                tuple(selected[key] for key in task_keys if key in selected),
                created_ids=(),
                replay=True,
            )
        head = await uow.investigation_plans.get_head(investigation_id)
        if head is None or head.current_version != version.version:
            raise _http_error(409, "stale_plan_version")
        if (
            payload.expected_sha256 is not None
            and version.content_sha256 != payload.expected_sha256
        ):
            raise _http_error(409, "stale_plan_version")

        materializer = InvestigationPlanMaterializationService(tasks=uow.investigation_tasks)
        try:
            plan = await materializer.prepare(version)
        except PlanMaterializationError as exc:
            raise _materialization_error(exc) from exc
        decision = AnalystDecision(
            id=decision_id,
            investigation_id=investigation_id,
            decision_type=AnalystDecisionType.PLAN_ACCEPT,
            target_type=AnalystDecisionTargetType.PLAN_VERSION,
            target_id=version.id,
            actor_id=identity.actor_id,
            reason=payload.reason.strip(),
            correlation_id=f"plan_accept:{investigation_id}:{idempotency_key}"[:128],
            details={
                "plan_version_id": str(version.id),
                "plan_version": version.version,
                "content_sha256": version.content_sha256,
                "task_keys": list(plan.task_keys),
            },
        )
        await uow.analyst_decisions.append(decision)
        try:
            outcome = await materializer.apply(plan, decision_id=decision_id)
        except PlanMaterializationError as exc:
            raise _materialization_error(exc) from exc
        except VersionConflictError as exc:
            raise _http_error(409, "plan_materialization_conflict") from exc
        await uow.commit()
        return _accept_response(
            investigation_id,
            version,
            decision_id,
            outcome.tasks,
            created_ids=outcome.created_task_ids,
            replay=False,
        )


def _accept_response(
    investigation_id: UUID,
    version: InvestigationPlanVersion,
    decision_id: UUID,
    tasks: Sequence[InvestigationTaskV1],
    *,
    created_ids: Sequence[UUID],
    replay: bool,
) -> PlanAcceptResponseV1:
    created = set(created_ids)
    return PlanAcceptResponseV1(
        investigation_id=investigation_id,
        plan_version_id=version.id,
        plan_version=version.version,
        decision_id=decision_id,
        tasks=tuple(
            PlanAcceptTaskV1(
                task_id=task.id,
                task_key=task.task_key,
                title=task.title,
                status=task.status.value,
                created=task.id in created,
            )
            for task in tasks
        ),
        idempotent_replay=replay,
    )
