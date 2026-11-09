from __future__ import annotations

import asyncio
import json
import subprocess
import xml.etree.ElementTree as ET
from dataclasses import replace
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest

from cti_app.application import publication_assembly as publication_assembly_module
from cti_app.application.brief_publication_builder import (
    BRIEF_ASSEMBLY_POLICY_VERSION,
    build_brief_publication_document_v5,
    wrap_publication_document_v7,
)
from cti_app.application.edition_typst_rendering import EditionTypstRenderer
from cti_app.application.media_assets import media_asset_id
from cti_app.application.production_relevance import (
    RelevanceProjectionExecution,
    RelevanceProjectionExecutionStatus,
    build_relevance_projection,
)
from cti_app.application.production_workflow import ProductionWorkflowOrchestrator
from cti_app.application.publication_assembly import PublicationAssemblyService
from cti_app.application.typst_rendering import (
    TypstRenderer,
    load_template_bundle,
    project_publication_to_typst_model,
)
from cti_app.domain.classification import TLP
from cti_app.domain.discovery import SourceRole
from cti_app.domain.edition_publication import EditionDocumentV2, EditionPublicationV2
from cti_app.domain.production import ProductionRun, ProductionStage
from cti_app.domain.production_editorial_enrichment import (
    EnrichmentPlacementKind,
    EnrichmentPlacementV1,
    EnrichmentTableKind,
    SourceFigureLocatorV1,
)
from cti_app.domain.production_pipeline import production_stages
from cti_app.domain.production_references import ProductionReferenceKind, ProductionReferenceTier
from cti_app.domain.production_routing import RoutingStatus
from cti_app.domain.production_synthesis import EvidenceKind, ExtractionEvidenceRefV1
from cti_app.domain.publication import (
    PublicationEvidenceKind,
    PublicationEvidenceRefV1,
    PublicationParagraphV1,
    PublicationReferenceEntryV1,
    PublicationSourceV1,
    PublicationTimelineEntryV1,
)
from cti_app.domain.publication_document import (
    PUBLICATION_DOCUMENT_V4_SCHEMA_VERSION,
    PublicationDocumentV4,
    PublicationDocumentV5,
    PublicationDocumentV8,
    PublicationSourceFigureV1,
    PublicationTableColumnV1,
    PublicationTableRowV1,
    PublicationTableV1,
    order_publication_references,
    parse_publication_document,
    publication_document_text_anchors,
    serialize_publication_document,
)
from cti_app.domain.publication_kind import PublicationKind
from cti_app.domain.semantic_annotation import (
    SEMANTIC_ANNOTATION_POLICY_VERSION,
    SEMANTIC_ANNOTATION_SCHEMA_VERSION,
    SemanticParagraphV1,
    SemanticRole,
    SemanticTextSpanV1,
    SemanticTextV1,
)
from tests.test_m19_typst_pdf_format import _compile_pdf, _extract_layout, _read_ppm
from tests.test_publication_builder_v4 import _canonical_inputs

_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
_TEMPLATE_ROOT = _REPOSITORY_ROOT / "chpTypst"
_REFERENCE_URLS = (
    "https://openai.example.test/report",
    "https://cnn.example.test/report",
)


async def _run_pdf_command(*arguments: str) -> bytes:
    process = await asyncio.create_subprocess_exec(
        *arguments,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=20)
    assert process.returncode == 0, stderr.decode(errors="replace")
    return stdout


def _assert_brief_timeline_rail(path: Path, pages, tmp_path: Path) -> None:
    page_index = next(
        index
        for index, page in enumerate(pages)
        if any(line["text"] == "Chronologie" for line in page["lines"])
    )
    page = pages[page_index]
    heading = next(line for line in page["lines"] if line["text"] == "Chronologie")
    last_event = next(
        line
        for line in page["lines"]
        if line["text"].startswith("Synthetic second event is documented.")
    )
    image_prefix = tmp_path / "brief-timeline-page"
    subprocess.run(
        (
            "pdftoppm",
            "-r",
            "72",
            "-f",
            str(page_index + 1),
            "-l",
            str(page_index + 1),
            "-singlefile",
            str(path),
            str(image_prefix),
        ),
        capture_output=True,
        check=True,
        timeout=20,
    )
    width, height, pixels = _read_ppm(image_prefix.with_suffix(".ppm"))
    y_start = max(0, int(heading["top"]))
    y_end = min(height, int(last_event["bottom"]) + 1)
    accent = bytes((36, 59, 83))
    longest_run = 0
    for x in range(int(width * 0.04), int(width * 0.2)):
        current_run = 0
        for y in range(y_start, y_end):
            offset = (y * width + x) * 3
            if pixels[offset : offset + 3] == accent:
                current_run += 1
                longest_run = max(longest_run, current_run)
            else:
                current_run = 0
    assert longest_run >= 20, longest_run


