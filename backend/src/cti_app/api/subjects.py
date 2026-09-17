from __future__ import annotations

from datetime import datetime
from typing import NoReturn
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from cti_app.application.editions import EditionNotFoundError
from cti_app.application.identity import IdentityProvider
from cti_app.application.subjects import (
    SubjectConcurrencyError,
    SubjectNotFoundError,
    SubjectService,
)
from cti_app.domain.classification import TLP
from cti_app.domain.editions import EditionImmutableError
from cti_app.domain.entities import Subject

router = APIRouter(prefix="/api", tags=["subjects"])


class SubjectView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID
    edition_id: UUID
    title: str
    slug: str
    tlp: TLP
    version: int
    created_at: datetime
    updated_at: datetime


class SubjectMetadataUpdate(BaseModel):
    """Mise à jour des seules métadonnées éditables d'un Subject.

    `slug` et `edition_id` sont stables par construction : ils ne sont pas
    exposés ici. `version` porte la concurrence optimiste, comme pour Edition.
    """

    model_config = ConfigDict(extra="forbid")

    version: int = Field(ge=1)
    title: str = Field(min_length=1, max_length=1000)
    tlp: TLP


@router.get("/editions/{edition_id}/subjects", response_model=list[SubjectView])
async def list_subjects(edition_id: UUID, request: Request) -> list[SubjectView]:
    service: SubjectService = request.app.state.subject_service
    try:
        subjects = await service.list_for_edition(edition_id)
    except Exception as exc:
        _raise_api_error(exc)
    return [_subject_view(subject) for subject in subjects]


@router.get("/subjects/{subject_id}", response_model=SubjectView)
async def get_subject(subject_id: UUID, request: Request) -> SubjectView:
    service: SubjectService = request.app.state.subject_service
    try:
        subject = await service.get(subject_id)
    except Exception as exc:
        _raise_api_error(exc)
    return _subject_view(subject)


@router.put("/subjects/{subject_id}", response_model=SubjectView)
async def update_subject_metadata(
    subject_id: UUID, payload: SubjectMetadataUpdate, request: Request
) -> SubjectView:
    service: SubjectService = request.app.state.subject_service
    provider: IdentityProvider = request.app.state.identity_provider
    identity = await provider.current()
    try:
        subject = await service.update_metadata(
            subject_id,
            expected_version=payload.version,
            title=payload.title,
            tlp=payload.tlp,
            actor_id=identity.actor_id,
        )
    except Exception as exc:
        _raise_api_error(exc)
    return _subject_view(subject)


def _subject_view(subject: Subject) -> SubjectView:
    return SubjectView(
        id=subject.id,
        edition_id=subject.edition_id,
        title=subject.title,
        slug=subject.slug,
        tlp=subject.tlp,
        version=subject.version,
        created_at=subject.created_at,
        updated_at=subject.updated_at,
    )


def _raise_api_error(exc: Exception) -> NoReturn:
    if isinstance(exc, EditionNotFoundError):
        raise HTTPException(status_code=404, detail={"code": "edition_not_found"}) from exc
    if isinstance(exc, SubjectNotFoundError):
        raise HTTPException(status_code=404, detail={"code": "subject_not_found"}) from exc
    if isinstance(exc, SubjectConcurrencyError):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "stale_subject_version",
                "message": "Le sujet a été modifié ailleurs. Rechargez-le avant de réessayer.",
            },
        ) from exc
    if isinstance(exc, EditionImmutableError):
        raise HTTPException(
            status_code=409,
            detail={"code": "invalid_edition_action", "message": str(exc)},
        ) from exc
    if isinstance(exc, ValueError):
        raise HTTPException(
            status_code=422,
            detail={"code": "invalid_subject", "message": str(exc)},
        ) from exc
    raise exc
