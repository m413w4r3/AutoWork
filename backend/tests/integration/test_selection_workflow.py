from __future__ import annotations

import asyncio
import hashlib
from datetime import date
from typing import Any
from uuid import UUID

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from cti_app.api.selection import selection_router
from cti_app.api.subjects import router as subjects_router
from cti_app.application.discovery.cumulative.errors import DiscoveryMergeNeedsReview
from cti_app.application.discovery.cumulative.service import CumulativeDiscoveryService
from cti_app.application.discovery.cumulative.types import (
    DiscoveryDelta,
    PlannedDiscoveryMerge,
    ResolvedMergeHandles,
)
from cti_app.application.discovery.fusion import FusionService
from cti_app.application.identity import LocalIdentityProvider
from cti_app.application.selection import SelectionDecisionCommand, SelectionService
from cti_app.application.subjects import SubjectService
from cti_app.domain.classification import TLP
from cti_app.domain.discovery import (
    CandidateTopic,
    DiscoveryBatch,
    DiscoverySourceMode,
    SourceCandidate,
    SourceRole,
)
from cti_app.domain.discovery_cumulative import (
    DiscoveryInputMode,
    DiscoveryMergeGroup,
    DiscoveryMergePlanV1,
    DiscoveryPlannerKind,
    DiscoverySnapshot,
    MergeConfidence,
    MergeDisposition,
)
from cti_app.domain.editions import Edition
from cti_app.domain.model_runs import ModelProvider, ModelRole, ModelRun
from cti_app.domain.selection import SelectionAction
from tests.discovery_support import make_discovery_run_for_edition, persist_batch_with_candidates

pytestmark = pytest.mark.integration


class ApplyPlanner:
    kind = DiscoveryPlannerKind.HEURISTIC
    policy_version = "selection-integration-apply-v1"

    async def plan(
        self,
        parent_snapshot: DiscoverySnapshot | None,
        delta: DiscoveryDelta,
        handles: ResolvedMergeHandles,
        *,
        edition_id: UUID,
        external_llm_allowed: bool,
        sensitivity: str,
    ) -> PlannedDiscoveryMerge:
        del parent_snapshot, delta, edition_id, external_llm_allowed, sensitivity
        return PlannedDiscoveryMerge(
            DiscoveryMergePlanV1(
                groups=[
                    DiscoveryMergeGroup(
                        existing_subject_handles=[],
                        incoming_candidate_handles=[handle],
                        confidence=MergeConfidence.HIGH,
                        disposition=MergeDisposition.APPLY,
                        rationale="selection integration test",
                    )
                    for handle in sorted(handles.incoming)
                ]
            )
        )


def _application(uow_factory: Any) -> FastAPI:
    application = FastAPI()
    application.include_router(selection_router)
    application.include_router(subjects_router)
    application.state.selection_service = SelectionService(uow_factory)
    application.state.subject_service = SubjectService(uow_factory)
    application.state.identity_provider = LocalIdentityProvider("selection-analyst")
    return application


def _edition(country_code: str) -> Edition:
    # The PostgreSQL database is shared by every integration test of the
    # session and editions are unique per (country_code, period): each
    # scenario therefore needs its own alpha-2 code.
    return Edition(
        country=f"Selection integration Iran {country_code}",
        country_code=country_code,
        period_start=date(2026, 7, 1),
        period_end=date(2026, 7, 31),
        tlp=TLP.AMBER,
        languages=("fr", "en"),
    )


def _model_run() -> ModelRun:
    suffix = "selection-workflow"
    return ModelRun(
        provider=ModelProvider.FAKE,
        model_role=ModelRole.RESEARCH,
        requested_model="fake",
        prompt_template_id="selection-integration",
        prompt_template_version="1",
        authorized_input_hash=hashlib.sha256(f"authorized:{suffix}".encode()).hexdigest(),
        evidence_pack_hash=hashlib.sha256(f"evidence:{suffix}".encode()).hexdigest(),
        parameters={},
    )


