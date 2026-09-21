"""AW-009 on PostgreSQL: Selection → Subject → ProductionRun → frozen input.

Every scenario goes through the real Discovery, Fusion and Selection services
and the real production API.  No projection of the Selection board is ever
created: Production reads the Subject, its SubjectDiscoveryOrigin and the
active Fusion snapshot only.
"""

from __future__ import annotations

from datetime import date
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

from cti_app.api.production import router as production_router
from cti_app.api.selection import selection_router
from cti_app.application.discovery.cumulative.service import CumulativeDiscoveryService
from cti_app.application.discovery.fusion import FusionService
from cti_app.application.editions import EditionService
from cti_app.application.identity import LocalIdentityProvider
from cti_app.application.jobs import JobExecutionContext, JobRegistry, JobService
from cti_app.application.production_jobs import ProductionStageParameters
from cti_app.application.selection import SelectionService
from cti_app.application.subjects import SubjectService
from cti_app.domain.classification import TLP
from cti_app.domain.discovery_cumulative import DiscoveryInputMode
from cti_app.domain.editions import Edition
from cti_app.domain.model_runs import ModelRun
from cti_app.domain.production import (
    ProductionBatchStatus,
    ProductionInputSnapshot,
    ProductionRun,
    ProductionRunStatus,
)
from tests.discovery_support import persist_batch_with_candidates
from tests.integration.test_selection_workflow import (
    ApplyPlanner,
    _batch,
    _seed,
    _selection_identity,
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


class _RecordingDispatcher:
    def __init__(self) -> None:
        self.dispatched: list[UUID] = []

    async def dispatch(self, job_id: UUID, *, delay_ms: int = 0) -> None:
        del delay_ms
        self.dispatched.append(job_id)


async def _noop_stage(parameters: Any, context: JobExecutionContext) -> dict[str, Any]:
    del parameters, context
    return {}


def _application(uow_factory: Any) -> FastAPI:
    registry = JobRegistry()
    registry.register("production.subject.sources", ProductionStageParameters, _noop_stage)
    application = FastAPI()
    application.include_router(selection_router)
    application.include_router(production_router)
    application.state.uow_factory = uow_factory
    application.state.selection_service = SelectionService(uow_factory)
    application.state.subject_service = SubjectService(uow_factory)
    application.state.job_service = JobService(uow_factory, registry)
    application.state.job_dispatcher = _RecordingDispatcher()
    application.state.identity_provider = LocalIdentityProvider("production-analyst")
    return application


def _client(application: FastAPI) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=application), base_url="http://test")


async def _select(client: AsyncClient, uow_factory: Any, edition_id: UUID) -> UUID:
    identity = await _selection_identity(uow_factory, edition_id)
    async with uow_factory() as uow:
        active = await uow.discovery_snapshots.get_active(edition_id)
    assert active is not None
    response = await client.post(
        f"/api/editions/{edition_id}/selection/decisions",
        headers={"Idempotency-Key": f"select-{identity}"},
        json={
            "snapshot_version": active.version,
            "decisions": [{"discovery_subject_id": str(identity), "action": "select"}],
        },
    )
    assert response.status_code == 200, response.text
    return UUID(response.json()["items"][0]["subject_id"])


async def _start(client: AsyncClient, edition_id: UUID, subject_ids: list[UUID], key: str) -> Any:
    return await client.post(
        f"/api/editions/{edition_id}/production/batches",
        headers={"Idempotency-Key": key},
        json={"subject_ids": [str(subject_id) for subject_id in subject_ids]},
    )


async def _runs(uow_factory: Any, subject_id: UUID) -> list[ProductionRun]:
    async with uow_factory() as uow:
        return list(await uow.production_runs.list_for_subject(subject_id))


async def _snapshot(uow_factory: Any, run_id: UUID) -> ProductionInputSnapshot:
    async with uow_factory() as uow:
        snapshot: ProductionInputSnapshot | None = await uow.production_input_snapshots.get_by_run(
            run_id
        )
    assert snapshot is not None
    return snapshot


