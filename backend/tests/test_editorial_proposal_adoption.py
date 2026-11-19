from __future__ import annotations

import copy
import hashlib
from dataclasses import dataclass, field, replace
from io import BytesIO
from typing import Any, cast
from uuid import UUID, uuid4

import pytest

from cti_app.application.editorial_proposal_adoption import (
    EditorialProposalAdoptionError,
    EditorialProposalAdoptionInputs,
    EditorialProposalAdoptionService,
    _Candidate,
    _CandidatePlan,
    _SynthesisCandidatePlan,
)
from cti_app.application.editorial_synthesis.candidate import (
    SynthesisCandidate,
    SynthesisCandidatePersistence,
)
from cti_app.application.editorial_workspace.registry import (
    WorkspaceOccurrenceResolution,
    WorkspaceSnapshotRecord,
    registry_hash,
)
from cti_app.application.persistence import UnitOfWork
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.domain.blobs import BlobDescriptor, BlobRecord
from cti_app.domain.editorial_proposal import (
    EditorialProposal,
    EditorialProposalEvent,
    EditorialProposalEventKind,
    EditorialProposalOrigin,
    EditorialProposalState,
)
from cti_app.domain.editorial_proposal_impact import EditorialProposalImpact
from cti_app.domain.editorial_workspace import (
    AdvancedBlockRegistry,
    BlockRegistryEntry,
    EditorialBlockKind,
    EditorialRevisionStatus,
    EditorialSourceStage,
    EditorialTransition,
)
from cti_app.domain.production import ProductionArtifact, ProductionArtifactStage, ProductionRun
from cti_app.domain.production_editorial_revision import (
    TargetedEditorialRevisionTarget,
    TargetedEditorialRevisionTargetKind,
)
from cti_app.domain.publication_kind import PublicationKind

_PATCH_BUCKET = "editorial-proposal-patches"
_PATCH_MIME = "application/vnd.autowork.editorial-patch+json"
_CANONICAL_BUCKET = "production-artifacts-canonical"


@dataclass
class _MemoryState:
    proposals: dict[UUID, EditorialProposal] = field(default_factory=dict)
    events: list[EditorialProposalEvent] = field(default_factory=list)
    blobs: dict[UUID, BlobRecord] = field(default_factory=dict)
    artifacts: dict[UUID, ProductionArtifact] = field(default_factory=dict)
    snapshots: dict[UUID, WorkspaceSnapshotRecord] = field(default_factory=dict)
    impacts: dict[UUID, EditorialProposalImpact] = field(default_factory=dict)
    thread_events: list[dict[str, Any]] = field(default_factory=list)
    run: ProductionRun | None = None


class _MemoryUowFactory:
    def __init__(self, state: _MemoryState) -> None:
        self.state = state
        self.commits = 0

    def __call__(self) -> _MemoryUow:
        return _MemoryUow(self)


class _MemoryUow:
    def __init__(self, factory: _MemoryUowFactory) -> None:
        self.factory = factory
        self.state = copy.deepcopy(factory.state)
        self.editorial_proposals = _MemoryProposalRepository(self)
        self.blobs = _MemoryBlobRepository(self)
        self.production_artifacts = _MemoryArtifactRepository(self)
        self.production_runs = _MemoryRunRepository(self)
        self.editorial_workspace_snapshots = _MemorySnapshotRepository(self)
        self.editorial_threads = _MemoryThreadRepository(self)
        self.editorial_proposal_impacts = _MemoryImpactRepository(self)

    async def __aenter__(self) -> _MemoryUow:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def commit(self) -> None:
        self.factory.state = copy.deepcopy(self.state)
        self.factory.commits += 1


