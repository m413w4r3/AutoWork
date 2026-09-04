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
    TechnicalExtraction,
    parse_reference_report,
    reconcile_reference_report_with_archives,
    reference_report_from_json,
    reference_report_to_json,
    technical_extraction_from_json,
    technical_extraction_to_json,
)
from cti_app.application.production_stages import ExtractionService, compute_input_hash
from cti_app.domain.collection import CollectionState
from cti_app.domain.discovery import canonicalize_http_url
from cti_app.domain.editions import EditionStatus
from cti_app.domain.production import (
    DetectionRule,
    DetectionRuleType,
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionArtifactStatus,
    ProductionEvidenceBasis,
    ProductionReconciliationRequiredError,
    ProductionRepairAction,
    ProductionRepairDecision,
    ProductionRepairIssueKind,
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
        value
        if isinstance(value, ProductionRepairIssueKind)
        else ProductionRepairIssueKind(value)
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


class ProductionRepairStatusError(ValueError):
    """The edition cannot accept repair decisions in its current state."""

    code = "production_repair_status_invalid"


class ProductionRepairStaleError(ValueError):
    """The decision no longer addresses the current production generation."""

    code = "production_repair_stale"


class ProductionRepairResolvedError(ValueError):
    """The requested issue already has an effective append-only decision."""

    code = "production_repair_resolved"


class ProductionRepairIssueNotFoundError(ValueError):
    """A requested repair issue is not present in the current extraction."""


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
            effective = await _effective_decisions_for_reader(
                uow, edition_id, subject_id
            )
            if any(item.repair_key == repair_key for item in effective):
                raise ProductionRepairResolvedError(ProductionRepairResolvedError.code)

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
                current_artifact = await get_current(
                    production_run_id, _enum_value(artifact.stage)
                )
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
            effective_keys = {(item.subject_id, item.repair_key) for item in effective}

            for item, _event in zip(decisions, events, strict=True):
                if (item.subject_id, item.repair_key) in effective_keys:
                    raise ProductionRepairResolvedError(ProductionRepairResolvedError.code)
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
    effective_decision: ProductionRepairDecision | None = None
    projection_applied: bool = False
    subject_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class ProductionRepairIssueDetail:
    """One issue plus its full inert value when the evidence pack has it."""

    issue: ProductionRepairIssueView
    value: str | None

    @property
    def payload_available(self) -> bool:
        return self.issue.payload_available


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
    repair_state: SupplementalSourceRepairState = (
        SupplementalSourceRepairState.UNARCHIVED
    )
    rebuild_required: bool = False
    effective_decision: ProductionRepairDecision | None = None
    recommended_action: str = "archive_manual_content"
    subject_id: UUID | None = None


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
    ) -> None:
        self._uow_factory = uow_factory
        self._artifact_store = artifact_store

    async def list_issues(
        self, edition_id: UUID, subject_id: UUID | None = None
    ) -> tuple[ProductionRepairIssueView, ...]:
        return tuple(
            view
            for view, _value in await self._records(edition_id, subject_id=subject_id)
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
            for view, _value in await self._records(
                edition_id, subject_id=subject_id, load_payload=False
            )
        )

    async def get_issue(
        self, edition_id: UUID, repair_key: str, subject_id: UUID | None = None
    ) -> ProductionRepairIssueDetail | None:
        for view, value in await self._records(edition_id, subject_id=subject_id):
            if view.repair_key == repair_key:
                return ProductionRepairIssueDetail(issue=view, value=value)
        return None

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
                    collections_by_subject.setdefault(collection.subject_id, []).append(
                        collection
                    )
            elif collection_repository is not None:
                for current_subject_id in subject_ids:
                    collections_by_subject[current_subject_id] = (
                        await collection_repository.list_for_subject(current_subject_id)
                    )
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
                contexts.append(
                    (run, artifact, collections, _reference_source_index(artifact))
                )
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
                repair_state, recommended_action = _supplemental_repair_state(
                    collection, decision
                )
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
                            getattr(collection, "id", None)
                            if collection is not None
                            else None
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
    ) -> list[tuple[ProductionRepairIssueView, str | None]]:
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
                    contexts.append(
                        _RepairContext(run=run, artifact=artifact, source_titles={})
                    )
            decisions = await _effective_decisions_for_reader(
                uow, edition_id, subject_id
            )

        decisions_by_key = {
            (decision.subject_id, decision.repair_key): decision for decision in decisions
        }
        records: list[tuple[ProductionRepairIssueView, str | None]] = []
        for context in contexts:
            entries, payload_available = await self._entries(
                context.artifact, load_payload=load_payload
            )
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
                    records.append(
                        (
                            replace(
                                view,
                                effective_decision=decisions_by_key.get(
                                    (context.run.subject_id, view.repair_key)
                                ),
                            ),
                            value,
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
class ProductionRepairProjectionResult:
    """Result of materializing the effective extraction projection."""

    artifact: ProductionArtifact
    changed: bool
    accepted_indicator_count: int = 0
    accepted_rule_count: int = 0
    unresolved_count: int = 0
    included_repair_keys: tuple[str, ...] = ()
    excluded_repair_keys: tuple[str, ...] = ()
    unresolved_repair_keys: tuple[str, ...] = ()
    # INCLUDE decisions the deterministic pipeline cannot rebuild. Recorded,
    # never fatal: the append-only log would otherwise freeze the article.
    unbuildable_repair_keys: tuple[str, ...] = ()


class ProductionRepairProjectionService:
    """Build an immutable effective extraction from Q2 plus append-only decisions."""

    _PROJECTION_VERSION = "1"
    _CANONICAL_BUCKET = "production-artifacts-canonical"

    def __init__(
        self,
        uow_factory: ProductionUnitOfWorkFactory,
        artifact_store: ProductionArtifactStore | None = None,
        extraction_service: ExtractionService | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._artifact_store = artifact_store
        self._extraction = extraction_service or ExtractionService(
            uow_factory, artifact_store
        )

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
                raise ProductionRepairProjectionError(
                    "extraction_payload_unavailable"
                ) from exc
            entries, payload_available = await _repair_entries_for_artifact(
                base, self._artifact_store
            )
            decisions = await _effective_decisions_for_reader(
                uow, run.edition_id, run.subject_id
            )
            decisions_by_key = {
                decision.repair_key: decision
                for decision in decisions
                if decision.subject_id == run.subject_id
            }

            active_entries: list[tuple[str, ProductionRepairIssueKind, dict[str, Any]]] = []
            for entry in entries:
                identity = _repair_entry_identity(
                    entry,
                    edition_id=run.edition_id,
                    subject_id=run.subject_id,
                    payload_available=payload_available,
                )
                if identity is not None:
                    active_entries.append((identity[0], identity[1], entry))

            items = list(base_extraction.items)
            rules = list(base_extraction.rules)
            included: list[str] = []
            excluded: list[str] = []
            unresolved: list[str] = []
            unbuildable: list[str] = []
            accepted_indicator_count = 0
            accepted_rule_count = 0
            additions: list[ExtractionItem] = []
            rule_additions: list[DetectionRule] = []

            for repair_key, kind, entry in sorted(
                active_entries, key=lambda value: (value[1].value, value[0])
            ):
                decision = decisions_by_key.get(repair_key)
                if decision is None:
                    unresolved.append(repair_key)
                    continue
                action = _enum_value(decision.action)
                if action == ProductionRepairAction.EXCLUDE.value:
                    excluded.append(repair_key)
                    continue
                if action != ProductionRepairAction.INCLUDE.value:
                    unresolved.append(repair_key)
                    continue
                value = entry.get("value")
                if not payload_available or not isinstance(value, str):
                    raise ProductionRepairProjectionError("repair_payload_unavailable")
                value_sha256 = str(
                    entry.get("value_sha256") or entry.get("value_hash") or ""
                ).casefold()
                if value_sha256 != _sha256(value):
                    raise ProductionRepairProjectionError("repair_payload_hash_mismatch")
                try:
                    if kind is ProductionRepairIssueKind.REJECTED_RULE:
                        rule_additions.append(
                            _build_override_rule(entry, value, repair_key)
                        )
                        accepted_rule_count += 1
                    else:
                        additions.append(_build_override_item(entry, value, repair_key))
                        if is_publication_ioc_artifact_type(entry.get("artifact_type")):
                            accepted_indicator_count += 1
                except (KeyError, TypeError, ValueError):
                    # The decision log is append-only, so raising here would
                    # make the article permanently unbuildable. Record the
                    # honoured-but-unbuildable include and keep projecting; the
                    # decision endpoint refuses such an include up front.
                    unbuildable.append(repair_key)
                    continue
                included.append(repair_key)

            projected = TechnicalExtraction(
                items=_merge_projection_items(items, additions),
                uncertainties=base_extraction.uncertainties,
                rules=_merge_projection_rules(rules, rule_additions),
            )
            current_extraction = base_extraction
            if current.id != base.id:
                try:
                    current_extraction = technical_extraction_from_json(
                        await self._artifact_store.read_json(current.canonical_blob_id)
                    )
                except Exception:
                    current_extraction = base_extraction

            if projected == current_extraction:
                await uow.commit()
                return ProductionRepairProjectionResult(
                    artifact=current,
                    changed=False,
                    accepted_indicator_count=accepted_indicator_count,
                    accepted_rule_count=accepted_rule_count,
                    unresolved_count=len(unresolved),
                    included_repair_keys=tuple(sorted(included)),
                    excluded_repair_keys=tuple(sorted(excluded)),
                    unresolved_repair_keys=tuple(sorted(unresolved)),
                    unbuildable_repair_keys=tuple(sorted(unbuildable)),
                )

            effective_for_base = [
                decision
                for repair_key, _kind, _entry in active_entries
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
                "decision_ids": [str(item.id) for item in effective_for_base],
                "included_repair_keys": sorted(included),
                "excluded_repair_keys": sorted(excluded),
                "unresolved_repair_keys": sorted(unresolved),
                "unbuildable_repair_keys": sorted(unbuildable),
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
                accepted_indicator_count=accepted_indicator_count,
                accepted_rule_count=accepted_rule_count,
                unresolved_count=len(unresolved),
                included_repair_keys=tuple(sorted(included)),
                excluded_repair_keys=tuple(sorted(excluded)),
                unresolved_repair_keys=tuple(sorted(unresolved)),
                unbuildable_repair_keys=tuple(sorted(unbuildable)),
            )


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
) -> tuple[str, ProductionRepairIssueKind, str] | None:
    """Derive the active issue key from immutable evidence, never its position."""
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
                str(entry.get("artifact_type"))
                if entry.get("artifact_type") is not None
                else None
            ),
            value_sha256=value_sha256,
        )
    except ValueError:
        return None
    return key, kind, source_id


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


