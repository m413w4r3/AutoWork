"""ORM rows for consumed external effects and their invocation lineage."""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    CHAR,
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    PrimaryKeyConstraint,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from cti_app.domain.external_effects import EffectClaimState, ExternalEffectUseKind

from .base import Base

_CLAIM_STATES_SQL = ", ".join(f"'{value.value}'" for value in EffectClaimState)
_USE_KINDS_SQL = ", ".join(f"'{value.value}'" for value in ExternalEffectUseKind)


class ExternalEffectClaimRow(Base):
    __tablename__ = "external_effect_claim"
    __table_args__ = (
        PrimaryKeyConstraint("id", name="pk_external_effect_claim"),
        UniqueConstraint(
            "investigation_id", "effect_key", name="uq_external_effect_claim_identity"
        ),
        CheckConstraint(
            "effect_key ~ '^[0-9a-f]{64}$' AND canonical_request_sha256 ~ '^[0-9a-f]{64}$' "
            "AND application_request_sha256 ~ '^[0-9a-f]{64}$' "
            "AND occurrence_sha256 ~ '^[0-9a-f]{64}$' "
            "AND (invocation_arguments_sha256 IS NULL OR "
            "invocation_arguments_sha256 ~ '^[0-9a-f]{64}$')",
            name="ck_external_effect_claim_hashes",
        ),
        CheckConstraint(f"state IN ({_CLAIM_STATES_SQL})", name="ck_external_effect_claim_state"),
        CheckConstraint("version >= 1", name="ck_external_effect_claim_version"),
        CheckConstraint("envelope_version >= 1", name="ck_external_effect_claim_envelope_version"),
        CheckConstraint(
            "(canonical_request IS NOT NULL OR request_ref IS NOT NULL) "
            "AND (canonical_request IS NULL OR jsonb_typeof(canonical_request) = 'object')",
            name="ck_external_effect_claim_request_shape",
        ),
        CheckConstraint(
            "jsonb_typeof(evidence_refs) = 'array' AND "
            "jsonb_typeof(reservation_amounts) = 'object'",
            name="ck_external_effect_claim_json_shape",
        ),
        CheckConstraint(
            "(bridge_turn_id IS NULL) = (model_run_id IS NULL)",
            name="ck_external_effect_claim_bridge_pair",
        ),
        CheckConstraint(
            "invocation_id IS NOT NULL OR bridge_turn_id IS NOT NULL "
            "OR acquisition_attempt_id IS NOT NULL OR retrohunt_reservation_id IS NOT NULL",
            name="ck_external_effect_claim_source",
        ),
        Index("ix_external_effect_claim_invocation_created", "invocation_id", "created_at"),
        Index("ix_external_effect_claim_bridge_turn", "bridge_turn_id"),
        Index("ix_external_effect_claim_acquisition_attempt", "acquisition_attempt_id"),
        Index("ix_external_effect_claim_state_updated", "state", "updated_at"),
        Index(
            "ix_external_effect_claim_family_provider_created", "family", "provider", "created_at"
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    investigation_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey(
            "analyst_investigations.id",
            name="fk_external_effect_claim_investigation",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    invocation_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey(
            "tool_invocations.id", name="fk_external_effect_claim_invocation", ondelete="RESTRICT"
        ),
    )
    bridge_turn_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey(
            "model_conversation_turns.id",
            name="fk_external_effect_claim_bridge_turn",
            ondelete="RESTRICT",
        ),
    )
    model_run_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("model_runs.id", name="fk_external_effect_claim_model_run", ondelete="RESTRICT"),
    )
    acquisition_attempt_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey(
            "sample_acquisition_attempts.id",
            name="fk_external_effect_claim_acquisition_attempt",
            ondelete="RESTRICT",
        ),
    )
    retrohunt_reservation_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey(
            "retrohunt_reservations.id",
            name="fk_external_effect_claim_retrohunt_reservation",
            ondelete="RESTRICT",
        ),
    )
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    provider_scope: Mapped[str] = mapped_column(String(255), nullable=False)
    family: Mapped[str] = mapped_column(String(128), nullable=False)
    identity_version: Mapped[str] = mapped_column(String(32), nullable=False)
    normalizer_version: Mapped[str] = mapped_column(String(64), nullable=False)
    effect_key: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    canonical_request_sha256: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    application_request_sha256: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    occurrence_sha256: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    occurrence_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    request_ref: Mapped[str | None] = mapped_column(Text)
    canonical_request: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    evidence_refs: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    operation_key: Mapped[str] = mapped_column(String(255), nullable=False)
    reservation_amounts: Mapped[dict[str, int]] = mapped_column(JSONB, nullable=False)
    envelope_version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    approval_decision_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    invocation_arguments_sha256: Mapped[str | None] = mapped_column(CHAR(64))
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    handoff_permit_nonce: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(String(64))


class ExternalEffectUseRow(Base):
    __tablename__ = "external_effect_use"
    __table_args__ = (
        PrimaryKeyConstraint("id", name="pk_external_effect_use"),
        UniqueConstraint(
            "claim_id", "invocation_id", name="uq_external_effect_use_claim_invocation"
        ),
        CheckConstraint(f"use_kind IN ({_USE_KINDS_SQL})", name="ck_external_effect_use_kind"),
        CheckConstraint(
            "jsonb_typeof(observation_refs) = 'array' AND jsonb_typeof(budget_delta) = 'object'",
            name="ck_external_effect_use_json_shape",
        ),
        CheckConstraint(
            "use_kind <> 'REUSED' OR NOT jsonb_path_exists(budget_delta, '$.* ? (@ != 0)')",
            name="ck_external_effect_use_reused_zero_budget",
        ),
        Index("ix_external_effect_use_invocation_created", "invocation_id", "created_at"),
        Index("ix_external_effect_use_claim_created", "claim_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    claim_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey(
            "external_effect_claim.id",
            name="fk_external_effect_use_claim",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    invocation_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey(
            "tool_invocations.id",
            name="fk_external_effect_use_invocation",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    use_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    observation_refs: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    budget_delta: Mapped[dict[str, int]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ExternalEffectAcquisitionDispatchRow(Base):
    """Link an S3 physical request claim to its durable logical pre-send attempt."""

    __tablename__ = "effect_acquisition_dispatch_links"
    __table_args__ = (
        PrimaryKeyConstraint("claim_id", name="pk_effect_acquisition_dispatch_link"),
        Index("ix_effect_acquisition_dispatch_attempt", "dispatch_attempt_id"),
    )

    claim_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey(
            "external_effect_claim.id",
            name="fk_effect_acquisition_dispatch_claim",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    dispatch_attempt_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey(
            "sample_acquisition_dispatch_attempts.id",
            name="fk_effect_acquisition_dispatch_attempt",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )


__all__ = [
    "ExternalEffectAcquisitionDispatchRow",
    "ExternalEffectClaimRow",
    "ExternalEffectUseRow",
]
