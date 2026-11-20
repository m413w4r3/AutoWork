"""Noncanonical, retention-limited editorial draft identity."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID


class EditorialDraftError(ValueError):
    """A server draft cannot be read or changed under its current fence."""

    code = "editorial_draft_error"


@dataclass(frozen=True, slots=True)
class EditorialDraft:
    id: UUID
    subject_id: UUID
    production_run_id: UUID
    occurrence_id: UUID
    base_snapshot_id: UUID
    base_artifact_id: UUID
    base_artifact_version: int
    base_canonical_sha256: str
    actor_id: UUID
    text_blob_ref: UUID
    text_sha256: str
    version: int
    created_at: datetime
    updated_at: datetime
    expires_at: datetime


def draft_expiry(updated_at: datetime, retention_days: int) -> datetime:
    if isinstance(retention_days, bool) or retention_days <= 0:
        raise EditorialDraftError("draft_retention_unconfigured")
    return updated_at + timedelta(days=retention_days)
