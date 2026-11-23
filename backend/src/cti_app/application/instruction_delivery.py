"""Backend-owned instruction delivery (M24.1 F3-D, lot ID-2).

The host operator cannot push (WORKSPACE-AGENT §1), so the backend drives the
instruction queue itself.  This module implements the three-phase drain of
[INSTRUCTION-DELIVERY.md](../../../docs/design/investigation-workshop/INSTRUCTION-DELIVERY.md)
§3.3:

* **Phase A** is one short locked transaction (advisory investigation lock, then
  the task row ``FOR UPDATE``, then the session and instruction rows).  It
  re-reads every mutable fact — session/task/lease/fence, the persisted
  ``supports_next_run_instruction`` capability, the disclosure envelope and its
  revocations, the note revisions — and compare-and-swaps ``queued`` to
  ``delivering`` with a fresh attempt token and the exact delivered-text
  SHA-256.  Nothing is delivered from a decision taken before this transaction.
* **Phase B** calls the raw host session client with no lock and no open
  transaction.  The host idempotency key is derived from
  ``(session, idempotency_key, delivery_sha256)`` so a replay is the host's own
  replay, never a second run.
* **Phase C** reloads the attempt by id + token in a second short transaction,
  re-verifies the session/task/lease/fence and the mutable disclosure inputs,
  and records the decisive receipt.  A typed refusal with no effect returns the
  row to ``queued``; an ambiguous transport failure records
  ``delivery_unknown`` and keeps the token.  An ambiguous attempt is never
  re-delivered blindly: it is reconciled against the host event journal, and
  only a proven absence returns it to the queue.

A crash between A and C leaves ``delivering``; the grace window
(``M24_INSTRUCTION_DELIVERY_GRACE_SECONDS``) later converts it to
``delivery_unknown``.  All timestamps come from the injectable clock, and the
service never logs or persists instruction text or a host error message — only
stable reason codes.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from types import TracebackType
from typing import Any, Protocol, Self
from uuid import UUID, uuid4

from cti_app.application.agent_session_disclosure import (
    SessionDisclosureCheckerPort,
    SessionDisclosurePreRead,
)
from cti_app.application.agentic_host_sessions import (
    HOST_MAX_INSTRUCTION_BYTES,
    HOST_MAX_PAGE_SIZE,
    HostInstructionRequestV1,
    HostSessionEventsPageV1,
    HostSessionNotFoundError,
    HostSessionsError,
    HostSessionsPort,
)
from cti_app.application.agentic_sessions import (
    InstructionNoteResolver,
    SessionError,
    SessionExternalDisclosureDeniedError,
)
from cti_app.application.investigation_persistence import (
    AgentSessionRepository,
    InvestigationEnvelopeRepository,
    InvestigationTaskRepository,
    VersionConflictError,
)
from cti_app.application.investigation_transfers import NOTE_BLOCK_BEGIN
from cti_app.application.persistence import (
    AnalystDecisionRepository,
    AnalystInvestigationRepository,
)
from cti_app.application.workspace_agent import (
    WorkspaceAgentError,
    WorkspaceLocalUnavailableError,
    WorkspaceNotImplementedError,
    WorkspaceUnauthorizedError,
    WorkspaceUnconfiguredError,
)
from cti_app.domain.agentic_operator import (
    INSTRUCTION_PENDING_STATES,
    AgentSessionInstructionV1,
    AgentSessionState,
    AgentSessionV1,
    InstructionDeliveryMode,
    InstructionState,
    OperatorCapabilitiesV1,
    instruction_text_sha256,
)
from cti_app.domain.investigation_task import InvestigationTaskV1, TaskStatus

logger = logging.getLogger(__name__)

# The advisory lock a drain tick holds (F3-D §3.3): two backend replicas must
# not scan the same queue concurrently.  The CAS reservation still serialises
# the actual attempt; this lock only avoids duplicated host-status work.
INSTRUCTION_DRAIN_LOCK_KEY = "m24.instruction-drain"
DEFAULT_DELIVERY_GRACE_SECONDS = 120
DEFAULT_DRAIN_BATCH_SIZE = 50
# A bounded event scan: 8 pages of 500 events proves "no delivery event" for
# every realistic session, and a longer journal stays undecidable instead of a
# false absence.
_EVENT_PAGE_LIMIT = 8
_DELIVERY_EVENT_KIND = "instruction_delivered"
_HOST_IDLE_STATE = "awaiting_instruction"
_HOST_BUSY_STATE = "running"
_HOST_LOST_STATES = frozenset({"lost", "closed", "failed", "stopped"})
_MAX_ERROR_CHARS = 512


class InstructionDeliveryError(SessionError):
    """The delivery service cannot act; the queue is untouched."""

    code = "instruction_delivery_error"
    status_code = 502


class InstructionDeliveryUnavailableError(InstructionDeliveryError):
    """No host session client is wired in this process."""

    code = "instruction_delivery_unavailable"
    status_code = 503


class InstructionAttemptOutcome(StrEnum):
    """What one drain attempt did to its instruction."""

    DELIVERED = "delivered"
    DEFERRED = "deferred"
    REFUSED = "refused"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class InstructionDeliveryResult:
    """The instruction after one attempt; ``outcome`` is the attempt's verdict."""

    instruction: AgentSessionInstructionV1 | None
    outcome: InstructionAttemptOutcome
    note_diffs: tuple[Mapping[str, Any], ...] = ()

    def __post_init__(self) -> None:
        if self.instruction is not None and not isinstance(
            self.instruction, AgentSessionInstructionV1
        ):
            raise TypeError("instruction must use AgentSessionInstructionV1")
        if not isinstance(self.outcome, InstructionAttemptOutcome):
            raise TypeError("outcome must use InstructionAttemptOutcome")

    @property
    def delivered(self) -> bool:
        return self.outcome is InstructionAttemptOutcome.DELIVERED

    def to_payload(self) -> dict[str, Any]:
        return {
            "instruction": self.instruction.to_payload() if self.instruction else None,
            "outcome": self.outcome.value,
            "note_diffs": [dict(item) for item in self.note_diffs],
        }


