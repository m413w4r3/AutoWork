from __future__ import annotations

from typing import Literal
from uuid import NAMESPACE_URL, UUID, uuid5

from fastapi import APIRouter, Request, status
from pydantic import BaseModel, ConfigDict, Field

from cti_app.api.discovery import (
    DiscoveryLaunchView,
    _discovery_parameters_from_edition,
)
from cti_app.api.discovery_errors import _raise_api_error
from cti_app.application.discovery.contracts import (
    DiscoverEditionParameters,
    discovery_request_hash,
)
from cti_app.application.discovery.jobs import DISCOVERY_JOB_KIND
from cti_app.application.discovery.service import DiscoveryService
from cti_app.application.identity import IdentityProvider
from cti_app.application.jobs import (
    JobDispatcher,
    JobNotFoundError,
    JobService,
)
from cti_app.domain.editions import EditionStatus
from cti_app.domain.jobs import Job, JobStatus

router = APIRouter(prefix="/api/editions/{edition_id}/discovery", tags=["discovery"])


class RecoveryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: UUID


class RecoveryConfirmation(RecoveryRequest):
    expected_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ManualRecoveryRequest(RecoveryRequest):
    markdown: str = Field(min_length=1, max_length=10_000_000)


class ManualRecoveryConfirmation(ManualRecoveryRequest):
    expected_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class RecoveryPreviewView(BaseModel):
    sha256: str
    subject_count: int
    publication_count: int
    ioc_count: int
    ioc_type_counts: dict[str, int]
    warnings: list[str]
    subjects: list[str]


class DiscoveryImportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    markdown: str = Field(min_length=1, max_length=10_000_000)
    complementary_axis: str = Field(
        default="manual-import",
        min_length=1,
        max_length=500,
    )
    sensitivity: str = Field(default="internal", min_length=1, max_length=64)
    external_llm_allowed: bool = True


class DiscoveryImportConfirmation(DiscoveryImportRequest):
    expected_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class DiscoveryImportConfirmView(BaseModel):
    batch_id: UUID
    reused: bool
    source_mode: Literal["manual_import"]
    subject_count: int
    publication_count: int
    # Consolidation runs async after this call returns; None when reused=True (already
    # consolidated by an earlier confirm). Poll this job and refresh only once terminal —
    # refreshing immediately races it and can show 0 consolidated subjects.
    reconciliation_job_id: UUID | None = None


@router.post(
    "/recovery/{research_model_run_id}/visible/preview",
    response_model=RecoveryPreviewView,
)
async def preview_visible_recovery(
    edition_id: UUID,
    research_model_run_id: UUID,
    payload: RecoveryRequest,
    request: Request,
) -> RecoveryPreviewView:
    service: DiscoveryService = request.app.state.discovery_service
    try:
        parameters, _ = await _recovery_context(
            edition_id, research_model_run_id, payload.job_id, request
        )
        return RecoveryPreviewView.model_validate(
            await service.preview_visible_recovery(parameters, research_model_run_id)
        )
    except Exception as exc:
        _raise_api_error(exc)


@router.post(
    "/recovery/{research_model_run_id}/visible/confirm",
    response_model=DiscoveryLaunchView,
    status_code=status.HTTP_202_ACCEPTED,
)
async def confirm_visible_recovery(
    edition_id: UUID,
    research_model_run_id: UUID,
    payload: RecoveryConfirmation,
    request: Request,
) -> DiscoveryLaunchView:
    service: DiscoveryService = request.app.state.discovery_service
    try:
        parameters, job = await _recovery_context(
            edition_id, research_model_run_id, payload.job_id, request
        )
        identity: IdentityProvider = request.app.state.identity_provider
        actor = await identity.current()
        await service.adopt_visible_recovery(
            parameters,
            research_model_run_id,
            expected_sha256=payload.expected_sha256,
            actor_id=actor.actor_id,
        )
        resumed = await _continue_after_recovery(
            job, research_model_run_id, actor.actor_id, request
        )
        return DiscoveryLaunchView(job_id=resumed.id, status=resumed.status.value, reused=True)
    except Exception as exc:
        _raise_api_error(exc)


@router.post(
    "/recovery/{research_model_run_id}/manual/preview",
    response_model=RecoveryPreviewView,
)
async def preview_manual_recovery(
    edition_id: UUID,
    research_model_run_id: UUID,
    payload: ManualRecoveryRequest,
    request: Request,
) -> RecoveryPreviewView:
    service: DiscoveryService = request.app.state.discovery_service
    try:
        parameters, _ = await _recovery_context(
            edition_id, research_model_run_id, payload.job_id, request
        )
        return RecoveryPreviewView.model_validate(
            await service.preview_manual_recovery(
                parameters, research_model_run_id, payload.markdown
            )
        )
    except Exception as exc:
        _raise_api_error(exc)


@router.post(
    "/recovery/{research_model_run_id}/manual/confirm",
    response_model=DiscoveryLaunchView,
    status_code=status.HTTP_202_ACCEPTED,
)
async def confirm_manual_recovery(
    edition_id: UUID,
    research_model_run_id: UUID,
    payload: ManualRecoveryConfirmation,
    request: Request,
) -> DiscoveryLaunchView:
    service: DiscoveryService = request.app.state.discovery_service
    try:
        parameters, job = await _recovery_context(
            edition_id, research_model_run_id, payload.job_id, request
        )
        identity: IdentityProvider = request.app.state.identity_provider
        actor = await identity.current()
        await service.adopt_recovery_report(
            parameters,
            research_model_run_id,
            payload.markdown,
            expected_sha256=payload.expected_sha256,
            provenance="manual_import",
            actor_id=actor.actor_id,
        )
        resumed = await _continue_after_recovery(
            job, research_model_run_id, actor.actor_id, request
        )
        return DiscoveryLaunchView(job_id=resumed.id, status=resumed.status.value, reused=True)
    except Exception as exc:
        _raise_api_error(exc)


