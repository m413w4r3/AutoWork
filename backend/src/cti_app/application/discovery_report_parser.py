from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import date
from itertools import pairwise
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from cti_app.domain.classification import TLP
from cti_app.domain.discovery import (
    CandidateTopic,
    IncompleteSourceCandidate,
    IocPresence,
    PeriodRelation,
    SourceCandidate,
    SourceRole,
    canonicalize_http_url,
)

PARSER_VERSION = "chatgpt-markdown-v1"
_SUBJECT = re.compile(r"^\s*##\s+SUBJECT\b\s*[:#-]?\s*(.*?)\s*$", re.IGNORECASE)
_PUBLICATION = re.compile(r"^\s*###\s+PUBLICATION\b\s*[:#-]?\s*(.*?)\s*$", re.IGNORECASE)
_HEADING = re.compile(r"^\s*(#{1,6})\s+(.+?)\s*$")
_FIELD = re.compile(
    r"^\s*(?:[-*]\s*)?(?:\*\*)?([\wÀ-ÿ][\wÀ-ÿ ./'-]*?)(?:\*\*)?\s*[:\N{FULLWIDTH COLON}]\s*(.*)$"
)
_MARKDOWN_URL = re.compile(r"\[[^\]]*\]\(\s*(https?://[^\s)>]+)\s*\)", re.IGNORECASE)
_ANGLE_URL = re.compile(r"<\s*(https?://[^>\s]+)\s*>", re.IGNORECASE)
_NAKED_URL = re.compile(r"https?://[^\s<>\]\[\"']+", re.IGNORECASE)
_CONTEXT_MARKERS = (
    "axe complementaire",
    "hors fenetre",
    "contexte",
    "controle a posteriori",
)
_KNOWN_TOPIC_FIELDS = {
    "title",
    "presentation",
    "actor_or_campaign",
    "technical_potential",
    "technical_potential_reason",
    "artifacts",
    "uncertainties",
}
_KNOWN_PUBLICATION_FIELDS = {
    "title",
    "url",
    "publisher",
    "published_at",
    "period_relation",
    "source_role",
    "ioc_presence",
    "ioc_declared_count",
    "ioc_visible_count",
}
_ARTIFACTS = {"ioc", "samples", "configurations", "pcap", "yara", "suricata", "none", "unknown"}


class ReportParsingError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.research_model_run_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class ParsedDiscoveryReport:
    candidates: list[CandidateTopic]
    citations: tuple[dict[str, str | None], ...]
    warnings: tuple[str, ...]
    report_sha256: str
    status: str


def parse_discovery_report(
    report: str,
    *,
    visible_citations: object,
    period_start: date,
    period_end: date,
    tlp: TLP,
    sensitivity: str,
    external_llm_allowed: bool,
) -> ParsedDiscoveryReport:
    del period_start, period_end  # Dates are never inferred from the edition window.
    if not report.strip():
        raise ReportParsingError("report_empty", "Le rapport ChatGPT archivé est vide.")
    if _is_contract_echo(report):
        raise ReportParsingError(
            "report_parsing_failed",
            "L'ancienne sortie de contrat ne constitue pas un rapport de découverte.",
        )
    report_sha = hashlib.sha256(report.encode()).hexdigest()
    citation_values = _normalize_citations(visible_citations)
    if any(_SUBJECT.match(line) for line in report.splitlines()):
        candidates, warnings = _parse_current(
            report,
            report_sha=report_sha,
            tlp=tlp,
            sensitivity=sensitivity,
            external_llm_allowed=external_llm_allowed,
        )
    else:
        candidates, warnings = _parse_legacy(
            report,
            report_sha=report_sha,
            tlp=tlp,
            sensitivity=sensitivity,
            external_llm_allowed=external_llm_allowed,
        )
    _preserve_unattached_citations(
        candidates,
        citation_values,
        report_sha=report_sha,
        tlp=tlp,
        sensitivity=sensitivity,
        external_llm_allowed=external_llm_allowed,
        warnings=warnings,
    )
    if not candidates:
        raise ReportParsingError(
            "report_parsing_failed", "Aucun sujet ni aucune URL explicite n'a pu être extrait."
        )
    valid_count = sum(len(candidate.sources) for candidate in candidates)
    if valid_count == 0:
        warnings.append("no_explicit_url: aucune URL HTTP(S) valide n'a été trouvée.")
    status = "partial" if warnings else "completed"
    return ParsedDiscoveryReport(
        candidates=candidates,
        citations=tuple(citation_values),
        warnings=tuple(dict.fromkeys(warnings)),
        report_sha256=report_sha,
        status=status,
    )


