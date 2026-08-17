"""Pure helpers for discovery candidate identity, matching, and deduplication.

Ces fonctions sont la référence unique partagée par :
- discovery_consolidation.py (projection consolidée de plusieurs batches)
- editorial.py (rapprochement candidat ↔ groupe éditorial)

Elles doivent rester pures : aucun accès base, aucun appel modèle.
"""

from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher
from typing import TYPE_CHECKING

from cti_app.domain.discovery import SourceRole

if TYPE_CHECKING:
    from cti_app.domain.discovery import CandidateTopic

STRONG_SOURCE_ROLES = frozenset({SourceRole.PRIMARY, SourceRole.INDEPENDENT})

# Seuil de similarité de titre valant "autre signal fort" (aligné sur editorial).
STRONG_TITLE_SIMILARITY = 0.75


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
        left_tokens = {token for item in left_values for token in explicit_entity_tokens(item)}
        right_tokens = {token for item in right_values for token in explicit_entity_tokens(item)}
        if left_tokens & right_tokens:
            return True
    return False


def shared_strong_urls(left: CandidateTopic, right: CandidateTopic) -> set[str]:
    """URL canoniques PRIMARY/INDEPENDENT communes aux deux candidats."""
    return {
        source.canonical_url for source in left.sources if source.role in STRONG_SOURCE_ROLES
    } & {source.canonical_url for source in right.sources if source.role in STRONG_SOURCE_ROLES}


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
