"""Real Typst parity for a typed in-place chart revision (F2-03b).

The rendered PDF must show the revised chart caption once and none of the base
caption/title. Axis labels, point labels and the title live inside the compiled
SVG image, so the test also asserts the freshly compiled SVG carries the new
display text and none of the old one.
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from io import BytesIO
from pathlib import Path
from uuid import UUID

import pytest
from pypdf import PdfReader

from cti_app.application.editorial_visual_revision import (
    VisualRevisionContext,
    plan_visual_revision,
)
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.application.production_editorial_enrichment import (
    build_editorial_enrichment_evidence_pack,
    canonical_synthesis_hash,
)
from cti_app.application.production_synthesis import canonical_extraction_hash
from cti_app.application.typst_compilation import (
    TypstCompileRequest,
    load_font_bundle_snapshot,
    materialize_font_bundle,
)
from cti_app.application.typst_rendering import load_template_bundle
from cti_app.domain.editorial_enrichment_patch import ReviseChartPatch
from cti_app.domain.editorial_workspace import bootstrap_block_registry
from cti_app.domain.production import ProductionEvidenceBasis
from cti_app.domain.production_editorial_enrichment import (
    ChartKind,
    ChartPointV1,
    ChartSpecV1,
    EditorialAnalyticPurposeV1,
    EnrichmentPlacementKind,
    EnrichmentPlacementV1,
)
from cti_app.domain.production_extraction import ExtractionFactV1
from cti_app.domain.production_synthesis import production_synthesis_to_json
from cti_app.domain.publication_document import (
    PublicationDocumentV10,
    parse_publication_document,
)
from cti_app.infrastructure.analytic_chart_renderer import AnalyticChartRenderer
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
    _normalized_pdf_text,
    _render,
)

pytest_plugins = ("tests.test_typst_compiler_runtime",)

BASE_TITLE = "Ancien titre du graphique révisé"
BASE_CAPTION = "Ancienne légende du graphique révisé"
REVISED_TITLE = "Nouveau titre du graphique révisé"
REVISED_CAPTION = "Nouvelle légende du graphique révisé"
BASE_ASSET_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
REVISED_ASSET_ID = UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc")


@pytest.mark.asyncio
async def test_typst_pdf_renders_only_the_revised_chart_caption(
    tmp_path: Path,
    typst_binary: str,
    font_bundle_root: Path,
) -> None:
    snapshot, references, extraction, synthesis = _canonical_inputs()
    source = extraction.sources[0]
    quantitative_facts = (
        ExtractionFactV1(
            category="other_technical",
            value="Alpha: 12 endpoints",
            attack_id=None,
            context="Synthetic chart revision category Alpha.",
            evidence_quote="The synthetic report records Alpha with 12 endpoints.",
            evidence_basis=ProductionEvidenceBasis.SOURCE_VERIFIED,
            source_document_ids=(source.source_document_id,),
        ),
        ExtractionFactV1(
            category="other_technical",
            value="Beta: 18 endpoints",
            attack_id=None,
            context="Synthetic chart revision category Beta.",
            evidence_quote="The synthetic report records Beta with 18 endpoints.",
            evidence_basis=ProductionEvidenceBasis.SOURCE_VERIFIED,
            source_document_ids=(source.source_document_id,),
        ),
    )
    extraction = replace(
        extraction, sources=(replace(source, facts=(*source.facts, *quantitative_facts)),)
    )
    synthesis = replace(synthesis, extraction_hash=canonical_extraction_hash(extraction))
    evidence_pack = build_editorial_enrichment_evidence_pack(snapshot, extraction, synthesis, None)
    refs_by_value = {
        str(record["value"]): evidence_pack.resolve_handle(str(record["handle"]))
        for record in evidence_pack.narrative_evidence
        if record.get("value") in {"Alpha: 12 endpoints", "Beta: 18 endpoints"}
    }
    alpha_ref = refs_by_value["Alpha: 12 endpoints"]
    beta_ref = refs_by_value["Beta: 18 endpoints"]
    run = _run(snapshot)
    synthesis_artifact_id = UUID(int=9781)
    anchor = next(
        entry
        for entry in bootstrap_block_registry(
            artifact_id=synthesis_artifact_id,
            canonical_sha256=canonical_synthesis_hash(synthesis),
            source_stage="synthesis",
            canonical_content=production_synthesis_to_json(synthesis),
        )
        if entry.kind.value in {"paragraph", "lead_paragraph"}
    )
    placement = EnrichmentPlacementV1(
        EnrichmentPlacementKind.AFTER_PARAGRAPH,
        anchor_block_id=anchor.block_id,
    )
    base_chart = ChartSpecV1(
        key="synthetic_endpoint_counts",
        kind=ChartKind.BAR,
        title=BASE_TITLE,
        caption=BASE_CAPTION,
        placement=placement,
        purpose=EditorialAnalyticPurposeV1(
            question="Quels comptes d'endpoints sont rapportés pour Alpha et Beta ?",
            available_data="Deux nombres exacts sont cités pour ces catégories.",
            comprehension_gain="La comparaison rend visible l'écart entre les valeurs.",
            scope="Uniquement Alpha et Beta dans cette fixture.",
            evidence_refs=(alpha_ref, beta_ref),
            knowledge_limits="Aucune autre catégorie n'est extrapolée.",
            placement_reason="Après le paragraphe d'ancrage.",
        ),
        points=(
            ChartPointV1("Alpha", None, "endpoints", (alpha_ref,), value=Decimal("12")),
            ChartPointV1("Beta", None, "endpoints", (beta_ref,), value=Decimal("18")),
        ),
        category_axis_label="Ancienne catégorie",
        value_axis_label="Ancien nombre rapporté",
        unit="endpoints",
        compiled_asset_id=BASE_ASSET_ID,
    )
    enrichment = replace(
        build_empty_editorial_enrichment(extraction=extraction, synthesis=synthesis),
        charts=(base_chart,),
    )
    revised = replace(
        base_chart,
        title=REVISED_TITLE,
        caption=REVISED_CAPTION,
        category_axis_label="Nouvelle catégorie",
        value_axis_label="Nouveau nombre rapporté",
        points=(
            replace(base_chart.points[0], label="Alpha révisé"),
            replace(base_chart.points[1], label="Beta révisé"),
        ),
    )

    plan = plan_visual_revision(
        VisualRevisionContext(
            base_enrichment=enrichment,
            evidence_pack=evidence_pack,
            extraction=extraction,
            synthesis=synthesis,
            registered_anchor_ids=frozenset({anchor.block_id}),
        ),
        ReviseChartPatch(element_key=base_chart.key, chart=revised),
    )
    assert plan.compile_chart is not None
    compiled_chart = await AnalyticChartRenderer().compile(plan.compile_chart)
    rendered_chart = replace(plan.compile_chart, compiled_asset_id=REVISED_ASSET_ID)
    revised_enrichment = replace(
        plan.revised_enrichment,
        charts=(rendered_chart,),
    )
    workspace_record = _workspace_record_for(
        run=run,
        synthesis=synthesis,
        enrichment=revised_enrichment,
        synthesis_artifact_id=synthesis_artifact_id,
    )

    service, catalog, _artifacts = _service()
    assembly = await _assemble(
        service,
        run=run,
        snapshot=snapshot,
        references=references,
        extraction=extraction,
        synthesis=synthesis,
        editorial_enrichment=revised_enrichment,
        editorial_enrichment_artifact_id=workspace_record.enrichment_artifact_id,
        workspace_snapshot_record=workspace_record,
    )
    document = parse_publication_document(
        await ProductionArtifactStore(catalog).read_json(assembly.artifact.canonical_blob_id)
    )
    assert isinstance(document, PublicationDocumentV10)
    assert [item.element_key for item in document.resolved_placements] == [base_chart.key]

    template_bundle = load_template_bundle(_CHP_TYPST_ROOT)
    render_source = _render(document, template_bundle)
    workspace_root = tmp_path / "chart-revision-workspace"
    _build_workspace(
        workspace_root,
        bundle_files=template_bundle.files,
        render_data_bytes=render_source.render_data_bytes,
        media_refs=render_source.media_refs,
    )
    chart_media = next(
        item for item in render_source.media_refs if item.asset_id == REVISED_ASSET_ID
    )
    chart_path = workspace_root / "RENDERER" / chart_media.media_path
    chart_path.parent.mkdir(parents=True, exist_ok=True)
    chart_path.write_bytes(compiled_chart.media_bytes)
    assert all(
        token in compiled_chart.media_bytes
        for token in (
            REVISED_TITLE.encode(),
            "Nouvelle catégorie".encode(),
            "Nouveau nombre rapporté".encode(),
            "Alpha révisé".encode(),
            "Beta révisé".encode(),
        )
    )
    assert BASE_TITLE.encode() not in compiled_chart.media_bytes
    assert BASE_CAPTION.encode() not in compiled_chart.media_bytes

    font_snapshot = load_font_bundle_snapshot(font_bundle_root, _FONT_BUNDLE_LOCK)
    font_root = tmp_path / "chart-revision-fonts"
    font_root.mkdir()
    compiled = await TypstSubprocessCompiler(binary=typst_binary).compile(
        TypstCompileRequest(
            workspace_root=workspace_root,
            entrypoint_relative_path="RENDERER/publication.typ",
            font_paths=materialize_font_bundle(font_snapshot, font_root),
        )
    )
    reader = PdfReader(BytesIO(compiled.content), strict=True)
    text, _compact = _normalized_pdf_text(reader)
    normalized = " ".join(text.split()).casefold()
    assert normalized.count(REVISED_CAPTION.casefold()) == 1
    assert BASE_CAPTION.casefold() not in normalized
    assert BASE_TITLE.casefold() not in normalized