@router.post(
    "/recovery/{research_model_run_id}/complete",
    response_model=DiscoveryLaunchView,
    status_code=status.HTTP_202_ACCEPTED,
)
async def request_completion_recovery(
    edition_id: UUID,
    research_model_run_id: UUID,
    payload: RecoveryRequest,
    request: Request,
) -> DiscoveryLaunchView:
    service: DiscoveryService = request.app.state.discovery_service
    try:
        parameters, job = await _recovery_context(
            edition_id, research_model_run_id, payload.job_id, request
        )
        identity: IdentityProvider = request.app.state.identity_provider
        actor = await identity.current()
        await service.start_completion_recovery(parameters, research_model_run_id)
        resumed = await _continue_after_recovery(
            job, research_model_run_id, actor.actor_id, request
        )
        return DiscoveryLaunchView(job_id=resumed.id, status=resumed.status.value, reused=True)
    except Exception as exc:
        _raise_api_error(exc)


@router.post(
    "/import/preview",
    response_model=RecoveryPreviewView,
)
async def preview_discovery_import(
    edition_id: UUID, payload: DiscoveryImportRequest, request: Request
) -> RecoveryPreviewView:
    # Persists nothing; lets the caller verify before confirming.
    service: DiscoveryService = request.app.state.discovery_service
    try:
        edition = await request.app.state.edition_service.get(edition_id)
        if edition.status in {EditionStatus.PUBLISHED, EditionStatus.ARCHIVED}:
            raise ValueError("A published or archived edition cannot import discovery")

        parameters = _discovery_parameters_from_edition(
            edition,
            complementary_axis=payload.complementary_axis,
            sensitivity=payload.sensitivity,
            external_llm_allowed=payload.external_llm_allowed,
        )
        return RecoveryPreviewView.model_validate(
            await service.preview_standalone_import(parameters, payload.markdown)
        )
    except Exception as exc:
        _raise_api_error(exc)


@router.post(
    "/import/confirm",
    response_model=DiscoveryImportConfirmView,
)
async def confirm_discovery_import(
    edition_id: UUID, payload: DiscoveryImportConfirmation, request: Request
) -> DiscoveryImportConfirmView:
    # Creates a synthetic ModelRun and a DiscoveryBatch with source_mode=manual_import.
    service: DiscoveryService = request.app.state.discovery_service
    provider: IdentityProvider = request.app.state.identity_provider
    try:
        edition = await request.app.state.edition_service.get(edition_id)
        if edition.status in {EditionStatus.PUBLISHED, EditionStatus.ARCHIVED}:
            raise ValueError("A published or archived edition cannot import discovery")

        identity = await provider.current()
        parameters = _discovery_parameters_from_edition(
            edition,
            complementary_axis=payload.complementary_axis,
            sensitivity=payload.sensitivity,
            external_llm_allowed=payload.external_llm_allowed,
        )
        batch, reused, reconciliation_job_id = await service.import_standalone_report(
            parameters,
            payload.markdown,
            expected_sha256=payload.expected_sha256,
            actor_id=identity.actor_id,
        )

        return DiscoveryImportConfirmView(
            batch_id=batch.id,
            reused=reused,
            reconciliation_job_id=reconciliation_job_id,
            source_mode="manual_import",
            subject_count=len(batch.candidates),
            publication_count=sum(len(c.sources) for c in batch.candidates),
        )
    except Exception as exc:
        _raise_api_error(exc)


async def _recovery_context(
    edition_id: UUID,
    research_model_run_id: UUID,
    job_id: UUID,
    request: Request,
) -> tuple[DiscoverEditionParameters, Job]:
    jobs: JobService = request.app.state.job_service
    try:
        job = await jobs.get(job_id)
    except JobNotFoundError as exc:
        raise ValueError("Recovery job does not exist") from exc
    if (
        job.kind != DISCOVERY_JOB_KIND
        or job.aggregate_type != "edition"
        or job.aggregate_id != edition_id
        or job.status
        not in {
            JobStatus.WAITING_HUMAN,
            JobStatus.QUEUED,
            JobStatus.RUNNING,
            JobStatus.SUCCEEDED,
            JobStatus.FAILED,
            JobStatus.CANCELLED,
        }
    ):
        raise ValueError("Job is not waiting for this discovery recovery")
    parameters = DiscoverEditionParameters.model_validate(job.input_parameters)
    details = job.error_details or {}
    expected_original = uuid5(
        NAMESPACE_URL,
        f"cti-discovery-model-run:{discovery_request_hash(parameters)}",
    )
    if (
        details.get("model_run_id") != str(research_model_run_id)
        and research_model_run_id != expected_original
    ):
        raise ValueError("ModelRun does not belong to this recovery job")
    return parameters, job


async def _continue_after_recovery(
    job: Job,
    research_model_run_id: UUID,
    actor_id: str,
    request: Request,
) -> Job:
    jobs: JobService = request.app.state.job_service
    dispatcher: JobDispatcher = request.app.state.job_dispatcher

    if job.status is JobStatus.WAITING_HUMAN:
        resumed = await jobs.resume_waiting_human(job.id, actor_id=actor_id)
        await dispatcher.dispatch(resumed.id)
        return resumed

    return job
