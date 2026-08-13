from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Literal, NoReturn
from uuid import UUID, uuid4

from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field

from cti_app.application.discovery import (
    DISCOVERY_JOB_KIND,
    RETRY_STRUCTURING_JOB_KIND,
    DiscoverEditionParameters,
    DiscoveryService,
    RetryStructuringParameters,
    SourceCandidateNotFoundError,
    discovery_idempotency_key,
)
from cti_app.application.discovery_report_parser import ReportParsingError
from cti_app.application.editions import EditionNotFoundError, EditionService
from cti_app.application.identity import IdentityProvider
from cti_app.application.jobs import DuplicateJobError, JobDispatcher, JobService
from cti_app.domain.discovery import (
    CandidateTopic,
    DiscoveryBatch,
    DiscoverySourceMode,
    IncompleteSourceCandidate,
    IocPresence,
    PeriodRelation,
    SourceCandidate,
    SourceRelationshipStatus,
    SourceRole,
    SourceVerificationStatus,
)
from cti_app.domain.editions import EditionStatus
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
    archived_report_url: str


class DiscoveryView(BaseModel):
    batches: list[BatchView]
    candidates: list[CandidateView]
    total: int
    warning: str = (
        "Les métadonnées et comptes IOC de découverte sont provisoires. Ils seront vérifiés "
        "depuis les documents archivés après la sélection."
    )


class SourceStatusUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: SourceVerificationStatus


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
                max_attempts=3,
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
) -> DiscoveryView:
    service: DiscoveryService = request.app.state.discovery_service
    batches = await service.list_batches(edition_id)
    candidates = [(batch.id, candidate) for batch in batches for candidate in batch.candidates]
    if search:
        needle = search.casefold()
        candidates = [
            item
            for item in candidates
            if needle in item[1].title.casefold() or needle in item[1].summary.casefold()
        ]
    candidates = [
        item for item in candidates if item[1].technical_potential >= min_technical_potential
    ]
    if source_status is not None:
        candidates = [
            item
            for item in candidates
            if any(source.verification_status is source_status for source in item[1].sources)
        ]
    key = {
        "newest": lambda item: (item[1].event_date or date.min, item[1].title.casefold()),
        "technical": lambda item: (item[1].technical_potential, item[1].title.casefold()),
        "novelty": lambda item: (item[1].novelty.casefold(), item[1].title.casefold()),
        "title": lambda item: item[1].title.casefold(),
    }[sort]
    ordered = sorted(candidates, key=key, reverse=sort != "title")
    return DiscoveryView(
        batches=[_batch_view(edition_id, batch) for batch in batches],
        candidates=[_candidate_view(batch_id, candidate) for batch_id, candidate in ordered],
        total=len(ordered),
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
        archived_report_url=(
            f"/api/editions/{edition_id}/discovery/reports/{batch.discovery_model_run_id}"
        ),
    )


def _candidate_view(batch_id: UUID, candidate: CandidateTopic) -> CandidateView:
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
    if isinstance(exc, ValueError):
        raise HTTPException(
            status_code=422,
            detail={"code": "invalid_discovery", "message": str(exc)},
        ) from exc
    raise exc
