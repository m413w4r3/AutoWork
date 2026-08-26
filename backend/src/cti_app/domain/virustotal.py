from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from cti_app.domain.errors import DomainError


class VirusTotalOperation(StrEnum):
    FILE_REPORT = "file_report"
    FILE_RELATIONSHIP = "file_relationship"
    INTELLIGENCE_SEARCH = "intelligence_search"


@dataclass(frozen=True, slots=True, kw_only=True)
class VirusTotalObservation:
    operation: VirusTotalOperation
    capability: str
    source_identifier: str
    safe_parameters: dict[str, Any]
    http_status: int
    blob_id: UUID
    raw_sha256: str
    raw_size: int
    observed_at: datetime
    subject_id: UUID | None = None
    input_cursor: str | None = None
    output_cursor: str | None = None
    observed_count: int = 0
    exhaustive: bool = True
    page_order: int = 0
    normalization_contract_version: str = "vt-normalization-v1"
    id: UUID = field(default_factory=uuid4)

    def __post_init__(self) -> None:
        if not self.capability or not self.source_identifier:
            raise DomainError("VirusTotal observation requires a capability and source identifier")
        if not 200 <= self.http_status < 300:
            raise DomainError("VirusTotal observations represent successful HTTP responses only")
        if self.raw_size < 0 or self.observed_count < 0 or self.page_order < 0:
            raise DomainError("VirusTotal observation counts cannot be negative")
        if self.observed_at.tzinfo is None or self.observed_at.utcoffset() is None:
            raise DomainError("observed_at must be timezone-aware")


@dataclass(frozen=True, slots=True, kw_only=True)
class VirusTotalFileView:
    observation_id: UUID
    vt_file_id: str
    file_type: str
    lookup_hash: str
    meaningful_name: str | None
    type_description: str | None
    size: int | None
    last_analysis_stats: dict[str, int] | None
    first_submission_date: int | None
    last_submission_date: int | None
    last_modification_date: int | None
    tags: tuple[str, ...]
    id: UUID = field(default_factory=uuid4)
