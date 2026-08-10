"""Add safe source collection, immutable attempts and evidence artifacts.

Revision ID: 0007
Revises: 0006
Create Date: 2026-08-10
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("ck_human_decisions_type", "human_decisions", type_="check")
    op.create_check_constraint(
        "ck_human_decisions_type",
        "human_decisions",
        "decision_type IN ('merge', 'split', 'reject', 'select', "
        "'claim_validate', 'claim_correct', 'claim_reject', "
        "'indicator_validate', 'indicator_correct', 'indicator_reject', "
        "'source_relationship_validate', 'source_relationship_correct')",
    )
    op.create_table(
        "source_collections",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("subject_id", sa.Uuid(), nullable=False),
        sa.Column("edition_id", sa.Uuid(), nullable=False),
        sa.Column("group_id", sa.Uuid(), nullable=False),
        sa.Column("batch_id", sa.Uuid(), nullable=False),
        sa.Column("source_candidate_id", sa.Uuid(), nullable=False),
        sa.Column("requested_url", sa.Text(), nullable=False),
        sa.Column("proposed_role", sa.String(length=32), nullable=False),
        sa.Column("relationship_status", sa.String(length=32), nullable=False),
        sa.Column("relationship_evidence", sa.Text(), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("source_document_id", sa.Uuid(), nullable=True),
        sa.Column("latest_attempt_id", sa.Uuid(), nullable=True),
        sa.Column("derived_artifact_id", sa.Uuid(), nullable=True),
        sa.Column("error_reason", sa.Text(), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "state IN ('queued', 'fetching', 'archived', 'extracted', 'completed', "
            "'unavailable', 'blocked', 'failed_retryable', 'failed_terminal')",
            name="ck_source_collections_state",
        ),
        sa.CheckConstraint(
            "proposed_role IN ('primary', 'independent', 'relay', 'aggregator', 'social', "
            "'unknown')",
            name="ck_source_collections_role",
        ),
        sa.CheckConstraint(
            "relationship_status IN ('provisional', 'verified')",
            name="ck_source_collections_relationship",
        ),
        sa.CheckConstraint("attempt_count >= 0", name="ck_source_collections_attempt_count"),
        sa.ForeignKeyConstraint(["subject_id"], ["subjects.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["edition_id"], ["editions.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["group_id"], ["editorial_groups.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["batch_id"], ["discovery_batches.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["source_document_id"], ["source_documents.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "subject_id", "source_candidate_id", name="uq_source_collections_subject_candidate"
        ),
    )
    op.create_index(
        "ix_source_collections_subject_state", "source_collections", ["subject_id", "state"]
    )
    op.create_table(
        "collection_attempts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("collection_id", sa.Uuid(), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("configuration_id", sa.String(length=64), nullable=False),
        sa.Column("requested_url", sa.Text(), nullable=False),
        sa.Column("final_url", sa.Text(), nullable=True),
        sa.Column("redirect_chain", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("attempted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("http_status", sa.Integer(), nullable=True),
        sa.Column("declared_content_type", sa.String(length=255), nullable=True),
        sa.Column("detected_content_type", sa.String(length=255), nullable=True),
        sa.Column("size", sa.BigInteger(), nullable=True),
        sa.Column("sha256", sa.String(length=64), nullable=True),
        sa.Column("allowed_headers", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("outcome", sa.String(length=32), nullable=False),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "outcome IN ('succeeded', 'unavailable', 'blocked', 'too_large', 'error')",
            name="ck_collection_attempts_outcome",
        ),
        sa.CheckConstraint("size IS NULL OR size >= 0", name="ck_collection_attempts_size"),
        sa.CheckConstraint(
            "sha256 IS NULL OR (char_length(sha256) = 64 AND sha256 ~ '^[0-9a-f]{64}$')",
            name="ck_collection_attempts_sha256",
        ),
        sa.ForeignKeyConstraint(["collection_id"], ["source_collections.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_collection_attempts_collection",
        "collection_attempts",
        ["collection_id", "attempted_at"],
    )
    op.create_index("ix_collection_attempts_job", "collection_attempts", ["job_id"])
    op.create_table(
        "derived_artifacts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("source_document_id", sa.Uuid(), nullable=False),
        sa.Column("text_blob_id", sa.Uuid(), nullable=False),
        sa.Column("parser_name", sa.String(length=128), nullable=False),
        sa.Column("parser_version", sa.String(length=64), nullable=False),
        sa.Column("text_length", sa.BigInteger(), nullable=False),
        sa.Column("publication_metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("text_length >= 0", name="ck_derived_artifacts_text_length"),
        sa.ForeignKeyConstraint(
            ["source_document_id"], ["source_documents.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["text_blob_id"], ["blobs.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_derived_artifacts_source",
        "derived_artifacts",
        ["source_document_id", "created_at"],
    )
    _create_evidence_table("claims", is_indicator=False)
    _create_evidence_table("indicators", is_indicator=True)
    op.create_foreign_key(
        "fk_source_collections_latest_attempt",
        "source_collections",
        "collection_attempts",
        ["latest_attempt_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_source_collections_derived_artifact",
        "source_collections",
        "derived_artifacts",
        ["derived_artifact_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.execute(
        """
        CREATE FUNCTION reject_evidence_mutation() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION '% is append-only', TG_TABLE_NAME USING ERRCODE = '55000';
        END;
        $$ LANGUAGE plpgsql
        """
    )
    for table in ("collection_attempts", "derived_artifacts", "claims", "indicators"):
        op.execute(
            f"""
            CREATE TRIGGER trg_{table}_append_only
            BEFORE UPDATE OR DELETE ON {table}
            FOR EACH ROW EXECUTE FUNCTION reject_evidence_mutation()
            """
        )


def downgrade() -> None:
    for table in ("collection_attempts", "derived_artifacts", "claims", "indicators"):
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_append_only ON {table}")
    op.execute("DROP FUNCTION IF EXISTS reject_evidence_mutation()")
    op.drop_constraint(
        "fk_source_collections_derived_artifact", "source_collections", type_="foreignkey"
    )
    op.drop_constraint(
        "fk_source_collections_latest_attempt", "source_collections", type_="foreignkey"
    )
    op.drop_index("ix_indicators_source", table_name="indicators")
    op.drop_index("ix_indicators_subject", table_name="indicators")
    op.drop_table("indicators")
    op.drop_index("ix_claims_source", table_name="claims")
    op.drop_index("ix_claims_subject", table_name="claims")
    op.drop_table("claims")
    op.drop_index("ix_derived_artifacts_source", table_name="derived_artifacts")
    op.drop_table("derived_artifacts")
    op.drop_index("ix_collection_attempts_job", table_name="collection_attempts")
    op.drop_index("ix_collection_attempts_collection", table_name="collection_attempts")
    op.drop_table("collection_attempts")
    op.drop_index("ix_source_collections_subject_state", table_name="source_collections")
    op.drop_table("source_collections")
    op.drop_constraint("ck_human_decisions_type", "human_decisions", type_="check")
    op.create_check_constraint(
        "ck_human_decisions_type",
        "human_decisions",
        "decision_type IN ('merge', 'split', 'reject', 'select')",
    )


def _create_evidence_table(table_name: str, *, is_indicator: bool) -> None:
    value_columns = (
        [
            sa.Column("original_value", sa.Text(), nullable=False),
            sa.Column("normalized_value", sa.Text(), nullable=False),
        ]
        if is_indicator
        else [
            sa.Column("value", sa.Text(), nullable=False),
            sa.Column("extraction_method", sa.String(length=128), nullable=False),
            sa.Column(
                "extraction_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False
            ),
        ]
    )
    kinds = (
        "'hash', 'domain', 'ip', 'url', 'cve', 'attack_id', 'email'"
        if is_indicator
        else "'name', 'date', 'ioc', 'cve', 'fact', 'assessment', 'uncertainty', "
        "'infection_chain', 'ttp', 'victimology'"
    )
    op.create_table(
        table_name,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("subject_id", sa.Uuid(), nullable=False),
        sa.Column("edition_id", sa.Uuid(), nullable=False),
        sa.Column("group_id", sa.Uuid(), nullable=False),
        sa.Column("source_document_id", sa.Uuid(), nullable=False),
        sa.Column("derived_artifact_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        *value_columns,
        sa.Column("span_start", sa.BigInteger(), nullable=False),
        sa.Column("span_end", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(f"kind IN ({kinds})", name=f"ck_{table_name}_kind"),
        sa.CheckConstraint(
            "span_start >= 0 AND span_end > span_start", name=f"ck_{table_name}_span"
        ),
        sa.ForeignKeyConstraint(["subject_id"], ["subjects.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["edition_id"], ["editions.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["group_id"], ["editorial_groups.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["source_document_id"], ["source_documents.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["derived_artifact_id"], ["derived_artifacts.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(f"ix_{table_name}_subject", table_name, ["subject_id", "created_at"])
    op.create_index(f"ix_{table_name}_source", table_name, ["source_document_id"])
