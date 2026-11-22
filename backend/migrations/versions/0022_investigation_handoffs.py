"""Create immutable investigation handoffs and the task handoff reference (M24-05A).

Revision ID: 0022_investigation_handoffs
Revises: 0021_investigation_tasks

``investigation_handoffs`` is the sole authority for a handoff's content
(ADR-0011 D1/D5).  Versions are per task, idempotency is scoped per task, and
the table is append-only by ``BEFORE UPDATE OR DELETE`` and ``BEFORE TRUNCATE``
triggers.  The revision also adds the ``investigation_tasks.handoff_ref`` foreign
key that 0021 deliberately deferred.

ADR-0007 discipline: an already-conforming shape is accepted without DDL, any
partial or different shape fails before mutation, and the downgrade refuses
while handoff rows exist.  The preflight tolerates objects added by *later*
additive revisions on the tables it references, so re-running the chain over a
newer schema still recognises its own shape.
"""

from __future__ import annotations

import re
from typing import Any, cast

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import Connection

revision = "0022_investigation_handoffs"
down_revision = "0021_investigation_tasks"
branch_labels = None
depends_on = None

_INVESTIGATIONS = "analyst_investigations"
_TASKS = "investigation_tasks"
_HANDOFFS = "investigation_handoffs"

_TASK_HANDOFF_FK = "fk_investigation_tasks_handoff"
_HANDOFF_FUNCTION = "reject_investigation_handoff_mutation"
_HANDOFF_TRIGGER = "trg_investigation_handoffs_append_only"
_HANDOFF_TRUNCATE_TRIGGER = "trg_investigation_handoffs_no_truncate"
_HANDOFF_FUNCTION_BODY = (
    "BEGIN RAISE EXCEPTION 'investigation handoffs are append-only' USING ERRCODE = '55000'; END;"
)

_HANDOFF_SCHEMA_VERSION = "investigation-handoff.v1"
_HANDOFF_PARTY_ACTOR_KIND_VALUES = "'human', 'operator'"
_HANDOFF_CREATOR_ACTOR_KIND_VALUES = "'human', 'operator', 'system'"
_CONTENT_IDENTITY_SQL = (
    "content ? 'handoff_id' AND content ? 'investigation_id' AND content ? 'task_id' "
    "AND content ? 'version' AND content ? 'schema_version' "
    "AND content->>'handoff_id' = id::text "
    "AND content->>'investigation_id' = investigation_id::text "
    "AND content->>'task_id' = task_id::text "
    "AND content->>'version' = version::text "
    "AND content->>'schema_version' = schema_version"
)