def _brief_v7_fixture(
    *,
    source_dates: tuple[date | None, date | None],
    include_sample_events: bool = False,
    include_enrichment: bool = False,
):
    source_ids = (UUID(int=8101), UUID(int=8102))
    publishers = ("OpenAI", "CNN")
    titles = ("Synthetic primary report", "Synthetic corroborating report")
    sources = tuple(
        PublicationSourceV1(
            source_document_id=source_id,
            canonical_url=url,
            title=title,
            publisher=publisher,
            published_at=published_at,
            tier=ProductionReferenceTier.CORE,
            kind=ProductionReferenceKind.PUBLICATION,
            role=SourceRole.PRIMARY,
        )
        for source_id, url, title, publisher, published_at in zip(
            source_ids, _REFERENCE_URLS, titles, publishers, source_dates, strict=True
        )
    )
    references = tuple(
        PublicationReferenceEntryV1(
            source_document_id=source_id,
            text=f"{publisher} publie « {title} ».",
            evidence_refs=(),
        )
        for source_id, publisher, title in zip(source_ids, publishers, titles, strict=True)
    )
    document = PublicationDocumentV4(
        schema_version=PUBLICATION_DOCUMENT_V4_SCHEMA_VERSION,
        subject_id=UUID(int=8100),
        publication_language="fr",
        title="Brève synthétique",
        lead=(
            PublicationParagraphV1(
                (
                    "SentinelTool records a payload for Example Lab using a technical field."
                    if include_enrichment
                    else "Two archived sources support this short update."
                ),
                (
                    PublicationEvidenceRefV1(
                        source_document_id=source_ids[0],
                        kind=PublicationEvidenceKind.FACT,
                        evidence_key="a" * 64,
                    ),
                ),
            ),
        ),
        sections=(),
        timeline=(
            (
                PublicationTimelineEntryV1(
                    event_date=date(2026, 3, 10),
                    date_text=None,
                    text="Synthetic first event is documented.",
                    evidence_refs=(
                        PublicationEvidenceRefV1(
                            source_ids[0], PublicationEvidenceKind.EVENT, "c" * 64
                        ),
                    ),
                ),
                PublicationTimelineEntryV1(
                    event_date=date(2026, 3, 11),
                    date_text=None,
                    text="Synthetic second event is documented.",
                    evidence_refs=(
                        PublicationEvidenceRefV1(
                            source_ids[1], PublicationEvidenceKind.EVENT, "d" * 64
                        ),
                    ),
                ),
            )
            if include_sample_events
            else ()
        ),
        indicators=(),
        sources=sources,
        uncertainties=(),
        tables=(),
        diagrams=(),
        figures=(),
        additional_source_ids=frozenset(source_ids),
    )
    if include_enrichment:
        source = sources[0]
        evidence_ref = ExtractionEvidenceRefV1(
            source.source_document_id, EvidenceKind.FACT, "e" * 64
        )
        locator = SourceFigureLocatorV1(page=1, figure_label="Archived source capture")
        document = replace(
            document,
            tables=(
                PublicationTableV1(
                    key="archived_observation",
                    kind=EnrichmentTableKind.CUSTOM,
                    title="Archived observation",
                    caption="Evidence recorded in the source.",
                    columns=(
                        PublicationTableColumnV1("entity", "Entity"),
                        PublicationTableColumnV1("role", "Role"),
                    ),
                    rows=(
                        PublicationTableRowV1(
                            ("Example Lab", "Reported organization"), (evidence_ref,)
                        ),
                    ),
                    placement=EnrichmentPlacementV1(EnrichmentPlacementKind.AFTER_LEAD),
                ),
            ),
            figures=(
                PublicationSourceFigureV1(
                    key="archived_capture",
                    asset_id=media_asset_id("f" * 64, "image/svg+xml"),
                    sha256="f" * 64,
                    mime_type="image/svg+xml",
                    byte_size=256,
                    source_document_id=source.source_document_id,
                    source_url=source.canonical_url,
                    caption="Archived source capture of Example Lab.",
                    provenance="Archived original media with source attribution.",
                    locator=locator,
                    placement=EnrichmentPlacementV1(EnrichmentPlacementKind.END),
                ),
            ),
        )
    ordered_references = order_publication_references(references, document.sources)
    anchors = publication_document_text_anchors(document, references=ordered_references)
    semantic_text = SemanticTextV1(
        schema_version=SEMANTIC_ANNOTATION_SCHEMA_VERSION,
        policy_version=SEMANTIC_ANNOTATION_POLICY_VERSION,
        paragraphs=tuple(
            SemanticParagraphV1(
                anchor=anchor,
                spans=(
                    (
                        SemanticTextSpanV1(SemanticRole.TOOL, "SentinelTool"),
                        SemanticTextSpanV1(SemanticRole.TEXT, " records a "),
                        SemanticTextSpanV1(SemanticRole.ENGLISH_TERM, "payload"),
                        SemanticTextSpanV1(SemanticRole.TEXT, " for "),
                        SemanticTextSpanV1(SemanticRole.PROPER_NAME, "Example Lab"),
                        SemanticTextSpanV1(SemanticRole.TEXT, " using a "),
                        SemanticTextSpanV1(SemanticRole.TECHNICAL, "technical field"),
                        SemanticTextSpanV1(SemanticRole.TEXT, "."),
                    )
                    if include_enrichment and anchor == "lead:0001"
                    else (SemanticTextSpanV1(SemanticRole.TEXT, text),)
                ),
            )
            for anchor, text in anchors.items()
        ),
    )
    base = PublicationDocumentV5(
        document=document,
        semantic_text=semantic_text,
        references=ordered_references,
    )
    publication = wrap_publication_document_v7(
        base,
        base_publication_artifact_id=UUID(int=8110),
        publication_kind=PublicationKind.BRIEF,
        routing_decision_hash="b" * 64,
        routing_policy_version="routing-policy-v1",
    )
    return publication


