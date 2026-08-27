"""static analysis feature sets

Revision ID: 0006_static_analysis
Revises: 0005_sample_acquisition
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0006_static_analysis"
down_revision = "0005_sample_acquisition"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table("sample_feature_sets", sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True), sa.Column("sample_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("samples.id", ondelete="RESTRICT"), nullable=False), sa.Column("blob_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("blobs.id", ondelete="RESTRICT"), nullable=False), sa.Column("feature_blob_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("blobs.id", ondelete="RESTRICT"), nullable=False), sa.Column("extractor_version", sa.String(64), nullable=False), sa.Column("parameters_sha256", sa.String(64), nullable=False), sa.Column("payload", postgresql.JSONB(), nullable=False), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), sa.UniqueConstraint("sample_id", "extractor_version", "parameters_sha256", name="uq_sample_feature_sets_replay"))
    op.create_index("ix_sample_feature_sets_blob_id", "sample_feature_sets", ["blob_id"])
    op.create_table("sample_feature_index", sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True), sa.Column("sample_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("samples.id", ondelete="RESTRICT"), nullable=False), sa.Column("feature_set_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("sample_feature_sets.id", ondelete="RESTRICT"), nullable=False), sa.Column("feature_kind", sa.String(32), nullable=False), sa.Column("normalized_value", sa.Text(), nullable=False), sa.Column("occurrence_count", sa.Integer(), nullable=False), sa.UniqueConstraint("feature_set_id", "feature_kind", "normalized_value", name="uq_sample_feature_index_value"))
    op.create_index("ix_sample_feature_index_sample_kind", "sample_feature_index", ["sample_id", "feature_kind"])


def downgrade() -> None:
    op.drop_table("sample_feature_index")
    op.drop_table("sample_feature_sets")
