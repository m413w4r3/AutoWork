from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import unicodedata
import xml.etree.ElementTree as ET
from dataclasses import replace
from datetime import date
from io import BytesIO
from itertools import pairwise
from pathlib import Path
from typing import Any, NoReturn
from uuid import UUID

import anyio
import pytest
from pypdf import PdfReader
from pypdf.generic import ArrayObject, DictionaryObject, IndirectObject, StreamObject

from cti_app.application.production_normalization import normalize_indicator_value
from cti_app.application.publication_augmentation import augment_publication_with_opinion
from cti_app.application.typst_compilation import (
    TYPST_COMPILER_VERSION,
    TypstCompileRequest,
    load_font_bundle_snapshot,
    materialize_font_bundle,
)
from cti_app.application.typst_rendering import (
    TypstRenderer,
    _breakable_typst_display_text,
    _table_cell_typst_spans,
    load_template_bundle,
)
from cti_app.domain.publication import (
    ArtifactType,
    PublicationIndicatorGroupV1,
    PublicationIndicatorV1,
    PublicationParagraphV1,
    PublicationSectionKind,
    PublicationSectionV1,
    PublicationTimelineEntryV1,
)
from cti_app.domain.publication_document import (
    PublicationDocumentV5,
    PublicationTableColumnV1,
    PublicationTableRowV1,
    publication_document_text_anchors,
)
from cti_app.domain.semantic_annotation import (
    SEMANTIC_ANNOTATION_POLICY_VERSION,
    SEMANTIC_ANNOTATION_SCHEMA_VERSION,
    SemanticParagraphV1,
    SemanticRole,
    SemanticTextSpanV1,
    SemanticTextV1,
    section_paragraph_anchor,
)
from cti_app.infrastructure.typst_compiler import TypstSubprocessCompiler
from tests.test_publication_augmentation import _reduced_article03_draft_preview
from tests.test_publication_builder_v4 import _cited_technical_context_case
from tests.test_publication_document_v6 import _approved_opinion, _base_v5
from tests.test_publication_v4 import _SOURCE_ID
from tests.test_typst_rendering import _full_document, _table

_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
_FONT_BUNDLE_LOCK = _REPOSITORY_ROOT / "infra" / "typst-fonts.lock"
_PUBLICATION_RENDER_LABEL = "Publication de base — artefact de fixture v1"
_COPYABLE_HASH = "0123456789abcdef" * 4
_COPYABLE_LONG_URL = "https://example.test/" + "a" * (200 - len("https://example.test/"))
_COPYABLE_EXACT_URL = "https://micbucket.ams1.vultrobjects.com/Exclude/Telegram.exe"
_COPYABLE_DEFANGED_LONG_URL = "hxxps://example[.]test/" + "a" * (200 - len("https://example.test/"))
_COPYABLE_DEFANGED_EXACT_URL = (
    "hxxps://micbucket[.]ams1[.]vultrobjects[.]com/Exclude/Telegram[.]exe"
)
_COPYABLE_WINDOWS_PATH = (
    r"C:\ProgramData\ssh-cache-default{8bda3848-495e-43f4-8d10-7d37a67f1604}\RuntimeSSH.exe"
)
_COPYABLE_REGISTRY_PATH = r"HKCU\Software\Microsoft\Windows\CurrentVersion\Run"
_COPYABLE_TABLE_URL = "https://example.test/" + "u" * 99
_PDF_INTERNAL_UUID = re.compile(
    rb"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
    re.IGNORECASE,
)
_PDF_AUTOWORK_URI = re.compile(rb"autowork://", re.IGNORECASE)


def _assert_pdf_has_no_internal_identity_data(
    pdf_bytes: bytes,
    *,
    internal_ids: tuple[str, ...] = (),
) -> PdfReader:
    """Check raw bytes, decoded streams, metadata, destinations, and annotations."""
    reader = PdfReader(BytesIO(pdf_bytes), strict=True)
    payloads = [pdf_bytes]
    pending: list[Any] = [reader.trailer]
    visited_references: set[tuple[int, int]] = set()
    visited_direct_objects: set[int] = set()

    while pending:
        value = pending.pop()
        if isinstance(value, IndirectObject):
            identity = (value.idnum, value.generation)
            if identity in visited_references:
                continue
            visited_references.add(identity)
            value = value.get_object()
        if isinstance(value, (ArrayObject, DictionaryObject, StreamObject)):
            identity = id(value)
            if identity in visited_direct_objects:
                continue
            visited_direct_objects.add(identity)

        payloads.append(str(value).encode("utf-8", errors="replace"))
        if isinstance(value, StreamObject):
            payloads.append(value.get_data())
            pending.extend(value.values())
        elif isinstance(value, DictionaryObject):
            pending.extend(value.values())
        elif isinstance(value, ArrayObject):
            pending.extend(value)

    metadata_views = (
        ("PDF document metadata", reader.metadata),
        ("PDF XMP metadata", reader.xmp_metadata),
        ("PDF named destinations", reader.named_destinations),
    )
    for name, view in metadata_views:
        if view is not None:
            payloads.append(str(view).encode("utf-8", errors="replace"))
            _assert_pdf_identity_payload_is_clean(payloads[-1], name)

    for page_index, page in enumerate(reader.pages, start=1):
        payloads.append((page.extract_text() or "").encode("utf-8"))
        contents = page.get_contents()
        if contents is not None:
            payloads.append(contents.get_data())
        annotations = page.get("/Annots", ())
        if isinstance(annotations, IndirectObject):
            annotations = annotations.get_object()
        for annotation_reference in annotations:
            annotation = (
                annotation_reference.get_object()
                if isinstance(annotation_reference, IndirectObject)
                else annotation_reference
            )
            annotation_payload = str(annotation).encode("utf-8", errors="replace")
            _assert_pdf_identity_payload_is_clean(
                annotation_payload,
                f"PDF page {page_index} annotation",
            )

    for index, payload in enumerate(payloads):
        _assert_pdf_identity_payload_is_clean(payload, f"PDF bytes/object/stream {index}")
    for identifier in internal_ids:
        identifier_bytes = identifier.encode("ascii").lower()
        assert all(identifier_bytes not in payload.lower() for payload in payloads), (
            "PDF contains a source, finding, or observation identifier"
        )
    return reader


def _assert_pdf_identity_payload_is_clean(payload: bytes, location: str) -> None:
    assert _PDF_AUTOWORK_URI.search(payload) is None, f"{location} contains an autowork URI target"
    assert _PDF_INTERNAL_UUID.search(payload) is None, (
        f"{location} contains an internal UUID; standard PDF /ID values are undashed hex"
    )


_COPYABLE_DEFANGED_URL = "hxxps://a[.]b[.]c"
_COPYABLE_VALUES = (
    _COPYABLE_HASH,
    _COPYABLE_LONG_URL,
    _COPYABLE_EXACT_URL,
    _COPYABLE_REGISTRY_PATH,
    _COPYABLE_TABLE_URL,
    "https://a.b.c",
)


def _run_blocking(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
    # Short Poppler/Typst probes in tests; thread offloading adds nothing here.
    return subprocess.run(*args, **kwargs)


def _compile_real_typst_cli(
    *,
    typst_binary: str,
    workspace_root: Path,
    entrypoint: str,
    font_paths: tuple[Path, ...],
) -> bytes:
    argv = [typst_binary, "compile", "--root", str(workspace_root)]
    for font_path in font_paths:
        argv.extend(("--font-path", str(font_path)))
    argv.extend(
        (
            "--ignore-system-fonts",
            "--creation-timestamp",
            "0",
            str(workspace_root / entrypoint),
            str(workspace_root / "output.pdf"),
        )
    )
    subprocess.run(tuple(argv), capture_output=True, check=True, timeout=30)
    return (workspace_root / "output.pdf").read_bytes()


def _copyable_ioc_document():
    groups = (
        (ArtifactType.IP, ("198.51.100.42",)),
        (ArtifactType.DOMAIN, ("host-" + "d" * 58 + ".example.test",)),
        (
            ArtifactType.URL,
            (
                _COPYABLE_LONG_URL,
                _COPYABLE_EXACT_URL,
                _COPYABLE_WINDOWS_PATH,
                _COPYABLE_DEFANGED_URL,
            ),
        ),
        (ArtifactType.EMAIL, ("analyst-" + "e" * 64 + "@example.test",)),
        (ArtifactType.HASH, (_COPYABLE_HASH,)),
    )
    indicators = tuple(
        PublicationIndicatorGroupV1(
            artifact_type,
            tuple(
                PublicationIndicatorV1(
                    value=value,
                    normalized_value=normalize_indicator_value(value, artifact_type),
                    artifact_type=artifact_type,
                    source_document_ids=(_SOURCE_ID,),
                )
                for value in values
            ),
        )
        for artifact_type, values in groups
    )
    table = replace(
        _table(),
        title="Narrow hash column",
        rows=(replace(_table().rows[0], cells=("SHA-256", _COPYABLE_HASH)),),
    )
    return replace(_full_document(tables=(table,)), indicators=indicators)


def _install_narrow_technical_table(render_data: dict[str, Any]) -> None:
    if "content_sections" in render_data:
        publications = [render_data]
    else:
        publications = render_data["publications"]
    table = next(
        block
        for publication in publications
        for section in publication["content_sections"]
        for block in section["blocks"]
        if block["type"] == "table"
    )
    table["columns"] = ["Observable", "Niveau de contrôle", "Contrôle recommandé"]
    table["rows"] = [
        [_COPYABLE_REGISTRY_PATH, "Registre Windows", "Surveiller la clé"],
        [_COPYABLE_TABLE_URL, "URL", "Bloquer la destination"],
        [_COPYABLE_HASH, "SHA-256", "Comparer l'empreinte"],
    ]
    table["column_weights"] = [0.8, 0.8, 1.6]
    table["semantic_cells"] = [
        _table_cell_typst_spans(value) for row in table["rows"] for value in row
    ]

    paragraph = next(
        block
        for publication in publications
        for section in publication["content_sections"]
        for block in section["blocks"]
        if block["type"] == "paragraph"
    )
    paragraph["text"] = f"Run key: {_COPYABLE_REGISTRY_PATH}."
    paragraph["semantic_spans"] = [
        {"style": "semantic-plain", "text": "Run key: "},
        {
            "style": "semantic-technical-literal",
            **_breakable_typst_display_text(_COPYABLE_REGISTRY_PATH),
        },
        {"style": "semantic-plain", "text": "."},
    ]


def _assert_exact_copyable_pdf_text(reader: PdfReader, values: tuple[str, ...]) -> str:
    extracted = "\n".join(page.extract_text() or "" for page in reader.pages)
    formatting_chars = tuple(
        (character, f"U+{ord(character):04X}")
        for character in extracted
        if unicodedata.category(character) == "Cf"
    )
    assert not formatting_chars, (
        f"format characters found in extracted PDF text: {formatting_chars!r}"
    )
    without_linebreaks = extracted.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "")
    for value in values:
        assert value in without_linebreaks, f"PDF text does not reconstruct {value!r}"
    return extracted


