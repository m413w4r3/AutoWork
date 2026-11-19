"""Add the transactional editorial proposal impact outbox.

Revision ID: 0015_editorial_proposal_impacts
Revises: 0014_editorial_proposals
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0015_editorial_proposal_impacts"
down_revision = "0014_editorial_proposals"
branch_labels = None
depends_on = None

_TABLE = "editorial_proposal_impacts"
_GUARD_FUNCTION = "editorial_proposal_impacts_guard_mutation"
_TRUNCATE_FUNCTION = "editorial_proposal_impacts_reject_truncate"


def upgrade() -> None:
    bind = op.get_bind()
    if sa.inspect(bind).has_table(_TABLE):
        raise RuntimeError(f"{_TABLE}: expected absent, actual present")

    op.create_table(
        _TABLE,
        sa.Column("impact_id", sa.Uuid(), nullable=False),
        sa.Column("subject_id", sa.Uuid(), nullable=False),
        sa.Column("production_run_id", sa.Uuid(), nullable=False),
        sa.Column("proposal_id", sa.Uuid(), nullable=False),
        sa.Column("accepted_artifact_id", sa.Uuid(), nullable=False),
        sa.Column("accepted_snapshot_id", sa.Uuid(), nullable=False),
        sa.Column("base_pin_hash", sa.String(length=64), nullable=False),
        sa.Column("accepted_pin_hash", sa.String(length=64), nullable=False),
        sa.Column("sequence", sa.BigInteger(), nullable=False),
        sa.Column("state", sa.String(length=24), server_default="pending", nullable=False),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_error_code", sa.String(length=100), nullable=True),
        sa.Column(
            "available_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("sequence >= 1", name="ck_editorial_proposal_impacts_sequence"),
        sa.CheckConstraint(
            "state IN ('pending', 'running', 'done', 'failed', 'superseded')",
            name="ck_editorial_proposal_impacts_state",
        ),
        sa.CheckConstraint("attempt_count >= 0", name="ck_editorial_proposal_impacts_attempts"),
        sa.CheckConstraint(
            "base_pin_hash ~ '^[0-9a-f]{64}$' AND accepted_pin_hash ~ '^[0-9a-f]{64}$'",
            name="ck_editorial_proposal_impacts_pin_hashes",
        ),
        sa.CheckConstraint(
            "state IN ('pending', 'running') OR completed_at IS NOT NULL",
            name="ck_editorial_proposal_impacts_completion",
        ),
        sa.ForeignKeyConstraint(
            ["subject_id"],
            ["subjects.id"],
            name="fk_editorial_proposal_impacts_subject",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["production_run_id"],
            ["production_runs.id"],
            name="fk_editorial_proposal_impacts_run",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["proposal_id"],
            ["editorial_proposals.id"],
            name="fk_editorial_proposal_impacts_proposal",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["accepted_artifact_id"],
            ["production_artifacts.id"],
            name="fk_editorial_proposal_impacts_artifact",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["accepted_snapshot_id"],
            ["editorial_workspace_snapshots.snapshot_id"],
            name="fk_editorial_proposal_impacts_snapshot",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("impact_id", name="pk_editorial_proposal_impacts"),
        sa.UniqueConstraint("proposal_id", name="uq_editorial_proposal_impacts_proposal"),
        sa.UniqueConstraint(
            "production_run_id", "sequence", name="uq_editorial_proposal_impacts_run_sequence"
        ),
    )
    op.create_index(
        "ix_editorial_proposal_impacts_dispatch",
        _TABLE,
        ["state", "available_at", "sequence"],
    )

    op.execute(
        f"""
        CREATE FUNCTION {_GUARD_FUNCTION}() RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'editorial proposal impacts cannot be deleted';
            END IF;
            IF ROW(
                OLD.impact_id, OLD.subject_id, OLD.production_run_id, OLD.proposal_id,
                OLD.accepted_artifact_id, OLD.accepted_snapshot_id, OLD.base_pin_hash,
                OLD.accepted_pin_hash, OLD.sequence, OLD.created_at
            ) IS DISTINCT FROM ROW(
                NEW.impact_id, NEW.subject_id, NEW.production_run_id, NEW.proposal_id,
                NEW.accepted_artifact_id, NEW.accepted_snapshot_id, NEW.base_pin_hash,
                NEW.accepted_pin_hash, NEW.sequence, NEW.created_at
            ) THEN
                RAISE EXCEPTION 'editorial proposal impact identity is immutable';
            END IF;
            IF NEW.attempt_count < OLD.attempt_count THEN
                RAISE EXCEPTION 'editorial proposal impact attempt count cannot decrease';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        f"""
        CREATE TRIGGER trg_editorial_proposal_impacts_guard
        BEFORE UPDATE OR DELETE ON {_TABLE}
        FOR EACH ROW EXECUTE FUNCTION {_GUARD_FUNCTION}()
        """
    )
    op.execute(
        f"""
        CREATE FUNCTION {_TRUNCATE_FUNCTION}() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'editorial proposal impacts cannot be truncated';
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        f"""
        CREATE TRIGGER trg_editorial_proposal_impacts_no_truncate
        BEFORE TRUNCATE ON {_TABLE}
        FOR EACH STATEMENT EXECUTE FUNCTION {_TRUNCATE_FUNCTION}()
        """
    )


def downgrade() -> None:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table(_TABLE):
        raise RuntimeError(f"{_TABLE}: expected present, actual absent")
    bind.execute(sa.text(f"LOCK TABLE {_TABLE} IN ACCESS EXCLUSIVE MODE"))
    row_count = bind.execute(sa.text(f"SELECT count(*) FROM {_TABLE}")).scalar_one()
    if row_count:
        raise RuntimeError(f"refusing to downgrade {_TABLE} after it has been used")

    op.drop_table(_TABLE)
    op.execute(f"DROP FUNCTION {_TRUNCATE_FUNCTION}()")
    op.execute(f"DROP FUNCTION {_GUARD_FUNCTION}()")
