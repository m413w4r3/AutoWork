"""Line-oriented wire format for the external discovery merge planner.

The ChatGPT bridge is never asked for JSON: it inserts UI markers and unescaped
quotes into JSON strings. The merge model reads a plain-text projection of the
subjects and answers with `GROUP` blocks, parsed tolerantly here and validated
afterwards by `validate_merge_plan`.
"""

from __future__ import annotations

import re

from cti_app.domain.discovery_cumulative import (
    DiscoveryMergeGroup,
    DiscoveryMergePlanV1,
    MergeConfidence,
    MergeDisposition,
    MergeEvidence,
)

MERGE_OUTPUT_FORMAT = """FORMAT DE SORTIE
Réponds uniquement avec des blocs texte, sans Markdown, sans texte autour.
Un bloc par groupe :

GROUP
EXISTING: X1, X2          (identifiants X du groupe ; "aucun" s'il n'y en a pas)
INCOMING: C1, C3          (identifiants C du groupe, au moins un)
CONFIDENCE: high|medium|low
DISPOSITION: apply|review
RATIONALE: une phrase qui justifie la décision
URLS: url ; url           (publications partagées, optionnel)
CAMPAIGNS: a ; b          (campagnes partagées, optionnel)
MALWARE: a ; b            (malwares partagés, optionnel)
IDENTIFIERS: a ; b        (identifiants explicites partagés, optionnel)
BASIS: a ; b              (base sémantique, optionnel)
CONFLICTS: a ; b          (signaux de conflit, optionnel)
FLAGS: a ; b              (par ex. incoming_subject_may_require_split, optionnel)
END

Puis, si nécessaire, une ligne WARNING: texte par avertissement global."""

_SUBJECT_LIST_FIELDS = (
    ("acteurs", "actors"),
    ("campagnes", "campaigns"),
    ("malwares", "malware"),
    ("cves", "cves"),
    ("victimes", "victims"),
    ("secteurs", "sectors"),
    ("pays", "countries"),
    ("artefacts probables", "likely_artifacts"),
    ("incertitudes", "uncertainties"),
)
# ChatGPT UI markers: private-use delimited citations and content references.
_CITATION_MARKER = re.compile(r"[-][^-]*[-]|:?chatgpt-content-reference\{[^}]*\}")
# The bridge renders URLs as Markdown links: keep the destination only.
_MARKDOWN_LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")
_FENCE = re.compile(r"^\s*```\w*\s*$")
_FIELD = re.compile(r"^\s*(?:[-*]\s*)?([A-Za-z_]+)\s*[:=]\s*(.*?)\s*$")
_HANDLE = re.compile(r"[XC]\d+")


def render_merge_subjects(subjects: list[dict[str, object]]) -> str:
    """Plain-text projection of merge subjects, one `[handle]` block each."""
    blocks: list[str] = []
    for subject in subjects:
        lines = [
            f"[{subject['handle']}]",
            f"titre: {_one_line(subject.get('title'))}",
            f"résumé: {_one_line(subject.get('summary'))}",
        ]
        for label, key in _SUBJECT_LIST_FIELDS:
            values = subject.get(key)
            if isinstance(values, list) and values:
                lines.append(f"{label}: " + " ; ".join(_one_line(value) for value in values))
        if subject.get("technical_potential"):
            lines.append(f"potentiel technique: {_one_line(subject['technical_potential'])}")
        sources = subject.get("sources")
        if isinstance(sources, list):
            lines.append("sources (url | titre | éditeur | rôle | publiée | événement):")
            for source in sources:
                if not isinstance(source, dict):
                    continue
                parts = (
                    source.get("canonical_url"),
                    source.get("title"),
                    source.get("publisher"),
                    source.get("role"),
                    source.get("published_at"),
                    source.get("event_date"),
                )
                lines.append("- " + " | ".join(_one_line(part) for part in parts))
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def parse_merge_plan(text: str) -> DiscoveryMergePlanV1:
    """Parse `GROUP … END` blocks. A malformed group raises `ValueError`."""
    groups: list[dict[str, str]] = []
    warnings: list[str] = []
    current: dict[str, str] | None = None
    for raw_line in text.splitlines():
        line = _CITATION_MARKER.sub("", raw_line).strip()
        if not line or _FENCE.match(line):
            continue
        keyword = line.rstrip(":").strip().upper()
        if keyword == "GROUP":
            current = {}
            groups.append(current)
            continue
        if keyword == "END":
            current = None
            continue
        match = _FIELD.match(line)
        if match is None:
            continue
        key, value = match.group(1).upper(), match.group(2)
        if key == "WARNING":
            if value:
                warnings.append(value)
        elif current is not None:
            current[key] = value
    if not groups:
        raise ValueError("Merge model returned no GROUP block")
    return DiscoveryMergePlanV1(
        groups=[_build_group(index, fields) for index, fields in enumerate(groups)],
        warnings=warnings,
    )


def _build_group(index: int, fields: dict[str, str]) -> DiscoveryMergeGroup:
    try:
        confidence = MergeConfidence(fields.get("CONFIDENCE", "").strip().lower())
        disposition = MergeDisposition(fields.get("DISPOSITION", "").strip().lower())
    except ValueError as exc:
        raise ValueError(f"group {index}: invalid CONFIDENCE or DISPOSITION") from exc
    return DiscoveryMergeGroup(
        existing_subject_handles=_handles(fields.get("EXISTING", "")),
        incoming_candidate_handles=_handles(fields.get("INCOMING", "")),
        confidence=confidence,
        disposition=disposition,
        rationale=fields.get("RATIONALE", ""),
        evidence=MergeEvidence(
            shared_publication_urls=_items(fields.get("URLS", "")),
            shared_campaigns=_items(fields.get("CAMPAIGNS", "")),
            shared_malware=_items(fields.get("MALWARE", "")),
            shared_explicit_identifiers=_items(fields.get("IDENTIFIERS", "")),
            semantic_basis=_items(fields.get("BASIS", "")),
            conflict_signals=_items(fields.get("CONFLICTS", "")),
        ),
        flags=_items(fields.get("FLAGS", "")),
    )


def _handles(value: str) -> list[str]:
    return _HANDLE.findall(value.upper())


def _items(value: str) -> list[str]:
    if value.strip().lower() in {"", "aucun", "aucune", "none", "-"}:
        return []
    value = _MARKDOWN_LINK.sub(r"\1", value)
    return [item.strip() for item in value.split(";") if item.strip()]


def _one_line(value: object) -> str:
    return " ".join(str(value if value is not None else "").split())
