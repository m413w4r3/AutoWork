from __future__ import annotations

import asyncio
import hashlib
from dataclasses import replace
from datetime import date
from uuid import UUID, uuid4

import pytest

from cti_app.application.editorial_workspace.registry import WorkspaceSnapshotRecord, registry_hash
from cti_app.application.persistence import UnitOfWork, UnitOfWorkFactory
from cti_app.domain.blobs import BlobDescriptor, BlobRecord
from cti_app.domain.classification import TLP
from cti_app.domain.editions import Edition
from cti_app.domain.editorial_proposal import (
    EditorialProposal,
    EditorialProposalEvent,
    EditorialProposalEventKind,
    EditorialProposalOrigin,
    EditorialProposalState,
)
from cti_app.domain.editorial_proposal_impact import EditorialProposalImpact
from cti_app.domain.entities import Subject
from cti_app.domain.production import ProductionArtifact, ProductionArtifactStage, ProductionRun
from cti_app.domain.publication_kind import PublicationKind

pytestmark = pytest.mark.integration


async def _seed(uow_factory: UnitOfWorkFactory, *, proposal_count: int = 2) -> dict[str, object]:
    edition = Edition(
        country="France",
        country_code="FR",
        period_start=date(2026, 11, 1),
        period_end=date(2026, 11, 30),
        tlp=TLP.AMBER,
        languages=("fr",),
    )
    subject = Subject(
        edition_id=edition.id,
        title="Synthetic adoption subject",
        slug=f"proposal-adoption-{uuid4().hex[:12]}",
        tlp=TLP.AMBER,
    )
    run = ProductionRun(subject_id=subject.id, edition_id=edition.id)
    base_artifact = ProductionArtifact(
        production_run_id=run.id,
        subject_id=subject.id,
        stage=ProductionArtifactStage.SYNTHESIS,
        version=1,
        input_hash="1" * 64,
    )
    snapshot = WorkspaceSnapshotRecord(
        snapshot_id=uuid4(),
        subject_id=subject.id,
        production_run_id=run.id,
        synthesis_artifact_id=base_artifact.id,
        synthesis_version=1,
        synthesis_input_hash=base_artifact.input_hash,
        synthesis_canonical_sha256="2" * 64,
        enrichment_artifact_id=None,
        enrichment_version=None,
        enrichment_input_hash=None,
        enrichment_canonical_sha256=None,
        publication_artifact_id=None,
        publication_version=None,
        publication_input_hash=None,
        publication_canonical_sha256=None,
        pipeline_generation=0,
        routing_decision_id=None,
        routing_decision_hash=None,
        publication_kind=PublicationKind.ARTICLE,
        workspace_pin_hash="3" * 64,
        registry_hash=registry_hash(()),
        block_registry=(),
    )
    patch_bytes = b'{"schema_version":"editorial-patch-v1","synthetic":true}'
    patch_sha256 = hashlib.sha256(patch_bytes).hexdigest()
    patch_blob = BlobRecord(
        descriptor=BlobDescriptor(
            sha256=patch_sha256,
            size=len(patch_bytes),
            mime_type="application/vnd.autowork.editorial-patch+json",
            logical_bucket="editorial-proposal-patches",
        )
    )
    actor_id = uuid4()
    variant_group_id = uuid4()
    occurrence_id = uuid4()
    proposals: list[EditorialProposal] = []
    for index in range(proposal_count):
        proposal = EditorialProposal(
            id=uuid4(),
            subject_id=subject.id,
            production_run_id=run.id,
            origin=EditorialProposalOrigin.MANUAL_PATCH,
            snapshot_id=snapshot.snapshot_id,
            occurrence_id=occurrence_id,
            base_artifact_id=base_artifact.id,
            base_artifact_version=1,
            base_input_hash=base_artifact.input_hash,
            base_canonical_sha256=snapshot.synthesis_canonical_sha256,
            base_routing_hash=None,
            base_pin_hash=snapshot.workspace_pin_hash,
            target_id=f"synthetic-target-{index}",
            operation="rewrite_title",
            before_hash="4" * 64,
            patch_blob_ref=patch_blob.id,
            patch_sha256=patch_sha256,
            evidence_refs=(),
            diagnostics={},
            thread_id=None,
            variant_group_id=variant_group_id,
            model_run_id=None,
            model_turn_id=None,
            state=EditorialProposalState.PROPOSED,
            state_version=1,
            actor_id=actor_id,
            idempotency_key=f"synthetic-adoption-{index}-{uuid4().hex}",
            request_sha256=hashlib.sha256(f"prepared-{index}".encode()).hexdigest(),
        )
        proposals.append(proposal)

    async with uow_factory() as uow:
        await uow.editions.add_if_absent(edition)
        await uow.subjects.add(subject)
        await uow.production_runs.add(run)
        await uow.production_artifacts.append(base_artifact)
        await uow.blobs.add_if_absent(patch_blob)
        await uow.editorial_workspace_snapshots.append_if_absent(snapshot)
        for proposal in proposals:
            event = EditorialProposalEvent(
                id=uuid4(),
                proposal_id=proposal.id,
                sequence=1,
                kind=EditorialProposalEventKind.PREPARED,
                state=EditorialProposalState.PROPOSED,
                state_version=1,
                actor_id=actor_id,
                idempotency_key=proposal.idempotency_key,
                request_sha256=proposal.request_sha256,
                patch_sha256=patch_sha256,
            )
            stored, inserted = await uow.editorial_proposals.add_if_absent(
                proposal, event=event, model_run=None
            )
            assert inserted and stored.id == proposal.id
        await uow.commit()
    return {
        "subject": subject,
        "run": run,
        "base_artifact": base_artifact,
        "snapshot": snapshot,
        "proposals": tuple(proposals),
        "actor_id": actor_id,
        "patch_sha256": patch_sha256,
    }


