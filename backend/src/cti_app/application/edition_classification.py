"""Stable article/brief classification for frozen edition publications."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from cti_app.domain.edition_publication import EditionPublicationV2
from cti_app.domain.publication_document import PublicationDocumentV4

BRIEF_MAX_WORDS = 120
_WORD = re.compile(r"\b[\w]+(?:['\u2019-][\w]+)*\b", re.UNICODE)


@dataclass(frozen=True, slots=True)
class ClassifiedEditionPublication:
    publication: EditionPublicationV2
    kind: Literal["article", "brief"]
    number: str
    anchor: str


def publication_word_count(document: PublicationDocumentV4) -> int:
    """Count words in the synthesis body, excluding references and IOC annexes."""
    text = " ".join(
        [paragraph.text for paragraph in document.lead]
        + [paragraph.text for section in document.sections for paragraph in section.paragraphs]
    )
    return len(_WORD.findall(text))


def is_brief(document: PublicationDocumentV4) -> bool:
    """A brief has no titled synthesis sections and contains at most 120 words."""
    return not any(section.heading.strip() for section in document.sections) and (
        publication_word_count(document) <= BRIEF_MAX_WORDS
    )


def classify_edition_publications(
    publications: tuple[EditionPublicationV2, ...],
) -> tuple[ClassifiedEditionPublication, ...]:
    """Preserve position within each group; number articles first, then briefs."""
    ordered = tuple(sorted(publications, key=lambda item: item.position))
    articles = [item for item in ordered if not is_brief(item.document)]
    briefs = [item for item in ordered if is_brief(item.document)]
    number_width = max(2, len(str(len(ordered))))
    result: list[ClassifiedEditionPublication] = []
    for index, publication in enumerate(articles, start=1):
        number = f"{index:0{number_width}d}"
        result.append(
            ClassifiedEditionPublication(
                publication=publication,
                kind="article",
                number=number,
                anchor=f"article{number}",
            )
        )
    for index, publication in enumerate(briefs, start=len(articles) + 1):
        number = f"{index:0{number_width}d}"
        result.append(
            ClassifiedEditionPublication(
                publication=publication,
                kind="brief",
                number=number,
                anchor=f"breve{number}",
            )
        )
    return tuple(result)


__all__ = [
    "BRIEF_MAX_WORDS",
    "ClassifiedEditionPublication",
    "classify_edition_publications",
    "is_brief",
    "publication_word_count",
]
