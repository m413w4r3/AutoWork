from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID, uuid4

from cti_app.application.discovery.contracts import (
    ReprocessDiscoveryReportParameters,
    discover_parameters_from_edition,
    discover_parameters_from_run,
    discovery_initial_batch_id,
    discovery_job_idempotency_key,
    discovery_request_snapshot,
)
from cti_app.application.discovery.jobs import (
    DISCOVERY_JOB_KIND,
    REPROCESS_DISCOVERY_REPORT_JOB_KIND,
)
from cti_app.application.editions import EditionNotFoundError
from cti_app.application.jobs import DuplicateJobError, JobDispatcher, JobService
from cti_app.application.persistence import DiscoveryRunUnitOfWorkFactory
from cti_app.domain.discovery import DiscoveryBatch, DiscoveryRun, DiscoveryRunInputMode
from cti_app.domain.editions import EditionStatus
from cti_app.domain.jobs import Job, JobStatus


class DiscoveryRunNotFoundError(LookupError):
    pass


@dataclass(frozen=True, slots=True)
class DiscoveryRunProjection:
    run: DiscoveryRun
    job: Job | None
    result: DiscoveryBatch | None


class DiscoveryRunService:
    def __init__(
        self,
        uow_factory: DiscoveryRunUnitOfWorkFactory,
        jobs: JobService,
        dispatcher: JobDispatcher,
    ) -> None:
        self._uow_factory = uow_factory
        self._jobs = jobs
        self._dispatcher = dispatcher

    async def _require_writable_run(self, edition_id: UUID, run_id: UUID) -> DiscoveryRun:
        async with self._uow_factory() as uow:
            run = await uow.discovery_runs.get(run_id)
            if run is None or run.edition_id != edition_id:
                raise DiscoveryRunNotFoundError(str(run_id))
            edition = await uow.editions.get(edition_id)
            if edition is None:
                raise EditionNotFoundError(str(edition_id))
            if edition.state is EditionStatus.ARCHIVED:
                raise ValueError("An archived edition cannot mutate discovery")
            return run

    async def create_bridge_run(
        self,
        edition_id: UUID,
        *,
        idempotency_key: str,
        source_profile: str,
        country_aliases: list[str],
        keywords: list[str],
        exclusions: list[str],
        complementary_axis: str,
        sensitivity: str,
        external_llm_allowed: bool,
        actor_id: str,
        correlation_id: str,
    ) -> DiscoveryRunProjection:
        if not idempotency_key.strip():
            raise ValueError("Idempotency-Key is required")

        job_id: UUID | None = None
        async with self._uow_factory() as uow:
            run = await uow.discovery_runs.get_by_idempotency_key(
                edition_id, DiscoveryRunInputMode.BRIDGE_RESEARCH, idempotency_key
            )
            if run is None:
                edition = await uow.editions.get(edition_id)
                if edition is None:
                    raise EditionNotFoundError(str(edition_id))
                if edition.state is EditionStatus.ARCHIVED:
                    raise ValueError("An archived edition cannot start discovery")
                run_id = uuid4()
                parameters = discover_parameters_from_edition(
                    edition,
                    discovery_run_id=run_id,
                    source_profile=source_profile,
                    country_aliases=country_aliases,
                    keywords=keywords,
                    exclusions=exclusions,
                    complementary_axis=complementary_axis,
                    sensitivity=sensitivity,
                    external_llm_allowed=external_llm_allowed,
                )
                run = DiscoveryRun(
                    id=run_id,
                    edition_id=edition.id,
                    input_mode=DiscoveryRunInputMode.BRIDGE_RESEARCH,
                    source_profile=parameters.source_profile,
                    complementary_axis=parameters.complementary_axis,
                    request_snapshot=discovery_request_snapshot(parameters),
                    idempotency_key=idempotency_key,
                    created_by=actor_id,
                )
                if not await uow.discovery_runs.add_if_absent(run):
                    run = await uow.discovery_runs.get_by_idempotency_key(
                        edition_id, DiscoveryRunInputMode.BRIDGE_RESEARCH, idempotency_key
                    )
                    if run is None:
                        raise RuntimeError("Discovery run conflict without canonical run")
                else:
                    parameters = discover_parameters_from_run(run)
                    try:
                        job = await self._jobs.submit_in_uow(
                            uow,
                            kind=DISCOVERY_JOB_KIND,
                            aggregate_type="discovery_run",
                            aggregate_id=run.id,
                            idempotency_key=discovery_job_idempotency_key(run.id),
                            correlation_id=correlation_id,
                            input_parameters=parameters.model_dump(mode="json"),
                            max_attempts=1,
                            actor_id=actor_id,
                        )
                    except DuplicateJobError as exc:
                        existing = await uow.jobs.get(exc.existing_job_id)
                        if existing is None:
                            raise RuntimeError(
                                "Discovery job conflict without canonical job"
                            ) from exc
                        job = existing
                    job_id = job.id
            if job_id is None:
                jobs = await uow.jobs.list_for_aggregate(
                    "discovery_run", run.id, kind=DISCOVERY_JOB_KIND
                )
                if not jobs:
                    raise RuntimeError("Discovery run has no discovery job")
                job_id = jobs[0].id
            await uow.commit()

        job = await self._jobs.get(job_id)
        if job.status is JobStatus.QUEUED:
            await self._dispatcher.dispatch(job.id)
        return await self.get(run.id)

    async def reprocess_archived_report(
        self,
        edition_id: UUID,
        run_id: UUID,
        research_model_run_id: UUID,
        *,
        transport_key: str,
        actor_id: str,
        correlation_id: str,
    ) -> tuple[DiscoveryRunProjection, Job, bool]:
        if not transport_key.strip():
            raise ValueError("Idempotency-Key is required")
        run = await self._require_writable_run(edition_id, run_id)
        parameters = ReprocessDiscoveryReportParameters(
            edition_id=edition_id,
            discovery_run_id=run.id,
            research_model_run_id=research_model_run_id,
            actor_id=actor_id,
        )
        key = f"reprocess-discovery-run:{run.id}:{transport_key}"
        reused = False
        async with self._uow_factory() as uow:
            try:
                job = await self._jobs.submit_in_uow(
                    uow,
                    kind=REPROCESS_DISCOVERY_REPORT_JOB_KIND,
                    aggregate_type="discovery_run",
                    aggregate_id=run.id,
                    idempotency_key=key,
                    correlation_id=correlation_id,
                    input_parameters=parameters.model_dump(mode="json"),
                    max_attempts=1,
                    actor_id=actor_id,
                )
            except DuplicateJobError as exc:
                reused = True
                existing = await uow.jobs.get(exc.existing_job_id)
                if existing is None:
                    raise RuntimeError(
                        "Reprocessing job conflict without canonical job"
                    ) from exc
                job = existing
            await uow.commit()
        job = await self._jobs.get(job.id)
        if job.status is JobStatus.QUEUED:
            await self._dispatcher.dispatch(job.id)
        return await self.get(run.id), job, reused

    async def list_for_edition(self, edition_id: UUID) -> list[DiscoveryRunProjection]:
        async with self._uow_factory() as uow:
            runs = list(await uow.discovery_runs.list_for_edition(edition_id))
        return [await self._projection(run) for run in runs]

    async def get(self, run_id: UUID) -> DiscoveryRunProjection:
        async with self._uow_factory() as uow:
            run = await uow.discovery_runs.get(run_id)
            if run is None:
                raise DiscoveryRunNotFoundError(str(run_id))
        return await self._projection(run)

    async def _projection(self, run: DiscoveryRun) -> DiscoveryRunProjection:
        jobs = await self._jobs.list_for_aggregate("discovery_run", run.id)
        job = jobs[0] if jobs else None
        async with self._uow_factory() as uow:
            result = await uow.discovery_batches.get(discovery_initial_batch_id(run.id))
            visited: set[UUID] = set()
            while result is not None and result.replaced_by_batch_id is not None:
                if result.id in visited:
                    raise RuntimeError("Discovery batch replacement cycle")
                visited.add(result.id)
                replacement = await uow.discovery_batches.get(result.replaced_by_batch_id)
                if replacement is None or replacement.discovery_run_id != run.id:
                    break
                result = replacement
        return DiscoveryRunProjection(run=run, job=job, result=result)
