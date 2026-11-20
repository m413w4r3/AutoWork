from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest

from cti_app.application.blobs import BlobCatalogService
from cti_app.application.editorial_workspace.service import (
    EditorialWorkspaceService,
    advance_workspace_registry,
)
from cti_app.application.persistence import UnitOfWorkFactory
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.application.production_repair_factory import (
    create_editorial_proposal_impact_service,
)
from cti_app.domain.editorial_proposal import (
    EditorialProposalState,
)
from cti_app.domain.editorial_proposal_impact import (
    EditorialProposalImpact,
    EditorialProposalImpactState,
)
from cti_app.domain.editorial_workspace import AdvancedBlockRegistry, BlockRegistryEntry
from cti_app.domain.production import ProductionArtifactStage, ProductionArtifactStatus
from cti_app.domain.typst_render import TypstRenderStatus
from cti_app.infrastructure.database.repositories.editorial_proposals import (
    SqlAlchemyEditorialProposalRepository,
)
from cti_app.infrastructure.database.repositories.production import (
    SqlAlchemyProductionArtifactRepository,
    SqlAlchemyProductionRunRepository,
)
from cti_app.workers.editorial_proposal_impacts import EditorialProposalImpactWorker
from tests.integration.editorial.test_proposal_adoption_postgres import (
    _adopt_kwargs,
    _seed,
    _seed_enrichment_proposal,
    _service,
    _Workspace,
)

pytestmark = pytest.mark.integration


class _ImpactWorkspace(_Workspace):
    """Use the production registry transition logic in impact adoption fixtures."""

    def advance(
        self,
        *,
        base_registry: tuple[BlockRegistryEntry, ...],
        canonical_sha256: str,
        retired_block_ids: tuple[UUID, ...] = (),
        **kwargs: Any,
    ) -> AdvancedBlockRegistry:
        return advance_workspace_registry(
            base_registry=base_registry,
            artifact_id=kwargs["artifact_id"],
            canonical_sha256=canonical_sha256,
            source_stage=kwargs["source_stage"],
            canonical_content=kwargs["canonical_content"],
            lineage=kwargs["lineage"],
            retired_block_ids=retired_block_ids,
        )


def _with_production_registry_advance(seeded):
    return replace(seeded, workspace=_ImpactWorkspace(seeded.workspace.resolution))


def _impact_worker(seeded, uow_factory: UnitOfWorkFactory, *, clock=None):
    artifact_store = ProductionArtifactStore(BlobCatalogService(seeded.blob_store, uow_factory))
    service = create_editorial_proposal_impact_service(
        uow_factory,
        artifact_store=artifact_store,
        model_gateway=object(),  # type: ignore[arg-type]  # No worker path may call it.
        clock=clock,
    )
    return EditorialProposalImpactWorker(service)


async def _insert_impact(
    uow_factory: UnitOfWorkFactory,
    *,
    seeded,
    proposal_id: UUID,
    sequence: int,
    snapshot_id: UUID | None = None,
    artifact_id: UUID | None = None,
    base_pin_hash: str | None = None,
    accepted_pin_hash: str | None = None,
    updated_at: datetime | None = None,
) -> EditorialProposalImpact:
    impact_time = updated_at or datetime.now(UTC)
    impact = EditorialProposalImpact(
        impact_id=uuid4(),
        subject_id=seeded.subject.id,
        production_run_id=seeded.run.id,
        proposal_id=proposal_id,
        accepted_artifact_id=artifact_id or seeded.base_artifact.id,
        accepted_snapshot_id=snapshot_id or seeded.snapshot.snapshot_id,
        base_pin_hash=base_pin_hash or seeded.snapshot.workspace_pin_hash,
        accepted_pin_hash=accepted_pin_hash or seeded.snapshot.workspace_pin_hash,
        sequence=sequence,
        created_at=impact_time,
        updated_at=impact_time,
    )
    async with uow_factory() as uow:
        await uow.editorial_proposal_impacts.add(impact)
        await uow.commit()
    return impact


