from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from cti_app.api.discovery import merge_runs_router
from cti_app.application.discovery_cumulative import MergeHandleLabel
from cti_app.application.identity import LocalIdentityProvider
from cti_app.domain.discovery_cumulative import (
    DiscoveryMergeRun,
    DiscoveryPlannerKind,
    DiscoverySnapshot,
    MergeValidationStatus,
)


class FakeCumulativeDiscoveryService:
    def __init__(self, run: DiscoveryMergeRun, snapshot: DiscoverySnapshot) -> None:
        self.run = run
        self.snapshot = snapshot
        self.decisions: list[object] = []

    async def list_merge_runs(self, edition_id: UUID) -> list[DiscoveryMergeRun]:
        return [self.run] if edition_id == self.run.edition_id else []

    async def get_merge_run(self, edition_id: UUID, run_id: UUID) -> DiscoveryMergeRun:
        if edition_id != self.run.edition_id or run_id != self.run.id:
            raise LookupError(run_id)
        return self.run

    async def describe_merge_handles(
        self, edition_id: UUID, run_id: UUID
    ) -> dict[str, MergeHandleLabel]:
        if edition_id != self.run.edition_id or run_id != self.run.id:
            raise LookupError(run_id)
        return {
            handle: MergeHandleLabel(
                handle=handle,
                title=f"Sujet {handle}",
                summary=f"Résumé {handle}",
                source_urls=("https://example.test/a",),
            )
            for handle in self.run.handle_map
        }

    async def resolve_merge_run(
        self,
        edition_id: UUID,
        run_id: UUID,
        decisions: list[object],
        *,
        actor_id: str,
    ) -> DiscoverySnapshot:
        assert edition_id == self.run.edition_id
        assert run_id == self.run.id
        assert actor_id == "dev-analyst"
        self.decisions = decisions
        return self.snapshot


@pytest.mark.asyncio
async def test_merge_review_api_lists_details_and_resolves_plan() -> None:
    edition_id = uuid4()
    run = DiscoveryMergeRun(
        id=uuid4(),
        edition_id=edition_id,
        parent_snapshot_id=uuid4(),
        intake_id=uuid4(),
        planner_kind=DiscoveryPlannerKind.CHATGPT,
        prompt_version="1.0",
        policy_version="identity-v1",
        blocking_version="recall-v1",
        merge_input_hash="a" * 64,
        handle_map={"X1": str(uuid4()), "C1": str(uuid4())},
        included_subject_ids=(uuid4(),),
        excluded_subject_count=4,
        validation_status=MergeValidationStatus.NEEDS_REVIEW,
        review_reasons=("confidence_medium",),
        plan_payload={
            "schema_version": "1",
            "groups": [
                {
                    "existing_subject_handles": ["X1"],
                    "incoming_candidate_handles": ["C1"],
                    "confidence": "medium",
                    "disposition": "review",
                    "rationale": "uncertain",
                    "evidence": {},
                    "flags": [],
                }
            ],
            "warnings": [],
        },
    )
    snapshot = DiscoverySnapshot(
        id=uuid4(),
        edition_id=edition_id,
        version=3,
        parent_snapshot_id=run.parent_snapshot_id,
        intake_id=run.intake_id,
        merge_run_id=uuid4(),
        planner_kind=DiscoveryPlannerKind.HUMAN,
        subjects=(),
        snapshot_hash="b" * 64,
        is_active=True,
        created_at=datetime.now(UTC),
    )
    service = FakeCumulativeDiscoveryService(run, snapshot)
    application = FastAPI()
    application.include_router(merge_runs_router)
    application.state.cumulative_discovery_service = service
    application.state.identity_provider = LocalIdentityProvider()

    async with AsyncClient(
        transport=ASGITransport(app=application), base_url="http://test"
    ) as client:
        listing = await client.get(f"/api/editions/{edition_id}/merge-runs")
        detail = await client.get(f"/api/editions/{edition_id}/merge-runs/{run.id}")
        resolution = await client.post(
            f"/api/editions/{edition_id}/merge-runs/{run.id}/resolve",
            json={"group_decisions": [{"group_index": 0, "action": "accept"}]},
        )

    assert listing.status_code == detail.status_code == resolution.status_code == 200
    assert detail.json()["review_reasons"] == ["confidence_medium"]
    assert detail.json()["projected_diff"][0]["incoming_candidate_handles"] == ["C1"]
    assert resolution.json() == {"snapshot_id": str(snapshot.id), "snapshot_version": 3}
    assert len(service.decisions) == 1
