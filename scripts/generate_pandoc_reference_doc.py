"""Derive the versioned Pandoc reference document from the editorial template."""

from __future__ import annotations

import argparse
import re
import tempfile
import zipfile
from pathlib import Path


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
