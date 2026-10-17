from datetime import datetime
from typing import Annotated, Literal, NoReturn, cast
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field

from cti_app.application.identity import IdentityProvider
from cti_app.application.model_conversations import (
    ModelConversationError,
    ModelConversationService,
)
from cti_app.domain.model_conversations import (
    ConversationMode,
    ConversationPurpose,
    ConversationStatus,
    ConversationTransport,
    ModelConversation,
    ModelConversationTurn,
)
from cti_app.domain.model_runs import ModelProvider
from cti_app.logging import get_correlation_id

router = APIRouter(prefix="/api/model-conversations", tags=["model-conversations"])


class CreateConversationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: ModelProvider = ModelProvider.OPENAI
    transport: ConversationTransport | None = None
    purpose: ConversationPurpose
    edition_id: UUID | None = None
    subject_id: UUID | None = None
    title: str = Field(min_length=1, max_length=500)
    expected_profile: str | None = Field(default=None, max_length=255)
    requested_model: str | None = Field(default=None, max_length=255)


class AddTurnRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1, max_length=100_000)
    mode: Literal["fresh", "continue"]
    external_llm_allowed: bool
    idempotency_key: str = Field(min_length=1, max_length=255)


class ReconcileRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    available: bool


class VisibleTurnAdoptRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_sha256: str = Field(..., min_length=64, max_length=64, pattern="^[0-9a-f]{64}$")


class TurnAbandonRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(..., min_length=1, max_length=1000)


class VisibleTurnPreviewView(BaseModel):
    turn_id: UUID
    sha256: str
    characters: int
    text: str


class ConversationView(BaseModel):
    id: UUID
    provider: str
    transport: str
    purpose: str
    edition_id: UUID | None
    subject_id: UUID | None
    title: str
    status: str
    external_id: str | None
    external_locator: str | None
    expected_profile: str | None
    requested_model: str | None
    head_turn_id: UUID | None
    turn_count: int
    created_at: datetime
    updated_at: datetime
    last_used_at: datetime | None
    version: int
    evidence_warning: Literal["not_primary_evidence"] = "not_primary_evidence"


class TurnView(BaseModel):
    id: UUID
    conversation_id: UUID
    sequence: int
    parent_turn_id: UUID | None
    model_run_id: UUID
    input_blob_reference: str
    input_sha256: str
    output_blob_reference: str | None
    output_sha256: str | None
    status: str
    external_turn_id: str | None
    idempotency_key: str
    correlation_id: str
    created_at: datetime
    started_at: datetime
    finished_at: datetime | None
    error: dict[str, object] | None
    recovery: dict[str, object] | None = None
    input_text: str | None = None
    output_text: str | None = None


@router.post("", response_model=ConversationView, status_code=status.HTTP_201_CREATED)
async def create_conversation(
    payload: CreateConversationRequest, request: Request
) -> ConversationView:
    transport = payload.transport or (
        ConversationTransport.CHATGPT_BRIDGE
        if payload.provider is ModelProvider.OPENAI
        else ConversationTransport.APPLICATION_MANAGED
    )
    try:
        conversation = await _service(request).create(
            provider=payload.provider,
            transport=transport,
            purpose=payload.purpose,
            title=payload.title,
            edition_id=payload.edition_id,
            subject_id=payload.subject_id,
            expected_profile=payload.expected_profile,
            requested_model=payload.requested_model,
        )
        return _conversation_view(conversation)
    except Exception as exc:
        _raise(exc)


@router.get("", response_model=list[ConversationView])
async def list_conversations(
    request: Request,
    edition_id: UUID | None = None,
    subject_id: UUID | None = None,
    purpose: ConversationPurpose | None = None,
    conversation_status: Annotated[ConversationStatus | None, Query(alias="status")] = None,
    provider: ModelProvider | None = None,
) -> list[ConversationView]:
    values = await _service(request).list(
        edition_id=edition_id,
        subject_id=subject_id,
        purpose=purpose,
        status=conversation_status,
        provider=provider,
    )
    return [_conversation_view(value) for value in values]


@router.get("/{conversation_id}", response_model=ConversationView)
async def get_conversation(
    conversation_id: UUID, request: Request, subject_id: UUID | None = None
) -> ConversationView:
    try:
        return _conversation_view(
            await _service(request).get(conversation_id, context_subject_id=subject_id)
        )
    except Exception as exc:
        _raise(exc)


@router.get("/{conversation_id}/turns", response_model=list[TurnView])
async def list_turns(
    conversation_id: UUID, request: Request, subject_id: UUID | None = None
) -> list[TurnView]:
    try:
        values = await _service(request).turns(conversation_id, context_subject_id=subject_id)
        return [
            _turn_view(value.turn, input_text=value.input_text, output_text=value.output_text)
            for value in values
        ]
    except Exception as exc:
        _raise(exc)


@router.post("/{conversation_id}/turns", response_model=TurnView)
async def add_turn(
    conversation_id: UUID,
    payload: AddTurnRequest,
    request: Request,
    subject_id: UUID | None = None,
) -> TurnView:
    try:
        turn = await _service(request).add_turn(
            conversation_id,
            message=payload.message,
            mode=ConversationMode(payload.mode),
            external_llm_allowed=payload.external_llm_allowed,
            idempotency_key=payload.idempotency_key,
            correlation_id=get_correlation_id(),
            context_subject_id=subject_id,
        )
        return _turn_view(turn)
    except Exception as exc:
        _raise(exc)


