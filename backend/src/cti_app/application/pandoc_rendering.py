"""Pure Pandoc Markdown renderer for the canonical publication model."""

from __future__ import annotations

from collections.abc import Mapping
from html import escape
from typing import Final

from cti_app.application.french_typography import format_french_date
from cti_app.domain.edition_publication import EditionDocumentV2
from cti_app.domain.publication import PublicationEvidenceRefV1
from cti_app.domain.publication_document import CanonicalPublicationDocument

PANDOC_RENDERER_VERSION = "1"

# This is intentionally the sole source of Word style names in application code.
WORD_STYLE_MAP: Final[Mapping[str, str | None]] = {
    "title": "Titre partie bulletin",
    "section": "Veille - Titre de section",
    "paragraph": "Paragraphe bulletin",
    "ioc_value": "Veille - IOC",
}

# Pandoc has no portable page-break syntax for DOCX, so the renderer emits the
# Word element itself through a raw OOXML block.  Tildes keep the fence out of
# the backtick budget enforced on publication Markdown.
PAGE_BREAK_MARKDOWN = '~~~~{=openxml}\n<w:p><w:r><w:br w:type="page"/></w:r></w:p>\n~~~~'


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


def _render_v3_citations(
    refs: tuple[PublicationEvidenceRefV1, ...], sources: dict[str, str]
) -> str:
    urls = list(dict.fromkeys(sources[str(ref.source_document_id)] for ref in refs))
    return f" ^[{' ; '.join(_footnote_url(url) for url in urls)}]" if urls else ""


def _render_publication_v3(document: CanonicalPublicationDocument) -> str:
    sources = {str(source.source_document_id): source.canonical_url for source in document.sources}
    blocks = [_styled_block("title", _safe_text(document.title))]
    blocks.extend(
        _styled_block(
            "paragraph", _safe_text(item.text) + _render_v3_citations(item.evidence_refs, sources)
        )
        for item in document.lead
    )
    for section in document.sections:
        blocks.append(_styled_block("section", _safe_text(section.heading)))
        blocks.extend(
            _styled_block(
                "paragraph",
                _safe_text(item.text) + _render_v3_citations(item.evidence_refs, sources),
            )
            for item in section.paragraphs
        )
    if document.timeline:
        blocks.append(_styled_block("section", "Chronologie"))
        for entry in document.timeline:
            label = (
                format_french_date(entry.event_date)
                if entry.event_date is not None
                else entry.date_text
            )
            content = f"{_safe_text(label)} : " if label else ""
            content += _safe_text(entry.text) + _render_v3_citations(entry.evidence_refs, sources)
            blocks.append(_styled_block("paragraph", content))
    if document.indicators:
        blocks.append(_styled_block("section", "IOC"))
        for group in document.indicators:
            for indicator in group.indicators:
                urls = list(
                    dict.fromkeys(
                        sources[str(source_id)] for source_id in indicator.source_document_ids
                    )
                )
                citation = f" ^[{' ; '.join(_footnote_url(url) for url in urls)}]" if urls else ""
                blocks.append(
                    _styled_block("ioc_value", _safe_text(indicator.normalized_value) + citation)
                )
    if document.uncertainties:
        blocks.append(_styled_block("section", "Incertitudes"))
        for uncertainty in document.uncertainties:
            urls = list(
                dict.fromkeys(
                    sources[str(source_id)] for source_id in uncertainty.source_document_ids
                )
            )
            citation = f" ^[{' ; '.join(_footnote_url(url) for url in urls)}]" if urls else ""
            blocks.append(_styled_block("paragraph", _safe_text(uncertainty.text) + citation))
    return "\n\n".join(blocks).rstrip() + "\n"


def render_publication_pandoc(document: CanonicalPublicationDocument) -> str:
    """Render publication Markdown without invoking Pandoc or reading the network."""
    rendered = _render_publication_v3(document)
    if "`" in rendered:
        raise ValueError("Pandoc publication Markdown must not contain backticks")
    return rendered


def render_edition_pandoc(document: EditionDocumentV2) -> str:
    """Render an ordered edition from its already frozen publication documents."""
    if not document.publications:
        raise ValueError("An edition document must contain at least one publication")
    separator = f"\n\n{PAGE_BREAK_MARKDOWN}\n\n"
    rendered = separator.join(
        render_publication_pandoc(publication.document).rstrip()
        for publication in document.publications
    )
    return rendered.rstrip() + "\n"


def _render_publication_html(document: CanonicalPublicationDocument) -> str:
    sources = {str(source.source_document_id): source.canonical_url for source in document.sources}

    def citations(ids: list[str]) -> str:
        links = list(dict.fromkeys(sources[source_id] for source_id in ids))
        return "".join(
            ' <sup class="citation"><a href="'
            f'{escape(url, quote=True)}" rel="noreferrer">source</a></sup>'
            for url in links
        )

    def paragraph_html(text: str, ids: list[str]) -> str:
        return f"<p>{escape(text)}{citations(ids)}</p>"

    blocks = [f"<h1>{escape(document.title)}</h1>"]
    blocks.extend(
        paragraph_html(item.text, [str(ref.source_document_id) for ref in item.evidence_refs])
        for item in document.lead
    )
    for section in document.sections:
        blocks.append(f"<h2>{escape(section.heading)}</h2>")
        blocks.extend(
            paragraph_html(item.text, [str(ref.source_document_id) for ref in item.evidence_refs])
            for item in section.paragraphs
        )
    if document.timeline:
        blocks.append("<h2>Chronologie</h2>")
        for item in document.timeline:
            label = format_french_date(item.event_date) if item.event_date else item.date_text
            blocks.append(
                f"<p>{escape(label + ' : ' if label else '')}{escape(item.text)}"
                f"{citations([str(ref.source_document_id) for ref in item.evidence_refs])}</p>"
            )
    if document.indicators:
        blocks.append("<h2>IOC</h2>")
        for group in document.indicators:
            blocks.append(f"<h3>{escape(group.artifact_type.value)}</h3><ul>")
            blocks.extend(
                f"<li><code>{escape(item.normalized_value)}</code>"
                f"{citations([str(source_id) for source_id in item.source_document_ids])}</li>"
                for item in group.indicators
            )
            blocks.append("</ul>")
    if document.uncertainties:
        blocks.append("<h2>Incertitudes</h2>")
        blocks.extend(
            paragraph_html(item.text, [str(source_id) for source_id in item.source_document_ids])
            for item in document.uncertainties
        )
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
