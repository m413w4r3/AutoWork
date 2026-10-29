"""W17: render and inspect a source-led analyst-opinion draft with real Typst."""

from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
import subprocess
import tempfile
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import pytest

from cti_app.application.production_synthesis import canonical_extraction_hash
from cti_app.application.publication_rendering import PublicationRenderService
from cti_app.application.typst_paths import typst_bundle_paths
from cti_app.application.typst_rendering import (
    TypstDraftPreviewDocument,
    TypstRenderer,
    project_publication_to_typst_model,
)
from cti_app.domain.analyst_opinion import (
    AnalystObservationRelation,
    AnalystObservationState,
    AnalystObservationV1,
    AnalystOpinionStatus,
    AnalystOpinionV1,
    OpinionBlock,
    OpinionBlockType,
    OpinionClaimQualification,
    opinion_blocks_with_observations,
)
from cti_app.domain.investigation import (
    EvidenceKind as InvestigationEvidenceKind,
)
from cti_app.domain.investigation import (
    EvidenceObservation,
    FindingAssessment,
    FindingKind,
    InvestigationFinding,
    NoveltyClass,
)
from cti_app.domain.production import ProductionEvidenceBasis
from cti_app.domain.production_extraction import (
    ExtractionEventV1,
    ExtractionIndicatorStatus,
    ExtractionIndicatorV1,
)
from cti_app.domain.production_synthesis import (
    EvidenceKind,
    ProductionSynthesisV1,
    SynthesisParagraphV1,
    SynthesisSectionKind,
    SynthesisSectionV1,
    SynthesisTimelineEntryV1,
    SynthesisUncertaintyV1,
    extraction_evidence_refs_v1,
)
from cti_app.domain.publication import ArtifactType
from cti_app.domain.publication_document import (
    PublicationDocumentV5,
    publication_document_v5_sha256,
)
from cti_app.infrastructure.typst_compiler import TypstSubprocessCompiler
from tests.test_publication_builder_v4 import (
    _canonical_inputs,
    _enrichment_with,
    build_publication_document_v5,
)


def _repository_temp_root() -> Path:
    return Path(__file__).resolve().parents[2] / ".tmp-typst-w17"


def _inspect_rendered_pdf(
    content: bytes,
    capture_path: Path,
    pdftotext: str,
    pdftoppm: str,
) -> tuple[str, tuple[Path, ...]]:
    capture_path.parent.mkdir(parents=True, exist_ok=True)
    capture_path.write_bytes(content)
    text_result = subprocess.run(
        [pdftotext, "-layout", str(capture_path), "-"],
        check=True,
        capture_output=True,
        timeout=30,
    )
    image_prefix = capture_path.with_suffix("")
    subprocess.run(
        [pdftoppm, "-png", "-r", "120", str(capture_path), str(image_prefix)],
        check=True,
        capture_output=True,
        timeout=30,
    )
    images = tuple(capture_path.parent.glob(f"{image_prefix.name}-*.png"))
    return text_result.stdout.decode("utf-8", errors="replace"), images