@router.post(
    "/{conversation_id}/turns/{turn_id}/reconciliation/visible/preview",
    response_model=VisibleTurnPreviewView,
)
async def preview_visible_turn_reconciliation(
    conversation_id: UUID,
    turn_id: UUID,
    request: Request,
    subject_id: UUID = Query(...),
) -> VisibleTurnPreviewView:
    try:
        await _actor_id(request)
        preview = await _service(request).preview_visible_recovery(
            conversation_id, turn_id, context_subject_id=subject_id
        )
        return VisibleTurnPreviewView.model_validate(preview)
    except Exception as exc:
        _raise(exc)


@router.post(
    "/{conversation_id}/turns/{turn_id}/reconciliation/visible/adopt",
    response_model=TurnView,
)
async def adopt_visible_turn_reconciliation(
    conversation_id: UUID,
    turn_id: UUID,
    payload: VisibleTurnAdoptRequest,
    request: Request,
    subject_id: UUID = Query(...),
) -> TurnView:
    try:
        turn = await _service(request).adopt_visible_recovery(
            conversation_id,
            turn_id,
            expected_sha256=payload.expected_sha256,
            actor_id=await _actor_id(request),
            context_subject_id=subject_id,
        )
        contents = await _service(request).turns(conversation_id, context_subject_id=subject_id)
        content = next((item for item in contents if item.turn.id == turn.id), None)
        return _turn_view(
            turn,
            input_text=content.input_text if content else None,
            output_text=content.output_text if content else None,
        )
    except Exception as exc:
        _raise(exc)


@router.post(
    "/{conversation_id}/turns/{turn_id}/reconciliation/abandon",
    response_model=TurnView,
)
async def abandon_turn_reconciliation(
    conversation_id: UUID,
    turn_id: UUID,
    payload: TurnAbandonRequest,
    request: Request,
    subject_id: UUID = Query(...),
) -> TurnView:
    try:
        turn = await _service(request).abandon_investigation_turn(
            conversation_id,
            turn_id,
            reason=payload.reason,
            actor_id=await _actor_id(request),
            context_subject_id=subject_id,
        )
        contents = await _service(request).turns(conversation_id, context_subject_id=subject_id)
        content = next((item for item in contents if item.turn.id == turn.id), None)
        return _turn_view(
            turn,
            input_text=content.input_text if content else None,
            output_text=content.output_text if content else None,
        )
    except Exception as exc:
        _raise(exc)


@router.post("/{conversation_id}/archive", response_model=ConversationView)
async def archive_conversation(
    conversation_id: UUID, request: Request, subject_id: UUID | None = None
) -> ConversationView:
    try:
        return _conversation_view(
            await _service(request).archive(conversation_id, context_subject_id=subject_id)
        )
    except Exception as exc:
        _raise(exc)


@router.post("/{conversation_id}/reconcile", response_model=ConversationView)
async def reconcile_conversation(
    conversation_id: UUID,
    payload: ReconcileRequest,
    request: Request,
    subject_id: UUID | None = None,
) -> ConversationView:
    try:
        return _conversation_view(
            await _service(request).reconcile(
                conversation_id,
                available=payload.available,
                context_subject_id=subject_id,
            )
        )
    except Exception as exc:
        _raise(exc)


def _service(request: Request) -> ModelConversationService:
    return cast(ModelConversationService, request.app.state.model_conversation_service)


async def _actor_id(request: Request) -> str:
    provider: IdentityProvider = request.app.state.identity_provider
    return (await provider.current()).actor_id


def _conversation_view(value: ModelConversation) -> ConversationView:
    return ConversationView(
        **{
            name: getattr(value, name)
            for name in ConversationView.model_fields
            if name != "evidence_warning"
        }
    )


def _turn_view(
    value: ModelConversationTurn,
    *,
    input_text: str | None = None,
    output_text: str | None = None,
) -> TurnView:
    error: dict[str, object] | None = None
    if value.error_code:
        error = {
            "code": value.error_code,
            "message": value.error_message or "",
            "details": value.error_details or {},
        }
    return TurnView(
        id=value.id,
        conversation_id=value.conversation_id,
        sequence=value.sequence,
        parent_turn_id=value.parent_turn_id,
        model_run_id=value.model_run_id,
        input_blob_reference=value.input_blob_reference,
        input_sha256=value.input_sha256,
        output_blob_reference=value.output_blob_reference,
        output_sha256=value.output_sha256,
        status=value.status.value,
        external_turn_id=value.external_turn_id,
        idempotency_key=value.idempotency_key,
        correlation_id=value.correlation_id,
        created_at=value.created_at,
        started_at=value.started_at,
        finished_at=value.finished_at,
        error=error,
        recovery=(
            value.error_details.get("recovery")
            if value.error_details and isinstance(value.error_details.get("recovery"), dict)
            else None
        ),
        input_text=input_text,
        output_text=output_text,
    )


def _raise(exc: Exception) -> NoReturn:
    if isinstance(exc, ModelConversationError):
        detail: dict[str, object] = {"code": exc.code, "message": str(exc)}
        for field in ("conversation_id", "cause_code", "retryable", "phase", "reason", "details"):
            value = getattr(exc, field, None)
            if value is not None:
                detail[field] = value
        raise HTTPException(
            status_code=exc.status_code,
            detail=detail,
        ) from exc
    code = str(getattr(exc, "code", "model_conversation_failed"))
    status_code = (
        409
        if code == "conversation_busy"
        else 422
        if code
        in {
            "external_llm_blocked",
            "conversation_unavailable",
            "conversation_profile_mismatch",
        }
        else 502
    )
    raise HTTPException(
        status_code=status_code,
        detail={"code": code, "message": str(exc)},
    ) from exc
