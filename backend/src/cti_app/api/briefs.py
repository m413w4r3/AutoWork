from __future__ import annotations

from typing import Literal, NoReturn
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field

from cti_app.application.briefs import (
    BriefError,
    BriefNotFoundError,
    BriefService,
    brief_generation_idempotency_key,
)
from cti_app.application.identity import IdentityProvider
from cti_app.application.jobs import DuplicateJobError, JobDispatcher, JobService
from cti_app.domain.briefs import BriefDraft, BriefEvidencePack
from cti_app.domain.editorial import HumanDecisionType
from cti_app.logging import get_correlation_id

router = APIRouter(prefix="/api/subjects/{subject_id}/brief", tags=["briefs"])


class GenerateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: Literal["qwen", "openai"] = "qwen"


class RegenerateRequest(GenerateRequest):
    instruction: str | None = Field(default=None, max_length=2_000)


class ChangesRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    note: str = Field(min_length=1, max_length=4_000)


class BriefLaunchView(BaseModel):
    job_id: UUID
    duplicate: bool


class EditBlockRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sentence_texts: list[str] = Field(min_length=1, max_length=30)


class PackView(BaseModel):
    id: UUID
    version: int
    content_hash: str
    object_hashes: list[str]
    source_count: int
    claim_count: int
    indicator_count: int
    entity_count: int
    uncertainty_count: int
    created_by: str


class SentenceView(BaseModel):
    id: UUID
    text: str
    factual: bool
    claim_ids: list[UUID]
    indicator_ids: list[UUID]
    evidence: list[dict[str, object]]


class BlockView(BaseModel):
    id: UUID
    sentences: list[SentenceView]


class DraftSummaryView(BaseModel):
    id: UUID
    version: int
    pack_id: UUID
    pack_hash: str
    title: str
    provider: str
    stale: bool


class BriefView(BaseModel):
    subject_id: UUID
    pack: PackView | None
    draft: DraftSummaryView | None
    blocks: list[BlockView]
    limits: list[str]
    references: list[dict[str, object]]
    versions: list[DraftSummaryView]
    status: Literal["empty", "draft", "changes_requested", "approved", "promoted", "stale"]
    qa: dict[str, bool]
    qa_errors: list[str]
    diff: str


@router.get("", response_model=BriefView)
async def get_brief(subject_id: UUID, request: Request) -> BriefView:
    return await _view(subject_id, request)


@router.post("/freeze", response_model=BriefView)
async def freeze_pack(subject_id: UUID, request: Request) -> BriefView:
    try:
        await _service(request).freeze(subject_id, actor_id=await _actor_id(request))
        return await _view(subject_id, request)
    except Exception as exc:
        _raise_api_error(exc)


@router.post("/generate", response_model=BriefLaunchView, status_code=status.HTTP_202_ACCEPTED)
async def generate_brief(
    subject_id: UUID, payload: GenerateRequest, request: Request
) -> BriefLaunchView:
    try:
        return await _launch_generation(
            subject_id,
            request,
            provider=payload.provider,
            block_id=None,
            instruction=None,
        )
    except Exception as exc:
        _raise_api_error(exc)


@router.post(
    "/blocks/{block_id}/regenerate",
    response_model=BriefLaunchView,
    status_code=status.HTTP_202_ACCEPTED,
)
async def regenerate_block(
    subject_id: UUID, block_id: UUID, payload: RegenerateRequest, request: Request
) -> BriefLaunchView:
    try:
        return await _launch_generation(
            subject_id,
            request,
            provider=payload.provider,
            block_id=block_id,
            instruction=payload.instruction,
        )
    except Exception as exc:
        _raise_api_error(exc)


@router.patch("/blocks/{block_id}", response_model=BriefView)
async def edit_block(
    subject_id: UUID, block_id: UUID, payload: EditBlockRequest, request: Request
) -> BriefView:
    try:
        await _service(request).revise_block(subject_id, block_id, payload.sentence_texts)
        return await _view(subject_id, request)
    except Exception as exc:
        _raise_api_error(exc)


@router.post("/request-changes", response_model=BriefView)
async def request_changes(subject_id: UUID, payload: ChangesRequest, request: Request) -> BriefView:
    try:
        await _service(request).request_changes(
            subject_id,
            note=payload.note,
            actor_id=await _actor_id(request),
            correlation_id=get_correlation_id(),
        )
        return await _view(subject_id, request)
    except Exception as exc:
        _raise_api_error(exc)


@router.post("/approve", response_model=BriefView)
async def approve(subject_id: UUID, request: Request) -> BriefView:
    try:
        await _service(request).approve(
            subject_id,
            actor_id=await _actor_id(request),
            correlation_id=get_correlation_id(),
        )
        return await _view(subject_id, request)
    except Exception as exc:
        _raise_api_error(exc)


@router.post("/promote", response_model=BriefView)
async def promote(subject_id: UUID, request: Request) -> BriefView:
    try:
        await _service(request).promote(
            subject_id,
            actor_id=await _actor_id(request),
            correlation_id=get_correlation_id(),
        )
        return await _view(subject_id, request)
    except Exception as exc:
        _raise_api_error(exc)


@router.get("/export.md")
async def export_markdown(subject_id: UUID, request: Request) -> Response:
    try:
        return Response(
            await _service(request).markdown(subject_id),
            media_type="text/markdown; charset=utf-8",
            headers={"Content-Disposition": 'attachment; filename="brief.md"'},
        )
    except Exception as exc:
        _raise_api_error(exc)


