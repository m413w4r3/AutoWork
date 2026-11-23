"""F2-C lease revalidation oracles (ADR-0011 D3, M24.1 hardening).

Each operation that persists an effect must revalidate the authority (active
session, bound task, current ``lease_fence``/``lease_session_id`` and a lease
the *database* clock has not expired) inside the transaction that persists the
effect, not only before a possibly long prelude.  These tests interleave a real
task command with a broker call using event-gated fake ports: the gate captures
the authority the cheap pre-check observed, a concurrent transaction revokes
it, and the test asserts no effect survives.

The PostgreSQL counterparts (real row/advisory locks and the production
repositories) live in ``tests/integration/test_lease_revalidation_postgres.py``.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest

from cti_app.application.agentic_note_tools import NoteCaptureResult
from cti_app.application.agentic_tool_broker import (
    AgenticLeaseRefusedError,
    AgenticSessionInactiveError,
    AgenticToolBroker,
)
from cti_app.application.investigation_handoffs import HandoffError
from cti_app.application.investigation_persistence import (
    InMemoryAgentSessionRepository,
    InMemoryInvestigationTaskRepository,
)
from cti_app.application.investigation_tasks import (
    InvestigationTaskService,
    RepositoryTaskSessionPort,
    TaskLeaseExpiredError,
)
from cti_app.domain.agentic_operator import AgentSessionState
from cti_app.domain.investigation import InvestigationExecutionMode
from cti_app.domain.investigation_task import (
    TASK_LIMIT_DEFAULTS,
    BlockedReason,
    ResultVerificationStatus,
    TaskActor,
    TaskActorKind,
    TaskPriority,
    TaskStatus,
)
from cti_app.domain.production import ProseAuthority
from tests.test_agentic_tool_broker import (
    _ARGUMENTS,
    NOW,
    OPERATOR_ID,
    _broker,
    _BrokerUnitOfWork,
    _fixture,
    _invocations,
    _seed_agent_session,
    _verified,
)
from tests.test_investigation_handoffs import (
    INVESTIGATION_ID,
    OPERATOR_ACTOR,
    TASK_ID,
    _Harness,
)
from tests.test_operator_tool_invocation import _service

HUMAN = TaskActor(kind=TaskActorKind.HUMAN, actor_id="analyst:test")
OPERATOR = TaskActor(kind=TaskActorKind.OPERATOR, actor_id=OPERATOR_ID)


class _GatedTaskRepository:
    """Delegating task repository whose first ``get`` yields a stale snapshot.

    The broker's cheap gate reads the task once.  The gate captures that row,
    publishes ``captured``, and waits: the test then commits a concurrent
    revocation through another transaction, so the value the gate returns is
    the authority that existed *before* the revocation.  Every later read
    (the locked revalidation) delegates to the live repository.
    """

    def __init__(self, inner: InMemoryInvestigationTaskRepository, *, task_id: UUID) -> None:
        self._inner = inner
        self._task_id = task_id
        self._armed = True
        self.captured = asyncio.Event()
        self.release = asyncio.Event()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    async def get(self, task_id: UUID) -> Any:
        row = await self._inner.get(task_id)
        if task_id == self._task_id and self._armed:
            self._armed = False
            self.captured.set()
            await self.release.wait()
            return row
        return row


class _RecordingNoteService:
    """A ``note.capture`` stand-in that records every host write."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def capture(self, investigation_id: UUID, **kwargs: Any) -> NoteCaptureResult:
        self.calls.append({"investigation_id": investigation_id, **kwargs})
        return NoteCaptureResult(
            target=str(kwargs["target"]),
            path="notes/agents/synthetic/note.md",
            revision_id="rev-" + "a" * 32,
            sha256="a" * 64,
            size=8,
            created=True,
            already_present=False,
        )


def _note_broker(
    fixture: Any,
    tasks: InMemoryInvestigationTaskRepository,
    sessions: InMemoryAgentSessionRepository,
    notes: _RecordingNoteService,
) -> tuple[AgenticToolBroker, _BrokerUnitOfWork]:
    uow = _BrokerUnitOfWork(fixture, tasks, sessions)
    broker = AgenticToolBroker(
        investigation_id=fixture.investigation_id,
        prose_authority=ProseAuthority.BRIDGE_ONLY,
        uow=uow,
        invocations=_service(fixture),
        handoffs=None,
        notes=notes,
        task_limits=TASK_LIMIT_DEFAULTS,
        clock=lambda: NOW,
        commit=uow.commit,
    )
    return broker, uow


