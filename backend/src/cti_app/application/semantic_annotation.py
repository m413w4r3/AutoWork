"""Conservative lexicon-based semantic annotation for publication."""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from cti_app.application.production_normalization import display_indicator_value
from cti_app.application.production_parsers import (
    DisplayPolicy,
    IndicatorStatus,
    SemanticType,
    TechnicalExtraction,
)
from cti_app.domain.production import ProductionEvidenceBasis
from cti_app.domain.production_extraction import (
    ExtractionIndicatorStatus,
    ProductionExtractionV1,
)
from cti_app.domain.publication import ArtifactType, RichSpan, RichSpanKind, RichText
from cti_app.domain.semantic_annotation import (
    SEMANTIC_ROLE_PRIORITY,
    SemanticAnnotationProposalV1,
    SemanticParagraphV1,
    SemanticRole,
    SemanticTextSpanV1,
)

SEMANTIC_ANNOTATOR_VERSION = "2-document-lexicon-technical-literals"


@dataclass(frozen=True)
class TextSpan:
    start: int
    end: int


class ForeignTermDetector(Protocol):
    def spans(self, text: str) -> Sequence[TextSpan]: ...


class EnglishTermDetector:
    """Find exact editorial English terms, including multi-word expressions."""

    def __init__(self, terms: Sequence[str] | None = None) -> None:
        if terms is None:
            path = Path(__file__).parent.parent / "resources" / "editorial_english_terms.txt"
            terms = tuple(
                line.strip()
                for line in path.read_text(encoding="utf-8").splitlines()
                if line.strip() and not line.lstrip().startswith("#")
            )
        self._terms = tuple(sorted(set(terms), key=len, reverse=True))

    def spans(self, text: str) -> Sequence[TextSpan]:
        found: list[TextSpan] = []
        occupied: set[int] = set()
        for term in self._terms:
            expression = _term_pattern(term)
            for match in expression.finditer(text):
                if any(index in occupied for index in range(match.start(), match.end())):
                    continue
                found.append(TextSpan(match.start(), match.end()))
                occupied.update(range(match.start(), match.end()))
        return tuple(sorted(found, key=lambda span: span.start))


_CITATION = re.compile(r"\[(S\d{1,3})\]", re.IGNORECASE)


def _term_pattern(term: str) -> re.Pattern[str]:
    pieces = re.split(r"([\s_-]+)", term.strip())
    pattern = "".join(
        r"[\s_-]+" if re.fullmatch(r"[\s_-]+", piece) else re.escape(piece) for piece in pieces
    )
    return re.compile(rf"(?<!\w){pattern}(?!\w)", re.IGNORECASE)


@dataclass(frozen=True)
class _Candidate:
    start: int
    end: int
    kind: RichSpanKind
    priority: int
    source_ids: tuple[str, ...] = ()
    replacement: str | None = None


@dataclass(frozen=True, slots=True)
class _RoleCandidate:
    start: int
    end: int
    role: SemanticRole


