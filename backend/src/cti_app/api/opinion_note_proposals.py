"""Bridge note-proposal routes (M24-08B, ADR-0011 D2/D4/D8).

The orchestrator owns ``api/main.py``: this module only exports ``router``.
Drafting requests reuse the existing ``advisor.consult`` path through the
investigation composition, exactly like a human plan request.  Human acceptance
is the only canonical write; it recopies the stored Bridge text and records the
human as the accepter, never as the author.  No route accepts note text from its
body: ``extra="forbid"`` turns a forged ``text``/provenance field into a typed
422 before any service call.
"""

from __future__ import annotations

from typing import Annotated, Literal, cast
from uuid import UUID

from fastapi import APIRouter, Header, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator

from cti_app.api.investigation import (
    _composition_factory,
    _http_error,
    _require_verified_human_operator,
    _uow_factory,
)
from cti_app.application.analyst_opinion_service import (
    AnalystOpinionService,
    OpinionError,
)
from cti_app.application.investigation_advisor_tools import ADVISOR_SCHEMA_VERSION
from cti_app.application.investigation_composition import InvestigationCompositionFactory
from cti_app.application.operator_tool_invocation import OperatorToolInvocationError
from cti_app.application.opinion_note_proposals import (
    AdvisorDraftingOutcomeV1,
    AdvisorDraftingRequestV1,
    NoteProposalError,
    OpinionNoteProposalService,
    advisor_consult_arguments,
)
from cti_app.application.persistence import UnitOfWork, UnitOfWorkFactory
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.domain.analyst_opinion import (
    MAX_CANONICAL_CODE_EXCERPT_BYTES,
    MAX_NOTE_IOC_OBSERVATIONS,
    AnalystObservationRelation,
    AnalystObservationState,
    ArticleElementNoteBlockV1,
    CanonicalCodeNoteBlockV1,
    IocTableNoteBlockV1,
    NoteArticleElementKind,
    NoteContentBlockV1,
    OpinionBlockType,
    ProtocolFigureNoteBlockV1,
    note_content_blocks_to_payload,
)
from cti_app.domain.investigation import ToolInvocationActorKind
from cti_app.domain.opinion_note_proposal import (
    PROSE_BLOCK_TYPES,
    NoteProposalConsultationType,
    NoteProposalStatus,
    NoteProposalV1,
    NoteTargetBlockType,
)
from cti_app.domain.protocol_model import ProtocolDiagramKind

router = APIRouter(prefix="/api/investigations", tags=["investigation-note-proposals"])

_SHA256_PATTERN = r"^[0-9a-f]{64}$"


class NoteArticleElementBlockV1(BaseModel):
    """Reference to one M23 element already rendered in the pinned article."""

    model_config = ConfigDict(extra="forbid")

    block_type: Literal["article_element"]
    kind: NoteArticleElementKind
    element_key: str = Field(min_length=1, max_length=128)
    element_sha256: str = Field(pattern=_SHA256_PATTERN)
    anchor: str = Field(min_length=1, max_length=128)

    def to_domain(self) -> ArticleElementNoteBlockV1:
        return ArticleElementNoteBlockV1(
            kind=self.kind,
            element_key=self.element_key,
            element_sha256=self.element_sha256,
            anchor=self.anchor,
        )


class NoteCanonicalCodeBlockV1(BaseModel):
    """An exact byte range of a canonical observation blob; never supplied text."""

    model_config = ConfigDict(extra="forbid")

    block_type: Literal["canonical_code"]
    observation_id: UUID
    blob_sha256: str = Field(pattern=_SHA256_PATTERN)
    offset: int = Field(ge=0)
    length: int = Field(ge=1, le=MAX_CANONICAL_CODE_EXCERPT_BYTES)
    excerpt_sha256: str = Field(pattern=_SHA256_PATTERN)
    language: str = Field(min_length=1, max_length=32, pattern=r"^[a-z0-9][a-z0-9+.#_-]*$")

    def to_domain(self) -> CanonicalCodeNoteBlockV1:
        return CanonicalCodeNoteBlockV1(
            observation_id=self.observation_id,
            blob_sha256=self.blob_sha256,
            offset=self.offset,
            length=self.length,
            excerpt_sha256=self.excerpt_sha256,
            language=self.language,
        )


