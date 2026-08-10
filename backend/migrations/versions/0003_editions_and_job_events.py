"""Edition management, audit trails and job operational events.

Revision ID: 0003
Revises: 0002
Create Date: 2026-08-08
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TLP_CHECK = "tlp IN ('CLEAR', 'GREEN', 'AMBER', 'AMBER+STRICT', 'RED')"
EDITION_STATUS_CHECK = (
    "status IN ('draft', 'discovery', 'selection', 'production', 'review', "
    "'assembling', 'published', 'archived')"
)
JOB_STATUS_VALUES = "'queued', 'running', 'waiting_human', 'succeeded', 'failed', 'cancelled'"


def upgrade() -> None:
    op.create_table(
        "editions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("country", sa.String(length=100), nullable=False),
        sa.Column("country_code", sa.String(length=2), nullable=False),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column("tlp", sa.String(length=16), nullable=False),
        sa.Column("languages", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("target_major_articles", sa.BigInteger(), nullable=False),
        sa.Column("target_briefs", sa.BigInteger(), nullable=False),
        sa.Column("previous_edition_id", sa.Uuid(), nullable=True),
        sa.Column("source_profile", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("version", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(TLP_CHECK, name="ck_editions_tlp"),
        sa.CheckConstraint(EDITION_STATUS_CHECK, name="ck_editions_status"),
        sa.CheckConstraint("version >= 1", name="ck_editions_version"),
        sa.CheckConstraint("target_major_articles BETWEEN 0 AND 20", name="ck_editions_major"),
        sa.CheckConstraint("target_briefs BETWEEN 0 AND 100", name="ck_editions_briefs"),
        sa.CheckConstraint("period_start <= period_end", name="ck_editions_period_order"),
        sa.CheckConstraint(
            "period_start = date_trunc('month', period_start)::date "
            "AND period_end = (date_trunc('month', period_start) + "
            "interval '1 month - 1 day')::date",
            name="ck_editions_complete_month",
        ),
        sa.CheckConstraint("jsonb_typeof(languages) = 'array'", name="ck_editions_languages"),
        sa.ForeignKeyConstraint(["previous_edition_id"], ["editions.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "country_code", "period_start", "period_end", name="uq_editions_country_period"
        ),
    )
    op.create_index("ix_editions_country_status", "editions", ["country_code", "status"])
    op.create_index("ix_editions_period", "editions", ["period_start", "period_end"])
    op.create_table(
        "edition_audit_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("edition_id", sa.Uuid(), nullable=False),
        sa.Column("actor_id", sa.String(length=255), nullable=False),
        sa.Column("action", sa.String(length=128), nullable=False),
        sa.Column("before", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("after", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("correlation_id", sa.String(length=128), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["edition_id"], ["editions.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_edition_audit_edition", "edition_audit_events", ["edition_id", "occurred_at"]
    )
    op.create_table(
        "job_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("event_type", sa.String(length=128), nullable=False),
        sa.Column("from_status", sa.String(length=32), nullable=True),
        sa.Column("to_status", sa.String(length=32), nullable=False),
        sa.Column("actor_id", sa.String(length=255), nullable=False),
        sa.Column("correlation_id", sa.String(length=128), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(f"to_status IN ({JOB_STATUS_VALUES})", name="ck_job_events_to_status"),
        sa.CheckConstraint(
            f"from_status IS NULL OR from_status IN ({JOB_STATUS_VALUES})",
            name="ck_job_events_from_status",
        ),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_job_events_job", "job_events", ["job_id", "occurred_at"])
    op.execute(
        """
        CREATE TRIGGER trg_editions_prevent_tlp_downgrade
        BEFORE UPDATE OF tlp ON editions
        FOR EACH ROW EXECUTE FUNCTION prevent_tlp_downgrade()
        """
    )
    _install_append_only_guards()


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_job_events_append_only ON job_events")
    op.execute(
        "DROP TRIGGER IF EXISTS trg_edition_audit_events_append_only ON edition_audit_events"
    )
    op.execute("DROP FUNCTION IF EXISTS reject_audit_mutation()")
    op.execute("DROP TRIGGER IF EXISTS trg_editions_prevent_tlp_downgrade ON editions")
    op.drop_index("ix_job_events_job", table_name="job_events")
    op.drop_table("job_events")
    op.drop_index("ix_edition_audit_edition", table_name="edition_audit_events")
    op.drop_table("edition_audit_events")
    op.drop_index("ix_editions_period", table_name="editions")
    op.drop_index("ix_editions_country_status", table_name="editions")
    op.drop_table("editions")


def _install_append_only_guards() -> None:
    op.execute(
        """
        CREATE FUNCTION reject_audit_mutation() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'audit journals are append-only' USING ERRCODE = '55000';
        END;
        $$ LANGUAGE plpgsql
        """
    )
    for table in ("edition_audit_events", "job_events"):
        op.execute(
            f"""
            CREATE TRIGGER trg_{table}_append_only
            BEFORE UPDATE OR DELETE ON {table}
            FOR EACH ROW EXECUTE FUNCTION reject_audit_mutation()
            """
        )
