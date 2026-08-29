"""Append-only editorial decisions for the publication review checkpoint."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID, uuid4

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
MAX_REVIEW_REASON_LENGTH = 500


class PublicationDecision(StrEnum):
    INCLUDE = "include"
    EXCLUDE = "exclude"


@dataclass(frozen=True, slots=True, kw_only=True)
class PublicationReviewDecision:
    """One decision about one exact rendered document artifact.

    The artifact identity is deliberately copied into the event.  A retry can
    therefore leave this event in history while making it inapplicable to the
    newly generated document.
    """

    edition_id: UUID
    subject_id: UUID
    production_run_id: UUID
    pipeline_generation: int
    document_artifact_id: UUID
    document_artifact_version: int
    document_input_hash: str
    decision: PublicationDecision
    actor_id: str
    reason: str | None = None
    occurred_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    id: UUID = field(default_factory=uuid4)

    def __post_init__(self) -> None:
        if self.pipeline_generation < 0:
            raise ValueError("pipeline_generation must be >= 0")
        if self.document_artifact_version < 1:
            raise ValueError("document_artifact_version must be >= 1")
        if not _SHA256_RE.fullmatch(self.document_input_hash):
            raise ValueError("document_input_hash must be lowercase SHA-256")
        if not self.actor_id.strip():
            raise ValueError("actor_id must not be empty")
        if self.reason is not None:
            normalized_reason = self.reason.strip()
            if len(normalized_reason) > MAX_REVIEW_REASON_LENGTH:
                raise ValueError("reason is too long")
            object.__setattr__(self, "reason", normalized_reason or None)
        if self.decision is PublicationDecision.EXCLUDE and not self.reason:
            raise ValueError("exclude decisions require a reason")
        if self.occurred_at.tzinfo is None or self.occurred_at.utcoffset() is None:
            raise ValueError("occurred_at must be timezone-aware")