def _batch(
    edition_id: UUID,
    model_run_id: UUID,
    discovery_run_id: UUID,
    *,
    title: str,
    url: str,
    local_ref: str,
    request_hash: str,
    identity_key: str = "Campaign",
) -> DiscoveryBatch:
    # `identity_key` drives the strict identity key (campaign/malware) used by
    # the duplicate guard: a follow-up batch meant to land as its own subject
    # must not collide with an already materialized one.
    candidate = CandidateTopic(
        title=title,
        summary=f"Summary for {title}",
        novelty="New evidence",
        technical_potential=4,
        uncertainties=("Attribution pending",),
        relevance_reasons=("Technical source",),
        actors=("Actor",),
        campaigns=(identity_key,),
        malware=(f"Malware {identity_key}",),
        cves=(),
        victims=(),
        sectors=("government",),
        countries=("Iran",),
        likely_artifacts=("ioc",),
        sources=[
            SourceCandidate(
                url=url,
                title=f"Report {local_ref}",
                publisher="Vendor",
                role=SourceRole.PRIMARY,
                tlp=TLP.AMBER,
                sensitivity="internal",
                external_llm_allowed=True,
            )
        ],
        tlp=TLP.AMBER,
        sensitivity="internal",
        external_llm_allowed=True,
        local_ref=local_ref,
    )
    return DiscoveryBatch(
        edition_id=edition_id,
        request_hash=request_hash,
        complementary_axis="initial",
        queries=(),
        citations=(),
        candidates=[candidate],
        discovery_run_id=discovery_run_id,
        discovery_model_run_id=model_run_id,
        tlp=TLP.AMBER,
        sensitivity="internal",
        external_llm_allowed=True,
        parser_version="selection-integration-v1",
        report_sha256=request_hash,
        source_mode=DiscoverySourceMode.MODEL_DECLARED_URLS,
    )


async def _seed(
    uow_factory: Any, country_code: str
) -> tuple[Edition, ModelRun, DiscoverySnapshot, UUID]:
    edition = _edition(country_code)
    model_run = _model_run()
    async with uow_factory() as uow:
        assert await uow.editions.add_if_absent(edition)
        await uow.model_runs.add(model_run)
        await uow.commit()
    discovery_run = await make_discovery_run_for_edition(uow_factory, edition)
    batch = _batch(
        edition.id,
        model_run.id,
        discovery_run.id,
        title="Canonical selection subject",
        url="https://vendor.example/selection-a",
        local_ref="A",
        request_hash="a" * 64,
    )
    async with uow_factory() as uow:
        await persist_batch_with_candidates(uow, batch)
        await uow.commit()
    _, snapshot = await CumulativeDiscoveryService(
        uow_factory, planner=ApplyPlanner()
    ).reconcile_batch(
        batch,
        input_mode=DiscoveryInputMode.BRIDGE_RESEARCH,
        actor_id="selection-analyst",
    )
    return edition, model_run, snapshot, snapshot.subjects[0].member_references[0].candidate_id


def _client(application: FastAPI) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=application), base_url="http://test")


async def _selection_identity_named(uow_factory: Any, edition_id: UUID, title: str) -> UUID:
    async with uow_factory() as uow:
        snapshot = await uow.discovery_snapshots.get_active(edition_id)
    assert snapshot is not None
    identity_id: UUID = next(
        item.subject_id for item in snapshot.subjects if item.candidate.title == title
    )
    return identity_id


async def _selection_identity(uow_factory: Any, edition_id: UUID) -> UUID:
    async with uow_factory() as uow:
        snapshot = await uow.discovery_snapshots.get_active(edition_id)
    assert snapshot is not None
    assert len(snapshot.subjects) == 1
    identity_id: UUID = snapshot.subjects[0].subject_id
    return identity_id