@dataclass(frozen=True, slots=True)
class InstructionDrainReport:
    """One drain tick's bounded tally; carries counts only, never content."""

    scanned: int = 0
    delivered: int = 0
    deferred: int = 0
    refused: int = 0
    unknown: int = 0
    stale_unknown: int = 0
    reconciled_delivered: int = 0
    reconciled_queued: int = 0

    def to_payload(self) -> dict[str, int]:
        return {
            "scanned": self.scanned,
            "delivered": self.delivered,
            "deferred": self.deferred,
            "refused": self.refused,
            "unknown": self.unknown,
            "stale_unknown": self.stale_unknown,
            "reconciled_delivered": self.reconciled_delivered,
            "reconciled_queued": self.reconciled_queued,
        }


class InstructionDeliveryUnitOfWork(Protocol):
    """The canonical stores the drain reads and writes; no host in here."""

    analyst_investigations: AnalystInvestigationRepository
    analyst_decisions: AnalystDecisionRepository
    investigation_envelopes: InvestigationEnvelopeRepository
    investigation_tasks: InvestigationTaskRepository
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
class _Reservation:
    token: UUID
    delivered_text: str = field(repr=False)
    delivery_sha256: str
    note_diffs: tuple[Mapping[str, Any], ...]


class _Deferred(Exception):
    """Nothing can be attempted now; the row stays untouched."""


