"""Add mutable, noncanonical editorial server drafts.

Revision ID: 0016_editorial_drafts
Revises: 0015_editorial_proposal_impacts
"""

from __future__ import annotations

import re
from typing import Any, cast

import sqlalchemy as sa
from alembic import op

revision = "0016_editorial_drafts"
down_revision = "0015_editorial_proposal_impacts"
branch_labels = None
depends_on = None

_TABLE = "editorial_drafts"


def _expected_table() -> sa.Table:
    metadata = sa.MetaData()
    for name, column in (
        ("subjects", "id"),
        ("production_runs", "id"),
        ("editorial_workspace_snapshots", "snapshot_id"),
        ("production_artifacts", "id"),
        ("blobs", "id"),
    ):
        sa.Table(name, metadata, sa.Column(column, sa.Uuid(), primary_key=True))
    table = sa.Table(
        _TABLE,
        metadata,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("subject_id", sa.Uuid(), nullable=False),
        sa.Column("production_run_id", sa.Uuid(), nullable=False),
        sa.Column("occurrence_id", sa.Uuid(), nullable=False),
        sa.Column("base_snapshot_id", sa.Uuid(), nullable=False),
        sa.Column("base_artifact_id", sa.Uuid(), nullable=False),
        sa.Column("base_artifact_version", sa.Integer(), nullable=False),
        sa.Column("base_canonical_sha256", sa.String(length=64), nullable=False),
        sa.Column("actor_id", sa.Uuid(), nullable=False),
        sa.Column("text_blob_ref", sa.Uuid(), nullable=False),
        sa.Column("text_sha256", sa.String(length=64), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("base_artifact_version >= 1", name="ck_editorial_drafts_base_version"),
        sa.CheckConstraint("version >= 1", name="ck_editorial_drafts_version"),
        sa.CheckConstraint(
            "char_length(base_canonical_sha256) = 64 AND base_canonical_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_editorial_drafts_base_sha256",
        ),
        sa.CheckConstraint(
            "char_length(text_sha256) = 64 AND text_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_editorial_drafts_text_sha256",
        ),
        sa.CheckConstraint("expires_at > updated_at", name="ck_editorial_drafts_expiry"),
        sa.ForeignKeyConstraint(
            ["subject_id"], ["subjects.id"], name="fk_editorial_drafts_subject", ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["production_run_id"],
            ["production_runs.id"],
            name="fk_editorial_drafts_run",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["base_snapshot_id"],
            ["editorial_workspace_snapshots.snapshot_id"],
            name="fk_editorial_drafts_snapshot",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["base_artifact_id"],
            ["production_artifacts.id"],
            name="fk_editorial_drafts_artifact",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["text_blob_ref"],
            ["blobs.id"],
            name="fk_editorial_drafts_text_blob",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_editorial_drafts"),
        sa.UniqueConstraint(
            "subject_id", "occurrence_id", name="uq_editorial_drafts_subject_occurrence"
        ),
    )
    sa.Index("ix_editorial_drafts_expiry", table.c.expires_at)
    return table


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
    normalized = re.sub(r"\(([a-z_][a-z0-9_.]*)\)", r"\1", normalized)
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


def _actual_constraint_signature(inspector: sa.Inspector) -> set[tuple[Any, ...]]:
    result: set[tuple[Any, ...]] = set()
    primary_key = inspector.get_pk_constraint(_TABLE)
    if primary_key.get("name"):
        result.add(("p", primary_key["name"], tuple(primary_key.get("constrained_columns", ()))))
    for unique_constraint in inspector.get_unique_constraints(_TABLE):
        result.add(
            (
                "u",
                unique_constraint.get("name"),
                tuple(unique_constraint.get("column_names", ())),
            )
        )
    for foreign_key in inspector.get_foreign_keys(_TABLE):
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
    for check_constraint in inspector.get_check_constraints(_TABLE):
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
    actual_columns = {item["name"]: item for item in inspector.get_columns(_TABLE)}
    expected_columns = set(table.columns.keys())
    for name in sorted(expected_columns | set(actual_columns)):
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
    actual_constraints = _actual_constraint_signature(inspector)
    for signature in sorted(expected_constraints - actual_constraints, key=repr):
        mismatches.append(f"constraint {signature[1]}: expected {signature}, actual absent")
    for signature in sorted(actual_constraints - expected_constraints, key=repr):
        mismatches.append(f"constraint {signature[1]}: expected absent, actual {signature}")

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
    for index in inspector.get_indexes(_TABLE):
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
    for signature in sorted(expected_indexes - actual_indexes, key=repr):
        mismatches.append(f"index {signature[0]}: expected {signature}, actual absent")
    for signature in sorted(actual_indexes - expected_indexes, key=repr):
        mismatches.append(f"index {signature[0]}: expected absent, actual {signature}")

    actual_triggers = {
        str(row[0])
        for row in bind.execute(
            sa.text(
                "SELECT tg.tgname "
                "FROM pg_catalog.pg_trigger AS tg "
                "JOIN pg_catalog.pg_class AS cls ON cls.oid = tg.tgrelid "
                "JOIN pg_catalog.pg_namespace AS ns ON ns.oid = cls.relnamespace "
                "WHERE cls.relname = :table_name "
                "AND ns.nspname = :schema_name "
                "AND NOT tg.tgisinternal"
            ),
            {"table_name": _TABLE, "schema_name": inspector.default_schema_name},
        )
    }
    if actual_triggers:
        mismatches.append(f"object triggers: expected absent, actual {sorted(actual_triggers)}")
    return mismatches


def upgrade() -> None:
    bind = op.get_bind()
    if sa.inspect(bind).has_table(_TABLE):
        mismatches = _schema_mismatches(bind, _expected_table())
        if mismatches:
            raise RuntimeError(f"{_TABLE} preflight rejected: {'; '.join(mismatches)}")
        return

    # Draft rows are intentionally mutable, so this table has no append-only
    # UPDATE/DELETE guard. The version CAS and expiry are application state.
    op.create_table(
        _TABLE,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("subject_id", sa.Uuid(), nullable=False),
        sa.Column("production_run_id", sa.Uuid(), nullable=False),
        sa.Column("occurrence_id", sa.Uuid(), nullable=False),
        sa.Column("base_snapshot_id", sa.Uuid(), nullable=False),
        sa.Column("base_artifact_id", sa.Uuid(), nullable=False),
        sa.Column("base_artifact_version", sa.Integer(), nullable=False),
        sa.Column("base_canonical_sha256", sa.String(length=64), nullable=False),
        sa.Column("actor_id", sa.Uuid(), nullable=False),
        sa.Column("text_blob_ref", sa.Uuid(), nullable=False),
        sa.Column("text_sha256", sa.String(length=64), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("base_artifact_version >= 1", name="ck_editorial_drafts_base_version"),
        sa.CheckConstraint("version >= 1", name="ck_editorial_drafts_version"),
        sa.CheckConstraint(
            "char_length(base_canonical_sha256) = 64 AND base_canonical_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_editorial_drafts_base_sha256",
        ),
        sa.CheckConstraint(
            "char_length(text_sha256) = 64 AND text_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_editorial_drafts_text_sha256",
        ),
        sa.CheckConstraint("expires_at > updated_at", name="ck_editorial_drafts_expiry"),
        sa.ForeignKeyConstraint(
            ["subject_id"],
            ["subjects.id"],
            name="fk_editorial_drafts_subject",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["production_run_id"],
            ["production_runs.id"],
            name="fk_editorial_drafts_run",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["base_snapshot_id"],
            ["editorial_workspace_snapshots.snapshot_id"],
            name="fk_editorial_drafts_snapshot",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["base_artifact_id"],
            ["production_artifacts.id"],
            name="fk_editorial_drafts_artifact",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["text_blob_ref"],
            ["blobs.id"],
            name="fk_editorial_drafts_text_blob",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_editorial_drafts"),
        sa.UniqueConstraint(
            "subject_id",
            "occurrence_id",
            name="uq_editorial_drafts_subject_occurrence",
        ),
    )
    op.create_index("ix_editorial_drafts_expiry", _TABLE, ["expires_at"])


def downgrade() -> None:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table(_TABLE):
        raise RuntimeError(f"{_TABLE}: expected present, actual absent")
    bind.execute(sa.text(f"LOCK TABLE {_TABLE} IN ACCESS EXCLUSIVE MODE"))
    row_count = bind.execute(sa.text(f"SELECT count(*) FROM {_TABLE}")).scalar_one()
    if row_count:
        raise RuntimeError(f"refusing to downgrade {_TABLE} after it has been used")
    op.drop_table(_TABLE)
