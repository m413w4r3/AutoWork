from __future__ import annotations

import asyncio
import hashlib
import html
import json
import os
import re
import shutil
import subprocess
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path
from uuid import UUID

import pytest

from cti_app.application.edition_typst_rendering import EditionTypstRenderer
from cti_app.application.production_synthesis import canonical_extraction_hash
from cti_app.application.publication_builder import build_publication_document_v5
from cti_app.application.typst_compilation import (
    TypstCompileRequest,
    load_font_bundle_snapshot,
    materialize_font_bundle,
)
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
    PublicationIOCDisposition,
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
from tests.test_edition_typst_rendering import (
    _EDITION_MANIFEST,
    _EDITION_RENDER_LABEL,
    _edition_document,
    _note_block_v6,
)
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
    assert any(
        indicator.normalized_value == "repaired.example"
        for group in document.indicators
        for indicator in group.indicators
    )
    assert all(item.value != "repaired.example" for item in document.ioc_details)
    assert all(section["type"] != "technical_annex" for section in model.content_sections)
    assert model.article_format is None


def test_reader_projection_drops_ioc_detail_rows_but_canonical_keeps_details() -> None:
    document, _values = _ioc_document()

    model = project_publication_to_typst_model(document)
    assert model.article_format is None
    assert all(
        section["type"] not in {"chronology", "technical_annex", "provenance"}
        for section in model.content_sections
    )

    payload = serialize_publication_document(document)
    restored = publication_document_from_json(payload)
    assert isinstance(restored, PublicationDocumentV5)
    assert restored.ioc_details == document.ioc_details
    assert serialize_publication_document(restored) == payload


def test_canonical_ioc_details_keep_independent_source_roles_without_reader_rows() -> None:
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
    assert all(section["type"] != "technical_annex" for section in model.content_sections)
    assert model.article_format is None
    assert (
        len(
            [
                item
                for item in document.ioc_details
                if item.normalized_value == primary.normalized_value
            ]
        )
        == 2
    )
    assert "Preuve, contexte et décision" not in str(model.content_sections[0])


def test_scheme_less_c2_url_reaches_ioc_list_and_invalid_urls_are_counted() -> None:
    snapshot, references, extraction, synthesis = _canonical_inputs()
    source = extraction.sources[0]
    values = (
        "www.xionamiconm[.]com:8050/ws/",
        "bad host/path",
        ":8050/ws/",
        "www.example.com:notaport/ws/",
        "this is not a URL",
        " www.leading.example/ws/ ",
    )
    indicators = tuple(
        ExtractionIndicatorV1(
            value=value,
            artifact_type=ArtifactType.URL,
            indicator_status=ExtractionIndicatorStatus.CONFIRMED_IOC,
            context="C2 infrastructure",
            evidence_quote=value,
            evidence_basis=ProductionEvidenceBasis.SOURCE_VERIFIED,
            source_document_ids=(source.source_document_id,),
        )
        for value in values
    )
    extraction = replace(extraction, sources=(replace(source, indicators=indicators),))
    synthesis = replace(synthesis, extraction_hash=canonical_extraction_hash(extraction))
    document = build_publication_document_v5(
        snapshot=snapshot,
        references=references,
        extraction=extraction,
        synthesis=synthesis,
        editorial_enrichment=_enrichment_with(extraction=extraction, synthesis=synthesis),
    )

    model = project_publication_to_typst_model(document)
    ioc_section = next(section for section in model.content_sections if section["type"] == "ioc")
    article_items = [item for group in ioc_section["source_indicators"] for item in group["items"]]
    displayed_values = {
        item["value"]["text"] if isinstance(item["value"], dict) else str(item["value"])
        for item in article_items
    }

    assert "www.xionamiconm.com:8050/ws/" in displayed_values
    assert "omitted_invalid_url_count" not in ioc_section
    assert all(
        set(item).issubset({"value", "value_spans"})
        for group in ioc_section["source_indicators"]
        for item in group["items"]
    )


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
    display_items = [item for group in ioc_section["source_indicators"] for item in group["items"]]

    def display_value(item: dict[str, object]) -> str:
        value = item["value"]
        return value["text"] if isinstance(value, dict) else str(value)

    projected_values = {display_value(item) for item in display_items}
    assert "dedup.example.invalid" in projected_values
    assert "198.51.100.42" in projected_values
    assert all(set(item).issubset({"value", "value_spans"}) for item in display_items)


