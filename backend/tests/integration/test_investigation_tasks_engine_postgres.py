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
from cti_app.application.investigation_task_events import TaskEventType
from cti_app.application.investigation_task_sweep import InvestigationTaskSweeper
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
from cti_app.domain.classification import TLP
from cti_app.domain.editions import Edition
from cti_app.domain.editorial import AnalystDecisionTargetType, AnalystDecisionType
from cti_app.domain.entities import Subject
from cti_app.domain.investigation_task import (
    BlockedReason,
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
    if lease:
        values.update(
            lease_id=uuid4(),
            lease_session_id=lease_session_id or uuid4(),
            lease_fence=1,
            lease_expires_at=NOW + timedelta(seconds=180),
            heartbeat_at=NOW,
        )
    task = InvestigationTaskV1(**values)
    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        created = await uow.investigation_tasks.add(task)
        await uow.commit()
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
            # The caller clock is deliberately a day behind PostgreSQL.  The
            # heartbeat may not use it: a live database lease must renew and
            # the new lease values must come from the database clock.
            async with SqlAlchemyUnitOfWork(factory) as uow:
                renewed = await _service(uow, sessions).heartbeat(
                    task_id=task.id,
                    actor=OPERATOR,
                    expected_status_version=task.status_version,
                    lease_fence=1,
                    idempotency_key="heartbeat-before-expiry",
                    now=db_now - timedelta(days=1),
                )
                await uow.commit()
            assert renewed.task.lease_expires_at is not None
            assert renewed.task.heartbeat_at is not None
            assert renewed.task.heartbeat_at != db_now - timedelta(days=1)
            assert (renewed.task.heartbeat_at - db_now).total_seconds() < 60
            assert renewed.task.lease_expires_at > renewed.task.heartbeat_at

            async with factory() as session:
                await session.execute(
                    sa.text(
                        "UPDATE investigation_tasks SET lease_expires_at = "
                        "statement_timestamp() - interval '1 millisecond' WHERE id = :task_id"
                    ),
                    {"task_id": task.id},
                )
                await session.commit()
            # Same trick in the other direction: the caller still believes the
            # lease is live, but the database boundary already passed, so the
            # conditional UPDATE must match zero rows.
            async with SqlAlchemyUnitOfWork(factory) as uow:
                with pytest.raises(TaskLeaseExpiredError) as expired:
                    await _service(uow, sessions).heartbeat(
                        task_id=task.id,
                        actor=OPERATOR,
                        expected_status_version=task.status_version,
                        lease_fence=1,
                        idempotency_key="heartbeat-after-expiry",
                        now=db_now - timedelta(days=1),
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


def test_concurrent_heartbeat_and_sweep_refuse_a_database_expired_lease(
    temporary_postgres_url: str,
) -> None:
    """Two real transactions race after the database lease boundary passed.

    The heartbeat still carries a caller timestamp captured before expiry; the
    old predicate would have renewed the lease from it.  Here the conditional
    UPDATE and the sweep both read PostgreSQL time, so neither revives it.
    """
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
                        "statement_timestamp() + interval '200 milliseconds' "
                        "WHERE id = :task_id"
                    ),
                    {"task_id": task.id},
                )
                await session.commit()
            caller_now = await _db_now(factory)
            async with factory() as session:
                db_expiry = await session.scalar(
                    sa.text("SELECT lease_expires_at FROM investigation_tasks WHERE id = :task_id"),
                    {"task_id": task.id},
                )
            assert isinstance(db_expiry, datetime)
            assert caller_now < db_expiry
            await asyncio.sleep(0.3)

            async def heartbeat() -> TaskCommandResult:
                async with SqlAlchemyUnitOfWork(factory) as uow:
                    result = await _service(uow, sessions).heartbeat(
                        task_id=task.id,
                        actor=OPERATOR,
                        expected_status_version=task.status_version,
                        lease_fence=1,
                        idempotency_key="concurrent-heartbeat",
                        now=caller_now,
                    )
                    await uow.commit()
                    return result

            async def sweep() -> tuple[TaskCommandResult, ...]:
                async with SqlAlchemyUnitOfWork(factory) as uow:
                    sweeper = InvestigationTaskSweeper(
                        tasks=uow.investigation_tasks, sessions=sessions
                    )
                    results = await sweeper.sweep_investigation(investigation.id)
                    await uow.commit()
                    return results

            heartbeat_result, swept = await asyncio.gather(
                heartbeat(), sweep(), return_exceptions=True
            )
            assert isinstance(heartbeat_result, TaskLeaseExpiredError)
            assert not isinstance(swept, BaseException)
            assert len(swept) == 1
            assert swept[0].task.status is TaskStatus.BLOCKED
            assert swept[0].task.blocked_reason is BlockedReason.LEASE_EXPIRED
            assert swept[0].task.lease is None

            async with SqlAlchemyUnitOfWork(factory) as uow:
                stored = await uow.investigation_tasks.get(task.id)
                assert stored is not None
                assert stored.status is TaskStatus.BLOCKED
                assert stored.lease is None
                assert stored.lease_fence == 2
                events = await uow.investigation_tasks.list_events(task.id)
            assert [event.event_type for event in events] == [TaskEventType.LEASE_EXPIRED]
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
            # The heartbeat boundary is PostgreSQL time; keep the lease live
            # relative to it instead of to the fixture's process timestamp.
            async with factory() as session:
                await session.execute(
                    sa.text(
                        "UPDATE investigation_tasks SET lease_expires_at = "
                        "statement_timestamp() + interval '1 hour' WHERE id = :task_id"
                    ),
                    {"task_id": task.id},
                )
                await session.commit()
            db_now = await _db_now(factory)

            async def heartbeat(key: str) -> TaskCommandResult:
                async with SqlAlchemyUnitOfWork(factory) as uow:
                    result = await _service(uow, sessions).heartbeat(
                        task_id=task.id,
                        actor=OPERATOR,
                        expected_status_version=1,
                        lease_fence=1,
                        idempotency_key=key,
                        now=db_now,
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
