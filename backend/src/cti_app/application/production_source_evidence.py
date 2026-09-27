"""Source-local evidence gate for Q2 proposals.

This module deliberately knows about one source text only.  It does not assign
provenance, call external services, or attempt to repair a model proposal.
It never performs OCR: ``source_evidence_not_text_verifiable`` means that a
proposal could not be proven in safe local text while the archive contains
visual material, not that the source collection itself is missing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from html import unescape
from html.parser import HTMLParser

from cti_app.application.production_parsers import (
    Q2ArtifactProposal,
    Q2EventProposal,
    Q2FactProposal,
    Q2RuleProposal,
    Q2SourceOutput,
)
from cti_app.domain.publication import ArtifactType

SOURCE_EVIDENCE_VERSION = "7"

_NBSP = "\u00a0"
_NARROW_NBSP = "\u202f"
# Zero-width and soft-hyphen characters are inserted by publishing pipelines to
# allow long IOC cells to wrap. They carry no value and must never decide
# whether a published indicator can be proven in its own source.
_INVISIBLE = str.maketrans(dict.fromkeys("\u00ad\u200b\u200c\u200d\u2060\ufeff"))
_DOT = re.compile(r"\[\.\]|\(\.\)|\{\.\}", re.IGNORECASE)
_COLON = re.compile(r"\[:\]", re.IGNORECASE)
_AT = re.compile(r"\[(?:at|@)\]|\((?:at|@)\)", re.IGNORECASE)
_DEFANGED_SCHEME = re.compile(r"(?<!\w)hxxp(?P<secure>s?)://", re.IGNORECASE)

# These are continuation characters of an indicator token.  Punctuation not
# listed here remains a delimiter; no punctuation is removed from the value.
_DOMAIN_CONTINUATION = ".-_"
_HASH_CONTINUATION = ""
_CVE_CONTINUATION = "-"
_IP_CONTINUATION = ".:"
_URL_CONTINUATION = "!#$%&'*+,-./:;=?@_~%"
_EMAIL_CONTINUATION = "!#$%&'*+-./=?^_`{|}~@"
_FILENAME_CONTINUATION = ".-_"
_FILEPATH_CONTINUATION = "._-/\\:"


class SourceEvidenceSpanKind(StrEnum):
    """Structural location available in the archived source evidence."""

    BODY_TEXT = "body_text"
    TABLE = "table"
    LIST = "list"
    CODE_BLOCK = "code_block"
    LINK_TEXT = "link_text"
    ALT_TEXT = "alt_text"
    VISUAL_UNLOCATED = "visual_unlocated"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class SourceEvidenceSpan:
    """A safe structural text span; it never claims visual localization."""

    kind: SourceEvidenceSpanKind
    text: str = ""


@dataclass(frozen=True, slots=True)
class SourceEvidenceDocument:
    """The local views allowed to prove one Q2 proposal.

    ``decoded_source_view`` is deliberately not the raw HTML.  It is a safe
    text projection containing rendered text and visible ``alt``/``title``
    attributes, while excluding URL-bearing attributes, scripts and metadata.
    """

    parsed_text: str
    decoded_source_view: str = ""
    has_unverifiable_visuals: bool = False
    spans: tuple[SourceEvidenceSpan, ...] = ()

    def __post_init__(self) -> None:
        if not self.spans and self.parsed_text:
            object.__setattr__(
                self,
                "spans",
                (SourceEvidenceSpan(SourceEvidenceSpanKind.BODY_TEXT, self.parsed_text),),
            )


class _SafeHtmlEvidenceParser(HTMLParser):
    _SKIPPED_TAGS = frozenset({"script", "style", "noscript", "template", "svg"})
    _VISUAL_TAGS = frozenset({"img", "picture", "canvas", "object", "embed", "svg"})
    # An inline element never separates two indicator characters: publishers
    # routinely wrap part of an IOC cell in ``<span>``, ``<b>`` or ``<wbr>``.
    # Breaking a line there would make a published indicator unprovable in its
    # own source. Every other element, ``<br>`` included, ends the line.
    _INLINE_TAGS = frozenset(
        {
            "a",
            "abbr",
            "b",
            "bdi",
            "bdo",
            "big",
            "cite",
            "code",
            "data",
            "del",
            "dfn",
            "em",
            "font",
            "i",
            "ins",
            "kbd",
            "mark",
            "q",
            "s",
            "samp",
            "small",
            "span",
            "strike",
            "strong",
            "sub",
            "sup",
            "time",
            "tt",
            "u",
            "var",
            "wbr",
        }
    )

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip_depth = 0
        self.has_unverifiable_visuals = False
        self._line: list[str] = []

    @property
    def text(self) -> str:
        self._flush()
        return "\n".join(part for part in self.parts if part).strip()

    def _flush(self) -> None:
        line = "".join(self._line).strip()
        self._line = []
        if line:
            self.parts.append(line)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.casefold()
        if self.skip_depth:
            if tag in self._SKIPPED_TAGS:
                self.skip_depth += 1
            return
        if tag in self._VISUAL_TAGS:
            self.has_unverifiable_visuals = True
        if tag in self._SKIPPED_TAGS:
            self.skip_depth += 1
            return
        if tag not in self._INLINE_TAGS:
            self._flush()
        for key, value in attrs:
            if key.casefold() in {"alt", "title"} and value:
                cleaned = " ".join(unescape(value).split())
                if cleaned:
                    self._flush()
                    self.parts.append(cleaned)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        if tag in self._SKIPPED_TAGS and self.skip_depth:
            self.skip_depth -= 1
            return
        if not self.skip_depth and tag not in self._INLINE_TAGS:
            self._flush()

    def handle_data(self, data: str) -> None:
        if self.skip_depth:
            return
        # Collapse runs of whitespace but keep the boundaries: an indicator
        # split across inline elements is joined, one separated by real
        # whitespace stays two tokens.
        cleaned = re.sub(r"\s+", " ", data)
        if cleaned:
            self._line.append(cleaned)


class _StructuredEvidenceParser(HTMLParser):
    """Build safe structural spans without including URL-bearing attributes."""

    _SKIPPED_TAGS = frozenset({"script", "style", "noscript", "template", "svg"})
    _VISUAL_TAGS = frozenset({"img", "picture", "canvas", "object", "embed", "svg"})
    _KNOWN_TAGS = frozenset(
        {
            "a",
            "abbr",
            "article",
            "b",
            "body",
            "br",
            "code",
            "dd",
            "div",
            "em",
            "h1",
            "h2",
            "h3",
            "h4",
            "h5",
            "h6",
            "head",
            "html",
            "i",
            "li",
            "main",
            "p",
            "pre",
            "section",
            "span",
            "strong",
            "table",
            "tbody",
            "td",
            "th",
            "thead",
            "tr",
            "ul",
            "ol",
        }
    )

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.spans: list[SourceEvidenceSpan] = []
        self.stack: list[str] = []
        self.skip_depth = 0
        self._kind: SourceEvidenceSpanKind | None = None
        self._buffer: list[str] = []

    def _flush(self) -> None:
        if self._kind is not None:
            text = "".join(self._buffer).strip()
            if text:
                self.spans.append(SourceEvidenceSpan(self._kind, text))
        self._kind = None
        self._buffer = []

    def _kind_for_stack(self) -> SourceEvidenceSpanKind:
        if "a" in self.stack:
            return SourceEvidenceSpanKind.LINK_TEXT
        if "pre" in self.stack or "code" in self.stack:
            return SourceEvidenceSpanKind.CODE_BLOCK
        if "li" in self.stack:
            return SourceEvidenceSpanKind.LIST
        if any(tag in self.stack for tag in ("table", "thead", "tbody", "tr", "td", "th")):
            return SourceEvidenceSpanKind.TABLE
        if self.stack and self.stack[-1] not in self._KNOWN_TAGS:
            return SourceEvidenceSpanKind.UNKNOWN
        return SourceEvidenceSpanKind.BODY_TEXT

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.casefold()
        if self.skip_depth:
            if tag in self._SKIPPED_TAGS:
                self.skip_depth += 1
            return
        if tag in self._VISUAL_TAGS:
            self._flush()
            self.spans.append(SourceEvidenceSpan(SourceEvidenceSpanKind.VISUAL_UNLOCATED))
        if tag in self._SKIPPED_TAGS:
            self.skip_depth += 1
            return
        for key, value in attrs:
            if key.casefold() == "alt" and value:
                self._flush()
                cleaned = " ".join(unescape(value).split())
                if cleaned:
                    self.spans.append(SourceEvidenceSpan(SourceEvidenceSpanKind.ALT_TEXT, cleaned))
        self.stack.append(tag)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        if tag in self._SKIPPED_TAGS and self.skip_depth:
            self.skip_depth -= 1
            return
        self._flush()
        if self.stack:
            try:
                self.stack.reverse()
                self.stack.remove(tag)
                self.stack.reverse()
            except ValueError:
                self.stack.reverse()

    def handle_data(self, data: str) -> None:
        if self.skip_depth:
            return
        cleaned = re.sub(r"\s+", " ", data)
        if not cleaned:
            return
        kind = self._kind_for_stack()
        if self._kind is not None and kind is not self._kind:
            self._flush()
        self._kind = kind
        self._buffer.append(cleaned)


def source_evidence_document_from_html(
    parsed_text: str,
    decoded_html: str,
) -> SourceEvidenceDocument:
    """Add the minimal safe HTML view needed after a demonstrated text loss."""
    parser = _SafeHtmlEvidenceParser()
    structured = _StructuredEvidenceParser()
    try:
        parser.feed(decoded_html)
        parser.close()
        structured.feed(decoded_html)
        structured.close()
    except Exception:
        return SourceEvidenceDocument(parsed_text=parsed_text)
    spans = tuple(structured.spans)
    if not spans and parsed_text:
        spans = (SourceEvidenceSpan(SourceEvidenceSpanKind.BODY_TEXT, parsed_text),)
    return SourceEvidenceDocument(
        parsed_text=parsed_text,
        decoded_source_view=parser.text,
        has_unverifiable_visuals=parser.has_unverifiable_visuals,
        spans=spans,
    )


@dataclass(frozen=True, slots=True)
class SourceEvidenceRejection:
    """One Q2 proposal removed because this source cannot prove it locally."""

    proposal_index: int
    proposal_kind: str
    reason_code: str
    value: str
    artifact_type: str | None = None

    @property
    def reason(self) -> str:
        """Compatibility alias for callers that use ``reason`` terminology."""
        return self.reason_code


@dataclass(frozen=True, slots=True)
class Q2ProposalIdentity:
    """One numbered Q2 proposal, as the source-evidence gate sees it."""

    proposal_index: int
    proposal_kind: str
    artifact_type: str | None
    value: str


def enumerate_q2_proposals(output: Q2SourceOutput) -> tuple[Q2ProposalIdentity, ...]:
    """Number every artifact and rule exactly as the gate numbers rejections.

    Recovering a historical rejection from an archived Q2 output means finding
    the proposal the gate rejected, so both must count proposals the same way:
    facts, then events, then artifacts, then rules.  Events are not enumerated
    because they are not repair-addressable; they only keep the offset right.
    """
    identities: list[Q2ProposalIdentity] = []
    proposal_index = len(output.facts) + len(output.events)
    for artifact in output.artifacts:
        proposal_index += 1
        identities.append(
            Q2ProposalIdentity(
                proposal_index=proposal_index,
                proposal_kind="artifact",
                artifact_type=artifact.artifact_type,
                value=artifact.value,
            )
        )
    for rule in output.rules:
        proposal_index += 1
        identities.append(
            Q2ProposalIdentity(
                proposal_index=proposal_index,
                proposal_kind="rule",
                artifact_type=rule.rule_type.value,
                value=rule.body,
            )
        )
    return tuple(identities)


@dataclass(frozen=True, slots=True)
class SourceEvidenceResult:
    """Filtered Q2 output plus deterministic local-gate diagnostics."""

    output: Q2SourceOutput
    warnings: tuple[str, ...]
    rejections: tuple[SourceEvidenceRejection, ...]

    @property
    def filtered_output(self) -> Q2SourceOutput:
        return self.output

    @property
    def rejected(self) -> tuple[SourceEvidenceRejection, ...]:
        return self.rejections


def verify_ioc_rules_output_against_source(
    output: Q2SourceOutput,
    source_text: str | SourceEvidenceDocument,
) -> SourceEvidenceResult:
    """Keep only IOC/rule proposals with literal proof in ``source_text``.

    Facts and events are outside the IOC_RULES contract and are always
    dropped.  Every surviving proposal keeps its exact value or rule body,
    loses its model-supplied context and carries its local evidence quote.
    """
    return _verify_output_against_source(output, source_text, preserve_narrative=False)


def verify_q2_output_against_source(
    output: Q2SourceOutput,
    source_text: str | SourceEvidenceDocument,
) -> SourceEvidenceResult:
    """Gate archived Q2 output against the exact archived document.

    Every fact, event, artifact and rule must be proven locally and carries
    its local evidence quote; a dated event must state its date inside the
    same evidence area.  Surviving artifacts and rules keep their exact value
    or body while model-supplied context is removed.
    """
    return _verify_output_against_source(output, source_text, preserve_narrative=True)


def _evidence_missing_reason(document: SourceEvidenceDocument, code: str) -> str:
    return "source_evidence_not_text_verifiable" if document.has_unverifiable_visuals else code


#: Upper bound of one canonical evidence quote.  A longer structural area is
#: cut to a deterministic window around the proven value.
MAX_EVIDENCE_QUOTE_CHARS = 1_000
_QUOTE_WINDOW_CHARS = 240

_MONTHS_EN = (
    "january",
    "february",
    "march",
    "april",
    "may",
    "june",
    "july",
    "august",
    "september",
    "october",
    "november",
    "december",
)
_MONTHS_FR = (
    "janvier",
    "février",
    "mars",
    "avril",
    "mai",
    "juin",
    "juillet",
    "août",
    "septembre",
    "octobre",
    "novembre",
    "décembre",
)


def _date_renderings(value: date) -> tuple[str, ...]:
    """The published spellings that state exactly one calendar date."""
    day, month, year = value.day, value.month, value.year
    days = {str(day), f"{day:02d}"}
    english = {_MONTHS_EN[month - 1], _MONTHS_EN[month - 1][:3]}
    if month == 9:
        english.add("sept")
    ordinal = {1: "st", 2: "nd", 3: "rd", 21: "st", 22: "nd", 23: "rd", 31: "st"}.get(day, "th")
    renderings = {
        value.isoformat(),
        f"{year}/{month:02d}/{day:02d}",
        f"{day:02d}/{month:02d}/{year}",
        f"{month:02d}/{day:02d}/{year}",
        f"{day:02d}.{month:02d}.{year}",
        f"{day:02d}-{month:02d}-{year}",
    }
    for name in english:
        for rendered_day in (*days, f"{day}{ordinal}"):
            renderings.add(f"{rendered_day} {name} {year}")
            renderings.add(f"{rendered_day} {name}, {year}")
            renderings.add(f"{name} {rendered_day}, {year}")
            renderings.add(f"{name} {rendered_day} {year}")
            renderings.add(f"{name}. {rendered_day}, {year}")
    french_days = {*days, "1er"} if day == 1 else days
    for rendered_day in french_days:
        renderings.add(f"{rendered_day} {_MONTHS_FR[month - 1]} {year}")
    return tuple(sorted(renderings))


def _date_is_stated(area: str, value: date) -> bool:
    folded = area.lower()
    return any(rendering in folded for rendering in _date_renderings(value))


def _find(view: str, needle: str) -> int:
    """Case-insensitive search that never shifts the quoted positions."""
    folded_view, folded_needle = view.lower(), needle.lower()
    if len(folded_view) == len(view) and len(folded_needle) == len(needle):
        return folded_view.find(folded_needle)
    return view.find(needle)


def _quote_around(view: str, start: int, length: int) -> str:
    if len(view) <= MAX_EVIDENCE_QUOTE_CHARS:
        return view
    window_start = max(0, start - _QUOTE_WINDOW_CHARS)
    window_end = min(len(view), start + length + _QUOTE_WINDOW_CHARS)
    return view[window_start:window_end][:MAX_EVIDENCE_QUOTE_CHARS].strip()


def _evidence_areas(document: SourceEvidenceDocument) -> tuple[str, ...]:
    """Structural areas first; whole views only when no span exists."""
    areas = tuple(
        span.text
        for span in document.spans
        if span.text and span.kind is not SourceEvidenceSpanKind.VISUAL_UNLOCATED
    )
    if areas:
        return areas
    return tuple(view for view in (document.parsed_text, document.decoded_source_view) if view)


def locate_text_evidence(
    document: SourceEvidenceDocument,
    anchor: str,
    *,
    stated_date: date | None = None,
) -> str | None:
    """Return the local quote proving ``anchor``, or ``None``.

    The anchor must appear, whitespace-collapsed and case-insensitively, in one
    structural area of the exact archived document.  When ``stated_date`` is
    given, the same area must also state that calendar date, so a date is never
    borrowed from an unrelated part of the publication.  An undated anchor
    wrapped across two structural areas is still found in the whole views.
    """
    needle = _text_comparison_view(anchor)
    if not needle:
        return None
    for area in _evidence_areas(document):
        view = _text_comparison_view(area)
        position = _find(view, needle)
        if position < 0:
            continue
        if stated_date is not None and not _date_is_stated(view, stated_date):
            continue
        return _quote_around(view, position, len(needle))
    if stated_date is not None:
        return None
    for whole in (document.parsed_text, document.decoded_source_view):
        view = _text_comparison_view(whole)
        position = _find(view, needle)
        if position >= 0:
            return _quote_around(view, position, len(needle))
    return None


def _first_located(
    document: SourceEvidenceDocument,
    anchors: tuple[str, ...],
    *,
    stated_date: date | None = None,
) -> str | None:
    for anchor in anchors:
        quote = locate_text_evidence(document, anchor, stated_date=stated_date)
        if quote is not None:
            return quote
    return None


def _artifact_quote(artifact: Q2ArtifactProposal, document: SourceEvidenceDocument) -> str:
    """Quote the structural area of a proven artifact, else its literal value."""
    candidate = _artifact_comparison_view(artifact.value)
    for span in source_evidence_context_for_artifact(artifact, document):
        view = _text_comparison_view(_artifact_comparison_view(span.text))
        position = _find(view, candidate)
        return _quote_around(view, max(position, 0), len(candidate))
    return candidate[:MAX_EVIDENCE_QUOTE_CHARS]


def _rule_quote(rule: Q2RuleProposal, document: SourceEvidenceDocument) -> str:
    body = _text_comparison_view(rule.body)
    spans = source_evidence_context_for_rule(rule, document)
    area = _text_comparison_view(spans[0].text) if spans else body
    return _quote_around(area, max(_find(area, body), 0), len(body))


def _verify_output_against_source(
    output: Q2SourceOutput,
    source_text: str | SourceEvidenceDocument,
    *,
    preserve_narrative: bool,
) -> SourceEvidenceResult:
    """Apply the shared source-local evidence gate to every proposal kind.

    Every surviving proposal carries, in ``evidence_quote``, the deterministic
    quote of the exact archived document that proves it; a model-supplied quote
    is only ever an anchor to locate, never evidence by itself.
    """
    evidence_document = (
        source_text
        if isinstance(source_text, SourceEvidenceDocument)
        else SourceEvidenceDocument(parsed_text=source_text)
    )
    base_source_views = tuple(
        _artifact_comparison_view(value)
        for value in (evidence_document.parsed_text, evidence_document.decoded_source_view)
        if value
    )
    # Un IOC publié dans une cellule de tableau est souvent replié : le rendu
    # texte insère un saut de ligne ou une suite d'espaces au milieu du token.
    # La vue compactée retire seulement ces coupures, sans rien réécrire.
    unwrapped_source_views = tuple(_artifact_unwrapped_view(value) for value in base_source_views)
    text_source_views = tuple(
        _text_comparison_view(value)
        for value in (evidence_document.parsed_text, evidence_document.decoded_source_view)
        if value
    )
    facts: list[Q2FactProposal] = []
    events: list[Q2EventProposal] = []
    artifacts: list[Q2ArtifactProposal] = []
    rules: list[Q2RuleProposal] = []
    warnings: list[str] = []
    rejections: list[SourceEvidenceRejection] = []

    if output.facts and not preserve_narrative:
        warnings.append("fact_not_allowed")
    if output.events and not preserve_narrative:
        warnings.append("event_not_allowed")

    proposal_index = 0

    for fact in output.facts:
        proposal_index += 1
        if not preserve_narrative:
            continue
        quote = _first_located(evidence_document, (fact.evidence_quote, fact.value))
        if quote is not None:
            facts.append(fact.model_copy(update={"evidence_quote": quote}))
            continue
        rejections.append(
            SourceEvidenceRejection(
                proposal_index=proposal_index,
                proposal_kind="fact",
                reason_code=_evidence_missing_reason(
                    evidence_document, "source_fact_evidence_missing"
                ),
                value=fact.value,
            )
        )

    for event in output.events:
        proposal_index += 1
        if not preserve_narrative:
            continue
        quote = _first_located(
            evidence_document,
            (event.evidence_quote, event.text),
            stated_date=event.event_date,
        )
        if quote is not None:
            events.append(event.model_copy(update={"evidence_quote": quote}))
            continue
        rejections.append(
            SourceEvidenceRejection(
                proposal_index=proposal_index,
                proposal_kind="event",
                reason_code=_evidence_missing_reason(
                    evidence_document, "source_event_evidence_missing"
                ),
                value=event.text,
            )
        )

    for artifact in output.artifacts:
        proposal_index += 1
        proven = any(_artifact_is_proven(artifact, source) for source in base_source_views)
        if not proven and any(
            _artifact_is_proven(artifact, source) for source in unwrapped_source_views
        ):
            warnings.append("artifact_proven_after_unwrap")
            proven = True
        if proven:
            artifacts.append(
                artifact.model_copy(
                    update={
                        "context": "",
                        "evidence_quote": _artifact_quote(artifact, evidence_document),
                    }
                )
            )
            continue
        rejections.append(
            SourceEvidenceRejection(
                proposal_index=proposal_index,
                proposal_kind="artifact",
                reason_code=_evidence_missing_reason(evidence_document, "source_evidence_missing"),
                value=artifact.value,
                artifact_type=artifact.artifact_type,
            )
        )

    for rule in output.rules:
        proposal_index += 1
        body_view = _text_comparison_view(rule.body)
        if body_view and any(body_view in source for source in text_source_views):
            rules.append(
                rule.model_copy(
                    update={"context": "", "evidence_quote": _rule_quote(rule, evidence_document)}
                )
            )
            continue
        rejections.append(
            SourceEvidenceRejection(
                proposal_index=proposal_index,
                proposal_kind="rule",
                reason_code="source_rule_evidence_missing",
                value=rule.body,
                artifact_type=rule.rule_type.value,
            )
        )

    filtered = Q2SourceOutput(
        facts=facts,
        events=events,
        artifacts=artifacts,
        rules=rules,
        uncertainties=list(output.uncertainties),
    )
    return SourceEvidenceResult(
        output=filtered,
        warnings=tuple(dict.fromkeys(warnings)),
        rejections=tuple(rejections),
    )


def _artifact_comparison_view(value: str) -> str:
    """Apply only the transport and CTI refanging allowed by this gate."""
    view = value.replace("\r\n", "\n").replace("\r", "\n")
    view = view.replace(_NBSP, " ").replace(_NARROW_NBSP, " ")
    view = view.translate(_INVISIBLE)
    view = view.replace(r"\:", ":")
    view = _DOT.sub(".", view)
    view = _COLON.sub(":", view)
    view = _AT.sub("@", view)
    view = _DEFANGED_SCHEME.sub(
        lambda match: "https://" if match.group("secure") else "http://",
        view,
    )
    return view


_ARTIFACT_WRAP = re.compile(r"[ \t]*\n[ \t]*")


def _artifact_unwrapped_view(view: str) -> str:
    """Remove renderer line wrapping inside an indicator token.

    Only whitespace surrounding a newline is removed. No character of the
    indicator is added, removed or rewritten, so a value proven in this view
    is still literally present in the source.
    """
    return _ARTIFACT_WRAP.sub("", view)


def _rule_comparison_view(value: str) -> str:
    """Rules permit line-ending normalization and nothing else."""
    return value.replace("\r\n", "\n").replace("\r", "\n")


_RULE_WHITESPACE = re.compile(r"\s+")


def _rule_whitespace_view(value: str) -> str:
    """Collapse every run of whitespace to one space.

    A rule published in a blog is re-indented, wrapped and decorated by the
    renderer. Requiring a byte-identical body means no rule is ever published:
    the pipeline emitted zero rules across twenty consecutive runs. Collapsing
    whitespace keeps every token and their order, so the rule is still proven
    in its own source.
    """
    return _RULE_WHITESPACE.sub(" ", _rule_comparison_view(value)).strip()


def _text_comparison_view(value: str) -> str:
    """Whitespace-collapsed view shared by facts, events and rule bodies.

    A publication is re-indented and wrapped by its renderer.  Collapsing
    whitespace keeps every token and their order, so a value proven in this
    view is still literally present in the deterministic source representation.
    """
    return _rule_whitespace_view(value)


def _artifact_is_proven(artifact: Q2ArtifactProposal, source: str) -> bool:
    try:
        artifact_type = ArtifactType(artifact.artifact_type)
    except ValueError:
        return False

    candidate = _artifact_comparison_view(artifact.value)
    if artifact_type is ArtifactType.DOMAIN:
        return _contains_domain(source, candidate.casefold())
    if artifact_type in {ArtifactType.HASH, ArtifactType.CVE}:
        return _contains_bounded(
            source,
            candidate.casefold(),
            _continuation_for(artifact_type),
            casefold_source=True,
        )
    if artifact_type is ArtifactType.IP:
        return _contains_ip(source, candidate)
    if artifact_type is ArtifactType.EMAIL:
        return _contains_email(source, candidate)
    if artifact_type is ArtifactType.URL:
        return _contains_url(source, candidate)
    return _contains_bounded(source, candidate, _continuation_for(artifact_type))


def source_evidence_context_for_artifact(
    artifact: Q2ArtifactProposal,
    source_text: str | SourceEvidenceDocument,
) -> tuple[SourceEvidenceSpan, ...]:
    """Return structural spans that can explain a source-gated artifact.

    This is intentionally a locator, not a second verifier: callers must use
    ``verify_*_output_against_source`` for the decision.  Visual spans are
    never returned as a located match or used as proof of an IOC.
    """
    document = (
        source_text
        if isinstance(source_text, SourceEvidenceDocument)
        else SourceEvidenceDocument(parsed_text=source_text)
    )
    matches: list[SourceEvidenceSpan] = []
    for span in document.spans:
        if span.kind is SourceEvidenceSpanKind.VISUAL_UNLOCATED or not span.text:
            continue
        comparison = _artifact_comparison_view(span.text)
        if _artifact_is_proven(artifact, comparison) or _artifact_is_proven(
            artifact, _artifact_unwrapped_view(comparison)
        ):
            matches.append(span)
    return tuple(matches)


def source_evidence_context_for_rule(
    rule: Q2RuleProposal,
    source_text: str | SourceEvidenceDocument,
) -> tuple[SourceEvidenceSpan, ...]:
    """Return structural spans containing the exact rule gate view."""
    document = (
        source_text
        if isinstance(source_text, SourceEvidenceDocument)
        else SourceEvidenceDocument(parsed_text=source_text)
    )
    candidate = _rule_whitespace_view(rule.body)
    if not candidate:
        return ()
    return tuple(
        span
        for span in document.spans
        if span.text and candidate in _rule_whitespace_view(span.text)
    )


def _continuation_for(artifact_type: ArtifactType) -> str:
    if artifact_type is ArtifactType.DOMAIN:
        return _DOMAIN_CONTINUATION
    if artifact_type is ArtifactType.HASH:
        return _HASH_CONTINUATION
    if artifact_type is ArtifactType.CVE:
        return _CVE_CONTINUATION
    if artifact_type is ArtifactType.IP:
        return _IP_CONTINUATION
    if artifact_type is ArtifactType.URL:
        return _URL_CONTINUATION
    if artifact_type is ArtifactType.EMAIL:
        return _EMAIL_CONTINUATION
    if artifact_type is ArtifactType.FILENAME:
        return _FILENAME_CONTINUATION
    if artifact_type is ArtifactType.FILEPATH:
        return _FILEPATH_CONTINUATION
    return ""


def _contains_email(source: str, candidate: str) -> bool:
    local, separator, domain = candidate.rpartition("@")
    if not separator:
        return _contains_bounded(source, candidate, _EMAIL_CONTINUATION)

    prefix = f"{local}@"
    start = source.find(prefix)
    while start >= 0:
        end = start + len(candidate)
        source_domain = source[start + len(prefix) : end]
        if source_domain.casefold() == domain.casefold() and _has_email_boundaries(
            source, start, end
        ):
            return True
        start = source.find(prefix, start + 1)
    return False


def _contains_domain(source: str, candidate: str) -> bool:
    comparable_source = source.casefold()
    start = comparable_source.find(candidate)
    while start >= 0:
        end = start + len(candidate)
        if _has_domain_boundaries(comparable_source, start, end):
            return True
        start = comparable_source.find(candidate, start + 1)
    return False


def _contains_ip(source: str, candidate: str) -> bool:
    start = source.find(candidate)
    while start >= 0:
        end = start + len(candidate)
        if _has_ip_boundaries(source, candidate, start, end):
            return True
        start = source.find(candidate, start + 1)
    return False


def _contains_url(source: str, candidate: str) -> bool:
    start = source.find(candidate)
    while start >= 0:
        end = start + len(candidate)
        if _has_url_boundaries(source, start, end):
            return True
        start = source.find(candidate, start + 1)
    return False


def _contains_bounded(
    source: str,
    candidate: str,
    continuation: str,
    *,
    casefold_source: bool = False,
) -> bool:
    if not candidate:
        return False
    comparable_source = source.casefold() if casefold_source else source
    start = comparable_source.find(candidate)
    while start >= 0:
        end = start + len(candidate)
        if _has_token_boundaries(comparable_source, start, end, continuation):
            return True
        start = comparable_source.find(candidate, start + 1)
    return False


def _has_token_boundaries(source: str, start: int, end: int, continuation: str) -> bool:
    before = source[start - 1] if start else ""
    after = source[end] if end < len(source) else ""
    return not _is_continuation(before, continuation) and not _is_continuation(after, continuation)


def _has_domain_boundaries(source: str, start: int, end: int) -> bool:
    before = source[start - 1] if start else ""
    after = source[end] if end < len(source) else ""
    if before and (before.isalnum() or before in _DOMAIN_CONTINUATION):
        return False
    if after and (after.isalnum() or after in "-_"):
        return False
    # A dot followed by another domain label extends the indicator. A final
    # dot followed by prose punctuation/whitespace is sentence punctuation.
    return not (
        after == "."
        and end + 1 < len(source)
        and (source[end + 1].isalnum() or source[end + 1] in "-_")
    )


def _has_ip_boundaries(source: str, candidate: str, start: int, end: int) -> bool:
    before = source[start - 1] if start else ""
    after = source[end] if end < len(source) else ""
    if ":" in candidate:
        return not _is_continuation(before, _IP_CONTINUATION) and not _is_continuation(
            after, _IP_CONTINUATION
        )
    if before and (before.isalnum() or before == "."):
        return False
    if after and (after.isalnum() or after == "."):
        if after != "." or (end + 1 < len(source) and source[end + 1].isdigit()):
            return False
    return True


def _has_email_boundaries(source: str, start: int, end: int) -> bool:
    before = source[start - 1] if start else ""
    after = source[end] if end < len(source) else ""
    if _is_continuation(before, _EMAIL_CONTINUATION):
        return False
    if after == ".":
        return not (end + 1 < len(source) and source[end + 1].isalnum())
    return not _is_continuation(after, _EMAIL_CONTINUATION)


def _has_url_boundaries(source: str, start: int, end: int) -> bool:
    before = source[start - 1] if start else ""
    after = source[end] if end < len(source) else ""
    if _is_continuation(before, _URL_CONTINUATION):
        return False
    if after in ".,;:!?":
        next_value = source[end + 1] if end + 1 < len(source) else ""
        return not next_value or next_value.isspace() or next_value in ")]}>"
    return not _is_continuation(after, _URL_CONTINUATION)


def _is_continuation(value: str, continuation: str) -> bool:
    return bool(value) and (value.isalnum() or value in continuation)
