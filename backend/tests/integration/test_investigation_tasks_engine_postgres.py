"""PostgreSQL races for the M24 task engine (ADR-0011 D3, lot 04B2).

These oracles prove at SQL level what the in-memory fake cannot: two
transactions contending on one task row serialise (idempotent replay, one CAS
winner, gap-free event sequence), the dependency advisory lock lets exactly one
of two opposite edges win, and the lease expiry boundary is evaluated against
the database clock.

Disposable databases only.  Run them through ``make test-integration`` with
``INTEGRATION_TEST_PATH=tests/integration/test_investigation_tasks_engine_postgres.py``.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, timedelta
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from alembic import command
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cti_app.application.investigation_persistence import InvestigationTaskRepository
from cti_app.application.investigation_tasks import (
    InvestigationTaskService,
    StaleTaskVersionError,
    TaskCommandResult,
    TaskDependencyError,
    TaskDependencyResult,
    TaskLeaseExpiredError,
    TaskLeaseFenceError,
    TaskSessionOutcome,
    TaskSessionPort,
)
from cti_app.domain.agentic_operator import (
    AgentSessionState,
    AgentSessionV1,
    OperatorAdapterKind,
    OperatorCapabilitiesV1,
    OperatorProbeCommand,
)
from cti_app.domain.classification import TLP
from cti_app.domain.editions import Edition
from cti_app.domain.editorial import AnalystDecisionTargetType, AnalystDecisionType
from cti_app.domain.entities import Subject
from cti_app.domain.investigation_task import (
    InvestigationTaskV1,
    ResultVerificationStatus,
    TaskActor,
    TaskActorKind,
    TaskLimits,
    TaskPriority,
    TaskStatus,
)
from cti_app.domain.production import (
    AnalystInvestigation,
    LoopBudget,
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionRun,
)
from cti_app.infrastructure.database.session import (
    create_postgres_engine,
    create_session_factory,
)
from cti_app.infrastructure.database.uow import SqlAlchemyUnitOfWork
from tests.integration.conftest import _alembic_config
from tests.integration.edition_codes import reserve_edition_code

pytestmark = pytest.mark.integration

NOW = datetime(2026, 11, 22, 12, 0, tzinfo=UTC)
HUMAN = TaskActor(kind=TaskActorKind.HUMAN, actor_id="analyst:test")
OPERATOR = TaskActor(kind=TaskActorKind.OPERATOR, actor_id="operator:test")
LIMITS = TaskLimits()


class _SessionPort:
    """In-memory ``TaskSessionPort`` fake (the aggregate belongs to 05B1)."""

    def __init__(self, *, active: tuple[UUID, ...] = ()) -> None:
        self.active = set(active)
        self.closed: list[tuple[UUID, UUID, TaskSessionOutcome, str]] = []

    async def is_active_session(
        self, *, session_id: UUID, investigation_id: UUID, task_id: UUID
    ) -> bool:
        return session_id in self.active

    async def close_session(
        self,
        *,
        session_id: UUID,
        investigation_id: UUID,
        task_id: UUID,
        outcome: TaskSessionOutcome,
        reason: str,
    ) -> None:
        self.closed.append((session_id, task_id, outcome, reason))
        self.active.discard(session_id)


async def _seed_investigation(
    session_factory: async_sessionmaker[AsyncSession],
) -> AnalystInvestigation:
    suffix = uuid4().hex
    edition = Edition(
        country=f"Task engine fixture {suffix}",
        country_code=reserve_edition_code(),
        period_start=date(2026, 11, 1),
        period_end=date(2026, 11, 30),
        tlp=TLP.AMBER,
        languages=("en",),
    )
    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        assert await uow.editions.add_if_absent(edition)
        subject = Subject(
            edition_id=edition.id,
            title="Task engine fixture",
            slug=f"task-engine-fixture-{suffix}",
            tlp=TLP.AMBER,
        )
        run = ProductionRun(subject_id=subject.id, edition_id=edition.id)
        artifact = ProductionArtifact(
            production_run_id=run.id,
            subject_id=subject.id,
            stage=ProductionArtifactStage.SYNTHESIS,
            version=1,
            input_hash="e" * 64,
        )
        investigation = AnalystInvestigation(
            production_run_id=run.id,
            subject_id=subject.id,
            synthesis_artifact_id=artifact.id,
            budget=LoopBudget(),
        )
        await uow.subjects.add(subject)
        await uow.production_runs.add(run)
        await uow.production_artifacts.append(artifact)
        await uow.analyst_investigations.add(investigation)
        await uow.commit()
    return investigation


SHA_A = "a" * 64


async def _create_agent_session(
    session_factory: async_sessionmaker[AsyncSession],
    investigation_id: UUID,
    task_id: UUID,
    session_id: UUID,
) -> AgentSessionV1:
    """0023's task lease FK requires a real session row for the fixture lease."""
    capabilities = OperatorCapabilitiesV1(
        adapter_kind=OperatorAdapterKind.CODEX_CLI,
        executable="/usr/local/bin/codex",
        version="codex-cli 1.2.3",
        modes=("exec",),
        supports_resume=True,
        permission_modes=("workspace-write",),
        probed_at=NOW,
        probes=(
            OperatorProbeCommand(
                argv=("/usr/local/bin/codex", "--help"),
                exit_code=0,
                stdout_sha256=SHA_A,
                advertised_modes=("exec", "workspace-write"),
            ),
        ),
    )
    session = AgentSessionV1(
        id=session_id,
        investigation_id=investigation_id,
        task_id=task_id,
        adapter_kind=OperatorAdapterKind.CODEX_CLI,
        state=AgentSessionState.RUNNING,
        state_version=1,
        capability_probe=capabilities.to_payload(),
        capability_sha256=capabilities.capability_sha256,
        probed_at=capabilities.probed_at,
        auth_secret_nonce=b"n" * 32,
        auth_secret_hash=SHA_A,
        auth_secret_issued_at=NOW,
        opened_at=NOW,
        idempotency_key=f"engine-session-{session_id}",
        created_at=NOW,
        updated_at=NOW,
        host_session_ref=f"host-{session_id}",
    )
    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        stored, _created = await uow.agent_sessions.add(session)
        await uow.commit()
    return stored


