"""Persist production batch pause state.

Revision ID: 0002_production_batch_pause
Revises: 0001_baseline
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision: str = "0002_production_batch_pause"
down_revision: str | None = "0001_baseline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | None = None

_TABLE = "edition_production_batches"
_OLD_STATUSES = "'queued', 'running', 'completed', 'completed_with_issues', 'cancelled'"
_PAUSED_STATUSES = (
    "'queued', 'running', 'paused', 'completed', 'completed_with_issues', 'cancelled'"
)


def upgrade() -> None:
    bind = op.get_bind()
    columns = {column["name"] for column in inspect(bind).get_columns(_TABLE)}
    if "paused_at" not in columns:
        op.add_column(_TABLE, sa.Column("paused_at", sa.DateTime(timezone=True), nullable=True))
    if "paused_by" not in columns:
        op.add_column(_TABLE, sa.Column("paused_by", sa.String(255), nullable=True))

    checks = {
        check["name"]: check.get("sqltext", "")
        for check in inspect(bind).get_check_constraints(_TABLE)
    }
    status_check = checks.get("ck_batch_status", "")
    if "paused" not in status_check:
        if "ck_batch_status" in checks:
            op.drop_constraint("ck_batch_status", _TABLE, type_="check")
        op.create_check_constraint("ck_batch_status", _TABLE, f"status IN ({_PAUSED_STATUSES})")
    if "ck_batch_pause_identity" not in checks:
        op.create_check_constraint(
            "ck_batch_pause_identity",
            _TABLE,
            "(paused_at IS NULL) = (paused_by IS NULL)",
        )


def downgrade() -> None:
    bind = op.get_bind()
    op.execute(
        "UPDATE edition_production_batches "
        "SET status = CASE WHEN started_at IS NULL THEN 'queued' ELSE 'running' END, "
        "paused_at = NULL, paused_by = NULL WHERE status = 'paused'"
    )
    checks = {check["name"] for check in inspect(bind).get_check_constraints(_TABLE)}
    if "ck_batch_pause_identity" in checks:
        op.drop_constraint("ck_batch_pause_identity", _TABLE, type_="check")
    if "ck_batch_status" in checks:
        op.drop_constraint("ck_batch_status", _TABLE, type_="check")
    op.create_check_constraint("ck_batch_status", _TABLE, f"status IN ({_OLD_STATUSES})")

    columns = {column["name"] for column in inspect(bind).get_columns(_TABLE)}
    if "paused_by" in columns:
        op.drop_column(_TABLE, "paused_by")
    if "paused_at" in columns:
        op.drop_column(_TABLE, "paused_at")
