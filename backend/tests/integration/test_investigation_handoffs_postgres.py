"""PostgreSQL oracles for migration 0022 and the handoff append rules.

Disposable databases only: the created table and triggers, concurrent creates
of one task (same idempotency key => one row; two keys => versions 1 and 2),
append-only enforcement, the downgrade guard and the ADR-0007 preflight.
"""

from __future__ import annotations

import asyncio
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
from cti_app.infrastructure.database.session import (
    create_postgres_engine,
    create_session_factory,
)
from cti_app.infrastructure.database.uow import SqlAlchemyUnitOfWork
from tests.integration.alembic_head import current_head
from tests.integration.conftest import _alembic_config
from tests.integration.edition_codes import reserve_edition_code

pytestmark = pytest.mark.integration

PREVIOUS = "0021_investigation_tasks"
HEAD = current_head()
HANDOFF_TABLE = "investigation_handoffs"
NOW = datetime(2026, 11, 22, 12, 0, tzinfo=UTC)
HUMAN_ACTOR = HandoffActor(kind=HandoffActorKind.HUMAN, actor_id="analyst:test")
HANDOFF_RECIPIENT = HandoffActor(kind=HandoffActorKind.HUMAN, actor_id="analyst:second")


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
        country=f"Handoff fixture {suffix}",
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
            title="Handoff fixture",
            slug=f"handoff-fixture-{suffix}",
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


def _service(session_factory: async_sessionmaker[AsyncSession]) -> InvestigationHandoffService:
    return InvestigationHandoffService(
        uow_factory=lambda: SqlAlchemyUnitOfWork(session_factory),
        clock=lambda: NOW,
    )


async def _create(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    investigation_id: UUID,
    task_id: UUID,
    idempotency_key: str,
    next_action: str = "Continue the analysis.",
):
    return await _service(session_factory).create(
        investigation_id,
        actor=HUMAN_ACTOR,
        idempotency_key=idempotency_key,
        request=HandoffCreateRequest(
            task_id=task_id,
            to_actor=HANDOFF_RECIPIENT,
            next_action=next_action,
        ),
    )


def test_handoff_versions_are_append_only_and_idempotent(
    temporary_postgres_url: str,
) -> None:
    config = _alembic_config(temporary_postgres_url)
    command.upgrade(config, "head")
    assert HANDOFF_TABLE in _table_names(temporary_postgres_url)
    assert _alembic_version(temporary_postgres_url) == HEAD

    async def run() -> None:
        engine = create_postgres_engine(temporary_postgres_url)
        try:
            factory = create_session_factory(engine)
            investigation = await _seed_investigation(factory)
            same_key_task = await _seed_task(factory, investigation.id, task_key="lead-same-key")
            first, second = await asyncio.gather(
                _create(
                    factory,
                    investigation_id=investigation.id,
                    task_id=same_key_task.id,
                    idempotency_key="same-key",
                ),
                _create(
                    factory,
                    investigation_id=investigation.id,
                    task_id=same_key_task.id,
                    idempotency_key="same-key",
                ),
            )
            assert first.handoff.handoff_id == second.handoff.handoff_id
            assert first.handoff.version == second.handoff.version == 1

            versions_task = await _seed_task(factory, investigation.id, task_key="lead-two-keys")
            left, right = await asyncio.gather(
                _create(
                    factory,
                    investigation_id=investigation.id,
                    task_id=versions_task.id,
                    idempotency_key="key-a",
                    next_action="First concurrent branch.",
                ),
                _create(
                    factory,
                    investigation_id=investigation.id,
                    task_id=versions_task.id,
                    idempotency_key="key-b",
                    next_action="Second concurrent branch.",
                ),
            )
            assert {left.handoff.version, right.handoff.version} == {1, 2}

            async with SqlAlchemyUnitOfWork(factory) as uow:
                same_key_rows = await uow.investigation_handoffs.list_for_investigation(
                    investigation.id, task_id=same_key_task.id
                )
                version_rows = await uow.investigation_handoffs.list_for_investigation(
                    investigation.id, task_id=versions_task.id
                )
            assert len(same_key_rows) == 1
            assert [record.handoff.version for record in version_rows] == [1, 2]
            assert version_rows[1].predecessor_id == version_rows[0].handoff.handoff_id
        finally:
            await engine.dispose()

    asyncio.run(run())

    async def attempt(*statements: str) -> str:
        engine = create_postgres_engine(temporary_postgres_url)
        try:
            async with engine.begin() as connection:
                for statement in statements:
                    await connection.execute(sa.text(statement))
        except DBAPIError as exc:
            return str(getattr(exc.orig, "sqlstate", "")) or str(exc)
        finally:
            await engine.dispose()
        return "no error"

    assert asyncio.run(attempt(f"UPDATE {HANDOFF_TABLE} SET version = version + 1")) == "55000"
    assert asyncio.run(attempt(f"DELETE FROM {HANDOFF_TABLE}")) == "55000"
    # ``investigation_tasks.handoff_ref`` and ``agent_sessions.handoff_id``
    # make PostgreSQL refuse a bare TRUNCATE before the trigger is even
    # reached (0A000).  Drop both foreign keys inside the failing transaction
    # to exercise the installed BEFORE TRUNCATE guard itself; the rollback
    # restores them.
    assert (
        asyncio.run(
            attempt(
                "ALTER TABLE investigation_tasks DROP CONSTRAINT fk_investigation_tasks_handoff",
                "ALTER TABLE agent_sessions DROP CONSTRAINT fk_agent_sessions_handoff",
                f"TRUNCATE {HANDOFF_TABLE}",
            )
        )
        == "55000"
    )

    with pytest.raises(RuntimeError, match="refusing to downgrade"):
        command.downgrade(config, PREVIOUS)
    assert _alembic_version(temporary_postgres_url) == HEAD
    assert HANDOFF_TABLE in _table_names(temporary_postgres_url)


def test_handoff_migration_accepts_an_already_conforming_shape(
    temporary_postgres_url: str,
) -> None:
    config = _alembic_config(temporary_postgres_url)
    # Stop at this revision before stamping back: a later head (0027) replaces
    # constraints on agent_session_instructions, so 0023 is only re-runnable
    # against the shape it created itself.
    command.upgrade(config, "0022_investigation_handoffs")
    command.stamp(config, PREVIOUS)
    command.upgrade(config, "head")
    assert HANDOFF_TABLE in _table_names(temporary_postgres_url)
    assert _alembic_version(temporary_postgres_url) == HEAD


def test_handoff_migration_rejects_a_partial_shape(
    temporary_postgres_url: str,
) -> None:
    config = _alembic_config(temporary_postgres_url)
    command.upgrade(config, "head")
    command.stamp(config, PREVIOUS)

    async def drop_handoffs() -> None:
        engine = create_postgres_engine(temporary_postgres_url)
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    sa.text(
                        "ALTER TABLE investigation_tasks "
                        "DROP CONSTRAINT fk_investigation_tasks_handoff"
                    )
                )
                # 0023 adds this second reference to the handoff table.
                await connection.execute(
                    sa.text("ALTER TABLE agent_sessions DROP CONSTRAINT fk_agent_sessions_handoff")
                )
                await connection.execute(sa.text(f"DROP TABLE {HANDOFF_TABLE}"))
        finally:
            await engine.dispose()

    asyncio.run(drop_handoffs())
    with pytest.raises(RuntimeError, match="preflight rejected"):
        command.upgrade(config, "head")
    assert _alembic_version(temporary_postgres_url) == PREVIOUS
    assert HANDOFF_TABLE not in _table_names(temporary_postgres_url)
