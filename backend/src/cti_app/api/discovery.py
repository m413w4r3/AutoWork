from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException, Query, Request, status
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field

from cti_app.api.discovery_errors import _raise_api_error
from cti_app.application.discovery.contracts import SOURCE_PROFILE_PATTERN
from cti_app.application.discovery.manual_source_edits import (
    ManualSourceEditOriginNotFoundError,
    ManualSourceEditService,
    ManualSourceEditSupersededError,
)
from cti_app.application.discovery.manual_source_edits import (
    SourceCandidateNotFoundError as ManualSourceCandidateNotFoundError,
)
from cti_app.application.discovery.runs import (
    DiscoveryRunNotFoundError,
    DiscoveryRunProjection,
    DiscoveryRunService,
)
from cti_app.application.discovery.service import (
    DiscoveryRunOwnershipError,
    DiscoveryService,
    SourceCandidateNotFoundError,
)
from cti_app.application.identity import IdentityProvider
from cti_app.domain.discovery import (
    DiscoveryBatch,
    DiscoveryCandidate,
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
from cti_app.logging import get_correlation_id

router = APIRouter(prefix="/api/editions/{edition_id}/discovery", tags=["discovery"])
candidate_router = APIRouter(prefix="/api/discovery", tags=["discovery"])


class DiscoveryLaunch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Capped at the durable DiscoveryRun.source_profile width so an accepted
    # request can always be persisted in its immutable request snapshot.
    source_profile: str = Field(
        min_length=1,
        max_length=64,
        pattern=SOURCE_PROFILE_PATTERN.pattern,
    )
    aliases: list[str] = Field(default_factory=list, max_length=30)
    keywords: list[str] = Field(default_factory=list, max_length=100)
    exclusions: list[str] = Field(default_factory=list, max_length=100)
    complementary_axis: str = Field(default="initial", min_length=1, max_length=500)
    sensitivity: str = Field(default="internal", min_length=1, max_length=64)
    external_llm_allowed: bool = True


class DiscoveryReportReprocess(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: UUID
    research_model_run_id: UUID


class DiscoveryJobActionView(BaseModel):
    job_id: UUID
    status: str
    reused: bool


class DiscoveryRunExecutionView(BaseModel):
    job_id: UUID
    status: str
    progress_current: int
    progress_total: int
    user_message: str | None
    error_code: str | None
    error_message: str | None
    error_details: dict[str, object] | None
    started_at: datetime | None
    finished_at: datetime | None


class DiscoveryRunResultView(BaseModel):
    batch_id: UUID
    research_model_run_id: UUID
    archived_report_url: str


class DiscoveryRunView(BaseModel):
    run_id: UUID
    edition_id: UUID
    input_mode: str
    source_profile: str
    complementary_axis: str
    request_snapshot: dict[str, object]
    created_by: str
    created_at: datetime
    execution: DiscoveryRunExecutionView | None
    result: DiscoveryRunResultView | None


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
    # local_ref (e.g. "P3") is unique only within its own batch, so it collides across
    # merged candidates; publication_ids pairs each relation with the surviving
    # SourceCandidate.id (stable across merges via remap_ioc_publication_ids) instead.
    publication_ids: list[UUID]
    warnings: list[str]


class CandidateView(BaseModel):
    id: UUID
    discovery_batch_id: UUID
    discovery_run_id: UUID
    created_at: datetime
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
    discovery_run_id: UUID
    complementary_axis: str
    queries: list[str]
    citations: list[dict[str, str | None]]
    discovery_model_run_id: UUID
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
    warning: str = (
        "Les métadonnées et comptes IOC de découverte sont provisoires. Ils seront vérifiés "
        "depuis les documents archivés après la sélection."
    )


class SourceStatusUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: SourceVerificationStatus


class IncompleteSourceUrlAttachment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str = Field(min_length=1, max_length=4000)


class SourceUrlReplacement(BaseModel):
    model_config = ConfigDict(extra="forbid")

    replaced_canonical_url: str
    url: str


class IncompleteSourceAttachmentView(BaseModel):
    source: SourceView
    updated_subject_ids: list[UUID]


@router.post("/runs", response_model=DiscoveryRunView, status_code=status.HTTP_202_ACCEPTED)
async def launch_discovery(
    edition_id: UUID,
    payload: DiscoveryLaunch,
    request: Request,
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
) -> DiscoveryRunView:
    service: DiscoveryRunService = request.app.state.discovery_run_service
    provider: IdentityProvider = request.app.state.identity_provider
    try:
        identity = await provider.current()
        projection = await service.create_bridge_run(
            edition_id,
            idempotency_key=idempotency_key,
            source_profile=payload.source_profile,
            country_aliases=payload.aliases,
            keywords=payload.keywords,
            exclusions=payload.exclusions,
            complementary_axis=payload.complementary_axis,
            sensitivity=payload.sensitivity,
            external_llm_allowed=payload.external_llm_allowed,
            actor_id=identity.actor_id,
            correlation_id=get_correlation_id(),
        )
        return _discovery_run_view(projection)
    except Exception as exc:
        _raise_api_error(exc)


@router.get("/runs", response_model=list[DiscoveryRunView])
async def list_discovery_runs(edition_id: UUID, request: Request) -> list[DiscoveryRunView]:
    service: DiscoveryRunService = request.app.state.discovery_run_service
    try:
        return [_discovery_run_view(item) for item in await service.list_for_edition(edition_id)]
    except Exception as exc:
        _raise_api_error(exc)


@router.get("/runs/{run_id}", response_model=DiscoveryRunView)
async def read_discovery_run(edition_id: UUID, run_id: UUID, request: Request) -> DiscoveryRunView:
    service: DiscoveryRunService = request.app.state.discovery_run_service
    try:
        projection = await service.get(run_id)
        if projection.run.edition_id != edition_id:
            raise DiscoveryRunNotFoundError(str(run_id))
        return _discovery_run_view(projection)
    except DiscoveryRunNotFoundError as exc:
        raise HTTPException(status_code=404, detail={"code": "discovery_run_not_found"}) from exc
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
    candidates = await service.list_candidates_for_edition(
        edition_id, include_replaced=include_replaced
    )

    filtered: list[DiscoveryCandidate] = []
    for candidate in candidates:
        if search:
            needle = search.casefold()
            if (
                needle not in candidate.title.casefold()
                and needle not in candidate.summary.casefold()
            ):
                continue

        if candidate.technical_potential < min_technical_potential:
            continue

        if source_status is not None:
            if not any(
                source.verification_status is source_status for source in candidate.evidence.sources
            ):
                continue

        filtered.append(candidate)

    key = {
        "newest": lambda item: (item.event_date or date.min, item.title.casefold()),
        "technical": lambda item: (item.technical_potential, item.title.casefold()),
        "novelty": lambda item: (item.novelty.casefold(), item.title.casefold()),
        "title": lambda item: item.title.casefold(),
    }[sort]
    ordered = sorted(filtered, key=key, reverse=sort != "title")

    candidate_views = [_candidate_view(candidate) for candidate in ordered]

    return DiscoveryView(
        batches=[_batch_view(edition_id, batch) for batch in batches],
        candidates=candidate_views,
        total=len(candidate_views),
    )


@router.get("/runs/{run_id}/candidates", response_model=list[CandidateView])
async def read_run_candidates(
    edition_id: UUID,
    run_id: UUID,
    request: Request,
    include_replaced: bool = False,
) -> list[CandidateView]:
    service: DiscoveryService = request.app.state.discovery_service
    try:
        candidates = await service.list_candidates_for_run(
            edition_id, run_id, include_replaced=include_replaced
        )
        return [_candidate_view(candidate) for candidate in candidates]
    except DiscoveryRunOwnershipError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "discovery_run_not_found"},
        ) from exc
    except Exception as exc:
        _raise_api_error(exc)


