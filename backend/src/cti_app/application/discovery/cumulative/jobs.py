from __future__ import annotations

from cti_app.application.discovery.cumulative.contracts import ReconcileDiscoveryParameters
from cti_app.application.discovery.cumulative.errors import (
    DiscoveryMergeNeedsReview,
    DiscoverySnapshotStaleError,
    MergeModelUnavailableError,
)
from cti_app.application.discovery.cumulative.service import CumulativeDiscoveryService
from cti_app.application.jobs import (
    JobExecutionContext,
    JobHandlerError,
    JobParameters,
    JobRegistry,
)

RECONCILE_DISCOVERY_JOB_KIND = "reconcile_discovery"


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
