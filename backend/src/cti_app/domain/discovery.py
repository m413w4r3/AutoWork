from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from enum import StrEnum
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import UUID, uuid4

from cti_app.domain.classification import TLP


class ContributionStatus(StrEnum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    REJECTED = "rejected"


class SourceRole(StrEnum):
    PRIMARY = "primary"
    INDEPENDENT = "independent"
    RELAY = "relay"
    AGGREGATOR = "aggregator"
    SOCIAL = "social"
    UNKNOWN = "unknown"


class SourceVerificationStatus(StrEnum):
    UNVERIFIED = "unverified"
    VERIFY_LATER = "verify_later"
    INVALID = "invalid"
    UNAVAILABLE = "unavailable"


class SourceRelationshipStatus(StrEnum):
    PROVISIONAL = "provisional"
    VERIFIED = "verified"


class DiscoverySourceMode(StrEnum):
    NATIVE_COMPLETE = "native_complete"
    VISIBLE_CITATIONS_ONLY = "visible_citations_only"
    MODEL_DECLARED_URLS = "model_declared_urls"
    MANUAL_IMPORT = "manual_import"


class DiscoveryBatchStatus(StrEnum):
    COMPLETED = "completed"


class PeriodRelation(StrEnum):
    IN_PERIOD = "in_period"
    OUTSIDE_PERIOD = "outside_period"
    UNKNOWN = "unknown"


class IocPresence(StrEnum):
    NONE = "none"
    DECLARED = "declared"
    VISIBLE = "visible"
    UNKNOWN = "unknown"


class DiscoveryIocType(StrEnum):
    IPV4 = "ipv4"
    IPV6 = "ipv6"
    DOMAIN = "domain"
    URL = "url"
    MD5 = "md5"
    SHA1 = "sha1"
    SHA256 = "sha256"
    EMAIL = "email"
    CVE = "cve"
    OTHER = "other"
    UNKNOWN = "unknown"


class DiscoveryIocStatus(StrEnum):
    PROVISIONAL_VISIBLE = "provisional_visible"


@dataclass(frozen=True, slots=True)
class ProvisionalIocPublicationRelation:
    publication_id: UUID
    publication_ref: str
    raw_value: str
    markdown_block: str


@dataclass(slots=True)
class ProvisionalDiscoveryIoc:
    raw_value: str
    normalized_value: str | None
    declared_type: str
    proposed_type: DiscoveryIocType
    publication_relations: tuple[ProvisionalIocPublicationRelation, ...]
    model_run_id: UUID | None
    markdown_block: str
    warnings: tuple[str, ...] = ()
    status: DiscoveryIocStatus = DiscoveryIocStatus.PROVISIONAL_VISIBLE
    id: UUID = field(default_factory=uuid4)

    def __post_init__(self) -> None:
        self.raw_value = self.raw_value.strip()
        self.declared_type = self.declared_type.strip() or "unknown"
        if not self.raw_value or not self.publication_relations:
            raise ValueError("A provisional IOC requires a value and publication relation")


@dataclass(slots=True)
class SourceCandidate:
    url: str
    title: str
    publisher: str
    role: SourceRole
    tlp: TLP
    sensitivity: str
    external_llm_allowed: bool
    published_at: date | None = None
    event_date: date | None = None
    citation: str | None = None
    local_ref: str | None = None
    source_ref: str = field(init=False)
    raw_url: str | None = None
    period_relation: PeriodRelation = PeriodRelation.UNKNOWN
    ioc_presence: IocPresence = IocPresence.UNKNOWN
    ioc_declared_count: int | None = None
    ioc_visible_count: int | None = None
    parsing_warnings: tuple[str, ...] = ()
    markdown_block: str | None = None
    id: UUID = field(default_factory=uuid4)
    verification_status: SourceVerificationStatus = SourceVerificationStatus.UNVERIFIED
    relationship_status: SourceRelationshipStatus = SourceRelationshipStatus.PROVISIONAL
    verification_changed_at: datetime | None = None
    verification_changed_by: str | None = None
    canonical_url: str = field(init=False)
    title_fingerprint: str = field(init=False)

    def __post_init__(self) -> None:
        self.canonical_url = canonicalize_http_url(self.url)
        self.raw_url = self.raw_url or self.url
        self.source_ref = "source-" + hashlib.sha256(self.canonical_url.encode()).hexdigest()[:20]
        self.title = self.title.strip()
        self.publisher = self.publisher.strip()
        self.sensitivity = self.sensitivity.strip()
        self.title_fingerprint = fingerprint_title(self.title)
        if not self.title or not self.publisher or not self.sensitivity:
            raise ValueError("Source title, publisher and sensitivity are required")
        if self.ioc_declared_count is not None and self.ioc_declared_count < 0:
            raise ValueError("Declared IOC count cannot be negative")
        if self.ioc_visible_count is not None and self.ioc_visible_count < 0:
            raise ValueError("Visible IOC count cannot be negative")

    def mark(self, status: SourceVerificationStatus, *, actor_id: str) -> None:
        if not actor_id.strip():
            raise ValueError("Source verification actor is required")
        self.verification_status = status
        self.verification_changed_at = datetime.now(UTC)
        self.verification_changed_by = actor_id.strip()


@dataclass(slots=True)
class IncompleteSourceCandidate:
    title: str
    publisher: str = "unknown"
    raw_url: str | None = None
    local_ref: str | None = None
    published_at: date | None = None
    period_relation: PeriodRelation = PeriodRelation.UNKNOWN
    role: SourceRole = SourceRole.UNKNOWN
    ioc_presence: IocPresence = IocPresence.UNKNOWN
    ioc_declared_count: int | None = None
    ioc_visible_count: int | None = None
    parsing_warnings: tuple[str, ...] = ()
    markdown_block: str | None = None
    id: UUID = field(default_factory=uuid4)

    def __post_init__(self) -> None:
        self.title = self.title.strip() or "Publication incomplète"
        self.publisher = self.publisher.strip() or "unknown"


@dataclass(slots=True)
class CandidateTopic:
    title: str
    summary: str
    novelty: str
    technical_potential: int
    uncertainties: tuple[str, ...]
    relevance_reasons: tuple[str, ...]
    actors: tuple[str, ...]
    campaigns: tuple[str, ...]
    malware: tuple[str, ...]
    cves: tuple[str, ...]
    victims: tuple[str, ...]
    sectors: tuple[str, ...]
    countries: tuple[str, ...]
    likely_artifacts: tuple[str, ...]
    sources: list[SourceCandidate]
    tlp: TLP
    sensitivity: str
    external_llm_allowed: bool
    incomplete_sources: list[IncompleteSourceCandidate] = field(default_factory=list)
    event_date: date | None = None
    iocs: tuple[str, ...] = ()
    provisional_iocs: list[ProvisionalDiscoveryIoc] = field(default_factory=list)
    local_ref: str | None = None
    actor_or_campaign: str = "unknown"
    technical_potential_reason: str = "Non précisé dans le rapport de découverte."
    parsing_warnings: tuple[str, ...] = ()
    markdown_block: str | None = None
    context_only: bool = False
    id: UUID = field(default_factory=uuid4)
    title_fingerprint: str = field(init=False)
    editorial_status: str = "proposed"

    def __post_init__(self) -> None:
        self.title = self.title.strip()
        self.summary = self.summary.strip()
        self.novelty = self.novelty.strip()
        self.sensitivity = self.sensitivity.strip()
        self.title_fingerprint = fingerprint_title(self.title)
        if not self.title or not self.summary or not self.novelty:
            raise ValueError("Candidate title, summary and novelty are required")
        if not 0 <= self.technical_potential <= 4:
            raise ValueError("Technical potential must be between 0 and 4")
        if self.editorial_status != "proposed":
            raise ValueError("Discovery cannot select a topic automatically")
        self.sources = deduplicate_sources(self.sources)

    @property
    def selectable(self) -> bool:
        return bool(self.sources) and not self.context_only


@dataclass(slots=True)
class DiscoveryContribution:
    """One CandidateTopic with contribution metadata for temporal tracking."""
    candidate: CandidateTopic
    status: ContributionStatus
    created_at: datetime
    accepted_at: datetime | None = None
    human_note: str = ""

    def __post_init__(self) -> None:
        if self.status == ContributionStatus.ACCEPTED and self.accepted_at is None:
            self.accepted_at = datetime.now(UTC)


@dataclass(slots=True)
class DiscoveryBatch:
    edition_id: UUID
    request_hash: str
    complementary_axis: str
    queries: tuple[str, ...]
    citations: tuple[dict[str, str | None], ...]
    contributions: list[DiscoveryContribution]
    discovery_model_run_id: UUID
    structuring_model_run_id: UUID
    tlp: TLP
    sensitivity: str
    external_llm_allowed: bool
    report_sha256: str | None = None
    parser_version: str = "legacy-model-structured"
    parsing_status: str = "completed"
    parsing_warnings: tuple[str, ...] = ()
    unattached_visible_citations: tuple[dict[str, str | None], ...] = ()
    parsing_revision: int = 1
    supersedes_batch_id: UUID | None = None
    replaced_by_batch_id: UUID | None = None
    source_mode: DiscoverySourceMode = DiscoverySourceMode.VISIBLE_CITATIONS_ONLY
    bridge_capabilities: dict[str, object] = field(default_factory=dict)
    citation_count: int = 0
    source_coverage_complete: bool = False
    source_coverage_incomplete_reason: str | None = (
        "Le bridge expose uniquement les citations visibles de ChatGPT."
    )
    id: UUID = field(default_factory=uuid4)
    status: DiscoveryBatchStatus = DiscoveryBatchStatus.COMPLETED
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[0-9a-f]{64}", self.request_hash):
            raise ValueError("Discovery request hash must be a lowercase SHA-256")
        if not self.complementary_axis.strip() or not self.sensitivity.strip():
            raise ValueError("Discovery axis and sensitivity are required")
        self.citation_count = len(self.citations)
        if self.source_mode is DiscoverySourceMode.VISIBLE_CITATIONS_ONLY:
            self.source_coverage_complete = False
            self.source_coverage_incomplete_reason = (
                self.source_coverage_incomplete_reason
                or "Le bridge expose uniquement les citations visibles de ChatGPT."
            )
            for contribution in self.contributions:
                for source in contribution.candidate.sources:
                    source.relationship_status = SourceRelationshipStatus.PROVISIONAL
        if not self.source_coverage_complete and not self.source_coverage_incomplete_reason:
            raise ValueError("Incomplete source coverage requires a reason")
        # Deduplicate candidate topics
        candidates = [c.candidate for c in self.contributions]
        deduplicated = deduplicate_topics(candidates)
        # Update contributions with deduplicated candidates
        candidate_map = {c.id: c for c in deduplicated}
        for contribution in self.contributions:
            if contribution.candidate.id in candidate_map:
                contribution.candidate = candidate_map[contribution.candidate.id]
        if self.parsing_revision < 1:
            raise ValueError("Parsing revision must be positive")

    @property
    def candidates(self) -> list[CandidateTopic]:
        """Return only ACCEPTED contributions (for backward compat)."""
        return [c.candidate for c in self.contributions if c.status == ContributionStatus.ACCEPTED]

    @property
    def is_active_revision(self) -> bool:
        return self.replaced_by_batch_id is None

    @property
    def history_hash(self) -> str:
        """Hash of accepted contributions for idempotency checking."""
        accepted = [c.candidate.id for c in self.contributions if c.status == ContributionStatus.ACCEPTED]
        content = "|".join(str(cid) for cid in sorted(accepted))
        return hashlib.sha256(content.encode()).hexdigest()

    def source(self, source_id: UUID) -> SourceCandidate | None:
        return next(
            (
                source
                for contribution in self.contributions
                for source in contribution.candidate.sources
                if source.id == source_id
            ),
            None,
        )


def canonicalize_http_url(value: str) -> str:
    parsed = urlsplit(value.strip())
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Source URL must use HTTP or HTTPS")
    scheme = parsed.scheme.lower()
    host = parsed.hostname.lower().encode("idna").decode("ascii")
    port = parsed.port
    if port and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        host = f"{host}:{port}"
    path = parsed.path or "/"
    if path != "/":
        path = path.rstrip("/") or "/"
    query = urlencode(
        sorted(
            (key, item)
            for key, item in parse_qsl(parsed.query, keep_blank_values=True)
            if not key.lower().startswith("utm_") and key.lower() not in {"fbclid", "gclid"}
        )
    )
    return urlunsplit((scheme, host, path, query, ""))


def fingerprint_title(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value.casefold())
    words = re.findall(r"[a-z0-9]+", normalized.encode("ascii", "ignore").decode())
    if not words:
        raise ValueError("Title must contain searchable characters")
    return hashlib.sha256(" ".join(words).encode()).hexdigest()


def deduplicate_sources(sources: list[SourceCandidate]) -> list[SourceCandidate]:
    seen_urls: set[str] = set()
    unique: list[SourceCandidate] = []
    for source in sources:
        if source.canonical_url in seen_urls:
            continue
        seen_urls.add(source.canonical_url)
        unique.append(source)
    return unique


def deduplicate_topics(topics: list[CandidateTopic]) -> list[CandidateTopic]:
    unique: dict[str, CandidateTopic] = {}
    for topic in topics:
        existing = unique.get(topic.title_fingerprint)
        if existing is None:
            unique[topic.title_fingerprint] = topic
            continue
        existing.sources = deduplicate_sources([*existing.sources, *topic.sources])
        existing.technical_potential = max(existing.technical_potential, topic.technical_potential)
        existing.uncertainties = tuple(
            dict.fromkeys((*existing.uncertainties, *topic.uncertainties))
        )
        existing.relevance_reasons = tuple(
            dict.fromkeys((*existing.relevance_reasons, *topic.relevance_reasons))
        )
    return list(unique.values())
