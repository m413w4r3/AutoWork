"""Small row snapshots for migration tests.

Schema snapshots live in :mod:`tests.support.schema_snapshot`; these helpers
capture row identities or complete rows for the cases where a migration must
preserve data or a refused downgrade must leave data untouched.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from sqlalchemy import MetaData, Table, func, inspect, select
from sqlalchemy.engine import Connection


def capture_primary_keys(
    connection: Connection, table_names: Iterable[str]
) -> dict[str, dict[str, Any]]:
    """Capture row counts and ordered primary-key values for named tables."""
    available = set(inspect(connection).get_table_names(schema="public"))
    snapshots: dict[str, dict[str, Any]] = {}
    for table_name in sorted(set(table_names)):
        if table_name not in available:
            raise ValueError(f"table {table_name!r} does not exist in public")
        table = Table(table_name, MetaData(), schema="public", autoload_with=connection)
        primary_key_columns = tuple(table.primary_key.columns)
        if not primary_key_columns:
            raise ValueError(f"table {table_name!r} has no primary key")
        row_count = connection.execute(select(func.count()).select_from(table)).scalar_one()
        rows = connection.execute(select(*primary_key_columns).order_by(*primary_key_columns)).all()
        snapshots[table_name] = {
            "row_count": int(row_count),
            "primary_key_columns": tuple(column.name for column in primary_key_columns),
            "primary_keys": [tuple(row) for row in rows],
        }
    return snapshots


def capture_table_rows(
    connection: Connection, table_names: Iterable[str]
) -> dict[str, list[dict[str, Any]]]:
    """Capture complete, primary-key-ordered rows from named public tables."""
    available = set(inspect(connection).get_table_names(schema="public"))
    snapshots: dict[str, list[dict[str, Any]]] = {}
    for table_name in sorted(set(table_names)):
        if table_name not in available:
            raise ValueError(f"table {table_name!r} does not exist in public")
        table = Table(table_name, MetaData(), schema="public", autoload_with=connection)
        primary_key_columns = tuple(table.primary_key.columns)
        if not primary_key_columns:
            raise ValueError(f"table {table_name!r} has no primary key")
        rows = connection.execute(select(table).order_by(*primary_key_columns)).mappings()
        snapshots[table_name] = [dict(row) for row in rows]
    return snapshots
