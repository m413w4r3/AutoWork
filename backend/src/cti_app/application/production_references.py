"""Deterministic serialization and compatibility projections for references."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from typing import Any
from uuid import UUID

from cti_app.application.production_parsers import (
    ParsedEvent,
    ParseResult,
    ReferenceReport,
    _fields,
    _fold,
    _is_explicit_unknown,
    _parse_date,
    _split_blocks,
    normalize_text,
    parse_reference_report,
)
from cti_app.domain.collection import CollectionState
from cti_app.domain.discovery import SourceRole, canonicalize_http_url
from cti_app.domain.production_references import (
    ProductionReferenceCorpusV1,
    ProductionReferenceKind,
    ProductionReferenceResearchStatus,
    ProductionReferenceSourceV1,
    ProductionReferenceTier,
)

_CORPUS_KEYS = {
    "schema_version",
    "subject_id",
    "research_date",
    "production_input_hash",
    "research_status",
    "sources",
    "warnings",
}
_SOURCE_KEYS = {
    "canonical_url",
    "tier",
    "kind",
    "role",
    "title",
    "publisher",
    "published_at",
    "source_collection_id",
    "source_document_id",
    "discovery_candidate_ids",
    "collection_state",
    "content_sha256",
    "relevance_reason",
    "proposed_by_model",
    "eligible_for_extraction",
}
_SOURCE_BLOCKS = {"source": "source"}


@dataclass(frozen=True, slots=True)
class ProductionReferenceProposal:
    canonical_url: str
    title: str | None
    publisher: str | None
    published_at: date | None
    role: SourceRole
    kind: ProductionReferenceKind
    relevance_reason: str

    @property
    def tier(self) -> ProductionReferenceTier:
        if self.kind is ProductionReferenceKind.PUBLICATION:
            return ProductionReferenceTier.SUPPORTING
        return ProductionReferenceTier.TECHNICAL


def production_reference_corpus_to_json(
    corpus: ProductionReferenceCorpusV1,
) -> dict[str, Any]:
    """Return the versioned, JSON-compatible canonical corpus payload."""
    if not isinstance(corpus, ProductionReferenceCorpusV1):
        raise ValueError("Expected a ProductionReferenceCorpusV1")
    return {
        "schema_version": corpus.schema_version,
        "subject_id": str(corpus.subject_id),
        "research_date": corpus.research_date.isoformat(),
        "production_input_hash": corpus.production_input_hash,
        "research_status": corpus.research_status.value,
        "sources": [
            {
                "canonical_url": source.canonical_url,
                "tier": source.tier.value,
                "kind": source.kind.value,
                "role": source.role.value,
                "title": source.title,
                "publisher": source.publisher,
                "published_at": (
                    source.published_at.isoformat() if source.published_at is not None else None
                ),
                "source_collection_id": (
                    str(source.source_collection_id)
                    if source.source_collection_id is not None
                    else None
                ),
                "source_document_id": (
                    str(source.source_document_id)
                    if source.source_document_id is not None
                    else None
                ),
                "discovery_candidate_ids": [
                    str(candidate_id) for candidate_id in source.discovery_candidate_ids
                ],
                "collection_state": source.collection_state.value,
                "content_sha256": source.content_sha256,
                "relevance_reason": source.relevance_reason,
                "proposed_by_model": source.proposed_by_model,
                "eligible_for_extraction": source.eligible_for_extraction,
            }
            for source in corpus.sources
        ],
        "warnings": list(corpus.warnings),
    }


def production_reference_corpus_from_json(
    payload: Mapping[str, Any],
) -> ProductionReferenceCorpusV1:
    """Load a corpus payload, rejecting missing, extra, and mistyped fields."""
    if not isinstance(payload, Mapping) or set(payload) != _CORPUS_KEYS:
        raise ValueError("Production reference corpus payload has an invalid shape")
    if type(payload["schema_version"]) is not int or payload["schema_version"] != 1:
        raise ValueError("Production reference corpus schema version must be 1")
    raw_sources = payload["sources"]
    raw_warnings = payload["warnings"]
    if not isinstance(raw_sources, list) or not isinstance(raw_warnings, list):
        raise ValueError("Production reference sources and warnings must be arrays")
    if any(not isinstance(warning, str) for warning in raw_warnings):
        raise ValueError("Production reference warnings must be strings")

    sources: list[ProductionReferenceSourceV1] = []
    for raw_source in raw_sources:
        if not isinstance(raw_source, Mapping) or set(raw_source) != _SOURCE_KEYS:
            raise ValueError("Production reference source has an invalid shape")
        candidates = raw_source["discovery_candidate_ids"]
        if not isinstance(candidates, list):
            raise ValueError("Discovery candidate identities must be an array")
        sources.append(
            ProductionReferenceSourceV1(
                canonical_url=_required_string(raw_source, "canonical_url"),
                tier=_enum(ProductionReferenceTier, raw_source["tier"], "tier"),
                kind=_enum(ProductionReferenceKind, raw_source["kind"], "kind"),
                role=_enum(SourceRole, raw_source["role"], "role"),
                title=_optional_string(raw_source, "title"),
                publisher=_optional_string(raw_source, "publisher"),
                published_at=_optional_date(raw_source, "published_at"),
                source_collection_id=_optional_uuid(raw_source, "source_collection_id"),
                source_document_id=_optional_uuid(raw_source, "source_document_id"),
                discovery_candidate_ids=tuple(
                    _uuid(item, "discovery_candidate_ids") for item in candidates
                ),
                collection_state=_enum(
                    CollectionState, raw_source["collection_state"], "collection_state"
                ),
                content_sha256=_optional_string(raw_source, "content_sha256"),
                relevance_reason=_optional_string(raw_source, "relevance_reason"),
                proposed_by_model=_required_bool(raw_source, "proposed_by_model"),
                eligible_for_extraction=_required_bool(raw_source, "eligible_for_extraction"),
            )
        )
    return ProductionReferenceCorpusV1(
        schema_version=1,
        subject_id=_uuid(payload["subject_id"], "subject_id"),
        research_date=_date(payload["research_date"], "research_date"),
        production_input_hash=_required_string(payload, "production_input_hash"),
        research_status=_enum(
            ProductionReferenceResearchStatus,
            payload["research_status"],
            "research_status",
        ),
        sources=tuple(sources),
        warnings=tuple(raw_warnings),
    )


def parse_production_reference_proposals(
    text: str,
    research_date: date,
) -> ParseResult[tuple[ProductionReferenceProposal, ...]]:
    """Parse only SOURCE blocks from the temporary Q1 Markdown wire format."""
    result: ParseResult[tuple[ProductionReferenceProposal, ...]] = ParseResult(value=())
    if not isinstance(text, str):
        result.warnings.append("reference_source_invalid")
        return result

    body = normalize_text(text)
    blocks, _ = _split_blocks(body, _SOURCE_BLOCKS)
    proposals: list[ProductionReferenceProposal] = []
    seen_urls: set[str] = set()
    for block in blocks:
        values = _fields(block.lines)
        raw_url = (values.get("url") or values.get("lien") or "").strip()
        try:
            canonical_url = canonicalize_http_url(raw_url)
        except (AttributeError, ValueError):
            result.warnings.append("reference_invalid_url")
            result.dropped_blocks.append(block.raw())
            continue

        raw_kind = (values.get("kind") or "").strip()
        if not raw_kind:
            kind = ProductionReferenceKind.PUBLICATION
            result.warnings.append("reference_kind_missing_defaulted_to_publication")
        else:
            try:
                kind = ProductionReferenceKind(_fold(raw_kind).replace(" ", "_"))
            except ValueError:
                result.warnings.append("reference_invalid_kind")
                result.dropped_blocks.append(block.raw())
                continue

        relevance_reason = (values.get("reason") or "").strip()
        if not relevance_reason:
            result.warnings.append("reference_missing_reason")
            result.dropped_blocks.append(block.raw())
            continue

        raw_date = (values.get("published-at") or values.get("date") or "").strip()
        published_at: date | None = None
        if raw_date and not _is_explicit_unknown(raw_date):
            published_at = _parse_date(raw_date)
            if published_at is None:
                result.warnings.append("reference_unreadable_date")
            elif published_at > research_date:
                result.warnings.append("reference_future_date")
                result.dropped_blocks.append(block.raw())
                continue

        raw_role = (values.get("role") or "").strip()
        try:
            role = SourceRole(_fold(raw_role).replace(" ", "_")) if raw_role else SourceRole.UNKNOWN
        except ValueError:
            role = SourceRole.UNKNOWN
            result.warnings.append("reference_unknown_role")

        if canonical_url in seen_urls:
            result.warnings.append("reference_duplicate_url_ignored")
            result.dropped_blocks.append(block.raw())
            continue
        seen_urls.add(canonical_url)
        proposals.append(
            ProductionReferenceProposal(
                canonical_url=canonical_url,
                title=_optional_field(values.get("title") or values.get("titre")),
                publisher=_optional_field(values.get("publisher") or values.get("editeur")),
                published_at=published_at,
                role=role,
                kind=kind,
                relevance_reason=relevance_reason,
            )
        )

    result.value = tuple(proposals)
    return result


def load_legacy_reference_report(
    raw_text: str,
    research_date: date,
    *,
    corpus: ProductionReferenceCorpusV1 | None = None,
    legacy_imported: bool = False,
) -> ReferenceReport:
    """Build the temporary ReferenceReport projection. TODO AW-012/AW-013.

    V4 imported references remain legacy ReferenceReport data and are never
    upgraded or represented as a ProductionReferenceCorpusV1.
    """
    if legacy_imported and corpus is not None:
        raise ValueError("A V4 imported legacy report cannot be labelled as a V1 corpus")
    if not legacy_imported and corpus is None:
        raise ValueError("A V1 corpus is required for a non-legacy reference artifact")

    parsed = parse_reference_report(raw_text, research_date)
    if not parsed.usable or parsed.value is None:
        raise ValueError("Legacy reference report could not be reconstructed from RAW")
    report = parsed.value
    if legacy_imported:
        return report

    eligible_urls = {
        source.canonical_url for source in corpus.sources if source.eligible_for_extraction
    }
    kept_sources = tuple(
        source for source in report.sources if source.canonical_url in eligible_urls
    )
    kept_ids = {source.local_id for source in kept_sources}
    kept_events = tuple(
        ParsedEvent(
            local_id=event.local_id,
            event_date=event.event_date,
            source_ids=tuple(source_id for source_id in event.source_ids if source_id in kept_ids),
            text=event.text,
        )
        for event in report.events
        if any(source_id in kept_ids for source_id in event.source_ids)
    )
    return ReferenceReport(
        sources=kept_sources,
        events=kept_events,
        uncertainties=report.uncertainties,
        editorial_title=report.editorial_title,
    )


def _required_string(payload: Mapping[str, Any], key: str) -> str:
    value = payload[key]
    if not isinstance(value, str):
        raise ValueError(f"{key} must be text")
    return value


def _optional_string(payload: Mapping[str, Any], key: str) -> str | None:
    value = payload[key]
    if value is not None and not isinstance(value, str):
        raise ValueError(f"{key} must be text or None")
    return value


def _required_bool(payload: Mapping[str, Any], key: str) -> bool:
    value = payload[key]
    if type(value) is not bool:
        raise ValueError(f"{key} must be boolean")
    return value


def _uuid(value: object, field: str) -> UUID:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a UUID string")
    try:
        return UUID(value)
    except ValueError as exc:
        raise ValueError(f"{field} must be a UUID string") from exc


def _optional_uuid(payload: Mapping[str, Any], key: str) -> UUID | None:
    value = payload[key]
    return None if value is None else _uuid(value, key)


def _date(value: object, field: str) -> date:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be an ISO date")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO date") from exc
    if parsed.isoformat() != value:
        raise ValueError(f"{field} must be a canonical ISO date")
    return parsed


def _optional_date(payload: Mapping[str, Any], key: str) -> date | None:
    value = payload[key]
    return None if value is None else _date(value, key)


def _enum[T: StrEnum](enum_type: type[T], value: object, field: str) -> T:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string enum value")
    try:
        return enum_type(value)  # type: ignore[call-arg]
    except ValueError as exc:
        raise ValueError(f"{field} is not a supported value") from exc


def _optional_field(value: str | None) -> str | None:
    if value is None:
        return None
    return value.strip() or None
