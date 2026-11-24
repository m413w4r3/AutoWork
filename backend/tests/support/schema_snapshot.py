"""Capture a deterministic, JSON-serializable PostgreSQL schema snapshot.

The snapshot is restricted to objects in ``public`` (plus database-wide
extensions) and deliberately omits catalog OIDs, ACLs, and object owners. It
keeps schema names and catalog-rendered definitions unchanged: differences in
types, defaults, constraints, indexes, function bodies, triggers, or view SQL
are therefore visible. Catalog result order is normalized by stable names;
sequence current values and table data are not schema and are not captured.

``capture_schema`` accepts a synchronous SQLAlchemy connection, including the
connection supplied to ``AsyncConnection.run_sync``, or a PostgreSQL DSN. DSNs
are opened with the project's asyncpg driver and must be called outside an
active asyncio event loop; async callers should pass ``run_sync``'s connection.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, cast

from sqlalchemy import text
from sqlalchemy.engine import URL, Connection, make_url
from sqlalchemy.ext.asyncio import create_async_engine

Snapshot = dict[str, Any]

_TABLES_SQL = """
SELECT n.nspname AS schema, c.relname AS name, c.relkind::text AS kind,
       c.relpersistence::text AS persistence, c.relispartition AS is_partition,
       pg_get_expr(c.relpartbound, c.oid, false) AS partition_bound
FROM pg_class AS c
JOIN pg_namespace AS n ON n.oid = c.relnamespace
WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p')
ORDER BY n.nspname, c.relname
"""

_COLUMNS_SQL = """
SELECT n.nspname AS schema, c.relname AS table, a.attname AS name,
       format_type(a.atttypid, a.atttypmod) AS type,
       pg_get_expr(d.adbin, d.adrelid, false) AS default,
       NOT a.attnotnull AS nullable,
       a.attidentity::text AS identity, a.attgenerated::text AS generated,
       CASE WHEN a.attcollation = 0 THEN NULL
            ELSE format('%I.%I', cn.nspname, co.collname) END AS collation
FROM pg_attribute AS a
JOIN pg_class AS c ON c.oid = a.attrelid
JOIN pg_namespace AS n ON n.oid = c.relnamespace
LEFT JOIN pg_attrdef AS d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
LEFT JOIN pg_collation AS co ON co.oid = a.attcollation
LEFT JOIN pg_namespace AS cn ON cn.oid = co.collnamespace
WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p', 'v', 'm', 'f')
  AND a.attnum > 0 AND NOT a.attisdropped
ORDER BY n.nspname, c.relname, a.attnum
"""

_CONSTRAINTS_SQL = """
SELECT n.nspname AS schema, r.relname AS table, c.conname AS name,
       c.contype::text AS kind, pg_get_constraintdef(c.oid, false) AS definition,
       c.condeferrable AS deferrable, c.condeferred AS initially_deferred,
       c.convalidated AS validated, c.connoinherit AS no_inherit,
       c.conislocal AS is_local, c.coninhcount AS inheritance_count
FROM pg_constraint AS c
JOIN pg_class AS r ON r.oid = c.conrelid
JOIN pg_namespace AS n ON n.oid = r.relnamespace
WHERE n.nspname = 'public'
ORDER BY n.nspname, r.relname, c.conname, c.contype
"""

_INDEXES_SQL = """
SELECT n.nspname AS schema, t.relname AS table, i.relname AS name,
       pg_get_indexdef(x.indexrelid) AS definition,
       x.indisunique AS unique, x.indisprimary AS primary,
       x.indisexclusion AS exclusion, x.indisvalid AS valid,
       x.indisready AS ready
FROM pg_index AS x
JOIN pg_class AS i ON i.oid = x.indexrelid
JOIN pg_class AS t ON t.oid = x.indrelid
JOIN pg_namespace AS n ON n.oid = t.relnamespace
WHERE n.nspname = 'public'
ORDER BY n.nspname, t.relname, i.relname
"""

_TRIGGERS_SQL = """
SELECT n.nspname AS schema, r.relname AS table, t.tgname AS name,
       pg_get_triggerdef(t.oid, false) AS definition,
       t.tgenabled::text AS enabled
