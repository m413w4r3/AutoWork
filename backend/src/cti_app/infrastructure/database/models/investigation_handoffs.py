"""PostgreSQL row for durable investigation handoffs (M24-05A, ADR-0011 D2/D5).

``investigation_handoffs`` is the sole authority for a handoff's content
(ADR-0011 D1): the canonical JSON payload plus its SHA-256, immutable by
``BEFORE UPDATE OR DELETE`` and ``BEFORE TRUNCATE`` triggers installed by
migration 0022.  Versions are per task and idempotency is scoped per task, so
``(task_id, version)`` and ``(task_id, idempotency_key)`` are both unique.

The row also carries the canonical request identity (``request_sha256``) that
lets an idempotent replay with a different body be refused, and the
``predecessor_id`` link to the previous version of the same task.  Bridge is
never an actor: parties and creators are human/operator (plus system for a
server-created handoff).
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
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base

HANDOFF_SCHEMA_VERSION = "investigation-handoff.v1"
HANDOFF_PARTY_ACTOR_KIND_VALUES_SQL = "'human', 'operator'"
HANDOFF_CREATOR_ACTOR_KIND_VALUES_SQL = "'human', 'operator', 'system'"

_CONTENT_IDENTITY_SQL = (
    "content ? 'handoff_id' AND content ? 'investigation_id' AND content ? 'task_id' "
    "AND content ? 'version' AND content ? 'schema_version' "
    "AND content->>'handoff_id' = id::text "
    "AND content->>'investigation_id' = investigation_id::text "
    "AND content->>'task_id' = task_id::text "
    "AND content->>'version' = version::text "
    "AND content->>'schema_version' = schema_version"
)


class InvestigationHandoffRow(Base):
    """One immutable handoff version of one task."""

    __tablename__ = "investigation_handoffs"
    __table_args__ = (
        UniqueConstraint(
            "task_id",
            "version",
            name="uq_investigation_handoffs_task_version",
        ),
        UniqueConstraint(
            "task_id",
            "idempotency_key",
            name="uq_investigation_handoffs_task_idempotency",
        ),
        ForeignKeyConstraint(
            ["task_id", "investigation_id"],
            ["investigation_tasks.id", "investigation_tasks.investigation_id"],
            name="fk_investigation_handoffs_task",
            ondelete="RESTRICT",
        ),
        CheckConstraint("version >= 1", name="ck_investigation_handoffs_version"),
        CheckConstraint(
            f"schema_version = '{HANDOFF_SCHEMA_VERSION}'",
            name="ck_investigation_handoffs_schema_version",
        ),
        CheckConstraint(
            "jsonb_typeof(content) = 'object'",
            name="ck_investigation_handoffs_content_object",
        ),
        CheckConstraint(
            "char_length(content_sha256) = 64 AND content_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_investigation_handoffs_content_sha256",
        ),
        CheckConstraint(
            "char_length(request_sha256) = 64 AND request_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_investigation_handoffs_request_sha256",
        ),
        CheckConstraint(
            f"from_actor_kind IN ({HANDOFF_PARTY_ACTOR_KIND_VALUES_SQL})",
            name="ck_investigation_handoffs_from_actor_kind",
        ),
        CheckConstraint(
            f"to_actor_kind IN ({HANDOFF_PARTY_ACTOR_KIND_VALUES_SQL})",
            name="ck_investigation_handoffs_to_actor_kind",
        ),
        CheckConstraint(
            f"created_by_actor_kind IN ({HANDOFF_CREATOR_ACTOR_KIND_VALUES_SQL})",
            name="ck_investigation_handoffs_created_by_actor_kind",
        ),
        CheckConstraint(
            _CONTENT_IDENTITY_SQL,
            name="ck_investigation_handoffs_content_identity",
        ),
        Index(
            "ix_investigation_handoffs_investigation_created",
            "investigation_id",
            "created_at",
            "id",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    investigation_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey(
            "analyst_investigations.id",
            name="fk_investigation_handoffs_investigation",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    task_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    schema_version: Mapped[str] = mapped_column(String(32), nullable=False)
    content: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    request_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    from_actor_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    from_actor_id: Mapped[str] = mapped_column(Text, nullable=False)
    to_actor_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    to_actor_id: Mapped[str] = mapped_column(Text, nullable=False)
    predecessor_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey(
            "investigation_handoffs.id",
            name="fk_investigation_handoffs_predecessor",
            ondelete="RESTRICT",
        ),
        nullable=True,
    )
    created_by_actor_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    created_by_actor_id: Mapped[str] = mapped_column(Text, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
