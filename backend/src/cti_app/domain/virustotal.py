import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from cti_app.domain.errors import DomainError

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class VirusTotalOperation(StrEnum):
    FILE_REPORT = "file_report"
    FILE_RELATIONSHIP = "file_relationship"
    OBJECT_RELATIONSHIP = "object_relationship"
    INTELLIGENCE_SEARCH = "intelligence_search"
    BEHAVIOURS = "behaviours"
    FILE_DOWNLOAD = "file_download"
    RETROHUNT_SUBMIT = "retrohunt_submit"
    RETROHUNT_STATUS = "retrohunt_status"
    RETROHUNT_RESULTS = "retrohunt_results"


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
    BEHAVIOUR_REPORTS = "behaviour_reports"
    BEHAVIOUR_PCAP = "behaviour_pcap"
    RETROHUNT = "retrohunt"


class VirusTotalObjectType(StrEnum):
    FILE = "file"
    IP_ADDRESS = "ip_address"
    DOMAIN = "domain"
    URL = "url"


class VirusTotalFileRelationship(StrEnum):
    CONTACTED_DOMAINS = "contacted_domains"
    CONTACTED_IPS = "contacted_ips"
    CONTACTED_URLS = "contacted_urls"
    DROPPED_FILES = "dropped_files"
    EXECUTION_PARENTS = "execution_parents"
    ITW_URLS = "itw_urls"
    EMBEDDED_URLS = "embedded_urls"
    SIMILAR_FILES = "similar_files"
    BUNDLED_FILES = "bundled_files"
    SUBMISSIONS = "submissions"


class VirusTotalIpRelationship(StrEnum):
    COMMENTS = "comments"
    COMMUNICATING_FILES = "communicating_files"
    DOWNLOADED_FILES = "downloaded_files"
    GRAPHS = "graphs"
    HISTORICAL_SSL_CERTIFICATES = "historical_ssl_certificates"
    HISTORICAL_WHOIS = "historical_whois"
    RELATED_COMMENTS = "related_comments"
    RELATED_REFERENCES = "related_references"
    RELATED_THREAT_ACTORS = "related_threat_actors"
    REFERRER_FILES = "referrer_files"
    RESOLUTIONS = "resolutions"
    URLS = "urls"


class VirusTotalDomainRelationship(StrEnum):
    CAA_RECORDS = "caa_records"
    CNAME_RECORDS = "cname_records"
    COMMENTS = "comments"
    COMMUNICATING_FILES = "communicating_files"
    DOWNLOADED_FILES = "downloaded_files"
    GRAPHS = "graphs"
    HISTORICAL_SSL_CERTIFICATES = "historical_ssl_certificates"
    HISTORICAL_WHOIS = "historical_whois"
    IMMEDIATE_PARENT = "immediate_parent"
    MX_RECORDS = "mx_records"
    NS_RECORDS = "ns_records"
    PARENT = "parent"
    REFERRER_FILES = "referrer_files"
    RELATED_COMMENTS = "related_comments"
    RELATED_REFERENCES = "related_references"
    RELATED_THREAT_ACTORS = "related_threat_actors"
    RESOLUTIONS = "resolutions"
    SOA_RECORDS = "soa_records"
    SIBLINGS = "siblings"
    SUBDOMAINS = "subdomains"
    URLS = "urls"
    USER_VOTES = "user_votes"


class VirusTotalUrlRelationship(StrEnum):
    COMMUNICATING_FILES = "communicating_files"
    DOWNLOADED_FILES = "downloaded_files"
    REFERRER_FILES = "referrer_files"
    REDIRECTING_URLS = "redirecting_urls"


type VirusTotalRelationship = (
    VirusTotalFileRelationship
    | VirusTotalIpRelationship
    | VirusTotalDomainRelationship
    | VirusTotalUrlRelationship
)

VIRUSTOTAL_RELATIONSHIPS_BY_OBJECT_TYPE: dict[VirusTotalObjectType, frozenset[str]] = {
    VirusTotalObjectType.FILE: frozenset(item.value for item in VirusTotalFileRelationship),
    VirusTotalObjectType.IP_ADDRESS: frozenset(item.value for item in VirusTotalIpRelationship),
    VirusTotalObjectType.DOMAIN: frozenset(item.value for item in VirusTotalDomainRelationship),
    VirusTotalObjectType.URL: frozenset(item.value for item in VirusTotalUrlRelationship),
}


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
    execution_id: UUID = field(default_factory=uuid4)
    id: UUID = field(default_factory=uuid4)

    def __post_init__(self) -> None:
        if not isinstance(self.capability, VirusTotalCapability):
            raise DomainError("VirusTotal observation capability must be a VirusTotalCapability")
        if not self.source_identifier.strip():
            raise DomainError("VirusTotal observation requires a non-empty source identifier")
        if not self.normalization_contract_version.strip():
            raise DomainError("VirusTotal observation requires a non-empty contract version")
        if not _SHA256_RE.fullmatch(self.raw_sha256):
            raise DomainError("VirusTotal observation raw_sha256 must be a lowercase 64-hex digest")
        if not 200 <= self.http_status < 300:
            raise DomainError("VirusTotal observations represent successful HTTP responses only")
        if self.raw_size < 0 or self.observed_count < 0 or self.page_order < 0:
            raise DomainError("VirusTotal observation counts cannot be negative")
        if self.observed_at.tzinfo is None or self.observed_at.utcoffset() is None:
            raise DomainError("observed_at must be timezone-aware")


@dataclass(frozen=True, slots=True)
class VirusTotalBehaviourSummary:
    sandbox: str | None
    date: int | str | None
    tags: tuple[str, ...]
    has_pcap: bool
    has_html_report: bool
    available_sections: tuple[str, ...]


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
    vhash: str | None = None
    imphash: str | None = None
    ssdeep: str | None = None
    tlsh: str | None = None
    main_icon_dhash: str | None = None
    rich_header_hash: str | None = None
    id: UUID = field(default_factory=uuid4)
