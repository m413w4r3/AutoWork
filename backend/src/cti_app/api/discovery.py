from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Literal, NoReturn
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field

from cti_app.application.discovery import (
    DISCOVERY_JOB_KIND,
    REPROCESS_DISCOVERY_REPORT_JOB_KIND,
    RETRY_STRUCTURING_JOB_KIND,
    DiscoverEditionParameters,
    DiscoveryService,
    RetryStructuringParameters,
    SourceCandidateNotFoundError,
    discovery_idempotency_key,
    discovery_request_hash,
)
from cti_app.application.discovery_consolidation import (
    consolidate_discovery_batches,
)
from cti_app.application.discovery_report_parser import ReportParsingError
from cti_app.application.editions import EditionNotFoundError, EditionService
from cti_app.application.identity import IdentityProvider
from cti_app.application.jobs import (
    DuplicateJobError,
    JobDispatcher,
    JobNotFoundError,
    JobService,
)
from cti_app.application.model_gateway import ModelGatewayError
from cti_app.domain.discovery import (
    CandidateTopic,
    DiscoveryBatch,
    DiscoveryIocType,
    DiscoverySourceMode,
    IncompleteSourceCandidate,
    IocPresence,
    PeriodRelation,
    ProvisionalDiscoveryIoc,
    SourceCandidate,
    SourceRelationshipStatus,
    SourceRole,
    SourceVerificationStatus,
)
from cti_app.domain.editions import Edition, EditionStatus
from cti_app.domain.jobs import Job, JobStatus
from cti_app.logging import get_correlation_id

router = APIRouter(prefix="/api/editions/{edition_id}/discovery", tags=["discovery"])


class DiscoveryLaunch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    country_aliases: list[str] = Field(default_factory=list, max_length=30)
    keywords: list[str] = Field(default_factory=list, max_length=100)
    exclusions: list[str] = Field(default_factory=list, max_length=100)
    complementary_axis: str = Field(default="initial", min_length=1, max_length=500)
    sensitivity: str = Field(default="internal", min_length=1, max_length=64)
    external_llm_allowed: bool = True
    confirm_new_research: bool = False


class DiscoveryLaunchView(BaseModel):
    job_id: UUID
    status: str
    reused: bool


class StructuringRetryLaunch(DiscoveryLaunch):
    research_model_run_id: UUID


class SourceView(BaseModel):
    id: UUID
    url: str
    canonical_url: str
    raw_url: str | None
    local_ref: str | None
    source_ref: str
    title: str
    publisher: str
    role: SourceRole
    published_at: date | None
    event_date: date | None
    citation: str | None
    period_relation: PeriodRelation
    ioc_presence: IocPresence
    ioc_declared_count: int | None
    ioc_visible_count: int | None
    parsing_warnings: list[str]
    verification_status: SourceVerificationStatus
    relationship_status: SourceRelationshipStatus
    verification_changed_at: datetime | None
    verification_changed_by: str | None


class IncompleteSourceView(BaseModel):
    id: UUID
    title: str
    publisher: str
    raw_url: str | None
    local_ref: str | None
    published_at: date | None
    period_relation: PeriodRelation
    role: SourceRole
    ioc_presence: IocPresence
    ioc_declared_count: int | None
    ioc_visible_count: int | None
    parsing_warnings: list[str]


class ProvisionalIocView(BaseModel):
    id: UUID
    raw_value: str
    normalized_value: str | None
    declared_type: str
    proposed_type: DiscoveryIocType
    status: Literal["provisional_visible"]
    publication_refs: list[str]
    warnings: list[str]


class CandidateReferenceView(BaseModel):
    """Reference to a candidate in a batch, used for consolidation tracking."""

    batch_id: UUID
    candidate_id: UUID


class DiscoveryMergeStats(BaseModel):
    """Statistics about consolidation of multiple discovery batches."""

    raw_batch_count: int = Field(description="Total number of active batches")
    raw_candidate_count: int = Field(description="Total number of candidates across all batches")
    consolidated_candidate_count: int = Field(
        description="Number of unique subjects after consolidation"
    )
    unique_publication_count: int = Field(
        description="Total number of unique URLs across consolidated candidates"
    )
    duplicate_publication_occurrence_count: int = Field(
        description="Number of duplicate URL occurrences merged away"
    )


