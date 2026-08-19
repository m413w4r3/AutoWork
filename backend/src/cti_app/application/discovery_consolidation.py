"""Projection consolidée de plusieurs DiscoveryBatch en une vue cohérente.

Algorithme conservatif (§20) :
1. Regroupement certain par titre normalisé identique.
2. Rapprochement fort : URL PRIMARY/INDEPENDENT commune ET autre signal fort.
3. Sinon, les sujets restent séparés.

Cette projection est en lecture seule : les batches d'origine ne sont jamais
mutés, ils restent auditables tels que produits par chaque contribution.
"""

from __future__ import annotations

from collections.abc import Sequence
from copy import deepcopy
from dataclasses import dataclass
from uuid import UUID

from cti_app.application.discovery_identity import (
    TopicMatchDecision,
    build_discovery_identity_index,
    canonical_source_key,
    match_topics,
)
from cti_app.domain.discovery import (
    CandidateTopic,
    DiscoveryBatch,
    ProvisionalDiscoveryIoc,
    SourceCandidate,
    SourceRole,
)

# Richesse relative d'un rôle de source : une contribution moins précise ne doit
# jamais dégrader un rôle déjà établi (§22).
_ROLE_PRIORITY = {
    SourceRole.PRIMARY: 5,
    SourceRole.INDEPENDENT: 4,
    SourceRole.RELAY: 3,
    SourceRole.AGGREGATOR: 2,
    SourceRole.SOCIAL: 2,
    SourceRole.UNKNOWN: 1,
}


@dataclass(frozen=True, slots=True)
class CandidateOccurrence:
    """Référence vers un candidat précis dans un batch précis."""

    batch_id: UUID
    candidate_id: UUID


@dataclass(slots=True)
class ConsolidatedCandidate:
    """Vue consolidée d'un sujet couvert par une ou plusieurs contributions."""

    representative: CandidateTopic
    member_references: tuple[CandidateOccurrence, ...]
    sources: list[SourceCandidate]
    duplicate_publication_count: int
    merge_warnings: tuple[str, ...]
    ambiguous_with: tuple[UUID, ...] = ()  # Refs to other clusters/occurrences this bridges

    @property
    def contribution_count(self) -> int:
        return len({ref.batch_id for ref in self.member_references})


