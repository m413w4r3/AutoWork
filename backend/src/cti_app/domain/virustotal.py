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


class VirusTotalCapability(StrEnum):
    """Authorization to request an operation. Says nothing about transport.

    A capability being enabled never implies proxy access, direct access, or
    any particular route: see `VirusTotalRoutingPolicy` in the application
    layer for how a route is chosen. Persisted verbatim on observations.
    """

    FILE_REPORT = "file_report"
    FILE_RELATIONSHIPS = "file_relationships"
    INTELLIGENCE_SEARCH = "intelligence_search"
    FILE_DOWNLOAD = "file_download"
    SUBMISSIONS = "submissions"
    BEHAVIOUR_PCAP = "behaviour_pcap"
    RETROHUNT = "retrohunt"


class VirusTotalTransportKind(StrEnum):
    """The network path used to reach VirusTotal, independent of authorization."""

    PROXY = "proxy"
    DIRECT = "direct"


class VirusTotalEndpointVariant(StrEnum):
    """The concrete wire endpoint a route step targets."""

    V3 = "v3"
    V3_FALLBACK = "v3_fallback"
    LEGACY_V2 = "legacy_v2"


class VirusTotalFallbackTrigger(StrEnum):
    """The exact upstream outcome allowed to advance a route to its next step.

    Any other outcome (403, 429, timeout, 5xx, ...) propagates immediately and
    never causes an implicit fallback.
    """

    NOT_FOUND = "not_found"


@dataclass(frozen=True, slots=True, kw_only=True)
class VirusTotalObservation:
    operation: VirusTotalOperation
    capability: VirusTotalCapability
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
