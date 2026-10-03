"""Freeze the CHP renderer's deliberate direct composition of PublicationDocumentV4.

The production Typst renderer does not call the historical ``article()`` helper:
V4 has no category, article number, or Victimologie/Arsenal/Objectif overview
fields. This is an intentional schema boundary. Both render paths must still
use the same CHP page identity, shared typography, and reusable content helpers.
"""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import replace
from io import BytesIO
from pathlib import Path

import pytest
from pypdf import PdfReader

from cti_app.application.semantic_annotation import EnglishTermDetector, SemanticAnnotator
from cti_app.application.typst_compilation import (
    TYPST_COMPILER_VERSION,
    TypstCompileRequest,
    load_font_bundle_snapshot,
    materialize_font_bundle,
)
from cti_app.application.typst_rendering import (
    TemplateFile,
    TypstMediaRef,
    TypstRenderer,
    load_template_bundle,
)
from cti_app.domain.production_editorial_enrichment import EnrichmentPlacementKind
from cti_app.domain.publication_document import (
    PublicationDocumentV4,
    PublicationDocumentV5,
    publication_document_text_anchors,
)
from cti_app.domain.semantic_annotation import (
    SEMANTIC_ANNOTATION_POLICY_VERSION,
    SEMANTIC_ANNOTATION_SCHEMA_VERSION,
    SemanticAnnotationProposalV1,
    SemanticRole,
    SemanticTextV1,
)
from cti_app.infrastructure.typst_compiler import TypstSubprocessCompiler
from tests.test_typst_rendering import (
    _diagram_at,
    _figure_at,
    _full_document,
    _table_at,
)

pytest_plugins = ("tests.test_typst_compiler_runtime",)

_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
_CHP_TYPST_ROOT = _REPOSITORY_ROOT / "chpTypst"
_FONT_BUNDLE_LOCK = _REPOSITORY_ROOT / "infra" / "typst-fonts.lock"
_IMPORT_RE = re.compile(r'^\s*#import\s+"([^"]+)"\s*:\s*(.*?)\s*$', re.MULTILINE)
_IDENTIFIER_RE = re.compile(r"\*|[A-Za-z_][A-Za-z_0-9-]*")


@pytest.fixture
def chp_parity_document() -> PublicationDocumentV4:
    """Use the existing domain-backed V4 fixture and its rich-block helpers."""
    document = _full_document(
        tables=(
            _table_at(
                "parity-table",
                "Observed command table",
                EnrichmentPlacementKind.AFTER_LEAD,
            ),
        ),
        diagrams=(
            replace(
                _diagram_at(
                    "parity-diagram",
                    "Diagram asset title",
                    EnrichmentPlacementKind.AFTER_SECTION,
                    index=0,
                ),
                caption="Diagram asset caption.",
            ),
        ),
        figures=(
            _figure_at(
                "parity-figure",
                "Source figure caption.",
                EnrichmentPlacementKind.END,
            ),
        ),
    )
    return replace(
        document,
        title="CHP visual parity fixture",
        lead=(
            replace(document.lead[0], text="Synthesis lead paragraph for parity coverage."),
            replace(document.lead[1], text="A second lead paragraph."),
        ),
        sections=(
            replace(
                document.sections[0],
                heading="synthesis_internal_heading",
                paragraphs=(
                    replace(
                        document.sections[0].paragraphs[0],
                        text="Section paragraph describing the observed activity.",
                    ),
                ),
            ),
        ),
        uncertainties=(
            replace(
                document.uncertainties[0],
                text="extraction_source_skipped:fixture",
            ),
            replace(
                document.uncertainties[0],
                text="synthesis_output_invalid:fixture",
            ),
        ),
    )


def _typst_imports(source: str) -> dict[str, set[str]]:
    imports: dict[str, set[str]] = {}
    for module_path, raw_identifiers in _IMPORT_RE.findall(source):
        identifiers = set(_IDENTIFIER_RE.findall(raw_identifiers))
        imports.setdefault(module_path, set()).update(identifiers)
    return imports


def _resolved_imports(
    source_path: Path, *, importer_path: Path | None = None
) -> dict[Path, set[str]]:
    import_directory = (importer_path or source_path).parent
    return {
        (import_directory / relative_path).resolve(): identifiers
        for relative_path, identifiers in _typst_imports(
            source_path.read_text(encoding="utf-8")
        ).items()
    }


