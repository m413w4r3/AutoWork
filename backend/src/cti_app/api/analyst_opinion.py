"""Editorial API for reviewing and publishing investigation opinions."""

from __future__ import annotations

import hashlib
import json
from typing import Any, cast
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic_core import PydanticCustomError

from cti_app.application.analyst_opinion_preview import AnalystOpinionPreviewService
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
from cti_app.application.diagram_compilation import DiagramCompilationError
from cti_app.application.identity import IdentityProvider
from cti_app.application.investigation_article_snapshot import ProductionArticleSnapshotAdapter
from cti_app.application.media_assets import MediaAssetStore
from cti_app.application.network_protocol_model import render_protocol_model
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.application.protocol_diagram_projection import (
    ProtocolDiagramProjectionError,
    compile_protocol_render_result,
    opinion_protocol_figures,
)
from cti_app.application.publication_augmentation import PublicationAugmentationArtifactService
from cti_app.application.publication_rendering import PublicationRenderError
from cti_app.application.typst_compilation import TypstCompilationError
from cti_app.domain.analyst_opinion import (
    AnalystObservationRelation,
    AnalystObservationState,
    AnalystObservationV1,
    AnalystOpinionV1,
    BridgeDraftReferenceV1,
    BridgeReviewReferenceV1,
    ImplantMaterialEditorialV1,
    ImplantMaterialType,
    OpinionBlock,
    OpinionBlockType,
    OpinionClaimQualification,
    OpinionProtocolFigureV1,
)
from cti_app.domain.protocol_model import ProtocolDiagramKind, ProtocolModelV1
from cti_app.infrastructure.d2_diagram_compiler import D2DiagramCompiler
from cti_app.logging import get_correlation_id

router = APIRouter(prefix="/api", tags=["analyst-opinion"])


class OpinionSelectionPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    finding_ids: tuple[UUID, ...] = ()
    claim_ids: tuple[str, ...] = ()
    block_types: tuple[OpinionBlockType, ...] = tuple(
        item
        for item in OpinionBlockType
        if item not in {OpinionBlockType.ANALYST_OBSERVATIONS, OpinionBlockType.IMPLANT_MATERIALS}
    )


class ComposeOpinionPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    selection: OpinionSelectionPayload = Field(default_factory=OpinionSelectionPayload)


class BridgeDraftReferencePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    invocation_id: str = Field(min_length=1, max_length=128)
    text_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class BridgeReviewReferencePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    invocation_id: str = Field(min_length=1, max_length=128)
    output_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reviewed_text_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class OpinionBlockEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    block_type: OpinionBlockType
    text: str
    finding_refs: tuple[UUID, ...] = ()
    evidence_refs: tuple[UUID, ...] = ()
    qualification: OpinionClaimQualification = OpinionClaimQualification.FACT
    certainty: float = Field(default=0.5, ge=0, le=1)
    bridge_authored: bool = False
    bridge_draft_ref: BridgeDraftReferencePayload | None = None
    bridge_review_ref: BridgeReviewReferencePayload | None = None

    @field_validator("block_type")
    @classmethod
    def reject_editorial_projection_types(cls, value: OpinionBlockType) -> OpinionBlockType:
        if value is OpinionBlockType.ANALYST_OBSERVATIONS:
            raise ValueError("Editorial projection blocks are not direct operator edits")
        return value

    @model_validator(mode="after")
    def validate_bridge_refs(self) -> OpinionBlockEdit:
        text_sha256 = hashlib.sha256(self.text.encode("utf-8")).hexdigest()
        if self.bridge_draft_ref is not None and self.bridge_draft_ref.text_sha256 != text_sha256:
            raise ValueError("Bridge draft reference does not match opinion text")
        if self.bridge_authored and self.bridge_draft_ref is None:
            raise ValueError("Bridge-authored opinion blocks require a draft reference")
        if self.bridge_review_ref is not None:
            if self.bridge_draft_ref is None:
                raise ValueError("Bridge review reference requires a Bridge draft reference")
            if self.bridge_review_ref.reviewed_text_sha256 != text_sha256:
                raise PydanticCustomError("review_text_stale", "review_text_stale")
        return self

    def to_domain(self) -> OpinionBlock:
        return OpinionBlock(
            block_type=self.block_type,
            text=self.text,
            finding_refs=self.finding_refs,
            evidence_refs=self.evidence_refs,
            qualification=self.qualification,
            certainty=self.certainty,
            bridge_authored=self.bridge_authored,
            bridge_draft_ref=(
                BridgeDraftReferenceV1(**self.bridge_draft_ref.model_dump())
                if self.bridge_draft_ref is not None
                else None
            ),
            bridge_review_ref=(
                BridgeReviewReferenceV1(**self.bridge_review_ref.model_dump())
                if self.bridge_review_ref is not None
                else None
            ),
        )


class ImplantMaterialEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: ImplantMaterialType
    value: str = Field(min_length=1, max_length=4096)
    role: str = Field(min_length=1, max_length=300)
    sample_location: str = Field(min_length=1, max_length=500)
    sample_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    evidence_observation_id: UUID

    def to_domain(self) -> ImplantMaterialEditorialV1:
        return ImplantMaterialEditorialV1(
            type=self.type,
            value=self.value,
            role=self.role,
            sample_location=self.sample_location,
            sample_sha256=self.sample_sha256,
            evidence_observation_id=self.evidence_observation_id,
        )


class OpinionObservationEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID
    anchor: str = Field(min_length=1, max_length=128)
    text: str = Field(min_length=1, max_length=6000)
    state: AnalystObservationState
    relation: AnalystObservationRelation
    finding_refs: tuple[UUID, ...] = ()
    evidence_refs: tuple[UUID, ...] = ()

    def to_domain(self) -> AnalystObservationV1:
        return AnalystObservationV1(
            id=self.id,
            anchor=self.anchor,
            text=self.text,
            state=self.state,
            relation=self.relation,
            finding_refs=self.finding_refs,
            evidence_refs=self.evidence_refs,
        )


class ReviseOpinionPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(ge=1)
    expected_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    blocks: tuple[OpinionBlockEdit, ...]
    unresolved_points: tuple[str, ...] = ()
    observations: tuple[OpinionObservationEdit, ...] | None = None
    implant_materials: tuple[ImplantMaterialEdit, ...] | None = None
    protocol_model_blob_id: UUID | None = None
    protocol_views: tuple[ProtocolDiagramKind, ...] | None = Field(default=None, max_length=3)


class OpinionReviewPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = Field(ge=1)
    expected_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reason: str = Field(min_length=1, max_length=4000)


class OpinionPublishPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = Field(ge=1)
    expected_base_publication_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


