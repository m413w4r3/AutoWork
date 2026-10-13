from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from cti_app.application.analyst_opinion_service import (
    AnalystOpinionService,
    OpinionCompositionSelection,
    OpinionError,
    OpinionErrorCode,
    OpinionRevisionEdits,
)
from cti_app.application.investigation_persistence import VersionConflictError
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.domain.analyst_opinion import (
    AnalystOpinionStatus,
    AnalystOpinionV1,
    OpinionBlock,
    OpinionBlockType,
    OpinionClaimQualification,
)
from cti_app.domain.editorial import AnalystDecision
from cti_app.domain.investigation import (
    DisclosureBounds,
    EvidenceKind,
    EvidenceObservation,
    FindingAssessment,
    FindingKind,
    InvestigationBudget,
    InvestigationEnvelope,
    InvestigationFinding,
    NoveltyClass,
    TLPLevel,
)
from cti_app.domain.production import (
    AnalystInvestigation,
    LoopBudget,
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionArtifactStatus,
)
from cti_app.domain.publication_document import (
    serialize_publication_document,
)
from tests.test_publication_document_v6 import _base_v5


class _OpinionRepository:
    def __init__(self) -> None:
        self.items: dict[UUID, list[AnalystOpinionV1]] = {}

    async def get(self, opinion_id: UUID, *, version: int | None = None):
        versions = self.items.get(opinion_id, [])
        if version is not None:
            return next((item for item in versions if item.version == version), None)
        return versions[-1] if versions else None

    async def append_version(self, opinion: AnalystOpinionV1, *, expected_version: int):
        versions = self.items.setdefault(opinion.id, [])
        existing = next((item for item in versions if item.version == opinion.version), None)
        if existing is not None:
            if existing == opinion:
                return existing
            raise ValueError("Opinion version is append-only")
        current_version = versions[-1].version if versions else 0
        if current_version != expected_version or opinion.version != expected_version + 1:
            raise VersionConflictError("Analyst opinion version changed")
        versions.append(opinion)
        return opinion


class _FindingRepository:
    def __init__(self, items: tuple[InvestigationFinding, ...]) -> None:
        self.items = items

    async def list_for_investigation(self, investigation_id: UUID):
        return tuple(item for item in self.items if item.investigation_id == investigation_id)


class _ObservationRepository:
    def __init__(self, items: tuple[EvidenceObservation, ...]) -> None:
        self.items = items

    async def list_for_investigation(self, investigation_id: UUID):
        return tuple(item for item in self.items if item.investigation_id == investigation_id)


class _InvestigationRepository:
    def __init__(self, investigation: AnalystInvestigation) -> None:
        self.item = investigation

    async def get(self, investigation_id: UUID):
        return self.item if self.item.id == investigation_id else None


class _DecisionRepository:
    def __init__(self) -> None:
        self.items: list[AnalystDecision] = []

    async def append(self, decision: AnalystDecision) -> None:
        self.items.append(decision)

    async def list_for_investigation(self, investigation_id: UUID):
        return tuple(item for item in self.items if item.investigation_id == investigation_id)


class _EnvelopeRepository:
    def __init__(self, envelope: InvestigationEnvelope) -> None:
        self.item = envelope

    async def get(self, investigation_id: UUID):
        return self.item if self.item.investigation_id == investigation_id else None


class _ArtifactRepository:
    def __init__(self, artifacts: tuple[ProductionArtifact, ...]) -> None:
        self.items = list(artifacts)
        self.appended: list[ProductionArtifact] = []

    async def get(self, artifact_id: UUID):
        return next((item for item in self.items if item.id == artifact_id), None)

    async def get_current_for_revision(self, run_id: UUID, stage: str):
        matches = [
            item
            for item in self.items
            if item.production_run_id == run_id
            and item.stage.value == stage
            and item.status is not ProductionArtifactStatus.STALE
        ]
        return max(matches, key=lambda item: item.version) if matches else None

    async def list_for_run(self, run_id: UUID):
        return [item for item in self.items if item.production_run_id == run_id]

    async def append(self, artifact: ProductionArtifact) -> None:
        self.items.append(artifact)
        self.appended.append(artifact)


class _MemoryStore:
    def __init__(self, blobs: dict[UUID, bytes]) -> None:
        self.blobs = blobs

    async def read_bytes(self, blob_id: UUID) -> bytes:
        return self.blobs[blob_id]

    async def store_stage_payloads(self, *, canonical: dict[str, object]):
        blob_id = uuid4()
        self.blobs[blob_id] = ProductionArtifactStore.canonical_json_bytes(canonical)
        return None, blob_id, None