def consolidate_discovery_batches(
    batches: Sequence[DiscoveryBatch],
) -> list[ConsolidatedCandidate]:
    """Consolide plusieurs batches de découverte en une vue unique.

    Algorithme : pairwise matrix + clique-only clustering (complet-link).
    Aucune transitivité, pas d'auto-merge sur titre seul, pas d'acteur comme signal fort.

    Args:
        batches: batches actifs d'une édition.

    Returns:
        Les sujets consolidés, publications dédupliquées et métadonnées fusionnées.
    """
    if not batches:
        return []

    # Build identity index once
    identity_index = build_discovery_identity_index(batches)

    # Index (batch_id, candidate_id) -> candidat, et rang chronologique du batch.
    candidates_by_batch: dict[UUID, dict[UUID, CandidateTopic]] = {
        batch.id: {candidate.id: candidate for candidate in batch.candidates} for batch in batches
    }
    batch_order: dict[UUID, int] = {batch.id: index for index, batch in enumerate(batches)}

    # Collect all occurrences
    all_occurrences: list[CandidateOccurrence] = []
    for batch in batches:
        for candidate in batch.candidates:
            all_occurrences.append(CandidateOccurrence(batch_id=batch.id, candidate_id=candidate.id))

    # Sort for determinism - CRITICAL for order-independence
    def stable_key(occ: CandidateOccurrence) -> tuple[str, str]:
        return (str(occ.batch_id), str(occ.candidate_id))

    all_occurrences = sorted(all_occurrences, key=stable_key)

    # === Step 1: Compute full pairwise match matrix (order-independent) ===
    pairwise_matrix: dict[tuple[CandidateOccurrence, CandidateOccurrence], TopicMatchDecision] = {}
    for i, occ1 in enumerate(all_occurrences):
        candidate1 = candidates_by_batch[occ1.batch_id].get(occ1.candidate_id)
        if candidate1 is None:
            continue
        for occ2 in all_occurrences[i + 1 :]:
            candidate2 = candidates_by_batch[occ2.batch_id].get(occ2.candidate_id)
            if candidate2 is None:
                continue
            result = match_topics(candidate1, candidate2, identity_index)
            # Store bidirectionally
            pairwise_matrix[(occ1, occ2)] = result.decision
            pairwise_matrix[(occ2, occ1)] = result.decision

    # === Step 2: Build cliques (complete-link: every pair must be SAME) ===
    # Track which occurrences have been assigned
    assigned: set[CandidateOccurrence] = set()
    clusters: list[set[CandidateOccurrence]] = []

    # Iterate in deterministic order
    for occ in all_occurrences:  # Already sorted above
        if occ in assigned:
            continue

        # Try to grow a clique starting from this occurrence
        clique: set[CandidateOccurrence] = {occ}
        assigned.add(occ)

        # Find all other occurrences that are SAME with occ
        same_with_occ = {
            other
            for other in all_occurrences
            if other != occ
            and other not in assigned
            and pairwise_matrix.get((occ, other)) is TopicMatchDecision.SAME
        }

        # Grow the clique greedily, but only if the new member is SAME with ALL current members
        # Sort for deterministic iteration order
        for candidate_member in sorted(same_with_occ, key=stable_key):
            is_compatible = all(
                pairwise_matrix.get((candidate_member, existing)) is TopicMatchDecision.SAME
                for existing in sorted(clique, key=stable_key)  # Sort clique too (it's a set)
                if existing != candidate_member
            )
            if is_compatible:
                clique.add(candidate_member)
                assigned.add(candidate_member)

        clusters.append(clique)

    # === Step 3: Detect bridges (occurrences matching multiple non-mutual clusters) ===
    # For each unassigned AMBIGUOUS bridge, mark it with the clusters it connects
    bridges: dict[CandidateOccurrence, tuple[UUID, ...]] = {}
    # Iterate in deterministic order (already sorted above)
    for occ in all_occurrences:
        if occ in assigned:
            continue
        # occ is a singleton; find which clusters it's SAME with
        matched_cluster_ids = set()
        for cluster_idx, cluster in enumerate(clusters):
            # Sort cluster members for deterministic iteration
            for cluster_member in sorted(cluster, key=stable_key):
                if pairwise_matrix.get((occ, cluster_member)) is TopicMatchDecision.SAME:
                    matched_cluster_ids.add(cluster_idx)
                    break
        if matched_cluster_ids:
            # This occurrence bridges multiple clusters -> flagged as ambiguous
            # Sort for deterministic ordering
            bridges[occ] = tuple(sorted(matched_cluster_ids))
        # Mark as assigned (singleton)
        assigned.add(occ)

    # === Step 4: Build consolidated candidates ===
    consolidated: list[ConsolidatedCandidate] = []
    for cluster in clusters:
        occurrences = sorted(
            cluster,
            key=lambda occ: (batch_order[occ.batch_id], str(occ.candidate_id)),
        )

        cluster_candidates: list[CandidateTopic] = []
        for occurrence in occurrences:
            member_candidate = candidates_by_batch[occurrence.batch_id].get(occurrence.candidate_id)
            if member_candidate is not None:
                cluster_candidates.append(member_candidate)
        if not cluster_candidates:
            continue

        representative = _pick_representative(occurrences, candidates_by_batch, batch_order)
        if representative is None:
            continue

        merged_sources, duplicate_count, merge_warnings = _merge_sources_in_cluster(
            cluster_candidates
        )
        merged_candidate = _merge_candidate_metadata(
            representative, cluster_candidates, merged_sources
        )

        consolidated.append(
            ConsolidatedCandidate(
                representative=merged_candidate,
                member_references=tuple(occurrences),
                sources=merged_sources,
                duplicate_publication_count=duplicate_count,
                merge_warnings=tuple(merge_warnings),
                ambiguous_with=(),  # Non-bridge
            )
        )

    # Add singleton bridges as separate consolidated candidates
    for bridge_occ in all_occurrences:
        if bridge_occ not in bridges:
            continue
        bridge_candidate = candidates_by_batch[bridge_occ.batch_id].get(bridge_occ.candidate_id)
        if bridge_candidate is None:
            continue

        merged_sources, duplicate_count, merge_warnings = _merge_sources_in_cluster(
            [bridge_candidate]
        )
        merged_candidate = _merge_candidate_metadata(
            bridge_candidate, [bridge_candidate], merged_sources
        )

        consolidated.append(
            ConsolidatedCandidate(
                representative=merged_candidate,
                member_references=(bridge_occ,),
                sources=merged_sources,
                duplicate_publication_count=duplicate_count,
                merge_warnings=tuple(merge_warnings),
                ambiguous_with=bridges[bridge_occ],  # Flag the bridges
            )
        )

    return consolidated


