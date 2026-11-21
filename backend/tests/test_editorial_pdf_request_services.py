from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
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
from cti_app.domain.editorial_proposal import (
    EditorialProposal,
    EditorialProposalOrigin,
    EditorialProposalState,
)
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


class _HistorySubjectRepository:
    def __init__(self, subject_id: UUID) -> None:
        self._subject_id = subject_id

    async def get(self, subject_id: UUID) -> object | None:
        return object() if subject_id == self._subject_id else None


class _HistoryProposalRepository:
    def __init__(self, proposals: tuple[EditorialProposal, ...]) -> None:
        self._proposals = proposals

    async def list_for_subject(
        self,
        subject_id: UUID,
        *,
        occurrence_id: UUID | None,
        before: tuple[datetime, UUID] | None,
        limit: int,
    ) -> tuple[EditorialProposal, ...]:
        proposals = [proposal for proposal in self._proposals if proposal.subject_id == subject_id]
        if occurrence_id is not None:
            proposals = [
                proposal for proposal in proposals if proposal.occurrence_id == occurrence_id
            ]
        if before is not None:
            proposals = [
                proposal
                for proposal in proposals
                if (proposal.created_at, proposal.id.int) < (before[0], before[1].int)
            ]
        proposals.sort(key=lambda proposal: (proposal.created_at, proposal.id.int), reverse=True)
        return tuple(proposals[:limit])


class _HistoryUnitOfWork:
    def __init__(self, subject_id: UUID, proposals: tuple[EditorialProposal, ...]) -> None:
        self.subjects = _HistorySubjectRepository(subject_id)
        self.editorial_proposals = _HistoryProposalRepository(proposals)

    async def __aenter__(self) -> _HistoryUnitOfWork:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None


def _history_proposal(
    *,
    proposal_id: UUID,
    subject_id: UUID,
    occurrence_id: UUID,
    origin: EditorialProposalOrigin,
    state: EditorialProposalState,
    created_at: datetime,
) -> EditorialProposal:
    is_refusal = state is EditorialProposalState.REFUSED
    is_bridge = origin is EditorialProposalOrigin.BRIDGE_DRAFT
    return EditorialProposal(
        id=proposal_id,
        subject_id=subject_id,
        production_run_id=uuid4(),
        origin=origin,
        snapshot_id=uuid4(),
        occurrence_id=occurrence_id,
        base_artifact_id=uuid4(),
        base_artifact_version=1,
        base_input_hash="a" * 64,
        base_canonical_sha256="b" * 64,
        base_routing_hash=None,
        base_pin_hash="c" * 64,
        target_id="synthetic-target",
        operation="rewrite_paragraph",
        before_hash="d" * 64,
        patch_blob_ref=None if is_refusal else uuid4(),
        patch_sha256=None if is_refusal else "e" * 64,
        evidence_refs=(),
        diagnostics={},
        thread_id=uuid4() if is_bridge else None,
        variant_group_id=uuid4(),
        model_run_id=uuid4() if is_bridge else None,
        model_turn_id=uuid4() if is_bridge else None,
        state=state,
        state_version=1,
        actor_id=uuid4(),
        idempotency_key=str(uuid4()),
        request_sha256="f" * 64,
        created_at=created_at,
        updated_at=created_at + timedelta(minutes=1),
    )


@pytest.mark.asyncio
async def test_editorial_history_route_serializes_real_domain_items_and_pages(
    tmp_path: Path,
) -> None:
    subject_id, occurrence_id = uuid4(), uuid4()
    instant = datetime(2026, 11, 21, tzinfo=UTC)
    proposals = (
        _history_proposal(
            proposal_id=UUID(int=10),
            subject_id=subject_id,
            occurrence_id=occurrence_id,
            origin=EditorialProposalOrigin.MANUAL_PATCH,
            state=EditorialProposalState.REFUSED,
            created_at=instant,
        ),
        _history_proposal(
            proposal_id=UUID(int=20),
            subject_id=subject_id,
            occurrence_id=occurrence_id,
            origin=EditorialProposalOrigin.MANUAL_PATCH,
            state=EditorialProposalState.REFUSED,
            created_at=instant,
        ),
        _history_proposal(
            proposal_id=UUID(int=30),
            subject_id=subject_id,
            occurrence_id=occurrence_id,
            origin=EditorialProposalOrigin.BRIDGE_DRAFT,
            state=EditorialProposalState.PROPOSED,
            created_at=instant + timedelta(minutes=1),
        ),
    )

    @asynccontextmanager
    async def open_history_uow() -> AsyncIterator[UnitOfWork]:
        yield cast(UnitOfWork, _HistoryUnitOfWork(subject_id, proposals))

    app = _app_without_lifespan(tmp_path / "history-blobs")
    app.state.uow_factory = cast(UnitOfWorkFactory, open_history_uow)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://editorial.test"
    ) as client:
        first_response = await client.get(
            f"/api/subjects/{subject_id}/editor/history",
            params={"block_id": str(occurrence_id), "limit": 2},
        )
        assert first_response.status_code == 200, first_response.text
        first_page = first_response.json()
        assert [item["proposal_id"] for item in first_page["items"]] == [
            str(proposals[2].id),
            str(proposals[1].id),
        ]
        assert [item["origin"] for item in first_page["items"]] == [
            EditorialProposalOrigin.BRIDGE_DRAFT.value,
            EditorialProposalOrigin.MANUAL_PATCH.value,
        ]
        assert first_page["next_cursor"]

        second_response = await client.get(
            f"/api/subjects/{subject_id}/editor/history",
            params={
                "block_id": str(occurrence_id),
                "limit": 2,
                "cursor": first_page["next_cursor"],
            },
        )
    assert second_response.status_code == 200, second_response.text
    second_page = second_response.json()
    assert [item["proposal_id"] for item in second_page["items"]] == [str(proposals[0].id)]
    assert second_page["items"][0]["origin"] == EditorialProposalOrigin.MANUAL_PATCH.value
    assert second_page["items"][0]["state"] == EditorialProposalState.REFUSED.value
    assert second_page["next_cursor"] is None
