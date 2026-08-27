"""versioned goodware baselines

Revision ID: 0007_goodware_baselines
Revises: 0006_static_analysis
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0007_goodware_baselines"
down_revision = "0006_static_analysis"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "goodware_baselines",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("source_set_sha256", sa.String(64), nullable=False),
        sa.Column("records_sha256", sa.String(64), nullable=False),
        sa.Column("record_count", sa.BigInteger(), nullable=False),
        sa.Column("occurrence_sum", sa.BigInteger(), nullable=False),
        sa.Column("pattern_version", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("source_set_sha256", name="uq_goodware_baselines_source_set"),
    )
    op.create_table(
        "goodware_baseline_sources",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("baseline_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("goodware_baselines.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("filename", sa.Text(), nullable=False),
        sa.Column("feature_kind", sa.String(32), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("size", sa.BigInteger(), nullable=False),
        sa.Column("blob_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("blobs.id", ondelete="RESTRICT"), nullable=False),
        sa.UniqueConstraint("baseline_id", "filename", name="uq_goodware_baseline_sources_filename"),
    )
    op.create_index("ix_goodware_baseline_sources_blob_id", "goodware_baseline_sources", ["blob_id"])
    op.create_table(
        "goodware_features",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("baseline_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("goodware_baselines.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("feature_kind", sa.String(32), nullable=False),
        sa.Column("normalized_value", sa.Text(), nullable=False),
        sa.Column("occurrence_count", sa.BigInteger(), nullable=False),
        sa.UniqueConstraint("baseline_id", "feature_kind", "normalized_value", name="uq_goodware_features_value"),
    )
    op.create_index("ix_goodware_features_lookup", "goodware_features", ["baseline_id", "feature_kind", "normalized_value"])
    op.create_table(
        "investigation_goodware_baselines",
        sa.Column("investigation_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("analyst_investigations.id", ondelete="RESTRICT"), primary_key=True),
        sa.Column("baseline_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("goodware_baselines.id", ondelete="RESTRICT"), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("investigation_goodware_baselines")
    op.drop_index("ix_goodware_features_lookup", table_name="goodware_features")
    op.drop_table("goodware_features")
    op.drop_index("ix_goodware_baseline_sources_blob_id", table_name="goodware_baseline_sources")
    op.drop_table("goodware_baseline_sources")
    op.drop_table("goodware_baselines")
