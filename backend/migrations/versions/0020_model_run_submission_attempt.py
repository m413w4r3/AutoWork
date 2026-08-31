"""Persist the provider submission attempt on each logical model run."""

import sqlalchemy as sa
from alembic import op


revision = "0020_model_run_submission"
down_revision = "0019_production_reuse"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "model_runs",
        sa.Column("submission_attempt", sa.BigInteger(), nullable=False, server_default="0"),
    )
    op.create_check_constraint(
        "ck_model_runs_submission_attempt",
        "model_runs",
        "submission_attempt >= 0",
    )


def downgrade() -> None:
    op.drop_constraint("ck_model_runs_submission_attempt", "model_runs", type_="check")
    op.drop_column("model_runs", "submission_attempt")
