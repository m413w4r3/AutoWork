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
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from enum import StrEnum
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from cti_app.application.discovery_report_parser import extract_http_urls
from cti_app.application.production_normalization import (
    normalize_indicator_value,
)
from cti_app.domain.discovery import SourceRole
from cti_app.domain.production import (
    DetectionRule,
    DetectionRuleType,
    ExtractionProfile,
    ProductionEvidenceBasis,
)
from cti_app.domain.publication import ArtifactType

PARSER_VERSION = "production-markdown-v4"

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


@dataclass(frozen=True, slots=True)
class ReferenceIntegrationResult:
    """Deterministic projection of a Q1 proposal onto archived URLs.

    The parser produces a proposal, while this projection is the only rule
    that decides which part of that proposal can become canonical production
    state.  The optional counters and diagnostics are populated by the live
    collection workflow; the reconciliation itself never performs I/O.
    """

    report: ReferenceReport
    dropped_source_ids: tuple[str, ...] = ()
    dropped_event_ids: tuple[str, ...] = ()
    restored_source_ids: tuple[str, ...] = ()
    restored_event_ids: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    new_sources: int = 0
    archived_sources: int = 0
    supplemental_collection_failures: tuple[dict[str, Any], ...] = ()

    @property
    def kept_events(self) -> tuple[ParsedEvent, ...]:
        return self.report.events

    def __getitem__(self, key: str) -> Any:
        """Keep the pre-LOT-19 mapping shape for internal/test callers."""
        if key == "kept_events":
            return list(self.kept_events)
        if key == "supplemental_collection_failures":
            return list(self.supplemental_collection_failures)
        return getattr(self, key)


def reconcile_reference_report_with_archives(
    proposed_report: ReferenceReport,
    archived_urls: set[str],
    *,
    previous_canonical_report: ReferenceReport | None = None,
) -> ReferenceIntegrationResult:
    """Keep only Q1 sources and events backed by currently archived URLs.

    This function is intentionally pure.  In particular, it does not inspect
    collections, read blobs, or attempt to fetch a missing source.  Its stable
    ordering is inherited from the already parsed Q1 report, which makes the
    result suitable for both the initial workflow and a human repair.
    """
    archived_source_ids = {
        source.local_id
        for source in proposed_report.sources
        if source.canonical_url in archived_urls
    }
    kept_sources = tuple(
        source for source in proposed_report.sources if source.local_id in archived_source_ids
    )
    dropped_source_ids = tuple(
        source.local_id
        for source in proposed_report.sources
        if source.local_id not in archived_source_ids
    )

    kept_events: list[ParsedEvent] = []
    dropped_event_ids: list[str] = []
    for event in proposed_report.events:
        backed_source_ids = tuple(
            source_id for source_id in event.source_ids if source_id in archived_source_ids
        )
        if not backed_source_ids:
            dropped_event_ids.append(event.local_id)
            continue
        kept_events.append(
            ParsedEvent(
                local_id=event.local_id,
                event_date=event.event_date,
                source_ids=backed_source_ids,
                text=event.text,
            )
        )

    previous_source_ids = (
        previous_canonical_report.source_ids() if previous_canonical_report is not None else set()
    )
    previous_event_ids = (
        {event.local_id for event in previous_canonical_report.events}
        if previous_canonical_report is not None
        else set()
    )
    restored_source_ids = tuple(
        source.local_id for source in kept_sources if source.local_id not in previous_source_ids
    )
    restored_event_ids = tuple(
        event.local_id for event in kept_events if event.local_id not in previous_event_ids
    )

    return ReferenceIntegrationResult(
        report=ReferenceReport(
            sources=kept_sources,
            events=tuple(kept_events),
            uncertainties=proposed_report.uncertainties,
            editorial_title=proposed_report.editorial_title,
        ),
        dropped_source_ids=dropped_source_ids,
        dropped_event_ids=tuple(dropped_event_ids),
        restored_source_ids=restored_source_ids,
        restored_event_ids=restored_event_ids,
    )