def _header_footer_labels(source: str) -> set[str]:
    """Read visible CHP labels from the literals in header_footer.typ itself."""
    labels = {
        " ".join(match.group(1).split()) for match in re.finditer(r"\)\s*\[([^\[\]#]+)\]", source)
    }
    labels.update(
        " ".join(match.group(1).split())
        for match in re.finditer(r"(?m)^\s{4,}([A-ZÀ-ÖØ-Þ][^#\n\[\](),:]{2,})\s*$", source)
    )
    return {label for label in labels if label}


def _normalized_pdf_text(reader: PdfReader) -> tuple[str, str]:
    extracted = "\n".join(page.extract_text() or "" for page in reader.pages)
    normalized = " ".join(extracted.split())
    return normalized, re.sub(r"\s+", "", extracted)


def _embedded_visual_xobject_count(reader: PdfReader) -> int:
    """Count image and form XObjects, including vector SVG forms, across pages."""
    count = 0
    for page in reader.pages:
        resources = page.get("/Resources")
        if resources is None:
            continue
        xobjects = resources.get_object().get("/XObject")
        if xobjects is None:
            continue
        for reference in xobjects.get_object().values():
            subtype = reference.get_object().get("/Subtype")
            if subtype in ("/Image", "/Form"):
                count += 1
    return count


def _build_workspace(
    workspace_root: Path,
    *,
    bundle_files: tuple[TemplateFile, ...],
    render_data_bytes: bytes,
    media_refs: tuple[TypstMediaRef, ...],
) -> None:
    for template_file in bundle_files:
        destination = workspace_root / template_file.relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(template_file.content)

    renderer_root = workspace_root / "RENDERER"
    (renderer_root / "render-data.json").write_bytes(render_data_bytes)
    media_root = renderer_root / "media"
    media_root.mkdir(parents=True, exist_ok=True)
    for media_ref in media_refs:
        destination = workspace_root / "RENDERER" / media_ref.media_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        if media_ref.expected_mime_type == "image/svg+xml":
            destination.write_text(
                '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 240 90">'
                '<rect x="2" y="2" width="236" height="86" rx="8" '
                'fill="#E8EEF3" stroke="#243B53" stroke-width="3"/>'
                '<path d="M35 45h65m0 0-12-12m12 12-12 12M100 45h70" '
                'fill="none" stroke="#243B53" stroke-width="4"/></svg>',
                encoding="utf-8",
            )
        elif media_ref.expected_mime_type == "image/png":
            shutil.copyfile(_CHP_TYPST_ROOT / "UTILS" / "chap.png", destination)
        else:
            raise AssertionError(f"Unexpected fixture media type: {media_ref.expected_mime_type}")


def test_direct_v4_composition_is_deliberate_and_shares_chp_visual_modules() -> None:
    """Keep the V4 schema boundary while sharing the human template's CHP identity."""
    production_entry = _CHP_TYPST_ROOT / "RENDERER" / "publication.typ"
    human_entry = _CHP_TYPST_ROOT / "TEMPLATES" / "article.typ"
    # `main.typ` is what applies the document style to the human articles; the
    # TEMPLATES/article.typ fragment is included inside it.
    human_document_entry = _CHP_TYPST_ROOT / "main.typ"
    production_source = production_entry.read_text(encoding="utf-8")
    human_source = human_entry.read_text(encoding="utf-8")
    human_document_source = human_document_entry.read_text(encoding="utf-8")
    production_imports = _resolved_imports(production_entry)
    production_helpers_path = (_CHP_TYPST_ROOT / "RENDERER" / "publication_helpers.typ").resolve()
    production_helpers_source = production_helpers_path.read_text(encoding="utf-8")
    # The template is copied to an article directory before Typst resolves its
    # ../../UTILS imports.
    human_imports = _resolved_imports(
        human_entry,
        importer_path=_CHP_TYPST_ROOT / "articles" / "article01" / "article01.typ",
    )
    human_document_imports = _resolved_imports(human_document_entry)

    document_style = (_CHP_TYPST_ROOT / "UTILS" / "document_style.typ").resolve()
    publication_helpers = (_CHP_TYPST_ROOT / "RENDERER" / "publication_helpers.typ").resolve()
    shared_helpers = (_CHP_TYPST_ROOT / "UTILS" / "helpers.typ").resolve()
    colors = (_CHP_TYPST_ROOT / "UTILS" / "colors.typ").resolve()
    header_footer = (_CHP_TYPST_ROOT / "UTILS" / "header_footer.typ").resolve()
    assert production_imports[document_style] == {"apply-document-style"}
    assert human_document_imports[document_style] == {"apply-document-style"}
    assert production_imports[publication_helpers] == {"render-publication"}
    assert human_imports[shared_helpers] >= {"article", "ioc-list", "styled-table"}
    assert re.search(r"#apply-document-style\s*\[\s*#render-publication\s*\(", production_source)
    assert re.search(r"#apply-document-style\s*\[", human_document_source)
    assert re.search(r"#article\s*\(", human_source)
    assert not re.search(r"\barticle\s*\(", production_source + production_helpers_source)
    assert re.search(
        r"heading\(level:\s*1\).*?publication\.title",
        production_helpers_source,
        re.DOTALL,
    )

    production_helpers_imports = _resolved_imports(production_helpers_path)
    assert production_helpers_imports[shared_helpers] >= {
        "section-title",
        "timeline",
        "styled-table",
        "ioc-list",
        "source-list",
    }
    assert production_helpers_imports[colors] == {"grey"}
    style_imports = _resolved_imports(document_style)
    assert style_imports[header_footer] == {"report-header", "report-footer"}
    assert style_imports[colors] == {"purple"}
    assert _resolved_imports(shared_helpers)[colors] == {"*"}
    assert _resolved_imports(header_footer)[colors] == {"*"}
    style_source = document_style.read_text(encoding="utf-8")
    helper_source = shared_helpers.read_text(encoding="utf-8")
    color_source = colors.read_text(encoding="utf-8")
    assert re.search(r"paper:\s*\"a4\"", style_source)
    assert re.search(r"font:\s*\"Hanken Grotesk\"", style_source)
    assert re.search(r"header:\s*report-header", style_source)
    assert re.search(r"footer:\s*report-footer", style_source)
    assert re.search(r"show heading\.where\(level:\s*1\).*?fill:\s*purple", style_source, re.DOTALL)
    assert re.search(r"#let article\s*\(.*?fill:\s*purple", helper_source, re.DOTALL)
    assert re.search(r"#let section-title\s*\(.*?fill:\s*purple", helper_source, re.DOTALL)
    assert re.search(r"#let purple\s*=\s*rgb\(\"#[0-9A-Fa-f]{6}\"\)", color_source)


