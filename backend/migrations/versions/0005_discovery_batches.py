"""Persist sourced discovery candidates.

Revision ID: 0005
Revises: 0004
Create Date: 2026-08-10
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "discovery_batches",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("edition_id", sa.Uuid(), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("complementary_axis", sa.String(length=500), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("discovery_model_run_id", sa.Uuid(), nullable=False),
        sa.Column("structuring_model_run_id", sa.Uuid(), nullable=False),
        sa.Column("tlp", sa.String(length=16), nullable=False),
        sa.Column("sensitivity", sa.String(length=64), nullable=False),
        sa.Column("external_llm_allowed", sa.Boolean(), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "tlp IN ('CLEAR', 'GREEN', 'AMBER', 'AMBER+STRICT', 'RED')",
            name="ck_discovery_batches_tlp",
        ),
        sa.CheckConstraint(
            "char_length(request_hash) = 64 AND request_hash ~ '^[0-9a-f]{64}$'",
            name="ck_discovery_batches_request_hash",
        ),
        sa.CheckConstraint("status = 'completed'", name="ck_discovery_batches_status"),
        sa.CheckConstraint("jsonb_typeof(payload) = 'object'", name="ck_discovery_payload_object"),
        sa.ForeignKeyConstraint(["edition_id"], ["editions.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["discovery_model_run_id"], ["model_runs.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["structuring_model_run_id"], ["model_runs.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("edition_id", "request_hash", name="uq_discovery_batches_request"),
    )
    op.create_index(
        "ix_discovery_batches_edition", "discovery_batches", ["edition_id", "created_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_discovery_batches_edition", table_name="discovery_batches")
    op.drop_table("discovery_batches")