def _write_review_pdf(directory: Path, name: str, content: bytes) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_bytes(content)
    return path


def _review_pngs(directory: Path, pattern: str) -> list[Path]:
    return sorted(directory.glob(pattern))


@pytest.fixture(scope="session")
def font_bundle_root() -> Path:
    configured_root = os.environ.get("FONT_BUNDLE_ROOT")
    return Path(configured_root) if configured_root else _REPOSITORY_ROOT / "chpTypst"


def _skip_unless_ci(reason: str) -> NoReturn:
    """Mirror test_d2_diagram_compiler_runtime.py: fail hard in CI, skip locally."""
    if os.environ.get("CI", "").lower() == "true":
        pytest.fail(reason)
    pytest.skip(reason)


@pytest.fixture(scope="session")
def typst_binary() -> str:
    """Mirror the d2_binary fixture: use a pre-installed pinned binary only.

    CI installs Typst once via scripts/install-typst.sh and exposes it as
    TYPST_BINARY/PATH (see .github/workflows/ci.yml), matching the existing D2
    pattern. FONT_BUNDLE_ROOT selects the matching installed font bundle and
    falls back to the local ignored chpTypst fonts when available. Local runs
    without a pinned Typst on PATH skip instead of re-downloading per session.
    """
    binary = os.environ.get("TYPST_BINARY") or shutil.which("typst")
    if binary is None:
        _skip_unless_ci(
            "typst executable is not installed; run scripts/install-typst.sh or set TYPST_BINARY"
        )
    assert binary is not None
    try:
        probe = subprocess.run((binary, "--version"), capture_output=True, check=False, timeout=10)
    except (OSError, subprocess.TimeoutExpired) as exc:
        _skip_unless_ci(f"typst version check failed: {exc}")
    reported = probe.stdout.decode("utf-8", errors="replace").strip()
    reported_parts = reported.split()
    reported_version = (
        reported_parts[1] if len(reported_parts) >= 2 and reported_parts[0] == "typst" else ""
    )
    if probe.returncode != 0 or reported_version != TYPST_COMPILER_VERSION:
        _skip_unless_ci(f"typst {TYPST_COMPILER_VERSION} is required, found {reported!r}")
    return binary


def _runtime_render_data() -> dict[str, Any]:
    table_rows = [
        ["Contexte", "Deux observations consignées dans le rapport"],
        ["Hash SHA-256", "a" * 64],
        [
            "URL de téléchargement",
            "https://downloads.example.test/releases/2026/10/06/example-rat-loader-package?campaign=autumn-update&source=bulletin",
        ],
        ["Adresse IP", "198.51.100.42"],
        ["Domaine", "cdn.example.test"],
    ]
    return {
        "schema_version": "typst-publication-model-v17-synthesis-section-headings",
        "document_label": _PUBLICATION_RENDER_LABEL,
        "language": "fr",
        "title": "AW-019 runtime publication fixture",
        "content_sections": [
            {
                "type": "references",
                "timeline": [
                    {
                        "display_date": "2026-09-30",
                        "text": "First observed event in the fixture timeline.",
                        "source_urls": ["https://example.test/timeline"],
                    },
                    {
                        "display_date": "2026-10-01",
                        "text": "A second timeline event.",
                        "source_urls": [],
                    },
                ],
                "blocks": [],
                "sources": [
                    {
                        "title": "Example runtime source",
                        "publisher": "AutoWork fixture",
                        "date": "2026-10-01",
                        "url": "https://example.test/source",
                    }
                ],
            },
            {
                "type": "synthesis",
                "blocks": [
                    {"type": "paragraph", "text": "This paragraph exercises the publication lead."},
                    {
                        "type": "table",
                        "key": "lead-table",
                        "title": "Observed technical values",
                        "caption": "Exact values from the cited source.",
                        "columns": ["Type", "Value"],
                        "rows": table_rows,
                        "semantic_cells": [
                            _table_cell_typst_spans(value) for row in table_rows for value in row
                        ],
                    },
                    {"type": "paragraph", "text": "First section content."},
                    {
                        "type": "figure",
                        "figure_number": 1,
                        "key": "runtime-figure",
                        "caption": "Vendored CHP image used as a figure.",
                        "source_note": "example.test — p. 1",
                        "media_path": "media/figure.png",
                    },
                    {"type": "paragraph", "text": "Second section content."},
                    {
                        "type": "table",
                        "key": "end-table",
                        "title": "End placement table",
                        "caption": "A rich block after the final section.",
                        "columns": ["Check", "Result"],
                        "rows": [["PDF", "valid"], ["Page size", "A4"]],
                    },
                ],
            },
            {
                "type": "technical_annex",
                "indicators": {
                    "ips": ["192.0.2.10"],
                    "domains": ["example.test"],
                    "urls": ["https://example.test/path"],
                    "emails": ["analyst@example.test"],
                    "hashes": ["0123456789abcdef0123456789abcdef"],
                },
                "original_indicators": {
                    "ips": [],
                    "domains": [],
                    "urls": [],
                    "emails": [],
                    "hashes": [],
                },
                "original_indicator_note": "Le lien avec le sujet n'est pas démontré.",
            },
        ],
    }


