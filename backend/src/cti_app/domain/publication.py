"""Canonical, renderer-independent publication model for an AutoWork brief."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date
from enum import StrEnum
from typing import Any

PUBLICATION_SCHEMA_VERSION = "1"


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
    schema_version: str
    title: str
    timeline: tuple[TimelineEntry, ...]
    synthesis: tuple[RichText, ...]
    indicators: tuple[IndicatorGroup, ...]
    sources: tuple[PublicationSource, ...]
    uncertainties: tuple[str, ...]
    analyst_note: RichText | None = None
    original_indicators: tuple[IndicatorGroup, ...] = ()

    def to_json(self) -> dict[str, Any]:
        """Return the stable JSON representation stored as the BRIEF artifact."""
        payload = asdict(self)
        for entry in payload["timeline"]:
            if entry["date"] is not None:
                entry["date"] = entry["date"].isoformat()
        return payload

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> BriefDocumentV1:
        def rich(items: list[dict[str, Any]]) -> RichText:
            return tuple(
                RichSpan(
                    kind=RichSpanKind(item["kind"]),
                    text=str(item.get("text", "")),
                    source_ids=tuple(item.get("source_ids", [])),
                )
                for item in items
            )

        def group(item: dict[str, Any]) -> IndicatorGroup:
            artifact_type = ArtifactType(item["artifact_type"])
            return IndicatorGroup(
                artifact_type=artifact_type,
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
        return cls(
            schema_version=str(payload.get("schema_version", PUBLICATION_SCHEMA_VERSION)),
            title=str(payload["title"]),
            timeline=tuple(
                TimelineEntry(
                    date=date.fromisoformat(item["date"]) if item.get("date") else None,
                    content=rich(item.get("content", [])),
                    source_ids=tuple(item.get("source_ids", [])),
                )
                for item in payload.get("timeline", [])
            ),
            synthesis=tuple(rich(item) for item in payload.get("synthesis", [])),
            indicators=tuple(group(item) for item in payload.get("indicators", [])),
            sources=tuple(
                PublicationSource(source_id=item["source_id"], canonical_url=item["canonical_url"])
                for item in payload.get("sources", [])
            ),
            uncertainties=tuple(payload.get("uncertainties", [])),
            analyst_note=rich(analyst) if analyst is not None else None,
            original_indicators=tuple(
                group(item) for item in payload.get("original_indicators", [])
            ),
        )
