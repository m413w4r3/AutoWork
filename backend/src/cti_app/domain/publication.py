"""Canonical, renderer-independent publication models.

``BriefDocumentV1`` remains a historical read model.  New production writes
use ``PublicationDocumentV2`` and callers cross the V1/V2 boundary through
``publication_document_from_json``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import date
from enum import StrEnum
from typing import Any

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
