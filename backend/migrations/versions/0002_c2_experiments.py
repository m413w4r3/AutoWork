"""Add C2 experiment storage and reconcile a prior manual installation.

Some installations were stamped at 0001 before the C2 objects were added
manually. This revision creates the objects when wholly absent, accepts them
only when their catalog shape matches the documented DDL, and refuses partial
or drifted installations before changing the schema.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

from alembic import op
from sqlalchemy import text

revision: str = "0002_c2_experiments"
down_revision: str | None = "0001_baseline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES = ("investigation_experiment_heads", "investigation_experiments")
_INDEX = "ix_investigation_experiments_claim_timeline"
_FUNCTION = "reject_investigation_experiment_mutation"
_TRIGGER = "trg_investigation_experiments_append_only"

# table -> column -> (information_schema data_type, character length, nullable)
_EXPECTED_COLUMNS: dict[str, dict[str, tuple[str, int | None, bool]]] = {
    "investigation_experiment_heads": {
        "investigation_id": ("uuid", None, False),
        "version": ("bigint", None, False),
        "updated_at": ("timestamp with time zone", None, False),
    },
    "investigation_experiments": {
        "id": ("uuid", None, False),
        "investigation_id": ("uuid", None, False),
        "version": ("bigint", None, False),
        "hypothesis_id": ("character varying", 128, False),
        "claim_ids": ("jsonb", None, False),
        "schema_version": ("character varying", 64, False),
        "payload": ("jsonb", None, False),
        "payload_sha256": ("character varying", 64, False),
        "recorded_at": ("timestamp with time zone", None, False),
    },
}

# name -> (table, pg_constraint.contype, ordered local columns, referenced table)
_EXPECTED_CONSTRAINTS: dict[str, tuple[str, str, tuple[str, ...], str | None]] = {
    "investigation_experiment_heads_pkey": (
        "investigation_experiment_heads",
        "p",
        ("investigation_id",),
        None,
    ),
    "ck_investigation_experiment_heads_version": (
        "investigation_experiment_heads",
        "c",
        ("version",),
        None,
    ),
    "investigation_experiment_heads_investigation_id_fkey": (
        "investigation_experiment_heads",
        "f",
        ("investigation_id",),
        "analyst_investigations",
    ),
    "investigation_experiments_pkey": ("investigation_experiments", "p", ("id",), None),
    "uq_investigation_experiments_version": (
        "investigation_experiments",
        "u",
        ("investigation_id", "version"),
        None,
    ),
    "investigation_experiments_investigation_id_fkey": (
        "investigation_experiments",
        "f",
        ("investigation_id",),
        "analyst_investigations",
    ),
    "ck_investigation_experiments_version": ("investigation_experiments", "c", ("version",), None),
    "ck_investigation_experiments_schema_version": (
        "investigation_experiments",
        "c",
        ("schema_version",),
        None,
    ),
    "ck_investigation_experiments_payload_sha256": (
        "investigation_experiments",
        "c",
        ("payload_sha256",),
        None,
    ),
    "ck_investigation_experiments_payload_identity": (
        "investigation_experiments",
        "c",
        ("payload", "schema_version", "id", "investigation_id", "version"),
        None,
    ),
    "ck_investigation_experiments_payload_shape": (
        "investigation_experiments",
        "c",
        ("payload", "claim_ids"),
        None,
    ),
}

# These exact token sequences are compared to PostgreSQL's deparsed check
# expressions after dropping its redundant parentheses and harmless text casts.
_EXPECTED_CHECK_TOKENS: dict[str, tuple[str, ...]] = {
    "ck_investigation_experiment_heads_version": ("version", ">=", "0"),
    "ck_investigation_experiments_version": ("version", ">=", "1"),
    "ck_investigation_experiments_schema_version": (
        "schema_version",
        "=",
        "'investigation-experiment.v1'",
    ),
    "ck_investigation_experiments_payload_sha256": ("payload_sha256", "~", "'^[0-9a-f]{64}$'"),
    "ck_investigation_experiments_payload_identity": (
        "payload",
        "->>",
        "'schema_version'",
        "=",
        "schema_version",
        "and",
        "payload",
        "->>",
        "'id'",
        "=",
        "id",
        "and",
        "payload",
        "->>",
        "'investigation_id'",
        "=",
        "investigation_id",
        "and",
        "payload",
        "->>",
        "'version'",
        "=",
        "version",
    ),
    "ck_investigation_experiments_payload_shape": (
        "jsonb_typeof",
        "payload",
        "=",
        "'object'",
        "and",
        "jsonb_typeof",
        "claim_ids",
        "=",
        "'array'",
    ),
}

# name -> (table, unique, primary, ordered key columns)
_EXPECTED_INDEXES: dict[str, tuple[str, bool, bool, tuple[str, ...]]] = {
    "investigation_experiment_heads_pkey": (
        "investigation_experiment_heads",
        True,
        True,
        ("investigation_id",),
    ),
    "investigation_experiments_pkey": ("investigation_experiments", True, True, ("id",)),
    "uq_investigation_experiments_version": (
        "investigation_experiments",
        True,
        False,
        ("investigation_id", "version"),
    ),
    _INDEX: (
        "investigation_experiments",
        False,
        False,
        ("investigation_id", "hypothesis_id", "version"),
    ),
}
_EXPECTED_INDEX_OPCLASSES: dict[str, tuple[str, ...]] = {
    "investigation_experiment_heads_pkey": ("uuid_ops",),
    "investigation_experiments_pkey": ("uuid_ops",),
    "uq_investigation_experiments_version": ("uuid_ops", "int8_ops"),
    _INDEX: ("uuid_ops", "text_ops", "int8_ops"),
}
_EXPECTED_INDEX_COLLATIONS: dict[str, tuple[str | None, ...]] = {
    "investigation_experiment_heads_pkey": (None,),
    "investigation_experiments_pkey": (None,),
    "uq_investigation_experiments_version": (None, None),
    _INDEX: (None, "pg_catalog.default", None),
}

_FUNCTION_BODY = """
BEGIN
    RAISE EXCEPTION 'investigation_experiments is append-only'
        USING ERRCODE = '55000';