class NoteIocTableBlockV1(BaseModel):
    """Rows are derived from the cited canonical observations; the title is derived."""

    model_config = ConfigDict(extra="forbid")

    block_type: Literal["ioc_table"]
    observation_ids: tuple[UUID, ...] = Field(min_length=1, max_length=MAX_NOTE_IOC_OBSERVATIONS)
    title: str = Field(min_length=1, max_length=64)

    def to_domain(self) -> IocTableNoteBlockV1:
        return IocTableNoteBlockV1(observation_ids=self.observation_ids, title=self.title)


class NoteProtocolFigureBlockV1(BaseModel):
    """Reference to one protocol view already carried by the opinion."""

    model_config = ConfigDict(extra="forbid")

    block_type: Literal["protocol_figure"]
    kind: ProtocolDiagramKind
    asset_sha256: str = Field(pattern=_SHA256_PATTERN)

    def to_domain(self) -> ProtocolFigureNoteBlockV1:
        return ProtocolFigureNoteBlockV1(kind=self.kind, asset_sha256=self.asset_sha256)


NoteContentBlockPayloadV1 = Annotated[
    NoteArticleElementBlockV1
    | NoteCanonicalCodeBlockV1
    | NoteIocTableBlockV1
    | NoteProtocolFigureBlockV1,
    Field(discriminator="block_type"),
]


class ResolvedNoteArticleElementBlockV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    block_type: Literal["article_element"]
    kind: NoteArticleElementKind
    element_key: str
    element_sha256: str
    anchor: str
    title: str | None
    caption: str | None


class ResolvedNoteCanonicalCodeBlockV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    block_type: Literal["canonical_code"]
    observation_id: UUID
    blob_sha256: str
    offset: int
    length: int
    excerpt_sha256: str
    language: str
    excerpt: str
    observation_source: str
    observation_object_type: str
    observation_object_id: str


class ResolvedNoteIocTableRowV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    observation_id: UUID
    object_type: str
    object_id: str
    source: str
    observed_at: str
    blob_sha256: str


class ResolvedNoteIocTableBlockV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    block_type: Literal["ioc_table"]
    observation_ids: tuple[UUID, ...]
    title: str
    rows: tuple[ResolvedNoteIocTableRowV1, ...]


class ResolvedNoteProtocolFigureBlockV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    block_type: Literal["protocol_figure"]
    kind: ProtocolDiagramKind
    asset_sha256: str
    caption: str


ResolvedNoteContentBlockV1 = Annotated[
    ResolvedNoteArticleElementBlockV1
    | ResolvedNoteCanonicalCodeBlockV1
    | ResolvedNoteIocTableBlockV1
    | ResolvedNoteProtocolFigureBlockV1,
    Field(discriminator="block_type"),
]


def _domain_content_blocks(
    blocks: tuple[NoteContentBlockPayloadV1, ...],
) -> tuple[NoteContentBlockV1, ...]:
    try:
        return tuple(block.to_domain() for block in blocks)
    except ValueError as exc:
        raise _http_error(422, "invalid_note_content_block", str(exc)) from exc


class NoteProposalRequestV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    anchor: str = Field(min_length=1, max_length=128)
    target_block_type: NoteTargetBlockType
    bridge_block_type: OpinionBlockType
    consultation_type: NoteProposalConsultationType = NoteProposalConsultationType.DRAFT_TEXT
    note_state: AnalystObservationState = AnalystObservationState.UNRESOLVED
    note_relation: AnalystObservationRelation = AnalystObservationRelation.UNRESOLVED
    finding_refs: tuple[UUID, ...] = ()
    observation_refs: tuple[UUID, ...] = ()
    content_blocks: tuple[NoteContentBlockPayloadV1, ...] = ()
    base_opinion_version: int | None = Field(default=None, ge=1)
    base_opinion_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    task_id: UUID | None = None
    question: str | None = Field(default=None, max_length=8_000)
    expected_working_state_version: int = Field(ge=1)
    expected_working_state_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def _validate_target(self) -> NoteProposalRequestV1:
        if self.bridge_block_type not in PROSE_BLOCK_TYPES:
            raise ValueError("bridge_block_type must be a publishable opinion block")
        if (self.base_opinion_version is None) != (self.base_opinion_sha256 is None):
            raise ValueError("base_opinion_version and base_opinion_sha256 are a pair")
        return self


class NoteProposalTransitionV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_status_version: int = Field(ge=1)
    reason: str = Field(default="Transition Bridge note proposal", min_length=1, max_length=4_000)


class NoteProposalIngestV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_status_version: int = Field(ge=1)


