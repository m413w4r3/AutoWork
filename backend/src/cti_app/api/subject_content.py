"""Stable Subject read endpoints."""

from __future__ import annotations

from typing import Any, cast
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, ConfigDict

from cti_app.application.media_assets import MediaAssetStore
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.application.production_parsers import IndicatorStatus
from cti_app.application.publication_rendering import (
    PublicationRenderError,
    PublicationRenderService,
)
from cti_app.application.subject_content import (
    SubjectAssetsView,
    SubjectContentService,
    SubjectContentView,
    SubjectIndicatorView,
)
from cti_app.application.typst_compilation import TypstCompilationError
from cti_app.application.typst_render_output import (
    TypstRenderOutputIntegrityError,
    TypstRenderOutputStorageError,
    read_verified_render_pdf,
)
from cti_app.domain.classification import TLP
from cti_app.domain.errors import EntityNotFoundError
from cti_app.domain.production import (
    ProductionArtifactStage,
    ProductionArtifactStatus,
)
from cti_app.domain.publication import ArtifactType
from cti_app.domain.publication_document import publication_document_from_json
from cti_app.domain.publication_render import (
    PublicationPreviewDisposition,
    PublicationPreviewStatus,
    PublicationRender,
)
from cti_app.domain.typst_render import TypstRenderStatus

router = APIRouter(prefix="/api", tags=["subject-content"])


class ContentResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    subject_id: UUID
    run_id: UUID
    pipeline_generation: int
    artifact_id: UUID
    artifact_version: int
    artifact_input_hash: str
    status: ProductionArtifactStatus
    schema_version: str
    canonical_content: dict[str, Any]


class IndicatorResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    artifact_type: ArtifactType
    display_value: str
    normalized_value: str
    indicator_status: IndicatorStatus
    source_ids: tuple[str, ...]


class AssetResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    original_name: str
    mime_type: str | None
    sha256: str | None
    size: int | None
    origin: str
    provenance: dict[str, str] | None
    tlp: TLP
    do_not_submit: bool
    external_llm_allowed: bool


class AssetsResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    sources: list[AssetResponse]
    samples: list[AssetResponse]


class PublicationArtifactPreviewResponse(BaseModel):
    status: PublicationPreviewStatus
    artifact_id: UUID
    artifact_version: int
    artifact_input_hash: str
    current_artifact_id: UUID
    current_artifact_version: int
    render_id: UUID | None = None
    render_identity: str | None = None
    render_disposition: PublicationPreviewDisposition
    published_edition_version: int | None = None
    error_code: str | None = None
    error_message: str | None = None
    pdf_url: str | None = None


def _service(request: Request) -> SubjectContentService:
    return cast(SubjectContentService, request.app.state.subject_content_service)


def _publication_render_service(request: Request) -> PublicationRenderService:
    configured = getattr(request.app.state, "publication_render_service", None)
    if configured is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "publication_render_unavailable"},
        )
    return cast(PublicationRenderService, configured)


def _media_asset_store(request: Request) -> MediaAssetStore:
    configured = getattr(request.app.state, "media_asset_store", None)
    if configured is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "publication_media_unavailable"},
        )
    return cast(MediaAssetStore, configured)


def _not_found() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail={
            "code": "subject_content_not_found",
            "message": "Aucun contenu produit n'est disponible pour ce sujet.",
        },
    )


@router.get("/subjects/{subject_id}/content", response_model=ContentResponse)
async def get_subject_content(subject_id: UUID, request: Request) -> ContentResponse:
    value: SubjectContentView | None = await _service(request).content(subject_id)
    if value is None:
        raise _not_found()
    return ContentResponse.model_validate(value, from_attributes=True)


@router.get("/subjects/{subject_id}/publication/pdf")
async def download_subject_publication_pdf(subject_id: UUID, request: Request) -> Response:
    publication = await _service(request).current_publication(subject_id)
    if publication is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "publication_not_available"},
        )
    if publication.artifact.status is not ProductionArtifactStatus.VERIFIED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "publication_not_verified"},
        )

    try:
        render = await _publication_render_service(request).render_pdf(publication.artifact.id)
    except (PublicationRenderError, TypstCompilationError) as exc:
        status_code = (
            status.HTTP_422_UNPROCESSABLE_CONTENT
            if exc.code
            in {
                "publication_render_document_invalid",
                "publication_render_media_kind_mismatch",
                "publication_render_media_integrity_mismatch",
                "typst_output_invalid",
            }
            else status.HTTP_503_SERVICE_UNAVAILABLE
        )
        raise HTTPException(
            status_code=status_code,
            detail={"code": exc.code},
        ) from exc

    artifact_store = cast(ProductionArtifactStore, request.app.state.production_artifact_store)
    try:
        content = await read_verified_render_pdf(artifact_store, render)
    except TypstRenderOutputStorageError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "publication_pdf_storage_error"},
        ) from exc
    except TypstRenderOutputIntegrityError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"code": "publication_pdf_integrity_mismatch"},
        ) from exc

    return Response(
        content=content,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="publication-{subject_id}.pdf"',
            "X-Publication-Artifact-Id": str(publication.artifact.id),
            "X-Publication-Artifact-Version": str(publication.artifact.version),
            "X-Publication-Render-Identity": render.input_hash,
        },
    )