def _build_override_item(
    entry: Mapping[str, Any], value: str, repair_key: str
) -> ExtractionItem:
    artifact_type = _entry_artifact_type(entry.get("artifact_type"))
    if artifact_type in {
        ArtifactType.YARA_RULE,
        ArtifactType.SIGMA_RULE,
        ArtifactType.SURICATA_RULE,
    } or artifact_type is ArtifactType.OTHER:
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


def _build_override_rule(
    entry: Mapping[str, Any], value: str, repair_key: str
) -> DetectionRule:
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


def _item_projection_key(item: ExtractionItem) -> tuple[str, str]:
    if item.artifact_type is None:
        return item.category, item.value.casefold()
    artifact_type = ArtifactType(item.artifact_type)
    normalized = item.normalized_value or canonical_indicator_key(item.value, artifact_type)
    return artifact_type.value, normalized


def _prefer_projection_object[T](
    previous: T, candidate: T, *, previous_basis: ProductionEvidenceBasis,
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
        if (
            artifact := await repository.get_current(run.id, stage)
        ) is not None
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
    if (
        decision is not None
        and decision.action is ProductionRepairAction.CONTINUE_WITHOUT_SOURCE
    ):
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
        source_url = (
            value.get("source_url") if isinstance(value, dict) else value
        )
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
    projection_marker = (
        context.artifact.metadata.get("repair_projection")
        if isinstance(getattr(context.artifact, "metadata", None), dict)
        else None
    )
    projection_decision_ids = (
        {
            str(value)
            for value in projection_marker.get("decision_ids", [])
        }
        if isinstance(projection_marker, dict)
        and isinstance(projection_marker.get("decision_ids"), list)
        else set()
    )
    view = ProductionRepairIssueView(
        repair_key=repair_key,
        kind=kind,
        artifact_type=artifact_type,
        source_id=source_id,
        source_title=str(
            entry.get("source_title") or context.source_titles.get(source_id, "")
        ),
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
        projection_applied=(
            effective_decision is not None
            and str(effective_decision.id) in projection_decision_ids
        ),
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


async def _archived_source_projection(
    uow: Any, subject_id: UUID
) -> tuple[tuple[str, str], ...]:
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
