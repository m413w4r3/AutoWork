"""PostgreSQL rows for agentic operator sessions (M24-05B1, ADR-0011 D2/D7).

``agent_sessions`` is the sole authority for a session's technical state
(``state`` + ``state_version``); it never replaces a task's business status or
its lease.  The partial unique index enforces one active session per task, and
the composite ``(id, investigation_id, task_id)`` key lets the instruction
queue point at a session that provably belongs to the same task and
investigation.

The session secret is stored as a 32-byte nonce plus ``SHA-256(secret)``;
the secret itself never appears in this schema.  ``agent_session_instructions``
is the durable mid-session queue: a busy agent leaves rows ``queued`` until it
acknowledges them with its signed operator route.
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
INSTRUCTION_STATE_VALUES_SQL = "'queued', 'delivered', 'acknowledged', 'cancelled'"
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
            "state <> 'queued' OR delivered_at IS NULL",
            name="ck_agent_session_instructions_queued",
        ),
        CheckConstraint(
            "state NOT IN ('delivered', 'acknowledged') OR delivered_at IS NOT NULL",
            name="ck_agent_session_instructions_delivered",
        ),
        CheckConstraint(
            "(state = 'acknowledged') = (acknowledged_at IS NOT NULL)",
            name="ck_agent_session_instructions_acknowledged",
        ),
        Index(
            "ix_agent_session_instructions_session_queue",
            "session_id",
            "queued_at",
            "id",
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
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


__all__ = [
    "ACTIVE_AGENT_SESSION_STATE_VALUES_SQL",
    "AGENT_SESSION_STATE_VALUES_SQL",
    "INSTRUCTION_ACTOR_KIND_VALUES_SQL",
    "INSTRUCTION_STATE_VALUES_SQL",
    "OPERATOR_ADAPTER_KIND_VALUES_SQL",
    "AgentSessionInstructionRow",
    "AgentSessionRow",
    "sha256_check",
]
