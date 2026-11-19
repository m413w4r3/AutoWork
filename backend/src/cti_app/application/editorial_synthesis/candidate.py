"""Model-free construction and caller-owned persistence of synthesis candidates."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID

from cti_app.application.editorial_synthesis.patch_validation import WirePatch
from cti_app.application.persistence import ProductionUnitOfWork
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.application.production_stages import SynthesisService
from cti_app.application.production_synthesis import SynthesisEvidencePackV1
from cti_app.domain.production import (
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionInputSnapshot,
    SynthesisMode,
)
from cti_app.domain.production_editorial_revision import (
    TargetedEditorialRevisionConflictError,
    TargetedEditorialRevisionOperation,
    TargetedEditorialRevisionTarget,
)
from cti_app.domain.production_extraction import ProductionExtractionV1
from cti_app.domain.production_synthesis import (
    ProductionSynthesisV1,
    production_synthesis_from_json,
    production_synthesis_to_json,
)
from cti_app.domain.publication_kind import PublicationKind


class PatchApplicator(Protocol):
    """The shared deterministic, write-free patch application port."""

    def __call__(
        self,
        *,
        base_synthesis: ProductionSynthesisV1,
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
    ) -> tuple[ProductionSynthesisV1, dict[str, Any], str]: ...


@dataclass(frozen=True, slots=True)
class SynthesisCandidate:
    """A validated canonical payload, ready for a caller-owned commit."""

    base_artifact: ProductionArtifact
    base_canonical_sha256: str
    synthesis: ProductionSynthesisV1
    payload: dict[str, Any]
    canonical_bytes: bytes
    canonical_sha256: str
    before_hash: str


@dataclass(frozen=True, slots=True)
class SynthesisCandidateBlobReference:
    """Reference to a preuploaded canonical candidate blob."""

    blob_id: UUID


@dataclass(frozen=True, slots=True)
class SynthesisCandidatePersistence:
    """The legacy revision's artifact row and fence fields for one UOW write."""

    run_id: UUID
    subject_id: UUID
    input_hash: str
    extraction: ProductionExtractionV1
    snapshot: ProductionInputSnapshot
    raw_result: str | None
    model_run_id: UUID | None
    publication_kind: PublicationKind = PublicationKind.ARTICLE
    mode: SynthesisMode = SynthesisMode.FRESH
    model_policy_version: str = ""
    routing_policy_version: str = ""
    projection_hash: str | None = None
    metadata_extra: Mapping[str, Any] | None = None
    artifact_id: UUID | None = None
    expected_current_artifact_id: UUID | None = None
    expected_current_input_hash: str | None = None
    expected_current_canonical_sha256: str | None = None
    expected_routing_decision_id: UUID | None = None
    expected_routing_decision_hash: str | None = None
    stale_stages: frozenset[str] | None = None
    candidate_blob: SynthesisCandidateBlobReference | None = None


