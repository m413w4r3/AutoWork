"""Versioned OSINT_OPEN policy, audit-event and inheritance rows."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as Uuid
from sqlalchemy.orm import Mapped, mapped_column

from cti_app.infrastructure.database.models.base import Base


class OsintOpenPolicyRow(Base):
    __tablename__ = "osint_open_policies"
    __table_args__ = (
        UniqueConstraint(
            "scope_kind", "scope_ref", "version", name="uq_osint_open_policies_scope_version"
        ),
        CheckConstraint("version >= 1", name="ck_osint_open_policies_version"),
        CheckConstraint(
            "previous_version IS NULL OR previous_version = version - 1",
            name="ck_osint_open_policies_previous_version",
        ),
        CheckConstraint(
            "scope_kind IN ('instance', 'subject_set')", name="ck_osint_open_policies_scope_kind"
        ),
        CheckConstraint("mode = 'osint_open'", name="ck_osint_open_policies_mode"),
        CheckConstraint("status IN ('active', 'revoked')", name="ck_osint_open_policies_status"),
        CheckConstraint(
            "jsonb_typeof(allowed_providers) = 'array'",
            name="ck_osint_open_policies_providers_array",
        ),
        CheckConstraint(
            "jsonb_typeof(budget_limits) = 'object'", name="ck_osint_open_policies_budgets_object"
        ),
        CheckConstraint(
            "disclosure_tlp IN ('white', 'clear', 'green', 'amber', 'red')",
            name="ck_osint_open_policies_tlp",
        ),
        CheckConstraint(
            "max_effect_claims_per_investigation >= 1", name="ck_osint_open_policies_effect_claims"
        ),
        CheckConstraint(
            "correlation_id <> '' AND activation_correlation_id <> ''",
            name="ck_osint_open_policies_correlations",
        ),
        ForeignKeyConstraint(
            ["policy_id", "previous_version"],
            ["osint_open_policies.policy_id", "osint_open_policies.version"],
            name="fk_osint_open_policies_previous",
            ondelete="RESTRICT",
            use_alter=True,
        ),
    )

    policy_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    version: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    scope_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    scope_ref: Mapped[str] = mapped_column(String(4096), nullable=False)
    mode: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    allowed_providers: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    budget_limits: Mapped[dict[str, int]] = mapped_column(JSONB, nullable=False)
    acquire_hits_allowed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    disclosure_tlp: Mapped[str] = mapped_column(String(16), nullable=False)
    disclosure_do_not_submit: Mapped[bool] = mapped_column(Boolean, nullable=False)
    disclosure_external_llm_allowed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    max_effect_claims_per_investigation: Mapped[int] = mapped_column(BigInteger, nullable=False)
    actor_id: Mapped[str] = mapped_column(String(255), nullable=False)
    reason: Mapped[str] = mapped_column(String(2000), nullable=False)
    correlation_id: Mapped[str] = mapped_column(String(255), nullable=False)
    activation_correlation_id: Mapped[str] = mapped_column(String(255), nullable=False)
    previous_version: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class OsintOpenPolicyEventRow(Base):
    __tablename__ = "osint_open_policy_events"
    __table_args__ = (
        UniqueConstraint("correlation_id", name="uq_osint_open_policy_events_correlation"),
        UniqueConstraint("policy_id", "policy_version", name="uq_osint_open_policy_events_version"),
        UniqueConstraint(
            "policy_id",
            "policy_version",
            "id",
            name="uq_osint_open_policy_events_version_id",
        ),
        CheckConstraint(
            "event_type IN ('activated', 'extended', 'revoked')",
            name="ck_osint_open_policy_events_type",
        ),
        CheckConstraint(
            "previous_version IS NULL OR previous_version >= 1",
            name="ck_osint_open_policy_events_previous_version",
        ),
        CheckConstraint(
            "payload_sha256 ~ '^[0-9a-f]{64}$'", name="ck_osint_open_policy_events_payload_sha256"
        ),
        ForeignKeyConstraint(
            ["policy_id", "policy_version"],
            ["osint_open_policies.policy_id", "osint_open_policies.version"],
            name="fk_osint_open_policy_events_policy_version",
            ondelete="RESTRICT",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    policy_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    policy_version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    event_type: Mapped[str] = mapped_column(String(16), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(255), nullable=False)
    reason: Mapped[str] = mapped_column(String(2000), nullable=False)
    correlation_id: Mapped[str] = mapped_column(String(255), nullable=False)
    payload_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    previous_version: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class OsintOpenInheritanceRow(Base):
    __tablename__ = "osint_open_inheritances"
    __table_args__ = (
        UniqueConstraint("correlation_id", name="uq_osint_open_inheritances_correlation"),
        CheckConstraint("policy_version >= 1", name="ck_osint_open_inheritances_policy_version"),
        CheckConstraint(
            "status IN ('active', 'revoked', 'superseded')",
            name="ck_osint_open_inheritances_status",
        ),
        ForeignKeyConstraint(
            ["investigation_id"],
            ["analyst_investigations.id"],
            name="fk_osint_open_inheritances_investigation",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["subject_id"],
            ["subjects.id"],
            name="fk_osint_open_inheritances_subject",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["policy_id", "policy_version"],
            ["osint_open_policies.policy_id", "osint_open_policies.version"],
            name="fk_osint_open_inheritances_policy_version",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["policy_id", "policy_version", "policy_event_id"],
            [
                "osint_open_policy_events.policy_id",
                "osint_open_policy_events.policy_version",
                "osint_open_policy_events.id",
            ],
            name="fk_osint_open_inheritances_policy_event",
            ondelete="RESTRICT",
        ),
    )

    investigation_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    subject_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    policy_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    policy_version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    policy_event_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    correlation_id: Mapped[str] = mapped_column(String(255), nullable=False)
    activation_correlation_id: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


__all__ = ["OsintOpenInheritanceRow", "OsintOpenPolicyEventRow", "OsintOpenPolicyRow"]
