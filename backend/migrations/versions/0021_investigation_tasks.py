"""Create durable investigation tasks, dependencies and the event journal (M24-04B1).

Revision ID: 0021_investigation_tasks
Revises: 0020_investigation_plans

``investigation_tasks`` is the sole authority for a task's business status,
verification status and lease (ADR-0011 D1/D3).  Its dependencies and its event
journal are append-only by ``BEFORE UPDATE OR DELETE`` and ``BEFORE TRUNCATE``
triggers.  ``event_sequence`` is written only by the application's
``append_task_event``; the schema installs the composite ``(id,
investigation_id)`` unique key that keeps dependency and event foreign keys
inside one investigation.

The revision also adds the execution-attribution columns to
``tool_invocations`` (``task_id``/``actor_kind``/``actor_id``, E4; the session
pair belongs to 0023), the deferred ``workspace_file_revisions.task_id``
foreign key (E9), and extends the ``analyst_decisions`` CHECK sets
cumulatively with the task decision surface (M11).

ADR-0007 discipline: an already-conforming shape is accepted without DDL, any
partial or different shape fails before mutation, and the downgrade refuses
while task rows, attributed invocations, attributed workspace revisions or task
decisions exist.  The preflight tolerates objects added by *later* additive
revisions on the tables it extends, so re-running the chain over a newer schema
still recognises its own shape.
"""

from __future__ import annotations

import re
from typing import Any, cast

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import Connection

revision = "0021_investigation_tasks"
down_revision = "0020_investigation_plans"
branch_labels = None
depends_on = None

_INVESTIGATIONS = "analyst_investigations"
_DECISIONS = "analyst_decisions"
_TOOL_INVOCATIONS = "tool_invocations"
_WORKSPACE_REVISIONS = "workspace_file_revisions"
_PLAN_VERSIONS = "investigation_plan_versions"
_TASKS = "investigation_tasks"
_DEPENDENCIES = "investigation_task_dependencies"
_EVENTS = "investigation_task_events"
_TABLE_ORDER = (_TASKS, _DEPENDENCIES, _EVENTS)
_PARENT_TABLES = (
    _INVESTIGATIONS,
    _DECISIONS,
    _TOOL_INVOCATIONS,
    _WORKSPACE_REVISIONS,
    _PLAN_VERSIONS,
)

_DECISION_TYPE_CHECK = "ck_analyst_decisions_type"
_DECISION_TARGET_CHECK = "ck_analyst_decisions_target"
_OLD_DECISION_VALUES = (
    "'member_validate', 'member_reject', 'feature_validate', 'feature_reject', "
    "'pivot_approve', 'pivot_reject', 'note_approve', 'note_changes_requested', "
    "'detection_approve', 'detection_reject', 'plan_accept', 'plan_reject'"
)
_NEW_DECISION_VALUES = _OLD_DECISION_VALUES + (
    ", 'task_result_accept', 'task_result_changes_requested'"
)
_OLD_TARGET_VALUES = (
    "'member', 'feature', 'tool', 'invariant', 'pivot', 'corpus', 'detection', 'note', "
    "'plan_version'"
)
_NEW_TARGET_VALUES = _OLD_TARGET_VALUES + ", 'task'"

_OLD_DECISION_SET = frozenset(re.findall(r"'([a-z_]+)'", _OLD_DECISION_VALUES))
_NEW_DECISION_SET = frozenset(re.findall(r"'([a-z_]+)'", _NEW_DECISION_VALUES))
_OLD_TARGET_SET = frozenset(re.findall(r"'([a-z_]+)'", _OLD_TARGET_VALUES))
_NEW_TARGET_SET = frozenset(re.findall(r"'([a-z_]+)'", _NEW_TARGET_VALUES))

_ATTRIBUTION_COLUMNS = ("task_id", "actor_kind", "actor_id")
_TOOL_TASK_FK = "fk_tool_invocations_task"
_TOOL_ACTOR_KIND_CHECK = "ck_tool_invocations_actor_kind"
_TOOL_ACTOR_PAIR_CHECK = "ck_tool_invocations_actor_pair"
_WORKSPACE_TASK_FK = "fk_workspace_file_revisions_task"

_EVENT_FUNCTION = "reject_investigation_task_event_mutation"
_DEPENDENCY_FUNCTION = "reject_investigation_task_dependency_mutation"
_EVENT_TRIGGER = "trg_investigation_task_events_append_only"
_EVENT_TRUNCATE_TRIGGER = "trg_investigation_task_events_no_truncate"
_DEPENDENCY_TRIGGER = "trg_investigation_task_dependencies_append_only"
_DEPENDENCY_TRUNCATE_TRIGGER = "trg_investigation_task_dependencies_no_truncate"

_EVENT_FUNCTION_BODY = (
    "BEGIN RAISE EXCEPTION 'investigation task events are append-only' "
    "USING ERRCODE = '55000'; END;"
)
_DEPENDENCY_FUNCTION_BODY = (
    "BEGIN RAISE EXCEPTION 'investigation task dependencies are append-only' "
    "USING ERRCODE = '55000'; END;"
)

_TASK_STATUS_VALUES = (
    "'proposed', 'ready', 'assigned', 'running', 'awaiting_input', "
    "'awaiting_review', 'blocked', 'completed', 'cancelled', 'failed'"
)
_TASK_PRIORITY_VALUES = "'critical', 'high', 'normal', 'low'"
_TASK_VERIFICATION_VALUES = "'unverified', 'pending_review', 'accepted', 'rejected'"
_TASK_BLOCKED_REASON_VALUES = (
    "'dependency', 'capability', 'approval', 'session_lost', 'lease_expired', "
    "'input_unavailable', 'external'"
)
_TASK_ASSIGNED_ACTOR_KIND_VALUES = "'human', 'operator'"
_TASK_ACTOR_KIND_VALUES = "'human', 'operator', 'system'"
_TASK_EVENT_TYPE_VALUES = (
    "'task_created', 'status_changed', 'assigned', 'lease_acquired', "
    "'lease_renewed', 'lease_released', 'lease_expired', 'result_submitted', "
    "'verification_recorded', 'session_started', 'session_state_changed', "
    "'handoff_created', 'contribution_imported'"
)
_TOOL_ACTOR_KIND_VALUES = "'human', 'operator'"

