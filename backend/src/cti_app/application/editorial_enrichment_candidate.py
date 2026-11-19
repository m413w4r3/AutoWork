"""Deterministic enrichment candidate assembly and caller-owned persistence."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any, Protocol
from uuid import UUID

from cti_app.application.editorial_enrichment_revision import (
    EditorialEnrichmentRevisionValidationError,
    assert_requested_placement,
)
from cti_app.application.persistence import ProductionUnitOfWork
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.application.production_editorial_enrichment import (
    EditorialEnrichmentRevisionConflictError,
    ResourceNeedProposalV1,
    semantic_annotation_anchor_texts,
    validate_editorial_enrichment,
)
from cti_app.application.production_stages import EditorialEnrichmentService
from cti_app.domain.production import ProductionArtifact, ProductionArtifactStage
from cti_app.domain.production_editorial_enrichment import (
    EDITORIAL_RESOURCE_PROPOSAL_POLICY_VERSION,
    DiagramSpecV1,
    EditorialEnrichmentElementKind,
    EditorialEnrichmentRevisionAction,
    EditorialEnrichmentRevisionOutcome,
    EditorialEnrichmentV1,
    EditorialResourceNeedV1,
    editorial_enrichment_from_json,
    editorial_enrichment_to_json,
)
from cti_app.domain.production_editorial_revision import TargetedEditorialRevisionRequest
from cti_app.domain.production_extraction import ProductionExtractionV1
from cti_app.domain.production_synthesis import ProductionSynthesisV1


class CandidatePayloadReader(Protocol):
    async def read_bytes(self, blob_id: UUID, *, max_bytes: int | None = None) -> bytes: ...


class DiagramMaterializer(Protocol):
    async def __call__(self, diagram: DiagramSpecV1) -> DiagramSpecV1: ...


@dataclass(frozen=True, slots=True)
class EnrichmentCandidateBlobReference:
    """Reference to canonical bytes uploaded and verified before the UOW."""

    blob_id: UUID
    sha256: str
    size: int


@dataclass(frozen=True, slots=True)
class EditorialEnrichmentCandidate:
    base_artifact: ProductionArtifact
    artifact_id: UUID
    input_hash: str
    base_canonical_sha256: str
    enrichment: EditorialEnrichmentV1
    extraction: ProductionExtractionV1
    synthesis: ProductionSynthesisV1
    canonical_payload: dict[str, Any]
    canonical_bytes: bytes
    canonical_sha256: str
    after_element: dict[str, Any] | None
    outcome: EditorialEnrichmentRevisionOutcome
    resource_need: dict[str, Any] | None
    diagram_materialization: DiagramSpecV1 | None = None
    candidate_blob: EnrichmentCandidateBlobReference | None = None


def _field_name(element_kind: EditorialEnrichmentElementKind) -> str:
    return {
        EditorialEnrichmentElementKind.TABLE: "tables",
        EditorialEnrichmentElementKind.DIAGRAM: "diagrams",
        EditorialEnrichmentElementKind.CHART: "charts",
        EditorialEnrichmentElementKind.FIGURE: "source_figures",
    }[element_kind]


def replace_target(
    base: EditorialEnrichmentV1,
    candidate: EditorialEnrichmentV1,
    *,
    element_kind: EditorialEnrichmentElementKind,
    element_key: str,
) -> EditorialEnrichmentV1:
    """Apply one validated replacement without writing any assets."""
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
        return replace(
            base,
            diagrams=tuple(
                diagram_replacement if item.key == element_key else item for item in base.diagrams
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
            [{"block_id": element_key, "reason_code": "editorial_enrichment_figure_not_selected"}]
        )
    figure_replacement = replace(candidate.source_figures[0], key=old.key)
    new_id = (
        figure_replacement.resolved_figure.figure_id
        if figure_replacement.resolved_figure is not None
        else None
    )
    old_id = old.resolved_figure.figure_id if old.resolved_figure is not None else None
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
    new_decision = next(
        (item for item in candidate.figure_decisions if item.figure_id == new_id), None
    )
    replaced_ids = {figure_id for figure_id in (old_id, new_id) if figure_id is not None}
    decisions = tuple(
        item for item in base.figure_decisions if item.figure_id not in replaced_ids
    ) + ((new_decision,) if new_decision is not None else ())
    return replace(
        base,
        source_figures=tuple(
            figure_replacement if item.key == element_key else item for item in base.source_figures
        ),
        figure_decisions=decisions,
    )


def insert_target(
    base: EditorialEnrichmentV1,
    candidate: EditorialEnrichmentV1,
    *,
    element_kind: EditorialEnrichmentElementKind,
    element_key: str,
) -> EditorialEnrichmentV1:
    """Append one validated element, retaining the legacy element-key rules."""
    if element_kind is EditorialEnrichmentElementKind.TABLE:
        return replace(base, tables=(*base.tables, replace(candidate.tables[0], key=element_key)))
    if element_kind is EditorialEnrichmentElementKind.CHART:
        return replace(base, charts=(*base.charts, replace(candidate.charts[0], key=element_key)))
    if element_kind is EditorialEnrichmentElementKind.DIAGRAM:
        diagram = replace(candidate.diagrams[0], key=element_key)
        return replace(base, diagrams=(*base.diagrams, diagram))
    raise ValueError("editorial_enrichment_source_figure_create_not_supported")


def remove_target(
    base: EditorialEnrichmentV1,
    *,
    element_kind: EditorialEnrichmentElementKind,
    element_key: str,
) -> EditorialEnrichmentV1:
    field_name = _field_name(element_kind)
    remaining = tuple(item for item in getattr(base, field_name) if item.key != element_key)
    anchor_prefix = f"{element_kind.value}:{element_key}:"
    annotations = tuple(
        item for item in base.annotations if not item.paragraph_anchor.startswith(anchor_prefix)
    )
    if element_kind is EditorialEnrichmentElementKind.TABLE:
        return replace(base, tables=remaining, annotations=annotations)
    if element_kind is EditorialEnrichmentElementKind.DIAGRAM:
        return replace(base, diagrams=remaining, annotations=annotations)
    if element_kind is EditorialEnrichmentElementKind.CHART:
        return replace(base, charts=remaining, annotations=annotations)
    removed = next(item for item in base.source_figures if item.key == element_key)
    removed_id = removed.resolved_figure.figure_id if removed.resolved_figure is not None else None
    decisions = (
        tuple(item for item in base.figure_decisions if item.figure_id != removed_id)
        if removed_id is not None
        else base.figure_decisions
    )
    return replace(
        base,
        source_figures=remaining,
        figure_decisions=decisions,
        annotations=annotations,
    )


def _next_resource_need_key(enrichment: EditorialEnrichmentV1) -> str:
    numbers = [int(item.key[1:]) for item in enrichment.resource_needs if item.key[1:].isdigit()]
    return f"N{max(numbers, default=0) + 1:03d}"


async def build_candidate_from_validated_patch(
    *,
    base_artifact: ProductionArtifact,
    base_payload: dict[str, Any],
    base_canonical_bytes: bytes,
    base_canonical_sha256: str,
    base_enrichment: EditorialEnrichmentV1,
    validated_patch: EditorialEnrichmentV1 | None,
    extraction: ProductionExtractionV1,
    synthesis: ProductionSynthesisV1,
    element_kind: EditorialEnrichmentElementKind,
    element_key: str,
    action: EditorialEnrichmentRevisionAction,
    input_hash: str,
    artifact_id: UUID,
    targeted_request: TargetedEditorialRevisionRequest | None = None,
    is_create: bool = False,
    is_delete: bool = False,
    resource_need_proposal: ResourceNeedProposalV1 | None = None,
) -> EditorialEnrichmentCandidate:
    """Prepare canonical enrichment bytes from a validated patch without external effects.

    Call :func:`materialize_candidate_diagrams` explicitly before persistence when
    ``diagram_materialization`` is set. This keeps candidate preparation free of
    media writes so future consumers can compose validation, materialization and
    persistence as separate steps. Adoption contract: check the base artifact ID,
    input hash and canonical hash before any materializer that writes media, then
    persist through :func:`persist_candidate`, which rechecks the same fence in the
    caller's UOW before storing payloads. The caller owns commit.
    """
    if base_artifact.canonical_blob_id is None:
        raise EditorialEnrichmentRevisionConflictError(
            "The requested base artifact has no canonical payload"
        )
    if hashlib.sha256(base_canonical_bytes).hexdigest() != base_canonical_sha256:
        raise EditorialEnrichmentRevisionConflictError(
            "The requested base artifact content hash changed"
        )
    if json.loads(base_canonical_bytes) != base_payload:
        raise ValueError("editorial_enrichment_revision_base_payload_mismatch")
    if base_artifact.stage is not ProductionArtifactStage.EDITORIAL_ENRICHMENT:
        raise ValueError("editorial_enrichment_revision_base_stage_invalid")

    list_name = _field_name(element_kind)
    base_elements = base_payload.get(list_name)
    if not isinstance(base_elements, list):
        raise ValueError("editorial_enrichment_revision_base_element_missing")
    element_index = next(
        (
            index
            for index, item in enumerate(base_elements)
            if isinstance(item, dict) and item.get("key") == element_key
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

    outcome = (
        EditorialEnrichmentRevisionOutcome.NEEDS_NEW_EVIDENCE
        if resource_need_proposal is not None
        else EditorialEnrichmentRevisionOutcome.REVISED
    )
    resource_need: dict[str, Any] | None = None
    diagram_materialization: DiagramSpecV1 | None = None
    if resource_need_proposal is not None:
        canonical_need = EditorialResourceNeedV1(
            key=_next_resource_need_key(base_enrichment),
            kind=resource_need_proposal.kind,
            reason=resource_need_proposal.reason,
            query_hint=resource_need_proposal.query_hint,
            policy_version=EDITORIAL_RESOURCE_PROPOSAL_POLICY_VERSION,
        )
        revised_enrichment = (
            replace(
                base_enrichment,
                resource_needs=(*base_enrichment.resource_needs, canonical_need),
            )
            if base_enrichment.schema_version >= 3
            else base_enrichment
        )
        resource_need = {
            "key": canonical_need.key,
            "kind": canonical_need.kind.value,
            "reason": canonical_need.reason,
            "query_hint": canonical_need.query_hint,
        }
        after_element = base_element
    elif is_delete or action is EditorialEnrichmentRevisionAction.REMOVE_ELEMENT:
        revised_enrichment = remove_target(
            base_enrichment, element_kind=element_kind, element_key=element_key
        )
        after_element = None
    else:
        if validated_patch is None:
            raise ValueError("editorial_enrichment_revision_validated_patch_missing")
        if is_create:
            if targeted_request is None:
                raise ValueError("targeted_enrichment_revision_placement_required")
            assert_requested_placement(
                candidate=validated_patch,
                element_kind=element_kind,
                targeted_request=targeted_request,
                synthesis=synthesis,
            )
            revised_enrichment = insert_target(
                base_enrichment,
                validated_patch,
                element_kind=element_kind,
                element_key=element_key,
            )
        else:
            if (
                element_kind is EditorialEnrichmentElementKind.FIGURE
                and action is EditorialEnrichmentRevisionAction.CHOOSE_ANOTHER_FIGURE
            ):
                previous = next(
                    item for item in base_enrichment.source_figures if item.key == element_key
                )
                replacement = validated_patch.source_figures[0]
                previous_id = (
                    previous.resolved_figure.figure_id
                    if previous.resolved_figure is not None
                    else None
                )
                replacement_id = (
                    replacement.resolved_figure.figure_id
                    if replacement.resolved_figure is not None
                    else None
                )
                if previous_id == replacement_id:
                    raise EditorialEnrichmentRevisionValidationError(
                        [
                            {
                                "block_id": element_key,
                                "reason_code": "editorial_enrichment_figure_not_changed",
                            }
                        ]
                    )
            revised_enrichment = replace_target(
                base_enrichment,
                validated_patch,
                element_kind=element_kind,
                element_key=element_key,
            )
            anchor_texts = semantic_annotation_anchor_texts(synthesis, revised_enrichment)
            retained_annotations = tuple(
                annotation
                for annotation in revised_enrichment.annotations
                if (anchor_text := anchor_texts.get(annotation.paragraph_anchor)) is not None
                and annotation.text in anchor_text
            )
            if retained_annotations != revised_enrichment.annotations:
                revised_enrichment = replace(revised_enrichment, annotations=retained_annotations)

        after_payload = editorial_enrichment_to_json(revised_enrichment)
        after_element = (
            after_payload[list_name][-1] if is_create else after_payload[list_name][element_index]
        )
        if element_kind is EditorialEnrichmentElementKind.DIAGRAM:
            diagram_materialization = next(
                item for item in revised_enrichment.diagrams if item.key == element_key
            )

    validate_editorial_enrichment(revised_enrichment, extraction=extraction, synthesis=synthesis)
    revised_payload = editorial_enrichment_to_json(revised_enrichment)
    final_payload = dict(base_payload)
    final_payload[list_name] = list(base_elements)
    if outcome is EditorialEnrichmentRevisionOutcome.NEEDS_NEW_EVIDENCE:
        pass
    elif is_delete or action is EditorialEnrichmentRevisionAction.REMOVE_ELEMENT:
        assert element_index is not None
        final_payload[list_name].pop(element_index)
    elif is_create:
        final_payload[list_name].append(after_element)
    elif outcome is EditorialEnrichmentRevisionOutcome.REVISED:
        assert element_index is not None
        final_payload[list_name][element_index] = after_element
    if element_kind is EditorialEnrichmentElementKind.FIGURE:
        final_payload["figure_decisions"] = revised_payload["figure_decisions"]
    if revised_payload.get("annotations") != base_payload.get("annotations", []):
        final_payload["annotations"] = revised_payload["annotations"]
    if resource_need_proposal is not None and "resource_needs" in revised_payload:
        final_payload["resource_needs"] = revised_payload["resource_needs"]

    if editorial_enrichment_from_json(final_payload) != revised_enrichment:
        raise ValueError("editorial_enrichment_revision_payload_mismatch")
    canonical_bytes = ProductionArtifactStore.canonical_json_bytes(final_payload)
    canonical_sha256 = hashlib.sha256(canonical_bytes).hexdigest()
    return EditorialEnrichmentCandidate(
        base_artifact=base_artifact,
        artifact_id=artifact_id,
        input_hash=input_hash,
        base_canonical_sha256=base_canonical_sha256,
        enrichment=revised_enrichment,
        extraction=extraction,
        synthesis=synthesis,
        canonical_payload=final_payload,
        canonical_bytes=canonical_bytes,
        canonical_sha256=canonical_sha256,
        after_element=after_element,
        outcome=outcome,
        resource_need=resource_need,
        diagram_materialization=diagram_materialization,
    )


async def materialize_candidate_diagrams(
    candidate: EditorialEnrichmentCandidate,
    *,
    materializer: DiagramMaterializer,
) -> EditorialEnrichmentCandidate:
    """Materialize a prepared diagram and refresh its canonical candidate bytes.

    Call only after a read-only base fence check when the materializer writes media.
    Persistence still rechecks the fence inside the caller-owned UOW.
    """
    requested = candidate.diagram_materialization
    if requested is None:
        return candidate
    materialized = await materializer(requested)
    if materialized.key != requested.key:
        raise ValueError("editorial_enrichment_diagram_materializer_key_mismatch")
    diagrams = tuple(
        materialized if diagram.key == requested.key else diagram
        for diagram in candidate.enrichment.diagrams
    )
    if all(diagram.key != requested.key for diagram in candidate.enrichment.diagrams):
        raise ValueError("editorial_enrichment_diagram_materializer_target_missing")
    enrichment = replace(candidate.enrichment, diagrams=diagrams)
    validate_editorial_enrichment(
        enrichment, extraction=candidate.extraction, synthesis=candidate.synthesis
    )
    revised_payload = editorial_enrichment_to_json(enrichment)
    payload = dict(candidate.canonical_payload)
    diagram_payloads = list(payload["diagrams"])
    materialized_payload = next(
        item for item in revised_payload["diagrams"] if item["key"] == requested.key
    )
    payload_index = next(
        (index for index, item in enumerate(diagram_payloads) if item.get("key") == requested.key),
        None,
    )
    if payload_index is None:
        raise ValueError("editorial_enrichment_diagram_materializer_target_missing")
    diagram_payloads[payload_index] = materialized_payload
    payload["diagrams"] = diagram_payloads
    if editorial_enrichment_from_json(payload) != enrichment:
        raise ValueError("editorial_enrichment_revision_payload_mismatch")
    encoded = ProductionArtifactStore.canonical_json_bytes(payload)
    return replace(
        candidate,
        enrichment=enrichment,
        canonical_payload=payload,
        canonical_bytes=encoded,
        canonical_sha256=hashlib.sha256(encoded).hexdigest(),
        after_element=materialized_payload,
        diagram_materialization=None,
    )


async def persist_candidate(
    uow: ProductionUnitOfWork,
    *,
    artifact_store: CandidatePayloadReader,
    persistence_service: EditorialEnrichmentService,
    candidate: EditorialEnrichmentCandidate,
    raw_result: str | None,
    model_run_id: UUID | None,
    evidence_pack_hash: str,
    access_policy_hash: str,
    model_policy_version: str,
    routing_policy_version: str,
    projection_hash: str | None = None,
    source_figure_inventory_hash: str | None = None,
    metadata_extra: Mapping[str, Any] | None = None,
    resource_search_enabled: bool = False,
    candidate_blob: EnrichmentCandidateBlobReference | None = None,
    invalidate_downstream: bool = True,
) -> ProductionArtifact:
    """Append the candidate using the caller's UOW; this function never commits it."""
    if candidate.diagram_materialization is not None:
        raise ValueError("editorial_enrichment_candidate_diagram_not_materialized")
    if (
        ProductionArtifactStore.canonical_json_bytes(candidate.canonical_payload)
        != (candidate.canonical_bytes)
        or hashlib.sha256(candidate.canonical_bytes).hexdigest() != candidate.canonical_sha256
    ):
        raise ValueError("editorial_enrichment_revision_candidate_hash_mismatch")
    candidate_blob = candidate_blob or candidate.candidate_blob
    preuploaded_payload_ids: tuple[UUID | None, UUID] | None = None
    preuploaded_payload_sha256: str | None = None
    if candidate_blob is not None:
        if candidate_blob.sha256 != candidate.canonical_sha256 or candidate_blob.size != len(
            candidate.canonical_bytes
        ):
            raise ValueError("editorial_enrichment_candidate_blob_reference_mismatch")
        preuploaded_payload_ids = (None, candidate_blob.blob_id)
        preuploaded_payload_sha256 = candidate_blob.sha256
    prior = await uow.production_artifacts.get(candidate.artifact_id)
    if prior is not None:
        if prior.subject_id != candidate.base_artifact.subject_id:
            raise EditorialEnrichmentRevisionConflictError(
                "Revision identity belongs to another subject"
            )
        if (
            prior.production_run_id != candidate.base_artifact.production_run_id
            or prior.stage is not ProductionArtifactStage.EDITORIAL_ENRICHMENT
            or prior.input_hash != candidate.input_hash
        ):
            raise ValueError("editorial_enrichment_revision_result_corrupt")
        if prior.canonical_blob_id is None:
            raise ValueError("editorial_enrichment_revision_result_corrupt")
        if candidate_blob is not None:
            # Adoption has already uploaded and read-back verified these exact
            # content-addressed bytes before opening its relational UOW.
            if prior.canonical_blob_id != candidate_blob.blob_id:
                raise ValueError("editorial_enrichment_revision_result_corrupt")
        else:
            prior_bytes = await artifact_store.read_bytes(prior.canonical_blob_id)
            if prior_bytes != candidate.canonical_bytes:
                raise ValueError("editorial_enrichment_revision_result_corrupt")
        await persistence_service.assert_editorial_enrichment_revision_fence(
            run_id=candidate.base_artifact.production_run_id,
            expected_current_artifact_id=candidate.base_artifact.id,
            expected_current_input_hash=candidate.base_artifact.input_hash,
            expected_current_canonical_sha256=candidate.base_canonical_sha256,
            uow=uow,
        )
        return prior
    return await persistence_service.store_editorial_enrichment_result(
        run_id=candidate.base_artifact.production_run_id,
        subject_id=candidate.base_artifact.subject_id,
        input_hash=candidate.input_hash,
        enrichment=candidate.enrichment,
        extraction=candidate.extraction,
        synthesis=candidate.synthesis,
        raw_result=raw_result,
        model_run_id=model_run_id,
        evidence_pack_hash=evidence_pack_hash,
        access_policy_hash=access_policy_hash,
        model_policy_version=model_policy_version,
        routing_policy_version=routing_policy_version,
        projection_hash=projection_hash,
        source_figure_inventory_hash=source_figure_inventory_hash,
        metadata_extra=metadata_extra,
        artifact_id=candidate.artifact_id,
        expected_current_artifact_id=candidate.base_artifact.id,
        expected_current_input_hash=candidate.base_artifact.input_hash,
        expected_current_canonical_sha256=candidate.base_canonical_sha256,
        canonical_payload=candidate.canonical_payload,
        resource_search_enabled=resource_search_enabled,
        preuploaded_payload_ids=preuploaded_payload_ids,
        preuploaded_payload_sha256=preuploaded_payload_sha256,
        invalidate_downstream=invalidate_downstream,
        uow=uow,
    )
