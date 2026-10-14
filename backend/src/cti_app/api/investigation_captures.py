"""Versioned operator routes for importing quarantined investigation captures."""

from __future__ import annotations

from typing import Literal, cast
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel

from cti_app.application.network_capture_import import (
    CaptureInvestigationNotFoundError,
    NetworkCaptureImportService,
)
from cti_app.application.network_workbench import NetworkCaptureRejected
from cti_app.domain.network_protocol import NetworkCaptureRejection

router = APIRouter(prefix="/api/v1", tags=["investigation-captures"])
_CAPTURE_CONTENT_TYPES = frozenset(
    {
        "application/octet-stream",
        "application/vnd.tcpdump.pcap",
        "application/x-pcapng",
    }
)


class CaptureImportResponse(BaseModel):
    schema_version: Literal["network-capture-import.v1"]
    investigation_id: UUID
    source_reference: str
    blob_id: UUID
    sha256: str
    size_bytes: int
    format: str


def _import_service(request: Request) -> NetworkCaptureImportService:
    service = getattr(request.app.state, "network_capture_import_service", None)
    if service is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "network_capture_import_unavailable"},
        )
    return cast(NetworkCaptureImportService, service)


async def _operator_id(request: Request) -> str:
    provider = getattr(request.app.state, "identity_provider", None)
    if provider is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "operator_identity_required"},
        )
    identity = await provider.current()
    actor_id = getattr(identity, "actor_id", None)
    if not isinstance(actor_id, str) or not actor_id.strip():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "operator_identity_required"},
        )
    return actor_id.strip()


@router.get("/network-captures/capabilities")
async def network_capture_import_capabilities(request: Request) -> dict[str, object]:
    service = _import_service(request)
    return {
        "schema_version": "network-capture-import-capabilities.v1",
        "upload_available": True,
        "method": "POST",
        "path_template": "/api/v1/investigations/{investigation_id}/network-captures",
        "max_upload_bytes": service.max_upload_bytes,
        "formats": ["pcap", "pcapng"],
        "content_types": sorted(_CAPTURE_CONTENT_TYPES),
        "sha256_header": "X-Content-SHA256",
        "declared_sha256_optional": True,
        "requires_operator_identity": True,
    }


@router.post(
    "/investigations/{investigation_id}/network-captures",
    response_model=CaptureImportResponse,
    status_code=status.HTTP_201_CREATED,
)
async def import_investigation_capture(
    investigation_id: UUID,
    request: Request,
) -> CaptureImportResponse:
    operator_id = await _operator_id(request)
    service = _import_service(request)
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type not in _CAPTURE_CONTENT_TYPES:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail={"code": "capture_content_type_required"},
        )
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            declared_length = int(content_length)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"code": "invalid_content_length"},
            ) from exc
        if declared_length < 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"code": "invalid_content_length"},
            )
        if declared_length > service.max_upload_bytes:
            raise _capture_http_error(NetworkCaptureRejected(NetworkCaptureRejection.SIZE_LIMIT))
    try:
        content = await _read_bounded_request(request, service.max_upload_bytes)
        imported = await service.import_capture(
            investigation_id=investigation_id,
            content=content,
            operator_id=operator_id,
            declared_sha256=request.headers.get("x-content-sha256"),
        )
    except CaptureInvestigationNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "investigation_not_found"},
        ) from exc
    except NetworkCaptureRejected as exc:
        raise _capture_http_error(exc) from exc

    return CaptureImportResponse(
        schema_version="network-capture-import.v1",
        investigation_id=imported.investigation_id,
        source_reference=imported.source_reference,
        blob_id=imported.blob_id,
        sha256=imported.sha256,
        size_bytes=imported.size_bytes,
        format=imported.format.value,
    )


async def _read_bounded_request(request: Request, max_bytes: int) -> bytes:
    content = bytearray()
    async for chunk in request.stream():
        if len(content) + len(chunk) > max_bytes:
            raise NetworkCaptureRejected(NetworkCaptureRejection.SIZE_LIMIT)
        content.extend(chunk)
    return bytes(content)


def _capture_http_error(error: NetworkCaptureRejected) -> HTTPException:
    code = error.reason
    http_status = (
        status.HTTP_413_CONTENT_TOO_LARGE
        if code is NetworkCaptureRejection.SIZE_LIMIT
        else status.HTTP_404_NOT_FOUND
        if code is NetworkCaptureRejection.CAPTURE_NOT_ASSOCIATED
        else status.HTTP_503_SERVICE_UNAVAILABLE
        if code is NetworkCaptureRejection.BLOB_UNAVAILABLE
        else status.HTTP_422_UNPROCESSABLE_CONTENT
    )
    return HTTPException(status_code=http_status, detail={"code": code.value})
