"""Main production workflow orchestration service."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any
from uuid import NAMESPACE_URL, UUID, uuid5

from cti_app.application.analyst_vt_enrichment import VirusTotalSeedEnrichmentService
from cti_app.application.collection import SupplementalSource
from cti_app.application.diagnostics import DiagnosticsLog
from cti_app.application.iana_tlds_snapshot import IANA_TLD_SNAPSHOT_VERSION
from cti_app.application.jobs import JobCancelledError, JobExecutionContext
from cti_app.application.model_conversations import (
    ConversationTurnFailedError,
    ModelConversationService,
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
from cti_app.application.production_artifact_verification import (
    ARTIFACT_VERIFIER_VERSION,
    Q2ProposalSubmission,
    verify_q2_proposals,
)
from cti_app.application.production_context import build_subject_production_context
from cti_app.application.production_pacing import ProductionPacingPolicy
from cti_app.application.production_parsers import (
    PARSER_VERSION,
    Q2_MARKDOWN_PARSER_VERSION,
    IndicatorStatus,
    ParsedEvent,
    ParseResult,
    ReferenceReport,
    parse_q2_proposals_markdown,
    parse_reference_report,
    reference_report_from_json,
    reference_report_to_json,
    technical_extraction_from_json,
    technical_extraction_to_json,
    validate_synthesis,
)
from cti_app.application.production_prompts import (
    EXTRACTION_PROMPT_VERSION,
    REFERENCES_FORMAT_REPAIR_VERSION,
    REFERENCES_PROMPT_VERSION,
    SYNTHESIS_FORMAT_REPAIR_VERSION,
    SYNTHESIS_PROMPT_VERSION,
    ProductionPromptTemplates,
)
from cti_app.application.production_stages import (
    ExtractionService,
    ProductionQAService,
    PublicationAssemblyService,
    ReferenceResearchService,
    SynthesisService,
    compute_input_hash,
)
from cti_app.domain.collection import SourceOriginKind
from cti_app.domain.model_conversations import (
    ConversationMode,
    ConversationPolicy,
    ConversationPurpose,
    ConversationTransport,
    ModelConversation,
)
from cti_app.domain.model_runs import ModelProvider, ModelRole
from cti_app.domain.production import (
    ProductionArtifactStage,
    ProductionInputSnapshot,
    SubjectProductionRun,
    SubjectProductionStage,
    SubjectProductionStatus,
)

if TYPE_CHECKING:
    from cti_app.application.collection import SubjectCollectionService


# Collection states that count as "the source is available for analysis".
_ARCHIVED_STATES = {"archived", "extracted", "completed"}

# Version routing decision separately from prompt/schema: changing provider policy
# must produce a distinct persisted Q2 checkpoint.
# "3": Q2 is one direct, web-enabled ModelGateway request per Q1 source.
# Its deterministic ModelRun identity includes this routing policy, so changing
# that policy creates a fresh checkpoint without conversations or repair turns.
Q2_ROUTING_POLICY_VERSION = "3"
REFERENCES_ROUTING_POLICY_VERSION = "openai-web-research-v1"


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
_SOURCE_CONTENT_CODES = frozenset({"source_content_invalid"})


class _Q2FailureClass(StrEnum):
    GLOBAL_TRANSIENT_PRE_SUBMISSION = "global_transient_pre_submission"
    RECONCILIATION_REQUIRED = "reconciliation_required"
    SOURCE_CONTENT_FAILURE = "source_content_failure"
    CONTROL_INVARIANT_FAILURE = "control_invariant_failure"


@dataclass(frozen=True, slots=True)
class _Q2FailureClassification:
    failure_class: _Q2FailureClass
    status: str
    error_code: str
    retryable: bool
    phase: str
    submission_state: str
    contributes_to_coverage: bool


class _Q2SourceContentFailure(ValueError):
    code = "source_content_invalid"
    retryable = False
    phase = "response_validation"
    submission_state = "post_submission"


class _Q2ControlFailure(RuntimeError):
    code = "q2_provider_response_missing"
    retryable = False
    phase = "model_call"
    submission_state = "post_submission"


def _classify_q2_failure(
    exc: Exception,
    *,
    provider_response_produced: bool = False,
) -> _Q2FailureClassification:
    """Classify one Q2 source failure before it can affect coverage.

    The caller supplies only the fact that a provider response was available;
    submission safety remains the ModelGateway's responsibility.  In
    particular, a previously persisted ModelRun error is always a control or
    reconciliation outcome, never a source-content failure.
    """
    details = getattr(exc, "details", None)
    detail_state = details.get("submission_state") if isinstance(details, dict) else None
    detail_phase = details.get("phase") if isinstance(details, dict) else None
    code = str(getattr(exc, "code", "") or "")
    retryable = bool(getattr(exc, "retryable", False))
    phase = str(getattr(exc, "phase", None) or detail_phase or "model_call")[:64]
    submission_state = str(getattr(exc, "submission_state", None) or detail_state or "unknown")[:32]

    if code == _MODEL_SUBMISSION_RECONCILIATION_CODE:
        return _Q2FailureClassification(
            _Q2FailureClass.RECONCILIATION_REQUIRED,
            "needs_review",
            code,
            False,
            "reconciliation",
            submission_state,
            False,
        )

    # The HTTP client raises bridge_unreachable on a failed connection before
    # it can send bytes, so this code is a safe pre-submit transport signal
    # even though that low-level exception has no explicit state field.  The
    # gateway remains the authority that persists NOT_SUBMITTED.
    if (
        submission_state == "pre_submission"
        or (submission_state == "unknown" and code == "bridge_unreachable")
    ) and (retryable or code in _TRANSIENT_CODES):
        return _Q2FailureClassification(
            _Q2FailureClass.GLOBAL_TRANSIENT_PRE_SUBMISSION,
            "transient_error",
            code or "q2_global_transient",
            True,
            phase,
            submission_state,
            False,
        )

    # An explicit source-content code is accepted only after submission.  A
    # ModelGatewayError from a checkpoint state has no such proof and falls
    # through to the control/invariant class below.
    if (
        not retryable
        and code in _SOURCE_CONTENT_CODES
        and submission_state in {"post_submission", "submitted_or_unknown"}
    ) or (provider_response_produced and not retryable and not isinstance(exc, ModelGatewayError)):
        return _Q2FailureClassification(
            _Q2FailureClass.SOURCE_CONTENT_FAILURE,
            "source_failure",
            code if code in _SOURCE_CONTENT_CODES else "source_content_invalid",
            False,
            phase,
            submission_state,
            True,
        )

    if submission_state in {"submission_attempted", "submitted_or_unknown", "post_submission"} and (
        retryable or code in _TRANSIENT_CODES
    ):
        return _Q2FailureClassification(
            _Q2FailureClass.RECONCILIATION_REQUIRED,
            "needs_review",
            _MODEL_SUBMISSION_RECONCILIATION_CODE,
            False,
            "reconciliation",
            submission_state,
            False,
        )

    return _Q2FailureClassification(
        _Q2FailureClass.CONTROL_INVARIANT_FAILURE,
        "needs_review",
        code or "q2_control_failure",
        False,
        phase,
        submission_state,
        False,
    )


def _transient_or_terminal(stage: str, exc: Exception) -> dict[str, Any]:
    code = str(getattr(exc, "code", "") or "")
    retryable = bool(getattr(exc, "retryable", False))
    if isinstance(exc, ConversationTurnFailedError) and exc.status.value == "needs_review":
        status = "needs_review"
    elif code in _REVIEW_CODES:
        # The conversation is gone or busy: an operator has to look, but the
        # subject is not corrupted and the batch must keep moving.
        status = "needs_review"
    elif retryable or code in _TRANSIENT_CODES:
        status = "transient_error"
    else:
        status = "terminal_error"
    return {
        "stage": stage,
        "status": status,
        "error_code": code or f"{stage}_failed",
        "error": str(exc),
        "details": getattr(exc, "details", None),
    }


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
        self._collection_service = collection_service
        self._artifact_store = artifact_store
        self._diagnostics = diagnostics or DiagnosticsLog(None)
        self._correlation_id = "-"
        self._references = ReferenceResearchService(uow_factory, artifact_store)
        self._extraction = ExtractionService(uow_factory, artifact_store)
        self._synthesis = SynthesisService(uow_factory, artifact_store)
        self._assembly = PublicationAssemblyService(uow_factory, artifact_store)
        self._artifact_reuse = ProductionArtifactReuseService(
            uow_factory, artifact_store, self._diagnostics
        )
        self._qa = ProductionQAService(uow_factory)
        self._seed_enrichment = seed_enrichment
        self._pacing = pacing or ProductionPacingPolicy.zero()

    async def _check_cancellation(self, run_id: UUID, context: JobExecutionContext | None) -> None:
        """Fence both the job cancellation flag and the persistent run state."""
        if context is not None:
            await context.check_cancelled()
        uow_factory = getattr(self, "_uow_factory", None)
        if uow_factory is None:
            return
        async with uow_factory() as uow:
            runs = getattr(uow, "subject_production_runs", None)
            if runs is None:
                return
            run = await runs.get(run_id)
        if run is not None and run.status is SubjectProductionStatus.CANCELLED:
            raise JobCancelledError

    async def execute_stage(
        self,
        run_id: UUID,
        expected_stage: SubjectProductionStage,
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
            run = await uow.subject_production_runs.get(run_id)
            snapshot_repository = getattr(uow, "production_input_snapshots", None)
            snapshot = (
                await snapshot_repository.get_by_run(run.id)
                if run is not None and snapshot_repository is not None
                else None
            )
        if not run:
            raise ValueError(f"Production run {run_id} not found")

        await self._check_cancellation(run.id, context)

        if run.current_stage != expected_stage:
            raise ValueError(
                f"Run on stage {run.current_stage.value}, expected {expected_stage.value}"
            )

        try:
            if expected_stage == SubjectProductionStage.SOURCES:
                result = await self._execute_sources_stage(run, context, snapshot)
            elif expected_stage == SubjectProductionStage.REFERENCES:
                result = await self._execute_references_stage(run, context, snapshot)
            elif expected_stage == SubjectProductionStage.EXTRACTION:
                result = await self._execute_extraction_stage(run, context, snapshot)
            elif expected_stage == SubjectProductionStage.SYNTHESIS:
                result = await self._execute_synthesis_stage(run, context, snapshot)
            elif expected_stage == SubjectProductionStage.ASSEMBLY:
                result = await self._execute_assembly_stage(run, context, snapshot)
            else:
                raise ValueError(f"Unknown stage: {expected_stage.value}")
        except ProductionReuseStorageUnavailableError as exc:
            result = self._handle_stage_exception(run, expected_stage.value, exc)

        self._diagnostics.record_stage_outcome(
            run_id=run.id,
            subject_id=run.subject_id,
            stage=expected_stage.value,
            correlation_id=correlation_id,
            result=result,
        )
        return result

    def _handle_stage_exception(
        self, run: SubjectProductionRun, stage: str, exc: Exception
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
        run: SubjectProductionRun,
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

    async def _ask_with_format_repair(
        self,
        *,
        run: SubjectProductionRun,
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

        Used by Q1 (references) and Q4 (synthesis): both draft FRESH with web
        search, then repair CONTINUE without web search — the repair turn
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
            stage=stage, problems=result.errors
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

    async def _integrate_reference_sources(
        self,
        run: SubjectProductionRun,
        report: ReferenceReport,
        context: JobExecutionContext | None,
        snapshot: ProductionInputSnapshot | None = None,
    ) -> dict[str, Any]:
        """Attach, collect and archive the publications Q1 proposed.

        An event survives if at least one of its sources ended up archived; a
        URL already attached to the subject is reused, never re-downloaded.
        """
        warnings: list[str] = []
        new_sources = 0

        if self._collection_service is not None:
            supplemental = [
                SupplementalSource(
                    url=source.canonical_url,
                    title=source.title or None,
                    publisher=source.publisher,
                    published_at=source.published_at,
                    role=source.role,
                )
                for source in report.sources
            ]
            try:
                await self._check_cancellation(run.id, context)
                added = await self._collection_service.add_supplemental_sources(
                    run.subject_id, supplemental
                )
                new_sources = len(added)
                if context is not None:
                    report_urls = {source.canonical_url for source in report.sources}
                    for collection in await self._collection_service.list_sources(run.subject_id):
                        if (
                            collection.canonical_url not in report_urls
                            or collection.state.value in _ARCHIVED_STATES
                        ):
                            continue
                        try:
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
                                f"supplemental_collection_failed:{collection.canonical_url}:{exc}"
                            )
            except JobCancelledError:
                raise
            except Exception as exc:
                warnings.append(f"supplemental_collection_failed:{exc}")

        archived_urls: set[str] = set()
        async with self._uow_factory() as uow:
            for item in await uow.source_collections.list_for_subject(run.subject_id):
                if item.state.value in _ARCHIVED_STATES:
                    archived_urls.add(item.canonical_url)

        archived_ids = {
            source.local_id for source in report.sources if source.canonical_url in archived_urls
        }
        kept_events = []
        for event in report.events:
            backed = tuple(sid for sid in event.source_ids if sid in archived_ids)
            if not backed:
                warnings.append(f"event_without_archived_source_dropped:{event.local_id}")
                continue
            kept_events.append(
                ParsedEvent(
                    local_id=event.local_id,
                    event_date=event.event_date,
                    source_ids=backed,
                    text=event.text,
                )
            )

        kept_sources = tuple(source for source in report.sources if source.local_id in archived_ids)
        return {
            "report": ReferenceReport(
                sources=kept_sources,
                events=tuple(kept_events),
                uncertainties=report.uncertainties,
                editorial_title=report.editorial_title,
            ),
            "kept_events": kept_events,
            "warnings": warnings,
            "new_sources": new_sources,
            "archived_sources": len(archived_ids),
        }

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
            if references.canonical_blob_id is not None:
                loaded["report"] = reference_report_from_json(
                    await store.read_json(references.canonical_blob_id)
                )
            if extraction.canonical_blob_id is not None:
                loaded["extraction"] = technical_extraction_from_json(
                    await store.read_json(extraction.canonical_blob_id)
                )
            if synthesis.rendered_blob_id is not None:
                loaded["synthesis_text"] = await store.read_text(synthesis.rendered_blob_id)
            if publication.rendered_blob_id is not None:
                loaded["publication_markdown"] = await store.read_text(publication.rendered_blob_id)
        except Exception:
            return loaded
        return loaded

    async def _load_reference_report(self, artifact: Any) -> ReferenceReport | None:
        """Read back the canonical Q1 report stored with the artifact."""
        if self._artifact_store is None or artifact.canonical_blob_id is None:
            return None
        try:
            payload = await self._artifact_store.read_json(artifact.canonical_blob_id)
            return reference_report_from_json(payload)
        except (OSError, UnicodeError, ValueError):
            return None

    def _log_parse(self, run: SubjectProductionRun, stage: str, result: ParseResult[Any]) -> None:
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
        """Editorial title and context for a subject.

        `Subject` itself only carries identifiers; the human-readable title
        lives on the editorial group that selected it.
        """
        if snapshot is not None:
            return snapshot.subject_title, snapshot.subject_description
        group = await uow.editorial_groups.get_by_subject(subject_id)
        if group is None:
            return str(subject_id), ""
        return group.title, group.grouping_justification

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

    async def _open_conversation(
        self, run: SubjectProductionRun, subject_title: str, purpose: ConversationPurpose
    ) -> ModelConversation:
        assert self._model_service is not None
        return await self._model_service.create(
            provider=ModelProvider.OPENAI,
            transport=ConversationTransport.CHATGPT_BRIDGE,
            purpose=purpose,
            title=(
                f"Production research — {subject_title}"
                if purpose is ConversationPurpose.SUBJECT_RESEARCH
                else f"Production synthesis — {subject_title}"
            ),
            edition_id=run.edition_id,
            subject_id=run.subject_id,
            expected_profile=None,
            requested_model=None,
        )

    async def _execute_sources_stage(
        self,
        run: SubjectProductionRun,
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
            return {
                "stage": "sources",
                "status": "error",
                "error_code": str(getattr(e, "code", "") or "sources_error"),
                "error": str(e),
                "details": getattr(e, "details", None),
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
        run: SubjectProductionRun,
        context: JobExecutionContext | None = None,
        snapshot: ProductionInputSnapshot | None = None,
    ) -> dict[str, Any]:
        await self._check_cancellation(run.id, context)
        async with self._uow_factory() as uow:
            research_date = run.research_date or datetime.now(UTC).date()
            ctx = await build_subject_production_context(
                uow,
                run.subject_id,
                research_date,
                snapshot=snapshot,
                relevant_source_urls=None,
            )
            subject_title = ctx.subject_title

            input_hash = _references_input_hash(
                subject_id=run.subject_id,
                snapshot=snapshot,
                subject_title=subject_title,
                subject_description=ctx.subject_description,
                research_date=research_date,
            )
            reused = await self._reuse_artifact(run, "references", input_hash)
            if reused is not None:
                return reused

            if not self._model_service:
                return {
                    "stage": "references",
                    "status": "error",
                    "error": "ModelConversationService not configured",
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

            # Q1 has its own research conversation; Q2 remains stateless.
            if not run.references_conversation_id:
                conversation = await self._open_conversation(
                    run, subject_title, ConversationPurpose.SUBJECT_RESEARCH
                )
                run.references_conversation_id = conversation.id
                persisted = await uow.subject_production_runs.get_for_update(run.id)
                if persisted is not None:
                    persisted.references_conversation_id = conversation.id
                    await uow.subject_production_runs.save(persisted)
                    await uow.commit()

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

            try:
                parsed, raw, turn_id, _ = await self._ask_with_format_repair(
                    run=run,
                    conversation_id=run.references_conversation_id,
                    stage="references",
                    prompt=prompt,
                    prompt_version=REFERENCES_PROMPT_VERSION,
                    repair_version=REFERENCES_FORMAT_REPAIR_VERSION,
                    mode=ConversationMode.FRESH,
                    parse=lambda text: parse_reference_report(text, research_date),
                    external_llm_allowed=ctx.external_llm_allowed,
                    web_search=True,
                    request_identity=f"g{run.pipeline_generation}",
                    context=context,
                )
            except JobCancelledError:
                raise
            except Exception as e:
                await self._check_cancellation(run.id, context)
                return self._handle_stage_exception(run, "references", e)

            if parsed is None:
                return {
                    "stage": "references",
                    "status": "needs_review",
                    "error_code": "no_model_response",
                    "error": "No response from model",
                }
            if not parsed.usable:
                # A format the parser cannot read is a review case, never a crash.
                return {
                    "stage": "references",
                    "status": "needs_review",
                    "error_code": "references_format_unusable",
                    "error": "; ".join(parsed.errors),
                    "warnings": parsed.warnings,
                }

            report = parsed.value
            assert report is not None

            # Order matters: the new publications must be attached, downloaded
            # and archived before Q1 is recorded, so extraction only ever sees
            # events backed by a source we actually hold.
            await self._check_cancellation(run.id, context)
            integration = await self._integrate_reference_sources(run, report, context, snapshot)
            parsed.warnings.extend(integration["warnings"])
            if not integration["kept_events"]:
                return {
                    "stage": "references",
                    "status": "needs_review",
                    "error_code": "no_event_with_archived_source",
                    "error": "No chronological event is backed by an archived source",
                    "warnings": parsed.warnings,
                }
            report = integration["report"]

            await self._check_cancellation(run.id, context)
            artifact = await self._references.store_references_result(
                run_id=run.id,
                subject_id=run.subject_id,
                input_hash=input_hash,
                raw_result=raw,
                canonical_json=reference_report_to_json(report),
                conversation_turn_id=turn_id,
                warnings=parsed.warnings,
            )

            return {
                "stage": "references",
                "status": "success",
                "artifact_id": str(artifact.id),
                "sources_count": len(report.sources),
                "events_count": len(report.events),
                "new_sources": integration["new_sources"],
                "archived_sources": integration["archived_sources"],
                "warnings": parsed.warnings,
                "repair_actions": parsed.repair_actions,
            }

    async def _execute_extraction_stage(
        self,
        run: SubjectProductionRun,
        context: JobExecutionContext | None = None,
        snapshot: ProductionInputSnapshot | None = None,
    ) -> dict[str, Any]:
        return await self._execute_direct_url_extraction(run, context, snapshot)

    async def _execute_direct_url_extraction(
        self,
        run: SubjectProductionRun,
        context: JobExecutionContext | None = None,
        snapshot: ProductionInputSnapshot | None = None,
    ) -> dict[str, Any]:
        """Q2: exactly one fresh, web-enabled model request per Q1 source."""
        await self._check_cancellation(run.id, context)
        async with self._uow_factory() as uow:
            references = await uow.production_artifacts.get_current(run.id, "references")
            if references is None:
                return {
                    "stage": "extraction",
                    "status": "error",
                    "error": "References artifact not found",
                }
            report = await self._load_reference_report(references)
            if report is None:
                return {
                    "stage": "extraction",
                    "status": "terminal_error",
                    "error_code": "references_payload_missing",
                    "error": "Reference report content is not readable",
                }
            research_date = run.research_date or datetime.now(UTC).date()
            policy = await build_subject_production_context(
                uow,
                run.subject_id,
                research_date,
                snapshot=snapshot,
                relevant_source_urls={source.canonical_url for source in report.sources},
            )
            input_hash = _extraction_input_hash(
                subject_id=run.subject_id,
                references_hash=references.input_hash,
                source_urls=[source.canonical_url for source in report.sources],
                references_payload_hash=compute_input_hash(reference_report_to_json(report)),
            )
            reused = await self._reuse_artifact(run, "extraction", input_hash)
            if reused is not None:
                return reused
            if self._model_gateway is None:
                return {
                    "stage": "extraction",
                    "status": "error",
                    "error": "ModelGateway not configured",
                }
            if not policy.external_llm_allowed:
                return {
                    "stage": "extraction",
                    "status": "needs_review",
                    "error_code": "external_llm_blocked",
                    "error": "Diffusion policy forbids sending this subject to an external model",
                }
            subject_title, _ = await self._subject_context(uow, run.subject_id, snapshot)

        submissions: list[Q2ProposalSubmission] = []
        url_raw_parts: list[str] = []
        warnings: list[str] = []
        completed: list[str] = []
        failed: list[str] = []
        failed_attempts: list[str] = []
        failures: dict[str, dict[str, Any]] = {}
        for source_index, source in enumerate(report.sources):
            if source_index:
                # Q2 stays one request per source; pacing is between requests,
                # never before the first source.
                await self._check_cancellation(run.id, context)
                await asyncio.sleep(self._pacing.model_delay_seconds())
            await self._check_cancellation(run.id, context)
            prompt = ProductionPromptTemplates.get_extraction_prompt(
                subject_title, source.local_id, source.title, source.canonical_url
            )
            model_run_id = _q2_source_model_run_id(
                production_run_id=run.id,
                pipeline_generation=run.pipeline_generation,
                source_id=source.local_id,
                canonical_url=source.canonical_url,
            )
            self._diagnostics.record(
                event="q2.source.started",
                run_id=run.id,
                subject_id=run.subject_id,
                stage="extraction",
                correlation_id=self._correlation_id,
                pipeline_generation=run.pipeline_generation,
                source_id=source.local_id,
                source_url=source.canonical_url,
                model_run_id=str(model_run_id),
                web_search=True,
            )
            started_at = time.monotonic()
            raw = ""
            try:
                execution = await self._model_gateway.execute(
                    ModelRequest(
                        text=prompt,
                        prompt_template_id="production-q2-url",
                        prompt_template_version=EXTRACTION_PROMPT_VERSION,
                        evidence_pack_hash=hashlib.sha256(prompt.encode()).hexdigest(),
                        external_llm_allowed=True,
                        routing_hint=ModelRoutingHint.WEB_RESEARCH,
                        provider=ModelProvider.OPENAI,
                        web_search=True,
                        run_id=model_run_id,
                        # The deterministic Q2 checkpoint is deliberately
                        # retried across technical job attempts.  The
                        # gateway still decides whether FAILED is safe.
                        allow_failed_resubmit=True,
                        metadata={
                            "subject_id": str(run.subject_id),
                            "source_id": source.local_id,
                            "source_url": source.canonical_url,
                            "pipeline_generation": run.pipeline_generation,
                        },
                    ),
                    ModelRole.RESEARCH,
                )
                await self._check_cancellation(run.id, context)
                raw = execution.output_text or ""
                if not raw:
                    raise _Q2ControlFailure("Provider returned no Q2 response")
                parsed = parse_q2_proposals_markdown(raw)
                self._log_parse(run, "extraction", parsed)
                if not parsed.usable or parsed.value is None:
                    raise _Q2SourceContentFailure("; ".join(parsed.errors) or "source_unavailable")
                submissions.append(
                    Q2ProposalSubmission(
                        output=parsed.value,
                        source_ids=(source.local_id,),
                        model_run_id=str(execution.run.id),
                    )
                )
                completed.append(source.local_id)
                url_raw_parts.append(raw)
                warnings.extend(parsed.warnings)
                self._diagnostics.record(
                    event="q2.source.completed",
                    run_id=run.id,
                    subject_id=run.subject_id,
                    stage="extraction",
                    correlation_id=self._correlation_id,
                    source_id=source.local_id,
                    model_run_id=str(model_run_id),
                    answer_chars=len(raw),
                    facts_count=len(parsed.value.facts),
                    artifacts_count=len(parsed.value.artifacts),
                    duration_ms=int((time.monotonic() - started_at) * 1000),
                )
            except JobCancelledError:
                raise
            except Exception as exc:
                await self._check_cancellation(run.id, context)
                classification = _classify_q2_failure(
                    exc,
                    provider_response_produced=bool(raw),
                )
                error = str(exc)[:1000]
                duration_ms = int((time.monotonic() - started_at) * 1000)
                failed_attempts.append(source.local_id)
                if classification.contributes_to_coverage:
                    failed.append(source.local_id)
                failures[source.local_id] = {
                    "model_run_id": str(model_run_id),
                    "source_url": source.canonical_url,
                    "error_code": classification.error_code,
                    "error": error,
                    "retryable": classification.retryable,
                    "phase": classification.phase,
                    "submission_state": classification.submission_state,
                    "failure_class": classification.failure_class.value,
                    "contributes_to_coverage": classification.contributes_to_coverage,
                    "duration_ms": duration_ms,
                }
                self._diagnostics.record(
                    event="q2.source.failed",
                    run_id=run.id,
                    subject_id=run.subject_id,
                    stage="extraction",
                    correlation_id=self._correlation_id,
                    source_id=source.local_id,
                    source_url=source.canonical_url,
                    model_run_id=str(model_run_id),
                    error_code=classification.error_code,
                    error=error,
                    retryable=classification.retryable,
                    phase=classification.phase,
                    submission_state=classification.submission_state,
                    failure_class=classification.failure_class.value,
                    duration_ms=duration_ms,
                )
                # Transport, reconciliation, and control failures describe the
                # request/checkpoint path, not source content. Stop Q2 at once
                # so S2+ cannot turn an infrastructure failure into coverage.
                if classification.failure_class is _Q2FailureClass.GLOBAL_TRANSIENT_PRE_SUBMISSION:
                    return {
                        "stage": "extraction",
                        "status": "transient_error",
                        "error_code": classification.error_code,
                        "error": error,
                        "details": {
                            "completed_source_ids": completed,
                            "failed_source_ids": failed_attempts,
                            "source_failures": failures,
                            "failure_class": classification.failure_class.value,
                        },
                        "completed_source_ids": completed,
                        "failed_source_ids": failed_attempts,
                        "source_failures": failures,
                    }
                if classification.failure_class in {
                    _Q2FailureClass.RECONCILIATION_REQUIRED,
                    _Q2FailureClass.CONTROL_INVARIANT_FAILURE,
                }:
                    return {
                        "stage": "extraction",
                        "status": classification.status,
                        "error_code": classification.error_code,
                        "error": error,
                        "details": {
                            "completed_source_ids": completed,
                            "failed_source_ids": failed_attempts,
                            "source_failures": failures,
                            "failure_class": classification.failure_class.value,
                        },
                        "completed_source_ids": completed,
                        "failed_source_ids": failed_attempts,
                        "source_failures": failures,
                    }
        if failed:
            return {
                "stage": "extraction",
                "status": "needs_review",
                "error_code": "q2_source_coverage_failed",
                "error": "One or more Q1 sources could not be analysed",
                "details": {
                    "completed_source_ids": completed,
                    "failed_source_ids": failed,
                    "source_failures": failures,
                },
                "completed_source_ids": completed,
                "failed_source_ids": failed,
                "source_failures": failures,
            }
        verification = verify_q2_proposals(submissions)
        extraction = verification.canonical
        status_totals = {
            status.value: sum(item.indicator_status is status for item in extraction.items)
            for status in IndicatorStatus
        }
        await self._check_cancellation(run.id, context)
        artifact = await self._extraction.store_extraction_result(
            run_id=run.id,
            subject_id=run.subject_id,
            input_hash=input_hash,
            raw_result="\n\n".join(url_raw_parts),
            canonical_json=technical_extraction_to_json(extraction),
            warnings=[
                *warnings,
                *verification.warnings,
                *(f"q2_rejected:{item.reason_code}" for item in verification.rejected),
            ],
            verification_diagnostics={
                "artifact_verifier_version": ARTIFACT_VERIFIER_VERSION,
                "iana_tld_snapshot_version": IANA_TLD_SNAPSHOT_VERSION,
                "completed_source_ids": completed,
                "failed_source_ids": failed,
                "q2_proposal_diagnostics": [
                    {
                        "status": item.status.value,
                        "proposal_index": item.proposal_index,
                        "proposal_kind": item.proposal_kind,
                        "artifact_type": item.artifact_type,
                        "value_hash": item.value_hash,
                        "reason_code": item.reason_code,
                    }
                    for item in verification.diagnostics
                ],
            },
        )
        return {
            "stage": "extraction",
            "status": "success",
            "artifact_id": str(artifact.id),
            "items_count": len(extraction.items),
            "supported_items": len(extraction.supported_items()),
            "status_totals": status_totals,
            "completed_source_ids": completed,
            "failed_source_ids": failed,
        }

    @staticmethod
    def _build_synthesis_evidence_pack(
        report: ReferenceReport,
        extraction: Any,
        source_tiers_by_url: dict[str, str],
    ) -> dict[str, Any]:
        """Deterministic Q4 input, stripped of operational/internal evidence.

        Q4 must write from the verified Q1/Q2 results, not from raw collection
        material. In particular, never expose source URLs or model IDs, or
        items explicitly kept out of publication.
        """
        items: list[dict[str, Any]] = []
        for item in extraction.items:
            if (
                not item.supported
                or item.indicator_status is IndicatorStatus.EXCLUDED
                or item.display_policy.value == "hidden"
            ):
                continue
            published: dict[str, Any] = {
                "category": item.category,
                "context": item.context,
                "source_ids": sorted(item.source_ids),
                "indicator_status": item.indicator_status.value,
                "display_policy": item.display_policy.value,
                "artifact_type": item.artifact_type.value if item.artifact_type else None,
            }
            # Precise indicators are publishable in prose only with BOTH.
            if item.artifact_type is None or item.display_policy.value == "both":
                published["value"] = item.value
            items.append(published)

        return {
            "version": "2",
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
                "items": sorted(
                    items,
                    key=lambda item: (
                        item["category"],
                        item.get("value", ""),
                        item["context"],
                    ),
                ),
                "uncertainties": sorted(extraction.uncertainties),
            },
        }

    async def _execute_synthesis_stage(
        self,
        run: SubjectProductionRun,
        context: JobExecutionContext | None = None,
        snapshot: ProductionInputSnapshot | None = None,
    ) -> dict[str, Any]:
        await self._check_cancellation(run.id, context)
        async with self._uow_factory() as uow:
            extraction = await uow.production_artifacts.get_current(run.id, "extraction")
            references = await uow.production_artifacts.get_current(run.id, "references")
            if not extraction:
                return {
                    "stage": "synthesis",
                    "status": "error",
                    "error": "Extraction artifact not found",
                }
            if references is None:
                return {
                    "stage": "synthesis",
                    "status": "error",
                    "error": "References artifact not found",
                }

            report = await self._load_reference_report(references)
            if (
                report is None
                or self._artifact_store is None
                or extraction.canonical_blob_id is None
            ):
                return {
                    "stage": "synthesis",
                    "status": "terminal_error",
                    "error_code": "synthesis_inputs_missing",
                    "error": "Reference or extraction payload is not readable",
                }
            synthesis_research_date = run.research_date or datetime.now(UTC).date()
            synthesis_ctx = await build_subject_production_context(
                uow,
                run.subject_id,
                synthesis_research_date,
                snapshot=snapshot,
                relevant_source_urls={source.canonical_url for source in report.sources},
            )
            synthesis_policy_allows = synthesis_ctx.external_llm_allowed
            extraction_payload = technical_extraction_from_json(
                await self._artifact_store.read_json(extraction.canonical_blob_id)
            )
            subject_title, _ = await self._subject_context(uow, run.subject_id, snapshot)
            collections = await uow.source_collections.list_for_subject(run.subject_id)
            source_tiers_by_url: dict[str, str] = {}
            if snapshot is not None:
                core_urls = {source.canonical_url for source in snapshot.core_sources}
                relevant_urls = {source.canonical_url for source in report.sources}
                source_tiers_by_url.update({url: "core" for url in core_urls})
                source_tiers_by_url.update({url: "supporting" for url in relevant_urls - core_urls})
            else:
                for collection in collections:
                    if collection.origin_kind in {
                        SourceOriginKind.DISCOVERY,
                        SourceOriginKind.MANUAL,
                    }:
                        source_tiers_by_url[collection.canonical_url] = "core"
                    elif collection.origin_kind is SourceOriginKind.REFERENCE_RESEARCH:
                        source_tiers_by_url[collection.canonical_url] = "supporting"
            synthesis_pack = self._build_synthesis_evidence_pack(
                report, extraction_payload, source_tiers_by_url
            )
            input_hash = _synthesis_input_hash(
                subject_id=run.subject_id,
                references_hash=references.input_hash,
                reference_report_hash=compute_input_hash(reference_report_to_json(report)),
                extraction_hash=extraction.input_hash,
                technical_extraction_hash=compute_input_hash(
                    technical_extraction_to_json(extraction_payload)
                ),
                synthesis_evidence_pack_hash=compute_input_hash(synthesis_pack),
            )
            reused = await self._reuse_artifact(run, "synthesis", input_hash)
            if reused is not None:
                return reused
            if not self._model_service:
                return {
                    "stage": "synthesis",
                    "status": "error",
                    "error": "ModelConversationService not configured",
                }
            if not synthesis_policy_allows:
                return {
                    "stage": "synthesis",
                    "status": "needs_review",
                    "error_code": "external_llm_blocked",
                    "error": "Diffusion policy forbids sending this subject to an external model",
                }

            if run.synthesis_conversation_id is None:
                conversation = await self._open_conversation(
                    run, subject_title, ConversationPurpose.DRAFTING
                )
                run.synthesis_conversation_id = conversation.id
                persisted = await uow.subject_production_runs.get_for_update(run.id)
                if persisted is not None:
                    persisted.synthesis_conversation_id = conversation.id
                    await uow.subject_production_runs.save(persisted)
                    await uow.commit()
            prompt = ProductionPromptTemplates.get_synthesis_prompt(
                subject_title=subject_title,
                synthesis_evidence_pack=json.dumps(
                    synthesis_pack,
                    ensure_ascii=False,
                    sort_keys=True,
                ),
            )

            try:
                parsed, output_text, turn_id, _ = await self._ask_with_format_repair(
                    run=run,
                    conversation_id=run.synthesis_conversation_id,
                    stage="synthesis",
                    prompt=prompt,
                    prompt_version=SYNTHESIS_PROMPT_VERSION,
                    repair_version=SYNTHESIS_FORMAT_REPAIR_VERSION,
                    mode=ConversationMode.FRESH,
                    parse=lambda text: validate_synthesis(text, report, extraction_payload),
                    external_llm_allowed=synthesis_policy_allows,
                    web_search=True,
                    request_identity=f"g{run.pipeline_generation}",
                    context=context,
                )
                await self._check_cancellation(run.id, context)
                if parsed is None:
                    return {
                        "stage": "synthesis",
                        "status": "needs_review",
                        "error_code": "no_model_response",
                        "error": "No response from model",
                    }
                if not parsed.usable:
                    return {
                        "stage": "synthesis",
                        "status": "needs_review",
                        "error_code": "synthesis_validation_failed",
                        "error": "; ".join(parsed.errors),
                        "details": {
                            "violations": [
                                {
                                    "code": violation.code,
                                    "detail": violation.detail,
                                    "span": violation.span,
                                }
                                for violation in parsed.violations
                            ],
                            "repair_actions": parsed.repair_actions,
                        },
                        "violations": [
                            {
                                "code": violation.code,
                                "detail": violation.detail,
                                "span": violation.span,
                            }
                            for violation in parsed.violations
                        ],
                        "repair_actions": parsed.repair_actions,
                    }

                source_tiers_by_id = {
                    source["id"]: source["tier"]
                    for source in synthesis_pack["reference_report"]["sources"]
                }
                citation_counts = {"core": 0, "supporting": 0, "unknown": 0}
                for source_id in re.findall(r"\[S(\d+)\]", output_text):
                    citation_counts[source_tiers_by_id.get(f"S{source_id}", "unknown")] += 1
                artifact = await self._synthesis.store_synthesis_result(
                    run_id=run.id,
                    subject_id=run.subject_id,
                    input_hash=input_hash,
                    raw_result=output_text,
                    markdown_content=output_text,
                    conversation_turn_id=turn_id,
                    diagnostics={
                        "core_citation_count": citation_counts["core"],
                        "supporting_citation_count": citation_counts["supporting"],
                        "unknown_citation_count": citation_counts["unknown"],
                    },
                )

                result = {
                    "stage": "synthesis",
                    "status": "success",
                    "artifact_id": str(artifact.id),
                    "word_count": len(output_text.split()),
                    "repair_actions": parsed.repair_actions,
                }
                return result
            except JobCancelledError:
                raise
            except Exception as e:
                await self._check_cancellation(run.id, context)
                return self._handle_stage_exception(run, "synthesis", e)

    async def _execute_assembly_stage(
        self,
        run: SubjectProductionRun,
        context: JobExecutionContext | None = None,
        snapshot: ProductionInputSnapshot | None = None,
    ) -> dict[str, Any]:
        """Deterministic: pure rendering from artifacts, no LLM call."""
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
            ending = await uow.subject_production_runs.get_for_update(run.id)
            if ending is None or ending.status is SubjectProductionStatus.CANCELLED:
                return {
                    "stage": "assembly",
                    "status": "cancelled",
                }

            if qa_result["passed"]:
                ending.mark_ready(now=datetime.now(UTC))
                await uow.subject_production_runs.save(ending)
                await uow.commit()

                return {
                    "stage": "assembly",
                    "status": "success",
                    "run_status": SubjectProductionStatus.READY.value,
                    "qa": qa_result,
                }
            else:
                ending.mark_needs_review(
                    code="qa_failed",
                    message="; ".join(qa_result["errors"]),
                    details=qa_result,
                    now=datetime.now(UTC),
                )
                await uow.subject_production_runs.save(ending)
                await uow.commit()

                return {
                    "stage": "assembly",
                    "status": "needs_review",
                    "run_status": SubjectProductionStatus.NEEDS_REVIEW.value,
                    "qa": qa_result,
                }


def _q2_source_model_run_id(
    *,
    production_run_id: UUID,
    pipeline_generation: int,
    source_id: str,
    canonical_url: str,
    prompt_version: str = EXTRACTION_PROMPT_VERSION,
    parser_version: str = Q2_MARKDOWN_PARSER_VERSION,
    provider: ModelProvider = ModelProvider.OPENAI,
) -> UUID:
    """Stable ModelRun identity for one Q1 source in a Q2 generation."""
    identity = json.dumps(
        {
            "production_run_id": str(production_run_id),
            "pipeline_generation": pipeline_generation,
            "source_id": source_id,
            "canonical_url": canonical_url,
            "prompt_version": prompt_version,
            "parser_version": parser_version,
            "routing_policy_version": Q2_ROUTING_POLICY_VERSION,
            "provider": provider.value,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return uuid5(NAMESPACE_URL, f"production-q2-source:{identity}")


def _references_input_hash(
    *,
    subject_id: UUID,
    snapshot: ProductionInputSnapshot | None,
    subject_title: str,
    subject_description: str,
    research_date: Any,
) -> str:
    """Functional Q1 identity; execution/run identities are deliberately absent."""
    snapshot_hash = (
        snapshot.input_hash
        if snapshot is not None
        else compute_input_hash(
            {
                "subject_id": str(subject_id),
                "title": subject_title,
                "description": subject_description,
                "research_date": str(research_date),
            }
        )
    )
    return compute_input_hash(
        {
            "subject_id": str(subject_id),
            "production_input_snapshot_hash": snapshot_hash,
            "prompt_version": REFERENCES_PROMPT_VERSION,
            "format_repair_version": REFERENCES_FORMAT_REPAIR_VERSION,
            "parser_version": PARSER_VERSION,
            "routing_policy_version": REFERENCES_ROUTING_POLICY_VERSION,
            "stage": "references",
        }
    )


def _extraction_input_hash(
    *,
    subject_id: UUID,
    references_hash: str,
    source_urls: list[str],
    references_payload_hash: str | None = None,
    pipeline_generation: int | None = None,
) -> str:
    """Q2 canonical identity, distinct from per-source model-run identities."""
    # ``pipeline_generation`` remains accepted for callers using the old
    # helper signature, but is intentionally not part of this hash.
    return compute_input_hash(
        {
            "subject_id": str(subject_id),
            "references_hash": references_hash,
            "references_payload_hash": references_payload_hash or references_hash,
            "source_urls": sorted(source_urls),
            "prompt_version": EXTRACTION_PROMPT_VERSION,
            "parser_version": Q2_MARKDOWN_PARSER_VERSION,
            "artifact_verifier_version": ARTIFACT_VERIFIER_VERSION,
            "iana_tld_snapshot_version": IANA_TLD_SNAPSHOT_VERSION,
            "routing_policy_version": Q2_ROUTING_POLICY_VERSION,
        }
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
) -> str:
    """Return the functional Q4 identity, excluding run and execution state."""
    return compute_input_hash(
        {
            "subject_id": str(subject_id),
            "references_hash": references_hash,
            "reference_report_hash": reference_report_hash,
            "extraction_hash": extraction_hash,
            "technical_extraction_hash": technical_extraction_hash,
            "synthesis_evidence_pack_version": "2",
            "synthesis_evidence_pack_hash": synthesis_evidence_pack_hash,
            "prompt_version": prompt_version,
            "format_repair_version": format_repair_version,
            "web_policy_version": web_policy_version,
            "model_routing_policy": routing_policy_version,
            "stage": "synthesis",
        }
    )