async def _cancel(
    fixture: Any,
    tasks: InMemoryInvestigationTaskRepository,
    sessions: InMemoryAgentSessionRepository,
    task: Any,
    *,
    key: str,
) -> None:
    """Commit a real human cancel through the engine, in its own transaction."""
    engine = InvestigationTaskService(
        tasks=tasks,
        decisions=fixture.analyst_decisions,
        sessions=RepositoryTaskSessionPort(sessions),
        limits=TASK_LIMIT_DEFAULTS,
    )
    await engine.cancel(
        task_id=task.id,
        actor=HUMAN,
        expected_status_version=task.status_version,
        idempotency_key=key,
        reason="revoke the lease mid-call",
        now=NOW,
    )


async def _start_fixture(fixture: Any) -> None:
    await fixture.start(execution_mode=InvestigationExecutionMode.OPERATOR_LED)


# ------------------------------------------------------------ registry tools


async def test_registry_tool_refuses_a_cancel_committed_between_gate_and_persist() -> None:
    """The persisting invocation transaction revalidates the revoked lease."""
    fixture = _fixture()
    await _start_fixture(fixture)
    tasks = InMemoryInvestigationTaskRepository()
    sessions = InMemoryAgentSessionRepository()
    task, session_id, _secret = await _seed_agent_session(fixture, tasks, sessions)
    broker, uow = _broker(fixture, tasks, sessions)
    gate = _GatedTaskRepository(tasks, task_id=task.id)
    uow.investigation_tasks = gate

    invoking = asyncio.create_task(
        broker.invoke(
            session=_verified(session_id, fixture.investigation_id, task),
            tool_name="vt.file_report",
            schema_version="1",
            arguments=_ARGUMENTS,
            client_idempotency_key="cancel-between-gate-and-persist",
        )
    )
    await asyncio.wait_for(gate.captured.wait(), timeout=10)
    await _cancel(fixture, tasks, sessions, task, key="cancel-registry-race")
    gate.release.set()

    with pytest.raises((AgenticLeaseRefusedError, AgenticSessionInactiveError)) as refused:
        await asyncio.wait_for(invoking, timeout=10)
    assert refused.value.status_code == 409
    assert await _invocations(fixture) == ()
    assert fixture.tools["vt.file_report"].calls == []


async def test_registry_tool_refuses_a_lease_expired_on_the_database_clock_mid_call() -> None:
    """A skewed process clock never authorizes a lease the database expired."""
    fixture = _fixture()
    await _start_fixture(fixture)
    database_clock = [NOW]
    tasks = InMemoryInvestigationTaskRepository(clock=lambda: database_clock[0])
    sessions = InMemoryAgentSessionRepository()
    task, session_id, _secret = await _seed_agent_session(
        fixture,
        tasks,
        sessions,
        expires_at=NOW + timedelta(seconds=30),
        heartbeat_at=NOW - timedelta(seconds=150),
    )
    broker, uow = _broker(fixture, tasks, sessions)  # broker clock is frozen at NOW
    gate = _GatedTaskRepository(tasks, task_id=task.id)
    uow.investigation_tasks = gate

    invoking = asyncio.create_task(
        broker.invoke(
            session=_verified(session_id, fixture.investigation_id, task),
            tool_name="vt.file_report",
            schema_version="1",
            arguments=_ARGUMENTS,
            client_idempotency_key="db-clock-expiry",
        )
    )
    await asyncio.wait_for(gate.captured.wait(), timeout=10)
    # The prelude outlives the lease: only the database clock says so.
    database_clock[0] = NOW + timedelta(seconds=60)
    gate.release.set()

    with pytest.raises(AgenticLeaseRefusedError):
        await asyncio.wait_for(invoking, timeout=10)
    assert await _invocations(fixture) == ()
    assert fixture.tools["vt.file_report"].calls == []


# --------------------------------------------------------------- note.capture


async def test_note_capture_refuses_a_cancel_committed_between_gate_and_host_write() -> None:
    fixture = _fixture()
    await _start_fixture(fixture)
    tasks = InMemoryInvestigationTaskRepository()
    sessions = InMemoryAgentSessionRepository()
    task, session_id, _secret = await _seed_agent_session(fixture, tasks, sessions)
    notes = _RecordingNoteService()
    broker, uow = _note_broker(fixture, tasks, sessions, notes)
    gate = _GatedTaskRepository(tasks, task_id=task.id)
    uow.investigation_tasks = gate

    capturing = asyncio.create_task(
        broker.invoke(
            session=_verified(session_id, fixture.investigation_id, task),
            tool_name="note.capture",
            schema_version="1",
            arguments={"target": "note", "relpath": "analyse.md", "text": "# Note\n"},
            client_idempotency_key="note-cancel-race",
        )
    )
    await asyncio.wait_for(gate.captured.wait(), timeout=10)
    await _cancel(fixture, tasks, sessions, task, key="cancel-note-race")
    gate.release.set()

    with pytest.raises((AgenticLeaseRefusedError, AgenticSessionInactiveError)) as refused:
        await asyncio.wait_for(capturing, timeout=10)
    assert refused.value.status_code == 409
    assert notes.calls == []


