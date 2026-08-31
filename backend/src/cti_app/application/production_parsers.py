"""Q1 Markdown-tolerant parsers and Q2 structured Pydantic schemas.

Strict JSON is a poor contract for a chat model: a single stray character makes
the whole answer unusable. These parsers accept a forgiving Markdown dialect and
degrade block by block — an unreadable block is dropped and reported, it never
sinks the rest of the answer.

Everything the model says is a *proposal*. Sources are deduplicated by canonical
URL, events must point at a known source, and extraction items lose any
reference the report does not define.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from cti_app.application.discovery_report_parser import extract_http_urls
from cti_app.application.production_normalization import (
    canonical_indicator_key,
    display_indicator_value,
    normalize_indicator_value,
)
from cti_app.domain.discovery import SourceRole
from cti_app.domain.production import DetectionRule, DetectionRuleType, ExtractionProfile
from cti_app.domain.publication import ArtifactType

PARSER_VERSION = "production-markdown-v3"

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
    uncertainties: list[str] = field(default_factory=list)

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
    NOT_APPLICABLE = "not_applicable"


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
    evidence_quote: str = ""
    model_run_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class TechnicalExtraction:
    items: tuple[ExtractionItem, ...]
    uncertainties: tuple[str, ...] = ()
    rules: tuple[DetectionRule, ...] = ()

    def supported_items(self) -> tuple[ExtractionItem, ...]:
        return tuple(item for item in self.items if item.supported)


class Q2FactProposal(BaseModel):
    """One source-supported CTI fact proposed by Q2.

    IDs and provenance are assigned by orchestration, never accepted from model
    output.  Source text remains untrusted data and is only used as evidence.
    """

    model_config = ConfigDict(extra="forbid")

    category: Literal[
        "actors",
        "campaigns",
        "malware",
        "tools",
        "infection_chain",
        "ttps",
        "victimology",
        "protocols",
        "infrastructure",
        "files",
        "commands",
        "persistence",
        "detections",
        "other_technical",
    ]
    value: str = Field(min_length=1, max_length=4000)
    attack_id: str | None = Field(default=None, pattern=r"^T\d{4}(?:\.\d{3})?$")
    context: str = Field(default="", max_length=4000)
    evidence_quote: str = Field(default="", max_length=8000)


class Q2ArtifactProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: str = Field(min_length=1, max_length=4000)
    artifact_type: Literal[
        "domain",
        "ip",
        "url",
        "email",
        "hash",
        "filename",
        "filepath",
        "cve",
    ]
    indicator_status: Literal["confirmed_ioc", "contextual", "excluded", "not_applicable"]
    context: str = Field(default="", max_length=4000)
    evidence_quote: str = Field(default="", max_length=8000)


class Q2RuleProposal(BaseModel):
    """One complete, literal detection rule proposed by Q2.

    Rule bodies are deliberately separate from IOC/artifact values. The model
    supplies no internal provenance identifiers; orchestration attaches those
    after deterministic verification.
    """

    model_config = ConfigDict(extra="forbid")

    rule_type: DetectionRuleType
    name: str | None = Field(default=None, max_length=4000)
    body: str = Field(min_length=1, max_length=131072)
    context: str = Field(default="", max_length=4000)
    evidence_quote: str = Field(default="", max_length=8000)


class Q2SourceOutput(BaseModel):
    """Strict Q2 contract; deliberately contains no internal identifiers."""

    model_config = ConfigDict(extra="forbid")

    facts: list[Q2FactProposal] = Field(default_factory=list)
    artifacts: list[Q2ArtifactProposal] = Field(default_factory=list)
    rules: list[Q2RuleProposal] = Field(default_factory=list)
    uncertainties: list[str] = Field(default_factory=list)


# Bump whenever Q2SourceOutput contract changes. Checkpoints validate against it.
Q2_SCHEMA_VERSION = "3"
Q2_EXTRACTION_CONTRACT_VERSION = "q2-source-extraction-v3"

# Bump whenever the Q2 Markdown dialect or its lexing rules change. Participates
# in the Q2 checkpoint identity so a parser change forces a fresh model call.
Q2_MARKDOWN_PARSER_VERSION = "q2-markdown-v3"


def q2_source_output_to_json(output: Q2SourceOutput) -> dict[str, Any]:
    """Serialize only source-centric Q2 proposals for the global cache."""
    return {
        "contract_version": Q2_EXTRACTION_CONTRACT_VERSION,
        "schema_version": Q2_SCHEMA_VERSION,
        "facts": [fact.model_dump(mode="json") for fact in output.facts],
        "artifacts": [artifact.model_dump(mode="json") for artifact in output.artifacts],
        "rules": [rule.model_dump(mode="json") for rule in output.rules],
        "uncertainties": list(output.uncertainties),
    }


def q2_source_output_from_json(payload: dict[str, Any]) -> Q2SourceOutput:
    """Load a source-centric checkpoint without accepting internal IDs."""
    if payload.get("contract_version") not in {None, Q2_EXTRACTION_CONTRACT_VERSION}:
        raise ValueError("Q2 source extraction contract is incompatible")
    if payload.get("schema_version") not in {None, Q2_SCHEMA_VERSION}:
        raise ValueError("Q2 source extraction schema is incompatible")
    return Q2SourceOutput.model_validate(
        {
            "facts": payload.get("facts", []),
            "artifacts": payload.get("artifacts", []),
            "rules": payload.get("rules", []),
            "uncertainties": payload.get("uncertainties", []),
        }
    )


def project_q2_source_output(output: Q2SourceOutput, profile: ExtractionProfile) -> Q2SourceOutput:
    """Project a FULL result for IOC_RULES consumers without reprompting."""
    if profile is ExtractionProfile.FULL:
        return output
    if profile is not ExtractionProfile.IOC_RULES:
        raise ValueError(f"Unsupported extraction profile: {profile}")
    return Q2SourceOutput(
        # Artifact proposals are the exhaustive IOC/rule channel. File and
        # detection facts are retained when a FULL result used a fact rather
        # than an ARTIFACT block for that narrow light-profile scope.
        facts=[fact for fact in output.facts if fact.category in {"files", "detections"}],
        artifacts=list(output.artifacts),
        rules=list(output.rules),
        uncertainties=list(output.uncertainties),
    )


# --- Shared lexing ---------------------------------------------------------

_FENCE = re.compile(r"^\s*```[^\n]*\n(?P<body>.*?)\n\s*```\s*$", re.DOTALL)
_FENCE_OPEN = re.compile(r"^\s*```(?P<language>[A-Za-z0-9_-]*)\s*$")
_FENCE_CLOSE = re.compile(r"^\s*```\s*$")
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
        "fact",
        "artifact",
        "artefact",
        "rule",
    }
)
_FIELD = re.compile(r"^\s{0,3}(?P<key>[A-Za-zÀ-ÿ][A-Za-zÀ-ÿ0-9 _-]{0,40}?)\s*[:=]\s*(?P<value>.*)$")
_BULLET = re.compile(r"^\s*[-*•]\s+(?P<text>.+?)\s*$")
# URL extraction is shared with the discovery parser: a real ChatGPT answer
# writes `[https://x](https://x)`, which a naive regex turns into garbage.
_LOCAL_ID = re.compile(r"\b([SR])\s*[-_]?\s*(\d{1,3})\b", re.IGNORECASE)
_ID_TOKEN = re.compile(r"[SR]\d{1,3}", re.IGNORECASE)


def normalize_text(raw: str) -> str:
    """Strip an outer code fence and normalise whitespace oddities."""
    # Markdown escaping may survive the bridge; unescape punctuation only, so
    # Windows paths (``C:\\Windows``) stay untouched.
    text = raw.replace("\r\n", "\n").replace("\r", "\n").replace("\\_", "_")
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


def _enum_value[T: StrEnum](raw: str | None, enum_type: type[T], default: T | None) -> T | None:
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
    in_fence = False

    for line in text.split("\n"):
        if in_fence:
            if current is not None:
                current.lines.append(line)
            if _FENCE_CLOSE.fullmatch(line):
                in_fence = False
            continue
        if _FENCE_OPEN.fullmatch(line):
            if current is not None:
                current.lines.append(line)
            in_fence = True
            continue
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
        _raw_url, canonical = urls[0]

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
                # Subject Production never exposes the model's tracking URL.
                # The canonical URL is also the user-visible URL and the URL
                # handed to Q2.
                url=canonical,
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


def _editorial_title(body: str) -> str | None:
    for line in body.splitlines():
        match = _FIELD.match(line)
        if match and _normalize_key(match.group("key")) == "editorial-title":
            return match.group("value").strip() or None
    return None


# --- Q2 compact grouped Markdown -------------------------------------------
#
# Q2 is a small, source-bound wire format. A response is already associated
# with one source by the orchestrator, so source ids, URLs, evidence,
# provenance and per-value status are intentionally absent from the model
# output. This parser expands grouped lists into the existing proposal
# objects; the verifier assigns canonical provenance and normalization.

_Q2_FACT_CATEGORIES = frozenset(
    {
        "actors",
        "campaigns",
        "malware",
        "tools",
        "infection_chain",
        "ttps",
        "victimology",
        "protocols",
        "infrastructure",
        "files",
        "commands",
        "persistence",
        "detections",
        "other_technical",
    }
)

# The bridge cannot be trusted to keep a single "hash" word; the prompt asks
# for the concrete algorithm instead. All of it normalizes to the internal
# ArtifactType the rest of the pipeline already understands.
_Q2_ARTIFACT_TYPE_ALIASES = {
    "domain": "domain",
    "ip": "ip",
    "ip_address": "ip",
    "url": "url",
    "email": "email",
    "hash": "hash",
    "md5": "hash",
    "sha1": "hash",
    "sha256": "hash",
    "sha512": "hash",
    "filename": "filename",
    "file_name": "filename",
    "filepath": "filepath",
    "file_path": "filepath",
    "cve": "cve",
}

_Q2_RULE_TYPE_ALIASES = {
    "yara": DetectionRuleType.YARA,
    "sigma": DetectionRuleType.SIGMA,
    "suricata": DetectionRuleType.SURICATA,
    "snort": DetectionRuleType.SNORT,
}

MAX_RULES_PER_SOURCE = 100
MAX_SINGLE_RULE_BODY_BYTES = 128 * 1024
MAX_TOTAL_RULE_CONTENT_BYTES_PER_SOURCE = 2 * 1024 * 1024

_ATTACK_ID = re.compile(r"T\d{4}(?:\.\d{3})?")

_Q2_TOP_SECTIONS = frozenset({"facts", "iocs", "rules", "uncertainties", "incertitudes"})
_Q2_IOC_STATUSES = {"confirmed": "confirmed_ioc", "contextual": "contextual"}
_Q2_COMPACT_TYPE_ALIASES = frozenset(_Q2_ARTIFACT_TYPE_ALIASES)
_Q2_RULE_HEADING = re.compile(
    r"^(?P<type>yara|sigma|suricata|snort)(?:\s*:\s*(?P<name>.*))?$", re.IGNORECASE
)
_Q2_TYPE_LINE = re.compile(r"^\s*(?P<type>[A-Za-z][A-Za-z0-9 _-]*)\s*:\s*$")


def _rule_issue(result: ParseResult[Q2SourceOutput], code: str) -> None:
    result.warnings.append(code)
    result.uncertainties.append(code)


def _normalize_token(raw: str) -> str:
    """Fold a field value onto the underscored vocabulary the schema expects."""
    return _normalize_key(raw).replace("-", "_")


def _normalize_q2_input(raw: str) -> str:
    """Normalize transport whitespace without changing visible rule literals."""
    text = raw.replace("\r\n", "\n").replace("\r", "\n")
    for exotic in (_NBSP, _NARROW_NBSP):
        text = text.replace(exotic, " ")
    text = text.replace(_BOM, "").strip()
    fenced = _FENCE.match(text)
    return fenced.group("body").strip() if fenced else text


def _q2_compact_heading(line: str) -> tuple[int, str] | None:
    """Read a compact heading, tolerating the bridge's lost hash markers."""
    match = re.match(r"^\s{0,3}(?P<hashes>#{1,6})\s+(?P<text>.+?)\s*#*\s*$", line)
    if match:
        return len(match.group("hashes")), match.group("text").strip()

    candidate = line.strip().rstrip("#").strip()
    folded = _fold(candidate)
    token = _normalize_token(candidate)
    if folded in _Q2_TOP_SECTIONS or token in _Q2_FACT_CATEGORIES:
        return 1, candidate
    if folded in _Q2_IOC_STATUSES or token in _Q2_COMPACT_TYPE_ALIASES:
        return 2, candidate
    if _Q2_RULE_HEADING.fullmatch(candidate):
        return 2, candidate
    return None


