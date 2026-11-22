"""Create durable investigation plans and the immutable prose authority (M24-04A1).

Revision ID: 0020_investigation_plans
Revises: 0019_workspace_bindings

``investigation_plan_versions`` is the sole authority for plan content; the
``investigation_plans`` row is its compare-and-swap head.  Versions are
append-only by triggers and carry the canonical JSON hash.  The revision also
adds ``analyst_investigations.prose_authority`` (default ``legacy``, fixed at
INSERT and immutable by trigger) and extends the ``analyst_decisions`` value
sets with the plan decision surface.

ADR-0007 discipline: an already-conforming shape is accepted without DDL, any
partial or different shape fails before mutation, and the downgrade refuses
while plan rows, plan decisions or a ``bridge_only`` investigation exist.
"""

from __future__ import annotations

import re
from typing import Any, cast

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import Connection

revision = "0020_investigation_plans"
down_revision = "0019_workspace_bindings"
branch_labels = None
depends_on = None

_INVESTIGATIONS = "analyst_investigations"
_DECISIONS = "analyst_decisions"
_PLANS = "investigation_plans"
_PLAN_VERSIONS = "investigation_plan_versions"
_TABLE_ORDER = (_PLANS, _PLAN_VERSIONS)

_PROSE_AUTHORITY_COLUMN = "prose_authority"
_PROSE_AUTHORITY_CHECK = "ck_analyst_investigation_prose_authority"
_PROSE_AUTHORITY_VALUES = "'legacy', 'bridge_only'"

_DECISION_TYPE_CHECK = "ck_analyst_decisions_type"
_DECISION_TARGET_CHECK = "ck_analyst_decisions_target"
_OLD_DECISION_VALUES = (
    "'member_validate', 'member_reject', 'feature_validate', 'feature_reject', "
    "'pivot_approve', 'pivot_reject', 'note_approve', 'note_changes_requested', "
    "'detection_approve', 'detection_reject'"
)
_NEW_DECISION_VALUES = _OLD_DECISION_VALUES + ", 'plan_accept', 'plan_reject'"
_OLD_TARGET_VALUES = (
    "'member', 'feature', 'tool', 'invariant', 'pivot', 'corpus', 'detection', 'note'"
)
_NEW_TARGET_VALUES = _OLD_TARGET_VALUES + ", 'plan_version'"

_OLD_DECISION_SET = frozenset(re.findall(r"'([a-z_]+)'", _OLD_DECISION_VALUES))
_NEW_DECISION_SET = frozenset(re.findall(r"'([a-z_]+)'", _NEW_DECISION_VALUES))
_OLD_TARGET_SET = frozenset(re.findall(r"'([a-z_]+)'", _OLD_TARGET_VALUES))
_NEW_TARGET_SET = frozenset(re.findall(r"'([a-z_]+)'", _NEW_TARGET_VALUES))

_PLAN_VERSION_FUNCTION = "reject_investigation_plan_version_mutation"
_PLAN_VERSION_TRIGGER = "trg_investigation_plan_versions_append_only"
_PLAN_VERSION_TRUNCATE_TRIGGER = "trg_investigation_plan_versions_no_truncate"
_PROSE_AUTHORITY_FUNCTION = "reject_analyst_investigation_prose_authority_mutation"
_PROSE_AUTHORITY_TRIGGER = "trg_analyst_investigations_prose_authority_immutable"

_PLAN_VERSION_FUNCTION_BODY = (
    "BEGIN RAISE EXCEPTION 'investigation plan versions are append-only' "
    "USING ERRCODE = '55000'; END;"
)
_PROSE_AUTHORITY_FUNCTION_BODY = (
    "BEGIN RAISE EXCEPTION 'analyst investigation prose authority is immutable' "
    "USING ERRCODE = '55000'; END;"
)