class CandidateView(BaseModel):
    id: UUID
    batch_id: UUID
    title: str
    summary: str
    novelty: str
    technical_potential: int
    event_date: date | None
    uncertainties: list[str]
    relevance_reasons: list[str]
    actors: list[str]
    campaigns: list[str]
    malware: list[str]
    cves: list[str]
    victims: list[str]
    sectors: list[str]
    countries: list[str]
    likely_artifacts: list[str]
    iocs: list[str]
    provisional_iocs: list[ProvisionalIocView]
    provisional_ioc_count: int
    provisional_ioc_type_counts: dict[str, int]
    has_publisher_ioc_count: bool
    editorial_status: Literal["proposed"]
    sources: list[SourceView]
    incomplete_sources: list[IncompleteSourceView]
    local_ref: str | None
    actor_or_campaign: str
    technical_potential_reason: str
    parsing_warnings: list[str]
    context_only: bool
    selectable: bool
    valid_publication_count: int
    incomplete_publication_count: int
    # Consolidation tracking (P2)
    member_references: list[CandidateReferenceView] = Field(default_factory=list)
    contribution_count: int = Field(
        default=1, description="Number of batches contributing to this candidate"
    )
    duplicate_publication_count: int = Field(
        default=0, description="Number of duplicate URLs merged"
    )
    merge_warnings: list[str] = Field(
        default_factory=list, description="Metadata conflicts during consolidation"
    )


class BatchView(BaseModel):
    id: UUID
    complementary_axis: str
    queries: list[str]
    citations: list[dict[str, str | None]]
    discovery_model_run_id: UUID
    structuring_model_run_id: UUID
    created_at: datetime
    source_mode: DiscoverySourceMode
    bridge_capabilities: dict[str, object]
    citation_count: int
    source_coverage_complete: bool
    source_coverage_incomplete_reason: str | None
    report_sha256: str | None
    parser_version: str
    parsing_status: str
    parsing_warnings: list[str]
    unattached_visible_citations: list[dict[str, str | None]]
    parsing_revision: int
    supersedes_batch_id: UUID | None
    replaced_by_batch_id: UUID | None
    is_active_revision: bool
    archived_report_url: str


class DiscoveryView(BaseModel):
    batches: list[BatchView]
    candidates: list[CandidateView]
    total: int
    merge_stats: DiscoveryMergeStats = Field(
        description="Statistics about consolidation of multiple discovery batches"
    )
    warning: str = (
        "Les métadonnées et comptes IOC de découverte sont provisoires. Ils seront vérifiés "
        "depuis les documents archivés après la sélection."
    )


class SourceStatusUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: SourceVerificationStatus


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
    """Import d'une réponse ChatGPT existante (Markdown)."""

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
    """Confirmation d'import avec hash de vérification."""

    expected_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class DiscoveryImportConfirmView(BaseModel):
    """Résultat de l'import d'une réponse ChatGPT."""

    batch_id: UUID
    reused: bool
    source_mode: Literal["manual_import"]
    subject_count: int
    publication_count: int


@router.post("", response_model=DiscoveryLaunchView, status_code=status.HTTP_202_ACCEPTED)
async def launch_discovery(
    edition_id: UUID, payload: DiscoveryLaunch, request: Request
) -> DiscoveryLaunchView:
    editions: EditionService = request.app.state.edition_service
    jobs: JobService = request.app.state.job_service
    dispatcher: JobDispatcher = request.app.state.job_dispatcher
    provider: IdentityProvider = request.app.state.identity_provider
    try:
        edition = await editions.get(edition_id)
        if edition.status in {EditionStatus.PUBLISHED, EditionStatus.ARCHIVED}:
            raise ValueError("A published or archived edition cannot start discovery")
        aliases = list(
            dict.fromkeys([edition.country, edition.country_code, *payload.country_aliases])
        )
        parameters = DiscoverEditionParameters(
            edition_id=edition.id,
            country=edition.country,
            country_aliases=aliases,
            period_start=edition.period_start,
            period_end=edition.period_end,
            languages=list(edition.languages),
            source_profile=edition.source_profile,
            keywords=payload.keywords,
            exclusions=payload.exclusions,
            complementary_axis=payload.complementary_axis,
            tlp=edition.tlp,
            sensitivity=payload.sensitivity,
            external_llm_allowed=payload.external_llm_allowed,
            research_nonce=uuid4() if payload.confirm_new_research else None,
        )
        identity = await provider.current()
        try:
            job = await jobs.submit(
                kind=DISCOVERY_JOB_KIND,
                aggregate_type="edition",
                aggregate_id=edition.id,
                idempotency_key=discovery_idempotency_key(parameters),
                correlation_id=get_correlation_id(),
                input_parameters=parameters.model_dump(mode="json"),
                max_attempts=1,
                actor_id=identity.actor_id,
            )
            await dispatcher.dispatch(job.id)
            return DiscoveryLaunchView(job_id=job.id, status=job.status.value, reused=False)
        except DuplicateJobError as exc:
            job = await jobs.get(exc.existing_job_id)
            return DiscoveryLaunchView(job_id=job.id, status=job.status.value, reused=True)
    except Exception as exc:
        _raise_api_error(exc)


