from __future__ import annotations

import hashlib
from dataclasses import replace
from typing import Any
from uuid import NAMESPACE_URL, uuid4, uuid5

import pytest

from cti_app.application.editorial_revision_effects import (
    EditorialRevisionEffects,
    EditorialRevisionWrite,
    editorial_actor_storage_id,
)
from cti_app.domain.editorial_proposal import EditorialProposalState
from cti_app.domain.production import ProductionArtifactStage
from tests.test_editorial_proposal_adoption import _fixture


def _artifact(base: Any, *, stage: ProductionArtifactStage, version: int) -> Any:
    return replace(
        base,
        id=uuid4(),
        stage=stage,
        version=version,
        canonical_blob_id=uuid4(),
    )


@pytest.mark.asyncio
async def test_legacy_synthesis_revise_advances_registry_and_stales_changed_pin_proposals() -> None:
    fixture = _fixture(with_sibling=True)
    base = fixture.factory.state.snapshots[fixture.proposal.snapshot_id]
    base_artifact = fixture.factory.state.artifacts[fixture.proposal.base_artifact_id]
    candidate = fixture.service.plan.candidate
    artifact = _artifact(base_artifact, stage=ProductionArtifactStage.SYNTHESIS, version=2)
    sibling_id = next(
        item for item in fixture.factory.state.proposals if item != fixture.proposal.id
    )
    effects = EditorialRevisionEffects(workspace_service=fixture.service._workspace_service)

    uow = fixture.factory()
    async with uow:
        guard = await effects.acquire_run_guard(uow, fixture.proposal.production_run_id)
        await effects.lock_run_row(uow, guard)
        await uow.production_artifacts.append(artifact)
        result = await effects.apply_after_write(
            uow,
            run_guard=guard,
            base_snapshot=base,
            stage=ProductionArtifactStage.SYNTHESIS,
            artifact=artifact,
            canonical_sha256=candidate.canonical_sha256,
            canonical_content=candidate.payload,
            lineage=None,
            actor_id="dev-analyst",
        )
        await uow.commit()

    stored = fixture.factory.state
    assert result.snapshot.parent_snapshot_id == base.snapshot_id
    assert result.snapshot.synthesis_artifact_id == artifact.id
    assert result.snapshot.synthesis_version == 2
    assert stored.proposals[fixture.proposal.id].state is EditorialProposalState.STALE
    assert stored.proposals[sibling_id].state is EditorialProposalState.STALE
    assert (
        len([event for event in stored.events if event.state is EditorialProposalState.STALE]) == 2
    )
    expected_actor_id = uuid5(NAMESPACE_URL, "autowork-actor:dev-analyst")
    assert all(
        event.actor_id == expected_actor_id
        for event in stored.events
        if event.state is EditorialProposalState.STALE
    )
    assert not stored.impacts


def test_opaque_actor_storage_id_matches_workspace_actor_convention() -> None:
    assert editorial_actor_storage_id("dev-analyst") == uuid5(
        NAMESPACE_URL, "autowork-actor:dev-analyst"
    )
    configured_uuid = uuid4()
    assert editorial_actor_storage_id(str(configured_uuid)) == configured_uuid
    assert editorial_actor_storage_id(configured_uuid) == configured_uuid


@pytest.mark.asyncio
async def test_combined_synthesis_and_enrichment_writes_append_one_final_snapshot() -> None:
    fixture = _fixture()
    base = fixture.factory.state.snapshots[fixture.proposal.snapshot_id]
    base_artifact = fixture.factory.state.artifacts[fixture.proposal.base_artifact_id]
    synthesis_artifact = _artifact(
        base_artifact, stage=ProductionArtifactStage.SYNTHESIS, version=2
    )
    enrichment_artifact = _artifact(
        base_artifact, stage=ProductionArtifactStage.EDITORIAL_ENRICHMENT, version=2
    )
    effects = EditorialRevisionEffects(workspace_service=fixture.service._workspace_service)
    synthesis_content = {"title": "Synthetic revised title"}
    enrichment_content = {"tables": [], "diagrams": [], "charts": []}
    uow = fixture.factory()
    async with uow:
        guard = await effects.acquire_run_guard(uow, fixture.proposal.production_run_id)
        await effects.lock_run_row(uow, guard)
        await uow.production_artifacts.append(synthesis_artifact)
        await uow.production_artifacts.append(enrichment_artifact)
        result = await effects.apply_after_writes(
            uow,
            run_guard=guard,
            base_snapshot=base,
            writes=(
                EditorialRevisionWrite(
                    stage=ProductionArtifactStage.SYNTHESIS,
                    artifact=synthesis_artifact,
                    canonical_sha256=hashlib.sha256(b"synthetic-synthesis").hexdigest(),
                    canonical_content=synthesis_content,
                ),
                EditorialRevisionWrite(
                    stage=ProductionArtifactStage.EDITORIAL_ENRICHMENT,
                    artifact=enrichment_artifact,
                    canonical_sha256=hashlib.sha256(b"synthetic-enrichment").hexdigest(),
                    canonical_content=enrichment_content,
                ),
            ),
            actor_id="dev-analyst",
        )
        await uow.commit()

    assert result.snapshot.parent_snapshot_id == base.snapshot_id
    assert result.snapshot.synthesis_artifact_id == synthesis_artifact.id
    assert result.snapshot.enrichment_artifact_id == enrichment_artifact.id
    assert len(fixture.factory.state.snapshots) == 2


