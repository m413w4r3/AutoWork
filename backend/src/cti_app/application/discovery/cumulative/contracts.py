from __future__ import annotations

from uuid import UUID

from pydantic import field_validator

from cti_app.application.jobs import JobParameters


class ReconcileDiscoveryParameters(JobParameters):
    intake_id: UUID
    edition_id: UUID
    expected_parent_snapshot_id: UUID | None
    actor_id: str
    rebase_count: int = 0

    @field_validator("intake_id", "edition_id", "expected_parent_snapshot_id", mode="before")
    @classmethod
    def parse_uuid(cls, value: object) -> object:
        return UUID(value) if isinstance(value, str) and value else value
