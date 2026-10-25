import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import MetaData, pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from cti_app.infrastructure.database.models import (  # noqa: F401
    collection,
    core,
    discovery,
    edition_publication,
    edition_render,
    editions,
    editorial,
    invariants,
    investigation,
    jobs,
    media_assets,
    model_execution,
    osint_open_policy,
    production,
    publication_render,
    publication_review,
    selection,
    source_media,
)
from cti_app.infrastructure.database.models.base import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

_OSINT_POLICY_TABLES = frozenset(
    {
        "osint_open_policies",
        "osint_open_policy_events",
        "osint_open_inheritances",
    }
)
_OSINT_ENVELOPE_COLUMNS = frozenset(
    {
        "authority_kind",
        "inherited_policy_id",
        "inherited_policy_version",
        "inherited_policy_event_id",
    }
)


def _metadata_for_frozen_baseline() -> MetaData:
    """Keep additive OSINT_OPEN objects out of the metadata-driven 0001 baseline.

    The baseline intentionally predates OSINT_OPEN, but its frozen migration
    derives its table list from ``Base.metadata``. Present that migration with
    its pre-policy shape while retaining the complete metadata as Alembic's
    target for current schema inspection and later additive revisions.
    """
    metadata = MetaData()
    for source_table in target_metadata.tables.values():
        if source_table.name in _OSINT_POLICY_TABLES:
            continue
        table = source_table.to_metadata(metadata)
        if table.name != "investigation_envelopes":
            continue

        for constraint in tuple(table.constraints):
            if constraint.name == "ck_investigation_envelopes_authority_lineage":
                table.constraints.remove(constraint)
        for column_name in _OSINT_ENVELOPE_COLUMNS:
            if column_name in table.c:
                table._columns.remove(table.c[column_name])
    return metadata


def _run_migrations_with_frozen_baseline_metadata() -> None:
    original_metadata = Base.metadata
    Base.metadata = _metadata_for_frozen_baseline()
    try:
        context.run_migrations()
    finally:
        Base.metadata = original_metadata


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
        _run_migrations_with_frozen_baseline_metadata()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
    with context.begin_transaction():
        _run_migrations_with_frozen_baseline_metadata()


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
