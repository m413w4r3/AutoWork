from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID

import pytest

from cti_app.application.edition_typst_rendering import (
    EditionRenderPublicationSchemaUnsupportedError,
    EditionTypstRenderer,
)
from cti_app.application.typst_compilation import (
    CompiledTypstDocument,
    FontBundleSnapshot,
    TypstCompileRequest,
    TypstOutputInvalidError,
)
from cti_app.application.typst_render_execution import TypstRenderExecutor
from cti_app.application.typst_rendering import (
    TemplateBundleInvalidError,
    TemplateFile,
    TypstRenderer,
    TypstRenderSource,
    TypstTemplateBundle,
    compute_template_bundle_hash,
    load_template_bundle,
    project_publication_to_typst_model,
)
from cti_app.domain.edition_publication import EditionDocumentV2, EditionPublicationV2
from cti_app.domain.production_editorial_enrichment import EnrichmentPlacementKind
from cti_app.domain.publication_document import (
    CanonicalPublicationDocument,
    PublicationDocumentV4,
)
from tests.test_typst_rendering import (
    _INJECTION_TEXT,
    _diagram_at,
    _figure_at,
    _full_document,
    _table_at,
)

_CHP_TYPST_ROOT = Path(__file__).resolve().parents[2] / "chpTypst"
_EDITION_MANIFEST = "edition-renderer-manifest.json"
_EDITION_ID = UUID("11111111-1111-4111-8111-111111111111")


def _edition_metadata() -> dict[str, object]:
    return {
        "id": str(_EDITION_ID),
        "country": "France",
        "country_code": "FR",
        "period_start": "2026-08-01",
        "period_end": "2026-08-31",
        "tlp": "TLP:GREEN",
        "languages": ["fr"],
        "state": "open",
        "version": 4,
        "created_at": "2026-08-01T00:00:00+00:00",
        "updated_at": "2026-08-02T00:00:00+00:00",
    }


def _publication(title: str, subject_id: int) -> PublicationDocumentV4:
    return replace(
        _full_document(),
        title=title,
        subject_id=UUID(int=subject_id),
    )


def _edition_document(
    publications: tuple[tuple[int, CanonicalPublicationDocument], ...],
) -> EditionDocumentV2:
    return EditionDocumentV2(
        edition=_edition_metadata(),
        publications=tuple(
            EditionPublicationV2(
                position=position,
                subject_id=publication.subject_id,
                document=publication,
            )
            for position, publication in publications
        ),
    )


def _edition_bundle(tmp_path: Path) -> TypstTemplateBundle:
    entrypoint = tmp_path / "RENDERER" / "edition.typ"
    entrypoint.parent.mkdir(parents=True)
    entrypoint.write_bytes(b'#let edition = json("edition-render-data.json")\n')
    (tmp_path / _EDITION_MANIFEST).write_text(
        json.dumps(
            {
                "template_version": "test-edition-v1",
                "files": ["RENDERER/edition.typ"],
            }
        ),
        encoding="utf-8",
    )
    return load_template_bundle(tmp_path, manifest_name=_EDITION_MANIFEST)


def _render_data(
    tmp_path: Path, document: EditionDocumentV2
) -> tuple[TypstRenderSource, dict[str, Any]]:
    bundle = _edition_bundle(tmp_path)
    source = EditionTypstRenderer().render(document, bundle)
    return source, json.loads(source.render_data_bytes)


def test_renders_one_article_with_edition_metadata_and_private_entrypoint(tmp_path: Path) -> None:
    document = _edition_document(((1, _publication("Single article", 1)),))

    source, data = _render_data(tmp_path, document)

    assert source.entrypoint_relative_path == "RENDERER/edition.typ"
    assert source.render_data_relative_path == "RENDERER/edition-render-data.json"
    assert data["schema_version"] == "typst-edition-model-v1"
    assert data["edition"] == {
        "id": str(_EDITION_ID),
        "country": "France",
        "country_code": "FR",
        "period_start": "2026-08-01",
        "period_end": "2026-08-31",
        "tlp": "TLP:GREEN",
        "languages": ["fr"],
        "version": 4,
    }
    assert [item["title"] for item in data["publications"]] == ["Single article"]