def extract_http_urls(text: str) -> list[tuple[str, str]]:
    """Return (raw, canonical) URLs in source order, deduplicated canonically."""
    found: list[tuple[int, str]] = []
    for expression in (_MARKDOWN_URL, _ANGLE_URL, _NAKED_URL):
        for match in expression.finditer(text):
            raw = match.group(1) if match.lastindex else match.group(0)
            found.append((match.start(), raw))
    unique: list[tuple[str, str]] = []
    seen: set[str] = set()
    for _, raw_value in sorted(found, key=lambda item: item[0]):
        raw = raw_value.rstrip(".,;:!?)\u00a0")
        try:
            canonical = canonicalize_http_url(raw)
        except (TypeError, UnicodeError, ValueError):
            continue
        if canonical in seen:
            continue
        seen.add(canonical)
        unique.append((raw, canonical))
    return unique


def _parse_current(
    report: str,
    *,
    report_sha: str,
    tlp: TLP,
    sensitivity: str,
    external_llm_allowed: bool,
) -> tuple[list[CandidateTopic], list[str]]:
    lines = report.splitlines()
    starts = [index for index, line in enumerate(lines) if _SUBJECT.match(line)]
    starts.append(len(lines))
    candidates: list[CandidateTopic] = []
    warnings: list[str] = []
    for ordinal, (start, end) in enumerate(pairwise(starts), 1):
        header = _SUBJECT.match(lines[start])
        assert header is not None
        local_ref = header.group(1).strip() or f"S{ordinal}"
        block = "\n".join(lines[start:end]).strip()
        publication_starts = [
            index for index in range(start + 1, end) if _PUBLICATION.match(lines[index])
        ]
        first_publication = publication_starts[0] if publication_starts else end
        topic_fields, topic_unknown = _parse_fields(lines[start + 1 : first_publication])
        topic_warnings = [
            f"subject {local_ref}: champ non reconnu '{name}'" for name in topic_unknown
        ]
        sources: list[SourceCandidate] = []
        incomplete: list[IncompleteSourceCandidate] = []
        publication_starts.append(end)
        for pub_ordinal, (pub_start, pub_end) in enumerate(pairwise(publication_starts), 1):
            pub_header = _PUBLICATION.match(lines[pub_start])
            assert pub_header is not None
            pub_ref = pub_header.group(1).strip() or f"P{pub_ordinal}"
            pub_block = "\n".join(lines[pub_start:pub_end]).strip()
            fields, unknown = _parse_fields(lines[pub_start + 1 : pub_end])
            pub_warnings = [
                f"publication {pub_ref}: champ non reconnu '{name}'" for name in unknown
            ]
            urls = extract_http_urls("\n".join((fields.get("url", ""), pub_block)))
            if not urls:
                raw_url = fields.get("url") or None
                pub_warnings.append(f"publication {pub_ref}: no_explicit_url")
                incomplete.append(
                    _incomplete_source(pub_ref, fields, raw_url, pub_warnings, pub_block)
                )
                continue
            for raw_url, canonical_url in urls:
                sources.append(
                    _valid_source(
                        pub_ref,
                        fields,
                        raw_url,
                        canonical_url,
                        pub_warnings,
                        pub_block,
                        report_sha,
                        tlp,
                        sensitivity,
                        external_llm_allowed,
                    )
                )
            topic_warnings.extend(pub_warnings)
        candidate = _candidate(
            local_ref,
            topic_fields,
            sources,
            incomplete,
            topic_warnings,
            block,
            report_sha,
            tlp,
            sensitivity,
            external_llm_allowed,
        )
        candidates.append(candidate)
        warnings.extend(topic_warnings)
    return candidates, warnings


