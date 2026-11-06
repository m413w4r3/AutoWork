from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from cti_app.domain.acquisition import (
    AcquisitionObservationKind,
    EvidenceClass,
    SourceEquivalenceClass,
)
from cti_app.domain.classification import TLP
from cti_app.domain.discovery import (
    SourceRelationshipStatus,
    SourceRole,
    canonicalize_http_url,
)


class CollectionState(StrEnum):
    PENDING = "pending"
    QUEUED = "queued"
    FETCHING = "fetching"
    ARCHIVED = "archived"
    EXTRACTED = "extracted"
    COMPLETED = "completed"
    UNAVAILABLE = "unavailable"
    BLOCKED = "blocked"
    FAILED_RETRYABLE = "failed_retryable"
    FAILED_TERMINAL = "failed_terminal"


class AttemptOutcome(StrEnum):
    SUCCEEDED = "succeeded"
    UNAVAILABLE = "unavailable"
    BLOCKED = "blocked"
    TOO_LARGE = "too_large"
    ERROR = "error"
    INTERRUPTED = "interrupted"
    SKIPPED = "skipped"


class CollectionFailureReason(StrEnum):
    ACCESS_BLOCKED = "access_blocked"
    HTTP_ERROR = "http_error"
    OBSOLETE_URL_404 = "obsolete_url_404"
    TIMEOUT = "timeout"
    TRANSPORT_ERROR = "transport_error"
    DNS_ERROR = "dns_error"
    TLS_ERROR = "tls_error"
    UNSAFE_DESTINATION = "unsafe_destination"
    SIZE_LIMIT = "size_limit"
    UNSUPPORTED_CONTENT = "unsupported_content"
    NON_PDF_CONTENT = "non_pdf_content"
    PDF_PARSE_FAILURE = "pdf_parse_failure"
    INTERRUPTED = "interrupted"
    FALLBACK_FAILED = "fallback_failed"
    BRIDGE_TRANSCRIPTION_FAILED = "bridge_transcription_failed"


class SourceAcquisitionMethod(StrEnum):
    """How the bytes attached to a source document were obtained."""

    EXISTING_ARCHIVE = "existing_archive"
    DIRECT_HTTP = "direct_http"
    HTTP_VARIANT = "http_variant"
    CANONICAL_VARIANT = "canonical_variant"
    AMP_VARIANT = "amp_variant"
    PRINT_VARIANT = "print_variant"
    MOBILE_VARIANT = "mobile_variant"
    RSS_ATOM_JSONFEED = "rss_atom_jsonfeed"
    PUBLISHER_PUBLIC_API = "publisher_public_api"
    EMBEDDED_STRUCTURED_DATA = "embedded_structured_data"
    PLAYWRIGHT_CHROMIUM_DOM = "playwright_chromium_dom"
    PLAYWRIGHT_NETWORK_RESPONSE = "playwright_network_response"
    PLAYWRIGHT_EMBEDDED_STATE = "playwright_embedded_state"
    PLAYWRIGHT_FIREFOX = "playwright_firefox"
    PLAYWRIGHT_WEBKIT = "playwright_webkit"
    MULTI_EXTRACTOR = "multi_extractor"
    WEB_ARCHIVE = "web_archive"
    WAYBACK = "wayback"
    WAYBACK_SNAPSHOT = "wayback_snapshot"
    ARCHIVE_PH = "archive_ph"
    COMMON_CRAWL = "common_crawl"
    COMMONCRAWL_WARC = "commoncrawl_warc"
    MEMENTO = "memento"
    MEMENTO_ARCHIVE = "memento_archive"
    SEARCH_CACHE = "search_cache"
    EXACT_TITLE_SEARCH = "exact_title_search"
    PUBLISHER_ALTERNATIVE_URL = "publisher_alternative_url"
    AUTHORIZED_SYNDICATION = "authorized_syndication"
    INDEPENDENT_CORROBORATION = "independent_corroboration"
    PUBLISHER_PDF = "publisher_pdf"
    VISUAL_OCR = "visual_ocr"
    OFFICIAL_NEWSLETTER_ARCHIVE = "official_newsletter_archive"
    THIRD_PARTY_EXTRACTION = "third_party_extraction"
    BRIDGE_SOURCE_DISCOVERY = "bridge_source_discovery"
    PLAYWRIGHT_RENDERED = "playwright_rendered"
    BRIDGE_TRANSCRIPTION = "bridge_transcription"
    MANUAL_UPLOAD = "manual_upload"


