from __future__ import annotations

from datetime import datetime
from typing import Any, cast
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field
from starlette.datastructures import FormData, UploadFile
from starlette.requests import Request as StarletteRequest

from cti_app.application.rule_corpus_import import (
    HoldoutDesignation,
    ManualRuleCorpusImportRejected,
    ManualRuleCorpusImportService,
    RuleCorpusImportInvestigationNotFound,
    RuleCorpusImportRejection,
)
from cti_app.domain.rule_workbench import RuleSampleProvenance, RuleTestCorpusRole

router = APIRouter(prefix="/api", tags=["investigation-rule-corpus"])
_MAX_MULTIPART_OVERHEAD_BYTES = 64 * 1024


class RuleCorpusImportResponse(BaseModel):
    schema_version: str = "manual-rule-corpus-import.v1"
    investigation_id: UUID
    observation_id: UUID
    sample_id: UUID
    association_id: UUID
    sha256: str
    size_bytes: int
    intended_role: RuleTestCorpusRole
    provenance: RuleSampleProvenance
    reason: str
    source_description: str
    source: str = "operator_import"
    idempotent_replay: bool
    local_only: bool = True
    executed: bool = False


class _StrictOperatorRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class HoldoutDesignationRequest(_StrictOperatorRequest):
    sha256: str = Field(pattern=r"^[0-9a-fA-F]{64}$")
    intended_role: RuleTestCorpusRole
    reason: str = Field(min_length=1, max_length=1_500)


class HoldoutDesignationResponse(BaseModel):
    schema_version: str = "rule-corpus-holdout-designation.v1"
    investigation_id: UUID
    sample_sha256: str
    intended_role: RuleTestCorpusRole
    designated_at: datetime
    operator_id: str
    reason: str
    append_only: bool = True
    idempotent_replay: bool = False


class ExistingSampleRoleRequest(_StrictOperatorRequest):
    sample_id: UUID | None = None
    sha256: str | None = Field(default=None, pattern=r"^[0-9a-fA-F]{64}$")
    intended_role: RuleTestCorpusRole
    provenance: RuleSampleProvenance
    reason: str = Field(min_length=1, max_length=1_500)
    source_description: str = Field(default="", max_length=1_000)


class ExistingSampleRoleResponse(BaseModel):
    schema_version: str = "existing-quarantine-sample-role.v1"
    investigation_id: UUID
    observation_id: UUID
    sample_id: UUID
    association_id: UUID
    sha256: str
    size_bytes: int
    intended_role: RuleTestCorpusRole
    provenance: RuleSampleProvenance
    reason: str
    source_description: str
    source: str = "existing_quarantined_sample"
    idempotent_replay: bool
    bytes_exposed: bool = False
    local_only: bool = True
    executed: bool = False


class RuleCorpusImportMetadataView(BaseModel):
    observation_id: UUID
    source_reference: str
    sha256: str
    size_bytes: int
    intended_role: RuleTestCorpusRole
    provenance: RuleSampleProvenance
    source_description: str
    format_magic: str
    designated_at: datetime | None
    operator_id: str


class RuleCorpusImportListResponse(BaseModel):
    schema_version: str = "manual-rule-corpus-import-inventory.v1"
    investigation_id: UUID
    limit: int
    offset: int
    imports: list[RuleCorpusImportMetadataView]


def _import_service(request: Request) -> ManualRuleCorpusImportService:
    service = getattr(request.app.state, "rule_corpus_import_service", None)
    if service is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "rule_corpus_import_unavailable"},
        )
    return cast(ManualRuleCorpusImportService, service)


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


@router.get(
    "/investigations/{investigation_id}/rule-corpus/imports",
    response_model=RuleCorpusImportListResponse,
)
async def list_rule_corpus_imports(
    investigation_id: UUID,
    request: Request,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=10_000),
) -> RuleCorpusImportListResponse:
    await _operator_id(request)
    service = _import_service(request)
    try:
        imports = await service.list_imports(investigation_id)
    except RuleCorpusImportInvestigationNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "investigation_not_found"},
        ) from exc
    page = imports[offset : offset + limit]
    return RuleCorpusImportListResponse(
        investigation_id=investigation_id,
        limit=limit,
        offset=offset,
        imports=[
            RuleCorpusImportMetadataView(
                observation_id=item.observation_id,
                source_reference=f"sha256:{item.sample_sha256}",
                sha256=item.sample_sha256,
                size_bytes=item.size_bytes,
                intended_role=item.role,
                provenance=item.provenance,
                source_description=item.source_description,
                format_magic=item.format_magic,
                designated_at=item.designated_at,
                operator_id=item.operator_id,
            )
            for item in page
        ],
    )


