"""Main production workflow orchestration service."""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import NAMESPACE_URL, UUID, uuid5

from cti_app.application.analyst_handoff import (
    AnalystHandoffPolicy,
    AnalystPostSynthesisService,
    loop_budget_from_settings,
)
from cti_app.application.analyst_vt_enrichment import VirusTotalSeedEnrichmentService
from cti_app.application.collection import SupplementalSource
from cti_app.application.diagnostics import DiagnosticsLog
from cti_app.application.iana_tlds_snapshot import IANA_TLD_SNAPSHOT_VERSION
from cti_app.application.jobs import JobExecutionContext
from cti_app.application.model_conversations import (
    ConversationTurnFailedError,
    ModelConversationService,
)
from cti_app.application.model_gateway import ModelGateway, ModelRequest, ModelRoutingHint
from cti_app.application.persistence import UnitOfWork, UnitOfWorkFactory
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.application.production_artifact_verification import (
    ARTIFACT_VERIFIER_VERSION,
    Q2ProposalSubmission,
    verify_q2_proposals,
)
from cti_app.application.production_context import build_subject_production_context
from cti_app.application.production_parsers import (
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
    BriefAssemblyService,
    ExtractionService,
    ProductionQAService,
    ReferenceResearchService,
    SynthesisService,
    compute_input_hash,
)
from cti_app.config import get_settings
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
    ProductionInputSnapshot,
    ProductionProfile,
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


# Bridge and network hiccups are worth retrying; anything else is a dead end
# for this attempt and must not silently burn the subject.
_TRANSIENT_CODES = {
    "bridge_server_error",
    "bridge_idle_timeout",
    "bridge_total_timeout",
    "bridge_timeout",
    "bridge_ui_timeout",
}

