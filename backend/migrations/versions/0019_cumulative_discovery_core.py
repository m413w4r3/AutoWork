"""Add the immutable cumulative discovery identity and snapshot core.

Revision ID: 0019
Revises: 0018
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "discovery_intakes",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("edition_id", sa.Uuid(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("input_mode", sa.String(length=32), nullable=False),
        sa.Column("raw_report_hash", sa.String(length=64), nullable=False),
        sa.Column("parsed_report_hash", sa.String(length=64), nullable=False),
        sa.Column("intake_hash", sa.String(length=64), nullable=False),
        sa.Column("research_model_run_id", sa.Uuid(), nullable=False),
        sa.Column("source_mode", sa.String(length=32), nullable=False),
        sa.Column("complementary_axis", sa.String(length=500), nullable=False),
        sa.Column("batch_id", sa.Uuid(), nullable=False),
        sa.Column("created_by", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("sequence > 0", name="ck_discovery_intakes_sequence"),
        sa.ForeignKeyConstraint(["edition_id"], ["editions.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["research_model_run_id"], ["model_runs.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["batch_id"], ["discovery_batches.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("edition_id", "sequence", name="uq_discovery_intakes_sequence"),
        sa.UniqueConstraint("edition_id", "intake_hash", name="uq_discovery_intakes_hash"),
        sa.UniqueConstraint("batch_id", name="uq_discovery_intakes_batch"),
    )
    op.create_index("ix_discovery_intakes_edition", "discovery_intakes", ["edition_id", "sequence"])

    op.create_table(
        "discovery_merge_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("edition_id", sa.Uuid(), nullable=False),
        sa.Column("parent_snapshot_id", sa.Uuid(), nullable=True),
        sa.Column("intake_id", sa.Uuid(), nullable=False),
        sa.Column("planner_kind", sa.String(length=32), nullable=False),
        sa.Column("merge_model_run_id", sa.Uuid(), nullable=True),
        sa.Column("prompt_version", sa.String(length=64), nullable=False),
        sa.Column("policy_version", sa.String(length=64), nullable=False),
        sa.Column("blocking_version", sa.String(length=64), nullable=False),
        sa.Column("merge_input_hash", sa.String(length=64), nullable=False),
        sa.Column("handle_map", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("included_subject_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("excluded_subject_count", sa.Integer(), nullable=False),
        sa.Column("raw_output_reference", sa.Text(), nullable=True),
        sa.Column("normalized_output_reference", sa.Text(), nullable=True),
        sa.Column("validation_status", sa.String(length=32), nullable=False),
        sa.Column("warnings", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("rebase_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("rebase_count BETWEEN 0 AND 2", name="ck_discovery_merge_runs_rebase"),
        sa.ForeignKeyConstraint(["edition_id"], ["editions.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["intake_id"], ["discovery_intakes.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["merge_model_run_id"], ["model_runs.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("merge_input_hash", name="uq_discovery_merge_runs_input_hash"),
    )
    op.create_index(
        "ix_discovery_merge_runs_edition", "discovery_merge_runs", ["edition_id", "created_at"]
    )

    op.create_table(
        "discovery_subject_identities",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("edition_id", sa.Uuid(), nullable=False),
        sa.Column("origin_key", sa.Text(), nullable=False),
        sa.Column("cross_edition_lineage_id", sa.Uuid(), nullable=True),
        sa.Column("created_by_merge_run_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("merged_into_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("status IN ('active', 'merged')", name="ck_discovery_subject_status"),
        sa.CheckConstraint(
            "(status = 'active' AND merged_into_id IS NULL) OR "
            "(status = 'merged' AND merged_into_id IS NOT NULL)",
            name="ck_discovery_subject_merge_projection",
        ),
        sa.ForeignKeyConstraint(["edition_id"], ["editions.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["created_by_merge_run_id"], ["discovery_merge_runs.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["merged_into_id"], ["discovery_subject_identities.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("edition_id", "origin_key", name="uq_discovery_subject_origin"),
    )
    op.create_index(
        "ix_discovery_subject_identities_edition",
        "discovery_subject_identities",
        ["edition_id", "status"],
    )

    op.create_table(
        "discovery_snapshots",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("edition_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("parent_snapshot_id", sa.Uuid(), nullable=True),
        sa.Column("intake_id", sa.Uuid(), nullable=False),
        sa.Column("merge_run_id", sa.Uuid(), nullable=False),
        sa.Column("planner_kind", sa.String(length=32), nullable=False),
        sa.Column("lineage", sa.String(length=16), nullable=False),
        sa.Column("replay_run_id", sa.Uuid(), nullable=True),
        sa.Column("subjects", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("snapshot_hash", sa.String(length=64), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("version > 0", name="ck_discovery_snapshots_version"),
        sa.ForeignKeyConstraint(["edition_id"], ["editions.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["parent_snapshot_id"], ["discovery_snapshots.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["intake_id"], ["discovery_intakes.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["merge_run_id"], ["discovery_merge_runs.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "edition_id", "lineage", "version", name="uq_discovery_snapshots_version"
        ),
        sa.UniqueConstraint("intake_id", "lineage", name="uq_discovery_snapshots_intake"),
        sa.UniqueConstraint("merge_run_id", name="uq_discovery_snapshots_merge_run"),
    )
    op.create_index(
        "ix_discovery_snapshots_edition",
        "discovery_snapshots",
        ["edition_id", "lineage", "version"],
    )
    op.create_index(
        "uq_discovery_snapshots_active_operational",
        "discovery_snapshots",
        ["edition_id"],
        unique=True,
        postgresql_where=sa.text("is_active AND lineage = 'operational'"),
    )
    op.create_foreign_key(
        "fk_discovery_merge_runs_parent_snapshot",
        "discovery_merge_runs",
        "discovery_snapshots",
        ["parent_snapshot_id"],
        ["id"],
        ondelete="RESTRICT",
    )

    op.create_table(
        "subject_merge_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("edition_id", sa.Uuid(), nullable=False),
        sa.Column("from_subject_id", sa.Uuid(), nullable=False),
        sa.Column("into_subject_id", sa.Uuid(), nullable=False),
        sa.Column("merge_run_id", sa.Uuid(), nullable=False),
        sa.Column("actor_id", sa.String(length=255), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "from_subject_id <> into_subject_id", name="ck_subject_merge_events_distinct"
        ),
        sa.ForeignKeyConstraint(["edition_id"], ["editions.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["from_subject_id"], ["discovery_subject_identities.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["into_subject_id"], ["discovery_subject_identities.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["merge_run_id"], ["discovery_merge_runs.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_subject_merge_events_edition", "subject_merge_events", ["edition_id", "created_at"]
    )

    op.create_table(
        "subject_contributions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("subject_id", sa.Uuid(), nullable=False),
        sa.Column("intake_id", sa.Uuid(), nullable=False),
        sa.Column("candidate_key", sa.Uuid(), nullable=False),
        sa.Column("candidate_id", sa.Uuid(), nullable=False),
        sa.Column("first_seen_snapshot_id", sa.Uuid(), nullable=False),
        sa.Column("first_seen_version", sa.Integer(), nullable=False),
        sa.Column("contributed_title", sa.String(length=1000), nullable=False),
        sa.Column("contributed_summary", sa.Text(), nullable=False),
        sa.Column(
            "contributed_source_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column(
            "contributed_provisional_ioc_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("merge_run_id", sa.Uuid(), nullable=False),
        sa.Column("merge_group_index", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("first_seen_version > 0", name="ck_subject_contributions_version"),
        sa.ForeignKeyConstraint(
            ["subject_id"], ["discovery_subject_identities.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["intake_id"], ["discovery_intakes.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["first_seen_snapshot_id"], ["discovery_snapshots.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["merge_run_id"], ["discovery_merge_runs.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "intake_id", "candidate_key", name="uq_subject_contributions_candidate"
        ),
    )
    op.create_index(
        "ix_subject_contributions_subject", "subject_contributions", ["subject_id", "created_at"]
    )

    op.add_column("editorial_groups", sa.Column("discovery_subject_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_editorial_groups_discovery_subject",
        "editorial_groups",
        "discovery_subject_identities",
        ["discovery_subject_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "ix_editorial_groups_discovery_subject", "editorial_groups", ["discovery_subject_id"]
    )

    for table in (
        "discovery_intakes",
        "subject_merge_events",
        "subject_contributions",
    ):
        function = f"reject_{table}_mutation"
        op.execute(
            f"""
            CREATE FUNCTION {function}() RETURNS trigger AS $$
            BEGIN
                RAISE EXCEPTION '{table} is append-only' USING ERRCODE = '55000';
            END;
            $$ LANGUAGE plpgsql
            """
        )
        op.execute(
            f"CREATE TRIGGER trg_{table}_append_only BEFORE UPDATE OR DELETE ON {table} "
            f"FOR EACH ROW EXECUTE FUNCTION {function}()"
        )


def downgrade() -> None:
    for table in (
        "subject_contributions",
        "subject_merge_events",
        "discovery_intakes",
    ):
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_append_only ON {table}")
        op.execute(f"DROP FUNCTION IF EXISTS reject_{table}_mutation()")
    op.drop_index("ix_editorial_groups_discovery_subject", table_name="editorial_groups")
    op.drop_constraint(
        "fk_editorial_groups_discovery_subject", "editorial_groups", type_="foreignkey"
    )
    op.drop_column("editorial_groups", "discovery_subject_id")
    op.drop_index("ix_subject_contributions_subject", table_name="subject_contributions")
    op.drop_table("subject_contributions")
    op.drop_index("ix_subject_merge_events_edition", table_name="subject_merge_events")
    op.drop_table("subject_merge_events")
    op.drop_constraint(
        "fk_discovery_merge_runs_parent_snapshot", "discovery_merge_runs", type_="foreignkey"
    )
    op.drop_index("uq_discovery_snapshots_active_operational", table_name="discovery_snapshots")
    op.drop_index("ix_discovery_snapshots_edition", table_name="discovery_snapshots")
    op.drop_table("discovery_snapshots")
    op.drop_index(
        "ix_discovery_subject_identities_edition", table_name="discovery_subject_identities"
    )
    op.drop_table("discovery_subject_identities")
    op.drop_index("ix_discovery_merge_runs_edition", table_name="discovery_merge_runs")
    op.drop_table("discovery_merge_runs")
    op.drop_index("ix_discovery_intakes_edition", table_name="discovery_intakes")
    op.drop_table("discovery_intakes")
