"""Canonical article anchor routes for the investigation cockpit (F4-D B4).

The orchestrator owns ``api/main.py``: this module only exports ``router``.
The catalog is a human-only read: it resolves the opinion's pinned publication
base when an opinion exists, else the current verified base, and returns the
exact anchor space the reader renders plus the M23 elements placed in it.
"""

from __future__ import annotations

from typing import Literal, cast
from uuid import UUID

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict

from cti_app.api.investigation import (
    _http_error,
    _require_verified_human_operator,
    _uow_factory,
)
from cti_app.application.investigation_article_anchors import (
    ArticleAnchorError,
    InvestigationArticleAnchorsService,
)
from cti_app.application.persistence import UnitOfWorkFactory
from cti_app.application.production_artifact_store import ProductionArtifactStore

router = APIRouter(prefix="/api/investigations", tags=["investigation-article-anchors"])


class ArticleAnchorViewV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    anchor: str
    text: str
    text_sha256: str
    section_index: int | None
    paragraph_index: int | None


class ArticleElementViewV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["table", "chart", "diagram", "figure"]
    key: str
    title: str | None
    caption: str | None
    element_sha256: str
    anchor: str | None


class ArticleAnchorCatalogViewV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_publication_artifact_id: UUID
    base_publication_version: int
    base_publication_sha256: str
    source: Literal["pinned_opinion_base", "current_base"]
    anchors: list[ArticleAnchorViewV1]
    elements: list[ArticleElementViewV1]
    truncated: bool


def _artifact_store(request: Request) -> ProductionArtifactStore:
    store = getattr(request.app.state, "production_artifact_store", None)
    if store is None:
        raise _http_error(503, "article_anchors_unavailable")
    return cast(ProductionArtifactStore, store)


def _service(request: Request) -> InvestigationArticleAnchorsService:
    configured = getattr(request.app.state, "investigation_article_anchors_service", None)
    if configured is not None:
        return cast(InvestigationArticleAnchorsService, configured)
    return InvestigationArticleAnchorsService(
        uow_factory=cast(UnitOfWorkFactory, _uow_factory(request)),
        artifact_store=_artifact_store(request),
    )


@router.get(
    "/{investigation_id}/article-anchors",
    response_model=ArticleAnchorCatalogViewV1,
)
async def get_article_anchors(
    investigation_id: UUID, request: Request
) -> ArticleAnchorCatalogViewV1:
    await _require_verified_human_operator(request)
    try:
        catalog = await _service(request).get(investigation_id)
    except ArticleAnchorError as exc:
        raise _http_error(exc.status_code, exc.code.value, str(exc)) from exc
    return ArticleAnchorCatalogViewV1(
        base_publication_artifact_id=catalog.base_publication_artifact_id,
        base_publication_version=catalog.base_publication_version,
        base_publication_sha256=catalog.base_publication_sha256,
        source=catalog.source,
        anchors=[
            ArticleAnchorViewV1(
                anchor=item.anchor,
                text=item.text,
                text_sha256=item.text_sha256,
                section_index=item.section_index,
                paragraph_index=item.paragraph_index,
            )
            for item in catalog.anchors
        ],
        elements=[
            ArticleElementViewV1(
                kind=cast(Literal["table", "chart", "diagram", "figure"], item.kind),
                key=item.key,
                title=item.title,
                caption=item.caption,
                element_sha256=item.element_sha256,
                anchor=item.anchor,
            )
            for item in catalog.elements
        ],
        truncated=catalog.truncated,
    )


__all__ = ["router"]
