"""Persist typed editorial proposals, exact model links, and proposal events.

Revision ID: 0014_editorial_proposals
Revises: 0013_bridge_generation_slots
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0014_editorial_proposals"
down_revision: str | None = "0013_bridge_generation_slots"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PROPOSALS = "editorial_proposals"
_MODEL_RUNS = "editorial_proposal_model_runs"
_EVENTS = "editorial_proposal_events"
_MUTATION_FUNCTION = "guard_editorial_proposal_mutation"
_APPEND_ONLY_FUNCTION = "reject_editorial_proposal_registry_mutation"
_EXPECTED_FUNCTIONS = {_MUTATION_FUNCTION, _APPEND_ONLY_FUNCTION}
_TRIGGERS = {
    (_PROPOSALS, "trg_editorial_proposals_identity_immutable"): (
        _MUTATION_FUNCTION,
        1 | 2 | 8 | 16,
    ),
    (_PROPOSALS, "trg_editorial_proposals_no_truncate"): (_MUTATION_FUNCTION, 2 | 32),
    (_MODEL_RUNS, "trg_editorial_proposal_model_runs_append_only"): (
        _APPEND_ONLY_FUNCTION,
        1 | 2 | 8 | 16,
    ),
    (_MODEL_RUNS, "trg_editorial_proposal_model_runs_no_truncate"): (
        _APPEND_ONLY_FUNCTION,
        2 | 32,
    ),
    (_EVENTS, "trg_editorial_proposal_events_append_only"): (
        _APPEND_ONLY_FUNCTION,
        1 | 2 | 8 | 16,
    ),
    (_EVENTS, "trg_editorial_proposal_events_no_truncate"): (
        _APPEND_ONLY_FUNCTION,
        2 | 32,
    ),
}
_FUNCTION_BODIES = {
    _MUTATION_FUNCTION: """
BEGIN
    IF TG_OP = 'DELETE' OR TG_OP = 'TRUNCATE' THEN
        RAISE EXCEPTION 'editorial_proposal_identity_cannot_be_removed';
    END IF;
    IF (
        to_jsonb(NEW)
        - ARRAY['state', 'state_version', 'updated_at', 'decision_id', 'accepted_artifact_id']
    ) IS DISTINCT FROM (
        to_jsonb(OLD)
        - ARRAY['state', 'state_version', 'updated_at', 'decision_id', 'accepted_artifact_id']
    ) THEN
        RAISE EXCEPTION 'editorial_proposal_identity_is_immutable';
    END IF;
    IF NEW.state_version <> OLD.state_version + 1 OR NOT (
        (OLD.state = 'preparing' AND NEW.state IN ('proposed', 'refused', 'needs_review')) OR
        (OLD.state = 'proposed' AND NEW.state IN ('accepted', 'rejected', 'stale'))
    ) THEN
        RAISE EXCEPTION 'editorial_proposal_transition_invalid';
    END IF;
    RETURN NEW;
END;
""",
    _APPEND_ONLY_FUNCTION: """
BEGIN
    RAISE EXCEPTION 'editorial_proposal_registry_is_append_only';
