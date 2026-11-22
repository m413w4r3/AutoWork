"""Create the append-only work contribution table (M24-07, 0024).

Revision ID: 0024_work_contributions
Revises: 0023_agent_sessions

``work_contributions`` is the provenance of one workspace file revision, not a
second evidence store (ADR-0011 D1/D6).  The composite foreign key pins the
exact per-file revision ``(workspace_id, revision_id, path)``; the partial
unique index allows one non-rejected import per revision while a rejected row
stays terminal and auditable with a typed ``validation_error_code``.  Module,
observation, finding, sample-association and blob references are all
``RESTRICT`` so nothing canonical is silently detached.  UPDATE, DELETE and
TRUNCATE are refused by triggers.

ADR-0007 discipline: an already-conforming shape is accepted without DDL, any
partial or different shape fails before mutation, and the downgrade refuses
while any contribution row exists.
"""

from __future__ import annotations

import re
from typing import Any, cast

import sqlalchemy as sa
from alembic import op
from sqlalchemy.engine import Connection

revision = "0024_work_contributions"
down_revision = "0023_agent_sessions"
branch_labels = None
depends_on = None

_TABLE = "work_contributions"
_FUNCTION = "reject_work_contribution_mutation"
_ROW_TRIGGER = "trg_work_contributions_append_only"
_TRUNCATE_TRIGGER = "trg_work_contributions_no_truncate"
_ACTIVE_INDEX = "uq_work_contributions_active_revision"
_LIST_INDEX = "ix_work_contributions_investigation_created"
_ACTIVE_PREDICATE = "import_status <> 'rejected'"

_CATEGORY_VALUES = "'work_contribution', 'observation', 'finding'"
_STATUS_VALUES = "'imported', 'rejected', 'skipped'"
_ORIGIN_VALUES = "'human', 'agent', 'host'"

_FUNCTION_BODY = (
    "BEGIN RAISE EXCEPTION 'work contributions are append-only' USING ERRCODE = '55000'; END;"
)

_PARENT_TABLES = (
    "analyst_investigations",
    "workspace_bindings",
    "investigation_tasks",
    "workspace_file_revisions",
    "blobs",
    "evidence_observations",
    "investigation_finding_versions",
    "sample_associations",
)


def _sha256_sql(column: str) -> str:
    return f"char_length({column}) = 64 AND {column} ~ '^[0-9a-f]{{64}}$'"