@pytest.mark.asyncio
async def test_selection_api_materializes_atomically_and_replays_idempotently(
    uow_factory: Any,
) -> None:
    edition, _, snapshot, _ = await _seed(uow_factory, "SI")
    application = _application(uow_factory)
    identity = await _selection_identity(uow_factory, edition.id)
    body = {
        "snapshot_version": snapshot.version,
        "decisions": [{"discovery_subject_id": str(identity), "action": "select"}],
    }

    async with _client(application) as client:
        initial = await client.get(f"/api/editions/{edition.id}/selection")
        first = await client.post(
            f"/api/editions/{edition.id}/selection/decisions",
            headers={"Idempotency-Key": "select-a"},
            json=body,
        )
        replay = await client.post(
            f"/api/editions/{edition.id}/selection/decisions",
            headers={"Idempotency-Key": "select-a"},
            json=body,
        )
        subjects = await client.get(f"/api/editions/{edition.id}/subjects")

    assert initial.status_code == 200
    assert initial.json()["undecided"] == 1
    assert first.status_code == replay.status_code == 200
    assert first.json()["items"][0]["effective_state"] == "selected"
    assert first.json()["items"][0]["subject_id"] == replay.json()["items"][0]["subject_id"]
    assert subjects.status_code == 200
    assert len(subjects.json()) == 1
    subject_id = subjects.json()[0]["id"]
    assert first.json()["items"][0]["subject_id"] == subject_id

    async with uow_factory() as uow:
        decisions = list(await uow.selection_decisions.list_for_edition(edition.id))
        origins = list(await uow.subject_discovery_origins.list_for_edition(edition.id))
        stored_subjects = list(await uow.subjects.list_for_edition(edition.id))
    assert len(decisions) == len(origins) == len(stored_subjects) == 1
    assert decisions[0].action is SelectionAction.SELECT
    assert decisions[0].snapshot_id == snapshot.id
    assert decisions[0].snapshot_version == snapshot.version
    assert decisions[0].subject_id == stored_subjects[0].id
    assert origins[0].selection_decision_id == decisions[0].id
    assert origins[0].subject_id == stored_subjects[0].id
    assert origins[0].discovery_subject_id == identity
    assert origins[0].selected_snapshot_id == snapshot.id
    assert origins[0].selected_snapshot_version == snapshot.version


@pytest.mark.asyncio
async def test_ignore_then_select_keeps_append_only_ignore_history(uow_factory: Any) -> None:
    edition, _, snapshot, _ = await _seed(uow_factory, "SJ")
    application = _application(uow_factory)
    identity = await _selection_identity(uow_factory, edition.id)
    async with _client(application) as client:
        ignored = await client.post(
            f"/api/editions/{edition.id}/selection/decisions",
            headers={"Idempotency-Key": "ignore-a"},
            json={
                "snapshot_version": snapshot.version,
                "decisions": [{"discovery_subject_id": str(identity), "action": "ignore"}],
            },
        )
        # Reversing an IGNORE is decided against the state the operator read:
        # the command therefore carries the IGNORE it supersedes.
        ignore_decision_id = ignored.json()["items"][0]["last_decision"]["id"]
        selected = await client.post(
            f"/api/editions/{edition.id}/selection/decisions",
            headers={"Idempotency-Key": "select-a-after-ignore"},
            json={
                "snapshot_version": snapshot.version,
                "decisions": [
                    {
                        "discovery_subject_id": str(identity),
                        "action": "select",
                        "expected_decision_id": ignore_decision_id,
                    }
                ],
            },
        )
    assert ignored.status_code == selected.status_code == 200
    assert selected.json()["selected"] == 1
    assert selected.json()["items"][0]["subject_id"] is not None

    async with uow_factory() as uow:
        decisions = list(await uow.selection_decisions.list_for_edition(edition.id))
        origins = list(await uow.subject_discovery_origins.list_for_edition(edition.id))
        subjects = list(await uow.subjects.list_for_edition(edition.id))
    assert [decision.action for decision in decisions] == [
        SelectionAction.IGNORE,
        SelectionAction.SELECT,
    ]
    assert len(origins) == len(subjects) == 1
    assert decisions[0].subject_id is None
    assert decisions[1].subject_id == subjects[0].id
    assert origins[0].selection_decision_id == decisions[1].id