class _MemoryProposalRepository:
    def __init__(self, uow: _MemoryUow) -> None:
        self.uow = uow

    async def lock_adoption_for_run(self, _production_run_id: UUID) -> None:
        return None

    async def get_for_occurrence(
        self, _subject_id: UUID, _occurrence_id: UUID, proposal_id: UUID
    ) -> EditorialProposal | None:
        return self.uow.state.proposals.get(proposal_id)

    async def get_for_occurrence_for_update(
        self, subject_id: UUID, occurrence_id: UUID, proposal_id: UUID
    ) -> EditorialProposal | None:
        proposal = self.uow.state.proposals.get(proposal_id)
        if (
            proposal is None
            or proposal.subject_id != subject_id
            or proposal.occurrence_id != occurrence_id
        ):
            return None
        return proposal

    async def get_event_by_idempotency(
        self, proposal_id: UUID, idempotency_key: str
    ) -> EditorialProposalEvent | None:
        return next(
            (
                item
                for item in self.uow.state.events
                if item.proposal_id == proposal_id and item.idempotency_key == idempotency_key
            ),
            None,
        )

    async def list_proposed_for_run_for_update(
        self, production_run_id: UUID
    ) -> tuple[EditorialProposal, ...]:
        return tuple(
            sorted(
                (
                    item
                    for item in self.uow.state.proposals.values()
                    if item.production_run_id == production_run_id
                    and item.state is EditorialProposalState.PROPOSED
                ),
                key=lambda item: str(item.id),
            )
        )

    async def transition_if_current(
        self,
        *,
        subject_id: UUID,
        occurrence_id: UUID,
        proposal_id: UUID,
        expected_state: EditorialProposalState,
        expected_state_version: int,
        state: EditorialProposalState,
        event: EditorialProposalEvent,
        decision_id: UUID | None = None,
        accepted_artifact_id: UUID | None = None,
    ) -> tuple[EditorialProposal | None, bool]:
        current = await self.get_for_occurrence_for_update(subject_id, occurrence_id, proposal_id)
        if (
            current is None
            or current.state is not expected_state
            or current.state_version != expected_state_version
        ):
            return current, False
        updated = replace(
            current,
            state=state,
            state_version=expected_state_version + 1,
            decision_id=decision_id,
            accepted_artifact_id=accepted_artifact_id,
        )
        self.uow.state.proposals[proposal_id] = updated
        self.uow.state.events.append(event)
        return updated, True


class _MemoryBlobRepository:
    def __init__(self, uow: _MemoryUow) -> None:
        self.uow = uow

    async def get(self, blob_id: UUID) -> BlobRecord | None:
        return self.uow.state.blobs.get(blob_id)

    async def lock_address(self, _logical_bucket: str, _sha256: str) -> None:
        return None

    async def get_by_address(self, logical_bucket: str, sha256: str) -> BlobRecord | None:
        return next(
            (
                blob
                for blob in self.uow.state.blobs.values()
                if blob.descriptor.logical_bucket == logical_bucket
                and blob.descriptor.sha256 == sha256
            ),
            None,
        )

    async def add_if_absent(self, blob: BlobRecord) -> BlobRecord:
        existing = await self.get_by_address(blob.descriptor.logical_bucket, blob.descriptor.sha256)
        if existing is not None:
            return existing
        self.uow.state.blobs[blob.id] = blob
        return blob

    async def count_references(self, blob_id: UUID) -> int:
        proposal_refs = sum(
            item.patch_blob_ref == blob_id for item in self.uow.state.proposals.values()
        )
        artifact_refs = sum(
            item.canonical_blob_id == blob_id for item in self.uow.state.artifacts.values()
        )
        return proposal_refs + artifact_refs

    async def delete(self, blob_id: UUID) -> None:
        self.uow.state.blobs.pop(blob_id, None)


class _MemoryArtifactRepository:
    def __init__(self, uow: _MemoryUow) -> None:
        self.uow = uow

    async def get(self, artifact_id: UUID) -> ProductionArtifact | None:
        return self.uow.state.artifacts.get(artifact_id)

    async def append(self, artifact: ProductionArtifact) -> None:
        self.uow.state.artifacts[artifact.id] = artifact


class _MemoryRunRepository:
    def __init__(self, uow: _MemoryUow) -> None:
        self.uow = uow

    async def get_for_update(self, _run_id: UUID) -> ProductionRun | None:
        return self.uow.state.run


class _MemorySnapshotRepository:
    def __init__(self, uow: _MemoryUow) -> None:
        self.uow = uow

    async def append_if_absent(self, record: WorkspaceSnapshotRecord) -> WorkspaceSnapshotRecord:
        self.uow.state.snapshots[record.snapshot_id] = record
        return record

    async def get_by_id(self, snapshot_id: UUID) -> WorkspaceSnapshotRecord | None:
        return self.uow.state.snapshots.get(snapshot_id)


