"""Agentic tool broker (M24-05B2, ADR-0011 D7/D8/D10 D04/D09).

The broker is the only path an agentic operator has to AutoWork tools.  It
**composes** :class:`~cti_app.application.operator_tool_invocation.OperatorToolInvocationService`
instead of introducing a second journal, budget or evidence pipeline:

* the D7 session has already been verified by the route; the broker re-checks
  that the session's task carries a *current* lease fence, so an expired agent
  can make no call at all (reads of context stay on their own routes);
* the agentic profile refuses ``opinion.*`` server-side (Bridge owns
  publishable prose) and any tool the host shim catalogue cannot request, so
  no arbitrary shell/host command exists;
* the idempotency key is ``agent:<session>:<task>:<lease_fence>:<client_key>``:
  a replay returns the recorded invocation, while a changed task or fence can
  never replay an older one;
* the registry tools (``advisor.consult``, VT, Shodan, sample/archive) run
  through the existing ``ToolRegistry``/``ToolDispatcher``/G2/budget path with
  ``task_id``/``agent_session_id``/operator attribution on the
  ``tool_invocations`` row;
* ``task.heartbeat``/``task.submit_result`` map onto the task engine and
  ``handoff.submit`` onto the handoff service, for this session's task only;
* ``analysis.run_script`` is a registry tool like VT/Shodan: the attested
  worker capability (``analysis.exec``) and G2/budgets stay authoritative;
* ``contribution.import`` reuses the M24-07 import service with an
  ``origin='agent'`` authority derived from the verified session and the locked
  task row, then appends the ``contribution_imported`` task event;
* ``note.capture`` publishes the agent's own note/result through the host
  workspace layer (origin ``agent`` + task header), never as evidence.

An ambiguous ``advisor.consult`` is never resubmitted here: the underlying
service's reconciliation refusal is propagated unchanged.
"""

from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from cti_app.application.agent_session_verifier import VerifiedAgentSession
from cti_app.application.agentic_note_tools import (
    NOTE_CAPTURE_TOOL,
    AgenticNoteCaptureService,
    NoteCaptureError,
)
from cti_app.application.investigation_handoffs import (
    HandoffCreateRequest,
    InvestigationHandoffService,
)
from cti_app.application.investigation_persistence import (
    AgentSessionRepository,
    InvestigationTaskRepository,
)
from cti_app.application.investigation_state import InvestigationStateRepository
from cti_app.application.investigation_task_events import (
    TaskEvent,
    TaskEventDraft,
    TaskEventType,
    append_task_event,
)
from cti_app.application.investigation_tasks import (
    InvestigationTaskService,
    TaskSessionOutcome,
)
from cti_app.application.operator_tool_invocation import (
    OperatorToolInvocationResult,
    OperatorToolInvocationService,
)
from cti_app.application.persistence import AnalystDecisionRepository
from cti_app.application.work_contributions import (
    ContributionAuthority,
    WorkContributionError,
    WorkContributionService,
)
from cti_app.application.workspace_agent import WorkspaceAgentError
from cti_app.domain.investigation import (
    FindingAssessment,
    FindingKind,
    NoveltyClass,
    ToolInvocationActorKind,
)
from cti_app.domain.investigation_handoff import HandoffActor, HandoffActorKind
from cti_app.domain.investigation_task import (
    TASK_LIMIT_DEFAULTS,
    InvestigationTaskV1,
    TaskActor,
    TaskActorKind,
    TaskLimits,
    TaskStatus,
)
from cti_app.domain.production import ProseAuthority
from cti_app.domain.work_contribution import (
    ContributionDeclarationV1,
    FindingDeclarationV1,
    ObservationDeclarationV1,
    WorkContributionCategory,
    WorkContributionMethod,
    WorkContributionObjectType,
    WorkContributionOrigin,
    WorkContributionV1,
)

