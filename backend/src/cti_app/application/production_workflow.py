"""Main production workflow orchestration service."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from cti_app.application.jobs import JobExecutionContext
from cti_app.application.model_conversations import ModelConversationService
from cti_app.application.persistence import UnitOfWork, UnitOfWorkFactory
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


class ProductionWorkflowOrchestrator:
    """Orchestrates the complete brief_auto production workflow."""

    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        model_service: ModelConversationService | None = None,
        collection_service: SubjectCollectionService | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._model_service = model_service
        self._collection_service = collection_service
        self._references = ReferenceResearchService(uow_factory)
        self._extraction = ExtractionService(uow_factory)
        self._synthesis = SynthesisService(uow_factory)
        self._assembly = BriefAssemblyService(uow_factory)
        self._qa = ProductionQAService(uow_factory)

    async def execute_stage(
        self,
        run_id: UUID,
        expected_stage: SubjectProductionStage,
        context: JobExecutionContext | None = None,
    ) -> dict[str, Any]:
        """Execute a single production stage.

        Idempotent: if stage is already complete, returns cached result.
        """
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
                result = await self._execute_references_stage(run)
            elif expected_stage == SubjectProductionStage.EXTRACTION:
                result = await self._execute_extraction_stage(run)
            elif expected_stage == SubjectProductionStage.SYNTHESIS:
                result = await self._execute_synthesis_stage(run)
            elif expected_stage == SubjectProductionStage.ASSEMBLY:
                result = await self._execute_assembly_stage(run)
            else:
                raise ValueError(f"Unknown stage: {expected_stage.value}")

            return result

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

    async def _execute_references_stage(self, run: SubjectProductionRun) -> dict[str, Any]:
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
            subject_title, subject_context = await self._subject_context(uow, run.subject_id)

            # Prepare input for hashing
            input_data = {
                "subject_id": str(run.subject_id),
                "title": subject_title,
                "context": subject_context,
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

            now = datetime.now(UTC)
            existing_sources = await uow.source_collections.list_for_subject(run.subject_id)
            existing_sources_text = "\n".join(
                f"- {item.requested_url}" for item in existing_sources
            )
            prompt = ProductionPromptTemplates.get_references_prompt(
                subject_title=subject_title,
                subject_description=subject_context,
                actor_info="",
                technical_summary="",
                research_date=now.date().isoformat(),
                period_start="",
                period_end="",
                existing_sources_text=existing_sources_text,
            )

            # Call LLM
            try:
                turn = await self._model_service.add_turn(
                    conversation_id=run.conversation_id,
                    message=prompt,
                    mode=ConversationMode.FRESH,
                    external_llm_allowed=True,
                    idempotency_key=f"references-{run.id}",
                    correlation_id=str(uuid4()),
                    context_subject_id=run.subject_id,
                )

                output_text = await self._turn_output_text(run.conversation_id, turn.id)
                if not output_text:
                    return {
                        "stage": "references",
                        "status": "error",
                        "error": "No response from model",
                    }

                response_data = json.loads(output_text)

                artifact = await self._references.store_references_result(
                    run_id=run.id,
                    subject_id=run.subject_id,
                    input_hash=input_hash,
                    raw_result=output_text,
                    canonical_json=response_data,
                    conversation_turn_id=turn.id,
                )

                return {
                    "stage": "references",
                    "status": "success",
                    "artifact_id": str(artifact.id),
                    "sources_count": len(response_data.get("sources", [])),
                }
            except Exception as e:
                return {
                    "stage": "references",
                    "status": "error",
                    "error": str(e),
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
                turn = await self._model_service.add_turn(
                    conversation_id=conversation_id,
                    message=prompt,
                    mode=ConversationMode.CONTINUE,
                    external_llm_allowed=True,
                    idempotency_key=f"extraction-{run.id}",
                    correlation_id=str(uuid4()),
                    context_subject_id=run.subject_id,
                )

                output_text = await self._turn_output_text(conversation_id, turn.id)
                if not output_text:
                    return {
                        "stage": "extraction",
                        "status": "error",
                        "error": "No response from model",
                    }

                response_data = json.loads(output_text)

                artifact = await self._extraction.store_extraction_result(
                    run_id=run.id,
                    subject_id=run.subject_id,
                    input_hash=input_hash,
                    raw_result=output_text,
                    canonical_json=response_data,
                    conversation_turn_id=turn.id,
                )

                return {
                    "stage": "extraction",
                    "status": "success",
                    "artifact_id": str(artifact.id),
                    "indicators_count": len(response_data.get("indicators", [])),
                }
            except Exception as e:
                return {
                    "stage": "extraction",
                    "status": "error",
                    "error": str(e),
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
                    external_llm_allowed=True,
                    idempotency_key=f"synthesis-{run.id}",
                    correlation_id=str(uuid4()),
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
