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
from cti_app.application.production_parsers import (
    parse_reference_report,
    reconcile_reference_report_with_archives,
    reference_report_from_json,
    reference_report_to_json,
)
from cti_app.application.production_stages import compute_input_hash
from cti_app.domain.collection import CollectionState
from cti_app.domain.discovery import canonicalize_http_url
from cti_app.domain.editions import EditionStatus
from cti_app.domain.production import (
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionArtifactStatus,
    ProductionReconciliationRequiredError,
    ProductionRepairAction,
    ProductionRepairDecision,
    ProductionRepairIssueKind,
    SubjectProductionStatus,
)

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


class ProductionRepairIssueNotFoundError(ValueError):
    """A requested repair issue is not present in the current extraction."""


class ProductionReferenceRepairError(ValueError):
    """The archived Q1 evidence cannot be safely reconstructed."""

    def __init__(self, code: str, message: str | None = None) -> None:
        self.code = code
        super().__init__(message or code)


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
                raise ProductionRepairStatusError(
                    f"Edition {edition_id} is not in production or review"
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
    """A Q1 source proposal that still lacks an archived collection."""

    repair_key: str
    kind: ProductionRepairIssueKind
    source_id: str
    source_title: str
    source_url: str
    publisher: str | None
    collection_id: UUID
    collection_state: str
    error_reason: str | None
    attempt_count: int
    production_run_id: UUID
    observed_artifact_id: UUID
    observed_artifact_version: int
    observed_pipeline_generation: int
    effective_decision: ProductionRepairDecision | None = None
    recommended_action: str = "archive_manual_content"


# Name used by Repair Desk consumers that distinguish list DTOs from the
# existing Q2 evidence issue view.
SupplementalSourceRepairIssueView = SupplementalSourceRepairIssue


@dataclass(frozen=True, slots=True)
class _RepairContext:
    run: Any
    artifact: Any


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
            contexts: list[tuple[Any, Any, Sequence[Any]]] = []
            for run in runs:
                if subject_id is not None and run.subject_id != subject_id:
                    continue
                artifact = await uow.production_artifacts.get_current(run.id, "references")
                if artifact is None or (
                    _enum_value(artifact.status) == ProductionArtifactStatus.STALE.value
                ):
                    continue
                collections = await uow.source_collections.list_for_subject(run.subject_id)
                contexts.append((run, artifact, collections))
            decisions = await _effective_decisions_for_reader(uow, edition_id, subject_id)

        decisions_by_key = {
            (decision.subject_id, decision.repair_key): decision for decision in decisions
        }
        issues: list[SupplementalSourceRepairIssue] = []
        for run, artifact, collections in contexts:
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

            canonical_urls = {source.canonical_url for source in canonical.sources}
            collections_by_url = {
                collection.canonical_url: collection
                for collection in collections
                if getattr(collection, "canonical_url", None)
            }
            for source in proposed_result.value.sources:
                if source.canonical_url in canonical_urls:
                    continue
                collection = collections_by_url.get(source.canonical_url)
                if collection is None or _is_archived_collection(collection):
                    continue
                repair_key = repair_key_for_supplemental_source(
                    edition_id=edition_id,
                    subject_id=run.subject_id,
                    source_url=source.canonical_url,
                )
                decision = decisions_by_key.get((run.subject_id, repair_key))
                issues.append(
                    SupplementalSourceRepairIssue(
                        repair_key=repair_key,
                        kind=ProductionRepairIssueKind.SUPPLEMENTAL_SOURCE_UNARCHIVED,
                        source_id=source.local_id,
                        source_title=source.title,
                        source_url=source.canonical_url,
                        publisher=source.publisher,
                        collection_id=collection.id,
                        collection_state=_enum_value(getattr(collection, "state", "")),
                        error_reason=getattr(collection, "error_reason", None),
                        attempt_count=int(getattr(collection, "attempt_count", 0)),
                        production_run_id=run.id,
                        observed_artifact_id=artifact.id,
                        observed_artifact_version=artifact.version,
                        observed_pipeline_generation=run.pipeline_generation,
                        effective_decision=decision,
                        recommended_action=(
                            "continue_without_source"
                            if decision is not None
                            and decision.action is ProductionRepairAction.CONTINUE_WITHOUT_SOURCE
                            else "archive_manual_content"
                        ),
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
        self, edition_id: UUID, *, subject_id: UUID | None
    ) -> list[tuple[ProductionRepairIssueView, str | None]]:
        async with self._uow_factory() as uow:
            runs = await uow.subject_production_runs.list_for_edition(edition_id)
            contexts: list[_RepairContext] = []
            for run in runs:
                if subject_id is not None and run.subject_id != subject_id:
                    continue
                artifact = await uow.production_artifacts.get_current(run.id, "extraction")
                if artifact is not None and (
                    _enum_value(artifact.status) != ProductionArtifactStatus.STALE.value
                ):
                    contexts.append(_RepairContext(run=run, artifact=artifact))
            decisions = await _effective_decisions_for_reader(
                uow, edition_id, subject_id
            )

        decisions_by_key = {
            (decision.subject_id, decision.repair_key): decision for decision in decisions
        }
        records: list[tuple[ProductionRepairIssueView, str | None]] = []
        for context in contexts:
            entries, payload_available = await self._entries(context.artifact)
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

    async def _entries(self, artifact: Any) -> tuple[list[dict[str, Any]], bool]:
        metadata = getattr(artifact, "metadata", {}) or {}
        marker = metadata.get("repair_evidence") if isinstance(metadata, dict) else None
        blob_id = marker.get("blob_id") if isinstance(marker, dict) else None
        if self._artifact_store is not None and blob_id:
            try:
                pack = await self._artifact_store.read_repair_evidence(UUID(str(blob_id)))
            except Exception:
                pack = None
            if isinstance(pack, dict):
                entries = pack.get("entries")
                if isinstance(entries, list):
                    return [entry for entry in entries if isinstance(entry, dict)], True

        verification = (
            metadata.get("deterministic_verification", {})
            if isinstance(metadata, dict)
            else {}
        )
        if not isinstance(verification, dict):
            return [], False
        legacy_entries = verification.get("q2_source_evidence_rejections")
        if not isinstance(legacy_entries, list):
            legacy_entries = verification.get("q2_rejected_rules", [])
        return [entry for entry in legacy_entries if isinstance(entry, dict)], False


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


def _effective_from_history(
    history: Sequence[ProductionRepairDecision],
) -> tuple[ProductionRepairDecision, ...]:
    latest: dict[tuple[UUID, str], ProductionRepairDecision] = {}
    for decision in sorted(history, key=lambda item: (item.created_at, item.id)):
        latest[(decision.subject_id, decision.repair_key)] = decision
    return tuple(latest.values())


def _enum_value(value: Any) -> Any:
    return getattr(value, "value", value)


def _is_archived_collection(collection: Any) -> bool:
    return _enum_value(getattr(collection, "state", None)) in {
        CollectionState.ARCHIVED.value,
        CollectionState.EXTRACTED.value,
        CollectionState.COMPLETED.value,
    }


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
        value = None
        payload_available = False

    artifact_type = entry.get("artifact_type")
    artifact_type = str(artifact_type) if artifact_type is not None else None
    repair_key = entry.get("repair_key")
    if not isinstance(repair_key, str) or not _SHA256_RE.fullmatch(repair_key):
        try:
            repair_key = (
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

    preview_source = raw_value if isinstance(raw_value, str) else str(entry.get("preview", ""))
    preview = preview_source[:MAX_REPAIR_PREVIEW_CHARS]
    view = ProductionRepairIssueView(
        repair_key=repair_key,
        kind=kind,
        artifact_type=artifact_type,
        source_id=source_id,
        source_url=source_url,
        reason_code=str(entry.get("reason_code", "")),
        value_sha256=value_sha256,
        preview=preview,
        payload_available=value is not None,
        production_run_id=context.run.id,
        observed_artifact_id=context.artifact.id,
        observed_artifact_version=context.artifact.version,
        observed_pipeline_generation=context.run.pipeline_generation,
        model_run_id=(
            str(entry["model_run_id"]) if entry.get("model_run_id") is not None else None
        ),
        batch_id=str(entry["batch_id"]) if entry.get("batch_id") is not None else None,
        effective_decision=effective_decision,
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