@candidate_router.get("/candidates/{candidate_id}", response_model=CandidateView)
async def read_candidate(candidate_id: UUID, request: Request) -> CandidateView:
    service: DiscoveryService = request.app.state.discovery_service
    candidate = await service.get_candidate(candidate_id)
    if candidate is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "discovery_candidate_not_found"},
        )
    return _candidate_view(candidate)


@router.post(
    "/reports/reprocess",
    response_model=DiscoveryJobActionView,
    status_code=status.HTTP_202_ACCEPTED,
)
async def reprocess_archived_report(
    edition_id: UUID,
    payload: DiscoveryReportReprocess,
    request: Request,
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
) -> DiscoveryJobActionView:
    service: DiscoveryRunService = request.app.state.discovery_run_service
    provider: IdentityProvider = request.app.state.identity_provider
    try:
        identity = await provider.current()
        _projection, job, reused = await service.reprocess_archived_report(
            edition_id,
            payload.run_id,
            payload.research_model_run_id,
            transport_key=idempotency_key,
            actor_id=identity.actor_id,
            correlation_id=get_correlation_id(),
        )
        return DiscoveryJobActionView(
            job_id=job.id,
            status=job.status.value,
            reused=reused,
        )
    except DiscoveryRunNotFoundError as exc:
        raise HTTPException(status_code=404, detail={"code": "discovery_run_not_found"}) from exc
    except Exception as exc:
        _raise_api_error(exc)


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


