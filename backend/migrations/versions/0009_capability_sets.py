from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0009_capability_sets"
down_revision = "0008_reference_corpus"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "capability_sets",
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
        sa.Column("tool_name", sa.String(32), nullable=False),
        sa.Column("tool_version", sa.String(64), nullable=False),
        sa.Column("ruleset_sha256", sa.String(64), nullable=False),
        sa.Column("parameters_sha256", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("capabilities", postgresql.JSONB, nullable=False),
        sa.Column("errors", postgresql.JSONB, nullable=False),
        sa.UniqueConstraint(
            "sample_id",
            "tool_version",
            "ruleset_sha256",
            "parameters_sha256",
            name="uq_capability_sets_replay",
        ),
    )
    op.create_index("ix_capability_sets_blob_id", "capability_sets", ["blob_id"])
    op.add_column(
        "sample_feature_index",
        sa.Column("capability_set_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_sample_feature_index_capability_set",
        "sample_feature_index",
        "capability_sets",
        ["capability_set_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.alter_column(
        "sample_feature_index",
        "feature_set_id",
        existing_type=postgresql.UUID(as_uuid=True),
        nullable=True,
    )


def downgrade() -> None:
    op.execute(sa.text("DELETE FROM sample_feature_index WHERE capability_set_id IS NOT NULL"))
    op.alter_column(
        "sample_feature_index",
        "feature_set_id",
        existing_type=postgresql.UUID(as_uuid=True),
        nullable=False,
    )
    op.drop_constraint(
        "fk_sample_feature_index_capability_set", "sample_feature_index", type_="foreignkey"
    )
    op.drop_column("sample_feature_index", "capability_set_id")
    op.drop_index("ix_capability_sets_blob_id", table_name="capability_sets")
    op.drop_table("capability_sets")
