"""Editorial API for reviewing and publishing investigation opinions."""

from __future__ import annotations

from typing import Any, cast
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field

from cti_app.application.analyst_opinion_service import (
    AnalystOpinionDraftView,
    AnalystOpinionService,
    FindingServiceClaimKnowledgeProvider,
    OpinionCompositionSelection,
    OpinionError,
    OpinionErrorCode,
    OpinionRevisionEdits,
)
from cti_app.application.analyst_publication_service import AnalystPublicationService
from cti_app.application.identity import IdentityProvider
from cti_app.application.investigation_article_snapshot import ProductionArticleSnapshotAdapter
from cti_app.application.publication_augmentation import PublicationAugmentationArtifactService
from cti_app.domain.analyst_opinion import (
    AnalystOpinionV1,
    OpinionBlock,
    OpinionBlockType,
    OpinionClaimQualification,
)
from cti_app.logging import get_correlation_id

router = APIRouter(prefix="/api", tags=["analyst-opinion"])


class OpinionSelectionPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    finding_ids: tuple[UUID, ...] = ()
    claim_ids: tuple[str, ...] = ()
    block_types: tuple[OpinionBlockType, ...] = tuple(OpinionBlockType)


class ComposeOpinionPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    selection: OpinionSelectionPayload = Field(default_factory=OpinionSelectionPayload)


class OpinionBlockEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    block_type: OpinionBlockType
    text: str
    finding_refs: tuple[UUID, ...] = ()
    evidence_refs: tuple[UUID, ...] = ()
    qualification: OpinionClaimQualification = OpinionClaimQualification.FACT
    certainty: float = Field(default=0.5, ge=0, le=1)

    def to_domain(self) -> OpinionBlock:
        return OpinionBlock(
            block_type=self.block_type,
            text=self.text,
            finding_refs=self.finding_refs,
            evidence_refs=self.evidence_refs,
            qualification=self.qualification,
            certainty=self.certainty,
        )


class ReviseOpinionPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(ge=1)
    blocks: tuple[OpinionBlockEdit, ...]
    unresolved_points: tuple[str, ...] = ()


class OpinionReviewPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = Field(ge=1)
    expected_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reason: str = Field(min_length=1, max_length=4000)


class OpinionPublishPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = Field(ge=1)
    expected_base_publication_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


def _opinion_service(request: Request) -> AnalystOpinionService:
    configured = getattr(request.app.state, "analyst_opinion_service", None)
    if configured is not None:
        return cast(AnalystOpinionService, configured)
    uow_factory = request.app.state.uow_factory
    artifact_store = request.app.state.production_artifact_store
    provider = getattr(request.app.state, "claim_knowledge_provider", None)
    if provider is None:
        provider = FindingServiceClaimKnowledgeProvider(
            uow_factory=uow_factory,
            article_snapshots=ProductionArticleSnapshotAdapter(uow_factory, artifact_store),
        )
    return AnalystOpinionService(
        uow_factory=uow_factory,
        artifact_store=artifact_store,
        claim_knowledge_provider=provider,
    )


def _publication_service(request: Request) -> AnalystPublicationService:
    configured = getattr(request.app.state, "analyst_publication_service", None)
    if configured is not None:
        return cast(AnalystPublicationService, configured)
    uow_factory = request.app.state.uow_factory
    artifact_store = request.app.state.production_artifact_store
    augmentation = PublicationAugmentationArtifactService(
        uow_factory=uow_factory,
        artifact_store=artifact_store,
    )
    return AnalystPublicationService(
        uow_factory=uow_factory,
        artifact_store=artifact_store,
        augmentation_service=augmentation,
    )


async def _actor_id(request: Request) -> str:
    provider: IdentityProvider = request.app.state.identity_provider
    return (await provider.current()).actor_id


def _require_idempotency_key(value: str | None) -> str:
    if value is None or not value.strip():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"code": "idempotency_key_required"},
        )
    if len(value) > 128:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"code": "invalid_idempotency_key"},
        )
    return value.strip()


