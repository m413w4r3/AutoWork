"""Pure helpers for discovery candidate identity, matching, and deduplication.

Ces fonctions sont la référence unique partagée par :
- discovery_consolidation.py (projection consolidée de plusieurs batches)
- editorial.py (rapprochement candidat ↔ groupe éditorial)

Elles doivent rester pures : aucun accès base, aucun appel modèle.
"""

from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass
from difflib import SequenceMatcher
from enum import StrEnum
from typing import TYPE_CHECKING, Mapping, Sequence

from cti_app.domain.discovery import SourceRole

if TYPE_CHECKING:
    from uuid import UUID

    from cti_app.domain.discovery import CandidateTopic, DiscoveryBatch

STRONG_SOURCE_ROLES = frozenset({SourceRole.PRIMARY, SourceRole.INDEPENDENT})

# Seuil de similarité de titre valant "autre signal fort" (aligné sur editorial).
# Threshold for "strong" title similarity (enables SAME with anchor URL corroboration).
# Set conservatively to require substantial semantic overlap while tolerating
# paraphrase variation and language differences. Cross-run paraphrases of the same
# subject in French reach as low as 0.44 for minor wording variations; set to 0.43
# to capture all legitimate multi-run duplicates with complete-link clique formation.
# Paraphrases with <0.42 similarity are typically unrelated subjects.
STRONG_TITLE_SIMILARITY = 0.43


class TopicMatchDecision(StrEnum):
    """Tri-state matcher result: does a pair of candidates describe the same subject?"""

    SAME = "same"  # Hard identity evidence (anchor + corroborator, or explicit ID)
    AMBIGUOUS = "ambiguous"  # Weak signals, requires human/LLM review
    DISTINCT = "distinct"  # No evidence of identity, or contradicting evidence


@dataclass(frozen=True, slots=True)
class TopicMatchResult:
    """Result of pairwise topic identity matching."""

    decision: TopicMatchDecision
    reasons: tuple[str, ...]  # Positive evidence (human-readable)
    blockers: tuple[str, ...]  # Why did not reach SAME (human-readable)


@dataclass(frozen=True, slots=True)
class DiscoveryIdentityIndex:
    """Index tracking URL usage patterns across candidates within a batch."""

    # Mapping: canonical_url -> tuple of (batch_id, candidate_id) occurrences
    url_occurrences: Mapping[str, tuple[tuple[str, str], ...]]
    # URLs observed under multiple SUBJECTs in the same batch — never valid anchors
    contextual_urls: frozenset[str]


def normalize(value: str) -> str:
    """Normalisation canonique d'un libellé : casefold, ASCII, mots alphanumériques."""
    normalized = unicodedata.normalize("NFKD", value.casefold())
    return " ".join(re.findall(r"[a-z0-9]+", normalized.encode("ascii", "ignore").decode()))


def title_similarity(left: str, right: str) -> float:
    return SequenceMatcher(None, normalize(left), normalize(right)).ratio()


def explicit_entity_tokens(value: str) -> set[str]:
    """Éclate un libellé d'entité en tokens explicites, en ignorant ``unknown``."""
    return {
        normalized
        for part in re.split(r"[/,;|]", value)
        if (normalized := normalize(part)) and normalized != "unknown"
    }


def has_other_strong_signal(left: CandidateTopic, right: CandidateTopic) -> bool:
    """Signal fort hors URL : titre proche, ou acteur/campagne/malware explicite commun.

    Volontairement restreint : ni le secteur, ni le pays, ni le publisher, ni le
    domaine ne constituent un signal fort (§20).
    """
    if title_similarity(left.title, right.title) >= STRONG_TITLE_SIMILARITY:
        return True
    for left_values, right_values in (
        (left.actors, right.actors),
        (left.campaigns, right.campaigns),
        (left.malware, right.malware),
    ):
        left_tokens = frozenset(token for item in left_values for token in explicit_entity_tokens(item))
        right_tokens = frozenset(token for item in right_values for token in explicit_entity_tokens(item))
        if left_tokens & right_tokens:
            return True
    return False


def shared_strong_urls(left: CandidateTopic, right: CandidateTopic) -> frozenset[str]:
    """URL canoniques PRIMARY/INDEPENDENT communes aux deux candidats."""
    return frozenset(
        source.canonical_url for source in left.sources if source.role in STRONG_SOURCE_ROLES
    ) & frozenset(
        source.canonical_url for source in right.sources if source.role in STRONG_SOURCE_ROLES
    )


