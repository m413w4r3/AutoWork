"""Add occurrence-bound editorial threads and append-only events.

Revision ID: 0012_editorial_threads
Revises: 0011_editorial_workspace_snapshots
"""

import re
from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.engine import Connection

revision: str = "0012_editorial_threads"
down_revision: str | None = "0011_editorial_workspace_snapshots"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_THREADS = "editorial_threads"
_EVENTS = "editorial_thread_events"
_JOBS = "editorial_thread_jobs"
_ORIGIN_FUNCTION = "reject_editorial_thread_origin_mutation"
_EVENT_FUNCTION = "reject_editorial_thread_event_mutation"
_ORIGIN_TRIGGER = "trg_editorial_threads_origin_immutable"
_EVENT_TRIGGER = "trg_editorial_thread_events_append_only"
_EVENT_TRUNCATE_TRIGGER = "trg_editorial_thread_events_no_truncate"
_OLD_PURPOSE_VALUES = {
    "discovery",
    "analyst_assistance",
    "pivot_research",
    "drafting",
    "critic",
    "subject_research",
}
_NEW_PURPOSE_VALUES = _OLD_PURPOSE_VALUES | {"editorial_discussion"}
_OBJECTIVE_VALUES = {"editorial_message", "editorial_proposal"}

# This is the 0012 preflight contract. Keep these historical signatures local
# to the revision so later ORM changes cannot redefine what 0012 accepts.
_EXPECTED_METADATA = sa.MetaData()
for _parent in (
    "subjects",
    "production_runs",
    "production_artifacts",
    "model_conversations",
    "model_conversation_turns",
    "blobs",
):
    sa.Table(_parent, _EXPECTED_METADATA, sa.Column("id", sa.Uuid(), primary_key=True))

