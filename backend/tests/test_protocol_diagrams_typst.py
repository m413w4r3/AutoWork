from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from cti_app.application.edition_typst_rendering import EditionTypstRenderer
from cti_app.application.protocol_diagram_projection import (
    compile_protocol_render_result,
    opinion_protocol_figures,
)
from cti_app.application.publication_augmentation import augment_publication_with_opinion
from cti_app.application.typst_compilation import (
    TypstCompileRequest,
    load_font_bundle_snapshot,
    materialize_font_bundle,
)
from cti_app.application.typst_rendering import (
    TypstRenderer,
    load_template_bundle,
)
from cti_app.domain.analyst_opinion import OpinionProtocolFigureV1
from cti_app.domain.errors import EntityNotFoundError
from cti_app.domain.media_assets import MediaAssetManifest
from cti_app.domain.protocol_model import ProtocolDiagramKind, ProtocolRenderResultV1
from cti_app.infrastructure.d2_diagram_compiler import D2DiagramCompiler
from cti_app.infrastructure.database.repositories.investigation import _opinion_from_row
from cti_app.infrastructure.typst_compiler import TypstSubprocessCompiler
from tests.test_edition_typst_rendering import _edition_document
from tests.test_protocol_diagrams import _three_views
from tests.test_publication_document_v6 import _approved_opinion, _base_v5
from tests.test_typst_compiler_runtime import _build_runtime_workspace

_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
_TYPST_BINARY = os.environ.get("TYPST_BINARY") or shutil.which("typst")
_D2_BINARY = os.environ.get("D2_BINARY") or shutil.which("d2")
_FONT_LOCK = _REPOSITORY_ROOT / "infra" / "typst-fonts.lock"
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_GENERIC_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 320 80">'
    '<rect width="320" height="80" fill="#f2f4f7"/>'
    '<text x="8" y="44" font-size="18">Synthetic fixture</text></svg>'
)


class _MemoryBlobStore:
    def __init__(self) -> None:
        self._contents: dict[UUID, bytes] = {}

    async def put_bytes(self, content: bytes, *, bucket: str, mime_type: str) -> UUID:
        del bucket, mime_type
        blob_id = uuid4()
        self._contents[blob_id] = content
        return blob_id

    async def read_bytes(self, blob_id: UUID, *, max_bytes: int) -> bytes:
        content = self._contents[blob_id]
        if len(content) > max_bytes:
            raise ValueError("test blob exceeds its read limit")
        return content


class _MemoryMediaAssetRepository:
    def __init__(self) -> None:
        self._assets: dict[UUID, MediaAssetManifest] = {}

    async def add_if_absent(self, manifest: MediaAssetManifest) -> MediaAssetManifest:
        return self._assets.setdefault(manifest.asset_id, manifest)

    async def get(self, asset_id: UUID) -> MediaAssetManifest | None:
        return self._assets.get(asset_id)


class _MemoryUnitOfWork:
    def __init__(self, repository: _MemoryMediaAssetRepository) -> None:
        self.media_assets = repository

    async def __aenter__(self) -> _MemoryUnitOfWork:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def commit(self) -> None:
        return None


class _MemoryUnitOfWorkFactory:
    def __init__(self, repository: _MemoryMediaAssetRepository) -> None:
        self._repository = repository

    def __call__(self) -> _MemoryUnitOfWork:
        return _MemoryUnitOfWork(self._repository)


def _memory_media_store():
    from cti_app.application.media_assets import MediaAssetStore

    blobs = _MemoryBlobStore()
    repository = _MemoryMediaAssetRepository()
    return MediaAssetStore(blobs, _MemoryUnitOfWorkFactory(repository)), blobs


def _typst_binary() -> str:
    if _TYPST_BINARY is None:
        pytest.skip("Typst compiler capability is unavailable")
    result = subprocess.run((_TYPST_BINARY, "--version"), capture_output=True, check=False)
    reported = result.stdout.decode("utf-8", errors="replace").strip()
    assert result.returncode == 0 and reported.startswith("typst 0.15.1"), reported
    return _TYPST_BINARY


def _render_result() -> ProtocolRenderResultV1:
    return ProtocolRenderResultV1(
        schema_version="ProtocolRenderResultV1",
        model_sha256="a" * 64,
        diagrams=_three_views(),
        factual_statements=("Three views use synthetic passive protocol evidence.",),
    )


