"""Pure Pandoc Markdown renderer for the canonical publication model."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

from cti_app.application.french_typography import format_french_date
from cti_app.application.production_normalization import display_indicator_value
from cti_app.domain.publication import ArtifactType, BriefDocumentV1, RichSpanKind, RichText

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
}

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
    return "".join(f"\\{char}" if char in "_~*" else char for char in url)


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
            output.extend(
                "^[" + _footnote_url(sources[source_id]) + "]"
                for source_id in dict.fromkeys(span.source_ids)
                if source_id in sources
            )
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


def render_brief_pandoc(document: BriefDocumentV1) -> str:
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

    by_type = {group.artifact_type: group for group in document.indicators}
    populated = [pair for pair in _GROUPS if pair[0] in by_type and by_type[pair[0]].values]
    if populated:
        blocks.append(_styled_block("section", "IOC"))
        for artifact_type, label in populated:
            blocks.append(_styled_block("paragraph", f"{label}\u00a0:"))
            values = "\n\n".join(
                _safe_text(
                    display_indicator_value(
                        item.normalized_value, artifact_type, defanged=False
                    )
                )
                for item in by_type[artifact_type].values
            )
            blocks.append(_styled_block("ioc_value", values))

    rendered = "\n\n".join(blocks).rstrip() + "\n"
    if "`" in rendered:
        raise ValueError("Pandoc publication Markdown must not contain backticks")
    return rendered