def _parse_legacy(
    report: str,
    *,
    report_sha: str,
    tlp: TLP,
    sensitivity: str,
    external_llm_allowed: bool,
) -> tuple[list[CandidateTopic], list[str]]:
    lines = report.splitlines()
    headings = [
        (index, len(match.group(1)), match.group(2).strip())
        for index, line in enumerate(lines)
        if (match := _HEADING.match(line))
    ]
    candidates: list[CandidateTopic] = []
    warnings = [
        "format historique: présentation et potentiel technique peuvent être incomplets; "
        "consulter le rapport original."
    ]
    context = False
    ordinal = 0
    for position, (start, level, heading) in enumerate(headings):
        normalized_heading = _normalize_text(heading)
        if any(marker in normalized_heading for marker in _CONTEXT_MARKERS):
            context = True
            continue
        if level != 2:
            continue
        end = headings[position + 1][0] if position + 1 < len(headings) else len(lines)
        block = "\n".join(lines[start:end]).strip()
        urls = extract_http_urls(block)
        if not urls:
            continue
        ordinal += 1
        in_period = bool(
            re.search(
                r"publication\s*:.*(?:dans la (?:fen[eê]tre|p[eé]riode)|in[ -]period)",
                block,
                re.IGNORECASE,
            )
        )
        context_only = context or not in_period
        local_ref = f"legacy-{ordinal}"
        title = re.sub(r"[*_`]", "", heading).strip()
        publisher = title.split("—", 1)[0].split("-", 1)[0].strip() or "unknown"
        sources = [
            SourceCandidate(
                id=uuid5(NAMESPACE_URL, f"{report_sha}:{canonical}"),
                url=canonical,
                raw_url=raw,
                title=title,
                publisher=publisher,
                role=SourceRole.UNKNOWN,
                published_at=_first_explicit_date(block),
                period_relation=(
                    PeriodRelation.IN_PERIOD if in_period else PeriodRelation.OUTSIDE_PERIOD
                ),
                citation=block,
                local_ref=f"P{url_index}",
                parsing_warnings=("Métadonnées extraites depuis un rapport historique.",),
                markdown_block=block,
                tlp=tlp,
                sensitivity=sensitivity,
                external_llm_allowed=external_llm_allowed,
            )
            for url_index, (raw, canonical) in enumerate(urls, 1)
        ]
        candidates.append(
            CandidateTopic(
                id=uuid5(NAMESPACE_URL, f"{report_sha}:{local_ref}"),
                local_ref=local_ref,
                title=title,
                summary="Présentation non disponible dans ce rapport historique.",
                novelty="Non précisée dans le rapport historique.",
                technical_potential=0,
                technical_potential_reason="Non extractible de manière fiable.",
                uncertainties=("Métadonnées historiques à vérifier.",),
                relevance_reasons=("Publication explicitement présente dans le rapport archivé.",),
                actors=(),
                campaigns=(),
                malware=(),
                cves=(),
                victims=(),
                sectors=(),
                countries=(),
                likely_artifacts=("unknown",),
                sources=sources,
                tlp=tlp,
                sensitivity=sensitivity,
                external_llm_allowed=external_llm_allowed,
                parsing_warnings=(warnings[0],),
                markdown_block=block,
                context_only=context_only,
            )
        )
    return candidates, warnings


def _parse_fields(lines: list[str]) -> tuple[dict[str, str], list[str]]:
    fields: dict[str, str] = {}
    unknown: list[str] = []
    current: str | None = None
    for line in lines:
        match = _FIELD.match(line)
        if match:
            key = _normalize_key(match.group(1))
            current = key
            value = match.group(2).strip().strip("*")
            fields[key] = value
            known = _KNOWN_TOPIC_FIELDS | _KNOWN_PUBLICATION_FIELDS
            if key not in known:
                unknown.append(key)
            continue
        if current is not None and line.strip() and not _HEADING.match(line):
            fields[current] = f"{fields[current]}\n{line.strip()}".strip()
    return fields, unknown