def test_orders_two_and_three_articles_by_position_not_title_or_input_order(
    tmp_path: Path,
) -> None:
    document = _edition_document(
        (
            (3, _publication("Mike", 3)),
            (1, _publication("Zeta", 1)),
            (2, _publication("Alpha", 2)),
        )
    )

    _, data = _render_data(tmp_path, document)

    assert [(item["position"], item["title"]) for item in data["publications"]] == [
        (1, "Zeta"),
        (2, "Alpha"),
        (3, "Mike"),
    ]


def test_shared_projection_covers_publication_content_and_typst_syntax_as_data(
    tmp_path: Path,
) -> None:
    document = _full_document(
        tables=(_table_at("table", "Observed commands", EnrichmentPlacementKind.AFTER_LEAD),),
        diagrams=(_diagram_at("diagram", "Command flow", EnrichmentPlacementKind.END),),
        figures=(_figure_at("figure", "Source capture", EnrichmentPlacementKind.END),),
    )
    edition = _edition_document(((1, document),))

    source, data = _render_data(tmp_path, edition)
    publication = data["publications"][0]
    projected = project_publication_to_typst_model(document)
    body_types = [block["type"] for block in publication["body_blocks"]]

    assert publication["title"] == "Intrusion report"
    assert publication["timeline"][0]["text"] == "No display date"
    assert publication["body_blocks"][0]["text"] == _INJECTION_TEXT
    assert "section_heading" in body_types
    assert "table" in body_types
    assert "diagram" in body_types
    assert "figure" in body_types
    assert publication["indicators"]["ips"] == ["Display ip"]
    assert publication["uncertainties"] == ["Attribution remains uncertain"]
    assert publication["sources"][0]["url"] == "https://example.test/one"
    assert len(source.media_refs) == 2
    assert json.loads(source.render_data_bytes)["publications"][0]["body_blocks"][0]["text"] == (
        _INJECTION_TEXT
    )
    assert source.source_bytes == b'#let edition = json("edition-render-data.json")\n'
    assert source.source_bytes != _INJECTION_TEXT.encode()
    assert publication["body_blocks"] == projected.body_blocks
    assert publication["timeline"] == projected.timeline


def test_shared_and_distinct_media_are_deduplicated_in_first_use_order(
    tmp_path: Path,
) -> None:
    common_id = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
    distinct_id = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
    common_diagram_a = _diagram_at(
        "common_a", "Common A", EnrichmentPlacementKind.AFTER_LEAD, asset_id=common_id
    )
    common_diagram_b = _diagram_at(
        "common_b", "Common B", EnrichmentPlacementKind.AFTER_LEAD, asset_id=common_id
    )
    distinct_figure = _figure_at(
        "distinct", "Distinct figure", EnrichmentPlacementKind.END, asset_id=distinct_id
    )
    first = replace(_full_document(diagrams=(common_diagram_a,)), title="First")
    second = replace(
        _full_document(diagrams=(common_diagram_b,), figures=(distinct_figure,)),
        title="Second",
    )
    document = _edition_document(((1, first), (2, second)))

    source, _ = _render_data(tmp_path, document)

    assert len(json.loads(source.render_data_bytes)["publications"]) == 2
    assert [media.asset_id for media in source.media_refs] == [common_id, distinct_id]


def test_render_data_bytes_are_deterministic_across_runs(tmp_path: Path) -> None:
    document = _edition_document(
        (
            (2, _publication("Second", 2)),
            (1, _publication("First", 1)),
        )
    )
    renderer = EditionTypstRenderer()
    bundle = _edition_bundle(tmp_path)

    first = renderer.render(document, bundle)
    second = renderer.render(document, bundle)

    assert first.render_data_bytes == second.render_data_bytes
    assert first.render_data_sha256 == second.render_data_sha256


