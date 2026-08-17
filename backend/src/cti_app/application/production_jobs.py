"""Job handlers for production workflow stages."""

from __future__ import annotations

from typing import Any, Protocol
from uuid import UUID

from pydantic import Field

from cti_app.application.jobs import (
    JobExecutionContext,
    JobHandlerError,
    JobParameters,
    JobRegistry,
)
from cti_app.application.persistence import ProductionUnitOfWork
from cti_app.application.production_workflow import ProductionWorkflowOrchestrator
from cti_app.domain.production import SubjectProductionStage, SubjectProductionStatus


class ProductionJobHandler(Protocol):
    """Protocol for production job handlers."""

    async def __call__(
        self,
        run_id: UUID,
        parameters: dict[str, Any],
    ) -> dict[str, Any]: ...


class ProductionJobDispatcher:
    """Dispatches production workflow jobs."""

    def __init__(self, uow_factory: ProductionUnitOfWork) -> None:
        self._uow_factory = uow_factory
        self._orchestrator = ProductionWorkflowOrchestrator(uow_factory)

    async def dispatch_sources_job(
        self,
        run_id: UUID,
    ) -> dict[str, Any]:
        """Dispatch sources collection job."""
        return {
            "job_kind": "production.subject.sources",
            "run_id": str(run_id),
            "expected_stage": SubjectProductionStage.SOURCES.value,
            "parameters": {},
        }

    async def dispatch_references_job(
        self,
        run_id: UUID,
    ) -> dict[str, Any]:
        """Dispatch reference research job."""
        return {
            "job_kind": "production.subject.references",
            "run_id": str(run_id),
            "expected_stage": SubjectProductionStage.REFERENCES.value,
            "parameters": {},
        }

    async def dispatch_extraction_job(
        self,
        run_id: UUID,
    ) -> dict[str, Any]:
        """Dispatch CTI extraction job."""
        return {
            "job_kind": "production.subject.extraction",
            "run_id": str(run_id),
            "expected_stage": SubjectProductionStage.EXTRACTION.value,
            "parameters": {},
        }

    async def dispatch_synthesis_job(
        self,
        run_id: UUID,
    ) -> dict[str, Any]:
        """Dispatch technical synthesis job."""
        return {
            "job_kind": "production.subject.synthesis",
            "run_id": str(run_id),
            "expected_stage": SubjectProductionStage.SYNTHESIS.value,
            "parameters": {},
        }

    async def dispatch_assembly_job(
        self,
        run_id: UUID,
    ) -> dict[str, Any]:
        """Dispatch brief assembly job."""
        return {
            "job_kind": "production.subject.assemble",
            "run_id": str(run_id),
            "expected_stage": SubjectProductionStage.ASSEMBLY.value,
            "parameters": {},
        }


# Job handler implementations
# These would be registered with the job system


async def handle_production_sources_job(
    run_id: UUID,
    expected_stage: str,
    uow_factory: ProductionUnitOfWork,
) -> dict[str, Any]:
    """Handle production.subject.sources job."""
    orchestrator = ProductionWorkflowOrchestrator(uow_factory)

    try:
        result = await orchestrator.execute_stage(
            run_id=run_id,
            expected_stage=SubjectProductionStage(expected_stage),
        )
        return {
            "status": "success",
            "result": result,
        }
    except Exception as e:
        return {
            "status": "error",
            "error": str(e),
        }


async def handle_production_references_job(
    run_id: UUID,
    expected_stage: str,
    uow_factory: ProductionUnitOfWork,
) -> dict[str, Any]:
    """Handle production.subject.references job.

    This job:
    1. Prepares the reference research prompt
    2. Calls ModelConversationService.add_turn(mode=FRESH)
    3. Parses and validates the response
    4. Stores the artifact
    5. Advances the run to next stage
    6. Dispatches extraction job
    """
    orchestrator = ProductionWorkflowOrchestrator(uow_factory)

    try:
        result = await orchestrator.execute_stage(
            run_id=run_id,
            expected_stage=SubjectProductionStage(expected_stage),
        )

        if result.get("status") == "waiting_for_model":
            # Job has been created to wait for model
            # Next handler will be triggered by ModelConversationService
            pass

        return {
            "status": "success",
            "result": result,
        }
    except Exception as e:
        async with uow_factory() as uow:
            run = await uow.subject_production_runs.get_for_update(run_id)
            if run:
                run.mark_failed(
                    code="references_error",
                    message=str(e),
                )
                await uow.subject_production_runs.save(run)
                await uow.commit()

        return {
            "status": "error",
            "error": str(e),
        }


