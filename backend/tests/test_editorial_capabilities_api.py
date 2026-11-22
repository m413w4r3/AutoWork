"""API-level capability truthfulness through the real editorial route wiring.

Each test mounts the real `editorial_workspace` router, lets `_service` build the
real `EditorialWorkspaceService` from the app state, and only replaces canonical
storage with the existing in-memory workspace fixtures. Capabilities must follow
the mounted dependencies instead of frontend fixtures.
"""

from __future__ import annotations

from pathlib import Path
from typing import cast
from uuid import UUID

import httpx
import pytest
from fastapi import FastAPI

from cti_app.api import editorial_workspace as editorial_workspace_api
from cti_app.application.editorial_workspace import registry as registry_module
from cti_app.application.model_conversations import ModelConversationService
from cti_app.application.persistence import UnitOfWorkFactory
from cti_app.config import Settings
from cti_app.infrastructure.blob_storage.filesystem import FilesystemBlobStore
from tests.test_editorial_legacy_read_only import _legacy_documents
from tests.test_editorial_workspace_snapshot import (
    _make_artifact,
    _paragraph,
    _synthesis,
    _workspace_service,
)


def _workspace(
    monkeypatch: pytest.MonkeyPatch,
    *,
    read_only: bool = False,
    insertion_ready: bool = False,
) -> tuple[UUID, UnitOfWorkFactory, object]:
    service, subject_id, _repository, artifacts, artifact_store = _workspace_service(
        _synthesis((_paragraph("Synthetic paragraph body."),)), monkeypatch
    )
    synthesis = artifacts.current["synthesis"]
    assert synthesis is not None
    if read_only:
        synthesis.content["schema_version"] = "1"
        artifact_store.write(synthesis)
    publication = _make_artifact(
        "publication",
        _legacy_documents()[-1],
        version=8,
        production_run_id=synthesis.production_run_id,
    )
    artifacts.current["publication"] = publication
    artifact_store.write(publication)
    if insertion_ready:
        monkeypatch.setattr(
            registry_module,
            "table_insertion_preparation_ready",
            lambda **_kwargs: True,
        )
    uow_factory = service._uow_factory
    return subject_id, uow_factory, artifact_store


def _app(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    uow_factory: UnitOfWorkFactory,
    artifact_store: object,
    *,
    discussions: bool = True,
    accept_enabled: bool = True,
    proposal_storage: bool = True,
) -> FastAPI:
    settings = Settings(_env_file=None, editorial_proposal_accept_enabled=accept_enabled)
    monkeypatch.setattr(editorial_workspace_api, "get_settings", lambda: settings)
    blob_store = FilesystemBlobStore(tmp_path / "blobs")
    app = FastAPI()
    app.state.uow_factory = uow_factory
    app.state.production_artifact_store = artifact_store
    if proposal_storage:
        app.state.blob_store = blob_store
    app.state.model_gateway = object()
    app.state.publication_render_service = object()
    if discussions:
        app.state.model_conversation_service = ModelConversationService(
            uow_factory,
            object(),
            blob_store,
        )
    app.include_router(editorial_workspace_api.router)
    return app


async def _snapshot_capabilities(app: FastAPI, subject_id: UUID) -> dict[str, object]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://editorial.test"
    ) as client:
        response = await client.get(f"/api/subjects/{subject_id}/editor/snapshot")
    assert response.status_code == 200, response.text
    return cast(dict[str, object], response.json()["capabilities"])


@pytest.mark.asyncio
async def test_fully_wired_application_exposes_discussion_edit_and_enrichment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    subject_id, uow_factory, artifact_store = _workspace(monkeypatch, insertion_ready=True)
    app = _app(monkeypatch, tmp_path, uow_factory, artifact_store)

    capabilities = await _snapshot_capabilities(app, subject_id)

    assert capabilities["read_only_reason"] is None
    assert capabilities["can_discuss"] is True
    assert capabilities["manual_edit"] is True
    assert capabilities["can_add_enrichment"] is True
    # The `kind="propose"` thread route and its worker prepare a persisted
    # proposal card, which is the revision preview.
    assert capabilities["can_preview_revision"] is True


