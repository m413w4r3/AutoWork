"""Deterministic OOXML post-processing and quality gate for exported DOCX.

Pandoc owns the document body.  Everything here covers what a ``--reference-doc``
cannot express: edition metadata carried by the template headers and footers,
and a final structural check on the produced archive.
"""

from __future__ import annotations

import re
import shutil
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from collections.abc import Mapping
from datetime import date
from pathlib import Path
from typing import Any, Final

from cti_app.application.french_typography import format_french_month

WORD_NAMESPACE: Final = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"

EDITION_MONTH_PLACEHOLDER: Final = "EDITION_MONTH"
EDITION_COUNTRY_PLACEHOLDER: Final = "EDITION_COUNTRY"
BULLETIN_NUMBER_PLACEHOLDER: Final = "BULLETIN_NUMBER"

#: Placeholders the versioned reference document is allowed to carry.
TEMPLATE_PLACEHOLDERS: Final[tuple[str, ...]] = (
    EDITION_MONTH_PLACEHOLDER,
    EDITION_COUNTRY_PLACEHOLDER,
    BULLETIN_NUMBER_PLACEHOLDER,
)

#: Literal values inherited from the editorial template.  They are only ever
#: looked for inside header/footer parts, never in analyst-authored content.
HISTORICAL_TEMPLATE_MARKERS: Final[tuple[str, ...]] = (
    "Bulletin n°32",
    "Juillet 2024",
    "XXX",
)

PLACEHOLDER_PATTERN: Final = re.compile(r"\{\{[A-Z][A-Z0-9_]*\}\}")
TEXT_ELEMENT_PATTERN: Final = re.compile(r"<w:t(?P<attributes> [^>]*)?>(?P<text>.*?)</w:t>", re.S)
TEMPLATE_PART_PATTERN: Final = re.compile(r"^word/(?:header|footer)\d*\.xml$")


class DocxQualityError(RuntimeError):
    """Raised when a produced DOCX fails the deterministic QA gate."""


