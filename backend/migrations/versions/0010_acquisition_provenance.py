"""Persist typed provenance on acquisition attempts.

Revision ID: 0010_acquisition_provenance
Revises: 0009_acquisition_strategy_attempts
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0010_acquisition_provenance"
down_revision: str | None = "0009_acquisition_strategy_attempts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "collection_attempts"
_COLUMNS = (
    "acquisition_method",
    "source_equivalence_class",
    "evidence_class",
    "observation_kind",
    "quality_score",
    "quality_assessment",
    "archive_snapshot_time",
    "model_run_id",
    "model_generated",
    "primary_source_verified",
)


def upgrade() -> None:
    existing = {column["name"] for column in sa.inspect(op.get_bind()).get_columns(_TABLE)}
    if existing.intersection(_COLUMNS):
        raise RuntimeError("refusing to add partial acquisition provenance columns")

    op.add_column(_TABLE, sa.Column("acquisition_method", sa.String(64), nullable=True))
    op.add_column(_TABLE, sa.Column("source_equivalence_class", sa.String(64), nullable=True))
    op.add_column(_TABLE, sa.Column("evidence_class", sa.String(64), nullable=True))
    op.add_column(_TABLE, sa.Column("observation_kind", sa.String(64), nullable=True))
    op.add_column(_TABLE, sa.Column("quality_score", sa.Float(), nullable=True))
    op.add_column(_TABLE, sa.Column("quality_assessment", JSONB(), nullable=True))
    op.add_column(
        _TABLE, sa.Column("archive_snapshot_time", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(_TABLE, sa.Column("model_run_id", sa.Uuid(), nullable=True))
    op.add_column(_TABLE, sa.Column("model_generated", sa.Boolean(), nullable=True))
    op.add_column(_TABLE, sa.Column("primary_source_verified", sa.Boolean(), nullable=True))
    op.create_check_constraint(
        "ck_collection_attempts_quality_score",
        _TABLE,
        "quality_score IS NULL OR (quality_score >= 0 AND quality_score <= 1)",
    )


def downgrade() -> None:
    bind = op.get_bind()
    has_provenance = bind.execute(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM collection_attempts WHERE "
            + " OR ".join(f"{column} IS NOT NULL" for column in _COLUMNS)
            + ")"
        )
    ).scalar_one()
    if has_provenance:
        raise RuntimeError("refusing to downgrade while acquisition provenance exists")

    op.drop_constraint("ck_collection_attempts_quality_score", _TABLE, type_="check")
    for column in reversed(_COLUMNS):
        op.drop_column(_TABLE, column)