@pytest.mark.asyncio
async def test_concurrent_selects_materialize_one_subject_and_origin(uow_factory: Any) -> None:
    edition, _, snapshot, _ = await _seed(uow_factory, "SK")
    application = _application(uow_factory)
    identity = await _selection_identity(uow_factory, edition.id)
    body = {
        "snapshot_version": snapshot.version,
        "decisions": [{"discovery_subject_id": str(identity), "action": "select"}],
    }

    async def submit(key: str) -> int:
        async with _client(application) as client:
            response = await client.post(
                f"/api/editions/{edition.id}/selection/decisions",
                headers={"Idempotency-Key": key},
                json=body,
            )
            assert response.status_code in {200, 409}
            return response.status_code

    statuses = await asyncio.gather(submit("concurrent-a"), submit("concurrent-b"))
    assert all(status in {200, 409} for status in statuses)
    async with uow_factory() as uow:
        decisions = list(await uow.selection_decisions.list_for_edition(edition.id))
        origins = list(await uow.subject_discovery_origins.list_for_edition(edition.id))
        subjects = list(await uow.subjects.list_for_edition(edition.id))
    assert len(subjects) == len(origins) == 1
    assert len(decisions) == 1
    assert origins[0].subject_id == subjects[0].id == decisions[0].subject_id


@pytest.mark.asyncio
async def test_selected_subject_survives_enrichment_in_a_later_snapshot(
    uow_factory: Any,
) -> None:
    edition, model_run, first_snapshot, _ = await _seed(uow_factory, "SM")
    application = _application(uow_factory)
    first_identity = await _selection_identity(uow_factory, edition.id)
    first_candidate_id = first_snapshot.subjects[0].member_references[0].candidate_id

    async with _client(application) as client:
        selected = await client.post(
            f"/api/editions/{edition.id}/selection/decisions",
            headers={"Idempotency-Key": "enrichment-select"},
            json={
                "snapshot_version": first_snapshot.version,
                "decisions": [{"discovery_subject_id": str(first_identity), "action": "select"}],
            },
        )
    assert selected.status_code == 200
    subject_id = selected.json()["items"][0]["subject_id"]

    async with uow_factory() as uow:
        runs = list(await uow.discovery_runs.list_for_edition(edition.id))
    discovery_run_id = runs[0].id
    second_batch = _batch(
        edition.id,
        model_run.id,
        discovery_run_id,
        title="Enrichment candidate",
        url="https://vendor.example/selection-b",
        local_ref="B",
        request_hash="b" * 64,
        identity_key="Enrichment campaign",
    )
    async with uow_factory() as uow:
        await persist_batch_with_candidates(uow, second_batch)
        await uow.commit()
    _, second_snapshot = await CumulativeDiscoveryService(
        uow_factory, planner=ApplyPlanner()
    ).reconcile_batch(
        second_batch,
        input_mode=DiscoveryInputMode.BRIDGE_RESEARCH,
        actor_id="selection-analyst",
    )
    second_identity = next(
        item.subject_id for item in second_snapshot.subjects if item.subject_id != first_identity
    )
    second_candidate_id = next(
        reference.candidate_id
        for item in second_snapshot.subjects
        if item.subject_id == second_identity
        for reference in item.member_references
    )
    merged = await FusionService(uow_factory).merge(
        edition.id,
        snapshot_version=second_snapshot.version,
        discovery_subject_ids=(first_identity, second_identity),
        actor_id="selection-analyst",
    )
    assert merged.snapshot_version == second_snapshot.version + 1

    async with _client(application) as client:
        board = await client.get(f"/api/editions/{edition.id}/selection")
    assert board.status_code == 200
    item = next(item for item in board.json()["items"] if item["subject_id"] == subject_id)
    assert item["effective_state"] == "selected"
    assert item["updated_since_decision"] is True
    assert set(item["member_candidate_ids"]) == {
        str(first_candidate_id),
        str(second_candidate_id),
    }
    async with uow_factory() as uow:
        assert len(await uow.subjects.list_for_edition(edition.id)) == 1


