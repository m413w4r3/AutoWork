from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0010_code_features"
down_revision = "0009_capability_sets"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "code_feature_sets",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "sample_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("samples.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "blob_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("blobs.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "feature_blob_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("blobs.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("tool_version", sa.String(64), nullable=False),
        sa.Column("escaper_compatibility_version", sa.String(64), nullable=False),
        sa.Column("intel_pic_hash_escape_version", sa.String(64), nullable=False),
        sa.Column("parameters_sha256", sa.String(64), nullable=False),
        sa.Column("architecture", sa.String(32), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("payload", postgresql.JSONB, nullable=False),
        sa.Column("errors", postgresql.JSONB, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "sample_id",
            "tool_version",
            "escaper_compatibility_version",
            "intel_pic_hash_escape_version",
            "parameters_sha256",
            name="uq_code_feature_sets_replay",
        ),
    )
    op.create_index("ix_code_feature_sets_blob_id", "code_feature_sets", ["blob_id"])
    op.create_index(
        "ix_code_feature_sets_feature_blob_id", "code_feature_sets", ["feature_blob_id"]
    )
    op.add_column(
        "sample_feature_index",
        sa.Column("code_feature_set_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_sample_feature_index_code_feature_set",
        "sample_feature_index",
        "code_feature_sets",
        ["code_feature_set_id"],
        ["id"],
        ondelete="RESTRICT",
    )


def downgrade() -> None:
    op.execute(sa.text("DELETE FROM sample_feature_index WHERE code_feature_set_id IS NOT NULL"))
    op.drop_constraint(
        "fk_sample_feature_index_code_feature_set", "sample_feature_index", type_="foreignkey"
    )
    op.drop_column("sample_feature_index", "code_feature_set_id")
    op.drop_index("ix_code_feature_sets_feature_blob_id", table_name="code_feature_sets")
    op.drop_index("ix_code_feature_sets_blob_id", table_name="code_feature_sets")
    op.drop_table("code_feature_sets")
