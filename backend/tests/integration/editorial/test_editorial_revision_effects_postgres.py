from __future__ import annotations

import asyncio
import hashlib
import json
from typing import Any
from uuid import uuid4

import pytest

from cti_app.application.editorial_revision_effects import (
    EditorialRevisionEffects,
    EditorialRevisionEffectsError,
)
from cti_app.application.persistence import UnitOfWorkFactory
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.domain.blobs import BlobDescriptor, BlobRecord
from cti_app.domain.editorial_proposal import EditorialProposalState
from cti_app.domain.production import ProductionArtifact, ProductionArtifactStage
from tests.integration.editorial.test_proposal_adoption_postgres import (
    _adopt_kwargs,
    _seed,
    _service,
)

pytestmark = pytest.mark.integration


async def _legacy_synthesis_revise(uow_factory: UnitOfWorkFactory, seeded: Any) -> Any:
    canonical_content = json.loads(
        seeded.artifact_store.blobs[seeded.base_artifact.canonical_blob_id]
    )
    canonical_content["title"] = "Synthetic legacy revision"
    payload = ProductionArtifactStore.canonical_json_bytes(canonical_content)
    canonical_sha256 = hashlib.sha256(payload).hexdigest()
    descriptor = BlobDescriptor(
        sha256=canonical_sha256,
        size=len(payload),
        mime_type="application/json",
        logical_bucket="production-artifacts-canonical",
    )
    effects = EditorialRevisionEffects(workspace_service=seeded.workspace)

    async with uow_factory() as uow:
        guard = await effects.acquire_run_guard(uow, seeded.run.id)
        await effects.lock_run_row(uow, guard)
        current = await uow.production_artifacts.get_current(
            seeded.run.id, ProductionArtifactStage.SYNTHESIS.value
        )
        if current is None or current.id != seeded.base_artifact.id:
            raise EditorialRevisionEffectsError("legacy_revision_base_stale")
        base_snapshot = await uow.editorial_workspace_snapshots.latest_for_run(seeded.run.id)
        if base_snapshot is None:
            raise EditorialRevisionEffectsError("editorial_snapshot_missing")
        blob = await uow.blobs.add_if_absent(BlobRecord(descriptor=descriptor))
        artifact = ProductionArtifact(
            id=uuid4(),
            production_run_id=seeded.run.id,
            subject_id=seeded.subject.id,
            stage=ProductionArtifactStage.SYNTHESIS,
            version=seeded.base_artifact.version + 1,
            input_hash=seeded.base_artifact.input_hash,
            canonical_blob_id=blob.id,
            metadata={"legacy_revision": {"base_artifact_id": str(seeded.base_artifact.id)}},
        )
        await uow.production_artifacts.append(artifact)
        result = await effects.apply_after_write(
            uow,
            run_guard=guard,
            base_snapshot=base_snapshot,
            stage=ProductionArtifactStage.SYNTHESIS,
            artifact=artifact,
            canonical_sha256=canonical_sha256,
            canonical_content=canonical_content,
            lineage=None,
            actor_id=seeded.actor_id,
        )
        await uow.commit()
        return result


@pytest.mark.asyncio
async def test_legacy_revise_advances_registry_and_persists_changed_pin_proposals_stale(
    uow_factory: UnitOfWorkFactory,
) -> None:
    seeded = await _seed(uow_factory, proposal_count=2)

    result = await _legacy_synthesis_revise(uow_factory, seeded)

    async with uow_factory() as uow:
        latest = await uow.editorial_workspace_snapshots.latest_for_run(seeded.run.id)
        assert latest is not None
        assert latest.snapshot_id == result.snapshot.snapshot_id
        assert latest.parent_snapshot_id == seeded.snapshot.snapshot_id
        assert latest.synthesis_artifact_id == result.snapshot.synthesis_artifact_id
        assert latest.synthesis_version == seeded.base_artifact.version + 1
        for proposal in seeded.proposals:
            current = await uow.editorial_proposals.get_for_occurrence(
                proposal.subject_id, proposal.occurrence_id, proposal.id
            )
            assert current is not None and current.state is EditorialProposalState.STALE
            assert current.state_version == proposal.state_version + 1
        assert await uow.editorial_proposal_impacts.get_by_proposal(seeded.proposals[0].id) is None


@pytest.mark.asyncio
async def test_accept_races_legacy_revise_with_one_winner_and_one_snapshot(
    uow_factory: UnitOfWorkFactory,
) -> None:
    seeded = await _seed(uow_factory, proposal_count=1)
    proposal = seeded.proposals[0]
    service = _service(seeded, uow_factory)

    results = await asyncio.gather(
        service.adopt(**_adopt_kwargs(seeded, proposal, f"accept-{uuid4().hex}")),
        _legacy_synthesis_revise(uow_factory, seeded),
        return_exceptions=True,
    )

    winners = [item for item in results if not isinstance(item, BaseException)]
    assert len(winners) == 1
    async with uow_factory() as uow:
        current_proposal = await uow.editorial_proposals.get_for_occurrence(
            proposal.subject_id, proposal.occurrence_id, proposal.id
        )
        assert current_proposal is not None
        latest = await uow.editorial_workspace_snapshots.latest_for_run(seeded.run.id)
        assert latest is not None
        assert latest.parent_snapshot_id == seeded.snapshot.snapshot_id
        if current_proposal.state is EditorialProposalState.ACCEPTED:
            impact = await uow.editorial_proposal_impacts.get_by_proposal(proposal.id)
            assert impact is not None and impact.accepted_snapshot_id == latest.snapshot_id
        else:
            assert current_proposal.state is EditorialProposalState.STALE
            assert await uow.editorial_proposal_impacts.get_by_proposal(proposal.id) is None