def _expected_table() -> sa.Table:
    metadata = sa.MetaData()
    # Minimal parent shapes so the composite foreign keys resolve inside this
    # private metadata; the real tables are validated by their own revisions.
    sa.Table("analyst_investigations", metadata, sa.Column("id", sa.Uuid(), primary_key=True))
    sa.Table(
        "workspace_bindings",
        metadata,
        sa.Column("workspace_id", sa.String(length=64), primary_key=True),
        sa.Column("investigation_id", sa.Uuid(), nullable=False),
        sa.UniqueConstraint(
            "workspace_id", "investigation_id", name="uq_workspace_bindings_workspace_investigation"
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
    sa.Table("blobs", metadata, sa.Column("id", sa.Uuid(), primary_key=True))
    sa.Table("evidence_observations", metadata, sa.Column("id", sa.Uuid(), primary_key=True))
    sa.Table(
        "investigation_finding_versions", metadata, sa.Column("id", sa.Uuid(), primary_key=True)
    )
    sa.Table("sample_associations", metadata, sa.Column("id", sa.Uuid(), primary_key=True))
    return sa.Table(
        _TABLE,
        metadata,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("investigation_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.String(length=64), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=True),
        sa.Column("category", sa.String(length=24), nullable=False),
        sa.Column("schema_version", sa.String(length=32), nullable=False),
        sa.Column("workspace_revision_id", sa.String(length=64), nullable=False),
        sa.Column("file_relpath", sa.Text(), nullable=False),
        sa.Column("file_sha256", sa.String(length=64), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("origin", sa.String(length=16), nullable=False),
        sa.Column("origin_actor_id", sa.Text(), nullable=True),
        sa.Column("lease_fence", sa.BigInteger(), nullable=True),
        sa.Column("content_blob_id", sa.Uuid(), nullable=True),
        sa.Column("content_sha256", sa.String(length=64), nullable=True),
        sa.Column("import_status", sa.String(length=16), nullable=False),
        sa.Column("observation_id", sa.Uuid(), nullable=True),
        sa.Column("finding_id", sa.Uuid(), nullable=True),
        sa.Column("sample_association_id", sa.Uuid(), nullable=True),
        sa.Column("validation_error_code", sa.String(length=48), nullable=True),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_work_contributions"),
        sa.UniqueConstraint(
            "investigation_id",
            "idempotency_key",
            name="uq_work_contributions_investigation_idempotency",
        ),
        sa.ForeignKeyConstraint(
            ["investigation_id"],
            ["analyst_investigations.id"],
            name="fk_work_contributions_investigation",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "investigation_id"],
            ["workspace_bindings.workspace_id", "workspace_bindings.investigation_id"],
            name="fk_work_contributions_workspace",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["task_id", "investigation_id"],
            ["investigation_tasks.id", "investigation_tasks.investigation_id"],
            name="fk_work_contributions_task",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "workspace_revision_id", "file_relpath"],
            [
                "workspace_file_revisions.workspace_id",
                "workspace_file_revisions.revision_id",
                "workspace_file_revisions.path",
            ],
            name="fk_work_contributions_revision",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["content_blob_id"],
            ["blobs.id"],
            name="fk_work_contributions_content_blob",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["observation_id"],
            ["evidence_observations.id"],
            name="fk_work_contributions_observation",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["finding_id"],
            ["investigation_finding_versions.id"],
            name="fk_work_contributions_finding",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["sample_association_id"],
            ["sample_associations.id"],
            name="fk_work_contributions_sample_association",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            f"category IN ({_CATEGORY_VALUES})", name="ck_work_contributions_category"
        ),
        sa.CheckConstraint(
            "schema_version = 'work-contribution.v1'",
            name="ck_work_contributions_schema_version",
        ),
        sa.CheckConstraint(
            f"import_status IN ({_STATUS_VALUES})", name="ck_work_contributions_import_status"
        ),
        sa.CheckConstraint(f"origin IN ({_ORIGIN_VALUES})", name="ck_work_contributions_origin"),
        sa.CheckConstraint(_sha256_sql("file_sha256"), name="ck_work_contributions_file_sha256"),
        sa.CheckConstraint(
            f"content_sha256 IS NULL OR ({_sha256_sql('content_sha256')})",
            name="ck_work_contributions_content_sha256",
        ),
        sa.CheckConstraint(
            "content_sha256 IS NULL OR content_sha256 = file_sha256",
            name="ck_work_contributions_content_hash_matches_file",
        ),
        sa.CheckConstraint(
            "file_relpath <> '' AND file_relpath !~ '(^/|(^|/)\\.\\.(/|$))'",
            name="ck_work_contributions_relpath_relative",
        ),
        sa.CheckConstraint(
            "(import_status = 'imported') = (validation_error_code IS NULL)",
            name="ck_work_contributions_reverse_check",
        ),
        sa.CheckConstraint(
            "(origin <> 'agent') OR "
            "(task_id IS NOT NULL AND lease_fence IS NOT NULL AND lease_fence >= 1)",
            name="ck_work_contributions_agent_authority",
        ),
        sa.CheckConstraint(
            "origin = 'agent' OR lease_fence IS NULL",
            name="ck_work_contributions_non_agent_no_lease",
        ),
        sa.CheckConstraint(
            "import_status <> 'imported' OR "
            "(content_blob_id IS NOT NULL AND content_sha256 IS NOT NULL)",
            name="ck_work_contributions_imported_content",
        ),
        sa.CheckConstraint(
            "("
            "(import_status = 'imported' AND category = 'work_contribution' AND "
            "observation_id IS NULL AND finding_id IS NULL AND sample_association_id IS NULL)"
            " OR (import_status = 'imported' AND category = 'observation' AND "
            "observation_id IS NOT NULL AND finding_id IS NULL AND sample_association_id IS NULL)"
            " OR (import_status = 'imported' AND category = 'finding' AND "
            "finding_id IS NOT NULL AND observation_id IS NULL)"
            " OR (import_status <> 'imported' AND observation_id IS NULL AND "
            "finding_id IS NULL AND sample_association_id IS NULL)"
            ")",
            name="ck_work_contributions_category_refs",
        ),
        sa.CheckConstraint(
            "sample_association_id IS NULL OR "
            "(category = 'finding' AND import_status = 'imported')",
            name="ck_work_contributions_sample_ref",
        ),
        sa.Index(
            _ACTIVE_INDEX,
            "workspace_id",
            "workspace_revision_id",
            unique=True,
            postgresql_where=sa.text(_ACTIVE_PREDICATE),
        ),
        sa.Index(
            _LIST_INDEX,
            "investigation_id",
            "created_at",
            "id",
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
    """Every expected column, constraint and index must exist with its shape.

    Extra objects are tolerated so re-running 0024 over a schema extended by a
    later additive revision still recognises its own shape.
    """
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
    expected_constraints = _constraint_signature(table)
    actual_constraints = _actual_constraint_signature(inspector, table.name)
    for signature in sorted(expected_constraints - actual_constraints, key=repr):
        mismatches.append(
            f"{table.name}: constraint {signature[1]}: expected {signature}, actual absent"
        )
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
    definition = str(row.indexdef).casefold()
    if "unique" not in definition or " where " not in definition:
        return [f"index {_ACTIVE_INDEX}: expected a unique partial index"]
    predicate = definition.split(" where ", maxsplit=1)[1]
    if "import_status" not in predicate or "rejected" not in predicate:
        return [f"index {_ACTIVE_INDEX}: expected the non-rejected import predicate"]
    if "workspace_id" not in definition or "workspace_revision_id" not in definition:
        return [f"index {_ACTIVE_INDEX}: expected the per-file revision columns"]
    return []


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
    if inspector.has_table(_TABLE):
        mismatches.extend(_active_index_mismatches(bind))
    mismatches.extend(_guard_objects_mismatches(bind))
    return mismatches


def _create_table() -> None:
    op.create_table(
        _TABLE,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("investigation_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.String(length=64), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=True),
        sa.Column("category", sa.String(length=24), nullable=False),
        sa.Column("schema_version", sa.String(length=32), nullable=False),
        sa.Column("workspace_revision_id", sa.String(length=64), nullable=False),
        sa.Column("file_relpath", sa.Text(), nullable=False),
        sa.Column("file_sha256", sa.String(length=64), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("origin", sa.String(length=16), nullable=False),
        sa.Column("origin_actor_id", sa.Text(), nullable=True),
        sa.Column("lease_fence", sa.BigInteger(), nullable=True),
        sa.Column("content_blob_id", sa.Uuid(), nullable=True),
        sa.Column("content_sha256", sa.String(length=64), nullable=True),
        sa.Column("import_status", sa.String(length=16), nullable=False),
        sa.Column("observation_id", sa.Uuid(), nullable=True),
        sa.Column("finding_id", sa.Uuid(), nullable=True),
        sa.Column("sample_association_id", sa.Uuid(), nullable=True),
        sa.Column("validation_error_code", sa.String(length=48), nullable=True),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_work_contributions"),
        sa.UniqueConstraint(
            "investigation_id",
            "idempotency_key",
            name="uq_work_contributions_investigation_idempotency",
        ),
        sa.ForeignKeyConstraint(
            ["investigation_id"],
            ["analyst_investigations.id"],
            name="fk_work_contributions_investigation",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "investigation_id"],
            ["workspace_bindings.workspace_id", "workspace_bindings.investigation_id"],
            name="fk_work_contributions_workspace",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["task_id", "investigation_id"],
            ["investigation_tasks.id", "investigation_tasks.investigation_id"],
            name="fk_work_contributions_task",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "workspace_revision_id", "file_relpath"],
            [
                "workspace_file_revisions.workspace_id",
                "workspace_file_revisions.revision_id",
                "workspace_file_revisions.path",
            ],
            name="fk_work_contributions_revision",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["content_blob_id"],
            ["blobs.id"],
            name="fk_work_contributions_content_blob",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["observation_id"],
            ["evidence_observations.id"],
            name="fk_work_contributions_observation",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["finding_id"],
            ["investigation_finding_versions.id"],
            name="fk_work_contributions_finding",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["sample_association_id"],
            ["sample_associations.id"],
            name="fk_work_contributions_sample_association",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            f"category IN ({_CATEGORY_VALUES})", name="ck_work_contributions_category"
        ),
        sa.CheckConstraint(
            "schema_version = 'work-contribution.v1'",
            name="ck_work_contributions_schema_version",
        ),
        sa.CheckConstraint(
            f"import_status IN ({_STATUS_VALUES})", name="ck_work_contributions_import_status"
        ),
        sa.CheckConstraint(f"origin IN ({_ORIGIN_VALUES})", name="ck_work_contributions_origin"),
        sa.CheckConstraint(_sha256_sql("file_sha256"), name="ck_work_contributions_file_sha256"),
        sa.CheckConstraint(
            f"content_sha256 IS NULL OR ({_sha256_sql('content_sha256')})",
            name="ck_work_contributions_content_sha256",
        ),
        sa.CheckConstraint(
            "content_sha256 IS NULL OR content_sha256 = file_sha256",
            name="ck_work_contributions_content_hash_matches_file",
        ),
        sa.CheckConstraint(
            "file_relpath <> '' AND file_relpath !~ '(^/|(^|/)\\.\\.(/|$))'",
            name="ck_work_contributions_relpath_relative",
        ),
        sa.CheckConstraint(
            "(import_status = 'imported') = (validation_error_code IS NULL)",
            name="ck_work_contributions_reverse_check",
        ),
        sa.CheckConstraint(
            "(origin <> 'agent') OR "
            "(task_id IS NOT NULL AND lease_fence IS NOT NULL AND lease_fence >= 1)",
            name="ck_work_contributions_agent_authority",
        ),
        sa.CheckConstraint(
            "origin = 'agent' OR lease_fence IS NULL",
            name="ck_work_contributions_non_agent_no_lease",
        ),
        sa.CheckConstraint(
            "import_status <> 'imported' OR "
            "(content_blob_id IS NOT NULL AND content_sha256 IS NOT NULL)",
            name="ck_work_contributions_imported_content",
        ),
        sa.CheckConstraint(
            "("
            "(import_status = 'imported' AND category = 'work_contribution' AND "
            "observation_id IS NULL AND finding_id IS NULL AND sample_association_id IS NULL)"
            " OR (import_status = 'imported' AND category = 'observation' AND "
            "observation_id IS NOT NULL AND finding_id IS NULL AND sample_association_id IS NULL)"
            " OR (import_status = 'imported' AND category = 'finding' AND "
            "finding_id IS NOT NULL AND observation_id IS NULL)"
            " OR (import_status <> 'imported' AND observation_id IS NULL AND "
            "finding_id IS NULL AND sample_association_id IS NULL)"
            ")",
            name="ck_work_contributions_category_refs",
        ),
        sa.CheckConstraint(
            "sample_association_id IS NULL OR "
            "(category = 'finding' AND import_status = 'imported')",
            name="ck_work_contributions_sample_ref",
        ),
    )
    op.create_index(
        _ACTIVE_INDEX,
        _TABLE,
        ["workspace_id", "workspace_revision_id"],
        unique=True,
        postgresql_where=sa.text(_ACTIVE_PREDICATE),
    )
    op.create_index(
        _LIST_INDEX,
        _TABLE,
        ["investigation_id", "created_at", "id"],
    )


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
            raise RuntimeError("work contribution preflight rejected: " + "; ".join(mismatches))
        return
    inspector = sa.inspect(bind)
    missing = [name for name in _PARENT_TABLES if not inspector.has_table(name)]
    if missing:
        raise RuntimeError(
            "work contribution preflight rejected: expected present, actual absent "
            + ", ".join(missing)
        )
    bind.execute(sa.text(f"LOCK TABLE {', '.join(_PARENT_TABLES)} IN ACCESS EXCLUSIVE MODE"))
    _create_table()
    _create_guards()


def downgrade() -> None:
    bind = op.get_bind()
    if not _present_objects(bind):
        raise RuntimeError("refusing to downgrade a missing work contribution schema")
    mismatches = _full_shape_mismatches(bind)
    if mismatches:
        raise RuntimeError(
            "refusing to downgrade a partial or changed work contribution schema: "
            + "; ".join(mismatches)
        )
    bind.execute(sa.text(f"LOCK TABLE {_TABLE} IN ACCESS EXCLUSIVE MODE"))
    if bind.execute(sa.text(f"SELECT count(*) FROM {_TABLE}")).scalar_one():
        raise RuntimeError(f"refusing to downgrade {_TABLE} after it has been used")
    _drop_guards()
    op.drop_index(_LIST_INDEX, table_name=_TABLE)
    op.drop_index(_ACTIVE_INDEX, table_name=_TABLE)
    op.drop_table(_TABLE)
