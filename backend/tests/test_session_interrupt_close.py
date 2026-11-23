"""F2-D oracles: interrupt/close lock discipline and restart reconstruction.

The defects are demonstrable at the ID-2 HEAD even though F2-C fixed the
lease-revalidation paths:

* ``SessionService.interrupt`` held the session row lock (from its own UPDATE)
  across the host call, so a cancel/close on the same session blocked while the
  host answered.
* ``SessionService.close`` read the task without its row lock and compared the
  session's lease *mirror*, so a ``closed`` row could remain bound to the
  task's current lease.
* The adapter dispatch map and the probed host facts were process-local, so
  after a backend restart an active session could no longer be interrupted and
  a persisted session could no longer be resumed.
* ``interrupt`` changed the session without the task row lock, so it could
  commit between a ``note.capture`` revalidation and its published revision.

These tests are deterministic: event-gated fakes, no sleeps that decide an
outcome.  PostgreSQL lock behaviour lives in
``tests/integration/test_session_interrupt_close_postgres.py``.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from uuid import UUID, uuid4

import pytest

from cti_app.application.agent_session_verification import session_secret_hash
from cti_app.application.agentic_host_sessions import (
    HostCapabilityProbeV1,
    HostConfinementV1,
    HostSessionContractError,
)
from cti_app.application.agentic_sessions import (
    SessionResumeUnavailableError,
    SessionService,
    SessionStateConflictError,
)
from cti_app.domain.agentic_operator import (
    AgentSessionState,
    AgentSessionV1,
    InterruptSessionRequest,
    InterruptSessionResultV1,
    OperatorAdapterKind,
    OperatorCapabilitiesV1,
    ProbeCapabilitiesRequest,
    SessionStatusRequest,
)
from cti_app.infrastructure.agentic.codex_cli import CodexCliOperatorAdapter
from cti_app.infrastructure.agentic.host_operator import HostSessionPreparationRegistry
from cti_app.infrastructure.agentic.wiring import DispatchingOperatorPort
from tests.agentic_support import FakeHostSessionsPort, probe_wire
from tests.test_agent_sessions import (
    EXECUTABLE,
    HUMAN_ACTOR,
    NOW,
    _FakeOperator,
    _Harness,
    _ready_session,
)

CODEX = OperatorAdapterKind.CODEX_CLI


class _GatedOperator(_FakeOperator):
    """An interrupt call that suspends until the test releases it."""

    def __init__(self) -> None:
        super().__init__()
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.failure: Exception | None = None
        self.stopped = True

    async def interrupt_session(self, request: InterruptSessionRequest) -> InterruptSessionResultV1:
        self._observe("interrupt")
        self.interrupted.append(request)
        self.entered.set()
        await self.release.wait()
        if self.failure is not None:
            raise self.failure
        return InterruptSessionResultV1(
            session_id=request.session_id, stopped=self.stopped, observed_at=NOW
        )


def _harness_with(operator: _FakeOperator) -> _Harness:
    harness = _Harness()
    operator.observer = harness
    harness.service._operator = operator
    return harness


async def test_interrupt_runs_the_host_call_outside_every_transaction() -> None:
    """Phase 2 must hold no transaction and no row lock while the host answers."""
    harness = _harness_with(_GatedOperator())
    operator = harness.service._operator
    assert isinstance(operator, _GatedOperator)
    task = await harness.seed_task()
    opened = await harness.open(task=task)

    interrupting = asyncio.create_task(
        harness.service.interrupt(
            harness.investigation_id, opened.session.id, reason="stop", actor=HUMAN_ACTOR
        )
    )
    await asyncio.wait_for(operator.entered.wait(), timeout=5)

    assert harness.transaction_depth == 0
    assert harness.current_locks == []
    # The durable intent is already committed before the host is asked.
    requested = await harness.sessions.get(opened.session.id)
    assert requested is not None
    assert requested.state is AgentSessionState.INTERRUPT_REQUESTED

    operator.release.set()
    stopped = await asyncio.wait_for(interrupting, timeout=5)
    assert stopped.state is AgentSessionState.STOPPED


async def test_interrupt_transitions_hold_the_canonical_advisory_then_task_lock() -> None:
    """The state changes serialize with note.capture/task commands (F2-D 4)."""
    harness = _Harness()
    task = await harness.seed_task()
    opened = await harness.open(task=task)
    harness.lock_transactions.clear()

    await harness.service.interrupt(
        harness.investigation_id, opened.session.id, reason="stop", actor=HUMAN_ACTOR
    )

    # Phase 1 (intent) and phase 3 (stop) each take advisory then task row.
    assert harness.lock_transactions == [["advisory", "row"], ["advisory", "row"]]


async def test_interrupt_never_overwrites_a_transition_committed_during_the_call() -> None:
    harness = _harness_with(_GatedOperator())
    operator = harness.service._operator
    assert isinstance(operator, _GatedOperator)
    task = await harness.seed_task()
    opened = await harness.open(task=task)

    interrupting = asyncio.create_task(
        harness.service.interrupt(
            harness.investigation_id, opened.session.id, reason="stop", actor=HUMAN_ACTOR
        )
    )
    await asyncio.wait_for(operator.entered.wait(), timeout=5)

    # A cancel/close wins while the host is still answering.
    requested = await harness.sessions.get(opened.session.id)
    assert requested is not None
    closed = replace(
        requested,
        state=AgentSessionState.CLOSED,
        state_version=requested.state_version + 1,
        close_reason="cancelled",
        closed_at=NOW,
    )
    await harness.sessions.update_state(closed, expected_state_version=requested.state_version)

    operator.release.set()
    result = await asyncio.wait_for(interrupting, timeout=5)

    assert result.state is AgentSessionState.CLOSED
    stored = await harness.sessions.get(opened.session.id)
    assert stored is not None and stored.state is AgentSessionState.CLOSED


async def test_close_refuses_a_session_the_current_task_lease_owns_with_a_stale_mirror() -> None:
    """The task row is the lease authority, not the session's lease mirror."""
    harness = _Harness()
    task = await harness.seed_task()
    opened = await harness.open(task=task)
    stored = await harness.sessions.get(opened.session.id)
    assert stored is not None
    # A direct task-command start can lease this session without writing the
    # session mirror; close must still refuse.
    harness.sessions._sessions[opened.session.id] = replace(
        stored, task_lease_id=None, task_lease_fence=None
    )

    with pytest.raises(SessionStateConflictError):
        await harness.service.close(
            harness.investigation_id,
            opened.session.id,
            reason="human close",
            actor=HUMAN_ACTOR,
        )

    unchanged = await harness.sessions.get(opened.session.id)
    assert unchanged is not None and unchanged.state is AgentSessionState.RUNNING


