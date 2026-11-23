"""Add the instruction delivery state machine to the agent session queue (M24.1 ID-1).

Revision ID: 0027_instruction_delivery
Revises: 0026_workspace_note_annotations

F3-D (docs/design/investigation-workshop/INSTRUCTION-DELIVERY.md) replaces the
four open instruction states with six control states — ``queued``,
``delivering``, ``delivered``, ``acknowledged``, ``delivery_unknown``,
``cancelled`` — and records the attempt that produced a delivery: token,
attempt count, exact delivered-text SHA-256, note diffs, timestamps, error and
cancellation reason.  ``backend/AGENTS.md`` forbids encoding those control
states as free-form strings or metadata keys, so they are typed here.

Two CHECK details of the design are deliberately narrower than §4 states, to
respect provenance already persisted by 0023:

* ``delivery_token``/``delivery_mode``/``delivery_sha256``/
  ``delivery_note_diffs`` are never invented for pre-0027 rows.  The design's
  "non-null exactly in delivered|acknowledged" would force the migration to
  fabricate a token and a mode (and a body digest for a note-diff delivery it
  never persisted) for existing ``delivered``/``acknowledged`` rows.  The
  constraints instead require the token and digest for the in-flight
  ``delivering``/``delivery_unknown`` states and forbid every delivery field
  outside its legal states; a proven row may keep them.
* ``delivery_sha256`` is recorded at reservation (§3.3 phase A: "CAS
  queued -> delivering (jeton, essai +1, horodatage, SHA)"), so it must be
  legal in ``delivering``/``delivery_unknown`` and not only in
  ``delivered``/``acknowledged`` as §4's summary sentence suggests.

A pre-0027 ``cancelled`` row carries no reason and cannot satisfy the new
"reason exactly when cancelled" contract; the upgrade refuses it explicitly
instead of inventing a human decision.

ADR-0007 discipline: the exact 0023 shape is verified before any DDL, a
conforming 0027 shape is accepted without DDL, any partial or different shape
fails before mutation, and the downgrade refuses while the table holds any row
(the same guard 0023 applies).
"""

from __future__ import annotations

import re
from typing import Any, cast

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import Connection

revision = "0027_instruction_delivery"
down_revision = "0026_workspace_note_annotations"
branch_labels = None
depends_on = None

_TABLE = "agent_session_instructions"
_PENDING_INDEX = "ix_agent_session_instructions_pending"
_QUEUE_INDEX = "ix_agent_session_instructions_session_queue"

_OLD_STATE_VALUES = "'queued', 'delivered', 'acknowledged', 'cancelled'"
_NEW_STATE_VALUES = (
    "'queued', 'delivering', 'delivered', 'acknowledged', 'delivery_unknown', 'cancelled'"
)
_DELIVERY_MODE_VALUES = "'live', 'next_run'"
_PENDING_STATE_VALUES = "'queued', 'delivering', 'delivery_unknown'"
_PENDING_STATES = {"queued", "delivering", "delivery_unknown"}

_OLD_STATE_CHECK = "ck_agent_session_instructions_state"
_ACTOR_KIND_CHECK = "ck_agent_session_instructions_actor_kind"
_BODY_SIZE_CHECK = "ck_agent_session_instructions_body_size"
_BODY_SHA256_CHECK = "ck_agent_session_instructions_body_sha256"
_OLD_QUEUED_CHECK = "ck_agent_session_instructions_queued"
_OLD_DELIVERED_CHECK = "ck_agent_session_instructions_delivered"
_ACKNOWLEDGED_CHECK = "ck_agent_session_instructions_acknowledged"
_DELIVERED_AT_CHECK = "ck_agent_session_instructions_delivered_at"
_DELIVERY_ATTEMPTS_CHECK = "ck_agent_session_instructions_delivery_attempts"
_DELIVERY_TOKEN_CHECK = "ck_agent_session_instructions_delivery_token"
_DELIVERY_MODE_CHECK = "ck_agent_session_instructions_delivery_mode"
_DELIVERY_SHA256_CHECK = "ck_agent_session_instructions_delivery_sha256"
_DELIVERY_DIFFS_CHECK = "ck_agent_session_instructions_delivery_note_diffs"
_DELIVERY_UNKNOWN_CHECK = "ck_agent_session_instructions_delivery_unknown_at"
_CANCELLED_AT_CHECK = "ck_agent_session_instructions_cancelled_at"
_CANCEL_REASON_CHECK = "ck_agent_session_instructions_cancel_reason"
_LAST_ERROR_CHECK = "ck_agent_session_instructions_last_delivery_error"

