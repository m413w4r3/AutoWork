"""Human-only task transfers and note-referenced instructions (M24-X1).

A transfer is ONE auditable flow (ADR-0011 D3/D5/D8):

1. **Sync** — scan the host workspace and ingest its event and snapshot pages
   through the existing client/ingest service, so the latest human note
   revisions are canonical before anything is handed over.
2. **Reassign** — a verified human moves the task through the task engine.
   A running/awaiting-input task is reconciled (typed host answer, never a
   blind relaunch), interrupted, then blocked: the engine releases the lease,
   increments ``lease_fence`` and closes the linked session.  An assigned task
   is released first; a blocked/ready task is assigned directly.
3. **Handoff** — a NEW immutable handoff version is appended, citing the
   latest note/results revisions, canonical observations, current finding
   versions, human decisions and Bridge consultations, plus a freshness diff
   against the predecessor.
4. **Open (optional)** — a new session for the destination adapter starts
   from that handoff through ``SessionService.open_session`` and its D5
   ``assert_startable`` gate.

Bridge is never a transfer destination: it is reached only through
``advisor.consult``.  The destination is a human or one of the two CLI
operators, and every refusal carries a typed code.

The same module owns the canonical note catalog behind ``note_refs`` on a
session instruction: it resolves the LATEST ``(revision_id, sha256)`` for each
``notes/humaines`` path at send time, records those exact refs in the durable
queue text, and reports a visible diff when a note changed after queueing.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from types import TracebackType
from typing import Any, Literal, Protocol, Self, cast
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from cti_app.application.agent_session_verification import SessionSecretDeriver
from cti_app.application.agentic_sessions import (
    AgentSessionSecretDeliveryPort,
    HostSessionReleaseService,
    NoteInstructionResolution,
    SessionError,
    SessionReleaseFlusher,
    SessionService,
    SessionUnitOfWork,
)
from cti_app.application.investigation_handoffs import (
    HandoffCreateRequest,
    HandoffError,
    HandoffFreshnessReport,
    InvestigationHandoffService,
)
from cti_app.application.investigation_persistence import (
    AgentSessionRepository,
    EvidenceObservationRepository,
    FindingRepository,
    HandoffRecord,
    InvestigationEnvelopeRepository,
    InvestigationHandoffRepository,
    InvestigationPlanRepository,
    InvestigationTaskRepository,
    ToolInvocationRepository,
    WorkspaceBindingRepository,
    WorkspaceFileEventRepository,
    WorkspaceFileRevisionRepository,
    WorkspaceSyncCursorRepository,
)
from cti_app.application.investigation_tasks import (
    InvestigationTaskService,
    TaskSessionOutcome,
    TaskSessionRelease,
)
from cti_app.application.persistence import (
    AnalystDecisionRepository,
    AnalystInvestigationRepository,
)
from cti_app.application.workspace_agent import (
    MAX_PAGE_SIZE,
    WorkspaceAgentError,
    WorkspaceAgentPort,
    WorkspaceFileRevisionV1,
    WorkspaceIngestService,
    WorkspaceUnitOfWork,
)
from cti_app.domain.agentic_operator import (
    INSTRUCTION_MAX_TEXT_BYTES,
    AgenticOperatorPort,
    AgentSessionState,
    AgentSessionV1,
    OperatorAdapterKind,
)
from cti_app.domain.editorial import AnalystDecision
from cti_app.domain.investigation import (
    EvidenceObservation,
    InvestigationFinding,
    ToolInvocation,
    is_advisory_observation,
    logical_finding_id_by_version,
)
from cti_app.domain.investigation_handoff import (
    HandoffActor,
    HandoffActorKind,
    HandoffWork,
)
from cti_app.domain.investigation_task import (
    TASK_LIMIT_DEFAULTS,
    BlockedReason,
    InvestigationTaskV1,
    TaskActor,
    TaskActorKind,
    TaskLimits,
    TaskStatus,
)

ADVISOR_TOOL_NAME = "advisor.consult"
ADVISOR_RESPONSE_SOURCE = "advisor.consult.response"
ADVISOR_RESPONSE_OBJECT_TYPE = "advisor_response_received"

NOTE_DIRECTORY = "notes/humaines/"
NOTE_BLOCK_BEGIN = "[autowork-workspace-notes v1]"
NOTE_BLOCK_END = "[/autowork-workspace-notes]"
MAX_NOTE_REFS = 32
MAX_SYNC_PAGES = 512
MAX_HANDOFF_FILES = 256
MAX_HANDOFF_OBSERVATIONS = 512
MAX_HANDOFF_FINDINGS = 256
MAX_HANDOFF_DECISIONS = 256
MAX_HANDOFF_CONSULTATIONS = 128
DEFAULT_NEXT_ACTION = "Continue the task from this handoff."
_HANDOFF_FILE_PREFIXES = ("notes/humaines/", "notes/agents/", "results/")
_RELATIVE_PATH = re.compile(r"^[^\x00\\]+$")
_TRANSFER_ACTIVE_STATUSES = frozenset(
    {TaskStatus.READY, TaskStatus.ASSIGNED, TaskStatus.RUNNING, TaskStatus.AWAITING_INPUT}
)


class TransferError(ValueError):
    """Base typed refusal for a transfer or a note-referenced instruction."""

    code = "transfer_invalid"
    status_code = 422

    def __init__(self, message: str, *, details: Mapping[str, Any] | None = None) -> None:
        self.details: dict[str, Any] = dict(details or {})
        super().__init__(message)


class TransferNotFoundError(TransferError):
    code = "transfer_not_found"
    status_code = 404


class TransferForbiddenError(TransferError):
    code = "transfer_forbidden"
    status_code = 403


class TransferStatusNotAllowedError(TransferError):
    code = "transfer_status_not_allowed"
    status_code = 409


class TransferStaleVersionError(TransferError):
    code = "stale_task_version"
    status_code = 409


class TransferConflictError(TransferError):
    code = "transfer_conflict"
    status_code = 409


class TransferIdempotencyConflictError(TransferError):
    code = "idempotency_conflict"
    status_code = 409


class TransferWorkspaceUnavailableError(TransferError):
    code = "workspace_local_unavailable"
    status_code = 503


class TransferSyncIncompleteError(TransferError):
    code = "workspace_sync_incomplete"
    status_code = 503


class TransferAgentSessionsUnavailableError(TransferError):
    code = "agent_sessions_unavailable"
    status_code = 503


class TransferHandoffTooLargeError(TransferError):
    code = "transfer_handoff_too_large"
    status_code = 409


class TransferUpstreamError(TransferError):
    """A canonical sub-service refused; its own typed code is preserved."""

    def __init__(
        self,
        message: str,
        *,
        code: str,
        status_code: int,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        self.code = code
        self.status_code = status_code
        super().__init__(message, details=details)


class NoteReferenceError(SessionError):
    """A note reference cannot be resolved to a canonical revision."""

    code = "note_ref_unresolved"
    status_code = 422


class NoteReferenceInvalidError(NoteReferenceError):
    code = "note_ref_invalid"


@dataclass(frozen=True, slots=True)
class NoteRevisionRefV1:
    """The latest canonical revision of one referenced human note."""

    path: str
    revision_id: str
    sha256: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", _note_path(self.path))
        if re.fullmatch(r"rev-[0-9a-f]{32}", self.revision_id) is None:
            raise ValueError("revision_id must match the per-file workspace identity")
        if re.fullmatch(r"[0-9a-f]{64}", self.sha256) is None:
            raise ValueError("sha256 must be a lowercase SHA-256")

    def to_payload(self) -> dict[str, str]:
        return {
            "path": self.path,
            "revision_id": self.revision_id,
            "sha256": self.sha256,
        }


@dataclass(frozen=True, slots=True)
class TransferSyncResult:
    """What the pre-transfer workspace synchronisation observed and ingested."""

    generation: int
    cursor: int
    inserted_events: int
    existing_events: int
    inserted_revisions: int
    resync_required: bool
    pages: int

    def to_payload(self) -> dict[str, Any]:
        return {
            "generation": self.generation,
            "cursor": self.cursor,
            "inserted_events": self.inserted_events,
            "existing_events": self.existing_events,
            "inserted_revisions": self.inserted_revisions,
            "resync_required": self.resync_required,
            "pages": self.pages,
        }


@dataclass(frozen=True, slots=True)
class TransferResult:
    """The committed transfer, safe to return to the human cockpit."""

    task: InvestigationTaskV1
    destination: HandoffActor
    sync: TransferSyncResult | None
    handoff: HandoffRecord
    previous_handoff_id: str | None
    previous_handoff_version: int | None
    previous_freshness: HandoffFreshnessReport | None
    session: AgentSessionV1 | None
    replayed: bool

    def to_payload(self) -> dict[str, Any]:
        return {
            "task": _task_state(self.task),
            "destination": {
                "actor_kind": self.destination.kind.value,
                "actor_id": self.destination.actor_id,
            },
            "sync": self.sync.to_payload() if self.sync is not None else None,
            "handoff": self.handoff.handoff.to_payload(),
            "previous_handoff_id": self.previous_handoff_id,
            "previous_handoff_version": self.previous_handoff_version,
            "previous_freshness": (
                self.previous_freshness.model_dump(mode="json")
                if self.previous_freshness is not None
                else None
            ),
            "session": self.session.to_payload() if self.session is not None else None,
            "replayed": self.replayed,
        }


class TransferTaskRequest(BaseModel):
    """The human's transfer intent; identity, versions and refs are derived."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    to_actor_kind: Literal["human", "operator"]
    to_adapter_kind: OperatorAdapterKind | None = None
    instruction: str | None = Field(default=None, max_length=16_384)
    expected_status_version: int = Field(ge=1)
    open_session: bool = False
    executable: str | None = Field(default=None, min_length=1, max_length=4096)
    model: str | None = Field(default=None, max_length=128)

    @model_validator(mode="after")
    def _validate_destination(self) -> TransferTaskRequest:
        if self.to_actor_kind == "operator":
            if self.to_adapter_kind is None:
                raise ValueError("an operator transfer names the destination adapter")
        elif self.to_adapter_kind is not None:
            raise ValueError("a human transfer names no adapter")
        if self.open_session:
            if self.to_actor_kind != "operator" or self.to_adapter_kind is None:
                raise ValueError("only an operator destination opens a session")
            if self.executable is None or not self.executable.strip():
                raise ValueError("opening a session requires the probed executable")
        if self.instruction is not None and not self.instruction.strip():
            raise ValueError("the instruction must not be empty")
        if self.to_adapter_kind is not None and not isinstance(
            self.to_adapter_kind, OperatorAdapterKind
        ):
            raise ValueError("to_adapter_kind must name a registered operator adapter")
        return self


