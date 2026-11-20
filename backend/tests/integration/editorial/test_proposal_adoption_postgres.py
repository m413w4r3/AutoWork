from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from io import BytesIO
from typing import Any, cast
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI

from cti_app.api import editorial_workspace as editorial_workspace_api
from cti_app.application.blob_storage import BlobStore
from cti_app.application.editorial_proposal_adoption import (
    EditorialProposalAdoptionError,
    EditorialProposalAdoptionInputs,
    EditorialProposalAdoptionService,
    EnrichmentCandidateInputs,
    SynthesisCandidateInputs,
)
from cti_app.application.editorial_proposal_adoption_inputs import (
    ProductionEditorialProposalAdoptionInputs,
)
from cti_app.application.editorial_synthesis import targets
from cti_app.application.editorial_synthesis.candidate import (
    SynthesisCandidatePersistence,
)
from cti_app.application.editorial_workspace.registry import (
    WorkspaceOccurrenceResolution,
    WorkspaceSnapshotRecord,
    registry_hash,
    resolve_workspace_target,
)
from cti_app.application.persistence import UnitOfWork, UnitOfWorkFactory
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.application.production_stages import SynthesisService
from cti_app.application.production_synthesis import (
    ProductionSynthesisService,
    build_synthesis_evidence_pack,
    canonical_extraction_hash,
)
from cti_app.application.production_synthesis_revision import _apply_wire_patch
from cti_app.application.subject_production import capture_production_input_snapshot
from cti_app.config import Settings
from cti_app.domain.blobs import BlobDescriptor, BlobRecord
from cti_app.domain.classification import TLP
from cti_app.domain.discovery import SourceRole
from cti_app.domain.editions import Edition
from cti_app.domain.editorial_proposal import (
    EditorialProposal,
    EditorialProposalEvent,
    EditorialProposalEventKind,
    EditorialProposalOrigin,
    EditorialProposalState,
)
from cti_app.domain.editorial_workspace import (
    AdvancedBlockRegistry,
    BlockRegistryEntry,
    EditorialBlockKind,
    EditorialRevisionStatus,
    EditorialSourceStage,
    EditorialTransition,
)
from cti_app.domain.entities import SourceDocument, Subject
from cti_app.domain.production import (
    EXTRACTION_PROFILE_POLICY_VERSION,
    ExtractionProfile,
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionArtifactStatus,
    ProductionEvidenceBasis,
    ProductionInputSnapshot,
    ProductionRun,
    SynthesisMode,
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
    production_extraction_to_json,
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
from tests.integration.test_production_reuse_postgres import (
    _prepare_reusable_article,
    _seed_subject_discovery_lineage,
)

pytestmark = pytest.mark.integration

_PATCH_BUCKET = "editorial-proposal-patches"
_PATCH_MIME = "application/vnd.autowork.editorial-patch+json"
_CANONICAL_BUCKET = "production-artifacts-canonical"


class _MemoryBlobStore:
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}
        self.put_calls = 0
        self.delete_calls = 0
        self.put_barrier_target: int | None = None
        self._put_barrier = asyncio.Event()

    def seed(self, descriptor: BlobDescriptor, content: bytes) -> None:
        self.objects[(descriptor.logical_bucket, descriptor.sha256)] = content

    def synchronize_next_puts(self, count: int) -> None:
        self.put_barrier_target = self.put_calls + count

    async def put(self, source: BytesIO, *, logical_bucket: str, mime_type: str) -> BlobDescriptor:
        self.put_calls += 1
        content = source.read()
        digest = hashlib.sha256(content).hexdigest()
        self.objects.setdefault((logical_bucket, digest), content)
        if self.put_barrier_target is not None and self.put_calls <= self.put_barrier_target:
            if self.put_calls == self.put_barrier_target:
                self._put_barrier.set()
            await self._put_barrier.wait()
        return BlobDescriptor(digest, len(content), mime_type, logical_bucket)

    async def read(self, descriptor: BlobDescriptor, *, max_bytes: int) -> bytes:
        content = self.objects[(descriptor.logical_bucket, descriptor.sha256)]
        if len(content) > max_bytes:
            raise ValueError("synthetic_read_limit")
        return content

    async def exists(self, descriptor: BlobDescriptor) -> bool:
        return (descriptor.logical_bucket, descriptor.sha256) in self.objects

    async def delete(self, descriptor: BlobDescriptor) -> None:
        self.delete_calls += 1
        self.objects.pop((descriptor.logical_bucket, descriptor.sha256), None)


class _MemoryArtifactStore:
    def __init__(self, blobs: Mapping[UUID, bytes]) -> None:
        self.blobs = dict(blobs)

    async def read_bytes(self, blob_id: UUID, *, max_bytes: int | None = None) -> bytes:
        content = self.blobs[blob_id]
        if max_bytes is not None and len(content) > max_bytes:
            raise ValueError("synthetic_read_limit")
        return content

    async def read_json(self, blob_id: UUID) -> Any:
        return json.loads(await self.read_bytes(blob_id))