async def test_close_locks_the_task_row_before_closing_an_unleased_session() -> None:
    harness = _Harness()
    task = await harness.seed_task()
    handoff = await harness.add_handoff(task)
    orphan = _ready_session(harness, task, UUID(handoff.handoff_id))
    await harness.sessions.add(orphan)
    harness.lock_transactions.clear()

    closed = await harness.service.close(
        harness.investigation_id, orphan.id, reason="orphaned", actor=HUMAN_ACTOR
    )

    assert closed.state is AgentSessionState.CLOSED
    assert harness.lock_transactions == [["advisory", "row"]]


# ----------------------------------------------------- restart reconstruction


def _probe(*, version: str = "1.2.3") -> HostCapabilityProbeV1:
    base = probe_wire(flags={"resume": True, "structured_output": True})
    return base.model_copy(
        update={
            "version": version,
            "confinement": HostConfinementV1(
                profile="fake-reasoning-session",
                bwrap_ok=True,
                netns=True,
                network_mode="isolated_egress",
                loopback_isolated=True,
            ),
        }
    )


async def _persisted_capabilities(probe: HostCapabilityProbeV1) -> OperatorCapabilitiesV1:
    first = CodexCliOperatorAdapter(
        host=FakeHostSessionsPort(probe=probe),
        preparations=HostSessionPreparationRegistry(),
        clock=lambda: NOW,
    )
    return await first.probe_capabilities(
        ProbeCapabilitiesRequest(adapter_kind=CODEX, executable=EXECUTABLE)
    )


def _stopped_session(
    harness: _Harness, task_id: UUID, capabilities: OperatorCapabilitiesV1
) -> AgentSessionV1:
    session_id = uuid4()
    nonce = b"r" * 32
    assert harness.deriver is not None
    secret = harness.deriver.derive(session_id=session_id, nonce=nonce)
    return AgentSessionV1(
        id=session_id,
        investigation_id=harness.investigation_id,
        task_id=task_id,
        adapter_kind=CODEX,
        state=AgentSessionState.STOPPED,
        state_version=3,
        capability_probe=capabilities.to_payload(),
        capability_sha256=capabilities.capability_sha256,
        probed_at=capabilities.probed_at,
        auth_secret_nonce=nonce,
        auth_secret_hash=session_secret_hash(secret),
        auth_secret_issued_at=NOW,
        opened_at=NOW,
        idempotency_key=f"stopped-{session_id}",
        created_at=NOW,
        updated_at=NOW,
        host_session_ref=str(session_id),
    )