@pytest.mark.asyncio
async def test_real_typst_pdf_preserves_chp_publication_structure(
    tmp_path: Path,
    typst_binary: str,
    font_bundle_root: Path,
    chp_parity_document: PublicationDocumentV4,
) -> None:
    template_bundle = load_template_bundle(_CHP_TYPST_ROOT)
    render_source = TypstRenderer().render(chp_parity_document, template_bundle)
    workspace_root = tmp_path / "workspace"
    _build_workspace(
        workspace_root,
        bundle_files=template_bundle.files,
        render_data_bytes=render_source.render_data_bytes,
        media_refs=render_source.media_refs,
    )

    font_snapshot = load_font_bundle_snapshot(font_bundle_root, _FONT_BUNDLE_LOCK)
    font_root = tmp_path / "font-snapshot"
    font_root.mkdir()
    request = TypstCompileRequest(
        workspace_root=workspace_root,
        entrypoint_relative_path="RENDERER/publication.typ",
        font_paths=materialize_font_bundle(font_snapshot, font_root),
    )
    compiled = await TypstSubprocessCompiler(binary=str(typst_binary)).compile(request)
    reader = PdfReader(BytesIO(compiled.content), strict=True)
    assert compiled.compiler_version == TYPST_COMPILER_VERSION
    assert len(reader.pages) >= 1
    assert not reader.is_encrypted

    text, compact_text = _normalized_pdf_text(reader)
    header_source = (_CHP_TYPST_ROOT / "UTILS" / "header_footer.typ").read_text(encoding="utf-8")
    header_labels = _header_footer_labels(header_source)
    assert header_labels
    for label in header_labels:
        assert label in text

    expected_text = (
        "CHP visual parity fixture",
        "RÉFÉRENCES",
        "Chronologie",
        "No display date",
        "One source event",
        "Several source event",
        "Sources complémentaires",
        "Primary source",
        "SYNTHÈSE",
        "Synthesis lead paragraph for parity coverage.",
        "A second lead paragraph.",
        "Observed command table",
        "Command",
        "Purpose",
        "-enc",
        "Execution",
        "Section paragraph describing the observed activity.",
        "Diagram asset title",
        "Diagram asset caption.",
        "Source figure caption.",
        "Provenance : Figure 1 from the source publication",
        "Display ip",
        "Display domain",
        "Display url",
        "Display email",
        "Display hash",
        "ANNEXE TECHNIQUE — INDICATEURS",
        "Example Lab",
        "Duplicate URL source",
    )
    for expected in expected_text:
        assert expected in text
    assert "synthesis_internal_heading" not in text
    assert "extraction_source_skipped" not in text
    assert "synthesis_output_invalid" not in text
    assert "Attribution remains unresolved" not in text

    for url in (
        "https://example.test/one",
        "https://example.test/two",
    ):
        assert url.replace(" ", "") in compact_text
    assert "2025-01-02" in text
    ordered_markers = (
        "CHP visual parity fixture",
        "RÉFÉRENCES",
        "Chronologie",
        "No display date",
        "Sources complémentaires",
        "SYNTHÈSE",
        "Synthesis lead paragraph for parity coverage.",
        "Observed command table",
        "Diagram asset title",
        "Source figure caption.",
        "ANNEXE TECHNIQUE — INDICATEURS",
        "Display ip",
    )
    marker_positions = tuple(text.index(marker) for marker in ordered_markers)
    assert marker_positions == tuple(sorted(marker_positions))
    assert text.count("Primary source") == 1
    assert text.count("Synthesis lead paragraph for parity coverage.") == 1
    assert text.count("A second lead paragraph.") == 1

    assert {media_ref.expected_mime_type for media_ref in render_source.media_refs} == {
        "image/svg+xml",
        "image/png",
    }
    assert _embedded_visual_xobject_count(reader) >= len(render_source.media_refs)


