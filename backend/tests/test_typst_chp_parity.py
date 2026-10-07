"""Freeze the CHP renderer's deliberate direct composition of PublicationDocumentV4.

The production Typst renderer does not call the historical ``article()`` helper:
V4 has no category, article number, or Victimologie/Arsenal/Objectif overview
fields. This is an intentional schema boundary. Both render paths must still
use the same CHP page identity, shared typography, and reusable content helpers.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import signal
import struct
import subprocess
import tempfile
import zlib
from dataclasses import replace
from io import BytesIO
from itertools import pairwise
from pathlib import Path
from typing import Any
from uuid import UUID

import anyio
import pytest
from pypdf import PdfReader

from cti_app.application.semantic_annotation import EnglishTermDetector, SemanticAnnotator
from cti_app.application.typst_compilation import (
    TYPST_COMPILER_VERSION,
    TypstCompileRequest,
    load_font_bundle_snapshot,
    materialize_font_bundle,
)
from cti_app.application.typst_render_execution import TypstRenderExecutor
from cti_app.application.typst_rendering import (
    TemplateFile,
    TypstMediaRef,
    TypstRenderer,
    TypstRenderSource,
    TypstTemplateBundle,
    load_template_bundle,
)
from cti_app.domain.production_editorial_enrichment import (
    DiagramEdgeV1,
    DiagramGroupV1,
    DiagramNodeRole,
    DiagramNodeV1,
    DiagramProfile,
    DiagramRelationDirection,
    DiagramRelationType,
    DiagramSpecV1,
    EnrichmentDiagramDirection,
    EnrichmentDiagramKind,
    EnrichmentPlacementKind,
    editorial_enrichment_from_json,
)
from cti_app.domain.publication import (
    ArtifactType,
    PublicationIndicatorGroupV1,
    PublicationIndicatorV1,
)
from cti_app.domain.publication_document import (
    PublicationDocumentV4,
    PublicationDocumentV5,
    PublicationTableColumnV1,
    PublicationTableRowV1,
    publication_document_text_anchors,
)
from cti_app.domain.semantic_annotation import (
    SEMANTIC_ANNOTATION_POLICY_VERSION,
    SEMANTIC_ANNOTATION_SCHEMA_VERSION,
    SemanticAnnotationProposalV1,
    SemanticRole,
    SemanticTextV1,
)
from cti_app.infrastructure.d2_diagram_compiler import D2_COMPILER_VERSION, D2DiagramCompiler
from cti_app.infrastructure.typst_compiler import (
    TypstProcessResult,
    TypstProcessStatus,
    TypstSubprocessCompiler,
)
from tests.test_typst_rendering import (
    _diagram_at,
    _figure_at,
    _frontmatter_v6_case,
    _full_document,
    _table_at,
)

pytest_plugins = (
    "tests.test_typst_compiler_runtime",
    "tests.test_d2_diagram_compiler_runtime",
)

_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
_CHP_TYPST_ROOT = _REPOSITORY_ROOT / "chpTypst"
_FONT_BUNDLE_LOCK = _REPOSITORY_ROOT / "infra" / "typst-fonts.lock"
_IMPORT_RE = re.compile(r'^\s*#import\s+"([^"]+)"\s*:\s*(.*?)\s*$', re.MULTILINE)
_IDENTIFIER_RE = re.compile(r"\*|[A-Za-z_][A-Za-z_0-9-]*")


def _run_typst_compile(argv: tuple[str, ...]) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(argv, capture_output=True, check=False, timeout=20)


@pytest.fixture
def d2_binary() -> str:
    binary = os.environ.get("D2_BINARY") or shutil.which("d2")
    if binary is None:
        pytest.skip("d2 executable is not installed")
    try:
        probe = subprocess.run((binary, "--version"), capture_output=True, check=False, timeout=10)
    except (OSError, subprocess.TimeoutExpired) as exc:
        pytest.skip(f"d2 version check failed: {exc}")
    reported = probe.stdout.decode("utf-8", errors="replace").strip().removeprefix("v")
    if probe.returncode != 0 or reported != D2_COMPILER_VERSION:
        pytest.skip(f"d2 {D2_COMPILER_VERSION} is required, found {reported!r}")
    return binary


def _small_png_fixture(width: int = 64, height: int = 48) -> bytes:
    def chunk(kind: bytes, payload: bytes) -> bytes:
        body = kind + payload
        return struct.pack(">I", len(payload)) + body + struct.pack(">I", zlib.crc32(body))

    pixels = b"".join(b"\x00" + b"\x20\x70\x90\xff" * width for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(pixels, level=9))
        + chunk(b"IEND", b"")
    )


class _SynchronousTypstProcessRunner:
    """Run the real pinned binary without Snap's lingering asyncio pipe handles."""

    async def run(
        self,
        argv: tuple[str, ...] | list[str],
        *,
        environment: dict[str, str],
        timeout_seconds: float,
        stdout_limit: int,
        stderr_limit: int,
    ) -> TypstProcessResult:
        def invoke() -> TypstProcessResult:
            with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
                process = subprocess.Popen(
                    tuple(argv),
                    stdin=subprocess.DEVNULL,
                    stdout=stdout_file,
                    stderr=stderr_file,
                    env=dict(environment),
                    start_new_session=True,
                )
                try:
                    exit_code = process.wait(timeout=timeout_seconds)
                    timed_out = False
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    exit_code = process.wait(timeout=5)
                    timed_out = True
                stdout_file.seek(0)
                stderr_file.seek(0)
                stdout = stdout_file.read(stdout_limit + 1)
                stderr = stderr_file.read(stderr_limit + 1)
                if timed_out:
                    status = TypstProcessStatus.TIMED_OUT
                    exit_code = None
                elif len(stdout) > stdout_limit:
                    status = TypstProcessStatus.STDOUT_LIMIT
                elif len(stderr) > stderr_limit:
                    status = TypstProcessStatus.STDERR_LIMIT
                else:
                    status = (
                        TypstProcessStatus.SUCCEEDED
                        if exit_code == 0
                        else TypstProcessStatus.NON_ZERO_EXIT
                    )
                return TypstProcessResult(
                    status,
                    exit_code,
                    stdout[:stdout_limit],
                    stderr[:stderr_limit],
                )

        return invoke()


