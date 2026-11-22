"""Create agent sessions, the instruction queue and invocation attribution (M24-05B1).

Revision ID: 0023_agent_sessions
Revises: 0022_investigation_handoffs

``agent_sessions`` is the sole authority for a session's technical state
(ADR-0011 D1/D2): one active session per task is a partial unique index, and
``(task_id, idempotency_key)`` makes prepare idempotent.  The revision also
adds the deferred ``investigation_tasks.lease_session_id`` foreign key that
0021 deliberately omitted, the cumulative ``tool_invocations`` attribution
pair ``(task_id IS NULL) = (agent_session_id IS NULL)`` (M9/E4) with its
``(task_id, created_at)`` index, and ``agent_session_instructions`` — the
durable mid-session queue the product mandate requires (the D2 sketch lists the
session table only; the queue is a separate child table so a busy agent never
loses an instruction and each enqueue is idempotent).

ADR-0007 discipline: an already-conforming shape is accepted without DDL, any
partial or different shape fails before mutation, and the downgrade refuses
while any session, instruction, attributed invocation or bound task remains.
The preflight tolerates objects added by *later* additive revisions on the
tables it references.
"""

from __future__ import annotations

import re
from typing import Any, cast

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import Connection

revision = "0023_agent_sessions"
down_revision = "0022_investigation_handoffs"
branch_labels = None
depends_on = None

_INVESTIGATIONS = "analyst_investigations"
_TASKS = "investigation_tasks"
_HANDOFFS = "investigation_handoffs"
_INVOCATIONS = "tool_invocations"
_SESSIONS = "agent_sessions"
_INSTRUCTIONS = "agent_session_instructions"

_TASK_LEASE_FK = "fk_investigation_tasks_lease_session"
_INVOCATION_FK = "fk_tool_invocations_agent_session"
_INVOCATION_CHECK = "ck_tool_invocations_agent_session_pair"
_INVOCATION_INDEX = "ix_tool_invocations_task_created"
_ACTIVE_INDEX = "uq_agent_sessions_active_task"

_AGENT_SESSION_STATE_VALUES = (
    "'provisioning', 'ready', 'running', 'awaiting_instruction', "
    "'interrupt_requested', 'stopped', 'lost', 'reconciling', 'closed', 'failed'"
)
_ACTIVE_AGENT_SESSION_STATES = {
    "provisioning",
    "ready",
    "running",
    "awaiting_instruction",
    "interrupt_requested",
    "reconciling",
}
_ACTIVE_AGENT_SESSION_STATE_VALUES = (
    "'provisioning', 'ready', 'running', 'awaiting_instruction', "
    "'interrupt_requested', 'reconciling'"
)
_ADAPTER_KIND_VALUES = "'codex_cli', 'claude_code'"
_INSTRUCTION_STATE_VALUES = "'queued', 'delivered', 'acknowledged', 'cancelled'"
_INSTRUCTION_ACTOR_VALUES = "'human', 'operator'"

_PAIR_CHECK_SQL = "(task_id IS NULL) = (agent_session_id IS NULL)"


def _sha256_sql(column: str) -> str:
    return f"char_length({column}) = 64 AND {column} ~ '^[0-9a-f]{{64}}$'"


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