@router.post(
    "/investigations/{investigation_id}/rule-corpus/holdout-designations",
    response_model=HoldoutDesignationResponse,
)
async def designate_rule_corpus_holdout(
    investigation_id: UUID,
    body: HoldoutDesignationRequest,
    request: Request,
) -> HoldoutDesignationResponse:
    operator_id = await _operator_id(request)
    service = _import_service(request)
    try:
        designation = await service.designate_holdout(
            investigation_id=investigation_id,
            sample_sha256=body.sha256,
            intended_role=body.intended_role,
            operator_id=operator_id,
            reason=body.reason,
        )
    except ManualRuleCorpusImportRejected as exc:
        raise _import_http_error(exc) from exc
    return _holdout_designation_response(designation)


@router.post(
    "/investigations/{investigation_id}/rule-corpus/roles",
    response_model=ExistingSampleRoleResponse,
)
async def assign_existing_rule_corpus_role(
    investigation_id: UUID,
    body: ExistingSampleRoleRequest,
    request: Request,
) -> ExistingSampleRoleResponse:
    operator_id = await _operator_id(request)
    service = _import_service(request)
    try:
        assigned = await service.assign_existing_sample(
            investigation_id=investigation_id,
            sample_id=body.sample_id,
            sample_sha256=body.sha256,
            intended_role=body.intended_role,
            provenance=body.provenance,
            operator_id=operator_id,
            reason=body.reason,
            source_description=body.source_description,
        )
    except ManualRuleCorpusImportRejected as exc:
        raise _import_http_error(exc) from exc
    return ExistingSampleRoleResponse(
        investigation_id=assigned.investigation_id,
        observation_id=assigned.observation_id,
        sample_id=assigned.sample_id,
        association_id=assigned.association_id,
        sha256=assigned.sha256,
        size_bytes=assigned.size_bytes,
        intended_role=assigned.intended_role,
        provenance=assigned.provenance,
        reason=assigned.reason,
        source_description=assigned.source_description,
        idempotent_replay=assigned.idempotent_replay,
    )


def _holdout_designation_response(
    designation: HoldoutDesignation,
) -> HoldoutDesignationResponse:
    return HoldoutDesignationResponse(
        investigation_id=designation.investigation_id,
        sample_sha256=designation.sample_sha256,
        intended_role=designation.intended_role,
        designated_at=designation.designated_at,
        operator_id=designation.operator_id,
        reason=designation.reason,
        idempotent_replay=designation.idempotent_replay,
    )


