"""One canonical enrichment order must drive the workshop, publication and PDF.

The fixture exercises both legacy V10 artifacts (where the workshop used to
honor ``figure.order`` while the PDF ignored it) and a V11 artifact with an
explicit ``group_order``. Snapshot order, ``resolved_placements`` order and the
Typst/PDF body order must all agree.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from pypdf import PdfReader

from cti_app.application.editorial_workspace.registry import project_blocks
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.application.production_editorial_enrichment import canonical_synthesis_hash
from cti_app.application.typst_compilation import (
    TypstCompileRequest,
    load_font_bundle_snapshot,
    materialize_font_bundle,
)
from cti_app.application.typst_rendering import load_template_bundle
from cti_app.domain.editorial_workspace import bootstrap_block_registry
from cti_app.domain.production_editorial_enrichment import (
    EDITORIAL_ENRICHMENT_V11_POLICY_VERSION,
    EditorialAnalyticPurposeV1,
    EditorialEnrichmentV1,
    EnrichmentPlacementKind,
    EnrichmentPlacementV1,
    EnrichmentTableKind,
    ResolvedSourceFigureV1,
    SourceFigureCandidateV1,
    SourceFigureDecision,
    SourceFigureInclusionStatus,
    SourceFigureLocatorV1,
    TableColumnV1,
    TableRowV1,
    TableSpecV1,
    editorial_enrichment_to_json,
    source_figure_id,
)
from cti_app.domain.production_synthesis import (
    SynthesisParagraphV1,
    SynthesisSectionKind,
    SynthesisSectionV1,
    extraction_evidence_refs_v1,
    production_synthesis_to_json,
)
from cti_app.domain.publication_document import (
    PublicationDocumentV9,
    PublicationDocumentV10,
    parse_publication_document,
)
from cti_app.infrastructure.typst_compiler import TypstSubprocessCompiler
from tests.editorial_enrichment_support import build_empty_editorial_enrichment
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


def _fixture_inputs():
    snapshot, references, extraction, synthesis = _canonical_inputs()
    evidence_ref = extraction_evidence_refs_v1(extraction)[0]
    paragraphs = tuple(
        SynthesisParagraphV1(f"Synthetic parity paragraph {index}.", (evidence_ref,))
        for index in range(3)
    )
    synthesis = replace(
        synthesis,
        sections=(
            SynthesisSectionV1(
                SynthesisSectionKind.TECHNICAL,
                "Synthetic parity section",
                paragraphs,
            ),
        ),
    )
    return snapshot, references, extraction, synthesis, evidence_ref


def _marker_table(
    evidence_ref: Any,
    *,
    key: str,
    marker: str,
    placement: EnrichmentPlacementV1,
    group_order: int | None = None,
) -> TableSpecV1:
    return TableSpecV1(
        key=key,
        kind=EnrichmentTableKind.COMMANDS,
        title=marker,
        caption=None,
        columns=(TableColumnV1("command", "Command"), TableColumnV1("purpose", "Purpose")),
        rows=(TableRowV1(("-enc", "Execution"), (evidence_ref,)),),
        placement=placement,
        purpose=EditorialAnalyticPurposeV1(
            question=f"Which parity value does {key} expose?",
            available_data="The extraction records one synthetic command.",
            comprehension_gain="The adjacent cells pair the literal and role.",
            scope="The single synthetic row.",
            evidence_refs=(evidence_ref,),
            knowledge_limits="No other command effects are stated.",
            placement_reason="Placed after the anchor paragraph.",
        ),
        group_order=group_order,
    )


def _marker_figure(
    extraction: Any,
    *,
    key: str,
    marker: str,
    placement: EnrichmentPlacementV1,
    order: int,
    ordinal: int,
    group_order: int | None = None,
) -> SourceFigureCandidateV1:
    locator = SourceFigureLocatorV1(
        page=ordinal + 1,
        section="Synthetic parity section",
        figure_label=f"Synthetic parity figure {ordinal + 1}",
    )
    sha256 = hashlib.sha256(f"synthetic parity figure {ordinal}".encode()).hexdigest()
    source = extraction.sources[0]
    figure_id = source_figure_id(
        source_document_id=source.source_document_id,
        sha256=sha256,
        source=source.canonical_url,
        locator=locator,
    )
    return SourceFigureCandidateV1(
        key=key,
        source_document_id=source.source_document_id,
        source_url=source.canonical_url,
        caption=marker,
        provenance="Synthetic archived figure fixture.",
        locator=locator,
        inclusion_status=SourceFigureInclusionStatus.INCLUDED,
        placement=placement,
        resolved_figure=ResolvedSourceFigureV1(
            figure_id=figure_id,
            blob_id=UUID(int=9100 + ordinal),
            sha256=sha256,
            mime_type="image/png",
            byte_size=128,
            source_document_id=source.source_document_id,
            source=source.canonical_url,
            provenance="Synthetic archived figure fixture.",
            locator=locator,
            decision=SourceFigureDecision.ACCEPTED,
            decision_reason="archived_blob_verified",
        ),
        purpose=None,
        width=8,
        height=6,
        order=order,
        group_order=group_order,
    )


def _parity_fixture(*, explicit: bool) -> dict[str, Any]:
    snapshot, references, extraction, synthesis, evidence_ref = _fixture_inputs()
    run = _run(snapshot)
    synthesis_artifact_id = uuid4()
    entries = bootstrap_block_registry(
        artifact_id=synthesis_artifact_id,
        canonical_sha256=canonical_synthesis_hash(synthesis),
        source_stage="synthesis",
        canonical_content=production_synthesis_to_json(synthesis),
    )
    anchor = next(
        entry for entry in entries if entry.structural_pointer == "/sections/0/paragraphs/1"
    )
    after_paragraph = EnrichmentPlacementV1(
        EnrichmentPlacementKind.AFTER_PARAGRAPH,
        anchor_block_id=anchor.block_id,
    )
    after_lead = EnrichmentPlacementV1(EnrichmentPlacementKind.AFTER_LEAD)
    end = EnrichmentPlacementV1(EnrichmentPlacementKind.END)

    markers = {
        "parity_table_lead": "Parity lead table marker",
        "parity_figure_lead": "Parity lead figure marker",
        "parity_figure_z": "Parity z figure marker",
        "parity_table_anchor": "Parity anchor table marker",
        "parity_figure_a": "Parity a figure marker",
        "parity_table_end": "Parity end table marker",
        "parity_figure_end": "Parity end figure marker",
    }
    after_paragraph_orders = {
        "parity_figure_a": 0 if explicit else None,
        "parity_table_anchor": 1 if explicit else None,
        "parity_figure_z": 2 if explicit else None,
    }
    tables = (
        _marker_table(
            evidence_ref,
            key="parity_table_lead",
            marker=markers["parity_table_lead"],
            placement=after_lead,
        ),
        _marker_table(
            evidence_ref,
            key="parity_table_anchor",
            marker=markers["parity_table_anchor"],
            placement=after_paragraph,
            group_order=after_paragraph_orders["parity_table_anchor"],
        ),
        _marker_table(
            evidence_ref,
            key="parity_table_end",
            marker=markers["parity_table_end"],
            placement=end,
        ),
    )
    figures = (
        _marker_figure(
            extraction,
            key="parity_figure_z",
            marker=markers["parity_figure_z"],
            placement=after_paragraph,
            order=1,
            ordinal=0,
            group_order=after_paragraph_orders["parity_figure_z"],
        ),
        _marker_figure(
            extraction,
            key="parity_figure_a",
            marker=markers["parity_figure_a"],
            placement=after_paragraph,
            order=0,
            ordinal=1,
            group_order=after_paragraph_orders["parity_figure_a"],
        ),
        _marker_figure(
            extraction,
            key="parity_figure_lead",
            marker=markers["parity_figure_lead"],
            placement=after_lead,
            order=0,
            ordinal=2,
        ),
        _marker_figure(
            extraction,
            key="parity_figure_end",
            marker=markers["parity_figure_end"],
            placement=end,
            order=1,
            ordinal=3,
        ),
    )
    base = build_empty_editorial_enrichment(extraction=extraction, synthesis=synthesis)
    enrichment = replace(
        base,
        schema_version=11 if explicit else 10,
        enrichment_policy_version=(
            EDITORIAL_ENRICHMENT_V11_POLICY_VERSION if explicit else base.enrichment_policy_version
        ),
        tables=tables,
        source_figures=figures,
    )
    workspace_record = _workspace_record_for(
        run=run,
        synthesis=synthesis,
        enrichment=enrichment,
        synthesis_artifact_id=synthesis_artifact_id,
    )
    # Legacy rank is (family-local index, key): figures are family indexes 0
    # and 1, the anchored table is table family index 1, so the historical PDF
    # order is figure_z, figure_a, table_anchor. The declared figure.order
    # values (1 then 0) are ignored.
    after_paragraph_order = (
        ["parity_figure_a", "parity_table_anchor", "parity_figure_z"]
        if explicit
        else ["parity_figure_z", "parity_figure_a", "parity_table_anchor"]
    )
    # Non-paragraph placements keep the legacy family order (table then
    # figure); the explicit fixture only reorders the after_paragraph group.
    expected_order = [
        "parity_table_lead",
        "parity_figure_lead",
        *after_paragraph_order,
        "parity_table_end",
        "parity_figure_end",
    ]
    return {
        "snapshot": snapshot,
        "references": references,
        "extraction": extraction,
        "synthesis": synthesis,
        "enrichment": enrichment,
        "workspace_record": workspace_record,
        "markers": markers,
        "after_paragraph_order": after_paragraph_order,
        "expected_order": expected_order,
        "expected_markers": [markers[key] for key in expected_order],
    }


def _snapshot_markers(
    workspace_record: Any, synthesis: Any, enrichment: EditorialEnrichmentV1
) -> list[str]:
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


def _typst_markers(render_data_bytes: bytes) -> list[str]:
    payload = json.loads(render_data_bytes)
    markers: list[str] = []
    for section in payload["content_sections"]:
        for block in section.get("blocks", []):
            kind = str(block.get("type"))
            if kind not in _ELEMENT_KINDS:
                continue
            markers.append(
                str(block["title"] if kind in {"table", "diagram"} else block["caption"])
            )
    return markers


@pytest.mark.asyncio
@pytest.mark.parametrize("explicit", (False, True), ids=("v10-legacy", "v11-reordered"))
async def test_snapshot_publication_and_typst_share_the_canonical_element_order(
    explicit: bool,
    tmp_path: Path,
    typst_binary: str,
    font_bundle_root: Path,
) -> None:
    fixture = _parity_fixture(explicit=explicit)
    snapshot = fixture["snapshot"]
    enrichment = fixture["enrichment"]
    workspace_record = fixture["workspace_record"]
    markers = fixture["markers"]
    expected_markers = fixture["expected_markers"]
    after_paragraph_order = fixture["after_paragraph_order"]

    snapshot_markers = _snapshot_markers(workspace_record, fixture["synthesis"], enrichment)

    service, catalog, _artifacts = _service()
    assembly = await _assemble(
        service,
        run=_run(snapshot),
        snapshot=snapshot,
        references=fixture["references"],
        extraction=fixture["extraction"],
        synthesis=fixture["synthesis"],
        editorial_enrichment=enrichment,
        editorial_enrichment_artifact_id=workspace_record.enrichment_artifact_id,
        workspace_snapshot_record=workspace_record,
    )
    publication = parse_publication_document(
        await ProductionArtifactStore(catalog).read_json(assembly.artifact.canonical_blob_id)
    )
    assert isinstance(publication, (PublicationDocumentV9, PublicationDocumentV10))
    assert [item.element_key for item in publication.resolved_placements] == after_paragraph_order
    assert [item.element_position for item in publication.resolved_placements] == [0, 1, 2]

    template_bundle = load_template_bundle(_CHP_TYPST_ROOT)
    render_source = _render(publication, template_bundle)
    typst_markers = _typst_markers(render_source.render_data_bytes)

    assert snapshot_markers == expected_markers
    assert typst_markers == expected_markers
    assert snapshot_markers == typst_markers
    assert [
        markers[item.element_key] for item in publication.resolved_placements
    ] == expected_markers[2:5]

    workspace_root = tmp_path / ("parity-explicit" if explicit else "parity-legacy")
    _build_workspace(
        workspace_root,
        bundle_files=template_bundle.files,
        render_data_bytes=render_source.render_data_bytes,
        media_refs=render_source.media_refs,
    )
    font_snapshot = load_font_bundle_snapshot(font_bundle_root, _FONT_BUNDLE_LOCK)
    font_root = tmp_path / "parity-fonts"
    font_root.mkdir()
    compiled = await TypstSubprocessCompiler(binary=typst_binary).compile(
        TypstCompileRequest(
            workspace_root=workspace_root,
            entrypoint_relative_path="RENDERER/publication.typ",
            font_paths=materialize_font_bundle(font_snapshot, font_root),
        )
    )
    pdf_path = tmp_path / ("parity-explicit.pdf" if explicit else "parity-legacy.pdf")
    pdf_path.write_bytes(compiled.content)

    pdftotext = shutil.which("pdftotext")
    if pdftotext is not None:
        result = _run_blocking(
            (pdftotext, "-layout", str(pdf_path), "-"),
            capture_output=True,
            check=True,
            timeout=20,
        )
        pdf_text = " ".join(result.stdout.decode("utf-8", errors="replace").split()).casefold()
    else:
        reader = PdfReader(str(pdf_path), strict=True)
        pdf_text = " ".join(
            "\n".join(page.extract_text() or "" for page in reader.pages).split()
        ).casefold()
    positions = [pdf_text.index(marker.casefold()) for marker in expected_markers]
    assert positions == sorted(positions), (expected_markers, positions)