_FACT_ROLE: dict[str, SemanticRole] = {
    "actors": SemanticRole.ACTOR,
    "campaigns": SemanticRole.CAMPAIGN,
    "malware": SemanticRole.MALWARE,
    "tools": SemanticRole.TOOL,
    "products": SemanticRole.PRODUCT,
    "protocols": SemanticRole.PROTOCOL_FIELD,
    "commands": SemanticRole.COMMAND,
    "files": SemanticRole.PATH,
    "infection_chain": SemanticRole.TECHNICAL,
    "ttps": SemanticRole.TECHNICAL,
    "infrastructure": SemanticRole.TECHNICAL,
    "persistence": SemanticRole.TECHNICAL,
    "detections": SemanticRole.TECHNICAL,
    "other_technical": SemanticRole.TECHNICAL,
}
_PORT_LITERAL = re.compile(
    r"\b(?:port\s*(?:[:=]\s*)?(\d{1,5})|(?:tcp|udp)/(\d{1,5})|"
    r"(\d{1,5})/(?:tcp|udp))\b",
    re.IGNORECASE,
)
_PARAMETER_LITERAL = re.compile(r"(?<![\w.-])[A-Za-z_][A-Za-z0-9_.-]*=[^\s,;]+")
_NAME_SENTENCE_PUNCTUATION = re.compile(r"[!?;:]|,(?=\s)|\.(?=\s|$)")
_FINITE_VERB_PHRASE = re.compile(
    r"\b(?:est|sont|était|étaient|a|ont|utilise|utilisent|utilisé|utilisée|"
    r"décrit|décrite|décrivent|mentionne|mentionnent|stocke|stockent|permet|"
    r"permettent|contient|contiennent|correspond|correspondent|déploie|déploient)\b",
    re.IGNORECASE,
)
_CVE_LITERAL = re.compile(r"(?<![\w-])CVE-\d{4}-\d{4,}(?![\w-])", re.IGNORECASE)
_ATTACK_LITERAL = re.compile(
    r"(?<![\w])(?:MITRE\s+ATT&CK\s+)?T\d{4}(?:\.\d{3})?(?![\w]|\.\d)", re.IGNORECASE
)
_IPV4_CANDIDATE = re.compile(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?!\w|\.\d)")
_IPV6_CANDIDATE = re.compile(
    r"(?<![\w:])(?:[0-9A-Fa-f]{0,4}:){2,}"
    r"(?:[0-9A-Fa-f]{0,4}|(?:\d{1,3}\.){3}\d{1,3})(?![\w:])"
)
_HASH_LITERAL = re.compile(
    r"(?<![A-Fa-f0-9])(?:[A-Fa-f0-9]{32}|[A-Fa-f0-9]{40}|[A-Fa-f0-9]{64})"
    r"(?![A-Fa-f0-9])"
)
_PORT_SYNTAX = re.compile(
    r"(?<![\w/])(?:port\s*(?:[:=]\s*)?\d{1,5}|(?:tcp|udp)/\d{1,5}|"
    r"\d{1,5}/(?:tcp|udp))(?![\w])",
    re.IGNORECASE,
)
_FILE_EXTENSION = (
    "exe|dll|sys|ps1|bat|cmd|vbs|js|hta|py|sh|bin|dat|json|conf|txt|zip|"
    "doc|docx|pdf|lnk|so|dylib|msi|scr|jar|class|elf|ocx|tmp"
)
_FILE_PATH = re.compile(
    rf"(?<![\w.-])(?:[A-Za-z]:[\\/]|/|\.{{1,2}}[\\/])"
    rf"(?:[A-Za-z0-9_. -]+[\\/])*[A-Za-z0-9_. -]+\.(?:{_FILE_EXTENSION})\b"
    rf"|(?<![\w.-])[A-Za-z0-9_.-]+\.(?:{_FILE_EXTENSION})\b",
    re.IGNORECASE,
)
_BACKTICKED = re.compile(r"`([^`\n]+)`")
_COMMAND_HEAD = re.compile(
    r"^(?:\$|\.\.?[\\/]|(?:sudo|cmd(?:\.exe)?|powershell(?:\.exe)?|pwsh|bash|sh|"
    r"curl|wget|python(?:[0-9.]*)?|perl|ruby|wscript|cscript|mshta|rundll32|"
    r"regsvr32|certutil|bitsadmin|wmic|schtasks|sc|net|whoami|ipconfig|ping|"
    r"chmod|nc|ncat|ssh|tar)(?:\s|$))",
    re.IGNORECASE,
)
_UPPER_TECHNICAL_IDENTIFIER = re.compile(r"(?<![\w])(?:[A-Z][A-Z0-9]*)(?:[_-][A-Z0-9]+)+(?![\w])")


def _name_like_fact_value(value: str) -> bool:
    """Keep only short entity-like fact values; never turn prose into lexicon terms."""
    tokens = value.split()
    return (
        1 <= len(tokens) <= 5
        and _NAME_SENTENCE_PUNCTUATION.search(value) is None
        and _FINITE_VERB_PHRASE.search(value) is None
    )


