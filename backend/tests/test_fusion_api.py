from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from cti_app.api.fusion import fusion_router
from cti_app.application.discovery.fusion import (
    FusionBoard,
    FusionEditionArchivedError,
    FusionSelectedSubjectConflictError,
    FusionSnapshotStaleError,
)
from cti_app.application.identity import LocalIdentityProvider


class FakeFusionService:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def get_board(self, edition_id: UUID) -> FusionBoard:
        return FusionBoard(edition_id, None, None, 0, 0, 0, (), (), ())

    async def _mutate(self, name: str, edition_id: UUID, **kwargs: Any) -> FusionBoard:
        self.calls.append((name, kwargs))
        if self.error is not None:
            raise self.error
        return await self.get_board(edition_id)

    async def resolve_review(
        self, edition_id: UUID, merge_run_id: UUID, **kwargs: Any
    ) -> FusionBoard:
        return await self._mutate("resolve", edition_id, merge_run_id=merge_run_id, **kwargs)

    async def merge(self, edition_id: UUID, **kwargs: Any) -> FusionBoard:
        return await self._mutate("merge", edition_id, **kwargs)

    async def split(self, edition_id: UUID, **kwargs: Any) -> FusionBoard:
        return await self._mutate("split", edition_id, **kwargs)


def _client(service: FakeFusionService) -> AsyncClient:
    application = FastAPI()
    application.include_router(fusion_router)
    application.state.fusion_service = service
    application.state.identity_provider = LocalIdentityProvider()
    return AsyncClient(transport=ASGITransport(app=application), base_url="http://test")


@pytest.mark.asyncio
async def test_fusion_board_api_has_no_planner_handles() -> None:
    edition_id = uuid4()
    async with _client(FakeFusionService()) as client:
        response = await client.get(f"/api/editions/{edition_id}/fusion")
    assert response.status_code == 200
    assert response.json()["pending_reviews"] == []
    assert "C1" not in response.text
    assert "X1" not in response.text
    assert "target_subject_handle" not in response.text
    assert "group_index" not in response.text


@pytest.mark.asyncio
async def test_fusion_mutations_transmit_business_uuids_and_snapshot_version() -> None:
    edition_id, run_id, subject_a, subject_b, candidate = (uuid4() for _ in range(5))
    service = FakeFusionService()
    async with _client(service) as client:
        resolved = await client.post(
            f"/api/editions/{edition_id}/fusion/reviews/{run_id}/resolve",
            json={
                "snapshot_version": 4,
                "decisions": [
                    {
                        "action": "attach",
                        "candidate_ids": [str(candidate)],
                        "target_discovery_subject_id": str(subject_a),
                    }
                ],
            },
        )
        merged = await client.post(
            f"/api/editions/{edition_id}/fusion/merge",
            json={"snapshot_version": 4, "discovery_subject_ids": [str(subject_a), str(subject_b)]},
        )
        split = await client.post(
            f"/api/editions/{edition_id}/fusion/split",
            json={
                "snapshot_version": 4,
                "discovery_subject_id": str(subject_a),
                "candidate_ids": [str(candidate)],
            },
        )
        handle = await client.post(
            f"/api/editions/{edition_id}/fusion/reviews/{run_id}/resolve",
            json={"snapshot_version": 4, "group_decisions": [{"group_index": 0}]},
        )

    assert resolved.status_code == merged.status_code == split.status_code == 200
    assert handle.status_code == 422
    assert [name for name, _ in service.calls] == ["resolve", "merge", "split"]
    assert all(kwargs["snapshot_version"] == 4 for _, kwargs in service.calls)
    decision = service.calls[0][1]["decisions"][0]
    assert decision.candidate_ids == (candidate,)
    assert decision.target_discovery_subject_id == subject_a


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "status", "code"),
    [
        (FusionSnapshotStaleError("stale"), 409, "fusion_snapshot_stale"),
        (FusionEditionArchivedError("Archived"), 409, "fusion_edition_archived"),
        (
            FusionSelectedSubjectConflictError("selected subjects conflict"),
            409,
            "fusion_selected_subject_conflict",
        ),
        (ValueError("bad"), 422, "invalid_fusion_decision"),
        (LookupError("missing"), 404, "fusion_not_found"),
    ],
)
async def test_fusion_mutation_errors_are_explicit(
    error: Exception, status: int, code: str
) -> None:
    edition_id = uuid4()
    async with _client(FakeFusionService(error)) as client:
        response = await client.post(
            f"/api/editions/{edition_id}/fusion/merge",
            json={"snapshot_version": 4, "discovery_subject_ids": [str(uuid4()), str(uuid4())]},
        )
    assert response.status_code == status
    assert response.json()["detail"]["code"] == code
