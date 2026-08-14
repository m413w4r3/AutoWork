"""Allow incomplete ChatGPT runs to wait for human recovery.

Revision ID: 0014
Revises: 0013
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("ck_model_runs_status", "model_runs", type_="check")
    op.create_check_constraint(
        "ck_model_runs_status",
        "model_runs",
        "status IN ('running','waiting_background','needs_review','succeeded','failed','blocked')",
    )


def downgrade() -> None:
    op.execute(
        "UPDATE model_runs SET status='failed', error_code=COALESCE(error_code,'no_final_answer') "
        "WHERE status='needs_review'"
    )
    op.drop_constraint("ck_model_runs_status", "model_runs", type_="check")
    op.create_check_constraint(
        "ck_model_runs_status",
        "model_runs",
        "status IN ('running','waiting_background','succeeded','failed','blocked')",
    )
