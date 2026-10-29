from __future__ import annotations

import asyncio
import hashlib
import os
import re
import shutil
import subprocess
import xml.etree.ElementTree as ET
from dataclasses import replace
from datetime import UTC, date, datetime
from itertools import pairwise
from pathlib import Path
from uuid import UUID

import httpx
import pytest
from fastapi import FastAPI
from pypdf import PdfReader

from cti_app.application.investigation_figures import (
    build_investigation_chain_figure,
    build_investigation_graph_figure,
    build_publication_timeline_figure,
)
from cti_app.application.production_synthesis import canonical_extraction_hash
from cti_app.application.publication_augmentation import augment_publication_with_opinion
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
    TypstMediaRef,
    TypstRenderer,
    load_template_bundle,
    project_publication_to_typst_model,
)
from cti_app.domain.analyst_opinion import (
    AnalystObservationRelation,
    AnalystObservationState,
    AnalystObservationV1,
    AnalystOpinionStatus,
    opinion_blocks_with_observations,
)
from cti_app.domain.investigation_graph import (
    InvestigationGraph,
    InvestigationGraphConfidence,
    InvestigationGraphDirection,
    InvestigationGraphEdge,
    InvestigationGraphEvidenceBasis,
    InvestigationGraphEvidenceRef,
    InvestigationGraphNode,
    InvestigationGraphNodeKind,
    InvestigationGraphSource,
)
from cti_app.domain.media_assets import MediaAssetKind
from cti_app.domain.production_editorial_enrichment import (
    EnrichmentPlacementKind,
    EnrichmentPlacementV1,
    ResolvedSourceFigureV1,
    SourceFigureCandidateV1,
    SourceFigureDecision,
    SourceFigureInclusionStatus,
    SourceFigureLocatorV1,
    source_figure_id,
)
from cti_app.domain.production_extraction import ExtractionEventV1, ProductionEvidenceBasis
from cti_app.domain.production_synthesis import (
    EvidenceKind,
    SynthesisTimelineEntryV1,
    extraction_evidence_refs_v1,
)
from cti_app.domain.publication_document import publication_document_v5_sha256
from cti_app.infrastructure.d2_diagram_compiler import D2DiagramCompiler
from cti_app.infrastructure.typst_compiler import TypstSubprocessCompiler
from tests.test_publication_builder_v4 import _canonical_inputs, _enrichment_with
from tests.test_publication_document_v6 import _approved_opinion
from tests.test_source_media_collection import _png


