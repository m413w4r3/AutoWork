from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import date
from io import BytesIO
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI

from cti_app.api import editorial_workspace as editorial_workspace_api
from cti_app.application.editorial_proposal_adoption import (
    EditorialProposalAdoptionError,
    EditorialProposalAdoptionInputs,
    EditorialProposalAdoptionService,
    SynthesisCandidateInputs,
    _CandidatePlan,
    _SynthesisCandidatePlan,
)
from cti_app.application.editorial_synthesis import targets
from cti_app.application.editorial_synthesis.candidate import (
    SynthesisCandidatePersistence,
)
from cti_app.application.editorial_workspace.registry import (
    WorkspaceOccurrenceResolution,
    WorkspaceSnapshotRecord,
    registry_hash,
)
from cti_app.application.persistence import ProductionUnitOfWorkFactory, UnitOfWork
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.application.production_stages import SynthesisService
from cti_app.application.production_synthesis import (
    build_synthesis_evidence_pack,
    canonical_extraction_hash,
)
from cti_app.application.production_synthesis_revision import _apply_wire_patch
from cti_app.config import Settings
from cti_app.domain.blobs import BlobDescriptor, BlobRecord
from cti_app.domain.classification import TLP
from cti_app.domain.discovery import SourceRole
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
from cti_app.domain.production import (
    EXTRACTION_PROFILE_POLICY_VERSION,
    ExtractionProfile,
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionEvidenceBasis,
    ProductionInputSnapshot,
    ProductionRun,
    SynthesisMode,
)
from cti_app.domain.production_editorial_enrichment import (
    EditorialEnrichmentElementKind,
    editorial_enrichment_from_json,
)
from cti_app.domain.production_editorial_revision import (
    TargetedEditorialRevisionOperation,
    TargetedEditorialRevisionTarget,
    TargetedEditorialRevisionTargetKind,
)
from cti_app.domain.production_extraction import (
    ExtractionFactV1,
    ExtractionReuseState,
    ProductionExtractionV1,
    ProductionSourceExtractionV1,
)
from cti_app.domain.production_references import ProductionReferenceKind, ProductionReferenceTier
from cti_app.domain.production_synthesis import (
    PRODUCTION_SYNTHESIS_SCHEMA_VERSION,
    SYNTHESIS_POLICY_VERSION,
    ProductionSynthesisV1,
    SynthesisParagraphV1,
    SynthesisSectionKind,
    SynthesisSectionV1,
    extraction_evidence_refs_v1,
    production_synthesis_to_json,
)
from cti_app.domain.publication_kind import PublicationKind
from cti_app.infrastructure.database.repositories.core import SqlAlchemyBlobRepository

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
        self.open_uows = 0
        self.fail_commit_once = False

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
        self.factory.open_uows += 1
        return self

    async def __aexit__(self, *_args: object) -> None:
        self.factory.open_uows -= 1
        return None

    async def commit(self) -> None:
        if self.factory.fail_commit_once:
            self.factory.fail_commit_once = False
            raise RuntimeError("synthetic database commit failure")
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

    async def list_for_run(self, run_id: UUID) -> tuple[ProductionArtifact, ...]:
        return tuple(
            artifact
            for artifact in self.uow.state.artifacts.values()
            if artifact.production_run_id == run_id
        )

    async def get_current(self, run_id: UUID, stage: str) -> ProductionArtifact | None:
        candidates = [
            artifact
            for artifact in self.uow.state.artifacts.values()
            if artifact.production_run_id == run_id
            and artifact.stage.value == stage
            and artifact.status.value == "verified"
        ]
        return max(candidates, key=lambda artifact: artifact.version, default=None)


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
        self.calls_while_uow_open = 0
        self.forbid_calls_while_uow_open = False
        self._uow_open: Callable[[], bool] = lambda: False
        self._commit_count: Callable[[], int] = lambda: 0
        self.commit_counts_at_delete: list[int] = []

    def set_uow_probe(self, probe: Callable[[], bool], commit_count: Callable[[], int]) -> None:
        self._uow_open = probe
        self._commit_count = commit_count

    def _check_uow(self) -> None:
        if self._uow_open():
            self.calls_while_uow_open += 1
            if self.forbid_calls_while_uow_open:
                raise AssertionError("BlobStore called while relational UOW is open")

    async def put(self, source: BytesIO, *, logical_bucket: str, mime_type: str) -> BlobDescriptor:
        self._check_uow()
        self.put_calls += 1
        payload = source.read()
        digest = hashlib.sha256(payload).hexdigest()
        key = (logical_bucket, digest)
        if key not in self.objects:
            self.objects[key] = payload
            self.new_objects += 1
        return BlobDescriptor(digest, len(payload), mime_type, logical_bucket)

    async def read(self, descriptor: BlobDescriptor, *, max_bytes: int) -> bytes:
        self._check_uow()
        key = (descriptor.logical_bucket, descriptor.sha256)
        value = self.read_overrides.get(key, self.objects[key])
        if len(value) > max_bytes:
            raise ValueError("synthetic_read_limit")
        return value

    async def exists(self, descriptor: BlobDescriptor) -> bool:
        self._check_uow()
        return (descriptor.logical_bucket, descriptor.sha256) in self.objects

    async def delete(self, descriptor: BlobDescriptor) -> None:
        self._check_uow()
        self.delete_calls += 1
        self.commit_counts_at_delete.append(self._commit_count())
        self.objects.pop((descriptor.logical_bucket, descriptor.sha256), None)