_OLD_CHECKS = frozenset(
    {
        _OLD_STATE_CHECK,
        _ACTOR_KIND_CHECK,
        _BODY_SIZE_CHECK,
        _BODY_SHA256_CHECK,
        _OLD_QUEUED_CHECK,
        _OLD_DELIVERED_CHECK,
        _ACKNOWLEDGED_CHECK,
    }
)
# Only the two checks dropped outright must be absent in the new shape; the
# state check keeps its name and is verified by its replacement definition.
_REPLACED_CHECKS = frozenset({_OLD_QUEUED_CHECK, _OLD_DELIVERED_CHECK})
_NEW_CHECKS = frozenset(
    {
        _OLD_STATE_CHECK,
        _ACTOR_KIND_CHECK,
        _BODY_SIZE_CHECK,
        _BODY_SHA256_CHECK,
        _DELIVERED_AT_CHECK,
        _ACKNOWLEDGED_CHECK,
        _DELIVERY_ATTEMPTS_CHECK,
        _DELIVERY_TOKEN_CHECK,
        _DELIVERY_MODE_CHECK,
        _DELIVERY_SHA256_CHECK,
        _DELIVERY_DIFFS_CHECK,
        _DELIVERY_UNKNOWN_CHECK,
        _CANCELLED_AT_CHECK,
        _CANCEL_REASON_CHECK,
        _LAST_ERROR_CHECK,
    }
)

_NEW_COLUMNS = frozenset(
    {
        "delivery_attempts",
        "delivery_token",
        "delivery_mode",
        "delivery_sha256",
        "delivery_note_diffs",
        "delivery_attempted_at",
        "delivery_unknown_at",
        "last_delivery_error",
        "cancelled_at",
        "cancel_reason",
    }
)

_SHA256_SQL = "char_length({column}) = 64 AND {column} ~ '^[0-9a-f]{{64}}$'"


def _sha256_sql(column: str) -> str:
    return _SHA256_SQL.format(column=column)


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