async def _publication_preview(
    subject_id: UUID,
    artifact_id: UUID,
    request: Request,
) -> tuple[PublicationArtifactPreviewResponse, PublicationRender | None]:
    current = await _service(request).current_publication(subject_id)
    if current is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "publication_not_available"},
        )

    async with request.app.state.uow_factory() as uow:
        requested = await uow.production_artifacts.get(artifact_id)
        if (
            requested is None
            or requested.subject_id != subject_id
            or requested.stage is not ProductionArtifactStage.PUBLICATION
        ):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"code": "publication_artifact_not_found"},
            )

        manifest = await uow.publication_manifests.get_latest_for_edition(current.run.edition_id)
        release = (
            await uow.edition_releases.get_by_manifest(manifest.id)
            if manifest is not None
            else None
        )
        accepted_version = None
        if (
            manifest is not None
            and release is not None
            and any(
                entry.subject_id == requested.subject_id
                and entry.production_run_id == requested.production_run_id
                and entry.pipeline_generation == current.run.pipeline_generation
                and entry.document_artifact_id == requested.id
                and entry.document_artifact_version == requested.version
                and entry.document_input_hash == requested.input_hash
                for entry in manifest.entries
            )
        ):
            accepted_version = manifest.edition_version

        is_current = requested.id == current.artifact.id
        old_render = (
            await uow.publication_renders.get_latest_for_artifact(requested.id)
            if not is_current
            else None
        )

    disposition = (
        PublicationPreviewDisposition.ACCEPTED_VERSION
        if accepted_version is not None
        else PublicationPreviewDisposition.EXPLICIT_RENDER
    )
    if not is_current:
        return (
            PublicationArtifactPreviewResponse(
                status=PublicationPreviewStatus.STALE,
                artifact_id=requested.id,
                artifact_version=requested.version,
                artifact_input_hash=requested.input_hash,
                current_artifact_id=current.artifact.id,
                current_artifact_version=current.artifact.version,
                render_id=old_render.id if old_render is not None else None,
                render_identity=old_render.input_hash if old_render is not None else None,
                render_disposition=disposition,
                published_edition_version=accepted_version,
                error_code="publication_preview_stale",
                error_message=(
                    "A newer publication artifact is current. This render belongs to the "
                    "older artifact version."
                ),
            ),
            old_render,
        )

    if requested.status is not ProductionArtifactStatus.VERIFIED:
        return (
            PublicationArtifactPreviewResponse(
                status=PublicationPreviewStatus.FAILED,
                artifact_id=requested.id,
                artifact_version=requested.version,
                artifact_input_hash=requested.input_hash,
                current_artifact_id=current.artifact.id,
                current_artifact_version=current.artifact.version,
                render_disposition=disposition,
                published_edition_version=accepted_version,
                error_code="publication_not_verified",
                error_message="Only verified publication artifacts can be rendered.",
            ),
            None,
        )

    attempt = await _publication_render_service(request).render_preview(requested.id)
    latest = await _service(request).current_publication(subject_id)
    if latest is None or latest.artifact.id != requested.id:
        current_artifact = latest.artifact if latest is not None else current.artifact
        return (
            PublicationArtifactPreviewResponse(
                status=PublicationPreviewStatus.STALE,
                artifact_id=requested.id,
                artifact_version=requested.version,
                artifact_input_hash=requested.input_hash,
                current_artifact_id=current_artifact.id,
                current_artifact_version=current_artifact.version,
                render_id=attempt.render.id if attempt.render is not None else None,
                render_identity=attempt.render.input_hash if attempt.render is not None else None,
                render_disposition=disposition,
                published_edition_version=accepted_version,
                error_code="publication_preview_stale",
                error_message=(
                    "A newer publication artifact became current while this PDF was rendering."
                ),
            ),
            attempt.render,
        )

    preview_status = {
        TypstRenderStatus.RUNNING: PublicationPreviewStatus.IN_PROGRESS,
        TypstRenderStatus.SUCCEEDED: PublicationPreviewStatus.READY,
        TypstRenderStatus.FAILED: PublicationPreviewStatus.FAILED,
    }[attempt.status]
    render = attempt.render
    pdf_url = (
        f"/api/subjects/{subject_id}/publication/preview/{requested.id}/pdf"
        f"?render_identity={render.input_hash}"
        if preview_status is PublicationPreviewStatus.READY and render is not None
        else None
    )
    return (
        PublicationArtifactPreviewResponse(
            status=preview_status,
            artifact_id=requested.id,
            artifact_version=requested.version,
            artifact_input_hash=requested.input_hash,
            current_artifact_id=latest.artifact.id,
            current_artifact_version=latest.artifact.version,
            render_id=render.id if render is not None else None,
            render_identity=render.input_hash if render is not None else None,
            render_disposition=disposition,
            published_edition_version=accepted_version,
            error_code=attempt.error_code,
            error_message=attempt.error_message,
            pdf_url=pdf_url,
        ),
        render,
    )


