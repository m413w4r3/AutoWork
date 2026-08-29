"""Deterministic rendering helpers for the final publication.

AutoWork owns the reference numbering, not the model: `[S1]` is an id local to
one conversation, `[1]` is what the reader sees. Numbers are assigned by first
use in the synthesis so the text reads in order, and a source keeps its number
for the whole publication.
"""

from __future__ import annotations

import re

from cti_app.application.production_normalization import canonical_indicator_key
from cti_app.application.production_parsers import (
    DisplayPolicy,
    ExtractionItem,
    IndicatorStatus,
    ReferenceReport,
    TechnicalExtraction,
)
from cti_app.domain.publication import ArtifactType

_MARKER = re.compile(r"\[(S\d{1,3})\]", re.IGNORECASE)

_CATEGORY_LABELS = {
    "actors": "Acteurs",
    "campaigns": "Campagnes",
    "victimology": "Victimologie",
    "infection_chain": "Chaîne d'infection",
    "malware": "Maliciels",
    "tools": "Outils",
    "ttps": "Techniques",
    "cves": "CVE",
    "protocols": "Protocoles",
    "network_artifacts": "Artefacts réseau",
    "infrastructure": "Infrastructure",
    "files": "Fichiers",
    "commands": "Commandes",
    "persistence": "Persistance",
    "detections": "Détections",
    "other_technical": "Autres éléments techniques",
}


def build_reference_numbering(report: ReferenceReport, synthesis_text: str) -> dict[str, int]:
    """Assign `[1]`, `[2]`, … by first use, then any source left over.

    Only sources the report actually defines get a number, so a marker the
    corpus does not know can never become a footnote.
    """
    known = {source.local_id for source in report.sources}
    numbering: dict[str, int] = {}
    for match in _MARKER.finditer(synthesis_text):
        marker = match.group(1).upper()
        if marker in known and marker not in numbering:
            numbering[marker] = len(numbering) + 1
    for source in report.sources:
        if source.local_id not in numbering:
            numbering[source.local_id] = len(numbering) + 1
    return numbering


def apply_numbering(text: str, numbering: dict[str, int]) -> str:
    """Replace conversation-local markers with the reader-facing numbers."""

    def swap(match: re.Match[str]) -> str:
        marker = match.group(1).upper()
        number = numbering.get(marker)
        return f"[{number}]" if number else ""

    return _MARKER.sub(swap, text)


def collect_indicators(extraction: TechnicalExtraction) -> list[ExtractionItem]:
    """Explicitly qualified IOC, deduplicated by type and canonical value."""
    seen: set[tuple[str, str]] = set()
    out: list[ExtractionItem] = []
    for item in extraction.items:
        if (
            not item.supported
            or item.indicator_status is not IndicatorStatus.CONFIRMED_IOC
            or item.display_policy not in {DisplayPolicy.IOC_SECTION, DisplayPolicy.BOTH}
            or item.artifact_type is None
        ):
            continue
        artifact_type = (
            item.artifact_type
            if isinstance(item.artifact_type, ArtifactType)
            else ArtifactType(item.artifact_type)
        )
        try:
            key = (artifact_type.value, canonical_indicator_key(item.value, artifact_type))
        except ValueError:
            continue
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def render_publication_markdown(
    *,
    subject_title: str,
    report: ReferenceReport,
    extraction: TechnicalExtraction,
    synthesis_text: str,
    numbering: dict[str, int],
) -> str:
    """Render the publication: title, chronology, synthesis, indicators."""
    lines: list[str] = [f"# {subject_title}", ""]

    lines.append("## Références")
    lines.append("")
    by_id = {source.local_id: source for source in report.sources}
    for event in report.events:
        markers = "".join(f"[{numbering[sid]}]" for sid in event.source_ids if sid in numbering)
        prefix = f"{event.event_date.isoformat()} — " if event.event_date else ""
        lines.append(f"- {prefix}{event.text} {markers}".rstrip())
    lines.append("")

    for local_id, number in sorted(numbering.items(), key=lambda item: item[1]):
        source = by_id.get(local_id)
        if source is None:
            continue
        parts = [part for part in (source.title, source.publisher) if part]
        if source.published_at:
            parts.append(source.published_at.isoformat())
        label = " · ".join(parts) if parts else source.canonical_url
        lines.append(f"[{number}] {label} — {source.canonical_url}")
    lines.append("")

    lines.append("## Synthèse technique")
    lines.append("")
    lines.append(apply_numbering(synthesis_text, numbering).strip())
    lines.append("")

    indicators = collect_indicators(extraction)
    if indicators:
        lines.append("## Indicateurs de compromission")
        lines.append("")
        for item in indicators:
            kind = (
                item.artifact_type.value
                if isinstance(item.artifact_type, ArtifactType)
                else item.artifact_type
            ) or _CATEGORY_LABELS.get(item.category, item.category)
            markers = "".join(f"[{numbering[sid]}]" for sid in item.source_ids if sid in numbering)
            lines.append(f"- `{item.value}` ({kind}) {markers}".rstrip())
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"
