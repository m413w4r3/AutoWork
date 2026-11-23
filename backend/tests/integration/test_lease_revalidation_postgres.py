"""PostgreSQL lease-revalidation oracles (F2-C, ADR-0011 D3/D7).

These tests use the real PostgreSQL repositories, the real row and advisory
locks, the real task engine and the real handoff service.  The broker's
invocation pipeline is the production admission logic with the provider
dispatcher stubbed (no worker, no provider), exactly like
``test_agentic_tool_broker_postgres.py``.

The interleaves are driven by events, never by sleeps that decide an outcome:
a gate captures the authority the cheap pre-check observed and lets a real
second transaction revoke it before the persisting transaction locks the row.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa

from cti_app.application.agent_session_verifier import VerifiedAgentSession
from cti_app.application.agentic_note_tools import NoteCaptureResult
from cti_app.application.agentic_tool_broker import (
    AgenticLeaseRefusedError,
    AgenticSessionInactiveError,
    AgenticToolBroker,
)
from cti_app.application.investigation_handoffs import (
    HandoffActor,
    HandoffCreateRequest,
    HandoffLeaseAuthority,
    HandoffLeaseAuthorityError,
    InvestigationHandoffService,
)
from cti_app.application.investigation_tasks import (
    InvestigationTaskService,
    RepositoryTaskSessionPort,
    StaleTaskVersionError,
    TaskCommandError,
    TaskCommandResult,
    TaskLeaseExpiredError,
    TaskLimits,
)
from cti_app.domain.agentic_operator import OperatorAdapterKind
from cti_app.domain.investigation_handoff import HandoffActorKind
from cti_app.domain.investigation_task import TaskActor, TaskActorKind, TaskStatus
from cti_app.infrastructure.database.session import create_postgres_engine
from cti_app.infrastructure.database.uow import SqlAlchemyUnitOfWork
from tests.integration.test_agentic_tool_broker_postgres import (
    OPERATOR_ID,
    SCHEMA_VERSION,
    TOOL_NAME,
    _ContextBuilder,
    _InvocationService,
    _PersistingExecutor,
    _registry,
    _seed,
)

pytestmark = pytest.mark.integration

HUMAN = TaskActor(kind=TaskActorKind.HUMAN, actor_id="analyst:test")
OPERATOR = TaskActor(kind=TaskActorKind.OPERATOR, actor_id=OPERATOR_ID)


class _GatedTaskRepository:
    """The real task repository with one gated ``get``.

    The broker's cheap gate calls ``get`` once and receives the row that
    existed *before* the gate released; the test commits a revocation in a
    second transaction while the gate is closed.
    """

    def __init__(self, inner: Any, *, task_id: UUID) -> None:
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


class _BlockingExecutor(_PersistingExecutor):
    """Holds the invocation transaction open so a cancel must wait for it."""

    def __init__(self, uow: Any) -> None:
        super().__init__(uow)
        self.reached_persist = asyncio.Event()
        self.release = asyncio.Event()

    async def execute_tool_call(self, **kwargs: Any) -> Any:
        self.reached_persist.set()
        await self.release.wait()
        return await super().execute_tool_call(**kwargs)


class _BlockingNoteService:
    """A host note write that signals and waits; no host process involved."""

    def __init__(self) -> None:
        self.capturing = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = 0

    async def capture(self, investigation_id: UUID, **kwargs: Any) -> NoteCaptureResult:
        self.calls += 1
        self.capturing.set()
        await self.release.wait()
        return NoteCaptureResult(
            target=str(kwargs["target"]),
            path=f"notes/agents/{kwargs['task_id']}/analyse.md",
            revision_id="rev-" + "b" * 32,
            sha256="b" * 64,
            size=8,
            created=True,
            already_present=False,
        )


def _verified(investigation_id: UUID, task_id: UUID, session_id: UUID) -> VerifiedAgentSession:
    return VerifiedAgentSession(
        session_id=session_id,
        investigation_id=investigation_id,
        task_id=task_id,
        adapter_kind=OperatorAdapterKind.CODEX_CLI,
        operator_id=OPERATOR_ID,
        task_lease_fence=1,
    )


def _service(uow: Any, investigation_id: UUID, executor: Any | None = None) -> _InvocationService:
    return _InvocationService(
        investigation_id=investigation_id,
        investigations=uow.analyst_investigations,
        states=uow.investigation_states,
        envelopes=uow.investigation_envelopes,
        invocations=uow.tool_invocations,
        context_builder=_ContextBuilder(),
        execution=executor or _PersistingExecutor(uow),
        registry=_registry(),
        available_capabilities=frozenset({"sample.analysis.read"}),
        unavailable_tool_reasons={},
        observations=uow.evidence_observations,
        evidence_summaries=None,
    )


async def _broker_with_authority(
    uow: Any, investigation_id: UUID, *, notes: Any = None, executor: Any = None
) -> AgenticToolBroker:
    investigation = await uow.analyst_investigations.get(investigation_id)
    assert investigation is not None
    return AgenticToolBroker(
        investigation_id=investigation_id,
        prose_authority=investigation.prose_authority,
        uow=uow,
        invocations=_service(uow, investigation_id, executor),
        handoffs=None,
        notes=notes,
        task_limits=TaskLimits(),
        commit=uow.commit,
    )


async def _cancel(
    uow_factory: Any, *, task_id: UUID, expected_status_version: int, key: str
) -> TaskCommandResult:
    async with uow_factory() as uow:
        engine = InvestigationTaskService(
            tasks=uow.investigation_tasks,
            decisions=uow.analyst_decisions,
            sessions=RepositoryTaskSessionPort(uow.agent_sessions),
            limits=TaskLimits(),
        )
        result = await engine.cancel(
            task_id=task_id,
            actor=HUMAN,
            expected_status_version=expected_status_version,
            idempotency_key=key,
            reason="revoke the lease mid-call",
        )
        await uow.commit()
        return result


async def _renew(
    uow_factory: Any, *, task_id: UUID, expected_status_version: int, fence: int, key: str
) -> TaskCommandResult:
    async with uow_factory() as uow:
        engine = InvestigationTaskService(
            tasks=uow.investigation_tasks,
            decisions=uow.analyst_decisions,
            sessions=RepositoryTaskSessionPort(uow.agent_sessions),
            limits=TaskLimits(),
        )
        result = await engine.heartbeat(
            task_id=task_id,
            actor=OPERATOR,
            expected_status_version=expected_status_version,
            lease_fence=fence,
            idempotency_key=key,
        )
        await uow.commit()
        return result


async def _invocation_count(uow_factory: Any, investigation_id: UUID) -> int:
    async with uow_factory() as uow:
        rows = await uow.tool_invocations.list_for_investigation(investigation_id)
    return len(rows)


async def test_registry_tool_refuses_a_cancel_committed_between_gate_and_persist(
    uow_factory: Any, migrated_postgres_url: str
) -> None:
    session_factory = uow_factory.session_factory
    investigation_id, task_id, session_id, _secret = await _seed(
        session_factory, migrated_postgres_url
    )
    async with uow_factory() as uow:
        broker = await _broker_with_authority(uow, investigation_id)
        gate = _GatedTaskRepository(uow.investigation_tasks, task_id=task_id)
        uow.investigation_tasks = gate
        invoking = asyncio.create_task(
            broker.invoke(
                session=_verified(investigation_id, task_id, session_id),
                tool_name=TOOL_NAME,
                schema_version=SCHEMA_VERSION,
                arguments={"association_id": str(uuid4())},
                client_idempotency_key="pg-cancel-between-gate-and-persist",
            )
        )
        await asyncio.wait_for(gate.captured.wait(), timeout=15)
        await asyncio.wait_for(
            _cancel(
                uow_factory,
                task_id=task_id,
                expected_status_version=3,
                key="pg-cancel-registry-race",
            ),
            timeout=15,
        )
        gate.release.set()
        with pytest.raises((AgenticLeaseRefusedError, AgenticSessionInactiveError)) as refused:
            await asyncio.wait_for(invoking, timeout=15)
        assert refused.value.status_code == 409

    assert await _invocation_count(uow_factory, investigation_id) == 0


async def test_cancel_waits_for_the_invocation_the_lease_authorized(
    uow_factory: Any, migrated_postgres_url: str
) -> None:
    """While the broker holds the task row, no revocation can commit."""
    session_factory = uow_factory.session_factory
    investigation_id, task_id, session_id, _secret = await _seed(
        session_factory, migrated_postgres_url
    )
    async with uow_factory() as uow:
        executor = _BlockingExecutor(uow)
        broker = await _broker_with_authority(uow, investigation_id, executor=executor)
        invoking = asyncio.create_task(
            broker.invoke(
                session=_verified(investigation_id, task_id, session_id),
                tool_name=TOOL_NAME,
                schema_version=SCHEMA_VERSION,
                arguments={"association_id": str(uuid4())},
                client_idempotency_key="pg-dispatch-under-lock",
            )
        )
        await asyncio.wait_for(executor.reached_persist.wait(), timeout=15)
        cancelling = asyncio.create_task(
            _cancel(
                uow_factory,
                task_id=task_id,
                expected_status_version=3,
                key="pg-cancel-blocked",
            )
        )
        # The cancel cannot get the investigation advisory lock or the task row
        # lock while the broker still owns the transaction.
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.shield(cancelling), timeout=0.5)
        executor.release.set()
        await asyncio.wait_for(invoking, timeout=15)
        await asyncio.wait_for(cancelling, timeout=15)

    async with uow_factory() as uow:
        rows = await uow.tool_invocations.list_for_investigation(investigation_id)
        task = await uow.investigation_tasks.get(task_id)
    assert len(rows) == 1
    assert task is not None
    assert task.status is TaskStatus.CANCELLED
    assert task.lease_id is None
    assert task.lease_fence == 2


async def test_note_capture_holds_the_lease_while_the_host_write_runs(
    uow_factory: Any, migrated_postgres_url: str
) -> None:
    session_factory = uow_factory.session_factory
    investigation_id, task_id, session_id, _secret = await _seed(
        session_factory, migrated_postgres_url
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
                client_idempotency_key="pg-note-under-lock",
            )
        )
        await asyncio.wait_for(notes.capturing.wait(), timeout=15)
        cancelling = asyncio.create_task(
            _cancel(
                uow_factory,
                task_id=task_id,
                expected_status_version=3,
                key="pg-note-cancel-blocked",
            )
        )
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.shield(cancelling), timeout=0.5)
        notes.release.set()
        captured = await asyncio.wait_for(capturing, timeout=15)

    # The route's unit of work owns the row lock; it is released on exit.
    await asyncio.wait_for(cancelling, timeout=15)
    assert captured.status == "captured"
    assert notes.calls == 1
    async with uow_factory() as uow:
        task = await uow.investigation_tasks.get(task_id)
    assert task is not None and task.status is TaskStatus.CANCELLED


async def test_operator_handoff_refuses_a_revoked_lease_under_the_row_lock(
    uow_factory: Any, migrated_postgres_url: str
) -> None:
    session_factory = uow_factory.session_factory
    investigation_id, task_id, session_id, _secret = await _seed(
        session_factory, migrated_postgres_url
    )
    service = InvestigationHandoffService(uow_factory=lambda: SqlAlchemyUnitOfWork(session_factory))
    request = HandoffCreateRequest(
        task_id=task_id,
        to_actor=HandoffActor(kind=HandoffActorKind.HUMAN, actor_id="analyst:test"),
        next_action="Confirm the first-stage gate and report back.",
    )
    authority = HandoffLeaseAuthority(task_id=task_id, session_id=session_id, lease_fence=1)
    record = await service.create(
        investigation_id,
        actor=HandoffActor(kind=HandoffActorKind.OPERATOR, actor_id=OPERATOR_ID),
        idempotency_key="pg-live-operator-handoff",
        request=request,
        authority=authority,
    )
    assert record.handoff.task_id == str(task_id)

    await _cancel(uow_factory, task_id=task_id, expected_status_version=3, key="pg-handoff-cancel")

    with pytest.raises(HandoffLeaseAuthorityError) as refused:
        await service.create(
            investigation_id,
            actor=HandoffActor(kind=HandoffActorKind.OPERATOR, actor_id=OPERATOR_ID),
            idempotency_key="pg-revoked-operator-handoff",
            request=request,
            authority=authority,
        )
    assert refused.value.code == "handoff_lease_stale"
    assert refused.value.status_code == 409

    async with uow_factory() as uow:
        rows = await uow.investigation_handoffs.list_for_investigation(investigation_id)
    assert len(rows) == 1


async def test_heartbeat_and_cancel_serialise_without_resurrecting_the_lease(
    uow_factory: Any, migrated_postgres_url: str
) -> None:
    session_factory = uow_factory.session_factory
    _investigation_id, task_id, _session_id, _secret = await _seed(
        session_factory, migrated_postgres_url
    )
    heartbeat, cancel = await asyncio.gather(
        _renew(
            uow_factory,
            task_id=task_id,
            expected_status_version=3,
            fence=1,
            key="pg-heartbeat-vs-cancel",
        ),
        _cancel(
            uow_factory, task_id=task_id, expected_status_version=3, key="pg-cancel-vs-heartbeat"
        ),
        return_exceptions=True,
    )
    # Exactly one of the two commands wins the row; the other is a typed 409.
    assert isinstance(cancel, TaskCommandResult)
    assert cancel.task.status is TaskStatus.CANCELLED
    if isinstance(heartbeat, BaseException):
        assert isinstance(
            heartbeat, (TaskCommandError, StaleTaskVersionError, TaskLeaseExpiredError)
        )
        assert heartbeat.status_code == 409  # type: ignore[attr-defined]
    else:
        assert heartbeat.task.status is TaskStatus.RUNNING

    async with uow_factory() as uow:
        task = await uow.investigation_tasks.get(task_id)
        events = await uow.investigation_tasks.list_events(task_id)
    assert task is not None
    assert task.status is TaskStatus.CANCELLED
    assert task.lease_id is None and task.lease_expires_at is None
    assert task.lease_fence == 2
    cancelled_at = next(
        event.sequence for event in events if event.to_status is TaskStatus.CANCELLED
    )
    renewed = [event.sequence for event in events if event.event_type.value == "lease_renewed"]
    assert all(sequence < cancelled_at for sequence in renewed)


async def test_identical_concurrent_calls_replay_one_invocation_twenty_times(
    uow_factory: Any, migrated_postgres_url: str
) -> None:
    session_factory = uow_factory.session_factory
    investigation_id, task_id, session_id, _secret = await _seed(
        session_factory, migrated_postgres_url
    )

    async def call(key: str, association_id: str) -> Any:
        async with uow_factory() as uow:
            broker = await _broker_with_authority(uow, investigation_id)
            return await broker.invoke(
                session=_verified(investigation_id, task_id, session_id),
                tool_name=TOOL_NAME,
                schema_version=SCHEMA_VERSION,
                arguments={"association_id": association_id},
                client_idempotency_key=key,
            )

    for attempt in range(20):
        key = f"pg-replay-{attempt}"
        association_id = str(uuid4())
        first, second = await asyncio.gather(call(key, association_id), call(key, association_id))
        assert first.invocation_id == second.invocation_id
        assert {first.idempotent_replay, second.idempotent_replay} == {False, True}

    # Under load: one client key, eight simultaneous requests.
    burst_key = "pg-burst-key"
    association_id = str(uuid4())
    burst = await asyncio.gather(*[call(burst_key, association_id) for _ in range(8)])
    assert len({result.invocation_id for result in burst}) == 1
    assert sum(1 for result in burst if result.idempotent_replay is False) == 1

    # Two *different* concurrent calls are two real invocations, never merged.
    left, right = await asyncio.gather(
        call("pg-distinct-left", str(uuid4())), call("pg-distinct-right", str(uuid4()))
    )
    assert left.invocation_id != right.invocation_id
    assert left.idempotent_replay is False and right.idempotent_replay is False

    assert await _invocation_count(uow_factory, investigation_id) == 23


async def test_expired_lease_race_never_persists_an_invocation(
    uow_factory: Any, migrated_postgres_url: str
) -> None:
    """A lease the database expired during the prelude refuses the effect."""
    session_factory = uow_factory.session_factory
    investigation_id, task_id, session_id, _secret = await _seed(
        session_factory, migrated_postgres_url
    )
    engine = create_postgres_engine(migrated_postgres_url)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                sa.text(
                    "UPDATE investigation_tasks SET heartbeat_at = :heartbeat, "
                    "lease_expires_at = :expires_at WHERE id = :task_id"
                ),
                {
                    "heartbeat": datetime.now(UTC) - timedelta(seconds=400),
                    "expires_at": datetime.now(UTC) - timedelta(seconds=1),
                    "task_id": task_id,
                },
            )
    finally:
        await engine.dispose()
    async with uow_factory() as uow:
        broker = await _broker_with_authority(uow, investigation_id)
        with pytest.raises(AgenticLeaseRefusedError):
            await broker.invoke(
                session=_verified(investigation_id, task_id, session_id),
                tool_name=TOOL_NAME,
                schema_version=SCHEMA_VERSION,
                arguments={"association_id": str(uuid4())},
                client_idempotency_key="pg-db-expired-lease",
            )
    assert await _invocation_count(uow_factory, investigation_id) == 0