def _install_workspace(
    source,
    media_bytes: dict[UUID, bytes],
    workspace_root: Path,
    *,
    entrypoint: str,
) -> None:
    render_data = json.loads(source.render_data_bytes)
    media_files: dict[str, str] = {}
    media_content: dict[str, bytes] = {}
    for reference in source.media_refs:
        content = media_bytes.get(reference.asset_id)
        if content is None:
            if reference.expected_mime_type == "image/svg+xml":
                content = _GENERIC_SVG.encode("utf-8")
            elif reference.expected_mime_type == "image/png":
                content = (_REPOSITORY_ROOT / "chpTypst" / "UTILS" / "chap.png").read_bytes()
            else:
                raise AssertionError(
                    f"Unexpected fixture media type: {reference.expected_mime_type}"
                )
        media_name = Path(reference.media_path).name
        media_content[media_name] = content
        if content.startswith(b"<svg"):
            media_files[media_name] = content.decode("utf-8")
    _build_runtime_workspace(
        _REPOSITORY_ROOT,
        workspace_root,
        render_data=render_data,
        media_files=media_files,
    )
    media_root = workspace_root / "RENDERER" / "media"
    for name, content in media_content.items():
        (media_root / name).write_bytes(content)
    if entrypoint == "RENDERER/edition.typ":
        (workspace_root / "RENDERER" / "edition-render-data.json").write_bytes(
            source.render_data_bytes
        )


def _compile_pdf(
    *,
    typst_binary: str,
    workspace_root: Path,
    font_root: Path,
    entrypoint: str,
):
    font_snapshot = load_font_bundle_snapshot(_REPOSITORY_ROOT / "chpTypst", _FONT_LOCK)
    font_paths = materialize_font_bundle(font_snapshot, font_root)
    return TypstSubprocessCompiler(binary=typst_binary).compile(
        TypstCompileRequest(
            workspace_root=workspace_root,
            entrypoint_relative_path=entrypoint,
            font_paths=font_paths,
        )
    )


def _persist_and_verify_pdf_outputs(
    output_root: Path,
    rendered,
    article_pdf: bytes,
    edition_pdf: bytes,
) -> None:
    output_root.mkdir(parents=True, exist_ok=True)
    by_kind = {item.spec.kind: item for item in rendered}
    for kind in ProtocolDiagramKind:
        preview_path = output_root / f"{kind.value}.png"
        preview_path.write_bytes(by_kind[kind].preview.media_bytes)
        assert preview_path.stat().st_size > len(_PNG_SIGNATURE)

    for name, pdf_content in (("article", article_pdf), ("edition", edition_pdf)):
        pdf_path = output_root / f"synthetic-{name}.pdf"
        pdf_path.write_bytes(pdf_content)
        info = subprocess.run(("pdfinfo", str(pdf_path)), capture_output=True, check=True)
        info_text = info.stdout.decode("utf-8", errors="replace")
        page_count = re.search(r"^Pages:\s+(\d+)$", info_text, re.MULTILINE)
        assert page_count is not None and int(page_count.group(1)) > 0
        extracted = subprocess.run(
            ("pdftotext", str(pdf_path), "-"), capture_output=True, check=True
        )
        pdf_text = " ".join(extracted.stdout.decode("utf-8", errors="replace").split())
        for spec in _three_views():
            assert spec.title in pdf_text
            assert spec.kind.value in pdf_text
        assert "Legend: OBSERVED green" in pdf_text
        assert "INFERRED orange" in pdf_text
        assert "UNRESOLVED gray" in pdf_text
        assert "CONTRADICTED red" in pdf_text
        raster_prefix = output_root / f"synthetic-{name}-page"
        subprocess.run(
            (
                "pdftoppm",
                "-f",
                "1",
                "-l",
                "1",
                "-singlefile",
                "-png",
                str(pdf_path),
                str(raster_prefix),
            ),
            capture_output=True,
            check=True,
        )
        assert raster_prefix.with_suffix(".png").stat().st_size > 1024


