from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from cti_app.domain.discovery import (
    SourceRelationshipStatus,
    SourceRole,
)


class CollectionState(StrEnum):
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


class DetectedMimeType(StrEnum):
    HTML = "text/html"
    PDF = "application/pdf"


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


class ReviewStatus(StrEnum):
    EXTRACTED = "extracted"
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
    group_id: UUID
    batch_id: UUID
    source_candidate_id: UUID
    requested_url: str
    proposed_role: SourceRole
    id: UUID = field(default_factory=uuid4)
    state: CollectionState = CollectionState.QUEUED
    relationship_status: SourceRelationshipStatus = SourceRelationshipStatus.PROVISIONAL
    relationship_evidence: str = "model_proposal"
    source_document_id: UUID | None = None
    latest_attempt_id: UUID | None = None
    derived_artifact_id: UUID | None = None
    error_reason: str | None = None
    attempt_count: int = 0
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        if not self.requested_url.strip():
            raise ValueError("A collection URL is required")
        if self.relationship_status is SourceRelationshipStatus.VERIFIED and not (
            self.relationship_evidence.startswith("human:")
            or self.relationship_evidence.startswith("deterministic:")
        ):
            raise ValueError(
                "A verified relationship requires deterministic evidence or a human decision"
            )

    def start(self) -> None:
        if self.state not in {
            CollectionState.QUEUED,
            CollectionState.FAILED_RETRYABLE,
            CollectionState.UNAVAILABLE,
        }:
            raise ValueError(f"Cannot fetch a source in state {self.state}")
        self.state = CollectionState.FETCHING
        self.error_reason = None
        self.attempt_count += 1
        self._touch()

    def archive(self, *, attempt_id: UUID, source_document_id: UUID) -> None:
        if self.state is not CollectionState.FETCHING:
            raise ValueError("Only a fetching source can be archived")
        self.latest_attempt_id = attempt_id
        self.source_document_id = source_document_id
        self.state = CollectionState.ARCHIVED
        self._touch()

    def extracted(self, artifact_id: UUID) -> None:
        if self.state is not CollectionState.ARCHIVED:
            raise ValueError("Only an archived source can be extracted")
        self.derived_artifact_id = artifact_id
        self.state = CollectionState.EXTRACTED
        self._touch()

    def complete(self) -> None:
        if self.state is not CollectionState.EXTRACTED:
            raise ValueError("Only an extracted source can be completed")
        self.state = CollectionState.COMPLETED
        self._touch()

    def fail(self, state: CollectionState, *, attempt_id: UUID, reason: str) -> None:
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

    def _touch(self) -> None:
        self.updated_at = datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class CollectionAttempt:
    collection_id: UUID
    job_id: UUID
    configuration_id: str
    requested_url: str
    final_url: str | None
    redirect_chain: tuple[str, ...]
    attempted_at: datetime
    completed_at: datetime
    http_status: int | None
    declared_content_type: str | None
    detected_content_type: str | None
    size: int | None
    sha256: str | None
    allowed_headers: dict[str, str]
    outcome: AttemptOutcome
    failure_reason: str | None
    id: UUID = field(default_factory=uuid4)

    def __post_init__(self) -> None:
        if not self.configuration_id.strip() or not self.requested_url.strip():
            raise ValueError("Collection attempt configuration and URL are required")
        if self.attempted_at.tzinfo is None or self.completed_at.tzinfo is None:
            raise ValueError("Collection attempt timestamps must be timezone-aware")
        if self.outcome is AttemptOutcome.SUCCEEDED:
            if not self.final_url or self.size is None or not self.sha256:
                raise ValueError("A successful attempt requires final URL, size and SHA-256")
        elif not self.failure_reason:
            raise ValueError("A failed attempt requires a reason")


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
    group_id: UUID
    source_document_id: UUID
    derived_artifact_id: UUID
    kind: ClaimKind
    value: str
    span: SourceSpan
    extraction_method: str
    extraction_payload: dict[str, Any]
    id: UUID = field(default_factory=uuid4)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        if not self.value.strip() or not self.extraction_method.strip():
            raise ValueError("Claim value and extraction method are required")


@dataclass(frozen=True, slots=True)
class Indicator:
    subject_id: UUID
    edition_id: UUID
    group_id: UUID
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