async def test_note_capture_refuses_a_session_stopped_between_gate_and_host_write() -> None:
    fixture = _fixture()
    await _start_fixture(fixture)
    tasks = InMemoryInvestigationTaskRepository()
    sessions = InMemoryAgentSessionRepository()
    task, session_id, _secret = await _seed_agent_session(fixture, tasks, sessions)
    notes = _RecordingNoteService()
    broker, uow = _note_broker(fixture, tasks, sessions, notes)
    gate = _GatedTaskRepository(tasks, task_id=task.id)
    uow.investigation_tasks = gate

    capturing = asyncio.create_task(
        broker.invoke(
            session=_verified(session_id, fixture.investigation_id, task),
            tool_name="note.capture",
            schema_version="1",
            arguments={"target": "note", "relpath": "analyse.md", "text": "# Note\n"},
            client_idempotency_key="note-stop-race",
        )
    )
    await asyncio.wait_for(gate.captured.wait(), timeout=10)
    row = await sessions.get(session_id)
    assert row is not None
    stopped = row.with_state(AgentSessionState.STOPPED, now=NOW)
    assert await sessions.update_state(stopped, expected_state_version=row.state_version)
    gate.release.set()

    with pytest.raises(AgenticSessionInactiveError):
        await asyncio.wait_for(capturing, timeout=10)
    assert notes.calls == []


# ------------------------------------------------------------------- handoffs


def _operator_task_kwargs(
    *,
    session_id: UUID,
    fence: int = 1,
    status: TaskStatus = TaskStatus.RUNNING,
    status_version: int = 3,
    **overrides: Any,
) -> dict[str, Any]:
    now = datetime.now(UTC)
    values: dict[str, Any] = {
        "id": TASK_ID,
        "investigation_id": INVESTIGATION_ID,
        "task_key": "lead-operator",
        "title": "Operator task",
        "question": "Which gate contacts the C2?",
        "objective": "Identify the second-stage downloader.",
        "priority": TaskPriority.NORMAL,
        "status": status,
        "status_version": status_version,
        "result_verification_status": ResultVerificationStatus.UNVERIFIED,
        "created_by": HUMAN,
        "created_at": now - timedelta(minutes=5),
        "updated_at": now,
        "assigned_actor_kind": TaskActorKind.OPERATOR,
        "assigned_actor_id": OPERATOR_ACTOR.actor_id,
        "lease_id": uuid4(),
        "lease_session_id": session_id,
        "lease_fence": fence,
        "lease_expires_at": now + timedelta(minutes=5),
        "heartbeat_at": now,
    }
    values.update(overrides)
    return values


async def test_operator_handoff_refuses_a_task_whose_lease_was_revoked() -> None:
    """A revoked lease never gains an operator handoff appended under it."""
    harness = _Harness()
    session_id = uuid4()
    await harness.seed_task(
        **_operator_task_kwargs(
            session_id=session_id,
            status=TaskStatus.BLOCKED,
            status_version=4,
            blocked_reason=BlockedReason.LEASE_EXPIRED,
            lease_id=None,
            lease_session_id=None,
            lease_expires_at=None,
            heartbeat_at=None,
            lease_fence=2,
        )
    )

    with pytest.raises(HandoffError) as refused:
        await harness.service.create(
            INVESTIGATION_ID,
            actor=OPERATOR_ACTOR,
            idempotency_key="revoked-operator-handoff",
            request=harness.request(),
        )

    assert refused.value.code == "handoff_lease_stale"
    assert refused.value.status_code == 409
    assert await harness.handoffs.list_for_investigation(INVESTIGATION_ID) == ()


async def test_operator_handoff_binds_the_session_and_fence_of_the_lease() -> None:
    from cti_app.application.investigation_handoffs import (
        HandoffLeaseAuthority,
        HandoffLeaseAuthorityError,
    )

    harness = _Harness()
    session_id = uuid4()
    await harness.seed_task(**_operator_task_kwargs(session_id=session_id))

    record = await harness.service.create(
        INVESTIGATION_ID,
        actor=OPERATOR_ACTOR,
        idempotency_key="live-operator-handoff",
        request=harness.request(),
        authority=HandoffLeaseAuthority(task_id=TASK_ID, session_id=session_id, lease_fence=1),
    )
    assert record.handoff.task_id == str(TASK_ID)

    with pytest.raises(HandoffLeaseAuthorityError):
        await harness.service.create(
            INVESTIGATION_ID,
            actor=OPERATOR_ACTOR,
            idempotency_key="wrong-session-handoff",
            request=harness.request(),
            authority=HandoffLeaseAuthority(task_id=TASK_ID, session_id=uuid4(), lease_fence=1),
        )
    with pytest.raises(HandoffLeaseAuthorityError):
        await harness.service.create(
            INVESTIGATION_ID,
            actor=OPERATOR_ACTOR,
            idempotency_key="stale-fence-handoff",
            request=harness.request(),
            authority=HandoffLeaseAuthority(task_id=TASK_ID, session_id=session_id, lease_fence=2),
        )


