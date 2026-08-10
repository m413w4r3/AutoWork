"""Persist editorial groups and append-only human decisions.

Revision ID: 0006
Revises: 0005
Create Date: 2026-08-10
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "editorial_groups",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("edition_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(length=1000), nullable=False),
        sa.Column("outcome", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("source_relationship_status", sa.String(length=32), nullable=False),
        sa.Column("needs_source_verification", sa.Boolean(), nullable=False),
        sa.Column("needs_source_expansion", sa.Boolean(), nullable=False),
        sa.Column("grouping_confidence", sa.String(length=32), nullable=False),
        sa.Column("grouping_justification", sa.Text(), nullable=False),
        sa.Column("potential_historical_group_id", sa.Uuid(), nullable=True),
        sa.Column("editorial_type", sa.String(length=32), nullable=True),
        sa.Column("subject_id", sa.Uuid(), nullable=True),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('proposed', 'rejected', 'selected', 'superseded')",
            name="ck_editorial_groups_status",
        ),
        sa.CheckConstraint(
            "outcome IN ('new_subject', 'duplicate_same_publication', "
            "'update_previous_subject', 'non_independent_reprint', 'ambiguous_review')",
            name="ck_editorial_groups_outcome",
        ),
        sa.CheckConstraint(
            "editorial_type IS NULL OR editorial_type IN ('brief', 'major')",
            name="ck_editorial_groups_type",
        ),
        sa.CheckConstraint(
            "source_relationship_status IN ('provisional', 'verified')",
            name="ck_editorial_groups_relationship",
        ),
        sa.CheckConstraint(
            "grouping_confidence IN ('low', 'medium', 'high')",
            name="ck_editorial_groups_confidence",
        ),
        sa.CheckConstraint("version > 0", name="ck_editorial_groups_version"),
        sa.CheckConstraint("jsonb_typeof(payload) = 'object'", name="ck_editorial_payload_object"),
        sa.ForeignKeyConstraint(["edition_id"], ["editions.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["potential_historical_group_id"],
            ["editorial_groups.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(["subject_id"], ["subjects.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_editorial_groups_edition", "editorial_groups", ["edition_id", "status", "created_at"]
    )
    op.create_table(
        "human_decisions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("edition_id", sa.Uuid(), nullable=False),
        sa.Column("decision_type", sa.String(length=32), nullable=False),
        sa.Column("group_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("actor_id", sa.String(length=255), nullable=False),
        sa.Column("correlation_id", sa.String(length=128), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "decision_type IN ('merge', 'split', 'reject', 'select')",
            name="ck_human_decisions_type",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(group_ids) = 'array'", name="ck_human_decisions_groups_array"
        ),
        sa.CheckConstraint(
            "jsonb_typeof(payload) = 'object'", name="ck_human_decisions_payload_object"
        ),
        sa.ForeignKeyConstraint(["edition_id"], ["editions.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_human_decisions_edition", "human_decisions", ["edition_id", "occurred_at"])
    op.execute(
        """
        CREATE FUNCTION reject_human_decision_mutation() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'human_decisions is append-only' USING ERRCODE = '55000';
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_human_decisions_append_only
        BEFORE UPDATE OR DELETE ON human_decisions
        FOR EACH ROW EXECUTE FUNCTION reject_human_decision_mutation()
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_human_decisions_append_only ON human_decisions")
    op.execute("DROP FUNCTION IF EXISTS reject_human_decision_mutation()")
    op.drop_index("ix_human_decisions_edition", table_name="human_decisions")
    op.drop_table("human_decisions")
    op.drop_index("ix_editorial_groups_edition", table_name="editorial_groups")
    op.drop_table("editorial_groups")
