"""Create non-canonical Bridge note proposals (M24-08B, 0025).

``investigation_note_proposals`` stores a targeted Bridge drafting answer that
awaits explicit human acceptance.  The text and provenance columns are absent
together until a persisted ``advisor.consult`` answer is ingested; acceptance
appends a canonical opinion version and records the human as accepter.  A
rejected proposal is terminal and auditable: a retry is a new row with a new
idempotency key, never an update.  The same revision extends the cumulative
``analyst_decisions`` CHECK sets with ``workspace_conflict_resolve`` /
``workspace_conflict`` for the later workspace conflict-resolution lot.

ADR-0007 discipline: an already-conforming shape is accepted without DDL, any
partial or different shape fails before mutation, and the downgrade refuses
while any proposal row exists or any decision uses the new values.
"""

from __future__ import annotations

import re
from typing import Any, cast

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import Connection

revision = "0025_opinion_note_proposals"
down_revision = "0024_work_contributions"
branch_labels = None
depends_on = None

_TABLE = "investigation_note_proposals"
_INVESTIGATIONS = "analyst_investigations"
_TASKS = "investigation_tasks"
_OBSERVATIONS = "evidence_observations"
_TOOL_INVOCATIONS = "tool_invocations"
_DECISIONS = "analyst_decisions"

_DECISION_TYPE_CHECK = "ck_analyst_decisions_type"
_DECISION_TARGET_CHECK = "ck_analyst_decisions_target"
_OLD_DECISION_VALUES = (
    "'member_validate', 'member_reject', 'feature_validate', 'feature_reject', "
    "'pivot_approve', 'pivot_reject', 'note_approve', 'note_changes_requested', "
    "'detection_approve', 'detection_reject', 'plan_accept', 'plan_reject', "
    "'task_result_accept', 'task_result_changes_requested'"
)
_NEW_DECISION_VALUES = _OLD_DECISION_VALUES + ", 'workspace_conflict_resolve'"
_OLD_TARGET_VALUES = (
    "'member', 'feature', 'tool', 'invariant', 'pivot', 'corpus', 'detection', 'note', "
    "'plan_version', 'task'"
)
_NEW_TARGET_VALUES = _OLD_TARGET_VALUES + ", 'workspace_conflict'"

_OLD_DECISION_SET = frozenset(re.findall(r"'([a-z_]+)'", _OLD_DECISION_VALUES))
_NEW_DECISION_SET = frozenset(re.findall(r"'([a-z_]+)'", _NEW_DECISION_VALUES))
_OLD_TARGET_SET = frozenset(re.findall(r"'([a-z_]+)'", _OLD_TARGET_VALUES))
_NEW_TARGET_SET = frozenset(re.findall(r"'([a-z_]+)'", _NEW_TARGET_VALUES))

_PARENT_TABLES = (
    _INVESTIGATIONS,
    _TASKS,
    _OBSERVATIONS,
    _TOOL_INVOCATIONS,
    _DECISIONS,
)

_STATUS_VALUES = "'proposed', 'accepted', 'rejected', 'stale', 'cancelled'"
_TARGET_BLOCK_VALUES = "'lead', 'section'"
_BRIDGE_BLOCK_VALUES = (
    "'key_findings', 'additional_samples', 'infection_chain_and_capabilities', "
    "'network_and_infrastructure', 'claim_verification', 'proposed_detections', "
    "'uncertainties_and_next_leads'"
)
_CONSULTATION_VALUES = "'draft_text', 'review_text'"
_NOTE_STATE_VALUES = "'observed', 'inferred', 'unresolved'"
_NOTE_RELATION_VALUES = "'confirms', 'refutes', 'qualifies', 'unresolved'"
_LIST_INDEX = "ix_investigation_note_proposals_investigation_created"
_STATUS_INDEX = "ix_investigation_note_proposals_investigation_status"

_SQL_VALUE = r"'[a-z_]+'(?:\s*::\s*(?:character varying|text))?"
_SQL_VALUE_LIST = rf"(?P<values>{_SQL_VALUE}(?:\s*,\s*{_SQL_VALUE})*)"


def _sha256_sql(column: str) -> str:
    return f"char_length({column}) = 64 AND {column} ~ '^[0-9a-f]{{64}}$'"


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


