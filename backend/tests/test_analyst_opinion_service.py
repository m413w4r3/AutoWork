from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from cti_app.application.analyst_opinion_service import (
    AnalystOpinionService,
    OperatorOpinionToolAdapter,
    OpinionCompositionSelection,
    OpinionError,
    OpinionErrorCode,
    OpinionRevisionEdits,
)
from cti_app.application.investigation_persistence import VersionConflictError
from cti_app.application.investigation_tools import ToolExecutionContext
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.domain.analyst_opinion import (
    AnalystObservationRelation,
    AnalystObservationState,
    AnalystObservationV1,
    AnalystOpinionStatus,
    AnalystOpinionV1,
    BridgeDraftReferenceV1,
    BridgeReviewReferenceV1,
    ImplantMaterialEditorialV1,
    ImplantMaterialType,
    OpinionBlock,
    OpinionBlockType,
    OpinionClaimQualification,
    OpinionProtocolFigureV1,
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
    ToolInvocationStatus,
)
from cti_app.domain.production import (
    AnalystInvestigation,
    LoopBudget,
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionArtifactStatus,
)
from cti_app.domain.production_synthesis import production_synthesis_to_json
from cti_app.domain.protocol_model import ProtocolDiagramKind
from cti_app.domain.publication_document import (
    serialize_publication_document,
)
from tests.test_publication_builder_v4 import _canonical_inputs
from tests.test_publication_document_v6 import _base_v5


def test_unstructured_v4_review_remains_attachable_with_real_bridge_provenance() -> None:
    invocation_id = str(uuid4())
    output_sha256 = "a" * 64
    reviewed_text_sha256 = "b" * 64
    input_refs: list[dict[str, str]] = []
    output = {
        "schema": "advisor-response.v4",
        "output_sha256": output_sha256,
        "usable": True,
        "unit_assignment_status": "unstructured_overall",
        "warnings": ["unstructured_overall"],
        "reviews": [
            {
                "unit_id": "U1",
                "remarks": ["Keep the measured claim separate from interpretation."],
                "usable": True,
                "assignment": "unstructured_overall",
            }
        ],
        "bridge_provenance": {
            "bridge_invocation_id": invocation_id,
            "bridge_invocation_id_kind": "conversation_turn",
            "output_sha256": output_sha256,
            "input_refs": input_refs,
        },
    }
    call = {
        "consultation_type": "review_text",
        "advisor_status": "partial",
        "bridge_invocation_id": invocation_id,
        "bridge_invocation_id_kind": "conversation_turn",
        "output_sha256": output_sha256,
        "input_refs": input_refs,
        "review_units": [
            {
                "unit_id": "U1",
                "reviewed_text_sha256": reviewed_text_sha256,
                "form": "body",
                "block_type": "key_findings",
                "anchor": None,
            }
        ],
        "output": output,
    }

    assert AnalystOpinionService._bridge_call_reviews_text(
        call,
        review_ref=BridgeReviewReferenceV1(invocation_id, output_sha256, reviewed_text_sha256),
        text_sha256=reviewed_text_sha256,
        block_type="key_findings",
        anchor=None,
    )


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

    async def read_bytes(self, blob_id: UUID, *, max_bytes: int | None = None) -> bytes:
        value = self.blobs[blob_id]
        if max_bytes is not None and len(value) > max_bytes:
            raise ValueError("blob exceeds requested read bound")
        return value

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
    _, _, _, synthesis = _canonical_inputs()
    synthesis_bytes = ProductionArtifactStore.canonical_json_bytes(
        production_synthesis_to_json(synthesis)
    )
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


