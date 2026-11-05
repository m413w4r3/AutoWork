"""Compute and durably adopt the deterministic publication route for a run."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from cti_app.application.persistence import ProductionUnitOfWorkFactory
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.domain.production import ProductionArtifact, ProductionArtifactStage
from cti_app.domain.production_extraction import (
    production_extraction_from_json,
    production_extraction_hashes,
)
from cti_app.domain.production_references import ProductionSourceCriticality
from cti_app.domain.production_relevance import (
    RELEVANCE_PROJECTION_SCHEMA_VERSION,
    relevance_projection_from_json,
)
from cti_app.domain.production_routing import (
    ProductionRoutingArtifactInputV1,
    ProductionRoutingDecisionV1,
    RoutingCriticalSourceDiagnosticV1,
    RoutingReasonCode,
    decide_publication_routing,
)
from cti_app.infrastructure.legitimate_services import load_legitimate_services


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
        decision = decide_publication_routing(
            production_run_id=run.id,
            subject_id=run.subject_id,
            pipeline_generation=run.pipeline_generation,
            extraction_input=extraction_input,
            relevance_input=relevance_input,
            legitimate_services=load_legitimate_services(),
            decided_at=now,
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
    if stored_projection_hash is not None and stored_projection_hash != projection.projection_hash:
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
                )
            )
    except (KeyError, TypeError, ValueError):
        return (), RoutingReasonCode.PROVENANCE_MISMATCH
    return tuple(diagnostics), None
