"""Adopt one validated editorial proposal with blob-first CAS persistence.

The route remains disabled until the legacy-writer cutover. This application
service contains no gateway dependency: operation-specific inputs are rebuilt
from current canonical state by the injected validator/context provider.
"""

from __future__ import annotations

import hashlib
import json
import re
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
    registry_hash,
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
    workspace_pin_hash,
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
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


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

    async def adopt(
        self,
        subject_id: UUID,
        occurrence_id: UUID,
        proposal_id: UUID,
        *,
        actor_id: UUID,
        expected_state_version: int,
        patch_sha256: str,
        idempotency_key: str,
    ) -> EditorialProposalAdoptionResult:
        if not _IDEMPOTENCY_KEY.fullmatch(idempotency_key):
            raise EditorialProposalAdoptionError("idempotency_key_invalid")
        if not _SHA256.fullmatch(patch_sha256):
            raise EditorialProposalAdoptionError("proposal_patch_hash_mismatch")
        if expected_state_version < 1:
            raise EditorialProposalAdoptionError("proposal_state_conflict")

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
            raise EditorialProposalAdoptionError("proposal_state_conflict")
        if proposal.state_version != expected_state_version:
            raise EditorialProposalAdoptionError("proposal_state_conflict")
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
            raise EditorialProposalAdoptionError("proposal_base_stale") from None

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
                raise EditorialProposalAdoptionError("proposal_base_stale")
            try:
                operation = TargetedEditorialRevisionOperation(proposal.operation)
            except ValueError:
                raise EditorialProposalAdoptionError("proposal_operation_unsupported") from None
            if synthesis_inputs.persistence.expected_current_artifact_id != base_artifact.id:
                raise EditorialProposalAdoptionError("proposal_base_stale")
            if synthesis_inputs.persistence.expected_current_input_hash != proposal.base_input_hash:
                raise EditorialProposalAdoptionError("proposal_base_stale")
            if synthesis_inputs.persistence.input_hash != proposal.base_input_hash:
                raise EditorialProposalAdoptionError("proposal_base_stale")
            if synthesis_inputs.persistence.expected_current_canonical_sha256 != (
                proposal.base_canonical_sha256
            ):
                raise EditorialProposalAdoptionError("proposal_base_stale")
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
                raise EditorialProposalAdoptionError("proposal_base_stale")
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
                expected_routing_decision_hash=resolution.record.routing_decision_hash,
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
            raise EditorialProposalAdoptionError("proposal_base_stale")
        request = enrichment_inputs.targeted_request
        try:
            operation = TargetedEditorialRevisionOperation(proposal.operation)
        except ValueError:
            raise EditorialProposalAdoptionError("proposal_operation_unsupported") from None
        if (
            request.base_artifact_id != proposal.base_artifact_id
            or request.base_version != proposal.base_artifact_version
            or request.base_input_hash != proposal.base_input_hash
            or request.base_canonical_sha256 != proposal.base_canonical_sha256
            or enrichment_inputs.input_hash != proposal.base_input_hash
            or request.operation is not operation
            or request.target.element_kind is not enrichment_inputs.element_kind
        ):
            raise EditorialProposalAdoptionError("proposal_base_stale")
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
            await uow.editorial_proposals.lock_adoption_for_run(proposal.production_run_id)
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
                raise EditorialProposalAdoptionError("proposal_state_conflict")

            run = await uow.production_runs.get_for_update(locked.production_run_id)
            if run is None:
                raise EditorialProposalAdoptionError("proposal_base_stale")
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
                    raise EditorialProposalAdoptionError("proposal_state_conflict")
                await self._stale_outdated_proposals(
                    uow,
                    base_proposal=stale,
                    actor_id=actor_id,
                    current_record=resolution.record if resolution is not None else None,
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
            snapshot = await self._append_workspace_snapshot(
                uow, resolution.record, stage, plan, artifact
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
                raise EditorialProposalAdoptionError("proposal_state_conflict")

            if stored.thread_id is not None:
                await uow.editorial_threads.append_event(
                    thread_id=stored.thread_id,
                    kind=EditorialThreadEventKind.ACCEPTED,
                    actor_id=actor_id,
                    idempotency_key=f"proposal-acceptance:{idempotency_key}",
                    request_sha256=request_sha256,
                    proposal_id=stored.id,
                )

            await self._stale_outdated_proposals(
                uow,
                base_proposal=stored,
                actor_id=actor_id,
                current_record=snapshot,
            )

            sequence = await uow.editorial_proposal_impacts.next_sequence_for_run(
                stored.production_run_id
            )
            impact = EditorialProposalImpact(
                impact_id=uuid4(),
                subject_id=stored.subject_id,
                production_run_id=stored.production_run_id,
                proposal_id=stored.id,
                accepted_artifact_id=artifact.id,
                accepted_snapshot_id=snapshot.snapshot_id,
                base_pin_hash=stored.base_pin_hash,
                accepted_pin_hash=snapshot.workspace_pin_hash,
                sequence=sequence,
            )
            await uow.editorial_proposal_impacts.add(impact)
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

    async def _append_workspace_snapshot(
        self,
        uow: UnitOfWork,
        base: WorkspaceSnapshotRecord,
        stage: _Stage,
        plan: _CandidatePlan,
        artifact: ProductionArtifact,
    ) -> WorkspaceSnapshotRecord:
        source_stage = EditorialSourceStage(stage.value)
        base_stage_entries = tuple(
            entry for entry in base.block_registry if entry.source_stage is source_stage
        )
        try:
            advanced = self._workspace_service.advance(
                base_registry=base_stage_entries,
                artifact_id=artifact.id,
                canonical_sha256=plan.candidate.canonical_sha256,
                source_stage=stage.value,
                canonical_content=plan.candidate.payload
                if isinstance(plan, _SynthesisCandidatePlan)
                else plan.candidate.canonical_payload,
                lineage=plan.registry_lineage,
                retired_block_ids=plan.retired_block_ids,
            )
        except Exception:
            raise EditorialProposalAdoptionError("editorial_registry_conflict") from None
        unchanged_entries = tuple(
            entry for entry in base.block_registry if entry.source_stage is not source_stage
        )
        if source_stage is EditorialSourceStage.SYNTHESIS:
            entries = (*advanced.active, *unchanged_entries)
        else:
            entries = (*unchanged_entries, *advanced.active)
        retired = tuple(
            sorted(set(base.retired_block_ids) | set(advanced.retired_block_ids), key=str)
        )

        pins: dict[str, object] = {
            "subject_id": base.subject_id,
            "production_run_id": base.production_run_id,
            "synthesis_artifact_id": base.synthesis_artifact_id,
            "synthesis_version": base.synthesis_version,
            "synthesis_input_hash": base.synthesis_input_hash,
            "synthesis_canonical_sha256": base.synthesis_canonical_sha256,
            "enrichment_artifact_id": base.enrichment_artifact_id,
            "enrichment_version": base.enrichment_version,
            "enrichment_input_hash": base.enrichment_input_hash,
            "enrichment_canonical_sha256": base.enrichment_canonical_sha256,
            "publication_artifact_id": base.publication_artifact_id,
            "publication_version": base.publication_version,
            "publication_input_hash": base.publication_input_hash,
            "publication_canonical_sha256": base.publication_canonical_sha256,
            "pipeline_generation": base.pipeline_generation,
            "routing_decision_id": base.routing_decision_id,
            "routing_decision_hash": base.routing_decision_hash,
            "publication_kind": base.publication_kind,
        }
        pin_fields = (
            ("synthesis",) if source_stage is EditorialSourceStage.SYNTHESIS else ("enrichment",)
        )
        prefix = pin_fields[0]
        pins[f"{prefix}_artifact_id"] = artifact.id
        pins[f"{prefix}_version"] = artifact.version
        pins[f"{prefix}_input_hash"] = artifact.input_hash
        pins[f"{prefix}_canonical_sha256"] = plan.candidate.canonical_sha256
        next_record = replace(
            base,
            snapshot_id=uuid4(),
            synthesis_artifact_id=(
                artifact.id
                if source_stage is EditorialSourceStage.SYNTHESIS
                else base.synthesis_artifact_id
            ),
            synthesis_version=(
                artifact.version
                if source_stage is EditorialSourceStage.SYNTHESIS
                else base.synthesis_version
            ),
            synthesis_input_hash=(
                artifact.input_hash
                if source_stage is EditorialSourceStage.SYNTHESIS
                else base.synthesis_input_hash
            ),
            synthesis_canonical_sha256=(
                plan.candidate.canonical_sha256
                if source_stage is EditorialSourceStage.SYNTHESIS
                else base.synthesis_canonical_sha256
            ),
            enrichment_artifact_id=(
                artifact.id
                if source_stage is EditorialSourceStage.EDITORIAL_ENRICHMENT
                else base.enrichment_artifact_id
            ),
            enrichment_version=(
                artifact.version
                if source_stage is EditorialSourceStage.EDITORIAL_ENRICHMENT
                else base.enrichment_version
            ),
            enrichment_input_hash=(
                artifact.input_hash
                if source_stage is EditorialSourceStage.EDITORIAL_ENRICHMENT
                else base.enrichment_input_hash
            ),
            enrichment_canonical_sha256=(
                plan.candidate.canonical_sha256
                if source_stage is EditorialSourceStage.EDITORIAL_ENRICHMENT
                else base.enrichment_canonical_sha256
            ),
            workspace_pin_hash=workspace_pin_hash(pins),
            registry_hash=registry_hash(entries),
            block_registry=tuple(entries),
            retired_block_ids=retired,
            parent_snapshot_id=base.snapshot_id,
        )
        stored = await uow.editorial_workspace_snapshots.append_if_absent(next_record)
        if (
            stored.workspace_pin_hash != next_record.workspace_pin_hash
            or stored.registry_hash != next_record.registry_hash
            or stored.block_registry != next_record.block_registry
        ):
            raise EditorialProposalAdoptionError("editorial_registry_conflict")
        return stored

    async def _stale_outdated_proposals(
        self,
        uow: UnitOfWork,
        *,
        base_proposal: EditorialProposal,
        actor_id: UUID,
        current_record: WorkspaceSnapshotRecord | None,
    ) -> None:
        for other in await uow.editorial_proposals.list_proposed_for_run_for_update(
            base_proposal.production_run_id
        ):
            if other.id == base_proposal.id:
                continue
            if (
                other.variant_group_id != base_proposal.variant_group_id
                and current_record is not None
                and _proposal_fences_match_record(other, current_record)
            ):
                continue
            event = (
                _stale_after_acceptance_event(
                    other, base_proposal, actor_id, current_record.workspace_pin_hash
                )
                if base_proposal.state is EditorialProposalState.ACCEPTED
                and current_record is not None
                else _state_event(
                    other,
                    state=EditorialProposalState.STALE,
                    actor_id=actor_id,
                    idempotency_key=f"stale-{other.id.hex}-{other.state_version}",
                    request_sha256=_state_request_sha256(other, "stale"),
                )
            )
            stale, changed = await uow.editorial_proposals.transition_if_current(
                subject_id=other.subject_id,
                occurrence_id=other.occurrence_id,
                proposal_id=other.id,
                expected_state=EditorialProposalState.PROPOSED,
                expected_state_version=other.state_version,
                state=EditorialProposalState.STALE,
                event=event,
            )
            if stale is None or not changed:
                raise EditorialProposalAdoptionError("proposal_state_conflict")

    async def _persist_stale(self, proposal: EditorialProposal, *, actor_id: UUID) -> None:
        async with self._uow_factory() as uow:
            await uow.editorial_proposals.lock_adoption_for_run(proposal.production_run_id)
            locked = await uow.editorial_proposals.get_for_occurrence_for_update(
                proposal.subject_id, proposal.occurrence_id, proposal.id
            )
            if locked is None:
                raise EditorialProposalAdoptionError("proposal_not_found")
            run = await uow.production_runs.get_for_update(locked.production_run_id)
            if run is None:
                raise EditorialProposalAdoptionError("proposal_base_stale")
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
                await self._stale_outdated_proposals(
                    uow,
                    base_proposal=stored,
                    actor_id=actor_id,
                    current_record=resolution.record if resolution is not None else None,
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
            raise EditorialProposalAdoptionError("proposal_state_conflict")
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
        or resolution.artifact_id != proposal.base_artifact_id
        or resolution.artifact_version != proposal.base_artifact_version
        or resolution.canonical_sha256 != proposal.base_canonical_sha256
        or not _proposal_fences_match_record(proposal, record)
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


def _proposal_fences_match_record(
    proposal: EditorialProposal, record: WorkspaceSnapshotRecord
) -> bool:
    if (
        record.subject_id != proposal.subject_id
        or record.production_run_id != proposal.production_run_id
        or record.snapshot_id != proposal.snapshot_id
        or record.workspace_pin_hash != proposal.base_pin_hash
        or record.routing_decision_hash != proposal.base_routing_hash
    ):
        return False
    if record.synthesis_artifact_id == proposal.base_artifact_id:
        return (
            record.synthesis_version == proposal.base_artifact_version
            and record.synthesis_input_hash == proposal.base_input_hash
            and record.synthesis_canonical_sha256 == proposal.base_canonical_sha256
        )
    if record.enrichment_artifact_id == proposal.base_artifact_id:
        return (
            record.enrichment_version == proposal.base_artifact_version
            and record.enrichment_input_hash == proposal.base_input_hash
            and record.enrichment_canonical_sha256 == proposal.base_canonical_sha256
        )
    return False


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


def _state_event(
    proposal: EditorialProposal,
    *,
    state: EditorialProposalState,
    actor_id: UUID,
    idempotency_key: str,
    request_sha256: str,
) -> EditorialProposalEvent:
    return EditorialProposalEvent(
        id=uuid4(),
        proposal_id=proposal.id,
        sequence=proposal.state_version + 1,
        kind=EditorialProposalEventKind.STATE_CHANGED,
        state=state,
        state_version=proposal.state_version + 1,
        actor_id=actor_id,
        idempotency_key=idempotency_key,
        request_sha256=request_sha256,
        patch_sha256=proposal.patch_sha256,
    )


def _state_request_sha256(proposal: EditorialProposal, action: str) -> str:
    body = {
        "action": action,
        "proposal_id": str(proposal.id),
        "state_version": proposal.state_version,
        "patch_sha256": proposal.patch_sha256,
    }
    return hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _stale_after_acceptance_event(
    proposal: EditorialProposal,
    accepted: EditorialProposal,
    actor_id: UUID,
    accepted_pin_hash: str,
) -> EditorialProposalEvent:
    body = {
        "action": "stale_after_acceptance",
        "proposal_id": str(proposal.id),
        "accepted_proposal_id": str(accepted.id),
        "accepted_pin_hash": accepted_pin_hash,
        "state_version": proposal.state_version,
    }
    digest = hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return _state_event(
        proposal,
        state=EditorialProposalState.STALE,
        actor_id=actor_id,
        idempotency_key=f"accept-stale-{accepted.id.hex}-{proposal.id.hex}",
        request_sha256=digest,
    )
