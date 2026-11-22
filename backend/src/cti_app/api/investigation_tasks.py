"""Task, command and event routes of the M24 investigation workshop (04B3).

ADR-0011 D8 owns this surface: the task reads, task creation, the thirteen D3
commands and the per-task event journal.  The business state machine lives in
:mod:`cti_app.application.investigation_tasks`; this module only resolves the
actor, maps the command onto one engine call and surfaces the engine's typed
refusals (403/409/422).

Actor resolution is server-side only:

* a human identity must pass the canonical verified-human gate
  (``_require_verified_human_operator``), exactly like the bootstrap and
  governance routes;
* an ``OPERATOR_CLIENT`` identity must present ``X-AutoWork-Agent-Session``
  and resolve it through the :class:`AgentSessionVerifier` port.  Until lot
  05B1 provides per-session HMAC secrets the default verifier is fail-closed,
  so the shared ``OPERATOR_CLIENT`` token alone can reach no command route.
  The operator attribution is the verified binding's, never the request body's.

Status changes are never written here: a command calls the engine, which
applies the D3 matrix, the lease and the CAS and appends the task event.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Annotated, Any, Literal, cast
from uuid import UUID

from fastapi import APIRouter, Header, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from cti_app.api.agentic_tools import require_signed_agent_session
from cti_app.api.investigation import (
    _http_error,
    _identity,
    _require_investigation,
    _require_verified_human_operator,
    _uow_factory,
)
from cti_app.application.agent_session_verifier import VerifiedAgentSession
from cti_app.application.agentic_sessions import (
    HostSessionReleaseService,
    SessionUnitOfWork,
)
from cti_app.application.identity import IdentityScope
from cti_app.application.investigation_persistence import (
    AgentSessionRepository,
    InvestigationTaskRepository,
)
from cti_app.application.investigation_task_events import TaskEvent, TaskEventType
from cti_app.application.investigation_tasks import (
    InvestigationTaskService,
    TaskCommandError,
    TaskCommandResult,
    TaskSessionOutcome,
    TaskSessionRelease,
)
from cti_app.application.investigation_timeline import (
    InvestigationTimelineService,
    TimelinePageV1,
    decode_position_cursor,
    encode_position_cursor,
)
from cti_app.application.persistence import UnitOfWork
from cti_app.config import Settings, get_settings
from cti_app.domain.agentic_operator import AgentSessionState, AgentSessionStateError
from cti_app.domain.investigation import thaw_json
from cti_app.domain.investigation_task import (
    BlockedReason,
    InvestigationTaskV1,
    ResultVerificationStatus,
    TaskActor,
    TaskActorKind,
    TaskPriority,
    TaskStatus,
)

router = APIRouter(prefix="/api/investigations", tags=["investigation-tasks"])

TaskCommandName = Literal[
    "assign",
    "start",
    "heartbeat",
    "request_input",
    "submit",
    "accept_result",
    "request_changes",
    "block",
    "unblock",
    "resume",
    "cancel",
    "fail",
    "release",
]


def _settings(request: Request) -> Settings:
    configured = getattr(request.app.state, "settings", None)
    return configured if isinstance(configured, Settings) else get_settings()


class TaskActorView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    actor_kind: TaskActorKind
    actor_id: str


class TaskView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: UUID
    investigation_id: UUID
    task_key: str
    plan_version_id: UUID | None
    parent_task_id: UUID | None
    depth: int
    title: str
    question: str
    objective: str
    priority: TaskPriority
    claim_refs: list[str]
    evidence_refs: list[str]
    sample_refs: list[str]
    required_capabilities: list[str]
    allowed_workspace_paths: list[str]
    execution_policy: dict[str, Any]
    expected_outputs: list[str]
    status: TaskStatus
    status_version: int
    result_verification_status: ResultVerificationStatus
    blocked_reason: BlockedReason | None
    assigned_actor_kind: TaskActorKind | None
    assigned_actor_id: str | None
    lease_id: UUID | None
    lease_session_id: UUID | None
    lease_fence: int
    lease_expires_at: datetime | None
    heartbeat_at: datetime | None
    handoff_ref: UUID | None
    result_refs: list[Any]
    event_sequence: int
    created_by: TaskActorView
    created_at: datetime
    updated_at: datetime


class TaskEventView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: UUID
    investigation_id: UUID
    task_id: UUID
    sequence: int
    event_type: TaskEventType
    from_status: TaskStatus | None
    to_status: TaskStatus | None
    status_version: int
    lease_fence: int | None
    actor: TaskActorView
    idempotency_key: str
    analyst_decision_id: UUID | None
    payload: dict[str, Any]
    occurred_at: datetime


class TaskCommandResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    task: TaskView
    event: TaskEventView | None
    replayed: bool


class TaskCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_key: str = Field(min_length=1, max_length=200)
    title: str = Field(min_length=1, max_length=500)
    question: str = Field(default="", max_length=4_000)
    objective: str = Field(default="", max_length=4_000)
    priority: TaskPriority = TaskPriority.NORMAL
    parent_task_id: UUID | None = None
    plan_version_id: UUID | None = None
    claim_refs: list[str] = Field(default_factory=list, max_length=128)
    evidence_refs: list[str] = Field(default_factory=list, max_length=128)
    sample_refs: list[str] = Field(default_factory=list, max_length=128)
    required_capabilities: list[str] = Field(default_factory=list, max_length=128)
    allowed_workspace_paths: list[str] = Field(default_factory=list, max_length=128)
    execution_policy: dict[str, Any] = Field(default_factory=dict)
    expected_outputs: list[str] = Field(default_factory=list, max_length=128)


class TaskActorBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    actor_kind: Literal["human", "operator"]
    actor_id: str = Field(min_length=1, max_length=200)


class TaskCommandRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    command: TaskCommandName
    expected_status_version: int = Field(ge=1)
    target: TaskActorBody | None = None
    lease_fence: int | None = Field(default=None, ge=0)
    reason: str | None = Field(default=None, min_length=1, max_length=4_000)
    blocked_reason: BlockedReason | None = None
    result_refs: list[dict[str, Any]] = Field(default_factory=list, max_length=128)


class TaskPageView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    items: list[TaskView]
    total: int


class TaskEventPageView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    items: list[TaskEventView]
    next_cursor: str | None
    has_more: bool


def _task_view(task: InvestigationTaskV1) -> TaskView:
    return TaskView(
        id=task.id,
        investigation_id=task.investigation_id,
        task_key=task.task_key,
        plan_version_id=task.plan_version_id,
        parent_task_id=task.parent_task_id,
        depth=task.depth,
        title=task.title,
        question=task.question,
        objective=task.objective,
        priority=task.priority,
        claim_refs=list(task.claim_refs),
        evidence_refs=list(task.evidence_refs),
        sample_refs=list(task.sample_refs),
        required_capabilities=list(task.required_capabilities),
        allowed_workspace_paths=list(task.allowed_workspace_paths),
        execution_policy=thaw_json(task.execution_policy),
        expected_outputs=list(task.expected_outputs),
        status=task.status,
        status_version=task.status_version,
        result_verification_status=task.result_verification_status,
        blocked_reason=task.blocked_reason,
        assigned_actor_kind=task.assigned_actor_kind,
        assigned_actor_id=task.assigned_actor_id,
        lease_id=task.lease_id,
        lease_session_id=task.lease_session_id,
        lease_fence=task.lease_fence,
        lease_expires_at=task.lease_expires_at,
        heartbeat_at=task.heartbeat_at,
        handoff_ref=task.handoff_ref,
        result_refs=[thaw_json(item) for item in task.result_refs],
        event_sequence=task.event_sequence,
        created_by=TaskActorView(
            actor_kind=task.created_by.kind, actor_id=task.created_by.actor_id
        ),
        created_at=task.created_at,
        updated_at=task.updated_at,
    )


def _event_view(event: TaskEvent) -> TaskEventView:
    return TaskEventView(
        id=event.id,
        investigation_id=event.investigation_id,
        task_id=event.task_id,
        sequence=event.sequence,
        event_type=event.event_type,
        from_status=event.from_status,
        to_status=event.to_status,
        status_version=event.status_version,
        lease_fence=event.lease_fence,
        actor=TaskActorView(actor_kind=event.actor.kind, actor_id=event.actor.actor_id),
        idempotency_key=event.idempotency_key,
        analyst_decision_id=event.analyst_decision_id,
        payload=thaw_json(event.payload),
        occurred_at=event.occurred_at,
    )


_LEASE_COMMANDS = frozenset({"heartbeat", "request_input", "submit", "resume", "block", "fail"})


class _VerifiedSessionPort:
    """``TaskSessionPort`` for the session the route already verified.

    The D7 gate in :func:`_task_actor` established that this exact session is
    active and bound to the task; the engine only needs that answer plus the
    durable outcome write when a lease is released.
    """

    def __init__(
        self,
        verified: VerifiedAgentSession | None,
        sessions: AgentSessionRepository | None,
        releases: list[TaskSessionRelease] | None = None,
    ) -> None:
        self._verified = verified
        self._sessions = sessions
        self._releases = releases

    async def is_active_session(
        self, *, session_id: UUID, investigation_id: UUID, task_id: UUID
    ) -> bool:
        return (
            self._verified is not None
            and session_id == self._verified.session_id
            and investigation_id == self._verified.investigation_id
            and task_id == self._verified.task_id
        )

    async def close_session(
        self,
        *,
        session_id: UUID,
        investigation_id: UUID,
        task_id: UUID,
        outcome: TaskSessionOutcome,
        reason: str,
    ) -> None:
        if self._sessions is None:
            return
        await self._sessions.close_session(
            session_id=session_id,
            investigation_id=investigation_id,
            task_id=task_id,
            outcome=outcome.value,
            reason=reason,
        )
        if self._releases is not None:
            self._releases.append(
                TaskSessionRelease(
                    session_id=session_id,
                    investigation_id=investigation_id,
                    task_id=task_id,
                    outcome=outcome,
                    reason=reason,
                )
            )


def _task_service(
    uow: UnitOfWork, request: Request
) -> tuple[InvestigationTaskService, list[TaskSessionRelease]]:
    """The per-request engine plus the host releases its commit must flush."""
    releases: list[TaskSessionRelease] = []
    service = InvestigationTaskService(
        tasks=uow.investigation_tasks,
        decisions=uow.analyst_decisions,
        sessions=_VerifiedSessionPort(
            _verified_session(request), getattr(uow, "agent_sessions", None), releases
        ),
        limits=_settings(request).task_limits(),
    )
    return service, releases


def _release_service(request: Request) -> HostSessionReleaseService:
    """Host teardown after commit: no DB lock is held during the UDS call."""
    return HostSessionReleaseService(
        uow_factory=cast(Callable[[], SessionUnitOfWork], _uow_factory(request)),
        host=getattr(request.app.state, "host_sessions", None),
        wait_seconds=_settings(request).m24_host_release_wait_seconds,
    )


async def _task_actor(
    request: Request,
    investigation_id: UUID,
    *,
    task_id: UUID | None = None,
    require_current_lease: bool = False,
) -> TaskActor:
    """Resolve the caller without ever trusting a body-supplied identity.

    A request carrying the D7 session header is always an operator request,
    whatever bearer identity reached the middleware; the signature is the
    only thing that admits it.  The shared ``OPERATOR_CLIENT`` token alone is
    refused on every M24 command route.
    """
    if request.headers.get("x-autowork-agent-session"):
        verified = await require_signed_agent_session(
            request,
            investigation_id=investigation_id,
            task_id=task_id,
            require_current_lease=require_current_lease,
        )
        request.state.verified_agent_session = verified
        return TaskActor(kind=TaskActorKind.OPERATOR, actor_id=verified.operator_id)
    identity = await _identity(request)
    if identity.has_scope(IdentityScope.OPERATOR_CLIENT):
        raise _http_error(403, "agent_session_required")
    await _require_verified_human_operator(request)
    return TaskActor(kind=TaskActorKind.HUMAN, actor_id=identity.actor_id)


def _verified_session(request: Request) -> VerifiedAgentSession | None:
    value = getattr(request.state, "verified_agent_session", None)
    return value if isinstance(value, VerifiedAgentSession) else None


def _require_bound_task(request: Request, task_id: UUID) -> None:
    verified = _verified_session(request)
    if verified is not None and verified.task_id != task_id:
        raise _http_error(403, "agent_session_mismatch")


async def _require_task(
    tasks: InvestigationTaskRepository, investigation_id: UUID, task_id: UUID
) -> InvestigationTaskV1:
    task = await tasks.get(task_id)
    if task is None or task.investigation_id != investigation_id:
        raise _http_error(404, "task_not_found")
    return task


def _visible_to_operator(
    task: InvestigationTaskV1, by_id: Mapping[UUID, InvestigationTaskV1], operator_id: str
) -> bool:
    """An operator reads its own tasks and their subtasks (ADR-0011 D8)."""
    seen: set[UUID] = set()
    current: InvestigationTaskV1 | None = task
    while current is not None and current.id not in seen:
        seen.add(current.id)
        if (
            current.assigned_actor_kind is TaskActorKind.OPERATOR
            and current.assigned_actor_id == operator_id
        ):
            return True
        if current.parent_task_id is None:
            return False
        current = by_id.get(current.parent_task_id)
    return False


def _command_error(exc: TaskCommandError) -> Any:
    return _http_error(exc.status_code, exc.code, str(exc))


def _required_target(payload: TaskCommandRequest) -> TaskActor:
    if payload.target is None:
        raise _http_error(422, "invalid_command_body", "assign requires a target actor")
    return TaskActor(
        kind=TaskActorKind(payload.target.actor_kind), actor_id=payload.target.actor_id
    )


def _required_fence(payload: TaskCommandRequest) -> int:
    if payload.lease_fence is None:
        raise _http_error(422, "invalid_command_body", "this command requires lease_fence")
    return payload.lease_fence


def _required_reason(payload: TaskCommandRequest) -> str:
    if payload.reason is None:
        raise _http_error(422, "invalid_command_body", "this command requires a reason")
    return payload.reason


def _required_blocked_reason(payload: TaskCommandRequest) -> BlockedReason:
    if payload.blocked_reason is None:
        raise _http_error(422, "invalid_command_body", "block requires blocked_reason")
    return payload.blocked_reason


async def _record_session_lease(
    uow: UnitOfWork,
    verified: VerifiedAgentSession,
    task: InvestigationTaskV1,
) -> None:
    """Mirror the D3 lease on the session the generic ``start`` route used (M3).

    Without this write, an operator ``start`` through ``/tasks/{id}/commands``
    leaves ``agent_sessions.task_lease_*`` null and the broker refuses every
    later call.  It runs in the command transaction, so a mirror failure rolls
    the start back instead of leaving a task running with an unusable session.
    """
    sessions = getattr(uow, "agent_sessions", None)
    if sessions is None or task.lease_id is None or task.lease_fence < 1:
        return
    current = await sessions.get(verified.session_id)
    if (
        current is None
        or current.investigation_id != verified.investigation_id
        or current.task_id != task.id
        or verified.task_id != task.id
    ):
        raise _http_error(409, "agent_session_inactive")
    if current.task_lease_id == task.lease_id and current.task_lease_fence == task.lease_fence:
        return
    if current.state is AgentSessionState.READY:
        ready = current.with_state(AgentSessionState.RUNNING, now=task.updated_at)
        stored = await sessions.update_state(ready, expected_state_version=current.state_version)
        if stored is None:
            raise _http_error(409, "agent_session_changed")
        current = stored
    try:
        mirrored = current.with_task_lease(task.lease_id, task.lease_fence, now=task.updated_at)
    except AgentSessionStateError as exc:
        raise _http_error(409, "agent_session_changed", str(exc)) from exc
    if await sessions.update_state(mirrored, expected_state_version=current.state_version) is None:
        raise _http_error(409, "agent_session_changed")


async def _run_command(
    service: InvestigationTaskService,
    task: InvestigationTaskV1,
    payload: TaskCommandRequest,
    *,
    uow: UnitOfWork,
    actor: TaskActor,
    verified: VerifiedAgentSession | None,
    idempotency_key: str,
) -> TaskCommandResult:
    command = payload.command
    expected = payload.expected_status_version
    if command == "assign":
        # ``assign`` covers D3's ``ready -> assigned`` and, once a review
        # blocked the task, the human reassignment ``blocked -> assigned``.
        target = _required_target(payload)
        if task.status is TaskStatus.BLOCKED:
            return await service.reassign(
                task_id=task.id,
                actor=actor,
                target=target,
                expected_status_version=expected,
                idempotency_key=idempotency_key,
            )
        return await service.assign(
            task_id=task.id,
            actor=actor,
            target=target,
            expected_status_version=expected,
            idempotency_key=idempotency_key,
        )
    if command == "release":
        return await service.release(
            task_id=task.id,
            actor=actor,
            expected_status_version=expected,
            idempotency_key=idempotency_key,
        )
    if command == "start":
        session_id = verified.session_id if verified is not None else None
        started = await service.start(
            task_id=task.id,
            actor=actor,
            expected_status_version=expected,
            idempotency_key=idempotency_key,
            session_id=session_id,
        )
        if verified is not None:
            await _record_session_lease(uow, verified, started.task)
        return started
    if command == "heartbeat":
        return await service.heartbeat(
            task_id=task.id,
            actor=actor,
            expected_status_version=expected,
            lease_fence=_required_fence(payload),
            idempotency_key=idempotency_key,
        )
    if command == "request_input":
        return await service.request_input(
            task_id=task.id,
            actor=actor,
            expected_status_version=expected,
            lease_fence=_required_fence(payload),
            idempotency_key=idempotency_key,
        )
    if command == "resume":
        return await service.resume(
            task_id=task.id,
            actor=actor,
            expected_status_version=expected,
            lease_fence=_required_fence(payload),
            idempotency_key=idempotency_key,
        )
    if command == "submit":
        return await service.submit(
            task_id=task.id,
            actor=actor,
            expected_status_version=expected,
            idempotency_key=idempotency_key,
            result_refs=tuple(payload.result_refs),
            lease_fence=payload.lease_fence,
        )
    if command == "accept_result":
        return await service.accept_result(
            task_id=task.id,
            actor=actor,
            expected_status_version=expected,
            idempotency_key=idempotency_key,
            reason=_required_reason(payload),
        )
    if command == "request_changes":
        return await service.request_changes(
            task_id=task.id,
            actor=actor,
            expected_status_version=expected,
            idempotency_key=idempotency_key,
            reason=_required_reason(payload),
        )
    if command == "block":
        return await service.block(
            task_id=task.id,
            actor=actor,
            expected_status_version=expected,
            idempotency_key=idempotency_key,
            blocked_reason=_required_blocked_reason(payload),
            lease_fence=payload.lease_fence,
        )
    if command == "unblock":
        return await service.unblock(
            task_id=task.id,
            actor=actor,
            expected_status_version=expected,
            idempotency_key=idempotency_key,
        )
    if command == "cancel":
        return await service.cancel(
            task_id=task.id,
            actor=actor,
            expected_status_version=expected,
            idempotency_key=idempotency_key,
            reason=_required_reason(payload),
        )
    if command == "fail":
        return await service.fail(
            task_id=task.id,
            actor=actor,
            expected_status_version=expected,
            idempotency_key=idempotency_key,
            reason=_required_reason(payload),
            lease_fence=payload.lease_fence,
        )
    raise _http_error(422, "invalid_command_body", f"unknown command {command}")


@router.get("/{investigation_id}/tasks", response_model=TaskPageView)
async def list_tasks(investigation_id: UUID, request: Request) -> TaskPageView:
    actor = await _task_actor(request, investigation_id)
    async with _uow_factory(request)() as uow:
        await _require_investigation(uow, investigation_id)
        tasks = await uow.investigation_tasks.list_for_investigation(investigation_id)
    if actor.kind is TaskActorKind.HUMAN:
        visible = list(tasks)
    else:
        by_id = {task.id: task for task in tasks}
        visible = [task for task in tasks if _visible_to_operator(task, by_id, actor.actor_id)]
    return TaskPageView(items=[_task_view(task) for task in visible], total=len(visible))


@router.get("/{investigation_id}/tasks/{task_id}", response_model=TaskView)
async def get_task(investigation_id: UUID, task_id: UUID, request: Request) -> TaskView:
    actor = await _task_actor(request, investigation_id)
    _require_bound_task(request, task_id)
    async with _uow_factory(request)() as uow:
        await _require_investigation(uow, investigation_id)
        task = await _require_task(uow.investigation_tasks, investigation_id, task_id)
        if actor.kind is TaskActorKind.OPERATOR:
            all_tasks = await uow.investigation_tasks.list_for_investigation(investigation_id)
            by_id = {item.id: item for item in all_tasks}
            if not _visible_to_operator(task, by_id, actor.actor_id):
                raise _http_error(403, "operator_task_scope_denied")
    return _task_view(task)


@router.post("/{investigation_id}/tasks", response_model=TaskCommandResponse)
async def create_task(
    investigation_id: UUID,
    payload: TaskCreateRequest,
    request: Request,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=255)],
) -> TaskCommandResponse:
    actor = await _task_actor(request, investigation_id, require_current_lease=True)
    verified = _verified_session(request)
    if actor.kind is TaskActorKind.OPERATOR:
        # An operator only opens a proposed subtask of its own running task:
        # the session must be bound to the parent, and the engine refuses a
        # parent that is not assigned to this operator.
        if payload.parent_task_id is None or verified is None:
            raise _http_error(403, "agent_session_mismatch")
        if verified.task_id != payload.parent_task_id:
            raise _http_error(403, "agent_session_mismatch")
    async with _uow_factory(request)() as uow:
        await _require_investigation(uow, investigation_id)
        service, _releases = _task_service(uow, request)
        try:
            result = await service.create_task(
                investigation_id=investigation_id,
                actor=actor,
                idempotency_key=idempotency_key,
                task_key=payload.task_key,
                title=payload.title,
                question=payload.question,
                objective=payload.objective,
                priority=payload.priority,
                parent_task_id=payload.parent_task_id,
                plan_version_id=payload.plan_version_id,
                claim_refs=payload.claim_refs,
                evidence_refs=payload.evidence_refs,
                sample_refs=payload.sample_refs,
                required_capabilities=payload.required_capabilities,
                allowed_workspace_paths=payload.allowed_workspace_paths,
                execution_policy=payload.execution_policy,
                expected_outputs=payload.expected_outputs,
            )
        except TaskCommandError as exc:
            raise _command_error(exc) from exc
        await uow.commit()
    return TaskCommandResponse(
        task=_task_view(result.task),
        event=_event_view(result.event) if result.event is not None else None,
        replayed=result.replayed,
    )


@router.post("/{investigation_id}/tasks/{task_id}/commands", response_model=TaskCommandResponse)
async def run_task_command(
    investigation_id: UUID,
    task_id: UUID,
    payload: TaskCommandRequest,
    request: Request,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=255)],
) -> TaskCommandResponse:
    actor = await _task_actor(
        request,
        investigation_id,
        require_current_lease=payload.command in _LEASE_COMMANDS,
    )
    _require_bound_task(request, task_id)
    releases: list[TaskSessionRelease] = []
    async with _uow_factory(request)() as uow:
        await _require_investigation(uow, investigation_id)
        task = await _require_task(uow.investigation_tasks, investigation_id, task_id)
        service, releases = _task_service(uow, request)
        try:
            result = await _run_command(
                service,
                task,
                payload,
                uow=uow,
                actor=actor,
                verified=_verified_session(request),
                idempotency_key=idempotency_key,
            )
        except TaskCommandError as exc:
            raise _command_error(exc) from exc
        await uow.commit()
    if releases:
        # The transaction and its locks are released; only now may the host be
        # stopped (B2).  A host failure is recorded on the session, never
        # raised to the caller who already committed the lease release.
        await _release_service(request).flush(releases)
    return TaskCommandResponse(
        task=_task_view(result.task),
        event=_event_view(result.event) if result.event is not None else None,
        replayed=result.replayed,
    )


@router.get("/{investigation_id}/tasks/{task_id}/events", response_model=TaskEventPageView)
async def list_task_events(
    investigation_id: UUID,
    task_id: UUID,
    request: Request,
    cursor: str | None = Query(default=None, max_length=1_024),
    limit: int = Query(default=100, ge=1, le=500),
) -> TaskEventPageView:
    actor = await _task_actor(request, investigation_id)
    _require_bound_task(request, task_id)
    async with _uow_factory(request)() as uow:
        await _require_investigation(uow, investigation_id)
        task = await _require_task(uow.investigation_tasks, investigation_id, task_id)
        if actor.kind is TaskActorKind.OPERATOR:
            all_tasks = await uow.investigation_tasks.list_for_investigation(investigation_id)
            by_id = {item.id: item for item in all_tasks}
            if not _visible_to_operator(task, by_id, actor.actor_id):
                raise _http_error(403, "operator_task_scope_denied")
        events = await uow.investigation_tasks.list_events(task_id)
    decoded = decode_position_cursor(cursor, investigation_id=investigation_id)
    if cursor is not None and decoded is None:
        raise _http_error(422, "invalid_event_cursor")
    ordered = sorted(events, key=lambda event: (event.occurred_at, event.id))
    if decoded is not None:
        ordered = [
            event for event in ordered if (event.occurred_at, event.id) > (decoded[0], decoded[1])
        ]
    page = ordered[:limit]
    has_more = len(ordered) > limit
    next_cursor = (
        encode_position_cursor(
            investigation_id=investigation_id,
            occurred_at=page[-1].occurred_at,
            item_id=page[-1].id,
        )
        if has_more and page
        else None
    )
    return TaskEventPageView(
        items=[_event_view(event) for event in page],
        next_cursor=next_cursor,
        has_more=has_more,
    )


@router.get("/{investigation_id}/timeline", response_model=TimelinePageV1)
async def get_timeline(
    investigation_id: UUID,
    request: Request,
    cursor: str | None = Query(default=None, max_length=1_024),
    limit: int = Query(default=100, ge=1, le=500),
) -> TimelinePageV1:
    await _task_actor(request, investigation_id)
    async with _uow_factory(request)() as uow:
        await _require_investigation(uow, investigation_id)
        service = InvestigationTimelineService(
            tasks=uow.investigation_tasks,
            decisions=uow.analyst_decisions,
            observations=uow.evidence_observations,
            invocations=uow.tool_invocations,
            plans=uow.investigation_plans,
        )
        try:
            return await service.page(investigation_id, cursor=cursor, limit=limit)
        except ValueError as exc:
            raise _http_error(422, "invalid_timeline_cursor", str(exc)) from exc


__all__ = ["router"]
