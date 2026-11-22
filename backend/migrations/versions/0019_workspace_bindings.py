"""Create the canonical workspace binding and ingest tables (M24-02, 0019).

Revision ID: 0019_workspace_bindings
Revises: 0018_editorial_enrichment_element_operations

The four tables store metadata only: no workspace file bytes ever enter
PostgreSQL through them. ``workspace_file_revisions`` is identified per file
(``workspace_id``, ``revision_id``) with a ``(workspace_id, revision_id, path)``
unique key so the later contribution lot can reference an exact revision of an
exact path. ``workspace_file_events.import_status`` stays ``received`` until a
canonical import applies the event; ``workspace_sync_cursors`` holds one cursor
per workspace generation.
"""

from __future__ import annotations

import re
from typing import Any, cast

import sqlalchemy as sa
from alembic import op

revision = "0019_workspace_bindings"
down_revision = "0018_editorial_enrichment_element_operations"
branch_labels = None
depends_on = None

_TABLE_ORDER = (
    "workspace_bindings",
    "workspace_file_revisions",
    "workspace_file_events",
    "workspace_sync_cursors",
)
_ORIGIN_VALUES = "'filesystem', 'ui', 'agent', 'system'"
_EVENT_KIND_VALUES = (
    "'file_created', 'file_modified', 'file_renamed', 'file_deleted', "
    "'conflict_created', 'policy_violation', 'sample_materialized', "
    "'sample_rejected', 'scan_completed'"
)
_IMPORT_STATUS_VALUES = "'received', 'applied', 'ignored', 'rejected'"
_POLICY_CLASS_VALUES = (
    "'human_editable', 'agent_owned', 'generated_immutable', 'read_only', 'agent_internal'"
)