def _raise_opinion_error(exc: OpinionError) -> None:
    status_code = {
        OpinionErrorCode.OPINION_NOT_FOUND: status.HTTP_404_NOT_FOUND,
        OpinionErrorCode.INVESTIGATION_NOT_FOUND: status.HTTP_404_NOT_FOUND,
        OpinionErrorCode.TLP_FORBIDDEN: status.HTTP_403_FORBIDDEN,
        OpinionErrorCode.DO_NOT_SUBMIT: status.HTTP_403_FORBIDDEN,
        OpinionErrorCode.INVALID_SELECTION: status.HTTP_422_UNPROCESSABLE_CONTENT,
        OpinionErrorCode.CITATIONS_INVALID: status.HTTP_422_UNPROCESSABLE_CONTENT,
        OpinionErrorCode.INVALID_STATE: status.HTTP_422_UNPROCESSABLE_CONTENT,
        OpinionErrorCode.OPINION_NOT_APPROVED: status.HTTP_409_CONFLICT,
        OpinionErrorCode.STALE_VERSION: status.HTTP_409_CONFLICT,
        OpinionErrorCode.VERSION_HASH_MISMATCH: status.HTTP_409_CONFLICT,
        OpinionErrorCode.STALE_BASE: status.HTTP_409_CONFLICT,
        OpinionErrorCode.BASE_INVALID: status.HTTP_409_CONFLICT,
        OpinionErrorCode.BASE_IDENTITY_MISMATCH: status.HTTP_409_CONFLICT,
        OpinionErrorCode.SYNTHESIS_IDENTITY_MISMATCH: status.HTTP_409_CONFLICT,
        OpinionErrorCode.IDEMPOTENCY_CONFLICT: status.HTTP_409_CONFLICT,
    }[exc.code]
    detail: dict[str, Any] = {"code": exc.code.value}
    if exc.citation_violations:
        detail["citation_violations"] = [
            {
                "code": item.code.value,
                "block_type": item.block_type.value,
                "reference_id": str(item.reference_id) if item.reference_id else None,
                "message": item.message,
            }
            for item in exc.citation_violations
        ]
    raise HTTPException(status_code=status_code, detail=detail) from exc


def _opinion_payload(opinion: AnalystOpinionV1 | None) -> dict[str, Any] | None:
    if opinion is None:
        return None
    item = opinion
    return {
        **item.to_payload(),
        "renderable_blocks": [
            {
                "type": block.block_type.value,
                "text": block.text,
                "finding_refs": [str(ref) for ref in block.finding_refs],
                "evidence_refs": [str(ref) for ref in block.evidence_refs],
                "qualification": block.qualification.value,
                "certainty": block.certainty,
            }
            for block in item.renderable_blocks
        ],
        "provenance": {
            "investigation_id": str(item.investigation_id),
            "synthesis_artifact_id": str(item.synthesis_artifact_id),
            "synthesis_sha256": item.synthesis_sha256,
            "base_publication_artifact_id": str(item.base_publication_artifact_id),
            "base_publication_sha256": item.base_publication_sha256,
        },
    }


def _draft_view_payload(view: AnalystOpinionDraftView) -> dict[str, Any]:
    opinion_payload = _opinion_payload(view.opinion)
    return {
        "draft": opinion_payload,
        "history": [_opinion_payload(item) for item in view.history],
        "citation_violations": [
            {
                "code": item.code.value,
                "block_type": item.block_type.value,
                "reference_id": str(item.reference_id) if item.reference_id else None,
                "message": item.message,
            }
            for item in view.citation_violations
        ],
        "renderable_blocks": opinion_payload["renderable_blocks"] if opinion_payload else [],
        "provenance": opinion_payload["provenance"] if opinion_payload else None,
    }


