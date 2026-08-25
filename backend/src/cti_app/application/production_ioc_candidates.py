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
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Final
from uuid import NAMESPACE_URL, UUID, uuid5

from cti_app.application.production_normalization import normalize_indicator_value
from cti_app.application.production_parsers import ReferenceReport
from cti_app.domain.collection import (
    DerivedArtifact,
    Indicator,
    IndicatorKind,
    SourceCollection,
    SourceOriginKind,
)
from cti_app.domain.discovery import (
    DiscoveryIocType,
    ProvisionalDiscoveryIoc,
    canonicalize_http_url,
)
from cti_app.domain.entities import SourceDocument
from cti_app.domain.publication import ArtifactType

PACK_BUILDER_VERSION: Final = "ioc-candidate-pack-v2"
MAX_SNIPPETS_PER_SOURCE: Final = 3
SNIPPET_CONTEXT_CHARS: Final = 200


class CandidateWarningCode(StrEnum):
    SOURCE_NOT_MAPPED = "source_not_mapped"
    TEXT_NOT_AVAILABLE = "text_not_available"
    INVALID_SPAN = "invalid_span"
    INVALID_INDICATOR = "invalid_indicator"
    PERSISTED_NORMALIZATION_MISMATCH = "persisted_normalization_mismatch"
    DISCOVERY_PUBLICATION_UNRESOLVED = "discovery_publication_unresolved"
    DISCOVERY_VALUE_NOT_FOUND = "discovery_value_not_found"
    DISCOVERY_TYPE_IGNORED = "discovery_type_ignored"
    Q2_LITERAL_UNRESOLVED = "q2_literal_unresolved"


@dataclass(frozen=True, slots=True)
class DiscoveryPublicationEvidence:
    """A real archived publication related to a provisional Discovery IOC."""

    source_document_id: UUID
    derived_artifact_id: UUID
    source_ids: tuple[str, ...]
    text: str


@dataclass(frozen=True, slots=True)
class DiscoveryIocProvenance:
    provisional_ioc_id: UUID
    publication_ids: tuple[UUID, ...]
    publication_refs: tuple[str, ...]
    raw_value: str


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
    is_referenced_evidence: bool = False


@dataclass(frozen=True, slots=True)
class IocCandidate:
    candidate_id: str
    artifact_type: ArtifactType
    preferred_original_value: str
    normalized_value: str
    source_ids: tuple[str, ...]
    evidence: tuple[IocCandidateEvidence, ...]
    indicator_ids: tuple[UUID, ...]
    discovery_provenance: tuple[DiscoveryIocProvenance, ...] = ()
    # Model-provided context is retained for audit only.  It is never evidence.
    q2_contexts: tuple[str, ...] = ()

    @property
    def source_backed(self) -> bool:
        # ``source_ids`` is the persisted pack fact; evidence may be omitted by
        # lightweight callers, while a discovery-only candidate has neither.
        return bool(self.source_ids or any(item.source_ids for item in self.evidence))


@dataclass(frozen=True, slots=True)
class IocCandidatePack:
    pack_version: str
    parser_versions: tuple[str, ...]
    builder_version: str
    candidates: tuple[IocCandidate, ...]
    total_candidates: int
    total_evidence_occurrences: int
    sources_mapped: int
    sources_unmapped: int
    linked_evidence_occurrences: int
    warnings: tuple[str, ...]
    pack_hash: str
    source_derived_candidates: int = 0
    discovery_augmented_candidates: int = 0
    discovery_only_candidates: int = 0
    discovery_matched_to_source: int = 0
    discovery_unmatched: int = 0


@dataclass(frozen=True, slots=True)
class Q2LiteralCandidate:
    """A literal surfaced by Q2, pending deterministic corpus recovery."""

    artifact_type: ArtifactType
    raw_value: str
    normalized_value: str
    context: str


@dataclass(frozen=True, slots=True)
class _Occurrence:
    indicator: Indicator
    artifact_type: ArtifactType
    source_ids: tuple[str, ...]
    snippet: str | None
    is_referenced_evidence: bool


