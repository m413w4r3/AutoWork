"""Pure Pandoc Markdown renderer for the canonical publication model."""

from __future__ import annotations

from collections.abc import Mapping
from html import escape
from typing import Final
from urllib.parse import urlsplit

from cti_app.application.french_typography import format_french_date
from cti_app.application.production_normalization import display_indicator_value
from cti_app.domain.edition_publication import EditionDocumentV1, EditionDocumentV2
from cti_app.domain.publication import (
    ArtifactType,
    BriefDocumentV1,
    PublicationDocumentV2,
    RichSpanKind,
    RichText,
)

PANDOC_RENDERER_VERSION = "1"

# This is intentionally the sole source of Word style names in application code.
WORD_STYLE_MAP: Final[Mapping[str, str | None]] = {
    "title": "Titre partie bulletin",
    "section": "Veille - Titre de section",
    "paragraph": "Paragraphe bulletin",
    "ioc_value": "Veille - IOC",
    "code_block": "Code",
    "date": "Veille - date Char",
    "tool": "Veille - Outil Char",
    "technical": "Veille - Elément technique Char",
    "ioc": "Veille - IOC Char",
    "code": "Code Char",
    "actor": None,
    "malware": "Veille - Outil Char",
    "analyst_note": "Veille - Titre d'avis",
    "analyst_note_body": "Veille - Fond d'avis",
}

ANALYST_NOTE_TITLE = "Note de l'analyste"

# Pandoc has no portable page-break syntax for DOCX, so the renderer emits the
# Word element itself through a raw OOXML block.  Tildes keep the fence out of
# the backtick budget enforced on publication Markdown.
PAGE_BREAK_MARKDOWN = '~~~~{=openxml}\n<w:p><w:r><w:br w:type="page"/></w:r></w:p>\n~~~~'

_GROUPS = (
    (ArtifactType.IP, "Adresses IP"),
    (ArtifactType.DOMAIN, "Noms de domaine"),
    (ArtifactType.URL, "URL"),
    (ArtifactType.HASH, "Fichiers"),
)


def _safe_text(text: str) -> str:
    text = text.replace("`", "\u02cb")
    if text.count("[") != text.count("]"):
        text = text.replace("[", r"\[").replace("]", r"\]")
    return text


def _footnote_url(url: str) -> str:
    return "".join(f"\\{char}" if char in "_~*[]" else char for char in url)


def _styled_block(style_key: str, content: str) -> str:
    style = WORD_STYLE_MAP[style_key]
    assert style is not None
    return f'::: {{custom-style="{style}"}}\n{content}\n:::'


def _styled_inline(style_key: str, content: str) -> str:
    style = WORD_STYLE_MAP[style_key]
    assert style is not None
    return f'[{content}]{{custom-style="{style}"}}'


def _render_rich(text: RichText, sources: dict[str, str]) -> str:
    output: list[str] = []
    for span in text:
        value = _safe_text(span.text)
        if span.kind is RichSpanKind.CITATION:
            urls = [
                _footnote_url(sources[source_id])
                for source_id in dict.fromkeys(span.source_ids)
                if source_id in sources
            ]
            if urls:
                output.append(f"^[{' ; '.join(urls)}]")
        elif span.kind in {RichSpanKind.ACTOR, RichSpanKind.MALWARE}:
            output.append(f"**{value}**")
        elif span.kind is RichSpanKind.TOOL:
            output.append(_styled_inline("tool", value))
        elif span.kind is RichSpanKind.TECHNICAL:
            output.append(_styled_inline("technical", value))
        elif span.kind is RichSpanKind.IOC:
            nested_safe = value.replace("[", r"\[").replace("]", r"\]")
            output.append(_styled_inline("ioc", nested_safe))
        elif span.kind is RichSpanKind.CODE:
            output.append(_styled_inline("code", value))
        elif span.kind is RichSpanKind.EMPHASIS:
            output.append(f"*{value}*")
        else:
            output.append(value)
    return "".join(output)


