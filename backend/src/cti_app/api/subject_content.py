"""Stable Subject read endpoints."""

from __future__ import annotations

from typing import Any, cast
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict

from cti_app.application.production_parsers import IndicatorStatus
from cti_app.application.subject_content import (
    SubjectAssetsView,
    SubjectContentService,
    SubjectContentView,
    SubjectIndicatorView,
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
    rendered_content: str | None


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


@router.get("/subjects/{subject_id}/indicators", response_model=list[IndicatorResponse])
async def get_subject_indicators(
    subject_id: UUID, request: Request
) -> list[IndicatorResponse]:
    values: list[SubjectIndicatorView] = await _service(request).indicators(subject_id)
    return [IndicatorResponse.model_validate(value, from_attributes=True) for value in values]


@router.get("/subjects/{subject_id}/assets", response_model=AssetsResponse)
async def get_subject_assets(subject_id: UUID, request: Request) -> AssetsResponse:
    value: SubjectAssetsView | None = await _service(request).assets(subject_id)
    if value is None:
        raise _not_found()
    return AssetsResponse(
        sources=[
            AssetResponse.model_validate(item, from_attributes=True)
            for item in value.sources
        ],
        samples=[
            AssetResponse.model_validate(item, from_attributes=True)
            for item in value.samples
        ],
    )