def _pick_representative(
    occurrences: Sequence[CandidateOccurrence],
    candidates_by_batch: dict[UUID, dict[UUID, CandidateTopic]],
    batch_order: dict[UUID, int],
) -> CandidateTopic | None:
    """Représentant = contribution la plus récente disposant de sources valides (§24)."""
    ranked = sorted(
        occurrences,
        key=lambda occ: (batch_order[occ.batch_id], str(occ.candidate_id)),
        reverse=True,
    )
    fallback: CandidateTopic | None = None
    for occurrence in ranked:
        candidate = candidates_by_batch[occurrence.batch_id].get(occurrence.candidate_id)
        if candidate is None:
            continue
        if candidate.selectable:
            return candidate
        fallback = fallback or candidate
    return fallback


def _merge_sources_in_cluster(
    candidates: Sequence[CandidateTopic],
) -> tuple[list[SourceCandidate], int, list[str]]:
    """Fusionne les publications d'un cluster en dédupliquant par URL canonique.

    Returns:
        (publications fusionnées, nombre d'occurrences déjà connues, avertissements)
    """
    merged: dict[str, SourceCandidate] = {}
    duplicate_count = 0
    merge_warnings: list[str] = []

    for candidate in candidates:
        for source in candidate.sources:
            url_key = canonical_source_key(source.canonical_url)
            existing = merged.get(url_key)
            if existing is None:
                # Copie défensive : la projection ne doit jamais muter le batch source.
                merged[url_key] = deepcopy(source)
                continue
            duplicate_count += 1
            _merge_source_metadata(existing, source, merge_warnings)

    return list(merged.values()), duplicate_count, merge_warnings


def _merge_source_metadata(
    existing: SourceCandidate,
    new: SourceCandidate,
    warnings: list[str],
) -> None:
    """Enrichit ``existing`` (déjà copié) à partir de ``new``.

    Une valeur connue comble une valeur inconnue sans avertissement ; deux valeurs
    connues contradictoires produisent un ``merge_warning`` et un choix déterministe.
    """
    if _is_unknown(existing.publisher):
        if not _is_unknown(new.publisher):
            existing.publisher = new.publisher
    elif not _is_unknown(new.publisher) and new.publisher != existing.publisher:
        warnings.append(
            f"publisher divergent pour {existing.canonical_url} : "
            f"{existing.publisher} / {new.publisher}"
        )

    for field_name, label in (
        ("published_at", "date de publication"),
        ("event_date", "date d'événement"),
    ):
        current = getattr(existing, field_name)
        incoming = getattr(new, field_name)
        if current is None:
            if incoming is not None:
                setattr(existing, field_name, incoming)
        elif incoming is not None and incoming != current:
            # Choix déterministe : la plus ancienne, mais le conflit reste tracé.
            setattr(existing, field_name, min(current, incoming))
            warnings.append(
                f"{label} divergente pour {existing.canonical_url} : "
                f"{current.isoformat()} / {incoming.isoformat()}"
            )

    if _ROLE_PRIORITY.get(new.role, 0) > _ROLE_PRIORITY.get(existing.role, 0):
        existing.role = new.role

    # Statut de vérification : le marquage humain le plus récent l'emporte (§23).
    if new.verification_changed_at is not None and (
        existing.verification_changed_at is None
        or new.verification_changed_at > existing.verification_changed_at
    ):
        existing.verification_status = new.verification_status
        existing.verification_changed_at = new.verification_changed_at
        existing.verification_changed_by = new.verification_changed_by


def _is_unknown(value: str | None) -> bool:
    return not value or value.strip().lower() == "unknown"


def _merge_candidate_metadata(
    representative: CandidateTopic,
    members: Sequence[CandidateTopic],
    merged_sources: list[SourceCandidate],
) -> CandidateTopic:
    """Retourne une copie du représentant enrichie de l'union des métadonnées (§24)."""
    result = deepcopy(representative)

    for field_name in (
        "uncertainties",
        "relevance_reasons",
        "actors",
        "campaigns",
        "malware",
        "cves",
        "victims",
        "sectors",
        "countries",
        "likely_artifacts",
        "iocs",
    ):
        # Union stable : ordre du représentant d'abord, puis les apports suivants.
        merged: dict[str, None] = dict.fromkeys(getattr(result, field_name))
        for member in members:
            merged.update(dict.fromkeys(getattr(member, field_name)))
        setattr(result, field_name, tuple(merged))

    result.technical_potential = max(member.technical_potential for member in members)

    seen_ioc_keys: set[tuple[str, str]] = set()
    provisional: list[ProvisionalDiscoveryIoc] = []
    for member in members:
        for ioc in member.provisional_iocs:
            key = (str(getattr(ioc, "type", "")).lower(), str(getattr(ioc, "value", "")).lower())
            if key in seen_ioc_keys:
                continue
            seen_ioc_keys.add(key)
            provisional.append(deepcopy(ioc))
    result.provisional_iocs = provisional

    # Les sources fusionnées remplacent celles du seul représentant.
    result.sources = merged_sources
    return result
