from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ArticleParagraphFixture:
    paragraph_id: str
    position: int
    text: str


@dataclass(frozen=True, slots=True)
class ArticleFixture:
    article_id: str
    title: str
    paragraphs: tuple[ArticleParagraphFixture, ...]


def duplicate_paragraph_article() -> ArticleFixture:
    """Return a synthetic article with two equal-text occurrences."""
    text = "Repeated synthetic paragraph for editorial occurrence tests."
    return ArticleFixture(
        article_id="synthetic-article-001",
        title="Synthetic editorial article",
        paragraphs=(
            ArticleParagraphFixture(
                paragraph_id="synthetic-paragraph-001",
                position=0,
                text=text,
            ),
            ArticleParagraphFixture(
                paragraph_id="synthetic-paragraph-002",
                position=1,
                text=text,
            ),
        ),
    )