async def _create_task(
    session_factory: async_sessionmaker[AsyncSession],
    investigation_id: UUID,
    *,
    status: TaskStatus = TaskStatus.READY,
    assigned_actor_kind: TaskActorKind | None = None,
    assigned_actor_id: str | None = None,
    lease: bool = False,
    lease_session_id: UUID | None = None,
) -> InvestigationTaskV1:
    values: dict[str, object] = {
        "investigation_id": investigation_id,
        "task_key": f"lead-{uuid4().hex[:16]}",
        "title": "Inspect the loader",
        "question": "Which gate contacts the C2?",
        "objective": "Identify the second-stage downloader.",
        "priority": TaskPriority.NORMAL,
        "status": status,
        "status_version": 1,
        "result_verification_status": (
            ResultVerificationStatus.PENDING_REVIEW
            if status is TaskStatus.AWAITING_REVIEW
            else ResultVerificationStatus.ACCEPTED
            if status is TaskStatus.COMPLETED
            else ResultVerificationStatus.UNVERIFIED
        ),
        "created_by": TaskActor(kind=TaskActorKind.SYSTEM, actor_id="plan_accept:test"),
        "created_at": NOW,
        "updated_at": NOW,
        "assigned_actor_kind": assigned_actor_kind,
        "assigned_actor_id": assigned_actor_id,
    }
    task = InvestigationTaskV1(**values)
    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        created = await uow.investigation_tasks.add(task)
        await uow.commit()
    if lease:
        # 0023 adds ``fk_investigation_tasks_lease_session``: the session row
        # must exist before the task can name it.  The task is inserted
        # operator-assigned and RUNNING without a lease, then the fixture lease
        # is set once the session exists, keeping status_version at 1.
        session_id = lease_session_id or uuid4()
        await _create_agent_session(session_factory, investigation_id, created.id, session_id)
        async with session_factory() as session:
            await session.execute(
                sa.text(
                    "UPDATE investigation_tasks SET lease_id = :lease_id, "
                    "lease_session_id = :session_id, lease_fence = 1, "
                    "lease_expires_at = :expires_at, heartbeat_at = :heartbeat "
                    "WHERE id = :task_id"
                ),
                {
                    "lease_id": uuid4(),
                    "session_id": session_id,
                    "expires_at": NOW + timedelta(seconds=180),
                    "heartbeat": NOW,
                    "task_id": created.id,
                },
            )
            await session.commit()
        async with SqlAlchemyUnitOfWork(session_factory) as uow:
            refreshed = await uow.investigation_tasks.get(created.id)
        assert refreshed is not None
        return refreshed
    return created