def _base_columns() -> tuple[Any, ...]:
    """The exact 0023 column list; both shapes start from it."""
    return (
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("investigation_id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("body_sha256", sa.String(length=64), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("created_by_actor_kind", sa.String(length=16), nullable=False),
        sa.Column("created_by_actor_id", sa.Text(), nullable=False),
        sa.Column("queued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True),
    )


def _base_constraints() -> tuple[Any, ...]:
    return (
        sa.PrimaryKeyConstraint("id", name="pk_agent_session_instructions"),
        sa.UniqueConstraint(
            "session_id",
            "idempotency_key",
            name="uq_agent_session_instructions_session_idempotency",
        ),
        sa.ForeignKeyConstraint(
            ["task_id", "investigation_id"],
            ["investigation_tasks.id", "investigation_tasks.investigation_id"],
            name="fk_agent_session_instructions_task",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["session_id", "investigation_id", "task_id"],
            [
                "agent_sessions.id",
                "agent_sessions.investigation_id",
                "agent_sessions.task_id",
            ],
            name="fk_agent_session_instructions_session",
            ondelete="RESTRICT",
        ),
    )


def _metadata_with_parents() -> sa.MetaData:
    metadata = sa.MetaData()
    sa.Table("analyst_investigations", metadata, sa.Column("id", sa.Uuid(), primary_key=True))
    sa.Table(
        "investigation_tasks",
        metadata,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("investigation_id", sa.Uuid(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "investigation_id", name="uq_investigation_tasks_identity"),
    )
    sa.Table(
        "agent_sessions",
        metadata,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("investigation_id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.UniqueConstraint("id", "investigation_id", "task_id", name="uq_agent_sessions_identity"),
        sa.PrimaryKeyConstraint("id"),
    )
    return metadata


def _expected_old_table() -> sa.Table:
    """The exact shape 0023 creates on ``agent_session_instructions``."""
    return sa.Table(
        _TABLE,
        _metadata_with_parents(),
        *_base_columns(),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        *_base_constraints(),
        sa.CheckConstraint(
            f"state IN ({_OLD_STATE_VALUES})",
            name=_OLD_STATE_CHECK,
        ),
        sa.CheckConstraint(
            "created_by_actor_kind IN ('human', 'operator')",
            name=_ACTOR_KIND_CHECK,
        ),
        sa.CheckConstraint(
            "octet_length(body) >= 1 AND octet_length(body) <= 65536",
            name=_BODY_SIZE_CHECK,
        ),
        sa.CheckConstraint(_sha256_sql("body_sha256"), name=_BODY_SHA256_CHECK),
        sa.CheckConstraint(
            "state <> 'queued' OR delivered_at IS NULL",
            name=_OLD_QUEUED_CHECK,
        ),
        sa.CheckConstraint(
            "state NOT IN ('delivered', 'acknowledged') OR delivered_at IS NOT NULL",
            name=_OLD_DELIVERED_CHECK,
        ),
        sa.CheckConstraint(
            "(state = 'acknowledged') = (acknowledged_at IS NOT NULL)",
            name=_ACKNOWLEDGED_CHECK,
        ),
        sa.Index(_QUEUE_INDEX, "session_id", "queued_at", "id"),
    )


def _expected_new_table() -> sa.Table:
    """The shape this revision leaves behind (columns, checks, indexes)."""
    return sa.Table(
        _TABLE,
        _metadata_with_parents(),
        *_base_columns(),
        sa.Column(
            "delivery_attempts",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("delivery_token", sa.Uuid(), nullable=True),
        sa.Column("delivery_mode", sa.Text(), nullable=True),
        sa.Column("delivery_sha256", sa.String(length=64), nullable=True),
        sa.Column("delivery_note_diffs", JSONB(none_as_null=True), nullable=True),
        sa.Column("delivery_attempted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("delivery_unknown_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_delivery_error", sa.Text(), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancel_reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        *_base_constraints(),
        sa.CheckConstraint(
            f"state IN ({_NEW_STATE_VALUES})",
            name=_OLD_STATE_CHECK,
        ),
        sa.CheckConstraint(
            "created_by_actor_kind IN ('human', 'operator')",
            name=_ACTOR_KIND_CHECK,
        ),
        sa.CheckConstraint(
            "octet_length(body) >= 1 AND octet_length(body) <= 65536",
            name=_BODY_SIZE_CHECK,
        ),
        sa.CheckConstraint(_sha256_sql("body_sha256"), name=_BODY_SHA256_CHECK),
        sa.CheckConstraint(
            "(state IN ('delivered', 'acknowledged')) = (delivered_at IS NOT NULL)",
            name=_DELIVERED_AT_CHECK,
        ),
        sa.CheckConstraint(
            "(state = 'acknowledged') = (acknowledged_at IS NOT NULL)",
            name=_ACKNOWLEDGED_CHECK,
        ),
        sa.CheckConstraint("delivery_attempts >= 0", name=_DELIVERY_ATTEMPTS_CHECK),
        sa.CheckConstraint(
            "(state NOT IN ('delivering', 'delivery_unknown') "
            "OR delivery_token IS NOT NULL) AND "
            "(delivery_token IS NULL OR state NOT IN ('queued', 'cancelled'))",
            name=_DELIVERY_TOKEN_CHECK,
        ),
        sa.CheckConstraint(
            f"(delivery_mode IS NULL OR delivery_mode IN ({_DELIVERY_MODE_VALUES})) "
            "AND (delivery_mode IS NULL OR state IN ('delivered', 'acknowledged'))",
            name=_DELIVERY_MODE_CHECK,
        ),
        sa.CheckConstraint(
            "(delivery_sha256 IS NULL OR (" + _sha256_sql("delivery_sha256") + ")) AND "
            "(delivery_sha256 IS NULL OR state IN "
            "('delivering', 'delivery_unknown', 'delivered', 'acknowledged')) AND "
            "(state NOT IN ('delivering', 'delivery_unknown') "
            "OR delivery_sha256 IS NOT NULL)",
            name=_DELIVERY_SHA256_CHECK,
        ),
        sa.CheckConstraint(
            "delivery_note_diffs IS NULL OR "
            "(jsonb_typeof(delivery_note_diffs) = 'array' "
            "AND state IN ('delivered', 'acknowledged'))",
            name=_DELIVERY_DIFFS_CHECK,
        ),
        sa.CheckConstraint(
            "(state = 'delivery_unknown') = (delivery_unknown_at IS NOT NULL)",
            name=_DELIVERY_UNKNOWN_CHECK,
        ),
        sa.CheckConstraint(
            "(state = 'cancelled') = (cancelled_at IS NOT NULL)",
            name=_CANCELLED_AT_CHECK,
        ),
        sa.CheckConstraint(
            "(cancel_reason IS NOT NULL) = (state = 'cancelled')",
            name=_CANCEL_REASON_CHECK,
        ),
        sa.CheckConstraint(
            "last_delivery_error IS NULL OR state NOT IN ('delivered', 'acknowledged')",
            name=_LAST_ERROR_CHECK,
        ),
        sa.Index(_QUEUE_INDEX, "session_id", "queued_at", "id"),
        sa.Index(
            _PENDING_INDEX,
            "state",
            "queued_at",
            "id",
            postgresql_where=sa.text(f"state IN ({_PENDING_STATE_VALUES})"),
        ),
    )


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
    columns = {item["name"] for item in inspector.get_columns(_TABLE)}
    if columns & _NEW_COLUMNS:
        return True
    if _PENDING_INDEX in {index.get("name") for index in inspector.get_indexes(_TABLE)}:
        return True
    return bool(
        (_NEW_CHECKS - _OLD_CHECKS)
        & {check.get("name") for check in inspector.get_check_constraints(_TABLE)}
    )


def _pending_index_mismatches(bind: Connection) -> list[str]:
    row = bind.execute(
        sa.text(
            "SELECT indexdef FROM pg_indexes "
            "WHERE schemaname = current_schema() AND indexname = :name"
        ),
        {"name": _PENDING_INDEX},
    ).first()
    if row is None:
        return [f"index {_PENDING_INDEX}: expected present, actual absent"]
    definition = str(row.indexdef).lower()
    if " where " not in definition:
        return [f"index {_PENDING_INDEX}: expected a partial index"]
    if "(state, queued_at, id)" not in definition:
        return [f"index {_PENDING_INDEX}: expected columns (state, queued_at, id)"]
    values = set(re.findall(r"'([a-z_]+)'", definition.split(" where ", maxsplit=1)[1]))
    if values != _PENDING_STATES:
        return [
            f"index {_PENDING_INDEX}: expected pending states " + ", ".join(sorted(_PENDING_STATES))
        ]
    return []


def _old_shape_mismatches(bind: Connection) -> list[str]:
    inspector = sa.inspect(bind)
    mismatches = _schema_mismatches(bind, _expected_old_table())
    actual_checks = {check.get("name") for check in inspector.get_check_constraints(_TABLE)}
    for name in sorted((_NEW_CHECKS - _OLD_CHECKS) & actual_checks):
        mismatches.append(f"{_TABLE}: check {name}: unexpected definition")
    if _PENDING_INDEX in {index.get("name") for index in inspector.get_indexes(_TABLE)}:
        mismatches.append(f"{_TABLE}: index {_PENDING_INDEX}: unexpected index")
    return mismatches


def _new_shape_mismatches(bind: Connection) -> list[str]:
    inspector = sa.inspect(bind)
    mismatches = _schema_mismatches(bind, _expected_new_table())
    actual_checks = {check.get("name") for check in inspector.get_check_constraints(_TABLE)}
    for name in sorted(_REPLACED_CHECKS & actual_checks):
        mismatches.append(f"{_TABLE}: check {name}: expected absent")
    mismatches.extend(_pending_index_mismatches(bind))
    return mismatches


def _add_delivery_columns() -> None:
    op.add_column(
        _TABLE,
        sa.Column(
            "delivery_attempts",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.add_column(_TABLE, sa.Column("delivery_token", sa.Uuid(), nullable=True))
    op.add_column(_TABLE, sa.Column("delivery_mode", sa.Text(), nullable=True))
    op.add_column(_TABLE, sa.Column("delivery_sha256", sa.String(length=64), nullable=True))
    op.add_column(
        _TABLE,
        sa.Column("delivery_note_diffs", JSONB(none_as_null=True), nullable=True),
    )
    op.add_column(
        _TABLE, sa.Column("delivery_attempted_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        _TABLE, sa.Column("delivery_unknown_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(_TABLE, sa.Column("last_delivery_error", sa.Text(), nullable=True))
    op.add_column(_TABLE, sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(_TABLE, sa.Column("cancel_reason", sa.Text(), nullable=True))


def _create_delivery_checks() -> None:
    op.create_check_constraint(_OLD_STATE_CHECK, _TABLE, f"state IN ({_NEW_STATE_VALUES})")
    op.create_check_constraint(
        _DELIVERED_AT_CHECK,
        _TABLE,
        "(state IN ('delivered', 'acknowledged')) = (delivered_at IS NOT NULL)",
    )
    op.create_check_constraint(_DELIVERY_ATTEMPTS_CHECK, _TABLE, "delivery_attempts >= 0")
    op.create_check_constraint(
        _DELIVERY_TOKEN_CHECK,
        _TABLE,
        "(state NOT IN ('delivering', 'delivery_unknown') "
        "OR delivery_token IS NOT NULL) AND "
        "(delivery_token IS NULL OR state NOT IN ('queued', 'cancelled'))",
    )
    op.create_check_constraint(
        _DELIVERY_MODE_CHECK,
        _TABLE,
        f"(delivery_mode IS NULL OR delivery_mode IN ({_DELIVERY_MODE_VALUES})) "
        "AND (delivery_mode IS NULL OR state IN ('delivered', 'acknowledged'))",
    )
    op.create_check_constraint(
        _DELIVERY_SHA256_CHECK,
        _TABLE,
        "(delivery_sha256 IS NULL OR (" + _sha256_sql("delivery_sha256") + ")) AND "
        "(delivery_sha256 IS NULL OR state IN "
        "('delivering', 'delivery_unknown', 'delivered', 'acknowledged')) AND "
        "(state NOT IN ('delivering', 'delivery_unknown') "
        "OR delivery_sha256 IS NOT NULL)",
    )
    op.create_check_constraint(
        _DELIVERY_DIFFS_CHECK,
        _TABLE,
        "delivery_note_diffs IS NULL OR "
        "(jsonb_typeof(delivery_note_diffs) = 'array' "
        "AND state IN ('delivered', 'acknowledged'))",
    )
    op.create_check_constraint(
        _DELIVERY_UNKNOWN_CHECK,
        _TABLE,
        "(state = 'delivery_unknown') = (delivery_unknown_at IS NOT NULL)",
    )
    op.create_check_constraint(
        _CANCELLED_AT_CHECK,
        _TABLE,
        "(state = 'cancelled') = (cancelled_at IS NOT NULL)",
    )
    op.create_check_constraint(
        _CANCEL_REASON_CHECK,
        _TABLE,
        "(cancel_reason IS NOT NULL) = (state = 'cancelled')",
    )
    op.create_check_constraint(
        _LAST_ERROR_CHECK,
        _TABLE,
        "last_delivery_error IS NULL OR state NOT IN ('delivered', 'acknowledged')",
    )


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table(_TABLE):
        raise RuntimeError(
            f"instruction delivery preflight rejected: {_TABLE}: expected present, actual absent"
        )
    if _present_new_objects(bind):
        mismatches = _new_shape_mismatches(bind)
        if mismatches:
            raise RuntimeError("instruction delivery preflight rejected: " + "; ".join(mismatches))
        return
    mismatches = _old_shape_mismatches(bind)
    if mismatches:
        raise RuntimeError("instruction delivery preflight rejected: " + "; ".join(mismatches))

    bind.execute(sa.text(f"LOCK TABLE {_TABLE} IN ACCESS EXCLUSIVE MODE"))
    if bind.execute(
        sa.text(f"SELECT count(*) FROM {_TABLE} WHERE state = 'cancelled'")
    ).scalar_one():
        raise RuntimeError(
            "instruction delivery preflight rejected: a cancelled instruction predates "
            "delivery tracking and carries no reason; resolve it before upgrading"
        )

    op.drop_constraint(_OLD_QUEUED_CHECK, _TABLE, type_="check")
    op.drop_constraint(_OLD_DELIVERED_CHECK, _TABLE, type_="check")
    op.drop_constraint(_OLD_STATE_CHECK, _TABLE, type_="check")
    _add_delivery_columns()
    _create_delivery_checks()
    op.create_index(
        _PENDING_INDEX,
        _TABLE,
        ["state", "queued_at", "id"],
        postgresql_where=sa.text(f"state IN ({_PENDING_STATE_VALUES})"),
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table(_TABLE):
        raise RuntimeError("refusing to downgrade a missing instruction delivery schema")
    mismatches = _new_shape_mismatches(bind)
    if mismatches:
        raise RuntimeError(
            "refusing to downgrade a partial or changed instruction delivery schema: "
            + "; ".join(mismatches)
        )

    bind.execute(sa.text(f"LOCK TABLE {_TABLE} IN ACCESS EXCLUSIVE MODE"))
    if bind.execute(sa.text(f"SELECT count(*) FROM {_TABLE}")).scalar_one():
        raise RuntimeError(f"refusing to downgrade {_TABLE} after it has been used")

    op.drop_index(_PENDING_INDEX, table_name=_TABLE)
    for name in (
        _DELIVERED_AT_CHECK,
        _DELIVERY_ATTEMPTS_CHECK,
        _DELIVERY_TOKEN_CHECK,
        _DELIVERY_MODE_CHECK,
        _DELIVERY_SHA256_CHECK,
        _DELIVERY_DIFFS_CHECK,
        _DELIVERY_UNKNOWN_CHECK,
        _CANCELLED_AT_CHECK,
        _CANCEL_REASON_CHECK,
        _LAST_ERROR_CHECK,
        _OLD_STATE_CHECK,
    ):
        op.drop_constraint(name, _TABLE, type_="check")
    for name in (
        "delivery_attempts",
        "delivery_token",
        "delivery_mode",
        "delivery_sha256",
        "delivery_note_diffs",
        "delivery_attempted_at",
        "delivery_unknown_at",
        "last_delivery_error",
        "cancelled_at",
        "cancel_reason",
    ):
        op.drop_column(_TABLE, name)
    op.create_check_constraint(_OLD_STATE_CHECK, _TABLE, f"state IN ({_OLD_STATE_VALUES})")
    op.create_check_constraint(
        _OLD_QUEUED_CHECK, _TABLE, "state <> 'queued' OR delivered_at IS NULL"
    )
    op.create_check_constraint(
        _OLD_DELIVERED_CHECK,
        _TABLE,
        "state NOT IN ('delivered', 'acknowledged') OR delivered_at IS NOT NULL",
    )
