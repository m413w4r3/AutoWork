"""ADR-0011 D4/E1: Bridge-only prose authority and advisory-observation exclusion."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest

from cti_app.application.analyst_opinion_preview import AnalystOpinionPreviewService
from cti_app.application.analyst_opinion_service import (
    OperatorOpinionToolAdapter,
    OpinionCompositionSelection,
    OpinionError,
    OpinionErrorCode,
    OpinionRevisionEdits,
)
from cti_app.application.investigation_contracts import FindingProposalV1
from cti_app.application.investigation_findings import FindingService
from cti_app.application.investigation_service import InvestigationService
from cti_app.application.investigation_tools import ToolExecutionContext
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.application.typst_compilation import CompiledTypstDocument
from cti_app.domain.analyst_opinion import (
    AnalystObservationRelation,
    AnalystObservationState,
    AnalystObservationV1,
    AnalystOpinionStatus,
    AnalystOpinionV1,
    BridgeDraftReferenceV1,
    CitationViolationCode,
    ImplantMaterialEditorialV1,
    ImplantMaterialType,
    OpinionBlock,
    OpinionBlockType,
    OpinionClaimQualification,
    OpinionWriterKind,
    investigation_opinion_id,
    verify_opinion_citations,
)
from cti_app.domain.investigation import (
    EvidenceKind,
    EvidenceObservation,
    FindingAssessment,
    FindingKind,
    InvestigationBudget,
    is_advisory_observation,
)
from cti_app.domain.production import (
    AnalystInvestigation,
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionArtifactStatus,
    ProseAuthority,
)
from cti_app.domain.publication_document import (
    publication_document_v5_from_json,
    serialize_publication_document,
)
from tests.test_analyst_opinion_service import OpinionHarness, make_opinion_harness


class _InvestigationRepository:
    def __init__(self, item: AnalystInvestigation) -> None:
        self.item = item

    async def get(self, investigation_id: UUID) -> AnalystInvestigation | None:
        return self.item if self.item.id == investigation_id else None


class _ObservationRepository:
    def __init__(self, items: tuple[EvidenceObservation, ...]) -> None:
        self.items = items

    async def get(self, observation_id: UUID) -> EvidenceObservation | None:
        return next((item for item in self.items if item.id == observation_id), None)

    async def list_for_investigation(
        self, investigation_id: UUID
    ) -> tuple[EvidenceObservation, ...]:
        return tuple(item for item in self.items if item.investigation_id == investigation_id)


class _FindingRepository:
    def __init__(self, items: tuple[Any, ...] = ()) -> None:
        self.items = list(items)

    async def get(self, finding_id: UUID) -> Any | None:
        return next((item for item in self.items if item.id == finding_id), None)

    async def list_for_investigation(self, investigation_id: UUID) -> tuple[Any, ...]:
        return tuple(item for item in self.items if item.investigation_id == investigation_id)

    async def append_version(self, finding: Any, *, expected_previous_id: UUID | None) -> Any:
        assert expected_previous_id is None
        existing = await self.get(finding.id)
        if existing is not None:
            return existing
        self.items.append(finding)
        return finding


class _ArticleSnapshots:
    async def resolve(self, synthesis_artifact_id: UUID) -> Any:
        del synthesis_artifact_id
        return SimpleNamespace(article=SimpleNamespace(claims=()))


def _bridge_only(harness: OpinionHarness) -> AnalystInvestigation:
    investigation = replace(harness.investigation, prose_authority=ProseAuthority.BRIDGE_ONLY)
    harness.uow.analyst_investigations.item = investigation
    return investigation


def _synthesis_sha256(harness: OpinionHarness) -> tuple[UUID, str]:
    synthesis = next(
        item for item in harness.artifacts.items if item.stage is ProductionArtifactStage.SYNTHESIS
    )
    assert synthesis.canonical_blob_id is not None
    return synthesis.id, hashlib.sha256(
        harness.store.blobs[synthesis.canonical_blob_id]
    ).hexdigest()


def _seed_opinion(
    harness: OpinionHarness,
    blocks: tuple[OpinionBlock, ...],
    *,
    status: AnalystOpinionStatus = AnalystOpinionStatus.DRAFT,
    unresolved_points: tuple[str, ...] = (),
) -> AnalystOpinionV1:
    synthesis_id, synthesis_sha256 = _synthesis_sha256(harness)
    opinion = AnalystOpinionV1(
        id=investigation_opinion_id(harness.investigation.id),
        version=1,
        investigation_id=harness.investigation.id,
        synthesis_artifact_id=synthesis_id,
        synthesis_sha256=synthesis_sha256,
        base_publication_artifact_id=harness.base_artifact.id,
        base_publication_sha256=harness.base_artifact.input_hash,
        blocks=blocks,
        unresolved_points=unresolved_points,
        author_id="editor:seed",
        status=status,
        reviewer_id="editor:reviewer" if status is not AnalystOpinionStatus.DRAFT else None,
    )
    harness.uow.analyst_opinions.items[opinion.id] = [opinion]
    return opinion


def _persist_draft_call(
    harness: OpinionHarness,
    text: str,
    *,
    block_type: str = OpinionBlockType.KEY_FINDINGS.value,
    anchor: str | None = None,
    source_text: str | None = None,
) -> tuple[str, EvidenceObservation]:
    invocation_id = str(uuid4())
    output_sha256 = hashlib.sha256(f"bridge-output:{invocation_id}".encode()).hexdigest()
    output = {
        "schema": "advisor-response.v3",
        "usable": True,
        "output_sha256": output_sha256,
        "blocks": [
            {
                "block_type": block_type,
                "form": "analyst_note" if anchor is not None else "body",
                "anchor": anchor,
                "usable": True,
                "sentences": [{"text": source_text if source_text is not None else text}],
            }
        ],
        "bridge_provenance": {
            "bridge_invocation_id": invocation_id,
            "bridge_invocation_id_kind": "conversation_turn",
            "output_sha256": output_sha256,
            "input_refs": [],
        },
    }
    payload = {
        "schema": "operator-advisor-call.v1",
        "consultation_type": "draft_text",
        "advisor_status": "succeeded",
        "bridge_invocation_id": invocation_id,
        "bridge_invocation_id_kind": "conversation_turn",
        "output_sha256": output_sha256,
        "input_refs": [],
        "output": output,
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    blob_id = uuid4()
    harness.store.blobs[blob_id] = raw
    observation = EvidenceObservation(
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
    harness.observations.items = (*harness.observations.items, observation)
    return invocation_id, observation


def _bridge_block(
    harness: OpinionHarness,
    text: str,
    *,
    block_type: OpinionBlockType = OpinionBlockType.KEY_FINDINGS,
    qualification: OpinionClaimQualification = OpinionClaimQualification.FACT,
    anchor: str | None = None,
    modified_from: str | None = None,
) -> OpinionBlock:
    invocation_id, observation = _persist_draft_call(
        harness,
        text,
        block_type=block_type.value,
        anchor=anchor,
        source_text=modified_from,
    )
    return OpinionBlock(
        block_type=block_type,
        text=text,
        finding_refs=(harness.finding.id,),
        evidence_refs=(harness.observation.id,),
        qualification=qualification,
        certainty=0.8,
        bridge_draft_ref=BridgeDraftReferenceV1(
            invocation_id,
            hashlib.sha256(text.encode()).hexdigest(),
            (
                hashlib.sha256(modified_from.encode()).hexdigest()
                if modified_from is not None
                else None
            ),
            observation.id,
        ),
    )


def _plain_block(harness: OpinionHarness, text: str) -> OpinionBlock:
    return OpinionBlock(
        block_type=OpinionBlockType.KEY_FINDINGS,
        text=text,
        finding_refs=(harness.finding.id,),
        evidence_refs=(harness.observation.id,),
        qualification=OpinionClaimQualification.FACT,
        certainty=0.8,
    )


def _advisory_observation(harness: OpinionHarness) -> EvidenceObservation:
    return EvidenceObservation(
        investigation_id=harness.investigation.id,
        kind=EvidenceKind.OTHER,
        blob_id=uuid4(),
        sha256=hashlib.sha256(b"bridge advisory fixture").hexdigest(),
        source="advisor.consult",
        source_version="advisor-consult.v1",
        observed_at=datetime.now(UTC),
        object_type="advisor_call",
        object_id="draft_text",
    )


def _tool_context(investigation_id: UUID, *, actor_id: str) -> ToolExecutionContext:
    return ToolExecutionContext(
        investigation_id=investigation_id,
        envelope=None,
        budget=InvestigationBudget(limits={}),
        idempotency_key=f"authority-test:{uuid4()}",
        actor_id=actor_id,
    )


def _operator_adapter(harness: OpinionHarness) -> OperatorOpinionToolAdapter:
    return OperatorOpinionToolAdapter(
        service=harness.service,
        investigations=harness.uow.analyst_investigations,
        article_snapshots=SimpleNamespace(),
    )


class _NoGraph:
    async def project(self, **_kwargs: Any) -> None:
        return None


class _RecordingDraftRenderer:
    """Deterministic PDF fixture: the preview contract and citations stay real."""

    def __init__(self) -> None:
        self.calls = 0
        self.opinion: AnalystOpinionV1 | None = None

    async def render_draft_preview(
        self,
        document: Any,
        opinion: AnalystOpinionV1,
        synthesis: Any,
        **_kwargs: Any,
    ) -> CompiledTypstDocument:
        del document, synthesis
        self.calls += 1
        self.opinion = opinion
        content = b"%PDF-1.7\nfixture opinion preview\n"
        return CompiledTypstDocument(
            media_type="application/pdf",
            content=content,
            sha256=hashlib.sha256(content).hexdigest(),
            byte_size=len(content),
            compiler="fixture",
            compiler_version="1",
        )


def _dummy_investigation_service(
    investigation: AnalystInvestigation,
    *,
    observations_repository: Any | None = None,
    findings_repository: Any | None = None,
    context_builder: Any | None = None,
) -> InvestigationService:
    return InvestigationService(
        post_synthesis=SimpleNamespace(),
        investigations=_InvestigationRepository(investigation),
        states=SimpleNamespace(),
        context_snapshots=SimpleNamespace(),
        envelopes=SimpleNamespace(),
        context_builder=context_builder or SimpleNamespace(),
        bridge=SimpleNamespace(),
        registry=SimpleNamespace(),
        dispatcher=SimpleNamespace(),
        invocations=SimpleNamespace(),
        observations=observations_repository or SimpleNamespace(),
        findings=findings_repository or SimpleNamespace(),
        opinions=SimpleNamespace(),
    )


@pytest.mark.asyncio
async def test_bridge_only_compose_refuses_deterministic_prose() -> None:
    harness = make_opinion_harness()
    investigation = _bridge_only(harness)

    with pytest.raises(OpinionError) as refused:
        await harness.service.compose_from_findings(investigation.id)

    assert refused.value.code is OpinionErrorCode.BRIDGE_AUTHORSHIP_REQUIRED
    assert harness.uow.analyst_opinions.items == {}


@pytest.mark.asyncio
async def test_bridge_only_revise_refuses_non_bridge_prose() -> None:
    harness = make_opinion_harness()
    investigation = _bridge_only(harness)
    draft = await harness.service.compose_from_findings(
        investigation.id,
        OpinionCompositionSelection(),
        draft_blocks=(_bridge_block(harness, "Texte rédigé par Bridge, cité au finding."),),
    )

    with pytest.raises(OpinionError) as refused:
        await harness.service.revise(
            draft.id,
            draft.version,
            OpinionRevisionEdits((_plain_block(harness, "Réécriture humaine non prouvée."),)),
            "editor:human",
            expected_sha256=draft.content_sha256,
        )

    assert refused.value.code is OpinionErrorCode.BRIDGE_AUTHORSHIP_REQUIRED


@pytest.mark.asyncio
async def test_bridge_only_rejects_modified_bridge_prose() -> None:
    harness = make_opinion_harness()
    investigation = _bridge_only(harness)
    draft = await harness.service.compose_from_findings(
        investigation.id,
        OpinionCompositionSelection(),
        draft_blocks=(_bridge_block(harness, "Texte Bridge d'origine."),),
    )
    modified = _bridge_block(
        harness,
        "Texte Bridge retouché par un humain.",
        modified_from="Texte Bridge d'origine.",
    )

    with pytest.raises(OpinionError) as refused:
        await harness.service.revise(
            draft.id,
            draft.version,
            OpinionRevisionEdits((modified,)),
            "editor:human",
            expected_sha256=draft.content_sha256,
        )

    assert refused.value.code is OpinionErrorCode.BRIDGE_MODIFIED_PROSE_FORBIDDEN


@pytest.mark.asyncio
async def test_bridge_only_notes_require_bridge_provenance() -> None:
    harness = make_opinion_harness()
    investigation = _bridge_only(harness)
    block = _bridge_block(harness, "Texte Bridge cité.")
    draft = await harness.service.compose_from_findings(
        investigation.id,
        OpinionCompositionSelection(),
        draft_blocks=(block,),
    )
    anchor = "lead:0001"
    plain_note = AnalystObservationV1(
        id=uuid4(),
        anchor=anchor,
        text="Note humaine sans provenance Bridge.",
        state=AnalystObservationState.UNRESOLVED,
        relation=AnalystObservationRelation.UNRESOLVED,
    )

    with pytest.raises(OpinionError) as refused:
        await harness.service.revise(
            draft.id,
            draft.version,
            OpinionRevisionEdits((block,), observations=(plain_note,)),
            "editor:human",
            expected_sha256=draft.content_sha256,
        )
    assert refused.value.code is OpinionErrorCode.BRIDGE_AUTHORSHIP_REQUIRED

    note_text = "Note de l'analyste rédigée par Bridge."
    invocation_id, observation = _persist_draft_call(
        harness,
        note_text,
        block_type=OpinionBlockType.KEY_FINDINGS.value,
        anchor=anchor,
    )
    bridge_note = AnalystObservationV1(
        id=uuid4(),
        anchor=anchor,
        text=note_text,
        state=AnalystObservationState.UNRESOLVED,
        relation=AnalystObservationRelation.UNRESOLVED,
        bridge_block_type=OpinionBlockType.KEY_FINDINGS,
        bridge_draft_ref=BridgeDraftReferenceV1(
            invocation_id,
            hashlib.sha256(note_text.encode()).hexdigest(),
            None,
            observation.id,
        ),
    )
    revised = await harness.service.revise(
        draft.id,
        draft.version,
        OpinionRevisionEdits((block,), observations=(bridge_note,)),
        "editor:human",
        expected_sha256=draft.content_sha256,
    )
    assert revised.observations == (bridge_note,)


@pytest.mark.asyncio
async def test_bridge_only_revise_refuses_unresolved_points() -> None:
    harness = make_opinion_harness()
    investigation = _bridge_only(harness)
    block = _bridge_block(harness, "Texte Bridge cité.")
    draft = await harness.service.compose_from_findings(
        investigation.id,
        OpinionCompositionSelection(),
        draft_blocks=(block,),
    )

    with pytest.raises(OpinionError) as refused:
        await harness.service.revise(
            draft.id,
            draft.version,
            OpinionRevisionEdits((block,), unresolved_points=("Limite non prouvée par Bridge.",)),
            "editor:human",
            expected_sha256=draft.content_sha256,
        )

    assert refused.value.code is OpinionErrorCode.BRIDGE_PROSE_FORBIDDEN


@pytest.mark.asyncio
async def test_bridge_only_refuses_implant_editorial_text() -> None:
    harness = make_opinion_harness()
    investigation = _bridge_only(harness)
    block = _bridge_block(harness, "Texte Bridge cité.")
    draft = await harness.service.compose_from_findings(
        investigation.id,
        OpinionCompositionSelection(),
        draft_blocks=(block,),
    )
    editorial_text = "Matériaux d'implant rédigés sans provenance Bridge."
    material_section = OpinionBlock(
        block_type=OpinionBlockType.IMPLANT_MATERIALS,
        text=editorial_text,
    )
    material = ImplantMaterialEditorialV1(
        type=ImplantMaterialType.RC4_KEY,
        value="00112233445566778899aabbccddeeff",
        role="Clé RC4 de l'implant",
        sample_location="offset 0x120",
        sample_sha256="a" * 64,
        evidence_observation_id=harness.observation.id,
    )

    with pytest.raises(OpinionError) as refused:
        await harness.service.revise(
            draft.id,
            draft.version,
            OpinionRevisionEdits(
                (block, material_section),
                implant_materials=(material,),
            ),
            "editor:human",
            expected_sha256=draft.content_sha256,
        )

    assert refused.value.code is OpinionErrorCode.BRIDGE_AUTHORSHIP_REQUIRED


def _prepare_rebase(harness: OpinionHarness, opinion: AnalystOpinionV1) -> tuple[Any, str]:
    base_document = publication_document_v5_from_json(json.loads(harness.base_bytes))
    same_text_document = replace(
        base_document,
        semantic_text=replace(
            base_document.semantic_text,
            policy_version=(
                "semantic-annotation-policy-v5-technical-literal-globs-exact-occurrence-coverage"
            ),
        ),
    )
    corrected_bytes = ProductionArtifactStore.canonical_json_bytes(
        serialize_publication_document(same_text_document)
    )
    corrected_sha256 = hashlib.sha256(corrected_bytes).hexdigest()
    corrected_blob_id = uuid4()
    harness.store.blobs[corrected_blob_id] = corrected_bytes
    corrected = ProductionArtifact(
        id=uuid4(),
        production_run_id=harness.base_artifact.production_run_id,
        subject_id=harness.base_artifact.subject_id,
        stage=ProductionArtifactStage.PUBLICATION,
        version=2,
        input_hash=hashlib.sha256(b"manual correction input").hexdigest(),
        canonical_blob_id=corrected_blob_id,
        status=ProductionArtifactStatus.VERIFIED,
        metadata={
            "manual_publication_text_correction": {
                "parent_artifact_id": str(harness.base_artifact.id),
                "before_sha256": hashlib.sha256(harness.base_bytes).hexdigest(),
                "after_sha256": corrected_sha256,
                "target_anchor": "section:0:paragraph:0001",
                "validation": {
                    "method": "canonical_publication_qa_v5_with_declared_text_edit",
                    "passed": True,
                    "checks_before": {"exact_projection": True},
                    "checks_after": {"exact_projection": False},
                    "accepted_exception": "exact_projection",
                    "warnings": [],
                },
            }
        },
    )
    harness.artifacts.items.append(corrected)
    assert opinion.base_publication_artifact_id == harness.base_artifact.id
    return corrected, corrected_sha256


@pytest.mark.asyncio
async def test_bridge_only_rebase_refuses_non_bridge_prose() -> None:
    harness = make_opinion_harness()
    _bridge_only(harness)
    opinion = _seed_opinion(harness, (_plain_block(harness, "Texte humain copié par rebase."),))
    corrected, corrected_sha256 = _prepare_rebase(harness, opinion)

    with pytest.raises(OpinionError) as refused:
        await harness.service.rebase(
            opinion.id,
            opinion.version,
            opinion.content_sha256,
            corrected.id,
            corrected_sha256,
            "editor:human",
        )

    assert refused.value.code is OpinionErrorCode.BRIDGE_AUTHORSHIP_REQUIRED
    assert harness.uow.analyst_opinions.items[opinion.id] == [opinion]


@pytest.mark.asyncio
async def test_bridge_only_approve_requires_bridge_provenance() -> None:
    harness = make_opinion_harness()
    _bridge_only(harness)
    opinion = _seed_opinion(harness, (_plain_block(harness, "Texte humain non prouvé."),))

    with pytest.raises(OpinionError) as refused:
        await harness.service.approve(
            opinion.id,
            opinion.version,
            opinion.content_sha256,
            "editor:reviewer",
            "Reviewed the uncited draft.",
            bridge_drafting_gap_acknowledged=True,
        )

    assert refused.value.code is OpinionErrorCode.BRIDGE_AUTHORSHIP_REQUIRED
    assert harness.uow.analyst_opinions.items[opinion.id] == [opinion]


@pytest.mark.asyncio
async def test_bridge_only_request_changes_requires_bridge_provenance() -> None:
    harness = make_opinion_harness()
    _bridge_only(harness)
    opinion = _seed_opinion(harness, (_plain_block(harness, "Texte humain non prouvé."),))

    with pytest.raises(OpinionError) as refused:
        await harness.service.request_changes(
            opinion.id,
            opinion.version,
            opinion.content_sha256,
            "editor:reviewer",
            "Please cite Bridge provenance.",
        )

    assert refused.value.code is OpinionErrorCode.BRIDGE_AUTHORSHIP_REQUIRED
    assert harness.uow.analyst_opinions.items[opinion.id] == [opinion]


@pytest.mark.asyncio
async def test_bridge_only_bridge_led_writer_refused() -> None:
    harness = make_opinion_harness()
    investigation = _bridge_only(harness)
    service = _dummy_investigation_service(investigation)

    with pytest.raises(OpinionError) as refused:
        await service._apply_opinion_delta(
            investigation_id=investigation.id,
            state=SimpleNamespace(
                base_publication_artifact_id=uuid4(),
                base_publication_sha256="a" * 64,
            ),
            delta=SimpleNamespace(blocks=(), notes=(), unresolved_points=()),
        )

    assert refused.value.code is OpinionErrorCode.BRIDGE_AUTHORSHIP_REQUIRED
    assert harness.uow.analyst_opinions.items == {}


@pytest.mark.asyncio
async def test_agentic_session_cannot_call_opinion_tools() -> None:
    harness = make_opinion_harness()
    investigation = _bridge_only(harness)
    adapter = _operator_adapter(harness)
    context = _tool_context(investigation.id, actor_id=f"agent-session:{uuid4()}")

    composed = await adapter.execute(
        {
            "action": "compose",
            "selection": {"finding_ids": [], "claim_ids": [], "block_types": []},
        },
        context,
    )
    noted = await adapter.execute(
        {
            "action": "note",
            "anchor": "section:0:paragraph:0001",
            "text": "An agent must never author opinion prose.",
            "state": "unresolved",
            "relation": "unresolved",
            "expected_opinion_version": 1,
            "expected_opinion_sha256": "a" * 64,
            "finding_refs": [],
            "evidence_refs": [],
        },
        context,
    )

    assert composed.status.value == "failed"
    assert composed.error_code == "bridge_authorship_required"
    assert noted.status.value == "failed"
    assert noted.error_code == "bridge_authorship_required"


@pytest.mark.asyncio
async def test_unlinked_operator_client_cannot_call_opinion_tools() -> None:
    harness = make_opinion_harness()
    investigation = _bridge_only(harness)
    draft = await harness.service.compose_from_findings(
        investigation.id,
        OpinionCompositionSelection(),
        draft_blocks=(_bridge_block(harness, "Texte Bridge cité."),),
    )
    adapter = _operator_adapter(harness)
    context = _tool_context(investigation.id, actor_id="operator:claude-client")

    revised = await adapter.execute(
        {
            "action": "revise",
            "opinion_id": str(draft.id),
            "expected_opinion_version": draft.version,
            "expected_opinion_sha256": draft.content_sha256,
            "blocks": [
                {
                    "block_type": "key_findings",
                    "text": "Un jeton partagé non lié ne rédige pas de prose.",
                    "finding_refs": [str(harness.finding.id)],
                    "evidence_refs": [str(harness.observation.id)],
                    "qualification": "fact",
                    "certainty": 0.8,
                }
            ],
            "unresolved_points": [],
        },
        context,
    )

    assert revised.status.value == "failed"
    assert revised.error_code == "bridge_authorship_required"

    # The refusal is server-side and keyed on prose authority and writer kind,
    # not on the (not yet built) D7 session binding.
    with pytest.raises(OpinionError) as refused:
        await harness.service.revise(
            draft.id,
            draft.version,
            OpinionRevisionEdits((_plain_block(harness, "Révision outil non autorisée."),)),
            "operator:claude-client",
            expected_sha256=draft.content_sha256,
            writer_kind=OpinionWriterKind.OPERATOR_TOOL,
        )
    assert refused.value.code is OpinionErrorCode.BRIDGE_AUTHORSHIP_REQUIRED


@pytest.mark.asyncio
async def test_advisor_observations_cannot_be_cited_as_evidence() -> None:
    harness = make_opinion_harness()
    advisory = _advisory_observation(harness)
    observations = _ObservationRepository((harness.observation, advisory))
    findings = _FindingRepository((harness.finding,))
    service = FindingService(
        findings=findings,  # type: ignore[arg-type]
        observations=observations,  # type: ignore[arg-type]
        article_snapshots=SimpleNamespace(),
    )

    assert is_advisory_observation(advisory) is True
    assert is_advisory_observation(harness.observation) is False

    with pytest.raises(ValueError, match="advisory"):
        await service.propose(
            investigation_id=harness.investigation.id,
            kind=FindingKind.CAPABILITY,
            assessment=FindingAssessment.SUPPORTED,
            statement="The cited advisory material is not evidence.",
            observation_refs=(advisory.id,),
        )

    block = OpinionBlock(
        block_type=OpinionBlockType.KEY_FINDINGS,
        text="Une conclusion qui cite un avis Bridge.",
        evidence_refs=(advisory.id,),
        qualification=OpinionClaimQualification.INFERENCE,
        certainty=0.4,
    )
    opinion = _seed_opinion(harness, (block,))
    violations = verify_opinion_citations(
        opinion, (harness.finding,), (harness.observation, advisory)
    )
    assert any(
        item.code is CitationViolationCode.ADVISORY_OBSERVATION_NOT_EVIDENCE for item in violations
    )


@pytest.mark.asyncio
async def test_bridge_led_finding_cannot_cite_advisor_observation() -> None:
    harness = make_opinion_harness()
    advisory = _advisory_observation(harness)
    observations = _ObservationRepository((harness.observation, advisory))
    findings = _FindingRepository()
    service = _dummy_investigation_service(
        harness.investigation,
        observations_repository=observations,
        findings_repository=findings,
        context_builder=SimpleNamespace(article_snapshots=_ArticleSnapshots()),
    )
    proposal = FindingProposalV1(
        kind=FindingKind.NETWORK_BEHAVIOUR,
        assessment=FindingAssessment.SUPPORTED,
        statement="The Bridge advice alone would support this finding.",
        observation_refs=(advisory.id,),
        claim_refs=(),
    )

    persisted, deduplicated = await service._persist_findings_with_count(
        investigation_id=harness.investigation.id,
        proposals=(proposal,),
        decision_trace="Bridge-led turn citing advisory material.",
    )

    assert deduplicated == 0
    assert len(persisted) == 1
    finding = persisted[0]
    assert finding.assessment is FindingAssessment.INCONCLUSIVE
    assert finding.observation_refs == ()
    assert any("advisory" in limitation.casefold() for limitation in finding.limitations)


@pytest.mark.asyncio
async def test_approval_is_distinct_from_publication_and_repin() -> None:
    harness = make_opinion_harness()
    investigation = _bridge_only(harness)
    draft = await harness.service.compose_from_findings(
        investigation.id,
        OpinionCompositionSelection(),
        draft_blocks=(_bridge_block(harness, "Texte Bridge approuvable."),),
    )
    artifacts_before = tuple(harness.artifacts.items)

    approved = await harness.service.approve(
        draft.id,
        draft.version,
        draft.content_sha256,
        "editor:reviewer",
        "Approved exact Bridge prose.",
        bridge_drafting_gap_acknowledged=True,
    )

    assert approved.status is AnalystOpinionStatus.APPROVED
    assert approved.version == draft.version + 1
    assert tuple(harness.artifacts.items) == artifacts_before
    assert harness.artifacts.appended == []
    current_publication = await harness.artifacts.get_current_for_revision(
        harness.base_artifact.production_run_id, "publication"
    )
    assert current_publication == harness.base_artifact


@pytest.mark.asyncio
async def test_bridge_only_preview_renders_resolvable_references() -> None:
    harness = make_opinion_harness()
    investigation = _bridge_only(harness)
    draft = await harness.service.compose_from_findings(
        investigation.id,
        OpinionCompositionSelection(),
        draft_blocks=(_bridge_block(harness, "Texte Bridge avec références résolubles."),),
    )
    renderer = _RecordingDraftRenderer()
    preview = AnalystOpinionPreviewService(
        uow_factory=harness.uow_factory,  # type: ignore[arg-type]
        artifact_store=harness.store,  # type: ignore[arg-type]
        renderer=renderer,
        graph_source=_NoGraph(),  # type: ignore[arg-type]
    )

    rendered = await preview.preview_pdf(
        draft.id,
        version=draft.version,
        expected_opinion_sha256=draft.content_sha256,
        expected_base_publication_sha256=draft.base_publication_sha256,
    )

    assert renderer.calls == 1
    assert renderer.opinion is not None
    assert renderer.opinion.blocks[0].bridge_draft_ref is not None
    assert rendered.content.startswith(b"%PDF-1.7")
    assert rendered.opinion_sha256 == draft.content_sha256
    assert rendered.lineage_manifest["opinion"]["sha256"] == draft.content_sha256
    assert harness.uow.analyst_opinions.items[draft.id] == [draft]


@pytest.mark.asyncio
async def test_legacy_operator_led_behaviour_is_preserved() -> None:
    harness = make_opinion_harness()
    adapter = _operator_adapter(harness)
    tool_result = await adapter.execute(
        {
            "action": "compose",
            "selection": {
                "finding_ids": [],
                "claim_ids": [],
                "block_types": ["key_findings"],
            },
        },
        _tool_context(harness.investigation.id, actor_id="operator:claude-client"),
    )
    composed = (await harness.service.get_draft(harness.investigation.id)).opinion
    assert composed is not None
    plain = _plain_block(harness, "Révision humaine conservée en legacy.")
    revised = await harness.service.revise(
        composed.id,
        composed.version,
        OpinionRevisionEdits((plain,)),
        "editor:human",
        expected_sha256=composed.content_sha256,
    )
    approved = await harness.service.approve(
        revised.id,
        revised.version,
        revised.content_sha256,
        "editor:reviewer",
        "Legacy approval remains unchanged.",
        bridge_drafting_gap_acknowledged=True,
    )

    assert tool_result.status.value == "succeeded"
    assert approved.status is AnalystOpinionStatus.APPROVED
    assert revised.blocks == (plain,)
