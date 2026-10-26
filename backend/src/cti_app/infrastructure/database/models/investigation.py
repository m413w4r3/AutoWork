"""PostgreSQL rows for durable investigation evidence and editorial decisions."""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from . import osint_open_policy as _osint_open_policy  # noqa: F401
from .base import Base

TOOL_INVOCATION_STATUS_VALUES_SQL = (
    "'pending', 'running', 'succeeded', 'empty', 'unavailable', 'policy_denied', "
    "'needs_review', 'failed', 'partial'"
)
EVIDENCE_KIND_VALUES_SQL = (
    "'vt_file_report', 'vt_relationship', 'vt_behaviour', 'shodan_observation', "
    "'binary_analysis', 'binary_string', 'yara_match', 'negative_result', 'claim_source', 'other'"
)
FINDING_KIND_VALUES_SQL = (
    "'new_sample', 'infection_chain', 'capability', 'network_behaviour', "
    "'claim_verification', 'detection'"
)
FINDING_ASSESSMENT_VALUES_SQL = "'supported', 'refuted', 'qualified', 'inconclusive'"
NOVELTY_CLASS_VALUES_SQL = (
    "'new_to_article', 'already_documented_but_new_detail', 'claim_correction', 'not_confirmed'"
)
SAMPLE_ORIGIN_VALUES_SQL = "'seed', 'vt_pivot', 'relationship', 'behavior', 'manual'"
SAMPLE_QUALIFICATION_VALUES_SQL = "'candidate', 'related', 'confirmed', 'rejected', 'uncertain'"
QUALIFICATION_ACTOR_KIND_VALUES_SQL = "'human', 'agent', 'system'"
DETECTION_CANDIDATE_STATUS_VALUES_SQL = (
    "'candidate', 'tested', 'rejected', 'approved', 'experimental'"
)
BUDGET_DIMENSION_VALUES_SQL = (
    "'bridge_turns', 'vt_units', 'shodan_credits', 'downloads', 'tool_seconds', "
    "'context_tokens', 'context_bytes'"
)
BUDGET_RESERVATION_STATUS_VALUES_SQL = "'reserved', 'committed', 'released'"
ANALYST_OPINION_STATUS_VALUES_SQL = "'draft', 'approved', 'rejected'"
INVESTIGATION_STATE_STOP_REASON_VALUES_SQL = (
    "'agent_finished', 'budget_exhausted', 'no_new_information', 'user_interrupt', "
    "'model_unavailable', 'permission_required', 'submission_needs_review'"
)


