"""Brief-specific synthesis instructions and deterministic content checks."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from datetime import date

from cti_app.domain.production_extraction import ProductionExtractionV1
from cti_app.domain.production_synthesis import (
    EvidenceKind,
    ExtractionEvidenceRefV1,
    SynthesisTimelineEntryV1,
    extraction_evidence_elements,
)

BRIEF_SYNTHESIS_PROMPT_VERSION = "brief-synthesis-v1-french-grounded-chronology"

_BRIEF_FORBIDDEN_OUTPUT = re.compile(
    r"(?i)\b(?:recommandation(?:s)?|note\s+d['\u2019]analyste|liste\s+d['\u2019]ioc|"
    r"vue\s+d['\u2019]ensemble|overview)\b"
)
_BRIEF_RECOMMENDATION_LANGUAGE = re.compile(
    r"(?i)\b(?:il\s+est\s+(?:recommandé|conseillé)|nous\s+recommandons|"
    r"il\s+faudrait|doit\s+être\s+mis\s+en\s+place)\b"
)
_BRIEF_LIST_PREFIX = re.compile(r"(?m)^\s*(?:[-*•]|\d+[.)])\s+")


def build_brief_synthesis_prompt(
    *,
    subject_title: str,
    publication_language: str,
    tlp: str,
    discovery_summary: str,
    actor_or_campaign: str,
    period_start: str,
    period_end: str,
    evidence_blocks: Sequence[Sequence[str]],
    reserve_blocks: Sequence[Sequence[str]],
    prompt_version: str = BRIEF_SYNTHESIS_PROMPT_VERSION,
) -> str:
    """Build the brief's French instructions around the shared evidence pack."""
    lines = [
        "BRIEF SYNTHESIS DRAFT REQUEST",
        f"Prompt version: {prompt_version}",
        "Publication kind: brief",
        f"Publication language: {publication_language}",
        f"Subject title: {subject_title}",
        f"TLP: {tlp}",
        f"Discovery summary: {discovery_summary}",
        f"Actor or campaign: {actor_or_campaign}",
        f"Period: {period_start} to {period_end}",
        "",
        "Rédige en français un titre éditorial et une synthèse narrative en paragraphes.",
        (
            "Vise environ 200 à 450 mots : c'est un objectif éditorial, "
            "jamais une limite ou un filtre."
        ),
        "Rends uniquement les blocs TITLE et LEAD du format de sortie partagé.",
        "Place tous les paragraphes de synthèse dans LEAD. N'ajoute aucune SECTION.",
        "N'invente aucun fait, aucune date, aucun lien causal ni aucune attribution.",
        "Chaque paragraphe doit citer les handles E exacts qui étayent ses affirmations.",
        "Présente les attributions comme des hypothèses lorsqu'elles ne sont pas confirmées.",
        "Attribue explicitement à leur source les réserves, contradictions et limites.",
        (
            "N'ajoute ni overview, ni catégorie vide, ni liste d'IOC, ni recommandation, "
            "ni note d'analyste."
        ),
        "N'utilise ni liste, tableau, puce, titre de section ou commentaire sur ces consignes.",
        "La chronologie de publication est construite séparément depuis les événements sourcés.",
        "N'ajoute pas de date à la prose si elle n'apparaît pas dans les preuves fournies.",
        "Utilise exactement ce format en blocs texte, sans JSON ni texte hors blocs :",
        "@@TITLE@@",
        "[Groupe] Titre",
        "@@END TITLE@@",
        "@@LEAD@@",
        "@@CLAIM L001@@",
        "EVIDENCE: E001, E002",
        "TEXT: un paragraphe narratif en français",
        "@@CLAIM L002@@",
        "EVIDENCE: E003",
        "TEXT: un autre paragraphe si les preuves le permettent",
        "",
        "",
        "CURRENT EVIDENCE PACK",
    ]
    for block in evidence_blocks:
        lines.extend(block)
        lines.append("")
    if reserve_blocks:
        lines.extend(
            (
                "@@RESERVES / CONTRADICTIONS — CONTEXTE NON AUTORITATIF@@",
                (
                    "Ces éléments qualifient les preuves. "
                    "Ne les présente pas comme des faits confirmés."
                ),
                "Nomme la source concernée chaque fois qu'une réserve est intégrée à la prose.",
            )
        )
        for block in reserve_blocks:
            lines.extend(block)
            lines.append("")
        lines.append("@@END RESERVES / CONTRADICTIONS@@")
    return "\n".join(lines).strip()