def escape_xml_text(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def unescape_xml_text(text: str) -> str:
    return text.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")


def substitute_placeholders(xml: str, values: Mapping[str, str]) -> str:
    """Resolve ``{{PLACEHOLDER}}`` tokens inside the text runs of an OOXML part.

    Word freely splits a logical string across several ``<w:t>`` elements, so
    the substitution rebuilds the logical text of each paragraph, locates the
    known placeholders there, and writes the result back into the existing runs
    without touching their properties.  Unknown tokens are left untouched: the
    QA gate is responsible for reporting them.
    """
    replacements = {f"{{{{{name}}}}}": values[name] for name in values}
    if not replacements:
        return xml
    # Splitting on paragraph ends bounds every logical string to one paragraph,
    # including paragraphs nested inside a text box, without ever having to
    # pair an opening tag with the right closing one.
    segments = xml.split("</w:p>")
    return "</w:p>".join(_substitute_in_segment(segment, replacements) for segment in segments)


def _substitute_in_segment(segment: str, replacements: Mapping[str, str]) -> str:
    matches = list(TEXT_ELEMENT_PATTERN.finditer(segment))
    if not matches:
        return segment
    texts = [unescape_xml_text(match.group("text")) for match in matches]
    logical = "".join(texts)
    if not any(token in logical for token in replacements):
        return segment

    starts: list[int] = []
    offset = 0
    for text in texts:
        starts.append(offset)
        offset += len(text)

    # Character-level plan: the replacement lands on the run holding the first
    # character of the placeholder, the remaining characters are dropped.
    planned: list[str | None] = [None] * len(logical)
    covered = [False] * len(logical)
    for token, value in replacements.items():
        for found in re.finditer(re.escape(token), logical):
            if any(covered[found.start() : found.end()]):
                continue
            planned[found.start()] = value
            for index in range(found.start(), found.end()):
                covered[index] = True

    rewritten: list[str] = []
    for index, text in enumerate(texts):
        start = starts[index]
        pieces: list[str] = []
        for position in range(start, start + len(text)):
            if planned[position] is not None:
                pieces.append(planned[position] or "")
            if not covered[position]:
                pieces.append(logical[position])
        rewritten.append("".join(pieces))

    result: list[str] = []
    cursor = 0
    for match, text in zip(matches, rewritten, strict=True):
        result.append(segment[cursor : match.start()])
        result.append(f'<w:t xml:space="preserve">{escape_xml_text(text)}</w:t>')
        cursor = match.end()
    result.append(segment[cursor:])
    return "".join(result)


def edition_template_values(edition: Mapping[str, Any]) -> dict[str, str]:
    """Map an edition metadata projection onto the template placeholders.

    ``BULLETIN_NUMBER`` resolves to an empty string: no bulletin numbering
    exists anywhere in the domain, and fabricating one from a version, a UUID
    or a publication index would be editorially wrong.
    """
    period_start = edition["period_start"]
    if not isinstance(period_start, date):
        period_start = date.fromisoformat(str(period_start))
    return {
        EDITION_MONTH_PLACEHOLDER: format_french_month(period_start),
        EDITION_COUNTRY_PLACEHOLDER: str(edition["country"]),
        BULLETIN_NUMBER_PLACEHOLDER: "",
    }


def apply_template_metadata(docx_path: Path, values: Mapping[str, str]) -> None:
    """Resolve template placeholders inside the header and footer parts only."""
    with tempfile.NamedTemporaryFile(
        suffix=".docx", dir=docx_path.parent, delete=False
    ) as temporary:
        temp_path = Path(temporary.name)
    try:
        with zipfile.ZipFile(docx_path) as incoming, zipfile.ZipFile(
            temp_path, "w", compression=zipfile.ZIP_DEFLATED
        ) as outgoing:
            for info in incoming.infolist():
                payload = incoming.read(info.filename)
                if TEMPLATE_PART_PATTERN.match(info.filename):
                    payload = substitute_placeholders(
                        payload.decode("utf-8"), values
                    ).encode("utf-8")
                outgoing.writestr(info, payload)
        shutil.move(str(temp_path), str(docx_path))
    finally:
        temp_path.unlink(missing_ok=True)


def validate_docx(docx_path: Path, *, expect_resolved_placeholders: bool = True) -> None:
    """Check the produced archive before it becomes an immutable artifact."""
    try:
        with zipfile.ZipFile(docx_path) as archive:
            if archive.testzip() is not None:
                raise DocxQualityError("docx_archive_corrupt")
            names = set(archive.namelist())
            if "word/document.xml" not in names:
                raise DocxQualityError("docx_missing_document_part")
            try:
                document = ET.fromstring(archive.read("word/document.xml"))
            except ET.ParseError as exc:
                raise DocxQualityError("docx_document_not_parsable") from exc

            body_text = "".join(document.itertext())
            if "[https://" in body_text:
                raise DocxQualityError("docx_unrendered_citation")
            references = document.findall(f".//{{{WORD_NAMESPACE}}}footnoteReference")
            if references and "word/footnotes.xml" not in names:
                raise DocxQualityError("docx_missing_footnotes_part")

            for name in sorted(names):
                if not TEMPLATE_PART_PATTERN.match(name):
                    continue
                part = archive.read(name).decode("utf-8")
                logical = "".join(
                    unescape_xml_text(match.group("text"))
                    for match in TEXT_ELEMENT_PATTERN.finditer(part)
                )
                if expect_resolved_placeholders and PLACEHOLDER_PATTERN.search(logical):
                    raise DocxQualityError(f"docx_unresolved_placeholder:{name}")
                for marker in HISTORICAL_TEMPLATE_MARKERS:
                    if marker in logical:
                        raise DocxQualityError(f"docx_template_leftover:{name}")
    except zipfile.BadZipFile as exc:
        raise DocxQualityError("docx_not_a_zip_archive") from exc


__all__ = [
    "BULLETIN_NUMBER_PLACEHOLDER",
    "EDITION_COUNTRY_PLACEHOLDER",
    "EDITION_MONTH_PLACEHOLDER",
    "HISTORICAL_TEMPLATE_MARKERS",
    "TEMPLATE_PART_PATTERN",
    "TEMPLATE_PLACEHOLDERS",
    "TEXT_ELEMENT_PATTERN",
    "DocxQualityError",
    "apply_template_metadata",
    "edition_template_values",
    "escape_xml_text",
    "substitute_placeholders",
    "unescape_xml_text",
    "validate_docx",
]
