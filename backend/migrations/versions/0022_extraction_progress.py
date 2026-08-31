"""Persist live Q2 extraction progress on production runs."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB


revision = "0022_extraction_progress"
down_revision = "0021_source_extraction_cache"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "subject_production_runs",
        sa.Column("extraction_progress", JSONB(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("subject_production_runs", "extraction_progress")