FROM pg_trigger AS t
JOIN pg_class AS r ON r.oid = t.tgrelid
JOIN pg_namespace AS n ON n.oid = r.relnamespace
WHERE n.nspname = 'public' AND NOT t.tgisinternal
ORDER BY n.nspname, r.relname, t.tgname
"""

_FUNCTIONS_SQL = """
SELECT n.nspname AS schema, p.proname AS name, p.prokind::text AS kind,
       pg_get_function_identity_arguments(p.oid) AS identity_arguments,
       pg_get_function_result(p.oid) AS result,
       CASE WHEN p.prokind = 'a' THEN NULL ELSE pg_get_functiondef(p.oid) END
           AS definition,
       l.lanname AS language, p.provolatile::text AS volatility,
       p.proisstrict AS strict, p.prosecdef AS security_definer,
       p.proleakproof AS leakproof, p.proparallel::text AS parallel,
       p.proconfig AS configuration,
       ag.aggkind::text AS aggregate_kind,
       ag.aggnumdirectargs AS aggregate_direct_arguments,
       ag.aggtransfn::regproc::text AS aggregate_transition_function,
       ag.aggfinalfn::regproc::text AS aggregate_final_function,
       ag.aggcombinefn::regproc::text AS aggregate_combine_function,
       ag.aggserialfn::regproc::text AS aggregate_serialize_function,
       ag.aggdeserialfn::regproc::text AS aggregate_deserialize_function,
       ag.aggmtransfn::regproc::text AS aggregate_moving_transition_function,
       ag.aggmfinalfn::regproc::text AS aggregate_moving_final_function,
       format_type(ag.aggtranstype, NULL) AS aggregate_transition_type,
       ag.aggtransspace AS aggregate_transition_space,
       format_type(ag.aggmtranstype, NULL) AS aggregate_moving_transition_type,
       ag.aggmtransspace AS aggregate_moving_transition_space,
       ag.aggfinalextra AS aggregate_final_extra,
       ag.aggfinalmodify::text AS aggregate_final_modify,
       ag.aggmfinalextra AS aggregate_moving_final_extra,
       ag.aggmfinalmodify::text AS aggregate_moving_final_modify,
       ag.agginitval AS aggregate_initial_value,
       ag.aggminitval AS aggregate_moving_initial_value,
       ag.aggsortop::regoperator::text AS aggregate_sort_operator
FROM pg_proc AS p
JOIN pg_namespace AS n ON n.oid = p.pronamespace
JOIN pg_language AS l ON l.oid = p.prolang
LEFT JOIN pg_aggregate AS ag ON ag.aggfnoid = p.oid
WHERE n.nspname = 'public'
ORDER BY n.nspname, p.proname, p.prokind,
         pg_get_function_identity_arguments(p.oid)
"""

_SEQUENCES_SQL = """
SELECT n.nspname AS schema, c.relname AS name,
       format_type(s.seqtypid, NULL) AS type,
       s.seqstart AS start, s.seqincrement AS increment,
       s.seqmin AS minimum, s.seqmax AS maximum,
       s.seqcache AS cache, s.seqcycle AS cycle,
       CASE WHEN owner_table.oid IS NULL THEN NULL
            ELSE format('%I.%I.%I', owner_schema.nspname,
                        owner_table.relname, owner_column.attname) END AS owned_by
FROM pg_class AS c
JOIN pg_namespace AS n ON n.oid = c.relnamespace
JOIN pg_sequence AS s ON s.seqrelid = c.oid
LEFT JOIN pg_depend AS dep
       ON dep.classid = 'pg_class'::regclass AND dep.objid = c.oid
      AND dep.refclassid = 'pg_class'::regclass AND dep.deptype IN ('a', 'i')
LEFT JOIN pg_class AS owner_table ON owner_table.oid = dep.refobjid
LEFT JOIN pg_namespace AS owner_schema ON owner_schema.oid = owner_table.relnamespace
LEFT JOIN pg_attribute AS owner_column
       ON owner_column.attrelid = dep.refobjid AND owner_column.attnum = dep.refobjsubid