@dataclass
class OpinionHarness:
    uow: _MemoryUow
    uow_factory: Callable[[], _MemoryUow]
    store: _MemoryStore
    decisions: _DecisionRepository
    observations: _ObservationRepository
    artifacts: _ArtifactRepository
    investigation: AnalystInvestigation
    base_artifact: ProductionArtifact
    base_bytes: bytes
    observation: EvidenceObservation
    finding: InvestigationFinding
    service: AnalystOpinionService


class _MemoryUow:
    def __init__(self, state: OpinionHarnessState) -> None:
        self._state = state
        self.analyst_opinions = state.opinions
        self.analyst_investigations = state.investigations
        self.investigation_findings = state.findings
        self.evidence_observations = state.observations
        self.investigation_envelopes = state.envelopes
        self.analyst_decisions = state.decisions
        self.production_artifacts = state.artifacts
        self.edition_compositions = _WriteProbe(state, "composition_writes")
        self.publication_manifests = _WriteProbe(state, "manifest_writes")

    @property
    def composition_writes(self) -> int:
        return self._state.composition_writes

    @property
    def manifest_writes(self) -> int:
        return self._state.manifest_writes

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def commit(self) -> None:
        return None


class _WriteProbe:
    def __init__(self, state: OpinionHarnessState, counter: str) -> None:
        self._state = state
        self._counter = counter

    def __getattr__(self, name: str):
        async def record_write(*_args: object, **_kwargs: object) -> None:
            setattr(self._state, self._counter, getattr(self._state, self._counter) + 1)

        return record_write


@dataclass
class OpinionHarnessState:
    opinions: _OpinionRepository
    investigations: _InvestigationRepository
    findings: _FindingRepository
    observations: _ObservationRepository
    envelopes: _EnvelopeRepository
    decisions: _DecisionRepository
    artifacts: _ArtifactRepository
    uow: _MemoryUow | None = None
    composition_writes: int = 0
    manifest_writes: int = 0


def make_opinion_harness(*, tlp: TLPLevel = TLPLevel.CLEAR) -> OpinionHarness:
    base = _base_v5()
    base_bytes = ProductionArtifactStore.canonical_json_bytes(serialize_publication_document(base))
    base_blob_id = uuid4()
    synthesis_blob_id = uuid4()
    synthesis_bytes = b"verified harmless synthesis fixture"
    run_id = uuid4()
    investigation_id = uuid4()
    base_artifact = ProductionArtifact(
        id=uuid4(),
        production_run_id=run_id,
        subject_id=base.subject_id,
        stage=ProductionArtifactStage.PUBLICATION,
        version=1,
        input_hash=hashlib.sha256(base_bytes).hexdigest(),
        canonical_blob_id=base_blob_id,
        status=ProductionArtifactStatus.VERIFIED,
    )
    synthesis_artifact = ProductionArtifact(
        id=uuid4(),
        production_run_id=run_id,
        subject_id=base.subject_id,
        stage=ProductionArtifactStage.SYNTHESIS,
        version=1,
        input_hash=hashlib.sha256(synthesis_bytes).hexdigest(),
        canonical_blob_id=synthesis_blob_id,
        status=ProductionArtifactStatus.VERIFIED,
    )
    investigation = AnalystInvestigation(
        id=investigation_id,
        production_run_id=run_id,
        subject_id=base.subject_id,
        synthesis_artifact_id=synthesis_artifact.id,
        budget=LoopBudget(),
    )
    observation = EvidenceObservation(
        investigation_id=investigation_id,
        kind=EvidenceKind.BINARY_ANALYSIS,
        blob_id=uuid4(),
        sha256=hashlib.sha256(b"harmless evidence fixture").hexdigest(),
        source="fixture-analysis",
        source_version="1",
        observed_at=datetime.now(UTC),
        object_type="sha256",
        object_id="a" * 64,
        limitations=("Static observation only.",),
    )
    finding = InvestigationFinding(
        investigation_id=investigation_id,
        kind=FindingKind.INFECTION_CHAIN,
        assessment=FindingAssessment.SUPPORTED,
        statement="The analyzed fixture loads a second-stage component.",
        observation_refs=(observation.id,),
        limitations=("Execution was not observed.",),
    )
    initial_decision = uuid4()
    envelope = InvestigationEnvelope(
        investigation_id=investigation_id,
        allowed_providers=(),
        budget=InvestigationBudget(limits={}),
        acquire_hits_allowed=False,
        disclosure=DisclosureBounds(tlp=tlp, do_not_submit=False),
        approval_decision_id=initial_decision,
        approval_actor_id="editor:fixture",
    )
    state = OpinionHarnessState(
        opinions=_OpinionRepository(),
        investigations=_InvestigationRepository(investigation),
        findings=_FindingRepository((finding,)),
        observations=_ObservationRepository((observation,)),
        envelopes=_EnvelopeRepository(envelope),
        decisions=_DecisionRepository(),
        artifacts=_ArtifactRepository((base_artifact, synthesis_artifact)),
    )
    blobs = {base_blob_id: base_bytes, synthesis_blob_id: synthesis_bytes}
    store = _MemoryStore(blobs)

    def uow_factory() -> _MemoryUow:
        unit = _MemoryUow(state)
        state.uow = unit
        return unit

    service = AnalystOpinionService(
        uow_factory=uow_factory,  # type: ignore[arg-type]
        artifact_store=store,  # type: ignore[arg-type]
    )
    harness = OpinionHarness(
        uow=_MemoryUow(state),
        uow_factory=uow_factory,
        store=store,
        decisions=state.decisions,
        observations=state.observations,
        artifacts=state.artifacts,
        investigation=investigation,
        base_artifact=base_artifact,
        base_bytes=base_bytes,
        observation=observation,
        finding=finding,
        service=service,
    )
    return harness