@dataclass(frozen=True)
class _SourceCandidate:
    """A parsed publication before canonical source IDs are assigned."""

    model_alias: str | None
    title: str
    canonical_url: str
    publisher: str | None
    published_at: date | None
    role: SourceRole


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
    evidence_basis: ProductionEvidenceBasis = ProductionEvidenceBasis.SOURCE_VERIFIED


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
Q2_MARKDOWN_PARSER_VERSION = "q2-markdown-v6"


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
    if payload.get("contract_version") != Q2_EXTRACTION_CONTRACT_VERSION:
        raise ValueError("Q2 source extraction contract is incompatible")
    if payload.get("schema_version") != Q2_SCHEMA_VERSION:
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
            f"{match.group(1).upper()}{int(match.group(2))}" for match in _LOCAL_ID.finditer(value)
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

    source_candidates: list[_SourceCandidate] = []

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

        source_candidates.append(
            _SourceCandidate(
                model_alias=block.local_id,
                title=(values.get("title") or values.get("titre") or "").strip(),
                canonical_url=canonical,
                publisher=(values.get("publisher") or values.get("editeur") or "").strip() or None,
                published_at=published_at,
                role=role,
            )
        )

    # Model IDs are transport aliases only. Make missing aliases usable for
    # references, while reserving every explicit alias so no generated alias
    # can silently change the meaning of an explicit one.
    reserved_aliases = {
        candidate.model_alias
        for candidate in source_candidates
        if candidate.model_alias is not None
    }
    next_generated_alias = 1
    source_candidates_with_aliases: list[_SourceCandidate] = []
    for candidate in source_candidates:
        model_alias = candidate.model_alias
        if model_alias is None:
            while f"S{next_generated_alias}" in reserved_aliases:
                next_generated_alias += 1
            model_alias = f"S{next_generated_alias}"
            reserved_aliases.add(model_alias)
            next_generated_alias += 1
            result.warnings.append("source_id_generated")
        source_candidates_with_aliases.append(replace(candidate, model_alias=model_alias))

    # Deduplicate after every publication has been parsed, preserving the
    # first occurrence as the surviving publication.
    surviving_candidates: list[_SourceCandidate] = []
    seen_canonical_urls: set[str] = set()
    for candidate in source_candidates_with_aliases:
        if candidate.canonical_url in seen_canonical_urls:
            result.warnings.append("duplicate_source_merged")
            continue
        seen_canonical_urls.add(candidate.canonical_url)
        surviving_candidates.append(candidate)

    canonical_id_by_url = {
        candidate.canonical_url: f"S{index}"
        for index, candidate in enumerate(surviving_candidates, 1)
    }
    sources = [
        ParsedSource(
            local_id=canonical_id_by_url[candidate.canonical_url],
            title=candidate.title,
            # Subject Production never exposes the model's tracking URL. The
            # canonical URL is also the user-visible URL and the URL handed to Q2.
            url=candidate.canonical_url,
            canonical_url=candidate.canonical_url,
            publisher=candidate.publisher,
            published_at=candidate.published_at,
            role=candidate.role,
        )
        for candidate in surviving_candidates
    ]

    alias_targets: dict[str, set[str]] = {}
    for candidate in source_candidates_with_aliases:
        assert candidate.model_alias is not None
        alias_targets.setdefault(candidate.model_alias, set()).add(
            canonical_id_by_url[candidate.canonical_url]
        )
    alias = {
        model_alias: next(iter(canonical_ids))
        for model_alias, canonical_ids in alias_targets.items()
        if len(canonical_ids) == 1
    }
    ambiguous_aliases = {
        model_alias
        for model_alias, canonical_ids in alias_targets.items()
        if len(canonical_ids) > 1
    }
    known_ids = set(canonical_id_by_url.values())
    event_candidates: list[tuple[date | None, tuple[str, ...], str]] = []

    for block in (b for b in blocks if b.kind == "event"):
        values = _fields(block.lines)
        if block.local_id is None:
            result.warnings.append("event_id_generated")

        event_text = (values.get("text") or values.get("texte") or "").strip()
        if not event_text:
            result.dropped_blocks.append(block.raw())
            result.warnings.append("event_without_text_dropped")
            continue

        raw_sources = values.get("sources") or values.get("source") or ""
        cited = _reference_tokens(raw_sources)
        if any(token in ambiguous_aliases for token in cited):
            result.dropped_blocks.append(block.raw())
            result.warnings.append("event_ambiguous_source_alias_dropped")
            continue
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

        event_candidates.append((event_date, resolved, event_text))

    events = [
        ParsedEvent(
            local_id=f"R{index}",
            event_date=event_date,
            source_ids=source_ids,
            text=event_text,
        )
        for index, (event_date, source_ids, event_text) in enumerate(event_candidates, 1)
    ]

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