def _service(uow: SqlAlchemyUnitOfWork, sessions: TaskSessionPort) -> InvestigationTaskService:
    tasks: InvestigationTaskRepository = uow.investigation_tasks
    return InvestigationTaskService(
        tasks=tasks,
        decisions=uow.analyst_decisions,
        sessions=sessions,
        limits=LIMITS,
    )


async def _db_now(session_factory: async_sessionmaker[AsyncSession]) -> datetime:
    async with session_factory() as session:
        value = await session.scalar(sa.text("SELECT statement_timestamp()"))
    assert isinstance(value, datetime)
    return value


async def _task_event_count(
    session_factory: async_sessionmaker[AsyncSession], task_id: UUID
) -> int:
    async with session_factory() as session:
        value = await session.scalar(
            sa.text("SELECT count(*) FROM investigation_task_events WHERE task_id = :task_id"),
            {"task_id": task_id},
        )
    assert isinstance(value, int)
    return value


def test_concurrent_identical_command_single_effect(temporary_postgres_url: str) -> None:
    config = _alembic_config(temporary_postgres_url)
    command.upgrade(config, "head")

    async def exercise() -> None:
        engine = create_postgres_engine(temporary_postgres_url)
        try:
            factory = create_session_factory(engine)
            investigation = await _seed_investigation(factory)
            task = await _create_task(factory, investigation.id)
            sessions = _SessionPort()

            async def assign() -> TaskCommandResult:
                async with SqlAlchemyUnitOfWork(factory) as uow:
                    result = await _service(uow, sessions).assign(
                        task_id=task.id,
                        actor=HUMAN,
                        target=HUMAN,
                        expected_status_version=1,
                        idempotency_key="same-key",
                        now=NOW,
                    )
                    await uow.commit()
                    return result

            first, second = await asyncio.gather(assign(), assign())
            assert first.event is not None and second.event is not None
            assert first.event.id == second.event.id
            assert sorted([first.replayed, second.replayed]) == [False, True]
            assert await _task_event_count(factory, task.id) == 1
            async with SqlAlchemyUnitOfWork(factory) as uow:
                stored = await uow.investigation_tasks.get(task.id)
                assert stored is not None
                assert stored.status is TaskStatus.ASSIGNED
                assert stored.status_version == 2
                assert stored.event_sequence == 1
        finally:
            await engine.dispose()

    asyncio.run(exercise())


def test_concurrent_cycle_insertion_lets_exactly_one_win(
    temporary_postgres_url: str,
) -> None:
    config = _alembic_config(temporary_postgres_url)
    command.upgrade(config, "head")

    async def exercise() -> None:
        engine = create_postgres_engine(temporary_postgres_url)
        try:
            factory = create_session_factory(engine)
            investigation = await _seed_investigation(factory)
            first = await _create_task(factory, investigation.id, status=TaskStatus.PROPOSED)
            second = await _create_task(factory, investigation.id, status=TaskStatus.PROPOSED)
            sessions = _SessionPort()

            async def add_edge(task_id: UUID, depends_on_task_id: UUID) -> object:
                async with SqlAlchemyUnitOfWork(factory) as uow:
                    result: TaskDependencyResult = await _service(uow, sessions).add_dependency(
                        task_id=task_id,
                        depends_on_task_id=depends_on_task_id,
                        actor=HUMAN,
                    )
                    await uow.commit()
                    return result

            results = await asyncio.gather(
                add_edge(first.id, second.id),
                add_edge(second.id, first.id),
                return_exceptions=True,
            )
            successes = [item for item in results if isinstance(item, TaskDependencyResult)]
            failures = [item for item in results if isinstance(item, TaskDependencyError)]
            assert len(successes) == 1
            assert len(failures) == 1
            assert failures[0].code == "task_dependency_invalid"
            async with SqlAlchemyUnitOfWork(factory) as uow:
                edges = await uow.investigation_tasks.list_dependencies(first.id)
                reverse = await uow.investigation_tasks.list_dependencies(second.id)
                assert len(edges) + len(reverse) == 1
        finally:
            await engine.dispose()

    asyncio.run(exercise())