def _document_and_opinion() -> tuple[
    PublicationDocumentV5,
    AnalystOpinionV1,
    ProductionSynthesisV1,
    tuple[InvestigationFinding, ...],
    tuple[EvidenceObservation, ...],
]:
    snapshot, references, extraction, synthesis = _canonical_inputs()
    source_id = extraction.sources[0].source_document_id
    event = ExtractionEventV1(
        event_date=date(2025, 1, 15),
        date_text="15 janvier 2025",
        text="Le rapport indique l'observation du domaine alpha.example.",
        context="Chronologie de l'infrastructure citée.",
        evidence_quote="alpha.example a été observé le 15 janvier 2025.",
        evidence_basis=ProductionEvidenceBasis.SOURCE_VERIFIED,
        source_document_ids=(source_id,),
    )
    indicator = ExtractionIndicatorV1(
        value="alpha.example",
        artifact_type=ArtifactType.DOMAIN,
        indicator_status=ExtractionIndicatorStatus.CONFIRMED_IOC,
        context="Domaine observé dans le rapport; attribution non établie.",
        evidence_quote="Le rapport mentionne alpha.example.",
        evidence_basis=ProductionEvidenceBasis.SOURCE_VERIFIED,
        source_document_ids=(source_id,),
    )
    extracted_source = replace(
        extraction.sources[0],
        events=(event,),
        indicators=(indicator,),
    )
    extraction = replace(extraction, sources=(extracted_source,))
    evidence_refs = extraction_evidence_refs_v1(extraction)
    fact_ref = next(ref for ref in evidence_refs if ref.kind is EvidenceKind.FACT)
    event_ref = next(ref for ref in evidence_refs if ref.kind is EvidenceKind.EVENT)
    indicator_ref = next(ref for ref in evidence_refs if ref.kind is EvidenceKind.INDICATOR)
    synthesis = replace(
        synthesis,
        title="Rapport de synthèse sur Example actor",
        lead=(
            SynthesisParagraphV1(
                "Le rapport décrit une infrastructure associée à Example actor.",
                (fact_ref,),
            ),
        ),
        sections=(
            SynthesisSectionV1(
                SynthesisSectionKind.OVERVIEW,
                "Synthèse",
                (
                    SynthesisParagraphV1(
                        "La source identifie Example actor et décrit son activité.",
                        (fact_ref,),
                    ),
                ),
            ),
            SynthesisSectionV1(
                SynthesisSectionKind.TECHNICAL,
                "Infrastructure réseau",
                (
                    SynthesisParagraphV1(
                        "Le rapport mentionne alpha.example comme domaine observé.",
                        (indicator_ref,),
                    ),
                ),
            ),
        ),
        timeline=(
            SynthesisTimelineEntryV1(
                event_date=date(2025, 1, 15),
                date_text="15 janvier 2025",
                text="Le domaine alpha.example est mentionné dans le rapport.",
                evidence_refs=(event_ref,),
            ),
        ),
        uncertainties=(
            SynthesisUncertaintyV1(
                "La source ne permet pas d'établir l'attribution opérationnelle du domaine.",
                (source_id,),
            ),
        ),
        extraction_hash=canonical_extraction_hash(extraction),
    )
    document = build_publication_document_v5(
        snapshot=snapshot,
        references=references,
        extraction=extraction,
        synthesis=synthesis,
        editorial_enrichment=_enrichment_with(extraction=extraction, synthesis=synthesis),
    )

    investigation_id = UUID(int=1701)
    observation_id = UUID(int=1702)
    finding_id = UUID(int=1703)
    observation = EvidenceObservation(
        investigation_id=investigation_id,
        kind=InvestigationEvidenceKind.OTHER,
        blob_id=UUID(int=1704),
        sha256="d" * 64,
        source="fixture-local-dns-capture",
        source_version="fixture-v1",
        observed_at=datetime(2025, 1, 16, 12, 0, tzinfo=UTC),
        object_type="dns_answer",
        object_id="alpha.example",
        limitations=(
            "Une réponse DNS ponctuelle ne démontre ni contrôle opérationnel ni attribution.",
        ),
        id=observation_id,
    )
    finding = InvestigationFinding(
        investigation_id=investigation_id,
        kind=FindingKind.NETWORK_BEHAVIOUR,
        assessment=FindingAssessment.SUPPORTED,
        statement="Une réponse DNS isolée associe alpha.example à 203.0.113.24.",
        observation_refs=(observation_id,),
        limitations=(
            "L'adresse est réservée à la documentation; l'observation ne prouve "
            "pas une attribution.",
        ),
        id=finding_id,
    )
    note = AnalystObservationV1(
        id=UUID(int=1705),
        anchor="section:1:paragraph:0001",
        text=(
            "Une résolution DNS isolée ajoute un indice d'infrastructure absent du rapport; "
            "elle ne démontre ni contrôle opérationnel ni attribution à l'acteur."
        ),
        state=AnalystObservationState.INFERRED,
        relation=AnalystObservationRelation.QUALIFIES,
        finding_refs=(finding_id,),
        evidence_refs=(observation_id,),
    )
    opinion = AnalystOpinionV1(
        id=UUID(int=1706),
        version=2,
        investigation_id=investigation_id,
        synthesis_artifact_id=UUID(int=1707),
        synthesis_sha256="e" * 64,
        base_publication_artifact_id=UUID(int=1708),
        base_publication_sha256=publication_document_v5_sha256(document),
        blocks=opinion_blocks_with_observations(
            (
                OpinionBlock(
                    block_type=OpinionBlockType.UNCERTAINTIES_AND_NEXT_LEADS,
                    text=(
                        "Une résolution DNS ponctuelle relie alpha.example à 203.0.113.24; "
                        "l'attribution opérationnelle demeure indéterminée."
                    ),
                    finding_refs=(finding_id,),
                    evidence_refs=(observation_id,),
                    qualification=OpinionClaimQualification.INFERENCE,
                    certainty=0.7,
                ),
            ),
            (note,),
        ),
        author_id="analyst:w17-fixture",
        status=AnalystOpinionStatus.DRAFT,
    )
    return document, opinion, synthesis, (finding,), (observation,)


