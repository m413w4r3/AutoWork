"""Job handlers for production workflow stages."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, cast
from uuid import UUID

from pydantic import ConfigDict, Field

from cti_app.application.analyst_vt_enrichment import VirusTotalSeedEnrichmentService
from cti_app.application.collection import SubjectCollectionService
from cti_app.application.diagnostics import DiagnosticsLog
from cti_app.application.edition_workspace import EditionProductionCheckpointService
from cti_app.application.jobs import (
    DuplicateJobError,
    JobCancelledError,
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
from cti_app.application.production_pacing import ProductionPacingPolicy
from cti_app.application.production_workflow import ProductionWorkflowOrchestrator
from cti_app.application.subject_production import EditionProductionService
from cti_app.domain.production import (
    PRODUCTION_RECONCILIATION_ERROR_CODE,
    ProductionBatchStatus,
    ProductionReconciliationRequiredError,
    ProductionSubmissionReconciliation,
    SubjectProductionRun,
    SubjectProductionStage,
    SubjectProductionStatus,
    model_run_awaits_reconciliation,
    production_stages,
)

# Every automatic production stage job — SOURCES from the API, and every
# chained stage submitted by ProductionStageChain — shares this retry policy.
PRODUCTION_STAGE_MAX_ATTEMPTS = 3
PRODUCTION_RECONCILIATION_RESUME_MAX_ATTEMPTS = 3
PRODUCTION_RECONCILIATION_RESUME_JOB_KIND = "production.subject.reconciliation_resume"

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


class ProductionReconciliationResumeParameters(ProductionStageParameters):
    """Exact adopted output identity carried by a reconciliation resume job."""

    reconciliation_model_run_id: UUID = Field(..., description="Exact adopted ModelRun ID")
    reconciled_output_sha256: str = Field(..., min_length=64, max_length=64)


def stage_job_kind(stage: SubjectProductionStage) -> str:
    if stage is SubjectProductionStage.ASSEMBLY:
        return "production.subject.assemble"
    return f"production.subject.{stage.value}"


def production_stage_idempotency_key(
    run: SubjectProductionRun, stage: SubjectProductionStage
) -> str:
    return f"production-{stage.value}-{run.id}-g{run.pipeline_generation}"


def production_reconciliation_resume_job_kind() -> str:
    return PRODUCTION_RECONCILIATION_RESUME_JOB_KIND


def production_reconciliation_resume_idempotency_key(
    run_id: UUID,
    stage: SubjectProductionStage,
    pipeline_generation: int,
    model_run_id: UUID,
    output_sha256: str,
) -> str:
    return (
        f"production-reconciliation-{run_id}-{stage.value}-g{pipeline_generation}"
        f"-m{model_run_id}-h{output_sha256}"
    )


def _reconciliation_model_run_id(value: object, inherited_exact: bool = False) -> UUID | None:
    """Find an exact backend-produced identity; the UI never parses this tree."""
    if isinstance(value, list):
        for child in value:
            found = _reconciliation_model_run_id(child, inherited_exact)
            if found is not None:
                return found
        return None
    if not isinstance(value, dict):
        return None
    exact = (
        value.get("error_code") == PRODUCTION_RECONCILIATION_ERROR_CODE
        if "error_code" in value
        else inherited_exact
    )
    if exact:
        for key in ("model_run_id", "batch_model_run_id"):
            candidate = value.get(key)
            if isinstance(candidate, str):
                try:
                    return UUID(candidate)
                except ValueError:
                    pass
    for child in value.values():
        found = _reconciliation_model_run_id(child, exact)
        if found is not None:
            return found
    return None


async def _reconciliation_identity(
    uow: Any,
    run: SubjectProductionRun,
    stage: SubjectProductionStage,
    result: dict[str, object],
) -> ProductionSubmissionReconciliation | None:
    if result.get("error_code") != PRODUCTION_RECONCILIATION_ERROR_CODE:
        return None
    model_run_id = _reconciliation_model_run_id(result)
    model_runs = getattr(uow, "model_runs", None)
    if model_run_id is None or model_runs is None:
        return None
    model_run = await model_runs.get(model_run_id)
    # The ModelRun keeps the real bridge reason (active_signal_stalled...) as its
    # error code; only the production run speaks the reconciliation code.
    if model_run is None or not model_run_awaits_reconciliation(model_run.error_code):
        return None
    return ProductionSubmissionReconciliation(
        production_run_id=run.id,
        model_run_id=model_run.id,
        stage=stage,
        bridge_response_id=model_run.response_id,
        submission_state=model_run.submission_state,
        phase="reconciliation",
    )


class ProductionStageChain:
    """Submits the job for the next stage of a run.

    The job registry has to exist before the `JobService` that consumes it, so
    the chain is created first and bound once both are constructed.
    """

    def __init__(self, pacing: ProductionPacingPolicy | None = None) -> None:
        self._jobs: JobService | None = None
        self._dispatcher: JobDispatcher | None = None
        self._pacing = pacing or ProductionPacingPolicy.zero()

    @property
    def pacing(self) -> ProductionPacingPolicy:
        return self._pacing

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
        delay_ms: int | None = None,
        before_dispatch: Callable[[], Awaitable[bool]] | None = None,
    ) -> UUID | None:
        """Worker attempts share a generation; manual retries get a new one."""
        if self._jobs is None or self._dispatcher is None:
            return None
        if run.status is SubjectProductionStatus.CANCELLED:
            raise JobCancelledError
        if run.requires_reconciliation:
            raise ProductionReconciliationRequiredError
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
        if before_dispatch is not None and not await before_dispatch():
            cancel = getattr(self._jobs, "cancel", None)
            if cancel is not None:
                await cancel(job.id, actor_id=actor_id)
            raise JobCancelledError
        await self._dispatcher.dispatch(
            job.id,
            delay_ms=(self._pacing.model_delay_ms(stage) if delay_ms is None else max(0, delay_ms)),
        )
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
    pacing: ProductionPacingPolicy | None = None,
    checkpoint: EditionProductionCheckpointService | None = None,
) -> None:
    """Register the five production stage jobs."""
    stage_chain = chain or ProductionStageChain()
    production_pacing = pacing or stage_chain.pacing

    async def can_dispatch(run_id: UUID, context: JobExecutionContext) -> bool:
        """Re-read the run and its batch immediately before dispatch."""
        await context.check_cancelled()
        async with uow_factory() as uow:
            run = await uow.subject_production_runs.get(run_id)
            if run is None or run.status is not SubjectProductionStatus.RUNNING:
                return False
            item = await uow.edition_production_batch_items.get_by_run(run_id)
            if item is None:
                return True
            batch = await uow.edition_production_batches.get(item.batch_id)
            return batch is not None and batch.status in {
                ProductionBatchStatus.QUEUED,
                ProductionBatchStatus.RUNNING,
            }

    async def advance_batch(
        run_id: UUID,
        correlation_id: str,
        context: JobExecutionContext,
    ) -> None:
        """Start the next subject of the batch this run belongs to.

        A subject that ends in needs_review or failed must not block the queue,
        so this runs on every terminal outcome, not only on success.
        """
        batches = EditionProductionService(uow_factory, production_pacing)
        await context.check_cancelled()
        async with uow_factory() as uow:
            current = await uow.subject_production_runs.get(run_id)
        if current is None or current.status is SubjectProductionStatus.CANCELLED:
            return
        if checkpoint is not None:
            # The projection is deliberately outside the batch transaction and
            # is never allowed to change the terminal production outcome.
            try:
                await checkpoint.checkpoint(run_id, correlation_id=correlation_id)
            except Exception:
                # The concrete service is best-effort too; this guard keeps
                # alternate/test implementations from changing batch semantics.
                pass
        await context.check_cancelled()
        async with uow_factory() as uow:
            item = await uow.edition_production_batch_items.get_by_run(run_id)
            if item is None:
                return
            batch_id = item.batch_id

        # The transaction is committed before dispatching, so the worker can
        # never pick the job up before the run is visible as RUNNING.
        started = await batches.on_subject_terminal(
            batch_id,
            run_id,
            correlation_id=correlation_id,
        )
        if started is None:
            return
        await context.check_cancelled()
        async with uow_factory() as uow:
            latest = await uow.subject_production_runs.get(started.id)
        if latest is None or latest.status is SubjectProductionStatus.CANCELLED:
            return
        subject_delay_ms = await batches.next_dispatch_delay_ms(batch_id)
        if latest.current_stage in {
            SubjectProductionStage.REFERENCES,
            SubjectProductionStage.SYNTHESIS,
        }:
            subject_delay_ms += production_pacing.model_delay_ms(latest.current_stage)
        try:
            await stage_chain.submit(
                run=latest,
                stage=latest.current_stage,
                correlation_id=correlation_id,
                delay_ms=subject_delay_ms,
                before_dispatch=lambda: can_dispatch(started.id, context),
            )
        except (DuplicateJobError, JobCancelledError):
            # A recovered worker may reach this hand-off after the original
            # dispatch already committed. The idempotency key is the proof
            # that the existing job is the exact same stage attempt.  A
            # cancellation can also win the final dispatch fence.
            return

    async def handle_stage(
        parameters: JobParameters,
        context: JobExecutionContext,
    ) -> str:
        if not isinstance(parameters, ProductionStageParameters):
            raise TypeError("Invalid production stage parameters")

        reconciliation_resume = isinstance(parameters, ProductionReconciliationResumeParameters)
        stage = SubjectProductionStage(parameters.expected_stage)
        await context.check_cancelled()
        async with uow_factory() as uow:
            current = await uow.subject_production_runs.get(parameters.run_id)
        if current is None or current.pipeline_generation != parameters.pipeline_generation:
            return f"production-stage://{parameters.run_id}/{stage.value}#superseded"
        if current.status is SubjectProductionStatus.CANCELLED:
            return f"production-stage://{parameters.run_id}/{stage.value}#cancelled"
        if current.requires_reconciliation and not reconciliation_resume:
            return f"production-stage://{parameters.run_id}/{stage.value}#reconciliation_required"
        if reconciliation_resume:
            reconciliation_parameters = cast(ProductionReconciliationResumeParameters, parameters)
            identity = current.reconciliation
            async with uow_factory() as uow:
                adopted = await uow.model_runs.get(
                    reconciliation_parameters.reconciliation_model_run_id
                )
            if (
                current.status is not SubjectProductionStatus.RUNNING
                or identity is None
                or identity.stage is not stage
                or identity.model_run_id != reconciliation_parameters.reconciliation_model_run_id
                or identity.output_sha256 != reconciliation_parameters.reconciled_output_sha256
                or adopted is None
                or adopted.status.value != "succeeded"
                or adopted.raw_output_sha256 != reconciliation_parameters.reconciled_output_sha256
            ):
                return f"production-stage://{parameters.run_id}/{stage.value}#superseded"
        if current.current_stage is not stage:
            if (
                current.status is SubjectProductionStatus.RUNNING
                and current.current_stage in production_stages()
            ):
                await context.check_cancelled()
                async with uow_factory() as uow:
                    current = await uow.subject_production_runs.get(parameters.run_id)
                if current is None or current.status is SubjectProductionStatus.CANCELLED:
                    return f"production-stage://{parameters.run_id}/{stage.value}#superseded"
                try:
                    await stage_chain.submit(
                        run=current,
                        stage=current.current_stage,
                        correlation_id=await context.correlation_id(),
                        before_dispatch=lambda: can_dispatch(parameters.run_id, context),
                    )
                except DuplicateJobError:
                    pass
            return f"production-stage://{parameters.run_id}/{stage.value}#superseded"
        await context.check_cancelled()
        async with uow_factory() as uow:
            current = await uow.subject_production_runs.get(parameters.run_id)
        if current is None or current.status is SubjectProductionStatus.CANCELLED:
            return f"production-stage://{parameters.run_id}/{stage.value}#cancelled"
        await EditionProductionService(uow_factory).clear_next_dispatch(parameters.run_id)
        orchestrator = ProductionWorkflowOrchestrator(
            uow_factory,
            model_service=model_service,
            model_gateway=model_gateway,
            collection_service=collection_service,
            artifact_store=artifact_store,
            diagnostics=diagnostics,
            seed_enrichment=seed_enrichment,
            pacing=production_pacing,
        )

        correlation_id = await context.correlation_id()
        result = await orchestrator.execute_stage(
            run_id=parameters.run_id,
            expected_stage=stage,
            context=context,
            correlation_id=correlation_id,
        )

        await context.check_cancelled()

        stages = list(production_stages())
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
        if run.status is SubjectProductionStatus.CANCELLED:
            return f"production-stage://{parameters.run_id}/{stage.value}#cancelled"
        outcome = str(result.get("status", "success"))
        error_code = str(result.get("error_code") or f"{stage.value}_error")
        error_message = str(result.get("error", "unknown error"))
        details = result.get("details")
        error_details = details if isinstance(details, dict) else None

        # A transient failure must stay retryable and must NOT end the run:
        # the batch keeps its slot and the job is retried.
        if outcome == "transient_error":
            await context.check_cancelled()
            async with uow_factory() as uow:
                current = await uow.subject_production_runs.get(parameters.run_id)
            if current is None or current.status is SubjectProductionStatus.CANCELLED:
                return f"production-stage://{parameters.run_id}/{stage.value}#cancelled"
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
                        reconciliation = await _reconciliation_identity(uow, ending, stage, result)
                        ending.mark_needs_review(
                            code=error_code,
                            message=error_message,
                            details=error_details,
                            reconciliation=reconciliation,
                        )
                        await uow.subject_production_runs.save(ending)
                        await uow.commit()
                await advance_batch(parameters.run_id, correlation_id, context)
                return f"production-stage://{parameters.run_id}/{stage.value}#needs_review"
            raise JobHandlerError(
                code=error_code,
                public_message=f"Erreur temporaire lors de l'étape {stage.value}",
                transient=True,
                details=error_details,
            )

        # needs_review is a business outcome, not a crash: the subject stops
        # here for a human, and the batch moves on.
        if outcome in {"needs_review", "terminal_error", "error"}:
            await context.check_cancelled()
            async with uow_factory() as uow:
                ending = await uow.subject_production_runs.get_for_update(parameters.run_id)
                if ending is not None and ending.status not in _TERMINAL_STATUSES:
                    if outcome == "needs_review":
                        reconciliation = await _reconciliation_identity(uow, ending, stage, result)
                        ending.mark_needs_review(
                            code=error_code,
                            message=error_message,
                            details=error_details,
                            reconciliation=reconciliation,
                        )
                    else:
                        ending.mark_failed(
                            code=error_code, message=error_message, details=error_details
                        )
                    await uow.subject_production_runs.save(ending)
                    await uow.commit()
            await advance_batch(parameters.run_id, correlation_id, context)
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
            await advance_batch(parameters.run_id, correlation_id, context)
            return f"production-stage://{parameters.run_id}/{stage.value}"

        await context.check_cancelled()
        async with uow_factory() as uow:
            advancing = await uow.subject_production_runs.get_for_update(parameters.run_id)
            if advancing is None or advancing.status is not SubjectProductionStatus.RUNNING:
                return f"production-stage://{parameters.run_id}/{stage.value}"
            advancing.advance_stage()
            await uow.subject_production_runs.save(advancing)
            await uow.commit()
            next_stage = advancing.current_stage

        await context.check_cancelled()
        async with uow_factory() as uow:
            latest = await uow.subject_production_runs.get(parameters.run_id)
        if latest is None or latest.status is SubjectProductionStatus.CANCELLED:
            return f"production-stage://{parameters.run_id}/{stage.value}#cancelled"
        job_id = await stage_chain.submit(
            run=latest,
            stage=next_stage,
            correlation_id=correlation_id,
            before_dispatch=lambda: can_dispatch(parameters.run_id, context),
        )
        if job_id is None:
            raise JobHandlerError(
                code="production_chain_unbound",
                public_message="La chaîne de production n'est pas configurée.",
                transient=False,
            )
        return f"production-stage://{parameters.run_id}/{stage.value}"

    for stage in production_stages():
        registry.register(
            stage_job_kind(stage),
            ProductionStageParameters,
            handle_stage,
            resume_after_worker_loss=True,
        )
    registry.register(
        production_reconciliation_resume_job_kind(),
        ProductionReconciliationResumeParameters,
        handle_stage,
        resume_after_worker_loss=True,
    )


__all__ = [
    "PRODUCTION_RECONCILIATION_RESUME_JOB_KIND",
    "PRODUCTION_RECONCILIATION_RESUME_MAX_ATTEMPTS",
    "ProductionReconciliationResumeParameters",
    "ProductionStageChain",
    "ProductionStageParameters",
    "production_reconciliation_resume_idempotency_key",
    "production_reconciliation_resume_job_kind",
    "production_stage_idempotency_key",
    "register_production_jobs",
    "stage_job_kind",
]