def _build_runtime_workspace(
    repository_root: Path,
    workspace_root: Path,
    *,
    render_data: dict[str, Any] | None = None,
    media_files: dict[str, str] | None = None,
) -> None:
    chp_typst_root = repository_root / "chpTypst"
    shutil.copytree(chp_typst_root / "RENDERER", workspace_root / "RENDERER")
    shutil.copytree(chp_typst_root / "UTILS", workspace_root / "UTILS")
    media_root = workspace_root / "RENDERER" / "media"
    media_root.mkdir()
    (media_root / "diagram.svg").write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 240 72">'
        '<rect x="1" y="1" width="238" height="70" rx="8" fill="#eee" stroke="#555"/>'
        '<text x="12" y="43" font-size="18">local diagram</text></svg>',
        encoding="utf-8",
    )
    shutil.copy2(chp_typst_root / "UTILS" / "chap.png", media_root / "figure.png")
    for name, content in (media_files or {}).items():
        (media_root / name).write_text(content, encoding="utf-8")
    data = json.dumps(
        render_data if render_data is not None else _runtime_render_data(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    (workspace_root / "RENDERER" / "render-data.json").write_text(data, encoding="utf-8")


@pytest.mark.asyncio
async def test_real_typst_article_preserves_copyable_technical_literals(
    tmp_path: Path,
    typst_binary: str,
    font_bundle_root: Path,
) -> None:
    typst_root = _REPOSITORY_ROOT / "chpTypst"
    bundle = load_template_bundle(typst_root)
    document = _copyable_ioc_document()
    source = TypstRenderer().render(document, bundle, document_label=_PUBLICATION_RENDER_LABEL)
    render_data = json.loads(source.render_data_bytes)
    canonical_url_values = [
        indicator.value
        for group in document.indicators
        if group.artifact_type is ArtifactType.URL
        for indicator in group.indicators
    ]
    assert _COPYABLE_DEFANGED_URL in canonical_url_values
    _install_narrow_technical_table(render_data)

    workspace_root = tmp_path / "copyable-article-workspace"
    _build_runtime_workspace(_REPOSITORY_ROOT, workspace_root, render_data=render_data)
    font_snapshot = load_font_bundle_snapshot(font_bundle_root, _FONT_BUNDLE_LOCK)
    font_root = tmp_path / "copyable-article-fonts"
    font_root.mkdir()
    compiled = await TypstSubprocessCompiler(binary=typst_binary).compile(
        TypstCompileRequest(
            workspace_root=workspace_root,
            entrypoint_relative_path="RENDERER/publication.typ",
            font_paths=materialize_font_bundle(font_snapshot, font_root),
        )
    )
    reader = PdfReader(BytesIO(compiled.content), strict=True)
    extracted = _assert_exact_copyable_pdf_text(reader, _COPYABLE_VALUES)
    # The fixture classifies this Windows path as a URL; C8 omits invalid URLs
    # instead of publishing them as network indicators.
    assert _COPYABLE_WINDOWS_PATH not in extracted.replace("\n", "")
    assert "IOC" in extracted
    assert "Narrow hash column" in extracted
    assert "Registre Windows" in extracted
    assert len(reader.pages) >= 1


@pytest.mark.asyncio
async def test_real_typst_base_and_augmented_pdf_include_each_canonical_marker_once(
    tmp_path: Path,
    typst_binary: str,
    font_bundle_root: Path,
) -> None:
    """Check M19 article references and every opinion text field in compiled PDFs.

    The article reference row prints its date and source note, with the URL in
    a footnote. The opinion reference row prints its date, ordinal identifier,
    reference text, and URL. The article frise prints its date and event text; the opinion
    chronology retains its source ordinals. Opinion heading,
    qualification, text, and short finding/evidence citations are checked separately.

    The test checks selectable text and scans the compiled opinion PDF's raw
    bytes, decoded streams, metadata, named destinations, and annotations for
    internal UUIDs and AutoWork URI targets. It also does not claim text coverage
    for any figure content rasterized as an image. The existing references and
    opinion traceability sections are retained; no editorial appendix is added.
    """

    pdftotext = shutil.which("pdftotext")
    if pdftotext is None:
        pytest.skip("pdftotext is required for the compiled PDF completeness oracle")

    title_marker = "M04_BASE_TITLE_7F32A9"
    lead_marker = "M04_BASE_LEAD_1C8E45"
    timeline_marker = "M04_BASE_TIMELINE_TEXT_5D7C32"
    timeline_date_marker = "15 septembre 2026"
    second_timeline_marker = "M04_BASE_TIMELINE_TEXT_4A920D"
    second_timeline_date_marker = "17 septembre 2026"
    opinion_marker = "M04_APPROVED_OPINION_93F4C8"
    base = _base_v5()

    # Give each article reference field a unique marker so the visible
    # publisher/title/date/URL row can be checked field by field.
    source_fields: dict[Any, dict[str, Any]] = {}
    reference_texts: dict[Any, str] = {}
    reference_expected_counts: dict[str, int] = {}
    reference_display_dates: dict[Any, str] = {}
    reference_urls: dict[Any, str] = {}
    for index, reference in enumerate(base.references, start=1):
        source = next(
            item
            for item in base.document.sources
            if item.source_document_id == reference.source_document_id
        )
        source_title_marker = f"M04_SOURCE_TITLE_{index:02d}_6B0D21"
        publisher_marker = f"M04_SOURCE_PUBLISHER_{index:02d}_71AC0E"
        source_url = f"https://example.test/M04_SOURCE_URL_{index:02d}_91D30F"
        published_at = date(2026, 10, 20 + index)
        reference_text = f"{publisher_marker} publie « {source_title_marker} »"
        source_fields[source.source_document_id] = {
            "title": source_title_marker,
            "publisher": publisher_marker,
            "canonical_url": source_url,
            "published_at": published_at,
        }
        reference_texts[reference.source_document_id] = reference_text
        reference_display_dates[reference.source_document_id] = (
            f"{published_at.day} octobre {published_at.year}"
        )
        reference_urls[reference.source_document_id] = source_url
        reference_expected_counts.update(
            {
                source_title_marker: 1,
                publisher_marker: 1,
                reference_text: 1,
                f"{published_at.day} octobre {published_at.year}": 1,
                source_url: 1,
            }
        )
    assert source_fields, "the real-PDF fixture must contain a canonical source reference"

    sources = tuple(
        replace(source, **source_fields[source.source_document_id])
        if source.source_document_id in source_fields
        else source
        for source in base.document.sources
    )
    document = replace(
        base.document,
        title=title_marker,
        sources=sources,
        sections=(
            PublicationSectionV1(
                kind=PublicationSectionKind.TECHNICAL,
                heading="Contexte technique",
                paragraphs=(
                    PublicationParagraphV1(
                        "M04_BASE_ARTICLE_SECTION_MARKER",
                        base.document.lead[0].evidence_refs,
                    ),
                ),
            ),
        ),
        lead=(replace(base.document.lead[0], text=lead_marker), *base.document.lead[1:]),
        timeline=(
            PublicationTimelineEntryV1(
                event_date=date(2026, 9, 15),
                date_text=None,
                text=timeline_marker,
                evidence_refs=base.document.lead[0].evidence_refs,
            ),
            PublicationTimelineEntryV1(
                event_date=date(2026, 9, 17),
                date_text=None,
                text=second_timeline_marker,
                evidence_refs=base.document.lead[0].evidence_refs,
            ),
        ),
    )
    references = tuple(
        replace(reference, text=reference_texts[reference.source_document_id])
        for reference in base.references
    )
    anchors = publication_document_text_anchors(document, references=references)
    assert set(anchors) == {
        "title",
        "lead:0001",
        section_paragraph_anchor(0, 1),
        "timeline:0001",
        "timeline:0002",
        *(f"reference:{index:04d}" for index in range(1, len(references) + 1)),
    }
    semantic_text = SemanticTextV1(
        schema_version=SEMANTIC_ANNOTATION_SCHEMA_VERSION,
        policy_version=SEMANTIC_ANNOTATION_POLICY_VERSION,
        paragraphs=tuple(
            SemanticParagraphV1(
                anchor,
                (SemanticTextSpanV1(SemanticRole.TEXT, text),),
            )
            for anchor, text in anchors.items()
        ),
    )
    base = replace(base, document=document, references=references, semantic_text=semantic_text)
    opinion, context = _approved_opinion(base)
    citation_count = 12
    observations = tuple(
        replace(context.observations[0], id=UUID(int=2000 + index))
        for index in range(citation_count)
    )
    findings = tuple(
        replace(
            context.findings[0],
            id=UUID(int=3000 + index),
            observation_refs=(observations[index].id,),
        )
        for index in range(citation_count)
    )
    opinion = replace(
        opinion,
        blocks=(
            replace(
                opinion.blocks[0],
                text=opinion_marker,
                finding_refs=(*(finding.id for finding in findings), findings[0].id),
                evidence_refs=(
                    *(observation.id for observation in observations),
                    observations[0].id,
                ),
            ),
        ),
        content_sha256="",
    )
    context = replace(context, findings=findings, observations=observations)
    augmented = augment_publication_with_opinion(base, opinion, context)
    template_bundle = load_template_bundle(_REPOSITORY_ROOT / "chpTypst")
    font_snapshot = load_font_bundle_snapshot(font_bundle_root, _FONT_BUNDLE_LOCK)

    async def compile_and_extract(
        document,
        name: str,
        *,
        omit_note_numbers: bool = False,
    ) -> tuple[str, bytes]:
        source = TypstRenderer().render(
            document,
            template_bundle,
            document_label=f"Publication de test — {name}",
        )
        workspace = tmp_path / f"{name}-workspace"
        render_data = json.loads(source.render_data_bytes)

        def without_note_numbers(value: Any) -> Any:
            if isinstance(value, dict):
                return {
                    key: without_note_numbers(item)
                    for key, item in value.items()
                    if key != "note_number"
                }
            if isinstance(value, list):
                return [without_note_numbers(item) for item in value]
            return value

        if omit_note_numbers:
            render_data = without_note_numbers(render_data)
        _build_runtime_workspace(
            _REPOSITORY_ROOT,
            workspace,
            render_data=render_data,
        )
        pdf_path = tmp_path / f"{name}.pdf"
        argv = [typst_binary, "compile", "--root", str(workspace)]
        for font_path in materialize_font_bundle(font_snapshot, tmp_path / f"{name}-fonts"):
            argv.extend(("--font-path", str(font_path)))
        argv.extend(
            (
                "--ignore-system-fonts",
                "--creation-timestamp",
                "0",
                str(workspace / "RENDERER" / "publication.typ"),
                str(pdf_path),
            )
        )
        compile_result = _run_blocking(argv, capture_output=True, check=False, timeout=20)
        assert compile_result.returncode == 0, compile_result.stderr.decode(
            "utf-8", errors="replace"
        )
        extracted = _run_blocking(
            (pdftotext, "-layout", str(pdf_path), "-"),
            capture_output=True,
            check=True,
            timeout=30,
        )
        return extracted.stdout.decode("utf-8", errors="replace"), pdf_path.read_bytes()

    base_text, _ = await compile_and_extract(base, "mission04-base")
    augmented_text, augmented_pdf = await compile_and_extract(augmented, "mission04-augmented")
    augmented_without_note_numbers_text, _ = await compile_and_extract(
        augmented,
        "mission04-augmented-without-note-numbers",
        omit_note_numbers=True,
    )
    internal_reference_ids = (
        tuple(finding.id for finding in findings)
        + tuple(observation.id for observation in observations)
        + tuple(source.source_document_id for source in sources)
    )
    _assert_pdf_has_no_internal_identity_data(
        augmented_pdf,
        internal_ids=tuple(
            value
            for reference_id in internal_reference_ids
            for value in (str(reference_id), reference_id.hex)
        ),
    )

    def normalize_pdf_text(value: str) -> str:
        return value.replace("\u2018", "'").replace("\u2019", "'")

    base_text = normalize_pdf_text(base_text)
    augmented_text = normalize_pdf_text(augmented_text)

    # The base fixture is routed as a BRIEF, so its legacy timeline cites source
    # ordinals and URLs without an article reference list. The augmented V6 opinion
    # keeps its numbered source list and existing traceability annex. ``pdftotext``
    # sees printed URLs but cannot verify the link destination bytes.
    finding_id = str(context.findings[0].id)
    evidence_id = str(context.observations[0].id)
    timeline_expectations = (
        (timeline_date_marker, timeline_marker, document.timeline[0]),
        (second_timeline_date_marker, second_timeline_marker, document.timeline[1]),
    )
    reference_numbers = {
        reference.source_document_id: index for index, reference in enumerate(references, start=1)
    }
    for timeline_date, timeline_text, entry in timeline_expectations:
        assert timeline_date in base_text
        assert entry.event_date is not None
        assert entry.text == timeline_text
        assert any(ref.source_document_id in reference_numbers for ref in entry.evidence_refs)

    def assert_reference_rows(text: str, label: str, *, article: bool) -> None:
        reference_text = re.split(r"(?i)chronologie", text, maxsplit=1)[0]
        for reference in references:
            number = reference_numbers[reference.source_document_id]
            display_date = reference_display_dates[reference.source_document_id]
            source_url = reference_urls[reference.source_document_id]
            if article:
                expected_row = f"{display_date}:{reference_texts[reference.source_document_id]}"
                compact_text = "".join(text.split())
                expected_row = "".join(expected_row.split())
                assert compact_text.count(expected_row) == 1, (
                    f"{label} PDF reference {number} date/source-note row: "
                    f"expected 1, found {compact_text.count(expected_row)}"
                )
                continue
            date_row = re.compile(rf"(?m)^\s*{number}\.\s*{re.escape(display_date)}\s*$")
            url_row = re.compile(rf"(?m)^\s*{number}\s+{re.escape(source_url)}\s*$")
            assert len(date_row.findall(reference_text)) == 1, (
                f"{label} PDF reference {number} date/identifier row: expected 1, "
                f"found {len(date_row.findall(reference_text))}"
            )
            assert len(url_row.findall(reference_text)) == 1, (
                f"{label} PDF reference {number} URL/identifier row: expected 1, "
                f"found {len(url_row.findall(reference_text))}"
            )

    def assert_timeline_citations(text: str, label: str) -> None:
        for _, marker, entry in timeline_expectations:
            expected_numbers = tuple(
                dict.fromkeys(
                    reference_numbers[ref.source_document_id]
                    for ref in entry.evidence_refs
                    if ref.source_document_id in reference_numbers
                )
            )
            assert expected_numbers
            for number in expected_numbers:
                citation = re.compile(rf"{re.escape(marker)}\s*{number}(?!\d)")
                assert len(citation.findall(text)) == 1, (
                    f"{label} PDF timeline marker {marker!r} citation {number}: "
                    f"expected 1, found {len(citation.findall(text))}"
                )

    base_reference_expected_counts = dict.fromkeys(reference_expected_counts, 0)
    for reference in references:
        source_id = reference.source_document_id
        source_url = reference_urls[source_id]
        base_reference_expected_counts[source_url] = 1
        source = next(item for item in sources if item.source_document_id == source_id)
        base_reference_expected_counts[source.title] = 1
        base_reference_expected_counts[source.publisher] = 1
        base_reference_expected_counts[reference_display_dates[source_id]] = 2
        # PDF line wrapping can split a source note; the compact row oracle below
        # proves the date and complete note occur together exactly once.
        base_reference_expected_counts[reference_texts[source_id]] = 0
    base_expected_counts = base_reference_expected_counts | {
        title_marker: 1,
        lead_marker: 1,
        "M04_BASE_ARTICLE_SECTION_MARKER": 1,
        timeline_marker: 1,
        timeline_date_marker: 1,
        second_timeline_marker: 1,
        second_timeline_date_marker: 1,
        "Note de l'analyste": 0,
        "Apports de l'analyse": 0,
        "Apports de l'enquête": 0,
        "Fait étayé": 0,
        "Confiance : 90%": 0,
        opinion_marker: 0,
        "[F1]": 0,
        "[E1]": 0,
        finding_id: 0,
        evidence_id: 0,
        "ANNEXE DE TRAÇABILITÉ": 0,
    }
    augmented_expected_counts = reference_expected_counts | {
        title_marker: 1,
        lead_marker: 1,
        timeline_marker: 1,
        timeline_date_marker: 1,
        second_timeline_marker: 1,
        second_timeline_date_marker: 1,
        "Note de l'analyste": 1,
        "Apports de l'analyse": 1,
        "Apports de l'enquête": 0,
        "Fait étayé": 1,
        "Confiance : 90%": 0,
        opinion_marker: 1,
        # Each short label appears once in the body and once in the traceability annex.
        **{f"[F{index}]": 2 for index in range(1, citation_count + 1)},
        **{f"[E{index}]": 2 for index in range(1, citation_count + 1)},
        finding_id: 0,
        evidence_id: 0,
        "Constat technique": citation_count,
        "Observation technique": citation_count,
        "Constats logiques cités": 1,
        "Observations": 1,
        "ANNEXE DE TRAÇABILITÉ": 1,
    }
    for marker, expected_count in base_expected_counts.items():
        assert base_text.count(marker) == expected_count, (
            f"base PDF marker {marker!r}: expected {expected_count}, "
            f"found {base_text.count(marker)}"
        )
    for marker, expected_count in augmented_expected_counts.items():
        assert augmented_text.count(marker) == expected_count, (
            f"augmented PDF marker {marker!r}: expected {expected_count}, "
            f"found {augmented_text.count(marker)}"
        )
    expected_citations = (
        "Références:"
        + "·".join(f"[F{index}]" for index in range(1, citation_count + 1))
        + "·"
        + "·".join(f"[E{index}]" for index in range(1, citation_count + 1))
    )

    def assert_body_citations(text: str, label: str) -> None:
        body_text = text.split("ANNEXE DE TRAÇABILITÉ", maxsplit=1)[0]
        compact_body_text = re.sub(r"\s+", "", body_text)
        assert re.search(
            re.escape(opinion_marker) + re.escape(expected_citations),
            compact_body_text,
        ), f"{label} PDF opinion body does not render the projected short reference labels"
        citation_lines = [
            line
            for line in body_text.splitlines()
            if "Références" in line or re.search(r"\[[FE]\d+\]", line)
        ]
        assert len(citation_lines) > 1, (
            f"{label} PDF long opinion-reference line did not wrap: {citation_lines!r}"
        )

    assert_body_citations(augmented_text, "projected")
    assert_body_citations(augmented_without_note_numbers_text, "synthetic no-note-number")
    for reference_id in (
        *(str(finding.id) for finding in findings),
        *(str(observation.id) for observation in observations),
    ):
        assert reference_id not in augmented_text
    assert re.search(r"CONFIANCE\s*:\s*\d+\s*%", base_text, flags=re.IGNORECASE) is None
    assert re.search(r"CONFIANCE\s*:\s*\d+\s*%", augmented_text, flags=re.IGNORECASE) is None
    assert base_text.count("Références") == 1
    assert_reference_rows(base_text, "base", article=True)
    assert_reference_rows(augmented_text, "augmented", article=False)
    assert_timeline_citations(augmented_text, "augmented")
    assert "reviewer-1" not in augmented_text
    assert "approver-1" not in augmented_text


@pytest.mark.asyncio
async def test_real_typst_wraps_long_tokens_without_overlapping_narrow_table_cells(
    tmp_path: Path,
    typst_binary: str,
    font_bundle_root: Path,
) -> None:
    typst_root = _REPOSITORY_ROOT / "chpTypst"
    bundle = load_template_bundle(typst_root)
    paragraph_text = (
        "Technical literal: Blackwood.Cheese.xml and "
        r"HKCU\SOFTWARE\Microsoft\Windows\CurrentVersion\Run"
    )
    rows = (
        (
            "PsProxy.dll",
            (
                "Moteur sans état ne disposant pas de capacités autonomes de persistance "
                "ou de communication réseau."
            ),
            "Utilisation de System.Management.Automation.dll avec un runspace personnalisé.",
            "Clé facultative et Blackwood.Cheese.xml, partagée via PowerShell.exe.",
        ),
        (
            "Blackwood.dll",
            "Chargeur de modules et de composants additionnels.",
            r"Création de HKCU\SOFTWARE\Microsoft\Windows\CurrentVersion\Run.",
            "Option facultative fournie par l'opérateur.",
        ),
        (
            "Sample.exe",
            "Composant de démonstration avec une fonction courte.",
            "Consultation d'une ressource hébergée sur Internet.",
            _COPYABLE_LONG_URL,
        ),
        (
            "Hash.dll",
            "Valeur d'empreinte conservée sans modification.",
            "Calcul de la somme de contrôle SHA-256.",
            _COPYABLE_HASH,
        ),
    )
    base = _copyable_ioc_document()
    table = base.tables[0]
    component_table = replace(
        table,
        columns=(
            PublicationTableColumnV1("component", "Composant"),
            PublicationTableColumnV1("function", "Fonction"),
            PublicationTableColumnV1("mechanism", "Mécanisme technique"),
            PublicationTableColumnV1("dependency", "Dépendance ou particularité"),
        ),
        rows=tuple(PublicationTableRowV1(row, table.rows[0].evidence_refs) for row in rows),
    )
    first_lead = replace(base.lead[0], text=paragraph_text)
    base = replace(base, lead=(first_lead, *base.lead[1:]), tables=(component_table,))

    technical_literals = (
        "PsProxy.dll",
        "Blackwood.dll",
        "System.Management.Automation.dll",
        r"HKCU\SOFTWARE\Microsoft\Windows\CurrentVersion\Run",
        "Blackwood.Cheese.xml",
        _COPYABLE_LONG_URL,
        _COPYABLE_HASH,
    )
    semantic_paragraphs = []
    for anchor, text in publication_document_text_anchors(base).items():
        matches = sorted(
            (position, literal)
            for literal in technical_literals
            if (position := text.find(literal)) >= 0
        )
        spans: list[SemanticTextSpanV1] = []
        cursor = 0
        for position, literal in matches:
            if position < cursor:
                continue
            if position > cursor:
                spans.append(SemanticTextSpanV1(SemanticRole.TEXT, text[cursor:position]))
            spans.append(SemanticTextSpanV1(SemanticRole.TECHNICAL_LITERAL, literal))
            cursor = position + len(literal)
        if cursor < len(text):
            spans.append(SemanticTextSpanV1(SemanticRole.TEXT, text[cursor:]))
        if not spans:
            spans.append(SemanticTextSpanV1(SemanticRole.TEXT, text))
        semantic_paragraphs.append(SemanticParagraphV1(anchor, tuple(spans)))
    document = PublicationDocumentV5(
        document=base,
        semantic_text=SemanticTextV1(
            schema_version=SEMANTIC_ANNOTATION_SCHEMA_VERSION,
            policy_version=SEMANTIC_ANNOTATION_POLICY_VERSION,
            paragraphs=tuple(semantic_paragraphs),
        ),
    )
    source = TypstRenderer().render(document, bundle, document_label=_PUBLICATION_RENDER_LABEL)
    render_data = json.loads(source.render_data_bytes)
    table = next(
        block
        for section in render_data["content_sections"]
        for block in section["blocks"]
        if block["type"] == "table"
    )
    assert 1.0 <= table["column_weights"][0] <= 1.2

    workspace_root = tmp_path / "long-token-article-workspace"
    _build_runtime_workspace(_REPOSITORY_ROOT, workspace_root, render_data=render_data)
    font_snapshot = load_font_bundle_snapshot(font_bundle_root, _FONT_BUNDLE_LOCK)
    font_root = tmp_path / "long-token-article-fonts"
    font_root.mkdir()
    compiled = await TypstSubprocessCompiler(binary=typst_binary).compile(
        TypstCompileRequest(
            workspace_root=workspace_root,
            entrypoint_relative_path="RENDERER/publication.typ",
            font_paths=materialize_font_bundle(font_snapshot, font_root),
        )
    )
    reader = PdfReader(BytesIO(compiled.content), strict=True)
    extracted = "\n".join(page.extract_text() or "" for page in reader.pages)
    normalized_extracted = "".join(
        character
        for character in extracted
        if not character.isspace() and unicodedata.category(character) != "Cf"
    )
    for exact_value in (
        "PsProxy.dll",
        "Blackwood.dll",
        "System.Management.Automation.dll",
        r"HKCU\SOFTWARE\Microsoft\Windows\CurrentVersion\Run",
        "Blackwood.Cheese.xml",
        _COPYABLE_LONG_URL,
        _COPYABLE_HASH,
    ):
        assert exact_value in normalized_extracted
    assert "Composant" in extracted

    pdftotext = shutil.which("pdftotext")
    assert pdftotext is not None, "pdftotext is required to check rendered word coordinates"
    pdf_path = tmp_path / "long-token-article.pdf"
    pdf_path.write_bytes(compiled.content)
    with anyio.fail_after(10):
        bbox = await anyio.run_process(
            (pdftotext, "-bbox", str(pdf_path), "-"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )
    root = ET.fromstring(bbox.stdout.decode("utf-8", errors="replace"))
    pages = [element for element in root.iter() if element.tag.rsplit("}", 1)[-1] == "page"]
    words = [
        (
            page_index,
            element.text or "",
            float(element.attrib["xMin"]),
            float(element.attrib["yMin"]),
            float(element.attrib["xMax"]),
            float(element.attrib["yMax"]),
        )
        for page_index, page in enumerate(pages)
        for element in page.iter()
        if element.tag.rsplit("}", 1)[-1] == "word"
    ]

    def assert_card_field_does_not_overlap(label: str, value: str) -> None:
        labels = [word for word in words if word[1] == label]
        values = [word for word in words if word[1] == value]
        assert labels and values, f"rendered component card is missing {label!r} or {value!r}"
        assert any(
            header[0] == candidate[0]
            and min(header[5], candidate[5]) > max(header[3], candidate[3])
            and candidate[2] >= header[4]
            for header in labels
            for candidate in values
        ), f"component-card label {label!r} overlaps its value {value!r}"

    for label, value in (
        ("Fonction:", "Moteur"),
        ("Mécanisme", "Utilisation"),
        ("Dépendance", "Clé"),
        ("Fonction:", "Chargeur"),
        ("Mécanisme", "Création"),
        ("Dépendance", "Option"),
        ("Fonction:", "Composant"),
    ):
        assert_card_field_does_not_overlap(label, value)


@pytest.mark.asyncio
async def test_real_typst_article_ioc_hashes_wrap_and_long_references_stay_left_aligned(
    tmp_path: Path,
    typst_binary: str,
    font_bundle_root: Path,
) -> None:
    sha512 = "a" * 128
    ssdeep = f"3:{'A' * 64}:{'B' * 64}"
    reference_digest = "7bdd7d62" + "c" * 48 + "628f27"
    rows = [
        {
            "value": _breakable_typst_display_text(
                value,
                interval=16,
                break_at_technical_separators=True,
            )
        }
        for value in (sha512, ssdeep)
    ]
    render_data = {
        "schema_version": "typst-publication-model-article-ioc-long-hash-fixture",
        "document_label": _PUBLICATION_RENDER_LABEL,
        "language": "fr",
        "title": "Long hash rendering fixture",
        "content_sections": [
            {
                "type": "references",
                "entries": [
                    {
                        "number": 1,
                        "display_date": "2026-10-30",
                        "text": f"Hatching Triage — Rapport de l'échantillon {reference_digest}",
                        "source_urls": [],
                        "target_id": "long-hash-reference",
                    }
                ],
                "blocks": [],
            },
            {
                "type": "ioc",
                "source_indicators": [
                    {
                        "heading": "Fichiers",
                        "items": rows,
                    }
                ],
            },
        ],
    }

    workspace_root = tmp_path / "article-ioc-long-hash-workspace"
    _build_runtime_workspace(_REPOSITORY_ROOT, workspace_root, render_data=render_data)
    font_snapshot = load_font_bundle_snapshot(font_bundle_root, _FONT_BUNDLE_LOCK)
    font_root = tmp_path / "long-hash-annex-fonts"
    font_root.mkdir()
    compiled = await TypstSubprocessCompiler(binary=typst_binary).compile(
        TypstCompileRequest(
            workspace_root=workspace_root,
            entrypoint_relative_path="RENDERER/publication.typ",
            font_paths=materialize_font_bundle(font_snapshot, font_root),
        )
    )
    reader = PdfReader(BytesIO(compiled.content), strict=True)
    extracted = _assert_exact_copyable_pdf_text(
        reader,
        (sha512, ssdeep, reference_digest),
    )
    assert "Hatching Triage" in extracted

    pdftotext = shutil.which("pdftotext")
    assert pdftotext is not None, "pdftotext is required to check rendered word coordinates"
    pdf_path = tmp_path / "article-ioc-long-hash.pdf"
    pdf_path.write_bytes(compiled.content)
    bbox = await anyio.run_process(
        (pdftotext, "-bbox", str(pdf_path), "-"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    root = ET.fromstring(bbox.stdout.decode("utf-8", errors="replace"))
    pages = [element for element in root.iter() if element.tag.rsplit("}", 1)[-1] == "page"]
    page_words = [
        (
            page_index,
            element.text or "",
            float(element.attrib["xMin"]),
            float(element.attrib["yMin"]),
            float(element.attrib["xMax"]),
            float(element.attrib["yMax"]),
        )
        for page_index, page in enumerate(pages)
        for element in page.iter()
        if element.tag.rsplit("}", 1)[-1] == "word"
    ]

    file_heading = next(word for word in page_words if word[1] == "Fichiers")
    ioc_page = file_heading[0]
    token_fragments = [
        word
        for word in page_words
        if word[0] == ioc_page
        and len(word[1]) >= 4
        and any(word[1] in value for value in (sha512, ssdeep))
    ]
    assert token_fragments, "the annex page is missing the rendered SHA-512/ssdeep values"
    token_lines = {round(fragment[3], 1) for fragment in token_fragments}
    assert len(token_lines) > 2, "long IOC hashes should wrap across multiple lines"
    reference_anchor = next(
        (word for word in page_words if word[1] == "Hatching"),
        None,
    )
    assert reference_anchor is not None, "the long reference title is missing from pdftotext -bbox"
    reference_line = [
        word
        for word in page_words
        if word[0] == reference_anchor[0] and abs(word[3] - reference_anchor[3]) <= 0.5
    ]
    reference_gaps = [right[2] - left[4] for left, right in pairwise(reference_line)]
    assert file_heading is not None
    assert max(reference_gaps, default=0) <= 12, f"long reference line gaps={reference_gaps!r}"


@pytest.mark.asyncio
async def test_real_typst_hides_candidate_indicators_and_annex_from_legacy_render_data(
    tmp_path: Path,
    typst_binary: str,
    font_bundle_root: Path,
) -> None:
    candidates = [{"value": f"candidate-{index:03d}.example"} for index in range(80)]
    row = {
        "type": "hash",
        "value": "a" * 64,
        "status_label": "documenté",
        "indicator_status_short_label": "Article",
        "relationship_status_short_label": "documenté",
    }
    render_data = {
        "schema_version": "typst-publication-model-orphan-note-fixture",
        "document_label": _PUBLICATION_RENDER_LABEL,
        "language": "fr",
        "title": "Invalid URL note pagination fixture",
        "content_sections": [
            {
                "type": "ioc",
                "source_indicators": [],
                "original_indicators": [],
                "candidate_indicators": [{"heading": "Valeurs à vérifier", "items": candidates}],
                "omitted_invalid_url_count": 1,
            },
            {
                "type": "technical_annex",
                "ioc_rows": [row],
                "ioc_row_groups": [
                    {"heading": "Fichiers — empreintes cryptographiques", "rows": [row]}
                ],
            },
        ],
    }
    workspace_root = tmp_path / "invalid-url-note-workspace"
    _build_runtime_workspace(_REPOSITORY_ROOT, workspace_root, render_data=render_data)
    font_snapshot = load_font_bundle_snapshot(font_bundle_root, _FONT_BUNDLE_LOCK)
    font_root = tmp_path / "invalid-url-note-fonts"
    font_root.mkdir()
    compiled = await TypstSubprocessCompiler(binary=typst_binary).compile(
        TypstCompileRequest(
            workspace_root=workspace_root,
            entrypoint_relative_path="RENDERER/publication.typ",
            font_paths=materialize_font_bundle(font_snapshot, font_root),
        )
    )
    reader = PdfReader(BytesIO(compiled.content), strict=True)
    text = "\n".join(page.extract_text() or "" for page in reader.pages)
    normalized_text = text.casefold()
    for hidden in (
        "Pistes non confirmées",
        "Valeurs à vérifier",
        "1 URL non valide(s) omise(s) des listes.",
        "ANNEXE TECHNIQUE",
        "candidate-000.example",
        "a" * 64,
    ):
        assert hidden.casefold() not in normalized_text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("omitted_invalid_url_count", "expected_note"),
    (
        (2, "2 URL non valide(s) omise(s) des listes."),
        (0, None),
    ),
)
async def test_real_typst_opinion_renders_candidate_url_omission_note(
    tmp_path: Path,
    typst_binary: str,
    font_bundle_root: Path,
    omitted_invalid_url_count: int,
    expected_note: str | None,
) -> None:
    render_data = {
        "schema_version": "typst-publication-model-opinion-ioc-note-fixture",
        "document_label": _PUBLICATION_RENDER_LABEL,
        "language": "fr",
        "title": "Opinion IOC note fixture",
        "opinion_document": True,
        "content_sections": [
            {
                "type": "ioc",
                "source_indicators": [],
                "original_indicators": [],
                "candidate_indicators": [
                    {
                        "heading": "Valeurs à vérifier",
                        "items": [{"value": "candidate-001.example"}],
                    }
                ],
                "omitted_invalid_url_count": omitted_invalid_url_count,
            }
        ],
    }
    workspace_root = tmp_path / f"opinion-ioc-note-{omitted_invalid_url_count}-workspace"
    _build_runtime_workspace(_REPOSITORY_ROOT, workspace_root, render_data=render_data)
    font_snapshot = load_font_bundle_snapshot(font_bundle_root, _FONT_BUNDLE_LOCK)
    font_root = tmp_path / f"opinion-ioc-note-{omitted_invalid_url_count}-fonts"
    font_root.mkdir()
    compiled = await TypstSubprocessCompiler(binary=typst_binary).compile(
        TypstCompileRequest(
            workspace_root=workspace_root,
            entrypoint_relative_path="RENDERER/publication.typ",
            font_paths=materialize_font_bundle(font_snapshot, font_root),
        )
    )

    reader = PdfReader(BytesIO(compiled.content), strict=True)
    extracted = " ".join("\n".join(page.extract_text() or "" for page in reader.pages).split())
    assert "Pistes non confirmées" in extracted
    if expected_note is None:
        assert "URL non valide(s) omise(s) des listes." not in extracted
    else:
        assert expected_note in extracted


@pytest.mark.asyncio
async def test_real_typst_article_renders_cited_technical_context_reference(
    tmp_path: Path,
    typst_binary: str,
    font_bundle_root: Path,
) -> None:
    publication = _cited_technical_context_case()[6]
    typst_root = _REPOSITORY_ROOT / "chpTypst"
    bundle = load_template_bundle(typst_root)
    source = TypstRenderer().render(publication, bundle, document_label=_PUBLICATION_RENDER_LABEL)
    render_data = json.loads(source.render_data_bytes)
    workspace_root = tmp_path / "technical-context-reference-workspace"
    _build_runtime_workspace(_REPOSITORY_ROOT, workspace_root, render_data=render_data)
    font_snapshot = load_font_bundle_snapshot(font_bundle_root, _FONT_BUNDLE_LOCK)
    font_root = tmp_path / "technical-context-reference-fonts"
    font_root.mkdir()
    compiled = await TypstSubprocessCompiler(binary=typst_binary).compile(
        TypstCompileRequest(
            workspace_root=workspace_root,
            entrypoint_relative_path="RENDERER/publication.typ",
            font_paths=materialize_font_bundle(font_snapshot, font_root),
        )
    )

    reader = PdfReader(BytesIO(compiled.content), strict=True)
    extracted = _assert_exact_copyable_pdf_text(
        reader,
        (
            "Fortra: NanoDump tool reference — ressource technique de contexte.",
            "NanoDump tool reference",
        ),
    )
    compact_text = "".join(extracted.split())
    assert "Contexte·Fortra" not in compact_text
    assert "Datedepublicationnonprécisée" not in compact_text
    assert compact_text.count("NanoDumptoolreference") == 1
    assert "https://github.com/fortra/nanodump" in compact_text
    assert compact_text.count("https://github.com/fortra/nanodump") == 1
    assert len(reader.pages) >= 1


def _svg_fixture(width: int, height: int, label: str, fill: str) -> str:
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}">'
        f'<rect x="1" y="1" width="{width - 2}" height="{height - 2}" '
        f'rx="18" fill="{fill}" stroke="#35445a" stroke-width="3"/>'
        f'<text x="24" y="{height // 2}" font-family="sans-serif" font-size="32" '
        f'fill="#172033">{label}</text></svg>'
    )


def _timeline_chart_fixture() -> str:
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" width="1000" height="430" '
        'viewBox="0 0 1000 430">'
        '<rect width="1000" height="430" fill="#e3f2e8"/>'
        '<line x1="80" y1="350" x2="950" y2="350" stroke="#40505b" stroke-width="3"/>'
        '<line x1="80" y1="65" x2="80" y2="350" stroke="#40505b" stroke-width="3"/>'
        '<line x1="80" y1="275" x2="950" y2="275" stroke="#b5c6bb" stroke-width="2"/>'
        '<line x1="80" y1="195" x2="950" y2="195" stroke="#b5c6bb" stroke-width="2"/>'
        '<line x1="80" y1="115" x2="950" y2="115" stroke="#b5c6bb" stroke-width="2"/>'
        '<polyline points="130,290 370,240 610,165 860,95" fill="none" '
        'stroke="#2d7550" stroke-width="8"/>'
        '<circle cx="130" cy="290" r="12" fill="#2d7550"/>'
        '<circle cx="370" cy="240" r="12" fill="#2d7550"/>'
        '<circle cx="610" cy="165" r="12" fill="#2d7550"/>'
        '<circle cx="860" cy="95" r="12" fill="#2d7550"/>'
        '<text x="95" y="400" font-family="sans-serif" font-size="24">Jan.</text>'
        '<text x="335" y="400" font-family="sans-serif" font-size="24">Mar.</text>'
        '<text x="575" y="400" font-family="sans-serif" font-size="24">Mai</text>'
        '<text x="825" y="400" font-family="sans-serif" font-size="24">Juil.</text>'
        '<text x="105" y="55" font-family="sans-serif" font-size="24">Sources publiées</text>'
        "</svg>"
    )


def _flow_diagram_fixture() -> str:
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" width="1000" height="540" '
        'viewBox="0 0 1000 540">'
        '<rect width="1000" height="540" fill="#e9e4f5"/>'
        '<rect x="35" y="195" width="250" height="135" rx="18" fill="#fff" '
        'stroke="#59627a" stroke-width="4"/>'
        '<rect x="375" y="195" width="250" height="135" rx="18" fill="#fff" '
        'stroke="#59627a" stroke-width="4"/>'
        '<rect x="715" y="195" width="250" height="135" rx="18" fill="#fff" '
        'stroke="#59627a" stroke-width="4"/>'
        '<line x1="285" y1="262" x2="350" y2="262" stroke="#39465d" stroke-width="6"/>'
        '<polygon points="350,250 375,262 350,274" fill="#39465d"/>'
        '<line x1="625" y1="262" x2="690" y2="262" stroke="#39465d" stroke-width="6"/>'
        '<polygon points="690,250 715,262 690,274" fill="#39465d"/>'
        '<text x="70" y="250" font-family="sans-serif" font-size="26">Accès initial</text>'
        '<text x="82" y="292" font-family="sans-serif" font-size="22">Compte compromis</text>'
        '<text x="433" y="250" font-family="sans-serif" font-size="26">Exécution</text>'
        '<text x="413" y="292" font-family="sans-serif" font-size="22">Script malveillant</text>'
        '<text x="792" y="250" font-family="sans-serif" font-size="26">C2</text>'
        '<text x="755" y="292" font-family="sans-serif" font-size="22">Infrastructure</text>'
        "</svg>"
    )