@router.patch(
    "/candidates/{candidate_id}/incomplete-sources/{incomplete_source_id}",
    response_model=IncompleteSourceAttachmentView,
)
async def attach_incomplete_source_url(
    edition_id: UUID,
    candidate_id: UUID,
    incomplete_source_id: UUID,
    payload: IncompleteSourceUrlAttachment,
    request: Request,
) -> IncompleteSourceAttachmentView:
    service: ManualSourceEditService = request.app.state.manual_source_edit_service
    provider: IdentityProvider = request.app.state.identity_provider
    try:
        identity = await provider.current()
        result = await service.attach_incomplete_source_url(
            edition_id,
            candidate_id,
            incomplete_source_id,
            payload.url,
            actor_id=identity.actor_id,
        )
        return IncompleteSourceAttachmentView(
            source=_source_view(result.promoted_source),
            updated_subject_ids=list(result.updated_subject_ids),
        )
    except ManualSourceEditSupersededError as exc:
        raise _superseded_candidate_conflict() from exc
    except ManualSourceEditOriginNotFoundError as exc:
        raise _legacy_projection_conflict() from exc
    except Exception as exc:
        _raise_api_error(exc)


@router.patch(
    "/candidates/{candidate_id}/sources/replacement",
    response_model=IncompleteSourceAttachmentView,
)
async def attach_replacement_source_url(
    edition_id: UUID,
    candidate_id: UUID,
    payload: SourceUrlReplacement,
    request: Request,
) -> IncompleteSourceAttachmentView:
    service: ManualSourceEditService = request.app.state.manual_source_edit_service
    provider: IdentityProvider = request.app.state.identity_provider
    try:
        identity = await provider.current()
        result = await service.attach_replacement_source_url(
            edition_id,
            candidate_id,
            payload.replaced_canonical_url,
            payload.url,
            actor_id=identity.actor_id,
        )
        return IncompleteSourceAttachmentView(
            source=_source_view(result.promoted_source),
            updated_subject_ids=list(result.updated_subject_ids),
        )
    except ManualSourceCandidateNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "source_candidate_not_found"},
        ) from exc
    except ManualSourceEditSupersededError as exc:
        raise _superseded_candidate_conflict() from exc
    except ManualSourceEditOriginNotFoundError as exc:
        raise _legacy_projection_conflict() from exc
    except ValueError as exc:
        # canonicalize_http_url rejects malformed replacement URLs at the
        # service boundary; malformed request data is a client error.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "invalid_source_url", "message": str(exc)},
        ) from exc
    except Exception as exc:
        _raise_api_error(exc)


@router.patch("/candidates/{candidate_id}/sources/{source_id}", response_model=SourceView)
async def mark_source(
    edition_id: UUID,
    candidate_id: UUID,
    source_id: UUID,
    payload: SourceStatusUpdate,
    request: Request,
) -> SourceView:
    service: DiscoveryService = request.app.state.discovery_service
    provider: IdentityProvider = request.app.state.identity_provider
    try:
        identity = await provider.current()
        return _source_view(
            await service.mark_source(
                edition_id, candidate_id, source_id, payload.status, actor_id=identity.actor_id
            )
        )
    except SourceCandidateNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "source_candidate_not_found"},
        ) from exc
    except Exception as exc:
        _raise_api_error(exc)


def _superseded_candidate_conflict() -> HTTPException:
    # An earlier correction already published a replacement for this candidate;
    # the client is editing a stale copy and must reload the candidate list.
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={"code": "discovery_candidate_superseded"},
    )


def _legacy_projection_conflict() -> HTTPException:
    # The candidate exists but the temporary cumulative adapter cannot map it to
    # exactly one active legacy subject (not yet consolidated, or ambiguous).
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={"code": "discovery_candidate_not_consolidated"},
    )