@pytest.mark.asyncio
@pytest.mark.skipif(_D2_BINARY is None, reason="D2 compiler capability is unavailable")
@pytest.mark.skipif(_TYPST_BINARY is None, reason="Typst compiler capability is unavailable")
async def test_real_article_and_edition_render_all_protocol_figures(
    tmp_path: Path,
) -> None:
    typst_binary = _typst_binary()
    d2 = D2DiagramCompiler(binary=_D2_BINARY or "d2")
    media_store, _blobs = _memory_media_store()
    rendered = await compile_protocol_render_result(
        _render_result(),
        compiler=d2,
        media_asset_store=media_store,
        production_run_id=UUID(int=9901),
    )
    figures = opinion_protocol_figures(rendered)
    format_figure = next(
        figure for figure in figures if figure.kind is ProtocolDiagramKind.MESSAGE_FORMAT
    )
    assert len(format_figure.field_evidence) == 3
    assert format_figure.field_evidence[0].field_id == "length"
    assert format_figure.field_evidence[0].evidence_refs[0].capture_sha256 == "e" * 64
    assert format_figure.field_evidence[0].evidence_refs[0].frame_numbers == (31, 32)
    assert OpinionProtocolFigureV1.from_payload(format_figure.to_payload()) == format_figure
    base = _base_v5()
    opinion, citation_context = _approved_opinion(base)
    approved_with_figures = replace(
        opinion,
        protocol_figures=figures,
        content_sha256="",
    )
    v6 = augment_publication_with_opinion(base, approved_with_figures, citation_context)
    assert v6.provenance.opinion_content_sha256 == approved_with_figures.content_sha256
    assert v6.rendered_opinion is not None
    assert v6.rendered_opinion.protocol_figures == figures

    publication_bundle = load_template_bundle(_REPOSITORY_ROOT / "chpTypst")
    article_source = TypstRenderer().render(v6, publication_bundle)
    media_bytes: dict[UUID, bytes] = {}
    for reference in article_source.media_refs:
        try:
            media_bytes[reference.asset_id] = await media_store.read(reference.asset_id)
        except EntityNotFoundError:
            continue
    article_workspace = tmp_path / "protocol-article-workspace"
    _install_workspace(
        article_source,
        media_bytes,
        article_workspace,
        entrypoint="RENDERER/publication.typ",
    )
    article_pdf = await _compile_pdf(
        typst_binary=typst_binary,
        workspace_root=article_workspace,
        font_root=tmp_path / "article-fonts",
        entrypoint="RENDERER/publication.typ",
    )

    edition_bundle = load_template_bundle(
        _REPOSITORY_ROOT / "chpTypst", manifest_name="edition-renderer-manifest.json"
    )
    edition_source = EditionTypstRenderer().render(
        _edition_document(((1, v6), (2, v6))),
        edition_bundle,
    )
    for source in (article_source, edition_source):
        data = json.loads(source.render_data_bytes)
        sections = data.get("content_sections")
        if sections is None:
            sections = [
                section
                for publication in data["publications"]
                for section in publication["content_sections"]
            ]
        projected_figures = [
            figure for section in sections for figure in section.get("protocol_figures", ())
        ]
        assert projected_figures
        assert {figure["pin_sha256"] for figure in projected_figures} == {
            item.pin_sha256 for item in rendered
        }
        assert {figure["spec_sha256"] for figure in projected_figures} == {
            item.spec_sha256 for item in rendered
        }
        projected_format = next(
            figure for figure in projected_figures if figure["key"] == "protocol-message_format"
        )
        assert projected_format["field_evidence"][0]["field_id"] == "length"
        assert projected_format["field_evidence"][0]["evidence_refs"][0]["frame_numbers"] == [
            31,
            32,
        ]
    edition_media = {
        reference.asset_id: await media_store.read(reference.asset_id)
        for reference in edition_source.media_refs
        if await media_store.get(reference.asset_id) is not None
    }
    edition_workspace = tmp_path / "protocol-edition-workspace"
    _install_workspace(
        edition_source,
        edition_media,
        edition_workspace,
        entrypoint="RENDERER/edition.typ",
    )
    edition_pdf = await _compile_pdf(
        typst_binary=typst_binary,
        workspace_root=edition_workspace,
        font_root=tmp_path / "edition-fonts",
        entrypoint="RENDERER/edition.typ",
    )

    _persist_and_verify_pdf_outputs(
        Path("/tmp/autowork-protocol-l7"),
        rendered,
        article_pdf.content,
        edition_pdf.content,
    )


def test_protocol_figures_are_persisted_in_the_exact_opinion_version() -> None:
    from dataclasses import replace
    from types import SimpleNamespace

    rendered_payloads = []
    for index, kind in enumerate(ProtocolDiagramKind, start=1):
        rendered_payloads.append(
            {
                "kind": kind.value,
                "model_sha256": "a" * 64,
                "spec_sha256": str(index) * 64,
                "asset_id": str(UUID(int=index)),
                "asset_sha256": "b" * 64,
                "preview_sha256": "c" * 64,
                "pin_sha256": "d" * 64,
                "compiler_name": "d2",
                "compiler_version": "0.9.0",
                "compiler_policy_version": "protocol-test-v1",
                "caption": f"{kind.value} synthetic view with status legend",
            }
        )
    figures = tuple(OpinionProtocolFigureV1.from_payload(item) for item in rendered_payloads)
    base = _base_v5()
    opinion, _context = _approved_opinion(base)
    with_figures = replace(opinion, protocol_figures=figures, content_sha256="")
    row = SimpleNamespace(
        opinion_id=with_figures.id,
        version=with_figures.version,
        investigation_id=with_figures.investigation_id,
        content=with_figures.to_payload(),
        content_sha256=with_figures.content_sha256,
        status=with_figures.status.value,
    )

    loaded = _opinion_from_row(row)

    assert loaded.protocol_figures == figures
    assert loaded.content_sha256 == with_figures.content_sha256
    assert "protocol_figures" not in opinion.content_payload()
    assert with_figures.content_payload()["protocol_figures"] == [
        figure.to_payload() for figure in figures
    ]