def _opinion_preview_service(request: Request) -> AnalystOpinionPreviewService:
    configured = getattr(request.app.state, "analyst_opinion_preview_service", None)
    if configured is not None:
        return cast(AnalystOpinionPreviewService, configured)
    renderer = getattr(request.app.state, "publication_render_service", None)
    if renderer is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "opinion_preview_rendering_unavailable"},
        )
    return AnalystOpinionPreviewService(
        uow_factory=request.app.state.uow_factory,
        artifact_store=request.app.state.production_artifact_store,
        renderer=renderer,
    )


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
        OpinionErrorCode.REVIEW_TEXT_STALE: status.HTTP_422_UNPROCESSABLE_CONTENT,
        OpinionErrorCode.INVALID_OBSERVATION_ANCHOR: status.HTTP_422_UNPROCESSABLE_CONTENT,
        OpinionErrorCode.CITATIONS_INVALID: status.HTTP_422_UNPROCESSABLE_CONTENT,
        OpinionErrorCode.OPINION_NOT_DRAFT: status.HTTP_409_CONFLICT,
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
    implant_materials_section = item.implant_materials_section
    item_payload = item.to_payload()
    item_payload["blocks"] = [
        {
            "type": block.block_type.value,
            "text": block.text,
            "finding_refs": [str(ref) for ref in block.finding_refs],
            "evidence_refs": [str(ref) for ref in block.evidence_refs],
            "qualification": block.qualification.value,
            "certainty": block.certainty,
        }
        for block in item.blocks
        if block.block_type
        not in {OpinionBlockType.ANALYST_OBSERVATIONS, OpinionBlockType.IMPLANT_MATERIALS}
    ]
    return {
        **item_payload,
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
        "observations": [
            {
                "id": str(observation.id),
                "anchor": observation.anchor,
                "text": observation.text,
                "state": observation.state.value,
                "relation": observation.relation.value,
                "finding_refs": [str(ref) for ref in observation.finding_refs],
                "evidence_refs": [str(ref) for ref in observation.evidence_refs],
            }
            for observation in item.observations
        ],
        "implant_materials": [material.to_payload() for material in item.implant_materials],
        "implant_materials_section": (
            {
                "type": OpinionBlockType.IMPLANT_MATERIALS.value,
                "text": implant_materials_section.text,
                "bridge_authored": implant_materials_section.bridge_authored,
                "bridge_draft_ref": (
                    implant_materials_section.bridge_draft_ref.to_payload()
                    if implant_materials_section.bridge_draft_ref is not None
                    else None
                ),
                "bridge_review_ref": (
                    implant_materials_section.bridge_review_ref.to_payload()
                    if implant_materials_section.bridge_review_ref is not None
                    else None
                ),
            }
            if implant_materials_section is not None
            else None
        ),
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
    if payload.observations is not None and payload.expected_sha256 is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"code": "expected_opinion_sha256_required_for_observation_edits"},
        )
    actor_id = await _actor_id(request)
    protocol_figures = None
    if payload.protocol_model_blob_id is not None:
        selected_views = payload.protocol_views
        if selected_views is not None and (
            len(set(selected_views)) != len(selected_views)
            or set(selected_views) != set(ProtocolDiagramKind)
        ):
            raise HTTPException(
                status_code=422,
                detail={"code": "invalid_protocol_views"},
            )
        protocol_figures = await _compile_opinion_protocol_figures(
            request,
            opinion_id=opinion_id,
            blob_id=payload.protocol_model_blob_id,
            selected_views=selected_views,
        )
    elif payload.protocol_views is not None:
        raise HTTPException(status_code=422, detail={"code": "protocol_model_required"})
    edits = OpinionRevisionEdits(
        blocks=tuple(block.to_domain() for block in payload.blocks),
        unresolved_points=payload.unresolved_points,
        observations=(
            tuple(item.to_domain() for item in payload.observations)
            if payload.observations is not None
            else None
        ),
        implant_materials=(
            tuple(item.to_domain() for item in payload.implant_materials)
            if payload.implant_materials is not None
            else None
        ),
        protocol_figures=protocol_figures,
    )
    try:
        opinion = await _opinion_service(request).revise(
            opinion_id,
            payload.expected_version,
            edits,
            actor_id,
            expected_sha256=payload.expected_sha256,
        )
    except OpinionError as exc:
        _raise_opinion_error(exc)
    return {"draft": _opinion_payload(opinion)}


