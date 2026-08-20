"""Add conversation_lifecycles table for Increment 4.

This migration adds the conversation_lifecycles table to support the new
conversation lifecycle management system with DELETE_ON_SUCCESS policy.

Revision ID: 0023
Revises: 0022
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0023"
down_revision: str | None = "0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Create conversation_lifecycles table
    op.create_table(
        "conversation_lifecycles",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("policy", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("release_outcome", sa.String(length=32), nullable=True),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cleanup_attempt_count", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("last_cleanup_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_cleanup_error_code", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("version", sa.BigInteger(), nullable=False, server_default="1"),
        sa.CheckConstraint(
            "policy IN ('keep', 'delete_on_success')",
            name="ck_conv_lifecycle_policy",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'released', 'delete_pending', 'deleting', 'deleted', 'cleanup_failed', 'retained')",
            name="ck_conv_lifecycle_status",
        ),
        sa.CheckConstraint(
            "release_outcome IS NULL OR release_outcome IN ('success', 'failure', 'needs_review', 'cancelled')",
            name="ck_conv_lifecycle_outcome",
        ),
        sa.CheckConstraint(
            "cleanup_attempt_count >= 0",
            name="ck_conv_lifecycle_attempts",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("conversation_id", name="uq_conv_lifecycle_conversation_id"),
    )
    op.create_index(
        "ix_conversation_lifecycles_conversation_id",
        "conversation_lifecycles",
        ["conversation_id"],
    )
    op.create_index(
        "ix_conversation_lifecycles_status",
        "conversation_lifecycles",
        ["status"],
    )
    op.create_index(
        "ix_conversation_lifecycles_created_at",
        "conversation_lifecycles",
        ["created_at"],
    )


def downgrade() -> None:
    op.drop_table("conversation_lifecycles")
