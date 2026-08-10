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


class DiscoveryBatchStatus(StrEnum):
    COMPLETED = "completed"


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
    id: UUID = field(default_factory=uuid4)
    verification_status: SourceVerificationStatus = SourceVerificationStatus.UNVERIFIED
    verification_changed_at: datetime | None = None
    verification_changed_by: str | None = None
    canonical_url: str = field(init=False)
    title_fingerprint: str = field(init=False)

    def __post_init__(self) -> None:
        self.canonical_url = canonicalize_http_url(self.url)
        self.title = self.title.strip()
        self.publisher = self.publisher.strip()
        self.sensitivity = self.sensitivity.strip()
        self.title_fingerprint = fingerprint_title(self.title)
        if not self.title or not self.publisher or not self.sensitivity:
            raise ValueError("Source title, publisher and sensitivity are required")

    def mark(self, status: SourceVerificationStatus, *, actor_id: str) -> None:
        if not actor_id.strip():
            raise ValueError("Source verification actor is required")
        self.verification_status = status
        self.verification_changed_at = datetime.now(UTC)
        self.verification_changed_by = actor_id.strip()


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
    event_date: date | None = None
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


@dataclass(slots=True)
class DiscoveryBatch:
    edition_id: UUID
    request_hash: str
    complementary_axis: str
    queries: tuple[str, ...]
    citations: tuple[dict[str, str | None], ...]
    candidates: list[CandidateTopic]
    discovery_model_run_id: UUID
    structuring_model_run_id: UUID
    tlp: TLP
    sensitivity: str
    external_llm_allowed: bool
    id: UUID = field(default_factory=uuid4)
    status: DiscoveryBatchStatus = DiscoveryBatchStatus.COMPLETED
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[0-9a-f]{64}", self.request_hash):
            raise ValueError("Discovery request hash must be a lowercase SHA-256")
        if not self.complementary_axis.strip() or not self.sensitivity.strip():
            raise ValueError("Discovery axis and sensitivity are required")
        self.candidates = deduplicate_topics(self.candidates)

    def source(self, source_id: UUID) -> SourceCandidate | None:
        return next(
            (
                source
                for candidate in self.candidates
                for source in candidate.sources
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
    seen_titles: set[str] = set()
    unique: list[SourceCandidate] = []
    for source in sources:
        if source.canonical_url in seen_urls or source.title_fingerprint in seen_titles:
            continue
        seen_urls.add(source.canonical_url)
        seen_titles.add(source.title_fingerprint)
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
