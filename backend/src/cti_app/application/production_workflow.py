"""Main production workflow orchestration service."""

from __future__ import annotations

import asyncio
import hashlib
import re
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Any, cast
from uuid import NAMESPACE_URL, UUID, uuid5

from cti_app.application.analyst_vt_enrichment import VirusTotalSeedEnrichmentService
from cti_app.application.collection import SupplementalSource
from cti_app.application.diagnostics import DiagnosticsLog
from cti_app.application.jobs import JobCancelledError, JobExecutionContext
from cti_app.application.model_conversations import (
    ConversationTurnFailedError,
    ModelConversationService,
    conversation_close_failure_fields,
)
from cti_app.application.model_gateway import (
    ModelGateway,
    ModelGatewayError,
    ModelRequest,
    ModelRoutingHint,
)
from cti_app.application.persistence import UnitOfWork, UnitOfWorkFactory
from cti_app.application.production_artifact_reuse import (
    ProductionArtifactReuseService,
    cross_run_reuse_allowed,
)
from cti_app.application.production_artifact_store import (
    ProductionArtifactStore,
    ProductionReuseStorageUnavailableError,
)
from cti_app.application.production_context import build_subject_production_context
from cti_app.application.production_extraction import (
    PRODUCTION_EXTRACTION_SERVICE_VERSION,
    ExtractionPlan,
    ExtractionRejection,
    ProductionExtractionControlError,
    ProductionExtractionService,
    legacy_technical_extraction_from_payload,
    production_extraction_metadata,
    source_text_contract_version,
)
from cti_app.application.production_pacing import ProductionPacingPolicy
from cti_app.application.production_parsers import (
    ParseResult,
    ReferenceReport,
    validate_synthesis,
)
from cti_app.application.production_prompts import (
    REFERENCES_PROMPT_VERSION,
    SYNTHESIS_FORMAT_REPAIR_VERSION,
    SYNTHESIS_PROMPT_VERSION,
    ProductionPromptTemplates,
)
from cti_app.application.production_recovery import ProductionRecoveryPolicyV1
from cti_app.application.production_references import (
    PRODUCTION_REFERENCE_CORPUS_SCHEMA_VERSION,
    PRODUCTION_REFERENCE_PARSER_VERSION,
    ProductionReferenceProposal,
    build_production_reference_corpus,
    has_usable_core_source,
    load_reference_projection,
    observe_reference_collections,
    parse_production_reference_proposals,
    production_reference_corpus_from_json,
    production_reference_corpus_metadata,
    report_source_labels,
)
from cti_app.application.production_repair_payloads import ProductionRepairPayloadResolver
from cti_app.application.production_repairs import (
    SYNTHESIS_EVIDENCE_PACK_VERSION,
    build_repair_evidence_pack,
    reconcile_effective_repairs_in_uow,
    repair_key_for_rejection,
    synthesis_projection_hash,
    synthesis_projection_payload,
)
from cti_app.application.production_resume import EXTRACTION_PROGRESS_COMPLETED_STATUSES
from cti_app.application.production_stages import (
    ExtractionService,
    ProductionQAService,
    PublicationAssemblyService,
    ReferenceResearchService,
    SynthesisService,
    compute_input_hash,
)
from cti_app.application.production_synthesis import (
    ProductionSynthesisExecution,
    ProductionSynthesisService,
    SynthesisExecutionStatus,
)
from cti_app.application.production_synthesis_revision import (
    MAX_SYNTHESIS_REVISION_CONTEXT_BYTES,
    MAX_SYNTHESIS_REVISION_TEXT_BYTES,
    SYNTHESIS_REVISION_PROMPT_VERSION,
    SynthesisRevisionContext,
    build_synthesis_revision_context,
    narrative_repair_keys,
    revision_context_size_bytes,
    synthesis_content_hash,
    synthesis_semantic_source_ids,
)
from cti_app.config import get_settings
from cti_app.domain.collection import CollectionState
from cti_app.domain.model_conversations import (
    ConversationMode,
    ConversationPolicy,
    ConversationPurpose,
    ConversationTransport,
    ModelConversation,
)
from cti_app.domain.model_runs import ModelProvider
from cti_app.domain.production import (
    DetectionRuleType,
    ExtractionProfile,
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionArtifactStatus,
    ProductionInputSnapshot,
    ProductionRepairIssueKind,
    ProductionRun,
    ProductionRunStatus,
    ProductionStage,
    SynthesisMode,
)
from cti_app.domain.production_extraction import (
    ExtractionIndicatorStatus,
    ExtractionReuseState,
    ProductionExtractionOmissionReason,
    ProductionExtractionV1,
)
from cti_app.domain.production_references import ProductionReferenceCorpusV1
from cti_app.domain.production_synthesis import production_synthesis_from_json
from cti_app.domain.publication import is_publication_ioc_artifact_type

if TYPE_CHECKING:
    from cti_app.application.collection import SubjectCollectionService


# Collection states that count as "the source is available for analysis".
_ARCHIVED_STATES = {"archived", "extracted", "completed"}

# REFERENCES names only the role and routing hint; the ModelRouter owns the
# backend choice, so this version carries no provider.
REFERENCES_ROUTING_POLICY_VERSION = "model-router-web-research-v1"
REFERENCES_PROMPT_TEMPLATE_ID = "production-references"

# Une fermeture d'onglet qui tombe pendant une éviction du service worker MV3
# réussit quelques secondes plus tard.
_CONVERSATION_CLOSE_RETRY_DELAY_SECONDS = 5.0

# Keep archive reads within the same decoded-document limit as collection and
# deterministic source processing. This is a local proof read, never prompt
# material.
MAX_ARCHIVED_SOURCE_BYTES = 25 * 1024 * 1024

# Bridge and network hiccups are worth retrying; anything else is a dead end
# for this attempt and must not silently burn the subject.
_TRANSIENT_CODES = {
    "bridge_server_error",
    "bridge_idle_timeout",
    "bridge_total_timeout",
    "bridge_timeout",
    "bridge_ui_timeout",
    "bridge_extension_disconnected",
    "bridge_unreachable",
    "bridge_rate_limited",
}

# The conversation itself is the problem, not the pipeline: retrying the same
# turn cannot help, but nothing is broken either.
_REVIEW_CODES = {
    "conversation_unavailable",
    "conversation_profile_mismatch",
    "conversation_busy",
    "external_llm_blocked",
}

_MODEL_SUBMISSION_RECONCILIATION_CODE = "model_submission_reconciliation_required"


def _transient_or_terminal(stage: str, exc: Exception) -> dict[str, Any]:
    code = str(getattr(exc, "code", "") or "")
    retryable = bool(getattr(exc, "retryable", False))
    if isinstance(exc, ConversationTurnFailedError) and exc.status.value == "needs_review":
        status = "needs_review"
    elif code == _MODEL_SUBMISSION_RECONCILIATION_CODE or code in _REVIEW_CODES:
        # The conversation is gone or busy: an operator has to look, but the
        # subject is not corrupted and the batch must keep moving.
        status = "needs_review"
    elif retryable or code in _TRANSIENT_CODES:
        status = "transient_error"
    else:
        status = "terminal_error"
    result = {
        "stage": stage,
        "status": status,
        "error_code": code or f"{stage}_failed",
        "error": str(exc),
        "details": getattr(exc, "details", None),
    }
    model_run_id = getattr(exc, "model_run_id", None)
    if isinstance(model_run_id, UUID):
        result["model_run_id"] = str(model_run_id)
    return result


def _supplemental_failure_fields(
    exc: Exception,
    *,
    canonical_url: str | None = None,
    collection_state: str | None = None,
    retry_attempted: bool = False,
) -> dict[str, Any]:
    """Keep structured collection failure context without exposing raw values."""
    raw_details = getattr(exc, "details", None)
    details = dict(raw_details) if isinstance(raw_details, dict) else {}
    fields: dict[str, Any] = {}
    if canonical_url is not None:
        fields["canonical_url"] = canonical_url
    code = getattr(exc, "code", None)
    if isinstance(code, str) and code:
        fields["error_code"] = code
    if collection_state is not None:
        fields["collection_state"] = collection_state
    retryable = getattr(exc, "retryable", None)
    if retryable is None:
        retryable = getattr(exc, "transient", None)
    if "failed_retryable" in details:
        retryable = bool(details["failed_retryable"])
    if isinstance(retryable, bool):
        fields["retryable"] = retryable
    if retry_attempted:
        fields["retry_attempted"] = True
    for key in ("failed_retryable", "blocked", "unavailable", "failed_terminal"):
        if key in details:
            fields[key] = details[key]
    if details:
        fields["details"] = details
    fields["error_type"] = type(exc).__name__
    message = str(exc).strip()
    if message and message != fields.get("error_code"):
        fields["error_message"] = message[:1000]
    return fields


def _supplemental_failure_warning(
    exc: Exception,
    *,
    canonical_url: str | None = None,
) -> str:
    """Render a compact UI warning; full fields stay in structured diagnostics."""
    raw_details = getattr(exc, "details", None)
    details = raw_details if isinstance(raw_details, dict) else {}
    parts = ["supplemental_collection_failed"]
    if canonical_url is not None:
        parts.append(f"url={canonical_url}")
    code = getattr(exc, "code", None)
    if isinstance(code, str) and code:
        parts.append(f"code={code}")
    else:
        parts.append(f"type={type(exc).__name__}")
    retryable = getattr(exc, "retryable", None)
    if retryable is None:
        retryable = getattr(exc, "transient", None)
    if isinstance(retryable, bool) and not any(
        key in details for key in ("failed_retryable", "blocked", "unavailable", "failed_terminal")
    ):
        parts.append(f"retryable={int(retryable)}")
    for key in ("failed_retryable", "blocked", "unavailable", "failed_terminal"):
        if key in details:
            parts.append(f"{key}={details[key]}")
    return ":".join(parts)