class NoteProposalAcceptV1(BaseModel):
    """Explicit version/hash acceptance; the body never carries prose."""

    model_config = ConfigDict(extra="forbid")

    expected_status_version: int = Field(ge=1)
    expected_base_version: int | None = Field(default=None, ge=1)
    expected_base_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    reason: str = Field(default="Accept the Bridge note proposal", min_length=1, max_length=4_000)


class NoteProposalViewV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID
    investigation_id: UUID
    opinion_id: UUID
    task_id: UUID | None
    anchor: str
    target_block_type: NoteTargetBlockType
    bridge_block_type: OpinionBlockType
    consultation_type: NoteProposalConsultationType
    base_note_id: UUID | None
    note_state: AnalystObservationState
    note_relation: AnalystObservationRelation
    cited_finding_refs: tuple[UUID, ...]
    cited_observation_refs: tuple[UUID, ...]
    proposed_text: str | None
    proposed_text_sha256: str | None
    bridge_invocation_id: str | None
    bridge_output_sha256: str | None
    bridge_response_observation_id: UUID | None
    tool_invocation_id: UUID | None
    base_opinion_version: int | None
    base_opinion_sha256: str | None
    base_publication_artifact_id: UUID | None
    base_publication_version: int | None
    base_publication_sha256: str | None
    anchor_before_sha256: str | None
    content_blocks: tuple[NoteContentBlockPayloadV1, ...]
    status: NoteProposalStatus
    status_version: int
    accepted_opinion_version: int | None
    analyst_decision_id: UUID | None
    idempotency_key: str
    materialised: bool
    created_at: str
    updated_at: str

    @classmethod
    def from_domain(cls, proposal: NoteProposalV1) -> NoteProposalViewV1:
        return cls.model_validate(proposal.to_payload())


class NoteProposalDiffViewV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    proposal: NoteProposalViewV1
    current_opinion_version: int | None
    current_opinion_sha256: str | None
    current_note_id: UUID | None
    current_note_text: str | None
    proposed_text: str | None
    text_changed: bool
    anchor: str
    target_block_type: NoteTargetBlockType
    anchor_excerpt: str | None
    cited_finding_refs: tuple[UUID, ...]
    cited_observation_refs: tuple[UUID, ...]
    content_blocks: tuple[NoteContentBlockPayloadV1, ...]
    resolved_content_blocks: tuple[ResolvedNoteContentBlockV1, ...]
    content_blocks_valid: bool
    content_block_error: str | None
    limits: tuple[str, ...]
    stale: bool
    accept_ready: bool


class NoteProposalRequestResponseV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    proposal: NoteProposalViewV1
    invocation_id: UUID
    invocation_status: str
    idempotent_replay: bool
    materialised: bool


class NoteAcceptanceViewV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    proposal: NoteProposalViewV1
    opinion_id: UUID
    opinion_version: int
    opinion_sha256: str
    decision_id: UUID
    note_id: UUID
    idempotent_replay: bool


def _artifact_store(request: Request) -> ProductionArtifactStore:
    store = getattr(request.app.state, "production_artifact_store", None)
    if store is None:
        raise _http_error(503, "opinion_unavailable")
    return cast(ProductionArtifactStore, store)


def _proposal_service(request: Request) -> OpinionNoteProposalService:
    configured = getattr(request.app.state, "opinion_note_proposal_service", None)
    if configured is not None:
        return cast(OpinionNoteProposalService, configured)
    factory = _composition_factory(request)
    return OpinionNoteProposalService(
        uow_factory=cast(UnitOfWorkFactory, _uow_factory(request)),
        artifact_store=_artifact_store(request),
        article_snapshots=factory.article_snapshots,
    )


def _opinion_service(request: Request) -> AnalystOpinionService:
    configured = getattr(request.app.state, "analyst_opinion_service", None)
    if configured is not None:
        return cast(AnalystOpinionService, configured)
    return AnalystOpinionService(
        uow_factory=cast(UnitOfWorkFactory, _uow_factory(request)),
        artifact_store=_artifact_store(request),
    )


