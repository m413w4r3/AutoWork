from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from cti_app.application import publication_rendering
from cti_app.application.analyst_opinion_preview import AnalystOpinionPreviewService
from cti_app.application.analyst_opinion_service import (
    OpinionCompositionSelection,
    OpinionError,
    OpinionErrorCode,
)
from cti_app.application.media_assets import MediaAssetStore
from cti_app.application.opinion_ioc_csv_export import build_opinion_ioc_csv_exports
from cti_app.application.publication_rendering import PublicationRenderService
from cti_app.application.typst_compilation import (
    CompiledTypstDocument,
    FontBundleSnapshot,
    TypstCompileRequest,
)
from cti_app.application.typst_rendering import (
    TemplateFile,
    TypstRenderer,
    TypstTemplateBundle,
)
from cti_app.domain.analyst_opinion import AnalystOpinionStatus, AnalystOpinionV1
from cti_app.domain.investigation_graph import InvestigationGraph
from tests.test_analyst_opinion_service import OpinionHarness, make_opinion_harness


class _NoCompile:
    def __init__(self) -> None:
        self.calls = 0

    async def compile(self, request: TypstCompileRequest) -> CompiledTypstDocument:
        del request
        self.calls += 1
        raise AssertionError("render-input preview must not compile a PDF")


class _NoGraph:
    async def project(self, **kwargs: Any) -> InvestigationGraph | None:
        del kwargs
        return None


def _preview_service(
    harness: OpinionHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[AnalystOpinionPreviewService, _NoCompile]:
    template_bundle = TypstTemplateBundle(
        template_version="fixture-v1",
        sha256="a" * 64,
        files=(TemplateFile("RENDERER/publication.typ", b"fixture template"),),
    )
    font_bundle = FontBundleSnapshot(font_bundle_version="fixture-v1", files=())
    monkeypatch.setattr(
        publication_rendering,
        "load_typst_render_bundle_snapshots",
        lambda **_kwargs: (template_bundle, font_bundle),
    )
    compiler = _NoCompile()
    renderer = PublicationRenderService(
        uow_factory=harness.uow_factory,  # type: ignore[arg-type]
        artifact_store=harness.store,  # type: ignore[arg-type]
        media_asset_store=MediaAssetStore(
            harness.store,  # type: ignore[arg-type]
            harness.uow_factory,  # type: ignore[arg-type]
        ),
        renderer=TypstRenderer(),
        compiler=compiler,
        chp_typst_root=Path("/unused"),
        font_bundle_root=Path("/unused"),
        typst_fonts_lock_path=Path("/unused"),
    )
    return (
        AnalystOpinionPreviewService(
            uow_factory=harness.uow_factory,  # type: ignore[arg-type]
            artifact_store=harness.store,  # type: ignore[arg-type]
            renderer=renderer,
            graph_source=_NoGraph(),  # type: ignore[arg-type]
        ),
        compiler,
    )


async def _draft(harness: OpinionHarness) -> AnalystOpinionV1:
    return await harness.service.compose_from_findings(
        harness.investigation.id,
        OpinionCompositionSelection(),
    )


@pytest.mark.asyncio
async def test_preview_render_inputs_feed_ioc_csv_export_without_compiling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = make_opinion_harness()
    opinion = await _draft(harness)
    preview, compiler = _preview_service(harness, monkeypatch)
    versions_before = tuple(harness.uow.analyst_opinions.items[opinion.id])
    artifacts_before = tuple(harness.artifacts.items)

    publication_document, render_data = await preview.preview_render_inputs(
        opinion.id,
        version=opinion.version,
        expected_opinion_sha256=opinion.content_sha256,
        expected_base_publication_sha256=opinion.base_publication_sha256,
    )

    exports = build_opinion_ioc_csv_exports(publication_document, render_data)
    assert set(exports) == {
        "LISEZ-MOI.md",
        "ioc_pipeline_article.csv",
        "ioc_fichiers.csv",
        "ioc_reseau.csv",
        "noms_et_chemins_de_fichiers.csv",
        "pistes_non_confirmees.csv",
        "ioc_exclus.csv",
    }
    assert render_data["content_sections"]
    assert compiler.calls == 0
    assert tuple(harness.uow.analyst_opinions.items[opinion.id]) == versions_before
    assert tuple(harness.artifacts.items) == artifacts_before


@pytest.mark.asyncio
async def test_preview_render_inputs_reject_stale_opinion_and_base_hashes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = make_opinion_harness()
    opinion = await _draft(harness)
    preview, compiler = _preview_service(harness, monkeypatch)

    with pytest.raises(OpinionError) as stale_opinion:
        await preview.preview_render_inputs(
            opinion.id,
            version=opinion.version,
            expected_opinion_sha256="0" * 64,
            expected_base_publication_sha256=opinion.base_publication_sha256,
        )
    assert stale_opinion.value.code is OpinionErrorCode.STALE_VERSION

    with pytest.raises(OpinionError) as stale_base:
        await preview.preview_render_inputs(
            opinion.id,
            version=opinion.version,
            expected_opinion_sha256=opinion.content_sha256,
            expected_base_publication_sha256="0" * 64,
        )
    assert stale_base.value.code is OpinionErrorCode.STALE_BASE
    assert compiler.calls == 0


@pytest.mark.asyncio
async def test_preview_render_inputs_reject_non_draft_opinion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = make_opinion_harness()
    opinion = await _draft(harness)
    approved = replace(
        opinion,
        status=AnalystOpinionStatus.APPROVED,
        reviewer_id="reviewer:fixture",
        content_sha256="",
    )
    harness.uow.analyst_opinions.items[opinion.id][-1] = approved
    preview, compiler = _preview_service(harness, monkeypatch)

    with pytest.raises(OpinionError) as not_draft:
        await preview.preview_render_inputs(
            approved.id,
            version=approved.version,
            expected_opinion_sha256=approved.content_sha256,
            expected_base_publication_sha256=approved.base_publication_sha256,
        )
    assert not_draft.value.code is OpinionErrorCode.OPINION_NOT_DRAFT
    assert compiler.calls == 0