def _expected_tables() -> dict[str, sa.Table]:
    metadata = sa.MetaData()
    sa.Table(
        "analyst_investigations",
        metadata,
        sa.Column("id", sa.Uuid(), primary_key=True),
    )
    bindings = sa.Table(
        "workspace_bindings",
        metadata,
        sa.Column("workspace_id", sa.String(length=64), nullable=False),
        sa.Column("investigation_id", sa.Uuid(), nullable=False),
        sa.Column("subject_id", sa.Uuid(), nullable=False),
        sa.Column("delivery_code", sa.String(length=64), nullable=False),
        sa.Column("article_slug", sa.String(length=80), nullable=False),
        sa.Column("synthesis_sha256", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("workspace_id", name="pk_workspace_bindings"),
        sa.UniqueConstraint(
            "workspace_id",
            "investigation_id",
            name="uq_workspace_bindings_workspace_investigation",
        ),
        sa.UniqueConstraint("investigation_id", name="uq_workspace_bindings_investigation"),
        sa.ForeignKeyConstraint(
            ["investigation_id"],
            ["analyst_investigations.id"],
            name="fk_workspace_bindings_investigation",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "char_length(synthesis_sha256) = 64 AND synthesis_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_workspace_bindings_synthesis_sha256",
        ),
    )
    revisions = sa.Table(
        "workspace_file_revisions",
        metadata,
        sa.Column("workspace_id", sa.String(length=64), nullable=False),
        sa.Column("revision_id", sa.String(length=64), nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("size", sa.BigInteger(), nullable=False),
        sa.Column("mode", sa.String(length=8), nullable=False),
        sa.Column("mtime_ns", sa.BigInteger(), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("origin", sa.String(length=16), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=True),
        sa.Column("supersedes_revision_id", sa.String(length=64), nullable=True),
        sa.PrimaryKeyConstraint("workspace_id", "revision_id", name="pk_workspace_file_revisions"),
        sa.UniqueConstraint(
            "workspace_id",
            "revision_id",
            "path",
            name="uq_workspace_file_revisions_workspace_revision_path",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace_bindings.workspace_id"],
            name="fk_workspace_file_revisions_workspace",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "char_length(sha256) = 64 AND sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_workspace_file_revisions_sha256",
        ),
        sa.CheckConstraint("size >= 0", name="ck_workspace_file_revisions_size"),
        sa.CheckConstraint("mtime_ns >= 0", name="ck_workspace_file_revisions_mtime_ns"),
        sa.CheckConstraint(
            f"origin IN ({_ORIGIN_VALUES})",
            name="ck_workspace_file_revisions_origin",
        ),
    )
    sa.Index(
        "ix_workspace_file_revisions_workspace_path",
        revisions.c.workspace_id,
        revisions.c.path,
        revisions.c.observed_at,
    )
    events = sa.Table(
        "workspace_file_events",
        metadata,
        sa.Column("workspace_id", sa.String(length=64), nullable=False),
        sa.Column("event_id", sa.String(length=64), nullable=False),
        sa.Column("seq", sa.BigInteger(), nullable=False),
        sa.Column("generation", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("path", sa.Text(), nullable=True),
        sa.Column("previous_path", sa.Text(), nullable=True),
        sa.Column("revision_id", sa.String(length=64), nullable=True),
        sa.Column("previous_revision_id", sa.String(length=64), nullable=True),
        sa.Column("sha256", sa.String(length=64), nullable=True),
        sa.Column("size", sa.BigInteger(), nullable=True),
        sa.Column("origin", sa.String(length=16), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=True),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("policy_class", sa.String(length=32), nullable=True),
        sa.Column(
            "import_status",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'received'"),
        ),
        sa.Column("imported_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("workspace_id", "event_id", name="pk_workspace_file_events"),
        sa.UniqueConstraint(
            "workspace_id",
            "generation",
            "seq",
            name="uq_workspace_file_events_workspace_generation_seq",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace_bindings.workspace_id"],
            name="fk_workspace_file_events_workspace",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint("seq >= 0", name="ck_workspace_file_events_seq"),
        sa.CheckConstraint("generation >= 0", name="ck_workspace_file_events_generation"),
        sa.CheckConstraint("size IS NULL OR size >= 0", name="ck_workspace_file_events_size"),
        sa.CheckConstraint(f"kind IN ({_EVENT_KIND_VALUES})", name="ck_workspace_file_events_kind"),
        sa.CheckConstraint(f"origin IN ({_ORIGIN_VALUES})", name="ck_workspace_file_events_origin"),
        sa.CheckConstraint(
            f"import_status IN ({_IMPORT_STATUS_VALUES})",
            name="ck_workspace_file_events_import_status",
        ),
        sa.CheckConstraint(
            f"policy_class IS NULL OR policy_class IN ({_POLICY_CLASS_VALUES})",
            name="ck_workspace_file_events_policy_class",
        ),
    )
    cursors = sa.Table(
        "workspace_sync_cursors",
        metadata,
        sa.Column("workspace_id", sa.String(length=64), nullable=False),
        sa.Column("generation", sa.BigInteger(), nullable=False),
        sa.Column("cursor", sa.BigInteger(), nullable=False),
        sa.Column("last_scan_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("workspace_id", name="pk_workspace_sync_cursors"),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace_bindings.workspace_id"],
            name="fk_workspace_sync_cursors_workspace",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint("generation >= 0", name="ck_workspace_sync_cursors_generation"),
        sa.CheckConstraint("cursor >= 0", name="ck_workspace_sync_cursors_cursor"),
    )
    return {
        "workspace_bindings": bindings,
        "workspace_file_revisions": revisions,
        "workspace_file_events": events,
        "workspace_sync_cursors": cursors,
    }


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
    # 0021 adds ``workspace_file_revisions.task_id -> investigation_tasks.id``
    # (ADR-0011 D2/E9) after the task table exists.  Accept that later additive
    # object so re-running the chain over a newer schema still recognises 0019's
    # own shape; every other extra still fails the preflight.
    later_constraints = {"fk_workspace_file_revisions_task"}
    for signature in sorted(actual_constraints - expected_constraints, key=repr):
        if signature[1] in later_constraints:
            continue
        mismatches.append(f"constraint {signature[1]}: expected absent, actual {signature}")

    expected_indexes = {
        (
            index.name,
            tuple(str(getattr(expression, "name", expression)) for expression in index.expressions),
            bool(index.unique),
        )
        for index in table.indexes
    }
    actual_indexes = {
        (index.get("name"), tuple(index.get("column_names", ())), bool(index.get("unique")))
        for index in inspector.get_indexes(table.name)
        if not index.get("duplicates_constraint")
    }
    for signature in sorted(expected_indexes - actual_indexes, key=repr):
        mismatches.append(f"index {signature[0]}: expected {signature}, actual absent")
    for signature in sorted(actual_indexes - expected_indexes, key=repr):
        mismatches.append(f"index {signature[0]}: expected absent, actual {signature}")
    return mismatches


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    expected = _expected_tables()
    present = [name for name in _TABLE_ORDER if inspector.has_table(name)]
    if present:
        mismatches: list[str] = []
        for name in _TABLE_ORDER:
            if not inspector.has_table(name):
                mismatches.append(f"{name}: expected present, actual absent")
                continue
            mismatches.extend(
                f"{name}: {mismatch}" for mismatch in _schema_mismatches(bind, expected[name])
            )
        if mismatches:
            raise RuntimeError("workspace binding preflight rejected: " + "; ".join(mismatches))
        return

    op.create_table(
        "workspace_bindings",
        sa.Column("workspace_id", sa.String(length=64), nullable=False),
        sa.Column("investigation_id", sa.Uuid(), nullable=False),
        sa.Column("subject_id", sa.Uuid(), nullable=False),
        sa.Column("delivery_code", sa.String(length=64), nullable=False),
        sa.Column("article_slug", sa.String(length=80), nullable=False),
        sa.Column("synthesis_sha256", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("workspace_id", name="pk_workspace_bindings"),
        sa.UniqueConstraint(
            "workspace_id",
            "investigation_id",
            name="uq_workspace_bindings_workspace_investigation",
        ),
        sa.UniqueConstraint("investigation_id", name="uq_workspace_bindings_investigation"),
        sa.ForeignKeyConstraint(
            ["investigation_id"],
            ["analyst_investigations.id"],
            name="fk_workspace_bindings_investigation",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "char_length(synthesis_sha256) = 64 AND synthesis_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_workspace_bindings_synthesis_sha256",
        ),
    )
    op.create_table(
        "workspace_file_revisions",
        sa.Column("workspace_id", sa.String(length=64), nullable=False),
        sa.Column("revision_id", sa.String(length=64), nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("size", sa.BigInteger(), nullable=False),
        sa.Column("mode", sa.String(length=8), nullable=False),
        sa.Column("mtime_ns", sa.BigInteger(), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("origin", sa.String(length=16), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=True),
        sa.Column("supersedes_revision_id", sa.String(length=64), nullable=True),
        sa.PrimaryKeyConstraint("workspace_id", "revision_id", name="pk_workspace_file_revisions"),
        sa.UniqueConstraint(
            "workspace_id",
            "revision_id",
            "path",
            name="uq_workspace_file_revisions_workspace_revision_path",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace_bindings.workspace_id"],
            name="fk_workspace_file_revisions_workspace",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "char_length(sha256) = 64 AND sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_workspace_file_revisions_sha256",
        ),
        sa.CheckConstraint("size >= 0", name="ck_workspace_file_revisions_size"),
        sa.CheckConstraint("mtime_ns >= 0", name="ck_workspace_file_revisions_mtime_ns"),
        sa.CheckConstraint(
            f"origin IN ({_ORIGIN_VALUES})",
            name="ck_workspace_file_revisions_origin",
        ),
    )
    op.create_index(
        "ix_workspace_file_revisions_workspace_path",
        "workspace_file_revisions",
        ["workspace_id", "path", "observed_at"],
    )
    op.create_table(
        "workspace_file_events",
        sa.Column("workspace_id", sa.String(length=64), nullable=False),
        sa.Column("event_id", sa.String(length=64), nullable=False),
        sa.Column("seq", sa.BigInteger(), nullable=False),
        sa.Column("generation", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("path", sa.Text(), nullable=True),
        sa.Column("previous_path", sa.Text(), nullable=True),
        sa.Column("revision_id", sa.String(length=64), nullable=True),
        sa.Column("previous_revision_id", sa.String(length=64), nullable=True),
        sa.Column("sha256", sa.String(length=64), nullable=True),
        sa.Column("size", sa.BigInteger(), nullable=True),
        sa.Column("origin", sa.String(length=16), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=True),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("policy_class", sa.String(length=32), nullable=True),
        sa.Column(
            "import_status",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'received'"),
        ),
        sa.Column("imported_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("workspace_id", "event_id", name="pk_workspace_file_events"),
        sa.UniqueConstraint(
            "workspace_id",
            "generation",
            "seq",
            name="uq_workspace_file_events_workspace_generation_seq",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace_bindings.workspace_id"],
            name="fk_workspace_file_events_workspace",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint("seq >= 0", name="ck_workspace_file_events_seq"),
        sa.CheckConstraint("generation >= 0", name="ck_workspace_file_events_generation"),
        sa.CheckConstraint("size IS NULL OR size >= 0", name="ck_workspace_file_events_size"),
        sa.CheckConstraint(f"kind IN ({_EVENT_KIND_VALUES})", name="ck_workspace_file_events_kind"),
        sa.CheckConstraint(f"origin IN ({_ORIGIN_VALUES})", name="ck_workspace_file_events_origin"),
        sa.CheckConstraint(
            f"import_status IN ({_IMPORT_STATUS_VALUES})",
            name="ck_workspace_file_events_import_status",
        ),
        sa.CheckConstraint(
            f"policy_class IS NULL OR policy_class IN ({_POLICY_CLASS_VALUES})",
            name="ck_workspace_file_events_policy_class",
        ),
    )
    op.create_table(
        "workspace_sync_cursors",
        sa.Column("workspace_id", sa.String(length=64), nullable=False),
        sa.Column("generation", sa.BigInteger(), nullable=False),
        sa.Column("cursor", sa.BigInteger(), nullable=False),
        sa.Column("last_scan_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("workspace_id", name="pk_workspace_sync_cursors"),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace_bindings.workspace_id"],
            name="fk_workspace_sync_cursors_workspace",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint("generation >= 0", name="ck_workspace_sync_cursors_generation"),
        sa.CheckConstraint("cursor >= 0", name="ck_workspace_sync_cursors_cursor"),
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    missing = [name for name in _TABLE_ORDER if not inspector.has_table(name)]
    if missing:
        raise RuntimeError(
            "refusing to downgrade workspace tables: expected present, actual absent "
            + ", ".join(missing)
        )
    for name in reversed(_TABLE_ORDER):
        bind.execute(sa.text(f"LOCK TABLE {name} IN ACCESS EXCLUSIVE MODE"))
    for name in reversed(_TABLE_ORDER):
        row_count = bind.execute(sa.text(f"SELECT count(*) FROM {name}")).scalar_one()
        if row_count:
            raise RuntimeError(f"refusing to downgrade {name} after it has been used")
    for name in reversed(_TABLE_ORDER):
        op.drop_table(name)
