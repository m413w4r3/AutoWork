"""Persist observable model executions without prompt contents.

Revision ID: 0004
Revises: 0003
Create Date: 2026-08-08
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "model_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("model_role", sa.String(length=32), nullable=False),
        sa.Column("requested_model", sa.String(length=128), nullable=False),
        sa.Column("actual_model_version", sa.String(length=255), nullable=True),
        sa.Column("prompt_template_id", sa.String(length=128), nullable=False),
        sa.Column("prompt_template_version", sa.String(length=64), nullable=False),
        sa.Column("authorized_input_hash", sa.String(length=64), nullable=False),
        sa.Column("evidence_pack_hash", sa.String(length=64), nullable=False),
        sa.Column("parameters", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("duration_ms", sa.BigInteger(), nullable=True),
        sa.Column("usage", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("response_id", sa.String(length=255), nullable=True),
        sa.Column("output_references", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("provider IN ('openai', 'qwen', 'fake')", name="ck_model_runs_provider"),
        sa.CheckConstraint(
            "model_role IN ('research', 'structured_extraction', 'drafting', 'critic')",
            name="ck_model_runs_role",
        ),
        sa.CheckConstraint(
            "status IN ('running', 'waiting_background', 'succeeded', 'failed', 'blocked')",
            name="ck_model_runs_status",
        ),
        sa.CheckConstraint(
            "duration_ms IS NULL OR duration_ms >= 0", name="ck_model_runs_duration"
        ),
        sa.CheckConstraint(
            "char_length(authorized_input_hash) = 64",
            name="ck_model_runs_input_hash_length",
        ),
        sa.CheckConstraint(
            "char_length(evidence_pack_hash) = 64",
            name="ck_model_runs_evidence_hash_length",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(parameters) = 'object'", name="ck_model_runs_parameters_object"
        ),
        sa.CheckConstraint(
            "jsonb_typeof(output_references) = 'array'",
            name="ck_model_runs_output_references_array",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("response_id", name="uq_model_runs_response_id"),
    )
    op.create_index("ix_model_runs_status", "model_runs", ["status", "updated_at"])
    op.create_index("ix_model_runs_evidence", "model_runs", ["evidence_pack_hash", "started_at"])


def downgrade() -> None:
    op.drop_index("ix_model_runs_evidence", table_name="model_runs")
    op.drop_index("ix_model_runs_status", table_name="model_runs")
    op.drop_table("model_runs")