@pytest.mark.asyncio
async def test_draft_preview_response_identifies_unapproved_status_and_hashes() -> None:
    from cti_app.api.analyst_opinion import router
    from cti_app.application.analyst_opinion_preview import AnalystOpinionPreviewService
    from tests.test_analyst_opinion_api import _IdentityProvider, _PreviewRenderer
    from tests.test_analyst_opinion_service import make_opinion_harness

    harness = make_opinion_harness()
    app = FastAPI()
    app.include_router(router)
    app.state.analyst_opinion_service = harness.service
    app.state.identity_provider = _IdentityProvider()
    app.state.analyst_opinion_preview_service = AnalystOpinionPreviewService(
        uow_factory=harness.uow_factory,  # type: ignore[arg-type]
        artifact_store=harness.store,  # type: ignore[arg-type]
        renderer=_PreviewRenderer(),
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        composed = await client.post(
            f"/api/investigations/{harness.investigation.id}/opinion/compose",
            json={"selection": {}},
            headers={"Idempotency-Key": "editorial-figure-draft"},
        )
        assert composed.status_code == 200
        draft = composed.json()["draft"]
        response = await client.get(
            f"/api/opinions/{draft['id']}/preview.pdf",
            params={
                "version": draft["version"],
                "opinion_sha256": draft["content_sha256"],
                "base_publication_sha256": draft["provenance"]["base_publication_sha256"],
            },
        )

    assert response.status_code == 200
    assert response.headers["x-opinion-status"] == "draft-unapproved"
    assert response.headers["x-pdf-sha256"] == hashlib.sha256(response.content).hexdigest()
    assert response.headers["x-opinion-content-sha256"] == draft["content_sha256"]
    assert (
        response.headers["x-base-publication-sha256"]
        == draft["provenance"]["base_publication_sha256"]
    )


def _publication_with_timeline(*, archived_image: bytes | None = None):
    snapshot, references, extraction, synthesis = _canonical_inputs()
    source_id = extraction.sources[0].source_document_id
    events = tuple(
        ExtractionEventV1(
            event_date=event_date,
            date_text=event_date.isoformat(),
            text=text,
            context="Chronologie publiée",
            evidence_quote=quote,
            evidence_basis=ProductionEvidenceBasis.SOURCE_VERIFIED,
            source_document_ids=(source_id,),
        )
        for event_date, text, quote in (
            (
                date(2025, 1, 12),
                "Premier événement observé",
                "Le premier événement survient le 12 janvier.",
            ),
            (
                date(2025, 1, 16),
                "Deuxième événement observé",
                "Le deuxième événement survient le 16 janvier.",
            ),
        )
    )
    extraction = replace(
        extraction,
        sources=(replace(extraction.sources[0], events=events),),
    )
    event_refs = tuple(
        ref for ref in extraction_evidence_refs_v1(extraction) if ref.kind is EvidenceKind.EVENT
    )
    synthesis = replace(
        synthesis,
        extraction_hash=canonical_extraction_hash(extraction),
        timeline=tuple(
            SynthesisTimelineEntryV1(
                event_date=event.event_date,
                date_text=event.date_text,
                text=event.text,
                evidence_refs=(event_refs[index],),
            )
            for index, event in enumerate(events)
        ),
    )
    source_figures = ()
    if archived_image is not None:
        source = extraction.sources[0]
        sha256 = hashlib.sha256(archived_image).hexdigest()
        locator = SourceFigureLocatorV1(page=1, figure_label="Figure 1")
        provenance = "Figure téléchargée et attribuée depuis le rapport source"
        resolved = ResolvedSourceFigureV1(
            figure_id=source_figure_id(
                source_document_id=source.source_document_id,
                sha256=sha256,
                source=source.canonical_url,
                locator=locator,
            ),
            blob_id=UUID(int=912),
            sha256=sha256,
            mime_type="image/png",
            byte_size=len(archived_image),
            source_document_id=source.source_document_id,
            source=source.canonical_url,
            provenance=provenance,
            locator=locator,
            decision=SourceFigureDecision.ACCEPTED,
            decision_reason="matched_archived_blob",
        )
        source_figures = (
            SourceFigureCandidateV1(
                key="archived-capture",
                source_document_id=source.source_document_id,
                source_url=source.canonical_url,
                caption="Capture archivée de l'infrastructure",
                provenance=provenance,
                locator=locator,
                inclusion_status=SourceFigureInclusionStatus.INCLUDED,
                placement=EnrichmentPlacementV1(EnrichmentPlacementKind.AFTER_LEAD),
                resolved_figure=resolved,
            ),
        )
    return build_publication_document_v5(
        snapshot=snapshot,
        references=references,
        extraction=extraction,
        synthesis=synthesis,
        editorial_enrichment=_enrichment_with(
            extraction=extraction,
            synthesis=synthesis,
            source_figures=source_figures,
        ),
    )


def test_archived_figure_provenance_survives_v5_and_v6_typst_projection() -> None:
    publication = _publication_with_timeline(archived_image=_png(320, 200))
    figure = publication.document.figures[0]
    opinion, context = _approved_opinion(publication)
    v6 = augment_publication_with_opinion(publication, opinion, context)

    for value in (publication, v6):
        model = project_publication_to_typst_model(value)
        figure_block = next(
            block
            for section in model.content_sections
            for block in section.get("blocks", ())
            if block.get("type") == "figure"
        )
        assert figure_block["caption"] == figure.caption
        assert "source_url" not in figure_block
        assert "provenance" not in figure_block
        assert figure_block["sha256"] == figure.sha256
        assert "source_note" in figure_block
        canonical_publication = getattr(value, "base_publication", value)
        assert canonical_publication.document.figures[0].provenance == figure.provenance


def test_derived_figures_require_canonical_dates_and_approved_citations() -> None:
    publication = _publication_with_timeline()
    timeline = build_publication_timeline_figure(publication)

    assert timeline is not None
    assert timeline["axis_unit"] == "jour calendaire"
    assert [item["date"] for item in timeline["events"]] == ["2025-01-12", "2025-01-16"]
    assert timeline["events"][0]["sources"][0]["url"] == "https://example.com/report"
    assert timeline["events"][0]["sources"][0]["source_sha256"] == "c" * 64
    assert len(timeline["events"][0]["sources"][0]["evidence_sha256"]) == 64
    assert build_publication_timeline_figure(publication) == timeline

    opinion, context = _approved_opinion(publication)
    observation = AnalystObservationV1(
        id=UUID(int=910),
        anchor="lead:0001",
        text="Le constat est appuyé par la pièce citée.",
        state=AnalystObservationState.INFERRED,
        relation=AnalystObservationRelation.QUALIFIES,
        finding_refs=(context.findings[0].id,),
        evidence_refs=(context.observations[0].id,),
    )
    opinion = replace(
        opinion,
        blocks=opinion_blocks_with_observations(opinion.blocks, (observation,)),
        content_sha256="",
    )
    v6 = augment_publication_with_opinion(publication, opinion, context)
    chain = build_investigation_chain_figure(v6)

    assert chain is not None
    assert chain["opinion_sha256"] == opinion.content_sha256
    assert chain["rows"][0]["anchor"] == observation.anchor
    assert chain["rows"][0]["relation"] == "qualifies"
    assert chain["rows"][0]["finding_refs"][0]["id"] == str(context.findings[0].id)
    assert chain["rows"][0]["evidence_refs"][0]["id"] == str(context.observations[0].id)


def test_figures_are_omitted_without_canonical_events_or_cited_observations() -> None:
    snapshot, references, extraction, synthesis = _canonical_inputs()
    publication = build_publication_document_v5(
        snapshot=snapshot,
        references=references,
        extraction=extraction,
        synthesis=synthesis,
        editorial_enrichment=_enrichment_with(extraction=extraction, synthesis=synthesis),
    )
    assert build_publication_timeline_figure(publication) is None
    opinion, context = _approved_opinion(publication)
    v6 = augment_publication_with_opinion(publication, opinion, context)
    assert build_investigation_chain_figure(v6) is None


@pytest.mark.asyncio
async def test_real_typst_renders_hash_pinned_timeline_and_anchored_chain(
    tmp_path: Path,
) -> None:
    archived_image = _png(320, 200)
    publication = _publication_with_timeline(archived_image=archived_image)
    source_id = publication.sources[0].source_document_id
    opinion, context = _approved_opinion(publication)
    observation = AnalystObservationV1(
        id=UUID(int=911),
        anchor="lead:0001",
        text="La pièce datée complète le constat éditorial.",
        state=AnalystObservationState.INFERRED,
        relation=AnalystObservationRelation.QUALIFIES,
        finding_refs=(context.findings[0].id,),
        evidence_refs=(context.observations[0].id,),
    )
    opinion = replace(
        opinion,
        blocks=opinion_blocks_with_observations(opinion.blocks, (observation,)),
        content_sha256="",
    )
    infrastructure_graph = InvestigationGraph(
        investigation_id=opinion.investigation_id,
        publication_sha256=publication_document_v5_sha256(publication),
        nodes=(
            InvestigationGraphNode(
                node_id="sample",
                kind=InvestigationGraphNodeKind.FILE_SHA256,
                value="d" * 64,
                label="SHA-256 échantillon",
            ),
            InvestigationGraphNode(
                node_id="pivot",
                kind=InvestigationGraphNodeKind.DOMAIN,
                value="pivot.example",
                label="pivot.example",
            ),
            InvestigationGraphNode(
                node_id="resolved_ip",
                kind=InvestigationGraphNodeKind.IP,
                value="198.51.100.42",
                label="198.51.100.42",
            ),
            InvestigationGraphNode(
                node_id="article",
                kind=InvestigationGraphNodeKind.ARTICLE,
                value=str(source_id),
                label="Article · example.com",
            ),
        ),
        edges=(
            InvestigationGraphEdge(
                source_node_id="sample",
                target_node_id="pivot",
                relation="contacted_domains",
                direction=InvestigationGraphDirection.PIVOT_TO_TARGET,
                source_kind=InvestigationGraphSource.VIRUSTOTAL,
                evidence_basis=InvestigationGraphEvidenceBasis.PROVIDER_ASSERTION,
                confidence=InvestigationGraphConfidence.UNASSESSED,
                observed_at=datetime(2026, 10, 17, 9, 0, tzinfo=UTC),
                evidence_refs=(
                    InvestigationGraphEvidenceRef(
                        observation_id=UUID(int=919),
                        sha256="e" * 64,
                        source_name="VirusTotal",
                        source_version="fixture-v1",
                        observed_at=datetime(2026, 10, 17, 9, 0, tzinfo=UTC),
                    ),
                ),
            ),
            InvestigationGraphEdge(
                source_node_id="pivot",
                target_node_id="resolved_ip",
                relation="dns:A",
                direction=InvestigationGraphDirection.PIVOT_TO_TARGET,
                source_kind=InvestigationGraphSource.SHODAN,
                evidence_basis=InvestigationGraphEvidenceBasis.PROVIDER_ASSERTION,
                confidence=InvestigationGraphConfidence.UNASSESSED,
                observed_at=datetime(2026, 10, 17, 10, 0, tzinfo=UTC),
                evidence_refs=(
                    InvestigationGraphEvidenceRef(
                        observation_id=UUID(int=923),
                        sha256="a" * 64,
                        source_name="Shodan",
                        source_version="fixture-v1",
                        observed_at=datetime(2026, 10, 17, 10, 0, tzinfo=UTC),
                    ),
                ),
            ),
            InvestigationGraphEdge(
                source_node_id="article",
                target_node_id="sample",
                relation="mentions IOC",
                direction=InvestigationGraphDirection.ARTICLE_TO_IOC,
                source_kind=InvestigationGraphSource.ARTICLE,
                evidence_basis=InvestigationGraphEvidenceBasis.ARTICLE_ASSERTION,
                confidence=InvestigationGraphConfidence.UNASSESSED,
                observed_at=None,
                evidence_refs=(
                    InvestigationGraphEvidenceRef(
                        source_document_id=source_id,
                        sha256="e" * 64,
                        source_name="Example publisher",
                        evidence_key="b" * 64,
                    ),
                ),
            ),
        ),
    )
    publication_v6 = augment_publication_with_opinion(
        publication,
        opinion,
        context,
        infrastructure_graph=infrastructure_graph,
    )
    preview_opinion = replace(
        opinion,
        status=AnalystOpinionStatus.DRAFT,
        content_sha256="",
    )
    preview_document = TypstDraftPreviewDocument(
        publication,
        preview_opinion,
        infrastructure_graph=infrastructure_graph,
    )

    graph_figure = build_investigation_graph_figure(publication, infrastructure_graph)
    assert graph_figure is not None
    assert len(infrastructure_graph.nodes) >= 3
    assert {edge.source_kind for edge in infrastructure_graph.edges} >= {
        InvestigationGraphSource.VIRUSTOTAL,
        InvestigationGraphSource.SHODAN,
        InvestigationGraphSource.ARTICLE,
    }
    compiled_graph = await D2DiagramCompiler().compile_source(
        diagram_key=str(graph_figure["figure_id"]),
        source_bytes=str(graph_figure["d2_source"]).encode("utf-8"),
    )
    assert compiled_graph.source_sha256 == graph_figure["d2_source_sha256"]
    graph_svg = compiled_graph.media_bytes
    graph_asset_id = UUID(int=920)
    graph_media_ref = TypstMediaRef(
        asset_id=graph_asset_id,
        expected_kind=MediaAssetKind.DIAGRAM_SVG,
        expected_mime_type="image/svg+xml",
        expected_sha256=compiled_graph.media_sha256,
        expected_byte_size=len(graph_svg),
        media_path=f"media/{graph_asset_id}.svg",
    )

    first_model = project_publication_to_typst_model(publication_v6)
    second_model = project_publication_to_typst_model(publication_v6)
    assert first_model.content_sections == second_model.content_sections
    chronology = next(item for item in first_model.content_sections if item["type"] == "chronology")
    opinion_section = next(
        item for item in first_model.content_sections if item["type"] == "analyst_opinion"
    )
    assert chronology["derived_timeline_figure"]["figure_id"]
    assert opinion_section["investigation_chain_figure"]["figure_id"]

    chp_typst_root, font_bundle_root, font_lock_path = typst_bundle_paths()
    template_bundle = load_template_bundle(chp_typst_root)
    binary = os.environ.get("TYPST_BINARY") or shutil.which("typst")
    assert binary is not None, "Typst 0.15.1 must be installed for this rendering test"
    version = await asyncio.to_thread(
        subprocess.run, (binary, "--version"), capture_output=True, check=True, timeout=10
    )
    assert version.stdout.decode().split()[:2] == ["typst", TYPST_COMPILER_VERSION]
    font_snapshot = load_font_bundle_snapshot(font_bundle_root, font_lock_path)
    pdftotext = shutil.which("pdftotext")
    pdftoppm = shutil.which("pdftoppm")
    assert pdftotext is not None and pdftoppm is not None

    async def render_to_text(label: str, render_document) -> tuple[Path, str, str]:
        render_source = TypstRenderer().render(
            render_document,
            template_bundle,
            document_label="Publication de test — figures éditoriales",
            infrastructure_graph_media_ref=graph_media_ref,
        )
        workspace_root = tmp_path / f"{label}-workspace"
        shutil.copytree(chp_typst_root / "RENDERER", workspace_root / "RENDERER")
        shutil.copytree(chp_typst_root / "UTILS", workspace_root / "UTILS")
        media_root = workspace_root / "RENDERER" / "media"
        media_root.mkdir()
        shutil.copy2(chp_typst_root / "UTILS" / "chap.png", media_root / "figure.png")
        for media_ref in render_source.media_refs:
            if media_ref.asset_id == graph_asset_id:
                media_content = graph_svg
            else:
                media_content = archived_image
            assert media_ref.expected_sha256 == hashlib.sha256(media_content).hexdigest()
            assert media_ref.expected_byte_size == len(media_content)
            media_path = media_root / Path(media_ref.media_path).name
            media_path.write_bytes(media_content)
        (workspace_root / "RENDERER" / "render-data.json").write_bytes(
            render_source.render_data_bytes
        )
        font_root = tmp_path / f"fonts-{label}"
        font_root.mkdir()
        compiled = await TypstSubprocessCompiler(binary=binary).compile(
            TypstCompileRequest(
                workspace_root=workspace_root,
                entrypoint_relative_path=render_source.entrypoint_relative_path,
                font_paths=materialize_font_bundle(font_snapshot, font_root),
            )
        )
        pdf_path = tmp_path / f"{label}.pdf"
        pdf_path.write_bytes(compiled.content)
        extracted_result = await asyncio.to_thread(
            subprocess.run,
            (pdftotext, "-raw", str(pdf_path), "-"),
            capture_output=True,
            check=True,
            timeout=10,
        )
        extracted = extracted_result.stdout.decode("utf-8", errors="replace")
        return pdf_path, extracted, render_source.render_data_sha256

    preview_pdf, preview_text, preview_render_hash = await render_to_text(
        "v5-preview", preview_document
    )
    assert "BROUILLON NON APPROUVÉ" in preview_text
    v6_pdf, extracted, render_hash = await render_to_text("v6-final", publication_v6)
    repeated_source = TypstRenderer().render(
        publication_v6,
        template_bundle,
        document_label="Publication de test — figures éditoriales",
        infrastructure_graph_media_ref=graph_media_ref,
    )
    repeated_render = TypstRenderer().render(
        publication_v6,
        template_bundle,
        document_label="Publication de test — figures éditoriales",
        infrastructure_graph_media_ref=graph_media_ref,
    )
    assert repeated_source.render_data_bytes == repeated_render.render_data_bytes
    assert render_hash == repeated_source.render_data_sha256
    assert preview_render_hash != render_hash
    normalized = "".join(
        character for character in extracted.replace("\u00ad", "") if not character.isspace()
    )
    assert "Événementsdatés" in normalized
    timeline_start = extracted.index("Événements datés")
    timeline_end = extracted.index("SYNTHÈSE", timeline_start)
    timeline_text = extracted[timeline_start:timeline_end]
    assert "Jours écoulés" not in timeline_text
    assert timeline_text.count("2025-01-12") == 1
    assert timeline_text.count("2025-01-16") == 1
    assert "Sources" in timeline_text and "1" in timeline_text
    assert "Référencesdel'avis" in normalized
    assert "jourcalendaire" not in normalized
    assert "2025-01-12" in normalized and "2025-01-16" in normalized
    assert observation.text in extracted
    assert str(observation.id) not in extracted
    assert "lead:0001" not in extracted
    assert "SourceSHA-256" not in normalized
    assert "Graphed'infrastructureetdepivots" in normalized
    assert "domainescontactés" in normalized
    assert "VirusTotal" in normalized
    assert "Shodan" in normalized
    assert "Article" in normalized
    assert "affirmationfournisseur" in normalized
    assert "Confiance" in normalized
    assert "nonévaluée" in normalized
    assert "pivotverscible" in normalized
    assert "dns:A" in extracted
    assert "mentions IOC" in extracted
    assert "pivot.example" in normalized
    assert infrastructure_graph.sha256 not in normalized
    assert "PreuveSHA-256" not in normalized
    assert "c" * 64 not in normalized
    assert "d" * 64 not in normalized
    assert "a" * 64 not in normalized
    source_figure = publication.document.figures[0]
    for text in (preview_text, extracted):
        normalized_figure_text = "".join(character for character in text if not character.isspace())
        assert "".join(source_figure.caption.split()) in normalized_figure_text
        assert "".join(source_figure.provenance.split()) in normalized_figure_text
        assert source_figure.sha256 not in normalized_figure_text
        assert "Sourcearchivée" not in normalized_figure_text
        assert "Attribution:" in normalized_figure_text
        assert "SHA-256archivé:" not in normalized_figure_text
        assert "Taille:" not in normalized_figure_text
    assert publication_v6.analyst_opinion.content_sha256 not in normalized
    assert publication_document_v5_sha256(publication) not in normalized

    async def read_bbox_pages(pdf_path: Path) -> list[list[tuple[float, float, str]]]:
        result = await asyncio.to_thread(
            subprocess.run,
            (pdftotext, "-bbox-layout", str(pdf_path), "-"),
            capture_output=True,
            check=True,
            timeout=10,
        )
        root = ET.fromstring(result.stdout)
        pages = [node for node in root.iter() if node.tag.endswith("page")]
        return [
            [
                (
                    float(line.attrib["yMin"]),
                    float(line.attrib["yMax"]),
                    " ".join(word.text or "" for word in line if word.tag.endswith("word")).strip(),
                )
                for line in page.iter()
                if line.tag.endswith("line")
            ]
            for page in pages
        ]

    def assert_figure_caption_spacing(pages: list[list[tuple[float, float, str]]]) -> int:
        page_index = next(
            index
            for index, lines in enumerate(pages)
            if any("Graphe d'infrastructure et de pivots" in text for _, _, text in lines)
        )
        all_lines = [line for page in pages for line in page]
        figure_page = next(
            lines for lines in pages if any(text.startswith("Source :") for _, _, text in lines)
        )
        image_page = pages[page_index]

        source_index = next(
            i for i, (_, _, text) in enumerate(figure_page) if text.startswith("Source :")
        )
        attribution_index = next(
            i for i, (_, _, text) in enumerate(figure_page) if text.startswith("Attribution :")
        )
        metadata_lines = figure_page[source_index : attribution_index + 1]
        assert "Attribution :" in figure_page[attribution_index][2]
        for previous, current in pairwise(metadata_lines):
            assert previous[1] <= current[0], (
                f"figure metadata lines overlap: {previous} / {current}"
            )

        chronology_heading = next(line for line in all_lines if line[2].strip() == "CHRONOLOGIE")
        chronology_caption = next(
            line for line in all_lines if line[2].startswith("Figure 2 : Événements datés")
        )
        assert chronology_heading[1] <= chronology_caption[0], (
            "chronology title overlaps its figure caption: "
            f"{chronology_heading} / {chronology_caption}"
        )

        graph_caption = next(
            line for line in image_page if "Graphe d'infrastructure et de pivots" in line[2]
        )
        graph_description = next(
            line for line in image_page if line[2].startswith("Les flèches représentent")
        )
        assert graph_caption[1] <= graph_description[0], (
            "infrastructure graph caption overlaps its description: "
            f"{graph_caption} / {graph_description}"
        )

        legend_page = next(
            lines for lines in pages if any(text.startswith("Légende :") for _, _, text in lines)
        )
        legend_start = next(
            i for i, (_, _, text) in enumerate(legend_page) if text.startswith("Légende :")
        )
        # The technical annex now starts on a new landscape page, so there is
        # no next-section heading on the legend page to use as a boundary.
        # Keep the footer out of the body spacing check.
        legend_lines = [
            line
            for line in legend_page[legend_start:]
            if line[2] != "BROUILLON NON APPROUVÉ"
            and re.fullmatch(r"\d+\s*/\s*\d+", line[2]) is None
        ]
        for previous, current in pairwise(legend_lines):
            assert previous[1] <= current[0], f"graph legend lines overlap: {previous} / {current}"
        return page_index

    for label, pdf_path in (("v5-preview", preview_pdf), ("v6-final", v6_pdf)):
        bbox_pages = await read_bbox_pages(pdf_path)
        graph_page_index = assert_figure_caption_spacing(bbox_pages)
        page_count = len(PdfReader(pdf_path, strict=True).pages)
        assert page_count == 3
        raster_prefix = tmp_path / f"{label}-page"
        await asyncio.to_thread(
            subprocess.run,
            (pdftoppm, "-png", "-r", "100", str(pdf_path), str(raster_prefix)),
            capture_output=True,
            check=True,
            timeout=20,
        )
        raster_pattern = f"{label}-page-*.png"

        def list_raster_paths(pattern: str) -> tuple[Path, ...]:
            return tuple(tmp_path.glob(pattern))

        rasterized = await asyncio.to_thread(list_raster_paths, raster_pattern)
        assert len(rasterized) == page_count
        if label == "v6-final":
            graph_page = tmp_path / f"v6-final-page-{graph_page_index + 1}.png"
            assert graph_page in rasterized
            shutil.copy2(graph_page, tmp_path / "v6-infrastructure-graph-page.png")
