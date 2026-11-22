"""PostgreSQL oracles for migration 0023 and the agent-session constraints.

Disposable databases only: the created tables/indexes, the partial unique
active-session index under a real race, the idempotency uniques, the tool
invocation attribution pair, the instruction checks, the task lease foreign
key, the ADR-0007 preflight and the non-empty downgrade guard.
"""

from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, date, datetime
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from alembic import command
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cti_app.application.investigation_handoffs import (
    HandoffCreateRequest,
    InvestigationHandoffService,
)
from cti_app.application.investigation_persistence import VersionConflictError
from cti_app.domain.agentic_operator import (
    AgentSessionState,
    AgentSessionV1,
    OperatorAdapterKind,
    OperatorCapabilitiesV1,
    OperatorProbeCommand,
)
from cti_app.domain.classification import TLP
from cti_app.domain.editions import Edition
from cti_app.domain.entities import Subject
from cti_app.domain.investigation_handoff import HandoffActor, HandoffActorKind
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
from cti_app.infrastructure.database.models.investigation import ToolInvocationRow
from cti_app.infrastructure.database.session import (
    create_postgres_engine,
    create_session_factory,
)
from cti_app.infrastructure.database.uow import SqlAlchemyUnitOfWork
from tests.integration.conftest import _alembic_config
from tests.integration.edition_codes import reserve_edition_code

pytestmark = pytest.mark.integration

PREVIOUS = "0022_investigation_handoffs"
HEAD = "0024_work_contributions"
SESSIONS = "agent_sessions"
INSTRUCTIONS = "agent_session_instructions"
NOW = datetime(2026, 11, 22, 12, 0, tzinfo=UTC)


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