def _q2_value_and_context(raw: str) -> tuple[str, str]:
    """Split an optional annotation, leaving IPv6 ``::`` literals intact."""
    parts = re.split(r"\s+::\s+", raw.strip(), maxsplit=1)
    value = parts[0].strip()
    context = parts[1].strip() if len(parts) == 2 else ""
    return value, context


def parse_q2_proposals_markdown(text: str) -> ParseResult[Q2SourceOutput]:
    """Parse one source-bound Q2 compact grouped response.

    The old per-item field dialect is intentionally not recognized. A compact
    response may contain facts, IOC groups, rules, uncertainties, or any
    combination of those sections; omitted values are simply absent proposals.
    """
    result: ParseResult[Q2SourceOutput] = ParseResult()
    body = _normalize_q2_input(text)
    if not body:
        result.errors.append("empty_response")
        return result

    facts: list[Q2FactProposal] = []
    artifacts: list[Q2ArtifactProposal] = []
    rules: list[Q2RuleProposal] = []
    uncertainties: list[str] = []
    recognized_sections: set[str] = set()
    section: str | None = None
    fact_category: str | None = None
    ioc_status: str | None = None
    artifact_type: str | None = None
    total_rule_content_bytes = 0
    lines = body.split("\n")
    i = 0
    while i < len(lines):
        heading = _q2_compact_heading(lines[i])
        if heading is not None:
            _level, heading_text = heading
            top = _fold(heading_text)
            token = _normalize_token(heading_text)
            if top in _Q2_TOP_SECTIONS:
                section = "uncertainties" if top == "incertitudes" else top
                recognized_sections.add(section)
                fact_category = None
                ioc_status = None
                artifact_type = None
                i += 1
                continue
            if section == "facts" and token in _Q2_FACT_CATEGORIES:
                fact_category = token
                i += 1
                continue
            if section == "iocs" and top in _Q2_IOC_STATUSES:
                ioc_status = _Q2_IOC_STATUSES[top]
                artifact_type = None
                i += 1
                continue
            if section == "rules":
                match = _Q2_RULE_HEADING.fullmatch(heading_text.strip())
                if match is not None:
                    rule_type = _Q2_RULE_TYPE_ALIASES[match.group("type").casefold()]
                    name = match.group("name").strip() or None if match.group("name") else None
                    opening_index = i + 1
                    while opening_index < len(lines) and not lines[opening_index].strip():
                        opening_index += 1
                    if (
                        opening_index >= len(lines)
                        or _FENCE_OPEN.fullmatch(lines[opening_index]) is None
                    ):
                        _rule_issue(result, "rule_without_body_fence")
                        i = opening_index
                        continue
                    closing_index: int | None = None
                    for candidate in range(opening_index + 1, len(lines)):
                        if _FENCE_CLOSE.fullmatch(lines[candidate]):
                            closing_index = candidate
                            break
                    if closing_index is None:
                        _rule_issue(result, "rule_truncated_not_promoted")
                        i = len(lines)
                        continue
                    # Preserve the literal body. Do not strip, refang,
                    # reconstruct or insert newlines into it.
                    body_value = "\n".join(lines[opening_index + 1 : closing_index])
                    body_bytes = len(body_value.encode("utf-8"))
                    if not body_value.strip():
                        _rule_issue(result, "rule_body_empty")
                    elif body_bytes > MAX_SINGLE_RULE_BODY_BYTES:
                        _rule_issue(result, "rule_limit_single_body")
                    elif rule_type is DetectionRuleType.YARA and not _yara_body_is_balanced(
                        body_value
                    ):
                        _rule_issue(result, "rule_truncated_not_promoted")
                    elif len(rules) >= MAX_RULES_PER_SOURCE:
                        _rule_issue(result, "rule_limit_max_rules_per_source")
                    elif (
                        total_rule_content_bytes + body_bytes
                        > MAX_TOTAL_RULE_CONTENT_BYTES_PER_SOURCE
                    ):
                        _rule_issue(result, "rule_limit_total_content_per_source")
                    else:
                        try:
                            rule = Q2RuleProposal(
                                rule_type=rule_type,
                                name=name,
                                body=body_value,
                                context="",
                                evidence_quote="",
                            )
                        except ValidationError:
                            _rule_issue(result, "rule_schema_invalid")
                        else:
                            rules.append(rule)
                            total_rule_content_bytes += body_bytes
                    i = closing_index + 1
                    continue

        if section == "facts" and fact_category is not None:
            bullet = _BULLET.match(lines[i])
            if bullet:
                value, context = _q2_value_and_context(bullet.group("text"))
                if value:
                    attack_id = (
                        value if fact_category == "ttps" and _ATTACK_ID.fullmatch(value) else None
                    )
                    try:
                        fact = Q2FactProposal(
                            category=cast(Any, fact_category),
                            value=value,
                            attack_id=attack_id,
                            context=context,
                            evidence_quote="",
                        )
                    except ValidationError:
                        result.warnings.append("fact_schema_invalid")
                    else:
                        facts.append(fact)
                else:
                    result.warnings.append("fact_without_value")
        elif section == "iocs" and ioc_status is not None:
            type_line = _Q2_TYPE_LINE.match(lines[i])
            if type_line:
                type_token = _normalize_token(type_line.group("type"))
                artifact_type = _Q2_ARTIFACT_TYPE_ALIASES.get(type_token)
                if artifact_type is None:
                    result.warnings.append("unknown_artifact_type")
            else:
                bullet = _BULLET.match(lines[i])
                if bullet and artifact_type is not None:
                    value, context = _q2_value_and_context(bullet.group("text"))
                    if value:
                        try:
                            artifact = Q2ArtifactProposal(
                                value=value,
                                artifact_type=cast(Any, artifact_type),
                                indicator_status=cast(Any, ioc_status),
                                context=context,
                                evidence_quote="",
                            )
                        except ValidationError:
                            result.warnings.append("artifact_schema_invalid")
                        else:
                            artifacts.append(artifact)
                    else:
                        result.warnings.append("artifact_without_value")
        elif section == "uncertainties":
            bullet = _BULLET.match(lines[i])
            if bullet and bullet.group("text").strip():
                uncertainties.append(bullet.group("text").strip())
        i += 1

    if not recognized_sections:
        result.errors.append("q2_compact_sections_missing")
        return result

    result.value = Q2SourceOutput(
        facts=facts,
        artifacts=artifacts,
        rules=rules,
        uncertainties=list(dict.fromkeys((*uncertainties, *result.uncertainties))),
    )
    return result


