from __future__ import annotations

from collections.abc import Sequence
from copy import deepcopy
from dataclasses import dataclass
from datetime import date, datetime
from typing import Annotated, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field

from cti_app.api.discovery_errors import _raise_api_error
from cti_app.application.discovery.contracts import (
    DiscoverEditionParameters,
    discovery_idempotency_key,
)
from cti_app.application.discovery.cumulative.service import CumulativeDiscoveryService
from cti_app.application.discovery.jobs import DISCOVERY_JOB_KIND
from cti_app.application.discovery.manual_source_edits import ManualSourceEditService
from cti_app.application.discovery.manual_source_edits import (
    SourceCandidateNotFoundError as ManualSourceCandidateNotFoundError,
)
from cti_app.application.discovery.service import DiscoveryService
from cti_app.application.editions import EditionService
from cti_app.application.identity import IdentityProvider
from cti_app.application.jobs import (
    DuplicateJobError,
    JobDispatcher,
    JobService,
)
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
from cti_app.domain.discovery_cumulative import DiscoveryMemberReference
from cti_app.domain.editions import Edition, EditionStatus
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


class CandidateReferenceView(BaseModel):
    batch_id: UUID
    candidate_id: UUID


@dataclass(frozen=True, slots=True)
class SnapshotCandidateProjection:
    representative: CandidateTopic
    member_references: tuple[DiscoveryMemberReference, ...]
    sources: list[SourceCandidate]
    duplicate_publication_count: int = 0
    merge_warnings: tuple[str, ...] = ()

    @property
    def contribution_count(self) -> int:
        return len(self.member_references)


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
    snapshot_version: int | None = None
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

    cumulative: CumulativeDiscoveryService | None = getattr(
        request.app.state, "cumulative_discovery_service", None
    )
    snapshot = await cumulative.active_snapshot(edition_id) if cumulative is not None else None
    if snapshot is not None:
        # Read-only projection of already-materialized state; no consolidation/merge here.
        consolidated = []
        for subject in snapshot.subjects:
            candidate = deepcopy(subject.candidate)
            candidate.id = subject.subject_id
            consolidated.append(
                SnapshotCandidateProjection(
                    representative=candidate,
                    member_references=subject.member_references,
                    sources=candidate.sources,
                )
            )
    else:
        consolidated = []

    raw_batch_count = len(active_batches)
    raw_candidate_count = sum(len(batch.candidates) for batch in active_batches)
    unique_publication_count = sum(len(cand.sources) for cand in consolidated)
    total_duplicate_count = sum(cand.duplicate_publication_count for cand in consolidated)

    filtered: list[
        tuple[CandidateTopic, list[CandidateReferenceView], int, int, tuple[str, ...]]
    ] = []
    for cand in consolidated:
        candidate = cand.representative

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
            if not any(source.verification_status is source_status for source in candidate.sources):
                continue

        filtered.append(
            (
                candidate,
                [
                    CandidateReferenceView(batch_id=ref.batch_id, candidate_id=ref.candidate_id)
                    for ref in cand.member_references
                ],
                cand.contribution_count,
                cand.duplicate_publication_count,
                cand.merge_warnings,
            )
        )

    key = {
        "newest": lambda item: (item[0].event_date or date.min, item[0].title.casefold()),
        "technical": lambda item: (item[0].technical_potential, item[0].title.casefold()),
        "novelty": lambda item: (item[0].novelty.casefold(), item[0].title.casefold()),
        "title": lambda item: item[0].title.casefold(),
    }[sort]
    ordered = sorted(filtered, key=key, reverse=sort != "title")

    candidate_views = [
        _candidate_view(candidate, references, contribution_count, dup_count, merge_warnings)
        for candidate, references, contribution_count, dup_count, merge_warnings in ordered
    ]

    return DiscoveryView(
        batches=[_batch_view(edition_id, batch) for batch in batches],
        candidates=candidate_views,
        total=len(candidate_views),
        snapshot_version=snapshot.version if snapshot else None,
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


@router.patch(
    "/candidates/{subject_id}/incomplete-sources/{incomplete_source_id}",
    response_model=IncompleteSourceAttachmentView,
)
async def attach_incomplete_source_url(
    edition_id: UUID,
    subject_id: UUID,
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
            subject_id,
            incomplete_source_id,
            payload.url,
            actor_id=identity.actor_id,
        )
        return IncompleteSourceAttachmentView(
            source=_source_view(result.promoted_source),
            updated_subject_ids=list(result.updated_subject_ids),
        )
    except Exception as exc:
        _raise_api_error(exc)


@router.patch(
    "/candidates/{subject_id}/sources/replacement",
    response_model=IncompleteSourceAttachmentView,
)
async def attach_replacement_source_url(
    edition_id: UUID,
    subject_id: UUID,
    payload: SourceUrlReplacement,
    request: Request,
) -> IncompleteSourceAttachmentView:
    service: ManualSourceEditService = request.app.state.manual_source_edit_service
    provider: IdentityProvider = request.app.state.identity_provider
    try:
        identity = await provider.current()
        result = await service.attach_replacement_source_url(
            edition_id,
            subject_id,
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
    except ValueError as exc:
        # canonicalize_http_url rejects malformed replacement URLs at the
        # service boundary; malformed request data is a client error.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "invalid_source_url", "message": str(exc)},
        ) from exc
    except Exception as exc:
        _raise_api_error(exc)


def _batch_view(edition_id: UUID, batch: DiscoveryBatch) -> BatchView:
    return BatchView(
        id=batch.id,
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
    # Single source of truth shared by launch, retry and both import endpoints so the scope
    # (country, period, languages, source profile) stays identical across all entry points.
    aliases = list(dict.fromkeys([edition.country, edition.country_code, *(country_aliases or [])]))
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
    merge_warnings: Sequence[str] | None = None,
) -> CandidateView:
    # member_references, when given, lists cluster members oldest contribution first.
    type_counts: dict[str, int] = {}
    for ioc in candidate.provisional_iocs:
        type_counts[ioc.proposed_type.value] = type_counts.get(ioc.proposed_type.value, 0) + 1

    batch_id = member_references[0].batch_id if member_references else candidate.id

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
        member_references=member_references or [],
        contribution_count=contribution_count,
        duplicate_publication_count=duplicate_publication_count,
        merge_warnings=list(merge_warnings or []),
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