async def _adopt_in_uow(
    uow: UnitOfWork,
    *,
    subject_id: UUID,
    run_id: UUID,
    proposal_id: UUID,
    occurrence_id: UUID,
    actor_id: UUID,
    fail_before_commit: bool = False,
) -> str:
    proposal = await uow.editorial_proposals.get_for_occurrence(
        subject_id, occurrence_id, proposal_id
    )
    if proposal is None:
        raise AssertionError("seeded proposal not found")
    key = f"accept-{proposal.id.hex}"
    request_sha256 = hashlib.sha256(f"accept-request-{proposal.id.hex}".encode()).hexdigest()

    await uow.editorial_proposals.lock_adoption_for_run(run_id)
    proposal = await uow.editorial_proposals.get_for_occurrence_for_update(
        subject_id, proposal.occurrence_id, proposal_id
    )
    if proposal is None:
        raise AssertionError("seeded proposal not found")
    replay = await uow.editorial_proposals.get_event_by_idempotency(proposal_id, key)
    if replay is not None:
        return "replayed" if replay.request_sha256 == request_sha256 else "conflict"
    if proposal.state is not EditorialProposalState.PROPOSED:
        return "stale"

    run = await uow.production_runs.get_for_update(run_id)
    if run is None:
        raise AssertionError("seeded run not found")
    base_snapshot = await uow.editorial_workspace_snapshots.get_by_id(proposal.snapshot_id)
    if base_snapshot is None:
        raise AssertionError("seeded snapshot not found")

    canonical_bytes = f'{{"adopted_proposal":"{proposal.id}"}}'.encode()
    canonical_sha256 = hashlib.sha256(canonical_bytes).hexdigest()
    canonical_blob = await uow.blobs.add_if_absent(
        BlobRecord(
            descriptor=BlobDescriptor(
                sha256=canonical_sha256,
                size=len(canonical_bytes),
                mime_type="application/json",
                logical_bucket="production-artifacts-canonical",
            )
        )
    )
    artifact = ProductionArtifact(
        production_run_id=run_id,
        subject_id=subject_id,
        stage=ProductionArtifactStage.SYNTHESIS,
        version=2,
        input_hash=proposal.base_input_hash,
        canonical_blob_id=canonical_blob.id,
        metadata={"synthetic_adoption": True},
    )
    await uow.production_artifacts.append(artifact)
    accepted_snapshot = replace(
        base_snapshot,
        snapshot_id=uuid4(),
        synthesis_artifact_id=artifact.id,
        synthesis_version=artifact.version,
        synthesis_canonical_sha256=canonical_sha256,
        workspace_pin_hash=hashlib.sha256(f"pins-{artifact.id}".encode()).hexdigest(),
        parent_snapshot_id=base_snapshot.snapshot_id,
    )
    accepted_snapshot = await uow.editorial_workspace_snapshots.append_if_absent(accepted_snapshot)
    decision_id = uuid4()
    accepted_event = EditorialProposalEvent(
        id=uuid4(),
        proposal_id=proposal.id,
        sequence=proposal.state_version + 1,
        kind=EditorialProposalEventKind.STATE_CHANGED,
        state=EditorialProposalState.ACCEPTED,
        state_version=proposal.state_version + 1,
        actor_id=actor_id,
        idempotency_key=key,
        request_sha256=request_sha256,
        patch_sha256=proposal.patch_sha256,
    )
    accepted, changed = await uow.editorial_proposals.transition_if_current(
        subject_id=subject_id,
        occurrence_id=proposal.occurrence_id,
        proposal_id=proposal.id,
        expected_state=EditorialProposalState.PROPOSED,
        expected_state_version=proposal.state_version,
        state=EditorialProposalState.ACCEPTED,
        event=accepted_event,
        decision_id=decision_id,
        accepted_artifact_id=artifact.id,
    )
    if accepted is None or not changed:
        return "stale"

    for sibling in await uow.editorial_proposals.list_proposed_for_run_for_update(run_id):
        if sibling.id == proposal.id:
            continue
        stale_event = EditorialProposalEvent(
            id=uuid4(),
            proposal_id=sibling.id,
            sequence=sibling.state_version + 1,
            kind=EditorialProposalEventKind.STATE_CHANGED,
            state=EditorialProposalState.STALE,
            state_version=sibling.state_version + 1,
            actor_id=actor_id,
            idempotency_key=f"stale-{sibling.id.hex}-{sibling.state_version}",
            request_sha256=hashlib.sha256(f"stale-{sibling.id}".encode()).hexdigest(),
            patch_sha256=sibling.patch_sha256,
        )
        await uow.editorial_proposals.transition_if_current(
            subject_id=sibling.subject_id,
            occurrence_id=sibling.occurrence_id,
            proposal_id=sibling.id,
            expected_state=EditorialProposalState.PROPOSED,
            expected_state_version=sibling.state_version,
            state=EditorialProposalState.STALE,
            event=stale_event,
        )

    impact = EditorialProposalImpact(
        impact_id=uuid4(),
        subject_id=subject_id,
        production_run_id=run_id,
        proposal_id=proposal.id,
        accepted_artifact_id=artifact.id,
        accepted_snapshot_id=accepted_snapshot.snapshot_id,
        base_pin_hash=proposal.base_pin_hash,
        accepted_pin_hash=accepted_snapshot.workspace_pin_hash,
        sequence=await uow.editorial_proposal_impacts.next_sequence_for_run(run_id),
    )
    await uow.editorial_proposal_impacts.add(impact)
    if fail_before_commit:
        raise RuntimeError("synthetic adoption transaction crash")
    await uow.commit()
    return "accepted"


