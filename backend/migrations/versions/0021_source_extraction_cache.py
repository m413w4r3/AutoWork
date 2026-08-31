"""Add the subject-independent source extraction checkpoint catalog."""

import sqlalchemy as sa
from alembic import op


revision = "0021_source_extraction_cache"
down_revision = "0020_model_run_submission"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "source_extractions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("canonical_url", sa.Text(), nullable=False),
        sa.Column("source_content_sha256", sa.String(length=64), nullable=False),
        sa.Column("profile", sa.String(length=32), nullable=False),
        sa.Column("contract_version", sa.String(length=64), nullable=False),
        sa.Column("prompt_version", sa.String(length=64), nullable=False),
        sa.Column("parser_version", sa.String(length=64), nullable=False),
        sa.Column("verifier_version", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column(
            "canonical_blob_id",
            sa.Uuid(),
            sa.ForeignKey("blobs.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column(
            "raw_blob_id",
            sa.Uuid(),
            sa.ForeignKey("blobs.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("model_run_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "char_length(source_content_sha256) = 64 AND source_content_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_source_extractions_content_sha256",
        ),
        sa.CheckConstraint(
            "profile IN ('full', 'ioc_rules')",
            name="ck_source_extractions_profile",
        ),
        sa.CheckConstraint(
            "status IN ('running', 'verified', 'needs_review', 'failed')",
            name="ck_source_extractions_status",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "source_content_sha256",
            "profile",
            "contract_version",
            "prompt_version",
            "parser_version",
            "verifier_version",
            name="uq_source_extractions_identity",
        ),
    )
    op.create_index(
        "ix_source_extractions_content_profile",
        "source_extractions",
        ["source_content_sha256", "profile", "status"],
    )


def downgrade() -> None:
    op.drop_index("ix_source_extractions_content_profile", table_name="source_extractions")
    op.drop_table("source_extractions")