_KIND_TO_ARTIFACT: Final = {
    IndicatorKind.IP: ArtifactType.IP,
    IndicatorKind.DOMAIN: ArtifactType.DOMAIN,
    IndicatorKind.URL: ArtifactType.URL,
    IndicatorKind.HASH: ArtifactType.HASH,
    IndicatorKind.EMAIL: ArtifactType.EMAIL,
}

_DISCOVERY_TYPE_TO_ARTIFACT: Final = {
    DiscoveryIocType.IPV4: ArtifactType.IP,
    DiscoveryIocType.IPV6: ArtifactType.IP,
    DiscoveryIocType.DOMAIN: ArtifactType.DOMAIN,
    DiscoveryIocType.URL: ArtifactType.URL,
    DiscoveryIocType.MD5: ArtifactType.HASH,
    DiscoveryIocType.SHA1: ArtifactType.HASH,
    DiscoveryIocType.SHA256: ArtifactType.HASH,
    DiscoveryIocType.EMAIL: ArtifactType.EMAIL,
}


def build_candidate_pack(
    indicators: Sequence[Indicator],
    *,
    collections: Sequence[SourceCollection],
    source_documents: Sequence[SourceDocument] = (),
    artifacts: Sequence[DerivedArtifact] = (),
    reference_report: ReferenceReport,
    artifact_texts: Mapping[UUID, str] | None = None,
    provisional_iocs: Sequence[ProvisionalDiscoveryIoc] = (),
    discovery_publications: Mapping[UUID, DiscoveryPublicationEvidence] | None = None,
    q2_literals: Sequence[Q2LiteralCandidate] = (),
) -> IocCandidatePack:
    """Build a canonical pack from already-loaded persisted records.

    ``artifact_texts`` is keyed by ``DerivedArtifact.id`` and contains the
    decoded text from its ``text_blob_id``.  Missing text is retained as
    internal provenance and reported as a warning; it never creates a source.
    """
    source_lookup = source_ids_by_document(collections, source_documents, reference_report)
    referenced_document_ids = {
        collection.source_document_id
        for collection in collections
        if (
            collection.origin_kind is SourceOriginKind.REFERENCED_EVIDENCE
            and collection.source_document_id is not None
        )
    }
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
        try:
            normalized = normalize_indicator_value(indicator.original_value, artifact_type)
        except ValueError:
            warnings.append(
                f"{CandidateWarningCode.INVALID_INDICATOR.value}: indicator={indicator.id}"
            )
            continue
        # Persisted normalized values are retained for audit, but normalization
        # from the original value is the grouping rule used by this builder.
        if indicator.normalized_value:
            try:
                persisted_normalized = normalize_indicator_value(
                    indicator.normalized_value, artifact_type
                )
            except ValueError:
                persisted_normalized = None
            if persisted_normalized != normalized:
                warnings.append(
                    f"{CandidateWarningCode.PERSISTED_NORMALIZATION_MISMATCH.value}: "
                    f"indicator={indicator.id}"
                )
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
        groups[key].append(
            _Occurrence(
                indicator,
                artifact_type,
                source_ids,
                snippet,
                indicator.source_document_id in referenced_document_ids,
            )
        )

    source_derived_candidates = tuple(
        _candidate_for(key, occurrences)
        for key, occurrences in sorted(
            groups.items(), key=lambda item: (item[0][0].value, item[0][1])
        )
    )
    candidates_list = list(source_derived_candidates)
    discovery_publications = discovery_publications or {}
    discovery_stats = {
        "augmented": 0,
        "only": 0,
        "matched": 0,
        "unmatched": 0,
    }
    discovery_provenance: dict[tuple[ArtifactType, str], list[DiscoveryIocProvenance]] = (
        defaultdict(list)
    )
    discovery_evidence: dict[tuple[ArtifactType, str], list[IocCandidateEvidence]] = defaultdict(
        list
    )
    for provisional in sorted(provisional_iocs, key=lambda item: str(item.id)):
        artifact_type = _DISCOVERY_TYPE_TO_ARTIFACT.get(provisional.proposed_type)
        if artifact_type is None:
            warnings.append(f"{CandidateWarningCode.DISCOVERY_TYPE_IGNORED.value}:{provisional.id}")
            continue
        try:
            normalized = normalize_indicator_value(
                provisional.normalized_value or provisional.raw_value, artifact_type
            )
        except ValueError:
            warnings.append(f"{CandidateWarningCode.DISCOVERY_TYPE_IGNORED.value}:{provisional.id}")
            continue
        key = (artifact_type, normalized)
        provenance = DiscoveryIocProvenance(
            provisional_ioc_id=provisional.id,
            publication_ids=tuple(
                sorted((item.publication_id for item in provisional.publication_relations), key=str)
            ),
            publication_refs=tuple(
                sorted({item.publication_ref for item in provisional.publication_relations})
            ),
            raw_value=provisional.raw_value,
        )
        discovery_provenance[key].append(provenance)
        found = 0
        search_values = _discovery_search_values(provisional, normalized)
        for relation in provisional.publication_relations:
            publication = discovery_publications.get(relation.publication_id)
            if publication is None:
                warnings.append(
                    f"{CandidateWarningCode.DISCOVERY_PUBLICATION_UNRESOLVED.value}:"
                    f"{provisional.id}:{relation.publication_ref}"
                )
                continue
            match = _find_literal(publication.text, search_values)
            if match is None:
                continue
            start, end = match
            source_ids = publication.source_ids
            discovery_evidence[key].append(
                IocCandidateEvidence(
                    indicator_id=uuid5(
                        NAMESPACE_URL,
                        f"discovery-indicator:{provisional.id}:{publication.derived_artifact_id}:{start}",
                    ),
                    derived_artifact_id=publication.derived_artifact_id,
                    source_document_id=publication.source_document_id,
                    source_ids=source_ids,
                    original_value=publication.text[start:end],
                    span_start=start,
                    span_end=end,
                    snippet=publication.text[
                        max(0, start - SNIPPET_CONTEXT_CHARS) : min(
                            len(publication.text), end + SNIPPET_CONTEXT_CHARS
                        )
                    ],
                )
            )
            found += 1
        if found:
            discovery_stats["matched"] += 1
        else:
            discovery_stats["unmatched"] += 1
            warnings.append(
                f"{CandidateWarningCode.DISCOVERY_VALUE_NOT_FOUND.value}:{provisional.id}"
            )
    by_key = {
        (candidate.artifact_type, candidate.normalized_value): candidate
        for candidate in candidates_list
    }
    for key in sorted(discovery_provenance, key=lambda item: (item[0].value, item[1])):
        current = by_key.get(key)
        provenance_items = tuple(discovery_provenance[key])
        discovered_evidence = tuple(discovery_evidence[key])
        if current is not None:
            discovery_stats["augmented"] += 1
            updated = replace(
                current,
                discovery_provenance=tuple(
                    sorted(
                        (*current.discovery_provenance, *provenance_items),
                        key=lambda item: str(item.provisional_ioc_id),
                    )
                ),
            )
            candidates_list[candidates_list.index(current)] = updated
            by_key[key] = updated
            continue
        if discovered_evidence:
            candidate = _discovery_candidate(
                key,
                provisional_ioc=provenance_items[0],
                evidence=discovered_evidence,
                provenance=provenance_items,
            )
        else:
            candidate = _discovery_candidate(
                key,
                provisional_ioc=provenance_items[0],
                evidence=(),
                provenance=provenance_items,
            )
            discovery_stats["only"] += 1
        candidates_list.append(candidate)
        by_key[key] = candidate

    # Q2 may surface a literal that deterministic extraction missed.  Recover
    # it only from the already archived corpus; its model context is not proof.
    artifact_documents = {
        collection.derived_artifact_id: collection.source_document_id
        for collection in collections
        if collection.derived_artifact_id is not None and collection.source_document_id is not None
    }
    for literal in sorted(
        q2_literals, key=lambda item: (item.artifact_type.value, item.normalized_value)
    ):
        key = (literal.artifact_type, literal.normalized_value)
        current = by_key.get(key)
        context = " ".join(literal.context.split())[:600]
        if current is not None:
            updated = replace(
                current,
                q2_contexts=tuple(sorted(set((*current.q2_contexts, context)) - {""})),
            )
            candidates_list[candidates_list.index(current)] = updated
            by_key[key] = updated
            continue

        evidence: list[IocCandidateEvidence] = []
        for artifact_id, text in sorted(artifact_texts.items(), key=lambda item: str(item[0])):
            source_document_id = artifact_documents.get(artifact_id)
            if source_document_id is None:
                continue
            match = _find_literal(text, _q2_search_values(literal))
            if match is None:
                continue
            start, end = match
            source_ids = source_lookup.get(source_document_id, ())
            evidence.append(
                IocCandidateEvidence(
                    indicator_id=uuid5(
                        NAMESPACE_URL,
                        f"q2-recovery:{literal.artifact_type.value}:{literal.normalized_value}:{artifact_id}:{start}",
                    ),
                    derived_artifact_id=artifact_id,
                    source_document_id=source_document_id,
                    source_ids=source_ids,
                    original_value=text[start:end],
                    span_start=start,
                    span_end=end,
                    snippet=text[
                        max(0, start - SNIPPET_CONTEXT_CHARS) : min(
                            len(text), end + SNIPPET_CONTEXT_CHARS
                        )
                    ],
                )
            )
        if not evidence:
            warnings.append(f"{CandidateWarningCode.Q2_LITERAL_UNRESOLVED.value}:{literal.raw_value}")
        candidate = _q2_candidate(key, literal, tuple(evidence), context)
        candidates_list.append(candidate)
        by_key[key] = candidate
    candidates = tuple(
        sorted(candidates_list, key=lambda item: (item.artifact_type.value, item.normalized_value))
    )
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
    total_occurrences = sum(len(candidate.evidence) for candidate in candidates)
    canonical_without_hash = {
        "pack_version": "1",
        "parser_versions": artifact_versions,
        "builder_version": PACK_BUILDER_VERSION,
        "candidates": [_candidate_hash_json(candidate) for candidate in candidates],
    }
    pack_hash = _sha256(canonical_without_hash)
    return IocCandidatePack(
        pack_version="1",
        parser_versions=artifact_versions,
        builder_version=PACK_BUILDER_VERSION,
        candidates=candidates,
        total_candidates=len(candidates),
        total_evidence_occurrences=total_occurrences,
        sources_mapped=len(mapped_source_ids),
        sources_unmapped=len(unmapped_documents - mapped_documents),
        linked_evidence_occurrences=sum(
            1 for occurrence in occurrences if occurrence.is_referenced_evidence
        ),
        warnings=tuple(sorted(set(warnings))),
        pack_hash=pack_hash,
        source_derived_candidates=len(source_derived_candidates),
        discovery_augmented_candidates=sum(
            1
            for candidate in candidates
            if candidate.discovery_provenance and candidate.source_backed
        ),
        discovery_only_candidates=discovery_stats["only"],
        discovery_matched_to_source=discovery_stats["matched"],
        discovery_unmatched=discovery_stats["unmatched"],
    )


