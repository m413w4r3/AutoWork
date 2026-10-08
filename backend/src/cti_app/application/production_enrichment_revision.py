"""Versioned, single-element Editorial Enrichment revisions."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any, cast
from uuid import NAMESPACE_URL, UUID, uuid5

from cti_app.application.media_assets import compile_and_store_diagrams
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.application.production_editorial_enrichment import (
    EDITORIAL_ENRICHMENT_MODEL_POLICY_VERSION,
    SOURCE_FIGURE_PROVENANCE_DIAGNOSTICS_VERSION,
    EditorialEnrichmentEvidencePackV1,
    EditorialEnrichmentProposalControlError,
    EditorialEnrichmentRevisionConflictError,
    EditorialEnrichmentWireParseResult,
    ProductionEditorialEnrichmentService,
    build_editorial_enrichment_evidence_pack,
    build_editorial_enrichment_model_request,
    build_editorial_figure_catalog,
    build_source_figure_provenance_diagnostics,
    canonical_synthesis_hash,
    compute_editorial_enrichment_input_hash,
    editorial_enrichment_evidence_pack_hash,
    editorial_enrichment_output_contract_example,
    parse_editorial_enrichment_proposal_wire,
    semantic_annotation_anchor_texts,
    validate_editorial_enrichment,
    validate_editorial_enrichment_proposal,
)
from cti_app.application.production_prompts import (
    EDITORIAL_ENRICHMENT_PROMPT_VERSION,
    EDITORIAL_ENRICHMENT_PROPOSAL_CONTRACT_VERSION,
    EDITORIAL_ENRICHMENT_REVISION_CONTRACT_VERSION,
    EDITORIAL_ENRICHMENT_REVISION_PROMPT_VERSION,
)
from cti_app.application.production_references import production_reference_corpus_from_json
from cti_app.application.production_stages import EditorialEnrichmentService
from cti_app.application.production_synthesis import (
    build_synthesis_access_policy,
    canonical_extraction_hash,
    synthesis_access_policy_hash,
)
from cti_app.application.production_synthesis_revision import (
    TargetedSynthesisRevisionPreview,
    TargetedSynthesisRevisionResult,
    TargetedSynthesisRevisionValidationError,
)
from cti_app.application.publication_rendering import PublicationRenderService
from cti_app.application.source_figure_inventory import load_archived_source_figure_inventory
from cti_app.domain.production import (
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionArtifactStatus,
    ProductionInputSnapshot,
    ProductionRun,
    ProductionStage,
)
from cti_app.domain.production_editorial_enrichment import (
    EDITORIAL_RESOURCE_PROPOSAL_POLICY_VERSION,
    EditorialEnrichmentElementKind,
    EditorialEnrichmentRevisionAction,
    EditorialEnrichmentRevisionOutcome,
    EditorialEnrichmentV1,
    EditorialResourceNeedV1,
    EnrichmentPlacementKind,
    EnrichmentPlacementV1,
    editorial_enrichment_from_json,
    editorial_enrichment_to_json,
)
from cti_app.domain.production_editorial_revision import (
    TargetedEditorialRevisionMode,
    TargetedEditorialRevisionOperation,
    TargetedEditorialRevisionPlacementKind,
    TargetedEditorialRevisionRefusal,
    TargetedEditorialRevisionRefusalCode,
    TargetedEditorialRevisionRequest,
    TargetedEditorialRevisionTargetKind,
)
from cti_app.domain.production_synthesis import (
    ExtractionEvidenceRefV1,
    production_synthesis_to_json,
)

MAX_EDITORIAL_REVISION_INSTRUCTION_LENGTH = 2000
_LEGACY_EDITORIAL_ENRICHMENT_GENERATOR_VERSION = "model-text-blocks-v8-editorial-tables"
_LEGACY_EDITORIAL_ENRICHMENT_ROUTING_POLICY_VERSION = (
    "editorial-enrichment-routing-policy-v2-resource-search-off"
)


class EditorialEnrichmentRevisionValidationError(RuntimeError):
    code = "editorial_enrichment_revision_rejected"

    def __init__(self, rejections: list[dict[str, str]]) -> None:
        self.rejections = rejections
        super().__init__("The proposed enrichment element failed validation")


@dataclass(frozen=True, slots=True)
class EditorialEnrichmentRevisionResult:
    artifact: ProductionArtifact
    outcome: EditorialEnrichmentRevisionOutcome
    revision: dict[str, Any]
    publication_artifact: ProductionArtifact | None
    previous_publication_artifact_id: UUID | None


@dataclass(frozen=True, slots=True)
class _EditorialEnrichmentRevisionFence:
    access_policy: Any
    inventory: Any
    figure_catalog: tuple[Any, ...]
    evidence_pack: Any
    evidence_pack_hash: str
    inventory_hash: str | None
    input_hash: str


class _StoredSourceMediaArchiver:
    """Expose already persisted media candidates without collecting new media."""

    def __init__(self, uow_factory: Any, delegate: Any) -> None:
        self._uow_factory = uow_factory
        self._delegate = delegate

    @property
    def policy_sha256(self) -> str:
        return cast(str, self._delegate.policy_sha256)

    async def collect(self, subject_id: UUID, sources: tuple[Any, ...]) -> tuple[Any, ...]:
        del sources
        async with self._uow_factory() as uow:
            return tuple(
                await uow.source_media_candidates.list_for_subject_policy(
                    subject_id, self.policy_sha256
                )
            )


class ProductionEditorialEnrichmentRevisionService:
    """Draft one replacement element against the exact proofs admitted by its base."""

    def __init__(
        self,
        *,
        enrichment_service: ProductionEditorialEnrichmentService,
        persistence_service: EditorialEnrichmentService,
        publication_render_service: PublicationRenderService,
    ) -> None:
        self._enrichment = enrichment_service
        self._persistence = persistence_service
        self._publication_render_service = publication_render_service

    async def _validate_input_fence(
        self,
        *,
        snapshot: ProductionInputSnapshot,
        base_artifact: ProductionArtifact,
        extraction: Any,
        projection: Any,
        synthesis: Any,
        read_only_media: bool = False,
    ) -> _EditorialEnrichmentRevisionFence:
        media_archiver: Any = self._enrichment._source_media_archiver
        if read_only_media and media_archiver is not None:
            media_archiver = _StoredSourceMediaArchiver(
                self._enrichment._uow_factory, media_archiver
            )
        async with self._enrichment._uow_factory() as uow:
            access_policy = await build_synthesis_access_policy(
                snapshot,
                extraction,
                uow.source_documents,
                uow.source_collections,
            )
            inventory = await load_archived_source_figure_inventory(
                subject_id=snapshot.subject_id,
                extraction_sources=extraction.sources,
                source_document_repository=uow.source_documents,
                blob_repository=uow.blobs,
                artifact_store=self._enrichment._artifact_store,
                media_archiver=media_archiver,
            )

        if synthesis_access_policy_hash(access_policy) != base_artifact.metadata.get(
            "access_policy_hash"
        ):
            raise EditorialEnrichmentRevisionConflictError(
                "The access policy no longer matches the base artifact"
            )

        evidence_pack = build_editorial_enrichment_evidence_pack(
            snapshot, extraction, synthesis, projection
        )
        evidence_pack_hash = editorial_enrichment_evidence_pack_hash(evidence_pack)
        if base_artifact.metadata.get("evidence_pack_hash") != evidence_pack_hash:
            raise EditorialEnrichmentRevisionConflictError(
                "The admitted evidence pack no longer matches the base artifact"
            )

        figure_catalog, inventory_hash = self._catalog_for_base_artifact(
            base_artifact=base_artifact,
            extraction=extraction,
            inventory=inventory,
        )
        input_hash = compute_editorial_enrichment_input_hash(
            extraction=extraction,
            synthesis=synthesis,
            evidence_pack_hash=evidence_pack_hash,
            access_policy_hash=base_artifact.metadata["access_policy_hash"],
            projection_hash=projection.projection_hash if projection is not None else None,
            source_figure_inventory_hash=inventory_hash,
            resource_search_enabled=self._enrichment._resource_search_enabled,
        )
        if input_hash != base_artifact.input_hash:
            if not self._legacy_input_fence_matches(
                base_artifact=base_artifact,
                extraction=extraction,
                synthesis=synthesis,
                projection=projection,
                resource_search_enabled=self._enrichment._resource_search_enabled,
            ):
                raise EditorialEnrichmentRevisionConflictError(
                    "The Editorial Enrichment inputs no longer match the base artifact"
                )

        return _EditorialEnrichmentRevisionFence(
            access_policy=access_policy,
            inventory=inventory,
            figure_catalog=figure_catalog,
            evidence_pack=evidence_pack,
            evidence_pack_hash=evidence_pack_hash,
            inventory_hash=inventory_hash,
            input_hash=input_hash,
        )

    @staticmethod
    def _legacy_input_fence_matches(
        *,
        base_artifact: ProductionArtifact,
        extraction: Any,
        synthesis: Any,
        projection: Any,
        resource_search_enabled: bool,
    ) -> bool:
        """Allow known v8 identities only when every admitted data input is unchanged."""
        metadata = base_artifact.metadata
        return (
            metadata.get("source_figure_provenance_version")
            == SOURCE_FIGURE_PROVENANCE_DIAGNOSTICS_VERSION
            and metadata.get("generator_version") == _LEGACY_EDITORIAL_ENRICHMENT_GENERATOR_VERSION
            and metadata.get("routing_policy_version")
            == _LEGACY_EDITORIAL_ENRICHMENT_ROUTING_POLICY_VERSION
            and not resource_search_enabled
            and metadata.get("extraction_hash") == canonical_extraction_hash(extraction)
            and metadata.get("synthesis_hash") == canonical_synthesis_hash(synthesis)
            and metadata.get("relevance_projection_hash")
            == (projection.projection_hash if projection is not None else None)
        )

    @staticmethod
    def _catalog_for_base_artifact(
        *, base_artifact: ProductionArtifact, extraction: Any, inventory: Any
    ) -> tuple[tuple[Any, ...], str | None]:
        stored_hash = base_artifact.metadata.get("source_figure_inventory_hash")
        current_hash = inventory.functional_hash()
        if stored_hash is None or stored_hash == current_hash:
            return build_editorial_figure_catalog(extraction, inventory), current_hash

        conflict_message = "The admitted figure catalog no longer matches the base artifact"
        diagnostics = base_artifact.metadata.get("source_figure_provenance_diagnostics")
        if (
            base_artifact.metadata.get("source_figure_provenance_version")
            != SOURCE_FIGURE_PROVENANCE_DIAGNOSTICS_VERSION
            or not isinstance(stored_hash, str)
            or not isinstance(diagnostics, list)
        ):
            raise EditorialEnrichmentRevisionConflictError(conflict_message)

        def diagnostic_signature(records: list[Any], figure_id: str) -> list[tuple[str, ...]]:
            signature: list[tuple[str, ...]] = []
            for record in records:
                if not isinstance(record, dict) or record.get("figure_id") != figure_id:
                    continue
                stage = record.get("stage")
                if stage in {"catalogued", "selected", "published"}:
                    continue
                if not isinstance(stage, str):
                    raise EditorialEnrichmentRevisionConflictError(conflict_message)
                signature.append(
                    (
                        stage,
                        str(record.get("source_document_id") or ""),
                        str(record.get("blob_id") or ""),
                        str(record.get("sha256") or ""),
                        str(record.get("reason_code") or ""),
                    )
                )
            return sorted(signature)

        admitted: dict[str, dict[str, Any]] = {}
        discovered_ids: set[str] = set()
        for record in diagnostics:
            if not isinstance(record, dict):
                raise EditorialEnrichmentRevisionConflictError(conflict_message)
            figure_id = record.get("figure_id")
            stage = record.get("stage")
            if stage == "discovered":
                if not isinstance(figure_id, str):
                    raise EditorialEnrichmentRevisionConflictError(conflict_message)
                discovered_ids.add(figure_id)
            if stage != "catalogued":
                continue
            if (
                not isinstance(figure_id, str)
                or not isinstance(record.get("source_document_id"), str)
                or not isinstance(record.get("figure_handle"), str)
                or figure_id in admitted
            ):
                raise EditorialEnrichmentRevisionConflictError(conflict_message)
            admitted[figure_id] = record

        current_by_id = {str(figure.figure_id): figure for figure in inventory.figures}
        if not set(admitted).issubset(discovered_ids) or not set(admitted).issubset(current_by_id):
            raise EditorialEnrichmentRevisionConflictError(conflict_message)
        for figure_id in admitted:
            if diagnostic_signature(diagnostics, figure_id) != diagnostic_signature(
                [record.to_json() for record in inventory.provenance_diagnostics], figure_id
            ):
                raise EditorialEnrichmentRevisionConflictError(conflict_message)

        admitted_ids = set(admitted)
        scoped_inventory = replace(
            inventory,
            figures=tuple(
                figure for figure in inventory.figures if str(figure.figure_id) in admitted_ids
            ),
            catalog_metadata={
                figure_id: metadata
                for figure_id, metadata in inventory.catalog_metadata.items()
                if str(figure_id) in admitted_ids
            },
        )
        figure_catalog = build_editorial_figure_catalog(extraction, scoped_inventory)
        current_catalog = {str(entry.figure.figure_id): entry for entry in figure_catalog}
        if set(current_catalog) != admitted_ids:
            raise EditorialEnrichmentRevisionConflictError(conflict_message)

        for figure_id, expected in admitted.items():
            entry = current_catalog[figure_id]
            figure = entry.figure
            expected_blob_id = expected.get("blob_id")
            expected_sha256 = expected.get("sha256")
            if (
                entry.handle != expected["figure_handle"]
                or str(figure.source_document_id) != expected["source_document_id"]
                or (expected_blob_id is None) != (expected_sha256 is None)
            ):
                raise EditorialEnrichmentRevisionConflictError(conflict_message)
            if expected_blob_id is not None:
                if (
                    figure.decision.value != "accepted"
                    or str(figure.blob_id) != expected_blob_id
                    or figure.sha256 != expected_sha256
                ):
                    raise EditorialEnrichmentRevisionConflictError(conflict_message)
            elif figure.decision.value == "accepted" or figure.decision_reason != expected.get(
                "reason_code"
            ):
                raise EditorialEnrichmentRevisionConflictError(conflict_message)

        # Legacy provenance v1 stores per-figure events but not a snapshot of the
        # inventory hash inputs. Keep the base catalog fenced: every admitted
        # figure and its archive decision must remain unchanged.
        return figure_catalog, stored_hash

    async def preview_targeted(
        self, request: TargetedEditorialRevisionRequest
    ) -> TargetedSynthesisRevisionPreview:
        self._validate_targeted_request(request)
        try:
            context = await self._load_context(
                subject_id=request.subject_id,
                base_artifact_id=request.base_artifact_id,
                base_version=request.base_version,
                base_input_hash=request.base_input_hash,
                base_canonical_sha256=request.base_canonical_sha256,
            )
        except EditorialEnrichmentRevisionConflictError as exc:
            from cti_app.domain.production_editorial_revision import (
                TargetedEditorialRevisionConflictError,
            )

            raise TargetedEditorialRevisionConflictError(str(exc)) from exc
        (
            run,
            snapshot,
            base_artifact,
            _base_payload,
            _base_enrichment,
            references_artifact,
            extraction_artifact,
            projection_artifact,
            synthesis_artifact,
            _previous_publication,
        ) = context
        extraction = await self._enrichment._load_extraction(run, snapshot, extraction_artifact)
        projection = (
            await self._enrichment._load_projection(run, snapshot, extraction, projection_artifact)
            if projection_artifact is not None
            else None
        )
        synthesis = await self._enrichment._load_synthesis(
            run, snapshot, synthesis_artifact, extraction
        )
        try:
            await self._validate_input_fence(
                snapshot=snapshot,
                base_artifact=base_artifact,
                extraction=extraction,
                projection=projection,
                synthesis=synthesis,
                read_only_media=True,
            )
        except EditorialEnrichmentRevisionConflictError as exc:
            from cti_app.domain.production_editorial_revision import (
                TargetedEditorialRevisionConflictError,
            )

            raise TargetedEditorialRevisionConflictError(str(exc)) from exc
        identity = self._targeted_request_identity(request)
        operation = request.operation
        is_delete = operation in {
            TargetedEditorialRevisionOperation.DELETE_TABLE,
            TargetedEditorialRevisionOperation.DELETE_DIAGRAM,
            TargetedEditorialRevisionOperation.DELETE_CHART,
            TargetedEditorialRevisionOperation.DELETE_SOURCE_FIGURE,
        }
        calls = 0
        if request.mode is TargetedEditorialRevisionMode.EXISTING_EVIDENCE_ONLY and not is_delete:
            references_blob_id = references_artifact.canonical_blob_id
            if references_blob_id is None:
                raise EditorialEnrichmentRevisionConflictError(
                    "The current References artifact has no canonical payload"
                )
            references = production_reference_corpus_from_json(
                await self._enrichment._artifact_store.read_json(references_blob_id)
            )
            from cti_app.application.production_synthesis_revision import (
                _critical_extraction_failures,
            )

            if not _critical_extraction_failures(extraction, references):
                run_id = uuid5(
                    NAMESPACE_URL,
                    f"production-editorial-enrichment-revision-model:{identity}",
                )
                calls = 0 if await self._enrichment._model_gateway.get_run(run_id) else 1
        target = request.target
        kind = target.element_kind.value if target.element_kind is not None else "unknown"
        key = target.element_key or "new"
        return TargetedSynthesisRevisionPreview(
            base_artifact_id=base_artifact.id,
            base_version=base_artifact.version,
            base_input_hash=base_artifact.input_hash,
            base_canonical_sha256=request.base_canonical_sha256,
            target_path=f"editorial_enrichment.{kind}[{key}]",
            stages_preserved=(
                ProductionArtifactStage.REFERENCES.value,
                ProductionArtifactStage.EXTRACTION.value,
                ProductionArtifactStage.RELEVANCE_PROJECTION.value,
                ProductionArtifactStage.SYNTHESIS.value,
            ),
            stages_invalidated=(ProductionArtifactStage.PUBLICATION.value,),
            stages_conditionally_rebased=(),
            source_extension=(request.mode is TargetedEditorialRevisionMode.ALLOW_SOURCE_EXTENSION),
            expected_model_calls=calls,
            enrichment_rebase="not_applicable; only the targeted enrichment artifact changes",
        )

    async def revise_targeted(
        self, request: TargetedEditorialRevisionRequest
    ) -> TargetedSynthesisRevisionResult:
        self._validate_targeted_request(request)
        assert request.target.element_kind is not None
        action = self._targeted_action(request.operation)
        try:
            result = await self.revise(
                subject_id=request.subject_id,
                base_artifact_id=request.base_artifact_id,
                base_version=request.base_version,
                base_input_hash=request.base_input_hash,
                base_canonical_sha256=request.base_canonical_sha256,
                element_kind=request.target.element_kind,
                element_key=request.target.element_key,
                action=action,
                instruction=request.instruction,
                targeted_request=request,
            )
            return cast(TargetedSynthesisRevisionResult, result)
        except EditorialEnrichmentRevisionConflictError as exc:
            from cti_app.domain.production_editorial_revision import (
                TargetedEditorialRevisionConflictError,
            )

            raise TargetedEditorialRevisionConflictError(str(exc)) from exc
        except EditorialEnrichmentRevisionValidationError as exc:
            raise TargetedSynthesisRevisionValidationError(
                "editorial_enrichment_revision_rejected",
                details={"rejections": exc.rejections},
            ) from exc

    @staticmethod
    def _targeted_request_identity_payload(
        request: TargetedEditorialRevisionRequest,
    ) -> dict[str, Any]:
        return {
            "subject_id": str(request.subject_id),
            "base_artifact_id": str(request.base_artifact_id),
            "base_version": request.base_version,
            "base_input_hash": request.base_input_hash,
            "base_canonical_sha256": request.base_canonical_sha256,
            "target": request.target.to_json(),
            "operation": request.operation.value,
            "instruction": request.instruction.strip(),
            "mode": request.mode.value,
            "requester": request.requester,
            "prompt_version": EDITORIAL_ENRICHMENT_REVISION_PROMPT_VERSION,
            "contract_version": EDITORIAL_ENRICHMENT_REVISION_CONTRACT_VERSION,
            "editorial_contract_prompt_version": EDITORIAL_ENRICHMENT_PROMPT_VERSION,
            "editorial_contract_version": EDITORIAL_ENRICHMENT_PROPOSAL_CONTRACT_VERSION,
            "parser_version": "editorial-enrichment-wire-parser-v1",
            "validator_version": "editorial-enrichment-validator-v1",
        }

    @classmethod
    def _targeted_request_identity(cls, request: TargetedEditorialRevisionRequest) -> str:
        return hashlib.sha256(
            ProductionArtifactStore.canonical_json_bytes(
                cls._targeted_request_identity_payload(request)
            )
        ).hexdigest()

    @staticmethod
    def _targeted_action(
        operation: TargetedEditorialRevisionOperation,
    ) -> EditorialEnrichmentRevisionAction:
        if operation in {
            TargetedEditorialRevisionOperation.DELETE_TABLE,
            TargetedEditorialRevisionOperation.DELETE_DIAGRAM,
            TargetedEditorialRevisionOperation.DELETE_CHART,
            TargetedEditorialRevisionOperation.DELETE_SOURCE_FIGURE,
        }:
            return EditorialEnrichmentRevisionAction.REMOVE_ELEMENT
        if operation in {
            TargetedEditorialRevisionOperation.REWRITE_TABLE,
            TargetedEditorialRevisionOperation.CREATE_TABLE,
        }:
            return EditorialEnrichmentRevisionAction.IMPROVE_TABLE
        if operation in {
            TargetedEditorialRevisionOperation.REWRITE_DIAGRAM,
            TargetedEditorialRevisionOperation.CREATE_DIAGRAM,
        }:
            return EditorialEnrichmentRevisionAction.DETAIL_DIAGRAM
        if operation in {
            TargetedEditorialRevisionOperation.REPLACE_SOURCE_FIGURE,
            TargetedEditorialRevisionOperation.REWRITE_SOURCE_FIGURE,
        }:
            return EditorialEnrichmentRevisionAction.CHOOSE_ANOTHER_FIGURE
        return EditorialEnrichmentRevisionAction.CUSTOM

    @classmethod
    def _validate_targeted_request(cls, request: TargetedEditorialRevisionRequest) -> None:
        target = request.target
        if target.kind is not TargetedEditorialRevisionTargetKind.ENRICHMENT_ELEMENT:
            raise ValueError("targeted_enrichment_revision_target_invalid")
        if target.element_kind is None:
            raise ValueError("targeted_enrichment_revision_kind_required")
        operation = request.operation
        create_operations = {
            TargetedEditorialRevisionOperation.CREATE_TABLE,
            TargetedEditorialRevisionOperation.CREATE_DIAGRAM,
            TargetedEditorialRevisionOperation.CREATE_CHART,
        }
        if operation in create_operations:
            if target.element_key is not None or target.placement is None:
                raise ValueError("targeted_enrichment_revision_create_target_invalid")
        elif target.element_key is None or target.placement is not None:
            raise ValueError("targeted_enrichment_revision_element_target_invalid")

    async def revise(
        self,
        *,
        subject_id: UUID,
        base_artifact_id: UUID,
        base_version: int,
        base_input_hash: str,
        base_canonical_sha256: str,
        element_kind: EditorialEnrichmentElementKind,
        element_key: str | None,
        action: EditorialEnrichmentRevisionAction,
        instruction: str,
        targeted_request: TargetedEditorialRevisionRequest | None = None,
    ) -> EditorialEnrichmentRevisionResult | TargetedSynthesisRevisionResult:
        normalized_instruction = instruction.strip()
        is_targeted_delete = targeted_request is not None and targeted_request.operation in {
            TargetedEditorialRevisionOperation.DELETE_TABLE,
            TargetedEditorialRevisionOperation.DELETE_DIAGRAM,
            TargetedEditorialRevisionOperation.DELETE_CHART,
            TargetedEditorialRevisionOperation.DELETE_SOURCE_FIGURE,
        }
        if (
            not normalized_instruction
            or len(normalized_instruction) > MAX_EDITORIAL_REVISION_INSTRUCTION_LENGTH
        ):
            raise ValueError("editorial_enrichment_revision_instruction_invalid")
        if targeted_request is not None:
            self._validate_targeted_request(targeted_request)
            if targeted_request.mode is TargetedEditorialRevisionMode.ALLOW_SOURCE_EXTENSION:
                return TargetedSynthesisRevisionResult(
                    artifact=None,
                    publication_artifact=None,
                    revision=None,
                    refusal=TargetedEditorialRevisionRefusal(
                        code=TargetedEditorialRevisionRefusalCode.SOURCE_EXTENSION_NOT_AVAILABLE_YET,
                        recommended_restart_stage=None,
                        reason=(
                            "Source extension requires the E2a archival and extraction workflow."
                        ),
                    ),
                )
            request_identity_payload = self._targeted_request_identity_payload(targeted_request)
            request_identity = self._targeted_request_identity(targeted_request)
            revision_artifact_id = uuid5(
                NAMESPACE_URL, f"targeted-editorial-enrichment-revision:{request_identity}"
            )
        else:
            if element_key is None or not element_key.strip() or len(element_key) > 128:
                raise ValueError("editorial_enrichment_revision_element_key_invalid")
            request_identity_payload = {
                "subject_id": str(subject_id),
                "base_artifact_id": str(base_artifact_id),
                "base_version": base_version,
                "base_input_hash": base_input_hash,
                "base_canonical_sha256": base_canonical_sha256,
                "element_kind": element_kind.value,
                "element_key": element_key,
                "action": action.value,
                "instruction": normalized_instruction,
                "revision_prompt_version": EDITORIAL_ENRICHMENT_REVISION_PROMPT_VERSION,
                "revision_contract_version": EDITORIAL_ENRICHMENT_REVISION_CONTRACT_VERSION,
            }
            request_identity = hashlib.sha256(
                ProductionArtifactStore.canonical_json_bytes(request_identity_payload)
            ).hexdigest()
            revision_artifact_id = uuid5(
                NAMESPACE_URL, f"production-editorial-enrichment-revision:{request_identity}"
            )
        self._validate_action(element_kind, action)

        # A replay resolves the deterministic result before checking staleness:
        # after success, the base is intentionally no longer current.
        async with self._enrichment._uow_factory() as uow:
            prior_result = await uow.production_artifacts.get(revision_artifact_id)
            if prior_result is not None:
                if prior_result.subject_id != subject_id:
                    raise EditorialEnrichmentRevisionConflictError(
                        "Revision identity belongs to another subject"
                    )
                prior_revision = prior_result.metadata.get("revision")
                if not isinstance(prior_revision, dict):
                    raise ValueError("editorial_enrichment_revision_result_corrupt")
                publication = self._publication_for_revision(
                    await uow.production_artifacts.list_for_run(prior_result.production_run_id),
                    prior_result.id,
                )
                if targeted_request is not None:
                    return TargetedSynthesisRevisionResult(
                        artifact=prior_result,
                        publication_artifact=publication,
                        revision=prior_revision,
                    )
                return EditorialEnrichmentRevisionResult(
                    artifact=prior_result,
                    outcome=EditorialEnrichmentRevisionOutcome(prior_revision["outcome"]),
                    revision=prior_revision,
                    publication_artifact=publication,
                    previous_publication_artifact_id=self._optional_uuid(
                        prior_revision.get("previous_publication_artifact_id")
                    ),
                )

        context = await self._load_context(
            subject_id=subject_id,
            base_artifact_id=base_artifact_id,
            base_version=base_version,
            base_input_hash=base_input_hash,
            base_canonical_sha256=base_canonical_sha256,
        )
        (
            run,
            snapshot,
            base_artifact,
            base_payload,
            base_enrichment,
            references_artifact,
            extraction_artifact,
            projection_artifact,
            synthesis_artifact,
            previous_publication,
        ) = context

        extraction = await self._enrichment._load_extraction(run, snapshot, extraction_artifact)
        projection = (
            await self._enrichment._load_projection(run, snapshot, extraction, projection_artifact)
            if projection_artifact is not None
            else None
        )
        synthesis = await self._enrichment._load_synthesis(
            run, snapshot, synthesis_artifact, extraction
        )

        if targeted_request is not None and not is_targeted_delete:
            references_blob_id = references_artifact.canonical_blob_id
            if references_blob_id is None:
                raise EditorialEnrichmentRevisionConflictError(
                    "The current References artifact has no canonical payload"
                )
            references_payload = await self._enrichment._artifact_store.read_json(
                references_blob_id
            )
            references = production_reference_corpus_from_json(references_payload)
            from cti_app.application.production_synthesis_revision import (
                _critical_extraction_failures,
            )

            if _critical_extraction_failures(extraction, references):
                return self._targeted_refusal(
                    "A critical source has an extraction failure; repair the evidence first.",
                    ProductionStage.EXTRACTION,
                )

        fence = await self._validate_input_fence(
            snapshot=snapshot,
            base_artifact=base_artifact,
            extraction=extraction,
            projection=projection,
            synthesis=synthesis,
        )
        access_policy = fence.access_policy
        inventory = fence.inventory
        figure_catalog = fence.figure_catalog
        evidence_pack = fence.evidence_pack
        evidence_pack_hash = fence.evidence_pack_hash
        inventory_hash = fence.inventory_hash
        input_hash = fence.input_hash
        if (access_policy.do_not_submit or not access_policy.external_llm_allowed) and not (
            is_targeted_delete
        ):
            if targeted_request is not None:
                return self._targeted_refusal(
                    "The current source access policy does not permit an editorial model call.",
                    None,
                )
            raise ValueError("editorial_enrichment_revision_policy_blocked")
        list_name = {
            EditorialEnrichmentElementKind.TABLE: "tables",
            EditorialEnrichmentElementKind.DIAGRAM: "diagrams",
            EditorialEnrichmentElementKind.CHART: "charts",
            EditorialEnrichmentElementKind.FIGURE: "source_figures",
        }[element_kind]
        base_elements = base_payload.get(list_name)
        if not isinstance(base_elements, list):
            raise ValueError("editorial_enrichment_revision_base_element_missing")
        is_create = targeted_request is not None and targeted_request.operation in {
            TargetedEditorialRevisionOperation.CREATE_TABLE,
            TargetedEditorialRevisionOperation.CREATE_DIAGRAM,
            TargetedEditorialRevisionOperation.CREATE_CHART,
        }
        if is_create:
            element_key = self._generated_element_key(element_kind, request_identity, base_elements)
        assert element_key is not None
        element_index = next(
            (
                index
                for index, value in enumerate(base_elements)
                if isinstance(value, dict) and value.get("key") == element_key
            ),
            None,
        )
        if is_create and element_index is not None:
            raise EditorialEnrichmentRevisionConflictError(
                "The deterministic enrichment key is already in use"
            )
        if not is_create and element_index is None:
            raise ValueError("editorial_enrichment_revision_base_element_missing")
        base_element = base_elements[element_index] if element_index is not None else None
        element_evidence_handles = (
            self._element_evidence_handles(
                element_kind, base_enrichment, element_key, evidence_pack
            )
            if not is_create
            else []
        )
        admitted_handles = sorted(evidence_pack._handle_to_ref)
        is_delete = (
            targeted_request is not None
            and action is EditorialEnrichmentRevisionAction.REMOVE_ELEMENT
        )
        execution = None
        raw_text: str | None = None
        parse_identity: str | None = None
        validator_rejections: list[dict[str, str]] = []
        parsed: EditorialEnrichmentWireParseResult | None = None
        if not is_delete:
            editorial_contract: dict[str, Any] | None = None
            if is_create:
                base_editorial_request = build_editorial_enrichment_model_request(
                    run,
                    snapshot,
                    extraction,
                    synthesis,
                    evidence_pack,
                    access_policy,
                    source_figure_inventory_hash=inventory_hash,
                    figure_catalog=figure_catalog,
                    resource_search_enabled=self._enrichment._resource_search_enabled,
                )
                base_editorial_prompt = json.loads(base_editorial_request.text)
                editorial_contract = {
                    "prompt_version": EDITORIAL_ENRICHMENT_PROMPT_VERSION,
                    "contract_version": EDITORIAL_ENRICHMENT_PROPOSAL_CONTRACT_VERSION,
                    "instructions": base_editorial_prompt["instructions"],
                    "editorial_guidance": base_editorial_prompt["editorial_guidance"],
                    "output_contract": base_editorial_prompt["output_contract"],
                }
            placement_json = (
                targeted_request.target.placement.to_json()
                if targeted_request is not None and targeted_request.target.placement is not None
                else None
            )
            request = self._model_request(
                request_identity=request_identity,
                request_identity_payload=request_identity_payload,
                action=action,
                instruction=normalized_instruction,
                element_kind=element_kind,
                element_key=element_key,
                base_element=base_element,
                element_evidence_handles=element_evidence_handles,
                evidence_pack=evidence_pack,
                evidence_pack_hash=evidence_pack_hash,
                figure_catalog=figure_catalog,
                access_policy=access_policy,
                create_mode=is_create,
                current_enrichment=base_payload if is_create else None,
                stable_synthesis_payload=(
                    production_synthesis_to_json(synthesis) if is_create else None
                ),
                target_placement=placement_json,
                editorial_contract=editorial_contract,
            )
            existing, archive_error = await self._enrichment._verified_existing_execution(request)
            if archive_error is not None:
                raise EditorialEnrichmentRevisionValidationError(
                    [
                        {
                            "block_id": "response",
                            "reason_code": str(archive_error.get("wire_error_code")),
                        }
                    ]
                )
            execution = existing or await self._enrichment._model_gateway.draft(request)
            if execution.run.status.value != "succeeded":
                raise EditorialEnrichmentRevisionValidationError(
                    [{"block_id": "response", "reason_code": "editorial_enrichment_model_failed"}]
                )
            raw_text, raw_error = await self._enrichment._verified_raw_text(
                execution.run,
                expected_text=execution.output_text if existing is None else None,
            )
            if raw_error is not None or raw_text is None:
                raise EditorialEnrichmentRevisionValidationError(
                    [
                        {
                            "block_id": "response",
                            "reason_code": str((raw_error or {}).get("wire_error_code")),
                        }
                    ]
                )
            parsed = parse_editorial_enrichment_proposal_wire(
                raw_text, evidence_pack, figure_catalog=figure_catalog
            )
            parse_identity = await self._enrichment._record_wire_parse(
                execution.run, evidence_pack, parsed, figure_catalog=figure_catalog
            )
            validator_rejections = [
                {"block_id": item.block_id, "reason_code": item.reason_code}
                for item in parsed.rejections
            ]
            if parsed.error_code is not None:
                validator_rejections.append(
                    {"block_id": "proposal", "reason_code": parsed.error_code}
                )
            if targeted_request is not None and parsed.explicit_empty:
                reason = (
                    "The evidence does not establish a useful enrichment for this target."
                    if is_create
                    else (
                        "The proposed rewrite is not analytically useful; "
                        "delete the element instead."
                    )
                )
                return self._targeted_refusal(reason, None)
            if parsed.proposal is None:
                if targeted_request is not None:
                    return self._targeted_refusal(
                        "; ".join(item["reason_code"] for item in validator_rejections)
                        or "The proposal is not supported by admitted evidence.",
                        ProductionStage.EXTRACTION,
                    )
                raise EditorialEnrichmentRevisionValidationError(validator_rejections)
            if targeted_request is not None and parsed.proposal.resource_needs:
                return self._targeted_refusal(
                    "The requested enrichment needs evidence absent from the admitted pack.",
                    ProductionStage.REFERENCES,
                )
        else:
            assert targeted_request is not None

        outcome = EditorialEnrichmentRevisionOutcome.REVISED
        is_remove = action is EditorialEnrichmentRevisionAction.REMOVE_ELEMENT
        resource_need: dict[str, Any] | None = None
        if parsed is not None and parsed.proposal is not None and parsed.proposal.resource_needs:
            need = parsed.proposal.resource_needs[0]
            unique_key = self._next_resource_need_key(base_enrichment)
            canonical_need = EditorialResourceNeedV1(
                key=unique_key,
                kind=need.kind,
                reason=need.reason,
                query_hint=need.query_hint,
                policy_version=EDITORIAL_RESOURCE_PROPOSAL_POLICY_VERSION,
            )
            if base_enrichment.schema_version >= 3:
                revised_enrichment = replace(
                    base_enrichment,
                    resource_needs=(*base_enrichment.resource_needs, canonical_need),
                )
            else:
                revised_enrichment = base_enrichment
            outcome = EditorialEnrichmentRevisionOutcome.NEEDS_NEW_EVIDENCE
            resource_need = {
                "key": canonical_need.key,
                "kind": canonical_need.kind.value,
                "reason": canonical_need.reason,
                "query_hint": canonical_need.query_hint,
            }
            after_element = base_element
        elif is_delete:
            revised_enrichment = self._remove_target(
                base_enrichment, element_kind=element_kind, element_key=element_key
            )
            validate_editorial_enrichment(
                revised_enrichment, extraction=extraction, synthesis=synthesis
            )
            after_element = None
        elif is_remove:
            if parsed is None or not parsed.explicit_empty:
                raise EditorialEnrichmentRevisionValidationError(
                    [
                        *validator_rejections,
                        {
                            "block_id": element_key,
                            "reason_code": (
                                "editorial_enrichment_revision_remove_requires_empty_marker"
                            ),
                        },
                    ]
                )
            revised_enrichment = self._remove_target(
                base_enrichment, element_kind=element_kind, element_key=element_key
            )
            validate_editorial_enrichment(
                revised_enrichment, extraction=extraction, synthesis=synthesis
            )
            after_element = None
        else:
            assert parsed is not None and parsed.proposal is not None
            try:
                candidate = self._validate_target_proposal(
                    parsed,
                    element_kind=element_kind,
                    element_key=element_key,
                    evidence_pack=evidence_pack,
                    extraction=extraction,
                    synthesis=synthesis,
                    figure_catalog=figure_catalog,
                    validator_rejections=validator_rejections,
                )
                candidate_elements = {
                    EditorialEnrichmentElementKind.TABLE: candidate.tables,
                    EditorialEnrichmentElementKind.DIAGRAM: candidate.diagrams,
                    EditorialEnrichmentElementKind.CHART: candidate.charts,
                    EditorialEnrichmentElementKind.FIGURE: candidate.source_figures,
                }[element_kind]
                if len(candidate_elements) != 1:
                    reason_code = "editorial_enrichment_target_failed_final_validation"
                    try:
                        validate_editorial_enrichment_proposal(
                            parsed.proposal,
                            evidence_pack,
                            extraction,
                            synthesis,
                            figure_catalog=figure_catalog,
                            _drop_invalid_blocks=False,
                        )
                    except EditorialEnrichmentProposalControlError as exc:
                        code = getattr(exc, "code", None)
                        reason_code = (
                            str(getattr(code, "value", code)) if code is not None else reason_code
                        )
                    validator_rejections.append(
                        {"block_id": element_key, "reason_code": reason_code}
                    )
                    raise EditorialEnrichmentRevisionValidationError(validator_rejections)
                if is_create:
                    self._assert_requested_placement(
                        candidate=candidate,
                        element_kind=element_kind,
                        targeted_request=targeted_request,
                        synthesis=synthesis,
                    )
                    revised_enrichment = await self._insert_target(
                        base_enrichment,
                        candidate,
                        element_kind=element_kind,
                        element_key=element_key,
                        run_id=run.id,
                    )
                else:
                    if (
                        element_kind is EditorialEnrichmentElementKind.FIGURE
                        and action is EditorialEnrichmentRevisionAction.CHOOSE_ANOTHER_FIGURE
                    ):
                        previous_figure = next(
                            item
                            for item in base_enrichment.source_figures
                            if item.key == element_key
                        )
                        replacement_figure = candidate.source_figures[0]
                        previous_figure_id = (
                            previous_figure.resolved_figure.figure_id
                            if previous_figure.resolved_figure is not None
                            else None
                        )
                        replacement_figure_id = (
                            replacement_figure.resolved_figure.figure_id
                            if replacement_figure.resolved_figure is not None
                            else None
                        )
                        if previous_figure_id == replacement_figure_id:
                            raise EditorialEnrichmentRevisionValidationError(
                                [
                                    {
                                        "block_id": element_key,
                                        "reason_code": "editorial_enrichment_figure_not_changed",
                                    }
                                ]
                            )
                    revised_enrichment = await self._replace_target(
                        base_enrichment,
                        candidate,
                        element_kind=element_kind,
                        element_key=element_key,
                        run_id=run.id,
                    )
                    anchor_texts = semantic_annotation_anchor_texts(synthesis, revised_enrichment)
                    retained_annotations = tuple(
                        annotation
                        for annotation in revised_enrichment.annotations
                        if (anchor_text := anchor_texts.get(annotation.paragraph_anchor))
                        is not None
                        and annotation.text in anchor_text
                    )
                    if retained_annotations != revised_enrichment.annotations:
                        revised_enrichment = replace(
                            revised_enrichment, annotations=retained_annotations
                        )
                validate_editorial_enrichment(
                    revised_enrichment,
                    extraction=extraction,
                    synthesis=synthesis,
                )
            except EditorialEnrichmentRevisionValidationError as exc:
                if targeted_request is not None:
                    reason = (
                        "; ".join(item["reason_code"] for item in exc.rejections)
                        or "The proposal failed an editorial enrichment rule."
                    )
                    if any(token in reason for token in ("trivial", "ioc_republication")):
                        reason = (
                            f"{reason}; delete the element instead of keeping a trivial rewrite."
                        )
                    return self._targeted_refusal(
                        reason,
                        ProductionStage.EXTRACTION,
                    )
                raise
            except ValueError as exc:
                if targeted_request is not None:
                    return self._targeted_refusal(
                        str(exc) or "The proposal failed canonical enrichment validation.",
                        ProductionStage.EXTRACTION,
                    )
                raise
            after_payload = editorial_enrichment_to_json(revised_enrichment)
            after_element = (
                after_payload[list_name][-1]
                if is_create
                else after_payload[list_name][element_index]
            )

        revised_payload = editorial_enrichment_to_json(revised_enrichment)
        if not is_remove and outcome is EditorialEnrichmentRevisionOutcome.REVISED:
            element_evidence_handles = self._element_evidence_handles(
                element_kind, revised_enrichment, element_key, evidence_pack
            )
        # Preserve every untouched serialized element exactly as it appeared in
        # the base canonical payload. Only the requested block and L7b needs
        # collection are replaced.
        final_payload = dict(base_payload)
        final_payload[list_name] = list(base_elements)
        if is_remove:
            assert element_index is not None
            final_payload[list_name].pop(element_index)
        elif is_create:
            final_payload[list_name].append(after_element)
        elif outcome is EditorialEnrichmentRevisionOutcome.REVISED:
            assert element_index is not None
            final_payload[list_name][element_index] = after_element
        if element_kind is EditorialEnrichmentElementKind.FIGURE:
            # Decision traces are coupled to the selected source figure. A
            # replacement or deletion must carry the matching trace set.
            final_payload["figure_decisions"] = revised_payload["figure_decisions"]
        if revised_payload.get("annotations") != base_payload.get("annotations", []):
            # Semantic annotations anchored to a deleted or rewritten enrichment
            # block follow that block; unrelated annotation bytes are preserved.
            final_payload["annotations"] = revised_payload["annotations"]
        if targeted_request is None and "resource_needs" in revised_payload:
            final_payload["resource_needs"] = revised_payload["resource_needs"]
        before_publication_id = previous_publication.id if previous_publication else None
        revision_data: dict[str, Any] = {
            "request_identity": request_identity,
            "outcome": outcome.value,
            "element_kind": element_kind.value,
            "element_key": element_key,
            "action": action.value,
            "instruction": normalized_instruction,
            "base_artifact_id": str(base_artifact.id),
            "base_version": base_artifact.version,
            "base_input_hash": base_artifact.input_hash,
            "base_canonical_sha256": base_canonical_sha256,
            "element_evidence_handles": element_evidence_handles,
            "admitted_evidence_handles": admitted_handles,
            "element_before": base_element,
            "element_after": after_element,
            "validator_results": [
                {"validator": "l1b_text_block_parser", "status": "passed"},
                {"validator": "l7b_evidence_handles", "status": "passed"},
                {"validator": "l8_enrichment_grounding", "status": "passed"},
                {"validator": "canonical_enrichment", "status": "passed"},
            ],
            "validator_rejections": validator_rejections,
            "parse_identity": parse_identity,
            "previous_publication_artifact_id": (
                str(before_publication_id) if before_publication_id else None
            ),
            "resource_need": resource_need,
        }
        if targeted_request is not None and is_delete:
            revision_data["validator_results"] = [
                {"validator": "target_scope", "status": "passed"},
                {"validator": "canonical_enrichment", "status": "passed"},
            ]
        revision_data["source_figure_provenance_version"] = (
            SOURCE_FIGURE_PROVENANCE_DIAGNOSTICS_VERSION
        )
        revision_data["source_figure_provenance_diagnostics"] = (
            build_source_figure_provenance_diagnostics(
                inventory, figure_catalog, revised_enrichment
            )
        )
        element_provenance: dict[str, Any] | None = None
        if targeted_request is not None:
            revision_id = str(revision_artifact_id)
            target_json = targeted_request.target.to_json()
            target_json["element_key"] = element_key
            before_bytes = ProductionArtifactStore.canonical_json_bytes({"value": base_element})
            after_bytes = ProductionArtifactStore.canonical_json_bytes({"value": after_element})
            evidence_refs = [
                {
                    "handle": handle,
                    "ref": {
                        "source_document_id": str(ref.source_document_id),
                        "kind": ref.kind.value,
                        "evidence_key": ref.evidence_key,
                    },
                }
                for handle in element_evidence_handles
                if (ref := evidence_pack._handle_to_ref.get(handle)) is not None
            ]
            revision_data.update(
                {
                    "revision_id": revision_id,
                    "base_artifact_hash": base_canonical_sha256,
                    "target": target_json,
                    "target_path": f"editorial_enrichment.{element_kind.value}[{element_key}]",
                    "operation": targeted_request.operation.value,
                    "request": normalized_instruction,
                    "mode": targeted_request.mode.value,
                    "actor": targeted_request.requester,
                    "model_run_id": str(execution.run.id) if execution is not None else None,
                    "prompt_version": EDITORIAL_ENRICHMENT_REVISION_PROMPT_VERSION,
                    "contract_version": EDITORIAL_ENRICHMENT_REVISION_CONTRACT_VERSION,
                    "editorial_contract_prompt_version": EDITORIAL_ENRICHMENT_PROMPT_VERSION,
                    "editorial_contract_version": EDITORIAL_ENRICHMENT_PROPOSAL_CONTRACT_VERSION,
                    "parser_version": "editorial-enrichment-wire-parser-v1",
                    "validator_version": "editorial-enrichment-validator-v1",
                    "evidence_refs": evidence_refs,
                    "evidence_handles": element_evidence_handles,
                    "source_additions": [],
                    "before_hash": hashlib.sha256(before_bytes).hexdigest(),
                    "after_hash": hashlib.sha256(after_bytes).hexdigest(),
                    "rationale": (
                        "Target deleted without a model call."
                        if is_delete
                        else "Element validated against the admitted enrichment evidence pack."
                    ),
                    "created_at": datetime.now(UTC).isoformat(),
                    "result_artifact_id": str(revision_artifact_id),
                }
            )
            element_provenance = await self._element_provenance_after_revision(
                base_artifact=base_artifact,
                base_payload=base_payload,
                revised_payload=final_payload,
                element_kind=element_kind,
                element_key=element_key,
                revision_id=revision_id,
                result_artifact_id=revision_artifact_id,
                is_delete=is_delete,
            )
        artifact = await self._persistence.store_editorial_enrichment_result(
            run_id=run.id,
            subject_id=subject_id,
            input_hash=input_hash,
            enrichment=revised_enrichment,
            extraction=extraction,
            synthesis=synthesis,
            raw_result=raw_text,
            model_run_id=execution.run.id if execution is not None else None,
            evidence_pack_hash=evidence_pack_hash,
            access_policy_hash=base_artifact.metadata["access_policy_hash"],
            model_policy_version=EDITORIAL_ENRICHMENT_MODEL_POLICY_VERSION,
            routing_policy_version=str(base_artifact.metadata.get("routing_policy_version", "")),
            projection_hash=projection.projection_hash if projection is not None else None,
            source_figure_inventory_hash=inventory_hash,
            metadata_extra={
                "source_figure_provenance_version": SOURCE_FIGURE_PROVENANCE_DIAGNOSTICS_VERSION,
                "source_figure_provenance_diagnostics": build_source_figure_provenance_diagnostics(
                    inventory, figure_catalog, revised_enrichment
                ),
                "revision": revision_data,
                **(
                    {"element_provenance": element_provenance}
                    if element_provenance is not None
                    else {}
                ),
            },
            artifact_id=revision_artifact_id,
            expected_current_artifact_id=base_artifact.id,
            expected_current_input_hash=base_artifact.input_hash,
            expected_current_canonical_sha256=base_canonical_sha256,
            canonical_payload=final_payload,
        )
        publication = await self._assemble_and_render(
            run=run,
            snapshot=snapshot,
            references_artifact=references_artifact,
            extraction_artifact=extraction_artifact,
            projection_artifact=projection_artifact,
            synthesis_artifact=synthesis_artifact,
            enrichment_artifact=artifact,
            references=production_reference_corpus_from_json(
                await self._enrichment._artifact_store.read_json(
                    references_artifact.canonical_blob_id
                )
            ),
            extraction=extraction,
            projection=projection,
            synthesis=synthesis,
            enrichment=revised_enrichment,
        )
        if targeted_request is not None:
            return TargetedSynthesisRevisionResult(
                artifact=artifact,
                publication_artifact=publication,
                revision=revision_data,
            )
        return EditorialEnrichmentRevisionResult(
            artifact=artifact,
            outcome=outcome,
            revision=revision_data,
            publication_artifact=publication,
            previous_publication_artifact_id=before_publication_id,
        )

    async def _load_context(
        self,
        *,
        subject_id: UUID,
        base_artifact_id: UUID,
        base_version: int,
        base_input_hash: str,
        base_canonical_sha256: str,
    ) -> tuple[Any, ...]:
        from cti_app.domain.production import ProductionArtifactStage

        async with self._enrichment._uow_factory() as uow:
            run = await uow.production_runs.get_current_for_subject(subject_id)
            base = await uow.production_artifacts.get(base_artifact_id)
            if run is None or base is None or base.production_run_id != run.id:
                raise EditorialEnrichmentRevisionConflictError(
                    "The requested base artifact is no longer current"
                )
            current = await uow.production_artifacts.get_current(
                run.id, ProductionArtifactStage.EDITORIAL_ENRICHMENT.value
            )
            if (
                current is None
                or current.id != base.id
                or base.version != base_version
                or base.input_hash != base_input_hash
                or base.status is not ProductionArtifactStatus.VERIFIED
                or base.canonical_blob_id is None
            ):
                raise EditorialEnrichmentRevisionConflictError(
                    "The requested base artifact is stale"
                )
            snapshot = await uow.production_input_snapshots.get_by_run(run.id)
            references = await uow.production_artifacts.get_current(
                run.id, ProductionArtifactStage.REFERENCES.value
            )
            extraction = await uow.production_artifacts.get_current(
                run.id, ProductionArtifactStage.EXTRACTION.value
            )
            projection = await uow.production_artifacts.get_current(
                run.id, ProductionArtifactStage.RELEVANCE_PROJECTION.value
            )
            synthesis = await uow.production_artifacts.get_current(
                run.id, ProductionArtifactStage.SYNTHESIS.value
            )
            publication = await uow.production_artifacts.get_current(
                run.id, ProductionArtifactStage.PUBLICATION.value
            )
        if snapshot is None or references is None or extraction is None or synthesis is None:
            raise ValueError("editorial_enrichment_revision_inputs_missing")
        for stage_artifact in (references, extraction, synthesis):
            if (
                stage_artifact.status is not ProductionArtifactStatus.VERIFIED
                or stage_artifact.canonical_blob_id is None
            ):
                raise ValueError("editorial_enrichment_revision_inputs_missing")
        base_bytes = await self._enrichment._artifact_store.read_bytes(base.canonical_blob_id)
        if hashlib.sha256(base_bytes).hexdigest() != base_canonical_sha256:
            raise EditorialEnrichmentRevisionConflictError(
                "The requested base artifact content hash changed"
            )
        base_payload = json.loads(base_bytes)
        if not isinstance(base_payload, dict):
            raise ValueError("editorial_enrichment_revision_base_invalid")
        base_enrichment = editorial_enrichment_from_json(base_payload)
        return (
            run,
            snapshot,
            base,
            base_payload,
            base_enrichment,
            references,
            extraction,
            projection,
            synthesis,
            publication,
        )

    @staticmethod
    def _validate_action(
        element_kind: EditorialEnrichmentElementKind,
        action: EditorialEnrichmentRevisionAction,
    ) -> None:
        allowed = {
            EditorialEnrichmentRevisionAction.REMOVE_ELEMENT: {
                EditorialEnrichmentElementKind.TABLE,
                EditorialEnrichmentElementKind.DIAGRAM,
                EditorialEnrichmentElementKind.CHART,
                EditorialEnrichmentElementKind.FIGURE,
            },
            EditorialEnrichmentRevisionAction.IMPROVE_TABLE: {EditorialEnrichmentElementKind.TABLE},
            EditorialEnrichmentRevisionAction.DETAIL_DIAGRAM: {
                EditorialEnrichmentElementKind.DIAGRAM
            },
            EditorialEnrichmentRevisionAction.CHANGE_CAPTION_PLACEMENT: {
                EditorialEnrichmentElementKind.TABLE,
                EditorialEnrichmentElementKind.DIAGRAM,
                EditorialEnrichmentElementKind.CHART,
                EditorialEnrichmentElementKind.FIGURE,
            },
            EditorialEnrichmentRevisionAction.CHOOSE_ANOTHER_FIGURE: {
                EditorialEnrichmentElementKind.FIGURE
            },
            EditorialEnrichmentRevisionAction.CUSTOM: set(EditorialEnrichmentElementKind),
        }
        if element_kind not in allowed[action]:
            raise ValueError("editorial_enrichment_revision_action_mismatch")

    @staticmethod
    def _element_evidence_handles(
        element_kind: EditorialEnrichmentElementKind,
        enrichment: EditorialEnrichmentV1,
        element_key: str,
        evidence_pack: EditorialEnrichmentEvidencePackV1,
    ) -> list[str]:
        refs: set[ExtractionEvidenceRefV1] = set()
        if element_kind is EditorialEnrichmentElementKind.TABLE:
            table_element = next(item for item in enrichment.tables if item.key == element_key)
            refs.update(ref for row in table_element.rows for ref in row.evidence_refs)
            if table_element.purpose is not None:
                refs.update(table_element.purpose.evidence_refs)
        elif element_kind is EditorialEnrichmentElementKind.DIAGRAM:
            diagram_element = next(item for item in enrichment.diagrams if item.key == element_key)
            refs.update(ref for node in diagram_element.nodes for ref in node.evidence_refs)
            refs.update(ref for edge in diagram_element.edges for ref in edge.evidence_refs)
            if diagram_element.purpose is not None:
                refs.update(diagram_element.purpose.evidence_refs)
        elif element_kind is EditorialEnrichmentElementKind.CHART:
            chart_element = next(item for item in enrichment.charts if item.key == element_key)
            refs.update(ref for point in chart_element.points for ref in point.evidence_refs)
            if chart_element.purpose is not None:
                refs.update(chart_element.purpose.evidence_refs)
        else:
            figure_element = next(
                item for item in enrichment.source_figures if item.key == element_key
            )
            if figure_element.resolved_figure is not None:
                decision = next(
                    (
                        item
                        for item in enrichment.figure_decisions
                        if item.figure_id == figure_element.resolved_figure.figure_id
                    ),
                    None,
                )
                if decision is not None:
                    refs.update(decision.evidence_refs)
        handles_by_ref = {ref: handle for handle, ref in evidence_pack._handle_to_ref.items()}
        if not refs <= handles_by_ref.keys():
            raise EditorialEnrichmentRevisionConflictError(
                "The base element cites evidence outside its admitted evidence pack"
            )
        handles = sorted(handles_by_ref[ref] for ref in refs)
        if element_kind is EditorialEnrichmentElementKind.FIGURE:
            source_figure = next(
                item for item in enrichment.source_figures if item.key == element_key
            )
            if source_figure.resolved_figure is not None:
                decision = next(
                    (
                        item
                        for item in enrichment.figure_decisions
                        if item.figure_id == source_figure.resolved_figure.figure_id
                    ),
                    None,
                )
                if decision is not None:
                    handles.append(decision.handle)
        return sorted(set(handles))

    @staticmethod
    def _model_request(
        *,
        request_identity: str,
        request_identity_payload: dict[str, Any],
        action: EditorialEnrichmentRevisionAction,
        instruction: str,
        element_kind: EditorialEnrichmentElementKind,
        element_key: str,
        base_element: dict[str, Any] | None,
        element_evidence_handles: list[str],
        evidence_pack: EditorialEnrichmentEvidencePackV1,
        evidence_pack_hash: str,
        figure_catalog: tuple[Any, ...],
        access_policy: Any,
        create_mode: bool = False,
        current_enrichment: dict[str, Any] | None = None,
        stable_synthesis_payload: dict[str, Any] | None = None,
        target_placement: dict[str, Any] | None = None,
        editorial_contract: dict[str, Any] | None = None,
    ) -> Any:
        from cti_app.application.model_gateway import ModelRequest, ModelRoutingHint

        target_base_element = base_element
        if element_kind is EditorialEnrichmentElementKind.FIGURE and base_element is not None:
            resolved = base_element.get("resolved_figure")
            resolved_id = resolved.get("figure_id") if isinstance(resolved, dict) else None
            catalog_record: dict[str, Any] = next(
                (
                    entry.prompt_record(
                        evidence_handles=evidence_pack.evidence_handles_for_source(
                            entry.figure.source_document_id
                        )
                    )
                    for entry in figure_catalog
                    if str(entry.figure.figure_id) == resolved_id
                ),
                {},
            )
            from cti_app.application.production_editorial_enrichment import (
                _safe_figure_prompt_text,
            )

            target_base_element = {
                "key": element_key,
                "caption": _safe_figure_prompt_text(base_element.get("caption")),
                "figure_handle": catalog_record.get("handle"),
                "anchor": catalog_record.get("anchor")
                or catalog_record.get("figure_label")
                or catalog_record.get("nearby_heading"),
                "page": catalog_record.get("page"),
                "dimensions": catalog_record.get("dimensions", {"width": None, "height": None}),
                "provenance_summary": catalog_record.get(
                    "provenance_summary", "Archived source media"
                ),
            }

        prompt_payload = {
            "instructions": (
                (
                    "Crée uniquement l'élément ciblé avec la clé fournie; n'ajoute ni ne réécris "
                    "aucun autre élément. Le pipeline demandé et chaque relation doivent être "
                    "établis par les preuves admises. Si ce n'est pas démontré, retourne "
                    "uniquement NO USEFUL ENRICHMENT. Respecte le placement demandé et le "
                    "contrat EDITORIAL_ENRICHMENT fourni. "
                    if create_mode
                    else "Révise uniquement l'élément ciblé. "
                )
                + "Utilise exclusivement les handles du "
                "paquet de preuves admis. Conserve les littéraux exactement. Ne fais aucune "
                "recherche, aucun appel web, n'invente aucun fait et ne modifie pas la "
                "narration. Si l'instruction exige une information absente des preuves, "
                "émets uniquement un bloc NEEDS avec un motif et un query_hint, sans proposer "
                "l'élément révisé. Pour l'action remove_element, retourne uniquement le marqueur "
                "exact NO USEFUL ENRICHMENT afin de supprimer l'élément ciblé; sinon retourne "
                "un seul bloc du type ciblé (TABLE, DIAGRAM, CHART ou FIGURE), "
                "en conservant sa clé "
                "pour TABLE/DIAGRAM/CHART. Respecte strictement "
                "les handles et règles de validation fournis. Pour une FIGURE, sélectionne "
                "uniquement un FIGURE_HANDLE accepté du catalogue, avec des evidence handles "
                "de la même source. Une architecture, chaîne d'exploitation ou chaîne malware, "
                "infrastructure, capture réseau, capture de code informative, screenshot de "
                "leurre, visualisation d'analyse ou graphique source peut être utile. Rejette "
                "logo, header/footer, bannière, hero décoratif, illustration marketing, portrait "
                "sans valeur analytique, image d'article adjacent ou figure sans lien direct aux "
                "preuves. Une URL ou une image ne remplace jamais le handle. La caption doit "
                "être courte et descriptive; si elle manque, le code préfère la caption source, "
                "puis un alt exploitable, et refuse de publier sans l'un de ces textes. Une table "
                "ne doit pas republier les IP, domaines, URL, hachés ou e-mails projetés lorsque "
                "ses autres colonnes sont constantes ou répètent seulement leur type; conserve-la "
                "si elle relie les IOC à des attributs analytiques utiles et documentés. Un "
                "diagramme doit clarifier une séquence, un pipeline, une architecture, des "
                "branches, une convergence, plusieurs relations, un contrôle-flow, une propagation "
                "ou un mapping complexe. Une branche ou convergence à un seul niveau autour d'un "
                "seul nœud ne suffit pas si au moins 80% des libellés réutilisent au moins la "
                "moitié "
                "de leurs termes de contenu d'une phrase d'ancrage, sans ajouter de chemin multi-"
                "étapes ou de mapping distinct; omets alors le bloc. "
                "Aucun nombre minimal de nœuds ne s'applique. Dans GAIN, réponds obligatoirement à "
                "« Qu'est-ce que le lecteur comprend mieux grâce à cet élément ? » par un gain "
                "concret; sinon omets le bloc."
            ),
            "action": action.value,
            "instruction": instruction,
            "target": {
                "kind": element_kind.value,
                "key": element_key,
                "base_element": target_base_element,
                "base_element_evidence_handles": element_evidence_handles,
                "placement": target_placement,
            },
            "current_editorial_enrichment": current_enrichment,
            "editorial_contract": editorial_contract,
            "stable_current_synthesis": (
                ProductionEditorialEnrichmentRevisionService._render_stable_synthesis(
                    stable_synthesis_payload
                )
                if stable_synthesis_payload is not None
                else None
            ),
            "requested_operation": request_identity_payload.get("operation", action.value),
            "admitted_evidence_pack": {
                "schema_version": 4,
                "policy_version": evidence_pack.policy_version,
                "relevance_projection_hash": evidence_pack.projection_hash,
                "publication_language": evidence_pack.publication_language,
                "current_synthesis": dict(evidence_pack.current_synthesis),
                "narrative_evidence": [dict(value) for value in evidence_pack.narrative_evidence],
                "technical_evidence": [dict(value) for value in evidence_pack.technical_evidence],
                "reserve_evidence": [dict(value) for value in evidence_pack.reserve_evidence],
                "source_pair_relations": [
                    dict(value) for value in evidence_pack.source_pair_relations
                ],
            },
            "figure_catalog": [
                value.prompt_record(
                    evidence_handles=evidence_pack.evidence_handles_for_source(
                        value.figure.source_document_id
                    )
                )
                for value in figure_catalog
            ],
            "output_contract": editorial_enrichment_output_contract_example(),
            "output_contract_version": (EDITORIAL_ENRICHMENT_REVISION_CONTRACT_VERSION),
        }
        return ModelRequest(
            text=json.dumps(
                prompt_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ),
            prompt_template_id="production-editorial-enrichment",
            prompt_template_version=EDITORIAL_ENRICHMENT_REVISION_PROMPT_VERSION,
            evidence_pack_hash=evidence_pack_hash,
            external_llm_allowed=access_policy.external_llm_allowed
            and not access_policy.do_not_submit,
            routing_hint=ModelRoutingHint.EDITORIAL_ENRICHMENT,
            sensitivity=access_policy.effective_tlp.value,
            web_search=False,
            background=False,
            conversation=None,
            run_id=uuid5(
                NAMESPACE_URL, f"production-editorial-enrichment-revision-model:{request_identity}"
            ),
            allow_failed_resubmit=True,
            metadata={
                "editorial_enrichment_input_hash": request_identity_payload["base_input_hash"],
                "editorial_enrichment_invocation_hash": request_identity,
                "editorial_revision_request_identity": request_identity,
                "editorial_revision_action": action.value,
                "editorial_revision_element_key": element_key,
                "evidence_pack_hash": evidence_pack_hash,
                "model_policy_version": EDITORIAL_ENRICHMENT_MODEL_POLICY_VERSION,
                "revision_request": request_identity_payload,
            },
            parameters={
                "contract_version": EDITORIAL_ENRICHMENT_REVISION_CONTRACT_VERSION,
                "revision": True,
            },
        )

    def _validate_target_proposal(
        self,
        parsed: EditorialEnrichmentWireParseResult,
        *,
        element_kind: EditorialEnrichmentElementKind,
        element_key: str,
        evidence_pack: EditorialEnrichmentEvidencePackV1,
        extraction: Any,
        synthesis: Any,
        figure_catalog: tuple[Any, ...],
        validator_rejections: list[dict[str, str]],
    ) -> EditorialEnrichmentV1:
        proposal = parsed.proposal
        assert proposal is not None
        if proposal.annotations or proposal.resource_needs:
            raise EditorialEnrichmentRevisionValidationError(
                [
                    *validator_rejections,
                    {"block_id": "proposal", "reason_code": "revision_extra_blocks_rejected"},
                ]
            )
        expected_counts = {
            "tables": int(element_kind is EditorialEnrichmentElementKind.TABLE),
            "diagrams": int(element_kind is EditorialEnrichmentElementKind.DIAGRAM),
            "charts": int(element_kind is EditorialEnrichmentElementKind.CHART),
            "figures": int(element_kind is EditorialEnrichmentElementKind.FIGURE),
        }
        actual_counts = {
            "tables": len(proposal.tables),
            "diagrams": len(proposal.diagrams),
            "charts": len(proposal.charts),
            "figures": len(proposal.figures),
        }
        if actual_counts != expected_counts:
            raise EditorialEnrichmentRevisionValidationError(
                [
                    *validator_rejections,
                    {"block_id": "proposal", "reason_code": "revision_target_count_mismatch"},
                ]
            )
        if element_kind is not EditorialEnrichmentElementKind.FIGURE:
            if element_kind is EditorialEnrichmentElementKind.TABLE:
                proposed_key = proposal.tables[0].key
            elif element_kind is EditorialEnrichmentElementKind.DIAGRAM:
                proposed_key = proposal.diagrams[0].key
            else:
                proposed_key = proposal.charts[0].key
            if proposed_key != element_key:
                raise EditorialEnrichmentRevisionValidationError(
                    [
                        *validator_rejections,
                        {"block_id": proposed_key, "reason_code": "revision_stable_key_changed"},
                    ]
                )
        try:
            return validate_editorial_enrichment_proposal(
                proposal,
                evidence_pack,
                extraction,
                synthesis,
                figure_catalog=figure_catalog,
            )
        except (EditorialEnrichmentProposalControlError, ValueError) as exc:
            code = getattr(exc, "code", "editorial_enrichment_revision_validation_failed")
            raise EditorialEnrichmentRevisionValidationError(
                [*validator_rejections, {"block_id": element_key, "reason_code": str(code)}]
            ) from exc

    async def _replace_target(
        self,
        base: EditorialEnrichmentV1,
        candidate: EditorialEnrichmentV1,
        *,
        element_kind: EditorialEnrichmentElementKind,
        element_key: str,
        run_id: UUID,
    ) -> EditorialEnrichmentV1:
        if element_kind is EditorialEnrichmentElementKind.TABLE:
            table_replacement = candidate.tables[0]
            return replace(
                base,
                tables=tuple(
                    table_replacement if item.key == element_key else item for item in base.tables
                ),
            )
        if element_kind is EditorialEnrichmentElementKind.DIAGRAM:
            diagram_replacement = candidate.diagrams[0]
            if (
                self._enrichment._diagram_compiler is None
                or self._enrichment._media_asset_store is None
            ):
                raise ValueError("editorial_enrichment_diagram_compilation_unavailable")
            compilation = await compile_and_store_diagrams(
                (diagram_replacement,),
                compiler=self._enrichment._diagram_compiler,
                media_asset_store=self._enrichment._media_asset_store,
                production_run_id=run_id,
            )
            if compilation.rejections:
                rejection = compilation.rejections[0]
                raise EditorialEnrichmentRevisionValidationError(
                    [
                        {
                            "block_id": rejection.diagram_key,
                            "reason_code": rejection.warning_code,
                            "compiler_error_code": rejection.reason_code,
                        }
                    ]
                )
            diagram_replacement = compilation.diagrams[0]
            return replace(
                base,
                diagrams=tuple(
                    diagram_replacement if item.key == element_key else item
                    for item in base.diagrams
                ),
            )
        if element_kind is EditorialEnrichmentElementKind.CHART:
            chart_replacement = replace(candidate.charts[0], key=element_key)
            return replace(
                base,
                charts=tuple(
                    chart_replacement if item.key == element_key else item for item in base.charts
                ),
            )

        old = next(item for item in base.source_figures if item.key == element_key)
        if not candidate.source_figures:
            raise EditorialEnrichmentRevisionValidationError(
                [
                    {
                        "block_id": element_key,
                        "reason_code": "editorial_enrichment_figure_not_selected",
                    }
                ]
            )
        figure_replacement = replace(candidate.source_figures[0], key=old.key)
        figures = tuple(
            figure_replacement if item.key == element_key else item for item in base.source_figures
        )
        old_id = old.resolved_figure.figure_id if old.resolved_figure is not None else None
        new_id = (
            figure_replacement.resolved_figure.figure_id
            if figure_replacement.resolved_figure is not None
            else None
        )
        new_decision = next(
            (item for item in candidate.figure_decisions if item.figure_id == new_id), None
        )
        if new_id is not None and any(
            item.resolved_figure is not None and item.resolved_figure.figure_id == new_id
            for item in base.source_figures
            if item.key != element_key
        ):
            raise EditorialEnrichmentRevisionValidationError(
                [
                    {
                        "block_id": element_key,
                        "reason_code": "editorial_enrichment_figure_already_selected",
                    }
                ]
            )
        replaced_ids = {figure_id for figure_id in (old_id, new_id) if figure_id is not None}
        decisions = tuple(
            item for item in base.figure_decisions if item.figure_id not in replaced_ids
        ) + ((new_decision,) if new_decision is not None else ())
        return replace(base, source_figures=figures, figure_decisions=decisions)

    async def _insert_target(
        self,
        base: EditorialEnrichmentV1,
        candidate: EditorialEnrichmentV1,
        *,
        element_kind: EditorialEnrichmentElementKind,
        element_key: str,
        run_id: UUID,
    ) -> EditorialEnrichmentV1:
        if element_kind is EditorialEnrichmentElementKind.TABLE:
            table_item = replace(candidate.tables[0], key=element_key)
            return replace(base, tables=(*base.tables, table_item))
        if element_kind is EditorialEnrichmentElementKind.CHART:
            chart_item = replace(candidate.charts[0], key=element_key)
            return replace(base, charts=(*base.charts, chart_item))
        if element_kind is EditorialEnrichmentElementKind.DIAGRAM:
            if (
                self._enrichment._diagram_compiler is None
                or self._enrichment._media_asset_store is None
            ):
                raise ValueError("editorial_enrichment_diagram_compilation_unavailable")
            compilation = await compile_and_store_diagrams(
                (replace(candidate.diagrams[0], key=element_key),),
                compiler=self._enrichment._diagram_compiler,
                media_asset_store=self._enrichment._media_asset_store,
                production_run_id=run_id,
            )
            if compilation.rejections:
                rejection = compilation.rejections[0]
                raise EditorialEnrichmentRevisionValidationError(
                    [
                        {
                            "block_id": rejection.diagram_key,
                            "reason_code": rejection.warning_code,
                            "compiler_error_code": rejection.reason_code,
                        }
                    ]
                )
            diagram_item = compilation.diagrams[0]
            return replace(base, diagrams=(*base.diagrams, diagram_item))
        raise ValueError("editorial_enrichment_source_figure_create_not_supported")

    @staticmethod
    def _remove_target(
        base: EditorialEnrichmentV1,
        *,
        element_kind: EditorialEnrichmentElementKind,
        element_key: str,
    ) -> EditorialEnrichmentV1:
        field_name = {
            EditorialEnrichmentElementKind.TABLE: "tables",
            EditorialEnrichmentElementKind.DIAGRAM: "diagrams",
            EditorialEnrichmentElementKind.CHART: "charts",
            EditorialEnrichmentElementKind.FIGURE: "source_figures",
        }[element_kind]
        remaining = tuple(item for item in getattr(base, field_name) if item.key != element_key)
        anchor_prefix = f"{element_kind.value}:{element_key}:"
        annotations = tuple(
            annotation
            for annotation in base.annotations
            if not annotation.paragraph_anchor.startswith(anchor_prefix)
        )
        if element_kind is EditorialEnrichmentElementKind.TABLE:
            return replace(base, tables=remaining, annotations=annotations)
        if element_kind is EditorialEnrichmentElementKind.DIAGRAM:
            return replace(base, diagrams=remaining, annotations=annotations)
        if element_kind is EditorialEnrichmentElementKind.CHART:
            return replace(base, charts=remaining, annotations=annotations)
        removed = next(item for item in base.source_figures if item.key == element_key)
        removed_figure_id = (
            removed.resolved_figure.figure_id if removed.resolved_figure is not None else None
        )
        decisions = (
            tuple(
                decision
                for decision in base.figure_decisions
                if decision.figure_id != removed_figure_id
            )
            if removed_figure_id is not None
            else base.figure_decisions
        )
        return replace(
            base,
            source_figures=remaining,
            figure_decisions=decisions,
            annotations=annotations,
        )

    @staticmethod
    def _generated_element_key(
        element_kind: EditorialEnrichmentElementKind,
        request_identity: str,
        existing_elements: list[Any],
    ) -> str:
        existing = {value.get("key") for value in existing_elements if isinstance(value, dict)}
        prefix = element_kind.value + "_"
        digest_length = 64 - len(prefix)
        counter = 0
        while True:
            seed = request_identity if counter == 0 else f"{request_identity}:{counter}"
            key = f"{prefix}{hashlib.sha256(seed.encode()).hexdigest()[:digest_length]}"
            if key not in existing:
                return key
            counter += 1

    @staticmethod
    def _render_stable_synthesis(payload: dict[str, Any]) -> str:
        from cti_app.application.production_synthesis_revision import _render_current_synthesis

        return _render_current_synthesis(payload)

    @staticmethod
    def _assert_requested_placement(
        *,
        candidate: EditorialEnrichmentV1,
        element_kind: EditorialEnrichmentElementKind,
        targeted_request: TargetedEditorialRevisionRequest | None,
        synthesis: Any,
    ) -> None:
        if targeted_request is None or targeted_request.target.placement is None:
            raise ValueError("targeted_enrichment_revision_placement_required")
        requested = targeted_request.target.placement
        if requested.kind is TargetedEditorialRevisionPlacementKind.AFTER_LEAD:
            expected = EnrichmentPlacementV1(EnrichmentPlacementKind.AFTER_LEAD)
        elif requested.kind is TargetedEditorialRevisionPlacementKind.AFTER_TIMELINE:
            expected = EnrichmentPlacementV1(EnrichmentPlacementKind.AFTER_TIMELINE)
        else:
            from cti_app.application.production_synthesis_revision import _section_id
            from cti_app.domain.production_synthesis import production_synthesis_to_json

            payload = production_synthesis_to_json(synthesis)
            section_index = next(
                (
                    index
                    for index, section in enumerate(payload["sections"])
                    if _section_id(section) == requested.section_id
                ),
                None,
            )
            if section_index is None:
                raise ValueError("targeted_enrichment_revision_placement_section_missing")
            expected = EnrichmentPlacementV1(
                EnrichmentPlacementKind.AFTER_SECTION, section_index=section_index
            )
        values = {
            EditorialEnrichmentElementKind.TABLE: candidate.tables,
            EditorialEnrichmentElementKind.DIAGRAM: candidate.diagrams,
            EditorialEnrichmentElementKind.CHART: candidate.charts,
            EditorialEnrichmentElementKind.FIGURE: candidate.source_figures,
        }[element_kind]
        if len(values) != 1 or getattr(values[0], "placement", None) != expected:
            raise EditorialEnrichmentRevisionValidationError(
                [
                    {
                        "block_id": "placement",
                        "reason_code": "editorial_enrichment_revision_placement_mismatch",
                    }
                ]
            )

    @staticmethod
    def _targeted_refusal(
        reason: str, restart_stage: ProductionStage | None
    ) -> TargetedSynthesisRevisionResult:
        return TargetedSynthesisRevisionResult(
            artifact=None,
            publication_artifact=None,
            revision=None,
            refusal=TargetedEditorialRevisionRefusal(
                code=TargetedEditorialRevisionRefusalCode.REVISION_NOT_APPLICABLE,
                recommended_restart_stage=restart_stage,
                reason=reason,
            ),
        )

    @staticmethod
    def _provenance_key(element_kind: EditorialEnrichmentElementKind, key: str) -> str:
        return f"{element_kind.value}:{key}"

    async def _element_provenance_after_revision(
        self,
        *,
        base_artifact: ProductionArtifact,
        base_payload: dict[str, Any],
        revised_payload: dict[str, Any],
        element_kind: EditorialEnrichmentElementKind,
        element_key: str,
        revision_id: str,
        result_artifact_id: UUID,
        is_delete: bool,
    ) -> dict[str, Any]:
        async with self._enrichment._uow_factory() as uow:

            async def provenance_for(
                artifact: ProductionArtifact,
                payload: dict[str, Any],
                visited: set[UUID],
            ) -> dict[str, Any]:
                if artifact.id in visited:
                    return {}
                visited.add(artifact.id)
                stored = artifact.metadata.get("element_provenance")
                if isinstance(stored, dict):
                    return {
                        key: dict(value) for key, value in stored.items() if isinstance(value, dict)
                    }
                metadata_revision = artifact.metadata.get("revision")
                inherited: dict[str, Any] = {}
                if isinstance(metadata_revision, dict) and metadata_revision.get(
                    "base_artifact_id"
                ):
                    try:
                        parent = await uow.production_artifacts.get(
                            UUID(str(metadata_revision["base_artifact_id"]))
                        )
                    except (ValueError, TypeError):
                        parent = None
                    if (
                        parent is not None
                        and parent.stage is ProductionArtifactStage.EDITORIAL_ENRICHMENT
                        and parent.canonical_blob_id is not None
                    ):
                        parent_payload = await self._enrichment._artifact_store.read_json(
                            parent.canonical_blob_id
                        )
                        if isinstance(parent_payload, dict):
                            inherited = await provenance_for(parent, parent_payload, visited)
                else:
                    for kind, field_name in (
                        (EditorialEnrichmentElementKind.TABLE, "tables"),
                        (EditorialEnrichmentElementKind.DIAGRAM, "diagrams"),
                        (EditorialEnrichmentElementKind.CHART, "charts"),
                        (EditorialEnrichmentElementKind.FIGURE, "source_figures"),
                    ):
                        for item in payload.get(field_name, []):
                            if isinstance(item, dict) and isinstance(item.get("key"), str):
                                inherited[self._provenance_key(kind, item["key"])] = {
                                    "origin": "initial",
                                    "artifact_id": str(artifact.id),
                                    "revision_id": None,
                                }
                if (
                    isinstance(metadata_revision, dict)
                    and metadata_revision.get("outcome") != "needs_new_evidence"
                ):
                    old_kind = metadata_revision.get("element_kind")
                    old_key = metadata_revision.get("element_key")
                    if isinstance(old_kind, str) and isinstance(old_key, str):
                        try:
                            legacy_kind: EditorialEnrichmentElementKind | None = (
                                EditorialEnrichmentElementKind(old_kind)
                            )
                        except ValueError:
                            legacy_kind = None
                        if legacy_kind is not None:
                            field_name = {
                                EditorialEnrichmentElementKind.TABLE: "tables",
                                EditorialEnrichmentElementKind.DIAGRAM: "diagrams",
                                EditorialEnrichmentElementKind.CHART: "charts",
                                EditorialEnrichmentElementKind.FIGURE: "source_figures",
                            }[legacy_kind]
                            present = any(
                                isinstance(item, dict) and item.get("key") == old_key
                                for item in payload.get(field_name, [])
                            )
                            if present:
                                inherited[self._provenance_key(legacy_kind, old_key)] = {
                                    "origin": "revision",
                                    "artifact_id": str(artifact.id),
                                    "revision_id": metadata_revision.get(
                                        "revision_id", metadata_revision.get("request_identity")
                                    ),
                                }
                current_keys = {
                    self._provenance_key(kind, item["key"])
                    for kind, field_name in (
                        (EditorialEnrichmentElementKind.TABLE, "tables"),
                        (EditorialEnrichmentElementKind.DIAGRAM, "diagrams"),
                        (EditorialEnrichmentElementKind.CHART, "charts"),
                        (EditorialEnrichmentElementKind.FIGURE, "source_figures"),
                    )
                    for item in payload.get(field_name, [])
                    if isinstance(item, dict) and isinstance(item.get("key"), str)
                }
                return {key: value for key, value in inherited.items() if key in current_keys}

            provenance = await provenance_for(base_artifact, base_payload, set())
        result_keys = {
            self._provenance_key(kind, item["key"])
            for kind, field_name in (
                (EditorialEnrichmentElementKind.TABLE, "tables"),
                (EditorialEnrichmentElementKind.DIAGRAM, "diagrams"),
                (EditorialEnrichmentElementKind.CHART, "charts"),
                (EditorialEnrichmentElementKind.FIGURE, "source_figures"),
            )
            for item in revised_payload.get(field_name, [])
            if isinstance(item, dict) and isinstance(item.get("key"), str)
        }
        provenance = {key: value for key, value in provenance.items() if key in result_keys}
        if not is_delete:
            provenance[self._provenance_key(element_kind, element_key)] = {
                "origin": "revision",
                "artifact_id": str(result_artifact_id),
                "revision_id": revision_id,
            }
        for kind, field_name in (
            (EditorialEnrichmentElementKind.TABLE, "tables"),
            (EditorialEnrichmentElementKind.DIAGRAM, "diagrams"),
            (EditorialEnrichmentElementKind.CHART, "charts"),
            (EditorialEnrichmentElementKind.FIGURE, "source_figures"),
        ):
            for item in revised_payload.get(field_name, []):
                if isinstance(item, dict) and isinstance(item.get("key"), str):
                    key = self._provenance_key(kind, item["key"])
                    provenance.setdefault(
                        key,
                        {
                            "origin": "initial",
                            "artifact_id": str(base_artifact.id),
                            "revision_id": None,
                        },
                    )
        return provenance

    async def lineage_targeted(self, artifact_id: UUID) -> dict[str, Any]:
        async with self._enrichment._uow_factory() as uow:
            artifact = await uow.production_artifacts.get(artifact_id)
            if artifact is None:
                raise ValueError("targeted_synthesis_revision_artifact_not_found")
            if artifact.stage is not ProductionArtifactStage.EDITORIAL_ENRICHMENT:
                raise ValueError("targeted_synthesis_revision_artifact_not_enrichment")
            chain: list[dict[str, Any]] = []
            cursor: ProductionArtifact | None = artifact
            visited: set[UUID] = set()
            while cursor is not None and cursor.id not in visited:
                visited.add(cursor.id)
                revision = cursor.metadata.get("revision")
                if not isinstance(revision, dict):
                    break
                chain.append(dict(revision))
                try:
                    cursor = await uow.production_artifacts.get(
                        UUID(str(revision.get("base_artifact_id")))
                    )
                except (ValueError, TypeError, AttributeError):
                    break
                if (
                    cursor is None
                    or cursor.stage is not ProductionArtifactStage.EDITORIAL_ENRICHMENT
                ):
                    break
            return {
                "artifact_id": str(artifact_id),
                "production_run_id": str(artifact.production_run_id),
                "revisions": chain,
                "paragraph_provenance": {},
                "element_provenance": artifact.metadata.get("element_provenance", {}),
            }

    async def _assemble_and_render(
        self,
        *,
        run: ProductionRun,
        snapshot: ProductionInputSnapshot,
        references_artifact: ProductionArtifact,
        extraction_artifact: ProductionArtifact,
        projection_artifact: ProductionArtifact | None,
        synthesis_artifact: ProductionArtifact,
        enrichment_artifact: ProductionArtifact,
        references: Any,
        extraction: Any,
        projection: Any,
        synthesis: Any,
        enrichment: EditorialEnrichmentV1,
    ) -> ProductionArtifact:
        from cti_app.application.publication_assembly import PublicationAssemblyService

        async with self._enrichment._uow_factory() as uow:
            publication = await PublicationAssemblyService(
                self._enrichment._artifact_store, uow.production_artifacts
            ).assemble_publication(
                run=run,
                snapshot=snapshot,
                references=references,
                extraction=extraction,
                relevance_projection=projection,
                synthesis=synthesis,
                editorial_enrichment=enrichment,
                metadata_extra={
                    "input_artifacts": {
                        "references_artifact_id": str(references_artifact.id),
                        "extraction_artifact_id": str(extraction_artifact.id),
                        "relevance_projection_artifact_id": (
                            str(projection_artifact.id) if projection_artifact else None
                        ),
                        "synthesis_artifact_id": str(synthesis_artifact.id),
                        "editorial_enrichment_artifact_id": str(enrichment_artifact.id),
                    },
                    "editorial_revision_artifact_id": str(enrichment_artifact.id),
                },
            )
            await uow.commit()
        await self._publication_render_service.render_preview(publication.id)
        return publication

    @staticmethod
    def _publication_for_revision(
        artifacts: Sequence[ProductionArtifact],
        enrichment_artifact_id: UUID,
    ) -> ProductionArtifact | None:
        for item in artifacts:
            inputs = item.metadata.get("input_artifacts")
            if (
                item.stage is ProductionArtifactStage.PUBLICATION
                and isinstance(inputs, dict)
                and inputs.get("editorial_enrichment_artifact_id") == str(enrichment_artifact_id)
            ):
                return item
        return None

    @staticmethod
    def _next_resource_need_key(enrichment: EditorialEnrichmentV1) -> str:
        numbers = [
            int(item.key[1:]) for item in enrichment.resource_needs if item.key[1:].isdigit()
        ]
        return f"N{max(numbers, default=0) + 1:03d}"

    @staticmethod
    def _optional_uuid(value: Any) -> UUID | None:
        if value is None:
            return None
        return UUID(str(value))
