import asyncio
import os
import shutil
import subprocess
import unicodedata
import xml.etree.ElementTree as ET
from dataclasses import replace
from pathlib import Path

import pytest
from pypdf import PdfReader

from cti_app.application.production_synthesis import canonical_extraction_hash
from cti_app.application.publication_builder import build_publication_document_v5
from cti_app.application.typst_compilation import (
    TYPST_COMPILER_VERSION,
    TypstCompileRequest,
    load_font_bundle_snapshot,
    materialize_font_bundle,
)
from cti_app.application.typst_paths import typst_bundle_paths
from cti_app.application.typst_rendering import (
    TypstRenderer,
    load_template_bundle,
    project_publication_to_typst_model,
)
from cti_app.domain.production_extraction import (
    ExtractionIndicatorStatus,
    ExtractionIndicatorV1,
    ProductionEvidenceBasis,
)
from cti_app.domain.publication import ArtifactType
from cti_app.domain.publication_document import (
    PublicationDocumentV5,
    PublicationIOCDisposition,
    publication_document_from_json,
    serialize_publication_document,
)
from cti_app.infrastructure.typst_compiler import TypstSubprocessCompiler
from tests.test_publication_builder_v4 import _canonical_inputs, _enrichment_with


def _ioc_document():
    snapshot, references, extraction, synthesis = _canonical_inputs()
    source_id = extraction.sources[0].source_document_id
    values = (
        (
            ArtifactType.HASH,
            "a" * 64,
            "SHA-256 d'un exécutable observé",
            ExtractionIndicatorStatus.CONFIRMED_IOC,
        ),
        (
            ArtifactType.URL,
            "https://cdn.example.invalid/a/long/path/with-more-than-one-word-and-accent-é.html",
            "URL rapportée dans la section infrastructure",
            ExtractionIndicatorStatus.CONFIRMED_IOC,
        ),
        (
            ArtifactType.DOMAIN,
            "debug.example.invalid",
            "Nom de domaine cité comme contexte, sans rôle IOC confirmé",
            ExtractionIndicatorStatus.CONTEXTUAL,
        ),
        (
            ArtifactType.CVE,
            "CVE-2025-12345",
            "Référence de vulnérabilité hors types IOC publiables",
            ExtractionIndicatorStatus.CONFIRMED_IOC,
        ),
        *tuple(
            (
                ArtifactType.HASH,
                f"{index:064x}",
                f"Hash SHA-256 n° {index} relevé dans l'annexe technique",
                ExtractionIndicatorStatus.CONFIRMED_IOC,
            )
            for index in range(1, 25)
        ),
    )
    indicators = tuple(
        ExtractionIndicatorV1(
            value=value,
            artifact_type=artifact_type,
            indicator_status=status,
            context=context,
            evidence_quote=f"La source documente {value}.",
            evidence_basis=ProductionEvidenceBasis.SOURCE_VERIFIED,
            source_document_ids=(source_id,),
        )
        for artifact_type, value, context, status in values
    )
    extraction = replace(
        extraction,
        sources=(replace(extraction.sources[0], indicators=indicators),),
    )
    synthesis = replace(synthesis, extraction_hash=canonical_extraction_hash(extraction))
    document = build_publication_document_v5(
        snapshot=snapshot,
        references=references,
        extraction=extraction,
        synthesis=synthesis,
        editorial_enrichment=_enrichment_with(extraction=extraction, synthesis=synthesis),
    )
    return document, values


def test_analyst_repair_ioc_without_source_quote_is_not_rendered_as_a_detail_row() -> None:
    snapshot, references, extraction, synthesis = _canonical_inputs()
    source = extraction.sources[0]
    repaired_indicator = ExtractionIndicatorV1(
        value="repaired.example",
        artifact_type=ArtifactType.DOMAIN,
        indicator_status=ExtractionIndicatorStatus.CONFIRMED_IOC,
        context="Analyst-approved repair",
        evidence_quote="",
        evidence_basis=ProductionEvidenceBasis.ANALYST_OVERRIDE,
        source_document_ids=(source.source_document_id,),
    )
    extraction = replace(extraction, sources=(replace(source, indicators=(repaired_indicator,)),))
    synthesis = replace(synthesis, extraction_hash=canonical_extraction_hash(extraction))
    document = build_publication_document_v5(
        snapshot=snapshot,
        references=references,
        extraction=extraction,
        synthesis=synthesis,
        editorial_enrichment=_enrichment_with(extraction=extraction, synthesis=synthesis),
    )

    annex = next(
        section
        for section in project_publication_to_typst_model(document).content_sections
        if section["type"] == "technical_annex"
    )
    assert any(
        indicator.normalized_value == "repaired.example"
        for group in document.indicators
        for indicator in group.indicators
    )
    assert all(item.value != "repaired.example" for item in document.ioc_details)
    assert "repaired.example" in {
        indicator["text"] for group in annex["indicators"].values() for indicator in group
    }