@router.get(
    "/subjects/{subject_id}/publication/preview",
    response_model=PublicationArtifactPreviewResponse,
)
async def get_subject_publication_preview(
    subject_id: UUID,
    request: Request,
    artifact_id: UUID = Query(...),
) -> PublicationArtifactPreviewResponse:
    preview, _ = await _publication_preview(subject_id, artifact_id, request)
    return preview


@router.get("/subjects/{subject_id}/publication/preview/{artifact_id}/pdf")
async def get_subject_publication_preview_pdf(
    subject_id: UUID,
    artifact_id: UUID,
    request: Request,
    render_identity: str = Query(pattern=r"^[0-9a-f]{64}$"),
) -> Response:
    preview, render = await _publication_preview(subject_id, artifact_id, request)
    if preview.status is not PublicationPreviewStatus.READY or render is None:
        response_status = (
            status.HTTP_202_ACCEPTED
            if preview.status is PublicationPreviewStatus.IN_PROGRESS
            else status.HTTP_409_CONFLICT
        )
        raise HTTPException(
            status_code=response_status,
            detail={"code": preview.error_code or preview.status.value},
        )
    if render.input_hash != render_identity or render.publication_artifact_id != artifact_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "publication_preview_identity_mismatch"},
        )

    artifact_store = cast(ProductionArtifactStore, request.app.state.production_artifact_store)
    try:
        content = await read_verified_render_pdf(artifact_store, render)
    except TypstRenderOutputStorageError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "publication_pdf_storage_error"},
        ) from exc
    except TypstRenderOutputIntegrityError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"code": "publication_pdf_integrity_mismatch"},
        ) from exc
    return Response(
        content=content,
        media_type="application/pdf",
        headers={
            "Content-Disposition": (
                f'inline; filename="publication-{subject_id}-v{preview.artifact_version}.pdf"'
            ),
            "X-Publication-Artifact-Id": str(artifact_id),
            "X-Publication-Artifact-Version": str(preview.artifact_version),
            "X-Publication-Render-Identity": render.input_hash,
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/subjects/{subject_id}/publication/assets/{asset_id}")
async def get_subject_publication_asset(
    subject_id: UUID,
    asset_id: UUID,
    request: Request,
) -> Response:
    content = await _service(request).content(subject_id)
    if content is None:
        raise _not_found()
    try:
        document = publication_document_from_json(content.canonical_content)
    except (ValueError, KeyError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"code": "publication_document_invalid"},
        ) from exc

    referenced_assets = {item.asset_id for item in document.diagrams} | {
        item.asset_id for item in document.figures
    }
    if asset_id not in referenced_assets:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "publication_asset_not_found"},
        )

    media_assets = _media_asset_store(request)
    try:
        manifest = await media_assets.get(asset_id)
        if manifest is None:
            raise EntityNotFoundError(f"Media asset {asset_id} does not exist")
        image_bytes = await media_assets.read(asset_id)
    except EntityNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "publication_asset_not_found"},
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "publication_asset_unavailable"},
        ) from exc

    return Response(
        content=image_bytes,
        media_type=manifest.mime_type,
        headers={
            "Cache-Control": "private, max-age=31536000, immutable",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/subjects/{subject_id}/indicators", response_model=list[IndicatorResponse])
async def get_subject_indicators(subject_id: UUID, request: Request) -> list[IndicatorResponse]:
    values: list[SubjectIndicatorView] = await _service(request).indicators(subject_id)
    return [IndicatorResponse.model_validate(value, from_attributes=True) for value in values]


@router.get("/subjects/{subject_id}/assets", response_model=AssetsResponse)
async def get_subject_assets(subject_id: UUID, request: Request) -> AssetsResponse:
    value: SubjectAssetsView | None = await _service(request).assets(subject_id)
    if value is None:
        raise _not_found()
    return AssetsResponse(
        sources=[
            AssetResponse.model_validate(item, from_attributes=True) for item in value.sources
        ],
        samples=[
            AssetResponse.model_validate(item, from_attributes=True) for item in value.samples
        ],
    )
