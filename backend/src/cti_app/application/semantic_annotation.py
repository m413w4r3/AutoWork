"""Conservative lexicon-based semantic annotation for publication."""

from __future__ import annotations

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

SEMANTIC_ANNOTATOR_VERSION = "1"


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

        for proposal in proposals:
            if proposal.paragraph_anchor != anchor:
                continue
            start = 0
            while True:
                start = text.find(proposal.text, start)
                if start < 0:
                    break
                candidates.append(_RoleCandidate(start, start + len(proposal.text), proposal.role))
                start += 1

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