@pytest.mark.asyncio
async def test_opinion_revision_accepts_only_persisted_bridge_draft_and_review_refs() -> None:
    harness = make_opinion_harness()
    text = "Le comportement mesuré correspond au finding cité."
    text_sha256 = hashlib.sha256(text.encode()).hexdigest()
    attacker_token = "ghp_" + "a" * 36
    material_text = (
        f"Configuration implant : RC4 00112233445566778899aabbccddeeff; C2 {attacker_token}."
    )
    material_text_sha256 = hashlib.sha256(material_text.encode()).hexdigest()
    draft_invocation_id = str(uuid4())
    review_invocation_id = str(uuid4())
    review_output_sha256 = "d" * 64

    def persist_call(payload: dict[str, object]) -> None:
        input_refs = payload.setdefault("input_refs", [])
        raw_output = payload["output"]
        assert isinstance(raw_output, dict)
        output = dict(raw_output)
        output.setdefault("schema", "advisor-response.v3")
        output["output_sha256"] = payload["output_sha256"]
        output["bridge_provenance"] = {
            "bridge_invocation_id": payload["bridge_invocation_id"],
            "bridge_invocation_id_kind": payload["bridge_invocation_id_kind"],
            "output_sha256": payload["output_sha256"],
            "input_refs": input_refs,
        }
        payload["output"] = output
        raw = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
        blob_id = uuid4()
        harness.store.blobs[blob_id] = raw
        call = EvidenceObservation(
            investigation_id=harness.investigation.id,
            kind=EvidenceKind.OTHER,
            blob_id=blob_id,
            sha256=hashlib.sha256(raw).hexdigest(),
            source="advisor.consult",
            source_version="advisor-consult.v1",
            observed_at=datetime.now(UTC),
            object_type="advisor_call",
            object_id=str(payload["consultation_type"]),
        )
        harness.observations.items = (*harness.observations.items, call)

    draft_output_sha = "c" * 64
    persist_call(
        {
            "schema": "operator-advisor-call.v1",
            "consultation_type": "draft_text",
            "advisor_status": "partial",
            "bridge_invocation_id": draft_invocation_id,
            "bridge_invocation_id_kind": "conversation_turn",
            "output_sha256": draft_output_sha,
            "output": {
                "schema": "advisor-response.v3",
                "usable": False,
                "output_sha256": draft_output_sha,
                "blocks": [
                    {
                        "block_type": "key_findings",
                        "form": "body",
                        "anchor": None,
                        "usable": True,
                        "sentences": [{"text": text, "assertion_ids": ["a1"]}],
                    },
                    {
                        "block_type": "implant_materials",
                        "form": "body",
                        "anchor": None,
                        "usable": True,
                        "sentences": [{"text": material_text, "assertion_ids": ["a2"]}],
                    },
                ],
            },
        }
    )
    persist_call(
        {
            "schema": "operator-advisor-call.v1",
            "consultation_type": "review_text",
            "advisor_status": "partial",
            "bridge_invocation_id": review_invocation_id,
            "bridge_invocation_id_kind": "conversation_turn",
            "output_sha256": review_output_sha256,
            "reviewed_text_sha256": text_sha256,
            "review_target": {
                "form": "body",
                "block_type": "key_findings",
                "anchor": None,
            },
            "review_units": [
                {
                    "unit_id": "U1",
                    "form": "body",
                    "block_type": "key_findings",
                    "anchor": None,
                    "reviewed_text_sha256": text_sha256,
                },
                {
                    "unit_id": "U2",
                    "form": "body",
                    "block_type": "implant_materials",
                    "anchor": None,
                    "reviewed_text_sha256": material_text_sha256,
                },
            ],
            "output": {
                "schema": "advisor-response.v3",
                "usable": False,
                "reviews": [
                    {
                        "unit_id": "U1",
                        "remarks": ["Conserver la distinction entre fait et interprétation."],
                        "usable": True,
                    },
                    {
                        "unit_id": "U2",
                        "remarks": ["Conserver les observables C2 tels qu'observés."],
                        "usable": True,
                    },
                ],
            },
        }
    )
    block = OpinionBlock(
        block_type=OpinionBlockType.KEY_FINDINGS,
        text=text,
        finding_refs=(harness.finding.id,),
        evidence_refs=(harness.observation.id,),
        qualification=OpinionClaimQualification.FACT,
        certainty=0.8,
        bridge_authored=True,
        bridge_draft_ref=BridgeDraftReferenceV1(draft_invocation_id, text_sha256),
        bridge_review_ref=BridgeReviewReferenceV1(
            review_invocation_id, review_output_sha256, text_sha256
        ),
    )

    composed_with_bridge = await harness.service.compose_from_findings(
        harness.investigation.id,
        OpinionCompositionSelection(),
        draft_blocks=(block,),
    )
    assert composed_with_bridge.blocks[0].bridge_draft_ref == block.bridge_draft_ref
    assert composed_with_bridge.blocks[0].bridge_review_ref == block.bridge_review_ref

    material_section = OpinionBlock(
        block_type=OpinionBlockType.IMPLANT_MATERIALS,
        text=material_text,
        bridge_authored=True,
        bridge_draft_ref=BridgeDraftReferenceV1(draft_invocation_id, material_text_sha256),
        bridge_review_ref=BridgeReviewReferenceV1(
            review_invocation_id, review_output_sha256, material_text_sha256
        ),
    )
    material = ImplantMaterialEditorialV1(
        type=ImplantMaterialType.ATTACKER_GITHUB_C2_TOKEN,
        value=attacker_token,
        role="Canal C2 de l'implant",
        sample_location="offset 0x120",
        sample_sha256="a" * 64,
        evidence_observation_id=harness.observation.id,
    )
    revised = await harness.service.revise(
        composed_with_bridge.id,
        composed_with_bridge.version,
        OpinionRevisionEdits(
            (block, material_section),
            implant_materials=(material,),
        ),
        "editor:one",
        expected_sha256=composed_with_bridge.content_sha256,
    )
    assert revised.blocks[0].bridge_authored is True
    assert revised.blocks[0].bridge_draft_ref == block.bridge_draft_ref
    assert revised.blocks[0].bridge_review_ref == block.bridge_review_ref
    assert revised.implant_materials == (material,)
    assert revised.implant_materials[0].value == attacker_token
    assert revised.implant_materials_section is not None
    assert revised.implant_materials_section.text == material_text
    assert revised.implant_materials_section.bridge_draft_ref == material_section.bridge_draft_ref
    assert revised.implant_materials_section.bridge_review_ref == material_section.bridge_review_ref


