"""Adopt one validated editorial proposal with blob-first CAS persistence.

The route remains disabled until the legacy-writer cutover. This application
service contains no gateway dependency: operation-specific inputs are rebuilt
from current canonical state by the injected validator/context provider.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any, Protocol
from uuid import UUID, uuid4

from cti_app.application.blob_storage import BlobStore
from cti_app.application.editorial_enrichment_candidate import (
    DiagramMaterializer,
    EditorialEnrichmentCandidate,
    EnrichmentCandidateBlobReference,
    materialize_candidate_diagrams,
)
from cti_app.application.editorial_enrichment_candidate import (
    build_candidate_from_validated_patch as build_enrichment_candidate,
)
from cti_app.application.editorial_enrichment_candidate import (
    persist_candidate as persist_enrichment_candidate,
)
from cti_app.application.editorial_enrichment_revision import (
    assert_requested_placement,
    targeted_action,
    validate_editorial_enrichment_patch,
    validate_targeted_action,
    validate_targeted_request,
)
from cti_app.application.editorial_proposal_blobs import (
    EditorialProposalBlobError,
    EditorialProposalBlobManager,
)
from cti_app.application.editorial_revision_effects import (
    EditorialConflictReason,
    EditorialRevisionEffects,
    EditorialRevisionEffectsError,
    editorial_actor_storage_id,
    stale_changed_pin_proposals,
)
from cti_app.application.editorial_revision_effects import (
    proposal_fences_match_record as _proposal_fences_match_record,
)
from cti_app.application.editorial_revision_effects import (
    state_event as _state_event,
)
from cti_app.application.editorial_revision_effects import (
    state_request_sha256 as _state_request_sha256,
)
from cti_app.application.editorial_synthesis.candidate import (
    PatchApplicator,
    SynthesisCandidate,
    SynthesisCandidateBlobReference,
    SynthesisCandidatePersistence,
)
from cti_app.application.editorial_synthesis.candidate import (
    build_candidate_from_validated_patch as build_synthesis_candidate,
)
from cti_app.application.editorial_synthesis.candidate import (
    persist_candidate as persist_synthesis_candidate,
)
from cti_app.application.editorial_synthesis.patch_validation import (
    TargetedSynthesisRevisionValidationError,
    parse_wire_patch,
)
from cti_app.application.editorial_synthesis.targets import resolve_target
from cti_app.application.editorial_workspace.registry import (
    WorkspaceOccurrenceResolution,
    WorkspaceSnapshotRecord,
)
from cti_app.application.persistence import UnitOfWork, UnitOfWorkFactory
from cti_app.application.production_artifact_store import (
    MAX_ARTIFACT_BYTES,
    ProductionArtifactStore,
)
from cti_app.application.production_editorial_enrichment import (
    EditorialEnrichmentEvidencePackV1,
    ResourceNeedProposalV1,
)
from cti_app.application.production_stages import EditorialEnrichmentService, SynthesisService
from cti_app.application.production_synthesis import SynthesisEvidencePackV1
from cti_app.domain.blobs import BlobDescriptor, BlobRecord
from cti_app.domain.editorial_proposal import (
    EditorialProposal,
    EditorialProposalEvent,
    EditorialProposalEventKind,
    EditorialProposalState,
)
from cti_app.domain.editorial_proposal_impact import EditorialProposalImpact
from cti_app.domain.editorial_thread import EditorialThreadEventKind
from cti_app.domain.editorial_workspace import (
    AdvancedBlockRegistry,
    BlockRegistryEntry,
    EditorialSourceStage,
)
from cti_app.domain.production import (
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionInputSnapshot,
)
from cti_app.domain.production_editorial_enrichment import (
    EditorialEnrichmentElementKind,
    EditorialEnrichmentV1,
    ResolvedSourceFigureV1,
)
from cti_app.domain.production_editorial_revision import (
    TargetedEditorialRevisionOperation,
    TargetedEditorialRevisionRequest,
    TargetedEditorialRevisionTarget,
    TargetedEditorialRevisionTargetKind,
)
from cti_app.domain.production_extraction import ProductionExtractionV1
from cti_app.domain.production_synthesis import ProductionSynthesisV1
from cti_app.domain.publication_kind import PublicationKind

_IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9_-]{16,128}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class EditorialProposalAdoptionError(ValueError):
    def __init__(
        self,
        code: str,
        *,
        reason: str | EditorialConflictReason | None = None,
        frame: str | None = None,
    ) -> None:
        if code == "proposal_base_stale" and (
            reason is None or re.fullmatch(r"[a-z][a-z0-9_]*:[a-z][a-z0-9_]*", reason) is None
        ):
            raise ValueError("proposal_base_stale requires a closed reason")
        if code in {
            "proposal_state_conflict",
            "editorial_proposal_state_conflict",
            "editorial_registry_conflict",
        }:
            if reason is None:
                raise ValueError("editorial proposal conflict requires a closed reason")
            try:
                reason = EditorialConflictReason(reason)
            except (TypeError, ValueError):
                raise ValueError("editorial proposal conflict requires a closed reason") from None
        caller = sys._getframe(1)
        self.code = code
        self.reason = reason.value if isinstance(reason, EditorialConflictReason) else reason
        self.frame = frame or (
            f"{caller.f_code.co_filename.rsplit('/', 1)[-1]}:"
            f"{caller.f_lineno}:{caller.f_code.co_name}"
        )
        super().__init__(code)


def assert_adoption_rewrite_table_placement(
    *,
    base_enrichment: EditorialEnrichmentV1,
    candidate: EditorialEnrichmentV1,
    element_kind: EditorialEnrichmentElementKind,
    element_key: str,
    operation: TargetedEditorialRevisionOperation,
    is_create: bool,
) -> None:
    """Prevent an adopted table rewrite from changing its existing placement."""
    if (
        is_create
        or operation is not TargetedEditorialRevisionOperation.REWRITE_TABLE
        or element_kind is not EditorialEnrichmentElementKind.TABLE
    ):
        return
    base_table = next((item for item in base_enrichment.tables if item.key == element_key), None)
    if base_table is None:
        raise EditorialProposalAdoptionError(
            "proposal_base_stale", reason="enrichment:table_key_missing"
        )
    if len(candidate.tables) != 1 or candidate.tables[0].placement != base_table.placement:
        raise EditorialProposalAdoptionError("adoption_placement_changed")


@dataclass(frozen=True, slots=True)
class SynthesisCandidateInputs:
    """Current, policy-checked inputs needed by the pure synthesis candidate core."""

    base_payload: dict[str, Any]
    base_synthesis: ProductionSynthesisV1
    target: TargetedEditorialRevisionTarget
    evidence_pack: SynthesisEvidencePackV1
    extraction: ProductionExtractionV1
    snapshot: ProductionInputSnapshot
    apply_patch: PatchApplicator
    persistence: SynthesisCandidatePersistence
    registry_lineage: tuple[Mapping[str, object], ...]
    retired_block_ids: tuple[UUID, ...] = ()
    publication_kind: PublicationKind | None = None
    source_addition_document_ids: frozenset[UUID] = frozenset()
    require_claim_scopes: bool = False


@dataclass(frozen=True, slots=True)
class EnrichmentCandidateInputs:
    """Current evidence and persistence metadata for an enrichment patch."""

    base_payload: dict[str, Any]
    base_enrichment: EditorialEnrichmentV1
    extraction: ProductionExtractionV1
    synthesis: ProductionSynthesisV1
    evidence_pack: EditorialEnrichmentEvidencePackV1
    targeted_request: TargetedEditorialRevisionRequest
    element_kind: EditorialEnrichmentElementKind
    element_key: str
    input_hash: str
    artifact_id: UUID
    figure_catalog: tuple[ResolvedSourceFigureV1, ...] = ()
    registry_lineage: tuple[Mapping[str, object], ...] = ()
    retired_block_ids: tuple[UUID, ...] = ()
    is_create: bool = False
    is_delete: bool = False
    resource_need_proposal: ResourceNeedProposalV1 | None = None
    evidence_pack_hash: str = ""
    access_policy_hash: str = ""
    model_policy_version: str = ""
    routing_policy_version: str = ""
    projection_hash: str | None = None
    source_figure_inventory_hash: str | None = None
    metadata_extra: Mapping[str, Any] | None = None
    resource_search_enabled: bool = False
    diagram_materializer: DiagramMaterializer | None = None
    compiled_chart_asset: Mapping[str, object] | None = None


class EditorialProposalAdoptionInputs(Protocol):
    """Load current evidence and recheck policy fences without external submissions.

    Implementations must recheck thread binding, evidence admission, source pins,
    revocations, TLP and ``external_llm_allowed`` inside the acceptance UOW.
    They must not call a gateway or use model output as evidence.
    """

    async def synthesis_inputs(
        self,
        proposal: EditorialProposal,
        patch: Mapping[str, Any],
        *,
        base_artifact: ProductionArtifact,
        base_payload: dict[str, Any],
        base_bytes: bytes,
        resolution: WorkspaceOccurrenceResolution,
    ) -> SynthesisCandidateInputs: ...

    async def enrichment_inputs(
        self,
        proposal: EditorialProposal,
        patch: Mapping[str, Any],
        *,
        base_artifact: ProductionArtifact,
        base_payload: dict[str, Any],
        base_bytes: bytes,
        resolution: WorkspaceOccurrenceResolution,
    ) -> EnrichmentCandidateInputs: ...

    async def revalidate_evidence_policy_and_thread(
        self,
        uow: UnitOfWork,
        proposal: EditorialProposal,
        patch: Mapping[str, Any],
        *,
        resolution: WorkspaceOccurrenceResolution,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class EditorialProposalAdoptionResult:
    proposal: EditorialProposal
    artifact: ProductionArtifact
    snapshot: WorkspaceSnapshotRecord
    impact: EditorialProposalImpact
    replayed: bool = False


type _Stage = ProductionArtifactStage


class EditorialProposalWorkspace(Protocol):
    async def resolve_occurrence(
        self, subject_id: UUID, occurrence_id: UUID
    ) -> WorkspaceOccurrenceResolution: ...

    async def resolve_occurrence_in_uow(
        self, uow: UnitOfWork, subject_id: UUID, occurrence_id: UUID
    ) -> WorkspaceOccurrenceResolution: ...

    def advance(
        self,
        *,
        base_registry: tuple[BlockRegistryEntry, ...],
        canonical_sha256: str,
        artifact_id: UUID,
        source_stage: str,
        canonical_content: dict[str, Any],
        lineage: tuple[Mapping[str, object], ...],
        retired_block_ids: tuple[UUID, ...],
    ) -> AdvancedBlockRegistry: ...


class EditorialProposalAdoptionService:
    def __init__(
        self,
        *,
        uow_factory: UnitOfWorkFactory,
        blob_store: BlobStore,
        artifact_store: ProductionArtifactStore,
        workspace_service: EditorialProposalWorkspace,
        inputs: EditorialProposalAdoptionInputs,
        synthesis_writer: SynthesisService,
        enrichment_writer: EditorialEnrichmentService,
    ) -> None:
        self._uow_factory = uow_factory
        self._blobs = EditorialProposalBlobManager(uow_factory, blob_store)
        self._artifact_store = artifact_store
        self._workspace_service = workspace_service
        self._inputs = inputs
        self._synthesis_writer = synthesis_writer
        self._enrichment_writer = enrichment_writer
        self._revision_effects = EditorialRevisionEffects(workspace_service=workspace_service)

    async def adopt(
        self,
        subject_id: UUID,
        occurrence_id: UUID,
        proposal_id: UUID,
        *,
        actor_id: str | UUID,
        expected_state_version: int,
        patch_sha256: str,
        idempotency_key: str,
    ) -> EditorialProposalAdoptionResult:
        try:
            actor_id = editorial_actor_storage_id(actor_id)
        except EditorialRevisionEffectsError as error:
            raise EditorialProposalAdoptionError(
                error.code, reason=error.reason, frame=error.frame
            ) from None
        if not _IDEMPOTENCY_KEY.fullmatch(idempotency_key):
            raise EditorialProposalAdoptionError("idempotency_key_invalid")
        if not _SHA256.fullmatch(patch_sha256):
            raise EditorialProposalAdoptionError("proposal_patch_hash_mismatch")
        if expected_state_version < 1:
            raise EditorialProposalAdoptionError(
                "proposal_state_conflict",
                reason=EditorialConflictReason.EXPECTED_STATE_VERSION_INVALID,
            )

        request_sha256 = _decision_request_sha256(
            proposal_id=proposal_id,
            subject_id=subject_id,
            occurrence_id=occurrence_id,
            actor_id=actor_id,
            expected_state_version=expected_state_version,
            patch_sha256=patch_sha256,
        )
        proposal, patch_record, base_artifact, event = await self._load_initial(
            subject_id,
            occurrence_id,
            proposal_id,
            idempotency_key=idempotency_key,
        )
        if event is not None:
            if _matches_accept_event(event, actor_id, request_sha256, patch_sha256):
                return await self._load_replay(proposal)
            raise EditorialProposalAdoptionError("proposal_idempotency_conflict")
        if proposal.state is not EditorialProposalState.PROPOSED:
            raise EditorialProposalAdoptionError(
                "proposal_state_conflict", reason=EditorialConflictReason.PROPOSAL_NOT_PROPOSED
            )
        if proposal.state_version != expected_state_version:
            raise EditorialProposalAdoptionError(
                "proposal_state_conflict",
                reason=EditorialConflictReason.EXPECTED_STATE_VERSION_MISMATCH,
            )
        if proposal.patch_sha256 != patch_sha256 or proposal.patch_blob_ref is None:
            raise EditorialProposalAdoptionError("proposal_patch_hash_mismatch")
        try:
            patch_payload = await self._blobs.read_verified_patch(proposal, patch_record)
        except EditorialProposalBlobError as error:
            raise EditorialProposalAdoptionError(error.code) from None

        resolution = await self._resolve_current(subject_id, occurrence_id)
        if not _proposal_pins_match(proposal, resolution):
            await self._persist_stale(proposal, actor_id=actor_id)
            raise EditorialProposalAdoptionError("proposal_stale")
        stage = _proposal_stage(proposal, patch_payload, resolution)
        if (
            base_artifact is None
            or base_artifact.id != proposal.base_artifact_id
            or base_artifact.production_run_id != proposal.production_run_id
            or base_artifact.subject_id != proposal.subject_id
            or base_artifact.stage is not stage
            or base_artifact.version != proposal.base_artifact_version
            or base_artifact.input_hash != proposal.base_input_hash
            or base_artifact.canonical_blob_id is None
        ):
            raise EditorialProposalAdoptionError("proposal_base_unavailable")
        base_bytes = await self._artifact_store.read_bytes(
            base_artifact.canonical_blob_id, max_bytes=MAX_ARTIFACT_BYTES
        )
        if hashlib.sha256(base_bytes).hexdigest() != proposal.base_canonical_sha256:
            raise EditorialProposalAdoptionError("proposal_base_hash_mismatch")
        try:
            decoded_base = json.loads(base_bytes)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise EditorialProposalAdoptionError("proposal_base_unavailable") from None
        if not isinstance(decoded_base, dict):
            raise EditorialProposalAdoptionError("proposal_base_unavailable")

        plan = await self._build_candidate(
            stage,
            proposal,
            patch_payload,
            base_artifact=base_artifact,
            base_payload=decoded_base,
            base_bytes=base_bytes,
            resolution=resolution,
        )
        try:
            descriptor = await self._blobs.upload_and_verify_candidate(plan.candidate)
        except EditorialProposalBlobError as error:
            raise EditorialProposalAdoptionError(error.code) from None
        try:
            return await self._persist_acceptance(
                subject_id=subject_id,
                occurrence_id=occurrence_id,
                proposal=proposal,
                patch_payload=patch_payload,
                actor_id=actor_id,
                expected_state_version=expected_state_version,
                patch_sha256=patch_sha256,
                idempotency_key=idempotency_key,
                request_sha256=request_sha256,
                stage=stage,
                plan=plan,
                descriptor=descriptor,
            )
        except Exception:
            try:
                await self._blobs.cleanup_orphan_blob(descriptor)
            except Exception:
                pass
            raise

    async def _load_initial(
        self,
        subject_id: UUID,
        occurrence_id: UUID,
        proposal_id: UUID,
        *,
        idempotency_key: str,
    ) -> tuple[
        EditorialProposal,
        BlobRecord | None,
        ProductionArtifact | None,
        EditorialProposalEvent | None,
    ]:
        async with self._uow_factory() as uow:
            proposal = await uow.editorial_proposals.get_for_occurrence(
                subject_id, occurrence_id, proposal_id
            )
            if proposal is None:
                raise EditorialProposalAdoptionError("proposal_not_found")
            event = await uow.editorial_proposals.get_event_by_idempotency(
                proposal_id, idempotency_key
            )
            patch_record = (
                await uow.blobs.get(proposal.patch_blob_ref)
                if proposal.patch_blob_ref is not None
                else None
            )
            artifact = await uow.production_artifacts.get(proposal.base_artifact_id)
            return proposal, patch_record, artifact, event

    async def _resolve_current(
        self, subject_id: UUID, occurrence_id: UUID
    ) -> WorkspaceOccurrenceResolution:
        try:
            resolution = await self._workspace_service.resolve_occurrence(subject_id, occurrence_id)
            return resolution
        except Exception:
            raise EditorialProposalAdoptionError(
                "proposal_base_stale", reason="workspace:occurrence_resolution"
            ) from None

    async def _build_candidate(
        self,
        stage: _Stage,
        proposal: EditorialProposal,
        patch: Mapping[str, Any],
        *,
        base_artifact: ProductionArtifact,
        base_payload: dict[str, Any],
        base_bytes: bytes,
        resolution: WorkspaceOccurrenceResolution,
    ) -> _CandidatePlan:
        if stage is ProductionArtifactStage.SYNTHESIS:
            synthesis_inputs = await self._inputs.synthesis_inputs(
                proposal,
                patch,
                base_artifact=base_artifact,
                base_payload=base_payload,
                base_bytes=base_bytes,
                resolution=resolution,
            )
            if synthesis_inputs.base_payload != base_payload:
                raise EditorialProposalAdoptionError(
                    "proposal_base_stale", reason="base_context:synthesis_payload"
                )
            try:
                operation = TargetedEditorialRevisionOperation(proposal.operation)
            except ValueError:
                raise EditorialProposalAdoptionError("proposal_operation_unsupported") from None
            if synthesis_inputs.persistence.expected_current_artifact_id != base_artifact.id:
                raise EditorialProposalAdoptionError(
                    "proposal_base_stale", reason="fence:synthesis_artifact_id"
                )
            if synthesis_inputs.persistence.expected_current_input_hash != proposal.base_input_hash:
                raise EditorialProposalAdoptionError(
                    "proposal_base_stale", reason="fence:synthesis_input_hash"
                )
            if synthesis_inputs.persistence.input_hash != proposal.base_input_hash:
                raise EditorialProposalAdoptionError(
                    "proposal_base_stale", reason="fence:synthesis_input_hash"
                )
            if synthesis_inputs.persistence.expected_current_canonical_sha256 != (
                proposal.base_canonical_sha256
            ):
                raise EditorialProposalAdoptionError(
                    "proposal_base_stale", reason="fence:synthesis_canonical_sha256"
                )
            if patch.get("target") != synthesis_inputs.target.to_json():
                raise EditorialProposalAdoptionError("proposal_target_mismatch")
            after = patch.get("after")
            rationale = patch.get("rationale")
            if not isinstance(after, str) or not isinstance(rationale, str):
                raise EditorialProposalAdoptionError("proposal_patch_invalid")
            try:
                resolved = resolve_target(base_payload, synthesis_inputs.target, operation)
            except Exception:
                raise EditorialProposalAdoptionError("proposal_target_mismatch") from None
            if resolved.before_hash != proposal.before_hash:
                raise EditorialProposalAdoptionError(
                    "proposal_base_stale", reason="fence:before_hash"
                )
            try:
                wire_patch = parse_wire_patch(
                    "\n".join(
                        (
                            "@@TARGET@@",
                            resolved.path,
                            "@@ACTION@@",
                            operation.value,
                            "@@BASE_ARTIFACT_HASH@@",
                            proposal.base_canonical_sha256,
                            "@@BEFORE_HASH@@",
                            proposal.before_hash,
                            "@@AFTER@@",
                            after,
                            "@@RATIONALE@@",
                            rationale,
                            "@@END@@",
                        )
                    )
                )
            except TargetedSynthesisRevisionValidationError as error:
                raise EditorialProposalAdoptionError(error.reason_code) from None
            if (
                wire_patch.refusal is not None
                or wire_patch.target != resolved.path
                or wire_patch.action is not operation
                or wire_patch.base_artifact_hash != proposal.base_canonical_sha256
                or wire_patch.before_hash != proposal.before_hash
                or wire_patch.after != after
                or wire_patch.rationale != rationale
            ):
                raise EditorialProposalAdoptionError("proposal_patch_invalid")
            try:
                candidate = build_synthesis_candidate(
                    base_artifact=base_artifact,
                    base_payload=base_payload,
                    base_synthesis=synthesis_inputs.base_synthesis,
                    base_canonical_sha256=proposal.base_canonical_sha256,
                    patch=wire_patch,
                    target=synthesis_inputs.target,
                    operation=operation,
                    evidence_pack=synthesis_inputs.evidence_pack,
                    extraction=synthesis_inputs.extraction,
                    snapshot=synthesis_inputs.snapshot,
                    apply_patch=synthesis_inputs.apply_patch,
                    publication_kind=synthesis_inputs.publication_kind,
                    source_addition_document_ids=synthesis_inputs.source_addition_document_ids,
                    require_claim_scopes=synthesis_inputs.require_claim_scopes,
                )
            except Exception as error:
                code = getattr(error, "reason_code", None) or getattr(error, "code", None)
                raise EditorialProposalAdoptionError(
                    str(code or "proposal_patch_invalid")
                ) from None
            persistence = replace(
                synthesis_inputs.persistence,
                run_id=proposal.production_run_id,
                subject_id=proposal.subject_id,
                raw_result=None,
                model_run_id=None,
                artifact_id=synthesis_inputs.persistence.artifact_id or uuid4(),
                expected_current_artifact_id=proposal.base_artifact_id,
                expected_current_input_hash=proposal.base_input_hash,
                expected_current_canonical_sha256=proposal.base_canonical_sha256,
                expected_current_canonical_blob_id=base_artifact.canonical_blob_id,
                base_canonical_bytes_verified=True,
                expected_routing_decision_id=resolution.record.routing_decision_id,
                expected_routing_decision_hash=(
                    resolution.record.routing_decision_hash
                    if resolution.record.routing_decision_id is not None
                    else None
                ),
                stale_stages=frozenset(),
                candidate_blob=None,
            )
            return _SynthesisCandidatePlan(
                candidate=candidate,
                persistence=persistence,
                registry_lineage=synthesis_inputs.registry_lineage,
                retired_block_ids=synthesis_inputs.retired_block_ids,
            )

        enrichment_inputs = await self._inputs.enrichment_inputs(
            proposal,
            patch,
            base_artifact=base_artifact,
            base_payload=base_payload,
            base_bytes=base_bytes,
            resolution=resolution,
        )
        if enrichment_inputs.base_payload != base_payload:
            raise EditorialProposalAdoptionError(
                "proposal_base_stale", reason="base_context:enrichment_payload"
            )
        request = enrichment_inputs.targeted_request
        try:
            operation = TargetedEditorialRevisionOperation(proposal.operation)
        except ValueError:
            raise EditorialProposalAdoptionError("proposal_operation_unsupported") from None
        if request.base_artifact_id != proposal.base_artifact_id:
            raise EditorialProposalAdoptionError(
                "proposal_base_stale", reason="fence:base_artifact_id"
            )
        if request.base_version != proposal.base_artifact_version:
            raise EditorialProposalAdoptionError("proposal_base_stale", reason="fence:base_version")
        if request.base_input_hash != proposal.base_input_hash:
            raise EditorialProposalAdoptionError(
                "proposal_base_stale", reason="fence:base_input_hash"
            )
        if request.base_canonical_sha256 != proposal.base_canonical_sha256:
            raise EditorialProposalAdoptionError(
                "proposal_base_stale", reason="fence:base_canonical_sha256"
            )
        if enrichment_inputs.input_hash != proposal.base_input_hash:
            raise EditorialProposalAdoptionError("proposal_base_stale", reason="fence:input_hash")
        if request.operation is not operation:
            raise EditorialProposalAdoptionError("proposal_base_stale", reason="fence:operation")
        if request.target.element_kind is not enrichment_inputs.element_kind:
            raise EditorialProposalAdoptionError("proposal_base_stale", reason="fence:element_kind")
        if patch.get("target") != request.target.to_json():
            raise EditorialProposalAdoptionError("proposal_target_mismatch")
        raw_enrichment_patch = patch.get("enrichment_patch")
        if not isinstance(raw_enrichment_patch, dict):
            raise EditorialProposalAdoptionError("proposal_patch_invalid")
        try:
            validate_targeted_request(request)
            action = targeted_action(operation)
            validate_targeted_action(enrichment_inputs.element_kind, action)
            validated_patch = validate_editorial_enrichment_patch(
                raw_enrichment_patch,
                element_kind=enrichment_inputs.element_kind,
                element_key=enrichment_inputs.element_key,
                evidence_pack=enrichment_inputs.evidence_pack,
                extraction=enrichment_inputs.extraction,
                synthesis=enrichment_inputs.synthesis,
                figure_catalog=enrichment_inputs.figure_catalog,
            )
            if operation is TargetedEditorialRevisionOperation.CREATE_BAR_CHART:
                asset = enrichment_inputs.compiled_chart_asset
                if (
                    not isinstance(asset, Mapping)
                    or dict(asset) != patch.get("compiled_chart_asset")
                    or len(validated_patch.charts) != 1
                ):
                    raise EditorialProposalAdoptionError("proposal_patch_invalid")
                try:
                    asset_id = UUID(str(asset["asset_id"]))
                except (KeyError, TypeError, ValueError):
                    raise EditorialProposalAdoptionError("proposal_patch_invalid") from None
                validated_patch = replace(
                    validated_patch,
                    charts=(replace(validated_patch.charts[0], compiled_asset_id=asset_id),),
                )
            assert_adoption_rewrite_table_placement(
                base_enrichment=enrichment_inputs.base_enrichment,
                candidate=validated_patch,
                element_kind=enrichment_inputs.element_kind,
                element_key=enrichment_inputs.element_key,
                operation=operation,
                is_create=enrichment_inputs.is_create,
            )
            if enrichment_inputs.is_create:
                assert_requested_placement(
                    candidate=validated_patch,
                    element_kind=enrichment_inputs.element_kind,
                    targeted_request=request,
                    synthesis=enrichment_inputs.synthesis,
                )
            enrichment_candidate = await build_enrichment_candidate(
                base_artifact=base_artifact,
                base_payload=base_payload,
                base_canonical_bytes=base_bytes,
                base_canonical_sha256=proposal.base_canonical_sha256,
                base_enrichment=enrichment_inputs.base_enrichment,
                validated_patch=validated_patch,
                extraction=enrichment_inputs.extraction,
                synthesis=enrichment_inputs.synthesis,
                element_kind=enrichment_inputs.element_kind,
                element_key=enrichment_inputs.element_key,
                action=action,
                input_hash=enrichment_inputs.input_hash,
                artifact_id=enrichment_inputs.artifact_id,
                targeted_request=request,
                is_create=enrichment_inputs.is_create,
                is_delete=enrichment_inputs.is_delete,
                resource_need_proposal=enrichment_inputs.resource_need_proposal,
            )
            if enrichment_candidate.diagram_materialization is not None:
                if enrichment_inputs.diagram_materializer is None:
                    raise EditorialProposalAdoptionError(
                        "editorial_enrichment_diagram_materializer_unavailable"
                    )
                enrichment_candidate = await materialize_candidate_diagrams(
                    enrichment_candidate,
                    materializer=enrichment_inputs.diagram_materializer,
                )
        except EditorialProposalAdoptionError:
            raise
        except Exception as error:
            code = getattr(error, "reason_code", None) or getattr(error, "code", None)
            raise EditorialProposalAdoptionError(str(code or "proposal_patch_invalid")) from None
        return _EnrichmentCandidatePlan(
            candidate=enrichment_candidate,
            inputs=enrichment_inputs,
            registry_lineage=enrichment_inputs.registry_lineage,
            retired_block_ids=enrichment_inputs.retired_block_ids,
        )

    async def _persist_acceptance(
        self,
        *,
        subject_id: UUID,
        occurrence_id: UUID,
        proposal: EditorialProposal,
        patch_payload: Mapping[str, Any],
        actor_id: UUID,
        expected_state_version: int,
        patch_sha256: str,
        idempotency_key: str,
        request_sha256: str,
        stage: _Stage,
        plan: _CandidatePlan,
        descriptor: BlobDescriptor,
    ) -> EditorialProposalAdoptionResult:
        async with self._uow_factory() as uow:
            run_guard = await self._revision_effects.acquire_run_guard(
                uow, proposal.production_run_id
            )
            locked = await uow.editorial_proposals.get_for_occurrence_for_update(
                subject_id, occurrence_id, proposal.id
            )
            if locked is None:
                raise EditorialProposalAdoptionError("proposal_not_found")
            replay_event = await uow.editorial_proposals.get_event_by_idempotency(
                proposal.id, idempotency_key
            )
            if replay_event is not None:
                if _matches_accept_event(replay_event, actor_id, request_sha256, patch_sha256):
                    result = await self._result_for_locked_proposal(uow, locked, replayed=True)
                    return result
                raise EditorialProposalAdoptionError("proposal_idempotency_conflict")
            if (
                locked.state is not EditorialProposalState.PROPOSED
                or locked.state_version != expected_state_version
                or locked.patch_sha256 != patch_sha256
            ):
                raise EditorialProposalAdoptionError(
                    "proposal_state_conflict",
                    reason=EditorialConflictReason.LOCKED_PROPOSAL_STATE_MISMATCH,
                )

            try:
                await self._revision_effects.lock_run_row(uow, run_guard)
            except EditorialRevisionEffectsError as error:
                raise EditorialProposalAdoptionError(
                    "proposal_base_stale", reason="fence:run_lock", frame=error.frame
                ) from None
            try:
                resolution = await self._workspace_service.resolve_occurrence_in_uow(
                    uow, subject_id, occurrence_id
                )
            except Exception:
                resolution = None
            if resolution is None or not _proposal_pins_match(locked, resolution):
                stale_event = _state_event(
                    locked,
                    state=EditorialProposalState.STALE,
                    actor_id=actor_id,
                    idempotency_key=f"stale-{locked.id.hex}-{locked.state_version}",
                    request_sha256=_state_request_sha256(locked, "stale"),
                )
                stale, changed = await uow.editorial_proposals.transition_if_current(
                    subject_id=subject_id,
                    occurrence_id=occurrence_id,
                    proposal_id=locked.id,
                    expected_state=EditorialProposalState.PROPOSED,
                    expected_state_version=locked.state_version,
                    state=EditorialProposalState.STALE,
                    event=stale_event,
                )
                if stale is None or not changed:
                    raise EditorialProposalAdoptionError(
                        "proposal_state_conflict",
                        reason=EditorialConflictReason.STALE_TRANSITION_CAS_CONFLICT,
                    )
                await stale_changed_pin_proposals(
                    uow,
                    production_run_id=stale.production_run_id,
                    actor_id=actor_id,
                    current_record=resolution.record if resolution is not None else None,
                    base_proposal=stale,
                )
                await uow.commit()
                raise EditorialProposalAdoptionError("proposal_stale")

            await self._inputs.revalidate_evidence_policy_and_thread(
                uow, locked, patch_payload, resolution=resolution
            )
            await uow.blobs.lock_address(descriptor.logical_bucket, descriptor.sha256)
            existing_blob = await uow.blobs.get_by_address(
                descriptor.logical_bucket, descriptor.sha256
            )
            if existing_blob is not None and (
                existing_blob.descriptor.sha256 != descriptor.sha256
                or existing_blob.descriptor.size != descriptor.size
                or existing_blob.descriptor.mime_type != descriptor.mime_type
            ):
                raise EditorialProposalAdoptionError("proposal_candidate_blob_hash_mismatch")
            blob = await uow.blobs.add_if_absent(BlobRecord(descriptor=descriptor))
            if (
                blob.descriptor.sha256 != descriptor.sha256
                or blob.descriptor.size != descriptor.size
                or blob.descriptor.mime_type != descriptor.mime_type
                or blob.descriptor.logical_bucket != descriptor.logical_bucket
            ):
                raise EditorialProposalAdoptionError("proposal_candidate_blob_hash_mismatch")

            artifact = await self._persist_candidate(
                uow,
                stage,
                plan,
                blob,
                proposal=locked,
                patch_sha256=patch_sha256,
            )
            decision_id = uuid4()
            accepted = locked.transition(EditorialProposalState.ACCEPTED)
            event = EditorialProposalEvent(
                id=uuid4(),
                proposal_id=locked.id,
                sequence=accepted.state_version,
                kind=EditorialProposalEventKind.STATE_CHANGED,
                state=EditorialProposalState.ACCEPTED,
                state_version=accepted.state_version,
                actor_id=actor_id,
                idempotency_key=idempotency_key,
                request_sha256=request_sha256,
                patch_sha256=patch_sha256,
            )
            stored, changed = await uow.editorial_proposals.transition_if_current(
                subject_id=subject_id,
                occurrence_id=occurrence_id,
                proposal_id=locked.id,
                expected_state=EditorialProposalState.PROPOSED,
                expected_state_version=expected_state_version,
                state=EditorialProposalState.ACCEPTED,
                event=event,
                decision_id=decision_id,
                accepted_artifact_id=artifact.id,
            )
            if stored is None or not changed:
                raise EditorialProposalAdoptionError(
                    "proposal_state_conflict",
                    reason=EditorialConflictReason.ACCEPT_TRANSITION_CAS_CONFLICT,
                )

            if stored.thread_id is not None:
                await uow.editorial_threads.append_event(
                    thread_id=stored.thread_id,
                    kind=EditorialThreadEventKind.ACCEPTED,
                    actor_id=actor_id,
                    idempotency_key=f"proposal-acceptance:{idempotency_key}",
                    request_sha256=request_sha256,
                    proposal_id=stored.id,
                )

            try:
                effects = await self._revision_effects.apply_after_write(
                    uow,
                    run_guard=run_guard,
                    base_snapshot=resolution.record,
                    stage=stage,
                    artifact=artifact,
                    canonical_sha256=plan.candidate.canonical_sha256,
                    canonical_content=(
                        plan.candidate.payload
                        if isinstance(plan, _SynthesisCandidatePlan)
                        else plan.candidate.canonical_payload
                    ),
                    lineage=plan.registry_lineage,
                    retired_block_ids=plan.retired_block_ids,
                    actor_id=actor_id,
                    accepted_proposal=stored,
                )
            except EditorialRevisionEffectsError as error:
                raise EditorialProposalAdoptionError(
                    error.code, reason=error.reason, frame=error.frame
                ) from None
            snapshot = effects.snapshot
            impact = effects.impact
            if impact is None:
                raise EditorialProposalAdoptionError("editorial_impact_missing")
            await uow.commit()
            return EditorialProposalAdoptionResult(
                proposal=stored,
                artifact=artifact,
                snapshot=snapshot,
                impact=impact,
            )

    async def _persist_candidate(
        self,
        uow: UnitOfWork,
        stage: _Stage,
        plan: _CandidatePlan,
        blob: BlobRecord,
        *,
        proposal: EditorialProposal,
        patch_sha256: str,
    ) -> ProductionArtifact:
        revision = {
            "proposal_id": str(proposal.id),
            "occurrence_id": str(proposal.occurrence_id),
            "target_id": proposal.target_id,
            "operation": proposal.operation,
            "base_artifact_id": str(proposal.base_artifact_id),
            "base_artifact_hash": proposal.base_canonical_sha256,
            "patch_sha256": patch_sha256,
            "before_hash": proposal.before_hash,
            "after_hash": plan.candidate.canonical_sha256,
            "result_artifact_id": str(_candidate_artifact_id(plan)),
        }
        if isinstance(plan, _SynthesisCandidatePlan):
            metadata = dict(plan.persistence.metadata_extra or {})
            metadata["editorial_adoption"] = revision
            persistence = replace(
                plan.persistence,
                metadata_extra=metadata,
                candidate_blob=SynthesisCandidateBlobReference(
                    blob_id=blob.id,
                    sha256=plan.candidate.canonical_sha256,
                    size=len(plan.candidate.canonical_bytes),
                ),
                stale_stages=frozenset(),
            )
            return await persist_synthesis_candidate(
                uow,
                writer=self._synthesis_writer,
                candidate=plan.candidate,
                persistence=persistence,
            )
        if stage is not ProductionArtifactStage.EDITORIAL_ENRICHMENT:
            raise EditorialProposalAdoptionError("proposal_operation_unsupported")
        metadata = dict(plan.inputs.metadata_extra or {})
        metadata["editorial_adoption"] = revision
        return await persist_enrichment_candidate(
            uow,
            artifact_store=self._artifact_store,
            persistence_service=self._enrichment_writer,
            candidate=plan.candidate,
            raw_result=None,
            model_run_id=None,
            evidence_pack_hash=plan.inputs.evidence_pack_hash,
            access_policy_hash=plan.inputs.access_policy_hash,
            model_policy_version=plan.inputs.model_policy_version,
            routing_policy_version=plan.inputs.routing_policy_version,
            projection_hash=plan.inputs.projection_hash,
            source_figure_inventory_hash=plan.inputs.source_figure_inventory_hash,
            metadata_extra=metadata,
            resource_search_enabled=plan.inputs.resource_search_enabled,
            candidate_blob=EnrichmentCandidateBlobReference(
                blob_id=blob.id,
                sha256=plan.candidate.canonical_sha256,
                size=len(plan.candidate.canonical_bytes),
            ),
            invalidate_downstream=False,
        )

    async def _persist_stale(self, proposal: EditorialProposal, *, actor_id: UUID) -> None:
        async with self._uow_factory() as uow:
            guard = await self._revision_effects.acquire_run_guard(uow, proposal.production_run_id)
            locked = await uow.editorial_proposals.get_for_occurrence_for_update(
                proposal.subject_id, proposal.occurrence_id, proposal.id
            )
            if locked is None:
                raise EditorialProposalAdoptionError("proposal_not_found")
            try:
                await self._revision_effects.lock_run_row(uow, guard)
            except EditorialRevisionEffectsError as error:
                raise EditorialProposalAdoptionError(
                    "proposal_base_stale", reason="fence:run_lock", frame=error.frame
                ) from None
            if locked.state is not EditorialProposalState.PROPOSED:
                return
            try:
                resolution = await self._workspace_service.resolve_occurrence_in_uow(
                    uow, locked.subject_id, locked.occurrence_id
                )
            except Exception:
                resolution = None
            event = _state_event(
                locked,
                state=EditorialProposalState.STALE,
                actor_id=actor_id,
                idempotency_key=f"stale-{locked.id.hex}-{locked.state_version}",
                request_sha256=_state_request_sha256(locked, "stale"),
            )
            stored, changed = await uow.editorial_proposals.transition_if_current(
                subject_id=locked.subject_id,
                occurrence_id=locked.occurrence_id,
                proposal_id=locked.id,
                expected_state=EditorialProposalState.PROPOSED,
                expected_state_version=locked.state_version,
                state=EditorialProposalState.STALE,
                event=event,
            )
            if stored is None:
                raise EditorialProposalAdoptionError("proposal_not_found")
            if changed:
                await stale_changed_pin_proposals(
                    uow,
                    production_run_id=stored.production_run_id,
                    actor_id=actor_id,
                    current_record=resolution.record if resolution is not None else None,
                    base_proposal=stored,
                )
                await uow.commit()

    async def _load_replay(self, proposal: EditorialProposal) -> EditorialProposalAdoptionResult:
        async with self._uow_factory() as uow:
            locked = await uow.editorial_proposals.get_for_occurrence(
                proposal.subject_id, proposal.occurrence_id, proposal.id
            )
            if locked is None:
                raise EditorialProposalAdoptionError("proposal_not_found")
            return await self._result_for_locked_proposal(uow, locked, replayed=True)

    async def _result_for_locked_proposal(
        self, uow: UnitOfWork, proposal: EditorialProposal, *, replayed: bool
    ) -> EditorialProposalAdoptionResult:
        if (
            proposal.state is not EditorialProposalState.ACCEPTED
            or proposal.accepted_artifact_id is None
        ):
            raise EditorialProposalAdoptionError(
                "proposal_state_conflict",
                reason=EditorialConflictReason.ACCEPTED_PROPOSAL_INVARIANT_CONFLICT,
            )
        artifact = await uow.production_artifacts.get(proposal.accepted_artifact_id)
        impact = await uow.editorial_proposal_impacts.get_by_proposal(proposal.id)
        if artifact is None or impact is None:
            raise EditorialProposalAdoptionError("proposal_acceptance_incomplete")
        snapshot = await uow.editorial_workspace_snapshots.get_by_id(impact.accepted_snapshot_id)
        if snapshot is None:
            raise EditorialProposalAdoptionError("proposal_acceptance_incomplete")
        return EditorialProposalAdoptionResult(
            proposal=proposal,
            artifact=artifact,
            snapshot=snapshot,
            impact=impact,
            replayed=replayed,
        )

    async def cleanup_orphan_blob(self, descriptor: BlobDescriptor) -> bool:
        return await self._blobs.cleanup_orphan_blob(descriptor)


@dataclass(frozen=True, slots=True)
class _SynthesisCandidatePlan:
    candidate: SynthesisCandidate
    persistence: SynthesisCandidatePersistence
    registry_lineage: tuple[Mapping[str, object], ...]
    retired_block_ids: tuple[UUID, ...]


@dataclass(frozen=True, slots=True)
class _EnrichmentCandidatePlan:
    candidate: EditorialEnrichmentCandidate
    inputs: EnrichmentCandidateInputs
    registry_lineage: tuple[Mapping[str, object], ...]
    retired_block_ids: tuple[UUID, ...]


type _CandidatePlan = _SynthesisCandidatePlan | _EnrichmentCandidatePlan


def _candidate_artifact_id(plan: _CandidatePlan) -> UUID:
    if isinstance(plan, _SynthesisCandidatePlan):
        artifact_id = plan.persistence.artifact_id
        if artifact_id is None:
            raise EditorialProposalAdoptionError("proposal_candidate_identity_missing")
        return artifact_id
    return plan.candidate.artifact_id


def _proposal_stage(
    proposal: EditorialProposal,
    patch: Mapping[str, Any],
    resolution: WorkspaceOccurrenceResolution,
) -> ProductionArtifactStage:
    target = patch.get("target")
    if not isinstance(target, Mapping):
        raise EditorialProposalAdoptionError("proposal_patch_invalid")
    target_kind = target.get("kind")
    if target_kind == TargetedEditorialRevisionTargetKind.ENRICHMENT_ELEMENT.value:
        stage = ProductionArtifactStage.EDITORIAL_ENRICHMENT
        placement = target.get("placement")
        is_paragraph_enrichment_create = (
            proposal.operation
            in {
                TargetedEditorialRevisionOperation.CREATE_TABLE.value,
                TargetedEditorialRevisionOperation.CREATE_BAR_CHART.value,
            }
            and target.get("element_kind")
            == (
                EditorialEnrichmentElementKind.TABLE.value
                if proposal.operation == TargetedEditorialRevisionOperation.CREATE_TABLE.value
                else EditorialEnrichmentElementKind.CHART.value
            )
            and target.get("element_key") is None
            and isinstance(placement, Mapping)
            and placement.get("kind") == "after_paragraph"
            and str(placement.get("anchor_block_id")) == str(proposal.occurrence_id)
        )
        if is_paragraph_enrichment_create:
            if (
                resolution.entry is None
                or resolution.entry.source_stage is not EditorialSourceStage.SYNTHESIS
                or resolution.entry.kind.value not in {"paragraph", "lead_paragraph"}
            ):
                raise EditorialProposalAdoptionError("editorial_anchor_stale")
            return stage
    elif target_kind in {
        TargetedEditorialRevisionTargetKind.TITLE.value,
        TargetedEditorialRevisionTargetKind.LEAD.value,
        TargetedEditorialRevisionTargetKind.LEAD_PARAGRAPH.value,
        TargetedEditorialRevisionTargetKind.SECTION.value,
        TargetedEditorialRevisionTargetKind.SECTION_PARAGRAPH.value,
        TargetedEditorialRevisionTargetKind.SECTIONS.value,
    }:
        stage = ProductionArtifactStage.SYNTHESIS
    else:
        raise EditorialProposalAdoptionError("proposal_target_mismatch")
    expected_source = (
        EditorialSourceStage.SYNTHESIS
        if stage is ProductionArtifactStage.SYNTHESIS
        else EditorialSourceStage.EDITORIAL_ENRICHMENT
    )
    if resolution.entry is None or resolution.entry.source_stage is not expected_source:
        raise EditorialProposalAdoptionError("proposal_target_mismatch")
    return stage


def _proposal_pins_match(
    proposal: EditorialProposal, resolution: WorkspaceOccurrenceResolution
) -> bool:
    record = resolution.record
    if (
        resolution.entry is None
        or resolution.binding_state != "valid"
        or not _proposal_fences_match_record(proposal, record)
    ):
        return False
    if proposal.operation in {
        TargetedEditorialRevisionOperation.CREATE_TABLE.value,
        TargetedEditorialRevisionOperation.CREATE_BAR_CHART.value,
    }:
        return (
            resolution.entry.source_stage is EditorialSourceStage.SYNTHESIS
            and resolution.entry.kind.value in {"paragraph", "lead_paragraph"}
            and resolution.artifact_id == record.synthesis_artifact_id
            and resolution.artifact_version == record.synthesis_version
            and resolution.canonical_sha256 == record.synthesis_canonical_sha256
            and record.enrichment_artifact_id == proposal.base_artifact_id
            and record.enrichment_version == proposal.base_artifact_version
            and record.enrichment_input_hash == proposal.base_input_hash
            and record.enrichment_canonical_sha256 == proposal.base_canonical_sha256
        )
    if (
        resolution.artifact_id != proposal.base_artifact_id
        or resolution.artifact_version != proposal.base_artifact_version
        or resolution.canonical_sha256 != proposal.base_canonical_sha256
    ):
        return False
    if resolution.entry.source_stage is EditorialSourceStage.SYNTHESIS:
        return (
            record.synthesis_artifact_id == proposal.base_artifact_id
            and record.synthesis_version == proposal.base_artifact_version
            and record.synthesis_input_hash == proposal.base_input_hash
            and record.synthesis_canonical_sha256 == proposal.base_canonical_sha256
        )
    return (
        record.enrichment_artifact_id == proposal.base_artifact_id
        and record.enrichment_version == proposal.base_artifact_version
        and record.enrichment_input_hash == proposal.base_input_hash
        and record.enrichment_canonical_sha256 == proposal.base_canonical_sha256
    )


def _decision_request_sha256(
    *,
    proposal_id: UUID,
    subject_id: UUID,
    occurrence_id: UUID,
    actor_id: UUID,
    expected_state_version: int,
    patch_sha256: str,
) -> str:
    body = {
        "action": "accept",
        "proposal_id": str(proposal_id),
        "subject_id": str(subject_id),
        "occurrence_id": str(occurrence_id),
        "actor_id": str(actor_id),
        "expected_state_version": expected_state_version,
        "patch_sha256": patch_sha256,
    }
    return hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _matches_accept_event(
    event: EditorialProposalEvent, actor_id: UUID, request_sha256: str, patch_sha256: str
) -> bool:
    return (
        event.kind is EditorialProposalEventKind.STATE_CHANGED
        and event.state is EditorialProposalState.ACCEPTED
        and event.actor_id == actor_id
        and event.request_sha256 == request_sha256
        and event.patch_sha256 == patch_sha256
    )
