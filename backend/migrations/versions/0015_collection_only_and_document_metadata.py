"""Separate collection from analysis and enrich source documents.

Revision ID: 0015
Revises: 0014
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("source_documents", sa.Column("logical_filename", sa.Text()))
    op.add_column("source_documents", sa.Column("source_collection_id", sa.Uuid()))
    op.add_column("source_documents", sa.Column("source_candidate_id", sa.Uuid()))
    op.add_column("source_documents", sa.Column("decoded_blob_id", sa.Uuid()))
    op.add_column("source_documents", sa.Column("title", sa.Text()))
    op.add_column("source_documents", sa.Column("publisher", sa.Text()))
    op.add_column("source_documents", sa.Column("published_at", sa.Date()))
    op.add_column("source_documents", sa.Column("final_url", sa.Text()))
    op.add_column("source_documents", sa.Column("declared_mime_type", sa.String(255)))
    op.add_column("source_documents", sa.Column("detected_mime_type", sa.String(255)))
    op.add_column("source_documents", sa.Column("encoded_sha256", sa.String(64)))
    op.add_column("source_documents", sa.Column("decoded_sha256", sa.String(64)))
    op.add_column("source_documents", sa.Column("encoded_size", sa.BigInteger()))
    op.add_column("source_documents", sa.Column("decoded_size", sa.BigInteger()))
    op.create_foreign_key(
        "fk_source_documents_decoded_blob",
        "source_documents",
        "blobs",
        ["decoded_blob_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.execute(
        """
        UPDATE source_documents AS document
        SET logical_filename = document.original_name,
            source_collection_id = collection.id,
            source_candidate_id = collection.source_candidate_id,
            decoded_blob_id = collection.decoded_blob_id,
            final_url = attempt.final_url,
            declared_mime_type = attempt.declared_content_type,
            detected_mime_type = attempt.detected_content_type,
            encoded_sha256 = attempt.encoded_sha256,
            decoded_sha256 = attempt.decoded_sha256,
            encoded_size = attempt.encoded_size,
            decoded_size = attempt.decoded_size
        FROM source_collections AS collection
        LEFT JOIN collection_attempts AS attempt ON attempt.id = collection.latest_attempt_id
        WHERE collection.source_document_id = document.id
        """
    )
    op.execute(
        "UPDATE source_documents SET logical_filename=original_name WHERE logical_filename IS NULL"
    )
    op.drop_constraint("ck_source_collections_state", "source_collections", type_="check")
    op.create_check_constraint(
        "ck_source_collections_state",
        "source_collections",
        "state IN ('pending','queued','fetching','archived','extracted','completed',"
        "'unavailable','blocked','failed_retryable','failed_terminal')",
    )


def downgrade() -> None:
    op.execute("UPDATE source_collections SET state='queued' WHERE state='pending'")
    op.drop_constraint("ck_source_collections_state", "source_collections", type_="check")
    op.create_check_constraint(
        "ck_source_collections_state",
        "source_collections",
        "state IN ('queued','fetching','archived','extracted','completed','unavailable',"
        "'blocked','failed_retryable','failed_terminal')",
    )
    op.drop_constraint("fk_source_documents_decoded_blob", "source_documents", type_="foreignkey")
    for column in (
        "decoded_size",
        "encoded_size",
        "decoded_sha256",
        "encoded_sha256",
        "detected_mime_type",
        "declared_mime_type",
        "final_url",
        "published_at",
        "publisher",
        "title",
        "decoded_blob_id",
        "source_candidate_id",
        "source_collection_id",
        "logical_filename",
    ):
        op.drop_column("source_documents", column)