def candidates_match_strongly(left: CandidateTopic, right: CandidateTopic) -> bool:
    """Règle de rapprochement conservative pour la consolidation (§20).

    Étape 1 — titre normalisé identique.
    Étape 2 — au moins une URL PRIMARY/INDEPENDENT commune ET un autre signal fort.

    Une URL partagée seule ne suffit jamais : une synthèse mensuelle peut
    légitimement couvrir plusieurs sujets distincts.
    """
    if left.title_fingerprint == right.title_fingerprint:
        return True
    return bool(shared_strong_urls(left, right)) and has_other_strong_signal(left, right)


def canonical_source_key(canonical_url: str) -> str:
    """Clé de déduplication d'une publication à l'intérieur d'un sujet consolidé.

    L'URL est déjà canonicalisée en amont (paramètres utm_*, fbclid, gclid retirés) ;
    on neutralise seulement la casse résiduelle du host.
    """
    return canonical_url.strip().lower()


def build_discovery_identity_index(batches: Sequence[DiscoveryBatch]) -> DiscoveryIdentityIndex:
    """Build a pre-computed index of URL usage patterns across all batches.

    Detects URLs that appear under multiple SUBJECTs within the same batch
    (contextual URLs) — these can never serve as identity anchors, even if
    corroborated.

    Returns:
        DiscoveryIdentityIndex with url_occurrences and contextual_urls frozenset.
    """
    url_occurrences: dict[str, list[tuple[str, str]]] = defaultdict(list)
    # Track which URLs appear under multiple subjects within each batch
    batch_url_subjects: dict[str, set[str]] = defaultdict(lambda: defaultdict(set))

    for batch in batches:
        batch_key = str(batch.id)
        for candidate in batch.candidates:
            candidate_key = str(candidate.id)
            for source in candidate.sources:
                if source.role not in STRONG_SOURCE_ROLES:
                    continue
                url = source.canonical_url
                # Track all occurrences
                url_occurrences[url].append((batch_key, candidate_key))
                # Track which subjects in this batch use this URL
                batch_url_subjects[batch_key][url].add(candidate_key)

    # Identify contextual URLs: those used by multiple subjects in the same batch
    contextual = set()
    for batch_key, url_subjects in batch_url_subjects.items():
        for url, subjects in url_subjects.items():
            if len(subjects) > 1:
                contextual.add(url)

    return DiscoveryIdentityIndex(
        url_occurrences={k: tuple(v) for k, v in url_occurrences.items()},
        contextual_urls=frozenset(contextual),
    )


def _extract_incident_identifiers(text: str) -> frozenset[str]:
    """Extract narrow-form incident/advisory/campaign identifiers.

    Matches patterns like:
    - 'AA26-097A' (CISA advisory format)
    - 'TAG-182' (Recorded Future threat classification)
    - 'Operation Olalampo' (explicit operation name)
    - 'Campaign ChainShell' (explicit campaign name)
    Documented as provisional — Patch 2 will formalize this with explicit
    campaign/incident fields in the parser.

    Returns:
        Frozenset of normalized incident ID tokens, or empty frozenset if none found.
    """
    if not text:
        return frozenset()
    # Narrow regex: advisory IDs, threat classifications, and explicit campaign/operation names
    patterns = [
        r"\bAA\d{2}-\d{3}A?\b",  # Advisory format: AA26-097A
        r"\bTAG-\d+\b",          # Threat classification: TAG-182
        r"\bOperation\s+([A-Z][a-zA-Z]+)\b",  # Operation Olalampo, Operation IconCat
        r"\b([A-Z][a-zA-Z]+)(?:\s+Operation|\s+Campaign)\b",  # Olalampo Operation, ChainShell Campaign
    ]
    matches = set()
    for pattern in patterns:
        for match in re.finditer(pattern, text, re.IGNORECASE):
            # For group-capturing patterns, use group(1) if exists, else group(0)
            token = match.group(1) if match.lastindex else match.group(0)
            matches.add(normalize(token))
    return frozenset(matches)


