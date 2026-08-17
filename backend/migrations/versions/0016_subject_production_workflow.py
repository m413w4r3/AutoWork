"""Subject production workflow tables for brief_auto.

Revision ID: 0016
Revises: 0015
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # subject_production_runs table
    op.create_table(
        "subject_production_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("subject_id", sa.Uuid(), nullable=False),
        sa.Column("edition_id", sa.Uuid(), nullable=False),
        sa.Column("profile", sa.String(32), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("current_stage", sa.String(32), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=True),
        sa.Column("run_number", sa.Integer(), nullable=False),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("error_message", sa.String(500), nullable=True),
        sa.Column("error_details", sa.JSON(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.CheckConstraint("version >= 1"),
        sa.CheckConstraint("run_number >= 1"),
        sa.CheckConstraint(
            "status IN ('queued','running','ready','needs_review','failed','cancelled')"
        ),
        sa.CheckConstraint(
            "current_stage IN ('sources','references','extraction','synthesis','assembly')"
        ),
        sa.CheckConstraint("profile IN ('brief_auto','major_assisted')"),
        sa.ForeignKeyConstraint(["subject_id"], ["subjects.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["edition_id"], ["editions.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["conversation_id"], ["model_conversations.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("subject_id", "run_number", name="uq_subject_run_number"),
    )
    op.create_index(
        "ix_subject_production_runs_subject_id_created_at",
        "subject_production_runs",
        ["subject_id", "created_at"],
    )
    op.create_index(
        "ix_subject_production_runs_edition_id_status",
        "subject_production_runs",
        ["edition_id", "status"],
    )

    # production_artifacts table
    op.create_table(
        "production_artifacts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("production_run_id", sa.Uuid(), nullable=False),
        sa.Column("subject_id", sa.Uuid(), nullable=False),
        sa.Column("stage", sa.String(32), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("input_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="verified"),
        sa.Column("raw_blob_id", sa.Uuid(), nullable=True),
        sa.Column("canonical_blob_id", sa.Uuid(), nullable=True),
        sa.Column("rendered_blob_id", sa.Uuid(), nullable=True),
        sa.Column("model_run_id", sa.Uuid(), nullable=True),
        sa.Column("conversation_turn_id", sa.Uuid(), nullable=True),
        sa.Column("metadata", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint("version >= 1"),
        sa.CheckConstraint("stage IN ('references','extraction','synthesis','brief')"),
        sa.CheckConstraint("status IN ('verified','stale','needs_review')"),
        sa.CheckConstraint("LENGTH(input_hash) = 64"),
        sa.ForeignKeyConstraint(
            ["production_run_id"], ["subject_production_runs.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["subject_id"], ["subjects.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["raw_blob_id"], ["blobs.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["canonical_blob_id"], ["blobs.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["rendered_blob_id"], ["blobs.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["model_run_id"], ["model_runs.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["conversation_turn_id"], ["model_conversation_turns.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "production_run_id", "stage", "version", name="uq_run_stage_version"
        ),
    )
    op.create_index(
        "ix_production_artifacts_run_stage_version",
        "production_artifacts",
        ["production_run_id", "stage", "version"],
    )

    # edition_production_batches table
    op.create_table(
        "edition_production_batches",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("edition_id", sa.Uuid(), nullable=False),
        sa.Column("profile", sa.String(32), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.CheckConstraint(
            "status IN ('queued','running','completed','completed_with_issues','cancelled')"
        ),
        sa.CheckConstraint("profile IN ('brief_auto','major_assisted')"),
        sa.ForeignKeyConstraint(["edition_id"], ["editions.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_edition_production_batches_edition_id_status",
        "edition_production_batches",
        ["edition_id", "status"],
    )

    # edition_production_batch_items table
    op.create_table(
        "edition_production_batch_items",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("batch_id", sa.Uuid(), nullable=False),
        sa.Column("subject_id", sa.Uuid(), nullable=False),
        sa.Column("production_run_id", sa.Uuid(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint("position >= 1"),
        sa.ForeignKeyConstraint(["batch_id"], ["edition_production_batches.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["subject_id"], ["subjects.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["production_run_id"], ["subject_production_runs.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("batch_id", "position", name="uq_batch_position"),
        sa.UniqueConstraint("batch_id", "subject_id", name="uq_batch_subject"),
    )


def downgrade() -> None:
    op.drop_table("edition_production_batch_items")
    op.drop_table("edition_production_batches")
    op.drop_table("production_artifacts")
    op.drop_table("subject_production_runs")