def _valid_ip_spans(text: str) -> tuple[TextSpan, ...]:
    spans: list[TextSpan] = []
    for expression in (_IPV4_CANDIDATE, _IPV6_CANDIDATE):
        for match in expression.finditer(text):
            try:
                ipaddress.ip_address(match.group())
            except ValueError:
                continue
            spans.append(TextSpan(match.start(), match.end()))
    return tuple(spans)


def _looks_like_backticked_command(value: str) -> bool:
    command = value.strip()
    if not command or "\n" in command:
        return False
    if _COMMAND_HEAD.match(command) is not None:
        return True
    # A quoted executable path with arguments is command syntax; a single
    # backticked noun or identifier remains ordinary text.
    return bool(_FILE_PATH.match(command) and re.search(r"\s+[/\-]", command))


def _technical_literal_spans(text: str) -> tuple[tuple[TextSpan, SemanticRole], ...]:
    """Find conservative, explicitly formatted technical literals in prose."""
    found: list[tuple[TextSpan, SemanticRole]] = []

    def add(expression: re.Pattern[str], role: SemanticRole) -> None:
        found.extend(
            (TextSpan(match.start(), match.end()), role) for match in expression.finditer(text)
        )

    add(_CVE_LITERAL, SemanticRole.TECHNICAL_LITERAL)
    add(_ATTACK_LITERAL, SemanticRole.TECHNICAL_LITERAL)
    found.extend((span, SemanticRole.IOC) for span in _valid_ip_spans(text))
    add(_HASH_LITERAL, SemanticRole.IOC)
    for match in _PORT_SYNTAX.finditer(text):
        port = re.search(r"\d{1,5}", match.group())
        if port is not None and int(port.group()) <= 65535:
            start = match.start() + port.start()
            found.append(
                (
                    TextSpan(start, start + len(port.group())),
                    SemanticRole.TECHNICAL_LITERAL,
                )
            )
    add(_FILE_PATH, SemanticRole.PATH)
    for match in _BACKTICKED.finditer(text):
        if _looks_like_backticked_command(match.group(1)):
            found.append((TextSpan(match.start(), match.end()), SemanticRole.COMMAND))
    add(_UPPER_TECHNICAL_IDENTIFIER, SemanticRole.TECHNICAL_LITERAL)
    return tuple(found)


def _exact_word_matches(text: str, term: str) -> tuple[TextSpan, ...]:
    """Return case-sensitive exact occurrences whose outer word edges are bounded."""
    spans: list[TextSpan] = []
    start = 0
    while True:
        start = text.find(term, start)
        if start < 0:
            break
        end = start + len(term)
        left_word = term[0].isalnum() or term[0] == "_"
        right_word = term[-1].isalnum() or term[-1] == "_"
        if not (
            left_word and start > 0 and (text[start - 1].isalnum() or text[start - 1] == "_")
        ) and not (right_word and end < len(text) and (text[end].isalnum() or text[end] == "_")):
            spans.append(TextSpan(start, end))
        start += 1
    return tuple(spans)


def _technical_literal_values(value: str) -> tuple[str, ...]:
    """Recognize only explicit port and parameter syntax in technical facts."""
    literals: list[str] = []
    for match in _PORT_LITERAL.finditer(value):
        port = next((group for group in match.groups() if group is not None), None)
        if port is not None and int(port) <= 65535:
            literals.append(port)
    for match in _PARAMETER_LITERAL.finditer(value):
        literal = match.group().rstrip(".,;:!?)]}")
        if literal:
            literals.append(literal)
    return tuple(dict.fromkeys(literals))


