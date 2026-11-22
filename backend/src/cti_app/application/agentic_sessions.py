"""Durable agent-session lifecycle, secret issuance and instruction queue (M24-05B1).

ADR-0011 D7 splits authority three ways and this module implements the middle
layer between them:

* ``investigation_tasks`` (lot 04B2) owns the business status and the lease.
  The service opens a session by calling the engine's ``start`` command, which
  acquires the fence through the narrow ``TaskSessionPort`` seam this module
  implements over the session repository.  It never writes task rows itself.
* ``agent_sessions`` owns the technical session state (``state`` +
  ``state_version`` CAS).  Every mutation here goes through
  :meth:`AgentSessionV1.with_state` and a compare-and-swap update.
* ``agent_session_instructions`` is the durable mid-session instruction queue:
  a busy agent keeps the instruction queued, and delivery never loses it.

The session secret is derived at prepare time from ``M24_AGENT_SESSION_KEY``
and a random nonce.  Only the nonce and ``SHA-256(secret)`` are stored; the
secret is returned exactly once in :class:`PreparedAgentSession`, whose
``repr``/``str`` and ``log_fields`` never expose it.  An idempotent replay
re-derives the same secret from the stored nonce.

Reconciliation never relaunches anything (D7/D07): an alive session returns to
its prior active state, a clean termination closes it, a lost session is
recorded ``lost`` with the adapter's typed outcome, and only a human ``resume``
decision may revive a session whose adapter proved ``resume_available``.  A
new session after an unresumable loss must present a new handoff.
"""

from __future__ import annotations

import secrets
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import TracebackType
from typing import Protocol, Self
from uuid import UUID, uuid4

from cti_app.application.agent_session_verification import (
    AgentSessionsUnavailableError,
    SessionSecretDeriver,
    session_secret_hash,
    session_secret_hex,
)
from cti_app.application.investigation_handoffs import (
    HandoffTaskStaleError,
    assert_startable,
)
from cti_app.application.investigation_persistence import (
    AgentSessionRepository,
    HandoffRecord,
    InvestigationHandoffRepository,
    InvestigationTaskRepository,
    VersionConflictError,
)
from cti_app.application.investigation_tasks import (
    InvestigationTaskService,
    TaskSessionOutcome,
)
from cti_app.application.persistence import (
    AnalystDecisionRepository,
    AnalystInvestigationRepository,
)
from cti_app.domain.agentic_operator import (
    AGENT_SESSION_SECRET_NONCE_BYTES,
    RECONCILE_OUTCOMES_RESUMABLE,
    RESUMABLE_AGENT_SESSION_STATES,
    AgenticOperatorPort,
    AgentSessionInstructionV1,
    AgentSessionState,
    AgentSessionV1,
    CollectResultsRequest,
    InstructionDeliveryOutcome,
    InstructionState,
    InterruptSessionRequest,
    OperatorAdapterKind,
    OperatorCapabilitiesV1,
    OperatorResultsV1,
    PrepareSessionRequest,
    ProbeCapabilitiesRequest,
    ReconcileSessionRequest,
    ResumeSessionRequest,
    SendInstructionRequest,
    SessionReconcileOutcome,
    SessionReconciliationV1,
    StartSessionRequest,
    instruction_text_sha256,
)
from cti_app.domain.investigation_task import (
    TASK_LIMIT_DEFAULTS,
    InvestigationTaskV1,
    TaskActor,
    TaskActorKind,
    TaskLimits,
    TaskStatus,
)


class SessionError(ValueError):
    """Base refusal for a session operation, carrying an HTTP-stable code."""

    code = "agent_session_invalid"
    status_code = 422

    def __init__(self, message: str, *, details: dict[str, str] | None = None) -> None:
        self.details = details or {}
        super().__init__(message)


class SessionNotFoundError(SessionError):
    code = "agent_session_not_found"
    status_code = 404


class SessionForbiddenError(SessionError):
    code = "agent_session_actor_not_allowed"
    status_code = 403


class SessionConflictError(SessionError):
    """Another active session already owns the task."""

    code = "active_session_exists"
    status_code = 409


class SessionCasConflictError(SessionError):
    """A competing ``state_version`` change won the compare-and-swap."""

    code = "agent_session_state_conflict"
    status_code = 409


class SessionStateConflictError(SessionError):
    code = "agent_session_state_conflict"
    status_code = 409


class SessionIdempotencyConflictError(SessionError):
    code = "idempotency_conflict"
    status_code = 409


class SessionInstructionConflictError(SessionError):
    code = "instruction_conflict"
    status_code = 409


class SessionResumeUnavailableError(SessionError):
    code = "resume_unavailable"
    status_code = 409


class SessionNewHandoffRequiredError(SessionError):
    code = "new_handoff_required"
    status_code = 409


class SessionOperatorUnavailableError(SessionError):
    code = "agentic_operator_unavailable"
    status_code = 503


class SessionOperatorContractError(SessionError):
    """An adapter returned a result that does not answer the request."""

    code = "agentic_operator_contract_invalid"
    status_code = 502


class SessionStartFailedError(SessionError):
    code = "agent_session_start_failed"
    status_code = 502


class SessionSecretDeliveryUnavailableError(SessionError):
    """No host workspace agent is wired to receive the one-time secret (D7)."""

    code = "agent_session_secret_delivery_unavailable"
    status_code = 503


