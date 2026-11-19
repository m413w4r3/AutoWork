"""Add the fenced global slot for editorial Bridge generations.

Revision ID: 0013_bridge_generation_slots
Revises: 0012_editorial_threads
"""

import re
from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op

revision: str = "0013_bridge_generation_slots"
down_revision: str | None = "0012_editorial_threads"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "bridge_generation_slots"
_FUNCTION = "reject_bridge_generation_slot_removal"
_DELETE_TRIGGER = "trg_bridge_generation_slots_no_delete"
_TRUNCATE_TRIGGER = "trg_bridge_generation_slots_no_truncate"
_EXPECTED_COLUMNS = (
    ("singleton_key", "smallint", True, None),
    ("holder_thread_id", "uuid", False, None),
    ("holder_job_id", "uuid", False, None),
    ("holder_turn_id", "uuid", False, None),
    ("lease_token", "bigint", True, "0"),
    ("acquired_at", "timestamp with time zone", False, None),
    ("lease_expires_at", "timestamp with time zone", False, None),
    ("state", "character varying(16)", True, "'free'"),
)
_EXPECTED_CONSTRAINTS = {
    "bridge_generation_slots_pkey": ("p", ("singleton_key",), None),
    "ck_bridge_generation_slots_holder_state": (
        "c",
        (
            "state",
            "holder_thread_id",
            "holder_job_id",
            "holder_turn_id",
            "acquired_at",
            "lease_expires_at",
        ),
        None,
    ),
    "ck_bridge_generation_slots_singleton": ("c", ("singleton_key",), None),
    "ck_bridge_generation_slots_state": ("c", ("state",), None),
    "ck_bridge_generation_slots_token": ("c", ("lease_token",), None),
    "fk_bridge_generation_slots_job": ("f", ("holder_job_id",), "editorial_thread_jobs"),
    "fk_bridge_generation_slots_thread": (
        "f",
        ("holder_thread_id",),
        "editorial_threads",
    ),
    "fk_bridge_generation_slots_turn": ("f", ("holder_turn_id",), "model_conversation_turns"),
}
_EXPECTED_CHECK_EXPRESSIONS = {
    "ck_bridge_generation_slots_holder_state": (
        "(state = 'free' AND holder_thread_id IS NULL AND holder_job_id IS NULL "
        "AND holder_turn_id IS NULL AND acquired_at IS NULL AND lease_expires_at IS NULL) "
        "OR (state IN ('held', 'held_unknown') AND holder_thread_id IS NOT NULL "
        "AND holder_job_id IS NOT NULL AND acquired_at IS NOT NULL "
        "AND lease_expires_at IS NOT NULL)"
    ),
    "ck_bridge_generation_slots_singleton": "singleton_key = 1",
    "ck_bridge_generation_slots_state": "state IN ('free', 'held', 'held_unknown')",
    "ck_bridge_generation_slots_token": "lease_token >= 0",
}
_EXPECTED_TRIGGER_TYPES = {
    _DELETE_TRIGGER: 1 | 2 | 8,  # FOR EACH ROW, BEFORE, DELETE
    _TRUNCATE_TRIGGER: 2 | 32,  # FOR EACH STATEMENT, BEFORE, TRUNCATE
}

_SQL_TOKEN = re.compile(
    r"'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"|::|>=|<=|<>|!=|"
    r"[A-Za-z_][A-Za-z0-9_$]*|[0-9]+|[^\s]"
)
_CAST_TYPES = (
    ("character", "varying", "[", "]"),
    ("text", "[", "]"),
    ("character", "varying"),
    ("text",),
)


def _previous_operand(tokens: list[str], end: int) -> list[str]:
    if end == 0:
        return []
    last = end - 1
    if tokens[last] == ")":
        depth = 0
        for index in range(last, -1, -1):
            if tokens[index] == ")":
                depth += 1
            elif tokens[index] == "(":
                depth -= 1
                if depth == 0:
                    return tokens[index + 1 : last]
    elif tokens[last] == "]":
        depth = 0
        for index in range(last, -1, -1):
            if tokens[index] == "]":
                depth += 1
            elif tokens[index] == "[":
                depth -= 1
                if depth == 0:
                    start = index - 1 if index and tokens[index - 1].lower() == "array" else index
                    return tokens[start : last + 1]
    return [tokens[last]]


