from __future__ import annotations

from datetime import datetime
from typing import NoReturn
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from cti_app.application.editions import EditionNotFoundError
from cti_app.application.subjects import SubjectNotFoundError, SubjectService
from cti_app.domain.classification import TLP
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
    raise exc
