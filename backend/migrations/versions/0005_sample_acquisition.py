"""One Sample per (subject, blob) and the VT sample-acquisition ledger.

Revision ID: 0005_sample_acquisition
Revises: 0004_sample_lifecycle
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_sample_acquisition"
down_revision: str | None = "0004_sample_lifecycle"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_unique_constraint("uq_samples_subject_blob", "samples", ["subject_id", "blob_id"])

    op.create_table(
        "sample_acquisition_attempts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("investigation_id", sa.Uuid(), nullable=False),
        sa.Column("requested_hash", sa.String(length=64), nullable=False),
        sa.Column("hash_family", sa.String(length=8), nullable=False),
        sa.Column("reason", sa.String(length=16), nullable=False),
        sa.Column("outcome", sa.String(length=16), nullable=False),
        sa.Column("sample_id", sa.Uuid(), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("reason IN ('seed', 'hit_review')", name="ck_sample_acquisition_reason"),
        sa.CheckConstraint("outcome IN ('success', 'error')", name="ck_sample_acquisition_outcome"),
        sa.CheckConstraint(
            "hash_family IN ('md5', 'sha1', 'sha256')",
            name="ck_sample_acquisition_hash_family",
        ),
        sa.CheckConstraint(
            "requested_hash ~ '^[0-9a-f]{32}$' OR requested_hash ~ '^[0-9a-f]{40}$' "
            "OR requested_hash ~ '^[0-9a-f]{64}$'",
            name="ck_sample_acquisition_requested_hash",
        ),
        sa.CheckConstraint(
            "(outcome = 'success' AND sample_id IS NOT NULL AND error_code IS NULL) OR "
            "(outcome = 'error' AND sample_id IS NULL AND error_code IS NOT NULL)",
            name="ck_sample_acquisition_outcome_shape",
        ),
        sa.ForeignKeyConstraint(
            ["investigation_id"], ["analyst_investigations.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["sample_id"], ["samples.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_sample_acquisition_attempts_investigation",
        "sample_acquisition_attempts",
        ["investigation_id"],
    )
    # The canonical, DB-enforced replay marker: at most one SUCCESS row per
    # (investigation_id, requested_hash) pair, independent of any Python
    # pre-check.
    op.create_index(
        "uq_sample_acquisition_success_replay",
        "sample_acquisition_attempts",
        ["investigation_id", "requested_hash"],
        unique=True,
        postgresql_where=sa.text("outcome = 'success'"),
    )


def downgrade() -> None:
    op.drop_index("uq_sample_acquisition_success_replay", table_name="sample_acquisition_attempts")
    op.drop_index(
        "ix_sample_acquisition_attempts_investigation", table_name="sample_acquisition_attempts"
    )
    op.drop_table("sample_acquisition_attempts")
    op.drop_constraint("uq_samples_subject_blob", "samples", type_="unique")