def _yara_body_is_balanced(body: str) -> bool:
    """Spot an obviously incomplete YARA body without parsing/compiling it."""
    depth = 0
    quote: str | None = None
    escaped = False
    in_line_comment = False
    index = 0
    while index < len(body):
        character = body[index]
        if in_line_comment:
            if character == "\n":
                in_line_comment = False
            index += 1
            continue
        if quote is not None:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == quote:
                quote = None
            index += 1
            continue
        if character in {'"', "'"}:
            quote = character
        elif character == "/" and body[index : index + 2] == "//":
            in_line_comment = True
            index += 1
        elif character == "{":
            depth += 1
        elif character == "}":
            depth -= 1
            if depth < 0:
                return False
        index += 1
    return quote is None and depth == 0


# --- Synthesis validation --------------------------------------------------


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

    # The Q4 prose is a publication-ready account of the evidence pack.  A
    # paragraph without a corpus marker cannot be traced back to that pack.
    offset = 0
    for paragraph in re.split(r"\n\s*\n", body):
        start = body.find(paragraph, offset)
        offset = start + len(paragraph)
        if paragraph.strip() and not _reference_tokens(paragraph):
            reject("uncited_factual_paragraph", paragraph.strip(), (start, offset))

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
        (
            "internal_display_label",
            re.compile(r"\b(?:excluded|hidden)\b", re.IGNORECASE),
            "Internal publication label",
        ),
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
        "schema_version": "3",
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
                "evidence_quote": item.evidence_quote,
                "model_run_ids": list(item.model_run_ids),
                "attack_id": item.attack_id,
                "reference_ids": list(item.reference_ids),
                "source_ids": list(item.source_ids),
                "supported": item.supported,
            }
            for item in extraction.items
        ],
        "rules": [
            {
                "rule_type": rule.rule_type.value,
                "name": rule.name,
                "body": rule.body,
                "source_ids": list(rule.source_ids),
                "context": rule.context,
                "evidence_quote": rule.evidence_quote,
                "supported": rule.supported,
                "model_run_ids": list(rule.model_run_ids),
                "sha256": rule.sha256,
            }
            for rule in extraction.rules
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
            semantic_type=_enum_value(item.get("semantic_type"), SemanticType, SemanticType.OTHER)
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
            evidence_quote=item.get("evidence_quote", ""),
            model_run_ids=tuple(item.get("model_run_ids", [])),
        )

    def read_rule(item: dict[str, Any]) -> DetectionRule:
        rule_type = DetectionRuleType(item["rule_type"])
        body = str(item["body"]).replace("\r\n", "\n").replace("\r", "\n")
        if not body.strip():
            raise ValueError("Detection rule body must not be empty")
        if len(body.encode("utf-8")) > MAX_SINGLE_RULE_BODY_BYTES:
            raise ValueError("Detection rule body exceeds its size limit")
        sha256 = hashlib.sha256(body.encode("utf-8")).hexdigest()
        stored_sha256 = item.get("sha256")
        if stored_sha256 is not None and stored_sha256 != sha256:
            raise ValueError("Detection rule sha256 does not match its body")
        name = item.get("name")
        if name is not None and not isinstance(name, str):
            raise ValueError("Detection rule name must be a string or null")
        return DetectionRule(
            rule_type=rule_type,
            name=name,
            body=body,
            source_ids=tuple(str(source_id) for source_id in item.get("source_ids", [])),
            context=str(item.get("context", "")),
            evidence_quote=str(item.get("evidence_quote", "")),
            supported=bool(item.get("supported", False)),
            model_run_ids=tuple(str(run_id) for run_id in item.get("model_run_ids", [])),
            sha256=sha256,
        )

    return TechnicalExtraction(
        items=tuple(read_item(item) for item in payload.get("items", [])),
        uncertainties=tuple(payload.get("uncertainties", [])),
        rules=tuple(read_rule(item) for item in payload.get("rules", [])),
    )