async def _counts(uow_factory: Any, edition_id: UUID) -> tuple[int, int, int]:
    async with uow_factory() as uow:
        session = uow._require_session()  # type: ignore[attr-defined]
        values = []
        for table in (
            "edition_production_batches",
            "production_runs",
            "production_input_snapshots",
        ):
            values.append(
                await session.scalar(
                    text(f"SELECT count(*) FROM {table} WHERE edition_id = :edition_id"),
                    {"edition_id": edition_id},
                )
            )
    return values[0], values[1], values[2]


async def _finish_active_batch(uow_factory: Any, edition_id: UUID) -> None:
    async with uow_factory() as uow:
        batch = await uow.edition_production_batches.get_active_for_edition(edition_id)
        assert batch is not None
        for item in await uow.edition_production_batch_items.list_for_batch(batch.id):
            run = await uow.production_runs.get_for_update(item.production_run_id)
            assert run is not None
            if run.status is ProductionRunStatus.QUEUED:
                run.start_running()
            run.mark_ready()
            await uow.production_runs.save(run)
        locked = await uow.edition_production_batches.get_for_update(batch.id)
        assert locked is not None
        locked.finish()
        await uow.edition_production_batches.save(locked)
        await uow.commit()


async def _enrich_and_merge(
    uow_factory: Any, edition: Edition, model_run: ModelRun, first_identity: UUID
) -> UUID:
    """A later DiscoveryRun brings a new candidate that Fusion merges into A."""
    async with uow_factory() as uow:
        discovery_run_id = next(iter(await uow.discovery_runs.list_for_edition(edition.id))).id
    batch = _batch(
        edition.id,
        model_run.id,
        discovery_run_id,
        title="Later enrichment",
        url="https://vendor.example/production-enrichment",
        local_ref="E",
        request_hash="e" * 64,
        identity_key="Enrichment campaign",
    )
    async with uow_factory() as uow:
        await persist_batch_with_candidates(uow, batch)
        await uow.commit()
    _, snapshot = await CumulativeDiscoveryService(
        uow_factory, planner=ApplyPlanner()
    ).reconcile_batch(
        batch,
        input_mode=DiscoveryInputMode.BRIDGE_RESEARCH,
        actor_id="production-analyst",
    )
    second_identity = next(
        item.subject_id for item in snapshot.subjects if item.subject_id != first_identity
    )
    new_candidate: UUID = next(
        reference.candidate_id
        for item in snapshot.subjects
        if item.subject_id == second_identity
        for reference in item.member_references
    )
    await FusionService(uow_factory).merge(
        edition.id,
        snapshot_version=snapshot.version,
        discovery_subject_ids=(first_identity, second_identity),
        actor_id="production-analyst",
    )
    return new_candidate


async def test_production_starts_from_subject_lineage_without_any_editorial_projection(
    uow_factory: Any,
) -> None:
    edition, _, discovery_snapshot, candidate_id = await _seed(uow_factory, "PR")
    origin_identity = await _selection_identity(uow_factory, edition.id)
    application = _application(uow_factory)

    async with _client(application) as client:
        subject_id = await _select(client, uow_factory, edition.id)

        # Selection never starts Production by itself.
        assert await _runs(uow_factory, subject_id) == []
        empty = await client.get(f"/api/editions/{edition.id}/production")
        assert empty.status_code == 200
        assert empty.json()["active_batch"] is None
        assert [item["subject_id"] for item in empty.json()["subjects"]] == [str(subject_id)]
        assert empty.json()["subjects"][0]["can_start"] is True

        started = await _start(client, edition.id, [subject_id], "aw009-first")

    assert started.status_code == 200, started.text
    async with uow_factory() as uow:
        session = uow._require_session()  # type: ignore[attr-defined]
        assert await session.scalar(text("SELECT to_regclass('public.editorial_groups')")) is None
    [run] = await _runs(uow_factory, subject_id)
    assert run.run_number == 1
    assert run.status is ProductionRunStatus.RUNNING
    snapshot = await _snapshot(uow_factory, run.id)
    assert snapshot.subject_id == subject_id
    assert snapshot.edition_id == edition.id
    assert snapshot.discovery_snapshot_id == discovery_snapshot.id
    assert snapshot.discovery_snapshot_version == discovery_snapshot.version
    assert snapshot.member_candidate_ids == (candidate_id,)
    assert snapshot.origin_discovery_subject_id == origin_identity
    assert snapshot.canonical_discovery_subject_id == origin_identity
    assert snapshot.discovery_summary == "Summary for Canonical selection subject"
    assert [source.canonical_url for source in snapshot.core_sources] == [
        "https://vendor.example/selection-a"
    ]
    assert snapshot.core_sources[0].discovery_candidate_id == candidate_id

    async with uow_factory() as uow:
        jobs = [
            job
            for job in await uow.jobs.list_for_aggregate("subject", subject_id)
            if job.kind == "production.subject.sources"
        ]
    assert len(jobs) == 1
    assert jobs[0].input_parameters["run_id"] == str(run.id)
    assert application.state.job_dispatcher.dispatched == [jobs[0].id]


