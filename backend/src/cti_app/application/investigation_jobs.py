"""Durable Dramatiq job registration for bounded investigation runs."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Protocol
from uuid import UUID

from pydantic import Field

from cti_app.application.investigation_composition import InvestigationCompositionFactory
from cti_app.application.investigation_state import InvestigationStateVersionConflict
from cti_app.application.jobs import (
    JobExecutionContext,
    JobHandlerError,
    JobParameters,
    JobRegistry,
)
from cti_app.application.persistence import UnitOfWorkFactory
from cti_app.domain.investigation_state import InvestigationWorkingState
from cti_app.domain.jobs import INVESTIGATION_RUN_JOB_KIND

INVESTIGATION_RUN_AGGREGATE_TYPE = "investigation"


class InvestigationRunParameters(JobParameters):
    # Jobs persist JSON UUID strings and are revalidated by the executor.
    investigation_id: UUID = Field(strict=False)
    run_generation: int = Field(ge=1)
    request_key_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class InvestigationRunner(Protocol):
    async def __call__(self, investigation_id: UUID) -> InvestigationWorkingState: ...


def register_investigation_jobs(
    registry: JobRegistry,
    runner: InvestigationRunner,
) -> None:
    async def handler(parameters: JobParameters, context: JobExecutionContext) -> str:
        if not isinstance(parameters, InvestigationRunParameters):
            raise TypeError("Invalid investigation-run parameters")
        await context.report_progress(0, 1, "Enquête en cours")
        # InvestigationService and BridgeInvestigator persist deterministic turn keys. A
        # replay therefore reloads a successful turn or moves an ambiguous one to review.
        try:
            state = await runner(parameters.investigation_id)
        except InvestigationStateVersionConflict as exc:
            # Bounded CAS rebases happen inside InvestigationService. If other
            # concurrent state mutations still prevent a safe merge, let the
            # durable job retry instead of marking the investigation run failed.
            raise JobHandlerError(
                "investigation_state_conflict",
                "Conflit de version concurrent; une nouvelle tentative est planifiée.",
                transient=True,
            ) from exc
        await context.report_progress(1, 1, _public_status(state))
        return f"investigation://{parameters.investigation_id}/runs/{parameters.run_generation}"

    registry.register(
        INVESTIGATION_RUN_JOB_KIND,
        InvestigationRunParameters,
        handler,
        resume_after_worker_loss=True,
    )


def make_investigation_runner(
    uow_factory: UnitOfWorkFactory,
    composition_factory: InvestigationCompositionFactory,
) -> InvestigationRunner:
    async def run(investigation_id: UUID) -> InvestigationWorkingState:
        async with uow_factory() as uow:
            composition = composition_factory.build(uow, investigation_id)
            state = await composition.service.run(investigation_id)
            await uow.commit()
            return state

    return run


def _public_status(state: InvestigationWorkingState) -> str:
    if state.stop_reason is not None:
        return f"Enquête terminée : {state.stop_reason.value}"
    return "Enquête suspendue à un point de reprise durable"


InvestigationRunnerFactory = Callable[[UUID], Awaitable[InvestigationWorkingState]]