@pytest.mark.asyncio
async def test_compose_keeps_supported_findings_reservations_and_novelty() -> None:
    harness = make_opinion_harness()
    sample_finding = InvestigationFinding(
        investigation_id=harness.investigation.id,
        kind=FindingKind.NEW_SAMPLE,
        assessment=FindingAssessment.SUPPORTED,
        statement="A related sample is absent from the article snapshot.",
        observation_refs=(harness.observation.id,),
        novelty=NoveltyClass.NEW_TO_ARTICLE,
    )
    harness.uow.investigation_findings.items = (harness.finding, sample_finding)

    opinion = await harness.service.compose_from_findings(
        harness.investigation.id, OpinionCompositionSelection()
    )

    assert opinion.status is AnalystOpinionStatus.DRAFT
    assert {block.block_type for block in opinion.blocks} == {
        OpinionBlockType.KEY_FINDINGS,
        OpinionBlockType.INFECTION_CHAIN_AND_CAPABILITIES,
        OpinionBlockType.ADDITIONAL_SAMPLES,
    }
    sample_block = next(
        block for block in opinion.blocks if block.block_type is OpinionBlockType.ADDITIONAL_SAMPLES
    )
    assert sample_block.finding_refs == (sample_finding.id,)
    assert NoveltyClass.NEW_TO_ARTICLE.value in sample_block.text
    assert all(block.evidence_refs == (harness.observation.id,) for block in opinion.blocks)
    assert any("Réserves" in block.text for block in opinion.blocks)
    view = await harness.service.get_draft(harness.investigation.id)
    assert view.opinion == opinion
    assert view.history == (opinion,)
    assert view.citation_violations == ()


@pytest.mark.asyncio
async def test_revision_rejects_missing_citation_and_stale_version() -> None:
    harness = make_opinion_harness()
    opinion = await harness.service.compose_from_findings(harness.investigation.id)
    block = opinion.blocks[0]
    missing_id = uuid4()
    invalid_block = OpinionBlock(
        block_type=block.block_type,
        text="An unsupported fact.",
        finding_refs=(missing_id,),
        evidence_refs=(harness.observation.id,),
        qualification=OpinionClaimQualification.FACT,
    )

    with pytest.raises(OpinionError) as citation_error:
        await harness.service.revise(
            opinion.id,
            opinion.version,
            OpinionRevisionEdits((invalid_block,)),
            "editor:one",
        )
    assert citation_error.value.code is OpinionErrorCode.CITATIONS_INVALID
    assert citation_error.value.citation_violations

    with pytest.raises(OpinionError) as cas_error:
        await harness.service.revise(
            opinion.id,
            0,
            OpinionRevisionEdits(opinion.blocks, opinion.unresolved_points),
            "editor:one",
        )
    assert cas_error.value.code is OpinionErrorCode.STALE_VERSION