# --- Q2 stateless Markdown wire format -------------------------------------
#
# Q2 is a small, source-bound wire format. A response is already associated
# with one source by the orchestrator, so source ids, URLs, evidence,
# provenance and per-value status are intentionally absent from the model
# output. This parser expands self-contained groups into the existing proposal
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

_Q2_IOC_TYPE_TO_ARTIFACT_TYPE = {
    "domain": "domain",
    "ip": "ip",
    "url": "url",
    "email": "email",
    "md5": "hash",
    "sha1": "hash",
    "sha256": "hash",
    "sha512": "hash",
    "filename": "filename",
    "filepath": "filepath",
    "cve": "cve",
}

_Q2_RULE_TYPES = {
    "yara": DetectionRuleType.YARA,
    "sigma": DetectionRuleType.SIGMA,
    "suricata": DetectionRuleType.SURICATA,
    "snort": DetectionRuleType.SNORT,
}

MAX_RULES_PER_SOURCE = 100
MAX_SINGLE_RULE_BODY_BYTES = 128 * 1024
MAX_TOTAL_RULE_CONTENT_BYTES_PER_SOURCE = 2 * 1024 * 1024

_ATTACK_ID = re.compile(r"T\d{4}(?:\.\d{3})?")

_Q2_HEADER_TEXT = re.compile(r"^\s*(?:#{1,6}\s+)?(?P<text>.+?)\s*#*\s*$")
_Q2_FACT_HEADER = re.compile(r"^FACT(?:\s+(?P<category>.+))?$", re.IGNORECASE)
_Q2_RULE_HEADER = re.compile(r"^RULE(?:\s+(?P<spec>.+))?$", re.IGNORECASE)
_Q2_FENCE_OPEN = re.compile(r"^\s*```[^\n]*$")
_Q2_RULE_FENCE_OPEN = re.compile(r"^\s*```(?P<language>[A-Za-z0-9_-]+)\s*$")
_Q2_TERMINAL_MARKERS = frozenset({"empty", "unavailable"})

_Q2_FACT_CATEGORIES_BY_CASEFOLD = {
    category.casefold(): category for category in _Q2_FACT_CATEGORIES
}
_Q2_IOC_TYPES_BY_CASEFOLD = {
    type_token.casefold(): artifact_type
    for type_token, artifact_type in _Q2_IOC_TYPE_TO_ARTIFACT_TYPE.items()
}
_Q2_RULE_TYPES_BY_CASEFOLD = {
    type_token.casefold(): rule_type for type_token, rule_type in _Q2_RULE_TYPES.items()
}


def _rule_issue(result: ParseResult[Q2SourceOutput], code: str) -> None:
    result.warnings.append(code)
    result.uncertainties.append(code)


def _normalize_q2_input(raw: str) -> str:
    """Normalize only transport line endings around the Q2 wire format."""
    text = raw.replace("\r\n", "\n").replace("\r", "\n")
    if text.startswith(_BOM):
        text = text[1:]
    return text.strip()


@dataclass(frozen=True)
class _Q2Header:
    kind: str
    category: str | None = None
    indicator_status: str | None = None
    artifact_type: str | None = None
    rule_type: DetectionRuleType | None = None
    name: str | None = None
    error_code: str | None = None


def _q2_header_text(line: str) -> str | None:
    """Return a header candidate, accepting optional Markdown hashes."""
    if not line.strip() or _BULLET.match(line):
        return None
    match = _Q2_HEADER_TEXT.fullmatch(line)
    return match.group("text").strip() if match else None


