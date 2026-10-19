import asyncio
import json
import os
import shutil
import subprocess
import unicodedata
import xml.etree.ElementTree as ET
from dataclasses import replace
from datetime import date
from pathlib import Path
from uuid import UUID

import pytest
from pypdf import PdfReader

from cti_app.application.production_normalization import display_indicator_value
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
from cti_app.domain.discovery import SourceRole
from cti_app.domain.production_extraction import (
    ExtractionIndicatorStatus,
    ExtractionIndicatorV1,
    ProductionEvidenceBasis,
)
from cti_app.domain.publication import ArtifactType, PublicationReferenceEntryV1
from cti_app.domain.publication_document import (
    PublicationDocumentV5,
    order_publication_references,
    publication_document_from_json,
    publication_document_text_anchors,
    serialize_publication_document,
)
from cti_app.domain.semantic_annotation import (
    SemanticParagraphV1,
    SemanticRole,
    SemanticTextSpanV1,
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


def _ioc_document_with_58_unique_values():
    document, values = _ioc_document()
    shared_context = "One shared long source context for the complete synthetic IOC set."
    shared_quote = "The source repeats one long campaign evidence passage for these values."
    document = replace(
        document,
        ioc_details=tuple(
            replace(item, context=shared_context, evidence_quote=shared_quote)
            for item in document.ioc_details
        ),
    )
    template = next(
        item for item in document.ioc_details if item.artifact_type is ArtifactType.HASH
    )
    added = tuple(
        replace(
            template,
            value=f"{index:064x}",
            normalized_value=f"{index:064x}",
            context=shared_context,
            evidence_quote=shared_quote,
        )
        for index in range(25, 55)
    )
    repeated_roles = tuple(
        replace(item, source_role=SourceRole.INDEPENDENT)
        for item in (*document.ioc_details, *added)[:12]
    )
    return (
        replace(document, ioc_details=(*document.ioc_details, *added, *repeated_roles)),
        values,
    )


def _ioc_document_with_four_published_references(document: PublicationDocumentV5):
    template_source = document.document.sources[0]
    sources = []
    references = list(document.references)
    for index in range(2, 5):
        source_id = UUID(int=100 + index)
        title = f"Published source {index}"
        publisher = f"Publisher {index}"
        source = replace(
            template_source,
            source_document_id=source_id,
            canonical_url=f"https://example.com/report-{index}",
            title=title,
            publisher=publisher,
            published_at=date(2025, 1, index),
        )
        sources.append(source)
        references.append(
            PublicationReferenceEntryV1(
                source_document_id=source_id,
                text=f"{title} — {publisher}",
            )
        )
    publication = replace(
        document.document,
        sources=(*document.document.sources, *sources),
        additional_source_ids=document.document.additional_source_ids
        | frozenset(source.source_document_id for source in sources),
    )
    ordered_references = order_publication_references(tuple(references), publication.sources)
    expected_anchors = publication_document_text_anchors(publication, references=ordered_references)
    prior_by_anchor = {
        paragraph.anchor: paragraph for paragraph in document.semantic_text.paragraphs
    }
    prior_by_text = {paragraph.text: paragraph for paragraph in document.semantic_text.paragraphs}
    semantic_by_anchor = {}
    for anchor, text in expected_anchors.items():
        paragraph = prior_by_anchor.get(anchor)
        if paragraph is None or paragraph.text != text:
            paragraph = prior_by_text.get(text)
        semantic_by_anchor[anchor] = (
            replace(paragraph, anchor=anchor)
            if paragraph is not None
            else SemanticParagraphV1(anchor, (SemanticTextSpanV1(SemanticRole.TEXT, text),))
        )
    semantic_text = replace(document.semantic_text, paragraphs=tuple(semantic_by_anchor.values()))
    return replace(
        document,
        document=publication,
        semantic_text=semantic_text,
        references=ordered_references,
    )


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

    model = project_publication_to_typst_model(document)
    annex = next(
        section for section in model.content_sections if section["type"] == "technical_annex"
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
    assert "repaired[.]example" in {row["value"] for row in annex["ioc_rows"]}


def test_typst_ioc_table_keeps_full_values_and_evidence_disposition() -> None:
    document, values = _ioc_document()

    model = project_publication_to_typst_model(document)
    annex = next(
        section for section in model.content_sections if section["type"] == "technical_annex"
    )
    rows = annex["ioc_rows"]

    rows_by_value = {row["value"]: row for row in rows}
    assert set(rows_by_value) == {
        display_indicator_value(value, kind, defanged=True)
        for kind, value, _context, _status in values
    }
    assert all(
        row["source_refs"] == [{"number": 1, "url": "https://example.com/report"}] for row in rows
    )
    assert all(row["roles"] for row in rows)
    references = "\n".join(item["text"] for item in model.content_sections[0]["timeline"])
    assert "Preuve, contexte et décision" not in references
    assert "Fiabilité : source_verified" not in references
    row_summaries = "\n".join(row["context_summary"] for row in rows)
    assert "fiab: source_verified" in row_summaries
    assert "ctx:" in row_summaries
    assert "preuve: La source documente…" in row_summaries
    contextual_value = "debug[.]example[.]invalid"
    assert values[2][2][:20] in row_summaries
    assert rows_by_value[values[0][1]]["status_label"] == "Retenu"
    assert rows_by_value[contextual_value]["status_label"] == "Incertain"
    assert rows_by_value[values[3][1]]["status_label"] == "Écarté"
    assert "type non publié" in rows_by_value[values[3][1]]["context_summary"]

    payload = serialize_publication_document(document)
    restored = publication_document_from_json(payload)
    assert isinstance(restored, PublicationDocumentV5)
    assert restored.ioc_details == document.ioc_details
    assert serialize_publication_document(restored) == payload


def test_ioc_table_merges_roles_for_one_canonical_value_without_dropping_provenance() -> None:
    document, _values = _ioc_document()
    primary = next(item for item in document.ioc_details if item.artifact_type is ArtifactType.HASH)
    independent = replace(
        primary,
        source_role=SourceRole.INDEPENDENT,
        context="Same hash independently reported in a second source",
        evidence_quote="The source independently reports the same SHA-256.",
    )
    document = replace(document, ioc_details=(*document.ioc_details, independent))

    model = project_publication_to_typst_model(document)
    annex = next(
        section for section in model.content_sections if section["type"] == "technical_annex"
    )
    matching = [row for row in annex["ioc_rows"] if row["value"] == primary.normalized_value]

    assert len(matching) == 1
    row = matching[0]
    assert set(row["roles"]) == {SourceRole.PRIMARY.value, SourceRole.INDEPENDENT.value}
    summary = row["context_summary"]
    assert "primary/independent" in summary
    assert "Hash SHA-256 n° 1 relevé" in summary
    assert "Same hash independently" in summary
    assert "lien établi" in summary
    assert "preuve:" in summary
    assert "fiab: source_verified" in summary
    assert "Preuve, contexte et décision" not in str(model.content_sections[0])


@pytest.mark.asyncio
async def test_real_typst_ioc_table_keeps_values_and_fits_a4_pages(tmp_path: Path) -> None:
    document, values = _ioc_document_with_58_unique_values()
    document = _ioc_document_with_four_published_references(document)
    chp_typst_root, font_bundle_root, font_lock_path = typst_bundle_paths()
    template_bundle = load_template_bundle(chp_typst_root)
    render_source = TypstRenderer().render(document, template_bundle)
    render_data = json.loads(render_source.render_data_bytes)
    technical_annex = next(
        section
        for section in render_data["content_sections"]
        if section["type"] == "technical_annex"
    )
    assert len(technical_annex["ioc_rows"]) == 58
    assert len({(row["type"], row["value"]) for row in technical_annex["ioc_rows"]}) == 58
    references = next(
        section for section in render_data["content_sections"] if section["type"] == "references"
    )
    assert len(references["timeline"]) == 4
    assert all(
        any(f"Published source {index}" in item["text"] for item in references["timeline"])
        for index in range(2, 5)
    )
    assert all(
        "Preuve, contexte et décision" not in item["text"] for item in references["timeline"]
    )

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

    audit_root = Path(os.environ.get("TMPDIR", "/tmp")) / "lotL2" / "regression-after" / "ioc"
    audit_root.mkdir(parents=True, exist_ok=True)
    pdf_path = audit_root / "ioc-table.pdf"
    pdf_path.write_bytes(compiled.content)
    pdftotext = shutil.which("pdftotext")
    pdftoppm = shutil.which("pdftoppm")
    assert pdftotext is not None and pdftoppm is not None
    text_result = await asyncio.to_thread(
        subprocess.run,
        (pdftotext, "-layout", str(pdf_path), "-"),
        capture_output=True,
        check=True,
        timeout=10,
    )
    extracted_text = text_result.stdout.decode("utf-8", errors="replace")
    references_text = extracted_text.split("RÉFÉRENCES", 1)[1].split("SYNTHÈSE", 1)[0]
    before_synthesis = extracted_text.split("SYNTHÈSE", 1)[0]
    assert "Preuve, contexte et décision" not in references_text
    assert "Preuve, contexte et décision" not in before_synthesis
    assert len([line for line in references_text.splitlines() if line.strip()]) <= 25
    raw_text_result = await asyncio.to_thread(
        subprocess.run,
        (pdftotext, "-raw", str(pdf_path), "-"),
        capture_output=True,
        check=True,
        timeout=10,
    )
    raw_text = raw_text_result.stdout.decode("utf-8", errors="replace")
    normalized_text = "".join(
        character
        for character in raw_text
        if not character.isspace() and unicodedata.category(character) != "Cf"
    )
    expected_values = {
        display_indicator_value(value, kind, defanged=True)
        for kind, value, _context, _status in values
    } | {f"{index:064x}" for index in range(25, 55)}
    assert len(expected_values) == 58
    for value in expected_values:
        assert value in normalized_text
    assert "Thesourcerepeatsonelongcampaignevidencepassageforthesevalues." not in normalized_text
    assert normalized_text.count("Thesourcerepeatsone…") >= 58
    assert "IOC—indicateurs,contexteetprovenance" in normalized_text
    assert "Type" in extracted_text
    assert "Valeurdéfanguée" in normalized_text
    assert "Contexteetstatut" in normalized_text
    assert "Source[n]" in normalized_text
    assert "fiab:source_verified" in normalized_text
    assert "TLPnonprécisé" in normalized_text
    assert "Retenu" in extracted_text
    assert "Écarté" in extracted_text
    assert "Incertain" in extracted_text
    assert "Retenu" in extracted_text
    page_count = len(PdfReader(pdf_path, strict=True).pages)
    assert 3 <= page_count <= 5
    assert extracted_text.count("Type") >= page_count

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

    raster_prefix = audit_root / "page"
    await asyncio.to_thread(
        subprocess.run,
        (pdftoppm, "-png", "-r", "110", str(pdf_path), str(raster_prefix)),
        capture_output=True,
        check=True,
        timeout=20,
    )
    rasterized = await asyncio.to_thread(lambda: tuple(audit_root.glob("page-*.png")))
    assert len(rasterized) == page_count
