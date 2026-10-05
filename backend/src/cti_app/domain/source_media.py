from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4


class SourceMediaStatus(StrEnum):
    CANDIDATE = "CANDIDATE"
    EXCLUDED_BY_RULE = "EXCLUDED_BY_RULE"
    ACCEPTED_FOR_REVIEW = "ACCEPTED_FOR_REVIEW"
    COLLECTION_FAILED = "COLLECTION_FAILED"
    PAGE_EXCERPT_NEEDED = "PAGE_EXCERPT_NEEDED"


class SourceMediaReasonCode(StrEnum):
    CANDIDATE_DISCOVERED = "candidate_discovered"
    ELIGIBLE_FOR_REVIEW = "eligible_for_review"
    BOILERPLATE_PATTERN = "boilerplate_pattern"
    NAVIGATION_LANDMARK = "navigation_landmark"
    RELATED_CONTENT_CARD = "related_content_card"
    DECORATIVE_ASSET = "decorative_asset"
    TRACKING_PIXEL = "tracking_pixel"
    BELOW_MINIMUM_DIMENSIONS = "below_minimum_dimensions"
    EXTREME_ASPECT_RATIO = "extreme_aspect_ratio"
    BELOW_MINIMUM_BYTES = "below_minimum_bytes"
    EXCEEDS_MAXIMUM_BYTES = "exceeds_maximum_bytes"
    IMAGE_TOO_LARGE_DIMENSIONS = "image_too_large_dimensions"
    UNSUPPORTED_IMAGE_TYPE = "unsupported_image_type"
    MISSING_SOURCE_URL = "missing_source_url"
    INVALID_SOURCE_URL = "invalid_source_url"
    UNSAFE_DESTINATION = "unsafe_destination"
    COLLECTION_SIZE_LIMIT = "collection_size_limit"
    COLLECTION_UNAVAILABLE = "collection_unavailable"
    COLLECTION_FAILED = "collection_failed"
    NOT_AN_IMAGE = "not_an_image"
    PDF_IMAGE_EXTRACTION_FAILED = "pdf_image_extraction_failed"
    DUPLICATE_EXACT_HASH = "duplicate_exact_hash"
    DUPLICATE_PERCEPTUAL_HASH = "duplicate_perceptual_hash"
    PDF_PAGE_EXCERPT_NEEDED = "pdf_page_excerpt_needed"


@dataclass(frozen=True, slots=True)
class SourceMediaRecord:
    """Canonical metadata for one media occurrence in an archived source."""

    subject_id: UUID
    source_document_id: UUID
    policy_version: str
    policy_sha256: str
    status: SourceMediaStatus
    reason_code: SourceMediaReasonCode
    id: UUID = field(default_factory=uuid4)
    blob_id: UUID | None = None
    sha256: str | None = None
    mime_type: str | None = None
    width: int | None = None
    height: int | None = None
    byte_size: int | None = None
    page: int | None = None
    page_bbox: dict[str, float] | None = None
    anchor: str | None = None
    dom_locator: str | None = None
    original_url: str | None = None
    requested_url: str | None = None
    final_url: str | None = None
    alt_text: str | None = None
    caption_text: str | None = None
    nearby_heading_text: str | None = None
    collection_diagnostics: dict[str, Any] = field(default_factory=dict)
    perceptual_hash: str | None = None
    collected_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        if not self.policy_version.strip():
            raise ValueError("Source media policy version must be non-empty")
        if len(self.policy_sha256) != 64 or any(
            character not in "0123456789abcdef" for character in self.policy_sha256
        ):
            raise ValueError("Source media policy SHA-256 must be lowercase hexadecimal")
        if self.page is not None and self.page < 1:
            raise ValueError("Source media page must be positive")
        if (self.width is not None and self.width < 1) or (
            self.height is not None and self.height < 1
        ):
            raise ValueError("Source media dimensions must be positive")
        if self.byte_size is not None and self.byte_size < 0:
            raise ValueError("Source media byte size must not be negative")
        if self.sha256 is not None and (
            len(self.sha256) != 64
            or any(character not in "0123456789abcdef" for character in self.sha256)
        ):
            raise ValueError("Source media SHA-256 must be lowercase hexadecimal")
        if self.collected_at.tzinfo is None:
            raise ValueError("Source media collection time must be timezone-aware")