@pytest.mark.asyncio
async def test_two_concurrent_adoptions_have_one_cas_winner_and_stale_sibling(
    uow_factory: UnitOfWorkFactory,
) -> None:
    seeded = await _seed(uow_factory)
    subject = seeded["subject"]
    run = seeded["run"]
    proposals = seeded["proposals"]
    actor_id = seeded["actor_id"]
    assert isinstance(subject, Subject) and isinstance(run, ProductionRun)
    assert isinstance(proposals, tuple) and len(proposals) == 2 and isinstance(actor_id, UUID)
    first, second = proposals

    async def adopt(proposal: EditorialProposal) -> str:
        async with uow_factory() as uow:
            return await _adopt_in_uow(
                uow,
                subject_id=subject.id,
                run_id=run.id,
                proposal_id=proposal.id,
                occurrence_id=proposal.occurrence_id,
                actor_id=actor_id,
            )

    results = await asyncio.gather(adopt(first), adopt(second))

    assert sorted(results) == ["accepted", "stale"]
    async with uow_factory() as uow:
        persisted = [
            await uow.editorial_proposals.get_for_occurrence(
                proposal.subject_id, proposal.occurrence_id, proposal.id
            )
            for proposal in proposals
        ]
        assert (
            sum(
                item is not None and item.state is EditorialProposalState.ACCEPTED
                for item in persisted
            )
            == 1
        )
        assert (
            sum(
                item is not None and item.state is EditorialProposalState.STALE
                for item in persisted
            )
            == 1
        )
        impacts = [
            await uow.editorial_proposal_impacts.get_by_proposal(proposal.id)
            for proposal in proposals
        ]
        assert sum(item is not None for item in impacts) == 1
        assert next(item for item in impacts if item is not None).sequence == 1