def _write_diagram_figure_review_artifacts(
    *,
    directory: Path,
    pdf_bytes: bytes,
    page_text: list[str],
    rasterizer: str | None,
) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    pdf_path = directory / "diagram-figure-review.pdf"
    pdf_path.write_bytes(pdf_bytes)
    if rasterizer is None:
        return

    diagram_page = next(
        index + 1 for index, text in enumerate(page_text) if "Résolution du C2 iranien" in text
    )
    relationship_page = next(
        index + 1
        for index, text in enumerate(page_text)
        if "Documents, fichiers déposés et infrastructure de résolution documentés" in text
    )
    figure_page = next(
        index + 1
        for index, text in enumerate(page_text)
        if "Capture d'une preuve technique" in text
    )
    for page_number, output_name in (
        (diagram_page, "diagram-bitcoin-flow"),
        (relationship_page, "diagram-relationship"),
        (figure_page, "figure-archived-source"),
    ):
        subprocess.run(
            (
                rasterizer,
                "-f",
                str(page_number),
                "-l",
                str(page_number),
                "-r",
                "144",
                "-png",
                "-singlefile",
                str(pdf_path),
                str(directory / output_name),
            ),
            check=True,
            capture_output=True,
        )
        assert (directory / f"{output_name}.png").is_file()


def _load_review_enrichment(path: str):
    return editorial_enrichment_from_json(json.loads(Path(path).read_text(encoding="utf-8")))