async def test_later_discovery_and_rename_never_rewrite_an_existing_run(
    uow_factory: Any,
) -> None:
    edition, model_run, first_snapshot, first_candidate = await _seed(uow_factory, "PS")
    origin_identity = await _selection_identity(uow_factory, edition.id)
    application = _application(uow_factory)

    async with _client(application) as client:
        subject_id = await _select(client, uow_factory, edition.id)
        assert (await _start(client, edition.id, [subject_id], "wave-1")).status_code == 200
        [run_1] = await _runs(uow_factory, subject_id)
        frozen = await _snapshot(uow_factory, run_1.id)
        await _finish_active_batch(uow_factory, edition.id)

        new_candidate = await _enrich_and_merge(uow_factory, edition, model_run, origin_identity)
        subject = await SubjectService(uow_factory).get(subject_id)
        await SubjectService(uow_factory).update_metadata(
            subject_id,
            expected_version=subject.version,
            title="Campagne renommée",
            tlp=subject.tlp,
            actor_id="production-analyst",
        )

        second = await _start(client, edition.id, [subject_id], "wave-2")

    assert second.status_code == 200, second.text
    run_2, run_1_reloaded = await _runs(uow_factory, subject_id)
    assert (run_2.run_number, run_1_reloaded.run_number) == (2, 1)

    unchanged = await _snapshot(uow_factory, run_1.id)
    assert unchanged == frozen
    assert unchanged.subject_title == "Canonical selection subject"
    assert unchanged.member_candidate_ids == (first_candidate,)
    assert unchanged.discovery_snapshot_version == first_snapshot.version

    captured = await _snapshot(uow_factory, run_2.id)
    async with uow_factory() as uow:
        active = await uow.discovery_snapshots.get_active(edition.id)
        canonical = await uow.discovery_subject_identities.resolve_canonical_subject(
            origin_identity
        )
    assert active is not None
    assert captured.subject_title == "Campagne renommée"
    assert captured.subject_version > unchanged.subject_version
    assert captured.discovery_snapshot_id == active.id
    assert captured.discovery_snapshot_version == active.version
    assert captured.discovery_snapshot_version > unchanged.discovery_snapshot_version
    assert set(captured.member_candidate_ids) == {first_candidate, new_candidate}
    assert captured.origin_discovery_subject_id == origin_identity
    assert captured.canonical_discovery_subject_id == canonical
    assert captured.input_hash != unchanged.input_hash