def test_heartbeat_expiry_boundary_uses_database_clock(
    temporary_postgres_url: str,
) -> None:
    config = _alembic_config(temporary_postgres_url)
    command.upgrade(config, "head")

    async def exercise() -> None:
        engine = create_postgres_engine(temporary_postgres_url)
        try:
            factory = create_session_factory(engine)
            investigation = await _seed_investigation(factory)
            session_id = uuid4()
            task = await _create_task(
                factory,
                investigation.id,
                status=TaskStatus.RUNNING,
                assigned_actor_kind=TaskActorKind.OPERATOR,
                assigned_actor_id=OPERATOR.actor_id,
                lease=True,
                lease_session_id=session_id,
            )
            sessions = _SessionPort(active=(session_id,))

            async with factory() as session:
                await session.execute(
                    sa.text(
                        "UPDATE investigation_tasks SET lease_expires_at = "
                        "statement_timestamp() + interval '60 seconds' WHERE id = :task_id"
                    ),
                    {"task_id": task.id},
                )
                await session.commit()
            db_now = await _db_now(factory)
            async with SqlAlchemyUnitOfWork(factory) as uow:
                renewed = await _service(uow, sessions).heartbeat(
                    task_id=task.id,
                    actor=OPERATOR,
                    expected_status_version=task.status_version,
                    lease_fence=1,
                    idempotency_key="heartbeat-before-expiry",
                    now=db_now,
                )
                await uow.commit()
            assert renewed.task.lease_expires_at is not None
            assert renewed.task.lease_expires_at > db_now

            async with factory() as session:
                await session.execute(
                    sa.text(
                        "UPDATE investigation_tasks SET lease_expires_at = "
                        "statement_timestamp() - interval '1 millisecond' WHERE id = :task_id"
                    ),
                    {"task_id": task.id},
                )
                await session.commit()
            expired_now = await _db_now(factory)
            async with SqlAlchemyUnitOfWork(factory) as uow:
                with pytest.raises(TaskLeaseExpiredError) as expired:
                    await _service(uow, sessions).heartbeat(
                        task_id=task.id,
                        actor=OPERATOR,
                        expected_status_version=task.status_version,
                        lease_fence=1,
                        idempotency_key="heartbeat-after-expiry",
                        now=expired_now,
                    )
                assert expired.value.code == "lease_expired"
                await uow.rollback()

            async with SqlAlchemyUnitOfWork(factory) as uow:
                with pytest.raises(TaskLeaseFenceError) as stale:
                    await _service(uow, sessions).heartbeat(
                        task_id=task.id,
                        actor=OPERATOR,
                        expected_status_version=task.status_version,
                        lease_fence=2,
                        idempotency_key="heartbeat-wrong-fence",
                        now=db_now,
                    )
                assert stale.value.code == "stale_fence"
                await uow.rollback()
        finally:
            await engine.dispose()

    asyncio.run(exercise())


def test_concurrent_status_commands_have_one_winner(temporary_postgres_url: str) -> None:
    config = _alembic_config(temporary_postgres_url)
    command.upgrade(config, "head")

    async def exercise() -> None:
        engine = create_postgres_engine(temporary_postgres_url)
        try:
            factory = create_session_factory(engine)
            investigation = await _seed_investigation(factory)
            session_id = uuid4()
            task = await _create_task(
                factory,
                investigation.id,
                status=TaskStatus.RUNNING,
                assigned_actor_kind=TaskActorKind.OPERATOR,
                assigned_actor_id=OPERATOR.actor_id,
                lease=True,
                lease_session_id=session_id,
            )
            sessions = _SessionPort(active=(session_id,))

            async def submit() -> TaskCommandResult:
                async with SqlAlchemyUnitOfWork(factory) as uow:
                    result = await _service(uow, sessions).submit(
                        task_id=task.id,
                        actor=OPERATOR,
                        expected_status_version=1,
                        idempotency_key="submit",
                        lease_fence=1,
                        now=NOW,
                    )
                    await uow.commit()
                    return result

            async def cancel() -> TaskCommandResult:
                async with SqlAlchemyUnitOfWork(factory) as uow:
                    result = await _service(uow, sessions).cancel(
                        task_id=task.id,
                        actor=HUMAN,
                        expected_status_version=1,
                        idempotency_key="cancel",
                        reason="operator work is obsolete",
                        now=NOW,
                    )
                    await uow.commit()
                    return result

            results = await asyncio.gather(submit(), cancel(), return_exceptions=True)
            successes = [item for item in results if isinstance(item, TaskCommandResult)]
            failures = [item for item in results if isinstance(item, StaleTaskVersionError)]
            assert len(successes) == 1
            assert len(failures) == 1
            assert failures[0].code == "stale_task_version"
            assert await _task_event_count(factory, task.id) == 1
            async with SqlAlchemyUnitOfWork(factory) as uow:
                stored = await uow.investigation_tasks.get(task.id)
                assert stored is not None
                assert stored.status_version == 2
                assert stored.event_sequence == 1
                assert stored.status in (TaskStatus.AWAITING_REVIEW, TaskStatus.CANCELLED)
        finally:
            await engine.dispose()

    asyncio.run(exercise())