@router.post(
    "/investigations/{investigation_id}/rule-corpus/imports",
    response_model=RuleCorpusImportResponse,
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "multipart/form-data": {
                    "schema": {
                        "type": "object",
                        "required": ["file", "intended_role", "provenance", "reason"],
                        "properties": {
                            "file": {"type": "string", "format": "binary"},
                            "intended_role": {
                                "type": "string",
                                "enum": [
                                    role.value
                                    for role in RuleTestCorpusRole
                                    if role is not RuleTestCorpusRole.BYTES_MISSING
                                ],
                            },
                            "provenance": {"type": "string", "enum": ["real", "synthetic"]},
                            "reason": {"type": "string", "maxLength": 1500},
                            "sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                            "source_description": {"type": "string", "maxLength": 1000},
                        },
                    }
                }
            },
        }
    },
)
async def import_rule_corpus_file(
    investigation_id: UUID,
    request: Request,
) -> RuleCorpusImportResponse:
    operator_id = await _operator_id(request)
    service = _import_service(request)
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type != "multipart/form-data":
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail={"code": "multipart_form_data_required"},
        )

    max_body_bytes = service.max_upload_bytes + _MAX_MULTIPART_OVERHEAD_BYTES
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
        if declared_length > max_body_bytes:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail={"code": "rule_corpus_file_too_large"},
            )

    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > max_body_bytes:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail={"code": "rule_corpus_file_too_large"},
            )
    payload = bytes(body)
    sent = False

    async def receive() -> dict[str, Any]:
        nonlocal sent
        if sent:
            return {"type": "http.disconnect"}
        sent = True
        return {"type": "http.request", "body": payload, "more_body": False}

    form_request = StarletteRequest(request.scope, receive=receive)
    try:
        form = await form_request.form(max_files=1, max_fields=8, max_part_size=4_096)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "invalid_rule_corpus_multipart"},
        ) from exc

    fields = _single_multipart_fields(form)
    upload = fields.get("file")
    if not isinstance(upload, UploadFile):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "rule_corpus_file_required"},
        )
    content = await upload.read(service.max_upload_bytes + 1)
    await upload.close()
    if len(content) > service.max_upload_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail={"code": "rule_corpus_file_too_large"},
        )

    try:
        imported = await service.import_bytes(
            investigation_id=investigation_id,
            content=content,
            intended_role=_multipart_text_field(fields, "intended_role"),
            provenance=_multipart_text_field(fields, "provenance"),
            operator_id=operator_id,
            reason=_multipart_text_field(fields, "reason"),
            source_description=_multipart_text_field(fields, "source_description"),
            declared_sha256=_multipart_text_field(fields, "sha256") or None,
        )
    except ManualRuleCorpusImportRejected as exc:
        raise _import_http_error(exc) from exc
    return RuleCorpusImportResponse(
        investigation_id=imported.investigation_id,
        observation_id=imported.observation_id,
        sample_id=imported.sample_id,
        association_id=imported.association_id,
        sha256=imported.sha256,
        size_bytes=imported.size_bytes,
        intended_role=imported.intended_role,
        provenance=imported.provenance,
        reason=imported.reason,
        source_description=imported.source_description,
        idempotent_replay=imported.idempotent_replay,
    )


def _single_multipart_fields(form: FormData) -> dict[str, str | UploadFile]:
    fields: dict[str, str | UploadFile] = {}
    for name in form.keys():
        values = form.getlist(name)
        if len(values) != 1:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={"code": "duplicate_rule_corpus_field"},
            )
        value = values[0]
        if name == "file":
            if not isinstance(value, UploadFile):
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail={"code": "rule_corpus_file_required"},
                )
            fields[name] = value
        elif name in {
            "intended_role",
            "provenance",
            "reason",
            "sha256",
            "source_description",
        } and isinstance(value, str):
            fields[name] = value
        else:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={"code": "unknown_rule_corpus_field"},
            )
    return fields


def _multipart_text_field(fields: dict[str, str | UploadFile], name: str) -> str:
    value = fields.get(name, "")
    if not isinstance(value, str):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "invalid_rule_corpus_field"},
        )
    return value


def _import_http_error(exc: ManualRuleCorpusImportRejected) -> HTTPException:
    reason = exc.reason
    status_code = (
        status.HTTP_404_NOT_FOUND
        if reason
        in {
            RuleCorpusImportRejection.INVESTIGATION_NOT_FOUND,
            RuleCorpusImportRejection.SAMPLE_NOT_FOUND,
        }
        else status.HTTP_409_CONFLICT
        if reason
        in {
            RuleCorpusImportRejection.INVESTIGATION_TERMINAL,
            RuleCorpusImportRejection.ROLE_CONFLICT,
            RuleCorpusImportRejection.HOLDOUT_OVERLAP,
            RuleCorpusImportRejection.SAMPLE_AMBIGUOUS,
            RuleCorpusImportRejection.SAMPLE_NOT_QUARANTINED,
        }
        else status.HTTP_413_REQUEST_ENTITY_TOO_LARGE
        if reason is RuleCorpusImportRejection.SIZE_LIMIT
        else status.HTTP_422_UNPROCESSABLE_ENTITY
    )
    return HTTPException(status_code=status_code, detail={"code": reason.value})
