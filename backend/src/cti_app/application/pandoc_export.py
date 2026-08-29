"""Subprocess boundary for exporting a canonical publication to DOCX."""

from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
from pathlib import Path

from cti_app.application.pandoc_rendering import render_publication_pandoc
from cti_app.domain.publication import BriefDocumentV1, PublicationDocumentV2

LOGGER = logging.getLogger(__name__)
PANDOC_EXTENSIONS = "markdown+fenced_divs+bracketed_spans+superscript"
DEFAULT_REFERENCE_DOC = Path(__file__).parents[3] / "assets" / "pandoc" / "reference-doc-v1.docx"


class PandocExportError(RuntimeError):
    pass


def export_publication_docx(
    document: BriefDocumentV1 | PublicationDocumentV2,
    output_path: Path,
    *,
    reference_doc: Path = DEFAULT_REFERENCE_DOC,
    executable: str = "pandoc",
    timeout: float = 30.0,
) -> Path:
    return export_markdown_docx(
        render_publication_pandoc(document),
        output_path,
        reference_doc=reference_doc,
        executable=executable,
        timeout=timeout,
    )


def export_markdown_docx(
    markdown_content: str,
    output_path: Path,
    *,
    reference_doc: Path = DEFAULT_REFERENCE_DOC,
    executable: str = "pandoc",
    timeout: float = 30.0,
) -> Path:
    """Export renderer-independent Markdown to DOCX with Pandoc."""
    binary = shutil.which(executable)
    if binary is None:
        raise PandocExportError(f"Pandoc executable not found: {executable}")
    if not reference_doc.is_file():
        raise PandocExportError(f"Pandoc reference document not found: {reference_doc}")
    version = subprocess.run(
        [binary, "--version"], capture_output=True, text=True, timeout=timeout, check=False
    )
    LOGGER.info("Pandoc version: %s", version.stdout.splitlines()[0])
    with tempfile.TemporaryDirectory(prefix="autowork-pandoc-") as directory:
        markdown = Path(directory) / "brief.md"
        markdown.write_text(markdown_content, encoding="utf-8")
        try:
            completed = subprocess.run(
                [
                    binary,
                    f"--from={PANDOC_EXTENSIONS}",
                    "--to=docx",
                    f"--reference-doc={reference_doc}",
                    "--output",
                    str(output_path),
                    str(markdown),
                ],
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise PandocExportError(f"Pandoc export timed out after {timeout:g}s") from exc
    if completed.returncode != 0:
        raise PandocExportError(
            f"Pandoc export failed ({completed.returncode}): {completed.stderr.strip()}"
        )
    return output_path