def build_candidate_from_validated_patch(
    *,
    base_artifact: ProductionArtifact,
    base_payload: dict[str, Any],
    base_synthesis: ProductionSynthesisV1,
    base_canonical_sha256: str,
    patch: WirePatch,
    target: TargetedEditorialRevisionTarget,
    operation: TargetedEditorialRevisionOperation,
    evidence_pack: SynthesisEvidencePackV1,
    extraction: ProductionExtractionV1,
    snapshot: ProductionInputSnapshot,
    apply_patch: PatchApplicator,
    publication_kind: PublicationKind | None = None,
    source_addition_document_ids: frozenset[UUID] = frozenset(),
    require_claim_scopes: bool = False,
) -> SynthesisCandidate:
    """Apply a validated patch and assemble exact canonical bytes without gateway or writes.

    The caller supplies the existing deterministic patch applicator so legacy
    revision and later proposal adoption share the same operation semantics.
    """
    if base_artifact.stage is not ProductionArtifactStage.SYNTHESIS:
        raise ValueError("synthesis_candidate_base_stage_invalid")
    if patch.refusal is not None:
        raise ValueError("synthesis_candidate_patch_refusal")
    if patch.base_artifact_hash != base_canonical_sha256:
        raise TargetedEditorialRevisionConflictError("The model patch references another base")

    synthesis, payload, before_hash = apply_patch(
        base_synthesis=base_synthesis,
        base_payload=base_payload,
        target=target,
        operation=operation,
        patch=patch,
        evidence_pack=evidence_pack,
        extraction=extraction,
        snapshot=snapshot,
        publication_kind=publication_kind,
        source_addition_document_ids=source_addition_document_ids,
        require_claim_scopes=require_claim_scopes,
    )
    if production_synthesis_to_json(synthesis) != payload:
        raise ValueError("synthesis_candidate_payload_inconsistent")

    canonical_bytes = ProductionArtifactStore.canonical_json_bytes(payload)
    canonical_sha256 = hashlib.sha256(canonical_bytes).hexdigest()
    round_trip = production_synthesis_from_json(json.loads(canonical_bytes))
    if round_trip != synthesis or production_synthesis_to_json(round_trip) != payload:
        raise ValueError("synthesis_candidate_round_trip_invalid")

    return SynthesisCandidate(
        base_artifact=base_artifact,
        base_canonical_sha256=base_canonical_sha256,
        synthesis=synthesis,
        payload=payload,
        canonical_bytes=canonical_bytes,
        canonical_sha256=canonical_sha256,
        before_hash=before_hash,
    )


async def persist_candidate(
    uow: ProductionUnitOfWork,
    *,
    writer: SynthesisService,
    candidate: SynthesisCandidate,
    persistence: SynthesisCandidatePersistence,
) -> ProductionArtifact:
    """Persist one candidate through the existing CAS writer in the supplied UOW."""
    base_artifact = candidate.base_artifact
    if (
        persistence.run_id != base_artifact.production_run_id
        or persistence.subject_id != base_artifact.subject_id
        or persistence.expected_current_artifact_id != base_artifact.id
        or persistence.expected_current_input_hash != base_artifact.input_hash
        or persistence.expected_current_canonical_sha256 != candidate.base_canonical_sha256
    ):
        raise TargetedEditorialRevisionConflictError("synthesis_candidate_base_fence_mismatch")

    store_kwargs: dict[str, Any] = {
        "run_id": persistence.run_id,
        "subject_id": persistence.subject_id,
        "input_hash": persistence.input_hash,
        "extraction": persistence.extraction,
        "snapshot": persistence.snapshot,
        "raw_result": persistence.raw_result,
        "model_run_id": persistence.model_run_id,
        "mode": persistence.mode,
        "publication_kind": persistence.publication_kind,
        "model_policy_version": persistence.model_policy_version,
        "routing_policy_version": persistence.routing_policy_version,
        "projection_hash": persistence.projection_hash,
        "metadata_extra": persistence.metadata_extra,
        "artifact_id": persistence.artifact_id,
        "expected_current_artifact_id": persistence.expected_current_artifact_id,
        "expected_current_input_hash": persistence.expected_current_input_hash,
        "expected_current_canonical_sha256": persistence.expected_current_canonical_sha256,
        "expected_routing_decision_id": persistence.expected_routing_decision_id,
        "expected_routing_decision_hash": persistence.expected_routing_decision_hash,
        "stale_stages": (
            set(persistence.stale_stages) if persistence.stale_stages is not None else None
        ),
    }
    if persistence.candidate_blob is not None:
        if (
            persistence.expected_current_artifact_id is None
            or persistence.expected_current_input_hash is None
            or persistence.expected_current_canonical_sha256 is None
        ):
            raise ValueError("synthesis_candidate_base_fence_required")
        store_kwargs["preuploaded_payload_ids"] = (
            None,
            persistence.candidate_blob.blob_id,
            None,
        )

    return await writer.store_synthesis_result(
        **store_kwargs,
        synthesis=candidate.synthesis,
        uow=uow,
    )