def _redundant_cast(tokens: list[str], index: int, cast: tuple[str, ...]) -> bool:
    operand = _previous_operand(tokens, index)
    is_string_literal = len(operand) == 1 and operand[0].startswith("'")
    normalized_operand = [token.lower() for token in operand]
    is_state_column = normalized_operand == ["state"]
    is_array = bool(normalized_operand and normalized_operand[0] == "array")
    if cast in (("character", "varying"), ("text",)):
        return is_string_literal or is_state_column
    return cast in (("character", "varying", "[", "]"), ("text", "[", "]")) and is_array


def _sql_tokens(value: str) -> list[str]:
    raw = _SQL_TOKEN.findall(value)
    tokens: list[str] = []
    index = 0
    while index < len(raw):
        token = raw[index]
        if token == "::":
            following = tuple(part.lower() for part in raw[index + 1 :])
            cast = next(
                (
                    candidate
                    for candidate in _CAST_TYPES
                    if following[: len(candidate)] == candidate
                ),
                None,
            )
            if cast is not None and _redundant_cast(raw, index, cast):
                index += len(cast) + 1
                continue
        if token.startswith("'") or token.startswith('"'):
            tokens.append(token)
        else:
            tokens.append(token.lower())
        index += 1
    return tokens


def _matching_close(tokens: list[str], start: int, opening: str, closing: str) -> int | None:
    depth = 0
    for index in range(start, len(tokens)):
        if tokens[index] == opening:
            depth += 1
        elif tokens[index] == closing:
            depth -= 1
            if depth == 0:
                return index
    return None


def _unwrap(tokens: list[str]) -> list[str]:
    while tokens and tokens[0] == "(":
        close = _matching_close(tokens, 0, "(", ")")
        if close != len(tokens) - 1:
            break
        tokens = tokens[1:-1]
    return tokens


def _normalize_any_arrays(tokens: list[str]) -> list[str]:
    normalized: list[str] = []
    index = 0
    while index < len(tokens):
        if tokens[index : index + 3] == ["=", "any", "("]:
            close = _matching_close(tokens, index + 2, "(", ")")
            if close is not None:
                array = _unwrap(tokens[index + 3 : close])
                if array[:2] == ["array", "["]:
                    array_close = _matching_close(array, 1, "[", "]")
                    if array_close == len(array) - 1:
                        normalized.extend(["in", "(", *array[2:array_close], ")"])
                        index = close + 1
                        continue
        normalized.append(tokens[index])
        index += 1
    return normalized


def _split_top_level(tokens: list[str], operator: str) -> list[list[str]]:
    parts: list[list[str]] = []
    start = 0
    depth = 0
    bracket_depth = 0
    for index, token in enumerate(tokens):
        if token == "(":
            depth += 1
        elif token == ")":
            depth -= 1
        elif token == "[":
            bracket_depth += 1
        elif token == "]":
            bracket_depth -= 1
        elif token == operator and depth == 0 and bracket_depth == 0:
            parts.append(tokens[start:index])
            start = index + 1
    if parts:
        parts.append(tokens[start:])
    return parts


def _canonical_expression(tokens: list[str]) -> str:
    tokens = _unwrap(_normalize_any_arrays(tokens))
    for operator in ("or", "and"):
        parts = _split_top_level(tokens, operator)
        if parts:
            return (
                operator.upper()
                + "("
                + ",".join(_canonical_expression(part) for part in parts)
                + ")"
            )

    # PostgreSQL adds parentheses around individual predicates and casts
    # varchar checks through text/character varying. Casts are removed by
    # _sql_tokens; remove only redundant wrappers around a single operand.
    while True:
        changed = False
        for index, token in enumerate(tokens):
            if token != "(":
                continue
            close = _matching_close(tokens, index, "(", ")")
            if close == index + 2 and close < len(tokens):
                tokens = [*tokens[:index], tokens[index + 1], *tokens[close + 1 :]]
                changed = True
                break
        if not changed:
            break
    return "".join(tokens)


def _normalize_default(value: str | None) -> str | None:
    return None if value is None else _canonical_expression(_sql_tokens(value))


def _normalize_constraint_definition(value: str) -> str:
    tokens = _sql_tokens(value)
    if tokens and tokens[0] == "check" and len(tokens) > 1 and tokens[1] == "(":
        close = _matching_close(tokens, 1, "(", ")")
        if close == len(tokens) - 1:
            return "CHECK:" + _canonical_expression(tokens[2:close])
    return "".join(tokens)


