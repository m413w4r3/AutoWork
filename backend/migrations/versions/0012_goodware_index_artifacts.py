"""replace legacy goodware rows with immutable v2 index artifacts

Revision ID: 0012_goodware_index_artifacts
Revises: 0011_invariant_registry
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0012_goodware_index_artifacts"
down_revision = "0011_invariant_registry"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.execute(sa.text("SELECT EXISTS (SELECT 1 FROM goodware_baselines)")).scalar():
        raise RuntimeError("refusing Goodware v2 migration while legacy baselines exist")

    op.drop_index("ix_goodware_features_lookup", table_name="goodware_features")
    op.drop_table("goodware_features")
    op.drop_constraint(
        "uq_goodware_baselines_source_set",
        table_name="goodware_baselines",
        type_="unique",
    )
    op.drop_column("goodware_baselines", "records_sha256")
    op.add_column(
        "goodware_baselines",
        sa.Column("baseline_fingerprint_sha256", sa.String(64), nullable=False),
    )
    op.add_column(
        "goodware_baselines",
        sa.Column("normalization_version", sa.String(64), nullable=False),
    )
    op.create_unique_constraint(
        "uq_goodware_baselines_fingerprint",
        "goodware_baselines",
        ["baseline_fingerprint_sha256"],
    )

    op.create_table(
        "goodware_baseline_indexes",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "baseline_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("goodware_baselines.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("schema_version", sa.String(64), nullable=False),
        sa.Column("key_version", sa.String(64), nullable=False),
        sa.Column("index_format_version", sa.String(64), nullable=False),
        sa.Column(
            "index_blob_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("blobs.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "manifest_blob_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("blobs.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "baseline_id",
            "index_format_version",
            "key_version",
            name="uq_goodware_baseline_indexes_version",
        ),
    )
    op.create_index(
        "ix_goodware_baseline_indexes_index_blob_id",
        "goodware_baseline_indexes",
        ["index_blob_id"],
    )
    op.create_index(
        "ix_goodware_baseline_indexes_manifest_blob_id",
        "goodware_baseline_indexes",
        ["manifest_blob_id"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.execute(sa.text("SELECT EXISTS (SELECT 1 FROM goodware_baselines)")).scalar():
        raise RuntimeError(
            "refusing Goodware v2 downgrade while baselines exist; v2 metadata cannot be reconstructed"
        )

    op.drop_index(
        "ix_goodware_baseline_indexes_manifest_blob_id",
        table_name="goodware_baseline_indexes",
    )
    op.drop_index(
        "ix_goodware_baseline_indexes_index_blob_id",
        table_name="goodware_baseline_indexes",
    )
    op.drop_table("goodware_baseline_indexes")
    op.drop_constraint(
        "uq_goodware_baselines_fingerprint",
        table_name="goodware_baselines",
        type_="unique",
    )
    op.drop_column("goodware_baselines", "normalization_version")
    op.drop_column("goodware_baselines", "baseline_fingerprint_sha256")
    op.add_column(
        "goodware_baselines",
        sa.Column("records_sha256", sa.String(64), nullable=False),
    )
    op.create_unique_constraint(
        "uq_goodware_baselines_source_set",
        "goodware_baselines",
        ["source_set_sha256"],
    )
    op.create_table(
        "goodware_features",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "baseline_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("goodware_baselines.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("feature_kind", sa.String(32), nullable=False),
        sa.Column("normalized_value", sa.Text(), nullable=False),
        sa.Column("occurrence_count", sa.BigInteger(), nullable=False),
        sa.UniqueConstraint(
            "baseline_id",
            "feature_kind",
            "normalized_value",
            name="uq_goodware_features_value",
        ),
    )
    op.create_index(
        "ix_goodware_features_lookup",
        "goodware_features",
        ["baseline_id", "feature_kind", "normalized_value"],
    )