@pytest.mark.parametrize("wrap_v8", (False, True), ids=("v7", "v8"))
def test_historical_brief_publications_round_trip_without_enrichment(wrap_v8: bool) -> None:
    publication = _brief_v7_fixture(source_dates=(None, None))
    historical = (
        PublicationDocumentV8(base_publication=publication, article_format=None)
        if wrap_v8
        else publication
    )

    payload = serialize_publication_document(historical)
    reread = parse_publication_document(payload)

    assert reread == historical
    assert serialize_publication_document(reread) == payload


async def _render_brief_pdf(tmp_path, publication, *, edition: bool):
    manifest_name = "edition-renderer-manifest.json" if edition else "renderer-manifest.json"
    bundle = load_template_bundle(_TEMPLATE_ROOT, manifest_name=manifest_name)
    if edition:
        edition_document = EditionDocumentV2(
            edition={
                "id": "11111111-1111-4111-8111-111111111111",
                "country": "Example",
                "country_code": "EX",
                "period_start": "2026-03-01",
                "period_end": "2026-03-31",
                "tlp": TLP.CLEAR.value,
                "languages": ["fr"],
                "version": 1,
            },
            publications=(
                EditionPublicationV2(
                    position=1,
                    subject_id=publication.subject_id,
                    document=publication,
                ),
            ),
        )
        source = EditionTypstRenderer().render(
            edition_document,
            bundle,
            document_label="anonymized-brief-edition-fixture",
        )
        render_data = json.loads(source.render_data_bytes)
        render_publication = render_data["publications"][0]
        media_paths = tuple(item.media_path for item in source.media_refs)
        entrypoint = "RENDERER/edition.typ"
    else:
        source = TypstRenderer().render(
            publication,
            bundle,
            document_label="anonymized-brief-fixture",
        )
        render_data = json.loads(source.render_data_bytes)
        render_publication = render_data
        media_paths = tuple(item.media_path for item in source.media_refs)
        entrypoint = "RENDERER/publication.typ"
    path = await _compile_pdf(
        tmp_path,
        render_data,
        entrypoint=entrypoint,
        manifest_name=manifest_name,
        media_paths=media_paths,
    )
    return render_publication, path