@pytest.mark.asyncio
async def test_impact_sequence_is_monotonic_per_run(
    uow_factory: UnitOfWorkFactory,
) -> None:
    seeded = await _seed(uow_factory, proposal_count=3)
    await _insert_impact(
        uow_factory,
        seeded=seeded,
        proposal_id=seeded.proposals[0].id,
        sequence=1,
    )
    await _insert_impact(
        uow_factory,
        seeded=seeded,
        proposal_id=seeded.proposals[1].id,
        sequence=2,
    )

    kwargs = _adopt_kwargs(seeded, seeded.proposals[2], "impact-sequence-dev-analyst")
    kwargs["actor_id"] = "dev-analyst"
    result = await _service(seeded, uow_factory).adopt(**kwargs)

    assert result.impact.sequence == 3
    async with uow_factory() as uow:
        impacts = [
            await uow.editorial_proposal_impacts.get_by_proposal(proposal.id)
            for proposal in seeded.proposals
        ]
    assert [impact.sequence for impact in impacts if impact is not None] == [1, 2, 3]


@pytest.mark.asyncio
async def test_pending_impact_never_blocks_adoption(
    uow_factory: UnitOfWorkFactory,
) -> None:
    seeded = await _seed(uow_factory, proposal_count=2)
    pending = await _insert_impact(
        uow_factory,
        seeded=seeded,
        proposal_id=seeded.proposals[0].id,
        sequence=1,
    )

    kwargs = _adopt_kwargs(seeded, seeded.proposals[1], "impact-pending-dev-analyst")
    kwargs["actor_id"] = "dev-analyst"
    result = await _service(seeded, uow_factory).adopt(**kwargs)

    assert result.proposal.state is EditorialProposalState.ACCEPTED
    assert result.impact.sequence == 2
    async with uow_factory() as uow:
        still_pending = await uow.editorial_proposal_impacts.get_by_id(pending.impact_id)
    assert still_pending is not None
    assert still_pending.state is EditorialProposalImpactState.PENDING


@pytest.mark.asyncio
async def test_out_of_order_impact_a_b_marks_old_pins_superseded_noop(
    uow_factory: UnitOfWorkFactory,
) -> None:
    seeded = await _seed(uow_factory, proposal_count=2, article_base=True)
    artifact_store = ProductionArtifactStore(BlobCatalogService(seeded.blob_store, uow_factory))
    current_view = EditorialWorkspaceService(
        uow_factory=uow_factory,
        artifact_store=artifact_store,
    )
    initial_snapshot = await current_view.snapshot(seeded.subject.id)
    async with uow_factory() as uow:
        initial_record = await uow.editorial_workspace_snapshots.get_by_id(
            UUID(str(initial_snapshot["snapshot_id"]))
        )
    assert initial_record is not None
    impact_a = await _insert_impact(
        uow_factory,
        seeded=seeded,
        proposal_id=seeded.proposals[0].id,
        sequence=1,
        snapshot_id=initial_record.snapshot_id,
        artifact_id=seeded.base_artifact.id,
        base_pin_hash=initial_record.workspace_pin_hash,
        accepted_pin_hash=initial_record.workspace_pin_hash,
    )

    async with uow_factory() as uow:
        newer = replace(seeded.base_artifact, id=uuid4(), version=seeded.base_artifact.version + 1)
        await uow.production_artifacts.append(newer)
        await uow.commit()
    current_snapshot = await current_view.snapshot(seeded.subject.id)
    assert current_snapshot["snapshot_id"] != str(initial_record.snapshot_id)
    async with uow_factory() as uow:
        current_record = await uow.editorial_workspace_snapshots.get_by_id(
            UUID(str(current_snapshot["snapshot_id"]))
        )
    assert current_record is not None
    impact_b = await _insert_impact(
        uow_factory,
        seeded=seeded,
        proposal_id=seeded.proposals[1].id,
        sequence=2,
        snapshot_id=current_record.snapshot_id,
        artifact_id=newer.id,
        accepted_pin_hash=current_record.workspace_pin_hash,
    )

    worker = _impact_worker(seeded, uow_factory)
    before_count: int
    async with uow_factory() as uow:
        before_count = len(await uow.production_artifacts.list_for_run(seeded.run.id))
    result = await worker.process(impact_a.impact_id)
    async with uow_factory() as uow:
        after_count = len(await uow.production_artifacts.list_for_run(seeded.run.id))
        stored = await uow.editorial_proposal_impacts.get_by_id(impact_a.impact_id)
        newer_stored = await uow.editorial_proposal_impacts.get_by_id(impact_b.impact_id)

    assert result is not None
    assert result.state is EditorialProposalImpactState.SUPERSEDED
    assert stored is not None and stored.state is EditorialProposalImpactState.SUPERSEDED
    assert newer_stored is not None
    assert newer_stored.state is EditorialProposalImpactState.PENDING
    assert after_count == before_count


