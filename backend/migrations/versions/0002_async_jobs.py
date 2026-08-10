"""Observable and idempotent asynchronous jobs.

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-07
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JOB_STATUS_CHECK = (
    "status IN ('queued', 'running', 'waiting_human', 'succeeded', 'failed', 'cancelled')"
)


def upgrade() -> None:
    op.create_table(
        "jobs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=128), nullable=False),
        sa.Column("aggregate_type", sa.String(length=64), nullable=False),
        sa.Column("aggregate_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("progress_current", sa.BigInteger(), nullable=False),
        sa.Column("progress_total", sa.BigInteger(), nullable=False),
        sa.Column("user_message", sa.Text(), nullable=True),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("attempt", sa.BigInteger(), nullable=False),
        sa.Column("max_attempts", sa.BigInteger(), nullable=False),
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("correlation_id", sa.String(length=128), nullable=False),
        sa.Column(
            "input_parameters",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("output_reference", sa.Text(), nullable=True),
        sa.Column("cancellation_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(JOB_STATUS_CHECK, name="ck_jobs_status"),
        sa.CheckConstraint("progress_current >= 0", name="ck_jobs_progress_current"),
        sa.CheckConstraint("progress_total >= 0", name="ck_jobs_progress_total"),
        sa.CheckConstraint(
            "progress_total = 0 OR progress_current <= progress_total",
            name="ck_jobs_progress_bounds",
        ),
        sa.CheckConstraint("attempt >= 0", name="ck_jobs_attempt"),
        sa.CheckConstraint("max_attempts BETWEEN 1 AND 20", name="ck_jobs_max_attempts"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key", name="uq_jobs_idempotency_key"),
    )
    op.create_index("ix_jobs_status_next_retry", "jobs", ["status", "next_retry_at"])
    op.create_index("ix_jobs_running_heartbeat", "jobs", ["status", "heartbeat_at"])
    op.create_index("ix_jobs_aggregate", "jobs", ["aggregate_type", "aggregate_id"])


def downgrade() -> None:
    op.drop_index("ix_jobs_aggregate", table_name="jobs")
    op.drop_index("ix_jobs_running_heartbeat", table_name="jobs")
    op.drop_index("ix_jobs_status_next_retry", table_name="jobs")
    op.drop_table("jobs")