def _relationship_diagram_fixture() -> str:
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" width="900" height="600" '
        'viewBox="0 0 900 600">'
        '<rect width="900" height="600" fill="#f5e4e4"/>'
        '<line x1="225" y1="285" x2="420" y2="190" stroke="#664a5b" stroke-width="5"/>'
        '<polygon points="410,178 438,182 422,204" fill="#664a5b"/>'
        '<line x1="480" y1="210" x2="680" y2="300" stroke="#664a5b" stroke-width="5"/>'
        '<polygon points="670,285 697,308 667,313" fill="#664a5b"/>'
        '<line x1="225" y1="325" x2="680" y2="325" stroke="#8b687c" stroke-width="4" '
        'stroke-dasharray="12 9"/>'
        '<ellipse cx="155" cy="305" rx="110" ry="72" fill="#fff" '
        'stroke="#664a5b" stroke-width="4"/>'
        '<rect x="390" y="120" width="180" height="140" rx="18" fill="#fff" '
        'stroke="#664a5b" stroke-width="4"/>'
        '<ellipse cx="745" cy="325" rx="120" ry="72" fill="#fff" '
        'stroke="#664a5b" stroke-width="4"/>'
        '<text x="93" y="315" font-family="sans-serif" font-size="27">APT31</text>'
        '<text x="420" y="185" font-family="sans-serif" font-size="25">Proxy</text>'
        '<text x="690" y="315" font-family="sans-serif" font-size="22">Victime</text>'
        '<text x="280" y="218" font-family="sans-serif" font-size="18">utilise</text>'
        '<text x="580" y="230" font-family="sans-serif" font-size="18">cible</text>'
        "</svg>"
    )