def _restarted_service(harness: _Harness, host: FakeHostSessionsPort) -> SessionService:
    """A fresh backend process: no adapter cache, no probed facts, same DB."""
    adapter = CodexCliOperatorAdapter(
        host=host,
        preparations=HostSessionPreparationRegistry(),
        clock=lambda: NOW,
    )

    async def resolve_adapter_kind(session_id: UUID) -> OperatorAdapterKind | None:
        stored = await harness.sessions.get(session_id)
        return None if stored is None else stored.adapter_kind

    port = DispatchingOperatorPort({CODEX: adapter}, resolve_adapter_kind=resolve_adapter_kind)
    return SessionService(
        uow_factory=lambda: harness.uow,
        deriver=harness.deriver,
        operator=port,
        secret_delivery=None,
        disclosure=harness.disclosure,
        cli_network_mode="isolated_egress",
        clock=lambda: NOW,
    )


async def test_dispatching_port_resolves_the_persisted_adapter_kind_after_restart() -> None:
    calls: list[str] = []

    class _Adapter:
        adapter_kind = CODEX

        async def interrupt_session(
            self, request: InterruptSessionRequest
        ) -> InterruptSessionResultV1:
            calls.append("interrupt")
            return InterruptSessionResultV1(
                session_id=request.session_id, stopped=True, observed_at=NOW
            )

    session_id = uuid4()

    async def resolve(candidate: UUID) -> OperatorAdapterKind | None:
        return CODEX if candidate == session_id else None

    port = DispatchingOperatorPort({CODEX: _Adapter()}, resolve_adapter_kind=resolve)  # type: ignore[dict-item]
    result = await port.interrupt_session(
        InterruptSessionRequest(
            session_id=session_id, host_session_ref=str(session_id), reason="restart"
        )
    )
    assert result.stopped
    assert calls == ["interrupt"]

    unknown = uuid4()
    with pytest.raises(HostSessionContractError):
        await port.get_session_status(
            SessionStatusRequest(session_id=unknown, host_session_ref=str(unknown))
        )


async def test_resume_after_restart_reprobes_the_host_and_verifies_the_recorded_probe() -> None:
    harness = _Harness()
    task = await harness.seed_task()
    capabilities = await _persisted_capabilities(_probe())
    row = _stopped_session(harness, task.id, capabilities)
    await harness.sessions.add(row)

    host = FakeHostSessionsPort(probe=_probe())
    service = _restarted_service(harness, host)

    resumed = await service.resume(harness.investigation_id, row.id, actor=HUMAN_ACTOR)

    assert resumed.state is AgentSessionState.READY
    assert host.names() == ["probe_capabilities", "resume_session"]


async def test_resume_after_restart_refuses_a_changed_host_probe() -> None:
    harness = _Harness()
    task = await harness.seed_task()
    capabilities = await _persisted_capabilities(_probe())
    row = _stopped_session(harness, task.id, capabilities)
    await harness.sessions.add(row)

    host = FakeHostSessionsPort(probe=_probe(version="9.9.9"))
    service = _restarted_service(harness, host)

    with pytest.raises(SessionResumeUnavailableError):
        await service.resume(harness.investigation_id, row.id, actor=HUMAN_ACTOR)

    assert host.names() == ["probe_capabilities"]
    stored = await harness.sessions.get(row.id)
    assert stored is not None and stored.state is AgentSessionState.STOPPED


async def test_interrupt_after_restart_uses_the_persisted_adapter_kind() -> None:
    harness = _Harness()
    task = await harness.seed_task()
    capabilities = await _persisted_capabilities(_probe())
    row = _stopped_session(harness, task.id, capabilities)
    # Interrupt is legal on a running/awaiting session; the persisted row is
    # what a restarted process has to work from.
    running = replace(row, state=AgentSessionState.RUNNING, state_version=4)
    await harness.sessions.add(running)

    host = FakeHostSessionsPort(probe=_probe())
    service = _restarted_service(harness, host)

    stopped = await service.interrupt(
        harness.investigation_id, running.id, reason="restart stop", actor=HUMAN_ACTOR
    )

    assert stopped.state is AgentSessionState.STOPPED
    assert host.names() == ["interrupt_session"]