_PLAN_SCHEMA_VERSION_CHECK_SQL = "schema_version = 'investigation-plan.v1'"
_PLAN_PARSE_STATUS_VALUES = "'parsed', 'partially_parsed'"
_CONTENT_IDENTITY_SQL = (
    "content ? 'investigation_id' AND content ? 'version' "
    "AND content ? 'schema_version' AND content ? 'leads' "
    "AND content->>'investigation_id' = investigation_id::text "
    "AND content->>'version' = version::text "
    "AND content->>'schema_version' = schema_version"
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
_PROSE_AUTHORITY_CHECK_RE = _check_expression(_PROSE_AUTHORITY_COLUMN)
_PROSE_AUTHORITY_ARRAY_CHECK_RE = _check_array_expression(_PROSE_AUTHORITY_COLUMN)


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


def _expected_tables() -> dict[str, sa.Table]:
    metadata = sa.MetaData()
    sa.Table(
        _INVESTIGATIONS,
        metadata,
        sa.Column("id", sa.Uuid(), primary_key=True),
    )
    sa.Table(
        "tool_invocations",
        metadata,
        sa.Column("id", sa.Uuid(), primary_key=True),
    )
    sa.Table(
        "evidence_observations",
        metadata,
        sa.Column("id", sa.Uuid(), primary_key=True),
    )
    plans = sa.Table(
        _PLANS,
        metadata,
        sa.Column("investigation_id", sa.Uuid(), nullable=False),
        sa.Column("current_version", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("investigation_id", name="pk_investigation_plans"),
        sa.ForeignKeyConstraint(
            ["investigation_id"],
            [f"{_INVESTIGATIONS}.id"],
            name="fk_investigation_plans_investigation",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint("current_version >= 1", name="ck_investigation_plans_current_version"),
    )
    versions = sa.Table(
        _PLAN_VERSIONS,
        metadata,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("investigation_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.BigInteger(), nullable=False),
        sa.Column("schema_version", sa.String(length=32), nullable=False),
        sa.Column("content", JSONB(), nullable=False),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column("parse_status", sa.String(length=32), nullable=False),
        sa.Column("source_invocation_id", sa.Uuid(), nullable=False),
        sa.Column("source_observation_id", sa.Uuid(), nullable=False),
        sa.Column("supersedes_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_investigation_plan_versions"),
        sa.UniqueConstraint(
            "investigation_id",
            "version",
            name="uq_investigation_plan_versions_investigation_version",
        ),
        sa.UniqueConstraint(
            "source_invocation_id",
            "source_observation_id",
            name="uq_investigation_plan_versions_source",
        ),
        sa.UniqueConstraint(
            "id", "investigation_id", name="uq_investigation_plan_versions_identity"
        ),
        sa.ForeignKeyConstraint(
            ["investigation_id"],
            [f"{_INVESTIGATIONS}.id"],
            name="fk_investigation_plan_versions_investigation",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["source_invocation_id"],
            ["tool_invocations.id"],
            name="fk_investigation_plan_versions_source_invocation",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["source_observation_id"],
            ["evidence_observations.id"],
            name="fk_investigation_plan_versions_source_observation",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["supersedes_id", "investigation_id"],
            ["investigation_plan_versions.id", "investigation_plan_versions.investigation_id"],
            name="fk_investigation_plan_versions_supersedes",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint("version >= 1", name="ck_investigation_plan_versions_version"),
        sa.CheckConstraint(
            _PLAN_SCHEMA_VERSION_CHECK_SQL,
            name="ck_investigation_plan_versions_schema_version",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(content) = 'object'",
            name="ck_investigation_plan_versions_content_object",
        ),
        sa.CheckConstraint(
            "char_length(content_sha256) = 64 AND content_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_investigation_plan_versions_content_sha256",
        ),
        sa.CheckConstraint(
            f"parse_status IN ({_PLAN_PARSE_STATUS_VALUES})",
            name="ck_investigation_plan_versions_parse_status",
        ),
        sa.CheckConstraint(
            _CONTENT_IDENTITY_SQL,
            name="ck_investigation_plan_versions_content_identity",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(content->'leads') = 'array'",
            name="ck_investigation_plan_versions_leads_array",
        ),
    )
    return {_PLANS: plans, _PLAN_VERSIONS: versions}


def _create_tables() -> None:
    op.create_table(
        _PLANS,
        sa.Column("investigation_id", sa.Uuid(), nullable=False),
        sa.Column("current_version", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("investigation_id", name="pk_investigation_plans"),
        sa.ForeignKeyConstraint(
            ["investigation_id"],
            [f"{_INVESTIGATIONS}.id"],
            name="fk_investigation_plans_investigation",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint("current_version >= 1", name="ck_investigation_plans_current_version"),
    )
    op.create_table(
        _PLAN_VERSIONS,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("investigation_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.BigInteger(), nullable=False),
        sa.Column("schema_version", sa.String(length=32), nullable=False),
        sa.Column("content", JSONB(), nullable=False),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column("parse_status", sa.String(length=32), nullable=False),
        sa.Column("source_invocation_id", sa.Uuid(), nullable=False),
        sa.Column("source_observation_id", sa.Uuid(), nullable=False),
        sa.Column("supersedes_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_investigation_plan_versions"),
        sa.UniqueConstraint(
            "investigation_id",
            "version",
            name="uq_investigation_plan_versions_investigation_version",
        ),
        sa.UniqueConstraint(
            "source_invocation_id",
            "source_observation_id",
            name="uq_investigation_plan_versions_source",
        ),
        sa.UniqueConstraint(
            "id", "investigation_id", name="uq_investigation_plan_versions_identity"
        ),
        sa.ForeignKeyConstraint(
            ["investigation_id"],
            [f"{_INVESTIGATIONS}.id"],
            name="fk_investigation_plan_versions_investigation",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["source_invocation_id"],
            ["tool_invocations.id"],
            name="fk_investigation_plan_versions_source_invocation",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["source_observation_id"],
            ["evidence_observations.id"],
            name="fk_investigation_plan_versions_source_observation",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["supersedes_id", "investigation_id"],
            ["investigation_plan_versions.id", "investigation_plan_versions.investigation_id"],
            name="fk_investigation_plan_versions_supersedes",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint("version >= 1", name="ck_investigation_plan_versions_version"),
        sa.CheckConstraint(
            _PLAN_SCHEMA_VERSION_CHECK_SQL,
            name="ck_investigation_plan_versions_schema_version",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(content) = 'object'",
            name="ck_investigation_plan_versions_content_object",
        ),
        sa.CheckConstraint(
            "char_length(content_sha256) = 64 AND content_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_investigation_plan_versions_content_sha256",
        ),
        sa.CheckConstraint(
            f"parse_status IN ({_PLAN_PARSE_STATUS_VALUES})",
            name="ck_investigation_plan_versions_parse_status",
        ),
        sa.CheckConstraint(
            _CONTENT_IDENTITY_SQL,
            name="ck_investigation_plan_versions_content_identity",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(content->'leads') = 'array'",
            name="ck_investigation_plan_versions_leads_array",
        ),
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


def _schema_mismatches(bind: Any, table: sa.Table) -> list[str]:
    inspector = sa.inspect(bind)
    mismatches: list[str] = []
    actual_columns = {item["name"]: item for item in inspector.get_columns(table.name)}
    for name in sorted(set(table.columns.keys()) | set(actual_columns)):
        expected = table.columns.get(name)
        actual = actual_columns.get(name)
        if expected is None:
            expected_signature = "absent"
        else:
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
            if actual is not None
            else "absent"
        )
        if expected_signature != actual_signature:
            mismatches.append(
                f"column {name}: expected {expected_signature}, actual {actual_signature}"
            )

    expected_constraints = _constraint_signature(table)
    actual_constraints = _actual_constraint_signature(inspector, table.name)
    for signature in sorted(expected_constraints - actual_constraints, key=repr):
        mismatches.append(f"constraint {signature[1]}: expected {signature}, actual absent")
    for signature in sorted(actual_constraints - expected_constraints, key=repr):
        mismatches.append(f"constraint {signature[1]}: expected absent, actual {signature}")
    return mismatches


def _prose_authority_mismatches(inspector: sa.Inspector) -> list[str]:
    mismatches: list[str] = []
    columns = {item["name"]: item for item in inspector.get_columns(_INVESTIGATIONS)}
    actual = columns.get(_PROSE_AUTHORITY_COLUMN)
    if actual is None:
        return ["column prose_authority: expected present, actual absent"]
    actual_type = str(actual["type"]).lower()
    if actual_type != "varchar(16)" or actual["nullable"]:
        mismatches.append(
            f"column prose_authority: expected varchar(16) not null, actual "
            f"{actual_type} nullable={actual['nullable']}"
        )
    default = _normalize_sql(actual.get("default"))
    if default != "'legacy'":
        mismatches.append(f"column prose_authority: expected default 'legacy', actual {default}")
    checks = {
        check.get("name"): check.get("sqltext")
        for check in inspector.get_check_constraints(_INVESTIGATIONS)
    }
    values = _parse_check_values(
        cast(str | None, checks.get(_PROSE_AUTHORITY_CHECK)),
        in_check=_PROSE_AUTHORITY_CHECK_RE,
        array_check=_PROSE_AUTHORITY_ARRAY_CHECK_RE,
    )
    if values is None or set(values) != {"legacy", "bridge_only"}:
        mismatches.append(
            f"constraint {_PROSE_AUTHORITY_CHECK}: expected legacy/bridge_only check, "
            f"actual {values}"
        )
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


def _decision_mismatches(inspector: sa.Inspector, *, expected: str) -> list[str]:
    mismatches: list[str] = []
    expected_decisions = _NEW_DECISION_SET if expected == "new" else _OLD_DECISION_SET
    expected_targets = _NEW_TARGET_SET if expected == "new" else _OLD_TARGET_SET
    observed_decisions = _decision_check_values(inspector, _DECISION_TYPE_CHECK, target=False)
    observed_targets = _decision_check_values(inspector, _DECISION_TARGET_CHECK, target=True)
    if observed_decisions != expected_decisions:
        mismatches.append(
            f"constraint {_DECISION_TYPE_CHECK}: expected {sorted(expected_decisions)}, "
            f"actual {sorted(observed_decisions) if observed_decisions else observed_decisions}"
        )
    if observed_targets != expected_targets:
        mismatches.append(
            f"constraint {_DECISION_TARGET_CHECK}: expected {sorted(expected_targets)}, "
            f"actual {sorted(observed_targets) if observed_targets else observed_targets}"
        )
    return mismatches


def _guard_objects_mismatches(bind: Connection) -> list[str]:
    expected_functions = {
        _PLAN_VERSION_FUNCTION: _PLAN_VERSION_FUNCTION_BODY,
        _PROSE_AUTHORITY_FUNCTION: _PROSE_AUTHORITY_FUNCTION_BODY,
    }
    rows = bind.execute(
        sa.text(
            "SELECT p.proname, l.lanname, p.prosrc FROM pg_proc p "
            "JOIN pg_namespace n ON n.oid = p.pronamespace "
            "JOIN pg_language l ON l.oid = p.prolang "
            "WHERE n.nspname = current_schema() AND p.proname IN "
            "(:plan_function, :authority_function)"
        ),
        {
            "plan_function": _PLAN_VERSION_FUNCTION,
            "authority_function": _PROSE_AUTHORITY_FUNCTION,
        },
    ).all()
    actual_functions = {row.proname: (row.lanname, _normalize_sql(row.prosrc)) for row in rows}
    mismatches: list[str] = []
    if set(actual_functions) != set(expected_functions):
        mismatches.append(
            f"functions: expected {sorted(expected_functions)}, actual {sorted(actual_functions)}"
        )
    for name, body in expected_functions.items():
        actual = actual_functions.get(name)
        if actual is not None and actual != ("plpgsql", _normalize_sql(body)):
            mismatches.append(f"function {name}: body differs")

    expected_triggers = {
        _PLAN_VERSION_TRIGGER: (_PLAN_VERSIONS, 27, _PLAN_VERSION_FUNCTION),
        _PLAN_VERSION_TRUNCATE_TRIGGER: (_PLAN_VERSIONS, 34, _PLAN_VERSION_FUNCTION),
        _PROSE_AUTHORITY_TRIGGER: (_INVESTIGATIONS, 19, _PROSE_AUTHORITY_FUNCTION),
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
            "(:plans, :versions, :investigations) AND NOT t.tgisinternal"
        ),
        {
            "plans": _PLANS,
            "versions": _PLAN_VERSIONS,
            "investigations": _INVESTIGATIONS,
        },
    ).all()
    actual_triggers = {row.tgname for row in trigger_rows}
    if actual_triggers != set(expected_triggers):
        mismatches.append(
            f"triggers: expected {sorted(expected_triggers)}, actual {sorted(actual_triggers)}"
        )
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
        if row.tgname == _PROSE_AUTHORITY_TRIGGER:
            if f"before update of {_PROSE_AUTHORITY_COLUMN} on {_INVESTIGATIONS}" not in definition:
                mismatches.append(f"trigger {row.tgname}: definition differs")
        elif row.tgname == _PLAN_VERSION_TRIGGER:
            if f"on {_PLAN_VERSIONS}" not in definition or not re.search(
                r"before (?:update or delete|delete or update)", definition
            ):
                mismatches.append(f"trigger {row.tgname}: definition differs")
        elif row.tgname == _PLAN_VERSION_TRUNCATE_TRIGGER:
            if f"before truncate on {_PLAN_VERSIONS}" not in definition:
                mismatches.append(f"trigger {row.tgname}: definition differs")
    return mismatches


def _present_objects(bind: Connection) -> bool:
    inspector = sa.inspect(bind)
    if inspector.has_table(_PLANS) or inspector.has_table(_PLAN_VERSIONS):
        return True
    if inspector.has_table(_INVESTIGATIONS):
        columns = {item["name"] for item in inspector.get_columns(_INVESTIGATIONS)}
        if _PROSE_AUTHORITY_COLUMN in columns:
            return True
        checks = {check.get("name") for check in inspector.get_check_constraints(_INVESTIGATIONS)}
        if _PROSE_AUTHORITY_CHECK in checks:
            return True
    if inspector.has_table(_DECISIONS):
        observed = _decision_check_values(inspector, _DECISION_TYPE_CHECK, target=False)
        if _NEW_DECISION_SET <= (observed or set()):
            return True
    guards = bind.execute(
        sa.text(
            "SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
            "WHERE n.nspname = current_schema() AND p.proname IN "
            "(:plan_function, :authority_function)"
        ),
        {
            "plan_function": _PLAN_VERSION_FUNCTION,
            "authority_function": _PROSE_AUTHORITY_FUNCTION,
        },
    ).scalar_one()
    triggers = bind.execute(
        sa.text(
            "SELECT count(*) FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = current_schema() AND NOT t.tgisinternal AND t.tgname IN "
            "(:plan_trigger, :truncate_trigger, :authority_trigger)"
        ),
        {
            "plan_trigger": _PLAN_VERSION_TRIGGER,
            "truncate_trigger": _PLAN_VERSION_TRUNCATE_TRIGGER,
            "authority_trigger": _PROSE_AUTHORITY_TRIGGER,
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
        mismatches.extend(
            f"{name}: {mismatch}" for mismatch in _schema_mismatches(bind, expected[name])
        )
    if not inspector.has_table(_INVESTIGATIONS):
        mismatches.append(f"{_INVESTIGATIONS}: expected present, actual absent")
    else:
        mismatches.extend(_prose_authority_mismatches(inspector))
    if not inspector.has_table(_DECISIONS):
        mismatches.append(f"{_DECISIONS}: expected present, actual absent")
    else:
        mismatches.extend(_decision_mismatches(inspector, expected="new"))
    mismatches.extend(_guard_objects_mismatches(bind))
    return mismatches


def _create_guards() -> None:
    op.execute(
        f"""
        CREATE FUNCTION {_PLAN_VERSION_FUNCTION}() RETURNS trigger AS $$
        {_PLAN_VERSION_FUNCTION_BODY}
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        f"""
        CREATE TRIGGER {_PLAN_VERSION_TRIGGER}
        BEFORE UPDATE OR DELETE ON {_PLAN_VERSIONS}
        FOR EACH ROW EXECUTE FUNCTION {_PLAN_VERSION_FUNCTION}()
        """
    )
    op.execute(
        f"""
        CREATE TRIGGER {_PLAN_VERSION_TRUNCATE_TRIGGER}
        BEFORE TRUNCATE ON {_PLAN_VERSIONS}
        FOR EACH STATEMENT EXECUTE FUNCTION {_PLAN_VERSION_FUNCTION}()
        """
    )
    op.execute(
        f"""
        CREATE FUNCTION {_PROSE_AUTHORITY_FUNCTION}() RETURNS trigger AS $$
        {_PROSE_AUTHORITY_FUNCTION_BODY}
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        f"""
        CREATE TRIGGER {_PROSE_AUTHORITY_TRIGGER}
        BEFORE UPDATE OF {_PROSE_AUTHORITY_COLUMN} ON {_INVESTIGATIONS}
        FOR EACH ROW EXECUTE FUNCTION {_PROSE_AUTHORITY_FUNCTION}()
        """
    )


def _drop_guards() -> None:
    op.execute(f"DROP TRIGGER {_PROSE_AUTHORITY_TRIGGER} ON {_INVESTIGATIONS}")
    op.execute(f"DROP TRIGGER {_PLAN_VERSION_TRUNCATE_TRIGGER} ON {_PLAN_VERSIONS}")
    op.execute(f"DROP TRIGGER {_PLAN_VERSION_TRIGGER} ON {_PLAN_VERSIONS}")
    op.execute(f"DROP FUNCTION {_PROSE_AUTHORITY_FUNCTION}()")
    op.execute(f"DROP FUNCTION {_PLAN_VERSION_FUNCTION}()")


def upgrade() -> None:
    bind = op.get_bind()
    if _present_objects(bind):
        mismatches = _full_shape_mismatches(bind)
        if mismatches:
            raise RuntimeError("investigation plan preflight rejected: " + "; ".join(mismatches))
        return

    bind.execute(sa.text(f"LOCK TABLE {_INVESTIGATIONS}, {_DECISIONS} IN ACCESS EXCLUSIVE MODE"))
    _create_tables()
    op.add_column(
        _INVESTIGATIONS,
        sa.Column(
            _PROSE_AUTHORITY_COLUMN,
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'legacy'"),
        ),
    )
    op.create_check_constraint(
        _PROSE_AUTHORITY_CHECK,
        _INVESTIGATIONS,
        f"{_PROSE_AUTHORITY_COLUMN} IN ({_PROSE_AUTHORITY_VALUES})",
    )
    _create_guards()

    op.drop_constraint(_DECISION_TYPE_CHECK, _DECISIONS, type_="check")
    op.create_check_constraint(
        _DECISION_TYPE_CHECK,
        _DECISIONS,
        f"decision_type IN ({_NEW_DECISION_VALUES})",
    )
    op.drop_constraint(_DECISION_TARGET_CHECK, _DECISIONS, type_="check")
    op.create_check_constraint(
        _DECISION_TARGET_CHECK,
        _DECISIONS,
        f"target_type IN ({_NEW_TARGET_VALUES})",
    )


def downgrade() -> None:
    bind = op.get_bind()
    if _present_objects(bind):
        mismatches = _full_shape_mismatches(bind)
        if mismatches:
            raise RuntimeError(
                "refusing to downgrade a partial or changed investigation plan schema: "
                + "; ".join(mismatches)
            )
    else:
        raise RuntimeError("refusing to downgrade a missing investigation plan schema")

    bind.execute(
        sa.text(
            f"LOCK TABLE {_PLANS}, {_PLAN_VERSIONS}, {_INVESTIGATIONS}, {_DECISIONS} "
            "IN ACCESS EXCLUSIVE MODE"
        )
    )
    for name in _TABLE_ORDER:
        row_count = bind.execute(sa.text(f"SELECT count(*) FROM {name}")).scalar_one()
        if row_count:
            raise RuntimeError(f"refusing to downgrade {name} after it has been used")
    bridge_only = bind.execute(
        sa.text(
            f"SELECT count(*) FROM {_INVESTIGATIONS} "
            f"WHERE {_PROSE_AUTHORITY_COLUMN} = 'bridge_only'"
        )
    ).scalar_one()
    if bridge_only:
        raise RuntimeError("refusing to downgrade while bridge_only investigations exist")
    plan_decisions = bind.execute(
        sa.text(
            f"SELECT count(*) FROM {_DECISIONS} "
            "WHERE decision_type IN ('plan_accept', 'plan_reject') "
            "OR target_type = 'plan_version'"
        )
    ).scalar_one()
    if plan_decisions:
        raise RuntimeError("refusing to downgrade while plan decisions exist")

    _drop_guards()
    op.drop_table(_PLAN_VERSIONS)
    op.drop_table(_PLANS)
    op.drop_constraint(_PROSE_AUTHORITY_CHECK, _INVESTIGATIONS, type_="check")
    op.drop_column(_INVESTIGATIONS, _PROSE_AUTHORITY_COLUMN)

    op.drop_constraint(_DECISION_TYPE_CHECK, _DECISIONS, type_="check")
    op.create_check_constraint(
        _DECISION_TYPE_CHECK,
        _DECISIONS,
        f"decision_type IN ({_OLD_DECISION_VALUES})",
    )
    op.drop_constraint(_DECISION_TARGET_CHECK, _DECISIONS, type_="check")
    op.create_check_constraint(
        _DECISION_TARGET_CHECK,
        _DECISIONS,
        f"target_type IN ({_OLD_TARGET_VALUES})",
    )