class CollectionTransportClassification(StrEnum):
    BLOCKED = "blocked"
    DNS = "dns"
    HTTP = "http"
    TLS = "tls"
    TIMEOUT = "timeout"
    TRANSPORT = "transport"


class CollectionResolutionProvenance(StrEnum):
    SAME_SITE_HTTP_REDIRECT = "same_site_http_redirect"


class DetectedMimeType(StrEnum):
    HTML = "text/html"
    PDF = "application/pdf"
    TEXT = "text/plain"
    JSON = "application/json"
    PNG = "image/png"
    JPEG = "image/jpeg"
    GIF = "image/gif"
    WEBP = "image/webp"
    SVG = "image/svg+xml"
    BINARY = "application/octet-stream"


class ClaimKind(StrEnum):
    NAME = "name"
    DATE = "date"
    IOC = "ioc"
    CVE = "cve"
    FACT = "fact"
    ASSESSMENT = "assessment"
    UNCERTAINTY = "uncertainty"
    INFECTION_CHAIN = "infection_chain"
    TTP = "ttp"
    VICTIMOLOGY = "victimology"


class IndicatorKind(StrEnum):
    HASH = "hash"
    DOMAIN = "domain"
    IP = "ip"
    URL = "url"
    CVE = "cve"
    ATTACK_ID = "attack_id"
    EMAIL = "email"


class SourceOriginKind(StrEnum):
    DISCOVERY = "discovery"
    REFERENCE_RESEARCH = "reference_research"
    REFERENCED_EVIDENCE = "referenced_evidence"
    MANUAL = "manual"


class ReviewStatus(StrEnum):
    EXTRACTED = "extracted"
    # Verified by the application itself (quote located in an archived source,
    # deterministic indicator) — never a fabricated HumanDecision.
    MACHINE_VERIFIED = "machine_verified"
    VALIDATED = "validated"
    CORRECTED = "corrected"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class SourceSpan:
    start: int
    end: int

    def __post_init__(self) -> None:
        if self.start < 0 or self.end <= self.start:
            raise ValueError("A source span must have positive ordered offsets")

    def passage(self, text: str) -> str:
        if self.end > len(text):
            raise ValueError("Source span exceeds extracted text")
        return text[self.start : self.end]