def _candidate(
    local_ref: str,
    fields: dict[str, str],
    sources: list[SourceCandidate],
    incomplete: list[IncompleteSourceCandidate],
    warnings: list[str],
    block: str,
    report_sha: str,
    tlp: TLP,
    sensitivity: str,
    external_llm_allowed: bool,
) -> CandidateTopic:
    title = fields.get("title", "").strip() or f"Sujet {local_ref}"
    presentation = fields.get("presentation", "").strip()
    if not presentation:
        presentation = "Présentation non disponible dans le rapport de découverte."
        warnings.append(f"subject {local_ref}: champ presentation absent")
    actor = fields.get("actor_or_campaign", "unknown").strip() or "unknown"
    technical = _bounded_int(fields.get("technical_potential"), 0, 4)
    if technical is None:
        technical = 0
        warnings.append(f"subject {local_ref}: technical_potential absent ou invalide")
    artifacts = tuple(
        value
        for value in _split_list(fields.get("artifacts", "unknown"), normalized=True)
        if value in _ARTIFACTS
    ) or ("unknown",)
    uncertainties = tuple(_split_list(fields.get("uncertainties", "")))
    return CandidateTopic(
        id=uuid5(NAMESPACE_URL, f"{report_sha}:{local_ref}"),
        local_ref=local_ref,
        title=title,
        summary=presentation,
        novelty="Proposition issue du regroupement ChatGPT, à vérifier humainement.",
        technical_potential=technical,
        technical_potential_reason=fields.get(
            "technical_potential_reason", "Non précisé dans le rapport de découverte."
        ),
        uncertainties=uncertainties,
        relevance_reasons=("Sujet proposé dans le rapport ChatGPT archivé.",),
        actors=() if actor.casefold() == "unknown" else (actor,),
        campaigns=(),
        malware=(),
        cves=(),
        victims=(),
        sectors=(),
        countries=(),
        likely_artifacts=artifacts,
        sources=sources,
        incomplete_sources=incomplete,
        tlp=tlp,
        sensitivity=sensitivity,
        external_llm_allowed=external_llm_allowed,
        actor_or_campaign=actor,
        parsing_warnings=tuple(dict.fromkeys(warnings)),
        markdown_block=block,
    )


def _valid_source(
    local_ref: str,
    fields: dict[str, str],
    raw_url: str,
    canonical_url: str,
    warnings: list[str],
    block: str,
    report_sha: str,
    tlp: TLP,
    sensitivity: str,
    external_llm_allowed: bool,
) -> SourceCandidate:
    title = fields.get("title", "").strip() or f"Publication {local_ref}"
    return SourceCandidate(
        id=uuid5(NAMESPACE_URL, f"{report_sha}:{canonical_url}"),
        url=canonical_url,
        raw_url=raw_url,
        title=title,
        publisher=fields.get("publisher", "unknown").strip() or "unknown",
        role=_enum_or_default(SourceRole, fields.get("source_role"), SourceRole.UNKNOWN),
        published_at=_explicit_date(fields.get("published_at")),
        period_relation=_enum_or_default(
            PeriodRelation, fields.get("period_relation"), PeriodRelation.UNKNOWN
        ),
        ioc_presence=_enum_or_default(IocPresence, fields.get("ioc_presence"), IocPresence.UNKNOWN),
        ioc_declared_count=_non_negative_int(fields.get("ioc_declared_count")),
        ioc_visible_count=_non_negative_int(fields.get("ioc_visible_count")),
        local_ref=local_ref,
        citation=block,
        parsing_warnings=tuple(dict.fromkeys(warnings)),
        markdown_block=block,
        tlp=tlp,
        sensitivity=sensitivity,
        external_llm_allowed=external_llm_allowed,
    )


def _incomplete_source(
    local_ref: str,
    fields: dict[str, str],
    raw_url: str | None,
    warnings: list[str],
    block: str,
) -> IncompleteSourceCandidate:
    return IncompleteSourceCandidate(
        title=fields.get("title", "").strip() or f"Publication {local_ref}",
        publisher=fields.get("publisher", "unknown"),
        raw_url=raw_url,
        local_ref=local_ref,
        published_at=_explicit_date(fields.get("published_at")),
        period_relation=_enum_or_default(
            PeriodRelation, fields.get("period_relation"), PeriodRelation.UNKNOWN
        ),
        role=_enum_or_default(SourceRole, fields.get("source_role"), SourceRole.UNKNOWN),
        ioc_presence=_enum_or_default(IocPresence, fields.get("ioc_presence"), IocPresence.UNKNOWN),
        ioc_declared_count=_non_negative_int(fields.get("ioc_declared_count")),
        ioc_visible_count=_non_negative_int(fields.get("ioc_visible_count")),
        parsing_warnings=tuple(dict.fromkeys(warnings)),
        markdown_block=block,
    )


