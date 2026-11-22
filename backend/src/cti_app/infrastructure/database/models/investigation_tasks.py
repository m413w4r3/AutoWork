"""PostgreSQL rows for durable investigation tasks (M24-04B1, ADR-0011 D2).

``investigation_tasks`` is the sole authority for a task's business status,
verification status and lease.  ``investigation_task_dependencies`` and
``investigation_task_events`` are append-only: migration 0021 installs
``BEFORE UPDATE OR DELETE`` and ``BEFORE TRUNCATE`` triggers, so a mutation is a
database error rather than a convention.  ``event_sequence`` is written only by
``append_task_event``; no ORM update in this module touches it.

The composite ``(id, investigation_id)`` unique key exists so dependencies and
events can reference a task and its investigation in one foreign key: a
cross-investigation edge is impossible at the database level.
"""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    PrimaryKeyConstraint,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base

TASK_STATUS_VALUES_SQL = (
    "'proposed', 'ready', 'assigned', 'running', 'awaiting_input', "
    "'awaiting_review', 'blocked', 'completed', 'cancelled', 'failed'"
)
TASK_PRIORITY_VALUES_SQL = "'critical', 'high', 'normal', 'low'"
TASK_RESULT_VERIFICATION_VALUES_SQL = "'unverified', 'pending_review', 'accepted', 'rejected'"
TASK_BLOCKED_REASON_VALUES_SQL = (
    "'dependency', 'capability', 'approval', 'session_lost', 'lease_expired', "
    "'input_unavailable', 'external'"
)
TASK_ASSIGNED_ACTOR_KIND_VALUES_SQL = "'human', 'operator'"
TASK_ACTOR_KIND_VALUES_SQL = "'human', 'operator', 'system'"
TASK_EVENT_TYPE_VALUES_SQL = (
    "'task_created', 'status_changed', 'assigned', 'lease_acquired', "
    "'lease_renewed', 'lease_released', 'lease_expired', 'result_submitted', "
    "'verification_recorded', 'session_started', 'session_state_changed', "
    "'handoff_created', 'contribution_imported'"
)

_LEASE_FIELDS_NULL_SQL = (
    "lease_id IS NULL AND lease_session_id IS NULL "
    "AND lease_expires_at IS NULL AND heartbeat_at IS NULL"
)
_LEASE_FIELDS_COMPLETE_SQL = (
    "lease_id IS NOT NULL AND lease_session_id IS NOT NULL "
    "AND lease_expires_at IS NOT NULL AND heartbeat_at IS NOT NULL "
    "AND lease_fence >= 1"
)