@pytest.mark.asyncio
async def test_operator_compose_and_revise_persist_typed_implant_materials() -> None:
    harness = make_opinion_harness()
    adapter = OperatorOpinionToolAdapter(
        service=harness.service,
        investigations=SimpleNamespace(),
        article_snapshots=SimpleNamespace(),
    )
    tool_context = ToolExecutionContext(
        investigation_id=harness.investigation.id,
        envelope=None,
        budget=InvestigationBudget(limits={}),
        idempotency_key="operator-opinion-materials",
    )
    ordinary_block = {
        "block_type": "key_findings",
        "text": "The fixture has a supported finding.",
        "finding_refs": [str(harness.finding.id)],
        "evidence_refs": [str(harness.observation.id)],
        "qualification": "fact",
        "certainty": 0.9,
    }
    material_block = {
        "block_type": "implant_materials",
        "text": "An attacker-controlled C2 token was recovered from the sample.",
        "finding_refs": [],
        "evidence_refs": [],
        "qualification": "fact",
        "certainty": 1.0,
    }
    initial_row = {
        "type": "attacker_github_c2_token",
        "value": "ghp_" + "a" * 36,
        "role": "C2 token",
        "sample_location": "offset 0x120",
        "sample_sha256": "a" * 64,
        "evidence_observation_id": str(harness.observation.id),
    }
    compose_arguments = {
        "action": "compose",
        "selection": {"finding_ids": [], "claim_ids": [], "block_types": []},
        "blocks": [ordinary_block, material_block],
        "implant_materials": [initial_row],
    }

    composed_result = await adapter.execute(compose_arguments, tool_context)
    assert composed_result.status is ToolInvocationStatus.SUCCEEDED
    composed = (await harness.service.get_draft(harness.investigation.id)).opinion
    assert composed is not None
    assert composed.implant_materials[0].value == initial_row["value"]
    assert composed.implant_materials_section is not None
    assert composed.implant_materials_section.text == material_block["text"]
    assert any(
        block.block_type is OpinionBlockType.KEY_FINDINGS and block.text == ordinary_block["text"]
        for block in composed.blocks
    )
    assert composed.content_sha256 == composed_result.output["content_sha256"]

    repeated_compose = await adapter.execute(compose_arguments, tool_context)
    assert repeated_compose.status is ToolInvocationStatus.SUCCEEDED
    assert repeated_compose.output["opinion_version"] == composed.version
    assert repeated_compose.output["content_sha256"] == composed.content_sha256

    revised_row = {**initial_row, "value": "ghp_" + "b" * 36}
    revise_result = await adapter.execute(
        {
            "action": "revise",
            "opinion_id": str(composed.id),
            "expected_opinion_version": composed.version,
            "expected_opinion_sha256": composed.content_sha256,
            "blocks": [ordinary_block, material_block],
            "unresolved_points": [],
            "implant_materials": [revised_row],
        },
        tool_context,
    )
    assert revise_result.status is ToolInvocationStatus.SUCCEEDED
    revised = (await harness.service.get_draft(harness.investigation.id)).opinion
    assert revised is not None
    assert revised.version == composed.version + 1
    assert revised.content_sha256 != composed.content_sha256
    assert revised.implant_materials[0].value == revised_row["value"]
    assert revised.implant_materials_section is not None
    assert revised.implant_materials_section.text == material_block["text"]
    assert any(
        block.block_type is OpinionBlockType.KEY_FINDINGS and block.text == ordinary_block["text"]
        for block in revised.blocks
    )

    invalid_citation = await adapter.execute(
        {
            "action": "revise",
            "opinion_id": str(revised.id),
            "expected_opinion_version": revised.version,
            "expected_opinion_sha256": revised.content_sha256,
            "blocks": [ordinary_block, material_block],
            "unresolved_points": [],
            "implant_materials": [{**revised_row, "evidence_observation_id": str(uuid4())}],
        },
        tool_context,
    )
    assert invalid_citation.status is ToolInvocationStatus.FAILED
    assert invalid_citation.error_code == "citations_invalid"
    assert invalid_citation.output["field"] == "implant_materials[0].evidence_observation_id"