def _normalize_citations(value: object) -> list[dict[str, str | None]]:
    if not isinstance(value, list):
        return []
    normalized: list[dict[str, str | None]] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, dict) or not isinstance(item.get("url"), str):
            continue
        raw = item["url"]
        try:
            canonical = canonicalize_http_url(raw)
        except ValueError:
            continue
        if canonical in seen:
            continue
        seen.add(canonical)
        normalized.append(
            {
                "label": str(item.get("label") or canonical),
                "url": raw,
                "canonical_url": canonical,
                "excerpt": str(item["excerpt"]) if item.get("excerpt") else None,
            }
        )
    return normalized


def _preserve_unattached_citations(
    candidates: list[CandidateTopic],
    citations: list[dict[str, str | None]],
    *,
    report_sha: str,
    tlp: TLP,
    sensitivity: str,
    external_llm_allowed: bool,
    warnings: list[str],
) -> None:
    attached = {source.canonical_url for candidate in candidates for source in candidate.sources}
    missing = [item for item in citations if item.get("canonical_url") not in attached]
    if not missing:
        return
    sources = [
        SourceCandidate(
            id=uuid5(NAMESPACE_URL, f"{report_sha}:{item['canonical_url']}"),
            url=str(item["canonical_url"]),
            raw_url=str(item["url"]),
            title=str(item["label"]),
            publisher="unknown",
            role=SourceRole.UNKNOWN,
            citation=item.get("excerpt"),
            local_ref=f"C{index}",
            parsing_warnings=("Citation visible non rattachée à un bloc publication.",),
            tlp=tlp,
            sensitivity=sensitivity,
            external_llm_allowed=external_llm_allowed,
        )
        for index, item in enumerate(missing, 1)
    ]
    candidates.append(
        CandidateTopic(
            id=uuid5(NAMESPACE_URL, f"{report_sha}:unattached-citations"),
            local_ref="CONTEXT-CITATIONS",
            title="Citations visibles non regroupées",
            summary="URLs conservées depuis les citations visibles du bridge.",
            novelty="Contexte de découverte non sélectionnable isolément.",
            technical_potential=0,
            technical_potential_reason="Non évalué.",
            uncertainties=("Rattachement éditorial inconnu.",),
            relevance_reasons=("Préservation déterministe des URLs trouvées.",),
            actors=(),
            campaigns=(),
            malware=(),
            cves=(),
            victims=(),
            sectors=(),
            countries=(),
            likely_artifacts=("unknown",),
            sources=sources,
            tlp=tlp,
            sensitivity=sensitivity,
            external_llm_allowed=external_llm_allowed,
            parsing_warnings=("Citations non rattachées conservées comme contexte.",),
            context_only=True,
        )
    )
    warnings.append("Des citations visibles non rattachées ont été conservées comme contexte.")


def _is_contract_echo(report: str) -> bool:
    compact = re.sub(r"\s+", "", report)
    return (
        '"version":"research-batch-compact-v1"' in compact
        and '"minimal_example"' in compact
        and not re.search(r"^\s*#{2,3}\s+", report, re.MULTILINE)
    )


def _normalize_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", _normalize_text(value)).strip("_")


def _normalize_text(value: str) -> str:
    import unicodedata

    normalized = unicodedata.normalize("NFKD", value.casefold())
    return normalized.encode("ascii", "ignore").decode()


def _split_list(value: str, *, normalized: bool = False) -> list[str]:
    cleaned = value.strip().strip("[]")
    items = [item.strip() for item in re.split(r"[,;|]", cleaned) if item.strip()]
    return [item.casefold() for item in items] if normalized else items


def _bounded_int(value: str | None, minimum: int, maximum: int) -> int | None:
    parsed = _non_negative_int(value)
    return parsed if parsed is not None and minimum <= parsed <= maximum else None


def _non_negative_int(value: str | None) -> int | None:
    if value is None or value.strip().casefold() == "unknown":
        return None
    return int(value.strip()) if re.fullmatch(r"\d+", value.strip()) else None


def _explicit_date(value: str | None) -> date | None:
    if value is None or value.strip().casefold() == "unknown":
        return None
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value.strip()):
        return None
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        return None


def _first_explicit_date(value: str) -> date | None:
    match = re.search(r"(?<!\d)(\d{4}-\d{2}-\d{2})(?!\d)", value)
    return _explicit_date(match.group(1)) if match else None


def _enum_or_default(enum_type: Any, value: str | None, default: Any) -> Any:
    if value is None:
        return default
    try:
        return enum_type(value.strip().casefold())
    except ValueError:
        return default