class _OperatorAdvisorDraftingPort:
    """Route adapter: the composition reuses the real ``advisor.consult`` path."""

    def __init__(self, factory: InvestigationCompositionFactory) -> None:
        self._factory = factory

    async def request_draft(
        self, uow: UnitOfWork, request: AdvisorDraftingRequestV1
    ) -> AdvisorDraftingOutcomeV1:
        composition = self._factory.build(uow, request.investigation_id, operator_mode=True)
        result = await composition.operator_tool_invocations.invoke(
            tool_name="advisor.consult",
            schema_version=ADVISOR_SCHEMA_VERSION,
            arguments=advisor_consult_arguments(request),
            purpose=request.purpose,
            expected_version=request.expected_working_state_version,
            expected_sha256=request.expected_working_state_sha256,
            idempotency_key=request.idempotency_key,
            actor_id=request.actor_id,
            requester_actor_kind=request.requester_actor_kind,
            context_task_id=request.context_task_id,
        )
        return AdvisorDraftingOutcomeV1(
            invocation_id=result.invocation.id,
            invocation_status=result.invocation.status,
            response_observation_ids=result.invocation.response_refs,
            idempotent_replay=result.idempotent_replay,
        )


def _proposal_error(exc: NoteProposalError) -> Exception:
    return _http_error(exc.status_code, exc.code.value, str(exc))


def _opinion_error(exc: OpinionError) -> Exception:
    status_code = 422
    if exc.code.value in {"note_proposal_not_found", "opinion_not_found"}:
        status_code = 404
    elif exc.code.value in {
        "stale_note_proposal",
        "stale_version",
        "stale_base",
        "note_anchor_text_changed",
        "note_block_ref_stale",
        "note_code_ref_stale",
        "invalid_opinion_state",
    }:
        status_code = 409
    return _http_error(status_code, exc.code.value, str(exc))


def _is_system_request(request: Request) -> bool:
    """Internal system ingestion only; the public route stays human-gated."""
    return getattr(request.app.state, "note_proposal_system_ingest", False) is True


@router.get("/{investigation_id}/note-proposals", response_model=list[NoteProposalViewV1])
async def list_note_proposals(
    investigation_id: UUID,
    request: Request,
    status: NoteProposalStatus | None = None,
) -> list[NoteProposalViewV1]:
    await _require_verified_human_operator(request)
    try:
        proposals = await _proposal_service(request).list(investigation_id, status=status)
    except NoteProposalError as exc:
        raise _proposal_error(exc) from exc
    return [NoteProposalViewV1.from_domain(item) for item in proposals]


@router.get(
    "/{investigation_id}/note-proposals/{proposal_id}",
    response_model=NoteProposalDiffViewV1,
)
async def get_note_proposal(
    investigation_id: UUID, proposal_id: UUID, request: Request
) -> NoteProposalDiffViewV1:
    await _require_verified_human_operator(request)
    try:
        diff = await _proposal_service(request).get(investigation_id, proposal_id)
    except NoteProposalError as exc:
        raise _proposal_error(exc) from exc
    return NoteProposalDiffViewV1(
        proposal=NoteProposalViewV1.from_domain(diff.proposal),
        current_opinion_version=diff.current_opinion_version,
        current_opinion_sha256=diff.current_opinion_sha256,
        current_note_id=diff.current_note_id,
        current_note_text=diff.current_note_text,
        proposed_text=diff.proposal.proposed_text,
        text_changed=diff.text_changed,
        anchor=diff.proposal.anchor,
        target_block_type=diff.proposal.target_block_type,
        anchor_excerpt=diff.anchor_excerpt,
        cited_finding_refs=diff.proposal.cited_finding_refs,
        cited_observation_refs=diff.proposal.cited_observation_refs,
        content_blocks=note_content_blocks_to_payload(diff.proposal.content_blocks),
        resolved_content_blocks=[item.to_payload() for item in diff.resolved_content_blocks],
        content_blocks_valid=diff.content_blocks_valid,
        content_block_error=diff.content_block_error,
        limits=diff.limits,
        stale=diff.stale,
        accept_ready=diff.accept_ready,
    )


