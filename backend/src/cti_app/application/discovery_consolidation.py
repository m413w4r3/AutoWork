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
    candidates_match_strongly,
    canonical_source_key,
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

    @property
    def contribution_count(self) -> int:
        return len({ref.batch_id for ref in self.member_references})


def consolidate_discovery_batches(
    batches: Sequence[DiscoveryBatch],
) -> list[ConsolidatedCandidate]:
    """Consolide plusieurs batches de découverte en une vue unique.

    Args:
        batches: batches actifs d'une édition, dans l'ordre chronologique.

    Returns:
        Les sujets consolidés, publications dédupliquées et métadonnées fusionnées.
    """
    if not batches:
        return []

    # Index (batch_id, candidate_id) -> candidat, et rang chronologique du batch.
    candidates_by_batch: dict[UUID, dict[UUID, CandidateTopic]] = {
        batch.id: {candidate.id: candidate for candidate in batch.candidates} for batch in batches
    }
    batch_order: dict[UUID, int] = {batch.id: index for index, batch in enumerate(batches)}

    # Chaque cluster est identifié par l'occurrence qui l'a ouvert.
    clusters: dict[CandidateOccurrence, list[CandidateOccurrence]] = {}

    for batch in batches:
        for candidate in batch.candidates:
            occurrence = CandidateOccurrence(batch_id=batch.id, candidate_id=candidate.id)

            matched_key: CandidateOccurrence | None = None
            for key, members in clusters.items():
                # Comparer à tous les membres déjà rattachés : le rapprochement
                # peut porter sur une occurrence enrichie plutôt que sur celle
                # qui a ouvert le cluster.
                for member in members:
                    other = candidates_by_batch[member.batch_id].get(member.candidate_id)
                    if other is not None and candidates_match_strongly(candidate, other):
                        matched_key = key
                        break
                if matched_key is not None:
                    break

            if matched_key is not None:
                clusters[matched_key].append(occurrence)
            else:
                clusters[occurrence] = [occurrence]

    consolidated: list[ConsolidatedCandidate] = []
    for occurrences in clusters.values():
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
                member_references=tuple(
                    sorted(
                        occurrences,
                        key=lambda occ: (batch_order[occ.batch_id], str(occ.candidate_id)),
                    )
                ),
                sources=merged_sources,
                duplicate_publication_count=duplicate_count,
                merge_warnings=tuple(merge_warnings),
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