def _expected_sessions_table() -> sa.Table:
    metadata = sa.MetaData()
    # Minimal parent shapes so the expected foreign keys resolve inside this
    # private metadata; the real tables are validated by their own revisions.
    sa.Table(_INVESTIGATIONS, metadata, sa.Column("id", sa.Uuid(), primary_key=True))
    sa.Table(
        _TASKS,
        metadata,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("investigation_id", sa.Uuid(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "investigation_id", name="uq_investigation_tasks_identity"),
    )
    sa.Table(_HANDOFFS, metadata, sa.Column("id", sa.Uuid(), primary_key=True))
    table = sa.Table(
        _SESSIONS,
        metadata,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("investigation_id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("adapter_kind", sa.String(length=16), nullable=False),
        sa.Column("host_session_ref", sa.Text(), nullable=True),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("state_version", sa.BigInteger(), nullable=False),
        sa.Column("capability_probe", JSONB(), nullable=False),
        sa.Column("capability_sha256", sa.String(length=64), nullable=False),
        sa.Column("probed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("handoff_id", sa.Uuid(), nullable=True),
        sa.Column("task_lease_id", sa.Uuid(), nullable=True),
        sa.Column("task_lease_fence", sa.BigInteger(), nullable=True),
        sa.Column("checkpoint_ref", sa.Text(), nullable=True),
        sa.Column("checkpoint_sha256", sa.String(length=64), nullable=True),
        sa.Column("reconciliation", JSONB(none_as_null=True), nullable=True),
        sa.Column("reconciliation_sha256", sa.String(length=64), nullable=True),
        sa.Column("auth_secret_nonce", sa.LargeBinary(), nullable=False),
        sa.Column("auth_secret_hash", sa.String(length=64), nullable=False),
        sa.Column("auth_secret_issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_activity_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("close_reason", sa.Text(), nullable=True),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_agent_sessions"),
        sa.UniqueConstraint(
            "task_id", "idempotency_key", name="uq_agent_sessions_task_idempotency"
        ),
        sa.UniqueConstraint("host_session_ref", name="uq_agent_sessions_host_session_ref"),
        sa.UniqueConstraint("id", "investigation_id", "task_id", name="uq_agent_sessions_identity"),
        sa.ForeignKeyConstraint(
            ["investigation_id"],
            [f"{_INVESTIGATIONS}.id"],
            name="fk_agent_sessions_investigation",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["task_id", "investigation_id"],
            [f"{_TASKS}.id", f"{_TASKS}.investigation_id"],
            name="fk_agent_sessions_task",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["handoff_id"],
            [f"{_HANDOFFS}.id"],
            name="fk_agent_sessions_handoff",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            f"adapter_kind IN ({_ADAPTER_KIND_VALUES})",
            name="ck_agent_sessions_adapter_kind",
        ),
        sa.CheckConstraint(
            f"state IN ({_AGENT_SESSION_STATE_VALUES})", name="ck_agent_sessions_state"
        ),
        sa.CheckConstraint("state_version >= 1", name="ck_agent_sessions_state_version"),
        sa.CheckConstraint(
            "jsonb_typeof(capability_probe) = 'object'",
            name="ck_agent_sessions_capability_probe_object",
        ),
        sa.CheckConstraint(
            _sha256_sql("capability_sha256"), name="ck_agent_sessions_capability_sha256"
        ),
        sa.CheckConstraint(
            "octet_length(auth_secret_nonce) = 32",
            name="ck_agent_sessions_auth_secret_nonce",
        ),
        sa.CheckConstraint(
            _sha256_sql("auth_secret_hash"), name="ck_agent_sessions_auth_secret_hash"
        ),
        sa.CheckConstraint(
            "(task_lease_id IS NULL) = (task_lease_fence IS NULL)",
            name="ck_agent_sessions_task_lease_pair",
        ),
        sa.CheckConstraint(
            "task_lease_fence IS NULL OR task_lease_fence >= 1",
            name="ck_agent_sessions_task_lease_fence",
        ),
        sa.CheckConstraint(
            "(checkpoint_ref IS NULL) = (checkpoint_sha256 IS NULL)",
            name="ck_agent_sessions_checkpoint_pair",
        ),
        sa.CheckConstraint(
            f"checkpoint_sha256 IS NULL OR ({_sha256_sql('checkpoint_sha256')})",
            name="ck_agent_sessions_checkpoint_sha256",
        ),
        sa.CheckConstraint(
            "(reconciliation IS NULL) = (reconciliation_sha256 IS NULL)",
            name="ck_agent_sessions_reconciliation_pair",
        ),
        sa.CheckConstraint(
            "reconciliation IS NULL OR jsonb_typeof(reconciliation) = 'object'",
            name="ck_agent_sessions_reconciliation_object",
        ),
        sa.CheckConstraint(
            f"reconciliation_sha256 IS NULL OR ({_sha256_sql('reconciliation_sha256')})",
            name="ck_agent_sessions_reconciliation_sha256",
        ),
        sa.CheckConstraint(
            "(state IN ('closed', 'failed')) = (closed_at IS NOT NULL)",
            name="ck_agent_sessions_closed_at",
        ),
        sa.CheckConstraint(
            "close_reason IS NULL OR state IN ('closed', 'failed', 'lost')",
            name="ck_agent_sessions_close_reason",
        ),
        sa.Index(
            _ACTIVE_INDEX,
            "task_id",
            unique=True,
            postgresql_where=sa.text(f"state IN ({_ACTIVE_AGENT_SESSION_STATE_VALUES})"),
        ),
        sa.Index(
            "ix_agent_sessions_investigation_created",
            "investigation_id",
            "created_at",
            "id",
        ),
        sa.Index("ix_agent_sessions_task_created", "task_id", "created_at", "id"),
    )
    return table


def _expected_instructions_table() -> sa.Table:
    metadata = sa.MetaData()
    sa.Table(_INVESTIGATIONS, metadata, sa.Column("id", sa.Uuid(), primary_key=True))
    sa.Table(
        _TASKS,
        metadata,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("investigation_id", sa.Uuid(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "investigation_id", name="uq_investigation_tasks_identity"),
    )
    sa.Table(
        _SESSIONS,
        metadata,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("investigation_id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.UniqueConstraint("id", "investigation_id", "task_id", name="uq_agent_sessions_identity"),
        sa.PrimaryKeyConstraint("id"),
    )
    return sa.Table(
        _INSTRUCTIONS,
        metadata,
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
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_agent_session_instructions"),
        sa.UniqueConstraint(
            "session_id",
            "idempotency_key",
            name="uq_agent_session_instructions_session_idempotency",
        ),
        sa.ForeignKeyConstraint(
            ["task_id", "investigation_id"],
            [f"{_TASKS}.id", f"{_TASKS}.investigation_id"],
            name="fk_agent_session_instructions_task",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["session_id", "investigation_id", "task_id"],
            [
                f"{_SESSIONS}.id",
                f"{_SESSIONS}.investigation_id",
                f"{_SESSIONS}.task_id",
            ],
            name="fk_agent_session_instructions_session",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            f"state IN ({_INSTRUCTION_STATE_VALUES})",
            name="ck_agent_session_instructions_state",
        ),
        sa.CheckConstraint(
            f"created_by_actor_kind IN ({_INSTRUCTION_ACTOR_VALUES})",
            name="ck_agent_session_instructions_actor_kind",
        ),
        sa.CheckConstraint(
            "octet_length(body) >= 1 AND octet_length(body) <= 65536",
            name="ck_agent_session_instructions_body_size",
        ),
        sa.CheckConstraint(
            _sha256_sql("body_sha256"),
            name="ck_agent_session_instructions_body_sha256",
        ),
        sa.CheckConstraint(
            "state <> 'queued' OR delivered_at IS NULL",
            name="ck_agent_session_instructions_queued",
        ),
        sa.CheckConstraint(
            "state NOT IN ('delivered', 'acknowledged') OR delivered_at IS NOT NULL",
            name="ck_agent_session_instructions_delivered",
        ),
        sa.CheckConstraint(
            "(state = 'acknowledged') = (acknowledged_at IS NOT NULL)",
            name="ck_agent_session_instructions_acknowledged",
        ),
        sa.Index(
            "ix_agent_session_instructions_session_queue",
            "session_id",
            "queued_at",
            "id",
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
        (index.get("name"), tuple(index.get("column_names", ())), bool(index.get("unique")))
        for index in inspector.get_indexes(table_name)
        if not index.get("duplicates_constraint")
    }


def _schema_mismatches(bind: Any, table: sa.Table) -> list[str]:
    """Every expected column/constraint/index must exist with its exact shape.

    Extra columns and constraints are tolerated so re-running 0023 over a
    schema extended by later additive revisions still recognises its shape.
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


def _active_index_mismatches(bind: Connection) -> list[str]:
    row = bind.execute(
        sa.text(
            "SELECT indexdef FROM pg_indexes "
            "WHERE schemaname = current_schema() AND indexname = :name"
        ),
        {"name": _ACTIVE_INDEX},
    ).first()
    if row is None:
        return [f"index {_ACTIVE_INDEX}: expected present, actual absent"]
    definition = str(row.indexdef)
    if "unique" not in definition.lower() or " where " not in definition.lower():
        return [f"index {_ACTIVE_INDEX}: expected a unique partial index"]
    predicate = definition.lower().split(" where ", maxsplit=1)[1]
    values = set(re.findall(r"'([a-z_]+)'", predicate))
    if values != _ACTIVE_AGENT_SESSION_STATES:
        return [
            f"index {_ACTIVE_INDEX}: expected active states "
            + ", ".join(sorted(_ACTIVE_AGENT_SESSION_STATES))
        ]
    return []


def _tool_invocation_attribution_mismatches(inspector: sa.Inspector) -> list[str]:
    if not inspector.has_table(_INVOCATIONS):
        return [f"{_INVOCATIONS}: expected present, actual absent"]
    columns = {item["name"]: item for item in inspector.get_columns(_INVOCATIONS)}
    column = columns.get("agent_session_id")
    if column is None:
        return [f"{_INVOCATIONS}: column agent_session_id: expected present, actual absent"]
    if not column["nullable"]:
        return [f"{_INVOCATIONS}: column agent_session_id: expected nullable"]
    foreign_keys = {
        foreign_key.get("name"): foreign_key
        for foreign_key in inspector.get_foreign_keys(_INVOCATIONS)
    }
    foreign_key = foreign_keys.get(_INVOCATION_FK)
    if foreign_key is None:
        return [f"{_INVOCATIONS}: foreign key {_INVOCATION_FK}: expected absent"]
    options = foreign_key.get("options", {})
    if (
        tuple(foreign_key.get("constrained_columns", ())) != ("agent_session_id",)
        or foreign_key.get("referred_table") != _SESSIONS
        or tuple(foreign_key.get("referred_columns", ())) != ("id",)
        or str(options.get("ondelete", "")).upper() != "RESTRICT"
    ):
        return [f"{_INVOCATIONS}: foreign key {_INVOCATION_FK}: shape differs"]
    checks = {check.get("name"): check for check in inspector.get_check_constraints(_INVOCATIONS)}
    check = checks.get(_INVOCATION_CHECK)
    if check is None:
        return [f"{_INVOCATIONS}: check {_INVOCATION_CHECK}: expected absent"]
    if _normalize_sql(cast(str | None, check.get("sqltext"))) != _normalize_sql(_PAIR_CHECK_SQL):
        return [f"{_INVOCATIONS}: check {_INVOCATION_CHECK}: shape differs"]
    indexes = {index.get("name"): index for index in inspector.get_indexes(_INVOCATIONS)}
    index = indexes.get(_INVOCATION_INDEX)
    if index is None:
        return [f"{_INVOCATIONS}: index {_INVOCATION_INDEX}: expected absent"]
    if tuple(index.get("column_names", ())) != ("task_id", "created_at"):
        return [f"{_INVOCATIONS}: index {_INVOCATION_INDEX}: shape differs"]
    return []


def _task_lease_fk_mismatches(inspector: sa.Inspector) -> list[str]:
    if not inspector.has_table(_TASKS):
        return [f"{_TASKS}: expected present, actual absent"]
    foreign_keys = {
        foreign_key.get("name"): foreign_key for foreign_key in inspector.get_foreign_keys(_TASKS)
    }
    foreign_key = foreign_keys.get(_TASK_LEASE_FK)
    if foreign_key is None:
        return [f"{_TASKS}: foreign key {_TASK_LEASE_FK}: expected absent"]
    options = foreign_key.get("options", {})
    if (
        tuple(foreign_key.get("constrained_columns", ())) != ("lease_session_id",)
        or foreign_key.get("referred_table") != _SESSIONS
        or tuple(foreign_key.get("referred_columns", ())) != ("id",)
        or str(options.get("ondelete", "")).upper() != "RESTRICT"
    ):
        return [f"{_TASKS}: foreign key {_TASK_LEASE_FK}: shape differs"]
    return []


def _present_objects(bind: Connection) -> bool:
    inspector = sa.inspect(bind)
    if inspector.has_table(_SESSIONS) or inspector.has_table(_INSTRUCTIONS):
        return True
    if inspector.has_table(_INVOCATIONS):
        if "agent_session_id" in {item["name"] for item in inspector.get_columns(_INVOCATIONS)}:
            return True
    if inspector.has_table(_TASKS):
        if _TASK_LEASE_FK in {
            foreign_key.get("name") for foreign_key in inspector.get_foreign_keys(_TASKS)
        }:
            return True
    return False


def _full_shape_mismatches(bind: Connection) -> list[str]:
    inspector = sa.inspect(bind)
    mismatches: list[str] = []
    mismatches.extend(_schema_mismatches(bind, _expected_sessions_table()))
    mismatches.extend(_schema_mismatches(bind, _expected_instructions_table()))
    for name in (_INVESTIGATIONS, _TASKS, _HANDOFFS, _INVOCATIONS):
        if not inspector.has_table(name):
            mismatches.append(f"{name}: expected present, actual absent")
    if inspector.has_table(_SESSIONS):
        mismatches.extend(_active_index_mismatches(bind))
    mismatches.extend(_tool_invocation_attribution_mismatches(inspector))
    mismatches.extend(_task_lease_fk_mismatches(inspector))
    return mismatches


def _baseline_mismatches(bind: Connection) -> list[str]:
    inspector = sa.inspect(bind)
    mismatches: list[str] = [
        f"{name}: expected present, actual absent"
        for name in (_INVESTIGATIONS, _TASKS, _HANDOFFS, _INVOCATIONS)
        if not inspector.has_table(name)
    ]
    for name in (_SESSIONS, _INSTRUCTIONS):
        if inspector.has_table(name):
            mismatches.append(f"{name}: unexpected table")
    if inspector.has_table(_INVOCATIONS):
        columns = {item["name"] for item in inspector.get_columns(_INVOCATIONS)}
        if "agent_session_id" in columns:
            mismatches.append(f"{_INVOCATIONS}: unexpected column agent_session_id")
        if _INVOCATION_CHECK in {
            check.get("name") for check in inspector.get_check_constraints(_INVOCATIONS)
        }:
            mismatches.append(f"{_INVOCATIONS}: unexpected check {_INVOCATION_CHECK}")
        if _INVOCATION_INDEX in {
            index.get("name") for index in inspector.get_indexes(_INVOCATIONS)
        }:
            mismatches.append(f"{_INVOCATIONS}: unexpected index {_INVOCATION_INDEX}")
    if inspector.has_table(_TASKS) and _TASK_LEASE_FK in {
        foreign_key.get("name") for foreign_key in inspector.get_foreign_keys(_TASKS)
    }:
        mismatches.append(f"{_TASKS}: unexpected foreign key {_TASK_LEASE_FK}")
    return mismatches


def _create_sessions() -> None:
    op.create_table(
        _SESSIONS,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("investigation_id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("adapter_kind", sa.String(length=16), nullable=False),
        sa.Column("host_session_ref", sa.Text(), nullable=True),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("state_version", sa.BigInteger(), nullable=False),
        sa.Column("capability_probe", JSONB(), nullable=False),
        sa.Column("capability_sha256", sa.String(length=64), nullable=False),
        sa.Column("probed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("handoff_id", sa.Uuid(), nullable=True),
        sa.Column("task_lease_id", sa.Uuid(), nullable=True),
        sa.Column("task_lease_fence", sa.BigInteger(), nullable=True),
        sa.Column("checkpoint_ref", sa.Text(), nullable=True),
        sa.Column("checkpoint_sha256", sa.String(length=64), nullable=True),
        sa.Column("reconciliation", JSONB(none_as_null=True), nullable=True),
        sa.Column("reconciliation_sha256", sa.String(length=64), nullable=True),
        sa.Column("auth_secret_nonce", sa.LargeBinary(), nullable=False),
        sa.Column("auth_secret_hash", sa.String(length=64), nullable=False),
        sa.Column("auth_secret_issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_activity_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("close_reason", sa.Text(), nullable=True),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_agent_sessions"),
        sa.UniqueConstraint(
            "task_id", "idempotency_key", name="uq_agent_sessions_task_idempotency"
        ),
        sa.UniqueConstraint("host_session_ref", name="uq_agent_sessions_host_session_ref"),
        sa.UniqueConstraint("id", "investigation_id", "task_id", name="uq_agent_sessions_identity"),
        sa.ForeignKeyConstraint(
            ["investigation_id"],
            [f"{_INVESTIGATIONS}.id"],
            name="fk_agent_sessions_investigation",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["task_id", "investigation_id"],
            [f"{_TASKS}.id", f"{_TASKS}.investigation_id"],
            name="fk_agent_sessions_task",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["handoff_id"],
            [f"{_HANDOFFS}.id"],
            name="fk_agent_sessions_handoff",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            f"adapter_kind IN ({_ADAPTER_KIND_VALUES})",
            name="ck_agent_sessions_adapter_kind",
        ),
        sa.CheckConstraint(
            f"state IN ({_AGENT_SESSION_STATE_VALUES})", name="ck_agent_sessions_state"
        ),
        sa.CheckConstraint("state_version >= 1", name="ck_agent_sessions_state_version"),
        sa.CheckConstraint(
            "jsonb_typeof(capability_probe) = 'object'",
            name="ck_agent_sessions_capability_probe_object",
        ),
        sa.CheckConstraint(
            _sha256_sql("capability_sha256"), name="ck_agent_sessions_capability_sha256"
        ),
        sa.CheckConstraint(
            "octet_length(auth_secret_nonce) = 32",
            name="ck_agent_sessions_auth_secret_nonce",
        ),
        sa.CheckConstraint(
            _sha256_sql("auth_secret_hash"), name="ck_agent_sessions_auth_secret_hash"
        ),
        sa.CheckConstraint(
            "(task_lease_id IS NULL) = (task_lease_fence IS NULL)",
            name="ck_agent_sessions_task_lease_pair",
        ),
        sa.CheckConstraint(
            "task_lease_fence IS NULL OR task_lease_fence >= 1",
            name="ck_agent_sessions_task_lease_fence",
        ),
        sa.CheckConstraint(
            "(checkpoint_ref IS NULL) = (checkpoint_sha256 IS NULL)",
            name="ck_agent_sessions_checkpoint_pair",
        ),
        sa.CheckConstraint(
            f"checkpoint_sha256 IS NULL OR ({_sha256_sql('checkpoint_sha256')})",
            name="ck_agent_sessions_checkpoint_sha256",
        ),
        sa.CheckConstraint(
            "(reconciliation IS NULL) = (reconciliation_sha256 IS NULL)",
            name="ck_agent_sessions_reconciliation_pair",
        ),
        sa.CheckConstraint(
            "reconciliation IS NULL OR jsonb_typeof(reconciliation) = 'object'",
            name="ck_agent_sessions_reconciliation_object",
        ),
        sa.CheckConstraint(
            f"reconciliation_sha256 IS NULL OR ({_sha256_sql('reconciliation_sha256')})",
            name="ck_agent_sessions_reconciliation_sha256",
        ),
        sa.CheckConstraint(
            "(state IN ('closed', 'failed')) = (closed_at IS NOT NULL)",
            name="ck_agent_sessions_closed_at",
        ),
        sa.CheckConstraint(
            "close_reason IS NULL OR state IN ('closed', 'failed', 'lost')",
            name="ck_agent_sessions_close_reason",
        ),
    )
    op.create_index(
        _ACTIVE_INDEX,
        _SESSIONS,
        ["task_id"],
        unique=True,
        postgresql_where=sa.text(f"state IN ({_ACTIVE_AGENT_SESSION_STATE_VALUES})"),
    )
    op.create_index(
        "ix_agent_sessions_investigation_created",
        _SESSIONS,
        ["investigation_id", "created_at", "id"],
    )
    op.create_index(
        "ix_agent_sessions_task_created",
        _SESSIONS,
        ["task_id", "created_at", "id"],
    )


def _create_instructions() -> None:
    op.create_table(
        _INSTRUCTIONS,
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
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_agent_session_instructions"),
        sa.UniqueConstraint(
            "session_id",
            "idempotency_key",
            name="uq_agent_session_instructions_session_idempotency",
        ),
        sa.ForeignKeyConstraint(
            ["task_id", "investigation_id"],
            [f"{_TASKS}.id", f"{_TASKS}.investigation_id"],
            name="fk_agent_session_instructions_task",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["session_id", "investigation_id", "task_id"],
            [
                f"{_SESSIONS}.id",
                f"{_SESSIONS}.investigation_id",
                f"{_SESSIONS}.task_id",
            ],
            name="fk_agent_session_instructions_session",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            f"state IN ({_INSTRUCTION_STATE_VALUES})",
            name="ck_agent_session_instructions_state",
        ),
        sa.CheckConstraint(
            f"created_by_actor_kind IN ({_INSTRUCTION_ACTOR_VALUES})",
            name="ck_agent_session_instructions_actor_kind",
        ),
        sa.CheckConstraint(
            "octet_length(body) >= 1 AND octet_length(body) <= 65536",
            name="ck_agent_session_instructions_body_size",
        ),
        sa.CheckConstraint(
            _sha256_sql("body_sha256"),
            name="ck_agent_session_instructions_body_sha256",
        ),
        sa.CheckConstraint(
            "state <> 'queued' OR delivered_at IS NULL",
            name="ck_agent_session_instructions_queued",
        ),
        sa.CheckConstraint(
            "state NOT IN ('delivered', 'acknowledged') OR delivered_at IS NOT NULL",
            name="ck_agent_session_instructions_delivered",
        ),
        sa.CheckConstraint(
            "(state = 'acknowledged') = (acknowledged_at IS NOT NULL)",
            name="ck_agent_session_instructions_acknowledged",
        ),
    )
    op.create_index(
        "ix_agent_session_instructions_session_queue",
        _INSTRUCTIONS,
        ["session_id", "queued_at", "id"],
    )


def upgrade() -> None:
    bind = op.get_bind()
    if _present_objects(bind):
        mismatches = _full_shape_mismatches(bind)
        if mismatches:
            raise RuntimeError("agent session preflight rejected: " + "; ".join(mismatches))
        return
    mismatches = _baseline_mismatches(bind)
    if mismatches:
        raise RuntimeError("agent session preflight rejected: " + "; ".join(mismatches))

    bind.execute(
        sa.text(
            f"LOCK TABLE {_INVESTIGATIONS}, {_TASKS}, {_HANDOFFS}, {_INVOCATIONS} "
            "IN ACCESS EXCLUSIVE MODE"
        )
    )
    _create_sessions()
    _create_instructions()
    op.add_column(_INVOCATIONS, sa.Column("agent_session_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        _INVOCATION_FK,
        _INVOCATIONS,
        _SESSIONS,
        ["agent_session_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_check_constraint(_INVOCATION_CHECK, _INVOCATIONS, _PAIR_CHECK_SQL)
    op.create_index(_INVOCATION_INDEX, _INVOCATIONS, ["task_id", "created_at"])
    op.create_foreign_key(
        _TASK_LEASE_FK,
        _TASKS,
        _SESSIONS,
        ["lease_session_id"],
        ["id"],
        ondelete="RESTRICT",
    )


def downgrade() -> None:
    bind = op.get_bind()
    if not _present_objects(bind):
        raise RuntimeError("refusing to downgrade a missing agent session schema")
    mismatches = _full_shape_mismatches(bind)
    if mismatches:
        raise RuntimeError(
            "refusing to downgrade a partial or changed agent session schema: "
            + "; ".join(mismatches)
        )

    bind.execute(
        sa.text(
            f"LOCK TABLE {_INVESTIGATIONS}, {_TASKS}, {_HANDOFFS}, {_INVOCATIONS}, "
            f"{_SESSIONS}, {_INSTRUCTIONS} IN ACCESS EXCLUSIVE MODE"
        )
    )
    if bind.execute(sa.text(f"SELECT count(*) FROM {_SESSIONS}")).scalar_one():
        raise RuntimeError(f"refusing to downgrade {_SESSIONS} after it has been used")
    if bind.execute(sa.text(f"SELECT count(*) FROM {_INSTRUCTIONS}")).scalar_one():
        raise RuntimeError(f"refusing to downgrade {_INSTRUCTIONS} after it has been used")
    if bind.execute(
        sa.text(f"SELECT count(*) FROM {_INVOCATIONS} WHERE agent_session_id IS NOT NULL")
    ).scalar_one():
        raise RuntimeError("refusing to downgrade while invocations name an agent session")
    if bind.execute(
        sa.text(f"SELECT count(*) FROM {_TASKS} WHERE lease_session_id IS NOT NULL")
    ).scalar_one():
        raise RuntimeError("refusing to downgrade while tasks name a lease session")

    op.drop_constraint(_TASK_LEASE_FK, _TASKS, type_="foreignkey")
    op.drop_index(_INVOCATION_INDEX, table_name=_INVOCATIONS)
    op.drop_constraint(_INVOCATION_CHECK, _INVOCATIONS, type_="check")
    op.drop_constraint(_INVOCATION_FK, _INVOCATIONS, type_="foreignkey")
    op.drop_column(_INVOCATIONS, "agent_session_id")
    op.drop_table(_INSTRUCTIONS)
    op.drop_table(_SESSIONS)
