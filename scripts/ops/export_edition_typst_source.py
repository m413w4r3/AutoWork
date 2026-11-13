#!/usr/bin/env python3
"""Export the exact Typst workspace of an edition preview (read-only; nothing is persisted).

Runs the real preview pipeline once and copies the private workspace (templates, render data,
media) just before compilation, then writes the PDF the compiler produced so the export can be
checked against a delivered bulletin. Run it inside the backend container.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import shutil
import sys
from pathlib import Path
from uuid import UUID

from cti_app.application import typst_render_execution
from cti_app.application.blobs import BlobCatalogService
from cti_app.application.edition_preview import EditionPreviewPdfRenderer, EditionPreviewService
from cti_app.application.edition_typst_rendering import EditionTypstRenderer
from cti_app.application.media_assets import MediaAssetStore
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.application.typst_paths import typst_bundle_paths
from cti_app.config import Settings
from cti_app.infrastructure.blob_storage.minio import MinioBlobStore
from cti_app.infrastructure.database.session import create_postgres_engine, create_session_factory
from cti_app.infrastructure.database.uow import SqlAlchemyUnitOfWork
from cti_app.infrastructure.typst_compiler import TypstSubprocessCompiler
from minio import Minio


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--edition-id", required=True, type=UUID)
    parser.add_argument("--output-dir", required=True, type=Path)
    return parser


async def _run(args: argparse.Namespace) -> int:
    settings = Settings(_env_file=None)
    engine = create_postgres_engine(settings.postgres_dsn)
    session_factory = create_session_factory(engine)

    def uow_factory() -> SqlAlchemyUnitOfWork:
        return SqlAlchemyUnitOfWork(session_factory)

    blob_store = MinioBlobStore(
        Minio(
            settings.s3_endpoint,
            access_key=settings.s3_access_key,
            secret_key=settings.s3_secret_key,
            secure=settings.s3_secure,
        ),
        physical_bucket=settings.s3_bucket,
    )
    artifact_store = ProductionArtifactStore(BlobCatalogService(blob_store, uow_factory))
    media_asset_store = MediaAssetStore(artifact_store, uow_factory)
    chp_typst_root, font_bundle_root, typst_fonts_lock_path = typst_bundle_paths()
    pdf_renderer = EditionPreviewPdfRenderer(
        renderer=EditionTypstRenderer(),
        media_asset_store=media_asset_store,
        compiler=TypstSubprocessCompiler(),
        chp_typst_root=chp_typst_root,
        font_bundle_root=font_bundle_root,
        typst_fonts_lock_path=typst_fonts_lock_path,
    )
    service = EditionPreviewService(uow_factory, artifact_store, pdf_renderer=pdf_renderer)

    output = args.output_dir
    workspace_copy = output / "workspace"
    if workspace_copy.exists():
        shutil.rmtree(workspace_copy)
    output.mkdir(parents=True, exist_ok=True)

    executor_class = typst_render_execution.TypstRenderExecutor
    original = executor_class._populate_workspace

    def capture(workspace_root, template_bundle, render_source, resolved_media):  # type: ignore[no-untyped-def]
        original(workspace_root, template_bundle, render_source, resolved_media)
        shutil.copytree(workspace_root, workspace_copy)

    executor_class._populate_workspace = staticmethod(capture)  # type: ignore[method-assign]
    try:
        preview = await service.preview(args.edition_id)
        pdf = await service.preview_pdf(
            args.edition_id, expected_preview_input_hash=preview.preview_input_hash
        )
    finally:
        executor_class._populate_workspace = original  # type: ignore[method-assign]
        await engine.dispose()

    (output / "bulletin_recompile.pdf").write_bytes(pdf.content)
    print(
        "preview_input_hash",
        preview.preview_input_hash,
        "pdf_sha256",
        hashlib.sha256(pdf.content).hexdigest(),
        "workspace",
        workspace_copy,
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(_run(build_parser().parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())