def source_ids_by_document(
    collections: Sequence[SourceCollection],
    documents: Sequence[SourceDocument],
    report: ReferenceReport,
) -> dict[UUID, tuple[str, ...]]:
    report_ids: dict[str, str] = {}
    for source in report.sources:
        try:
            report_ids[canonicalize_http_url(source.canonical_url)] = source.local_id
        except ValueError:
            continue
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
        candidate_urls: tuple[str | None, ...]
        if collection.parent_source_collection_id is not None:
            candidate_urls = (root.canonical_url,)
        else:
            candidate_urls = ((document.final_url if document else None), root.canonical_url)
        source_id = None
        for value in candidate_urls:
            if not value:
                continue
            try:
                canonical_url = canonicalize_http_url(value)
            except ValueError:
                continue
            source_id = report_ids.get(canonical_url)
            if source_id:
                break
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
                is_referenced_evidence=o.is_referenced_evidence,
            )
            for o in ordered
        ),
        indicator_ids=tuple(o.indicator.id for o in ordered),
    )


def _discovery_search_values(
    provisional: ProvisionalDiscoveryIoc,
    normalized: str,
) -> tuple[str, ...]:
    values = [provisional.raw_value, provisional.normalized_value or "", normalized]
    # This is only a search representation; canonicalization remains owned by
    # production_normalization.  It permits the existing defanged spelling.
    values.append(
        provisional.raw_value.replace("[.]", ".")
        .replace("(.)", ".")
        .replace("[@]", "@")
        .replace("(at)", "@")
    )
    return tuple(dict.fromkeys(value for value in values if value.strip()))


