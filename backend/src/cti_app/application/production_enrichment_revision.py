"""Versioned, single-element Editorial Enrichment revisions."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from cti_app.application.media_assets import compile_and_store_diagrams
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.application.production_editorial_enrichment import (
    EDITORIAL_ENRICHMENT_MODEL_POLICY_VERSION,
    EditorialEnrichmentEvidencePackV1,
    EditorialEnrichmentProposalControlError,
    EditorialEnrichmentRevisionConflictError,
    EditorialEnrichmentWireParseResult,
    ProductionEditorialEnrichmentService,
    build_editorial_enrichment_evidence_pack,
    build_editorial_figure_catalog,
    compute_editorial_enrichment_input_hash,
    editorial_enrichment_evidence_pack_hash,
    editorial_enrichment_output_contract_example,
    parse_editorial_enrichment_proposal_wire,
    validate_editorial_enrichment,
    validate_editorial_enrichment_proposal,
)
from cti_app.application.production_prompts import (
    EDITORIAL_ENRICHMENT_REVISION_CONTRACT_VERSION,
    EDITORIAL_ENRICHMENT_REVISION_PROMPT_VERSION,
)
from cti_app.application.production_references import production_reference_corpus_from_json
from cti_app.application.production_stages import EditorialEnrichmentService
from cti_app.application.production_synthesis import (
    build_synthesis_access_policy,
    synthesis_access_policy_hash,
)
from cti_app.application.publication_rendering import PublicationRenderService
from cti_app.application.source_figure_inventory import load_archived_source_figure_inventory
from cti_app.domain.production import (
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionArtifactStatus,
    ProductionInputSnapshot,
    ProductionRun,
)
from cti_app.domain.production_editorial_enrichment import (
    EDITORIAL_RESOURCE_PROPOSAL_POLICY_VERSION,
    EditorialEnrichmentElementKind,
    EditorialEnrichmentRevisionAction,
    EditorialEnrichmentRevisionOutcome,
    EditorialEnrichmentV1,
    EditorialResourceNeedV1,
    editorial_enrichment_from_json,
    editorial_enrichment_to_json,
)
from cti_app.domain.production_synthesis import ExtractionEvidenceRefV1

MAX_EDITORIAL_REVISION_INSTRUCTION_LENGTH = 2000


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

    async def revise(
        self,
        *,
        subject_id: UUID,
        base_artifact_id: UUID,
        base_version: int,
        base_input_hash: str,
        base_canonical_sha256: str,
        element_kind: EditorialEnrichmentElementKind,
        element_key: str,
        action: EditorialEnrichmentRevisionAction,
        instruction: str,
    ) -> EditorialEnrichmentRevisionResult:
        normalized_instruction = instruction.strip()
        if (
            not normalized_instruction
            or len(normalized_instruction) > MAX_EDITORIAL_REVISION_INSTRUCTION_LENGTH
        ):
            raise ValueError("editorial_enrichment_revision_instruction_invalid")
        if not element_key.strip() or len(element_key) > 128:
            raise ValueError("editorial_enrichment_revision_element_key_invalid")
        self._validate_action(element_kind, action)

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

        async with self._enrichment._uow_factory() as uow:
            access_policy = await build_synthesis_access_policy(
                snapshot,
                extraction,
                uow.source_documents,
                uow.source_collections,
            )
            inventory = await load_archived_source_figure_inventory(
                subject_id=subject_id,
                extraction_sources=extraction.sources,
                source_document_repository=uow.source_documents,
                blob_repository=uow.blobs,
                artifact_store=self._enrichment._artifact_store,
                media_archiver=self._enrichment._source_media_archiver,
            )
        if access_policy.do_not_submit or not access_policy.external_llm_allowed:
            raise ValueError("editorial_enrichment_revision_policy_blocked")
        if synthesis_access_policy_hash(access_policy) != base_artifact.metadata.get(
            "access_policy_hash"
        ):
            raise EditorialEnrichmentRevisionConflictError(
                "The access policy no longer matches the base artifact"
            )

        figure_catalog = build_editorial_figure_catalog(extraction, inventory)
        evidence_pack = build_editorial_enrichment_evidence_pack(
            snapshot, extraction, synthesis, projection
        )
        evidence_pack_hash = editorial_enrichment_evidence_pack_hash(evidence_pack)
        stored_evidence_hash = base_artifact.metadata.get("evidence_pack_hash")
        if stored_evidence_hash != evidence_pack_hash:
            raise EditorialEnrichmentRevisionConflictError(
                "The admitted evidence pack no longer matches the base artifact"
            )
        inventory_hash = inventory.functional_hash()
        stored_inventory_hash = base_artifact.metadata.get("source_figure_inventory_hash")
        if stored_inventory_hash is not None and stored_inventory_hash != inventory_hash:
            raise EditorialEnrichmentRevisionConflictError(
                "The admitted figure catalog no longer matches the base artifact"
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
            raise EditorialEnrichmentRevisionConflictError(
                "The Editorial Enrichment inputs no longer match the base artifact"
            )

        list_name = {
            EditorialEnrichmentElementKind.TABLE: "tables",
            EditorialEnrichmentElementKind.DIAGRAM: "diagrams",
            EditorialEnrichmentElementKind.FIGURE: "source_figures",
        }[element_kind]
        base_elements = base_payload.get(list_name)
        if not isinstance(base_elements, list):
            raise ValueError("editorial_enrichment_revision_base_element_missing")
        element_index = next(
            (
                index
                for index, value in enumerate(base_elements)
                if isinstance(value, dict) and value.get("key") == element_key
            ),
            None,
        )
        if element_index is None:
            raise ValueError("editorial_enrichment_revision_base_element_missing")
        base_element = base_elements[element_index]
        element_evidence_handles = self._element_evidence_handles(
            element_kind, base_enrichment, element_key, evidence_pack
        )
        admitted_handles = sorted(evidence_pack._handle_to_ref)
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
        )
        existing, archive_error = await self._enrichment._verified_existing_execution(request)
        if archive_error is not None:
            raise EditorialEnrichmentRevisionValidationError(
                [{"block_id": "response", "reason_code": str(archive_error.get("wire_error_code"))}]
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
            validator_rejections.append({"block_id": "proposal", "reason_code": parsed.error_code})
        if parsed.proposal is None:
            raise EditorialEnrichmentRevisionValidationError(validator_rejections)

        outcome = EditorialEnrichmentRevisionOutcome.REVISED
        resource_need: dict[str, Any] | None = None
        if parsed.proposal.resource_needs:
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
        else:
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
            if (
                element_kind is EditorialEnrichmentElementKind.FIGURE
                and action is EditorialEnrichmentRevisionAction.CHOOSE_ANOTHER_FIGURE
            ):
                previous_figure = next(
                    item for item in base_enrichment.source_figures if item.key == element_key
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
            validate_editorial_enrichment(
                revised_enrichment, extraction=extraction, synthesis=synthesis
            )
            after_payload = editorial_enrichment_to_json(revised_enrichment)
            after_element = after_payload[list_name][element_index]

        revised_payload = editorial_enrichment_to_json(revised_enrichment)
        # Preserve every untouched serialized element exactly as it appeared in
        # the base canonical payload. Only the requested block and L7b needs
        # collection are replaced.
        final_payload = dict(base_payload)
        final_payload[list_name] = list(base_elements)
        if outcome is EditorialEnrichmentRevisionOutcome.REVISED:
            final_payload[list_name][element_index] = after_element
        if "resource_needs" in revised_payload:
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
        artifact = await self._persistence.store_editorial_enrichment_result(
            run_id=run.id,
            subject_id=subject_id,
            input_hash=input_hash,
            enrichment=revised_enrichment,
            extraction=extraction,
            synthesis=synthesis,
            raw_result=raw_text,
            model_run_id=execution.run.id,
            evidence_pack_hash=evidence_pack_hash,
            access_policy_hash=base_artifact.metadata["access_policy_hash"],
            model_policy_version=EDITORIAL_ENRICHMENT_MODEL_POLICY_VERSION,
            routing_policy_version=str(base_artifact.metadata.get("routing_policy_version", "")),
            projection_hash=projection.projection_hash if projection is not None else None,
            source_figure_inventory_hash=inventory_hash,
            metadata_extra={"revision": revision_data},
            artifact_id=revision_artifact_id,
            expected_current_artifact_id=base_artifact.id,
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
            EditorialEnrichmentRevisionAction.IMPROVE_TABLE: {EditorialEnrichmentElementKind.TABLE},
            EditorialEnrichmentRevisionAction.DETAIL_DIAGRAM: {
                EditorialEnrichmentElementKind.DIAGRAM
            },
            EditorialEnrichmentRevisionAction.CHANGE_CAPTION_PLACEMENT: {
                EditorialEnrichmentElementKind.TABLE,
                EditorialEnrichmentElementKind.DIAGRAM,
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
        base_element: dict[str, Any],
        element_evidence_handles: list[str],
        evidence_pack: EditorialEnrichmentEvidencePackV1,
        evidence_pack_hash: str,
        figure_catalog: tuple[Any, ...],
        access_policy: Any,
    ) -> Any:
        from cti_app.application.model_gateway import ModelRequest, ModelRoutingHint

        target_base_element = base_element
        if element_kind is EditorialEnrichmentElementKind.FIGURE:
            resolved = base_element.get("resolved_figure")
            resolved_id = resolved.get("figure_id") if isinstance(resolved, dict) else None
            catalog_record: dict[str, Any] = next(
                (
                    entry.prompt_record()
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
                "Révise uniquement l'élément ciblé. Utilise exclusivement les handles du "
                "paquet de preuves admis. Conserve les littéraux exactement. Ne fais aucune "
                "recherche, aucun appel web, n'invente aucun fait et ne modifie pas la "
                "narration. Si l'instruction exige une information absente des preuves, "
                "émets uniquement un bloc NEEDS avec un motif et un query_hint, sans proposer "
                "l'élément révisé. Retourne un seul bloc TABLE, DIAGRAM ou FIGURE selon le "
                "type ciblé, en conservant sa clé pour TABLE/DIAGRAM. Respecte strictement "
                "les handles et règles de validation fournis."
            ),
            "action": action.value,
            "instruction": instruction,
            "target": {
                "kind": element_kind.value,
                "key": element_key,
                "base_element": target_base_element,
                "base_element_evidence_handles": element_evidence_handles,
            },
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
            "figure_catalog": [value.prompt_record() for value in figure_catalog],
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
            "figures": int(element_kind is EditorialEnrichmentElementKind.FIGURE),
        }
        actual_counts = {
            "tables": len(proposal.tables),
            "diagrams": len(proposal.diagrams),
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
            proposed_key = (
                proposal.tables[0].key
                if element_kind is EditorialEnrichmentElementKind.TABLE
                else proposal.diagrams[0].key
            )
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
        decisions = tuple(item for item in base.figure_decisions if item.figure_id != old_id) + (
            (new_decision,) if new_decision is not None else ()
        )
        return replace(base, source_figures=figures, figure_decisions=decisions)

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