END;
""",
}

_metadata = sa.MetaData()
# Register FK targets for SQLAlchemy's dependency sorter without creating them.
for _table_name, _column_name in (
    ("subjects", "id"),
    ("production_runs", "id"),
    ("production_artifacts", "id"),
    ("editorial_workspace_snapshots", "snapshot_id"),
    ("editorial_threads", "id"),
    ("model_runs", "id"),
    ("model_conversation_turns", "id"),
    ("blobs", "id"),
):
    sa.Table(_table_name, _metadata, sa.Column(_column_name, sa.Uuid(), primary_key=True))

_operation_values = (
    "'rewrite_title', 'rewrite_lead', 'rewrite_paragraph', 'insert_paragraph', "
    "'delete_paragraph', 'rewrite_section', 'insert_section', 'delete_section', "
    "'merge_sections', 'split_section', 'rename_section', 'create_table', "
    "'rewrite_table', 'delete_table', 'create_diagram', 'rewrite_diagram', "
    "'delete_diagram', 'create_chart', 'rewrite_chart', 'delete_chart', "
    "'create_source_figure', 'rewrite_source_figure', 'replace_source_figure', "
    "'delete_source_figure'"
)
_state_values = (
    "'preparing', 'proposed', 'refused', 'needs_review', 'accepted', 'rejected', 'stale'"
)

_proposal_table = sa.Table(
    _PROPOSALS,
    _metadata,
    sa.Column("id", sa.Uuid(), primary_key=True),
    sa.Column(
        "subject_id",
        sa.Uuid(),
        sa.ForeignKey("subjects.id", name="fk_editorial_proposals_subject", ondelete="RESTRICT"),
        nullable=False,
    ),
    sa.Column(
        "production_run_id",
        sa.Uuid(),
        sa.ForeignKey("production_runs.id", name="fk_editorial_proposals_run", ondelete="RESTRICT"),
        nullable=False,
    ),
    sa.Column("origin", sa.String(32), nullable=False),
    sa.Column(
        "snapshot_id",
        sa.Uuid(),
        sa.ForeignKey(
            "editorial_workspace_snapshots.snapshot_id",
            name="fk_editorial_proposals_snapshot",
            ondelete="RESTRICT",
        ),
        nullable=False,
    ),
    sa.Column("occurrence_id", sa.Uuid(), nullable=False),
    sa.Column(
        "base_artifact_id",
        sa.Uuid(),
        sa.ForeignKey(
            "production_artifacts.id",
            name="fk_editorial_proposals_base_artifact",
            ondelete="RESTRICT",
        ),
        nullable=False,
    ),
    sa.Column("base_artifact_version", sa.BigInteger(), nullable=False),
    sa.Column("base_input_hash", sa.String(64), nullable=False),
    sa.Column("base_canonical_sha256", sa.String(64), nullable=False),
    sa.Column("base_routing_hash", sa.String(64)),
    sa.Column("base_pin_hash", sa.String(64), nullable=False),
    sa.Column("target_id", sa.String(128), nullable=False),
    sa.Column("operation", sa.String(64), nullable=False),
    sa.Column("before_hash", sa.String(64), nullable=False),
    sa.Column(
        "patch_blob_ref",
        sa.Uuid(),
        sa.ForeignKey("blobs.id", name="fk_editorial_proposals_patch_blob", ondelete="RESTRICT"),
    ),
    sa.Column("patch_sha256", sa.String(64)),
    sa.Column("evidence_refs", postgresql.JSONB(), nullable=False),
    sa.Column("diagnostics", postgresql.JSONB(), nullable=False),
    sa.Column(
        "thread_id",
        sa.Uuid(),
        sa.ForeignKey(
            "editorial_threads.id", name="fk_editorial_proposals_thread", ondelete="RESTRICT"
        ),
    ),
    sa.Column("variant_group_id", sa.Uuid(), nullable=False),
    sa.Column("state", sa.String(24), nullable=False),
    sa.Column("state_version", sa.BigInteger(), nullable=False),
    sa.Column("actor_id", sa.Uuid(), nullable=False),
    sa.Column("idempotency_key", sa.String(128), nullable=False),
    sa.Column("request_sha256", sa.String(64), nullable=False),
    sa.Column("decision_id", sa.Uuid()),
    sa.Column(
        "accepted_artifact_id",
        sa.Uuid(),
        sa.ForeignKey(
            "production_artifacts.id",
            name="fk_editorial_proposals_accepted_artifact",
            ondelete="RESTRICT",
        ),
    ),
    sa.Column(
        "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
    ),
    sa.Column(
        "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
    ),
    sa.UniqueConstraint(
        "subject_id",
        "actor_id",
        "idempotency_key",
        name="uq_editorial_proposals_subject_actor_idempotency",
    ),
    sa.CheckConstraint(
        "origin IN ('bridge_draft', 'manual_patch')", name="ck_editorial_proposals_origin"
    ),
    sa.CheckConstraint(f"state IN ({_state_values})", name="ck_editorial_proposals_state"),
    sa.CheckConstraint(
        f"operation IN ({_operation_values})", name="ck_editorial_proposals_operation"
    ),
    sa.CheckConstraint(
        "base_artifact_version >= 1 AND state_version >= 1",
        name="ck_editorial_proposals_versions",
    ),
    sa.CheckConstraint(
        "base_input_hash ~ '^[0-9a-f]{64}$' AND base_canonical_sha256 ~ '^[0-9a-f]{64}$' AND "
        "base_pin_hash ~ '^[0-9a-f]{64}$' AND before_hash ~ '^[0-9a-f]{64}$' AND "
        "request_sha256 ~ '^[0-9a-f]{64}$'",
        name="ck_editorial_proposals_hashes",
    ),
    sa.CheckConstraint(
        "base_routing_hash IS NULL OR base_routing_hash ~ '^[0-9a-f]{64}$'",
        name="ck_editorial_proposals_routing_hash",
    ),
    sa.CheckConstraint(
        "patch_sha256 IS NULL OR patch_sha256 ~ '^[0-9a-f]{64}$'",
        name="ck_editorial_proposals_patch_sha256",
    ),
    sa.CheckConstraint(
        "patch_blob_ref IS NULL AND patch_sha256 IS NULL OR "
        "patch_blob_ref IS NOT NULL AND patch_sha256 IS NOT NULL",
        name="ck_editorial_proposals_patch_reference",
    ),
    sa.CheckConstraint(
        "jsonb_typeof(evidence_refs) = 'array' AND jsonb_typeof(diagnostics) = 'object'",
        name="ck_editorial_proposals_json_shapes",
    ),
    sa.CheckConstraint(
        "origin <> 'bridge_draft' OR thread_id IS NOT NULL",
        name="ck_editorial_proposals_bridge_thread",
    ),
)
sa.Index(
    "ix_editorial_proposals_occurrence",
    _proposal_table.c.subject_id,
    _proposal_table.c.occurrence_id,
    _proposal_table.c.created_at,
)
sa.Index(
    "ix_editorial_proposals_variant", _proposal_table.c.variant_group_id, _proposal_table.c.state
)
sa.Index("ix_editorial_proposals_patch_blob", _proposal_table.c.patch_blob_ref)

_model_run_table = sa.Table(
    _MODEL_RUNS,
    _metadata,
    sa.Column(
        "proposal_id",
        sa.Uuid(),
        sa.ForeignKey(
            "editorial_proposals.id",
            name="fk_editorial_proposal_model_runs_proposal",
            ondelete="RESTRICT",
        ),
        primary_key=True,
    ),
    sa.Column(
        "thread_id",
        sa.Uuid(),
        sa.ForeignKey(
            "editorial_threads.id",
            name="fk_editorial_proposal_model_runs_thread",
            ondelete="RESTRICT",
        ),
        nullable=False,
    ),
    sa.Column(
        "model_run_id",
        sa.Uuid(),
        sa.ForeignKey(
            "model_runs.id", name="fk_editorial_proposal_model_runs_run", ondelete="RESTRICT"
        ),
        nullable=False,
    ),
    sa.Column(
        "model_turn_id",
        sa.Uuid(),
        sa.ForeignKey(
            "model_conversation_turns.id",
            name="fk_editorial_proposal_model_runs_turn",
            ondelete="RESTRICT",
        ),
        nullable=False,
    ),
    sa.Column(
        "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
    ),
    sa.UniqueConstraint("model_run_id", name="uq_editorial_proposal_model_runs_run"),
    sa.UniqueConstraint("model_turn_id", name="uq_editorial_proposal_model_runs_turn"),
    sa.CheckConstraint(
        "model_run_id IS NOT NULL AND model_turn_id IS NOT NULL",
        name="ck_editorial_proposal_model_runs_exact",
    ),
)
sa.Index("ix_editorial_proposal_model_runs_thread", _model_run_table.c.thread_id)

_event_table = sa.Table(
    _EVENTS,
    _metadata,
    sa.Column("id", sa.Uuid(), primary_key=True),
    sa.Column(
        "proposal_id",
        sa.Uuid(),
        sa.ForeignKey(
            "editorial_proposals.id",
            name="fk_editorial_proposal_events_proposal",
            ondelete="RESTRICT",
        ),
        nullable=False,
    ),
    sa.Column("sequence", sa.BigInteger(), nullable=False),
    sa.Column("kind", sa.String(24), nullable=False),
    sa.Column("state", sa.String(24), nullable=False),
    sa.Column("state_version", sa.BigInteger(), nullable=False),
    sa.Column("actor_id", sa.Uuid(), nullable=False),
    sa.Column("idempotency_key", sa.String(128), nullable=False),
    sa.Column("request_sha256", sa.String(64), nullable=False),
    sa.Column("patch_sha256", sa.String(64)),
    sa.Column("diagnostics", postgresql.JSONB(), nullable=False),
    sa.Column(
        "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
    ),
    sa.UniqueConstraint("proposal_id", "sequence", name="uq_editorial_proposal_events_sequence"),
    sa.UniqueConstraint(
        "proposal_id", "idempotency_key", name="uq_editorial_proposal_events_idempotency"
    ),
    sa.CheckConstraint(
        "sequence >= 1 AND state_version >= 1",
        name="ck_editorial_proposal_events_counters",
    ),
    sa.CheckConstraint(
        "kind IN ('prepared', 'refused', 'needs_review', 'state_changed')",
        name="ck_editorial_proposal_events_kind",
    ),
    sa.CheckConstraint(f"state IN ({_state_values})", name="ck_editorial_proposal_events_state"),
    sa.CheckConstraint(
        "request_sha256 ~ '^[0-9a-f]{64}$' AND "
        "(patch_sha256 IS NULL OR patch_sha256 ~ '^[0-9a-f]{64}$')",
        name="ck_editorial_proposal_events_hashes",
    ),
    sa.CheckConstraint(
        "jsonb_typeof(diagnostics) = 'object'",
        name="ck_editorial_proposal_events_diagnostics",
    ),
)
sa.Index(
    "ix_editorial_proposal_events_timeline", _event_table.c.proposal_id, _event_table.c.sequence
)


def _normalize_sql(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.lower()
    normalized = re.sub(r"::(?:character varying|text)(?:\[\])?", "", normalized)
    normalized = re.sub(
        r"=\s*any\s*\(\s*\(\s*array\s*\[(.*?)\]\s*\)\s*\)",
        r"in (\1)",
        normalized,
    )
    normalized = re.sub(
        r"=\s*any\s*\(\s*array\s*\[(.*?)\]\s*\)",
        r"in (\1)",
        normalized,
    )
    normalized = re.sub(r"\(([a-z_][a-z0-9_.]*)\)", r"\1", normalized)
    normalized = re.sub(
        r"\(\s*([a-z_][a-z0-9_.]*\s+in\s*\([^()]*\))\s*\)",
        r"\1",
        normalized,
    )
    normalized = re.sub(r"\s+", " ", normalized).strip()
    while normalized.startswith("(") and normalized.endswith(")"):
        depth = 0
        closes_at_end = True
        for index, character in enumerate(normalized):
            if character == "(":
                depth += 1
            elif character == ")":
                depth -= 1
                if depth == 0 and index != len(normalized) - 1:
                    closes_at_end = False
                    break
        if not closes_at_end or depth != 0:
            break
        normalized = normalized[1:-1]
    return normalized


def _table_errors(bind: Any, table: sa.Table, schema: str) -> list[str]:
    inspector = sa.inspect(bind)
    errors: list[str] = []
    table_name = table.name

    def mismatch(object_name: str, expected: object, actual: object) -> None:
        errors.append(f"{object_name}: expected {expected!r}, actual {actual!r}")

    actual_columns = {
        item["name"]: item for item in inspector.get_columns(table_name, schema=schema)
    }
    expected_columns = {column.name: column for column in table.columns}
    if set(actual_columns) != set(expected_columns):
        mismatch(f"columns {table_name}", sorted(expected_columns), sorted(actual_columns))
    for name, column in expected_columns.items():
        actual = actual_columns.get(name)
        if actual is None:
            continue
        server_default = column.server_default
        expected_default = (
            str(server_default.arg) if isinstance(server_default, sa.schema.DefaultClause) else None
        )
        for field, expected, value in (
            (
                "type",
                str(column.type.compile(dialect=bind.dialect)).lower(),
                str(actual["type"].compile(dialect=bind.dialect)).lower(),
            ),
            ("nullable", column.nullable, bool(actual["nullable"])),
            (
                "default",
                _normalize_sql(expected_default),
                _normalize_sql(str(actual["default"]) if actual["default"] is not None else None),
            ),
        ):
            if expected != value:
                mismatch(f"column {table_name}.{name}.{field}", expected, value)

    expected_primary = table.primary_key
    actual_primary = inspector.get_pk_constraint(table_name, schema=schema)
    expected_pk = (
        expected_primary.name or f"{table_name}_pkey",
        tuple(column.name for column in expected_primary.columns),
    )
    actual_pk = (actual_primary.get("name"), tuple(actual_primary.get("constrained_columns") or ()))
    if expected_pk != actual_pk:
        mismatch(f"primary key {table_name}", expected_pk, actual_pk)

    expected_unique = {
        constraint.name: tuple(column.name for column in constraint.columns)
        for constraint in table.constraints
        if isinstance(constraint, sa.UniqueConstraint)
    }
    actual_unique = {
        item.get("name"): tuple(item.get("column_names") or ())
        for item in inspector.get_unique_constraints(table_name, schema=schema)
    }
    if expected_unique != actual_unique:
        mismatch(f"unique constraints {table_name}", expected_unique, actual_unique)

    expected_checks = {
        constraint.name: _normalize_sql(str(constraint.sqltext))
        for constraint in table.constraints
        if isinstance(constraint, sa.CheckConstraint)
    }
    actual_checks = {
        item.get("name"): _normalize_sql(item.get("sqltext"))
        for item in inspector.get_check_constraints(table_name, schema=schema)
    }
    if expected_checks != actual_checks:
        mismatch(f"check constraints {table_name}", expected_checks, actual_checks)

    def fk_shape(items: list[dict[str, Any]]) -> dict[str, tuple[object, ...]]:
        result: dict[str, tuple[object, ...]] = {}
        for item in items:
            options = item.get("options") or {}
            result[str(item.get("name"))] = (
                tuple(item.get("constrained_columns") or ()),
                item.get("referred_table"),
                tuple(item.get("referred_columns") or ()),
                str(options.get("ondelete", "")).upper() or None,
                str(options.get("onupdate", "")).upper() or None,
                bool(options.get("deferrable", False)),
                str(options.get("initially", "")).upper() or None,
                str(options.get("match", "")).upper() or None,
            )
        return result

    expected_fks: dict[str, tuple[object, ...]] = {}
    for constraint in table.foreign_key_constraints:
        elements = tuple(constraint.elements)
        expected_fks[str(constraint.name)] = (
            tuple(element.parent.name for element in elements),
            elements[0].column.table.name,
            tuple(element.column.name for element in elements),
            str(constraint.ondelete).upper() if constraint.ondelete else None,
            str(constraint.onupdate).upper() if constraint.onupdate else None,
            bool(constraint.deferrable),
            str(constraint.initially).upper() if constraint.initially else None,
            str(constraint.match).upper() if constraint.match else None,
        )
    actual_fks = fk_shape(inspector.get_foreign_keys(table_name, schema=schema))
    if expected_fks != actual_fks:
        mismatch(f"foreign keys {table_name}", expected_fks, actual_fks)

    expected_indexes = {
        index.name: (
            tuple(
                expression if isinstance(expression, str) else expression.name
                for expression in index.expressions
            ),
            bool(index.unique),
            _normalize_sql(str(index.dialect_options["postgresql"].get("where")))
            if index.dialect_options["postgresql"].get("where") is not None
            else None,
        )
        for index in table.indexes
    }
    actual_indexes = {}
    for item in inspector.get_indexes(table_name, schema=schema):
        if item.get("duplicates_constraint"):
            continue
        actual_indexes[item["name"]] = (
            tuple(item.get("column_names") or ()),
            bool(item.get("unique")),
            _normalize_sql(
                str((item.get("dialect_options") or {}).get("postgresql_where"))
                if (item.get("dialect_options") or {}).get("postgresql_where") is not None
                else None
            ),
        )
    if expected_indexes != actual_indexes:
        mismatch(f"indexes {table_name}", expected_indexes, actual_indexes)

    return errors


def _guard_errors(bind: Any, schema: str) -> list[str]:
    errors: list[str] = []
    trigger_rows = (
        bind.execute(
            sa.text(
                "SELECT table_relation.relname AS table_name, trigger.tgname, "
                "trigger.tgtype::integer AS tgtype, trigger.tgenabled::text AS tgenabled, "
                "trigger.tgattr::text AS trigger_columns, "
                "trigger.tgqual IS NULL AS no_when_clause, "
                "trigger.tgisinternal, trigger.tgconstraint, trigger.tgnargs, "
                "function.proname AS function_name, function_namespace.nspname AS function_schema "
                "FROM pg_trigger trigger "
                "JOIN pg_class table_relation ON table_relation.oid = trigger.tgrelid "
                "JOIN pg_namespace table_namespace "
                "ON table_namespace.oid = table_relation.relnamespace "
                "JOIN pg_proc function ON function.oid = trigger.tgfoid "
                "JOIN pg_namespace function_namespace "
                "ON function_namespace.oid = function.pronamespace "
                "WHERE table_namespace.nspname = :schema "
                "AND table_relation.relname IN (:p, :m, :e) "
                "AND NOT trigger.tgisinternal"
            ),
            {"schema": schema, "p": _PROPOSALS, "m": _MODEL_RUNS, "e": _EVENTS},
        )
        .mappings()
        .all()
    )
    actual_triggers = {(row["table_name"], row["tgname"]): row for row in trigger_rows}
    if set(actual_triggers) != set(_TRIGGERS):
        errors.append(
            f"triggers: expected {sorted(_TRIGGERS)!r}, actual {sorted(actual_triggers)!r}"
        )
    for key, (function_name, trigger_type) in _TRIGGERS.items():
        row = actual_triggers.get(key)
        if row is None:
            continue
        actual = (
            row["function_name"],
            row["function_schema"],
            row["tgtype"],
            row["tgenabled"],
            row["trigger_columns"] or "",
            bool(row["no_when_clause"]),
            bool(row["tgisinternal"]),
            row["tgconstraint"],
            row["tgnargs"],
        )
        expected = (function_name, schema, trigger_type, "O", "", True, False, 0, 0)
        if actual != expected:
            errors.append(f"trigger {key!r}: expected {expected!r}, actual {actual!r}")

    function_rows = (
        bind.execute(
            sa.text(
                "SELECT function.proname, function.pronargs, function.pronargdefaults, "
                "function.prorettype::regtype::text AS result_type, language.lanname, "
                "function.prokind::text AS prokind, "
                "function.provolatile::text AS provolatile, "
                "function.prosecdef, function.proconfig, "
                "function.prosrc FROM pg_proc function JOIN pg_namespace namespace "
                "ON namespace.oid = function.pronamespace JOIN pg_language language "
                "ON language.oid = function.prolang WHERE namespace.nspname = :schema "
                "AND function.proname IN (:mutation, :append_only)"
            ),
            {
                "schema": schema,
                "mutation": _MUTATION_FUNCTION,
                "append_only": _APPEND_ONLY_FUNCTION,
            },
        )
        .mappings()
        .all()
    )
    actual_functions = {row["proname"]: row for row in function_rows}
    if set(actual_functions) != _EXPECTED_FUNCTIONS:
        errors.append(
            "functions: expected "
            f"{sorted(_EXPECTED_FUNCTIONS)!r}, actual {sorted(actual_functions)!r}"
        )
    if len(function_rows) != len(_EXPECTED_FUNCTIONS):
        errors.append(
            "function overloads: expected exactly one function for each guard name, "
            f"actual {[row['proname'] for row in function_rows]!r}"
        )
    for name, expected_body in _FUNCTION_BODIES.items():
        row = actual_functions.get(name)
        if row is None:
            continue
        signature = (
            row["pronargs"],
            row["pronargdefaults"],
            row["result_type"],
            row["lanname"],
            row["prokind"],
            row["provolatile"],
            bool(row["prosecdef"]),
            row["proconfig"],
        )
        expected_signature = (0, 0, "trigger", "plpgsql", "f", "v", False, None)
        if signature != expected_signature:
            errors.append(
                f"function {name} signature: expected {expected_signature!r}, actual {signature!r}"
            )
        if _normalize_sql(row["prosrc"]) != _normalize_sql(expected_body):
            errors.append(f"function {name} body does not match the migration contract")
    return errors


def _preflight() -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    schema = bind.execute(sa.text("SELECT current_schema()")).scalar_one()
    parents = {
        "subjects",
        "production_runs",
        "production_artifacts",
        "editorial_workspace_snapshots",
        "editorial_threads",
        "model_runs",
        "model_conversation_turns",
        "blobs",
    }
    missing_parents = parents - set(inspector.get_table_names(schema=schema))
    if missing_parents:
        raise RuntimeError(
            "refusing editorial proposal migration with missing parent tables: "
            + ", ".join(sorted(missing_parents))
        )

    expected_tables = {_PROPOSALS, _MODEL_RUNS, _EVENTS}
    present_tables = expected_tables & set(inspector.get_table_names(schema=schema))
    relation_rows = (
        bind.execute(
            sa.text(
                "SELECT relation.relname, relation.relkind::text AS relkind "
                "FROM pg_class relation JOIN pg_namespace namespace "
                "ON namespace.oid = relation.relnamespace "
                "WHERE namespace.nspname = :schema AND relation.relname IN (:p, :m, :e)"
            ),
            {"schema": schema, "p": _PROPOSALS, "m": _MODEL_RUNS, "e": _EVENTS},
        )
        .mappings()
        .all()
    )
    relation_kinds = {row["relname"]: row["relkind"] for row in relation_rows}
    function_count = bind.execute(
        sa.text(
            "SELECT count(*) FROM pg_proc procedure JOIN pg_namespace namespace "
            "ON namespace.oid = procedure.pronamespace WHERE namespace.nspname = :schema "
            "AND procedure.proname = ANY(:names)"
        ),
        {"schema": schema, "names": list(_EXPECTED_FUNCTIONS)},
    ).scalar_one()
    no_objects = not relation_kinds and not function_count
    if no_objects:
        return False

    errors: list[str] = []
    if present_tables != expected_tables:
        errors.append(
            f"tables: expected {sorted(expected_tables)!r}, actual {sorted(present_tables)!r}"
        )
    if relation_kinds != {table: "r" for table in expected_tables}:
        errors.append(
            "relations: expected ordinary tables "
            f"{sorted(expected_tables)!r}, actual {relation_kinds!r}"
        )
    for table in (_proposal_table, _model_run_table, _event_table):
        if table.name in present_tables:
            errors.extend(_table_errors(bind, table, schema))
    errors.extend(_guard_errors(bind, schema))
    if errors:
        raise RuntimeError(
            "refusing non-conforming editorial proposal schema or guards:\n - "
            + "\n - ".join(errors)
        )
    return True


def upgrade() -> None:
    if _preflight():
        return
    bind = op.get_bind()
    _metadata.create_all(
        bind=bind,
        tables=[_proposal_table, _model_run_table, _event_table],
        checkfirst=False,
    )
    op.execute(
        f"""
        CREATE FUNCTION {_MUTATION_FUNCTION}() RETURNS trigger AS $$
        {_FUNCTION_BODIES[_MUTATION_FUNCTION]}
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        f"""
        CREATE FUNCTION {_APPEND_ONLY_FUNCTION}() RETURNS trigger AS $$
        {_FUNCTION_BODIES[_APPEND_ONLY_FUNCTION]}
        $$ LANGUAGE plpgsql;
        """
    )
    for (table, trigger), (function, trigger_type) in _TRIGGERS.items():
        if trigger_type & 32:
            event = "TRUNCATE"
            level = "STATEMENT"
        else:
            event = "UPDATE OR DELETE"
            level = "ROW"
        op.execute(
            f"CREATE TRIGGER {trigger} BEFORE {event} ON {table} "
            f"FOR EACH {level} EXECUTE FUNCTION {function}();"
        )


def downgrade() -> None:
    if not _preflight():
        raise RuntimeError("refusing to downgrade a missing editorial proposal schema")
    bind = op.get_bind()
    bind.execute(
        sa.text(f"LOCK TABLE {_PROPOSALS}, {_MODEL_RUNS}, {_EVENTS} IN ACCESS EXCLUSIVE MODE")
    )
    if not _preflight():
        raise RuntimeError("refusing to downgrade a changed editorial proposal schema")
    rows = bind.execute(
        sa.text(
            f"SELECT (SELECT count(*) FROM {_PROPOSALS}) + "
            f"(SELECT count(*) FROM {_MODEL_RUNS}) + (SELECT count(*) FROM {_EVENTS})"
        )
    ).scalar_one()
    if rows:
        raise RuntimeError("refusing to downgrade after editorial proposal data has been written")
    for table, trigger in _TRIGGERS:
        op.execute(f"DROP TRIGGER {trigger} ON {table}")
    op.drop_table(_EVENTS)
    op.drop_table(_MODEL_RUNS)
    op.drop_table(_PROPOSALS)
    op.execute(f"DROP FUNCTION {_APPEND_ONLY_FUNCTION}()")
    op.execute(f"DROP FUNCTION {_MUTATION_FUNCTION}()")
