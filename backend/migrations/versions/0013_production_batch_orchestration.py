"""Freeze production inputs and add batch orchestration state.

Revision ID: 0013_production_batch
Revises: 0012_goodware_index_artifacts
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0013_production_batch"
down_revision = "0012_goodware_index_artifacts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "edition_production_batches",
        sa.Column("phase", sa.String(length=16), nullable=False, server_default="initial"),
    )
    op.add_column(
        "edition_production_batches",
        sa.Column("next_dispatch_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_check_constraint(
        "ck_batch_phase",
        "edition_production_batches",
        "phase IN ('initial', 'recovery', 'review')",
    )

    op.add_column(
        "edition_production_batch_items",
        sa.Column("auto_recovery_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_check_constraint(
        "ck_batch_item_auto_recovery_count",
        "edition_production_batch_items",
        "auto_recovery_count BETWEEN 0 AND 1",
    )

    op.create_table(
        "production_input_snapshots",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("production_run_id", sa.Uuid(), nullable=False),
        sa.Column("subject_id", sa.Uuid(), nullable=False),
        sa.Column("edition_id", sa.Uuid(), nullable=False),
        sa.Column("editorial_group_id", sa.Uuid(), nullable=False),
        sa.Column("editorial_group_version", sa.Integer(), nullable=False),
        sa.Column("subject_title", sa.Text(), nullable=False),
        sa.Column("subject_description", sa.Text(), nullable=False),
        sa.Column("actor_or_campaign", sa.Text(), nullable=False),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column("research_date", sa.Date(), nullable=False),
        sa.Column(
            "core_sources",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("input_hash", sa.String(length=64), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "editorial_group_version >= 1", name="ck_production_input_group_version"
        ),
        sa.CheckConstraint("period_start <= period_end", name="ck_production_input_period_order"),
        sa.CheckConstraint(
            "jsonb_typeof(core_sources) = 'array'", name="ck_production_input_sources_array"
        ),
        sa.CheckConstraint(
            "char_length(input_hash) = 64 AND input_hash ~ '^[0-9a-f]{64}$'",
            name="ck_production_input_hash",
        ),
        sa.ForeignKeyConstraint(
            ["production_run_id"], ["subject_production_runs.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["subject_id"], ["subjects.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["edition_id"], ["editions.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["editorial_group_id"], ["editorial_groups.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("production_run_id", name="uq_production_input_snapshots_run"),
    )
    op.create_index(
        "ix_production_input_snapshots_subject",
        "production_input_snapshots",
        ["subject_id", "captured_at"],
    )
    op.execute(
        "CREATE TRIGGER trg_production_input_snapshots_append_only "
        "BEFORE UPDATE OR DELETE ON production_input_snapshots "
        "FOR EACH ROW EXECUTE FUNCTION reject_evidence_mutation()"
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER trg_production_input_snapshots_append_only ON production_input_snapshots"
    )
    op.drop_index("ix_production_input_snapshots_subject", table_name="production_input_snapshots")
    op.drop_table("production_input_snapshots")

    op.drop_constraint(
        "ck_batch_item_auto_recovery_count",
        "edition_production_batch_items",
        type_="check",
    )
    op.drop_column("edition_production_batch_items", "auto_recovery_count")

    op.drop_constraint("ck_batch_phase", "edition_production_batches", type_="check")
    op.drop_column("edition_production_batches", "next_dispatch_at")
    op.drop_column("edition_production_batches", "phase")