def _q2_search_values(literal: Q2LiteralCandidate) -> tuple[str, ...]:
    values = [literal.raw_value, literal.normalized_value]
    values.append(
        literal.raw_value.replace("[.]", ".")
        .replace("(.)", ".")
        .replace("[@]", "@")
        .replace("(at)", "@")
    )
    return tuple(dict.fromkeys(value for value in values if value.strip()))


def _find_literal(text: str, values: Sequence[str]) -> tuple[int, int] | None:
    folded = text.casefold()
    matches = [
        (folded.find(value.casefold()), value)
        for value in values
        if value and folded.find(value.casefold()) >= 0
    ]
    if not matches:
        return None
    start, value = min(matches, key=lambda item: (item[0], -len(item[1])))
    return start, start + len(value)


def _discovery_candidate(
    key: tuple[ArtifactType, str],
    *,
    provisional_ioc: DiscoveryIocProvenance,
    evidence: tuple[IocCandidateEvidence, ...],
    provenance: tuple[DiscoveryIocProvenance, ...],
) -> IocCandidate:
    artifact_type, normalized = key
    return IocCandidate(
        candidate_id="ioc-"
        + hashlib.sha256(f"{artifact_type.value}\0{normalized}".encode()).hexdigest(),
        artifact_type=artifact_type,
        preferred_original_value=provisional_ioc.raw_value,
        normalized_value=normalized,
        source_ids=tuple(sorted({source_id for item in evidence for source_id in item.source_ids})),
        evidence=evidence,
        indicator_ids=tuple(item.indicator_id for item in evidence),
        discovery_provenance=provenance,
    )


