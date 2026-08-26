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

from cti_app.application.collection import ReferencedEvidence, SupplementalSource
from cti_app.application.diagnostics import DiagnosticsLog
from cti_app.application.iana_tlds_snapshot import IANA_TLD_SNAPSHOT_VERSION
from cti_app.application.jobs import JobExecutionContext
from cti_app.application.model_conversations import (
    ConversationTurnFailedError,
    ModelConversationService,
)
from cti_app.application.persistence import UnitOfWork, UnitOfWorkFactory
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.application.production_artifact_verification import (
    ARTIFACT_VERIFIER_VERSION,
    Q2ProposalSubmission,
    verify_q2_proposals,
)
from cti_app.application.production_context import build_subject_production_context
from cti_app.application.production_evidence_pack import (
    ArchivedCorpusDocument,
    ProductionEvidencePack,
    build_production_evidence_pack,
)
from cti_app.application.production_parsers import (
    Q2_MARKDOWN_PARSER_VERSION,
    Q2_SCHEMA_VERSION,
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
    EXTRACTION_FORMAT_REPAIR_VERSION,
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
from cti_app.application.source_evidence_processing import SourceEvidenceProcessingService
from cti_app.domain.model_conversations import (
    ConversationMode,
    ConversationPolicy,
    ConversationPurpose,
    ConversationTransport,
    ModelConversation,
)
from cti_app.domain.model_runs import ModelProvider
from cti_app.domain.production import (
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
# "2": Q2 moved off the OpenAI structured-output contract onto free-text GPT
# via the ChatGPT bridge + a permissive Markdown parser (P23.7). Qwen structured
# output remains available outside production/benchmark but is no longer the
# default Q2 provider.
Q2_ROUTING_POLICY_VERSION = "2"


# Bridge and network hiccups are worth retrying; anything else is a dead end
# for this attempt and must not silently burn the subject.
_TRANSIENT_CODES = {
    "bridge_server_error",
    "bridge_timeout",
    "bridge_ui_timeout",
}

# The conversation itself is the problem, not the pipeline: retrying the same
# turn cannot help, but nothing is broken either.
_REVIEW_CODES = {
    "conversation_unavailable",
    "conversation_locator_invalid",
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
    }


class ProductionWorkflowOrchestrator:
    """Orchestrates the complete brief_auto production workflow."""

    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        model_service: ModelConversationService | None = None,
        collection_service: SubjectCollectionService | None = None,
        artifact_store: ProductionArtifactStore | None = None,
        diagnostics: DiagnosticsLog | None = None,
        source_evidence_processor: SourceEvidenceProcessingService | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._model_service = model_service
        self._collection_service = collection_service
        self._artifact_store = artifact_store
        self._diagnostics = diagnostics or DiagnosticsLog(None)
        self._source_evidence_processor = source_evidence_processor
        self._correlation_id = "-"
        self._references = ReferenceResearchService(uow_factory, artifact_store)
        self._extraction = ExtractionService(uow_factory, artifact_store)
        self._synthesis = SynthesisService(uow_factory, artifact_store)
        self._assembly = BriefAssemblyService(uow_factory, artifact_store)
        self._qa = ProductionQAService(uow_factory)

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
        if not run:
            raise ValueError(f"Production run {run_id} not found")

        if run.current_stage != expected_stage:
            raise ValueError(
                f"Run on stage {run.current_stage.value}, expected {expected_stage.value}"
            )

        if expected_stage == SubjectProductionStage.SOURCES:
            result = await self._execute_sources_stage(run, context)
        elif expected_stage == SubjectProductionStage.REFERENCES:
            result = await self._execute_references_stage(run, context)
        elif expected_stage == SubjectProductionStage.EXTRACTION:
            result = await self._execute_extraction_stage(run, context)
        elif expected_stage == SubjectProductionStage.SYNTHESIS:
            result = await self._execute_synthesis_stage(run)
        elif expected_stage == SubjectProductionStage.ASSEMBLY:
            result = await self._execute_assembly_stage(run)
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
    ) -> tuple[Any | None, str, UUID | None]:
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
        self._last_model_run_id = getattr(turn, "model_run_id", None)
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
            return None, "", turn.id

        result = parse(raw)
        self._log_parse(run, stage, result)
        if result.usable:
            return result, raw, turn.id

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
        self._last_model_run_id = getattr(repair_turn, "model_run_id", None)
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
            return result, raw, turn.id

        repaired = parse(repaired_raw)
        self._log_parse(run, f"{stage}-repair", repaired)
        repaired.repair_actions.append(f"{stage}_format_repair")
        repaired.warnings.extend(result.errors)
        return repaired, repaired_raw, repair_turn.id

    async def _integrate_reference_sources(
        self,
        run: SubjectProductionRun,
        report: ReferenceReport,
        context: JobExecutionContext | None,
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
                        run.subject_id, context.job_id, context
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

    async def _subject_context(self, uow: UnitOfWork, subject_id: UUID) -> tuple[str, str]:
        """Editorial title and context for a subject.

        `Subject` itself only carries identifiers; the human-readable title
        lives on the editorial group that selected it.
        """
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
        self, run: SubjectProductionRun, context: JobExecutionContext | None = None
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
            )
            sources = await self._collection_service.list_sources(run.subject_id)
        except Exception as e:
            return {
                "stage": "sources",
                "status": "error",
                "error": str(e),
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
        self, run: SubjectProductionRun, context: JobExecutionContext | None = None
    ) -> dict[str, Any]:
        if not self._model_service:
            return {
                "stage": "references",
                "status": "error",
                "error": "ModelConversationService not configured",
            }

        async with self._uow_factory() as uow:
            research_date = run.research_date or datetime.now(UTC).date()
            ctx = await build_subject_production_context(uow, run.subject_id, research_date)
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
                existing_sources_text=ctx.existing_sources_text,
            )

            try:
                parsed, raw, turn_id = await self._ask_with_format_repair(
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
            integration = await self._integrate_reference_sources(run, report, context)
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
    ) -> dict[str, Any]:
        referenced_evidence = {"selected": 0, "added": 0}
        evidence_processing = None
        if self._source_evidence_processor is not None:
            links = await self._source_evidence_processor.select_referenced_evidence(run.subject_id)
            referenced_evidence["selected"] = len(links)
            if self._collection_service is not None and links:
                if context is None:
                    raise RuntimeError(
                        "Extraction with referenced evidence requires a persisted job context"
                    )
                children = await self._collection_service.add_referenced_evidence(
                    run.subject_id,
                    tuple(
                        ReferencedEvidence(
                            parent_source_collection_id=link.parent_source_collection_id,
                            url=link.url,
                            anchor_text=link.anchor_text,
                        )
                        for link in links
                    ),
                )
                referenced_evidence["added"] = len(children)
                for child in children:
                    await self._collection_service.archive_one(
                        child.id,
                        context.job_id,
                        context=context,
                    )
            evidence_processing = await self._source_evidence_processor.process_subject(
                run.subject_id
            )

        if not self._model_service:
            return {
                "stage": "extraction",
                "status": "error",
                "error": "ModelConversationService not configured",
            }

        async with self._uow_factory() as uow:
            references = await uow.production_artifacts.get_current(run.id, "references")
            if not references:
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
            evidence_pack = await self._build_production_evidence_pack(uow, run.subject_id, report)
            if evidence_pack.needs_review:
                return {
                    "stage": "extraction",
                    "status": "needs_review",
                    "error_code": evidence_pack.error_code,
                    "error": evidence_pack.error_message,
                    "pack_hash": evidence_pack.pack_hash,
                }
            input_data = {
                "subject_id": str(run.subject_id),
                "references_version": references.version,
                "references_hash": references.input_hash,
                "stage": "extraction",
                "prompt_version": EXTRACTION_PROMPT_VERSION,
                "q2_schema_version": Q2_SCHEMA_VERSION,
                "routing_policy_version": Q2_ROUTING_POLICY_VERSION,
                "evidence_pack_hash": evidence_pack.pack_hash,
                # Canonical Verification Cache identity: a change here forces the
                # canonical extraction artifact to be recomputed, but never forces
                # a new Q2 model call — that call is checkpointed independently
                # by _q2_logical_request_id, which does not include these.
                "artifact_verifier_version": ARTIFACT_VERIFIER_VERSION,
                "iana_tld_snapshot_version": IANA_TLD_SNAPSHOT_VERSION,
            }
            input_hash = compute_input_hash(input_data)

            research_date = run.research_date or datetime.now(UTC).date()
            ctx = await build_subject_production_context(uow, run.subject_id, research_date)
            policy_allows = ctx.external_llm_allowed
            if not policy_allows:
                return {
                    "stage": "extraction",
                    "status": "needs_review",
                    "error_code": "external_llm_blocked",
                    "error": "Diffusion policy forbids sending this subject to an external model",
                }

            existing = await uow.production_artifacts.get_current(run.id, "extraction")
            if existing and existing.input_hash == input_hash:
                return {
                    "stage": "extraction",
                    "status": "cached",
                    "artifact_id": str(existing.id),
                    "source_evidence_processing": (
                        evidence_processing.as_dict() if evidence_processing is not None else None
                    ),
                    "referenced_evidence": referenced_evidence,
                }

            subject_title, _ = await self._subject_context(uow, run.subject_id)
            prompt = ProductionPromptTemplates.get_extraction_prompt(subject_title=subject_title)
            try:
                if not evidence_pack.chunks:
                    return {
                        "stage": "extraction",
                        "status": "needs_review",
                        "error_code": "evidence_pack_empty",
                        "error": "ProductionEvidencePack contains no Q2 chunks",
                        "completed_chunk_ids": [],
                        "failed_chunk_ids": [],
                    }
                q2_submissions: list[Q2ProposalSubmission] = []
                parsed_warnings: list[str] = []
                raw_parts: list[str] = []
                turn_id = None
                completed_chunk_ids: list[str] = []
                failed_chunk_ids: list[str] = []
                chunk_failures: dict[str, str] = {}
                chunk_provenance: dict[str, dict[str, str | None]] = {}
                for chunk in evidence_pack.chunks:
                    logical_request_id = _q2_logical_request_id(
                        run_id=run.id,
                        subject_id=run.subject_id,
                        evidence_pack_hash=evidence_pack.pack_hash,
                        chunk_sha256=chunk.sha256,
                    )
                    conversation_id = _q2_chunk_conversation_id(logical_request_id)
                    # Idempotency keys are column-limited (255 chars); the full
                    # logical_request_id JSON does not fit, so a short digest of
                    # it stands in — it is exactly as version-sensitive.
                    chunk_identity = hashlib.sha256(logical_request_id.encode()).hexdigest()
                    chunk_prompt = (
                        prompt
                        + "\n\nCorpus Q2 — segment borné, uniquement données archivées.\n"
                        + _q2_chunk_source_context(chunk)
                        + "\n\n<TEXT_ARCHIVÉ>\n"
                        + chunk.text
                        + "\n</TEXT_ARCHIVÉ>"
                    )
                    self._diagnostics.record(
                        event="q2.chunk.started",
                        run_id=run.id,
                        subject_id=run.subject_id,
                        stage="extraction",
                        correlation_id=self._correlation_id,
                        chunk_id=chunk.chunk_id,
                        source_document_id=str(chunk.source_document_id),
                        chunk_chars=len(chunk.text),
                        provider=ModelProvider.OPENAI.value,
                        web_search=False,
                        prompt_version=EXTRACTION_PROMPT_VERSION,
                        parser_version=Q2_MARKDOWN_PARSER_VERSION,
                        logical_request_id=logical_request_id,
                    )
                    started_at = time.monotonic()
                    try:
                        conversation = await self._model_service.get_or_create(
                            conversation_id,
                            provider=ModelProvider.OPENAI,
                            transport=ConversationTransport.CHATGPT_BRIDGE,
                            purpose=ConversationPurpose.SUBJECT_RESEARCH,
                            title=(
                                f"Production Q2 extraction — {subject_title} — "
                                f"chunk {chunk.chunk_id}"
                            ),
                            edition_id=run.edition_id,
                            subject_id=run.subject_id,
                            expected_profile=None,
                            requested_model=None,
                        )
                        chunk_parsed, chunk_raw, chunk_turn_id = await self._ask_with_format_repair(
                            run=run,
                            conversation_id=conversation.id,
                            stage="extraction",
                            prompt=chunk_prompt,
                            prompt_version=EXTRACTION_PROMPT_VERSION,
                            repair_version=EXTRACTION_FORMAT_REPAIR_VERSION,
                            mode=ConversationMode.FRESH,
                            parse=parse_q2_proposals_markdown,
                            external_llm_allowed=policy_allows,
                            web_search=False,
                            request_identity=chunk_identity,
                            lifecycle_policy=ConversationPolicy.DELETE_ON_SUCCESS,
                        )
                    except Exception as exc:
                        failed_chunk_ids.append(chunk.chunk_id)
                        chunk_failures[chunk.chunk_id] = str(exc)
                        chunk_provenance[chunk.chunk_id] = {
                            "logical_request_id": logical_request_id,
                            "conversation_id": str(conversation_id),
                            "model_run_id": (
                                str(self._last_model_run_id) if self._last_model_run_id else None
                            ),
                            "recovery_action": "none",
                            "status": "failed",
                        }
                        self._diagnostics.record(
                            event="q2.chunk.failed",
                            run_id=run.id,
                            subject_id=run.subject_id,
                            stage="extraction",
                            correlation_id=self._correlation_id,
                            chunk_id=chunk.chunk_id,
                            error=str(exc),
                        )
                        continue
                    duration_ms = int((time.monotonic() - started_at) * 1000)
                    if chunk_parsed is None:
                        failed_chunk_ids.append(chunk.chunk_id)
                        chunk_failures[chunk.chunk_id] = "no response"
                        chunk_provenance[chunk.chunk_id] = {
                            "logical_request_id": logical_request_id,
                            "conversation_id": str(conversation_id),
                            "turn_id": str(chunk_turn_id) if chunk_turn_id else None,
                            "model_run_id": (
                                str(self._last_model_run_id) if self._last_model_run_id else None
                            ),
                            "provider": ModelProvider.OPENAI.value,
                            "recovery_action": "none",
                            "duration_ms": str(duration_ms),
                            "status": "no_response",
                        }
                        self._diagnostics.record(
                            event="q2.chunk.failed",
                            run_id=run.id,
                            subject_id=run.subject_id,
                            stage="extraction",
                            correlation_id=self._correlation_id,
                            chunk_id=chunk.chunk_id,
                            error="no_model_response",
                        )
                        continue
                    repaired = "extraction_format_repair" in chunk_parsed.repair_actions
                    self._diagnostics.record(
                        event="q2.chunk.answer",
                        run_id=run.id,
                        subject_id=run.subject_id,
                        stage="extraction",
                        correlation_id=self._correlation_id,
                        chunk_id=chunk.chunk_id,
                        answer_chars=len(chunk_raw),
                        parse_usable=chunk_parsed.usable,
                        facts_count=len(chunk_parsed.value.facts) if chunk_parsed.value else 0,
                        artifacts_count=(
                            len(chunk_parsed.value.artifacts) if chunk_parsed.value else 0
                        ),
                        warning_count=len(chunk_parsed.warnings),
                        structural_loss_count=len(chunk_parsed.errors),
                        repaired=repaired,
                        duration_ms=duration_ms,
                    )
                    chunk_provenance[chunk.chunk_id] = {
                        "logical_request_id": logical_request_id,
                        "conversation_id": str(conversation_id),
                        "turn_id": str(chunk_turn_id) if chunk_turn_id else None,
                        "model_run_id": (
                            str(self._last_model_run_id) if self._last_model_run_id else None
                        ),
                        "provider": ModelProvider.OPENAI.value,
                        "recovery_action": "repair" if repaired else "none",
                        "duration_ms": str(duration_ms),
                        "status": "succeeded" if chunk_parsed.usable else "unusable",
                    }
                    if not chunk_parsed.usable or chunk_parsed.value is None:
                        failed_chunk_ids.append(chunk.chunk_id)
                        chunk_failures[chunk.chunk_id] = "; ".join(chunk_parsed.errors) or (
                            "no response"
                        )
                        self._diagnostics.record(
                            event="q2.chunk.failed",
                            run_id=run.id,
                            subject_id=run.subject_id,
                            stage="extraction",
                            correlation_id=self._correlation_id,
                            chunk_id=chunk.chunk_id,
                            errors=chunk_parsed.errors,
                        )
                        continue
                    completed_chunk_ids.append(chunk.chunk_id)
                    q2_submissions.append(
                        Q2ProposalSubmission(
                            output=chunk_parsed.value,
                            source_document_id=str(chunk.source_document_id),
                            chunk_id=chunk.chunk_id,
                            source_ids=chunk.source_ids,
                            model_run_id=(
                                str(self._last_model_run_id) if self._last_model_run_id else None
                            ),
                        )
                    )
                    raw_parts.append(chunk_raw)
                    parsed_warnings.extend(chunk_parsed.warnings)
                    turn_id = chunk_turn_id
                    self._diagnostics.record(
                        event="q2.chunk.completed",
                        run_id=run.id,
                        subject_id=run.subject_id,
                        stage="extraction",
                        correlation_id=self._correlation_id,
                        chunk_id=chunk.chunk_id,
                    )
                if failed_chunk_ids:
                    self._diagnostics.record(
                        event="extraction.q2_chunk_coverage_failed",
                        run_id=run.id,
                        subject_id=run.subject_id,
                        stage="extraction",
                        correlation_id=self._correlation_id,
                        completed_chunk_ids=completed_chunk_ids,
                        failed_chunk_ids=failed_chunk_ids,
                        chunk_failures=chunk_failures,
                        chunk_provenance=chunk_provenance,
                    )
                    return {
                        "stage": "extraction",
                        "status": "needs_review",
                        "error_code": "q2_chunk_coverage_failed",
                        "error": "One or more Q2 chunks failed or were unparsable",
                        "completed_chunk_ids": completed_chunk_ids,
                        "failed_chunk_ids": failed_chunk_ids,
                        "chunk_failures": chunk_failures,
                        "chunk_provenance": chunk_provenance,
                    }
                verification = verify_q2_proposals(q2_submissions, evidence_pack)
                parsed = ParseResult(
                    value=verification.canonical,
                    warnings=[
                        *parsed_warnings,
                        *verification.warnings,
                        *(f"q2_rejected:{item.reason_code}" for item in verification.rejected),
                    ],
                )
                raw = "\n\n".join(raw_parts)
            except Exception as e:
                return self._handle_stage_exception(run, "extraction", e)

            if parsed is None:
                return {
                    "stage": "extraction",
                    "status": "needs_review",
                    "error_code": "no_model_response",
                    "error": "No response from model",
                }
            if not parsed.usable:
                return {
                    "stage": "extraction",
                    "status": "needs_review",
                    "error_code": "extraction_format_unusable",
                    "error": "; ".join(parsed.errors),
                    "warnings": parsed.warnings,
                }

            assert parsed.value is not None
            extraction = parsed.value
            status_totals = {
                status.value: sum(item.indicator_status is status for item in extraction.items)
                for status in IndicatorStatus
            }
            artifact = await self._extraction.store_extraction_result(
                run_id=run.id,
                subject_id=run.subject_id,
                input_hash=input_hash,
                raw_result=raw,
                canonical_json=technical_extraction_to_json(extraction),
                conversation_turn_id=turn_id,
                warnings=parsed.warnings,
                verification_diagnostics={
                    # Also carried in this artifact's input_hash; repeated here
                    # so a manual run inspection doesn't have to decode the hash.
                    "artifact_verifier_version": ARTIFACT_VERIFIER_VERSION,
                    "iana_tld_snapshot_version": IANA_TLD_SNAPSHOT_VERSION,
                    "status_totals": status_totals,
                    "evidence_pack_hash": evidence_pack.pack_hash,
                    "evidence_pack_chunk_ids": list(evidence_pack.chunk_ids),
                    "evidence_pack_source_document_ids": sorted(
                        {str(chunk.source_document_id) for chunk in evidence_pack.chunks}
                    ),
                    "evidence_pack_parser_versions": dict(evidence_pack.parser_versions),
                    "completed_chunk_ids": completed_chunk_ids,
                    "failed_chunk_ids": failed_chunk_ids,
                    "chunk_provenance": chunk_provenance,
                    "q2_proposal_diagnostics": [
                        {
                            "status": item.status.value,
                            "proposal_index": item.proposal_index,
                            "proposal_kind": item.proposal_kind,
                            "artifact_type": item.artifact_type,
                            "source_document_id": item.source_document_id,
                            "chunk_id": item.chunk_id,
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
                "warnings": parsed.warnings,
                "repair_actions": parsed.repair_actions,
                "status_totals": status_totals,
                "evidence_pack_hash": evidence_pack.pack_hash,
                "evidence_pack_chunk_ids": list(evidence_pack.chunk_ids),
                "evidence_pack_source_document_ids": sorted(
                    {str(chunk.source_document_id) for chunk in evidence_pack.chunks}
                ),
                "evidence_pack_parser_versions": dict(evidence_pack.parser_versions),
                "completed_chunk_ids": completed_chunk_ids,
                "failed_chunk_ids": failed_chunk_ids,
                "chunk_provenance": chunk_provenance,
                "source_evidence_processing": (
                    evidence_processing.as_dict() if evidence_processing is not None else None
                ),
                "referenced_evidence": referenced_evidence,
            }

    async def _build_production_evidence_pack(
        self, uow: UnitOfWork, subject_id: UUID, report: ReferenceReport
    ) -> ProductionEvidencePack:
        repositories = ("source_collections", "source_documents", "derived_artifacts")
        if not all(hasattr(uow, name) for name in repositories):
            return build_production_evidence_pack(report, ())
        collections = await uow.source_collections.list_for_subject(subject_id)
        documents = {
            document.id: document
            for document in await uow.source_documents.list_for_subject(subject_id)
        }
        artifact_ids = {
            collection.derived_artifact_id
            for collection in collections
            if collection.derived_artifact_id is not None
        }
        artifacts = {}
        for artifact_id in artifact_ids:
            artifact = await uow.derived_artifacts.get(artifact_id)
            if artifact is not None:
                artifacts[artifact.id] = artifact
        texts: dict[UUID, str] = {}
        if self._source_evidence_processor is not None:
            for artifact in artifacts.values():
                try:
                    texts[
                        artifact.text_blob_id
                    ] = await self._source_evidence_processor.read_derived_text(
                        artifact.text_blob_id
                    )
                except Exception:
                    pass
        items: list[ArchivedCorpusDocument] = []
        children: list[ArchivedCorpusDocument] = []
        for collection in collections:
            collection_artifact_id = collection.derived_artifact_id
            collection_document_id = collection.source_document_id
            if collection_artifact_id is None or collection_document_id is None:
                continue
            document = documents.get(collection_document_id)
            artifact = artifacts.get(collection_artifact_id)
            if document is None or artifact is None:
                continue
            item = ArchivedCorpusDocument(
                collection=collection,
                document=document,
                text=texts.get(artifact.text_blob_id, ""),
                derived_artifact_id=artifact.id,
                parser_version=artifact.parser_version,
                source_document_id=document.id,
            )
            (children if collection.origin_kind.value == "referenced_evidence" else items).append(
                item
            )
        return build_production_evidence_pack(report, items, children)

    @staticmethod
    def _build_synthesis_evidence_pack(report: ReferenceReport, extraction: Any) -> dict[str, Any]:
        """Deterministic Q4 input, stripped of operational/internal evidence.

        Q4 must write from the verified Q1/Q2 results, not from raw collection
        material.  In particular, never expose source URLs, chunk/model IDs, or
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
            "version": "1",
            "reference_report": {
                "sources": [
                    {
                        "id": source.local_id,
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

    async def _execute_synthesis_stage(self, run: SubjectProductionRun) -> dict[str, Any]:
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
                uow, run.subject_id, synthesis_research_date
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
            subject_title, _ = await self._subject_context(uow, run.subject_id)
            synthesis_pack = self._build_synthesis_evidence_pack(report, extraction_payload)
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
                    "synthesis_evidence_pack_version": "1",
                    "synthesis_evidence_pack_hash": synthesis_pack_hash,
                    "prompt_version": SYNTHESIS_PROMPT_VERSION,
                    "web_policy_version": "q4-web-non-authoritative-v1",
                    "model_routing_policy": "openai-drafting-v1",
                    "stage": "synthesis",
                    "synthesis_generation": run.synthesis_generation,
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
                parsed, output_text, turn_id = await self._ask_with_format_repair(
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
                    # A synthesis retry bumps this: it must never replay the
                    # previous, already-SUCCEEDED Q4 turn.
                    request_identity=f"generation-{run.synthesis_generation}",
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

                artifact = await self._synthesis.store_synthesis_result(
                    run_id=run.id,
                    subject_id=run.subject_id,
                    input_hash=input_hash,
                    raw_result=output_text,
                    markdown_content=output_text,
                    conversation_turn_id=turn_id,
                )

                return {
                    "stage": "synthesis",
                    "status": "success",
                    "artifact_id": str(artifact.id),
                    "word_count": len(output_text.split()),
                    "repair_actions": parsed.repair_actions,
                }
            except Exception as e:
                return self._handle_stage_exception(run, "synthesis", e)

    async def _execute_assembly_stage(self, run: SubjectProductionRun) -> dict[str, Any]:
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

            subject_title, _ = await self._subject_context(uow, run.subject_id)
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


def _q2_chunk_source_context(chunk: Any) -> str:
    """Archived source context deliberately safe for the stateless Q2 prompt."""
    lines = ["Métadonnées source archivée (contexte, jamais preuve) :"]
    if chunk.title:
        lines.append(f"- titre: {chunk.title}")
    lines.append(f"- origine: {chunk.origin_kind.value}")
    if chunk.parent_source_ids:
        lines.append(f"- source parente: {', '.join(chunk.parent_source_ids)}")
    if chunk.source_ids:
        lines.append(f"- source: {', '.join(chunk.source_ids)}")
    if chunk.origin_kind.value == "referenced_evidence":
        lines.append("- evidence référencée: oui")
    return "\n".join(lines)


def _q2_logical_request_id(
    *, run_id: UUID, subject_id: UUID, evidence_pack_hash: str, chunk_sha256: str
) -> str:
    """Stable Q2 checkpoint identity.  Every semantic input/policy version participates.

    Provider is implicitly `openai` (the routing policy version pins that), the
    Markdown parser version pins the dialect, and the repair policy version
    pins the one-shot repair prompt: a change to any of them must invalidate
    both the deterministic chunk conversation id and the turn idempotency key,
    so a stale checkpoint is never replayed.
    """
    return "q2:" + json.dumps(
        {
            "production_run_id": str(run_id),
            "subject_id": str(subject_id),
            "evidence_pack_hash": evidence_pack_hash,
            "chunk_sha256": chunk_sha256,
            "provider": ModelProvider.OPENAI.value,
            "q2_schema_version": Q2_SCHEMA_VERSION,
            "parser_version": Q2_MARKDOWN_PARSER_VERSION,
            "prompt_version": EXTRACTION_PROMPT_VERSION,
            "repair_policy_version": EXTRACTION_FORMAT_REPAIR_VERSION,
            "routing_policy_version": Q2_ROUTING_POLICY_VERSION,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _q2_chunk_conversation_id(logical_request_id: str) -> UUID:
    """Deterministic id for a chunk's bounded conversation.

    Q2 checkpointing on `add_turn`'s idempotency key requires calling with the
    *same* conversation id on every retry — a fresh random id per attempt would
    make the dedup check reject the retry as belonging to "another
    conversation". Deriving it from the same versioned identity as the turn's
    idempotency key means any prompt/parser/policy bump naturally opens a new
    conversation instead of replaying an old FRESH turn under a new contract.
    """
    return uuid5(NAMESPACE_URL, f"q2-chunk-conversation:{logical_request_id}")