async def _seed_second_subject(uow_factory: Any, edition: Edition, model_run: ModelRun) -> UUID:
    """Add a second, independent discovery identity to the active snapshot."""

    async with uow_factory() as uow:
        runs = list(await uow.discovery_runs.list_for_edition(edition.id))
    second_batch = _batch(
        edition.id,
        model_run.id,
        runs[0].id,
        title="Second selection subject",
        url="https://vendor.example/selection-second",
        local_ref="C",
        request_hash="c" * 64,
    )
    async with uow_factory() as uow:
        await persist_batch_with_candidates(uow, second_batch)
        await uow.commit()
    _, snapshot = await CumulativeDiscoveryService(
        uow_factory, planner=ApplyPlanner()
    ).reconcile_batch(
        second_batch,
        input_mode=DiscoveryInputMode.BRIDGE_RESEARCH,
        actor_id="selection-analyst",
    )
    identity_id: UUID = next(
        item.subject_id
        for item in snapshot.subjects
        if item.candidate.title == "Second selection subject"
    )
    return identity_id


async def _selection_state(uow_factory: Any, edition_id: UUID) -> tuple[int, int, int]:
    async with uow_factory() as uow:
        decisions = list(await uow.selection_decisions.list_for_edition(edition_id))
        origins = list(await uow.subject_discovery_origins.list_for_edition(edition_id))
        subjects = list(await uow.subjects.list_for_edition(edition_id))
    return len(decisions), len(origins), len(subjects)


@pytest.mark.asyncio
async def test_one_idempotency_key_binds_one_batch_payload(uow_factory: Any) -> None:
    edition, model_run, _, _ = await _seed(uow_factory, "SN")
    first = await _selection_identity_named(uow_factory, edition.id, "Canonical selection subject")
    second = await _seed_second_subject(uow_factory, edition, model_run)
    async with uow_factory() as uow:
        active = await uow.discovery_snapshots.get_active(edition.id)
    assert active is not None
    application = _application(uow_factory)

    async with _client(application) as client:
        applied = await client.post(
            f"/api/editions/{edition.id}/selection/decisions",
            headers={"Idempotency-Key": "batch-key"},
            json={
                "snapshot_version": active.version,
                "decisions": [{"discovery_subject_id": str(first), "action": "select"}],
            },
        )
        superset = await client.post(
            f"/api/editions/{edition.id}/selection/decisions",
            headers={"Idempotency-Key": "batch-key"},
            json={
                "snapshot_version": active.version,
                "decisions": [
                    {"discovery_subject_id": str(first), "action": "select"},
                    {"discovery_subject_id": str(second), "action": "ignore"},
                ],
            },
        )
    assert applied.status_code == 200
    assert superset.status_code == 409
    assert superset.json()["detail"]["code"] == "selection_idempotency_conflict"
    assert await _selection_state(uow_factory, edition.id) == (1, 1, 1)

    async with _client(application) as client:
        board = await client.get(f"/api/editions/{edition.id}/selection")
        expected = {
            item["discovery_subject_id"]: (item["last_decision"] or {}).get("id")
            for item in board.json()["items"]
        }
        pair = await client.post(
            f"/api/editions/{edition.id}/selection/decisions",
            headers={"Idempotency-Key": "pair-key"},
            json={
                "snapshot_version": active.version,
                "decisions": [
                    {
                        "discovery_subject_id": str(first),
                        "action": "select",
                        "expected_decision_id": expected[str(first)],
                    },
                    {
                        "discovery_subject_id": str(second),
                        "action": "ignore",
                        "expected_decision_id": expected[str(second)],
                    },
                ],
            },
        )
        subset = await client.post(
            f"/api/editions/{edition.id}/selection/decisions",
            headers={"Idempotency-Key": "pair-key"},
            json={
                "snapshot_version": active.version,
                "decisions": [{"discovery_subject_id": str(second), "action": "ignore"}],
            },
        )
    assert pair.status_code == 200
    assert subset.status_code == 409
    assert subset.json()["detail"]["code"] == "selection_idempotency_conflict"
    assert await _selection_state(uow_factory, edition.id) == (2, 1, 1)