def _repair_problem_descriptions(result: Any) -> list[str]:
    """Describe the parse failures precisely enough for a repair turn.

    Bare error codes collapse several distinct violations into the same line
    and hide which value must be rewritten. When the parse result carries
    violations, expose ``code: detail``.
    """
    violations = getattr(result, "violations", None) or ()
    described: list[str] = []
    for violation in violations:
        code = getattr(violation, "code", "")
        if not code:
            continue
        detail = " ".join((getattr(violation, "detail", "") or "").split())
        if len(detail) > 200:
            detail = f"{detail[:200]}…"
        described.append(f"{code}: {detail}" if detail else code)
    if described:
        # Chaque occurrence distincte doit rester visible : deux violations du
        # même code portent sur deux valeurs différentes à réécrire.
        return list(dict.fromkeys(described))[:12]
    return list(getattr(result, "errors", ()) or ())


def _canonical_extraction_progress(
    plan: ExtractionPlan,
    *,
    extraction: ProductionExtractionV1 | None = None,
    model_calls: int = 0,
    failed_source_id: str | None = None,
) -> dict[str, Any]:
    """Publish the canonical per-source verdict the desk and resume planner read.

    The corpus is the authority on which sources the stage owned: every planned
    source and every omitted source stays visible, with the status the
    canonical execution recorded for it.
    """

    produced = (
        {source.source_document_id: source for source in extraction.sources}
        if extraction is not None
        else {}
    )
    failed_urls = {
        omission.canonical_url
        for omission in (extraction.omitted_sources if extraction is not None else ())
        if omission.reason is ProductionExtractionOmissionReason.SOURCE_EXTRACTION_FAILED
    }
    sources: list[dict[str, Any]] = []
    for planned in plan.sources:
        source = produced.get(planned.source_document_id)
        if source is not None:
            status = "succeeded" if source.reuse_state is ExtractionReuseState.FRESH else "cached"
        elif (
            planned.canonical_url in failed_urls
            or str(planned.source_document_id) == failed_source_id
        ):
            status = "failed"
        else:
            status = "pending"
        sources.append(
            {
                "source_id": str(planned.source_document_id),
                "title": planned.title,
                "canonical_url": planned.canonical_url,
                "tier": planned.tier.value,
                "profile": planned.profile.value,
                "status": status,
                "reuse_state": source.reuse_state.value if source is not None else None,
                "ioc_count": len(source.indicators) if source is not None else 0,
                "rule_count": len(source.rules) if source is not None else 0,
            }
        )
    for omitted in plan.omitted_sources:
        sources.append(
            {
                "source_id": omitted.canonical_url,
                "title": None,
                "canonical_url": omitted.canonical_url,
                "tier": omitted.tier.value,
                "profile": None,
                "status": "omitted",
                "reuse_state": None,
                "ioc_count": 0,
                "rule_count": 0,
            }
        )
    rules = [rule for source in produced.values() for rule in source.rules]
    indicators = [item for source in produced.values() for item in source.indicators]
    completed = [
        entry for entry in sources if entry["status"] in EXTRACTION_PROGRESS_COMPLETED_STATUSES
    ]

    def profile_count(entries: list[dict[str, Any]], profile: ExtractionProfile) -> int:
        return sum(entry["profile"] == profile.value for entry in entries)

    return {
        "total_sources": len(sources),
        "completed_sources": len(completed),
        "full_total": profile_count(sources, ExtractionProfile.FULL),
        "full_completed": profile_count(completed, ExtractionProfile.FULL),
        "ioc_rules_total": profile_count(sources, ExtractionProfile.IOC_RULES),
        "ioc_rules_completed": profile_count(completed, ExtractionProfile.IOC_RULES),
        "cache_hits": sum(entry["status"] == "cached" for entry in sources),
        "model_calls": model_calls,
        "skipped_sources": sum(entry["status"] in {"omitted", "failed"} for entry in sources),
        "confirmed_iocs": sum(
            item.indicator_status is ExtractionIndicatorStatus.CONFIRMED_IOC for item in indicators
        ),
        "contextual_iocs": sum(
            item.indicator_status is ExtractionIndicatorStatus.CONTEXTUAL for item in indicators
        ),
        "rules_total": len(rules),
        **{
            f"{rule_type.value}_rules": sum(rule.rule_type is rule_type for rule in rules)
            for rule_type in DetectionRuleType
        },
        "sources": sources,
        "profile_policy_version": plan.profile_policy_version,
        "references_corpus_hash": plan.references_corpus_hash,
    }