def test_reader_projection_hides_ioc_disposition_while_canonical_keeps_it() -> None:
    document, _values = _ioc_document()
    model = project_publication_to_typst_model(document)

    assert model.article_format is None
    assert all(
        section["type"] not in {"chronology", "technical_annex", "provenance"}
        for section in model.content_sections
    )
    projected_json = json.dumps(
        {"content_sections": model.content_sections, "article_format": model.article_format},
        ensure_ascii=False,
    ).casefold()
    assert "retained" not in projected_json
    assert "source_verified" not in projected_json
    assert "indicator_status" not in projected_json
    assert "source_role" not in projected_json

    restored = publication_document_from_json(serialize_publication_document(document))
    assert isinstance(restored, PublicationDocumentV5)
    assert restored.ioc_details == document.ioc_details
    assert any(
        item.disposition is PublicationIOCDisposition.RETAINED for item in restored.ioc_details
    )
    assert any(item.evidence_quote for item in restored.ioc_details)


# ---------------------------------------------------------------------------
# R6-F2 — a note ioc_table must not let « Observé le » overlap « Empreinte »
# ---------------------------------------------------------------------------

_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
_WORD_BBOX = re.compile(
    r'<word xMin="(?P<xmin>[0-9.]+)" yMin="(?P<ymin>[0-9.]+)" '
    r'xMax="(?P<xmax>[0-9.]+)" yMax="(?P<ymax>[0-9.]+)">(?P<text>[^<]*)</word>'
)


def _compile_rendered_pdf(tmp_path: Path, source, bundle, binary: str) -> bytes:
    workspace = tmp_path / "workspace"
    for template_file in bundle.files:
        destination = workspace / template_file.relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(template_file.content)
    render_data_path = workspace / source.render_data_relative_path
    render_data_path.parent.mkdir(parents=True, exist_ok=True)
    render_data_path.write_bytes(source.render_data_bytes)
    font_root = Path(os.environ.get("FONT_BUNDLE_ROOT", str(_REPOSITORY_ROOT / "chpTypst")))
    font_snapshot = load_font_bundle_snapshot(
        font_root, _REPOSITORY_ROOT / "infra" / "typst-fonts.lock"
    )
    font_paths = materialize_font_bundle(font_snapshot, tmp_path / "fonts")
    compiled = asyncio.run(
        TypstSubprocessCompiler(binary=binary).compile(
            TypstCompileRequest(
                workspace_root=workspace,
                entrypoint_relative_path=source.entrypoint_relative_path,
                font_paths=font_paths,
            )
        )
    )
    return compiled.content


def _word_bboxes(pdf_path: Path) -> list[tuple[str, float, float, float, float]]:
    extracted = subprocess.run(
        ["pdftotext", "-bbox", str(pdf_path), "-"],
        capture_output=True,
        check=True,
    ).stdout.decode("utf-8", errors="replace")
    return [
        (
            html.unescape(match.group("text")),
            float(match.group("xmin")),
            float(match.group("ymin")),
            float(match.group("xmax")),
            float(match.group("ymax")),
        )
        for match in _WORD_BBOX.finditer(extracted)
    ]


