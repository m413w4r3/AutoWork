from __future__ import annotations

from typing import Any, Literal, NoReturn, cast
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field

from cti_app.application.typst_input_inspection import (
    MAX_FILE_LINES,
    RenderInputInspectionError,
    RenderInputInspectionService,
)
from cti_app.domain.typst_input_inspection import (
    RenderInputDiagnosticCategory,
    RenderInputInspectionAvailability,
    RenderInputInspectionErrorCode,
)

router = APIRouter(prefix="/api/render-inspections", tags=["render-inspection"])


class RenderIdentity(BaseModel):
    model_config = ConfigDict(extra="allow")


class RenderInputRender(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    type: str
    status: str
    identity: dict[str, Any]
    input_hash: str | None
    error_code: str | None


class RenderInputTemplate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: str | None
    sha256: str | None
    snapshot_sha256: str | None
    verified: bool


class RenderInputFileMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")

    file_id: str
    path: str
    sha256: str
    byte_size: int
    kind: str


class RenderInputSource(BaseModel):
    model_config = ConfigDict(extra="forbid")

    availability: RenderInputInspectionAvailability
    entrypoint: RenderInputFileMetadata | None
    files_href: str


class RenderInputDataMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")

    availability: RenderInputInspectionAvailability
    schema_version: str | None
    sha256: str | None
    href: str


class RenderInputMediaHash(BaseModel):
    model_config = ConfigDict(extra="forbid")

    asset_id: str | None
    relative_path: str | None
    expected_sha256: str | None
    sha256: str | None
    byte_size: int | None


class RenderInputHashes(BaseModel):
    model_config = ConfigDict(extra="forbid")

    template_sha256: str | None
    source_sha256: str | None
    render_data_sha256: str | None
    media: list[RenderInputMediaHash]
    pdf_sha256: str | None


class RenderInputCompiler(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    version: str | None
    renderer: str
    renderer_version: str | None
    font_bundle_version: str | None


class RenderInputPdf(BaseModel):
    model_config = ConfigDict(extra="allow")

    availability: str
    sha256: str | None
    page_count: int | None
    href: str | None
    visual_diagnostics_href: str | None
    canonical_blocks: list[dict[str, Any]] = Field(default_factory=list)


class RenderInputInspectionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["render-input-inspection-v1"]
    render: RenderInputRender
    template: RenderInputTemplate
    compiler: RenderInputCompiler
    source: RenderInputSource
    render_data: RenderInputDataMetadata
    hashes: RenderInputHashes
    origin: dict[str, Any] | str | None
    pdf: RenderInputPdf


class RenderInputFilesResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["render-input-files-v1"]
    render_id: str
    availability: RenderInputInspectionAvailability
    files: list[RenderInputFileMetadata]


class RenderInputFileResponse(RenderInputFileMetadata):
    schema_version: Literal["render-input-file-v1"]
    render_id: str
    start_line: int
    end_line: int
    content: str


class RenderDataInspectionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    schema_version: Literal["render-data-inspection-v1"]
    render_id: str
    sha256: str | None
    schema_name: str | None = Field(alias="schema")
    availability: RenderInputInspectionAvailability
    data: Any = Field(default=None, alias="json")
    matches: list[dict[str, Any]] | None = None
    selection: dict[str, Any] | None = None
    error: str | None = None


class RenderInputDiagnostic(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    category: RenderInputDiagnosticCategory
    severity: Literal["error", "warning"]
    message: str
    location: dict[str, Any] | None
    suggested_channel: dict[str, Any]


class RenderInputWarningSet(BaseModel):
    model_config = ConfigDict(extra="forbid")

    availability: Literal["available", "unavailable", "not_persisted"]
    items: list[RenderInputDiagnostic]


class RenderInputWarnings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    compiler: RenderInputWarningSet
    pdf: RenderInputWarningSet


class RenderInputDiagnosticsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["render-input-diagnostics-v1"]
    render_id: str
    diagnostics: list[RenderInputDiagnostic]
    warnings: RenderInputWarnings
    pdf: dict[str, Any]


def _service(request: Request) -> RenderInputInspectionService:
    configured = getattr(request.app.state, "render_input_inspection_service", None)
    if configured is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": RenderInputInspectionErrorCode.INPUT_SNAPSHOT_UNAVAILABLE},
        )
    return cast(RenderInputInspectionService, configured)


def _raise_inspection_error(error: RenderInputInspectionError) -> NoReturn:
    status_code = {
        RenderInputInspectionErrorCode.RENDER_NOT_FOUND: status.HTTP_404_NOT_FOUND,
        RenderInputInspectionErrorCode.FILE_NOT_IN_BUNDLE: status.HTTP_404_NOT_FOUND,
        RenderInputInspectionErrorCode.INPUT_SNAPSHOT_UNAVAILABLE: (
            status.HTTP_503_SERVICE_UNAVAILABLE
        ),
        RenderInputInspectionErrorCode.INPUT_HASH_MISMATCH: status.HTTP_409_CONFLICT,
        RenderInputInspectionErrorCode.INVALID_SELECTOR: status.HTTP_422_UNPROCESSABLE_CONTENT,
        RenderInputInspectionErrorCode.SIZE_LIMIT_EXCEEDED: status.HTTP_413_CONTENT_TOO_LARGE,
    }[error.code]
    raise HTTPException(status_code=status_code, detail={"code": error.code.value}) from error


@router.get("/previews/{inspection_id}", response_model=RenderInputInspectionResponse)
async def get_preview_render_inspection(inspection_id: str, request: Request) -> Any:
    try:
        return await _service(request).preview_metadata(inspection_id)
    except RenderInputInspectionError as exc:
        _raise_inspection_error(exc)


@router.get("/previews/{inspection_id}/files", response_model=RenderInputFilesResponse)
async def get_preview_render_files(inspection_id: str, request: Request) -> Any:
    try:
        return await _service(request).files(inspection_id, preview=True)
    except RenderInputInspectionError as exc:
        _raise_inspection_error(exc)


@router.get("/previews/{inspection_id}/files/{file_id}", response_model=RenderInputFileResponse)
async def get_preview_render_file(
    inspection_id: str,
    file_id: str,
    request: Request,
    start_line: int = Query(1, ge=1),
    line_count: int = Query(100, ge=1, le=MAX_FILE_LINES),
) -> Any:
    try:
        return await _service(request).file(
            inspection_id,
            file_id,
            start_line=start_line,
            line_count=line_count,
            preview=True,
        )
    except RenderInputInspectionError as exc:
        _raise_inspection_error(exc)


@router.get("/previews/{inspection_id}/render-data", response_model=RenderDataInspectionResponse)
async def get_preview_render_data(
    inspection_id: str,
    request: Request,
    subject_id: UUID | None = None,
    block_key: str | None = Query(None, max_length=256),
    pointer: str | None = Query(None, max_length=1024),
) -> Any:
    try:
        return await _service(request).render_data(
            inspection_id,
            subject_id=subject_id,
            block_key=block_key,
            pointer=pointer,
            preview=True,
        )
    except RenderInputInspectionError as exc:
        _raise_inspection_error(exc)


@router.get("/previews/{inspection_id}/diagnostics", response_model=RenderInputDiagnosticsResponse)
async def get_preview_render_diagnostics(inspection_id: str, request: Request) -> Any:
    try:
        return await _service(request).diagnostics(inspection_id, preview=True)
    except RenderInputInspectionError as exc:
        _raise_inspection_error(exc)


@router.get("/{render_id}", response_model=RenderInputInspectionResponse)
async def get_render_inspection(render_id: str, request: Request) -> Any:
    try:
        return await _service(request).metadata(render_id)
    except RenderInputInspectionError as exc:
        _raise_inspection_error(exc)


@router.get("/{render_id}/files", response_model=RenderInputFilesResponse)
async def get_render_files(render_id: str, request: Request) -> Any:
    try:
        return await _service(request).files(render_id)
    except RenderInputInspectionError as exc:
        _raise_inspection_error(exc)


@router.get("/{render_id}/files/{file_id}", response_model=RenderInputFileResponse)
async def get_render_file(
    render_id: str,
    file_id: str,
    request: Request,
    start_line: int = Query(1, ge=1),
    line_count: int = Query(100, ge=1, le=MAX_FILE_LINES),
) -> Any:
    try:
        return await _service(request).file(
            render_id, file_id, start_line=start_line, line_count=line_count
        )
    except RenderInputInspectionError as exc:
        _raise_inspection_error(exc)


@router.get("/{render_id}/render-data", response_model=RenderDataInspectionResponse)
async def get_render_data(
    render_id: str,
    request: Request,
    subject_id: UUID | None = None,
    block_key: str | None = Query(None, max_length=256),
    pointer: str | None = Query(None, max_length=1024),
) -> Any:
    try:
        return await _service(request).render_data(
            render_id,
            subject_id=subject_id,
            block_key=block_key,
            pointer=pointer,
        )
    except RenderInputInspectionError as exc:
        _raise_inspection_error(exc)


@router.get("/{render_id}/diagnostics", response_model=RenderInputDiagnosticsResponse)
async def get_render_diagnostics(render_id: str, request: Request) -> Any:
    try:
        return await _service(request).diagnostics(render_id)
    except RenderInputInspectionError as exc:
        _raise_inspection_error(exc)
