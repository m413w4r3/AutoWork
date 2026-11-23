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
import json
import logging
import secrets
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import TracebackType
from typing import TYPE_CHECKING, Any, Protocol, Self
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
from cti_app.application.investigation_task_events import (
    TaskEventConflictError,
    TaskEventDraft,
    TaskEventType,
    TaskNotFoundError,
    append_task_event,
)
from cti_app.application.investigation_tasks import (
    SYSTEM_TASK_ACTOR,
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
    InstructionState,
    InterruptSessionRequest,
    OperatorAdapterKind,
    OperatorCapabilitiesV1,
    OperatorResultsV1,
    PrepareSessionRequest,
    ProbeCapabilitiesRequest,
    ReconcileSessionRequest,
    ResumeSessionRequest,
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

if TYPE_CHECKING:
    from cti_app.application.instruction_delivery import InstructionDeliveryService

logger = logging.getLogger(__name__)


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


class SessionInstructionNotDeliveredError(SessionInstructionConflictError):
    """Ack of an instruction no host effect ever delivered (F3-D §3.6)."""

    code = "instruction_not_delivered"
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


# F3-D §3.6: one signed ack and one pull response are both bounded.
MAX_ACK_INSTRUCTION_IDS = 32
MAX_PULL_INSTRUCTIONS = 32
MAX_CANCEL_REASON_CHARS = 4096


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
    """One delivered instruction plus the exact text the operator received.

    ``delivery_text`` is rebuilt from the persisted ``body`` and
    ``delivery_note_diffs``; ``note_refs`` are the exact ``(path, revision_id,
    sha256)`` refs recorded in that body.  ``delivery_text_verified`` reports
    whether the rebuilt text matches the persisted ``delivery_sha256``; a row
    promoted by a signed ack may carry a digest but no diff evidence, so its
    rebuilt text stays visibly unverified instead of being invented.
    """

    instruction: AgentSessionInstructionV1
    delivery_text: str = field(repr=False)
    note_diffs: tuple[Mapping[str, Any], ...] = ()
    note_refs: tuple[Mapping[str, str], ...] = ()
    delivery_text_verified: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.instruction, AgentSessionInstructionV1):
            raise TypeError("instruction must use AgentSessionInstructionV1")
        if not isinstance(self.delivery_text, str):
            raise TypeError("delivery_text must be a string")
        for name in ("note_diffs", "note_refs"):
            items = getattr(self, name)
            if not isinstance(items, tuple) or not all(isinstance(item, Mapping) for item in items):
                raise TypeError(f"{name} must be a tuple of mappings")
        if not isinstance(self.delivery_text_verified, bool):
            raise TypeError("delivery_text_verified must be a boolean")

    def to_payload(self) -> dict[str, Any]:
        payload = self.instruction.to_payload()
        payload["delivery_text"] = self.delivery_text
        payload["note_diffs"] = [dict(item) for item in self.note_diffs]
        payload["note_refs"] = [dict(item) for item in self.note_refs]
        payload["delivery_text_verified"] = self.delivery_text_verified
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


def host_release_event_key(session_id: UUID) -> str:
    """Durable confirmation key: a release without this event is still pending."""
    return f"host-release:{session_id}"


SessionReleaseFlusher = Callable[[Sequence[TaskSessionRelease]], Awaitable[None]]


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
            return
        await self._mark_released(session)

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

    async def _mark_released(self, session: AgentSessionV1) -> None:
        """Record a durable host-teardown confirmation on the task journal.

        The event is the marker the crash recovery reads: a terminal session
        whose host reference carries no such event still has a pending host
        release.  The payload is stable so a concurrent or retried release
        replays the same event instead of conflicting.
        """
        now = self._clock()
        async with self._uow_factory() as uow:
            task = await uow.investigation_tasks.get(session.task_id)
            if task is None:
                # Nothing canonical to journal; the session row stays terminal.
                return
            try:
                await append_task_event(
                    uow.investigation_tasks,
                    TaskEventDraft(
                        investigation_id=session.investigation_id,
                        task_id=session.task_id,
                        event_type=TaskEventType.SESSION_STATE_CHANGED,
                        actor=SYSTEM_TASK_ACTOR,
                        idempotency_key=host_release_event_key(session.id),
                        payload={
                            "session_id": str(session.id),
                            "action": "host_session_released",
                        },
                    ),
                    occurred_at=now,
                )
            except (TaskEventConflictError, TaskNotFoundError):
                # Another release path confirmed this session concurrently, or
                # the task row vanished with no journal left to write.
                return
            await uow.commit()

    async def _mark_unconfirmed(self, session: AgentSessionV1, detail: str) -> None:
        """Persist a ``lost_unreconciled`` marker without overwriting a real one."""
        now = self._clock()
        async with self._uow_factory() as uow:
            current = await uow.agent_sessions.get(session.id)
            if current is None:
                return
            await mark_host_release_unconfirmed(uow, current, detail, now=now)
            await uow.commit()


