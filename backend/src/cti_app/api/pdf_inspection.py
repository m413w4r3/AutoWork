from __future__ import annotations

from typing import Any, Literal, NoReturn, cast
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field

from cti_app.application.pdf_inspection import PdfInspectionError, PdfInspectionService
from cti_app.domain.pdf_inspection import (
    PdfDiagnosticClass,
    PdfInspectionAvailability,
    PdfInspectionErrorCode,
    PdfInspectionProfile,
    PdfMappingStatus,
    PdfRasterStatus,
)
from cti_app.domain.typst_render import TypstRenderStatus

router = APIRouter(prefix="/api/pdf-inspections", tags=["pdf-inspection"])


class PdfMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sha256: str
    byte_size: int
    page_count: int
    page_size_points: tuple[float, float] | None
    encrypted: bool
    parser_warnings: list[str] = Field(max_length=20)


class PdfRenderMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID
    status: TypstRenderStatus
    input_hash: str
    renderer_version: str
    template_sha256: str
    compiler_version: str
    font_bundle_version: str


class PdfSourceArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subject_id: UUID
    artifact_id: UUID
    artifact_version: int
    artifact_input_hash: str


class PdfSourceMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["subject_publication", "edition_release", "edition_preview"]
    render_id: UUID | None = None
    subject_id: UUID | None = None
    artifact_id: UUID | None = None
    artifact_version: int | None = None
    artifact_input_hash: str | None = None
    edition_id: UUID | None = None
    release_id: UUID | None = None
    manifest_id: UUID | None = None
    manifest_sha256: str | None = None
    edition_document_sha256: str | None = None
    preview_input_hash: str | None = None
    artifacts: list[PdfSourceArtifact] = Field(default_factory=list)


class PdfPreviewProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dpi: int
    max_edge_px: int


class PdfThumbnailProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_edge_px: int


class PdfRasterProfiles(BaseModel):
    model_config = ConfigDict(extra="forbid")

    preview: PdfPreviewProfile
    thumbnail: PdfThumbnailProfile


class PdfRasterMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: PdfRasterStatus
    rasterizer: Literal["poppler"]
    rasterizer_version: str
    profiles: PdfRasterProfiles
    page_png_url_template: str
    thumbnail_url_template: str


class PdfDiagnostic(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    class_: PdfDiagnosticClass = Field(alias="class")
    page: int | None
    measurement: dict[str, Any]
    target: None = None


class PdfDiagnosticCounts(BaseModel):
    model_config = ConfigDict(extra="forbid")

    deterministic: int
    heuristic: int


class PdfDiagnostics(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    status: Literal["ready", "unavailable"]
    engine_version: str
    counts: PdfDiagnosticCounts
    items: list[PdfDiagnostic]


class PdfMapping(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: PdfMappingStatus
    items: list[Any] = Field(default_factory=list, max_length=0)


class PdfInspectionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["pdf-inspection-v1"]
    pdf: PdfMetadata
    availability: PdfInspectionAvailability
    render: list[PdfRenderMetadata]
    source: list[PdfSourceMetadata]
    raster: PdfRasterMetadata
    diagnostics: PdfDiagnostics
    mapping: PdfMapping


def _service(request: Request) -> PdfInspectionService:
    configured = getattr(request.app.state, "pdf_inspection_service", None)
    if configured is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "pdf_inspection_unavailable"},
        )
    return cast(PdfInspectionService, configured)


def _raise_inspection_error(error: PdfInspectionError) -> NoReturn:
    status_code = {
        PdfInspectionErrorCode.ABSENT: status.HTTP_404_NOT_FOUND,
        PdfInspectionErrorCode.HASH_INVALID: status.HTTP_422_UNPROCESSABLE_CONTENT,
        PdfInspectionErrorCode.OUTPUT_CORRUPT: status.HTTP_422_UNPROCESSABLE_CONTENT,
        PdfInspectionErrorCode.LIMIT_EXCEEDED: status.HTTP_413_CONTENT_TOO_LARGE,
        PdfInspectionErrorCode.PREVIEW_CACHE_EXPIRED: status.HTTP_410_GONE,
        PdfInspectionErrorCode.PAGE_OUT_OF_BOUNDS: status.HTTP_416_RANGE_NOT_SATISFIABLE,
    }[error.code]
    raise HTTPException(status_code=status_code, detail={"code": error.code.value}) from error


@router.get("/{pdf_sha256}", response_model=PdfInspectionResponse)
async def get_pdf_inspection(pdf_sha256: str, request: Request) -> PdfInspectionResponse:
    try:
        return PdfInspectionResponse.model_validate(await _service(request).metadata(pdf_sha256))
    except PdfInspectionError as exc:
        _raise_inspection_error(exc)


@router.get("/{pdf_sha256}/pages/{page}.png")
async def get_pdf_page_png(
    pdf_sha256: str,
    page: int,
    request: Request,
    profile: Literal["preview"] = "preview",
) -> Response:
    del profile  # The profile is intentionally fixed by the endpoint contract.
    try:
        image = await _service(request).page_png(
            pdf_sha256,
            page,
            PdfInspectionProfile.PREVIEW,
        )
    except PdfInspectionError as exc:
        _raise_inspection_error(exc)
    return Response(
        content=image.content,
        media_type="image/png",
        headers={
            "ETag": image.etag,
            "X-PDF-SHA256": image.pdf_sha256,
            "X-PDF-Page": str(image.page),
            "X-PDF-Profile": image.profile.value,
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/{pdf_sha256}/pages/{page}/thumbnail.png")
async def get_pdf_page_thumbnail(pdf_sha256: str, page: int, request: Request) -> Response:
    try:
        image = await _service(request).page_png(
            pdf_sha256,
            page,
            PdfInspectionProfile.THUMBNAIL,
        )
    except PdfInspectionError as exc:
        _raise_inspection_error(exc)
    return Response(
        content=image.content,
        media_type="image/png",
        headers={
            "ETag": image.etag,
            "X-PDF-SHA256": image.pdf_sha256,
            "X-PDF-Page": str(image.page),
            "X-PDF-Profile": image.profile.value,
            "X-Content-Type-Options": "nosniff",
        },
    )