@pytest.mark.asyncio
async def test_approval_requires_exact_draft_hash_and_respects_tlp() -> None:
    harness = make_opinion_harness()
    opinion = await harness.service.compose_from_findings(harness.investigation.id)

    with pytest.raises(OpinionError) as hash_error:
        await harness.service.approve(
            opinion.id,
            opinion.version,
            "0" * 64,
            "editor:one",
            "Approved after review",
        )
    assert hash_error.value.code is OpinionErrorCode.VERSION_HASH_MISMATCH

    approved = await harness.service.approve(
        opinion.id,
        opinion.version,
        opinion.content_sha256,
        "editor:one",
        "Evidence and reservations reviewed",
        correlation_id="corr-approval",
        idempotency_key="approve-once",
    )
    replay = await harness.service.approve(
        opinion.id,
        opinion.version,
        opinion.content_sha256,
        "editor:one",
        "Evidence and reservations reviewed",
        correlation_id="corr-retry",
        idempotency_key="approve-once",
    )
    assert approved.version == opinion.version + 1
    assert approved.status is AnalystOpinionStatus.APPROVED
    assert replay == approved
    assert len(harness.decisions.items) == 1
    assert harness.decisions.items[0].decision_type.value == "note_approve"
    assert harness.decisions.items[0].target_type.value == "note"

    red_harness = make_opinion_harness(tlp=TLPLevel.RED)
    red_draft = await red_harness.service.compose_from_findings(red_harness.investigation.id)
    with pytest.raises(OpinionError) as tlp_error:
        await red_harness.service.approve(
            red_draft.id,
            red_draft.version,
            red_draft.content_sha256,
            "editor:one",
            "Reviewed",
        )
    assert tlp_error.value.code is OpinionErrorCode.TLP_FORBIDDEN

    no_submit_harness = make_opinion_harness()
    no_submit_harness.uow.investigation_envelopes.item = replace(
        no_submit_harness.uow.investigation_envelopes.item,
        disclosure=DisclosureBounds(tlp=TLPLevel.CLEAR, do_not_submit=True),
    )
    no_submit_draft = await no_submit_harness.service.compose_from_findings(
        no_submit_harness.investigation.id
    )
    with pytest.raises(OpinionError) as no_submit_error:
        await no_submit_harness.service.approve(
            no_submit_draft.id,
            no_submit_draft.version,
            no_submit_draft.content_sha256,
            "editor:one",
            "Reviewed",
        )
    assert no_submit_error.value.code is OpinionErrorCode.DO_NOT_SUBMIT


@pytest.mark.asyncio
async def test_claim_verification_uses_the_local_knowledge_provider_protocol() -> None:
    harness = make_opinion_harness()
    claim_finding = InvestigationFinding(
        investigation_id=harness.investigation.id,
        kind=FindingKind.CLAIM_VERIFICATION,
        assessment=FindingAssessment.SUPPORTED,
        statement="The cited loader behavior is present in the analysis.",
        observation_refs=(harness.observation.id,),
        claim_refs=("C1",),
    )
    harness.uow.investigation_findings.items = (claim_finding,)

    class _Provider:
        def __init__(self) -> None:
            self.calls: list[tuple[UUID, str]] = []

        async def knowledge_of_claim(self, investigation_id: UUID, claim_id: str):
            self.calls.append((investigation_id, claim_id))
            return SimpleNamespace(
                claim_id=claim_id,
                claim_statement="The sample uses a second-stage loader.",
                verdict="confirmed",
                current_findings=(claim_finding,),
                favorable_observations=(harness.observation,),
                contradictory_observations=(),
                qualified_observations=(),
                absence_of_evidence=False,
            )

    provider = _Provider()
    service = AnalystOpinionService(
        uow_factory=harness.uow_factory,  # type: ignore[arg-type]
        artifact_store=harness.store,  # type: ignore[arg-type]
        claim_knowledge_provider=provider,
    )
    opinion = await service.compose_from_findings(
        harness.investigation.id,
        OpinionCompositionSelection(block_types=(OpinionBlockType.CLAIM_VERIFICATION,)),
    )

    assert provider.calls == [(harness.investigation.id, "C1")]
    assert len(opinion.blocks) == 1
    assert opinion.blocks[0].qualification is OpinionClaimQualification.FACT
    assert opinion.blocks[0].finding_refs == (claim_finding.id,)
