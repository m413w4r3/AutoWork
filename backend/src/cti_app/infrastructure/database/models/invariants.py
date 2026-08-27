from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base

INVARIANT_TYPES_SQL = (
    "'literal_string', 'hex_pattern', 'code_ngram', 'opcode_sequence', 'import_name', "
    "'export_name', 'section_name', 'capability', 'similarity_hash', "
    "'structural_metadata', 'relation'"
)
INVARIANT_CATEGORIES_SQL = (
    "'c2_indicator', 'mutex_or_event', 'pdb_or_build_path', 'config_marker', "
    "'crypto_constant', 'custom_protocol', 'ransom_or_ui_text', 'code_sequence', "
    "'capability_pattern', 'similarity_key', 'library_noise', 'packer_artifact', "
    "'compiler_artifact', 'generic_winapi', 'unknown'"
)
INVARIANT_STATUSES_SQL = (
    "'proposed', 'approved_for_pivot', 'validated', 'rejected', 'unselective', 'shared_component'"
)
INVARIANT_PROVENANCE_KINDS_SQL = (
    "'sample_feature', 'code_feature', 'tool_output', 'capability', 'report_claim', "
    "'analyst_manual'"
)
INVARIANT_REJECTION_CAUSES_SQL = (
    "'provenance_invalid', 'invalid_category', 'library_noise', 'packer_artifact', "
    "'compiler_artifact', 'generic_winapi', 'banal', 'multi_family', 'empty_pattern', "
    "'pattern_too_long', 'code_ngram_mask_ratio', 'code_ngram_contiguous_fixed_run'"
)


