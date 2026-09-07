"""Stable production-repair identities, evidence packs and decision services."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

from cti_app.application.persistence import ProductionUnitOfWorkFactory
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.application.production_artifact_verification import (
    Q2ProposalSubmission,
    verify_q2_proposals,
)
from cti_app.application.production_normalization import canonical_indicator_key
from cti_app.application.production_parsers import (
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
from cti_app.application.production_repair_payloads import (
    ProductionRepairPayloadResolver,
    RepairPayloadOrigin,
)
from cti_app.application.production_stages import (
    ExtractionService,
    ProductionQAService,
    PublicationAssemblyService,
    compute_input_hash,
)
from cti_app.domain.collection import CollectionState, SourceOriginKind
from cti_app.domain.discovery import canonicalize_http_url
from cti_app.domain.editions import EditionAuditEvent, EditionStatus
from cti_app.domain.production import (
    DetectionRule,
    DetectionRuleType,
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionArtifactStatus,
    ProductionDerivedOutput,
    ProductionEvidenceBasis,
    ProductionReconciliationRequiredError,
    ProductionRepairAction,
    ProductionRepairDecision,
    ProductionRepairImpact,
    ProductionRepairImpactKind,
    ProductionRepairIssueKind,
    RepairDecisionApplicationState,
    SubjectProductionStage,
    SubjectProductionStatus,
    SupplementalSourceRepairState,
)
from cti_app.domain.publication import ArtifactType, is_publication_ioc_artifact_type

REPAIR_EVIDENCE_SCHEMA_VERSION = "1"
MAX_REPAIR_PREVIEW_CHARS = 512
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


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

    # An analyst override created only for the publication IOC section is not
    # narrative evidence and must not trigger a new Q4 draft.
    if (
        item.evidence_basis is ProductionEvidenceBasis.ANALYST_OVERRIDE
        and item.display_policy is DisplayPolicy.IOC_SECTION
        and not item.context.strip()
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


class ProductionRepairProjectionError(ValueError):
    """The effective extraction cannot be safely projected."""


class ProductionReferenceRepairError(ValueError):
    """The archived Q1 evidence cannot be safely reconstructed."""

    def __init__(self, code: str, message: str | None = None) -> None:
        self.code = code
        super().__init__(message or code)


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
            if current is not None and _enum_value(current.action) == _enum_value(action):
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
            )
            for item in decisions
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
                if current is not None and _enum_value(current.action) == _enum_value(item.action):
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


def _repair_impact(
    kind: ProductionRepairImpactKind,
    affected_outputs: frozenset[ProductionDerivedOutput],
    *,
    model_call_required: bool,
    reason: str,
) -> ProductionRepairImpact:
    return ProductionRepairImpact(
        kind=kind,
        affected_outputs=affected_outputs,
        model_call_required=model_call_required,
        reason=reason,
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
    if _decision_action(decision) != ProductionRepairAction.INCLUDE.value:
        return False
    try:
        state = RepairDecisionApplicationState(
            _projection_enum_value(getattr(issue, "application_state", None))
        )
    except ValueError:
        return False
    return state is RepairDecisionApplicationState.ALREADY_EFFECTIVE


def classify_repair_impact(
    issue: ProductionRepairIssueView | SupplementalSourceRepairIssue,
    decision: ProductionRepairDecision | None,
) -> ProductionRepairImpact:
    """Classify a repair by the derived products whose content can change."""
    action = _decision_action(decision)

    if action == ProductionRepairAction.CONTINUE_WITHOUT_SOURCE.value:
        return _repair_impact(
            ProductionRepairImpactKind.NO_DELIVERABLE_CHANGE,
            _NO_DELIVERABLE_OUTPUTS,
            model_call_required=False,
            reason="The analyst waived the supplemental source without adding content.",
        )

    if isinstance(issue, SupplementalSourceRepairIssue):
        if issue.repair_state is SupplementalSourceRepairState.ARCHIVED_PENDING_REFERENCES:
            return _repair_impact(
                ProductionRepairImpactKind.SOURCE_CORPUS,
                _SOURCE_CORPUS_OUTPUTS,
                model_call_required=True,
                reason=(
                    "An archived Q1 source is absent from REFERENCES and can change the "
                    "source corpus."
                ),
            )
        return _repair_impact(
            ProductionRepairImpactKind.NO_DELIVERABLE_CHANGE,
            _NO_DELIVERABLE_OUTPUTS,
            model_call_required=False,
            reason="The supplemental source has no materialized deliverable change.",
        )

    if decision is None:
        return _repair_impact(
            ProductionRepairImpactKind.NO_DELIVERABLE_CHANGE,
            _NO_DELIVERABLE_OUTPUTS,
            model_call_required=False,
            reason="No repair decision is materialized.",
        )

    action_is_include = action == ProductionRepairAction.INCLUDE.value
    action_is_exclude = action == ProductionRepairAction.EXCLUDE.value
    changes_projected_content = (
        action_is_include and not _include_is_already_effective(issue, decision)
    ) or (action_is_exclude and _exclude_revises_projected_content(issue, decision))
    if not changes_projected_content:
        return _repair_impact(
            ProductionRepairImpactKind.NO_DELIVERABLE_CHANGE,
            _NO_DELIVERABLE_OUTPUTS,
            model_call_required=False,
            reason="The rejected value was not previously included in a deliverable.",
        )

    issue_kind = _repair_kind(issue.kind)
    if issue_kind is ProductionRepairIssueKind.REJECTED_RULE:
        return _repair_impact(
            ProductionRepairImpactKind.RULE_BUNDLE_ONLY,
            _RULE_BUNDLE_OUTPUTS,
            model_call_required=False,
            reason="The repair changes only the accepted detection-rule bundle.",
        )

    # Deliberately classify from the actual artifact type, never from the
    # broad rejected-indicator label or a copied UI boolean.
    if is_publication_ioc_artifact_type(issue.artifact_type):
        return _repair_impact(
            ProductionRepairImpactKind.PUBLICATION_ONLY,
            _PUBLICATION_OUTPUTS,
            model_call_required=False,
            reason="The repair changes only a public IOC projection.",
        )

    return _repair_impact(
        ProductionRepairImpactKind.NARRATIVE,
        _NARRATIVE_OUTPUTS,
        model_call_required=True,
        reason="The repair adds or removes narrative technical evidence.",
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
            decisions = await _effective_decisions_for_reader(uow, edition_id, subject_id)

        decisions_by_key = {
            (decision.subject_id, decision.repair_key): decision for decision in decisions
        }
        issues: list[SupplementalSourceRepairIssue] = []
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
            ),
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
                prepared.append(await self._prepare(edition_id, item))
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
        self, edition_id: UUID, request: ProductionRepairAdjudicationRequest
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

        if request.action is ProductionRepairAction.INCLUDE:
            self._require_buildable_include(request.repair_key, detail)

        return ProductionRepairDecisionInput(
            subject_id=request.subject_id,
            production_run_id=issue.production_run_id,
            observed_artifact_id=issue.observed_artifact_id,
            observed_pipeline_generation=issue.observed_pipeline_generation,
            repair_key=request.repair_key,
            issue_kind=kind,
            action=request.action,
            expected_effective_decision_id=request.expected_effective_decision_id,
        )

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
    return action in {ProductionRepairAction.INCLUDE, ProductionRepairAction.EXCLUDE}


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
            if action != ProductionRepairAction.INCLUDE.value:
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
                    "action": ProductionRepairAction.INCLUDE.value,
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
            entries, payload_available = await _repair_entries_for_artifact(
                base, self._artifact_store
            )
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
                and _enum_value(decision.action) == ProductionRepairAction.INCLUDE.value
            ]
            resolved_payload_objects = dict(
                zip(
                    (repair_key for repair_key, _entry, _hash in include_entries),
                    await self._payloads.resolve_many(
                        [entry for _key, entry, _hash in include_entries],
                        payload_available=payload_available,
                        value_sha256_by_index={
                            index: value_sha256
                            for index, (_key, _entry, value_sha256) in enumerate(include_entries)
                        },
                    ),
                    strict=True,
                )
            )
            resolved_payloads = {
                repair_key: payload.value
                for repair_key, payload in resolved_payload_objects.items()
                if payload.available and payload.value is not None
            }
            projector_entries = [
                dict(entry)
                | {
                    "repair_key": repair_key,
                    "kind": kind.value,
                    "value_sha256": value_sha256,
                }
                for repair_key, kind, entry, value_sha256 in active_entries
            ]
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

            projection_hashes = await self._projection_hashes(
                uow, run, current_extraction, projected
            )
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
                await uow.commit()
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
                )

            effective_for_base = [
                decision
                for repair_key, _kind, _entry, _hash in active_entries
                if (decision := decisions_by_key.get(repair_key)) is not None
            ]
            effective_for_base.sort(key=lambda item: (item.repair_key, item.id))
            effective_decision_payload = [
                [item.repair_key, _enum_value(item.action), str(item.id)]
                for item in effective_for_base
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
            await uow.commit()
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
            and _enum_value(decision.action) == ProductionRepairAction.INCLUDE.value
            for decision in decisions
        )
    ]
    resolver = payload_resolver or ProductionRepairPayloadResolver()
    payload_objects = dict(
        zip(
            (repair_key for repair_key, _entry, _hash in include_entries),
            await resolver.resolve_many(
                [entry for _key, entry, _hash in include_entries],
                payload_available=payload_available,
                value_sha256_by_index={
                    index: value_sha256
                    for index, (_key, _entry, value_sha256) in enumerate(include_entries)
                },
            ),
            strict=True,
        )
    )
    projected = EffectiveExtractionProjector().project(
        base=base,
        repair_entries=[
            dict(entry)
            | {
                "repair_key": repair_key,
                "kind": kind.value,
                "value_sha256": value_sha256,
            }
            for repair_key, kind, entry, value_sha256 in active_entries
        ],
        effective_decisions=decisions,
        resolved_payloads={
            repair_key: payload.value
            for repair_key, payload in payload_objects.items()
            if payload.available and payload.value is not None
        },
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

    @property
    def artifact(self) -> ProductionArtifact:
        return self.projection.artifact

    @property
    def impact(self) -> ProductionRepairImpact:
        return self.projection.impact

    @property
    def changed(self) -> bool:
        return self.projection.changed


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

    async def apply(
        self,
        *,
        edition_id: UUID,
        subject_id: UUID,
        actor_id: str,
        observed_run_id: UUID | None = None,
        observed_pipeline_generation: int | None = None,
    ) -> ProductionRepairMaterializationResult:
        actor_id = actor_id.strip()
        if not actor_id:
            raise ProductionRepairProjectionError("production_repair_actor_required")

        run = await self._fenced_run(
            edition_id=edition_id,
            subject_id=subject_id,
            observed_run_id=observed_run_id,
            observed_pipeline_generation=observed_pipeline_generation,
        )
        projection = await self._projection.project_effective_extraction(run.id, actor_id=actor_id)
        if projection.unresolved_count:
            return ProductionRepairMaterializationResult(
                projection=projection,
                action="awaiting_repair_decision",
            )

        impact = projection.impact
        if (
            not projection.changed
            or impact.kind is ProductionRepairImpactKind.NO_DELIVERABLE_CHANGE
        ):
            return ProductionRepairMaterializationResult(projection=projection, action="none")

        if impact.kind is ProductionRepairImpactKind.SOURCE_CORPUS:
            return ProductionRepairMaterializationResult(
                projection=projection,
                action="retry_required",
                retry_stage=SubjectProductionStage.REFERENCES.value,
                full_chain=True,
            )

        publication: ProductionArtifact | None = None
        qa_result: dict[str, Any] | None = None
        if impact.kind is ProductionRepairImpactKind.PUBLICATION_ONLY:
            publication, qa_result = await self._materialize_publication(
                edition_id=edition_id,
                subject_id=subject_id,
                run_id=run.id,
                pipeline_generation=run.pipeline_generation,
                extraction=projection.artifact,
            )
            action = "publication_reassembled"
        elif impact.kind is ProductionRepairImpactKind.RULE_BUNDLE_ONLY:
            qa_result = await self._materialize_rules_and_qa(
                edition_id=edition_id,
                subject_id=subject_id,
                run_id=run.id,
                pipeline_generation=run.pipeline_generation,
                extraction=projection.artifact,
            )
            action = "rules_materialized"
        else:
            await self._stale_narrative(
                edition_id=edition_id,
                subject_id=subject_id,
                run_id=run.id,
                pipeline_generation=run.pipeline_generation,
            )
            return ProductionRepairMaterializationResult(
                projection=projection,
                action="retry_required",
                retry_stage=SubjectProductionStage.SYNTHESIS.value,
            )

        if self._checkpoint is not None:
            await self._checkpoint.checkpoint(run.id)
        return ProductionRepairMaterializationResult(
            projection=projection,
            action=action,
            publication_artifact=publication,
            qa=qa_result,
        )

    async def _fenced_run(
        self,
        *,
        edition_id: UUID,
        subject_id: UUID,
        observed_run_id: UUID | None,
        observed_pipeline_generation: int | None,
    ) -> Any:
        async with self._uow_factory() as uow:
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

    async def _materialize_publication(
        self,
        *,
        edition_id: UUID,
        subject_id: UUID,
        run_id: UUID,
        pipeline_generation: int,
        extraction: ProductionArtifact,
    ) -> tuple[ProductionArtifact, dict[str, Any] | None]:
        async with self._uow_factory() as uow:
            await self._lock_edition_and_check(uow, edition_id)
            run: Any = await _get_for_update(uow.subject_production_runs, run_id)
            self._check_reviewable_run(run, edition_id, subject_id)
            if run.pipeline_generation != pipeline_generation:
                raise ProductionRepairStaleError(ProductionRepairStaleError.code)
            references = await uow.production_artifacts.get_current(
                run_id, ProductionArtifactStage.REFERENCES.value
            )
            synthesis = await uow.production_artifacts.get_current(
                run_id, ProductionArtifactStage.SYNTHESIS.value
            )
            if references is None or synthesis is None:
                raise ProductionRepairProjectionError("publication_inputs_missing")
            publication = await uow.production_artifacts.get_current(
                run_id, ProductionArtifactStage.PUBLICATION.value
            )
            if publication is not None:
                await self._mark_stages_stale(
                    uow, run_id, {ProductionArtifactStage.PUBLICATION.value}
                )
            title = await self._subject_title(uow, run_id, subject_id)
            new_publication = await self._assembly.assemble_publication_in_uow(
                uow,
                run_id,
                subject_id,
                title,
                references,
                extraction,
                synthesis,
            )
            qa_result = await self._run_qa(
                uow,
                run_id,
                references,
                extraction,
                synthesis,
                new_publication,
                subject_id,
                run,
            )
            await self._ensure_qa_passed(qa_result)
            await uow.commit()
            return new_publication, qa_result

    async def _materialize_rules_and_qa(
        self,
        *,
        edition_id: UUID,
        subject_id: UUID,
        run_id: UUID,
        pipeline_generation: int,
        extraction: ProductionArtifact,
    ) -> dict[str, Any] | None:
        async with self._uow_factory() as uow:
            await self._lock_edition_and_check(uow, edition_id)
            run: Any = await _get_for_update(uow.subject_production_runs, run_id)
            self._check_reviewable_run(run, edition_id, subject_id)
            if run.pipeline_generation != pipeline_generation:
                raise ProductionRepairStaleError(ProductionRepairStaleError.code)
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
                subject_id,
                run,
            )
            await self._ensure_qa_passed(qa_result)
            await uow.commit()
            return qa_result

    async def _stale_narrative(
        self,
        *,
        edition_id: UUID,
        subject_id: UUID,
        run_id: UUID,
        pipeline_generation: int,
    ) -> None:
        async with self._uow_factory() as uow:
            await self._lock_edition_and_check(uow, edition_id)
            run: Any = await _get_for_update(uow.subject_production_runs, run_id)
            self._check_reviewable_run(run, edition_id, subject_id)
            if run.pipeline_generation != pipeline_generation:
                raise ProductionRepairStaleError(ProductionRepairStaleError.code)
            await self._mark_stages_stale(
                uow,
                run_id,
                {
                    ProductionArtifactStage.SYNTHESIS.value,
                    ProductionArtifactStage.PUBLICATION.value,
                },
            )
            await uow.commit()

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
        evidence_basis=ProductionEvidenceBasis.ANALYST_OVERRIDE,
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
    return replace(
        verified.rules[0],
        source_ids=(source_id,),
        supported=True,
        model_run_ids=_entry_model_run_ids(entry),
        evidence_basis=ProductionEvidenceBasis.ANALYST_OVERRIDE,
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
            if applied_action == ProductionRepairAction.INCLUDE.value
            else RepairDecisionApplicationState.ALREADY_EFFECTIVE
        )
    if action == ProductionRepairAction.INCLUDE.value:
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


def repair_issue_blocks_signoff(issue: Any) -> bool:
    """Return whether a repair issue still changes the deliverable before freeze."""
    if (
        _enum_value(getattr(issue, "repair_state", None))
        == SupplementalSourceRepairState.ARCHIVED_PENDING_REFERENCES.value
    ):
        return True
    return repair_issue_application_state(issue) in {
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
            if reconciliation.report == canonical:
                await uow.commit()
                return ProductionReferenceRepairResult(artifact=base, changed=False)

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
            await uow.commit()
            return ProductionReferenceRepairResult(
                artifact=artifact,
                changed=True,
                restored_source_ids=reconciliation.restored_source_ids,
                restored_event_ids=reconciliation.restored_event_ids,
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


def _is_sha256(value: Any) -> bool:
    return isinstance(value, str) and bool(re.fullmatch(r"[0-9a-fA-F]{64}", value))


# LOT 18 used the shorter service name in design notes; keep it importable
# while retaining the explicit issue-service name used by existing callers.
ProductionRepairService = ProductionRepairIssueService
