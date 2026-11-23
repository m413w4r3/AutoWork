"""Record the requesting task of a human advisor consultation (M24.1 F4-D B1).

Revision ID: 0028_task_context_consultations
Revises: 0027_instruction_delivery

``tool_invocations.context_task_id`` links a *human* Bridge/advisor request to
the investigation task that motivated it, without touching the cumulative
agentic attribution pair ``(task_id IS NULL) = (agent_session_id IS NULL)``
that 0023 installed.  ``task_id``/``agent_session_id`` remain the sole
attribution of an operator execution; ``context_task_id`` is request context
and is never inferred from tool arguments.

The same-investigation composite foreign key
``(context_task_id, investigation_id) -> investigation_tasks(id,
investigation_id)`` reuses the 0021 identity unique key, so a consultation can
never cite a task from another investigation.  The CHECK accepts a
``context_task_id`` only on an invocation attributed to a human requester that
carries no operator execution pair.  An agentic invocation keeps it NULL — not
even an equal ``task_id`` — because the design keeps the two authorities
(request context vs execution attribution) distinguishable without a second
interpretation.

ADR-0007 discipline: the exact 0027 shape is verified before any DDL, a
conforming 0028 shape is accepted without DDL, any partial or different shape
fails before mutation, and the downgrade refuses while any row still carries a
``context_task_id``.

The frozen ``0001_baseline`` predates operator attribution, so a fresh install
reaches this revision without the column and receives the additive DDL here; an
installation that already carries the conforming shape (a later stamp or a
manual application) is accepted only after full validation and without DDL.
"""

from __future__ import annotations

import re
from typing import Any, cast

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import Connection

revision = "0028_task_context_consultations"
down_revision = "0027_instruction_delivery"
branch_labels = None
depends_on = None

_TABLE = "tool_invocations"
_TASKS = "investigation_tasks"
_COLUMN = "context_task_id"

_CONTEXT_FK = "fk_tool_invocations_context_task"
_CONTEXT_CHECK = "ck_tool_invocations_context_task_human"
_CONTEXT_INDEX = "ix_tool_invocations_context_task"

_TOOL_STATUS_CHECK = "ck_tool_invocations_status"
_TOOL_VERSION_CHECK = "ck_tool_invocations_version"
_TOOL_PARAMETERS_CHECK = "ck_tool_invocations_parameters_object"
_TOOL_BUDGET_CHECK = "ck_tool_invocations_budget_objects"
_TOOL_REFS_CHECK = "ck_tool_invocations_response_refs_array"
_TOOL_ACTOR_KIND_CHECK = "ck_tool_invocations_actor_kind"
_TOOL_ACTOR_PAIR_CHECK = "ck_tool_invocations_actor_pair"
_TOOL_SESSION_PAIR_CHECK = "ck_tool_invocations_agent_session_pair"

_TOOL_STATUS_VALUES = (
    "'pending', 'running', 'succeeded', 'empty', 'unavailable', 'policy_denied', "
    "'needs_review', 'failed', 'partial'"
)
_TOOL_ACTOR_VALUES = "'human', 'operator'"
_CONTEXT_CHECK_SQL = (
    "context_task_id IS NULL OR "
    "(actor_kind = 'human' AND task_id IS NULL AND agent_session_id IS NULL)"
)

