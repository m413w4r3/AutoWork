"""PostgreSQL oracles for migration 0019 application and downgrade guards.

These tests use disposable databases only: fresh upgrade, empty downgrade,
conforming re-application after a manual stamp, partial-state refusal and the
row guard that refuses to drop used workspace tables (ADR-0007 discipline).
"""

from __future__ import annotations

import asyncio

import pytest
import sqlalchemy as sa
from alembic import command

from cti_app.infrastructure.database.session import (
    create_postgres_engine,
    create_session_factory,
)
from cti_app.infrastructure.database.uow import SqlAlchemyUnitOfWork
from tests.integration.conftest import _alembic_config
from tests.integration.test_workspace_agent_persistence_postgres import (
    binding_for,
    make_investigation,
)

pytestmark = pytest.mark.integration

HEAD = "0019_workspace_bindings"
PREVIOUS = "0018_editorial_enrichment_element_operations"
WORKSPACE_TABLES = {
    "workspace_bindings",
    "workspace_file_revisions",
    "workspace_file_events",
    "workspace_sync_cursors",
}


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


async def _insert_bound_workspace(database_url: str) -> None:
    engine = create_postgres_engine(database_url)
    try:
        factory = create_session_factory(engine)
        investigation = await make_investigation(factory)
        async with SqlAlchemyUnitOfWork(factory) as uow:
            _, created = await uow.workspace_bindings.add_if_absent(binding_for(investigation))
            assert created is True
            await uow.commit()
    finally:
        await engine.dispose()


def test_workspace_migration_creates_tables_and_refuses_used_downgrade(
    temporary_postgres_url: str,
) -> None:
    config = _alembic_config(temporary_postgres_url)
    command.upgrade(config, "head")
    assert WORKSPACE_TABLES <= _table_names(temporary_postgres_url)
    assert _alembic_version(temporary_postgres_url) == HEAD

    command.downgrade(config, PREVIOUS)
    assert not WORKSPACE_TABLES & _table_names(temporary_postgres_url)
    assert _alembic_version(temporary_postgres_url) == PREVIOUS

    command.upgrade(config, "head")
    asyncio.run(_insert_bound_workspace(temporary_postgres_url))
    with pytest.raises(RuntimeError, match="refusing to downgrade"):
        command.downgrade(config, PREVIOUS)
    assert _alembic_version(temporary_postgres_url) == HEAD
    assert WORKSPACE_TABLES <= _table_names(temporary_postgres_url)


def test_workspace_migration_accepts_an_already_conforming_shape(
    temporary_postgres_url: str,
) -> None:
    config = _alembic_config(temporary_postgres_url)
    command.upgrade(config, "head")
    command.stamp(config, PREVIOUS)
    command.upgrade(config, "head")
    assert WORKSPACE_TABLES <= _table_names(temporary_postgres_url)
    assert _alembic_version(temporary_postgres_url) == HEAD


def test_workspace_migration_rejects_a_partial_workspace_shape(
    temporary_postgres_url: str,
) -> None:
    config = _alembic_config(temporary_postgres_url)
    # The widened alembic_version column arrives with the chain itself, so the
    # partial state is built from a complete head and then stamped back.
    command.upgrade(config, "head")
    command.stamp(config, PREVIOUS)

    async def drop_from_dependents() -> None:
        engine = create_postgres_engine(temporary_postgres_url)
        try:
            async with engine.begin() as connection:
                for table_name in (
                    "workspace_sync_cursors",
                    "workspace_file_events",
                    "workspace_file_revisions",
                ):
                    await connection.execute(sa.text(f"DROP TABLE {table_name}"))
        finally:
            await engine.dispose()

    asyncio.run(drop_from_dependents())
    with pytest.raises(RuntimeError, match="preflight rejected"):
        command.upgrade(config, "head")
    assert _alembic_version(temporary_postgres_url) == PREVIOUS
    remaining = WORKSPACE_TABLES - {"workspace_bindings"}
    assert remaining.isdisjoint(_table_names(temporary_postgres_url))
    assert "workspace_bindings" in _table_names(temporary_postgres_url)