def _parse_q2_header(line: str) -> _Q2Header | None:
    candidate = _q2_header_text(line)
    if candidate is None:
        return None

    if candidate.casefold() == "uncertainties":
        return _Q2Header(kind="uncertainties")

    fact_match = _Q2_FACT_HEADER.fullmatch(candidate)
    if fact_match is not None:
        category = fact_match.group("category")
        canonical_category = (
            _Q2_FACT_CATEGORIES_BY_CASEFOLD.get(category.casefold())
            if category is not None
            else None
        )
        if canonical_category is not None:
            return _Q2Header(kind="fact", category=canonical_category)
        return _Q2Header(kind="invalid", error_code="q2_unknown_fact_category")

    parts = candidate.split()
    if parts and parts[0].casefold() == "ioc":
        status = parts[1].casefold() if len(parts) > 1 else ""
        type_token = parts[2].casefold() if len(parts) > 2 else ""
        if status not in {"confirmed", "contextual"}:
            return _Q2Header(kind="invalid", error_code="q2_unknown_ioc_status")
        artifact_type = _Q2_IOC_TYPES_BY_CASEFOLD.get(type_token)
        if artifact_type is None or len(parts) != 3:
            return _Q2Header(kind="invalid", error_code="q2_unknown_ioc_type")
        return _Q2Header(
            kind="ioc",
            indicator_status=("confirmed_ioc" if status == "confirmed" else "contextual"),
            artifact_type=artifact_type,
        )

    rule_match = _Q2_RULE_HEADER.fullmatch(candidate)
    if rule_match is not None:
        specification = rule_match.group("spec")
        if not specification:
            return _Q2Header(kind="invalid", error_code="q2_unknown_rule_type")
        raw_type, separator, raw_name = specification.partition(":")
        rule_type = _Q2_RULE_TYPES_BY_CASEFOLD.get(raw_type.strip().casefold())
        if rule_type is None:
            return _Q2Header(kind="invalid", error_code="q2_unknown_rule_type")
        if separator and not raw_name.strip():
            return _Q2Header(kind="invalid", error_code="q2_rule_name_missing")
        return _Q2Header(
            kind="rule",
            rule_type=rule_type,
            name=raw_name.strip() if separator else None,
        )

    return None


def _q2_is_markdown_heading(line: str) -> bool:
    return bool(re.match(r"^\s*#{1,6}(?:\s+|$)", line))


def _q2_terminal_markers_outside_fences(lines: list[str]) -> tuple[str, ...]:
    markers: list[str] = []
    in_fence = False
    for line in lines:
        if in_fence:
            if _FENCE_CLOSE.fullmatch(line):
                in_fence = False
            continue
        if _Q2_FENCE_OPEN.fullmatch(line) and not _FENCE_CLOSE.fullmatch(line):
            in_fence = True
            continue
        marker = line.strip().casefold()
        if marker in _Q2_TERMINAL_MARKERS:
            markers.append(marker)
    return tuple(markers)


def _q2_fence_close(lines: list[str], opening_index: int) -> int | None:
    return next(
        (
            candidate
            for candidate in range(opening_index + 1, len(lines))
            if _FENCE_CLOSE.fullmatch(lines[candidate])
        ),
        None,
    )


def _q2_undecorate_value(value: str) -> str:
    """Remove balanced Markdown code/emphasis wrappers around a whole value.

    A chat model routinely renders an IOC as `` `1.2.3.4` `` or ``**1.2.3.4**``.
    Those wrappers are presentation, not part of the indicator, and keeping
    them turned a published IOC into an invalid value.  Only wrappers balanced
    around the entire token are removed, and nothing inside the value is
    touched.
    """
    for wrapper in ("```", "``", "`", "***", "**", "*"):
        while (
            value.startswith(wrapper) and value.endswith(wrapper) and len(value) > 2 * len(wrapper)
        ):
            value = value[len(wrapper) : -len(wrapper)].strip()
    return value


def _q2_value_and_context(raw: str) -> tuple[str, str]:
    """Split an optional annotation, leaving IPv6 ``::`` literals intact."""
    parts = re.split(r"\s+::\s+", raw.strip(), maxsplit=1)
    value = _q2_undecorate_value(parts[0].strip())
    context = parts[1].strip() if len(parts) == 2 else ""
    return value, context