@dataclass(slots=True)
class SourceCollection:
    subject_id: UUID
    edition_id: UUID
    requested_url: str
    proposed_role: SourceRole
    # Derived from requested_url when not supplied; this is the key that
    # makes a subject hold one collection per publication.
    canonical_url: str = ""
    origin_kind: SourceOriginKind = SourceOriginKind.DISCOVERY
    parent_source_collection_id: UUID | None = None
    # A reference-research source has no DiscoveryBatch candidate to read its
    # metadata from, so the collection carries its own snapshot.
    batch_id: UUID | None = None
    source_candidate_id: UUID | None = None
    title: str | None = None
    publisher: str | None = None
    published_at: date | None = None
    source_tlp: TLP = TLP.CLEAR
    sensitivity: str = "public"
    external_llm_allowed: bool = True
    do_not_submit: bool = False
    id: UUID = field(default_factory=uuid4)
    state: CollectionState = CollectionState.PENDING
    relationship_status: SourceRelationshipStatus = SourceRelationshipStatus.PROVISIONAL
    relationship_evidence: str = "model_proposal"
    source_document_id: UUID | None = None
    decoded_blob_id: UUID | None = None
    latest_attempt_id: UUID | None = None
    derived_artifact_id: UUID | None = None
    fetch_job_id: UUID | None = None
    # A manual upload is not a collector job, so it holds its own lease token
    # instead of a `jobs.id`.  Only the operation that opened it may archive.
    manual_lease_id: UUID | None = None
    fetch_policy_snapshot_id: str | None = None
    fetch_started_at: datetime | None = None
    fetch_lease_expires_at: datetime | None = None
    error_reason: str | None = None
    failure_reason_code: CollectionFailureReason | None = None
    attempt_count: int = 0
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        if not self.requested_url.strip():
            raise ValueError("A collection URL is required")
        if not self.canonical_url:
            try:
                self.canonical_url = canonicalize_http_url(self.requested_url)
            except ValueError:
                self.canonical_url = self.requested_url.strip()
        if self.relationship_status is SourceRelationshipStatus.VERIFIED and not (
            self.relationship_evidence.startswith("human:")
            or self.relationship_evidence.startswith("deterministic:")
        ):
            raise ValueError(
                "A verified relationship requires deterministic evidence or a human decision"
            )
        if (
            self.origin_kind is SourceOriginKind.REFERENCED_EVIDENCE
            and self.parent_source_collection_id is None
        ):
            raise ValueError("Referenced evidence requires a parent source collection")
        if (
            self.origin_kind is not SourceOriginKind.REFERENCED_EVIDENCE
            and self.parent_source_collection_id is not None
        ):
            raise ValueError("Only referenced evidence may have a parent source collection")

    def claim_fetch(
        self,
        job_id: UUID,
        *,
        lease_duration: timedelta,
        policy_snapshot_id: str,
        now: datetime | None = None,
    ) -> bool:
        timestamp = now or datetime.now(UTC)
        if lease_duration <= timedelta(0):
            raise ValueError("A positive fetch lease is required")
        if self.state is CollectionState.FETCHING:
            if self.fetch_lease_expires_at and self.fetch_lease_expires_at > timestamp:
                return False
        elif self.state not in {
            CollectionState.PENDING,
            CollectionState.QUEUED,
            CollectionState.FAILED_RETRYABLE,
            CollectionState.UNAVAILABLE,
        }:
            return False
        self.state = CollectionState.FETCHING
        self.fetch_job_id = job_id
        self.manual_lease_id = None
        self.fetch_policy_snapshot_id = policy_snapshot_id
        self.fetch_started_at = timestamp
        self.fetch_lease_expires_at = timestamp + lease_duration
        self.error_reason = None
        self.failure_reason_code = None
        self.attempt_count += 1
        self._touch(timestamp)
        return True

    def claim_manual_upload(
        self,
        manual_lease_id: UUID,
        *,
        lease_duration: timedelta,
        policy_snapshot_id: str,
        allow_replace_archived_fallback: bool = False,
        now: datetime | None = None,
    ) -> bool:
        """Open an archiving lease for content supplied by the analyst.

        Unlike `claim_fetch`, this accepts BLOCKED and FAILED_TERMINAL: those
        are exactly the states an analyst supplies content for. Direct and
        manual archives are immutable; a provenance-verified fallback can be
        superseded while its old document and blobs remain queryable.

        The lease token is generated for this one operation and is deliberately
        NOT a job id: an analyst upload is not a collector download, and no
        row in ``jobs`` describes it.  A newer upload taking over an expired
        lease changes the token, so the older request can no longer archive.
        """
        timestamp = now or datetime.now(UTC)
        if lease_duration <= timedelta(0):
            raise ValueError("A positive fetch lease is required")
        if self.state in {
            CollectionState.ARCHIVED,
            CollectionState.EXTRACTED,
            CollectionState.COMPLETED,
        } and not (allow_replace_archived_fallback and self.source_document_id is not None):
            return False
        if self.state is CollectionState.FETCHING:
            if self.fetch_lease_expires_at and self.fetch_lease_expires_at > timestamp:
                return False
        self.state = CollectionState.FETCHING
        self.fetch_job_id = None
        self.manual_lease_id = manual_lease_id
        self.fetch_policy_snapshot_id = policy_snapshot_id
        self.fetch_started_at = timestamp
        self.fetch_lease_expires_at = timestamp + lease_duration
        self.error_reason = None
        self.failure_reason_code = None
        self.attempt_count += 1
        self._touch(timestamp)
        return True

    def archive(
        self,
        *,
        job_id: UUID,
        attempt_id: UUID,
        source_document_id: UUID,
        decoded_blob_id: UUID,
    ) -> None:
        if self.state is not CollectionState.FETCHING:
            raise ValueError("Only a fetching source can be archived")
        if self.fetch_job_id != job_id:
            raise ValueError("Only the current fetch lease owner can archive a source")
        self._complete_archive(attempt_id, source_document_id, decoded_blob_id)

    def archive_manual(
        self,
        *,
        manual_lease_id: UUID,
        attempt_id: UUID,
        source_document_id: UUID,
        decoded_blob_id: UUID,
    ) -> None:
        """Close an analyst upload opened by ``claim_manual_upload``.

        The collector lease and the manual lease are distinct tokens, so this
        also refuses a collector job that took the source over meanwhile.
        """
        if self.state is not CollectionState.FETCHING:
            raise ValueError("Only a fetching source can be archived")
        if self.fetch_job_id is not None or self.manual_lease_id != manual_lease_id:
            raise ValueError("Only the current manual lease owner can archive a source")
        self._complete_archive(attempt_id, source_document_id, decoded_blob_id)

    def archive_fallback(
        self,
        *,
        attempt_id: UUID,
        source_document_id: UUID,
        decoded_blob_id: UUID,
    ) -> None:
        """Archive a safe fallback only while the source remains unarchived.

        The row lock held by the caller serializes this check with a manual
        upload. An archive already supplied by a person is immutable and wins.
        """
        if self.state in {
            CollectionState.ARCHIVED,
            CollectionState.EXTRACTED,
            CollectionState.COMPLETED,
        }:
            raise ValueError("An archived source cannot be replaced by a fallback")
        if self.state is CollectionState.FETCHING:
            raise ValueError("A fallback cannot replace an active collection lease")
        self.attempt_count += 1
        self._complete_archive(attempt_id, source_document_id, decoded_blob_id)

    def record_fallback_attempt(self, attempt_id: UUID) -> None:
        """Track a failed fallback without changing the direct failure state."""
        if self.state in {
            CollectionState.ARCHIVED,
            CollectionState.EXTRACTED,
            CollectionState.COMPLETED,
            CollectionState.FETCHING,
        }:
            raise ValueError("A fallback attempt cannot be recorded for this source state")
        self.attempt_count += 1
        self.latest_attempt_id = attempt_id
        self._touch()

    def _complete_archive(
        self, attempt_id: UUID, source_document_id: UUID, decoded_blob_id: UUID
    ) -> None:
        self.latest_attempt_id = attempt_id
        self.source_document_id = source_document_id
        self.decoded_blob_id = decoded_blob_id
        self.derived_artifact_id = None
        self.state = CollectionState.ARCHIVED
        self.error_reason = None
        self.failure_reason_code = None
        self._clear_fetch_lease()
        self._touch()

    def renew_fetch_lease(
        self,
        job_id: UUID,
        *,
        lease_duration: timedelta,
        now: datetime | None = None,
    ) -> bool:
        if self.state is not CollectionState.FETCHING or self.fetch_job_id != job_id:
            return False
        timestamp = now or datetime.now(UTC)
        self.fetch_lease_expires_at = timestamp + lease_duration
        self._touch(timestamp)
        return True

    def extracted(self, artifact_id: UUID) -> None:
        if self.state is not CollectionState.ARCHIVED:
            raise ValueError("Only an archived source can be extracted")
        self.derived_artifact_id = artifact_id
        self.state = CollectionState.EXTRACTED
        self._touch()

    def set_current_derived_artifact(self, artifact_id: UUID) -> None:
        if self.state is CollectionState.ARCHIVED:
            self.extracted(artifact_id)
            return
        if self.state not in {CollectionState.EXTRACTED, CollectionState.COMPLETED}:
            raise ValueError("Only archived or extracted sources may replace their artifact")
        self.derived_artifact_id = artifact_id
        self._touch()

    def complete(self) -> None:
        if self.state is not CollectionState.EXTRACTED:
            raise ValueError("Only an extracted source can be completed")
        self.state = CollectionState.COMPLETED
        self._touch()

    def fail(
        self,
        state: CollectionState,
        *,
        attempt_id: UUID,
        reason: str,
        reason_code: CollectionFailureReason | None = None,
    ) -> None:
        if state not in {
            CollectionState.UNAVAILABLE,
            CollectionState.BLOCKED,
            CollectionState.FAILED_RETRYABLE,
            CollectionState.FAILED_TERMINAL,
        }:
            raise ValueError("Invalid collection failure state")
        if self.state is not CollectionState.FETCHING:
            raise ValueError("Only a fetching source can fail")
        self.latest_attempt_id = attempt_id
        self.state = state
        self.error_reason = _clean_reason(reason)
        self.failure_reason_code = reason_code
        self._clear_fetch_lease()
        self._touch()

    def fail_processing(
        self,
        *,
        reason: str,
        reason_code: CollectionFailureReason | None = None,
        retryable: bool = False,
        state: CollectionState | None = None,
    ) -> None:
        if self.state not in {CollectionState.ARCHIVED, CollectionState.EXTRACTED}:
            raise ValueError("Only archived evidence can record a processing failure")
        resolved_state = state or (
            CollectionState.FAILED_RETRYABLE if retryable else CollectionState.FAILED_TERMINAL
        )
        if resolved_state not in {
            CollectionState.BLOCKED,
            CollectionState.FAILED_RETRYABLE,
            CollectionState.FAILED_TERMINAL,
        }:
            raise ValueError("Invalid collection processing failure state")
        self.state = resolved_state
        self.error_reason = _clean_reason(reason)
        self.failure_reason_code = reason_code
        self._clear_fetch_lease()
        self._touch()

    def prepare_explicit_retry(self, *, policy_changed: bool) -> None:
        if self.state is CollectionState.BLOCKED:
            raise ValueError("A blocked source cannot bypass collection safety policy")
        if self.state is CollectionState.COMPLETED:
            return
        if self.state is CollectionState.FAILED_TERMINAL and not policy_changed:
            raise ValueError("A terminal failure requires a compatible policy change")
        if self.source_document_id and self.decoded_blob_id:
            self.state = CollectionState.ARCHIVED
        elif self.state is not CollectionState.FETCHING:
            self.state = CollectionState.PENDING
        self.error_reason = None
        self.failure_reason_code = None
        self._clear_fetch_lease()
        self._touch()

    def verify_relationship(
        self,
        role: SourceRole,
        *,
        actor_id: str | None = None,
        deterministic_evidence: str | None = None,
    ) -> None:
        if not (actor_id and actor_id.strip()) and not (
            deterministic_evidence and deterministic_evidence.strip()
        ):
            raise ValueError(
                "A verified relationship requires deterministic evidence or a human decision"
            )
        self.proposed_role = role
        self.relationship_status = SourceRelationshipStatus.VERIFIED
        if actor_id and actor_id.strip():
            self.relationship_evidence = f"human:{actor_id.strip()}"
        else:
            assert deterministic_evidence is not None
            self.relationship_evidence = f"deterministic:{deterministic_evidence.strip()}"
        self._touch()

    def correct_relationship(self, role: SourceRole, *, actor_id: str) -> None:
        self.verify_relationship(role, actor_id=actor_id)

    def _clear_fetch_lease(self) -> None:
        self.fetch_job_id = None
        self.manual_lease_id = None
        self.fetch_policy_snapshot_id = None
        self.fetch_started_at = None
        self.fetch_lease_expires_at = None

    def _touch(self, now: datetime | None = None) -> None:
        self.updated_at = now or datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class CollectionPolicySnapshot:
    id: str
    max_redirects: int
    timeout_seconds: float
    max_download_bytes: int
    max_expanded_bytes: int
    max_decompression_ratio: float
    user_agent: str
    allowed_domains: tuple[str, ...]
    blocked_domains: tuple[str, ...]
    collector_version: str
    extraction_limits: dict[str, int | float | str]
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[0-9a-f]{64}", self.id):
            raise ValueError("Policy snapshot id must be a lowercase SHA-256")
        if self.created_at.tzinfo is None:
            raise ValueError("Policy snapshot creation time must be timezone-aware")


