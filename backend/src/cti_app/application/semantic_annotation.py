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
from cti_app.domain.publication import ArtifactType, RichSpan, RichSpanKind, RichText

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
        r"[\s_-]+" if re.fullmatch(r"[\s_-]+", piece) else re.escape(piece)
        for piece in pieces
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
                candidate.start < other.end and candidate.end > other.start
                for other in selected
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