@pytest.mark.parametrize("edition", (False, True), ids=("standalone", "edition"))
@pytest.mark.asyncio
async def test_brief_v7_references_render_first_with_source_footnotes_and_no_inline_urls(
    tmp_path, edition: bool
) -> None:
    publication = _brief_v7_fixture(
        source_dates=(date(2026, 3, 12), date(2026, 3, 13)),
        include_sample_events=True,
    )
    assert all(not reference.evidence_refs for reference in publication.base_publication.references)

    render_publication, pdf_path = await _render_brief_pdf(tmp_path, publication, edition=edition)
    sections = render_publication["content_sections"]
    references_section = sections[0]
    assert references_section["type"] == "references"
    assert len(references_section["entries"]) == 2

    text, pages = _extract_layout(pdf_path)
    assert text.index("Références") < text.index("Chronologie") < text.index("Synthèse")
    assert "OpenAI" in text and "Synthetic primary report" in text
    assert "CNN" in text and "Synthetic corroborating report" in text
    assert "12 mars 2026" in text and "13 mars 2026" in text
    assert all(url in text for url in _REFERENCE_URLS)
    reference_lines = [
        line["text"]
        for page in pages
        for line in page["lines"]
        if "OpenAI" in line["text"] or "CNN" in line["text"]
    ]
    assert reference_lines
    assert all(not any(url in line for url in _REFERENCE_URLS) for line in reference_lines)
    _assert_brief_timeline_rail(pdf_path, pages, tmp_path)


@pytest.mark.asyncio
async def test_brief_with_one_source_publication_date_has_no_chronology_pdf(tmp_path) -> None:
    publication = _brief_v7_fixture(source_dates=(date(2026, 3, 12), None))
    model = project_publication_to_typst_model(publication, include_brief_chronology=True)
    assert [section["type"] for section in model.content_sections] == ["references", "synthesis"]

    _rendered, pdf_path = await _render_brief_pdf(tmp_path, publication, edition=False)
    text, _pages = _extract_layout(pdf_path)
    assert "Références" in text
    assert "12 mars 2026" in text
    assert "Chronologie" not in text