#: Registry-backed tools the host shim catalogue can request (see
#: ``sidecars/workspace_agent/sessions/catalog.py``; the cross-contract test
#: keeps both lists aligned).  The shim's ``advisor.consult`` is the only
#: Bridge entry; ``bridge.consult`` is accepted as an alias of it.
AGENTIC_REGISTRY_TOOLS: frozenset[str] = frozenset(
    {
        "advisor.consult",
        "analysis.run_script",
        "vt.file_report",
        "vt.object_report",
        "vt.relationships",
        "vt.intelligence_search",
        "vt.behaviour",
        "vt.behaviour_pcap",
        "vt.download_sample",
        "sample.analyze",
        "archive.extract_member",
        "shodan.host",
        "shodan.domain",
        "shodan.count",
        "shodan.search",
    }
)
AGENTIC_TASK_TOOLS: frozenset[str] = frozenset({"task.heartbeat", "task.submit_result"})
AGENTIC_HANDOFF_TOOL = "handoff.submit"
AGENTIC_CONTRIBUTION_TOOL = "contribution.import"
AGENTIC_TOOL_NAMES: frozenset[str] = (
    AGENTIC_REGISTRY_TOOLS
    | AGENTIC_TASK_TOOLS
    | {AGENTIC_HANDOFF_TOOL, AGENTIC_CONTRIBUTION_TOOL, NOTE_CAPTURE_TOOL}
)
BRIDGE_CONSULT_ALIAS = "bridge.consult"
OPINION_TOOL_NAMES: frozenset[str] = frozenset(
    {"opinion.compose", "opinion.revise", "opinion.note"}
)
AGENTIC_IDEMPOTENCY_PREFIX = "agent"
AGENTIC_IDEMPOTENCY_MAX_LENGTH = 255


class AgenticBrokerError(ValueError):
    """Base typed refusal of the broker; the route maps it to HTTP."""

    code = "agentic_tool_refused"
    status_code = 403

    def __init__(self, message: str, *, details: dict[str, str] | None = None) -> None:
        self.details = details or {}
        super().__init__(message)


class AgenticToolNotAllowedError(AgenticBrokerError):
    """The tool is outside the closed catalogue the host shim can request."""

    code = "agentic_tool_not_allowed"


class AgenticProseAuthorityRequiredError(AgenticBrokerError):
    """``opinion.*`` belongs to Bridge under the bridge_only prose authority."""

    code = "bridge_authorship_required"


class AgenticBindingRefusedError(AgenticBrokerError):
    """The verified session does not match the task or investigation."""

    code = "agent_session_binding_mismatch"


class AgenticLeaseRefusedError(AgenticBrokerError):
    """The task lease is missing, expired or superseded: every call is refused."""

    code = "agent_session_lease_stale"
    status_code = 409


class AgenticSessionInactiveError(AgenticBrokerError):
    """The session row is no longer active or no longer bound to the task."""

    code = "agent_session_inactive"
    status_code = 409


class AgenticArgumentsInvalidError(AgenticBrokerError):
    code = "agentic_tool_arguments_invalid"
    status_code = 422


class AgenticContributionRefusedError(AgenticBrokerError):
    """The M24-07 import service refused the brokered contribution."""

    code = "work_contribution_invalid"

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        status_code: int | None = None,
        details: Mapping[str, str] | None = None,
    ) -> None:
        if code is not None:
            self.code = code
        if status_code is not None:
            self.status_code = status_code
        super().__init__(message, details=dict(details or {}))


class AgenticNoteCaptureRefusedError(AgenticBrokerError):
    """``note.capture`` refused or the host workspace agent failed."""

    code = "note_capture_refused"

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        status_code: int | None = None,
        details: Mapping[str, str] | None = None,
    ) -> None:
        if code is not None:
            self.code = code
        if status_code is not None:
            self.status_code = status_code
        super().__init__(message, details=dict(details or {}))


class _ContributionObservationArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    object_type: WorkContributionObjectType
    method: WorkContributionMethod
    object_id: str = Field(min_length=1, max_length=512)
    tool: str = Field(min_length=1, max_length=128)
    tool_version: str = Field(min_length=1, max_length=64)
    parameters: dict[str, Any] = Field(default_factory=dict)
    limitations: tuple[str, ...] = ()
    observed_at: datetime | None = None