class _MemoryThreadRepository:
    def __init__(self, uow: _MemoryUow) -> None:
        self.uow = uow

    async def append_event(self, **event: Any) -> None:
        self.uow.state.thread_events.append(event)


class _MemoryImpactRepository:
    def __init__(self, uow: _MemoryUow) -> None:
        self.uow = uow

    async def next_sequence_for_run(self, production_run_id: UUID) -> int:
        return (
            max(
                (
                    item.sequence
                    for item in self.uow.state.impacts.values()
                    if item.production_run_id == production_run_id
                ),
                default=0,
            )
            + 1
        )

    async def add(self, impact: EditorialProposalImpact) -> None:
        self.uow.state.impacts[impact.proposal_id] = impact

    async def get_by_proposal(self, proposal_id: UUID) -> EditorialProposalImpact | None:
        return self.uow.state.impacts.get(proposal_id)


class _MemoryBlobStore:
    def __init__(self, patch_descriptor: BlobDescriptor, patch_bytes: bytes) -> None:
        self.objects = {(patch_descriptor.logical_bucket, patch_descriptor.sha256): patch_bytes}
        self.read_overrides: dict[tuple[str, str], bytes] = {}
        self.put_calls = 0
        self.new_objects = 0
        self.delete_calls = 0

    async def put(self, source: BytesIO, *, logical_bucket: str, mime_type: str) -> BlobDescriptor:
        self.put_calls += 1
        payload = source.read()
        digest = hashlib.sha256(payload).hexdigest()
        key = (logical_bucket, digest)
        if key not in self.objects:
            self.objects[key] = payload
            self.new_objects += 1
        return BlobDescriptor(digest, len(payload), mime_type, logical_bucket)

    async def read(self, descriptor: BlobDescriptor, *, max_bytes: int) -> bytes:
        key = (descriptor.logical_bucket, descriptor.sha256)
        value = self.read_overrides.get(key, self.objects[key])
        if len(value) > max_bytes:
            raise ValueError("synthetic_read_limit")
        return value

    async def exists(self, descriptor: BlobDescriptor) -> bool:
        return (descriptor.logical_bucket, descriptor.sha256) in self.objects

    async def delete(self, descriptor: BlobDescriptor) -> None:
        self.delete_calls += 1
        self.objects.pop((descriptor.logical_bucket, descriptor.sha256), None)


class _MemoryArtifactStore:
    def __init__(self, canonical_bytes: bytes) -> None:
        self.canonical_bytes = canonical_bytes

    async def read_bytes(self, _blob_id: UUID, *, max_bytes: int) -> bytes:
        if len(self.canonical_bytes) > max_bytes:
            raise ValueError("synthetic_read_limit")
        return self.canonical_bytes


class _MemoryWorkspace:
    def __init__(self, resolution: WorkspaceOccurrenceResolution) -> None:
        self.resolution = resolution

    async def resolve_occurrence(
        self, _subject_id: UUID, _occurrence_id: UUID
    ) -> WorkspaceOccurrenceResolution:
        return self.resolution

    async def resolve_occurrence_in_uow(
        self, _uow: _MemoryUow, _subject_id: UUID, _occurrence_id: UUID
    ) -> WorkspaceOccurrenceResolution:
        return self.resolution

    def advance(
        self,
        *,
        base_registry: tuple[BlockRegistryEntry, ...],
        canonical_sha256: str,
        retired_block_ids: tuple[UUID, ...] = (),
        **_kwargs: Any,
    ) -> AdvancedBlockRegistry:
        active = tuple(
            replace(
                entry,
                content_sha256=canonical_sha256,
                transition=EditorialTransition.REWRITE,
            )
            for entry in base_registry
        )
        return AdvancedBlockRegistry(active=active, retired_block_ids=frozenset(retired_block_ids))