@router.get("/candidates", response_model=DiscoveryView)
async def read_candidates(
    edition_id: UUID,
    request: Request,
    search: Annotated[str | None, Query(max_length=200)] = None,
    min_technical_potential: Annotated[int, Query(ge=0, le=4)] = 0,
    source_status: SourceVerificationStatus | None = None,
    sort: Literal["newest", "technical", "novelty", "title"] = "technical",
    include_replaced: bool = False,
) -> DiscoveryView:
    service: DiscoveryService = request.app.state.discovery_service
    batches = await service.list_batches(edition_id, include_replaced=include_replaced)
    active_batches = [batch for batch in batches if batch.is_active_revision]

    # Consolidate multiple batches into single coherent view
    consolidated = consolidate_discovery_batches(active_batches)

    # Track raw stats before filtering
    raw_batch_count = len(active_batches)
    raw_candidate_count = sum(len(batch.candidates) for batch in active_batches)
    unique_publication_count = sum(len(cand.sources) for cand in consolidated)
    total_duplicate_count = sum(cand.duplicate_publication_count for cand in consolidated)

    # Apply filters to consolidated candidates
    filtered: list[tuple[CandidateTopic, list[CandidateReferenceView], int, int, list[str]]] = []
    for cand in consolidated:
        candidate = cand.representative

        # Search filter
        if search:
            needle = search.casefold()
            if (
                needle not in candidate.title.casefold()
                and needle not in candidate.summary.casefold()
            ):
                continue

        # Technical potential filter
        if candidate.technical_potential < min_technical_potential:
            continue

        # Source status filter
        if source_status is not None:
            if not any(source.verification_status is source_status for source in candidate.sources):
                continue

        # Add to filtered list
        filtered.append(
            (
                candidate,
                [
                    CandidateReferenceView(
                        batch_id=ref.batch_id, candidate_id=ref.candidate_id
                    )
                    for ref in cand.member_references
                ],
                cand.contribution_count,
                cand.duplicate_publication_count,
                cand.merge_warnings,
            )
        )

    # Sort
    key = {
        "newest": lambda item: (item[0].event_date or date.min, item[0].title.casefold()),
        "technical": lambda item: (item[0].technical_potential, item[0].title.casefold()),
        "novelty": lambda item: (item[0].novelty.casefold(), item[0].title.casefold()),
        "title": lambda item: item[0].title.casefold(),
    }[sort]
    ordered = sorted(filtered, key=key, reverse=sort != "title")

    # Build candidate views
    candidate_views = [
        _candidate_view(candidate, references, contribution_count, dup_count, merge_warnings)
        for candidate, references, contribution_count, dup_count, merge_warnings in ordered
    ]

    return DiscoveryView(
        batches=[_batch_view(edition_id, batch) for batch in batches],
        candidates=candidate_views,
        total=len(candidate_views),
        merge_stats=DiscoveryMergeStats(
            raw_batch_count=raw_batch_count,
            raw_candidate_count=raw_candidate_count,
            consolidated_candidate_count=len(consolidated),
            unique_publication_count=unique_publication_count,
            duplicate_publication_occurrence_count=total_duplicate_count,
        ),
    )


@router.get("/reports/{research_model_run_id}", response_class=PlainTextResponse)
async def read_archived_report(
    edition_id: UUID, research_model_run_id: UUID, request: Request
) -> PlainTextResponse:
    service: DiscoveryService = request.app.state.discovery_service
    try:
        report = await service.read_archived_report(edition_id, research_model_run_id)
        return PlainTextResponse(
            report,
            media_type="text/markdown; charset=utf-8",
            headers={"Content-Disposition": 'inline; filename="chatgpt-discovery-report.md"'},
        )
    except Exception as exc:
        _raise_api_error(exc)


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
    """Prévisualiser l'import d'une réponse ChatGPT Markdown existante.

    Ne persiste rien : permet à l'utilisateur de vérifier avant de confirmer.
    """
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
    """Confirmer et archiver l'import d'une réponse ChatGPT Markdown.

    Crée un ModelRun synthétique et un DiscoveryBatch source_mode=manual_import.
    """
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
        batch, reused = await service.import_standalone_report(
            parameters,
            payload.markdown,
            expected_sha256=payload.expected_sha256,
            actor_id=identity.actor_id,
        )

        return DiscoveryImportConfirmView(
            batch_id=batch.id,
            reused=reused,
            source_mode="manual_import",
            subject_count=len(batch.candidates),
            publication_count=sum(len(c.sources) for c in batch.candidates),
        )
    except Exception as exc:
        _raise_api_error(exc)


