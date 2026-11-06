"""Record fallback acquisition strategy details on collection attempts.

Revision ID: 0009_acquisition_strategy_attempts
Revises: 0008_production_routing
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009_acquisition_strategy_attempts"
down_revision: str | None = "0008_production_routing"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    columns = {
        column["name"] for column in sa.inspect(op.get_bind()).get_columns("collection_attempts")
    }
    expected = {"strategy_name", "strategy_version", "duration_ms"}
    if columns.intersection(expected):
        raise RuntimeError("refusing to add partial acquisition strategy attempt metadata")
    op.add_column("collection_attempts", sa.Column("strategy_name", sa.String(64), nullable=True))
    op.add_column(
        "collection_attempts", sa.Column("strategy_version", sa.String(32), nullable=True)
    )
    op.add_column("collection_attempts", sa.Column("duration_ms", sa.BigInteger(), nullable=True))
    op.create_check_constraint(
        "ck_collection_attempts_strategy_metadata",
        "collection_attempts",
        "(strategy_name IS NULL AND strategy_version IS NULL) OR "
        "(strategy_name IS NOT NULL AND strategy_version IS NOT NULL)",
    )
    op.create_check_constraint(
        "ck_collection_attempts_duration_ms",
        "collection_attempts",
        "duration_ms IS NULL OR duration_ms >= 0",
    )
    op.drop_constraint("ck_collection_attempts_outcome", "collection_attempts", type_="check")
    op.create_check_constraint(
        "ck_collection_attempts_outcome",
        "collection_attempts",
        "outcome IN ('succeeded', 'unavailable', 'blocked', 'too_large', 'error', "
        "'interrupted', 'skipped')",
    )


def downgrade() -> None:
    bind = op.get_bind()
    has_strategy_history = bind.execute(
        sa.text("SELECT EXISTS (SELECT 1 FROM collection_attempts WHERE strategy_name IS NOT NULL)")
    ).scalar_one()
    if has_strategy_history:
        raise RuntimeError(
            "refusing to downgrade while acquisition strategy attempt history exists"
        )
    op.drop_constraint("ck_collection_attempts_outcome", "collection_attempts", type_="check")
    op.create_check_constraint(
        "ck_collection_attempts_outcome",
        "collection_attempts",
        "outcome IN ('succeeded', 'unavailable', 'blocked', 'too_large', 'error', 'interrupted')",
    )
    op.drop_constraint("ck_collection_attempts_duration_ms", "collection_attempts", type_="check")
    op.drop_constraint(
        "ck_collection_attempts_strategy_metadata", "collection_attempts", type_="check"
    )
    op.drop_column("collection_attempts", "duration_ms")
    op.drop_column("collection_attempts", "strategy_version")
    op.drop_column("collection_attempts", "strategy_name")