async def handle_production_extraction_job(
    run_id: UUID,
    expected_stage: str,
    uow_factory: ProductionUnitOfWork,
) -> dict[str, Any]:
    """Handle production.subject.extraction job.

    This job:
    1. Retrieves the references artifact
    2. Prepares the extraction prompt
    3. Calls ModelConversationService.add_turn(mode=CONTINUE)
    4. Parses and validates the response
    5. Stores the artifact
    6. Advances the run to next stage
    7. Dispatches synthesis job
    """
    orchestrator = ProductionWorkflowOrchestrator(uow_factory)

    try:
        result = await orchestrator.execute_stage(
            run_id=run_id,
            expected_stage=SubjectProductionStage(expected_stage),
        )

        if result.get("status") == "waiting_for_model":
            pass

        return {
            "status": "success",
            "result": result,
        }
    except Exception as e:
        async with uow_factory() as uow:
            run = await uow.subject_production_runs.get_for_update(run_id)
            if run:
                run.mark_failed(
                    code="extraction_error",
                    message=str(e),
                )
                await uow.subject_production_runs.save(run)
                await uow.commit()

        return {
            "status": "error",
            "error": str(e),
        }


async def handle_production_synthesis_job(
    run_id: UUID,
    expected_stage: str,
    uow_factory: ProductionUnitOfWork,
) -> dict[str, Any]:
    """Handle production.subject.synthesis job.

    This job:
    1. Retrieves the extraction artifact
    2. Prepares the synthesis prompt
    3. Calls ModelConversationService.add_turn(mode=CONTINUE)
    4. Parses and validates the response
    5. Stores the artifact
    6. Advances the run to next stage
    7. Dispatches assembly job
    """
    orchestrator = ProductionWorkflowOrchestrator(uow_factory)

    try:
        result = await orchestrator.execute_stage(
            run_id=run_id,
            expected_stage=SubjectProductionStage(expected_stage),
        )

        if result.get("status") == "waiting_for_model":
            pass

        return {
            "status": "success",
            "result": result,
        }
    except Exception as e:
        async with uow_factory() as uow:
            run = await uow.subject_production_runs.get_for_update(run_id)
            if run:
                run.mark_failed(
                    code="synthesis_error",
                    message=str(e),
                )
                await uow.subject_production_runs.save(run)
                await uow.commit()

        return {
            "status": "error",
            "error": str(e),
        }


async def handle_production_assembly_job(
    run_id: UUID,
    expected_stage: str,
    uow_factory: ProductionUnitOfWork,
) -> dict[str, Any]:
    """Handle production.subject.assemble job.

    This job:
    1. Retrieves all artifacts (references, extraction, synthesis)
    2. Renders the brief deterministically
    3. Runs QA checks
    4. Marks run as READY (if QA passes) or NEEDS_REVIEW
    5. Advances batch to next subject
    """
    orchestrator = ProductionWorkflowOrchestrator(uow_factory)

    try:
        result = await orchestrator.execute_stage(
            run_id=run_id,
            expected_stage=SubjectProductionStage(expected_stage),
        )

        # After assembly, dispatch batch advancement
        async with uow_factory() as uow:
            run = await uow.subject_production_runs.get(run_id)
            if run:
                # TODO: Notify batch to advance to next subject
                pass

        return {
            "status": "success",
            "result": result,
        }
    except Exception as e:
        async with uow_factory() as uow:
            run = await uow.subject_production_runs.get_for_update(run_id)
            if run:
                run.mark_failed(
                    code="assembly_error",
                    message=str(e),
                )
                await uow.subject_production_runs.save(run)
                await uow.commit()

        return {
            "status": "error",
            "error": str(e),
        }


