"""Real-Typst oracle for the placement and pagination of analyst notes (M24 R07).

Two independent regressions are pinned here:

* a note anchored on a paragraph the reader projection actually emits must be
  printed right after that paragraph, and a note may not anchor on a paragraph
  the projection drops (lead-duplicated section paragraph);
* a long analyst note stays breakable: every stored paragraph reaches the PDF
  instead of being silently clipped at a page boundary.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from cti_app.application.typst_compilation import (
    TypstCompileRequest,
    load_font_bundle_snapshot,
    materialize_font_bundle,
)
from cti_app.application.typst_rendering import (
    TypstDraftPreviewDocument,
    load_template_bundle,
)
from cti_app.domain.analyst_opinion import (
    AnalystObservationRelation,
    AnalystObservationState,
    AnalystObservationV1,
    AnalystOpinionStatus,
    AnalystOpinionV1,
    opinion_blocks_with_observations,
)
from cti_app.domain.publication import (
    PublicationParagraphV1,
    PublicationSectionKind,
    PublicationSectionV1,
)
from cti_app.domain.publication_document import (
    PublicationDocumentV5,
    publication_document_text_anchors,
    publication_document_v5_sha256,
)
from cti_app.domain.semantic_annotation import (
    SemanticParagraphV1,
    SemanticRole,
    SemanticTextSpanV1,
    SemanticTextV1,
)
from cti_app.infrastructure.typst_compiler import TypstSubprocessCompiler
from tests.test_publication_document_v6 import _base_v5
from tests.test_typst_chp_parity import (
    _CHP_TYPST_ROOT,
    _FONT_BUNDLE_LOCK,
    _build_workspace,
    _render,
)

pytest_plugins = ("tests.test_typst_compiler_runtime",)

LEAD_ONE = "LEAD-ONE: le chargeur synthetique prepare le second etage."
LEAD_TWO = "LEAD-TWO: la verification porte sur les octets observes."
ANCHOR_PARAGRAPH = "ANCHOR-PARAGRAPH: la seconde etape est confirmee."
TAIL_PARAGRAPH = "TAIL-PARAGRAPH: portee synthetique finale."
NOTE_AT_LEAD = "NOTE-AT-LEAD-TWO: lien verifie avec les octets observes."
NOTE_AT_SECTION = "NOTE-AT-SECTION-ONE: le second etage est confirme."


def _placement_publication() -> PublicationDocumentV5:
    base = _base_v5()
    evidence = base.document.lead[0].evidence_refs
    document = replace(
        base.document,
        lead=(
            PublicationParagraphV1(LEAD_ONE, evidence),
            PublicationParagraphV1(LEAD_TWO, evidence),
        ),
        sections=(
            # The overview restates the first lead paragraph: the projection
            # does not emit it as a body paragraph (the R07 B1 reproducer).
            PublicationSectionV1(
                kind=PublicationSectionKind.OVERVIEW,
                heading="Vue d'ensemble",
                paragraphs=(PublicationParagraphV1(LEAD_ONE, evidence),),
            ),
            PublicationSectionV1(
                kind=PublicationSectionKind.TECHNICAL,
                heading="ANALYSIS-HEADING: seconde etape",
                paragraphs=(PublicationParagraphV1(ANCHOR_PARAGRAPH, evidence),),
            ),
            PublicationSectionV1(
                kind=PublicationSectionKind.TECHNICAL,
                heading="TAIL-HEADING: portee",
                paragraphs=(PublicationParagraphV1(TAIL_PARAGRAPH, evidence),),
            ),
        ),
    )
    anchors = publication_document_text_anchors(document, references=base.references)
    semantic_text = SemanticTextV1(
        schema_version=base.semantic_text.schema_version,
        policy_version=base.semantic_text.policy_version,
        paragraphs=tuple(
            SemanticParagraphV1(
                anchor=anchor,
                spans=(SemanticTextSpanV1(SemanticRole.TEXT, text),),
            )
            for anchor, text in anchors.items()
        ),
    )
    return replace(base, document=document, semantic_text=semantic_text)


def _note(anchor: str, text: str) -> AnalystObservationV1:
    return AnalystObservationV1(
        id=uuid4(),
        anchor=anchor,
        text=text,
        state=AnalystObservationState.UNRESOLVED,
        relation=AnalystObservationRelation.UNRESOLVED,
    )


def _draft(
    publication: PublicationDocumentV5,
    notes: tuple[AnalystObservationV1, ...],
) -> TypstDraftPreviewDocument:
    opinion = AnalystOpinionV1(
        investigation_id=uuid4(),
        synthesis_artifact_id=uuid4(),
        synthesis_sha256="a" * 64,
        base_publication_artifact_id=uuid4(),
        base_publication_sha256=publication_document_v5_sha256(publication),
        blocks=opinion_blocks_with_observations((), notes),
        author_id="fixture-analyst",
        status=AnalystOpinionStatus.DRAFT,
    )
    return TypstDraftPreviewDocument(publication, opinion)


def _pdf_text(pdf_path: Path) -> str:
    pdftotext = shutil.which("pdftotext")
    assert pdftotext is not None, "pdftotext is required for this real-Typst oracle"
    result = subprocess.run(
        (pdftotext, "-layout", str(pdf_path), "-"),
        capture_output=True,
        check=True,
        text=True,
        timeout=20,
    )
    return " ".join(result.stdout.split())


async def _compile(
    tmp_path: Path,
    *,
    render_source: Any,
    template_bundle: Any,
    typst_binary: str,
    font_bundle_root: Path,
) -> Path:
    workspace = tmp_path / "note-placement-workspace"
    _build_workspace(
        workspace,
        bundle_files=template_bundle.files,
        render_data_bytes=render_source.render_data_bytes,
        media_refs=render_source.media_refs,
    )
    font_snapshot = load_font_bundle_snapshot(font_bundle_root, _FONT_BUNDLE_LOCK)
    font_root = tmp_path / "note-placement-fonts"
    font_root.mkdir()
    compiled = await TypstSubprocessCompiler(binary=typst_binary).compile(
        TypstCompileRequest(
            workspace_root=workspace,
            entrypoint_relative_path=render_source.entrypoint_relative_path,
            font_paths=materialize_font_bundle(font_snapshot, font_root),
        )
    )
    pdf_path = tmp_path / "note-placement.pdf"
    pdf_path.write_bytes(compiled.content)
    return pdf_path


def test_deduplicated_section_anchor_is_not_renderable() -> None:
    publication = _placement_publication()
    renderable = {
        anchor
        for anchor in publication_document_text_anchors(
            publication.document, references=publication.references
        )
        if anchor.startswith(("lead:", "section:"))
    }
    # The overview paragraph exists in the text anchor space but is dropped by
    # the reader projection: accepting it would silently move the note.
    assert "section:0:paragraph:0001" in renderable
    with pytest.raises(ValueError, match="stale or unsupported"):
        _draft(
            publication,
            (_note("section:0:paragraph:0001", "NOTE-DEDUPLICATED"),),
        )


@pytest.mark.asyncio
async def test_real_typst_renders_notes_right_after_their_anchor_paragraph(
    tmp_path: Path,
    typst_binary: str,
    font_bundle_root: Path,
) -> None:
    publication = _placement_publication()
    draft = _draft(
        publication,
        (
            _note("lead:0002", NOTE_AT_LEAD),
            _note("section:1:paragraph:0001", NOTE_AT_SECTION),
        ),
    )
    template_bundle = load_template_bundle(_CHP_TYPST_ROOT)
    render_source = _render(draft, template_bundle)
    data = json.loads(render_source.render_data_bytes)
    synthesis = next(
        section for section in data["content_sections"] if section["type"] == "synthesis"
    )
    notes = [block for block in synthesis["blocks"] if block["type"] == "analyst_observation"]
    assert [note["text"] for note in notes] == [NOTE_AT_LEAD, NOTE_AT_SECTION]
    assert all(note.get("anchor_target") for note in notes)
    analyst_opinion = next(
        (section for section in data["content_sections"] if section["type"] == "analyst_opinion"),
        None,
    )
    assert analyst_opinion is None or analyst_opinion["unanchored_observations"] == []

    pdf_path = await _compile(
        tmp_path,
        render_source=render_source,
        template_bundle=template_bundle,
        typst_binary=typst_binary,
        font_bundle_root=font_bundle_root,
    )
    text = _pdf_text(pdf_path)
    positions = [
        text.index(marker)
        for marker in (
            LEAD_ONE,
            LEAD_TWO,
            NOTE_AT_LEAD,
            "ANALYSIS-HEADING",
            ANCHOR_PARAGRAPH,
            NOTE_AT_SECTION,
            TAIL_PARAGRAPH,
        )
    ]
    assert positions == sorted(positions), (positions, text[:4000])
    assert "NOTE-DEDUPLICATED" not in text


@pytest.mark.asyncio
async def test_real_typst_long_note_keeps_every_paragraph(
    tmp_path: Path,
    typst_binary: str,
    font_bundle_root: Path,
) -> None:
    publication = _placement_publication()
    paragraph_count = 72
    markers = [f"LONG-NOTE-PARAGRAPH-{index:03d}" for index in range(paragraph_count)]
    note_text = "\n\n".join(
        f"{marker} : l'observation synthetique reste qualifiee et ses limites sont explicites."
        for marker in markers
    )
    draft = _draft(publication, (_note("lead:0001", note_text),))
    template_bundle = load_template_bundle(_CHP_TYPST_ROOT)
    render_source = _render(draft, template_bundle)
    data = json.loads(render_source.render_data_bytes)
    synthesis = next(
        section for section in data["content_sections"] if section["type"] == "synthesis"
    )
    projected = next(
        block for block in synthesis["blocks"] if block["type"] == "analyst_observation"
    )
    assert projected["text"] == note_text

    pdf_path = await _compile(
        tmp_path,
        render_source=render_source,
        template_bundle=template_bundle,
        typst_binary=typst_binary,
        font_bundle_root=font_bundle_root,
    )
    text = _pdf_text(pdf_path)
    missing = [marker for marker in markers if marker not in text]
    assert missing == [], f"truncated analyst note paragraphs: {missing}"