def test_v3_publication_is_rejected_with_stable_code(tmp_path: Path) -> None:
    legacy_document = cast(
        CanonicalPublicationDocument,
        SimpleNamespace(schema_version="3", subject_id=UUID(int=99)),
    )
    edition = _edition_document(((1, legacy_document),))

    with pytest.raises(EditionRenderPublicationSchemaUnsupportedError) as raised:
        EditionTypstRenderer().render(edition, _edition_bundle(tmp_path))

    assert raised.value.code == "edition_render_publication_schema_unsupported"


def test_template_bundle_loader_accepts_edition_manifest_deterministically(
    tmp_path: Path,
) -> None:
    bundle = _edition_bundle(tmp_path)

    assert bundle.template_version == "test-edition-v1"
    assert bundle.sha256 == load_template_bundle(tmp_path, manifest_name=_EDITION_MANIFEST).sha256
    assert compute_template_bundle_hash(tmp_path, manifest_name=_EDITION_MANIFEST) == (
        bundle.template_version,
        bundle.sha256,
    )


def test_edition_manifest_rejects_parent_traversal(tmp_path: Path) -> None:
    (tmp_path / "edition-renderer-manifest.json").write_text(
        json.dumps({"template_version": "bad", "files": ["../outside.typ"]}),
        encoding="utf-8",
    )

    with pytest.raises(TemplateBundleInvalidError, match="escapes the bundle"):
        load_template_bundle(tmp_path, manifest_name=_EDITION_MANIFEST)


def test_publication_and_edition_bundle_hashes_are_independent(tmp_path: Path) -> None:
    root = tmp_path / "chpTypst"
    renderer = root / "RENDERER"
    renderer.mkdir(parents=True)
    (renderer / "publication.typ").write_bytes(b"publication")
    (renderer / "edition.typ").write_bytes(b"edition")
    publication_manifest = root / "renderer-manifest.json"
    edition_manifest = root / _EDITION_MANIFEST
    publication_manifest.write_text(
        json.dumps({"template_version": "publication-v1", "files": ["RENDERER/publication.typ"]}),
        encoding="utf-8",
    )
    edition_manifest.write_text(
        json.dumps({"template_version": "edition-v1", "files": ["RENDERER/edition.typ"]}),
        encoding="utf-8",
    )
    publication_before = compute_template_bundle_hash(root)
    edition_before = compute_template_bundle_hash(root, manifest_name=_EDITION_MANIFEST)

    (renderer / "publication-extra.typ").write_bytes(b"publication extra")
    publication_manifest.write_text(
        json.dumps(
            {
                "template_version": "publication-v2",
                "files": ["RENDERER/publication.typ", "RENDERER/publication-extra.typ"],
            }
        ),
        encoding="utf-8",
    )
    publication_after = compute_template_bundle_hash(root)
    assert publication_after[1] != publication_before[1]
    assert compute_template_bundle_hash(root, manifest_name=_EDITION_MANIFEST) == edition_before

    (renderer / "edition-extra.typ").write_bytes(b"edition extra")
    edition_manifest.write_text(
        json.dumps(
            {
                "template_version": "edition-v2",
                "files": ["RENDERER/edition.typ", "RENDERER/edition-extra.typ"],
            }
        ),
        encoding="utf-8",
    )
    edition_after = compute_template_bundle_hash(root, manifest_name=_EDITION_MANIFEST)
    assert edition_after[1] != edition_before[1]
    assert compute_template_bundle_hash(root) == publication_after


class _NoMediaStore:
    async def get(self, asset_id: UUID) -> None:
        del asset_id
        return None

    async def read(self, asset_id: UUID) -> bytes:
        del asset_id
        raise AssertionError("no media should be resolved")


class _FakeCompiler:
    def __init__(self, content: bytes = b"%PDF-executor-test") -> None:
        self.content = content
        self.request: TypstCompileRequest | None = None
        self.workspace_files: dict[str, bytes] = {}

    async def compile(self, request: TypstCompileRequest) -> CompiledTypstDocument:
        self.request = request
        self.workspace_files = {
            path.relative_to(request.workspace_root).as_posix(): path.read_bytes()
            for path in request.workspace_root.rglob("*")
            if path.is_file()
        }
        return CompiledTypstDocument(
            media_type="application/pdf",
            content=self.content,
            sha256=hashlib.sha256(self.content).hexdigest(),
            byte_size=len(self.content),
            compiler="typst",
            compiler_version="0.15.1",
        )