@dataclass(frozen=True, slots=True)
class _StepKeys:
    """Deterministic child keys; the same route key always replays the same steps."""

    block: str
    release: str
    assign: str
    handoff: str
    open: str
    scan: str


def _step_keys(idempotency_key: str) -> _StepKeys:
    digest = hashlib.sha256(f"m24-transfer\x00{idempotency_key}".encode()).hexdigest()[:32]
    return _StepKeys(
        block=f"m24-transfer-{digest}-block",
        release=f"m24-transfer-{digest}-release",
        assign=f"m24-transfer-{digest}-assign",
        handoff=f"m24-transfer-{digest}-handoff",
        open=f"m24-transfer-{digest}-open",
        scan=f"m24t-scan-{digest}",
    )


class TransferUnitOfWork(Protocol):
    """The canonical stores one transfer reads or writes."""

    analyst_investigations: AnalystInvestigationRepository
    analyst_decisions: AnalystDecisionRepository
    investigation_tasks: InvestigationTaskRepository
    investigation_handoffs: InvestigationHandoffRepository
    investigation_plans: InvestigationPlanRepository
    agent_sessions: AgentSessionRepository
    workspace_bindings: WorkspaceBindingRepository
    workspace_file_revisions: WorkspaceFileRevisionRepository
    workspace_file_events: WorkspaceFileEventRepository
    workspace_sync_cursors: WorkspaceSyncCursorRepository
    evidence_observations: EvidenceObservationRepository
    investigation_findings: FindingRepository
    investigation_envelopes: InvestigationEnvelopeRepository
    tool_invocations: ToolInvocationRepository

    async def __aenter__(self) -> Self: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...

    async def commit(self) -> None: ...


