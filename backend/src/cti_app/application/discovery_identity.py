"""Small deterministic identity helpers used by local discovery planning."""

from __future__ import annotations

import re
import unicodedata

from cti_app.domain.discovery import CandidateTopic


def normalize(value: str) -> str:
    ascii_value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    return " ".join(re.findall(r"[a-z0-9]+", ascii_value.casefold()))


def candidates_match_strongly(left: CandidateTopic, right: CandidateTopic) -> bool:
    """Return true only for deterministic identity anchors, never a bare actor."""
    if normalize(left.title) == normalize(right.title):
        return True
    if {source.canonical_url for source in left.sources} & {
        source.canonical_url for source in right.sources
    }:
        return True
    for field_name in ("campaigns", "malware", "cves"):
        left_values = {normalize(value) for value in getattr(left, field_name) if normalize(value)}
        right_values = {
            normalize(value) for value in getattr(right, field_name) if normalize(value)
        }
        if left_values & right_values:
            return True
    return False
