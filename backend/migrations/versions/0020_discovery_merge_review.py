"""Persist merge plans and human-review lineage.

Revision ID: 0020
Revises: 0019
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "discovery_merge_runs",
        sa.Column("plan_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "discovery_merge_runs",
        sa.Column(
            "review_reasons",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.add_column(
        "discovery_merge_runs",
        sa.Column("supersedes_merge_run_id", sa.Uuid(), nullable=True),
    )
    op.create_foreign_key(
        "fk_discovery_merge_runs_supersedes",
        "discovery_merge_runs",
        "discovery_merge_runs",
        ["supersedes_merge_run_id"],
        ["id"],
        ondelete="RESTRICT",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_discovery_merge_runs_supersedes", "discovery_merge_runs", type_="foreignkey"
    )
    op.drop_column("discovery_merge_runs", "supersedes_merge_run_id")
    op.drop_column("discovery_merge_runs", "review_reasons")
    op.drop_column("discovery_merge_runs", "plan_payload")