@router.post(
    "/reports/reprocess",
    response_model=DiscoveryLaunchView,
    status_code=status.HTTP_202_ACCEPTED,
)
@router.post(
    "/structuring/retry",
    response_model=DiscoveryLaunchView,
    status_code=status.HTTP_202_ACCEPTED,
    include_in_schema=False,
)
async def retry_structuring(
    edition_id: UUID, payload: StructuringRetryLaunch, request: Request
) -> DiscoveryLaunchView:
    editions: EditionService = request.app.state.edition_service
    jobs: JobService = request.app.state.job_service
    dispatcher: JobDispatcher = request.app.state.job_dispatcher
    provider: IdentityProvider = request.app.state.identity_provider
    try:
        edition = await editions.get(edition_id)
        aliases = list(
            dict.fromkeys([edition.country, edition.country_code, *payload.country_aliases])
        )
        discovery = DiscoverEditionParameters(
            edition_id=edition.id,
            country=edition.country,
            country_aliases=aliases,
            period_start=edition.period_start,
            period_end=edition.period_end,
            languages=list(edition.languages),
            source_profile=edition.source_profile,
            keywords=payload.keywords,
            exclusions=payload.exclusions,
            complementary_axis=payload.complementary_axis,
            tlp=edition.tlp,
            sensitivity=payload.sensitivity,
            external_llm_allowed=payload.external_llm_allowed,
        )
        nonce = uuid4()
        parameters = RetryStructuringParameters(
            discovery=discovery,
            research_model_run_id=payload.research_model_run_id,
            retry_nonce=nonce,
        )
        identity = await provider.current()
        job = await jobs.submit(
            kind=RETRY_STRUCTURING_JOB_KIND,
            aggregate_type="edition",
            aggregate_id=edition.id,
            idempotency_key=(
                f"retry-discovery-structuring:{edition.id}:{payload.research_model_run_id}:{nonce}"
            ),
            correlation_id=get_correlation_id(),
            input_parameters=parameters.model_dump(mode="json"),
            max_attempts=1,
            actor_id=identity.actor_id,
        )
        await dispatcher.dispatch(job.id)
        return DiscoveryLaunchView(job_id=job.id, status=job.status.value, reused=False)
    except Exception as exc:
        _raise_api_error(exc)