def parse_q2_proposals_markdown(text: str) -> ParseResult[Q2SourceOutput]:
    """Parse the stateless Q2 wire format, failing closed at group boundaries."""
    result: ParseResult[Q2SourceOutput] = ParseResult()
    body = _normalize_q2_input(text)
    if not body:
        result.errors.append("empty_response")
        return result

    if body.casefold() == "empty":
        result.value = Q2SourceOutput()
        return result
    if body.casefold() == "unavailable":
        result.errors.append("q2_source_unavailable")
        return result

    facts: list[Q2FactProposal] = []
    artifacts: list[Q2ArtifactProposal] = []
    rules: list[Q2RuleProposal] = []
    uncertainties: list[str] = []
    lines = body.split("\n")
    if _q2_terminal_markers_outside_fences(lines):
        result.errors.append("q2_terminal_marker_mixed")
        return result

    recognized_groups = 0
    current: _Q2Header | None = None
    total_rule_content_bytes = 0
    i = 0
    while i < len(lines):
        if _Q2_FENCE_OPEN.fullmatch(lines[i]) and not _FENCE_CLOSE.fullmatch(lines[i]):
            if current is not None:
                result.warnings.append("q2_unexpected_structure")
                result.dropped_blocks.append(lines[i].strip())
                current = None
            closing_index = _q2_fence_close(lines, i)
            i = len(lines) if closing_index is None else closing_index + 1
            continue
        if _FENCE_CLOSE.fullmatch(lines[i]):
            if current is not None:
                result.warnings.append("q2_unexpected_structure")
                result.dropped_blocks.append(lines[i].strip())
                current = None
            i += 1
            continue

        header = _parse_q2_header(lines[i])
        if header is not None:
            if header.kind == "invalid":
                result.warnings.append(header.error_code or "q2_invalid_group")
                result.dropped_blocks.append(lines[i].strip())
                current = None
                i += 1
                continue

            recognized_groups += 1
            current = header
            if header.kind != "rule":
                i += 1
                continue

            opening_index = i + 1
            while opening_index < len(lines) and not lines[opening_index].strip():
                opening_index += 1
            if (
                opening_index >= len(lines)
                or _Q2_RULE_FENCE_OPEN.fullmatch(lines[opening_index]) is None
            ):
                _rule_issue(result, "rule_without_body_fence")
                result.dropped_blocks.append(lines[i].strip())
                current = None
                # A malformed fence still encloses opaque text until its close.
                if opening_index < len(lines) and lines[opening_index].lstrip().startswith("```"):
                    closing_index = _q2_fence_close(lines, opening_index)
                    i = len(lines) if closing_index is None else closing_index + 1
                else:
                    i += 1
                continue

            closing_index = _q2_fence_close(lines, opening_index)
            if closing_index is None:
                _rule_issue(result, "rule_truncated_not_promoted")
                result.dropped_blocks.append(lines[i].strip())
                current = None
                i = len(lines)
                continue

            # Preserve the body literally. Only line endings were normalized
            # before this point; do not strip or otherwise rewrite it.
            body_value = "\n".join(lines[opening_index + 1 : closing_index])
            body_bytes = len(body_value.encode("utf-8"))
            rule_type = header.rule_type
            if not body_value.strip():
                _rule_issue(result, "rule_body_empty")
            elif body_bytes > MAX_SINGLE_RULE_BODY_BYTES:
                _rule_issue(result, "rule_limit_single_body")
            elif rule_type is DetectionRuleType.YARA and not _yara_body_is_balanced(body_value):
                _rule_issue(result, "rule_truncated_not_promoted")
            elif len(rules) >= MAX_RULES_PER_SOURCE:
                _rule_issue(result, "rule_limit_max_rules_per_source")
            elif total_rule_content_bytes + body_bytes > MAX_TOTAL_RULE_CONTENT_BYTES_PER_SOURCE:
                _rule_issue(result, "rule_limit_total_content_per_source")
            else:
                try:
                    rule = Q2RuleProposal(
                        rule_type=cast(Any, rule_type),
                        name=header.name,
                        body=body_value,
                        context="",
                        evidence_quote="",
                    )
                except ValidationError:
                    _rule_issue(result, "rule_schema_invalid")
                else:
                    rules.append(rule)
                    total_rule_content_bytes += body_bytes
            current = None
            i = closing_index + 1
            continue

        if not lines[i].strip():
            i += 1
            continue

        if current is None:
            i += 1
            continue

        bullet = _BULLET.match(lines[i])
        if bullet is None:
            result.warnings.append(
                "q2_unknown_heading"
                if _q2_is_markdown_heading(lines[i])
                else "q2_unexpected_structure"
            )
            result.dropped_blocks.append(lines[i].strip())
            current = None
            i += 1
            continue

        value, context = _q2_value_and_context(bullet.group("text"))
        if not value:
            result.warnings.append("q2_bullet_without_value")
            current = None
            i += 1
            continue

        if current.kind == "fact":
            attack_id = (
                value if current.category == "ttps" and _ATTACK_ID.fullmatch(value) else None
            )
            try:
                fact = Q2FactProposal(
                    category=cast(Any, current.category),
                    value=value,
                    attack_id=attack_id,
                    context=context,
                    evidence_quote="",
                )
            except ValidationError:
                result.warnings.append("fact_schema_invalid")
            else:
                facts.append(fact)
        elif current.kind == "ioc":
            try:
                artifact = Q2ArtifactProposal(
                    value=value,
                    artifact_type=cast(Any, current.artifact_type),
                    indicator_status=cast(Any, current.indicator_status),
                    context=context,
                    evidence_quote="",
                )
            except ValidationError:
                result.warnings.append("artifact_schema_invalid")
            else:
                artifacts.append(artifact)
        elif current.kind == "uncertainties":
            uncertainties.append(bullet.group("text").strip())
        i += 1

    if not recognized_groups:
        result.errors.append("q2_compact_sections_missing")
        return result

    if not (facts or artifacts or rules or uncertainties):
        result.errors.append("q2_no_payload")
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


