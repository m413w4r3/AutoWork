"""Stable production-repair identities, evidence packs and decision services."""

from __future__ import annotations

import hashlib
import inspect
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from time import perf_counter
from typing import Any, cast
from uuid import NAMESPACE_URL, UUID, uuid5

from cti_app.application.diagnostics import DiagnosticsLog
from cti_app.application.extraction import _html_encoding, parse_document
from cti_app.application.persistence import ProductionUnitOfWorkFactory
from cti_app.application.production_artifact_store import (
    MAX_REPAIR_EVIDENCE_BYTES,
    REPAIR_CORRECTION_BUCKET,
    ProductionArtifactStore,
)
from cti_app.application.production_artifact_verification import (
    ARTIFACT_VERIFIER_VERSION,
    Q2ProposalSubmission,
    verify_q2_proposals,
)
from cti_app.application.production_normalization import canonical_indicator_key
from cti_app.application.production_parsers import (
    Q2_EXTRACTION_CONTRACT_VERSION,
    Q2_MARKDOWN_PARSER_VERSION,
    DisplayPolicy,
    ExtractionItem,
    IndicatorProvenance,
    IndicatorStatus,
    Q2ArtifactProposal,
    Q2RuleProposal,
    Q2SourceOutput,
    ReferenceReport,
    TechnicalExtraction,
    parse_reference_report,
    reconcile_reference_report_with_archives,
    reference_report_from_json,
    reference_report_to_json,
    technical_extraction_from_json,
    technical_extraction_to_json,
)
from cti_app.application.production_prompts import (
    EXTRACTION_PROMPT_VERSION_BY_PROFILE,
    IOC_RULES_BATCH_PROMPT_VERSION,
)
from cti_app.application.production_q2_batch import Q2_BATCH_PARSER_VERSION
from cti_app.application.production_repair_payloads import (
    ProductionRepairPayloadResolver,
    RepairPayloadOrigin,
)
from cti_app.application.production_source_evidence import (
    SourceEvidenceDocument,
    SourceEvidenceSpan,
    SourceEvidenceSpanKind,
    source_evidence_context_for_artifact,
    source_evidence_context_for_rule,
    source_evidence_document_from_html,
    verify_ioc_rules_output_against_source,
)
from cti_app.application.production_stages import (
    ExtractionService,
    ProductionQAService,
    PublicationAssemblyService,
    compute_input_hash,
)
from cti_app.domain.collection import CollectionState, DetectedMimeType, SourceOriginKind
from cti_app.domain.discovery import canonicalize_http_url
from cti_app.domain.editions import EditionAuditEvent, EditionStatus
from cti_app.domain.production import (
    PUBLICATION_REBUILD_REQUIRED_ERROR_CODE,
    DetectionRule,
    DetectionRuleType,
    ExtractionProfile,
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionArtifactStatus,
    ProductionDerivedOutput,
    ProductionEvidenceBasis,
    ProductionReconciliationRequiredError,
    ProductionRepairAction,
    ProductionRepairCorrection,
    ProductionRepairDecision,
    ProductionRepairImpact,
    ProductionRepairImpactKind,
    ProductionRepairIssueKind,
    ProductionRepairVerificationState,
    RepairApplicationStage,
    RepairDecisionApplicationState,
    RepairIssueExecutionState,
    RepairRemediation,
    SubjectProductionStage,
    SubjectProductionStatus,
    SupplementalSourceRepairState,
)
from cti_app.domain.publication import ArtifactType, is_publication_ioc_artifact_type

