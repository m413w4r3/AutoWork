"""Consolidation of multiple discovery batches into a coherent view.

Algorithm (conservative):
1. Group candidates by title fingerprint (exact match)
2. Merge if strong signal (shared entity, title similarity, etc.)
3. Deduplicate URLs by canonical_url within each cluster
4. Preserve all metadata with enrichment strategy
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Sequence
from uuid import UUID

from cti_app.domain.discovery import (
    CandidateTopic,
    DiscoveryBatch,
    SourceCandidate,
    SourceVerificationStatus,
)
from cti_app.application.discovery_identity import (
    canonical_source_key,
    explicit_entity_tokens,
    has_strong_signal,
    title_fingerprint,
)


@dataclass(frozen=True, slots=True)
class CandidateOccurrence:
    """Reference to a single candidate in a batch."""

    batch_id: UUID
    candidate_id: UUID


@dataclass(slots=True)
class ConsolidatedCandidate:
    """Consolidated view of one or more candidates from multiple batches.

    Represents a single subject with:
    - A representative candidate (most recent/richest)
    - References to all member occurrences
    - Deduplicated sources
    - Warnings about conflicts
    """

    representative: CandidateTopic
    member_references: tuple[CandidateOccurrence, ...]
    sources: list[SourceCandidate]
    duplicate_publication_count: int
    merge_warnings: tuple[str, ...]

    @property
    def contribution_count(self) -> int:
        """Number of distinct batches contributing to this candidate."""
        batch_ids = {ref.batch_id for ref in self.member_references}
        return len(batch_ids)


def consolidate_discovery_batches(
    batches: Sequence[DiscoveryBatch],
) -> list[ConsolidatedCandidate]:
    """Consolidate multiple discovery batches into a single coherent view.

    Args:
        batches: Active discovery batches for an edition (chronological order expected)

    Returns:
        List of consolidated candidates with merged metadata and deduped URLs.
    """
    if not batches:
        return []

    # Map (title_fingerprint, candidate_id) → CandidateOccurrence for clustering
    candidates_by_batch: list[dict[UUID, CandidateTopic]] = [
        {c.id: c for c in batch.candidates} for batch in batches
    ]

    # Cluster candidates: {representative_id} → [occurrence1, occurrence2, ...]
    # We'll use a merge-find approach to handle transitive matches
    clusters: dict[tuple[UUID, UUID], list[CandidateOccurrence]] = {}

    for batch_idx, batch in enumerate(batches):
        for candidate in batch.candidates:
            occurrence = CandidateOccurrence(batch_id=batch.id, candidate_id=candidate.id)

            # Check if this candidate belongs to an existing cluster
            matched_cluster_key = None

            # Step 1: Exact match by title fingerprint
            current_fp = title_fingerprint(candidate.title)
            for (repr_batch_idx, repr_cand_id), members in clusters.items():
                if repr_batch_idx < len(candidates_by_batch):
                    repr_cand = candidates_by_batch[repr_batch_idx].get(repr_cand_id)
                    if repr_cand and title_fingerprint(repr_cand.title) == current_fp:
                        matched_cluster_key = (repr_batch_idx, repr_cand_id)
                        break

            # Step 2: Strong signal match (only if no exact match)
            if matched_cluster_key is None:
                current_entities = explicit_entity_tokens(candidate)
                current_campaigns = {s.lower() for s in candidate.campaigns}
                current_malware = {s.lower() for s in candidate.malware}

                for (repr_batch_idx, repr_cand_id), members in clusters.items():
                    if repr_batch_idx < len(candidates_by_batch):
                        repr_cand = candidates_by_batch[repr_batch_idx].get(repr_cand_id)
                        if repr_cand:
                            repr_entities = explicit_entity_tokens(repr_cand)
                            repr_campaigns = {s.lower() for s in repr_cand.campaigns}
                            repr_malware = {s.lower() for s in repr_cand.malware}

                            if has_strong_signal(
                                candidate.title,
                                repr_cand.title,
                                current_entities,
                                repr_entities,
                                current_campaigns,
                                repr_campaigns,
                                current_malware,
                                repr_malware,
                            ):
                                matched_cluster_key = (repr_batch_idx, repr_cand_id)
                                break

            # Add to cluster
            if matched_cluster_key:
                clusters[matched_cluster_key].append(occurrence)
            else:
                # Start new cluster with this candidate as representative
                clusters[(batch_idx, candidate.id)] = [occurrence]

    # Step 3: Merge clusters into ConsolidatedCandidate
    consolidated: list[ConsolidatedCandidate] = []

    for (repr_batch_idx, repr_cand_id), occurrences in clusters.items():
        # Retrieve representative candidate
        repr_cand = candidates_by_batch[repr_batch_idx].get(repr_cand_id)
        if not repr_cand:
            continue

        # Collect all candidates in this cluster
        all_candidates_in_cluster = [
            candidates_by_batch[occ.batch_id].get(occ.candidate_id)
            for occ in occurrences
        ]
        all_candidates_in_cluster = [c for c in all_candidates_in_cluster if c is not None]

        # Merge sources with deduplication and metadata enrichment
        merged_sources, duplicate_count, merge_warnings = _merge_sources_in_cluster(
            all_candidates_in_cluster
        )

        # Merge candidate metadata (uncertainties, entities, IOCs)
        merged_candidate = _merge_candidate_metadata(repr_cand, all_candidates_in_cluster)

        consolidated.append(
            ConsolidatedCandidate(
                representative=merged_candidate,
                member_references=tuple(sorted(occurrences, key=lambda o: (o.batch_id, o.candidate_id))),
                sources=merged_sources,
                duplicate_publication_count=duplicate_count,
                merge_warnings=tuple(merge_warnings),
            )
        )

    return consolidated


def _merge_sources_in_cluster(
    candidates: list[CandidateTopic],
) -> tuple[list[SourceCandidate], int, list[str]]:
    """Merge sources from multiple candidates in a cluster.

    Returns:
        (merged_sources, duplicate_count, merge_warnings)
    """
    seen_urls: dict[str, SourceCandidate] = {}
    duplicate_count = 0
    merge_warnings: list[str] = []

    for candidate in candidates:
        for source in candidate.sources:
            url_key = canonical_source_key(source.canonical_url)

            if url_key in seen_urls:
                duplicate_count += 1
                # Optionally enrich existing entry
                existing = seen_urls[url_key]
                _merge_source_metadata(existing, source, merge_warnings)
            else:
                seen_urls[url_key] = source

    return list(seen_urls.values()), duplicate_count, merge_warnings


def _merge_source_metadata(
    existing: SourceCandidate,
    new: SourceCandidate,
    warnings: list[str],
) -> None:
    """Enrich source metadata in-place, preferring non-empty/known values.

    Known values should override unknown/empty values without warning.
    Conflicts between two non-empty values generate a warning.
    """
    # Publisher enrichment
    if not existing.publisher or existing.publisher.lower() == "unknown":
        if new.publisher and new.publisher.lower() != "unknown":
            existing.publisher = new.publisher
    elif new.publisher and new.publisher.lower() != "unknown" and new.publisher != existing.publisher:
        warnings.append(f"conflicting_publisher: {existing.publisher} vs {new.publisher}")

    # Published date enrichment
    if existing.published_at is None and new.published_at is not None:
        existing.published_at = new.published_at
    elif (
        existing.published_at is not None
        and new.published_at is not None
        and existing.published_at != new.published_at
    ):
        warnings.append(f"conflicting_published_at: {existing.published_at} vs {new.published_at}")

    # Event date enrichment
    if existing.event_date is None and new.event_date is not None:
        existing.event_date = new.event_date
    elif (
        existing.event_date is not None
        and new.event_date is not None
        and existing.event_date != new.event_date
    ):
        warnings.append(f"conflicting_event_date: {existing.event_date} vs {new.event_date}")

    # Role: prefer richer role (primary > independent > relay > aggregator > unknown)
    role_priority = {"primary": 5, "independent": 4, "relay": 3, "aggregator": 2, "unknown": 1}
    existing_priority = role_priority.get(existing.role.value.lower(), 0)
    new_priority = role_priority.get(new.role.value.lower(), 0)
    if new_priority > existing_priority:
        existing.role = new.role

    # Verification status: use most recently changed
    if new.verification_changed_at and existing.verification_changed_at:
        if new.verification_changed_at > existing.verification_changed_at:
            existing.verification_status = new.verification_status
            existing.verification_changed_at = new.verification_changed_at
            existing.verification_changed_by = new.verification_changed_by
    elif new.verification_changed_at and not existing.verification_changed_at:
        existing.verification_status = new.verification_status
        existing.verification_changed_at = new.verification_changed_at
        existing.verification_changed_by = new.verification_changed_by


def _merge_candidate_metadata(
    representative: CandidateTopic,
    all_candidates: list[CandidateTopic],
) -> CandidateTopic:
    """Merge metadata from multiple candidates while preserving the representative.

    Returns a new candidate with merged uncertainties, actors, campaigns, etc.
    """
    from copy import deepcopy

    result = deepcopy(representative)

    # Union sets (deduplicated)
    all_uncertainties = set(result.uncertainties)
    all_actors = set(result.actors)
    all_campaigns = set(result.campaigns)
    all_malware = set(result.malware)
    all_cves = set(result.cves)
    all_victims = set(result.victims)
    all_sectors = set(result.sectors)
    all_countries = set(result.countries)
    all_artifacts = set(result.likely_artifacts)
    all_iocs = set(result.iocs)

    for candidate in all_candidates[1:]:  # Skip representative (already included)
        all_uncertainties.update(candidate.uncertainties)
        all_actors.update(candidate.actors)
        all_campaigns.update(candidate.campaigns)
        all_malware.update(candidate.malware)
        all_cves.update(candidate.cves)
        all_victims.update(candidate.victims)
        all_sectors.update(candidate.sectors)
        all_countries.update(candidate.countries)
        all_artifacts.update(candidate.likely_artifacts)
        all_iocs.update(candidate.iocs)

    result.uncertainties = tuple(sorted(all_uncertainties))
    result.actors = tuple(sorted(all_actors))
    result.campaigns = tuple(sorted(all_campaigns))
    result.malware = tuple(sorted(all_malware))
    result.cves = tuple(sorted(all_cves))
    result.victims = tuple(sorted(all_victims))
    result.sectors = tuple(sorted(all_sectors))
    result.countries = tuple(sorted(all_countries))
    result.likely_artifacts = tuple(sorted(all_artifacts))
    result.iocs = tuple(sorted(all_iocs))

    # Max technical potential
    result.technical_potential = max(
        (c.technical_potential for c in all_candidates),
        default=representative.technical_potential,
    )

    return result