class _ContributionFindingArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: FindingKind
    assessment: FindingAssessment
    statement: str = Field(min_length=1)
    observation_refs: tuple[UUID, ...] = ()
    claim_refs: tuple[str, ...] = ()
    novelty: NoveltyClass | None = None
    scope: str = ""
    limitations: tuple[str, ...] = ()
    proposed_sample_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class AgenticContributionImportArguments(BaseModel):
    """The closed ``contribution.import`` payload (M24-07 request minus authority).

    ``origin``, ``author_actor_id``, ``task_id`` and ``lease_fence`` are never
    accepted from the agent: the broker derives them from the verified session
    and the locked task row so an agent cannot claim another task's lease.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    workspace_id: str = Field(pattern=r"^ws-[0-9a-f]{32}$")
    workspace_revision_id: str = Field(pattern=r"^rev-[0-9a-f]{32}$")
    category: WorkContributionCategory = WorkContributionCategory.WORK_CONTRIBUTION
    observation: _ContributionObservationArguments | None = None
    finding: _ContributionFindingArguments | None = None

    @model_validator(mode="after")
    def _validate_category_shape(self) -> AgenticContributionImportArguments:
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

    def declaration(self) -> ContributionDeclarationV1:
        observation = (
            ObservationDeclarationV1(
                object_type=self.observation.object_type,
                method=self.observation.method,
                object_id=self.observation.object_id,
                tool=self.observation.tool,
                tool_version=self.observation.tool_version,
                parameters=self.observation.parameters,
                limitations=self.observation.limitations,
                observed_at=self.observation.observed_at,
            )
            if self.observation is not None
            else None
        )
        finding = (
            FindingDeclarationV1(
                kind=self.finding.kind,
                assessment=self.finding.assessment,
                statement=self.finding.statement,
                observation_refs=self.finding.observation_refs,
                claim_refs=self.finding.claim_refs,
                novelty=self.finding.novelty,
                scope=self.finding.scope,
                limitations=self.finding.limitations,
                proposed_sample_sha256=self.finding.proposed_sample_sha256,
            )
            if self.finding is not None
            else None
        )
        return ContributionDeclarationV1(
            category=self.category, observation=observation, finding=finding
        )


class AgenticNoteCaptureArguments(BaseModel):
    """The closed ``note.capture`` payload; the backend builds the full path."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    target: str = Field(default="note", pattern=r"^(?:note|result)$")
    relpath: str = Field(min_length=1, max_length=1024)
    text: str = Field(min_length=1)
    expected_sha256: str = Field(default="new", pattern=r"^(?:new|[0-9a-f]{64})$")


class AgenticBrokerUnavailableError(AgenticBrokerError):
    code = "agentic_broker_unavailable"
    status_code = 503


class AgenticInvestigationNotStartedError(AgenticBrokerError):
    """The investigation has no working state the call could be attributed to."""

    code = "investigation_not_started"
    status_code = 409


class AgenticBrokerUnitOfWork(Protocol):
    """The canonical stores one brokered call reads or writes."""

    investigation_tasks: InvestigationTaskRepository
    investigation_states: InvestigationStateRepository
    analyst_decisions: AnalystDecisionRepository
    agent_sessions: AgentSessionRepository

    async def commit(self) -> None: ...


@dataclass(frozen=True, slots=True)
class AgenticToolResult:
    """One brokered call's bounded, JSON-safe result."""

    investigation_id: UUID
    session_id: UUID
    task_id: UUID
    tool_name: str
    status: str
    idempotent_replay: bool
    invocation_id: UUID | None = None
    output: Mapping[str, Any] = field(default_factory=dict)
    error_code: str | None = None


def agentic_idempotency_key(
    *,
    session_id: UUID,
    task_id: UUID,
    lease_fence: int,
    client_key: str,
) -> str:
    """The D7/M3 bound replay key; a changed task or fence cannot replay."""
    if not isinstance(client_key, str) or not client_key.strip():
        raise AgenticArgumentsInvalidError("a client Idempotency-Key is required")
    normalized = client_key.strip()
    key = f"{AGENTIC_IDEMPOTENCY_PREFIX}:{session_id}:{task_id}:{lease_fence}:{normalized}"
    if len(key) > AGENTIC_IDEMPOTENCY_MAX_LENGTH:
        raise AgenticArgumentsInvalidError("the bound idempotency key exceeds 255 characters")
    return key