@router.get("/opinions/{opinion_id}/preview.pdf")
async def preview_opinion_pdf(
    opinion_id: UUID,
    request: Request,
    version: int = Query(ge=1),
    opinion_sha256: str = Query(pattern=r"^[0-9a-f]{64}$"),
    base_publication_sha256: str = Query(pattern=r"^[0-9a-f]{64}$"),
    protocol_model_blob_id: UUID | None = Query(default=None),
) -> Response:
    try:
        protocol_figures = (
            await _compile_opinion_protocol_figures(
                request,
                opinion_id=opinion_id,
                blob_id=protocol_model_blob_id,
                selected_views=None,
            )
            if protocol_model_blob_id is not None
            else None
        )
        preview = await _opinion_preview_service(request).preview_pdf(
            opinion_id,
            version=version,
            expected_opinion_sha256=opinion_sha256,
            expected_base_publication_sha256=base_publication_sha256,
            protocol_figures=protocol_figures,
        )
    except OpinionError as exc:
        _raise_opinion_error(exc)
    except (PublicationRenderError, TypstCompilationError) as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": getattr(exc, "code", "opinion_preview_render_failed")},
        ) from exc
    return Response(
        content=preview.content,
        media_type="application/pdf",
        headers={
            "Content-Disposition": (
                f'attachment; filename="opinion-{preview.opinion_id}-v'
                f'{preview.opinion_version}-draft.pdf"'
            ),
            "X-PDF-SHA256": preview.sha256,
            "X-Opinion-Version": str(preview.opinion_version),
            "X-Opinion-Content-SHA256": preview.opinion_sha256,
            "X-Base-Publication-SHA256": preview.base_publication_sha256,
            "X-Opinion-Status": "draft-unapproved",
            "X-PDF-Lineage": json.dumps(
                preview.lineage_manifest, ensure_ascii=True, separators=(",", ":")
            ),
        },
    )


async def _compile_opinion_protocol_figures(
    request: Request,
    *,
    opinion_id: UUID,
    blob_id: UUID,
    selected_views: tuple[ProtocolDiagramKind, ...] | None,
) -> tuple[OpinionProtocolFigureV1, ...]:
    uow_factory = getattr(request.app.state, "uow_factory", None)
    artifact_store = getattr(request.app.state, "production_artifact_store", None)
    if uow_factory is None or artifact_store is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "protocol_figure_attachment_unavailable"},
        )

    async with uow_factory() as uow:
        opinion = await uow.analyst_opinions.get(opinion_id)
        if opinion is None:
            raise HTTPException(status_code=404, detail={"code": "opinion_not_found"})
        if opinion.status.value != "draft":
            raise HTTPException(status_code=409, detail={"code": "opinion_not_editable"})
        investigation = await uow.analyst_investigations.get(opinion.investigation_id)
        blob = await uow.blobs.get(blob_id)
        observations = await uow.evidence_observations.list_for_investigation(
            opinion.investigation_id
        )
        belongs = (
            blob is not None
            and blob.descriptor.logical_bucket == "network-protocol-models"
            and any(
                item.object_type == "protocol_model" and item.object_id == blob.descriptor.sha256
                for item in observations
            )
        )
        if investigation is None or not belongs:
            raise HTTPException(
                status_code=404,
                detail={"code": "protocol_model_not_in_investigation"},
            )
        run_id = investigation.production_run_id

    raw = await cast(ProductionArtifactStore, artifact_store).read_json(blob_id)
    model = ProtocolModelV1.from_dict(raw)
    if model.investigation_id != opinion.investigation_id or model.subject_id != str(
        investigation.subject_id
    ):
        raise HTTPException(
            status_code=404,
            detail={"code": "protocol_model_not_in_investigation"},
        )
    media_asset_store = getattr(request.app.state, "media_asset_store", None)
    if media_asset_store is None:
        media_asset_store = MediaAssetStore(
            cast(ProductionArtifactStore, artifact_store), uow_factory
        )
    try:
        rendered = await compile_protocol_render_result(
            render_protocol_model(model),
            compiler=D2DiagramCompiler(),
            media_asset_store=cast(MediaAssetStore, media_asset_store),
            production_run_id=run_id,
        )
    except (DiagramCompilationError, ProtocolDiagramProjectionError) as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "protocol_diagram_rendering_unavailable"},
        ) from exc
    figures = opinion_protocol_figures(rendered)
    if selected_views is not None and set(selected_views) != set(ProtocolDiagramKind):
        raise HTTPException(status_code=422, detail={"code": "invalid_protocol_views"})
    return figures


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
