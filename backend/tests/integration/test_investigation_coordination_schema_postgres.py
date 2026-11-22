"""PostgreSQL oracles for migration 0021 and the task schema/guards.

Disposable databases only: fresh upgrade, conforming re-application, partial
state refusal, append-only triggers, the per-task event idempotency key, the
cumulative decision CHECK sets, the composite same-investigation dependency
foreign key, the status/verification CHECK pairs and the repository CAS/lease
primitives the 04B2 engine will compose.
"""

from __future__ import annotations

import asyncio
import re
from datetime import UTC, date, datetime, timedelta
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from alembic import command
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cti_app.application.workspace_agent import (
    WorkspaceBindingV1,
    WorkspaceFileRevisionV1,
    workspace_revision_id,
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
from cti_app.domain.editorial import (
    AnalystDecision,
    AnalystDecisionTargetType,
    AnalystDecisionType,
)
from cti_app.domain.entities import Subject
from cti_app.domain.investigation_task import (
    InvestigationTaskV1,
    ResultVerificationStatus,
    TaskActor,
    TaskActorKind,
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
from cti_app.domain.work_contribution import (
    WorkContributionImportStatus,
    WorkContributionOrigin,
    WorkContributionV1,
    WorkContributionValidationCode,
)
from cti_app.infrastructure.database.session import (
    create_postgres_engine,
    create_session_factory,
)
from cti_app.infrastructure.database.uow import SqlAlchemyUnitOfWork
from tests.integration.conftest import _alembic_config
from tests.integration.edition_codes import reserve_edition_code

pytestmark = pytest.mark.integration

PREVIOUS = "0020_investigation_plans"
HEAD = "0024_work_contributions"
CONTRIBUTIONS_REVISION = "0024_work_contributions"
TASK_TABLES = {
    "investigation_tasks",
    "investigation_task_dependencies",
    "investigation_task_events",
}
NOW = datetime(2026, 11, 22, 12, 0, tzinfo=UTC)
SHA_A = "a" * 64


def _table_names(database_url: str) -> set[str]:
    async def run() -> set[str]:
        engine = create_postgres_engine(database_url)
        try:
            async with engine.connect() as connection:
                return set(
                    await connection.run_sync(
                        lambda sync_connection: sa.inspect(sync_connection).get_table_names()
                    )
                )
        finally:
            await engine.dispose()

    return asyncio.run(run())


def _alembic_version(database_url: str) -> str:
    async def run() -> str:
        engine = create_postgres_engine(database_url)
        try:
            async with engine.connect() as connection:
                value = await connection.scalar(sa.text("SELECT version_num FROM alembic_version"))
                assert isinstance(value, str)
                return value
        finally:
            await engine.dispose()

    return asyncio.run(run())


def _sqlstate(exc: DBAPIError) -> str:
    return str(getattr(exc.orig, "sqlstate", ""))


async def _seed_investigation(
    session_factory: async_sessionmaker[AsyncSession],
) -> AnalystInvestigation:
    suffix = uuid4().hex
    edition = Edition(
        country=f"Task fixture {suffix}",
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
            title="Task fixture",
            slug=f"task-fixture-{suffix}",
            tlp=TLP.AMBER,
        )
        run = ProductionRun(subject_id=subject.id, edition_id=edition.id)
        artifact = ProductionArtifact(
            production_run_id=run.id,
            subject_id=subject.id,
            stage=ProductionArtifactStage.SYNTHESIS,
            version=1,
            input_hash="d" * 64,
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


async def _create_agent_session(
    session_factory: async_sessionmaker[AsyncSession],
    investigation_id: UUID,
    task_id: UUID,
    session_id: UUID,
) -> None:
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
        state=AgentSessionState.READY,
        state_version=1,
        capability_probe=capabilities.to_payload(),
        capability_sha256=capabilities.capability_sha256,
        probed_at=capabilities.probed_at,
        auth_secret_nonce=b"n" * 32,
        auth_secret_hash=SHA_A,
        auth_secret_issued_at=NOW,
        opened_at=NOW,
        idempotency_key=f"lease-session-{session_id}",
        created_at=NOW,
        updated_at=NOW,
        host_session_ref=f"host-{session_id}",
    )
    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        await uow.agent_sessions.add(session)
        await uow.commit()


async def _create_task(
    session_factory: async_sessionmaker[AsyncSession],
    investigation_id: UUID,
    *,
    task_key: str | None = None,
    status: TaskStatus = TaskStatus.READY,
    assigned_actor_kind: TaskActorKind | None = None,
    assigned_actor_id: str | None = None,
    lease: bool = False,
    lease_session_id: UUID | None = None,
) -> InvestigationTaskV1:
    values: dict[str, object] = {
        "investigation_id": investigation_id,
        "task_key": task_key or f"lead-{uuid4().hex[:16]}",
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


async def _expect_violation(
    session_factory: async_sessionmaker[AsyncSession],
    statement: str,
    params: dict[str, object],
    *,
    sqlstate: str,
) -> None:
    async with session_factory() as session:
        with pytest.raises(DBAPIError) as exc_info:
            await session.execute(sa.text(statement), params)
        assert _sqlstate(exc_info.value) == sqlstate
        await session.rollback()


async def _execute(
    session_factory: async_sessionmaker[AsyncSession],
    statement: str,
    params: dict[str, object],
) -> None:
    async with session_factory() as session:
        await session.execute(sa.text(statement), params)
        await session.commit()


def test_task_migration_creates_tables_and_refuses_used_downgrade(
    temporary_postgres_url: str,
) -> None:
    config = _alembic_config(temporary_postgres_url)
    command.upgrade(config, "head")
    assert TASK_TABLES <= _table_names(temporary_postgres_url)
    assert _alembic_version(temporary_postgres_url) == HEAD

    async def seed() -> UUID:
        engine = create_postgres_engine(temporary_postgres_url)
        try:
            factory = create_session_factory(engine)
            investigation = await _seed_investigation(factory)
            task = await _create_task(factory, investigation.id)
            return task.id
        finally:
            await engine.dispose()

    task_id = asyncio.run(seed())
    assert isinstance(task_id, UUID)
    with pytest.raises(RuntimeError, match="refusing to downgrade"):
        command.downgrade(config, PREVIOUS)
    assert _alembic_version(temporary_postgres_url) == HEAD
    assert TASK_TABLES <= _table_names(temporary_postgres_url)


def test_task_migration_accepts_an_already_conforming_shape(
    temporary_postgres_url: str,
) -> None:
    config = _alembic_config(temporary_postgres_url)
    command.upgrade(config, "head")
    command.stamp(config, PREVIOUS)
    command.upgrade(config, "head")
    assert TASK_TABLES <= _table_names(temporary_postgres_url)
    assert _alembic_version(temporary_postgres_url) == HEAD


def test_task_migration_rejects_a_partial_task_shape(
    temporary_postgres_url: str,
) -> None:
    config = _alembic_config(temporary_postgres_url)
    command.upgrade(config, "head")
    command.stamp(config, PREVIOUS)

    async def drop_events() -> None:
        engine = create_postgres_engine(temporary_postgres_url)
        try:
            async with engine.begin() as connection:
                await connection.execute(sa.text("DROP TABLE investigation_task_events"))
        finally:
            await engine.dispose()

    asyncio.run(drop_events())
    with pytest.raises(RuntimeError, match="preflight rejected"):
        command.upgrade(config, "head")
    assert _alembic_version(temporary_postgres_url) == PREVIOUS
    assert "investigation_tasks" in _table_names(temporary_postgres_url)
    assert "investigation_task_events" not in _table_names(temporary_postgres_url)


def test_tasks_checks_match_status_verification_pairs(
    temporary_postgres_url: str,
) -> None:
    config = _alembic_config(temporary_postgres_url)
    command.upgrade(config, "head")

    async def exercise() -> None:
        engine = create_postgres_engine(temporary_postgres_url)
        try:
            factory = create_session_factory(engine)
            investigation = await _seed_investigation(factory)
            task = await _create_task(factory, investigation.id)
            sibling = await _create_task(factory, investigation.id)
            lease_session = uuid4()
            params = {
                "id": task.id,
                "sibling": sibling.id,
                "lease": uuid4(),
                "session": lease_session,
                "sha": SHA_A,
            }

            await _expect_violation(
                factory,
                "UPDATE investigation_tasks SET status = 'awaiting_review' WHERE id = :id",
                params,
                sqlstate="23514",
            )
            await _expect_violation(
                factory,
                "UPDATE investigation_tasks SET status = 'completed' WHERE id = :id",
                params,
                sqlstate="23514",
            )
            await _expect_violation(
                factory,
                "UPDATE investigation_tasks SET status = 'blocked' WHERE id = :id",
                params,
                sqlstate="23514",
            )
            await _expect_violation(
                factory,
                "UPDATE investigation_tasks SET blocked_reason = 'dependency' WHERE id = :id",
                params,
                sqlstate="23514",
            )
            await _expect_violation(
                factory,
                "UPDATE investigation_tasks SET parent_task_id = :sibling, depth = 0 "
                "WHERE id = :id",
                params,
                sqlstate="23514",
            )
            await _expect_violation(
                factory,
                "UPDATE investigation_tasks SET depth = 1 WHERE id = :id",
                params,
                sqlstate="23514",
            )
            await _expect_violation(
                factory,
                "UPDATE investigation_tasks SET status = 'completed', "
                "result_verification_status = 'accepted', assigned_actor_kind = 'operator', "
                "assigned_actor_id = 'operator:test', lease_id = :lease, "
                "lease_session_id = :session, lease_fence = 1, "
                "lease_expires_at = now(), heartbeat_at = now() WHERE id = :id",
                params,
                sqlstate="23514",
            )
            await _expect_violation(
                factory,
                "UPDATE investigation_tasks SET assigned_actor_kind = 'human', "
                "assigned_actor_id = 'analyst:test', lease_id = :lease, "
                "lease_session_id = :session, lease_fence = 1, "
                "lease_expires_at = now(), heartbeat_at = now() WHERE id = :id",
                params,
                sqlstate="23514",
            )

            await _execute(
                factory,
                "UPDATE investigation_tasks SET status = 'awaiting_review', "
                "result_verification_status = 'pending_review' WHERE id = :id",
                params,
            )
            await _execute(
                factory,
                "UPDATE investigation_tasks SET status = 'completed', "
                "result_verification_status = 'accepted' WHERE id = :id",
                params,
            )
            await _execute(
                factory,
                "UPDATE investigation_tasks SET status = 'blocked', "
                "blocked_reason = 'dependency' WHERE id = :id",
                {"id": sibling.id},
            )
        finally:
            await engine.dispose()

    asyncio.run(exercise())


def test_tool_invocation_task_attribution_columns(
    temporary_postgres_url: str,
) -> None:
    config = _alembic_config(temporary_postgres_url)
    command.upgrade(config, "head")

    async def exercise() -> None:
        engine = create_postgres_engine(temporary_postgres_url)
        try:
            factory = create_session_factory(engine)
            investigation = await _seed_investigation(factory)
            task = await _create_task(factory, investigation.id)
            session_id = uuid4()
            await _create_agent_session(factory, investigation.id, task.id, session_id)
            base = {
                "id": uuid4(),
                "investigation_id": investigation.id,
                "task_id": task.id,
                "agent_session_id": session_id,
                "operation_key": f"attribution-{uuid4()}",
                "now": NOW,
            }
            insert = (
                "INSERT INTO tool_invocations "
                "(id, investigation_id, operation_key, tool_name, schema_version, "
                "normalized_parameters, purpose, status, reserved_budget, consumed_budget, "
                "response_refs, created_at, updated_at, version, task_id, actor_kind, "
                "actor_id, agent_session_id) "
                "VALUES (:id, :investigation_id, :operation_key, 'vt.file_report', "
                "'vt-file-report.v1', '{}'::jsonb, 'Attribution oracle', 'pending', "
                "'{}'::jsonb, '{}'::jsonb, '[]'::jsonb, :now, :now, 1, :task_id, "
                ":actor_kind, :actor_id, :agent_session_id)"
            )
            await _execute(
                factory,
                insert,
                {**base, "actor_kind": "operator", "actor_id": "operator:test"},
            )
            await _expect_violation(
                factory,
                insert,
                {
                    **base,
                    "id": uuid4(),
                    "operation_key": f"pair-{uuid4()}",
                    "actor_kind": None,
                    "actor_id": "operator:test",
                },
                sqlstate="23514",
            )
            await _expect_violation(
                factory,
                insert,
                {
                    **base,
                    "id": uuid4(),
                    "operation_key": f"system-{uuid4()}",
                    "actor_kind": "system",
                    "actor_id": "system:test",
                },
                sqlstate="23514",
            )
            await _expect_violation(
                factory,
                insert,
                {
                    **base,
                    "id": uuid4(),
                    "operation_key": f"fk-{uuid4()}",
                    "task_id": uuid4(),
                    "actor_kind": "operator",
                    "actor_id": "operator:test",
                },
                sqlstate="23503",
            )
        finally:
            await engine.dispose()

    asyncio.run(exercise())


def test_task_events_and_dependencies_are_append_only(
    temporary_postgres_url: str,
) -> None:
    config = _alembic_config(temporary_postgres_url)
    command.upgrade(config, "head")

    async def exercise() -> None:
        engine = create_postgres_engine(temporary_postgres_url)
        try:
            factory = create_session_factory(engine)
            investigation = await _seed_investigation(factory)
            task = await _create_task(factory, investigation.id)
            dependency = await _create_task(factory, investigation.id)
            async with SqlAlchemyUnitOfWork(factory) as uow:
                await uow.investigation_tasks.add_dependency(
                    investigation_id=investigation.id,
                    task_id=task.id,
                    depends_on_task_id=dependency.id,
                )
                await uow.commit()
            event_id = uuid4()
            async with factory() as session:
                await session.execute(
                    sa.text(
                        "INSERT INTO investigation_task_events "
                        "(id, investigation_id, task_id, sequence, event_type, from_status, "
                        "to_status, status_version, lease_fence, actor_kind, actor_id, "
                        "idempotency_key, request_sha256, payload, occurred_at) "
                        "VALUES (:id, :investigation_id, :task_id, 1, 'status_changed', "
                        "'proposed', 'ready', 1, 0, 'system', 'sweep:test', "
                        "'append-only', :sha, '{}'::jsonb, :now)"
                    ),
                    {
                        "id": event_id,
                        "investigation_id": investigation.id,
                        "task_id": task.id,
                        "sha": SHA_A,
                        "now": NOW,
                    },
                )
                await session.commit()

            for statement, params in (
                (
                    "UPDATE investigation_task_events SET actor_id = 'x' WHERE id = :id",
                    {"id": event_id},
                ),
                ("DELETE FROM investigation_task_events WHERE id = :id", {"id": event_id}),
                ("TRUNCATE investigation_task_events", {}),
                (
                    "UPDATE investigation_task_dependencies SET depends_on_task_id = task_id",
                    {},
                ),
                ("DELETE FROM investigation_task_dependencies", {}),
                ("TRUNCATE investigation_task_dependencies", {}),
            ):
                async with factory() as session:
                    with pytest.raises(DBAPIError, match="append-only") as exc_info:
                        await session.execute(sa.text(statement), params)
                    assert _sqlstate(exc_info.value) == "55000"
                    await session.rollback()
        finally:
            await engine.dispose()

    asyncio.run(exercise())


def test_task_event_idempotency_key_is_unique_per_task(
    temporary_postgres_url: str,
) -> None:
    config = _alembic_config(temporary_postgres_url)
    command.upgrade(config, "head")

    async def exercise() -> None:
        engine = create_postgres_engine(temporary_postgres_url)
        try:
            factory = create_session_factory(engine)
            investigation = await _seed_investigation(factory)
            first = await _create_task(factory, investigation.id)
            second = await _create_task(factory, investigation.id)
            insert = (
                "INSERT INTO investigation_task_events "
                "(id, investigation_id, task_id, sequence, event_type, status_version, "
                "lease_fence, actor_kind, actor_id, idempotency_key, request_sha256, "
                "payload, occurred_at) "
                "VALUES (:id, :investigation_id, :task_id, :sequence, 'task_created', 1, 0, "
                "'system', 'plan_accept:test', :key, :sha, '{}'::jsonb, :now)"
            )
            params = {
                "id": uuid4(),
                "investigation_id": investigation.id,
                "task_id": first.id,
                "sequence": 1,
                "key": "plan:shared",
                "sha": SHA_A,
                "now": NOW,
            }
            await _execute(factory, insert, params)
            await _expect_violation(
                factory,
                insert,
                {**params, "id": uuid4(), "sequence": 2},
                sqlstate="23505",
            )
            await _expect_violation(
                factory,
                insert,
                {**params, "id": uuid4(), "sequence": 1, "key": "plan:other"},
                sqlstate="23505",
            )
            await _execute(
                factory,
                insert,
                {**params, "id": uuid4(), "task_id": second.id, "sequence": 1},
            )
        finally:
            await engine.dispose()

    asyncio.run(exercise())


def test_analyst_decision_checks_are_cumulative(
    temporary_postgres_url: str,
) -> None:
    config = _alembic_config(temporary_postgres_url)
    command.upgrade(config, "head")

    async def exercise() -> None:
        engine = create_postgres_engine(temporary_postgres_url)
        try:
            factory = create_session_factory(engine)
            investigation = await _seed_investigation(factory)
            async with SqlAlchemyUnitOfWork(factory) as uow:
                await uow.analyst_decisions.append(
                    AnalystDecision(
                        investigation_id=investigation.id,
                        decision_type=AnalystDecisionType.PLAN_ACCEPT,
                        target_type=AnalystDecisionTargetType.PLAN_VERSION,
                        target_id=uuid4(),
                        actor_id="analyst:test",
                        reason="Accept the plan under review",
                        correlation_id=f"plan-{uuid4()}",
                    )
                )
                await uow.analyst_decisions.append(
                    AnalystDecision(
                        investigation_id=investigation.id,
                        decision_type=AnalystDecisionType.TASK_RESULT_ACCEPT,
                        target_type=AnalystDecisionTargetType.TASK,
                        target_id=uuid4(),
                        actor_id="analyst:test",
                        reason="Accept the operator result",
                        correlation_id=f"task-{uuid4()}",
                    )
                )
                await uow.commit()
        finally:
            await engine.dispose()

    asyncio.run(exercise())


def test_task_dependencies_require_same_investigation(
    temporary_postgres_url: str,
) -> None:
    config = _alembic_config(temporary_postgres_url)
    command.upgrade(config, "head")

    async def exercise() -> None:
        engine = create_postgres_engine(temporary_postgres_url)
        try:
            factory = create_session_factory(engine)
            investigation = await _seed_investigation(factory)
            foreign = await _seed_investigation(factory)
            task = await _create_task(factory, investigation.id)
            foreign_task = await _create_task(factory, foreign.id)
            async with SqlAlchemyUnitOfWork(factory) as uow:
                with pytest.raises(DBAPIError) as exc_info:
                    await uow.investigation_tasks.add_dependency(
                        investigation_id=investigation.id,
                        task_id=task.id,
                        depends_on_task_id=foreign_task.id,
                    )
                    await uow.commit()
                assert _sqlstate(exc_info.value) == "23503"
                await uow.rollback()
            async with factory() as session:
                with pytest.raises(DBAPIError) as exc_info:
                    await session.execute(
                        sa.text(
                            "INSERT INTO investigation_task_dependencies "
                            "(task_id, depends_on_task_id, investigation_id) "
                            "VALUES (:task_id, :task_id, :investigation_id)"
                        ),
                        {"task_id": task.id, "investigation_id": investigation.id},
                    )
                assert _sqlstate(exc_info.value) == "23514"
                await session.rollback()
        finally:
            await engine.dispose()

    asyncio.run(exercise())


def test_task_repository_cas_rejects_stale_status_version(
    temporary_postgres_url: str,
) -> None:
    config = _alembic_config(temporary_postgres_url)
    command.upgrade(config, "head")

    async def exercise() -> None:
        engine = create_postgres_engine(temporary_postgres_url)
        try:
            factory = create_session_factory(engine)
            investigation = await _seed_investigation(factory)
            task = await _create_task(factory, investigation.id)
            cancelled = task.with_status(TaskStatus.CANCELLED, now=NOW)
            async with SqlAlchemyUnitOfWork(factory) as uow:
                updated = await uow.investigation_tasks.update_status(
                    cancelled,
                    expected_status_version=task.status_version,
                    expected_status=task.status,
                )
                assert updated is not None
                assert updated.status is TaskStatus.CANCELLED
                assert updated.status_version == task.status_version + 1
                await uow.commit()
            async with SqlAlchemyUnitOfWork(factory) as uow:
                stale = await uow.investigation_tasks.update_status(
                    cancelled,
                    expected_status_version=task.status_version,
                    expected_status=task.status,
                )
                assert stale is None
                await uow.rollback()
        finally:
            await engine.dispose()

    asyncio.run(exercise())


def test_task_repository_acquires_and_renews_lease_with_fence(
    temporary_postgres_url: str,
) -> None:
    config = _alembic_config(temporary_postgres_url)
    command.upgrade(config, "head")

    async def exercise() -> None:
        engine = create_postgres_engine(temporary_postgres_url)
        try:
            factory = create_session_factory(engine)
            investigation = await _seed_investigation(factory)
            task = await _create_task(
                factory,
                investigation.id,
                status=TaskStatus.ASSIGNED,
                assigned_actor_kind=TaskActorKind.OPERATOR,
                assigned_actor_id="operator:test",
            )
            lease_session_id = uuid4()
            await _create_agent_session(factory, investigation.id, task.id, lease_session_id)
            async with SqlAlchemyUnitOfWork(factory) as uow:
                running = await uow.investigation_tasks.acquire_lease(
                    task.id,
                    expected_status_version=task.status_version,
                    lease_id=uuid4(),
                    lease_session_id=lease_session_id,
                    now=NOW,
                    ttl_seconds=180,
                )
                assert running is not None
                assert running.status is TaskStatus.RUNNING
                assert running.lease_fence == task.lease_fence + 1
                assert running.lease is not None
                await uow.commit()

            # The heartbeat boundary is the database clock, so pin the lease
            # relative to it instead of to the process timestamp used above.
            async with factory() as session:
                await session.execute(
                    sa.text(
                        "UPDATE investigation_tasks SET lease_expires_at = "
                        "statement_timestamp() + interval '1 hour' WHERE id = :task_id"
                    ),
                    {"task_id": task.id},
                )
                await session.commit()

            async with SqlAlchemyUnitOfWork(factory) as uow:
                renewed = await uow.investigation_tasks.renew_lease(
                    task.id,
                    expected_status_version=running.status_version,
                    expected_lease_fence=running.lease_fence,
                    ttl_seconds=180,
                )
                assert renewed is not None
                assert renewed.status_version == running.status_version
                assert renewed.heartbeat_at is not None
                database_now = await uow.investigation_tasks.database_now()
                assert abs((database_now - renewed.heartbeat_at).total_seconds()) < 60
                stale = await uow.investigation_tasks.renew_lease(
                    task.id,
                    expected_status_version=running.status_version,
                    expected_lease_fence=running.lease_fence - 1,
                    ttl_seconds=180,
                )
                assert stale is None
                await uow.commit()

            async with factory() as session:
                await session.execute(
                    sa.text(
                        "UPDATE investigation_tasks SET lease_expires_at = "
                        "statement_timestamp() - interval '1 millisecond' WHERE id = :task_id"
                    ),
                    {"task_id": task.id},
                )
                await session.commit()

            async with SqlAlchemyUnitOfWork(factory) as uow:
                expired = await uow.investigation_tasks.renew_lease(
                    task.id,
                    expected_status_version=running.status_version,
                    expected_lease_fence=running.lease_fence,
                    ttl_seconds=180,
                )
                assert expired is None
                await uow.rollback()
        finally:
            await engine.dispose()

    asyncio.run(exercise())


def test_decision_check_definition_lists_task_values(
    temporary_postgres_url: str,
) -> None:
    config = _alembic_config(temporary_postgres_url)
    command.upgrade(config, "head")

    async def exercise() -> dict[str, str]:
        engine = create_postgres_engine(temporary_postgres_url)
        try:
            async with engine.connect() as connection:
                checks = await connection.run_sync(
                    lambda sync: {
                        str(check["name"]): str(check["sqltext"])
                        for check in sa.inspect(sync).get_check_constraints("analyst_decisions")
                    }
                )
                return checks
        finally:
            await engine.dispose()

    checks = asyncio.run(exercise())
    decision_values = set(re.findall(r"'([a-z_]+)'", checks["ck_analyst_decisions_type"]))
    target_values = set(re.findall(r"'([a-z_]+)'", checks["ck_analyst_decisions_target"]))
    assert decision_values == {item.value for item in AnalystDecisionType}
    assert target_values == {item.value for item in AnalystDecisionTargetType}
    assert {"plan_accept", "plan_reject", "task_result_accept"} <= decision_values
    assert {"plan_version", "task"} <= target_values


def test_tool_invocation_attribution_pair_check(temporary_postgres_url: str) -> None:
    """The cumulative 0023 pair: an agentic invocation carries task and session."""
    config = _alembic_config(temporary_postgres_url)
    command.upgrade(config, "head")

    async def exercise() -> None:
        engine = create_postgres_engine(temporary_postgres_url)
        try:
            factory = create_session_factory(engine)
            investigation = await _seed_investigation(factory)
            task = await _create_task(factory, investigation.id)
            insert = (
                "INSERT INTO tool_invocations "
                "(id, investigation_id, operation_key, tool_name, schema_version, "
                "normalized_parameters, purpose, status, reserved_budget, consumed_budget, "
                "response_refs, created_at, updated_at, version, task_id, actor_kind, "
                "actor_id, agent_session_id) "
                "VALUES (:id, :investigation_id, :operation_key, 'vt.file_report', "
                "'vt-file-report.v1', '{}'::jsonb, 'Attribution pair oracle', 'pending', "
                "'{}'::jsonb, '{}'::jsonb, '[]'::jsonb, :now, :now, 1, :task_id, "
                ":actor_kind, :actor_id, :agent_session_id)"
            )
            base: dict[str, object] = {
                "investigation_id": investigation.id,
                "now": NOW,
                "actor_kind": "operator",
                "actor_id": "operator:test",
            }
            await _expect_violation(
                factory,
                insert,
                {
                    **base,
                    "id": uuid4(),
                    "operation_key": f"task-only-{uuid4()}",
                    "task_id": task.id,
                    "agent_session_id": None,
                },
                sqlstate="23514",
            )
            await _expect_violation(
                factory,
                insert,
                {
                    **base,
                    "id": uuid4(),
                    "operation_key": f"session-only-{uuid4()}",
                    "task_id": None,
                    "agent_session_id": uuid4(),
                },
                sqlstate="23514",
            )
            await _expect_violation(
                factory,
                insert,
                {
                    **base,
                    "id": uuid4(),
                    "operation_key": f"unknown-session-{uuid4()}",
                    "task_id": task.id,
                    "agent_session_id": uuid4(),
                },
                sqlstate="23503",
            )
        finally:
            await engine.dispose()

    asyncio.run(exercise())

    async def check_definition() -> str:
        engine = create_postgres_engine(temporary_postgres_url)
        try:
            async with engine.connect() as connection:
                checks = await connection.run_sync(
                    lambda sync: sa.inspect(sync).get_check_constraints("tool_invocations")
                )
        finally:
            await engine.dispose()
        definitions = {
            check["name"]: str(check["sqltext"]) for check in checks if check.get("name")
        }
        return "".join(definitions["ck_tool_invocations_agent_session_pair"].lower().split())

    assert asyncio.run(check_definition()) == "(task_idisnull)=(agent_session_idisnull)"


def _contribution_row(
    *,
    investigation_id: UUID,
    workspace_id: str,
    revision: WorkspaceFileRevisionV1,
    file_relpath: str | None = None,
    status: WorkContributionImportStatus = WorkContributionImportStatus.SKIPPED,
    idempotency_key: str,
) -> WorkContributionV1:
    return WorkContributionV1(
        investigation_id=investigation_id,
        workspace_id=workspace_id,
        workspace_revision_id=revision.revision_id,
        file_relpath=file_relpath or revision.path,
        file_sha256=revision.sha256,
        observed_at=NOW,
        origin=WorkContributionOrigin.HOST,
        idempotency_key=idempotency_key,
        import_status=status,
        validation_error_code=(
            WorkContributionValidationCode.INTERNAL_WORKSPACE_PATH
            if status is WorkContributionImportStatus.SKIPPED
            else WorkContributionValidationCode.FORBIDDEN_FILE_TYPE
        ),
    )


def test_work_contributions_revision_fk_is_per_file(
    temporary_postgres_url: str,
) -> None:
    """The revision FK names the path, and one non-rejected import is unique."""
    config = _alembic_config(temporary_postgres_url)
    command.upgrade(config, CONTRIBUTIONS_REVISION)
    assert "work_contributions" in _table_names(temporary_postgres_url)

    async def exercise() -> None:
        engine = create_postgres_engine(temporary_postgres_url)
        try:
            factory = create_session_factory(engine)
            investigation = await _seed_investigation(factory)
            workspace_id = "ws-" + uuid4().hex
            binding = WorkspaceBindingV1(
                workspace_id=workspace_id,
                investigation_id=investigation.id,
                subject_id=investigation.subject_id,
                delivery_code="IR202611",
                article_slug="task-fixture",
                synthesis_sha256=SHA_A,
                created_at=NOW,
            )
            first = WorkspaceFileRevisionV1(
                workspace_id=workspace_id,
                revision_id=workspace_revision_id(
                    workspace_id=workspace_id, path="notes/humaines/a.md", sha256=SHA_A
                ),
                path="notes/humaines/a.md",
                sha256=SHA_A,
                size=4,
                mode="0640",
                mtime_ns=1,
                observed_at=NOW,
                origin="filesystem",
            )
            second = WorkspaceFileRevisionV1(
                workspace_id=workspace_id,
                revision_id=workspace_revision_id(
                    workspace_id=workspace_id, path="notes/humaines/b.md", sha256="b" * 64
                ),
                path="notes/humaines/b.md",
                sha256="b" * 64,
                size=4,
                mode="0640",
                mtime_ns=1,
                observed_at=NOW,
                origin="filesystem",
            )
            async with SqlAlchemyUnitOfWork(factory) as uow:
                await uow.workspace_bindings.add_if_absent(binding)
                await uow.workspace_file_revisions.append_if_absent(first)
                await uow.workspace_file_revisions.append_if_absent(second)
                await uow.commit()

            # Same (workspace, revision) with another file's path is not a revision.
            async with SqlAlchemyUnitOfWork(factory) as uow:
                with pytest.raises(DBAPIError) as exc_info:
                    await uow.work_contributions.append_if_absent(
                        _contribution_row(
                            investigation_id=investigation.id,
                            workspace_id=workspace_id,
                            revision=first,
                            file_relpath=second.path,
                            idempotency_key="revision-fk-mismatch-0001",
                        )
                    )
                assert _sqlstate(exc_info.value) == "23503"

            async with SqlAlchemyUnitOfWork(factory) as uow:
                stored, created = await uow.work_contributions.append_if_absent(
                    _contribution_row(
                        investigation_id=investigation.id,
                        workspace_id=workspace_id,
                        revision=first,
                        idempotency_key="revision-fk-ok-0001",
                    )
                )
                await uow.commit()
            assert created is True
            assert stored.file_relpath == first.path

            # One non-rejected import per revision: a new key returns the row.
            async with SqlAlchemyUnitOfWork(factory) as uow:
                replay, created = await uow.work_contributions.append_if_absent(
                    _contribution_row(
                        investigation_id=investigation.id,
                        workspace_id=workspace_id,
                        revision=first,
                        idempotency_key="revision-fk-ok-0002",
                    )
                )
            assert created is False
            assert replay.id == stored.id

            # A rejected row is terminal and never blocks a new attempt.
            async with SqlAlchemyUnitOfWork(factory) as uow:
                rejected, created = await uow.work_contributions.append_if_absent(
                    _contribution_row(
                        investigation_id=investigation.id,
                        workspace_id=workspace_id,
                        revision=first,
                        status=WorkContributionImportStatus.REJECTED,
                        idempotency_key="revision-fk-rejected-0001",
                    )
                )
                await uow.commit()
            assert created is True
            assert rejected.import_status is WorkContributionImportStatus.REJECTED
        finally:
            await engine.dispose()

    asyncio.run(exercise())


def test_work_contribution_migration_preflight_and_downgrade_guard(
    temporary_postgres_url: str,
) -> None:
    """0024 accepts its shape, refuses a partial one and never drops used rows."""
    config = _alembic_config(temporary_postgres_url)
    command.upgrade(config, "head")

    async def seed_row() -> None:
        engine = create_postgres_engine(temporary_postgres_url)
        try:
            factory = create_session_factory(engine)
            investigation = await _seed_investigation(factory)
            workspace_id = "ws-" + uuid4().hex
            binding = WorkspaceBindingV1(
                workspace_id=workspace_id,
                investigation_id=investigation.id,
                subject_id=investigation.subject_id,
                delivery_code="IR202611",
                article_slug="task-fixture",
                synthesis_sha256=SHA_A,
                created_at=NOW,
            )
            revision = WorkspaceFileRevisionV1(
                workspace_id=workspace_id,
                revision_id=workspace_revision_id(
                    workspace_id=workspace_id, path="notes/humaines/used.md", sha256=SHA_A
                ),
                path="notes/humaines/used.md",
                sha256=SHA_A,
                size=4,
                mode="0640",
                mtime_ns=1,
                observed_at=NOW,
                origin="filesystem",
            )
            async with SqlAlchemyUnitOfWork(factory) as uow:
                await uow.workspace_bindings.add_if_absent(binding)
                await uow.workspace_file_revisions.append_if_absent(revision)
                await uow.work_contributions.append_if_absent(
                    _contribution_row(
                        investigation_id=investigation.id,
                        workspace_id=workspace_id,
                        revision=revision,
                        idempotency_key="downgrade-guard-0001",
                    )
                )
                await uow.commit()
        finally:
            await engine.dispose()

    asyncio.run(seed_row())
    with pytest.raises(RuntimeError, match="refusing to downgrade work_contributions"):
        command.downgrade(config, "0023_agent_sessions")
    assert _alembic_version(temporary_postgres_url) == CONTRIBUTIONS_REVISION

    async def drop_guard_trigger() -> None:
        engine = create_postgres_engine(temporary_postgres_url)
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    sa.text("DROP TRIGGER trg_work_contributions_append_only ON work_contributions")
                )
        finally:
            await engine.dispose()

    asyncio.run(drop_guard_trigger())
    command.stamp(config, "0023_agent_sessions")
    with pytest.raises(RuntimeError, match="preflight rejected"):
        command.upgrade(config, "head")
    assert _alembic_version(temporary_postgres_url) == "0023_agent_sessions"
    assert "work_contributions" in _table_names(temporary_postgres_url)