# Job kind registry
PRODUCTION_JOB_HANDLERS = {
    "production.subject.sources": handle_production_sources_job,
    "production.subject.references": handle_production_references_job,
    "production.subject.extraction": handle_production_extraction_job,
    "production.subject.synthesis": handle_production_synthesis_job,
    "production.subject.assemble": handle_production_assembly_job,
}


# Job Parameters
class ProductionStageParameters(JobParameters):
    """Parameters for production stage jobs."""

    run_id: UUID = Field(..., description="Production run ID")
    expected_stage: str = Field(..., description="Expected production stage")


# Job Registration
def register_production_jobs(
    registry: JobRegistry,
    uow_factory: ProductionUnitOfWork,
) -> None:
    """Register all production workflow jobs."""

    async def handle_stage(
        parameters: JobParameters,
        context: JobExecutionContext,
    ) -> str:
        """Generic handler for production stage jobs."""
        if not isinstance(parameters, ProductionStageParameters):
            raise TypeError("Invalid production stage parameters")

        orchestrator = ProductionWorkflowOrchestrator(uow_factory)

        try:
            # Execute stage
            result = await orchestrator.execute_stage(
                run_id=parameters.run_id,
                expected_stage=SubjectProductionStage(parameters.expected_stage),
            )

            # Update progress
            stage_index = list(SubjectProductionStage).index(
                SubjectProductionStage(parameters.expected_stage)
            )
            total_stages = len(list(SubjectProductionStage))
            await context.report_progress(
                stage_index + 1,
                total_stages,
                f"Étape {stage_index + 1}/{total_stages} complète",
            )

            if result.get("status") == "error":
                raise JobHandlerError(
                    code=result.get("error", "unknown_error"),
                    public_message=f"Erreur lors de l'étape {parameters.expected_stage}",
                    transient=False,
                )

            # Auto-advance to next stage
            next_stage_job_id = None
            async with uow_factory() as uow:
                run = await uow.subject_production_runs.get(parameters.run_id)
                if run and run.status == SubjectProductionStatus.RUNNING:
                    # Check if there's a next stage
                    stages = list(SubjectProductionStage)
                    current_index = stages.index(SubjectProductionStage(parameters.expected_stage))
                    if current_index < len(stages) - 1:
                        # Advance to next stage
                        run.advance_stage()
                        await uow.subject_production_runs.save(run)
                        await uow.commit()

                        # Return job kind for next stage to be dispatched
                        next_stage = stages[current_index + 1]
                        next_stage_job_id = f"production.subject.{next_stage.value}"

            if next_stage_job_id:
                # Signal to dispatch next job (job service will handle this)
                return f"next-job://{next_stage_job_id}:{parameters.run_id}"

            return f"production-stage://{parameters.run_id}/{parameters.expected_stage}"

        except Exception as e:
            raise JobHandlerError(
                code="production_stage_error",
                public_message=f"Erreur lors de l'étape {parameters.expected_stage}: {e!s}",
                transient=False,
            ) from e

    # Register all 5 stage jobs
    registry.register(
        "production.subject.sources",
        ProductionStageParameters,
        handle_stage,
        resume_after_worker_loss=True,
    )
    registry.register(
        "production.subject.references",
        ProductionStageParameters,
        handle_stage,
        resume_after_worker_loss=True,
    )
    registry.register(
        "production.subject.extraction",
        ProductionStageParameters,
        handle_stage,
        resume_after_worker_loss=True,
    )
    registry.register(
        "production.subject.synthesis",
        ProductionStageParameters,
        handle_stage,
        resume_after_worker_loss=True,
    )
    registry.register(
        "production.subject.assemble",
        ProductionStageParameters,
        handle_stage,
        resume_after_worker_loss=True,
    )
