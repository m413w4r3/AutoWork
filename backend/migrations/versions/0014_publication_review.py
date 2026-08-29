"""Add the append-only publication review decision journal."""

from alembic import op
import sqlalchemy as sa


revision = "0014_publication_review"
down_revision = "0013_production_batch"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "publication_review_decisions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("edition_id", sa.Uuid(), nullable=False),
        sa.Column("subject_id", sa.Uuid(), nullable=False),
        sa.Column("production_run_id", sa.Uuid(), nullable=False),
        sa.Column("pipeline_generation", sa.Integer(), nullable=False),
        sa.Column("document_artifact_id", sa.Uuid(), nullable=False),
        sa.Column("document_artifact_version", sa.Integer(), nullable=False),
        sa.Column("document_input_hash", sa.String(length=64), nullable=False),
        sa.Column("decision", sa.String(length=16), nullable=False),
        sa.Column("actor_id", sa.String(length=255), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "decision IN ('include', 'exclude')",
            name="ck_publication_review_decision",
        ),
        sa.CheckConstraint(
            "pipeline_generation >= 0",
            name="ck_publication_review_generation",
        ),
        sa.CheckConstraint(
            "document_artifact_version >= 1",
            name="ck_publication_review_artifact_version",
        ),
        sa.CheckConstraint(
            "char_length(document_input_hash) = 64 "
            "AND document_input_hash ~ '^[0-9a-f]{64}$'",
            name="ck_publication_review_input_hash",
        ),
        sa.CheckConstraint(
            "char_length(btrim(actor_id)) > 0",
            name="ck_publication_review_actor",
        ),
        sa.CheckConstraint(
            "reason IS NULL OR char_length(reason) <= 500",
            name="ck_publication_review_reason_length",
        ),
        sa.CheckConstraint(
            "decision <> 'exclude' OR char_length(btrim(reason)) > 0",
            name="ck_publication_review_exclude_reason",
        ),
        sa.ForeignKeyConstraint(
            ["edition_id"],
            ["editions.id"],
            name="fk_publication_review_edition",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["subject_id"],
            ["subjects.id"],
            name="fk_publication_review_subject",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["production_run_id"],
            ["subject_production_runs.id"],
            name="fk_publication_review_run",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["document_artifact_id"],
            ["production_artifacts.id"],
            name="fk_publication_review_artifact",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_publication_review_edition_occurred",
        "publication_review_decisions",
        ["edition_id", "occurred_at", "id"],
    )
    op.create_index(
        "ix_publication_review_subject_history",
        "publication_review_decisions",
        ["subject_id", "occurred_at", "id"],
    )
    op.create_index(
        "ix_publication_review_current_artifact",
        "publication_review_decisions",
        [
            "production_run_id",
            "pipeline_generation",
            "document_artifact_id",
            "document_artifact_version",
            "document_input_hash",
        ],
    )
    op.execute(
        "CREATE TRIGGER trg_publication_review_decisions_append_only "
        "BEFORE UPDATE OR DELETE ON publication_review_decisions "
        "FOR EACH ROW EXECUTE FUNCTION reject_evidence_mutation()"
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER trg_publication_review_decisions_append_only "
        "ON publication_review_decisions"
    )
    op.drop_index(
        "ix_publication_review_current_artifact",
        table_name="publication_review_decisions",
    )
    op.drop_index(
        "ix_publication_review_subject_history",
        table_name="publication_review_decisions",
    )
    op.drop_index(
        "ix_publication_review_edition_occurred",
        table_name="publication_review_decisions",
    )
    op.drop_table("publication_review_decisions")