def brief_timeline_from_extraction(
    extraction: ProductionExtractionV1,
    entries: Iterable[SynthesisTimelineEntryV1],
) -> tuple[SynthesisTimelineEntryV1, ...] | None:
    """Keep at most four dated events whose date text is present in source evidence.

    ``None`` means no defensible chronology exists and the brief must be sent
    to review.  The function never fills dates or event wording from metadata.
    """
    payload_by_ref: dict[ExtractionEvidenceRefV1, Mapping[str, object]] = {
        ref: payload for ref, payload in extraction_evidence_elements(extraction)
    }
    grounded: list[SynthesisTimelineEntryV1] = []
    for entry in entries:
        if entry.event_date is None or not entry.evidence_refs:
            continue
        source_support = False
        for ref in entry.evidence_refs:
            if ref.kind is not EvidenceKind.EVENT:
                continue
            payload = payload_by_ref.get(ref)
            if payload is None:
                continue
            source_date_text = payload.get("date_text")
            evidence_quote = payload.get("evidence_quote")
            # ``date_text`` is extracted model output, not evidence. When it
            # carries a date, require it to agree with both the canonical sort
            # date and the cited source quote before displaying it in a brief.
            source_values = [evidence_quote]
            if isinstance(source_date_text, str) and source_date_text.strip():
                source_values.append(source_date_text)
            if all(
                isinstance(value, str) and _source_contains_date(value, entry.event_date)
                for value in source_values
            ):
                source_support = True
                break
        if source_support:
            grounded.append(entry)
    if not grounded:
        return None
    return tuple(grounded[:4])


def validate_brief_narrative(
    *,
    title: str,
    paragraphs: Iterable[str],
    section_count: int,
) -> str | None:
    """Reject output shapes that the brief projection must never publish."""
    if section_count:
        return "brief_sections_forbidden"
    text = "\n".join((title, *tuple(paragraphs)))
    if _BRIEF_LIST_PREFIX.search(text):
        return "brief_list_forbidden"
    if _BRIEF_FORBIDDEN_OUTPUT.search(text) or _BRIEF_RECOMMENDATION_LANGUAGE.search(text):
        return "brief_editorial_content_forbidden"
    return None


def _source_contains_date(value: str, event_date: date) -> bool:
    """Match source date wording at day precision without trusting a sort key."""
    normalized = value.casefold()

    def contains_literal(literal: str) -> bool:
        return (
            re.search(
                rf"(?<!\w){re.escape(literal)}(?!\w)",
                normalized,
            )
            is not None
        )

    iso = event_date.isoformat()
    if contains_literal(iso):
        return True
    numeric = (
        f"{event_date.day:02d}/{event_date.month:02d}/{event_date.year}",
        f"{event_date.day}/{event_date.month}/{event_date.year}",
        f"{event_date.day:02d}-{event_date.month:02d}-{event_date.year}",
    )
    if any(contains_literal(form) for form in numeric):
        return True
    month_names = (
        ("janvier", "janv", "january", "jan"),
        ("février", "fevrier", "févr", "fevr", "february", "feb"),
        ("mars", "march", "mar"),
        ("avril", "avr", "april", "apr"),
        ("mai", "may"),
        ("juin", "june", "jun"),
        ("juillet", "juil", "july", "jul"),
        ("août", "aout", "august", "aug"),
        ("septembre", "sept", "september", "sep"),
        ("octobre", "oct", "october"),
        ("novembre", "nov", "november"),
        ("décembre", "decembre", "déc", "dec", "december"),
    )[event_date.month - 1]
    day = str(event_date.day)
    year = str(event_date.year)
    if 10 <= event_date.day % 100 <= 13:
        ordinal_suffix = "th"
    else:
        ordinal_suffix = {1: "st", 2: "nd", 3: "rd"}.get(event_date.day % 10, "th")
    suffixes = (ordinal_suffix, "er") if event_date.day == 1 else (ordinal_suffix,)
    day_pattern = rf"{day}(?:{'|'.join(suffixes)})?"
    month_pattern = (
        "(?:"
        + "|".join(re.escape(month) for month in sorted(month_names, key=len, reverse=True))
        + ")"
    )
    return any(
        re.search(pattern, normalized) is not None
        for pattern in (
            rf"(?<!\w){month_pattern}\s+{day_pattern},?\s+{year}(?!\w)",
            rf"(?<!\w){day_pattern}(?:\s+of)?\s+{month_pattern}\s+{year}(?!\w)",
        )
    )


__all__ = [
    "BRIEF_SYNTHESIS_PROMPT_VERSION",
    "brief_timeline_from_extraction",
    "build_brief_synthesis_prompt",
    "validate_brief_narrative",
]
