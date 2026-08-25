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
from enum import StrEnum
from typing import Any

from cti_app.application.discovery_report_parser import extract_http_urls
from cti_app.application.production_normalization import (
    canonical_indicator_key,
    display_indicator_value,
    normalize_indicator_value,
)
from cti_app.domain.discovery import SourceRole
from cti_app.domain.publication import ArtifactType

PARSER_VERSION = "production-markdown-v2"

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
    violations: list[SynthesisViolation] = field(default_factory=list)

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
    editorial_title: str | None = None

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


class SemanticType(StrEnum):
    ACTOR = "actor"
    CAMPAIGN = "campaign"
    MALWARE = "malware"
    TOOL = "tool"
    PRODUCT = "product"
    TECHNIQUE = "technique"
    PROTOCOL = "protocol"
    INFRASTRUCTURE = "infrastructure"
    FILE = "file"
    INDICATOR = "indicator"
    OTHER = "other"


class IndicatorStatus(StrEnum):
    CONFIRMED_IOC = "confirmed_ioc"
    CONTEXTUAL = "contextual"
    EXCLUDED = "excluded"


class IndicatorProvenance(StrEnum):
    SOURCE = "source"
    DERIVED = "derived"
    ANALYST = "analyst"


class DisplayPolicy(StrEnum):
    IOC_SECTION = "ioc_section"
    BODY_ONLY = "body_only"
    BOTH = "both"
    HIDDEN = "hidden"


@dataclass(frozen=True)
class ExtractionItem:
    local_id: str
    category: str
    value: str
    context: str
    artifact_type: ArtifactType | None
    attack_id: str | None
    reference_ids: tuple[str, ...]
    source_ids: tuple[str, ...]
    supported: bool
    semantic_type: SemanticType = SemanticType.OTHER
    indicator_status: IndicatorStatus = IndicatorStatus.CONTEXTUAL
    provenance: IndicatorProvenance = IndicatorProvenance.SOURCE
    display_policy: DisplayPolicy = DisplayPolicy.BODY_ONLY
    normalized_value: str | None = None


@dataclass(frozen=True)
class TechnicalExtraction:
    items: tuple[ExtractionItem, ...]
    uncertainties: tuple[str, ...] = ()

    def supported_items(self) -> tuple[ExtractionItem, ...]:
        return tuple(item for item in self.items if item.supported)


# --- Shared lexing ---------------------------------------------------------

_FENCE = re.compile(r"^\s*```[^\n]*\n(?P<body>.*?)\n\s*```\s*$", re.DOTALL)
# The bridge serialises ChatGPT's rendered DOM, where headings have already
# lost their `#` markers: `## SOURCE S1` reaches us as `SOURCE S1`. The hash
# is therefore optional, and a heading is told apart from prose by the fact
# that it carries no `key: value` pair and stays short.
_HEADING = re.compile(r"^\s{0,3}(?P<hashes>#{0,6})\s*(?P<text>\S.*?)\s*#*\s*$")
_MAX_HEADING_CHARS = 90

# Structure names the model may emit as a bare line once the bridge has
# stripped the `#` markers.
_HEADING_WORDS = frozenset(
    {
        "references",
        "reference",
        "uncertainties",
        "incertitudes",
        "source",
        "publication",
        "event",
        "evenement",
        "extraction",
        "extraction cti",
        "item",
        "element",
        "entree",
    }
    # Q2 category headings arrive bare as well.
    | set(_CATEGORY_ALIASES)
)
_FIELD = re.compile(r"^\s{0,3}(?P<key>[A-Za-zÀ-ÿ][A-Za-zÀ-ÿ0-9 _-]{0,40}?)\s*[:=]\s*(?P<value>.*)$")
_BULLET = re.compile(r"^\s*[-*•]\s+(?P<text>.+?)\s*$")
# URL extraction is shared with the discovery parser: a real ChatGPT answer
# writes `[https://x](https://x)`, which a naive regex turns into garbage.
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