class InvestigationTaskRow(Base):
    """One investigation task; the mutable business-status authority."""

    __tablename__ = "investigation_tasks"
    __table_args__ = (
        UniqueConstraint(
            "investigation_id",
            "task_key",
            name="uq_investigation_tasks_investigation_task_key",
        ),
        UniqueConstraint("id", "investigation_id", name="uq_investigation_tasks_identity"),
        CheckConstraint(
            f"status IN ({TASK_STATUS_VALUES_SQL})", name="ck_investigation_tasks_status"
        ),
        CheckConstraint(
            f"priority IN ({TASK_PRIORITY_VALUES_SQL})",
            name="ck_investigation_tasks_priority",
        ),
        CheckConstraint("status_version >= 1", name="ck_investigation_tasks_status_version"),
        CheckConstraint(
            f"result_verification_status IN ({TASK_RESULT_VERIFICATION_VALUES_SQL})",
            name="ck_investigation_tasks_result_verification",
        ),
        CheckConstraint(
            f"blocked_reason IN ({TASK_BLOCKED_REASON_VALUES_SQL})",
            name="ck_investigation_tasks_blocked_reason",
        ),
        CheckConstraint(
            "(blocked_reason IS NOT NULL) = (status = 'blocked')",
            name="ck_investigation_tasks_blocked_reason_pair",
        ),
        CheckConstraint(
            "(status = 'completed') = (result_verification_status = 'accepted')",
            name="ck_investigation_tasks_completed_accepted",
        ),
        CheckConstraint(
            "(status = 'awaiting_review') = (result_verification_status = 'pending_review')",
            name="ck_investigation_tasks_awaiting_review_pending",
        ),
        CheckConstraint(
            "(assigned_actor_kind IS NULL) = (assigned_actor_id IS NULL)",
            name="ck_investigation_tasks_assignment_pair",
        ),
        CheckConstraint(
            f"assigned_actor_kind IN ({TASK_ASSIGNED_ACTOR_KIND_VALUES_SQL})",
            name="ck_investigation_tasks_assigned_actor_kind",
        ),
        CheckConstraint(
            "assigned_actor_kind <> 'human' OR ("
            "lease_id IS NULL AND lease_session_id IS NULL "
            "AND lease_expires_at IS NULL AND heartbeat_at IS NULL)",
            name="ck_investigation_tasks_human_assignment_no_lease",
        ),
        CheckConstraint(
            f"({_LEASE_FIELDS_NULL_SQL}) OR ({_LEASE_FIELDS_COMPLETE_SQL})",
            name="ck_investigation_tasks_lease_complete",
        ),
        CheckConstraint(
            "status NOT IN ('completed', 'cancelled', 'failed') OR ("
            "lease_id IS NULL AND lease_session_id IS NULL "
            "AND lease_expires_at IS NULL AND heartbeat_at IS NULL)",
            name="ck_investigation_tasks_terminal_lease_null",
        ),
        CheckConstraint(
            "(parent_task_id IS NULL) = (depth = 0)",
            name="ck_investigation_tasks_parent_depth",
        ),
        CheckConstraint(
            f"created_by_actor_kind IN ({TASK_ACTOR_KIND_VALUES_SQL})",
            name="ck_investigation_tasks_created_by_actor_kind",
        ),
        CheckConstraint("event_sequence >= 0", name="ck_investigation_tasks_event_sequence"),
        Index(
            "ix_investigation_tasks_investigation_status_priority",
            "investigation_id",
            "status",
            "priority",
            "created_at",
        ),
        Index(
            "ix_investigation_tasks_status_lease_expires",
            "status",
            "lease_expires_at",
        ),
        Index(
            "ix_investigation_tasks_assignment",
            "assigned_actor_kind",
            "assigned_actor_id",
            "status",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    investigation_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey(
            "analyst_investigations.id",
            name="fk_investigation_tasks_investigation",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    task_key: Mapped[str] = mapped_column(Text, nullable=False)
    plan_version_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey(
            "investigation_plan_versions.id",
            name="fk_investigation_tasks_plan_version",
            ondelete="RESTRICT",
        ),
        nullable=True,
    )
    parent_task_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey(
            "investigation_tasks.id",
            name="fk_investigation_tasks_parent",
            ondelete="RESTRICT",
        ),
        nullable=True,
    )
    depth: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    objective: Mapped[str] = mapped_column(Text, nullable=False)
    priority: Mapped[str] = mapped_column(String(16), nullable=False)
    claim_refs: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    evidence_refs: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    sample_refs: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    required_capabilities: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    allowed_workspace_paths: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    execution_policy: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    expected_outputs: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    status_version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    result_verification_status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=text("'unverified'")
    )
    blocked_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    assigned_actor_kind: Mapped[str | None] = mapped_column(String(16), nullable=True)
    assigned_actor_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    lease_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    lease_session_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    lease_fence: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # The foreign key to ``investigation_handoffs`` is added by 0022: the
    # handoff table does not exist yet when 0021 runs.
    handoff_ref: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey(
            "investigation_handoffs.id",
            name="fk_investigation_tasks_handoff",
            ondelete="RESTRICT",
            use_alter=True,
        ),
        nullable=True,
    )
    result_refs: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    event_sequence: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0")
    )
    created_by_actor_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    created_by_actor_id: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class InvestigationTaskDependencyRow(Base):
    """Append-only ``task depends on task`` edge of one investigation."""

    __tablename__ = "investigation_task_dependencies"
    __table_args__ = (
        PrimaryKeyConstraint(
            "task_id", "depends_on_task_id", name="pk_investigation_task_dependencies"
        ),
        ForeignKeyConstraint(
            ["task_id", "investigation_id"],
            ["investigation_tasks.id", "investigation_tasks.investigation_id"],
            name="fk_investigation_task_dependencies_task",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["depends_on_task_id", "investigation_id"],
            ["investigation_tasks.id", "investigation_tasks.investigation_id"],
            name="fk_investigation_task_dependencies_depends_on",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "task_id <> depends_on_task_id",
            name="ck_investigation_task_dependencies_not_self",
        ),
    )

    task_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    depends_on_task_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    investigation_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)


class InvestigationTaskEventRow(Base):
    """Append-only business-event journal; sequence is per task."""

    __tablename__ = "investigation_task_events"
    __table_args__ = (
        UniqueConstraint("task_id", "sequence", name="uq_investigation_task_events_task_sequence"),
        UniqueConstraint(
            "task_id",
            "idempotency_key",
            name="uq_investigation_task_events_task_idempotency",
        ),
        ForeignKeyConstraint(
            ["task_id", "investigation_id"],
            ["investigation_tasks.id", "investigation_tasks.investigation_id"],
            name="fk_investigation_task_events_task",
            ondelete="RESTRICT",
        ),
        CheckConstraint("sequence >= 1", name="ck_investigation_task_events_sequence"),
        CheckConstraint(
            f"event_type IN ({TASK_EVENT_TYPE_VALUES_SQL})",
            name="ck_investigation_task_events_event_type",
        ),
        CheckConstraint("status_version >= 1", name="ck_investigation_task_events_status_version"),
        CheckConstraint(
            f"actor_kind IN ({TASK_ACTOR_KIND_VALUES_SQL})",
            name="ck_investigation_task_events_actor_kind",
        ),
        CheckConstraint(
            "char_length(request_sha256) = 64 AND request_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_investigation_task_events_request_sha256",
        ),
        Index(
            "ix_investigation_task_events_investigation_timeline",
            "investigation_id",
            "occurred_at",
            "id",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    investigation_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    task_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    from_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    to_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    status_version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    lease_fence: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    actor_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    actor_id: Mapped[str] = mapped_column(Text, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(Text, nullable=False)
    request_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    analyst_decision_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey(
            "analyst_decisions.id",
            name="fk_investigation_task_events_analyst_decision",
            ondelete="RESTRICT",
        ),
        nullable=True,
    )
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