@pytest.mark.asyncio
async def test_protocol_figures_empty_list_detaches_but_none_preserves() -> None:
    harness = make_opinion_harness()
    adapter = OperatorOpinionToolAdapter(
        service=harness.service,
        investigations=SimpleNamespace(),
        article_snapshots=SimpleNamespace(),
    )
    figures = tuple(
        OpinionProtocolFigureV1(
            kind=kind,
            model_sha256="a" * 64,
            spec_sha256="b" * 64,
            asset_id=uuid4(),
            asset_sha256="c" * 64,
            preview_sha256="d" * 64,
            pin_sha256="e" * 64,
            compiler_name="d2",
            compiler_version="0.6.9",
            compiler_policy_version="protocol-diagram.v1",
            caption=f"Diagramme synthétique {kind.value}.",
        )
        for kind in ProtocolDiagramKind
    )
    opinion = await harness.service.compose_from_findings(
        harness.investigation.id,
        OpinionCompositionSelection(),
        protocol_figures=figures,
        draft_blocks=(),
    )

    unchanged, unchanged_observations = await adapter._protocol_figures(
        harness.investigation.id, None
    )
    unchanged_opinion = await harness.service.revise(
        opinion.id,
        opinion.version,
        OpinionRevisionEdits(blocks=opinion.blocks, protocol_figures=unchanged),
        "editor:test",
        expected_sha256=opinion.content_sha256,
    )
    detached, detached_observations = await adapter._protocol_figures(harness.investigation.id, [])
    detached_opinion = await harness.service.revise(
        unchanged_opinion.id,
        unchanged_opinion.version,
        OpinionRevisionEdits(
            blocks=unchanged_opinion.blocks,
            protocol_figures=detached,
        ),
        "editor:test",
        expected_sha256=unchanged_opinion.content_sha256,
    )

    assert unchanged is None
    assert unchanged_observations == ()
    assert detached == ()
    assert detached_observations == ()
    assert unchanged_opinion.protocol_figures == figures
    assert detached_opinion.protocol_figures == ()


