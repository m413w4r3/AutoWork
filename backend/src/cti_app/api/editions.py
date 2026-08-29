import calendar
import re
from datetime import date, datetime
from typing import Annotated, NoReturn, TypedDict
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field

from cti_app.application.editions import (
    DuplicateEditionError,
    EditionConcurrencyError,
    EditionNotFoundError,
    EditionPage,
    EditionService,
    EditionTransitionRequiresUseCaseError,
    PreviousEditionError,
)
from cti_app.application.identity import Identity, IdentityProvider
from cti_app.domain.classification import TLP
from cti_app.domain.editions import (
    Edition,
    EditionAuditEvent,
    EditionImmutableError,
    EditionStatus,
    InvalidEditionTransitionError,
)
from cti_app.domain.errors import TlpDowngradeError
from cti_app.logging import get_correlation_id

router = APIRouter(prefix="/api/editions", tags=["editions"])
MONTH_PATTERN = re.compile(r"^(\d{4})-(\d{2})$")


class EditionFields(BaseModel):
    model_config = ConfigDict(extra="forbid")

    country: str = Field(min_length=2, max_length=100)
    country_code: str = Field(pattern=r"^[A-Za-z]{2}$")
    period_start: date
    period_end: date
    tlp: TLP
    languages: list[str] = Field(min_length=1, max_length=10)
    target_articles: int = Field(ge=0, le=120)
    previous_edition_id: UUID | None = None
    source_profile: str = Field(min_length=1, max_length=128)


class EditionCreate(EditionFields):
    pass


class EditionUpdate(EditionFields):
    version: int = Field(ge=1)


class EditionTransition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_status: EditionStatus
    version: int = Field(ge=1)


class EditionView(EditionFields):
    id: UUID
    status: EditionStatus
    version: int
    progress_percent: int
    allowed_transitions: list[EditionStatus]
    created_at: datetime
    updated_at: datetime


class EditionPageView(BaseModel):
    items: list[EditionView]
    total: int
    page: int
    page_size: int


class EditionAuditView(BaseModel):
    id: UUID
    edition_id: UUID
    actor_id: str
    action: str
    before: dict[str, object] | None
    after: dict[str, object]
    correlation_id: str
    occurred_at: datetime


@router.post("", response_model=EditionView, status_code=status.HTTP_201_CREATED)
async def create_edition(payload: EditionCreate, request: Request) -> EditionView:
    service, identity = await _runtime(request)
    try:
        edition = await service.create(
            **_field_arguments(payload),
            actor_id=identity.actor_id,
            correlation_id=get_correlation_id(),
        )
        return _edition_view(edition)
    except Exception as exc:
        _raise_api_error(exc)


@router.get("", response_model=EditionPageView)
async def list_editions(
    request: Request,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    country_code: Annotated[str | None, Query(pattern=r"^[A-Za-z]{2}$")] = None,
    period: Annotated[str | None, Query(pattern=r"^\d{4}-\d{2}$")] = None,
    edition_status: Annotated[EditionStatus | None, Query(alias="status")] = None,
) -> EditionPageView:
    service, _ = await _runtime(request)
    period_start, period_end = _parse_month(period) if period else (None, None)
    page_result = await service.list(
        page=page,
        page_size=page_size,
        country_code=country_code,
        period_start=period_start,
        period_end=period_end,
        status=edition_status,
    )
    return _page_view(page_result)


@router.get("/{edition_id}", response_model=EditionView)
async def get_edition(edition_id: UUID, request: Request) -> EditionView:
    service, _ = await _runtime(request)
    try:
        return _edition_view(await service.get(edition_id))
    except Exception as exc:
        _raise_api_error(exc)


@router.put("/{edition_id}", response_model=EditionView)
async def update_edition(edition_id: UUID, payload: EditionUpdate, request: Request) -> EditionView:
    service, identity = await _runtime(request)
    try:
        edition = await service.update(
            edition_id,
            expected_version=payload.version,
            **_field_arguments(payload),
            actor_id=identity.actor_id,
            correlation_id=get_correlation_id(),
        )
        return _edition_view(edition)
    except Exception as exc:
        _raise_api_error(exc)


@router.delete("/{edition_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_edition(
    edition_id: UUID,
    request: Request,
    version: Annotated[int, Query(ge=1)],
) -> Response:
    service, _ = await _runtime(request)
    try:
        await service.delete(edition_id, expected_version=version)
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    except Exception as exc:
        _raise_api_error(exc)


