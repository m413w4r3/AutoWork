"""Create the append-only human note annotation table (M24-WS2, 0026).

``workspace_note_annotations`` records one human action on one per-file
workspace note revision: a link to a task, an IOC value or a sample of the
same investigation, a useful/unuseful mark, an explicit author attribution or
a dismissal.  It is never a second evidence store (ADR-0011 D1/D6): no row
creates an observation, a finding or a sample qualification, and a note stays
unverified until a canonical observation is imported through ``work_contributions``.

The composite foreign keys pin the exact per-file revision and the task to the
same investigation; IOC and sample membership are resolved by the service
because those aggregates are subject-scoped.  UPDATE, DELETE and TRUNCATE are
refused by triggers, so every later mark is a new row.

ADR-0007 discipline: an already-conforming shape is accepted without DDL, any
partial or different shape fails before mutation, and the downgrade refuses
while any annotation row exists.
"""

from __future__ import annotations

import re
from typing import Any, cast

import sqlalchemy as sa
from alembic import op
from sqlalchemy.engine import Connection

revision = "0026_workspace_note_annotations"
down_revision = "0025_opinion_note_proposals"
branch_labels = None
depends_on = None

_TABLE = "workspace_note_annotations"
_FUNCTION = "reject_workspace_note_annotation_mutation"
_ROW_TRIGGER = "trg_workspace_note_annotations_append_only"
_TRUNCATE_TRIGGER = "trg_workspace_note_annotations_no_truncate"
_LIST_INDEX = "ix_workspace_note_annotations_investigation_path"
_REVISION_INDEX = "ix_workspace_note_annotations_revision"

_KIND_VALUES = (
    "'link_task', 'link_ioc', 'link_sample', 'mark_useful', 'unmark_useful', "
    "'attribute_author', 'dismiss'"
)
_IOC_TYPE_VALUES = "'md5', 'sha1', 'sha256', 'ipv4', 'domain', 'url'"

_FUNCTION_BODY = (
    "BEGIN RAISE EXCEPTION 'workspace note annotations are append-only' "
    "USING ERRCODE = '55000'; END;"
)

_PARENT_TABLES = (
    "analyst_investigations",
    "workspace_bindings",
    "workspace_file_revisions",
    "investigation_tasks",
)