@pytest.mark.asyncio
async def test_absent_expectation_is_stale_after_a_concurrent_decision(uow_factory: Any) -> None:
    edition, _, snapshot, _ = await _seed(uow_factory, "SP")
    application = _application(uow_factory)
    identity = await _selection_identity(uow_factory, edition.id)

    async with _client(application) as client:
        # The board was read undecided, so the SELECT below carries no
        # expectation; meanwhile another operator ignored the same subject.
        concurrent = await client.post(
            f"/api/editions/{edition.id}/selection/decisions",
            headers={"Idempotency-Key": "concurrent-ignore"},
            json={
                "snapshot_version": snapshot.version,
                "decisions": [{"discovery_subject_id": str(identity), "action": "ignore"}],
            },
        )
        stale = await client.post(
            f"/api/editions/{edition.id}/selection/decisions",
            headers={"Idempotency-Key": "stale-select"},
            json={
                "snapshot_version": snapshot.version,
                "decisions": [
                    {
                        "discovery_subject_id": str(identity),
                        "action": "select",
                        "expected_decision_id": None,
                    }
                ],
            },
        )
    assert concurrent.status_code == 200
    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] == "selection_decision_stale"
    assert await _selection_state(uow_factory, edition.id) == (1, 0, 0)


@pytest.mark.asyncio
async def test_selection_survives_a_failing_workspace_projection(uow_factory: Any) -> None:
    class FailingWorkspace:
        def __init__(self) -> None:
            self.calls = 0

        async def materialize(self, *args: Any, **kwargs: Any) -> Any:
            self.calls += 1
            raise OSError("workspace unavailable")

    edition, _, snapshot, _ = await _seed(uow_factory, "SQ")
    identity = await _selection_identity(uow_factory, edition.id)
    workspace = FailingWorkspace()
    service = SelectionService(uow_factory, materializer=workspace)
    command = SelectionDecisionCommand(
        discovery_subject_id=identity,
        action=SelectionAction.SELECT,
        expected_decision_id=None,
        actor_id="selection-analyst",
        correlation_id="workspace-correlation",
        idempotency_key="workspace-key",
    )

    board = await service.decide_many(edition.id, [command], snapshot_version=snapshot.version)

    # The workspace is a projection: it runs after the commit and its failure
    # can never roll back the canonical Subject.
    assert workspace.calls == 1
    assert board.items[0].effective_state == "selected"
    assert await _selection_state(uow_factory, edition.id) == (1, 1, 1)
    async with uow_factory() as uow:
        subjects = list(await uow.subjects.list_for_edition(edition.id))
        events = list(await uow.provenance.list_for_aggregate("subject", subjects[0].id))
    assert any(event.event_type == "subject.created_from_selection" for event in events)

    retried = await service.decide_many(edition.id, [command], snapshot_version=snapshot.version)
    assert retried.items[0].effective_state == "selected"
    assert await _selection_state(uow_factory, edition.id) == (1, 1, 1)


