"""Canonical, structural manifest of the `public` schema of a PostgreSQL database.

Used by [R07a] to prove that squashing the historical Alembic migration chain
into a single `0001_baseline` migration produces a byte-for-byte-equivalent
schema: tables, columns (order, name, PostgreSQL type, nullability, default,
identity/generated), constraints (PK/FK/UNIQUE/CHECK, via
`pg_get_constraintdef`), indexes (via `pg_get_indexdef`, partial predicates
included), sequences, functions (via `pg_get_functiondef`) and triggers (via
`pg_get_triggerdef`).

Deliberately excludes non-structural catalog noise: owners, ACLs, OIDs,
comments, and statistics.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

# ---------------------------------------------------------------------------
# Queries: every list is ordered deterministically in SQL so the resulting
# JSON manifest is stable across runs modulo the underlying schema itself.
# ---------------------------------------------------------------------------

_TABLES_SQL = """
    SELECT c.relname
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p')
    ORDER BY c.relname
"""

_COLUMNS_SQL = """
    SELECT
        c.relname AS table_name,
        a.attnum AS ordinal_position,
        a.attname AS column_name,
        pg_catalog.format_type(a.atttypid, a.atttypmod) AS data_type,
        NOT a.attnotnull AS is_nullable,
        pg_get_expr(ad.adbin, ad.adrelid) AS column_default,
        a.attidentity::text AS identity,
        a.attgenerated::text AS generated
    FROM pg_attribute a
    JOIN pg_class c ON c.oid = a.attrelid
    JOIN pg_namespace n ON n.oid = c.relnamespace
    LEFT JOIN pg_attrdef ad ON ad.adrelid = a.attrelid AND ad.adnum = a.attnum
    WHERE n.nspname = 'public'
      AND c.relkind IN ('r', 'p')
      AND a.attnum > 0
      AND NOT a.attisdropped
    ORDER BY c.relname, a.attnum
"""

_CONSTRAINTS_SQL = """
    SELECT
        c.relname AS table_name,
        con.conname AS constraint_name,
        con.contype::text AS constraint_type,
        pg_get_constraintdef(con.oid) AS definition
    FROM pg_constraint con
    JOIN pg_class c ON c.oid = con.conrelid
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = 'public'
    ORDER BY c.relname, con.conname
"""

_INDEXES_SQL = """
    SELECT
        t.relname AS table_name,
        i.relname AS index_name,
        pg_get_indexdef(i.oid) AS definition
    FROM pg_index ix
    JOIN pg_class i ON i.oid = ix.indexrelid
    JOIN pg_class t ON t.oid = ix.indrelid
    JOIN pg_namespace n ON n.oid = t.relnamespace
    WHERE n.nspname = 'public'
    ORDER BY t.relname, i.relname
"""

_SEQUENCES_SQL = """
    SELECT sequencename, data_type, start_value, min_value, max_value, increment_by, cycle
    FROM pg_sequences
    WHERE schemaname = 'public'
    ORDER BY sequencename
"""

_FUNCTIONS_SQL = """
    SELECT
        p.proname AS function_name,
        pg_get_function_identity_arguments(p.oid) AS identity_arguments,
        pg_get_functiondef(p.oid) AS definition
    FROM pg_proc p
    JOIN pg_namespace n ON n.oid = p.pronamespace
    WHERE n.nspname = 'public'
    ORDER BY p.proname, identity_arguments
"""

_TRIGGERS_SQL = """
    SELECT
        c.relname AS table_name,
        t.tgname AS trigger_name,
        pg_get_triggerdef(t.oid) AS definition,
        p.proname AS function_name
    FROM pg_trigger t
    JOIN pg_class c ON c.oid = t.tgrelid
    JOIN pg_proc p ON p.oid = t.tgfoid
    WHERE NOT t.tgisinternal
    ORDER BY c.relname, t.tgname
"""


async def capture_schema_catalog(database_url: str) -> dict[str, Any]:
    """Capture the canonical structural manifest of the `public` schema."""
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as connection:
            tables = [row[0] for row in await connection.execute(text(_TABLES_SQL))]

            columns: dict[str, list[dict[str, Any]]] = {name: [] for name in tables}
            for row in await connection.execute(text(_COLUMNS_SQL)):
                columns[row.table_name].append(
                    {
                        "position": row.ordinal_position,
                        "name": row.column_name,
                        "type": row.data_type,
                        "nullable": row.is_nullable,
                        "default": row.column_default,
                        "identity": row.identity,
                        "generated": row.generated,
                    }
                )

            constraints: dict[str, list[dict[str, Any]]] = {name: [] for name in tables}
            for row in await connection.execute(text(_CONSTRAINTS_SQL)):
                constraints[row.table_name].append(
                    {
                        "name": row.constraint_name,
                        "type": row.constraint_type,
                        "definition": row.definition,
                    }
                )

            indexes: dict[str, list[dict[str, Any]]] = {name: [] for name in tables}
            for row in await connection.execute(text(_INDEXES_SQL)):
                indexes[row.table_name].append(
                    {"name": row.index_name, "definition": row.definition}
                )

            sequences = [
                {
                    "name": row.sequencename,
                    "data_type": row.data_type,
                    "start_value": row.start_value,
                    "min_value": row.min_value,
                    "max_value": row.max_value,
                    "increment_by": row.increment_by,
                    "cycle": row.cycle,
                }
                for row in await connection.execute(text(_SEQUENCES_SQL))
            ]

            functions = [
                {
                    "name": row.function_name,
                    "identity_arguments": row.identity_arguments,
                    "definition": row.definition,
                }
                for row in await connection.execute(text(_FUNCTIONS_SQL))
            ]

            triggers = [
                {
                    "table": row.table_name,
                    "name": row.trigger_name,
                    "definition": row.definition,
                    "function": row.function_name,
                }
                for row in await connection.execute(text(_TRIGGERS_SQL))
            ]

            return {
                "tables": tables,
                "columns": columns,
                "constraints": constraints,
                "indexes": indexes,
                "sequences": sequences,
                "functions": functions,
                "triggers": triggers,
            }
    finally:
        await engine.dispose()