class _MemoryArtifactStore:
    def __init__(self, canonical_bytes: bytes) -> None:
        self.canonical_bytes = canonical_bytes
        self._uow_open: Callable[[], bool] = lambda: False
        self.calls_while_uow_open = 0
        self.forbid_calls_while_uow_open = False

    def set_uow_probe(self, probe: Callable[[], bool]) -> None:
        self._uow_open = probe

    async def read_bytes(self, _blob_id: UUID, *, max_bytes: int | None = None) -> bytes:
        if self._uow_open():
            self.calls_while_uow_open += 1
            if self.forbid_calls_while_uow_open:
                raise AssertionError("ArtifactStore called while relational UOW is open")
        if max_bytes is not None and len(self.canonical_bytes) > max_bytes:
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
    def __init__(self, synthesis: SynthesisCandidateInputs) -> None:
        self.synthesis = synthesis
        self.revalidated = 0
        self.gateway_calls = 0
        self.source_tlp = TLP.AMBER
        self.source_revoked = False
        self.external_llm_allowed = True
        self.thread_binding_valid = True

    async def synthesis_inputs(self, *_args: Any, **_kwargs: Any) -> SynthesisCandidateInputs:
        return self.synthesis

    async def enrichment_inputs(self, *_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("test candidate plan bypasses generation and gateway inputs")

    async def revalidate_evidence_policy_and_thread(self, *_args: Any, **_kwargs: Any) -> None:
        self.revalidated += 1
        if self.source_tlp is TLP.RED:
            raise EditorialProposalAdoptionError("external_llm_tlp_blocked")
        if self.source_revoked:
            raise EditorialProposalAdoptionError("proposal_evidence_revoked")
        if not self.external_llm_allowed:
            raise EditorialProposalAdoptionError("external_llm_blocked")
        if not self.thread_binding_valid:
            raise EditorialProposalAdoptionError("thread_binding_stale")


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
    async def _build_candidate(self, *_args: Any, **_kwargs: Any) -> _SynthesisCandidatePlan:
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
    service: EditorialProposalAdoptionService
    factory: _MemoryUowFactory
    blob_store: _MemoryBlobStore
    inputs: _Inputs
    proposal: EditorialProposal
    actor_id: UUID
    occurrence_id: UUID
    patch_sha256: str
    idempotency_key: str

    async def adopt(
        self,
        proposal: EditorialProposal | None = None,
        *,
        idempotency_key: str | None = None,
    ) -> Any:
        target = proposal or self.proposal
        return await self.service.adopt(
            target.subject_id,
            target.occurrence_id,
            target.id,
            actor_id=self.actor_id,
            expected_state_version=1,
            patch_sha256=self.patch_sha256,
            idempotency_key=idempotency_key or self.idempotency_key,
        )


def _fixture(
    *,
    service_type: type[EditorialProposalAdoptionService] = _AdoptionHarness,
    with_sibling: bool = False,
) -> _Fixture:
    subject_id, run_id, occurrence_id = uuid4(), uuid4(), uuid4()
    production_input = ProductionInputSnapshot(
        production_run_id=run_id,
        edition_id=uuid4(),
        subject_id=subject_id,
        subject_version=1,
        subject_title="Synthetic subject",
        subject_tlp=TLP.CLEAR,
        selection_decision_id=uuid4(),
        origin_discovery_subject_id=uuid4(),
        canonical_discovery_subject_id=uuid4(),
        discovery_snapshot_id=uuid4(),
        discovery_snapshot_version=1,
        member_candidate_ids=(),
        discovery_summary="Synthetic summary.",
        actor_or_campaign="Synthetic actor",
        period_start=date(2026, 1, 1),
        period_end=date(2026, 1, 31),
        publication_language="fr",
        research_date=date(2026, 2, 1),
    )
    source_document_id = uuid4()
    extraction = ProductionExtractionV1(
        schema_version=1,
        subject_id=subject_id,
        production_input_hash=production_input.input_hash,
        references_corpus_hash="c" * 64,
        profile_policy_version=EXTRACTION_PROFILE_POLICY_VERSION,
        sources=(
            ProductionSourceExtractionV1(
                source_document_id=source_document_id,
                canonical_url="https://example.test/synthetic-report",
                content_sha256="d" * 64,
                tier=ProductionReferenceTier.CORE,
                kind=ProductionReferenceKind.PUBLICATION,
                role=SourceRole.PRIMARY,
                profile=ExtractionProfile.FULL,
                checkpoint_id=None,
                reuse_state=ExtractionReuseState.FRESH,
                facts=(
                    ExtractionFactV1(
                        category="other_technical",
                        value="Synthetic finding",
                        attack_id=None,
                        context="Synthetic test context.",
                        evidence_quote="The synthetic report records this finding.",
                        evidence_basis=ProductionEvidenceBasis.SOURCE_VERIFIED,
                        source_document_ids=(source_document_id,),
                    ),
                ),
                events=(),
                indicators=(),
                rules=(),
                uncertainties=(),
            ),
        ),
        omitted_sources=(),
        warnings=(),
    )
    evidence_ref = extraction_evidence_refs_v1(extraction)[0]
    synthesis = ProductionSynthesisV1(
        schema_version=PRODUCTION_SYNTHESIS_SCHEMA_VERSION,
        subject_id=subject_id,
        production_input_hash=production_input.input_hash,
        extraction_hash=canonical_extraction_hash(extraction),
        publication_language=production_input.publication_language,
        synthesis_policy_version=SYNTHESIS_POLICY_VERSION,
        title="[Synthetic actor] Synthetic report",
        lead=(SynthesisParagraphV1("A synthetic lead claim.", (evidence_ref,)),),
        sections=(
            SynthesisSectionV1(
                SynthesisSectionKind.OVERVIEW,
                "Synthetic observations",
                (SynthesisParagraphV1("A synthetic section claim.", (evidence_ref,)),),
            ),
        ),
        timeline=(),
        uncertainties=(),
        warnings=(),
    )
    base_payload = production_synthesis_to_json(synthesis)
    base_bytes = ProductionArtifactStore.canonical_json_bytes(base_payload)
    base_sha256 = hashlib.sha256(base_bytes).hexdigest()
    base_artifact_id, base_blob_id = uuid4(), uuid4()
    base_artifact = ProductionArtifact(
        id=base_artifact_id,
        production_run_id=run_id,
        subject_id=subject_id,
        stage=ProductionArtifactStage.SYNTHESIS,
        version=1,
        input_hash=production_input.input_hash,
        canonical_blob_id=base_blob_id,
    )
    operation = TargetedEditorialRevisionOperation.REWRITE_TITLE
    target = TargetedEditorialRevisionTarget(
        kind=TargetedEditorialRevisionTargetKind.TITLE,
        stable_ids=(),
    )
    resolved_target = targets.resolve_target(base_payload, target, operation)
    before_hash = resolved_target.before_hash
    entry = BlockRegistryEntry(
        block_id=occurrence_id,
        kind=EditorialBlockKind.TITLE,
        parent_block_id=None,
        position=0,
        content_sha256=before_hash,
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
        publication_kind=PublicationKind.BRIEF,
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
    patch_payload = {
        "schema_version": "editorial-patch-v1",
        "operation": operation.value,
        "target": target.to_json(),
        "target_id": "synthetic-title",
        "occurrence_id": str(occurrence_id),
        "before_hash": before_hash,
        "after": "[Synthetic actor] Revised report",
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
        operation=operation.value,
        before_hash=before_hash,
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

    evidence_pack = build_synthesis_evidence_pack(production_input, extraction)
    apply_patch = _apply_wire_patch
    from cti_app.application.editorial_synthesis.patch_validation import WirePatch

    wire_patch = WirePatch(
        target=resolved_target.path,
        action=operation,
        base_artifact_hash=base_sha256,
        before_hash=before_hash,
        after=patch_payload["after"],
        rationale=patch_payload["rationale"],
    )
    from cti_app.application.editorial_synthesis.candidate import (
        build_candidate_from_validated_patch,
    )

    candidate = build_candidate_from_validated_patch(
        base_artifact=base_artifact,
        base_payload=base_payload,
        base_synthesis=synthesis,
        base_canonical_sha256=base_sha256,
        patch=wire_patch,
        target=target,
        operation=operation,
        evidence_pack=evidence_pack,
        extraction=extraction,
        snapshot=production_input,
        apply_patch=apply_patch,
        publication_kind=PublicationKind.BRIEF,
    )
    persistence = SynthesisCandidatePersistence(
        run_id=run_id,
        subject_id=subject_id,
        input_hash=base_artifact.input_hash,
        extraction=extraction,
        snapshot=production_input,
        raw_result=None,
        model_run_id=None,
        publication_kind=PublicationKind.BRIEF,
        mode=SynthesisMode.FRESH,
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
    synthesis_inputs = SynthesisCandidateInputs(
        base_payload=base_payload,
        base_synthesis=synthesis,
        target=target,
        evidence_pack=evidence_pack,
        extraction=extraction,
        snapshot=production_input,
        apply_patch=apply_patch,
        persistence=persistence,
        registry_lineage=(),
        publication_kind=PublicationKind.BRIEF,
    )
    factory = _MemoryUowFactory(state)
    blob_store = _MemoryBlobStore(patch_descriptor, patch_bytes)
    blob_store.set_uow_probe(lambda: factory.open_uows > 0, lambda: factory.commits)
    inputs = _Inputs(synthesis_inputs)
    artifact_store = _MemoryArtifactStore(base_bytes)
    artifact_store.set_uow_probe(lambda: factory.open_uows > 0)
    synthesis_writer = SynthesisService(
        cast(ProductionUnitOfWorkFactory, factory),
        cast(ProductionArtifactStore, artifact_store),
    )
    common = {
        "uow_factory": factory,
        "blob_store": blob_store,
        "artifact_store": cast(ProductionArtifactStore, artifact_store),
        "workspace_service": _MemoryWorkspace(resolution),
        "inputs": cast(EditorialProposalAdoptionInputs, inputs),
        "synthesis_writer": synthesis_writer,
        "enrichment_writer": cast(Any, None),
    }
    if service_type is EditorialProposalAdoptionService:
        service = service_type(**common)
    else:
        service = cast(Any, service_type)(plan=plan, **common)
    return _Fixture(
        service=service,
        factory=factory,
        blob_store=blob_store,
        inputs=inputs,
        proposal=proposal,
        actor_id=actor_id,
        occurrence_id=occurrence_id,
        patch_sha256=patch_sha256,
        idempotency_key=f"adopt-{uuid4().hex}",
    )


@pytest.mark.asyncio
async def test_adopt_rehashes_patch_revalidates_fences_and_never_calls_gateway() -> None:
    fixture = _fixture()
    patch_key = (_PATCH_BUCKET, fixture.patch_sha256)
    fixture.blob_store.read_overrides[patch_key] = b"tampered synthetic patch"

    with pytest.raises(EditorialProposalAdoptionError, match="proposal_patch_hash_mismatch"):
        await fixture.adopt()

    assert fixture.blob_store.put_calls == 0
    fixture.blob_store.read_overrides.clear()
    result = await fixture.adopt()

    assert result.proposal.state is EditorialProposalState.ACCEPTED
    assert fixture.inputs.revalidated == 1
    assert fixture.inputs.gateway_calls == 0


@pytest.mark.asyncio
async def test_same_idempotency_key_with_different_body_conflicts() -> None:
    fixture = _fixture()
    accepted = await fixture.adopt()
    different_patch_hash = "0" * 64
    assert different_patch_hash != fixture.patch_sha256

    with pytest.raises(EditorialProposalAdoptionError, match="proposal_idempotency_conflict"):
        await fixture.service.adopt(
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
async def test_adopt_cas_appends_artifact_lineage_registry_and_outbox_atomically() -> None:
    fixture = _fixture(service_type=EditorialProposalAdoptionService)

    result = await fixture.adopt()

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
@pytest.mark.parametrize("placement_changed", [False, True])
async def test_simple_enrichment_table_adoption_uses_parsed_base_and_production_candidate_core(
    monkeypatch: pytest.MonkeyPatch,
    placement_changed: bool,
) -> None:
    from cti_app.application.editorial_proposal_adoption_inputs import (
        ProductionEditorialProposalAdoptionInputs,
        _target_id,
    )
    from cti_app.application.editorial_workspace.registry import resolve_occurrence
    from cti_app.application.production_editorial_enrichment import (
        EditorialEnrichmentV1,
        EnrichmentPlacementKind,
        EnrichmentPlacementProposalV1,
        build_editorial_figure_catalog,
        parse_editorial_enrichment_proposal_wire,
    )
    from cti_app.application.production_stages import EditorialEnrichmentService
    from cti_app.application.production_synthesis import SynthesisAccessPolicyV1
    from cti_app.domain.editorial_workspace import (
        EditorialSourceStage,
    )
    from cti_app.domain.production import ProductionArtifactStatus
    from cti_app.domain.production_synthesis import production_synthesis_to_json
    from tests.test_production_editorial_enrichment_application import (
        _RecordingGateway,
        _revision_harness,
        _table_revision_wire,
    )
    from tests.test_production_enrichment_candidate_core import _validated_table_patch

    harness = await _revision_harness(
        monkeypatch,
        _RecordingGateway(AssertionError("adoption must not call the model")),
    )
    validated_patch = await _validated_table_patch(harness)
    base_enrichment = editorial_enrichment_from_json(harness.base_payload)
    assert isinstance(base_enrichment, EditorialEnrichmentV1)
    figure_catalog = build_editorial_figure_catalog(
        harness.world.extraction, harness.inventory_state.value
    )
    assert harness.evidence_pack.registered_anchor_ids is not None
    anchor_block_id = min(harness.evidence_pack.registered_anchor_ids, key=str)
    parsed_patch = parse_editorial_enrichment_proposal_wire(
        _table_revision_wire(anchor_block_id=anchor_block_id),
        harness.evidence_pack,
        figure_catalog=figure_catalog,
    )
    assert parsed_patch.proposal is not None
    base_bytes = await harness.world.store.read_bytes(harness.base.canonical_blob_id)
    run = harness.world.run
    subject_id = harness.world.snapshot.subject_id
    occurrence_id = uuid4()
    actor_id = uuid4()

    target = TargetedEditorialRevisionTarget(
        kind=TargetedEditorialRevisionTargetKind.ENRICHMENT_ELEMENT,
        element_kind=EditorialEnrichmentElementKind.TABLE,
        element_key="tools_table",
    )
    target_id = _target_id(target)
    before_hash = hashlib.sha256(
        ProductionArtifactStore.canonical_json_bytes({"value": harness.base_payload["tables"][0]})
    ).hexdigest()
    entry = BlockRegistryEntry(
        block_id=occurrence_id,
        kind=EditorialBlockKind.TABLE,
        parent_block_id=None,
        position=0,
        content_sha256=before_hash,
        source_stage=EditorialSourceStage.EDITORIAL_ENRICHMENT,
        structural_pointer="/tables/0",
    )
    snapshot = WorkspaceSnapshotRecord(
        snapshot_id=uuid4(),
        subject_id=subject_id,
        production_run_id=run.id,
        synthesis_artifact_id=harness.world.synthesis_artifact.id,
        synthesis_version=harness.world.synthesis_artifact.version,
        synthesis_input_hash=harness.world.synthesis_artifact.input_hash,
        synthesis_canonical_sha256=hashlib.sha256(
            ProductionArtifactStore.canonical_json_bytes(
                production_synthesis_to_json(harness.world.synthesis)
            )
        ).hexdigest(),
        enrichment_artifact_id=harness.base.id,
        enrichment_version=harness.base.version,
        enrichment_input_hash=harness.base.input_hash,
        enrichment_canonical_sha256=harness.base_sha256,
        publication_artifact_id=None,
        publication_version=None,
        publication_input_hash=None,
        publication_canonical_sha256=None,
        pipeline_generation=0,
        routing_decision_id=None,
        routing_decision_hash=None,
        publication_kind=PublicationKind.BRIEF,
        workspace_pin_hash="7" * 64,
        registry_hash=registry_hash((entry,)),
        block_registry=(entry,),
    )
    resolution = resolve_occurrence(snapshot, occurrence_id)
    workspace = _MemoryWorkspace(resolution)
    enrichment_patch = parsed_patch.proposal
    if placement_changed:
        changed_table = enrichment_patch.tables[0].model_copy(
            update={
                "placement": EnrichmentPlacementProposalV1(
                    kind=EnrichmentPlacementKind.AFTER_SECTION,
                    section_index=0,
                )
            }
        )
        enrichment_patch = enrichment_patch.model_copy(update={"tables": (changed_table,)})
    patch_payload = {
        "schema_version": "editorial-patch-v1",
        "operation": TargetedEditorialRevisionOperation.REWRITE_TABLE.value,
        "target": target.to_json(),
        "target_id": target_id,
        "occurrence_id": str(occurrence_id),
        "before_hash": before_hash,
        "enrichment_patch": enrichment_patch.model_dump(mode="json"),
        "rationale": "Synthetic table replacement.",
    }
    patch_bytes = ProductionArtifactStore.canonical_json_bytes(patch_payload)
    patch_sha256 = hashlib.sha256(patch_bytes).hexdigest()
    patch_descriptor = BlobDescriptor(patch_sha256, len(patch_bytes), _PATCH_MIME, _PATCH_BUCKET)
    patch_blob = BlobRecord(descriptor=patch_descriptor)
    proposal_id = uuid4()
    proposal = EditorialProposal(
        id=proposal_id,
        subject_id=subject_id,
        production_run_id=run.id,
        origin=EditorialProposalOrigin.MANUAL_PATCH,
        snapshot_id=snapshot.snapshot_id,
        occurrence_id=occurrence_id,
        base_artifact_id=harness.base.id,
        base_artifact_version=harness.base.version,
        base_input_hash=harness.base.input_hash,
        base_canonical_sha256=harness.base_sha256,
        base_routing_hash=None,
        base_pin_hash=snapshot.workspace_pin_hash,
        target_id=target_id,
        operation=TargetedEditorialRevisionOperation.REWRITE_TABLE.value,
        before_hash=before_hash,
        patch_blob_ref=patch_blob.id,
        patch_sha256=patch_sha256,
        evidence_refs=(),
        diagnostics={},
        thread_id=None,
        variant_group_id=uuid4(),
        model_run_id=None,
        model_turn_id=None,
        state=EditorialProposalState.PROPOSED,
        state_version=1,
        actor_id=actor_id,
        idempotency_key=f"proposal-{uuid4().hex}",
        request_sha256="4" * 64,
    )

    fixture = _fixture(service_type=EditorialProposalAdoptionService)
    fixture.factory.state.proposals = {proposal.id: proposal}
    fixture.factory.state.blobs = {patch_blob.id: patch_blob}
    fixture.factory.state.artifacts = {
        artifact.id: artifact
        for artifact in (
            harness.world.extraction_artifact,
            harness.world.synthesis_artifact,
            harness.base,
        )
    }
    fixture.factory.state.snapshots = {snapshot.snapshot_id: snapshot}
    fixture.factory.state.run = run
    fixture.actor_id = actor_id
    fixture.occurrence_id = occurrence_id
    fixture.patch_sha256 = patch_sha256
    fixture.blob_store.objects[(patch_descriptor.logical_bucket, patch_sha256)] = patch_bytes
    artifact_store = fixture.service._artifact_store
    artifact_store.canonical_bytes = base_bytes

    evidence_pack_hash = str(harness.base.metadata["evidence_pack_hash"])
    access_policy = SynthesisAccessPolicyV1(
        subject_tlp=TLP.CLEAR,
        effective_tlp=TLP.CLEAR,
        external_llm_allowed=True,
        do_not_submit=False,
        sources=(),
    )
    fence = SimpleNamespace(
        evidence_pack=harness.evidence_pack,
        evidence_pack_hash=evidence_pack_hash,
        inventory_hash=str(harness.base.metadata["source_figure_inventory_hash"]),
        figure_catalog=figure_catalog,
        input_hash=harness.input_hash,
    )
    enrichment_loader = SimpleNamespace()

    async def load_extraction(*_args: Any, **_kwargs: Any) -> Any:
        return harness.world.extraction

    async def load_synthesis(*_args: Any, **_kwargs: Any) -> Any:
        return harness.world.synthesis

    enrichment_loader._load_extraction = load_extraction
    enrichment_loader._load_synthesis = load_synthesis
    revision_service = SimpleNamespace()

    async def validate_input_fence(**_kwargs: Any) -> Any:
        return fence

    revision_service._validate_input_fence = validate_input_fence
    inputs = ProductionEditorialProposalAdoptionInputs(
        uow_factory=fixture.factory,
        artifact_store=artifact_store,
        workspace_service=workspace,
        synthesis_loader=SimpleNamespace(),
        enrichment_loader=enrichment_loader,
        enrichment_revision_service=revision_service,
    )

    async def load_base_context(_proposal: EditorialProposal, **_kwargs: Any) -> tuple[Any, ...]:
        return (
            run,
            harness.world.snapshot,
            harness.world.extraction_artifact,
            None,
            harness.world.synthesis_artifact,
        )

    async def load_access_policy(*_args: Any, **_kwargs: Any) -> tuple[Any, int]:
        return access_policy, harness.world.snapshot.subject_version

    async def revalidate_fences(*_args: Any, **_kwargs: Any) -> None:
        return None

    inputs._load_base_context = load_base_context
    inputs._access_policy = load_access_policy
    inputs.revalidate_evidence_policy_and_thread = revalidate_fences

    class _PersistedEnrichmentAdoption(EditorialProposalAdoptionService):
        async def _persist_candidate(
            self,
            uow: UnitOfWork,
            stage: ProductionArtifactStage,
            plan: _CandidatePlan,
            blob: BlobRecord,
            *,
            proposal: EditorialProposal,
            patch_sha256: str,
        ) -> ProductionArtifact:
            assert stage is ProductionArtifactStage.EDITORIAL_ENRICHMENT
            candidate = plan.candidate
            artifact = ProductionArtifact(
                id=candidate.artifact_id,
                production_run_id=proposal.production_run_id,
                subject_id=proposal.subject_id,
                stage=stage,
                version=2,
                input_hash=candidate.input_hash,
                canonical_blob_id=blob.id,
                status=ProductionArtifactStatus.VERIFIED,
                metadata={"patch_sha256": patch_sha256},
            )
            await uow.production_artifacts.append(artifact)
            return artifact

    fixture.service = _PersistedEnrichmentAdoption(
        uow_factory=fixture.factory,
        blob_store=cast(Any, fixture.blob_store),
        artifact_store=artifact_store,
        workspace_service=workspace,
        inputs=cast(EditorialProposalAdoptionInputs, inputs),
        synthesis_writer=cast(Any, fixture.service._synthesis_writer),
        enrichment_writer=cast(EditorialEnrichmentService, object()),
    )

    artifact_ids_before = set(fixture.factory.state.artifacts)
    snapshot_ids_before = set(fixture.factory.state.snapshots)
    if placement_changed:
        with pytest.raises(EditorialProposalAdoptionError) as rejected:
            await fixture.adopt(proposal)
        assert rejected.value.code == "adoption_placement_changed"
        assert fixture.factory.commits == 0
        assert set(fixture.factory.state.artifacts) == artifact_ids_before
        assert set(fixture.factory.state.snapshots) == snapshot_ids_before
        assert fixture.blob_store.new_objects == 0
        assert fixture.factory.state.proposals[proposal.id].state is EditorialProposalState.PROPOSED
        return

    result = await fixture.adopt(proposal)

    assert result.proposal.state is EditorialProposalState.ACCEPTED
    assert result.artifact.stage is ProductionArtifactStage.EDITORIAL_ENRICHMENT
    assert result.artifact.version == 2
    accepted_blob = fixture.factory.state.blobs[result.artifact.canonical_blob_id]
    adopted_payload = json.loads(
        fixture.blob_store.objects[
            (accepted_blob.descriptor.logical_bucket, accepted_blob.descriptor.sha256)
        ]
    )
    assert adopted_payload["tables"][0]["title"] == validated_patch.tables[0].title
    assert (
        adopted_payload["tables"][0]["placement"] == harness.base_payload["tables"][0]["placement"]
    )
    assert result.snapshot.enrichment_version == 2


@pytest.mark.asyncio
async def test_adopting_variant_stales_siblings_atomically() -> None:
    fixture = _fixture(with_sibling=True)
    sibling_id = next(
        item for item in fixture.factory.state.proposals if item != fixture.proposal.id
    )

    result = await fixture.adopt()

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
        await fixture.adopt()

    assert fixture.blob_store.put_calls == 0
    assert (
        fixture.factory.state.proposals[fixture.proposal.id].state
        is EditorialProposalState.PROPOSED
    )


@pytest.mark.asyncio
async def test_adoption_crash_after_upload_before_uow() -> None:
    fixture = _fixture(service_type=_FailBeforeUowOnce)

    with pytest.raises(RuntimeError, match="after upload before UOW"):
        await fixture.adopt()
    assert (
        fixture.factory.state.proposals[fixture.proposal.id].state
        is EditorialProposalState.PROPOSED
    )
    assert fixture.factory.state.impacts == {}
    assert fixture.blob_store.new_objects == 1
    assert fixture.blob_store.delete_calls == 0

    result = await fixture.adopt()

    assert result.proposal.state is EditorialProposalState.ACCEPTED
    assert fixture.blob_store.new_objects == 1
    assert fixture.factory.state.impacts[fixture.proposal.id].state.value == "pending"


@pytest.mark.asyncio
async def test_blob_store_is_not_called_while_adoption_uow_is_open() -> None:
    fixture = _fixture(service_type=EditorialProposalAdoptionService)
    fixture.blob_store.forbid_calls_while_uow_open = True
    artifact_store = cast(_MemoryArtifactStore, fixture.service._artifact_store)
    artifact_store.forbid_calls_while_uow_open = True

    result = await fixture.adopt()

    assert result.proposal.state is EditorialProposalState.ACCEPTED
    assert fixture.blob_store.calls_while_uow_open == 0
    assert artifact_store.calls_while_uow_open == 0


@pytest.mark.asyncio
async def test_losing_same_blob_adoption_preserves_the_winning_artifact_object() -> None:
    fixture = _fixture()
    loser_key = f"adopt-loser-{uuid4().hex}"
    winner_key = f"adopt-winner-{uuid4().hex}"
    persist = fixture.service._persist_acceptance

    async def interleave_winner(**kwargs: Any) -> Any:
        if kwargs["idempotency_key"] == loser_key:
            result = await fixture.adopt(idempotency_key=winner_key)
            assert result.proposal.state is EditorialProposalState.ACCEPTED
        return await persist(**kwargs)

    fixture.service._persist_acceptance = interleave_winner  # type: ignore[method-assign]
    with pytest.raises(EditorialProposalAdoptionError, match="proposal_state_conflict"):
        await fixture.adopt(idempotency_key=loser_key)

    accepted = fixture.factory.state.proposals[fixture.proposal.id]
    assert accepted.state is EditorialProposalState.ACCEPTED
    blob_id = accepted.accepted_artifact_id
    assert blob_id is not None
    artifact = fixture.factory.state.artifacts[blob_id]
    candidate_blob = fixture.factory.state.blobs[artifact.canonical_blob_id]
    address = (candidate_blob.descriptor.logical_bucket, candidate_blob.descriptor.sha256)
    assert address in fixture.blob_store.objects
    assert fixture.blob_store.delete_calls == 0


@pytest.mark.asyncio
async def test_adoption_revalidates_synthetic_policy_fences() -> None:
    fixture = _fixture()
    fixture.inputs.external_llm_allowed = False

    with pytest.raises(EditorialProposalAdoptionError, match="external_llm_blocked"):
        await fixture.adopt()

    assert fixture.factory.state.impacts == {}
    assert (
        fixture.factory.state.proposals[fixture.proposal.id].state
        is EditorialProposalState.PROPOSED
    )


@pytest.mark.asyncio
async def test_adoption_crash_after_commit_before_impact() -> None:
    fixture = _fixture(service_type=_CrashAfterCommitOnce)

    with pytest.raises(RuntimeError, match="after commit before impact worker"):
        await fixture.adopt()
    committed = fixture.factory.state
    accepted = committed.proposals[fixture.proposal.id]
    impact = committed.impacts[fixture.proposal.id]
    assert accepted.state is EditorialProposalState.ACCEPTED
    assert impact.state.value == "pending"

    replay = await fixture.adopt()

    assert replay.replayed
    assert replay.proposal.decision_id == accepted.decision_id
    assert replay.artifact.id == accepted.accepted_artifact_id
    assert fixture.inputs.revalidated == 1
    assert fixture.blob_store.put_calls == 1


@pytest.mark.asyncio
async def test_cleanup_orphan_blob_is_idempotent_and_preserves_referenced_blobs() -> None:
    fixture = _fixture()
    plan = cast(_AdoptionHarness, fixture.service).plan
    orphan_bytes = plan.candidate.canonical_bytes
    orphan_sha256 = hashlib.sha256(orphan_bytes).hexdigest()
    orphan_descriptor = BlobDescriptor(
        orphan_sha256,
        len(orphan_bytes),
        "application/json",
        _CANONICAL_BUCKET,
    )
    address = (_CANONICAL_BUCKET, orphan_sha256)
    fixture.blob_store.objects[address] = orphan_bytes
    async with fixture.factory() as uow:
        orphan_record = await uow.blobs.add_if_absent(BlobRecord(descriptor=orphan_descriptor))
        await uow.commit()

    assert await fixture.service.cleanup_orphan_blob(orphan_descriptor)
    assert not await fixture.service.cleanup_orphan_blob(orphan_descriptor)
    assert address in fixture.blob_store.objects
    assert fixture.blob_store.delete_calls == 0

    accepted = await fixture.adopt()
    candidate_blob = fixture.factory.state.blobs[accepted.artifact.canonical_blob_id]
    assert candidate_blob.id != orphan_record.id
    assert await fixture.service.cleanup_orphan_blob(candidate_blob.descriptor) is False
    assert (
        candidate_blob.descriptor.logical_bucket,
        candidate_blob.descriptor.sha256,
    ) in fixture.blob_store.objects
    assert await fixture.blob_store.read(
        candidate_blob.descriptor, max_bytes=len(orphan_bytes)
    ) == (orphan_bytes)
    assert fixture.blob_store.delete_calls == 0


@pytest.mark.asyncio
async def test_orphan_object_is_retained_when_blob_row_commit_fails() -> None:
    fixture = _fixture()
    orphan_bytes = b"synthetic commit-failure orphan"
    orphan_sha256 = hashlib.sha256(orphan_bytes).hexdigest()
    descriptor = BlobDescriptor(
        orphan_sha256,
        len(orphan_bytes),
        "application/json",
        _CANONICAL_BUCKET,
    )
    address = (descriptor.logical_bucket, descriptor.sha256)
    fixture.blob_store.objects[address] = orphan_bytes
    async with fixture.factory() as uow:
        await uow.blobs.add_if_absent(BlobRecord(descriptor=descriptor))
        await uow.commit()
    commits_before_cleanup = fixture.factory.commits
    fixture.factory.fail_commit_once = True

    with pytest.raises(RuntimeError, match="database commit failure"):
        await fixture.service.cleanup_orphan_blob(descriptor)

    assert address in fixture.blob_store.objects
    assert fixture.blob_store.delete_calls == 0
    assert fixture.factory.commits == commits_before_cleanup


@pytest.mark.asyncio
async def test_blob_reference_query_includes_every_orm_blob_foreign_key() -> None:
    class _CaptureSession:
        statement: Any | None = None

        async def scalar(self, statement: Any) -> int:
            self.statement = statement
            return 2

    session = _CaptureSession()
    references = await SqlAlchemyBlobRepository(cast(Any, session)).count_references(uuid4())

    from sqlalchemy.dialects import postgresql

    sql = str(session.statement.compile(dialect=postgresql.dialect()))
    assert references == 2
    assert "production_artifacts.canonical_blob_id" in sql
    assert "production_artifacts.raw_blob_id" in sql


@pytest.mark.asyncio
async def test_accept_route_adopts_once_and_replays_same_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _fixture(service_type=EditorialProposalAdoptionService)
    monkeypatch.setattr(
        editorial_workspace_api,
        "get_settings",
        lambda: Settings(_env_file=None, editorial_proposal_accept_enabled=True),
    )
    monkeypatch.setattr(
        editorial_workspace_api, "_proposal_adoption_service", lambda _request: fixture.service
    )

    async def current_actor(_request: Any) -> UUID:
        return fixture.actor_id

    monkeypatch.setattr(editorial_workspace_api, "_thread_actor_id", current_actor)
    app = FastAPI()
    app.include_router(editorial_workspace_api.router)
    path = (
        f"/api/subjects/{fixture.proposal.subject_id}/editor/threads/"
        f"{fixture.occurrence_id}/proposals/{fixture.proposal.id}/accept"
    )
    headers = {"Idempotency-Key": f"accept_{uuid4().hex}"}
    payload = {"expected_state_version": 1, "patch_sha256": fixture.patch_sha256}

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://editorial.test"
    ) as client:
        first = await client.post(path, headers=headers, json=payload)
        replay = await client.post(path, headers=headers, json=payload)

    assert first.status_code == replay.status_code == 200
    assert first.json() == replay.json()
    assert first.json()["state"] == EditorialProposalState.ACCEPTED.value
    assert first.json()["artifact_id"] == str(
        fixture.factory.state.proposals[fixture.proposal.id].accepted_artifact_id
    )
    assert fixture.factory.commits == 1
    assert fixture.inputs.revalidated == 1


@pytest.mark.asyncio
async def test_accept_route_maps_candidate_blob_unavailable_to_static_503(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _fixture(service_type=EditorialProposalAdoptionService)

    class _UnavailableService:
        async def adopt(self, *_args: Any, **_kwargs: Any) -> None:
            raise EditorialProposalAdoptionError("proposal_candidate_blob_unavailable")

    monkeypatch.setattr(
        editorial_workspace_api,
        "get_settings",
        lambda: Settings(_env_file=None, editorial_proposal_accept_enabled=True),
    )
    monkeypatch.setattr(
        editorial_workspace_api,
        "_proposal_adoption_service",
        lambda _request: _UnavailableService(),
    )

    async def current_actor(_request: Any) -> UUID:
        return fixture.actor_id

    monkeypatch.setattr(editorial_workspace_api, "_thread_actor_id", current_actor)
    app = FastAPI()
    app.include_router(editorial_workspace_api.router)
    path = (
        f"/api/subjects/{fixture.proposal.subject_id}/editor/threads/"
        f"{fixture.occurrence_id}/proposals/{fixture.proposal.id}/accept"
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://editorial.test"
    ) as client:
        response = await client.post(
            path,
            headers={"Idempotency-Key": f"accept_{uuid4().hex}"},
            json={"expected_state_version": 1, "patch_sha256": fixture.patch_sha256},
        )

    assert response.status_code == 503
    assert response.json()["detail"] == {
        "code": "proposal_candidate_blob_unavailable",
        "message": "L'acceptation des propositions est temporairement indisponible.",
    }


@pytest.mark.asyncio
async def test_accept_route_maps_rewrite_placement_change_to_conflict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _fixture(service_type=EditorialProposalAdoptionService)

    class _PlacementChangedService:
        async def adopt(self, *_args: Any, **_kwargs: Any) -> None:
            raise EditorialProposalAdoptionError("adoption_placement_changed")

    monkeypatch.setattr(
        editorial_workspace_api,
        "get_settings",
        lambda: Settings(_env_file=None, editorial_proposal_accept_enabled=True),
    )
    monkeypatch.setattr(
        editorial_workspace_api,
        "_proposal_adoption_service",
        lambda _request: _PlacementChangedService(),
    )

    async def current_actor(_request: Any) -> UUID:
        return fixture.actor_id

    monkeypatch.setattr(editorial_workspace_api, "_thread_actor_id", current_actor)
    app = FastAPI()
    app.include_router(editorial_workspace_api.router)
    path = (
        f"/api/subjects/{fixture.proposal.subject_id}/editor/threads/"
        f"{fixture.occurrence_id}/proposals/{fixture.proposal.id}/accept"
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://editorial.test"
    ) as client:
        response = await client.post(
            path,
            headers={"Idempotency-Key": f"accept_{uuid4().hex}"},
            json={"expected_state_version": 1, "patch_sha256": fixture.patch_sha256},
        )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "adoption_placement_changed"
