import asyncio
import os
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from cti_app.application.persistence import UnitOfWorkFactory
from cti_app.infrastructure.database.session import (
    create_postgres_engine,
    create_session_factory,
)
from cti_app.infrastructure.database.uow import SqlAlchemyUnitOfWork

BACKEND_ROOT = Path(__file__).resolve().parents[2]


def _alembic_config(database_url: str) -> Config:
    config = Config(BACKEND_ROOT / "alembic.ini")
    config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    return config


async def _create_database(admin_url: str, database_name: str) -> None:
    engine = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as connection:
            await connection.execute(text(f'CREATE DATABASE "{database_name}"'))
    finally:
        await engine.dispose()


async def _drop_database(admin_url: str, database_name: str) -> None:
    engine = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as connection:
            await connection.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = :database_name AND pid <> pg_backend_pid()"
                ),
                {"database_name": database_name},
            )
            await connection.execute(text(f'DROP DATABASE IF EXISTS "{database_name}"'))
    finally:
        await engine.dispose()


@pytest.fixture
def temporary_postgres_url() -> Iterator[str]:
    admin_url = os.getenv("TEST_POSTGRES_ADMIN_DSN")
    if not admin_url:
        pytest.skip("TEST_POSTGRES_ADMIN_DSN is required for PostgreSQL integration tests")
    database_name = f"cti_test_{uuid4().hex}"
    asyncio.run(_create_database(admin_url, database_name))
    database_url = (
        make_url(admin_url).set(database=database_name).render_as_string(hide_password=False)
    )
    try:
        yield database_url
    finally:
        asyncio.run(_drop_database(admin_url, database_name))


@pytest.fixture(scope="session")
def migrated_postgres_url() -> Iterator[str]:
    admin_url = os.getenv("TEST_POSTGRES_ADMIN_DSN")
    if not admin_url:
        pytest.skip("TEST_POSTGRES_ADMIN_DSN is required for PostgreSQL integration tests")
    database_name = f"cti_test_{uuid4().hex}"
    asyncio.run(_create_database(admin_url, database_name))
    database_url = (
        make_url(admin_url).set(database=database_name).render_as_string(hide_password=False)
    )
    command.upgrade(_alembic_config(database_url), "head")
    try:
        yield database_url
    finally:
        asyncio.run(_drop_database(admin_url, database_name))


@pytest.fixture
def uow_factory(migrated_postgres_url: str) -> UnitOfWorkFactory:
    """Postgres-backed UnitOfWork factory for production workflow tests."""
    engine = create_postgres_engine(migrated_postgres_url)
    session_factory = create_session_factory(engine)

    def factory() -> SqlAlchemyUnitOfWork:
        return SqlAlchemyUnitOfWork(session_factory)

    return factory
