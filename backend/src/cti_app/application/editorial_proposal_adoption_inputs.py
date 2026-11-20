"""Production inputs for model-free editorial proposal adoption."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

from cti_app.application.editorial_enrichment_candidate import (
    build_candidate_from_validated_patch as build_enrichment_candidate,
)
from cti_app.application.editorial_enrichment_revision import (
    assert_requested_placement,
    targeted_action,
    validate_editorial_enrichment_patch,
)
from cti_app.application.editorial_proposal_adoption import (
    EditorialProposalAdoptionError,
    EditorialProposalAdoptionInputs,
    EnrichmentCandidateInputs,
    SynthesisCandidateInputs,
)
from cti_app.application.editorial_synthesis.candidate import (
    SynthesisCandidatePersistence,
)
from cti_app.application.editorial_synthesis.candidate import (
    build_candidate_from_validated_patch as build_synthesis_candidate,
)
from cti_app.application.editorial_synthesis.patch_validation import WirePatch
from cti_app.application.editorial_workspace.registry import (
    WorkspaceOccurrenceResolution,
    read_pointer,
    resolve_workspace_target,
)
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.application.production_editorial_enrichment import (
    EDITORIAL_ENRICHMENT_MODEL_POLICY_VERSION,
    EditorialEnrichmentEvidencePackV1,
)
from cti_app.application.production_routing import get_adopted_production_routing_decision
from cti_app.application.production_synthesis import (
    SynthesisAccessPolicyV1,
    SynthesisEvidencePackV1,
    build_synthesis_access_policy,
    build_synthesis_evidence_pack,
    synthesis_access_policy_hash,
)
from cti_app.application.production_synthesis_revision import _apply_wire_patch
from cti_app.domain.classification import TLP
from cti_app.domain.collection import CollectionState
from cti_app.domain.editorial_proposal import EditorialProposal
from cti_app.domain.editorial_workspace import (
    EditorialSourceStage,
    bootstrap_block_registry,
    canonical_content_sha256,
)
from cti_app.domain.production import (
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionArtifactStatus,
    ProductionInputSnapshot,
    ProductionRun,
    SynthesisMode,
)
from cti_app.domain.production_editorial_enrichment import (
    EditorialEnrichmentElementKind,
    editorial_enrichment_from_json,
)
from cti_app.domain.production_editorial_revision import (
    TargetedEditorialRevisionMode,
    TargetedEditorialRevisionOperation,
    TargetedEditorialRevisionPlacement,
    TargetedEditorialRevisionPlacementKind,
    TargetedEditorialRevisionRequest,
    TargetedEditorialRevisionTarget,
    TargetedEditorialRevisionTargetKind,
)
from cti_app.domain.production_extraction import ProductionExtractionV1
from cti_app.domain.production_synthesis import production_synthesis_from_json
from cti_app.domain.publication_kind import PublicationKind


@dataclass(frozen=True, slots=True)
class _PreparedInputs:
    run: ProductionRun
    snapshot: ProductionInputSnapshot
    extraction: ProductionExtractionV1
    base_artifact: ProductionArtifact
    extraction_artifact: ProductionArtifact
    projection_artifact: ProductionArtifact | None
    synthesis_artifact: ProductionArtifact
    evidence_pack: SynthesisEvidencePackV1 | EditorialEnrichmentEvidencePackV1
    stage: ProductionArtifactStage


_ACTIVE_SOURCE_STATES = frozenset(
    {CollectionState.ARCHIVED, CollectionState.EXTRACTED, CollectionState.COMPLETED}
)


class ProductionEditorialProposalAdoptionInputs(EditorialProposalAdoptionInputs):
    """Rebuild candidate evidence before the adoption UOW and recheck DB fences in it.

    Artifact payloads are read while preparing a candidate, before adoption opens its
    relational transaction. ``revalidate_evidence_policy_and_thread`` uses only the
    prepared, parsed evidence and current UOW repositories.
    """

    def __init__(
        self,
        *,
        uow_factory: Any,
        artifact_store: ProductionArtifactStore,
        workspace_service: Any,
        synthesis_loader: Any,
        enrichment_loader: Any,
        enrichment_revision_service: Any,
        resource_search_enabled: bool = False,
    ) -> None:
        self._uow_factory = uow_factory
        self._artifact_store = artifact_store
        self._workspace_service = workspace_service
        self._synthesis_loader = synthesis_loader
        self._enrichment_loader = enrichment_loader
        self._enrichment_revision_service = enrichment_revision_service
        self._resource_search_enabled = resource_search_enabled
        self._prepared: dict[UUID, _PreparedInputs] = {}

    async def synthesis_inputs(
        self,
        proposal: EditorialProposal,
        patch: Mapping[str, Any],
        *,
        base_artifact: ProductionArtifact,
        base_payload: dict[str, Any],
        base_bytes: bytes,
        resolution: WorkspaceOccurrenceResolution,
    ) -> SynthesisCandidateInputs:
        (
            run,
            snapshot,
            extraction_artifact,
            projection_artifact,
            synthesis_artifact,
        ) = await self._load_base_context(
            proposal,
            base_artifact=base_artifact,
            base_bytes=base_bytes,
            resolution=resolution,
            stage=ProductionArtifactStage.SYNTHESIS,
        )
        if base_artifact.stage is not ProductionArtifactStage.SYNTHESIS:
            raise EditorialProposalAdoptionError("proposal_target_mismatch")
        try:
            extraction, _extraction_hash = await self._synthesis_loader._load_extraction(
                run, snapshot, extraction_artifact
            )
            projection = (
                await self._synthesis_loader._load_projection(
                    run,
                    snapshot,
                    extraction,
                    _extraction_hash,
                    projection_artifact,
                )
                if projection_artifact is not None
                else None
            )
            access_policy, subject_version = await self._access_policy(snapshot, extraction)
            self._require_allowed(access_policy)
            self._require_snapshot_subject_version(snapshot, subject_version)
            evidence_pack = build_synthesis_evidence_pack(snapshot, extraction, projection)
            base_synthesis = production_synthesis_from_json(base_payload)
            operation = TargetedEditorialRevisionOperation(proposal.operation)
            if resolution.entry is None or resolution.binding_state != "valid":
                raise EditorialProposalAdoptionError("proposal_base_stale")
            resolved = resolve_workspace_target(
                entry=resolution.entry,
                canonical_content=base_payload,
                operation=operation.value,
            )
            if (
                resolved.legacy_target_id != proposal.target_id
                or resolved.before_hash != proposal.before_hash
                or patch.get("target") != resolved.target.to_json()
            ):
                raise EditorialProposalAdoptionError("proposal_target_mismatch")
            after = patch.get("after")
            rationale = patch.get("rationale")
            if not isinstance(after, str) or not isinstance(rationale, str):
                raise EditorialProposalAdoptionError("proposal_patch_invalid")
            wire_patch = WirePatch(
                target=resolved.path,
                action=operation,
                base_artifact_hash=proposal.base_canonical_sha256,
                before_hash=proposal.before_hash,
                after=after,
                rationale=rationale,
            )
            candidate = build_synthesis_candidate(
                base_artifact=base_artifact,
                base_payload=base_payload,
                base_synthesis=base_synthesis,
                base_canonical_sha256=proposal.base_canonical_sha256,
                patch=wire_patch,
                target=resolved.target,
                operation=operation,
                evidence_pack=evidence_pack,
                extraction=extraction,
                snapshot=snapshot,
                apply_patch=_apply_wire_patch,
                publication_kind=resolution.record.publication_kind,
            )
            lineage = self._synthesis_lineage(
                resolution,
                base_payload,
                candidate.payload,
                target_block_id=resolution.entry.block_id,
                operation=operation,
                target=resolved.target,
            )
            routing = await self._routing(run)
            persistence = SynthesisCandidatePersistence(
                run_id=run.id,
                subject_id=run.subject_id,
                input_hash=base_artifact.input_hash,
                extraction=extraction,
                snapshot=snapshot,
                raw_result=None,
                model_run_id=None,
                publication_kind=resolution.record.publication_kind or PublicationKind.ARTICLE,
                mode=SynthesisMode.FRESH,
                model_policy_version=str(base_artifact.metadata.get("model_policy_version", "")),
                routing_policy_version=str(
                    base_artifact.metadata.get("routing_policy_version", "")
                ),
                projection_hash=projection.projection_hash if projection is not None else None,
                artifact_id=uuid4(),
                expected_current_artifact_id=base_artifact.id,
                expected_current_input_hash=base_artifact.input_hash,
                expected_current_canonical_sha256=proposal.base_canonical_sha256,
                expected_routing_decision_id=(routing.decision_id if routing else None),
                expected_routing_decision_hash=(routing.decision_hash if routing else None),
            )
        except EditorialProposalAdoptionError:
            raise
        except Exception as error:
            code = getattr(error, "reason_code", None) or getattr(error, "code", None)
            raise EditorialProposalAdoptionError(str(code or "proposal_base_unavailable")) from None

        self._prepared[proposal.id] = _PreparedInputs(
            run=run,
            snapshot=snapshot,
            extraction=extraction,
            base_artifact=base_artifact,
            extraction_artifact=extraction_artifact,
            projection_artifact=projection_artifact,
            synthesis_artifact=synthesis_artifact,
            evidence_pack=evidence_pack,
            stage=ProductionArtifactStage.SYNTHESIS,
        )
        return SynthesisCandidateInputs(
            base_payload=base_payload,
            base_synthesis=base_synthesis,
            target=resolved.target,
            evidence_pack=evidence_pack,
            extraction=extraction,
            snapshot=snapshot,
            apply_patch=_apply_wire_patch,
            persistence=persistence,
            registry_lineage=lineage,
            publication_kind=resolution.record.publication_kind,
        )

    async def enrichment_inputs(
        self,
        proposal: EditorialProposal,
        patch: Mapping[str, Any],
        *,
        base_artifact: ProductionArtifact,
        base_payload: dict[str, Any],
        base_bytes: bytes,
        resolution: WorkspaceOccurrenceResolution,
    ) -> EnrichmentCandidateInputs:
        (
            run,
            snapshot,
            extraction_artifact,
            projection_artifact,
            current_synthesis_artifact,
        ) = await self._load_base_context(
            proposal,
            base_artifact=base_artifact,
            base_bytes=base_bytes,
            resolution=resolution,
            stage=ProductionArtifactStage.EDITORIAL_ENRICHMENT,
        )
        if base_artifact.stage is not ProductionArtifactStage.EDITORIAL_ENRICHMENT:
            raise EditorialProposalAdoptionError("proposal_target_mismatch")
        try:
            extraction = await self._enrichment_loader._load_extraction(
                run, snapshot, extraction_artifact
            )
            projection = (
                await self._enrichment_loader._load_projection(
                    run, snapshot, extraction, projection_artifact
                )
                if projection_artifact is not None
                else None
            )
            synthesis = await self._enrichment_loader._load_synthesis(
                run, snapshot, current_synthesis_artifact, extraction
            )
            access_policy, subject_version = await self._access_policy(snapshot, extraction)
            self._require_allowed(access_policy)
            self._require_snapshot_subject_version(snapshot, subject_version)
            fence = await self._enrichment_revision_service._validate_input_fence(
                snapshot=snapshot,
                base_artifact=base_artifact,
                extraction_artifact=extraction_artifact,
                projection_artifact=projection_artifact,
                synthesis_artifact=current_synthesis_artifact,
                extraction=extraction,
                projection=projection,
                synthesis=synthesis,
                read_only_media=True,
            )
            self._require_allowed(access_policy)
            operation = TargetedEditorialRevisionOperation(proposal.operation)
            target = _enrichment_target(patch.get("target"))
            if target.kind is not TargetedEditorialRevisionTargetKind.ENRICHMENT_ELEMENT:
                raise EditorialProposalAdoptionError("proposal_target_mismatch")
            if _target_id(target) != proposal.target_id:
                raise EditorialProposalAdoptionError("proposal_target_mismatch")
            if target.element_kind is None or target.element_key is None:
                raise EditorialProposalAdoptionError("proposal_target_mismatch")
            is_create = operation in {
                TargetedEditorialRevisionOperation.CREATE_TABLE,
                TargetedEditorialRevisionOperation.CREATE_DIAGRAM,
                TargetedEditorialRevisionOperation.CREATE_CHART,
                TargetedEditorialRevisionOperation.CREATE_SOURCE_FIGURE,
            }
            is_delete = operation in {
                TargetedEditorialRevisionOperation.DELETE_TABLE,
                TargetedEditorialRevisionOperation.DELETE_DIAGRAM,
                TargetedEditorialRevisionOperation.DELETE_CHART,
                TargetedEditorialRevisionOperation.DELETE_SOURCE_FIGURE,
            }
            base_enrichment = editorial_enrichment_from_json(base_payload)
            target_entry = resolution.entry
            if (
                target_entry is None
                or resolution.binding_state != "valid"
                or target_entry.source_stage is not EditorialSourceStage.EDITORIAL_ENRICHMENT
                or target_entry.content_sha256 != proposal.before_hash
            ):
                raise EditorialProposalAdoptionError("proposal_base_stale")
            request = TargetedEditorialRevisionRequest(
                subject_id=proposal.subject_id,
                base_artifact_id=base_artifact.id,
                base_version=base_artifact.version,
                base_input_hash=base_artifact.input_hash,
                base_canonical_sha256=proposal.base_canonical_sha256,
                target=target,
                operation=operation,
                instruction="Accepted editorial proposal",
                mode=TargetedEditorialRevisionMode.EXISTING_EVIDENCE_ONLY,
                requester=str(proposal.actor_id),
                before_hash=proposal.before_hash,
            )
            raw_patch = patch.get("enrichment_patch")
            if not isinstance(raw_patch, dict):
                raise EditorialProposalAdoptionError("proposal_patch_invalid")
            validated_patch = validate_editorial_enrichment_patch(
                raw_patch,
                element_kind=target.element_kind,
                element_key=target.element_key,
                evidence_pack=fence.evidence_pack,
                extraction=extraction,
                synthesis=synthesis,
                figure_catalog=fence.figure_catalog,
            )
            if is_create:
                assert_requested_placement(
                    candidate=validated_patch,
                    element_kind=target.element_kind,
                    targeted_request=request,
                    synthesis=synthesis,
                )
            candidate = await build_enrichment_candidate(
                base_artifact=base_artifact,
                base_payload=base_payload,
                base_canonical_bytes=base_bytes,
                base_canonical_sha256=proposal.base_canonical_sha256,
                base_enrichment=base_enrichment,
                validated_patch=validated_patch,
                extraction=extraction,
                synthesis=synthesis,
                element_kind=target.element_kind,
                element_key=target.element_key,
                action=targeted_action(operation),
                input_hash=fence.input_hash,
                artifact_id=uuid4(),
                targeted_request=request,
                is_create=is_create,
                is_delete=is_delete,
            )
            lineage = self._enrichment_lineage(
                resolution, base_payload, candidate.canonical_payload
            )
            return_inputs = EnrichmentCandidateInputs(
                base_payload=base_payload,
                base_enrichment=base_enrichment,
                extraction=extraction,
                synthesis=synthesis,
                evidence_pack=fence.evidence_pack,
                targeted_request=request,
                element_kind=target.element_kind,
                element_key=target.element_key,
                input_hash=fence.input_hash,
                artifact_id=candidate.artifact_id,
                figure_catalog=fence.figure_catalog,
                registry_lineage=lineage,
                is_create=is_create,
                is_delete=is_delete,
                evidence_pack_hash=fence.evidence_pack_hash,
                access_policy_hash=synthesis_access_policy_hash(access_policy),
                model_policy_version=EDITORIAL_ENRICHMENT_MODEL_POLICY_VERSION,
                routing_policy_version=str(
                    base_artifact.metadata.get("routing_policy_version", "")
                ),
                projection_hash=projection.projection_hash if projection is not None else None,
                source_figure_inventory_hash=fence.inventory_hash,
                resource_search_enabled=self._resource_search_enabled,
                diagram_materializer=lambda diagram: (
                    self._enrichment_revision_service._materialize_candidate_diagram(
                        diagram, run_id=run.id
                    )
                ),
            )
        except EditorialProposalAdoptionError:
            raise
        except Exception as error:
            code = getattr(error, "reason_code", None) or getattr(error, "code", None)
            raise EditorialProposalAdoptionError(str(code or "proposal_base_unavailable")) from None

        self._prepared[proposal.id] = _PreparedInputs(
            run=run,
            snapshot=snapshot,
            extraction=extraction,
            base_artifact=base_artifact,
            extraction_artifact=extraction_artifact,
            projection_artifact=projection_artifact,
            synthesis_artifact=current_synthesis_artifact,
            evidence_pack=fence.evidence_pack,
            stage=ProductionArtifactStage.EDITORIAL_ENRICHMENT,
        )
        return return_inputs

    async def revalidate_evidence_policy_and_thread(
        self,
        uow: Any,
        proposal: EditorialProposal,
        patch: Mapping[str, Any],
        *,
        resolution: WorkspaceOccurrenceResolution,
    ) -> None:
        del patch
        prepared = self._prepared.pop(proposal.id, None)
        if prepared is None:
            raise EditorialProposalAdoptionError("proposal_base_unavailable")
        record = resolution.record
        expected_artifact_id = (
            record.synthesis_artifact_id
            if prepared.stage is ProductionArtifactStage.SYNTHESIS
            else record.enrichment_artifact_id
        )
        expected_version = (
            record.synthesis_version
            if prepared.stage is ProductionArtifactStage.SYNTHESIS
            else record.enrichment_version
        )
        expected_sha256 = (
            record.synthesis_canonical_sha256
            if prepared.stage is ProductionArtifactStage.SYNTHESIS
            else record.enrichment_canonical_sha256
        )
        if (
            resolution.binding_state != "valid"
            or record.subject_id != proposal.subject_id
            or record.production_run_id != proposal.production_run_id
            or record.snapshot_id != proposal.snapshot_id
            or resolution.entry is None
            or resolution.entry.block_id != proposal.occurrence_id
            or record.workspace_pin_hash != proposal.base_pin_hash
            or expected_artifact_id != proposal.base_artifact_id
            or expected_version != proposal.base_artifact_version
            or expected_sha256 != proposal.base_canonical_sha256
            or record.routing_decision_hash != proposal.base_routing_hash
        ):
            raise EditorialProposalAdoptionError("proposal_base_stale")

        run = await uow.production_runs.get_for_update(proposal.production_run_id)
        current_run = await uow.production_runs.get_current_for_subject(proposal.subject_id)
        snapshot = await uow.production_input_snapshots.get_by_run(proposal.production_run_id)
        current_base = await uow.production_artifacts.get_current(
            proposal.production_run_id, prepared.stage.value
        )
        current_extraction = await uow.production_artifacts.get_current(
            proposal.production_run_id, ProductionArtifactStage.EXTRACTION.value
        )
        current_projection = await uow.production_artifacts.get_current(
            proposal.production_run_id, ProductionArtifactStage.RELEVANCE_PROJECTION.value
        )
        current_synthesis = await uow.production_artifacts.get_current(
            proposal.production_run_id, ProductionArtifactStage.SYNTHESIS.value
        )
        current_subject = await uow.subjects.get(proposal.subject_id)
        routing = (
            await get_adopted_production_routing_decision(uow=uow, run=run)
            if run is not None
            else None
        )
        if (
            run is None
            or run.id != prepared.run.id
            or run.subject_id != proposal.subject_id
            or current_run is None
            or current_run.id != run.id
            or current_run.pipeline_generation != prepared.run.pipeline_generation
            or snapshot is None
            or snapshot.id != prepared.snapshot.id
            or current_subject is None
            or current_subject.id != proposal.subject_id
            or not _same_current_artifact(current_base, prepared.base_artifact)
            or not _same_current_artifact(current_extraction, prepared.extraction_artifact)
            or not _same_optional_current_artifact(current_projection, prepared.projection_artifact)
            or not _same_current_artifact(current_synthesis, prepared.synthesis_artifact)
            or record.routing_decision_id != (routing.decision_id if routing is not None else None)
            or record.routing_decision_hash
            != (routing.decision_hash if routing is not None else None)
        ):
            raise EditorialProposalAdoptionError("proposal_base_stale")

        await self._revalidate_thread(uow, proposal, resolution)
        await self._revalidate_sources(uow, prepared.extraction)
        try:
            access_policy = await build_synthesis_access_policy(
                prepared.snapshot,
                prepared.extraction,
                uow.source_documents,
                uow.source_collections,
                current_subject_tlp=current_subject.tlp,
            )
        except Exception:
            raise EditorialProposalAdoptionError("proposal_policy_unavailable") from None
        self._require_allowed(access_policy)
        self._require_snapshot_subject_version(prepared.snapshot, current_subject.version)
        current_refs = _evidence_ref_keys(prepared.evidence_pack)
        if not _proposal_evidence_keys(proposal.evidence_refs) <= current_refs:
            raise EditorialProposalAdoptionError("proposal_evidence_not_admissible")

    async def _load_base_context(
        self,
        proposal: EditorialProposal,
        *,
        base_artifact: ProductionArtifact,
        base_bytes: bytes,
        resolution: WorkspaceOccurrenceResolution,
        stage: ProductionArtifactStage,
    ) -> tuple[
        ProductionRun,
        ProductionInputSnapshot,
        ProductionArtifact,
        ProductionArtifact | None,
        ProductionArtifact,
    ]:
        if hashlib.sha256(base_bytes).hexdigest() != proposal.base_canonical_sha256:
            raise EditorialProposalAdoptionError("proposal_base_hash_mismatch")
        async with self._uow_factory() as uow:
            run = await uow.production_runs.get_current_for_subject(proposal.subject_id)
            artifact = await uow.production_artifacts.get(proposal.base_artifact_id)
            snapshot = await uow.production_input_snapshots.get_by_run(proposal.production_run_id)
            current = await uow.production_artifacts.get_current(
                proposal.production_run_id, stage.value
            )
            extraction_artifact = await uow.production_artifacts.get_current(
                proposal.production_run_id, ProductionArtifactStage.EXTRACTION.value
            )
            projection_artifact = await uow.production_artifacts.get_current(
                proposal.production_run_id, ProductionArtifactStage.RELEVANCE_PROJECTION.value
            )
            current_synthesis_artifact = await uow.production_artifacts.get_current(
                proposal.production_run_id, ProductionArtifactStage.SYNTHESIS.value
            )
            routing = (
                await get_adopted_production_routing_decision(uow=uow, run=run)
                if run is not None
                else None
            )
        if (
            run is None
            or run.id != proposal.production_run_id
            or artifact is None
            or artifact != base_artifact
            or current is None
            or current.id != artifact.id
            or artifact.stage is not stage
            or artifact.version != proposal.base_artifact_version
            or artifact.input_hash != proposal.base_input_hash
            or artifact.status is not ProductionArtifactStatus.VERIFIED
            or artifact.canonical_blob_id is None
            or snapshot is None
            or snapshot.production_run_id != run.id
            or resolution.record.snapshot_id != proposal.snapshot_id
            or resolution.record.workspace_pin_hash != proposal.base_pin_hash
            or resolution.record.routing_decision_hash
            != (routing.decision_hash if routing is not None else None)
            or resolution.record.routing_decision_id
            != (routing.decision_id if routing is not None else None)
            or resolution.artifact_id != artifact.id
            or resolution.artifact_version != artifact.version
            or resolution.canonical_sha256 != proposal.base_canonical_sha256
        ):
            raise EditorialProposalAdoptionError("proposal_base_stale")
        if extraction_artifact is None or current_synthesis_artifact is None:
            raise EditorialProposalAdoptionError("proposal_base_unavailable")
        if stage is ProductionArtifactStage.SYNTHESIS:
            if (
                current_synthesis_artifact.id != artifact.id
                or resolution.record.synthesis_artifact_id != artifact.id
                or resolution.record.synthesis_version != artifact.version
                or resolution.record.synthesis_canonical_sha256 != proposal.base_canonical_sha256
            ):
                raise EditorialProposalAdoptionError("proposal_base_stale")
            if resolution.record.publication_kind is None:
                raise EditorialProposalAdoptionError("publication_kind_unresolved")
        else:
            if (
                resolution.record.synthesis_artifact_id != current_synthesis_artifact.id
                or resolution.record.synthesis_version != current_synthesis_artifact.version
                or resolution.record.synthesis_canonical_sha256 is None
                or resolution.record.enrichment_artifact_id != artifact.id
                or resolution.record.enrichment_version != artifact.version
                or resolution.record.enrichment_canonical_sha256 != proposal.base_canonical_sha256
            ):
                raise EditorialProposalAdoptionError("proposal_base_stale")
        # Only the service's read-only, canonical artifact readers are used after this point.
        return run, snapshot, extraction_artifact, projection_artifact, current_synthesis_artifact

    async def _access_policy(
        self, snapshot: ProductionInputSnapshot, extraction: ProductionExtractionV1
    ) -> tuple[SynthesisAccessPolicyV1, int]:
        async with self._uow_factory() as uow:
            try:
                subject = await uow.subjects.get(snapshot.subject_id)
                if subject is None or subject.id != snapshot.subject_id:
                    raise ValueError("current_subject_unavailable")
                policy = await build_synthesis_access_policy(
                    snapshot,
                    extraction,
                    uow.source_documents,
                    uow.source_collections,
                    current_subject_tlp=subject.tlp,
                )
                return policy, subject.version
            except Exception:
                raise EditorialProposalAdoptionError("proposal_policy_unavailable") from None

    @staticmethod
    def _require_snapshot_subject_version(snapshot: ProductionInputSnapshot, version: int) -> None:
        if version != snapshot.subject_version:
            raise EditorialProposalAdoptionError("proposal_base_stale")

    async def _revalidate_sources(self, uow: Any, extraction: ProductionExtractionV1) -> None:
        for source in extraction.sources:
            document = await uow.source_documents.get(source.source_document_id)
            if document is None or document.id != source.source_document_id:
                raise EditorialProposalAdoptionError("proposal_evidence_revoked")
            collection_id = document.source_collection_id
            if collection_id is None:
                continue
            collection = await uow.source_collections.get_for_update(collection_id)
            if (
                collection is None
                or collection.id != collection_id
                or collection.subject_id != document.subject_id
                or collection.state not in _ACTIVE_SOURCE_STATES
                or (
                    collection.source_document_id is not None
                    and collection.source_document_id != document.id
                )
            ):
                raise EditorialProposalAdoptionError("proposal_evidence_revoked")

    async def _revalidate_thread(
        self, uow: Any, proposal: EditorialProposal, resolution: WorkspaceOccurrenceResolution
    ) -> None:
        if proposal.thread_id is None:
            if proposal.origin.value == "bridge_draft":
                raise EditorialProposalAdoptionError("thread_binding_stale")
            return
        record = await uow.editorial_threads.get(proposal.thread_id)
        thread = getattr(record, "thread", None)
        origin = getattr(thread, "origin", None)
        if (
            thread is None
            or thread.id != proposal.thread_id
            or origin is None
            or origin.subject_id != proposal.subject_id
            or origin.production_run_id != proposal.production_run_id
            or origin.occurrence_id != proposal.occurrence_id
            or origin.subject_id != resolution.record.subject_id
            or origin.production_run_id != resolution.record.production_run_id
            or resolution.entry is None
            or origin.occurrence_id != resolution.entry.block_id
            or resolution.record.snapshot_id != proposal.snapshot_id
        ):
            raise EditorialProposalAdoptionError("thread_binding_stale")

    async def _routing(self, run: ProductionRun) -> Any:
        async with self._uow_factory() as uow:
            return await get_adopted_production_routing_decision(uow=uow, run=run)

    @staticmethod
    def _require_allowed(policy: SynthesisAccessPolicyV1) -> None:
        if policy.effective_tlp is TLP.RED:
            raise EditorialProposalAdoptionError("external_llm_tlp_blocked")
        if not policy.external_llm_allowed:
            raise EditorialProposalAdoptionError("external_llm_blocked")

    @staticmethod
    def _synthesis_lineage(
        resolution: WorkspaceOccurrenceResolution,
        base_payload: Mapping[str, Any],
        candidate_payload: Mapping[str, Any],
        *,
        target_block_id: UUID,
        operation: TargetedEditorialRevisionOperation,
        target: TargetedEditorialRevisionTarget,
    ) -> tuple[Mapping[str, object], ...]:
        base_entries = tuple(
            entry
            for entry in resolution.record.block_registry
            if entry.source_stage is EditorialSourceStage.SYNTHESIS
        )
        candidates = bootstrap_block_registry(
            artifact_id=uuid4(),
            canonical_sha256="0" * 64,
            source_stage=EditorialSourceStage.SYNTHESIS,
            canonical_content=candidate_payload,
        )
        by_pointer = {entry.structural_pointer: entry for entry in candidates}
        replace_prefix: str | None = None
        if operation in {
            TargetedEditorialRevisionOperation.REWRITE_LEAD,
            TargetedEditorialRevisionOperation.REWRITE_SECTION,
            TargetedEditorialRevisionOperation.SPLIT_SECTION,
            TargetedEditorialRevisionOperation.MERGE_SECTIONS,
        }:
            if target.kind is TargetedEditorialRevisionTargetKind.LEAD:
                replace_prefix = "/lead/"
            elif target.kind is TargetedEditorialRevisionTargetKind.SECTION:
                matches = [
                    entry.structural_pointer
                    for entry in base_entries
                    if entry.block_id == target_block_id
                ]
                if matches:
                    replace_prefix = matches[0].rsplit("/", 1)[0]
            elif target.kind is TargetedEditorialRevisionTargetKind.SECTIONS:
                # New sections have no prior identity. Existing sections must remain at their
                # pinned pointers or the transition is deliberately refused below.
                replace_prefix = None

        lineage: list[Mapping[str, object]] = []
        claimed: set[str] = set()
        for entry in base_entries:
            candidate_entry = by_pointer.get(entry.structural_pointer)
            in_replaced_scope = bool(
                replace_prefix and entry.structural_pointer.startswith(replace_prefix)
            )
            if in_replaced_scope:
                lineage.append({"block_id": str(entry.block_id), "transition": "delete"})
                continue
            if candidate_entry is None:
                if (
                    operation
                    in {
                        TargetedEditorialRevisionOperation.DELETE_PARAGRAPH,
                        TargetedEditorialRevisionOperation.DELETE_SECTION,
                    }
                    and entry.block_id == target_block_id
                ):
                    lineage.append({"block_id": str(entry.block_id), "transition": "delete"})
                    continue
                raise EditorialProposalAdoptionError("proposal_registry_lineage_unavailable")
            if candidate_entry.kind is not entry.kind:
                raise EditorialProposalAdoptionError("proposal_registry_lineage_unavailable")
            old_value = read_pointer(base_payload, entry.structural_pointer)
            new_value = read_pointer(candidate_payload, entry.structural_pointer)
            changed = canonical_content_sha256(old_value) != canonical_content_sha256(new_value)
            if changed and entry.block_id != target_block_id:
                raise EditorialProposalAdoptionError("proposal_registry_lineage_unavailable")
            lineage.append(
                {
                    "block_id": str(entry.block_id),
                    "transition": "rewrite" if changed else "move",
                    "pointer": entry.structural_pointer,
                }
            )
            claimed.add(entry.structural_pointer)
        for entry in candidates:
            if entry.structural_pointer not in claimed:
                lineage.append({"transition": "insert", "pointer": entry.structural_pointer})
        return tuple(lineage)

    @staticmethod
    def _enrichment_lineage(
        resolution: WorkspaceOccurrenceResolution,
        base_payload: Mapping[str, Any],
        candidate_payload: Mapping[str, Any],
    ) -> tuple[Mapping[str, object], ...]:
        base_entries = tuple(
            entry
            for entry in resolution.record.block_registry
            if entry.source_stage is EditorialSourceStage.EDITORIAL_ENRICHMENT
        )
        candidates = bootstrap_block_registry(
            artifact_id=uuid4(),
            canonical_sha256="0" * 64,
            source_stage=EditorialSourceStage.EDITORIAL_ENRICHMENT,
            canonical_content=candidate_payload,
        )
        candidate_by_key: dict[str, Any] = {}
        for entry in candidates:
            value = read_pointer(candidate_payload, entry.structural_pointer)
            key = value.get("key") if isinstance(value, Mapping) else None
            if not isinstance(key, str) or key in candidate_by_key:
                raise EditorialProposalAdoptionError("proposal_registry_lineage_unavailable")
            candidate_by_key[key] = entry
        lineage: list[Mapping[str, object]] = []
        claimed: set[str] = set()
        for entry in base_entries:
            value = read_pointer(base_payload, entry.structural_pointer)
            key = value.get("key") if isinstance(value, Mapping) else None
            replacement = candidate_by_key.get(key) if isinstance(key, str) else None
            if replacement is None:
                lineage.append({"block_id": str(entry.block_id), "transition": "delete"})
                continue
            if replacement.kind is not entry.kind:
                raise EditorialProposalAdoptionError("proposal_registry_lineage_unavailable")
            lineage.append(
                {
                    "block_id": str(entry.block_id),
                    "transition": "rewrite",
                    "pointer": replacement.structural_pointer,
                }
            )
            claimed.add(replacement.structural_pointer)
        for entry in candidates:
            if entry.structural_pointer not in claimed:
                lineage.append({"transition": "insert", "pointer": entry.structural_pointer})
        return tuple(lineage)


def _target_id(target: TargetedEditorialRevisionTarget) -> str:
    return hashlib.sha256(
        ProductionArtifactStore.canonical_json_bytes(target.to_json())
    ).hexdigest()


def _same_current_artifact(
    current: ProductionArtifact | None, expected: ProductionArtifact
) -> bool:
    return (
        current is not None
        and current.id == expected.id
        and current.production_run_id == expected.production_run_id
        and current.subject_id == expected.subject_id
        and current.stage is expected.stage
        and current.version == expected.version
        and current.input_hash == expected.input_hash
        and current.status is ProductionArtifactStatus.VERIFIED
        and expected.status is ProductionArtifactStatus.VERIFIED
        and current.canonical_blob_id == expected.canonical_blob_id
    )


def _same_optional_current_artifact(
    current: ProductionArtifact | None, expected: ProductionArtifact | None
) -> bool:
    if current is None or expected is None:
        return current is None and expected is None
    return _same_current_artifact(current, expected)


def _enrichment_target(raw: object) -> TargetedEditorialRevisionTarget:
    if not isinstance(raw, Mapping):
        raise EditorialProposalAdoptionError("proposal_target_mismatch")
    try:
        kind = TargetedEditorialRevisionTargetKind(str(raw.get("kind")))
        element_kind = EditorialEnrichmentElementKind(str(raw.get("element_kind")))
        placement_data = raw.get("placement")
        placement = None
        if placement_data is not None:
            if not isinstance(placement_data, Mapping):
                raise ValueError("placement_invalid")
            placement = TargetedEditorialRevisionPlacement(
                kind=TargetedEditorialRevisionPlacementKind(str(placement_data.get("kind"))),
                section_id=(
                    str(placement_data["section_id"])
                    if placement_data.get("section_id") is not None
                    else None
                ),
            )
        return TargetedEditorialRevisionTarget(
            kind=kind,
            element_kind=element_kind,
            element_key=(str(raw["element_key"]) if raw.get("element_key") is not None else None),
            placement=placement,
        )
    except (KeyError, TypeError, ValueError):
        raise EditorialProposalAdoptionError("proposal_target_mismatch") from None


def _evidence_ref_keys(evidence_pack: Any) -> frozenset[tuple[str, str, str]]:
    refs = getattr(evidence_pack, "_handle_to_ref", {})
    keys: set[tuple[str, str, str]] = set()
    for ref in getattr(refs, "values", lambda: ())():
        source_id = getattr(ref, "source_document_id", None)
        kind = getattr(ref, "kind", None)
        evidence_key = getattr(ref, "evidence_key", None)
        if source_id is not None and kind is not None and evidence_key is not None:
            keys.add((str(source_id), str(getattr(kind, "value", kind)), str(evidence_key)))
    return frozenset(keys)


def _proposal_evidence_keys(
    raw_refs: Sequence[Mapping[str, object]],
) -> frozenset[tuple[str, str, str]]:
    keys: set[tuple[str, str, str]] = set()
    for item in raw_refs:
        raw = item.get("ref", item)
        if not isinstance(raw, Mapping):
            raise EditorialProposalAdoptionError("proposal_evidence_not_admissible")
        try:
            keys.add(
                (
                    str(UUID(str(raw["source_document_id"]))),
                    str(raw["kind"]),
                    str(raw["evidence_key"]),
                )
            )
        except (KeyError, TypeError, ValueError):
            raise EditorialProposalAdoptionError("proposal_evidence_not_admissible") from None
    return frozenset(keys)
