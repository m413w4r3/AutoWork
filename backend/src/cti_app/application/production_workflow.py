"""Main production workflow orchestration service."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import UUID, uuid4

from cti_app.application.model_conversations import ModelConversationService
from cti_app.application.persistence import ProductionUnitOfWork
from cti_app.application.production_prompts import ProductionPromptTemplates
from cti_app.application.production_stages import (
    BriefAssemblyService,
    ExtractionService,
    ProductionQAService,
    ReferenceResearchService,
    SynthesisService,
    compute_input_hash,
)
from cti_app.domain.model_conversations import ConversationMode, ConversationPurpose
from cti_app.domain.production import SubjectProductionStage, SubjectProductionStatus


class ProductionWorkflowOrchestrator:
    """Orchestrates the complete brief_auto production workflow."""

    def __init__(
        self,
        uow_factory: ProductionUnitOfWork,
        model_service: ModelConversationService | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._model_service = model_service
        self._references = ReferenceResearchService(uow_factory)
        self._extraction = ExtractionService(uow_factory)
        self._synthesis = SynthesisService(uow_factory)
        self._assembly = BriefAssemblyService(uow_factory)
        self._qa = ProductionQAService(uow_factory)

    async def execute_stage(
        self,
        run_id: UUID,
        expected_stage: SubjectProductionStage,
    ) -> dict:
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
                result = await self._execute_sources_stage(run)
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

    async def _execute_sources_stage(self, run) -> dict:
        """Execute sources stage.

        This stage doesn't require LLM - it retrieves existing sources
        from the subject and prepares them for archive.
        """
        from cti_app.application.evidence import SubjectEvidenceService

        evidence_service = SubjectEvidenceService(self._uow_factory)

        try:
            # Extract evidence from all archived sources
            result = await evidence_service.extract_subject(
                subject_id=run.subject_id,
                only_pending=True,
            )

            if result.get("status") != "success":
                return {
                    "stage": "sources",
                    "status": "error",
                    "error": result.get("error", "Unknown error"),
                }

            return {
                "stage": "sources",
                "status": "success",
                "sources_count": result.get("collections_processed", 0),
                "extracted": result.get("extracted", 0),
            }
        except Exception as e:
            return {
                "stage": "sources",
                "status": "error",
                "error": str(e),
            }

    async def _execute_references_stage(self, run) -> dict:
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
            subject = await uow.subjects.get(run.subject_id)
            if not subject:
                return {
                    "stage": "references",
                    "status": "error",
                    "error": f"Subject {run.subject_id} not found",
                }

            # Prepare input for hashing
            input_data = {
                "subject_id": str(run.subject_id),
                "title": subject.title,
                "context": subject.description or "",
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

            # Create or get conversation
            if not run.conversation_id:
                conversation = await self._model_service.create_conversation(
                    purpose=ConversationPurpose.SUBJECT_PRODUCTION,
                    external_llm_allowed=True,
                )
                run.conversation_id = conversation.id
            else:
                conversation = await self._model_service.get_conversation(run.conversation_id)

            # Prepare prompt
            prompts = ProductionPromptTemplates()
            prompt = prompts.populate_references_template(
                subject_title=subject.title,
                subject_description=subject.description or "",
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

                # Parse response
                if not turn.output_text:
                    return {
                        "stage": "references",
                        "status": "error",
                        "error": "No response from model",
                    }

                response_data = json.loads(turn.output_text)

                # Store artifact
                artifact = await self._references.store_references_result(
                    run_id=run.id,
                    subject_id=run.subject_id,
                    input_hash=input_hash,
                    data=response_data,
                    turn_id=turn.id,
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

    async def _execute_extraction_stage(self, run) -> dict:
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

            # Check if we already have artifact with same hash
            existing = await uow.production_artifacts.get_current(run.id, "extraction")
            if existing and existing.input_hash == input_hash:
                return {
                    "stage": "extraction",
                    "status": "cached",
                    "artifact_id": str(existing.id),
                }

            # Prepare prompt with references data
            prompts = ProductionPromptTemplates()
            references_json = json.dumps(references.metadata)
            prompt = prompts.populate_extraction_template(
                references_json=references_json,
            )

            # Call LLM (continue mode, same conversation)
            try:
                turn = await self._model_service.add_turn(
                    conversation_id=run.conversation_id,
                    message=prompt,
                    mode=ConversationMode.CONTINUE,
                    external_llm_allowed=True,
                    idempotency_key=f"extraction-{run.id}",
                    correlation_id=str(uuid4()),
                    context_subject_id=run.subject_id,
                )

                if not turn.output_text:
                    return {
                        "stage": "extraction",
                        "status": "error",
                        "error": "No response from model",
                    }

                response_data = json.loads(turn.output_text)

                # Store artifact
                artifact = await self._extraction.store_extraction_result(
                    run_id=run.id,
                    subject_id=run.subject_id,
                    input_hash=input_hash,
                    data=response_data,
                    turn_id=turn.id,
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

    async def _execute_synthesis_stage(self, run) -> dict:
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

            # Check if we already have artifact with same hash
            existing = await uow.production_artifacts.get_current(run.id, "synthesis")
            if existing and existing.input_hash == input_hash:
                return {
                    "stage": "synthesis",
                    "status": "cached",
                    "artifact_id": str(existing.id),
                }

            # Prepare prompt with extraction data
            prompts = ProductionPromptTemplates()
            extraction_json = json.dumps(extraction.metadata)
            prompt = prompts.populate_synthesis_template(
                extraction_json=extraction_json,
            )

            # Call LLM (continue mode, same conversation)
            try:
                turn = await self._model_service.add_turn(
                    conversation_id=run.conversation_id,
                    message=prompt,
                    mode=ConversationMode.CONTINUE,
                    external_llm_allowed=True,
                    idempotency_key=f"synthesis-{run.id}",
                    correlation_id=str(uuid4()),
                    context_subject_id=run.subject_id,
                )

                if not turn.output_text:
                    return {
                        "stage": "synthesis",
                        "status": "error",
                        "error": "No response from model",
                    }

                # Store artifact (markdown response)
                artifact = await self._synthesis.store_synthesis_result(
                    run_id=run.id,
                    subject_id=run.subject_id,
                    input_hash=input_hash,
                    markdown_content=turn.output_text,
                    turn_id=turn.id,
                )

                return {
                    "stage": "synthesis",
                    "status": "success",
                    "artifact_id": str(artifact.id),
                    "word_count": len(turn.output_text.split()),
                }
            except Exception as e:
                return {
                    "stage": "synthesis",
                    "status": "error",
                    "error": str(e),
                }

    async def _execute_assembly_stage(self, run) -> dict:
        """Execute brief assembly stage (deterministic).

        No LLM call - pure rendering from artifacts.
        """
        async with self._uow_factory() as uow:
            # Get all artifacts
            references = await uow.production_artifacts.get_current(run.id, "references")
            extraction = await uow.production_artifacts.get_current(run.id, "extraction")
            synthesis = await uow.production_artifacts.get_current(run.id, "synthesis")

            if not all([references, extraction, synthesis]):
                return {
                    "stage": "assembly",
                    "status": "error",
                    "error": "Missing upstream artifacts",
                }

            # Assemble brief
            brief = await self._assembly.assemble_brief(
                run_id=run.id,
                subject_id=run.subject_id,
                subject_title="Subject Title",  # Would get from subject
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

    async def retry_references(self, run_id: UUID) -> dict:
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

            # Archive old conversation
            if run.conversation_id:
                try:
                    await self._model_service.archive_conversation(run.conversation_id)
                except Exception:
                    pass  # Continue even if archival fails

            # Create new conversation
            conversation = await self._model_service.create_conversation(
                purpose=ConversationPurpose.SUBJECT_PRODUCTION,
                external_llm_allowed=True,
            )
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

    async def retry_synthesis(self, run_id: UUID) -> dict:
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