# ---------------------------------------------------------------- task engine


def _engine(
    fixture: Any,
    tasks: InMemoryInvestigationTaskRepository,
    sessions: InMemoryAgentSessionRepository,
) -> InvestigationTaskService:
    return InvestigationTaskService(
        tasks=tasks,
        decisions=fixture.analyst_decisions,
        sessions=RepositoryTaskSessionPort(sessions),
        limits=TASK_LIMIT_DEFAULTS,
    )


async def test_submit_refuses_a_lease_the_database_clock_has_expired() -> None:
    fixture = _fixture()
    await _start_fixture(fixture)
    database_clock = [NOW]
    tasks = InMemoryInvestigationTaskRepository(clock=lambda: database_clock[0])
    sessions = InMemoryAgentSessionRepository()
    task, _session_id, _secret = await _seed_agent_session(
        fixture,
        tasks,
        sessions,
        expires_at=NOW + timedelta(seconds=30),
        heartbeat_at=NOW - timedelta(seconds=150),
    )
    database_clock[0] = NOW + timedelta(seconds=60)
    engine = _engine(fixture, tasks, sessions)

    with pytest.raises(TaskLeaseExpiredError):
        await engine.submit(
            task_id=task.id,
            actor=OPERATOR,
            expected_status_version=task.status_version,
            idempotency_key="submit-db-clock-expiry",
            result_refs=(),
            lease_fence=task.lease_fence,
            now=NOW,
        )
    stored = await tasks.get(task.id)
    assert stored is not None and stored.status is TaskStatus.RUNNING
    assert await tasks.list_events(task.id) == ()


async def test_submit_accepts_a_lease_the_database_clock_still_holds() -> None:
    """A skewed application clock must not revoke a lease the database keeps."""
    fixture = _fixture()
    await _start_fixture(fixture)
    database_clock = [NOW]
    tasks = InMemoryInvestigationTaskRepository(clock=lambda: database_clock[0])
    sessions = InMemoryAgentSessionRepository()
    task, _session_id, _secret = await _seed_agent_session(
        fixture,
        tasks,
        sessions,
        expires_at=NOW + timedelta(seconds=120),
        heartbeat_at=NOW,
    )
    engine = _engine(fixture, tasks, sessions)

    result = await engine.submit(
        task_id=task.id,
        actor=OPERATOR,
        expected_status_version=task.status_version,
        idempotency_key="submit-app-skew",
        result_refs=(),
        lease_fence=task.lease_fence,
        now=NOW + timedelta(seconds=180),
    )
    assert result.task.status is TaskStatus.AWAITING_REVIEW


@pytest.mark.parametrize("command", ["request_input", "block", "fail"])
async def test_operator_commands_refuse_an_expired_lease_on_the_database_clock(
    command: str,
) -> None:
    fixture = _fixture()
    await _start_fixture(fixture)
    database_clock = [NOW]
    tasks = InMemoryInvestigationTaskRepository(clock=lambda: database_clock[0])
    sessions = InMemoryAgentSessionRepository()
    task, _session_id, _secret = await _seed_agent_session(
        fixture,
        tasks,
        sessions,
        expires_at=NOW + timedelta(seconds=30),
        heartbeat_at=NOW - timedelta(seconds=150),
    )
    database_clock[0] = NOW + timedelta(seconds=60)
    engine = _engine(fixture, tasks, sessions)
    common: dict[str, Any] = {
        "task_id": task.id,
        "actor": OPERATOR,
        "expected_status_version": task.status_version,
        "lease_fence": task.lease_fence,
        "idempotency_key": f"{command}-db-clock-expiry",
        "now": NOW,
    }
    with pytest.raises(TaskLeaseExpiredError):
        if command == "request_input":
            await engine.request_input(**common)
        elif command == "block":
            await engine.block(blocked_reason=BlockedReason.EXTERNAL, **common)
        else:
            await engine.fail(reason="worker crashed", **common)
    stored = await tasks.get(task.id)
    assert stored is not None and stored.status is TaskStatus.RUNNING