@pytest.mark.asyncio
async def test_adoption_relational_changes_roll_back_atomically(
    uow_factory: UnitOfWorkFactory,
) -> None:
    seeded = await _seed(uow_factory, proposal_count=1)
    subject, run = seeded["subject"], seeded["run"]
    proposal = seeded["proposals"][0]
    actor_id = seeded["actor_id"]
    assert isinstance(subject, Subject) and isinstance(run, ProductionRun)
    assert isinstance(proposal, EditorialProposal) and isinstance(actor_id, UUID)

    with pytest.raises(RuntimeError, match="transaction crash"):
        async with uow_factory() as uow:
            await _adopt_in_uow(
                uow,
                subject_id=subject.id,
                run_id=run.id,
                proposal_id=proposal.id,
                occurrence_id=proposal.occurrence_id,
                actor_id=actor_id,
                fail_before_commit=True,
            )

    async with uow_factory() as uow:
        persisted = await uow.editorial_proposals.get_for_occurrence(
            subject.id, proposal.occurrence_id, proposal.id
        )
        assert persisted is not None and persisted.state is EditorialProposalState.PROPOSED
        assert await uow.editorial_proposal_impacts.get_by_proposal(proposal.id) is None
        assert len(await uow.production_artifacts.list_for_run(run.id)) == 1
        assert (
            await uow.editorial_proposals.get_event_by_idempotency(
                proposal.id, f"accept-{proposal.id.hex}"
            )
            is None
        )


@pytest.mark.asyncio
async def test_accepting_variant_marks_sibling_stale_in_same_transaction(
    uow_factory: UnitOfWorkFactory,
) -> None:
    seeded = await _seed(uow_factory)
    subject, run = seeded["subject"], seeded["run"]
    proposals = seeded["proposals"]
    actor_id = seeded["actor_id"]
    assert isinstance(subject, Subject) and isinstance(run, ProductionRun)
    assert isinstance(proposals, tuple) and len(proposals) == 2 and isinstance(actor_id, UUID)
    accepted, sibling = proposals

    async with uow_factory() as uow:
        result = await _adopt_in_uow(
            uow,
            subject_id=subject.id,
            run_id=run.id,
            proposal_id=accepted.id,
            occurrence_id=accepted.occurrence_id,
            actor_id=actor_id,
        )
    assert result == "accepted"

    async with uow_factory() as uow:
        accepted_row = await uow.editorial_proposals.get_for_occurrence(
            subject.id, accepted.occurrence_id, accepted.id
        )
        stale_row = await uow.editorial_proposals.get_for_occurrence(
            subject.id, sibling.occurrence_id, sibling.id
        )
        impact = await uow.editorial_proposal_impacts.get_by_proposal(accepted.id)
        assert accepted_row is not None and accepted_row.state is EditorialProposalState.ACCEPTED
        assert stale_row is not None and stale_row.state is EditorialProposalState.STALE
        assert (
            impact is not None and impact.accepted_artifact_id == accepted_row.accepted_artifact_id
        )


@pytest.mark.asyncio
async def test_adoption_crash_after_commit_before_impact_is_replayable(
    uow_factory: UnitOfWorkFactory,
) -> None:
    seeded = await _seed(uow_factory, proposal_count=1)
    subject, run = seeded["subject"], seeded["run"]
    proposal = seeded["proposals"][0]
    actor_id = seeded["actor_id"]
    assert isinstance(subject, Subject) and isinstance(run, ProductionRun)
    assert isinstance(proposal, EditorialProposal) and isinstance(actor_id, UUID)

    async with uow_factory() as uow:
        assert (
            await _adopt_in_uow(
                uow,
                subject_id=subject.id,
                run_id=run.id,
                proposal_id=proposal.id,
                occurrence_id=proposal.occurrence_id,
                actor_id=actor_id,
            )
            == "accepted"
        )
    async with uow_factory() as uow:
        persisted = await uow.editorial_proposals.get_for_occurrence(
            subject.id, proposal.occurrence_id, proposal.id
        )
        impact = await uow.editorial_proposal_impacts.get_by_proposal(proposal.id)
        assert persisted is not None and persisted.state is EditorialProposalState.ACCEPTED
        assert impact is not None and impact.state.value == "pending"
        assert (
            await _adopt_in_uow(
                uow,
                subject_id=subject.id,
                run_id=run.id,
                proposal_id=proposal.id,
                occurrence_id=proposal.occurrence_id,
                actor_id=actor_id,
            )
            == "replayed"
        )
