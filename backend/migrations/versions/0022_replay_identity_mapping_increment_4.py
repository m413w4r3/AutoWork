"""Add replay identity mapping and comparison tables for Increment 4.

This migration adds tables for:
1. ReplayIdentityMapping - mapping replay identities to operational
2. ReplayComparison - summary of replay vs operational differences

Revision ID: 0022
Revises: 0021
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0022"
down_revision: str | None = "0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Create replay_identity_mappings table
    op.create_table(
        "replay_identity_mappings",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("replay_run_id", sa.Uuid(), nullable=False),
        sa.Column("replay_subject_id", sa.Uuid(), nullable=False),
        sa.Column("operational_subject_id", sa.Uuid(), nullable=True),
        sa.Column("resolution", sa.String(length=20), nullable=False),
        sa.Column("actor_id", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["replay_subject_id"],
            ["discovery_subject_identities.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["operational_subject_id"],
            ["discovery_subject_identities.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["replay_run_id"],
            ["discovery_merge_runs.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_replay_identity_mappings_replay_run",
        "replay_identity_mappings",
        ["replay_run_id"],
    )
    op.create_index(
        "ix_replay_identity_mappings_subjects",
        "replay_identity_mappings",
        ["replay_subject_id", "operational_subject_id"],
    )
    op.create_unique_constraint(
        "uq_replay_identity_mappings_replay_subject",
        "replay_identity_mappings",
        ["replay_run_id", "replay_subject_id"],
    )

    # Create replay_comparisons table (for reporting)
    op.create_table(
        "replay_comparisons",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("replay_run_id", sa.Uuid(), nullable=False),
        sa.Column("edition_id", sa.Uuid(), nullable=False),
        sa.Column("subjects_same_count", sa.Integer(), nullable=False),
        sa.Column("subjects_split_count", sa.Integer(), nullable=False),
        sa.Column("subjects_merged_count", sa.Integer(), nullable=False),
        sa.Column("subjects_created_count", sa.Integer(), nullable=False),
        sa.Column("subjects_impacting_editorial", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("subjects_same_count >= 0", name="ck_subjects_same_count"),
        sa.CheckConstraint("subjects_split_count >= 0", name="ck_subjects_split_count"),
        sa.CheckConstraint("subjects_merged_count >= 0", name="ck_subjects_merged_count"),
        sa.CheckConstraint("subjects_created_count >= 0", name="ck_subjects_created_count"),
        sa.CheckConstraint(
            "subjects_impacting_editorial >= 0",
            name="ck_subjects_impacting_editorial",
        ),
        sa.ForeignKeyConstraint(
            ["replay_run_id"],
            ["discovery_merge_runs.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(["edition_id"], ["editions.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_replay_comparisons_replay_run", "replay_comparisons", ["replay_run_id"])
    op.create_index("ix_replay_comparisons_edition", "replay_comparisons", ["edition_id"])


def downgrade() -> None:
    op.drop_table("replay_comparisons")
    op.drop_table("replay_identity_mappings")