@router.get("/investigations/{investigation_id}/opinion")
async def get_investigation_opinion(investigation_id: UUID, request: Request) -> dict[str, Any]:
    try:
        view = await _opinion_service(request).get_draft(investigation_id)
    except OpinionError as exc:
        _raise_opinion_error(exc)
    return _draft_view_payload(view)


@router.post("/investigations/{investigation_id}/opinion/compose")
async def compose_investigation_opinion(
    investigation_id: UUID,
    request: Request,
    payload: ComposeOpinionPayload,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, Any]:
    _require_idempotency_key(idempotency_key)
    selection = OpinionCompositionSelection(
        finding_ids=payload.selection.finding_ids,
        claim_ids=payload.selection.claim_ids,
        block_types=payload.selection.block_types,
    )
    try:
        opinion = await _opinion_service(request).compose_from_findings(investigation_id, selection)
    except OpinionError as exc:
        _raise_opinion_error(exc)
    return {"draft": _opinion_payload(opinion)}


@router.post("/opinions/{opinion_id}/revise")
async def revise_opinion(
    opinion_id: UUID,
    request: Request,
    payload: ReviseOpinionPayload,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, Any]:
    _require_idempotency_key(idempotency_key)
    edits = OpinionRevisionEdits(
        blocks=tuple(block.to_domain() for block in payload.blocks),
        unresolved_points=payload.unresolved_points,
    )
    try:
        opinion = await _opinion_service(request).revise(
            opinion_id,
            payload.expected_version,
            edits,
            await _actor_id(request),
        )
    except OpinionError as exc:
        _raise_opinion_error(exc)
    return {"draft": _opinion_payload(opinion)}


@router.post("/opinions/{opinion_id}/approve")
async def approve_opinion(
    opinion_id: UUID,
    request: Request,
    payload: OpinionReviewPayload,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, Any]:
    key = _require_idempotency_key(idempotency_key)
    try:
        opinion = await _opinion_service(request).approve(
            opinion_id,
            payload.version,
            payload.expected_sha256,
            await _actor_id(request),
            payload.reason,
            correlation_id=get_correlation_id(),
            idempotency_key=key,
        )
    except OpinionError as exc:
        _raise_opinion_error(exc)
    return {
        "approved": _opinion_payload(opinion),
        "reviewed_version": payload.version,
        "approved_version": opinion.version,
    }


@router.post("/opinions/{opinion_id}/request-changes")
async def request_opinion_changes(
    opinion_id: UUID,
    request: Request,
    payload: OpinionReviewPayload,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, Any]:
    key = _require_idempotency_key(idempotency_key)
    try:
        opinion = await _opinion_service(request).request_changes(
            opinion_id,
            payload.version,
            payload.expected_sha256,
            await _actor_id(request),
            payload.reason,
            correlation_id=get_correlation_id(),
            idempotency_key=key,
        )
    except OpinionError as exc:
        _raise_opinion_error(exc)
    return {"opinion": _opinion_payload(opinion), "changes_requested": True}


@router.post("/opinions/{opinion_id}/publish")
async def publish_opinion(
    opinion_id: UUID,
    request: Request,
    payload: OpinionPublishPayload,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, Any]:
    _require_idempotency_key(idempotency_key)
    try:
        artifact = await _publication_service(request).publish_augmented_article(
            opinion_id,
            payload.version,
            await _actor_id(request),
            payload.expected_base_publication_sha256,
        )
    except OpinionError as exc:
        _raise_opinion_error(exc)
    return {
        "publication": {
            "artifact_id": str(artifact.id),
            "run_id": str(artifact.production_run_id),
            "subject_id": str(artifact.subject_id),
            "version": artifact.version,
            "sha256": artifact.input_hash,
            "previous_publication_artifact_id": artifact.metadata.get(
                "previous_publication_artifact_id"
            ),
        },
        "composition_repin_required": True,
        "message": (
            "Nouvelle PUBLICATION créée. Le réépinglage dans une composition reste "
            "une action de l'éditeur."
        ),
    }


__all__ = ["router"]