# The conversation itself is the problem, not the pipeline: retrying the same
# turn cannot help, but nothing is broken either.
_REVIEW_CODES = {
    "conversation_unavailable",
    "conversation_profile_mismatch",
    "conversation_busy",
    "external_llm_blocked",
}


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
    """Orchestrates the complete production workflow for supported profiles."""

    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        model_service: ModelConversationService | None = None,
        model_gateway: ModelGateway | None = None,
        collection_service: SubjectCollectionService | None = None,
        artifact_store: ProductionArtifactStore | None = None,
        diagnostics: DiagnosticsLog | None = None,
        seed_enrichment: VirusTotalSeedEnrichmentService | None = None,
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
        self._assembly = BriefAssemblyService(uow_factory, artifact_store)
        self._qa = ProductionQAService(uow_factory)
        self._seed_enrichment = seed_enrichment
        self._analyst_handoff = (
            AnalystPostSynthesisService(
                uow_factory,
                artifact_store,
                lambda: loop_budget_from_settings(get_settings()),
            )
            if artifact_store is not None
            else None
        )

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

        if run.current_stage != expected_stage:
            raise ValueError(
                f"Run on stage {run.current_stage.value}, expected {expected_stage.value}"
            )

        if expected_stage == SubjectProductionStage.SOURCES:
            result = await self._execute_sources_stage(run, context, snapshot)
        elif expected_stage == SubjectProductionStage.REFERENCES:
            result = await self._execute_references_stage(run, context, snapshot)
        elif expected_stage == SubjectProductionStage.EXTRACTION:
            result = await self._execute_extraction_stage(run, context, snapshot)
        elif expected_stage == SubjectProductionStage.SYNTHESIS:
            result = await self._execute_synthesis_stage(run, snapshot)
        elif expected_stage == SubjectProductionStage.ASSEMBLY:
            result = await self._execute_assembly_stage(run, snapshot)
        else:
            raise ValueError(f"Unknown stage: {expected_stage.value}")

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
                added = await self._collection_service.add_supplemental_sources(
                    run.subject_id, supplemental
                )
                new_sources = len(added)
                if added and context is not None:
                    await self._collection_service.collect_subject(
                        run.subject_id, context.job_id, context, snapshot=snapshot
                    )
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
        brief: Any,
    ) -> dict[str, Any]:
        """Read back what QA needs to judge the brief."""
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
            if brief.rendered_blob_id is not None:
                loaded["brief_markdown"] = await store.read_text(brief.rendered_blob_id)
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
        except Exception:
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

        try:
            await self._collection_service.collect_subject(
                run.subject_id,
                context.job_id,
                context,
                snapshot=snapshot,
            )
            sources = await self._collection_service.list_sources(run.subject_id)
        except Exception as e:
            return {
                "stage": "sources",
                "status": "error",
                "error_code": str(getattr(e, "code", "") or "sources_error"),
                "error": str(e),
                "details": getattr(e, "details", None),
            }

        archived = sum(1 for source in sources if source.state in _ARCHIVED_STATES)
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
        if not self._model_service:
            return {
                "stage": "references",
                "status": "error",
                "error": "ModelConversationService not configured",
            }

        async with self._uow_factory() as uow:
            research_date = run.research_date or datetime.now(UTC).date()
            ctx = await build_subject_production_context(
                uow, run.subject_id, research_date, snapshot=snapshot
            )
            subject_title = ctx.subject_title

            # The diffusion policy, not a hardcoded flag, decides whether this
            # subject may be sent to an external model.
            if not ctx.external_llm_allowed:
                return {
                    "stage": "references",
                    "status": "needs_review",
                    "error_code": "external_llm_blocked",
                    "error": "Diffusion policy forbids sending this subject to an external model",
                }

            input_data = {
                "subject_id": str(run.subject_id),
                "title": subject_title,
                "context": ctx.subject_description,
                "research_date": research_date.isoformat(),
                "stage": "references",
                "prompt_version": REFERENCES_PROMPT_VERSION,
                "pipeline_generation": run.pipeline_generation,
            }
            input_hash = compute_input_hash(input_data)

            existing = await uow.production_artifacts.get_current(run.id, "references")
            if existing and existing.input_hash == input_hash:
                return {
                    "stage": "references",
                    "status": "cached",
                    "artifact_id": str(existing.id),
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
                )
            except Exception as e:
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
        context: JobExecutionContext | None,
        snapshot: ProductionInputSnapshot | None = None,
    ) -> dict[str, Any]:
        return await self._execute_direct_url_extraction(run, snapshot)

    async def _execute_direct_url_extraction(
        self, run: SubjectProductionRun, snapshot: ProductionInputSnapshot | None = None
    ) -> dict[str, Any]:
        """Q2: exactly one fresh, web-enabled model request per Q1 source."""
        if self._model_gateway is None:
            return {
                "stage": "extraction",
                "status": "error",
                "error": "ModelGateway not configured",
            }
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
                uow, run.subject_id, research_date, snapshot=snapshot
            )
            if not policy.external_llm_allowed:
                return {
                    "stage": "extraction",
                    "status": "needs_review",
                    "error_code": "external_llm_blocked",
                    "error": "Diffusion policy forbids sending this subject to an external model",
                }
            input_hash = _extraction_input_hash(
                subject_id=run.subject_id,
                references_hash=references.input_hash,
                source_urls=[source.canonical_url for source in report.sources],
                pipeline_generation=run.pipeline_generation,
            )
            existing = await uow.production_artifacts.get_current(run.id, "extraction")
            if existing and existing.input_hash == input_hash:
                return {"stage": "extraction", "status": "cached", "artifact_id": str(existing.id)}
            subject_title, _ = await self._subject_context(uow, run.subject_id, snapshot)

        submissions: list[Q2ProposalSubmission] = []
        url_raw_parts: list[str] = []
        warnings: list[str] = []
        completed: list[str] = []
        failed: list[str] = []
        failures: dict[str, dict[str, str]] = {}
        for source in report.sources:
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
                        metadata={
                            "subject_id": str(run.subject_id),
                            "source_id": source.local_id,
                            "source_url": source.canonical_url,
                            "pipeline_generation": run.pipeline_generation,
                        },
                    ),
                    ModelRole.RESEARCH,
                )
                raw = execution.output_text or ""
                parsed = parse_q2_proposals_markdown(raw)
                self._log_parse(run, "extraction", parsed)
                if not parsed.usable or parsed.value is None:
                    raise ValueError("; ".join(parsed.errors) or "source_unavailable")
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
            except Exception as exc:
                error_code = str(getattr(exc, "code", "") or "q2_source_failed")
                error = str(exc)[:1000]
                failed.append(source.local_id)
                failures[source.local_id] = {
                    "model_run_id": str(model_run_id),
                    "error_code": error_code,
                    "error": error,
                }
                self._diagnostics.record(
                    event="q2.source.failed",
                    run_id=run.id,
                    subject_id=run.subject_id,
                    stage="extraction",
                    correlation_id=self._correlation_id,
                    source_id=source.local_id,
                    model_run_id=str(model_run_id),
                    error_code=error_code,
                    error=error,
                    duration_ms=int((time.monotonic() - started_at) * 1000),
                )
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
        self, run: SubjectProductionRun, snapshot: ProductionInputSnapshot | None = None
    ) -> dict[str, Any]:
        if not self._model_service:
            return {
                "stage": "synthesis",
                "status": "error",
                "error": "ModelConversationService not configured",
            }

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

            synthesis_research_date = run.research_date or datetime.now(UTC).date()
            synthesis_ctx = await build_subject_production_context(
                uow, run.subject_id, synthesis_research_date, snapshot=snapshot
            )
            synthesis_policy_allows = synthesis_ctx.external_llm_allowed
            if not synthesis_policy_allows:
                return {
                    "stage": "synthesis",
                    "status": "needs_review",
                    "error_code": "external_llm_blocked",
                    "error": "Diffusion policy forbids sending this subject to an external model",
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
            extraction_payload = technical_extraction_from_json(
                await self._artifact_store.read_json(extraction.canonical_blob_id)
            )
            subject_title, _ = await self._subject_context(uow, run.subject_id, snapshot)
            collections = await uow.source_collections.list_for_subject(run.subject_id)
            source_tiers_by_url: dict[str, str] = {}
            for collection in collections:
                if collection.origin_kind in {SourceOriginKind.DISCOVERY, SourceOriginKind.MANUAL}:
                    source_tiers_by_url[collection.canonical_url] = "core"
                elif collection.origin_kind is SourceOriginKind.REFERENCE_RESEARCH:
                    source_tiers_by_url[collection.canonical_url] = "supporting"
            synthesis_pack = self._build_synthesis_evidence_pack(
                report, extraction_payload, source_tiers_by_url
            )
            synthesis_pack_hash = compute_input_hash(synthesis_pack)
            input_hash = compute_input_hash(
                {
                    "subject_id": str(run.subject_id),
                    "references_version": references.version,
                    "references_hash": references.input_hash,
                    "reference_report_hash": compute_input_hash(reference_report_to_json(report)),
                    "extraction_version": extraction.version,
                    "extraction_hash": extraction.input_hash,
                    "technical_extraction_hash": compute_input_hash(
                        technical_extraction_to_json(extraction_payload)
                    ),
                    "synthesis_evidence_pack_version": "2",
                    "synthesis_evidence_pack_hash": synthesis_pack_hash,
                    "prompt_version": SYNTHESIS_PROMPT_VERSION,
                    "web_policy_version": "q4-web-non-authoritative-v1",
                    "model_routing_policy": "openai-drafting-v1",
                    "stage": "synthesis",
                    "pipeline_generation": run.pipeline_generation,
                }
            )
            existing = await uow.production_artifacts.get_current(run.id, "synthesis")
            if existing and existing.input_hash == input_hash:
                return {
                    "stage": "synthesis",
                    "status": "cached",
                    "artifact_id": str(existing.id),
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
                )
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

                analyst_handoff = await self._ensure_analyst_handoff(
                    run=run,
                    synthesis=artifact,
                    extraction=extraction,
                    extraction_payload=extraction_payload,
                    policy=synthesis_ctx,
                )

                result = {
                    "stage": "synthesis",
                    "status": "success",
                    "artifact_id": str(artifact.id),
                    "word_count": len(output_text.split()),
                    "repair_actions": parsed.repair_actions,
                }
                if analyst_handoff is not None:
                    result["analyst_investigation_id"] = str(analyst_handoff.investigation_id)
                    await self._enrich_analyst_handoff(run, synthesis_ctx, analyst_handoff)
                return result
            except Exception as e:
                return self._handle_stage_exception(run, "synthesis", e)

    async def _ensure_analyst_handoff(
        self,
        *,
        run: SubjectProductionRun,
        synthesis: Any,
        extraction: Any,
        extraction_payload: Any,
        policy: Any,
    ) -> Any | None:
        """Persist the major handoff; seed enrichment is deliberately separate."""
        if run.profile is not ProductionProfile.MAJOR_ASSISTED:
            return None
        if self._analyst_handoff is None:
            raise ValueError("Analyst input pack requires the production artifact store")
        return await self._analyst_handoff.ensure_for_verified_synthesis(
            run=run,
            synthesis=synthesis,
            extraction_artifacts=(extraction,),
            extraction_items=extraction_payload.items,
            policy=AnalystHandoffPolicy(
                tlp=getattr(policy, "tlp", None),
                do_not_submit=bool(getattr(policy, "do_not_submit", False)),
                external_llm_allowed=bool(getattr(policy, "external_llm_allowed", False)),
            ),
        )

    async def _enrich_analyst_handoff(
        self, run: SubjectProductionRun, policy: Any, handoff: Any
    ) -> None:
        if self._seed_enrichment is None:
            return
        for value in handoff.file_indicators:
            await self._seed_enrichment.enrich(
                value,
                subject_id=run.subject_id,
                external_lookup_allowed=bool(getattr(policy, "external_llm_allowed", False)),
                has_bytes=False,
                checkpoint_id=(
                    f"analyst-input-pack:{handoff.investigation_id}:{handoff.input_sha256}:{value}"
                ),
            )

    async def _execute_assembly_stage(
        self, run: SubjectProductionRun, snapshot: ProductionInputSnapshot | None = None
    ) -> dict[str, Any]:
        """Deterministic: pure rendering from artifacts, no LLM call."""
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
            brief = await self._assembly.assemble_brief(
                run_id=run.id,
                subject_id=run.subject_id,
                subject_title=subject_title,
                references_artifact=references,
                extraction_artifact=extraction,
                synthesis_artifact=synthesis,
            )

            # QA reads the real payloads, not the counters.
            qa_inputs = await self._load_qa_inputs(references, extraction, synthesis, brief)
            qa_result = await self._qa.run_qa(
                run_id=run.id,
                references_artifact=references,
                extraction_artifact=extraction,
                synthesis_artifact=synthesis,
                brief_artifact=brief,
                archived_urls=archived_urls,
                research_date=run.research_date,
                **qa_inputs,
            )

            if qa_result["passed"]:
                run.mark_ready(now=datetime.now(UTC))
                await uow.subject_production_runs.save(run)
                await uow.commit()

                return {
                    "stage": "assembly",
                    "status": "success",
                    "run_status": SubjectProductionStatus.READY.value,
                    "qa": qa_result,
                }
            else:
                run.mark_needs_review(
                    code="qa_failed",
                    message="; ".join(qa_result["errors"]),
                    details=qa_result,
                    now=datetime.now(UTC),
                )
                await uow.subject_production_runs.save(run)
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


def _extraction_input_hash(
    *,
    subject_id: UUID,
    references_hash: str,
    source_urls: list[str],
    pipeline_generation: int,
) -> str:
    """Q2 canonical-artifact identity, distinct from per-source model runs."""
    return compute_input_hash(
        {
            "subject_id": str(subject_id),
            "references_hash": references_hash,
            "source_urls": source_urls,
            "prompt_version": EXTRACTION_PROMPT_VERSION,
            "parser_version": Q2_MARKDOWN_PARSER_VERSION,
            "artifact_verifier_version": ARTIFACT_VERIFIER_VERSION,
            "iana_tld_snapshot_version": IANA_TLD_SNAPSHOT_VERSION,
            "routing_policy_version": Q2_ROUTING_POLICY_VERSION,
            "pipeline_generation": pipeline_generation,
        }
    )