def _tokens(value: str | None) -> tuple[str, ...] | None:
    if value is None:
        return None
    return tuple(_sql_tokens(value))


def _read_shape(bind: Any, *, schema: str) -> dict[str, Any]:
    relation = (
        bind.execute(
            sa.text(
                "SELECT c.relkind::text AS relkind, c.relpersistence::text AS relpersistence "
                "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = :schema AND c.relname = :table_name"
            ),
            {"schema": schema, "table_name": _TABLE},
        )
        .mappings()
        .one_or_none()
    )
    table_type_exists = bool(
        bind.execute(
            sa.text(
                "SELECT EXISTS (SELECT 1 FROM pg_type t JOIN pg_namespace n "
                "ON n.oid = t.typnamespace WHERE n.nspname = :schema AND t.typname = :name)"
            ),
            {"schema": schema, "name": _TABLE},
        ).scalar_one()
    )

    columns = (
        bind.execute(
            sa.text(
                "SELECT a.attname AS column_name, "
                "format_type(a.atttypid, a.atttypmod) AS column_type, "
                "a.attnotnull, pg_get_expr(d.adbin, d.adrelid, true) AS default_expression "
                "FROM pg_attribute a LEFT JOIN pg_attrdef d "
                "ON d.adrelid = a.attrelid AND d.adnum = a.attnum "
                "WHERE a.attrelid = (SELECT c.oid FROM pg_class c "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = :schema AND c.relname = :table_name) "
                "AND a.attnum > 0 "
                "AND NOT a.attisdropped ORDER BY a.attnum"
            ),
            {"schema": schema, "table_name": _TABLE},
        )
        .mappings()
        .all()
    )

    constraints = (
        bind.execute(
            sa.text(
                "SELECT con.conname, con.contype::text AS contype, con.convalidated, "
                "con.condeferrable, con.condeferred, table_namespace.nspname AS table_schema, "
                "table_relation.relname AS table_name, "
                "ARRAY(SELECT attribute.attname::text FROM unnest(con.conkey) "
                "WITH ORDINALITY AS key_column(attnum, ordinality) "
                "JOIN pg_attribute attribute ON attribute.attrelid = con.conrelid "
                "AND attribute.attnum = key_column.attnum "
                "ORDER BY key_column.ordinality) AS columns, "
                "referenced_namespace.nspname AS referenced_schema, "
                "referenced_relation.relname AS referenced_table, "
                "ARRAY(SELECT attribute.attname::text FROM unnest(con.confkey) "
                "WITH ORDINALITY AS key_column(attnum, ordinality) "
                "JOIN pg_attribute attribute ON attribute.attrelid = con.confrelid "
                "AND attribute.attnum = key_column.attnum ORDER BY key_column.ordinality) "
                "AS referenced_columns, con.confdeltype::text AS confdeltype, "
                "con.confupdtype::text AS confupdtype, con.confmatchtype::text AS confmatchtype, "
                "pg_get_constraintdef(con.oid, true) AS definition "
                "FROM pg_constraint con JOIN pg_class table_relation "
                "ON table_relation.oid = con.conrelid "
                "JOIN pg_namespace table_namespace "
                "ON table_namespace.oid = table_relation.relnamespace "
                "LEFT JOIN pg_class referenced_relation ON referenced_relation.oid = con.confrelid "
                "LEFT JOIN pg_namespace referenced_namespace "
                "ON referenced_namespace.oid = referenced_relation.relnamespace "
                "WHERE table_namespace.nspname = :schema AND table_relation.relname = :table_name "
                "ORDER BY con.conname"
            ),
            {"schema": schema, "table_name": _TABLE},
        )
        .mappings()
        .all()
    )

    indexes = (
        bind.execute(
            sa.text(
                "SELECT index_relation.relname AS index_name, "
                "table_relation.relname AS table_name, "
                "index_data.indisunique, index_data.indisprimary, index_data.indisvalid, "
                "index_data.indisready, index_data.indnkeyatts, index_data.indnatts, "
                "access_method.amname, ARRAY(SELECT option::integer FROM "
                "unnest(index_data.indoption::smallint[]) WITH ORDINALITY "
                "AS item(option, ordinality) ORDER BY item.ordinality) AS sort_options, "
                "ARRAY(SELECT operator_class.opcname::text FROM "
                "unnest(index_data.indclass::oid[]) WITH ORDINALITY "
                "AS item(opclass_oid, ordinality) JOIN pg_opclass operator_class "
                "ON operator_class.oid = item.opclass_oid ORDER BY item.ordinality) AS opclasses, "
                "ARRAY(SELECT CASE WHEN item.collation_oid = 0 THEN NULL "
                "ELSE collation_namespace.nspname || '.' || collation_catalog.collname END FROM "
                "unnest(index_data.indcollation::oid[]) WITH ORDINALITY "
                "AS item(collation_oid, ordinality) LEFT JOIN pg_collation collation_catalog "
                "ON collation_catalog.oid = item.collation_oid "
                "LEFT JOIN pg_namespace collation_namespace "
                "ON collation_namespace.oid = collation_catalog.collnamespace "
                "ORDER BY item.ordinality) AS collations, ARRAY(SELECT attribute.attname::text "
                "FROM unnest(index_data.indkey::smallint[]) WITH ORDINALITY "
                "AS item(attnum, ordinality) LEFT JOIN pg_attribute attribute "
                "ON attribute.attrelid = index_data.indrelid AND attribute.attnum = item.attnum "
                "ORDER BY item.ordinality) AS columns, "
                "pg_get_expr(index_data.indpred, index_data.indrelid, true) AS predicate "
                "FROM pg_index index_data JOIN pg_class table_relation "
                "ON table_relation.oid = index_data.indrelid JOIN pg_namespace table_namespace "
                "ON table_namespace.oid = table_relation.relnamespace JOIN pg_class index_relation "
                "ON index_relation.oid = index_data.indexrelid JOIN pg_am access_method "
                "ON access_method.oid = index_relation.relam "
                "WHERE table_namespace.nspname = :schema "
                "AND (table_relation.relname = :table_name "
                "OR index_relation.relname = :expected_index) "
                "ORDER BY index_relation.relname"
            ),
            {
                "schema": schema,
                "table_name": _TABLE,
                "expected_index": "bridge_generation_slots_pkey",
            },
        )
        .mappings()
        .all()
    )

    functions = (
        bind.execute(
            sa.text(
                "SELECT p.pronargs, p.pronargdefaults, pg_get_function_identity_arguments(p.oid) "
                "AS identity_arguments, pg_get_function_result(p.oid) AS result_type, "
                "l.lanname, p.prokind::text AS prokind, p.provolatile::text AS provolatile, "
                "p.proisstrict, p.prosecdef, p.proleakproof, p.proparallel::text AS proparallel, "
                "p.proconfig, p.prosrc FROM pg_proc p "
                "JOIN pg_namespace n ON n.oid = p.pronamespace "
                "JOIN pg_language l ON l.oid = p.prolang "
                "WHERE n.nspname = :schema AND p.proname = :function_name ORDER BY p.oid"
            ),
            {"schema": schema, "function_name": _FUNCTION},
        )
        .mappings()
        .all()
    )

    triggers = (
        bind.execute(
            sa.text(
                "SELECT tg.tgname, table_namespace.nspname AS table_schema, "
                "table_relation.relname AS table_name, tg.tgtype, tg.tgenabled::text "
                "AS tgenabled, tg.tgisinternal, tg.tgconstraint, tg.tgnargs, "
                "cardinality(tg.tgattr::smallint[]) AS update_column_count, "
                "tg.tgqual IS NULL AS has_no_condition, "
                "octet_length(tg.tgargs) AS argument_bytes, "
                "fn.proname AS function_name, fn_ns.nspname AS function_schema "
                "FROM pg_trigger tg JOIN pg_class table_relation "
                "ON table_relation.oid = tg.tgrelid JOIN pg_namespace table_namespace "
                "ON table_namespace.oid = table_relation.relnamespace JOIN pg_proc fn "
                "ON fn.oid = tg.tgfoid JOIN pg_namespace fn_ns "
                "ON fn_ns.oid = fn.pronamespace "
                "WHERE table_namespace.nspname = :schema AND NOT tg.tgisinternal "
                "AND (table_relation.relname = :table_name OR tg.tgname IN "
                "(:delete_trigger, :truncate_trigger)) ORDER BY tg.tgname"
            ),
            {
                "schema": schema,
                "table_name": _TABLE,
                "delete_trigger": _DELETE_TRIGGER,
                "truncate_trigger": _TRUNCATE_TRIGGER,
            },
        )
        .mappings()
        .all()
    )

    singleton_keys = []
    if relation is not None and any(row["column_name"] == "singleton_key" for row in columns):
        quote = bind.dialect.identifier_preparer.quote_identifier
        qualified_table = f"{quote(schema)}.{quote(_TABLE)}"
        singleton_keys = (
            bind.execute(
                sa.text(f"SELECT singleton_key FROM {qualified_table} ORDER BY singleton_key")
            )
            .scalars()
            .all()
        )

    return {
        "relation": dict(relation) if relation is not None else None,
        "table_type_exists": table_type_exists,
        "columns": [dict(row) for row in columns],
        "constraints": [dict(row) for row in constraints],
        "indexes": [dict(row) for row in indexes],
        "functions": [dict(row) for row in functions],
        "triggers": [dict(row) for row in triggers],
        "singleton_keys": singleton_keys,
    }