@pytest.mark.asyncio
async def test_legacy_enrichment_revise_and_restore_advance_registry() -> None:
    fixture = _fixture()
    base = fixture.factory.state.snapshots[fixture.proposal.snapshot_id]
    base_artifact = fixture.factory.state.artifacts[fixture.proposal.base_artifact_id]
    effects = EditorialRevisionEffects(workspace_service=fixture.service._workspace_service)
    payload = {"tables": [], "diagrams": [], "charts": [], "source_figures": [], "annotations": []}
    content_sha = hashlib.sha256(b"synthetic-enrichment").hexdigest()

    first_artifact = _artifact(
        base_artifact, stage=ProductionArtifactStage.EDITORIAL_ENRICHMENT, version=2
    )
    uow = fixture.factory()
    async with uow:
        guard = await effects.acquire_run_guard(uow, fixture.proposal.production_run_id)
        await effects.lock_run_row(uow, guard)
        await uow.production_artifacts.append(first_artifact)
        first = await effects.apply_after_write(
            uow,
            run_guard=guard,
            base_snapshot=base,
            stage=ProductionArtifactStage.EDITORIAL_ENRICHMENT,
            artifact=first_artifact,
            canonical_sha256=content_sha,
            canonical_content=payload,
            lineage=None,
            actor_id=fixture.actor_id,
        )
        await uow.commit()

    restored_artifact = _artifact(
        base_artifact, stage=ProductionArtifactStage.EDITORIAL_ENRICHMENT, version=3
    )
    uow = fixture.factory()
    async with uow:
        guard = await effects.acquire_run_guard(uow, fixture.proposal.production_run_id)
        await effects.lock_run_row(uow, guard)
        await uow.production_artifacts.append(restored_artifact)
        restored = await effects.apply_after_write(
            uow,
            run_guard=guard,
            base_snapshot=first.snapshot,
            stage=ProductionArtifactStage.EDITORIAL_ENRICHMENT,
            artifact=restored_artifact,
            canonical_sha256=hashlib.sha256(b"synthetic-restored-enrichment").hexdigest(),
            canonical_content=payload,
            lineage=None,
            actor_id=fixture.actor_id,
        )
        await uow.commit()

    assert first.snapshot.enrichment_artifact_id == first_artifact.id
    assert restored.snapshot.enrichment_artifact_id == restored_artifact.id
    assert restored.snapshot.parent_snapshot_id == first.snapshot.snapshot_id
    assert restored.impact is None


@pytest.mark.asyncio
async def test_impact_rebase_advances_registry_under_run_guard() -> None:
    fixture = _fixture()
    base = fixture.factory.state.snapshots[fixture.proposal.snapshot_id]
    base_artifact = fixture.factory.state.artifacts[fixture.proposal.base_artifact_id]
    artifact = _artifact(
        base_artifact, stage=ProductionArtifactStage.EDITORIAL_ENRICHMENT, version=2
    )
    effects = EditorialRevisionEffects(workspace_service=fixture.service._workspace_service)
    lock_order: list[str] = []
    content = {"tables": [], "diagrams": [], "charts": [], "source_figures": [], "annotations": []}

    uow = fixture.factory()
    async with uow:
        original_advisory_lock = uow.editorial_proposals.lock_adoption_for_run
        original_run_lock = uow.production_runs.get_for_update

        async def advisory_lock(run_id):
            lock_order.append("run_advisory")
            await original_advisory_lock(run_id)

        async def run_lock(run_id):
            lock_order.append("run_row")
            return await original_run_lock(run_id)

        uow.editorial_proposals.lock_adoption_for_run = advisory_lock
        uow.production_runs.get_for_update = run_lock
        guard = await effects.acquire_run_guard(uow, fixture.proposal.production_run_id)
        await effects.lock_run_row(uow, guard)
        await uow.production_artifacts.append(artifact)
        result = await effects.apply_after_write(
            uow,
            run_guard=guard,
            base_snapshot=base,
            stage=ProductionArtifactStage.EDITORIAL_ENRICHMENT,
            artifact=artifact,
            canonical_sha256=hashlib.sha256(b"synthetic-rebase").hexdigest(),
            canonical_content=content,
            lineage=None,
            actor_id=fixture.actor_id,
        )
        await uow.commit()

    assert lock_order[:2] == ["run_advisory", "run_row"]
    assert result.snapshot.enrichment_artifact_id == artifact.id
    assert result.snapshot.parent_snapshot_id == base.snapshot_id
    assert not result.impact


@pytest.mark.asyncio
async def test_adoption_uses_the_same_effects_step_without_behavior_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _fixture()
    original_apply = EditorialRevisionEffects.apply_after_write
    calls = 0

    async def record_call(self, *args, **kwargs):
        nonlocal calls
        calls += 1
        return await original_apply(self, *args, **kwargs)

    monkeypatch.setattr(EditorialRevisionEffects, "apply_after_write", record_call)
    result = await fixture.adopt()

    assert calls == 1
    assert result.proposal.state is EditorialProposalState.ACCEPTED
    assert result.snapshot.synthesis_version == 2
    assert result.impact.accepted_snapshot_id == result.snapshot.snapshot_id
    assert fixture.factory.commits == 1
