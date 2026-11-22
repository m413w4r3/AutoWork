"""Real Typst/PDF parity for figure replacement and element removal (F2-03c).

After a replacement the PDF must show the new caption exactly once and never
the old one; after a removal the element and its caption must disappear. The
fixtures reuse the shipped CHP template and the parity helpers, so the check is
on the actual compiled PDF, not only on the render data.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from pypdf import PdfReader

from cti_app.application.editorial_figure_catalog import opaque_figure_id
from cti_app.application.editorial_visual_revision import (
    VisualRevisionContext,
    plan_visual_revision,
)
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.application.production_editorial_enrichment import (
    EditorialFigureCatalogEntry,
)
from cti_app.application.source_figure_inventory import SourceFigureCatalogMetadata
from cti_app.application.typst_compilation import (
    TypstCompileRequest,
    load_font_bundle_snapshot,
    materialize_font_bundle,
)
from cti_app.application.typst_rendering import load_template_bundle
from cti_app.domain.editorial_enrichment_patch import (
    RemoveEnrichmentElementPatch,
    ReplaceSourceFigurePatch,
)
from cti_app.domain.production_editorial_enrichment import (
    EditorialEnrichmentElementKind,
    EditorialEnrichmentV1,
    EnrichmentPlacementKind,
    EnrichmentPlacementV1,
    ResolvedSourceFigureV1,
    SourceFigureCandidateV1,
    SourceFigureDecision,
    SourceFigureInclusionStatus,
    SourceFigureLocatorV1,
    source_figure_id,
)
from cti_app.domain.publication_document import parse_publication_document
from cti_app.domain.publication_kind import PublicationKind
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

OLD_CAPTION = "Ancienne légende de figure remplacée."
NEW_CAPTION = "Nouvelle légende de figure retenue."


def _figure(
    *,
    caption: str,
    page: int,
    sha_seed: str,
) -> tuple[ResolvedSourceFigureV1, SourceFigureCandidateV1]:
    snapshot, _references, extraction, _synthesis = _canonical_inputs()
    del snapshot
    source = extraction.sources[0]
    locator = SourceFigureLocatorV1(page=page, figure_label=f"Synthetic figure {page}")
    sha256 = hashlib.sha256(sha_seed.encode()).hexdigest()
    resolved = ResolvedSourceFigureV1(
        figure_id=source_figure_id(
            source_document_id=source.source_document_id,
            sha256=sha256,
            source=source.canonical_url,
            locator=locator,
        ),
        blob_id=UUID(int=9200 + page),
        sha256=sha256,
        mime_type="image/png",
        byte_size=128,
        source_document_id=source.source_document_id,
        source=source.canonical_url,
        provenance="Synthetic archived figure fixture.",
        locator=locator,
        decision=SourceFigureDecision.ACCEPTED,
        decision_reason="matched_archived_blob",
    )
    candidate = SourceFigureCandidateV1(
        key="figure_one",
        source_document_id=source.source_document_id,
        source_url=source.canonical_url,
        caption=caption,
        provenance="Synthetic archived figure fixture.",
        locator=locator,
        inclusion_status=SourceFigureInclusionStatus.INCLUDED,
        placement=EnrichmentPlacementV1(EnrichmentPlacementKind.AFTER_LEAD),
        resolved_figure=resolved,
        width=640,
        height=480,
        order=0,
    )
    return resolved, candidate


def _catalog_entry(resolved: ResolvedSourceFigureV1, handle: str) -> EditorialFigureCatalogEntry:
    return EditorialFigureCatalogEntry(
        handle=handle,
        source_role="primary",
        editorial_role="analysis",
        figure=resolved,
        metadata=SourceFigureCatalogMetadata(width=640, height=480),
    )


def _context(
    enrichment: EditorialEnrichmentV1,
    catalog: tuple[EditorialFigureCatalogEntry, ...],
    *,
    production_run_id: UUID,
) -> VisualRevisionContext:
    _snapshot, _references, extraction, synthesis = _canonical_inputs()
    return VisualRevisionContext(
        base_enrichment=enrichment,
        evidence_pack=None,
        extraction=extraction,
        synthesis=synthesis,
        figure_catalog=catalog,
        registered_anchor_ids=None,
        production_run_id=production_run_id,
        publication_kind=PublicationKind.ARTICLE,
    )


def _base_enrichment() -> tuple[
    EditorialEnrichmentV1, ResolvedSourceFigureV1, ResolvedSourceFigureV1, UUID
]:
    snapshot, _references, extraction, synthesis = _canonical_inputs()
    old_resolved, old_candidate = _figure(caption=OLD_CAPTION, page=1, sha_seed="old")
    new_resolved, _new_candidate = _figure(caption=NEW_CAPTION, page=2, sha_seed="new")
    base = build_empty_editorial_enrichment(extraction=extraction, synthesis=synthesis)
    enrichment = replace(base, source_figures=(old_candidate,))
    return enrichment, old_resolved, new_resolved, snapshot.production_run_id


async def _render_pdf_text(
    enrichment: EditorialEnrichmentV1,
    *,
    tmp_path: Path,
    typst_binary: str,
    font_bundle_root: Path,
    label: str,
) -> tuple[str, dict[str, Any]]:
    snapshot, references, extraction, synthesis = _canonical_inputs()
    service, catalog, _artifacts = _service()
    workspace_record = _workspace_record_for(
        run=_run(snapshot), synthesis=synthesis, enrichment=enrichment
    )
    assembly = await _assemble(
        service,
        run=_run(snapshot),
        snapshot=snapshot,
        references=references,
        extraction=extraction,
        synthesis=synthesis,
        editorial_enrichment=enrichment,
        editorial_enrichment_artifact_id=workspace_record.enrichment_artifact_id,
        workspace_snapshot_record=workspace_record,
    )
    publication = parse_publication_document(
        await ProductionArtifactStore(catalog).read_json(assembly.artifact.canonical_blob_id)
    )
    template_bundle = load_template_bundle(_CHP_TYPST_ROOT)
    render_source = _render(publication, template_bundle)
    workspace_root = tmp_path / label
    _build_workspace(
        workspace_root,
        bundle_files=template_bundle.files,
        render_data_bytes=render_source.render_data_bytes,
        media_refs=render_source.media_refs,
    )
    font_snapshot = load_font_bundle_snapshot(font_bundle_root, _FONT_BUNDLE_LOCK)
    font_root = tmp_path / f"{label}-fonts"
    font_root.mkdir()
    compiled = await TypstSubprocessCompiler(binary=typst_binary).compile(
        TypstCompileRequest(
            workspace_root=workspace_root,
            entrypoint_relative_path="RENDERER/publication.typ",
            font_paths=materialize_font_bundle(font_snapshot, font_root),
        )
    )
    pdf_path = tmp_path / f"{label}.pdf"
    pdf_path.write_bytes(compiled.content)
    pdftotext = shutil.which("pdftotext")
    if pdftotext is not None:
        result = _run_blocking(
            (pdftotext, "-layout", str(pdf_path), "-"),
            capture_output=True,
            check=True,
            timeout=20,
        )
        text = " ".join(result.stdout.decode("utf-8", errors="replace").split()).casefold()
    else:
        reader = PdfReader(str(pdf_path), strict=True)
        text = " ".join(
            "\n".join(page.extract_text() or "" for page in reader.pages).split()
        ).casefold()
    render_data = json.loads(render_source.render_data_bytes)
    return text, render_data


def _figure_blocks(render_data: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        block
        for section in render_data["content_sections"]
        for block in section.get("blocks", [])
        if block.get("type") == "figure"
    ]


@pytest.mark.asyncio
async def test_replaced_figure_renders_the_new_caption_once_and_hides_the_old(
    tmp_path: Path,
    typst_binary: str,
    font_bundle_root: Path,
) -> None:
    enrichment, old_resolved, new_resolved, run_id = _base_enrichment()
    context = _context(
        enrichment,
        (
            _catalog_entry(old_resolved, "F001"),
            _catalog_entry(new_resolved, "F002"),
        ),
        production_run_id=run_id,
    )
    replacement_id = UUID(opaque_figure_id(enrichment.subject_id, run_id, new_resolved.figure_id))
    plan = plan_visual_revision(
        context,
        ReplaceSourceFigurePatch(
            element_key="figure_one",
            figure_id=replacement_id,
            caption=NEW_CAPTION,
        ),
    )
    assert plan.revised_enrichment.source_figures[0].resolved_figure == new_resolved

    text, render_data = await _render_pdf_text(
        plan.revised_enrichment,
        tmp_path=tmp_path,
        typst_binary=typst_binary,
        font_bundle_root=font_bundle_root,
        label="replace-figure",
    )

    assert text.count(NEW_CAPTION.casefold()) == 1
    assert OLD_CAPTION.casefold() not in text
    figures = _figure_blocks(render_data)
    assert len(figures) == 1
    assert str(figures[0]["caption"]).casefold() == NEW_CAPTION.casefold()


@pytest.mark.asyncio
async def test_removed_figure_disappears_from_the_pdf(
    tmp_path: Path,
    typst_binary: str,
    font_bundle_root: Path,
) -> None:
    enrichment, _old_resolved, _new_resolved, run_id = _base_enrichment()
    context = _context(enrichment, (), production_run_id=run_id)
    plan = plan_visual_revision(
        context,
        RemoveEnrichmentElementPatch(EditorialEnrichmentElementKind.FIGURE, "figure_one"),
    )
    assert plan.revised_enrichment.source_figures == ()

    text, render_data = await _render_pdf_text(
        plan.revised_enrichment,
        tmp_path=tmp_path,
        typst_binary=typst_binary,
        font_bundle_root=font_bundle_root,
        label="remove-figure",
    )

    assert OLD_CAPTION.casefold() not in text
    assert _figure_blocks(render_data) == []