def _parse_check_values(
    raw: str | None,
    *,
    in_check: re.Pattern[str],
    array_check: re.Pattern[str],
) -> tuple[str, ...] | None:
    if not isinstance(raw, str):
        return None
    match = in_check.search(raw) or array_check.search(raw)
    if match is None:
        return None
    return tuple(re.findall(r"'([a-z_]+)'", match.group("values")))


def _expected_table() -> sa.Table:
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
    sa.Table(_OBSERVATIONS, metadata, sa.Column("id", sa.Uuid(), primary_key=True))
    sa.Table(_TOOL_INVOCATIONS, metadata, sa.Column("id", sa.Uuid(), primary_key=True))
    sa.Table(_DECISIONS, metadata, sa.Column("id", sa.Uuid(), primary_key=True))
    return sa.Table(
        _TABLE,
        metadata,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("investigation_id", sa.Uuid(), nullable=False),
        sa.Column("opinion_id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=True),
        sa.Column("anchor", sa.Text(), nullable=False),
        sa.Column("target_block_type", sa.String(length=16), nullable=False),
        sa.Column("bridge_block_type", sa.String(length=32), nullable=False),
        sa.Column("consultation_type", sa.String(length=16), nullable=False),
        sa.Column("base_note_id", sa.Uuid(), nullable=True),
        sa.Column("note_state", sa.String(length=16), nullable=False),
        sa.Column("note_relation", sa.String(length=16), nullable=False),
        sa.Column("cited_finding_refs", JSONB(), nullable=False),
        sa.Column("cited_observation_refs", JSONB(), nullable=False),
        sa.Column("proposed_text", sa.Text(), nullable=True),
        sa.Column("proposed_text_sha256", sa.String(length=64), nullable=True),
        sa.Column("bridge_invocation_id", sa.Text(), nullable=True),
        sa.Column("bridge_output_sha256", sa.String(length=64), nullable=True),
        sa.Column("bridge_response_observation_id", sa.Uuid(), nullable=True),
        sa.Column("tool_invocation_id", sa.Uuid(), nullable=True),
        sa.Column("base_opinion_version", sa.BigInteger(), nullable=True),
        sa.Column("base_opinion_sha256", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("status_version", sa.BigInteger(), nullable=False),
        sa.Column("accepted_opinion_version", sa.BigInteger(), nullable=True),
        sa.Column("analyst_decision_id", sa.Uuid(), nullable=True),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_investigation_note_proposals"),
        sa.UniqueConstraint(
            "investigation_id",
            "idempotency_key",
            name="uq_investigation_note_proposals_investigation_idempotency",
        ),
        sa.ForeignKeyConstraint(
            ["investigation_id"],
            [f"{_INVESTIGATIONS}.id"],
            name="fk_investigation_note_proposals_investigation",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["task_id", "investigation_id"],
            [f"{_TASKS}.id", f"{_TASKS}.investigation_id"],
            name="fk_investigation_note_proposals_task",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["bridge_response_observation_id"],
            [f"{_OBSERVATIONS}.id"],
            name="fk_investigation_note_proposals_response_observation",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tool_invocation_id"],
            [f"{_TOOL_INVOCATIONS}.id"],
            name="fk_investigation_note_proposals_tool_invocation",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["analyst_decision_id"],
            [f"{_DECISIONS}.id"],
            name="fk_investigation_note_proposals_analyst_decision",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            f"status IN ({_STATUS_VALUES})",
            name="ck_investigation_note_proposals_status",
        ),
        sa.CheckConstraint(
            f"target_block_type IN ({_TARGET_BLOCK_VALUES})",
            name="ck_investigation_note_proposals_target_block",
        ),
        sa.CheckConstraint(
            f"bridge_block_type IN ({_BRIDGE_BLOCK_VALUES})",
            name="ck_investigation_note_proposals_bridge_block",
        ),
        sa.CheckConstraint(
            f"consultation_type IN ({_CONSULTATION_VALUES})",
            name="ck_investigation_note_proposals_consultation",
        ),
        sa.CheckConstraint(
            f"note_state IN ({_NOTE_STATE_VALUES})",
            name="ck_investigation_note_proposals_note_state",
        ),
        sa.CheckConstraint(
            f"note_relation IN ({_NOTE_RELATION_VALUES})",
            name="ck_investigation_note_proposals_note_relation",
        ),
        sa.CheckConstraint(
            "(target_block_type = 'lead' AND anchor ~ '^lead:[0-9]{4}$') OR "
            "(target_block_type = 'section' AND "
            "anchor ~ '^section:[0-9]+:paragraph:[0-9]{4}$')",
            name="ck_investigation_note_proposals_anchor",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(cited_finding_refs) = 'array' AND "
            "jsonb_typeof(cited_observation_refs) = 'array'",
            name="ck_investigation_note_proposals_citations_shape",
        ),
        sa.CheckConstraint(
            "note_state = 'unresolved' OR jsonb_array_length(cited_finding_refs) + "
            "jsonb_array_length(cited_observation_refs) > 0",
            name="ck_investigation_note_proposals_sourced_note",
        ),
        sa.CheckConstraint(
            "(consultation_type = 'review_text') = (base_note_id IS NOT NULL)",
            name="ck_investigation_note_proposals_base_note",
        ),
        sa.CheckConstraint(
            "(proposed_text IS NULL AND proposed_text_sha256 IS NULL AND "
            "bridge_invocation_id IS NULL AND bridge_output_sha256 IS NULL AND "
            "bridge_response_observation_id IS NULL) OR "
            "(proposed_text IS NOT NULL AND proposed_text_sha256 IS NOT NULL AND "
            "bridge_invocation_id IS NOT NULL AND bridge_output_sha256 IS NOT NULL AND "
            "bridge_response_observation_id IS NOT NULL)",
            name="ck_investigation_note_proposals_provenance_complete",
        ),
        sa.CheckConstraint(
            "proposed_text IS NULL OR btrim(proposed_text) <> ''",
            name="ck_investigation_note_proposals_text_non_empty",
        ),
        sa.CheckConstraint(
            f"proposed_text_sha256 IS NULL OR ({_sha256_sql('proposed_text_sha256')})",
            name="ck_investigation_note_proposals_text_sha256",
        ),
        sa.CheckConstraint(
            f"bridge_output_sha256 IS NULL OR ({_sha256_sql('bridge_output_sha256')})",
            name="ck_investigation_note_proposals_output_sha256",
        ),
        sa.CheckConstraint(
            "bridge_invocation_id IS NULL OR btrim(bridge_invocation_id) <> ''",
            name="ck_investigation_note_proposals_bridge_invocation",
        ),
        sa.CheckConstraint(
            "(base_opinion_version IS NULL) = (base_opinion_sha256 IS NULL)",
            name="ck_investigation_note_proposals_base_pair",
        ),
        sa.CheckConstraint(
            "base_opinion_version IS NULL OR base_opinion_version >= 1",
            name="ck_investigation_note_proposals_base_version",
        ),
        sa.CheckConstraint(
            f"base_opinion_sha256 IS NULL OR ({_sha256_sql('base_opinion_sha256')})",
            name="ck_investigation_note_proposals_base_sha256",
        ),
        sa.CheckConstraint(
            "status_version >= 1",
            name="ck_investigation_note_proposals_status_version",
        ),
        sa.CheckConstraint(
            "(status = 'accepted') = "
            "(accepted_opinion_version IS NOT NULL AND analyst_decision_id IS NOT NULL)",
            name="ck_investigation_note_proposals_accepted",
        ),
        sa.CheckConstraint(
            "accepted_opinion_version IS NULL OR accepted_opinion_version >= 1",
            name="ck_investigation_note_proposals_accepted_version",
        ),
        sa.CheckConstraint(
            "status <> 'accepted' OR bridge_response_observation_id IS NOT NULL",
            name="ck_investigation_note_proposals_accepted_provenance",
        ),
        sa.Index(_LIST_INDEX, "investigation_id", "created_at", "id"),
        sa.Index(_STATUS_INDEX, "investigation_id", "status"),
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


def _decision_check_values(
    inspector: sa.Inspector, constraint_name: str, *, target: bool
) -> set[str] | None:
    checks = {
        check.get("name"): check.get("sqltext")
        for check in inspector.get_check_constraints(_DECISIONS)
    }
    raw = checks.get(constraint_name)
    observed = _parse_check_values(
        raw if isinstance(raw, str) else None,
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


def _present_objects(bind: Connection) -> bool:
    inspector = sa.inspect(bind)
    if inspector.has_table(_TABLE):
        return True
    if inspector.has_table(_DECISIONS):
        observed = _decision_check_values(inspector, _DECISION_TYPE_CHECK, target=False)
        if (_NEW_DECISION_SET - _OLD_DECISION_SET) <= (observed or set()):
            return True
    return False


def _full_shape_mismatches(bind: Connection) -> list[str]:
    inspector = sa.inspect(bind)
    mismatches: list[str] = []
    for name in _PARENT_TABLES:
        if not inspector.has_table(name):
            mismatches.append(f"{name}: expected present, actual absent")
    mismatches.extend(_schema_mismatches(bind, _expected_table()))
    if inspector.has_table(_DECISIONS):
        mismatches.extend(
            _decision_mismatches(
                inspector,
                expected=_NEW_DECISION_SET,
                expected_targets=_NEW_TARGET_SET,
            )
        )
    return mismatches


def _baseline_mismatches(bind: Connection) -> list[str]:
    inspector = sa.inspect(bind)
    mismatches: list[str] = [
        f"{name}: expected present, actual absent"
        for name in _PARENT_TABLES
        if not inspector.has_table(name)
    ]
    if inspector.has_table(_TABLE):
        mismatches.append(f"{_TABLE}: unexpected table before 0025")
    if inspector.has_table(_DECISIONS):
        observed_decisions = _decision_check_values(inspector, _DECISION_TYPE_CHECK, target=False)
        observed_targets = _decision_check_values(inspector, _DECISION_TARGET_CHECK, target=True)
        if observed_decisions != set(_OLD_DECISION_SET):
            mismatches.append(
                f"constraint {_DECISION_TYPE_CHECK}: expected exactly "
                f"{sorted(_OLD_DECISION_SET)}, actual "
                f"{sorted(observed_decisions) if observed_decisions else observed_decisions}"
            )
        if observed_targets != set(_OLD_TARGET_SET):
            mismatches.append(
                f"constraint {_DECISION_TARGET_CHECK}: expected exactly "
                f"{sorted(_OLD_TARGET_SET)}, actual "
                f"{sorted(observed_targets) if observed_targets else observed_targets}"
            )
        used = bind.execute(
            sa.text(
                f"SELECT count(*) FROM {_DECISIONS} "
                "WHERE decision_type = 'workspace_conflict_resolve' "
                "OR target_type = 'workspace_conflict'"
            )
        ).scalar_one()
        if used:
            mismatches.append(f"{_DECISIONS}: {used} row(s) already use the new values")
    return mismatches


def _create_table() -> None:
    _expected_table().create(bind=op.get_bind())


def _replace_decision_checks(*, new: bool) -> None:
    decision_values = _NEW_DECISION_VALUES if new else _OLD_DECISION_VALUES
    target_values = _NEW_TARGET_VALUES if new else _OLD_TARGET_VALUES
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
            raise RuntimeError("note proposal preflight rejected: " + "; ".join(mismatches))
        return
    mismatches = _baseline_mismatches(bind)
    if mismatches:
        raise RuntimeError("note proposal preflight rejected: " + "; ".join(mismatches))
    bind.execute(sa.text(f"LOCK TABLE {', '.join(_PARENT_TABLES)} IN ACCESS EXCLUSIVE MODE"))
    _create_table()
    _replace_decision_checks(new=True)


def downgrade() -> None:
    bind = op.get_bind()
    if not _present_objects(bind):
        raise RuntimeError("refusing to downgrade a missing note proposal schema")
    mismatches = _full_shape_mismatches(bind)
    if mismatches:
        raise RuntimeError(
            "refusing to downgrade a partial or changed note proposal schema: "
            + "; ".join(mismatches)
        )
    bind.execute(sa.text(f"LOCK TABLE {_TABLE}, {_DECISIONS} IN ACCESS EXCLUSIVE MODE"))
    if bind.execute(sa.text(f"SELECT count(*) FROM {_TABLE}")).scalar_one():
        raise RuntimeError(f"refusing to downgrade {_TABLE} after it has been used")
    used = bind.execute(
        sa.text(
            f"SELECT count(*) FROM {_DECISIONS} "
            "WHERE decision_type = 'workspace_conflict_resolve' "
            "OR target_type = 'workspace_conflict'"
        )
    ).scalar_one()
    if used:
        raise RuntimeError(
            "refusing to downgrade analyst_decisions while workspace-conflict decisions exist"
        )
    _replace_decision_checks(new=False)
    op.drop_index(_STATUS_INDEX, table_name=_TABLE)
    op.drop_index(_LIST_INDEX, table_name=_TABLE)
    op.drop_table(_TABLE)