def _assert_observed_at_does_not_overlap_fingerprint(
    pdf_path: Path, *, observed_at: str, fingerprint: str
) -> None:
    """Fail when a date glyph box crosses into the fingerprint column on its line."""
    words = _word_bboxes(pdf_path)
    date_words = [
        (text, xmin, ymin, xmax, ymax)
        for text, xmin, ymin, xmax, ymax in words
        if text and text in observed_at
    ]
    hash_words = [
        (text, xmin, ymin, xmax, ymax)
        for text, xmin, ymin, xmax, ymax in words
        if len(text) >= 8 and text in fingerprint
    ]
    assert date_words, f"the rendered timestamp {observed_at!r} was not found"
    assert hash_words, f"the rendered fingerprint {fingerprint!r} was not found"
    overlaps = [
        (date_word[0], date_word[3], hash_word[0], hash_word[1])
        for date_word in date_words
        for hash_word in hash_words
        if date_word[1] < hash_word[3]
        and hash_word[1] < date_word[3]
        and date_word[2] < hash_word[4]
        and hash_word[2] < date_word[4]
        and date_word[3] > hash_word[1] + 0.5
    ]
    assert not overlaps, (
        f"the observed-at cell overflows into the fingerprint column on the same line: {overlaps}"
    )


def _typst_and_pdftotext() -> str:
    binary = os.environ.get("TYPST_BINARY") or str(
        Path.home() / ".local" / "share" / "autowork-tools" / "typst"
    )
    if (shutil.which(binary) is None and not Path(binary).is_file()) or (
        shutil.which("pdftotext") is None
    ):
        pytest.skip("pinned Typst 0.15.1 and pdftotext are required for the layout oracle")
    version = subprocess.run(
        (binary, "--version"), capture_output=True, check=True, text=True, timeout=10
    ).stdout.strip()
    assert version.startswith("typst 0.15.1"), version
    return binary


def _note_ioc_table_pdf_fixture():
    publication, context, _raw = _note_block_v6()
    ioc_observation = context.observations[1]
    assert ioc_observation.observed_at == datetime(2026, 11, 1, tzinfo=UTC)
    assert ioc_observation.sha256 == "f" * 64
    return publication, context


def test_note_ioc_table_columns_do_not_overlap_in_opinion_pdf(tmp_path: Path) -> None:
    """R6-F2: the opinion renders the exact timestamp inside its own cell."""
    binary = _typst_and_pdftotext()
    publication, context = _note_ioc_table_pdf_fixture()
    # Microsecond timestamps are the longest ISO form the cell must hold.
    microseconds = datetime(2026, 11, 20, 10, 0, 0, 123456, tzinfo=UTC)
    context = replace(
        context,
        observations=(
            context.observations[0],
            replace(context.observations[1], observed_at=microseconds),
        ),
    )
    bundle = load_template_bundle(_REPOSITORY_ROOT / "chpTypst")
    source = TypstRenderer().render(
        publication,
        bundle,
        document_label="Avis — table IOC",
        note_block_context=context,
    )
    pdf_path = tmp_path / "opinion.pdf"
    pdf_path.write_bytes(_compile_rendered_pdf(tmp_path, source, bundle, binary))
    _assert_observed_at_does_not_overlap_fingerprint(
        pdf_path,
        observed_at=microseconds.isoformat(),
        fingerprint="f" * 64,
    )


def test_note_ioc_table_columns_do_not_overlap_in_edition_pdf(tmp_path: Path) -> None:
    """R6-F2: the bulletin renders the same table without overlapping columns."""
    binary = _typst_and_pdftotext()
    publication, context = _note_ioc_table_pdf_fixture()
    edition = _edition_document(((1, publication),))
    bundle = load_template_bundle(_REPOSITORY_ROOT / "chpTypst", manifest_name=_EDITION_MANIFEST)
    source = EditionTypstRenderer().render(
        edition,
        bundle,
        document_label=_EDITION_RENDER_LABEL,
        note_block_contexts={publication.subject_id: context},
    )
    pdf_path = tmp_path / "edition.pdf"
    pdf_path.write_bytes(_compile_rendered_pdf(tmp_path, source, bundle, binary))
    _assert_observed_at_does_not_overlap_fingerprint(
        pdf_path,
        observed_at=context.observations[1].observed_at.isoformat(),
        fingerprint="f" * 64,
    )
