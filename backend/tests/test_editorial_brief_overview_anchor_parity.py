"""BRIEF overview anchors must stay on their paragraph in the rendered PDF.

The brief publication builder merges the OVERVIEW section into ``document.lead``
and re-indexes the remaining sections. Enrichment anchors resolved against the
synthesis document therefore need one deterministic mapping into the merged
publication body: an element anchored after an overview paragraph must render
right after the paragraph it cites, never in another section and never dropped.
``after_lead`` elements render after the whole merged overview block.

These are real-Typst tests: the PDF text order is the oracle, not only the
render data.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from pypdf import PdfReader

from cti_app.application.editorial_workspace.registry import project_blocks
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.application.production_editorial_enrichment import canonical_synthesis_hash
from cti_app.application.production_relevance import build_relevance_projection
from cti_app.application.publication_assembly import PublicationAssemblyValidationError
from cti_app.application.typst_compilation import (
    TypstCompileRequest,
    load_font_bundle_snapshot,
    materialize_font_bundle,
)
from cti_app.application.typst_rendering import load_template_bundle
from cti_app.domain.editorial_workspace import bootstrap_block_registry
from cti_app.domain.production_editorial_enrichment import (
    EnrichmentPlacementKind,
    EnrichmentPlacementV1,
    editorial_enrichment_to_json,
)
from cti_app.domain.production_synthesis import (
    SynthesisParagraphV1,
    SynthesisSectionKind,
    SynthesisSectionV1,
    extraction_evidence_refs_v1,
    production_synthesis_to_json,
)
from cti_app.domain.publication import PublicationAssemblyErrorCode
from cti_app.domain.publication_document import (
    PublicationDocumentV9,
    PublicationDocumentV10,
    parse_publication_document,
)
from cti_app.domain.publication_kind import PublicationKind
from cti_app.infrastructure.typst_compiler import TypstSubprocessCompiler
from tests.editorial_enrichment_support import build_empty_editorial_enrichment
from tests.test_editorial_enrichment_order_parity import _marker_figure, _marker_table
from tests.test_publication_assembly import (
    _assemble,
    _canonical_inputs,
    _run,
    _service,
    _workspace_record_for,
)
from tests.test_typst_chp_parity import (
    _CHP_TYPST_ROOT,
    _FONT_BUNDLE_LOCK,
    _build_workspace,
    _render,
    _run_blocking,
)

pytest_plugins = ("tests.test_typst_compiler_runtime",)

_ELEMENT_KINDS = {"table", "figure", "diagram", "chart"}

LEAD_MARKER = "Brief lead marker paragraph."
OVERVIEW_ONE_MARKER = "Brief overview paragraph one marker."
OVERVIEW_TWO_MARKER = "Brief overview paragraph two marker."
TECHNICAL_MARKER = "Brief technical paragraph marker."
OVERVIEW_ANCHOR_TITLE = "Overview anchor table marker"
OVERVIEW_ANCHORED_FIGURE_CAPTION = "Overview anchored figure marker"
AFTER_LEAD_TITLE = "After lead table marker"
TECHNICAL_ANCHOR_TITLE = "Technical anchor table marker"
END_TITLE = "End table marker"


def _brief_overview_inputs() -> tuple[Any, ...]:
    snapshot, references, extraction, synthesis = _canonical_inputs()
    evidence_ref = extraction_evidence_refs_v1(extraction)[0]
    synthesis = replace(
        synthesis,
        lead=(SynthesisParagraphV1(LEAD_MARKER, (evidence_ref,)),),
        sections=(
            SynthesisSectionV1(
                SynthesisSectionKind.OVERVIEW,
                "Vue d'ensemble",
                (
                    SynthesisParagraphV1(OVERVIEW_ONE_MARKER, (evidence_ref,)),
                    SynthesisParagraphV1(OVERVIEW_TWO_MARKER, (evidence_ref,)),
                ),
            ),
            SynthesisSectionV1(
                SynthesisSectionKind.TECHNICAL,
                "Brief technical section",
                (SynthesisParagraphV1(TECHNICAL_MARKER, (evidence_ref,)),),
            ),
        ),
    )
    return snapshot, references, extraction, synthesis, evidence_ref


def _brief_overview_fixture() -> dict[str, Any]:
    snapshot, references, extraction, synthesis, evidence_ref = _brief_overview_inputs()
    run = _run(snapshot)
    synthesis_artifact_id = uuid4()
    entries = bootstrap_block_registry(
        artifact_id=synthesis_artifact_id,
        canonical_sha256=canonical_synthesis_hash(synthesis),
        source_stage="synthesis",
        canonical_content=production_synthesis_to_json(synthesis),
    )
    overview_anchor = next(
        entry for entry in entries if entry.structural_pointer == "/sections/0/paragraphs/0"
    )
    technical_anchor = next(
        entry for entry in entries if entry.structural_pointer == "/sections/1/paragraphs/0"
    )
    after_paragraph = EnrichmentPlacementV1(
        EnrichmentPlacementKind.AFTER_PARAGRAPH,
        anchor_block_id=overview_anchor.block_id,
    )
    tables = (
        _marker_table(
            evidence_ref,
            key="brief_overview_anchor",
            marker=OVERVIEW_ANCHOR_TITLE,
            placement=after_paragraph,
        ),
        _marker_table(
            evidence_ref,
            key="brief_after_lead",
            marker=AFTER_LEAD_TITLE,
            placement=EnrichmentPlacementV1(EnrichmentPlacementKind.AFTER_LEAD),
        ),
        _marker_table(
            evidence_ref,
            key="brief_technical_anchor",
            marker=TECHNICAL_ANCHOR_TITLE,
            placement=EnrichmentPlacementV1(
                EnrichmentPlacementKind.AFTER_PARAGRAPH,
                anchor_block_id=technical_anchor.block_id,
            ),
        ),
        _marker_table(
            evidence_ref,
            key="brief_end",
            marker=END_TITLE,
            placement=EnrichmentPlacementV1(EnrichmentPlacementKind.END),
        ),
    )
    figures = (
        _marker_figure(
            extraction,
            key="brief_overview_figure",
            marker=OVERVIEW_ANCHORED_FIGURE_CAPTION,
            placement=EnrichmentPlacementV1(
                EnrichmentPlacementKind.AFTER_PARAGRAPH,
                anchor_block_id=overview_anchor.block_id,
            ),
            order=0,
            ordinal=0,
        ),
    )
    enrichment = replace(
        build_empty_editorial_enrichment(extraction=extraction, synthesis=synthesis),
        tables=tables,
        source_figures=figures,
    )
    workspace_record = replace(
        _workspace_record_for(
            run=run,
            synthesis=synthesis,
            enrichment=enrichment,
            synthesis_artifact_id=synthesis_artifact_id,
        ),
        publication_kind=PublicationKind.BRIEF,
    )
    # Canonical element order in both the merged publication and the PDF:
    # the two overview-anchored elements follow their paragraph, after_lead
    # follows the whole merged overview block, then the technical section.
    expected_markers = [
        OVERVIEW_ONE_MARKER,
        OVERVIEW_ANCHOR_TITLE,
        OVERVIEW_ANCHORED_FIGURE_CAPTION,
        OVERVIEW_TWO_MARKER,
        AFTER_LEAD_TITLE,
        TECHNICAL_ANCHOR_TITLE,
        END_TITLE,
    ]
    return {
        "snapshot": snapshot,
        "references": references,
        "extraction": extraction,
        "synthesis": synthesis,
        "enrichment": enrichment,
        "workspace_record": workspace_record,
        "expected_markers": expected_markers,
    }


def _snapshot_markers(workspace_record: Any, synthesis: Any, enrichment: Any) -> list[str]:
    blocks, _enrichments, _outline = project_blocks(
        entries=workspace_record.block_registry,
        synthesis=production_synthesis_to_json(synthesis),
        enrichment=editorial_enrichment_to_json(enrichment),
        record=workspace_record,
        allow_operations=False,
    )
    markers: list[str] = []
    for block in blocks:
        kind = str(block["kind"])
        if kind not in _ELEMENT_KINDS:
            continue
        if kind in {"table", "figure"}:
            payload = block[kind]
            assert isinstance(payload, dict)
            markers.append(str(payload["title" if kind == "table" else "caption"]))
        else:
            markers.append(str(block["text"]))
    return markers


def _synthesis_blocks(render_data_bytes: bytes) -> list[dict[str, Any]]:
    payload = json.loads(render_data_bytes)
    section = next(item for item in payload["content_sections"] if item.get("type") == "synthesis")
    return [block for block in section["blocks"] if isinstance(block, dict)]


def _element_markers(blocks: list[dict[str, Any]]) -> list[str]:
    markers: list[str] = []
    for block in blocks:
        kind = str(block.get("type"))
        if kind not in _ELEMENT_KINDS:
            continue
        markers.append(str(block["title"] if kind in {"table", "diagram"} else block["caption"]))
    return markers


def _pdf_text(pdf_path: Path) -> str:
    pdftotext = shutil.which("pdftotext")
    if pdftotext is not None:
        result = _run_blocking(
            (pdftotext, "-layout", str(pdf_path), "-"),
            capture_output=True,
            check=True,
            timeout=20,
        )
        return " ".join(result.stdout.decode("utf-8", errors="replace").split())
    reader = PdfReader(str(pdf_path), strict=True)
    return " ".join("\n".join(page.extract_text() or "" for page in reader.pages).split())


@pytest.mark.asyncio
async def test_brief_overview_anchors_render_after_their_paragraphs(
    tmp_path: Path,
    typst_binary: str,
    font_bundle_root: Path,
) -> None:
    fixture = _brief_overview_fixture()
    expected_markers = fixture["expected_markers"]

    snapshot_markers = _snapshot_markers(
        fixture["workspace_record"], fixture["synthesis"], fixture["enrichment"]
    )

    publication = await _assemble_brief(fixture)
    assert publication.base_publication.publication_kind is PublicationKind.BRIEF

    # The merged brief lead holds the synthesis lead then the overview paragraphs.
    base_document = publication.base_publication.base_publication.base_publication.document
    assert [paragraph.text for paragraph in base_document.lead] == [
        LEAD_MARKER,
        OVERVIEW_ONE_MARKER,
        OVERVIEW_TWO_MARKER,
    ]
    assert [section.heading for section in base_document.sections] == ["Brief technical section"]

    # Resolved placements carry the publication body identity, not the synthesis
    # coordinates: both overview-anchored elements point at the merged lead
    # paragraph that contains the anchored overview paragraph.
    assert [
        (
            item.element_kind,
            item.element_key,
            item.paragraph_kind,
            item.section_index,
            item.paragraph_index,
        )
        for item in publication.resolved_placements
    ] == [
        ("table", "brief_overview_anchor", "lead", None, 1),
        ("figure", "brief_overview_figure", "lead", None, 1),
        ("table", "brief_technical_anchor", "section", 0, 0),
    ]
    assert [item.anchor_pointer for item in publication.resolved_placements] == [
        "/lead/1",
        "/lead/1",
        "/sections/0/paragraphs/0",
    ]

    template_bundle = load_template_bundle(_CHP_TYPST_ROOT)
    render_source = _render(publication, template_bundle)
    blocks = _synthesis_blocks(render_source.render_data_bytes)
    paragraph_markers = [
        block["text"]
        for block in blocks
        if block.get("type") == "paragraph"
        and block.get("text")
        in {
            LEAD_MARKER,
            OVERVIEW_ONE_MARKER,
            OVERVIEW_TWO_MARKER,
            TECHNICAL_MARKER,
        }
    ]
    assert paragraph_markers == [
        LEAD_MARKER,
        OVERVIEW_ONE_MARKER,
        OVERVIEW_TWO_MARKER,
        TECHNICAL_MARKER,
    ]
    assert _element_markers(blocks) == [
        OVERVIEW_ANCHOR_TITLE,
        OVERVIEW_ANCHORED_FIGURE_CAPTION,
        AFTER_LEAD_TITLE,
        TECHNICAL_ANCHOR_TITLE,
        END_TITLE,
    ]
    marker_by_key = {
        "brief_overview_anchor": OVERVIEW_ANCHOR_TITLE,
        "brief_overview_figure": OVERVIEW_ANCHORED_FIGURE_CAPTION,
        "brief_technical_anchor": TECHNICAL_ANCHOR_TITLE,
    }
    assert [marker_by_key[item.element_key] for item in publication.resolved_placements] == [
        OVERVIEW_ANCHOR_TITLE,
        OVERVIEW_ANCHORED_FIGURE_CAPTION,
        TECHNICAL_ANCHOR_TITLE,
    ]

    workspace_root = tmp_path / "brief-overview"
    _build_workspace(
        workspace_root,
        bundle_files=template_bundle.files,
        render_data_bytes=render_source.render_data_bytes,
        media_refs=render_source.media_refs,
    )
    font_snapshot = load_font_bundle_snapshot(font_bundle_root, _FONT_BUNDLE_LOCK)
    font_root = tmp_path / "brief-overview-fonts"
    font_root.mkdir()
    compiled = await TypstSubprocessCompiler(binary=typst_binary).compile(
        TypstCompileRequest(
            workspace_root=workspace_root,
            entrypoint_relative_path="RENDERER/publication.typ",
            font_paths=materialize_font_bundle(font_snapshot, font_root),
        )
    )
    pdf_path = tmp_path / "brief-overview.pdf"
    pdf_path.write_bytes(compiled.content)
    pdf_text = _pdf_text(pdf_path)
    positions = [pdf_text.index(marker) for marker in expected_markers]
    assert positions == sorted(positions), (expected_markers, positions)
    assert pdf_text.index(OVERVIEW_ONE_MARKER) < pdf_text.index(OVERVIEW_ANCHOR_TITLE)
    assert pdf_text.index(OVERVIEW_ANCHOR_TITLE) < pdf_text.index(OVERVIEW_TWO_MARKER)
    assert pdf_text.index(OVERVIEW_TWO_MARKER) < pdf_text.index(AFTER_LEAD_TITLE)
    assert pdf_text.index(AFTER_LEAD_TITLE) < pdf_text.index(TECHNICAL_MARKER)
    assert pdf_text.index(TECHNICAL_MARKER) < pdf_text.index(TECHNICAL_ANCHOR_TITLE)

    # The workshop snapshot exposes the same element order as the merged PDF.
    assert snapshot_markers == _element_markers(blocks)


@pytest.mark.asyncio
async def test_brief_ambiguous_merged_anchor_is_refused_not_dropped() -> None:
    """A paragraph that the merge collapses into an ambiguous duplicate refuses."""
    snapshot, references, extraction, synthesis, evidence_ref = _brief_overview_inputs()
    duplicated = "Brief duplicated overview paragraph marker."
    synthesis = replace(
        synthesis,
        lead=(
            SynthesisParagraphV1(duplicated, (evidence_ref,)),
            SynthesisParagraphV1(duplicated, (evidence_ref,)),
        ),
        sections=(
            SynthesisSectionV1(
                SynthesisSectionKind.OVERVIEW,
                "Vue d'ensemble",
                (SynthesisParagraphV1(duplicated, (evidence_ref,)),),
            ),
        ),
    )
    run = _run(snapshot)
    synthesis_artifact_id = uuid4()
    entries = bootstrap_block_registry(
        artifact_id=synthesis_artifact_id,
        canonical_sha256=canonical_synthesis_hash(synthesis),
        source_stage="synthesis",
        canonical_content=production_synthesis_to_json(synthesis),
    )
    anchor = next(
        entry for entry in entries if entry.structural_pointer == "/sections/0/paragraphs/0"
    )
    enrichment = replace(
        build_empty_editorial_enrichment(extraction=extraction, synthesis=synthesis),
        tables=(
            _marker_table(
                evidence_ref,
                key="brief_ambiguous_anchor",
                marker="Ambiguous anchor table marker",
                placement=EnrichmentPlacementV1(
                    EnrichmentPlacementKind.AFTER_PARAGRAPH,
                    anchor_block_id=anchor.block_id,
                ),
            ),
        ),
    )
    workspace_record = replace(
        _workspace_record_for(
            run=run,
            synthesis=synthesis,
            enrichment=enrichment,
            synthesis_artifact_id=synthesis_artifact_id,
        ),
        publication_kind=PublicationKind.BRIEF,
    )
    service, _catalog, _artifacts = _service()
    with pytest.raises(PublicationAssemblyValidationError) as raised:
        await _assemble(
            service,
            run=run,
            snapshot=snapshot,
            references=references,
            extraction=extraction,
            synthesis=synthesis,
            editorial_enrichment=enrichment,
            relevance_projection=build_relevance_projection(snapshot, extraction),
            publication_kind=PublicationKind.BRIEF,
            editorial_enrichment_artifact_id=workspace_record.enrichment_artifact_id,
            editorial_enrichment_artifact_input_hash="e" * 64,
            workspace_snapshot_record=workspace_record,
            routing_decision_hash="b" * 64,
            routing_policy_version="routing-policy-v1",
        )
    assert raised.value.code is PublicationAssemblyErrorCode.EDITORIAL_ANCHOR_STALE


@pytest.mark.asyncio
async def test_brief_unemitted_resolved_placement_raises() -> None:
    """The renderer refuses a BRIEF placement whose anchor is never emitted."""
    fixture = _brief_overview_fixture()
    publication = await _assemble_brief(fixture)
    placed = publication.resolved_placements[0]
    broken = (
        replace(placed, paragraph_index=4, anchor_pointer="/lead/4")
        if placed.paragraph_kind == "lead"
        else replace(
            placed,
            paragraph_index=4,
            anchor_pointer=f"/sections/{placed.section_index}/paragraphs/4",
        )
    )
    document = replace(
        publication,
        resolved_placements=(broken, *publication.resolved_placements[1:]),
    )
    template_bundle = load_template_bundle(_CHP_TYPST_ROOT)
    with pytest.raises(ValueError, match="editorial_anchor_stale"):
        _render(document, template_bundle)


async def _assemble_brief(
    fixture: dict[str, Any],
) -> PublicationDocumentV9 | PublicationDocumentV10:
    service, catalog, _artifacts = _service()
    assembly = await _assemble(
        service,
        run=_run(fixture["snapshot"]),
        snapshot=fixture["snapshot"],
        references=fixture["references"],
        extraction=fixture["extraction"],
        synthesis=fixture["synthesis"],
        editorial_enrichment=fixture["enrichment"],
        relevance_projection=build_relevance_projection(fixture["snapshot"], fixture["extraction"]),
        publication_kind=PublicationKind.BRIEF,
        editorial_enrichment_artifact_id=fixture["workspace_record"].enrichment_artifact_id,
        editorial_enrichment_artifact_input_hash="e" * 64,
        workspace_snapshot_record=fixture["workspace_record"],
        routing_decision_hash="b" * 64,
        routing_policy_version="routing-policy-v1",
    )
    publication = parse_publication_document(
        await ProductionArtifactStore(catalog).read_json(assembly.artifact.canonical_blob_id)
    )
    assert isinstance(publication, (PublicationDocumentV9, PublicationDocumentV10))
    return publication
