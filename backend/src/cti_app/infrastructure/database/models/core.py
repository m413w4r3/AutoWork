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
from .classification import TLP_VALUES_SQL


class BlobRow(Base):
    __tablename__ = "blobs"
    __table_args__ = (
        UniqueConstraint("logical_bucket", "sha256", name="uq_blobs_bucket_sha256"),
        UniqueConstraint("object_key", name="uq_blobs_object_key"),
        CheckConstraint("size >= 0", name="ck_blobs_size_non_negative"),
        CheckConstraint("char_length(sha256) = 64", name="ck_blobs_sha256_length"),
        CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="ck_blobs_sha256_format"),
        CheckConstraint(
            "logical_bucket ~ '^[a-z0-9][a-z0-9._-]{0,62}$'",
            name="ck_blobs_logical_bucket_format",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    mime_type: Mapped[str] = mapped_column(String(255), nullable=False)
    logical_bucket: Mapped[str] = mapped_column(String(63), nullable=False)
    object_key: Mapped[str] = mapped_column(String(160), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class GoodwareBaselineRow(Base):
    __tablename__ = "goodware_baselines"
    __table_args__ = (
        UniqueConstraint(
            "baseline_fingerprint_sha256", name="uq_goodware_baselines_fingerprint"
        ),
    )
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    baseline_fingerprint_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    source_set_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    normalization_version: Mapped[str] = mapped_column(String(64), nullable=False)
    record_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    occurrence_sum: Mapped[int] = mapped_column(BigInteger, nullable=False)
    pattern_version: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class GoodwareBaselineSourceRow(Base):
    __tablename__ = "goodware_baseline_sources"
    __table_args__ = (
        UniqueConstraint("baseline_id", "filename", name="uq_goodware_baseline_sources_filename"),
    )
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    baseline_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("goodware_baselines.id", ondelete="RESTRICT"), nullable=False
    )
    filename: Mapped[str] = mapped_column(Text, nullable=False)
    feature_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    blob_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("blobs.id", ondelete="RESTRICT"), nullable=False
    )


class GoodwareBaselineIndexRow(Base):
    __tablename__ = "goodware_baseline_indexes"
    __table_args__ = (
        UniqueConstraint(
            "baseline_id",
            "index_format_version",
            "key_version",
            name="uq_goodware_baseline_indexes_version",
        ),
        Index("ix_goodware_baseline_indexes_index_blob_id", "index_blob_id"),
        Index("ix_goodware_baseline_indexes_manifest_blob_id", "manifest_blob_id"),
    )
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    baseline_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("goodware_baselines.id", ondelete="RESTRICT"), nullable=False
    )
    schema_version: Mapped[str] = mapped_column(String(64), nullable=False)
    key_version: Mapped[str] = mapped_column(String(64), nullable=False)
    index_format_version: Mapped[str] = mapped_column(String(64), nullable=False)
    index_blob_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("blobs.id", ondelete="RESTRICT"), nullable=False
    )
    manifest_blob_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("blobs.id", ondelete="RESTRICT"), nullable=False
    )


class InvestigationGoodwareBaselineRow(Base):
    __tablename__ = "investigation_goodware_baselines"
    investigation_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("analyst_investigations.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    baseline_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("goodware_baselines.id", ondelete="RESTRICT"), nullable=False
    )


class ReferenceMemberRow(Base):
    __tablename__ = "reference_members"
    __table_args__ = (
        UniqueConstraint("sample_id", "family_label", name="uq_reference_members_sample_label"),
        CheckConstraint(
            "label_source IN ('ANALYST','OPERATOR_IMPORT')", name="ck_reference_members_source"
        ),
        Index("ix_reference_members_sample_id", "sample_id"),
    )
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    sample_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("samples.id", ondelete="RESTRICT"), nullable=False
    )
    sample_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    family_label: Mapped[str] = mapped_column(Text, nullable=False)
    origin_investigation_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("analyst_investigations.id", ondelete="RESTRICT")
    )
    promoted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(255), nullable=False)
    label_source: Mapped[str] = mapped_column(String(32), nullable=False)