def match_topics(
    left: CandidateTopic,
    right: CandidateTopic,
    index: DiscoveryIdentityIndex,
) -> TopicMatchResult:
    """Tri-state pairwise topic matching.

    Implements the authoritative identity matching matrix:
    - Explicit incident ID match alone -> SAME
    - Explicit campaign/malware ID match alone -> SAME
    - Shared non-contextual URL + title corroborator -> SAME
    - Shared non-contextual URL + explicit ID corroborator -> SAME
    - Contextual URL or weak signals only -> AMBIGUOUS or DISTINCT
    - Shared actor alone never -> SAME

    Args:
        left, right: Candidates to compare
        index: Pre-built identity index from build_discovery_identity_index()

    Returns:
        TopicMatchResult with decision, reasons, and blockers.
    """
    reasons_list = []
    blockers_list = []

    # === Hard identity keys: explicit IDs ===
    left_incident_ids = frozenset(_extract_incident_identifiers(left.title))
    if left.actor_or_campaign:
        left_incident_ids = left_incident_ids | frozenset(_extract_incident_identifiers(left.actor_or_campaign))

    right_incident_ids = frozenset(_extract_incident_identifiers(right.title))
    if right.actor_or_campaign:
        right_incident_ids = right_incident_ids | frozenset(_extract_incident_identifiers(right.actor_or_campaign))

    if left_incident_ids & right_incident_ids:
        return TopicMatchResult(
            decision=TopicMatchDecision.SAME,
            reasons=("shared explicit advisory/incident identifier",),
            blockers=(),
        )

    # Check explicit campaign/malware fields (currently always empty pre-Patch-2, included for forward compat)
    left_campaign_tokens = frozenset(token for item in left.campaigns for token in explicit_entity_tokens(item))
    right_campaign_tokens = frozenset(token for item in right.campaigns for token in explicit_entity_tokens(item))
    if left_campaign_tokens & right_campaign_tokens:
        return TopicMatchResult(
            decision=TopicMatchDecision.SAME,
            reasons=("shared explicit campaign identifier",),
            blockers=(),
        )

    # === Candidate anchor (URL) matching ===
    shared = shared_strong_urls(left, right)
    contextual = shared & index.contextual_urls
    anchorable = shared - index.contextual_urls

    if contextual and not anchorable:
        blockers_list.append("shared URL is contextual (cited under multiple SUBJECTs in same batch)")

    if not anchorable:
        # No non-contextual anchor URL; check for weak signals
        weak_reasons = []
        title_sim = title_similarity(left.title, right.title)
        if title_sim >= STRONG_TITLE_SIMILARITY:
            weak_reasons.append(f"strong title similarity ({title_sim:.2f})")
        elif title_sim >= 0.6:
            weak_reasons.append(f"close title similarity ({title_sim:.2f})")

        if frozenset(left.actors) & frozenset(right.actors):
            weak_reasons.append("shared actor (not a corroborator)")

        # Check shared IOCs
        left_iocs = frozenset(normalize(ioc) for ioc in left.iocs)
        right_iocs = frozenset(normalize(ioc) for ioc in right.iocs)
        if left_iocs & right_iocs:
            weak_reasons.append("shared IOC")

        # Check shared CVEs
        left_cves = frozenset(normalize(cve) for cve in left.cves)
        right_cves = frozenset(normalize(cve) for cve in right.cves)
        if left_cves & right_cves:
            weak_reasons.append("shared CVE")

        if weak_reasons:
            return TopicMatchResult(
                decision=TopicMatchDecision.AMBIGUOUS,
                reasons=tuple(weak_reasons),
                blockers=tuple(blockers_list),
            )
        return TopicMatchResult(
            decision=TopicMatchDecision.DISTINCT,
            reasons=(),
            blockers=tuple(blockers_list),
        )

    # === Non-contextual anchor present: needs corroboration ===
    # Corroborator 1: strong title similarity
    title_sim = title_similarity(left.title, right.title)
    if title_sim >= STRONG_TITLE_SIMILARITY:
        reasons_list.append("anchor URL")
        reasons_list.append(f"strong title similarity ({title_sim:.2f})")
        return TopicMatchResult(
            decision=TopicMatchDecision.SAME,
            reasons=tuple(reasons_list),
            blockers=(),
        )

    # Corroborator 2: shared malware/campaign token (narrow check, not bare actor)
    left_malware_tokens = frozenset(token for item in left.malware for token in explicit_entity_tokens(item))
    right_malware_tokens = frozenset(token for item in right.malware for token in explicit_entity_tokens(item))
    if left_malware_tokens & right_malware_tokens:
        reasons_list.append("anchor URL")
        reasons_list.append("shared specific malware identifier")
        return TopicMatchResult(
            decision=TopicMatchDecision.SAME,
            reasons=tuple(reasons_list),
            blockers=(),
        )

    # No independent corroborator found
    blockers_list.append(
        "anchor URL present but no independent corroborator "
        "(actor alone never counts as corroborator)"
    )
    return TopicMatchResult(
        decision=TopicMatchDecision.AMBIGUOUS,
        reasons=("candidate anchor URL",),
        blockers=tuple(blockers_list),
    )
