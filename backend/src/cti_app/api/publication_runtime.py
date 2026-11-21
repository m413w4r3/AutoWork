"""Shared publication service wiring for the API lifespan and request fallbacks."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from fastapi import Request

from cti_app.application.blobs import BlobCatalogService
from cti_app.application.editorial_pdf_preview import PublicationPreviewRenderer
from cti_app.application.media_assets import MediaAssetStore
from cti_app.application.persistence import UnitOfWorkFactory
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.application.publication_rendering import PublicationRenderService
from cti_app.application.subject_content import SubjectContentService
from cti_app.application.typst_compilation import TypstCompiler
from cti_app.application.typst_paths import typst_bundle_paths
from cti_app.application.typst_render_lifecycle import TypstMappingSidecarCache
from cti_app.application.typst_rendering import TypstRenderer
from cti_app.config import Settings, get_settings
from cti_app.infrastructure.pdf_inspection import PdfInspectionCache
from cti_app.infrastructure.typst_compiler import TypstSubprocessCompiler


@dataclass(frozen=True, slots=True)
class PublicationRuntimeServices:
    uow_factory: UnitOfWorkFactory
    artifact_store: ProductionArtifactStore
    media_asset_store: MediaAssetStore
    typst_compiler: TypstCompiler
    chp_typst_root: Path
    font_bundle_root: Path
    typst_fonts_lock_path: Path
    mapping_cache: TypstMappingSidecarCache
    render_service: PublicationPreviewRenderer
    subject_content_service: SubjectContentService


def build_production_artifact_store(blob_catalog: BlobCatalogService) -> ProductionArtifactStore:
    """Build the production artifact facade from the canonical blob catalog."""
    return ProductionArtifactStore(blob_catalog)


def uow_factory_for_request(request: Request) -> UnitOfWorkFactory | None:
    configured = getattr(request.app.state, "uow_factory", None)
    return cast(UnitOfWorkFactory, configured) if configured is not None else None


def build_publication_runtime_services(
    *,
    uow_factory: UnitOfWorkFactory,
    artifact_store: ProductionArtifactStore,
    settings: Settings,
    media_asset_store: MediaAssetStore | None = None,
    typst_compiler: TypstCompiler | None = None,
    render_service: PublicationPreviewRenderer | None = None,
    subject_content_service: SubjectContentService | None = None,
    mapping_cache: TypstMappingSidecarCache | None = None,
) -> PublicationRuntimeServices:
    """Create the shared API publication dependencies from canonical adapters."""
    media_assets = (
        media_asset_store
        if media_asset_store is not None
        else MediaAssetStore(artifact_store, uow_factory)
    )
    chp_typst_root, font_bundle_root, typst_fonts_lock_path = typst_bundle_paths()
    compiler = (
        typst_compiler
        if typst_compiler is not None
        else TypstSubprocessCompiler(binary=os.environ.get("TYPST_BINARY", "typst"))
    )
    cache = (
        mapping_cache
        if mapping_cache is not None
        else PdfInspectionCache(settings.pdf_inspection_cache_root)
    )
    publication_renderer = render_service
    if publication_renderer is None:
        publication_renderer = PublicationRenderService(
            uow_factory=uow_factory,
            artifact_store=artifact_store,
            media_asset_store=media_assets,
            renderer=TypstRenderer(),
            compiler=compiler,
            chp_typst_root=chp_typst_root,
            font_bundle_root=font_bundle_root,
            typst_fonts_lock_path=typst_fonts_lock_path,
            mapping_cache=cache,
        )
    content_service = (
        subject_content_service
        if subject_content_service is not None
        else SubjectContentService(uow_factory, artifact_store)
    )
    return PublicationRuntimeServices(
        uow_factory=uow_factory,
        artifact_store=artifact_store,
        media_asset_store=media_assets,
        typst_compiler=compiler,
        chp_typst_root=chp_typst_root,
        font_bundle_root=font_bundle_root,
        typst_fonts_lock_path=typst_fonts_lock_path,
        mapping_cache=cache,
        render_service=publication_renderer,
        subject_content_service=content_service,
    )


def production_artifact_store_for_request(request: Request) -> ProductionArtifactStore | None:
    """Reuse the lifespan store or build it from the request's canonical adapters."""
    configured = getattr(request.app.state, "production_artifact_store", None)
    if configured is not None:
        return cast(ProductionArtifactStore, configured)
    blob_store = getattr(request.app.state, "blob_store", None)
    uow_factory = uow_factory_for_request(request)
    if blob_store is None or uow_factory is None:
        return None
    artifact_store = build_production_artifact_store(BlobCatalogService(blob_store, uow_factory))
    request.app.state.production_artifact_store = artifact_store
    return artifact_store


def publication_runtime_for_request(request: Request) -> PublicationRuntimeServices | None:
    """Reuse lifespan services or lazily build the same runtime for reduced apps."""
    uow_factory = uow_factory_for_request(request)
    artifact_store = production_artifact_store_for_request(request)
    if uow_factory is None or artifact_store is None:
        return None

    configured_settings = getattr(request.app.state, "settings", None)
    settings = configured_settings if isinstance(configured_settings, Settings) else get_settings()
    runtime = build_publication_runtime_services(
        uow_factory=uow_factory,
        artifact_store=artifact_store,
        settings=settings,
        media_asset_store=cast(
            MediaAssetStore | None,
            getattr(request.app.state, "media_asset_store", None),
        ),
        typst_compiler=cast(
            TypstCompiler | None,
            getattr(request.app.state, "typst_compiler", None),
        ),
        render_service=cast(
            PublicationPreviewRenderer | None,
            getattr(request.app.state, "publication_render_service", None),
        ),
        subject_content_service=cast(
            SubjectContentService | None,
            getattr(request.app.state, "subject_content_service", None),
        ),
        mapping_cache=cast(
            TypstMappingSidecarCache | None,
            getattr(request.app.state, "pdf_inspection_cache", None),
        ),
    )
    request.app.state.production_artifact_store = runtime.artifact_store
    request.app.state.media_asset_store = runtime.media_asset_store
    request.app.state.typst_compiler = runtime.typst_compiler
    request.app.state.pdf_inspection_cache = runtime.mapping_cache
    request.app.state.publication_render_service = runtime.render_service
    request.app.state.subject_content_service = runtime.subject_content_service
    return runtime
