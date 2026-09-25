"""Deterministic serialization and compatibility projections for references."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date
from enum import StrEnum
from typing import TYPE_CHECKING, Any
from uuid import UUID

from cti_app.application.production_parsers import (
    ParseResult,
    ReferenceReport,
    _fields,
    _fold,
    _is_explicit_unknown,
    _parse_date,
    _split_blocks,
    normalize_text,
    parse_reference_report,
    reconcile_reference_report_with_archives,
    reference_report_from_json,
)
from cti_app.domain.collection import CollectionState
from cti_app.domain.discovery import SourceRole, canonicalize_http_url
from cti_app.domain.production_references import (
    ProductionReferenceCorpusV1,
    ProductionReferenceKind,
    ProductionReferenceResearchStatus,
    ProductionReferenceSourceV1,
    ProductionReferenceTier,
    is_eligible_for_extraction,
)

if TYPE_CHECKING:
    from cti_app.application.production_artifact_store import ProductionArtifactStore
    from cti_app.domain.production import ProductionArtifact, ProductionInputSource

# AW-010 contract versions. They participate in the functional REFERENCES
# identity: a parser or schema change invalidates the stored corpus.
PRODUCTION_REFERENCE_PARSER_VERSION = "production-reference-proposal-v1"
PRODUCTION_REFERENCE_CORPUS_SCHEMA_VERSION = 1

#: Corpus warnings that restate availability and are recomputed on each build.
_AVAILABILITY_WARNING_PREFIXES = (
    "core_source_unavailable:",
    "supporting_source_unavailable:",
)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

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


@dataclass(frozen=True, slots=True)
class ReferenceCollectionObservation:
    """The exact SourceCollection/SourceDocument behind one corpus source."""

    collection_id: UUID | None = None
    state: CollectionState = CollectionState.UNAVAILABLE
    source_document_id: UUID | None = None
    content_sha256: str | None = None


async def observe_reference_collections(
    uow: Any, subject_id: UUID, urls: Sequence[str]
) -> dict[str, ReferenceCollectionObservation]:
    """Read the collection state each canonical URL holds right now.

    The corpus captures the ``SourceDocument`` attached to the exact
    ``SourceCollection`` of this subject -- never the most recent document that
    happens to share the URL. A URL without a collection is ``UNAVAILABLE``.
    """
    observations = {url: ReferenceCollectionObservation() for url in urls}
    collections_repository = getattr(uow, "source_collections", None)
    if collections_repository is None:
        return observations
    documents_repository = getattr(uow, "source_documents", None)
    collections = await collections_repository.list_for_subject(subject_id)
    documents = (
        await documents_repository.list_for_subject(subject_id)
        if documents_repository is not None
        else ()
    )
    documents_by_id = {document.id: document for document in documents}
    collections_by_url: dict[str, Any] = {}
    for collection in collections:
        collections_by_url.setdefault(collection.canonical_url, collection)
    for url in urls:
        collection = collections_by_url.get(url)
        if collection is None:
            continue
        document_id = getattr(collection, "source_document_id", None)
        digest = getattr(documents_by_id.get(document_id), "decoded_sha256", None)
        content_sha256 = digest.casefold() if isinstance(digest, str) else None
        if content_sha256 is not None and not _SHA256_RE.fullmatch(content_sha256):
            content_sha256 = None
        observations[url] = ReferenceCollectionObservation(
            collection_id=collection.id,
            state=_collection_state(collection.state),
            source_document_id=document_id,
            content_sha256=content_sha256,
        )
    return observations


def build_production_reference_corpus(
    *,
    subject_id: UUID,
    research_date: date,
    production_input_hash: str,
    core_sources: Sequence[ProductionInputSource],
    proposals: Sequence[ProductionReferenceProposal],
    observations: Mapping[str, ReferenceCollectionObservation],
    warnings: Sequence[str],
    previous: ProductionReferenceCorpusV1 | None = None,
) -> ProductionReferenceCorpusV1:
    """Assemble the canonical corpus from its identities and observations.

    Identity precedence: a snapshot CORE always wins a duplicated URL (its URL,
    role and discovery candidates are authoritative); a rebuild keeps every
    source of the ``previous`` corpus so nothing disappears; model proposals are
    additive and the first one for a URL wins. Every source then receives the
    collection observation read now, and availability warnings are recomputed.
    """
    identities: dict[str, ProductionReferenceSourceV1] = {}
    if previous is not None:
        identities.update((source.canonical_url, source) for source in previous.sources)
    for core in core_sources:
        identities[core.canonical_url] = _unobserved_source(
            canonical_url=core.canonical_url,
            tier=ProductionReferenceTier.CORE,
            kind=ProductionReferenceKind.PUBLICATION,
            role=core.role,
            title=core.title,
            publisher=core.publisher,
            published_at=core.published_at,
            discovery_candidate_ids=(core.discovery_candidate_id,),
            relevance_reason=None,
            proposed_by_model=False,
        )
    for proposal in proposals:
        identities.setdefault(
            proposal.canonical_url,
            _unobserved_source(
                canonical_url=proposal.canonical_url,
                tier=proposal.tier,
                kind=proposal.kind,
                role=proposal.role,
                title=proposal.title,
                publisher=proposal.publisher,
                published_at=proposal.published_at,
                discovery_candidate_ids=(),
                relevance_reason=proposal.relevance_reason,
                proposed_by_model=True,
            ),
        )

    sources: list[ProductionReferenceSourceV1] = []
    for url, identity in identities.items():
        observation = observations.get(url) or ReferenceCollectionObservation()
        sources.append(
            replace(
                identity,
                source_collection_id=observation.collection_id,
                source_document_id=observation.source_document_id,
                collection_state=observation.state,
                content_sha256=observation.content_sha256,
                eligible_for_extraction=is_eligible_for_extraction(
                    collection_state=observation.state,
                    source_document_id=observation.source_document_id,
                    content_sha256=observation.content_sha256,
                ),
            )
        )
    corpus = ProductionReferenceCorpusV1(
        schema_version=PRODUCTION_REFERENCE_CORPUS_SCHEMA_VERSION,
        subject_id=subject_id,
        research_date=research_date,
        production_input_hash=production_input_hash,
        research_status=ProductionReferenceResearchStatus.COMPLETED,
        sources=tuple(sources),
        warnings=(),
    )
    availability: list[str] = []
    for source in corpus.sources:
        if source.eligible_for_extraction:
            continue
        prefix = (
            "core_source_unavailable"
            if source.tier is ProductionReferenceTier.CORE
            else "supporting_source_unavailable"
        )
        availability.append(f"{prefix}:{source.canonical_url}")
    kept = tuple(
        warning for warning in warnings if not warning.startswith(_AVAILABILITY_WARNING_PREFIXES)
    )
    return replace(corpus, warnings=(*kept, *availability))


def production_reference_corpus_metadata(corpus: ProductionReferenceCorpusV1) -> dict[str, int]:
    """The bounded counters of a REFERENCES artifact; sources are not copied."""
    counts = {tier: 0 for tier in ProductionReferenceTier}
    eligible = 0
    for source in corpus.sources:
        counts[source.tier] += 1
        eligible += source.eligible_for_extraction
    return {
        "schema_version": corpus.schema_version,
        "core_source_count": counts[ProductionReferenceTier.CORE],
        "supporting_source_count": counts[ProductionReferenceTier.SUPPORTING],
        "technical_source_count": counts[ProductionReferenceTier.TECHNICAL],
        "eligible_source_count": eligible,
        "unavailable_source_count": len(corpus.sources) - eligible,
    }


def has_usable_core_source(corpus: ProductionReferenceCorpusV1) -> bool:
    """REFERENCES may advance only with at least one extractable CORE source."""
    return any(
        source.tier is ProductionReferenceTier.CORE and source.eligible_for_extraction
        for source in corpus.sources
    )


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
    """Project REFERENCES back to the legacy ``ReferenceReport``.

    TODO AW-012/AW-013: legacy production compatibility only. This is the
    single boundary through which Q2, Synthesis, Assembly, QA, the Repair Desk
    and ProductionState V4 still read ``ReferenceReport``; delete it once they
    consume ``ProductionReferenceCorpusV1`` directly.

    For an AW-010 artifact the report is re-parsed from the RAW wire format and
    reduced to the corpus sources eligible for extraction; events keep only the
    source IDs that survive and disappear when none does. A V4 imported report
    (``legacy_imported``) is returned as-is and is never upgraded to a corpus.
    """
    if legacy_imported == (corpus is not None):
        raise ValueError("Pass exactly one of a V1 corpus or legacy_imported=True")

    parsed = parse_reference_report(raw_text, research_date)
    if not parsed.usable or parsed.value is None:
        raise ValueError("Legacy reference report could not be reconstructed from RAW")
    report = parsed.value
    if corpus is None:
        return report

    eligible_urls = {
        source.canonical_url for source in corpus.sources if source.eligible_for_extraction
    }
    return reconcile_reference_report_with_archives(report, eligible_urls).report


def legacy_reference_source_labels(raw_text: str, research_date: date) -> dict[str, str]:
    """Map each RAW SOURCE block's canonical URL to its wire label ("S1").

    TODO AW-012/AW-013: display metadata for the Repair Desk only. The label is
    the model's block name, never the identity of a corpus source.
    """
    parsed = parse_reference_report(raw_text, research_date)
    if parsed.value is None:
        return {}
    return {source.canonical_url: source.local_id for source in parsed.value.sources}


async def load_reference_projection(
    store: ProductionArtifactStore,
    artifact: ProductionArtifact,
) -> ReferenceReport | None:
    """Read a REFERENCES artifact as the legacy ``ReferenceReport``.

    TODO AW-012/AW-013: the async side of the single compatibility boundary
    (see ``load_legacy_reference_report``). A V4 imported artifact keeps its
    legacy report payload; an AW-010 artifact is projected from RAW + corpus.
    """
    if artifact.canonical_blob_id is None:
        return None
    payload = await store.read_json(artifact.canonical_blob_id)
    try:
        corpus = production_reference_corpus_from_json(payload)
    except ValueError:
        return reference_report_from_json(payload)
    if artifact.raw_blob_id is None:
        return None
    raw = await store.read_text(artifact.raw_blob_id)
    return load_legacy_reference_report(raw, corpus.research_date, corpus=corpus)


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
        return enum_type(value)
    except ValueError as exc:
        raise ValueError(f"{field} is not a supported value") from exc


def _unobserved_source(**identity: Any) -> ProductionReferenceSourceV1:
    return ProductionReferenceSourceV1(
        **identity,
        source_collection_id=None,
        source_document_id=None,
        collection_state=CollectionState.UNAVAILABLE,
        content_sha256=None,
        eligible_for_extraction=False,
    )


def _collection_state(value: Any) -> CollectionState:
    """One typed collection state, never a free-form string."""
    if isinstance(value, CollectionState):
        return value
    try:
        return CollectionState(str(getattr(value, "value", value)))
    except ValueError:
        return CollectionState.UNAVAILABLE


def _optional_field(value: str | None) -> str | None:
    if value is None:
        return None
    return value.strip() or None