@pytest.mark.asyncio
async def test_discussion_is_false_without_the_bridge_conversation_runtime(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    subject_id, uow_factory, artifact_store = _workspace(monkeypatch, insertion_ready=True)
    app = _app(monkeypatch, tmp_path, uow_factory, artifact_store, discussions=False)

    capabilities = await _snapshot_capabilities(app, subject_id)

    assert capabilities["can_discuss"] is False
    assert capabilities["can_preview_revision"] is False
    assert capabilities["manual_edit"] is True
    assert capabilities["can_add_enrichment"] is True


@pytest.mark.asyncio
async def test_discussion_is_false_when_the_message_worker_is_not_importable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    subject_id, uow_factory, artifact_store = _workspace(monkeypatch, insertion_ready=True)
    app = _app(monkeypatch, tmp_path, uow_factory, artifact_store)
    monkeypatch.setattr(editorial_workspace_api, "_editorial_thread_worker_wired", lambda: False)

    capabilities = await _snapshot_capabilities(app, subject_id)

    assert capabilities["can_discuss"] is False


@pytest.mark.asyncio
async def test_manual_and_insertion_are_false_when_accept_is_disabled(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    subject_id, uow_factory, artifact_store = _workspace(monkeypatch, insertion_ready=True)
    app = _app(
        monkeypatch,
        tmp_path,
        uow_factory,
        artifact_store,
        accept_enabled=False,
    )

    capabilities = await _snapshot_capabilities(app, subject_id)

    assert capabilities["can_discuss"] is True
    assert capabilities["can_preview_revision"] is True
    assert capabilities["manual_edit"] is False
    assert capabilities["can_add_enrichment"] is False


@pytest.mark.asyncio
async def test_preview_is_false_without_the_proposal_writer(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    subject_id, uow_factory, artifact_store = _workspace(monkeypatch, insertion_ready=True)
    app = _app(
        monkeypatch,
        tmp_path,
        uow_factory,
        artifact_store,
        proposal_storage=False,
    )

    capabilities = await _snapshot_capabilities(app, subject_id)

    assert capabilities["can_discuss"] is True
    assert capabilities["can_preview_revision"] is False
    assert capabilities["manual_edit"] is False
    assert capabilities["can_add_enrichment"] is False


@pytest.mark.asyncio
async def test_manual_and_insertion_are_false_without_the_impact_consumer(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    subject_id, uow_factory, artifact_store = _workspace(monkeypatch, insertion_ready=True)
    app = _app(monkeypatch, tmp_path, uow_factory, artifact_store)
    monkeypatch.setattr(editorial_workspace_api, "_proposal_impact_consumer_wired", lambda: False)

    capabilities = await _snapshot_capabilities(app, subject_id)

    assert capabilities["manual_edit"] is False
    assert capabilities["can_add_enrichment"] is False


@pytest.mark.asyncio
async def test_insertion_is_false_without_a_real_create_operation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    subject_id, uow_factory, artifact_store = _workspace(monkeypatch)
    app = _app(monkeypatch, tmp_path, uow_factory, artifact_store)

    capabilities = await _snapshot_capabilities(app, subject_id)

    assert capabilities["manual_edit"] is True
    assert capabilities["can_add_enrichment"] is False


@pytest.mark.asyncio
async def test_unknown_publication_kind_disables_every_write_capability(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    service, subject_id, _repository, _artifacts, artifact_store = _workspace_service(
        _synthesis((_paragraph("Synthetic paragraph body."),)), monkeypatch
    )
    app = _app(
        monkeypatch,
        tmp_path,
        service._uow_factory,
        artifact_store,
    )

    capabilities = await _snapshot_capabilities(app, subject_id)

    assert capabilities["read_only_reason"] == "publication_kind_unknown"
    assert capabilities["can_discuss"] is False
    assert capabilities["manual_edit"] is False
    assert capabilities["can_add_enrichment"] is False


@pytest.mark.asyncio
async def test_read_only_snapshot_disables_every_write_capability(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    subject_id, uow_factory, artifact_store = _workspace(monkeypatch, read_only=True)
    app = _app(monkeypatch, tmp_path, uow_factory, artifact_store)

    capabilities = await _snapshot_capabilities(app, subject_id)

    assert capabilities["read_only_reason"] == "format_not_editable"
    assert capabilities["can_discuss"] is False
    assert capabilities["can_preview_revision"] is False
    assert capabilities["manual_edit"] is False
    assert capabilities["can_add_enrichment"] is False