class SessionSecretDeliveryError(SessionError):
    """The host agent did not accept the one-time session secret."""

    code = "agent_session_secret_delivery_failed"
    status_code = 502


class SessionUnitOfWork(Protocol):
    """The canonical stores one session operation reads or writes."""

    analyst_investigations: AnalystInvestigationRepository
    analyst_decisions: AnalystDecisionRepository
    investigation_tasks: InvestigationTaskRepository
    investigation_handoffs: InvestigationHandoffRepository
    agent_sessions: AgentSessionRepository

    async def __aenter__(self) -> Self: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...

    async def commit(self) -> None: ...


@dataclass(frozen=True, slots=True)
class SessionSecret:
    """The one-time session secret; every textual rendering is redacted."""

    _value: str = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self._value, str) or len(self._value) != 64:
            raise ValueError("a session secret is a 32-byte value encoded as 64 hex chars")
        try:
            int(self._value, 16)
        except ValueError as exc:
            raise ValueError("a session secret is lowercase hex") from exc
        if self._value.lower() != self._value:
            raise ValueError("a session secret is lowercase hex")

    def __repr__(self) -> str:
        return "SessionSecret(***)"

    __str__ = __repr__

    def reveal(self) -> str:
        """Return the secret for the single host delivery; never log the result."""
        return self._value

    @classmethod
    def from_bytes(cls, secret: bytes) -> SessionSecret:
        return cls(session_secret_hex(secret))


