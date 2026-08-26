"""Job handlers for production workflow stages."""

from __future__ import annotations

from uuid import UUID

from pydantic import ConfigDict, Field

from cti_app.application.analyst_vt_enrichment import VirusTotalSeedEnrichmentService
from cti_app.application.collection import SubjectCollectionService
from cti_app.application.diagnostics import DiagnosticsLog
from cti_app.application.jobs import (
    JobDispatcher,
    JobExecutionContext,
    JobHandlerError,
    JobParameters,
    JobRegistry,
    JobService,
)
from cti_app.application.model_conversations import ModelConversationService
from cti_app.application.model_gateway import ModelGateway
from cti_app.application.persistence import UnitOfWorkFactory
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.application.production_workflow import ProductionWorkflowOrchestrator
from cti_app.application.subject_production import EditionProductionService
from cti_app.domain.production import (
    ProductionProfile,
    SubjectProductionRun,
    SubjectProductionStage,
    SubjectProductionStatus,
    production_stages,
)

# Every automatic production stage job — SOURCES from the API, and every
# chained stage submitted by ProductionStageChain — shares this retry policy.
PRODUCTION_STAGE_MAX_ATTEMPTS = 3

_TERMINAL_STATUSES = {
    SubjectProductionStatus.READY,
    SubjectProductionStatus.NEEDS_REVIEW,
    SubjectProductionStatus.FAILED,
    SubjectProductionStatus.CANCELLED,
}


class ProductionStageParameters(JobParameters):
    """Parameters for production stage jobs.

    Parameters round-trip through JSON on their way to the worker, so the UUID
    comes back as a string: strict mode would reject it on the way in.
    """

    model_config = ConfigDict(extra="forbid", strict=False)

    run_id: UUID = Field(..., description="Production run ID")
    expected_stage: str = Field(..., description="Expected production stage")
    pipeline_generation: int = Field(0, ge=0, description="Pipeline generation")


def stage_job_kind(stage: SubjectProductionStage) -> str:
    if stage is SubjectProductionStage.ASSEMBLY:
        return "production.subject.assemble"
    return f"production.subject.{stage.value}"


def production_stage_idempotency_key(
    run: SubjectProductionRun, stage: SubjectProductionStage
) -> str:
    return f"production-{stage.value}-{run.id}-g{run.pipeline_generation}"


class ProductionStageChain:
    """Submits the job for the next stage of a run.

    The job registry has to exist before the `JobService` that consumes it, so
    the chain is created first and bound once both are constructed.
    """

    def __init__(self) -> None:
        self._jobs: JobService | None = None
        self._dispatcher: JobDispatcher | None = None

    def bind(self, jobs: JobService, dispatcher: JobDispatcher) -> None:
        self._jobs = jobs
        self._dispatcher = dispatcher

    @property
    def bound(self) -> bool:
        return self._jobs is not None and self._dispatcher is not None

    async def submit(
        self,
        *,
        run: SubjectProductionRun,
        stage: SubjectProductionStage,
        correlation_id: str,
        actor_id: str = "system",
    ) -> UUID | None:
        """Worker attempts share a generation; manual retries get a new one."""
        if self._jobs is None or self._dispatcher is None:
            return None
        parameters = ProductionStageParameters(
            run_id=run.id,
            expected_stage=stage.value,
            pipeline_generation=run.pipeline_generation,
        )
        job = await self._jobs.submit(
            kind=stage_job_kind(stage),
            aggregate_type="subject",
            aggregate_id=run.subject_id,
            idempotency_key=production_stage_idempotency_key(run, stage),
            correlation_id=correlation_id,
            input_parameters=parameters.model_dump(mode="json"),
            max_attempts=PRODUCTION_STAGE_MAX_ATTEMPTS,
            actor_id=actor_id,
        )
        await self._dispatcher.dispatch(job.id)
        return job.id


