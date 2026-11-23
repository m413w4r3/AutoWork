"""PostgreSQL oracles for F2-D: interrupt/close lock behaviour under real locks.

The F2-C audit left four demonstrable holes outside its scope.  These tests use
the real PostgreSQL repositories, row and advisory locks, the real task engine
and the real broker admission path (provider dispatcher stubbed).  Interleaves
are driven by events, never by sleeps that decide an outcome.

Verified here, with a real database:

* ``interrupt`` commits its intent and then calls the host with **no** session
  row lock held, so a real task cancel can commit while the host is still
  answering; before the fix the cancel blocked on the interrupt's own UPDATE.
* a failed/timed-out host call leaves the committed ``interrupt_requested`` row
  (reconcilable), never a rolled-back ``running`` row that presumes the stop
  did not happen.
* ``close`` runs under the advisory-then-task-row order and treats the task's
  ``lease_session_id`` as the authority, so a closed session is never bound to
  a current lease, and an in-flight start lease transaction blocks it.
* ``interrupt`` cannot change the session while a ``note.capture`` holds the
  task row (same serialization the F2-C broker path relies on).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa

from cti_app.application.agent_session_verification import HmacSessionSecretDeriver
from cti_app.application.agentic_sessions import SessionService, SessionStateConflictError
from cti_app.application.workspace_agent import WorkspaceAgentError, WorkspaceLocalUnavailableError
from cti_app.domain.agentic_operator import (
    AgentSessionState,
    InterruptSessionRequest,
    InterruptSessionResultV1,
)
from cti_app.domain.investigation_task import TaskStatus
from cti_app.infrastructure.database.session import create_postgres_engine
from tests.integration.test_agentic_tool_broker_postgres import SESSION_KEY, _seed
from tests.integration.test_lease_revalidation_postgres import (
    HUMAN,
    _BlockingNoteService,
    _broker_with_authority,
    _cancel,
    _verified,
)

pytestmark = pytest.mark.integration

NOW = datetime(2026, 11, 23, 12, 0, tzinfo=UTC)


class _GatedInterruptOperator:
    """The only operator surface an ``interrupt`` needs; event-gated."""

    def __init__(self, *, failure: Exception | None = None) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.failure = failure
        self.requests: list[InterruptSessionRequest] = []

    async def interrupt_session(self, request: InterruptSessionRequest) -> InterruptSessionResultV1:
        self.requests.append(request)
        self.entered.set()
        await self.release.wait()
        if self.failure is not None:
            raise self.failure
        return InterruptSessionResultV1(
            session_id=request.session_id, stopped=True, observed_at=NOW
        )


class _UnusedOperator:
    """``close`` never calls the adapter; the host teardown runs after commit."""


async def _allow(_investigation_id: UUID) -> None:
    return None


def _service(uow_factory: Any, operator: Any) -> SessionService:
    return SessionService(
        uow_factory=uow_factory,
        deriver=HmacSessionSecretDeriver(SESSION_KEY),
        operator=operator,
        disclosure=_allow,
        cli_network_mode="isolated_egress",
        clock=lambda: datetime.now(UTC),
    )


async def _reset_to_assigned_ready(database_url: str, *, task_id: UUID, session_id: UUID) -> None:
    """A task with no lease and a ready session: the start/close race fixture."""
    engine = create_postgres_engine(database_url)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                sa.text(
                    "UPDATE agent_sessions SET state = 'ready', "
                    "state_version = state_version + 1, task_lease_id = NULL, "
                    "task_lease_fence = NULL, close_reason = NULL, "
                    "closed_at = NULL, updated_at = :now WHERE id = :session_id"
                ),
                {"now": datetime.now(UTC), "session_id": session_id},
            )
            await connection.execute(
                sa.text(
                    "UPDATE investigation_tasks SET status = 'assigned', "
                    "status_version = 2, lease_id = NULL, lease_session_id = NULL, "
                    "lease_expires_at = NULL, heartbeat_at = NULL, updated_at = :now "
                    "WHERE id = :task_id"
                ),
                {"now": datetime.now(UTC), "task_id": task_id},
            )
    finally:
        await engine.dispose()


async def test_interrupt_releases_the_session_row_while_the_host_answers(
    uow_factory: Any, migrated_postgres_url: str
) -> None:
    investigation_id, task_id, session_id, _secret = await _seed(
        uow_factory.session_factory, migrated_postgres_url
    )
    operator = _GatedInterruptOperator()
    service = _service(uow_factory, operator)

    interrupting = asyncio.create_task(
        service.interrupt(investigation_id, session_id, reason="human stop", actor=HUMAN)
    )
    await asyncio.wait_for(operator.entered.wait(), timeout=15)

    # The host is still answering.  A real cancel closes the session row; it
    # must not wait for the interrupt transaction (F2-D 1).
    cancelling = asyncio.create_task(
        _cancel(
            uow_factory,
            task_id=task_id,
            expected_status_version=3,
            key="pg-cancel-during-interrupt",
        )
    )
    try:
        await asyncio.wait_for(asyncio.shield(cancelling), timeout=15)
    finally:
        operator.release.set()
        await asyncio.wait_for(interrupting, timeout=15)
        if not cancelling.done():
            await asyncio.wait_for(cancelling, timeout=15)

    result = await interrupting
    # The cancel won the session row; the late host answer never overwrites it.
    assert result.state is AgentSessionState.CLOSED

    async with uow_factory() as uow:
        task = await uow.investigation_tasks.get(task_id)
        session = await uow.agent_sessions.get(session_id)
    assert task is not None
    assert task.status is TaskStatus.CANCELLED
    assert task.lease_id is None
    assert session is not None and session.state is AgentSessionState.CLOSED


async def test_host_failure_leaves_a_durable_reconcilable_interrupt_intent(
    uow_factory: Any, migrated_postgres_url: str
) -> None:
    investigation_id, _task_id, session_id, _secret = await _seed(
        uow_factory.session_factory, migrated_postgres_url
    )
    operator = _GatedInterruptOperator(
        failure=WorkspaceLocalUnavailableError("the host timed out mid-interrupt")
    )
    operator.release.set()
    service = _service(uow_factory, operator)

    with pytest.raises(WorkspaceAgentError):
        await service.interrupt(investigation_id, session_id, reason="stop", actor=HUMAN)

    async with uow_factory() as uow:
        session = await uow.agent_sessions.get(session_id)
    assert session is not None
    # Committed before the host call: a timeout is ambiguous, never "not run".
    assert session.state is AgentSessionState.INTERRUPT_REQUESTED
    assert session.is_active
    assert session.reconciliation_outcome() is None


async def test_close_refuses_a_session_the_current_task_lease_owns(
    uow_factory: Any, migrated_postgres_url: str
) -> None:
    investigation_id, _task_id, session_id, _secret = await _seed(
        uow_factory.session_factory, migrated_postgres_url
    )
    # A direct task-command start can lease this session without writing the
    # session's own lease mirror; the task row is the authority.
    engine = create_postgres_engine(migrated_postgres_url)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                sa.text(
                    "UPDATE agent_sessions SET task_lease_id = NULL, "
                    "task_lease_fence = NULL WHERE id = :session_id"
                ),
                {"session_id": session_id},
            )
    finally:
        await engine.dispose()

    service = _service(uow_factory, _UnusedOperator())
    with pytest.raises(SessionStateConflictError):
        await service.close(investigation_id, session_id, reason="human close", actor=HUMAN)

    async with uow_factory() as uow:
        session = await uow.agent_sessions.get(session_id)
    assert session is not None and session.state is AgentSessionState.RUNNING


async def test_close_waits_for_an_in_flight_start_then_refuses_the_lease(
    uow_factory: Any, migrated_postgres_url: str
) -> None:
    investigation_id, task_id, session_id, _secret = await _seed(
        uow_factory.session_factory, migrated_postgres_url
    )
    await _reset_to_assigned_ready(migrated_postgres_url, task_id=task_id, session_id=session_id)
    holding = asyncio.Event()
    release = asyncio.Event()

    async def hold_start_lease() -> None:
        async with uow_factory() as uow:
            await uow.investigation_tasks.lock_investigation(investigation_id)
            task = await uow.investigation_tasks.lock_task(task_id)
            assert task is not None
            leased = await uow.investigation_tasks.acquire_lease(
                task_id,
                expected_status_version=task.status_version,
                lease_id=uuid4(),
                lease_session_id=session_id,
                now=datetime.now(UTC),
                ttl_seconds=180,
            )
            assert leased is not None
            holding.set()
            await release.wait()
            await uow.commit()

    starter = asyncio.create_task(hold_start_lease())
    await asyncio.wait_for(holding.wait(), timeout=15)
    service = _service(uow_factory, _UnusedOperator())
    closing = asyncio.create_task(
        service.close(investigation_id, session_id, reason="human close", actor=HUMAN)
    )
    try:
        # The start transaction owns the advisory lock and the task row; close
        # must not decide against a pre-commit task read (F2-D 2).
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.shield(closing), timeout=0.5)
    finally:
        release.set()
        await asyncio.wait_for(starter, timeout=15)

    with pytest.raises(SessionStateConflictError):
        await asyncio.wait_for(closing, timeout=15)

    async with uow_factory() as uow:
        session = await uow.agent_sessions.get(session_id)
        task = await uow.investigation_tasks.get(task_id)
    assert session is not None and session.state is AgentSessionState.READY
    assert task is not None and task.lease_session_id == session_id


async def test_interrupt_waits_for_an_in_flight_note_capture(
    uow_factory: Any, migrated_postgres_url: str
) -> None:
    investigation_id, task_id, session_id, _secret = await _seed(
        uow_factory.session_factory, migrated_postgres_url
    )
    notes = _BlockingNoteService()
    async with uow_factory() as uow:
        broker = await _broker_with_authority(uow, investigation_id, notes=notes)
        capturing = asyncio.create_task(
            broker.invoke(
                session=_verified(investigation_id, task_id, session_id),
                tool_name="note.capture",
                schema_version="1",
                arguments={"target": "note", "relpath": "analyse.md", "text": "# Note\n"},
                client_idempotency_key="pg-f2d-note-under-lock",
            )
        )
        await asyncio.wait_for(notes.capturing.wait(), timeout=15)

        operator = _GatedInterruptOperator()
        operator.release.set()
        service = _service(uow_factory, operator)
        interrupting = asyncio.create_task(
            service.interrupt(investigation_id, session_id, reason="stop", actor=HUMAN)
        )
        # The note capture holds the task row across its host write; the
        # interrupt's state change must wait for it (F2-D 4).
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.shield(interrupting), timeout=0.5)
        notes.release.set()
        await asyncio.wait_for(capturing, timeout=15)
    # The route UoW released its advisory and task row locks on exit; only
    # then may the interrupt's write phase proceed.
    result = await asyncio.wait_for(interrupting, timeout=15)

    assert result.state is AgentSessionState.STOPPED
    assert len(operator.requests) == 1
