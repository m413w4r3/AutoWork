"""Canonical VirusTotal observations.

Revision ID: 0002_virustotal_observations
Revises: 0001_baseline
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002_virustotal_observations"
down_revision: str | None = "0001_baseline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "virustotal_observations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("subject_id", sa.Uuid(), nullable=True),
        sa.Column("operation", sa.String(length=64), nullable=False),
        sa.Column("capability", sa.String(length=64), nullable=False),
        sa.Column("source_identifier", sa.Text(), nullable=False),
        sa.Column("safe_parameters", postgresql.JSONB(), nullable=False),
        sa.Column("http_status", sa.Integer(), nullable=False),
        sa.Column("blob_id", sa.Uuid(), nullable=False),
        sa.Column("raw_sha256", sa.String(length=64), nullable=False),
        sa.Column("raw_size", sa.BigInteger(), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("input_cursor", sa.Text(), nullable=True),
        sa.Column("output_cursor", sa.Text(), nullable=True),
        sa.Column("observed_count", sa.Integer(), nullable=False),
        sa.Column("exhaustive", sa.Boolean(), nullable=False),
        sa.Column("page_order", sa.Integer(), nullable=False),
        sa.Column("normalization_contract_version", sa.String(length=64), nullable=False),
        sa.CheckConstraint(
            "http_status >= 200 AND http_status < 300",
            name="ck_vt_observation_http_success",
        ),
        sa.CheckConstraint("raw_size >= 0", name="ck_vt_observation_raw_size"),
        sa.CheckConstraint("observed_count >= 0", name="ck_vt_observation_count"),
        sa.CheckConstraint("page_order >= 0", name="ck_vt_observation_page_order"),
        sa.ForeignKeyConstraint(["subject_id"], ["subjects.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["blob_id"], ["blobs.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_vt_observations_blob_id", "virustotal_observations", ["blob_id"])
    op.create_index("ix_vt_observations_subject_id", "virustotal_observations", ["subject_id"])
    op.create_table(
        "virustotal_file_views",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("observation_id", sa.Uuid(), nullable=False),
        sa.Column("vt_file_id", sa.String(length=128), nullable=False),
        sa.Column("file_type", sa.String(length=64), nullable=False),
        sa.Column("lookup_hash", sa.String(length=64), nullable=False),
        sa.Column("meaningful_name", sa.Text(), nullable=True),
        sa.Column("type_description", sa.Text(), nullable=True),
        sa.Column("size", sa.BigInteger(), nullable=True),
        sa.Column("last_analysis_stats", postgresql.JSONB(), nullable=True),
        sa.Column("first_submission_date", sa.BigInteger(), nullable=True),
        sa.Column("last_submission_date", sa.BigInteger(), nullable=True),
        sa.Column("last_modification_date", sa.BigInteger(), nullable=True),
        sa.Column("tags", postgresql.JSONB(), nullable=False),
        sa.ForeignKeyConstraint(
            ["observation_id"], ["virustotal_observations.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("observation_id", name="uq_vt_file_views_observation"),
    )
    op.execute(
        "CREATE TRIGGER trg_vt_observations_append_only BEFORE UPDATE OR DELETE "
        "ON virustotal_observations FOR EACH ROW EXECUTE FUNCTION reject_evidence_mutation()"
    )
    op.execute(
        "CREATE TRIGGER trg_vt_file_views_append_only BEFORE UPDATE OR DELETE "
        "ON virustotal_file_views FOR EACH ROW EXECUTE FUNCTION reject_evidence_mutation()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER trg_vt_file_views_append_only ON virustotal_file_views")
    op.execute("DROP TRIGGER trg_vt_observations_append_only ON virustotal_observations")
    op.drop_table("virustotal_file_views")
    op.drop_index("ix_vt_observations_subject_id", table_name="virustotal_observations")
    op.drop_index("ix_vt_observations_blob_id", table_name="virustotal_observations")
    op.drop_table("virustotal_observations")