def _normalize_sql(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.lower()
    normalized = re.sub(r"::(?:character varying|text)(?:\[\])?", "", normalized)
    normalized = re.sub(
        r"=\s*any\s*\(\s*\(?\s*array\s*\[(.*?)\]\s*\)?\s*\)",
        r"in (\1)",
        normalized,
    )
    return re.sub(r"[()\s]", "", normalized)


def _expected_table() -> sa.Table:
    metadata = sa.MetaData()
    # Minimal parent shapes so the expected foreign keys resolve inside this
    # private metadata; the real tables are validated by their own revisions.
    sa.Table(
        _INVESTIGATIONS,
        metadata,
        sa.Column("id", sa.Uuid(), primary_key=True),
    )
    sa.Table(
        _TASKS,
        metadata,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("investigation_id", sa.Uuid(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "investigation_id", name="uq_investigation_tasks_identity"),
    )
    table = sa.Table(
        _HANDOFFS,
        metadata,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("investigation_id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.BigInteger(), nullable=False),
        sa.Column("schema_version", sa.String(length=32), nullable=False),
        sa.Column("content", JSONB(), nullable=False),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column("request_sha256", sa.String(length=64), nullable=False),
        sa.Column("from_actor_kind", sa.String(length=16), nullable=False),
        sa.Column("from_actor_id", sa.Text(), nullable=False),
        sa.Column("to_actor_kind", sa.String(length=16), nullable=False),
        sa.Column("to_actor_id", sa.Text(), nullable=False),
        sa.Column("predecessor_id", sa.Uuid(), nullable=True),
        sa.Column("created_by_actor_kind", sa.String(length=16), nullable=False),
        sa.Column("created_by_actor_id", sa.Text(), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_investigation_handoffs"),
        sa.UniqueConstraint("task_id", "version", name="uq_investigation_handoffs_task_version"),
        sa.UniqueConstraint(
            "task_id",
            "idempotency_key",
            name="uq_investigation_handoffs_task_idempotency",
        ),
        sa.ForeignKeyConstraint(
            ["investigation_id"],
            [f"{_INVESTIGATIONS}.id"],
            name="fk_investigation_handoffs_investigation",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["task_id", "investigation_id"],
            [f"{_TASKS}.id", f"{_TASKS}.investigation_id"],
            name="fk_investigation_handoffs_task",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["predecessor_id"],
            [f"{_HANDOFFS}.id"],
            name="fk_investigation_handoffs_predecessor",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint("version >= 1", name="ck_investigation_handoffs_version"),
        sa.CheckConstraint(
            f"schema_version = '{_HANDOFF_SCHEMA_VERSION}'",
            name="ck_investigation_handoffs_schema_version",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(content) = 'object'",
            name="ck_investigation_handoffs_content_object",
        ),
        sa.CheckConstraint(
            "char_length(content_sha256) = 64 AND content_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_investigation_handoffs_content_sha256",
        ),
        sa.CheckConstraint(
            "char_length(request_sha256) = 64 AND request_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_investigation_handoffs_request_sha256",
        ),
        sa.CheckConstraint(
            f"from_actor_kind IN ({_HANDOFF_PARTY_ACTOR_KIND_VALUES})",
            name="ck_investigation_handoffs_from_actor_kind",
        ),
        sa.CheckConstraint(
            f"to_actor_kind IN ({_HANDOFF_PARTY_ACTOR_KIND_VALUES})",
            name="ck_investigation_handoffs_to_actor_kind",
        ),
        sa.CheckConstraint(
            f"created_by_actor_kind IN ({_HANDOFF_CREATOR_ACTOR_KIND_VALUES})",
            name="ck_investigation_handoffs_created_by_actor_kind",
        ),
        sa.CheckConstraint(
            _CONTENT_IDENTITY_SQL,
            name="ck_investigation_handoffs_content_identity",
        ),
        sa.Index(
            "ix_investigation_handoffs_investigation_created",
            "investigation_id",
            "created_at",
            "id",
        ),
    )
    return table


def _constraint_signature(table: sa.Table) -> set[tuple[Any, ...]]:
    result: set[tuple[Any, ...]] = set()
    for constraint in table.constraints:
        if isinstance(constraint, sa.PrimaryKeyConstraint):
            result.add(("p", constraint.name, tuple(column.name for column in constraint.columns)))
        elif isinstance(constraint, sa.UniqueConstraint):
            result.add(("u", constraint.name, tuple(column.name for column in constraint.columns)))
        elif isinstance(constraint, sa.ForeignKeyConstraint):
            elements = tuple(constraint.elements)
            result.add(
                (
                    "f",
                    constraint.name,
                    tuple(item.parent.name for item in elements),
                    elements[0].column.table.name,
                    tuple(item.column.name for item in elements),
                    (constraint.ondelete or "").upper(),
                    (constraint.onupdate or "").upper(),
                )
            )
        elif isinstance(constraint, sa.CheckConstraint):
            result.add(("c", constraint.name, _normalize_sql(str(constraint.sqltext))))
    return result


def _actual_constraint_signature(inspector: sa.Inspector, table_name: str) -> set[tuple[Any, ...]]:
    result: set[tuple[Any, ...]] = set()
    primary_key = inspector.get_pk_constraint(table_name)
    if primary_key.get("name"):
        result.add(("p", primary_key["name"], tuple(primary_key.get("constrained_columns", ()))))
    for unique_constraint in inspector.get_unique_constraints(table_name):
        result.add(
            (
                "u",
                unique_constraint.get("name"),
                tuple(unique_constraint.get("column_names", ())),
            )
        )
    for foreign_key in inspector.get_foreign_keys(table_name):
        options = foreign_key.get("options", {})
        result.add(
            (
                "f",
                foreign_key.get("name"),
                tuple(cast(list[str] | None, foreign_key.get("constrained_columns")) or ()),
                foreign_key.get("referred_table"),
                tuple(cast(list[str] | None, foreign_key.get("referred_columns")) or ()),
                str(options.get("ondelete", "")).upper(),
                str(options.get("onupdate", "")).upper(),
            )
        )
    for check_constraint in inspector.get_check_constraints(table_name):
        result.add(
            (
                "c",
                check_constraint.get("name"),
                _normalize_sql(cast(str | None, check_constraint.get("sqltext"))),
            )
        )
    return result


def _index_signature(table: sa.Table) -> set[tuple[str, tuple[str, ...], bool]]:
    return {
        (
            str(index.name),
            tuple(str(getattr(expression, "name", expression)) for expression in index.expressions),
            bool(index.unique),
        )
        for index in table.indexes
    }


def _actual_index_signature(
    inspector: sa.Inspector, table_name: str
) -> set[tuple[str, tuple[str, ...], bool]]:
    return {
        (index.get("name"), tuple(index.get("column_names", ())), bool(index.get("unique")))
        for index in inspector.get_indexes(table_name)
        if not index.get("duplicates_constraint")
    }


def _schema_mismatches(bind: Any, table: sa.Table) -> list[str]:
    """Every expected column/constraint/index must exist with its exact shape.

    Extra columns and constraints are tolerated: later additive revisions may
    extend this table, and re-running 0022 over such a schema must still
    recognise its own shape (ADR-0007).
    """
    inspector = sa.inspect(bind)
    mismatches: list[str] = []
    actual_columns = {item["name"]: item for item in inspector.get_columns(table.name)}
    for name in table.columns.keys():
        expected = table.columns[name]
        actual = actual_columns.get(name)
        if actual is None:
            mismatches.append(f"{table.name}: column {name}: expected present, actual absent")
            continue
        expected_signature = (
            f"type={expected.type.compile(dialect=bind.dialect).lower()}, "
            f"nullable={expected.nullable}"
        )
        actual_signature = (
            f"type={actual['type'].compile(dialect=bind.dialect).lower()}, "
            f"nullable={actual['nullable']}"
        )
        if expected_signature != actual_signature:
            mismatches.append(
                f"{table.name}: column {name}: expected {expected_signature}, "
                f"actual {actual_signature}"
            )
    expected_constraints = _constraint_signature(table)
    actual_constraints = _actual_constraint_signature(inspector, table.name)
    for signature in sorted(expected_constraints - actual_constraints, key=repr):
        mismatches.append(f"{table.name}: constraint {signature[1]}: expected absent")
    expected_indexes = _index_signature(table)
    actual_indexes = _actual_index_signature(inspector, table.name)
    for signature in sorted(expected_indexes - actual_indexes, key=repr):
        mismatches.append(f"{table.name}: index {signature[0]}: expected absent")
    return mismatches


def _task_handoff_fk_mismatches(inspector: sa.Inspector) -> list[str]:
    if not inspector.has_table(_TASKS):
        return [f"{_TASKS}: expected present, actual absent"]
    foreign_keys = {
        foreign_key.get("name"): foreign_key for foreign_key in inspector.get_foreign_keys(_TASKS)
    }
    foreign_key = foreign_keys.get(_TASK_HANDOFF_FK)
    if foreign_key is None:
        return [f"{_TASKS}: foreign key {_TASK_HANDOFF_FK}: expected absent"]
    options = foreign_key.get("options", {})
    if (
        tuple(foreign_key.get("constrained_columns", ())) != ("handoff_ref",)
        or foreign_key.get("referred_table") != _HANDOFFS
        or tuple(foreign_key.get("referred_columns", ())) != ("id",)
        or str(options.get("ondelete", "")).upper() != "RESTRICT"
    ):
        return [f"{_TASKS}: foreign key {_TASK_HANDOFF_FK}: shape differs"]
    return []


def _guard_mismatches(bind: Connection) -> list[str]:
    mismatches: list[str] = []
    functions = bind.execute(
        sa.text(
            "SELECT p.proname, l.lanname, p.prosrc FROM pg_proc p "
            "JOIN pg_namespace n ON n.oid = p.pronamespace "
            "JOIN pg_language l ON l.oid = p.prolang "
            "WHERE n.nspname = current_schema() AND p.proname = :function"
        ),
        {"function": _HANDOFF_FUNCTION},
    ).all()
    if len(functions) != 1:
        mismatches.append(f"function {_HANDOFF_FUNCTION}: expected present, actual absent")
    else:
        row = functions[0]
        if (row.lanname, _normalize_sql(row.prosrc)) != (
            "plpgsql",
            _normalize_sql(_HANDOFF_FUNCTION_BODY),
        ):
            mismatches.append(f"function {_HANDOFF_FUNCTION}: body differs")

    expected_triggers = {
        _HANDOFF_TRIGGER: (_HANDOFFS, 27, _HANDOFF_FUNCTION),
        _HANDOFF_TRUNCATE_TRIGGER: (_HANDOFFS, 34, _HANDOFF_FUNCTION),
    }
    trigger_rows = bind.execute(
        sa.text(
            "SELECT t.tgname, t.tgrelid::regclass::text AS table_name, t.tgtype, "
            "t.tgenabled, t.tgisinternal, p.proname AS function_name, "
            "pg_get_triggerdef(t.oid) AS definition "
            "FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "JOIN pg_proc p ON p.oid = t.tgfoid "
            "WHERE n.nspname = current_schema() AND c.relname = :handoffs "
            "AND NOT t.tgisinternal"
        ),
        {"handoffs": _HANDOFFS},
    ).all()
    actual_triggers = {row.tgname for row in trigger_rows}
    for name in expected_triggers:
        if name not in actual_triggers:
            mismatches.append(f"trigger {name}: expected present, actual absent")
    for row in trigger_rows:
        expected = expected_triggers.get(row.tgname)
        if expected is None:
            if row.tgname in (_HANDOFF_TRIGGER, _HANDOFF_TRUNCATE_TRIGGER):
                mismatches.append(f"trigger {row.tgname}: attributes differ")
            continue
        table_name, trigger_type, function_name = expected
        actual_table = row.table_name.rsplit(".", maxsplit=1)[-1].strip('"')
        if (
            actual_table != table_name
            or row.tgtype != trigger_type
            or row.tgenabled not in ("O", b"O")
            or row.tgisinternal
            or row.function_name != function_name
        ):
            mismatches.append(f"trigger {row.tgname}: attributes differ")
            continue
        definition = re.sub(r"\b(?:public\.)", "", row.definition.lower())
        definition = re.sub(r'"([a-z_]+)"', r"\1", definition)
        definition = re.sub(r"\s+", " ", definition).strip()
        if trigger_type == 34:
            if f"before truncate on {table_name}" not in definition:
                mismatches.append(f"trigger {row.tgname}: definition differs")
        elif f"on {table_name}" not in definition or not re.search(
            r"before (?:update or delete|delete or update)", definition
        ):
            mismatches.append(f"trigger {row.tgname}: definition differs")
    return mismatches


def _present_objects(bind: Connection) -> bool:
    inspector = sa.inspect(bind)
    if inspector.has_table(_HANDOFFS):
        return True
    if inspector.has_table(_TASKS):
        foreign_keys = {
            foreign_key.get("name") for foreign_key in inspector.get_foreign_keys(_TASKS)
        }
        if _TASK_HANDOFF_FK in foreign_keys:
            return True
    guards = bind.execute(
        sa.text(
            "SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
            "WHERE n.nspname = current_schema() AND p.proname = :function"
        ),
        {"function": _HANDOFF_FUNCTION},
    ).scalar_one()
    triggers = bind.execute(
        sa.text(
            "SELECT count(*) FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = current_schema() AND NOT t.tgisinternal AND t.tgname IN "
            "(:trigger, :truncate_trigger)"
        ),
        {
            "trigger": _HANDOFF_TRIGGER,
            "truncate_trigger": _HANDOFF_TRUNCATE_TRIGGER,
        },
    ).scalar_one()
    return bool(guards or triggers)


def _full_shape_mismatches(bind: Connection) -> list[str]:
    inspector = sa.inspect(bind)
    mismatches: list[str] = []
    if not inspector.has_table(_HANDOFFS):
        mismatches.append(f"{_HANDOFFS}: expected present, actual absent")
    else:
        mismatches.extend(_schema_mismatches(bind, _expected_table()))
    for name in (_INVESTIGATIONS, _TASKS):
        if not inspector.has_table(name):
            mismatches.append(f"{name}: expected present, actual absent")
    mismatches.extend(_task_handoff_fk_mismatches(inspector))
    mismatches.extend(_guard_mismatches(bind))
    return mismatches


def _baseline_mismatches(bind: Connection) -> list[str]:
    inspector = sa.inspect(bind)
    mismatches: list[str] = [
        f"{name}: expected present, actual absent"
        for name in (_INVESTIGATIONS, _TASKS)
        if not inspector.has_table(name)
    ]
    if inspector.has_table(_HANDOFFS):
        mismatches.append(f"{_HANDOFFS}: unexpected table")
    if inspector.has_table(_TASKS):
        foreign_keys = {
            foreign_key.get("name") for foreign_key in inspector.get_foreign_keys(_TASKS)
        }
        if _TASK_HANDOFF_FK in foreign_keys:
            mismatches.append(f"{_TASKS}: unexpected foreign key {_TASK_HANDOFF_FK}")
    return mismatches


def _create_table() -> None:
    op.create_table(
        _HANDOFFS,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("investigation_id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.BigInteger(), nullable=False),
        sa.Column("schema_version", sa.String(length=32), nullable=False),
        sa.Column("content", JSONB(), nullable=False),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column("request_sha256", sa.String(length=64), nullable=False),
        sa.Column("from_actor_kind", sa.String(length=16), nullable=False),
        sa.Column("from_actor_id", sa.Text(), nullable=False),
        sa.Column("to_actor_kind", sa.String(length=16), nullable=False),
        sa.Column("to_actor_id", sa.Text(), nullable=False),
        sa.Column("predecessor_id", sa.Uuid(), nullable=True),
        sa.Column("created_by_actor_kind", sa.String(length=16), nullable=False),
        sa.Column("created_by_actor_id", sa.Text(), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_investigation_handoffs"),
        sa.UniqueConstraint("task_id", "version", name="uq_investigation_handoffs_task_version"),
        sa.UniqueConstraint(
            "task_id",
            "idempotency_key",
            name="uq_investigation_handoffs_task_idempotency",
        ),
        sa.ForeignKeyConstraint(
            ["investigation_id"],
            [f"{_INVESTIGATIONS}.id"],
            name="fk_investigation_handoffs_investigation",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["task_id", "investigation_id"],
            [f"{_TASKS}.id", f"{_TASKS}.investigation_id"],
            name="fk_investigation_handoffs_task",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["predecessor_id"],
            [f"{_HANDOFFS}.id"],
            name="fk_investigation_handoffs_predecessor",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint("version >= 1", name="ck_investigation_handoffs_version"),
        sa.CheckConstraint(
            f"schema_version = '{_HANDOFF_SCHEMA_VERSION}'",
            name="ck_investigation_handoffs_schema_version",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(content) = 'object'",
            name="ck_investigation_handoffs_content_object",
        ),
        sa.CheckConstraint(
            "char_length(content_sha256) = 64 AND content_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_investigation_handoffs_content_sha256",
        ),
        sa.CheckConstraint(
            "char_length(request_sha256) = 64 AND request_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_investigation_handoffs_request_sha256",
        ),
        sa.CheckConstraint(
            f"from_actor_kind IN ({_HANDOFF_PARTY_ACTOR_KIND_VALUES})",
            name="ck_investigation_handoffs_from_actor_kind",
        ),
        sa.CheckConstraint(
            f"to_actor_kind IN ({_HANDOFF_PARTY_ACTOR_KIND_VALUES})",
            name="ck_investigation_handoffs_to_actor_kind",
        ),
        sa.CheckConstraint(
            f"created_by_actor_kind IN ({_HANDOFF_CREATOR_ACTOR_KIND_VALUES})",
            name="ck_investigation_handoffs_created_by_actor_kind",
        ),
        sa.CheckConstraint(
            _CONTENT_IDENTITY_SQL,
            name="ck_investigation_handoffs_content_identity",
        ),
    )
    op.create_index(
        "ix_investigation_handoffs_investigation_created",
        _HANDOFFS,
        ["investigation_id", "created_at", "id"],
    )


def _create_guards() -> None:
    op.execute(
        f"""
        CREATE FUNCTION {_HANDOFF_FUNCTION}() RETURNS trigger AS $$
        {_HANDOFF_FUNCTION_BODY}
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        f"""
        CREATE TRIGGER {_HANDOFF_TRIGGER}
        BEFORE UPDATE OR DELETE ON {_HANDOFFS}
        FOR EACH ROW EXECUTE FUNCTION {_HANDOFF_FUNCTION}()
        """
    )
    op.execute(
        f"""
        CREATE TRIGGER {_HANDOFF_TRUNCATE_TRIGGER}
        BEFORE TRUNCATE ON {_HANDOFFS}
        FOR EACH STATEMENT EXECUTE FUNCTION {_HANDOFF_FUNCTION}()
        """
    )


def _drop_guards() -> None:
    op.execute(f"DROP TRIGGER {_HANDOFF_TRUNCATE_TRIGGER} ON {_HANDOFFS}")
    op.execute(f"DROP TRIGGER {_HANDOFF_TRIGGER} ON {_HANDOFFS}")
    op.execute(f"DROP FUNCTION {_HANDOFF_FUNCTION}()")


def upgrade() -> None:
    bind = op.get_bind()
    if _present_objects(bind):
        mismatches = _full_shape_mismatches(bind)
        if mismatches:
            raise RuntimeError("investigation handoff preflight rejected: " + "; ".join(mismatches))
        return
    mismatches = _baseline_mismatches(bind)
    if mismatches:
        raise RuntimeError("investigation handoff preflight rejected: " + "; ".join(mismatches))

    bind.execute(sa.text(f"LOCK TABLE {_INVESTIGATIONS}, {_TASKS} IN ACCESS EXCLUSIVE MODE"))
    _create_table()
    _create_guards()
    op.create_foreign_key(
        _TASK_HANDOFF_FK,
        _TASKS,
        _HANDOFFS,
        ["handoff_ref"],
        ["id"],
        ondelete="RESTRICT",
    )


def downgrade() -> None:
    bind = op.get_bind()
    if not _present_objects(bind):
        raise RuntimeError("refusing to downgrade a missing investigation handoff schema")
    mismatches = _full_shape_mismatches(bind)
    if mismatches:
        raise RuntimeError(
            "refusing to downgrade a partial or changed investigation handoff schema: "
            + "; ".join(mismatches)
        )

    bind.execute(
        sa.text(f"LOCK TABLE {_INVESTIGATIONS}, {_TASKS}, {_HANDOFFS} IN ACCESS EXCLUSIVE MODE")
    )
    row_count = bind.execute(sa.text(f"SELECT count(*) FROM {_HANDOFFS}")).scalar_one()
    if row_count:
        raise RuntimeError(f"refusing to downgrade {_HANDOFFS} after it has been used")
    referenced = bind.execute(
        sa.text(f"SELECT count(*) FROM {_TASKS} WHERE handoff_ref IS NOT NULL")
    ).scalar_one()
    if referenced:
        raise RuntimeError("refusing to downgrade while tasks reference a handoff")

    op.drop_constraint(_TASK_HANDOFF_FK, _TASKS, type_="foreignkey")
    _drop_guards()
    op.drop_table(_HANDOFFS)