@pytest.mark.asyncio
async def test_compose_and_revise_accept_per_unit_refs_from_batched_advisor_calls() -> None:
    harness = make_opinion_harness()
    draft_invocation_id = str(uuid4())
    review_invocation_id = str(uuid4())
    draft_output_sha = "c" * 64
    review_output_sha = "d" * 64
    body_text = "Le comportement mesuré apparaît dans le premier bloc."
    note_text = "La réserve de portée accompagne le paragraphe cité."
    anchor = "lead:0001"
    body_sha = hashlib.sha256(body_text.encode()).hexdigest()
    note_sha = hashlib.sha256(note_text.encode()).hexdigest()

    def persist_call(payload: dict[str, object]) -> None:
        input_refs = payload.setdefault("input_refs", [])
        raw_output = payload["output"]
        assert isinstance(raw_output, dict)
        output = dict(raw_output)
        output.setdefault("schema", "advisor-response.v3")
        output["output_sha256"] = payload["output_sha256"]
        output["bridge_provenance"] = {
            "bridge_invocation_id": payload["bridge_invocation_id"],
            "bridge_invocation_id_kind": payload["bridge_invocation_id_kind"],
            "output_sha256": payload["output_sha256"],
            "input_refs": input_refs,
        }
        payload["output"] = output
        raw = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
        blob_id = uuid4()
        harness.store.blobs[blob_id] = raw
        call = EvidenceObservation(
            investigation_id=harness.investigation.id,
            kind=EvidenceKind.OTHER,
            blob_id=blob_id,
            sha256=hashlib.sha256(raw).hexdigest(),
            source="advisor.consult",
            source_version="advisor-consult.v1",
            observed_at=datetime.now(UTC),
            object_type="advisor_call",
            object_id=str(payload["consultation_type"]),
        )
        harness.observations.items = (*harness.observations.items, call)

    persist_call(
        {
            "schema": "operator-advisor-call.v1",
            "consultation_type": "draft_text",
            "advisor_status": "partial",
            "bridge_invocation_id": draft_invocation_id,
            "bridge_invocation_id_kind": "conversation_turn",
            "output_sha256": draft_output_sha,
            "output": {
                "schema": "advisor-response.v3",
                "usable": False,
                "output_sha256": draft_output_sha,
                "blocks": [
                    {
                        "unit_id": "U1",
                        "block_type": "key_findings",
                        "form": "body",
                        "anchor": None,
                        "usable": True,
                        "sentences": [{"text": body_text, "assertion_ids": ["a1"]}],
                    },
                    {
                        "unit_id": "U2",
                        "block_type": "claim_verification",
                        "form": "analyst_note",
                        "anchor": anchor,
                        "usable": True,
                        "sentences": [{"text": note_text, "assertion_ids": ["a2"]}],
                    },
                ],
            },
        }
    )
    persist_call(
        {
            "schema": "operator-advisor-call.v1",
            "consultation_type": "review_text",
            "advisor_status": "partial",
            "bridge_invocation_id": review_invocation_id,
            "bridge_invocation_id_kind": "conversation_turn",
            "output_sha256": review_output_sha,
            "review_units": [
                {
                    "unit_id": "U1",
                    "form": "body",
                    "block_type": "key_findings",
                    "anchor": None,
                    "reviewed_text_sha256": body_sha,
                },
                {
                    "unit_id": "U2",
                    "form": "analyst_note",
                    "block_type": "claim_verification",
                    "anchor": anchor,
                    "reviewed_text_sha256": note_sha,
                },
            ],
            "output": {
                "schema": "advisor-response.v3",
                "usable": False,
                "reviews": [
                    {
                        "unit_id": "U1",
                        "remarks": ["Conserver la distinction fait/interprétation."],
                        "usable": True,
                    },
                    {
                        "unit_id": "U2",
                        "remarks": ["Préciser la limite de portée."],
                        "usable": True,
                    },
                ],
            },
        }
    )
    body = OpinionBlock(
        block_type=OpinionBlockType.KEY_FINDINGS,
        text=body_text,
        finding_refs=(harness.finding.id,),
        evidence_refs=(harness.observation.id,),
        qualification=OpinionClaimQualification.FACT,
        certainty=0.8,
        bridge_authored=True,
        bridge_draft_ref=BridgeDraftReferenceV1(draft_invocation_id, body_sha),
        bridge_review_ref=BridgeReviewReferenceV1(
            review_invocation_id, review_output_sha, body_sha
        ),
    )
    note = AnalystObservationV1(
        id=uuid4(),
        anchor=anchor,
        text=note_text,
        state=AnalystObservationState.OBSERVED,
        relation=AnalystObservationRelation.QUALIFIES,
        finding_refs=(harness.finding.id,),
        evidence_refs=(harness.observation.id,),
        bridge_authored=True,
        bridge_block_type=OpinionBlockType.CLAIM_VERIFICATION,
        bridge_draft_ref=BridgeDraftReferenceV1(draft_invocation_id, note_sha),
        bridge_review_ref=BridgeReviewReferenceV1(
            review_invocation_id, review_output_sha, note_sha
        ),
    )

    composed = await harness.service.compose_from_findings(
        harness.investigation.id,
        OpinionCompositionSelection(),
        draft_blocks=(body,),
    )
    revised = await harness.service.revise(
        composed.id,
        composed.version,
        OpinionRevisionEdits(composed.blocks, observations=(note,)),
        "editor:one",
        expected_sha256=composed.content_sha256,
    )

    assert revised.blocks[0].bridge_draft_ref == body.bridge_draft_ref
    assert revised.blocks[0].bridge_review_ref == body.bridge_review_ref
    assert revised.observations[0].bridge_draft_ref == note.bridge_draft_ref
    assert revised.observations[0].bridge_review_ref == note.bridge_review_ref


