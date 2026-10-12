"""FULL proof: real PostgreSQL seed, Composition, Review, Acceptance, Assembly,
Typst PDF compilation, Poppler inspection and text extraction. A successful compile
proves compiler execution and the asserted PDF contents; it does not prove visual
quality or editorial quality.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from cti_app.application.edition_rendering import (
    EDITION_RENDERER_MANIFEST,
    EditionRenderService,
)
from cti_app.application.edition_typst_rendering import EditionTypstRenderer
from cti_app.application.media_assets import MediaAssetStore
from cti_app.application.pdf_inspection import PdfInspectionService
from cti_app.application.persistence import UnitOfWorkFactory
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.application.typst_paths import typst_bundle_paths
from cti_app.application.typst_render_execution import load_typst_render_bundle_snapshots
from cti_app.domain.typst_render import TypstRenderStatus
from cti_app.infrastructure.database.repositories.pdf_inspection import (
    SqlAlchemyPdfInspectionSourceRepository,
)
from cti_app.infrastructure.database.session import create_postgres_engine, create_session_factory
from cti_app.infrastructure.pdf_inspection import PdfInspectionCache, PopplerSubprocess
from cti_app.infrastructure.typst_compiler import TypstSubprocessCompiler
from tests.integration.test_edition_multibatch_publication_postgres import _compose_three_runs

from . import invariants
from .driver import WorkflowDriver

pytestmark = [pytest.mark.integration, pytest.mark.workflow_full]


def _render_roots_exist(template_root: Path, font_root: Path, fonts_lock: Path) -> bool:
    return template_root.is_dir() and font_root.is_dir() and fonts_lock.is_file()


def _extract_with_pdftotext(pdf_bytes: bytes, temp_root: Path) -> str:
    executable = shutil.which("pdftotext")
    if executable is None:
        raise AssertionError("W9-real-typst-render / inspect_real_pdf: pdftotext is required")
    temp_root.mkdir(parents=True, exist_ok=True)
    pdf_path = temp_root / "edition.pdf"
    pdf_path.write_bytes(pdf_bytes)
    result = subprocess.run(
        [executable, str(pdf_path), "-"],
        check=True,
        capture_output=True,
        timeout=15,
        env={**os.environ, "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"},
    )
    return result.stdout.decode("utf-8", errors="replace")


@pytest.mark.asyncio
async def test_w9_real_typst_render_contains_frozen_article_titles(
    workflow_driver: WorkflowDriver,
    uow_factory: UnitOfWorkFactory,
    migrated_postgres_url: str,
    tmp_path: Path,
) -> None:
    """Compile and inspect a release PDF with the real pinned Typst and Poppler tools."""
    driver = workflow_driver
    driver.begin("W9-real-typst-render")
    async with driver:
        typst_binary = os.environ.get("TYPST_BINARY", "typst")
        binary_path = shutil.which(typst_binary)
        default_template_root, default_font_root, fonts_lock = typst_bundle_paths()
        template_root = Path(os.environ.get("CHP_TYPST_ROOT", str(default_template_root)))
        font_root = Path(os.environ.get("FONT_BUNDLE_ROOT", str(default_font_root)))
        inspection_processors: list[PopplerSubprocess] = []

        async def check_render_prerequisites() -> tuple[str, ...]:
            invariants.require(
                binary_path is not None,
                driver.scenario,
                "typst_prerequisites",
                "real Typst binary is required; install Typst 0.15.1 and set TYPST_BINARY "
                "or put it on PATH",
            )
            paths_exist = await asyncio.to_thread(
                _render_roots_exist, template_root, font_root, fonts_lock
            )
            invariants.require(
                paths_exist,
                driver.scenario,
                "typst_prerequisites",
                "real edition template, font bundle and typst-fonts.lock are required; "
                "check CHP_TYPST_ROOT, FONT_BUNDLE_ROOT and TYPST_FONTS_LOCK_FILE",
            )
            try:
                template, fonts = load_typst_render_bundle_snapshots(
                    chp_typst_root=template_root,
                    font_bundle_root=font_root,
                    typst_fonts_lock_path=fonts_lock,
                    manifest_name=EDITION_RENDERER_MANIFEST,
                )
            except Exception as exc:
                raise AssertionError(
                    f"W9-real-typst-render / typst_prerequisites: cannot load real "
                    f"Typst template/font snapshots: {exc}"
                ) from exc
            try:
                processor = PopplerSubprocess(temp_root=tmp_path / "poppler")
            except Exception as exc:
                raise AssertionError(
                    f"W9-real-typst-render / typst_prerequisites: Poppler inspection tools "
                    f"are required: {exc}"
                ) from exc
            inspection_processors.append(processor)
            return (
                f"typst-binary:{binary_path}",
                f"template-sha256:{template.sha256}",
                f"font-files:{len(fonts.files)}",
                f"poppler-build:{processor.build}",
            )

        await driver._perform(
            "typst_prerequisites",
            check_render_prerequisites,
            via="service",
            action_name="check real Typst, template, fonts and Poppler prerequisites",
            identities={"binary": typst_binary},
            assertions=("missing-typst-or-fonts-fail-explicitly",),
        )

        async def seed_release_inputs() -> tuple[
            Any, ProductionArtifactStore, dict[str, Any], dict[str, Any]
        ]:
            return await _compose_three_runs(
                uow_factory,
                tmp_path,
                production_order=("A", "B", "C"),
                composition_order=(),
            )

        edition, artifact_store, subjects, batches = await driver._perform(
            "seed_ready_runs",
            seed_release_inputs,
            via="fixture",
            action_name="_compose_three_runs with minimal valid Article A/B/C documents",
            assertions=("postgres-ready-runs", "verified-publication-blobs"),
        )
        driver.edition_id = edition.id
        driver.set_artifact_store(artifact_store)
        await driver.record_seed(
            {label: subjects[label].id for label in ("A", "B", "C")},
            mechanism=(
                "_compose_three_runs: READY rows, VERIFIED canonical publications, "
                "synthetic article titles"
            ),
        )
        await driver.compose(
            (subjects["A"].id, subjects["B"].id, subjects["C"].id),
            provenance_batch_ids={subjects[label].id: batches[label] for label in ("A", "B", "C")},
        )
        await driver.review(expect_can_accept=True)
        await driver.validate()
        accepted = await driver.accept()
        release = await driver.assemble()

        render_service = EditionRenderService(
            uow_factory=uow_factory,
            artifact_store=artifact_store,
            media_asset_store=MediaAssetStore(artifact_store, uow_factory),
            renderer=EditionTypstRenderer(),
            compiler=TypstSubprocessCompiler(binary=typst_binary),
            chp_typst_root=template_root,
            font_bundle_root=font_root,
            typst_fonts_lock_path=fonts_lock,
        )

        async def render_edition() -> Any:
            return await render_service.render_pdf(release.id)

        rendered = await driver._perform(
            "render_real_pdf",
            render_edition,
            via="service",
            action_name="EditionRenderService.render_pdf with TypstSubprocessCompiler",
            identities={"release": release.id, "manifest": release.manifest_id},
            assertions=("real-typst-subprocess", "pdf-render-persisted-in-postgresql"),
        )

        engine = create_postgres_engine(migrated_postgres_url)
        source_repository = SqlAlchemyPdfInspectionSourceRepository(create_session_factory(engine))
        inspector = PdfInspectionService(
            source_repository,
            artifact_store,
            inspection_processors[0],
            PdfInspectionCache(tmp_path / "pdf-inspection-cache"),
        )

        async def inspect_release_pdf() -> tuple[str, ...]:
            try:
                invariants.require(
                    rendered.status is TypstRenderStatus.SUCCEEDED
                    and rendered.output_blob_id is not None
                    and rendered.output_sha256 is not None
                    and rendered.output_byte_size is not None,
                    driver.scenario,
                    "inspect_real_pdf",
                    f"real render did not persist a successful PDF identity: {rendered!r}",
                )
                assert rendered.output_blob_id is not None
                assert rendered.output_sha256 is not None
                pdf_bytes = await artifact_store.read_bytes(rendered.output_blob_id)
                observed_sha256 = hashlib.sha256(pdf_bytes).hexdigest()
                async with uow_factory() as uow:
                    persisted_render = await uow.edition_renders.get(rendered.id)
                    persisted_release = await uow.edition_releases.get(release.id)
                    manifest = await uow.publication_manifests.get(accepted.manifest.id)
                    manifest_entries = await uow.publication_manifest_entries.list_for_manifest(
                        accepted.manifest.id
                    )
                    output_blob = await uow.blobs.get(rendered.output_blob_id)
                invariants.require(
                    persisted_render == rendered
                    and persisted_render is not None
                    and persisted_render.edition_release_id == release.id
                    and persisted_release is not None
                    and persisted_release.manifest_id == accepted.manifest.id
                    and manifest is not None
                    and manifest.content_sha256 == accepted.manifest.content_sha256
                    and output_blob is not None
                    and output_blob.descriptor.sha256 == observed_sha256
                    and observed_sha256 == rendered.output_sha256
                    and rendered.output_byte_size == len(pdf_bytes) > 0,
                    driver.scenario,
                    "inspect_real_pdf",
                    "render, release, manifest, output blob size or SHA-256 differ in PostgreSQL",
                )
                assert persisted_release is not None
                invariants.require(
                    {entry.subject_id for entry in manifest_entries}
                    == {subjects["A"].id, subjects["B"].id, subjects["C"].id},
                    driver.scenario,
                    "inspect_real_pdf",
                    "frozen manifest does not contain all three seeded publication subjects",
                )
                document_bytes = await artifact_store.read_bytes(
                    persisted_release.edition_document_blob_id
                )
                invariants.require(
                    hashlib.sha256(document_bytes).hexdigest()
                    == persisted_release.edition_document_sha256,
                    driver.scenario,
                    "inspect_real_pdf",
                    "release document bytes do not match the frozen document SHA-256",
                )
                source = await source_repository.find_by_render_id(rendered.id)
                invariants.require(
                    source is not None
                    and source.output_sha256 == observed_sha256
                    and source.source.get("kind") == "edition_release"
                    and source.source.get("release_id") == str(release.id)
                    and source.source.get("manifest_id") == str(accepted.manifest.id)
                    and source.source.get("manifest_sha256") == accepted.manifest.content_sha256
                    and source.source.get("edition_document_sha256")
                    == persisted_release.edition_document_sha256
                    and len(source.source.get("artifacts", [])) == 3,
                    driver.scenario,
                    "inspect_real_pdf",
                    "render provenance does not link the same release, manifest, "
                    "document and artifacts",
                )
                inspection = await inspector.metadata(observed_sha256)
                pdf_record = inspection["pdf"]
                diagnostics = inspection["diagnostics"]
                blocking_codes = {
                    "pdf_unreadable",
                    "pdf_encrypted",
                    "page_count_limit_exceeded",
                }
                found_codes = {item["code"] for item in diagnostics["items"]}
                invariants.require(
                    pdf_record["page_count"] > 0
                    and diagnostics["status"] == "ready"
                    and blocking_codes.isdisjoint(found_codes)
                    and any(
                        item["code"] == "pdf_integrity_verified" for item in diagnostics["items"]
                    ),
                    driver.scenario,
                    "inspect_real_pdf",
                    f"Poppler inspection is blocked or unreadable: {diagnostics!r}",
                )
                extracted_text = await asyncio.to_thread(
                    _extract_with_pdftotext,
                    pdf_bytes,
                    tmp_path / "pdftotext",
                )
                invariants.require(
                    all(f"Article {label}" in extracted_text for label in ("A", "B")),
                    driver.scenario,
                    "inspect_real_pdf",
                    "pdftotext/PdfReader did not extract both frozen article titles",
                )
                source_items = inspection["source"]
                invariants.require(
                    len(source_items) == 1
                    and source_items[0]["release_id"] == str(release.id)
                    and source_items[0]["manifest_id"] == str(accepted.manifest.id)
                    and source_items[0]["manifest_sha256"] == accepted.manifest.content_sha256
                    and source_items[0]["edition_document_sha256"]
                    == persisted_release.edition_document_sha256,
                    driver.scenario,
                    "inspect_real_pdf",
                    "PDF inspection provenance does not expose the frozen release chain",
                )
                return (
                    f"pdf-bytes:{len(pdf_bytes)}",
                    f"pdf-sha256:{observed_sha256}",
                    f"pages:{pdf_record['page_count']}",
                    "poppler-diagnostics-no-blocking-errors",
                    "article-a-and-b-titles-extracted-from-pdf",
                    "manifest-release-document-render-identities-match",
                )
            finally:
                await engine.dispose()

        await driver._perform(
            "inspect_real_pdf",
            inspect_release_pdf,
            via="service",
            action_name=(
                "PdfInspectionService + Poppler + extracted PDF text + PostgreSQL provenance"
            ),
            identities={
                "render": rendered.id,
                "release": release.id,
                "manifest": accepted.manifest.id,
            },
            assertions=("pdf-blob-hash-and-pages-verified", "both-article-titles-present"),
        )
