"""Stable article/brief classification for frozen edition publications."""

from __future__ import annotations

import re
from dataclasses import dataclass

from cti_app.domain.edition_publication import EditionPublicationV2
from cti_app.domain.publication_document import CanonicalPublicationDocument
from cti_app.domain.publication_kind import PublicationKind

BRIEF_MAX_WORDS = 120
_WORD = re.compile(r"\b[\w]+(?:['\u2019-][\w]+)*\b", re.UNICODE)


@dataclass(frozen=True, slots=True)
class ClassifiedEditionPublication:
    publication: EditionPublicationV2
    kind: PublicationKind
    number: str
    anchor: str


def publication_word_count(document: CanonicalPublicationDocument) -> int:
    """Count words in the synthesis body, excluding references and IOC annexes."""
    text = " ".join(
        [paragraph.text for paragraph in document.lead]
        + [paragraph.text for section in document.sections for paragraph in section.paragraphs]
    )
    return len(_WORD.findall(text))


def explicit_publication_kind(
    document: CanonicalPublicationDocument,
) -> PublicationKind | None:
    """Return an explicit format when the publication schema carries one."""
    value = getattr(document, "publication_kind", None)
    if value is None:
        return None
    if isinstance(value, PublicationKind):
        return value
    try:
        return PublicationKind(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"unsupported explicit publication kind: {value!r}") from exc


def is_legacy_historical_brief(document: CanonicalPublicationDocument) -> bool:
    """Apply the pre-V7 length heuristic to documents without explicit format."""
    return not any(section.heading.strip() for section in document.sections) and (
        publication_word_count(document) <= BRIEF_MAX_WORDS
    )


def is_brief(document: CanonicalPublicationDocument) -> bool:
    """Return explicit format when present, otherwise use the historical heuristic."""
    kind = explicit_publication_kind(document)
    if kind is not None:
        return kind is PublicationKind.BRIEF
    return is_legacy_historical_brief(document)


def classify_edition_publications(
    publications: tuple[EditionPublicationV2, ...],
) -> tuple[ClassifiedEditionPublication, ...]:
    """Preserve position within each group; number articles first, then briefs."""
    ordered = tuple(sorted(publications, key=lambda item: item.position))
    articles: list[EditionPublicationV2] = []
    briefs: list[EditionPublicationV2] = []
    for item in ordered:
        kind = explicit_publication_kind(item.document)
        if kind is None:
            kind = (
                PublicationKind.BRIEF
                if is_legacy_historical_brief(item.document)
                else PublicationKind.ARTICLE
            )
        if kind is PublicationKind.ARTICLE:
            articles.append(item)
        else:
            briefs.append(item)
    number_width = max(2, len(str(len(ordered))))
    result: list[ClassifiedEditionPublication] = []
    for index, publication in enumerate(articles, start=1):
        number = f"{index:0{number_width}d}"
        result.append(
            ClassifiedEditionPublication(
                publication=publication,
                kind=PublicationKind.ARTICLE,
                number=number,
                anchor=f"article{number}",
            )
        )
    for index, publication in enumerate(briefs, start=len(articles) + 1):
        number = f"{index:0{number_width}d}"
        result.append(
            ClassifiedEditionPublication(
                publication=publication,
                kind=PublicationKind.BRIEF,
                number=number,
                anchor=f"breve{number}",
            )
        )
    return tuple(result)


__all__ = [
    "BRIEF_MAX_WORDS",
    "ClassifiedEditionPublication",
    "classify_edition_publications",
    "explicit_publication_kind",
    "is_brief",
    "is_legacy_historical_brief",
    "publication_word_count",
]