class ToolInvocationRow(Base):
    __tablename__ = "tool_invocations"
    __table_args__ = (
        UniqueConstraint("operation_key", name="uq_tool_invocations_operation_key"),
        CheckConstraint(
            f"status IN ({TOOL_INVOCATION_STATUS_VALUES_SQL})",
            name="ck_tool_invocations_status",
        ),
        CheckConstraint("version >= 1", name="ck_tool_invocations_version"),
        CheckConstraint(
            "jsonb_typeof(normalized_parameters) = 'object'",
            name="ck_tool_invocations_parameters_object",
        ),
        CheckConstraint(
            "jsonb_typeof(reserved_budget) = 'object' AND jsonb_typeof(consumed_budget) = 'object'",
            name="ck_tool_invocations_budget_objects",
        ),
        CheckConstraint(
            "jsonb_typeof(response_refs) = 'array'",
            name="ck_tool_invocations_response_refs_array",
        ),
        Index("ix_tool_invocations_investigation", "investigation_id", "created_at", "id"),
        Index("ix_tool_invocations_status", "status", "updated_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    investigation_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("analyst_investigations.id", ondelete="RESTRICT"),
        nullable=False,
    )
    operation_key: Mapped[str] = mapped_column(String(255), nullable=False)
    tool_name: Mapped[str] = mapped_column(String(128), nullable=False)
    schema_version: Mapped[str] = mapped_column(String(64), nullable=False)
    normalized_parameters: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    purpose: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    reserved_budget: Mapped[dict[str, int]] = mapped_column(JSONB, nullable=False)
    consumed_budget: Mapped[dict[str, int]] = mapped_column(JSONB, nullable=False)
    response_refs: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    version: Mapped[int] = mapped_column(BigInteger, nullable=False)


class EvidenceObservationRow(Base):
    __tablename__ = "evidence_observations"
    __table_args__ = (
        UniqueConstraint(
            "investigation_id",
            "sha256",
            "kind",
            "object_type",
            "object_id",
            name="uq_evidence_observations_identity",
        ),
        CheckConstraint(
            f"kind IN ({EVIDENCE_KIND_VALUES_SQL})", name="ck_evidence_observations_kind"
        ),
        CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="ck_evidence_observations_sha256"),
        CheckConstraint(
            "jsonb_typeof(limitations) = 'array'",
            name="ck_evidence_observations_limitations_array",
        ),
        Index(
            "ix_evidence_observations_investigation",
            "investigation_id",
            "observed_at",
            "id",
        ),
        Index("ix_evidence_observations_blob", "blob_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    investigation_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("analyst_investigations.id", ondelete="RESTRICT"),
        nullable=False,
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    blob_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("blobs.id", ondelete="RESTRICT"), nullable=False
    )
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    source_version: Mapped[str] = mapped_column(String(128), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    object_type: Mapped[str] = mapped_column(String(128), nullable=False)
    object_id: Mapped[str] = mapped_column(Text, nullable=False)
    limitations: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    invocation_ref: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tool_invocations.id", ondelete="RESTRICT")
    )


class InvestigationFindingVersionRow(Base):
    __tablename__ = "investigation_finding_versions"
    __table_args__ = (
        UniqueConstraint("id", "investigation_id", "kind", name="uq_finding_version_identity"),
        ForeignKeyConstraint(
            ["supersedes_id", "investigation_id", "kind"],
            [
                "investigation_finding_versions.id",
                "investigation_finding_versions.investigation_id",
                "investigation_finding_versions.kind",
            ],
            name="fk_finding_version_supersedes",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("supersedes_id", name="uq_finding_version_supersedes"),
        CheckConstraint(f"kind IN ({FINDING_KIND_VALUES_SQL})", name="ck_finding_versions_kind"),
        CheckConstraint(
            f"assessment IN ({FINDING_ASSESSMENT_VALUES_SQL})",
            name="ck_finding_versions_assessment",
        ),
        CheckConstraint(
            f"novelty IS NULL OR novelty IN ({NOVELTY_CLASS_VALUES_SQL})",
            name="ck_finding_versions_novelty",
        ),
        CheckConstraint(
            "(version = 1 AND supersedes_id IS NULL) OR "
            "(version > 1 AND supersedes_id IS NOT NULL)",
            name="ck_finding_versions_lineage",
        ),
        CheckConstraint(
            "assessment <> 'supported' OR jsonb_array_length(observation_ids) > 0",
            name="ck_finding_versions_supported_evidence",
        ),
        CheckConstraint(
            "jsonb_typeof(observation_ids) = 'array' AND "
            "jsonb_typeof(claim_refs) = 'array' AND jsonb_typeof(limitations) = 'array'",
            name="ck_finding_versions_metadata_arrays",
        ),
        Index("ix_finding_versions_investigation", "investigation_id", "created_at", "id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    investigation_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("analyst_investigations.id", ondelete="RESTRICT"),
        nullable=False,
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    assessment: Mapped[str] = mapped_column(String(32), nullable=False)
    statement: Mapped[str] = mapped_column(Text, nullable=False)
    observation_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    claim_refs: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    novelty: Mapped[str | None] = mapped_column(String(64))
    scope: Mapped[str] = mapped_column(Text, nullable=False)
    limitations: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    supersedes_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class InvestigationExperimentHeadRow(Base):
    __tablename__ = "investigation_experiment_heads"
    __table_args__ = (
        CheckConstraint("version >= 0", name="ck_investigation_experiment_heads_version"),
    )

    investigation_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("analyst_investigations.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class InvestigationExperimentRow(Base):
    __tablename__ = "investigation_experiments"
    __table_args__ = (
        UniqueConstraint(
            "investigation_id", "version", name="uq_investigation_experiments_version"
        ),
        CheckConstraint("version >= 1", name="ck_investigation_experiments_version"),
        CheckConstraint(
            "schema_version = 'investigation-experiment.v1'",
            name="ck_investigation_experiments_schema_version",
        ),
        CheckConstraint(
            "payload->>'schema_version' = schema_version AND "
            "payload->>'id' = id::text AND "
            "payload->>'investigation_id' = investigation_id::text AND "
            "payload->>'version' = version::text",
            name="ck_investigation_experiments_payload_identity",
        ),
        CheckConstraint(
            "jsonb_typeof(payload) = 'object' AND jsonb_typeof(claim_ids) = 'array'",
            name="ck_investigation_experiments_payload_shape",
        ),
        CheckConstraint(
            "payload_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_investigation_experiments_payload_sha256",
        ),
        Index(
            "ix_investigation_experiments_claim_timeline",
            "investigation_id",
            "hypothesis_id",
            "version",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    investigation_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("analyst_investigations.id", ondelete="RESTRICT"),
        nullable=False,
    )
    version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    hypothesis_id: Mapped[str] = mapped_column(String(128), nullable=False)
    claim_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    schema_version: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    payload_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class InvestigationFindingObservationRow(Base):
    __tablename__ = "investigation_finding_observations"
    __table_args__ = (
        ForeignKeyConstraint(
            ["finding_id"],
            ["investigation_finding_versions.id"],
            name="fk_finding_observations_finding",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["observation_id"],
            ["evidence_observations.id"],
            name="fk_finding_observations_evidence",
            ondelete="RESTRICT",
        ),
        Index("ix_finding_observations_observation", "observation_id"),
    )

    finding_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    position: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    observation_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)


class InvestigationFindingClaimRow(Base):
    __tablename__ = "investigation_finding_claims"
    __table_args__ = (
        ForeignKeyConstraint(
            ["finding_id"],
            ["investigation_finding_versions.id"],
            name="fk_finding_claims_finding",
            ondelete="RESTRICT",
        ),
    )

    finding_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    position: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    claim_ref: Mapped[str] = mapped_column(Text, nullable=False)


class SampleAssociationRow(Base):
    __tablename__ = "sample_associations"
    __table_args__ = (
        CheckConstraint("sample_sha256 ~ '^[0-9a-f]{64}$'", name="ck_sample_associations_sha256"),
        CheckConstraint(
            f"origin IN ({SAMPLE_ORIGIN_VALUES_SQL})", name="ck_sample_associations_origin"
        ),
        Index("ix_sample_associations_investigation", "investigation_id", "sample_sha256"),
        Index("ix_sample_associations_sample", "sample_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    investigation_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("analyst_investigations.id", ondelete="RESTRICT"),
        nullable=False,
    )
    sample_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("samples.id", ondelete="RESTRICT")
    )
    sample_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    origin: Mapped[str] = mapped_column(String(32), nullable=False)


class SampleAssociationVersionRow(Base):
    __tablename__ = "sample_association_versions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["association_id"],
            ["sample_associations.id"],
            name="fk_sample_association_versions_identity",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            f"qualification IN ({SAMPLE_QUALIFICATION_VALUES_SQL})",
            name="ck_sample_association_versions_qualification",
        ),
        CheckConstraint("version >= 1", name="ck_sample_association_versions_version"),
        CheckConstraint(
            "qualification <> 'confirmed' OR version >= 2",
            name="ck_sample_association_versions_confirmed_history",
        ),
        CheckConstraint(
            "jsonb_typeof(related_sample_sha256) = 'array'",
            name="ck_sample_association_versions_related_array",
        ),
        Index("ix_sample_association_versions_latest", "association_id", "version"),
    )

    association_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    version: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    qualification: Mapped[str] = mapped_column(String(32), nullable=False)
    related_sample_sha256: Mapped[list[str]] = mapped_column(JSONB, nullable=False)


class QualificationChangeRow(Base):
    __tablename__ = "sample_association_qualification_changes"
    __table_args__ = (
        ForeignKeyConstraint(
            ["association_id", "version"],
            ["sample_association_versions.association_id", "sample_association_versions.version"],
            name="fk_qualification_changes_version",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            f"from_qualification IN ({SAMPLE_QUALIFICATION_VALUES_SQL}) AND "
            f"to_qualification IN ({SAMPLE_QUALIFICATION_VALUES_SQL})",
            name="ck_qualification_changes_qualification",
        ),
        CheckConstraint(
            f"actor_kind IN ({QUALIFICATION_ACTOR_KIND_VALUES_SQL})",
            name="ck_qualification_changes_actor_kind",
        ),
        CheckConstraint(
            "to_qualification <> 'confirmed' OR (actor_kind = 'human' AND btrim(actor_id) <> '')",
            name="ck_qualification_changes_human_confirmation",
        ),
        Index("ix_qualification_changes_association", "association_id", "version"),
    )

    association_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    version: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    from_qualification: Mapped[str] = mapped_column(String(32), nullable=False)
    to_qualification: Mapped[str] = mapped_column(String(32), nullable=False)
    actor_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(255), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class DetectionCandidateVersionRow(Base):
    __tablename__ = "detection_candidate_versions"
    __table_args__ = (
        UniqueConstraint("candidate_id", "version", name="uq_detection_candidate_version"),
        UniqueConstraint(
            "id", "candidate_id", "investigation_id", name="uq_detection_version_identity"
        ),
        ForeignKeyConstraint(
            ["parent_version_id", "candidate_id", "investigation_id"],
            [
                "detection_candidate_versions.id",
                "detection_candidate_versions.candidate_id",
                "detection_candidate_versions.investigation_id",
            ],
            name="fk_detection_version_parent",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("parent_version_id", name="uq_detection_version_parent"),
        CheckConstraint(
            f"status IN ({DETECTION_CANDIDATE_STATUS_VALUES_SQL})",
            name="ck_detection_candidate_status",
        ),
        CheckConstraint(
            "(version = 1 AND parent_version_id IS NULL) OR "
            "(version > 1 AND parent_version_id IS NOT NULL)",
            name="ck_detection_candidate_lineage",
        ),
        CheckConstraint("source_sha256 ~ '^[0-9a-f]{64}$'", name="ck_detection_candidate_sha256"),
        Index("ix_detection_candidates_investigation", "investigation_id", "created_at"),
        Index("ix_detection_candidates_candidate", "candidate_id", "version"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    candidate_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    investigation_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("analyst_investigations.id", ondelete="RESTRICT"),
        nullable=False,
    )
    source: Mapped[str] = mapped_column(Text, nullable=False)
    source_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    parent_version_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class DetectionCandidateMotifRow(Base):
    __tablename__ = "detection_candidate_motifs"
    __table_args__ = (
        ForeignKeyConstraint(
            ["version_id"],
            ["detection_candidate_versions.id"],
            name="fk_detection_motifs_version",
            ondelete="RESTRICT",
        ),
    )

    version_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    position: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    motif: Mapped[str] = mapped_column(Text, nullable=False)
    rationale: Mapped[str] = mapped_column(Text, nullable=False)


class DetectionCandidateMotifObservationRow(Base):
    __tablename__ = "detection_candidate_motif_observations"
    __table_args__ = (
        ForeignKeyConstraint(
            ["version_id", "motif_position"],
            ["detection_candidate_motifs.version_id", "detection_candidate_motifs.position"],
            name="fk_detection_motif_observations_motif",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["observation_id"],
            ["evidence_observations.id"],
            name="fk_detection_motif_observations_evidence",
            ondelete="RESTRICT",
        ),
        Index("ix_detection_motif_observations_observation", "observation_id"),
    )

    version_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    motif_position: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    observation_position: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    observation_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)


class DetectionCandidateTestRefRow(Base):
    __tablename__ = "detection_candidate_test_refs"
    __table_args__ = (
        ForeignKeyConstraint(
            ["version_id"],
            ["detection_candidate_versions.id"],
            name="fk_detection_test_refs_version",
            ondelete="RESTRICT",
        ),
    )

    version_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    position: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    test_ref: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)


class AnalystOpinionVersionRow(Base):
    __tablename__ = "analyst_opinion_versions"
    __table_args__ = (
        CheckConstraint("version >= 1", name="ck_analyst_opinion_versions_version"),
        CheckConstraint(
            f"status IN ({ANALYST_OPINION_STATUS_VALUES_SQL})",
            name="ck_analyst_opinion_versions_status",
        ),
        CheckConstraint(
            "content_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_analyst_opinion_versions_content_sha256",
        ),
        CheckConstraint(
            "jsonb_typeof(content) = 'object'", name="ck_analyst_opinion_versions_content_object"
        ),
        Index(
            "ix_analyst_opinion_versions_investigation",
            "investigation_id",
            "opinion_id",
            "version",
        ),
    )

    opinion_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    version: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    investigation_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("analyst_investigations.id", ondelete="RESTRICT"),
        nullable=False,
    )
    content: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class InvestigationEnvelopeRow(Base):
    __tablename__ = "investigation_envelopes"
    __table_args__ = (
        CheckConstraint("version >= 1", name="ck_investigation_envelopes_version"),
        CheckConstraint(
            "jsonb_typeof(allowed_providers) = 'array'",
            name="ck_investigation_envelopes_providers_array",
        ),
        CheckConstraint(
            "jsonb_typeof(budget_snapshot) = 'object'",
            name="ck_investigation_envelopes_budget_object",
        ),
        CheckConstraint(
            "disclosure_tlp IS NULL OR disclosure_tlp IN "
            "('white', 'clear', 'green', 'amber', 'red')",
            name="ck_investigation_envelopes_tlp",
        ),
        CheckConstraint(
            "(authority_kind IS NULL AND inherited_policy_id IS NULL "
            "AND inherited_policy_version IS NULL AND inherited_policy_event_id IS NULL) OR "
            "(authority_kind = 'human_g2' AND inherited_policy_id IS NULL "
            "AND inherited_policy_version IS NULL AND inherited_policy_event_id IS NULL) OR "
            "(authority_kind = 'osint_policy' AND inherited_policy_id IS NOT NULL "
            "AND inherited_policy_version IS NOT NULL AND inherited_policy_event_id IS NOT NULL "
            "AND approval_decision_id = inherited_policy_event_id)",
            name="ck_investigation_envelopes_authority_lineage",
        ),
        ForeignKeyConstraint(
            ["inherited_policy_id", "inherited_policy_version"],
            ["osint_open_policies.policy_id", "osint_open_policies.version"],
            name="fk_investigation_envelopes_inherited_policy",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            [
                "inherited_policy_id",
                "inherited_policy_version",
                "inherited_policy_event_id",
            ],
            [
                "osint_open_policy_events.policy_id",
                "osint_open_policy_events.policy_version",
                "osint_open_policy_events.id",
            ],
            name="fk_investigation_envelopes_inherited_event",
            ondelete="RESTRICT",
        ),
        Index("ix_investigation_envelopes_updated", "updated_at"),
    )

    investigation_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("analyst_investigations.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    allowed_providers: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    budget_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    acquire_hits_allowed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    disclosure_tlp: Mapped[str | None] = mapped_column(String(16))
    disclosure_do_not_submit: Mapped[bool] = mapped_column(Boolean, nullable=False)
    disclosure_external_llm_allowed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    approval_decision_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    approval_actor_id: Mapped[str] = mapped_column(String(255), nullable=False)
    authority_kind: Mapped[str | None] = mapped_column(String(32))
    inherited_policy_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    inherited_policy_version: Mapped[int | None] = mapped_column(BigInteger)
    inherited_policy_event_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    approved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    version: Mapped[int] = mapped_column(BigInteger, nullable=False)


class InvestigationBudgetRecordRow(Base):
    __tablename__ = "investigation_budget_records"
    __table_args__ = (
        CheckConstraint("version >= 1", name="ck_investigation_budget_records_version"),
        Index("ix_investigation_budget_records_updated", "updated_at"),
    )

    investigation_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("analyst_investigations.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class InvestigationBudgetLimitRow(Base):
    __tablename__ = "investigation_budget_limits"
    __table_args__ = (
        ForeignKeyConstraint(
            ["investigation_id"],
            ["investigation_budget_records.investigation_id"],
            name="fk_budget_limits_record",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            f"dimension IN ({BUDGET_DIMENSION_VALUES_SQL})", name="ck_budget_limits_dimension"
        ),
        CheckConstraint("amount >= 0", name="ck_budget_limits_amount"),
    )

    investigation_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    dimension: Mapped[str] = mapped_column(String(32), primary_key=True)
    amount: Mapped[int] = mapped_column(BigInteger, nullable=False)


class InvestigationBudgetReservationRow(Base):
    __tablename__ = "investigation_budget_reservations"
    __table_args__ = (
        ForeignKeyConstraint(
            ["investigation_id"],
            ["investigation_budget_records.investigation_id"],
            name="fk_budget_reservations_record",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("investigation_id", "sequence", name="uq_budget_reservations_sequence"),
        CheckConstraint(
            f"status IN ({BUDGET_RESERVATION_STATUS_VALUES_SQL})",
            name="ck_budget_reservations_status",
        ),
        CheckConstraint("sequence >= 1", name="ck_budget_reservations_sequence"),
        Index("ix_budget_reservations_status", "investigation_id", "status"),
    )

    investigation_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    operation_key: Mapped[str] = mapped_column(String(255), primary_key=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)


class InvestigationBudgetReservationAmountRow(Base):
    __tablename__ = "investigation_budget_reservation_amounts"
    __table_args__ = (
        ForeignKeyConstraint(
            ["investigation_id", "operation_key"],
            [
                "investigation_budget_reservations.investigation_id",
                "investigation_budget_reservations.operation_key",
            ],
            name="fk_budget_reservation_amounts_reservation",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            f"dimension IN ({BUDGET_DIMENSION_VALUES_SQL})",
            name="ck_budget_reservation_amounts_dimension",
        ),
        CheckConstraint(
            "(reserved_amount IS NULL OR reserved_amount >= 0) AND "
            "(committed_amount IS NULL OR committed_amount >= 0) AND "
            "(reserved_amount IS NOT NULL OR committed_amount IS NOT NULL) AND "
            "(committed_amount IS NULL OR committed_amount <= coalesce(reserved_amount, 0))",
            name="ck_budget_reservation_amounts_values",
        ),
    )

    investigation_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    operation_key: Mapped[str] = mapped_column(String(255), primary_key=True)
    dimension: Mapped[str] = mapped_column(String(32), primary_key=True)
    reserved_amount: Mapped[int | None] = mapped_column(BigInteger)
    committed_amount: Mapped[int | None] = mapped_column(BigInteger)


class InvestigationWorkingStateRow(Base):
    __tablename__ = "investigation_working_states"
    __table_args__ = (
        CheckConstraint("version >= 1", name="ck_investigation_working_states_version"),
        CheckConstraint("turn_index >= 0", name="ck_investigation_working_states_turn_index"),
        CheckConstraint(
            f"stop_reason IS NULL OR stop_reason IN ({INVESTIGATION_STATE_STOP_REASON_VALUES_SQL})",
            name="ck_investigation_working_states_stop_reason",
        ),
        CheckConstraint(
            "jsonb_typeof(state) = 'object' AND "
            "jsonb_typeof(state->'agenda') = 'array' AND "
            "jsonb_typeof(state->'hypotheses') = 'array' AND "
            "state->>'schema' = 'investigation-state.v1'",
            name="ck_investigation_working_states_payload_shape",
        ),
        CheckConstraint(
            "state->>'investigation_id' = investigation_id::text AND "
            "state->>'version' = version::text AND "
            "state->>'turn_index' = turn_index::text AND "
            "(state->>'stop_reason') IS NOT DISTINCT FROM stop_reason",
            name="ck_investigation_working_states_payload_matches_columns",
        ),
        CheckConstraint(
            "NOT jsonb_path_exists(state, "
            '\'$.hypotheses[*] ? (@.status != "open" && '
            '@.status != "supported" && @.status != "refuted" && '
            '@.status != "abandoned")\')',
            name="ck_investigation_working_states_hypothesis_status",
        ),
        Index("ix_investigation_working_states_stop_updated", "stop_reason", "updated_at"),
    )

    investigation_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("analyst_investigations.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    state: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    turn_index: Mapped[int] = mapped_column(BigInteger, nullable=False)
    stop_reason: Mapped[str | None] = mapped_column(String(32))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