END;
"""


def _tokens(value: str) -> tuple[str, ...]:
    value = re.sub(r"::(?:text|character varying)\b", "", value, flags=re.IGNORECASE)
    return tuple(
        re.findall(
            r"->>|>=|<=|<>|!=|=|~|[a-z_][a-z0-9_]*|\d+|'[^']*'",
            value.lower(),
        )
    )


def _presence(bind: Any) -> list[str]:
    rows = bind.execute(
        text(
            """
            SELECT 'relation:' || c.relname AS object_name
            FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'public' AND c.relname = ANY(:relation_names)
            UNION ALL
            SELECT 'constraint:' || c.conname
            FROM pg_constraint c JOIN pg_namespace n ON n.oid = c.connamespace
            WHERE n.nspname = 'public' AND c.conname = ANY(:constraint_names)
            UNION ALL
            SELECT 'function:' || p.proname
            FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
            WHERE n.nspname = 'public' AND p.proname = :function_name
            UNION ALL
            SELECT 'trigger:' || t.tgname
            FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'public' AND t.tgname = :trigger_name
            """
        ),
        {
            "relation_names": [*_TABLES, _INDEX],
            "constraint_names": list(_EXPECTED_CONSTRAINTS),
            "function_name": _FUNCTION,
            "trigger_name": _TRIGGER,
        },
    )
    return [str(row[0]) for row in rows]


def _read_shape(bind: Any) -> dict[str, Any]:
    columns = (
        bind.execute(
            text(
                """
            SELECT table_name, column_name, data_type, character_maximum_length,
                   is_nullable, column_default
            FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = ANY(:table_names)
            ORDER BY table_name, ordinal_position
            """
            ),
            {"table_names": list(_TABLES)},
        )
        .mappings()
        .all()
    )

    constraints = (
        bind.execute(
            text(
                """
            SELECT c.conname, c.contype::text AS contype, c.convalidated, c.condeferrable,
                   c.condeferred, c.conrelid::regclass::text AS table_name,
                   COALESCE((
                       SELECT array_agg(a.attname::text ORDER BY k.ordinality)
                       FROM unnest(c.conkey) WITH ORDINALITY AS k(attnum, ordinality)
                       JOIN pg_attribute a
                         ON a.attrelid = c.conrelid AND a.attnum = k.attnum
                   ), ARRAY[]::text[]) AS columns,
                   CASE WHEN c.contype = 'f' THEN confrel.relname END AS referred_table,
                   COALESCE((
                       SELECT array_agg(a.attname::text ORDER BY k.ordinality)
                       FROM unnest(c.confkey) WITH ORDINALITY AS k(attnum, ordinality)
                       JOIN pg_attribute a
                         ON a.attrelid = c.confrelid AND a.attnum = k.attnum
                   ), ARRAY[]::text[]) AS referred_columns,
                   c.confdeltype::text AS confdeltype,
                   c.confupdtype::text AS confupdtype,
                   c.confmatchtype::text AS confmatchtype,
                   pg_get_constraintdef(c.oid, true) AS definition
            FROM pg_constraint c
            LEFT JOIN pg_class confrel ON confrel.oid = c.confrelid
            WHERE c.connamespace = 'public'::regnamespace
              AND c.conrelid IN (
                  to_regclass('public.investigation_experiment_heads'),
                  to_regclass('public.investigation_experiments')
              )
            ORDER BY c.conname
            """
            )
        )
        .mappings()
        .all()
    )

    indexes = (
        bind.execute(
            text(
                """
            SELECT ic.relname AS index_name, tc.relname AS table_name,
                   i.indisunique, i.indisprimary, i.indisvalid, i.indisready,
                   i.indnkeyatts, i.indnatts, am.amname,
                   ARRAY(
                       SELECT option::integer
                       FROM unnest(i.indoption::smallint[]) WITH ORDINALITY
                            AS k(option, ordinality)
                       ORDER BY k.ordinality
                   ) AS sort_options,
                   ARRAY(
                       SELECT opc.opcname::text
                       FROM unnest(i.indclass::oid[]) WITH ORDINALITY
                            AS k(opclass_oid, ordinality)
                       JOIN pg_opclass opc ON opc.oid = k.opclass_oid
                       ORDER BY k.ordinality
                   ) AS opclasses,
                   ARRAY(
                       SELECT CASE WHEN k.collation_oid = 0 THEN NULL
                                   ELSE cn.nspname || '.' || coll.collname END
                       FROM unnest(i.indcollation::oid[]) WITH ORDINALITY
                            AS k(collation_oid, ordinality)
                       LEFT JOIN pg_collation coll ON coll.oid = k.collation_oid
                       LEFT JOIN pg_namespace cn ON cn.oid = coll.collnamespace
                       ORDER BY k.ordinality
                   ) AS collations,
                   ARRAY(
                       SELECT a.attname::text
                       FROM unnest(i.indkey::smallint[]) WITH ORDINALITY AS k(attnum, ordinality)
                       LEFT JOIN pg_attribute a
                         ON a.attrelid = i.indrelid AND a.attnum = k.attnum
                       ORDER BY k.ordinality
                   ) AS columns,
                   pg_get_expr(i.indpred, i.indrelid, true) AS predicate
            FROM pg_index i
            JOIN pg_class tc ON tc.oid = i.indrelid
            JOIN pg_namespace n ON n.oid = tc.relnamespace
            JOIN pg_class ic ON ic.oid = i.indexrelid
            JOIN pg_am am ON am.oid = ic.relam
            WHERE n.nspname = 'public' AND tc.relname = ANY(:table_names)
            ORDER BY ic.relname
            """
            ),
            {"table_names": list(_TABLES)},
        )
        .mappings()
        .all()
    )

    functions = (
        bind.execute(
            text(
                """
            SELECT p.proname, l.lanname, p.pronargs, p.prorettype::regtype::text AS return_type,
                   p.prosecdef, p.provolatile::text AS provolatile,
                   p.prokind::text AS prokind, p.prosrc
            FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
            JOIN pg_language l ON l.oid = p.prolang
            WHERE n.nspname = 'public' AND p.proname = :function_name
            ORDER BY p.oid
            """
            ),
            {"function_name": _FUNCTION},
        )
        .mappings()
        .all()
    )

    triggers = (
        bind.execute(
            text(
                """
            SELECT t.tgname, c.relname AS table_name, t.tgtype,
                   t.tgenabled::text AS tgenabled,
                   cardinality(t.tgattr::smallint[]) AS update_column_count,
                   t.tgqual IS NULL AS has_no_condition,
                   octet_length(t.tgargs) AS argument_bytes,
                   p.proname AS function_name, pn.nspname AS function_schema
            FROM pg_trigger t
            JOIN pg_class c ON c.oid = t.tgrelid
            JOIN pg_namespace n ON n.oid = c.relnamespace
            JOIN pg_proc p ON p.oid = t.tgfoid
            JOIN pg_namespace pn ON pn.oid = p.pronamespace
            WHERE n.nspname = 'public' AND c.relname = ANY(:table_names)
              AND NOT t.tgisinternal
            ORDER BY t.tgname
            """
            ),
            {"table_names": list(_TABLES)},
        )
        .mappings()
        .all()
    )

    relations = (
        bind.execute(
            text(
                """
            SELECT c.relname, c.relkind::text AS relkind
            FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'public' AND c.relname = ANY(:table_names)
            """
            ),
            {"table_names": list(_TABLES)},
        )
        .mappings()
        .all()
    )

    return {
        "columns": columns,
        "constraints": constraints,
        "indexes": indexes,
        "functions": functions,
        "triggers": triggers,
        "relations": relations,
    }


def _schema_errors(shape: Mapping[str, Any]) -> list[str]:
    errors: list[str] = []
    relations = {row["relname"]: row["relkind"] for row in shape["relations"]}
    for table_name in _TABLES:
        if relations.get(table_name) != "r":
            errors.append(f"public.{table_name}: expected an ordinary table")

    actual_columns: dict[str, dict[str, tuple[str, int | None, bool, Any]]] = {
        table_name: {} for table_name in _TABLES
    }
    for row in shape["columns"]:
        actual_columns[row["table_name"]][row["column_name"]] = (
            row["data_type"],
            row["character_maximum_length"],
            row["is_nullable"] == "YES",
            row["column_default"],
        )
    for table_name, expected in _EXPECTED_COLUMNS.items():
        actual = actual_columns.get(table_name, {})
        if set(actual) != set(expected):
            errors.append(
                f"public.{table_name}: expected columns {sorted(expected)}, got {sorted(actual)}"
            )
        for column_name, (data_type, max_length, nullable) in expected.items():
            actual_column = actual.get(column_name)
            if actual_column is None:
                continue
            got_type, got_length, got_nullable, got_default = actual_column
            if (got_type, got_length, got_nullable, got_default) != (
                data_type,
                max_length,
                nullable,
                None,
            ):
                errors.append(
                    f"public.{table_name}.{column_name}: expected "
                    f"{data_type}({max_length}) nullable={nullable} default=None, got "
                    f"{got_type}({got_length}) nullable={got_nullable} default={got_default!r}"
                )

    actual_constraints = {row["conname"]: row for row in shape["constraints"]}
    if set(actual_constraints) != set(_EXPECTED_CONSTRAINTS):
        errors.append(
            "C2 constraints: expected "
            f"{sorted(_EXPECTED_CONSTRAINTS)}, got {sorted(actual_constraints)}"
        )
    for name, (table_name, contype, columns, referred_table) in _EXPECTED_CONSTRAINTS.items():
        row = actual_constraints.get(name)
        if row is None:
            continue
        local_columns = tuple(row["columns"])
        if contype == "c":
            columns_match = set(local_columns) == set(columns)
        else:
            columns_match = local_columns == columns
        if (
            row["table_name"] != table_name
            or row["contype"] != contype
            or not columns_match
            or row["referred_table"] != referred_table
        ):
            errors.append(f"constraint {name}: table, type, columns or FK target differ")
        if not row["convalidated"] or row["condeferrable"] or row["condeferred"]:
            errors.append(f"constraint {name}: expected validated and non-deferrable")
        if contype == "f" and (
            row["confdeltype"] != "r"
            or row["confupdtype"] != "a"
            or row["confmatchtype"] != "s"
            or tuple(row["referred_columns"]) != ("id",)
        ):
            errors.append(
                f"constraint {name}: expected FK to analyst_investigations(id) ON DELETE RESTRICT"
            )
        if contype == "c":
            actual_tokens = _tokens(row["definition"])
            if actual_tokens[:1] == ("check",):
                actual_tokens = actual_tokens[1:]
            expected_tokens = _EXPECTED_CHECK_TOKENS[name]
            if actual_tokens != expected_tokens:
                errors.append(
                    f"constraint {name}: expected expression tokens {expected_tokens}, "
                    f"got {actual_tokens}"
                )

    actual_indexes = {row["index_name"]: row for row in shape["indexes"]}
    if set(actual_indexes) != set(_EXPECTED_INDEXES):
        errors.append(
            f"C2 indexes: expected {sorted(_EXPECTED_INDEXES)}, got {sorted(actual_indexes)}"
        )
    for name, (table_name, unique, primary, columns) in _EXPECTED_INDEXES.items():
        row = actual_indexes.get(name)
        if row is None:
            continue
        key_columns = tuple(row["columns"][: row["indnkeyatts"]])
        if (
            row["table_name"],
            row["indisunique"],
            row["indisprimary"],
            row["indisvalid"],
            row["indisready"],
            row["indnkeyatts"],
            row["indnatts"],
            row["amname"],
            tuple(row["sort_options"]),
            tuple(row["opclasses"]),
            tuple(row["collations"]),
            key_columns,
            row["predicate"],
        ) != (
            table_name,
            unique,
            primary,
            True,
            True,
            len(columns),
            len(columns),
            "btree",
            (0,) * len(columns),
            _EXPECTED_INDEX_OPCLASSES[name],
            _EXPECTED_INDEX_COLLATIONS[name],
            columns,
            None,
        ):
            errors.append(f"index {name}: access method, columns or flags differ")

    functions = shape["functions"]
    if len(functions) != 1:
        errors.append(
            f"public.{_FUNCTION}(): expected one zero-argument function, got {len(functions)}"
        )
    elif (
        functions[0]["lanname"] != "plpgsql"
        or functions[0]["pronargs"] != 0
        or functions[0]["return_type"] != "trigger"
        or functions[0]["prosecdef"]
        or functions[0]["provolatile"] != "v"
        or functions[0]["prokind"] != "f"
        or _tokens(functions[0]["prosrc"]) != _tokens(_FUNCTION_BODY)
    ):
        errors.append(
            f"public.{_FUNCTION}(): language, signature or body differs "
            f"(actual tokens={_tokens(functions[0]['prosrc'])}, "
            f"expected tokens={_tokens(_FUNCTION_BODY)})"
        )

    expected_triggers = {
        (_TRIGGER, "investigation_experiments", 27, "O", 0, True, 0, _FUNCTION, "public")
    }
    actual_triggers = {
        (
            row["tgname"],
            row["table_name"],
            row["tgtype"],
            row["tgenabled"],
            row["update_column_count"],
            row["has_no_condition"],
            row["argument_bytes"],
            row["function_name"],
            row["function_schema"],
        )
        for row in shape["triggers"]
    }
    if actual_triggers != expected_triggers:
        errors.append(
            f"C2 triggers: expected {sorted(expected_triggers)}, got {sorted(actual_triggers)}"
        )

    return errors


def _require_exact_existing_shape(bind: Any) -> None:
    errors = _schema_errors(_read_shape(bind))
    if errors:
        raise RuntimeError(
            "C2 migration preflight rejected a partial or non-conforming schema; "
            f"no schema changes were applied: {'; '.join(errors)}"
        )


def _create_c2_schema() -> None:
    op.execute(
        """
        CREATE TABLE investigation_experiment_heads (
            investigation_id uuid PRIMARY KEY
                REFERENCES analyst_investigations(id) ON DELETE RESTRICT,
            version bigint NOT NULL
                CONSTRAINT ck_investigation_experiment_heads_version CHECK (version >= 0),
            updated_at timestamptz NOT NULL
        )
        """
    )
    op.execute(
        """
        CREATE TABLE investigation_experiments (
            id uuid PRIMARY KEY,
            investigation_id uuid NOT NULL
                REFERENCES analyst_investigations(id) ON DELETE RESTRICT,
            version bigint NOT NULL
                CONSTRAINT ck_investigation_experiments_version CHECK (version >= 1),
            hypothesis_id varchar(128) NOT NULL,
            claim_ids jsonb NOT NULL,
            schema_version varchar(64) NOT NULL
                CONSTRAINT ck_investigation_experiments_schema_version
                    CHECK (schema_version = 'investigation-experiment.v1'),
            payload jsonb NOT NULL,
            payload_sha256 varchar(64) NOT NULL
                CONSTRAINT ck_investigation_experiments_payload_sha256
                    CHECK (payload_sha256 ~ '^[0-9a-f]{64}$'),
            recorded_at timestamptz NOT NULL,
            CONSTRAINT uq_investigation_experiments_version
                UNIQUE (investigation_id, version),
            CONSTRAINT ck_investigation_experiments_payload_identity CHECK (
                payload->>'schema_version' = schema_version
                AND payload->>'id' = id::text
                AND payload->>'investigation_id' = investigation_id::text
                AND payload->>'version' = version::text
            ),
            CONSTRAINT ck_investigation_experiments_payload_shape CHECK (
                jsonb_typeof(payload) = 'object'
                AND jsonb_typeof(claim_ids) = 'array'
            )
        )
        """
    )
    op.execute(
        """
        CREATE INDEX ix_investigation_experiments_claim_timeline
            ON investigation_experiments (investigation_id, hypothesis_id, version)
        """
    )
    op.execute(
        """
        CREATE FUNCTION reject_investigation_experiment_mutation() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'investigation_experiments is append-only'
                USING ERRCODE = '55000';
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_investigation_experiments_append_only
            BEFORE UPDATE OR DELETE ON investigation_experiments
            FOR EACH ROW EXECUTE FUNCTION reject_investigation_experiment_mutation()
        """
    )


def upgrade() -> None:
    bind = op.get_bind()
    if _presence(bind):
        _require_exact_existing_shape(bind)
        return
    _create_c2_schema()


def downgrade() -> None:
    bind = op.get_bind()
    for table_name in _TABLES:
        exists = bind.execute(
            text("SELECT to_regclass(:relation_name) IS NOT NULL"),
            {"relation_name": f"public.{table_name}"},
        ).scalar_one()
        if not exists:
            raise RuntimeError(
                f"refusing to downgrade C2 experiment storage: public.{table_name} is missing"
            )
    experiments = int(
        bind.execute(text("SELECT count(*) FROM public.investigation_experiments")).scalar_one()
    )
    heads = int(
        bind.execute(
            text("SELECT count(*) FROM public.investigation_experiment_heads")
        ).scalar_one()
    )
    if experiments or heads:
        raise RuntimeError(
            "refusing to downgrade C2 experiment storage while experiment or head rows exist; "
            "preserve the history and use a non-destructive recovery plan"
        )
    _require_exact_existing_shape(bind)

    op.execute(
        "DROP TRIGGER trg_investigation_experiments_append_only ON public.investigation_experiments"
    )
    op.execute("DROP TABLE public.investigation_experiments")
    op.execute("DROP TABLE public.investigation_experiment_heads")
    op.execute("DROP FUNCTION public.reject_investigation_experiment_mutation()")