@pytest.fixture
def chp_parity_document() -> PublicationDocumentV4:
    """Use the existing domain-backed V4 fixture and its rich-block helpers."""
    table = _table_at(
        "parity-table",
        "Observed command table",
        EnrichmentPlacementKind.AFTER_LEAD,
    )
    wide_table = replace(
        table,
        columns=(
            PublicationTableColumnV1("command", "Command literal and documented invocation"),
            PublicationTableColumnV1("effect", "Purpose, effect, and evidence scope"),
        ),
        rows=(
            PublicationTableRowV1(
                (
                    "-enc powershell.exe [placeholder invocation from evidence]",
                    "Execution role and observed effect, with limits stated beside the "
                    "mechanism rather than compressed into prose.",
                ),
                table.rows[0].evidence_refs,
            ),
            PublicationTableRowV1(
                (
                    "[second documented command placeholder]",
                    "A longer effect description demonstrates predictable wrapping within "
                    "the content-derived column width.",
                ),
                table.rows[0].evidence_refs,
            ),
        ),
    )
    document = _full_document(
        tables=(wide_table,),
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
            replace(
                _figure_at(
                    "parity-figure",
                    "Source figure caption.",
                    EnrichmentPlacementKind.END,
                ),
                provenance=(
                    "archived source document 65d67fad-2db8-4e50-a6a0-6fd09e790000; "
                    "/html[1]/body[1]/div[3]/img[3]; anchor: "
                    "https://secret.example/image.png"
                ),
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


def _normalized_pdf_text(reader: PdfReader) -> tuple[str, str]:
    extracted = "\n".join(page.extract_text() or "" for page in reader.pages)
    normalized = " ".join(extracted.split())
    return normalized, re.sub(r"\s+", "", extracted)


def _page_content_lines(page_text: str) -> list[str]:
    return [
        line.strip()
        for line in page_text.splitlines()
        if line.strip()
        and re.fullmatch(r"\d+\s*/\s*\d+", line.strip()) is None
        and not line.strip().startswith("Bulletin-")
    ]


def _page_body_text_lines(page: Any) -> list[tuple[float, float, str]]:
    positioned: list[tuple[float, float, str]] = []

    def collect(
        text: str,
        ctm: Any,
        text_matrix: Any,
        font_dictionary: Any,
        font_size: float,
    ) -> None:
        del font_dictionary
        if not text.strip() or font_size < 7:
            return
        y = ctm[1] * text_matrix[4] + ctm[3] * text_matrix[5] + ctm[5]
        if 50 <= y <= 785:
            positioned.append((float(y), float(font_size), text.strip()))

    page.extract_text(visitor_text=collect)
    positioned.sort(key=lambda item: item[0], reverse=True)
    lines: list[tuple[float, float, str]] = []
    for y, font_size, text in positioned:
        if lines and abs(lines[-1][0] - y) <= max(2.0, 0.45 * min(lines[-1][1], font_size)):
            prior_y, prior_size, prior_text = lines[-1]
            line_y = prior_y if prior_size >= font_size else y
            lines[-1] = (line_y, max(prior_size, font_size), f"{prior_text} {text}")
        else:
            lines.append((y, font_size, text))
    return lines


def _assert_page_body_text_does_not_overlap(reader: PdfReader) -> None:
    for page_number, page in enumerate(reader.pages, start=1):
        lines = _page_body_text_lines(page)
        for upper, lower in pairwise(lines):
            gap = upper[0] - lower[0]
            minimum_gap = 0.9 * max(upper[1], lower[1])
            assert gap >= minimum_gap, (
                f"page {page_number} has overlapping text lines: "
                f"{upper[2]!r} at y={upper[0]:.2f}pt and "
                f"{lower[2]!r} at y={lower[0]:.2f}pt "
                f"(gap {gap:.2f}pt, need {minimum_gap:.2f}pt)"
            )


def _keep_with_next_sections(
    target: str,
    filler_count: int,
    table: dict[str, object] | None = None,
) -> list[dict[str, object]]:
    filler = [
        {"type": "paragraph", "text": f"Boundary filler {index:03d}."}
        for index in range(filler_count)
    ]
    sections: list[dict[str, object]] = [
        {"type": "references", "timeline": [], "blocks": [], "sources": []}
    ]
    if target in {"table", "long-table"}:
        assert table is not None
        table["title"] = (
            "Vulnérabilités cataloguées par système"
            if target == "table"
            else "Long table pagination probe"
        )
        table["columns"] = ["Système", "Vulnérabilités recherchées"]
        table["column_weights"] = [1.0, 1.0]
        if target == "table":
            table["rows"] = [
                ["Windows", "CVE-2025-001"],
                ["Linux", "CVE-2025-002"],
                ["VPN", "CVE-2025-003"],
                ["Firewall", "CVE-2025-004"],
                ["Hypervisor", "CVE-2025-005"],
            ]
        else:
            long_description = (
                "Description text that wraps across multiple table lines to exercise "
                "repeated headers when the table continues onto another page. "
            ) * 3
            table["rows"] = [
                [f"System {index:02d}", f"Finding {index:02d}. {long_description}"]
                for index in range(1, 31)
            ]
        table.pop("semantic_title", None)
        table.pop("semantic_columns", None)
        table.pop("semantic_cells", None)
        sections.append({"type": "synthesis", "blocks": [*filler, table]})
    else:
        sections.extend(
            (
                {"type": "synthesis", "blocks": filler},
                {
                    "type": "technical_annex",
                    "indicators": {
                        "ips": [f"192.0.2.{index}" for index in range(1, 14)],
                        "domains": [],
                        "urls": [],
                        "emails": [],
                        "hashes": [],
                    },
                    "original_indicators": {
                        "ips": [],
                        "domains": [],
                        "urls": [],
                        "emails": [],
                        "hashes": [],
                    },
                    "original_indicator_note": "",
                },
            )
        )
    return sections


async def _assert_real_typst_keep_with_next_case(
    *,
    tmp_path: Path,
    typst_binary: str,
    font_bundle_root: Path,
    template_files: tuple[TemplateFile, ...],
    source: TypstRenderSource,
    target: str,
    edition: bool = False,
) -> None:
    """Check real Typst page-boundary behavior and body-line geometry."""
    render_source = source
    control_files = tuple(
        replace(
            file,
            content=file.content.replace(b"sticky: true", b"sticky: false")
            .replace(b"breakable: false", b"breakable: true")
            .replace(b"table.header(repeat: true, ..header-cells),", b"..header-cells,")
            .replace(b"repeat: true", b"repeat: false"),
        )
        for file in template_files
    )
    font_snapshot = load_font_bundle_snapshot(font_bundle_root, _FONT_BUNDLE_LOCK)
    font_root = tmp_path / f"{target}-font-snapshot"
    font_root.mkdir()
    font_paths = materialize_font_bundle(font_snapshot, font_root)

    def make_render_data(filler_count: int) -> bytes:
        data = json.loads(render_source.render_data_bytes)
        publication = data["publications"][0] if edition else data
        table = None
        if target in {"table", "long-table"}:
            table = next(
                block
                for section in publication["content_sections"]
                for block in section["blocks"]
                if block["type"] == "table"
            )
        publication["content_sections"] = _keep_with_next_sections(target, filler_count, table)
        return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()

    async def prepare_workspace(name: str, files: tuple[TemplateFile, ...]) -> tuple[Path, Path]:
        workspace = tmp_path / name
        _build_workspace(
            workspace,
            bundle_files=files,
            render_data_bytes=make_render_data(0),
            media_refs=render_source.media_refs,
        )
        data_path = workspace / render_source.render_data_relative_path
        if data_path != workspace / "RENDERER" / "render-data.json":
            (workspace / "RENDERER" / "render-data.json").unlink(missing_ok=True)
            data_path.parent.mkdir(parents=True, exist_ok=True)
            data_path.write_bytes(make_render_data(0))
        return workspace, data_path

    fixed_workspace, fixed_data_path = await prepare_workspace(
        f"{target}-sticky-output", template_files
    )

    async def compile_at(workspace: Path, data_path: Path, filler_count: int) -> PdfReader:
        await anyio.Path(data_path).write_bytes(make_render_data(filler_count))
        output_path = workspace / "keep-with-next.pdf"
        argv = [typst_binary, "compile", "--root", str(workspace)]
        for font_path in font_paths:
            argv.extend(("--font-path", str(font_path)))
        argv.extend(
            (
                "--ignore-system-fonts",
                "--creation-timestamp",
                "0",
                str(workspace / render_source.entrypoint_relative_path),
                str(output_path),
            )
        )
        completed = await anyio.to_thread.run_sync(_run_typst_compile, tuple(argv))
        assert completed.returncode == 0, completed.stderr.decode("utf-8", errors="replace")
        return PdfReader(BytesIO(await anyio.Path(output_path).read_bytes()), strict=True)

    is_table = target in {"table", "long-table"}
    marker = (
        "Vulnérabilités cataloguées par système"
        if target == "table"
        else "Long table pagination probe"
        if target == "long-table"
        else "IOC"
    )
    following = (
        ("Système", "Windows")
        if target == "table"
        else ("Système", "System 01")
        if target == "long-table"
        else ("Adresses IP :", "192.0.2.1")
    )
    filler_count = (29 if edition else 30) if is_table else (30 if edition else 31)
    control_page_index: int | None = None
    selected_filler_count = filler_count
    if target == "long-table":
        control_workspace, control_data_path = await prepare_workspace(
            f"{target}-unsticky-control", control_files
        )
        observed_endings: list[str] = []
        for candidate_filler_count in range(filler_count, max(-1, filler_count - 9), -1):
            control_reader = await compile_at(
                control_workspace, control_data_path, candidate_filler_count
            )
            for page_index, page in enumerate(control_reader.pages[:-1]):
                page_text = page.extract_text() or ""
                lines = _page_content_lines(page_text)
                observed_endings.append(lines[-1] if lines else "<empty>")
                next_page_text = control_reader.pages[page_index + 1].extract_text() or ""
                page_ending = " ".join(lines[-2:])
                header_is_last = all(
                    heading in page_ending for heading in ("Système", "Vulnérabilités recherchées")
                )
                first_row = following[-1]
                if (
                    marker in page_text
                    and header_is_last
                    and first_row not in page_text
                    and first_row in next_page_text
                ):
                    control_page_index = page_index
                    selected_filler_count = candidate_filler_count
                    break
            if control_page_index is not None:
                break
        assert control_page_index is not None, (
            f"could not calibrate a split table {marker!r} at {filler_count} filler blocks; "
            f"page endings: {observed_endings!r}"
        )

    fixed_reader = await compile_at(fixed_workspace, fixed_data_path, selected_filler_count)
    _assert_page_body_text_does_not_overlap(fixed_reader)
    fixed_page_index = next(
        (
            page_index
            for page_index, page in enumerate(fixed_reader.pages)
            if marker in (page.extract_text() or "")
        ),
        None,
    )
    assert fixed_page_index is not None
    if control_page_index is not None:
        assert fixed_page_index > control_page_index
    else:
        assert fixed_page_index > 0
    fixed_page_text = fixed_reader.pages[fixed_page_index].extract_text() or ""
    assert all(item in fixed_page_text for item in following)
    fixed_page_lines = _page_body_text_lines(fixed_reader.pages[fixed_page_index])
    marker_line_index = next(
        index for index, line in enumerate(fixed_page_lines) if marker in line[2]
    )
    following_lines = fixed_page_lines[marker_line_index + 1 :]
    assert len(following_lines) >= 3, (
        f"{marker!r} has fewer than three following body lines on its page: "
        f"{[line[2] for line in following_lines]!r}"
    )
    if target == "ioc":
        following_text = " ".join(line[2] for line in following_lines)
        assert "Adresses IP" in following_text
        assert all(f"192.0.2.{index}" in following_text for index in range(1, 4))
    elif target == "table":
        table_page_text = fixed_reader.pages[fixed_page_index].extract_text() or ""
        assert all(
            row_label in table_page_text
            for row_label in ("Windows", "Linux", "VPN", "Firewall", "Hypervisor")
        )
        assert "Vulnérabilités recherchées" in table_page_text
        previous_page_lines = _page_content_lines(
            fixed_reader.pages[fixed_page_index - 1].extract_text() or ""
        )
        assert previous_page_lines
        assert "Vulnérabilités recherchées" not in " ".join(previous_page_lines[-2:])
    else:
        assert target == "long-table"
        first_table_page_text = fixed_reader.pages[fixed_page_index].extract_text() or ""
        first_page_rows = [
            f"System {index:02d}"
            for index in range(1, 31)
            if f"System {index:02d}" in first_table_page_text
        ]
        assert len(first_page_rows) >= 2, (
            f"long table starts with fewer than two body rows: {first_page_rows!r}"
        )
        assert "Vulnérabilités recherchées" in first_table_page_text
        continuation_pages = [
            page
            for page in fixed_reader.pages[fixed_page_index + 1 :]
            if re.search(r"System \d{2}", page.extract_text() or "")
        ]
        assert continuation_pages, "30-row table did not continue on a later page"
        for continuation_page in continuation_pages:
            continuation_text = continuation_page.extract_text() or ""
            assert "Vulnérabilités recherchées" in continuation_text
            continuation_rows = [
                f"System {index:02d}"
                for index in range(1, 31)
                if f"System {index:02d}" in continuation_text
            ]
            assert len(continuation_rows) >= 2, (
                f"continuation page has fewer than two body rows: {continuation_rows!r}"
            )
    previous_page_lines = _page_content_lines(
        fixed_reader.pages[fixed_page_index - 1].extract_text() or ""
    )
    assert previous_page_lines
    assert previous_page_lines[-1] != marker
    assert any("Boundary filler" in line for line in previous_page_lines[-3:])


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
                '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 480 160">'
                '<rect x="8" y="34" width="180" height="92" rx="8" '
                'fill="#E8EEF3" stroke="#243B53" stroke-width="3"/>'
                '<rect x="292" y="34" width="180" height="92" rx="8" '
                'fill="#E8EEF3" stroke="#243B53" stroke-width="3"/>'
                '<path d="M190 80h96m0 0-14-12m14 12-14 12" '
                'fill="none" stroke="#243B53" stroke-width="4"/>'
                '<text x="26" y="85" font-family="sans-serif" font-size="19" '
                'fill="#243B53">ExampleRAT</text>'
                '<text x="328" y="85" font-family="sans-serif" font-size="19" '
                'fill="#243B53">Execution</text>'
                '<text x="201" y="112" font-family="sans-serif" font-size="13" '
                'fill="#243B53">launches</text></svg>',
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
    }
    assert "source-list" not in production_helpers_imports[shared_helpers]
    assert production_helpers_imports[colors] == {"grey"}
    style_imports = _resolved_imports(document_style)
    assert style_imports[header_footer] == {"report-header", "report-footer"}
    assert style_imports[colors] == {"purple"}
    assert _resolved_imports(shared_helpers)[colors] == {"*"}
    assert _resolved_imports(header_footer)[colors] == {"dark", "light-grey"}
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

    text, _ = _normalized_pdf_text(reader)
    for placeholder in (
        "Bulletin-CODE",
        "Bulletin n°XX",
        "infrastructures X",
        "XX",
        "CODE",
    ):
        assert placeholder not in text

    expected_text = (
        "CHP visual parity fixture",
        "RÉFÉRENCES",
        "No display date",
        "One source event",
        "Several source event",
        "SYNTHÈSE",
        "Synthesis lead paragraph for parity coverage.",
        "A second lead paragraph.",
        "Observed command table",
        "Command",
        "Purpose",
        "-enc",
        "Execution",
        "Section paragraph describing the observed activity.",
        "Figure 1 : Diagram asset caption.",
        "Figure 2 : Source figure caption.",
        "Source : example.test — p. 1",
        "Display ip",
        "Display domain",
        "Display url",
        "Display email",
        "Display hash",
        "IOC",
        "https://example.test/one",
        "https://example.test/two",
    )
    for expected in expected_text:
        assert expected in text
    for internal_detail in (
        "Provenance",
        "65d67fad-2db8-4e50-a6a0-6fd09e790000",
        "/html[1]",
        "secret.example",
        "HTML image",
    ):
        assert internal_detail not in text
    assert "synthesis_internal_heading" not in text
    assert "extraction_source_skipped" not in text
    assert "synthesis_output_invalid" not in text
    assert "Attribution remains unresolved" not in text
    assert text.count("RÉFÉRENCES") == 1
    assert "Chronologie" not in text
    assert "Sources complémentaires" not in text
    assert text.count("https://example.test/one") == 1
    assert text.count("https://example.test/two") == 1
    assert re.search(r"No display date\s*1", text)
    assert re.search(r"Several source event\s*1\s*,\s*2", text)
    assert text.index("RÉFÉRENCES") < text.index("SYNTHÈSE")
    assert text.count("Synthesis lead paragraph for parity coverage.") == 1
    assert text.count("A second lead paragraph.") == 1
    assert "Primary source" not in text
    assert {media_ref.expected_mime_type for media_ref in render_source.media_refs} == {
        "image/svg+xml",
        "image/png",
    }
    assert _embedded_visual_xobject_count(reader) >= len(render_source.media_refs)


@pytest.mark.asyncio
async def test_real_typst_render_shows_diagram_and_archived_figure_captions(
    tmp_path: Path,
    typst_binary: str,
    d2_binary: str,
    font_bundle_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base_diagram = _diagram_at(
        "bitcoin-resolution",
        "Flux de résolution C2 via Bitcoin",
        EnrichmentPlacementKind.AFTER_LEAD,
        asset_id=UUID(int=8181),
    )
    evidence_refs = base_diagram.nodes[0].evidence_refs
    nodes = (
        DiagramNodeV1(
            "operator",
            "Opérateur",
            evidence_refs,
            DiagramNodeRole.ACTOR,
        ),
        DiagramNodeV1(
            "transaction",
            "Transaction Bitcoin C2",
            evidence_refs,
            DiagramNodeRole.DATA_ARTIFACT,
        ),
        DiagramNodeV1(
            "malware",
            "Malware",
            evidence_refs,
            DiagramNodeRole.MALWARE_TOOL,
        ),
        DiagramNodeV1(
            "offchain",
            "Cycle hors chaîne",
            evidence_refs,
            DiagramNodeRole.TECHNIQUE_STEP,
        ),
    )
    edges = (
        DiagramEdgeV1(
            "operator",
            "transaction",
            "inscrit C2",
            evidence_refs,
            DiagramRelationType.FACTUAL,
        ),
        DiagramEdgeV1(
            "transaction",
            "malware",
            "fournit au malware",
            evidence_refs,
            DiagramRelationType.FACTUAL,
        ),
        DiagramEdgeV1(
            "malware",
            "offchain",
            "reprend hors chaîne",
            evidence_refs,
            DiagramRelationType.FACTUAL,
        ),
    )
    diagram = replace(
        base_diagram,
        kind=EnrichmentDiagramKind.NETWORK_FLOW,
        title="Résolution du C2 iranien via Bitcoin",
        caption=None,
        direction=EnrichmentDiagramDirection.LEFT_TO_RIGHT,
        nodes=nodes,
        edges=edges,
        groups=(),
    )
    enrichment_path = os.environ.get("AUTOWORK_ENRICH_JSON")
    if enrichment_path is None:
        semantic_diagram = DiagramSpecV1(
            key=diagram.key,
            kind=diagram.kind,
            title=diagram.title,
            caption=diagram.caption,
            direction=diagram.direction,
            profile=DiagramProfile.FLOW,
            nodes=diagram.nodes,
            edges=diagram.edges,
            groups=diagram.groups,
            placement=diagram.placement,
        )
    else:
        enrichment = _load_review_enrichment(enrichment_path)
        source_diagram = next(
            item for item in enrichment.diagrams if item.key == "iran_bitcoin_bdd_resolution_flow"
        )
        role_by_id = {
            "iran_nexus_operator": DiagramNodeRole.ACTOR,
            "bitcoin_c2_transaction": DiagramNodeRole.DATA_ARTIFACT,
            "malware_retrieval": DiagramNodeRole.MALWARE_TOOL,
            "offchain_lifecycle": DiagramNodeRole.TECHNIQUE_STEP,
        }
        label_by_id = {
            "iran_nexus_operator": "Opérateur",
            "bitcoin_c2_transaction": "Transaction Bitcoin C2",
            "malware_retrieval": "Malware",
            "offchain_lifecycle": "Cycle hors chaîne",
        }
        edge_labels = ("inscrit C2", "fournit au malware", "reprend hors chaîne")
        diagram = replace(diagram, title="Résolution du C2 iranien via Bitcoin")
        semantic_diagram = replace(
            source_diagram,
            kind=EnrichmentDiagramKind.NETWORK_FLOW,
            title=diagram.title,
            direction=EnrichmentDiagramDirection.LEFT_TO_RIGHT,
            nodes=tuple(
                replace(node, label=label_by_id[node.node_id], role=role_by_id[node.node_id])
                for node in source_diagram.nodes
            ),
            edges=tuple(
                replace(edge, label=edge_labels[index])
                for index, edge in enumerate(source_diagram.edges)
            ),
        )
    compiled_diagram = await D2DiagramCompiler(binary=d2_binary).compile(semantic_diagram)

    relationship_title = "Relations entre document, fichiers et infrastructure"
    relationship_caption = "Documents, fichiers déposés et infrastructure de résolution documentés."
    relationship_diagram = replace(
        _diagram_at(
            "relationship-pivots",
            relationship_title,
            EnrichmentPlacementKind.AFTER_LEAD,
            asset_id=UUID(int=8183),
        ),
        caption=relationship_caption,
    )
    relationship_nodes = (
        DiagramNodeV1("document", "Document Word", evidence_refs, DiagramNodeRole.DATA_ARTIFACT),
        DiagramNodeV1("dll", "sample.dll", evidence_refs, DiagramNodeRole.DATA_ARTIFACT),
        DiagramNodeV1(
            "url", "https://drop.example/a", evidence_refs, DiagramNodeRole.INFRASTRUCTURE
        ),
        DiagramNodeV1("domain", "drop.example", evidence_refs, DiagramNodeRole.INFRASTRUCTURE),
        DiagramNodeV1("ip", "203.0.113.8", evidence_refs, DiagramNodeRole.INFRASTRUCTURE),
    )
    relationship_edges = (
        DiagramEdgeV1("document", "dll", "dépose", evidence_refs),
        DiagramEdgeV1("document", "url", "référence", evidence_refs),
        DiagramEdgeV1("domain", "ip", "résout vers", evidence_refs),
        DiagramEdgeV1(
            "dll",
            "url",
            "pivot possible",
            evidence_refs,
            DiagramRelationType.INFERENCE,
            DiagramRelationDirection.UNDIRECTED,
        ),
    )
    semantic_relationship = DiagramSpecV1(
        key="relationship-pivots",
        kind=EnrichmentDiagramKind.COMPONENT_RELATIONSHIP,
        profile=DiagramProfile.RELATIONSHIP,
        title=relationship_title,
        caption=relationship_diagram.caption,
        direction=EnrichmentDiagramDirection.TOP_TO_BOTTOM,
        nodes=relationship_nodes,
        edges=relationship_edges,
        groups=(DiagramGroupV1("operator-infra", "Infrastructure opérateur", ("domain", "ip")),),
        placement=relationship_diagram.placement,
    )
    compiled_relationship = await D2DiagramCompiler(binary=d2_binary).compile(semantic_relationship)

    png_bytes = _small_png_fixture()
    figure = replace(
        _figure_at(
            "archived-source-visual",
            "Capture d'une preuve technique",
            EnrichmentPlacementKind.AFTER_LEAD,
            asset_id=UUID(int=8282),
        ),
        sha256=hashlib.sha256(png_bytes).hexdigest(),
        byte_size=len(png_bytes),
    )
    document = _full_document(diagrams=(diagram, relationship_diagram), figures=(figure,))
    production_bundle = load_template_bundle(_CHP_TYPST_ROOT)
    # Keep this render focused on the production diagram/figure helpers. The shared
    # general-purpose helper currently has an unrelated Typst syntax error in its
    # timeline URL branch, so this local bundle supplies only those imported stubs.
    test_entrypoint = """
#import "publication_helpers.typ": render-diagram, render-figure
#let publication = json("render-data.json")
#set page(width: 21cm, height: 29.7cm, margin: 1.5cm)
#set text(font: "Hanken Grotesk", size: 10pt)
#heading(level: 1)[#publication.title]
#for section in publication.content_sections {
  if section.type == "synthesis" {
    for item in section.blocks {
      if item.type == "diagram" { render-diagram(item) }
      else if item.type == "figure" { render-figure(item) }
    }
  }
}
"""
    helper_shim = """
#let section-title(body) = body
#let timeline(events) = events
#let styled-table(item) = item
#let ioc-list(title: none, ips: (), domains: (), urls: (), emails: (), hashes: (), note: none) = []
#let semantic-text(role, body) = body
#let semantic-or-plain(text, semantic-spans) = text
"""
    template_bundle = TypstTemplateBundle(
        template_version="review-diagram-figure-harness-v1",
        sha256="a" * 64,
        files=tuple(
            TemplateFile(
                file.relative_path,
                test_entrypoint.encode()
                if file.relative_path == "RENDERER/publication.typ"
                else helper_shim.encode()
                if file.relative_path == "UTILS/helpers.typ"
                else file.content,
            )
            for file in production_bundle.files
        ),
    )
    render_source = TypstRenderer().render(document, template_bundle)
    compiled_by_asset_id = {
        UUID(int=8181): compiled_diagram,
        UUID(int=8183): compiled_relationship,
    }
    media = {
        media_ref.asset_id: (
            compiled_by_asset_id[media_ref.asset_id].media_bytes
            if media_ref.expected_mime_type == "image/svg+xml"
            else png_bytes
        )
        for media_ref in render_source.media_refs
    }
    font_bundle = load_font_bundle_snapshot(font_bundle_root, _FONT_BUNDLE_LOCK)
    # Keep process lookup stable for this Snap-backed local compiler.
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    compiler = TypstSubprocessCompiler(
        runner=_SynchronousTypstProcessRunner(),
        binary=typst_binary,
    )
    executor = TypstRenderExecutor(media_asset_store=object(), compiler=compiler)
    executed = await executor.execute(
        render_source=render_source,
        template_bundle=template_bundle,
        font_bundle=font_bundle,
        resolved_media=media,
    )
    pdf_bytes = executed.compiled_document.content
    reader = PdfReader(BytesIO(pdf_bytes), strict=True)
    page_text = [page.extract_text() or "" for page in reader.pages]
    normalized = " ".join(" ".join(text.split()) for text in page_text)
    assert "Figure 1 : Résolution du C2 iranien via Bitcoin" in normalized
    assert f"Figure 2 : {relationship_caption}" in normalized
    assert "Figure 3 : Capture d'une preuve technique" in normalized
    assert "Source : example.test" in normalized
    assert "Provenance" not in normalized
    assert "HTML image" not in normalized

    review_directory = Path(os.environ.get("AUTOWORK_REVIEW_ARTIFACT_DIR", tmp_path / "review"))
    rasterizer = shutil.which("pdftoppm") if "AUTOWORK_REVIEW_ARTIFACT_DIR" in os.environ else None
    if "AUTOWORK_REVIEW_ARTIFACT_DIR" in os.environ:
        assert rasterizer is not None
    _write_diagram_figure_review_artifacts(
        directory=review_directory,
        pdf_bytes=pdf_bytes,
        page_text=page_text,
        rasterizer=rasterizer,
    )


@pytest.mark.asyncio
async def test_real_typst_omits_references_heading_when_timeline_is_empty(
    tmp_path: Path,
    typst_binary: str,
    font_bundle_root: Path,
    chp_parity_document: PublicationDocumentV4,
) -> None:
    document = replace(chp_parity_document, timeline=())
    template_bundle = load_template_bundle(_CHP_TYPST_ROOT)
    render_source = TypstRenderer().render(document, template_bundle)
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
    compiled = await TypstSubprocessCompiler(binary=str(typst_binary)).compile(
        TypstCompileRequest(
            workspace_root=workspace_root,
            entrypoint_relative_path="RENDERER/publication.typ",
            font_paths=materialize_font_bundle(font_snapshot, font_root),
        )
    )
    text, _ = _normalized_pdf_text(PdfReader(BytesIO(compiled.content), strict=True))

    assert "SYNTHÈSE" in text
    assert "RÉFÉRENCES" not in text


async def test_real_typst_renders_annotated_literal_content_without_evaluating_it(
    tmp_path: Path,
    typst_binary: str,
    font_bundle_root: Path,
    chp_parity_document: PublicationDocumentV4,
) -> None:
    command = '`curl "$x" #import "evil" ] $math$`'
    literal_text = (
        r"NetSync_<username>, %APPDATA%\Microsoft\Network, "
        "# shepherd-persist; and ~/.node_packages"
    )
    lead_text = f"APT Étoile used the literal command {command}. It also used {literal_text}."
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
    for literal in (
        "NetSync_<username>",
        r"%APPDATA%\Microsoft\Network",
        "# shepherd-persist;",
        "~/.node_packages",
    ):
        assert literal in text
    for literal in ("#import", "evil", "]", "$math$", "`"):
        assert literal in text or literal.replace(" ", "") in compact_text


@pytest.mark.asyncio
async def test_real_typst_frontmatter_and_120_original_iocs_flow_across_pages(
    tmp_path: Path,
    typst_binary: str,
    font_bundle_root: Path,
) -> None:
    source_document, *_case = _frontmatter_v6_case()
    source_id = source_document.original_indicators[0].indicators[0].source_document_ids[0]
    indicators = tuple(
        PublicationIndicatorV1(
            value=f"original-{index:03d}.example",
            normalized_value=f"original-{index:03d}.example",
            artifact_type=ArtifactType.DOMAIN,
            source_document_ids=(source_id,),
        )
        for index in range(120)
    )
    publication = PublicationDocumentV5(
        document=source_document.document,
        semantic_text=source_document.semantic_text,
        references=source_document.references,
        original_indicators=(PublicationIndicatorGroupV1(ArtifactType.DOMAIN, indicators),),
    )
    template_bundle = load_template_bundle(_CHP_TYPST_ROOT)
    render_source = TypstRenderer().render(publication, template_bundle)
    workspace_root = tmp_path / "workspace-frontmatter"
    _build_workspace(
        workspace_root,
        bundle_files=template_bundle.files,
        render_data_bytes=render_source.render_data_bytes,
        media_refs=render_source.media_refs,
    )
    font_snapshot = load_font_bundle_snapshot(font_bundle_root, _FONT_BUNDLE_LOCK)
    font_root = tmp_path / "font-snapshot-frontmatter"
    font_root.mkdir()
    compiled = await TypstSubprocessCompiler(binary=str(typst_binary)).compile(
        TypstCompileRequest(
            workspace_root=workspace_root,
            entrypoint_relative_path="RENDERER/publication.typ",
            font_paths=materialize_font_bundle(font_snapshot, font_root),
        )
    )
    reader = PdfReader(BytesIO(compiled.content), strict=True)
    text, compact_text = _normalized_pdf_text(reader)
    assert len(reader.pages) >= 2
    assert "[Example actor] Décrit une activité documentée" in text
    assert "RÉFÉRENCES" in text
    assert text.count("IOC") == 2
    reserve_heading = "IOC originaux à lien non démontré"
    assert reserve_heading in text
    reserve_position = text.index(reserve_heading)
    assert text.index("core.example") < reserve_position
    assert "Le lien avec le sujet" in text and "pas démontré" in text
    assert "10 janvier 2026" in text
    assert "https://a.example/report" in text
    assert "Date de publication non précisée" in text
    assert all(f"original-{index:03d}.example" in compact_text for index in range(120))
    main_list_text = text[:reserve_position]
    reserve_list_text = text[reserve_position:]
    assert all(f"original-{index:03d}.example" not in main_list_text for index in range(120))
    assert all(f"original-{index:03d}.example" in reserve_list_text for index in range(120))

    output_dir = anyio.Path(
        os.environ.get("AUTOWORK_REVIEW_ARTIFACT_DIR", str(tmp_path / "review"))
    )
    await output_dir.mkdir(parents=True, exist_ok=True)
    pdf_path = output_dir / "frontmatter-ioc-review.pdf"
    await pdf_path.write_bytes(compiled.content)
    rasterizer = shutil.which("pdftoppm")
    assert rasterizer is not None, "pdftoppm is required to save review PNG pages"
    png_prefix = output_dir / "frontmatter-ioc-review"
    rasterized = await anyio.run_process(
        (rasterizer, "-png", "-r", "120", str(pdf_path), str(png_prefix)),
        check=False,
    )
    assert rasterized.returncode == 0, rasterized.stderr.decode("utf-8", errors="replace")
    review_pages = [path async for path in output_dir.glob("frontmatter-ioc-review-*.png")]
    assert len(review_pages) >= 2


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ("table", "long-table", "ioc"))
async def test_real_typst_article_keeps_titles_with_following_content(
    target: str,
    tmp_path: Path,
    typst_binary: str,
    font_bundle_root: Path,
) -> None:
    template_bundle = load_template_bundle(_CHP_TYPST_ROOT)
    tables = (
        (
            _table_at(
                "sticky-title",
                "Migrations recommandées des protocoles OT",
                EnrichmentPlacementKind.AFTER_LEAD,
            ),
        )
        if target in {"table", "long-table"}
        else ()
    )
    render_source = TypstRenderer().render(_full_document(tables=tables), template_bundle)
    await _assert_real_typst_keep_with_next_case(
        tmp_path=tmp_path,
        typst_binary=typst_binary,
        font_bundle_root=font_bundle_root,
        template_files=template_bundle.files,
        source=render_source,
        target=target,
    )