async def test_real_typst_renders_annotated_literal_content_without_evaluating_it(
    tmp_path: Path,
    typst_binary: str,
    font_bundle_root: Path,
    chp_parity_document: PublicationDocumentV4,
) -> None:
    command = '`curl "$x" #import "evil" ] $math$`'
    lead_text = f"APT Étoile used the literal command {command}."
    base = replace(
        chp_parity_document,
        title="APT Étoile activity",
        lead=(
            replace(chp_parity_document.lead[0], text=lead_text),
            *chp_parity_document.lead[1:],
        ),
    )
    annotator = SemanticAnnotator(EnglishTermDetector(()))
    proposal = SemanticAnnotationProposalV1(
        paragraph_anchor="lead:0001",
        role=SemanticRole.COMMAND,
        text=command,
    )
    semantic_text = SemanticTextV1(
        schema_version=SEMANTIC_ANNOTATION_SCHEMA_VERSION,
        policy_version=SEMANTIC_ANNOTATION_POLICY_VERSION,
        paragraphs=tuple(
            annotator.annotate_paragraph(
                anchor=anchor,
                text=text,
                entities=((SemanticRole.ACTOR, "APT Étoile"),),
                proposals=(proposal,),
            )
            for anchor, text in publication_document_text_anchors(base).items()
        ),
    )
    document = PublicationDocumentV5(document=base, semantic_text=semantic_text)
    template_bundle = load_template_bundle(_CHP_TYPST_ROOT)
    render_source = TypstRenderer().render(document, template_bundle)
    render_data = json.loads(render_source.render_data_bytes)
    first_paragraph = render_data["content_sections"][1]["blocks"][0]
    assert any(span["style"] == "semantic-actor" for span in first_paragraph["semantic_spans"])
    assert any(span["style"] == "semantic-command" for span in first_paragraph["semantic_spans"])
    assert b"render-publication" in render_source.source_bytes
    shared_helpers = (_CHP_TYPST_ROOT / "UTILS" / "helpers.typ").read_text(encoding="utf-8")
    assert "#let semantic-command(body)" in shared_helpers
    renderer_helpers = (_CHP_TYPST_ROOT / "RENDERER" / "publication_helpers.typ").read_text(
        encoding="utf-8"
    )
    assert "semantic-or-plain(item.text" in renderer_helpers

    workspace_root = tmp_path / "semantic-workspace"
    _build_workspace(
        workspace_root,
        bundle_files=template_bundle.files,
        render_data_bytes=render_source.render_data_bytes,
        media_refs=render_source.media_refs,
    )
    font_snapshot = load_font_bundle_snapshot(font_bundle_root, _FONT_BUNDLE_LOCK)
    font_root = tmp_path / "semantic-font-snapshot"
    font_root.mkdir()
    request = TypstCompileRequest(
        workspace_root=workspace_root,
        entrypoint_relative_path="RENDERER/publication.typ",
        font_paths=materialize_font_bundle(font_snapshot, font_root),
    )
    compiled = await TypstSubprocessCompiler(binary=str(typst_binary)).compile(request)
    text, compact_text = _normalized_pdf_text(PdfReader(BytesIO(compiled.content), strict=True))

    assert "APT Étoile" in text
    assert 'curl "$x"' in text
    for literal in ("#import", "evil", "]", "$math$", "`"):
        assert literal in text or literal.replace(" ", "") in compact_text