@router.post("/{edition_id}/transitions", response_model=EditionView)
async def transition_edition(
    edition_id: UUID, payload: EditionTransition, request: Request
) -> EditionView:
    service, identity = await _runtime(request)
    try:
        edition = await service.transition(
            edition_id,
            target=payload.target_status,
            expected_version=payload.version,
            actor_id=identity.actor_id,
            correlation_id=get_correlation_id(),
        )
        return _edition_view(edition)
    except Exception as exc:
        _raise_api_error(exc)


@router.get("/{edition_id}/audit", response_model=list[EditionAuditView])
async def edition_audit(edition_id: UUID, request: Request) -> list[EditionAuditView]:
    service, _ = await _runtime(request)
    try:
        return [_audit_view(event) for event in await service.audit(edition_id)]
    except Exception as exc:
        _raise_api_error(exc)


async def _runtime(request: Request) -> tuple[EditionService, Identity]:
    service: EditionService = request.app.state.edition_service
    provider: IdentityProvider = request.app.state.identity_provider
    return service, await provider.current()


class EditionFieldArguments(TypedDict):
    country: str
    country_code: str
    period_start: date
    period_end: date
    tlp: TLP
    languages: tuple[str, ...]
    target_articles: int
    previous_edition_id: UUID | None
    source_profile: str


def _field_arguments(payload: EditionFields) -> EditionFieldArguments:
    return {
        "country": payload.country,
        "country_code": payload.country_code,
        "period_start": payload.period_start,
        "period_end": payload.period_end,
        "tlp": payload.tlp,
        "languages": tuple(payload.languages),
        "target_articles": payload.target_articles,
        "previous_edition_id": payload.previous_edition_id,
        "source_profile": payload.source_profile,
    }


def _edition_view(edition: Edition) -> EditionView:
    return EditionView(
        id=edition.id,
        country=edition.country,
        country_code=edition.country_code,
        period_start=edition.period_start,
        period_end=edition.period_end,
        tlp=edition.tlp,
        languages=list(edition.languages),
        target_articles=edition.target_articles,
        previous_edition_id=edition.previous_edition_id,
        source_profile=edition.source_profile,
        status=edition.status,
        version=edition.version,
        progress_percent=edition.progress_percent,
        allowed_transitions=list(edition.allowed_transitions),
        created_at=edition.created_at,
        updated_at=edition.updated_at,
    )


def _page_view(page: EditionPage) -> EditionPageView:
    return EditionPageView(
        items=[_edition_view(edition) for edition in page.items],
        total=page.total,
        page=page.page,
        page_size=page.page_size,
    )


def _audit_view(event: EditionAuditEvent) -> EditionAuditView:
    return EditionAuditView(
        id=event.id,
        edition_id=event.edition_id,
        actor_id=event.actor_id,
        action=event.action,
        before=event.before,
        after=event.after,
        correlation_id=event.correlation_id,
        occurred_at=event.occurred_at,
    )


def _parse_month(value: str) -> tuple[date, date]:
    match = MONTH_PATTERN.fullmatch(value)
    if match is None:
        raise HTTPException(status_code=422, detail="Invalid period filter")
    year, month = (int(part) for part in match.groups())
    try:
        last_day = calendar.monthrange(year, month)[1]
    except calendar.IllegalMonthError as exc:
        raise HTTPException(status_code=422, detail="Invalid period filter") from exc
    return date(year, month, 1), date(year, month, last_day)


def _raise_api_error(exc: Exception) -> NoReturn:
    if isinstance(exc, EditionNotFoundError):
        raise HTTPException(status_code=404, detail={"code": "edition_not_found"}) from exc
    if isinstance(exc, DuplicateEditionError):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "duplicate_edition",
                "message": "Une édition existe déjà pour ce pays et cette période.",
                "existing_edition_id": (
                    str(exc.existing_edition_id) if exc.existing_edition_id else None
                ),
            },
        ) from exc
    if isinstance(exc, EditionConcurrencyError):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "stale_edition_version",
                "message": "L'édition a été modifiée ailleurs. Rechargez-la avant de réessayer.",
            },
        ) from exc
    if isinstance(exc, EditionTransitionRequiresUseCaseError):
        raise HTTPException(
            status_code=409,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc
    if isinstance(exc, (InvalidEditionTransitionError, EditionImmutableError)):
        raise HTTPException(
            status_code=409,
            detail={"code": "invalid_edition_action", "message": str(exc)},
        ) from exc
    if isinstance(exc, (PreviousEditionError, TlpDowngradeError, ValueError)):
        raise HTTPException(
            status_code=422,
            detail={"code": "invalid_edition", "message": str(exc)},
        ) from exc
    raise exc