REPAIR_EVIDENCE_SCHEMA_VERSION = "1"
REPAIR_PLANNER_VERSION = "33.1"
MAX_REPAIR_PREVIEW_CHARS = 512
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def production_repair_correction_identity(
    *,
    original_repair_key: str,
    artifact_type: str,
    source_id: str,
    source_url: str,
    replacement_value_sha256: str,
    verification_state: ProductionRepairVerificationState | str,
) -> UUID:
    """Return a stable correction id independent of run/fence/version data."""
    state = ProductionRepairVerificationState(verification_state)
    payload = {
        "version": "1",
        "original_repair_key": original_repair_key,
        "artifact_type": artifact_type,
        "source_id": source_id,
        "source_url": canonicalize_http_url(source_url),
        "replacement_value_sha256": replacement_value_sha256,
        "verification_state": state.value,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return uuid5(NAMESPACE_URL, f"cti-production-repair-correction:{encoded}")


def _repair_kind(value: ProductionRepairIssueKind | str) -> ProductionRepairIssueKind:
    return (
        value if isinstance(value, ProductionRepairIssueKind) else ProductionRepairIssueKind(value)
    )


def repair_key_for_rejection(
    *,
    edition_id: UUID,
    subject_id: UUID,
    kind: ProductionRepairIssueKind | str,
    source_url: str,
    artifact_type: str | None,
    value: str,
) -> str:
    """Return the stable identity of one rejected indicator or rule."""
    issue_kind = _repair_kind(kind)
    if issue_kind not in {
        ProductionRepairIssueKind.REJECTED_INDICATOR,
        ProductionRepairIssueKind.REJECTED_RULE,
    }:
        raise ValueError("rejection repair keys require an indicator or rule kind")
    return repair_key_for_rejection_hash(
        edition_id=edition_id,
        subject_id=subject_id,
        kind=issue_kind,
        source_url=source_url,
        artifact_type=artifact_type,
        value_sha256=_sha256(value),
    )


def repair_key_for_rejection_hash(
    *,
    edition_id: UUID,
    subject_id: UUID,
    kind: ProductionRepairIssueKind | str,
    source_url: str,
    artifact_type: str | None,
    value_sha256: str,
) -> str:
    """Build a rejection key from a previously persisted exact-value hash."""
    issue_kind = _repair_kind(kind)
    if issue_kind not in {
        ProductionRepairIssueKind.REJECTED_INDICATOR,
        ProductionRepairIssueKind.REJECTED_RULE,
    }:
        raise ValueError("rejection repair keys require an indicator or rule kind")
    if not _SHA256_RE.fullmatch(value_sha256):
        raise ValueError("value_sha256 must be lowercase SHA-256")
    canonical_url = canonicalize_http_url(source_url)
    payload = {
        "version": "1",
        "edition_id": str(edition_id),
        "subject_id": str(subject_id),
        "kind": issue_kind.value,
        "source_url": canonical_url,
        "artifact_type": artifact_type,
        "value_sha256": value_sha256,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def repair_key_for_supplemental_source(
    *, edition_id: UUID, subject_id: UUID, source_url: str
) -> str:
    """Return the stable identity reserved for an unarchived source issue."""
    payload = {
        "version": "1",
        "edition_id": str(edition_id),
        "subject_id": str(subject_id),
        "kind": ProductionRepairIssueKind.SUPPLEMENTAL_SOURCE_UNARCHIVED.value,
        "source_url": canonicalize_http_url(source_url),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


# Short aliases make the pure helper convenient to use without hiding the
# distinction between rejection and supplemental-source identities.
build_repair_key = repair_key_for_rejection
build_supplemental_source_repair_key = repair_key_for_supplemental_source
compute_repair_key = repair_key_for_rejection


def build_repair_evidence_pack(entries: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Create the versioned, inert Q2 rejection evidence pack."""
    return {
        "schema_version": REPAIR_EVIDENCE_SCHEMA_VERSION,
        "entries": [dict(entry) for entry in entries],
    }


# This version is part of the functional Q4 input.  Keep it alongside the
# pure projection so callers cannot accidentally hash a different pack from
# the one sent to the synthesis stage.
SYNTHESIS_EVIDENCE_PACK_VERSION = "7"


def _projection_enum_value(value: Any) -> Any:
    return getattr(value, "value", value)


def extraction_item_contributes_to_synthesis(item: ExtractionItem) -> bool:
    """Return whether one extraction item belongs in the Q4 evidence pack."""
    if not item.supported:
        return False
    if item.indicator_status is IndicatorStatus.EXCLUDED:
        return False
    if item.display_policy is DisplayPolicy.HIDDEN:
        return False

    # A value re-admitted by a repair carries no narrative evidence: the
    # projection builds it with an empty context and no evidence quote, so it
    # belongs to a deterministic publication list -- the IOC section for a
    # publication IOC, the technical body otherwise -- and never to the Q4
    # evidence pack.  Restricting this carve-out to ``IOC_SECTION`` made a
    # filename/filepath/CVE repair change the Q4 projection, which classified
    # the whole article as NARRATIVE and charged it a synthesis model call.
    if (
        not item.context.strip()
        and not item.evidence_quote.strip()
        and (
            item.evidence_basis is ProductionEvidenceBasis.ANALYST_OVERRIDE
            or item.provenance is IndicatorProvenance.ANALYST
        )
    ):
        return False

    return True


def synthesis_projection_payload(
    report: ReferenceReport,
    extraction: TechnicalExtraction,
    source_tiers_by_url: Mapping[str, str],
) -> dict[str, Any]:
    """Build the exact deterministic evidence projection consumed by Q4."""
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    for item in extraction.items:
        if not extraction_item_contributes_to_synthesis(item):
            continue

        category = item.category or ""
        dedup_key = (category, item.value.strip().casefold())
        artifact_type = (
            item.artifact_type.value
            if isinstance(item.artifact_type, ArtifactType)
            else item.artifact_type
        )
        candidate = {
            "category": category,
            "value": item.value,
            "context": item.context,
            "source_ids": sorted(item.source_ids),
            "is_confirmed_indicator": item.indicator_status is IndicatorStatus.CONFIRMED_IOC,
            "artifact_type": artifact_type,
        }
        existing = merged.get(dedup_key)
        if existing is None:
            merged[dedup_key] = candidate
            continue

        # These choices make duplicate extraction rows independent of their
        # input order while preserving the historical preference for the most
        # informative context.
        contexts = (str(existing["context"] or ""), str(candidate["context"] or ""))
        existing["context"] = max(contexts, key=lambda value: (len(value), value))
        existing["value"] = min(
            (str(existing["value"]), str(candidate["value"])),
            key=lambda value: (value.casefold(), value),
        )
        existing["category"] = min(str(existing["category"]), str(candidate["category"]))
        artifact_types: set[str] = {
            str(value)
            for value in (existing.get("artifact_type"), candidate.get("artifact_type"))
            if value is not None
        }
        existing["artifact_type"] = min(artifact_types) if artifact_types else None
        existing["is_confirmed_indicator"] = bool(
            existing["is_confirmed_indicator"] or candidate["is_confirmed_indicator"]
        )
        existing_source_ids = {
            str(source_id) for source_id in cast(Sequence[Any], existing["source_ids"])
        }
        candidate_source_ids = {
            str(source_id) for source_id in cast(Sequence[Any], candidate["source_ids"])
        }
        existing["source_ids"] = sorted(existing_source_ids | candidate_source_ids)

    items = sorted(
        merged.values(),
        key=lambda item: (
            str(item["category"]),
            str(item["value"]),
            str(item["context"]),
            tuple(item["source_ids"]),
        ),
    )

    return {
        "version": SYNTHESIS_EVIDENCE_PACK_VERSION,
        "reference_report": {
            "sources": [
                {
                    "id": source.local_id,
                    "tier": source_tiers_by_url.get(source.canonical_url, "unknown"),
                    "title": source.title,
                    "publisher": source.publisher,
                    "published_at": (
                        source.published_at.isoformat() if source.published_at else None
                    ),
                }
                for source in sorted(report.sources, key=lambda source: source.local_id)
            ],
            "events": [
                {
                    "date": event.event_date.isoformat() if event.event_date else None,
                    "source_ids": sorted(event.source_ids),
                    "text": re.sub(
                        r"\b(?:https?|hxxps?)://\S+",
                        "[URL omitted]",
                        event.text,
                        flags=re.IGNORECASE,
                    ),
                }
                for event in sorted(
                    report.events,
                    key=lambda event: (
                        event.event_date.isoformat() if event.event_date else "",
                        event.local_id,
                    ),
                )
            ],
            "uncertainties": sorted(report.uncertainties),
        },
        "technical_extraction": {
            "items": items,
            "uncertainties": sorted(extraction.uncertainties),
        },
    }


def _publication_item_projection(item: ExtractionItem) -> dict[str, Any]:
    """Keep only fields consumed by the publication builder and annotator."""
    artifact_type = (
        item.artifact_type.value
        if isinstance(item.artifact_type, ArtifactType)
        else item.artifact_type
    )
    return {
        "category": item.category,
        "value": item.value,
        "supported": item.supported,
        "semantic_type": _projection_enum_value(item.semantic_type),
        "indicator_status": _projection_enum_value(item.indicator_status),
        "artifact_type": artifact_type,
        "display_policy": _projection_enum_value(item.display_policy),
        "normalized_value": item.normalized_value,
        "source_ids": sorted(item.source_ids),
    }


def publication_projection_payload(
    report: ReferenceReport,
    extraction: TechnicalExtraction,
    synthesis_text: str,
) -> dict[str, Any]:
    """Build the functional inputs used by ``build_publication_document``."""
    items = [_publication_item_projection(item) for item in extraction.items]
    items.sort(key=lambda item: json.dumps(item, sort_keys=True, separators=(",", ":")))
    return {
        "reference_report": {
            "sources": [
                {
                    "id": source.local_id,
                    "canonical_url": source.canonical_url,
                    "title": source.title,
                    "publisher": source.publisher,
                    "published_at": (
                        source.published_at.isoformat() if source.published_at else None
                    ),
                }
                for source in sorted(report.sources, key=lambda source: source.local_id)
            ],
            "events": [
                {
                    "date": event.event_date.isoformat() if event.event_date else None,
                    "source_ids": sorted(event.source_ids),
                    "text": event.text,
                }
                for event in report.events
            ],
            "uncertainties": sorted(report.uncertainties),
            "editorial_title": report.editorial_title,
        },
        "extraction": {
            "items": items,
            "uncertainties": sorted(extraction.uncertainties),
        },
        "synthesis_text": synthesis_text,
    }


def rule_bundle_projection_payload(extraction: TechnicalExtraction) -> dict[str, Any]:
    """Build the functional projection of the detection-rule bundle."""
    rules = [
        {
            "rule_type": _projection_enum_value(rule.rule_type),
            "name": rule.name,
            "body": rule.body,
            "sha256": rule.sha256,
            "source_ids": sorted(rule.source_ids),
            "context": rule.context,
            "evidence_quote": rule.evidence_quote,
            "supported": rule.supported,
            "evidence_basis": _projection_enum_value(rule.evidence_basis),
        }
        for rule in extraction.rules
    ]
    rules.sort(key=lambda rule: json.dumps(rule, sort_keys=True, separators=(",", ":")))
    return {"rules": rules}


def _projection_hash(payload: dict[str, Any]) -> str:
    return _sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")))


def synthesis_projection_hash(
    report: ReferenceReport,
    extraction: TechnicalExtraction,
    source_tiers_by_url: Mapping[str, str],
) -> str:
    return _projection_hash(synthesis_projection_payload(report, extraction, source_tiers_by_url))


def publication_projection_hash(
    report: ReferenceReport,
    extraction: TechnicalExtraction,
    synthesis_text: str,
) -> str:
    return _projection_hash(publication_projection_payload(report, extraction, synthesis_text))


def rule_bundle_projection_hash(extraction: TechnicalExtraction) -> str:
    return _projection_hash(rule_bundle_projection_payload(extraction))


class ProductionRepairStatusError(ValueError):
    """The edition cannot accept repair decisions in its current state."""

    code = "production_repair_status_invalid"


class ProductionRepairStaleError(ValueError):
    """The decision no longer addresses the current production generation."""

    code = "production_repair_stale"


class ProductionRepairDecisionChangedError(ValueError):
    """The effective decision moved under the caller's optimistic fence.

    Decisions are revisable, so "already decided" is not a refusal.  What must
    be refused is writing over an answer the caller never saw: the request
    carries the decision id it observed, and a mismatch means someone else
    (or a retried request) already appended a newer one.
    """

    code = "production_repair_decision_changed"


class ProductionRepairDecisionNoopError(ValueError):
    """The requested revision would leave the effective action unchanged."""

    code = "production_repair_decision_noop"

    def __init__(self, repair_key: str | None = None) -> None:
        self.repair_key = repair_key
        super().__init__(self.code)


class ProductionRepairActionInvalidError(ValueError):
    """The requested action does not exist for this issue kind."""

    code = "production_repair_action_invalid"


class ProductionRepairIssueNotFoundError(ValueError):
    """A requested repair issue is not present in the current extraction."""

    code = "production_repair_issue_not_found"


class ProductionRepairValueNotVerifiableError(ValueError):
    """An INCLUDE was asked for a value the deterministic pipeline rejects.

    Q2 rejects proposals for evidence reasons but also for shape reasons
    (``normalization_error``, an unsupported artifact type, an oversized rule).
    Accepting such a value would make every later projection of the article
    fail, and the append-only decision log would keep it that way, so the
    gesture is refused at the point where the analyst can still choose
    ``exclude`` instead.
    """

    code = "production_repair_value_not_verifiable"


class ProductionRepairAuditReasonRequiredError(ValueError):
    """An unverified analyst override must explain why it was forced."""

    code = "production_repair_audit_reason_required"


@dataclass(frozen=True, slots=True)
class ProductionRepairCorrectionVerification:
    """Result of checking a proposed replacement against one archive."""

    verified: bool
    replacement_value: str
    normalized_value: str | None
    format_valid: bool
    artifact_type: str
    source_id: str
    source_url: str
    reason_code: str | None = None
    context_spans: tuple[SourceEvidenceSpan, ...] = ()
    verification_state: ProductionRepairVerificationState | None = None


class ProductionRepairProjectionError(ValueError):
    """The effective extraction cannot be safely projected."""


class ProductionReferenceRepairError(ValueError):
    """The archived Q1 evidence cannot be safely reconstructed."""

    def __init__(self, code: str, message: str | None = None) -> None:
        self.code = code
        super().__init__(message or code)


#: Every way a repair application can stop, mapped to the step that stopped it
#: and to the one action that unblocks it.  A code absent from this table is
#: still reported -- with the generic remediation -- so a new failure is never
#: rendered as an unexplained "the review could not be updated".
_REPAIR_FAILURE_DIAGNOSTICS: dict[str, tuple[RepairApplicationStage, RepairRemediation]] = {
    "production_repair_actor_required": (
        RepairApplicationStage.FENCE,
        RepairRemediation.REAUTHENTICATE,
    ),
    "production_run_not_found": (
        RepairApplicationStage.FENCE,
        RepairRemediation.RELOAD_REPAIR_QUEUE,
    ),
    "production_run_edition_changed": (
        RepairApplicationStage.FENCE,
        RepairRemediation.RELOAD_REPAIR_QUEUE,
    ),
    "production_repair_stale": (
        RepairApplicationStage.FENCE,
        RepairRemediation.RELOAD_REPAIR_QUEUE,
    ),
    "edition_not_found": (
        RepairApplicationStage.FENCE,
        RepairRemediation.RELOAD_REPAIR_QUEUE,
    ),
    "production_repair_run_not_reviewable": (
        RepairApplicationStage.FENCE,
        RepairRemediation.WAIT_FOR_RUN,
    ),
    "edition_frozen_for_publication": (
        RepairApplicationStage.FENCE,
        RepairRemediation.REOPEN_EDITION,
    ),
    "production_reconciliation_required": (
        RepairApplicationStage.FENCE,
        RepairRemediation.RECONCILE_SUBMISSION,
    ),
    "repair_payload_unavailable": (
        RepairApplicationStage.PAYLOAD,
        RepairRemediation.RERUN_EXTRACTION,
    ),
    "repair_payload_hash_mismatch": (
        RepairApplicationStage.PAYLOAD,
        RepairRemediation.RERUN_EXTRACTION,
    ),
    "repair_correction_unavailable": (
        RepairApplicationStage.PAYLOAD,
        RepairRemediation.RESUBMIT_CORRECTION,
    ),
    "repair_correction_payload_unavailable": (
        RepairApplicationStage.PAYLOAD,
        RepairRemediation.RESUBMIT_CORRECTION,
    ),
    "repair_correction_hash_mismatch": (
        RepairApplicationStage.PAYLOAD,
        RepairRemediation.RESUBMIT_CORRECTION,
    ),
    "extraction_artifact_not_found": (
        RepairApplicationStage.PROJECTION,
        RepairRemediation.RERUN_EXTRACTION,
    ),
    "extraction_payload_missing": (
        RepairApplicationStage.PROJECTION,
        RepairRemediation.RERUN_EXTRACTION,
    ),
    "extraction_payload_unavailable": (
        RepairApplicationStage.PROJECTION,
        RepairRemediation.RERUN_EXTRACTION,
    ),
    "repair_projection_base_not_found": (
        RepairApplicationStage.PROJECTION,
        RepairRemediation.RERUN_EXTRACTION,
    ),
    "production_repair_storage_unavailable": (
        RepairApplicationStage.PROJECTION,
        RepairRemediation.CONTACT_OPERATIONS,
    ),
    "production_artifact_stale_port_unavailable": (
        RepairApplicationStage.PROJECTION,
        RepairRemediation.CONTACT_OPERATIONS,
    ),
    "publication_inputs_missing": (
        RepairApplicationStage.ASSEMBLY,
        RepairRemediation.RERUN_ASSEMBLY,
    ),
    "qa_inputs_missing": (
        RepairApplicationStage.QA,
        RepairRemediation.RERUN_ASSEMBLY,
    ),
    "production_repair_qa_failed": (
        RepairApplicationStage.QA,
        RepairRemediation.OPEN_QA_REPORT,
    ),
    "references_artifact_not_found": (
        RepairApplicationStage.REFERENCES,
        RepairRemediation.RERUN_REFERENCES,
    ),
    "references_payload_missing": (
        RepairApplicationStage.REFERENCES,
        RepairRemediation.RERUN_REFERENCES,
    ),
    "research_date_missing": (
        RepairApplicationStage.REFERENCES,
        RepairRemediation.RERUN_REFERENCES,
    ),
}


def repair_application_diagnostic(
    error_code: str,
    message: str,
    *,
    repair_id: str,
) -> dict[str, str]:
    """Describe a failed repair application in terms an analyst can act on.

    ``repair_id`` identifies the arbitration the analyst was applying -- the
    article's subject for an article-wide application -- so a support exchange
    names the same object the desk shows.  An unmapped code degrades to the
    projection stage and a queue reload rather than to nothing at all.
    """
    stage, remediation = _REPAIR_FAILURE_DIAGNOSTICS.get(
        error_code,
        (RepairApplicationStage.PROJECTION, RepairRemediation.RELOAD_REPAIR_QUEUE),
    )
    return {
        "repair_id": repair_id,
        "stage": stage.value,
        "error_code": error_code,
        "message": message or error_code,
        "remediation": remediation.value,
    }


@dataclass(frozen=True, slots=True)
class ProductionRepairDecisionInput:
    """Validated identity supplied by an edition-scoped bulk decision."""

    subject_id: UUID
    production_run_id: UUID
    observed_artifact_id: UUID
    observed_pipeline_generation: int
    repair_key: str
    issue_kind: ProductionRepairIssueKind
    action: ProductionRepairAction
    correction: ProductionRepairCorrection | None = None
    # Optimistic fence: the effective decision the caller was looking at, or
    # ``None`` for a first decision.  Recomputed under the transaction.
    expected_effective_decision_id: UUID | None = None


class ProductionRepairDecisionService:
    """Validate and append human repair decisions."""

    def __init__(self, uow_factory: ProductionUnitOfWorkFactory) -> None:
        self._uow_factory = uow_factory

    async def decide(
        self,
        *,
        edition_id: UUID,
        subject_id: UUID,
        production_run_id: UUID,
        observed_artifact_id: UUID,
        observed_pipeline_generation: int,
        repair_key: str,
        issue_kind: ProductionRepairIssueKind,
        action: ProductionRepairAction,
        actor_id: str,
        reason: str | None = None,
        expected_effective_decision_id: UUID | None = None,
        correction: ProductionRepairCorrection | None = None,
    ) -> ProductionRepairDecision:
        # Construct first so pure invariants fail before opening a transaction.
        decision = ProductionRepairDecision(
            edition_id=edition_id,
            subject_id=subject_id,
            production_run_id=production_run_id,
            observed_artifact_id=observed_artifact_id,
            observed_pipeline_generation=observed_pipeline_generation,
            repair_key=repair_key,
            issue_kind=issue_kind,
            action=action,
            actor_id=actor_id,
            reason=reason,
            correction_id=correction.id if correction is not None else None,
        )
        if action is ProductionRepairAction.REPLACE and correction is None:
            raise ProductionRepairValueNotVerifiableError("production_repair_correction_required")
        if correction is not None and (
            correction.edition_id != edition_id
            or correction.subject_id != subject_id
            or correction.production_run_id != production_run_id
            or correction.original_repair_key != repair_key
        ):
            raise ProductionRepairValueNotVerifiableError(
                "production_repair_correction_identity_mismatch"
            )
        if (
            correction is not None
            and correction.verification_state is ProductionRepairVerificationState.ANALYST_OVERRIDE
            and not (reason or "").strip()
        ):
            raise ProductionRepairAuditReasonRequiredError(
                ProductionRepairAuditReasonRequiredError.code
            )

        async with self._uow_factory() as uow:
            edition = await _get_for_update(uow.editions, edition_id)
            if edition is None or _enum_value(edition.status) not in {
                EditionStatus.PRODUCTION.value,
                EditionStatus.REVIEW.value,
            }:
                raise ProductionRepairStatusError("edition_frozen_for_publication")
            manifests = getattr(uow, "publication_manifests", None)
            if (
                manifests is not None
                and await manifests.get_latest_for_edition(edition_id) is not None
            ):
                raise ProductionRepairStatusError("edition_frozen_for_publication")
            effective = await _effective_decisions_for_reader(uow, edition_id, subject_id)
            current = _effective_decision_for_key(effective, subject_id, repair_key)
            if (
                current is not None
                and _enum_value(current.action) == _enum_value(action)
                and not (
                    action is ProductionRepairAction.REPLACE
                    and correction is not None
                    and current.correction_id != correction.id
                )
            ):
                raise ProductionRepairDecisionNoopError(repair_key)
            _require_decision_fence(
                effective,
                subject_id=subject_id,
                repair_key=repair_key,
                expected_effective_decision_id=expected_effective_decision_id,
            )

            run_repository = uow.subject_production_runs
            run = await _get_for_update(run_repository, production_run_id)
            if (
                run is None
                or run.edition_id != edition_id
                or run.subject_id != subject_id
                or run.pipeline_generation != observed_pipeline_generation
            ):
                raise ProductionRepairStaleError(ProductionRepairStaleError.code)
            run_status = _enum_value(getattr(run, "status", None))
            if run_status in {
                SubjectProductionStatus.QUEUED.value,
                SubjectProductionStatus.RUNNING.value,
                SubjectProductionStatus.CANCELLED.value,
            }:
                raise ProductionRepairStatusError("production_repair_run_not_reviewable")
            if getattr(run, "requires_reconciliation", False):
                raise ProductionRepairStatusError("production_reconciliation_required")
            if run_status is not None and run_status not in {
                SubjectProductionStatus.READY.value,
                SubjectProductionStatus.NEEDS_REVIEW.value,
                SubjectProductionStatus.FAILED.value,
            }:
                raise ProductionRepairStatusError("production_repair_run_not_reviewable")

            artifact = await uow.production_artifacts.get(observed_artifact_id)
            if (
                artifact is None
                or artifact.id != observed_artifact_id
                or artifact.production_run_id != production_run_id
                or _enum_value(artifact.status) == ProductionArtifactStatus.STALE.value
            ):
                raise ProductionRepairStaleError(ProductionRepairStaleError.code)
            expected_stage = (
                ProductionArtifactStage.REFERENCES
                if issue_kind is ProductionRepairIssueKind.SUPPLEMENTAL_SOURCE_UNARCHIVED
                else ProductionArtifactStage.EXTRACTION
            )
            if _enum_value(getattr(artifact, "stage", None)) != expected_stage.value:
                raise ProductionRepairStaleError(ProductionRepairStaleError.code)
            get_current = getattr(uow.production_artifacts, "get_current", None)
            if callable(get_current):
                current_artifact = await get_current(production_run_id, _enum_value(artifact.stage))
                if current_artifact is None or current_artifact.id != artifact.id:
                    raise ProductionRepairStaleError(ProductionRepairStaleError.code)

            if correction is not None:
                corrections = getattr(uow, "production_repair_corrections", None)
                if corrections is None:
                    raise ProductionRepairValueNotVerifiableError(
                        "production_repair_correction_repository_unavailable"
                    )
                existing = await corrections.get(correction.id)
                if existing is None:
                    await corrections.append(correction)
            await uow.production_repair_decisions.append(decision)
            await uow.commit()
            return decision

    async def decide_bulk(
        self,
        *,
        edition_id: UUID,
        decisions: Sequence[ProductionRepairDecisionInput],
        actor_id: str,
        reason: str | None = None,
    ) -> tuple[ProductionRepairDecision, ...]:
        """Append a batch of decisions atomically after validating every fence."""
        if not decisions:
            raise ValueError("production_repair_bulk_empty")
        if len(decisions) > 200:
            raise ValueError("production_repair_bulk_limit_exceeded")
        identities = [(item.subject_id, item.repair_key) for item in decisions]
        if len(set(identities)) != len(identities):
            raise ValueError("production_repair_duplicate")

        events = tuple(
            ProductionRepairDecision(
                edition_id=edition_id,
                subject_id=item.subject_id,
                production_run_id=item.production_run_id,
                observed_artifact_id=item.observed_artifact_id,
                observed_pipeline_generation=item.observed_pipeline_generation,
                repair_key=item.repair_key,
                issue_kind=item.issue_kind,
                action=item.action,
                actor_id=actor_id,
                reason=reason,
                correction_id=(item.correction.id if item.correction is not None else None),
            )
            for item in decisions
        )
        for item in decisions:
            if item.correction is not None and (
                item.correction.edition_id != edition_id
                or item.correction.subject_id != item.subject_id
                or item.correction.production_run_id != item.production_run_id
                or item.correction.original_repair_key != item.repair_key
            ):
                raise ProductionRepairValueNotVerifiableError(
                    "production_repair_correction_identity_mismatch"
                )

        async with self._uow_factory() as uow:
            edition = await _get_for_update(uow.editions, edition_id)
            if edition is None or _enum_value(edition.status) not in {
                EditionStatus.PRODUCTION.value,
                EditionStatus.REVIEW.value,
            }:
                raise ProductionRepairStatusError("edition_frozen_for_publication")
            manifests = getattr(uow, "publication_manifests", None)
            if (
                manifests is not None
                and await manifests.get_latest_for_edition(edition_id) is not None
            ):
                raise ProductionRepairStatusError("edition_frozen_for_publication")

            repository = uow.production_repair_decisions
            effective_getter = getattr(repository, "effective_decisions", None)
            effective = (
                await effective_getter(edition_id)
                if callable(effective_getter)
                else _effective_from_history(await repository.list_for_edition(edition_id))
            )

            for item, _event in zip(decisions, events, strict=True):
                current = _effective_decision_for_key(effective, item.subject_id, item.repair_key)
                if (
                    current is not None
                    and _enum_value(current.action) == _enum_value(item.action)
                    and not (
                        item.action is ProductionRepairAction.REPLACE
                        and item.correction is not None
                        and current.correction_id != item.correction.id
                    )
                ):
                    raise ProductionRepairDecisionNoopError(item.repair_key)
                _require_decision_fence(
                    effective,
                    subject_id=item.subject_id,
                    repair_key=item.repair_key,
                    expected_effective_decision_id=item.expected_effective_decision_id,
                )
                run = await _get_for_update(uow.subject_production_runs, item.production_run_id)
                if (
                    run is None
                    or run.edition_id != edition_id
                    or run.subject_id != item.subject_id
                    or run.pipeline_generation != item.observed_pipeline_generation
                ):
                    raise ProductionRepairStaleError(ProductionRepairStaleError.code)
                run_status = _enum_value(getattr(run, "status", None))
                if run_status in {
                    SubjectProductionStatus.QUEUED.value,
                    SubjectProductionStatus.RUNNING.value,
                    SubjectProductionStatus.CANCELLED.value,
                }:
                    raise ProductionRepairStatusError("production_repair_run_not_reviewable")
                if getattr(run, "requires_reconciliation", False):
                    raise ProductionRepairStatusError("production_reconciliation_required")
                if run_status is not None and run_status not in {
                    SubjectProductionStatus.READY.value,
                    SubjectProductionStatus.NEEDS_REVIEW.value,
                    SubjectProductionStatus.FAILED.value,
                }:
                    raise ProductionRepairStatusError("production_repair_run_not_reviewable")

                artifact = await uow.production_artifacts.get(item.observed_artifact_id)
                expected_stage = (
                    ProductionArtifactStage.REFERENCES
                    if item.issue_kind is ProductionRepairIssueKind.SUPPLEMENTAL_SOURCE_UNARCHIVED
                    else ProductionArtifactStage.EXTRACTION
                )
                get_current = getattr(uow.production_artifacts, "get_current", None)
                current_artifact = (
                    await get_current(item.production_run_id, expected_stage.value)
                    if callable(get_current)
                    else artifact
                )
                if (
                    artifact is None
                    or artifact.production_run_id != item.production_run_id
                    or _enum_value(artifact.status) == ProductionArtifactStatus.STALE.value
                    or _enum_value(artifact.stage) != expected_stage.value
                    or current_artifact is None
                    or current_artifact.id != artifact.id
                ):
                    raise ProductionRepairStaleError(ProductionRepairStaleError.code)

            corrections = getattr(uow, "production_repair_corrections", None)
            for item in decisions:
                if item.correction is not None:
                    if corrections is None:
                        raise ProductionRepairValueNotVerifiableError(
                            "production_repair_correction_repository_unavailable"
                        )
                    if await corrections.get(item.correction.id) is None:
                        await corrections.append(item.correction)
            for event in events:
                await uow.production_repair_decisions.append(event)
            await uow.commit()
            return events

    async def effective_decisions(
        self, edition_id: UUID, subject_id: UUID | None = None
    ) -> tuple[ProductionRepairDecision, ...]:
        async with self._uow_factory() as uow:
            repository = uow.production_repair_decisions
            getter = getattr(repository, "effective_decisions", None)
            if callable(getter):
                return tuple(await getter(edition_id, subject_id))
            history = await repository.list_for_edition(edition_id, subject_id)
            return _effective_from_history(history)

    async def decision_history(
        self, edition_id: UUID, repair_key: str, subject_id: UUID | None = None
    ) -> tuple[ProductionRepairDecision, ...]:
        """Return every decision ever appended for one repair identity."""
        async with self._uow_factory() as uow:
            history = await uow.production_repair_decisions.list_for_edition(edition_id, subject_id)
        return decision_history_for_key(history, repair_key)


def decision_history_for_key(
    history: Sequence[ProductionRepairDecision], repair_key: str
) -> tuple[ProductionRepairDecision, ...]:
    """Order one identity's audit deterministically, oldest first."""
    return tuple(
        sorted(
            (decision for decision in history if decision.repair_key == repair_key),
            key=lambda item: (item.created_at, str(item.id)),
        )
    )


def _require_decision_fence(
    effective: Sequence[ProductionRepairDecision],
    *,
    subject_id: UUID,
    repair_key: str,
    expected_effective_decision_id: UUID | None,
) -> None:
    """Refuse a revision written over an answer the caller never observed.

    A first decision passes ``None``; a revision passes the id it displayed.
    A retried HTTP request therefore fails unambiguously after its first
    append instead of silently appending the same event twice.
    """
    current = _effective_decision_for_key(effective, subject_id, repair_key)
    current_id = current.id if current is not None else None
    if current_id != expected_effective_decision_id:
        raise ProductionRepairDecisionChangedError(ProductionRepairDecisionChangedError.code)


def _effective_decision_for_key(
    effective: Sequence[ProductionRepairDecision], subject_id: UUID, repair_key: str
) -> ProductionRepairDecision | None:
    return next(
        (
            decision
            for decision in effective
            if decision.subject_id == subject_id and decision.repair_key == repair_key
        ),
        None,
    )


@dataclass(frozen=True, slots=True)
class ProductionRepairIssueView:
    """Bounded list representation of one current Q2 repair issue."""

    repair_key: str
    kind: ProductionRepairIssueKind
    artifact_type: str | None
    source_id: str
    source_title: str
    is_publication_ioc: bool
    source_url: str
    reason_code: str
    value_sha256: str
    preview: str
    payload_available: bool
    production_run_id: UUID
    observed_artifact_id: UUID
    observed_artifact_version: int
    observed_pipeline_generation: int
    model_run_id: str | None = None
    batch_id: str | None = None
    #: Where the displayed value came from. The bounded list never reads an
    #: archive, so it reports UNAVAILABLE until the detail resolves the issue.
    payload_origin: RepairPayloadOrigin = RepairPayloadOrigin.UNAVAILABLE
    #: True when this entry comes from pre-LOT18 diagnostics, whose value the
    #: detail endpoint may still recover from the archived Q2 output.
    legacy_evidence: bool = False
    effective_decision: ProductionRepairDecision | None = None
    # True only when the effective decision's content is proven materialized
    # by the current projection marker -- never inferred from its action.
    projection_applied: bool = False
    application_state: RepairDecisionApplicationState = RepairDecisionApplicationState.UNRESOLVED
    subject_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class ProductionRepairIssueDetail:
    """One issue plus the exact value the resolver could prove."""

    issue: ProductionRepairIssueView
    value: str | None
    decision_history: tuple[ProductionRepairDecision, ...] = ()

    @property
    def payload_available(self) -> bool:
        return self.issue.payload_available

    @property
    def payload_origin(self) -> RepairPayloadOrigin:
        return self.issue.payload_origin


@dataclass(frozen=True, slots=True)
class SupplementalSourceRepairIssue:
    """A Q1 source proposal absent from the CURRENT canonical ReferenceReport.

    The issue lives until one of two terminal situations is true: the analyst
    waived the source with ``continue_without_source`` while it stays
    unarchived, or a new REFERENCES version actually put its URL back in the
    canonical report.  Archiving alone only moves it to
    ``ARCHIVED_PENDING_REFERENCES`` -- a rebuild debt that no longer needs an
    arbitration but must never be forgotten.
    """

    repair_key: str
    kind: ProductionRepairIssueKind
    source_id: str
    source_title: str
    source_url: str
    publisher: str | None
    collection_id: UUID | None
    collection_state: str | None
    error_reason: str | None
    attempt_count: int
    production_run_id: UUID
    observed_artifact_id: UUID
    observed_artifact_version: int
    observed_pipeline_generation: int
    repair_state: SupplementalSourceRepairState = SupplementalSourceRepairState.UNARCHIVED
    rebuild_required: bool = False
    effective_decision: ProductionRepairDecision | None = None
    recommended_action: str = "archive_manual_content"
    subject_id: UUID | None = None
    expected_q2_calls: int = 0
    expected_q2_reuses: int = 0
    reuse_unknown_count: int = 0


_NO_DELIVERABLE_OUTPUTS = frozenset[ProductionDerivedOutput]()
_RULE_BUNDLE_OUTPUTS = frozenset(
    {
        ProductionDerivedOutput.EXTRACTION,
        ProductionDerivedOutput.RULE_BUNDLE,
        ProductionDerivedOutput.CHECKPOINT,
    }
)
_PUBLICATION_OUTPUTS = frozenset(
    {
        ProductionDerivedOutput.EXTRACTION,
        ProductionDerivedOutput.PUBLICATION,
        ProductionDerivedOutput.CHECKPOINT,
    }
)
_NARRATIVE_OUTPUTS = frozenset(
    {
        ProductionDerivedOutput.EXTRACTION,
        ProductionDerivedOutput.SYNTHESIS,
        ProductionDerivedOutput.PUBLICATION,
        ProductionDerivedOutput.CHECKPOINT,
    }
)
_SOURCE_CORPUS_OUTPUTS = frozenset(
    {
        ProductionDerivedOutput.REFERENCES,
        ProductionDerivedOutput.EXTRACTION,
        ProductionDerivedOutput.SYNTHESIS,
        ProductionDerivedOutput.PUBLICATION,
        ProductionDerivedOutput.CHECKPOINT,
    }
)

_REPAIR_IMPACT_DOMINANCE = {
    ProductionRepairImpactKind.NO_DELIVERABLE_CHANGE: 0,
    ProductionRepairImpactKind.RULE_BUNDLE_ONLY: 1,
    ProductionRepairImpactKind.PUBLICATION_ONLY: 2,
    ProductionRepairImpactKind.NARRATIVE: 3,
    ProductionRepairImpactKind.SOURCE_CORPUS: 4,
}

_REPAIR_PLAN_STEPS: dict[ProductionRepairImpactKind, tuple[str, ...]] = {
    ProductionRepairImpactKind.NO_DELIVERABLE_CHANGE: ("Décision analyste",),
    ProductionRepairImpactKind.RULE_BUNDLE_ONLY: (
        "Décision analyste",
        "Projection Extraction",
        "Mise à jour des fichiers YARA/Sigma",
        "Contrôle QA",
    ),
    ProductionRepairImpactKind.PUBLICATION_ONLY: (
        "Décision analyste",
        "Projection Extraction",
        "Rendu Publication",
        "Contrôle QA",
    ),
    ProductionRepairImpactKind.NARRATIVE: (
        "Décision analyste",
        "Projection Extraction",
        "Nouvelle synthèse",
        "Rendu Publication",
        "Contrôle QA",
    ),
    ProductionRepairImpactKind.SOURCE_CORPUS: (
        "Source archivée",
        "Références",
        "Extraction",
        "Synthèse si nécessaire",
        "Publication",
        "Contrôle QA",
    ),
}

_REPAIR_PROVIDER_STEPS: dict[ProductionRepairImpactKind, tuple[str, ...]] = {
    ProductionRepairImpactKind.NO_DELIVERABLE_CHANGE: (),
    ProductionRepairImpactKind.RULE_BUNDLE_ONLY: (),
    ProductionRepairImpactKind.PUBLICATION_ONLY: (),
    ProductionRepairImpactKind.NARRATIVE: ("Nouvelle synthèse",),
    ProductionRepairImpactKind.SOURCE_CORPUS: (
        "Nouvelle extraction possible",
        "Nouvelle synthèse possible",
    ),
}


def _repair_impact(
    kind: ProductionRepairImpactKind,
    affected_outputs: frozenset[ProductionDerivedOutput],
    *,
    model_call_required: bool,
    reason: str,
    ready_to_apply: bool = True,
    provider_steps: tuple[str, ...] | None = None,
    deterministic_steps: tuple[str, ...] | None = None,
    expected_q2_calls: int = 0,
    expected_q2_reuses: int = 0,
    reuse_unknown_count: int = 0,
) -> ProductionRepairImpact:
    return ProductionRepairImpact(
        kind=kind,
        affected_outputs=affected_outputs,
        model_call_required=model_call_required,
        reason=reason,
        provider_steps=(_REPAIR_PROVIDER_STEPS[kind] if provider_steps is None else provider_steps),
        deterministic_steps=(
            _REPAIR_PLAN_STEPS[kind] if deterministic_steps is None else deterministic_steps
        ),
        ready_to_apply=ready_to_apply,
        expected_q2_calls=max(0, expected_q2_calls),
        expected_q2_reuses=max(0, expected_q2_reuses),
        reuse_unknown_count=max(0, reuse_unknown_count),
    )


def merge_repair_impacts(
    impacts: Iterable[ProductionRepairImpact],
) -> ProductionRepairImpact:
    """Merge issue impacts for one article using the semantic dominance order."""
    values = tuple(impacts)
    if not values:
        return _repair_impact(
            ProductionRepairImpactKind.NO_DELIVERABLE_CHANGE,
            frozenset(),
            model_call_required=False,
            reason="No repair changes a deliverable.",
        )

    dominant = max(values, key=lambda impact: _REPAIR_IMPACT_DOMINANCE[impact.kind])
    affected_outputs = frozenset(output for impact in values for output in impact.affected_outputs)
    provider_steps = tuple(
        step
        for step in _REPAIR_PROVIDER_STEPS[dominant.kind]
        if any(
            step in (impact.provider_steps or _REPAIR_PROVIDER_STEPS[impact.kind])
            for impact in values
        )
    )
    deterministic_steps = tuple(
        step
        for step in (
            "Décision analyste",
            "Source archivée",
            "Références",
            "Projection Extraction",
            "Extraction",
            "Nouvelle synthèse",
            "Synthèse si nécessaire",
            "Mise à jour des fichiers YARA/Sigma",
            "Rendu Publication",
            "Publication",
            "Contrôle QA",
        )
        if any(
            step in (impact.deterministic_steps or _REPAIR_PLAN_STEPS[impact.kind])
            for impact in values
        )
    )
    return ProductionRepairImpact(
        kind=dominant.kind,
        affected_outputs=affected_outputs,
        model_call_required=any(impact.model_call_required for impact in values),
        reason=dominant.reason,
        provider_steps=provider_steps,
        deterministic_steps=deterministic_steps,
        ready_to_apply=all(impact.ready_to_apply for impact in values),
        expected_q2_calls=sum(impact.expected_q2_calls for impact in values),
        expected_q2_reuses=sum(impact.expected_q2_reuses for impact in values),
        reuse_unknown_count=sum(impact.reuse_unknown_count for impact in values),
    )


def _decision_action(decision: ProductionRepairDecision | None) -> Any:
    return _projection_enum_value(getattr(decision, "action", None))


def _exclude_revises_projected_content(
    issue: ProductionRepairIssueView,
    decision: ProductionRepairDecision,
) -> bool:
    """Use the existing application state to detect an applied INCLUDE."""
    if _decision_action(decision) != ProductionRepairAction.EXCLUDE.value:
        return False
    try:
        state = RepairDecisionApplicationState(
            _projection_enum_value(getattr(issue, "application_state", None))
        )
    except ValueError:
        return False
    return state is RepairDecisionApplicationState.PROJECTION_REQUIRED


def _include_is_already_effective(
    issue: ProductionRepairIssueView,
    decision: ProductionRepairDecision,
) -> bool:
    if _decision_action(decision) not in {
        ProductionRepairAction.INCLUDE.value,
        ProductionRepairAction.REPLACE.value,
    }:
        return False
    try:
        state = RepairDecisionApplicationState(
            _projection_enum_value(getattr(issue, "application_state", None))
        )
    except ValueError:
        return False
    return state is RepairDecisionApplicationState.ALREADY_EFFECTIVE


def _q2_preview(issue: Any) -> dict[str, int]:
    """Read the optional precomputed source-corpus cost preview."""
    return {
        "expected_q2_calls": int(getattr(issue, "expected_q2_calls", 0) or 0),
        "expected_q2_reuses": int(getattr(issue, "expected_q2_reuses", 0) or 0),
        "reuse_unknown_count": int(getattr(issue, "reuse_unknown_count", 0) or 0),
    }


#: Every arbitration of a rejected Q2 artifact -- an IOC or any other
#: extraction value -- is projected without context and without an evidence
#: quote, so it can only change the deterministic publication.  Narrative
#: content (attribution, chronology, conclusion) is decided by Q4 from the Q1
#: report and the contextual Q2 evidence, neither of which a repair touches.
_INDICATOR_REPAIR_REASON = (
    "The repair changes only the deterministic publication projection of an extracted value."
)


def classify_repair_impact(
    issue: ProductionRepairIssueView | SupplementalSourceRepairIssue,
    decision: ProductionRepairDecision | None,
) -> ProductionRepairImpact:
    """Classify a repair by the derived products whose content can change.

    An arbitration of a rejected extraction value is never NARRATIVE: the
    projection admits it with an empty context, so it reaches the publication
    (IOC section or technical body) without entering the Q4 evidence pack.
    Only a source-corpus change can still owe a new synthesis.

    A Q1 source is classified from the CURRENT factual state of its
    collection, before any decision is read: current factual source state
    dominates an older waiver.  Once the analyst finally supplied the
    publication, the edition owes a REFERENCES reconciliation even though an
    older ``continue_without_source`` said it could be published without it.
    The waiver is never rewritten or deleted -- it stays in the append-only
    audit, it simply no longer describes the corpus.
    """
    action = _decision_action(decision)
    issue_kind = _repair_kind(getattr(issue, "kind", ""))

    if issue_kind is ProductionRepairIssueKind.SUPPLEMENTAL_SOURCE_UNARCHIVED:
        preview = _q2_preview(issue)
        repair_state = _projection_enum_value(getattr(issue, "repair_state", None))
        if repair_state == SupplementalSourceRepairState.ARCHIVED_PENDING_REFERENCES.value:
            return _repair_impact(
                ProductionRepairImpactKind.SOURCE_CORPUS,
                _SOURCE_CORPUS_OUTPUTS,
                model_call_required=True,
                reason=(
                    "An archived Q1 source is absent from REFERENCES and can change the "
                    "source corpus."
                ),
                expected_q2_calls=preview["expected_q2_calls"],
                expected_q2_reuses=preview["expected_q2_reuses"],
                reuse_unknown_count=preview["reuse_unknown_count"],
            )
        if repair_state == SupplementalSourceRepairState.COLLECTION_MISSING.value:
            # There is nothing to reintegrate yet: the plan describes real
            # work but no rebuild can run before the source is prepared.
            return _repair_impact(
                ProductionRepairImpactKind.SOURCE_CORPUS,
                _SOURCE_CORPUS_OUTPUTS,
                model_call_required=True,
                ready_to_apply=False,
                reason="The supplemental source has no collection to archive yet.",
                expected_q2_calls=preview["expected_q2_calls"],
                expected_q2_reuses=preview["expected_q2_reuses"],
                reuse_unknown_count=preview["reuse_unknown_count"],
            )
        if action == ProductionRepairAction.CONTINUE_WITHOUT_SOURCE.value:
            return _repair_impact(
                ProductionRepairImpactKind.NO_DELIVERABLE_CHANGE,
                _NO_DELIVERABLE_OUTPUTS,
                model_call_required=False,
                reason="The analyst waived the supplemental source without adding content.",
            )
        if decision is None:
            return _repair_impact(
                ProductionRepairImpactKind.SOURCE_CORPUS,
                _SOURCE_CORPUS_OUTPUTS,
                model_call_required=True,
                ready_to_apply=False,
                reason="Archiving the supplemental source may change the source corpus.",
                expected_q2_calls=preview["expected_q2_calls"],
                expected_q2_reuses=preview["expected_q2_reuses"],
                reuse_unknown_count=preview["reuse_unknown_count"],
            )
        return _repair_impact(
            ProductionRepairImpactKind.NO_DELIVERABLE_CHANGE,
            _NO_DELIVERABLE_OUTPUTS,
            model_call_required=False,
            reason="The supplemental source has no materialized deliverable change.",
        )

    if decision is None:
        if issue_kind is ProductionRepairIssueKind.REJECTED_RULE:
            return _repair_impact(
                ProductionRepairImpactKind.RULE_BUNDLE_ONLY,
                _RULE_BUNDLE_OUTPUTS,
                model_call_required=False,
                ready_to_apply=False,
                reason="Including the rule changes only the accepted detection-rule bundle.",
            )
        return _repair_impact(
            ProductionRepairImpactKind.PUBLICATION_ONLY,
            _PUBLICATION_OUTPUTS,
            model_call_required=False,
            ready_to_apply=False,
            reason=_INDICATOR_REPAIR_REASON,
        )

    q2_issue = cast(ProductionRepairIssueView, issue)
    action_is_include = action in {
        ProductionRepairAction.INCLUDE.value,
        ProductionRepairAction.REPLACE.value,
    }
    action_is_exclude = action == ProductionRepairAction.EXCLUDE.value
    changes_projected_content = (
        action_is_include and not _include_is_already_effective(q2_issue, decision)
    ) or (action_is_exclude and _exclude_revises_projected_content(q2_issue, decision))
    if not changes_projected_content:
        return _repair_impact(
            ProductionRepairImpactKind.NO_DELIVERABLE_CHANGE,
            _NO_DELIVERABLE_OUTPUTS,
            model_call_required=False,
            reason="The rejected value was not previously included in a deliverable.",
        )

    if issue_kind is ProductionRepairIssueKind.REJECTED_RULE:
        return _repair_impact(
            ProductionRepairImpactKind.RULE_BUNDLE_ONLY,
            _RULE_BUNDLE_OUTPUTS,
            model_call_required=False,
            ready_to_apply=not _issue_is_unbuildable(q2_issue),
            reason="The repair changes only the accepted detection-rule bundle.",
        )

    return _repair_impact(
        ProductionRepairImpactKind.PUBLICATION_ONLY,
        _PUBLICATION_OUTPUTS,
        model_call_required=False,
        ready_to_apply=not _issue_is_unbuildable(q2_issue),
        reason=_INDICATOR_REPAIR_REASON,
    )


def _issue_is_unbuildable(issue: Any) -> bool:
    return bool(
        _projection_enum_value(getattr(issue, "application_state", None))
        == RepairDecisionApplicationState.UNBUILDABLE.value
    )


def repair_issue_pending_references(issue: Any) -> bool:
    """True when the source is archived but REFERENCES has not caught up.

    This is the single backend definition of the rebuild debt: the Repair Desk
    read model, the review sign-off rule and the publication freeze all use it,
    so no client-side state can make the debt disappear.
    """
    return bool(
        _projection_enum_value(getattr(issue, "repair_state", None))
        == SupplementalSourceRepairState.ARCHIVED_PENDING_REFERENCES.value
    )


def repair_issue_execution_state(
    issue: Any,
    decision: ProductionRepairDecision | None = None,
    *,
    document_missing: bool = False,
) -> RepairIssueExecutionState:
    """Derive the one business truth of an issue: plan, debt and next action.

    Every consumer -- the Repair Desk read model, the issue detail endpoint and
    the sign-off rule -- goes through this function instead of recomputing its
    own answer from the decision action, so an issue can never advertise a
    blocking rebuild together with a plan the desk refuses to show.

    ``document_missing`` is the only row-level fact the issue cannot carry: an
    already-effective decision whose article has no current publication still
    owes a synthesis rebuild.
    """
    effective = decision if decision is not None else getattr(issue, "effective_decision", None)
    is_source = (
        _repair_kind(getattr(issue, "kind", ""))
        is ProductionRepairIssueKind.SUPPLEMENTAL_SOURCE_UNARCHIVED
    )
    pending_references = repair_issue_pending_references(issue)
    application_state = repair_issue_application_state(issue, effective)
    impact = classify_repair_impact(issue, effective)
    blocking = repair_issue_blocks_signoff(issue, effective)
    # An archived source no longer needs an arbitration: what it owes the
    # edition is a REFERENCES reconciliation, tracked as a rebuild debt.
    resolved = effective is not None or pending_references

    if is_source and pending_references:
        # The content exists; only the deterministic REFERENCES rebuild is
        # missing. This debt is served by the backend, so a page reload cannot
        # lose it -- and an older waiver cannot cancel it.
        rebuild_required = blocking
        recommended_stage: str | None = "rebuild_references"
    elif is_source:
        rebuild_required = False
        # The source still needs an explicit archive/waive decision before the
        # deterministic REFERENCES reconciliation can run.
        recommended_stage = "none" if resolved else "rebuild_references"
    elif application_state is RepairDecisionApplicationState.UNRESOLVED:
        rebuild_required = False
        recommended_stage = None
    elif application_state is RepairDecisionApplicationState.UNBUILDABLE:
        # The decision is recorded but nothing materialized it. It stays a
        # blocking debt the analyst clears by revising it to EXCLUDE.
        rebuild_required = blocking
        recommended_stage = "revise_decision"
    elif application_state is RepairDecisionApplicationState.PROJECTION_REQUIRED:
        # Covers the first INCLUDE as well as every revision that makes the
        # applied projection disagree with the effective decision.
        rebuild_required = blocking
        recommended_stage = "apply_projection"
    else:
        rebuild_required = document_missing
        recommended_stage = "synthesis" if document_missing else "none"

    return RepairIssueExecutionState(
        application_state=application_state,
        impact=impact,
        resolved=resolved,
        blocking=blocking,
        rebuild_required=rebuild_required,
        recommended_stage=recommended_stage,
    )


# Name used by Repair Desk consumers that distinguish list DTOs from the
# existing Q2 evidence issue view.
SupplementalSourceRepairIssueView = SupplementalSourceRepairIssue


@dataclass(frozen=True, slots=True)
class _RepairContext:
    run: Any
    artifact: Any
    source_titles: Mapping[str, str]


class ProductionRepairIssueService:
    """Read current repair issues from packs, with a bounded legacy fallback."""

    def __init__(
        self,
        uow_factory: ProductionUnitOfWorkFactory,
        artifact_store: ProductionArtifactStore | None = None,
        payload_resolver: ProductionRepairPayloadResolver | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._artifact_store = artifact_store
        self._payloads = payload_resolver or ProductionRepairPayloadResolver()

    async def list_issues(
        self, edition_id: UUID, subject_id: UUID | None = None
    ) -> tuple[ProductionRepairIssueView, ...]:
        return tuple(
            view for view, _value, _entry in await self._records(edition_id, subject_id=subject_id)
        )

    async def list_issue_views(
        self, edition_id: UUID, subject_id: UUID | None = None
    ) -> tuple[ProductionRepairIssueView, ...]:
        """List bounded issue projections without loading evidence bodies.

        New extraction artifacts carry a compact repair index in metadata.  A
        legacy artifact without that index falls back to its bounded diagnostic
        projection; it is intentionally not promoted to an evidence-pack read.
        The detail endpoint remains the only path that needs the inert value.
        """
        return tuple(
            view
            for view, _value, _entry in await self._records(
                edition_id, subject_id=subject_id, load_payload=False
            )
        )

    async def get_issue(
        self, edition_id: UUID, repair_key: str, subject_id: UUID | None = None
    ) -> ProductionRepairIssueDetail | None:
        """Resolve one issue's exact value, archives included.

        The list stays bounded; only the selected issue is allowed to read a
        historical Q2 output, and only to recover a value whose SHA-256 the
        rejection already recorded.
        """
        for view, value, entry in await self._records(edition_id, subject_id=subject_id):
            if view.repair_key != repair_key:
                continue
            payload = await self._payloads.resolve(
                entry,
                payload_available=value is not None,
                value_sha256=view.value_sha256,
            )
            return ProductionRepairIssueDetail(
                issue=replace(
                    view,
                    payload_available=payload.available,
                    payload_origin=payload.origin,
                ),
                value=payload.value,
                decision_history=await self.decision_history(edition_id, repair_key, subject_id),
            )
        return None

    async def decision_history(
        self, edition_id: UUID, repair_key: str, subject_id: UUID | None = None
    ) -> tuple[ProductionRepairDecision, ...]:
        """Expose the complete append-only audit of one repair identity."""
        async with self._uow_factory() as uow:
            repository = getattr(uow, "production_repair_decisions", None)
            if repository is None:
                return ()
            history = await repository.list_for_edition(edition_id, subject_id)
        return decision_history_for_key(history, repair_key)

    async def resolve_issue(
        self, edition_id: UUID, repair_key: str, subject_id: UUID | None = None
    ) -> ProductionRepairIssueDetail | None:
        """Explicit resolver alias for the future detail endpoint."""
        return await self.get_issue(edition_id, repair_key, subject_id)

    async def list_supplemental_source_issues(
        self, edition_id: UUID, subject_id: UUID | None = None
    ) -> tuple[SupplementalSourceRepairIssue, ...]:
        """Read unarchived Q1 proposals from raw/canonical reference blobs."""
        if self._artifact_store is None:
            return ()

        async with self._uow_factory() as uow:
            runs = await uow.subject_production_runs.list_for_edition(edition_id)
            references_by_run = await _current_artifacts_by_run(
                uow,
                edition_id,
                ProductionArtifactStage.REFERENCES.value,
                runs,
            )
            subject_ids = {run.subject_id for run in runs}
            collections_by_subject: dict[UUID, list[Any]] = {}
            collection_repository = getattr(uow, "source_collections", None)
            bulk_collections = getattr(collection_repository, "list_for_subjects", None)
            if callable(bulk_collections):
                collections = await bulk_collections(subject_ids)
                for collection in collections:
                    collections_by_subject.setdefault(collection.subject_id, []).append(collection)
            elif collection_repository is not None:
                for current_subject_id in subject_ids:
                    collections_by_subject[
                        current_subject_id
                    ] = await collection_repository.list_for_subject(current_subject_id)
            contexts: list[
                tuple[Any, Any, Sequence[Any], tuple[list[dict[str, Any]], set[str]] | None]
            ] = []
            for run in runs:
                if subject_id is not None and run.subject_id != subject_id:
                    continue
                artifact = references_by_run.get(run.id)
                if artifact is None or (
                    _enum_value(artifact.status) == ProductionArtifactStatus.STALE.value
                ):
                    continue
                collections = collections_by_subject.get(run.subject_id, ())
                contexts.append((run, artifact, collections, _reference_source_index(artifact)))
            q2_previews: dict[UUID, dict[str, int]] = {}
            for run, _artifact, collections, source_index in contexts:
                if source_index is None:
                    continue
                proposed_sources, canonical_urls = source_index
                archived_urls = {
                    str(collection.canonical_url)
                    for collection in collections
                    if _enum_value(getattr(collection, "state", None))
                    in {"archived", "extracted", "completed"}
                }
                proposed_urls = {
                    str(item.get("source_url"))
                    for item in proposed_sources
                    if isinstance(item, dict) and item.get("source_url")
                }
                q2_previews[run.id] = await _q2_reuse_preview(
                    uow,
                    run=run,
                    source_urls=sorted((set(canonical_urls) | proposed_urls) & archived_urls),
                    collections=collections,
                )
            decisions = await _effective_decisions_for_reader(uow, edition_id, subject_id)

        decisions_by_key = {
            (decision.subject_id, decision.repair_key): decision for decision in decisions
        }
        issues: list[SupplementalSourceRepairIssue] = []
        preview_assigned: set[UUID] = set()
        for run, artifact, collections, source_index in contexts:
            if source_index is not None:
                proposed_sources, canonical_urls = source_index
            else:
                if artifact.raw_blob_id is None or artifact.canonical_blob_id is None:
                    continue
                research_date = getattr(run, "research_date", None)
                if research_date is None:
                    continue
                try:
                    raw = await self._artifact_store.read_text(artifact.raw_blob_id)
                    proposed_result = parse_reference_report(raw, research_date)
                    if not proposed_result.usable or proposed_result.value is None:
                        continue
                    canonical = reference_report_from_json(
                        await self._artifact_store.read_json(artifact.canonical_blob_id)
                    )
                except Exception:
                    # A read endpoint must not turn one corrupt historical payload
                    # into a 500 for every other Repair Desk issue.
                    continue
                proposed_sources = [
                    {
                        "source_id": source.local_id,
                        "source_title": source.title,
                        "source_url": source.canonical_url,
                        "publisher": source.publisher,
                    }
                    for source in proposed_result.value.sources
                ]
                canonical_urls = {source.canonical_url for source in canonical.sources}
            collections_by_url = {
                collection.canonical_url: collection
                for collection in collections
                if getattr(collection, "canonical_url", None)
            }
            for source in proposed_sources:
                source_url = str(source.get("source_url", ""))
                if not source_url or source_url in canonical_urls:
                    continue
                collection = collections_by_url.get(source_url)
                repair_key = repair_key_for_supplemental_source(
                    edition_id=edition_id,
                    subject_id=run.subject_id,
                    source_url=source_url,
                )
                decision = decisions_by_key.get((run.subject_id, repair_key))
                repair_state, recommended_action = _supplemental_repair_state(collection, decision)
                preview = (
                    q2_previews.get(run.id, {})
                    if (
                        run.id not in preview_assigned
                        and repair_state
                        == SupplementalSourceRepairState.ARCHIVED_PENDING_REFERENCES
                    )
                    else {}
                )
                if preview:
                    preview_assigned.add(run.id)
                issues.append(
                    SupplementalSourceRepairIssue(
                        repair_key=repair_key,
                        kind=ProductionRepairIssueKind.SUPPLEMENTAL_SOURCE_UNARCHIVED,
                        source_id=str(source.get("source_id", "")),
                        source_title=str(source.get("source_title", "")),
                        source_url=source_url,
                        publisher=(
                            str(source["publisher"])
                            if source.get("publisher") is not None
                            else None
                        ),
                        collection_id=(
                            getattr(collection, "id", None) if collection is not None else None
                        ),
                        collection_state=(
                            _enum_value(getattr(collection, "state", None))
                            if collection is not None
                            else None
                        ),
                        error_reason=getattr(collection, "error_reason", None),
                        attempt_count=int(getattr(collection, "attempt_count", 0) or 0),
                        production_run_id=run.id,
                        observed_artifact_id=artifact.id,
                        observed_artifact_version=artifact.version,
                        observed_pipeline_generation=run.pipeline_generation,
                        repair_state=repair_state,
                        rebuild_required=(
                            repair_state
                            is SupplementalSourceRepairState.ARCHIVED_PENDING_REFERENCES
                        ),
                        effective_decision=decision,
                        recommended_action=recommended_action,
                        subject_id=run.subject_id,
                        expected_q2_calls=int(preview.get("expected_q2_calls", 0)),
                        expected_q2_reuses=int(preview.get("expected_q2_reuses", 0)),
                        reuse_unknown_count=int(preview.get("reuse_unknown_count", 0)),
                    )
                )
        return tuple(sorted(issues, key=lambda item: (item.source_url, item.source_id)))

    async def get_supplemental_source_issue(
        self, edition_id: UUID, repair_key: str, subject_id: UUID | None = None
    ) -> SupplementalSourceRepairIssue | None:
        return next(
            (
                issue
                for issue in await self.list_supplemental_source_issues(edition_id, subject_id)
                if issue.repair_key == repair_key
            ),
            None,
        )

    async def list_reference_issues(
        self, edition_id: UUID, subject_id: UUID | None = None
    ) -> tuple[SupplementalSourceRepairIssue, ...]:
        """Naming alias for clients that call all Q1 issues "references"."""
        return await self.list_supplemental_source_issues(edition_id, subject_id)

    async def get_reference_issue(
        self, edition_id: UUID, repair_key: str, subject_id: UUID | None = None
    ) -> SupplementalSourceRepairIssue | None:
        return await self.get_supplemental_source_issue(edition_id, repair_key, subject_id)

    async def _records(
        self,
        edition_id: UUID,
        *,
        subject_id: UUID | None,
        load_payload: bool = True,
    ) -> list[tuple[ProductionRepairIssueView, str | None, Mapping[str, Any]]]:
        async with self._uow_factory() as uow:
            runs = await uow.subject_production_runs.list_for_edition(edition_id)
            artifacts_by_run = await _current_artifacts_by_run(
                uow,
                edition_id,
                ProductionArtifactStage.EXTRACTION.value,
                runs,
            )
            contexts: list[_RepairContext] = []
            for run in runs:
                if subject_id is not None and run.subject_id != subject_id:
                    continue
                artifact = artifacts_by_run.get(run.id)
                if artifact is not None and (
                    _enum_value(artifact.status) != ProductionArtifactStatus.STALE.value
                ):
                    contexts.append(_RepairContext(run=run, artifact=artifact, source_titles={}))
            decisions = await _effective_decisions_for_reader(uow, edition_id, subject_id)

        decisions_by_key = {
            (decision.subject_id, decision.repair_key): decision for decision in decisions
        }
        records: list[tuple[ProductionRepairIssueView, str | None, Mapping[str, Any]]] = []
        for context in contexts:
            entries, payload_available = await self._entries(
                context.artifact, load_payload=load_payload
            )
            marker = _repair_projection_marker(context.artifact)
            for entry in entries:
                record = _issue_record(
                    context,
                    entry,
                    payload_available=payload_available,
                    effective_decision=None,
                    edition_id=edition_id,
                )
                if record is not None:
                    view, value = record
                    decision = decisions_by_key.get((context.run.subject_id, view.repair_key))
                    view = replace(view, effective_decision=decision)
                    records.append(
                        (
                            replace(
                                view,
                                application_state=repair_decision_application_state(
                                    view, decision, marker
                                ),
                                projection_applied=repair_decision_is_materialized(
                                    marker, view.repair_key, decision
                                ),
                            ),
                            value,
                            entry,
                        )
                    )
        return records

    async def _entries(
        self, artifact: Any, *, load_payload: bool = True
    ) -> tuple[list[dict[str, Any]], bool]:
        if load_payload:
            return await _repair_entries_for_artifact(artifact, self._artifact_store)
        return _repair_index_entries_for_artifact(artifact)


@dataclass(frozen=True, slots=True)
class ProductionRepairAdjudicationRequest:
    """One arbitration expressed against what the analyst actually saw."""

    subject_id: UUID
    repair_key: str
    action: ProductionRepairAction
    observed_artifact_id: UUID
    observed_pipeline_generation: int
    #: ``None`` for a first decision, the displayed decision id for a revision.
    expected_effective_decision_id: UUID | None = None
    observed_run_id: UUID | None = None
    replacement_value: str | None = None
    force_override: bool = False


class ProductionRepairAdjudicationService:
    """The single business policy for arbitrating a Repair Desk issue.

    Every endpoint -- subject-scoped, edition-scoped and bulk -- goes through
    this service, so an INCLUDE the deterministic projection could never
    rebuild is refused everywhere and not only where a router remembered to
    check it.  The low-level append service stays responsible for the locks,
    the freeze rules and the optimistic fence.
    """

    def __init__(
        self,
        uow_factory: ProductionUnitOfWorkFactory,
        issue_service: ProductionRepairIssueService | None = None,
        decision_service: ProductionRepairDecisionService | None = None,
        artifact_store: ProductionArtifactStore | None = None,
        payload_resolver: ProductionRepairPayloadResolver | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._artifact_store = artifact_store or getattr(issue_service, "_artifact_store", None)
        # Same issue service, therefore the same resolver: an INCLUDE is only
        # accepted for the exact value the analyst was shown.
        self._issues = issue_service or ProductionRepairIssueService(
            uow_factory, artifact_store, payload_resolver
        )
        self._decisions = decision_service or ProductionRepairDecisionService(uow_factory)

    async def decide_current_issue(
        self,
        *,
        edition_id: UUID,
        subject_id: UUID,
        repair_key: str,
        action: ProductionRepairAction,
        observed_artifact_id: UUID,
        observed_pipeline_generation: int,
        expected_effective_decision_id: UUID | None,
        actor_id: str,
        reason: str | None = None,
        observed_run_id: UUID | None = None,
        replacement_value: str | None = None,
        force_override: bool = False,
    ) -> ProductionRepairDecision:
        """Resolve, validate and append one decision for the CURRENT issue."""
        prepared = await self._prepare(
            edition_id,
            ProductionRepairAdjudicationRequest(
                subject_id=subject_id,
                repair_key=repair_key,
                action=action,
                observed_artifact_id=observed_artifact_id,
                observed_pipeline_generation=observed_pipeline_generation,
                expected_effective_decision_id=expected_effective_decision_id,
                observed_run_id=observed_run_id,
                replacement_value=replacement_value,
                force_override=force_override,
            ),
            actor_id=actor_id,
            reason=reason,
        )
        return await self._decisions.decide(
            edition_id=edition_id,
            subject_id=prepared.subject_id,
            production_run_id=prepared.production_run_id,
            observed_artifact_id=prepared.observed_artifact_id,
            observed_pipeline_generation=prepared.observed_pipeline_generation,
            repair_key=prepared.repair_key,
            issue_kind=prepared.issue_kind,
            action=prepared.action,
            actor_id=actor_id,
            reason=reason,
            expected_effective_decision_id=prepared.expected_effective_decision_id,
            correction=prepared.correction,
        )

    async def decide_current_issues(
        self,
        *,
        edition_id: UUID,
        requests: Sequence[ProductionRepairAdjudicationRequest],
        actor_id: str,
        reason: str | None = None,
    ) -> tuple[ProductionRepairDecision, ...]:
        """Apply the same invariant to a batch, appended in one transaction."""
        prepared: list[ProductionRepairDecisionInput] = []
        for item in requests:
            try:
                prepared.append(
                    await self._prepare(edition_id, item, actor_id=actor_id, reason=reason)
                )
            except ValueError as exc:
                # Name the offending item so a batch refusal stays actionable.
                exc.repair_key = item.repair_key  # type: ignore[attr-defined]
                raise
        return await self._decisions.decide_bulk(
            edition_id=edition_id,
            decisions=prepared,
            actor_id=actor_id,
            reason=reason,
        )

    async def decision_history(
        self, edition_id: UUID, repair_key: str, subject_id: UUID | None = None
    ) -> tuple[ProductionRepairDecision, ...]:
        return await self._decisions.decision_history(edition_id, repair_key, subject_id)

    async def _prepare(
        self,
        edition_id: UUID,
        request: ProductionRepairAdjudicationRequest,
        *,
        actor_id: str = "",
        reason: str | None = None,
    ) -> ProductionRepairDecisionInput:
        detail: ProductionRepairIssueDetail | None = None
        source: SupplementalSourceRepairIssue | None = None
        if request.action is ProductionRepairAction.CONTINUE_WITHOUT_SOURCE:
            source = await self._issues.get_supplemental_source_issue(
                edition_id, request.repair_key, request.subject_id
            )
            issue: Any = source
        else:
            detail = await self._issues.get_issue(
                edition_id, request.repair_key, request.subject_id
            )
            issue = detail.issue if detail is not None else None
        if issue is None:
            raise ProductionRepairIssueNotFoundError(ProductionRepairIssueNotFoundError.code)

        kind = _repair_kind(_enum_value(issue.kind))
        if not _repair_action_is_compatible(kind, request.action):
            raise ProductionRepairActionInvalidError(ProductionRepairActionInvalidError.code)
        if (
            issue.observed_artifact_id != request.observed_artifact_id
            or issue.observed_pipeline_generation != request.observed_pipeline_generation
            or (
                request.observed_run_id is not None
                and issue.production_run_id != request.observed_run_id
            )
            or (getattr(issue, "subject_id", request.subject_id) != request.subject_id)
        ):
            raise ProductionRepairStaleError(ProductionRepairStaleError.code)

        correction: ProductionRepairCorrection | None = None
        if request.action is ProductionRepairAction.INCLUDE:
            self._require_buildable_include(request.repair_key, detail)
        elif request.action is ProductionRepairAction.REPLACE:
            correction = await self._prepare_correction(
                edition_id=edition_id,
                issue=issue,
                detail=detail,
                replacement_value=request.replacement_value,
                force_override=request.force_override,
                actor_id=actor_id,
                reason=reason,
            )

        return ProductionRepairDecisionInput(
            subject_id=request.subject_id,
            production_run_id=issue.production_run_id,
            observed_artifact_id=issue.observed_artifact_id,
            observed_pipeline_generation=issue.observed_pipeline_generation,
            repair_key=request.repair_key,
            issue_kind=kind,
            action=request.action,
            correction=correction,
            expected_effective_decision_id=request.expected_effective_decision_id,
        )

    async def _prepare_correction(
        self,
        *,
        edition_id: UUID,
        issue: Any,
        detail: ProductionRepairIssueDetail | None,
        replacement_value: str | None,
        force_override: bool,
        actor_id: str,
        reason: str | None,
    ) -> ProductionRepairCorrection:
        if detail is None or _repair_kind(issue.kind) not in {
            ProductionRepairIssueKind.REJECTED_INDICATOR,
            ProductionRepairIssueKind.REJECTED_RULE,
        }:
            raise ProductionRepairValueNotVerifiableError(
                ProductionRepairValueNotVerifiableError.code
            )
        value = replacement_value.strip() if isinstance(replacement_value, str) else ""
        if not value:
            raise ProductionRepairValueNotVerifiableError(
                "production_repair_replacement_value_required"
            )
        if force_override and not (reason or "").strip():
            raise ProductionRepairAuditReasonRequiredError(
                ProductionRepairAuditReasonRequiredError.code
            )
        validation = await self._validate_replacement(
            issue=issue,
            value=value,
            require_source=not force_override,
        )
        if not validation.format_valid:
            raise ProductionRepairValueNotVerifiableError(
                validation.reason_code or ProductionRepairValueNotVerifiableError.code
            )
        if not validation.verified and not force_override:
            raise ProductionRepairValueNotVerifiableError(
                validation.reason_code or ProductionRepairValueNotVerifiableError.code
            )
        state = (
            ProductionRepairVerificationState.ANALYST_OVERRIDE
            if force_override
            else ProductionRepairVerificationState.SOURCE_VERIFIED
        )
        artifact_store = self._artifact_store
        if artifact_store is None:
            raise ProductionRepairValueNotVerifiableError("production_repair_storage_unavailable")
        blob_id = await artifact_store.put_text(value, bucket=REPAIR_CORRECTION_BUCKET)
        correction_id = production_repair_correction_identity(
            original_repair_key=issue.repair_key,
            artifact_type=str(issue.artifact_type or ""),
            source_id=issue.source_id,
            source_url=issue.source_url,
            replacement_value_sha256=_sha256(value),
            verification_state=state,
        )
        return ProductionRepairCorrection(
            id=correction_id,
            edition_id=edition_id,
            subject_id=issue.subject_id,
            production_run_id=issue.production_run_id,
            original_repair_key=issue.repair_key,
            artifact_type=str(issue.artifact_type or ""),
            source_id=issue.source_id,
            source_url=issue.source_url,
            replacement_value_sha256=_sha256(value),
            replacement_payload_blob_id=blob_id,
            actor_id=actor_id,
            verification_state=state,
        )

    async def verify_current_issue_replacement(
        self,
        *,
        edition_id: UUID,
        subject_id: UUID,
        repair_key: str,
        replacement_value: str,
    ) -> ProductionRepairCorrectionVerification:
        """Verify a proposed replacement without appending a decision."""
        detail = await self._issues.get_issue(edition_id, repair_key, subject_id)
        if detail is None:
            raise ProductionRepairIssueNotFoundError(ProductionRepairIssueNotFoundError.code)
        if _repair_kind(detail.issue.kind) not in {
            ProductionRepairIssueKind.REJECTED_INDICATOR,
            ProductionRepairIssueKind.REJECTED_RULE,
        }:
            raise ProductionRepairActionInvalidError(ProductionRepairActionInvalidError.code)
        return await self._validate_replacement(
            issue=detail.issue,
            value=replacement_value.strip(),
            require_source=True,
        )

    async def _validate_replacement(
        self,
        *,
        issue: ProductionRepairIssueView,
        value: str,
        require_source: bool,
    ) -> ProductionRepairCorrectionVerification:
        """Use Q2 shape validation and the exact source-evidence gate again."""
        artifact_type = str(issue.artifact_type or "")
        source_id = issue.source_id
        source_url = issue.source_url
        if not value:
            return ProductionRepairCorrectionVerification(
                verified=False,
                replacement_value=value,
                normalized_value=None,
                artifact_type=artifact_type,
                source_id=source_id,
                source_url=source_url,
                format_valid=False,
                reason_code="replacement_value_empty",
            )

        rule_proposal: Q2RuleProposal | None = None
        artifact_proposal: Q2ArtifactProposal | None = None
        if _repair_kind(issue.kind) is ProductionRepairIssueKind.REJECTED_RULE:
            try:
                rule_proposal = Q2RuleProposal(
                    rule_type=_entry_rule_type(artifact_type),
                    body=value,
                    context="",
                    evidence_quote="",
                )
            except (TypeError, ValueError):
                return ProductionRepairCorrectionVerification(
                    verified=False,
                    replacement_value=value,
                    normalized_value=None,
                    artifact_type=artifact_type,
                    source_id=source_id,
                    source_url=source_url,
                    format_valid=False,
                    reason_code="invalid_rule_type",
                )
            output = Q2SourceOutput(rules=[rule_proposal])
            verified_rule_shape = verify_q2_proposals(
                [Q2ProposalSubmission(output=output, source_ids=(source_id,))]
            )
            # Verified rules live in the canonical extraction, exactly like
            # verified artifacts: reading them off the result itself would
            # raise, so a rule correction would never reach the source gate.
            format_valid = len(verified_rule_shape.canonical.rules) == 1
            normalized_value = value if format_valid else None
            if not format_valid:
                reason_code = (
                    verified_rule_shape.rejected[0].reason_code
                    if verified_rule_shape.rejected
                    else "invalid_rule"
                )
                return ProductionRepairCorrectionVerification(
                    verified=False,
                    replacement_value=value,
                    normalized_value=normalized_value,
                    artifact_type=artifact_type,
                    source_id=source_id,
                    source_url=source_url,
                    format_valid=False,
                    reason_code=reason_code,
                )
        else:
            try:
                artifact_enum = _entry_artifact_type(artifact_type)
                artifact_proposal = Q2ArtifactProposal(
                    value=value,
                    artifact_type=artifact_enum.value,
                    indicator_status="confirmed_ioc",
                    context="",
                    evidence_quote="",
                )
            except (TypeError, ValueError):
                return ProductionRepairCorrectionVerification(
                    verified=False,
                    replacement_value=value,
                    normalized_value=None,
                    artifact_type=artifact_type,
                    source_id=source_id,
                    source_url=source_url,
                    format_valid=False,
                    reason_code="invalid_artifact_type",
                )
            verified_shape = verify_q2_proposals(
                [
                    Q2ProposalSubmission(
                        output=Q2SourceOutput(artifacts=[artifact_proposal]),
                        source_ids=(source_id,),
                    )
                ]
            )
            format_valid = len(verified_shape.canonical.items) == 1
            normalized_value = (
                verified_shape.canonical.items[0].normalized_value if format_valid else None
            )
            if not format_valid:
                reason_code = (
                    verified_shape.rejected[0].reason_code
                    if verified_shape.rejected
                    else "invalid_value"
                )
                return ProductionRepairCorrectionVerification(
                    verified=False,
                    replacement_value=value,
                    normalized_value=normalized_value,
                    artifact_type=artifact_type,
                    source_id=source_id,
                    source_url=source_url,
                    format_valid=False,
                    reason_code=reason_code,
                )
            output = Q2SourceOutput(artifacts=[artifact_proposal])

        if not require_source:
            return ProductionRepairCorrectionVerification(
                verified=False,
                replacement_value=value,
                normalized_value=normalized_value,
                artifact_type=artifact_type,
                source_id=source_id,
                source_url=source_url,
                format_valid=True,
                reason_code="source_verification_bypassed",
            )

        document = (
            await self._archived_source_document(issue.subject_id, source_url)
            if issue.subject_id is not None
            else None
        )
        if document is None:
            return ProductionRepairCorrectionVerification(
                verified=False,
                replacement_value=value,
                normalized_value=normalized_value,
                artifact_type=artifact_type,
                source_id=source_id,
                source_url=source_url,
                format_valid=True,
                reason_code="source_evidence_unavailable",
            )
        evidence = verify_ioc_rules_output_against_source(output, document)
        source_verified = bool(evidence.output.artifacts or evidence.output.rules)
        context_spans = (
            source_evidence_context_for_artifact(artifact_proposal, document)
            if artifact_proposal is not None
            else tuple(
                span
                for span in source_evidence_context_for_rule(
                    cast(Q2RuleProposal, rule_proposal), document
                )
                if span.kind is not SourceEvidenceSpanKind.VISUAL_UNLOCATED
            )
        )
        return ProductionRepairCorrectionVerification(
            verified=source_verified,
            replacement_value=value,
            normalized_value=normalized_value,
            artifact_type=artifact_type,
            source_id=source_id,
            source_url=source_url,
            format_valid=True,
            reason_code=(
                None
                if source_verified
                else (
                    evidence.rejections[0].reason_code
                    if evidence.rejections
                    else "source_evidence_missing"
                )
            ),
            context_spans=context_spans,
            verification_state=(
                ProductionRepairVerificationState.SOURCE_VERIFIED if source_verified else None
            ),
        )

    async def _archived_source_document(
        self, subject_id: UUID, source_url: str
    ) -> SourceEvidenceDocument | None:
        if self._artifact_store is None:
            return None
        async with self._uow_factory() as uow:
            collections = await uow.source_collections.list_for_subject(subject_id)
            try:
                expected_url = canonicalize_http_url(source_url)
            except ValueError:
                expected_url = source_url.strip()
            collection = next(
                (
                    item
                    for item in collections
                    if str(getattr(item, "canonical_url", "")) == expected_url
                ),
                None,
            )
            if collection is None:
                return None
            document = None
            source_document_id = getattr(collection, "source_document_id", None)
            if source_document_id is not None:
                document = await uow.source_documents.get(source_document_id)
            blob_id = getattr(collection, "decoded_blob_id", None) or getattr(
                document, "decoded_blob_id", None
            )
            expected_sha256 = getattr(document, "decoded_sha256", None)
            mime_type = getattr(document, "detected_mime_type", None)
            if blob_id is not None:
                blob = await uow.blobs.get(blob_id)
                descriptor = getattr(blob, "descriptor", None)
                mime_type = mime_type or getattr(descriptor, "mime_type", None)
                expected_sha256 = expected_sha256 or getattr(descriptor, "sha256", None)
        if blob_id is None:
            return None
        reader = getattr(self._artifact_store, "read_bytes", None)
        if not callable(reader):
            return None
        try:
            content = await reader(blob_id)
            if expected_sha256 and hashlib.sha256(content).hexdigest() != expected_sha256:
                return None
            detected = DetectedMimeType(
                str(mime_type or DetectedMimeType.HTML.value).split(";", 1)[0]
            )
            parsed = parse_document(content, detected)
            if detected is DetectedMimeType.HTML:
                return source_evidence_document_from_html(
                    parsed.text,
                    content.decode(_html_encoding(content), errors="replace"),
                )
            return SourceEvidenceDocument(parsed_text=parsed.text)
        except (OSError, TypeError, ValueError):
            return None

    @staticmethod
    def _require_buildable_include(
        repair_key: str, detail: ProductionRepairIssueDetail | None
    ) -> None:
        """Refuse, before any append, an INCLUDE nothing could ever rebuild.

        The log is append-only, so an impossible include would otherwise stay
        an unmaterializable debt; refusing here leaves ``exclude`` available
        while the analyst is still looking at the value.
        """
        if detail is None or not detail.payload_available:
            raise ProductionRepairValueNotVerifiableError(
                ProductionRepairValueNotVerifiableError.code
            )
        entry = {
            "repair_key": repair_key,
            "source_id": detail.issue.source_id,
            "source_url": detail.issue.source_url,
            "artifact_type": detail.issue.artifact_type,
            "model_run_id": detail.issue.model_run_id,
        }
        if not repair_include_is_buildable(detail.issue.kind, entry, detail.value):
            raise ProductionRepairValueNotVerifiableError(
                ProductionRepairValueNotVerifiableError.code
            )


def _repair_action_is_compatible(
    kind: ProductionRepairIssueKind, action: ProductionRepairAction
) -> bool:
    """Mirror the domain compatibility rule before a decision is built."""
    if kind is ProductionRepairIssueKind.SUPPLEMENTAL_SOURCE_UNARCHIVED:
        return action is ProductionRepairAction.CONTINUE_WITHOUT_SOURCE
    return action in {
        ProductionRepairAction.INCLUDE,
        ProductionRepairAction.EXCLUDE,
        ProductionRepairAction.REPLACE,
    }


def _fallback_synthesis_projection_hash(extraction: TechnicalExtraction) -> str:
    """Hash extraction's contribution to Q4 when Q1 is unavailable.

    The empty report is intentional: the report is identical before and
    after a Q2 repair, so it is only used as a conservative equality test.
    """
    return _projection_hash(
        synthesis_projection_payload(ReferenceReport(sources=(), events=()), extraction, {})
    )


def _fallback_publication_projection_hash(extraction: TechnicalExtraction) -> str:
    """Hash extraction's publication-visible fields without Q1 payloads."""
    items = [_publication_item_projection(item) for item in extraction.items]
    items.sort(key=lambda item: json.dumps(item, sort_keys=True, separators=(",", ":")))
    return _projection_hash({"items": items, "uncertainties": sorted(extraction.uncertainties)})


def _impact_from_projection_hashes(
    previous: TechnicalExtraction,
    projected: TechnicalExtraction,
    *,
    previous_synthesis_projection_hash: str | None,
    new_synthesis_projection_hash: str | None,
    previous_publication_projection_hash: str | None,
    new_publication_projection_hash: str | None,
    previous_rule_bundle_hash: str,
    new_rule_bundle_hash: str,
) -> ProductionRepairImpact:
    """Classify the actual functional before/after projection delta."""
    if previous == projected:
        return _repair_impact(
            ProductionRepairImpactKind.NO_DELIVERABLE_CHANGE,
            _NO_DELIVERABLE_OUTPUTS,
            model_call_required=False,
            reason="The effective extraction is unchanged.",
        )

    previous_synthesis = previous_synthesis_projection_hash or _fallback_synthesis_projection_hash(
        previous
    )
    new_synthesis = new_synthesis_projection_hash or _fallback_synthesis_projection_hash(projected)
    synthesis_changed = previous_synthesis != new_synthesis

    previous_publication = (
        previous_publication_projection_hash or _fallback_publication_projection_hash(previous)
    )
    new_publication = new_publication_projection_hash or _fallback_publication_projection_hash(
        projected
    )
    publication_changed = previous_publication != new_publication
    rules_changed = previous_rule_bundle_hash != new_rule_bundle_hash

    affected = {
        ProductionDerivedOutput.EXTRACTION,
        ProductionDerivedOutput.CHECKPOINT,
    }
    if rules_changed:
        affected.add(ProductionDerivedOutput.RULE_BUNDLE)
    if synthesis_changed:
        affected.update({ProductionDerivedOutput.SYNTHESIS, ProductionDerivedOutput.PUBLICATION})
        kind = ProductionRepairImpactKind.NARRATIVE
        reason = "The repair changes the evidence consumed by synthesis."
    elif publication_changed:
        affected.add(ProductionDerivedOutput.PUBLICATION)
        kind = ProductionRepairImpactKind.PUBLICATION_ONLY
        reason = "The repair changes publication-visible content but not synthesis evidence."
    elif rules_changed:
        kind = ProductionRepairImpactKind.RULE_BUNDLE_ONLY
        reason = "The repair changes only the accepted detection-rule bundle."
    else:
        # This is defensive for a future extraction field that is not yet
        # represented in either downstream projection.
        kind = ProductionRepairImpactKind.NO_DELIVERABLE_CHANGE
        affected = set()
        reason = "The repair changes no functional deliverable projection."

    return _repair_impact(
        kind,
        frozenset(affected),
        model_call_required=kind is ProductionRepairImpactKind.NARRATIVE,
        reason=reason,
    )


@dataclass(frozen=True, slots=True)
class ProductionRepairProjectionResult:
    """Result of materializing the effective extraction projection."""

    artifact: ProductionArtifact
    changed: bool
    impact: ProductionRepairImpact
    previous_synthesis_projection_hash: str | None = None
    new_synthesis_projection_hash: str | None = None
    previous_publication_projection_hash: str | None = None
    new_publication_projection_hash: str | None = None
    previous_rule_bundle_hash: str | None = None
    new_rule_bundle_hash: str | None = None
    accepted_indicator_count: int = 0
    accepted_rule_count: int = 0
    unresolved_count: int = 0
    included_repair_keys: tuple[str, ...] = ()
    excluded_repair_keys: tuple[str, ...] = ()
    unresolved_repair_keys: tuple[str, ...] = ()
    # INCLUDE decisions the deterministic pipeline cannot rebuild. Recorded,
    # never fatal: the append-only log would otherwise freeze the article.
    unbuildable_repair_keys: tuple[str, ...] = ()
    decision_ids: tuple[str, ...] = ()
    reused_synthesis_artifact_id: UUID | None = None


def _repair_materialization_metadata(
    *,
    impact: ProductionRepairImpact,
    decision_ids: Sequence[str],
    base_extraction_artifact_id: UUID,
    result_extraction_artifact_id: UUID | None = None,
    reused_synthesis_artifact_id: UUID | None = None,
    result_publication_artifact_id: UUID | None = None,
) -> dict[str, Any]:
    """Return the stable audit explanation for one repair materialization.

    This is intentionally metadata, not a mutable status record.  The
    artifact that receives it is immutable; later output versions carry their
    own copy with the IDs that were not known when the extraction was stored.
    """
    return {
        "planner_version": REPAIR_PLANNER_VERSION,
        "impact_kind": impact.kind.value,
        "affected_outputs": sorted(output.value for output in impact.affected_outputs),
        "model_call_required": impact.model_call_required,
        "decision_ids": sorted(set(decision_ids)),
        "base_extraction_artifact_id": str(base_extraction_artifact_id),
        "result_extraction_artifact_id": (
            str(result_extraction_artifact_id)
            if result_extraction_artifact_id is not None
            else None
        ),
        "reused_synthesis_artifact_id": (
            str(reused_synthesis_artifact_id) if reused_synthesis_artifact_id is not None else None
        ),
        "result_publication_artifact_id": (
            str(result_publication_artifact_id)
            if result_publication_artifact_id is not None
            else None
        ),
    }


@dataclass(frozen=True, slots=True)
class EffectiveExtractionProjection:
    """Pure result of applying the still-active repair decisions to Q2."""

    extraction: TechnicalExtraction
    applied_decisions: tuple[dict[str, str], ...]
    unbuildable_decisions: tuple[dict[str, str], ...]
    included_repair_keys: tuple[str, ...]
    excluded_repair_keys: tuple[str, ...]
    unresolved_repair_keys: tuple[str, ...] = ()
    accepted_indicator_count: int = 0
    accepted_rule_count: int = 0
    unbuildable_repair_keys: tuple[str, ...] = ()


class EffectiveExtractionProjector:
    """Apply repair decisions without locks, I/O or artifact persistence.

    Callers must provide entries whose identity has already been derived from
    persisted Q2 evidence.  This keeps source/edition identity resolution in
    the application layer while ensuring the actual merge rules have one home.
    """

    def project(
        self,
        *,
        base: TechnicalExtraction,
        repair_entries: Sequence[Mapping[str, Any]],
        effective_decisions: Sequence[ProductionRepairDecision],
        resolved_payloads: Mapping[str, str],
    ) -> EffectiveExtractionProjection:
        decisions_by_key = {decision.repair_key: decision for decision in effective_decisions}
        active_entries: list[tuple[str, ProductionRepairIssueKind, Mapping[str, Any], str]] = []
        for entry in repair_entries:
            repair_key = entry.get("repair_key")
            value_sha256 = entry.get("value_sha256") or entry.get("value_hash")
            kind_value = entry.get("kind") or (
                ProductionRepairIssueKind.REJECTED_RULE.value
                if entry.get("proposal_kind") == "rule"
                else ProductionRepairIssueKind.REJECTED_INDICATOR.value
            )
            try:
                kind = ProductionRepairIssueKind(str(kind_value))
            except ValueError:
                continue
            if (
                kind
                not in {
                    ProductionRepairIssueKind.REJECTED_INDICATOR,
                    ProductionRepairIssueKind.REJECTED_RULE,
                }
                or not isinstance(repair_key, str)
                or not isinstance(value_sha256, str)
            ):
                continue
            active_entries.append((repair_key, kind, entry, value_sha256.casefold()))

        items = list(base.items)
        rules = list(base.rules)
        included: list[str] = []
        excluded: list[str] = []
        unresolved: list[str] = []
        unbuildable: list[str] = []
        applied_decisions: list[dict[str, str]] = []
        unbuildable_decisions: list[dict[str, str]] = []
        accepted_indicator_count = 0
        accepted_rule_count = 0
        additions: list[ExtractionItem] = []
        rule_additions: list[DetectionRule] = []

        for repair_key, kind, entry, entry_hash in sorted(
            active_entries, key=lambda value: (value[1].value, value[0])
        ):
            decision = decisions_by_key.get(repair_key)
            if decision is None:
                unresolved.append(repair_key)
                continue
            action = _enum_value(decision.action)
            if action == ProductionRepairAction.EXCLUDE.value:
                excluded.append(repair_key)
                applied_decisions.append(
                    {
                        "repair_key": repair_key,
                        "decision_id": str(decision.id),
                        "action": ProductionRepairAction.EXCLUDE.value,
                    }
                )
                continue
            if action not in {
                ProductionRepairAction.INCLUDE.value,
                ProductionRepairAction.REPLACE.value,
            }:
                unresolved.append(repair_key)
                continue
            value = resolved_payloads.get(repair_key)
            if value is None:
                raise ProductionRepairProjectionError("repair_payload_unavailable")
            if _sha256(value) != entry_hash:
                raise ProductionRepairProjectionError("repair_payload_hash_mismatch")
            try:
                if kind is ProductionRepairIssueKind.REJECTED_RULE:
                    rule_additions.append(_build_override_rule(entry, value, repair_key))
                    accepted_rule_count += 1
                else:
                    additions.append(_build_override_item(entry, value, repair_key))
                    if is_publication_ioc_artifact_type(entry.get("artifact_type")):
                        accepted_indicator_count += 1
            except (KeyError, TypeError, ValueError):
                unbuildable.append(repair_key)
                unbuildable_decisions.append(
                    {"repair_key": repair_key, "decision_id": str(decision.id)}
                )
                continue
            included.append(repair_key)
            applied_decisions.append(
                {
                    "repair_key": repair_key,
                    "decision_id": str(decision.id),
                    "action": action,
                }
            )

        return EffectiveExtractionProjection(
            extraction=TechnicalExtraction(
                # _merge_projection_items/_merge_projection_rules deliberately
                # prefer SOURCE_VERIFIED objects over analyst overrides.
                items=_merge_projection_items(items, additions),
                uncertainties=base.uncertainties,
                rules=_merge_projection_rules(rules, rule_additions),
            ),
            applied_decisions=tuple(
                sorted(
                    applied_decisions, key=lambda entry: (entry["repair_key"], entry["decision_id"])
                )
            ),
            unbuildable_decisions=tuple(
                sorted(
                    unbuildable_decisions,
                    key=lambda entry: (entry["repair_key"], entry["decision_id"]),
                )
            ),
            included_repair_keys=tuple(sorted(included)),
            excluded_repair_keys=tuple(sorted(excluded)),
            unresolved_repair_keys=tuple(sorted(unresolved)),
            accepted_indicator_count=accepted_indicator_count,
            accepted_rule_count=accepted_rule_count,
            unbuildable_repair_keys=tuple(sorted(unbuildable)),
        )


class ProductionRepairProjectionService:
    """Build an immutable effective extraction from Q2 plus append-only decisions."""

    _PROJECTION_VERSION = "1"
    _CANONICAL_BUCKET = "production-artifacts-canonical"

    def __init__(
        self,
        uow_factory: ProductionUnitOfWorkFactory,
        artifact_store: ProductionArtifactStore | None = None,
        extraction_service: ExtractionService | None = None,
        payload_resolver: ProductionRepairPayloadResolver | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._artifact_store = artifact_store
        self._extraction = extraction_service or ExtractionService(uow_factory, artifact_store)
        self._payloads = payload_resolver or ProductionRepairPayloadResolver()

    async def _projection_hashes(
        self,
        uow: Any,
        run: Any,
        previous: TechnicalExtraction,
        projected: TechnicalExtraction,
    ) -> dict[str, str | None]:
        """Return functional hashes for the before/after effective outputs."""
        hashes: dict[str, str | None] = {
            "previous_rule_bundle": rule_bundle_projection_hash(previous),
            "new_rule_bundle": rule_bundle_projection_hash(projected),
            "previous_synthesis": None,
            "new_synthesis": None,
            "previous_publication": None,
            "new_publication": None,
            "synthesis_artifact_id": None,
        }
        if self._artifact_store is None:
            return hashes

        references = await uow.production_artifacts.get_current(
            run.id, ProductionArtifactStage.REFERENCES.value
        )
        if references is None or references.canonical_blob_id is None:
            return hashes
        try:
            report = reference_report_from_json(
                await self._artifact_store.read_json(references.canonical_blob_id)
            )
        except Exception:
            return hashes

        source_tiers_by_url: dict[str, str] = {}
        snapshots = getattr(uow, "production_input_snapshots", None)
        snapshot = (
            await snapshots.get_by_run(run.id)
            if snapshots is not None and callable(getattr(snapshots, "get_by_run", None))
            else None
        )
        relevant_urls = {source.canonical_url for source in report.sources}
        if snapshot is not None:
            core_urls = {
                str(source.canonical_url)
                for source in getattr(snapshot, "core_sources", ())
                if getattr(source, "canonical_url", None)
            }
            source_tiers_by_url.update({url: "core" for url in core_urls})
            source_tiers_by_url.update({url: "supporting" for url in relevant_urls - core_urls})
        else:
            collections = getattr(uow, "source_collections", None)
            values = (
                await collections.list_for_subject(run.subject_id)
                if collections is not None
                and callable(getattr(collections, "list_for_subject", None))
                else ()
            )
            for collection in values:
                origin = getattr(collection, "origin_kind", None)
                if origin in {SourceOriginKind.DISCOVERY, SourceOriginKind.MANUAL}:
                    source_tiers_by_url[collection.canonical_url] = "core"
                elif origin is SourceOriginKind.REFERENCE_RESEARCH:
                    source_tiers_by_url[collection.canonical_url] = "supporting"

        hashes["previous_synthesis"] = synthesis_projection_hash(
            report, previous, source_tiers_by_url
        )
        hashes["new_synthesis"] = synthesis_projection_hash(report, projected, source_tiers_by_url)

        synthesis = await uow.production_artifacts.get_current(
            run.id, ProductionArtifactStage.SYNTHESIS.value
        )
        if synthesis is None or synthesis.rendered_blob_id is None:
            return hashes
        hashes["synthesis_artifact_id"] = str(synthesis.id)
        try:
            synthesis_text = await self._artifact_store.read_text(synthesis.rendered_blob_id)
        except Exception:
            return hashes
        hashes["previous_publication"] = publication_projection_hash(
            report, previous, synthesis_text
        )
        hashes["new_publication"] = publication_projection_hash(report, projected, synthesis_text)
        return hashes

    async def project_effective_extraction(
        self,
        run_id: UUID,
        *,
        actor_id: str,
    ) -> ProductionRepairProjectionResult:
        """Project and commit on its own, for callers with no downstream plan.

        The materialization path deliberately does NOT use this wrapper: it
        commits the effective Extraction before the outputs that depend on it
        were invalidated or rebuilt.  Use
        :meth:`project_effective_extraction_in_uow` under the caller's own
        Edition -> Run locks whenever downstream work follows.
        """
        actor_id = actor_id.strip()
        if not actor_id:
            raise ProductionRepairProjectionError("production_repair_actor_required")
        if self._artifact_store is None:
            raise ProductionRepairProjectionError("production_repair_storage_unavailable")

        async with self._uow_factory() as uow:
            # Discover the owner first, then acquire Edition and Run locks in
            # the same order as the other production repair services.
            initial_run = await uow.subject_production_runs.get(run_id)
            if initial_run is None:
                raise ProductionRepairProjectionError("production_run_not_found")

            editions = getattr(uow, "editions", None)
            if editions is not None:
                edition = await _get_for_update(editions, initial_run.edition_id)
                if edition is None:
                    raise ProductionRepairProjectionError("edition_not_found")
                if _enum_value(edition.status) not in {
                    EditionStatus.PRODUCTION.value,
                    EditionStatus.REVIEW.value,
                }:
                    raise ProductionRepairProjectionError("edition_frozen_for_publication")
                manifests = getattr(uow, "publication_manifests", None)
                if (
                    manifests is not None
                    and await manifests.get_latest_for_edition(initial_run.edition_id) is not None
                ):
                    raise ProductionRepairProjectionError("edition_frozen_for_publication")

            run = await _get_for_update(uow.subject_production_runs, run_id)
            if run is None:
                raise ProductionRepairProjectionError("production_run_not_found")
            if run.edition_id != initial_run.edition_id:
                raise ProductionRepairProjectionError("production_run_edition_changed")
            result = await self.project_effective_extraction_in_uow(uow, run=run, actor_id=actor_id)
            await uow.commit()
            return result

    async def project_effective_extraction_in_uow(
        self,
        uow: Any,
        *,
        run: Any,
        actor_id: str,
        expected_pipeline_generation: int | None = None,
    ) -> ProductionRepairProjectionResult:
        """Build the effective Extraction inside the caller's transaction.

        The caller already holds the Edition then Run locks and owns the
        commit.  Nothing here stales or rebuilds a downstream output: the
        caller decides, under the same fence, what the semantic impact
        requires before anything becomes visible.
        """
        actor_id = actor_id.strip()
        if not actor_id:
            raise ProductionRepairProjectionError("production_repair_actor_required")
        if self._artifact_store is None:
            raise ProductionRepairProjectionError("production_repair_storage_unavailable")
        if run is None:
            raise ProductionRepairProjectionError("production_run_not_found")

        if _enum_value(run.status) in {
            SubjectProductionStatus.QUEUED.value,
            SubjectProductionStatus.RUNNING.value,
            SubjectProductionStatus.CANCELLED.value,
        }:
            raise ProductionRepairProjectionError("production_repair_run_not_reviewable")
        if _enum_value(run.status) not in {
            SubjectProductionStatus.READY.value,
            SubjectProductionStatus.NEEDS_REVIEW.value,
            SubjectProductionStatus.FAILED.value,
        }:
            raise ProductionRepairProjectionError("production_repair_run_not_reviewable")
        if getattr(run, "requires_reconciliation", False):
            raise ProductionReconciliationRequiredError
        if (
            expected_pipeline_generation is not None
            and getattr(run, "pipeline_generation", None) != expected_pipeline_generation
        ):
            raise ProductionRepairStaleError(ProductionRepairStaleError.code)

        current = await uow.production_artifacts.get_current(
            run.id, ProductionArtifactStage.EXTRACTION.value
        )
        if current is None or current.canonical_blob_id is None:
            raise ProductionRepairProjectionError("extraction_artifact_not_found")

        base: Any = current
        marker = (
            current.metadata.get("repair_projection")
            if isinstance(getattr(current, "metadata", None), dict)
            else None
        )
        if isinstance(marker, dict):
            base_id = marker.get("base_extraction_artifact_id")
            try:
                base = await uow.production_artifacts.get(UUID(str(base_id)))
            except (TypeError, ValueError):
                base = None
            if base is None:
                raise ProductionRepairProjectionError("repair_projection_base_not_found")
        if base.canonical_blob_id is None:
            raise ProductionRepairProjectionError("extraction_payload_missing")

        try:
            base_extraction = technical_extraction_from_json(
                await self._artifact_store.read_json(base.canonical_blob_id)
            )
        except Exception as exc:
            raise ProductionRepairProjectionError("extraction_payload_unavailable") from exc
        entries, payload_available = await _repair_entries_for_artifact(base, self._artifact_store)
        decisions = await _effective_decisions_for_reader(uow, run.edition_id, run.subject_id)
        decisions_by_key = {
            decision.repair_key: decision
            for decision in decisions
            if decision.subject_id == run.subject_id
        }

        active_entries: list[tuple[str, ProductionRepairIssueKind, dict[str, Any], str]] = []
        for entry in entries:
            identity = _repair_entry_identity(
                entry,
                edition_id=run.edition_id,
                subject_id=run.subject_id,
                payload_available=payload_available,
            )
            if identity is not None:
                active_entries.append((identity[0], identity[1], entry, identity[3]))

        # Resolve every honoured INCLUDE through the SAME resolver the
        # detail and the adjudication used, grouped so each archived Q2
        # output is read and parsed at most once for this projection.
        include_entries = [
            (repair_key, entry, value_sha256)
            for repair_key, _kind, entry, value_sha256 in active_entries
            if (decision := decisions_by_key.get(repair_key)) is not None
            and _enum_value(decision.action)
            in {ProductionRepairAction.INCLUDE.value, ProductionRepairAction.REPLACE.value}
        ]
        original_include_entries = [
            item
            for item in include_entries
            if _enum_value(decisions_by_key[item[0]].action) == ProductionRepairAction.INCLUDE.value
        ]
        include_payload_objects = dict(
            zip(
                (repair_key for repair_key, _entry, _hash in original_include_entries),
                await self._payloads.resolve_many(
                    [entry for _repair_key, entry, _hash in original_include_entries],
                    payload_available=payload_available,
                    value_sha256_by_index={
                        index: value_sha256
                        for index, (_key, _entry, value_sha256) in enumerate(
                            original_include_entries
                        )
                    },
                ),
                strict=True,
            )
        )
        resolved_payloads = {
            repair_key: payload.value
            for repair_key, payload in include_payload_objects.items()
            if payload.available and payload.value is not None
        }
        corrections = getattr(uow, "production_repair_corrections", None)
        for repair_key, _entry, _hash in include_entries:
            decision = decisions_by_key[repair_key]
            if _enum_value(decision.action) != ProductionRepairAction.REPLACE.value:
                continue
            if corrections is None or decision.correction_id is None:
                raise ProductionRepairProjectionError("repair_correction_unavailable")
            correction = await corrections.get(decision.correction_id)
            if correction is None or self._artifact_store is None:
                raise ProductionRepairProjectionError("repair_correction_unavailable")
            try:
                replacement = await self._artifact_store.read_text(
                    correction.replacement_payload_blob_id,
                    max_bytes=MAX_REPAIR_EVIDENCE_BYTES,
                )
            except Exception as exc:
                raise ProductionRepairProjectionError(
                    "repair_correction_payload_unavailable"
                ) from exc
            if _sha256(replacement) != correction.replacement_value_sha256:
                raise ProductionRepairProjectionError("repair_correction_hash_mismatch")
            resolved_payloads[repair_key] = replacement
        projector_entries = []
        for repair_key, kind, entry, value_sha256 in active_entries:
            projected_entry = dict(entry)
            decision = decisions_by_key.get(repair_key)
            projected_basis: str | None = None
            if (
                decision is not None
                and _enum_value(decision.action) == ProductionRepairAction.REPLACE.value
                and corrections is not None
                and decision.correction_id is not None
            ):
                correction = await corrections.get(decision.correction_id)
                if correction is None:
                    raise ProductionRepairProjectionError("repair_correction_unavailable")
                projected_value_hash = correction.replacement_value_sha256
                projected_basis = correction.verification_state.value
            else:
                projected_value_hash = value_sha256
            projected_entry["value_sha256"] = projected_value_hash
            if projected_basis is not None:
                projected_entry["evidence_basis"] = projected_basis
            projector_entries.append(
                projected_entry
                | {
                    "repair_key": repair_key,
                    "kind": kind.value,
                }
            )
        projection = EffectiveExtractionProjector().project(
            base=base_extraction,
            repair_entries=projector_entries,
            effective_decisions=tuple(decisions_by_key.values()),
            resolved_payloads=resolved_payloads,
        )
        projected = projection.extraction
        current_extraction = base_extraction
        if current.id != base.id:
            try:
                current_extraction = technical_extraction_from_json(
                    await self._artifact_store.read_json(current.canonical_blob_id)
                )
            except Exception:
                current_extraction = base_extraction

        projection_hashes = await self._projection_hashes(uow, run, current_extraction, projected)
        impact = _impact_from_projection_hashes(
            current_extraction,
            projected,
            previous_synthesis_projection_hash=projection_hashes["previous_synthesis"],
            new_synthesis_projection_hash=projection_hashes["new_synthesis"],
            previous_publication_projection_hash=projection_hashes["previous_publication"],
            new_publication_projection_hash=projection_hashes["new_publication"],
            previous_rule_bundle_hash=str(projection_hashes["previous_rule_bundle"]),
            new_rule_bundle_hash=str(projection_hashes["new_rule_bundle"]),
        )
        decision_ids = tuple(
            sorted(
                {
                    str(entry["decision_id"])
                    for entry in (
                        *projection.applied_decisions,
                        *projection.unbuildable_decisions,
                    )
                    if entry.get("decision_id")
                }
            )
        )
        reused_synthesis_artifact_id: UUID | None = None
        if projection_hashes.get("synthesis_artifact_id") is not None and (
            projection_hashes["previous_synthesis"] == projection_hashes["new_synthesis"]
        ):
            reused_synthesis_artifact_id = UUID(str(projection_hashes["synthesis_artifact_id"]))

        # An unbuildable INCLUDE changes nothing in the content, but the
        # article owes the record that it was honoured and not applied.
        # Without a new version that debt would be invisible forever.
        unbuildable_already_recorded = {
            (str(entry.get("repair_key")), str(entry.get("decision_id")))
            for entry in _marker_entries(
                _repair_projection_marker(current), "unbuildable_decisions"
            )
        }
        unbuildable_is_recorded = all(
            (entry["repair_key"], entry["decision_id"]) in unbuildable_already_recorded
            for entry in projection.unbuildable_decisions
        )
        if projected == current_extraction and unbuildable_is_recorded:
            return ProductionRepairProjectionResult(
                artifact=current,
                changed=False,
                impact=impact,
                previous_synthesis_projection_hash=projection_hashes["previous_synthesis"],
                new_synthesis_projection_hash=projection_hashes["new_synthesis"],
                previous_publication_projection_hash=projection_hashes["previous_publication"],
                new_publication_projection_hash=projection_hashes["new_publication"],
                previous_rule_bundle_hash=projection_hashes["previous_rule_bundle"],
                new_rule_bundle_hash=projection_hashes["new_rule_bundle"],
                accepted_indicator_count=projection.accepted_indicator_count,
                accepted_rule_count=projection.accepted_rule_count,
                unresolved_count=len(projection.unresolved_repair_keys),
                included_repair_keys=projection.included_repair_keys,
                excluded_repair_keys=projection.excluded_repair_keys,
                unresolved_repair_keys=projection.unresolved_repair_keys,
                unbuildable_repair_keys=projection.unbuildable_repair_keys,
                decision_ids=decision_ids,
                reused_synthesis_artifact_id=reused_synthesis_artifact_id,
            )

        effective_for_base = [
            decision
            for repair_key, _kind, _entry, _hash in active_entries
            if (decision := decisions_by_key.get(repair_key)) is not None
        ]
        effective_for_base.sort(key=lambda item: (item.repair_key, item.id))
        effective_decision_payload = [
            [item.repair_key, _enum_value(item.action), str(item.id)] for item in effective_for_base
        ]
        input_hash = compute_input_hash(
            {
                "repair_projection_version": self._PROJECTION_VERSION,
                "base_extraction_artifact_id": str(base.id),
                "base_input_hash": base.input_hash,
                "effective_decisions": effective_decision_payload,
            }
        )
        canonical_json = technical_extraction_to_json(projected)
        base_metadata = dict(getattr(base, "metadata", {}) or {})
        base_diagnostics = base_metadata.get("deterministic_verification", {})
        projection_metadata = {
            "version": self._PROJECTION_VERSION,
            "base_extraction_artifact_id": str(base.id),
            # ``applied_decisions`` is the only proof that a decision is
            # materialized here; a decision merely considered but not
            # rebuildable lands in ``unbuildable_decisions`` instead.
            "applied_decisions": list(projection.applied_decisions),
            "unbuildable_decisions": list(projection.unbuildable_decisions),
            "included_repair_keys": list(projection.included_repair_keys),
            "excluded_repair_keys": list(projection.excluded_repair_keys),
            "unresolved_repair_keys": list(projection.unresolved_repair_keys),
            "unbuildable_repair_keys": list(projection.unbuildable_repair_keys),
            "actor_id": actor_id,
        }
        metadata: dict[str, Any] = {
            "element_counts": {
                category: len(value)
                for category, value in canonical_json.items()
                if isinstance(value, list)
            },
            "warnings": list(base_metadata.get("warnings", []))
            if isinstance(base_metadata.get("warnings", []), list)
            else [],
            "parser_version": canonical_json.get("parser_version"),
            # Rule counters belong to the effective extraction/rule
            # bundle.  Keeping them here prevents a YARA-only repair from
            # changing the functional publication projection.
            "analyst_override_rule_count": sum(
                rule.evidence_basis is ProductionEvidenceBasis.ANALYST_OVERRIDE
                for rule in projected.rules
            ),
            "generated_at": datetime.now(UTC).isoformat(),
            # These diagnostics describe BASE, never a fresh model call.
            "deterministic_verification": dict(base_diagnostics)
            if isinstance(base_diagnostics, dict)
            else {},
            "repair_projection": projection_metadata,
            "projection_diagnostics_basis": "base_extraction",
        }
        metadata["repair_materialization"] = _repair_materialization_metadata(
            impact=impact,
            decision_ids=decision_ids,
            base_extraction_artifact_id=base.id,
            reused_synthesis_artifact_id=reused_synthesis_artifact_id,
        )
        if isinstance(base_metadata.get("repair_evidence"), dict):
            metadata["repair_evidence"] = dict(base_metadata["repair_evidence"])

        artifact = await self._extraction._store_repair_projection_in_uow(
            uow,
            run_id=run.id,
            subject_id=run.subject_id,
            input_hash=input_hash,
            canonical_json=canonical_json,
            metadata=metadata,
        )
        return ProductionRepairProjectionResult(
            artifact=artifact,
            changed=True,
            impact=impact,
            previous_synthesis_projection_hash=projection_hashes["previous_synthesis"],
            new_synthesis_projection_hash=projection_hashes["new_synthesis"],
            previous_publication_projection_hash=projection_hashes["previous_publication"],
            new_publication_projection_hash=projection_hashes["new_publication"],
            previous_rule_bundle_hash=projection_hashes["previous_rule_bundle"],
            new_rule_bundle_hash=projection_hashes["new_rule_bundle"],
            accepted_indicator_count=projection.accepted_indicator_count,
            accepted_rule_count=projection.accepted_rule_count,
            unresolved_count=len(projection.unresolved_repair_keys),
            included_repair_keys=projection.included_repair_keys,
            excluded_repair_keys=projection.excluded_repair_keys,
            unresolved_repair_keys=projection.unresolved_repair_keys,
            unbuildable_repair_keys=projection.unbuildable_repair_keys,
            decision_ids=decision_ids,
            reused_synthesis_artifact_id=reused_synthesis_artifact_id,
        )


async def reconcile_effective_repairs_in_uow(
    uow: Any,
    *,
    run: Any,
    base_extraction_artifact: ProductionArtifact,
    artifact_store: ProductionArtifactStore | None,
    payload_resolver: ProductionRepairPayloadResolver | None = None,
    actor_id: str = "system:repair-replay",
) -> ProductionArtifact | None:
    """Reconcile decisions after Q2 while the workflow owns the transaction.

    This deliberately does not inspect or mutate run status, pipeline
    generation, conversations, or review fences.  It is the internal
    transaction primitive for a live RUNNING workflow; the review-facing
    ``ProductionRepairProjectionService`` remains fenced separately.
    """
    actor_id = actor_id.strip()
    if not actor_id:
        raise ProductionRepairProjectionError("production_repair_actor_required")
    if artifact_store is None or base_extraction_artifact.canonical_blob_id is None:
        raise ProductionRepairProjectionError("production_repair_storage_unavailable")

    base = technical_extraction_from_json(
        await artifact_store.read_json(base_extraction_artifact.canonical_blob_id)
    )
    entries, payload_available = await _repair_entries_for_artifact(
        base_extraction_artifact, artifact_store
    )
    decisions = tuple(
        decision
        for decision in await _effective_decisions_for_reader(uow, run.edition_id, run.subject_id)
        if decision.subject_id == run.subject_id
    )
    active_entries: list[tuple[str, ProductionRepairIssueKind, dict[str, Any], str]] = []
    for entry in entries:
        identity = _repair_entry_identity(
            entry,
            edition_id=run.edition_id,
            subject_id=run.subject_id,
            payload_available=payload_available,
        )
        if identity is not None:
            active_entries.append((identity[0], identity[1], entry, identity[3]))

    active_keys = {repair_key for repair_key, _kind, _entry, _hash in active_entries}
    superseded = tuple(
        decision
        for decision in decisions
        if decision.issue_kind
        in {
            ProductionRepairIssueKind.REJECTED_INDICATOR,
            ProductionRepairIssueKind.REJECTED_RULE,
        }
        and decision.repair_key not in active_keys
    )
    if superseded:
        audit = getattr(uow, "edition_audit", None)
        append_audit = getattr(audit, "append", None)
        if callable(append_audit):
            for decision in superseded:
                await append_audit(
                    EditionAuditEvent(
                        edition_id=run.edition_id,
                        actor_id=actor_id,
                        action="production.repair_decision_superseded_by_new_extraction",
                        before=None,
                        after={
                            "subject_id": str(run.subject_id),
                            "production_run_id": str(run.id),
                            "decision_id": str(decision.id),
                            "repair_key": decision.repair_key,
                            "reason": "superseded by new extraction",
                            "base_extraction_artifact_id": str(base_extraction_artifact.id),
                        },
                        correlation_id="production-repair-replay",
                    )
                )

    include_entries = [
        (repair_key, entry, value_sha256)
        for repair_key, _kind, entry, value_sha256 in active_entries
        if any(
            decision.repair_key == repair_key
            and _enum_value(decision.action)
            in {ProductionRepairAction.INCLUDE.value, ProductionRepairAction.REPLACE.value}
            for decision in decisions
        )
    ]
    original_include_entries = [
        item
        for item in include_entries
        if any(
            decision.repair_key == item[0]
            and _enum_value(decision.action) == ProductionRepairAction.INCLUDE.value
            for decision in decisions
        )
    ]
    resolver = payload_resolver or ProductionRepairPayloadResolver()
    payload_objects = dict(
        zip(
            (repair_key for repair_key, _entry, _hash in original_include_entries),
            await resolver.resolve_many(
                [entry for _key, entry, _hash in original_include_entries],
                payload_available=payload_available,
                value_sha256_by_index={
                    index: value_sha256
                    for index, (_key, _entry, value_sha256) in enumerate(original_include_entries)
                },
            ),
            strict=True,
        )
    )
    resolved_payloads = {
        repair_key: payload.value
        for repair_key, payload in payload_objects.items()
        if payload.available and payload.value is not None
    }
    correction_repository = getattr(uow, "production_repair_corrections", None)
    decisions_by_key = {decision.repair_key: decision for decision in decisions}
    for repair_key, _entry, _hash in include_entries:
        decision = decisions_by_key[repair_key]
        if _enum_value(decision.action) != ProductionRepairAction.REPLACE.value:
            continue
        if correction_repository is None or decision.correction_id is None:
            raise ProductionRepairProjectionError("repair_correction_unavailable")
        correction = await correction_repository.get(decision.correction_id)
        if correction is None:
            raise ProductionRepairProjectionError("repair_correction_unavailable")
        try:
            replacement = await artifact_store.read_text(
                correction.replacement_payload_blob_id,
                max_bytes=MAX_REPAIR_EVIDENCE_BYTES,
            )
        except Exception as exc:
            raise ProductionRepairProjectionError("repair_correction_payload_unavailable") from exc
        if _sha256(replacement) != correction.replacement_value_sha256:
            raise ProductionRepairProjectionError("repair_correction_hash_mismatch")
        resolved_payloads[repair_key] = replacement
    replay_entries = []
    for repair_key, kind, entry, value_sha256 in active_entries:
        effective = decisions_by_key.get(repair_key)
        projected_value_hash = value_sha256
        projected_basis: str | None = None
        if (
            effective is not None
            and _enum_value(effective.action) == ProductionRepairAction.REPLACE.value
        ):
            if correction_repository is None or effective.correction_id is None:
                raise ProductionRepairProjectionError("repair_correction_unavailable")
            correction = await correction_repository.get(effective.correction_id)
            if correction is None:
                raise ProductionRepairProjectionError("repair_correction_unavailable")
            projected_value_hash = correction.replacement_value_sha256
            projected_basis = correction.verification_state.value
        replay_entry = dict(entry)
        replay_entry["value_sha256"] = projected_value_hash
        if projected_basis is not None:
            replay_entry["evidence_basis"] = projected_basis
        replay_entries.append(
            replay_entry
            | {
                "repair_key": repair_key,
                "kind": kind.value,
            }
        )
    projected = EffectiveExtractionProjector().project(
        base=base,
        repair_entries=replay_entries,
        effective_decisions=decisions,
        resolved_payloads=resolved_payloads,
    )
    if projected.extraction == base:
        return None

    effective_decision_payload = [
        [decision.repair_key, _enum_value(decision.action), str(decision.id)]
        for decision in sorted(
            decisions,
            key=lambda item: (item.repair_key, str(item.id)),
        )
        if decision.repair_key in active_keys
    ]
    input_hash = compute_input_hash(
        {
            "repair_projection_version": "1",
            "base_extraction_artifact_id": str(base_extraction_artifact.id),
            "base_input_hash": base_extraction_artifact.input_hash,
            "effective_decisions": effective_decision_payload,
            "replay_origin": "post_q2_reconciliation",
        }
    )
    canonical_json = technical_extraction_to_json(projected.extraction)
    base_metadata = dict(getattr(base_extraction_artifact, "metadata", {}) or {})
    replay_impact = _impact_from_projection_hashes(
        base,
        projected.extraction,
        previous_synthesis_projection_hash=None,
        new_synthesis_projection_hash=None,
        previous_publication_projection_hash=None,
        new_publication_projection_hash=None,
        previous_rule_bundle_hash=rule_bundle_projection_hash(base),
        new_rule_bundle_hash=rule_bundle_projection_hash(projected.extraction),
    )
    replay_decision_ids = tuple(
        sorted(
            {
                str(entry["decision_id"])
                for entry in (
                    *projected.applied_decisions,
                    *projected.unbuildable_decisions,
                )
                if entry.get("decision_id")
            }
        )
    )
    projection_metadata = {
        "version": "1",
        "base_extraction_artifact_id": str(base_extraction_artifact.id),
        "derived_repair": True,
        "replay_origin": "post_q2_reconciliation",
        "applied_decisions": list(projected.applied_decisions),
        "unbuildable_decisions": list(projected.unbuildable_decisions),
        "included_repair_keys": list(projected.included_repair_keys),
        "excluded_repair_keys": list(projected.excluded_repair_keys),
        "unresolved_repair_keys": list(projected.unresolved_repair_keys),
        "unbuildable_repair_keys": list(projected.unbuildable_repair_keys),
        "superseded_decisions": [
            {
                "decision_id": str(decision.id),
                "repair_key": decision.repair_key,
                "reason": "superseded by new extraction",
            }
            for decision in superseded
        ],
        "actor_id": actor_id,
    }
    metadata: dict[str, Any] = {
        "element_counts": {
            category: len(value)
            for category, value in canonical_json.items()
            if isinstance(value, list)
        },
        "warnings": list(base_metadata.get("warnings", []))
        if isinstance(base_metadata.get("warnings", []), list)
        else [],
        "parser_version": canonical_json.get("parser_version"),
        "generated_at": datetime.now(UTC).isoformat(),
        "deterministic_verification": dict(base_metadata.get("deterministic_verification", {}))
        if isinstance(base_metadata.get("deterministic_verification"), dict)
        else {},
        "repair_projection": projection_metadata,
        "derived_repair": True,
        "base_extraction_artifact_id": str(base_extraction_artifact.id),
        "applied_decisions": list(projected.applied_decisions),
        "replay_origin": "post_q2_reconciliation",
        "projection_diagnostics_basis": "base_extraction",
        "repair_materialization": _repair_materialization_metadata(
            impact=replay_impact,
            decision_ids=replay_decision_ids,
            base_extraction_artifact_id=base_extraction_artifact.id,
        ),
    }
    if isinstance(base_metadata.get("repair_evidence"), dict):
        metadata["repair_evidence"] = dict(base_metadata["repair_evidence"])

    extraction_service = ExtractionService(cast(Any, lambda: None), artifact_store)
    return await extraction_service._store_repair_projection_in_uow(
        uow,
        run_id=run.id,
        subject_id=run.subject_id,
        input_hash=input_hash,
        canonical_json=canonical_json,
        metadata=metadata,
    )


@dataclass(frozen=True, slots=True)
class ProductionRepairMaterializationResult:
    """Durable outcome of applying a repair projection to derived products."""

    projection: ProductionRepairProjectionResult
    action: str
    retry_stage: str | None = None
    full_chain: bool = False
    publication_artifact: ProductionArtifact | None = None
    qa: dict[str, Any] | None = None
    repair_materialization: dict[str, Any] | None = None

    @property
    def artifact(self) -> ProductionArtifact:
        return self.projection.artifact

    @property
    def impact(self) -> ProductionRepairImpact:
        return self.projection.impact

    @property
    def changed(self) -> bool:
        return self.projection.changed


def _repair_artifacts_updated(
    result: ProductionRepairMaterializationResult,
) -> list[str]:
    """Name the artifacts an application really rewrote, never those it planned.

    ``affected_outputs`` is a plan; this is the observed outcome, so a
    diagnostic can be read as proof that an IOC repair left SYNTHESIS alone.
    """
    updated: list[str] = []
    if result.changed and result.action not in {"none", "awaiting_repair_decision"}:
        updated.append(ProductionArtifactStage.EXTRACTION.value)
    if result.publication_artifact is not None:
        updated.append(ProductionArtifactStage.PUBLICATION.value)
    if result.action == "rules_materialized":
        updated.append(ProductionDerivedOutput.RULE_BUNDLE.value)
    if result.retry_stage is not None:
        # A retry stales an output instead of rewriting it: the analyst is
        # told which stage now owes a rebuild.
        updated.append(f"stale:{result.retry_stage}")
    return updated


class ProductionRepairMaterializationService:
    """Apply only the downstream work required by a semantic repair impact."""

    def __init__(
        self,
        uow_factory: ProductionUnitOfWorkFactory,
        projection_service: ProductionRepairProjectionService | None = None,
        publication_assembly_service: PublicationAssemblyService | None = None,
        qa_service: ProductionQAService | None = None,
        checkpoint_service: Any | None = None,
        artifact_store: ProductionArtifactStore | None = None,
        diagnostics: DiagnosticsLog | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._projection = projection_service or ProductionRepairProjectionService(
            uow_factory, artifact_store
        )
        resolved_store = artifact_store or getattr(self._projection, "_artifact_store", None)
        self._artifact_store = resolved_store
        self._assembly = publication_assembly_service or PublicationAssemblyService(
            uow_factory, resolved_store
        )
        self._qa = qa_service or ProductionQAService(uow_factory)
        self._checkpoint = checkpoint_service
        self._diagnostics = diagnostics or DiagnosticsLog(None)

    async def apply(
        self,
        *,
        edition_id: UUID,
        subject_id: UUID,
        actor_id: str,
        observed_run_id: UUID | None = None,
        observed_pipeline_generation: int | None = None,
    ) -> ProductionRepairMaterializationResult:
        """Apply a repair and record exactly what it changed.

        The outcome is recorded here rather than at each early return, so
        ``production.repair.applied`` is emitted once per application and
        always describes the result the caller receives.
        """
        started = perf_counter()
        result = await self._apply(
            edition_id=edition_id,
            subject_id=subject_id,
            actor_id=actor_id,
            observed_run_id=observed_run_id,
            observed_pipeline_generation=observed_pipeline_generation,
            started=started,
        )
        self._diagnostics.record(
            event="production.repair.applied",
            run_id=result.projection.artifact.production_run_id,
            subject_id=subject_id,
            stage="repair",
            impact_kind=result.impact.kind.value,
            action=result.action,
            projection_changed=result.changed,
            artifacts_updated=_repair_artifacts_updated(result),
            model_call_required=result.impact.model_call_required,
            decision_count=len(result.projection.decision_ids),
            duration_ms=max(0, round((perf_counter() - started) * 1000)),
        )
        return result

    async def _apply(
        self,
        *,
        edition_id: UUID,
        subject_id: UUID,
        actor_id: str,
        observed_run_id: UUID | None,
        observed_pipeline_generation: int | None,
        started: float,
    ) -> ProductionRepairMaterializationResult:
        """Apply a repair as a single transactional change to the deliverable.

        One transaction and one fence: the optimistic generation check, the
        effective Extraction, the selective stale, the deterministic assembly
        and the QA all run under the same Edition -> Run locks.  A repaired
        Extraction is therefore never committed as "applied" while the DB
        outputs it invalidates are still the previous ones, and a freeze
        running concurrently observes either the whole repair or none of it.
        """
        actor_id = actor_id.strip()
        if not actor_id:
            raise ProductionRepairProjectionError("production_repair_actor_required")

        async with self._uow_factory() as uow:
            run = await self._locked_run(
                uow,
                edition_id=edition_id,
                subject_id=subject_id,
                observed_run_id=observed_run_id,
                observed_pipeline_generation=observed_pipeline_generation,
            )
            projection = await self._projection.project_effective_extraction_in_uow(
                uow,
                run=run,
                actor_id=actor_id,
                expected_pipeline_generation=run.pipeline_generation,
            )
            repair_audit = self._repair_audit(projection)
            self._record_repair_diagnostic(
                event="production.repair.plan",
                run=run,
                projection=projection,
                started=started,
                reused_synthesis=projection.reused_synthesis_artifact_id is not None,
            )
            self._record_repair_diagnostic(
                event="production.repair.projection_completed",
                run=run,
                projection=projection,
                started=started,
                reused_synthesis=projection.reused_synthesis_artifact_id is not None,
            )
            if projection.unresolved_count:
                await uow.commit()
                return ProductionRepairMaterializationResult(
                    projection=projection,
                    action="awaiting_repair_decision",
                    repair_materialization=repair_audit,
                )

            impact = projection.impact
            if (
                not projection.changed
                or impact.kind is ProductionRepairImpactKind.NO_DELIVERABLE_CHANGE
            ):
                await uow.commit()
                return ProductionRepairMaterializationResult(
                    projection=projection,
                    action="none",
                    repair_materialization=repair_audit,
                )

            if impact.kind is ProductionRepairImpactKind.SOURCE_CORPUS:
                self._record_repair_diagnostic(
                    event="production.repair.model_retry_requested",
                    run=run,
                    projection=projection,
                    started=started,
                    reused_synthesis=False,
                )
                await uow.commit()
                return ProductionRepairMaterializationResult(
                    projection=projection,
                    action="retry_required",
                    retry_stage=SubjectProductionStage.REFERENCES.value,
                    full_chain=True,
                    repair_materialization=repair_audit,
                )

            publication: ProductionArtifact | None = None
            qa_result: dict[str, Any] | None = None
            if impact.kind is ProductionRepairImpactKind.PUBLICATION_ONLY:
                publication, qa_result = await self._materialize_publication_in_uow(
                    uow,
                    run=run,
                    extraction=projection.artifact,
                    repair_materialization=repair_audit,
                )
                repair_audit["result_publication_artifact_id"] = str(publication.id)
                self._record_repair_diagnostic(
                    event="production.repair.publication_reassembled",
                    run=run,
                    projection=projection,
                    started=started,
                    reused_synthesis=True,
                )
            elif impact.kind is ProductionRepairImpactKind.RULE_BUNDLE_ONLY:
                # Synthesis and Publication stay semantically valid, so the
                # QA runs against the outputs that remain current.
                qa_result = await self._qa_current_outputs_in_uow(
                    uow, run=run, extraction=projection.artifact
                )
            else:
                await self._mark_stages_stale(
                    uow,
                    run.id,
                    {
                        ProductionArtifactStage.SYNTHESIS.value,
                        ProductionArtifactStage.PUBLICATION.value,
                    },
                )
                self._record_repair_diagnostic(
                    event="production.repair.model_retry_requested",
                    run=run,
                    projection=projection,
                    started=started,
                    reused_synthesis=False,
                )
                # The run no longer has the deliverable it claimed: it leaves
                # READY in the same transaction as the stale, so no reader ever
                # observes a READY run without a current PUBLICATION.
                await _require_publication_rebuild(
                    uow, run, retry_stage=SubjectProductionStage.SYNTHESIS.value
                )
                # The stale is committed with the new Extraction, so no reader
                # ever sees the repaired content beside the old narrative.
                await uow.commit()
                return ProductionRepairMaterializationResult(
                    projection=projection,
                    action="retry_required",
                    retry_stage=SubjectProductionStage.SYNTHESIS.value,
                    repair_materialization=repair_audit,
                )

            await uow.commit()

        if impact.kind is ProductionRepairImpactKind.RULE_BUNDLE_ONLY:
            # The canonical Extraction already carries the decision.  The
            # filesystem sidecars are a disposable projection of it, so a
            # failure here is reported as pending, never as materialized.
            materialized = await self.materialize_rule_bundle_from_current_extraction(run.id)
            action = "rules_materialized" if materialized else "rules_projection_pending"
            self._record_repair_diagnostic(
                event=(
                    "production.repair.rule_bundle_materialized"
                    if materialized
                    else "production.repair.rule_bundle_projection_pending"
                ),
                run=run,
                projection=projection,
                started=started,
                reused_synthesis=True,
            )
        else:
            action = "publication_reassembled"
            if self._checkpoint is not None:
                await self._checkpoint.checkpoint(run.id)
        return ProductionRepairMaterializationResult(
            projection=projection,
            action=action,
            publication_artifact=publication,
            qa=qa_result,
            repair_materialization=repair_audit,
        )

    async def materialize_rule_bundle_from_current_extraction(self, run_id: UUID) -> bool:
        """Rebuild the rule sidecars from the current canonical Extraction.

        Idempotent: the checkpoint recomputes the whole workspace projection
        from the artifacts that are current now, so replaying it after a
        failure repeats work rather than losing or duplicating a decision.
        Returns whether the sidecars are materialized; the canonical
        Extraction remains the authority in both cases.  A deployment with no
        checkpoint service owes no sidecar at all and is reported as done.
        """
        if self._checkpoint is None:
            return True
        materialization = await self._checkpoint.checkpoint(run_id)
        if materialization is None:
            return False
        return getattr(materialization, "rule_sidecar_error", None) is None

    @staticmethod
    def _repair_audit(projection: ProductionRepairProjectionResult) -> dict[str, Any]:
        base_artifact_id_value: Any = projection.artifact.id
        projection_audit = (
            projection.artifact.metadata.get("repair_materialization")
            if isinstance(projection.artifact.metadata, dict)
            else None
        )
        if isinstance(projection_audit, dict):
            base_artifact_id_value = projection_audit.get(
                "base_extraction_artifact_id", base_artifact_id_value
            )
        try:
            base_artifact_id = UUID(str(base_artifact_id_value))
        except (TypeError, ValueError):
            base_artifact_id = projection.artifact.id
        return _repair_materialization_metadata(
            impact=projection.impact,
            decision_ids=projection.decision_ids,
            base_extraction_artifact_id=base_artifact_id,
            result_extraction_artifact_id=projection.artifact.id,
            reused_synthesis_artifact_id=projection.reused_synthesis_artifact_id,
        )

    def _record_repair_diagnostic(
        self,
        *,
        event: str,
        run: Any,
        projection: ProductionRepairProjectionResult,
        started: float,
        reused_synthesis: bool,
    ) -> None:
        self._diagnostics.record(
            event=event,
            run_id=run.id,
            subject_id=run.subject_id,
            stage="repair",
            impact_kind=projection.impact.kind.value,
            decision_count=len(projection.decision_ids),
            affected_outputs=sorted(output.value for output in projection.impact.affected_outputs),
            model_call_required=projection.impact.model_call_required,
            reused_synthesis=reused_synthesis,
            duration_ms=max(0, round((perf_counter() - started) * 1000)),
        )

    async def _locked_run(
        self,
        uow: Any,
        *,
        edition_id: UUID,
        subject_id: UUID,
        observed_run_id: UUID | None,
        observed_pipeline_generation: int | None,
    ) -> Any:
        """Take the Edition then Run locks and hold them for the whole plan.

        The lock order matches the publication freeze exactly, and the
        optimistic generation check is re-evaluated once the Run row is
        locked: releasing that lock before the writes would make the check
        worthless.
        """
        runs = uow.subject_production_runs
        initial = (
            await runs.get(observed_run_id)
            if observed_run_id is not None
            else await runs.get_current_for_subject(subject_id)
        )
        if initial is None:
            raise ProductionRepairProjectionError("production_run_not_found")
        if initial.edition_id != edition_id or initial.subject_id != subject_id:
            raise ProductionRepairProjectionError("production_run_edition_changed")
        if (observed_run_id is not None and initial.id != observed_run_id) or (
            observed_pipeline_generation is not None
            and initial.pipeline_generation != observed_pipeline_generation
        ):
            raise ProductionRepairStaleError(ProductionRepairStaleError.code)
        await self._lock_edition_and_check(uow, edition_id)
        run: Any = await _get_for_update(runs, initial.id)
        self._check_reviewable_run(run, edition_id, subject_id)
        if (
            observed_pipeline_generation is not None
            and run.pipeline_generation != observed_pipeline_generation
        ):
            raise ProductionRepairStaleError(ProductionRepairStaleError.code)
        return run

    async def _lock_edition_and_check(self, uow: Any, edition_id: UUID) -> Any:
        editions = getattr(uow, "editions", None)
        if editions is None:
            return None
        edition = await _get_for_update(editions, edition_id)
        if edition is None:
            raise ProductionRepairProjectionError("edition_not_found")
        if _enum_value(getattr(edition, "status", None)) not in {
            EditionStatus.PRODUCTION.value,
            EditionStatus.REVIEW.value,
        }:
            raise ProductionRepairProjectionError("edition_frozen_for_publication")
        manifests = getattr(uow, "publication_manifests", None)
        if manifests is not None and await manifests.get_latest_for_edition(edition_id) is not None:
            raise ProductionRepairProjectionError("edition_frozen_for_publication")
        return edition

    @staticmethod
    def _check_reviewable_run(run: Any, edition_id: UUID, subject_id: UUID) -> None:
        if run is None:
            raise ProductionRepairProjectionError("production_run_not_found")
        if run.edition_id != edition_id or run.subject_id != subject_id:
            raise ProductionRepairProjectionError("production_run_edition_changed")
        if _enum_value(getattr(run, "status", None)) not in {
            SubjectProductionStatus.READY.value,
            SubjectProductionStatus.NEEDS_REVIEW.value,
            SubjectProductionStatus.FAILED.value,
        }:
            raise ProductionRepairProjectionError("production_repair_run_not_reviewable")
        if getattr(run, "requires_reconciliation", False):
            raise ProductionReconciliationRequiredError

    async def _materialize_publication_in_uow(
        self,
        uow: Any,
        *,
        run: Any,
        extraction: ProductionArtifact,
        repair_materialization: Mapping[str, Any] | None = None,
    ) -> tuple[ProductionArtifact, dict[str, Any] | None]:
        """Stale, reassemble and QA the publication in the caller's UoW.

        The old Publication row, the new one and the repaired Extraction are
        one change: an Assembly or QA failure raises, the caller never
        commits, and the article stays exactly as it was before the repair.
        """
        run_id = run.id
        references = await uow.production_artifacts.get_current(
            run_id, ProductionArtifactStage.REFERENCES.value
        )
        # PUBLICATION_ONLY deliberately reuses the current Synthesis: its
        # functional projection is unchanged, so no model call is owed.
        synthesis = await uow.production_artifacts.get_current(
            run_id, ProductionArtifactStage.SYNTHESIS.value
        )
        if references is None or synthesis is None:
            raise ProductionRepairProjectionError("publication_inputs_missing")
        publication = await uow.production_artifacts.get_current(
            run_id, ProductionArtifactStage.PUBLICATION.value
        )
        if publication is not None:
            await self._mark_stages_stale(uow, run_id, {ProductionArtifactStage.PUBLICATION.value})
        title = await self._subject_title(uow, run_id, run.subject_id)
        assembly_kwargs: dict[str, Any] = (
            {"metadata_extra": {"repair_materialization": dict(repair_materialization)}}
            if repair_materialization is not None
            else {}
        )
        try:
            parameters: Mapping[str, inspect.Parameter]
            parameters = inspect.signature(self._assembly.assemble_publication_in_uow).parameters
        except (TypeError, ValueError):
            parameters = {}
        accepts_metadata = "metadata_extra" in parameters or any(
            parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters.values()
        )
        if not accepts_metadata:
            assembly_kwargs = {}
        new_publication = await self._assembly.assemble_publication_in_uow(
            uow,
            run_id,
            run.subject_id,
            title,
            references,
            extraction,
            synthesis,
            **assembly_kwargs,
        )
        qa_result = await self._run_qa(
            uow,
            run_id,
            references,
            extraction,
            synthesis,
            new_publication,
            run.subject_id,
            run,
        )
        await self._ensure_qa_passed(qa_result)
        return new_publication, qa_result

    async def _qa_current_outputs_in_uow(
        self,
        uow: Any,
        *,
        run: Any,
        extraction: ProductionArtifact,
    ) -> dict[str, Any] | None:
        """QA the repaired Extraction against the outputs that stay current."""
        run_id = run.id
        references = await uow.production_artifacts.get_current(
            run_id, ProductionArtifactStage.REFERENCES.value
        )
        synthesis = await uow.production_artifacts.get_current(
            run_id, ProductionArtifactStage.SYNTHESIS.value
        )
        publication = await uow.production_artifacts.get_current(
            run_id, ProductionArtifactStage.PUBLICATION.value
        )
        if references is None or synthesis is None or publication is None:
            raise ProductionRepairProjectionError("qa_inputs_missing")
        qa_result = await self._run_qa(
            uow,
            run_id,
            references,
            extraction,
            synthesis,
            publication,
            run.subject_id,
            run,
        )
        await self._ensure_qa_passed(qa_result)
        return qa_result

    @staticmethod
    async def _mark_stages_stale(uow: Any, run_id: UUID, stages: set[str]) -> list[str]:
        marker = getattr(uow.production_artifacts, "mark_stages_stale", None)
        if not callable(marker):
            raise ProductionRepairProjectionError("production_artifact_stale_port_unavailable")
        return cast(list[str], await marker(run_id, stages))

    async def _subject_title(self, uow: Any, run_id: UUID, subject_id: UUID) -> str:
        snapshots = getattr(uow, "production_input_snapshots", None)
        if snapshots is not None and callable(getattr(snapshots, "get_by_run", None)):
            snapshot = await snapshots.get_by_run(run_id)
            title = getattr(snapshot, "subject_title", None) if snapshot is not None else None
            if isinstance(title, str) and title:
                return title
        return str(subject_id)

    async def _run_qa(
        self,
        uow: Any,
        run_id: UUID,
        references: ProductionArtifact,
        extraction: ProductionArtifact,
        synthesis: ProductionArtifact,
        publication: ProductionArtifact,
        subject_id: UUID,
        run: Any,
    ) -> dict[str, Any]:
        if self._artifact_store is None:
            return {"passed": True, "checks": {}, "errors": [], "warnings": []}
        report, extraction_value, synthesis_text = await self._assembly._load_inputs(
            references, extraction, synthesis
        )
        publication_markdown = ""
        if publication.rendered_blob_id is not None:
            publication_markdown = await self._artifact_store.read_text(
                publication.rendered_blob_id
            )
        collections = getattr(uow, "source_collections", None)
        archived_urls = {
            collection.canonical_url
            for collection in (
                await collections.list_for_subject(subject_id)
                if collections is not None
                and callable(getattr(collections, "list_for_subject", None))
                else ()
            )
            if _enum_value(getattr(collection, "state", None))
            in {"archived", "extracted", "completed"}
        }
        return await self._qa.run_qa(
            run_id=run_id,
            references_artifact=references,
            extraction_artifact=extraction,
            synthesis_artifact=synthesis,
            publication_artifact=publication,
            report=report,
            extraction=extraction_value,
            synthesis_text=synthesis_text,
            publication_markdown=publication_markdown,
            archived_urls=archived_urls,
            research_date=getattr(run, "research_date", None),
        )

    @staticmethod
    async def _ensure_qa_passed(qa_result: dict[str, Any]) -> None:
        if not qa_result.get("passed", False):
            raise ProductionRepairProjectionError("production_repair_qa_failed")


async def _repair_entries_for_artifact(
    artifact: Any, artifact_store: ProductionArtifactStore | None
) -> tuple[list[dict[str, Any]], bool]:
    """Read the complete pack, falling back to bounded legacy diagnostics."""
    metadata = getattr(artifact, "metadata", {}) or {}
    marker = metadata.get("repair_evidence") if isinstance(metadata, dict) else None
    blob_id = marker.get("blob_id") if isinstance(marker, dict) else None
    if artifact_store is not None and blob_id:
        try:
            pack = await artifact_store.read_repair_evidence(UUID(str(blob_id)))
        except Exception:
            pack = None
        if isinstance(pack, dict) and isinstance(pack.get("entries"), list):
            return [entry for entry in pack["entries"] if isinstance(entry, dict)], True

    verification = (
        metadata.get("deterministic_verification", {}) if isinstance(metadata, dict) else {}
    )
    if not isinstance(verification, dict):
        return [], False
    legacy_entries = verification.get("q2_source_evidence_rejections")
    if not isinstance(legacy_entries, list):
        legacy_entries = verification.get("q2_rejected_rules", [])
    return [entry for entry in legacy_entries if isinstance(entry, dict)], False


def _repair_index_entries_for_artifact(
    artifact: Any,
) -> tuple[list[dict[str, Any]], bool]:
    """Return only the compact JSONB repair index; never read a blob."""
    metadata = getattr(artifact, "metadata", {}) or {}
    marker = metadata.get("repair_evidence") if isinstance(metadata, dict) else None
    index = marker.get("index") if isinstance(marker, dict) else None
    if not isinstance(index, list) and isinstance(metadata, dict):
        index = metadata.get("repair_index")
    if isinstance(index, list):
        payload_available = bool(marker.get("blob_id")) if isinstance(marker, dict) else False
        return [entry for entry in index if isinstance(entry, dict)], payload_available

    verification = (
        metadata.get("deterministic_verification", {}) if isinstance(metadata, dict) else {}
    )
    if not isinstance(verification, dict):
        return [], False
    legacy_entries = verification.get("q2_source_evidence_rejections")
    if not isinstance(legacy_entries, list):
        legacy_entries = verification.get("q2_rejected_rules", [])
    return [entry for entry in legacy_entries if isinstance(entry, dict)], False


def _repair_entry_identity(
    entry: Mapping[str, Any],
    *,
    edition_id: UUID,
    subject_id: UUID,
    payload_available: bool = True,
) -> tuple[str, ProductionRepairIssueKind, str, str] | None:
    """Derive the active issue key from immutable evidence, never its position.

    Returns the repair key, its kind, the source id and the exact-value hash
    the key was built from — the hash any later resolution must match.
    """
    proposal_kind = str(entry.get("proposal_kind", ""))
    kind_value = entry.get("kind") or (
        ProductionRepairIssueKind.REJECTED_RULE.value
        if proposal_kind == "rule"
        else ProductionRepairIssueKind.REJECTED_INDICATOR.value
    )
    try:
        kind = ProductionRepairIssueKind(str(kind_value))
    except ValueError:
        return None
    if kind not in {
        ProductionRepairIssueKind.REJECTED_INDICATOR,
        ProductionRepairIssueKind.REJECTED_RULE,
    }:
        return None
    source_url = str(entry.get("source_url", ""))
    source_id = str(entry.get("source_id", ""))
    if not source_url or not source_id:
        return None
    try:
        canonical_url = canonicalize_http_url(source_url)
    except ValueError:
        canonical_url = source_url
    value = entry.get("value")
    value_sha256 = entry.get("value_sha256") or entry.get("value_hash")
    if payload_available and isinstance(value, str):
        # The complete evidence pack is authoritative for the identity. This
        # makes a changed value a new repair key even if a stale copied hash
        # or repair_key is present in an old diagnostic.
        value_sha256 = _sha256(value)
    elif isinstance(value, str) and (
        not isinstance(value_sha256, str) or not _SHA256_RE.fullmatch(value_sha256.casefold())
    ):
        value_sha256 = _sha256(value)
    if not isinstance(value_sha256, str) or not _SHA256_RE.fullmatch(value_sha256.casefold()):
        return None
    value_sha256 = value_sha256.casefold()
    try:
        key = repair_key_for_rejection_hash(
            edition_id=edition_id,
            subject_id=subject_id,
            kind=kind,
            source_url=canonical_url,
            artifact_type=(
                str(entry.get("artifact_type")) if entry.get("artifact_type") is not None else None
            ),
            value_sha256=value_sha256,
        )
    except ValueError:
        return None
    return key, kind, source_id, value_sha256


def _entry_artifact_type(value: object) -> ArtifactType:
    token = str(value or "").casefold()
    if token in {"md5", "sha1", "sha256", "sha512", "hash"}:
        return ArtifactType.HASH
    return ArtifactType(token)


def _entry_rule_type(value: object) -> DetectionRuleType:
    token = str(value or "").casefold()
    if token.endswith("_rule"):
        token = token[:-5]
    return DetectionRuleType(token)


def _entry_model_run_ids(entry: Mapping[str, Any]) -> tuple[str, ...]:
    value = entry.get("model_run_ids")
    if isinstance(value, list | tuple):
        return tuple(str(item) for item in value)
    model_run_id = entry.get("model_run_id")
    return (str(model_run_id),) if model_run_id is not None else ()


def _build_override_item(entry: Mapping[str, Any], value: str, repair_key: str) -> ExtractionItem:
    artifact_type = _entry_artifact_type(entry.get("artifact_type"))
    if (
        artifact_type
        in {
            ArtifactType.YARA_RULE,
            ArtifactType.SIGMA_RULE,
            ArtifactType.SURICATA_RULE,
        }
        or artifact_type is ArtifactType.OTHER
    ):
        raise ValueError("Unsupported repair artifact type")
    source_id = str(entry["source_id"])
    proposal = Q2ArtifactProposal(
        value=value,
        artifact_type=artifact_type.value,
        indicator_status="confirmed_ioc",
        context="",
        evidence_quote="",
    )
    verified = verify_q2_proposals(
        [
            Q2ProposalSubmission(
                output=Q2SourceOutput(artifacts=[proposal]),
                source_ids=(source_id,),
                model_run_id=(str(entry["model_run_id"]) if entry.get("model_run_id") else None),
            )
        ]
    ).canonical
    if len(verified.items) != 1:
        raise ValueError("Repair artifact failed deterministic validation")
    item = verified.items[0]
    evidence_basis = ProductionEvidenceBasis(
        str(entry.get("evidence_basis", ProductionEvidenceBasis.ANALYST_OVERRIDE.value))
    )
    publication_ioc = is_publication_ioc_artifact_type(artifact_type)
    return replace(
        item,
        local_id=f"RPA-{repair_key[:16]}",
        category=("network_artifacts" if publication_ioc else item.category),
        source_ids=(source_id,),
        supported=True,
        indicator_status=(
            IndicatorStatus.CONFIRMED_IOC if publication_ioc else IndicatorStatus.CONTEXTUAL
        ),
        provenance=IndicatorProvenance.ANALYST,
        display_policy=(DisplayPolicy.IOC_SECTION if publication_ioc else DisplayPolicy.BODY_ONLY),
        evidence_quote="",
        model_run_ids=_entry_model_run_ids(entry),
        evidence_basis=evidence_basis,
    )


def _build_override_rule(entry: Mapping[str, Any], value: str, repair_key: str) -> DetectionRule:
    source_id = str(entry["source_id"])
    rule_type = _entry_rule_type(entry.get("artifact_type"))
    name = entry.get("name")
    proposal = Q2RuleProposal(
        rule_type=rule_type,
        name=name if isinstance(name, str) else None,
        body=value,
        context="",
        evidence_quote="",
    )
    verified = verify_q2_proposals(
        [
            Q2ProposalSubmission(
                output=Q2SourceOutput(rules=[proposal]),
                source_ids=(source_id,),
                model_run_id=(str(entry["model_run_id"]) if entry.get("model_run_id") else None),
            )
        ]
    ).canonical
    if len(verified.rules) != 1:
        raise ValueError("Repair rule failed deterministic validation")
    evidence_basis = ProductionEvidenceBasis(
        str(entry.get("evidence_basis", ProductionEvidenceBasis.ANALYST_OVERRIDE.value))
    )
    return replace(
        verified.rules[0],
        source_ids=(source_id,),
        supported=True,
        model_run_ids=_entry_model_run_ids(entry),
        evidence_basis=evidence_basis,
    )


def repair_include_is_buildable(
    kind: ProductionRepairIssueKind | str,
    entry: Mapping[str, Any],
    value: str | None,
) -> bool:
    """Report whether an INCLUDE could actually be projected later.

    This is the very same construction the projection performs, run ahead of
    the decision so an unbuildable value is refused while the analyst can
    still exclude it.
    """
    if not isinstance(value, str) or not value:
        return False
    issue_kind = _repair_kind(kind)
    repair_key = str(entry.get("repair_key") or _sha256(value))
    try:
        if issue_kind is ProductionRepairIssueKind.REJECTED_RULE:
            _build_override_rule(entry, value, repair_key)
        elif issue_kind is ProductionRepairIssueKind.REJECTED_INDICATOR:
            _build_override_item(entry, value, repair_key)
        else:
            return True
    except (KeyError, TypeError, ValueError):
        return False
    return True


#: Impacts whose materialization owes a Publication rebuilt from the repaired
#: Extraction.  ``RULE_BUNDLE_ONLY`` and ``NO_DELIVERABLE_CHANGE`` are absent on
#: purpose: they leave the existing document semantically valid.
_IMPACTS_REQUIRING_PUBLICATION_REBUILD = frozenset(
    {
        ProductionRepairImpactKind.PUBLICATION_ONLY.value,
        ProductionRepairImpactKind.NARRATIVE.value,
        ProductionRepairImpactKind.SOURCE_CORPUS.value,
    }
)


def extraction_requires_publication_rebuild(extraction: Any) -> bool:
    """True when the effective Extraction owes a rebuilt Publication."""
    metadata = getattr(extraction, "metadata", None)
    marker = metadata.get("repair_materialization") if isinstance(metadata, dict) else None
    if not isinstance(marker, Mapping):
        return False
    return str(marker.get("impact_kind")) in _IMPACTS_REQUIRING_PUBLICATION_REBUILD


def publication_is_compatible_with_current_effective_inputs(
    *,
    publication: Any,
    extraction: Any,
    references: Any = None,
) -> bool:
    """Prove a document was assembled from the current effective Extraction.

    Defence in depth for the freeze: it does not trust
    ``repair_projection.applied_decisions`` on the Extraction, it asks the
    Publication which artifacts it consumed.

    A deliberately reused Synthesis is accepted — that is exactly what a
    ``PUBLICATION_ONLY`` repair does — and identical versions are never
    required.  Only the Extraction identity is enforced, and only when the
    current Extraction is one whose repair owed a rebuilt document.
    """
    if publication is None:
        return False
    if not extraction_requires_publication_rebuild(extraction):
        return True
    extraction_id = str(getattr(extraction, "id", ""))
    metadata = getattr(publication, "metadata", None)
    if not isinstance(metadata, Mapping):
        return False
    inputs = metadata.get("input_artifacts")
    if isinstance(inputs, Mapping):
        if str(inputs.get("extraction_artifact_id")) != extraction_id:
            return False
        if references is not None and str(inputs.get("references_artifact_id")) != str(
            getattr(references, "id", "")
        ):
            return False
        return True
    # Documents assembled by a repair before ``input_artifacts`` existed carry
    # the same proof inside their materialization audit.
    audit = metadata.get("repair_materialization")
    if isinstance(audit, Mapping):
        return str(audit.get("result_extraction_artifact_id")) == extraction_id
    return False


def _repair_projection_marker(artifact: Any) -> Mapping[str, Any] | None:
    """Read the ``repair_projection`` marker of one extraction artifact."""
    metadata = getattr(artifact, "metadata", None)
    marker = metadata.get("repair_projection") if isinstance(metadata, dict) else None
    return marker if isinstance(marker, Mapping) else None


def _marker_entries(marker: Mapping[str, Any] | None, field: str) -> list[Mapping[str, Any]]:
    values = marker.get(field) if marker is not None else None
    return (
        [value for value in values if isinstance(value, Mapping)]
        if isinstance(values, list)
        else []
    )


def repair_decision_is_materialized(
    marker: Mapping[str, Any] | None,
    repair_key: str,
    decision: ProductionRepairDecision | None,
) -> bool:
    """Report whether this exact decision's content is really projected.

    "Applied" must mean materialized, not merely considered: only a decision
    listed in ``applied_decisions`` of the current projection is applied.
    """
    if decision is None:
        return False
    decision_id = str(decision.id)
    return any(
        str(entry.get("repair_key")) == repair_key and str(entry.get("decision_id")) == decision_id
        for entry in _marker_entries(marker, "applied_decisions")
    )


def repair_projection_decision_ids(marker: Mapping[str, Any] | None) -> set[str]:
    """Every decision the projection took into account, applied or not."""
    return {
        str(entry["decision_id"])
        for field in ("applied_decisions", "unbuildable_decisions")
        for entry in _marker_entries(marker, field)
        if entry.get("decision_id") is not None
    }


def _marker_applied_action(marker: Mapping[str, Any] | None, repair_key: str) -> str | None:
    for entry in _marker_entries(marker, "applied_decisions"):
        if str(entry.get("repair_key")) == repair_key:
            return str(entry.get("action"))
    return None


def repair_decision_application_state(
    issue: Any,
    effective_decision: ProductionRepairDecision | None,
    current_projection_marker: Mapping[str, Any] | None,
) -> RepairDecisionApplicationState:
    """Compare the effective decision with what is really applied.

    The current action alone cannot answer this.  A first EXCLUDE needs no
    projection because the deterministic pipeline already rejected the value,
    but an EXCLUDE that revises an applied INCLUDE must produce a projection
    that removes it -- and symmetrically for INCLUDE after EXCLUDE.
    """
    if effective_decision is None:
        return RepairDecisionApplicationState.UNRESOLVED
    kind_value = _enum_value(getattr(issue, "kind", None))
    if kind_value == ProductionRepairIssueKind.SUPPLEMENTAL_SOURCE_UNARCHIVED.value:
        # A waived source owes a REFERENCES reconciliation, never an
        # extraction projection; that debt is tracked by its repair state.
        return RepairDecisionApplicationState.ALREADY_EFFECTIVE
    repair_key = str(getattr(issue, "repair_key", ""))
    decision_id = str(effective_decision.id)
    if any(
        str(entry.get("repair_key")) == repair_key and str(entry.get("decision_id")) == decision_id
        for entry in _marker_entries(current_projection_marker, "unbuildable_decisions")
    ):
        return RepairDecisionApplicationState.UNBUILDABLE
    if repair_decision_is_materialized(current_projection_marker, repair_key, effective_decision):
        return RepairDecisionApplicationState.ALREADY_EFFECTIVE
    action = _enum_value(effective_decision.action)
    if action == ProductionRepairAction.EXCLUDE.value:
        applied_action = _marker_applied_action(current_projection_marker, repair_key)
        return (
            RepairDecisionApplicationState.PROJECTION_REQUIRED
            if applied_action
            in {
                ProductionRepairAction.INCLUDE.value,
                ProductionRepairAction.REPLACE.value,
            }
            else RepairDecisionApplicationState.ALREADY_EFFECTIVE
        )
    if action in {
        ProductionRepairAction.INCLUDE.value,
        ProductionRepairAction.REPLACE.value,
    }:
        return RepairDecisionApplicationState.PROJECTION_REQUIRED
    return RepairDecisionApplicationState.ALREADY_EFFECTIVE


def repair_issue_application_state(
    issue: Any, effective_decision: ProductionRepairDecision | None = None
) -> RepairDecisionApplicationState:
    """Read or derive the application state carried by a repair issue DTO."""
    decision = (
        effective_decision
        if effective_decision is not None
        else getattr(issue, "effective_decision", None)
    )
    value = getattr(issue, "application_state", None)
    if value is not None:
        state = RepairDecisionApplicationState(_enum_value(value))
        if state is not RepairDecisionApplicationState.UNRESOLVED or decision is None:
            return state
    marker = (
        {
            "applied_decisions": [
                {
                    "repair_key": str(getattr(issue, "repair_key", "")),
                    "decision_id": str(getattr(decision, "id", "")),
                    "action": str(_enum_value(getattr(decision, "action", ""))),
                }
            ]
        }
        if bool(getattr(issue, "projection_applied", False))
        else None
    )
    return repair_decision_application_state(issue, decision, marker)


def repair_issue_blocks_signoff(
    issue: Any, effective_decision: ProductionRepairDecision | None = None
) -> bool:
    """Return whether a repair issue still changes the deliverable before freeze.

    An archived source pending REFERENCES blocks whatever the decision says:
    the current state of the corpus dominates an older waiver.
    """
    if repair_issue_pending_references(issue):
        return True
    return repair_issue_application_state(issue, effective_decision) in {
        RepairDecisionApplicationState.PROJECTION_REQUIRED,
        RepairDecisionApplicationState.UNBUILDABLE,
    }


def _item_projection_key(item: ExtractionItem) -> tuple[str, str]:
    if item.artifact_type is None:
        return item.category, item.value.casefold()
    artifact_type = ArtifactType(item.artifact_type)
    normalized = item.normalized_value or canonical_indicator_key(item.value, artifact_type)
    return artifact_type.value, normalized


def _prefer_projection_object[T](
    previous: T,
    candidate: T,
    *,
    previous_basis: ProductionEvidenceBasis,
    candidate_basis: ProductionEvidenceBasis,
) -> T:
    if (
        previous_basis is ProductionEvidenceBasis.ANALYST_OVERRIDE
        and candidate_basis is ProductionEvidenceBasis.SOURCE_VERIFIED
    ):
        return candidate
    return previous


def _merge_projection_items(
    base: Sequence[ExtractionItem], additions: Sequence[ExtractionItem]
) -> tuple[ExtractionItem, ...]:
    merged: dict[tuple[str, str], ExtractionItem] = {}
    for item in (*base, *sorted(additions, key=lambda value: _item_projection_key(value))):
        key = _item_projection_key(item)
        previous = merged.get(key)
        if previous is None:
            merged[key] = item
            continue
        chosen = _prefer_projection_object(
            previous,
            item,
            previous_basis=previous.evidence_basis,
            candidate_basis=item.evidence_basis,
        )
        merged[key] = replace(
            chosen,
            source_ids=tuple(sorted(set(previous.source_ids + item.source_ids))),
            model_run_ids=tuple(sorted(set(previous.model_run_ids + item.model_run_ids))),
        )
    return tuple(merged.values())


def _merge_projection_rules(
    base: Sequence[DetectionRule], additions: Sequence[DetectionRule]
) -> tuple[DetectionRule, ...]:
    merged: dict[tuple[DetectionRuleType, str], DetectionRule] = {}
    values = (*base, *sorted(additions, key=lambda value: (value.rule_type.value, value.sha256)))
    for rule in values:
        key = (rule.rule_type, rule.sha256)
        previous = merged.get(key)
        if previous is None:
            merged[key] = rule
            continue
        chosen = _prefer_projection_object(
            previous,
            rule,
            previous_basis=previous.evidence_basis,
            candidate_basis=rule.evidence_basis,
        )
        merged[key] = replace(
            chosen,
            source_ids=tuple(sorted(set(previous.source_ids + rule.source_ids))),
            model_run_ids=tuple(sorted(set(previous.model_run_ids + rule.model_run_ids))),
        )
    return tuple(
        merged[key] for key in sorted(merged, key=lambda value: (value[0].value, value[1]))
    )


async def _get_for_update(repository: Any, entity_id: UUID) -> Any | None:
    getter = getattr(repository, "get_for_update", None)
    if getter is not None:
        return await getter(entity_id)
    return await repository.get(entity_id)


async def _effective_decisions_for_reader(
    uow: Any, edition_id: UUID, subject_id: UUID | None
) -> Sequence[ProductionRepairDecision]:
    repository = getattr(uow, "production_repair_decisions", None)
    if repository is None:
        return ()
    getter = getattr(repository, "effective_decisions", None)
    if callable(getter):
        return cast(
            Sequence[ProductionRepairDecision],
            await getter(edition_id, subject_id),
        )
    history = await repository.list_for_edition(edition_id, subject_id)
    return _effective_from_history(history)


async def _current_artifacts_by_run(
    uow: Any,
    edition_id: UUID,
    stage: str,
    runs: Sequence[Any],
) -> dict[UUID, Any]:
    """Load one current artifact per run, using the set-based repository port."""
    repository = uow.production_artifacts
    bulk_getter = getattr(repository, "list_current_for_edition", None)
    if callable(bulk_getter):
        artifacts = await bulk_getter(edition_id, stage)
        return {artifact.production_run_id: artifact for artifact in artifacts}
    return {
        run.id: artifact
        for run in runs
        if (artifact := await repository.get_current(run.id, stage)) is not None
    }


def _effective_from_history(
    history: Sequence[ProductionRepairDecision],
) -> tuple[ProductionRepairDecision, ...]:
    latest: dict[tuple[UUID, str], ProductionRepairDecision] = {}
    for decision in sorted(history, key=lambda item: (item.created_at, item.id)):
        latest[(decision.subject_id, decision.repair_key)] = decision
    return tuple(latest.values())


def _enum_value(value: Any) -> Any:
    return getattr(value, "value", value)


async def _require_publication_rebuild(uow: Any, run: Any, *, retry_stage: str) -> bool:
    """Move a run that just lost its deliverable out of READY.

    Called in the SAME transaction as the stale, never after the commit:
    between the two there would be a window where the run claims READY while
    no PUBLICATION artifact is current -- exactly the state the review read
    model cannot describe, since it only ever sees non-STALE artifacts.  The
    article then shows as "à corriger" with no reason and no working gesture.

    A run that is already NEEDS_REVIEW or FAILED keeps its own diagnosis: the
    rebuild it owes is not more informative than the failure that put it
    there.  CANCELLED is left alone entirely -- ``mark_needs_review`` refuses
    it, and a cancelled run owns its own resume use case.

    Returns whether the run actually changed.
    """
    if _enum_value(getattr(run, "status", None)) != SubjectProductionStatus.READY.value:
        return False
    mark = getattr(run, "mark_needs_review", None)
    if not callable(mark):
        return False
    mark(
        code=PUBLICATION_REBUILD_REQUIRED_ERROR_CODE,
        message=(
            "La publication a été invalidée par une réparation en amont. "
            f"Reconstruction requise à partir de l'étape « {retry_stage} »."
        ),
        details={"retry_stage": retry_stage},
    )
    runs = getattr(uow, "subject_production_runs", None)
    save = getattr(runs, "save", None) if runs is not None else None
    if callable(save):
        await save(run)
    return True


def _supplemental_repair_state(
    collection: Any, decision: ProductionRepairDecision | None
) -> tuple[SupplementalSourceRepairState, str]:
    """Derive the durable state of a Q1 proposal missing from the canonical.

    Archiving wins over an older waiver on purpose: the analyst supplying the
    content is a newer fact than the decision to publish without it, and the
    reconciliation must be allowed to put the source back.  The waiver itself
    is never rewritten; it stays in the append-only audit.
    """
    if collection is None:
        return SupplementalSourceRepairState.COLLECTION_MISSING, "prepare_source"
    if _is_archived_collection(collection):
        return (
            SupplementalSourceRepairState.ARCHIVED_PENDING_REFERENCES,
            "rebuild_references",
        )
    if decision is not None and decision.action is ProductionRepairAction.CONTINUE_WITHOUT_SOURCE:
        return SupplementalSourceRepairState.UNARCHIVED, "continue_without_source"
    return SupplementalSourceRepairState.UNARCHIVED, "archive_manual_content"


def _is_archived_collection(collection: Any) -> bool:
    return _enum_value(getattr(collection, "state", None)) in {
        CollectionState.ARCHIVED.value,
        CollectionState.EXTRACTED.value,
        CollectionState.COMPLETED.value,
    }


def _reference_source_index(
    artifact: Any,
) -> tuple[list[dict[str, Any]], set[str]] | None:
    """Read the bounded Q1 proposal/canonical index from artifact metadata."""
    metadata = getattr(artifact, "metadata", {}) or {}
    index = metadata.get("repair_source_index") if isinstance(metadata, dict) else None
    if not isinstance(index, dict):
        return None
    proposed_raw = index.get("proposed")
    canonical_raw = index.get("canonical")
    if not isinstance(proposed_raw, list) or not isinstance(canonical_raw, list):
        return None

    proposed: list[dict[str, Any]] = []
    for value in proposed_raw:
        if not isinstance(value, dict):
            continue
        source_url = value.get("source_url")
        source_id = value.get("source_id")
        if not isinstance(source_url, str) or not isinstance(source_id, str):
            continue
        proposed.append(dict(value))

    canonical_urls: set[str] = set()
    for value in canonical_raw:
        source_url = value.get("source_url") if isinstance(value, dict) else value
        if isinstance(source_url, str) and source_url:
            canonical_urls.add(source_url)
    return proposed, canonical_urls


def _issue_record(
    context: _RepairContext,
    entry: Mapping[str, Any],
    *,
    payload_available: bool,
    effective_decision: ProductionRepairDecision | None,
    edition_id: UUID,
) -> tuple[ProductionRepairIssueView, str | None] | None:
    proposal_kind = str(entry.get("proposal_kind", ""))
    kind_value = entry.get("kind")
    if kind_value is None:
        kind_value = (
            ProductionRepairIssueKind.REJECTED_RULE.value
            if proposal_kind == "rule"
            else ProductionRepairIssueKind.REJECTED_INDICATOR.value
        )
    try:
        kind = ProductionRepairIssueKind(str(kind_value))
    except ValueError:
        return None
    source_id = str(entry.get("source_id", ""))
    raw_source_url = str(entry.get("source_url", ""))
    if not source_id or not raw_source_url:
        return None
    try:
        source_url = canonicalize_http_url(raw_source_url)
    except ValueError:
        source_url = raw_source_url

    raw_value = entry.get("value")
    value = raw_value if payload_available and isinstance(raw_value, str) else None
    value_sha256 = entry.get("value_sha256") or entry.get("value_hash")
    if not isinstance(value_sha256, str) or not _SHA256_RE.fullmatch(value_sha256):
        value_sha256 = _sha256(raw_value) if isinstance(raw_value, str) else _sha256("")
    if value is not None and _sha256(value) != value_sha256:
        value_sha256 = _sha256(value)
        value = None
        payload_available = False

    artifact_type = entry.get("artifact_type")
    artifact_type = str(artifact_type) if artifact_type is not None else None
    supplied_repair_key = entry.get("repair_key")
    try:
        expected_repair_key = (
            repair_key_for_supplemental_source(
                edition_id=edition_id,
                subject_id=context.run.subject_id,
                source_url=source_url,
            )
            if kind is ProductionRepairIssueKind.SUPPLEMENTAL_SOURCE_UNARCHIVED
            else repair_key_for_rejection_hash(
                edition_id=edition_id,
                subject_id=context.run.subject_id,
                kind=kind,
                source_url=source_url,
                artifact_type=artifact_type,
                value_sha256=value_sha256,
            )
        )
    except ValueError:
        return None
    # Recompute the identity from the persisted content. A stale/corrupt
    # supplied key must never make a decision for an old value adopt a new one.
    repair_key = (
        supplied_repair_key
        if isinstance(supplied_repair_key, str)
        and _SHA256_RE.fullmatch(supplied_repair_key)
        and supplied_repair_key == expected_repair_key
        else expected_repair_key
    )

    preview_source = raw_value if isinstance(raw_value, str) else str(entry.get("preview", ""))
    preview = preview_source[:MAX_REPAIR_PREVIEW_CHARS]
    view = ProductionRepairIssueView(
        payload_origin=(
            RepairPayloadOrigin.REPAIR_EVIDENCE_PACK
            if value is not None
            else RepairPayloadOrigin.UNAVAILABLE
        ),
        # A bounded list never opens an archive, so an entry that predates the
        # evidence pack is only flagged as possibly recoverable on the detail.
        legacy_evidence=not payload_available,
        repair_key=repair_key,
        kind=kind,
        artifact_type=artifact_type,
        source_id=source_id,
        source_title=str(entry.get("source_title") or context.source_titles.get(source_id, "")),
        is_publication_ioc=is_publication_ioc_artifact_type(artifact_type),
        source_url=source_url,
        reason_code=str(entry.get("reason_code", "")),
        value_sha256=value_sha256,
        preview=preview,
        payload_available=payload_available,
        production_run_id=context.run.id,
        observed_artifact_id=context.artifact.id,
        observed_artifact_version=context.artifact.version,
        observed_pipeline_generation=context.run.pipeline_generation,
        model_run_id=(
            str(entry["model_run_id"]) if entry.get("model_run_id") is not None else None
        ),
        batch_id=str(entry["batch_id"]) if entry.get("batch_id") is not None else None,
        effective_decision=effective_decision,
        subject_id=context.run.subject_id,
    )
    return view, value


@dataclass(frozen=True, slots=True)
class ProductionReferenceRepairResult:
    """Outcome of rebuilding REFERENCES from one archived Q1 response."""

    artifact: ProductionArtifact
    changed: bool
    restored_source_ids: tuple[str, ...] = ()
    restored_event_ids: tuple[str, ...] = ()
    source_delta: dict[str, list[dict[str, str | None]]] = field(default_factory=dict)

    @property
    def references_artifact(self) -> ProductionArtifact:
        return self.artifact


class ProductionReferenceRepairService:
    """Reconcile a persisted Q1 answer without contacting a model."""

    _REPAIR_PROJECTION_VERSION = "1"
    _CANONICAL_BUCKET = "production-artifacts-canonical"

    def __init__(
        self,
        uow_factory: ProductionUnitOfWorkFactory,
        artifact_store: ProductionArtifactStore | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._artifact_store = artifact_store

    async def rebuild_from_archived_q1(
        self,
        run_id: UUID,
        *,
        actor_id: str,
    ) -> ProductionReferenceRepairResult:
        actor_id = actor_id.strip()
        if not actor_id:
            raise ProductionReferenceRepairError("production_repair_actor_required")
        if self._artifact_store is None:
            raise ProductionReferenceRepairError("production_repair_storage_unavailable")

        async with self._uow_factory() as uow:
            # Discover first, then acquire Edition and Run locks in that order.
            initial_run = await uow.subject_production_runs.get(run_id)
            if initial_run is None:
                raise ProductionReferenceRepairError("production_run_not_found")

            editions = getattr(uow, "editions", None)
            if editions is not None:
                get_edition_for_update = getattr(editions, "get_for_update", None)
                edition = (
                    await get_edition_for_update(initial_run.edition_id)
                    if get_edition_for_update is not None
                    else await editions.get(initial_run.edition_id)
                )
                if edition is None:
                    raise ProductionReferenceRepairError("edition_not_found")
                if _enum_value(edition.status) not in {
                    EditionStatus.PRODUCTION.value,
                    EditionStatus.REVIEW.value,
                }:
                    raise ProductionReferenceRepairError("edition_frozen_for_publication")

                manifests = getattr(uow, "publication_manifests", None)
                if (
                    manifests is not None
                    and await manifests.get_latest_for_edition(initial_run.edition_id) is not None
                ):
                    raise ProductionReferenceRepairError("edition_frozen_for_publication")

            run = await uow.subject_production_runs.get_for_update(run_id)
            if run is None:
                raise ProductionReferenceRepairError("production_run_not_found")
            if run.edition_id != initial_run.edition_id:
                raise ProductionReferenceRepairError("production_run_edition_changed")

            run_status = _enum_value(run.status)
            if run_status in {
                SubjectProductionStatus.QUEUED.value,
                SubjectProductionStatus.RUNNING.value,
                SubjectProductionStatus.CANCELLED.value,
            }:
                raise ProductionReferenceRepairError("production_repair_run_not_reviewable")
            if run_status not in {
                SubjectProductionStatus.READY.value,
                SubjectProductionStatus.NEEDS_REVIEW.value,
                SubjectProductionStatus.FAILED.value,
            }:
                raise ProductionReferenceRepairError("production_repair_run_not_reviewable")
            if run.requires_reconciliation:
                raise ProductionReconciliationRequiredError

            base = await uow.production_artifacts.get_current(
                run.id, ProductionArtifactStage.REFERENCES.value
            )
            if base is None:
                raise ProductionReferenceRepairError("references_artifact_not_found")
            if base.raw_blob_id is None or base.canonical_blob_id is None:
                raise ProductionReferenceRepairError("references_payload_missing")
            if run.research_date is None:
                raise ProductionReferenceRepairError("research_date_missing")

            try:
                raw_q1 = await self._artifact_store.read_text(base.raw_blob_id)
                proposed = parse_reference_report(raw_q1, run.research_date)
                canonical = reference_report_from_json(
                    await self._artifact_store.read_json(base.canonical_blob_id)
                )
            except Exception as exc:
                raise ProductionReferenceRepairError(
                    "references_payload_unavailable", str(exc)
                ) from exc
            if not proposed.usable or proposed.value is None:
                raise ProductionReferenceRepairError(
                    "references_raw_unusable",
                    "; ".join(proposed.errors) or "The archived Q1 response is unusable",
                )

            archived_projection = await _archived_source_projection(uow, run.subject_id)
            reconciliation = reconcile_reference_report_with_archives(
                proposed.value,
                {item[0] for item in archived_projection},
                previous_canonical_report=canonical,
            )
            source_delta = await _source_delta(
                uow,
                previous_report=canonical,
                current_report=reconciliation.report,
                archived_projection=archived_projection,
                base_artifact=base,
            )
            has_source_delta = any(
                source_delta[key]
                for key in ("added_sources", "removed_sources", "changed_content_sources")
            )
            if reconciliation.report == canonical and not has_source_delta:
                await uow.commit()
                return ProductionReferenceRepairResult(
                    artifact=base,
                    changed=False,
                    source_delta=source_delta,
                )

            derived_input_hash = compute_input_hash(
                {
                    "repair_projection_version": self._REPAIR_PROJECTION_VERSION,
                    "base_references_artifact_id": str(base.id),
                    "base_input_hash": base.input_hash,
                    "archived_sources": [list(item) for item in archived_projection],
                }
            )
            canonical_json = reference_report_to_json(reconciliation.report)
            try:
                canonical_blob_id, _ = await self._artifact_store.put_canonical_json(
                    canonical_json, bucket=self._CANONICAL_BUCKET
                )
            except Exception as exc:
                raise ProductionReferenceRepairError(
                    "production_repair_storage_unavailable", str(exc)
                ) from exc

            prior_versions = [
                artifact.version
                for artifact in await uow.production_artifacts.list_for_run(run.id)
                if artifact.stage is ProductionArtifactStage.REFERENCES
            ]
            generated_at = datetime.now(UTC)
            base_warnings = base.metadata.get("warnings", [])
            if not isinstance(base_warnings, list):
                base_warnings = []
            repair_source_index = None
            base_source_index = base.metadata.get("repair_source_index")
            if isinstance(base_source_index, dict) and isinstance(
                base_source_index.get("proposed"), list
            ):
                repair_source_index = {
                    "proposed": [
                        dict(item)
                        for item in base_source_index["proposed"]
                        if isinstance(item, dict)
                    ],
                    "canonical": [
                        {
                            "source_id": source.local_id,
                            "source_url": source.canonical_url,
                        }
                        for source in reconciliation.report.sources
                    ],
                    "source_hashes": {url: digest for url, digest in archived_projection if digest},
                }
            artifact = ProductionArtifact(
                production_run_id=run.id,
                subject_id=run.subject_id,
                stage=ProductionArtifactStage.REFERENCES,
                version=max(prior_versions, default=0) + 1,
                input_hash=derived_input_hash,
                status=ProductionArtifactStatus.VERIFIED,
                raw_blob_id=base.raw_blob_id,
                canonical_blob_id=canonical_blob_id,
                model_run_id=base.model_run_id,
                conversation_turn_id=base.conversation_turn_id,
                metadata={
                    "event_count": len(reconciliation.report.events),
                    "source_count": len(reconciliation.report.sources),
                    "warnings": list(base_warnings),
                    "parser_version": canonical_json.get("parser_version"),
                    "generated_at": generated_at.isoformat(),
                    "derived_repair": True,
                    "repaired_from_artifact_id": str(base.id),
                    "repair_kind": "reference_reconciliation",
                    "actor_id": actor_id,
                    "restored_source_ids": list(reconciliation.restored_source_ids),
                    "restored_event_ids": list(reconciliation.restored_event_ids),
                    "dropped_source_ids": list(reconciliation.dropped_source_ids),
                    "dropped_event_ids": list(reconciliation.dropped_event_ids),
                    "archived_sources": [list(item) for item in archived_projection],
                    "source_delta": source_delta,
                    "added_sources": [
                        item["canonical_url"] for item in source_delta["added_sources"]
                    ],
                    "removed_sources": [
                        item["canonical_url"] for item in source_delta["removed_sources"]
                    ],
                    "unchanged_sources": [
                        item["canonical_url"] for item in source_delta["unchanged_sources"]
                    ],
                    "changed_content_sources": [
                        item["canonical_url"] for item in source_delta["changed_content_sources"]
                    ],
                    "unknown_baseline_sources": [
                        item["canonical_url"] for item in source_delta["unknown_baseline_sources"]
                    ],
                    **(
                        {"repair_source_index": repair_source_index}
                        if repair_source_index is not None
                        else {}
                    ),
                },
            )
            await uow.production_artifacts.append(artifact)
            await uow.production_artifacts.mark_downstream_stale(
                run.id, ProductionArtifactStage.REFERENCES.value
            )
            # Same invariant as the Extraction repair: the deliverable this run
            # published is gone, so the run cannot keep claiming READY. The
            # transition rides the stale's transaction.
            await _require_publication_rebuild(
                uow, run, retry_stage=SubjectProductionStage.EXTRACTION.value
            )
            await uow.commit()
            return ProductionReferenceRepairResult(
                artifact=artifact,
                changed=True,
                restored_source_ids=reconciliation.restored_source_ids,
                restored_event_ids=reconciliation.restored_event_ids,
                source_delta=source_delta,
            )


async def _archived_source_projection(uow: Any, subject_id: UUID) -> tuple[tuple[str, str], ...]:
    """Return ``(canonical_url, decoded_sha256)`` in stable URL order."""
    collections = await uow.source_collections.list_for_subject(subject_id)
    documents_repository = getattr(uow, "source_documents", None)
    documents = (
        await documents_repository.list_for_subject(subject_id)
        if documents_repository is not None
        else ()
    )
    documents_by_id = {document.id: document for document in documents}
    attempts_repository = getattr(uow, "collection_attempts", None)
    blobs_repository = getattr(uow, "blobs", None)
    projection: dict[str, str] = {}
    for collection in collections:
        if not _is_archived_collection(collection):
            continue
        canonical_url = str(collection.canonical_url)
        document = documents_by_id.get(getattr(collection, "source_document_id", None))
        digest = getattr(document, "decoded_sha256", None)
        if not _is_sha256(digest):
            digest = getattr(collection, "decoded_sha256", None)
        if not _is_sha256(digest) and attempts_repository is not None:
            attempts = await attempts_repository.list_for_collection(collection.id)
            digest = attempts[-1].decoded_sha256 if attempts else None
        if not _is_sha256(digest) and blobs_repository is not None:
            blob_id = getattr(collection, "decoded_blob_id", None) or getattr(
                document, "decoded_blob_id", None
            )
            blob = await blobs_repository.get(blob_id) if blob_id is not None else None
            digest = getattr(getattr(blob, "descriptor", None), "sha256", None)
        projection[canonical_url] = str(digest).casefold() if _is_sha256(digest) else ""
    return tuple(sorted(projection.items()))


async def _source_delta(
    uow: Any,
    *,
    previous_report: ReferenceReport,
    current_report: ReferenceReport,
    archived_projection: Sequence[tuple[str, str]],
    base_artifact: ProductionArtifact,
) -> dict[str, list[dict[str, str | None]]]:
    """Compare REFERENCES sources by canonical URL and archived content hash."""
    current_hashes = {url: digest or None for url, digest in archived_projection}
    previous_hashes: dict[str, str | None] = {}
    metadata = base_artifact.metadata if isinstance(base_artifact.metadata, dict) else {}
    source_index = metadata.get("repair_source_index")
    historical_hashes = (
        source_index.get("source_hashes") if isinstance(source_index, dict) else None
    )
    if isinstance(historical_hashes, dict):
        previous_hashes.update(
            {
                str(url): str(value).casefold()
                for url, value in historical_hashes.items()
                if isinstance(url, str) and isinstance(value, str) and _is_sha256(value)
            }
        )
    archived_sources = metadata.get("archived_sources")
    if isinstance(archived_sources, list):
        for item in archived_sources:
            if (
                isinstance(item, list | tuple)
                and len(item) == 2
                and isinstance(item[0], str)
                and isinstance(item[1], str)
                and _is_sha256(item[1])
            ):
                previous_hashes.setdefault(item[0], item[1].casefold())

    # The baseline is deliberately NOT completed from ``source_extractions``:
    # that table is content-addressed and shared by every subject, so an
    # arbitrary row for the same URL may describe another edition's capture.
    # Attributing it to this subject would report "content changed" for a
    # source this subject never captured differently. When this subject holds
    # no recorded baseline, the honest answer is "unknown", below.

    previous_urls = {source.canonical_url for source in previous_report.sources}
    current_urls = {source.canonical_url for source in current_report.sources}
    added = sorted(current_urls - previous_urls)
    removed = sorted(previous_urls - current_urls)
    unchanged: list[dict[str, str | None]] = []
    changed: list[dict[str, str | None]] = []
    unknown_baseline: list[dict[str, str | None]] = []
    for url in sorted(previous_urls & current_urls):
        previous_sha = previous_hashes.get(url)
        current_sha = current_hashes.get(url)
        entry = {
            "canonical_url": url,
            "previous_source_sha256": previous_sha,
            "current_source_sha256": current_sha,
        }
        if previous_sha is None:
            # No recorded capture for this subject: the source predates the
            # per-source baseline. "We do not know" is not "it changed" --
            # reporting it as changed tells the analyst their correction
            # rewrote sources it never touched.
            unknown_baseline.append(entry)
        elif current_sha == previous_sha:
            unchanged.append(entry)
        else:
            changed.append(entry)

    return {
        "added_sources": [
            {
                "canonical_url": url,
                "previous_source_sha256": None,
                "current_source_sha256": current_hashes.get(url),
            }
            for url in added
        ],
        "removed_sources": [
            {
                "canonical_url": url,
                "previous_source_sha256": previous_hashes.get(url),
                "current_source_sha256": None,
            }
            for url in removed
        ],
        "unchanged_sources": unchanged,
        "changed_content_sources": changed,
        "unknown_baseline_sources": unknown_baseline,
    }


async def _q2_reuse_preview(
    uow: Any,
    *,
    run: Any,
    source_urls: Sequence[str],
    collections: Sequence[Any],
) -> dict[str, int]:
    """Estimate Q2 calls from durable source checkpoints before a rebuild."""
    documents_repository = getattr(uow, "source_documents", None)
    documents = (
        await documents_repository.list_for_subject(run.subject_id)
        if documents_repository is not None
        and callable(getattr(documents_repository, "list_for_subject", None))
        else ()
    )
    documents_by_id = {getattr(document, "id", None): document for document in documents}
    hashes: dict[str, str] = {}
    for collection in collections:
        url = getattr(collection, "canonical_url", None)
        document = documents_by_id.get(getattr(collection, "source_document_id", None))
        digest = getattr(document, "decoded_sha256", None)
        if isinstance(url, str) and isinstance(digest, str) and _is_sha256(digest):
            hashes[url] = digest.casefold()

    snapshots = getattr(uow, "production_input_snapshots", None)
    snapshot = (
        await snapshots.get_by_run(run.id)
        if snapshots is not None and callable(getattr(snapshots, "get_by_run", None))
        else None
    )
    core_urls = {source.canonical_url for source in getattr(snapshot, "core_sources", ())}
    repository = getattr(uow, "source_extractions", None)
    finder = getattr(repository, "list_for_url", None)
    expected_reuses = 0
    unknown = 0
    for url in sorted(set(source_urls)):
        digest = hashes.get(url)
        if digest is None or not callable(finder):
            unknown += 1
            continue
        profile = ExtractionProfile.FULL if url in core_urls else ExtractionProfile.IOC_RULES
        rows = await finder(url)
        reusable = any(
            getattr(row, "source_content_sha256", None) == digest
            and getattr(row, "profile", None) is profile
            and _enum_value(getattr(row, "status", None)) == "verified"
            and getattr(row, "canonical_blob_id", None) is not None
            and getattr(row, "model_run_id", None) is not None
            and getattr(row, "contract_version", None) == Q2_EXTRACTION_CONTRACT_VERSION
            and getattr(row, "prompt_version", None)
            in {
                EXTRACTION_PROMPT_VERSION_BY_PROFILE[profile],
                # Supporting-source checkpoints may have been produced by a
                # deterministic IOC batch.
                IOC_RULES_BATCH_PROMPT_VERSION,
            }
            and getattr(row, "parser_version", None)
            in {Q2_MARKDOWN_PARSER_VERSION, Q2_BATCH_PARSER_VERSION}
            and getattr(row, "verifier_version", None) == ARTIFACT_VERIFIER_VERSION
            for row in rows
        )
        if reusable:
            expected_reuses += 1
    return {
        "expected_q2_calls": max(0, len(set(source_urls)) - expected_reuses - unknown),
        "expected_q2_reuses": expected_reuses,
        "reuse_unknown_count": unknown,
    }


def _is_sha256(value: Any) -> bool:
    return isinstance(value, str) and bool(re.fullmatch(r"[0-9a-fA-F]{64}", value))


# LOT 18 used the shorter service name in design notes; keep it importable
# while retaining the explicit issue-service name used by existing callers.
ProductionRepairService = ProductionRepairIssueService