def semantic_entities_from_extraction(
    extraction: ProductionExtractionV1,
) -> tuple[tuple[SemanticRole, str], ...]:
    """Return only source-verified typed entity and literal terms."""
    terms: list[tuple[SemanticRole, str]] = []
    for source in extraction.sources:
        for fact in source.facts:
            role = _FACT_ROLE.get(fact.category)
            if role is not None and fact.evidence_basis is ProductionEvidenceBasis.SOURCE_VERIFIED:
                if _name_like_fact_value(fact.value):
                    terms.append((role, fact.value))
                if fact.category == "other_technical":
                    terms.extend(
                        (SemanticRole.TECHNICAL_LITERAL, value)
                        for value in _technical_literal_values(fact.value)
                    )
        for indicator in source.indicators:
            if indicator.artifact_type in {ArtifactType.FILEPATH, ArtifactType.FILENAME}:
                terms.append((SemanticRole.PATH, indicator.value))
            elif (
                indicator.indicator_status is ExtractionIndicatorStatus.CONFIRMED_IOC
                and indicator.artifact_type
                in {
                    ArtifactType.IP,
                    ArtifactType.DOMAIN,
                    ArtifactType.URL,
                    ArtifactType.EMAIL,
                    ArtifactType.HASH,
                }
            ):
                terms.append((SemanticRole.IOC, indicator.value))
    return tuple(dict.fromkeys((role, value) for role, value in terms if value.strip()))


_KIND_FOR_SEMANTIC = {
    SemanticType.ACTOR: RichSpanKind.ACTOR,
    SemanticType.MALWARE: RichSpanKind.MALWARE,
    SemanticType.TOOL: RichSpanKind.TOOL,
    SemanticType.PRODUCT: RichSpanKind.PRODUCT,
    SemanticType.TECHNIQUE: RichSpanKind.TECHNICAL,
    SemanticType.PROTOCOL: RichSpanKind.TECHNICAL,
}

_PRIORITY = {
    RichSpanKind.CITATION: 70,
    RichSpanKind.CODE: 60,
    RichSpanKind.IOC: 50,
    RichSpanKind.TECHNICAL: 40,
    RichSpanKind.ACTOR: 30,
    RichSpanKind.MALWARE: 30,
    RichSpanKind.TOOL: 30,
    RichSpanKind.PRODUCT: 30,
    RichSpanKind.EMPHASIS: 20,
}


