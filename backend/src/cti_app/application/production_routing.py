"""Compute and durably adopt the deterministic publication route for a run."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from cti_app.application.persistence import ProductionUnitOfWork, ProductionUnitOfWorkFactory
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.application.production_brief_synthesis import (
    brief_core_primary_sources_without_source_verified_date,
    brief_timeline_from_extraction,
)
from cti_app.domain.production import (
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionInputSnapshot,
    ProductionRun,
)
from cti_app.domain.production_extraction import (
    ProductionExtractionV1,
    production_extraction_from_json,
    production_extraction_hashes,
)
from cti_app.domain.production_references import (
    ProductionEditorialRole,
    ProductionSourceCriticality,
)
from cti_app.domain.production_relevance import (
    RELEVANCE_PROJECTION_SCHEMA_VERSION,
    RelevanceProjectionV1,
    relevance_projection_from_json,
)
from cti_app.domain.production_routing import (
    BriefRoutingFeasibilityV1,
    ProductionRoutingArtifactInputV1,
    ProductionRoutingDecisionV1,
    RoutingCriticalSourceDiagnosticV1,
    RoutingReasonCode,
    RoutingStatus,
    decide_publication_routing,
)
from cti_app.infrastructure.legitimate_services import load_legitimate_services


async def get_adopted_production_routing_decision(
    *, uow: ProductionUnitOfWork, run: ProductionRun
) -> ProductionRoutingDecisionV1 | None:
    """Load the valid decided route currently adopted by this production run."""
    if run.routing_decision_id is None:
        return None
    decision = await uow.production_routing_decisions.get(run.routing_decision_id)
    if (
        decision is None
        or decision.status is not RoutingStatus.DECIDED
        or decision.publication_kind is None
        or decision.production_run_id != run.id
        or decision.pipeline_generation != run.pipeline_generation
    ):
        return None
    return decision


def compute_brief_routing_feasibility(
    *,
    extraction: ProductionExtractionV1,
    projection: RelevanceProjectionV1,
    snapshot: ProductionInputSnapshot | None,
) -> BriefRoutingFeasibilityV1:
    """Run the same pre-model chronology predicate used by brief synthesis."""
    from cti_app.application.production_synthesis import build_synthesis_timeline

    if snapshot is not None and not _snapshot_matches_extraction(snapshot, extraction):
        raise ValueError("production input snapshot does not match canonical extraction")
    candidates = build_synthesis_timeline(extraction)
    timeline = brief_timeline_from_extraction(
        extraction,
        candidates,
        snapshot=snapshot,
        projection=projection,
    )
    return BriefRoutingFeasibilityV1(
        dated_entry_count=len(timeline or ()),
        reason=None if timeline else "brief_chronology_unavailable",
        missing_source_urls=brief_core_primary_sources_without_source_verified_date(extraction),
        needs=(() if timeline else ("dated source acquisition or an eligible publication date",)),
    )


def _snapshot_matches_extraction(
    snapshot: ProductionInputSnapshot, extraction: ProductionExtractionV1
) -> bool:
    return (
        snapshot.subject_id == extraction.subject_id
        and snapshot.input_hash == extraction.production_input_hash
    )


async def decide_and_adopt_production_routing(
    *,
    uow_factory: ProductionUnitOfWorkFactory,
    artifact_store: ProductionArtifactStore,
    production_run_id: UUID,
    expected_version: int,
    decided_at: datetime | None = None,
) -> ProductionRoutingDecisionV1:
    """Load exact current artifacts, decide, append, and adopt in one UOW."""
    now = decided_at or datetime.now(UTC)
    async with uow_factory() as uow:
        run = await uow.production_runs.get(production_run_id)
        if run is None:
            raise ValueError("production routing run does not exist")
        extraction_input, diagnostics = await _load_extraction_input(
            uow=uow,
            artifact_store=artifact_store,
            run_id=run.id,
        )
        relevance_input = await _load_relevance_input(
            uow=uow,
            artifact_store=artifact_store,
            run_id=run.id,
        )
        snapshot = await uow.production_input_snapshots.get_by_run(run.id)
        snapshot_provenance_mismatch = False
        if snapshot is not None and (
            not isinstance(snapshot, ProductionInputSnapshot)
            or snapshot.production_run_id != run.id
            or snapshot.subject_id != run.subject_id
        ):
            snapshot_provenance_mismatch = True
            snapshot = None
        if (
            isinstance(snapshot, ProductionInputSnapshot)
            and isinstance(extraction_input.payload, ProductionExtractionV1)
            and not _snapshot_matches_extraction(snapshot, extraction_input.payload)
        ):
            snapshot_provenance_mismatch = True
            snapshot = None
        if snapshot_provenance_mismatch and extraction_input.validation_error is None:
            extraction_input = replace(
                extraction_input,
                validation_error=RoutingReasonCode.PROVENANCE_MISMATCH,
            )
        if isinstance(extraction_input.payload, ProductionExtractionV1) and isinstance(
            relevance_input.payload, RelevanceProjectionV1
        ):
            if snapshot_provenance_mismatch:
                brief_feasibility = BriefRoutingFeasibilityV1(
                    dated_entry_count=0,
                    reason="brief_feasibility_inputs_unavailable",
                    needs=("a production input snapshot matching the canonical extraction",),
                )
            else:
                try:
                    brief_feasibility = compute_brief_routing_feasibility(
                        extraction=extraction_input.payload,
                        projection=relevance_input.payload,
                        snapshot=snapshot,
                    )
                except (KeyError, TypeError, ValueError):
                    brief_feasibility = BriefRoutingFeasibilityV1(
                        dated_entry_count=0,
                        reason="brief_feasibility_inputs_unavailable",
                        needs=("verified extraction and relevance artifacts",),
                    )
        else:
            brief_feasibility = BriefRoutingFeasibilityV1(
                dated_entry_count=0,
                reason="brief_feasibility_inputs_unavailable",
                needs=("verified extraction and relevance artifacts",),
            )
        decision = decide_publication_routing(
            production_run_id=run.id,
            subject_id=run.subject_id,
            pipeline_generation=run.pipeline_generation,
            extraction_input=extraction_input,
            relevance_input=relevance_input,
            legitimate_services=load_legitimate_services(),
            decided_at=now,
            brief_feasibility=brief_feasibility,
            critical_source_diagnostics=diagnostics,
        )
        stored = await uow.production_routing_decisions.append_and_adopt(
            decision,
            expected_version=expected_version,
        )
        await uow.commit()
        return stored


async def _load_artifact_payload(
    *,
    uow: Any,
    artifact_store: ProductionArtifactStore,
    run_id: UUID,
    stage: ProductionArtifactStage,
) -> tuple[ProductionArtifact | None, dict[str, Any] | None, RoutingReasonCode | None]:
    artifact = await uow.production_artifacts.get_current(run_id, stage.value)
    if artifact is None:
        return None, None, None
    if artifact.canonical_blob_id is None:
        return (
            artifact,
            None,
            (
                RoutingReasonCode.INVALID_EXTRACTION_ARTIFACT
                if stage is ProductionArtifactStage.EXTRACTION
                else RoutingReasonCode.INVALID_RELEVANCE_ARTIFACT
            ),
        )
    blob = await uow.blobs.get(artifact.canonical_blob_id)
    if blob is None:
        return (
            artifact,
            None,
            (
                RoutingReasonCode.INVALID_EXTRACTION_ARTIFACT
                if stage is ProductionArtifactStage.EXTRACTION
                else RoutingReasonCode.INVALID_RELEVANCE_ARTIFACT
            ),
        )
    try:
        raw = await artifact_store.read_bytes(artifact.canonical_blob_id)
        if hashlib.sha256(raw).hexdigest() != blob.descriptor.sha256:
            raise ValueError("canonical artifact bytes do not match the blob SHA-256")
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("canonical artifact payload must be an object")
    except Exception:
        return (
            artifact,
            None,
            (
                RoutingReasonCode.INVALID_EXTRACTION_ARTIFACT
                if stage is ProductionArtifactStage.EXTRACTION
                else RoutingReasonCode.INVALID_RELEVANCE_ARTIFACT
            ),
        )
    return artifact, payload, None


async def _load_extraction_input(
    *,
    uow: Any,
    artifact_store: ProductionArtifactStore,
    run_id: UUID,
) -> tuple[ProductionRoutingArtifactInputV1, tuple[RoutingCriticalSourceDiagnosticV1, ...]]:
    artifact, payload, error = await _load_artifact_payload(
        uow=uow,
        artifact_store=artifact_store,
        run_id=run_id,
        stage=ProductionArtifactStage.EXTRACTION,
    )
    if artifact is None or payload is None:
        return ProductionRoutingArtifactInputV1(artifact, None, None, error), ()
    try:
        extraction = production_extraction_from_json(payload)
    except (KeyError, TypeError, ValueError):
        return (
            ProductionRoutingArtifactInputV1(
                artifact,
                None,
                None,
                RoutingReasonCode.INVALID_EXTRACTION_ARTIFACT,
            ),
            (),
        )
    diagnostics, diagnostics_error = _source_loss_diagnostics(artifact)
    artifact_hash = production_extraction_hashes(extraction)[0]
    return (
        ProductionRoutingArtifactInputV1(
            artifact,
            artifact_hash,
            extraction,
            diagnostics_error or error,
        ),
        diagnostics,
    )


async def _load_relevance_input(
    *,
    uow: Any,
    artifact_store: ProductionArtifactStore,
    run_id: UUID,
) -> ProductionRoutingArtifactInputV1:
    artifact, payload, error = await _load_artifact_payload(
        uow=uow,
        artifact_store=artifact_store,
        run_id=run_id,
        stage=ProductionArtifactStage.RELEVANCE_PROJECTION,
    )
    if artifact is None or payload is None:
        return ProductionRoutingArtifactInputV1(artifact, None, None, error)
    try:
        projection = relevance_projection_from_json(payload)
    except (KeyError, TypeError, ValueError):
        reason = (
            RoutingReasonCode.INCOMPLETE_PROJECTION
            if _projection_has_incomplete_coverage(payload)
            else RoutingReasonCode.INVALID_RELEVANCE_ARTIFACT
        )
        return ProductionRoutingArtifactInputV1(
            artifact,
            None,
            None,
            reason,
        )
    stored_projection_hash = artifact.metadata.get("projection_hash")
    if (
        stored_projection_hash is not None and stored_projection_hash != projection.projection_hash
    ) or artifact.input_hash != projection.input_hash:
        error = RoutingReasonCode.PROVENANCE_MISMATCH
    return ProductionRoutingArtifactInputV1(
        artifact,
        projection.projection_hash,
        projection,
        error,
    )


def _projection_has_incomplete_coverage(payload: dict[str, Any]) -> bool:
    """Recognize an omitted/duplicated classification before schema parsing hides it."""
    refs = payload.get("extraction_evidence_refs")
    classifications = payload.get("classifications")
    if not isinstance(refs, list) or not isinstance(classifications, list):
        return False
    if len(refs) != len(classifications):
        return True
    if payload.get("schema_version") != RELEVANCE_PROJECTION_SCHEMA_VERSION:
        return False
    indexes = [item.get("evidence_ref_index") for item in classifications if isinstance(item, dict)]
    if len(indexes) != len(classifications) or any(type(index) is not int for index in indexes):
        return False
    return set(indexes) != set(range(len(refs)))


def _source_loss_diagnostics(
    artifact: ProductionArtifact,
) -> tuple[tuple[RoutingCriticalSourceDiagnosticV1, ...], RoutingReasonCode | None]:
    verification = artifact.metadata.get("deterministic_verification", {})
    if not isinstance(verification, dict):
        return (), RoutingReasonCode.PROVENANCE_MISMATCH
    raw_diagnostics = verification.get("source_loss_diagnostics", [])
    if not isinstance(raw_diagnostics, list):
        return (), RoutingReasonCode.PROVENANCE_MISMATCH
    diagnostics: list[RoutingCriticalSourceDiagnosticV1] = []
    try:
        for item in raw_diagnostics:
            if not isinstance(item, dict):
                raise ValueError("source loss diagnostic must be an object")
            diagnostics.append(
                RoutingCriticalSourceDiagnosticV1(
                    source_id=item.get("source_id"),
                    source_url=item["source_url"],
                    failure_code=item["failure_code"],
                    tier=item["tier"],
                    criticality=ProductionSourceCriticality(item["source_criticality"]),
                    criticality_reason=item["criticality_reason"],
                    policy_version=item["criticality_policy_version"],
                    editorial_role=(
                        ProductionEditorialRole(item["editorial_role"])
                        if item.get("editorial_role") is not None
                        else None
                    ),
                )
            )
    except (KeyError, TypeError, ValueError):
        return (), RoutingReasonCode.PROVENANCE_MISMATCH
    return tuple(diagnostics), None