def _repair_evidence(
    run: ProductionRun, rejections: Sequence[ExtractionRejection]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Turn evidence-gate rejections into the inert Repair Desk evidence.

    Returns the complete repair entries (stored in the evidence pack blob) and
    the bounded diagnostics kept in the artifact metadata.
    """

    entries: list[dict[str, Any]] = []
    diagnostics: list[dict[str, Any]] = []
    for item in rejections:
        rejection = item.rejection
        value_sha256 = hashlib.sha256(rejection.value.encode("utf-8")).hexdigest()
        identity = {
            "source_id": str(item.source.source_document_id),
            "source_url": item.source.canonical_url,
            "batch_id": None,
            "model_run_id": str(item.model_run_id) if item.model_run_id is not None else None,
            "proposal_index": rejection.proposal_index,
            "proposal_kind": rejection.proposal_kind,
            "artifact_type": rejection.artifact_type,
            "reason_code": rejection.reason_code,
        }
        entries.append(
            {
                "repair_key": repair_key_for_rejection(
                    edition_id=run.edition_id,
                    subject_id=run.subject_id,
                    kind=(
                        ProductionRepairIssueKind.REJECTED_RULE
                        if rejection.proposal_kind == "rule"
                        else ProductionRepairIssueKind.REJECTED_INDICATOR
                    ),
                    source_url=item.source.canonical_url,
                    artifact_type=rejection.artifact_type,
                    value=rejection.value,
                ),
                "source_title": item.source.title,
                **identity,
                "value": rejection.value,
                "value_sha256": value_sha256,
            }
        )
        diagnostics.append({**identity, "value": rejection.value[:512], "value_hash": value_sha256})
    return entries, diagnostics


@dataclass(frozen=True, slots=True)
class _ReferenceCollectionOutcome:
    """What the supplementary collection pass added or failed to add."""

    new_sources: int = 0
    warnings: tuple[str, ...] = ()
    failures: tuple[dict[str, Any], ...] = ()


def _reference_corpus_result(
    *,
    corpus: ProductionReferenceCorpusV1,
    artifact: ProductionArtifact,
    warnings: Sequence[str],
    new_sources: int,
    research_model_run_id: UUID | None,
    rebuilt: bool = False,
) -> dict[str, Any]:
    """One deterministic stage result shape for a first run and a rebuild."""
    result: dict[str, Any] = {
        "stage": "references",
        "artifact_id": str(artifact.id),
        **production_reference_corpus_metadata(corpus),
        "sources_count": len(corpus.sources),
        "new_sources": new_sources,
        "warnings": list(warnings),
        "research_model_run_id": (
            str(research_model_run_id) if research_model_run_id is not None else None
        ),
        "rebuilt": rebuilt,
    }
    if has_usable_core_source(corpus):
        result["status"] = "success"
    else:
        result.update(
            {
                "status": "needs_review",
                "error_code": "references_no_usable_core_source",
                "error": "No core source of this run is archived and extractable",
            }
        )
    return result


class ProductionWorkflowOrchestrator:
    """Orchestrates the single article publication workflow."""

    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        model_service: ModelConversationService | None = None,
        model_gateway: ModelGateway | None = None,
        collection_service: SubjectCollectionService | None = None,
        artifact_store: ProductionArtifactStore | None = None,
        diagnostics: DiagnosticsLog | None = None,
        seed_enrichment: VirusTotalSeedEnrichmentService | None = None,
        pacing: ProductionPacingPolicy | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._model_service = model_service
        self._model_gateway = model_gateway or getattr(model_service, "_gateway", None)
        self._repair_payloads = ProductionRepairPayloadResolver(self._model_gateway)
        self._collection_service = collection_service
        self._artifact_store = artifact_store
        self._diagnostics = diagnostics or DiagnosticsLog(None)
        self._correlation_id = "-"
        production_uow_factory = cast(Any, uow_factory)
        self._references = ReferenceResearchService(production_uow_factory, artifact_store)
        self._extraction = ExtractionService(production_uow_factory, artifact_store)
        # The EXTRACTION stage asks the gateway for one structured capability
        # and reads the exact archived documents; without both it fails
        # explicitly.
        self._canonical_extraction: ProductionExtractionService | None = (
            ProductionExtractionService(
                uow_factory=production_uow_factory,
                model_gateway=self._model_gateway,
                artifact_store=artifact_store,
            )
            if self._model_gateway is not None and artifact_store is not None
            else None
        )
        self._synthesis = SynthesisService(production_uow_factory, artifact_store)
        self._assembly = PublicationAssemblyService(production_uow_factory, artifact_store)
        self._artifact_reuse = ProductionArtifactReuseService(
            production_uow_factory, artifact_store, self._diagnostics
        )
        # The canonical SYNTHESIS stage consumes only the frozen snapshot and
        # the current canonical EXTRACTION artifact. Without a gateway and a
        # store it fails explicitly instead of falling back to the legacy path.
        self._canonical_synthesis: ProductionSynthesisService | None = (
            ProductionSynthesisService(
                uow_factory=production_uow_factory,
                artifact_store=artifact_store,
                model_gateway=self._model_gateway,
                synthesis_service=self._synthesis,
                artifact_reuse=self._artifact_reuse,
            )
            if self._model_gateway is not None and artifact_store is not None
            else None
        )
        self._qa = ProductionQAService(production_uow_factory)
        self._seed_enrichment = seed_enrichment
        self._pacing = pacing or ProductionPacingPolicy.zero()
        self._settings = get_settings()

    async def _check_cancellation(self, run_id: UUID, context: JobExecutionContext | None) -> None:
        """Fence both the job cancellation flag and the persistent run state."""
        if context is not None:
            await context.check_cancelled()
        uow_factory = getattr(self, "_uow_factory", None)
        if uow_factory is None:
            return
        async with uow_factory() as uow:
            runs = getattr(uow, "production_runs", None)
            if runs is None:
                return
            run = await runs.get(run_id)
        if run is not None and run.status is ProductionRunStatus.CANCELLED:
            raise JobCancelledError

    async def _persist_extraction_progress(
        self,
        run_id: UUID,
        progress: dict[str, Any],
    ) -> None:
        """Write one compact progress snapshot in its own short transaction."""
        async with self._uow_factory() as uow:
            runs = getattr(uow, "production_runs", None)
            if runs is None:
                return
            get_for_update = getattr(runs, "get_for_update", None)
            get_run = get_for_update or getattr(runs, "get", None)
            if get_run is None:
                return
            persisted = await get_run(run_id)
            if persisted is None:
                return
            persisted.set_extraction_progress(progress)
            save = getattr(runs, "save", None)
            if save is not None:
                await save(persisted)
            commit = getattr(uow, "commit", None)
            if commit is not None:
                await commit()

    async def _close_completed_stage_conversation_best_effort(
        self,
        run: ProductionRun,
        stage: ProductionStage,
    ) -> None:
        """Archive the model conversation after a durable stage result.

        Archiving also asks the bridge to close the exact bound browser tab.
        Cleanup is deliberately best-effort: the stage artifact is already
        durable, so a browser cleanup failure must not turn a successful stage
        into a production failure.
        """
        model_service = getattr(self, "_model_service", None)
        if model_service is None:
            return

        conversation_id: UUID | None = None
        if stage is ProductionStage.SYNTHESIS:
            conversation_id = run.synthesis_conversation_id

        if conversation_id is None:
            return

        try:
            await model_service.archive(
                conversation_id,
                context_subject_id=run.subject_id,
            )
        except Exception as first_failure:
            first_failure_fields = conversation_close_failure_fields(first_failure)
            failure_code = first_failure_fields.get("error_code")
            # ModelConversationService wraps the bridge error to preserve the
            # durable archive/close boundary; recover from its typed cause.
            if failure_code == "conversation_session_close_failed":
                failure_code = first_failure_fields.get("cause_code")
            retry_allowed = ProductionRecoveryPolicyV1.is_auto_recoverable(failure_code)
            exc: Exception | None = first_failure
            if retry_allowed:
                # Le service worker MV3 de l'extension est évincé environ
                # toutes les trois minutes : une fermeture qui tombe dans
                # cette fenêtre réussit à la tentative suivante.
                await asyncio.sleep(_CONVERSATION_CLOSE_RETRY_DELAY_SECONDS)
                try:
                    await model_service.archive(
                        conversation_id,
                        context_subject_id=run.subject_id,
                    )
                    exc = None
                except Exception as second_failure:
                    exc = second_failure
            if exc is not None:
                failure = conversation_close_failure_fields(exc)
                self._diagnostics.record(
                    event="production.conversation_close_failed",
                    run_id=run.id,
                    subject_id=run.subject_id,
                    stage=stage.value,
                    correlation_id=self._correlation_id,
                    conversation_id=str(conversation_id),
                    error_type=type(exc).__name__,
                    error_code=failure["error_code"],
                    retryable=failure.get("retryable"),
                    phase=failure.get("phase"),
                    cause_code=failure.get("cause_code"),
                    reason=failure.get("reason"),
                    details=failure["details"],
                    attempts=2 if retry_allowed else 1,
                    error=str(exc)[:512],
                    error_message=str(exc)[:512],
                )

    async def execute_stage(
        self,
        run_id: UUID,
        expected_stage: ProductionStage,
        context: JobExecutionContext | None = None,
        correlation_id: str = "-",
    ) -> dict[str, Any]:
        """Idempotent: if stage is already complete, returns cached result."""
        self._correlation_id = correlation_id

        # Read the run without locking it. A stage spans a full model
        # round-trip; holding `FOR UPDATE` on the run for that long deadlocks
        # the stage against itself as soon as it opens its own unit of work,
        # and blocks the batch besides. State transitions take their own short
        # lock, in the job handler.
        async with self._uow_factory() as uow:
            run = await uow.production_runs.get(run_id)
            snapshot = (
                await uow.production_input_snapshots.get_by_run(run.id) if run is not None else None
            )
        if not run:
            raise ValueError(f"Production run {run_id} not found")
        if snapshot is None:
            raise RuntimeError("production_input_snapshot_missing")

        await self._check_cancellation(run.id, context)

        if run.current_stage != expected_stage:
            raise ValueError(
                f"Run on stage {run.current_stage.value}, expected {expected_stage.value}"
            )

        try:
            if expected_stage == ProductionStage.SOURCES:
                result = await self._execute_sources_stage(run, context, snapshot)
            elif expected_stage == ProductionStage.REFERENCES:
                result = await self._execute_references_stage(run, context, snapshot)
            elif expected_stage == ProductionStage.EXTRACTION:
                result = await self._execute_extraction_stage(run, context, snapshot)
            elif expected_stage == ProductionStage.SYNTHESIS:
                result = await self._execute_synthesis_stage(run, context, snapshot)
            elif expected_stage == ProductionStage.ASSEMBLY:
                result = await self._execute_assembly_stage(run, context, snapshot)
            else:
                raise ValueError(f"Unknown stage: {expected_stage.value}")
        except ProductionReuseStorageUnavailableError as exc:
            result = self._handle_stage_exception(run, expected_stage.value, exc)

        if result.get("status") in {"success", "cached", "reused"}:
            await self._close_completed_stage_conversation_best_effort(run, expected_stage)

        self._diagnostics.record_stage_outcome(
            run_id=run.id,
            subject_id=run.subject_id,
            stage=expected_stage.value,
            correlation_id=correlation_id,
            result=result,
        )
        return result

    def _handle_stage_exception(
        self, run: ProductionRun, stage: str, exc: Exception
    ) -> dict[str, Any]:
        """Preserves the original exception's traceback in diagnostics before
        converting it to a safe error result for the caller."""
        self._diagnostics.record_failure(
            event="stage.exception",
            run_id=run.id,
            subject_id=run.subject_id,
            stage=stage,
            correlation_id=self._correlation_id,
            error=exc,
        )
        return _transient_or_terminal(stage, exc)

    async def _reuse_artifact(
        self,
        run: ProductionRun,
        stage: str,
        input_hash: str,
    ) -> dict[str, Any] | None:
        artifact_stage = ProductionArtifactStage(stage)
        result = await self._artifact_reuse.find_or_reuse(
            run=run,
            stage=artifact_stage,
            input_hash=input_hash,
            allow_cross_run=cross_run_reuse_allowed(run, artifact_stage),
        )
        if result is None:
            return None
        return {
            "stage": stage,
            "status": "reused" if result.reused else "cached",
            "artifact_id": str(result.artifact.id),
            "reused": result.reused,
            "reused_from_artifact_id": (
                str(result.artifact.reused_from_artifact_id)
                if result.artifact.reused_from_artifact_id is not None
                else None
            ),
        }

    async def _find_compatible_historical_synthesis(
        self,
        *,
        uow: Any,
        run: ProductionRun,
        report: ReferenceReport,
        extraction_payload: Any,
        source_tiers_by_url: dict[str, str],
        semantic_projection_hash: str,
    ) -> ProductionArtifact | None:
        """Find a verified canonical draft with compatible semantic input."""
        if self._artifact_store is None:
            return None
        artifacts = await uow.production_artifacts.list_for_run(run.id)
        candidates = sorted(
            (
                artifact
                for artifact in artifacts
                if artifact.stage is ProductionArtifactStage.SYNTHESIS
                and artifact.canonical_blob_id is not None
                and artifact.rendered_blob_id is not None
                and artifact.status is ProductionArtifactStatus.VERIFIED
            ),
            key=lambda artifact: (artifact.created_at, str(artifact.id)),
            reverse=True,
        )
        extraction_artifacts = sorted(
            (
                artifact
                for artifact in artifacts
                if artifact.stage is ProductionArtifactStage.EXTRACTION
                and artifact.canonical_blob_id is not None
                and artifact.status
                in {ProductionArtifactStatus.VERIFIED, ProductionArtifactStatus.STALE}
            ),
            key=lambda artifact: (artifact.created_at, artifact.version),
            reverse=True,
        )
        for candidate in candidates:
            try:
                canonical = production_synthesis_from_json(
                    await self._artifact_store.read_json(cast(UUID, candidate.canonical_blob_id))
                )
                if canonical.subject_id != run.subject_id:
                    continue
                candidate_text = await self._artifact_store.read_text(
                    cast(UUID, candidate.rendered_blob_id)
                )
            except Exception:
                continue
            parsed = validate_synthesis(candidate_text, report, extraction_payload)
            if not parsed.usable:
                continue

            metadata_hash = (
                candidate.metadata.get("semantic_projection_hash")
                if isinstance(candidate.metadata, dict)
                else None
            )
            if metadata_hash is None and isinstance(candidate.metadata, dict):
                diagnostics = candidate.metadata.get("diagnostics")
                if isinstance(diagnostics, dict):
                    metadata_hash = diagnostics.get("semantic_projection_hash")
            if metadata_hash == semantic_projection_hash:
                return cast(ProductionArtifact, candidate)

            # Legacy rows have no semantic marker.  Compare the candidate to
            # every extraction version that existed before it; this is the
            # explicit BEFORE/AFTER compatibility check for historical hashes.
            for historical_extraction in extraction_artifacts:
                if historical_extraction.created_at > candidate.created_at:
                    continue
                try:
                    historical = legacy_technical_extraction_from_payload(
                        await self._artifact_store.read_json(
                            cast(UUID, historical_extraction.canonical_blob_id)
                        ),
                        source_labels=report_source_labels(report),
                    )
                except Exception:
                    continue
                if (
                    synthesis_projection_hash(report, historical, source_tiers_by_url)
                    == semantic_projection_hash
                ):
                    return cast(ProductionArtifact, candidate)
        return None

    @staticmethod
    def _synthesis_candidate_status_is_eligible(candidate: ProductionArtifact) -> bool:
        """Accept only durable synthesis rows that may serve as a draft.

        Production artifact staleness is produced by downstream invalidation;
        the repository deliberately keeps the old immutable row and changes
        only its status.  A separate stale row is never treated as a draft.
        """
        if candidate.status is ProductionArtifactStatus.VERIFIED:
            return True
        if candidate.status is not ProductionArtifactStatus.STALE:
            return False
        metadata = candidate.metadata if isinstance(candidate.metadata, dict) else {}
        stale_cause = metadata.get("stale_cause", metadata.get("stale_reason"))
        # Older rows predate the explicit marker.  Their STALE status can only
        # be produced by the downstream invalidation repository, so retain
        # compatibility while rejecting an explicitly unrelated cause.
        if stale_cause is None:
            return True
        return str(stale_cause) in {
            "downstream",
            "downstream_invalidation",
            "downstream_invalidated",
            "downstream_known",
            "narrative_repair",
            "retry_from_upstream",
            "stage_retry",
        }

    async def _historical_extraction_for_synthesis(
        self,
        *,
        candidate: ProductionArtifact,
        artifacts: Sequence[ProductionArtifact],
        report: ReferenceReport,
    ) -> tuple[ProductionArtifact, Any] | None:
        """Load the latest extraction that predates one historical Q4 row."""
        if self._artifact_store is None:
            return None
        extraction_artifacts = sorted(
            (
                artifact
                for artifact in artifacts
                if artifact.stage is ProductionArtifactStage.EXTRACTION
                and artifact.canonical_blob_id is not None
                and artifact.created_at <= candidate.created_at
                and artifact.status
                in {ProductionArtifactStatus.VERIFIED, ProductionArtifactStatus.STALE}
            ),
            key=lambda artifact: (artifact.created_at, artifact.version, str(artifact.id)),
            reverse=True,
        )
        for extraction_artifact in extraction_artifacts:
            try:
                extraction = legacy_technical_extraction_from_payload(
                    await self._artifact_store.read_json(
                        cast(UUID, extraction_artifact.canonical_blob_id)
                    ),
                    source_labels=report_source_labels(report),
                )
            except Exception:
                continue
            return extraction_artifact, extraction
        return None

    async def _find_revision_candidate(
        self,
        *,
        uow: Any,
        run: ProductionRun,
        report: ReferenceReport,
        extraction_artifact: ProductionArtifact,
        extraction_payload: Any,
        synthesis_pack: dict[str, Any],
        source_tiers_by_url: dict[str, str],
        current_semantic_hash: str,
    ) -> tuple[ProductionArtifact, SynthesisRevisionContext] | None:
        """Find a readable historical draft for a real semantic revision."""
        if self._artifact_store is None:
            return None
        artifacts = tuple(await uow.production_artifacts.list_for_run(run.id))
        candidates = sorted(
            (
                artifact
                for artifact in artifacts
                if artifact.stage is ProductionArtifactStage.SYNTHESIS
                and artifact.rendered_blob_id is not None
                and artifact.subject_id == run.subject_id
                and self._synthesis_candidate_status_is_eligible(artifact)
            ),
            key=lambda artifact: (artifact.created_at, str(artifact.id)),
            reverse=True,
        )
        current_source_ids = synthesis_semantic_source_ids(synthesis_pack)
        current_repair_keys = narrative_repair_keys(
            extraction_payload,
            extraction_artifact.metadata
            if isinstance(extraction_artifact.metadata, dict)
            else None,
        )

        for candidate in candidates:
            try:
                previous_text = await self._artifact_store.read_text(
                    cast(UUID, candidate.rendered_blob_id)
                )
            except Exception:
                continue
            if not previous_text.strip():
                continue
            if len(previous_text.encode("utf-8")) > MAX_SYNTHESIS_REVISION_TEXT_BYTES:
                continue

            metadata = candidate.metadata if isinstance(candidate.metadata, dict) else {}
            previous_source_ids_value = metadata.get("semantic_source_ids")
            previous_source_ids: tuple[str, ...]
            previous_pack: dict[str, Any] | None = None
            if isinstance(previous_source_ids_value, list | tuple):
                previous_source_ids = tuple(
                    sorted(str(value) for value in previous_source_ids_value)
                )
            else:
                historical = await self._historical_extraction_for_synthesis(
                    candidate=candidate, artifacts=artifacts, report=report
                )
                if historical is not None:
                    _, historical_extraction = historical
                    previous_pack = self._build_synthesis_evidence_pack(
                        report, historical_extraction, source_tiers_by_url
                    )
                    previous_source_ids = synthesis_semantic_source_ids(previous_pack)
                else:
                    previous_source_ids = ()
            previous_source_ids = tuple(
                sorted(
                    set(previous_source_ids)
                    | set(re.findall(r"\[(S\d+)\]", previous_text, re.IGNORECASE))
                )
            )

            previous_repair_keys_value = metadata.get("semantic_repair_keys")
            if isinstance(previous_repair_keys_value, list | tuple):
                previous_repair_keys = tuple(
                    sorted(str(value) for value in previous_repair_keys_value)
                )
            else:
                historical = await self._historical_extraction_for_synthesis(
                    candidate=candidate, artifacts=artifacts, report=report
                )
                previous_repair_keys = (
                    narrative_repair_keys(
                        historical[1],
                        getattr(historical[0], "metadata", None),
                    )
                    if historical is not None
                    else ()
                )

            previous_semantic_hash = metadata.get("semantic_projection_hash")
            if not isinstance(previous_semantic_hash, str):
                diagnostics = metadata.get("diagnostics")
                previous_semantic_hash = (
                    diagnostics.get("semantic_projection_hash")
                    if isinstance(diagnostics, dict)
                    else None
                )
            if not isinstance(previous_semantic_hash, str):
                if previous_pack is None:
                    historical = await self._historical_extraction_for_synthesis(
                        candidate=candidate, artifacts=artifacts, report=report
                    )
                    if historical is not None:
                        previous_pack = self._build_synthesis_evidence_pack(
                            report, historical[1], source_tiers_by_url
                        )
                previous_semantic_hash = (
                    compute_input_hash(previous_pack)
                    if previous_pack is not None
                    else current_semantic_hash
                )

            context = build_synthesis_revision_context(
                previous_artifact_id=candidate.id,
                previous_input_hash=candidate.input_hash,
                previous_text=previous_text,
                previous_semantic_hash=previous_semantic_hash,
                current_semantic_hash=current_semantic_hash,
                previous_source_ids=previous_source_ids,
                current_source_ids=current_source_ids,
                previous_repair_keys=previous_repair_keys,
                current_repair_keys=current_repair_keys,
            )
            if revision_context_size_bytes(context) > MAX_SYNTHESIS_REVISION_CONTEXT_BYTES:
                continue
            return candidate, context
        return None

    def _record_synthesis_mode(
        self,
        *,
        run: ProductionRun,
        mode: SynthesisMode,
        previous_artifact_id: UUID | None,
        previous_word_count: int,
        context: SynthesisRevisionContext | None,
    ) -> None:
        self._diagnostics.record(
            event="synthesis.mode",
            run_id=run.id,
            subject_id=run.subject_id,
            stage="synthesis",
            mode=mode.value,
            previous_synthesis_artifact_id=(
                str(previous_artifact_id) if previous_artifact_id is not None else None
            ),
            # The revision identity depends on the previous *content*, never on
            # the artifact id, so the log has to name that content hash too.
            previous_synthesis_sha256=(
                synthesis_content_hash(context.previous_text) if context is not None else None
            ),
            semantic_projection_changed=(
                context.previous_semantic_hash != context.current_semantic_hash
                if context is not None
                else False
            ),
            previous_word_count=previous_word_count,
            semantic_delta_added_sources=(
                list(context.added_source_ids) if context is not None else []
            ),
            semantic_delta_removed_sources=(
                list(context.removed_source_ids) if context is not None else []
            ),
            semantic_delta_repair_count=(
                len(context.added_repair_keys) + len(context.removed_repair_keys)
                if context is not None
                else 0
            ),
        )

    async def _ask_with_format_repair(
        self,
        *,
        run: ProductionRun,
        conversation_id: UUID,
        stage: str,
        prompt: str,
        prompt_version: str,
        repair_version: str,
        mode: ConversationMode,
        parse: Callable[[str], Any],
        external_llm_allowed: bool,
        web_search: bool = False,
        request_identity: str | None = None,
        lifecycle_policy: ConversationPolicy = ConversationPolicy.KEEP,
        context: JobExecutionContext | None = None,
    ) -> tuple[Any | None, str, UUID | None, UUID | None]:
        """Ask the model, and give it exactly one chance to fix its formatting.

        Used by Q4 (synthesis): it drafts FRESH with web search, then repairs
        CONTINUE without web search — the repair turn
        never researches again, it restates the same answer in the expected
        structure. Returns the parse result, the raw text used, and the turn
        id it came from.
        """
        assert self._model_service is not None
        identity = f"-{request_identity}" if request_identity else ""
        idempotency_key = f"{stage}-{run.id}-v{prompt_version}{identity}"
        await self._check_cancellation(run.id, context)
        turn = await self._model_service.add_turn(
            conversation_id=conversation_id,
            message=prompt,
            mode=mode,
            external_llm_allowed=external_llm_allowed,
            web_search=web_search,
            idempotency_key=idempotency_key,
            correlation_id=self._correlation_id,
            context_subject_id=run.subject_id,
            lifecycle_policy=lifecycle_policy,
        )
        model_run_id = getattr(turn, "model_run_id", None)
        raw = await self._turn_output_text(conversation_id, turn.id) or ""
        await self._check_cancellation(run.id, context)
        self._diagnostics.record_model_answer(
            run_id=run.id,
            subject_id=run.subject_id,
            stage=stage,
            correlation_id=self._correlation_id,
            prompt=prompt,
            answer=raw,
            idempotency_key=idempotency_key,
        )
        if not raw:
            return None, "", turn.id, model_run_id

        result = parse(raw)
        self._log_parse(run, stage, result)
        if result.usable:
            return result, raw, turn.id, model_run_id

        repair_prompt = ProductionPromptTemplates.get_format_repair_prompt(
            stage=stage, problems=_repair_problem_descriptions(result)
        )
        repair_idempotency_key = (
            f"{stage}-format-repair-{run.id}-v{prompt_version}{identity}-rv{repair_version}"
            if request_identity
            else f"{stage}-format-repair-{run.id}-v{repair_version}"
        )
        await self._check_cancellation(run.id, context)
        repair_turn = await self._model_service.add_turn(
            conversation_id=conversation_id,
            message=repair_prompt,
            mode=ConversationMode.CONTINUE,
            external_llm_allowed=external_llm_allowed,
            web_search=False,
            idempotency_key=repair_idempotency_key,
            correlation_id=self._correlation_id,
            context_subject_id=run.subject_id,
        )
        repair_model_run_id = getattr(repair_turn, "model_run_id", None)
        repaired_raw = await self._turn_output_text(conversation_id, repair_turn.id) or ""
        await self._check_cancellation(run.id, context)
        self._diagnostics.record_model_answer(
            run_id=run.id,
            subject_id=run.subject_id,
            stage=f"{stage}-repair",
            correlation_id=self._correlation_id,
            prompt=repair_prompt,
            answer=repaired_raw,
            idempotency_key=repair_idempotency_key,
        )
        if not repaired_raw:
            return result, raw, turn.id, model_run_id

        repaired = parse(repaired_raw)
        self._log_parse(run, f"{stage}-repair", repaired)
        repaired.repair_actions.append(f"{stage}_format_repair")
        repaired.warnings.extend(result.errors)
        return repaired, repaired_raw, repair_turn.id, repair_model_run_id

    async def _collect_reference_proposals(
        self,
        run: ProductionRun,
        proposals: Sequence[ProductionReferenceProposal],
        context: JobExecutionContext | None,
        snapshot: ProductionInputSnapshot | None = None,
    ) -> _ReferenceCollectionOutcome:
        """Attach, collect and archive the supplementary URLs Q1 proposed.

        Collection is additive: a failed download never removes a source from
        the corpus and never fails the stage on its own. A URL already attached
        to the subject is reused, never re-downloaded.
        """
        warnings: list[str] = []
        supplemental_failures: list[dict[str, Any]] = []
        new_sources = 0

        if self._collection_service is not None and proposals:
            supplemental = [
                SupplementalSource(
                    url=proposal.canonical_url,
                    title=proposal.title,
                    publisher=proposal.publisher,
                    published_at=proposal.published_at,
                    role=proposal.role,
                )
                for proposal in proposals
            ]
            proposed_urls = {proposal.canonical_url for proposal in proposals}
            try:
                await self._check_cancellation(run.id, context)
                added = await self._collection_service.add_supplemental_sources(
                    run.subject_id, supplemental
                )
                new_sources = len(added)
                if context is not None:
                    for collection in await self._collection_service.list_sources(run.subject_id):
                        if (
                            collection.canonical_url not in proposed_urls
                            or collection.state.value in _ARCHIVED_STATES
                        ):
                            continue
                        collection_state = getattr(collection.state, "value", collection.state)
                        retry_attempted = False
                        try:
                            await self._check_cancellation(run.id, context)
                            if (
                                collection_state == CollectionState.FAILED_RETRYABLE.value
                                and callable(
                                    getattr(self._collection_service, "prepare_retry", None)
                                )
                            ):
                                # One targeted retry is allowed for a retryable
                                # supplemental collection. Blocked and terminal
                                # states never enter this branch.
                                await self._collection_service.prepare_retry(collection.id)
                                retry_attempted = True
                                await self._check_cancellation(run.id, context)
                            await self._collection_service.collect_subject(
                                run.subject_id,
                                context.job_id,
                                context,
                                collection_id=collection.id,
                                snapshot=snapshot,
                            )
                        except JobCancelledError:
                            raise
                        except Exception as exc:
                            warnings.append(
                                _supplemental_failure_warning(
                                    exc,
                                    canonical_url=collection.canonical_url,
                                )
                            )
                            failure_fields = _supplemental_failure_fields(
                                exc,
                                canonical_url=collection.canonical_url,
                                collection_state=str(collection_state),
                                retry_attempted=retry_attempted,
                            )
                            supplemental_failures.append(failure_fields)
                            diagnostics = getattr(self, "_diagnostics", None)
                            if diagnostics is not None:
                                diagnostics.record(
                                    event="q1.supplemental_collection_failed",
                                    run_id=run.id,
                                    subject_id=run.subject_id,
                                    stage="references",
                                    correlation_id=getattr(self, "_correlation_id", None),
                                    **failure_fields,
                                )
            except JobCancelledError:
                raise
            except Exception as exc:
                warnings.append(_supplemental_failure_warning(exc))
                failure_fields = _supplemental_failure_fields(exc)
                supplemental_failures.append(failure_fields)
                diagnostics = getattr(self, "_diagnostics", None)
                if diagnostics is not None:
                    diagnostics.record(
                        event="q1.supplemental_collection_failed",
                        run_id=run.id,
                        subject_id=run.subject_id,
                        stage="references",
                        correlation_id=getattr(self, "_correlation_id", None),
                        **failure_fields,
                    )

        return _ReferenceCollectionOutcome(
            new_sources=new_sources,
            warnings=tuple(warnings),
            failures=tuple(supplemental_failures),
        )

    async def _load_qa_inputs(
        self,
        references: Any,
        extraction: Any,
        synthesis: Any,
        publication: Any,
    ) -> dict[str, Any]:
        """Read back what QA needs to judge the publication."""
        if self._artifact_store is None:
            return {}
        store = self._artifact_store
        loaded: dict[str, Any] = {}
        try:
            report, legacy_extraction, synthesis_text = await self._assembly._load_inputs(
                references, extraction, synthesis
            )
            loaded.update(
                {
                    "report": report,
                    "extraction": legacy_extraction,
                    "synthesis_text": synthesis_text,
                }
            )
            if publication.rendered_blob_id is not None:
                loaded["publication_markdown"] = await store.read_text(publication.rendered_blob_id)
        except Exception:
            return loaded
        return loaded

    async def _load_reference_report(self, artifact: Any) -> ReferenceReport | None:
        """Read the REFERENCES artifact as the temporary legacy `ReferenceReport`.

        The single Q1 compatibility boundary: a canonical AW-010 corpus is
        projected back through the RAW wire format, while a V4 imported
        artifact stays the legacy report it already is.
        """
        if self._artifact_store is None or getattr(artifact, "canonical_blob_id", None) is None:
            return None
        try:
            return await load_reference_projection(self._artifact_store, artifact)
        except (OSError, UnicodeError, ValueError):
            return None

    def _log_parse(self, run: ProductionRun, stage: str, result: ParseResult[Any]) -> None:
        self._diagnostics.record_parse(
            run_id=run.id,
            subject_id=run.subject_id,
            stage=stage,
            correlation_id=self._correlation_id,
            usable=result.usable,
            warnings=result.warnings,
            errors=result.errors,
            repair_actions=result.repair_actions,
            dropped_blocks=result.dropped_blocks,
        )

    async def _subject_context(
        self,
        uow: UnitOfWork,
        subject_id: UUID,
        snapshot: ProductionInputSnapshot | None = None,
    ) -> tuple[str, str]:
        """Canonical subject title and discovery context from the frozen snapshot."""
        if snapshot is None:
            raise ValueError("production_input_snapshot_missing")
        if snapshot.subject_id != subject_id:
            raise ValueError("production_input_snapshot_subject_mismatch")
        return snapshot.subject_title, snapshot.discovery_summary

    async def _turn_output_text(self, conversation_id: UUID, turn_id: UUID) -> str | None:
        """Read a turn's output text.

        The turn entity only carries a blob reference; the conversation service
        is what resolves it back to text.
        """
        assert self._model_service is not None
        for content in await self._model_service.turns(conversation_id):
            if content.turn.id == turn_id:
                return content.output_text
        return None

    async def _open_synthesis_conversation(
        self, run: ProductionRun, subject_title: str
    ) -> ModelConversation:
        """Synthesis keeps its drafting conversation; REFERENCES is stateless."""
        assert self._model_service is not None
        return await self._model_service.create(
            provider=ModelProvider.OPENAI,
            transport=ConversationTransport.CHATGPT_BRIDGE,
            purpose=ConversationPurpose.DRAFTING,
            title=f"Production synthesis — {subject_title}",
            edition_id=run.edition_id,
            subject_id=run.subject_id,
            expected_profile=None,
            requested_model=None,
        )

    async def _execute_sources_stage(
        self,
        run: ProductionRun,
        context: JobExecutionContext | None = None,
        snapshot: ProductionInputSnapshot | None = None,
    ) -> dict[str, Any]:
        """No LLM. Pulls publications retained at discovery, dedupes by canonical
        URL, downloads and archives them into the subject workspace."""
        if self._collection_service is None:
            return {
                "stage": "sources",
                "status": "error",
                "error": "SubjectCollectionService not configured",
            }
        if context is None:
            return {
                "stage": "sources",
                "status": "error",
                "error": "Job context required for source collection",
            }

        await self._check_cancellation(run.id, context)
        try:
            await self._collection_service.collect_subject(
                run.subject_id,
                context.job_id,
                context,
                snapshot=snapshot,
            )
            sources = await self._collection_service.list_sources(run.subject_id)
            if snapshot is not None:
                snapshot_urls = {source.canonical_url for source in snapshot.core_sources}
                sources = [source for source in sources if source.canonical_url in snapshot_urls]
        except JobCancelledError:
            raise
        except Exception as e:
            details = getattr(e, "details", None)
            # Le job dispatcher n'accorde ses trois tentatives qu'à un
            # `transient_error`. Un échec de collecte dont au moins une source
            # est elle-même retryable doit rester retryable, sinon le sujet
            # meurt sur un aléa réseau.
            retryable = bool(
                isinstance(details, dict) and int(details.get("failed_retryable", 0) or 0) > 0
            )
            return {
                "stage": "sources",
                "status": "transient_error" if retryable else "error",
                "error_code": str(getattr(e, "code", "") or "sources_error"),
                "error": str(e),
                "details": details,
            }

        archived = sum(1 for source in sources if source.state in _ARCHIVED_STATES)
        await self._check_cancellation(run.id, context)
        if archived == 0:
            return {
                "stage": "sources",
                "status": "error",
                "error": "No source could be archived for this subject",
            }

        return {
            "stage": "sources",
            "status": "success",
            "sources_count": len(sources),
            "archived": archived,
        }

    async def _execute_references_stage(
        self,
        run: ProductionRun,
        context: JobExecutionContext | None = None,
        snapshot: ProductionInputSnapshot | None = None,
    ) -> dict[str, Any]:
        """Q1: one stateless web-research call, then an additive corpus.

        The corpus is the canonical REFERENCES state: every snapshot core plus
        the supplementary publications the model proposed, each with its exact
        collection observation. A rebuild of the same run re-materializes from
        the RAW already held, so a manual archive never calls the model again.
        """
        await self._check_cancellation(run.id, context)
        if snapshot is None:
            return {
                "stage": "references",
                "status": "error",
                "error": "production_input_snapshot_missing",
            }
        async with self._uow_factory() as uow:
            research_date = run.research_date or datetime.now(UTC).date()
            ctx = await build_subject_production_context(
                uow,
                run.subject_id,
                snapshot=snapshot,
                relevant_source_urls=None,
            )
            subject_title = ctx.subject_title

            input_hash = _references_input_hash(snapshot=snapshot, research_date=research_date)
            current = await uow.production_artifacts.get_current(run.id, "references")

        # A rebuild of this run re-materializes the corpus from the RAW it
        # already holds and the collection state observed right now.  This is
        # what lets an analyst archive a proposed source by hand and get a new
        # corpus version without a second model call.
        if (
            current is not None
            and current.status is ProductionArtifactStatus.VERIFIED
            and current.input_hash == input_hash
        ):
            rebuilt = await self._rematerialize_references_artifact(
                run=run, snapshot=snapshot, current=current
            )
            if rebuilt is not None:
                return rebuilt

        # Cross-run reuse happens before any submission: an identical
        # functional identity must never cost another web research.
        reused = await self._reuse_artifact(run, "references", input_hash)
        if reused is not None:
            return reused

        if self._model_gateway is None:
            return {
                "stage": "references",
                "status": "error",
                "error": "ModelGateway not configured",
            }

        # The diffusion policy, not a hardcoded flag, decides whether this
        # subject may be sent to an external model.
        if not ctx.external_llm_allowed:
            return {
                "stage": "references",
                "status": "needs_review",
                "error_code": "external_llm_blocked",
                "error": "Diffusion policy forbids sending this subject to an external model",
            }

        prompt = ProductionPromptTemplates.get_references_prompt(
            subject_title=subject_title,
            subject_description=ctx.subject_description,
            actor_info=ctx.actor_info,
            technical_summary=ctx.technical_summary,
            research_date=research_date.isoformat(),
            period_start=ctx.period_start,
            period_end=ctx.period_end,
            core_sources_text=ctx.core_sources_text,
            supporting_sources_text=ctx.supporting_sources_text,
        )
        # Stateless research: no conversation, exact retry identity.  The
        # gateway owns provider selection, background resume, reconciliation
        # and the refusal to replay an unsafe submission.
        model_run_id = production_references_model_run_id(run.id, input_hash)
        request = ModelRequest(
            text=prompt,
            prompt_template_id=REFERENCES_PROMPT_TEMPLATE_ID,
            prompt_template_version=REFERENCES_PROMPT_VERSION,
            evidence_pack_hash=hashlib.sha256(prompt.encode()).hexdigest(),
            external_llm_allowed=ctx.external_llm_allowed,
            routing_hint=ModelRoutingHint.WEB_RESEARCH,
            web_search=True,
            conversation=None,
            run_id=model_run_id,
            metadata={
                "subject_id": str(run.subject_id),
                "references_input_hash": input_hash,
                "production_input_snapshot_hash": snapshot.input_hash,
                "research_date": research_date.isoformat(),
                "parser_version": PRODUCTION_REFERENCE_PARSER_VERSION,
                "corpus_schema_version": PRODUCTION_REFERENCE_CORPUS_SCHEMA_VERSION,
                "routing_policy_version": REFERENCES_ROUTING_POLICY_VERSION,
            },
            parameters={
                "references_input_hash": input_hash,
                "prompt_version": REFERENCES_PROMPT_VERSION,
                "parser_version": PRODUCTION_REFERENCE_PARSER_VERSION,
                "corpus_schema_version": PRODUCTION_REFERENCE_CORPUS_SCHEMA_VERSION,
                "routing_policy_version": REFERENCES_ROUTING_POLICY_VERSION,
            },
        )
        model_gateway = cast(ModelGateway, self._model_gateway)
        try:
            execution = await model_gateway.research(request)
        except JobCancelledError:
            raise
        except Exception as e:
            await self._check_cancellation(run.id, context)
            return self._handle_stage_exception(run, "references", e)

        raw = execution.output_text or ""
        research_model_run_id = getattr(execution.run, "id", None)
        if not raw:
            return {
                "stage": "references",
                "status": "needs_review",
                "error_code": "no_model_response",
                "error": "No response from model",
            }

        # The canonical parser reads SOURCE blocks only; the legacy
        # editorial-title/EVENT/UNCERTAINTIES sections of the RAW are ignored
        # here and re-projected later for the stages that still need them.
        parsed = parse_production_reference_proposals(raw, research_date)
        self._log_parse(run, "references", parsed)
        await self._check_cancellation(run.id, context)
        collection = await self._collect_reference_proposals(
            run, parsed.value or (), context, snapshot
        )
        corpus = await self._build_reference_corpus(
            run=run,
            snapshot=snapshot,
            research_date=research_date,
            proposals=parsed.value or (),
            warnings=[*parsed.warnings, *collection.warnings],
        )

        await self._check_cancellation(run.id, context)
        artifact, created = await self._references.store_references_result(
            run_id=run.id,
            subject_id=run.subject_id,
            input_hash=input_hash,
            raw_result=raw,
            corpus=corpus,
            model_run_id=research_model_run_id,
        )
        return _reference_corpus_result(
            corpus=corpus,
            artifact=artifact,
            warnings=corpus.warnings,
            new_sources=collection.new_sources,
            research_model_run_id=research_model_run_id,
            rebuilt=not created,
        )

    async def _rematerialize_references_artifact(
        self,
        *,
        run: ProductionRun,
        snapshot: ProductionInputSnapshot,
        current: ProductionArtifact,
    ) -> dict[str, Any] | None:
        """Rebuild this run's corpus from its RAW and the live collection state.

        Returns ``None`` when the stored canonical payload is not an AW-010
        corpus (a V4 import or another legacy artifact), so the caller falls
        back to the ordinary reuse path instead of upgrading it.
        """
        store = self._artifact_store
        if store is None or current.raw_blob_id is None or current.canonical_blob_id is None:
            return None
        try:
            payload = await store.read_json(current.canonical_blob_id)
        except ProductionReuseStorageUnavailableError:
            raise
        except (OSError, UnicodeError, ValueError):
            return None
        try:
            stored_corpus = production_reference_corpus_from_json(payload)
        except ValueError:
            # TODO AW-012/AW-013: a legacy imported artifact is reused as-is.
            return None

        raw = await store.read_text(current.raw_blob_id)
        parsed = parse_production_reference_proposals(raw, stored_corpus.research_date)
        self._log_parse(run, "references-rebuild", parsed)
        # No collection, no model call: the rebuild only observes what the
        # subject holds right now. Parser and collection warnings are those
        # of the previous corpus, so an unchanged corpus stays byte-identical.
        corpus = await self._build_reference_corpus(
            run=run,
            snapshot=snapshot,
            research_date=stored_corpus.research_date,
            proposals=parsed.value or (),
            warnings=stored_corpus.warnings,
            previous=stored_corpus,
        )
        artifact, created = await self._references.store_references_result(
            run_id=run.id,
            subject_id=run.subject_id,
            input_hash=current.input_hash,
            raw_result=raw,
            corpus=corpus,
            model_run_id=current.model_run_id,
        )
        if not created:
            # Same functional corpus: no artificial V+1.
            return {
                "stage": "references",
                "status": "cached",
                "artifact_id": str(artifact.id),
                "reused": False,
                "reused_from_artifact_id": None,
            }
        return _reference_corpus_result(
            corpus=corpus,
            artifact=artifact,
            warnings=corpus.warnings,
            new_sources=0,
            research_model_run_id=current.model_run_id,
            rebuilt=True,
        )

    async def _build_reference_corpus(
        self,
        *,
        run: ProductionRun,
        snapshot: ProductionInputSnapshot,
        research_date: date,
        proposals: Sequence[ProductionReferenceProposal],
        warnings: Sequence[str],
        previous: ProductionReferenceCorpusV1 | None = None,
    ) -> ProductionReferenceCorpusV1:
        """Assemble the canonical corpus: core sources then additive proposals."""
        urls = [source.canonical_url for source in snapshot.core_sources]
        urls.extend(proposal.canonical_url for proposal in proposals)
        if previous is not None:
            urls.extend(source.canonical_url for source in previous.sources)
        async with self._uow_factory() as uow:
            observations = await observe_reference_collections(uow, run.subject_id, urls)
        return build_production_reference_corpus(
            subject_id=run.subject_id,
            research_date=research_date,
            production_input_hash=snapshot.input_hash,
            core_sources=snapshot.core_sources,
            proposals=proposals,
            observations=observations,
            warnings=warnings,
            previous=previous,
        )

    async def _execute_extraction_stage(
        self,
        run: ProductionRun,
        context: JobExecutionContext | None = None,
        snapshot: ProductionInputSnapshot | None = None,
    ) -> dict[str, Any]:
        """Run the canonical, archive-backed EXTRACTION stage (AW-011).

        The stage consumes the frozen ``ProductionReferenceCorpusV1`` of
        REFERENCES, resolves every eligible source by its exact archived
        document, and never opens the network: acquiring and archiving sources
        belongs to SOURCES/REFERENCES.
        """

        await self._check_cancellation(run.id, context)
        service = self._canonical_extraction
        if service is None or self._artifact_store is None:
            return {
                "stage": "extraction",
                "status": "terminal_error",
                "error_code": "extraction_service_unavailable",
                "error": "Canonical extraction requires a ModelGateway and an artifact store",
            }

        try:
            plan = await service.plan(run=run, snapshot=snapshot)
        except ProductionExtractionControlError as control:
            # A control invariant failed: no silent recovery through the legacy
            # REFERENCES RAW, and no model call.
            return {
                "stage": "extraction",
                "status": "needs_review",
                "error_code": control.code,
                "error": str(control),
                "details": dict(control.details),
            }

        reused = await self._reuse_artifact(run, "extraction", plan.input_hash)
        if reused is not None:
            return reused

        calls = 0

        async def before_model_call() -> None:
            nonlocal calls
            await self._check_cancellation(run.id, context)
            if calls:
                await asyncio.sleep(self._pacing.model_delay_seconds())
            calls += 1

        try:
            execution = await service.execute(
                run=run, snapshot=snapshot, plan=plan, before_model_call=before_model_call
            )
        except ModelGatewayError as exc:
            # Only a proven pre-submission failure is retryable; the retry
            # reuses the same deterministic ModelRun identities.
            return self._handle_stage_exception(run, "extraction", exc)
        extraction = execution.extraction
        if not execution.succeeded or extraction is None:
            failed_source_id = execution.details.get("source_document_id")
            await self._persist_extraction_progress(
                run.id,
                _canonical_extraction_progress(
                    plan,
                    model_calls=execution.model_calls,
                    failed_source_id=(
                        failed_source_id if isinstance(failed_source_id, str) else None
                    ),
                ),
            )
            return {
                "stage": "extraction",
                "status": "needs_review",
                "error_code": execution.error_code,
                "error": execution.error,
                "details": dict(execution.details),
            }

        await self._check_cancellation(run.id, context)
        repair_entries, rejection_diagnostics = _repair_evidence(run, execution.rejections)
        rejected_rules = [
            entry for entry in rejection_diagnostics if entry["proposal_kind"] == "rule"
        ]
        repair_evidence_blob_id = await self._artifact_store.put_repair_evidence(
            build_repair_evidence_pack(repair_entries)
        )
        artifact = await self._extraction.store_extraction_result(
            run_id=run.id,
            subject_id=run.subject_id,
            input_hash=plan.input_hash,
            extraction=extraction,
            warnings=list(extraction.warnings),
            verification_diagnostics={
                "extraction_service_version": PRODUCTION_EXTRACTION_SERVICE_VERSION,
                "source_text_contract_version": source_text_contract_version(),
                "references_corpus_hash": extraction.references_corpus_hash,
                "model_calls": execution.model_calls,
                "q2_source_evidence_rejections": rejection_diagnostics[:200],
                "q2_rejected_rules": rejected_rules,
                "q2_rejected_rule_count": len(rejected_rules),
                "q2_rejected_artifact_count": len(rejection_diagnostics) - len(rejected_rules),
                "q2_rejected_ioc_count": sum(
                    is_publication_ioc_artifact_type(entry.get("artifact_type"))
                    for entry in rejection_diagnostics
                    if entry["proposal_kind"] == "artifact"
                ),
            },
            repair_evidence_blob_id=repair_evidence_blob_id,
            repair_evidence_entry_count=len(repair_entries),
            repair_evidence_index=[
                {key: value for key, value in entry.items() if key != "value"}
                | {"preview": str(entry["value"])[:512]}
                for entry in repair_entries
            ],
        )
        # The extraction is durable before the Repair Desk overlay is replayed
        # over it: a repair projection is a derivative of this exact artifact.
        effective_artifact_id: str | None = None
        async with self._uow_factory() as replay_uow:
            effective_decisions = [
                decision
                for decision in await replay_uow.production_repair_decisions.effective_decisions(
                    run.edition_id, run.subject_id
                )
                if decision.issue_kind
                in {
                    ProductionRepairIssueKind.REJECTED_INDICATOR,
                    ProductionRepairIssueKind.REJECTED_RULE,
                }
            ]
            if effective_decisions:
                replay_run = await replay_uow.production_runs.get(run.id) or run
                effective_artifact = await reconcile_effective_repairs_in_uow(
                    replay_uow,
                    run=replay_run,
                    base_extraction_artifact=artifact,
                    artifact_store=self._artifact_store,
                    payload_resolver=self._repair_payloads,
                )
                if effective_artifact is not None:
                    effective_artifact_id = str(effective_artifact.id)
                await replay_uow.commit()

        progress = _canonical_extraction_progress(
            plan, extraction=extraction, model_calls=execution.model_calls
        )
        await self._persist_extraction_progress(run.id, progress)
        return {
            "stage": "extraction",
            "status": "success",
            "artifact_id": str(artifact.id),
            "effective_artifact_id": effective_artifact_id,
            **production_extraction_metadata(extraction),
            "model_calls": execution.model_calls,
            "references_corpus_hash": extraction.references_corpus_hash,
        }

    @staticmethod
    def _build_synthesis_evidence_pack(
        report: ReferenceReport,
        extraction: Any,
        source_tiers_by_url: dict[str, str],
    ) -> dict[str, Any]:
        """Return the canonical Q4 projection, including the pure item filter."""
        return synthesis_projection_payload(report, extraction, source_tiers_by_url)

    async def _execute_synthesis_stage(
        self,
        run: ProductionRun,
        context: JobExecutionContext | None = None,
        snapshot: ProductionInputSnapshot | None = None,
    ) -> dict[str, Any]:
        """Canonical SYNTHESIS: frozen snapshot plus current EXTRACTION artifact.

        AW-012 removed REFERENCES, the reference report, the legacy technical
        extraction and the conversational transport from this stage.
        ``ProductionSynthesisService`` owns the deterministic evidence pack, the
        single stateless ``ModelGateway.draft`` submission, the grounding
        validation, the reuse and the revision; this boundary only loads the
        current canonical EXTRACTION artifact and maps the bounded execution
        outcome onto the durable job state transitions.
        """
        await self._check_cancellation(run.id, context)
        if snapshot is None:
            return {
                "stage": "synthesis",
                "status": "terminal_error",
                "error_code": "synthesis_inputs_missing",
                "error": "Production input snapshot is not readable",
            }
        service = self._canonical_synthesis
        if service is None:
            return {
                "stage": "synthesis",
                "status": "error",
                "error": "Canonical synthesis service is not configured",
            }
        async with self._uow_factory() as uow:
            # Only the current EXTRACTION artifact is read: REFERENCES is no
            # longer loaded, hashed or projected by the canonical branch.
            extraction_artifact = await uow.production_artifacts.get_current(run.id, "extraction")
        if extraction_artifact is None:
            return {
                "stage": "synthesis",
                "status": "error",
                "error": "Extraction artifact not found",
            }
        try:
            execution = await service.execute(run, snapshot, extraction_artifact)
        except JobCancelledError:
            raise
        except Exception as exc:
            await self._check_cancellation(run.id, context)
            return self._handle_stage_exception(run, "synthesis", exc)
        await self._check_cancellation(run.id, context)
        return self._synthesis_execution_result(execution)

    @staticmethod
    def _synthesis_execution_result(
        execution: ProductionSynthesisExecution,
    ) -> dict[str, Any]:
        """Map the canonical execution onto the durable stage-result contract."""
        details = dict(execution.details)
        result: dict[str, Any] = {
            "stage": "synthesis",
            "mode": execution.mode.value,
            "input_hash": execution.input_hash,
            "extraction_hash": execution.extraction_hash,
            "model_calls": execution.model_calls,
        }
        if execution.model_run_id is not None:
            result["model_run_id"] = str(execution.model_run_id)
        artifact_id = str(execution.artifact_id) if execution.artifact_id is not None else None
        if execution.status is SynthesisExecutionStatus.SUCCEEDED:
            result.update(
                {
                    "status": "success",
                    "artifact_id": artifact_id,
                    "reused": False,
                    "reused_from_artifact_id": None,
                    **details,
                }
            )
            return result
        if execution.status is SynthesisExecutionStatus.REUSED:
            reused = bool(details.get("reused"))
            result.update(
                {
                    "status": "reused" if reused else "cached",
                    "artifact_id": artifact_id,
                    "reused": reused,
                    "reused_from_artifact_id": details.get("reused_from_artifact_id"),
                }
            )
            return result
        if execution.status is SynthesisExecutionStatus.NEEDS_REVIEW:
            result.update(
                {
                    "status": "needs_review",
                    "error_code": execution.error_code or "synthesis_validation_failed",
                    "error": execution.error or "Synthesis requires review",
                    "details": details,
                }
            )
            return result
        # BLOCKED: a canonical input is absent or inconsistent and drafting
        # never started, so the run fails instead of pretending a draft exists.
        result.update(
            {
                "status": "terminal_error",
                "error_code": execution.error_code or "synthesis_inputs_missing",
                "error": execution.error or "Canonical synthesis inputs are missing",
                "details": details,
            }
        )
        return result

    async def _execute_assembly_stage(
        self,
        run: ProductionRun,
        context: JobExecutionContext | None = None,
        snapshot: ProductionInputSnapshot | None = None,
    ) -> dict[str, Any]:
        """Deterministic: pure rendering from artifacts, no LLM call."""
        started = time.perf_counter()
        await self._check_cancellation(run.id, context)
        async with self._uow_factory() as uow:
            references = await uow.production_artifacts.get_current(run.id, "references")
            extraction = await uow.production_artifacts.get_current(run.id, "extraction")
            synthesis = await uow.production_artifacts.get_current(run.id, "synthesis")

            if references is None or extraction is None or synthesis is None:
                return {
                    "stage": "assembly",
                    "status": "error",
                    "error": "Missing upstream artifacts",
                }

            repair_marker = None
            for candidate in (synthesis, extraction):
                candidate_marker = (
                    candidate.metadata.get("repair_materialization")
                    if isinstance(candidate.metadata, dict)
                    else None
                )
                if isinstance(candidate_marker, dict):
                    repair_marker = dict(candidate_marker)
                    break

            subject_title, _ = await self._subject_context(uow, run.subject_id, snapshot)
            archived_urls = {
                item.canonical_url
                for item in await uow.source_collections.list_for_subject(run.subject_id)
                if item.state.value in _ARCHIVED_STATES
            }
            publication = await self._assembly.assemble_publication(
                run_id=run.id,
                subject_id=run.subject_id,
                subject_title=subject_title,
                references_artifact=references,
                extraction_artifact=extraction,
                synthesis_artifact=synthesis,
                metadata_extra=(
                    {"repair_materialization": repair_marker} if repair_marker is not None else None
                ),
            )

            if repair_marker is not None:
                decision_ids = repair_marker.get("decision_ids", ())
                affected_outputs = repair_marker.get("affected_outputs", ())
                self._diagnostics.record(
                    event="production.repair.publication_reassembled",
                    run_id=run.id,
                    subject_id=run.subject_id,
                    stage="assembly",
                    impact_kind=repair_marker.get("impact_kind", "unknown"),
                    decision_count=len(decision_ids) if isinstance(decision_ids, list) else 0,
                    affected_outputs=(
                        sorted(str(output) for output in affected_outputs)
                        if isinstance(affected_outputs, list)
                        else []
                    ),
                    model_call_required=bool(repair_marker.get("model_call_required", False)),
                    reused_synthesis=bool(repair_marker.get("reused_synthesis_artifact_id")),
                    duration_ms=max(0, round((time.perf_counter() - started) * 1000)),
                )

            # QA reads the real payloads, not the counters.
            qa_inputs = await self._load_qa_inputs(references, extraction, synthesis, publication)
            qa_result = await self._qa.run_qa(
                run_id=run.id,
                references_artifact=references,
                extraction_artifact=extraction,
                synthesis_artifact=synthesis,
                publication_artifact=publication,
                archived_urls=archived_urls,
                research_date=run.research_date,
                **qa_inputs,
            )

            await self._check_cancellation(run.id, context)
            ending = await uow.production_runs.get_for_update(run.id)
            if ending is None or ending.status is ProductionRunStatus.CANCELLED:
                return {
                    "stage": "assembly",
                    "status": "cancelled",
                }

            if qa_result["passed"]:
                ending.mark_ready(now=datetime.now(UTC))
                await uow.production_runs.save(ending)
                await uow.commit()

                return {
                    "stage": "assembly",
                    "status": "success",
                    "run_status": ProductionRunStatus.READY.value,
                    "qa": qa_result,
                }
            else:
                ending.mark_needs_review(
                    code="qa_failed",
                    message="; ".join(qa_result["errors"]),
                    details=qa_result,
                    now=datetime.now(UTC),
                )
                await uow.production_runs.save(ending)
                await uow.commit()

                return {
                    "stage": "assembly",
                    "status": "needs_review",
                    "run_status": ProductionRunStatus.NEEDS_REVIEW.value,
                    "qa": qa_result,
                }


def _references_input_hash(
    *,
    snapshot: ProductionInputSnapshot,
    research_date: Any,
) -> str:
    """Functional Q1 identity: snapshot, research date and contract versions.

    Execution identities (run id, job id, model run id, provider, response or
    conversation id, pipeline generation) and technical timestamps are
    deliberately absent, so two runs of the same Subject compute one corpus and
    reuse it across runs.
    """
    return compute_input_hash(
        {
            "production_input_snapshot_hash": snapshot.input_hash,
            "research_date": str(research_date),
            "prompt_version": REFERENCES_PROMPT_VERSION,
            "parser_version": PRODUCTION_REFERENCE_PARSER_VERSION,
            "corpus_schema_version": PRODUCTION_REFERENCE_CORPUS_SCHEMA_VERSION,
            "routing_policy_version": REFERENCES_ROUTING_POLICY_VERSION,
            "stage": "references",
        }
    )


def production_references_model_run_id(
    production_run_id: UUID,
    references_input_hash: str,
) -> UUID:
    """Stable execution identity for one run's REFERENCES research submission.

    ``ModelRun`` is an execution identity, so the production run belongs here;
    it stays out of the functional corpus.
    """
    return uuid5(
        NAMESPACE_URL,
        f"production-references-model:{production_run_id}:{references_input_hash}",
    )


def _synthesis_input_hash(
    *,
    subject_id: UUID,
    references_hash: str,
    reference_report_hash: str,
    extraction_hash: str,
    technical_extraction_hash: str,
    synthesis_evidence_pack_hash: str,
    prompt_version: str = SYNTHESIS_PROMPT_VERSION,
    format_repair_version: str = SYNTHESIS_FORMAT_REPAIR_VERSION,
    web_policy_version: str = "q4-web-non-authoritative-v1",
    routing_policy_version: str = "openai-drafting-v1",
    revision_prompt_version: str = SYNTHESIS_REVISION_PROMPT_VERSION,
    previous_synthesis_content_hash: str | None = None,
    current_synthesis_semantic_hash: str | None = None,
) -> str:
    """Return the functional Q4 identity, excluding run and artifact IDs.

    The previous draft participates by content hash only.  Two immutable
    artifacts carrying identical draft text therefore have the same revision
    dependency.
    """
    return compute_input_hash(
        {
            "subject_id": str(subject_id),
            "references_hash": references_hash,
            "reference_report_hash": reference_report_hash,
            "extraction_hash": extraction_hash,
            "technical_extraction_hash": technical_extraction_hash,
            "synthesis_evidence_pack_version": SYNTHESIS_EVIDENCE_PACK_VERSION,
            "synthesis_evidence_pack_hash": synthesis_evidence_pack_hash,
            "prompt_version": prompt_version,
            "format_repair_version": format_repair_version,
            "web_policy_version": web_policy_version,
            "model_routing_policy": routing_policy_version,
            "revision_prompt_version": revision_prompt_version,
            "previous_synthesis_content_hash": previous_synthesis_content_hash,
            "current_synthesis_semantic_hash": current_synthesis_semantic_hash,
            "stage": "synthesis",
        }
    )
