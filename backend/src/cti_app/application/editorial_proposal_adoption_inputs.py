"""Production inputs for model-free editorial proposal adoption."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
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
from cti_app.application.editorial_figure_catalog import (
    EditorialFigureCatalogError,
    opaque_figure_id,
    validate_figure_insertion_limit,
)
from cti_app.application.editorial_proposal_adoption import (
    EditorialProposalAdoptionError,
    EditorialProposalAdoptionInputs,
    EnrichmentCandidateInputs,
    SynthesisCandidateInputs,
    assert_adoption_rewrite_table_placement,
)
from cti_app.application.editorial_proposals import (
    _validate_editorial_paragraph_wire_grounding,
    _validate_editorial_text_grounding,
)
from cti_app.application.editorial_synthesis.candidate import (
    SynthesisCandidatePersistence,
)
from cti_app.application.editorial_synthesis.candidate import (
    build_candidate_from_validated_patch as build_synthesis_candidate,
)
from cti_app.application.editorial_synthesis.patch_validation import WirePatch
from cti_app.application.editorial_synthesis.targets import resolve_target
from cti_app.application.editorial_text_policy import editorial_patch_text_violation
from cti_app.application.editorial_workspace.registry import (
    WorkspaceOccurrenceResolution,
    read_pointer,
    resolve_workspace_target,
)
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.application.production_editorial_enrichment import (
    EDITORIAL_ENRICHMENT_MODEL_POLICY_VERSION,
    EditorialEnrichmentEvidencePackV1,
    EditorialFigureCatalogEntry,
    build_editorial_figure_catalog,
    validate_editorial_enrichment,
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
from cti_app.application.source_figure_inventory import load_archived_source_figure_inventory
from cti_app.domain.classification import TLP
from cti_app.domain.collection import CollectionState
from cti_app.domain.editorial_proposal import EditorialProposal, EditorialProposalOrigin
from cti_app.domain.editorial_workspace import (
    EditorialSourceStage,
    bootstrap_block_registry,
    canonical_content_sha256,
)
from cti_app.domain.media_assets import MediaAssetKind
from cti_app.domain.production import (
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionArtifactStatus,
    ProductionInputSnapshot,
    ProductionRun,
    SynthesisMode,
)
from cti_app.domain.production_editorial_enrichment import (
    EDITORIAL_ENRICHMENT_V9_POLICY_VERSION,
    ChartKind,
    EditorialEnrichmentElementKind,
    EditorialEnrichmentV1,
    EnrichmentPlacementKind,
    EnrichmentPlacementV1,
    SourceFigureCandidateV1,
    SourceFigureDecision,
    SourceFigureInclusionStatus,
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
from cti_app.domain.production_synthesis import (
    EvidenceKind,
    ExtractionEvidenceRefV1,
    production_synthesis_from_json,
    production_synthesis_to_json,
)
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
    base_enrichment: EditorialEnrichmentV1 | None = None
    synthesis: Any | None = None


_ACTIVE_SOURCE_STATES = frozenset(
    {CollectionState.ARCHIVED, CollectionState.EXTRACTED, CollectionState.COMPLETED}
)

_MANUAL_SOURCE_FIGURE_FIELDS = frozenset(
    {
        "figure_id",
        "caption",
        "order",
        "source_document_id",
        "source_url",
        "sha256",
        "mime_type",
        "byte_size",
        "width",
        "height",
        "provenance",
    }
)


def _manual_source_figure_candidate_patch(
    raw_patch: Mapping[str, Any],
    *,
    subject_id: UUID,
    run_id: UUID,
    catalog: tuple[EditorialFigureCatalogEntry, ...],
    extraction: ProductionExtractionV1,
    base_enrichment: EditorialEnrichmentV1,
    anchor_block_id: UUID,
    publication_kind: PublicationKind,
    inventory_limit: int,
) -> tuple[EditorialEnrichmentV1, str]:
    """Rebuild a multi-figure candidate exclusively from current archived inventory."""
    raw_figures = raw_patch.get("manual_source_figures")
    if not isinstance(raw_figures, list) or not raw_figures:
        raise EditorialProposalAdoptionError("proposal_patch_invalid")
    by_public_id = {
        opaque_figure_id(subject_id, run_id, entry.figure.figure_id): entry for entry in catalog
    }
    sources = {source.source_document_id: source for source in extraction.sources}
    placements = EnrichmentPlacementV1(
        kind=EnrichmentPlacementKind.AFTER_PARAGRAPH,
        anchor_block_id=anchor_block_id,
    )
    candidates: list[SourceFigureCandidateV1] = []
    selected_ids: set[UUID] = set()
    existing_ids = {
        item.resolved_figure.figure_id
        for item in base_enrichment.source_figures
        if item.resolved_figure is not None
    }
    included_ids = {
        item.resolved_figure.figure_id
        for item in base_enrichment.source_figures
        if item.inclusion_status is SourceFigureInclusionStatus.INCLUDED
        and item.resolved_figure is not None
    }
    for position, raw in enumerate(raw_figures):
        if not isinstance(raw, Mapping) or set(raw) != _MANUAL_SOURCE_FIGURE_FIELDS:
            raise EditorialProposalAdoptionError("proposal_patch_invalid")
        public_id = raw.get("figure_id")
        caption = raw.get("caption")
        order = raw.get("order")
        if (
            not isinstance(public_id, str)
            or not isinstance(caption, str)
            or editorial_patch_text_violation(caption)
            or not caption.strip()
            or len(caption.strip()) > 500
            or type(order) is not int
            or order != position
            or type(raw.get("byte_size")) is not int
            or type(raw.get("width")) is not int
            or type(raw.get("height")) is not int
        ):
            raise EditorialProposalAdoptionError("proposal_patch_invalid")
        entry = by_public_id.get(public_id)
        if entry is None:
            raise EditorialProposalAdoptionError("editorial_figure_not_eligible")
        figure = entry.figure
        metadata = entry.metadata
        source = sources.get(figure.source_document_id)
        if (
            figure.decision is not SourceFigureDecision.ACCEPTED
            or figure.blob_id is None
            or figure.sha256 is None
            or figure.mime_type is None
            or figure.byte_size is None
            or metadata.width is None
            or metadata.height is None
            or metadata.width < 1
            or metadata.height < 1
            or source is None
            or figure.source != source.canonical_url
            or figure.figure_id in existing_ids
            or figure.figure_id in selected_ids
            or raw.get("source_document_id") != str(figure.source_document_id)
            or raw.get("source_url") != source.canonical_url
            or raw.get("sha256") != figure.sha256
            or raw.get("mime_type") != figure.mime_type
            or raw.get("byte_size") != figure.byte_size
            or raw.get("width") != metadata.width
            or raw.get("height") != metadata.height
            or raw.get("provenance") != figure.provenance
        ):
            raise EditorialProposalAdoptionError("editorial_figure_not_eligible")
        selected_ids.add(figure.figure_id)
        candidates.append(
            SourceFigureCandidateV1(
                key=f"source_figure_{figure.figure_id.hex}",
                source_document_id=figure.source_document_id,
                source_url=source.canonical_url,
                caption=caption.strip(),
                provenance=figure.provenance,
                locator=figure.locator,
                inclusion_status=SourceFigureInclusionStatus.INCLUDED,
                placement=placements,
                resolved_figure=figure,
                width=metadata.width,
                height=metadata.height,
                order=len(base_enrichment.source_figures) + order,
            )
        )
    try:
        validate_figure_insertion_limit(
            publication_kind=publication_kind,
            integrated_count=len(included_ids),
            requested_count=len(candidates),
            inventory_limit=inventory_limit,
        )
    except EditorialFigureCatalogError as error:
        raise EditorialProposalAdoptionError(error.code) from None
    if base_enrichment.schema_version < 8:
        raise EditorialProposalAdoptionError("proposal_operation_unsupported")
    candidate_enrichment = replace(
        replace(
            base_enrichment,
            schema_version=9,
            enrichment_policy_version=EDITORIAL_ENRICHMENT_V9_POLICY_VERSION,
        )
        if base_enrichment.schema_version < 9
        else base_enrichment,
        tables=(),
        diagrams=(),
        charts=(),
        source_figures=tuple(candidates),
        figure_decisions=(),
        annotations=(),
    )
    identity = "|".join(sorted(str(value) for value in selected_ids))
    key_suffix = hashlib.sha256(identity.encode()).hexdigest()[:24]
    return candidate_enrichment, f"manual_source_figures_{key_suffix}"


def _manual_text_without_evidence_patch(
    *,
    base_synthesis: Any,
    base_payload: dict[str, Any],
    target: TargetedEditorialRevisionTarget,
    operation: TargetedEditorialRevisionOperation,
    patch: WirePatch,
    evidence_pack: SynthesisEvidencePackV1,
    extraction: ProductionExtractionV1,
    snapshot: ProductionInputSnapshot,
    publication_kind: PublicationKind | None = None,
    source_addition_document_ids: frozenset[UUID] = frozenset(),
    require_claim_scopes: bool = False,
) -> tuple[Any, dict[str, Any], str]:
    del evidence_pack, extraction, snapshot, publication_kind
    del source_addition_document_ids, require_claim_scopes
    if operation is not TargetedEditorialRevisionOperation.REWRITE_PARAGRAPH:
        raise ValueError("manual_edit_patch_invalid")
    resolved = resolve_target(base_payload, target, operation)
    if patch.target != resolved.path or patch.before_hash != resolved.before_hash:
        raise EditorialProposalAdoptionError(
            "proposal_base_stale", reason="patch:target_or_before_hash"
        )
    if target.kind is TargetedEditorialRevisionTargetKind.LEAD_PARAGRAPH:
        target_index = resolved.indexes[0]
        old_paragraph = base_synthesis.lead[target_index]
        if old_paragraph.evidence_refs:
            raise EditorialProposalAdoptionError("proposal_target_mismatch")
        paragraphs = list(base_synthesis.lead)
        paragraphs[target_index] = replace(old_paragraph, text=patch.after, evidence_refs=())
        revised_synthesis = replace(base_synthesis, lead=tuple(paragraphs))
    elif target.kind is TargetedEditorialRevisionTargetKind.SECTION_PARAGRAPH:
        if resolved.parent_index is None:
            raise EditorialProposalAdoptionError("proposal_target_mismatch")
        section_index = resolved.parent_index
        paragraph_index = resolved.indexes[0]
        sections = list(base_synthesis.sections)
        old_paragraph = sections[section_index].paragraphs[paragraph_index]
        if old_paragraph.evidence_refs:
            raise EditorialProposalAdoptionError("proposal_target_mismatch")
        paragraphs = list(sections[section_index].paragraphs)
        paragraphs[paragraph_index] = replace(old_paragraph, text=patch.after, evidence_refs=())
        sections[section_index] = replace(sections[section_index], paragraphs=tuple(paragraphs))
        revised_synthesis = replace(base_synthesis, sections=tuple(sections))
    else:
        raise EditorialProposalAdoptionError("proposal_target_mismatch")
    _validate_editorial_text_grounding(patch.after, admitted_evidence_texts=())
    return revised_synthesis, production_synthesis_to_json(revised_synthesis), resolved.before_hash


def _proposal_evidence_texts(
    evidence_pack: SynthesisEvidencePackV1,
    references: Sequence[Mapping[str, object]],
) -> tuple[str, ...]:
    handles: set[str] = set()
    for reference in references:
        try:
            evidence_ref = ExtractionEvidenceRefV1(
                source_document_id=UUID(str(reference["source_document_id"])),
                kind=EvidenceKind(str(reference["kind"])),
                evidence_key=str(reference["evidence_key"]),
            )
        except (KeyError, TypeError, ValueError):
            raise EditorialProposalAdoptionError("proposal_evidence_not_admissible") from None
        handle = evidence_pack.handle_for(evidence_ref)
        if handle is None:
            raise EditorialProposalAdoptionError("proposal_evidence_not_admissible")
        handles.add(handle)
    entries = (
        *evidence_pack.narrative_evidence,
        *evidence_pack.technical_evidence,
        *evidence_pack.reserve_evidence,
    )
    return tuple(
        json.dumps(entry, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        for entry in entries
        if isinstance(entry.get("handle"), str) and entry["handle"] in handles
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

    async def _verify_bar_chart_asset(
        self,
        *,
        proposal_patch: Mapping[str, Any],
        candidate: EditorialEnrichmentV1,
        operation: TargetedEditorialRevisionOperation,
    ) -> tuple[EditorialEnrichmentV1, Mapping[str, object] | None]:
        if operation is not TargetedEditorialRevisionOperation.CREATE_BAR_CHART:
            return candidate, None
        raw_manifest = proposal_patch.get("compiled_chart_asset")
        required = {
            "asset_id",
            "sha256",
            "compiler_name",
            "compiler_version",
            "policy_version",
        }
        if not isinstance(raw_manifest, Mapping) or set(raw_manifest) != required:
            raise EditorialProposalAdoptionError("proposal_patch_invalid")
        try:
            asset_id = UUID(str(raw_manifest["asset_id"]))
        except (TypeError, ValueError):
            raise EditorialProposalAdoptionError("proposal_patch_invalid") from None
        if (
            len(candidate.charts) != 1
            or candidate.charts[0].kind is not ChartKind.BAR
            or not isinstance(raw_manifest.get("sha256"), str)
            or re.fullmatch(r"[0-9a-f]{64}", str(raw_manifest["sha256"])) is None
            or any(
                not isinstance(raw_manifest.get(field_name), str)
                for field_name in ("compiler_name", "compiler_version", "policy_version")
            )
        ):
            raise EditorialProposalAdoptionError("proposal_patch_invalid")
        media_store = getattr(self._enrichment_loader, "_media_asset_store", None)
        compiler = getattr(self._enrichment_loader, "_chart_compiler", None)
        if media_store is None or compiler is None:
            raise EditorialProposalAdoptionError("editorial_enrichment_chart_render_failed")
        chart = candidate.charts[0]
        try:
            compiled = await compiler.compile(chart)
            manifest = await media_store.get(asset_id)
            content = await media_store.read(asset_id)
        except Exception:
            raise EditorialProposalAdoptionError(
                "editorial_enrichment_chart_render_failed"
            ) from None
        if (
            manifest is None
            or manifest.asset_id != asset_id
            or manifest.kind is not MediaAssetKind.CHART_SVG
            or manifest.sha256 != raw_manifest["sha256"]
            or manifest.compiler_name != raw_manifest["compiler_name"]
            or manifest.compiler_version != raw_manifest["compiler_version"]
            or manifest.policy_version != raw_manifest["policy_version"]
            or compiled.media_sha256 != manifest.sha256
            or compiled.media_bytes != content
        ):
            raise EditorialProposalAdoptionError("proposal_patch_invalid")
        compiled_chart = replace(chart, compiled_asset_id=asset_id)
        return replace(candidate, charts=(compiled_chart,)), dict(raw_manifest)

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
                raise EditorialProposalAdoptionError(
                    "proposal_base_stale", reason="snapshot:occurrence_binding"
                )
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
            manual_text_without_evidence = patch.get("manual_text_without_evidence") is True
            if manual_text_without_evidence and (
                proposal.origin is not EditorialProposalOrigin.MANUAL_PATCH
                or patch.get("evidence_refs") != []
            ):
                raise EditorialProposalAdoptionError("proposal_patch_invalid")
            admitted_evidence_texts = (
                ()
                if manual_text_without_evidence
                else _proposal_evidence_texts(evidence_pack, proposal.evidence_refs)
            )
            if manual_text_without_evidence:
                _validate_editorial_text_grounding(
                    after,
                    admitted_evidence_texts=(),
                    allow_newline_tab=True,
                )
            elif operation is TargetedEditorialRevisionOperation.REWRITE_PARAGRAPH:
                _validate_editorial_paragraph_wire_grounding(
                    after,
                    admitted_evidence_texts=admitted_evidence_texts,
                )
            else:
                _validate_editorial_text_grounding(
                    after,
                    admitted_evidence_texts=admitted_evidence_texts,
                )
            canonical_target = resolve_target(base_payload, resolved.target, operation)
            wire_patch = WirePatch(
                target=canonical_target.path,
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
                apply_patch=(
                    _manual_text_without_evidence_patch
                    if manual_text_without_evidence
                    else _apply_wire_patch
                ),
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
            mapped_code = str(code or "proposal_base_unavailable")
            reason = "candidate:input_validation" if mapped_code == "proposal_base_stale" else None
            raise EditorialProposalAdoptionError(mapped_code, reason=reason) from None

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
            persistence=persistence,
            registry_lineage=lineage,
            publication_kind=resolution.record.publication_kind,
            apply_patch=(
                _manual_text_without_evidence_patch
                if patch.get("manual_text_without_evidence") is True
                else _apply_wire_patch
            ),
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
            if target.element_kind is None:
                raise EditorialProposalAdoptionError("proposal_target_mismatch")
            is_create = operation in {
                TargetedEditorialRevisionOperation.CREATE_TABLE,
                TargetedEditorialRevisionOperation.CREATE_BAR_CHART,
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
            table_insertion = operation is TargetedEditorialRevisionOperation.CREATE_TABLE
            bar_chart_insertion = operation is TargetedEditorialRevisionOperation.CREATE_BAR_CHART
            source_figure_insertion = (
                operation is TargetedEditorialRevisionOperation.CREATE_SOURCE_FIGURE
            )
            paragraph_enrichment_insertion = (
                table_insertion or bar_chart_insertion or source_figure_insertion
            )
            raw_patch = patch.get("enrichment_patch")
            if not isinstance(raw_patch, dict):
                raise EditorialProposalAdoptionError("proposal_patch_invalid")
            table_key: str | None = None
            chart_key: str | None = None
            source_figure_key: str | None = None
            if table_insertion:
                raw_tables = raw_patch.get("tables")
                if (
                    target.element_kind is not EditorialEnrichmentElementKind.TABLE
                    or target.element_key is not None
                    or target.placement is None
                    or target.placement.kind
                    is not TargetedEditorialRevisionPlacementKind.AFTER_PARAGRAPH
                    or target.placement.anchor_block_id != proposal.occurrence_id
                    or not isinstance(raw_tables, list)
                    or len(raw_tables) != 1
                    or not isinstance(raw_tables[0], Mapping)
                    or not isinstance(raw_tables[0].get("key"), str)
                ):
                    raise EditorialProposalAdoptionError("editorial_anchor_stale")
                table_key = str(raw_tables[0]["key"])
            if bar_chart_insertion:
                raw_charts = raw_patch.get("charts")
                if (
                    target.element_kind is not EditorialEnrichmentElementKind.CHART
                    or target.element_key is not None
                    or target.placement is None
                    or target.placement.kind
                    is not TargetedEditorialRevisionPlacementKind.AFTER_PARAGRAPH
                    or target.placement.anchor_block_id != proposal.occurrence_id
                    or not isinstance(raw_charts, list)
                    or len(raw_charts) != 1
                    or not isinstance(raw_charts[0], Mapping)
                    or raw_charts[0].get("kind") != ChartKind.BAR.value
                    or not isinstance(raw_charts[0].get("key"), str)
                ):
                    raise EditorialProposalAdoptionError("editorial_anchor_stale")
                chart_key = str(raw_charts[0]["key"])
            if source_figure_insertion:
                if (
                    target.element_kind is not EditorialEnrichmentElementKind.FIGURE
                    or target.element_key is not None
                    or target.placement is None
                    or target.placement.kind
                    is not TargetedEditorialRevisionPlacementKind.AFTER_PARAGRAPH
                    or target.placement.anchor_block_id != proposal.occurrence_id
                ):
                    raise EditorialProposalAdoptionError("editorial_anchor_stale")
            element_key = (
                table_key
                if table_insertion
                else chart_key
                if bar_chart_insertion
                else target.element_key
            )
            base_enrichment = editorial_enrichment_from_json(base_payload)
            validated_manual_figure_patch: EditorialEnrichmentV1 | None = None
            if source_figure_insertion:
                if resolution.record.publication_kind not in {
                    PublicationKind.ARTICLE,
                    PublicationKind.BRIEF,
                }:
                    raise EditorialProposalAdoptionError("proposal_operation_unsupported")
                validated_manual_figure_patch, source_figure_key = (
                    _manual_source_figure_candidate_patch(
                        raw_patch,
                        subject_id=proposal.subject_id,
                        run_id=proposal.production_run_id,
                        catalog=tuple(fence.figure_catalog),
                        extraction=extraction,
                        base_enrichment=base_enrichment,
                        anchor_block_id=proposal.occurrence_id,
                        publication_kind=resolution.record.publication_kind,
                        inventory_limit=fence.inventory.max_figures,
                    )
                )
                validate_editorial_enrichment(
                    validated_manual_figure_patch,
                    extraction=extraction,
                    synthesis=synthesis,
                )
            element_key = (
                table_key
                if table_insertion
                else chart_key
                if bar_chart_insertion
                else source_figure_key
                if source_figure_insertion
                else target.element_key
            )
            if element_key is None:
                raise EditorialProposalAdoptionError("proposal_target_mismatch")
            target_entry = resolution.entry
            if (
                target_entry is None
                or resolution.binding_state != "valid"
                or (
                    target_entry.source_stage is not EditorialSourceStage.SYNTHESIS
                    if paragraph_enrichment_insertion
                    else target_entry.source_stage is not EditorialSourceStage.EDITORIAL_ENRICHMENT
                )
                or target_entry.content_sha256 != proposal.before_hash
                or (
                    paragraph_enrichment_insertion
                    and (
                        target_entry.kind.value not in {"paragraph", "lead_paragraph"}
                        or target_entry.block_id != proposal.occurrence_id
                    )
                )
            ):
                raise EditorialProposalAdoptionError(
                    "editorial_anchor_stale"
                    if paragraph_enrichment_insertion
                    else "proposal_base_stale",
                    reason=None if paragraph_enrichment_insertion else "pins:enrichment_occurrence",
                )
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
            validated_patch = validated_manual_figure_patch
            if validated_patch is None:
                validated_patch = validate_editorial_enrichment_patch(
                    raw_patch,
                    element_kind=target.element_kind,
                    element_key=element_key,
                    evidence_pack=fence.evidence_pack,
                    extraction=extraction,
                    synthesis=synthesis,
                    figure_catalog=fence.figure_catalog,
                )
            validated_patch, compiled_chart_asset = await self._verify_bar_chart_asset(
                proposal_patch=patch,
                candidate=validated_patch,
                operation=operation,
            )
            assert_adoption_rewrite_table_placement(
                base_enrichment=base_enrichment,
                candidate=validated_patch,
                element_kind=target.element_kind,
                element_key=element_key,
                operation=operation,
                is_create=is_create,
            )
            if is_create:
                assert_requested_placement(
                    candidate=validated_patch,
                    element_kind=target.element_kind,
                    targeted_request=request,
                    synthesis=synthesis,
                    evidence_pack=(fence.evidence_pack if paragraph_enrichment_insertion else None),
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
                element_key=element_key,
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
                element_key=element_key,
                input_hash=fence.input_hash,
                artifact_id=candidate.artifact_id,
                figure_catalog=fence.figure_catalog,
                validated_patch=validated_manual_figure_patch,
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
                compiled_chart_asset=compiled_chart_asset,
            )
        except EditorialProposalAdoptionError:
            raise
        except Exception as error:
            code = getattr(error, "reason_code", None) or getattr(error, "code", None)
            mapped_code = str(code or "proposal_base_unavailable")
            reason = "candidate:input_validation" if mapped_code == "proposal_base_stale" else None
            raise EditorialProposalAdoptionError(mapped_code, reason=reason) from None

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
            base_enrichment=base_enrichment,
            synthesis=synthesis,
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
        if resolution.binding_state != "valid":
            raise EditorialProposalAdoptionError(
                "proposal_base_stale", reason="snapshot:binding_state"
            )
        if record.subject_id != proposal.subject_id:
            raise EditorialProposalAdoptionError(
                "proposal_base_stale", reason="snapshot:subject_id"
            )
        if record.production_run_id != proposal.production_run_id:
            raise EditorialProposalAdoptionError(
                "proposal_base_stale", reason="snapshot:production_run_id"
            )
        if record.snapshot_id != proposal.snapshot_id:
            raise EditorialProposalAdoptionError(
                "proposal_base_stale", reason="snapshot:id_mismatch"
            )
        if resolution.entry is None:
            raise EditorialProposalAdoptionError(
                "proposal_base_stale", reason="snapshot:entry_missing"
            )
        if resolution.entry.block_id != proposal.occurrence_id:
            raise EditorialProposalAdoptionError(
                "proposal_base_stale", reason="snapshot:occurrence_id"
            )
        if record.workspace_pin_hash != proposal.base_pin_hash:
            raise EditorialProposalAdoptionError(
                "proposal_base_stale", reason="pins:workspace_pin_hash"
            )
        if expected_artifact_id != proposal.base_artifact_id:
            field = (
                "synthesis_artifact_id"
                if prepared.stage is ProductionArtifactStage.SYNTHESIS
                else "enrichment_artifact_id"
            )
            raise EditorialProposalAdoptionError("proposal_base_stale", reason=f"pins:{field}")
        if expected_version != proposal.base_artifact_version:
            field = (
                "synthesis_version"
                if prepared.stage is ProductionArtifactStage.SYNTHESIS
                else "enrichment_version"
            )
            raise EditorialProposalAdoptionError("proposal_base_stale", reason=f"pins:{field}")
        if expected_sha256 != proposal.base_canonical_sha256:
            field = (
                "synthesis_canonical_sha256"
                if prepared.stage is ProductionArtifactStage.SYNTHESIS
                else "enrichment_canonical_sha256"
            )
            raise EditorialProposalAdoptionError("proposal_base_stale", reason=f"pins:{field}")
        if record.routing_decision_hash != proposal.base_routing_hash:
            raise EditorialProposalAdoptionError(
                "proposal_base_stale", reason="routing:decision_hash"
            )

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
        if run is None or run.id != prepared.run.id:
            raise EditorialProposalAdoptionError("proposal_base_stale", reason="run:id")
        if run.subject_id != proposal.subject_id:
            raise EditorialProposalAdoptionError("proposal_base_stale", reason="run:subject_id")
        if current_run is None or current_run.id != run.id:
            raise EditorialProposalAdoptionError("proposal_base_stale", reason="run:not_current")
        if current_run.pipeline_generation != prepared.run.pipeline_generation:
            raise EditorialProposalAdoptionError(
                "proposal_base_stale", reason="run:pipeline_generation"
            )
        if snapshot is None:
            raise EditorialProposalAdoptionError(
                "proposal_base_stale", reason="base_context:input_snapshot_missing"
            )
        if snapshot.id != prepared.snapshot.id:
            raise EditorialProposalAdoptionError(
                "proposal_base_stale", reason="snapshot:id_mismatch"
            )
        if current_subject is None or current_subject.id != proposal.subject_id:
            raise EditorialProposalAdoptionError(
                "proposal_base_stale", reason="subject:id_mismatch"
            )
        if not _same_current_artifact(current_base, prepared.base_artifact):
            raise EditorialProposalAdoptionError(
                "proposal_base_stale", reason="artifacts:current_base"
            )
        if not _same_current_artifact(current_extraction, prepared.extraction_artifact):
            raise EditorialProposalAdoptionError(
                "proposal_base_stale", reason="artifacts:current_extraction"
            )
        if not _same_optional_current_artifact(current_projection, prepared.projection_artifact):
            raise EditorialProposalAdoptionError(
                "proposal_base_stale", reason="artifacts:current_projection"
            )
        if not _same_current_artifact(current_synthesis, prepared.synthesis_artifact):
            raise EditorialProposalAdoptionError(
                "proposal_base_stale", reason="artifacts:current_synthesis"
            )
        if record.routing_decision_id != (routing.decision_id if routing is not None else None):
            raise EditorialProposalAdoptionError(
                "proposal_base_stale", reason="routing:decision_id"
            )
        if routing is None and run.routing_decision_id is not None:
            raise EditorialProposalAdoptionError(
                "proposal_base_stale", reason="routing:decision_missing"
            )
        if routing is not None and record.routing_decision_hash != routing.decision_hash:
            raise EditorialProposalAdoptionError(
                "proposal_base_stale", reason="routing:decision_hash"
            )

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
        if getattr(proposal, "operation", None) == (
            TargetedEditorialRevisionOperation.CREATE_SOURCE_FIGURE.value
        ):
            if prepared.stage is not ProductionArtifactStage.EDITORIAL_ENRICHMENT:
                raise EditorialProposalAdoptionError("proposal_target_mismatch")
            if prepared.base_enrichment is None:
                raise EditorialProposalAdoptionError("proposal_base_unavailable")
            try:
                source_media_archiver = (
                    self._enrichment_revision_service._enrichment._source_media_archiver
                )
                if source_media_archiver is not None:
                    from cti_app.application.production_enrichment_revision import (
                        _StoredSourceMediaArchiver,
                    )

                    source_media_archiver = _StoredSourceMediaArchiver(
                        self._uow_factory, source_media_archiver
                    )
                inventory = await load_archived_source_figure_inventory(
                    subject_id=proposal.subject_id,
                    extraction_sources=prepared.extraction.sources,
                    source_document_repository=uow.source_documents,
                    blob_repository=uow.blobs,
                    artifact_store=self._enrichment_revision_service._enrichment._artifact_store,
                    media_archiver=source_media_archiver,
                )
                current_catalog = build_editorial_figure_catalog(prepared.extraction, inventory)
                raw_enrichment_patch = patch.get("enrichment_patch")
                if not isinstance(raw_enrichment_patch, Mapping):
                    raise EditorialProposalAdoptionError("proposal_patch_invalid")
                publication_kind = record.publication_kind
                if publication_kind not in {PublicationKind.ARTICLE, PublicationKind.BRIEF}:
                    raise EditorialProposalAdoptionError("proposal_operation_unsupported")
                synthesis = prepared.synthesis
                if synthesis is None:
                    raise EditorialProposalAdoptionError("proposal_base_unavailable")
                current_candidate, _current_element_key = _manual_source_figure_candidate_patch(
                    raw_enrichment_patch,
                    subject_id=proposal.subject_id,
                    run_id=proposal.production_run_id,
                    catalog=current_catalog,
                    extraction=prepared.extraction,
                    base_enrichment=prepared.base_enrichment,
                    anchor_block_id=proposal.occurrence_id,
                    publication_kind=publication_kind,
                    inventory_limit=inventory.max_figures,
                )
                validate_editorial_enrichment(
                    current_candidate,
                    extraction=prepared.extraction,
                    synthesis=synthesis,
                )
            except EditorialProposalAdoptionError:
                raise
            except Exception:
                raise EditorialProposalAdoptionError("editorial_figure_not_eligible") from None

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
        if run is None or run.id != proposal.production_run_id:
            raise EditorialProposalAdoptionError("proposal_base_stale", reason="run:not_current")
        if artifact is None:
            raise EditorialProposalAdoptionError(
                "proposal_base_stale", reason="base_context:artifact_missing"
            )
        if artifact != base_artifact:
            raise EditorialProposalAdoptionError(
                "proposal_base_stale", reason="base_context:artifact_mismatch"
            )
        if current is None or current.id != artifact.id:
            raise EditorialProposalAdoptionError(
                "proposal_base_stale", reason="pins:current_artifact_id"
            )
        if artifact.stage is not stage:
            raise EditorialProposalAdoptionError(
                "proposal_base_stale", reason="pins:artifact_stage"
            )
        if artifact.version != proposal.base_artifact_version:
            raise EditorialProposalAdoptionError(
                "proposal_base_stale", reason="pins:artifact_version"
            )
        if artifact.input_hash != proposal.base_input_hash:
            raise EditorialProposalAdoptionError(
                "proposal_base_stale", reason="pins:artifact_input_hash"
            )
        if artifact.status is not ProductionArtifactStatus.VERIFIED:
            raise EditorialProposalAdoptionError(
                "proposal_base_stale", reason="base_context:artifact_status"
            )
        if artifact.canonical_blob_id is None:
            raise EditorialProposalAdoptionError(
                "proposal_base_stale", reason="base_context:canonical_blob_id"
            )
        if snapshot is None:
            raise EditorialProposalAdoptionError(
                "proposal_base_stale", reason="base_context:input_snapshot_missing"
            )
        if snapshot.production_run_id != run.id:
            raise EditorialProposalAdoptionError(
                "proposal_base_stale", reason="snapshot:production_run_id"
            )
        if resolution.record.snapshot_id != proposal.snapshot_id:
            raise EditorialProposalAdoptionError(
                "proposal_base_stale", reason="snapshot:id_mismatch"
            )
        if resolution.record.workspace_pin_hash != proposal.base_pin_hash:
            raise EditorialProposalAdoptionError(
                "proposal_base_stale", reason="pins:workspace_pin_hash"
            )
        if resolution.record.routing_decision_id != (
            routing.decision_id if routing is not None else None
        ):
            raise EditorialProposalAdoptionError(
                "proposal_base_stale", reason="routing:decision_id"
            )
        if routing is None and run.routing_decision_id is not None:
            raise EditorialProposalAdoptionError(
                "proposal_base_stale", reason="routing:decision_missing"
            )
        # V7+ publication envelopes may carry the resolved route hash when no
        # separate routing-decision row exists. The workspace pin already
        # fences that envelope hash; compare against a row only when one is
        # currently adopted.
        if routing is not None and resolution.record.routing_decision_hash != routing.decision_hash:
            raise EditorialProposalAdoptionError(
                "proposal_base_stale", reason="routing:decision_hash"
            )
        paragraph_enrichment_insertion = (
            stage is ProductionArtifactStage.EDITORIAL_ENRICHMENT
            and TargetedEditorialRevisionOperation(
                proposal.operation
            ).paragraph_enrichment_element_kind
            is not None
            and resolution.entry is not None
            and resolution.entry.source_stage is EditorialSourceStage.SYNTHESIS
        )
        expected_occurrence_artifact = (
            current_synthesis_artifact if paragraph_enrichment_insertion else artifact
        )
        if (
            expected_occurrence_artifact is None
            or resolution.artifact_id != expected_occurrence_artifact.id
            or resolution.artifact_version != expected_occurrence_artifact.version
        ):
            raise EditorialProposalAdoptionError(
                "proposal_base_stale", reason="resolution:artifact_id"
            )
        expected_resolution_sha = (
            resolution.record.synthesis_canonical_sha256
            if paragraph_enrichment_insertion
            else proposal.base_canonical_sha256
        )
        if resolution.canonical_sha256 != expected_resolution_sha:
            raise EditorialProposalAdoptionError(
                "proposal_base_stale", reason="resolution:canonical_sha256"
            )
        if extraction_artifact is None or current_synthesis_artifact is None:
            raise EditorialProposalAdoptionError("proposal_base_unavailable")
        if stage is ProductionArtifactStage.SYNTHESIS:
            if current_synthesis_artifact.id != artifact.id:
                raise EditorialProposalAdoptionError(
                    "proposal_base_stale", reason="pins:synthesis_artifact_id"
                )
            if resolution.record.synthesis_artifact_id != artifact.id:
                raise EditorialProposalAdoptionError(
                    "proposal_base_stale", reason="pins:synthesis_artifact_id"
                )
            if resolution.record.synthesis_version != artifact.version:
                raise EditorialProposalAdoptionError(
                    "proposal_base_stale", reason="pins:synthesis_version"
                )
            if resolution.record.synthesis_canonical_sha256 != proposal.base_canonical_sha256:
                raise EditorialProposalAdoptionError(
                    "proposal_base_stale", reason="pins:synthesis_canonical_sha256"
                )
            if resolution.record.publication_kind is None:
                raise EditorialProposalAdoptionError("publication_kind_unresolved")
        else:
            if resolution.record.synthesis_artifact_id != current_synthesis_artifact.id:
                raise EditorialProposalAdoptionError(
                    "proposal_base_stale", reason="pins:synthesis_artifact_id"
                )
            if resolution.record.synthesis_version != current_synthesis_artifact.version:
                raise EditorialProposalAdoptionError(
                    "proposal_base_stale", reason="pins:synthesis_version"
                )
            if resolution.record.synthesis_canonical_sha256 is None:
                raise EditorialProposalAdoptionError(
                    "proposal_base_stale", reason="pins:synthesis_canonical_sha256"
                )
            if resolution.record.enrichment_artifact_id != artifact.id:
                raise EditorialProposalAdoptionError(
                    "proposal_base_stale", reason="pins:enrichment_artifact_id"
                )
            if resolution.record.enrichment_version != artifact.version:
                raise EditorialProposalAdoptionError(
                    "proposal_base_stale", reason="pins:enrichment_version"
                )
            if resolution.record.enrichment_canonical_sha256 != proposal.base_canonical_sha256:
                raise EditorialProposalAdoptionError(
                    "proposal_base_stale", reason="pins:enrichment_canonical_sha256"
                )
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
            raise EditorialProposalAdoptionError(
                "proposal_base_stale", reason="snapshot:subject_version"
            )

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
                anchor_block_id=(
                    UUID(str(placement_data["anchor_block_id"]))
                    if placement_data.get("anchor_block_id") is not None
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