@pytest.mark.asyncio
async def test_typst_render_executor_materializes_inputs_and_validates_pdf(
    tmp_path: Path,
) -> None:
    compiler = _FakeCompiler()
    executor = TypstRenderExecutor(media_asset_store=_NoMediaStore(), compiler=compiler)
    source = TypstRenderSource(
        source_bytes=b"#edition",
        source_sha256=hashlib.sha256(b"#edition").hexdigest(),
        render_data_bytes=b'{"schema_version":"test"}',
        render_data_sha256=hashlib.sha256(b'{"schema_version":"test"}').hexdigest(),
        media_refs=(),
        entrypoint_relative_path="RENDERER/edition.typ",
        render_data_relative_path="RENDERER/edition-render-data.json",
    )
    bundle = TypstTemplateBundle(
        template_version="test-v1",
        sha256="a" * 64,
        files=(TemplateFile("RENDERER/edition.typ", b"#edition"),),
    )
    fonts = FontBundleSnapshot("font-v1", (("TestFont/test.ttf", b"font bytes"),))

    executed = await executor.execute(
        render_source=source,
        template_bundle=bundle,
        font_bundle=fonts,
    )

    assert compiler.request is not None
    assert compiler.request.entrypoint_relative_path == "RENDERER/edition.typ"
    assert compiler.workspace_files["RENDERER/edition-render-data.json"] == source.render_data_bytes
    assert executed.compiled_document.content == compiler.content
    assert executed.resolved_media == {}


@pytest.mark.asyncio
async def test_typst_render_executor_rejects_invalid_fake_compiler_pdf() -> None:
    compiler = _FakeCompiler(content=b"not a PDF")
    executor = TypstRenderExecutor(media_asset_store=_NoMediaStore(), compiler=compiler)
    source = TypstRenderSource(
        source_bytes=b"#publication",
        source_sha256=hashlib.sha256(b"#publication").hexdigest(),
        render_data_bytes=b"{}",
        render_data_sha256=hashlib.sha256(b"{}").hexdigest(),
        media_refs=(),
    )
    bundle = TypstTemplateBundle(
        template_version="test-v1",
        sha256="a" * 64,
        files=(TemplateFile("RENDERER/publication.typ", b"#publication"),),
    )

    with pytest.raises(TypstOutputInvalidError):
        await executor.execute(
            render_source=source,
            template_bundle=bundle,
            font_bundle=FontBundleSnapshot("font-v1", (("font.ttf", b"font"),)),
        )


def test_shared_publication_projection_is_pure_and_complete(tmp_path: Path) -> None:
    document = _full_document()
    publication_bundle = TypstTemplateBundle(
        template_version="publication-v1",
        sha256="b" * 64,
        files=(TemplateFile("RENDERER/publication.typ", b"#publication"),),
    )

    model = project_publication_to_typst_model(document)
    rendered = TypstRenderer().render(document, publication_bundle)
    render_data = json.loads(rendered.render_data_bytes)

    assert model.title == render_data["title"]
    assert model.timeline == render_data["timeline"]
    assert model.body_blocks == render_data["body_blocks"]
    assert model.indicators == render_data["indicators"]
    assert model.uncertainties == render_data["uncertainties"]
    assert model.sources == render_data["sources"]
    assert model.media_refs == rendered.media_refs


def test_repository_edition_manifest_snapshots_expected_assets() -> None:
    bundle = load_template_bundle(_CHP_TYPST_ROOT, manifest_name=_EDITION_MANIFEST)

    assert bundle.template_version == "chp-edition-v1"
    assert {file.relative_path for file in bundle.files} == {
        "RENDERER/edition.typ",
        "RENDERER/edition_helpers.typ",
        "RENDERER/publication_helpers.typ",
        "UTILS/document_style.typ",
        "UTILS/helpers.typ",
        "UTILS/colors.typ",
        "UTILS/header_footer.typ",
        "UTILS/chap.png",
    }