@pytest.mark.asyncio
async def test_opinion_revision_rejects_unpersisted_bridge_invocation() -> None:
    harness = make_opinion_harness()
    current = await harness.service.compose_from_findings(harness.investigation.id)
    text = "Le finding mesuré est repris dans ce brouillon."
    block = OpinionBlock(
        block_type=OpinionBlockType.KEY_FINDINGS,
        text=text,
        finding_refs=(harness.finding.id,),
        evidence_refs=(harness.observation.id,),
        qualification=OpinionClaimQualification.FACT,
        bridge_authored=True,
        bridge_draft_ref=BridgeDraftReferenceV1(
            str(uuid4()), hashlib.sha256(text.encode()).hexdigest()
        ),
    )

    with pytest.raises(OpinionError) as error:
        await harness.service.revise(
            current.id,
            current.version,
            OpinionRevisionEdits((block,)),
            "editor:one",
            expected_sha256=current.content_sha256,
        )

    assert error.value.code is OpinionErrorCode.INVALID_SELECTION


@pytest.mark.asyncio
async def test_service_rejects_review_sha_from_an_older_target_as_review_text_stale() -> None:
    harness = make_opinion_harness()
    text = "Texte de la version courante."
    text_sha256 = hashlib.sha256(text.encode("utf-8")).hexdigest()
    block = SimpleNamespace(
        block_type=OpinionBlockType.CLAIM_VERIFICATION,
        text=text,
        bridge_authored=True,
        bridge_draft_ref=SimpleNamespace(text_sha256=text_sha256),
        bridge_review_ref=SimpleNamespace(reviewed_text_sha256="b" * 64),
    )
    opinion = SimpleNamespace(
        investigation_id=harness.investigation.id,
        blocks=(block,),
        observations=(),
        implant_materials_section=None,
    )

    class EmptyObservationRepository:
        async def list_for_investigation(self, _investigation_id):
            return ()

    uow = SimpleNamespace(evidence_observations=EmptyObservationRepository())
    with pytest.raises(OpinionError) as error:
        await harness.service._validate_bridge_provenance(uow, opinion)

    assert error.value.code is OpinionErrorCode.REVIEW_TEXT_STALE


@pytest.mark.asyncio
async def test_opinion_revision_rejects_call_without_nested_bridge_provenance() -> None:
    harness = make_opinion_harness()
    current = await harness.service.compose_from_findings(harness.investigation.id)
    text = "Le constat conserve son lien avec la mesure observée."
    invocation_id = str(uuid4())
    text_sha256 = hashlib.sha256(text.encode()).hexdigest()
    output_sha256 = "f" * 64
    payload = {
        "schema": "operator-advisor-call.v1",
        "consultation_type": "draft_text",
        "advisor_status": "partial",
        "bridge_invocation_id": invocation_id,
        "bridge_invocation_id_kind": "conversation_turn",
        "input_refs": [],
        "output_sha256": output_sha256,
        "output": {
            "schema": "advisor-response.v3",
            "usable": False,
            "output_sha256": output_sha256,
            "blocks": [
                {
                    "block_type": "key_findings",
                    "form": "body",
                    "anchor": None,
                    "usable": True,
                    "sentences": [{"text": text, "assertion_ids": ["a1"]}],
                }
            ],
        },
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    blob_id = uuid4()
    harness.store.blobs[blob_id] = raw
    call = EvidenceObservation(
        investigation_id=harness.investigation.id,
        kind=EvidenceKind.OTHER,
        blob_id=blob_id,
        sha256=hashlib.sha256(raw).hexdigest(),
        source="advisor.consult",
        source_version="advisor-consult.v1",
        observed_at=datetime.now(UTC),
        object_type="advisor_call",
        object_id="draft_text",
    )
    harness.observations.items = (*harness.observations.items, call)
    block = OpinionBlock(
        block_type=OpinionBlockType.KEY_FINDINGS,
        text=text,
        finding_refs=(harness.finding.id,),
        evidence_refs=(harness.observation.id,),
        qualification=OpinionClaimQualification.FACT,
        bridge_authored=True,
        bridge_draft_ref=BridgeDraftReferenceV1(invocation_id, text_sha256),
    )

    with pytest.raises(OpinionError) as error:
        await harness.service.revise(
            current.id,
            current.version,
            OpinionRevisionEdits((block,)),
            "editor:one",
            expected_sha256=current.content_sha256,
        )

    assert error.value.code is OpinionErrorCode.INVALID_SELECTION