_LEASE_FIELDS_NULL_SQL = (
    "lease_id IS NULL AND lease_session_id IS NULL "
    "AND lease_expires_at IS NULL AND heartbeat_at IS NULL"
)
_LEASE_FIELDS_COMPLETE_SQL = (
    "lease_id IS NOT NULL AND lease_session_id IS NOT NULL "
    "AND lease_expires_at IS NOT NULL AND heartbeat_at IS NOT NULL "
    "AND lease_fence >= 1"
)

_SQL_VALUE = r"'[a-z_]+'(?:\s*::\s*(?:character varying|text))?"
_SQL_VALUE_LIST = rf"(?P<values>{_SQL_VALUE}(?:\s*,\s*{_SQL_VALUE})*)"


def _check_expression(column: str) -> re.Pattern[str]:
    return re.compile(
        rf"(?:{column}|\(\s*{column}\s*\))(?:\s*::\s*(?:character varying|text))?\s+IN\s*"
        rf"\(\s*{_SQL_VALUE_LIST}\s*\)",
        re.IGNORECASE,
    )


def _check_array_expression(column: str) -> re.Pattern[str]:
    return re.compile(
        rf"(?:{column}|\(\s*{column}\s*\))(?:\s*::\s*(?:character varying|text))?\s*"
        rf"=\s*ANY\s*\(\s*\(?\s*ARRAY\s*\[\s*{_SQL_VALUE_LIST}\s*\]\s*\)?"
        rf"\s*(?:::\s*text\s*\[\])?\s*\)",
        re.IGNORECASE,
    )


_DECISION_CHECK = _check_expression("decision_type")
_DECISION_ARRAY_CHECK = _check_array_expression("decision_type")
_TARGET_CHECK = _check_expression("target_type")
_TARGET_ARRAY_CHECK = _check_array_expression("target_type")
_ACTOR_KIND_CHECK = _check_expression("actor_kind")
_ACTOR_KIND_ARRAY_CHECK = _check_array_expression("actor_kind")


def _strip_outer_parentheses(value: str) -> str:
    normalized = value.strip()
    while normalized.startswith("(") and normalized.endswith(")"):
        depth = 0
        in_string = False
        closes_at_end = True
        index = 0
        while index < len(normalized):
            character = normalized[index]
            if character == "'":
                if in_string and index + 1 < len(normalized) and normalized[index + 1] == "'":
                    index += 2
                    continue
                in_string = not in_string
            elif not in_string and character == "(":
                depth += 1
            elif not in_string and character == ")":
                depth -= 1
                if depth < 0:
                    closes_at_end = False
                    break
                if depth == 0 and index != len(normalized) - 1:
                    closes_at_end = False
                    break
            index += 1
        if in_string or depth != 0 or not closes_at_end:
            break
        normalized = normalized[1:-1].strip()
    return normalized


def _parse_check_values(
    sqltext: str | None, *, in_check: re.Pattern[str], array_check: re.Pattern[str]
) -> list[str] | None:
    if sqltext is None:
        return None
    expression = _strip_outer_parentheses(sqltext)
    for pattern in (in_check, array_check):
        match = pattern.fullmatch(expression)
        if match is not None:
            return re.findall(r"'([a-z_]+)'", match.group("values"))
    return None


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
    # PostgreSQL rewrites ``x NOT IN ('a', 'b')`` as ``x <> ALL (ARRAY[...])``.
    normalized = re.sub(
        r"<>\s*all\s*\(\s*\(?\s*array\s*\[(.*?)\]\s*\)?\s*\)",
        r"not in (\1)",
        normalized,
    )
    return re.sub(r"[()\s]", "", normalized)