def _heading_text(line: str) -> str | None:
    """The heading carried by a line, with or without `#` markers.

    Without a `#` the line has to name a structure we expect; anything else is
    prose, and a continuation line must not be mistaken for a new block.
    """
    match = _HEADING.match(line)
    if match is None:
        return None
    text = match.group("text")
    if match.group("hashes"):
        return text
    if _FIELD.match(line) or _BULLET.match(line) or len(text) > _MAX_HEADING_CHARS:
        return None
    folded = _fold(text)
    if any(folded == word or folded.startswith(f"{word} ") for word in _HEADING_WORDS):
        return text
    return None


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
        if current and stripped and _heading_text(line) is None:
            values[current] = f"{values[current]} {stripped}".strip()
    return values


# Values the model uses to say "I could not establish this".
_EXPLICIT_UNKNOWN = {"unknown", "inconnu", "inconnue", "n/a", "na", "none", "null", "-"}


def _is_explicit_unknown(value: str) -> bool:
    return value.strip().strip(".").lower() in _EXPLICIT_UNKNOWN


def _parse_date(value: str) -> date | None:
    candidate = value.strip()[:10]
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(candidate, fmt).date()
        except ValueError:
            continue
    return None


def _enum_value[T: StrEnum](
    raw: str | None, enum_type: type[T], default: T | None
) -> T | None:
    if not raw:
        return default
    try:
        return enum_type(_fold(raw).replace(" ", "_"))
    except ValueError:
        return default


def _split_blocks(text: str, block_keywords: dict[str, str]) -> tuple[list[_Block], list[str]]:
    """Split a document into blocks keyed by heading keyword.

    Returns the blocks plus the top-level section each belongs to.
    """
    blocks: list[_Block] = []
    section = "root"
    sections: list[str] = []
    current: _Block | None = None

    for line in text.split("\n"):
        heading = _heading_text(line)
        if heading is not None:
            folded = _fold(heading)
            keyword = _match_keyword(folded, block_keywords)
            if keyword is not None:
                local_id = None
                token = _LOCAL_ID.search(heading)
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
        heading = _heading_text(line)
        if heading is not None:
            capturing = _fold(heading).startswith(("uncertainties", "incertitudes"))
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
        field_value = values.get("url") or values.get("lien") or ""
        urls = extract_http_urls(field_value)
        if not urls:
            # The model often puts the link in prose instead of the field.
            urls = extract_http_urls(block.raw())
            if urls:
                result.warnings.append("source_url_recovered_from_text")
        if not urls:
            result.dropped_blocks.append(block.raw())
            result.warnings.append("source_without_url_dropped")
            continue
        raw_url, canonical = urls[0]

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
        if raw_date and not _is_explicit_unknown(raw_date):
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
        if raw_date and not _is_explicit_unknown(raw_date):
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
        editorial_title=_editorial_title(body),
    )
    return result


# --- Q2 --------------------------------------------------------------------

_Q2_ITEM_BLOCKS = {"item": "item", "element": "item", "entree": "item"}