def render_publication_pandoc(document: BriefDocumentV1 | PublicationDocumentV2) -> str:
    """Render publication Markdown without invoking Pandoc or reading the network."""
    sources = {source.source_id: source.canonical_url for source in document.sources}
    blocks = [_styled_block("title", _safe_text(document.title))]

    for entry in document.timeline:
        content = _render_rich(entry.content, sources)
        if entry.date is not None:
            date = _styled_inline("date", format_french_date(entry.date))
            content = f"{date}\u00a0: {content}"
        blocks.append(_styled_block("paragraph", content))

    blocks.append(_styled_block("section", "Synthèse"))
    blocks.extend(
        _styled_block("paragraph", _render_rich(paragraph, sources))
        for paragraph in document.synthesis
    )

    # The note is never synthesised by the renderer: it is only displayed when
    # an analyst explicitly attached one to the publication.
    if document.analyst_note is not None:
        blocks.append(_styled_block("analyst_note", _safe_text(ANALYST_NOTE_TITLE)))
        blocks.append(
            _styled_block("analyst_note_body", _render_rich(document.analyst_note, sources))
        )

    by_type = {group.artifact_type: group for group in document.indicators}
    populated = [pair for pair in _GROUPS if pair[0] in by_type and by_type[pair[0]].values]
    if populated:
        blocks.append(_styled_block("section", "IOC"))
        for artifact_type, label in populated:
            blocks.append(_styled_block("paragraph", f"{label}\u00a0:"))
            values = "\n\n".join(
                _safe_text(
                    display_indicator_value(item.normalized_value, artifact_type, defanged=False)
                )
                for item in by_type[artifact_type].values
            )
            blocks.append(_styled_block("ioc_value", values))

    rendered = "\n\n".join(blocks).rstrip() + "\n"
    if "`" in rendered:
        raise ValueError("Pandoc publication Markdown must not contain backticks")
    return rendered


def render_edition_pandoc(document: EditionDocumentV1 | EditionDocumentV2) -> str:
    """Render an ordered edition from its already frozen publication documents."""
    if not document.publications:
        raise ValueError("An edition document must contain at least one publication")
    separator = f"\n\n{PAGE_BREAK_MARKDOWN}\n\n"
    rendered = separator.join(
        render_publication_pandoc(publication.document).rstrip()
        for publication in document.publications
    )
    return rendered.rstrip() + "\n"


def _safe_html_url(url: str) -> str | None:
    parsed = urlsplit(url)
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.netloc:
        return None
    return escape(url, quote=True)


def _render_rich_html(text: RichText, sources: dict[str, str]) -> str:
    output: list[str] = []
    for span in text:
        value = escape(span.text)
        if span.kind is RichSpanKind.CITATION:
            links = []
            for source_id in dict.fromkeys(span.source_ids):
                url = _safe_html_url(sources[source_id]) if source_id in sources else None
                if url is not None:
                    links.append(f'<a href="{url}" rel="noreferrer">source</a>')
            if links:
                output.append(f'<sup class="citation">{" ; ".join(links)}</sup>')
        elif span.kind in {RichSpanKind.ACTOR, RichSpanKind.MALWARE}:
            output.append(f"<strong>{value}</strong>")
        elif span.kind is RichSpanKind.EMPHASIS:
            output.append(f"<em>{value}</em>")
        elif span.kind is RichSpanKind.CODE:
            output.append(f"<code>{value}</code>")
        elif span.kind in {RichSpanKind.TOOL, RichSpanKind.TECHNICAL, RichSpanKind.IOC}:
            output.append(f'<span class="{span.kind.value}">{value}</span>')
        else:
            output.append(value)
    return "".join(output)


def _render_publication_html(document: PublicationDocumentV2) -> str:
    sources = {source.source_id: source.canonical_url for source in document.sources}
    blocks = [f"<h1>{escape(document.title)}</h1>"]
    for entry in document.timeline:
        content = _render_rich_html(entry.content, sources)
        if entry.date is not None:
            content = f"<time>{escape(format_french_date(entry.date))}</time>\u00a0: {content}"
        blocks.append(f"<p>{content}</p>")

    blocks.append("<h2>Synthèse</h2>")
    blocks.extend(
        f"<p>{_render_rich_html(paragraph, sources)}</p>" for paragraph in document.synthesis
    )
    if document.analyst_note is not None:
        blocks.append(f"<h2>{escape(ANALYST_NOTE_TITLE)}</h2>")
        blocks.append(f"<p>{_render_rich_html(document.analyst_note, sources)}</p>")

    by_type = {group.artifact_type: group for group in document.indicators}
    populated = [pair for pair in _GROUPS if pair[0] in by_type and by_type[pair[0]].values]
    if populated:
        blocks.append("<h2>IOC</h2>")
        for artifact_type, label in populated:
            blocks.append(f"<h3>{escape(label)}</h3>")
            values = "".join(
                "<li>"
                + escape(
                    display_indicator_value(item.normalized_value, artifact_type, defanged=False)
                )
                + "</li>"
                for item in by_type[artifact_type].values
            )
            blocks.append(f"<ul>{values}</ul>")
    return "\n".join(blocks) + "\n"


def render_edition_html(document: EditionDocumentV2) -> str:
    """Render a deliberately small, escaped HTML view for browser preview."""
    return (
        "\n<hr />\n".join(
            _render_publication_html(publication.document).rstrip()
            for publication in document.publications
        )
        + "\n"
    )