class ReferenceMemberDisputeRow(Base):
    __tablename__ = "reference_member_disputes"
    member_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("reference_members.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    actor_id: Mapped[str] = mapped_column(String(255), nullable=False)


class CapabilitySetRow(Base):
    __tablename__ = "capability_sets"
    __table_args__ = (
        UniqueConstraint(
            "sample_id",
            "tool_version",
            "ruleset_sha256",
            "parameters_sha256",
            name="uq_capability_sets_replay",
        ),
        Index("ix_capability_sets_blob_id", "blob_id"),
    )
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    sample_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("samples.id", ondelete="RESTRICT"), nullable=False
    )
    blob_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("blobs.id", ondelete="RESTRICT"), nullable=False
    )
    tool_name: Mapped[str] = mapped_column(String(32), nullable=False)
    tool_version: Mapped[str] = mapped_column(String(64), nullable=False)
    ruleset_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    parameters_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    capabilities: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    errors: Mapped[list[str]] = mapped_column(JSONB, nullable=False)


class CodeFeatureSetRow(Base):
    __tablename__ = "code_feature_sets"
    __table_args__ = (
        UniqueConstraint(
            "sample_id",
            "tool_version",
            "escaper_compatibility_version",
            "intel_pic_hash_escape_version",
            "parameters_sha256",
            name="uq_code_feature_sets_replay",
        ),
        Index("ix_code_feature_sets_blob_id", "blob_id"),
        Index("ix_code_feature_sets_feature_blob_id", "feature_blob_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    sample_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("samples.id", ondelete="RESTRICT"), nullable=False
    )
    blob_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("blobs.id", ondelete="RESTRICT"), nullable=False
    )
    feature_blob_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("blobs.id", ondelete="RESTRICT"), nullable=False
    )
    tool_version: Mapped[str] = mapped_column(String(64), nullable=False)
    escaper_compatibility_version: Mapped[str] = mapped_column(String(64), nullable=False)
    intel_pic_hash_escape_version: Mapped[str] = mapped_column(String(64), nullable=False)
    parameters_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    architecture: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    errors: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class SubjectRow(Base):
    __tablename__ = "subjects"
    __table_args__ = (
        CheckConstraint(f"tlp IN ({TLP_VALUES_SQL})", name="ck_subjects_tlp"),
        CheckConstraint("slug ~ '^[a-z0-9]+(-[a-z0-9]+)*$'", name="ck_subjects_slug_format"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    external_id: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    slug: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    tlp: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class SourceDocumentRow(Base):
    __tablename__ = "source_documents"
    __table_args__ = (
        CheckConstraint(f"tlp IN ({TLP_VALUES_SQL})", name="ck_source_documents_tlp"),
        Index("ix_source_documents_subject_id", "subject_id"),
        Index("ix_source_documents_blob_id", "blob_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    subject_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("subjects.id", ondelete="RESTRICT"), nullable=False
    )
    blob_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("blobs.id", ondelete="RESTRICT"), nullable=False
    )
    original_name: Mapped[str] = mapped_column(Text, nullable=False)
    origin: Mapped[str] = mapped_column(Text, nullable=False)
    acquired_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    license_restriction: Mapped[str | None] = mapped_column(Text)
    tlp: Mapped[str] = mapped_column(String(16), nullable=False)
    do_not_submit: Mapped[bool] = mapped_column(Boolean, nullable=False)
    external_llm_allowed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    logical_filename: Mapped[str | None] = mapped_column(Text)
    source_collection_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    source_candidate_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    decoded_blob_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("blobs.id", ondelete="RESTRICT")
    )
    title: Mapped[str | None] = mapped_column(Text)
    publisher: Mapped[str | None] = mapped_column(Text)
    published_at: Mapped[date | None] = mapped_column(Date)
    final_url: Mapped[str | None] = mapped_column(Text)
    declared_mime_type: Mapped[str | None] = mapped_column(String(255))
    detected_mime_type: Mapped[str | None] = mapped_column(String(255))
    encoded_sha256: Mapped[str | None] = mapped_column(String(64))
    decoded_sha256: Mapped[str | None] = mapped_column(String(64))
    encoded_size: Mapped[int | None] = mapped_column(BigInteger)
    decoded_size: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class SampleRow(Base):
    __tablename__ = "samples"
    __table_args__ = (
        UniqueConstraint("subject_id", "blob_id", name="uq_samples_subject_blob"),
        CheckConstraint(f"tlp IN ({TLP_VALUES_SQL})", name="ck_samples_tlp"),
        CheckConstraint(
            "origin_kind IN ('source_seed','vt_seed','vt_hunt_hit','benign_reference','manual')",
            name="ck_samples_origin_kind",
        ),
        CheckConstraint(
            "state IN ('quarantined','review_candidate','validated','rejected')",
            name="ck_samples_state",
        ),
        CheckConstraint(
            "expected_hash IS NULL OR (char_length(expected_hash) IN (32, 40, 64) "
            "AND expected_hash ~ '^[0-9a-f]+$')",
            name="ck_samples_expected_hash",
        ),
        *(
            CheckConstraint(
                f"{name}_source IS NULL OR {name}_source IN ('local','vt')",
                name=f"ck_samples_{name}_source",
            )
            for name in (
                "imphash",
                "ssdeep",
                "tlsh",
                "rich_header_hash",
                "vhash",
                "main_icon_dhash",
            )
        ),
        Index("ix_samples_subject_id", "subject_id"),
        Index("ix_samples_blob_id", "blob_id"),
        Index("ix_samples_imphash", "imphash"),
        Index("ix_samples_ssdeep", "ssdeep"),
        Index("ix_samples_tlsh", "tlsh"),
        Index("ix_samples_rich_header_hash", "rich_header_hash"),
        Index("ix_samples_vhash", "vhash"),
        Index("ix_samples_main_icon_dhash", "main_icon_dhash"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    subject_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("subjects.id", ondelete="RESTRICT"), nullable=False
    )
    blob_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("blobs.id", ondelete="RESTRICT"), nullable=False
    )
    original_name: Mapped[str] = mapped_column(Text, nullable=False)
    origin: Mapped[str] = mapped_column(Text, nullable=False)
    acquired_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    license_restriction: Mapped[str | None] = mapped_column(Text)
    tlp: Mapped[str] = mapped_column(String(16), nullable=False)
    do_not_submit: Mapped[bool] = mapped_column(Boolean, nullable=False)
    external_llm_allowed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    origin_kind: Mapped[str] = mapped_column(String(32), nullable=False, default="source_seed")
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="validated")
    source_service: Mapped[str | None] = mapped_column(Text)
    source_object_id: Mapped[str | None] = mapped_column(Text)
    expected_hash: Mapped[str | None] = mapped_column(String(64))
    validation_actor: Mapped[str | None] = mapped_column(String(255))
    validation_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    validation_reason: Mapped[str | None] = mapped_column(Text)
    imphash: Mapped[str | None] = mapped_column(String(255))
    ssdeep: Mapped[str | None] = mapped_column(String(255))
    tlsh: Mapped[str | None] = mapped_column(String(255))
    rich_header_hash: Mapped[str | None] = mapped_column(String(255))
    vhash: Mapped[str | None] = mapped_column(String(255))
    main_icon_dhash: Mapped[str | None] = mapped_column(String(255))
    imphash_source: Mapped[str | None] = mapped_column(String(8))
    ssdeep_source: Mapped[str | None] = mapped_column(String(8))
    tlsh_source: Mapped[str | None] = mapped_column(String(8))
    rich_header_hash_source: Mapped[str | None] = mapped_column(String(8))
    vhash_source: Mapped[str | None] = mapped_column(String(8))
    main_icon_dhash_source: Mapped[str | None] = mapped_column(String(8))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class SampleFeatureSetRow(Base):
    __tablename__ = "sample_feature_sets"
    __table_args__ = (
        UniqueConstraint(
            "sample_id",
            "extractor_version",
            "parameters_sha256",
            name="uq_sample_feature_sets_replay",
        ),
        Index("ix_sample_feature_sets_blob_id", "blob_id"),
    )
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    sample_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("samples.id", ondelete="RESTRICT"), nullable=False
    )
    blob_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("blobs.id", ondelete="RESTRICT"), nullable=False
    )
    feature_blob_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("blobs.id", ondelete="RESTRICT"), nullable=False
    )
    extractor_version: Mapped[str] = mapped_column(String(64), nullable=False)
    parameters_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class SampleFeatureIndexRow(Base):
    __tablename__ = "sample_feature_index"
    __table_args__ = (
        UniqueConstraint(
            "feature_set_id",
            "feature_kind",
            "normalized_value",
            name="uq_sample_feature_index_value",
        ),
        Index("ix_sample_feature_index_sample_kind", "sample_id", "feature_kind"),
    )
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    sample_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("samples.id", ondelete="RESTRICT"), nullable=False
    )
    feature_set_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("sample_feature_sets.id", ondelete="RESTRICT")
    )
    capability_set_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("capability_sets.id", ondelete="RESTRICT")
    )
    code_feature_set_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("code_feature_sets.id", ondelete="RESTRICT")
    )
    feature_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    normalized_value: Mapped[str] = mapped_column(Text, nullable=False)
    occurrence_count: Mapped[int] = mapped_column(nullable=False)