def _lot6_composition_render_data() -> tuple[dict[str, Any], tuple[str, ...], tuple[str, ...]]:
    ips = [f"198.51.100.{index}" for index in range(1, 21)]
    domains = [f"host-{index:03d}.long-indicator.example.test" for index in range(1, 21)]
    raw_urls = [
        "https://downloads.example.test/releases/2026/10/06/"
        + f"package-{index:03d}/"
        + "segment/" * 10
        + f"artifact-{index:03d}.bin?"
        + "campaign=autumn-update&source=publication-fixture&signature="
        + "a" * 80
        for index in range(1, 21)
    ]
    emails = [f"analyst-{index:03d}@mail.example.test" for index in range(1, 21)]
    hashes = [f"{index:064x}" for index in range(1, 21)]
    values = tuple(value for group in (ips, domains, raw_urls, emails, hashes) for value in group)
    assert len(values) == 100
    rendered_values = {
        key: [_breakable_typst_display_text(value) for value in group]
        for key, group in (
            ("ips", ips),
            ("domains", domains),
            ("urls", raw_urls),
            ("emails", emails),
            ("hashes", hashes),
        )
    }
    original_iocs = (
        "203.0.113.254",
        "unlinked-reserve.example.test",
        "https://unlinked-reserve.example.test/path",
        "reserve-analyst@mail.example.test",
        "f" * 64,
    )
    original_values = {
        key: [_breakable_typst_display_text(value)]
        for key, value in zip(
            ("ips", "domains", "urls", "emails", "hashes"), original_iocs, strict=True
        )
    }
    category_labels = {
        "ips": ("Adresses IP", "IP"),
        "domains": ("Noms de domaine", "Domaine"),
        "urls": ("URL", "URL"),
        "emails": ("Courriels", "Adresse e-mail"),
        "hashes": ("Empreintes de fichier", "Hash"),
    }

    def ioc_row(value: dict[str, Any], *, original: bool = False) -> dict[str, Any]:
        row_marker = hashlib.sha256(value["text"].encode()).hexdigest()[:10]
        return {
            "type": "ioc",
            "type_label": "Indicateur",
            "value": value,
            "indicator_status_label": "Documenté",
            "relationship_status_label": "Lien non démontré" if original else "Documenté",
            "indicator_status_short_label": "documenté",
            "relationship_status_short_label": "non démontré" if original else "documenté",
            "context_summary": (
                "Le lien avec le sujet n'est pas démontré."
                if original
                else f"Source : rapport public — ligne {row_marker}."
            ),
            "hash_context_summary": (
                "Le lien avec le sujet n'est pas démontré."
                if original
                else f"Source : rapport public — ligne {row_marker}."
            ),
            "source_refs": [],
            "draft_refs": [],
        }

    main_groups: list[dict[str, Any]] = []
    main_rows: list[dict[str, Any]] = []
    for category, (heading, type_label) in category_labels.items():
        rows = [
            {
                **ioc_row(value),
                "type": "hash" if category == "hashes" else category,
                "type_label": type_label,
            }
            for value in rendered_values[category]
        ]
        main_rows.extend(rows)
        main_groups.append({"heading": heading, "rows": rows})
    original_rows = [
        {
            **ioc_row(value, original=True),
            "type": "hash" if category == "hashes" else category,
            "type_label": category_labels[category][1],
        }
        for category, values_for_category in original_values.items()
        for value in values_for_category
    ]
    original_group = {
        "heading": "IOC originaux à lien non démontré",
        "rows": original_rows,
    }

    return (
        {
            "schema_version": "typst-publication-model-v17-synthesis-section-headings",
            "document_label": "Publication de base — artefact de fixture v1",
            "language": "fr",
            "title": "[LOT 6] Fixture de composition A4",
            "opinion_document": True,
            "content_sections": [
                {
                    "type": "references",
                    "timeline": [],
                    "blocks": [
                        {
                            "type": "figure",
                            "figure_number": 1,
                            "key": "source-wide",
                            "caption": "Figure source wide, repère A.",
                            "source_note": "example.test — section A — p. 2",
                            "media_path": "media/source-wide.svg",
                        }
                    ],
                    "sources": [],
                },
                {
                    "type": "chronology",
                    "timeline": [
                        {
                            "display_date": "5 octobre 2026",
                            "text": "La source décrit l'activité après sa publication.",
                            "source_urls": [],
                            "source_refs": [],
                        }
                    ],
                },
                {
                    "type": "synthesis",
                    "blocks": [
                        {
                            "type": "paragraph",
                            "text": "Composition complète de référence pour le rendu final.",
                        },
                        {
                            "type": "figure",
                            "figure_number": 2,
                            "key": "source-portrait",
                            "caption": "Figure source portrait, repère B.",
                            "source_note": "example.test — section B — p. 3",
                            "media_path": "media/source-portrait.svg",
                        },
                        {
                            "type": "chart",
                            "figure_number": 3,
                            "key": "timeline-chart",
                            "kind": "timeline",
                            "title": "Titre chart qui ne doit pas apparaître au-dessus",
                            "caption": "Événements datés selon leur source.",
                            "media_path": "media/timeline-chart.svg",
                        },
                        {
                            "type": "diagram",
                            "figure_number": 4,
                            "key": "flow-diagram",
                            "title": "Titre FLOW qui ne doit pas apparaître au-dessus",
                            "caption": "Flux de compromission observé.",
                            "media_path": "media/flow-diagram.svg",
                        },
                        {
                            "type": "table",
                            "key": "runtime-table",
                            "title": "Tableau des artefacts observés",
                            "caption": "Valeurs représentatives de la source.",
                            "columns": ["Type", "Valeur"],
                            "rows": [["Domaine", "host-001.long-indicator.example.test"]],
                        },
                        {
                            "type": "diagram",
                            "figure_number": 5,
                            "key": "relationship-diagram",
                            "title": "Titre RELATIONSHIP qui ne doit pas apparaître au-dessus",
                            "caption": "Relations entre opérateur et infrastructure.",
                            "media_path": "media/relationship-diagram.svg",
                        },
                    ],
                },
                {
                    "type": "technical_annex",
                    "ioc_rows": [*main_rows, *original_rows],
                    "ioc_row_groups": [*main_groups, original_group],
                },
            ],
        },
        values,
        original_iocs,
    )