class _Workspace:
    def __init__(self, resolution: WorkspaceOccurrenceResolution) -> None:
        self.resolution = resolution

    async def resolve_occurrence(
        self, _subject_id: UUID, _occurrence_id: UUID
    ) -> WorkspaceOccurrenceResolution:
        return self.resolution

    async def resolve_occurrence_in_uow(
        self, _uow: UnitOfWork, _subject_id: UUID, _occurrence_id: UUID
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


@dataclass
class _SyntheticFences:
    tlp: TLP = TLP.AMBER
    revoked: bool = False
    external_llm_allowed: bool = True
    thread_binding_valid: bool = True
    admitted_evidence_refs: frozenset[str] = frozenset()


class _Inputs(EditorialProposalAdoptionInputs):
    def __init__(self, synthesis: SynthesisCandidateInputs) -> None:
        self.synthesis = synthesis
        self.fences = _SyntheticFences()
        self.revalidated = 0

    async def synthesis_inputs(
        self,
        _proposal: EditorialProposal,
        _patch: Mapping[str, Any],
        *,
        base_artifact: ProductionArtifact,
        base_payload: dict[str, Any],
        base_bytes: bytes,
        resolution: WorkspaceOccurrenceResolution,
    ) -> SynthesisCandidateInputs:
        assert base_artifact.id == resolution.artifact_id
        assert self.synthesis.base_payload == base_payload
        assert base_bytes
        return self.synthesis

    async def enrichment_inputs(
        self,
        *_args: Any,
        **_kwargs: Any,
    ) -> EnrichmentCandidateInputs:
        raise AssertionError("synthetic integration fixture only adopts synthesis proposals")

    async def revalidate_evidence_policy_and_thread(
        self,
        _uow: UnitOfWork,
        proposal: EditorialProposal,
        _patch: Mapping[str, Any],
        *,
        resolution: WorkspaceOccurrenceResolution,
    ) -> None:
        self.revalidated += 1
        if resolution.record.subject_id != proposal.subject_id:
            raise EditorialProposalAdoptionError("proposal_base_stale")
        if self.fences.tlp is TLP.RED:
            raise EditorialProposalAdoptionError("external_llm_tlp_blocked")
        if self.fences.revoked:
            raise EditorialProposalAdoptionError("proposal_evidence_revoked")
        if not self.fences.external_llm_allowed:
            raise EditorialProposalAdoptionError("external_llm_blocked")
        if not self.fences.thread_binding_valid:
            raise EditorialProposalAdoptionError("thread_binding_stale")
        if not set(proposal.evidence_refs) <= self.fences.admitted_evidence_refs:
            raise EditorialProposalAdoptionError("proposal_evidence_not_admissible")


@dataclass(frozen=True)
class _Seeded:
    subject: Subject
    run: ProductionRun
    base_artifact: ProductionArtifact
    snapshot: WorkspaceSnapshotRecord
    production_input: ProductionInputSnapshot
    extraction: ProductionExtractionV1
    synthesis: ProductionSynthesisV1
    proposals: tuple[EditorialProposal, ...]
    actor_id: UUID
    patch_sha256: str
    blob_store: _MemoryBlobStore
    artifact_store: _MemoryArtifactStore
    workspace: _Workspace
    inputs: _Inputs


def _synthetic_candidate_context(
    *,
    run_id: UUID,
    artifact: ProductionArtifact,
    production_input: ProductionInputSnapshot,
    extraction: ProductionExtractionV1,
    synthesis: ProductionSynthesisV1,
    base_payload: dict[str, Any],
    base_sha256: str,
) -> SynthesisCandidateInputs:
    persistence = SynthesisCandidatePersistence(
        run_id=run_id,
        subject_id=artifact.subject_id,
        input_hash=artifact.input_hash,
        extraction=extraction,
        snapshot=production_input,
        raw_result=None,
        model_run_id=None,
        publication_kind=PublicationKind.BRIEF,
        mode=SynthesisMode.FRESH,
        expected_current_artifact_id=artifact.id,
        expected_current_input_hash=artifact.input_hash,
        expected_current_canonical_sha256=base_sha256,
    )
    target = TargetedEditorialRevisionTarget(
        kind=TargetedEditorialRevisionTargetKind.TITLE,
        stable_ids=(),
    )
    return SynthesisCandidateInputs(
        base_payload=base_payload,
        base_synthesis=synthesis,
        target=target,
        evidence_pack=build_synthesis_evidence_pack(production_input, extraction),
        extraction=extraction,
        snapshot=production_input,
        apply_patch=_apply_wire_patch,
        persistence=persistence,
        registry_lineage=(),
        publication_kind=PublicationKind.BRIEF,
    )


async def _seed(
    uow_factory: UnitOfWorkFactory,
    *,
    proposal_count: int = 2,
    seed_discovery_lineage: bool = False,
) -> _Seeded:
    blob_store = _MemoryBlobStore()
    edition = Edition(
        country="France",
        country_code="FR",
        period_start=date(2026, 11, 1),
        period_end=date(2026, 11, 30),
        tlp=TLP.AMBER,
        languages=("fr",),
    )
    async with uow_factory() as uow:
        # Resolve the logical edition key before deriving child foreign keys.
        # add_if_absent updates edition.id when this shared row already exists.
        await uow.editions.add_if_absent(edition)
        await uow.commit()

    subject = Subject(
        edition_id=edition.id,
        title="Synthetic adoption subject",
        slug=f"proposal-adoption-{uuid4().hex}",
        tlp=TLP.AMBER,
    )
    run = ProductionRun(subject_id=subject.id, edition_id=edition.id)
    if seed_discovery_lineage:
        async with uow_factory() as uow:
            await uow.subjects.add(subject)
            await uow.commit()
        discovery_batch, _ = await _prepare_reusable_article(
            uow_factory,
            edition=edition,
            subject=subject,
            title=subject.title,
        )
        await _seed_subject_discovery_lineage(
            uow_factory,
            edition=edition,
            subject_batches=((subject, discovery_batch),),
        )
        async with uow_factory() as uow:
            production_input = await capture_production_input_snapshot(
                uow,
                production_run_id=run.id,
                subject_id=subject.id,
                edition_id=edition.id,
                research_date=date(2026, 11, 19),
                captured_at=run.created_at,
            )
    else:
        production_input = ProductionInputSnapshot(
            production_run_id=run.id,
            edition_id=edition.id,
            subject_id=subject.id,
            subject_version=1,
            subject_title=subject.title,
            subject_tlp=TLP.AMBER,
            selection_decision_id=uuid4(),
            origin_discovery_subject_id=uuid4(),
            canonical_discovery_subject_id=uuid4(),
            discovery_snapshot_id=uuid4(),
            discovery_snapshot_version=1,
            member_candidate_ids=(),
            discovery_summary="Synthetic summary.",
            actor_or_campaign="Synthetic actor",
            period_start=date(2026, 11, 1),
            period_end=date(2026, 11, 30),
            publication_language="fr",
            research_date=date(2026, 11, 19),
        )
    operation = TargetedEditorialRevisionOperation.REWRITE_TITLE
    target = TargetedEditorialRevisionTarget(
        kind=TargetedEditorialRevisionTargetKind.TITLE,
        stable_ids=(),
    )
    source_document_id = uuid4()
    extraction = ProductionExtractionV1(
        schema_version=1,
        subject_id=subject.id,
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
    base_synthesis = ProductionSynthesisV1(
        schema_version=PRODUCTION_SYNTHESIS_SCHEMA_VERSION,
        subject_id=subject.id,
        production_input_hash=production_input.input_hash,
        extraction_hash=canonical_extraction_hash(extraction),
        publication_language="fr",
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
    base_payload = production_synthesis_to_json(base_synthesis)
    base_bytes = ProductionArtifactStore.canonical_json_bytes(base_payload)
    base_sha256 = hashlib.sha256(base_bytes).hexdigest()
    base_artifact = ProductionArtifact(
        production_run_id=run.id,
        subject_id=subject.id,
        stage=ProductionArtifactStage.SYNTHESIS,
        version=1,
        input_hash=production_input.input_hash,
        status=ProductionArtifactStatus.VERIFIED,
    )
    resolved_target = targets.resolve_target(base_payload, target, operation)
    entry = BlockRegistryEntry(
        block_id=uuid4(),
        kind=EditorialBlockKind.TITLE,
        parent_block_id=None,
        position=0,
        content_sha256=resolved_target.before_hash,
        source_stage=EditorialSourceStage.SYNTHESIS,
        structural_pointer="/title",
        revision_status=EditorialRevisionStatus.READY,
    )
    workspace_target = resolve_workspace_target(
        entry=entry,
        canonical_content=base_payload,
        operation=operation.value,
    )
    proposal_target_id = workspace_target.legacy_target_id
    assert proposal_target_id is not None
    snapshot = WorkspaceSnapshotRecord(
        snapshot_id=uuid4(),
        subject_id=subject.id,
        production_run_id=run.id,
        synthesis_artifact_id=base_artifact.id,
        synthesis_version=base_artifact.version,
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
        workspace_pin_hash="3" * 64,
        registry_hash=registry_hash((entry,)),
        block_registry=(entry,),
    )
    patch_payload = {
        "schema_version": "editorial-patch-v1",
        "operation": operation.value,
        "target": target.to_json(),
        "target_id": proposal_target_id,
        "occurrence_id": str(entry.block_id),
        "before_hash": resolved_target.before_hash,
        "after": "[Synthetic actor] Revised report",
        "rationale": "Synthetic test rationale.",
    }
    patch_bytes = ProductionArtifactStore.canonical_json_bytes(patch_payload)
    patch_sha256 = hashlib.sha256(patch_bytes).hexdigest()
    patch_descriptor = BlobDescriptor(patch_sha256, len(patch_bytes), _PATCH_MIME, _PATCH_BUCKET)
    base_descriptor = BlobDescriptor(
        base_sha256, len(base_bytes), "application/json", _CANONICAL_BUCKET
    )
    blob_store.seed(patch_descriptor, patch_bytes)
    blob_store.seed(base_descriptor, base_bytes)
    base_blob = BlobRecord(descriptor=base_descriptor)
    patch_blob = BlobRecord(descriptor=patch_descriptor)
    actor_id, variant_group_id = uuid4(), uuid4()
    proposals: list[EditorialProposal] = []

    async with uow_factory() as uow:
        if not seed_discovery_lineage:
            await uow.subjects.add(subject)
        await uow.production_runs.add(run)
        base_blob = await uow.blobs.add_if_absent(base_blob)
        patch_blob = await uow.blobs.add_if_absent(patch_blob)
        base_artifact = replace(base_artifact, canonical_blob_id=base_blob.id)
        await uow.production_artifacts.append(base_artifact)
        await uow.editorial_workspace_snapshots.append_if_absent(snapshot)
        for index in range(proposal_count):
            proposal = EditorialProposal(
                id=uuid4(),
                subject_id=subject.id,
                production_run_id=run.id,
                origin=EditorialProposalOrigin.MANUAL_PATCH,
                snapshot_id=snapshot.snapshot_id,
                occurrence_id=entry.block_id,
                base_artifact_id=base_artifact.id,
                base_artifact_version=base_artifact.version,
                base_input_hash=base_artifact.input_hash,
                base_canonical_sha256=base_sha256,
                base_routing_hash=None,
                base_pin_hash=snapshot.workspace_pin_hash,
                target_id=proposal_target_id,
                operation=operation.value,
                before_hash=resolved_target.before_hash,
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
                idempotency_key=f"synthetic-prepared-{index}-{uuid4().hex}",
                request_sha256=hashlib.sha256(f"prepared-{index}".encode()).hexdigest(),
            )
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
            proposals.append(proposal)
        await uow.commit()

    resolution = WorkspaceOccurrenceResolution(
        record=snapshot,
        entry=entry,
        binding_state="valid",
        artifact_id=base_artifact.id,
        artifact_version=base_artifact.version,
        canonical_sha256=base_sha256,
    )
    synthesis_inputs = _synthetic_candidate_context(
        run_id=run.id,
        artifact=base_artifact,
        production_input=production_input,
        extraction=extraction,
        synthesis=base_synthesis,
        base_payload=base_payload,
        base_sha256=base_sha256,
    )
    inputs = _Inputs(synthesis_inputs)
    return _Seeded(
        subject=subject,
        run=run,
        base_artifact=base_artifact,
        snapshot=snapshot,
        production_input=production_input,
        extraction=extraction,
        synthesis=base_synthesis,
        proposals=tuple(proposals),
        actor_id=actor_id,
        patch_sha256=patch_sha256,
        blob_store=blob_store,
        artifact_store=_MemoryArtifactStore({base_blob.id: base_bytes}),
        workspace=_Workspace(resolution),
        inputs=inputs,
    )


def _service(seeded: _Seeded, uow_factory: UnitOfWorkFactory) -> EditorialProposalAdoptionService:
    artifact_store = cast(ProductionArtifactStore, seeded.artifact_store)
    return EditorialProposalAdoptionService(
        uow_factory=uow_factory,
        blob_store=cast(BlobStore, seeded.blob_store),
        artifact_store=artifact_store,
        workspace_service=seeded.workspace,
        inputs=seeded.inputs,
        synthesis_writer=SynthesisService(uow_factory, artifact_store),
        enrichment_writer=cast(Any, None),
    )


class _FailingImpactRepository:
    def __init__(self, repository: Any) -> None:
        self.repository = repository

    def __getattr__(self, name: str) -> Any:
        return getattr(self.repository, name)

    async def add(self, _impact: Any) -> None:
        raise RuntimeError("synthetic impact insert failure")


class _FaultingUow:
    def __init__(self, inner: UnitOfWork, state: _FaultState) -> None:
        self.inner = inner
        self.state = state

    async def __aenter__(self) -> _FaultingUow:
        await self.inner.__aenter__()
        if self.state.fail_impact:
            self.inner.editorial_proposal_impacts = _FailingImpactRepository(
                self.inner.editorial_proposal_impacts
            )  # type: ignore[assignment]
        return self

    async def __aexit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> None:
        await self.inner.__aexit__(exc_type, exc_value, traceback)

    async def commit(self) -> None:
        await self.inner.commit()
        if self.state.fail_commit_once:
            self.state.fail_commit_once = False
            raise RuntimeError("synthetic post-commit response loss")

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)


@dataclass
class _FaultState:
    fail_impact: bool = False
    fail_commit_once: bool = False


class _FaultingUowFactory:
    def __init__(self, inner: UnitOfWorkFactory, state: _FaultState) -> None:
        self.inner = inner
        self.state = state

    def __call__(self) -> _FaultingUow:
        return _FaultingUow(self.inner(), self.state)


def _adopt_kwargs(seeded: _Seeded, proposal: EditorialProposal, key: str) -> dict[str, Any]:
    return {
        "subject_id": proposal.subject_id,
        "occurrence_id": proposal.occurrence_id,
        "proposal_id": proposal.id,
        "actor_id": seeded.actor_id,
        "expected_state_version": 1,
        "patch_sha256": seeded.patch_sha256,
        "idempotency_key": key,
    }


@pytest.mark.asyncio
async def test_two_concurrent_adoptions_of_same_blob_have_one_winner_and_keep_object(
    uow_factory: UnitOfWorkFactory,
) -> None:
    seeded = await _seed(uow_factory, proposal_count=1)
    proposal = seeded.proposals[0]
    seeded.blob_store.synchronize_next_puts(2)
    service = _service(seeded, uow_factory)

    results = await asyncio.gather(
        service.adopt(**_adopt_kwargs(seeded, proposal, f"adopt-a-{uuid4().hex}")),
        service.adopt(**_adopt_kwargs(seeded, proposal, f"adopt-b-{uuid4().hex}")),
        return_exceptions=True,
    )

    successes = [item for item in results if not isinstance(item, BaseException)]
    failures = [item for item in results if isinstance(item, BaseException)]
    assert len(successes) == 1
    assert len(failures) == 1
    assert isinstance(failures[0], EditorialProposalAdoptionError)
    assert failures[0].code == "proposal_state_conflict"
    async with uow_factory() as uow:
        accepted = await uow.editorial_proposals.get_for_occurrence(
            proposal.subject_id, proposal.occurrence_id, proposal.id
        )
        assert accepted is not None and accepted.state is EditorialProposalState.ACCEPTED
        artifact = await uow.production_artifacts.get(cast(UUID, accepted.accepted_artifact_id))
        assert artifact is not None and artifact.canonical_blob_id is not None
        blob = await uow.blobs.get(artifact.canonical_blob_id)
        assert blob is not None
        assert await uow.blobs.count_references(blob.id) > 0
        assert await uow.editorial_proposal_impacts.get_by_proposal(proposal.id) is not None
    assert await seeded.blob_store.exists(blob.descriptor)
    adopted_bytes = await seeded.blob_store.read(blob.descriptor, max_bytes=blob.descriptor.size)
    assert hashlib.sha256(adopted_bytes).hexdigest() == blob.descriptor.sha256
    assert seeded.blob_store.delete_calls == 0


@pytest.mark.asyncio
async def test_accept_route_uses_sql_uow_and_replays_same_result(
    uow_factory: UnitOfWorkFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seeded = await _seed(uow_factory, proposal_count=1)
    proposal = seeded.proposals[0]
    service = _service(seeded, uow_factory)
    monkeypatch.setattr(
        editorial_workspace_api,
        "get_settings",
        lambda: Settings(_env_file=None, editorial_proposal_accept_enabled=True),
    )
    monkeypatch.setattr(
        editorial_workspace_api, "_proposal_adoption_service", lambda _request: service
    )

    async def current_actor(_request: Any) -> UUID:
        return seeded.actor_id

    monkeypatch.setattr(editorial_workspace_api, "_thread_actor_id", current_actor)
    app = FastAPI()
    app.include_router(editorial_workspace_api.router)
    path = (
        f"/api/subjects/{proposal.subject_id}/editor/threads/{proposal.occurrence_id}/"
        f"proposals/{proposal.id}/accept"
    )
    headers = {"Idempotency-Key": f"accept_{uuid4().hex}"}
    payload = {"expected_state_version": 1, "patch_sha256": seeded.patch_sha256}

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://editorial.test"
    ) as client:
        first = await client.post(path, headers=headers, json=payload)
        replay = await client.post(path, headers=headers, json=payload)

    assert first.status_code == replay.status_code == 200
    assert first.json() == replay.json()
    assert first.json()["state"] == EditorialProposalState.ACCEPTED.value
    assert seeded.inputs.revalidated == 1
    async with uow_factory() as uow:
        accepted = await uow.editorial_proposals.get_for_occurrence(
            proposal.subject_id, proposal.occurrence_id, proposal.id
        )
        assert accepted is not None
        assert accepted.state is EditorialProposalState.ACCEPTED
        assert first.json()["artifact_id"] == str(accepted.accepted_artifact_id)
        impact = await uow.editorial_proposal_impacts.get_by_proposal(proposal.id)
        assert impact is not None


@pytest.mark.asyncio
async def test_accept_route_policy_refusal_keeps_sql_state_unchanged(
    uow_factory: UnitOfWorkFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seeded = await _seed(uow_factory, proposal_count=1)
    proposal = seeded.proposals[0]
    seeded.inputs.fences.revoked = True
    service = _service(seeded, uow_factory)
    idempotency_key = f"reject_{uuid4().hex}"
    async with uow_factory() as uow:
        before_artifact_ids = {
            item.id
            for item in await uow.production_artifacts.list_for_run(proposal.production_run_id)
        }
    monkeypatch.setattr(
        editorial_workspace_api,
        "get_settings",
        lambda: Settings(_env_file=None, editorial_proposal_accept_enabled=True),
    )
    monkeypatch.setattr(
        editorial_workspace_api, "_proposal_adoption_service", lambda _request: service
    )

    async def current_actor(_request: Any) -> UUID:
        return seeded.actor_id

    monkeypatch.setattr(editorial_workspace_api, "_thread_actor_id", current_actor)
    app = FastAPI()
    app.include_router(editorial_workspace_api.router)
    path = (
        f"/api/subjects/{proposal.subject_id}/editor/threads/{proposal.occurrence_id}/"
        f"proposals/{proposal.id}/accept"
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://editorial.test"
    ) as client:
        response = await client.post(
            path,
            headers={"Idempotency-Key": idempotency_key},
            json={"expected_state_version": 1, "patch_sha256": seeded.patch_sha256},
        )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "proposal_evidence_revoked"
    assert seeded.inputs.revalidated == 1
    async with uow_factory() as uow:
        current = await uow.editorial_proposals.get_for_occurrence(
            proposal.subject_id, proposal.occurrence_id, proposal.id
        )
        assert current is not None and current.state is EditorialProposalState.PROPOSED
        assert current.state_version == proposal.state_version
        assert {
            item.id
            for item in await uow.production_artifacts.list_for_run(proposal.production_run_id)
        } == before_artifact_ids
        assert (
            await uow.production_artifacts.get_current(
                proposal.production_run_id, ProductionArtifactStage.SYNTHESIS.value
            )
            == seeded.base_artifact
        )
        assert await uow.editorial_proposal_impacts.get_by_proposal(proposal.id) is None
        assert (
            await uow.editorial_proposals.get_event_by_idempotency(proposal.id, idempotency_key)
            is None
        )


@pytest.mark.asyncio
async def test_adoption_rechecks_current_red_subject_tlp_without_sql_writes(
    uow_factory: UnitOfWorkFactory,
) -> None:
    seeded = await _seed(uow_factory, proposal_count=1, seed_discovery_lineage=True)
    proposal = seeded.proposals[0]
    extraction_bytes = ProductionArtifactStore.canonical_json_bytes(
        production_extraction_to_json(seeded.extraction)
    )
    extraction_sha256 = hashlib.sha256(extraction_bytes).hexdigest()
    extraction_blob = BlobRecord(
        descriptor=BlobDescriptor(
            extraction_sha256,
            len(extraction_bytes),
            "application/json",
            _CANONICAL_BUCKET,
        )
    )
    source_bytes = b"synthetic source document for policy revalidation"
    source_sha256 = hashlib.sha256(source_bytes).hexdigest()
    source_blob = BlobRecord(
        descriptor=BlobDescriptor(
            source_sha256,
            len(source_bytes),
            "text/plain",
            "source-documents",
        )
    )
    extraction_artifact = ProductionArtifact(
        production_run_id=seeded.run.id,
        subject_id=seeded.subject.id,
        stage=ProductionArtifactStage.EXTRACTION,
        version=1,
        input_hash=seeded.production_input.input_hash,
        canonical_blob_id=extraction_blob.id,
    )

    seeded.blob_store.seed(extraction_blob.descriptor, extraction_bytes)
    seeded.blob_store.seed(source_blob.descriptor, source_bytes)
    seeded.artifact_store.blobs[extraction_blob.id] = extraction_bytes
    async with uow_factory() as uow:
        await uow.blobs.add_if_absent(extraction_blob)
        await uow.blobs.add_if_absent(source_blob)
        await uow.production_artifacts.append(extraction_artifact)
        await uow.production_input_snapshots.add(seeded.production_input)
        await uow.source_documents.add(
            SourceDocument(
                id=seeded.extraction.sources[0].source_document_id,
                subject_id=seeded.subject.id,
                blob_id=source_blob.id,
                original_name="synthetic-report.txt",
                origin="synthetic integration fixture",
                acquired_at=datetime.now(UTC),
                license_restriction=None,
                tlp=TLP.AMBER,
                do_not_submit=False,
                external_llm_allowed=True,
            )
        )
        current_subject = await uow.subjects.get(seeded.subject.id)
        assert current_subject is not None and current_subject.version == 1
        current_subject.update_metadata(title=current_subject.title, tlp=TLP.RED)
        assert await uow.subjects.update(current_subject, expected_version=1)
        await uow.commit()

    artifact_store = cast(ProductionArtifactStore, seeded.artifact_store)
    synthesis_writer = SynthesisService(uow_factory, artifact_store)
    synthesis_loader = ProductionSynthesisService(
        uow_factory=uow_factory,
        artifact_store=artifact_store,
        model_gateway=cast(Any, None),
        synthesis_service=synthesis_writer,
    )

    production_inputs = ProductionEditorialProposalAdoptionInputs(
        uow_factory=uow_factory,
        artifact_store=artifact_store,
        workspace_service=seeded.workspace,
        synthesis_loader=synthesis_loader,
        enrichment_loader=cast(Any, None),
        enrichment_revision_service=cast(Any, None),
    )
    service = EditorialProposalAdoptionService(
        uow_factory=uow_factory,
        blob_store=cast(BlobStore, seeded.blob_store),
        artifact_store=artifact_store,
        workspace_service=seeded.workspace,
        inputs=production_inputs,
        synthesis_writer=synthesis_writer,
        enrichment_writer=cast(Any, None),
    )
    before_blob_addresses = set(seeded.blob_store.objects)
    before_put_calls = seeded.blob_store.put_calls
    async with uow_factory() as uow:
        before_input_snapshot = await uow.production_input_snapshots.get_by_run(seeded.run.id)
        assert before_input_snapshot == seeded.production_input
        before_proposal = await uow.editorial_proposals.get_for_occurrence(
            proposal.subject_id, proposal.occurrence_id, proposal.id
        )
        assert before_proposal == proposal
        before_artifact_ids = {
            item.id for item in await uow.production_artifacts.list_for_run(seeded.run.id)
        }

    idempotency_key = f"red-subject-{uuid4().hex}"
    with pytest.raises(
        EditorialProposalAdoptionError,
        match="external_llm_tlp_blocked",
    ):
        await service.adopt(**_adopt_kwargs(seeded, proposal, idempotency_key))

    assert set(seeded.blob_store.objects) == before_blob_addresses
    assert seeded.blob_store.put_calls == before_put_calls
    async with uow_factory() as uow:
        current_subject = await uow.subjects.get(seeded.subject.id)
        assert current_subject is not None
        assert current_subject.tlp is TLP.RED and current_subject.version == 2
        current = await uow.editorial_proposals.get_for_occurrence(
            proposal.subject_id, proposal.occurrence_id, proposal.id
        )
        assert current == before_proposal
        assert current is not None and current.state is EditorialProposalState.PROPOSED
        assert (
            await uow.production_input_snapshots.get_by_run(seeded.run.id) == before_input_snapshot
        )
        assert (
            await uow.editorial_proposals.get_event_by_idempotency(proposal.id, idempotency_key)
            is None
        )
        assert {
            item.id for item in await uow.production_artifacts.list_for_run(seeded.run.id)
        } == before_artifact_ids
        assert await uow.editorial_proposal_impacts.get_by_proposal(proposal.id) is None


@pytest.mark.asyncio
async def test_orphan_catalog_row_can_be_recreated_by_later_adoption(
    uow_factory: UnitOfWorkFactory,
) -> None:
    seeded = await _seed(uow_factory, proposal_count=1)
    existing_blob_addresses = set(seeded.blob_store.objects)
    proposal = seeded.proposals[0]
    service = _service(seeded, uow_factory)
    persist_acceptance = service._persist_acceptance
    fail_once = True

    async def fail_before_persist(**kwargs: Any) -> Any:
        nonlocal fail_once
        if fail_once:
            fail_once = False
            raise RuntimeError("synthetic failure after candidate upload")
        return await persist_acceptance(**kwargs)

    service._persist_acceptance = fail_before_persist  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="after candidate upload"):
        await service.adopt(**_adopt_kwargs(seeded, proposal, f"adopt-after-collect-{uuid4().hex}"))

    new_blob_addresses = set(seeded.blob_store.objects) - existing_blob_addresses
    candidate_objects = [
        (address, seeded.blob_store.objects[address])
        for address in new_blob_addresses
        if address[0] == _CANONICAL_BUCKET
    ]
    assert len(candidate_objects) == 1
    address, candidate_bytes = candidate_objects[0]
    logical_bucket, digest = address
    descriptor = BlobDescriptor(digest, len(candidate_bytes), "application/json", logical_bucket)
    async with uow_factory() as uow:
        orphan = await uow.blobs.add_if_absent(BlobRecord(descriptor=descriptor))
        assert await uow.blobs.count_references(orphan.id) == 0
        await uow.commit()

    assert await service.cleanup_orphan_blob(descriptor)
    async with uow_factory() as uow:
        assert await uow.blobs.get_by_address(logical_bucket, digest) is None
    assert await seeded.blob_store.exists(descriptor)

    result = await service.adopt(
        **_adopt_kwargs(seeded, proposal, f"adopt-after-collect-{uuid4().hex}")
    )
    async with uow_factory() as uow:
        accepted = await uow.editorial_proposals.get_for_occurrence(
            proposal.subject_id, proposal.occurrence_id, proposal.id
        )
        assert accepted is not None and accepted.state is EditorialProposalState.ACCEPTED
        artifact = await uow.production_artifacts.get(cast(UUID, accepted.accepted_artifact_id))
        assert artifact is not None and artifact.canonical_blob_id is not None
        blob = await uow.blobs.get(artifact.canonical_blob_id)
        assert blob is not None and blob.id != orphan.id
        assert await uow.blobs.count_references(blob.id) > 0

    assert result.artifact.id == artifact.id
    assert await seeded.blob_store.read(blob.descriptor, max_bytes=blob.descriptor.size) == (
        candidate_bytes
    )
    assert seeded.blob_store.delete_calls == 0


@pytest.mark.asyncio
async def test_adopting_variant_stales_sibling_in_same_transaction(
    uow_factory: UnitOfWorkFactory,
) -> None:
    seeded = await _seed(uow_factory)
    accepted, sibling = seeded.proposals
    result = await _service(seeded, uow_factory).adopt(
        **_adopt_kwargs(seeded, accepted, f"adopt-variant-{uuid4().hex}")
    )
    assert result.proposal.state is EditorialProposalState.ACCEPTED

    async with uow_factory() as uow:
        accepted_row = await uow.editorial_proposals.get_for_occurrence(
            accepted.subject_id, accepted.occurrence_id, accepted.id
        )
        stale_row = await uow.editorial_proposals.get_for_occurrence(
            sibling.subject_id, sibling.occurrence_id, sibling.id
        )
        assert accepted_row is not None and accepted_row.state is EditorialProposalState.ACCEPTED
        assert stale_row is not None and stale_row.state is EditorialProposalState.STALE
        assert stale_row.state_version == 2
        event = await uow.editorial_proposals.get_event_by_idempotency(
            sibling.id, f"accept-stale-{accepted.id.hex}-{sibling.id.hex}"
        )
        assert event is not None and event.state is EditorialProposalState.STALE


@pytest.mark.asyncio
async def test_adoption_relation_changes_roll_back_if_a_late_step_fails(
    uow_factory: UnitOfWorkFactory,
) -> None:
    seeded = await _seed(uow_factory)
    proposal, sibling = seeded.proposals
    key = f"adopt-rollback-{uuid4().hex}"
    faulting_factory = _FaultingUowFactory(uow_factory, _FaultState(fail_impact=True))

    with pytest.raises(RuntimeError, match="impact insert failure"):
        await _service(seeded, cast(UnitOfWorkFactory, faulting_factory)).adopt(
            **_adopt_kwargs(seeded, proposal, key)
        )

    async with uow_factory() as uow:
        stored = await uow.editorial_proposals.get_for_occurrence(
            proposal.subject_id, proposal.occurrence_id, proposal.id
        )
        assert stored is not None and stored.state is EditorialProposalState.PROPOSED
        sibling_row = await uow.editorial_proposals.get_for_occurrence(
            sibling.subject_id, sibling.occurrence_id, sibling.id
        )
        assert sibling_row is not None and sibling_row.state is EditorialProposalState.PROPOSED
        assert await uow.editorial_proposal_impacts.get_by_proposal(proposal.id) is None
        assert len(await uow.production_artifacts.list_for_run(proposal.production_run_id)) == 1
        assert await uow.editorial_proposals.get_event_by_idempotency(proposal.id, key) is None


@pytest.mark.asyncio
async def test_replay_after_post_commit_response_loss_returns_same_adoption(
    uow_factory: UnitOfWorkFactory,
) -> None:
    seeded = await _seed(uow_factory, proposal_count=1)
    proposal = seeded.proposals[0]
    key = f"adopt-replay-{uuid4().hex}"
    faulting_factory = _FaultingUowFactory(uow_factory, _FaultState(fail_commit_once=True))
    service = _service(seeded, cast(UnitOfWorkFactory, faulting_factory))

    with pytest.raises(RuntimeError, match="post-commit response loss"):
        await service.adopt(**_adopt_kwargs(seeded, proposal, key))
    first_put_count = seeded.blob_store.put_calls

    replay = await service.adopt(**_adopt_kwargs(seeded, proposal, key))

    assert replay.replayed
    assert replay.proposal.state is EditorialProposalState.ACCEPTED
    assert replay.artifact.id == replay.proposal.accepted_artifact_id
    assert replay.impact.state.value == "pending"
    assert seeded.blob_store.put_calls == first_put_count
    assert seeded.inputs.revalidated == 1