class ProvenanceEventRow(Base):
    __tablename__ = "provenance_events"
    __table_args__ = (
        CheckConstraint(f"tlp IN ({TLP_VALUES_SQL})", name="ck_provenance_events_tlp"),
        Index(
            "ix_provenance_events_aggregate",
            "aggregate_type",
            "aggregate_id",
            "occurred_at",
        ),
        Index("ix_provenance_events_subject_id", "subject_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    subject_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("subjects.id", ondelete="RESTRICT")
    )
    aggregate_type: Mapped[str] = mapped_column(String(64), nullable=False)
    aggregate_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    event_type: Mapped[str] = mapped_column(String(128), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    tlp: Mapped[str] = mapped_column(String(16), nullable=False)
    actor_id: Mapped[str | None] = mapped_column(String(255))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class VirusTotalObservationRow(Base):
    __tablename__ = "virustotal_observations"
    __table_args__ = (
        CheckConstraint(
            "http_status >= 200 AND http_status < 300", name="ck_vt_observation_http_success"
        ),
        CheckConstraint("raw_size >= 0", name="ck_vt_observation_raw_size"),
        CheckConstraint("observed_count >= 0", name="ck_vt_observation_count"),
        CheckConstraint("page_order >= 0", name="ck_vt_observation_page_order"),
        CheckConstraint(
            "capability IN ("
            "'file_report','file_relationships','intelligence_search',"
            "'file_download','submissions','behaviour_pcap','retrohunt'"
            ")",
            name="ck_vt_observation_capability",
        ),
        CheckConstraint("raw_sha256 ~ '^[0-9a-f]{64}$'", name="ck_vt_observation_raw_sha256"),
        Index("ix_vt_observations_blob_id", "blob_id"),
        Index("ix_vt_observations_subject_id", "subject_id"),
        Index("ix_vt_observations_execution_id", "execution_id"),
    )
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    subject_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("subjects.id", ondelete="RESTRICT")
    )
    operation: Mapped[str] = mapped_column(String(64), nullable=False)
    capability: Mapped[str] = mapped_column(String(64), nullable=False)
    source_identifier: Mapped[str] = mapped_column(Text, nullable=False)
    safe_parameters: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    http_status: Mapped[int] = mapped_column(nullable=False)
    blob_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("blobs.id", ondelete="RESTRICT"), nullable=False
    )
    raw_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    raw_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    input_cursor: Mapped[str | None] = mapped_column(Text)
    output_cursor: Mapped[str | None] = mapped_column(Text)
    observed_count: Mapped[int] = mapped_column(nullable=False)
    exhaustive: Mapped[bool] = mapped_column(Boolean, nullable=False)
    page_order: Mapped[int] = mapped_column(nullable=False)
    normalization_contract_version: Mapped[str] = mapped_column(String(64), nullable=False)
    execution_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)