def _q2_candidate(
    key: tuple[ArtifactType, str],
    literal: Q2LiteralCandidate,
    evidence: tuple[IocCandidateEvidence, ...],
    context: str,
) -> IocCandidate:
    artifact_type, normalized = key
    return IocCandidate(
        candidate_id="ioc-"
        + hashlib.sha256(f"{artifact_type.value}\0{normalized}".encode()).hexdigest(),
        artifact_type=artifact_type,
        preferred_original_value=literal.raw_value,
        normalized_value=normalized,
        source_ids=tuple(sorted({source_id for item in evidence for source_id in item.source_ids})),
        evidence=evidence,
        indicator_ids=tuple(item.indicator_id for item in evidence),
        q2_contexts=(context,) if context else (),
    )


def _candidate_json(candidate: IocCandidate) -> dict[str, object]:
    payload: dict[str, object] = {
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
    if candidate.discovery_provenance:
        payload["discovery_provenance"] = [
            {
                "provisional_ioc_id": str(item.provisional_ioc_id),
                "publication_ids": [str(value) for value in item.publication_ids],
                "publication_refs": item.publication_refs,
                "raw_value": item.raw_value,
            }
            for item in candidate.discovery_provenance
        ]
    if candidate.q2_contexts:
        payload["q2_contexts_not_evidence"] = candidate.q2_contexts
    return payload


def _candidate_hash_json(candidate: IocCandidate) -> dict[str, object]:
    """Stable pack facts; raw spellings remain in the auditable payload only."""
    return {
        "candidate_id": candidate.candidate_id,
        "artifact_type": candidate.artifact_type.value,
        "normalized_value": candidate.normalized_value,
        "source_ids": candidate.source_ids,
        "indicator_ids": [str(item) for item in candidate.indicator_ids],
        "evidence": [
            {
                "indicator_id": str(item.indicator_id),
                "derived_artifact_id": str(item.derived_artifact_id),
                "source_document_id": str(item.source_document_id),
                "source_ids": item.source_ids,
                "span": [item.span_start, item.span_end],
            }
            for item in candidate.evidence
        ],
        "discovery_provenance": [
            {
                "provisional_ioc_id": str(item.provisional_ioc_id),
                "publication_ids": [str(value) for value in item.publication_ids],
                "publication_refs": item.publication_refs,
            }
            for item in candidate.discovery_provenance
        ],
        "q2_contexts": candidate.q2_contexts,
    }


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _sha256(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()
