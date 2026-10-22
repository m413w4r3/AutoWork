import asyncio
import hashlib
import json
import os
import shutil
import subprocess
import unicodedata
import xml.etree.ElementTree as ET
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
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
    TypstDraftPreviewDocument,
    TypstRenderer,
    load_template_bundle,
    project_publication_to_typst_model,
)
from cti_app.domain.analyst_opinion import (
    AnalystObservationRelation,
    AnalystObservationState,
    AnalystObservationV1,
    AnalystOpinionStatus,
    AnalystOpinionV1,
    opinion_blocks_with_observations,
)
from cti_app.domain.discovery import SourceRole
from cti_app.domain.investigation import EvidenceKind, EvidenceObservation
from cti_app.domain.production_extraction import (
    ExtractionIndicatorStatus,
    ExtractionIndicatorV1,
    ProductionEvidenceBasis,
)
from cti_app.domain.publication import (
    ArtifactType,
    PublicationIndicatorGroupV1,
    PublicationIndicatorV1,
    PublicationReferenceEntryV1,
)
from cti_app.domain.publication_document import (
    PublicationDocumentV5,
    order_publication_references,
    publication_document_from_json,
    publication_document_text_anchors,
    publication_document_v5_sha256,
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
    reference_target = model.content_sections[0]["entries"][0]["target_id"]
    assert all(
        row["source_refs"]
        == [
            {
                "number": 1,
                "url": "https://example.com/report",
                "target_id": reference_target,
            }
        ]
        for row in rows
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
    assert "Same hash…" in summary
    assert "lien établi" in summary
    assert "Preuve, contexte et décision" not in str(model.content_sections[0])


def test_ioc_groups_deduplicate_candidate_against_article_and_original_statuses() -> None:
    base, _values = _ioc_document()
    source_id = base.document.sources[0].source_document_id
    article_domain = "dedup.example.invalid"
    original_ip = "198.51.100.42"
    indicator_groups = (
        *(
            group
            for group in base.document.indicators
            if group.artifact_type is not ArtifactType.DOMAIN
        ),
        PublicationIndicatorGroupV1(
            ArtifactType.DOMAIN,
            (
                PublicationIndicatorV1(
                    value=article_domain,
                    normalized_value=article_domain,
                    artifact_type=ArtifactType.DOMAIN,
                    source_document_ids=(source_id,),
                ),
            ),
        ),
    )
    original_group = PublicationIndicatorGroupV1(
        ArtifactType.IP,
        (
            PublicationIndicatorV1(
                value=original_ip,
                normalized_value=original_ip,
                artifact_type=ArtifactType.IP,
                source_document_ids=(source_id,),
            ),
        ),
    )
    base = replace(
        base,
        document=replace(base.document, indicators=indicator_groups),
        original_indicators=(original_group,),
    )
    investigation_id = UUID(int=71_001)
    evidence_id = UUID(int=71_002)
    observation = EvidenceObservation(
        id=evidence_id,
        investigation_id=investigation_id,
        kind=EvidenceKind.CLAIM_SOURCE,
        blob_id=UUID(int=71_003),
        sha256=hashlib.sha256(b"synthetic IOC dedupe evidence").hexdigest(),
        source="synthetic fixture",
        source_version="v1",
        observed_at=datetime(2026, 10, 20, tzinfo=UTC),
        object_type="domain",
        object_id=article_domain,
    )
    note = AnalystObservationV1(
        id=UUID(int=71_004),
        anchor="lead:0001",
        text="Candidate links dedup[.]example[.]invalid and 198[.]51[.]100[.]42.",
        state=AnalystObservationState.INFERRED,
        relation=AnalystObservationRelation.QUALIFIES,
        evidence_refs=(evidence_id,),
    )
    opinion = AnalystOpinionV1(
        id=UUID(int=71_005),
        version=1,
        investigation_id=investigation_id,
        synthesis_artifact_id=UUID(int=71_006),
        synthesis_sha256=hashlib.sha256(b"synthetic synthesis").hexdigest(),
        base_publication_artifact_id=UUID(int=71_007),
        base_publication_sha256=publication_document_v5_sha256(base),
        blocks=opinion_blocks_with_observations((), (note,)),
        author_id="synthetic-fixture",
        status=AnalystOpinionStatus.DRAFT,
    )

    model = project_publication_to_typst_model(
        TypstDraftPreviewDocument(
            base_publication=base,
            analyst_opinion=opinion,
            evidence_observations=(observation,),
        )
    )
    ioc_section = next(section for section in model.content_sections if section["type"] == "ioc")
    article_items = [item for group in ioc_section["source_indicators"] for item in group["items"]]
    original_items = [
        item for group in ioc_section["original_indicators"] for item in group["items"]
    ]
    candidate_items = [
        item for group in ioc_section["candidate_indicators"] for item in group["items"]
    ]

    def display_value(item: dict[str, object]) -> str:
        value = item["value"]
        return value["text"] if isinstance(value, dict) else str(value)

    article_item = next(
        item for item in article_items if display_value(item) == "dedup[.]example[.]invalid"
    )
    original_item = next(
        item for item in original_items if display_value(item) == "198[.]51[.]100[.]42"
    )
    assert not any(
        display_value(item) in {"dedup[.]example[.]invalid", "198[.]51[.]100[.]42"}
        for item in candidate_items
    )
    assert article_item["indicator_status_label"] == "Documenté dans l'article"
    assert "Candidat non confirmé" in article_item["relationship_status_label"]
    assert original_item["indicator_status_label"] == "IOC original du run"
    assert "Candidat non confirmé" in original_item["relationship_status_label"]


@pytest.mark.asyncio
async def test_real_typst_ioc_table_keeps_values_and_fits_a4_pages(tmp_path: Path) -> None:
    document, values = _ioc_document_with_58_unique_values()
    document = _ioc_document_with_four_published_references(document)
    chp_typst_root, font_bundle_root, font_lock_path = typst_bundle_paths()
    template_bundle = load_template_bundle(chp_typst_root)
    render_source = TypstRenderer().render(
        document,
        template_bundle,
        document_label="Publication de base — artefact fixture v1",
    )
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
    traceability_refs: list[dict[str, Any]] = []
    for index in range(1, 68):
        kind = "finding" if index <= 15 else "evidence"
        reference_id = str(UUID(int=90_000 + index))
        traceability_refs.append(
            {
                "label": f"[{kind[0].upper()}{index:03d}]",
                "target_id": f"trace-{kind[0]}-{index:03d}",
                "kind_label": "Finding" if kind == "finding" else "Évidence",
                "reference_id": reference_id,
                "reference_id_spans": {"text": reference_id},
                "href": f"autowork://fixture/{kind}/{reference_id}",
            }
        )
    technical_annex["opinion_refs"] = traceability_refs
    render_data_bytes = json.dumps(
        render_data, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")

    workspace_root = tmp_path / "ioc-table-workspace"
    shutil.copytree(chp_typst_root / "RENDERER", workspace_root / "RENDERER")
    shutil.copytree(chp_typst_root / "UTILS", workspace_root / "UTILS")
    media_root = workspace_root / "RENDERER" / "media"
    media_root.mkdir()
    shutil.copy2(chp_typst_root / "UTILS" / "chap.png", media_root / "figure.png")
    (workspace_root / "RENDERER" / "render-data.json").write_bytes(render_data_bytes)

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

    audit_root = Path(os.environ.get("TMPDIR", "/tmp")) / "lotR" / "fixture-after"
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
    assert normalized_text.count("Onesharedlongsourcecontext") >= 40
    # C8 uses an IOC list in the body and labels the exhaustive table as an annex.
    assert "IOC" in normalized_text
    assert "ANNEXETECHNIQUE—inventaireexhaustifdesIOC" in normalized_text
    assert "Empreintedefichier—unevaleurparligne" in normalized_text
    assert "Statut,relationetprovenance" in normalized_text
    assert "Indicateur:" in normalized_text
    assert "Relation:" in normalized_text
    assert "src" in normalized_text
    assert "TLPnonprécisé" in normalized_text
    assert "Retenu" in extracted_text
    assert "Écarté" in extracted_text
    assert "Incertain" in extracted_text
    assert "Retenu" in extracted_text
    reader = PdfReader(pdf_path, strict=True)
    page_count = len(reader.pages)
    assert 4 <= page_count <= 7
    page_texts = [page.extract_text() or "" for page in reader.pages]
    hash_pages = [
        page_number
        for page_number, page_text in enumerate(page_texts, start=1)
        if "Empreinte de fichier" in page_text
    ]
    hash_row_page_counts = {
        page_number: sum(row["value"] in page_text for row in technical_annex["ioc_rows"])
        for page_number, page_text in enumerate(page_texts, start=1)
    }
    hash_table_page = max(hash_row_page_counts, key=hash_row_page_counts.get)
    trace_pages = [
        page_number
        for page_number, page_text in enumerate(page_texts, start=1)
        if "[F001]" in page_text or "[E016]" in page_text
    ]
    matching_trace_refs = sum(
        reference["reference_id"] in page_text
        for page_text in page_texts
        for reference in traceability_refs
    )
    assert 1 <= len(hash_pages) <= 4
    assert hash_row_page_counts[hash_table_page] >= 15
    assert len(trace_pages) == 1
    assert matching_trace_refs == 67

    for image_name, page_number in (
        ("hash-table-70dpi", hash_table_page),
        ("traceability-70dpi", trace_pages[0]),
    ):
        await asyncio.to_thread(
            subprocess.run,
            (
                pdftoppm,
                "-png",
                "-r",
                "70",
                "-f",
                str(page_number),
                "-l",
                str(page_number),
                "-singlefile",
                str(pdf_path),
                str(audit_root / image_name),
            ),
            capture_output=True,
            check=True,
            timeout=20,
        )

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
