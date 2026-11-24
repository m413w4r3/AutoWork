import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from cti_app.infrastructure.database.models import (  # noqa: F401
    agent_sessions,
    bridge_generation_slots,
    collection,
    core,
    discovery,
    edition_composition,
    edition_publication,
    edition_render,
    editions,
    editorial,
    editorial_drafts,
    editorial_proposal_impacts,
    editorial_proposals,
    editorial_threads,
    editorial_workspace,
    external_effects,
    invariants,
    investigation,
    investigation_coordination,
    investigation_handoffs,
    investigation_tasks,
    jobs,
    media_assets,
    model_execution,
    opinion_note_proposals,
    osint_open_policy,
    production,
    production_routing,
    publication_render,
    publication_review,
    retrohunt,
    selection,
    source_media,
    work_contributions,
    workspace,
    workspace_note_annotations,
)
from cti_app.infrastructure.database.models.base import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