class AgentSessionSecretDeliveryPort(Protocol):
    """The host-side delivery of the one-time secret (D7: Unix socket only).

    The backend never returns the secret to the browser: it hands it to the
    host workspace agent, which launches the operator process with it.  An
    implementation must never log the secret and should deliver it once per
    session (an idempotent replay re-delivers the same secret).
    """

    async def deliver_session_secret(
        self,
        *,
        session_id: UUID,
        investigation_id: UUID,
        task_id: UUID,
        adapter_kind: OperatorAdapterKind,
        host_session_ref: str,
        secret: SessionSecret,
        idempotency_key: str,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class PreparedAgentSession:
    """A prepared (optionally started) session plus its one-time secret.

    ``repr``/``str`` are deliberately fixed: the nested :class:`SessionSecret`
    is redacted, and :meth:`log_fields` is the only structured form meant for
    logs or traces.
    """

    session: AgentSessionV1
    secret: SessionSecret

    def __post_init__(self) -> None:
        if not isinstance(self.session, AgentSessionV1):
            raise TypeError("session must use AgentSessionV1")
        if not isinstance(self.secret, SessionSecret):
            raise TypeError("secret must use SessionSecret")

    def __repr__(self) -> str:
        return (
            f"PreparedAgentSession(session_id={self.session.id}, "
            f"state={self.session.state.value}, secret=***)"
        )

    __str__ = __repr__

    def log_fields(self) -> dict[str, object]:
        return {
            "session_id": str(self.session.id),
            "investigation_id": str(self.session.investigation_id),
            "task_id": str(self.session.task_id),
            "adapter_kind": self.session.adapter_kind.value,
            "state": self.session.state.value,
            "secret": "***",
        }


class _EngineSessionPort:
    """Adapts the session repository to lot 04B2's ``TaskSessionPort`` seam."""

    def __init__(self, repository: AgentSessionRepository) -> None:
        self._repository = repository

    async def is_active_session(
        self, *, session_id: UUID, investigation_id: UUID, task_id: UUID
    ) -> bool:
        return await self._repository.is_active_session(
            session_id=session_id,
            investigation_id=investigation_id,
            task_id=task_id,
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


@dataclass(frozen=True, slots=True)
class _PrepareRequest:
    """The request identity an idempotent replay must match exactly."""

    task_id: UUID
    adapter_kind: OperatorAdapterKind
    handoff_id: UUID
    executable: str


class SessionService:
    """Prepare/open/start/drive/close one technical operator session."""

    def __init__(
        self,
        *,
        uow_factory: Callable[[], SessionUnitOfWork],
        deriver: SessionSecretDeriver | None,
        operator: AgenticOperatorPort | None = None,
        secret_delivery: AgentSessionSecretDeliveryPort | None = None,
        task_limits: TaskLimits = TASK_LIMIT_DEFAULTS,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._deriver = deriver
        self._operator = operator
        self._secret_delivery = secret_delivery
        self._task_limits = task_limits
        self._clock = clock or (lambda: datetime.now(UTC))

    # --------------------------------------------------------------- prepare

    async def prepare(
        self,
        investigation_id: UUID,
        *,
        task_id: UUID,
        adapter_kind: OperatorAdapterKind,
        handoff_id: UUID,
        executable: str,
        actor: TaskActor,
        idempotency_key: str,
        now: datetime | None = None,
    ) -> PreparedAgentSession:
        """Create the durable session in ``provisioning``/``ready`` and issue
        the secret.  The adapter is probed (real facts only) and asked to
        prepare its host session; a replay never probes a second host session.
        """
        request = _PrepareRequest(
            task_id=task_id,
            adapter_kind=adapter_kind,
            handoff_id=handoff_id,
            executable=executable,
        )
        key = _idempotency_key(idempotency_key)
        timestamp = _now(self._clock, now)
        self._require_human(actor)
        deriver = self._require_deriver()
        operator = self._require_operator()
        async with self._uow_factory() as uow:
            task = await self._locked_task(uow, investigation_id, task_id)
            existing = await uow.agent_sessions.get_by_idempotency(task.id, key)
            if existing is not None:
                prepared = self._replay_prepare(existing, request, deriver)
                await self._deliver_secret(prepared, key)
                await uow.commit()
                return prepared
            session, secret, _created = await self._prepare_locked(
                uow, investigation_id, task, request, operator, deriver, key, timestamp
            )
            prepared = PreparedAgentSession(session=session, secret=secret)
            await self._deliver_secret(prepared, key)
            await uow.commit()
            return prepared

    async def open_session(
        self,
        investigation_id: UUID,
        *,
        task_id: UUID,
        adapter_kind: OperatorAdapterKind,
        handoff_id: UUID,
        executable: str,
        actor: TaskActor,
        idempotency_key: str,
        now: datetime | None = None,
    ) -> PreparedAgentSession:
        """The human "open a session" action: prepare and start in one UoW.

        A stale handoff (D5) refuses before anything is persisted, so a failed
        open never leaves an active ``provisioning`` session blocking the task.
        """
        request = _PrepareRequest(
            task_id=task_id,
            adapter_kind=adapter_kind,
            handoff_id=handoff_id,
            executable=executable,
        )
        key = _idempotency_key(idempotency_key)
        timestamp = _now(self._clock, now)
        self._require_human(actor)
        deriver = self._require_deriver()
        operator = self._require_operator()
        async with self._uow_factory() as uow:
            task = await self._locked_task(uow, investigation_id, task_id)
            existing = await uow.agent_sessions.get_by_idempotency(task.id, key)
            if existing is not None:
                replayed = self._replay_prepare(existing, request, deriver)
                await self._deliver_secret(replayed, key)
                await uow.commit()
                return replayed
            session, secret, created = await self._prepare_locked(
                uow, investigation_id, task, request, operator, deriver, key, timestamp
            )
            # A concurrent identical open already started its own session; a
            # replay never relaunches anything.
            started = (
                await self._start_locked(uow, session, task, operator, timestamp)
                if created
                else session
            )
            prepared = PreparedAgentSession(session=started, secret=secret)
            await self._deliver_secret(prepared, key)
            await uow.commit()
            return prepared

    # ----------------------------------------------------------------- start

    async def start(
        self,
        investigation_id: UUID,
        session_id: UUID,
        *,
        actor: TaskActor,
        now: datetime | None = None,
    ) -> AgentSessionV1:
        """Start a prepared session: adapter start, then the D3 lease acquisition.

        The task engine is the only writer of the business status and lease;
        this method supplies the session id it requires and records the
        acquired lease pair on the session.
        """
        self._require_human(actor)
        operator = self._require_operator()
        timestamp = _now(self._clock, now)
        async with self._uow_factory() as uow:
            session = await self._session_for(uow, investigation_id, session_id)
            task = await self._locked_task(uow, investigation_id, session.task_id)
            started = await self._start_locked(uow, session, task, operator, timestamp)
            await uow.commit()
            return started

    # ------------------------------------------------------------------ read

    async def get(self, investigation_id: UUID, session_id: UUID) -> AgentSessionV1:
        async with self._uow_factory() as uow:
            return await self._session_for(uow, investigation_id, session_id)

    async def list(
        self, investigation_id: UUID, *, task_id: UUID | None = None
    ) -> tuple[AgentSessionV1, ...]:
        async with self._uow_factory() as uow:
            if await uow.analyst_investigations.get(investigation_id) is None:
                raise SessionNotFoundError(f"investigation {investigation_id} does not exist")
            return await uow.agent_sessions.list_for_investigation(
                investigation_id, task_id=task_id
            )

    async def instructions(
        self, investigation_id: UUID, session_id: UUID
    ) -> tuple[AgentSessionInstructionV1, ...]:
        async with self._uow_factory() as uow:
            await self._session_for(uow, investigation_id, session_id)
            return await uow.agent_sessions.list_instructions(session_id)

    # ----------------------------------------------------------- instructions

    async def enqueue_instruction(
        self,
        investigation_id: UUID,
        session_id: UUID,
        *,
        text: str,
        actor: TaskActor,
        idempotency_key: str,
        now: datetime | None = None,
    ) -> AgentSessionInstructionV1:
        """Persist one human instruction; deliver it only to an idle session.

        A busy (``running``) agent keeps the instruction queued; the operator
        acknowledges it through its signed route when it consumes the queue.
        """
        key = _idempotency_key(idempotency_key)
        timestamp = _now(self._clock, now)
        self._require_human(actor)
        async with self._uow_factory() as uow:
            session = await self._session_for(uow, investigation_id, session_id)
            if session.state not in (
                AgentSessionState.RUNNING,
                AgentSessionState.AWAITING_INSTRUCTION,
                AgentSessionState.INTERRUPT_REQUESTED,
            ):
                raise SessionStateConflictError(
                    f"a {session.state.value} session does not accept instructions"
                )
            instruction = AgentSessionInstructionV1(
                investigation_id=investigation_id,
                task_id=session.task_id,
                session_id=session.id,
                idempotency_key=key,
                text=text,
                text_sha256=instruction_text_sha256(text.strip()),
                state=InstructionState.QUEUED,
                created_by_kind=actor.kind.value,
                created_by_id=actor.actor_id,
                queued_at=timestamp,
                created_at=timestamp,
                updated_at=timestamp,
            )
            try:
                stored, created = await uow.agent_sessions.add_instruction(instruction)
            except VersionConflictError as exc:
                raise SessionIdempotencyConflictError(str(exc)) from exc
            if not created:
                return stored
            delivered = stored
            if (
                session.state is AgentSessionState.AWAITING_INSTRUCTION
                and self._operator is not None
                and session.host_session_ref is not None
            ):
                receipt = await self._operator.send_instruction(
                    SendInstructionRequest(
                        session_id=session.id,
                        host_session_ref=session.host_session_ref,
                        instruction_id=stored.id,
                        instruction_sha256=stored.text_sha256,
                        text=stored.text,
                        idempotency_key=stored.idempotency_key,
                    )
                )
                if receipt.session_id != session.id or receipt.instruction_id != stored.id:
                    raise SessionOperatorContractError(
                        "send_instruction answered a different instruction"
                    )
                if receipt.outcome is InstructionDeliveryOutcome.ACCEPTED:
                    delivered = await self._persist_instruction(
                        uow, stored.with_state(InstructionState.DELIVERED, now=timestamp)
                    )
                    await self._persist(
                        uow, session.with_state(AgentSessionState.RUNNING, now=timestamp)
                    )
            await uow.commit()
            return delivered

    async def acknowledge_instructions(
        self,
        investigation_id: UUID,
        session_id: UUID,
        *,
        instruction_ids: Sequence[UUID],
        actor: TaskActor,
        now: datetime | None = None,
    ) -> tuple[AgentSessionInstructionV1, ...]:
        """Operator-signed acknowledgement of consumed instructions."""
        timestamp = _now(self._clock, now)
        self._require_operator_actor(actor)
        if not instruction_ids:
            raise SessionInstructionConflictError("no instruction id was supplied")
        async with self._uow_factory() as uow:
            session = await self._session_for(uow, investigation_id, session_id)
            await self._require_current_task_lease(uow, session, timestamp)
            acknowledged: list[AgentSessionInstructionV1] = []
            for instruction_id in instruction_ids:
                instruction = await uow.agent_sessions.get_instruction(instruction_id)
                if instruction is None or instruction.session_id != session.id:
                    raise SessionInstructionConflictError(
                        "the instruction does not belong to this session"
                    )
                if instruction.state is InstructionState.ACKNOWLEDGED:
                    acknowledged.append(instruction)
                    continue
                if instruction.state is InstructionState.CANCELLED:
                    raise SessionInstructionConflictError(
                        "a cancelled instruction cannot be acknowledged"
                    )
                current = instruction
                if current.state is InstructionState.QUEUED:
                    current = await self._persist_instruction(
                        uow, current.with_state(InstructionState.DELIVERED, now=timestamp)
                    )
                acknowledged.append(
                    await self._persist_instruction(
                        uow, current.with_state(InstructionState.ACKNOWLEDGED, now=timestamp)
                    )
                )
            await uow.commit()
            return tuple(acknowledged)

    # ------------------------------------------------------------- lifecycle

    async def interrupt(
        self,
        investigation_id: UUID,
        session_id: UUID,
        *,
        reason: str,
        actor: TaskActor,
        now: datetime | None = None,
    ) -> AgentSessionV1:
        """Human interrupt: persist the intent, ask the adapter, record the stop."""
        timestamp = _now(self._clock, now)
        self._require_human(actor)
        operator = self._require_operator()
        if not isinstance(reason, str) or not reason.strip():
            raise SessionError("interrupt reason must not be empty")
        async with self._uow_factory() as uow:
            session = await self._session_for(uow, investigation_id, session_id)
            if session.state is AgentSessionState.INTERRUPT_REQUESTED:
                return session
            if session.state not in (
                AgentSessionState.RUNNING,
                AgentSessionState.AWAITING_INSTRUCTION,
            ):
                raise SessionStateConflictError(
                    f"a {session.state.value} session cannot be interrupted"
                )
            if session.host_session_ref is None:
                raise SessionOperatorContractError("the session has no host reference to stop")
            requested = await self._persist(
                uow,
                session.with_state(AgentSessionState.INTERRUPT_REQUESTED, now=timestamp),
            )
            result = await operator.interrupt_session(
                InterruptSessionRequest(
                    session_id=requested.id,
                    host_session_ref=requested.host_session_ref or "",
                    reason=reason.strip(),
                )
            )
            if result.session_id != requested.id:
                raise SessionOperatorContractError("interrupt_session answered another session")
            if result.stopped:
                requested = await self._persist(
                    uow,
                    requested.with_state(AgentSessionState.STOPPED, now=timestamp),
                )
            await uow.commit()
            return requested

    async def resume(
        self,
        investigation_id: UUID,
        session_id: UUID,
        *,
        actor: TaskActor,
        now: datetime | None = None,
    ) -> AgentSessionV1:
        """Human decision to resume; never automatic and never a relaunch.

        Only a stopped session whose probe supports resume, or a lost session
        whose reconciliation proved ``resume_available``, may resume.  A failed
        adapter resume leaves the old session ``lost``; the human must open a
        new session with a new handoff.
        """
        timestamp = _now(self._clock, now)
        self._require_human(actor)
        operator = self._require_operator()
        async with self._uow_factory() as uow:
            session = await self._session_for(uow, investigation_id, session_id)
            if session.state not in RESUMABLE_AGENT_SESSION_STATES:
                raise SessionStateConflictError(f"a {session.state.value} session is not resumable")
            outcome = session.reconciliation_outcome()
            if (
                session.state is AgentSessionState.LOST
                and outcome not in RECONCILE_OUTCOMES_RESUMABLE
            ):
                # Fail closed: an unreconciled or non-resumable loss is final.
                raise SessionResumeUnavailableError(
                    "the adapter did not prove the lost session can be resumed"
                )
            if (
                session.state is AgentSessionState.STOPPED
                and not session.capabilities().supports_resume
            ):
                raise SessionResumeUnavailableError(
                    "the probed adapter does not support resuming a stopped session"
                )
            if session.host_session_ref is None:
                raise SessionResumeUnavailableError("the session has no host reference to resume")
            result = await operator.resume_session(
                ResumeSessionRequest(
                    session_id=session.id,
                    adapter_kind=session.adapter_kind,
                    host_session_ref=session.host_session_ref,
                    capability_sha256=session.capability_sha256,
                )
            )
            if result.session_id != session.id:
                raise SessionOperatorContractError("resume_session answered another session")
            if not result.resumed:
                if session.state is AgentSessionState.STOPPED:
                    await self._persist(
                        uow,
                        session.with_state(
                            AgentSessionState.LOST,
                            now=timestamp,
                            close_reason="resume_unavailable",
                        ),
                    )
                    await uow.commit()
                raise SessionResumeUnavailableError(
                    "the adapter refused to resume; open a new session with a new handoff"
                )
            current = session
            if result.host_session_ref and result.host_session_ref != session.host_session_ref:
                current = await self._persist(
                    uow,
                    current.with_host_session_ref(result.host_session_ref, now=timestamp),
                )
            resumed = await self._persist(
                uow, current.with_state(AgentSessionState.READY, now=timestamp)
            )
            await uow.commit()
            return resumed

    async def reconcile(
        self,
        investigation_id: UUID,
        session_id: UUID,
        *,
        actor: TaskActor,
        now: datetime | None = None,
    ) -> AgentSessionV1:
        """Typed reconciliation; it records what happened and relaunches nothing."""
        timestamp = _now(self._clock, now)
        self._require_operator_actor(actor)
        operator = self._require_operator()
        async with self._uow_factory() as uow:
            session = await self._session_for(uow, investigation_id, session_id)
            if not session.is_active:
                raise SessionStateConflictError(
                    f"a {session.state.value} session is not reconcilable"
                )
            result = await operator.reconcile_session(
                ReconcileSessionRequest(
                    session_id=session.id,
                    host_session_ref=session.host_session_ref,
                )
            )
            self._require_reconciliation_answer(session, result)
            prior_state = session.state
            current = session
            if prior_state is not AgentSessionState.RECONCILING:
                current = await self._persist(
                    uow,
                    current.with_state(AgentSessionState.RECONCILING, now=timestamp),
                )
            current = await self._persist(
                uow,
                current.with_reconciliation(
                    result.payload(), result.reconciliation_sha256, now=timestamp
                ),
            )
            target = result.outcome
            if target is SessionReconcileOutcome.TERMINATED_CLEAN:
                current = await self._persist(
                    uow,
                    current.with_state(
                        AgentSessionState.CLOSED,
                        now=timestamp,
                        close_reason=target.value,
                    ),
                )
            elif target in (
                SessionReconcileOutcome.LOST_WITH_HANDOFF,
                SessionReconcileOutcome.LOST_UNRECONCILED,
                SessionReconcileOutcome.RESUME_AVAILABLE,
                SessionReconcileOutcome.RESUME_UNAVAILABLE,
            ):
                if target is SessionReconcileOutcome.LOST_WITH_HANDOFF:
                    if result.checkpoint_ref is None or result.checkpoint_sha256 is None:
                        raise SessionOperatorContractError(
                            "lost_with_handoff requires a checkpoint pair"
                        )
                    current = await self._persist(
                        uow,
                        current.with_checkpoint(
                            result.checkpoint_ref, result.checkpoint_sha256, now=timestamp
                        ),
                    )
                current = await self._persist(
                    uow,
                    current.with_state(
                        AgentSessionState.LOST,
                        now=timestamp,
                        close_reason=target.value,
                    ),
                )
            else:
                # ``session_alive``: return to the prior active state.  Any
                # state a live process may hold is admitted by the machine.
                restored = (
                    prior_state
                    if prior_state
                    in (
                        AgentSessionState.RUNNING,
                        AgentSessionState.AWAITING_INSTRUCTION,
                    )
                    else AgentSessionState.RUNNING
                )
                current = await self._persist(uow, current.with_state(restored, now=timestamp))
            await uow.commit()
            return current

    async def collect(
        self,
        investigation_id: UUID,
        session_id: UUID,
        *,
        actor: TaskActor,
        cursor: str | None = None,
        now: datetime | None = None,
    ) -> OperatorResultsV1:
        """Collect bounded adapter results and record the checkpoint pair."""
        timestamp = _now(self._clock, now)
        self._require_operator_actor(actor)
        operator = self._require_operator()
        async with self._uow_factory() as uow:
            session = await self._session_for(uow, investigation_id, session_id)
            if not session.is_active:
                raise SessionStateConflictError(
                    f"a {session.state.value} session cannot collect results"
                )
            if session.host_session_ref is None:
                raise SessionOperatorContractError("the session has no host reference")
            results = await operator.collect_results(
                CollectResultsRequest(
                    session_id=session.id,
                    host_session_ref=session.host_session_ref,
                    cursor=cursor,
                )
            )
            if results.session_id != session.id:
                raise SessionOperatorContractError("collect_results answered another session")
            current = session
            if results.checkpoint_ref is not None and results.checkpoint_sha256 is not None:
                current = await self._persist(
                    uow,
                    current.with_checkpoint(
                        results.checkpoint_ref, results.checkpoint_sha256, now=timestamp
                    ),
                )
            await self._persist(uow, current.with_last_activity(now=timestamp))
            await uow.commit()
            return results

    async def close(
        self,
        investigation_id: UUID,
        session_id: UUID,
        *,
        reason: str,
        actor: TaskActor,
        now: datetime | None = None,
    ) -> AgentSessionV1:
        """Human close of an unleased active session; a leased session is closed
        by the task transition that releases the lease (D3)."""
        timestamp = _now(self._clock, now)
        self._require_human(actor)
        if not isinstance(reason, str) or not reason.strip():
            raise SessionError("close reason must not be empty")
        async with self._uow_factory() as uow:
            session = await self._session_for(uow, investigation_id, session_id)
            if session.is_terminal:
                return session
            task = await uow.investigation_tasks.get(session.task_id)
            if (
                task is not None
                and session.task_lease_id is not None
                and task.lease_id == session.task_lease_id
            ):
                raise SessionStateConflictError(
                    "the task lease still owns this session; release it first"
                )
            target = (
                AgentSessionState.FAILED
                if session.state is AgentSessionState.PROVISIONING
                else AgentSessionState.CLOSED
            )
            closed = await self._persist(
                uow,
                session.with_state(target, now=timestamp, close_reason=reason.strip()),
            )
            await uow.commit()
            return closed

    # ---------------------------------------------------------------- helpers

    async def _prepare_locked(
        self,
        uow: SessionUnitOfWork,
        investigation_id: UUID,
        task: InvestigationTaskV1,
        request: _PrepareRequest,
        operator: AgenticOperatorPort,
        deriver: SessionSecretDeriver,
        key: str,
        timestamp: datetime,
    ) -> tuple[AgentSessionV1, SessionSecret, bool]:
        self._require_task_assignment(task)
        handoff = await self._handoff_for(uow, investigation_id, task, request.handoff_id)
        assert_startable(handoff.handoff, task)
        await self._require_unclaimed_handoff(uow, task, request.handoff_id)
        capabilities = await self._probe(operator, request.adapter_kind, request.executable)
        session_id = uuid4()
        prepared = await operator.prepare_session(
            PrepareSessionRequest(
                session_id=session_id,
                investigation_id=investigation_id,
                task_id=task.id,
                adapter_kind=request.adapter_kind,
                handoff_id=request.handoff_id,
                capability_sha256=capabilities.capability_sha256,
            )
        )
        if (
            prepared.session_id != session_id
            or prepared.investigation_id != investigation_id
            or prepared.task_id != task.id
            or prepared.adapter_kind is not request.adapter_kind
            or prepared.capabilities.capability_sha256 != capabilities.capability_sha256
        ):
            raise SessionOperatorContractError(
                "prepare_session answered a different session or capability probe"
            )
        nonce = secrets.token_bytes(AGENT_SESSION_SECRET_NONCE_BYTES)
        secret = deriver.derive(session_id=session_id, nonce=nonce)
        session = AgentSessionV1(
            id=session_id,
            investigation_id=investigation_id,
            task_id=task.id,
            adapter_kind=request.adapter_kind,
            state=AgentSessionState.PROVISIONING,
            state_version=1,
            capability_probe=capabilities.to_payload(),
            capability_sha256=capabilities.capability_sha256,
            probed_at=capabilities.probed_at,
            auth_secret_nonce=nonce,
            auth_secret_hash=session_secret_hash(secret),
            auth_secret_issued_at=timestamp,
            opened_at=timestamp,
            idempotency_key=key,
            created_at=timestamp,
            updated_at=timestamp,
            host_session_ref=prepared.host_session_ref,
            handoff_id=request.handoff_id,
        )
        try:
            stored, created = await uow.agent_sessions.add(session)
        except VersionConflictError as exc:
            raise SessionConflictError(str(exc)) from exc
        if not created:
            return self._replay_session(stored, request, deriver)
        ready = await self._persist(uow, stored.with_state(AgentSessionState.READY, now=timestamp))
        return ready, SessionSecret.from_bytes(secret), True

    async def _start_locked(
        self,
        uow: SessionUnitOfWork,
        session: AgentSessionV1,
        task: InvestigationTaskV1,
        operator: AgenticOperatorPort,
        timestamp: datetime,
    ) -> AgentSessionV1:
        if session.state not in (AgentSessionState.PROVISIONING, AgentSessionState.READY):
            raise SessionStateConflictError(f"a {session.state.value} session cannot be started")
        if session.host_session_ref is None:
            raise SessionOperatorContractError("the prepared session has no host reference")
        self._require_task_assignment(task)
        assigned_actor_id = task.assigned_actor_id
        if assigned_actor_id is None:  # pragma: no cover - checked just above
            raise SessionForbiddenError("the task is not assigned to an operator")
        if session.handoff_id is not None:
            handoff = await uow.investigation_handoffs.get(session.handoff_id)
            if handoff is None:
                raise HandoffTaskStaleError("the session handoff no longer exists")
            assert_startable(handoff.handoff, task)
        if session.state is AgentSessionState.PROVISIONING:
            session = await self._persist(
                uow, session.with_state(AgentSessionState.READY, now=timestamp)
            )
        status = await operator.start_session(
            StartSessionRequest(
                session_id=session.id,
                adapter_kind=session.adapter_kind,
                host_session_ref=session.host_session_ref,
                capability_sha256=session.capability_sha256,
            )
        )
        if status.session_id != session.id:
            raise SessionOperatorContractError("start_session answered another session")
        if not status.alive:
            await self._persist(
                uow,
                session.with_state(
                    AgentSessionState.FAILED,
                    now=timestamp,
                    close_reason="start_session_not_alive",
                ),
            )
            await uow.commit()
            raise SessionStartFailedError(f"the adapter did not keep session {session.id} alive")
        engine = InvestigationTaskService(
            tasks=uow.investigation_tasks,
            decisions=uow.analyst_decisions,
            sessions=_EngineSessionPort(uow.agent_sessions),
            limits=self._task_limits,
        )
        running = await engine.start(
            task_id=task.id,
            actor=TaskActor(kind=TaskActorKind.OPERATOR, actor_id=assigned_actor_id),
            expected_status_version=task.status_version,
            idempotency_key=f"session-start:{session.id}",
            session_id=session.id,
            now=timestamp,
        )
        session = await self._persist(
            uow, session.with_state(AgentSessionState.RUNNING, now=timestamp)
        )
        if running.task.lease_id is None or running.task.lease_fence < 1:
            raise SessionOperatorContractError("the task engine did not record a lease")
        return await self._persist(
            uow,
            session.with_task_lease(running.task.lease_id, running.task.lease_fence, now=timestamp),
        )

    async def _deliver_secret(self, prepared: PreparedAgentSession, idempotency_key: str) -> None:
        """Hand the secret to the host agent; the browser never receives it."""
        delivery = self._secret_delivery
        if delivery is None:
            raise SessionSecretDeliveryUnavailableError(
                "no host workspace agent is wired to receive the session secret"
            )
        session = prepared.session
        if session.host_session_ref is None:
            raise SessionOperatorContractError("the session has no host reference")
        try:
            await delivery.deliver_session_secret(
                session_id=session.id,
                investigation_id=session.investigation_id,
                task_id=session.task_id,
                adapter_kind=session.adapter_kind,
                host_session_ref=session.host_session_ref,
                secret=prepared.secret,
                idempotency_key=idempotency_key,
            )
        except Exception as exc:  # boundary: typed refusal, never a leaked secret
            raise SessionSecretDeliveryError(
                "the host agent did not accept the session secret"
            ) from exc

    async def _persist(self, uow: SessionUnitOfWork, session: AgentSessionV1) -> AgentSessionV1:
        expected = session.state_version - 1
        stored = await uow.agent_sessions.update_state(session, expected_state_version=expected)
        if stored is None:
            raise SessionCasConflictError("the agent session changed concurrently")
        return stored

    async def _persist_instruction(
        self, uow: SessionUnitOfWork, instruction: AgentSessionInstructionV1
    ) -> AgentSessionInstructionV1:
        previous = _previous_instruction_state(instruction.state)
        stored = await uow.agent_sessions.update_instruction(instruction, expected_state=previous)
        if stored is None:
            raise SessionInstructionConflictError("the instruction changed concurrently")
        return stored

    async def _probe(
        self,
        operator: AgenticOperatorPort,
        adapter_kind: OperatorAdapterKind,
        executable: str,
    ) -> OperatorCapabilitiesV1:
        capabilities = await operator.probe_capabilities(
            ProbeCapabilitiesRequest(adapter_kind=adapter_kind, executable=executable)
        )
        if capabilities.adapter_kind is not adapter_kind:
            raise SessionOperatorContractError(
                "probe_capabilities answered for another adapter kind"
            )
        if capabilities.executable != executable.strip():
            raise SessionOperatorContractError("probe_capabilities answered for another executable")
        return capabilities

    def _replay_prepare(
        self,
        session: AgentSessionV1,
        request: _PrepareRequest,
        deriver: SessionSecretDeriver,
    ) -> PreparedAgentSession:
        replayed, secret, _created = self._replay_session(session, request, deriver)
        return PreparedAgentSession(session=replayed, secret=secret)

    def _replay_session(
        self,
        session: AgentSessionV1,
        request: _PrepareRequest,
        deriver: SessionSecretDeriver,
    ) -> tuple[AgentSessionV1, SessionSecret, bool]:
        if (
            session.task_id != request.task_id
            or session.adapter_kind is not request.adapter_kind
            or session.handoff_id != request.handoff_id
            or session.capabilities().executable != request.executable.strip()
        ):
            raise SessionIdempotencyConflictError(
                "Idempotency-Key already records a different session request"
            )
        if session.state in (
            AgentSessionState.CLOSED,
            AgentSessionState.FAILED,
            AgentSessionState.LOST,
        ):
            raise SessionStateConflictError(
                f"the idempotent session is {session.state.value}; use a new key"
            )
        secret = deriver.derive(session_id=session.id, nonce=session.auth_secret_nonce)
        if session_secret_hash(secret) != session.auth_secret_hash:
            raise AgentSessionsUnavailableError("M24_AGENT_SESSION_KEY does not match this session")
        return session, SessionSecret.from_bytes(secret), False

    async def _session_for(
        self, uow: SessionUnitOfWork, investigation_id: UUID, session_id: UUID
    ) -> AgentSessionV1:
        session = await uow.agent_sessions.get(session_id)
        if session is None or session.investigation_id != investigation_id:
            raise SessionNotFoundError(f"agent session {session_id} does not exist")
        return session

    async def _locked_task(
        self, uow: SessionUnitOfWork, investigation_id: UUID, task_id: UUID
    ) -> InvestigationTaskV1:
        if await uow.analyst_investigations.get(investigation_id) is None:
            raise SessionNotFoundError(f"investigation {investigation_id} does not exist")
        task = await uow.investigation_tasks.lock_task(task_id)
        if task is None or task.investigation_id != investigation_id:
            raise SessionNotFoundError(
                f"task {task_id} is not part of investigation {investigation_id}"
            )
        return task

    async def _handoff_for(
        self,
        uow: SessionUnitOfWork,
        investigation_id: UUID,
        task: InvestigationTaskV1,
        handoff_id: UUID,
    ) -> HandoffRecord:
        handoff = await uow.investigation_handoffs.get(handoff_id)
        if (
            handoff is None
            or handoff.handoff.task_id != str(task.id)
            or handoff.handoff.investigation_id != str(investigation_id)
        ):
            raise SessionNotFoundError(f"handoff {handoff_id} is not part of task {task.id}")
        return handoff

    async def _require_unclaimed_handoff(
        self, uow: SessionUnitOfWork, task: InvestigationTaskV1, handoff_id: UUID
    ) -> None:
        """After an unresumable loss, a new session needs a new handoff (D7)."""
        for prior in await uow.agent_sessions.list_for_task(task.id):
            if prior.state is not AgentSessionState.LOST or prior.handoff_id != handoff_id:
                continue
            if prior.reconciliation_outcome() not in RECONCILE_OUTCOMES_RESUMABLE:
                raise SessionNewHandoffRequiredError(
                    "the previous session was lost without a proven resume; "
                    "create a new handoff before opening a new session"
                )

    @staticmethod
    def _require_human(actor: TaskActor) -> None:
        if not isinstance(actor, TaskActor):
            raise TypeError("actor must use TaskActor")
        if actor.kind is not TaskActorKind.HUMAN:
            raise SessionForbiddenError("only a verified human performs this session action")

    @staticmethod
    def _require_operator_actor(actor: TaskActor) -> None:
        if not isinstance(actor, TaskActor):
            raise TypeError("actor must use TaskActor")
        if actor.kind is not TaskActorKind.OPERATOR:
            raise SessionForbiddenError("only the owning operator performs this session action")

    @staticmethod
    def _require_task_assignment(task: InvestigationTaskV1) -> None:
        if task.assigned_actor_kind is not TaskActorKind.OPERATOR or task.assigned_actor_id is None:
            raise SessionForbiddenError("an agent session requires a task assigned to an operator")

    def _require_deriver(self) -> SessionSecretDeriver:
        if self._deriver is None:
            raise AgentSessionsUnavailableError(
                "M24_AGENT_SESSION_KEY is not configured; agent sessions are disabled"
            )
        return self._deriver

    def _require_operator(self) -> AgenticOperatorPort:
        if self._operator is None:
            raise SessionOperatorUnavailableError(
                "no agentic operator adapter is wired; the session cannot be launched"
            )
        return self._operator

    async def _require_current_task_lease(
        self, uow: SessionUnitOfWork, session: AgentSessionV1, now: datetime
    ) -> None:
        task = await uow.investigation_tasks.get(session.task_id)
        if (
            task is None
            or session.task_lease_fence is None
            or task.lease_session_id != session.id
            or task.lease_fence != session.task_lease_fence
            or task.lease_expires_at is None
            or task.lease_expires_at <= now
            or task.status not in (TaskStatus.RUNNING, TaskStatus.AWAITING_INPUT)
        ):
            raise SessionStateConflictError(
                "the task lease is expired or superseded; the operator cannot write"
            )

    @staticmethod
    def _require_reconciliation_answer(
        session: AgentSessionV1, result: SessionReconciliationV1
    ) -> None:
        if result.session_id != session.id:
            raise SessionOperatorContractError("reconcile_session answered another session")


def _previous_instruction_state(state: InstructionState) -> InstructionState:
    if state is InstructionState.DELIVERED:
        return InstructionState.QUEUED
    if state is InstructionState.ACKNOWLEDGED:
        return InstructionState.DELIVERED
    raise SessionInstructionConflictError(
        "only delivery or acknowledgement advances an instruction"
    )


def _idempotency_key(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SessionError("Idempotency-Key must be a non-empty string")
    if len(value) > 255:
        raise SessionError("Idempotency-Key is bounded to 255 characters")
    return value.strip()


def _now(clock: Callable[[], datetime], value: datetime | None) -> datetime:
    timestamp = clock() if value is None else value
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    return timestamp


__all__ = [
    "AgentSessionSecretDeliveryPort",
    "PreparedAgentSession",
    "SessionCasConflictError",
    "SessionConflictError",
    "SessionError",
    "SessionForbiddenError",
    "SessionIdempotencyConflictError",
    "SessionInstructionConflictError",
    "SessionNewHandoffRequiredError",
    "SessionNotFoundError",
    "SessionOperatorContractError",
    "SessionOperatorUnavailableError",
    "SessionResumeUnavailableError",
    "SessionSecret",
    "SessionSecretDeliveryError",
    "SessionSecretDeliveryUnavailableError",
    "SessionService",
    "SessionStartFailedError",
    "SessionStateConflictError",
    "SessionUnitOfWork",
]