class AgentSessionMutationUnitOfWork(Protocol):
    """The one store a transport marker needs; any wider UoW satisfies it."""

    agent_sessions: AgentSessionRepository


async def mark_host_release_unconfirmed(
    uow: AgentSessionMutationUnitOfWork,
    session: AgentSessionV1,
    detail: str,
    *,
    now: datetime,
) -> AgentSessionV1 | None:
    """Mark a terminal session's unconfirmed host effect; never overwrite.

    ``None`` means a live lease path owns the row or a stronger typed D7
    outcome already exists; the caller still owns its commit.
    """
    if session.reconciliation is not None or session.is_active:
        # A still-active row belongs to a live lease path; a terminal or
        # stopped row may carry the transport marker.  An existing typed D7
        # outcome is never overwritten.
        return None
    payload = {
        "outcome": SessionReconcileOutcome.LOST_UNRECONCILED.value,
        "detail": f"host release unconfirmed: {detail}",
        "observed_at": now.isoformat(),
        "session_id": str(session.id),
    }
    digest = hashlib.sha256(canonical_json_bytes(payload)).hexdigest()
    marked = session.with_reconciliation(payload, digest, now=now)
    stored = await uow.agent_sessions.update_state(
        marked, expected_state_version=session.state_version
    )
    return stored


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
        release_host: SessionReleaseFlusher | None = None,
        instruction_delivery: InstructionDeliveryService | None = None,
        cli_network_mode: str = "isolated_egress",
        task_limits: TaskLimits = TASK_LIMIT_DEFAULTS,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if cli_network_mode != "isolated_egress":
            raise ValueError("cli_network_mode must be isolated_egress")
        self._uow_factory = uow_factory
        self._deriver = deriver
        self._operator = operator
        self._secret_delivery = secret_delivery
        self._note_resolver = note_resolver
        self._disclosure = disclosure
        self._release_host = release_host
        self._instruction_delivery = instruction_delivery
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
        try:
            session = await self._transition_prepared(prepared.session, timestamp)
        except Exception:
            # A crash inside this phase must not leave an active row that no
            # later open can replace: the durable ``failed`` row is the marker.
            await self._compensate_failed_activation(
                operator, prepared.session, "transition_ready_failed"
            )
            raise
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
        try:
            ready = await self._transition_prepared(prepared.session, timestamp)
        except Exception:
            await self._compensate_failed_activation(
                operator, prepared.session, "transition_ready_failed"
            )
            raise
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
        refs.  The ``queued`` row is committed before any host effect (D1); an
        idle (``awaiting_instruction``) session then gets one best-effort inline
        drain, while a busy one keeps the instruction queued for the background
        drain.  A note modified after queueing is reported as a visible diff
        appended to the delivered text (INS-05).
        """
        key = _idempotency_key(idempotency_key)
        timestamp = _now(self._clock, now)
        self._require_human(actor)
        # R03b M1: the disclosure gate binds every delivery to the external
        # provider, not only the session opening.  This instruction may be
        # delivered to an idle CLI, and a revoke/TLP change after the opening
        # must stop it (B2).  Queueing alone has no host effect, but the gate
        # runs first so the decision cannot drift before delivery.
        await self._require_external_disclosure(investigation_id)
        composed_text = text.strip()
        if note_refs:
            resolver = self._require_note_resolver()
            prepared = await resolver.prepare(
                investigation_id=investigation_id, text=text, note_refs=note_refs
            )
            composed_text = prepared.queued_text
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
            if created:
                # D1: the queued row is durable before any host call.
                await uow.commit()
            idle = session.state is AgentSessionState.AWAITING_INSTRUCTION
        # D3: the inline drain is best effort and runs after the commit, with
        # no transaction or lock held.  It re-checks every mutable fact itself,
        # so a revoke or a state change since the gate above still refuses.
        if (
            idle
            and self._instruction_delivery is not None
            and stored.state is InstructionState.QUEUED
        ):
            try:
                await self._instruction_delivery.deliver_instruction(stored.id)
            except Exception as exc:  # never fail an already-committed enqueue
                logger.warning(
                    "inline instruction delivery failed",
                    extra={
                        "event": "m24.instruction_delivery.inline_failed",
                        "instruction_id": str(stored.id),
                        "error_type": type(exc).__name__,
                    },
                )
        async with self._uow_factory() as uow:
            final = await uow.agent_sessions.get_instruction(stored.id) or stored
            queue_position = await _queue_position(uow, session.id, final)
        return EnqueuedInstructionV1(
            instruction=final,
            queue_position=queue_position,
            delivered_live=final.state
            in (InstructionState.DELIVERED, InstructionState.ACKNOWLEDGED),
            note_diffs=tuple(final.delivery_note_diffs or ()),
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
        """Operator-signed acknowledgement of instructions it has consumed.

        Only a proven delivery is acknowledged (F3-D §3.6).  ``delivering`` and
        ``delivery_unknown`` are first promoted to ``delivered`` because the
        signed acknowledgement itself proves the operator received the text;
        the promotion is journalled and never invents a mode or a diff.  A
        ``queued`` instruction was never handed to a host: acknowledging it
        would forge the delivery proof, so it is refused with
        ``instruction_not_delivered``.  Replaying an already acknowledged id
        returns the same row.
        """
        timestamp = _now(self._clock, now)
        self._require_operator_actor(actor)
        ids = _bounded_instruction_ids(instruction_ids)
        async with self._uow_factory() as uow:
            await uow.investigation_tasks.lock_investigation(investigation_id)
            session = await self._session_for(uow, investigation_id, session_id)
            task = await uow.investigation_tasks.lock_task(session.task_id)
            self._require_instruction_authority(
                session, task, await uow.investigation_tasks.database_now()
            )
            acknowledged: list[AgentSessionInstructionV1] = []
            for instruction_id in ids:
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
                if instruction.state is InstructionState.QUEUED:
                    raise SessionInstructionNotDeliveredError(
                        f"instruction {instruction.id} was never delivered to the session"
                    )
                current = instruction
                if current.state in (
                    InstructionState.DELIVERING,
                    InstructionState.DELIVERY_UNKNOWN,
                ):
                    current = await self._cas_instruction(
                        uow,
                        current.with_state(InstructionState.DELIVERED, now=timestamp),
                        expected=current,
                    )
                    logger.info(
                        "instruction acknowledged as delivered",
                        extra={
                            "event": "m24.instruction_delivery.ack_promoted",
                            "instruction_id": str(current.id),
                            "from_state": instruction.state.value,
                        },
                    )
                acknowledged.append(
                    await self._cas_instruction(
                        uow,
                        current.with_state(InstructionState.ACKNOWLEDGED, now=timestamp),
                        expected=current,
                    )
                )
            # F2-C: the authority that allows this write is re-read under the
            # investigation advisory lock and the task row lock in the same
            # transaction that persists the acknowledgements.
            self._require_instruction_authority(
                (await uow.agent_sessions.get(session.id)) or session,
                await uow.investigation_tasks.lock_task(session.task_id),
                await uow.investigation_tasks.database_now(),
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
        """Acknowledge and report the exact text the delivery handed over (D7).

        The view is rebuilt from the persisted ``body`` and the delivery-time
        note diffs, never from a fresh note read: a note changed after the
        delivery must not rewrite what the operator actually consumed.
        """
        acknowledged = await self.acknowledge_instructions(
            investigation_id,
            session_id,
            instruction_ids=instruction_ids,
            actor=actor,
            now=now,
        )
        return tuple(_delivered_view(instruction) for instruction in acknowledged)

    async def pull_delivered_instructions(
        self,
        investigation_id: UUID,
        session_id: UUID,
        *,
        actor: TaskActor,
        limit: int = MAX_PULL_INSTRUCTIONS,
    ) -> tuple[DeliveredInstructionV1, ...]:
        """The session's delivered-but-unacknowledged instructions (F3-D §3.6).

        The operator pulls exactly what a host delivery handed to it, in queue
        order and bounded by ``limit``; ``acknowledged`` rows are history and
        stay out of the pull.
        """
        self._require_operator_actor(actor)
        if isinstance(limit, bool) or not isinstance(limit, int):
            raise TypeError("limit must be an integer")
        if limit < 1:
            raise ValueError("limit must be >= 1")
        capped = min(limit, MAX_PULL_INSTRUCTIONS)
        async with self._uow_factory() as uow:
            session = await self._session_for(uow, investigation_id, session_id)
            instructions = await uow.agent_sessions.list_instructions(session.id)
        pending: list[DeliveredInstructionV1] = []
        for instruction in instructions:
            if instruction.state is not InstructionState.DELIVERED:
                continue
            pending.append(_delivered_view(instruction))
            if len(pending) >= capped:
                break
        return tuple(pending)

    async def cancel_instruction(
        self,
        investigation_id: UUID,
        session_id: UUID,
        *,
        instruction_id: UUID,
        reason: str,
        actor: TaskActor,
        now: datetime | None = None,
    ) -> AgentSessionInstructionV1:
        """Human-only cancellation of an instruction no host effect delivered.

        ``queued`` and ``delivery_unknown`` are cancellable; a ``delivering``
        attempt must resolve first (§3.5) and a proven delivery is history.
        The human authority is the verified human identity, not the task
        lease: a lost session's queue stays cleanable with a bounded visible
        reason (G7).
        """
        timestamp = _now(self._clock, now)
        self._require_human(actor)
        if not isinstance(instruction_id, UUID):
            raise TypeError("instruction_id must be a UUID")
        bounded_reason = _bounded_cancel_reason(reason)
        async with self._uow_factory() as uow:
            await uow.investigation_tasks.lock_investigation(investigation_id)
            session = await self._session_for(uow, investigation_id, session_id)
            task = await uow.investigation_tasks.get(session.task_id)
            if task is None or task.investigation_id != investigation_id:
                raise SessionStateConflictError("the session task no longer exists")
            instruction = await uow.agent_sessions.get_instruction(instruction_id)
            if (
                instruction is None
                or instruction.session_id != session.id
                or instruction.task_id != session.task_id
            ):
                raise SessionInstructionConflictError(
                    "the instruction does not belong to this session"
                )
            if instruction.state is InstructionState.CANCELLED:
                return instruction
            if instruction.state is InstructionState.DELIVERING:
                raise SessionInstructionConflictError(
                    "a delivery attempt is in flight; wait for it to resolve",
                    details={"reason": "delivery_in_flight"},
                )
            if instruction.state in (
                InstructionState.DELIVERED,
                InstructionState.ACKNOWLEDGED,
            ):
                raise SessionInstructionConflictError(
                    "a delivered instruction is history and cannot be cancelled",
                    details={"reason": "instruction_delivered"},
                )
            try:
                cancelled = await uow.agent_sessions.cancel_instruction(
                    instruction.id,
                    expected_state=instruction.state,
                    expected_delivery_token=instruction.delivery_token,
                    reason=bounded_reason,
                    now=timestamp,
                )
            except VersionConflictError as exc:
                raise SessionInstructionConflictError(
                    "the instruction changed concurrently"
                ) from exc
            # Re-read in the transaction that persists the cancellation: a
            # concurrent reassignment must not rebind this instruction.
            refreshed = await uow.agent_sessions.get_instruction(instruction.id)
            if (
                refreshed is None
                or refreshed.session_id != session.id
                or refreshed.task_id != session.task_id
            ):
                raise SessionInstructionConflictError("the instruction changed concurrently")
            await uow.commit()
            return cancelled

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
        # Pre-lease close: the DB row is terminal, so the host object (if any)
        # must be stopped or its unconfirmed effect durably marked (B2).
        await self._flush_releases(
            [
                TaskSessionRelease(
                    session_id=closed.id,
                    investigation_id=closed.investigation_id,
                    task_id=closed.task_id,
                    outcome=(
                        TaskSessionOutcome.LOST
                        if closed.state is AgentSessionState.FAILED
                        else TaskSessionOutcome.CLOSED
                    ),
                    reason="session_closed",
                )
            ]
            if closed.host_session_ref is not None
            else []
        )
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
        host_session_ref: str | None = None
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
            host_session_ref = host.host_session_ref
            recorded = await self._record_host_ref(session, host_session_ref)
            prepared = PreparedAgentSession(session=recorded, secret=prepared.secret)
            await self._deliver_secret(prepared, key)
            return prepared
        except Exception:
            await self._fail_session(session.id, "prepare_failed_before_start")
            if host_session_ref is not None:
                # The prepared host object exists even though the durable row
                # could not record or deliver: stop it best effort.
                await self._interrupt_host_quietly(
                    operator, session.id, host_session_ref, "prepare_failed_before_start"
                )
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
        """Start the host run with no lock held; a dead or failed run is durably failed.

        An adapter exception is the same outcome as ``alive=False``: a CLI may
        already be running while the backend knows nothing, so the durable
        ``failed`` row plus a best-effort interrupt is the only safe marker.
        """
        if session.host_session_ref is None:
            raise SessionOperatorContractError("the prepared session has no host reference")
        try:
            status = await operator.start_session(
                StartSessionRequest(
                    session_id=session.id,
                    adapter_kind=session.adapter_kind,
                    host_session_ref=session.host_session_ref,
                    capability_sha256=session.capability_sha256,
                )
            )
        except Exception:
            await self._compensate_failed_activation(operator, session, "start_session_failed")
            raise
        if status.session_id != session.id:
            await self._compensate_failed_activation(
                operator, session, "start_session_contract_invalid"
            )
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
            running = await self._start_engine_locked(uow, session, task, timestamp)
            await uow.commit()
            return running

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
        await self._interrupt_host_quietly(operator, session.id, session.host_session_ref, reason)

    @staticmethod
    async def _interrupt_host_quietly(
        operator: AgenticOperatorPort, session_id: UUID, host_session_ref: str, reason: str
    ) -> None:
        """Best-effort stop of a host object this process may not even know (M2)."""
        try:
            await operator.interrupt_session(
                InterruptSessionRequest(
                    session_id=session_id,
                    host_session_ref=host_session_ref,
                    reason=reason,
                )
            )
        except WorkspaceAgentError:
            return

    async def _flush_releases(self, releases: Sequence[TaskSessionRelease]) -> None:
        """Request host teardown after the DB commit; never under a lock.

        A service built without a wired host runtime treats the effect as
        unconfirmed and records the ``lost_unreconciled`` marker: the host is
        never silently assumed stopped.
        """
        if not releases:
            return
        flusher = self._release_host
        if flusher is None:
            flusher = HostSessionReleaseService(uow_factory=self._uow_factory, host=None).flush
        await flusher(releases)

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

    async def _cas_instruction(
        self,
        uow: SessionUnitOfWork,
        instruction: AgentSessionInstructionV1,
        *,
        expected: AgentSessionInstructionV1,
    ) -> AgentSessionInstructionV1:
        """CAS one acknowledgement transition on (id, state, attempt token)."""
        try:
            return await uow.agent_sessions.update_instruction_delivery(
                instruction,
                expected_state=expected.state,
                expected_delivery_token=expected.delivery_token,
            )
        except VersionConflictError as exc:
            raise SessionInstructionConflictError("the instruction changed concurrently") from exc

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

        ``isolated_egress`` is the only supported CLI network mode (M24.1
        CFG-1). A host that did not attest it refuses the real session instead
        of silently sharing the host network.
        """
        if capabilities.adapter_kind is not adapter_kind:
            raise SessionOperatorContractError("the capability probe names another adapter")
        confinement = capabilities.confinement
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
        self._require_instruction_authority(session, task, now)

    @staticmethod
    def _require_instruction_authority(
        session: AgentSessionV1 | None,
        task: InvestigationTaskV1 | None,
        now: datetime,
    ) -> None:
        """The operator write authority, re-read under the persisting locks (F2-C)."""
        if session is None or not session.is_active:
            raise SessionStateConflictError("the agent session is not active")
        if task is None or task.investigation_id != session.investigation_id:
            raise SessionStateConflictError("the session task no longer exists")
        if (
            session.task_lease_fence is None
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


def _bounded_instruction_ids(values: Sequence[UUID]) -> tuple[UUID, ...]:
    if not isinstance(values, Sequence):
        raise TypeError("instruction_ids must be a sequence of UUIDs")
    unique: list[UUID] = []
    for value in values:
        if not isinstance(value, UUID):
            raise TypeError("instruction_ids must be UUIDs")
        if value not in unique:
            unique.append(value)
    if not unique:
        raise SessionInstructionConflictError("no instruction id was supplied")
    if len(unique) > MAX_ACK_INSTRUCTION_IDS:
        raise SessionInstructionConflictError(
            f"at most {MAX_ACK_INSTRUCTION_IDS} instructions are acknowledged at once"
        )
    return tuple(unique)


def _bounded_cancel_reason(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SessionError("cancel_reason must be a non-empty string")
    reason = value.strip()
    if len(reason) > MAX_CANCEL_REASON_CHARS:
        raise SessionError(f"cancel_reason is bounded to {MAX_CANCEL_REASON_CHARS} characters")
    return reason


def _delivered_view(instruction: AgentSessionInstructionV1) -> DeliveredInstructionV1:
    """Build the operator view from the persisted delivery evidence (D7)."""
    delivery_text, note_refs = _delivery_text(instruction)
    verified = instruction.delivery_sha256 is None or (
        instruction_text_sha256(delivery_text) == instruction.delivery_sha256
    )
    return DeliveredInstructionV1(
        instruction=instruction,
        delivery_text=delivery_text,
        note_diffs=tuple(instruction.delivery_note_diffs or ()),
        note_refs=note_refs,
        delivery_text_verified=verified,
    )


def _delivery_text(
    instruction: AgentSessionInstructionV1,
) -> tuple[str, tuple[Mapping[str, str], ...]]:
    """Rebuild the exact text persisted as ``body + delivery_note_diffs``.

    The diff block is the one ``WorkspaceNoteCatalog.refresh`` appended at
    delivery time (F3-D §3.6).  The persisted diffs are authoritative: a note
    changed again after the delivery never rewrites what was handed over, and
    ``delivery_sha256`` lets the caller verify the rebuild.
    """
    body = instruction.text
    diffs = instruction.delivery_note_diffs or ()
    if not diffs:
        text = body
    else:
        lines = ["", "[notes modifiées depuis la mise en file]"]
        lines.extend(_note_diff_line(item) for item in diffs)
        text = body + "\n".join(lines)
    return text, _recorded_note_refs(body)


def _note_diff_line(item: Mapping[str, Any]) -> str:
    def _text(value: object) -> str:
        return value if isinstance(value, str) else ""

    current = _text(item.get("current_revision_id"))
    return (
        f"- {_text(item.get('path'))}: {_text(item.get('queued_revision_id'))} -> "
        f"{current or 'révision absente'}"
    )


def _recorded_note_refs(text: str) -> tuple[Mapping[str, str], ...]:
    """The exact ``(path, revision_id, sha256)`` refs recorded in the body."""
    # Local import: investigation_transfers imports SessionService from here.
    from cti_app.application.investigation_transfers import (
        MAX_NOTE_REFS,
        NOTE_BLOCK_BEGIN,
        NOTE_BLOCK_END,
    )

    start = text.find(NOTE_BLOCK_BEGIN)
    end = text.find(NOTE_BLOCK_END, start + len(NOTE_BLOCK_BEGIN))
    if start < 0 or end < 0:
        return ()
    raw = text[start + len(NOTE_BLOCK_BEGIN) : end].strip()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return ()
    notes = payload.get("notes") if isinstance(payload, dict) else None
    if not isinstance(notes, list):
        return ()
    refs: list[Mapping[str, str]] = []
    for item in notes[:MAX_NOTE_REFS]:
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
        refs.append({"path": path, "revision_id": revision_id, "sha256": sha256})
    return tuple(refs)


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
    "MAX_ACK_INSTRUCTION_IDS",
    "MAX_CANCEL_REASON_CHARS",
    "MAX_PULL_INSTRUCTIONS",
    "AgentSessionMutationUnitOfWork",
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
    "SessionInstructionNotDeliveredError",
    "SessionNewHandoffRequiredError",
    "SessionNotFoundError",
    "SessionOperatorContractError",
    "SessionOperatorUnavailableError",
    "SessionReleaseFlusher",
    "SessionResumeUnavailableError",
    "SessionSecret",
    "SessionSecretDeliveryError",
    "SessionSecretDeliveryUnavailableError",
    "SessionService",
    "SessionStartFailedError",
    "SessionStateConflictError",
    "SessionUnitOfWork",
    "host_release_event_key",
    "mark_host_release_unconfirmed",
]