@pytest.mark.parametrize("edition", (False, True), ids=("standalone", "edition"))
@pytest.mark.asyncio
async def test_enriched_brief_renders_archived_figure_table_and_semantic_styles(
    tmp_path, edition: bool
) -> None:
    publication = _brief_v7_fixture(
        source_dates=(None, None),
        include_enrichment=True,
    )
    render_publication, pdf_path = await _render_brief_pdf(tmp_path, publication, edition=edition)
    text, pages = _extract_layout(pdf_path)

    synthesis = next(
        section
        for section in render_publication["content_sections"]
        if section["type"] == "synthesis"
    )
    paragraph = next(
        block
        for block in synthesis["blocks"]
        if block["type"] == "paragraph" and "SentinelTool" in block["text"]
    )
    style_by_text = {span["text"]: span["style"] for span in paragraph["semantic_spans"]}
    assert style_by_text["SentinelTool"] == "semantic-tool"
    assert style_by_text["payload"] == "semantic-english-term"
    assert style_by_text["Example Lab"] == "semantic-proper-name"
    assert style_by_text["technical field"] == "semantic-technical"
    assert "Archived observation" in text
    assert "Reported organization" in text
    assert "Archived source capture of Example Lab." in text
    assert "Figure 1 : Archived source capture of Example Lab." in text
    for absent in ("Documenté, non observé", "Archived original media", "Attribution :"):
        assert absent not in text

    caption_line, caption_page = next(
        (line, page)
        for page in pages
        for line in page["lines"]
        if "Figure 1 : Archived source capture of Example Lab." in line["text"]
    )
    caption_center = (caption_line["left"] + caption_line["right"]) / 2
    assert abs(caption_center - caption_page["width"] / 2) <= 2.0

    xml_output = await _run_pdf_command(
        "pdftohtml", "-xml", "-stdout", "-fontfullname", str(pdf_path)
    )
    xml_root = ET.fromstring(xml_output.decode())
    font_names = {
        item.attrib["id"]: item.attrib.get("family", "").casefold()
        for item in xml_root.iter("fontspec")
    }
    rendered_fonts = [
        (
            "".join(item.itertext()).strip(),
            font_names.get(item.attrib.get("font", ""), ""),
        )
        for item in xml_root.iter("text")
    ]
    assert any(text == "SentinelTool" and "bold" in font for text, font in rendered_fonts)
    assert any(text == "payload" and "italic" in font for text, font in rendered_fonts)
    assert any(text == "Example Lab" and "italic" in font for text, font in rendered_fonts)
    assert any(text == "technical field" and "cascadia" in font for text, font in rendered_fonts)

    technical_line = next(
        line for page in pages for line in page["lines"] if "technical field" in line["text"]
    )
    technical_page_index = next(
        index for index, page in enumerate(pages) if technical_line in page["lines"]
    )
    image_prefix = tmp_path / "brief-enrichment-page"
    await _run_pdf_command(
        "pdftoppm",
        "-r",
        "72",
        "-f",
        str(technical_page_index + 1),
        "-singlefile",
        str(pdf_path),
        str(image_prefix),
    )
    width, height, pixels = _read_ppm(image_prefix.with_suffix(".ppm"))
    x_start = max(0, int(technical_line["left"]) - 3)
    x_end = min(width, int(technical_line["right"]) + 4)
    y_start = max(0, int(technical_line["top"]) - 3)
    y_end = min(height, int(technical_line["bottom"]) + 4)
    assert any(
        pixels[(y * width + x) * 3 : (y * width + x) * 3 + 3] == bytes((190, 190, 190))
        for x in range(x_start, x_end)
        for y in range(y_start, y_end)
    )


def test_archived_core_source_cited_in_brief_lead_is_a_publication_reference() -> None:
    snapshot, references, extraction, synthesis = _canonical_inputs()
    relevance = build_relevance_projection(snapshot, extraction)
    brief = build_brief_publication_document_v5(
        snapshot=snapshot,
        references=references,
        extraction=extraction,
        relevance_projection=relevance,
        synthesis=synthesis,
    )
    publication = wrap_publication_document_v7(
        brief,
        base_publication_artifact_id=UUID(int=8111),
        publication_kind=PublicationKind.BRIEF,
        routing_decision_hash="e" * 64,
        routing_policy_version="routing-policy-v1",
    )

    core_source = references.sources[0]
    assert core_source.tier is ProductionReferenceTier.CORE
    assert core_source.role is SourceRole.PRIMARY
    assert core_source.collection_state.value == "archived"
    assert any(
        ref.source_document_id == core_source.source_document_id
        for paragraph in synthesis.lead
        for ref in paragraph.evidence_refs
    )
    reference = next(
        item
        for item in publication.base_publication.base_publication.references
        if item.source_document_id == core_source.source_document_id
    )
    assert "Example publisher publie" in reference.text