@dataclass(frozen=True, slots=True)
class CollectionAttempt:
    collection_id: UUID
    # Exactly one acquisition token: a collector job, or the analyst upload
    # lease.  An attempt without a job is only ever a manual acquisition.
    job_id: UUID | None
    policy_snapshot_id: str
    requested_url: str
    final_url: str | None
    redirect_chain: tuple[str, ...]
    attempted_at: datetime
    completed_at: datetime
    http_status: int | None
    declared_content_type: str | None
    detected_content_type: str | None
    encoded_size: int | None
    encoded_sha256: str | None
    decoded_size: int | None
    decoded_sha256: str | None
    content_encoding: str | None
    allowed_headers: dict[str, str]
    outcome: AttemptOutcome
    failure_reason: str | None
    manual_lease_id: UUID | None = None
    reason_code: CollectionFailureReason | None = None
    transport_classification: CollectionTransportClassification | None = None
    candidate_resolution_url: str | None = None
    candidate_resolution_provenance: CollectionResolutionProvenance | None = None
    strategy_name: str | None = None
    strategy_version: str | None = None
    duration_ms: int | None = None
    acquisition_method: SourceAcquisitionMethod | None = None
    source_equivalence_class: SourceEquivalenceClass | None = None
    evidence_class: EvidenceClass | None = None
    observation_kind: AcquisitionObservationKind | None = None
    quality_score: float | None = None
    quality_assessment: dict[str, Any] | None = None
    archive_snapshot_time: datetime | None = None
    model_run_id: UUID | None = None
    model_generated: bool | None = None
    primary_source_verified: bool | None = None
    id: UUID = field(default_factory=uuid4)

    def __post_init__(self) -> None:
        if not self.policy_snapshot_id.strip() or not self.requested_url.strip():
            raise ValueError("Collection attempt configuration and URL are required")
        if (self.job_id is None) == (self.manual_lease_id is None):
            raise ValueError(
                "A collection attempt is either a collector job or a manual upload lease"
            )
        if self.attempted_at.tzinfo is None or self.completed_at.tzinfo is None:
            raise ValueError("Collection attempt timestamps must be timezone-aware")
        if self.outcome is AttemptOutcome.SUCCEEDED:
            if (
                not self.final_url
                or self.encoded_size is None
                or not self.encoded_sha256
                or self.decoded_size is None
                or not self.decoded_sha256
            ):
                raise ValueError(
                    "A successful attempt requires final URL and both content representations"
                )
        elif not self.failure_reason:
            raise ValueError("A failed attempt requires a reason")
        elif self.reason_code is None:
            raise ValueError("A failed attempt requires a structured reason code")
        if (self.candidate_resolution_url is None) != (
            self.candidate_resolution_provenance is None
        ):
            raise ValueError("A candidate resolution URL requires its provenance")
        if (self.strategy_name is None) != (self.strategy_version is None):
            raise ValueError("A strategy name and version must be recorded together")
        if self.duration_ms is not None and self.duration_ms < 0:
            raise ValueError("A collection attempt duration cannot be negative")
        for field_name, enum_type in (
            ("acquisition_method", SourceAcquisitionMethod),
            ("source_equivalence_class", SourceEquivalenceClass),
            ("evidence_class", EvidenceClass),
            ("observation_kind", AcquisitionObservationKind),
        ):
            value = getattr(self, field_name)
            if value is not None and not isinstance(value, enum_type):
                object.__setattr__(self, field_name, enum_type(value))
        if self.quality_score is not None and (
            isinstance(self.quality_score, bool)
            or not math.isfinite(self.quality_score)
            or not 0.0 <= self.quality_score <= 1.0
        ):
            raise ValueError("Acquisition quality score must be between 0 and 1")
        if self.quality_assessment is not None:
            if set(self.quality_assessment) != {"score", "reasons"}:
                raise ValueError("Acquisition quality assessment contains unsupported fields")
            score = self.quality_assessment["score"]
            if (
                isinstance(score, bool)
                or not isinstance(score, (int, float))
                or not math.isfinite(score)
                or not 0.0 <= score <= 1.0
            ):
                raise ValueError("Acquisition quality assessment score must be between 0 and 1")
            if self.quality_score is not None and float(score) != self.quality_score:
                raise ValueError("Acquisition quality score and assessment must agree")
            reasons = self.quality_assessment["reasons"]
            if not isinstance(reasons, (tuple, list)) or any(
                not isinstance(reason, str) for reason in reasons
            ):
                raise ValueError("Acquisition quality reasons must be typed strings")
        if self.archive_snapshot_time is not None and self.archive_snapshot_time.tzinfo is None:
            raise ValueError("Archive snapshot time must be timezone-aware")