def _editorial_title(body: str) -> str | None:
    for line in body.splitlines():
        match = _FIELD.match(line)
        if match and _normalize_key(match.group("key")) in {"editorial-title", "brief-title"}:
            return match.group("value").strip() or None
    return None


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

        artifact_raw = values.get("artifact-type") or values.get("artifacttype")
        if artifact_raw is None:
            # Read migration for persisted/model V1 payloads. Its presence does
            # not promote the item to an IOC: the cautious V2 defaults remain.
            artifact_raw = values.get("type")
        artifact_type = _enum_value(artifact_raw, ArtifactType, None)
        semantic_type = _enum_value(values.get("semantic-type"), SemanticType, SemanticType.OTHER)
        indicator_status = _enum_value(
            values.get("indicator-status"), IndicatorStatus, IndicatorStatus.CONTEXTUAL
        )
        provenance = _enum_value(
            values.get("provenance"), IndicatorProvenance, IndicatorProvenance.SOURCE
        )
        display_policy = _enum_value(
            values.get("display-policy"), DisplayPolicy, DisplayPolicy.BODY_ONLY
        )
        normalized_value = None
        if artifact_type is not None:
            try:
                normalized_value = normalize_indicator_value(value, artifact_type)
            except ValueError:
                result.warnings.append("invalid_artifact_value")

        items.append(
            ExtractionItem(
                local_id=local_id,
                category=category,
                value=value,
                context=(values.get("context") or values.get("contexte") or "").strip(),
                artifact_type=artifact_type,
                attack_id=(values.get("attack-id") or values.get("attackid") or "").strip() or None,
                reference_ids=refs,
                source_ids=srcs,
                supported=bool(refs or srcs),
                semantic_type=semantic_type or SemanticType.OTHER,
                indicator_status=indicator_status or IndicatorStatus.CONTEXTUAL,
                provenance=provenance or IndicatorProvenance.SOURCE,
                display_policy=display_policy or DisplayPolicy.BODY_ONLY,
                normalized_value=normalized_value,
            )
        )
        current = None

    for line in body.split("\n"):
        heading = _heading_text(line)
        if heading is not None:
            folded = _fold(heading)
            if _match_keyword(folded, _Q2_ITEM_BLOCKS) is not None:
                flush()
                token = re.search(r"\b([A-Za-z]{1,3})\s*[-_]?\s*(\d{1,3})\b", heading)
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


@dataclass(frozen=True)
class SynthesisViolation:
    code: str
    detail: str
    span: tuple[int, int] | None = None

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
    known_indicators: set[str] | TechnicalExtraction,
) -> ParseResult[str]:
    """Validate the model prose before publication and expose repairable violations."""
    result: ParseResult[str] = ParseResult()
    body = text.replace("\r\n", "\n").replace("\r", "\n").strip()

    def reject(code: str, detail: str, span: tuple[int, int] | None = None) -> None:
        violation = SynthesisViolation(code, detail, span)
        result.violations.append(violation)
        result.errors.append(code)

    if not body.strip():
        reject("empty_synthesis", "The synthesis is empty")
        return result

    known_sources = reference_report.source_ids()
    cited = {t for t in _reference_tokens(body) if t.startswith("S")}
    unknown = sorted(cited - known_sources)
    if unknown:
        reject("unknown_source_marker", ",".join(unknown))

    checks = (
        ("heading", re.compile(r"(?m)^\s{0,3}#{1,6}\s+.+"), "Markdown heading"),
        ("code_fence", re.compile(r"```"), "Code fence"),
        ("inline_code", re.compile(r"`"), "Inline code marker"),
        (
            "sources_corpus",
            re.compile(r"sources\s+du\s+corpus", re.IGNORECASE),
            "Sources du corpus line",
        ),
        (
            "bibliography",
            re.compile(r"(?m)^\s*\[\d+\]\s+(?:https?://|hxxps?://)"),
            "Final bibliography entry",
        ),
        ("raw_url", re.compile(r"\b(?:https?|hxxps?)://\S+", re.IGNORECASE), "Raw URL"),
        ("bold", re.compile(r"\*\*"), "Bold marker"),
    )
    for code, pattern, detail in checks:
        for match in pattern.finditer(body):
            reject(code, detail, match.span())

    if isinstance(known_indicators, TechnicalExtraction):
        extraction = known_indicators
        for item in extraction.items:
            if (
                item.indicator_status is not IndicatorStatus.CONFIRMED_IOC
                or item.display_policy is DisplayPolicy.BOTH
                or item.artifact_type is None
            ):
                continue
            artifact_type = item.artifact_type
            try:
                variants = {
                    canonical_indicator_key(item.value, artifact_type),
                    display_indicator_value(item.value, artifact_type, defanged=True),
                }
            except ValueError:
                continue
            for variant in sorted(variants, key=len, reverse=True):
                occurrence = re.search(re.escape(variant), body, re.IGNORECASE)
                if occurrence:
                    reject(
                        "ioc_repeated_in_body",
                        f"{artifact_type.value}:{variant}",
                        occurrence.span(),
                    )
                    break

        hash_count = sum(
            len(pattern.findall(body))
            for kind, pattern in _SYNTHESIS_IOC_PATTERNS.items()
            if kind in {"sha256", "sha1", "md5"}
        )
        if hash_count >= 3:
            reject("mass_hash_enumeration", f"{hash_count} hash values")
        # Compare only against extraction values: dotted prose is never guessed as a domain.
        repeated_network = 0
        for item in extraction.items:
            if item.artifact_type not in {ArtifactType.IP, ArtifactType.DOMAIN}:
                continue
            try:
                variants = {
                    canonical_indicator_key(item.value, item.artifact_type),
                    display_indicator_value(item.value, item.artifact_type, defanged=True),
                }
            except ValueError:
                continue
            if any(re.search(re.escape(value), body, re.IGNORECASE) for value in variants):
                repeated_network += 1
        if repeated_network >= 3:
            reject("mass_network_enumeration", f"{repeated_network} network values")
    else:
        normalized_known = {value.strip().lower() for value in known_indicators}
        for pattern in _SYNTHESIS_IOC_PATTERNS.values():
            for match in pattern.finditer(body):
                if match.group(0).strip().lower() not in normalized_known:
                    reject("unknown_indicator", match.group(0), match.span())

    if result.errors:
        return result
    result.value = body
    return result


