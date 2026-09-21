from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from cti_app.api.selection import selection_router
from cti_app.application.identity import Identity
from cti_app.application.selection import (
    SelectionBoard,
    SelectionDecisionCommand,
    SelectionDecisionStaleError,
    SelectionEditionArchivedError,
    SelectionSnapshotStaleError,
    SelectionSubjectAlreadyMaterializedError,
)


class FakeIdentityProvider:
    async def current(self) -> Identity:
        return Identity(actor_id="analyst-7")


class FakeSelectionService:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.board_calls: list[UUID] = []
        self.decide_calls: list[tuple[UUID, tuple[SelectionDecisionCommand, ...], int]] = []

    @staticmethod
    def _board(edition_id: UUID) -> SelectionBoard:
        return SelectionBoard(edition_id, None, None, (), 0)

    async def board(self, edition_id: UUID) -> SelectionBoard:
        self.board_calls.append(edition_id)
        if self.error is not None:
            raise self.error
        return self._board(edition_id)

    async def decide_many(
        self,
        edition_id: UUID,
        commands: tuple[SelectionDecisionCommand, ...],
        *,
        snapshot_version: int,
    ) -> SelectionBoard:
        self.decide_calls.append((edition_id, commands, snapshot_version))
        if self.error is not None:
            raise self.error
        return self._board(edition_id)


def _client(service: FakeSelectionService) -> AsyncClient:
    application = FastAPI()
    application.include_router(selection_router)
    application.state.selection_service = service
    application.state.identity_provider = FakeIdentityProvider()
    return AsyncClient(transport=ASGITransport(app=application), base_url="http://test")


@pytest.mark.asyncio
async def test_get_selection_works_for_archived_edition() -> None:
    edition_id = uuid4()
    service = FakeSelectionService()
    async with _client(service) as client:
        response = await client.get(f"/api/editions/{edition_id}/selection")
    assert response.status_code == 200
    assert service.board_calls == [edition_id]


@pytest.mark.asyncio
async def test_selection_requires_idempotency_key() -> None:
    service = FakeSelectionService()
    async with _client(service) as client:
        response = await client.post(
            f"/api/editions/{uuid4()}/selection/decisions",
            json={
                "snapshot_version": 3,
                "decisions": [{"discovery_subject_id": str(uuid4()), "action": "select"}],
            },
        )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "selection_idempotency_key_required"
    assert service.decide_calls == []


@pytest.mark.asyncio
async def test_selection_batch_forwards_identity_correlation_and_one_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import cti_app.api.selection as selection_api

    monkeypatch.setattr(selection_api, "get_correlation_id", lambda: "corr-42")
    edition_id, subject_a, subject_b = uuid4(), uuid4(), uuid4()
    service = FakeSelectionService()
    async with _client(service) as client:
        response = await client.post(
            f"/api/editions/{edition_id}/selection/decisions",
            headers={"Idempotency-Key": "batch-42"},
            json={
                "snapshot_version": 3,
                "decisions": [
                    {"discovery_subject_id": str(subject_a), "action": "select"},
                    {
                        "discovery_subject_id": str(subject_b),
                        "action": "ignore",
                        "expected_decision_id": None,
                    },
                ],
            },
        )
    assert response.status_code == 200
    assert len(service.decide_calls) == 1
    called_edition, commands, version = service.decide_calls[0]
    assert called_edition == edition_id
    assert version == 3
    assert [command.discovery_subject_id for command in commands] == [subject_a, subject_b]
    assert {command.actor_id for command in commands} == {"analyst-7"}
    assert {command.correlation_id for command in commands} == {"corr-42"}
    assert {command.idempotency_key for command in commands} == {"batch-42"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        SelectionSnapshotStaleError(),
        SelectionDecisionStaleError(),
        SelectionSubjectAlreadyMaterializedError(),
        SelectionEditionArchivedError(),
    ],
)
async def test_selection_concurrency_errors_are_stable(error: Exception) -> None:
    service = FakeSelectionService(error)
    async with _client(service) as client:
        response = await client.post(
            f"/api/editions/{uuid4()}/selection/decisions",
            headers={"Idempotency-Key": "batch-1"},
            json={
                "snapshot_version": 3,
                "decisions": [{"discovery_subject_id": str(uuid4()), "action": "select"}],
            },
        )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == error.code  # type: ignore[attr-defined]


def test_application_has_selection_and_no_editorial_group_route() -> None:
    from cti_app.api.main import create_app

    paths = set(create_app().openapi()["paths"])
    assert "/api/editions/{edition_id}/selection" in paths
    assert not any("editorial-groups" in path for path in paths)
