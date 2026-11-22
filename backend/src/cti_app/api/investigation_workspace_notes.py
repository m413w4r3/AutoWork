"""Workspace note annotation routes (M24-WS2, ADR-0011 D6/D8).

The orchestrator owns ``api/main.py``: this module only exports ``router``.
Every route is a verified-human action; the shared ``OPERATOR_CLIENT`` token
alone is refused by :func:`_require_verified_human_operator` and by the main
middleware's exact route allowlist.  A note stays a non-evidence work object:

* reading returns the canonical revision history and the annotations, never a
  copy of the file bytes (the backend never reads the host filesystem);
* a Bridge review goes through the existing ``advisor.consult`` path, so G2,
  TLP and ``external_llm_allowed`` keep their one gate.  Only the consultation
  identity is returned; no answer text is fabricated;
* an agent continuation is a persisted session instruction referencing the
  latest revision at send time;
* a verification task is created ``proposed`` through the 04B engine;
* an observation proposal is a typed declaration that writes nothing.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Annotated, Any, Literal, cast
from uuid import UUID

from fastapi import APIRouter, Header, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator

from cti_app.api.agent_sessions import _disclosure_gate
from cti_app.api.investigation import (
    _composition_factory,
    _http_error,
    _require_verified_human_operator,
    _uow_factory,
)
from cti_app.api.investigation_tasks import _settings
from cti_app.application.agentic_sessions import (
    SessionError,
    SessionService,
    SessionUnitOfWork,
)
from cti_app.application.investigation_advisor_tools import ADVISOR_SCHEMA_VERSION
from cti_app.application.investigation_composition import InvestigationCompositionFactory
from cti_app.application.investigation_tasks import TaskCommandError
from cti_app.application.operator_tool_invocation import OperatorToolInvocationError
from cti_app.application.persistence import UnitOfWork
from cti_app.application.workspace_notes import (
    NoteBridgeReviewOutcomeV1,
    NoteBridgeReviewPort,
    NoteBridgeReviewRequestV1,
    NoteError,
    NoteErrorCode,
    NoteInstructionPort,
    NoteInstructionReceiptV1,
    NoteInstructionRequestV1,
    NotePassageRangeV1,
    WorkspaceNoteService,
    WorkspaceNoteUnitOfWork,
    WorkspaceNoteUnitOfWorkFactory,
)
from cti_app.domain.agentic_operator import (
    AgenticOperatorPort,
    AgentSessionInstructionV1,
)
from cti_app.domain.investigation import ToolInvocationActorKind
from cti_app.domain.investigation_task import (
    ResultVerificationStatus,
    TaskActor,
    TaskActorKind,
    TaskStatus,
)
from cti_app.domain.work_contribution import (
    WorkContributionMethod,
    WorkContributionObjectType,
)
from cti_app.domain.workspace_note_annotation import (
    NoteAnnotationKind,
    NoteIocType,
)

router = APIRouter(prefix="/api/investigations", tags=["investigation-workspace-notes"])

_SHA256_PATTERN = r"^[0-9a-f]{64}$"


class NoteAnnotationRequestV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: NoteAnnotationKind
    task_id: UUID | None = None
    ioc_value: str | None = Field(default=None, max_length=2_048)
    ioc_type: NoteIocType | None = None
    sample_id: UUID | None = None
    detail: str | None = Field(default=None, max_length=4_000)


class NotePassageBodyV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    passage_start: int | None = Field(default=None, ge=0)
    passage_end: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def _validate_pair(self) -> NotePassageBodyV1:
        if (self.passage_start is None) != (self.passage_end is None):
            raise ValueError("passage_start and passage_end are a pair")
        return self

    def resolved(self) -> NotePassageRangeV1 | None:
        if self.passage_start is None or self.passage_end is None:
            return None
        return NotePassageRangeV1(start=self.passage_start, end=self.passage_end)


class AskBridgeRequestV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    consultation_type: Literal["interpretation", "critique"] = "critique"
    question: str = Field(min_length=1, max_length=8_000)
    note_excerpt: str | None = Field(default=None, max_length=20_000)
    passage_start: int | None = Field(default=None, ge=0)
    passage_end: int | None = Field(default=None, ge=1)
    evidence_refs: tuple[UUID, ...] = ()
    finding_refs: tuple[UUID, ...] = ()
    expected_working_state_version: int = Field(ge=1)
    expected_working_state_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def _validate_passage(self) -> AskBridgeRequestV1:
        if (self.passage_start is None) != (self.passage_end is None):
            raise ValueError("passage_start and passage_end are a pair")
        return self


class NoteAskAgentRequestV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: UUID
    session_id: UUID | None = None
    question: str = Field(min_length=1, max_length=8_000)


class NoteVerificationTaskRequestV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    passage_start: int = Field(ge=0)
    passage_end: int = Field(ge=1)
    question: str = Field(min_length=1, max_length=8_000)
    evidence_refs: tuple[UUID, ...] = ()
    claim_refs: tuple[str, ...] = ()
    task_id: UUID | None = None


class NoteObservationProposalRequestV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    object_type: WorkContributionObjectType
    method: WorkContributionMethod
    tool: str = Field(min_length=1, max_length=128)
    tool_version: str = Field(default="unspecified", min_length=1, max_length=64)
    object_id: str | None = Field(default=None, min_length=1, max_length=512)
    parameters: dict[str, Any] = Field(default_factory=dict)
    limitations: tuple[str, ...] = ()
    passage_start: int | None = Field(default=None, ge=0)
    passage_end: int | None = Field(default=None, ge=1)
    observed_at: str | None = None

    @model_validator(mode="after")
    def _validate_passage(self) -> NoteObservationProposalRequestV1:
        if (self.passage_start is None) != (self.passage_end is None):
            raise ValueError("passage_start and passage_end are a pair")
        return self


class NoteAnnotationViewV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID
    investigation_id: UUID
    workspace_id: str
    revision_id: str
    file_relpath: str
    kind: NoteAnnotationKind
    actor_kind: str
    actor_id: str
    target_task_id: UUID | None
    ioc_value: str | None
    ioc_type: NoteIocType | None
    target_sample_id: UUID | None
    detail: str | None
    schema_version: str
    idempotency_key: str
    created_at: str


class NoteRevisionViewV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    revision_id: str
    path: str
    sha256: str
    size: int
    observed_at: str
    origin: str
    supersedes_revision_id: str | None


class NoteContributionViewV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID
    category: str
    import_status: str
    observation_id: UUID | None
    finding_id: UUID | None


class NoteInstructionUsageViewV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    instruction_id: UUID
    session_id: UUID
    state: str
    text_sha256: str


class NoteHandoffUsageViewV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    handoff_id: UUID
    task_id: UUID
    version: int


class NoteConsultationUsageViewV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    invocation_id: UUID
    operation_key: str
    consultation_type: str
    status: str


class NoteIocLinkViewV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ioc_type: NoteIocType
    value: str


class NoteViewResponseV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    investigation_id: UUID
    workspace_id: str
    path: str
    latest: NoteRevisionViewV1
    versions: tuple[NoteRevisionViewV1, ...]
    annotations: tuple[NoteAnnotationViewV1, ...]
    linked_task_ids: tuple[UUID, ...]
    linked_iocs: tuple[NoteIocLinkViewV1, ...]
    linked_sample_ids: tuple[UUID, ...]
    useful: bool
    dismissed: bool
    attributed_authors: tuple[str, ...]
    observation_imported: bool
    is_evidence: bool
    unverified_hypothesis: bool
    contributions: tuple[NoteContributionViewV1, ...]
    instructions: tuple[NoteInstructionUsageViewV1, ...]
    handoffs: tuple[NoteHandoffUsageViewV1, ...]
    consultations: tuple[NoteConsultationUsageViewV1, ...]


class NoteBridgeReviewResponseV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    investigation_id: UUID
    note_revision_id: str
    note_sha256: str
    consultation_type: str
    invocation_id: UUID
    invocation_status: str
    pending: bool
    idempotent_replay: bool
    advisory_only: bool
    is_evidence: bool


class NoteAgentContinuationResponseV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    investigation_id: UUID
    note_revision_id: str
    note_sha256: str
    note_path: str
    task_id: UUID
    session_id: UUID
    instruction_id: UUID
    instruction_state: str
    instruction_sha256: str


class NoteVerificationTaskResponseV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: UUID
    investigation_id: UUID
    task_key: str
    title: str
    status: TaskStatus
    status_version: int
    result_verification_status: ResultVerificationStatus
    created_by_actor_kind: TaskActorKind
    created_by_actor_id: str
    replayed: bool


class NoteObservationProposalResponseV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    investigation_id: UUID
    workspace_id: str
    note_revision_id: str
    note_sha256: str
    note_path: str
    passage: dict[str, int] | None
    object_type: WorkContributionObjectType
    method: WorkContributionMethod
    object_id: str
    tool: str
    tool_version: str
    parameters: dict[str, Any]
    limitations: tuple[str, ...]
    evidence_kind: str
    import_category: str
    import_service: str
    requires_human_decision: bool
    canonical_writes: tuple[str, ...]
    is_evidence: bool


class _OperatorAdvisorReviewPort:
    """Route adapter: the composition reuses the real ``advisor.consult`` path."""

    def __init__(self, factory: InvestigationCompositionFactory) -> None:
        self._factory = factory

    async def request_review(
        self, uow: WorkspaceNoteUnitOfWork, request: NoteBridgeReviewRequestV1
    ) -> NoteBridgeReviewOutcomeV1:
        composition = self._factory.build(
            cast(UnitOfWork, uow), request.investigation_id, operator_mode=True
        )
        arguments: dict[str, Any] = {
            "consultation_type": request.consultation_type,
            "question": request.composed_question,
            "evidence_refs": [str(item) for item in request.evidence_refs],
            "finding_refs": [str(item) for item in request.finding_refs],
        }
        result = await composition.operator_tool_invocations.invoke(
            tool_name="advisor.consult",
            schema_version=ADVISOR_SCHEMA_VERSION,
            arguments=arguments,
            purpose="Revue ciblée d'une note humaine locale non probante (M24-WS2).",
            expected_version=request.expected_working_state_version,
            expected_sha256=request.expected_working_state_sha256,
            idempotency_key=request.idempotency_key,
            actor_id=request.actor_id,
            requester_actor_kind=ToolInvocationActorKind.HUMAN,
        )
        return NoteBridgeReviewOutcomeV1(
            invocation_id=result.invocation.id,
            invocation_status=result.invocation.status.value,
            response_observation_ids=result.invocation.response_refs,
            idempotent_replay=result.idempotent_replay,
        )


class _SessionInstructionPort:
    """Adapter over the public 05B1 session instruction method."""

    def __init__(self, request: Request) -> None:
        self._request = request

    async def enqueue(self, request: NoteInstructionRequestV1) -> NoteInstructionReceiptV1:
        service = SessionService(
            uow_factory=cast(Callable[[], SessionUnitOfWork], _uow_factory(self._request)),
            deriver=None,
            operator=cast(
                AgenticOperatorPort | None,
                getattr(self._request.app.state, "agentic_operator_port", None),
            ),
            # R03b M1: the note continuation can be delivered live to the CLI,
            # so it must pass the same canonical G2/TLP disclosure gate.
            disclosure=_disclosure_gate(self._request),
            task_limits=_settings(self._request).task_limits(),
        )
        instruction: AgentSessionInstructionV1 = await service.enqueue_instruction(
            request.investigation_id,
            request.session_id,
            text=request.text,
            actor=TaskActor(kind=TaskActorKind.HUMAN, actor_id=request.actor_id),
            idempotency_key=request.idempotency_key,
        )
        return NoteInstructionReceiptV1(
            instruction_id=instruction.id,
            session_id=instruction.session_id,
            state=instruction.state.value,
            text_sha256=instruction.text_sha256,
        )


def _note_service(request: Request) -> WorkspaceNoteService:
    configured = getattr(request.app.state, "workspace_note_service", None)
    if configured is not None:
        return cast(WorkspaceNoteService, configured)
    factory = _composition_factory(request)
    return WorkspaceNoteService(
        uow_factory=cast(WorkspaceNoteUnitOfWorkFactory, _uow_factory(request)),
        bridge=cast(NoteBridgeReviewPort, _OperatorAdvisorReviewPort(factory)),
        instructions=cast(NoteInstructionPort, _SessionInstructionPort(request)),
        task_limits=_settings(request).task_limits(),
    )


def _note_error(exc: NoteError) -> Exception:
    return _http_error(exc.status_code, exc.code.value, str(exc))


@router.get(
    "/{investigation_id}/workspace/notes/{note_path:path}/view",
    response_model=NoteViewResponseV1,
)
async def get_workspace_note_view(
    investigation_id: UUID, note_path: str, request: Request
) -> NoteViewResponseV1:
    await _require_verified_human_operator(request)
    try:
        view = await _note_service(request).list_note_view(investigation_id, note_path)
    except NoteError as exc:
        raise _note_error(exc) from exc
    payload = view.to_payload()
    return NoteViewResponseV1.model_validate(payload)


@router.post(
    "/{investigation_id}/workspace/notes/{revision_id}/annotations",
    response_model=NoteAnnotationViewV1,
)
async def annotate_workspace_note(
    investigation_id: UUID,
    revision_id: str,
    body: NoteAnnotationRequestV1,
    request: Request,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=255)],
) -> NoteAnnotationViewV1:
    identity = await _require_verified_human_operator(request)
    try:
        annotation = await _note_service(request).annotate(
            investigation_id,
            revision_id,
            kind=body.kind,
            task_id=body.task_id,
            ioc_value=body.ioc_value,
            ioc_type=body.ioc_type,
            sample_id=body.sample_id,
            detail=body.detail,
            actor_id=identity.actor_id,
            idempotency_key=idempotency_key,
        )
    except NoteError as exc:
        raise _note_error(exc) from exc
    return NoteAnnotationViewV1.model_validate(annotation.to_payload())


@router.post(
    "/{investigation_id}/workspace/notes/{revision_id}/ask-bridge",
    response_model=NoteBridgeReviewResponseV1,
)
async def ask_bridge_to_review_note(
    investigation_id: UUID,
    revision_id: str,
    body: AskBridgeRequestV1,
    request: Request,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=255)],
) -> NoteBridgeReviewResponseV1:
    identity = await _require_verified_human_operator(request)
    try:
        passage = _optional_passage(body.passage_start, body.passage_end)
    except NoteError as exc:
        raise _note_error(exc) from exc
    try:
        result = await _note_service(request).ask_bridge_review(
            investigation_id,
            revision_id,
            consultation_type=body.consultation_type,
            question=body.question,
            note_excerpt=body.note_excerpt,
            passage=passage,
            evidence_refs=body.evidence_refs,
            finding_refs=body.finding_refs,
            expected_working_state_version=body.expected_working_state_version,
            expected_working_state_sha256=body.expected_working_state_sha256,
            actor_id=identity.actor_id,
            idempotency_key=idempotency_key,
        )
    except NoteError as exc:
        raise _note_error(exc) from exc
    except OperatorToolInvocationError as exc:
        raise _http_error(exc.status_code, exc.code, details=exc.details) from exc
    return NoteBridgeReviewResponseV1.model_validate(result.to_payload())


@router.post(
    "/{investigation_id}/workspace/notes/{revision_id}/ask-agent",
    response_model=NoteAgentContinuationResponseV1,
)
async def ask_agent_to_continue_from_note(
    investigation_id: UUID,
    revision_id: str,
    body: NoteAskAgentRequestV1,
    request: Request,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=255)],
) -> NoteAgentContinuationResponseV1:
    identity = await _require_verified_human_operator(request)
    try:
        continuation = await _note_service(request).ask_agent_continue(
            investigation_id,
            revision_id,
            task_id=body.task_id,
            session_id=body.session_id,
            question=body.question,
            actor_id=identity.actor_id,
            idempotency_key=idempotency_key,
        )
    except NoteError as exc:
        raise _note_error(exc) from exc
    except SessionError as exc:
        raise _http_error(exc.status_code, exc.code, str(exc), details=exc.details) from exc
    return NoteAgentContinuationResponseV1.model_validate(continuation.to_payload())


@router.post(
    "/{investigation_id}/workspace/notes/{revision_id}/verification-task",
    response_model=NoteVerificationTaskResponseV1,
)
async def propose_note_verification_task(
    investigation_id: UUID,
    revision_id: str,
    body: NoteVerificationTaskRequestV1,
    request: Request,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=255)],
) -> NoteVerificationTaskResponseV1:
    identity = await _require_verified_human_operator(request)
    try:
        passage = NotePassageRangeV1(start=body.passage_start, end=body.passage_end)
    except NoteError as exc:
        raise _note_error(exc) from exc
    try:
        result = await _note_service(request).propose_verification_task(
            investigation_id,
            revision_id,
            passage=passage,
            question=body.question,
            evidence_refs=body.evidence_refs,
            claim_refs=body.claim_refs,
            task_id=body.task_id,
            actor_id=identity.actor_id,
            idempotency_key=idempotency_key,
        )
    except NoteError as exc:
        raise _note_error(exc) from exc
    except TaskCommandError as exc:
        raise _http_error(exc.status_code, exc.code, str(exc)) from exc
    task = result.task
    return NoteVerificationTaskResponseV1(
        task_id=task.id,
        investigation_id=task.investigation_id,
        task_key=task.task_key,
        title=task.title,
        status=task.status,
        status_version=task.status_version,
        result_verification_status=task.result_verification_status,
        created_by_actor_kind=task.created_by.kind,
        created_by_actor_id=task.created_by.actor_id,
        replayed=result.replayed,
    )


@router.post(
    "/{investigation_id}/workspace/notes/{revision_id}/observation-proposal",
    response_model=NoteObservationProposalResponseV1,
)
async def propose_note_observation_conversion(
    investigation_id: UUID,
    revision_id: str,
    body: NoteObservationProposalRequestV1,
    request: Request,
) -> NoteObservationProposalResponseV1:
    await _require_verified_human_operator(request)
    try:
        passage = _optional_passage(body.passage_start, body.passage_end)
        observed_at = _parse_observed_at(body.observed_at)
    except NoteError as exc:
        raise _note_error(exc) from exc
    try:
        proposal = await _note_service(request).propose_observation_conversion(
            investigation_id,
            revision_id,
            object_type=body.object_type,
            method=body.method,
            tool=body.tool,
            tool_version=body.tool_version,
            object_id=body.object_id,
            parameters=body.parameters,
            limitations=body.limitations,
            passage=passage,
            observed_at=observed_at,
        )
    except NoteError as exc:
        raise _note_error(exc) from exc
    return NoteObservationProposalResponseV1.model_validate(proposal.to_payload())


def _optional_passage(start: int | None, end: int | None) -> NotePassageRangeV1 | None:
    if start is None or end is None:
        return None
    return NotePassageRangeV1(start=start, end=end)


def _parse_observed_at(value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise NoteError(
            NoteErrorCode.INVALID_REQUEST, message="observed_at is not ISO-8601"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise NoteError(NoteErrorCode.INVALID_REQUEST, message="observed_at must be timezone-aware")
    return parsed


__all__ = ["router"]
