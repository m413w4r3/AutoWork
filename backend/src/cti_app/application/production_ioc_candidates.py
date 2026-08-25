"""Deterministic candidate IOC packs built from persisted indicators.

This module deliberately has no model or persistence side effects.  Callers
load the persisted records and derived-text blobs, then pass those snapshots to
``build_candidate_pack``.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Final
from uuid import UUID

from cti_app.application.production_normalization import normalize_indicator_value
from cti_app.application.production_parsers import ReferenceReport
from cti_app.domain.collection import (
    DerivedArtifact,
    Indicator,
    IndicatorKind,
    SourceCollection,
)
from cti_app.domain.entities import SourceDocument
from cti_app.domain.publication import ArtifactType

PACK_BUILDER_VERSION: Final = "ioc-candidate-pack-v1"
DEFAULT_MAX_CANDIDATES_PER_BATCH: Final = 64
DEFAULT_MAX_BATCH_CHARS: Final = 120_000
MAX_SNIPPETS_PER_SOURCE: Final = 3
SNIPPET_CONTEXT_CHARS: Final = 200


class CandidateWarningCode(StrEnum):
    SOURCE_NOT_MAPPED = "source_not_mapped"
    TEXT_NOT_AVAILABLE = "text_not_available"
    INVALID_SPAN = "invalid_span"


@dataclass(frozen=True, slots=True)
class IocCandidateEvidence:
    indicator_id: UUID
    derived_artifact_id: UUID
    source_document_id: UUID
    source_ids: tuple[str, ...]
    original_value: str
    span_start: int
    span_end: int
    snippet: str | None


@dataclass(frozen=True, slots=True)
class IocCandidate:
    candidate_id: str
    artifact_type: ArtifactType
    preferred_original_value: str
    normalized_value: str
    source_ids: tuple[str, ...]
    evidence: tuple[IocCandidateEvidence, ...]
    indicator_ids: tuple[UUID, ...]


@dataclass(frozen=True, slots=True)
class IocCandidateBatch:
    batch_id: str
    ordinal: int
    candidates: tuple[IocCandidate, ...]
    serialized_chars: int


@dataclass(frozen=True, slots=True)
class IocCandidatePack:
    pack_version: str
    parser_versions: tuple[str, ...]
    builder_version: str
    candidates: tuple[IocCandidate, ...]
    batches: tuple[IocCandidateBatch, ...]
    total_candidates: int
    total_evidence_occurrences: int
    sources_mapped: int
    sources_unmapped: int
    linked_evidence_occurrences: int
    warnings: tuple[str, ...]
    pack_hash: str


@dataclass(frozen=True, slots=True)
class _Occurrence:
    indicator: Indicator
    artifact_type: ArtifactType
    source_ids: tuple[str, ...]
    snippet: str | None


_KIND_TO_ARTIFACT: Final = {
    IndicatorKind.IP: ArtifactType.IP,
    IndicatorKind.DOMAIN: ArtifactType.DOMAIN,
    IndicatorKind.URL: ArtifactType.URL,
    IndicatorKind.HASH: ArtifactType.HASH,
    IndicatorKind.EMAIL: ArtifactType.EMAIL,
}


def build_candidate_pack(
    indicators: Sequence[Indicator],
    *,
    collections: Sequence[SourceCollection],
    source_documents: Sequence[SourceDocument] = (),
    artifacts: Sequence[DerivedArtifact] = (),
    reference_report: ReferenceReport,
    artifact_texts: Mapping[UUID, str] | None = None,
    max_candidates_per_batch: int = DEFAULT_MAX_CANDIDATES_PER_BATCH,
    max_batch_chars: int = DEFAULT_MAX_BATCH_CHARS,
) -> IocCandidatePack:
    """Build a canonical pack from already-loaded persisted records.

    ``artifact_texts`` is keyed by ``DerivedArtifact.id`` and contains the
    decoded text from its ``text_blob_id``.  Missing text is retained as
    internal provenance and reported as a warning; it never creates a source.
    """
    if max_candidates_per_batch <= 0 or max_batch_chars <= 0:
        raise ValueError("Batch limits must be positive")

    source_lookup = _source_ids_by_document(collections, source_documents, reference_report)
    artifact_texts = artifact_texts or {}
    artifact_versions = tuple(sorted({a.parser_version for a in artifacts}))
    warnings: list[str] = []
    groups: dict[tuple[ArtifactType, str], list[_Occurrence]] = defaultdict(list)
    ordered = sorted(indicators, key=lambda i: (str(i.id), str(i.derived_artifact_id)))
    snippet_counts: defaultdict[tuple[str, tuple[ArtifactType, str]], int] = defaultdict(int)

    for indicator in ordered:
        artifact_type = _KIND_TO_ARTIFACT.get(indicator.kind)
        if artifact_type is None:
            continue
        normalized = normalize_indicator_value(indicator.original_value, artifact_type)
        # Persisted normalized values are retained for audit, but normalization
        # from the original value is the grouping rule used by this builder.
        if (
            indicator.normalized_value
            and normalize_indicator_value(indicator.normalized_value, artifact_type) == normalized
        ):
            normalized = normalize_indicator_value(indicator.normalized_value, artifact_type)
        source_ids = source_lookup.get(indicator.source_document_id, ())
        if not source_ids:
            warnings.append(
                f"{CandidateWarningCode.SOURCE_NOT_MAPPED.value}:"
                f" indicator={indicator.id} document={indicator.source_document_id}"
            )
        key = (artifact_type, normalized)
        snippet: str | None = None
        if source_ids and indicator.derived_artifact_id in artifact_texts:
            text = artifact_texts[indicator.derived_artifact_id]
            try:
                value = indicator.span.passage(text)
                start = max(0, indicator.span.start - SNIPPET_CONTEXT_CHARS)
                end = min(len(text), indicator.span.end + SNIPPET_CONTEXT_CHARS)
                if any(
                    snippet_counts[(source_id, key)] < MAX_SNIPPETS_PER_SOURCE
                    for source_id in source_ids
                ):
                    snippet = text[start:end]
                    for source_id in source_ids:
                        snippet_counts[(source_id, key)] += 1
                if snippet is not None and value.casefold() not in snippet.casefold():
                    raise ValueError("indicator value absent from snippet")
            except (ValueError, IndexError):
                warnings.append(
                    f"{CandidateWarningCode.INVALID_SPAN.value}: indicator={indicator.id}"
                )
        elif source_ids:
            warnings.append(
                f"{CandidateWarningCode.TEXT_NOT_AVAILABLE.value}: "
                f"artifact={indicator.derived_artifact_id}"
            )
        groups[key].append(_Occurrence(indicator, artifact_type, source_ids, snippet))

    candidates = tuple(
        _candidate_for(key, occurrences)
        for key, occurrences in sorted(
            groups.items(), key=lambda item: (item[0][0].value, item[0][1])
        )
    )
    batches = _make_batches(candidates, max_candidates_per_batch, max_batch_chars)
    occurrences = tuple(o for values in groups.values() for o in values)
    mapped_source_ids = {
        source_id for occurrence in occurrences for source_id in occurrence.source_ids
    }
    mapped_documents = {
        occurrence.indicator.source_document_id
        for occurrence in occurrences
        if occurrence.source_ids
    }
    unmapped_documents = {
        occurrence.indicator.source_document_id
        for occurrence in occurrences
        if not occurrence.source_ids
    }
    total_occurrences = sum(len(values) for values in groups.values())
    canonical_without_hash = {
        "pack_version": "1",
        "parser_versions": artifact_versions,
        "builder_version": PACK_BUILDER_VERSION,
        "candidates": [_candidate_json(candidate) for candidate in candidates],
        "batches": [
            [candidate.candidate_id for candidate in batch.candidates] for batch in batches
        ],
    }
    pack_hash = _sha256(canonical_without_hash)
    return IocCandidatePack(
        pack_version="1",
        parser_versions=artifact_versions,
        builder_version=PACK_BUILDER_VERSION,
        candidates=candidates,
        batches=batches,
        total_candidates=len(candidates),
        total_evidence_occurrences=total_occurrences,
        sources_mapped=len(mapped_source_ids),
        sources_unmapped=len(unmapped_documents - mapped_documents),
        linked_evidence_occurrences=sum(1 for occurrence in occurrences if occurrence.source_ids),
        warnings=tuple(sorted(set(warnings))),
        pack_hash=pack_hash,
    )


def _source_ids_by_document(
    collections: Sequence[SourceCollection],
    documents: Sequence[SourceDocument],
    report: ReferenceReport,
) -> dict[UUID, tuple[str, ...]]:
    report_ids = {source.canonical_url: source.local_id for source in report.sources}
    docs = {document.id: document for document in documents}
    by_id = {collection.id: collection for collection in collections}
    result: dict[UUID, tuple[str, ...]] = {}
    for collection in sorted(collections, key=lambda item: str(item.id)):
        root = collection
        seen: set[UUID] = set()
        while root.parent_source_collection_id is not None and root.id not in seen:
            seen.add(root.id)
            root = by_id.get(root.parent_source_collection_id, root)
            if root.id in seen:
                break
        document = (
            docs.get(collection.source_document_id) if collection.source_document_id else None
        )
        canonical_url = (
            root.canonical_url
            if collection.parent_source_collection_id is not None
            else (document.final_url if document else None) or root.canonical_url
        )
        source_id = report_ids.get(canonical_url)
        if source_id and collection.source_document_id:
            result.setdefault(collection.source_document_id, tuple())
            result[collection.source_document_id] = tuple(
                sorted(set(result[collection.source_document_id] + (source_id,)))
            )
    return result


def _candidate_for(
    key: tuple[ArtifactType, str], occurrences: Sequence[_Occurrence]
) -> IocCandidate:
    artifact_type, normalized = key
    ordered = tuple(
        sorted(occurrences, key=lambda o: (str(o.indicator.id), o.indicator.span.start))
    )
    source_ids = tuple(
        sorted({source_id for occurrence in ordered for source_id in occurrence.source_ids})
    )
    candidate_id = (
        "ioc-" + hashlib.sha256(f"{artifact_type.value}\0{normalized}".encode()).hexdigest()
    )
    return IocCandidate(
        candidate_id=candidate_id,
        artifact_type=artifact_type,
        preferred_original_value=ordered[0].indicator.original_value,
        normalized_value=normalized,
        source_ids=source_ids,
        evidence=tuple(
            IocCandidateEvidence(
                indicator_id=o.indicator.id,
                derived_artifact_id=o.indicator.derived_artifact_id,
                source_document_id=o.indicator.source_document_id,
                source_ids=o.source_ids,
                original_value=o.indicator.original_value,
                span_start=o.indicator.span.start,
                span_end=o.indicator.span.end,
                snippet=o.snippet,
            )
            for o in ordered
        ),
        indicator_ids=tuple(o.indicator.id for o in ordered),
    )


def _candidate_json(candidate: IocCandidate) -> dict[str, object]:
    return {
        "candidate_id": candidate.candidate_id,
        "artifact_type": candidate.artifact_type.value,
        "preferred_original_value": candidate.preferred_original_value,
        "normalized_value": candidate.normalized_value,
        "source_ids": candidate.source_ids,
        "indicator_ids": [str(item) for item in candidate.indicator_ids],
        "evidence": [
            {
                "indicator_id": str(item.indicator_id),
                "derived_artifact_id": str(item.derived_artifact_id),
                "source_document_id": str(item.source_document_id),
                "source_ids": item.source_ids,
                "original_value": item.original_value,
                "span": [item.span_start, item.span_end],
                "snippet": item.snippet,
            }
            for item in candidate.evidence
        ],
    }


def _make_batches(
    candidates: tuple[IocCandidate, ...], max_candidates: int, max_chars: int
) -> tuple[IocCandidateBatch, ...]:
    batches: list[IocCandidateBatch] = []
    current: list[IocCandidate] = []
    for candidate in candidates:
        prospective = [*current, candidate]
        prospective_chars = len(_canonical_json([_candidate_json(item) for item in prospective]))
        if current and (len(current) >= max_candidates or prospective_chars > max_chars):
            batches.append(_batch(len(batches), current))
            current = []
        current.append(candidate)
    if current:
        batches.append(_batch(len(batches), current))
    return tuple(batches)


def _batch(ordinal: int, candidates: Sequence[IocCandidate]) -> IocCandidateBatch:
    payload = [_candidate_json(candidate) for candidate in candidates]
    return IocCandidateBatch(
        batch_id="batch-" + hashlib.sha256(_canonical_json(payload).encode()).hexdigest(),
        ordinal=ordinal,
        candidates=tuple(candidates),
        serialized_chars=len(_canonical_json(payload)),
    )


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _sha256(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()