@pytest.mark.asyncio
async def test_worker_locks_run_and_rereads_current_pins(
    uow_factory: UnitOfWorkFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seeded = _with_production_registry_advance(await _seed_enrichment_proposal(uow_factory))
    kwargs = _adopt_kwargs(seeded, seeded.proposals[0], "impact-lock-dev-analyst")
    kwargs["actor_id"] = "dev-analyst"
    accepted = await _service(seeded, uow_factory).adopt(**kwargs)

    events: list[str] = []
    original_advisory = SqlAlchemyEditorialProposalRepository.lock_adoption_for_run
    original_run_lock = SqlAlchemyProductionRunRepository.get_for_update
    original_snapshot = EditorialWorkspaceService.snapshot_record_in_uow

    async def trace_advisory(self, run_id):
        events.append("advisory")
        await original_advisory(self, run_id)

    async def trace_run_lock(self, run_id):
        events.append("run_lock")
        return await original_run_lock(self, run_id)

    async def trace_snapshot(self, uow, subject_id):
        events.append("reread_pins")
        return await original_snapshot(self, uow, subject_id)

    monkeypatch.setattr(
        SqlAlchemyEditorialProposalRepository, "lock_adoption_for_run", trace_advisory
    )
    monkeypatch.setattr(SqlAlchemyProductionRunRepository, "get_for_update", trace_run_lock)
    monkeypatch.setattr(EditorialWorkspaceService, "snapshot_record_in_uow", trace_snapshot)

    result = await _impact_worker(seeded, uow_factory).process(accepted.impact.impact_id)

    assert result is not None and result.state is EditorialProposalImpactState.DONE
    assert events.index("advisory") < events.index("run_lock") < events.index("reread_pins")


@pytest.mark.asyncio
async def test_impact_failure_never_rolls_back_accepted_artifact(
    uow_factory: UnitOfWorkFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seeded = _with_production_registry_advance(
        await _seed_enrichment_proposal(uow_factory, publication_base=True)
    )
    kwargs = _adopt_kwargs(seeded, seeded.proposals[0], "impact-failure-dev-analyst")
    kwargs["actor_id"] = "dev-analyst"
    accepted = await _service(seeded, uow_factory).adopt(**kwargs)

    async def fail_publication_stale(self, _run_id, _stages):
        raise RuntimeError("synthetic impact failure")

    monkeypatch.setattr(
        SqlAlchemyProductionArtifactRepository,
        "mark_stages_stale",
        fail_publication_stale,
    )
    clock_values = [datetime.now(UTC)]
    worker = _impact_worker(seeded, uow_factory, clock=lambda: clock_values[0])
    result = await worker.process(accepted.impact.impact_id)

    async with uow_factory() as uow:
        artifact = await uow.production_artifacts.get(accepted.artifact.id)
        proposal = await uow.editorial_proposals.get_for_occurrence(
            accepted.proposal.subject_id,
            accepted.proposal.occurrence_id,
            accepted.proposal.id,
        )
    assert result is not None and result.state is EditorialProposalImpactState.FAILED
    assert result.last_error_code == "editorial_impact_failed"
    assert artifact is not None and artifact.status is ProductionArtifactStatus.VERIFIED
    assert proposal is not None and proposal.state is EditorialProposalState.ACCEPTED
    status = await EditorialWorkspaceService(
        uow_factory=uow_factory,
        artifact_store=ProductionArtifactStore(BlobCatalogService(seeded.blob_store, uow_factory)),
    ).snapshot(seeded.subject.id)
    assert status["impact_status"] == "failed"
    assert status["pdf_status"] == "STALE"

    monkeypatch.undo()
    clock_values[0] += timedelta(minutes=2)
    retried = await worker.process(accepted.impact.impact_id)
    assert retried is not None and retried.state is EditorialProposalImpactState.DONE


@pytest.mark.asyncio
async def test_impact_recovers_after_adoption_commit_before_worker_and_replays_idempotently(
    uow_factory: UnitOfWorkFactory,
) -> None:
    seeded = _with_production_registry_advance(
        await _seed_enrichment_proposal(uow_factory, publication_base=True)
    )
    kwargs = _adopt_kwargs(seeded, seeded.proposals[0], "impact-crash-dev-analyst")
    kwargs["actor_id"] = "dev-analyst"
    accepted = await _service(seeded, uow_factory).adopt(**kwargs)
    # A crash after this commit leaves both the accepted artifact and pending outbox row durable.
    async with uow_factory() as uow:
        pending = await uow.editorial_proposal_impacts.get_by_id(accepted.impact.impact_id)
        accepted_artifact = await uow.production_artifacts.get(accepted.artifact.id)
        artifacts_before = tuple(await uow.production_artifacts.list_for_run(seeded.run.id))
        snapshots_before = tuple(
            await uow.editorial_workspace_snapshots.list_for_run(seeded.run.id)
        )
        composition_before = await uow.edition_compositions.get_for_edition(seeded.run.edition_id)
        release_before = await uow.edition_releases.get_for_edition(seeded.run.edition_id)
        publication_before = next(
            item for item in artifacts_before if item.stage is ProductionArtifactStage.PUBLICATION
        )
        render_before = await uow.publication_renders.get_latest_for_artifact(publication_before.id)

    assert pending is not None and pending.state is EditorialProposalImpactState.PENDING
    assert accepted_artifact is not None
    assert publication_before.status is ProductionArtifactStatus.VERIFIED
    assert render_before is not None and render_before.status is TypstRenderStatus.SUCCEEDED
    assert publication_before.metadata["input_artifacts"]["synthesis_artifact_id"] == str(
        seeded.snapshot.synthesis_artifact_id
    )
    assert publication_before.metadata["input_artifacts"]["editorial_enrichment_artifact_id"]
    assert publication_before.metadata["input_artifacts"][
        "editorial_enrichment_artifact_id"
    ] != str(accepted.artifact.id)

    projection_pending = await EditorialWorkspaceService(
        uow_factory=uow_factory,
        artifact_store=ProductionArtifactStore(BlobCatalogService(seeded.blob_store, uow_factory)),
    ).snapshot(seeded.subject.id)
    assert projection_pending["impact_status"] == "pending"
    assert projection_pending["publication_artifact_id"] == str(publication_before.id)
    assert projection_pending["preview_status"] == "STALE"
    assert projection_pending["pdf_status"] == "STALE"

    worker = _impact_worker(seeded, uow_factory)
    processed = await worker.process(accepted.impact.impact_id)
    replayed = await worker.process(accepted.impact.impact_id)
    async with uow_factory() as uow:
        artifacts_after = tuple(await uow.production_artifacts.list_for_run(seeded.run.id))
        snapshots_after = tuple(await uow.editorial_workspace_snapshots.list_for_run(seeded.run.id))
        composition_after = await uow.edition_compositions.get_for_edition(seeded.run.edition_id)
        release_after = await uow.edition_releases.get_for_edition(seeded.run.edition_id)
    projection = await EditorialWorkspaceService(
        uow_factory=uow_factory,
        artifact_store=ProductionArtifactStore(BlobCatalogService(seeded.blob_store, uow_factory)),
    ).snapshot(seeded.subject.id)

    assert processed is not None and processed.state is EditorialProposalImpactState.DONE
    assert replayed is not None and replayed.state is EditorialProposalImpactState.DONE
    assert {item.id for item in artifacts_after} == {item.id for item in artifacts_before}
    accepted_after = next(item for item in artifacts_after if item.id == accepted.artifact.id)
    publication_after = next(
        item for item in artifacts_after if item.stage is ProductionArtifactStage.PUBLICATION
    )
    assert accepted_after.status is ProductionArtifactStatus.VERIFIED
    assert publication_after.status is ProductionArtifactStatus.STALE
    assert snapshots_after == snapshots_before
    assert composition_after == composition_before
    assert release_after == release_before
    assert projection["impact_status"] == "up_to_date"
    assert projection["publication_artifact_id"] is None
    assert projection["preview_status"] == "STALE"
    assert projection["pdf_status"] == "STALE"


@pytest.mark.asyncio
async def test_two_workers_claim_same_impact_with_one_winner_and_one_canonical_effect(
    uow_factory: UnitOfWorkFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seeded = _with_production_registry_advance(await _seed_enrichment_proposal(uow_factory))
    kwargs = _adopt_kwargs(seeded, seeded.proposals[0], "impact-concurrent-dev-analyst")
    kwargs["actor_id"] = "dev-analyst"
    accepted = await _service(seeded, uow_factory).adopt(**kwargs)
    async with uow_factory() as uow:
        artifacts_before = tuple(await uow.production_artifacts.list_for_run(seeded.run.id))
        snapshots_before = tuple(
            await uow.editorial_workspace_snapshots.list_for_run(seeded.run.id)
        )

    stale_mark_calls = 0
    original_mark_stale = SqlAlchemyProductionArtifactRepository.mark_stages_stale

    async def count_stale_mark(self, run_id, stages):
        nonlocal stale_mark_calls
        stale_mark_calls += 1
        return await original_mark_stale(self, run_id, stages)

    monkeypatch.setattr(
        SqlAlchemyProductionArtifactRepository,
        "mark_stages_stale",
        count_stale_mark,
    )
    worker = _impact_worker(seeded, uow_factory)
    results = await asyncio.gather(
        worker.process(accepted.impact.impact_id),
        worker.process(accepted.impact.impact_id),
    )

    async with uow_factory() as uow:
        stored = await uow.editorial_proposal_impacts.get_by_id(accepted.impact.impact_id)
        artifacts_after = tuple(await uow.production_artifacts.list_for_run(seeded.run.id))
        snapshots_after = tuple(await uow.editorial_workspace_snapshots.list_for_run(seeded.run.id))

    assert all(result is not None for result in results)
    assert any(result.state is EditorialProposalImpactState.DONE for result in results if result)
    assert stored is not None and stored.state is EditorialProposalImpactState.DONE
    assert stored.attempt_count == 1
    assert stale_mark_calls == 1
    assert artifacts_after == artifacts_before
    assert snapshots_after == snapshots_before


@pytest.mark.asyncio
async def test_worker_reclaims_stale_running_impact_idempotently(
    uow_factory: UnitOfWorkFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seeded = _with_production_registry_advance(await _seed_enrichment_proposal(uow_factory))
    kwargs = _adopt_kwargs(seeded, seeded.proposals[0], "impact-stale-running-dev-analyst")
    kwargs["actor_id"] = "dev-analyst"
    accepted = await _service(seeded, uow_factory).adopt(**kwargs)
    now = datetime.now(UTC)
    async with uow_factory() as uow:
        stale_claim = await uow.editorial_proposal_impacts.claim(
            accepted.impact.impact_id,
            now=now,
            stale_running_before=now - timedelta(minutes=5),
        )
        artifacts_before = tuple(await uow.production_artifacts.list_for_run(seeded.run.id))
        snapshots_before = tuple(
            await uow.editorial_workspace_snapshots.list_for_run(seeded.run.id)
        )
        await uow.commit()
    assert stale_claim is not None
    assert stale_claim.state is EditorialProposalImpactState.RUNNING

    stale_mark_calls = 0
    original_mark_stale = SqlAlchemyProductionArtifactRepository.mark_stages_stale

    async def count_stale_mark(self, run_id, stages):
        nonlocal stale_mark_calls
        stale_mark_calls += 1
        return await original_mark_stale(self, run_id, stages)

    monkeypatch.setattr(
        SqlAlchemyProductionArtifactRepository,
        "mark_stages_stale",
        count_stale_mark,
    )

    worker = _impact_worker(
        seeded,
        uow_factory,
        clock=lambda: now + timedelta(minutes=6),
    )
    recovered = await worker.process(accepted.impact.impact_id)
    replayed = await worker.process(accepted.impact.impact_id)
    async with uow_factory() as uow:
        stored = await uow.editorial_proposal_impacts.get_by_id(accepted.impact.impact_id)
        artifacts = tuple(await uow.production_artifacts.list_for_run(seeded.run.id))
        snapshots = tuple(await uow.editorial_workspace_snapshots.list_for_run(seeded.run.id))

    assert recovered is not None and recovered.state is EditorialProposalImpactState.DONE
    assert replayed is not None and replayed.state is EditorialProposalImpactState.DONE
    assert stored is not None and stored.state is EditorialProposalImpactState.DONE
    assert stored.attempt_count == 2
    assert stale_mark_calls == 1
    assert artifacts == artifacts_before
    assert snapshots == snapshots_before


@pytest.mark.asyncio
async def test_dispatch_reservation_throttles_reenqueue_without_schema_changes(
    uow_factory: UnitOfWorkFactory,
) -> None:
    seeded = await _seed(uow_factory, proposal_count=1)
    now = datetime.now(UTC) + timedelta(seconds=1)
    impact = await _insert_impact(
        uow_factory,
        seeded=seeded,
        proposal_id=seeded.proposals[0].id,
        sequence=1,
        updated_at=now - timedelta(minutes=2),
    )
    dispatch_before = now - timedelta(minutes=1)
    async with uow_factory() as uow:
        candidates = await uow.editorial_proposal_impacts.list_dispatchable(
            now=now,
            stale_running_before=now - timedelta(minutes=5),
            dispatch_before=dispatch_before,
            limit=100,
        )
        assert impact.impact_id in candidates
        assert await uow.editorial_proposal_impacts.reserve_dispatch(
            impact.impact_id,
            now=now,
            stale_running_before=now - timedelta(minutes=5),
            dispatch_before=dispatch_before,
        )
        await uow.commit()

    async with uow_factory() as uow:
        throttled = await uow.editorial_proposal_impacts.list_dispatchable(
            now=now + timedelta(seconds=30),
            stale_running_before=now - timedelta(minutes=5),
            dispatch_before=now + timedelta(seconds=30) - timedelta(minutes=1),
            limit=100,
        )
        retryable = await uow.editorial_proposal_impacts.list_dispatchable(
            now=now + timedelta(minutes=1, seconds=1),
            stale_running_before=now - timedelta(minutes=5),
            dispatch_before=now + timedelta(seconds=1),
            limit=100,
        )

    assert impact.impact_id not in throttled
    assert impact.impact_id in retryable