class _AttemptRefused(Exception):
    """A typed, side-effect-free refusal that leaves the row queued."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class _HostFailureKind(StrEnum):
    REFUSED = "refused"
    UNKNOWN = "unknown"
    SESSION_LOST = "session_lost"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class _HostFailure:
    kind: _HostFailureKind
    reason: str


def instruction_delivery_idempotency_key(
    session_id: UUID, idempotency_key: str, delivery_sha256: str
) -> str:
    """The host wire key of one attempt: same session, key and text replay it."""
    material = f"{session_id}\x00{idempotency_key}\x00{delivery_sha256}".encode()
    return f"m24i-{hashlib.sha256(material).hexdigest()[:48]}"


def _supports_next_run_instruction(session: AgentSessionV1) -> bool:
    try:
        capabilities = OperatorCapabilitiesV1.model_validate(session.capability_probe)
    except ValueError:
        return False
    return bool(capabilities.supports_next_run_instruction)


def _bounded_reason(value: str) -> str:
    return value if len(value) <= _MAX_ERROR_CHARS else value[:_MAX_ERROR_CHARS]


def _denied_reason(exc: SessionExternalDisclosureDeniedError) -> str:
    reason = exc.details.get("reason") if isinstance(exc.details, Mapping) else None
    return _bounded_reason(
        f"disclosure_denied:{reason}" if isinstance(reason, str) and reason else "disclosure_denied"
    )


def _classify_host_failure(exc: BaseException) -> _HostFailure:
    """Map a host transport failure to its effect on the delivery attempt.

    Only an explicit refusal proves there was no effect; every ambiguous
    failure (timeout, unreadable response, 5xx) keeps the attempt resolvable
    through the event journal instead of a blind re-send.
    """
    if isinstance(exc, HostSessionNotFoundError):
        return _HostFailure(_HostFailureKind.SESSION_LOST, "host_session_lost")
    if isinstance(exc, HostSessionsError):
        code = getattr(exc, "code", type(exc).__name__)
        status = getattr(exc, "http_status", 502)
        if getattr(exc, "code", "") == "invalid_response" or not 400 <= int(status) < 500:
            return _HostFailure(_HostFailureKind.UNKNOWN, _bounded_reason(f"host_unknown:{code}"))
        return _HostFailure(_HostFailureKind.REFUSED, _bounded_reason(f"host_refused:{code}"))
    if isinstance(exc, WorkspaceUnconfiguredError):
        return _HostFailure(_HostFailureKind.UNAVAILABLE, "host_unconfigured")
    if isinstance(exc, (WorkspaceUnauthorizedError, WorkspaceNotImplementedError)):
        code = getattr(exc, "code", type(exc).__name__)
        return _HostFailure(_HostFailureKind.REFUSED, _bounded_reason(f"host_refused:{code}"))
    if isinstance(exc, WorkspaceLocalUnavailableError):
        return _HostFailure(_HostFailureKind.UNKNOWN, "host_transport_error")
    if isinstance(exc, WorkspaceAgentError):
        code = getattr(exc, "code", type(exc).__name__)
        return _HostFailure(_HostFailureKind.UNKNOWN, _bounded_reason(f"host_unknown:{code}"))
    return _HostFailure(
        _HostFailureKind.UNKNOWN, _bounded_reason(f"host_unknown:{type(exc).__name__}")
    )


def _delivery_event_matches(event: object, instruction: AgentSessionInstructionV1) -> bool:
    if getattr(event, "kind", None) != _DELIVERY_EVENT_KIND:
        return False
    payload = getattr(event, "payload", None)
    if not isinstance(payload, Mapping):
        return False
    raw_id = payload.get("instruction_id", payload.get("id"))
    raw_sha = payload.get("sha256", payload.get("instruction_sha256"))
    return str(raw_id) == str(instruction.id) and raw_sha == instruction.delivery_sha256


def _event_delivery_mode(payload: Mapping[str, Any]) -> InstructionDeliveryMode:
    return (
        InstructionDeliveryMode.LIVE
        if payload.get("mode") == InstructionDeliveryMode.LIVE.value
        else InstructionDeliveryMode.NEXT_RUN
    )


class InstructionDeliveryService:
    """The backend-owned drain of the durable instruction queue (F3-D §3.3).

    The service is deliberately host-client-shaped rather than adapter-shaped:
    it talks to the raw ``/v1/sessions/**`` client, so it keeps working after a
    backend restart and never depends on an adapter's in-memory preparation
    registry.  It is safe to run from the lifespan loop and inline from an
    enqueue: the CAS reservation admits exactly one attempt per instruction.
    """

    def __init__(
        self,
        *,
        uow_factory: Callable[[], InstructionDeliveryUnitOfWork],
        host: HostSessionsPort | None,
        notes: InstructionNoteResolver | None = None,
        disclosure: SessionDisclosureCheckerPort | None = None,
        clock: Callable[[], datetime] | None = None,
        grace_seconds: int = DEFAULT_DELIVERY_GRACE_SECONDS,
        batch_size: int = DEFAULT_DRAIN_BATCH_SIZE,
    ) -> None:
        if isinstance(grace_seconds, bool) or not isinstance(grace_seconds, int):
            raise TypeError("grace_seconds must be an integer")
        if grace_seconds < 1:
            raise ValueError("grace_seconds must be >= 1")
        if isinstance(batch_size, bool) or not isinstance(batch_size, int):
            raise TypeError("batch_size must be an integer")
        if batch_size < 1:
            raise ValueError("batch_size must be >= 1")
        self._uow_factory = uow_factory
        self._host = host
        self._notes = notes
        self._disclosure = disclosure
        self._clock = clock or (lambda: datetime.now(UTC))
        self._grace_seconds = grace_seconds
        self._batch_size = batch_size

    # ---------------------------------------------------------------- public

    async def drain_once(
        self, *, limit: int | None = None, now: datetime | None = None
    ) -> InstructionDrainReport:
        """One bounded pass: reconcile first, then deliver one per session."""
        self._require_host()
        timestamp = self._now(now)
        size = self._batch_size if limit is None else max(1, int(limit))
        stale_unknown = await self._reconcile_stale_delivering(timestamp)
        reconciled_delivered, reconciled_queued = await self._reconcile_unknown_attempts(
            timestamp, limit=size
        )
        pending = await self._list_pending(size)
        counts = {
            InstructionAttemptOutcome.DELIVERED: 0,
            InstructionAttemptOutcome.DEFERRED: 0,
            InstructionAttemptOutcome.REFUSED: 0,
            InstructionAttemptOutcome.UNKNOWN: 0,
        }
        handled: set[UUID] = set()
        for instruction in pending:
            # One instruction per session and per pass, in (queued_at, id)
            # order: a later instruction never overtakes an unresolved earlier
            # one (F3-D §3.3).
            if instruction.session_id in handled:
                continue
            handled.add(instruction.session_id)
            if instruction.state is not InstructionState.QUEUED:
                continue
            try:
                result = await self.deliver_instruction(instruction.id, now=timestamp)
            except Exception as exc:  # one broken row never starves the queue
                logger.warning(
                    "instruction delivery attempt failed",
                    extra={
                        "event": "m24.instruction_delivery.attempt_failed",
                        "instruction_id": str(instruction.id),
                        "error_type": type(exc).__name__,
                    },
                )
                counts[InstructionAttemptOutcome.DEFERRED] += 1
                continue
            counts[result.outcome] += 1
        return InstructionDrainReport(
            scanned=len(pending),
            delivered=counts[InstructionAttemptOutcome.DELIVERED],
            deferred=counts[InstructionAttemptOutcome.DEFERRED],
            refused=counts[InstructionAttemptOutcome.REFUSED],
            unknown=counts[InstructionAttemptOutcome.UNKNOWN],
            stale_unknown=stale_unknown,
            reconciled_delivered=reconciled_delivered,
            reconciled_queued=reconciled_queued,
        )

    async def deliver_instruction(
        self, instruction_id: UUID, *, now: datetime | None = None
    ) -> InstructionDeliveryResult:
        """Attempt one queued instruction through phases A, B and C."""
        host = self._require_host()
        timestamp = self._now(now)
        instruction = await self._load_instruction(instruction_id)
        if instruction is None:
            return InstructionDeliveryResult(
                instruction=None, outcome=InstructionAttemptOutcome.DEFERRED
            )
        if instruction.state is not InstructionState.QUEUED:
            return InstructionDeliveryResult(
                instruction=instruction, outcome=InstructionAttemptOutcome.DEFERRED
            )
        session = await self._load_session(instruction.session_id)
        if session is None:
            await self._record_error(instruction, "session_missing", timestamp)
            return await self._result(instruction_id, InstructionAttemptOutcome.REFUSED)
        if (
            not session.is_active
            or session.is_terminal
            or session.state is AgentSessionState.LOST
            or session.host_session_ref is None
        ):
            await self._record_error(instruction, "host_session_lost", timestamp)
            return await self._result(instruction_id, InstructionAttemptOutcome.REFUSED)
        if not _supports_next_run_instruction(session):
            await self._record_error(instruction, "next_run_instruction_unsupported", timestamp)
            return await self._result(instruction_id, InstructionAttemptOutcome.REFUSED)

        # Step 1 (§3.3): the host fact is the receive point, read from the raw
        # client so a backend restart never relies on in-memory adapter facts.
        try:
            status = await host.get_session_status(session.id)
        except HostSessionNotFoundError:
            await self._record_error(instruction, "host_session_lost", timestamp)
            return await self._result(instruction_id, InstructionAttemptOutcome.REFUSED)
        except WorkspaceAgentError:
            return await self._result(instruction_id, InstructionAttemptOutcome.DEFERRED)
        if status.state == _HOST_IDLE_STATE:
            if session.state is AgentSessionState.RUNNING:
                synced = await self._sync_awaiting_instruction(session, timestamp)
                if synced is None:
                    return await self._result(instruction_id, InstructionAttemptOutcome.DEFERRED)
                session = synced
            elif session.state is not AgentSessionState.AWAITING_INSTRUCTION:
                return await self._result(instruction_id, InstructionAttemptOutcome.DEFERRED)
        elif status.state == _HOST_BUSY_STATE:
            return await self._result(instruction_id, InstructionAttemptOutcome.DEFERRED)
        elif status.state in _HOST_LOST_STATES:
            await self._record_error(instruction, "host_session_lost", timestamp)
            return await self._result(instruction_id, InstructionAttemptOutcome.REFUSED)
        else:
            return await self._result(instruction_id, InstructionAttemptOutcome.DEFERRED)

        if self._disclosure is None:
            await self._record_error(instruction, "disclosure_gate_unavailable", timestamp)
            return await self._result(instruction_id, InstructionAttemptOutcome.REFUSED)
        try:
            pre_read = await self._disclosure.pre_read(instruction.investigation_id)
        except SessionExternalDisclosureDeniedError as exc:
            await self._record_error(instruction, _denied_reason(exc), timestamp)
            return await self._result(instruction_id, InstructionAttemptOutcome.REFUSED)

        # Phase A: the short locked transaction that reserves the attempt.
        try:
            reservation = await self._reserve(instruction, pre_read, timestamp)
        except _Deferred:
            return await self._result(instruction_id, InstructionAttemptOutcome.DEFERRED)
        except _AttemptRefused as refusal:
            await self._record_error(instruction, refusal.reason, timestamp)
            return await self._result(instruction_id, InstructionAttemptOutcome.REFUSED)

        # Phase B: the host call runs with no lock and no open transaction.
        try:
            receipt = await host.send_instruction(
                session.id,
                HostInstructionRequestV1(
                    instruction_id=instruction.id,
                    text=reservation.delivered_text,
                    sha256=reservation.delivery_sha256,
                ),
                idempotency_key=instruction_delivery_idempotency_key(
                    session.id, instruction.idempotency_key, reservation.delivery_sha256
                ),
            )
        except Exception as exc:  # boundary: every host failure is classified
            failure = _classify_host_failure(exc)
            if failure.kind is _HostFailureKind.SESSION_LOST:
                await self._finish_requeued(
                    instruction.id, reservation.token, failure.reason, timestamp
                )
                return await self._result(instruction_id, InstructionAttemptOutcome.REFUSED)
            if failure.kind in (_HostFailureKind.REFUSED, _HostFailureKind.UNAVAILABLE):
                await self._finish_requeued(
                    instruction.id, reservation.token, failure.reason, timestamp
                )
                return await self._result(instruction_id, InstructionAttemptOutcome.REFUSED)
            await self._finish_unknown(instruction.id, reservation.token, failure.reason, timestamp)
            return await self._result(instruction_id, InstructionAttemptOutcome.UNKNOWN)
        if not receipt.accepted:
            await self._finish_requeued(
                instruction.id, reservation.token, "host_refused:not_accepted", timestamp
            )
            return await self._result(instruction_id, InstructionAttemptOutcome.REFUSED)

        # Phase C: re-verify and record the decisive receipt.
        mode = (
            InstructionDeliveryMode.LIVE
            if receipt.delivered == InstructionDeliveryMode.LIVE.value
            else InstructionDeliveryMode.NEXT_RUN
        )
        outcome = await self._finish_delivered(
            instruction.id, reservation, pre_read, mode, timestamp
        )
        return await self._result(instruction_id, outcome)

    # ------------------------------------------------------------- phase A

    async def _reserve(
        self,
        instruction: AgentSessionInstructionV1,
        pre_read: SessionDisclosurePreRead,
        now: datetime,
    ) -> _Reservation:
        """Reserve one attempt inside the short locked transaction (§3.3)."""
        try:
            async with self._uow_factory() as uow:
                await uow.investigation_tasks.lock_investigation(instruction.investigation_id)
                task = await uow.investigation_tasks.lock_task(instruction.task_id)
                session = await uow.agent_sessions.get(instruction.session_id)
                current = await uow.agent_sessions.get_instruction(instruction.id)
                if current is None or current.state is not InstructionState.QUEUED:
                    raise _Deferred
                self._require_deliverable(current, session, task, now)
                await self._is_first_pending(uow, current)
                if self._disclosure is None:
                    raise _AttemptRefused("disclosure_gate_unavailable")
                await self._disclosure.require(pre_read, uow)
                delivered_text, note_diffs = await self._delivered_text(current)
                token = uuid4()
                delivery_sha256 = instruction_text_sha256(delivered_text)
                reserved = current.with_delivery_reserved(
                    token=token, delivery_sha256=delivery_sha256, now=now
                )
                await uow.agent_sessions.update_instruction_delivery(
                    reserved,
                    expected_state=InstructionState.QUEUED,
                    expected_delivery_token=None,
                )
                await uow.commit()
                return _Reservation(
                    token=token,
                    delivered_text=delivered_text,
                    delivery_sha256=delivery_sha256,
                    note_diffs=note_diffs,
                )
        except SessionExternalDisclosureDeniedError as exc:
            raise _AttemptRefused(_denied_reason(exc)) from exc
        except SessionError as exc:
            raise _AttemptRefused(_bounded_reason(str(exc.code) or "note_ref_unresolved")) from exc
        except VersionConflictError as exc:
            raise _Deferred from exc

    def _require_deliverable(
        self,
        current: AgentSessionInstructionV1,
        session: AgentSessionV1 | None,
        task: InvestigationTaskV1 | None,
        now: datetime,
    ) -> None:
        """Refuse or defer when the locked facts no longer allow an attempt."""
        if (
            session is None
            or session.investigation_id != current.investigation_id
            or session.task_id != current.task_id
        ):
            raise _AttemptRefused("host_session_lost")
        if (
            not session.is_active
            or session.is_terminal
            or session.state is AgentSessionState.LOST
            or session.host_session_ref is None
        ):
            raise _AttemptRefused("host_session_lost")
        if session.state is not AgentSessionState.AWAITING_INSTRUCTION:
            raise _Deferred
        if not _supports_next_run_instruction(session):
            raise _AttemptRefused("next_run_instruction_unsupported")
        if task is None or task.investigation_id != current.investigation_id:
            raise _AttemptRefused("task_missing")
        if task.status not in (TaskStatus.RUNNING, TaskStatus.AWAITING_INPUT):
            raise _AttemptRefused("task_not_running")
        if (
            session.task_lease_fence is None
            or task.lease_session_id != session.id
            or task.lease_fence != session.task_lease_fence
            or task.lease_expires_at is None
            or task.lease_expires_at <= now
        ):
            raise _AttemptRefused("lease_expired")
        if instruction_text_sha256(current.text) != current.text_sha256:
            raise _AttemptRefused("instruction_text_hash_mismatch")

    async def _is_first_pending(
        self, uow: InstructionDeliveryUnitOfWork, current: AgentSessionInstructionV1
    ) -> None:
        """Refuse to overtake an earlier unresolved instruction of the session."""
        for item in await uow.agent_sessions.list_instructions(current.session_id):
            if item.id == current.id:
                return
            if item.state in INSTRUCTION_PENDING_STATES:
                raise _Deferred

    async def _delivered_text(
        self, current: AgentSessionInstructionV1
    ) -> tuple[str, tuple[Mapping[str, Any], ...]]:
        """Recompute the exact text and note diff at delivery time (§3.6)."""
        if self._notes is None:
            if NOTE_BLOCK_BEGIN in current.text:
                raise _AttemptRefused("note_refs_unavailable")
            delivered_text = current.text
            note_diffs: tuple[Mapping[str, Any], ...] = ()
        else:
            resolution = await self._notes.refresh(
                investigation_id=current.investigation_id, text=current.text
            )
            delivered_text = resolution.delivery_text
            note_diffs = tuple(dict(item) for item in resolution.note_diffs)
        for item in note_diffs:
            if not item.get("current_revision_id"):
                raise _AttemptRefused("note_ref_unresolved")
        if len(delivered_text.encode("utf-8")) > HOST_MAX_INSTRUCTION_BYTES:
            raise _AttemptRefused("instruction_text_exceeds_host_bound")
        return delivered_text, note_diffs

    async def _sync_awaiting_instruction(
        self, session: AgentSessionV1, now: datetime
    ) -> AgentSessionV1 | None:
        """Record the host's receive point on a still-running backend session."""
        async with self._uow_factory() as uow:
            current = await uow.agent_sessions.get(session.id)
            if current is None or current.state is not AgentSessionState.RUNNING:
                return None
            updated = current.with_state(AgentSessionState.AWAITING_INSTRUCTION, now=now)
            stored = await uow.agent_sessions.update_state(
                updated, expected_state_version=current.state_version
            )
            if stored is None:
                return None
            await uow.commit()
            return stored

    # ------------------------------------------------------------- phase C

    async def _finish_delivered(
        self,
        instruction_id: UUID,
        reservation: _Reservation,
        pre_read: SessionDisclosurePreRead,
        mode: InstructionDeliveryMode,
        now: datetime,
    ) -> InstructionAttemptOutcome:
        """Re-verify and record ``delivering -> delivered`` in one transaction."""
        try:
            async with self._uow_factory() as uow:
                current = await uow.agent_sessions.get_instruction_by_token(
                    instruction_id, reservation.token, state=InstructionState.DELIVERING
                )
                if current is None:
                    return InstructionAttemptOutcome.DEFERRED
                await uow.investigation_tasks.lock_investigation(current.investigation_id)
                task = await uow.investigation_tasks.lock_task(current.task_id)
                session = await uow.agent_sessions.get(current.session_id)
                current = await uow.agent_sessions.get_instruction_by_token(
                    instruction_id, reservation.token, state=InstructionState.DELIVERING
                )
                if current is None:
                    raise _Deferred
                failure = await self._confirmation_error(uow, current, session, task, pre_read, now)
                if failure is not None:
                    unknown = current.with_state(
                        InstructionState.DELIVERY_UNKNOWN,
                        now=now,
                        last_delivery_error=failure,
                    )
                    await uow.agent_sessions.update_instruction_delivery(
                        unknown,
                        expected_state=InstructionState.DELIVERING,
                        expected_delivery_token=reservation.token,
                    )
                    await uow.commit()
                    return InstructionAttemptOutcome.UNKNOWN
                delivered = current.with_state(
                    InstructionState.DELIVERED,
                    now=now,
                    delivery_mode=mode,
                    delivery_sha256=reservation.delivery_sha256,
                    delivery_note_diffs=reservation.note_diffs,
                )
                await uow.agent_sessions.update_instruction_delivery(
                    delivered,
                    expected_state=InstructionState.DELIVERING,
                    expected_delivery_token=reservation.token,
                )
                if session is not None and session.state is AgentSessionState.AWAITING_INSTRUCTION:
                    await uow.agent_sessions.update_state(
                        session.with_state(AgentSessionState.RUNNING, now=now),
                        expected_state_version=session.state_version,
                    )
                await uow.commit()
                return InstructionAttemptOutcome.DELIVERED
        except _Deferred:
            return InstructionAttemptOutcome.DEFERRED
        except VersionConflictError:
            # The attempt token is gone: another writer (reconciliation or a
            # concurrent drainer) owns the row.  Never overwrite it.
            return InstructionAttemptOutcome.DEFERRED

    async def _confirmation_error(
        self,
        uow: InstructionDeliveryUnitOfWork,
        current: AgentSessionInstructionV1,
        session: AgentSessionV1 | None,
        task: InvestigationTaskV1 | None,
        pre_read: SessionDisclosurePreRead,
        now: datetime,
    ) -> str | None:
        """Every mutable fact is re-read before ``delivered`` is persisted."""
        if (
            session is None
            or session.investigation_id != current.investigation_id
            or session.task_id != current.task_id
        ):
            return "host_session_lost"
        if (
            not session.is_active
            or session.is_terminal
            or session.state is AgentSessionState.LOST
            or session.host_session_ref is None
        ):
            return "host_session_lost"
        if session.state not in (
            AgentSessionState.AWAITING_INSTRUCTION,
            AgentSessionState.RUNNING,
        ):
            return "session_state_changed"
        if not _supports_next_run_instruction(session):
            return "next_run_instruction_unsupported"
        if task is None or task.investigation_id != current.investigation_id:
            return "task_missing"
        if task.status not in (TaskStatus.RUNNING, TaskStatus.AWAITING_INPUT):
            return "task_not_running"
        if (
            session.task_lease_fence is None
            or task.lease_session_id != session.id
            or task.lease_fence != session.task_lease_fence
            or task.lease_expires_at is None
            or task.lease_expires_at <= now
        ):
            return "lease_expired"
        if self._disclosure is None:
            return "disclosure_gate_unavailable"
        try:
            await self._disclosure.require(pre_read, uow)
        except SessionExternalDisclosureDeniedError as exc:
            return _denied_reason(exc)
        except SessionError as exc:
            return _bounded_reason(str(exc.code) or "disclosure_gate_unavailable")
        return None

    async def _finish_requeued(
        self, instruction_id: UUID, token: UUID, reason: str, now: datetime
    ) -> None:
        """A typed, side-effect-free refusal returns the attempt to the queue."""
        reason = _bounded_reason(reason)
        try:
            async with self._uow_factory() as uow:
                current = await uow.agent_sessions.get_instruction_by_token(
                    instruction_id, token, state=InstructionState.DELIVERING
                )
                if current is None:
                    return
                requeued = current.with_state(
                    InstructionState.QUEUED, now=now, last_delivery_error=reason
                )
                await uow.agent_sessions.update_instruction_delivery(
                    requeued,
                    expected_state=InstructionState.DELIVERING,
                    expected_delivery_token=token,
                )
                await uow.commit()
        except VersionConflictError:
            return

    async def _finish_unknown(
        self, instruction_id: UUID, token: UUID, reason: str, now: datetime
    ) -> None:
        """An ambiguous attempt keeps its token and is reconciled later."""
        reason = _bounded_reason(reason)
        try:
            async with self._uow_factory() as uow:
                current = await uow.agent_sessions.get_instruction_by_token(
                    instruction_id, token, state=InstructionState.DELIVERING
                )
                if current is None:
                    return
                unknown = current.with_state(
                    InstructionState.DELIVERY_UNKNOWN,
                    now=now,
                    last_delivery_error=reason,
                )
                await uow.agent_sessions.update_instruction_delivery(
                    unknown,
                    expected_state=InstructionState.DELIVERING,
                    expected_delivery_token=token,
                )
                await uow.commit()
        except VersionConflictError:
            return

    # ---------------------------------------------------------- reconciliation

    async def _reconcile_stale_delivering(self, now: datetime) -> int:
        """A crash between A and C becomes an ambiguous attempt after the grace."""
        stale = await self._list_pending(self._batch_size)
        converted = 0
        for instruction in stale:
            if instruction.state is not InstructionState.DELIVERING:
                continue
            attempted = instruction.delivery_attempted_at or instruction.updated_at
            if attempted + timedelta(seconds=self._grace_seconds) > now:
                continue
            unknown = instruction.with_state(
                InstructionState.DELIVERY_UNKNOWN,
                now=now,
                last_delivery_error="delivery_timeout",
            )
            try:
                async with self._uow_factory() as uow:
                    await uow.agent_sessions.update_instruction_delivery(
                        unknown,
                        expected_state=InstructionState.DELIVERING,
                        expected_delivery_token=instruction.delivery_token,
                    )
                    await uow.commit()
            except VersionConflictError:
                continue
            converted += 1
        return converted

    async def _reconcile_unknown_attempts(self, now: datetime, *, limit: int) -> tuple[int, int]:
        """Prove an ambiguous attempt delivered, absent, or leave it unknown."""
        host = self._require_host()
        pending = await self._list_pending(limit)
        delivered = 0
        requeued = 0
        for instruction in pending:
            if instruction.state is not InstructionState.DELIVERY_UNKNOWN:
                continue
            since = instruction.delivery_unknown_at or instruction.updated_at
            if since + timedelta(seconds=self._grace_seconds) > now:
                # Give the host time to journal the delivery before deciding.
                continue
            conclusive, proof = await self._scan_delivery_events(instruction)
            if proof is not None:
                marked = instruction.with_state(
                    InstructionState.DELIVERED,
                    now=now,
                    delivery_mode=_event_delivery_mode(proof),
                    delivery_sha256=instruction.delivery_sha256,
                    delivery_note_diffs=None,
                )
                if await self._replace_attempt(
                    instruction, marked, InstructionState.DELIVERY_UNKNOWN
                ):
                    delivered += 1
                continue
            if not conclusive:
                # A resync window or an over-long journal proves nothing.
                continue
            try:
                status = await host.get_session_status(instruction.session_id)
            except WorkspaceAgentError:
                continue
            if status.state != _HOST_IDLE_STATE:
                # A running session cannot prove that our attempt did not
                # start this run; only an idle receive point can.
                continue
            requeued_row = instruction.with_state(
                InstructionState.QUEUED, now=now, last_delivery_error="delivery_absent"
            )
            if await self._replace_attempt(
                instruction, requeued_row, InstructionState.DELIVERY_UNKNOWN
            ):
                requeued += 1
        return delivered, requeued

    async def _replace_attempt(
        self,
        current: AgentSessionInstructionV1,
        updated: AgentSessionInstructionV1,
        expected_state: InstructionState,
    ) -> bool:
        try:
            async with self._uow_factory() as uow:
                await uow.agent_sessions.update_instruction_delivery(
                    updated,
                    expected_state=expected_state,
                    expected_delivery_token=current.delivery_token,
                )
                await uow.commit()
                return True
        except VersionConflictError:
            return False

    async def _scan_delivery_events(
        self, instruction: AgentSessionInstructionV1
    ) -> tuple[bool, Mapping[str, Any] | None]:
        """Bounded event-journal scan: ``(conclusive, matching payload)``."""
        host = self._require_host()
        after = 0
        generation: int | None = None
        for _page in range(_EVENT_PAGE_LIMIT):
            try:
                events: HostSessionEventsPageV1 = await host.get_events(
                    instruction.session_id,
                    after=after,
                    generation=generation,
                    limit=HOST_MAX_PAGE_SIZE,
                )
            except WorkspaceAgentError:
                return False, None
            if events.resync_required:
                return False, None
            generation = events.generation
            for event in events.events:
                if _delivery_event_matches(event, instruction):
                    return True, event.payload
            if events.next_cursor <= after:
                return True, None
            after = events.next_cursor
        return False, None

    # -------------------------------------------------------------- helpers

    def _require_host(self) -> HostSessionsPort:
        if self._host is None:
            raise InstructionDeliveryUnavailableError(
                "no host session client is wired; instruction delivery is disabled"
            )
        return self._host

    def _now(self, value: datetime | None) -> datetime:
        timestamp = self._clock() if value is None else value
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        return timestamp

    async def _list_pending(self, limit: int) -> tuple[AgentSessionInstructionV1, ...]:
        async with self._uow_factory() as uow:
            return await uow.agent_sessions.list_pending_instructions(limit=limit)

    async def _load_instruction(self, instruction_id: UUID) -> AgentSessionInstructionV1 | None:
        async with self._uow_factory() as uow:
            return await uow.agent_sessions.get_instruction(instruction_id)

    async def _load_session(self, session_id: UUID) -> AgentSessionV1 | None:
        async with self._uow_factory() as uow:
            return await uow.agent_sessions.get(session_id)

    async def _record_error(
        self, instruction: AgentSessionInstructionV1, reason: str, now: datetime
    ) -> None:
        """Persist the visible refusal on a still-pending row; never consume it."""
        reason = _bounded_reason(reason)
        if instruction.state not in (
            InstructionState.QUEUED,
            InstructionState.DELIVERY_UNKNOWN,
        ):
            return
        if instruction.last_delivery_error == reason:
            return
        updated = replace(instruction, last_delivery_error=reason, updated_at=now)
        try:
            async with self._uow_factory() as uow:
                await uow.agent_sessions.update_instruction_delivery(
                    updated,
                    expected_state=instruction.state,
                    expected_delivery_token=instruction.delivery_token,
                )
                await uow.commit()
        except VersionConflictError:
            return

    async def _result(
        self, instruction_id: UUID, outcome: InstructionAttemptOutcome
    ) -> InstructionDeliveryResult:
        final = await self._load_instruction(instruction_id)
        note_diffs = tuple(final.delivery_note_diffs or ()) if final is not None else ()
        return InstructionDeliveryResult(instruction=final, outcome=outcome, note_diffs=note_diffs)


__all__ = [
    "DEFAULT_DELIVERY_GRACE_SECONDS",
    "DEFAULT_DRAIN_BATCH_SIZE",
    "INSTRUCTION_DRAIN_LOCK_KEY",
    "InstructionAttemptOutcome",
    "InstructionDeliveryError",
    "InstructionDeliveryResult",
    "InstructionDeliveryService",
    "InstructionDeliveryUnavailableError",
    "InstructionDeliveryUnitOfWork",
    "InstructionDrainReport",
    "instruction_delivery_idempotency_key",
]