@router.post(
    "/{investigation_id}/note-proposals",
    response_model=NoteProposalRequestResponseV1,
)
async def request_note_proposal(
    investigation_id: UUID,
    body: NoteProposalRequestV1,
    request: Request,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=255)],
) -> NoteProposalRequestResponseV1:
    identity = await _require_verified_human_operator(request)
    factory = _composition_factory(request)
    content_blocks = _domain_content_blocks(body.content_blocks)
    try:
        result = await _proposal_service(request).request(
            investigation_id,
            anchor=body.anchor,
            target_block_type=body.target_block_type,
            bridge_block_type=body.bridge_block_type,
            consultation_type=body.consultation_type,
            note_state=body.note_state,
            note_relation=body.note_relation,
            finding_refs=body.finding_refs,
            observation_refs=body.observation_refs,
            content_blocks=content_blocks,
            base_opinion_version=body.base_opinion_version,
            base_opinion_sha256=body.base_opinion_sha256,
            task_id=body.task_id,
            question=body.question,
            expected_working_state_version=body.expected_working_state_version,
            expected_working_state_sha256=body.expected_working_state_sha256,
            actor_id=identity.actor_id,
            requester_actor_kind=ToolInvocationActorKind.HUMAN,
            idempotency_key=idempotency_key,
            advisor=_OperatorAdvisorDraftingPort(factory),
        )
    except NoteProposalError as exc:
        raise _proposal_error(exc) from exc
    except OperatorToolInvocationError as exc:
        raise _http_error(exc.status_code, exc.code, details=exc.details) from exc
    return NoteProposalRequestResponseV1(
        proposal=NoteProposalViewV1.from_domain(result.proposal),
        invocation_id=result.invocation_id,
        invocation_status=result.invocation_status,
        idempotent_replay=result.idempotent_replay,
        materialised=result.materialised,
    )


@router.post(
    "/{investigation_id}/note-proposals/{proposal_id}/ingest",
    response_model=NoteProposalViewV1,
)
async def ingest_note_proposal_response(
    investigation_id: UUID,
    proposal_id: UUID,
    body: NoteProposalIngestV1,
    request: Request,
) -> NoteProposalViewV1:
    if _is_system_request(request):
        actor = "system:note-proposal-ingest"
    else:
        identity = await _require_verified_human_operator(request)
        actor = identity.actor_id
    try:
        proposal = await _proposal_service(request).ingest_response(
            investigation_id,
            proposal_id,
            expected_status_version=body.expected_status_version,
            actor=actor,
        )
    except NoteProposalError as exc:
        raise _proposal_error(exc) from exc
    return NoteProposalViewV1.from_domain(proposal)


@router.post(
    "/{investigation_id}/note-proposals/{proposal_id}/accept",
    response_model=NoteAcceptanceViewV1,
)
async def accept_note_proposal(
    investigation_id: UUID,
    proposal_id: UUID,
    body: NoteProposalAcceptV1,
    request: Request,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=255)],
) -> NoteAcceptanceViewV1:
    identity = await _require_verified_human_operator(request)
    try:
        acceptance = await _opinion_service(request).accept_note_proposal(
            investigation_id,
            proposal_id,
            expected_status_version=body.expected_status_version,
            expected_base_version=body.expected_base_version,
            expected_base_sha256=body.expected_base_sha256,
            actor=identity.actor_id,
            reason=body.reason,
            idempotency_key=idempotency_key,
        )
    except OpinionError as exc:
        raise _opinion_error(exc) from exc
    return NoteAcceptanceViewV1(
        proposal=NoteProposalViewV1.from_domain(acceptance.proposal),
        opinion_id=acceptance.opinion.id,
        opinion_version=acceptance.opinion.version,
        opinion_sha256=acceptance.opinion.content_sha256,
        decision_id=acceptance.decision.id,
        note_id=acceptance.decision.target_id,
        idempotent_replay=acceptance.idempotent_replay,
    )


@router.post(
    "/{investigation_id}/note-proposals/{proposal_id}/reject",
    response_model=NoteProposalViewV1,
)
async def reject_note_proposal(
    investigation_id: UUID,
    proposal_id: UUID,
    body: NoteProposalTransitionV1,
    request: Request,
) -> NoteProposalViewV1:
    identity = await _require_verified_human_operator(request)
    try:
        proposal = await _proposal_service(request).reject(
            investigation_id,
            proposal_id,
            expected_status_version=body.expected_status_version,
            actor=identity.actor_id,
            reason=body.reason,
        )
    except NoteProposalError as exc:
        raise _proposal_error(exc) from exc
    return NoteProposalViewV1.from_domain(proposal)


@router.post(
    "/{investigation_id}/note-proposals/{proposal_id}/cancel",
    response_model=NoteProposalViewV1,
)
async def cancel_note_proposal(
    investigation_id: UUID,
    proposal_id: UUID,
    body: NoteProposalTransitionV1,
    request: Request,
) -> NoteProposalViewV1:
    identity = await _require_verified_human_operator(request)
    try:
        proposal = await _proposal_service(request).cancel(
            investigation_id,
            proposal_id,
            expected_status_version=body.expected_status_version,
            actor=identity.actor_id,
            reason=body.reason,
        )
    except NoteProposalError as exc:
        raise _proposal_error(exc) from exc
    return NoteProposalViewV1.from_domain(proposal)


__all__ = ["router"]