def test_brief_assembly_policy_version_changes_the_reuse_identity(monkeypatch) -> None:
    assert BRIEF_ASSEMBLY_POLICY_VERSION == "brief-assembly-v3-m19-enrichment"
    assert len(BRIEF_ASSEMBLY_POLICY_VERSION) <= 64
    snapshot, references, extraction, synthesis = _canonical_inputs()
    relevance = build_relevance_projection(snapshot, extraction)
    identity_args = {
        "publication_kind": PublicationKind.BRIEF,
        "snapshot": snapshot,
        "references": references,
        "extraction": extraction,
        "synthesis": synthesis,
        "editorial_enrichment": None,
        "relevance_projection": relevance,
        "routing_decision_hash": "c" * 64,
        "routing_policy_version": "routing-policy-v1",
    }
    monkeypatch.setattr(
        publication_assembly_module,
        "BRIEF_ASSEMBLY_POLICY_VERSION",
        "brief-assembly-v1-sourced-timeline-no-ioc-annex",
    )
    old_identity = PublicationAssemblyService._assembly_input_hash(**identity_args)
    monkeypatch.setattr(
        publication_assembly_module,
        "BRIEF_ASSEMBLY_POLICY_VERSION",
        "brief-assembly-v2-m19-timeline-references",
    )
    m4a_identity = PublicationAssemblyService._assembly_input_hash(**identity_args)
    monkeypatch.setattr(
        publication_assembly_module,
        "BRIEF_ASSEMBLY_POLICY_VERSION",
        BRIEF_ASSEMBLY_POLICY_VERSION,
    )
    new_identity = PublicationAssemblyService._assembly_input_hash(**identity_args)
    assert old_identity != new_identity
    assert m4a_identity != new_identity


@pytest.mark.parametrize("publication_kind", (PublicationKind.ARTICLE, PublicationKind.BRIEF))
@pytest.mark.asyncio
async def test_workflow_dispatches_references_extraction_and_projection_before_routing(
    publication_kind: PublicationKind,
) -> None:
    snapshot, _references, extraction, _synthesis = _canonical_inputs()
    relevance = build_relevance_projection(snapshot, extraction)
    run = ProductionRun(subject_id=snapshot.subject_id, edition_id=snapshot.edition_id)
    run.start_running()
    events: list[str] = []
    decision = SimpleNamespace(
        status=RoutingStatus.DECIDED,
        publication_kind=publication_kind,
        decision_id=UUID(int=8120),
        decision_hash="d" * 64,
        policy_version="routing-policy-v1",
    )

    class Repository:
        async def get(self, _run_id):
            return run

        async def get_by_run(self, _run_id):
            return snapshot

        async def get_current(self, _run_id, stage):
            if stage == "extraction":
                return object()
            raise AssertionError(f"unexpected artifact stage: {stage}")

    class UnitOfWork:
        production_runs = Repository()
        production_input_snapshots = Repository()
        production_artifacts = Repository()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

    class RelevanceService:
        async def execute(self, _run, _snapshot, _extraction_artifact):
            events.append("relevance_projection")
            return RelevanceProjectionExecution(
                status=RelevanceProjectionExecutionStatus.SUCCEEDED,
                artifact=None,
                projection=relevance,
            )

    workflow = ProductionWorkflowOrchestrator.__new__(ProductionWorkflowOrchestrator)
    workflow._uow_factory = UnitOfWork
    workflow._active_generation = run.pipeline_generation
    workflow._relevance_projection = RelevanceService()
    workflow._diagnostics = SimpleNamespace(record_stage_outcome=lambda **_kwargs: None)

    async def record(stage):
        async def execute(_run, _context, _snapshot):
            events.append(stage.value)
            return {"stage": stage.value, "status": "success"}

        return execute

    workflow._execute_sources_stage = await record(ProductionStage.SOURCES)
    workflow._execute_references_stage = await record(ProductionStage.REFERENCES)
    workflow._execute_extraction_stage = await record(ProductionStage.EXTRACTION)

    async def ensure_routing(_run):
        events.append("routing_choice")
        return decision

    workflow._ensure_current_routing_decision = ensure_routing

    for stage in (
        ProductionStage.SOURCES,
        ProductionStage.REFERENCES,
        ProductionStage.EXTRACTION,
        ProductionStage.RELEVANCE_PROJECTION,
    ):
        run.current_stage = stage
        result = await workflow.execute_stage(run.id, stage)

    assert events == [
        "sources",
        "references",
        "extraction",
        "relevance_projection",
        "routing_choice",
    ]
    assert result["publication_kind"] == publication_kind.value
    assert production_stages(publication_kind)[:4] == (
        ProductionStage.SOURCES,
        ProductionStage.REFERENCES,
        ProductionStage.EXTRACTION,
        ProductionStage.RELEVANCE_PROJECTION,
    )
