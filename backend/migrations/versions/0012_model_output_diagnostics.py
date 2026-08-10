"""Persist safe model output diagnostics and append-only rejections.

Revision ID: 0012
Revises: 0011
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "jobs",
        sa.Column("error_details", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    for name, column in (
        ("raw_output_reference", sa.Text()),
        ("raw_output_sha256", sa.String(64)),
        ("raw_output_chars", sa.BigInteger()),
        ("normalized_output_reference", sa.Text()),
        ("normalized_output_sha256", sa.String(64)),
        ("parser_stage", sa.String(64)),
        ("serializer_version", sa.String(64)),
        ("normalization_version", sa.String(64)),
        ("json_error_line", sa.BigInteger()),
        ("json_error_column", sa.BigInteger()),
    ):
        op.add_column("model_runs", sa.Column(name, column, nullable=True))
    op.add_column(
        "model_runs",
        sa.Column(
            "validation_errors",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.add_column(
        "model_runs",
        sa.Column(
            "transformations",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.add_column(
        "model_runs",
        sa.Column("citation_count", sa.BigInteger(), nullable=False, server_default="0"),
    )
    op.add_column(
        "model_runs",
        sa.Column("extracted_url_count", sa.BigInteger(), nullable=False, server_default="0"),
    )
    op.add_column(
        "model_runs",
        sa.Column(
            "visible_citations",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.create_check_constraint(
        "ck_model_runs_output_diagnostic_counts",
        "model_runs",
        "(raw_output_chars IS NULL OR raw_output_chars >= 0) "
        "AND citation_count >= 0 AND extracted_url_count >= 0",
    )
    op.create_table(
        "model_output_rejections",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("model_run_id", sa.Uuid(), nullable=False),
        sa.Column("path", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("error_type", sa.String(128), nullable=False),
        sa.Column("value_sha256", sa.String(64), nullable=False),
        sa.Column("raw_output_reference", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "value_sha256 ~ '^[0-9a-f]{64}$'", name="ck_model_output_rejections_hash"
        ),
        sa.ForeignKeyConstraint(["model_run_id"], ["model_runs.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_model_output_rejections_run",
        "model_output_rejections",
        ["model_run_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_model_output_rejections_run", table_name="model_output_rejections")
    op.drop_table("model_output_rejections")
    op.drop_constraint("ck_model_runs_output_diagnostic_counts", "model_runs", type_="check")
    for name in (
        "visible_citations",
        "extracted_url_count",
        "citation_count",
        "transformations",
        "validation_errors",
        "json_error_column",
        "json_error_line",
        "normalization_version",
        "serializer_version",
        "parser_stage",
        "normalized_output_sha256",
        "normalized_output_reference",
        "raw_output_chars",
        "raw_output_sha256",
        "raw_output_reference",
    ):
        op.drop_column("model_runs", name)
    op.drop_column("jobs", "error_details")
