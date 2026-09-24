"""Canonical source corpus contracts for editorial production."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from uuid import UUID

from cti_app.domain.collection import CollectionState
from cti_app.domain.discovery import SourceRole, canonicalize_http_url


class ProductionReferenceTier(StrEnum):
    CORE = "core"
    SUPPORTING = "supporting"
    TECHNICAL = "technical"


class ProductionReferenceKind(StrEnum):
    PUBLICATION = "publication"
    TECHNICAL_RESOURCE = "technical_resource"


class ProductionReferenceResearchStatus(StrEnum):
    COMPLETED = "completed"


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_TIER_ORDER = {
    ProductionReferenceTier.CORE: 0,
    ProductionReferenceTier.SUPPORTING: 1,
    ProductionReferenceTier.TECHNICAL: 2,
}
_EXTRACTION_STATES = {
    CollectionState.ARCHIVED,
    CollectionState.EXTRACTED,
    CollectionState.COMPLETED,
}


@dataclass(frozen=True, slots=True, kw_only=True)
class ProductionReferenceSourceV1:
    canonical_url: str
    tier: ProductionReferenceTier
    kind: ProductionReferenceKind
    role: SourceRole
    title: str | None
    publisher: str | None
    published_at: date | None
    source_collection_id: UUID | None
    source_document_id: UUID | None
    discovery_candidate_ids: tuple[UUID, ...]
    collection_state: CollectionState
    content_sha256: str | None
    relevance_reason: str | None
    proposed_by_model: bool
    eligible_for_extraction: bool

    def __post_init__(self) -> None:
        try:
            canonical = canonicalize_http_url(self.canonical_url)
        except (AttributeError, ValueError) as exc:
            raise ValueError("Reference URL must be a canonical HTTP(S) URL") from exc
        if canonical != self.canonical_url:
            raise ValueError("Reference URL must be canonical")
        if not isinstance(self.tier, ProductionReferenceTier):
            raise ValueError("Reference tier is invalid")
        if not isinstance(self.kind, ProductionReferenceKind):
            raise ValueError("Reference kind is invalid")
        if not isinstance(self.role, SourceRole):
            raise ValueError("Reference role is invalid")
        if not isinstance(self.collection_state, CollectionState):
            raise ValueError("Reference collection state is invalid")
        if self.title is not None and not isinstance(self.title, str):
            raise ValueError("Reference title must be text or None")
        if self.publisher is not None and not isinstance(self.publisher, str):
            raise ValueError("Reference publisher must be text or None")
        if self.published_at is not None and (
            not isinstance(self.published_at, date) or hasattr(self.published_at, "hour")
        ):
            raise ValueError("Reference publication date must be a date or None")
        if self.source_collection_id is not None and not isinstance(
            self.source_collection_id, UUID
        ):
            raise ValueError("Reference collection identity must be a UUID or None")
        if self.source_document_id is not None and not isinstance(self.source_document_id, UUID):
            raise ValueError("Reference document identity must be a UUID or None")
        if not isinstance(self.discovery_candidate_ids, tuple) or any(
            not isinstance(item, UUID) for item in self.discovery_candidate_ids
        ):
            raise ValueError("Reference discovery candidate identities must be UUIDs")
        if self.content_sha256 is not None and not _SHA256.fullmatch(self.content_sha256):
            raise ValueError("Reference content hash must be a lowercase SHA-256")
        if self.relevance_reason is not None and not isinstance(self.relevance_reason, str):
            raise ValueError("Reference relevance reason must be text or None")
        if type(self.proposed_by_model) is not bool:
            raise ValueError("Reference model provenance flag must be boolean")
        if self.tier is ProductionReferenceTier.CORE and self.proposed_by_model:
            raise ValueError("CORE references must come from the production input snapshot")

        eligible = (
            self.collection_state in _EXTRACTION_STATES
            and self.source_document_id is not None
            and self.content_sha256 is not None
        )
        if type(self.eligible_for_extraction) is not bool or (
            self.eligible_for_extraction is not eligible
        ):
            raise ValueError("Reference extraction eligibility does not match its archive state")


@dataclass(frozen=True, slots=True, kw_only=True)
class ProductionReferenceCorpusV1:
    schema_version: int
    subject_id: UUID
    research_date: date
    production_input_hash: str
    research_status: ProductionReferenceResearchStatus
    sources: tuple[ProductionReferenceSourceV1, ...]
    warnings: tuple[str, ...]

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError("Production reference corpus schema version must be 1")
        if not isinstance(self.subject_id, UUID):
            raise ValueError("Production reference subject identity must be a UUID")
        if not isinstance(self.research_date, date) or hasattr(self.research_date, "hour"):
            raise ValueError("Production reference research date must be a date")
        if not isinstance(self.production_input_hash, str) or not _SHA256.fullmatch(
            self.production_input_hash
        ):
            raise ValueError("Production input hash must be a lowercase SHA-256")
        if not isinstance(self.research_status, ProductionReferenceResearchStatus):
            raise ValueError("Production reference research status is invalid")
        if not isinstance(self.sources, tuple) or any(
            not isinstance(source, ProductionReferenceSourceV1) for source in self.sources
        ):
            raise ValueError("Production reference sources must be a tuple of V1 sources")
        if not isinstance(self.warnings, tuple) or any(
            not isinstance(warning, str) for warning in self.warnings
        ):
            raise ValueError("Production reference warnings must be a tuple of strings")

        # Deduplicate solely by canonical URL. A snapshot CORE entry wins over
        # any model proposal; among model proposals the first valid entry wins.
        by_url: dict[str, ProductionReferenceSourceV1] = {}
        for source in self.sources:
            current = by_url.get(source.canonical_url)
            if current is None or (
                source.tier is ProductionReferenceTier.CORE
                and current.tier is not ProductionReferenceTier.CORE
            ):
                by_url[source.canonical_url] = source
        ordered = tuple(
            sorted(
                by_url.values(),
                key=lambda source: (_TIER_ORDER[source.tier], source.canonical_url),
            )
        )
        object.__setattr__(self, "sources", ordered)