def _schema_errors(shape: Any, *, schema: str) -> list[str]:
    errors: list[str] = []

    def mismatch(object_name: str, expected: object, actual: object) -> None:
        errors.append(f"{object_name}: expected {expected!r}, actual {actual!r}")

    relation = shape["relation"]
    if relation != {"relkind": "r", "relpersistence": "p"}:
        mismatch(
            f"relation {_TABLE}",
            {"relkind": "r", "relpersistence": "p"},
            relation,
        )
    if shape["table_type_exists"] is not True:
        mismatch(f"type {_TABLE}", True, shape["table_type_exists"])

    actual_columns = {row["column_name"]: row for row in shape["columns"]}
    expected_columns = {
        name: (column_type, not_null, default)
        for name, column_type, not_null, default in _EXPECTED_COLUMNS
    }
    if set(actual_columns) != set(expected_columns):
        mismatch(f"columns {_TABLE}", sorted(expected_columns), sorted(actual_columns))
    for name, (column_type, not_null, default) in expected_columns.items():
        row = actual_columns.get(name)
        if row is None:
            continue
        actual_type = " ".join(str(row["column_type"]).lower().split())
        actual_default = _normalize_default(row["default_expression"])
        for field, expected, actual in (
            ("type", column_type, actual_type),
            ("notnull", not_null, bool(row["attnotnull"])),
            ("default", _normalize_default(default), actual_default),
        ):
            if expected != actual:
                mismatch(f"column {_TABLE}.{name}.{field}", expected, actual)

    actual_constraints = {row["conname"]: row for row in shape["constraints"]}
    if set(actual_constraints) != set(_EXPECTED_CONSTRAINTS):
        mismatch(
            f"constraints {_TABLE}",
            sorted(_EXPECTED_CONSTRAINTS),
            sorted(actual_constraints),
        )
    for name, (contype, columns, referenced_table) in _EXPECTED_CONSTRAINTS.items():
        row = actual_constraints.get(name)
        if row is None:
            continue
        actual_columns = tuple(row["columns"])
        if contype == "c":
            actual_columns = tuple(sorted(actual_columns))
            columns = tuple(sorted(columns))
        expected_fields = {
            "contype": contype,
            "table_schema": schema,
            "table_name": _TABLE,
            "columns": columns,
            "convalidated": True,
            "condeferrable": False,
            "condeferred": False,
        }
        actual_fields = {
            "contype": row["contype"],
            "table_schema": row["table_schema"],
            "table_name": row["table_name"],
            "columns": actual_columns,
            "convalidated": bool(row["convalidated"]),
            "condeferrable": bool(row["condeferrable"]),
            "condeferred": bool(row["condeferred"]),
        }
        if contype == "f":
            expected_fields.update(
                {
                    "referenced_schema": schema,
                    "referenced_table": referenced_table,
                    "referenced_columns": ("id",),
                    "confdeltype": "r",
                    "confupdtype": "a",
                    "confmatchtype": "s",
                }
            )
            actual_fields.update(
                {
                    "referenced_schema": row["referenced_schema"],
                    "referenced_table": row["referenced_table"],
                    "referenced_columns": tuple(row["referenced_columns"]),
                    "confdeltype": row["confdeltype"],
                    "confupdtype": row["confupdtype"],
                    "confmatchtype": row["confmatchtype"],
                }
            )
        for field, expected in expected_fields.items():
            actual = actual_fields[field]
            if expected != actual:
                mismatch(f"constraint {name}.{field}", expected, actual)
        if contype == "c":
            expected_definition = _normalize_constraint_definition(
                f"CHECK ({_EXPECTED_CHECK_EXPRESSIONS[name]})"
            )
            actual_definition = _normalize_constraint_definition(row["definition"])
            if expected_definition != actual_definition:
                mismatch(f"constraint {name}.expression", expected_definition, actual_definition)

    actual_indexes = {row["index_name"]: row for row in shape["indexes"]}
    expected_index_names = {"bridge_generation_slots_pkey"}
    if set(actual_indexes) != expected_index_names:
        mismatch(f"indexes {_TABLE}", sorted(expected_index_names), sorted(actual_indexes))
    expected_index = {
        "table_name": _TABLE,
        "indisunique": True,
        "indisprimary": True,
        "indisvalid": True,
        "indisready": True,
        "indnkeyatts": 1,
        "indnatts": 1,
        "amname": "btree",
        "sort_options": (0,),
        "opclasses": ("int2_ops",),
        "collations": (None,),
        "columns": ("singleton_key",),
        "predicate": None,
    }
    index = actual_indexes.get("bridge_generation_slots_pkey")
    if index is not None:
        for field, expected in expected_index.items():
            raw_actual = index[field]
            actual = (
                tuple(raw_actual)
                if field in {"sort_options", "opclasses", "collations", "columns"}
                else raw_actual
            )
            if expected != actual:
                mismatch(f"index bridge_generation_slots_pkey.{field}", expected, actual)

    functions = shape["functions"]
    if len(functions) != 1:
        mismatch(f"function {_FUNCTION}() count", 1, len(functions))
    else:
        function = functions[0]
        expected_function = {
            "pronargs": 0,
            "pronargdefaults": 0,
            "identity_arguments": "",
            "result_type": "trigger",
            "lanname": "plpgsql",
            "prokind": "f",
            "provolatile": "v",
            "proisstrict": False,
            "prosecdef": False,
            "proleakproof": False,
            "proparallel": "u",
            "proconfig": None,
        }
        for field, expected in expected_function.items():
            actual = function[field]
            if expected != actual:
                mismatch(f"function {_FUNCTION}().{field}", expected, actual)
        expected_body = _tokens(
            "BEGIN RAISE EXCEPTION 'bridge_generation_slot_singleton_cannot_be_removed'; END;"
        )
        actual_body = _tokens(function["prosrc"])
        if expected_body != actual_body:
            mismatch(f"function {_FUNCTION}().body", expected_body, actual_body)

    actual_triggers = {row["tgname"]: row for row in shape["triggers"]}
    expected_trigger_names = set(_EXPECTED_TRIGGER_TYPES)
    if len(actual_triggers) != len(shape["triggers"]):
        mismatch(f"trigger objects {_TABLE}", len(actual_triggers), len(shape["triggers"]))
    if set(actual_triggers) != expected_trigger_names:
        mismatch(f"triggers {_TABLE}", sorted(expected_trigger_names), sorted(actual_triggers))
    for name, trigger_type in _EXPECTED_TRIGGER_TYPES.items():
        row = actual_triggers.get(name)
        if row is None:
            continue
        expected_trigger = {
            "table_schema": schema,
            "table_name": _TABLE,
            "tgtype": trigger_type,
            "tgenabled": "O",
            "tgisinternal": False,
            "tgconstraint": 0,
            "tgnargs": 0,
            "update_column_count": 0,
            "has_no_condition": True,
            "argument_bytes": 0,
            "function_name": _FUNCTION,
            "function_schema": schema,
        }
        for field, expected in expected_trigger.items():
            actual = row[field]
            if expected != actual:
                mismatch(f"trigger {name}.{field}", expected, actual)
    if shape["singleton_keys"] != [1]:
        mismatch(f"rows {_TABLE}.singleton_key", [1], shape["singleton_keys"])

    return errors