@pytest.mark.asyncio
async def test_real_typst_compiles_representative_workspace_deterministically(
    tmp_path: Path, typst_binary: str, font_bundle_root: Path
) -> None:
    workspace_root = tmp_path / "workspace"
    _build_runtime_workspace(_REPOSITORY_ROOT, workspace_root)
    font_snapshot = load_font_bundle_snapshot(font_bundle_root, _FONT_BUNDLE_LOCK)
    font_root = tmp_path / "font-snapshot"
    font_root.mkdir()
    request = TypstCompileRequest(
        workspace_root=workspace_root,
        entrypoint_relative_path="RENDERER/publication.typ",
        font_paths=materialize_font_bundle(font_snapshot, font_root),
    )
    compiler = TypstSubprocessCompiler(binary=str(typst_binary))

    first = await compiler.compile(request)
    first_reader = PdfReader(BytesIO(first.content), strict=True)
    second = await compiler.compile(request)
    second_reader = PdfReader(BytesIO(second.content), strict=True)

    assert first.compiler_version == "0.15.1"
    assert len(first_reader.pages) >= 1
    assert not first_reader.is_encrypted
    assert len(second_reader.pages) >= 1
    assert not second_reader.is_encrypted
    assert second.sha256 == first.sha256, (
        "identical Typst workspace produced different PDF SHA-256 values: "
        f"{first.sha256} != {second.sha256}"
    )