# Le modèle ne doit pas recopier les libellés internes du pack. Un mot isolé
# ("hidden" dans `-WindowStyle Hidden`) est du contenu technique légitime :
# seule la forme clé/valeur, ou le nom du champ lui-même, est un libellé.
_INTERNAL_LABEL_PATTERN = re.compile(
    r"(?:\b(?:display_policy|indicator_status|display\s+policy|indicator\s+status)\b"
    r"|(?<![\w-])(?:excluded|hidden|not_applicable|body_only|ioc_section|confirmed_ioc)"
    r"\s*(?:[:=]|\u00a0:)"
    r"|[:=]\s*(?:excluded|hidden|not_applicable|body_only|ioc_section|confirmed_ioc)\b)",
    re.IGNORECASE,
)


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
            _INTERNAL_LABEL_PATTERN,
            "Internal publication label",
        ),
    )
    for code, pattern, detail in checks:
        for match in pattern.finditer(body):
            # Le tour de réparation a besoin du texte exact à réécrire, pas
            # seulement du nom de la règle.
            offending = " ".join(match.group(0).split())
            reject(code, f"{detail}: {offending}" if offending else detail, match.span())

    if not isinstance(known_indicators, TechnicalExtraction):
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
        "schema_version": "4",
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
                "evidence_basis": item.evidence_basis.value,
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
                "evidence_basis": rule.evidence_basis.value,
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
            evidence_basis=_enum_value(
                item.get("evidence_basis"),
                ProductionEvidenceBasis,
                ProductionEvidenceBasis.SOURCE_VERIFIED,
            )
            or ProductionEvidenceBasis.SOURCE_VERIFIED,
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
            evidence_basis=_enum_value(
                item.get("evidence_basis"),
                ProductionEvidenceBasis,
                ProductionEvidenceBasis.SOURCE_VERIFIED,
            )
            or ProductionEvidenceBasis.SOURCE_VERIFIED,
        )

    return TechnicalExtraction(
        items=tuple(read_item(item) for item in payload.get("items", [])),
        uncertainties=tuple(payload.get("uncertainties", [])),
        rules=tuple(read_rule(item) for item in payload.get("rules", [])),
    )
