"""Coverage for the OOXML post-processing and the DOCX quality gate."""

from __future__ import annotations

import re
import zipfile
from datetime import date
from pathlib import Path

import pytest

from cti_app.application.docx_postprocessing import (
    DocxQualityError,
    apply_template_metadata,
    edition_template_values,
    substitute_placeholders,
    validate_docx,
)

DOCUMENT_HEAD = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
    "<w:body>"
)
DOCUMENT_TAIL = "</w:body></w:document>"

VALUES = {
    "EDITION_MONTH": "juillet 2026",
    "EDITION_COUNTRY": "Iran",
    "BULLETIN_NUMBER": "",
}


def _paragraph(*texts: str) -> str:
    runs = "".join(f"<w:r><w:rPr><w:b/></w:rPr><w:t>{text}</w:t></w:r>" for text in texts)
    return f"<w:p>{runs}</w:p>"


def _logical_text(xml: str) -> str:
    return "".join(re.findall(r"<w:t(?: [^>]*)?>(.*?)</w:t>", xml, re.S))


def _write_docx(path: Path, parts: dict[str, str]) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        for name, payload in parts.items():
            archive.writestr(name, payload)
    return path


def test_placeholder_split_across_several_text_elements_is_resolved() -> None:
    part = f"<w:hdr>{_paragraph('Bulletin ', '{{EDITION_', 'MONTH}}', ' final')}</w:hdr>"

    result = substitute_placeholders(part, VALUES)

    assert _logical_text(result) == "Bulletin juillet 2026 final"
    # Runs and their properties survive the substitution.
    assert result.count("<w:r>") == 4
    assert result.count("<w:b/>") == 4


def test_substitution_keeps_paragraphs_independent() -> None:
    part = "<w:hdr>{}{}</w:hdr>".format(
        _paragraph("{{EDITION_"),
        _paragraph("MONTH}} et {{EDITION_COUNTRY}}"),
    )

    result = substitute_placeholders(part, VALUES)

    assert "{{EDITION_" in _logical_text(result)
    assert "Iran" in _logical_text(result)
    assert "juillet 2026" not in _logical_text(result)


def test_empty_value_removes_the_whole_bulletin_label() -> None:
    part = f"<w:hdr>{_paragraph('{{BULLETIN_', 'NUMBER}}')}</w:hdr>"

    assert _logical_text(substitute_placeholders(part, VALUES)) == ""


def test_unknown_placeholders_are_left_untouched() -> None:
    part = f"<w:hdr>{_paragraph('{{UNKNOWN_TOKEN}}')}</w:hdr>"

    assert _logical_text(substitute_placeholders(part, VALUES)) == "{{UNKNOWN_TOKEN}}"


def test_edition_template_values_derive_the_month_from_the_period_start() -> None:
    july = edition_template_values({"period_start": "2026-07-01", "country": "Iran"})
    august = edition_template_values({"period_start": date(2026, 8, 1), "country": "Iran"})

    assert july["EDITION_MONTH"] == "juillet 2026"
    assert august["EDITION_MONTH"] == "août 2026"
    assert july["EDITION_COUNTRY"] == "Iran"
    # No bulletin numbering exists in the domain: it must stay empty.
    assert july["BULLETIN_NUMBER"] == ""


def test_apply_template_metadata_only_rewrites_header_and_footer_parts(tmp_path: Path) -> None:
    body = f"{DOCUMENT_HEAD}{_paragraph('{{EDITION_COUNTRY}}')}{DOCUMENT_TAIL}"
    path = _write_docx(
        tmp_path / "edition.docx",
        {
            "word/document.xml": body,
            "word/header1.xml": f"<w:hdr>{_paragraph('{{EDITION_', 'MONTH}}')}</w:hdr>",
            "word/footer1.xml": f"<w:ftr>{_paragraph('{{EDITION_COUNTRY}}')}</w:ftr>",
        },
    )

    apply_template_metadata(path, VALUES)

    with zipfile.ZipFile(path) as archive:
        assert _logical_text(archive.read("word/header1.xml").decode()) == "juillet 2026"
        assert _logical_text(archive.read("word/footer1.xml").decode()) == "Iran"
        # An analyst-authored body is never rewritten by the metadata pass.
        assert "{{EDITION_COUNTRY}}" in archive.read("word/document.xml").decode()


