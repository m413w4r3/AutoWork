"""Add transport-independent persistent model conversations and immutable turns.

Revision ID: 0011
Revises: 0010
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "model_conversations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("transport", sa.String(32), nullable=False),
        sa.Column("purpose", sa.String(32), nullable=False),
        sa.Column("edition_id", sa.Uuid(), nullable=True),
        sa.Column("subject_id", sa.Uuid(), nullable=True),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("external_id", sa.String(255), nullable=True),
        sa.Column("external_locator", sa.Text(), nullable=True),
        sa.Column("expected_profile", sa.String(255), nullable=True),
        sa.Column("requested_model", sa.String(255), nullable=True),
        sa.Column("head_turn_id", sa.Uuid(), nullable=True),
        sa.Column("turn_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "provider IN ('openai','qwen','fake')", name="ck_model_conversations_provider"
        ),
        sa.CheckConstraint(
            "transport IN ('chatgpt_bridge','openai_responses','application_managed')",
            name="ck_model_conversations_transport",
        ),
        sa.CheckConstraint(
            "purpose IN ('discovery','analyst_assistance','pivot_research','drafting','critic')",
            name="ck_model_conversations_purpose",
        ),
        sa.CheckConstraint(
            "status IN ('pending','ready','busy','needs_review','unavailable','archived')",
            name="ck_model_conversations_status",
        ),
        sa.CheckConstraint(
            "turn_count >= 0 AND version >= 1", name="ck_model_conversations_counters"
        ),
        sa.ForeignKeyConstraint(["edition_id"], ["editions.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["subject_id"], ["subjects.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_model_conversations_subject", "model_conversations", ["subject_id", "updated_at"]
    )
    op.create_index(
        "ix_model_conversations_edition", "model_conversations", ["edition_id", "updated_at"]
    )
    op.create_table(
        "model_conversation_turns",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("parent_turn_id", sa.Uuid(), nullable=True),
        sa.Column("model_run_id", sa.Uuid(), nullable=False),
        sa.Column("input_blob_reference", sa.Text(), nullable=False),
        sa.Column("input_sha256", sa.String(64), nullable=False),
        sa.Column("output_blob_reference", sa.Text(), nullable=True),
        sa.Column("output_sha256", sa.String(64), nullable=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("external_turn_id", sa.String(255), nullable=True),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column("correlation_id", sa.String(128), nullable=False),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("error_details", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("sequence >= 1", name="ck_model_conversation_turns_sequence"),
        sa.CheckConstraint(
            "status IN ('running','succeeded','failed','needs_review','blocked')",
            name="ck_model_conversation_turns_status",
        ),
        sa.CheckConstraint(
            "input_sha256 ~ '^[0-9a-f]{64}$' "
            "AND (output_sha256 IS NULL OR output_sha256 ~ '^[0-9a-f]{64}$')",
            name="ck_model_conversation_turns_hashes",
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"], ["model_conversations.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["parent_turn_id"], ["model_conversation_turns.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["model_run_id"], ["model_runs.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "conversation_id", "sequence", name="uq_model_conversation_turn_sequence"
        ),
        sa.UniqueConstraint("model_run_id", name="uq_model_conversation_turn_model_run"),
        sa.UniqueConstraint("idempotency_key", name="uq_model_conversation_turn_idempotency"),
    )
    op.create_index(
        "ix_model_conversation_turns_conversation",
        "model_conversation_turns",
        ["conversation_id", "sequence"],
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_model_conversation_turn_running "
        "ON model_conversation_turns (conversation_id) WHERE status = 'running'"
    )
    op.create_foreign_key(
        "fk_model_conversations_head_turn",
        "model_conversations",
        "model_conversation_turns",
        ["head_turn_id"],
        ["id"],
        ondelete="RESTRICT",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_model_conversations_head_turn", "model_conversations", type_="foreignkey"
    )
    op.drop_index("uq_model_conversation_turn_running", table_name="model_conversation_turns")
    op.drop_index("ix_model_conversation_turns_conversation", table_name="model_conversation_turns")
    op.drop_table("model_conversation_turns")
    op.drop_index("ix_model_conversations_edition", table_name="model_conversations")
    op.drop_index("ix_model_conversations_subject", table_name="model_conversations")
    op.drop_table("model_conversations")