async def _view(subject_id: UUID, request: Request) -> BriefView:
    service = _service(request)
    pack, draft, versions, qa, diff = await service.view(subject_id)
    decisions = await service.decisions(pack.edition_id) if pack else []
    status_value: Literal[
        "empty", "draft", "changes_requested", "approved", "promoted", "stale"
    ] = "empty"
    if draft is not None:
        status_value = "stale" if pack is None or draft.pack_id != pack.id else "draft"
        relevant = [
            item
            for item in decisions
            if item.payload.get("draft_id") == str(draft.id)
            and item.decision_type
            in {
                HumanDecisionType.BRIEF_CHANGES_REQUESTED,
                HumanDecisionType.BRIEF_APPROVE,
                HumanDecisionType.BRIEF_PROMOTE,
            }
        ]
        if relevant and status_value != "stale":
            status_by_decision: dict[
                HumanDecisionType,
                Literal["changes_requested", "approved", "promoted"],
            ] = {
                HumanDecisionType.BRIEF_CHANGES_REQUESTED: "changes_requested",
                HumanDecisionType.BRIEF_APPROVE: "approved",
                HumanDecisionType.BRIEF_PROMOTE: "promoted",
            }
            status_value = status_by_decision[relevant[-1].decision_type]
    claims = {str(item["id"]): item for item in pack.claims} if pack else {}
    sources = {str(item["id"]): item for item in pack.sources} if pack else {}
    return BriefView(
        subject_id=subject_id,
        pack=_pack_view(pack) if pack else None,
        draft=_draft_summary(draft, pack) if draft else None,
        blocks=[
            BlockView(
                id=block.id,
                sentences=[
                    SentenceView(
                        id=sentence.id,
                        text=sentence.text,
                        factual=sentence.factual,
                        claim_ids=list(sentence.claim_ids),
                        indicator_ids=list(sentence.indicator_ids),
                        evidence=[
                            claims[str(item)] for item in sentence.claim_ids if str(item) in claims
                        ],
                    )
                    for sentence in block.sentences
                ],
            )
            for block in draft.blocks
        ]
        if draft
        else [],
        limits=list(draft.limits) if draft else [],
        references=[sources[str(item)] for item in draft.source_ids if str(item) in sources]
        if draft
        else [],
        versions=[_draft_summary(item, pack) for item in versions],
        status=status_value,
        qa=qa.checks if qa else {},
        qa_errors=qa.errors if qa else [],
        diff=diff,
    )


def _pack_view(pack: BriefEvidencePack) -> PackView:
    return PackView(
        id=pack.id,
        version=pack.version,
        content_hash=pack.content_hash,
        object_hashes=list(pack.object_hashes),
        source_count=len(pack.sources),
        claim_count=len(pack.claims),
        indicator_count=len(pack.indicators),
        entity_count=len(pack.normalized_entities),
        uncertainty_count=len(pack.uncertainties),
        created_by=pack.created_by,
    )


def _draft_summary(draft: BriefDraft, current_pack: BriefEvidencePack | None) -> DraftSummaryView:
    return DraftSummaryView(
        id=draft.id,
        version=draft.version,
        pack_id=draft.pack_id,
        pack_hash=draft.pack_hash,
        title=draft.title,
        provider=draft.provider,
        stale=current_pack is None or current_pack.id != draft.pack_id,
    )


def _service(request: Request) -> BriefService:
    service: BriefService = request.app.state.brief_service
    return service


async def _actor_id(request: Request) -> str:
    provider: IdentityProvider = request.app.state.identity_provider
    return (await provider.current()).actor_id


async def _launch_generation(
    subject_id: UUID,
    request: Request,
    *,
    provider: Literal["qwen", "openai"],
    block_id: UUID | None,
    instruction: str | None,
) -> BriefLaunchView:
    service = _service(request)
    actor_id = await _actor_id(request)
    pack = await service.freeze(subject_id, actor_id=actor_id)
    _, previous, _, _, _ = await service.view(subject_id)
    jobs: JobService = request.app.state.job_service
    dispatcher: JobDispatcher = request.app.state.job_dispatcher
    key = brief_generation_idempotency_key(
        subject_id,
        pack.content_hash,
        previous.id if previous else None,
        provider,
        block_id,
        instruction,
    )
    duplicate = False
    try:
        job = await jobs.submit(
            kind="brief.generate",
            aggregate_type="subject",
            aggregate_id=subject_id,
            idempotency_key=key,
            correlation_id=get_correlation_id(),
            input_parameters={
                "subject_id": str(subject_id),
                "actor_id": actor_id,
                "provider": provider,
                "block_id": str(block_id) if block_id else None,
                "instruction": instruction,
            },
            max_attempts=2,
            actor_id=actor_id,
        )
        await dispatcher.dispatch(job.id)
    except DuplicateJobError as exc:
        job = await jobs.get(exc.existing_job_id)
        duplicate = True
    return BriefLaunchView(job_id=job.id, duplicate=duplicate)


def _raise_api_error(exc: Exception) -> NoReturn:
    if isinstance(exc, BriefNotFoundError):
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if isinstance(exc, (BriefError, ValueError)):
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    raise exc