def test_draft_new_sample_iocs_require_supported_finding_and_persisted_subject_refs() -> None:
    document, opinion, synthesis, findings, observations = _document_and_opinion()
    hashes = (
        "77e8056d432b3aa2ac47cba783cc7912fe7dbfea12c62e654e84d11c80e5f3f8",
        "0c75d6000177936adafef1e244dbab16e76362368b5ceb78caa50e0223184545",
    )
    candidate_hash = "f" * 64
    unrelated_statement_hash = "e" * 64
    sample_observations = tuple(
        replace(
            observations[0],
            id=UUID(int=1800 + index),
            object_type="file",
            object_id=value,
        )
        for index, value in enumerate((*hashes, candidate_hash), start=1)
    )
    sample_findings = (
        replace(
            findings[0],
            id=UUID(int=1901),
            kind=FindingKind.NEW_SAMPLE,
            assessment=FindingAssessment.SUPPORTED,
            novelty=NoveltyClass.NEW_TO_ARTICLE,
            statement=(
                "[UNTRUSTED ARTICLE OR TOOL DATA] Blackwood.dll.conf "
                f"{hashes[0]} and {hashes[1]} were decoded; {unrelated_statement_hash} "
                "is not cited. [END UNTRUSTED DATA]"
            ),
            scope=f"Subject reference: {hashes[0]}",
            observation_refs=(sample_observations[0].id, sample_observations[1].id),
        ),
        replace(
            findings[0],
            id=UUID(int=1902),
            kind=FindingKind.NEW_SAMPLE,
            assessment=FindingAssessment.QUALIFIED,
            novelty=NoveltyClass.NEW_TO_ARTICLE,
            statement=f"Blackwood.dll.conf {candidate_hash} is not confirmed.",
            scope=f"Subject reference: {candidate_hash}",
            observation_refs=(sample_observations[2].id,),
        ),
    )

    model = project_publication_to_typst_model(
        TypstDraftPreviewDocument(
            document,
            opinion,
            synthesis,
            findings=sample_findings,
            evidence_observations=sample_observations,
        )
    )
    ioc_section = next(item for item in model.content_sections if item["type"] == "ioc")
    original_hashes = {
        item["value"]["text"]
        for group in ioc_section["original_indicators"]
        for item in group["items"]
    }
    candidate_hashes = {
        item["value"]["text"]
        for group in ioc_section["candidate_indicators"]
        for item in group["items"]
    }
    assert original_hashes == set(hashes)
    assert candidate_hashes == {candidate_hash}
    assert unrelated_statement_hash not in original_hashes | candidate_hashes
    original_items = [
        item for group in ioc_section["original_indicators"] for item in group["items"]
    ]
    assert all("Blackwood.dll.conf" in item["context"] for item in original_items)
    assert all(len(item["draft_refs"]) == 3 for item in original_items)
    annex = next(item for item in model.content_sections if item["type"] == "technical_annex")
    provenance_ids = {item["reference_id"] for item in annex["opinion_refs"]}
    assert {str(item.id) for item in sample_findings[:2]}.issubset(provenance_ids)
    assert {str(item.id) for item in sample_observations[:2]}.issubset(provenance_ids)