def _preflight() -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    parents = {"editorial_threads", "editorial_thread_jobs", "model_conversation_turns"}
    missing = parents - tables
    if missing:
        raise RuntimeError(
            "refusing Bridge generation slot migration with missing parent tables: "
            + ", ".join(sorted(missing))
        )

    schema = bind.execute(sa.text("SELECT current_schema()")).scalar_one()
    shape = _read_shape(bind, schema=schema)
    no_objects = (
        shape["relation"] is None
        and not shape["table_type_exists"]
        and not shape["functions"]
        and not shape["triggers"]
        and not shape["indexes"]
    )
    if no_objects:
        return False

    errors = _schema_errors(shape, schema=schema)
    if errors:
        detail = "\n - ".join(errors)
        raise RuntimeError(
            "refusing non-conforming Bridge generation slot schema or guards:\n - " + detail
        )
    return True


def upgrade() -> None:
    if _preflight():
        return

    op.create_table(
        _TABLE,
        sa.Column("singleton_key", sa.SmallInteger(), autoincrement=False, nullable=False),
        sa.Column("holder_thread_id", sa.Uuid(), nullable=True),
        sa.Column("holder_job_id", sa.Uuid(), nullable=True),
        sa.Column("holder_turn_id", sa.Uuid(), nullable=True),
        sa.Column("lease_token", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("acquired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("state", sa.String(16), server_default=sa.text("'free'"), nullable=False),
        sa.PrimaryKeyConstraint("singleton_key", name="bridge_generation_slots_pkey"),
        sa.ForeignKeyConstraint(
            ["holder_thread_id"],
            ["editorial_threads.id"],
            name="fk_bridge_generation_slots_thread",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["holder_job_id"],
            ["editorial_thread_jobs.id"],
            name="fk_bridge_generation_slots_job",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["holder_turn_id"],
            ["model_conversation_turns.id"],
            name="fk_bridge_generation_slots_turn",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint("singleton_key = 1", name="ck_bridge_generation_slots_singleton"),
        sa.CheckConstraint("lease_token >= 0", name="ck_bridge_generation_slots_token"),
        sa.CheckConstraint(
            "state IN ('free', 'held', 'held_unknown')",
            name="ck_bridge_generation_slots_state",
        ),
        sa.CheckConstraint(
            "(state = 'free' AND holder_thread_id IS NULL AND holder_job_id IS NULL "
            "AND holder_turn_id IS NULL AND acquired_at IS NULL AND lease_expires_at IS NULL) "
            "OR (state IN ('held', 'held_unknown') AND holder_thread_id IS NOT NULL "
            "AND holder_job_id IS NOT NULL AND acquired_at IS NOT NULL "
            "AND lease_expires_at IS NOT NULL)",
            name="ck_bridge_generation_slots_holder_state",
        ),
    )
    op.execute(
        f"""
        CREATE FUNCTION {_FUNCTION}() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'bridge_generation_slot_singleton_cannot_be_removed';
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        f"""
        CREATE TRIGGER {_DELETE_TRIGGER}
        BEFORE DELETE ON {_TABLE}
        FOR EACH ROW EXECUTE FUNCTION {_FUNCTION}();
        """
    )
    op.execute(
        f"""
        CREATE TRIGGER {_TRUNCATE_TRIGGER}
        BEFORE TRUNCATE ON {_TABLE}
        FOR EACH STATEMENT EXECUTE FUNCTION {_FUNCTION}();
        """
    )
    op.execute(
        "INSERT INTO bridge_generation_slots "
        "(singleton_key, lease_token, state) VALUES (1, 0, 'free')"
    )


def downgrade() -> None:
    if not _preflight():
        raise RuntimeError("refusing to downgrade a missing Bridge generation slot schema")
    bind = op.get_bind()
    bind.execute(sa.text(f"LOCK TABLE {_TABLE} IN ACCESS EXCLUSIVE MODE"))
    if not _preflight():
        raise RuntimeError("refusing to downgrade a changed Bridge generation slot schema")
    slot = (
        bind.execute(
            sa.text(
                f"SELECT holder_thread_id, holder_job_id, holder_turn_id, lease_token, "
                "acquired_at, lease_expires_at, state "
                f"FROM {_TABLE} WHERE singleton_key = 1"
            )
        )
        .mappings()
        .one()
    )
    has_holder = any(
        slot[column] is not None
        for column in (
            "holder_thread_id",
            "holder_job_id",
            "holder_turn_id",
            "acquired_at",
            "lease_expires_at",
        )
    )
    if slot["state"] != "free" or slot["lease_token"] != 0 or has_holder:
        raise RuntimeError("refusing to downgrade after Bridge generation slot has been used")
    op.execute(f"DROP TRIGGER {_DELETE_TRIGGER} ON {_TABLE}")
    op.execute(f"DROP TRIGGER {_TRUNCATE_TRIGGER} ON {_TABLE}")
    op.drop_table(_TABLE)
    op.execute(f"DROP FUNCTION {_FUNCTION}()")
