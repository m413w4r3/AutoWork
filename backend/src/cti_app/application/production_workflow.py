"""Main production workflow orchestration service."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

from cti_app.application.collection import SupplementalSource
from cti_app.application.jobs import JobExecutionContext
from cti_app.application.model_conversations import ModelConversationService
from cti_app.application.persistence import UnitOfWork, UnitOfWorkFactory
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.application.production_context import build_subject_production_context
from cti_app.application.production_parsers import (
    ParsedEvent,
    ParseResult,
    ReferenceReport,
    parse_reference_report,
    parse_technical_extraction,
    reference_report_from_json,
    reference_report_to_json,
    technical_extraction_to_json,
)
from cti_app.application.production_prompts import ProductionPromptTemplates
from cti_app.application.production_stages import (
    BriefAssemblyService,
    ExtractionService,
    ProductionQAService,
    ReferenceResearchService,
    SynthesisService,
    compute_input_hash,
)
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
    "conversation_busy",
}


def _transient_or_terminal(stage: str, exc: Exception) -> dict[str, Any]:
    code = str(getattr(exc, "code", "") or "")
    retryable = bool(getattr(exc, "retryable", False))
    transient = retryable or code in _TRANSIENT_CODES
    return {
        "stage": stage,
        "status": "transient_error" if transient else "terminal_error",
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
    ) -> None:
        self._uow_factory = uow_factory
        self._model_service = model_service
        self._collection_service = collection_service
        self._artifact_store = artifact_store
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
        """Execute a single production stage.

        Idempotent: if stage is already complete, returns cached result.
        """
        self._correlation_id = correlation_id
        async with self._uow_factory() as uow:
            run = await uow.subject_production_runs.get_for_update(run_id)
            if not run:
                raise ValueError(f"Production run {run_id} not found")

            # Verify we're on expected stage
            if run.current_stage != expected_stage:
                raise ValueError(
                    f"Run on stage {run.current_stage.value}, expected {expected_stage.value}"
                )

            # Check for cached result with same input_hash
            # (would be implemented with actual artifact storage)

            # Execute the stage based on type
            if expected_stage == SubjectProductionStage.SOURCES:
                result = await self._execute_sources_stage(run, context)
            elif expected_stage == SubjectProductionStage.REFERENCES:
                result = await self._execute_references_stage(run, context)
            elif expected_stage == SubjectProductionStage.EXTRACTION:
                result = await self._execute_extraction_stage(run)
            elif expected_stage == SubjectProductionStage.SYNTHESIS:
                result = await self._execute_synthesis_stage(run)
            elif expected_stage == SubjectProductionStage.ASSEMBLY:
                result = await self._execute_assembly_stage(run)
            else:
                raise ValueError(f"Unknown stage: {expected_stage.value}")

            return result

    async def _ask_with_format_repair(
        self,
        *,
        run: SubjectProductionRun,
        conversation_id: UUID,
        stage: str,
        prompt: str,
        mode: ConversationMode,
        parse: Callable[[str], ParseResult[Any]],
        external_llm_allowed: bool,
    ) -> tuple[ParseResult[Any] | None, str, UUID | None]:
        """Ask the model, and give it exactly one chance to fix its formatting.

        The repair turn never researches again: it restates the same answer in
        the expected structure. Returns the parse result, the raw text used, and
        the turn id it came from.
        """
        assert self._model_service is not None
        turn = await self._model_service.add_turn(
            conversation_id=conversation_id,
            message=prompt,
            mode=mode,
            external_llm_allowed=external_llm_allowed,
            idempotency_key=f"{stage}-{run.id}-v1",
            correlation_id=self._correlation_id,
            context_subject_id=run.subject_id,
        )
        raw = await self._turn_output_text(conversation_id, turn.id) or ""
        if not raw:
            return None, "", turn.id

        result = parse(raw)
        if result.usable:
            return result, raw, turn.id

        repair_prompt = ProductionPromptTemplates.get_format_repair_prompt(
            stage=stage, problems=result.errors
        )
        repair_turn = await self._model_service.add_turn(
            conversation_id=conversation_id,
            message=repair_prompt,
            mode=ConversationMode.CONTINUE,
            external_llm_allowed=external_llm_allowed,
            idempotency_key=f"{stage}-format-repair-{run.id}-v1",
            correlation_id=self._correlation_id,
            context_subject_id=run.subject_id,
        )
        repaired_raw = await self._turn_output_text(conversation_id, repair_turn.id) or ""
        if not repaired_raw:
            return result, raw, turn.id

        repaired = parse(repaired_raw)
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

    async def _load_reference_report(self, artifact: Any) -> ReferenceReport | None:
        """Read back the canonical Q1 report stored with the artifact."""
        if self._artifact_store is None or artifact.canonical_blob_id is None:
            return None
        try:
            payload = await self._artifact_store.read_json(artifact.canonical_blob_id)
            return reference_report_from_json(payload)
        except Exception:
            return None

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
        """Open the dedicated ChatGPT conversation for this subject."""
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
        """Execute the sources stage (no LLM).

        Pulls the publications retained at discovery, deduplicates them by
        canonical URL, downloads and archives them into the subject workspace.
        """
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
        """Execute references research stage.

        Calls LLM to conduct web research and build timeline.
        """
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

            # Prepare input for hashing
            input_data = {
                "subject_id": str(run.subject_id),
                "title": subject_title,
                "context": ctx.subject_description,
                "research_date": research_date.isoformat(),
                "stage": "references",
            }
            input_hash = compute_input_hash(input_data)

            # Check if we already have artifact with same hash
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
                    mode=ConversationMode.FRESH,
                    parse=lambda text: parse_reference_report(text, research_date),
                    external_llm_allowed=ctx.external_llm_allowed,
                )
            except Exception as e:
                return _transient_or_terminal("references", e)

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

    async def _execute_extraction_stage(self, run: SubjectProductionRun) -> dict[str, Any]:
        """Execute CTI extraction stage.

        Calls LLM to extract technical intelligence from references.
        """
        if not self._model_service:
            return {
                "stage": "extraction",
                "status": "error",
                "error": "ModelConversationService not configured",
            }

        async with self._uow_factory() as uow:
            # Get references artifact
            references = await uow.production_artifacts.get_current(run.id, "references")
            if not references:
                return {
                    "stage": "extraction",
                    "status": "error",
                    "error": "References artifact not found",
                }

            # Prepare input for hashing
            input_data = {
                "subject_id": str(run.subject_id),
                "references_version": references.version,
                "stage": "extraction",
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

            report = await self._load_reference_report(references)
            if report is None:
                return {
                    "stage": "extraction",
                    "status": "terminal_error",
                    "error_code": "references_payload_missing",
                    "error": "Reference report content is not readable",
                }

            # Check if we already have artifact with same hash
            existing = await uow.production_artifacts.get_current(run.id, "extraction")
            if existing and existing.input_hash == input_hash:
                return {
                    "stage": "extraction",
                    "status": "cached",
                    "artifact_id": str(existing.id),
                }

            subject_title, _ = await self._subject_context(uow, run.subject_id)
            prompt = ProductionPromptTemplates.get_extraction_prompt(
                subject_title=subject_title,
            )

            # Call LLM (continue mode, same conversation)
            try:
                parsed, raw, turn_id = await self._ask_with_format_repair(
                    run=run,
                    conversation_id=conversation_id,
                    stage="extraction",
                    prompt=prompt,
                    mode=ConversationMode.CONTINUE,
                    parse=lambda text: parse_technical_extraction(text, report),
                    external_llm_allowed=policy_allows,
                )
            except Exception as e:
                return _transient_or_terminal("extraction", e)

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

            extraction = parsed.value
            assert extraction is not None
            artifact = await self._extraction.store_extraction_result(
                run_id=run.id,
                subject_id=run.subject_id,
                input_hash=input_hash,
                raw_result=raw,
                canonical_json=technical_extraction_to_json(extraction),
                conversation_turn_id=turn_id,
            )

            return {
                "stage": "extraction",
                "status": "success",
                "artifact_id": str(artifact.id),
                "items_count": len(extraction.items),
                "supported_items": len(extraction.supported_items()),
                "warnings": parsed.warnings,
                "repair_actions": parsed.repair_actions,
            }

    async def _execute_synthesis_stage(self, run: SubjectProductionRun) -> dict[str, Any]:
        """Execute technical synthesis stage.

        Calls LLM to write French technical summary.
        """
        if not self._model_service:
            return {
                "stage": "synthesis",
                "status": "error",
                "error": "ModelConversationService not configured",
            }

        async with self._uow_factory() as uow:
            # Get extraction artifact
            extraction = await uow.production_artifacts.get_current(run.id, "extraction")
            if not extraction:
                return {
                    "stage": "synthesis",
                    "status": "error",
                    "error": "Extraction artifact not found",
                }

            # Prepare input for hashing
            input_data = {
                "subject_id": str(run.subject_id),
                "extraction_version": extraction.version,
                "stage": "synthesis",
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

            # Check if we already have artifact with same hash
            existing = await uow.production_artifacts.get_current(run.id, "synthesis")
            if existing and existing.input_hash == input_hash:
                return {
                    "stage": "synthesis",
                    "status": "cached",
                    "artifact_id": str(existing.id),
                }

            subject_title, _ = await self._subject_context(uow, run.subject_id)
            prompt = ProductionPromptTemplates.get_synthesis_prompt(
                subject_title=subject_title,
            )

            # Call LLM (continue mode, same conversation)
            try:
                turn = await self._model_service.add_turn(
                    conversation_id=conversation_id,
                    message=prompt,
                    mode=ConversationMode.CONTINUE,
                    external_llm_allowed=synthesis_policy_allows,
                    idempotency_key=f"synthesis-{run.id}-v1",
                    correlation_id=self._correlation_id,
                    context_subject_id=run.subject_id,
                )

                output_text = await self._turn_output_text(conversation_id, turn.id)
                if not output_text:
                    return {
                        "stage": "synthesis",
                        "status": "error",
                        "error": "No response from model",
                    }

                artifact = await self._synthesis.store_synthesis_result(
                    run_id=run.id,
                    subject_id=run.subject_id,
                    input_hash=input_hash,
                    raw_result=output_text,
                    markdown_content=output_text,
                    conversation_turn_id=turn.id,
                )

                return {
                    "stage": "synthesis",
                    "status": "success",
                    "artifact_id": str(artifact.id),
                    "word_count": len(output_text.split()),
                }
            except Exception as e:
                return {
                    "stage": "synthesis",
                    "status": "error",
                    "error": str(e),
                }

    async def _execute_assembly_stage(self, run: SubjectProductionRun) -> dict[str, Any]:
        """Execute brief assembly stage (deterministic).

        No LLM call - pure rendering from artifacts.
        """
        async with self._uow_factory() as uow:
            # Get all artifacts
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
            brief = await self._assembly.assemble_brief(
                run_id=run.id,
                subject_id=run.subject_id,
                subject_title=subject_title,
                references_artifact=references,
                extraction_artifact=extraction,
                synthesis_artifact=synthesis,
            )

            # Run QA
            qa_result = await self._qa.run_qa(
                run_id=run.id,
                references_artifact=references,
                extraction_artifact=extraction,
                synthesis_artifact=synthesis,
                brief_artifact=brief,
            )

            if qa_result["passed"]:
                # Mark run as READY
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
                # Mark run as NEEDS_REVIEW
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
        """Retry reference research stage.

        Archives old conversation, creates new one, regenerates pipeline.
        """
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

            # Reset to SOURCES stage
            run.current_stage = SubjectProductionStage.SOURCES
            run.status = SubjectProductionStatus.QUEUED

            # Mark downstream artifacts stale
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
        """Retry synthesis stage only.

        Keeps same conversation and references/extraction.
        """
        async with self._uow_factory() as uow:
            run = await uow.subject_production_runs.get_for_update(run_id)
            if not run:
                return {
                    "action": "retry_synthesis",
                    "status": "error",
                    "error": f"Run {run_id} not found",
                }

            # Verify run is in correct state
            if run.status not in {
                SubjectProductionStatus.READY,
                SubjectProductionStatus.NEEDS_REVIEW,
            }:
                return {
                    "action": "retry_synthesis",
                    "status": "error",
                    "error": f"Run is in {run.status.value} state, cannot retry",
                }

            # Set stage to SYNTHESIS
            run.current_stage = SubjectProductionStage.SYNTHESIS
            run.status = SubjectProductionStatus.RUNNING

            # Mark brief stale
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