_EXISTING_INDEXES = (
    ("ix_tool_invocations_investigation", ("investigation_id", "created_at", "id")),
    ("ix_tool_invocations_status", ("status", "updated_at")),
    ("ix_tool_invocations_task_created", ("task_id", "created_at")),
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
    normalized = re.sub(
        r"<>\s*all\s*\(\s*\(?\s*array\s*\[(.*?)\]\s*\)?\s*\)",
        r"not in (\1)",
        normalized,
    )
    return re.sub(r"[()\s]", "", normalized)


def _metadata_with_parents() -> sa.MetaData:
    metadata = sa.MetaData()
    sa.Table("analyst_investigations", metadata, sa.Column("id", sa.Uuid(), primary_key=True))
    sa.Table(
        _TASKS,
        metadata,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("investigation_id", sa.Uuid(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "investigation_id", name="uq_investigation_tasks_identity"),
    )
    sa.Table(
        "agent_sessions",
        metadata,
        sa.Column("id", sa.Uuid(), primary_key=True),
    )
    return metadata


def _expected_table(*, with_context: bool) -> sa.Table:
    """The exact 0027 shape, or the 0028 shape when ``with_context`` is true."""
    columns: list[Any] = [
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("investigation_id", sa.Uuid(), nullable=False),
        sa.Column("operation_key", sa.String(length=255), nullable=False),
        sa.Column("tool_name", sa.String(length=128), nullable=False),
        sa.Column("schema_version", sa.String(length=64), nullable=False),
        sa.Column("normalized_parameters", JSONB, nullable=False),
        sa.Column("passive_read_retry_trusted", sa.Boolean(), nullable=True),
        sa.Column("purpose", sa.Text(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=True),
        sa.Column("actor_kind", sa.String(length=16), nullable=True),
        sa.Column("actor_id", sa.Text(), nullable=True),
        sa.Column("agent_session_id", sa.Uuid(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("reserved_budget", JSONB, nullable=False),
        sa.Column("consumed_budget", JSONB, nullable=False),
        sa.Column("response_refs", JSONB, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("version", sa.BigInteger(), nullable=False),
    ]
    if with_context:
        columns.append(sa.Column(_COLUMN, sa.Uuid(), nullable=True))
    definitions: list[Any] = [
        *columns,
        sa.PrimaryKeyConstraint("id", name="tool_invocations_pkey"),
        sa.UniqueConstraint("operation_key", name="uq_tool_invocations_operation_key"),
        sa.ForeignKeyConstraint(
            ["task_id"],
            [f"{_TASKS}.id"],
            name="fk_tool_invocations_task",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["agent_session_id"],
            ["agent_sessions.id"],
            name="fk_tool_invocations_agent_session",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            f"status IN ({_TOOL_STATUS_VALUES})",
            name=_TOOL_STATUS_CHECK,
        ),
        sa.CheckConstraint("version >= 1", name=_TOOL_VERSION_CHECK),
        sa.CheckConstraint(
            "jsonb_typeof(normalized_parameters) = 'object'",
            name=_TOOL_PARAMETERS_CHECK,
        ),
        sa.CheckConstraint(
            "jsonb_typeof(reserved_budget) = 'object' AND jsonb_typeof(consumed_budget) = 'object'",
            name=_TOOL_BUDGET_CHECK,
        ),
        sa.CheckConstraint(
            "jsonb_typeof(response_refs) = 'array'",
            name=_TOOL_REFS_CHECK,
        ),
        sa.CheckConstraint(
            f"actor_kind IN ({_TOOL_ACTOR_VALUES})",
            name=_TOOL_ACTOR_KIND_CHECK,
        ),
        sa.CheckConstraint(
            "(actor_kind IS NULL) = (actor_id IS NULL)",
            name=_TOOL_ACTOR_PAIR_CHECK,
        ),
        sa.CheckConstraint(
            "(task_id IS NULL) = (agent_session_id IS NULL)",
            name=_TOOL_SESSION_PAIR_CHECK,
        ),
    ]
    if with_context:
        definitions.extend(
            [
                sa.ForeignKeyConstraint(
                    [_COLUMN, "investigation_id"],
                    [f"{_TASKS}.id", f"{_TASKS}.investigation_id"],
                    name=_CONTEXT_FK,
                    ondelete="RESTRICT",
                ),
                sa.CheckConstraint(_CONTEXT_CHECK_SQL, name=_CONTEXT_CHECK),
            ]
        )
    for name, index_columns in _EXISTING_INDEXES:
        definitions.append(sa.Index(name, *index_columns))
    if with_context:
        definitions.append(sa.Index(_CONTEXT_INDEX, "investigation_id", _COLUMN))
    return sa.Table(_TABLE, _metadata_with_parents(), *definitions)


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
        (
            str(index.get("name")),
            tuple(
                str(column) for column in cast(list[str] | None, index.get("column_names")) or ()
            ),
            bool(index.get("unique")),
        )
        for index in inspector.get_indexes(table_name)
        if not index.get("duplicates_constraint")
    }


def _schema_mismatches(bind: Any, table: sa.Table) -> list[str]:
    """Every expected column/constraint/index must exist with its exact shape.

    Extra columns and constraints are tolerated so a later additive revision
    still recognizes its predecessor's shape.
    """
    inspector = sa.inspect(bind)
    mismatches: list[str] = []
    if not inspector.has_table(table.name):
        return [f"{table.name}: expected present, actual absent"]
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


def _present_new_objects(bind: Connection) -> bool:
    inspector = sa.inspect(bind)
    if not inspector.has_table(_TABLE):
        return False
    if _COLUMN in {item["name"] for item in inspector.get_columns(_TABLE)}:
        return True
    if _CONTEXT_CHECK in {check.get("name") for check in inspector.get_check_constraints(_TABLE)}:
        return True
    if _CONTEXT_FK in {fk.get("name") for fk in inspector.get_foreign_keys(_TABLE)}:
        return True
    return _CONTEXT_INDEX in {index.get("name") for index in inspector.get_indexes(_TABLE)}


def _old_shape_mismatches(bind: Connection) -> list[str]:
    mismatches = _schema_mismatches(bind, _expected_table(with_context=False))
    inspector = sa.inspect(bind)
    if _COLUMN in {item["name"] for item in inspector.get_columns(_TABLE)}:
        mismatches.append(f"{_TABLE}: column {_COLUMN}: unexpected definition")
    if _CONTEXT_CHECK in {check.get("name") for check in inspector.get_check_constraints(_TABLE)}:
        mismatches.append(f"{_TABLE}: check {_CONTEXT_CHECK}: unexpected definition")
    if _CONTEXT_FK in {fk.get("name") for fk in inspector.get_foreign_keys(_TABLE)}:
        mismatches.append(f"{_TABLE}: foreign key {_CONTEXT_FK}: unexpected definition")
    if _CONTEXT_INDEX in {index.get("name") for index in inspector.get_indexes(_TABLE)}:
        mismatches.append(f"{_TABLE}: index {_CONTEXT_INDEX}: unexpected definition")
    return mismatches


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table(_TABLE):
        raise RuntimeError(
            f"task context preflight rejected: {_TABLE}: expected present, actual absent"
        )
    if _present_new_objects(bind):
        mismatches = _schema_mismatches(bind, _expected_table(with_context=True))
        if mismatches:
            raise RuntimeError("task context preflight rejected: " + "; ".join(mismatches))
        return
    mismatches = _old_shape_mismatches(bind)
    if mismatches:
        raise RuntimeError("task context preflight rejected: " + "; ".join(mismatches))

    bind.execute(sa.text(f"LOCK TABLE {_TABLE} IN ACCESS EXCLUSIVE MODE"))
    op.add_column(_TABLE, sa.Column(_COLUMN, sa.Uuid(), nullable=True))
    op.create_foreign_key(
        _CONTEXT_FK,
        _TABLE,
        _TASKS,
        [_COLUMN, "investigation_id"],
        ["id", "investigation_id"],
        ondelete="RESTRICT",
    )
    op.create_check_constraint(_CONTEXT_CHECK, _TABLE, _CONTEXT_CHECK_SQL)
    op.create_index(_CONTEXT_INDEX, _TABLE, ["investigation_id", _COLUMN])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table(_TABLE):
        raise RuntimeError("refusing to downgrade a missing tool invocation schema")
    mismatches = _schema_mismatches(bind, _expected_table(with_context=True))
    if mismatches:
        raise RuntimeError(
            "refusing to downgrade a partial or changed task context schema: "
            + "; ".join(mismatches)
        )

    bind.execute(sa.text(f"LOCK TABLE {_TABLE} IN ACCESS EXCLUSIVE MODE"))
    if bind.execute(
        sa.text(f"SELECT count(*) FROM {_TABLE} WHERE {_COLUMN} IS NOT NULL")
    ).scalar_one():
        raise RuntimeError(f"refusing to downgrade {_TABLE} while rows carry a {_COLUMN}")

    op.drop_index(_CONTEXT_INDEX, table_name=_TABLE)
    op.drop_constraint(_CONTEXT_CHECK, _TABLE, type_="check")
    op.drop_constraint(_CONTEXT_FK, _TABLE, type_="foreignkey")
    op.drop_column(_TABLE, _COLUMN)