def register_production_jobs(
    registry: JobRegistry,
    uow_factory: UnitOfWorkFactory,
    *,
    chain: ProductionStageChain | None = None,
    model_service: ModelConversationService | None = None,
    model_gateway: ModelGateway | None = None,
    collection_service: SubjectCollectionService | None = None,
    artifact_store: ProductionArtifactStore | None = None,
    diagnostics: DiagnosticsLog | None = None,
    seed_enrichment: VirusTotalSeedEnrichmentService | None = None,
) -> None:
    """Register the five production stage jobs."""
    stage_chain = chain or ProductionStageChain()

    async def advance_batch(run_id: UUID, correlation_id: str) -> None:
        """Start the next subject of the batch this run belongs to.

        A subject that ends in needs_review or failed must not block the queue,
        so this runs on every terminal outcome, not only on success.
        """
        batches = EditionProductionService(uow_factory)
        async with uow_factory() as uow:
            item = await uow.edition_production_batch_items.get_by_run(run_id)
            if item is None:
                return
            batch_id = item.batch_id

        # The transaction is committed before dispatching, so the worker can
        # never pick the job up before the run is visible as RUNNING.
        started = await batches.on_subject_terminal(batch_id, run_id)
        if started is None:
            return
        await stage_chain.submit(
            run=started,
            stage=SubjectProductionStage.SOURCES,
            correlation_id=correlation_id,
        )

    async def handle_stage(
        parameters: JobParameters,
        context: JobExecutionContext,
    ) -> str:
        if not isinstance(parameters, ProductionStageParameters):
            raise TypeError("Invalid production stage parameters")

        stage = SubjectProductionStage(parameters.expected_stage)
        async with uow_factory() as uow:
            current = await uow.subject_production_runs.get(parameters.run_id)
        if current is None or current.pipeline_generation != parameters.pipeline_generation:
            return f"production-stage://{parameters.run_id}/{stage.value}#superseded"
        orchestrator = ProductionWorkflowOrchestrator(
            uow_factory,
            model_service=model_service,
            model_gateway=model_gateway,
            collection_service=collection_service,
            artifact_store=artifact_store,
            diagnostics=diagnostics,
            seed_enrichment=seed_enrichment,
        )

        correlation_id = await context.correlation_id()
        result = await orchestrator.execute_stage(
            run_id=parameters.run_id,
            expected_stage=stage,
            context=context,
            correlation_id=correlation_id,
        )

        stages = list(production_stages(current.profile))
        stage_index = stages.index(stage)
        await context.report_progress(
            stage_index + 1,
            len(stages),
            f"Étape {stage_index + 1}/{len(stages)} complète",
        )

        async with uow_factory() as uow:
            run = await uow.subject_production_runs.get(parameters.run_id)
        if run is None:
            raise JobHandlerError(
                code="production_run_missing",
                public_message="Le run de production est introuvable.",
                transient=False,
            )
        outcome = str(result.get("status", "success"))
        error_code = str(result.get("error_code") or f"{stage.value}_error")
        error_message = str(result.get("error", "unknown error"))

        # A transient failure must stay retryable and must NOT end the run:
        # the batch keeps its slot and the job is retried.
        if outcome == "transient_error":
            # The generic job service exhausts retries only after the handler
            # returns. Finish the production aggregate here on its last
            # attempt so it cannot remain RUNNING behind a failed terminal job.
            async with uow_factory() as uow:
                job = await uow.jobs.get(context.job_id)
            exhausted = job is not None and job.attempt >= job.max_attempts
            if exhausted:
                async with uow_factory() as uow:
                    ending = await uow.subject_production_runs.get_for_update(parameters.run_id)
                    if ending is not None and ending.status not in _TERMINAL_STATUSES:
                        ending.mark_needs_review(code=error_code, message=error_message)
                        await uow.subject_production_runs.save(ending)
                        await uow.commit()
                await advance_batch(parameters.run_id, correlation_id)
                return f"production-stage://{parameters.run_id}/{stage.value}#needs_review"
            raise JobHandlerError(
                code=error_code,
                public_message=f"Erreur temporaire lors de l'étape {stage.value}",
                transient=True,
            )

        # needs_review is a business outcome, not a crash: the subject stops
        # here for a human, and the batch moves on.
        if outcome in {"needs_review", "terminal_error", "error"}:
            async with uow_factory() as uow:
                ending = await uow.subject_production_runs.get_for_update(parameters.run_id)
                if ending is not None and ending.status not in _TERMINAL_STATUSES:
                    if outcome == "needs_review":
                        ending.mark_needs_review(code=error_code, message=error_message)
                    else:
                        ending.mark_failed(code=error_code, message=error_message)
                    await uow.subject_production_runs.save(ending)
                    await uow.commit()
            await advance_batch(parameters.run_id, correlation_id)
            if outcome == "needs_review":
                return f"production-stage://{parameters.run_id}/{stage.value}#needs_review"
            raise JobHandlerError(
                code=error_code,
                public_message=f"Erreur lors de l'étape {stage.value}",
                transient=False,
            )

        # Assembly is the last stage: it already marked the run ready or
        # needs_review, so the batch moves to the next subject.
        if stage is SubjectProductionStage.ASSEMBLY:
            await advance_batch(parameters.run_id, correlation_id)
            return f"production-stage://{parameters.run_id}/{stage.value}"

        async with uow_factory() as uow:
            advancing = await uow.subject_production_runs.get_for_update(parameters.run_id)
            if advancing is None or advancing.status is not SubjectProductionStatus.RUNNING:
                return f"production-stage://{parameters.run_id}/{stage.value}"
            advancing.advance_stage()
            await uow.subject_production_runs.save(advancing)
            await uow.commit()
            next_stage = advancing.current_stage

        # ANALYST_RESEARCH is a deliberate manual checkpoint in P04.
        if next_stage is SubjectProductionStage.ANALYST_RESEARCH:
            return f"production-stage://{parameters.run_id}/{stage.value}#awaiting_analyst"

        job_id = await stage_chain.submit(
            run=advancing,
            stage=next_stage,
            correlation_id=correlation_id,
        )
        if job_id is None:
            raise JobHandlerError(
                code="production_chain_unbound",
                public_message="La chaîne de production n'est pas configurée.",
                transient=False,
            )
        return f"production-stage://{parameters.run_id}/{stage.value}"

    for stage in SubjectProductionStage:
        registry.register(
            stage_job_kind(stage),
            ProductionStageParameters,
            handle_stage,
            resume_after_worker_loss=True,
        )


__all__ = [
    "ProductionProfile",
    "ProductionStageChain",
    "ProductionStageParameters",
    "production_stage_idempotency_key",
    "register_production_jobs",
    "stage_job_kind",
]
