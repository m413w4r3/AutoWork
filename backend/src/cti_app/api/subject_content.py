"""Stable Subject read endpoints."""

from __future__ import annotations

import hashlib
from typing import Any, cast
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request, Response, status
from pydantic import BaseModel, ConfigDict

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
from cti_app.application.typst_compilation import (
    TYPST_MAX_PDF_BYTES,
    TypstCompilationError,
)
from cti_app.domain.classification import TLP
from cti_app.domain.production import ProductionArtifactStatus
from cti_app.domain.publication import ArtifactType

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

    assert render.output_blob_id is not None
    artifact_store = cast(ProductionArtifactStore, request.app.state.production_artifact_store)
    try:
        content = await artifact_store.read_bytes(
            render.output_blob_id,
            max_bytes=TYPST_MAX_PDF_BYTES,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "publication_pdf_storage_error"},
        ) from exc
    if (
        len(content) != render.output_byte_size
        or hashlib.sha256(content).hexdigest() != render.output_sha256
    ):
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"code": "publication_pdf_integrity_mismatch"},
        )

    return Response(
        content=content,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="publication-{subject_id}.pdf"'},
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
