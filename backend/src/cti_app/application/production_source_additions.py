"""Acquire an additive source and prepare versioned production artifacts."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date
from enum import StrEnum
from typing import Any, Protocol
from urllib.parse import urlsplit
from uuid import NAMESPACE_URL, UUID, uuid5

from cti_app.application.collection import (
    ManualContentAlreadyArchivedError,
    ManualContentEmptyError,
    ManualContentTooLargeError,
    ManualContentTypeError,
    SubjectCollectionService,
    SupplementalSource,
)
from cti_app.application.collection_errors import CollectionNotAllowedError
from cti_app.application.persistence import ProductionUnitOfWorkFactory
from cti_app.application.production_access_policy import resolve_source_policy
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.application.production_extraction import (
    ExtractionExecutionStatus,
    ProductionExtractionControlError,
    ProductionExtractionService,
    production_extraction_metadata,
)
from cti_app.application.production_references import (
    ProductionReferenceProposal,
    build_production_reference_corpus,
    observe_reference_collections,
    production_reference_corpus_from_json,
    production_reference_corpus_metadata,
    production_reference_corpus_to_json,
)
from cti_app.application.production_relevance import (
    relevance_projection_input_hash,
)
from cti_app.application.production_repairs import (
    _decision_for_current_source_issue,
    repair_key_for_supplemental_source,
)
from cti_app.application.production_synthesis import (
    canonical_extraction_hash,
)
from cti_app.domain.collection import (
    CollectionFailureReason,
    CollectionState,
    SourceOriginKind,
)
from cti_app.domain.discovery import SourceRole, canonicalize_http_url
from cti_app.domain.production import (
    EXTRACTION_PROFILE_POLICY_VERSION,
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionArtifactStatus,
    ProductionInputSnapshot,
    ProductionRepairAction,
    ProductionRun,
    ProductionRunStatus,
    ProductionStage,
    SourceExtractionStatus,
)
from cti_app.domain.production_extraction import (
    ExtractionReuseState,
    ProductionExtractionV1,
    production_extraction_from_json,
    production_extraction_to_json,
)
from cti_app.domain.production_references import (
    PRODUCTION_SOURCE_CRITICALITY_POLICY_VERSION,
    ProductionEditorialRole,
    ProductionReferenceCorpusV1,
    ProductionReferenceKind,
    ProductionReferenceTier,
    ProductionSourceCriticality,
    ProductionSourceCriticalityReason,
    production_source_criticality,
)
from cti_app.domain.production_relevance import (
    DEFAULT_RELEVANCE_CLASSIFIER_VERSION,
    RELEVANCE_PROJECTION_POLICY_VERSION,
    RelevanceClassification,
    RelevanceClassificationItemV1,
    RelevanceDecisionProvenance,
    RelevanceProjectionV1,
    RelevanceReasonCode,
    relevance_projection_from_json,
    relevance_projection_to_json,
    validate_relevance_projection_lineage,
)
from cti_app.domain.production_source_additions import (
    SourceAdditionArtifactFenceV1,
    SourceAdditionArtifactProjectionV1,
    SourceAdditionJournalEntryV1,
    SourceAdditionOrigin,
    SourceAdditionPromotionPlanV1,
    SourceAdditionState,
    SourceAdditionV1,
    source_addition_manifest,
    source_addition_source_key,
    source_additions_hash,
)
from cti_app.domain.production_synthesis import extraction_evidence_refs_v1


class SourceAdditionRefusalCode(StrEnum):
    SOURCE_DATE_AFTER_RUN = "source_date_after_run"
    SOURCE_ALREADY_IN_REFERENCE_CORPUS = "source_already_in_reference_corpus"
    ARCHIVE_NOT_ADMISSIBLE = "archive_not_admissible"
    MANUAL_ARCHIVE_BYTES_REQUIRED = "manual_archive_bytes_required"
    MANUAL_ARCHIVE_NOT_ALLOWED = "manual_archive_not_allowed"
    ARCHIVE_RECEIPT_INCOMPLETE = "archive_receipt_incomplete"
    FINAL_URL_OUT_OF_SCOPE = "final_url_out_of_scope"
    SOURCE_POLICY_BLOCKED = "source_policy_blocked"
    SOURCE_EXTRACTION_FAILED = "source_extraction_failed"
    SOURCE_EXTRACTION_NO_EVIDENCE = "source_extraction_no_evidence"
    SOURCE_EXTRACTION_LINEAGE_MISSING = "source_extraction_lineage_missing"
    BASE_ARTIFACTS_MISSING = "base_artifacts_missing"
    BASE_ARTIFACTS_INVALID = "base_artifacts_invalid"


@dataclass(frozen=True, slots=True)
class SourceAdditionRefusal:
    code: SourceAdditionRefusalCode
    recommended_restart_stage: ProductionStage | None
    reason: str
    details: tuple[tuple[str, str], ...] = ()


class SourceAdditionError(RuntimeError):
    code = "source_addition_error"


class SourceAdditionConflictError(SourceAdditionError):
    code = "source_addition_fence_conflict"


class SourceAdditionPromotionBlockedError(SourceAdditionError):
    code = "source_addition_promotion_blocked"

    def __init__(self, blockers: Sequence[SourceAdditionBlocker]) -> None:
        super().__init__(self.code)
        self.blockers = tuple(blockers)


@dataclass(frozen=True, slots=True)
class SourceAdditionBlocker:
    source_url: str
    repair_key: str
    criticality: ProductionSourceCriticality
    criticality_reason: ProductionSourceCriticalityReason
    failure_code: str | None
    reason: str


@dataclass(frozen=True, slots=True)
class SourceAdditionResult:
    addition: SourceAdditionV1
    refusal: SourceAdditionRefusal | None = None
    model_calls: int = 0
    reused_checkpoints: int = 0


@dataclass(frozen=True, slots=True)
class SourceAdditionArtifactPreview:
    stage: ProductionArtifactStage
    artifact_id: UUID
    version: int
    input_hash: str
    canonical_sha256: str


@dataclass(frozen=True, slots=True)
class SourceAdditionPreview:
    production_run_id: UUID
    request_id: UUID
    pipeline_generation: int
    source_additions: tuple[Mapping[str, Any], ...]
    source_additions_hash: str
    base_artifacts: tuple[SourceAdditionArtifactFenceV1, ...]
    projected_artifacts: tuple[SourceAdditionArtifactPreview, ...]
    stages_preserved: tuple[str, ...]
    stages_rebased: tuple[str, ...]
    stages_requiring_followup: tuple[str, ...]
    blockers: tuple[SourceAdditionBlocker, ...]
    model_calls: Mapping[str, Mapping[str, int]]
    ready: bool


@dataclass(frozen=True, slots=True)
class SourceAdditionPromotionResult:
    production_run_id: UUID
    request_id: UUID
    artifacts: tuple[ProductionArtifact, ...]
    source_additions_hash: str
    already_promoted: bool
    promotion_plan: SourceAdditionPromotionPlanV1 | None = None


@dataclass(frozen=True, slots=True)
class _AdditionContext:
    run: ProductionRun
    snapshot: ProductionInputSnapshot
    references_artifact: ProductionArtifact
    extraction_artifact: ProductionArtifact
    projection_artifact: ProductionArtifact
    references: ProductionReferenceCorpusV1
    extraction: ProductionExtractionV1
    projection: RelevanceProjectionV1
    references_bytes: bytes
    extraction_bytes: bytes
    projection_bytes: bytes


class BoundedEditorialResourceProposal(Protocol):
    """The existing bounded enrichment proposal shape; proposal is not evidence."""

    url: str


class SourceAdditionService:
    """Manage source-addition intents without mutating downstream artifacts.

    ``promote_in_uow`` is the transaction boundary for E2a-2. The caller owns
    the UoW and commits it together with its new SYNTHESIS artifact.
    """

    def __init__(
        self,
        *,
        uow_factory: ProductionUnitOfWorkFactory,
        collection_service: SubjectCollectionService,
        extraction_service: ProductionExtractionService,
        artifact_store: ProductionArtifactStore,
        selection_criterion_version: str = "source-selection-v1",
    ) -> None:
        self._uow_factory = uow_factory
        self._collection_service = collection_service
        self._extraction_service = extraction_service
        self._artifact_store = artifact_store
        self._selection_criterion_version = selection_criterion_version

    async def record_candidate(
        self,
        *,
        production_run_id: UUID,
        request_id: UUID,
        url: str,
        origin: SourceAdditionOrigin,
        title: str | None = None,
        publisher: str | None = None,
        published_at: date | None = None,
        kind: ProductionReferenceKind = ProductionReferenceKind.TECHNICAL_RESOURCE,
        role: SourceRole = SourceRole.UNKNOWN,
    ) -> SourceAdditionV1:
        """Persist one candidate; callers pass bounded discovery proposals as candidates.

        This command stores no proposal as evidence. Only a later collection
        receipt and archive-backed extraction can produce usable handles.
        """
        canonical_url = canonicalize_http_url(url)
        tier = (
            ProductionReferenceTier.SUPPORTING
            if kind is ProductionReferenceKind.PUBLICATION
            else ProductionReferenceTier.TECHNICAL
        )
        async with self._uow_factory() as uow:
            run = await uow.production_runs.get_for_update(production_run_id)
            if run is None:
                raise SourceAdditionError("production_run_not_found")
            self._require_revisionable_run(run)
            source_key = source_addition_source_key(canonical_url)
            previous = await uow.production_source_additions.get_latest_for_identity(
                run.id, request_id, source_key
            )
            if previous is not None:
                addition = previous.addition
                if (
                    addition.canonical_url != canonical_url
                    or addition.origin is not origin
                    or addition.kind is not kind
                ):
                    raise SourceAdditionConflictError("source_addition_request_identity_changed")
                return addition
            addition_id = uuid5(
                NAMESPACE_URL,
                f"source-addition:{run.id}:{request_id}:{source_key}",
            )
            addition = SourceAdditionV1(
                addition_id=addition_id,
                request_id=request_id,
                production_run_id=run.id,
                subject_id=run.subject_id,
                state=SourceAdditionState.CANDIDATE,
                origin=origin,
                canonical_url=canonical_url,
                title=_clean_optional(title),
                publisher=_clean_optional(publisher),
                published_at=published_at,
                tier=tier,
                kind=kind,
                role=role,
                editorial_role=ProductionEditorialRole.CONTEXT,
            )
            await uow.production_source_additions.append(
                SourceAdditionJournalEntryV1(addition=addition, version=1)
            )
            await uow.commit()
            return addition

    async def record_discovery_proposals(
        self,
        *,
        production_run_id: UUID,
        request_id: UUID,
        proposals: Sequence[ProductionReferenceProposal | BoundedEditorialResourceProposal],
    ) -> tuple[SourceAdditionV1, ...]:
        """Journal outputs from existing bounded proposal flows as candidates only.

        ``EditorialResourceProposalV1`` values from the bounded enrichment
        search expose ``url`` and are accepted directly; this service does not
        rerun that search or treat its output as evidence.
        """
        additions: list[SourceAdditionV1] = []
        for proposal in proposals:
            if isinstance(proposal, ProductionReferenceProposal):
                additions.append(
                    await self.record_candidate(
                        production_run_id=production_run_id,
                        request_id=request_id,
                        url=proposal.canonical_url,
                        origin=SourceAdditionOrigin.DISCOVERY,
                        title=proposal.title,
                        publisher=proposal.publisher,
                        published_at=proposal.published_at,
                        kind=proposal.kind,
                        role=proposal.role,
                    )
                )
            else:
                additions.append(
                    await self.record_candidate(
                        production_run_id=production_run_id,
                        request_id=request_id,
                        url=proposal.url,
                        origin=SourceAdditionOrigin.DISCOVERY,
                        kind=ProductionReferenceKind.TECHNICAL_RESOURCE,
                        role=SourceRole.UNKNOWN,
                    )
                )
        return tuple(additions)

    async def select(
        self,
        *,
        production_run_id: UUID,
        request_id: UUID,
        url: str,
        criterion: str,
        actor_id: str,
        criterion_version: str | None = None,
    ) -> SourceAdditionV1:
        canonical_url = canonicalize_http_url(url)
        normalized_criterion = _required_text(criterion, label="selection_criterion", limit=1000)
        normalized_actor = _required_text(actor_id, label="selected_by", limit=255)
        selected_version = criterion_version or self._selection_criterion_version
        selected_version = _required_text(
            selected_version, label="selection_criterion_version", limit=120
        )
        async with self._uow_factory() as uow:
            run = await uow.production_runs.get_for_update(production_run_id)
            if run is None:
                raise SourceAdditionError("production_run_not_found")
            self._require_revisionable_run(run)
            entry = await uow.production_source_additions.get_latest_for_identity(
                run.id, request_id, source_addition_source_key(canonical_url)
            )
            if entry is None:
                raise SourceAdditionError("source_addition_candidate_not_found")
            addition = entry.addition
            if addition.state is not SourceAdditionState.CANDIDATE:
                if (
                    addition.selection_criterion == normalized_criterion
                    and addition.selection_criterion_version == selected_version
                    and addition.selected_by == normalized_actor
                ):
                    return addition
                raise SourceAdditionConflictError("source_addition_selection_already_fenced")
            selected = replace(
                addition,
                state=SourceAdditionState.SELECTED,
                selection_criterion=normalized_criterion,
                selection_criterion_version=selected_version,
                selected_by=normalized_actor,
            )
            next_entry = SourceAdditionJournalEntryV1(
                addition=selected,
                version=entry.version + 1,
            )
            await uow.production_source_additions.append(next_entry)
            await uow.commit()
            return selected

    async def acquire(
        self,
        *,
        production_run_id: UUID,
        request_id: UUID,
        url: str,
        job_id: UUID | None = None,
        manual_content: bytes | None = None,
        declared_mime_type: str | None = None,
        final_url: str | None = None,
    ) -> SourceAdditionResult:
        """Archive, extract, and persist a prospective additive artifact set."""
        canonical_url = canonicalize_http_url(url)
        entry = await self._latest_entry(production_run_id, request_id, canonical_url)
        if entry is None:
            raise SourceAdditionError("source_addition_candidate_not_found")
        addition = entry.addition
        if addition.state is SourceAdditionState.READY:
            return SourceAdditionResult(addition=addition, reused_checkpoints=1)
        if addition.state is SourceAdditionState.REFUSED:
            return SourceAdditionResult(
                addition=addition, refusal=self._refusal_from_addition(addition)
            )
        if addition.state is SourceAdditionState.CANDIDATE:
            raise SourceAdditionError("source_addition_requires_selection")

        run = await self._load_run(production_run_id)
        research_date = run.research_date
        if research_date is None:
            raise SourceAdditionError("source_addition_run_date_missing")
        if addition.published_at is not None and addition.published_at > research_date:
            date_refusal = SourceAdditionRefusal(
                code=SourceAdditionRefusalCode.SOURCE_DATE_AFTER_RUN,
                recommended_restart_stage=ProductionStage.REFERENCES,
                reason="La date de publication est postérieure à la date de recherche du run.",
                details=(("published_at", addition.published_at.isoformat()),),
            )
            refused = await self._refuse(entry, date_refusal)
            return SourceAdditionResult(addition=refused, refusal=date_refusal)

        if addition.state is SourceAdditionState.SELECTED:
            archived, refusal = await self._archive_selected(
                addition,
                job_id=job_id,
                manual_content=manual_content,
                declared_mime_type=declared_mime_type,
                final_url=final_url,
                research_date=research_date,
            )
            if refusal is not None:
                refused = await self._refuse(entry, refusal)
                return SourceAdditionResult(addition=refused, refusal=refusal)
            assert archived is not None
            entry = await self._save_transition(
                expected_version=entry.version,
                addition=archived,
                production_run_id=production_run_id,
                request_id=request_id,
                canonical_url=canonical_url,
            )
        return await self._prepare_prospective(
            production_run_id=production_run_id,
            request_id=request_id,
            canonical_url=canonical_url,
            entry=entry,
        )

    async def preview(self, *, production_run_id: UUID, request_id: UUID) -> SourceAdditionPreview:
        async with self._uow_factory() as uow:
            context = await self._load_context(uow, production_run_id)
            entries = await uow.production_source_additions.list_latest_for_request(
                production_run_id, request_id
            )
            decisions = await self._effective_repair_decisions(uow, context.run)
            blockers = await self._critical_source_blockers(
                uow,
                context.run,
                context.references_artifact,
                context.snapshot,
                context.references,
                decisions,
            )
        additions = tuple(
            entry.addition
            for entry in entries
            if entry.addition.state in {SourceAdditionState.EXTRACTED, SourceAdditionState.READY}
        )
        ready_entries = tuple(
            entry for entry in entries if entry.addition.state is SourceAdditionState.READY
        )
        plan = ready_entries[0].promotion_plan if ready_entries else None
        if ready_entries and any(entry.promotion_plan != plan for entry in ready_entries):
            raise SourceAdditionConflictError("source_addition_projection_plan_mismatch")
        manifest = source_addition_manifest(additions)
        manifest_hash = source_additions_hash(additions)
        projected = (
            tuple(
                SourceAdditionArtifactPreview(
                    stage=item.stage,
                    artifact_id=item.artifact_id,
                    version=item.version,
                    input_hash=item.input_hash,
                    canonical_sha256=item.canonical_sha256,
                )
                for item in plan.projected_artifacts
            )
            if plan is not None
            else ()
        )
        fully_ready = (
            bool(ready_entries)
            and len(ready_entries) == len(entries)
            and all(entry.addition.state is SourceAdditionState.READY for entry in entries)
        )
        extraction_calls_made = sum(
            item.reuse_state is ExtractionReuseState.FRESH for item in additions
        )
        extraction_reuses = sum(
            item.reuse_state
            in {ExtractionReuseState.REUSED, ExtractionReuseState.CONTENT_DUPLICATE}
            for item in additions
        )
        has_projection = plan is not None and fully_ready
        old_checkpoint_reuses = len(
            {source.checkpoint_id for source in context.extraction.sources if source.checkpoint_id}
        )
        remaining_extraction = (
            0
            if has_projection
            else sum(
                entry.addition.state in {SourceAdditionState.SELECTED, SourceAdditionState.ARCHIVED}
                for entry in entries
            )
        )
        calls = {
            "bounded_resource_search": {"made": 0, "remaining": 0},
            "old_source_extraction": {
                "made": 0,
                "remaining": 0,
                "checkpoint_reuses": old_checkpoint_reuses,
            },
            "addition_extraction": {
                "made": extraction_calls_made,
                "remaining": remaining_extraction,
                "checkpoint_reuses": extraction_reuses,
            },
            "relevance_rebase": {"made": 0, "remaining": 0},
            "synthesis_revision": {"made": 0, "remaining": 1},
            "publication_render": {"made": 0, "remaining": 0},
        }
        return SourceAdditionPreview(
            production_run_id=production_run_id,
            request_id=request_id,
            pipeline_generation=context.run.pipeline_generation,
            source_additions=manifest,
            source_additions_hash=manifest_hash,
            base_artifacts=tuple(
                _artifact_fence(artifact, payload)
                for artifact, payload in (
                    (context.references_artifact, context.references_bytes),
                    (context.extraction_artifact, context.extraction_bytes),
                    (context.projection_artifact, context.projection_bytes),
                )
            ),
            projected_artifacts=projected,
            stages_preserved=("production_input_snapshot", "old_source_checkpoints"),
            stages_rebased=(
                ProductionArtifactStage.REFERENCES.value,
                ProductionArtifactStage.EXTRACTION.value,
                ProductionArtifactStage.RELEVANCE_PROJECTION.value,
            )
            if has_projection
            else (),
            stages_requiring_followup=(ProductionArtifactStage.SYNTHESIS.value,),
            blockers=blockers,
            model_calls=calls,
            ready=has_projection and not blockers,
        )

    async def promote(
        self,
        uow: Any,
        *,
        production_run_id: UUID,
        request_id: UUID,
    ) -> SourceAdditionPromotionResult:
        """Append prospective heads inside the caller's transaction.

        Delivery 2 must append its SYNTHESIS revision to the same UoW and
        commit once after both operations succeed.
        """
        return await self.promote_in_uow(
            uow, production_run_id=production_run_id, request_id=request_id
        )

    async def promote_in_uow(
        self,
        uow: Any,
        *,
        production_run_id: UUID,
        request_id: UUID,
    ) -> SourceAdditionPromotionResult:
        """Append REFERENCES, EXTRACTION and projection in the caller's transaction.

        This method never commits and never stales historical artifacts. The
        E2a-2 transaction should call this first, append its new SYNTHESIS in
        the same UoW, then commit once. Any error must unwind that UoW.
        """
        run = await uow.production_runs.get_for_update(production_run_id)
        if run is None:
            raise SourceAdditionConflictError("source_addition_run_missing")
        self._require_revisionable_run(run)
        current_run = await uow.production_runs.get_current_for_subject(run.subject_id)
        if current_run is None or current_run.id != run.id:
            raise SourceAdditionConflictError("source_addition_run_not_current")
        entries = await uow.production_source_additions.list_latest_for_request(run.id, request_id)
        ready = tuple(
            entry for entry in entries if entry.addition.state is SourceAdditionState.READY
        )
        if not ready:
            raise SourceAdditionError("source_addition_not_ready")
        if len(ready) != len(entries):
            raise SourceAdditionError("source_addition_request_not_fully_ready")
        plan = ready[0].promotion_plan
        if plan is None or any(entry.promotion_plan != plan for entry in ready):
            raise SourceAdditionConflictError("source_addition_projection_plan_mismatch")
        ready_additions_hash = source_additions_hash(tuple(entry.addition for entry in ready))
        if ready_additions_hash != plan.source_additions_hash:
            raise SourceAdditionConflictError("source_addition_manifest_changed")

        active_items: list[ProductionArtifact | None] = []
        for item in plan.projected_artifacts:
            active_items.append(
                await uow.production_artifacts.get_current(run.id, item.stage.value)
            )
        active = tuple(active_items)
        if all(
            item is not None and item.id == projection.artifact_id
            for item, projection in zip(active, plan.projected_artifacts, strict=True)
        ):
            return SourceAdditionPromotionResult(
                production_run_id=run.id,
                request_id=request_id,
                artifacts=tuple(item for item in active if item is not None),
                source_additions_hash=plan.source_additions_hash,
                already_promoted=True,
                promotion_plan=plan,
            )

        if run.pipeline_generation != plan.pipeline_generation:
            raise SourceAdditionConflictError("source_addition_generation_changed")
        base_by_stage = {item.stage: item for item in plan.base_artifacts}
        current_base: dict[ProductionArtifactStage, ProductionArtifact] = {}
        for stage in (
            ProductionArtifactStage.REFERENCES,
            ProductionArtifactStage.EXTRACTION,
            ProductionArtifactStage.RELEVANCE_PROJECTION,
        ):
            artifact = await uow.production_artifacts.get_current_for_revision(run.id, stage.value)
            fence = base_by_stage[stage]
            if (
                artifact is None
                or artifact.id != fence.artifact_id
                or artifact.version != fence.version
                or artifact.input_hash != fence.input_hash
                or artifact.status is not ProductionArtifactStatus.VERIFIED
                or artifact.canonical_blob_id is None
            ):
                raise SourceAdditionConflictError("source_addition_base_artifact_changed")
            if await self._blob_sha256(artifact.canonical_blob_id) != fence.canonical_sha256:
                raise SourceAdditionConflictError("source_addition_base_artifact_hash_changed")
            current_base[stage] = artifact

        snapshot = await uow.production_input_snapshots.get_by_run(run.id)
        references = await self._read_references(current_base[ProductionArtifactStage.REFERENCES])
        decisions = await self._effective_repair_decisions(uow, run)
        blockers = await self._critical_source_blockers(
            uow,
            run,
            current_base[ProductionArtifactStage.REFERENCES],
            snapshot,
            references,
            decisions,
        )
        if blockers:
            raise SourceAdditionPromotionBlockedError(blockers)

        manifest = source_addition_manifest(tuple(entry.addition for entry in ready))
        manifest_json = [dict(item) for item in manifest]
        promoted: list[ProductionArtifact] = []
        for projection in plan.projected_artifacts:
            blob_bytes = await self._artifact_store.read_bytes(projection.canonical_blob_id)
            if hashlib.sha256(blob_bytes).hexdigest() != projection.canonical_sha256:
                raise SourceAdditionConflictError("source_addition_projected_blob_hash_changed")
            payload = json.loads(blob_bytes)
            if not isinstance(payload, dict):
                raise SourceAdditionConflictError("source_addition_projected_blob_invalid")
            metadata = self._projected_artifact_metadata(
                projection.stage,
                payload,
                manifest=manifest_json,
                manifest_hash=plan.source_additions_hash,
                request_id=request_id,
            )
            artifact = ProductionArtifact(
                production_run_id=run.id,
                subject_id=run.subject_id,
                stage=projection.stage,
                version=projection.version,
                input_hash=projection.input_hash,
                status=ProductionArtifactStatus.VERIFIED,
                canonical_blob_id=projection.canonical_blob_id,
                metadata=metadata,
                id=projection.artifact_id,
            )
            await uow.production_artifacts.append(artifact)
            promoted.append(artifact)
        return SourceAdditionPromotionResult(
            production_run_id=run.id,
            request_id=request_id,
            artifacts=tuple(promoted),
            source_additions_hash=plan.source_additions_hash,
            already_promoted=False,
            promotion_plan=plan,
        )

    async def _archive_selected(
        self,
        addition: SourceAdditionV1,
        *,
        job_id: UUID | None,
        manual_content: bytes | None,
        declared_mime_type: str | None,
        final_url: str | None,
        research_date: date,
    ) -> tuple[SourceAdditionV1 | None, SourceAdditionRefusal | None]:
        async with self._uow_factory() as uow:
            run = await uow.production_runs.get(addition.production_run_id)
            if run is None:
                raise SourceAdditionConflictError("source_addition_run_missing")
            if addition.subject_id != run.subject_id:
                raise SourceAdditionConflictError("source_addition_subject_changed")
            current_references = await uow.production_artifacts.get_current(
                run.id, ProductionArtifactStage.REFERENCES.value
            )
            if current_references is not None:
                corpus = await self._read_references(current_references)
                existing = next(
                    (
                        source
                        for source in corpus.sources
                        if source.canonical_url == addition.canonical_url
                    ),
                    None,
                )
                if existing is not None and existing.eligible_for_extraction:
                    return None, SourceAdditionRefusal(
                        code=SourceAdditionRefusalCode.SOURCE_ALREADY_IN_REFERENCE_CORPUS,
                        recommended_restart_stage=ProductionStage.REFERENCES,
                        reason="Cette URL est déjà une source admissible du corpus courant.",
                    )

        try:
            collection = await self._collection_service.ensure_supplemental_source(
                addition.subject_id,
                SupplementalSource(
                    url=addition.canonical_url,
                    title=addition.title or "",
                    publisher=addition.publisher or "",
                    published_at=addition.published_at,
                    role=addition.role,
                ),
            )
        except CollectionNotAllowedError as exc:
            return None, SourceAdditionRefusal(
                code=SourceAdditionRefusalCode.ARCHIVE_NOT_ADMISSIBLE,
                recommended_restart_stage=ProductionStage.REFERENCES,
                reason="La source ne peut pas être rattachée au sujet courant.",
                details=(("diagnostic", type(exc).__name__),),
            )

        receipt = None
        if collection.state not in {
            CollectionState.ARCHIVED,
            CollectionState.EXTRACTED,
            CollectionState.COMPLETED,
        }:
            if addition.origin is SourceAdditionOrigin.MANUAL_ARCHIVE:
                if manual_content is None or not declared_mime_type:
                    return None, SourceAdditionRefusal(
                        code=SourceAdditionRefusalCode.MANUAL_ARCHIVE_BYTES_REQUIRED,
                        recommended_restart_stage=ProductionStage.REFERENCES,
                        reason=(
                            "L'origine manual_archive exige les octets exacts "
                            "fournis par l'operateur."
                        ),
                    )
                try:
                    manual_result = await self._collection_service.archive_manual_content(
                        collection.id,
                        content=manual_content,
                        declared_mime_type=declared_mime_type,
                        final_url=final_url or addition.canonical_url,
                        actor_id=addition.selected_by or "",
                        return_receipt=True,
                    )
                    if not isinstance(manual_result, tuple):
                        return None, SourceAdditionRefusal(
                            code=SourceAdditionRefusalCode.ARCHIVE_RECEIPT_INCOMPLETE,
                            recommended_restart_stage=ProductionStage.REFERENCES,
                            reason="L'archive manuelle n'a pas retourné son reçu durable.",
                        )
                    collection, receipt = manual_result
                except (
                    CollectionNotAllowedError,
                    ManualContentAlreadyArchivedError,
                    ManualContentEmptyError,
                    ManualContentTooLargeError,
                    ManualContentTypeError,
                ) as exc:
                    return None, SourceAdditionRefusal(
                        code=SourceAdditionRefusalCode.MANUAL_ARCHIVE_NOT_ALLOWED,
                        recommended_restart_stage=ProductionStage.REFERENCES,
                        reason=(
                            "L'archive manuelle n'a pas ete acceptee par la politique de collecte."
                        ),
                        details=(("diagnostic", type(exc).__name__),),
                    )
            else:
                if job_id is None:
                    raise SourceAdditionError("source_addition_collection_job_required")
                state = await self._collection_service.archive_one(collection.id, job_id)
                if state in {CollectionState.BLOCKED, CollectionState.FAILED_TERMINAL}:
                    async with self._uow_factory() as uow:
                        refreshed_collection = await uow.source_collections.get(collection.id)
                    if (
                        refreshed_collection is not None
                        and refreshed_collection.failure_reason_code
                        is CollectionFailureReason.ACCESS_BLOCKED
                    ):
                        collection = refreshed_collection
                        if manual_content is None or not declared_mime_type:
                            return None, SourceAdditionRefusal(
                                code=SourceAdditionRefusalCode.MANUAL_ARCHIVE_BYTES_REQUIRED,
                                recommended_restart_stage=ProductionStage.REFERENCES,
                                reason=(
                                    "La collecte est bloquee (403/robots); "
                                    "l'operateur doit fournir "
                                    "les octets exacts avant tout archivage manuel."
                                ),
                            )
                        try:
                            manual_result = await self._collection_service.archive_manual_content(
                                collection.id,
                                content=manual_content,
                                declared_mime_type=declared_mime_type,
                                final_url=final_url or addition.canonical_url,
                                actor_id=addition.selected_by or "",
                                return_receipt=True,
                            )
                            if not isinstance(manual_result, tuple):
                                return None, SourceAdditionRefusal(
                                    code=SourceAdditionRefusalCode.ARCHIVE_RECEIPT_INCOMPLETE,
                                    recommended_restart_stage=ProductionStage.REFERENCES,
                                    reason="L'archive manuelle n'a pas retourné son reçu durable.",
                                )
                            collection, receipt = manual_result
                        except (
                            CollectionNotAllowedError,
                            ManualContentAlreadyArchivedError,
                            ManualContentEmptyError,
                            ManualContentTooLargeError,
                            ManualContentTypeError,
                        ) as exc:
                            return None, SourceAdditionRefusal(
                                code=SourceAdditionRefusalCode.MANUAL_ARCHIVE_NOT_ALLOWED,
                                recommended_restart_stage=ProductionStage.REFERENCES,
                                reason=(
                                    "L'archive manuelle n'a pas ete acceptee par la politique "
                                    "de collecte."
                                ),
                                details=(("diagnostic", type(exc).__name__),),
                            )
                elif state not in {
                    CollectionState.ARCHIVED,
                    CollectionState.EXTRACTED,
                    CollectionState.COMPLETED,
                }:
                    return None, SourceAdditionRefusal(
                        code=SourceAdditionRefusalCode.ARCHIVE_NOT_ADMISSIBLE,
                        recommended_restart_stage=ProductionStage.REFERENCES,
                        reason="La collecte n'a produit aucune archive admissible.",
                        details=(("collection_state", state.value),),
                    )

        async with self._uow_factory() as uow:
            refreshed_collection = await uow.source_collections.get(collection.id)
            if refreshed_collection is None:
                return None, SourceAdditionRefusal(
                    code=SourceAdditionRefusalCode.ARCHIVE_RECEIPT_INCOMPLETE,
                    recommended_restart_stage=ProductionStage.REFERENCES,
                    reason="La collecte n'est plus disponible après archivage.",
                )
            document_id = refreshed_collection.source_document_id
            if document_id is None:
                return None, SourceAdditionRefusal(
                    code=SourceAdditionRefusalCode.ARCHIVE_RECEIPT_INCOMPLETE,
                    recommended_restart_stage=ProductionStage.REFERENCES,
                    reason="Le reçu de collecte ne référence aucun document archivé.",
                )
            collection = refreshed_collection
            document = await uow.source_documents.get(document_id)
            manual_attempt = (
                await self._manual_archive_attempt(uow, collection)
                if collection.origin_kind is SourceOriginKind.MANUAL
                else None
            )
        if document is None or document.subject_id != addition.subject_id:
            return None, SourceAdditionRefusal(
                code=SourceAdditionRefusalCode.ARCHIVE_RECEIPT_INCOMPLETE,
                recommended_restart_stage=ProductionStage.REFERENCES,
                reason="Le document archive n'appartient pas au sujet de l'addition.",
            )
        resolved_final_url = canonicalize_http_url(document.final_url or collection.canonical_url)
        if not _same_site(addition.canonical_url, resolved_final_url):
            return None, SourceAdditionRefusal(
                code=SourceAdditionRefusalCode.FINAL_URL_OUT_OF_SCOPE,
                recommended_restart_stage=ProductionStage.REFERENCES,
                reason="L'URL finale de l'archive sort du site de la source selectionnee.",
            )
        if document.published_at is not None and document.published_at > research_date:
            return None, SourceAdditionRefusal(
                code=SourceAdditionRefusalCode.SOURCE_DATE_AFTER_RUN,
                recommended_restart_stage=ProductionStage.REFERENCES,
                reason="La date du document archivé dépasse la date de recherche du run.",
                details=(("published_at", document.published_at.isoformat()),),
            )
        if not resolve_source_policy(document, collection).submission_allowed:
            return None, SourceAdditionRefusal(
                code=SourceAdditionRefusalCode.SOURCE_POLICY_BLOCKED,
                recommended_restart_stage=ProductionStage.EXTRACTION,
                reason="La politique d'acces interdit de soumettre cette source a l'extracteur.",
            )
        encoded_sha = receipt.encoded_sha256 if receipt is not None else document.encoded_sha256
        decoded_sha = receipt.decoded_sha256 if receipt is not None else document.decoded_sha256
        if not encoded_sha or not decoded_sha:
            return None, SourceAdditionRefusal(
                code=SourceAdditionRefusalCode.ARCHIVE_RECEIPT_INCOMPLETE,
                recommended_restart_stage=ProductionStage.REFERENCES,
                reason="L'archive ne fournit pas les deux empreintes SHA-256 requises.",
            )
        if collection.origin_kind is SourceOriginKind.MANUAL:
            if (
                manual_attempt is None
                or manual_attempt.encoded_sha256 != encoded_sha
                or manual_attempt.decoded_sha256 != decoded_sha
            ):
                return None, SourceAdditionRefusal(
                    code=SourceAdditionRefusalCode.ARCHIVE_RECEIPT_INCOMPLETE,
                    recommended_restart_stage=ProductionStage.REFERENCES,
                    reason="La tentative d'archive manuelle ne correspond pas au document archivé.",
                )
            policy_snapshot_id = manual_attempt.policy_snapshot_id
        else:
            policy_snapshot_id = getattr(collection, "fetch_policy_snapshot_id", None) or getattr(
                self._collection_service.policy_snapshot, "id", None
            )
        if not policy_snapshot_id:
            return None, SourceAdditionRefusal(
                code=SourceAdditionRefusalCode.ARCHIVE_RECEIPT_INCOMPLETE,
                recommended_restart_stage=ProductionStage.REFERENCES,
                reason="La version de la politique de collecte n'est pas archivee.",
            )
        archived = replace(
            addition,
            state=SourceAdditionState.ARCHIVED,
            final_url=resolved_final_url,
            title=document.title or addition.title,
            publisher=document.publisher or addition.publisher,
            published_at=document.published_at or addition.published_at,
            collection_id=collection.id,
            source_document_id=document.id,
            encoded_sha256=encoded_sha,
            decoded_sha256=decoded_sha,
            collection_policy_snapshot_id=str(policy_snapshot_id),
        )
        return archived, None

    async def _prepare_prospective(
        self,
        *,
        production_run_id: UUID,
        request_id: UUID,
        canonical_url: str,
        entry: SourceAdditionJournalEntryV1,
    ) -> SourceAdditionResult:
        async with self._uow_factory() as uow:
            context = await self._load_context(uow, production_run_id)
            entries = tuple(
                item
                for item in await uow.production_source_additions.list_latest_for_request(
                    production_run_id, request_id
                )
                if item.addition.state
                in {
                    SourceAdditionState.ARCHIVED,
                    SourceAdditionState.EXTRACTED,
                    SourceAdditionState.READY,
                }
            )
            if not entries:
                raise SourceAdditionError("source_addition_archive_missing")
            refreshed_corpus = await self._refresh_reference_corpus(
                uow,
                context.run,
                context.snapshot,
                context.references,
                tuple(item.addition for item in entries),
            )
            receipt_refusal = None
            for item in entries:
                refusal = await self._archived_receipt_refusal(
                    uow,
                    context.run,
                    refreshed_corpus,
                    item.addition,
                )
                if refusal is not None:
                    receipt_refusal = (item, refusal)
                    break
        if receipt_refusal is not None:
            failed_entry, refusal = receipt_refusal
            refused = await self._refuse(failed_entry, refusal)
            return SourceAdditionResult(addition=refused, refusal=refusal)
        if not refreshed_corpus.sources:
            raise SourceAdditionError("source_addition_projected_corpus_empty")

        addition_documents = frozenset(
            item.addition.source_document_id
            for item in entries
            if item.addition.source_document_id is not None
        )
        if not addition_documents:
            raise SourceAdditionError("source_addition_document_missing")
        try:
            plan = await self._extraction_service.plan_for_corpus(
                run=context.run,
                snapshot=context.snapshot,
                corpus=refreshed_corpus,
                source_document_ids=addition_documents,
            )
            execution = await self._extraction_service.execute(
                run=context.run,
                snapshot=context.snapshot,
                plan=plan,
            )
        except ProductionExtractionControlError as exc:
            refusal = SourceAdditionRefusal(
                code=SourceAdditionRefusalCode.SOURCE_EXTRACTION_FAILED,
                recommended_restart_stage=ProductionStage.EXTRACTION,
                reason="Le plan d'extraction de la source complementaire est invalide.",
                details=(("diagnostic", str(exc.code)),),
            )
            refused = await self._refuse(entry, refusal)
            return SourceAdditionResult(addition=refused, refusal=refusal)
        if (
            execution.status is not ExtractionExecutionStatus.SUCCEEDED
            or execution.extraction is None
        ):
            refusal = SourceAdditionRefusal(
                code=SourceAdditionRefusalCode.SOURCE_EXTRACTION_FAILED,
                recommended_restart_stage=ProductionStage.EXTRACTION,
                reason="L'extracteur n'a pas produit une extraction canonique pour l'addition.",
                details=(("diagnostic", execution.error_code or execution.status.value),),
            )
            refused = await self._refuse(entry, refusal)
            return SourceAdditionResult(
                addition=refused,
                refusal=refusal,
                model_calls=execution.model_calls,
            )
        if any(
            source.source_document_id not in addition_documents
            for source in execution.extraction.sources
        ) or {source.source_document_id for source in execution.extraction.sources} != set(
            addition_documents
        ):
            refusal = SourceAdditionRefusal(
                code=SourceAdditionRefusalCode.SOURCE_EXTRACTION_FAILED,
                recommended_restart_stage=ProductionStage.EXTRACTION,
                reason="Une source complementaire selectionnee a echoue a l'extraction.",
                details=(("diagnostic", "source_extraction_failed"),),
            )
            refused = await self._refuse(entry, refusal)
            return SourceAdditionResult(
                addition=refused,
                refusal=refusal,
                model_calls=execution.model_calls,
            )

        additions_by_document: dict[UUID, SourceAdditionV1] = {}
        extracted_sources = {
            source.source_document_id: source for source in execution.extraction.sources
        }
        for item in entries:
            addition = item.addition
            assert addition.source_document_id is not None
            extracted = extracted_sources[addition.source_document_id]
            handles = tuple(
                ref
                for ref in extraction_evidence_refs_v1(execution.extraction)
                if ref.source_document_id == addition.source_document_id
            )
            if not handles:
                refusal = SourceAdditionRefusal(
                    code=SourceAdditionRefusalCode.SOURCE_EXTRACTION_NO_EVIDENCE,
                    recommended_restart_stage=ProductionStage.EXTRACTION,
                    reason="L'extraction ne contient aucun handle d'evidence verifiable.",
                )
                refused = await self._refuse(item, refusal)
                return SourceAdditionResult(
                    addition=refused,
                    refusal=refusal,
                    model_calls=execution.model_calls,
                )
            checkpoint = await self._checkpoint_for_source(
                addition.canonical_url, extracted.checkpoint_id
            )
            if checkpoint is None or checkpoint.model_run_id is None:
                refusal = SourceAdditionRefusal(
                    code=SourceAdditionRefusalCode.SOURCE_EXTRACTION_LINEAGE_MISSING,
                    recommended_restart_stage=ProductionStage.EXTRACTION,
                    reason="Le checkpoint ou son ModelRun durable est absent.",
                )
                refused = await self._refuse(item, refusal)
                return SourceAdditionResult(
                    addition=refused,
                    refusal=refusal,
                    model_calls=execution.model_calls,
                )
            reference = next(
                source
                for source in refreshed_corpus.sources
                if source.canonical_url == addition.canonical_url
            )
            criticality, reason = production_source_criticality(
                reference, sources=refreshed_corpus.sources
            )
            additions_by_document[addition.source_document_id] = replace(
                addition,
                state=SourceAdditionState.EXTRACTED,
                criticality=criticality,
                criticality_reason=reason,
                criticality_policy_version=PRODUCTION_SOURCE_CRITICALITY_POLICY_VERSION,
                extraction_profile=extracted.profile,
                profile_reason_code=extracted.profile_reason_code,
                extraction_profile_policy_version=EXTRACTION_PROFILE_POLICY_VERSION,
                checkpoint_contract_version=checkpoint.contract_version,
                checkpoint_prompt_version=checkpoint.prompt_version,
                checkpoint_parser_version=checkpoint.parser_version,
                checkpoint_verifier_version=checkpoint.verifier_version,
                source_text_contract_version=checkpoint.source_text_contract_version,
                model_policy_version=checkpoint.model_policy_version,
                routing_policy_version=checkpoint.routing_policy_version,
                checkpoint_id=extracted.checkpoint_id,
                extraction_id=uuid5(
                    NAMESPACE_URL,
                    f"source-addition-checkpoint-result:{addition.source_document_id}:{extracted.checkpoint_id}",
                ),
                model_run_id=checkpoint.model_run_id,
                reuse_state=extracted.reuse_state,
                evidence_handles=handles,
            )

        new_extraction = _merge_additive_extraction(
            context.extraction,
            execution.extraction,
            refreshed_corpus,
        )
        projection = _rebase_relevance_projection(
            context.snapshot,
            context.projection,
            new_extraction,
        )
        extraction_payload_hash = _sha256(
            ProductionArtifactStore.canonical_json_bytes(
                production_extraction_to_json(new_extraction)
            )
        )
        projected_extraction_id = uuid5(
            NAMESPACE_URL,
            f"source-addition-projection-extraction:{production_run_id}:{request_id}:{extraction_payload_hash}",
        )
        additions_by_document = {
            document_id: replace(addition, extraction_id=projected_extraction_id)
            for document_id, addition in additions_by_document.items()
        }
        extracted_additions = tuple(additions_by_document.values())
        for addition in extracted_additions:
            source_entry = next(
                item for item in entries if item.addition.addition_id == addition.addition_id
            )
            if source_entry.addition.state is SourceAdditionState.READY:
                continue
            await self._save_transition(
                expected_version=next(
                    item.version
                    for item in entries
                    if item.addition.addition_id == addition.addition_id
                ),
                addition=addition,
                production_run_id=production_run_id,
                request_id=request_id,
                canonical_url=addition.canonical_url,
            )

        ready_additions = tuple(
            replace(addition, state=SourceAdditionState.READY) for addition in extracted_additions
        )
        manifest_hash = source_additions_hash(ready_additions)
        payloads: dict[ProductionArtifactStage, tuple[dict[str, Any], str]] = {
            ProductionArtifactStage.REFERENCES: (
                production_reference_corpus_to_json(refreshed_corpus),
                _sha256(
                    ProductionArtifactStore.canonical_json_bytes(
                        production_reference_corpus_to_json(refreshed_corpus)
                    )
                ),
            ),
            ProductionArtifactStage.EXTRACTION: (
                production_extraction_to_json(new_extraction),
                _sha256(
                    ProductionArtifactStore.canonical_json_bytes(
                        production_extraction_to_json(new_extraction)
                    )
                ),
            ),
            ProductionArtifactStage.RELEVANCE_PROJECTION: (
                relevance_projection_to_json(projection),
                _sha256(
                    ProductionArtifactStore.canonical_json_bytes(
                        relevance_projection_to_json(projection)
                    )
                ),
            ),
        }
        histories = await self._artifacts_for_run(production_run_id)
        versions = {
            stage: max(
                (item.version for item in histories if item.stage is stage),
                default=0,
            )
            + 1
            for stage in payloads
        }
        input_hashes = {
            ProductionArtifactStage.REFERENCES: _sha256(
                ProductionArtifactStore.canonical_json_bytes(
                    payloads[ProductionArtifactStage.REFERENCES][0]
                )
            ),
            ProductionArtifactStage.EXTRACTION: plan.input_hash,
            ProductionArtifactStage.RELEVANCE_PROJECTION: projection.input_hash,
        }
        stored: dict[ProductionArtifactStage, SourceAdditionArtifactProjectionV1] = {}
        for stage, (payload, payload_hash) in payloads.items():
            _, blob_id, _ = await self._artifact_store.store_stage_payloads(canonical=payload)
            if blob_id is None:
                raise SourceAdditionError("source_addition_prospective_blob_missing")
            stored[stage] = SourceAdditionArtifactProjectionV1(
                stage=stage,
                artifact_id=(
                    projected_extraction_id
                    if stage is ProductionArtifactStage.EXTRACTION
                    else uuid5(
                        NAMESPACE_URL,
                        f"source-addition-artifact:{production_run_id}:{request_id}:{stage.value}:{payload_hash}",
                    )
                ),
                version=versions[stage],
                input_hash=input_hashes[stage],
                canonical_blob_id=blob_id,
                canonical_sha256=payload_hash,
            )
        plan_projection = SourceAdditionPromotionPlanV1(
            production_run_id=production_run_id,
            subject_id=context.run.subject_id,
            pipeline_generation=context.run.pipeline_generation,
            base_artifacts=(
                _artifact_fence(context.references_artifact, context.references_bytes),
                _artifact_fence(context.extraction_artifact, context.extraction_bytes),
                _artifact_fence(context.projection_artifact, context.projection_bytes),
            ),
            projected_artifacts=tuple(stored.values()),
            source_additions_hash=manifest_hash,
        )
        for addition in ready_additions:
            current = next(
                item for item in extracted_additions if item.addition_id == addition.addition_id
            )
            previous = next(
                item for item in entries if item.addition.addition_id == current.addition_id
            )
            expected_ready_version = (
                previous.version
                if previous.addition.state is SourceAdditionState.READY
                else previous.version + 1
            )
            await self._save_transition(
                expected_version=expected_ready_version,
                addition=addition,
                promotion_plan=plan_projection,
                production_run_id=production_run_id,
                request_id=request_id,
                canonical_url=addition.canonical_url,
            )
        latest = await self._latest_entry(production_run_id, request_id, canonical_url)
        if latest is None:
            raise SourceAdditionError("source_addition_ready_record_missing")
        return SourceAdditionResult(
            addition=latest.addition,
            model_calls=execution.model_calls,
            reused_checkpoints=sum(
                source.reuse_state
                in {ExtractionReuseState.REUSED, ExtractionReuseState.CONTENT_DUPLICATE}
                for source in execution.extraction.sources
            )
            + len(
                {
                    source.checkpoint_id
                    for source in context.extraction.sources
                    if source.checkpoint_id is not None
                }
            ),
        )

    async def _refresh_reference_corpus(
        self,
        uow: Any,
        run: ProductionRun,
        snapshot: ProductionInputSnapshot,
        base_corpus: ProductionReferenceCorpusV1,
        additions: Sequence[SourceAdditionV1],
    ) -> ProductionReferenceCorpusV1:
        if run.research_date is None:
            raise SourceAdditionError("source_addition_run_date_missing")
        proposals = tuple(
            ProductionReferenceProposal(
                canonical_url=item.canonical_url,
                title=item.title,
                publisher=item.publisher,
                published_at=item.published_at,
                role=item.role,
                kind=item.kind,
                relevance_reason=(item.selection_criterion or "operator-selected additive source"),
                editorial_role=ProductionEditorialRole.CONTEXT,
                proposed_by_model=item.origin is SourceAdditionOrigin.DISCOVERY,
            )
            for item in additions
        )
        urls = tuple(
            {source.canonical_url for source in base_corpus.sources}
            | {item.canonical_url for item in additions}
        )
        observations = await observe_reference_collections(uow, run.subject_id, urls)
        return build_production_reference_corpus(
            subject_id=run.subject_id,
            research_date=run.research_date,
            production_input_hash=snapshot.input_hash,
            core_sources=snapshot.core_sources,
            proposals=proposals,
            observations=observations,
            warnings=base_corpus.warnings,
            previous=base_corpus,
        )

    async def _archived_receipt_refusal(
        self,
        uow: Any,
        run: ProductionRun,
        corpus: ProductionReferenceCorpusV1,
        addition: SourceAdditionV1,
    ) -> SourceAdditionRefusal | None:
        """Re-resolve the exact archive identity before resuming extraction.

        This is the crash-recovery fence: an archived journal row may resume
        from existing collection/document IDs, but a different receipt under
        the same URL is never silently adopted.
        """
        reference = next(
            (item for item in corpus.sources if item.canonical_url == addition.canonical_url),
            None,
        )
        collection = await uow.source_collections.get(addition.collection_id)
        document = await uow.source_documents.get(addition.source_document_id)
        manual_attempt = (
            await self._manual_archive_attempt(uow, collection)
            if collection is not None and collection.origin_kind is SourceOriginKind.MANUAL
            else None
        )
        policy_snapshot_matches = (
            manual_attempt is not None
            and manual_attempt.policy_snapshot_id == addition.collection_policy_snapshot_id
            and manual_attempt.encoded_sha256 == addition.encoded_sha256
            and manual_attempt.decoded_sha256 == addition.decoded_sha256
            if collection is not None and collection.origin_kind is SourceOriginKind.MANUAL
            else collection is not None
            and collection.fetch_policy_snapshot_id == addition.collection_policy_snapshot_id
        )
        mismatch = (
            reference is None
            or collection is None
            or document is None
            or reference.source_collection_id != addition.collection_id
            or reference.source_document_id != addition.source_document_id
            or reference.content_sha256 != addition.decoded_sha256
            or not reference.eligible_for_extraction
            or collection.subject_id != addition.subject_id
            or collection.canonical_url != addition.canonical_url
            or collection.source_document_id != addition.source_document_id
            or document.subject_id != addition.subject_id
            or document.source_collection_id != addition.collection_id
            or document.encoded_sha256 != addition.encoded_sha256
            or document.decoded_sha256 != addition.decoded_sha256
            or not policy_snapshot_matches
        )
        if mismatch:
            return SourceAdditionRefusal(
                code=SourceAdditionRefusalCode.ARCHIVE_RECEIPT_INCOMPLETE,
                recommended_restart_stage=ProductionStage.REFERENCES,
                reason="Le reçu repris ne correspond plus aux identités et empreintes archivées.",
            )
        assert collection is not None and document is not None
        final_url = canonicalize_http_url(document.final_url or addition.final_url or "")
        if final_url != addition.final_url or not _same_site(addition.canonical_url, final_url):
            return SourceAdditionRefusal(
                code=SourceAdditionRefusalCode.FINAL_URL_OUT_OF_SCOPE,
                recommended_restart_stage=ProductionStage.REFERENCES,
                reason="L'URL finale reprise sort du scope de la source sélectionnée.",
            )
        if document.published_at is not None and document.published_at > run.research_date:
            return SourceAdditionRefusal(
                code=SourceAdditionRefusalCode.SOURCE_DATE_AFTER_RUN,
                recommended_restart_stage=ProductionStage.REFERENCES,
                reason="La date du document archivé dépasse la date de recherche du run.",
            )
        if not resolve_source_policy(document, collection).submission_allowed:
            return SourceAdditionRefusal(
                code=SourceAdditionRefusalCode.SOURCE_POLICY_BLOCKED,
                recommended_restart_stage=ProductionStage.EXTRACTION,
                reason="La politique d'accès interdit de soumettre cette archive à l'extracteur.",
            )
        return None

    @staticmethod
    async def _manual_archive_attempt(uow: Any, collection: Any) -> Any | None:
        """Return the exact persisted manual attempt for an archived collection."""
        attempt_id = getattr(collection, "latest_attempt_id", None)
        if attempt_id is None:
            return None
        attempts = await uow.collection_attempts.list_for_collection(collection.id)
        return next(
            (
                attempt
                for attempt in attempts
                if attempt.id == attempt_id
                and attempt.collection_id == collection.id
                and attempt.manual_lease_id is not None
                and attempt.job_id is None
            ),
            None,
        )

    async def _critical_source_blockers(
        self,
        uow: Any,
        run: ProductionRun,
        references_artifact: ProductionArtifact,
        snapshot: ProductionInputSnapshot | None,
        corpus: ProductionReferenceCorpusV1,
        decisions: Sequence[Any],
    ) -> tuple[SourceAdditionBlocker, ...]:
        if snapshot is None:
            return (
                SourceAdditionBlocker(
                    source_url="",
                    repair_key="",
                    criticality=ProductionSourceCriticality.CRITICAL,
                    criticality_reason=ProductionSourceCriticalityReason.COVERAGE_UNKNOWN,
                    failure_code="production_snapshot_missing",
                    reason="Le snapshot de production du run est introuvable.",
                ),
            )
        refreshed = await self._refresh_reference_corpus(uow, run, snapshot, corpus, ())
        blockers: list[SourceAdditionBlocker] = []
        for source in refreshed.sources:
            criticality, reason = production_source_criticality(source, sources=refreshed.sources)
            if (
                source.eligible_for_extraction
                or criticality is not ProductionSourceCriticality.CRITICAL
            ):
                continue
            repair_key = repair_key_for_supplemental_source(
                edition_id=run.edition_id,
                subject_id=run.subject_id,
                source_url=source.canonical_url,
            )
            decision = next(
                (
                    item
                    for item in decisions
                    if item.subject_id == run.subject_id and item.repair_key == repair_key
                ),
                None,
            )
            current_decision = _decision_for_current_source_issue(
                decision,
                run,
                references_artifact,
                criticality_policy_version=PRODUCTION_SOURCE_CRITICALITY_POLICY_VERSION,
            )
            if (
                current_decision is not None
                and ProductionRepairAction(current_decision.action)
                is ProductionRepairAction.CONTINUE_WITHOUT_SOURCE
                and bool((current_decision.reason or "").strip())
            ):
                continue
            collection = await uow.source_collections.get_by_canonical_url(
                run.subject_id, source.canonical_url
            )
            failure_code = (
                collection.failure_reason_code.value
                if collection is not None and collection.failure_reason_code is not None
                else None
            )
            blockers.append(
                SourceAdditionBlocker(
                    source_url=source.canonical_url,
                    repair_key=repair_key,
                    criticality=criticality,
                    criticality_reason=reason,
                    failure_code=failure_code,
                    reason=(
                        "Une source critique du run n'est pas archivee et ne possede pas de "
                        "décision continue_without_source raisonnée pour cette génération."
                    ),
                )
            )
        return tuple(sorted(blockers, key=lambda item: item.source_url))

    async def _effective_repair_decisions(self, uow: Any, run: ProductionRun) -> tuple[Any, ...]:
        repository = getattr(uow, "production_repair_decisions", None)
        if repository is None:
            return ()
        getter = getattr(repository, "effective_decisions", None)
        if callable(getter):
            return tuple(await getter(run.edition_id, run.subject_id))
        return tuple(await repository.list_for_edition(run.edition_id, run.subject_id))

    async def _load_context(self, uow: Any, production_run_id: UUID) -> _AdditionContext:
        run = await uow.production_runs.get(production_run_id)
        if run is None:
            raise SourceAdditionError("production_run_not_found")
        self._require_revisionable_run(run)
        current_run = await uow.production_runs.get_current_for_subject(run.subject_id)
        if current_run is None or current_run.id != run.id:
            raise SourceAdditionConflictError("source_addition_run_not_current")
        snapshot = await uow.production_input_snapshots.get_by_run(run.id)
        references = await uow.production_artifacts.get_current(
            run.id, ProductionArtifactStage.REFERENCES.value
        )
        extraction_artifact = await uow.production_artifacts.get_current(
            run.id, ProductionArtifactStage.EXTRACTION.value
        )
        projection_artifact = await uow.production_artifacts.get_current(
            run.id, ProductionArtifactStage.RELEVANCE_PROJECTION.value
        )
        if (
            snapshot is None
            or references is None
            or extraction_artifact is None
            or projection_artifact is None
        ):
            raise SourceAdditionError("source_addition_base_artifacts_missing")
        artifacts = (references, extraction_artifact, projection_artifact)
        if any(
            artifact.status is not ProductionArtifactStatus.VERIFIED
            or artifact.canonical_blob_id is None
            for artifact in artifacts
        ):
            raise SourceAdditionError("source_addition_base_artifacts_invalid")
        references_bytes = await self._artifact_store.read_bytes(references.canonical_blob_id)
        extraction_bytes = await self._artifact_store.read_bytes(
            extraction_artifact.canonical_blob_id
        )
        projection_bytes = await self._artifact_store.read_bytes(
            projection_artifact.canonical_blob_id
        )
        reference_corpus = production_reference_corpus_from_json(json.loads(references_bytes))
        extraction = production_extraction_from_json(json.loads(extraction_bytes))
        projection = relevance_projection_from_json(json.loads(projection_bytes))
        if (
            reference_corpus.subject_id != run.subject_id
            or extraction.subject_id != run.subject_id
            or projection.subject_id != run.subject_id
            or extraction.references_corpus_hash
            != _sha256(
                ProductionArtifactStore.canonical_json_bytes(
                    production_reference_corpus_to_json(reference_corpus)
                )
            )
            or projection.extraction_hash != canonical_extraction_hash(extraction)
        ):
            raise SourceAdditionError("source_addition_base_artifact_lineage_invalid")
        validate_relevance_projection_lineage(
            projection,
            extraction,
            extraction_hash=canonical_extraction_hash(extraction),
        )
        return _AdditionContext(
            run=run,
            snapshot=snapshot,
            references_artifact=references,
            extraction_artifact=extraction_artifact,
            projection_artifact=projection_artifact,
            references=reference_corpus,
            extraction=extraction,
            projection=projection,
            references_bytes=references_bytes,
            extraction_bytes=extraction_bytes,
            projection_bytes=projection_bytes,
        )

    async def _read_references(self, artifact: ProductionArtifact) -> ProductionReferenceCorpusV1:
        if artifact.canonical_blob_id is None:
            raise SourceAdditionError("source_addition_references_blob_missing")
        return production_reference_corpus_from_json(
            await self._artifact_store.read_json(artifact.canonical_blob_id)
        )

    async def _checkpoint_for_source(
        self, canonical_url: str, checkpoint_id: UUID | None
    ) -> Any | None:
        if checkpoint_id is None:
            return None
        async with self._uow_factory() as uow:
            checkpoints = await uow.source_extractions.list_for_url(canonical_url)
        return next(
            (
                item
                for item in checkpoints
                if item.id == checkpoint_id
                and item.status is SourceExtractionStatus.VERIFIED
                and item.canonical_blob_id is not None
            ),
            None,
        )

    async def _artifacts_for_run(self, run_id: UUID) -> Sequence[ProductionArtifact]:
        async with self._uow_factory() as uow:
            return await uow.production_artifacts.list_for_run(run_id)

    async def _latest_entry(
        self, production_run_id: UUID, request_id: UUID, canonical_url: str
    ) -> SourceAdditionJournalEntryV1 | None:
        async with self._uow_factory() as uow:
            return await uow.production_source_additions.get_latest_for_identity(
                production_run_id,
                request_id,
                source_addition_source_key(canonical_url),
            )

    async def _load_run(self, production_run_id: UUID) -> ProductionRun:
        async with self._uow_factory() as uow:
            run = await uow.production_runs.get(production_run_id)
        if run is None:
            raise SourceAdditionError("production_run_not_found")
        self._require_revisionable_run(run)
        return run

    async def _save_transition(
        self,
        *,
        expected_version: int,
        addition: SourceAdditionV1,
        production_run_id: UUID,
        request_id: UUID,
        canonical_url: str,
        promotion_plan: SourceAdditionPromotionPlanV1 | None = None,
    ) -> SourceAdditionJournalEntryV1:
        async with self._uow_factory() as uow:
            run = await uow.production_runs.get_for_update(production_run_id)
            if run is None or run.subject_id != addition.subject_id:
                raise SourceAdditionConflictError("source_addition_run_changed")
            latest = await uow.production_source_additions.get_latest_for_identity(
                production_run_id,
                request_id,
                source_addition_source_key(canonical_url),
            )
            if latest is None or latest.version != expected_version:
                raise SourceAdditionConflictError("source_addition_transition_fence_changed")
            if latest.addition.addition_id != addition.addition_id:
                raise SourceAdditionConflictError("source_addition_identity_changed")
            next_entry = SourceAdditionJournalEntryV1(
                addition=addition,
                version=latest.version + 1,
                promotion_plan=promotion_plan,
            )
            await uow.production_source_additions.append(next_entry)
            await uow.commit()
            return next_entry

    async def _refuse(
        self,
        entry: SourceAdditionJournalEntryV1,
        refusal: SourceAdditionRefusal,
    ) -> SourceAdditionV1:
        if entry.addition.state is SourceAdditionState.REFUSED:
            return entry.addition
        diagnostic_details = {
            **dict(refusal.details),
            "reason": refusal.reason,
        }
        if refusal.recommended_restart_stage is not None:
            diagnostic_details["recommended_restart_stage"] = (
                refusal.recommended_restart_stage.value
            )
        refused = replace(
            entry.addition,
            state=SourceAdditionState.REFUSED,
            diagnostic_code=refusal.code.value,
            diagnostic_details=tuple(sorted(diagnostic_details.items())),
        )
        await self._save_transition(
            expected_version=entry.version,
            addition=refused,
            production_run_id=entry.addition.production_run_id,
            request_id=entry.addition.request_id,
            canonical_url=entry.addition.canonical_url,
        )
        return refused

    @staticmethod
    def _refusal_from_addition(addition: SourceAdditionV1) -> SourceAdditionRefusal:
        try:
            code = SourceAdditionRefusalCode(addition.diagnostic_code or "")
        except ValueError:
            code = SourceAdditionRefusalCode.SOURCE_EXTRACTION_FAILED
        return SourceAdditionRefusal(
            code=code,
            recommended_restart_stage=(
                ProductionStage.EXTRACTION
                if code
                in {
                    SourceAdditionRefusalCode.SOURCE_EXTRACTION_FAILED,
                    SourceAdditionRefusalCode.SOURCE_EXTRACTION_NO_EVIDENCE,
                    SourceAdditionRefusalCode.SOURCE_EXTRACTION_LINEAGE_MISSING,
                }
                else ProductionStage.REFERENCES
            ),
            reason="Cette addition a été refusée avec un diagnostic durable.",
            details=addition.diagnostic_details,
        )

    @staticmethod
    def _projected_artifact_metadata(
        stage: ProductionArtifactStage,
        payload: Mapping[str, Any],
        *,
        manifest: list[dict[str, Any]],
        manifest_hash: str,
        request_id: UUID,
    ) -> dict[str, Any]:
        metadata: dict[str, Any] = {
            "source_additions": manifest,
            "source_additions_hash": manifest_hash,
            "source_addition_request_id": str(request_id),
        }
        if stage is ProductionArtifactStage.REFERENCES:
            metadata.update(
                production_reference_corpus_metadata(production_reference_corpus_from_json(payload))
            )
        elif stage is ProductionArtifactStage.EXTRACTION:
            extraction = production_extraction_from_json(dict(payload))
            metadata.update(production_extraction_metadata(extraction))
            metadata["extraction_hash"] = canonical_extraction_hash(extraction)
        else:
            projection = relevance_projection_from_json(dict(payload))
            metadata.update(
                {
                    "schema_version": projection.schema_version,
                    "policy_version": projection.policy_version,
                    "classifier_version": projection.classifier_version,
                    "projection_hash": projection.projection_hash,
                    "extraction_hash": projection.extraction_hash,
                    "classification_count": len(projection.classifications),
                }
            )
        return metadata

    @staticmethod
    def _require_revisionable_run(run: ProductionRun) -> None:
        if run.status not in {ProductionRunStatus.READY, ProductionRunStatus.NEEDS_REVIEW}:
            raise SourceAdditionError("source_addition_run_not_revisionable")

    async def _blob_sha256(self, blob_id: UUID) -> str:
        return hashlib.sha256(await self._artifact_store.read_bytes(blob_id)).hexdigest()


def _merge_additive_extraction(
    previous: ProductionExtractionV1,
    additions: ProductionExtractionV1,
    corpus: ProductionReferenceCorpusV1,
) -> ProductionExtractionV1:
    """Rebase old source outputs byte-for-byte and append only new outputs."""
    previous_by_url = {source.canonical_url: source for source in previous.sources}
    addition_sources = tuple(
        source for source in additions.sources if source.canonical_url not in previous_by_url
    )
    if len(addition_sources) != len(additions.sources):
        raise SourceAdditionConflictError("source_addition_extraction_duplicates_existing_source")
    sources = (*previous.sources, *addition_sources)
    order = {source.canonical_url: index for index, source in enumerate(corpus.sources)}
    sources = tuple(
        sorted(
            sources,
            key=lambda source: (order.get(source.canonical_url, 10**9), source.canonical_url),
        )
    )
    added_urls = {source.canonical_url for source in addition_sources}
    omitted_by_url = {
        item.canonical_url: item
        for item in previous.omitted_sources
        if item.canonical_url not in added_urls
    }
    omitted_by_url.update({item.canonical_url: item for item in additions.omitted_sources})
    return replace(
        previous,
        references_corpus_hash=additions.references_corpus_hash,
        sources=sources,
        omitted_sources=tuple(sorted(omitted_by_url.values(), key=lambda item: item.canonical_url)),
        warnings=tuple(dict.fromkeys((*previous.warnings, *additions.warnings))),
    )


def _rebase_relevance_projection(
    snapshot: ProductionInputSnapshot,
    previous: RelevanceProjectionV1,
    extraction: ProductionExtractionV1,
) -> RelevanceProjectionV1:
    """Keep prior decisions exact and assign CONTEXT to every new evidence ref."""
    old_refs = set(previous.extraction_evidence_refs)
    refs = extraction_evidence_refs_v1(extraction)
    new_refs = set(refs) - old_refs
    prior_classifications = {item.evidence_ref: item for item in previous.classifications}
    if set(prior_classifications) != old_refs:
        raise SourceAdditionConflictError("source_addition_projection_base_incomplete")
    additions = tuple(
        RelevanceClassificationItemV1(
            evidence_ref=ref,
            classification=RelevanceClassification.CONTEXT,
            reason_code=RelevanceReasonCode.CONTEXT_SOURCE_WITHOUT_RELATION,
            supporting_evidence_refs=(ref,),
            provenance=RelevanceDecisionProvenance.DETERMINISTIC_POLICY,
        )
        for ref in sorted(
            new_refs,
            key=lambda item: (str(item.source_document_id), item.kind.value, item.evidence_key),
        )
    )
    if set(refs) - new_refs != old_refs:
        raise SourceAdditionConflictError("source_addition_projection_old_evidence_changed")
    result = RelevanceProjectionV1(
        subject_id=snapshot.subject_id,
        production_input_hash=snapshot.input_hash,
        extraction_hash=canonical_extraction_hash(extraction),
        input_hash=relevance_projection_input_hash(
            snapshot,
            extraction,
            classifier_version=previous.classifier_version,
        ),
        extraction_evidence_refs=refs,
        classifications=(*previous.classifications, *additions),
        source_pair_relations=previous.source_pair_relations,
        model_proposal_rejections=previous.model_proposal_rejections,
        policy_version=RELEVANCE_PROJECTION_POLICY_VERSION,
        classifier_version=previous.classifier_version or DEFAULT_RELEVANCE_CLASSIFIER_VERSION,
    )
    for ref, classification in prior_classifications.items():
        if result.classification_for(ref) != classification:
            raise SourceAdditionConflictError("source_addition_projection_old_decision_changed")
    validate_relevance_projection_lineage(
        result,
        extraction,
        extraction_hash=canonical_extraction_hash(extraction),
    )
    return result


def _artifact_fence(
    artifact: ProductionArtifact,
    payload: bytes,
) -> SourceAdditionArtifactFenceV1:
    return SourceAdditionArtifactFenceV1(
        stage=artifact.stage,
        artifact_id=artifact.id,
        version=artifact.version,
        input_hash=artifact.input_hash,
        canonical_sha256=hashlib.sha256(payload).hexdigest(),
    )


def _same_site(first: str, second: str) -> bool:
    first_host = (urlsplit(first).hostname or "").lower().removeprefix("www.")
    second_host = (urlsplit(second).hostname or "").lower().removeprefix("www.")
    return bool(first_host) and first_host == second_host


def _clean_optional(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = " ".join(value.replace("\x00", "").split())
    return normalized or None


def _required_text(value: str, *, label: str, limit: int) -> str:
    normalized = _clean_optional(value)
    if normalized is None or len(normalized) > limit:
        raise ValueError(f"{label}_invalid")
    return normalized


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()