class SemanticAnnotator:
    def __init__(self, foreign_terms: ForeignTermDetector | None = None) -> None:
        self._foreign_terms = foreign_terms or EnglishTermDetector()

    def annotate(self, text: str, extraction: TechnicalExtraction) -> RichText:
        candidates: list[_Candidate] = []
        for match in _CITATION.finditer(text):
            candidates.append(
                _Candidate(
                    match.start(),
                    match.end(),
                    RichSpanKind.CITATION,
                    _PRIORITY[RichSpanKind.CITATION],
                    (match.group(1).upper(),),
                    "",
                )
            )

        for item in extraction.items:
            kind = _KIND_FOR_SEMANTIC.get(item.semantic_type)
            replacement = None
            match_values = [item.value]
            if (
                item.semantic_type is SemanticType.INDICATOR
                and item.indicator_status is IndicatorStatus.CONFIRMED_IOC
                and item.artifact_type is not None
                and item.display_policy is DisplayPolicy.BOTH
            ):
                kind = RichSpanKind.IOC
                artifact_type = (
                    item.artifact_type
                    if isinstance(item.artifact_type, ArtifactType)
                    else ArtifactType(item.artifact_type)
                )
                replacement = display_indicator_value(item.value, artifact_type, defanged=True)
                match_values.extend(
                    (
                        display_indicator_value(item.value, artifact_type, defanged=False),
                        replacement,
                    )
                )
            if kind is None and item.category in {"commands", "other_technical"}:
                kind = RichSpanKind.TECHNICAL
            if kind is None or not item.value.strip():
                continue
            aliases = [
                part.strip()
                for value in dict.fromkeys(match_values)
                for part in re.split(r"\s*/\s*", value)
                if part.strip()
            ]
            for alias in aliases:
                for match in _term_pattern(alias).finditer(text):
                    candidates.append(
                        _Candidate(
                            match.start(),
                            match.end(),
                            kind,
                            _PRIORITY[kind],
                            replacement=replacement,
                        )
                    )

        for span in self._foreign_terms.spans(text):
            candidates.append(
                _Candidate(
                    span.start,
                    span.end,
                    RichSpanKind.EMPHASIS,
                    _PRIORITY[RichSpanKind.EMPHASIS],
                )
            )

        # Priority first, then longer values, then stable source position.
        selected: list[_Candidate] = []
        for candidate in sorted(
            candidates, key=lambda item: (-item.priority, -(item.end - item.start), item.start)
        ):
            if any(
                candidate.start < other.end and candidate.end > other.start for other in selected
            ):
                continue
            selected.append(candidate)
        selected.sort(key=lambda item: item.start)

        output: list[RichSpan] = []
        cursor = 0
        for candidate in selected:
            if cursor < candidate.start:
                output.append(RichSpan(RichSpanKind.TEXT, text[cursor : candidate.start]))
            output.append(
                RichSpan(
                    candidate.kind,
                    candidate.replacement
                    if candidate.replacement is not None
                    else text[candidate.start : candidate.end],
                    candidate.source_ids,
                )
            )
            cursor = candidate.end
        if cursor < len(text):
            output.append(RichSpan(RichSpanKind.TEXT, text[cursor:]))
        return tuple(output)

    def annotate_paragraph(
        self,
        *,
        anchor: str,
        text: str,
        entities: Sequence[tuple[SemanticRole, str]],
        proposals: Sequence[SemanticAnnotationProposalV1] = (),
    ) -> SemanticParagraphV1:
        """Build full-coverage spans without changing any source character."""
        candidates: list[_RoleCandidate] = []
        for match in _CITATION.finditer(text):
            candidates.append(_RoleCandidate(match.start(), match.end(), SemanticRole.SOURCE))

        for role, value in entities:
            if not value.strip() or role is SemanticRole.TEXT:
                continue
            # A slash surrounded by whitespace separates aliases. Keep slashes
            # inside paths and URLs so they can be annotated as full literals.
            for alias in (part.strip() for part in re.split(r"\s+/\s+", value)):
                if not alias:
                    continue
                for match in _term_pattern(alias).finditer(text):
                    candidates.append(_RoleCandidate(match.start(), match.end(), role))

        for span in self._foreign_terms.spans(text):
            candidates.append(_RoleCandidate(span.start, span.end, SemanticRole.ENGLISH_TERM))

        document_terms: dict[str, SemanticRole] = {}
        for proposal in proposals:
            current = document_terms.get(proposal.text)
            if (
                current is None
                or SEMANTIC_ROLE_PRIORITY[proposal.role] > SEMANTIC_ROLE_PRIORITY[current]
                or (
                    SEMANTIC_ROLE_PRIORITY[proposal.role] == SEMANTIC_ROLE_PRIORITY[current]
                    and proposal.role.value < current.value
                )
            ):
                document_terms[proposal.text] = proposal.role
        for term, role in document_terms.items():
            candidates.extend(
                _RoleCandidate(span.start, span.end, role)
                for span in _exact_word_matches(text, term)
            )

        candidates.extend(
            _RoleCandidate(span.start, span.end, role)
            for span, role in _technical_literal_spans(text)
        )

        selected: list[_RoleCandidate] = []
        for candidate in sorted(
            candidates,
            key=lambda item: (
                -SEMANTIC_ROLE_PRIORITY[item.role],
                -(item.end - item.start),
                item.start,
                item.role.value,
            ),
        ):
            if any(
                candidate.start < other.end and candidate.end > other.start for other in selected
            ):
                continue
            selected.append(candidate)
        selected.sort(key=lambda item: item.start)

        spans: list[SemanticTextSpanV1] = []
        cursor = 0
        for candidate in selected:
            if cursor < candidate.start:
                spans.append(SemanticTextSpanV1(SemanticRole.TEXT, text[cursor : candidate.start]))
            spans.append(SemanticTextSpanV1(candidate.role, text[candidate.start : candidate.end]))
            cursor = candidate.end
        if cursor < len(text):
            spans.append(SemanticTextSpanV1(SemanticRole.TEXT, text[cursor:]))
        if not spans:
            spans.append(SemanticTextSpanV1(SemanticRole.TEXT, text))
        return SemanticParagraphV1(anchor=anchor, spans=tuple(spans))