def _expected_table() -> sa.Table:
    metadata = sa.MetaData()
    sa.Table("analyst_investigations", metadata, sa.Column("id", sa.Uuid(), primary_key=True))
    sa.Table(
        "workspace_bindings",
        metadata,
        sa.Column("workspace_id", sa.String(length=64), nullable=False),
        sa.Column("investigation_id", sa.Uuid(), nullable=False),
        sa.PrimaryKeyConstraint("workspace_id"),
        sa.UniqueConstraint(
            "workspace_id",
            "investigation_id",
            name="uq_workspace_bindings_workspace_investigation",
        ),
    )
    sa.Table(
        "workspace_file_revisions",
        metadata,
        sa.Column("workspace_id", sa.String(length=64), nullable=False),
        sa.Column("revision_id", sa.String(length=64), nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("workspace_id", "revision_id"),
        sa.UniqueConstraint(
            "workspace_id",
            "revision_id",
            "path",
            name="uq_workspace_file_revisions_workspace_revision_path",
        ),
    )
    sa.Table(
        "investigation_tasks",
        metadata,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("investigation_id", sa.Uuid(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "investigation_id", name="uq_investigation_tasks_identity"),
    )
    return sa.Table(
        _TABLE,
        metadata,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("investigation_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.String(length=64), nullable=False),
        sa.Column("revision_id", sa.String(length=64), nullable=False),
        sa.Column("file_relpath", sa.Text(), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("actor_kind", sa.String(length=16), nullable=False),
        sa.Column("actor_id", sa.Text(), nullable=False),
        sa.Column("target_task_id", sa.Uuid(), nullable=True),
        sa.Column("ioc_value", sa.Text(), nullable=True),
        sa.Column("ioc_type", sa.String(length=16), nullable=True),
        sa.Column("target_sample_id", sa.Uuid(), nullable=True),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_workspace_note_annotations"),
        sa.UniqueConstraint(
            "investigation_id",
            "idempotency_key",
            name="uq_workspace_note_annotations_investigation_idempotency",
        ),
        sa.ForeignKeyConstraint(
            ["investigation_id"],
            ["analyst_investigations.id"],
            name="fk_workspace_note_annotations_investigation",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "investigation_id"],
            ["workspace_bindings.workspace_id", "workspace_bindings.investigation_id"],
            name="fk_workspace_note_annotations_binding",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "revision_id", "file_relpath"],
            [
                "workspace_file_revisions.workspace_id",
                "workspace_file_revisions.revision_id",
                "workspace_file_revisions.path",
            ],
            name="fk_workspace_note_annotations_revision",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["target_task_id", "investigation_id"],
            ["investigation_tasks.id", "investigation_tasks.investigation_id"],
            name="fk_workspace_note_annotations_task",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            f"kind IN ({_KIND_VALUES})",
            name="ck_workspace_note_annotations_kind",
        ),
        sa.CheckConstraint(
            "actor_kind = 'human'",
            name="ck_workspace_note_annotations_actor_kind",
        ),
        sa.CheckConstraint(
            "btrim(actor_id) <> '' AND char_length(actor_id) <= 255",
            name="ck_workspace_note_annotations_actor_id",
        ),
        sa.CheckConstraint(
            (
                "file_relpath LIKE 'notes/%' "
                "AND file_relpath !~ '(^|/)\\.\\.?(/|$)' "
                "AND char_length(file_relpath) <= 4096"
            ),
            name="ck_workspace_note_annotations_note_path",
        ),
        sa.CheckConstraint(
            f"ioc_type IS NULL OR ioc_type IN ({_IOC_TYPE_VALUES})",
            name="ck_workspace_note_annotations_ioc_type",
        ),
        sa.CheckConstraint(
            "ioc_value IS NULL OR (btrim(ioc_value) <> '' AND char_length(ioc_value) <= 2048)",
            name="ck_workspace_note_annotations_ioc_value",
        ),
        sa.CheckConstraint(
            "(ioc_value IS NULL) = (ioc_type IS NULL)",
            name="ck_workspace_note_annotations_ioc_pair",
        ),
        sa.CheckConstraint(
            "detail IS NULL OR (btrim(detail) <> '' AND char_length(detail) <= 4000)",
            name="ck_workspace_note_annotations_detail",
        ),
        sa.CheckConstraint(
            "btrim(idempotency_key) <> '' AND char_length(idempotency_key) <= 255",
            name="ck_workspace_note_annotations_idempotency_key",
        ),
        sa.CheckConstraint(
            "(kind = 'link_task' AND target_task_id IS NOT NULL AND "
            "ioc_value IS NULL AND target_sample_id IS NULL AND detail IS NULL) OR "
            "(kind = 'link_ioc' AND target_task_id IS NULL AND "
            "ioc_value IS NOT NULL AND ioc_type IS NOT NULL AND "
            "target_sample_id IS NULL AND detail IS NULL) OR "
            "(kind = 'link_sample' AND target_task_id IS NULL AND ioc_value IS NULL AND "
            "target_sample_id IS NOT NULL AND detail IS NULL) OR "
            "(kind = 'attribute_author' AND target_task_id IS NULL AND ioc_value IS NULL AND "
            "target_sample_id IS NULL AND detail IS NOT NULL) OR "
            "(kind = 'dismiss' AND target_task_id IS NULL AND ioc_value IS NULL AND "
            "target_sample_id IS NULL AND detail IS NOT NULL) OR "
            "(kind IN ('mark_useful', 'unmark_useful') AND target_task_id IS NULL AND "
            "ioc_value IS NULL AND target_sample_id IS NULL AND detail IS NULL)",
            name="ck_workspace_note_annotations_targets",
        ),
        sa.Index(
            _LIST_INDEX,
            "investigation_id",
            "file_relpath",
            "created_at",
            "id",
        ),
        sa.Index(
            _REVISION_INDEX,
            "workspace_id",
            "revision_id",
            "created_at",
        ),
    )


def _normalize_sql(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.lower()
    normalized = re.sub(r"::(?:character varying|text)(?:\[\])?", "", normalized)
    # PostgreSQL renders ``x IN (...)`` as ``x = ANY (ARRAY[...])`` in a check
    # definition; compare both spellings as the same expression.
    normalized = re.sub(
        r"=\s*any\s*\(\s*\(?\s*array\s*\[(.*?)\]\s*\)?\s*\)",
        r"in (\1)",
        normalized,
    )
    normalized = re.sub(
        r"<>\s*all\s*\(\s*\(?\s*array\s*\[(.*?)\]\s*\)?\s*\)",
        r"not in (\1)",
        normalized,
    )
    normalized = normalized.replace("!~~", "not like").replace("~~", "like")
    return re.sub(r"[()\s]", "", normalized)


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
    result: set[tuple[str, tuple[str, ...], bool]] = set()
    for index in table.indexes:
        columns = tuple(
            str(getattr(expression, "name", expression)) for expression in index.expressions
        )
        result.add((str(index.name), columns, bool(index.unique)))
    return result


def _actual_index_signature(
    inspector: sa.Inspector, table_name: str
) -> set[tuple[str, tuple[str, ...], bool]]:
    result: set[tuple[str, tuple[str, ...], bool]] = set()
    for index in inspector.get_indexes(table_name):
        if index.get("duplicates_constraint"):
            continue
        name = str(index.get("name") or "")
        columns = tuple(str(item) for item in index.get("column_names", ()))
        result.add((name, columns, bool(index.get("unique"))))
    return result


def _schema_mismatches(bind: Any, table: sa.Table) -> list[str]:
    inspector = sa.inspect(bind)
    if not inspector.has_table(table.name):
        return [f"{table.name}: expected present, actual absent"]
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
    for signature in sorted(
        _constraint_signature(table) - _actual_constraint_signature(inspector, table.name),
        key=repr,
    ):
        mismatches.append(
            f"{table.name}: constraint {signature[1]}: expected {signature}, actual absent"
        )
    for signature in sorted(
        _index_signature(table) - _actual_index_signature(inspector, table.name), key=repr
    ):
        mismatches.append(f"{table.name}: index {signature[0]}: expected absent")
    return mismatches


def _guard_objects_mismatches(bind: Connection) -> list[str]:
    mismatches: list[str] = []
    functions = bind.execute(
        sa.text(
            "SELECT p.proname, l.lanname, p.prosrc FROM pg_proc p "
            "JOIN pg_namespace n ON n.oid = p.pronamespace "
            "JOIN pg_language l ON l.oid = p.prolang "
            "WHERE n.nspname = current_schema() AND p.proname = :name"
        ),
        {"name": _FUNCTION},
    ).all()
    if not functions:
        mismatches.append(f"function {_FUNCTION}: expected present, actual absent")
    else:
        name, language, body = functions[0]
        if (
            name != _FUNCTION
            or language != "plpgsql"
            or _normalize_sql(body) != _normalize_sql(_FUNCTION_BODY)
        ):
            mismatches.append(f"function {_FUNCTION}: shape differs")

    expected_triggers = {
        _ROW_TRIGGER: (27, ("before", "update", "delete", "for each row")),
        _TRUNCATE_TRIGGER: (34, ("before", "truncate", "for each statement")),
    }
    rows = bind.execute(
        sa.text(
            "SELECT t.tgname, c.relname AS table_name, t.tgtype, t.tgenabled, "
            "t.tgisinternal, p.proname AS function_name, pg_get_triggerdef(t.oid) AS definition "
            "FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid "
            "JOIN pg_proc p ON p.oid = t.tgfoid "
            "WHERE NOT t.tgisinternal AND c.relname = :table"
        ),
        {"table": _TABLE},
    ).all()
    actual = {row.tgname: row for row in rows}
    for name, (tgtype, clauses) in expected_triggers.items():
        row = actual.get(name)
        if row is None:
            mismatches.append(f"trigger {name}: expected present, actual absent")
            continue
        enabled = (
            row.tgenabled.decode("ascii")
            if isinstance(row.tgenabled, bytes)
            else str(row.tgenabled)
        )
        if (
            row.table_name != _TABLE
            or int(row.tgtype) != tgtype
            or enabled != "O"
            or row.function_name != _FUNCTION
        ):
            mismatches.append(f"trigger {name}: shape differs")
            continue
        definition = str(row.definition).casefold()
        if not all(clause in definition for clause in clauses):
            mismatches.append(f"trigger {name}: definition differs")
    for name in sorted(set(actual) - set(expected_triggers)):
        mismatches.append(f"trigger {name}: unexpected trigger on {_TABLE}")
    return mismatches


def _present_objects(bind: Connection) -> bool:
    inspector = sa.inspect(bind)
    if inspector.has_table(_TABLE):
        return True
    if bind.execute(
        sa.text(
            "SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
            "WHERE n.nspname = current_schema() AND p.proname = :name"
        ),
        {"name": _FUNCTION},
    ).scalar_one():
        return True
    return bool(
        bind.execute(
            sa.text(
                "SELECT count(*) FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid "
                "WHERE NOT t.tgisinternal AND c.relname = :table"
            ),
            {"table": _TABLE},
        ).scalar_one()
    )


def _full_shape_mismatches(bind: Connection) -> list[str]:
    inspector = sa.inspect(bind)
    mismatches: list[str] = []
    for name in _PARENT_TABLES:
        if not inspector.has_table(name):
            mismatches.append(f"{name}: expected present, actual absent")
    mismatches.extend(_schema_mismatches(bind, _expected_table()))
    mismatches.extend(_guard_objects_mismatches(bind))
    return mismatches


def _create_guards() -> None:
    op.execute(
        f"""
        CREATE FUNCTION {_FUNCTION}() RETURNS trigger AS $$
        {_FUNCTION_BODY}
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        f"""
        CREATE TRIGGER {_ROW_TRIGGER}
        BEFORE UPDATE OR DELETE ON {_TABLE}
        FOR EACH ROW EXECUTE FUNCTION {_FUNCTION}()
        """
    )
    op.execute(
        f"""
        CREATE TRIGGER {_TRUNCATE_TRIGGER}
        BEFORE TRUNCATE ON {_TABLE}
        FOR EACH STATEMENT EXECUTE FUNCTION {_FUNCTION}()
        """
    )


def _drop_guards() -> None:
    op.execute(f"DROP TRIGGER {_TRUNCATE_TRIGGER} ON {_TABLE}")
    op.execute(f"DROP TRIGGER {_ROW_TRIGGER} ON {_TABLE}")
    op.execute(f"DROP FUNCTION {_FUNCTION}()")


def upgrade() -> None:
    bind = op.get_bind()
    if _present_objects(bind):
        mismatches = _full_shape_mismatches(bind)
        if mismatches:
            raise RuntimeError("note annotation preflight rejected: " + "; ".join(mismatches))
        return
    inspector = sa.inspect(bind)
    missing = [name for name in _PARENT_TABLES if not inspector.has_table(name)]
    if missing:
        raise RuntimeError(
            "note annotation preflight rejected: expected present, actual absent "
            + ", ".join(missing)
        )
    bind.execute(sa.text(f"LOCK TABLE {', '.join(_PARENT_TABLES)} IN ACCESS EXCLUSIVE MODE"))
    _expected_table().create(bind=bind)
    _create_guards()


def downgrade() -> None:
    bind = op.get_bind()
    if not _present_objects(bind):
        raise RuntimeError("refusing to downgrade a missing note annotation schema")
    mismatches = _full_shape_mismatches(bind)
    if mismatches:
        raise RuntimeError(
            "refusing to downgrade a partial or changed note annotation schema: "
            + "; ".join(mismatches)
        )
    bind.execute(sa.text(f"LOCK TABLE {_TABLE} IN ACCESS EXCLUSIVE MODE"))
    if bind.execute(sa.text(f"SELECT count(*) FROM {_TABLE}")).scalar_one():
        raise RuntimeError(f"refusing to downgrade {_TABLE} after it has been used")
    _drop_guards()
    op.drop_index(_REVISION_INDEX, table_name=_TABLE)
    op.drop_index(_LIST_INDEX, table_name=_TABLE)
    op.drop_table(_TABLE)
