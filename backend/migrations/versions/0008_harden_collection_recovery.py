"""Harden source collection recovery and evidence provenance.

Revision ID: 0008
Revises: 0007
Create Date: 2026-08-10
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "collection_policy_snapshots",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("max_redirects", sa.Integer(), nullable=False),
        sa.Column("timeout_seconds", sa.Float(), nullable=False),
        sa.Column("max_download_bytes", sa.BigInteger(), nullable=False),
        sa.Column("max_expanded_bytes", sa.BigInteger(), nullable=False),
        sa.Column("max_decompression_ratio", sa.Float(), nullable=False),
        sa.Column("user_agent", sa.String(length=500), nullable=False),
        sa.Column("allowed_domains", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("blocked_domains", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("collector_version", sa.String(length=64), nullable=False),
        sa.Column("extraction_limits", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "char_length(id) = 64 AND id ~ '^[0-9a-f]{64}$'",
            name="ck_collection_policy_snapshots_id",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.execute(
        """
        INSERT INTO collection_policy_snapshots (
            id, max_redirects, timeout_seconds, max_download_bytes,
            max_expanded_bytes, max_decompression_ratio, user_agent,
            allowed_domains, blocked_domains, collector_version,
            extraction_limits, created_at
        )
        SELECT configuration_id, 0, 0, 0, 0, 0,
               'legacy-unreconstructible', '[]'::jsonb, '[]'::jsonb,
               'legacy-0007', '{"legacy_unreconstructible": true}'::jsonb,
               min(attempted_at)
        FROM collection_attempts
        GROUP BY configuration_id
        """
    )

    op.add_column("source_collections", sa.Column("decoded_blob_id", sa.Uuid(), nullable=True))
    op.add_column("source_collections", sa.Column("fetch_job_id", sa.Uuid(), nullable=True))
    op.add_column(
        "source_collections", sa.Column("fetch_policy_snapshot_id", sa.String(64), nullable=True)
    )
    op.add_column(
        "source_collections",
        sa.Column("fetch_started_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "source_collections",
        sa.Column("fetch_lease_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_source_collections_decoded_blob",
        "source_collections",
        "blobs",
        ["decoded_blob_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_source_collections_fetch_job",
        "source_collections",
        "jobs",
        ["fetch_job_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_source_collections_fetch_policy_snapshot",
        "source_collections",
        "collection_policy_snapshots",
        ["fetch_policy_snapshot_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.execute(
        """
        UPDATE source_collections AS collection
        SET decoded_blob_id = document.blob_id
        FROM source_documents AS document
        WHERE collection.source_document_id = document.id
          AND collection.decoded_blob_id IS NULL
        """
    )
    op.create_check_constraint(
        "ck_source_collections_verified_evidence",
        "source_collections",
        "relationship_status <> 'verified' OR "
        "relationship_evidence LIKE 'human:%' OR "
        "relationship_evidence LIKE 'deterministic:%'",
    )

    op.add_column(
        "collection_attempts", sa.Column("policy_snapshot_id", sa.String(64), nullable=True)
    )
    op.add_column("collection_attempts", sa.Column("encoded_size", sa.BigInteger(), nullable=True))
    op.add_column("collection_attempts", sa.Column("encoded_sha256", sa.String(64), nullable=True))
    op.add_column("collection_attempts", sa.Column("decoded_size", sa.BigInteger(), nullable=True))
    op.add_column("collection_attempts", sa.Column("decoded_sha256", sa.String(64), nullable=True))
    op.add_column(
        "collection_attempts", sa.Column("content_encoding", sa.String(64), nullable=True)
    )
    op.execute(
        """
        UPDATE collection_attempts
        SET policy_snapshot_id = configuration_id,
            encoded_size = size,
            encoded_sha256 = sha256,
            decoded_size = size,
            decoded_sha256 = sha256,
            content_encoding = CASE WHEN outcome = 'succeeded' THEN 'legacy-decoded' ELSE NULL END
        """
    )
    op.alter_column("collection_attempts", "policy_snapshot_id", nullable=False)
    op.create_foreign_key(
        "fk_collection_attempts_policy_snapshot",
        "collection_attempts",
        "collection_policy_snapshots",
        ["policy_snapshot_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.drop_constraint("ck_collection_attempts_outcome", "collection_attempts", type_="check")
    op.create_check_constraint(
        "ck_collection_attempts_outcome",
        "collection_attempts",
        "outcome IN ('succeeded', 'unavailable', 'blocked', 'too_large', 'error', 'interrupted')",
    )
    op.create_check_constraint(
        "ck_collection_attempts_encoded_size",
        "collection_attempts",
        "encoded_size IS NULL OR encoded_size >= 0",
    )
    op.create_check_constraint(
        "ck_collection_attempts_decoded_size",
        "collection_attempts",
        "decoded_size IS NULL OR decoded_size >= 0",
    )
    op.create_check_constraint(
        "ck_collection_attempts_encoded_sha256",
        "collection_attempts",
        "encoded_sha256 IS NULL OR "
        "(char_length(encoded_sha256) = 64 AND encoded_sha256 ~ '^[0-9a-f]{64}$')",
    )
    op.create_check_constraint(
        "ck_collection_attempts_decoded_sha256",
        "collection_attempts",
        "decoded_sha256 IS NULL OR "
        "(char_length(decoded_sha256) = 64 AND decoded_sha256 ~ '^[0-9a-f]{64}$')",
    )

    op.add_column("claims", sa.Column("chunk_id", sa.String(128), nullable=True))
    op.add_column("claims", sa.Column("local_span_start", sa.BigInteger(), nullable=True))
    op.add_column("claims", sa.Column("local_span_end", sa.BigInteger(), nullable=True))
    op.add_column("claims", sa.Column("model_run_id", sa.Uuid(), nullable=True))
    op.execute(
        """
        UPDATE claims
        SET chunk_id = 'legacy-full-document',
            local_span_start = span_start,
            local_span_end = span_end
        """
    )
    op.alter_column("claims", "chunk_id", nullable=False)
    op.create_foreign_key(
        "fk_claims_model_run",
        "claims",
        "model_runs",
        ["model_run_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        "ck_claims_local_span",
        "claims",
        "(local_span_start IS NULL AND local_span_end IS NULL) OR "
        "(local_span_start >= 0 AND local_span_end > local_span_start)",
    )

    op.create_table(
        "rejected_model_proposals",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("source_document_id", sa.Uuid(), nullable=False),
        sa.Column("derived_artifact_id", sa.Uuid(), nullable=False),
        sa.Column("chunk_id", sa.String(128), nullable=False),
        sa.Column("category", sa.String(64), nullable=False),
        sa.Column("requested_kind", sa.String(32), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("proposal_hash", sa.String(64), nullable=False),
        sa.Column("model_run_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "char_length(proposal_hash) = 64 AND proposal_hash ~ '^[0-9a-f]{64}$'",
            name="ck_rejected_model_proposals_hash",
        ),
        sa.ForeignKeyConstraint(
            ["source_document_id"], ["source_documents.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["derived_artifact_id"], ["derived_artifacts.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["model_run_id"], ["model_runs.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_rejected_model_proposals_source",
        "rejected_model_proposals",
        ["source_document_id", "created_at"],
    )
    for table in ("collection_policy_snapshots", "rejected_model_proposals"):
        op.execute(
            f"""
            CREATE TRIGGER trg_{table}_append_only
            BEFORE UPDATE OR DELETE ON {table}
            FOR EACH ROW EXECUTE FUNCTION reject_evidence_mutation()
            """
        )


def downgrade() -> None:
    for table in ("rejected_model_proposals", "collection_policy_snapshots"):
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_append_only ON {table}")
    op.drop_index("ix_rejected_model_proposals_source", table_name="rejected_model_proposals")
    op.drop_table("rejected_model_proposals")
    op.drop_constraint("ck_claims_local_span", "claims", type_="check")
    op.drop_constraint("fk_claims_model_run", "claims", type_="foreignkey")
    op.drop_column("claims", "model_run_id")
    op.drop_column("claims", "local_span_end")
    op.drop_column("claims", "local_span_start")
    op.drop_column("claims", "chunk_id")

    op.drop_constraint(
        "ck_collection_attempts_decoded_sha256", "collection_attempts", type_="check"
    )
    op.drop_constraint(
        "ck_collection_attempts_encoded_sha256", "collection_attempts", type_="check"
    )
    op.drop_constraint("ck_collection_attempts_decoded_size", "collection_attempts", type_="check")
    op.drop_constraint("ck_collection_attempts_encoded_size", "collection_attempts", type_="check")
    op.drop_constraint("ck_collection_attempts_outcome", "collection_attempts", type_="check")
    op.create_check_constraint(
        "ck_collection_attempts_outcome",
        "collection_attempts",
        "outcome IN ('succeeded', 'unavailable', 'blocked', 'too_large', 'error')",
    )
    op.drop_constraint(
        "fk_collection_attempts_policy_snapshot", "collection_attempts", type_="foreignkey"
    )
    op.drop_column("collection_attempts", "content_encoding")
    op.drop_column("collection_attempts", "decoded_sha256")
    op.drop_column("collection_attempts", "decoded_size")
    op.drop_column("collection_attempts", "encoded_sha256")
    op.drop_column("collection_attempts", "encoded_size")
    op.drop_column("collection_attempts", "policy_snapshot_id")

    op.drop_constraint(
        "ck_source_collections_verified_evidence", "source_collections", type_="check"
    )
    op.drop_constraint("fk_source_collections_fetch_job", "source_collections", type_="foreignkey")
    op.drop_constraint(
        "fk_source_collections_fetch_policy_snapshot", "source_collections", type_="foreignkey"
    )
    op.drop_constraint(
        "fk_source_collections_decoded_blob", "source_collections", type_="foreignkey"
    )
    op.drop_column("source_collections", "fetch_lease_expires_at")
    op.drop_column("source_collections", "fetch_started_at")
    op.drop_column("source_collections", "fetch_job_id")
    op.drop_column("source_collections", "fetch_policy_snapshot_id")
    op.drop_column("source_collections", "decoded_blob_id")
    op.drop_table("collection_policy_snapshots")
