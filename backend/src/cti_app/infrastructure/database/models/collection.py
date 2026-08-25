from datetime import date, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base
from .source_relationships import RELATIONSHIP_STATUS_VALUES_SQL

COLLECTION_STATE_VALUES_SQL = (
    "'pending', 'queued', 'fetching', 'archived', 'extracted', 'completed', 'unavailable', "
    "'blocked', 'failed_retryable', 'failed_terminal'"
)
SOURCE_ROLE_VALUES_SQL = "'primary', 'independent', 'relay', 'aggregator', 'social', 'unknown'"
ATTEMPT_OUTCOME_VALUES_SQL = (
    "'succeeded', 'unavailable', 'blocked', 'too_large', 'error', 'interrupted'"
)
CLAIM_KIND_VALUES_SQL = (
    "'name', 'date', 'ioc', 'cve', 'fact', 'assessment', 'uncertainty', "
    "'infection_chain', 'ttp', 'victimology'"
)
INDICATOR_KIND_VALUES_SQL = "'hash', 'domain', 'ip', 'url', 'cve', 'attack_id', 'email'"
SOURCE_ORIGIN_KIND_VALUES_SQL = "'discovery', 'reference_research', 'referenced_evidence', 'manual'"


class SourceCollectionRow(Base):
    __tablename__ = "source_collections"
    __table_args__ = (
        UniqueConstraint(
            "subject_id", "source_candidate_id", name="uq_source_collections_subject_candidate"
        ),
        UniqueConstraint(
            "subject_id", "canonical_url", name="uq_source_collections_subject_canonical_url"
        ),
        CheckConstraint(
            f"origin_kind IN ({SOURCE_ORIGIN_KIND_VALUES_SQL})",
            name="ck_source_collections_origin_kind",
        ),
        CheckConstraint(
            "(origin_kind = 'referenced_evidence') = (parent_source_collection_id IS NOT NULL)",
            name="ck_source_collections_referenced_parent",
        ),
        CheckConstraint(
            f"state IN ({COLLECTION_STATE_VALUES_SQL})", name="ck_source_collections_state"
        ),
        CheckConstraint(
            f"proposed_role IN ({SOURCE_ROLE_VALUES_SQL})",
            name="ck_source_collections_role",
        ),
        CheckConstraint(
            f"relationship_status IN ({RELATIONSHIP_STATUS_VALUES_SQL})",
            name="ck_source_collections_relationship",
        ),
        CheckConstraint(
            "relationship_status <> 'verified' OR "
            "relationship_evidence LIKE 'human:%' OR "
            "relationship_evidence LIKE 'deterministic:%'",
            name="ck_source_collections_verified_evidence",
        ),
        CheckConstraint("attempt_count >= 0", name="ck_source_collections_attempt_count"),
        Index("ix_source_collections_subject_state", "subject_id", "state"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    subject_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("subjects.id", ondelete="RESTRICT"), nullable=False
    )
    edition_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("editions.id", ondelete="RESTRICT"), nullable=False
    )
    group_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("editorial_groups.id", ondelete="RESTRICT"), nullable=False
    )
    batch_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("discovery_batches.id", ondelete="RESTRICT")
    )
    source_candidate_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    parent_source_collection_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("source_collections.id", ondelete="RESTRICT")
    )
    origin_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    requested_url: Mapped[str] = mapped_column(Text, nullable=False)
    canonical_url: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str | None] = mapped_column(Text)
    publisher: Mapped[str | None] = mapped_column(Text)
    published_at: Mapped[date | None] = mapped_column(Date)
    source_tlp: Mapped[str] = mapped_column(String(32), nullable=False)
    sensitivity: Mapped[str] = mapped_column(String(32), nullable=False)
    external_llm_allowed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    do_not_submit: Mapped[bool] = mapped_column(Boolean, nullable=False)
    proposed_role: Mapped[str] = mapped_column(String(32), nullable=False)
    relationship_status: Mapped[str] = mapped_column(String(32), nullable=False)
    relationship_evidence: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    source_document_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("source_documents.id", ondelete="RESTRICT")
    )
    decoded_blob_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("blobs.id", ondelete="RESTRICT")
    )
    latest_attempt_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("collection_attempts.id", ondelete="RESTRICT")
    )
    derived_artifact_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("derived_artifacts.id", ondelete="RESTRICT")
    )
    fetch_job_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("jobs.id", ondelete="RESTRICT")
    )
    fetch_policy_snapshot_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("collection_policy_snapshots.id", ondelete="RESTRICT")
    )
    fetch_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fetch_lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_reason: Mapped[str | None] = mapped_column(Text)
    attempt_count: Mapped[int] = mapped_column(nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class CollectionPolicySnapshotRow(Base):
    __tablename__ = "collection_policy_snapshots"
    __table_args__ = (
        CheckConstraint(
            "char_length(id) = 64 AND id ~ '^[0-9a-f]{64}$'",
            name="ck_collection_policy_snapshots_id",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    max_redirects: Mapped[int] = mapped_column(nullable=False)
    timeout_seconds: Mapped[float] = mapped_column(nullable=False)
    max_download_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    max_expanded_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    max_decompression_ratio: Mapped[float] = mapped_column(nullable=False)
    user_agent: Mapped[str] = mapped_column(String(500), nullable=False)
    allowed_domains: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    blocked_domains: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    collector_version: Mapped[str] = mapped_column(String(64), nullable=False)
    extraction_limits: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class CollectionAttemptRow(Base):
    __tablename__ = "collection_attempts"
    __table_args__ = (
        CheckConstraint(
            f"outcome IN ({ATTEMPT_OUTCOME_VALUES_SQL})", name="ck_collection_attempts_outcome"
        ),
        CheckConstraint("size IS NULL OR size >= 0", name="ck_collection_attempts_size"),
        CheckConstraint(
            "sha256 IS NULL OR (char_length(sha256) = 64 AND sha256 ~ '^[0-9a-f]{64}$')",
            name="ck_collection_attempts_sha256",
        ),
        CheckConstraint(
            "encoded_size IS NULL OR encoded_size >= 0",
            name="ck_collection_attempts_encoded_size",
        ),
        CheckConstraint(
            "decoded_size IS NULL OR decoded_size >= 0",
            name="ck_collection_attempts_decoded_size",
        ),
        CheckConstraint(
            "encoded_sha256 IS NULL OR "
            "(char_length(encoded_sha256) = 64 AND encoded_sha256 ~ '^[0-9a-f]{64}$')",
            name="ck_collection_attempts_encoded_sha256",
        ),
        CheckConstraint(
            "decoded_sha256 IS NULL OR "
            "(char_length(decoded_sha256) = 64 AND decoded_sha256 ~ '^[0-9a-f]{64}$')",
            name="ck_collection_attempts_decoded_sha256",
        ),
        Index("ix_collection_attempts_collection", "collection_id", "attempted_at"),
        Index("ix_collection_attempts_job", "job_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    collection_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("source_collections.id", ondelete="RESTRICT"), nullable=False
    )
    job_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("jobs.id", ondelete="RESTRICT"), nullable=False
    )
    policy_snapshot_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("collection_policy_snapshots.id", ondelete="RESTRICT"),
        nullable=False,
    )
    requested_url: Mapped[str] = mapped_column(Text, nullable=False)
    final_url: Mapped[str | None] = mapped_column(Text)
    redirect_chain: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    attempted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    http_status: Mapped[int | None] = mapped_column()
    declared_content_type: Mapped[str | None] = mapped_column(String(255))
    detected_content_type: Mapped[str | None] = mapped_column(String(255))
    size: Mapped[int | None] = mapped_column(BigInteger)
    sha256: Mapped[str | None] = mapped_column(String(64))
    encoded_size: Mapped[int | None] = mapped_column(BigInteger)
    encoded_sha256: Mapped[str | None] = mapped_column(String(64))
    decoded_size: Mapped[int | None] = mapped_column(BigInteger)
    decoded_sha256: Mapped[str | None] = mapped_column(String(64))
    content_encoding: Mapped[str | None] = mapped_column(String(64))
    allowed_headers: Mapped[dict[str, str]] = mapped_column(JSONB, nullable=False)
    outcome: Mapped[str] = mapped_column(String(32), nullable=False)
    failure_reason: Mapped[str | None] = mapped_column(Text)


class DerivedArtifactRow(Base):
    __tablename__ = "derived_artifacts"
    __table_args__ = (
        CheckConstraint("text_length >= 0", name="ck_derived_artifacts_text_length"),
        Index("ix_derived_artifacts_source", "source_document_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    source_document_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("source_documents.id", ondelete="RESTRICT"), nullable=False
    )
    text_blob_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("blobs.id", ondelete="RESTRICT"), nullable=False
    )
    parser_name: Mapped[str] = mapped_column(String(128), nullable=False)
    parser_version: Mapped[str] = mapped_column(String(64), nullable=False)
    text_length: Mapped[int] = mapped_column(BigInteger, nullable=False)
    publication_metadata: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ClaimRow(Base):
    __tablename__ = "claims"
    __table_args__ = (
        CheckConstraint(f"kind IN ({CLAIM_KIND_VALUES_SQL})", name="ck_claims_kind"),
        CheckConstraint("span_start >= 0 AND span_end > span_start", name="ck_claims_span"),
        CheckConstraint(
            "(local_span_start IS NULL AND local_span_end IS NULL) OR "
            "(local_span_start >= 0 AND local_span_end > local_span_start)",
            name="ck_claims_local_span",
        ),
        Index("ix_claims_subject", "subject_id", "created_at"),
        Index("ix_claims_source", "source_document_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    subject_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("subjects.id", ondelete="RESTRICT"), nullable=False
    )
    edition_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("editions.id", ondelete="RESTRICT"), nullable=False
    )
    group_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("editorial_groups.id", ondelete="RESTRICT"), nullable=False
    )
    source_document_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("source_documents.id", ondelete="RESTRICT"), nullable=False
    )
    derived_artifact_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("derived_artifacts.id", ondelete="RESTRICT"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    span_start: Mapped[int] = mapped_column(BigInteger, nullable=False)
    span_end: Mapped[int] = mapped_column(BigInteger, nullable=False)
    extraction_method: Mapped[str] = mapped_column(String(128), nullable=False)
    extraction_payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    chunk_id: Mapped[str] = mapped_column(String(128), nullable=False)
    local_span_start: Mapped[int | None] = mapped_column(BigInteger)
    local_span_end: Mapped[int | None] = mapped_column(BigInteger)
    model_run_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("model_runs.id", ondelete="RESTRICT")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class RejectedModelProposalRow(Base):
    __tablename__ = "rejected_model_proposals"
    __table_args__ = (
        CheckConstraint(
            "char_length(proposal_hash) = 64 AND proposal_hash ~ '^[0-9a-f]{64}$'",
            name="ck_rejected_model_proposals_hash",
        ),
        Index("ix_rejected_model_proposals_source", "source_document_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    source_document_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("source_documents.id", ondelete="RESTRICT"), nullable=False
    )
    derived_artifact_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("derived_artifacts.id", ondelete="RESTRICT"), nullable=False
    )
    chunk_id: Mapped[str] = mapped_column(String(128), nullable=False)
    category: Mapped[str] = mapped_column(String(64), nullable=False)
    requested_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    proposal_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    model_run_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("model_runs.id", ondelete="RESTRICT")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class IndicatorRow(Base):
    __tablename__ = "indicators"
    __table_args__ = (
        CheckConstraint(f"kind IN ({INDICATOR_KIND_VALUES_SQL})", name="ck_indicators_kind"),
        CheckConstraint("span_start >= 0 AND span_end > span_start", name="ck_indicators_span"),
        Index("ix_indicators_subject", "subject_id", "created_at"),
        Index("ix_indicators_source", "source_document_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    subject_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("subjects.id", ondelete="RESTRICT"), nullable=False
    )
    edition_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("editions.id", ondelete="RESTRICT"), nullable=False
    )
    group_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("editorial_groups.id", ondelete="RESTRICT"), nullable=False
    )
    source_document_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("source_documents.id", ondelete="RESTRICT"), nullable=False
    )
    derived_artifact_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("derived_artifacts.id", ondelete="RESTRICT"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    original_value: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_value: Mapped[str] = mapped_column(Text, nullable=False)
    span_start: Mapped[int] = mapped_column(BigInteger, nullable=False)
    span_end: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