WHERE n.nspname = 'public' AND c.relkind = 'S'
ORDER BY n.nspname, c.relname
"""

_ENUMS_SQL = """
SELECT n.nspname AS schema, t.typname AS name,
       array_agg(e.enumlabel ORDER BY e.enumsortorder) AS labels
FROM pg_type AS t
JOIN pg_namespace AS n ON n.oid = t.typnamespace
JOIN pg_enum AS e ON e.enumtypid = t.oid
WHERE n.nspname = 'public'
GROUP BY n.nspname, t.typname
ORDER BY n.nspname, t.typname
"""

_EXTENSIONS_SQL = """
SELECT e.extname AS name, e.extversion AS version,
       n.nspname AS schema, obj_description(e.oid, 'pg_extension') AS comment
FROM pg_extension AS e
JOIN pg_namespace AS n ON n.oid = e.extnamespace
ORDER BY e.extname
"""

_VIEWS_SQL = """
SELECT n.nspname AS schema, c.relname AS name, c.relkind::text AS kind,
       pg_get_viewdef(c.oid, false) AS definition,
       c.reloptions AS options
FROM pg_class AS c
JOIN pg_namespace AS n ON n.oid = c.relnamespace
WHERE n.nspname = 'public' AND c.relkind IN ('v', 'm')
ORDER BY n.nspname, c.relname
"""

_COMMENTS_SQL = """
SELECT 'schema' AS object_type, n.nspname AS object_schema,
       n.nspname AS object_name, NULL::text AS subobject, d.description AS comment
FROM pg_description AS d
JOIN pg_namespace AS n ON n.oid = d.objoid
WHERE d.classoid = 'pg_namespace'::regclass AND d.objsubid = 0
  AND n.nspname = 'public'
UNION ALL
SELECT 'relation', n.nspname, c.relname, NULL::text, d.description
FROM pg_description AS d
JOIN pg_class AS c ON c.oid = d.objoid
JOIN pg_namespace AS n ON n.oid = c.relnamespace
WHERE d.classoid = 'pg_class'::regclass AND d.objsubid = 0
  AND n.nspname = 'public'
UNION ALL
SELECT 'column', n.nspname, c.relname, a.attname, d.description
FROM pg_description AS d
JOIN pg_class AS c ON c.oid = d.objoid
JOIN pg_namespace AS n ON n.oid = c.relnamespace
JOIN pg_attribute AS a ON a.attrelid = c.oid AND a.attnum = d.objsubid
WHERE d.classoid = 'pg_class'::regclass AND d.objsubid > 0
  AND n.nspname = 'public'
UNION ALL
SELECT 'constraint', n.nspname, r.relname, c.conname, d.description
FROM pg_description AS d
JOIN pg_constraint AS c ON c.oid = d.objoid
JOIN pg_class AS r ON r.oid = c.conrelid
JOIN pg_namespace AS n ON n.oid = r.relnamespace
WHERE d.classoid = 'pg_constraint'::regclass AND n.nspname = 'public'
UNION ALL
SELECT 'function', n.nspname, p.proname,
       pg_get_function_identity_arguments(p.oid), d.description
FROM pg_description AS d
JOIN pg_proc AS p ON p.oid = d.objoid
JOIN pg_namespace AS n ON n.oid = p.pronamespace
WHERE d.classoid = 'pg_proc'::regclass AND n.nspname = 'public'
UNION ALL
SELECT 'type', n.nspname, t.typname, NULL::text, d.description
FROM pg_description AS d
JOIN pg_type AS t ON t.oid = d.objoid
JOIN pg_namespace AS n ON n.oid = t.typnamespace
WHERE d.classoid = 'pg_type'::regclass AND d.objsubid = 0
  AND n.nspname = 'public'
