"""Canonical, renderer-independent publication models.

``BriefDocumentV1`` and ``PublicationDocumentV2`` remain historical read models.
New Production writes use ``PublicationDocumentV3`` and readers dispatch by
``schema_version``.
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
PUBLICATION_DOCUMENT_V3_SCHEMA_VERSION = "3"


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
) -> BriefDocumentV1 | PublicationDocumentV2 | PublicationDocumentV3:
    """Read canonical V3 and isolated historical publication payloads."""

    schema_version = str(payload.get("schema_version", LEGACY_PUBLICATION_SCHEMA_VERSION))
    if schema_version == LEGACY_PUBLICATION_SCHEMA_VERSION:
        return BriefDocumentV1.from_json(payload)
    if schema_version == PUBLICATION_SCHEMA_VERSION:
        return PublicationDocumentV2.from_json(payload)
    if schema_version == PUBLICATION_DOCUMENT_V3_SCHEMA_VERSION:
        return PublicationDocumentV3.from_json(payload)
    raise ValueError(f"unsupported publication document schema_version={schema_version!r}")


class PublicationEvidenceKind(StrEnum):
    FACT = "fact"
    EVENT = "event"
    INDICATOR = "indicator"
    RULE = "rule"


class PublicationAssemblyErrorCode(StrEnum):
    INPUTS_MISSING = "assembly_inputs_missing"
    INPUTS_MISMATCH = "assembly_inputs_mismatch"
    EVIDENCE_MISSING = "assembly_evidence_missing"
    SOURCE_MISSING = "assembly_source_missing"
    VALIDATION_FAILED = "assembly_validation_failed"


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


def _publication_v3_object(value: Any, label: str, fields: frozenset[str]) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be a JSON object")
    if any(not isinstance(key, str) for key in value):
        raise ValueError(f"{label} field names must be text")
    actual_fields = frozenset(value)
    if actual_fields != fields:
        missing = sorted(fields - actual_fields)
        unexpected = sorted(actual_fields - fields)
        raise ValueError(f"{label} fields are invalid (missing={missing}, unexpected={unexpected})")
    return value


def _publication_v3_array(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(f"{label} must be a JSON array")
    return value


def _publication_v3_string(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be text")
    return value


def _publication_v3_uuid(value: Any, label: str) -> UUID:
    text = _publication_v3_string(value, label)
    try:
        parsed = UUID(text)
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a UUID string") from exc
    if str(parsed) != text:
        raise ValueError(f"{label} must use canonical lowercase UUID form")
    return parsed


def _publication_v3_date(value: Any, label: str) -> date | None:
    if value is None:
        return None
    text = _publication_v3_string(value, label)
    try:
        parsed = date.fromisoformat(text)
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be an ISO date string or null") from exc
    if parsed.isoformat() != text:
        raise ValueError(f"{label} must use canonical ISO date form")
    return parsed


def _publication_v3_evidence_refs(value: Any) -> tuple[PublicationEvidenceRefV1, ...]:
    fields = frozenset({"source_document_id", "kind", "evidence_key"})
    return tuple(
        PublicationEvidenceRefV1(
            source_document_id=_publication_v3_uuid(
                item["source_document_id"], "Evidence source_document_id"
            ),
            kind=PublicationEvidenceKind(_publication_v3_string(item["kind"], "Evidence kind")),
            evidence_key=_publication_v3_string(item["evidence_key"], "Evidence key"),
        )
        for item in (
            _publication_v3_object(raw, "Evidence reference", fields)
            for raw in _publication_v3_array(value, "Evidence references")
        )
    )


def _publication_v3_source_ids(value: Any, label: str) -> tuple[UUID, ...]:
    return tuple(_publication_v3_uuid(item, label) for item in _publication_v3_array(value, label))


@dataclass(frozen=True, slots=True)
class PublicationDocumentV3:
    schema_version: str
    subject_id: UUID
    publication_language: str
    title: str
    lead: tuple[PublicationParagraphV1, ...]
    sections: tuple[PublicationSectionV1, ...]
    timeline: tuple[PublicationTimelineEntryV1, ...]
    indicators: tuple[PublicationIndicatorGroupV1, ...]
    sources: tuple[PublicationSourceV1, ...]
    uncertainties: tuple[PublicationUncertaintyV1, ...]

    def __post_init__(self) -> None:
        if self.schema_version != PUBLICATION_DOCUMENT_V3_SCHEMA_VERSION:
            raise ValueError(
                "PublicationDocumentV3 requires "
                f"schema_version={PUBLICATION_DOCUMENT_V3_SCHEMA_VERSION!r}"
            )
        if not isinstance(self.subject_id, UUID):
            raise ValueError("Publication subject identity must be a UUID")
        if not isinstance(self.publication_language, str) or not self.publication_language.strip():
            raise ValueError("Publication language must be non-empty text")
        if not isinstance(self.title, str) or not self.title.strip():
            raise ValueError("Publication title must be non-empty text")

        tuple_fields: tuple[tuple[str, Any, type[Any]], ...] = (
            ("lead", self.lead, PublicationParagraphV1),
            ("sections", self.sections, PublicationSectionV1),
            ("timeline", self.timeline, PublicationTimelineEntryV1),
            ("indicators", self.indicators, PublicationIndicatorGroupV1),
            ("sources", self.sources, PublicationSourceV1),
            ("uncertainties", self.uncertainties, PublicationUncertaintyV1),
        )
        for label, items, item_type in tuple_fields:
            if not isinstance(items, tuple):
                raise ValueError(f"Publication {label} must be a tuple")
            if any(not isinstance(item, item_type) for item in items):
                raise ValueError(f"Publication {label} contains an invalid value type")

        indicator_types = tuple(group.artifact_type for group in self.indicators)
        if len(set(indicator_types)) != len(indicator_types):
            raise ValueError("Publication indicator groups must not repeat artifact types")

        source_ids = tuple(source.source_document_id for source in self.sources)
        if len(set(source_ids)) != len(source_ids):
            raise ValueError("Publication sources must not repeat source_document_id values")
        if len(set(self.uncertainties)) != len(self.uncertainties):
            raise ValueError("Publication uncertainties must not repeat")

        used_source_ids = {
            ref.source_document_id for paragraph in self.lead for ref in paragraph.evidence_refs
        }
        used_source_ids.update(
            ref.source_document_id
            for section in self.sections
            for paragraph in section.paragraphs
            for ref in paragraph.evidence_refs
        )
        used_source_ids.update(
            ref.source_document_id for entry in self.timeline for ref in entry.evidence_refs
        )
        used_source_ids.update(
            source_id
            for group in self.indicators
            for indicator in group.indicators
            for source_id in indicator.source_document_ids
        )
        used_source_ids.update(
            source_id
            for uncertainty in self.uncertainties
            for source_id in uncertainty.source_document_ids
        )
        if used_source_ids != set(source_ids):
            unknown = sorted(map(str, used_source_ids - set(source_ids)))
            unused = sorted(map(str, set(source_ids) - used_source_ids))
            raise ValueError(
                "Publication sources must exactly match source identities used by the "
                f"document (unknown={unknown}, unused={unused})"
            )

        object.__setattr__(
            self,
            "indicators",
            tuple(sorted(self.indicators, key=lambda group: group.artifact_type.value)),
        )
        object.__setattr__(
            self,
            "sources",
            tuple(sorted(self.sources, key=lambda source: str(source.source_document_id))),
        )
        object.__setattr__(
            self,
            "uncertainties",
            tuple(
                sorted(
                    self.uncertainties,
                    key=lambda item: (
                        item.text,
                        tuple(map(str, item.source_document_ids)),
                    ),
                )
            ),
        )

    def to_json(self) -> dict[str, Any]:
        def evidence_refs_json(
            refs: tuple[PublicationEvidenceRefV1, ...],
        ) -> list[dict[str, str]]:
            return [
                {
                    "source_document_id": str(ref.source_document_id),
                    "kind": ref.kind.value,
                    "evidence_key": ref.evidence_key,
                }
                for ref in refs
            ]

        def paragraph_json(paragraph: PublicationParagraphV1) -> dict[str, Any]:
            return {
                "text": paragraph.text,
                "evidence_refs": evidence_refs_json(paragraph.evidence_refs),
            }

        return {
            "schema_version": self.schema_version,
            "subject_id": str(self.subject_id),
            "publication_language": self.publication_language,
            "title": self.title,
            "lead": [paragraph_json(paragraph) for paragraph in self.lead],
            "sections": [
                {
                    "kind": section.kind.value,
                    "heading": section.heading,
                    "paragraphs": [paragraph_json(paragraph) for paragraph in section.paragraphs],
                }
                for section in self.sections
            ],
            "timeline": [
                {
                    "event_date": entry.event_date.isoformat()
                    if entry.event_date is not None
                    else None,
                    "date_text": entry.date_text,
                    "text": entry.text,
                    "evidence_refs": evidence_refs_json(entry.evidence_refs),
                }
                for entry in self.timeline
            ],
            "indicators": [
                {
                    "artifact_type": group.artifact_type.value,
                    "indicators": [
                        {
                            "value": indicator.value,
                            "normalized_value": indicator.normalized_value,
                            "artifact_type": indicator.artifact_type.value,
                            "source_document_ids": [
                                str(source_id) for source_id in indicator.source_document_ids
                            ],
                        }
                        for indicator in group.indicators
                    ],
                }
                for group in self.indicators
            ],
            "sources": [
                {
                    "source_document_id": str(source.source_document_id),
                    "canonical_url": source.canonical_url,
                    "title": source.title,
                    "publisher": source.publisher,
                    "published_at": source.published_at.isoformat()
                    if source.published_at is not None
                    else None,
                    "tier": source.tier.value,
                    "kind": source.kind.value,
                    "role": source.role.value,
                }
                for source in self.sources
            ],
            "uncertainties": [
                {
                    "text": uncertainty.text,
                    "source_document_ids": [
                        str(source_id) for source_id in uncertainty.source_document_ids
                    ],
                }
                for uncertainty in self.uncertainties
            ],
        }

    @classmethod
    def from_json(cls, payload: Mapping[str, Any]) -> PublicationDocumentV3:
        document_fields = frozenset(
            {
                "schema_version",
                "subject_id",
                "publication_language",
                "title",
                "lead",
                "sections",
                "timeline",
                "indicators",
                "sources",
                "uncertainties",
            }
        )

        def paragraph(raw: Any) -> PublicationParagraphV1:
            item = _publication_v3_object(
                raw, "Publication paragraph", frozenset({"text", "evidence_refs"})
            )
            return PublicationParagraphV1(
                text=_publication_v3_string(item["text"], "Paragraph text"),
                evidence_refs=_publication_v3_evidence_refs(item["evidence_refs"]),
            )

        document = _publication_v3_object(payload, "PublicationDocumentV3", document_fields)
        sections = tuple(
            PublicationSectionV1(
                kind=PublicationSectionKind(_publication_v3_string(item["kind"], "Section kind")),
                heading=_publication_v3_string(item["heading"], "Section heading"),
                paragraphs=tuple(
                    paragraph(nested)
                    for nested in _publication_v3_array(item["paragraphs"], "Section paragraphs")
                ),
            )
            for item in (
                _publication_v3_object(
                    raw, "Publication section", frozenset({"kind", "heading", "paragraphs"})
                )
                for raw in _publication_v3_array(document["sections"], "Sections")
            )
        )
        timeline = tuple(
            PublicationTimelineEntryV1(
                event_date=_publication_v3_date(item["event_date"], "Timeline event_date"),
                date_text=(
                    None
                    if item["date_text"] is None
                    else _publication_v3_string(item["date_text"], "Timeline date_text")
                ),
                text=_publication_v3_string(item["text"], "Timeline text"),
                evidence_refs=_publication_v3_evidence_refs(item["evidence_refs"]),
            )
            for item in (
                _publication_v3_object(
                    raw,
                    "Publication timeline entry",
                    frozenset({"event_date", "date_text", "text", "evidence_refs"}),
                )
                for raw in _publication_v3_array(document["timeline"], "Timeline")
            )
        )
        indicators = tuple(
            PublicationIndicatorGroupV1(
                artifact_type=ArtifactType(
                    _publication_v3_string(item["artifact_type"], "Indicator group type")
                ),
                indicators=tuple(
                    PublicationIndicatorV1(
                        value=_publication_v3_string(nested["value"], "Indicator value"),
                        normalized_value=_publication_v3_string(
                            nested["normalized_value"], "Indicator normalized_value"
                        ),
                        artifact_type=ArtifactType(
                            _publication_v3_string(
                                nested["artifact_type"], "Indicator artifact_type"
                            )
                        ),
                        source_document_ids=_publication_v3_source_ids(
                            nested["source_document_ids"], "Indicator source_document_ids"
                        ),
                    )
                    for nested in (
                        _publication_v3_object(
                            raw_indicator,
                            "Publication indicator",
                            frozenset(
                                {
                                    "value",
                                    "normalized_value",
                                    "artifact_type",
                                    "source_document_ids",
                                }
                            ),
                        )
                        for raw_indicator in _publication_v3_array(
                            item["indicators"], "Indicators in group"
                        )
                    )
                ),
            )
            for item in (
                _publication_v3_object(
                    raw,
                    "Publication indicator group",
                    frozenset({"artifact_type", "indicators"}),
                )
                for raw in _publication_v3_array(document["indicators"], "Indicator groups")
            )
        )
        sources = tuple(
            PublicationSourceV1(
                source_document_id=_publication_v3_uuid(
                    item["source_document_id"], "Source source_document_id"
                ),
                canonical_url=_publication_v3_string(item["canonical_url"], "Source canonical_url"),
                title=(
                    None
                    if item["title"] is None
                    else _publication_v3_string(item["title"], "Source title")
                ),
                publisher=(
                    None
                    if item["publisher"] is None
                    else _publication_v3_string(item["publisher"], "Source publisher")
                ),
                published_at=_publication_v3_date(item["published_at"], "Source published_at"),
                tier=ProductionReferenceTier(_publication_v3_string(item["tier"], "Source tier")),
                kind=ProductionReferenceKind(_publication_v3_string(item["kind"], "Source kind")),
                role=SourceRole(_publication_v3_string(item["role"], "Source role")),
            )
            for item in (
                _publication_v3_object(
                    raw,
                    "Publication source",
                    frozenset(
                        {
                            "source_document_id",
                            "canonical_url",
                            "title",
                            "publisher",
                            "published_at",
                            "tier",
                            "kind",
                            "role",
                        }
                    ),
                )
                for raw in _publication_v3_array(document["sources"], "Sources")
            )
        )
        uncertainties = tuple(
            PublicationUncertaintyV1(
                text=_publication_v3_string(item["text"], "Uncertainty text"),
                source_document_ids=_publication_v3_source_ids(
                    item["source_document_ids"], "Uncertainty source_document_ids"
                ),
            )
            for item in (
                _publication_v3_object(
                    raw,
                    "Publication uncertainty",
                    frozenset({"text", "source_document_ids"}),
                )
                for raw in _publication_v3_array(document["uncertainties"], "Uncertainties")
            )
        )
        return cls(
            schema_version=_publication_v3_string(
                document["schema_version"], "Publication schema_version"
            ),
            subject_id=_publication_v3_uuid(document["subject_id"], "Publication subject_id"),
            publication_language=_publication_v3_string(
                document["publication_language"], "Publication language"
            ),
            title=_publication_v3_string(document["title"], "Publication title"),
            lead=tuple(paragraph(raw) for raw in _publication_v3_array(document["lead"], "Lead")),
            sections=sections,
            timeline=timeline,
            indicators=indicators,
            sources=sources,
            uncertainties=uncertainties,
        )
