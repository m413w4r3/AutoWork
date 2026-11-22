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

import asyncio
import hashlib
import secrets
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import TracebackType
from typing import Any, Protocol, Self
from uuid import UUID, uuid4

from cti_app.application.agent_session_verification import (
    AgentSessionsUnavailableError,
    SessionSecretDeriver,
    session_secret_hash,
    session_secret_hex,
)
from cti_app.application.agentic_host_sessions import (
    HostReasonRequestV1,
    HostSessionNotFoundError,
    HostSessionsPort,
    HostSessionStateConflictError,
)
from cti_app.application.investigation_handoffs import (
    HandoffTaskStaleError,
    assert_startable,
)
from cti_app.application.investigation_persistence import (
    AgentSessionRepository,
    HandoffRecord,
    InvestigationEnvelopeRepository,
    InvestigationHandoffRepository,
    InvestigationTaskRepository,
    VersionConflictError,
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
from cti_app.application.workspace_agent import WorkspaceAgentError
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
from cti_app.domain.investigation import canonical_json_bytes
from cti_app.domain.investigation_task import (
    TASK_LIMIT_DEFAULTS,
    InvestigationTaskV1,
    TaskActor,
    TaskActorKind,
    TaskLimits,
    TaskStatus,
)

_CLI_NETWORK_MODES = frozenset({"isolated_egress", "host_network_unsafe"})


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


class SessionRuntimeUnavailableError(SessionError):
    """The host runtime was mounted unavailable (no socket, token or UoW)."""

    code = "agent_sessions_unavailable"
    status_code = 503


class SessionOperatorContractError(SessionError):
    """An adapter returned a result that does not answer the request."""

    code = "agentic_operator_contract_invalid"
    status_code = 502


class SessionExternalDisclosureDeniedError(SessionError):
    """The investigation's G2/TLP disclosure policy refuses an operator session (R03 B2)."""

    code = "agent_session_external_llm_not_allowed"
    status_code = 403


class SessionNetworkIsolationUnavailableError(SessionError):
    """No real CLI session may run without an attested loopback isolation (R03 B1)."""

    code = "agent_session_network_isolation_unavailable"
    status_code = 503


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


class NoteInstructionUnavailableError(SessionError):
    """A note-referenced instruction arrived without a canonical note resolver."""

    code = "note_refs_unavailable"
    status_code = 503


@dataclass(frozen=True, slots=True)
class NoteInstructionResolution:
    """One instruction text with its canonical note refs and delivery diff.

    ``queued_text`` is what the persistent queue records (the human's text plus
    the exact revision ids/SHA-256 resolved at send time).  ``delivery_text``
    adds the visible diff a note changed after queueing; it is the text handed
    to the host when the instruction is actually delivered.  Both are prose and
    stay out of ``repr``.
    """

    queued_text: str = field(repr=False)
    delivery_text: str = field(repr=False)
    note_diffs: tuple[Mapping[str, str], ...] = ()


class InstructionNoteResolver(Protocol):
    """Resolve canonical note revisions for a note-referenced instruction."""

    async def prepare(
        self, *, investigation_id: UUID, text: str, note_refs: Sequence[str]
    ) -> NoteInstructionResolution: ...

    async def refresh(self, *, investigation_id: UUID, text: str) -> NoteInstructionResolution: ...


@dataclass(frozen=True, slots=True)
class EnqueuedInstructionV1:
    """The queue view the human needs: position and any delivery-time diff."""

    instruction: AgentSessionInstructionV1
    queue_position: int
    delivered_live: bool
    note_diffs: tuple[Mapping[str, str], ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.instruction, AgentSessionInstructionV1):
            raise TypeError("instruction must use AgentSessionInstructionV1")
        if isinstance(self.queue_position, bool) or not isinstance(self.queue_position, int):
            raise TypeError("queue_position must be an integer")
        if self.queue_position < 0:
            raise ValueError("queue_position is zero (delivered) or one-based")
        if not isinstance(self.delivered_live, bool):
            raise TypeError("delivered_live must be a boolean")

    def to_payload(self) -> dict[str, Any]:
        payload = self.instruction.to_payload()
        payload["queue_position"] = self.queue_position
        payload["delivered_live"] = self.delivered_live
        payload["note_diffs"] = [dict(item) for item in self.note_diffs]
        return payload


@dataclass(frozen=True, slots=True)
class DeliveredInstructionV1:
    """One acknowledged instruction plus the text an operator actually receives."""

    instruction: AgentSessionInstructionV1
    delivery_text: str = field(repr=False)
    note_diffs: tuple[Mapping[str, str], ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.instruction, AgentSessionInstructionV1):
            raise TypeError("instruction must use AgentSessionInstructionV1")
        if not isinstance(self.delivery_text, str):
            raise TypeError("delivery_text must be a string")

    def to_payload(self) -> dict[str, Any]:
        payload = self.instruction.to_payload()
        payload["delivery_text"] = self.delivery_text
        payload["note_diffs"] = [dict(item) for item in self.note_diffs]
        return payload


class SessionUnitOfWork(Protocol):
    """The canonical stores one session operation reads or writes."""

    analyst_investigations: AnalystInvestigationRepository
    analyst_decisions: AnalystDecisionRepository
    investigation_envelopes: InvestigationEnvelopeRepository
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


_HOST_CLOSABLE_STATES = frozenset({"ready", "stopped", "lost", "failed", "closed"})
_HOST_RELEASE_POLL_SECONDS = 0.2


class HostSessionReleaseService:
    """Request host teardown for sessions the DB already released (B2).

    Protocol: the command transaction never talks to the host.  It closes the
    ``agent_sessions`` row (idempotent CAS) and buffers a
    :class:`TaskSessionRelease`; the caller flushes this service **after its
    commit**, so no advisory lock, row lock or pool transaction is held across
    the UDS call.  A missing host runtime, an unreachable socket or a host that
    refuses to stop is recorded on the session as a D7 ``lost_unreconciled``
    reconciliation marker — never silently swallowed and never retried as a
    relaunch.  The host ``close``/``interrupt`` keys are deterministic, so a
    retry of the same release replays host-side.
    """

    def __init__(
        self,
        *,
        uow_factory: Callable[[], SessionUnitOfWork],
        host: HostSessionsPort | None,
        clock: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        wait_seconds: float = 5.0,
    ) -> None:
        self._uow_factory = uow_factory
        self._host = host
        self._clock = clock or (lambda: datetime.now(UTC))
        self._monotonic = monotonic or time.monotonic
        self._sleep = sleep or asyncio.sleep
        self._wait_seconds = max(0.0, wait_seconds)

    async def flush(self, releases: Iterable[TaskSessionRelease]) -> None:
        """Best-effort host teardown; never raises for a host-side failure."""
        seen: set[UUID] = set()
        for release in releases:
            if release.session_id in seen:
                continue
            seen.add(release.session_id)
            await self._release(release)

    async def _release(self, release: TaskSessionRelease) -> None:
        async with self._uow_factory() as uow:
            session = await uow.agent_sessions.get(release.session_id)
        if session is None or session.host_session_ref is None:
            # No host object was ever prepared for this row: nothing to stop.
            return
        host = self._host
        if host is None:
            await self._mark_unconfirmed(session, "host runtime is not wired")
            return
        unconfirmed = await self._close_host(host, session, release.reason)
        if unconfirmed is not None:
            await self._mark_unconfirmed(session, unconfirmed)

    async def _close_host(
        self, host: HostSessionsPort, session: AgentSessionV1, reason: str
    ) -> str | None:
        """Return a reason when the host teardown is unconfirmed."""
        wire_reason = f"task lease released: {reason}".strip()
        try:
            await host.close_session(
                session.id,
                HostReasonRequestV1(reason=wire_reason),
                idempotency_key=_host_release_key("close", session.id),
            )
            return None
        except HostSessionNotFoundError:
            return None
        except HostSessionStateConflictError:
            pass
        except WorkspaceAgentError as exc:
            return f"host close failed ({type(exc).__name__})"
        try:
            await host.interrupt_session(
                session.id,
                HostReasonRequestV1(reason=wire_reason),
                idempotency_key=_host_release_key("interrupt", session.id),
            )
        except HostSessionNotFoundError:
            return None
        except WorkspaceAgentError as exc:
            return f"host interrupt failed ({type(exc).__name__})"
        deadline = self._monotonic() + self._wait_seconds
        while True:
            try:
                status = await host.get_session_status(session.id)
            except HostSessionNotFoundError:
                return None
            except WorkspaceAgentError as exc:
                return f"host status failed ({type(exc).__name__})"
            if status.state in _HOST_CLOSABLE_STATES:
                break
            if self._monotonic() >= deadline:
                return "the host process did not stop after the interrupt"
            await self._sleep(_HOST_RELEASE_POLL_SECONDS)
        try:
            await host.close_session(
                session.id,
                HostReasonRequestV1(reason=wire_reason),
                idempotency_key=_host_release_key("close", session.id),
            )
        except HostSessionNotFoundError:
            return None
        except WorkspaceAgentError as exc:
            return f"host close after interrupt failed ({type(exc).__name__})"
        return None

    async def _mark_unconfirmed(self, session: AgentSessionV1, detail: str) -> None:
        """Persist a ``lost_unreconciled`` marker without overwriting a real one."""
        now = self._clock()
        payload = {
            "outcome": SessionReconcileOutcome.LOST_UNRECONCILED.value,
            "detail": f"host release unconfirmed: {detail}",
            "observed_at": now.isoformat(),
            "session_id": str(session.id),
        }
        digest = hashlib.sha256(canonical_json_bytes(payload)).hexdigest()
        async with self._uow_factory() as uow:
            current = await uow.agent_sessions.get(session.id)
            if current is None or current.reconciliation is not None or current.is_active:
                # A still-active row belongs to a live lease path; a terminal
                # or stopped row may carry the transport marker.  An existing
                # typed D7 outcome is never overwritten.
                return
            marked = current.with_reconciliation(payload, digest, now=now)
            if (
                await uow.agent_sessions.update_state(
                    marked, expected_state_version=current.state_version
                )
                is None
            ):
                # A concurrent writer recorded its own typed outcome; that is
                # stronger than this transport marker.
                return
            await uow.commit()


def _host_release_key(action: str, session_id: UUID) -> str:
    return f"task-release.{action}.{session_id}"


@dataclass(frozen=True, slots=True)
class _PrepareRequest:
    """The request identity an idempotent replay must match exactly."""

    task_id: UUID
    adapter_kind: OperatorAdapterKind
    handoff_id: UUID
    executable: str
    model: str | None = None


class SessionService:
    """Prepare/open/start/drive/close one technical operator session."""

    def __init__(
        self,
        *,
        uow_factory: Callable[[], SessionUnitOfWork],
        deriver: SessionSecretDeriver | None,
        operator: AgenticOperatorPort | None = None,
        secret_delivery: AgentSessionSecretDeliveryPort | None = None,
        note_resolver: InstructionNoteResolver | None = None,
        disclosure: Callable[[UUID], Awaitable[None]] | None = None,
        cli_network_mode: str = "isolated_egress",
        task_limits: TaskLimits = TASK_LIMIT_DEFAULTS,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if cli_network_mode not in _CLI_NETWORK_MODES:
            raise ValueError("cli_network_mode must be isolated_egress or host_network_unsafe")
        self._uow_factory = uow_factory
        self._deriver = deriver
        self._operator = operator
        self._secret_delivery = secret_delivery
        self._note_resolver = note_resolver
        self._disclosure = disclosure
        self._cli_network_mode = cli_network_mode
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
        model: str | None = None,
        now: datetime | None = None,
    ) -> PreparedAgentSession:
        """Create the durable session in ``provisioning``/``ready`` and issue
        the secret.  The adapter is probed (real facts only) and asked to
        prepare its host session; a replay never probes a second host session.

        Lock protocol (B3/M1): the probe is a read-only host call made before
        any transaction; the row is committed in ``provisioning`` under the
        advisory-then-task-row order; only then are the host prepare and the
        one-shot secret delivery performed, outside every lock.  A failure
        after the row exists marks the session ``failed`` (the durable marker)
        instead of rolling back into an untracked host process.
        """
        request = _PrepareRequest(
            task_id=task_id,
            adapter_kind=adapter_kind,
            handoff_id=handoff_id,
            executable=executable,
            model=model,
        )
        key = _idempotency_key(idempotency_key)
        timestamp = _now(self._clock, now)
        self._require_human(actor)
        deriver = self._require_deriver()
        operator = self._require_operator()
        await self._require_external_disclosure(investigation_id)
        replayed = await self._existing_prepared(investigation_id, request, deriver, key)
        if replayed is not None:
            # A replay never probes or prepares a second host session.
            self._require_network_attestation(
                replayed.session.adapter_kind, replayed.session.capabilities()
            )
            await self._deliver_secret(replayed, key)
            return replayed
        capabilities = await self._probe(operator, adapter_kind, executable)
        self._require_network_attestation(adapter_kind, capabilities)
        provisioned, created = await self._provision(
            investigation_id, request, capabilities, deriver, key, timestamp
        )
        if not created:
            await self._deliver_secret(provisioned, key)
            return provisioned
        prepared = await self._prepare_host(operator, provisioned, request, key, timestamp)
        session = await self._transition_prepared(prepared.session, timestamp)
        return PreparedAgentSession(session=session, secret=prepared.secret)

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
        model: str | None = None,
        now: datetime | None = None,
    ) -> PreparedAgentSession:
        """The human "open a session" action: prepare, deliver, start.

        A stale handoff (D5) refuses before anything is persisted, so a failed
        open never leaves an active ``provisioning`` session blocking the task.
        The order is the host contract's: the one-shot secret delivery *is* the
        supervisor prepare (SESSION-RUNNER §6), so start can only follow it.
        Every host call happens outside the advisory/row locks (B3): the
        session row is committed ``provisioning``/``ready`` first, then the
        host is prepared and started, then a short transaction acquires the D3
        lease.  A failed activation compensates through ``failed`` + interrupt,
        never by leaving a runnable session without a lease.
        """
        request = _PrepareRequest(
            task_id=task_id,
            adapter_kind=adapter_kind,
            handoff_id=handoff_id,
            executable=executable,
            model=model,
        )
        key = _idempotency_key(idempotency_key)
        timestamp = _now(self._clock, now)
        self._require_human(actor)
        deriver = self._require_deriver()
        operator = self._require_operator()
        await self._require_external_disclosure(investigation_id)
        replayed = await self._existing_prepared(investigation_id, request, deriver, key)
        if replayed is not None:
            # A replay re-derives the same secret and re-delivers it; the host
            # refuses a second, different hand-off.  It never starts anything:
            # a concurrent identical open already did.
            self._require_network_attestation(
                replayed.session.adapter_kind, replayed.session.capabilities()
            )
            await self._deliver_secret(replayed, key)
            return replayed
        capabilities = await self._probe(operator, adapter_kind, executable)
        self._require_network_attestation(adapter_kind, capabilities)
        provisioned, created = await self._provision(
            investigation_id, request, capabilities, deriver, key, timestamp
        )
        if not created:
            await self._deliver_secret(provisioned, key)
            return provisioned
        prepared = await self._prepare_host(operator, provisioned, request, key, timestamp)
        ready = await self._transition_prepared(prepared.session, timestamp)
        prepared = PreparedAgentSession(session=ready, secret=prepared.secret)
        await self._host_start(operator, ready, timestamp)
        return await self._activate_prepared(operator, investigation_id, prepared, timestamp)

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
        acquired lease pair on the session.  Validation, host start and lease
        acquisition are three separate short phases: the host call runs with no
        lock held, and a failed lease acquisition compensates the host run.
        """
        self._require_human(actor)
        operator = self._require_operator()
        timestamp = _now(self._clock, now)
        await self._require_external_disclosure(investigation_id)
        async with self._uow_factory() as uow:
            await uow.investigation_tasks.lock_investigation(investigation_id)
            session = await self._session_for(uow, investigation_id, session_id)
            self._require_network_attestation(session.adapter_kind, session.capabilities())
            task = await self._locked_task(uow, investigation_id, session.task_id)
            await self._require_startable_locked(uow, session, task)
            await uow.commit()
        await self._host_start(operator, session, timestamp)
        try:
            return await self._activate_lease(investigation_id, session_id, timestamp)
        except Exception:
            await self._compensate_failed_activation(operator, session, "lease_activation_failed")
            raise

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
        note_refs: Sequence[str] = (),
        now: datetime | None = None,
    ) -> AgentSessionInstructionV1:
        """Persist one human instruction; deliver it only to an idle session.

        A busy (``running``) agent keeps the instruction queued; the operator
        acknowledges it through its signed route when it consumes the queue.
        """
        enqueued = await self.enqueue_instruction_with_notes(
            investigation_id,
            session_id,
            text=text,
            actor=actor,
            idempotency_key=idempotency_key,
            note_refs=note_refs,
            now=now,
        )
        return enqueued.instruction

    async def enqueue_instruction_with_notes(
        self,
        investigation_id: UUID,
        session_id: UUID,
        *,
        text: str,
        actor: TaskActor,
        idempotency_key: str,
        note_refs: Sequence[str] = (),
        now: datetime | None = None,
    ) -> EnqueuedInstructionV1:
        """Queue an instruction and resolve its canonical note refs at send time.

        ``note_refs`` are workspace paths under ``notes/humaines``.  The
        resolver records the LATEST ``(revision_id, sha256)`` for each path in
        the persistent instruction text, so the queue itself carries exact
        refs.  Delivery happens only to an explicitly idle
        (``awaiting_instruction``) session, never to a session whose technical
        state is unknown; a note modified after queueing is reported as a
        visible diff appended to the delivered text.
        """
        key = _idempotency_key(idempotency_key)
        timestamp = _now(self._clock, now)
        self._require_human(actor)
        resolution: NoteInstructionResolution | None = None
        composed_text = text.strip()
        if note_refs:
            resolver = self._require_note_resolver()
            resolution = await resolver.prepare(
                investigation_id=investigation_id, text=text, note_refs=note_refs
            )
            composed_text = resolution.queued_text
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
                text=composed_text,
                text_sha256=instruction_text_sha256(composed_text),
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
                return EnqueuedInstructionV1(
                    instruction=stored,
                    queue_position=await _queue_position(uow, session.id, stored),
                    delivered_live=False,
                )
            delivered = stored
            delivered_live = False
            note_diffs: tuple[Mapping[str, str], ...] = ()
            if (
                session.state is AgentSessionState.AWAITING_INSTRUCTION
                and self._operator is not None
                and session.host_session_ref is not None
            ):
                delivery_text = stored.text
                if resolution is not None:
                    refreshed = await self._require_note_resolver().refresh(
                        investigation_id=investigation_id, text=stored.text
                    )
                    delivery_text = refreshed.delivery_text
                    note_diffs = refreshed.note_diffs
                receipt = await self._operator.send_instruction(
                    SendInstructionRequest(
                        session_id=session.id,
                        host_session_ref=session.host_session_ref,
                        instruction_id=stored.id,
                        instruction_sha256=instruction_text_sha256(delivery_text),
                        text=delivery_text,
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
                    delivered_live = True
            queue_position = await _queue_position(uow, session.id, delivered)
            await uow.commit()
            return EnqueuedInstructionV1(
                instruction=delivered,
                queue_position=queue_position,
                delivered_live=delivered_live,
                note_diffs=note_diffs,
            )

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

    async def acknowledge_instructions_with_notes(
        self,
        investigation_id: UUID,
        session_id: UUID,
        *,
        instruction_ids: Sequence[UUID],
        actor: TaskActor,
        now: datetime | None = None,
    ) -> tuple[DeliveredInstructionV1, ...]:
        """Acknowledge and report the note diff visible at delivery time."""
        acknowledged = await self.acknowledge_instructions(
            investigation_id,
            session_id,
            instruction_ids=instruction_ids,
            actor=actor,
            now=now,
        )
        resolver = self._note_resolver
        views: list[DeliveredInstructionV1] = []
        for instruction in acknowledged:
            if resolver is None:
                views.append(
                    DeliveredInstructionV1(instruction=instruction, delivery_text=instruction.text)
                )
                continue
            refreshed = await resolver.refresh(
                investigation_id=investigation_id, text=instruction.text
            )
            views.append(
                DeliveredInstructionV1(
                    instruction=instruction,
                    delivery_text=refreshed.delivery_text,
                    note_diffs=refreshed.note_diffs,
                )
            )
        return tuple(views)

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
        await self._require_external_disclosure(investigation_id)
        async with self._uow_factory() as uow:
            session = await self._session_for(uow, investigation_id, session_id)
            self._require_network_attestation(session.adapter_kind, session.capabilities())
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
        return await self._reconcile_locked(
            investigation_id, session_id, operator=operator, timestamp=timestamp
        )

    async def reconcile_for_transfer(
        self,
        investigation_id: UUID,
        session_id: UUID,
        *,
        actor: TaskActor,
        now: datetime | None = None,
    ) -> AgentSessionV1:
        """Human-initiated reconciliation inside a transfer (ADR-0011 D3).

        A transfer must learn whether an old host process is alive before it
        releases the lease; the operation is technical (it records the host
        answer and relaunches nothing), but the decision to transfer is the
        verified human's, so this seam admits only that actor.
        """
        timestamp = _now(self._clock, now)
        self._require_human(actor)
        operator = self._require_operator()
        return await self._reconcile_locked(
            investigation_id, session_id, operator=operator, timestamp=timestamp
        )

    async def _reconcile_locked(
        self,
        investigation_id: UUID,
        session_id: UUID,
        *,
        operator: AgenticOperatorPort,
        timestamp: datetime,
    ) -> AgentSessionV1:
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

    async def _existing_prepared(
        self,
        investigation_id: UUID,
        request: _PrepareRequest,
        deriver: SessionSecretDeriver,
        key: str,
    ) -> PreparedAgentSession | None:
        """Cheap unlocked replay lookup so a replay never probes the host again.

        The authoritative replay check remains inside ``_provision`` under the
        advisory/task lock; this one only avoids a second capability probe.
        """
        async with self._uow_factory() as uow:
            existing = await uow.agent_sessions.get_by_idempotency(request.task_id, key)
        if existing is None or existing.investigation_id != investigation_id:
            return None
        return self._replay_prepare(existing, request, deriver)

    async def _provision(
        self,
        investigation_id: UUID,
        request: _PrepareRequest,
        capabilities: OperatorCapabilitiesV1,
        deriver: SessionSecretDeriver,
        key: str,
        timestamp: datetime,
    ) -> tuple[PreparedAgentSession, bool]:
        """Commit the durable ``provisioning`` row under advisory-then-row locks.

        This is the only part of prepare that holds locks; the host probe has
        already happened outside them, and no host object is created here.
        """
        async with self._uow_factory() as uow:
            await uow.investigation_tasks.lock_investigation(investigation_id)
            task = await self._locked_task(uow, investigation_id, request.task_id)
            existing = await uow.agent_sessions.get_by_idempotency(task.id, key)
            if existing is not None:
                prepared = self._replay_prepare(existing, request, deriver)
                await uow.commit()
                return prepared, False
            self._require_task_assignment(task)
            handoff = await self._handoff_for(uow, investigation_id, task, request.handoff_id)
            assert_startable(handoff.handoff, task)
            await self._require_unclaimed_handoff(uow, task, request.handoff_id)
            session_id = uuid4()
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
                host_session_ref=None,
                handoff_id=request.handoff_id,
            )
            try:
                stored, created = await uow.agent_sessions.add(session)
            except VersionConflictError as exc:
                raise SessionConflictError(str(exc)) from exc
            if not created:
                prepared = self._replay_prepare(stored, request, deriver)
                await uow.commit()
                return prepared, False
            await uow.commit()
            return PreparedAgentSession(
                session=stored, secret=SessionSecret.from_bytes(secret)
            ), True

    async def _prepare_host(
        self,
        operator: AgenticOperatorPort,
        prepared: PreparedAgentSession,
        request: _PrepareRequest,
        key: str,
        timestamp: datetime,
    ) -> PreparedAgentSession:
        """Host prepare + one-shot secret delivery, with no lock held."""
        session = prepared.session
        try:
            host = await operator.prepare_session(
                PrepareSessionRequest(
                    session_id=session.id,
                    investigation_id=session.investigation_id,
                    task_id=session.task_id,
                    adapter_kind=request.adapter_kind,
                    handoff_id=request.handoff_id,
                    capability_sha256=session.capability_sha256,
                    model=request.model,
                )
            )
            if (
                host.session_id != session.id
                or host.investigation_id != session.investigation_id
                or host.task_id != session.task_id
                or host.adapter_kind is not request.adapter_kind
                or host.capabilities.capability_sha256 != session.capability_sha256
            ):
                raise SessionOperatorContractError(
                    "prepare_session answered a different session or capability probe"
                )
            recorded = await self._record_host_ref(session, host.host_session_ref)
            prepared = PreparedAgentSession(session=recorded, secret=prepared.secret)
            await self._deliver_secret(prepared, key)
            return prepared
        except Exception:
            await self._fail_session(session.id, "prepare_failed_before_start")
            raise

    async def _record_host_ref(
        self, session: AgentSessionV1, host_session_ref: str
    ) -> AgentSessionV1:
        async with self._uow_factory() as uow:
            current = await uow.agent_sessions.get(session.id)
            if current is None:
                raise SessionNotFoundError(f"agent session {session.id} does not exist")
            if current.is_terminal:
                raise SessionStateConflictError(
                    f"a {current.state.value} session cannot record a host reference"
                )
            return await self._commit_session(
                uow, current.with_host_session_ref(host_session_ref, now=self._clock())
            )

    async def _transition_prepared(
        self, session: AgentSessionV1, timestamp: datetime
    ) -> AgentSessionV1:
        """PROVISIONING -> READY in one short advisory-then-row transaction."""
        async with self._uow_factory() as uow:
            await uow.investigation_tasks.lock_investigation(session.investigation_id)
            current = await uow.agent_sessions.get(session.id)
            if current is None:
                raise SessionNotFoundError(f"agent session {session.id} does not exist")
            if current.state is AgentSessionState.READY:
                return current
            if current.state is not AgentSessionState.PROVISIONING:
                raise SessionStateConflictError(
                    f"a {current.state.value} session cannot become ready"
                )
            return await self._commit_session(
                uow, current.with_state(AgentSessionState.READY, now=timestamp)
            )

    async def _host_start(
        self, operator: AgenticOperatorPort, session: AgentSessionV1, timestamp: datetime
    ) -> None:
        """Start the host run with no lock held; a dead run is durably failed."""
        if session.host_session_ref is None:
            raise SessionOperatorContractError("the prepared session has no host reference")
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
            await self._fail_session(session.id, "start_session_not_alive")
            raise SessionStartFailedError(f"the adapter did not keep session {session.id} alive")

    async def _activate_prepared(
        self,
        operator: AgenticOperatorPort,
        investigation_id: UUID,
        prepared: PreparedAgentSession,
        timestamp: datetime,
    ) -> PreparedAgentSession:
        try:
            session = await self._activate_lease(investigation_id, prepared.session.id, timestamp)
        except Exception:
            await self._compensate_failed_activation(
                operator, prepared.session, "lease_activation_failed"
            )
            raise
        return PreparedAgentSession(session=session, secret=prepared.secret)

    async def _activate_lease(
        self, investigation_id: UUID, session_id: UUID, timestamp: datetime
    ) -> AgentSessionV1:
        """Short transaction: D3 lease acquisition + session RUNNING mirror."""
        async with self._uow_factory() as uow:
            await uow.investigation_tasks.lock_investigation(investigation_id)
            session = await self._session_for(uow, investigation_id, session_id)
            task = await self._locked_task(uow, investigation_id, session.task_id)
            return await self._start_engine_locked(uow, session, task, timestamp)

    async def _start_engine_locked(
        self,
        uow: SessionUnitOfWork,
        session: AgentSessionV1,
        task: InvestigationTaskV1,
        timestamp: datetime,
    ) -> AgentSessionV1:
        await self._require_startable_locked(uow, session, task)
        assigned_actor_id = task.assigned_actor_id
        if assigned_actor_id is None:  # pragma: no cover - checked just above
            raise SessionForbiddenError("the task is not assigned to an operator")
        if session.state is AgentSessionState.PROVISIONING:
            session = await self._persist(
                uow, session.with_state(AgentSessionState.READY, now=timestamp)
            )
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

    async def _require_startable_locked(
        self,
        uow: SessionUnitOfWork,
        session: AgentSessionV1,
        task: InvestigationTaskV1,
    ) -> None:
        if session.state not in (AgentSessionState.PROVISIONING, AgentSessionState.READY):
            raise SessionStateConflictError(f"a {session.state.value} session cannot be started")
        if session.host_session_ref is None:
            raise SessionOperatorContractError("the prepared session has no host reference")
        self._require_task_assignment(task)
        if task.assigned_actor_id is None:  # pragma: no cover - checked just above
            raise SessionForbiddenError("the task is not assigned to an operator")
        if session.handoff_id is not None:
            handoff = await uow.investigation_handoffs.get(session.handoff_id)
            if handoff is None:
                raise HandoffTaskStaleError("the session handoff no longer exists")
            assert_startable(handoff.handoff, task)

    async def _fail_session(self, session_id: UUID, reason: str) -> None:
        """Durably mark an unleased prepared/running session ``failed``."""
        async with self._uow_factory() as uow:
            current = await uow.agent_sessions.get(session_id)
            if current is None or current.is_terminal or current.task_lease_id is not None:
                return
            if current.state not in (
                AgentSessionState.PROVISIONING,
                AgentSessionState.READY,
            ):
                return
            failed = current.with_state(
                AgentSessionState.FAILED, now=self._clock(), close_reason=reason
            )
            stored = await uow.agent_sessions.update_state(
                failed, expected_state_version=current.state_version
            )
            if stored is not None:
                await uow.commit()

    async def _compensate_failed_activation(
        self, operator: AgenticOperatorPort, session: AgentSessionV1, reason: str
    ) -> None:
        """Stop a host run whose lease never committed; never relaunch it.

        The ``failed`` row is written first and is the durable marker; the
        interrupt is best effort because the host process may already be gone
        or the adapter may not be prepared in this process (M2).  A lost
        effect is visible in ``agent_sessions``/``reconciliation``, not silent.
        """
        await self._fail_session(session.id, reason)
        if session.host_session_ref is None:
            return
        try:
            await operator.interrupt_session(
                InterruptSessionRequest(
                    session_id=session.id,
                    host_session_ref=session.host_session_ref,
                    reason=reason,
                )
            )
        except WorkspaceAgentError:
            return

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

    async def _commit_session(
        self, uow: SessionUnitOfWork, session: AgentSessionV1
    ) -> AgentSessionV1:
        stored = await self._persist(uow, session)
        await uow.commit()
        return stored

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

    async def probe_session_capabilities(
        self, adapter_kind: OperatorAdapterKind, *, executable: str
    ) -> OperatorCapabilitiesV1:
        """The read-only capability probe the cockpit uses (no session, no secret)."""
        operator = self._require_operator()
        capabilities = await self._probe(operator, adapter_kind, executable)
        self._require_network_attestation(adapter_kind, capabilities)
        return capabilities

    async def _require_external_disclosure(self, investigation_id: UUID) -> None:
        """R03 B2: the canonical G2/TLP gate, before every host call and row write."""
        gate = self._disclosure
        if gate is None:
            # A service built without the canonical gate must never open a
            # session that can disclose evidence to an external provider.
            raise SessionExternalDisclosureDeniedError(
                "no canonical external-disclosure gate is wired; operator sessions are refused",
                details={"reason": "gate_unavailable"},
            )
        await gate(investigation_id)

    def _require_network_attestation(
        self, adapter_kind: OperatorAdapterKind, capabilities: OperatorCapabilitiesV1
    ) -> None:
        """R03 B1: a real CLI session needs the attested loopback isolation.

        ``host_network_unsafe`` is an explicit development override; even then
        the host must have attested that same mode, so a deployment that forgot
        the override still fails closed instead of silently sharing the host
        network.
        """
        if capabilities.adapter_kind is not adapter_kind:
            raise SessionOperatorContractError("the capability probe names another adapter")
        confinement = capabilities.confinement
        if self._cli_network_mode == "host_network_unsafe":
            if confinement is not None and confinement.network_mode not in (
                None,
                "host_network_unsafe",
            ):
                raise SessionNetworkIsolationUnavailableError(
                    "the host attested an isolated network while the backend is in its explicit "
                    "host_network_unsafe override",
                    details={"reason": "network_mode_mismatch"},
                )
            return
        if confinement is None:
            raise SessionNetworkIsolationUnavailableError(
                "the host did not attest a CLI network mode; refusing the real session",
                details={"reason": "attestation_absent"},
            )
        if (
            confinement.network_mode != "isolated_egress"
            or not confinement.loopback_isolated
            or not confinement.netns
        ):
            raise SessionNetworkIsolationUnavailableError(
                "the host CLI sandbox is not attested loopback-isolated; refusing the real session",
                details={
                    "reason": "loopback_not_isolated",
                    "network_mode": str(confinement.network_mode),
                },
            )

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

    def _require_note_resolver(self) -> InstructionNoteResolver:
        if self._note_resolver is None:
            raise NoteInstructionUnavailableError(
                "no canonical note resolver is wired; note-referenced instructions are unavailable"
            )
        return self._note_resolver

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


async def _queue_position(
    uow: SessionUnitOfWork,
    session_id: UUID,
    instruction: AgentSessionInstructionV1,
) -> int:
    """One-based position among still-queued instructions (0 once delivered)."""
    if instruction.state is not InstructionState.QUEUED:
        return 0
    instructions = await uow.agent_sessions.list_instructions(session_id)
    earlier_queued = 0
    for item in instructions:
        if item.id == instruction.id:
            return earlier_queued + 1
        if item.state is InstructionState.QUEUED:
            earlier_queued += 1
    return 0


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
    "DeliveredInstructionV1",
    "EnqueuedInstructionV1",
    "HostSessionReleaseService",
    "InstructionNoteResolver",
    "NoteInstructionResolution",
    "NoteInstructionUnavailableError",
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
