"""Canonical, renderer-independent publication models.

``BriefDocumentV1`` remains a historical read model.  New production writes
use ``PublicationDocumentV2`` and callers cross the V1/V2 boundary through
``publication_document_from_json``.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import date
from enum import StrEnum
from typing import Any
from uuid import UUID

from cti_app.domain.discovery import SourceRole, canonicalize_http_url
from cti_app.domain.production_references import ProductionReferenceKind, ProductionReferenceTier

LEGACY_PUBLICATION_SCHEMA_VERSION = "1"
PUBLICATION_SCHEMA_VERSION = "2"


class ArtifactType(StrEnum):
    IP = "ip"
    DOMAIN = "domain"
    URL = "url"
    HASH = "hash"
    EMAIL = "email"
    FILEPATH = "filepath"
    FILENAME = "filename"
    CVE = "cve"
    YARA_RULE = "yara_rule"
    SIGMA_RULE = "sigma_rule"
    SURICATA_RULE = "suricata_rule"
    OTHER = "other"


# These are the only artifact types rendered in the publication IOC section.
# The enum is the vocabulary; this set is the single classification used by
# extraction, repair and review projections.
PUBLICATION_IOC_ARTIFACT_TYPES = frozenset(
    {
        ArtifactType.IP,
        ArtifactType.DOMAIN,
        ArtifactType.URL,
        ArtifactType.EMAIL,
        ArtifactType.HASH,
    }
)


def is_publication_ioc_artifact_type(value: ArtifactType | str | None) -> bool:
    """Return whether an artifact belongs to the final IOC section."""
    try:
        return ArtifactType(value) in PUBLICATION_IOC_ARTIFACT_TYPES if value is not None else False
    except ValueError:
        return False


class RichSpanKind(StrEnum):
    TEXT = "text"
    EMPHASIS = "emphasis"
    ACTOR = "actor"
    MALWARE = "malware"
    TOOL = "tool"
    PRODUCT = "product"
    TECHNICAL = "technical"
    IOC = "ioc"
    CODE = "code"
    CITATION = "citation"


@dataclass(frozen=True)
class RichSpan:
    kind: RichSpanKind
    text: str
    source_ids: tuple[str, ...] = ()


type RichText = tuple[RichSpan, ...]


@dataclass(frozen=True)
class TimelineEntry:
    date: date | None
    content: RichText
    source_ids: tuple[str, ...]


@dataclass(frozen=True)
class Indicator:
    value: str
    normalized_value: str
    artifact_type: ArtifactType
    source_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class IndicatorGroup:
    artifact_type: ArtifactType
    values: tuple[Indicator, ...]


@dataclass(frozen=True)
class PublicationSource:
    source_id: str
    canonical_url: str


@dataclass(frozen=True)
class BriefDocumentV1:
    """Historical publication document kept for read compatibility."""

    schema_version: str
    title: str
    timeline: tuple[TimelineEntry, ...]
    synthesis: tuple[RichText, ...]
    indicators: tuple[IndicatorGroup, ...]
    sources: tuple[PublicationSource, ...]
    uncertainties: tuple[str, ...]
    analyst_note: RichText | None = None
    original_indicators: tuple[IndicatorGroup, ...] = ()

    def __post_init__(self) -> None:
        if self.schema_version != LEGACY_PUBLICATION_SCHEMA_VERSION:
            raise ValueError(
                f"BriefDocumentV1 requires schema_version={LEGACY_PUBLICATION_SCHEMA_VERSION!r}"
            )

    def to_json(self) -> dict[str, Any]:
        """Return the historical JSON representation stored as a BRIEF artifact."""
        payload = asdict(self)
        for entry in payload["timeline"]:
            if entry["date"] is not None:
                entry["date"] = entry["date"].isoformat()
        return payload

    @classmethod
    def from_json(cls, payload: Mapping[str, Any]) -> BriefDocumentV1:
        return cls(**_publication_document_fields(payload, LEGACY_PUBLICATION_SCHEMA_VERSION))


@dataclass(frozen=True)
class PublicationDocumentV2:
    """Generic document written by the unified publication pipeline."""

    schema_version: str
    title: str
    timeline: tuple[TimelineEntry, ...]
    synthesis: tuple[RichText, ...]
    indicators: tuple[IndicatorGroup, ...]
    sources: tuple[PublicationSource, ...]
    uncertainties: tuple[str, ...]
    analyst_note: RichText | None = None
    original_indicators: tuple[IndicatorGroup, ...] = ()

    def __post_init__(self) -> None:
        if self.schema_version != PUBLICATION_SCHEMA_VERSION:
            raise ValueError(
                f"PublicationDocumentV2 requires schema_version={PUBLICATION_SCHEMA_VERSION!r}"
            )

    def to_json(self) -> dict[str, Any]:
        payload = asdict(self)
        for entry in payload["timeline"]:
            if entry["date"] is not None:
                entry["date"] = entry["date"].isoformat()
        return payload

    @classmethod
    def from_json(cls, payload: Mapping[str, Any]) -> PublicationDocumentV2:
        return cls(**_publication_document_fields(payload, PUBLICATION_SCHEMA_VERSION))


def _publication_document_fields(
    payload: Mapping[str, Any], default_schema_version: str
) -> dict[str, Any]:
    def rich(items: list[Mapping[str, Any]]) -> RichText:
        return tuple(
            RichSpan(
                kind=RichSpanKind(item["kind"]),
                text=str(item.get("text", "")),
                source_ids=tuple(item.get("source_ids", [])),
            )
            for item in items
        )

    def group(item: Mapping[str, Any]) -> IndicatorGroup:
        return IndicatorGroup(
            artifact_type=ArtifactType(item["artifact_type"]),
            values=tuple(
                Indicator(
                    value=value["value"],
                    normalized_value=value["normalized_value"],
                    artifact_type=ArtifactType(value["artifact_type"]),
                    source_ids=tuple(value.get("source_ids", [])),
                )
                for value in item.get("values", [])
            ),
        )

    analyst = payload.get("analyst_note")
    return {
        "schema_version": str(payload.get("schema_version", default_schema_version)),
        "title": str(payload["title"]),
        "timeline": tuple(
            TimelineEntry(
                date=date.fromisoformat(item["date"]) if item.get("date") else None,
                content=rich(item.get("content", [])),
                source_ids=tuple(item.get("source_ids", [])),
            )
            for item in payload.get("timeline", [])
        ),
        "synthesis": tuple(rich(item) for item in payload.get("synthesis", [])),
        "indicators": tuple(group(item) for item in payload.get("indicators", [])),
        "sources": tuple(
            PublicationSource(source_id=item["source_id"], canonical_url=item["canonical_url"])
            for item in payload.get("sources", [])
        ),
        "uncertainties": tuple(payload.get("uncertainties", [])),
        "analyst_note": rich(analyst) if analyst is not None else None,
        "original_indicators": tuple(
            group(item) for item in payload.get("original_indicators", [])
        ),
    }


def publication_document_from_json(
    payload: Mapping[str, Any],
) -> BriefDocumentV1 | PublicationDocumentV2:
    """Read either the historical V1 or current V2 publication payload."""

    schema_version = str(payload.get("schema_version", LEGACY_PUBLICATION_SCHEMA_VERSION))
    if schema_version == LEGACY_PUBLICATION_SCHEMA_VERSION:
        return BriefDocumentV1.from_json(payload)
    if schema_version == PUBLICATION_SCHEMA_VERSION:
        return PublicationDocumentV2.from_json(payload)
    raise ValueError(f"unsupported publication document schema_version={schema_version!r}")


class PublicationEvidenceKind(StrEnum):
    FACT = "fact"
    EVENT = "event"
    INDICATOR = "indicator"
    RULE = "rule"


class PublicationSectionKind(StrEnum):
    OVERVIEW = "overview"
    CAMPAIGN = "campaign"
    INFECTION_CHAIN = "infection_chain"
    TECHNICAL = "technical"
    VICTIMOLOGY = "victimology"
    INFRASTRUCTURE = "infrastructure"
    DETECTION = "detection"
    IMPACT = "impact"
    OTHER = "other"


_PUBLICATION_EVIDENCE_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _normalize_publication_source_document_ids(
    source_document_ids: tuple[UUID, ...], label: str
) -> tuple[UUID, ...]:
    if not isinstance(source_document_ids, tuple) or not source_document_ids:
        raise ValueError(f"{label} requires a non-empty tuple of source document identities")
    if any(not isinstance(source_document_id, UUID) for source_document_id in source_document_ids):
        raise ValueError(f"{label} source document identities must be UUIDs")
    if len(set(source_document_ids)) != len(source_document_ids):
        raise ValueError(f"{label} must not repeat source document identities")
    return tuple(sorted(source_document_ids, key=str))


def _normalize_publication_evidence_refs(
    refs: tuple[PublicationEvidenceRefV1, ...], label: str
) -> tuple[PublicationEvidenceRefV1, ...]:
    if not isinstance(refs, tuple) or not refs:
        raise ValueError(f"{label} requires a tuple of evidence references")
    if any(not isinstance(ref, PublicationEvidenceRefV1) for ref in refs):
        raise ValueError(f"{label} evidence references have an invalid type")
    if len(set(refs)) != len(refs):
        raise ValueError(f"{label} must not repeat evidence references")
    return tuple(
        sorted(
            refs,
            key=lambda ref: (
                str(ref.source_document_id),
                ref.kind.value,
                ref.evidence_key,
            ),
        )
    )


@dataclass(frozen=True, slots=True)
class PublicationEvidenceRefV1:
    source_document_id: UUID
    kind: PublicationEvidenceKind
    evidence_key: str

    def __post_init__(self) -> None:
        if not isinstance(self.source_document_id, UUID):
            raise ValueError("Evidence source document identity must be a UUID")
        if not isinstance(self.kind, PublicationEvidenceKind):
            raise ValueError("Evidence kind is invalid")
        if (
            not isinstance(self.evidence_key, str)
            or _PUBLICATION_EVIDENCE_SHA256.fullmatch(self.evidence_key) is None
        ):
            raise ValueError("Evidence key must be a lowercase SHA-256")


@dataclass(frozen=True, slots=True)
class PublicationParagraphV1:
    text: str
    evidence_refs: tuple[PublicationEvidenceRefV1, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.text, str) or not self.text.strip():
            raise ValueError("Publication paragraph text must be non-empty text")
        object.__setattr__(
            self,
            "evidence_refs",
            _normalize_publication_evidence_refs(self.evidence_refs, "Paragraph"),
        )


@dataclass(frozen=True, slots=True)
class PublicationSectionV1:
    kind: PublicationSectionKind
    heading: str
    paragraphs: tuple[PublicationParagraphV1, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.kind, PublicationSectionKind):
            raise ValueError("Publication section kind is invalid")
        if not isinstance(self.heading, str) or not self.heading.strip():
            raise ValueError("Publication section heading must be non-empty text")
        if not isinstance(self.paragraphs, tuple) or not self.paragraphs:
            raise ValueError("Publication section paragraphs must be a non-empty tuple")
        if any(not isinstance(paragraph, PublicationParagraphV1) for paragraph in self.paragraphs):
            raise ValueError("Publication section paragraphs have an invalid type")


@dataclass(frozen=True, slots=True)
class PublicationTimelineEntryV1:
    event_date: date | None
    date_text: str | None
    text: str
    evidence_refs: tuple[PublicationEvidenceRefV1, ...]

    def __post_init__(self) -> None:
        if self.event_date is not None and type(self.event_date) is not date:
            raise ValueError("Timeline event date must be a date or None")
        if self.date_text is not None and (
            not isinstance(self.date_text, str) or not self.date_text.strip()
        ):
            raise ValueError("Timeline date text must be non-empty text")
        if not isinstance(self.text, str) or not self.text.strip():
            raise ValueError("Timeline text must be non-empty text")
        object.__setattr__(
            self,
            "evidence_refs",
            _normalize_publication_evidence_refs(self.evidence_refs, "Timeline entry"),
        )


@dataclass(frozen=True, slots=True)
class PublicationIndicatorV1:
    value: str
    normalized_value: str
    artifact_type: ArtifactType
    source_document_ids: tuple[UUID, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.value, str) or not self.value.strip():
            raise ValueError("Publication indicator value must be non-empty text")
        if not isinstance(self.normalized_value, str) or not self.normalized_value.strip():
            raise ValueError("Publication indicator normalized value must be non-empty text")
        if not isinstance(self.artifact_type, ArtifactType) or (
            self.artifact_type not in PUBLICATION_IOC_ARTIFACT_TYPES
        ):
            raise ValueError("Publication indicator artifact type is not publishable")
        object.__setattr__(
            self,
            "source_document_ids",
            _normalize_publication_source_document_ids(
                self.source_document_ids, "Publication indicator"
            ),
        )


@dataclass(frozen=True, slots=True)
class PublicationIndicatorGroupV1:
    artifact_type: ArtifactType
    indicators: tuple[PublicationIndicatorV1, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.artifact_type, ArtifactType) or (
            self.artifact_type not in PUBLICATION_IOC_ARTIFACT_TYPES
        ):
            raise ValueError("Publication indicator group artifact type is not publishable")
        if not isinstance(self.indicators, tuple) or not self.indicators:
            raise ValueError("Publication indicator group requires a non-empty tuple")
        if any(not isinstance(indicator, PublicationIndicatorV1) for indicator in self.indicators):
            raise ValueError("Publication indicator group contains an invalid indicator")
        if any(indicator.artifact_type is not self.artifact_type for indicator in self.indicators):
            raise ValueError("Publication indicator artifact type must match its group")
        normalized_values = tuple(indicator.normalized_value for indicator in self.indicators)
        if len(set(normalized_values)) != len(normalized_values):
            raise ValueError("Publication indicator group must not repeat normalized values")
        object.__setattr__(
            self,
            "indicators",
            tuple(
                sorted(
                    self.indicators,
                    key=lambda indicator: (
                        indicator.normalized_value,
                        indicator.value,
                        tuple(map(str, indicator.source_document_ids)),
                    ),
                )
            ),
        )


@dataclass(frozen=True, slots=True)
class PublicationSourceV1:
    source_document_id: UUID
    canonical_url: str
    title: str | None
    publisher: str | None
    published_at: date | None
    tier: ProductionReferenceTier
    kind: ProductionReferenceKind
    role: SourceRole

    def __post_init__(self) -> None:
        if not isinstance(self.source_document_id, UUID):
            raise ValueError("Publication source document identity must be a UUID")
        try:
            canonical = canonicalize_http_url(self.canonical_url)
        except (AttributeError, TypeError, ValueError) as exc:
            raise ValueError("Publication source URL must be a canonical HTTP(S) URL") from exc
        if canonical != self.canonical_url:
            raise ValueError("Publication source URL must be canonical")
        if not isinstance(self.tier, ProductionReferenceTier):
            raise ValueError("Publication source tier is invalid")
        if not isinstance(self.kind, ProductionReferenceKind):
            raise ValueError("Publication source kind is invalid")
        if not isinstance(self.role, SourceRole):
            raise ValueError("Publication source role is invalid")
        if self.title is not None and not isinstance(self.title, str):
            raise ValueError("Publication source title must be text or None")
        if self.publisher is not None and not isinstance(self.publisher, str):
            raise ValueError("Publication source publisher must be text or None")
        if self.published_at is not None and type(self.published_at) is not date:
            raise ValueError("Publication source date must be a date or None")


@dataclass(frozen=True, slots=True)
class PublicationUncertaintyV1:
    text: str
    source_document_ids: tuple[UUID, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.text, str) or not self.text.strip():
            raise ValueError("Publication uncertainty text must be non-empty text")
        object.__setattr__(
            self,
            "source_document_ids",
            _normalize_publication_source_document_ids(
                self.source_document_ids, "Publication uncertainty"
            ),
        )
