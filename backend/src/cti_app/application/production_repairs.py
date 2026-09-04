"""Stable production-repair identities, evidence packs and decision services."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, cast
from uuid import UUID

from cti_app.application.persistence import ProductionUnitOfWorkFactory
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.domain.discovery import canonicalize_http_url
from cti_app.domain.editions import EditionStatus
from cti_app.domain.production import (
    ProductionArtifactStatus,
    ProductionRepairAction,
    ProductionRepairDecision,
    ProductionRepairIssueKind,
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

            artifact = await uow.production_artifacts.get(observed_artifact_id)
            if (
                artifact is None
                or artifact.id != observed_artifact_id
                or artifact.production_run_id != production_run_id
                or _enum_value(artifact.status) == ProductionArtifactStatus.STALE.value
            ):
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