@pytest.mark.asyncio
async def test_real_typst_compiles_reduced_article03_draft_preview(
    tmp_path: Path, typst_binary: str, font_bundle_root: Path
) -> None:
    base = _base_v5()
    preview = _reduced_article03_draft_preview(base)
    bundle = load_template_bundle(_REPOSITORY_ROOT / "chpTypst")
    render_source = TypstRenderer().render(
        preview,
        bundle,
        document_label=_PUBLICATION_RENDER_LABEL,
    )
    render_data = json.loads(render_source.render_data_bytes)
    workspace_root = tmp_path / "reduced-art03-opinion-workspace"
    _build_runtime_workspace(
        _REPOSITORY_ROOT,
        workspace_root,
        render_data=render_data,
    )
    font_snapshot = load_font_bundle_snapshot(font_bundle_root, _FONT_BUNDLE_LOCK)
    font_root = tmp_path / "reduced-art03-opinion-fonts"
    font_root.mkdir()
    compiled = await TypstSubprocessCompiler(binary=typst_binary).compile(
        TypstCompileRequest(
            workspace_root=workspace_root,
            entrypoint_relative_path="RENDERER/publication.typ",
            font_paths=materialize_font_bundle(font_snapshot, font_root),
        )
    )

    reader = PdfReader(BytesIO(compiled.content), strict=True)
    assert len(reader.pages) >= 1
    assert not reader.is_encrypted
    assert "Anonymized opinion block" in (reader.pages[0].extract_text() or "")