async def test_input_snapshots_are_immutable_in_postgres(uow_factory: Any) -> None:
    edition, _, _, _ = await _seed(uow_factory, "PT")
    application = _application(uow_factory)
    async with _client(application) as client:
        subject_id = await _select(client, uow_factory, edition.id)
        assert (await _start(client, edition.id, [subject_id], "immutable")).status_code == 200
    [run] = await _runs(uow_factory, subject_id)

    for statement in (
        "UPDATE production_input_snapshots SET subject_title = 'rewritten' "
        "WHERE production_run_id = :run_id",
        "DELETE FROM production_input_snapshots WHERE production_run_id = :run_id",
    ):
        async with uow_factory() as uow:
            session = uow._require_session()  # type: ignore[attr-defined]
            with pytest.raises(DBAPIError, match="append-only"):
                await session.execute(text(statement), {"run_id": run.id})
    assert (await _snapshot(uow_factory, run.id)).subject_title == "Canonical selection subject"


async def test_batch_idempotency_active_batch_and_atomicity_in_postgres(
    uow_factory: Any,
) -> None:
    edition, _, _, _ = await _seed(uow_factory, "PU")
    application = _application(uow_factory)
    async with _client(application) as client:
        subject_id = await _select(client, uow_factory, edition.id)
        first = await _start(client, edition.id, [subject_id], "K")
        replay = await _start(client, edition.id, [subject_id], "K")
        conflict = await _start(client, edition.id, [subject_id, uuid4()], "K")
        active = await _start(client, edition.id, [subject_id], "Y")
        assert await _counts(uow_factory, edition.id) == (1, 1, 1)

        await _finish_active_batch(uow_factory, edition.id)
        unknown = uuid4()
        atomic = await _start(client, edition.id, [subject_id, unknown], "Z")
        after_atomic = await _counts(uow_factory, edition.id)
        next_wave = await _start(client, edition.id, [subject_id], "Z2")

    assert first.status_code == replay.status_code == 200
    assert replay.json()["batch_id"] == first.json()["batch_id"]
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "production_idempotency_conflict"
    assert active.status_code == 409
    assert active.json()["detail"]["code"] == "production_batch_active"
    assert atomic.status_code == 404
    assert atomic.json()["detail"]["subject_ids"] == [str(unknown)]
    assert after_atomic == (1, 1, 1)
    assert next_wave.status_code == 200, next_wave.text
    assert await _counts(uow_factory, edition.id) == (2, 2, 2)
    async with uow_factory() as uow:
        recent = await uow.edition_production_batches.list_recent_for_edition(edition.id, 10)
    assert [batch.status for batch in recent] == [ProductionBatchStatus.COMPLETED]


async def test_archived_edition_is_readable_but_refuses_production(uow_factory: Any) -> None:
    edition, _, _, _ = await _seed(uow_factory, "PW")
    application = _application(uow_factory)
    async with _client(application) as client:
        subject_id = await _select(client, uow_factory, edition.id)
        async with uow_factory() as uow:
            current = await uow.editions.get(edition.id)
        assert current is not None
        await EditionService(uow_factory).archive(
            edition.id, expected_version=current.version, actor_id="production-analyst"
        )

        board = await client.get(f"/api/editions/{edition.id}/production")
        started = await _start(client, edition.id, [subject_id], "archived")

    assert board.status_code == 200, board.text
    assert board.json()["subjects"][0]["blocking_reason"] == "production_edition_archived"
    assert started.status_code == 409
    assert started.json()["detail"]["code"] == "production_edition_archived"
    assert await _counts(uow_factory, edition.id) == (0, 0, 0)


async def test_a_run_cannot_belong_to_another_edition_than_its_subject(
    uow_factory: Any,
) -> None:
    edition, _, _, _ = await _seed(uow_factory, "PY")
    other = Edition(
        country="Production lineage other edition",
        country_code="PY",
        period_start=date(2031, 1, 1),
        period_end=date(2031, 1, 31),
        tlp=TLP.AMBER,
        languages=("fr",),
    )
    async with uow_factory() as uow:
        assert await uow.editions.add_if_absent(other)
        await uow.commit()
    application = _application(uow_factory)
    async with _client(application) as client:
        subject_id = await _select(client, uow_factory, edition.id)

    async with uow_factory() as uow:
        with pytest.raises(IntegrityError, match="fk_production_runs_subject_edition"):
            await uow.production_runs.add(ProductionRun(subject_id=subject_id, edition_id=other.id))
            await uow.commit()
