"""Persist canonical cross-run production reuse state."""

import hashlib
import json

import sqlalchemy as sa
from alembic import op


revision = "0019_production_reuse"
down_revision = "0018_subject_active_run"
branch_labels = None
depends_on = None


def _reuse_basis_hash(row: sa.RowMapping) -> str:
    """Backfill the Python canonical payload without importing application code."""
    sources = sorted(
        row["core_sources"],
        key=lambda source: (
            source["canonical_url"],
            source["batch_id"],
            source["candidate_id"],
            source["source_candidate_id"],
        ),
    )
    payload = {
        "subject_id": str(row["subject_id"]),
        "edition_id": str(row["edition_id"]),
        "editorial_group_id": str(row["editorial_group_id"]),
        "editorial_group_version": row["editorial_group_version"],
        "subject_title": row["subject_title"],
        "subject_description": row["subject_description"],
        "actor_or_campaign": row["actor_or_campaign"],
        "period_start": row["period_start"].isoformat(),
        "period_end": row["period_end"].isoformat(),
        "core_sources": sources,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def upgrade() -> None:
    op.add_column(
        "subject_production_runs",
        sa.Column("force_recompute_from_stage", sa.String(length=32), nullable=True),
    )
    op.create_check_constraint(
        "ck_run_force_recompute_stage",
        "subject_production_runs",
        "force_recompute_from_stage IS NULL OR force_recompute_from_stage IN "
        "('references', 'extraction', 'synthesis')",
    )

    op.add_column(
        "production_input_snapshots",
        sa.Column("reuse_basis_hash", sa.String(length=64), nullable=True),
    )
    bind = op.get_bind()
    snapshots = bind.execute(
        sa.text(
            "SELECT id, subject_id, edition_id, editorial_group_id, "
            "editorial_group_version, subject_title, subject_description, "
            "actor_or_campaign, period_start, period_end, core_sources "
            "FROM production_input_snapshots"
        )
    ).mappings()
    for row in snapshots:
        bind.execute(
            sa.text(
                "UPDATE production_input_snapshots "
                "SET reuse_basis_hash = :reuse_basis_hash WHERE id = :id"
            ),
            {"id": row["id"], "reuse_basis_hash": _reuse_basis_hash(row)},
        )
    op.alter_column("production_input_snapshots", "reuse_basis_hash", nullable=False)
    op.create_check_constraint(
        "ck_production_input_reuse_basis_hash",
        "production_input_snapshots",
        "char_length(reuse_basis_hash) = 64 AND reuse_basis_hash ~ '^[0-9a-f]{64}$'",
    )

    op.add_column(
        "production_artifacts",
        sa.Column("reused_from_artifact_id", sa.Uuid(), nullable=True),
    )
    op.create_check_constraint(
        "ck_artifact_reuse_not_self",
        "production_artifacts",
        "reused_from_artifact_id IS NULL OR reused_from_artifact_id <> id",
    )
    op.create_foreign_key(
        "fk_production_artifacts_reused_from",
        "production_artifacts",
        "production_artifacts",
        ["reused_from_artifact_id"],
        ["id"],
        ondelete="RESTRICT",
    )

    op.create_table(
        "production_reuse_invalidations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("edition_id", sa.Uuid(), nullable=False),
        sa.Column("subject_id", sa.Uuid(), nullable=False),
        sa.Column("from_stage", sa.String(length=32), nullable=False),
        sa.Column("actor_id", sa.String(length=255), nullable=False),
        sa.Column("correlation_id", sa.String(length=128), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "from_stage IN ('references', 'extraction', 'synthesis')",
            name="ck_production_reuse_invalidation_stage",
        ),
        sa.ForeignKeyConstraint(["edition_id"], ["editions.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["subject_id"], ["subjects.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_production_reuse_invalidations_subject",
        "production_reuse_invalidations",
        ["edition_id", "subject_id", "occurred_at"],
    )
    op.execute(
        "CREATE TRIGGER trg_production_reuse_invalidations_append_only "
        "BEFORE UPDATE OR DELETE ON production_reuse_invalidations "
        "FOR EACH ROW EXECUTE FUNCTION reject_evidence_mutation()"
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER trg_production_reuse_invalidations_append_only "
        "ON production_reuse_invalidations"
    )
    op.drop_index(
        "ix_production_reuse_invalidations_subject",
        table_name="production_reuse_invalidations",
    )
    op.drop_table("production_reuse_invalidations")

    op.drop_constraint(
        "fk_production_artifacts_reused_from",
        "production_artifacts",
        type_="foreignkey",
    )
    op.drop_constraint("ck_artifact_reuse_not_self", "production_artifacts", type_="check")
    op.drop_column("production_artifacts", "reused_from_artifact_id")

    op.drop_constraint(
        "ck_production_input_reuse_basis_hash",
        "production_input_snapshots",
        type_="check",
    )
    op.drop_column("production_input_snapshots", "reuse_basis_hash")

    op.drop_constraint("ck_run_force_recompute_stage", "subject_production_runs", type_="check")
    op.drop_column("subject_production_runs", "force_recompute_from_stage")