UNION ALL
SELECT 'trigger', n.nspname, r.relname, t.tgname, d.description
FROM pg_description AS d
JOIN pg_trigger AS t ON t.oid = d.objoid
JOIN pg_class AS r ON r.oid = t.tgrelid
JOIN pg_namespace AS n ON n.oid = r.relnamespace
WHERE d.classoid = 'pg_trigger'::regclass AND n.nspname = 'public'
ORDER BY object_type, object_schema, object_name, subobject
"""


def _query(connection: Connection, statement: str) -> list[dict[str, Any]]:
    return [dict(row) for row in connection.execute(text(statement)).mappings().all()]


def _capture_connection(connection: Connection) -> Snapshot:
    snapshot: Snapshot = {
        "normalization": {
            "scope": "public schema objects and database-wide extensions",
            "omitted": [
                "catalog OIDs (catalog references are rendered as names or definitions)",
                "object owners, roles, and ACLs",
                "temporary schemas (only the literal public schema is captured)",
                "table rows and sequence current values",
            ],
            "ordering": "catalog collections are sorted by schema and stable object names",
            "definitions": "functional SQL is preserved without whitespace or name rewriting",
        },
        "tables": _query(connection, _TABLES_SQL),
        "columns": _query(connection, _COLUMNS_SQL),
        "constraints": _query(connection, _CONSTRAINTS_SQL),
        "indexes": _query(connection, _INDEXES_SQL),
        "triggers": _query(connection, _TRIGGERS_SQL),
        "functions": _query(connection, _FUNCTIONS_SQL),
        "sequences": _query(connection, _SEQUENCES_SQL),
        "enums": _query(connection, _ENUMS_SQL),
        "extensions": _query(connection, _EXTENSIONS_SQL),
        "views": _query(connection, _VIEWS_SQL),
        "comments": _query(connection, _COMMENTS_SQL),
        "alembic_version": [],
    }
    has_alembic_version = connection.execute(
        text("SELECT to_regclass('public.alembic_version')")
    ).scalar_one()
    if has_alembic_version is not None:
        snapshot["alembic_version"] = _query(
            connection,
            "SELECT version_num FROM public.alembic_version ORDER BY version_num",
        )
    # Sorting every mapping level makes both the Python return value and JSON
    # serialization deterministic, including driver-returned mapping order.
    return cast(Snapshot, json.loads(json.dumps(snapshot, sort_keys=True)))


async def _capture_dsn(url: URL) -> Snapshot:
    async_url = url.set(drivername="postgresql+asyncpg")
    engine = create_async_engine(async_url)
    try:
        async with engine.connect() as connection:
            return await connection.run_sync(_capture_connection)
    finally:
        await engine.dispose()


def capture_schema(sync_connection_or_dsn: Connection | str | URL) -> Snapshot:
    """Return a deterministic PostgreSQL schema snapshot from a connection or DSN."""
    if isinstance(sync_connection_or_dsn, Connection):
        return _capture_connection(sync_connection_or_dsn)
    url = (
        sync_connection_or_dsn
        if isinstance(sync_connection_or_dsn, URL)
        else make_url(sync_connection_or_dsn)
    )
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(_capture_dsn(url))
    raise RuntimeError(
        "capture_schema(DSN) cannot run inside an active event loop; "
        "pass an AsyncConnection.run_sync connection instead"
    )


def _render(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _diff(path: str, left: Any, right: Any) -> list[str]:
    if isinstance(left, dict) and isinstance(right, dict):
        differences: list[str] = []
        keys = sorted(left.keys() | right.keys())
        for key in keys:
            child_path = f"{path}.{key}"
            if key not in left:
                differences.append(f"{child_path}: missing on left; right={_render(right[key])}")
            elif key not in right:
                differences.append(f"{child_path}: left={_render(left[key])}; missing on right")
            else:
                differences.extend(_diff(child_path, left[key], right[key]))
        return differences
    if isinstance(left, list) and isinstance(right, list):
        differences = []
        common_length = min(len(left), len(right))
        for index in range(common_length):
            differences.extend(_diff(f"{path}[{index}]", left[index], right[index]))
        for index in range(common_length, len(left)):
            differences.append(f"{path}[{index}]: left={_render(left[index])}; missing on right")
        for index in range(common_length, len(right)):
            differences.append(f"{path}[{index}]: missing on left; right={_render(right[index])}")
        return differences
    if left != right:
        return [f"{path}: left={_render(left)}; right={_render(right)}"]
    return []


def diff_snapshots(a: Snapshot, b: Snapshot) -> list[str]:
    """Return sorted, readable paths describing differences between snapshots."""
    return sorted(_diff("$", a, b))