def test_concurrent_heartbeats_keep_one_sequence_per_task(
    temporary_postgres_url: str,
) -> None:
    config = _alembic_config(temporary_postgres_url)
    command.upgrade(config, "head")

    async def exercise() -> None:
        engine = create_postgres_engine(temporary_postgres_url)
        try:
            factory = create_session_factory(engine)
            investigation = await _seed_investigation(factory)
            session_id = uuid4()
            task = await _create_task(
                factory,
                investigation.id,
                status=TaskStatus.RUNNING,
                assigned_actor_kind=TaskActorKind.OPERATOR,
                assigned_actor_id=OPERATOR.actor_id,
                lease=True,
                lease_session_id=session_id,
            )
            sessions = _SessionPort(active=(session_id,))

            async def heartbeat(key: str) -> TaskCommandResult:
                async with SqlAlchemyUnitOfWork(factory) as uow:
                    result = await _service(uow, sessions).heartbeat(
                        task_id=task.id,
                        actor=OPERATOR,
                        expected_status_version=1,
                        lease_fence=1,
                        idempotency_key=key,
                        now=NOW,
                    )
                    await uow.commit()
                    return result

            first, second = await asyncio.gather(heartbeat("hb-a"), heartbeat("hb-b"))
            assert first.event is not None and second.event is not None
            assert first.task.status_version == 1
            assert second.task.status_version == 1
            async with SqlAlchemyUnitOfWork(factory) as uow:
                events = await uow.investigation_tasks.list_events(task.id)
                stored = await uow.investigation_tasks.get(task.id)
            assert sorted(event.sequence for event in events) == [1, 2]
            assert {event.idempotency_key for event in events} == {"hb-a", "hb-b"}
            assert stored is not None
            assert stored.event_sequence == 2
            assert stored.status_version == 1
        finally:
            await engine.dispose()

    asyncio.run(exercise())


def test_accept_result_writes_decision_and_event_in_one_uow(
    temporary_postgres_url: str,
) -> None:
    config = _alembic_config(temporary_postgres_url)
    command.upgrade(config, "head")

    async def exercise() -> None:
        engine = create_postgres_engine(temporary_postgres_url)
        try:
            factory = create_session_factory(engine)
            investigation = await _seed_investigation(factory)
            task = await _create_task(factory, investigation.id, status=TaskStatus.AWAITING_REVIEW)
            sessions = _SessionPort()

            async with SqlAlchemyUnitOfWork(factory) as uow:
                accepted = await _service(uow, sessions).accept_result(
                    task_id=task.id,
                    actor=HUMAN,
                    expected_status_version=1,
                    idempotency_key="accept",
                    reason="result verified against the observations",
                    now=NOW,
                )
                await uow.commit()
            assert accepted.task.status is TaskStatus.COMPLETED
            assert accepted.task.result_verification_status is ResultVerificationStatus.ACCEPTED
            assert accepted.event is not None
            async with SqlAlchemyUnitOfWork(factory) as uow:
                decisions = await uow.analyst_decisions.list_for_investigation(investigation.id)
            matching = [item for item in decisions if item.target_id == task.id]
            assert len(matching) == 1
            assert accepted.event.analyst_decision_id == matching[0].id
            assert matching[0].decision_type is AnalystDecisionType.TASK_RESULT_ACCEPT
            assert matching[0].target_type is AnalystDecisionTargetType.TASK

            async with SqlAlchemyUnitOfWork(factory) as uow:
                replayed = await _service(uow, sessions).accept_result(
                    task_id=task.id,
                    actor=HUMAN,
                    expected_status_version=1,
                    idempotency_key="accept",
                    reason="result verified against the observations",
                    now=NOW,
                )
                await uow.rollback()
            assert replayed.replayed is True
            assert replayed.event is not None
            assert replayed.event.id == accepted.event.id
            async with SqlAlchemyUnitOfWork(factory) as uow:
                decisions = await uow.analyst_decisions.list_for_investigation(investigation.id)
            assert len([item for item in decisions if item.target_id == task.id]) == 1
        finally:
            await engine.dispose()

    asyncio.run(exercise())
