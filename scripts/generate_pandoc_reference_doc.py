"""Derive the versioned Pandoc reference document from the editorial template.

The body is dropped so Pandoc owns the content, and the header/footer metadata
of the source template is normalized into explicit ``{{PLACEHOLDER}}`` tokens.
The normalization is shape-based, never value-based, so regenerating from a
newer template keeps working and re-running it on an already generated
reference document is a no-op.
"""

from __future__ import annotations

import argparse
import re
import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "backend" / "src"))

from cti_app.application.docx_postprocessing import (  # noqa: E402
    TEMPLATE_PART_PATTERN,
    TEXT_ELEMENT_PATTERN,
    escape_xml_text,
    unescape_xml_text,
)

_MONTH_NAMES = (
    "janvier|février|mars|avril|mai|juin|"
    "juillet|août|septembre|octobre|novembre|décembre"
)

#: Ordered shape rules turning template metadata into placeholders.
PLACEHOLDER_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    # The label is part of the placeholder: the bulletin number does not exist
    # in the domain yet, so it must be able to disappear without leaving a
    # dangling "Bulletin n°" behind.
    (re.compile(r"Bulletin\s*n°\s*\d+", re.IGNORECASE), "{{BULLETIN_NUMBER}}"),
    (re.compile(rf"(?:{_MONTH_NAMES})\s+\d{{4}}", re.IGNORECASE), "{{EDITION_MONTH}}"),
    (re.compile(r"X{3,}"), "{{EDITION_COUNTRY}}"),
)


def normalize_template_part(xml: str) -> str:
    """Rewrite the logical text of each paragraph into placeholder tokens."""
    segments = xml.split("</w:p>")
    return "</w:p>".join(_normalize_segment(segment) for segment in segments)


def _normalize_segment(segment: str) -> str:
    matches = list(TEXT_ELEMENT_PATTERN.finditer(segment))
    if not matches:
        return segment
    texts = [unescape_xml_text(match.group("text")) for match in matches]
    logical = "".join(texts)
    normalized = logical
    for pattern, placeholder in PLACEHOLDER_RULES:
        normalized = pattern.sub(placeholder, normalized)
    if normalized == logical:
        return segment

    # The whole logical string lands in the first run of the paragraph; the
    # other runs are emptied but kept, so their properties survive.
    result: list[str] = []
    cursor = 0
    for index, match in enumerate(matches):
        result.append(segment[cursor : match.start()])
        text = normalized if index == 0 else ""
        result.append(f'<w:t xml:space="preserve">{escape_xml_text(text)}</w:t>')
        cursor = match.end()
    result.append(segment[cursor:])
    return "".join(result)


def generate(source: Path, destination: Path) -> None:
    if not source.is_file():
        raise FileNotFoundError(source)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        suffix=".docx", dir=destination.parent, delete=False
    ) as temporary:
        temp_path = Path(temporary.name)
    try:
        with zipfile.ZipFile(source) as incoming, zipfile.ZipFile(
            temp_path, "w", compression=zipfile.ZIP_DEFLATED
        ) as outgoing:
            for info in incoming.infolist():
                payload = incoming.read(info.filename)
                if info.filename == "word/document.xml":
                    document = payload.decode("utf-8")
                    match = re.fullmatch(
                        r"(?s)(?P<head>.*?<w:body(?:\s[^>]*)?>).*?"
                        r"(?P<section><w:sectPr(?:\s[^>]*)?>.*?</w:sectPr>)"
                        r"(?P<tail></w:body>.*)",
                        document,
                    )
                    if match is None:
                        raise ValueError("Template has no Word document body")
                    payload = (
                        match.group("head") + match.group("section") + match.group("tail")
                    ).encode("utf-8")
                elif TEMPLATE_PART_PATTERN.match(info.filename):
                    payload = normalize_template_part(payload.decode("utf-8")).encode("utf-8")
                outgoing.writestr(info, payload)
        temp_path.replace(destination)
        destination.chmod(0o644)
    finally:
        temp_path.unlink(missing_ok=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    generate(args.source, args.destination)