@pytest.mark.asyncio
async def test_real_typst_lot6_a4_composition_fixture(
    tmp_path: Path,
    typst_binary: str,
    font_bundle_root: Path,
) -> None:
    render_data, expected_iocs, original_iocs = _lot6_composition_render_data()
    media_files = {
        "source-wide.svg": _svg_fixture(1200, 420, "SOURCE WIDE MEDIA", "#e7eef8"),
        "source-portrait.svg": _svg_fixture(480, 1500, "SOURCE PORTRAIT MEDIA", "#f6eadb"),
        "timeline-chart.svg": _timeline_chart_fixture(),
        "flow-diagram.svg": _flow_diagram_fixture(),
        "relationship-diagram.svg": _relationship_diagram_fixture(),
    }
    workspace_root = tmp_path / "lot6-a4-workspace"
    _build_runtime_workspace(
        _REPOSITORY_ROOT,
        workspace_root,
        render_data=render_data,
        media_files=media_files,
    )
    font_snapshot = load_font_bundle_snapshot(font_bundle_root, _FONT_BUNDLE_LOCK)
    font_root = tmp_path / "lot6-a4-font-snapshot"
    font_root.mkdir()
    compiled_pdf = _compile_real_typst_cli(
        typst_binary=str(typst_binary),
        workspace_root=workspace_root,
        entrypoint="RENDERER/publication.typ",
        font_paths=materialize_font_bundle(font_snapshot, font_root),
    )

    review_directory = Path(
        os.environ.get("AUTOWORK_REVIEW_ARTIFACT_DIR", tmp_path / "lot6-a4-review")
    )
    pdf_path = _write_review_pdf(
        review_directory,
        "lot6-composition-a4.pdf",
        compiled_pdf,
    )
    rasterizer = shutil.which("pdftoppm")
    assert rasterizer is not None, "pdftoppm is required to inspect every fixture page"
    png_prefix = review_directory / "lot6-composition-a4"
    with anyio.fail_after(90):
        rasterized = await anyio.run_process(
            (rasterizer, "-png", "-r", "120", str(pdf_path), str(png_prefix))
        )
    assert rasterized.returncode == 0, rasterized.stderr.decode("utf-8", errors="replace")
    png_pages = _review_pngs(review_directory, "lot6-composition-a4-*.png")

    reader = PdfReader(BytesIO(compiled_pdf), strict=True)
    page_text = [page.extract_text() or "" for page in reader.pages]
    text = " ".join(" ".join(value.split()) for value in page_text)
    compact_text = "".join(text.split())
    assert len(reader.pages) >= 3
    assert all(
        (
            abs(float(page.mediabox.width) - 595.28) < 0.2
            and abs(float(page.mediabox.height) - 841.89) < 0.2
        )
        or (
            abs(float(page.mediabox.width) - 841.89) < 0.2
            and abs(float(page.mediabox.height) - 595.28) < 0.2
        )
        for page in reader.pages
    )
    assert "5 octobre 2026 : La source décrit l'activité après sa publication." in text
    for number, caption in enumerate(
        (
            "Figure source wide, repère A.",
            "Figure source portrait, repère B.",
            "Événements datés selon leur source.",
            "Flux de compromission observé.",
            "Relations entre opérateur et infrastructure.",
        ),
        start=1,
    ):
        assert f"Figure {number} : {caption}" in text
    for hidden_title in (
        "Titre chart qui ne doit pas apparaître au-dessus",
        "Titre FLOW qui ne doit pas apparaître au-dessus",
        "Titre RELATIONSHIP qui ne doit pas apparaître au-dessus",
    ):
        assert hidden_title not in text
    assert "Tableau des artefacts observés" in text
    # V17 keeps the retained and unconfirmed original lists in its annex shape.
    assert text.count("IOC") == 2
    reserve_heading = "IOC originaux à lien non démontré"
    assert reserve_heading in text
    compact_reserve_heading = "".join(reserve_heading.split())
    reserve_position = compact_text.index(compact_reserve_heading)
    assert "Le lien avec le sujet" in text and "pas démontré" in text
    assert all(
        "".join(value.split()) not in compact_text[:reserve_position] for value in original_iocs
    )
    assert all("".join(value.split()) in compact_text[reserve_position:] for value in original_iocs)
    missing_iocs = tuple(value for value in expected_iocs if value not in compact_text)
    assert not missing_iocs, f"IOC values missing from extracted PDF text: {missing_iocs[:8]!r}"
    ioc_annex_start = next(
        index for index, page in enumerate(page_text) if "ANNEXE TECHNIQUE" in page.upper()
    )
    ioc_annex_pages = tuple(page_text[ioc_annex_start:])
    compact_pages = tuple("".join(page.split()) for page in ioc_annex_pages)
    hash_values = set(expected_iocs[-20:])
    for value in expected_iocs:
        row_marker = hashlib.sha256(value.encode()).hexdigest()[:10]
        page_indices = [
            index
            for index, compact_page in enumerate(compact_pages)
            if "".join(value.split()) in compact_page
        ]
        assert len(page_indices) == 1, f"IOC row value {value!r} appears on pages {page_indices!r}"
        page_index = page_indices[0]
        assert row_marker in compact_pages[page_index], (
            f"IOC row {value!r} was separated from its provenance marker"
        )
        expected_header = (
            "Empreinte de fichier — une valeur par ligne"
            if value in hash_values
            else "Valeur refangée"
        )
        assert "".join(expected_header.split()) in compact_pages[page_index], (
            f"IOC table continuation page omitted {expected_header!r}"
        )
    assert len(png_pages) == len(reader.pages)
    png_sizes = [path.stat().st_size for path in png_pages]
    assert all(size > 0 for size in png_sizes)


@pytest.mark.asyncio
@pytest.mark.parametrize("media_type", ("diagram", "chart"))
async def test_real_typst_bounds_tall_media_and_keeps_following_text_on_page(
    tmp_path: Path,
    typst_binary: str,
    font_bundle_root: Path,
    media_type: str,
) -> None:
    render_data = {
        "schema_version": "typst-publication-model-v17-synthesis-section-headings",
        "document_label": "Publication de base — artefact de fixture v1",
        "language": "fr",
        "title": "Tall diagram layout regression",
        "content_sections": [
            {"type": "references", "timeline": [], "blocks": [], "sources": []},
            {
                "type": "synthesis",
                "blocks": [
                    {"type": "paragraph", "text": "Text immediately before the diagram."},
                    {
                        "type": media_type,
                        "figure_number": 1,
                        "key": "portrait-diagram",
                        "title": "Portrait diagram title fallback",
                        "caption": "Portrait diagram caption stays with the image.",
                        "source_note": "example.test — p. 1",
                        "media_path": "media/portrait-diagram.svg",
                    },
                    {"type": "paragraph", "text": "Text immediately after the diagram."},
                ],
            },
        ],
    }
    workspace_root = tmp_path / "tall-diagram-workspace"
    _build_runtime_workspace(
        _REPOSITORY_ROOT,
        workspace_root,
        render_data=render_data,
        media_files={
            "portrait-diagram.svg": _svg_fixture(
                420,
                1200,
                "Node label",
                "#e9e4f5",
            )
        },
    )
    font_snapshot = load_font_bundle_snapshot(font_bundle_root, _FONT_BUNDLE_LOCK)
    font_root = tmp_path / "tall-diagram-font-snapshot"
    font_root.mkdir()
    compiled = await TypstSubprocessCompiler(binary=str(typst_binary)).compile(
        TypstCompileRequest(
            workspace_root=workspace_root,
            entrypoint_relative_path="RENDERER/publication.typ",
            font_paths=materialize_font_bundle(font_snapshot, font_root),
        )
    )

    reader = PdfReader(BytesIO(compiled.content), strict=True)
    assert all(
        (
            abs(float(page.mediabox.width) - 595.28) < 0.2
            and abs(float(page.mediabox.height) - 841.89) < 0.2
        )
        or (
            abs(float(page.mediabox.width) - 841.89) < 0.2
            and abs(float(page.mediabox.height) - 595.28) < 0.2
        )
        for page in reader.pages
    )
    page_texts = [" ".join((page.extract_text() or "").split()) for page in reader.pages]
    assert any(
        "Text immediately before the diagram." in text
        and "Figure 1 : Portrait diagram caption stays with the image." in text
        and "Text immediately after the diagram." in text
        for text in page_texts
    )
    assert any("Node label" in text for text in page_texts)

    pdftotext = shutil.which("pdftotext")
    assert pdftotext is not None, "pdftotext is required to check rendered text bounds"
    pdf_path = tmp_path / "tall-media.pdf"
    pdf_path.write_bytes(compiled.content)
    with anyio.fail_after(10):
        bbox = await anyio.run_process(
            (pdftotext, "-bbox-layout", str(pdf_path), "-"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )
    root = ET.fromstring(bbox.stdout.decode("utf-8", errors="replace"))
    pages = [element for element in root.iter() if element.tag.rsplit("}", 1)[-1] == "page"]
    assert len(pages) == len(reader.pages)
    for page in pages:
        page_width = float(page.attrib["width"])
        page_height = float(page.attrib["height"])
        page_lines = [
            (
                float(line.attrib["yMin"]),
                float(line.attrib["yMax"]),
                " ".join(
                    word.text or "" for word in line if word.tag.rsplit("}", 1)[-1] == "word"
                ).strip(),
            )
            for line in page.iter()
            if line.tag.rsplit("}", 1)[-1] == "line"
        ]
        for word in page.iter():
            if word.tag.rsplit("}", 1)[-1] == "word":
                assert 0 <= float(word.attrib["xMin"])
                assert float(word.attrib["xMax"]) <= page_width
                assert 0 <= float(word.attrib["yMin"])
                assert float(word.attrib["yMax"]) <= page_height
        caption_line = next(
            line
            for line in page_lines
            if "Portrait diagram caption stays with the image." in line[2]
        )
        after_line = next(
            line for line in page_lines if "Text immediately after the diagram." in line[2]
        )
        assert caption_line[1] <= after_line[0], (
            f"figure caption overlaps following text: {caption_line} / {after_line}"
        )