class _Inputs:
    def __init__(self) -> None:
        self.revalidated = 0
        self.gateway_calls = 0

    async def synthesis_inputs(self, *_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("test candidate plan bypasses generation and gateway inputs")

    async def enrichment_inputs(self, *_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("test candidate plan bypasses generation and gateway inputs")

    async def revalidate_evidence_policy_and_thread(self, *_args: Any, **_kwargs: Any) -> None:
        self.revalidated += 1


class _AdoptionHarness(EditorialProposalAdoptionService):
    def __init__(self, *, plan: _SynthesisCandidatePlan, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.plan = plan
        self.build_calls = 0

    async def _build_candidate(self, *_args: Any, **_kwargs: Any) -> _SynthesisCandidatePlan:
        self.build_calls += 1
        return self.plan

    async def _persist_candidate(
        self,
        uow: UnitOfWork,
        _stage: ProductionArtifactStage,
        plan: _CandidatePlan,
        blob: BlobRecord,
        *,
        proposal: EditorialProposal,
        patch_sha256: str,
    ) -> ProductionArtifact:
        assert isinstance(plan, _SynthesisCandidatePlan)
        artifact = ProductionArtifact(
            id=cast(UUID, plan.persistence.artifact_id),
            production_run_id=proposal.production_run_id,
            subject_id=proposal.subject_id,
            stage=ProductionArtifactStage.SYNTHESIS,
            version=2,
            input_hash=proposal.base_input_hash,
            canonical_blob_id=blob.id,
            metadata={
                "editorial_adoption": {
                    "proposal_id": str(proposal.id),
                    "patch_sha256": patch_sha256,
                }
            },
        )
        await uow.production_artifacts.append(artifact)
        return artifact


class _FailBeforeUpload(_AdoptionHarness):
    async def _upload_and_verify_candidate(self, _candidate: _Candidate) -> BlobDescriptor:
        raise RuntimeError("synthetic crash before upload")


class _FailBeforeUowOnce(_AdoptionHarness):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.fail_once = True

    async def _persist_acceptance(self, **kwargs: Any) -> Any:
        if self.fail_once:
            self.fail_once = False
            raise RuntimeError("synthetic crash after upload before UOW")
        return await super()._persist_acceptance(**kwargs)


class _CollectedBeforeUowOnce(_AdoptionHarness):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.collect_once = True

    async def _persist_acceptance(self, **kwargs: Any) -> Any:
        if self.collect_once:
            self.collect_once = False
            await self._blob_store.delete(kwargs["descriptor"])
        return await super()._persist_acceptance(**kwargs)


class _CrashAfterCommitOnce(_AdoptionHarness):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.fail_once = True

    async def _persist_acceptance(self, **kwargs: Any) -> Any:
        result = await super()._persist_acceptance(**kwargs)
        if self.fail_once:
            self.fail_once = False
            raise RuntimeError("synthetic crash after commit before impact worker")
        return result


@dataclass
class _Fixture:
    service: _AdoptionHarness
    factory: _MemoryUowFactory
    blob_store: _MemoryBlobStore
    inputs: _Inputs
    proposal: EditorialProposal
    actor_id: UUID
    occurrence_id: UUID
    patch_sha256: str
    idempotency_key: str

    async def accept(self, proposal: EditorialProposal | None = None) -> Any:
        target = proposal or self.proposal
        return await self.service.accept(
            target.subject_id,
            target.occurrence_id,
            target.id,
            actor_id=self.actor_id,
            expected_state_version=1,
            patch_sha256=self.patch_sha256,
            idempotency_key=self.idempotency_key,
        )


def _fixture(
    *,
    service_type: type[_AdoptionHarness] = _AdoptionHarness,
    with_sibling: bool = False,
) -> _Fixture:
    subject_id, run_id, occurrence_id = uuid4(), uuid4(), uuid4()
    base_payload = {"title": "Synthetic original"}
    base_bytes = ProductionArtifactStore.canonical_json_bytes(base_payload)
    base_sha256 = hashlib.sha256(base_bytes).hexdigest()
    base_artifact_id, base_blob_id = uuid4(), uuid4()
    base_artifact = ProductionArtifact(
        id=base_artifact_id,
        production_run_id=run_id,
        subject_id=subject_id,
        stage=ProductionArtifactStage.SYNTHESIS,
        version=1,
        input_hash="1" * 64,
        canonical_blob_id=base_blob_id,
    )
    entry = BlockRegistryEntry(
        block_id=occurrence_id,
        kind=EditorialBlockKind.TITLE,
        parent_block_id=None,
        position=0,
        content_sha256="2" * 64,
        source_stage=EditorialSourceStage.SYNTHESIS,
        structural_pointer="/title",
        revision_status=EditorialRevisionStatus.READY,
    )
    base_pin = "7" * 64
    snapshot = WorkspaceSnapshotRecord(
        snapshot_id=uuid4(),
        subject_id=subject_id,
        production_run_id=run_id,
        synthesis_artifact_id=base_artifact_id,
        synthesis_version=1,
        synthesis_input_hash=base_artifact.input_hash,
        synthesis_canonical_sha256=base_sha256,
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
        workspace_pin_hash=base_pin,
        registry_hash=registry_hash((entry,)),
        block_registry=(entry,),
    )
    resolution = WorkspaceOccurrenceResolution(
        record=snapshot,
        entry=entry,
        binding_state="valid",
        artifact_id=base_artifact_id,
        artifact_version=1,
        canonical_sha256=base_sha256,
    )
    operation = "rewrite_title"
    target = TargetedEditorialRevisionTarget(
        kind=TargetedEditorialRevisionTargetKind.TITLE,
        stable_ids=(),
    )
    patch_payload = {
        "schema_version": "editorial-patch-v1",
        "operation": operation,
        "target": target.to_json(),
        "target_id": "synthetic-title",
        "occurrence_id": str(occurrence_id),
        "before_hash": "3" * 64,
        "after": "Synthetic accepted title",
        "rationale": "Synthetic test rationale.",
    }
    patch_bytes = ProductionArtifactStore.canonical_json_bytes(patch_payload)
    patch_sha256 = hashlib.sha256(patch_bytes).hexdigest()
    patch_descriptor = BlobDescriptor(patch_sha256, len(patch_bytes), _PATCH_MIME, _PATCH_BUCKET)
    patch_blob = BlobRecord(descriptor=patch_descriptor)
    proposal_id, actor_id, variant_group_id = uuid4(), uuid4(), uuid4()
    proposal = EditorialProposal(
        id=proposal_id,
        subject_id=subject_id,
        production_run_id=run_id,
        origin=EditorialProposalOrigin.MANUAL_PATCH,
        snapshot_id=snapshot.snapshot_id,
        occurrence_id=occurrence_id,
        base_artifact_id=base_artifact_id,
        base_artifact_version=1,
        base_input_hash=base_artifact.input_hash,
        base_canonical_sha256=base_sha256,
        base_routing_hash=None,
        base_pin_hash=base_pin,
        target_id="synthetic-title",
        operation=operation,
        before_hash="3" * 64,
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
        idempotency_key=f"proposal-{uuid4().hex}",
        request_sha256="4" * 64,
    )
    state = _MemoryState(
        proposals={proposal.id: proposal},
        blobs={patch_blob.id: patch_blob},
        artifacts={base_artifact.id: base_artifact},
        snapshots={snapshot.snapshot_id: snapshot},
        run=ProductionRun(subject_id=subject_id, edition_id=uuid4(), id=run_id),
    )
    if with_sibling:
        sibling = replace(proposal, id=uuid4(), idempotency_key=f"proposal-{uuid4().hex}")
        state.proposals[sibling.id] = sibling

    candidate_payload = {"title": "Synthetic accepted title"}
    candidate_bytes = ProductionArtifactStore.canonical_json_bytes(candidate_payload)
    candidate_sha256 = hashlib.sha256(candidate_bytes).hexdigest()
    candidate = SynthesisCandidate(
        base_artifact=base_artifact,
        base_canonical_sha256=base_sha256,
        synthesis=cast(Any, None),
        payload=candidate_payload,
        canonical_bytes=candidate_bytes,
        canonical_sha256=candidate_sha256,
        before_hash=proposal.before_hash,
    )
    persistence = SynthesisCandidatePersistence(
        run_id=run_id,
        subject_id=subject_id,
        input_hash=base_artifact.input_hash,
        extraction=cast(Any, None),
        snapshot=cast(Any, None),
        raw_result=None,
        model_run_id=None,
        artifact_id=uuid4(),
        expected_current_artifact_id=base_artifact.id,
        expected_current_input_hash=base_artifact.input_hash,
        expected_current_canonical_sha256=base_sha256,
    )
    plan = _SynthesisCandidatePlan(
        candidate=candidate,
        persistence=persistence,
        registry_lineage=(),
        retired_block_ids=(),
    )
    factory = _MemoryUowFactory(state)
    blob_store = _MemoryBlobStore(patch_descriptor, patch_bytes)
    inputs = _Inputs()
    service = service_type(
        plan=plan,
        uow_factory=factory,
        blob_store=blob_store,
        artifact_store=_MemoryArtifactStore(base_bytes),
        workspace_service=_MemoryWorkspace(resolution),
        inputs=cast(EditorialProposalAdoptionInputs, inputs),
        synthesis_writer=cast(Any, None),
        enrichment_writer=cast(Any, None),
    )
    return _Fixture(
        service=service,
        factory=factory,
        blob_store=blob_store,
        inputs=inputs,
        proposal=proposal,
        actor_id=actor_id,
        occurrence_id=occurrence_id,
        patch_sha256=patch_sha256,
        idempotency_key=f"accept-{uuid4().hex}",
    )


@pytest.mark.asyncio
async def test_accept_rehashes_patch_revalidates_fences_and_never_calls_gateway() -> None:
    fixture = _fixture()
    patch_key = (_PATCH_BUCKET, fixture.patch_sha256)
    fixture.blob_store.read_overrides[patch_key] = b"tampered synthetic patch"

    with pytest.raises(EditorialProposalAdoptionError, match="proposal_patch_hash_mismatch"):
        await fixture.accept()

    assert fixture.blob_store.put_calls == 0
    fixture.blob_store.read_overrides.clear()
    result = await fixture.accept()

    assert result.proposal.state is EditorialProposalState.ACCEPTED
    assert fixture.inputs.revalidated == 1
    assert fixture.inputs.gateway_calls == 0


@pytest.mark.asyncio
async def test_same_idempotency_key_with_different_body_conflicts() -> None:
    fixture = _fixture()
    accepted = await fixture.accept()
    different_patch_hash = "0" * 64
    assert different_patch_hash != fixture.patch_sha256

    with pytest.raises(EditorialProposalAdoptionError, match="proposal_idempotency_conflict"):
        await fixture.service.accept(
            fixture.proposal.subject_id,
            fixture.proposal.occurrence_id,
            fixture.proposal.id,
            actor_id=fixture.actor_id,
            expected_state_version=1,
            patch_sha256=different_patch_hash,
            idempotency_key=fixture.idempotency_key,
        )

    assert accepted.proposal.state is EditorialProposalState.ACCEPTED
    assert fixture.inputs.gateway_calls == 0
    assert fixture.blob_store.put_calls == 1


@pytest.mark.asyncio
async def test_accept_cas_appends_artifact_lineage_registry_and_outbox_atomically() -> None:
    fixture = _fixture()

    result = await fixture.accept()

    stored = fixture.factory.state
    assert result.artifact.id == result.proposal.accepted_artifact_id
    assert stored.artifacts[result.artifact.id].canonical_blob_id is not None
    assert (
        result.snapshot.parent_snapshot_id
        == fixture.factory.state.snapshots[fixture.proposal.snapshot_id].snapshot_id
    )
    assert result.snapshot.synthesis_version == 2
    assert result.proposal.decision_id is not None
    assert len(stored.events) == 1
    assert stored.events[0].kind is EditorialProposalEventKind.STATE_CHANGED
    assert stored.impacts[fixture.proposal.id].sequence == 1
    assert stored.impacts[fixture.proposal.id].accepted_snapshot_id == result.snapshot.snapshot_id
    assert fixture.factory.commits == 1


@pytest.mark.asyncio
async def test_accepting_variant_stales_siblings_atomically() -> None:
    fixture = _fixture(with_sibling=True)
    sibling_id = next(
        item for item in fixture.factory.state.proposals if item != fixture.proposal.id
    )

    result = await fixture.accept()

    assert result.proposal.state is EditorialProposalState.ACCEPTED
    assert fixture.factory.state.proposals[sibling_id].state is EditorialProposalState.STALE
    assert fixture.factory.state.proposals[sibling_id].state_version == 2
    assert any(
        event.proposal_id == sibling_id and event.state is EditorialProposalState.STALE
        for event in fixture.factory.state.events
    )


@pytest.mark.asyncio
async def test_adoption_crash_before_upload() -> None:
    fixture = _fixture(service_type=_FailBeforeUpload)

    with pytest.raises(RuntimeError, match="synthetic crash before upload"):
        await fixture.accept()

    assert fixture.blob_store.put_calls == 0
    assert fixture.factory.commits == 0
    assert (
        fixture.factory.state.proposals[fixture.proposal.id].state
        is EditorialProposalState.PROPOSED
    )


@pytest.mark.asyncio
async def test_adoption_crash_after_upload_before_uow() -> None:
    fixture = _fixture(service_type=_FailBeforeUowOnce)

    with pytest.raises(RuntimeError, match="after upload before UOW"):
        await fixture.accept()
    assert (
        fixture.factory.state.proposals[fixture.proposal.id].state
        is EditorialProposalState.PROPOSED
    )
    assert fixture.factory.state.impacts == {}
    assert fixture.blob_store.new_objects == 1
    assert fixture.blob_store.delete_calls == 1

    result = await fixture.accept()

    assert result.proposal.state is EditorialProposalState.ACCEPTED
    assert fixture.blob_store.new_objects == 2
    assert fixture.factory.state.impacts[fixture.proposal.id].state.value == "pending"


@pytest.mark.asyncio
async def test_collected_candidate_is_reuploaded_before_acceptance_uow() -> None:
    fixture = _fixture(service_type=_CollectedBeforeUowOnce)

    result = await fixture.accept()

    assert result.proposal.state is EditorialProposalState.ACCEPTED
    assert fixture.blob_store.put_calls == 2
    assert fixture.blob_store.delete_calls == 1
    assert fixture.factory.state.impacts[fixture.proposal.id].state.value == "pending"


@pytest.mark.asyncio
async def test_adoption_crash_after_commit_before_impact() -> None:
    fixture = _fixture(service_type=_CrashAfterCommitOnce)

    with pytest.raises(RuntimeError, match="after commit before impact worker"):
        await fixture.accept()
    committed = fixture.factory.state
    accepted = committed.proposals[fixture.proposal.id]
    impact = committed.impacts[fixture.proposal.id]
    assert accepted.state is EditorialProposalState.ACCEPTED
    assert impact.state.value == "pending"

    replay = await fixture.accept()

    assert replay.replayed
    assert replay.proposal.decision_id == accepted.decision_id
    assert replay.artifact.id == accepted.accepted_artifact_id
    assert fixture.inputs.revalidated == 1
    assert fixture.blob_store.put_calls == 1


@pytest.mark.asyncio
async def test_cleanup_orphan_blob_is_idempotent_and_preserves_referenced_blobs() -> None:
    fixture = _fixture()
    orphan_bytes = b"synthetic orphan"
    orphan_sha256 = hashlib.sha256(orphan_bytes).hexdigest()
    orphan_descriptor = BlobDescriptor(
        orphan_sha256,
        len(orphan_bytes),
        "application/json",
        _CANONICAL_BUCKET,
    )
    fixture.blob_store.objects[(_CANONICAL_BUCKET, orphan_sha256)] = orphan_bytes

    assert await fixture.service.cleanup_orphan_blob(orphan_descriptor)
    assert not await fixture.service.cleanup_orphan_blob(orphan_descriptor)
    assert (_CANONICAL_BUCKET, orphan_sha256) not in fixture.blob_store.objects
    assert fixture.blob_store.delete_calls == 1

    accepted = await fixture.accept()
    candidate_blob = fixture.factory.state.blobs[accepted.artifact.canonical_blob_id]
    assert not await fixture.service.cleanup_orphan_blob(candidate_blob.descriptor)
    assert (
        candidate_blob.descriptor.logical_bucket,
        candidate_blob.descriptor.sha256,
    ) in fixture.blob_store.objects