def test_validate_docx_accepts_a_resolved_document(tmp_path: Path) -> None:
    path = _write_docx(
        tmp_path / "ok.docx",
        {
            "word/document.xml": f"{DOCUMENT_HEAD}{_paragraph('Contenu')}{DOCUMENT_TAIL}",
            "word/header1.xml": f"<w:hdr>{_paragraph('juillet 2026')}</w:hdr>",
        },
    )

    validate_docx(path)


def test_validate_docx_rejects_a_residual_placeholder(tmp_path: Path) -> None:
    path = _write_docx(
        tmp_path / "placeholder.docx",
        {
            "word/document.xml": f"{DOCUMENT_HEAD}{_paragraph('Contenu')}{DOCUMENT_TAIL}",
            "word/header1.xml": f"<w:hdr>{_paragraph('{{EDITION_', 'MONTH}}')}</w:hdr>",
        },
    )

    with pytest.raises(DocxQualityError, match="docx_unresolved_placeholder"):
        validate_docx(path)
    # A preview without edition context legitimately keeps its placeholders.
    validate_docx(path, expect_resolved_placeholders=False)


def test_validate_docx_rejects_a_surviving_template_value(tmp_path: Path) -> None:
    path = _write_docx(
        tmp_path / "historical.docx",
        {
            "word/document.xml": f"{DOCUMENT_HEAD}{_paragraph('Contenu')}{DOCUMENT_TAIL}",
            "word/header1.xml": f"<w:hdr>{_paragraph('Bulletin n°32')}</w:hdr>",
        },
    )

    with pytest.raises(DocxQualityError, match="docx_template_leftover"):
        validate_docx(path)


def test_validate_docx_ignores_template_markers_written_by_an_analyst(tmp_path: Path) -> None:
    """``XXX`` inside an article is content, not a template leftover."""
    body = _paragraph("Le groupe utilise le mutex XXXX et le bulletin n°32 cité.")
    path = _write_docx(
        tmp_path / "content.docx",
        {"word/document.xml": f"{DOCUMENT_HEAD}{body}{DOCUMENT_TAIL}"},
    )

    validate_docx(path)


def test_validate_docx_rejects_a_raw_citation_in_the_body(tmp_path: Path) -> None:
    body = _paragraph("Information [https://example.test/1]")
    path = _write_docx(
        tmp_path / "citation.docx",
        {"word/document.xml": f"{DOCUMENT_HEAD}{body}{DOCUMENT_TAIL}"},
    )

    with pytest.raises(DocxQualityError, match="docx_unrendered_citation"):
        validate_docx(path)


def test_validate_docx_rejects_missing_footnotes(tmp_path: Path) -> None:
    body = '<w:p><w:r><w:footnoteReference w:id="2"/></w:r></w:p>'
    path = _write_docx(
        tmp_path / "footnotes.docx",
        {"word/document.xml": f"{DOCUMENT_HEAD}{body}{DOCUMENT_TAIL}"},
    )

    with pytest.raises(DocxQualityError, match="docx_missing_footnotes_part"):
        validate_docx(path)


def test_validate_docx_rejects_a_broken_archive(tmp_path: Path) -> None:
    broken = tmp_path / "broken.docx"
    broken.write_bytes(b"not a zip archive")

    with pytest.raises(DocxQualityError, match="docx_not_a_zip_archive"):
        validate_docx(broken)


def test_validate_docx_rejects_an_unparsable_document_part(tmp_path: Path) -> None:
    path = _write_docx(tmp_path / "invalid.docx", {"word/document.xml": "<w:body>"})

    with pytest.raises(DocxQualityError, match="docx_document_not_parsable"):
        validate_docx(path)


def test_validate_docx_rejects_an_archive_without_document_part(tmp_path: Path) -> None:
    path = _write_docx(tmp_path / "empty.docx", {"word/styles.xml": "<w:styles/>"})

    with pytest.raises(DocxQualityError, match="docx_missing_document_part"):
        validate_docx(path)
