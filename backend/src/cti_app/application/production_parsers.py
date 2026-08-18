"""Parsers for the semi-structured Markdown returned by the production model.

Strict JSON is a poor contract for a chat model: a single stray character makes
the whole answer unusable. These parsers accept a forgiving Markdown dialect and
degrade block by block — an unreadable block is dropped and reported, it never
sinks the rest of the answer.

Everything the model says is a *proposal*. Sources are deduplicated by canonical
URL, events must point at a known source, and extraction items lose any
reference the report does not define.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from cti_app.domain.discovery import SourceRole, canonicalize_http_url

PARSER_VERSION = "production-markdown-v1"

# Whitespace a chat model routinely emits and that would break field parsing.
_NBSP = "\u00a0"
_NARROW_NBSP = "\u202f"
_BOM = "\ufeff"


@dataclass
class ParseResult[T]:
    """Outcome of a tolerant parse.

    `warnings` are recoveries the parser made, `errors` are what made the answer
    unusable, `dropped_blocks` are the raw blocks that could not be read.
    """

    value: T | None = None
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    repair_actions: list[str] = field(default_factory=list)
    dropped_blocks: list[str] = field(default_factory=list)

    @property
    def usable(self) -> bool:
        return self.value is not None and not self.errors


# --- Reference report (Q1) -------------------------------------------------


@dataclass(frozen=True)
class ParsedSource:
    local_id: str
    title: str
    url: str
    canonical_url: str
    publisher: str | None
    published_at: date | None
    role: SourceRole


@dataclass(frozen=True)
class ParsedEvent:
    local_id: str
    event_date: date | None
    source_ids: tuple[str, ...]
    text: str


@dataclass(frozen=True)
class ReferenceReport:
    sources: tuple[ParsedSource, ...]
    events: tuple[ParsedEvent, ...]
    uncertainties: tuple[str, ...] = ()

    def source_ids(self) -> set[str]:
        return {source.local_id for source in self.sources}


# --- Technical extraction (Q2) ---------------------------------------------

EXTRACTION_CATEGORIES = (
    "actors",
    "campaigns",
    "victimology",
    "infection_chain",
    "malware",
    "tools",
    "ttps",
    "cves",
    "protocols",
    "network_artifacts",
    "infrastructure",
    "files",
    "commands",
    "persistence",
    "detections",
    "other_technical",
)

# Heading aliases, French and English, singular and plural.
_CATEGORY_ALIASES: dict[str, str] = {
    "actors": "actors",
    "actor": "actors",
    "acteurs": "actors",
    "acteur": "actors",
    "campaigns": "campaigns",
    "campaign": "campaigns",
    "campagnes": "campaigns",
    "campagne": "campaigns",
    "victimology": "victimology",
    "victimologie": "victimology",
    "victimes": "victimology",
    "infection chain": "infection_chain",
    "infection_chain": "infection_chain",
    "chaine d infection": "infection_chain",
    "chaine dinfection": "infection_chain",
    "chaine d'infection": "infection_chain",
    "malware": "malware",
    "malwares": "malware",
    "maliciels": "malware",
    "tools": "tools",
    "tool": "tools",
    "outils": "tools",
    "outil": "tools",
    "ttp": "ttps",
    "ttps": "ttps",
    "techniques": "ttps",
    "cve": "cves",
    "cves": "cves",
    "protocols": "protocols",
    "protocol": "protocols",
    "protocoles": "protocols",
    "protocole": "protocols",
    "network artifacts": "network_artifacts",
    "network_artifacts": "network_artifacts",
    "artefacts reseau": "network_artifacts",
    "artefacts réseau": "network_artifacts",
    "infrastructure": "infrastructure",
    "infrastructures": "infrastructure",
    "files": "files",
    "file": "files",
    "fichiers": "files",
    "fichier": "files",
    "commands": "commands",
    "command": "commands",
    "commandes": "commands",
    "commande": "commands",
    "persistence": "persistence",
    "persistance": "persistence",
    "detections": "detections",
    "detection": "detections",
    "detections et regles": "detections",
    "other technical": "other_technical",
    "other_technical": "other_technical",
    "autres elements techniques": "other_technical",
    "autres": "other_technical",
}


@dataclass(frozen=True)
class ExtractionItem:
    local_id: str
    category: str
    value: str
    context: str
    artifact_type: str | None
    attack_id: str | None
    reference_ids: tuple[str, ...]
    source_ids: tuple[str, ...]
    supported: bool


@dataclass(frozen=True)
class TechnicalExtraction:
    items: tuple[ExtractionItem, ...]
    uncertainties: tuple[str, ...] = ()

    def supported_items(self) -> tuple[ExtractionItem, ...]:
        return tuple(item for item in self.items if item.supported)


# --- Shared lexing ---------------------------------------------------------

_FENCE = re.compile(r"^\s*```[^\n]*\n(?P<body>.*?)\n\s*```\s*$", re.DOTALL)
_HEADING = re.compile(r"^\s{0,3}(#{1,6})\s*(?P<text>.+?)\s*#*\s*$")
_FIELD = re.compile(r"^\s{0,3}(?P<key>[A-Za-zÀ-ÿ][A-Za-zÀ-ÿ0-9 _-]{0,40}?)\s*[:=]\s*(?P<value>.*)$")
_BULLET = re.compile(r"^\s*[-*•]\s+(?P<text>.+?)\s*$")
_URL = re.compile(r"https?://[^\s<>\"'\])}]+")
_LOCAL_ID = re.compile(r"\b([SR])\s*[-_]?\s*(\d{1,3})\b", re.IGNORECASE)
_ID_TOKEN = re.compile(r"[SR]\d{1,3}", re.IGNORECASE)


def normalize_text(raw: str) -> str:
    """Strip an outer code fence and normalise whitespace oddities."""
    text = raw.replace("\r\n", "\n").replace("\r", "\n")
    for exotic in (_NBSP, _NARROW_NBSP):
        text = text.replace(exotic, " ")
    text = text.replace(_BOM, "")
    fenced = _FENCE.match(text.strip())
    if fenced:
        text = fenced.group("body")
    return text.strip()


def _normalize_key(key: str) -> str:
    return re.sub(r"[\s_]+", "-", key.strip().lower())


def _fold(text: str) -> str:
    """Lowercase and flatten accents/punctuation for heading comparison."""
    lowered = text.strip().lower()
    for accented, plain in (
        ("é", "e"),
        ("è", "e"),
        ("ê", "e"),
        ("ë", "e"),
        ("à", "a"),
        ("â", "a"),
        ("ä", "a"),
        ("î", "i"),
        ("ï", "i"),
        ("ô", "o"),
        ("ö", "o"),
        ("ù", "u"),
        ("û", "u"),
        ("ü", "u"),
        ("ç", "c"),
    ):
        lowered = lowered.replace(accented, plain)
    return re.sub(r"\s+", " ", lowered).strip(" :#")


@dataclass
class _Block:
    kind: str
    local_id: str | None
    lines: list[str] = field(default_factory=list)

    def raw(self) -> str:
        return "\n".join(self.lines).strip()


def _fields(lines: list[str]) -> dict[str, str]:
    """Read `key: value` pairs, letting a value continue on following lines."""
    values: dict[str, str] = {}
    current: str | None = None
    for line in lines:
        match = _FIELD.match(line)
        if match:
            current = _normalize_key(match.group("key"))
            values[current] = match.group("value").strip()
            continue
        stripped = line.strip()
        if current and stripped and not _HEADING.match(line):
            values[current] = f"{values[current]} {stripped}".strip()
    return values


def _parse_date(value: str) -> date | None:
    candidate = value.strip()[:10]
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(candidate, fmt).date()
        except ValueError:
            continue
    return None


def _split_blocks(text: str, block_keywords: dict[str, str]) -> tuple[list[_Block], list[str]]:
    """Split a document into blocks keyed by heading keyword.

    Returns the blocks plus the top-level section each belongs to.
    """
    blocks: list[_Block] = []
    section = "root"
    sections: list[str] = []
    current: _Block | None = None

    for line in text.split("\n"):
        heading = _HEADING.match(line)
        if heading:
            folded = _fold(heading.group("text"))
            keyword = _match_keyword(folded, block_keywords)
            if keyword is not None:
                local_id = None
                token = _LOCAL_ID.search(heading.group("text"))
                if token:
                    local_id = f"{token.group(1).upper()}{int(token.group(2))}"
                current = _Block(kind=keyword, local_id=local_id)
                blocks.append(current)
                sections.append(section)
                continue
            # A heading we do not recognise starts a new top-level section.
            section = folded
            current = None
            continue
        if current is not None:
            current.lines.append(line)
    return blocks, sections


def _match_keyword(folded: str, keywords: dict[str, str]) -> str | None:
    for prefix, kind in keywords.items():
        if folded == prefix or folded.startswith(f"{prefix} "):
            return kind
    return None


def _collect_uncertainties(text: str) -> tuple[str, ...]:
    out: list[str] = []
    capturing = False
    for line in text.split("\n"):
        heading = _HEADING.match(line)
        if heading:
            capturing = _fold(heading.group("text")).startswith(("uncertainties", "incertitudes"))
            continue
        if capturing:
            bullet = _BULLET.match(line)
            if bullet:
                out.append(bullet.group("text"))
    return tuple(out)


def _reference_tokens(value: str) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            f"{m.group(0)[0].upper()}{int(m.group(0)[1:])}" for m in _ID_TOKEN.finditer(value)
        )
    )


# --- Q1 --------------------------------------------------------------------

_Q1_BLOCKS = {
    "source": "source",
    "publication": "source",
    "event": "event",
    "evenement": "event",
    "reference": "event",
}


def parse_reference_report(text: str, research_date: date) -> ParseResult[ReferenceReport]:
    """Parse the Q1 reference report.

    Sources are deduplicated by canonical URL and events are remapped onto the
    surviving ids. An event survives if it still cites at least one known
    source; a date after `research_date` is rejected as impossible.
    """
    result: ParseResult[ReferenceReport] = ParseResult()
    body = normalize_text(text)
    if not body:
        result.errors.append("empty_response")
        return result

    blocks, _ = _split_blocks(body, _Q1_BLOCKS)
    if not blocks:
        result.errors.append("no_source_or_event_block")
        return result

    sources: list[ParsedSource] = []
    canonical_to_id: dict[str, str] = {}
    alias: dict[str, str] = {}
    auto_source = 0

    for block in (b for b in blocks if b.kind == "source"):
        values = _fields(block.lines)
        raw_url = values.get("url") or values.get("lien") or ""
        if not raw_url:
            found = _URL.search(block.raw())
            if found:
                raw_url = found.group(0)
                result.warnings.append("source_url_recovered_from_text")
        if not raw_url:
            result.dropped_blocks.append(block.raw())
            result.warnings.append("source_without_url_dropped")
            continue
        try:
            canonical = canonicalize_http_url(raw_url)
        except ValueError:
            result.dropped_blocks.append(block.raw())
            result.warnings.append("source_with_invalid_url_dropped")
            continue

        auto_source += 1
        local_id = block.local_id
        if local_id is None:
            local_id = f"S{auto_source}"
            result.warnings.append("source_id_generated")

        if canonical in canonical_to_id:
            # Same publication announced twice: keep one, remap the other.
            alias[local_id] = canonical_to_id[canonical]
            result.warnings.append("duplicate_source_merged")
            continue

        published_at = None
        raw_date = values.get("published-at") or values.get("date") or ""
        if raw_date:
            published_at = _parse_date(raw_date)
            if published_at is None:
                result.warnings.append("source_date_unreadable")

        role_value = _fold(values.get("role", "") or "")
        try:
            role = SourceRole(role_value) if role_value else SourceRole.UNKNOWN
        except ValueError:
            role = SourceRole.UNKNOWN
            result.warnings.append("source_role_unknown")

        canonical_to_id[canonical] = local_id
        alias[local_id] = local_id
        sources.append(
            ParsedSource(
                local_id=local_id,
                title=(values.get("title") or values.get("titre") or "").strip(),
                url=raw_url.strip(),
                canonical_url=canonical,
                publisher=(values.get("publisher") or values.get("editeur") or "").strip() or None,
                published_at=published_at,
                role=role,
            )
        )

    known_ids = {source.local_id for source in sources}
    events: list[ParsedEvent] = []
    auto_event = 0

    for block in (b for b in blocks if b.kind == "event"):
        values = _fields(block.lines)
        auto_event += 1
        local_id = block.local_id
        if local_id is None:
            local_id = f"R{auto_event}"
            result.warnings.append("event_id_generated")

        event_text = (values.get("text") or values.get("texte") or "").strip()
        if not event_text:
            result.dropped_blocks.append(block.raw())
            result.warnings.append("event_without_text_dropped")
            continue

        raw_sources = values.get("sources") or values.get("source") or ""
        cited = _reference_tokens(raw_sources)
        resolved = tuple(
            dict.fromkeys(
                alias[token] for token in cited if token in alias and alias[token] in known_ids
            )
        )
        if not resolved:
            result.dropped_blocks.append(block.raw())
            result.warnings.append("event_without_known_source_dropped")
            continue
        if len(resolved) < len(cited):
            result.warnings.append("event_unknown_source_removed")

        event_date = None
        raw_date = values.get("date") or ""
        if raw_date:
            event_date = _parse_date(raw_date)
            if event_date is None:
                result.warnings.append("event_date_unreadable")
            elif event_date > research_date:
                result.dropped_blocks.append(block.raw())
                result.warnings.append("event_with_future_date_dropped")
                continue

        events.append(
            ParsedEvent(
                local_id=local_id,
                event_date=event_date,
                source_ids=resolved,
                text=event_text,
            )
        )

    if not sources:
        result.errors.append("no_usable_source")
    if not events:
        result.errors.append("no_usable_event")
    if result.errors:
        return result

    result.value = ReferenceReport(
        sources=tuple(sources),
        events=tuple(events),
        uncertainties=_collect_uncertainties(body),
    )
    return result


# --- Q2 --------------------------------------------------------------------

_Q2_ITEM_BLOCKS = {"item": "item", "element": "item", "entree": "item"}


def parse_technical_extraction(
    text: str, reference_report: ReferenceReport
) -> ParseResult[TechnicalExtraction]:
    """Parse the Q2 CTI extraction.

    Any reference or source the report does not define is stripped. An item that
    ends up with no evidence at all is kept but marked unsupported, so it can be
    shown for review without ever reaching the brief.
    """
    result: ParseResult[TechnicalExtraction] = ParseResult()
    body = normalize_text(text)
    if not body:
        result.errors.append("empty_response")
        return result

    known_sources = reference_report.source_ids()
    known_events = {event.local_id for event in reference_report.events}

    items: list[ExtractionItem] = []
    category: str | None = None
    current: _Block | None = None
    auto_item = 0

    def flush() -> None:
        nonlocal current, auto_item
        if current is None or category is None:
            current = None
            return
        values = _fields(current.lines)
        value = (values.get("value") or values.get("valeur") or "").strip()
        if not value:
            result.dropped_blocks.append(current.raw())
            result.warnings.append("item_without_value_dropped")
            current = None
            return

        auto_item += 1
        local_id = current.local_id or f"I{auto_item}"
        if current.local_id is None:
            result.warnings.append("item_id_generated")

        raw_refs = values.get("references") or values.get("refs") or values.get("reference") or ""
        raw_srcs = values.get("sources") or values.get("source") or ""
        refs = tuple(t for t in _reference_tokens(raw_refs) if t in known_events)
        srcs = tuple(t for t in _reference_tokens(raw_srcs) if t in known_sources)
        if len(refs) < len(_reference_tokens(raw_refs)) or len(srcs) < len(
            _reference_tokens(raw_srcs)
        ):
            result.warnings.append("item_unknown_reference_removed")

        items.append(
            ExtractionItem(
                local_id=local_id,
                category=category,
                value=value,
                context=(values.get("context") or values.get("contexte") or "").strip(),
                artifact_type=(values.get("type") or "").strip() or None,
                attack_id=(values.get("attack-id") or values.get("attackid") or "").strip() or None,
                reference_ids=refs,
                source_ids=srcs,
                supported=bool(refs or srcs),
            )
        )
        current = None

    for line in body.split("\n"):
        heading = _HEADING.match(line)
        if heading:
            folded = _fold(heading.group("text"))
            if _match_keyword(folded, _Q2_ITEM_BLOCKS) is not None:
                flush()
                token = re.search(r"\b([A-Za-z]{1,3})\s*[-_]?\s*(\d{1,3})\b", heading.group("text"))
                local_id = f"{token.group(1).upper()}{int(token.group(2))}" if token else None
                current = _Block(kind="item", local_id=local_id)
                continue
            flush()
            mapped = _CATEGORY_ALIASES.get(folded)
            if mapped is not None:
                category = mapped
            elif folded.startswith(("uncertainties", "incertitudes")):
                category = None
            elif folded.startswith("extraction"):
                category = None
            else:
                category = None
                result.warnings.append("unknown_extraction_section")
            continue
        if current is not None:
            current.lines.append(line)
    flush()

    if not items:
        result.errors.append("no_usable_item")
        return result

    result.value = TechnicalExtraction(
        items=tuple(items),
        uncertainties=_collect_uncertainties(body),
    )
    if not result.value.supported_items():
        result.warnings.append("no_supported_item")
    return result


# --- Q3 --------------------------------------------------------------------

# Only kinds with a low false-positive rate in French prose. Bare IPv4 is
# deliberately excluded: version numbers such as "4.2.1.3" match it.
_SYNTHESIS_IOC_PATTERNS = {
    "sha256": re.compile(r"\b[a-fA-F0-9]{64}\b"),
    "sha1": re.compile(r"\b[a-fA-F0-9]{40}\b"),
    "md5": re.compile(r"\b[a-fA-F0-9]{32}\b"),
    "cve": re.compile(r"\bCVE-\d{4}-\d{4,7}\b", re.IGNORECASE),
}


def validate_synthesis(
    text: str,
    reference_report: ReferenceReport,
    known_indicators: set[str],
) -> ParseResult[str]:
    """Check the Q3 synthesis against the corpus it was allowed to use.

    The synthesis must not introduce a source, a URL or an indicator that the
    references and extraction did not already establish.
    """
    result: ParseResult[str] = ParseResult()
    body = normalize_text(text)
    if not body.strip():
        result.errors.append("empty_synthesis")
        return result

    known_sources = reference_report.source_ids()
    cited = {t for t in _reference_tokens(body) if t.startswith("S")}
    unknown = sorted(cited - known_sources)
    if unknown:
        result.errors.append(f"unknown_source_marker:{','.join(unknown)}")

    corpus_urls = {source.canonical_url for source in reference_report.sources}
    for match in _URL.finditer(body):
        try:
            canonical = canonicalize_http_url(match.group(0))
        except ValueError:
            result.errors.append("invalid_url_in_synthesis")
            continue
        if canonical not in corpus_urls:
            result.errors.append(f"url_outside_corpus:{canonical}")

    normalized_known = {value.strip().lower() for value in known_indicators}
    for pattern in _SYNTHESIS_IOC_PATTERNS.values():
        for match in pattern.finditer(body):
            if match.group(0).strip().lower() not in normalized_known:
                result.errors.append(f"unknown_indicator:{match.group(0)}")

    if result.errors:
        return result
    result.value = body
    return result


# --- Serialisation ---------------------------------------------------------


def reference_report_to_json(report: ReferenceReport) -> dict[str, Any]:
    return {
        "parser_version": PARSER_VERSION,
        "sources": [
            {
                "id": source.local_id,
                "title": source.title,
                "url": source.url,
                "canonical_url": source.canonical_url,
                "publisher": source.publisher,
                "published_at": source.published_at.isoformat() if source.published_at else None,
                "role": source.role.value,
            }
            for source in report.sources
        ],
        "events": [
            {
                "id": event.local_id,
                "date": event.event_date.isoformat() if event.event_date else None,
                "source_ids": list(event.source_ids),
                "text": event.text,
            }
            for event in report.events
        ],
        "uncertainties": list(report.uncertainties),
    }


def reference_report_from_json(payload: dict[str, Any]) -> ReferenceReport:
    return ReferenceReport(
        sources=tuple(
            ParsedSource(
                local_id=item["id"],
                title=item.get("title", ""),
                url=item["url"],
                canonical_url=item["canonical_url"],
                publisher=item.get("publisher"),
                published_at=(
                    date.fromisoformat(item["published_at"]) if item.get("published_at") else None
                ),
                role=SourceRole(item.get("role", SourceRole.UNKNOWN.value)),
            )
            for item in payload.get("sources", [])
        ),
        events=tuple(
            ParsedEvent(
                local_id=item["id"],
                event_date=date.fromisoformat(item["date"]) if item.get("date") else None,
                source_ids=tuple(item.get("source_ids", [])),
                text=item.get("text", ""),
            )
            for item in payload.get("events", [])
        ),
        uncertainties=tuple(payload.get("uncertainties", [])),
    )


def technical_extraction_to_json(extraction: TechnicalExtraction) -> dict[str, Any]:
    return {
        "parser_version": PARSER_VERSION,
        "items": [
            {
                "id": item.local_id,
                "category": item.category,
                "value": item.value,
                "context": item.context,
                "type": item.artifact_type,
                "attack_id": item.attack_id,
                "reference_ids": list(item.reference_ids),
                "source_ids": list(item.source_ids),
                "supported": item.supported,
            }
            for item in extraction.items
        ],
        "uncertainties": list(extraction.uncertainties),
    }


def technical_extraction_from_json(payload: dict[str, Any]) -> TechnicalExtraction:
    return TechnicalExtraction(
        items=tuple(
            ExtractionItem(
                local_id=item["id"],
                category=item["category"],
                value=item["value"],
                context=item.get("context", ""),
                artifact_type=item.get("type"),
                attack_id=item.get("attack_id"),
                reference_ids=tuple(item.get("reference_ids", [])),
                source_ids=tuple(item.get("source_ids", [])),
                supported=bool(item.get("supported", False)),
            )
            for item in payload.get("items", [])
        ),
        uncertainties=tuple(payload.get("uncertainties", [])),
    )
