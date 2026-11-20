"""Workshop-facing validation and lineage for enrichment revisions.

The legacy production revision service remains the public workflow façade. This
module owns its typed patch, target, placement, and lineage rules so the
editorial workshop can use the same validation without owning that workflow.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Any, cast
from uuid import UUID

from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.application.production_editorial_enrichment import (
    SOURCE_FIGURE_PROVENANCE_DIAGNOSTICS_VERSION,
    EditorialEnrichmentEvidencePackV1,
    EditorialEnrichmentProposalControlError,
    EditorialEnrichmentProposalV1,
    EditorialEnrichmentWireParseResult,
    ProductionEditorialEnrichmentService,
    canonical_synthesis_hash,
    validate_editorial_enrichment_proposal,
)
from cti_app.application.production_prompts import (
    EDITORIAL_ENRICHMENT_PROMPT_VERSION,
    EDITORIAL_ENRICHMENT_PROPOSAL_CONTRACT_VERSION,
    EDITORIAL_ENRICHMENT_REVISION_CONTRACT_VERSION,
    EDITORIAL_ENRICHMENT_REVISION_PROMPT_VERSION,
    EDITORIAL_ENRICHMENT_WIRE_PARSER_VERSION,
)
from cti_app.application.production_synthesis import (
    canonical_extraction_hash,
)
from cti_app.application.production_synthesis_revision import _section_id
from cti_app.domain.production import ProductionArtifact, ProductionArtifactStage
from cti_app.domain.production_editorial_enrichment import (
    EditorialEnrichmentElementKind,
    EditorialEnrichmentRevisionAction,
    EditorialEnrichmentV1,
    EnrichmentPlacementKind,
    EnrichmentPlacementV1,
)
from cti_app.domain.production_editorial_revision import (
    TargetedEditorialRevisionOperation,
    TargetedEditorialRevisionPlacementKind,
    TargetedEditorialRevisionRequest,
    TargetedEditorialRevisionTargetKind,
)
from cti_app.domain.production_extraction import ProductionExtractionV1
from cti_app.domain.production_synthesis import ProductionSynthesisV1, production_synthesis_to_json

_LEGACY_EDITORIAL_ENRICHMENT_GENERATOR_VERSION = "model-text-blocks-v8-editorial-tables"
_LEGACY_EDITORIAL_ENRICHMENT_ROUTING_POLICY_VERSION = (
    "editorial-enrichment-routing-policy-v2-resource-search-off"
)


class EditorialEnrichmentRevisionValidationError(RuntimeError):
    code = "editorial_enrichment_revision_rejected"

    def __init__(self, rejections: list[dict[str, str]]) -> None:
        self.rejections = rejections
        super().__init__("The proposed enrichment element failed validation")


def targeted_request_identity_payload(
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
        "parser_version": EDITORIAL_ENRICHMENT_WIRE_PARSER_VERSION,
        "validator_version": "editorial-enrichment-validator-v1",
    }


def targeted_request_identity(request: TargetedEditorialRevisionRequest) -> str:
    return hashlib.sha256(
        ProductionArtifactStore.canonical_json_bytes(targeted_request_identity_payload(request))
    ).hexdigest()


def targeted_action(
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


def validate_targeted_request(request: TargetedEditorialRevisionRequest) -> None:
    target = request.target
    if target.kind is not TargetedEditorialRevisionTargetKind.ENRICHMENT_ELEMENT:
        raise ValueError("targeted_enrichment_revision_target_invalid")
    if target.element_kind is None:
        raise ValueError("targeted_enrichment_revision_kind_required")
    create_operations = {
        TargetedEditorialRevisionOperation.CREATE_TABLE,
        TargetedEditorialRevisionOperation.CREATE_DIAGRAM,
        TargetedEditorialRevisionOperation.CREATE_CHART,
    }
    if request.operation in create_operations:
        if target.element_key is not None or target.placement is None:
            raise ValueError("targeted_enrichment_revision_create_target_invalid")
    elif target.element_key is None or target.placement is not None:
        raise ValueError("targeted_enrichment_revision_element_target_invalid")


def validate_targeted_action(
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
        EditorialEnrichmentRevisionAction.DETAIL_DIAGRAM: {EditorialEnrichmentElementKind.DIAGRAM},
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


def validate_editorial_enrichment_patch(
    patch: object,
    *,
    element_kind: EditorialEnrichmentElementKind,
    element_key: str,
    evidence_pack: EditorialEnrichmentEvidencePackV1,
    extraction: ProductionExtractionV1,
    synthesis: ProductionSynthesisV1,
    figure_catalog: tuple[Any, ...],
    validator_rejections: list[dict[str, str]] | None = None,
) -> EditorialEnrichmentV1:
    """Validate one supported typed enrichment patch against its exact target."""
    rejections = list(validator_rejections or ())
    if isinstance(patch, EditorialEnrichmentWireParseResult):
        proposal = patch.proposal
    else:
        try:
            proposal = EditorialEnrichmentProposalV1.model_validate(cast(Any, patch))
        except (TypeError, ValueError):
            proposal = None
    if proposal is None:
        raise EditorialEnrichmentRevisionValidationError(
            [*rejections, {"block_id": "proposal", "reason_code": "revision_patch_shape_invalid"}]
        )
    if proposal.annotations or proposal.resource_needs:
        raise EditorialEnrichmentRevisionValidationError(
            [*rejections, {"block_id": "proposal", "reason_code": "revision_extra_blocks_rejected"}]
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
            [*rejections, {"block_id": "proposal", "reason_code": "revision_target_count_mismatch"}]
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
                    *rejections,
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
        rejection = {"block_id": element_key, "reason_code": str(code)}
        if isinstance(exc, EditorialEnrichmentProposalControlError):
            rejection["reason_code"] = exc.code.value
            rejection["diagnostic_code"] = exc.diagnostic_code
            rejection["diagnostic"] = str(exc)
        raise EditorialEnrichmentRevisionValidationError([*rejections, rejection]) from exc


def assert_requested_placement(
    *,
    candidate: EditorialEnrichmentV1,
    element_kind: EditorialEnrichmentElementKind,
    targeted_request: TargetedEditorialRevisionRequest | None,
    synthesis: ProductionSynthesisV1,
    evidence_pack: Any | None = None,
) -> None:
    """Check the placement requested for a new element without rewriting legacy data."""
    if targeted_request is None or targeted_request.target.placement is None:
        raise ValueError("targeted_enrichment_revision_placement_required")
    requested = targeted_request.target.placement
    if evidence_pack is not None:
        expected = requested_registered_placement(
            targeted_request=targeted_request,
            synthesis=synthesis,
            evidence_pack=evidence_pack,
        )
    elif requested.kind is TargetedEditorialRevisionPlacementKind.AFTER_LEAD:
        expected = EnrichmentPlacementV1(EnrichmentPlacementKind.AFTER_LEAD)
    elif requested.kind is TargetedEditorialRevisionPlacementKind.AFTER_TIMELINE:
        expected = EnrichmentPlacementV1(EnrichmentPlacementKind.AFTER_TIMELINE)
    else:
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


def requested_registered_placement(
    *,
    targeted_request: TargetedEditorialRevisionRequest,
    synthesis: ProductionSynthesisV1,
    evidence_pack: Any,
) -> EnrichmentPlacementV1:
    """Resolve a targeted legacy placement request to its last registered paragraph."""
    requested = targeted_request.target.placement
    if requested is None:
        raise ValueError("targeted_enrichment_revision_placement_required")
    current_synthesis = evidence_pack.current_synthesis
    if requested.kind is TargetedEditorialRevisionPlacementKind.AFTER_LEAD:
        paragraphs = current_synthesis.get("lead", ())
    elif requested.kind is TargetedEditorialRevisionPlacementKind.AFTER_SECTION:
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
        section = next(
            (
                value
                for value in current_synthesis.get("sections", ())
                if isinstance(value, Mapping) and value.get("section_index") == section_index
            ),
            None,
        )
        paragraphs = section.get("paragraphs", ()) if isinstance(section, Mapping) else ()
    else:
        raise ValueError("targeted_enrichment_revision_after_timeline_unsupported")

    registered_anchor_ids = evidence_pack.registered_anchor_ids
    if registered_anchor_ids is None:
        raise ValueError("editorial_anchor_stale")
    for paragraph in reversed(tuple(paragraphs)):
        if not isinstance(paragraph, Mapping):
            continue
        try:
            anchor_id = UUID(str(paragraph.get("block_id")))
        except (TypeError, ValueError):
            continue
        if anchor_id in registered_anchor_ids:
            return EnrichmentPlacementV1(
                EnrichmentPlacementKind.AFTER_PARAGRAPH,
                anchor_block_id=anchor_id,
            )
    raise ValueError("editorial_anchor_stale")


def rebased_lineage_matches(
    *,
    base_artifact: ProductionArtifact,
    extraction_artifact: ProductionArtifact,
    projection_artifact: ProductionArtifact | None,
    synthesis_artifact: ProductionArtifact,
    extraction_hash: str,
    synthesis_hash: str,
    projection_hash: str | None,
) -> bool:
    """Fence a narrowly rebased identity to its verified current inputs."""
    metadata = base_artifact.metadata
    input_artifacts = metadata.get("input_artifacts")
    if not isinstance(input_artifacts, dict):
        return False
    expected_artifacts = {
        "extraction_artifact_id": str(extraction_artifact.id),
        "relevance_projection_artifact_id": (
            str(projection_artifact.id) if projection_artifact is not None else None
        ),
        "synthesis_artifact_id": str(synthesis_artifact.id),
    }
    if any(
        key not in input_artifacts or input_artifacts[key] != value
        for key, value in expected_artifacts.items()
    ):
        return False
    return (
        metadata.get("extraction_hash") == extraction_hash
        and metadata.get("synthesis_hash") == synthesis_hash
        and metadata.get("relevance_projection_hash") == projection_hash
    )


def legacy_input_fence_matches(
    *,
    base_artifact: ProductionArtifact,
    extraction: ProductionExtractionV1,
    synthesis: ProductionSynthesisV1,
    projection: Any,
    resource_search_enabled: bool,
) -> bool:
    """Accept known V8 identities only when every admitted data input is unchanged."""
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


class EditorialEnrichmentRevisionLineage:
    """Project existing enrichment revision lineage without changing artifact bytes."""

    def __init__(self, enrichment_service: ProductionEditorialEnrichmentService) -> None:
        self._enrichment = enrichment_service

    @staticmethod
    def _provenance_key(element_kind: EditorialEnrichmentElementKind, key: str) -> str:
        return f"{element_kind.value}:{key}"

    async def element_provenance_after_revision(
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