class VirusTotalFileViewRow(Base):
    __tablename__ = "virustotal_file_views"
    __table_args__ = (UniqueConstraint("observation_id", name="uq_vt_file_views_observation"),)
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    observation_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("virustotal_observations.id", ondelete="RESTRICT"),
        nullable=False,
    )
    vt_file_id: Mapped[str] = mapped_column(String(128), nullable=False)
    file_type: Mapped[str] = mapped_column(String(64), nullable=False)
    lookup_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    meaningful_name: Mapped[str | None] = mapped_column(Text)
    type_description: Mapped[str | None] = mapped_column(Text)
    size: Mapped[int | None] = mapped_column(BigInteger)
    last_analysis_stats: Mapped[dict[str, int] | None] = mapped_column(JSONB)
    first_submission_date: Mapped[int | None] = mapped_column(BigInteger)
    last_submission_date: Mapped[int | None] = mapped_column(BigInteger)
    last_modification_date: Mapped[int | None] = mapped_column(BigInteger)
    tags: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    vhash: Mapped[str | None] = mapped_column(String(255))
    imphash: Mapped[str | None] = mapped_column(String(255))
    ssdeep: Mapped[str | None] = mapped_column(String(255))
    tlsh: Mapped[str | None] = mapped_column(String(255))
    main_icon_dhash: Mapped[str | None] = mapped_column(String(255))
    rich_header_hash: Mapped[str | None] = mapped_column(String(255))
