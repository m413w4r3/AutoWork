"""Brief-specific synthesis instructions and deterministic content checks."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import replace
from datetime import date, timedelta
from difflib import SequenceMatcher

from cti_app.domain.production import ProductionEvidenceBasis, ProductionInputSnapshot
from cti_app.domain.production_extraction import ProductionExtractionV1
from cti_app.domain.production_references import (
    ProductionEditorialRole,
    ProductionReferenceKind,
    ProductionReferenceTier,
)
from cti_app.domain.production_relevance import (
    RelevanceClassification,
    RelevanceProjectionV1,
    RelevanceReasonCode,
)
from cti_app.domain.production_synthesis import (
    EvidenceKind,
    ExtractionEvidenceRefV1,
    SynthesisTimelineEntryV1,
    extraction_evidence_elements,
    source_metadata_evidence_ref_v1,
)
from cti_app.domain.wire_markers import contains_wire_marker_line

BRIEF_SYNTHESIS_PROMPT_VERSION = "brief-synthesis-v1-french-grounded-chronology"
BRIEF_TIMELINE_POLICY_VERSION = (
    "brief-timeline-v4-narrative-cited-relevance-ranked-publication-fallback"
)
BRIEF_CHRONOLOGY_SOURCE_PUBLICATION_FALLBACK_WARNING = (
    "brief_chronology_source_publication_fallback"
)
BRIEF_CHRONOLOGY_METADATA_PUBLICATION_FALLBACK_WARNING = (
    "brief_chronology_metadata_publication_fallback"
)

_BRIEF_FORBIDDEN_OUTPUT = re.compile(
    r"(?i)\b(?:recommandation(?:s)?|note\s+d['\u2019]analyste|liste\s+d['\u2019]ioc|"
    r"vue\s+d['\u2019]ensemble|overview)\b"
)
_BRIEF_RECOMMENDATION_LANGUAGE = re.compile(
    r"(?i)\b(?:il\s+est\s+(?:recommandé|conseillé)|nous\s+recommandons|"
    r"il\s+faudrait|doit\s+être\s+mis\s+en\s+place)\b"
)
_BRIEF_LIST_PREFIX = re.compile(r"(?m)^\s*(?:[-*•]|\d+[.)])\s+")
_PUBLICATION_EVENT_TEXT = re.compile(
    r"(?i)\b(?:publication|parution|publi(?:é|ée|és|ées)|published|release(?:d)?|"
    r"mise en ligne|mis en ligne)\b"
)
_TIMELINE_WORD = re.compile(r"[^\W_]+", re.UNICODE)
_TIMELINE_ARTICLES = frozenset({"le", "la", "les", "un", "une", "l", "the", "an"})
_TIMELINE_DEDUPE_MIN_WORDS = 7
_TIMELINE_DEDUPE_SEQUENCE_OVERLAP = 0.88


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
    *,
    snapshot: ProductionInputSnapshot | None = None,
    projection: RelevanceProjectionV1 | None = None,
    cited_evidence_refs: Iterable[ExtractionEvidenceRefV1] = (),
    warnings: list[str] | None = None,
) -> tuple[SynthesisTimelineEntryV1, ...] | None:
    """Select up to four grounded events cited by the lead, or relevant fallbacks.

    Without a grounded lead citation, direct subject-matched primary events rank
    first, followed by other direct events. The four selected events nearest to
    the subject period win; ties prefer the most recent dates, and output is
    chronological. Metadata supplies only the final, explicitly marked fallback.
    """
    cited_refs = set(cited_evidence_refs)
    payload_by_ref: dict[ExtractionEvidenceRefV1, Mapping[str, object]] = {
        ref: payload for ref, payload in extraction_evidence_elements(extraction)
    }
    grounded: list[SynthesisTimelineEntryV1] = []
    for entry in entries:
        if entry.event_date is None or not entry.evidence_refs:
            continue
        supporting_refs: list[ExtractionEvidenceRefV1] = []
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
                supporting_refs.append(ref)
        if supporting_refs:
            grounded.append(replace(entry, evidence_refs=tuple(supporting_refs)))

    deduplicated: list[SynthesisTimelineEntryV1] = []
    for entry in grounded:
        matches = [
            index
            for index, existing in enumerate(deduplicated)
            if _same_brief_timeline_event(existing, entry)
        ]
        if len(matches) != 1:
            deduplicated.append(entry)
            continue
        index = matches[0]
        existing = deduplicated[index]
        evidence_refs = tuple(dict.fromkeys((*existing.evidence_refs, *entry.evidence_refs)))
        deduplicated[index] = replace(existing, evidence_refs=evidence_refs)

    cited_grounded = [
        replace(
            entry,
            evidence_refs=tuple(ref for ref in entry.evidence_refs if ref in cited_refs),
        )
        for entry in deduplicated
        if any(ref in cited_refs for ref in entry.evidence_refs)
    ]
    if cited_grounded:
        return tuple(sorted(cited_grounded, key=lambda entry: entry.event_date or date.max)[:4])

    relevance_ranked: list[tuple[tuple[int, int, int], SynthesisTimelineEntryV1]] = []
    for entry in deduplicated:
        direct_refs: list[ExtractionEvidenceRefV1] = []
        subject_matched_primary = False
        for ref in entry.evidence_refs:
            if projection is None:
                # Legacy callers without a projection have no classification data.
                direct_refs.append(ref)
                continue
            item = projection.classification_for(ref)
            if item.classification is not RelevanceClassification.DIRECT:
                continue
            direct_refs.append(ref)
            subject_matched_primary = subject_matched_primary or (
                item.reason_code is RelevanceReasonCode.SUBJECT_MATCHED_PRIMARY
            )
        if not direct_refs:
            continue
        ranked_entry = replace(entry, evidence_refs=tuple(direct_refs))
        assert ranked_entry.event_date is not None
        period_distance = _brief_period_distance(ranked_entry.event_date, snapshot)
        rank = (
            0 if subject_matched_primary else 1,
            period_distance,
            -ranked_entry.event_date.toordinal(),
        )
        relevance_ranked.append((rank, ranked_entry))

    if relevance_ranked:
        selected = [entry for _, entry in sorted(relevance_ranked, key=lambda item: item[0])[:4]]
        return tuple(sorted(selected, key=lambda entry: entry.event_date or date.max))

    fallback = _core_primary_publication_timeline_entry(extraction, payload_by_ref)
    if fallback is not None:
        if warnings is not None:
            warnings.append(BRIEF_CHRONOLOGY_SOURCE_PUBLICATION_FALLBACK_WARNING)
        return (fallback,)
    metadata_fallback = (
        brief_metadata_publication_fallback_entry(snapshot, extraction)
        if snapshot is not None
        else None
    )
    if metadata_fallback is None:
        return None
    if warnings is not None:
        warnings.append(BRIEF_CHRONOLOGY_METADATA_PUBLICATION_FALLBACK_WARNING)
    return (metadata_fallback,)


def _brief_period_distance(
    event_date: date,
    snapshot: ProductionInputSnapshot | None,
) -> int:
    """Distance in days from an event to the frozen subject period."""
    if snapshot is None or snapshot.period_start <= event_date <= snapshot.period_end:
        return 0
    if event_date < snapshot.period_start:
        return (snapshot.period_start - event_date).days
    return (event_date - snapshot.period_end).days


def brief_metadata_publication_fallback_entry(
    snapshot: ProductionInputSnapshot,
    extraction: ProductionExtractionV1,
) -> SynthesisTimelineEntryV1 | None:
    """Build a dated, source-linked entry from the earliest eligible frozen source metadata."""
    if (
        snapshot.subject_id != extraction.subject_id
        or snapshot.input_hash != extraction.production_input_hash
    ):
        return None
    extraction_sources_by_url = {source.canonical_url: source for source in extraction.sources}
    earliest: tuple[date, str, SynthesisTimelineEntryV1] | None = None
    earliest_allowed = snapshot.period_start - timedelta(days=365)
    for metadata in snapshot.core_sources:
        source = extraction_sources_by_url.get(metadata.canonical_url)
        published_at = metadata.published_at
        if (
            source is None
            or source.tier is not ProductionReferenceTier.CORE
            or source.editorial_role is not ProductionEditorialRole.PRIMARY
            or source.kind is not ProductionReferenceKind.PUBLICATION
            or type(published_at) is not date
            or published_at > snapshot.research_date
            or published_at < earliest_allowed
        ):
            continue
        publisher = _bounded_metadata_component(metadata.publisher, 120)
        title = _bounded_metadata_component(metadata.title, 240)
        if title is None:
            continue
        ref = source_metadata_evidence_ref_v1(
            source_document_id=source.source_document_id,
            canonical_url=metadata.canonical_url,
            title=metadata.title,
            publisher=metadata.publisher,
            published_at=published_at,
        )
        publisher_is_latin = publisher is not None and _is_predominantly_latin(publisher)
        if not publisher_is_latin:
            text = "Publication d'un article de la source principale."
        elif not _is_predominantly_latin(title):
            text = f"Publication d'un article par {publisher}."
        else:
            text = f"Publication par {publisher} de « {title} »."
        entry = SynthesisTimelineEntryV1(
            event_date=published_at,
            date_text=None,
            text=text,
            evidence_refs=(ref,),
        )
        candidate = (published_at, metadata.canonical_url, entry)
        if earliest is None or candidate[:2] < earliest[:2]:
            earliest = candidate
    return earliest[2] if earliest is not None else None


def _bounded_metadata_component(value: str, max_chars: int) -> str | None:
    printable = "".join(
        char if char.isprintable() and unicodedata.category(char) != "Cf" else " " for char in value
    )
    normalized = " ".join(printable.split())
    if not normalized:
        return None
    if len(normalized) <= max_chars:
        return normalized
    shortened = normalized[: max_chars - 1].rsplit(" ", 1)[0].rstrip()
    if not shortened:
        shortened = normalized[: max_chars - 1]
    return shortened + "…"


def _is_predominantly_latin(value: str) -> bool:
    for char in value:
        if unicodedata.bidirectional(char) in {"R", "AL", "AN"}:
            return False
        if unicodedata.category(char).startswith("L"):
            if "LATIN" not in unicodedata.name(char, ""):
                return False
    return True


def _core_primary_publication_timeline_entry(
    extraction: ProductionExtractionV1,
    payload_by_ref: Mapping[ExtractionEvidenceRefV1, Mapping[str, object]],
) -> SynthesisTimelineEntryV1 | None:
    """Return one source-verified publication event from the CORE primary source."""
    sources_by_id = {source.source_document_id: source for source in extraction.sources}
    for ref, payload in payload_by_ref.items():
        if ref.kind is not EvidenceKind.EVENT:
            continue
        source = sources_by_id[ref.source_document_id]
        if (
            source.tier is not ProductionReferenceTier.CORE
            or source.editorial_role is not ProductionEditorialRole.PRIMARY
            or source.kind is not ProductionReferenceKind.PUBLICATION
        ):
            continue
        raw_date = payload.get("event_date")
        evidence_quote = payload.get("evidence_quote")
        text = payload.get("text")
        date_text = payload.get("date_text")
        if (
            not isinstance(raw_date, str)
            or payload.get("evidence_basis") != ProductionEvidenceBasis.SOURCE_VERIFIED.value
            or not isinstance(evidence_quote, str)
            or not isinstance(text, str)
            or _PUBLICATION_EVENT_TEXT.search(text) is None
        ):
            continue
        event_date = date.fromisoformat(raw_date)
        if not _source_contains_date(evidence_quote, event_date):
            continue
        display_date_text = (
            date_text
            if isinstance(date_text, str)
            and date_text.strip()
            and _source_contains_date(date_text, event_date)
            else None
        )
        return SynthesisTimelineEntryV1(
            event_date=event_date,
            date_text=display_date_text,
            text=text,
            evidence_refs=(ref,),
        )
    return None


def _same_brief_timeline_event(
    left: SynthesisTimelineEntryV1,
    right: SynthesisTimelineEntryV1,
) -> bool:
    """Match same-date entries only when their content words are unchanged."""
    if left.event_date is None or left.event_date != right.event_date:
        return False
    left_words = tuple(
        word.casefold()
        for word in _TIMELINE_WORD.findall(left.text)
        if word.casefold() not in _TIMELINE_ARTICLES
    )
    right_words = tuple(
        word.casefold()
        for word in _TIMELINE_WORD.findall(right.text)
        if word.casefold() not in _TIMELINE_ARTICLES
    )
    if left_words == right_words:
        return True
    if min(len(left_words), len(right_words)) < _TIMELINE_DEDUPE_MIN_WORDS:
        return False
    left_tokens = set(left_words)
    right_tokens = set(right_words)
    # Different content words may carry a different actor, action, or detail.
    # Keep those entries separate even when their overall wording is similar.
    if left_tokens != right_tokens:
        return False
    sequence_overlap = SequenceMatcher(None, left_words, right_words, autojunk=False).ratio()
    return sequence_overlap >= _TIMELINE_DEDUPE_SEQUENCE_OVERLAP


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
    if contains_wire_marker_line(text):
        return "brief_wire_marker_forbidden"
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
    "BRIEF_CHRONOLOGY_METADATA_PUBLICATION_FALLBACK_WARNING",
    "BRIEF_CHRONOLOGY_SOURCE_PUBLICATION_FALLBACK_WARNING",
    "BRIEF_SYNTHESIS_PROMPT_VERSION",
    "BRIEF_TIMELINE_POLICY_VERSION",
    "brief_metadata_publication_fallback_entry",
    "brief_timeline_from_extraction",
    "build_brief_synthesis_prompt",
    "validate_brief_narrative",
]