# --- Serialisation ---------------------------------------------------------


def reference_report_to_json(report: ReferenceReport) -> dict[str, Any]:
    return {
        "parser_version": PARSER_VERSION,
        "schema_version": "2",
        "editorial_title": report.editorial_title,
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
        editorial_title=payload.get("editorial_title"),
    )


def technical_extraction_to_json(extraction: TechnicalExtraction) -> dict[str, Any]:
    return {
        "parser_version": PARSER_VERSION,
        "schema_version": "2",
        "items": [
            {
                "id": item.local_id,
                "category": item.category,
                "value": item.value,
                "context": item.context,
                "artifact_type": item.artifact_type.value if item.artifact_type else None,
                "semantic_type": item.semantic_type.value,
                "indicator_status": item.indicator_status.value,
                "provenance": item.provenance.value,
                "display_policy": item.display_policy.value,
                "normalized_value": item.normalized_value,
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
    def read_item(item: dict[str, Any]) -> ExtractionItem:
        artifact_type = _enum_value(
            item.get("artifact_type") or item.get("type"), ArtifactType, None
        )
        normalized = item.get("normalized_value")
        if normalized is None and artifact_type is not None:
            try:
                normalized = normalize_indicator_value(str(item["value"]), artifact_type)
            except ValueError:
                normalized = None
        return ExtractionItem(
            local_id=item["id"],
            category=item["category"],
            value=item["value"],
            context=item.get("context", ""),
            artifact_type=artifact_type,
            attack_id=item.get("attack_id"),
            reference_ids=tuple(item.get("reference_ids", [])),
            source_ids=tuple(item.get("source_ids", [])),
            supported=bool(item.get("supported", False)),
            semantic_type=_enum_value(
                item.get("semantic_type"), SemanticType, SemanticType.OTHER
            )
            or SemanticType.OTHER,
            indicator_status=_enum_value(
                item.get("indicator_status"), IndicatorStatus, IndicatorStatus.CONTEXTUAL
            )
            or IndicatorStatus.CONTEXTUAL,
            provenance=_enum_value(
                item.get("provenance"), IndicatorProvenance, IndicatorProvenance.SOURCE
            )
            or IndicatorProvenance.SOURCE,
            display_policy=_enum_value(
                item.get("display_policy"), DisplayPolicy, DisplayPolicy.BODY_ONLY
            )
            or DisplayPolicy.BODY_ONLY,
            normalized_value=normalized,
        )

    return TechnicalExtraction(
        items=tuple(read_item(item) for item in payload.get("items", [])),
        uncertainties=tuple(payload.get("uncertainties", [])),
    )