@dataclass(frozen=True, slots=True)
class DerivedArtifact:
    source_document_id: UUID
    text_blob_id: UUID
    parser_name: str
    parser_version: str
    text_length: int
    publication_metadata: dict[str, Any]
    id: UUID = field(default_factory=uuid4)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass(frozen=True, slots=True)
class Claim:
    subject_id: UUID
    edition_id: UUID
    source_document_id: UUID
    derived_artifact_id: UUID
    kind: ClaimKind
    value: str
    span: SourceSpan
    extraction_method: str
    extraction_payload: dict[str, Any]
    chunk_id: str
    local_span: SourceSpan | None = None
    model_run_id: UUID | None = None
    id: UUID = field(default_factory=uuid4)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        if not self.value.strip() or not self.extraction_method.strip():
            raise ValueError("Claim value and extraction method are required")
        if not self.chunk_id.strip():
            raise ValueError("Claim chunk id is required")


@dataclass(frozen=True, slots=True)
class RejectedModelProposal:
    source_document_id: UUID
    derived_artifact_id: UUID
    chunk_id: str
    category: str
    requested_kind: str
    reason: str
    proposal_hash: str
    model_run_id: UUID | None = None
    id: UUID = field(default_factory=uuid4)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        if not self.chunk_id.strip() or not self.category.strip() or not self.reason.strip():
            raise ValueError("Rejected proposal provenance is incomplete")
        if not re.fullmatch(r"[0-9a-f]{64}", self.proposal_hash):
            raise ValueError("Rejected proposal hash must be a lowercase SHA-256")


@dataclass(frozen=True, slots=True)
class Indicator:
    subject_id: UUID
    edition_id: UUID
    source_document_id: UUID
    derived_artifact_id: UUID
    kind: IndicatorKind
    original_value: str
    normalized_value: str
    span: SourceSpan
    id: UUID = field(default_factory=uuid4)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        if not self.original_value.strip() or not self.normalized_value.strip():
            raise ValueError("Indicator values are required")


LITERAL_REQUIRED_CLAIMS = {ClaimKind.NAME, ClaimKind.DATE, ClaimKind.IOC, ClaimKind.CVE}


def validate_claim_literal(claim: Claim, extracted_text: str) -> None:
    passage = claim.span.passage(extracted_text)
    if claim.kind in LITERAL_REQUIRED_CLAIMS and claim.value.casefold() not in passage.casefold():
        raise ValueError(f"{claim.kind.value} claim is absent from its source passage")


def _clean_reason(value: str) -> str:
    return " ".join(value.replace("\x00", "").split())[:1000] or "unspecified"


SHA256_PATTERN = re.compile(r"(?i)\b[a-f0-9]{64}\b")
