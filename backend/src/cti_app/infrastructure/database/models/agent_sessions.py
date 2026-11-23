"""PostgreSQL rows for agentic operator sessions (M24-05B1, ADR-0011 D2/D7).

``agent_sessions`` is the sole authority for a session's technical state
(``state`` + ``state_version``); it never replaces a task's business status or
its lease.  The partial unique index enforces one active session per task, and
the composite ``(id, investigation_id, task_id)`` key lets the instruction
queue point at a session that provably belongs to the same task and
investigation.

The session secret is stored as a 32-byte nonce plus ``SHA-256(secret)``;
the secret itself never appears in this schema.  ``agent_session_instructions``
is the durable mid-session queue: a busy agent leaves rows ``queued`` until a
delivery attempt reserves them (``delivering``/``delivery_unknown`` with a
token and exact text digest) and a proven receipt makes them ``delivered``.
Migration 0027 owns the delivery columns and the partial drain index.
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
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base

AGENT_SESSION_STATE_VALUES_SQL = (
    "'provisioning', 'ready', 'running', 'awaiting_instruction', "
    "'interrupt_requested', 'stopped', 'lost', 'reconciling', 'closed', 'failed'"
)
ACTIVE_AGENT_SESSION_STATE_VALUES_SQL = (
    "'provisioning', 'ready', 'running', 'awaiting_instruction', "
    "'interrupt_requested', 'reconciling'"
)
OPERATOR_ADAPTER_KIND_VALUES_SQL = "'codex_cli', 'claude_code'"
INSTRUCTION_STATE_VALUES_SQL = (
    "'queued', 'delivering', 'delivered', 'acknowledged', 'delivery_unknown', 'cancelled'"
)
INSTRUCTION_PENDING_STATE_VALUES_SQL = "'queued', 'delivering', 'delivery_unknown'"
INSTRUCTION_DELIVERY_MODE_VALUES_SQL = "'live', 'next_run'"
INSTRUCTION_ACTOR_KIND_VALUES_SQL = "'human', 'operator'"

_SHA256_SQL = "char_length({column}) = 64 AND {column} ~ '^[0-9a-f]{{64}}$'"


def sha256_check(column: str) -> str:
    return _SHA256_SQL.format(column=column)


class AgentSessionRow(Base):
    """One technical operator session, bound to one investigation task."""

    __tablename__ = "agent_sessions"
    __table_args__ = (
        UniqueConstraint("task_id", "idempotency_key", name="uq_agent_sessions_task_idempotency"),
        UniqueConstraint("host_session_ref", name="uq_agent_sessions_host_session_ref"),
        UniqueConstraint(
            "id",
            "investigation_id",
            "task_id",
            name="uq_agent_sessions_identity",
        ),
        ForeignKeyConstraint(
            ["task_id", "investigation_id"],
            ["investigation_tasks.id", "investigation_tasks.investigation_id"],
            name="fk_agent_sessions_task",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            f"adapter_kind IN ({OPERATOR_ADAPTER_KIND_VALUES_SQL})",
            name="ck_agent_sessions_adapter_kind",
        ),
        CheckConstraint(
            f"state IN ({AGENT_SESSION_STATE_VALUES_SQL})",
            name="ck_agent_sessions_state",
        ),
        CheckConstraint("state_version >= 1", name="ck_agent_sessions_state_version"),
        CheckConstraint(
            "jsonb_typeof(capability_probe) = 'object'",
            name="ck_agent_sessions_capability_probe_object",
        ),
        CheckConstraint(
            sha256_check("capability_sha256"),
            name="ck_agent_sessions_capability_sha256",
        ),
        CheckConstraint(
            "octet_length(auth_secret_nonce) = 32",
            name="ck_agent_sessions_auth_secret_nonce",
        ),
        CheckConstraint(
            sha256_check("auth_secret_hash"),
            name="ck_agent_sessions_auth_secret_hash",
        ),
        CheckConstraint(
            "(task_lease_id IS NULL) = (task_lease_fence IS NULL)",
            name="ck_agent_sessions_task_lease_pair",
        ),
        CheckConstraint(
            "task_lease_fence IS NULL OR task_lease_fence >= 1",
            name="ck_agent_sessions_task_lease_fence",
        ),
        CheckConstraint(
            "(checkpoint_ref IS NULL) = (checkpoint_sha256 IS NULL)",
            name="ck_agent_sessions_checkpoint_pair",
        ),
        CheckConstraint(
            "checkpoint_sha256 IS NULL OR (" + sha256_check("checkpoint_sha256") + ")",
            name="ck_agent_sessions_checkpoint_sha256",
        ),
        CheckConstraint(
            "(reconciliation IS NULL) = (reconciliation_sha256 IS NULL)",
            name="ck_agent_sessions_reconciliation_pair",
        ),
        CheckConstraint(
            "reconciliation IS NULL OR jsonb_typeof(reconciliation) = 'object'",
            name="ck_agent_sessions_reconciliation_object",
        ),
        CheckConstraint(
            "reconciliation_sha256 IS NULL OR (" + sha256_check("reconciliation_sha256") + ")",
            name="ck_agent_sessions_reconciliation_sha256",
        ),
        CheckConstraint(
            "(state IN ('closed', 'failed')) = (closed_at IS NOT NULL)",
            name="ck_agent_sessions_closed_at",
        ),
        CheckConstraint(
            "close_reason IS NULL OR state IN ('closed', 'failed', 'lost')",
            name="ck_agent_sessions_close_reason",
        ),
        Index(
            "uq_agent_sessions_active_task",
            "task_id",
            unique=True,
            postgresql_where=text(f"state IN ({ACTIVE_AGENT_SESSION_STATE_VALUES_SQL})"),
        ),
        Index(
            "ix_agent_sessions_investigation_created",
            "investigation_id",
            "created_at",
            "id",
        ),
        Index("ix_agent_sessions_task_created", "task_id", "created_at", "id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    investigation_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey(
            "analyst_investigations.id",
            name="fk_agent_sessions_investigation",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    task_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    adapter_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    host_session_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    state_version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    capability_probe: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    capability_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    probed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    handoff_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey(
            "investigation_handoffs.id",
            name="fk_agent_sessions_handoff",
            ondelete="RESTRICT",
        ),
        nullable=True,
    )
    task_lease_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    task_lease_fence: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    checkpoint_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    checkpoint_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reconciliation: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB(none_as_null=True), nullable=True
    )
    reconciliation_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    auth_secret_nonce: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    auth_secret_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    auth_secret_issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_activity_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    close_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    idempotency_key: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class AgentSessionInstructionRow(Base):
    """One durably queued human instruction for one session (D05)."""

    __tablename__ = "agent_session_instructions"
    __table_args__ = (
        UniqueConstraint(
            "session_id",
            "idempotency_key",
            name="uq_agent_session_instructions_session_idempotency",
        ),
        ForeignKeyConstraint(
            ["task_id", "investigation_id"],
            ["investigation_tasks.id", "investigation_tasks.investigation_id"],
            name="fk_agent_session_instructions_task",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["session_id", "investigation_id", "task_id"],
            [
                "agent_sessions.id",
                "agent_sessions.investigation_id",
                "agent_sessions.task_id",
            ],
            name="fk_agent_session_instructions_session",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            f"state IN ({INSTRUCTION_STATE_VALUES_SQL})",
            name="ck_agent_session_instructions_state",
        ),
        CheckConstraint(
            f"created_by_actor_kind IN ({INSTRUCTION_ACTOR_KIND_VALUES_SQL})",
            name="ck_agent_session_instructions_actor_kind",
        ),
        CheckConstraint(
            "octet_length(body) >= 1 AND octet_length(body) <= 65536",
            name="ck_agent_session_instructions_body_size",
        ),
        CheckConstraint(
            sha256_check("body_sha256"),
            name="ck_agent_session_instructions_body_sha256",
        ),
        CheckConstraint(
            "(state IN ('delivered', 'acknowledged')) = (delivered_at IS NOT NULL)",
            name="ck_agent_session_instructions_delivered_at",
        ),
        CheckConstraint(
            "(state = 'acknowledged') = (acknowledged_at IS NOT NULL)",
            name="ck_agent_session_instructions_acknowledged",
        ),
        CheckConstraint(
            "delivery_attempts >= 0",
            name="ck_agent_session_instructions_delivery_attempts",
        ),
        # F3-D §3.3/§3.4: an in-flight attempt always carries its token and the
        # exact delivered-text digest; queued/cancelled rows carry neither.  A
        # proven row may keep the token but pre-0027 rows have none.
        CheckConstraint(
            "(state NOT IN ('delivering', 'delivery_unknown') "
            "OR delivery_token IS NOT NULL) AND "
            "(delivery_token IS NULL OR state NOT IN ('queued', 'cancelled'))",
            name="ck_agent_session_instructions_delivery_token",
        ),
        CheckConstraint(
            f"(delivery_mode IS NULL OR delivery_mode IN ({INSTRUCTION_DELIVERY_MODE_VALUES_SQL})) "
            "AND (delivery_mode IS NULL OR state IN ('delivered', 'acknowledged'))",
            name="ck_agent_session_instructions_delivery_mode",
        ),
        CheckConstraint(
            "(delivery_sha256 IS NULL OR (" + sha256_check("delivery_sha256") + ")) AND "
            "(delivery_sha256 IS NULL OR state IN "
            "('delivering', 'delivery_unknown', 'delivered', 'acknowledged')) AND "
            "(state NOT IN ('delivering', 'delivery_unknown') "
            "OR delivery_sha256 IS NOT NULL)",
            name="ck_agent_session_instructions_delivery_sha256",
        ),
        CheckConstraint(
            "delivery_note_diffs IS NULL OR "
            "(jsonb_typeof(delivery_note_diffs) = 'array' "
            "AND state IN ('delivered', 'acknowledged'))",
            name="ck_agent_session_instructions_delivery_note_diffs",
        ),
        CheckConstraint(
            "(state = 'delivery_unknown') = (delivery_unknown_at IS NOT NULL)",
            name="ck_agent_session_instructions_delivery_unknown_at",
        ),
        CheckConstraint(
            "(state = 'cancelled') = (cancelled_at IS NOT NULL)",
            name="ck_agent_session_instructions_cancelled_at",
        ),
        CheckConstraint(
            "(cancel_reason IS NOT NULL) = (state = 'cancelled')",
            name="ck_agent_session_instructions_cancel_reason",
        ),
        CheckConstraint(
            "last_delivery_error IS NULL OR state NOT IN ('delivered', 'acknowledged')",
            name="ck_agent_session_instructions_last_delivery_error",
        ),
        Index(
            "ix_agent_session_instructions_session_queue",
            "session_id",
            "queued_at",
            "id",
        ),
        Index(
            "ix_agent_session_instructions_pending",
            "state",
            "queued_at",
            "id",
            postgresql_where=text(f"state IN ({INSTRUCTION_PENDING_STATE_VALUES_SQL})"),
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    investigation_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    task_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    session_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(Text, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    body_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    created_by_actor_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    created_by_actor_id: Mapped[str] = mapped_column(Text, nullable=False)
    queued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    delivery_attempts: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0")
    )
    delivery_token: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    delivery_mode: Mapped[str | None] = mapped_column(Text, nullable=True)
    delivery_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    delivery_note_diffs: Mapped[list[Any] | None] = mapped_column(
        JSONB(none_as_null=True), nullable=True
    )
    delivery_attempted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    delivery_unknown_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_delivery_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancel_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


__all__ = [
    "ACTIVE_AGENT_SESSION_STATE_VALUES_SQL",
    "AGENT_SESSION_STATE_VALUES_SQL",
    "INSTRUCTION_ACTOR_KIND_VALUES_SQL",
    "INSTRUCTION_DELIVERY_MODE_VALUES_SQL",
    "INSTRUCTION_PENDING_STATE_VALUES_SQL",
    "INSTRUCTION_STATE_VALUES_SQL",
    "OPERATOR_ADAPTER_KIND_VALUES_SQL",
    "AgentSessionInstructionRow",
    "AgentSessionRow",
    "sha256_check",
]
