"""Immutable contracts for additive source acquisition during editorial work."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from typing import Any
from uuid import UUID

from cti_app.domain.discovery import SourceRole, canonicalize_http_url
from cti_app.domain.production import (
    ExtractionProfile,
    ProductionArtifactStage,
    SourceAdditionExtractionIntent,
)
from cti_app.domain.production_extraction import (
    ExtractionProfileReasonCode,
    ExtractionReuseState,
)
from cti_app.domain.production_references import (
    PRODUCTION_SOURCE_CRITICALITY_POLICY_VERSION,
    ProductionEditorialRole,
    ProductionReferenceKind,
    ProductionReferenceTier,
    ProductionSourceCriticality,
    ProductionSourceCriticalityReason,
)
from cti_app.domain.production_synthesis import (
    EvidenceKind,
    ExtractionEvidenceRefV1,
    evidence_ref_sort_key,
)


class SourceAdditionState(StrEnum):
    CANDIDATE = "candidate"
    SELECTED = "selected"
    ARCHIVED = "archived"
    EXTRACTED = "extracted"
    READY = "ready"
    REFUSED = "refused"


class SourceAdditionOrigin(StrEnum):
    DISCOVERY = "discovery"
    OPERATOR_URL = "operator_url"
    MANUAL_ARCHIVE = "manual_archive"


_SHA256 = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True, kw_only=True)
class SourceAdditionArtifactFenceV1:
    stage: ProductionArtifactStage
    artifact_id: UUID
    version: int
    input_hash: str
    canonical_sha256: str

    def __post_init__(self) -> None:
        if not isinstance(self.stage, ProductionArtifactStage):
            raise ValueError("source_addition_fence_stage_invalid")
        if self.version < 1 or _SHA256.fullmatch(self.input_hash) is None:
            raise ValueError("source_addition_fence_invalid")
        if _SHA256.fullmatch(self.canonical_sha256) is None:
            raise ValueError("source_addition_fence_canonical_hash_invalid")


@dataclass(frozen=True, slots=True, kw_only=True)
class SourceAdditionArtifactProjectionV1:
    stage: ProductionArtifactStage
    artifact_id: UUID
    version: int
    input_hash: str
    canonical_blob_id: UUID
    canonical_sha256: str

    def __post_init__(self) -> None:
        if self.stage not in {
            ProductionArtifactStage.REFERENCES,
            ProductionArtifactStage.EXTRACTION,
            ProductionArtifactStage.RELEVANCE_PROJECTION,
        }:
            raise ValueError("source_addition_projection_stage_invalid")
        if self.version < 1 or _SHA256.fullmatch(self.input_hash) is None:
            raise ValueError("source_addition_projection_invalid")
        if _SHA256.fullmatch(self.canonical_sha256) is None:
            raise ValueError("source_addition_projection_hash_invalid")


@dataclass(frozen=True, slots=True, kw_only=True)
class SourceAdditionPromotionPlanV1:
    production_run_id: UUID
    subject_id: UUID
    pipeline_generation: int
    base_artifacts: tuple[SourceAdditionArtifactFenceV1, ...]
    projected_artifacts: tuple[SourceAdditionArtifactProjectionV1, ...]
    source_additions_hash: str

    def __post_init__(self) -> None:
        if self.pipeline_generation < 0:
            raise ValueError("source_addition_generation_invalid")
        if _SHA256.fullmatch(self.source_additions_hash) is None:
            raise ValueError("source_addition_manifest_hash_invalid")
        required = {
            ProductionArtifactStage.REFERENCES,
            ProductionArtifactStage.EXTRACTION,
            ProductionArtifactStage.RELEVANCE_PROJECTION,
        }
        if {item.stage for item in self.base_artifacts} != required:
            raise ValueError("source_addition_base_artifact_fences_incomplete")
        if {item.stage for item in self.projected_artifacts} != required:
            raise ValueError("source_addition_projected_artifacts_incomplete")
        if len(self.base_artifacts) != len(required) or len(self.projected_artifacts) != len(
            required
        ):
            raise ValueError("source_addition_artifact_stage_duplicate")
        object.__setattr__(
            self,
            "base_artifacts",
            tuple(sorted(self.base_artifacts, key=lambda item: item.stage.value)),
        )
        object.__setattr__(
            self,
            "projected_artifacts",
            tuple(sorted(self.projected_artifacts, key=lambda item: item.stage.value)),
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class SourceAdditionJournalEntryV1:
    addition: SourceAdditionV1
    version: int
    promotion_plan: SourceAdditionPromotionPlanV1 | None = None

    def __post_init__(self) -> None:
        if self.version < 1:
            raise ValueError("source_addition_journal_version_invalid")
        if (self.addition.state is SourceAdditionState.READY) != (self.promotion_plan is not None):
            raise ValueError("source_addition_ready_promotion_plan_mismatch")
        if self.promotion_plan is not None and (
            self.promotion_plan.production_run_id != self.addition.production_run_id
            or self.promotion_plan.subject_id != self.addition.subject_id
        ):
            raise ValueError("source_addition_promotion_identity_mismatch")


@dataclass(frozen=True, slots=True, kw_only=True)
class SourceAdditionV1:
    """One immutable snapshot in the source-addition state machine.

    Archive bodies remain in MinIO. This record contains only identities,
    hashes, policy decisions, and evidence handles needed to resume safely.
    Fields that are not known at candidate time are filled by later versions.
    """

    addition_id: UUID
    request_id: UUID
    production_run_id: UUID
    subject_id: UUID
    state: SourceAdditionState
    origin: SourceAdditionOrigin
    canonical_url: str
    final_url: str | None = None
    title: str | None = None
    publisher: str | None = None
    published_at: date | None = None
    collection_id: UUID | None = None
    source_document_id: UUID | None = None
    encoded_sha256: str | None = None
    decoded_sha256: str | None = None
    tier: ProductionReferenceTier = ProductionReferenceTier.TECHNICAL
    kind: ProductionReferenceKind = ProductionReferenceKind.TECHNICAL_RESOURCE
    role: SourceRole = SourceRole.UNKNOWN
    extraction_intent: SourceAdditionExtractionIntent = (
        SourceAdditionExtractionIntent.INDICATORS_ONLY
    )
    editorial_role: ProductionEditorialRole = ProductionEditorialRole.CONTEXT
    criticality: ProductionSourceCriticality | None = None
    criticality_reason: ProductionSourceCriticalityReason | None = None
    criticality_policy_version: str = PRODUCTION_SOURCE_CRITICALITY_POLICY_VERSION
    selection_criterion: str | None = None
    selection_criterion_version: str | None = None
    extraction_profile: ExtractionProfile | None = None
    profile_reason_code: ExtractionProfileReasonCode | None = None
    reuse_state: ExtractionReuseState | None = None
    extraction_profile_policy_version: str | None = None
    checkpoint_contract_version: str | None = None
    checkpoint_prompt_version: str | None = None
    checkpoint_parser_version: str | None = None
    checkpoint_verifier_version: str | None = None
    source_text_contract_version: str | None = None
    model_policy_version: str | None = None
    routing_policy_version: str | None = None
    collection_policy_snapshot_id: str | None = None
    selected_by: str | None = None
    checkpoint_id: UUID | None = None
    extraction_id: UUID | None = None
    model_run_id: UUID | None = None
    evidence_handles: tuple[ExtractionEvidenceRefV1, ...] = ()
    diagnostic_code: str | None = None
    diagnostic_details: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.state, SourceAdditionState):
            raise ValueError("source_addition_state_invalid")
        if not isinstance(self.origin, SourceAdditionOrigin):
            raise ValueError("source_addition_origin_invalid")
        canonical = canonicalize_http_url(self.canonical_url)
        if canonical != self.canonical_url:
            raise ValueError("source_addition_url_must_be_canonical")
        if self.final_url is not None and canonicalize_http_url(self.final_url) != self.final_url:
            raise ValueError("source_addition_final_url_must_be_canonical")
        for field_name in ("encoded_sha256", "decoded_sha256"):
            value = getattr(self, field_name)
            if value is not None and _SHA256.fullmatch(value) is None:
                raise ValueError(f"source_addition_{field_name}_invalid")
        if not isinstance(self.tier, ProductionReferenceTier):
            raise ValueError("source_addition_tier_invalid")
        if not isinstance(self.kind, ProductionReferenceKind):
            raise ValueError("source_addition_kind_invalid")
        if not isinstance(self.role, SourceRole):
            raise ValueError("source_addition_role_invalid")
        if not isinstance(self.extraction_intent, SourceAdditionExtractionIntent):
            raise ValueError("source_addition_extraction_intent_invalid")
        if self.editorial_role is not ProductionEditorialRole.CONTEXT:
            raise ValueError("source_addition_editorial_role_must_be_context")
        if self.kind is ProductionReferenceKind.PUBLICATION:
            if self.tier is not ProductionReferenceTier.SUPPORTING:
                raise ValueError("source_addition_publication_tier_must_be_supporting")
        elif self.tier is not ProductionReferenceTier.TECHNICAL:
            raise ValueError("source_addition_technical_tier_must_be_technical")
        if (self.criticality is None) != (self.criticality_reason is None):
            raise ValueError("source_addition_criticality_pair_required")
        if not self.criticality_policy_version.strip():
            raise ValueError("source_addition_criticality_policy_required")
        if self.published_at is not None and (
            not isinstance(self.published_at, date) or hasattr(self.published_at, "hour")
        ):
            raise ValueError("source_addition_publication_date_invalid")
        if not isinstance(self.evidence_handles, tuple) or any(
            not isinstance(handle, ExtractionEvidenceRefV1) for handle in self.evidence_handles
        ):
            raise ValueError("source_addition_evidence_handles_invalid")
        handles = tuple(sorted(set(self.evidence_handles), key=evidence_ref_sort_key))
        if len(handles) != len(self.evidence_handles):
            raise ValueError("source_addition_evidence_handles_duplicate")
        if self.source_document_id is not None and any(
            handle.source_document_id != self.source_document_id for handle in handles
        ):
            raise ValueError("source_addition_handle_document_mismatch")
        object.__setattr__(self, "evidence_handles", handles)
        if self.state in {
            SourceAdditionState.SELECTED,
            SourceAdditionState.ARCHIVED,
            SourceAdditionState.EXTRACTED,
            SourceAdditionState.READY,
        }:
            if not self.selection_criterion or not self.selection_criterion.strip():
                raise ValueError("source_addition_selection_criterion_required")
            if not self.selection_criterion_version or not self.selection_criterion_version.strip():
                raise ValueError("source_addition_selection_version_required")
            if not self.selected_by or not self.selected_by.strip():
                raise ValueError("source_addition_selector_required")
        if self.state in {
            SourceAdditionState.ARCHIVED,
            SourceAdditionState.EXTRACTED,
            SourceAdditionState.READY,
        }:
            if any(
                value is None
                for value in (
                    self.final_url,
                    self.collection_id,
                    self.source_document_id,
                    self.encoded_sha256,
                    self.decoded_sha256,
                    self.collection_policy_snapshot_id,
                )
            ):
                raise ValueError("source_addition_archive_receipt_incomplete")
        if self.state in {SourceAdditionState.EXTRACTED, SourceAdditionState.READY}:
            if any(
                value is None
                for value in (
                    self.extraction_profile,
                    self.profile_reason_code,
                    self.extraction_profile_policy_version,
                    self.checkpoint_contract_version,
                    self.checkpoint_prompt_version,
                    self.checkpoint_parser_version,
                    self.checkpoint_verifier_version,
                    self.source_text_contract_version,
                    self.model_policy_version,
                    self.routing_policy_version,
                    self.checkpoint_id,
                    self.extraction_id,
                    self.model_run_id,
                    self.reuse_state,
                )
            ):
                raise ValueError("source_addition_extraction_lineage_incomplete")
        if self.state is SourceAdditionState.READY and not self.evidence_handles:
            raise ValueError("source_addition_ready_requires_evidence_handles")
        if self.state is SourceAdditionState.REFUSED and not self.diagnostic_code:
            raise ValueError("source_addition_refusal_requires_diagnostic")
        for field_name in (
            "selection_criterion",
            "selection_criterion_version",
            "selected_by",
            "collection_policy_snapshot_id",
            "extraction_profile_policy_version",
            "diagnostic_code",
        ):
            value = getattr(self, field_name)
            if value is not None:
                normalized = value.strip()
                if not normalized:
                    raise ValueError(f"source_addition_{field_name}_empty")
                object.__setattr__(self, field_name, normalized)
        details = tuple(sorted(self.diagnostic_details))
        if any(not key.strip() or not value.strip() for key, value in details):
            raise ValueError("source_addition_diagnostic_detail_invalid")
        object.__setattr__(self, "diagnostic_details", details)


def source_addition_source_key(canonical_url: str) -> str:
    canonical = canonicalize_http_url(canonical_url)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def source_addition_manifest(additions: Sequence[SourceAdditionV1]) -> tuple[dict[str, Any], ...]:
    """Return the stable, metadata-only manifest for archived additions."""
    ready = tuple(
        item
        for item in additions
        if item.state in {SourceAdditionState.EXTRACTED, SourceAdditionState.READY}
    )
    ordered = sorted(
        ready,
        key=lambda item: (
            item.canonical_url,
            item.decoded_sha256 or "",
            str(item.collection_id or ""),
            str(item.source_document_id or ""),
            str(item.addition_id),
        ),
    )
    return tuple(
        {
            "addition_id": str(item.addition_id),
            "url": item.canonical_url,
            "final_url": item.final_url,
            "encoded_sha256": item.encoded_sha256,
            "decoded_sha256": item.decoded_sha256,
            "collection_id": str(item.collection_id) if item.collection_id else None,
            "source_document_id": (
                str(item.source_document_id) if item.source_document_id else None
            ),
            "checkpoint_id": str(item.checkpoint_id) if item.checkpoint_id else None,
            "extraction_id": str(item.extraction_id) if item.extraction_id else None,
            "model_run_id": str(item.model_run_id) if item.model_run_id else None,
            "evidence_handles": [
                {
                    "source_document_id": str(handle.source_document_id),
                    "kind": handle.kind.value,
                    "evidence_key": handle.evidence_key,
                }
                for handle in item.evidence_handles
            ],
            "origin": item.origin.value,
            "title": item.title,
            "publisher": item.publisher,
            "published_at": item.published_at.isoformat() if item.published_at else None,
            "tier": item.tier.value,
            "kind": item.kind.value,
            "role": item.role.value,
            "extraction_intent": item.extraction_intent.value,
            "editorial_role": item.editorial_role.value,
            "criticality": item.criticality.value if item.criticality else None,
            "criticality_reason": (
                item.criticality_reason.value if item.criticality_reason else None
            ),
            "criticality_policy_version": item.criticality_policy_version,
            "selection_criterion": item.selection_criterion,
            "selection_criterion_version": item.selection_criterion_version,
            "selected_by": item.selected_by,
            "extraction_profile": item.extraction_profile.value
            if item.extraction_profile
            else None,
            "profile_reason_code": item.profile_reason_code.value
            if item.profile_reason_code
            else None,
            "reuse_state": item.reuse_state.value if item.reuse_state else None,
            "extraction_profile_policy_version": item.extraction_profile_policy_version,
            "checkpoint_contract_version": item.checkpoint_contract_version,
            "checkpoint_prompt_version": item.checkpoint_prompt_version,
            "checkpoint_parser_version": item.checkpoint_parser_version,
            "checkpoint_verifier_version": item.checkpoint_verifier_version,
            "source_text_contract_version": item.source_text_contract_version,
            "model_policy_version": item.model_policy_version,
            "routing_policy_version": item.routing_policy_version,
            "collection_policy_snapshot_id": item.collection_policy_snapshot_id,
        }
        for item in ordered
    )


def source_additions_hash(additions: Sequence[SourceAdditionV1]) -> str:
    encoded = json.dumps(
        source_addition_manifest(additions),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def source_addition_to_json(addition: SourceAdditionV1) -> dict[str, Any]:
    return {
        "addition_id": str(addition.addition_id),
        "request_id": str(addition.request_id),
        "production_run_id": str(addition.production_run_id),
        "subject_id": str(addition.subject_id),
        "state": addition.state.value,
        "origin": addition.origin.value,
        "canonical_url": addition.canonical_url,
        "final_url": addition.final_url,
        "title": addition.title,
        "publisher": addition.publisher,
        "published_at": addition.published_at.isoformat() if addition.published_at else None,
        "collection_id": str(addition.collection_id) if addition.collection_id else None,
        "source_document_id": (
            str(addition.source_document_id) if addition.source_document_id else None
        ),
        "encoded_sha256": addition.encoded_sha256,
        "decoded_sha256": addition.decoded_sha256,
        "tier": addition.tier.value,
        "kind": addition.kind.value,
        "role": addition.role.value,
        "extraction_intent": addition.extraction_intent.value,
        "editorial_role": addition.editorial_role.value,
        "criticality": addition.criticality.value if addition.criticality else None,
        "criticality_reason": (
            addition.criticality_reason.value if addition.criticality_reason else None
        ),
        "criticality_policy_version": addition.criticality_policy_version,
        "selection_criterion": addition.selection_criterion,
        "selection_criterion_version": addition.selection_criterion_version,
        "extraction_profile": addition.extraction_profile.value
        if addition.extraction_profile
        else None,
        "profile_reason_code": addition.profile_reason_code.value
        if addition.profile_reason_code
        else None,
        "reuse_state": addition.reuse_state.value if addition.reuse_state else None,
        "extraction_profile_policy_version": addition.extraction_profile_policy_version,
        "checkpoint_contract_version": addition.checkpoint_contract_version,
        "checkpoint_prompt_version": addition.checkpoint_prompt_version,
        "checkpoint_parser_version": addition.checkpoint_parser_version,
        "checkpoint_verifier_version": addition.checkpoint_verifier_version,
        "source_text_contract_version": addition.source_text_contract_version,
        "model_policy_version": addition.model_policy_version,
        "routing_policy_version": addition.routing_policy_version,
        "collection_policy_snapshot_id": addition.collection_policy_snapshot_id,
        "selected_by": addition.selected_by,
        "checkpoint_id": str(addition.checkpoint_id) if addition.checkpoint_id else None,
        "extraction_id": str(addition.extraction_id) if addition.extraction_id else None,
        "model_run_id": str(addition.model_run_id) if addition.model_run_id else None,
        "evidence_handles": [
            {
                "source_document_id": str(handle.source_document_id),
                "kind": handle.kind.value,
                "evidence_key": handle.evidence_key,
            }
            for handle in addition.evidence_handles
        ],
        "diagnostic_code": addition.diagnostic_code,
        "diagnostic_details": [list(item) for item in addition.diagnostic_details],
    }


def source_addition_from_json(payload: Any) -> SourceAdditionV1:
    if not isinstance(payload, dict):
        raise ValueError("source_addition_record_invalid")

    def optional_uuid(key: str) -> UUID | None:
        value = payload.get(key)
        return UUID(value) if isinstance(value, str) else None

    def optional_enum(enum_type: type[StrEnum], key: str) -> Any:
        value = payload.get(key)
        return enum_type(value) if value is not None else None

    raw_handles = payload.get("evidence_handles", [])
    raw_details = payload.get("diagnostic_details", [])
    if not isinstance(raw_handles, list) or not isinstance(raw_details, list):
        raise ValueError("source_addition_record_invalid")
    handles = tuple(
        ExtractionEvidenceRefV1(
            source_document_id=UUID(str(item["source_document_id"])),
            kind=EvidenceKind(str(item["kind"])),
            evidence_key=str(item["evidence_key"]),
        )
        for item in raw_handles
        if isinstance(item, dict)
    )
    if len(handles) != len(raw_handles):
        raise ValueError("source_addition_evidence_handles_invalid")
    details = tuple(
        (str(item[0]), str(item[1]))
        for item in raw_details
        if isinstance(item, list) and len(item) == 2
    )
    if len(details) != len(raw_details):
        raise ValueError("source_addition_diagnostic_details_invalid")
    date_value = payload.get("published_at")
    return SourceAdditionV1(
        addition_id=UUID(str(payload["addition_id"])),
        request_id=UUID(str(payload["request_id"])),
        production_run_id=UUID(str(payload["production_run_id"])),
        subject_id=UUID(str(payload["subject_id"])),
        state=SourceAdditionState(str(payload["state"])),
        origin=SourceAdditionOrigin(str(payload["origin"])),
        canonical_url=str(payload["canonical_url"]),
        final_url=payload.get("final_url"),
        title=payload.get("title"),
        publisher=payload.get("publisher"),
        published_at=date.fromisoformat(date_value) if isinstance(date_value, str) else None,
        collection_id=optional_uuid("collection_id"),
        source_document_id=optional_uuid("source_document_id"),
        encoded_sha256=payload.get("encoded_sha256"),
        decoded_sha256=payload.get("decoded_sha256"),
        tier=ProductionReferenceTier(str(payload["tier"])),
        kind=ProductionReferenceKind(str(payload["kind"])),
        role=SourceRole(str(payload["role"])),
        extraction_intent=SourceAdditionExtractionIntent(
            str(
                payload.get(
                    "extraction_intent",
                    SourceAdditionExtractionIntent.INDICATORS_ONLY.value,
                )
            )
        ),
        editorial_role=ProductionEditorialRole(str(payload["editorial_role"])),
        criticality=optional_enum(ProductionSourceCriticality, "criticality"),
        criticality_reason=optional_enum(ProductionSourceCriticalityReason, "criticality_reason"),
        criticality_policy_version=str(payload["criticality_policy_version"]),
        selection_criterion=payload.get("selection_criterion"),
        selection_criterion_version=payload.get("selection_criterion_version"),
        extraction_profile=optional_enum(ExtractionProfile, "extraction_profile"),
        profile_reason_code=optional_enum(ExtractionProfileReasonCode, "profile_reason_code"),
        reuse_state=optional_enum(ExtractionReuseState, "reuse_state"),
        extraction_profile_policy_version=payload.get("extraction_profile_policy_version"),
        checkpoint_contract_version=payload.get("checkpoint_contract_version"),
        checkpoint_prompt_version=payload.get("checkpoint_prompt_version"),
        checkpoint_parser_version=payload.get("checkpoint_parser_version"),
        checkpoint_verifier_version=payload.get("checkpoint_verifier_version"),
        source_text_contract_version=payload.get("source_text_contract_version"),
        model_policy_version=payload.get("model_policy_version"),
        routing_policy_version=payload.get("routing_policy_version"),
        collection_policy_snapshot_id=payload.get("collection_policy_snapshot_id"),
        selected_by=payload.get("selected_by"),
        checkpoint_id=optional_uuid("checkpoint_id"),
        extraction_id=optional_uuid("extraction_id"),
        model_run_id=optional_uuid("model_run_id"),
        evidence_handles=handles,
        diagnostic_code=payload.get("diagnostic_code"),
        diagnostic_details=details,
    )


def promotion_plan_to_json(plan: SourceAdditionPromotionPlanV1 | None) -> dict[str, Any] | None:
    if plan is None:
        return None
    return {
        "production_run_id": str(plan.production_run_id),
        "subject_id": str(plan.subject_id),
        "pipeline_generation": plan.pipeline_generation,
        "source_additions_hash": plan.source_additions_hash,
        "base_artifacts": [
            {
                "stage": item.stage.value,
                "artifact_id": str(item.artifact_id),
                "version": item.version,
                "input_hash": item.input_hash,
                "canonical_sha256": item.canonical_sha256,
            }
            for item in plan.base_artifacts
        ],
        "projected_artifacts": [
            {
                "stage": item.stage.value,
                "artifact_id": str(item.artifact_id),
                "version": item.version,
                "input_hash": item.input_hash,
                "canonical_blob_id": str(item.canonical_blob_id),
                "canonical_sha256": item.canonical_sha256,
            }
            for item in plan.projected_artifacts
        ],
    }


def promotion_plan_from_json(payload: Any) -> SourceAdditionPromotionPlanV1 | None:
    if payload is None:
        return None
    if not isinstance(payload, dict):
        raise ValueError("source_addition_promotion_plan_invalid")
    bases = payload.get("base_artifacts")
    projected = payload.get("projected_artifacts")
    if not isinstance(bases, list) or not isinstance(projected, list):
        raise ValueError("source_addition_promotion_plan_invalid")
    return SourceAdditionPromotionPlanV1(
        production_run_id=UUID(str(payload["production_run_id"])),
        subject_id=UUID(str(payload["subject_id"])),
        pipeline_generation=int(payload["pipeline_generation"]),
        source_additions_hash=str(payload["source_additions_hash"]),
        base_artifacts=tuple(
            SourceAdditionArtifactFenceV1(
                stage=ProductionArtifactStage(str(item["stage"])),
                artifact_id=UUID(str(item["artifact_id"])),
                version=int(item["version"]),
                input_hash=str(item["input_hash"]),
                canonical_sha256=str(item["canonical_sha256"]),
            )
            for item in bases
            if isinstance(item, dict)
        ),
        projected_artifacts=tuple(
            SourceAdditionArtifactProjectionV1(
                stage=ProductionArtifactStage(str(item["stage"])),
                artifact_id=UUID(str(item["artifact_id"])),
                version=int(item["version"]),
                input_hash=str(item["input_hash"]),
                canonical_blob_id=UUID(str(item["canonical_blob_id"])),
                canonical_sha256=str(item["canonical_sha256"]),
            )
            for item in projected
            if isinstance(item, dict)
        ),
    )
