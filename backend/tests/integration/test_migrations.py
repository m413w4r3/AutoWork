import asyncio

import pytest
from alembic import command
from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import create_async_engine

from tests.integration.conftest import _alembic_config

pytestmark = pytest.mark.integration

EXPECTED_TABLES = {
    "alembic_version",
    "blobs",
    "subjects",
    "source_documents",
    "samples",
    "provenance_events",
    "jobs",
    "editions",
    "edition_audit_events",
    "job_events",
    "model_runs",
    "discovery_batches",
    "editorial_groups",
    "human_decisions",
    "source_collections",
    "collection_attempts",
    "derived_artifacts",
    "claims",
    "indicators",
    "collection_policy_snapshots",
    "rejected_model_proposals",
    "brief_evidence_packs",
    "brief_drafts",
}


async def _table_names(database_url: str) -> set[str]:
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as connection:
            return set(await connection.run_sync(lambda sync: inspect(sync).get_table_names()))
    finally:
        await engine.dispose()


def test_migration_up_and_down_on_temporary_postgres(temporary_postgres_url: str) -> None:
    config = _alembic_config(temporary_postgres_url)

    command.upgrade(config, "head")
    assert EXPECTED_TABLES <= asyncio.run(_table_names(temporary_postgres_url))

    command.downgrade(config, "base")
    assert asyncio.run(_table_names(temporary_postgres_url)) <= {"alembic_version"}

    command.upgrade(config, "head")
    assert EXPECTED_TABLES <= asyncio.run(_table_names(temporary_postgres_url))
