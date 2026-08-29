"""Protect the one-active-production-run invariant at the database boundary."""

import sqlalchemy as sa
from alembic import op


revision = "0018_subject_active_run"
down_revision = "0017_unified_article_production"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "uq_subject_production_one_active_run",
        "subject_production_runs",
        ["subject_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('queued', 'running')"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_subject_production_one_active_run",
        table_name="subject_production_runs",
    )