@router.patch("/sources/{source_id}", response_model=SourceView)
async def mark_source(
    edition_id: UUID, source_id: UUID, payload: SourceStatusUpdate, request: Request
) -> SourceView:
    service: DiscoveryService = request.app.state.discovery_service
    provider: IdentityProvider = request.app.state.identity_provider
    try:
        identity = await provider.current()
        return _source_view(
            await service.mark_source(
                edition_id, source_id, payload.status, actor_id=identity.actor_id
            )
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
    """Poursuit le traitement après une récupération (visible ou manuelle).

    Règles :
    - WAITING_HUMAN : reprendre le job existant ;
    - FAILED/CANCELLED : créer un NOUVEAU job local reprocess_discovery_report,
      l'ancien job terminal restant inchangé dans l'historique ;
    - autre statut : retourner le job tel quel.
    """
    jobs: JobService = request.app.state.job_service
    dispatcher: JobDispatcher = request.app.state.job_dispatcher

    if job.status is JobStatus.WAITING_HUMAN:
        resumed = await jobs.resume_waiting_human(job.id, actor_id=actor_id)
        await dispatcher.dispatch(resumed.id)
        return resumed

    if job.status in {JobStatus.FAILED, JobStatus.CANCELLED}:
        parameters = DiscoverEditionParameters.model_validate(job.input_parameters)
        nonce = uuid4()
        # Le reparse relit le ModelRun de recherche archivé, jamais le Job :
        # passer job.id ici rendrait le nouveau job systématiquement en échec.
        retry_parameters = RetryStructuringParameters(
            discovery=parameters,
            research_model_run_id=research_model_run_id,
            retry_nonce=nonce,
        )
        new_job = await jobs.submit(
            kind=REPROCESS_DISCOVERY_REPORT_JOB_KIND,
            aggregate_type="edition",
            aggregate_id=job.aggregate_id,
            idempotency_key=(
                f"recovery-reprocess:{job.id}:{research_model_run_id}:{nonce}"
            ),
            correlation_id=get_correlation_id(),
            input_parameters=retry_parameters.model_dump(mode="json"),
            max_attempts=1,
            actor_id=actor_id,
        )
        await dispatcher.dispatch(new_job.id)
        return new_job

    return job


def _batch_view(edition_id: UUID, batch: DiscoveryBatch) -> BatchView:
    return BatchView(
        id=batch.id,
        complementary_axis=batch.complementary_axis,
        queries=list(batch.queries),
        citations=list(batch.citations),
        discovery_model_run_id=batch.discovery_model_run_id,
        structuring_model_run_id=batch.structuring_model_run_id,
        created_at=batch.created_at,
        source_mode=batch.source_mode,
        bridge_capabilities=batch.bridge_capabilities,
        citation_count=batch.citation_count,
        source_coverage_complete=batch.source_coverage_complete,
        source_coverage_incomplete_reason=batch.source_coverage_incomplete_reason,
        report_sha256=batch.report_sha256,
        parser_version=batch.parser_version,
        parsing_status=batch.parsing_status,
        parsing_warnings=list(batch.parsing_warnings),
        unattached_visible_citations=list(batch.unattached_visible_citations),
        parsing_revision=batch.parsing_revision,
        supersedes_batch_id=batch.supersedes_batch_id,
        replaced_by_batch_id=batch.replaced_by_batch_id,
        is_active_revision=batch.is_active_revision,
        archived_report_url=(
            f"/api/editions/{edition_id}/discovery/reports/{batch.discovery_model_run_id}"
        ),
    )


def _discovery_parameters_from_edition(
    edition: Edition,
    *,
    complementary_axis: str,
    sensitivity: str,
    external_llm_allowed: bool,
    country_aliases: list[str] | None = None,
    keywords: list[str] | None = None,
    exclusions: list[str] | None = None,
    research_nonce: UUID | None = None,
) -> DiscoverEditionParameters:
    """Construit les paramètres de découverte à partir d'une édition.

    Point unique utilisé par le lancement d'une recherche, le retraitement et
    les deux endpoints d'import, pour que le périmètre (pays, période, langues,
    profil de sources) soit identique quel que soit le chemin emprunté.
    """
    aliases = list(
        dict.fromkeys([edition.country, edition.country_code, *(country_aliases or [])])
    )
    return DiscoverEditionParameters(
        edition_id=edition.id,
        country=edition.country,
        country_aliases=aliases,
        period_start=edition.period_start,
        period_end=edition.period_end,
        languages=list(edition.languages),
        source_profile=edition.source_profile,
        keywords=keywords or [],
        exclusions=exclusions or [],
        complementary_axis=complementary_axis,
        tlp=edition.tlp,
        sensitivity=sensitivity,
        external_llm_allowed=external_llm_allowed,
        research_nonce=research_nonce,
    )


def _candidate_view(
    candidate: CandidateTopic,
    member_references: list[CandidateReferenceView] | None = None,
    contribution_count: int = 1,
    duplicate_publication_count: int = 0,
    merge_warnings: list[str] | None = None,
) -> CandidateView:
    """Build a CandidateView with consolidation tracking.

    Args:
        candidate: The representative candidate
        member_references: All member candidates of the cluster, oldest contribution first
        contribution_count: Number of batches contributing to this candidate
        duplicate_publication_count: Number of duplicate URLs merged
        merge_warnings: Metadata conflict warnings
    """
    type_counts: dict[str, int] = {}
    for ioc in candidate.provisional_iocs:
        type_counts[ioc.proposed_type.value] = type_counts.get(ioc.proposed_type.value, 0) + 1

    # Use first member reference's batch_id if available, else candidate's own id
    batch_id = (
        member_references[0].batch_id if member_references else candidate.id
    )

    return CandidateView(
        id=candidate.id,
        batch_id=batch_id,
        title=candidate.title,
        summary=candidate.summary,
        novelty=candidate.novelty,
        technical_potential=candidate.technical_potential,
        event_date=candidate.event_date,
        uncertainties=list(candidate.uncertainties),
        relevance_reasons=list(candidate.relevance_reasons),
        actors=list(candidate.actors),
        campaigns=list(candidate.campaigns),
        malware=list(candidate.malware),
        cves=list(candidate.cves),
        victims=list(candidate.victims),
        sectors=list(candidate.sectors),
        countries=list(candidate.countries),
        likely_artifacts=list(candidate.likely_artifacts),
        iocs=list(candidate.iocs),
        provisional_iocs=[_provisional_ioc_view(ioc) for ioc in candidate.provisional_iocs],
        provisional_ioc_count=len(candidate.provisional_iocs),
        provisional_ioc_type_counts=type_counts,
        has_publisher_ioc_count=any(
            source.ioc_declared_count is not None for source in candidate.sources
        ),
        editorial_status="proposed",
        sources=[_source_view(source) for source in candidate.sources],
        incomplete_sources=[
            _incomplete_source_view(source) for source in candidate.incomplete_sources
        ],
        local_ref=candidate.local_ref,
        actor_or_campaign=candidate.actor_or_campaign,
        technical_potential_reason=candidate.technical_potential_reason,
        parsing_warnings=list(candidate.parsing_warnings),
        context_only=candidate.context_only,
        selectable=candidate.selectable,
        valid_publication_count=len(candidate.sources),
        incomplete_publication_count=len(candidate.incomplete_sources),
        # Consolidation tracking
        member_references=member_references or [],
        contribution_count=contribution_count,
        duplicate_publication_count=duplicate_publication_count,
        merge_warnings=merge_warnings or [],
    )


def _provisional_ioc_view(ioc: ProvisionalDiscoveryIoc) -> ProvisionalIocView:
    return ProvisionalIocView(
        id=ioc.id,
        raw_value=ioc.raw_value,
        normalized_value=ioc.normalized_value,
        declared_type=ioc.declared_type,
        proposed_type=ioc.proposed_type,
        status=ioc.status.value,
        publication_refs=list(
            dict.fromkeys(relation.publication_ref for relation in ioc.publication_relations)
        ),
        warnings=list(ioc.warnings),
    )


def _source_view(source: SourceCandidate) -> SourceView:
    return SourceView(
        id=source.id,
        url=source.url,
        canonical_url=source.canonical_url,
        raw_url=source.raw_url,
        local_ref=source.local_ref,
        source_ref=source.source_ref,
        title=source.title,
        publisher=source.publisher,
        role=source.role,
        published_at=source.published_at,
        event_date=source.event_date,
        citation=source.citation,
        period_relation=source.period_relation,
        ioc_presence=source.ioc_presence,
        ioc_declared_count=source.ioc_declared_count,
        ioc_visible_count=source.ioc_visible_count,
        parsing_warnings=list(source.parsing_warnings),
        verification_status=source.verification_status,
        relationship_status=source.relationship_status,
        verification_changed_at=source.verification_changed_at,
        verification_changed_by=source.verification_changed_by,
    )


def _incomplete_source_view(source: IncompleteSourceCandidate) -> IncompleteSourceView:
    return IncompleteSourceView(
        id=source.id,
        title=source.title,
        publisher=source.publisher,
        raw_url=source.raw_url,
        local_ref=source.local_ref,
        published_at=source.published_at,
        period_relation=source.period_relation,
        role=source.role,
        ioc_presence=source.ioc_presence,
        ioc_declared_count=source.ioc_declared_count,
        ioc_visible_count=source.ioc_visible_count,
        parsing_warnings=list(source.parsing_warnings),
    )


def _raise_api_error(exc: Exception) -> NoReturn:
    if isinstance(exc, EditionNotFoundError):
        raise HTTPException(status_code=404, detail={"code": "edition_not_found"}) from exc
    if isinstance(exc, SourceCandidateNotFoundError):
        raise HTTPException(status_code=404, detail={"code": "source_candidate_not_found"}) from exc
    if isinstance(exc, ReportParsingError):
        status_code = 404 if exc.code == "report_unavailable" else 422
        raise HTTPException(
            status_code=status_code,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc
    if isinstance(exc, ModelGatewayError):
        raise HTTPException(
            status_code=409,
            detail={"code": "recovery_unavailable", "message": str(exc)},
        ) from exc
    if isinstance(exc, ValueError):
        raise HTTPException(
            status_code=422,
            detail={"code": "invalid_discovery", "message": str(exc)},
        ) from exc
    raise exc