def test_typst_ioc_table_keeps_full_values_and_evidence_disposition() -> None:
    document, values = _ioc_document()

    model = project_publication_to_typst_model(document)
    annex = next(
        section for section in model.content_sections if section["type"] == "technical_annex"
    )
    rows = annex["ioc_rows"]

    rows_by_value = {row["value"]: row for row in rows}
    assert set(rows_by_value) == {value for _kind, value, _context, _status in values}
    assert all(
        [source["url"] for source in row["source_urls"]] == ["https://example.com/report"]
        for row in rows
    )
    assert all(row["reliability"] == "source_verified" for row in rows)
    assert all(row["first_observed"] is None and row["last_observed"] is None for row in rows)
    assert rows_by_value[values[0][1]]["context"] == values[0][2]
    assert rows_by_value[values[0][1]]["status"] == PublicationIOCDisposition.RETAINED.value
    assert rows_by_value[values[2][1]]["status"] == PublicationIOCDisposition.UNCERTAIN.value
    assert rows_by_value[values[3][1]]["status"] == PublicationIOCDisposition.EXCLUDED.value

    payload = serialize_publication_document(document)
    restored = publication_document_from_json(payload)
    assert isinstance(restored, PublicationDocumentV5)
    assert restored.ioc_details == document.ioc_details
    assert serialize_publication_document(restored) == payload


@pytest.mark.asyncio
async def test_real_typst_ioc_table_keeps_values_and_fits_a4_pages(tmp_path: Path) -> None:
    document, values = _ioc_document()
    chp_typst_root, font_bundle_root, font_lock_path = typst_bundle_paths()
    template_bundle = load_template_bundle(chp_typst_root)
    render_source = TypstRenderer().render(document, template_bundle)

    workspace_root = tmp_path / "ioc-table-workspace"
    shutil.copytree(chp_typst_root / "RENDERER", workspace_root / "RENDERER")
    shutil.copytree(chp_typst_root / "UTILS", workspace_root / "UTILS")
    media_root = workspace_root / "RENDERER" / "media"
    media_root.mkdir()
    shutil.copy2(chp_typst_root / "UTILS" / "chap.png", media_root / "figure.png")
    (workspace_root / "RENDERER" / "render-data.json").write_bytes(render_source.render_data_bytes)

    binary = os.environ.get("TYPST_BINARY") or shutil.which("typst")
    assert binary is not None, "Typst 0.15.1 must be installed for this rendering test"
    version = await asyncio.to_thread(
        subprocess.run, (binary, "--version"), capture_output=True, check=True, timeout=10
    )
    version_parts = version.stdout.decode().split()
    assert version_parts[:2] == ["typst", TYPST_COMPILER_VERSION]
    font_snapshot = load_font_bundle_snapshot(font_bundle_root, font_lock_path)
    font_root = tmp_path / "fonts"
    font_root.mkdir()
    compiled = await TypstSubprocessCompiler(binary=binary).compile(
        TypstCompileRequest(
            workspace_root=workspace_root,
            entrypoint_relative_path=render_source.entrypoint_relative_path,
            font_paths=materialize_font_bundle(font_snapshot, font_root),
        )
    )

    pdf_path = tmp_path / "ioc-table.pdf"
    pdf_path.write_bytes(compiled.content)
    pdftotext = shutil.which("pdftotext")
    pdftoppm = shutil.which("pdftoppm")
    assert pdftotext is not None and pdftoppm is not None
    text_result = await asyncio.to_thread(
        subprocess.run,
        (pdftotext, "-raw", str(pdf_path), "-"),
        capture_output=True,
        check=True,
        timeout=10,
    )
    extracted_text = text_result.stdout.decode("utf-8", errors="replace")
    normalized_text = "".join(
        character
        for character in extracted_text
        if not character.isspace() and unicodedata.category(character) != "Cf"
    )
    for _kind, value, _context, _status in values:
        assert value in normalized_text
    assert "IOC—valeurs,provenanceetdécisiondepublication" in normalized_text
    assert "Première/dernièreobservation" in normalized_text
    assert "Sourceetfiabilité" in normalized_text
    assert "Statutetréserves" in normalized_text
    assert "Écarté" in extracted_text
    assert "Incertain" in extracted_text
    assert "Retenu" in extracted_text
    page_count = len(PdfReader(pdf_path, strict=True).pages)
    assert page_count >= 2
    assert extracted_text.count("Valeur exacte") >= 2

    bbox = await asyncio.to_thread(
        subprocess.run,
        (pdftotext, "-bbox", str(pdf_path), "-"),
        capture_output=True,
        check=True,
        timeout=10,
    )
    root = ET.fromstring(bbox.stdout)
    pages = [element for element in root.iter() if element.tag.rsplit("}", 1)[-1] == "page"]
    assert pages
    for page in pages:
        page_width = float(page.attrib["width"])
        for word in page.iter():
            if word.tag.rsplit("}", 1)[-1] == "word":
                assert 0 <= float(word.attrib["xMin"])
                assert float(word.attrib["xMax"]) <= page_width

    raster_prefix = tmp_path / "ioc-page"
    await asyncio.to_thread(
        subprocess.run,
        (pdftoppm, "-png", "-r", "110", str(pdf_path), str(raster_prefix)),
        capture_output=True,
        check=True,
        timeout=20,
    )
    rasterized = await asyncio.to_thread(lambda: tuple(tmp_path.glob("ioc-page-*.png")))
    assert len(rasterized) == page_count