def test_preview_without_eligible_findings_has_no_empty_or_invented_ioc_section() -> None:
    document, opinion, synthesis, _findings, _observations = _document_and_opinion()
    empty_document = replace(
        document,
        document=replace(document.document, indicators=()),
        original_indicators=(),
        ioc_details=(),
    )
    empty_opinion = replace(
        opinion,
        blocks=(),
        base_publication_sha256=publication_document_v5_sha256(empty_document),
        content_sha256="",
    )
    model = project_publication_to_typst_model(
        TypstDraftPreviewDocument(
            empty_document,
            empty_opinion,
            synthesis,
        )
    )
    assert all(item["type"] != "ioc" for item in model.content_sections)
    assert "f" * 64 not in str(model.content_sections)


@pytest.mark.asyncio
async def test_w17_real_typst_draft_preview_preserves_structure_and_limits(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Documented fixture: source report says alpha.example; local DNS adds one limited clue."""
    typst_binary = os.environ.get("TYPST_BINARY", "typst")
    assert shutil.which(typst_binary), "W17 requires the pinned real Typst compiler"
    pdftotext = shutil.which("pdftotext")
    pdftoppm = shutil.which("pdftoppm")
    assert pdftotext, "W17 requires Poppler pdftotext"
    assert pdftoppm, "W17 requires Poppler pdftoppm"

    document, opinion, synthesis, findings, observations = _document_and_opinion()
    projection = TypstDraftPreviewDocument(document, opinion, synthesis)
    model = project_publication_to_typst_model(projection)
    body = next(item for item in model.content_sections if item["type"] == "synthesis")["blocks"]
    technical_paragraph = next(
        index
        for index, item in enumerate(body)
        if item.get("type") == "paragraph"
        and item.get("text") == "Le rapport mentionne alpha.example comme domaine observé."
    )
    assert body[technical_paragraph + 1]["type"] == "analyst_observation"
    chronology = next(item for item in model.content_sections if item["type"] == "chronology")
    assert chronology["timeline"][0]["display_date"] == "15 janvier 2025"
    assert chronology["timeline"][0]["source_urls"] == ["https://example.com/report"]
    empty_opinion = replace(opinion, blocks=(), content_sha256="")
    empty_model = project_publication_to_typst_model(
        TypstDraftPreviewDocument(document, empty_opinion, synthesis)
    )
    assert all(item["type"] != "analyst_opinion" for item in empty_model.content_sections)
    assert any(group.artifact_type is ArtifactType.DOMAIN for group in document.indicators)
    assert synthesis.uncertainties
    assert any(item["type"] == "limitations" for item in model.content_sections)
    assert findings[0].observation_refs == (observations[0].id,)

    template_root, font_root, fonts_lock = typst_bundle_paths()
    repository_tmp = _repository_temp_root()
    await asyncio.to_thread(repository_tmp.mkdir, parents=True, exist_ok=True)
    monkeypatch.setenv("TMPDIR", str(repository_tmp))
    tempfile.tempdir = None
    service = PublicationRenderService(
        uow_factory=cast(Any, object()),
        artifact_store=cast(Any, object()),
        media_asset_store=cast(Any, object()),
        renderer=TypstRenderer(),
        compiler=TypstSubprocessCompiler(binary=typst_binary),
        chp_typst_root=template_root,
        font_bundle_root=font_root,
        typst_fonts_lock_path=fonts_lock,
    )
    try:
        draft_render = await service.render_draft_preview(document, opinion, synthesis)
    finally:
        tempfile.tempdir = None

    compiled = draft_render.compiled_document
    expected_sha256 = hashlib.sha256(compiled.content).hexdigest()
    assert compiled.sha256 == expected_sha256
    capture_path = Path(
        os.environ.get("AUTOWORK_LOTC_PREVIEW_PATH", str(tmp_path / "w17-draft-preview.pdf"))
    )
    extracted_text, rendered_pages = await asyncio.to_thread(
        _inspect_rendered_pdf,
        compiled.content,
        capture_path,
        pdftotext,
        pdftoppm,
    )
    text = " ".join(extracted_text.split())
    normalized_text = text.casefold().replace("\u2019", "'")
    for required in (
        "BROUILLON NON APPROUVÉ",
        "Synthèse",
        "Infrastructure réseau",
        "CHRONOLOGIE",
        "AVIS DE L'ANALYSTE",
        "Incertitudes et prochaines pistes",
        "15 janvier 2025",
        "alpha.example",
        "Le rapport mentionne alpha.example comme domaine observé",
        "LIMITES DES PREUVES",
        "La source ne permet pas d'établir l'attribution opérationnelle",
        "Inférence de l'analyste",
        "ne démontre ni contrôle opérationnel ni attribution",
        "203.0.113.24",
    ):
        assert required.casefold() in normalized_text, f"W17 PDF text is missing {required!r}"

    reference_text = normalized_text.split("références", 1)[1].split("synthèse", 1)[0]
    # C8 renders navigable numbered references and a single ordered chronology.
    assert "1." in reference_text and "https://example.com/report" in reference_text
    assert "https://example.com/report" in reference_text
    assert "1" in reference_text, "W17 source links must keep their numbered anchor"
    chronology_text = normalized_text.split("chronologie", 1)[1].split("limites des preuves", 1)[0]
    assert "1." in chronology_text, "W17 chronology must render as a numbered list"
    assert "rapport. 1" in chronology_text
    annex_text = normalized_text.split("annexe technique", 1)[1]
    for table_heading in (
        "type",
        "valeur défanguée",
        "statut indicateur",
        "statut relation",
        "contexte et provenance",
    ):
        assert table_heading in annex_text, f"W17 IOC annex table is missing {table_heading!r}"

    assert rendered_pages
    final_page_label = f"{len(rendered_pages)}/{len(rendered_pages)}"
    assert final_page_label in normalized_text
    assert normalized_text.count("brouillon non approuvé") >= len(rendered_pages)
    source_paragraph = normalized_text.index(
        "le rapport mentionne alpha.example comme domaine observé"
    )
    anchored_note = normalized_text.index("une résolution dns isolée ajoute un indice")
    assert source_paragraph < anchored_note, "W17 analyst note must follow its anchored paragraph"
    assert len(compiled.content) > 2_000

    empty_draft_render = await service.render_draft_preview(document, empty_opinion, synthesis)
    empty_compiled = empty_draft_render.compiled_document
    empty_pdf_path = tmp_path / "w17-empty-opinion-preview.pdf"
    empty_text, _ = await asyncio.to_thread(
        _inspect_rendered_pdf,
        empty_compiled.content,
        empty_pdf_path,
        pdftotext,
        pdftoppm,
    )
    normalized_empty_text = " ".join(empty_text.split()).casefold().replace("\u2019", "'")
    assert "avis de l'analyste" not in normalized_empty_text
    assert "aucun avis publié" not in normalized_empty_text