class CandidateInvariantRow(Base):
    __tablename__ = "candidate_invariants"
    __table_args__ = (
        UniqueConstraint("proposal_key", name="uq_candidate_invariants_proposal_key"),
        CheckConstraint(
            f"type IN ({INVARIANT_TYPES_SQL})", name="ck_candidate_invariants_type"
        ),
        CheckConstraint(
            f"category IN ({INVARIANT_CATEGORIES_SQL})", name="ck_candidate_invariants_category"
        ),
        CheckConstraint(
            f"status IN ({INVARIANT_STATUSES_SQL})", name="ck_candidate_invariants_status"
        ),
        CheckConstraint(
            "char_length(proposal_key) = 64 AND proposal_key ~ '^[0-9a-f]{64}$'",
            name="ck_candidate_invariants_proposal_key",
        ),
        CheckConstraint(
            "banality_occurrence_count IS NULL OR banality_occurrence_count > 0",
            name="ck_candidate_invariants_banality_count",
        ),
        CheckConstraint(
            "benign_prevalence IS NULL OR benign_prevalence >= 0",
            name="ck_candidate_invariants_benign_prevalence",
        ),
        CheckConstraint(
            "positive_support IS NULL OR positive_support >= 0",
            name="ck_candidate_invariants_positive_support",
        ),
        Index("ix_candidate_invariants_investigation", "investigation_id"),
        Index("ix_candidate_invariants_status", "status"),
        Index("ix_candidate_invariants_type", "type"),
        Index("ix_candidate_invariants_category", "category"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    investigation_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("analyst_investigations.id", ondelete="RESTRICT"),
        nullable=False,
    )
    type: Mapped[str] = mapped_column("type", String(32), nullable=False)
    category: Mapped[str] = mapped_column(String(32), nullable=False)
    pattern: Mapped[str] = mapped_column(Text, nullable=False)
    proposal_key: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    banality_verdict: Mapped[str] = mapped_column(String(32), nullable=False)
    banality_occurrence_count: Mapped[int | None] = mapped_column(BigInteger)
    goodware_baseline_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("goodware_baselines.id", ondelete="RESTRICT")
    )
    corpus_verdict: Mapped[str] = mapped_column(String(32), nullable=False)
    corpus_malware_sample_count: Mapped[int | None] = mapped_column(BigInteger)
    family_labels: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    benign_prevalence: Mapped[int | None] = mapped_column(BigInteger)
    positive_support: Mapped[int | None] = mapped_column(BigInteger)
    positive_sample_confirmed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    masked_pattern: Mapped[str | None] = mapped_column(Text)
    byte_count: Mapped[int | None] = mapped_column(Integer)
    fixed_byte_count: Mapped[int | None] = mapped_column(Integer)
    masked_byte_count: Mapped[int | None] = mapped_column(Integer)
    longest_fixed_run: Mapped[int | None] = mapped_column(Integer)
    likely_packed: Mapped[bool | None] = mapped_column(Boolean)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class CandidateInvariantProvenanceRow(Base):
    __tablename__ = "candidate_invariant_provenances"
    __table_args__ = (
        CheckConstraint(
            f"kind IN ({INVARIANT_PROVENANCE_KINDS_SQL})",
            name="ck_candidate_invariant_provenances_kind",
        ),
        Index("ix_candidate_invariant_provenances_invariant", "invariant_id"),
        Index("ix_candidate_invariant_provenances_sample_sha256", "sample_sha256"),
        Index("ix_candidate_invariant_provenances_kind", "kind"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    invariant_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("candidate_invariants.id", ondelete="RESTRICT"),
        nullable=False,
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    sample_sha256: Mapped[str | None] = mapped_column(String(64))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class CandidateInvariantTransitionRow(Base):
    __tablename__ = "candidate_invariant_transitions"
    __table_args__ = (
        CheckConstraint(
            f"from_status IN ({INVARIANT_STATUSES_SQL})",
            name="ck_candidate_invariant_transitions_from_status",
        ),
        CheckConstraint(
            f"to_status IN ({INVARIANT_STATUSES_SQL})",
            name="ck_candidate_invariant_transitions_to_status",
        ),
        CheckConstraint(
            "char_length(reason) <= 500", name="ck_candidate_invariant_transitions_reason"
        ),
        Index("ix_candidate_invariant_transitions_invariant", "invariant_id", "occurred_at"),
        Index("ix_candidate_invariant_transitions_status", "to_status"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    invariant_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("candidate_invariants.id", ondelete="RESTRICT"),
        nullable=False,
    )
    from_status: Mapped[str] = mapped_column(String(32), nullable=False)
    to_status: Mapped[str] = mapped_column(String(32), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(255), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    reason: Mapped[str] = mapped_column(String(500), nullable=False)


class InvariantRejectionRow(Base):
    __tablename__ = "invariant_rejections"
    __table_args__ = (
        UniqueConstraint("proposal_key", name="uq_invariant_rejections_proposal_key"),
        CheckConstraint(
            f"cause IN ({INVARIANT_REJECTION_CAUSES_SQL})",
            name="ck_invariant_rejections_cause",
        ),
        CheckConstraint(
            "char_length(proposal_key) = 64 AND proposal_key ~ '^[0-9a-f]{64}$'",
            name="ck_invariant_rejections_proposal_key",
        ),
        CheckConstraint("char_length(reason) <= 500", name="ck_invariant_rejections_reason"),
        CheckConstraint(
            "cycle_number IS NULL OR cycle_number >= 1",
            name="ck_invariant_rejections_cycle",
        ),
        Index("ix_invariant_rejections_investigation", "investigation_id", "cycle_number"),
        Index("ix_invariant_rejections_cause", "cause"),
        Index("ix_invariant_rejections_type", "type"),
        Index("ix_invariant_rejections_category", "category"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    investigation_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("analyst_investigations.id", ondelete="RESTRICT"),
        nullable=False,
    )
    cycle_number: Mapped[int | None] = mapped_column(Integer)
    cause: Mapped[str] = mapped_column(String(64), nullable=False)
    type: Mapped[str] = mapped_column("type", String(32), nullable=False)
    category: Mapped[str] = mapped_column(String(32), nullable=False)
    pattern: Mapped[str] = mapped_column(Text, nullable=False)
    proposal_key: Mapped[str] = mapped_column(String(64), nullable=False)
    reason: Mapped[str] = mapped_column(String(500), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
