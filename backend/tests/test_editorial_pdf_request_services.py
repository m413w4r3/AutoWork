from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI

from cti_app.api import main as api_main
from cti_app.api import publication_runtime as publication_runtime_api
from cti_app.application.media_assets import MediaAssetStore
from cti_app.application.persistence import UnitOfWork, UnitOfWorkFactory
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.application.publication_rendering import PublicationRenderService
from cti_app.application.subject_content import SubjectContentService
from cti_app.config import Settings
from cti_app.infrastructure.blob_storage.filesystem import FilesystemBlobStore


class _EmptySubjectRepository:
    async def get(self, _subject_id: UUID) -> None:
        return None


class _EmptyProductionRunRepository:
    async def get_current_for_subject(self, _subject_id: UUID) -> None:
        return None


class _EmptyUnitOfWork:
    def __init__(self) -> None:
        self.subjects = _EmptySubjectRepository()
        self.production_runs = _EmptyProductionRunRepository()

    async def __aenter__(self) -> _EmptyUnitOfWork:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None


def _empty_uow_factory() -> UnitOfWorkFactory:
    @asynccontextmanager
    async def open_uow() -> AsyncIterator[UnitOfWork]:
        yield cast(UnitOfWork, _EmptyUnitOfWork())

    return cast(UnitOfWorkFactory, open_uow)


def _app_without_lifespan(blob_root: Path) -> FastAPI:
    app = api_main.create_app()
    app.state.uow_factory = _empty_uow_factory()
    app.state.blob_store = FilesystemBlobStore(blob_root)
    app.state.settings = object()
    return app


@pytest.mark.asyncio
async def test_editorial_pdf_routes_build_real_services_without_lifespan(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = Settings(_env_file=None)
    monkeypatch.setattr(api_main, "get_settings", lambda: settings)
    monkeypatch.setattr(publication_runtime_api, "get_settings", lambda: settings)

    subject_id = uuid4()
    requests = (
        ("GET", f"/api/subjects/{subject_id}/editor/pdf"),
        ("POST", f"/api/subjects/{subject_id}/editor/pdf/regenerate"),
        ("GET", f"/api/subjects/{subject_id}/editor/composition"),
        ("GET", f"/api/subjects/{subject_id}/editor/history"),
        ("GET", f"/api/subjects/{subject_id}/content"),
    )
    for index, (method, path) in enumerate(requests):
        app = _app_without_lifespan(tmp_path / f"blobs-{index}")
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://editorial.test"
        ) as client:
            response = await client.request(method, path)

        assert response.status_code == 404, response.text
        if path.endswith("/editor/pdf") or method == "POST" or path.endswith("/content"):
            assert isinstance(app.state.production_artifact_store, ProductionArtifactStore)
        if method == "POST" or path.endswith("/content"):
            assert isinstance(app.state.media_asset_store, MediaAssetStore)
            assert isinstance(app.state.publication_render_service, PublicationRenderService)
            assert isinstance(app.state.subject_content_service, SubjectContentService)