def _discovery_run_view(projection: DiscoveryRunProjection) -> DiscoveryRunView:
    run = projection.run
    job = projection.job
    batch = projection.result
    return DiscoveryRunView(
        run_id=run.id,
        edition_id=run.edition_id,
        input_mode=run.input_mode.value,
        source_profile=run.source_profile,
        complementary_axis=run.complementary_axis,
        request_snapshot=run.request_snapshot.model_dump(mode="json"),
        created_by=run.created_by,
        created_at=run.created_at,
        execution=(
            DiscoveryRunExecutionView(
                job_id=job.id,
                status=job.status.value,
                progress_current=job.progress_current,
                progress_total=job.progress_total,
                user_message=job.user_message,
                error_code=job.error_code,
                error_message=job.error_message,
                error_details=job.error_details,
                started_at=job.started_at,
                finished_at=job.finished_at,
            )
            if job is not None
            else None
        ),
        result=(
            DiscoveryRunResultView(
                batch_id=batch.id,
                research_model_run_id=batch.discovery_model_run_id,
                archived_report_url=(
                    f"/api/editions/{run.edition_id}/discovery/reports/"
                    f"{batch.discovery_model_run_id}"
                ),
            )
            if batch is not None
            else None
        ),
    )


def _batch_view(edition_id: UUID, batch: DiscoveryBatch) -> BatchView:
    return BatchView(
        id=batch.id,
        discovery_run_id=batch.discovery_run_id,
        complementary_axis=batch.complementary_axis,
        queries=list(batch.queries),
        citations=list(batch.citations),
        discovery_model_run_id=batch.discovery_model_run_id,
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


def _candidate_view(candidate: DiscoveryCandidate) -> CandidateView:
    """Render exactly what was persisted for this raw candidate.

    No projection through `CandidateTopic`: that parser-side structure re-runs
    publication deduplication and incomplete-URL recovery in its constructor,
    so reading through it would answer with something subtly different from the
    stored evidence a source-verification action operates on.
    """
    evidence = candidate.evidence
    type_counts: dict[str, int] = {}
    for ioc in evidence.provisional_iocs:
        type_counts[ioc.proposed_type.value] = type_counts.get(ioc.proposed_type.value, 0) + 1

    return CandidateView(
        id=candidate.id,
        discovery_batch_id=candidate.discovery_batch_id,
        discovery_run_id=candidate.discovery_run_id,
        created_at=candidate.created_at,
        title=candidate.title,
        summary=candidate.summary,
        novelty=candidate.novelty,
        technical_potential=candidate.technical_potential,
        event_date=candidate.event_date,
        uncertainties=list(evidence.uncertainties),
        relevance_reasons=list(evidence.relevance_reasons),
        actors=list(evidence.actors),
        campaigns=list(evidence.campaigns),
        malware=list(evidence.malware),
        cves=list(evidence.cves),
        victims=list(evidence.victims),
        sectors=list(evidence.sectors),
        countries=list(evidence.countries),
        likely_artifacts=list(evidence.likely_artifacts),
        iocs=list(evidence.iocs),
        provisional_iocs=[_provisional_ioc_view(ioc) for ioc in evidence.provisional_iocs],
        provisional_ioc_count=len(evidence.provisional_iocs),
        provisional_ioc_type_counts=type_counts,
        has_publisher_ioc_count=any(
            source.ioc_declared_count is not None for source in evidence.sources
        ),
        sources=[_source_view(source) for source in evidence.sources],
        incomplete_sources=[
            _incomplete_source_view(source) for source in evidence.incomplete_sources
        ],
        local_ref=candidate.local_ref,
        actor_or_campaign=candidate.actor_or_campaign,
        technical_potential_reason=candidate.technical_potential_reason,
        parsing_warnings=list(evidence.parsing_warnings),
        context_only=candidate.context_only,
        # Projection UI, jamais un statut : une candidate sans source ou
        # purement contextuelle n'est pas matérialisable en Subject.
        selectable=bool(evidence.sources) and not candidate.context_only,
        valid_publication_count=len(evidence.sources),
        incomplete_publication_count=len(evidence.incomplete_sources),
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
        publication_ids=list(
            dict.fromkeys(relation.publication_id for relation in ioc.publication_relations)
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