@pytest.mark.asyncio
async def test_split_after_selection_keeps_the_subject_on_the_historical_identity(
    uow_factory: Any,
) -> None:
    """AW-008 S9/S51: a split never duplicates nor moves an existing Subject.

    The historical identity keeps the materialized `Subject`; the branch
    carved out of it comes back as a plain undecided selection item.
    """
    edition, model_run, snapshot, _ = await _seed(uow_factory, "SR")
    application = _application(uow_factory)
    identity = await _selection_identity(uow_factory, edition.id)

    async with _client(application) as client:
        selected = await client.post(
            f"/api/editions/{edition.id}/selection/decisions",
            headers={"Idempotency-Key": "split-select"},
            json={
                "snapshot_version": snapshot.version,
                "decisions": [{"discovery_subject_id": str(identity), "action": "select"}],
            },
        )
    assert selected.status_code == 200
    subject_id = selected.json()["items"][0]["subject_id"]

    # Grow the selected identity with a second candidate, then split it back out.
    async with uow_factory() as uow:
        runs = list(await uow.discovery_runs.list_for_edition(edition.id))
    second_batch = _batch(
        edition.id,
        model_run.id,
        runs[0].id,
        title="Split candidate",
        url="https://vendor.example/selection-split",
        local_ref="S",
        request_hash="c" * 64,
        identity_key="Split campaign",
    )
    async with uow_factory() as uow:
        await persist_batch_with_candidates(uow, second_batch)
        await uow.commit()
    _, grown = await CumulativeDiscoveryService(
        uow_factory, planner=ApplyPlanner()
    ).reconcile_batch(
        second_batch,
        input_mode=DiscoveryInputMode.BRIDGE_RESEARCH,
        actor_id="selection-analyst",
    )
    second_identity = next(
        item.subject_id for item in grown.subjects if item.subject_id != identity
    )
    split_candidate_id = next(
        reference.candidate_id
        for item in grown.subjects
        if item.subject_id == second_identity
        for reference in item.member_references
    )
    fusion = FusionService(uow_factory)
    merged = await fusion.merge(
        edition.id,
        snapshot_version=grown.version,
        discovery_subject_ids=(identity, second_identity),
        actor_id="selection-analyst",
    )
    canonical_id = merged.groups[0].discovery_subject_id
    await fusion.split(
        edition.id,
        snapshot_version=merged.snapshot_version,
        discovery_subject_id=canonical_id,
        candidate_ids=(split_candidate_id,),
        actor_id="selection-analyst",
    )

    async with _client(application) as client:
        board = await client.get(f"/api/editions/{edition.id}/selection")
    assert board.status_code == 200
    items = board.json()["items"]
    assert len(items) == 2
    kept = next(item for item in items if item["effective_state"] == "selected")
    carved = next(item for item in items if item["effective_state"] == "undecided")
    assert kept["subject_id"] == subject_id
    assert carved["subject_id"] is None
    assert carved["last_decision"] is None
    assert str(split_candidate_id) in carved["member_candidate_ids"]
    # Exactly one Subject and one origin survive the structural change.
    assert await _selection_state(uow_factory, edition.id) == (1, 1, 1)


@pytest.mark.asyncio
async def test_materialized_subject_duplicate_guard_reads_origins(
    uow_factory: Any,
) -> None:
    """AW-008 S38: Fusion learns about materialized Subjects from origins.

    A later wave whose candidate strictly matches an already materialized
    identity must land in Fusion review instead of silently creating a rival
    discovery subject. The only signal feeding that guard is
    `SubjectDiscoveryOrigin`; no editorial projection is involved.
    """
    edition, model_run, snapshot, _ = await _seed(uow_factory, "ST")
    identity = await _selection_identity(uow_factory, edition.id)
    service = SelectionService(uow_factory)
    await service.decide_many(
        edition.id,
        [
            SelectionDecisionCommand(
                discovery_subject_id=identity,
                action=SelectionAction.SELECT,
                expected_decision_id=None,
                actor_id="selection-analyst",
                correlation_id="guard-correlation",
                idempotency_key="guard-key",
            )
        ],
        snapshot_version=snapshot.version,
    )

    async with uow_factory() as uow:
        runs = list(await uow.discovery_runs.list_for_edition(edition.id))
    duplicate = _batch(
        edition.id,
        model_run.id,
        runs[0].id,
        title="Rival report on the same campaign",
        url="https://vendor.example/selection-duplicate",
        local_ref="D",
        request_hash="d" * 64,
    )
    async with uow_factory() as uow:
        await persist_batch_with_candidates(uow, duplicate)
        await uow.commit()

    with pytest.raises(DiscoveryMergeNeedsReview) as raised:
        await CumulativeDiscoveryService(uow_factory, planner=ApplyPlanner()).reconcile_batch(
            duplicate,
            input_mode=DiscoveryInputMode.BRIDGE_RESEARCH,
            actor_id="selection-analyst",
        )
    assert "possible_duplicate_of_editorial_subject" in raised.value.reasons
    # The guard parks the wave: no rival Subject is created behind the operator.
    assert await _selection_state(uow_factory, edition.id) == (1, 1, 1)
