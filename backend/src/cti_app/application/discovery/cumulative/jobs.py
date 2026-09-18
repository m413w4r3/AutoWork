from __future__ import annotations

from cti_app.application.discovery.cumulative.contracts import ReconcileDiscoveryParameters
from cti_app.application.discovery.cumulative.errors import (
    DiscoveryMergeNeedsReview,
    DiscoverySnapshotStaleError,
    MergeModelUnavailableError,
)
from cti_app.application.discovery.cumulative.service import CumulativeDiscoveryService
from cti_app.application.jobs import (
    DuplicateJobError,
    JobDispatcher,
    JobExecutionContext,
    JobHandlerError,
    JobParameters,
    JobRegistry,
    JobService,
)
from cti_app.domain.discovery import DiscoveryBatch
from cti_app.domain.discovery_cumulative import DiscoveryInputMode
from cti_app.domain.jobs import Job, JobStatus

RECONCILE_DISCOVERY_JOB_KIND = "reconcile_discovery"


async def ensure_discovery_reconciliation_job(
    batch: DiscoveryBatch,
    *,
    input_mode: DiscoveryInputMode,
    actor_id: str,
    cumulative_discovery_service: CumulativeDiscoveryService,
    job_service: JobService,
    job_dispatcher: JobDispatcher,
    correlation_id: str,
) -> Job:
    """Handoff cumulative d'un DiscoveryBatch persisté, rejouable à l'identique.

    Le batch est déjà durable quand on arrive ici : l'intake et le Job de
    réconciliation sont donc adoptés plutôt que recréés si une tentative
    précédente est morte en route. Le dispatch reste après le commit de
    `JobService.submit`, et un échec de dispatch remonte, pour qu'un parent ne
    puisse jamais conclure sur un enfant durable jamais remis à l'exécuteur.
    """
    intake, _ = await cumulative_discovery_service.ingest_batch(
        batch,
        input_mode=input_mode,
        actor_id=actor_id,
    )
    parent = await cumulative_discovery_service.active_snapshot(batch.edition_id)
    parameters = ReconcileDiscoveryParameters(
        intake_id=intake.id,
        edition_id=batch.edition_id,
        expected_parent_snapshot_id=parent.id if parent else None,
        actor_id=actor_id,
    )
    try:
        job = await job_service.submit(
            kind=RECONCILE_DISCOVERY_JOB_KIND,
            aggregate_type="edition",
            aggregate_id=batch.edition_id,
            idempotency_key=f"reconcile-discovery:{intake.id}",
            correlation_id=correlation_id,
            input_parameters=parameters.model_dump(mode="json"),
            max_attempts=3,
            actor_id=actor_id,
        )
    except DuplicateJobError as exc:
        existing = await job_service.get(exc.existing_job_id)
        # QUEUED sans retry planifié = la ligne Job a été committée puis le
        # dispatch a été perdu. C'est le seul état qu'on redispatche : un
        # `next_retry_at` appartient au backoff, et un état RUNNING ou terminal
        # signifie que le handoff a déjà atteint l'exécuteur.
        if existing.status is JobStatus.QUEUED and existing.next_retry_at is None:
            await job_dispatcher.dispatch(existing.id)
        return existing
    await job_dispatcher.dispatch(job.id)
    return job


def register_cumulative_discovery_jobs(
    registry: JobRegistry, service: CumulativeDiscoveryService
) -> None:
    async def handler(parameters: JobParameters, context: JobExecutionContext) -> str:
        if not isinstance(parameters, ReconcileDiscoveryParameters):
            raise TypeError("Invalid cumulative discovery reconciliation parameters")
        await context.report_progress(1, 2, "Réconciliation de la découverte cumulative")
        try:
            snapshot = await service.reconcile_intake(
                parameters.intake_id,
                expected_parent_snapshot_id=parameters.expected_parent_snapshot_id,
                actor_id=parameters.actor_id,
                rebase_count=parameters.rebase_count,
            )
        except DiscoverySnapshotStaleError as exc:
            await context.wait_for_human(
                "La réconciliation a dépassé la limite de rebase.",
                {"reason": str(exc), "intake_id": str(parameters.intake_id)},
            )
        except MergeModelUnavailableError as exc:
            # No plan exists to review, so parking this for a human would create
            # an empty merge run nobody can resolve. Retry instead: the bridge
            # stalling is an incident, not an editorial decision.
            raise JobHandlerError(
                exc.code,
                "Le modèle de fusion n'a pas répondu ; nouvelle tentative programmée.",
                transient=True,
                details={
                    "intake_id": str(parameters.intake_id),
                    "merge_model_run_id": (
                        str(exc.merge_model_run_id) if exc.merge_model_run_id else None
                    ),
                },
            ) from exc
        except DiscoveryMergeNeedsReview as exc:
            await context.wait_for_human(
                "La réconciliation nécessite une décision humaine.",
                {
                    "merge_run_id": str(exc.run_id),
                    "reasons": list(exc.reasons),
                    "intake_id": str(parameters.intake_id),
                },
            )
        await context.report_progress(2, 2, "Nouveau snapshot de découverte activé")
        return f"discovery-snapshot://{snapshot.id}"

    registry.register(
        RECONCILE_DISCOVERY_JOB_KIND,
        ReconcileDiscoveryParameters,
        handler,
        resume_after_worker_loss=True,
    )
