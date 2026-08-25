"""Main production workflow orchestration service."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

from cti_app.application.collection import ReferencedEvidence, SupplementalSource
from cti_app.application.diagnostics import DiagnosticsLog
from cti_app.application.jobs import JobExecutionContext
from cti_app.application.model_conversations import (
    ConversationTurnFailedError,
    ModelConversationService,
)
from cti_app.application.persistence import UnitOfWork, UnitOfWorkFactory
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.application.production_context import build_subject_production_context
from cti_app.application.production_evidence_pack import (
    ArchivedCorpusDocument,
    ProductionEvidencePack,
    build_production_evidence_pack,
)
from cti_app.application.production_ioc_candidates import (
    DiscoveryPublicationEvidence,
    IocCandidate,
    IocCandidatePack,
    Q2LiteralCandidate,
    build_candidate_pack,
    source_ids_by_document,
)
from cti_app.application.production_normalization import canonical_indicator_key
from cti_app.application.production_parsers import (
    DisplayPolicy,
    IndicatorStatus,
    ParsedEvent,
    ParseResult,
    ReferenceReport,
    TechnicalExtraction,
    parse_reference_report,
    parse_technical_extraction,
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
from cti_app.domain.publication import ArtifactType

if TYPE_CHECKING:
    from cti_app.application.collection import SubjectCollectionService


# Collection states that count as "the source is available for analysis".
_ARCHIVED_STATES = {"archived", "extracted", "completed"}


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
    ) -> tuple[Any | None, str, UUID | None]:
        """Ask the model, and give it exactly one chance to fix its formatting.

        The repair turn never researches again: it restates the same answer in
        the expected structure. Returns the parse result, the raw text used, and
        the turn id it came from.
        """
        assert self._model_service is not None
        idempotency_key = f"{stage}-{run.id}-v{prompt_version}"
        turn = await self._model_service.add_turn(
            conversation_id=conversation_id,
            message=prompt,
            mode=mode,
            external_llm_allowed=external_llm_allowed,
            web_search=web_search,
            idempotency_key=idempotency_key,
            correlation_id=self._correlation_id,
            context_subject_id=run.subject_id,
        )
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
        repair_idempotency_key = f"{stage}-format-repair-{run.id}-v{repair_version}"
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
        self, run: SubjectProductionRun, subject_title: str
    ) -> ModelConversation:
        assert self._model_service is not None
        return await self._model_service.create(
            provider=ModelProvider.OPENAI,
            transport=ConversationTransport.CHATGPT_BRIDGE,
            purpose=ConversationPurpose.SUBJECT_PRODUCTION,
            title=f"Production — {subject_title}",
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

            # One dedicated conversation per subject.
            if not run.conversation_id:
                conversation = await self._open_conversation(run, subject_title)
                run.conversation_id = conversation.id
                persisted = await uow.subject_production_runs.get_for_update(run.id)
                if persisted is not None:
                    persisted.conversation_id = conversation.id
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
                    conversation_id=run.conversation_id,
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
            initial_candidate_pack = await self._build_ioc_candidate_pack(
                uow, run.subject_id, report
            )
            input_data = {
                "subject_id": str(run.subject_id),
                "references_version": references.version,
                "references_hash": references.input_hash,
                "stage": "extraction",
                "prompt_version": EXTRACTION_PROMPT_VERSION,
                "initial_candidate_pack_hash": initial_candidate_pack.pack_hash,
                "evidence_pack_hash": evidence_pack.pack_hash,
            }
            input_hash = compute_input_hash(input_data)

            conversation_id = run.conversation_id
            if conversation_id is None:
                return {
                    "stage": "extraction",
                    "status": "error",
                    "error": "No conversation opened for this run",
                }

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
            archive_repositories_available = all(
                hasattr(uow, name)
                for name in ("source_collections", "source_documents", "derived_artifacts")
            )

            try:
                parsed_items = []
                parsed_warnings: list[str] = []
                raw_parts: list[str] = []
                turn_id = None
                # Only lightweight unit-test UoWs lack archive repositories.
                # Production always takes the explicit-pack path.
                legacy_fallback = not evidence_pack.chunks and not archive_repositories_available
                chunk_count = len(evidence_pack.chunks) or int(legacy_fallback)
                for index in range(chunk_count):
                    chunk = evidence_pack.chunks[index] if not legacy_fallback else None
                    chunk_prompt = (
                        prompt
                        if chunk is None
                        else (
                            prompt
                            + "\n\nCorpus Q2 — segment borné, uniquement données archivées.\n"
                            + f"chunk_id: {chunk.chunk_id}\n"
                            + f"source_ids: {', '.join(chunk.source_ids) or 'none'}\n"
                            + chunk.text
                        )
                    )
                    parsed, raw, chunk_turn_id = await self._ask_with_format_repair(
                        run=run,
                        conversation_id=conversation_id,
                        stage="extraction",
                        prompt=chunk_prompt,
                        prompt_version=(
                            EXTRACTION_PROMPT_VERSION
                            if chunk is None
                            else f"{EXTRACTION_PROMPT_VERSION}-{chunk.chunk_id}"
                        ),
                        repair_version=EXTRACTION_FORMAT_REPAIR_VERSION,
                        mode=ConversationMode.CONTINUE,
                        parse=lambda text: parse_technical_extraction(text, report),
                        external_llm_allowed=policy_allows,
                        web_search=False,
                    )
                    if parsed is not None and parsed.usable and parsed.value is not None:
                        parsed_items.extend(parsed.value.items)
                        parsed_warnings.extend(parsed.warnings)
                        raw_parts.append(raw)
                        turn_id = chunk_turn_id
                parsed = (
                    ParseResult(
                        value=TechnicalExtraction(tuple(parsed_items)),
                        warnings=parsed_warnings,
                    )
                    if parsed_items
                    else None
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
            q2_literals = self._q2_literals(parsed.value)
            candidate_pack = await self._build_ioc_candidate_pack(
                uow, run.subject_id, report, extraction=parsed.value
            )
            q2_diagnostics = self._q2_literal_diagnostics(
                q2_literals, initial_candidate_pack, candidate_pack
            )
            extraction = self._suppress_unbacked_q2_literals(
                parsed.value, candidate_pack.candidates
            )
            assert extraction is not None
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
                    "candidate_total": candidate_pack.total_candidates,
                    "status_totals": status_totals,
                    "repair_actions": parsed.repair_actions,
                    "candidate_pack_hash": candidate_pack.pack_hash,
                    "initial_candidate_pack_hash": initial_candidate_pack.pack_hash,
                    "evidence_pack_hash": evidence_pack.pack_hash,
                    "evidence_pack_chunk_ids": list(evidence_pack.chunk_ids),
                    "evidence_pack_source_document_ids": sorted(
                        {str(chunk.source_document_id) for chunk in evidence_pack.chunks}
                    ),
                    "evidence_pack_parser_versions": dict(evidence_pack.parser_versions),
                    **q2_diagnostics,
                    "source_derived_candidates": candidate_pack.source_derived_candidates,
                    "discovery_augmented_candidates": candidate_pack.discovery_augmented_candidates,
                    "discovery_only_candidates": candidate_pack.discovery_only_candidates,
                    "discovery_matched_to_source": candidate_pack.discovery_matched_to_source,
                    "discovery_unmatched": candidate_pack.discovery_unmatched,
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
                "candidate_total": candidate_pack.total_candidates,
                "status_totals": status_totals,
                "candidate_pack_hash": candidate_pack.pack_hash,
                "initial_candidate_pack_hash": initial_candidate_pack.pack_hash,
                "evidence_pack_hash": evidence_pack.pack_hash,
                "evidence_pack_chunk_ids": list(evidence_pack.chunk_ids),
                "evidence_pack_source_document_ids": sorted(
                    {str(chunk.source_document_id) for chunk in evidence_pack.chunks}
                ),
                "evidence_pack_parser_versions": dict(evidence_pack.parser_versions),
                **q2_diagnostics,
                "source_derived_candidates": candidate_pack.source_derived_candidates,
                "discovery_augmented_candidates": candidate_pack.discovery_augmented_candidates,
                "discovery_only_candidates": candidate_pack.discovery_only_candidates,
                "discovery_matched_to_source": candidate_pack.discovery_matched_to_source,
                "discovery_unmatched": candidate_pack.discovery_unmatched,
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
                collection, document, texts.get(artifact.text_blob_id, "")
            )
            (children if collection.origin_kind.value == "referenced_evidence" else items).append(
                item
            )
        return build_production_evidence_pack(report, items, children)

    async def _build_ioc_candidate_pack(
        self,
        uow: UnitOfWork,
        subject_id: UUID,
        report: ReferenceReport,
        extraction: TechnicalExtraction | None = None,
    ) -> IocCandidatePack:
        """Load persisted evidence snapshots and build the pack before cache lookup."""
        # Lightweight test UoWs intentionally omit these repositories.
        repositories = ("indicators", "source_collections", "source_documents", "derived_artifacts")
        if not all(hasattr(uow, name) for name in repositories):
            return build_candidate_pack((), collections=(), reference_report=report)
        collections = await uow.source_collections.list_for_subject(subject_id)
        documents = await uow.source_documents.list_for_subject(subject_id)
        artifact_ids = {
            collection.derived_artifact_id
            for collection in collections
            if collection.derived_artifact_id is not None
        }
        indicators = tuple(
            indicator
            for indicator in await uow.indicators.list_for_subject(subject_id)
            if indicator.derived_artifact_id in artifact_ids
        )
        artifacts_list = []
        for artifact_id in sorted(artifact_ids, key=str):
            artifact = await uow.derived_artifacts.get(artifact_id)
            if artifact is not None:
                artifacts_list.append(artifact)
        artifacts = tuple(artifacts_list)
        texts: dict[UUID, str] = {}
        if self._source_evidence_processor is not None:
            for artifact in artifacts:
                try:
                    texts[artifact.id] = await self._source_evidence_processor.read_derived_text(
                        artifact.text_blob_id
                    )
                except Exception:
                    continue
        provisional_iocs = []
        discovery_publications: dict[UUID, DiscoveryPublicationEvidence] = {}
        discovery_repositories = (
            "editorial_groups",
            "discovery_subject_identities",
            "subject_contributions",
            "discovery_intakes",
            "discovery_batches",
        )
        if all(hasattr(uow, name) for name in discovery_repositories):
            group = await uow.editorial_groups.get_by_subject(subject_id)
            if group is not None and group.discovery_subject_id is not None:
                contributions = await uow.discovery_subject_identities.contribution_closure(
                    group.discovery_subject_id
                )
                provisional_ids = {
                    provisional_id
                    for contribution in contributions
                    for provisional_id in contribution.contributed_provisional_ioc_ids
                }
                for contribution in contributions:
                    intake = await uow.discovery_intakes.get(contribution.intake_id)
                    batch = (
                        await uow.discovery_batches.get(intake.batch_id)
                        if intake is not None
                        else None
                    )
                    if batch is None:
                        continue
                    for topic in batch.candidates:
                        for provisional in topic.provisional_iocs:
                            if provisional.id in provisional_ids:
                                provisional_iocs.append(provisional)

                collections_by_publication = {
                    collection.source_candidate_id: collection
                    for collection in collections
                    if collection.source_candidate_id is not None
                }
                source_ids = source_ids_by_document(collections, documents, report)
                for provisional in provisional_iocs:
                    for relation in provisional.publication_relations:
                        collection = collections_by_publication.get(relation.publication_id)
                        if collection is None or collection.source_document_id is None:
                            continue
                        publication_artifact_id = collection.derived_artifact_id
                        if publication_artifact_id is None or publication_artifact_id not in texts:
                            continue
                        discovery_publications[relation.publication_id] = (
                            DiscoveryPublicationEvidence(
                                source_document_id=collection.source_document_id,
                                derived_artifact_id=publication_artifact_id,
                                source_ids=source_ids.get(collection.source_document_id, ()),
                                text=texts[publication_artifact_id],
                            )
                        )
        return build_candidate_pack(
            indicators,
            collections=collections,
            source_documents=documents,
            artifacts=artifacts,
            reference_report=report,
            artifact_texts=texts,
            provisional_iocs=tuple({item.id: item for item in provisional_iocs}.values()),
            discovery_publications=discovery_publications,
            q2_literals=self._q2_literals(extraction),
        )

    @staticmethod
    def _q2_literals(extraction: TechnicalExtraction | None) -> tuple[Q2LiteralCandidate, ...]:
        if extraction is None:
            return ()
        literal_types = {
            ArtifactType.IP,
            ArtifactType.DOMAIN,
            ArtifactType.URL,
            ArtifactType.HASH,
            ArtifactType.EMAIL,
        }
        literals: dict[tuple[ArtifactType, str], Q2LiteralCandidate] = {}
        for item in extraction.items:
            if item.artifact_type not in literal_types:
                continue
            try:
                normalized = canonical_indicator_key(item.value, item.artifact_type)
            except ValueError:
                continue
            key = (item.artifact_type, normalized)
            literals.setdefault(
                key,
                Q2LiteralCandidate(
                    artifact_type=item.artifact_type,
                    raw_value=item.value,
                    normalized_value=normalized,
                    context=item.context,
                ),
            )
        return tuple(
            literals[key] for key in sorted(literals, key=lambda item: (item[0].value, item[1]))
        )

    @staticmethod
    def _q2_literal_diagnostics(
        literals: tuple[Q2LiteralCandidate, ...],
        initial_pack: IocCandidatePack,
        final_pack: IocCandidatePack,
    ) -> dict[str, int]:
        initial_keys = {
            (candidate.artifact_type, candidate.normalized_value)
            for candidate in initial_pack.candidates
        }
        final_candidates = {
            (candidate.artifact_type, candidate.normalized_value): candidate
            for candidate in final_pack.candidates
        }
        matched = 0
        recovered = 0
        for literal in literals:
            key = (literal.artifact_type, literal.normalized_value)
            if key in initial_keys:
                matched += 1
            elif (candidate := final_candidates.get(key)) is not None and candidate.source_backed:
                recovered += 1
        return {
            "q2_literal_total": len(literals),
            "q2_literal_matched_candidates": matched,
            "q2_literal_recovered_from_source": recovered,
            "q2_literal_unresolved": len(literals) - matched - recovered,
        }

    @staticmethod
    def _suppress_unbacked_q2_literals(
        extraction: TechnicalExtraction, candidates: tuple[IocCandidate, ...]
    ) -> TechnicalExtraction:
        known = {(candidate.artifact_type, candidate.normalized_value) for candidate in candidates}
        literal_types = {
            ArtifactType.IP,
            ArtifactType.DOMAIN,
            ArtifactType.URL,
            ArtifactType.HASH,
            ArtifactType.EMAIL,
        }
        items = []
        for item in extraction.items:
            if item.artifact_type not in literal_types:
                items.append(item)
                continue
            try:
                key = (item.artifact_type, canonical_indicator_key(item.value, item.artifact_type))
            except ValueError:
                key = None
            if key not in known:
                items.append(
                    replace(
                        item,
                        indicator_status=IndicatorStatus.EXCLUDED,
                        display_policy=DisplayPolicy.HIDDEN,
                        context=("unbacked_ioc_literal: " + item.context).strip(),
                    )
                )
            else:
                items.append(item)
        return replace(extraction, items=tuple(items))

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

            input_data = {
                "subject_id": str(run.subject_id),
                "extraction_version": extraction.version,
                "stage": "synthesis",
                "prompt_version": SYNTHESIS_PROMPT_VERSION,
            }
            input_hash = compute_input_hash(input_data)

            conversation_id = run.conversation_id
            if conversation_id is None:
                return {
                    "stage": "synthesis",
                    "status": "error",
                    "error": "No conversation opened for this run",
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

            existing = await uow.production_artifacts.get_current(run.id, "synthesis")
            if existing and existing.input_hash == input_hash:
                return {
                    "stage": "synthesis",
                    "status": "cached",
                    "artifact_id": str(existing.id),
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
            prompt = ProductionPromptTemplates.get_synthesis_prompt(
                subject_title=subject_title,
                technical_extraction=json.dumps(
                    technical_extraction_to_json(extraction_payload),
                    ensure_ascii=False,
                    sort_keys=True,
                ),
            )

            try:
                parsed, output_text, turn_id = await self._ask_with_format_repair(
                    run=run,
                    conversation_id=conversation_id,
                    stage="synthesis",
                    prompt=prompt,
                    prompt_version=SYNTHESIS_PROMPT_VERSION,
                    repair_version=SYNTHESIS_FORMAT_REPAIR_VERSION,
                    mode=ConversationMode.CONTINUE,
                    parse=lambda text: validate_synthesis(text, report, extraction_payload),
                    external_llm_allowed=synthesis_policy_allows,
                    web_search=False,
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

    async def retry_references(self, run_id: UUID) -> dict[str, Any]:
        """Archives the old conversation, opens a new one, resets the run to SOURCES."""
        if not self._model_service:
            return {
                "action": "retry_references",
                "status": "error",
                "error": "ModelConversationService not configured",
            }

        async with self._uow_factory() as uow:
            run = await uow.subject_production_runs.get_for_update(run_id)
            if not run:
                return {
                    "action": "retry_references",
                    "status": "error",
                    "error": f"Run {run_id} not found",
                }

            subject_title, _ = await self._subject_context(uow, run.subject_id)

            # Archive the old conversation; a failure here must not block the retry.
            if run.conversation_id:
                try:
                    await self._model_service.archive(
                        run.conversation_id, context_subject_id=run.subject_id
                    )
                except Exception:
                    pass

            conversation = await self._open_conversation(run, subject_title)
            run.conversation_id = conversation.id

            run.current_stage = SubjectProductionStage.SOURCES
            run.status = SubjectProductionStatus.QUEUED

            await uow.production_artifacts.mark_downstream_stale(
                run_id, SubjectProductionStage.REFERENCES
            )

            await uow.subject_production_runs.save(run)
            await uow.commit()

        return {
            "action": "retry_references",
            "status": "success",
            "new_conversation_id": str(conversation.id),
        }

    async def retry_synthesis(self, run_id: UUID) -> dict[str, Any]:
        """Keeps the same conversation and references/extraction artifacts."""
        async with self._uow_factory() as uow:
            run = await uow.subject_production_runs.get_for_update(run_id)
            if not run:
                return {
                    "action": "retry_synthesis",
                    "status": "error",
                    "error": f"Run {run_id} not found",
                }

            if run.status not in {
                SubjectProductionStatus.READY,
                SubjectProductionStatus.NEEDS_REVIEW,
            }:
                return {
                    "action": "retry_synthesis",
                    "status": "error",
                    "error": f"Run is in {run.status.value} state, cannot retry",
                }

            run.current_stage = SubjectProductionStage.SYNTHESIS
            run.status = SubjectProductionStatus.RUNNING

            await uow.production_artifacts.mark_downstream_stale(
                run_id, SubjectProductionStage.SYNTHESIS
            )

            await uow.subject_production_runs.save(run)
            await uow.commit()

        return {
            "action": "retry_synthesis",
            "status": "success",
            "stage": "synthesis",
        }