def _expected_tables() -> dict[str, sa.Table]:
    metadata = sa.MetaData()
    sa.Table(
        _INVESTIGATIONS,
        metadata,
        sa.Column("id", sa.Uuid(), primary_key=True),
    )
    sa.Table(
        _PLAN_VERSIONS,
        metadata,
        sa.Column("id", sa.Uuid(), primary_key=True),
    )
    sa.Table(
        _DECISIONS,
        metadata,
        sa.Column("id", sa.Uuid(), primary_key=True),
    )
    tasks = sa.Table(
        _TASKS,
        metadata,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("investigation_id", sa.Uuid(), nullable=False),
        sa.Column("task_key", sa.Text(), nullable=False),
        sa.Column("plan_version_id", sa.Uuid(), nullable=True),
        sa.Column("parent_task_id", sa.Uuid(), nullable=True),
        sa.Column("depth", sa.SmallInteger(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("objective", sa.Text(), nullable=False),
        sa.Column("priority", sa.String(length=16), nullable=False),
        sa.Column("claim_refs", JSONB(), nullable=False),
        sa.Column("evidence_refs", JSONB(), nullable=False),
        sa.Column("sample_refs", JSONB(), nullable=False),
        sa.Column("required_capabilities", JSONB(), nullable=False),
        sa.Column("allowed_workspace_paths", JSONB(), nullable=False),
        sa.Column("execution_policy", JSONB(), nullable=False),
        sa.Column("expected_outputs", JSONB(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("status_version", sa.BigInteger(), nullable=False),
        sa.Column(
            "result_verification_status",
            sa.String(length=32),
            nullable=False,
            server_default=sa.text("'unverified'"),
        ),
        sa.Column("blocked_reason", sa.String(length=32), nullable=True),
        sa.Column("assigned_actor_kind", sa.String(length=16), nullable=True),
        sa.Column("assigned_actor_id", sa.Text(), nullable=True),
        sa.Column("lease_id", sa.Uuid(), nullable=True),
        sa.Column("lease_session_id", sa.Uuid(), nullable=True),
        sa.Column("lease_fence", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("handoff_ref", sa.Uuid(), nullable=True),
        sa.Column("result_refs", JSONB(), nullable=False),
        sa.Column("event_sequence", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("created_by_actor_kind", sa.String(length=16), nullable=False),
        sa.Column("created_by_actor_id", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_investigation_tasks"),
        sa.UniqueConstraint(
            "investigation_id",
            "task_key",
            name="uq_investigation_tasks_investigation_task_key",
        ),
        sa.UniqueConstraint("id", "investigation_id", name="uq_investigation_tasks_identity"),
        sa.ForeignKeyConstraint(
            ["investigation_id"],
            [f"{_INVESTIGATIONS}.id"],
            name="fk_investigation_tasks_investigation",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["plan_version_id"],
            [f"{_PLAN_VERSIONS}.id"],
            name="fk_investigation_tasks_plan_version",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["parent_task_id"],
            [f"{_TASKS}.id"],
            name="fk_investigation_tasks_parent",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            f"status IN ({_TASK_STATUS_VALUES})", name="ck_investigation_tasks_status"
        ),
        sa.CheckConstraint(
            f"priority IN ({_TASK_PRIORITY_VALUES})",
            name="ck_investigation_tasks_priority",
        ),
        sa.CheckConstraint("status_version >= 1", name="ck_investigation_tasks_status_version"),
        sa.CheckConstraint(
            f"result_verification_status IN ({_TASK_VERIFICATION_VALUES})",
            name="ck_investigation_tasks_result_verification",
        ),
        sa.CheckConstraint(
            f"blocked_reason IN ({_TASK_BLOCKED_REASON_VALUES})",
            name="ck_investigation_tasks_blocked_reason",
        ),
        sa.CheckConstraint(
            "(blocked_reason IS NOT NULL) = (status = 'blocked')",
            name="ck_investigation_tasks_blocked_reason_pair",
        ),
        sa.CheckConstraint(
            "(status = 'completed') = (result_verification_status = 'accepted')",
            name="ck_investigation_tasks_completed_accepted",
        ),
        sa.CheckConstraint(
            "(status = 'awaiting_review') = (result_verification_status = 'pending_review')",
            name="ck_investigation_tasks_awaiting_review_pending",
        ),
        sa.CheckConstraint(
            "(assigned_actor_kind IS NULL) = (assigned_actor_id IS NULL)",
            name="ck_investigation_tasks_assignment_pair",
        ),
        sa.CheckConstraint(
            f"assigned_actor_kind IN ({_TASK_ASSIGNED_ACTOR_KIND_VALUES})",
            name="ck_investigation_tasks_assigned_actor_kind",
        ),
        sa.CheckConstraint(
            "assigned_actor_kind <> 'human' OR ("
            "lease_id IS NULL AND lease_session_id IS NULL "
            "AND lease_expires_at IS NULL AND heartbeat_at IS NULL)",
            name="ck_investigation_tasks_human_assignment_no_lease",
        ),
        sa.CheckConstraint(
            f"({_LEASE_FIELDS_NULL_SQL}) OR ({_LEASE_FIELDS_COMPLETE_SQL})",
            name="ck_investigation_tasks_lease_complete",
        ),
        sa.CheckConstraint(
            "status NOT IN ('completed', 'cancelled', 'failed') OR ("
            "lease_id IS NULL AND lease_session_id IS NULL "
            "AND lease_expires_at IS NULL AND heartbeat_at IS NULL)",
            name="ck_investigation_tasks_terminal_lease_null",
        ),
        sa.CheckConstraint(
            "(parent_task_id IS NULL) = (depth = 0)",
            name="ck_investigation_tasks_parent_depth",
        ),
        sa.CheckConstraint(
            f"created_by_actor_kind IN ({_TASK_ACTOR_KIND_VALUES})",
            name="ck_investigation_tasks_created_by_actor_kind",
        ),
        sa.CheckConstraint("event_sequence >= 0", name="ck_investigation_tasks_event_sequence"),
        sa.Index(
            "ix_investigation_tasks_investigation_status_priority",
            "investigation_id",
            "status",
            "priority",
            "created_at",
        ),
        sa.Index(
            "ix_investigation_tasks_status_lease_expires",
            "status",
            "lease_expires_at",
        ),
        sa.Index(
            "ix_investigation_tasks_assignment",
            "assigned_actor_kind",
            "assigned_actor_id",
            "status",
        ),
    )
    dependencies = sa.Table(
        _DEPENDENCIES,
        metadata,
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("depends_on_task_id", sa.Uuid(), nullable=False),
        sa.Column("investigation_id", sa.Uuid(), nullable=False),
        sa.PrimaryKeyConstraint(
            "task_id", "depends_on_task_id", name="pk_investigation_task_dependencies"
        ),
        sa.ForeignKeyConstraint(
            ["task_id", "investigation_id"],
            [f"{_TASKS}.id", f"{_TASKS}.investigation_id"],
            name="fk_investigation_task_dependencies_task",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["depends_on_task_id", "investigation_id"],
            [f"{_TASKS}.id", f"{_TASKS}.investigation_id"],
            name="fk_investigation_task_dependencies_depends_on",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "task_id <> depends_on_task_id",
            name="ck_investigation_task_dependencies_not_self",
        ),
    )
    events = sa.Table(
        _EVENTS,
        metadata,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("investigation_id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("sequence", sa.BigInteger(), nullable=False),
        sa.Column("event_type", sa.String(length=32), nullable=False),
        sa.Column("from_status", sa.String(length=32), nullable=True),
        sa.Column("to_status", sa.String(length=32), nullable=True),
        sa.Column("status_version", sa.BigInteger(), nullable=False),
        sa.Column("lease_fence", sa.BigInteger(), nullable=True),
        sa.Column("actor_kind", sa.String(length=16), nullable=False),
        sa.Column("actor_id", sa.Text(), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("request_sha256", sa.String(length=64), nullable=False),
        sa.Column("analyst_decision_id", sa.Uuid(), nullable=True),
        sa.Column("payload", JSONB(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_investigation_task_events"),
        sa.UniqueConstraint(
            "task_id", "sequence", name="uq_investigation_task_events_task_sequence"
        ),
        sa.UniqueConstraint(
            "task_id",
            "idempotency_key",
            name="uq_investigation_task_events_task_idempotency",
        ),
        sa.ForeignKeyConstraint(
            ["task_id", "investigation_id"],
            [f"{_TASKS}.id", f"{_TASKS}.investigation_id"],
            name="fk_investigation_task_events_task",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["analyst_decision_id"],
            [f"{_DECISIONS}.id"],
            name="fk_investigation_task_events_analyst_decision",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint("sequence >= 1", name="ck_investigation_task_events_sequence"),
        sa.CheckConstraint(
            f"event_type IN ({_TASK_EVENT_TYPE_VALUES})",
            name="ck_investigation_task_events_event_type",
        ),
        sa.CheckConstraint(
            "status_version >= 1", name="ck_investigation_task_events_status_version"
        ),
        sa.CheckConstraint(
            f"actor_kind IN ({_TASK_ACTOR_KIND_VALUES})",
            name="ck_investigation_task_events_actor_kind",
        ),
        sa.CheckConstraint(
            "char_length(request_sha256) = 64 AND request_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_investigation_task_events_request_sha256",
        ),
        sa.Index(
            "ix_investigation_task_events_investigation_timeline",
            "investigation_id",
            "occurred_at",
            "id",
        ),
    )
    return {_TASKS: tasks, _DEPENDENCIES: dependencies, _EVENTS: events}


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


def _column_mismatches(bind: Any, table: sa.Table) -> list[str]:
    """Every expected column must exist with its exact shape.

    Extra columns are tolerated: later additive revisions (0022/0023 add
    ``handoff_ref``/``lease_session_id`` foreign keys, 0023 adds
    ``agent_session_id``) extend these tables, and re-running this revision
    over such a schema must still recognise its own shape.
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
        default_arg = (
            expected.server_default.arg
            if isinstance(expected.server_default, sa.schema.DefaultClause)
            else None
        )
        expected_default = _normalize_sql(str(default_arg)) if default_arg is not None else None
        expected_signature = (
            f"type={expected.type.compile(dialect=bind.dialect).lower()}, "
            f"nullable={expected.nullable}, default={expected_default}"
        )
        actual_signature = (
            f"type={actual['type'].compile(dialect=bind.dialect).lower()}, "
            f"nullable={actual['nullable']}, default={_normalize_sql(actual.get('default'))}"
        )
        if expected_signature != actual_signature:
            mismatches.append(
                f"{table.name}: column {name}: expected {expected_signature}, "
                f"actual {actual_signature}"
            )
    return mismatches


def _schema_mismatches(bind: Any, table: sa.Table) -> list[str]:
    inspector = sa.inspect(bind)
    mismatches = _column_mismatches(bind, table)
    expected_constraints = _constraint_signature(table)
    actual_constraints = _actual_constraint_signature(inspector, table.name)
    for signature in sorted(expected_constraints - actual_constraints, key=repr):
        mismatches.append(f"{table.name}: constraint {signature[1]}: expected absent")
    expected_indexes = _index_signature(table)
    actual_indexes = _actual_index_signature(inspector, table.name)
    for signature in sorted(expected_indexes - actual_indexes, key=repr):
        mismatches.append(f"{table.name}: index {signature[0]}: expected absent")
    return mismatches


def _decision_check_values(
    inspector: sa.Inspector, constraint_name: str, *, target: bool
) -> set[str] | None:
    checks = {
        check.get("name"): check.get("sqltext")
        for check in inspector.get_check_constraints(_DECISIONS)
    }
    raw = checks.get(constraint_name)
    observed = _parse_check_values(
        cast(str | None, raw),
        in_check=_TARGET_CHECK if target else _DECISION_CHECK,
        array_check=_TARGET_ARRAY_CHECK if target else _DECISION_ARRAY_CHECK,
    )
    return set(observed) if observed is not None else None


def _decision_mismatches(
    inspector: sa.Inspector, *, expected: frozenset[str], expected_targets: frozenset[str]
) -> list[str]:
    mismatches: list[str] = []
    observed_decisions = _decision_check_values(inspector, _DECISION_TYPE_CHECK, target=False)
    observed_targets = _decision_check_values(inspector, _DECISION_TARGET_CHECK, target=True)
    if observed_decisions is None or not expected <= observed_decisions:
        mismatches.append(
            f"constraint {_DECISION_TYPE_CHECK}: expected at least {sorted(expected)}, "
            f"actual {sorted(observed_decisions) if observed_decisions else observed_decisions}"
        )
    if observed_targets is None or not expected_targets <= observed_targets:
        mismatches.append(
            f"constraint {_DECISION_TARGET_CHECK}: expected at least {sorted(expected_targets)}, "
            f"actual {sorted(observed_targets) if observed_targets else observed_targets}"
        )
    return mismatches


def _attribution_mismatches(bind: Any, inspector: sa.Inspector) -> list[str]:
    mismatches: list[str] = []
    if not inspector.has_table(_TOOL_INVOCATIONS):
        return [f"{_TOOL_INVOCATIONS}: expected present, actual absent"]
    columns = {item["name"]: item for item in inspector.get_columns(_TOOL_INVOCATIONS)}
    expected = {
        "task_id": "uuid",
        "actor_kind": "varchar(16)",
        "actor_id": "text",
    }
    for name, expected_type in expected.items():
        actual = columns.get(name)
        if actual is None:
            mismatches.append(
                f"{_TOOL_INVOCATIONS}: column {name}: expected present, actual absent"
            )
            continue
        actual_type = actual["type"].compile(dialect=bind.dialect).lower()
        if actual_type != expected_type or not actual["nullable"]:
            mismatches.append(
                f"{_TOOL_INVOCATIONS}: column {name}: expected {expected_type} nullable, "
                f"actual {actual_type} nullable={actual['nullable']}"
            )
    foreign_keys = {
        foreign_key.get("name"): foreign_key
        for foreign_key in inspector.get_foreign_keys(_TOOL_INVOCATIONS)
    }
    task_fk = foreign_keys.get(_TOOL_TASK_FK)
    if task_fk is None:
        mismatches.append(f"{_TOOL_INVOCATIONS}: foreign key {_TOOL_TASK_FK}: expected absent")
    else:
        options = task_fk.get("options", {})
        if (
            tuple(task_fk.get("constrained_columns", ())) != ("task_id",)
            or task_fk.get("referred_table") != _TASKS
            or tuple(task_fk.get("referred_columns", ())) != ("id",)
            or str(options.get("ondelete", "")).upper() != "RESTRICT"
        ):
            mismatches.append(f"{_TOOL_INVOCATIONS}: foreign key {_TOOL_TASK_FK}: shape differs")
    checks = {
        check.get("name"): check.get("sqltext")
        for check in inspector.get_check_constraints(_TOOL_INVOCATIONS)
    }
    actor_values = _parse_check_values(
        cast(str | None, checks.get(_TOOL_ACTOR_KIND_CHECK)),
        in_check=_ACTOR_KIND_CHECK,
        array_check=_ACTOR_KIND_ARRAY_CHECK,
    )
    if actor_values is None or set(actor_values) != {"human", "operator"}:
        mismatches.append(
            f"{_TOOL_INVOCATIONS}: constraint {_TOOL_ACTOR_KIND_CHECK}: shape differs"
        )
    pair_sql = _normalize_sql(cast(str | None, checks.get(_TOOL_ACTOR_PAIR_CHECK)))
    expected_pair = _normalize_sql("(actor_kind IS NULL) = (actor_id IS NULL)")
    if pair_sql != expected_pair:
        mismatches.append(
            f"{_TOOL_INVOCATIONS}: constraint {_TOOL_ACTOR_PAIR_CHECK}: expected "
            f"{expected_pair}, actual {pair_sql}"
        )
    return mismatches


def _workspace_fk_mismatches(inspector: sa.Inspector) -> list[str]:
    if not inspector.has_table(_WORKSPACE_REVISIONS):
        return [f"{_WORKSPACE_REVISIONS}: expected present, actual absent"]
    foreign_keys = {
        foreign_key.get("name"): foreign_key
        for foreign_key in inspector.get_foreign_keys(_WORKSPACE_REVISIONS)
    }
    task_fk = foreign_keys.get(_WORKSPACE_TASK_FK)
    if task_fk is None:
        return [f"{_WORKSPACE_REVISIONS}: foreign key {_WORKSPACE_TASK_FK}: expected absent"]
    options = task_fk.get("options", {})
    if (
        tuple(task_fk.get("constrained_columns", ())) != ("task_id",)
        or task_fk.get("referred_table") != _TASKS
        or tuple(task_fk.get("referred_columns", ())) != ("id",)
        or str(options.get("ondelete", "")).upper() != "RESTRICT"
    ):
        return [f"{_WORKSPACE_REVISIONS}: foreign key {_WORKSPACE_TASK_FK}: shape differs"]
    return []


def _guard_mismatches(bind: Connection) -> list[str]:
    expected_functions = {
        _EVENT_FUNCTION: _EVENT_FUNCTION_BODY,
        _DEPENDENCY_FUNCTION: _DEPENDENCY_FUNCTION_BODY,
    }
    rows = bind.execute(
        sa.text(
            "SELECT p.proname, l.lanname, p.prosrc FROM pg_proc p "
            "JOIN pg_namespace n ON n.oid = p.pronamespace "
            "JOIN pg_language l ON l.oid = p.prolang "
            "WHERE n.nspname = current_schema() AND p.proname IN "
            "(:event_function, :dependency_function)"
        ),
        {
            "event_function": _EVENT_FUNCTION,
            "dependency_function": _DEPENDENCY_FUNCTION,
        },
    ).all()
    actual_functions = {row.proname: (row.lanname, _normalize_sql(row.prosrc)) for row in rows}
    mismatches: list[str] = []
    for name, body in expected_functions.items():
        actual = actual_functions.get(name)
        if actual is None:
            mismatches.append(f"function {name}: expected present, actual absent")
        elif actual != ("plpgsql", _normalize_sql(body)):
            mismatches.append(f"function {name}: body differs")

    expected_triggers = {
        _EVENT_TRIGGER: (_EVENTS, 27, _EVENT_FUNCTION),
        _EVENT_TRUNCATE_TRIGGER: (_EVENTS, 34, _EVENT_FUNCTION),
        _DEPENDENCY_TRIGGER: (_DEPENDENCIES, 27, _DEPENDENCY_FUNCTION),
        _DEPENDENCY_TRUNCATE_TRIGGER: (_DEPENDENCIES, 34, _DEPENDENCY_FUNCTION),
    }
    trigger_rows = bind.execute(
        sa.text(
            "SELECT t.tgname, t.tgrelid::regclass::text AS table_name, t.tgtype, "
            "t.tgenabled, t.tgisinternal, p.proname AS function_name, "
            "pg_get_triggerdef(t.oid) AS definition "
            "FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "JOIN pg_proc p ON p.oid = t.tgfoid "
            "WHERE n.nspname = current_schema() AND c.relname IN "
            "(:events, :dependencies) AND NOT t.tgisinternal"
        ),
        {"events": _EVENTS, "dependencies": _DEPENDENCIES},
    ).all()
    actual_triggers = {row.tgname for row in trigger_rows}
    for name in expected_triggers:
        if name not in actual_triggers:
            mismatches.append(f"trigger {name}: expected present, actual absent")
    for row in trigger_rows:
        expected = expected_triggers.get(row.tgname)
        if expected is None:
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
    if any(inspector.has_table(name) for name in _TABLE_ORDER):
        return True
    if inspector.has_table(_TOOL_INVOCATIONS):
        columns = {item["name"] for item in inspector.get_columns(_TOOL_INVOCATIONS)}
        if set(_ATTRIBUTION_COLUMNS) & columns:
            return True
        checks = {check.get("name") for check in inspector.get_check_constraints(_TOOL_INVOCATIONS)}
        if _TOOL_ACTOR_PAIR_CHECK in checks:
            return True
    if inspector.has_table(_WORKSPACE_REVISIONS):
        foreign_keys = {
            foreign_key.get("name")
            for foreign_key in inspector.get_foreign_keys(_WORKSPACE_REVISIONS)
        }
        if _WORKSPACE_TASK_FK in foreign_keys:
            return True
    if inspector.has_table(_DECISIONS):
        observed = _decision_check_values(inspector, _DECISION_TYPE_CHECK, target=False)
        if (_NEW_DECISION_SET - _OLD_DECISION_SET) <= (observed or set()):
            return True
    guards = bind.execute(
        sa.text(
            "SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
            "WHERE n.nspname = current_schema() AND p.proname IN "
            "(:event_function, :dependency_function)"
        ),
        {
            "event_function": _EVENT_FUNCTION,
            "dependency_function": _DEPENDENCY_FUNCTION,
        },
    ).scalar_one()
    triggers = bind.execute(
        sa.text(
            "SELECT count(*) FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = current_schema() AND NOT t.tgisinternal AND t.tgname IN "
            "(:event_trigger, :event_truncate, :dependency_trigger, :dependency_truncate)"
        ),
        {
            "event_trigger": _EVENT_TRIGGER,
            "event_truncate": _EVENT_TRUNCATE_TRIGGER,
            "dependency_trigger": _DEPENDENCY_TRIGGER,
            "dependency_truncate": _DEPENDENCY_TRUNCATE_TRIGGER,
        },
    ).scalar_one()
    return bool(guards or triggers)


def _full_shape_mismatches(bind: Connection) -> list[str]:
    inspector = sa.inspect(bind)
    expected = _expected_tables()
    mismatches: list[str] = []
    for name in _TABLE_ORDER:
        if not inspector.has_table(name):
            mismatches.append(f"{name}: expected present, actual absent")
            continue
        mismatches.extend(_schema_mismatches(bind, expected[name]))
    for name in _PARENT_TABLES:
        if not inspector.has_table(name):
            mismatches.append(f"{name}: expected present, actual absent")
    if inspector.has_table(_DECISIONS):
        mismatches.extend(
            _decision_mismatches(
                inspector,
                expected=_NEW_DECISION_SET,
                expected_targets=_NEW_TARGET_SET,
            )
        )
    mismatches.extend(_attribution_mismatches(bind, inspector))
    mismatches.extend(_workspace_fk_mismatches(inspector))
    mismatches.extend(_guard_mismatches(bind))
    return mismatches


def _baseline_mismatches(bind: Connection) -> list[str]:
    """Verify the 0020 state before the first DDL on a disposable or live base."""
    inspector = sa.inspect(bind)
    mismatches: list[str] = [
        f"{name}: expected present, actual absent"
        for name in _PARENT_TABLES
        if not inspector.has_table(name)
    ]
    if inspector.has_table(_DECISIONS):
        observed_decisions = _decision_check_values(inspector, _DECISION_TYPE_CHECK, target=False)
        observed_targets = _decision_check_values(inspector, _DECISION_TARGET_CHECK, target=True)
        if observed_decisions != _OLD_DECISION_SET:
            mismatches.append(
                f"constraint {_DECISION_TYPE_CHECK}: expected exactly "
                f"{sorted(_OLD_DECISION_SET)}, actual "
                f"{sorted(observed_decisions) if observed_decisions else observed_decisions}"
            )
        if observed_targets != _OLD_TARGET_SET:
            mismatches.append(
                f"constraint {_DECISION_TARGET_CHECK}: expected exactly "
                f"{sorted(_OLD_TARGET_SET)}, actual "
                f"{sorted(observed_targets) if observed_targets else observed_targets}"
            )
    if inspector.has_table(_TOOL_INVOCATIONS):
        columns = {item["name"] for item in inspector.get_columns(_TOOL_INVOCATIONS)}
        unexpected = set(_ATTRIBUTION_COLUMNS) & columns
        if unexpected:
            mismatches.append(
                f"{_TOOL_INVOCATIONS}: unexpected attribution columns {sorted(unexpected)}"
            )
        checks = {check.get("name") for check in inspector.get_check_constraints(_TOOL_INVOCATIONS)}
        if _TOOL_ACTOR_PAIR_CHECK in checks:
            mismatches.append(
                f"{_TOOL_INVOCATIONS}: unexpected constraint {_TOOL_ACTOR_PAIR_CHECK}"
            )
    if inspector.has_table(_WORKSPACE_REVISIONS):
        foreign_keys = {
            foreign_key.get("name")
            for foreign_key in inspector.get_foreign_keys(_WORKSPACE_REVISIONS)
        }
        if _WORKSPACE_TASK_FK in foreign_keys:
            mismatches.append(
                f"{_WORKSPACE_REVISIONS}: unexpected foreign key {_WORKSPACE_TASK_FK}"
            )
        attributed_revisions = bind.execute(
            sa.text(f"SELECT count(*) FROM {_WORKSPACE_REVISIONS} WHERE task_id IS NOT NULL")
        ).scalar_one()
        if attributed_revisions:
            mismatches.append(
                f"{_WORKSPACE_REVISIONS}: {attributed_revisions} row(s) already name a task"
            )
    return mismatches


def _create_tables() -> None:
    op.create_table(
        _TASKS,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("investigation_id", sa.Uuid(), nullable=False),
        sa.Column("task_key", sa.Text(), nullable=False),
        sa.Column("plan_version_id", sa.Uuid(), nullable=True),
        sa.Column("parent_task_id", sa.Uuid(), nullable=True),
        sa.Column("depth", sa.SmallInteger(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("objective", sa.Text(), nullable=False),
        sa.Column("priority", sa.String(length=16), nullable=False),
        sa.Column("claim_refs", JSONB(), nullable=False),
        sa.Column("evidence_refs", JSONB(), nullable=False),
        sa.Column("sample_refs", JSONB(), nullable=False),
        sa.Column("required_capabilities", JSONB(), nullable=False),
        sa.Column("allowed_workspace_paths", JSONB(), nullable=False),
        sa.Column("execution_policy", JSONB(), nullable=False),
        sa.Column("expected_outputs", JSONB(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("status_version", sa.BigInteger(), nullable=False),
        sa.Column(
            "result_verification_status",
            sa.String(length=32),
            nullable=False,
            server_default=sa.text("'unverified'"),
        ),
        sa.Column("blocked_reason", sa.String(length=32), nullable=True),
        sa.Column("assigned_actor_kind", sa.String(length=16), nullable=True),
        sa.Column("assigned_actor_id", sa.Text(), nullable=True),
        sa.Column("lease_id", sa.Uuid(), nullable=True),
        sa.Column("lease_session_id", sa.Uuid(), nullable=True),
        sa.Column("lease_fence", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("handoff_ref", sa.Uuid(), nullable=True),
        sa.Column("result_refs", JSONB(), nullable=False),
        sa.Column("event_sequence", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("created_by_actor_kind", sa.String(length=16), nullable=False),
        sa.Column("created_by_actor_id", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_investigation_tasks"),
        sa.UniqueConstraint(
            "investigation_id",
            "task_key",
            name="uq_investigation_tasks_investigation_task_key",
        ),
        sa.UniqueConstraint("id", "investigation_id", name="uq_investigation_tasks_identity"),
        sa.ForeignKeyConstraint(
            ["investigation_id"],
            [f"{_INVESTIGATIONS}.id"],
            name="fk_investigation_tasks_investigation",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["plan_version_id"],
            [f"{_PLAN_VERSIONS}.id"],
            name="fk_investigation_tasks_plan_version",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["parent_task_id"],
            [f"{_TASKS}.id"],
            name="fk_investigation_tasks_parent",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            f"status IN ({_TASK_STATUS_VALUES})", name="ck_investigation_tasks_status"
        ),
        sa.CheckConstraint(
            f"priority IN ({_TASK_PRIORITY_VALUES})",
            name="ck_investigation_tasks_priority",
        ),
        sa.CheckConstraint("status_version >= 1", name="ck_investigation_tasks_status_version"),
        sa.CheckConstraint(
            f"result_verification_status IN ({_TASK_VERIFICATION_VALUES})",
            name="ck_investigation_tasks_result_verification",
        ),
        sa.CheckConstraint(
            f"blocked_reason IN ({_TASK_BLOCKED_REASON_VALUES})",
            name="ck_investigation_tasks_blocked_reason",
        ),
        sa.CheckConstraint(
            "(blocked_reason IS NOT NULL) = (status = 'blocked')",
            name="ck_investigation_tasks_blocked_reason_pair",
        ),
        sa.CheckConstraint(
            "(status = 'completed') = (result_verification_status = 'accepted')",
            name="ck_investigation_tasks_completed_accepted",
        ),
        sa.CheckConstraint(
            "(status = 'awaiting_review') = (result_verification_status = 'pending_review')",
            name="ck_investigation_tasks_awaiting_review_pending",
        ),
        sa.CheckConstraint(
            "(assigned_actor_kind IS NULL) = (assigned_actor_id IS NULL)",
            name="ck_investigation_tasks_assignment_pair",
        ),
        sa.CheckConstraint(
            f"assigned_actor_kind IN ({_TASK_ASSIGNED_ACTOR_KIND_VALUES})",
            name="ck_investigation_tasks_assigned_actor_kind",
        ),
        sa.CheckConstraint(
            "assigned_actor_kind <> 'human' OR ("
            "lease_id IS NULL AND lease_session_id IS NULL "
            "AND lease_expires_at IS NULL AND heartbeat_at IS NULL)",
            name="ck_investigation_tasks_human_assignment_no_lease",
        ),
        sa.CheckConstraint(
            f"({_LEASE_FIELDS_NULL_SQL}) OR ({_LEASE_FIELDS_COMPLETE_SQL})",
            name="ck_investigation_tasks_lease_complete",
        ),
        sa.CheckConstraint(
            "status NOT IN ('completed', 'cancelled', 'failed') OR ("
            "lease_id IS NULL AND lease_session_id IS NULL "
            "AND lease_expires_at IS NULL AND heartbeat_at IS NULL)",
            name="ck_investigation_tasks_terminal_lease_null",
        ),
        sa.CheckConstraint(
            "(parent_task_id IS NULL) = (depth = 0)",
            name="ck_investigation_tasks_parent_depth",
        ),
        sa.CheckConstraint(
            f"created_by_actor_kind IN ({_TASK_ACTOR_KIND_VALUES})",
            name="ck_investigation_tasks_created_by_actor_kind",
        ),
        sa.CheckConstraint("event_sequence >= 0", name="ck_investigation_tasks_event_sequence"),
    )
    for index in _expected_tables()[_TASKS].indexes:
        op.create_index(index.name, _TASKS, [column.name for column in index.columns])
    op.create_table(
        _DEPENDENCIES,
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("depends_on_task_id", sa.Uuid(), nullable=False),
        sa.Column("investigation_id", sa.Uuid(), nullable=False),
        sa.PrimaryKeyConstraint(
            "task_id", "depends_on_task_id", name="pk_investigation_task_dependencies"
        ),
        sa.ForeignKeyConstraint(
            ["task_id", "investigation_id"],
            [f"{_TASKS}.id", f"{_TASKS}.investigation_id"],
            name="fk_investigation_task_dependencies_task",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["depends_on_task_id", "investigation_id"],
            [f"{_TASKS}.id", f"{_TASKS}.investigation_id"],
            name="fk_investigation_task_dependencies_depends_on",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "task_id <> depends_on_task_id",
            name="ck_investigation_task_dependencies_not_self",
        ),
    )
    op.create_table(
        _EVENTS,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("investigation_id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("sequence", sa.BigInteger(), nullable=False),
        sa.Column("event_type", sa.String(length=32), nullable=False),
        sa.Column("from_status", sa.String(length=32), nullable=True),
        sa.Column("to_status", sa.String(length=32), nullable=True),
        sa.Column("status_version", sa.BigInteger(), nullable=False),
        sa.Column("lease_fence", sa.BigInteger(), nullable=True),
        sa.Column("actor_kind", sa.String(length=16), nullable=False),
        sa.Column("actor_id", sa.Text(), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("request_sha256", sa.String(length=64), nullable=False),
        sa.Column("analyst_decision_id", sa.Uuid(), nullable=True),
        sa.Column("payload", JSONB(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_investigation_task_events"),
        sa.UniqueConstraint(
            "task_id", "sequence", name="uq_investigation_task_events_task_sequence"
        ),
        sa.UniqueConstraint(
            "task_id",
            "idempotency_key",
            name="uq_investigation_task_events_task_idempotency",
        ),
        sa.ForeignKeyConstraint(
            ["task_id", "investigation_id"],
            [f"{_TASKS}.id", f"{_TASKS}.investigation_id"],
            name="fk_investigation_task_events_task",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["analyst_decision_id"],
            [f"{_DECISIONS}.id"],
            name="fk_investigation_task_events_analyst_decision",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint("sequence >= 1", name="ck_investigation_task_events_sequence"),
        sa.CheckConstraint(
            f"event_type IN ({_TASK_EVENT_TYPE_VALUES})",
            name="ck_investigation_task_events_event_type",
        ),
        sa.CheckConstraint(
            "status_version >= 1", name="ck_investigation_task_events_status_version"
        ),
        sa.CheckConstraint(
            f"actor_kind IN ({_TASK_ACTOR_KIND_VALUES})",
            name="ck_investigation_task_events_actor_kind",
        ),
        sa.CheckConstraint(
            "char_length(request_sha256) = 64 AND request_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_investigation_task_events_request_sha256",
        ),
    )
    for index in _expected_tables()[_EVENTS].indexes:
        op.create_index(index.name, _EVENTS, [column.name for column in index.columns])


def _create_guards() -> None:
    op.execute(
        f"""
        CREATE FUNCTION {_EVENT_FUNCTION}() RETURNS trigger AS $$
        {_EVENT_FUNCTION_BODY}
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        f"""
        CREATE TRIGGER {_EVENT_TRIGGER}
        BEFORE UPDATE OR DELETE ON {_EVENTS}
        FOR EACH ROW EXECUTE FUNCTION {_EVENT_FUNCTION}()
        """
    )
    op.execute(
        f"""
        CREATE TRIGGER {_EVENT_TRUNCATE_TRIGGER}
        BEFORE TRUNCATE ON {_EVENTS}
        FOR EACH STATEMENT EXECUTE FUNCTION {_EVENT_FUNCTION}()
        """
    )
    op.execute(
        f"""
        CREATE FUNCTION {_DEPENDENCY_FUNCTION}() RETURNS trigger AS $$
        {_DEPENDENCY_FUNCTION_BODY}
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        f"""
        CREATE TRIGGER {_DEPENDENCY_TRIGGER}
        BEFORE UPDATE OR DELETE ON {_DEPENDENCIES}
        FOR EACH ROW EXECUTE FUNCTION {_DEPENDENCY_FUNCTION}()
        """
    )
    op.execute(
        f"""
        CREATE TRIGGER {_DEPENDENCY_TRUNCATE_TRIGGER}
        BEFORE TRUNCATE ON {_DEPENDENCIES}
        FOR EACH STATEMENT EXECUTE FUNCTION {_DEPENDENCY_FUNCTION}()
        """
    )


def _drop_guards() -> None:
    op.execute(f"DROP TRIGGER {_EVENT_TRUNCATE_TRIGGER} ON {_EVENTS}")
    op.execute(f"DROP TRIGGER {_EVENT_TRIGGER} ON {_EVENTS}")
    op.execute(f"DROP TRIGGER {_DEPENDENCY_TRUNCATE_TRIGGER} ON {_DEPENDENCIES}")
    op.execute(f"DROP TRIGGER {_DEPENDENCY_TRIGGER} ON {_DEPENDENCIES}")
    op.execute(f"DROP FUNCTION {_EVENT_FUNCTION}()")
    op.execute(f"DROP FUNCTION {_DEPENDENCY_FUNCTION}()")


def _add_tool_invocation_attribution() -> None:
    op.add_column(_TOOL_INVOCATIONS, sa.Column("task_id", sa.Uuid(), nullable=True))
    op.add_column(_TOOL_INVOCATIONS, sa.Column("actor_kind", sa.String(length=16), nullable=True))
    op.add_column(_TOOL_INVOCATIONS, sa.Column("actor_id", sa.Text(), nullable=True))
    op.create_foreign_key(
        _TOOL_TASK_FK,
        _TOOL_INVOCATIONS,
        _TASKS,
        ["task_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        _TOOL_ACTOR_KIND_CHECK,
        _TOOL_INVOCATIONS,
        f"actor_kind IN ({_TOOL_ACTOR_KIND_VALUES})",
    )
    op.create_check_constraint(
        _TOOL_ACTOR_PAIR_CHECK,
        _TOOL_INVOCATIONS,
        "(actor_kind IS NULL) = (actor_id IS NULL)",
    )


def _drop_tool_invocation_attribution() -> None:
    op.drop_constraint(_TOOL_ACTOR_PAIR_CHECK, _TOOL_INVOCATIONS, type_="check")
    op.drop_constraint(_TOOL_ACTOR_KIND_CHECK, _TOOL_INVOCATIONS, type_="check")
    op.drop_constraint(_TOOL_TASK_FK, _TOOL_INVOCATIONS, type_="foreignkey")
    op.drop_column(_TOOL_INVOCATIONS, "actor_id")
    op.drop_column(_TOOL_INVOCATIONS, "actor_kind")
    op.drop_column(_TOOL_INVOCATIONS, "task_id")


def _add_workspace_task_fk() -> None:
    op.create_foreign_key(
        _WORKSPACE_TASK_FK,
        _WORKSPACE_REVISIONS,
        _TASKS,
        ["task_id"],
        ["id"],
        ondelete="RESTRICT",
    )


def _replace_decision_checks(decision_values: str, target_values: str) -> None:
    op.drop_constraint(_DECISION_TYPE_CHECK, _DECISIONS, type_="check")
    op.create_check_constraint(
        _DECISION_TYPE_CHECK,
        _DECISIONS,
        f"decision_type IN ({decision_values})",
    )
    op.drop_constraint(_DECISION_TARGET_CHECK, _DECISIONS, type_="check")
    op.create_check_constraint(
        _DECISION_TARGET_CHECK,
        _DECISIONS,
        f"target_type IN ({target_values})",
    )


def upgrade() -> None:
    bind = op.get_bind()
    if _present_objects(bind):
        mismatches = _full_shape_mismatches(bind)
        if mismatches:
            raise RuntimeError("investigation task preflight rejected: " + "; ".join(mismatches))
        return
    mismatches = _baseline_mismatches(bind)
    if mismatches:
        raise RuntimeError("investigation task preflight rejected: " + "; ".join(mismatches))

    bind.execute(
        sa.text(
            f"LOCK TABLE {_TOOL_INVOCATIONS}, {_WORKSPACE_REVISIONS}, {_DECISIONS} "
            "IN ACCESS EXCLUSIVE MODE"
        )
    )
    _create_tables()
    _create_guards()
    _add_tool_invocation_attribution()
    _add_workspace_task_fk()
    _replace_decision_checks(_NEW_DECISION_VALUES, _NEW_TARGET_VALUES)


def downgrade() -> None:
    bind = op.get_bind()
    if not _present_objects(bind):
        raise RuntimeError("refusing to downgrade a missing investigation task schema")
    mismatches = _full_shape_mismatches(bind)
    if mismatches:
        raise RuntimeError(
            "refusing to downgrade a partial or changed investigation task schema: "
            + "; ".join(mismatches)
        )

    bind.execute(
        sa.text(
            f"LOCK TABLE {_TASKS}, {_DEPENDENCIES}, {_EVENTS}, {_TOOL_INVOCATIONS}, "
            f"{_WORKSPACE_REVISIONS}, {_DECISIONS} IN ACCESS EXCLUSIVE MODE"
        )
    )
    for name in _TABLE_ORDER:
        row_count = bind.execute(sa.text(f"SELECT count(*) FROM {name}")).scalar_one()
        if row_count:
            raise RuntimeError(f"refusing to downgrade {name} after it has been used")
    attributed_invocations = bind.execute(
        sa.text(
            f"SELECT count(*) FROM {_TOOL_INVOCATIONS} "
            "WHERE task_id IS NOT NULL OR actor_kind IS NOT NULL OR actor_id IS NOT NULL"
        )
    ).scalar_one()
    if attributed_invocations:
        raise RuntimeError("refusing to downgrade while attributed tool invocations exist")
    attributed_revisions = bind.execute(
        sa.text(f"SELECT count(*) FROM {_WORKSPACE_REVISIONS} WHERE task_id IS NOT NULL")
    ).scalar_one()
    if attributed_revisions:
        raise RuntimeError("refusing to downgrade while workspace revisions name a task")
    task_decisions = bind.execute(
        sa.text(
            f"SELECT count(*) FROM {_DECISIONS} "
            "WHERE decision_type IN ('task_result_accept', 'task_result_changes_requested') "
            "OR target_type = 'task'"
        )
    ).scalar_one()
    if task_decisions:
        raise RuntimeError("refusing to downgrade while task decisions exist")

    _drop_guards()
    _drop_tool_invocation_attribution()
    op.drop_constraint(_WORKSPACE_TASK_FK, _WORKSPACE_REVISIONS, type_="foreignkey")
    op.drop_table(_EVENTS)
    op.drop_table(_DEPENDENCIES)
    op.drop_table(_TASKS)
    _replace_decision_checks(_OLD_DECISION_VALUES, _OLD_TARGET_VALUES)