_EXPECTED_THREADS = sa.Table(
    _THREADS,
    _EXPECTED_METADATA,
    sa.Column("id", sa.Uuid(), nullable=False),
    sa.Column("subject_id", sa.Uuid(), nullable=False),
    sa.Column("production_run_id", sa.Uuid(), nullable=False),
    sa.Column("occurrence_id", sa.Uuid(), nullable=False),
    sa.Column("session_number", sa.BigInteger(), nullable=False),
    sa.Column("origin_artifact_id", sa.Uuid(), nullable=False),
    sa.Column("origin_artifact_version", sa.BigInteger(), nullable=False),
    sa.Column("origin_canonical_sha256", sa.String(64), nullable=False),
    sa.Column("model_conversation_id", sa.Uuid(), nullable=True),
    sa.Column("generation_state", sa.String(32), nullable=False),
    sa.Column("next_sequence", sa.BigInteger(), nullable=False),
    sa.Column("version", sa.BigInteger(), nullable=False),
    sa.Column(
        "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    ),
    sa.Column(
        "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    ),
    sa.PrimaryKeyConstraint("id", name="editorial_threads_pkey"),
    sa.ForeignKeyConstraint(
        ["subject_id"], ["subjects.id"], name="fk_editorial_threads_subject", ondelete="RESTRICT"
    ),
    sa.ForeignKeyConstraint(
        ["production_run_id"],
        ["production_runs.id"],
        name="fk_editorial_threads_run",
        ondelete="RESTRICT",
    ),
    sa.ForeignKeyConstraint(
        ["origin_artifact_id"],
        ["production_artifacts.id"],
        name="fk_editorial_threads_origin_artifact",
        ondelete="RESTRICT",
    ),
    sa.ForeignKeyConstraint(
        ["model_conversation_id"],
        ["model_conversations.id"],
        name="fk_editorial_threads_model_conversation",
        ondelete="RESTRICT",
    ),
    sa.UniqueConstraint(
        "subject_id",
        "occurrence_id",
        "session_number",
        name="uq_editorial_threads_subject_occurrence_session",
    ),
    sa.UniqueConstraint("model_conversation_id", name="uq_editorial_threads_model_conversation"),
    sa.CheckConstraint(
        "origin_artifact_version >= 1 AND char_length(origin_canonical_sha256) = 64 AND "
        "origin_canonical_sha256 ~ '^[0-9a-f]{64}$'",
        name="ck_editorial_threads_origin_fence",
    ),
    sa.CheckConstraint(
        "generation_state IN ('ready', 'waiting_for_capacity', 'generating', 'reply_available', "
        "'refused', 'session_lost', 'reconciliation_required')",
        name="ck_editorial_threads_generation_state",
    ),
    sa.CheckConstraint("next_sequence >= 1 AND version >= 1", name="ck_editorial_threads_counters"),
    sa.CheckConstraint("session_number >= 1", name="ck_editorial_threads_session_number"),
)
sa.Index(
    "ix_editorial_threads_subject_occurrence",
    _EXPECTED_THREADS.c.subject_id,
    _EXPECTED_THREADS.c.occurrence_id,
)
sa.Index(
    "ix_editorial_threads_current_session",
    _EXPECTED_THREADS.c.subject_id,
    _EXPECTED_THREADS.c.production_run_id,
    _EXPECTED_THREADS.c.occurrence_id,
    _EXPECTED_THREADS.c.session_number,
)

_EXPECTED_EVENTS = sa.Table(
    _EVENTS,
    _EXPECTED_METADATA,
    sa.Column("id", sa.Uuid(), nullable=False),
    sa.Column("thread_id", sa.Uuid(), nullable=False),
    sa.Column("sequence", sa.BigInteger(), nullable=False),
    sa.Column("event_kind", sa.String(32), nullable=False),
    sa.Column("actor_id", sa.Uuid(), nullable=True),
    sa.Column("idempotency_key", sa.String(255), nullable=False),
    sa.Column("request_sha256", sa.String(64), nullable=False),
    sa.Column("payload_blob_ref", sa.Uuid(), nullable=True),
    sa.Column("payload_sha256", sa.String(64), nullable=True),
    sa.Column("proposal_id", sa.Uuid(), nullable=True),
    sa.Column("model_turn_id", sa.Uuid(), nullable=True),
    sa.Column(
        "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    ),
    sa.PrimaryKeyConstraint("id", name="editorial_thread_events_pkey"),
    sa.ForeignKeyConstraint(
        ["thread_id"],
        [f"{_THREADS}.id"],
        name="fk_editorial_thread_events_thread",
        ondelete="RESTRICT",
    ),
    sa.ForeignKeyConstraint(
        ["payload_blob_ref"],
        ["blobs.id"],
        name="fk_editorial_thread_events_payload_blob",
        ondelete="RESTRICT",
    ),
    sa.ForeignKeyConstraint(
        ["model_turn_id"],
        ["model_conversation_turns.id"],
        name="fk_editorial_thread_events_model_turn",
        ondelete="RESTRICT",
    ),
    sa.UniqueConstraint("thread_id", "sequence", name="uq_editorial_thread_events_sequence"),
    sa.UniqueConstraint(
        "thread_id", "idempotency_key", name="uq_editorial_thread_events_idempotency"
    ),
    sa.CheckConstraint("sequence >= 1", name="ck_editorial_thread_events_sequence"),
    sa.CheckConstraint(
        "event_kind IN ('user_message', 'bridge_reply', 'proposal', 'accepted', 'rejected', "
        "'refused', 'reconciliation', 'generation')",
        name="ck_editorial_thread_events_kind",
    ),
    sa.CheckConstraint(
        "char_length(request_sha256) = 64 AND request_sha256 ~ '^[0-9a-f]{64}$'",
        name="ck_editorial_thread_events_request_hash",
    ),
    sa.CheckConstraint(
        "payload_blob_ref IS NULL AND payload_sha256 IS NULL OR payload_blob_ref IS NOT NULL "
        "AND payload_sha256 IS NOT NULL AND char_length(payload_sha256) = 64 AND "
        "payload_sha256 ~ '^[0-9a-f]{64}$'",
        name="ck_editorial_thread_events_payload_ref",
    ),
)
sa.Index(
    "ix_editorial_thread_events_timeline", _EXPECTED_EVENTS.c.thread_id, _EXPECTED_EVENTS.c.sequence
)

_EXPECTED_JOBS = sa.Table(
    _JOBS,
    _EXPECTED_METADATA,
    sa.Column("id", sa.Uuid(), nullable=False),
    sa.Column("thread_id", sa.Uuid(), nullable=False),
    sa.Column("send_event_id", sa.Uuid(), nullable=False),
    sa.Column("state", sa.String(32), nullable=False),
    sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
    sa.Column("lease_token", sa.Uuid(), nullable=True),
    sa.Column("attempt_count", sa.BigInteger(), nullable=False),
    sa.Column(
        "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    ),
    sa.Column(
        "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    ),
    sa.PrimaryKeyConstraint("id", name="editorial_thread_jobs_pkey"),
    sa.ForeignKeyConstraint(
        ["thread_id"],
        [f"{_THREADS}.id"],
        name="fk_editorial_thread_jobs_thread",
        ondelete="RESTRICT",
    ),
    sa.ForeignKeyConstraint(
        ["send_event_id"],
        [f"{_EVENTS}.id"],
        name="fk_editorial_thread_jobs_send_event",
        ondelete="RESTRICT",
    ),
    sa.UniqueConstraint("send_event_id", name="uq_editorial_thread_jobs_send_event"),
    sa.CheckConstraint(
        "state IN ('queued', 'waiting_for_capacity', 'running', 'reconciliation_required', "
        "'complete', 'failed', 'cancelled')",
        name="ck_editorial_thread_jobs_state",
    ),
    sa.CheckConstraint("attempt_count >= 0", name="ck_editorial_thread_jobs_attempt_count"),
)
sa.Index(
    "uq_editorial_thread_jobs_active_thread",
    _EXPECTED_JOBS.c.thread_id,
    unique=True,
    postgresql_where=sa.text(
        "state IN ('queued', 'waiting_for_capacity', 'running', 'reconciliation_required')"
    ),
)
sa.Index("ix_editorial_thread_jobs_lease", _EXPECTED_JOBS.c.state, _EXPECTED_JOBS.c.lease_until)


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
        normalized = normalized[1:-1].strip()
    return normalized


def _constraint_signature(table: sa.Table) -> set[tuple[Any, ...]]:
    signature: set[tuple[Any, ...]] = set()
    for constraint in table.constraints:
        if isinstance(constraint, sa.PrimaryKeyConstraint):
            signature.add(
                (
                    "p",
                    constraint.name or f"{table.name}_pkey",
                    tuple(column.name for column in constraint.columns),
                )
            )
        elif isinstance(constraint, sa.UniqueConstraint):
            signature.add(
                ("u", constraint.name, tuple(column.name for column in constraint.columns))
            )
        elif isinstance(constraint, sa.ForeignKeyConstraint):
            elements = tuple(constraint.elements)
            signature.add(
                (
                    "f",
                    constraint.name,
                    tuple(element.parent.name for element in elements),
                    elements[0].column.table.name,
                    tuple(element.column.name for element in elements),
                    (constraint.ondelete or "").upper(),
                )
            )
        elif isinstance(constraint, sa.CheckConstraint):
            signature.add(("c", constraint.name, _normalize_sql(str(constraint.sqltext))))
    return signature


def _actual_constraint_signature(inspector: sa.Inspector, table_name: str) -> set[tuple[Any, ...]]:
    signature: set[tuple[Any, ...]] = set()
    primary_key = inspector.get_pk_constraint(table_name)
    if primary_key.get("name"):
        signature.add(("p", primary_key["name"], tuple(primary_key.get("constrained_columns", ()))))
    for unique_constraint in inspector.get_unique_constraints(table_name):
        signature.add(
            (
                "u",
                unique_constraint.get("name"),
                tuple(unique_constraint.get("column_names", ())),
            )
        )
    for foreign_key in inspector.get_foreign_keys(table_name):
        signature.add(
            (
                "f",
                foreign_key.get("name"),
                tuple(foreign_key.get("constrained_columns", ())),
                foreign_key.get("referred_table"),
                tuple(foreign_key.get("referred_columns", ())),
                str(foreign_key.get("options", {}).get("ondelete", "")).upper(),
            )
        )
    for check_constraint in inspector.get_check_constraints(table_name):
        sqltext = check_constraint.get("sqltext")
        signature.add(
            (
                "c",
                check_constraint.get("name"),
                _normalize_sql(sqltext if isinstance(sqltext, str) else None),
            )
        )
    return signature


def _table_matches(bind: Connection, table: sa.Table) -> bool:
    inspector = sa.inspect(bind)
    actual_columns = {column["name"]: column for column in inspector.get_columns(table.name)}
    if set(actual_columns) != set(table.columns.keys()):
        return False
    for column in table.columns:
        actual = actual_columns[column.name]
        expected_type = column.type.compile(dialect=bind.dialect).lower()
        actual_type = actual["type"].compile(dialect=bind.dialect).lower()
        if actual_type != expected_type or actual["nullable"] != column.nullable:
            return False
        default_arg = getattr(column.server_default, "arg", None)
        expected_default = str(default_arg) if default_arg is not None else None
        actual_default = actual.get("default")
        if _normalize_sql(actual_default) != _normalize_sql(expected_default):
            return False

    if _actual_constraint_signature(inspector, table.name) != _constraint_signature(table):
        return False

    expected_indexes = {
        (
            index.name,
            tuple(str(getattr(expression, "name", expression)) for expression in index.expressions),
            bool(index.unique),
            _normalize_sql(str(index.dialect_options["postgresql"].get("where")))
            if index.dialect_options["postgresql"].get("where") is not None
            else None,
        )
        for index in table.indexes
    }
    actual_indexes = set()
    for index in inspector.get_indexes(table.name):
        if index.get("duplicates_constraint"):
            continue
        where = index.get("dialect_options", {}).get("postgresql_where")
        actual_indexes.add(
            (
                index.get("name"),
                tuple(index.get("column_names", ())),
                bool(index.get("unique")),
                _normalize_sql(str(where)) if where is not None else None,
            )
        )
    return actual_indexes == expected_indexes


def _table_mismatch_summary(bind: Connection, table: sa.Table) -> dict[str, object]:
    inspector = sa.inspect(bind)
    actual_columns = {column["name"] for column in inspector.get_columns(table.name)}
    expected_columns = set(table.columns.keys())
    actual_constraints = _actual_constraint_signature(inspector, table.name)
    expected_constraints = _constraint_signature(table)
    actual_indexes = {
        (
            index.get("name"),
            tuple(index.get("column_names", ())),
            bool(index.get("unique")),
            _normalize_sql(str(index.get("dialect_options", {}).get("postgresql_where")))
            if index.get("dialect_options", {}).get("postgresql_where") is not None
            else None,
        )
        for index in inspector.get_indexes(table.name)
        if not index.get("duplicates_constraint")
    }
    expected_indexes = {
        (
            index.name,
            tuple(str(getattr(expression, "name", expression)) for expression in index.expressions),
            bool(index.unique),
            _normalize_sql(str(index.dialect_options["postgresql"].get("where")))
            if index.dialect_options["postgresql"].get("where") is not None
            else None,
        )
        for index in table.indexes
    }
    return {
        "columns_missing": sorted(expected_columns - actual_columns),
        "columns_unexpected": sorted(actual_columns - expected_columns),
        "constraints_missing": sorted(
            repr(item) for item in expected_constraints - actual_constraints
        ),
        "constraints_unexpected": sorted(
            repr(item) for item in actual_constraints - expected_constraints
        ),
        "indexes_missing": sorted(repr(item) for item in expected_indexes - actual_indexes),
        "indexes_unexpected": sorted(repr(item) for item in actual_indexes - expected_indexes),
    }


def _conversation_extension_state(inspector: sa.Inspector) -> str:
    conversation_columns = {item["name"] for item in inspector.get_columns("model_conversations")}
    turn_columns = {
        item["name"]: item for item in inspector.get_columns("model_conversation_turns")
    }
    conversation_checks = {
        item["name"]: item["sqltext"]
        for item in inspector.get_check_constraints("model_conversations")
    }
    turn_checks = {
        item["name"]: item["sqltext"]
        for item in inspector.get_check_constraints("model_conversation_turns")
    }
    purpose_check = conversation_checks.get("ck_model_conversations_purpose")
    purpose_values = set(re.findall(r"'([^']+)'", purpose_check or ""))
    old_purpose_sql = (
        "purpose IN ('discovery', 'analyst_assistance', 'pivot_research', 'drafting', "
        "'critic', 'subject_research')"
    )
    new_purpose_sql = (
        "purpose IN ('discovery', 'analyst_assistance', 'pivot_research', 'drafting', "
        "'critic', 'subject_research', 'editorial_discussion')"
    )
    if (
        "objective" in conversation_columns
        or "ck_model_conversations_purpose" not in conversation_checks
    ):
        return "partial"
    objective = turn_columns.get("objective")
    objective_check = turn_checks.get("ck_model_conversation_turns_objective")
    if (
        objective is None
        and objective_check is None
        and purpose_values == _OLD_PURPOSE_VALUES
        and _normalize_sql(purpose_check) == _normalize_sql(old_purpose_sql)
    ):
        return "absent"
    objective_type = objective["type"] if objective is not None else None
    if (
        objective is not None
        and isinstance(objective_type, sa.String)
        and objective_type.length == 32
        and objective["nullable"]
        and objective_check is not None
        and purpose_values == _NEW_PURPOSE_VALUES
        and _normalize_sql(purpose_check) == _normalize_sql(new_purpose_sql)
        and _normalize_sql(objective_check)
        == _normalize_sql(
            "objective IS NULL OR objective IN ('editorial_message', 'editorial_proposal')"
        )
    ):
        return "present"
    return "partial"


def _guard_objects_match(bind: Connection, diagnostics: list[str] | None = None) -> bool:
    expected_functions = {
        _ORIGIN_FUNCTION: (
            "BEGIN IF ROW(OLD.subject_id, OLD.production_run_id, OLD.occurrence_id, "
            "OLD.session_number, "
            "OLD.origin_artifact_id, OLD.origin_artifact_version, OLD.origin_canonical_sha256) "
            "IS DISTINCT FROM ROW(NEW.subject_id, NEW.production_run_id, NEW.occurrence_id, "
            "NEW.session_number, "
            "NEW.origin_artifact_id, NEW.origin_artifact_version, "
            "NEW.origin_canonical_sha256) THEN RAISE EXCEPTION "
            "'editorial thread origin is immutable' USING ERRCODE = '55000'; "
            "END IF; RETURN NEW; END;"
        ),
        _EVENT_FUNCTION: (
            "BEGIN RAISE EXCEPTION 'editorial thread events are append-only' "
            "USING ERRCODE = '55000'; END;"
        ),
    }
    rows = bind.execute(
        sa.text(
            "SELECT p.proname, l.lanname, p.prosrc FROM pg_proc p "
            "JOIN pg_namespace n ON n.oid = p.pronamespace "
            "JOIN pg_language l ON l.oid = p.prolang "
            "WHERE n.nspname = current_schema() AND p.proname IN "
            "(:origin_function, :event_function)"
        ),
        {"origin_function": _ORIGIN_FUNCTION, "event_function": _EVENT_FUNCTION},
    ).all()
    actual_functions = {row.proname: (row.lanname, _normalize_sql(row.prosrc)) for row in rows}
    if set(actual_functions) != set(expected_functions):
        if diagnostics is not None:
            diagnostics.append("function_names")
        return False
    if any(
        actual_functions[name] != ("plpgsql", _normalize_sql(body))
        for name, body in expected_functions.items()
    ):
        if diagnostics is not None:
            diagnostics.append("function_bodies")
        return False

    expected_triggers = {
        _ORIGIN_TRIGGER: ("editorial_threads", 19, _ORIGIN_FUNCTION),
        _EVENT_TRIGGER: ("editorial_thread_events", 27, _EVENT_FUNCTION),
        _EVENT_TRUNCATE_TRIGGER: ("editorial_thread_events", 34, _EVENT_FUNCTION),
    }
    trigger_rows = bind.execute(
        sa.text(
            "SELECT t.tgname, t.tgrelid::regclass::text AS table_name, t.tgtype, "
            "t.tgenabled, t.tgisinternal, t.tgqual IS NULL AS no_condition, "
            "p.proname AS function_name, pg_get_triggerdef(t.oid) AS definition "
            "FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "JOIN pg_proc p ON p.oid = t.tgfoid "
            "WHERE n.nspname = current_schema() AND c.relname IN "
            "(:threads, :events) AND NOT t.tgisinternal"
        ),
        {"threads": _THREADS, "events": _EVENTS},
    ).all()
    if {row.tgname for row in trigger_rows} != set(expected_triggers):
        if diagnostics is not None:
            diagnostics.append("trigger_names")
        return False
    for row in trigger_rows:
        expected_table, expected_type, expected_function = expected_triggers[row.tgname]
        actual_table = row.table_name.rsplit(".", maxsplit=1)[-1].strip('"')
        if (
            actual_table != expected_table
            or row.tgtype != expected_type
            or row.tgenabled not in ("O", b"O")
            or row.tgisinternal
            or not row.no_condition
            or row.function_name != expected_function
        ):
            if diagnostics is not None:
                diagnostics.append(
                    "trigger_attributes:"
                    f"{row.tgname}:{actual_table}:{row.tgtype}:{row.tgenabled}:"
                    f"{row.tgisinternal}:{row.no_condition}:{row.function_name}"
                )
            return False
        definition = re.sub(r"\b(?:public\.)", "", row.definition.lower())
        definition = re.sub(r'"([a-z_]+)"', r"\1", definition)
        definition = re.sub(r"\s+", " ", definition).strip()
        if row.tgname == _ORIGIN_TRIGGER:
            expected_definition = (
                "create trigger trg_editorial_threads_origin_immutable before update of "
                "subject_id, production_run_id, occurrence_id, session_number, origin_artifact_id, "
                "origin_artifact_version, origin_canonical_sha256 on editorial_threads "
                "for each row execute function reject_editorial_thread_origin_mutation()"
            )
        elif row.tgname == _EVENT_TRIGGER:
            expected_definition = (
                "create trigger trg_editorial_thread_events_append_only before update or delete "
                "on editorial_thread_events for each row execute function "
                "reject_editorial_thread_event_mutation()"
            )
        else:
            expected_definition = (
                "create trigger trg_editorial_thread_events_no_truncate before truncate on "
                "editorial_thread_events for each statement execute function "
                "reject_editorial_thread_event_mutation()"
            )
        definition_matches = definition == expected_definition
        if row.tgname == _EVENT_TRIGGER:
            definition_matches = definition in {
                expected_definition,
                expected_definition.replace("before update or delete", "before delete or update"),
            }
        if not definition_matches:
            if diagnostics is not None:
                diagnostics.append(f"trigger_definition:{row.tgname}:{definition}")
            return False
    return True


def _preflight() -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    expected_parents = {
        "subjects",
        "production_runs",
        "production_artifacts",
        "blobs",
        "model_conversations",
        "model_conversation_turns",
    }
    missing = expected_parents - tables
    if missing:
        raise RuntimeError(
            "refusing editorial thread migration with missing parent tables: "
            + ", ".join(sorted(missing))
        )
    thread_tables = {_THREADS, _EVENTS, _JOBS} & tables
    extension_state = _conversation_extension_state(inspector)
    if not thread_tables and extension_state == "absent":
        functions_or_triggers = bind.execute(
            sa.text(
                "SELECT EXISTS (SELECT 1 FROM pg_proc WHERE proname IN "
                "(:origin_function, :event_function)) OR EXISTS (SELECT 1 FROM pg_trigger "
                "WHERE tgname IN (:origin_trigger, :event_trigger, :truncate_trigger) "
                "AND NOT tgisinternal)"
            ),
            {
                "origin_function": _ORIGIN_FUNCTION,
                "event_function": _EVENT_FUNCTION,
                "origin_trigger": _ORIGIN_TRIGGER,
                "event_trigger": _EVENT_TRIGGER,
                "truncate_trigger": _EVENT_TRUNCATE_TRIGGER,
            },
        ).scalar_one()
        if functions_or_triggers:
            raise RuntimeError("refusing to create partial editorial thread guards")
        return False

    expected_tables = {
        _THREADS: _EXPECTED_THREADS,
        _EVENTS: _EXPECTED_EVENTS,
        _JOBS: _EXPECTED_JOBS,
    }
    table_matches = {name: _table_matches(bind, table) for name, table in expected_tables.items()}
    guard_diagnostics: list[str] = []
    guards_match = _guard_objects_match(bind, guard_diagnostics)
    complete_schema = (
        thread_tables == set(expected_tables)
        and extension_state == "present"
        and all(table_matches.values())
        and guards_match
    )
    if not complete_schema:
        extension_details = {
            "purpose_check": {
                item["name"]: item["sqltext"]
                for item in inspector.get_check_constraints("model_conversations")
                if item["name"] == "ck_model_conversations_purpose"
            },
            "objective_column": {
                item["name"]: (str(item["type"]), item["nullable"])
                for item in inspector.get_columns("model_conversation_turns")
                if item["name"] == "objective"
            },
            "objective_check": {
                item["name"]: item["sqltext"]
                for item in inspector.get_check_constraints("model_conversation_turns")
                if item["name"] == "ck_model_conversation_turns_objective"
            },
        }
        mismatch_summary = {
            name: _table_mismatch_summary(bind, table)
            for name, table in expected_tables.items()
            if not table_matches[name]
        }
        raise RuntimeError(
            "refusing editorial thread migration from a partial or non-conforming schema "
            f"(tables={sorted(thread_tables)}, extension={extension_state}, "
            f"extension_details={extension_details}, "
            f"conforming_tables={table_matches}, table_mismatches={mismatch_summary}, "
            f"guards={guards_match}, guard_mismatches={guard_diagnostics})"
        )
    return True


def upgrade() -> None:
    if _preflight():
        return

    op.add_column("model_conversation_turns", sa.Column("objective", sa.String(32), nullable=True))
    op.create_check_constraint(
        "ck_model_conversation_turns_objective",
        "model_conversation_turns",
        "objective IS NULL OR objective IN ('editorial_message', 'editorial_proposal')",
    )
    op.drop_constraint("ck_model_conversations_purpose", "model_conversations", type_="check")
    op.create_check_constraint(
        "ck_model_conversations_purpose",
        "model_conversations",
        "purpose IN ('discovery', 'analyst_assistance', 'pivot_research', 'drafting', "
        "'critic', 'subject_research', 'editorial_discussion')",
    )

    op.create_table(
        _THREADS,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("subject_id", sa.Uuid(), nullable=False),
        sa.Column("production_run_id", sa.Uuid(), nullable=False),
        sa.Column("occurrence_id", sa.Uuid(), nullable=False),
        sa.Column("session_number", sa.BigInteger(), nullable=False),
        sa.Column("origin_artifact_id", sa.Uuid(), nullable=False),
        sa.Column("origin_artifact_version", sa.BigInteger(), nullable=False),
        sa.Column("origin_canonical_sha256", sa.String(64), nullable=False),
        sa.Column("model_conversation_id", sa.Uuid(), nullable=True),
        sa.Column("generation_state", sa.String(32), nullable=False),
        sa.Column("next_sequence", sa.BigInteger(), nullable=False),
        sa.Column("version", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.PrimaryKeyConstraint("id", name="editorial_threads_pkey"),
        sa.ForeignKeyConstraint(
            ["subject_id"],
            ["subjects.id"],
            name="fk_editorial_threads_subject",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["production_run_id"],
            ["production_runs.id"],
            name="fk_editorial_threads_run",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["origin_artifact_id"],
            ["production_artifacts.id"],
            name="fk_editorial_threads_origin_artifact",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["model_conversation_id"],
            ["model_conversations.id"],
            name="fk_editorial_threads_model_conversation",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "subject_id",
            "occurrence_id",
            "session_number",
            name="uq_editorial_threads_subject_occurrence_session",
        ),
        sa.UniqueConstraint(
            "model_conversation_id", name="uq_editorial_threads_model_conversation"
        ),
        sa.CheckConstraint(
            "origin_artifact_version >= 1 AND "
            "char_length(origin_canonical_sha256) = 64 AND "
            "origin_canonical_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_editorial_threads_origin_fence",
        ),
        sa.CheckConstraint(
            "generation_state IN ('ready', 'waiting_for_capacity', 'generating', "
            "'reply_available', 'refused', 'session_lost', 'reconciliation_required')",
            name="ck_editorial_threads_generation_state",
        ),
        sa.CheckConstraint(
            "next_sequence >= 1 AND version >= 1", name="ck_editorial_threads_counters"
        ),
        sa.CheckConstraint("session_number >= 1", name="ck_editorial_threads_session_number"),
    )
    op.create_index(
        "ix_editorial_threads_subject_occurrence",
        _THREADS,
        ["subject_id", "occurrence_id"],
    )
    op.create_index(
        "ix_editorial_threads_current_session",
        _THREADS,
        ["subject_id", "production_run_id", "occurrence_id", "session_number"],
    )

    op.create_table(
        _EVENTS,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("thread_id", sa.Uuid(), nullable=False),
        sa.Column("sequence", sa.BigInteger(), nullable=False),
        sa.Column("event_kind", sa.String(32), nullable=False),
        sa.Column("actor_id", sa.Uuid(), nullable=True),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column("request_sha256", sa.String(64), nullable=False),
        sa.Column("payload_blob_ref", sa.Uuid(), nullable=True),
        sa.Column("payload_sha256", sa.String(64), nullable=True),
        sa.Column("proposal_id", sa.Uuid(), nullable=True),
        sa.Column("model_turn_id", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.PrimaryKeyConstraint("id", name="editorial_thread_events_pkey"),
        sa.ForeignKeyConstraint(
            ["thread_id"],
            [f"{_THREADS}.id"],
            name="fk_editorial_thread_events_thread",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["payload_blob_ref"],
            ["blobs.id"],
            name="fk_editorial_thread_events_payload_blob",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["model_turn_id"],
            ["model_conversation_turns.id"],
            name="fk_editorial_thread_events_model_turn",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("thread_id", "sequence", name="uq_editorial_thread_events_sequence"),
        sa.UniqueConstraint(
            "thread_id", "idempotency_key", name="uq_editorial_thread_events_idempotency"
        ),
        sa.CheckConstraint("sequence >= 1", name="ck_editorial_thread_events_sequence"),
        sa.CheckConstraint(
            "event_kind IN ('user_message', 'bridge_reply', 'proposal', 'accepted', 'rejected', "
            "'refused', 'reconciliation', 'generation')",
            name="ck_editorial_thread_events_kind",
        ),
        sa.CheckConstraint(
            "char_length(request_sha256) = 64 AND request_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_editorial_thread_events_request_hash",
        ),
        sa.CheckConstraint(
            "payload_blob_ref IS NULL AND payload_sha256 IS NULL OR "
            "payload_blob_ref IS NOT NULL AND payload_sha256 IS NOT NULL AND "
            "char_length(payload_sha256) = 64 AND payload_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_editorial_thread_events_payload_ref",
        ),
    )
    op.create_index("ix_editorial_thread_events_timeline", _EVENTS, ["thread_id", "sequence"])

    op.create_table(
        _JOBS,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("thread_id", sa.Uuid(), nullable=False),
        sa.Column("send_event_id", sa.Uuid(), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_token", sa.Uuid(), nullable=True),
        sa.Column("attempt_count", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.PrimaryKeyConstraint("id", name="editorial_thread_jobs_pkey"),
        sa.ForeignKeyConstraint(
            ["thread_id"],
            [f"{_THREADS}.id"],
            name="fk_editorial_thread_jobs_thread",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["send_event_id"],
            [f"{_EVENTS}.id"],
            name="fk_editorial_thread_jobs_send_event",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("send_event_id", name="uq_editorial_thread_jobs_send_event"),
        sa.CheckConstraint(
            "state IN ('queued', 'waiting_for_capacity', 'running', 'reconciliation_required', "
            "'complete', 'failed', 'cancelled')",
            name="ck_editorial_thread_jobs_state",
        ),
        sa.CheckConstraint("attempt_count >= 0", name="ck_editorial_thread_jobs_attempt_count"),
    )
    op.create_index(
        "uq_editorial_thread_jobs_active_thread",
        _JOBS,
        ["thread_id"],
        unique=True,
        postgresql_where=sa.text(
            "state IN ('queued', 'waiting_for_capacity', 'running', 'reconciliation_required')"
        ),
    )
    op.create_index("ix_editorial_thread_jobs_lease", _JOBS, ["state", "lease_until"])

    op.execute(
        f"""
        CREATE FUNCTION {_ORIGIN_FUNCTION}() RETURNS trigger AS $$
        BEGIN
            IF ROW(OLD.subject_id, OLD.production_run_id, OLD.occurrence_id, OLD.session_number,
                   OLD.origin_artifact_id, OLD.origin_artifact_version,
                   OLD.origin_canonical_sha256)
               IS DISTINCT FROM
               ROW(NEW.subject_id, NEW.production_run_id, NEW.occurrence_id, NEW.session_number,
                   NEW.origin_artifact_id, NEW.origin_artifact_version,
                   NEW.origin_canonical_sha256) THEN
                RAISE EXCEPTION 'editorial thread origin is immutable' USING ERRCODE = '55000';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        f"""
        CREATE TRIGGER {_ORIGIN_TRIGGER}
        BEFORE UPDATE OF subject_id, production_run_id, occurrence_id, session_number,
                         origin_artifact_id, origin_artifact_version, origin_canonical_sha256
        ON {_THREADS}
        FOR EACH ROW EXECUTE FUNCTION {_ORIGIN_FUNCTION}();
        """
    )
    op.execute(
        f"""
        CREATE FUNCTION {_EVENT_FUNCTION}() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'editorial thread events are append-only' USING ERRCODE = '55000';
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        f"""
        CREATE TRIGGER {_EVENT_TRIGGER}
        BEFORE UPDATE OR DELETE ON {_EVENTS}
        FOR EACH ROW EXECUTE FUNCTION {_EVENT_FUNCTION}();
        """
    )
    op.execute(
        f"""
        CREATE TRIGGER {_EVENT_TRUNCATE_TRIGGER}
        BEFORE TRUNCATE ON {_EVENTS}
        FOR EACH STATEMENT EXECUTE FUNCTION {_EVENT_FUNCTION}();
        """
    )


def downgrade() -> None:
    if not _preflight():
        raise RuntimeError("refusing to downgrade a missing editorial thread schema")
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    if not {_THREADS, _EVENTS, _JOBS} <= tables:
        raise RuntimeError("refusing to downgrade a partial editorial thread schema")

    triggers = (
        bind.execute(
            sa.text(
                "SELECT tgname FROM pg_trigger WHERE tgrelid IN "
                "(to_regclass(:threads), to_regclass(:events)) "
                "AND tgname IN (:origin_trigger, :event_trigger, :truncate_trigger) "
                "AND NOT tgisinternal"
            ),
            {
                "threads": _THREADS,
                "events": _EVENTS,
                "origin_trigger": _ORIGIN_TRIGGER,
                "event_trigger": _EVENT_TRIGGER,
                "truncate_trigger": _EVENT_TRUNCATE_TRIGGER,
            },
        )
        .scalars()
        .all()
    )
    functions = (
        bind.execute(
            sa.text(
                "SELECT proname FROM pg_proc WHERE proname IN (:origin_function, :event_function)"
            ),
            {"origin_function": _ORIGIN_FUNCTION, "event_function": _EVENT_FUNCTION},
        )
        .scalars()
        .all()
    )
    if set(triggers) != {_ORIGIN_TRIGGER, _EVENT_TRIGGER, _EVENT_TRUNCATE_TRIGGER} or set(
        functions
    ) != {_ORIGIN_FUNCTION, _EVENT_FUNCTION}:
        raise RuntimeError("refusing to downgrade a partial editorial thread guard")

    for table in (_THREADS, _EVENTS, _JOBS, "model_conversations", "model_conversation_turns"):
        bind.execute(sa.text(f"LOCK TABLE {table} IN ACCESS EXCLUSIVE MODE"))
    if not _preflight():
        raise RuntimeError("refusing to downgrade a changed editorial thread schema")
    has_thread_rows = any(
        bind.execute(sa.text(f"SELECT EXISTS (SELECT 1 FROM {table})")).scalar_one()
        for table in (_THREADS, _EVENTS, _JOBS)
    )
    has_new_conversation_rows = bind.execute(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM model_conversations "
            "WHERE purpose = 'editorial_discussion') OR "
            "EXISTS (SELECT 1 FROM model_conversation_turns WHERE objective IS NOT NULL)"
        )
    ).scalar_one()
    if has_thread_rows or has_new_conversation_rows:
        raise RuntimeError("refusing to downgrade while editorial thread history exists")

    op.execute(f"DROP TRIGGER {_ORIGIN_TRIGGER} ON {_THREADS}")
    op.execute(f"DROP TRIGGER {_EVENT_TRIGGER} ON {_EVENTS}")
    op.execute(f"DROP TRIGGER {_EVENT_TRUNCATE_TRIGGER} ON {_EVENTS}")
    op.drop_index("ix_editorial_thread_jobs_lease", table_name=_JOBS)
    op.drop_index("uq_editorial_thread_jobs_active_thread", table_name=_JOBS)
    op.drop_table(_JOBS)
    op.drop_index("ix_editorial_thread_events_timeline", table_name=_EVENTS)
    op.drop_table(_EVENTS)
    op.drop_index("ix_editorial_threads_current_session", table_name=_THREADS)
    op.drop_index("ix_editorial_threads_subject_occurrence", table_name=_THREADS)
    op.drop_table(_THREADS)
    op.execute(f"DROP FUNCTION {_EVENT_FUNCTION}()")
    op.execute(f"DROP FUNCTION {_ORIGIN_FUNCTION}()")

    op.drop_constraint(
        "ck_model_conversation_turns_objective", "model_conversation_turns", type_="check"
    )
    op.drop_column("model_conversation_turns", "objective")
    op.drop_constraint("ck_model_conversations_purpose", "model_conversations", type_="check")
    op.create_check_constraint(
        "ck_model_conversations_purpose",
        "model_conversations",
        "purpose IN ('discovery', 'analyst_assistance', 'pivot_research', 'drafting', "
        "'critic', 'subject_research')",
    )
