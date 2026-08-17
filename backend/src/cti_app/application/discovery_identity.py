"""Pure helpers for discovery candidate identity, matching, and deduplication.

These functions are used by:
- discovery_consolidation.py (consolidating multiple batches)
- editorial.py (matching candidates to editorial groups)
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from difflib import SequenceMatcher
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from cti_app.domain.discovery import CandidateTopic


def normalize_title(title: str) -> str:
    """Produce a fingerprint for title-based exact matching.

    Removes accents, normalizes whitespace, lowercases.
    """
    nfd = unicodedata.normalize("NFD", title)
    sans_accents = "".join(c for c in nfd if unicodedata.category(c) != "Mn")
    normalized = re.sub(r"\s+", " ", sans_accents.lower().strip())
    return normalized


def title_fingerprint(title: str) -> str:
    """SHA256 of normalized title for consistent hashing."""
    return hashlib.sha256(normalize_title(title).encode()).hexdigest()


def explicit_entity_tokens(candidate: CandidateTopic) -> set[str]:
    """Extract unique entity strings from a candidate.

    Returns a set of normalized, lowercased entity names (actors, campaigns, etc.)
    for strong signal matching.
    """
    tokens = set()

    for actor in candidate.actors:
        tokens.add(normalize_title(actor))
    for campaign in candidate.campaigns:
        tokens.add(normalize_title(campaign))
    for malware in candidate.malware:
        tokens.add(normalize_title(malware))
    for victim in candidate.victims:
        tokens.add(normalize_title(victim))
    for sector in candidate.sectors:
        tokens.add(normalize_title(sector))

    return tokens


def has_strong_signal(
    left_title: str,
    right_title: str,
    left_entities: set[str],
    right_entities: set[str],
    left_campaigns: set[str],
    right_campaigns: set[str],
    left_malware: set[str],
    right_malware: set[str],
) -> bool:
    """Check if two candidates have strong semantic overlap.

    Strong signals:
    - Title similarity > 0.7
    - Shared explicit entity (actor, campaign, malware, etc.)
    - Shared campaign or malware
    """
    # Title similarity
    left_norm = normalize_title(left_title)
    right_norm = normalize_title(right_title)
    similarity = SequenceMatcher(None, left_norm, right_norm).ratio()
    if similarity > 0.7:
        return True

    # Shared entity tokens (actors, victims, sectors)
    if left_entities & right_entities:
        return True

    # Shared campaign or malware
    if left_campaigns & right_campaigns:
        return True
    if left_malware & right_malware:
        return True

    return False


def canonical_source_key(canonical_url: str) -> str:
    """Unique key for URL deduplication within a consolidated candidate.

    The URL should already be canonicalized (UTM params removed, etc.)
    but we normalize to handle case-insensitive domains.
    """
    return canonical_url.lower()