class _BoundSessionPort:
    """``TaskSessionPort`` for one already-verified session.

    The engine only asks whether *this* session is active and, on lease release,
    records the outcome on the session row (lot 05B1's aggregate).
    """

    def __init__(
        self,
        session: VerifiedAgentSession,
        sessions: AgentSessionRepository | None,
    ) -> None:
        self._session = session
        self._sessions = sessions

    async def is_active_session(
        self, *, session_id: UUID, investigation_id: UUID, task_id: UUID
    ) -> bool:
        return (
            session_id == self._session.session_id
            and investigation_id == self._session.investigation_id
            and task_id == self._session.task_id
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


class AgenticToolBroker:
    """Composes the operator invocation service for one verified session."""

    def __init__(
        self,
        *,
        investigation_id: UUID,
        prose_authority: ProseAuthority,
        uow: AgenticBrokerUnitOfWork,
        invocations: OperatorToolInvocationService | None,
        handoffs: InvestigationHandoffService | None,
        contributions: WorkContributionService | None = None,
        notes: AgenticNoteCaptureService | None = None,
        task_limits: TaskLimits = TASK_LIMIT_DEFAULTS,
        clock: Callable[[], datetime] | None = None,
        commit: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        if not isinstance(investigation_id, UUID):
            raise TypeError("investigation_id must be a UUID")
        if not isinstance(prose_authority, ProseAuthority):
            raise TypeError("prose_authority must use ProseAuthority")
        self._investigation_id = investigation_id
        self._prose_authority = prose_authority
        self._uow = uow
        self._invocations = invocations
        self._handoffs = handoffs
        self._contributions = contributions
        self._notes = notes
        self._task_limits = task_limits
        self._clock = clock or (lambda: datetime.now(UTC))
        self._commit = commit

    async def invoke(
        self,
        *,
        session: VerifiedAgentSession,
        tool_name: str,
        schema_version: str,
        arguments: Mapping[str, Any],
        client_idempotency_key: str,
        now: datetime | None = None,
    ) -> AgenticToolResult:
        if not isinstance(session, VerifiedAgentSession):
            raise TypeError("session must use VerifiedAgentSession")
        if not isinstance(tool_name, str) or not tool_name.strip():
            raise AgenticArgumentsInvalidError("tool_name must be a non-empty string")
        if not isinstance(schema_version, str) or not schema_version.strip():
            raise AgenticArgumentsInvalidError("schema_version must be a non-empty string")
        if not isinstance(arguments, Mapping):
            raise AgenticArgumentsInvalidError("arguments must be a JSON object")
        canonical_tool = "advisor.consult" if tool_name == BRIDGE_CONSULT_ALIAS else tool_name
        if canonical_tool in OPINION_TOOL_NAMES:
            # D4: an agentic session never writes publishable prose, whatever
            # the prompt says; Bridge is the sole author.  The profile refuses
            # the tool for every investigation, and a bridge_only investigation
            # additionally requires verified Bridge provenance from D4.
            raise AgenticProseAuthorityRequiredError(
                "opinion.* is reserved to Bridge for an agentic session"
                if self._prose_authority is not ProseAuthority.BRIDGE_ONLY
                else "bridge_only prose needs verified Bridge provenance, not an operator"
            )
        if canonical_tool not in AGENTIC_TOOL_NAMES:
            raise AgenticToolNotAllowedError(f"{tool_name} is not in the agentic tool catalogue")
        if session.investigation_id != self._investigation_id:
            raise AgenticBindingRefusedError("the agent session belongs to another investigation")
        timestamp = self._clock() if now is None else now
        task = await self._current_lease_task(session, now=timestamp)
        operator_id = task.assigned_actor_id
        if operator_id is None:  # pragma: no cover - checked by the lease gate
            raise AgenticBindingRefusedError("the task is not assigned to an operator")
        key = agentic_idempotency_key(
            session_id=session.session_id,
            task_id=task.id,
            lease_fence=task.lease_fence,
            client_key=client_idempotency_key,
        )
        if canonical_tool in AGENTIC_TASK_TOOLS:
            return await self._task_command(
                session=session,
                task=task,
                operator_id=operator_id,
                tool_name=canonical_tool,
                arguments=arguments,
                idempotency_key=key,
                now=timestamp,
            )
        if canonical_tool == AGENTIC_HANDOFF_TOOL:
            return await self._handoff(
                session=session,
                task=task,
                operator_id=operator_id,
                arguments=arguments,
                idempotency_key=key,
            )
        if canonical_tool == AGENTIC_CONTRIBUTION_TOOL:
            return await self._contribution(
                session=session,
                task=task,
                operator_id=operator_id,
                arguments=arguments,
                idempotency_key=key,
                now=timestamp,
            )
        if canonical_tool == NOTE_CAPTURE_TOOL:
            return await self._note_capture(
                session=session,
                task=task,
                arguments=arguments,
                idempotency_key=key,
            )
        return await self._registry_tool(
            session=session,
            task=task,
            operator_id=operator_id,
            tool_name=canonical_tool,
            schema_version=schema_version,
            arguments=arguments,
            idempotency_key=key,
        )

    # ------------------------------------------------------------- access gate

    async def _current_lease_task(
        self, session: VerifiedAgentSession, *, now: datetime
    ) -> InvestigationTaskV1:
        # Defense in depth on top of the route's D7 verification: the session
        # row itself must still be active and bound to this task.
        session_row = await self._uow.agent_sessions.get(session.session_id)
        if (
            session_row is None
            or session_row.investigation_id != self._investigation_id
            or session_row.task_id != session.task_id
            or not session_row.is_active
        ):
            raise AgenticSessionInactiveError("the agent session is not active")
        task = await self._uow.investigation_tasks.get(session.task_id)
        if task is None:
            raise AgenticBindingRefusedError("the bound task no longer exists")
        if task.investigation_id != self._investigation_id:
            raise AgenticBindingRefusedError("the bound task belongs to another investigation")
        if task.assigned_actor_kind is not TaskActorKind.OPERATOR:
            raise AgenticBindingRefusedError("the task is not assigned to an operator")
        # An expired or superseded lease refuses *every* broker call, including
        # calls the catalogue labels as reads (D3/D1 D09).
        if (
            session.task_lease_fence is None
            or task.lease_session_id != session.session_id
            or task.lease_fence != session.task_lease_fence
            or task.lease_expires_at is None
            or task.lease_expires_at <= now
            or task.status not in (TaskStatus.RUNNING, TaskStatus.AWAITING_INPUT)
        ):
            raise AgenticLeaseRefusedError("the task lease is expired or superseded")
        return task

    # ----------------------------------------------------------- registry path

    async def _registry_tool(
        self,
        *,
        session: VerifiedAgentSession,
        task: InvestigationTaskV1,
        operator_id: str,
        tool_name: str,
        schema_version: str,
        arguments: Mapping[str, Any],
        idempotency_key: str,
    ) -> AgenticToolResult:
        if self._invocations is None:
            raise AgenticBrokerUnavailableError("the operator invocation service is not wired")
        state = await self._uow.investigation_states.get(self._investigation_id)
        if state is None:
            raise AgenticInvestigationNotStartedError("the investigation has no working state")
        result: OperatorToolInvocationResult = await self._invocations.invoke(
            tool_name=tool_name,
            schema_version=schema_version,
            arguments=arguments,
            purpose=f"agentic tool call {tool_name} for task {task.id}",
            # The bound D7 key, not the mutable working-state version, is the
            # replay identity for an agentic call: an identical replay must
            # return the same invocation while any changed body still conflicts.
            expected_version=state.version,
            expected_sha256=None,
            idempotency_key=idempotency_key,
            idempotency_scope="agent-session.v1",
            actor_id=operator_id,
            requester_actor_kind=ToolInvocationActorKind.OPERATOR,
            task_id=session.task_id,
            agent_session_id=session.session_id,
        )
        return AgenticToolResult(
            investigation_id=self._investigation_id,
            session_id=session.session_id,
            task_id=task.id,
            tool_name=tool_name,
            status=result.invocation.status.value,
            idempotent_replay=result.idempotent_replay,
            invocation_id=result.invocation.id,
            output=dict(result.output),
            error_code=result.error_code,
        )

    # ------------------------------------------------------------- task path

    async def _task_command(
        self,
        *,
        session: VerifiedAgentSession,
        task: InvestigationTaskV1,
        operator_id: str,
        tool_name: str,
        arguments: Mapping[str, Any],
        idempotency_key: str,
        now: datetime,
    ) -> AgenticToolResult:
        self._require_bound_arguments(arguments, task=task)
        expected = _required_int(arguments, "expected_status_version", minimum=1)
        engine = InvestigationTaskService(
            tasks=self._uow.investigation_tasks,
            decisions=self._uow.analyst_decisions,
            sessions=_BoundSessionPort(session, self._uow.agent_sessions),
            limits=self._task_limits,
        )
        actor = TaskActor(kind=TaskActorKind.OPERATOR, actor_id=operator_id)
        if tool_name == "task.heartbeat":
            fence = _required_int(arguments, "lease_fence", minimum=0)
            command = await engine.heartbeat(
                task_id=task.id,
                actor=actor,
                expected_status_version=expected,
                lease_fence=fence,
                idempotency_key=idempotency_key,
                now=now,
            )
        else:
            fence_value = arguments.get("lease_fence")
            submit_fence = (
                _required_int(arguments, "lease_fence", minimum=0)
                if fence_value is not None
                else None
            )
            result_refs = arguments.get("result_refs", ())
            if not isinstance(result_refs, Sequence) or isinstance(result_refs, (str, bytes)):
                raise AgenticArgumentsInvalidError("result_refs must be an array")
            command = await engine.submit(
                task_id=task.id,
                actor=actor,
                expected_status_version=expected,
                idempotency_key=idempotency_key,
                result_refs=tuple(result_refs),
                lease_fence=submit_fence,
                now=now,
            )
        if self._commit is not None:
            await self._commit()
        return AgenticToolResult(
            investigation_id=self._investigation_id,
            session_id=session.session_id,
            task_id=task.id,
            tool_name=tool_name,
            status=command.task.status.value,
            idempotent_replay=command.replayed,
            output={
                "task": {
                    "id": str(command.task.id),
                    "status": command.task.status.value,
                    "status_version": command.task.status_version,
                    "result_verification_status": (command.task.result_verification_status.value),
                    "event_sequence": command.task.event_sequence,
                },
                "event_id": str(command.event.id) if command.event is not None else None,
            },
        )

    # ----------------------------------------------------------- handoff path

    async def _handoff(
        self,
        *,
        session: VerifiedAgentSession,
        task: InvestigationTaskV1,
        operator_id: str,
        arguments: Mapping[str, Any],
        idempotency_key: str,
    ) -> AgenticToolResult:
        payload = {
            name: value for name, value in arguments.items() if name not in {"investigation_id"}
        }
        payload["task_id"] = payload.get("task_id", task.id)
        self._require_bound_arguments(payload, task=task)
        try:
            request = HandoffCreateRequest.model_validate(payload)
        except ValueError as exc:
            raise AgenticArgumentsInvalidError(f"invalid handoff payload: {exc}") from None
        if self._handoffs is None:
            raise AgenticBrokerUnavailableError("the handoff service is not wired")
        record = await self._handoffs.create(
            self._investigation_id,
            actor=HandoffActor(kind=HandoffActorKind.OPERATOR, actor_id=operator_id),
            idempotency_key=idempotency_key,
            request=request,
        )
        return AgenticToolResult(
            investigation_id=self._investigation_id,
            session_id=session.session_id,
            task_id=task.id,
            tool_name=AGENTIC_HANDOFF_TOOL,
            status="handoff_recorded",
            idempotent_replay=False,
            output=record.handoff.to_payload(),
        )

    # ------------------------------------------------------ contribution path

    async def _contribution(
        self,
        *,
        session: VerifiedAgentSession,
        task: InvestigationTaskV1,
        operator_id: str,
        arguments: Mapping[str, Any],
        idempotency_key: str,
        now: datetime,
    ) -> AgenticToolResult:
        """Import one revision through the M24-07 writer under this task's lease.

        ``origin``, the actor, ``task_id`` and ``lease_fence`` are derived from
        the verified session and the locked task row; the agent payload cannot
        claim them.  Every terminal import of this agent's row appends the
        ``contribution_imported`` task event (D3), including a rejected one and
        its typed ``validation_error_code``, so the append-only journal records
        the writing task and the fence the row carried.
        """
        if self._contributions is None:
            raise AgenticBrokerUnavailableError("the contribution import service is not wired")
        self._require_bound_arguments(arguments, task=task)
        payload = {
            name: value
            for name, value in arguments.items()
            if name not in {"investigation_id", "task_id"}
        }
        try:
            request = AgenticContributionImportArguments.model_validate(payload)
            declaration = request.declaration()
        except ValueError as exc:
            raise AgenticArgumentsInvalidError(f"invalid contribution payload: {exc}") from None
        authority = ContributionAuthority(
            origin=WorkContributionOrigin.AGENT,
            actor_id=operator_id,
            task_id=task.id,
            lease_fence=task.lease_fence,
        )
        try:
            contribution = await self._contributions.import_revision(
                self._investigation_id,
                workspace_id=request.workspace_id,
                workspace_revision_id=request.workspace_revision_id,
                declaration=declaration,
                authority=authority,
                idempotency_key=_bound_effect_key("contribution", idempotency_key),
            )
        except WorkContributionError as exc:
            raise AgenticContributionRefusedError(
                str(exc),
                code=exc.code,
                status_code=exc.status_code,
                details={str(name): str(value) for name, value in exc.details.items()},
            ) from exc
        event, created = await self._append_contribution_event(
            contribution, task=task, operator_id=operator_id, now=now
        )
        if self._commit is not None:
            await self._commit()
        output = dict(contribution.to_payload())
        output["event_id"] = str(event.id) if event is not None else None
        output["event_created"] = created
        return AgenticToolResult(
            investigation_id=self._investigation_id,
            session_id=session.session_id,
            task_id=task.id,
            tool_name=AGENTIC_CONTRIBUTION_TOOL,
            status=contribution.import_status.value,
            idempotent_replay=event is None or not created,
            output=output,
        )

    async def _append_contribution_event(
        self,
        contribution: WorkContributionV1,
        *,
        task: InvestigationTaskV1,
        operator_id: str,
        now: datetime,
    ) -> tuple[TaskEvent | None, bool]:
        """Append ``contribution_imported`` once for this agent's own row.

        A row already imported by another actor (host watcher or another task)
        is returned unchanged but must not be journaled as this task's import.
        """
        if (
            contribution.origin is not WorkContributionOrigin.AGENT
            or contribution.task_id != task.id
        ):
            return None, False
        return await append_task_event(
            self._uow.investigation_tasks,
            TaskEventDraft(
                investigation_id=self._investigation_id,
                task_id=task.id,
                event_type=TaskEventType.CONTRIBUTION_IMPORTED,
                actor=TaskActor(kind=TaskActorKind.OPERATOR, actor_id=operator_id),
                idempotency_key=f"contribution:{contribution.id}",
                payload={
                    "contribution_id": str(contribution.id),
                    "category": contribution.category.value,
                    "import_status": contribution.import_status.value,
                    "origin": contribution.origin.value,
                    "workspace_id": contribution.workspace_id,
                    "workspace_revision_id": contribution.workspace_revision_id,
                    "file_relpath": contribution.file_relpath,
                    "file_sha256": contribution.file_sha256,
                    "task_id": str(contribution.task_id),
                    "lease_fence": contribution.lease_fence,
                    "observation_id": (
                        str(contribution.observation_id)
                        if contribution.observation_id is not None
                        else None
                    ),
                    "finding_id": (
                        str(contribution.finding_id)
                        if contribution.finding_id is not None
                        else None
                    ),
                    "sample_association_id": (
                        str(contribution.sample_association_id)
                        if contribution.sample_association_id is not None
                        else None
                    ),
                    "validation_error_code": (
                        contribution.validation_error_code.value
                        if contribution.validation_error_code is not None
                        else None
                    ),
                },
            ),
            occurred_at=now,
        )

    # ------------------------------------------------------- note capture path

    async def _note_capture(
        self,
        *,
        session: VerifiedAgentSession,
        task: InvestigationTaskV1,
        arguments: Mapping[str, Any],
        idempotency_key: str,
    ) -> AgenticToolResult:
        """Publish one agent note/result through the host workspace layer.

        The write is origin ``agent`` with this task's header (or
        ``generated_by=session:<id>`` for ``results/``); it is a workspace
        revision, never evidence.
        """
        if self._notes is None:
            raise AgenticBrokerUnavailableError("the note capture service is not wired")
        self._require_bound_arguments(arguments, task=task)
        payload = {
            name: value
            for name, value in arguments.items()
            if name not in {"investigation_id", "task_id"}
        }
        try:
            request = AgenticNoteCaptureArguments.model_validate(payload)
        except ValueError as exc:
            raise AgenticArgumentsInvalidError(f"invalid note capture payload: {exc}") from None
        try:
            result = await self._notes.capture(
                self._investigation_id,
                task_id=task.id,
                session_id=session.session_id,
                target=request.target,
                relpath=request.relpath,
                text=request.text,
                expected_sha256=request.expected_sha256,
                idempotency_key=_bound_effect_key("note", idempotency_key),
            )
        except NoteCaptureError as exc:
            raise AgenticNoteCaptureRefusedError(
                str(exc),
                code=exc.code,
                status_code=exc.status_code,
                details={str(name): str(value) for name, value in exc.details.items()},
            ) from exc
        except WorkspaceAgentError as exc:
            raise AgenticNoteCaptureRefusedError(
                str(exc), code=exc.code, status_code=exc.http_status
            ) from exc
        return AgenticToolResult(
            investigation_id=self._investigation_id,
            session_id=session.session_id,
            task_id=task.id,
            tool_name=NOTE_CAPTURE_TOOL,
            status="captured",
            idempotent_replay=False,
            output={
                "target": result.target,
                "path": result.path,
                "revision_id": result.revision_id,
                "sha256": result.sha256,
                "size": result.size,
                "created": result.created,
                "already_present": result.already_present,
                "evidence": False,
            },
        )

    # ---------------------------------------------------------------- helpers

    @staticmethod
    def _require_bound_arguments(
        arguments: Mapping[str, Any], *, task: InvestigationTaskV1
    ) -> None:
        for name in ("task_id",):
            value = arguments.get(name)
            if value is None:
                continue
            try:
                bound = UUID(str(value))
            except (TypeError, ValueError):
                raise AgenticArgumentsInvalidError(f"{name} is not a UUID") from None
            if bound != task.id:
                raise AgenticBindingRefusedError("the tool arguments target another task")
        investigation_id = arguments.get("investigation_id")
        if investigation_id is not None:
            try:
                bound_investigation = UUID(str(investigation_id))
            except (TypeError, ValueError):
                raise AgenticArgumentsInvalidError("investigation_id is not a UUID") from None
            if bound_investigation != task.investigation_id:
                raise AgenticBindingRefusedError("the tool arguments target another investigation")


def _bound_effect_key(prefix: str, bound_key: str) -> str:
    """A <=128-char effect idempotency key derived from the D7-bound broker key.

    The M24-07 import key and the workspace-agent key are bounded to 128
    characters while the broker's own ``agent:<session>:<task>:<fence>:<client>``
    identity is longer; hashing keeps the same session/task/fence binding.
    """
    digest = hashlib.sha256(bound_key.encode("ascii")).hexdigest()
    return f"{prefix}:{digest}"


def _required_int(arguments: Mapping[str, Any], name: str, *, minimum: int) -> int:
    value = arguments.get(name)
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise AgenticArgumentsInvalidError(f"{name} must be an integer >= {minimum}")
    return value


__all__ = [
    "AGENTIC_CONTRIBUTION_TOOL",
    "AGENTIC_HANDOFF_TOOL",
    "AGENTIC_IDEMPOTENCY_MAX_LENGTH",
    "AGENTIC_IDEMPOTENCY_PREFIX",
    "AGENTIC_REGISTRY_TOOLS",
    "AGENTIC_TASK_TOOLS",
    "AGENTIC_TOOL_NAMES",
    "BRIDGE_CONSULT_ALIAS",
    "OPINION_TOOL_NAMES",
    "AgenticArgumentsInvalidError",
    "AgenticBindingRefusedError",
    "AgenticBrokerError",
    "AgenticBrokerUnavailableError",
    "AgenticContributionImportArguments",
    "AgenticContributionRefusedError",
    "AgenticInvestigationNotStartedError",
    "AgenticLeaseRefusedError",
    "AgenticNoteCaptureArguments",
    "AgenticNoteCaptureRefusedError",
    "AgenticProseAuthorityRequiredError",
    "AgenticSessionInactiveError",
    "AgenticToolBroker",
    "AgenticToolNotAllowedError",
    "AgenticToolResult",
    "agentic_idempotency_key",
]
