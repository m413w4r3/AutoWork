"""Add analyst investigation state, bounded budgets and append-only decisions.

Revision ID: 0003_analyst_investigation
Revises: 0002_virustotal_observations
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_analyst_investigation"
down_revision: str | None = "0002_virustotal_observations"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("ck_run_stage", "subject_production_runs", type_="check")
    op.create_check_constraint(
        "ck_run_stage",
        "subject_production_runs",
        "current_stage IN ('sources', 'references', 'extraction', 'synthesis', "
        "'analyst_research', 'analyst_note', 'assembly')",
    )
    op.create_table(
        "analyst_investigations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("production_run_id", sa.Uuid(), nullable=False),
        sa.Column("subject_id", sa.Uuid(), nullable=False),
        sa.Column("synthesis_artifact_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("current_stage", sa.String(length=32), nullable=False),
        sa.Column("cycle_number", sa.Integer(), nullable=False),
        sa.Column("input_pack_blob_id", sa.Uuid(), nullable=True),
        sa.Column("input_sha256", sa.String(length=64), nullable=True),
        sa.Column("pivot_conversation_id", sa.Uuid(), nullable=True),
        sa.Column("max_cycles", sa.Integer(), nullable=False),
        sa.Column("max_pivot_runs", sa.Integer(), nullable=False),
        sa.Column("max_hits_acquired", sa.Integer(), nullable=False),
        sa.Column("max_new_samples", sa.Integer(), nullable=False),
        sa.Column("max_vt_read_units", sa.Integer(), nullable=False),
        sa.Column("consumed_pivot_runs", sa.Integer(), nullable=False),
        sa.Column("consumed_hits_acquired", sa.Integer(), nullable=False),
        sa.Column("consumed_new_samples", sa.Integer(), nullable=False),
        sa.Column("consumed_vt_read_units", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.CheckConstraint("version >= 1", name="ck_analyst_investigation_version"),
        sa.CheckConstraint("cycle_number >= 1", name="ck_analyst_investigation_cycle"),
        sa.CheckConstraint("max_cycles >= 1", name="ck_analyst_investigation_max_cycles"),
        sa.CheckConstraint(
            "max_pivot_runs >= 0 AND max_hits_acquired >= 0 AND max_new_samples >= 0 "
            "AND max_vt_read_units >= 0",
            name="ck_analyst_investigation_budget_max",
        ),
        sa.CheckConstraint(
            "consumed_pivot_runs BETWEEN 0 AND max_pivot_runs AND "
            "consumed_hits_acquired BETWEEN 0 AND max_hits_acquired AND "
            "consumed_new_samples BETWEEN 0 AND max_new_samples AND "
            "consumed_vt_read_units BETWEEN 0 AND max_vt_read_units",
            name="ck_analyst_investigation_budget_consumed",
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'awaiting_review', 'completed', 'exhausted', "
            "'failed', 'cancelled')",
            name="ck_analyst_investigation_status",
        ),
        sa.CheckConstraint(
            "current_stage IN ('seeds', 'features', 'tooling', 'invariants', 'pivots', "
            "'corpus', 'detection', 'note')",
            name="ck_analyst_investigation_stage",
        ),
        sa.CheckConstraint(
            "(input_pack_blob_id IS NULL) = (input_sha256 IS NULL)",
            name="ck_analyst_investigation_input_pair",
        ),
        sa.CheckConstraint(
            "input_sha256 IS NULL OR (char_length(input_sha256) = 64 "
            "AND input_sha256 ~ '^[0-9a-f]{64}$')",
            name="ck_analyst_investigation_input_sha256",
        ),
        sa.ForeignKeyConstraint(
            ["production_run_id"], ["subject_production_runs.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["subject_id"], ["subjects.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["synthesis_artifact_id"], ["production_artifacts.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["input_pack_blob_id"], ["blobs.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["pivot_conversation_id"], ["model_conversations.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("production_run_id", name="uq_analyst_investigation_run"),
    )
    op.create_index(
        "ix_analyst_investigations_subject_status",
        "analyst_investigations",
        ["subject_id", "status"],
    )
    op.create_table(
        "analyst_decisions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("investigation_id", sa.Uuid(), nullable=False),
        sa.Column("decision_type", sa.String(length=32), nullable=False),
        sa.Column("target_type", sa.String(length=32), nullable=False),
        sa.Column("target_id", sa.Uuid(), nullable=False),
        sa.Column("actor_id", sa.String(length=255), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("correlation_id", sa.String(length=128), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "decision_type IN ('member_validate', 'member_reject', 'feature_validate', "
            "'feature_reject', 'pivot_approve', 'pivot_reject', 'note_approve', "
            "'note_changes_requested')",
            name="ck_analyst_decisions_type",
        ),
        sa.CheckConstraint(
            "target_type IN ('member', 'feature', 'tool', 'invariant', 'pivot', "
            "'corpus', 'detection', 'note')",
            name="ck_analyst_decisions_target",
        ),
        sa.ForeignKeyConstraint(
            ["investigation_id"], ["analyst_investigations.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_analyst_decisions_investigation",
        "analyst_decisions",
        ["investigation_id", "occurred_at"],
    )
    op.execute(
        "CREATE TRIGGER trg_analyst_decisions_append_only BEFORE UPDATE OR DELETE "
        "ON analyst_decisions FOR EACH ROW EXECUTE FUNCTION reject_evidence_mutation()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER trg_analyst_decisions_append_only ON analyst_decisions")
    op.drop_index("ix_analyst_decisions_investigation", table_name="analyst_decisions")
    op.drop_table("analyst_decisions")
    op.drop_index("ix_analyst_investigations_subject_status", table_name="analyst_investigations")
    op.drop_table("analyst_investigations")
    op.drop_constraint("ck_run_stage", "subject_production_runs", type_="check")
    op.create_check_constraint(
        "ck_run_stage",
        "subject_production_runs",
        "current_stage IN ('sources', 'references', 'extraction', 'synthesis', 'assembly')",
    )