async def _seed_investigation(
    session_factory: async_sessionmaker[AsyncSession],
) -> AnalystInvestigation:
    suffix = uuid4().hex
    edition = Edition(
        country=f"Agent session fixture {suffix}",
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
            title="Agent session fixture",
            slug=f"agent-session-fixture-{suffix}",
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


async def _seed_task(
    session_factory: async_sessionmaker[AsyncSession],
    investigation_id: UUID,
    *,
    task_key: str,
) -> InvestigationTaskV1:
    task = InvestigationTaskV1(
        investigation_id=investigation_id,
        task_key=task_key,
        title="Inspect the loader",
        question="Which network gate does the loader use?",
        objective="Identify the first-stage C2 gate.",
        priority=TaskPriority.HIGH,
        status=TaskStatus.READY,
        status_version=1,
        result_verification_status=ResultVerificationStatus.UNVERIFIED,
        created_by=TaskActor(kind=TaskActorKind.HUMAN, actor_id="analyst:test"),
        created_at=NOW,
        updated_at=NOW,
    )
    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        stored = await uow.investigation_tasks.add(task)
        await uow.commit()
    return stored


async def _seed_handoff(
    session_factory: async_sessionmaker[AsyncSession],
    investigation_id: UUID,
    task_id: UUID,
) -> UUID:
    service = InvestigationHandoffService(uow_factory=lambda: SqlAlchemyUnitOfWork(session_factory))
    record = await service.create(
        investigation_id,
        actor=HandoffActor(kind=HandoffActorKind.HUMAN, actor_id="analyst:test"),
        idempotency_key="session-fixture-handoff",
        request=HandoffCreateRequest(
            task_id=task_id,
            to_actor=HandoffActor(kind=HandoffActorKind.OPERATOR, actor_id="codex:operator-1"),
            next_action="Continue the analysis.",
        ),
    )
    return UUID(record.handoff.handoff_id)


def _capabilities() -> OperatorCapabilitiesV1:
    return OperatorCapabilitiesV1(
        adapter_kind=OperatorAdapterKind.CODEX_CLI,
        executable="/usr/local/bin/codex",
        version="codex-cli 1.2.3",
        modes=("exec",),
        supports_resume=True,
        supports_structured_output=True,
        supports_non_interactive=True,
        supports_stop=True,
        permission_modes=("workspace-write",),
        probed_at=NOW,
        probes=(
            OperatorProbeCommand(
                argv=("/usr/local/bin/codex", "--help"),
                exit_code=0,
                stdout_sha256="a" * 64,
                advertised_modes=("exec", "resume", "workspace-write"),
            ),
        ),
    )


def _session(
    investigation_id: UUID,
    task_id: UUID,
    handoff_id: UUID,
    *,
    idempotency_key: str,
    state: AgentSessionState = AgentSessionState.RUNNING,
) -> AgentSessionV1:
    session_id = uuid4()
    nonce = b"s" * 32
    capabilities = _capabilities()
    return AgentSessionV1(
        id=session_id,
        investigation_id=investigation_id,
        task_id=task_id,
        adapter_kind=OperatorAdapterKind.CODEX_CLI,
        state=state,
        state_version=2,
        capability_probe=capabilities.to_payload(),
        capability_sha256=capabilities.capability_sha256,
        probed_at=capabilities.probed_at,
        auth_secret_nonce=nonce,
        auth_secret_hash=hashlib.sha256(nonce).hexdigest(),
        auth_secret_issued_at=NOW,
        opened_at=NOW,
        idempotency_key=idempotency_key,
        created_at=NOW,
        updated_at=NOW,
        host_session_ref=f"host-{session_id}",
        handoff_id=handoff_id,
    )


def _invocation_values(
    investigation_id: UUID,
    *,
    task_id: UUID | None,
    agent_session_id: UUID | None,
) -> dict[str, object]:
    return {
        "id": uuid4(),
        "investigation_id": investigation_id,
        "operation_key": "op-" + uuid4().hex,
        "tool_name": "advisor.consult",
        "schema_version": "advisor-consult.v1",
        "normalized_parameters": {},
        "purpose": "attribution fixture",
        "task_id": task_id,
        "agent_session_id": agent_session_id,
        "actor_kind": "operator" if task_id is not None else None,
        "actor_id": "codex:operator-1" if task_id is not None else None,
        "status": "pending",
        "reserved_budget": {},
        "consumed_budget": {},
        "response_refs": [],
        "created_at": NOW,
        "updated_at": NOW,
        "version": 1,
    }


async def _insert_invocation(
    session_factory: async_sessionmaker[AsyncSession],
    values: dict[str, object],
) -> None:
    async with session_factory() as session:
        await session.execute(sa.insert(ToolInvocationRow).values(**values))
        await session.commit()


async def _sqlstate_for(statement: sa.Executable, params: dict[str, object], url: str) -> str:
    """Run one statement in its own transaction; return the SQLSTATE or 'ok'."""
    engine = create_postgres_engine(url)
    try:
        try:
            async with engine.begin() as connection:
                await connection.execute(statement, params)
        except DBAPIError as exc:
            return str(getattr(exc.orig, "sqlstate", ""))
        return "ok"
    finally:
        await engine.dispose()


async def _sqlstate_for_invocation(values: dict[str, object], url: str) -> str:
    """Insert one raw invocation row; the domain pair check is bypassed."""
    return await _sqlstate_for(sa.insert(ToolInvocationRow).values(**values), {}, url)


def test_agent_session_migration_and_single_active_race(
    temporary_postgres_url: str,
) -> None:
    config = _alembic_config(temporary_postgres_url)
    command.upgrade(config, "head")
    tables = _table_names(temporary_postgres_url)
    assert SESSIONS in tables and INSTRUCTIONS in tables
    assert _alembic_version(temporary_postgres_url) == HEAD

    async def run() -> None:
        engine = create_postgres_engine(temporary_postgres_url)
        try:
            factory = create_session_factory(engine)
            investigation = await _seed_investigation(factory)
            task = await _seed_task(factory, investigation.id, task_key="lead-race")
            handoff_id = await _seed_handoff(factory, investigation.id, task.id)
            first = _session(investigation.id, task.id, handoff_id, idempotency_key="race-a")
            second = _session(investigation.id, task.id, handoff_id, idempotency_key="race-b")

            async def insert(session_row: AgentSessionV1) -> object:
                async with SqlAlchemyUnitOfWork(factory) as uow:
                    stored, created = await uow.agent_sessions.add(session_row)
                    await uow.commit()
                    return stored, created

            outcomes = await asyncio.gather(insert(first), insert(second), return_exceptions=True)
            successes = [item for item in outcomes if not isinstance(item, BaseException)]
            failures = [item for item in outcomes if isinstance(item, VersionConflictError)]
            assert len(successes) == 1
            assert len(failures) == 1

            async with SqlAlchemyUnitOfWork(factory) as uow:
                same_key, created = await uow.agent_sessions.add(
                    _session(
                        investigation.id,
                        task.id,
                        handoff_id,
                        idempotency_key="race-a",
                    )
                )
                # The same key replays whichever row won key ``race-a``.
                assert created is False
                assert same_key.task_id == task.id
                sessions = await uow.agent_sessions.list_for_task(task.id)
                assert len(sessions) == 1
                assert sessions[0].state is AgentSessionState.RUNNING
                stale = sessions[0]
            async with engine.begin() as connection:
                await connection.execute(
                    sa.text(
                        "UPDATE agent_sessions SET state_version = state_version + 1 WHERE id = :id"
                    ),
                    {"id": stale.id},
                )
            async with SqlAlchemyUnitOfWork(factory) as uow:
                updated = stale.with_last_activity(now=NOW)
                conflict = await uow.agent_sessions.update_state(
                    updated, expected_state_version=stale.state_version
                )
                assert conflict is None
        finally:
            await engine.dispose()

    asyncio.run(run())

    with pytest.raises(RuntimeError, match="refusing to downgrade"):
        command.downgrade(config, PREVIOUS)
    assert _alembic_version(temporary_postgres_url) == HEAD


def test_tool_invocation_attribution_pair_and_lease_fk(
    temporary_postgres_url: str,
) -> None:
    config = _alembic_config(temporary_postgres_url)
    command.upgrade(config, "head")

    async def run() -> None:
        engine = create_postgres_engine(temporary_postgres_url)
        try:
            factory = create_session_factory(engine)
            investigation = await _seed_investigation(factory)
            task = await _seed_task(factory, investigation.id, task_key="lead-pair")
            handoff_id = await _seed_handoff(factory, investigation.id, task.id)
            session_row = _session(
                investigation.id, task.id, handoff_id, idempotency_key="pair-session"
            )
            async with SqlAlchemyUnitOfWork(factory) as uow:
                await uow.agent_sessions.add(session_row)
                await uow.commit()

            # A task-only invocation and a session-only invocation both violate
            # the cumulative attribution pair.
            assert (
                await _sqlstate_for_invocation(
                    _invocation_values(investigation.id, task_id=task.id, agent_session_id=None),
                    temporary_postgres_url,
                )
                == "23514"
            )
            assert (
                await _sqlstate_for_invocation(
                    _invocation_values(
                        investigation.id,
                        task_id=None,
                        agent_session_id=session_row.id,
                    ),
                    temporary_postgres_url,
                )
                == "23514"
            )

            # The full pair persists, and an unknown session id is an FK error.
            await _insert_invocation(
                factory,
                _invocation_values(
                    investigation.id,
                    task_id=task.id,
                    agent_session_id=session_row.id,
                ),
            )
            assert (
                await _sqlstate_for_invocation(
                    _invocation_values(investigation.id, task_id=task.id, agent_session_id=uuid4()),
                    temporary_postgres_url,
                )
                == "23503"
            )

            # A complete lease naming an unknown session is refused by the D2 FK
            # (setting only the session column would trip the pair check first).
            assert (
                await _sqlstate_for(
                    sa.text(
                        "UPDATE investigation_tasks SET lease_id = :lease_id, "
                        "lease_session_id = :session_id, lease_fence = 1, "
                        "heartbeat_at = :now, lease_expires_at = :later "
                        "WHERE id = :task_id"
                    ),
                    {
                        "lease_id": uuid4(),
                        "session_id": uuid4(),
                        "now": NOW,
                        "later": datetime(2026, 11, 22, 12, 5, tzinfo=UTC),
                        "task_id": task.id,
                    },
                    temporary_postgres_url,
                )
                == "23503"
            )
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_agent_session_instructions_unique_and_state_checks(
    temporary_postgres_url: str,
) -> None:
    config = _alembic_config(temporary_postgres_url)
    command.upgrade(config, "head")

    async def run() -> None:
        engine = create_postgres_engine(temporary_postgres_url)
        try:
            factory = create_session_factory(engine)
            investigation = await _seed_investigation(factory)
            task = await _seed_task(factory, investigation.id, task_key="lead-instr")
            handoff_id = await _seed_handoff(factory, investigation.id, task.id)
            session_row = _session(
                investigation.id, task.id, handoff_id, idempotency_key="instr-session"
            )
            async with SqlAlchemyUnitOfWork(factory) as uow:
                await uow.agent_sessions.add(session_row)
                await uow.commit()

            async def insert_instruction(**overrides: object) -> str:
                values: dict[str, object] = {
                    "id": uuid4(),
                    "investigation_id": investigation.id,
                    "task_id": task.id,
                    "session_id": session_row.id,
                    "idempotency_key": "instruction-key",
                    "body": "Check the gate.",
                    "body_sha256": hashlib.sha256(b"Check the gate.").hexdigest(),
                    "state": "queued",
                    "created_by_actor_kind": "human",
                    "created_by_actor_id": "analyst:test",
                    "queued_at": NOW,
                    "delivered_at": None,
                    "acknowledged_at": None,
                    "created_at": NOW,
                    "updated_at": NOW,
                }
                values.update(overrides)
                return await _sqlstate_for(
                    sa.text(
                        "INSERT INTO agent_session_instructions ("
                        "id, investigation_id, task_id, session_id, idempotency_key, "
                        "body, body_sha256, state, created_by_actor_kind, "
                        "created_by_actor_id, queued_at, delivered_at, "
                        "acknowledged_at, created_at, updated_at) VALUES ("
                        ":id, :investigation_id, :task_id, :session_id, "
                        ":idempotency_key, :body, :body_sha256, :state, "
                        ":created_by_actor_kind, :created_by_actor_id, :queued_at, "
                        ":delivered_at, :acknowledged_at, :created_at, :updated_at)"
                    ),
                    values,
                    temporary_postgres_url,
                )

            assert await insert_instruction() == "ok"
            assert await insert_instruction(id=uuid4()) == "23505"
            assert (
                await insert_instruction(
                    id=uuid4(),
                    idempotency_key="instruction-2",
                    state="acknowledged",
                )
                == "23514"
            )
            assert (
                await insert_instruction(
                    id=uuid4(),
                    idempotency_key="instruction-3",
                    session_id=uuid4(),
                )
                == "23503"
            )
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_agent_session_migration_accepts_conforming_and_rejects_partial(
    temporary_postgres_url: str,
) -> None:
    config = _alembic_config(temporary_postgres_url)
    command.upgrade(config, "head")
    command.stamp(config, PREVIOUS)
    command.upgrade(config, "head")
    assert _alembic_version(temporary_postgres_url) == HEAD
    assert SESSIONS in _table_names(temporary_postgres_url)

    async def drop_instructions() -> None:
        engine = create_postgres_engine(temporary_postgres_url)
        try:
            async with engine.begin() as connection:
                await connection.execute(sa.text(f"DROP TABLE {INSTRUCTIONS}"))
        finally:
            await engine.dispose()

    asyncio.run(drop_instructions())
    command.stamp(config, PREVIOUS)
    with pytest.raises(RuntimeError, match="preflight rejected"):
        command.upgrade(config, "head")
    assert _alembic_version(temporary_postgres_url) == PREVIOUS