class _EngineSessionPort:
    """Adapts the session repository to the task engine's narrow lease seam.

    A lease release closes the session row inside the transfer transaction and
    buffers the host teardown for a post-commit flush (B2).
    """

    def __init__(
        self,
        repository: AgentSessionRepository,
        releases: list[TaskSessionRelease] | None = None,
    ) -> None:
        self._repository = repository
        self._releases = releases

    async def is_active_session(
        self, *, session_id: UUID, investigation_id: UUID, task_id: UUID
    ) -> bool:
        return await self._repository.is_active_session(
            session_id=session_id, investigation_id=investigation_id, task_id=task_id
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
        await self._repository.close_session(
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


@dataclass(frozen=True, slots=True)
class _Destination:
    handoff: HandoffActor
    task: TaskActor
    adapter_kind: OperatorAdapterKind | None = None


def _operator_actor_id(adapter_kind: OperatorAdapterKind) -> str:
    return f"operator:{adapter_kind.value}"


def _destination_for(request: TransferTaskRequest, human: TaskActor) -> _Destination:
    if request.to_actor_kind == "human":
        return _Destination(
            handoff=HandoffActor(kind=HandoffActorKind.HUMAN, actor_id=human.actor_id),
            task=TaskActor(kind=TaskActorKind.HUMAN, actor_id=human.actor_id),
        )
    adapter_kind = request.to_adapter_kind
    if adapter_kind is None:  # pragma: no cover - the request model validates it
        raise TransferError("an operator transfer names the destination adapter")
    actor_id = _operator_actor_id(adapter_kind)
    return _Destination(
        handoff=HandoffActor(kind=HandoffActorKind.OPERATOR, actor_id=actor_id),
        task=TaskActor(kind=TaskActorKind.OPERATOR, actor_id=actor_id),
        adapter_kind=adapter_kind,
    )


class InvestigationTransferService:
    """Execute one human transfer across sync, task engine, handoff and session."""

    def __init__(
        self,
        *,
        uow_factory: Callable[[], TransferUnitOfWork],
        workspace_agent: WorkspaceAgentPort | None,
        operator: AgenticOperatorPort | None = None,
        secret_delivery: AgentSessionSecretDeliveryPort | None = None,
        deriver: SessionSecretDeriver | None = None,
        disclosure: Callable[[UUID], Awaitable[None]] | None = None,
        release_host: SessionReleaseFlusher | None = None,
        cli_network_mode: str = "isolated_egress",
        task_limits: TaskLimits = TASK_LIMIT_DEFAULTS,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._workspace_agent = workspace_agent
        self._operator = operator
        self._secret_delivery = secret_delivery
        self._deriver = deriver
        self._disclosure = disclosure
        self._release_host = release_host
        self._cli_network_mode = cli_network_mode
        self._task_limits = task_limits
        self._clock = clock or (lambda: datetime.now(UTC))
        self._ingest = WorkspaceIngestService(uow_factory=self._workspace_uow)

    def _workspace_uow(self) -> WorkspaceUnitOfWork:
        # The full canonical UoW structurally satisfies the narrow ingest slice.
        return self._uow_factory()

    async def transfer(
        self,
        investigation_id: UUID,
        *,
        task_id: UUID,
        actor: TaskActor,
        idempotency_key: str,
        request: TransferTaskRequest,
        now: datetime | None = None,
    ) -> TransferResult:
        if not isinstance(investigation_id, UUID):
            raise TypeError("investigation_id must be a UUID")
        if not isinstance(task_id, UUID):
            raise TypeError("task_id must be a UUID")
        if not isinstance(actor, TaskActor):
            raise TypeError("actor must use TaskActor")
        if actor.kind is not TaskActorKind.HUMAN:
            raise TransferForbiddenError("only a verified human transfers a task")
        if not isinstance(request, TransferTaskRequest):
            raise TypeError("request must use TransferTaskRequest")
        if not isinstance(idempotency_key, str) or not idempotency_key.strip():
            raise TransferError("Idempotency-Key must be a non-empty string")
        if len(idempotency_key) > 255:
            raise TransferError("Idempotency-Key is bounded to 255 characters")
        key = idempotency_key.strip()
        timestamp = _now(self._clock, now)
        destination = _destination_for(request, actor)
        keys = _step_keys(key)
        instruction = request.instruction.strip() if request.instruction is not None else None

        replay = await self._replay(
            investigation_id,
            task_id=task_id,
            actor=actor,
            request=request,
            keys=keys,
            destination=destination,
            now=timestamp,
        )
        if replay is not None:
            return replay

        task = await self._load_task(investigation_id, task_id)
        if task.status not in _TRANSFER_ACTIVE_STATUSES:
            raise TransferStatusNotAllowedError(
                f"a {task.status.value} task cannot be transferred",
                details={"task_id": str(task.id), "status": task.status.value},
            )
        started = await self._transfer_started(task, keys)
        if not started and task.status_version != request.expected_status_version:
            raise TransferStaleVersionError(
                f"expected status_version {request.expected_status_version}, "
                f"current is {task.status_version}",
                details={
                    "expected_status_version": str(request.expected_status_version),
                    "current_status_version": str(task.status_version),
                },
            )

        sync = await self._sync_workspace(investigation_id, keys)
        await self._stop_sessions(investigation_id, task, actor, now=timestamp)
        assigned = await self._apply_assignment(task, actor, destination, keys, now=timestamp)
        handoff, previous, freshness = await self._create_handoff(
            investigation_id,
            assigned,
            actor,
            destination,
            instruction,
            keys,
            now=timestamp,
        )
        session = await self._open_destination(
            investigation_id,
            assigned,
            actor,
            destination,
            handoff,
            request,
            keys,
            now=timestamp,
        )
        current = await self._reload_task_state(investigation_id, assigned.id)
        return TransferResult(
            task=current,
            destination=destination.handoff,
            sync=sync,
            handoff=handoff,
            previous_handoff_id=previous.handoff.handoff_id if previous is not None else None,
            previous_handoff_version=previous.handoff.version if previous is not None else None,
            previous_freshness=freshness,
            session=session,
            replayed=False,
        )

    # ------------------------------------------------------------- replay

    async def _replay(
        self,
        investigation_id: UUID,
        *,
        task_id: UUID,
        actor: TaskActor,
        request: TransferTaskRequest,
        keys: _StepKeys,
        destination: _Destination,
        now: datetime,
    ) -> TransferResult | None:
        async with self._uow_factory() as uow:
            if await uow.analyst_investigations.get(investigation_id) is None:
                raise TransferNotFoundError(f"investigation {investigation_id} does not exist")
            task = await uow.investigation_tasks.get(task_id)
            if task is None or task.investigation_id != investigation_id:
                raise TransferNotFoundError(
                    f"task {task_id} is not part of investigation {investigation_id}"
                )
            handoff = await uow.investigation_handoffs.get_by_idempotency(task.id, keys.handoff)
            if handoff is None:
                return None
            recorded = handoff.handoff.to_actor
            if (recorded.kind, recorded.actor_id) != (
                destination.handoff.kind,
                destination.handoff.actor_id,
            ):
                raise TransferIdempotencyConflictError(
                    "Idempotency-Key already records another transfer destination"
                )
            expected_next_action = (
                request.instruction.strip()
                if request.instruction is not None
                else DEFAULT_NEXT_ACTION
            )
            if handoff.handoff.next_action != expected_next_action:
                raise TransferIdempotencyConflictError(
                    "Idempotency-Key already records another transfer instruction"
                )
            session = await uow.agent_sessions.get_by_idempotency(task.id, keys.open)
            if session is not None:
                if not request.open_session:
                    raise TransferIdempotencyConflictError(
                        "Idempotency-Key already opened the destination session"
                    )
                if session.adapter_kind is not destination.adapter_kind:
                    raise TransferIdempotencyConflictError(
                        "Idempotency-Key already records another destination adapter"
                    )
                if (
                    request.executable is not None
                    and session.capabilities().executable != request.executable.strip()
                ):
                    raise TransferIdempotencyConflictError(
                        "Idempotency-Key already records another executable"
                    )
            previous = (
                await uow.investigation_handoffs.get(UUID(handoff.predecessor_id))
                if handoff.predecessor_id is not None
                else None
            )
        if request.open_session and session is None:
            session = await self._open_destination(
                investigation_id,
                task,
                actor,
                destination,
                handoff,
                request,
                keys,
                now=now,
            )
        freshness = (
            await self._handoff_service().freshness(
                investigation_id, UUID(previous.handoff.handoff_id)
            )
            if previous is not None
            else None
        )
        current = await self._reload_task_state(investigation_id, task.id)
        return TransferResult(
            task=current,
            destination=destination.handoff,
            sync=None,
            handoff=handoff,
            previous_handoff_id=previous.handoff.handoff_id if previous is not None else None,
            previous_handoff_version=previous.handoff.version if previous is not None else None,
            previous_freshness=freshness,
            session=session,
            replayed=True,
        )

    # -------------------------------------------------------------- steps

    async def _load_task(self, investigation_id: UUID, task_id: UUID) -> InvestigationTaskV1:
        async with self._uow_factory() as uow:
            if await uow.analyst_investigations.get(investigation_id) is None:
                raise TransferNotFoundError(f"investigation {investigation_id} does not exist")
            task = await uow.investigation_tasks.get(task_id)
            if task is None or task.investigation_id != investigation_id:
                raise TransferNotFoundError(
                    f"task {task_id} is not part of investigation {investigation_id}"
                )
            return task

    async def _transfer_started(self, task: InvestigationTaskV1, keys: _StepKeys) -> bool:
        async with self._uow_factory() as uow:
            for key in (keys.block, keys.release, keys.assign):
                if await uow.investigation_tasks.get_event(task.id, key) is not None:
                    return True
        return False

    async def _sync_workspace(self, investigation_id: UUID, keys: _StepKeys) -> TransferSyncResult:
        agent = self._workspace_agent
        if agent is None:
            raise TransferWorkspaceUnavailableError(
                "the host workspace agent is not configured; the transfer did not start"
            )
        async with self._uow_factory() as uow:
            binding = await uow.workspace_bindings.get_for_investigation(investigation_id)
        if binding is None:
            raise TransferWorkspaceUnavailableError(
                "the investigation has no bound host workspace",
                details={"investigation_id": str(investigation_id)},
            )
        inserted_events = existing_events = inserted_revisions = 0
        pages = 0
        resync = False
        generation = 0
        cursor = 0
        try:
            scan = await agent.scan_workspace(
                binding.workspace_id, full=False, idempotency_key=keys.scan
            )
            generation = scan.generation
            after: int | None = None
            while True:
                page = await agent.events(binding.workspace_id, after=after, limit=MAX_PAGE_SIZE)
                outcome = await self._ingest.ingest_events_page(
                    workspace_id=binding.workspace_id, page=page
                )
                inserted_events += outcome.inserted_events
                existing_events += outcome.existing_events
                resync = resync or outcome.resync_required
                generation = page.generation
                cursor = page.next_cursor
                pages += 1
                _require_page_budget(pages)
                if page.resync_required:
                    after = None
                elif page.events and page.next_cursor > (after or 0):
                    after = page.next_cursor
                else:
                    break
            token: str | None = None
            while True:
                snapshot = await agent.snapshot(
                    binding.workspace_id, page_token=token, limit=MAX_PAGE_SIZE
                )
                outcome = await self._ingest.ingest_snapshot_page(
                    workspace_id=binding.workspace_id, page=snapshot
                )
                inserted_revisions += outcome.inserted_revisions
                generation = snapshot.generation
                pages += 1
                _require_page_budget(pages)
                token = snapshot.next_page_token
                if token is None:
                    break
        except TransferError:
            raise
        except WorkspaceAgentError as exc:
            raise TransferWorkspaceUnavailableError(
                "the host workspace could not be synchronised before the transfer",
                details={"workspace_id": binding.workspace_id, "code": exc.code},
            ) from exc
        return TransferSyncResult(
            generation=generation,
            cursor=cursor,
            inserted_events=inserted_events,
            existing_events=existing_events,
            inserted_revisions=inserted_revisions,
            resync_required=resync,
            pages=pages,
        )

    async def _stop_sessions(
        self,
        investigation_id: UUID,
        task: InvestigationTaskV1,
        actor: TaskActor,
        *,
        now: datetime,
    ) -> None:
        """Reconcile, interrupt and release every session this task still holds.

        The D3 rule is ``reconcile before relaunch``: an unknown host process
        is asked what happened, never assumed dead, and never silently
        relaunched.  Only then does the task transition release the lease.
        """
        async with self._uow_factory() as uow:
            sessions = await uow.agent_sessions.list_for_task(task.id)
        if not sessions:
            return
        service = self._session_service()
        for session in sessions:
            if session.is_terminal or session.state is AgentSessionState.LOST:
                continue
            if session.state in (
                AgentSessionState.READY,
                AgentSessionState.PROVISIONING,
                AgentSessionState.STOPPED,
            ):
                if task.lease_id is not None and session.task_lease_id == task.lease_id:
                    # The task transition below closes this leased session.
                    continue
                await service.close(
                    investigation_id,
                    session.id,
                    reason="transferred",
                    actor=actor,
                    now=now,
                )
                continue
            if self._operator is None:
                raise TransferAgentSessionsUnavailableError(
                    "an active agent session cannot be reconciled before the transfer",
                    details={"session_id": str(session.id)},
                )
            reconciled = await service.reconcile_for_transfer(
                investigation_id, session.id, actor=actor, now=now
            )
            if (
                reconciled.state
                in (AgentSessionState.RUNNING, AgentSessionState.AWAITING_INSTRUCTION)
                and session.state is not AgentSessionState.INTERRUPT_REQUESTED
            ):
                await service.interrupt(
                    investigation_id, session.id, reason="transferred", actor=actor, now=now
                )

    async def _apply_assignment(
        self,
        task: InvestigationTaskV1,
        actor: TaskActor,
        destination: _Destination,
        keys: _StepKeys,
        *,
        now: datetime,
    ) -> InvestigationTaskV1:
        """Move the task to ``assigned`` for the destination through the engine."""
        releases: list[TaskSessionRelease] = []
        async with self._uow_factory() as uow:
            engine = InvestigationTaskService(
                tasks=uow.investigation_tasks,
                decisions=uow.analyst_decisions,
                sessions=_EngineSessionPort(uow.agent_sessions, releases),
                limits=self._task_limits,
            )
            current = await uow.investigation_tasks.get(task.id)
            if current is None:  # pragma: no cover - it was just loaded
                raise TransferNotFoundError(f"task {task.id} disappeared")
            block_event = await uow.investigation_tasks.get_event(current.id, keys.block)
            if current.status in (TaskStatus.RUNNING, TaskStatus.AWAITING_INPUT):
                if block_event is None:
                    current = (
                        await engine.block(
                            task_id=current.id,
                            actor=actor,
                            expected_status_version=current.status_version,
                            idempotency_key=keys.block,
                            blocked_reason=BlockedReason.EXTERNAL,
                            now=now,
                        )
                    ).task
                else:
                    current = await self._reload(uow, current.id)
            assign_event = await uow.investigation_tasks.get_event(current.id, keys.assign)
            if block_event is not None and current.status not in (
                TaskStatus.BLOCKED,
                TaskStatus.ASSIGNED,
            ):
                raise TransferConflictError(
                    "the task changed after this transfer blocked it",
                    details={"status": current.status.value},
                )
            if current.status is TaskStatus.ASSIGNED:
                if assign_event is None:
                    release_event = await uow.investigation_tasks.get_event(
                        current.id, keys.release
                    )
                    if release_event is not None:
                        raise TransferConflictError(
                            "the task was released and assigned by another actor"
                        )
                    current = (
                        await engine.release(
                            task_id=current.id,
                            actor=actor,
                            expected_status_version=current.status_version,
                            idempotency_key=keys.release,
                            now=now,
                        )
                    ).task
                else:
                    _require_assignment(current, destination.task)
            if current.status is TaskStatus.READY:
                if assign_event is not None:
                    raise TransferConflictError("the transfer assignment was superseded")
                current = (
                    await engine.assign(
                        task_id=current.id,
                        actor=actor,
                        target=destination.task,
                        expected_status_version=current.status_version,
                        idempotency_key=keys.assign,
                        now=now,
                    )
                ).task
            elif current.status is TaskStatus.BLOCKED:
                if assign_event is not None:
                    raise TransferConflictError("the transfer assignment was superseded")
                current = (
                    await engine.reassign(
                        task_id=current.id,
                        actor=actor,
                        target=destination.task,
                        expected_status_version=current.status_version,
                        idempotency_key=keys.assign,
                        now=now,
                    )
                ).task
            if current.status is not TaskStatus.ASSIGNED:
                raise TransferStatusNotAllowedError(
                    f"the task ended in {current.status.value}, not assigned",
                    details={"task_id": str(current.id)},
                )
            _require_assignment(current, destination.task)
            await uow.commit()
        # The blocking transition committed: only now may the old session's
        # host process be stopped (B2), outside every lock.
        await self._flush_host_releases(releases)
        return current

    async def _flush_host_releases(self, releases: Sequence[TaskSessionRelease]) -> None:
        """Post-commit host teardown; an unconfirmed effect is marked, never lost."""
        if not releases:
            return
        flusher = self._release_host
        if flusher is None:
            flusher = HostSessionReleaseService(
                uow_factory=cast(Callable[[], SessionUnitOfWork], self._uow_factory),
                host=None,
            ).flush
        await flusher(releases)

    async def _create_handoff(
        self,
        investigation_id: UUID,
        task: InvestigationTaskV1,
        actor: TaskActor,
        destination: _Destination,
        instruction: str | None,
        keys: _StepKeys,
        *,
        now: datetime,
    ) -> tuple[HandoffRecord, HandoffRecord | None, HandoffFreshnessReport | None]:
        _latest, request = await self._handoff_request(
            investigation_id, task, destination, instruction
        )
        try:
            handoff = await self._handoff_service().create(
                investigation_id,
                actor=HandoffActor(kind=HandoffActorKind.HUMAN, actor_id=actor.actor_id),
                idempotency_key=keys.handoff,
                request=request,
                now=now,
            )
        except HandoffError as exc:
            if exc.code == "idempotency_conflict":
                raise TransferIdempotencyConflictError(str(exc)) from exc
            raise TransferConflictError(str(exc), details=exc.details) from exc
        previous = (
            await self._handoff_service().get(investigation_id, UUID(handoff.predecessor_id))
            if handoff.predecessor_id is not None
            else None
        )
        freshness = (
            await self._handoff_service().freshness(
                investigation_id, UUID(previous.handoff.handoff_id)
            )
            if previous is not None
            else None
        )
        return handoff, previous, freshness

    async def _handoff_request(
        self,
        investigation_id: UUID,
        task: InvestigationTaskV1,
        destination: _Destination,
        instruction: str | None,
    ) -> tuple[HandoffRecord | None, HandoffCreateRequest]:
        async with self._uow_factory() as uow:
            binding = await uow.workspace_bindings.get_for_investigation(investigation_id)
            if binding is None:
                raise TransferWorkspaceUnavailableError(
                    "the investigation has no bound host workspace",
                    details={"investigation_id": str(investigation_id)},
                )
            previous = await uow.investigation_handoffs.latest_for_task(task.id)
            revisions = await uow.workspace_file_revisions.list_for_workspace(binding.workspace_id)
            file_revision_ids = _latest_handoff_files(revisions)
            observations = await uow.evidence_observations.list_for_investigation(investigation_id)
            observation_ids = _evidence_observation_ids(observations)
            findings = await uow.investigation_findings.list_for_investigation(investigation_id)
            finding_version_ids = _latest_finding_version_ids(findings)
            decisions = await uow.analyst_decisions.list_for_investigation(investigation_id)
            decision_ids = _decision_ids(decisions)
            invocations = await uow.tool_invocations.list_for_investigation(investigation_id)
            consultation_ids = await _consultation_invocation_ids(uow, invocations)
        work = previous.handoff.work if previous is not None else HandoffWork()
        next_action = instruction or DEFAULT_NEXT_ACTION
        return previous, HandoffCreateRequest(
            task_id=task.id,
            to_actor=destination.handoff,
            work=work,
            observation_ids=observation_ids,
            finding_version_ids=finding_version_ids,
            decision_ids=decision_ids,
            consultation_invocation_ids=consultation_ids,
            file_revision_ids=file_revision_ids,
            next_action=next_action,
        )

    async def _open_destination(
        self,
        investigation_id: UUID,
        task: InvestigationTaskV1,
        actor: TaskActor,
        destination: _Destination,
        handoff: HandoffRecord,
        request: TransferTaskRequest,
        keys: _StepKeys,
        *,
        now: datetime,
    ) -> AgentSessionV1 | None:
        if not request.open_session:
            return None
        adapter_kind = destination.adapter_kind
        if adapter_kind is None or request.executable is None:
            raise TransferError("the destination session has no adapter or executable")
        try:
            prepared = await self._session_service().open_session(
                investigation_id,
                task_id=task.id,
                adapter_kind=adapter_kind,
                handoff_id=UUID(handoff.handoff.handoff_id),
                executable=request.executable,
                actor=actor,
                idempotency_key=keys.open,
                model=request.model,
                now=now,
            )
        except SessionError as exc:
            raise TransferUpstreamError(
                str(exc),
                code=exc.code,
                status_code=exc.status_code,
                details=getattr(exc, "details", None),
            ) from exc
        return prepared.session

    # ------------------------------------------------------------ helpers

    def _handoff_service(self) -> InvestigationHandoffService:
        return InvestigationHandoffService(
            uow_factory=lambda: self._uow_factory(), clock=self._clock
        )

    def _session_service(self) -> SessionService:
        return SessionService(
            uow_factory=lambda: self._session_uow(),
            deriver=self._deriver,
            operator=self._operator,
            secret_delivery=self._secret_delivery,
            disclosure=self._disclosure,
            release_host=self._release_host,
            cli_network_mode=self._cli_network_mode,
            task_limits=self._task_limits,
            clock=self._clock,
        )

    def _session_uow(self) -> SessionUnitOfWork:
        return self._uow_factory()

    async def _reload_task_state(
        self, investigation_id: UUID, task_id: UUID
    ) -> InvestigationTaskV1:
        return await self._load_task(investigation_id, task_id)

    @staticmethod
    async def _reload(uow: TransferUnitOfWork, task_id: UUID) -> InvestigationTaskV1:
        current = await uow.investigation_tasks.get(task_id)
        if current is None:  # pragma: no cover - it was just written
            raise TransferNotFoundError(f"task {task_id} disappeared")
        return current


def _require_page_budget(pages: int) -> None:
    if pages > MAX_SYNC_PAGES:
        raise TransferSyncIncompleteError(
            "the workspace produced more pages than a bounded transfer may ingest",
            details={"limit": str(MAX_SYNC_PAGES)},
        )


def _task_state(task: InvestigationTaskV1) -> dict[str, Any]:
    """The task fields a transfer opens or closes; never the whole aggregate."""
    return {
        "id": str(task.id),
        "investigation_id": str(task.investigation_id),
        "task_key": task.task_key,
        "status": task.status.value,
        "status_version": task.status_version,
        "lease_fence": task.lease_fence,
        "assigned_actor_kind": (
            task.assigned_actor_kind.value if task.assigned_actor_kind is not None else None
        ),
        "assigned_actor_id": task.assigned_actor_id,
        "result_verification_status": task.result_verification_status.value,
        "updated_at": task.updated_at.isoformat(),
    }


def _require_assignment(task: InvestigationTaskV1, destination: TaskActor) -> None:
    if (task.assigned_actor_kind, task.assigned_actor_id) != (
        destination.kind,
        destination.actor_id,
    ):
        raise TransferConflictError(
            "the task is assigned to another actor",
            details={
                "assigned_actor_kind": (
                    task.assigned_actor_kind.value if task.assigned_actor_kind is not None else ""
                ),
                "assigned_actor_id": task.assigned_actor_id or "",
            },
        )


def _latest_handoff_files(
    revisions: Sequence[WorkspaceFileRevisionV1],
) -> tuple[str, ...]:
    latest: dict[str, WorkspaceFileRevisionV1] = {}
    for revision in revisions:
        if not revision.path.startswith(_HANDOFF_FILE_PREFIXES):
            continue
        current = latest.get(revision.path)
        if current is None or (revision.observed_at, revision.revision_id) > (
            current.observed_at,
            current.revision_id,
        ):
            latest[revision.path] = revision
    if len(latest) > MAX_HANDOFF_FILES:
        raise TransferHandoffTooLargeError(
            "the workspace holds more files than one handoff may reference",
            details={"count": str(len(latest)), "limit": str(MAX_HANDOFF_FILES)},
        )
    return tuple(latest[path].revision_id for path in sorted(latest))


def _evidence_observation_ids(
    observations: Sequence[EvidenceObservation],
) -> tuple[UUID, ...]:
    evidence = [item for item in observations if not is_advisory_observation(item)]
    return _bounded_ids(
        [item.id for item in evidence],
        limit=MAX_HANDOFF_OBSERVATIONS,
        label="observations",
    )


def _latest_finding_version_ids(
    findings: Sequence[InvestigationFinding],
) -> tuple[UUID, ...]:
    by_id = {finding.id: finding for finding in findings}
    mapping = logical_finding_id_by_version(findings)
    latest = {mapping.get(finding_id, finding_id) for finding_id in by_id}
    selected = [by_id[item].id for item in latest if item in by_id]
    return _bounded_ids(selected, limit=MAX_HANDOFF_FINDINGS, label="findings")


def _decision_ids(decisions: Sequence[AnalystDecision]) -> tuple[UUID, ...]:
    return _bounded_ids(
        [decision.id for decision in decisions],
        limit=MAX_HANDOFF_DECISIONS,
        label="decisions",
    )


async def _consultation_invocation_ids(
    uow: TransferUnitOfWork, invocations: Sequence[ToolInvocation]
) -> tuple[UUID, ...]:
    selected: list[UUID] = []
    for invocation in invocations:
        if invocation.tool_name != ADVISOR_TOOL_NAME:
            continue
        if await _consultation_output_sha256(uow, invocation) is None:
            continue
        selected.append(invocation.id)
    return _bounded_ids(selected, limit=MAX_HANDOFF_CONSULTATIONS, label="consultations")


async def _consultation_output_sha256(
    uow: TransferUnitOfWork, invocation: ToolInvocation
) -> str | None:
    output_shas: set[str] = set()
    for reference in invocation.response_refs:
        observation = await uow.evidence_observations.get(reference)
        if observation is None:
            continue
        if (
            observation.source == ADVISOR_RESPONSE_SOURCE
            or observation.object_type == ADVISOR_RESPONSE_OBJECT_TYPE
        ):
            output_shas.add(observation.sha256)
    return output_shas.pop() if len(output_shas) == 1 else None


def _bounded_ids(ids: Sequence[UUID], *, limit: int, label: str) -> tuple[UUID, ...]:
    unique = tuple(dict.fromkeys(ids))
    if len(unique) > limit:
        raise TransferHandoffTooLargeError(
            f"the investigation holds more {label} than one handoff may reference",
            details={"label": label, "count": str(len(unique)), "limit": str(limit)},
        )
    return unique


def _now(clock: Callable[[], datetime], value: datetime | None) -> datetime:
    timestamp = clock() if value is None else value
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    return timestamp


def _note_path(value: str) -> str:
    if not isinstance(value, str):
        raise NoteReferenceInvalidError("a note reference must be a string")
    normalized = value.strip()
    if not normalized or len(normalized) > 1024 or "\x00" in normalized:
        raise NoteReferenceInvalidError("a note reference must be a bounded path")
    if not normalized.startswith(NOTE_DIRECTORY):
        raise NoteReferenceInvalidError(
            "a note reference must be a workspace path under notes/humaines",
            details={"path": normalized},
        )
    segments = normalized.split("/")
    if any(segment in ("", ".", "..") for segment in segments):
        raise NoteReferenceInvalidError(
            "a note reference must not contain empty, '.' or '..' segments",
            details={"path": normalized},
        )
    if _RELATIVE_PATH.fullmatch(normalized) is None:
        raise NoteReferenceInvalidError("a note reference must be relative")
    return normalized


class WorkspaceNoteCatalog:
    """Resolve and refresh the canonical revisions behind ``note_refs``.

    The queue text carries a compact JSON block of exact ``(path, revision_id,
    sha256)`` refs.  Refresh re-reads the workspace revisions and appends a
    visible diff for any note that changed after the instruction was queued.
    """

    def __init__(self, *, uow_factory: Callable[[], TransferUnitOfWork]) -> None:
        self._uow_factory = uow_factory

    async def _latest_for_paths(
        self, investigation_id: UUID, paths: Sequence[str]
    ) -> dict[str, WorkspaceFileRevisionV1]:
        async with self._uow_factory() as uow:
            binding = await uow.workspace_bindings.get_for_investigation(investigation_id)
            if binding is None:
                raise NoteReferenceError(
                    "the investigation has no bound host workspace",
                    details={"investigation_id": str(investigation_id)},
                )
            revisions = await uow.workspace_file_revisions.list_for_workspace(binding.workspace_id)
        wanted = set(paths)
        latest: dict[str, WorkspaceFileRevisionV1] = {}
        for revision in revisions:
            if revision.path not in wanted:
                continue
            current = latest.get(revision.path)
            if current is None or (revision.observed_at, revision.revision_id) > (
                current.observed_at,
                current.revision_id,
            ):
                latest[revision.path] = revision
        return latest

    async def resolve(
        self, investigation_id: UUID, note_refs: Sequence[str]
    ) -> tuple[NoteRevisionRefV1, ...]:
        paths = _note_paths(note_refs)
        latest = await self._latest_for_paths(investigation_id, paths)
        resolved: list[NoteRevisionRefV1] = []
        for path in paths:
            revision = latest.get(path)
            if revision is None:
                raise NoteReferenceError(
                    "a referenced note has no canonical workspace revision",
                    details={"path": path},
                )
            resolved.append(_note_ref(path, revision))
        return tuple(resolved)

    async def prepare(
        self, *, investigation_id: UUID, text: str, note_refs: Sequence[str]
    ) -> NoteInstructionResolution:
        refs = await self.resolve(investigation_id, note_refs)
        queued_text = _with_note_block(text, refs)
        if len(queued_text.encode("utf-8")) > INSTRUCTION_MAX_TEXT_BYTES:
            raise NoteReferenceInvalidError(
                "the instruction plus its note references exceed the queue bound"
            )
        return NoteInstructionResolution(queued_text=queued_text, delivery_text=queued_text)

    async def refresh(self, *, investigation_id: UUID, text: str) -> NoteInstructionResolution:
        recorded = _recorded_note_refs(text)
        if not recorded:
            return NoteInstructionResolution(queued_text=text, delivery_text=text)
        latest = await self._latest_for_paths(investigation_id, [ref.path for ref in recorded])
        diffs: list[Mapping[str, str]] = []
        for ref in recorded:
            candidate = latest.get(ref.path)
            if (
                candidate is None
                or candidate.revision_id != ref.revision_id
                or candidate.sha256 != ref.sha256
            ):
                diffs.append(
                    {
                        "path": ref.path,
                        "queued_revision_id": ref.revision_id,
                        "queued_sha256": ref.sha256,
                        "current_revision_id": candidate.revision_id if candidate else "",
                        "current_sha256": candidate.sha256 if candidate else "",
                    }
                )
        if not diffs:
            return NoteInstructionResolution(queued_text=text, delivery_text=text)
        lines = [
            "",
            "[notes modifiées depuis la mise en file]",
            *(
                f"- {item['path']}: {item['queued_revision_id']} -> "
                f"{item['current_revision_id'] or 'révision absente'}"
                for item in diffs
            ),
        ]
        delivery_text = text + "\n".join(lines)
        return NoteInstructionResolution(
            queued_text=text,
            delivery_text=delivery_text,
            note_diffs=tuple(diffs),
        )


def _note_paths(note_refs: Sequence[str]) -> tuple[str, ...]:
    if not isinstance(note_refs, (tuple, list)):
        raise NoteReferenceInvalidError("note_refs must be a sequence of paths")
    if len(note_refs) > MAX_NOTE_REFS:
        raise NoteReferenceInvalidError(
            "too many note references on one instruction",
            details={"limit": str(MAX_NOTE_REFS)},
        )
    seen: set[str] = set()
    paths: list[str] = []
    for item in note_refs:
        path = _note_path(item)
        if path in seen:
            continue
        seen.add(path)
        paths.append(path)
    if not paths:
        raise NoteReferenceInvalidError("note_refs must not be empty")
    return tuple(paths)


def _note_ref(path: str, revision: WorkspaceFileRevisionV1) -> NoteRevisionRefV1:
    return NoteRevisionRefV1(path=path, revision_id=revision.revision_id, sha256=revision.sha256)


def _with_note_block(text: str, refs: Sequence[NoteRevisionRefV1]) -> str:
    payload = json.dumps(
        {"notes": [ref.to_payload() for ref in refs]},
        sort_keys=True,
        separators=(",", ":"),
    )
    normalized = text.strip()
    return f"{normalized}\n\n{NOTE_BLOCK_BEGIN}\n{payload}\n{NOTE_BLOCK_END}"


def _recorded_note_refs(text: str) -> tuple[NoteRevisionRefV1, ...]:
    start = text.find(NOTE_BLOCK_BEGIN)
    end = text.find(NOTE_BLOCK_END, start + len(NOTE_BLOCK_BEGIN))
    if start < 0 or end < 0:
        return ()
    raw = text[start + len(NOTE_BLOCK_BEGIN) : end].strip()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return ()
    if not isinstance(payload, dict):
        return ()
    notes = payload.get("notes")
    if not isinstance(notes, list):
        return ()
    refs: list[NoteRevisionRefV1] = []
    for item in notes:
        if not isinstance(item, dict):
            return ()
        path = item.get("path")
        revision_id = item.get("revision_id")
        sha256 = item.get("sha256")
        if (
            not isinstance(path, str)
            or not isinstance(revision_id, str)
            or not isinstance(sha256, str)
        ):
            return ()
        try:
            refs.append(NoteRevisionRefV1(path=path, revision_id=revision_id, sha256=sha256))
        except (TypeError, ValueError):
            return ()
    return tuple(refs)


__all__ = [
    "InvestigationTransferService",
    "NoteReferenceError",
    "NoteReferenceInvalidError",
    "NoteRevisionRefV1",
    "TransferAgentSessionsUnavailableError",
    "TransferConflictError",
    "TransferError",
    "TransferForbiddenError",
    "TransferHandoffTooLargeError",
    "TransferIdempotencyConflictError",
    "TransferNotFoundError",
    "TransferResult",
    "TransferStaleVersionError",
    "TransferStatusNotAllowedError",
    "TransferSyncIncompleteError",
    "TransferSyncResult",
    "TransferTaskRequest",
    "TransferUnitOfWork",
    "TransferWorkspaceUnavailableError",
    "WorkspaceNoteCatalog",
]
