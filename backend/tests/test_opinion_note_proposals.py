"""ADR-0011 D2/D4 note-proposal oracles for M24-08B.

No live Bridge: every drafting answer is a scripted persisted
``advisor.consult`` exchange (call observation + response-link observation +
tool invocation journal row).  Human acceptance is exercised through the real
``AnalystOpinionService.accept_note_proposal`` writer and the same
``require_bridge_only_provenance`` gate as every other opinion writer.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport

from cti_app.api.opinion_note_proposals import router as note_proposals_router
from cti_app.application.analyst_opinion_service import (
    AnalystOpinionService,
    OpinionError,
    OpinionErrorCode,
)
from cti_app.application.identity import LocalIdentityProvider
from cti_app.application.investigation_persistence import (
    InMemoryNoteProposalRepository,
    InMemoryToolInvocationRepository,
)
from cti_app.application.operator_tool_invocation import OperatorToolInvocationResult
from cti_app.application.opinion_note_proposals import (
    AdvisorDraftingOutcomeV1,
    NoteArticleElementFactV1,
    NoteContentBlockContextV1,
    NoteProposalError,
    NoteProposalErrorCode,
    OpinionNoteProposalService,
    ResolvedArticleElementBlockV1,
    ResolvedCanonicalCodeBlockV1,
    ResolvedIocTableBlockV1,
    validate_note_content_blocks,
)
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.application.publication_builder import (
    build_article_format_v1,
    build_publication_document_v5,
)
from cti_app.application.typst_rendering import project_publication_to_typst_model
from cti_app.config import Settings
from cti_app.domain.analyst_opinion import (
    AnalystObservationRelation,
    AnalystObservationState,
    AnalystObservationV1,
    AnalystOpinionV1,
    ArticleElementNoteBlockV1,
    CanonicalCodeNoteBlockV1,
    IocTableNoteBlockV1,
    NoteArticleElementKind,
    OpinionBlock,
    OpinionBlockType,
    investigation_opinion_id,
    note_ioc_table_title,
    opinion_blocks_with_observations,
)
from cti_app.domain.editorial import (
    AnalystDecisionTargetType,
    AnalystDecisionType,
)
from cti_app.domain.investigation import (
    EvidenceKind,
    EvidenceObservation,
    ToolInvocation,
    ToolInvocationActorKind,
    ToolInvocationStatus,
    canonical_json_bytes,
    is_advisory_observation,
)
from cti_app.domain.opinion_note_proposal import (
    NoteProposalConsultationType,
    NoteProposalStatus,
    NoteProposalV1,
    NoteTargetBlockType,
    note_proposal_note_id,
)
from cti_app.domain.production import (
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionArtifactStatus,
    ProseAuthority,
)
from cti_app.domain.production_editorial_enrichment import (
    EnrichmentPlacementKind,
    EnrichmentPlacementV1,
)
from cti_app.domain.production_synthesis import extraction_evidence_refs_v1
from cti_app.domain.publication import (
    PublicationEvidenceKind,
    PublicationEvidenceRefV1,
    PublicationParagraphV1,
    PublicationSectionKind,
    PublicationSectionV1,
)
from cti_app.domain.publication_document import (
    PublicationDocumentV7,
    PublicationDocumentV9,
    ResolvedPublicationPlacementV1,
    publication_element_sha256,
    publication_note_body_anchors,
    serialize_publication_document,
)
from cti_app.domain.publication_kind import PublicationKind
from tests.test_analyst_opinion_service import OpinionHarness, make_opinion_harness
from tests.test_publication_builder_v4 import (
    _canonical_inputs,
    _enrichment_with,
)
from tests.test_publication_builder_v4 import _table as _enrichment_table
from tests.test_publication_document_v6 import _base_v5
from tests.test_publication_document_v7 import _v6

NOW = datetime(2026, 11, 22, 12, 0, tzinfo=UTC)
LEAD_ANCHOR = "lead:0001"
SECTION_ANCHOR = "section:0:paragraph:0001"


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _annotated_v5(sections: tuple[PublicationSectionV1, ...]) -> Any:
    """The shared V5 fixture with the given published sections."""
    from cti_app.application.publication_builder import (
        SemanticAnnotator,
        semantic_entities_from_extraction,
    )
    from cti_app.domain.publication_document import (
        PublicationDocumentV5,
        publication_document_text_anchors,
    )
    from cti_app.domain.semantic_annotation import (
        SEMANTIC_ANNOTATION_POLICY_VERSION,
        SEMANTIC_ANNOTATION_SCHEMA_VERSION,
        SemanticTextV1,
    )
    from tests.test_publication_builder_v4 import _canonical_inputs

    base = _base_v5()
    body = replace(base.document, sections=sections)
    _, _, extraction, _ = _canonical_inputs()
    semantic_text = SemanticTextV1(
        schema_version=SEMANTIC_ANNOTATION_SCHEMA_VERSION,
        policy_version=SEMANTIC_ANNOTATION_POLICY_VERSION,
        paragraphs=SemanticAnnotator().annotate_paragraphs(
            tuple(publication_document_text_anchors(body, references=base.references).items()),
            entities=semantic_entities_from_extraction(extraction),
            proposals=(),
        ),
    )
    return PublicationDocumentV5(
        document=body,
        semantic_text=semantic_text,
        references=base.references,
        original_indicators=base.original_indicators,
        ioc_details=base.ioc_details,
    )


def _section(paragraph_text: str, *, heading: str = "Campagne") -> PublicationSectionV1:
    return PublicationSectionV1(
        kind=PublicationSectionKind.CAMPAIGN,
        heading=heading,
        paragraphs=(
            PublicationParagraphV1(
                text=paragraph_text,
                evidence_refs=(
                    PublicationEvidenceRefV1(
                        source_document_id=UUID("00000000-0000-0000-0000-000000000002"),
                        kind=PublicationEvidenceKind.FACT,
                        evidence_key="a" * 64,
                    ),
                ),
            ),
        ),
    )


def base_v5_with_section(section_paragraph: str | None = None) -> Any:
    """The shared publication fixture with one section anchor available."""
    return base_v5_with_sections(
        (
            section_paragraph
            if section_paragraph is not None
            else "Le paragraphe de section décrit la campagne observée.",
        )
    )


def base_v5_with_sections(section_paragraphs: tuple[str, ...]) -> Any:
    return _annotated_v5(
        tuple(
            _section(text, heading=f"Campagne {index}")
            for index, text in enumerate(section_paragraphs, start=1)
        )
    )


def v9_with_overview_duplicate() -> Any:
    """A V9 article whose overview paragraph repeats the lead text verbatim.

    The legacy projection drops both the overview section and duplicated
    paragraphs; the V9 projection renders them, so a note may anchor there.
    """
    from cti_app.application.publication_builder import build_article_format_v1
    from cti_app.domain.publication_document import PublicationDocumentV7, PublicationDocumentV9
    from cti_app.domain.publication_kind import PublicationKind
    from tests.test_publication_builder_v4 import _canonical_inputs
    from tests.test_publication_document_v7 import _v6

    lead = _base_v5().document.lead[0]
    overview = PublicationSectionV1(
        kind=PublicationSectionKind.OVERVIEW,
        heading="Vue d'ensemble",
        paragraphs=(PublicationParagraphV1(text=lead.text, evidence_refs=lead.evidence_refs),),
    )
    analysis = PublicationSectionV1(
        kind=PublicationSectionKind.TECHNICAL,
        heading="Analyse synthétique",
        paragraphs=(
            PublicationParagraphV1(
                text="Le paragraphe d'analyse décrit la campagne observée.",
                evidence_refs=lead.evidence_refs,
            ),
        ),
    )
    base = _annotated_v5((overview, analysis))
    _, _, extraction, _ = _canonical_inputs()
    v7 = PublicationDocumentV7(
        base_publication=_v6(base),
        publication_kind=PublicationKind.ARTICLE,
        routing_decision_hash="a" * 64,
        routing_policy_version="routing-policy-v1",
    )
    return PublicationDocumentV9(
        base_publication=v7,
        article_format=build_article_format_v1(base, extraction),
        registry_hash="b" * 64,
        synthesis_canonical_sha256="c" * 64,
        enrichment_canonical_sha256="d" * 64,
        resolved_placements=(),
    )


class _ObservationRepository:
    """Mutable test double: two tests deliberately tamper a stored observation."""

    def __init__(self, items: tuple[EvidenceObservation, ...] = ()) -> None:
        self.items: dict[UUID, EvidenceObservation] = {item.id: item for item in items}

    async def get(self, observation_id: UUID) -> EvidenceObservation | None:
        return self.items.get(observation_id)

    async def list_for_investigation(
        self, investigation_id: UUID
    ) -> tuple[EvidenceObservation, ...]:
        return tuple(
            item for item in self.items.values() if item.investigation_id == investigation_id
        )

    async def append_if_absent(self, observation: EvidenceObservation) -> EvidenceObservation:
        existing = self.items.get(observation.id)
        if existing is not None:
            if existing != observation:
                raise ValueError("Immutable observation identity was reused")
            return existing
        self.items[observation.id] = observation
        return observation

    def replace_blob(
        self, store: Any, observation_id: UUID, payload: dict[str, Any]
    ) -> EvidenceObservation:
        raw = canonical_json_bytes(payload)
        blob_id = uuid4()
        store.blobs[blob_id] = raw
        updated = replace(self.items[observation_id], blob_id=blob_id, sha256=_sha256(raw))
        self.items[observation_id] = updated
        return updated


class _Uow:
    def __init__(
        self,
        harness: OpinionHarness,
        observations: _ObservationRepository,
        invocations: InMemoryToolInvocationRepository,
        proposals: InMemoryNoteProposalRepository,
    ) -> None:
        base = harness.uow
        self.analyst_investigations = base.analyst_investigations
        self.analyst_opinions = base.analyst_opinions
        self.investigation_findings = base.investigation_findings
        self.analyst_decisions = base.analyst_decisions
        self.production_artifacts = base.production_artifacts
        self.production_runs = base.production_runs
        self.evidence_observations = observations
        self.tool_invocations = invocations
        self.investigation_note_proposals = proposals

    async def __aenter__(self) -> _Uow:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def commit(self) -> None:
        return None


class _Fixture:
    def __init__(self, *, section_paragraph: str | None = None) -> None:
        harness = make_opinion_harness()
        self._extend_base_with_section(harness, section_paragraph)
        self.harness = harness
        self.observations = _ObservationRepository((harness.observation,))
        self.invocations = InMemoryToolInvocationRepository()
        self.proposals = InMemoryNoteProposalRepository()
        investigation = replace(harness.investigation, prose_authority=ProseAuthority.BRIDGE_ONLY)
        harness.uow.analyst_investigations.item = investigation
        self.investigation = investigation

        def uow_factory() -> _Uow:
            return _Uow(harness, self.observations, self.invocations, self.proposals)

        self.uow_factory = uow_factory
        self.proposal_service = OpinionNoteProposalService(
            uow_factory=uow_factory,
            artifact_store=harness.store,
        )
        self.opinion_service = AnalystOpinionService(
            uow_factory=uow_factory,
            artifact_store=harness.store,
        )

    @staticmethod
    def _install_base(harness: OpinionHarness, document: Any) -> None:
        base_bytes = ProductionArtifactStore.canonical_json_bytes(
            serialize_publication_document(document)
        )
        harness.store.blobs[harness.base_artifact.canonical_blob_id] = base_bytes
        base_sha256 = _sha256(base_bytes)
        harness.artifacts.items = [
            replace(item, input_hash=base_sha256) if item.id == harness.base_artifact.id else item
            for item in harness.artifacts.items
        ]
        harness.base_artifact = replace(harness.base_artifact, input_hash=base_sha256)
        harness.base_bytes = base_bytes

    @staticmethod
    def _extend_base_with_section(
        harness: OpinionHarness, section_paragraph: str | None = None
    ) -> None:
        _Fixture._install_base(harness, base_v5_with_section(section_paragraph))

    def install_document(self, document: Any) -> None:
        self._install_base(self.harness, document)

    def advisor(self, **options: Any) -> _ScriptedAdvisor:
        return _ScriptedAdvisor(self, **options)

    async def request(self, advisor: _ScriptedAdvisor, **overrides: Any) -> Any:
        defaults: dict[str, Any] = {
            "anchor": LEAD_ANCHOR,
            "target_block_type": NoteTargetBlockType.LEAD,
            "bridge_block_type": OpinionBlockType.KEY_FINDINGS,
            "consultation_type": NoteProposalConsultationType.DRAFT_TEXT,
            "note_state": AnalystObservationState.UNRESOLVED,
            "note_relation": AnalystObservationRelation.UNRESOLVED,
            "finding_refs": (),
            "observation_refs": (),
            "content_blocks": (),
            "base_opinion_version": None,
            "base_opinion_sha256": None,
            "task_id": None,
            "question": None,
            "expected_working_state_version": 1,
            "expected_working_state_sha256": None,
            "actor_id": "analyst:human",
            "requester_actor_kind": ToolInvocationActorKind.HUMAN,
            "idempotency_key": f"note-request-{uuid4()}",
            "advisor": advisor,
            "now": NOW,
        }
        defaults.update(overrides)
        return await self.proposal_service.request(self.investigation.id, **defaults)

    async def accept(self, proposal: NoteProposalV1, **overrides: Any) -> Any:
        values: dict[str, Any] = {
            "expected_status_version": proposal.status_version,
            "expected_base_version": proposal.base_opinion_version,
            "expected_base_sha256": proposal.base_opinion_sha256,
            "actor": "analyst:human",
            "reason": "Accept the Bridge note proposal",
            "idempotency_key": f"note-accept-{uuid4()}",
        }
        values.update(overrides)
        return await self.opinion_service.accept_note_proposal(
            self.investigation.id, proposal.id, **values
        )


class _ScriptedAdvisor:
    """Persist one scripted Bridge exchange and return its invocation identity."""

    def __init__(
        self,
        fixture: _Fixture,
        *,
        text: str | dict[str, str] = "Texte rédigé par Bridge pour la note ciblée.",
        sentence_texts: tuple[str, ...] | None = None,
        anchored_text: str | None = None,
        response: bool = True,
        advisor_status: str = "succeeded",
        usable: bool = True,
        real_provenance: bool = True,
        review_usable: bool = True,
    ) -> None:
        self.fixture = fixture
        self.text = text
        self.sentence_texts = sentence_texts
        self.anchored_text = anchored_text
        self.response = response
        self.advisor_status = advisor_status
        self.usable = usable
        self.real_provenance = real_provenance
        self.review_usable = review_usable
        self.requests: list[Any] = []
        self.invocations: list[ToolInvocation] = []
        self.call_observations: list[EvidenceObservation] = []

    def text_for(self, anchor: str) -> str:
        if isinstance(self.text, dict):
            return self.text[anchor]
        if self.anchored_text is not None:
            return f"{self.anchored_text} [{anchor}]"
        return self.text

    async def request_draft(self, uow: Any, request: Any) -> AdvisorDraftingOutcomeV1:
        self.requests.append(request)
        text = self.text_for(request.anchor)
        block_type = request.bridge_block_type.value
        raw_wire = (
            "advisor-wire.v1\n"
            f"TYPE: {request.consultation_type.value}\n"
            f"STATUS: {self.advisor_status}\n"
            "UNIT: U1\n"
            f"S: {text}\n"
            "LIMITATION: Note scriptée pour oracle hors ligne."
        ).encode()
        output_sha256 = _sha256(raw_wire)
        output: dict[str, Any] = {
            "schema": "advisor-response.v3",
            "usable": self.usable,
            "output_sha256": output_sha256,
            "blocks": [
                {
                    "block_type": block_type,
                    "form": "analyst_note",
                    "anchor": request.anchor,
                    "usable": self.usable,
                    "sentences": [
                        {"text": value}
                        for value in (
                            self.sentence_texts if self.sentence_texts is not None else (text,)
                        )
                    ],
                }
            ],
            "bridge_provenance": {
                "bridge_invocation_id": str(uuid4()),
                "bridge_invocation_id_kind": "conversation_turn",
                "output_sha256": output_sha256,
                "input_refs": [],
            },
        }
        bridge_invocation_id = output["bridge_provenance"]["bridge_invocation_id"]
        if request.consultation_type is NoteProposalConsultationType.REVIEW_TEXT:
            reviewed = next(
                item
                for item in (await _head(self.fixture)).observations
                if item.anchor == request.anchor
            )
            reviewed_sha256 = _sha256(reviewed.text.encode("utf-8"))
            output = {
                "schema": "advisor-response.v4",
                "usable": self.usable,
                "output_sha256": output_sha256,
                "reviews": [
                    {
                        "unit_id": "U1",
                        "remarks": ["La note reste mesurée."],
                        "usable": self.review_usable,
                    }
                ],
                "bridge_provenance": {
                    "bridge_invocation_id": bridge_invocation_id,
                    "bridge_invocation_id_kind": "conversation_turn",
                    "output_sha256": output_sha256,
                    "input_refs": [],
                },
            }
            review_units = [
                {
                    "unit_id": "U1",
                    "form": "analyst_note",
                    "block_type": block_type,
                    "anchor": request.anchor,
                    "reviewed_text_sha256": reviewed_sha256,
                }
            ]
        else:
            review_units = None
        call_payload: dict[str, Any] = {
            "schema": "operator-advisor-call.v1",
            "consultation_type": request.consultation_type.value,
            "advisor_status": self.advisor_status,
            "bridge_invocation_id": bridge_invocation_id,
            "bridge_invocation_id_kind": "conversation_turn",
            "output_sha256": output_sha256,
            "input_refs": [],
            "output": output,
        }
        if review_units is not None:
            call_payload["review_units"] = review_units
        if not self.real_provenance:
            call_payload["bridge_invocation_id_kind"] = "unavailable"
            output["bridge_provenance"]["bridge_invocation_id_kind"] = "unavailable"
        call_raw = canonical_json_bytes(call_payload)
        call_blob_id = uuid4()
        fixture_store = self.fixture.harness.store
        fixture_store.blobs[call_blob_id] = call_raw
        call_observation = EvidenceObservation(
            investigation_id=self.fixture.investigation.id,
            kind=EvidenceKind.OTHER,
            blob_id=call_blob_id,
            sha256=_sha256(call_raw),
            source="advisor.consult",
            source_version="advisor-consult.v1",
            observed_at=NOW,
            object_type="advisor_call",
            object_id=request.consultation_type.value,
            limitations=("Bridge output is untrusted advisory text.",),
        )
        await uow.evidence_observations.append_if_absent(call_observation)
        self.call_observations.append(call_observation)
        response_refs: list[UUID] = [call_observation.id]
        response_observation: EvidenceObservation | None = None
        if self.response:
            raw_blob_id = uuid4()
            fixture_store.blobs[raw_blob_id] = raw_wire
            link_payload = {
                "schema": "operator-advisor-response-link.v1",
                "stage": "response_received",
                "consultation_type": request.consultation_type.value,
                "bridge_invocation_id": bridge_invocation_id,
                "raw_response_blob_id": str(raw_blob_id),
                "raw_response_sha256": output_sha256,
            }
            link_raw = canonical_json_bytes(link_payload)
            link_blob_id = uuid4()
            fixture_store.blobs[link_blob_id] = link_raw
            response_observation = EvidenceObservation(
                investigation_id=self.fixture.investigation.id,
                kind=EvidenceKind.OTHER,
                blob_id=link_blob_id,
                sha256=_sha256(link_raw),
                source="advisor.consult.response",
                source_version="advisor-consult.v1",
                observed_at=NOW,
                object_type="advisor_response_received",
                object_id=str(uuid4()),
                limitations=("Raw Bridge text is untrusted advisory material.",),
            )
            await uow.evidence_observations.append_if_absent(response_observation)
            response_refs.append(response_observation.id)
        invocation = ToolInvocation(
            investigation_id=self.fixture.investigation.id,
            operation_key=f"advisor-{uuid4()}",
            tool_name="advisor.consult",
            schema_version="advisor-consult.v1",
            normalized_parameters={
                "consultation_type": request.consultation_type.value,
                "anchor": request.anchor,
                "block_type": block_type,
            },
            purpose=request.purpose,
            status=ToolInvocationStatus.SUCCEEDED,
            response_refs=tuple(response_refs),
            created_at=NOW,
            updated_at=NOW,
        )
        await uow.tool_invocations.add_if_absent(invocation)
        self.invocations.append(invocation)
        return AdvisorDraftingOutcomeV1(
            invocation_id=invocation.id,
            invocation_status=invocation.status,
            response_observation_ids=tuple(response_refs),
            idempotent_replay=False,
        )


async def _head(fixture: _Fixture) -> Any:
    async with fixture.uow_factory() as uow:
        return await uow.analyst_opinions.get(investigation_opinion_id(fixture.investigation.id))


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _materialised_proposal(fixture: _Fixture, result: Any) -> NoteProposalV1:
    del fixture
    proposal = result.proposal
    assert isinstance(proposal, NoteProposalV1)
    assert proposal.materialised is True
    return proposal


def test_proposal_requires_advisor_response_observation() -> None:
    """F01: a pending request never fabricates Bridge text or provenance."""
    fixture = _Fixture()

    async def scenario() -> None:
        result = await fixture.request(fixture.advisor(response=False))
        assert result.proposal.status is NoteProposalStatus.PROPOSED
        assert result.proposal.materialised is False
        assert result.proposal.proposed_text is None
        assert result.proposal.bridge_invocation_id is None
        with pytest.raises(NoteProposalError) as pending:
            await fixture.proposal_service.ingest_response(
                fixture.investigation.id,
                result.proposal.id,
                expected_status_version=result.proposal.status_version,
                actor="system:test",
            )
        assert pending.value.code is NoteProposalErrorCode.RESPONSE_PENDING
        with pytest.raises(OpinionError) as refused:
            await fixture.accept(result.proposal)
        assert refused.value.code is OpinionErrorCode.BRIDGE_PROVENANCE_INVALID

    _run(scenario())


def test_note_acceptance_uses_stored_bridge_text_and_human_decision() -> None:
    """F02: the canonical version copies the exact stored Bridge text."""
    fixture = _Fixture()

    async def scenario() -> None:
        text = "Le chargement de second étage est cohérent avec le finding cité."
        advisor = fixture.advisor(text=text)
        proposal = _materialised_proposal(fixture, await fixture.request(advisor))
        acceptance = await fixture.accept(proposal)
        assert acceptance.opinion.version == 1
        assert acceptance.opinion.author_id == "system:note-acceptance"
        assert acceptance.decision.decision_type is AnalystDecisionType.NOTE_APPROVE
        assert acceptance.decision.target_type is AnalystDecisionTargetType.NOTE
        assert acceptance.decision.target_id == note_proposal_note_id(proposal.id)
        assert acceptance.decision.actor_id == "analyst:human"
        assert acceptance.decision.details["proposal_id"] == str(proposal.id)
        assert acceptance.decision.details["opinion_version"] == 1
        note = acceptance.opinion.observations[0]
        assert note.text == text
        assert note.anchor == LEAD_ANCHOR
        assert note.bridge_draft_ref is not None
        assert note.bridge_draft_ref.invocation_id == proposal.bridge_invocation_id
        assert note.bridge_draft_ref.text_sha256 == _sha256(text.encode("utf-8"))
        assert note.bridge_draft_ref.source_observation_id == (advisor.call_observations[0].id)
        assert acceptance.proposal.status is NoteProposalStatus.ACCEPTED
        assert acceptance.proposal.accepted_opinion_version == 1
        assert acceptance.proposal.analyst_decision_id == acceptance.decision.id

    _run(scenario())


def test_bridge_sentences_preserve_stored_paragraph_and_list_breaks() -> None:
    """R07 M2: sentence transport must not flatten stored paragraph/list breaks."""
    fixture = _Fixture()
    sentence_texts = (
        "Premier paragraphe de la note.\n",
        "\nDeuxieme paragraphe de la note.",
        "- premier point\n- second point",
    )
    expected = (
        "Premier paragraphe de la note.\n\nDeuxieme paragraphe de la note. "
        "- premier point\n- second point"
    )

    async def scenario() -> None:
        advisor = fixture.advisor(sentence_texts=sentence_texts)
        proposal = _materialised_proposal(fixture, await fixture.request(advisor))
        assert proposal.proposed_text == expected
        assert "\n\n" in proposal.proposed_text
        assert "- premier point\n- second point" in proposal.proposed_text
        acceptance = await fixture.accept(proposal)
        note = acceptance.opinion.observations[0]
        assert note.text == expected
        assert note.bridge_draft_ref is not None
        assert note.bridge_draft_ref.text_sha256 == _sha256(expected.encode("utf-8"))

    _run(scenario())


def test_legacy_space_joined_bridge_note_still_verifies() -> None:
    """R07 M2 keeps pre-fix notes verifiable against their immutable Bridge call."""
    sentences = ["Premier paragraphe de la note.\n", "\nDeuxieme paragraphe de la note."]
    materialised = "Premier paragraphe de la note.\n\nDeuxieme paragraphe de la note."
    legacy = "Premier paragraphe de la note.\n \nDeuxieme paragraphe de la note."

    assert AnalystOpinionService._sentence_text_matches_source(
        sentences, _sha256(materialised.encode("utf-8"))
    )
    assert AnalystOpinionService._sentence_text_matches_source(
        sentences, _sha256(legacy.encode("utf-8"))
    )
    assert not AnalystOpinionService._sentence_text_matches_source(sentences, "0" * 64)


def test_deduplicated_v5_anchor_is_refused_at_request() -> None:
    """F4-D B4: the legacy V5 projection drops the duplicate, so request refuses.

    The anchor exists in the raw text-anchor space but not in the rendered
    body; refusing at request keeps an unrenderable anchor out of a proposal
    before Bridge is consulted.  The V9 counterpart is renderable and accepted.
    """
    fixture = _Fixture(section_paragraph=_base_v5().document.lead[0].text)

    async def scenario() -> None:
        advisor = fixture.advisor(text="Note sur un paragraphe non rendu.")
        with pytest.raises(NoteProposalError) as refused:
            await fixture.request(
                advisor,
                anchor=SECTION_ANCHOR,
                target_block_type=NoteTargetBlockType.SECTION,
            )
        assert refused.value.code is NoteProposalErrorCode.INVALID_ANCHOR

    _run(scenario())


def test_duplicate_paragraph_anchor_is_renderable_and_accepted_on_v9() -> None:
    """F4-D B4: V9 renders duplicated/overview paragraphs, so a note may anchor there."""
    fixture = _Fixture()
    v9 = v9_with_overview_duplicate()
    _Fixture._install_base(fixture.harness, v9)
    lead_text = _base_v5().document.lead[0].text

    async def scenario() -> None:
        advisor = fixture.advisor(text="Note sur le paragraphe d'ensemble.")
        proposal = _materialised_proposal(
            fixture,
            await fixture.request(
                advisor,
                anchor=SECTION_ANCHOR,
                target_block_type=NoteTargetBlockType.SECTION,
            ),
        )
        assert proposal.base_publication_artifact_id == fixture.harness.base_artifact.id
        assert proposal.base_publication_version == fixture.harness.base_artifact.version
        assert proposal.base_publication_sha256 == _sha256(fixture.harness.base_bytes)
        assert proposal.anchor_before_sha256 == _sha256(lead_text.encode("utf-8"))
        acceptance = await fixture.accept(proposal)
        assert [item.anchor for item in acceptance.opinion.observations] == [SECTION_ANCHOR]

    _run(scenario())

    texts = publication_note_body_anchors(v9)
    assert texts[SECTION_ANCHOR] == lead_text
    model = project_publication_to_typst_model(v9)
    expected_target = (
        "publication-paragraph-" + hashlib.sha256(SECTION_ANCHOR.encode("utf-8")).hexdigest()[:20]
    )
    assert any(
        block.get("text") == lead_text and block.get("anchor_target") == expected_target
        for section in model.content_sections
        if section["type"] == "synthesis"
        for block in section["blocks"]
        if block.get("type") == "paragraph"
    )


def test_anchor_uses_pinned_base_publication() -> None:
    """F4-D B4: request validates and pins the opinion's base, never a newer one."""
    fixture = _Fixture()

    async def scenario() -> None:
        first = _materialised_proposal(
            fixture, await fixture.request(fixture.advisor(text="Note initiale."))
        )
        accepted = await fixture.accept(first)
        pinned = fixture.harness.base_artifact
        # A newer publication base becomes current while the opinion keeps its pin.
        newer_document = base_v5_with_sections(
            ("Campagne initiale.", "Paragraphe publié après la note.")
        )
        newer_bytes = ProductionArtifactStore.canonical_json_bytes(
            serialize_publication_document(newer_document)
        )
        newer_blob_id = uuid4()
        fixture.harness.store.blobs[newer_blob_id] = newer_bytes
        newer = ProductionArtifact(
            id=uuid4(),
            production_run_id=pinned.production_run_id,
            subject_id=pinned.subject_id,
            stage=ProductionArtifactStage.PUBLICATION,
            version=2,
            input_hash=_sha256(newer_bytes),
            canonical_blob_id=newer_blob_id,
            status=ProductionArtifactStatus.VERIFIED,
        )
        async with fixture.uow_factory() as uow:
            await uow.production_artifacts.append(newer)

        # An anchor that only the newer base renders is refused.
        with pytest.raises(NoteProposalError) as refused:
            await fixture.request(
                fixture.advisor(text="Note sur le nouveau paragraphe."),
                anchor="section:1:paragraph:0001",
                target_block_type=NoteTargetBlockType.SECTION,
                base_opinion_version=accepted.opinion.version,
                base_opinion_sha256=accepted.opinion.content_sha256,
            )
        assert refused.value.code is NoteProposalErrorCode.INVALID_ANCHOR

        second = _materialised_proposal(
            fixture,
            await fixture.request(
                fixture.advisor(text="Note sur la base épinglée."),
                anchor=SECTION_ANCHOR,
                target_block_type=NoteTargetBlockType.SECTION,
                base_opinion_version=accepted.opinion.version,
                base_opinion_sha256=accepted.opinion.content_sha256,
            ),
        )
        assert second.base_publication_artifact_id == pinned.id
        assert second.base_publication_version == pinned.version
        assert second.base_publication_sha256 == _sha256(fixture.harness.base_bytes)

    _run(scenario())


def test_acceptance_refuses_a_moved_publication_base_with_stale_base() -> None:
    """F4-D B4: a publication revision between request and acceptance is stale_base."""
    fixture = _Fixture()

    async def scenario() -> None:
        proposal = _materialised_proposal(
            fixture, await fixture.request(fixture.advisor(text="Note sur la base initiale."))
        )
        newer_document = base_v5_with_sections(
            ("Campagne initiale.", "Paragraphe publié après la note.")
        )
        newer_bytes = ProductionArtifactStore.canonical_json_bytes(
            serialize_publication_document(newer_document)
        )
        newer_blob_id = uuid4()
        fixture.harness.store.blobs[newer_blob_id] = newer_bytes
        async with fixture.uow_factory() as uow:
            await uow.production_artifacts.append(
                ProductionArtifact(
                    id=uuid4(),
                    production_run_id=fixture.harness.base_artifact.production_run_id,
                    subject_id=fixture.harness.base_artifact.subject_id,
                    stage=ProductionArtifactStage.PUBLICATION,
                    version=2,
                    input_hash=_sha256(newer_bytes),
                    canonical_blob_id=newer_blob_id,
                    status=ProductionArtifactStatus.VERIFIED,
                )
            )
        with pytest.raises(OpinionError) as refused:
            await fixture.accept(proposal)
        assert refused.value.code is OpinionErrorCode.STALE_BASE

    _run(scenario())


def test_acceptance_refuses_changed_anchor_text_with_typed_code() -> None:
    """F4-D B4: a paragraph identity mismatch is a typed refusal."""
    fixture = _Fixture()

    async def scenario() -> None:
        proposal = _materialised_proposal(
            fixture, await fixture.request(fixture.advisor(text="Note identitaire."))
        )
        tampered = replace(
            proposal,
            id=uuid4(),
            idempotency_key=f"tampered-{uuid4()}",
            anchor_before_sha256="0" * 64,
        )
        await fixture.proposals.append_if_absent(tampered)
        with pytest.raises(OpinionError) as refused:
            await fixture.accept(tampered)
        assert refused.value.code is OpinionErrorCode.NOTE_ANCHOR_TEXT_CHANGED

    _run(scenario())


def test_removed_anchor_refused_with_typed_code() -> None:
    """F4-D B4: a legacy proposal whose anchor disappeared never moves silently."""
    fixture = _Fixture()

    async def scenario() -> None:
        proposal = _materialised_proposal(
            fixture,
            await fixture.request(
                fixture.advisor(text="Note sur un paragraphe ensuite retiré."),
                anchor=SECTION_ANCHOR,
                target_block_type=NoteTargetBlockType.SECTION,
            ),
        )
        legacy = replace(
            proposal,
            id=uuid4(),
            idempotency_key=f"legacy-{uuid4()}",
            base_publication_artifact_id=None,
            base_publication_version=None,
            base_publication_sha256=None,
            anchor_before_sha256=None,
        )
        await fixture.proposals.append_if_absent(legacy)
        # The next base revision deduplicates the section paragraph against the
        # lead, so the rendered anchor space no longer carries it.
        _Fixture._extend_base_with_section(fixture.harness, _base_v5().document.lead[0].text)
        with pytest.raises(OpinionError) as refused:
            await fixture.accept(legacy)
        assert refused.value.code is OpinionErrorCode.INVALID_OBSERVATION_ANCHOR

    _run(scenario())


def test_note_acceptance_idempotent_and_stale_refused() -> None:
    """F03/F04: replay is one version; a moved head is refused and marked stale."""
    fixture = _Fixture()

    async def scenario() -> None:
        proposal = _materialised_proposal(
            fixture, await fixture.request(fixture.advisor(text="Version initiale."))
        )
        acceptance = await fixture.accept(proposal, idempotency_key="accept-key-1")
        replay = await fixture.accept(proposal, idempotency_key="accept-key-1")
        assert replay.idempotent_replay is True
        assert replay.opinion.version == acceptance.opinion.version
        async with fixture.uow_factory() as uow:
            decisions = await uow.analyst_decisions.list_for_investigation(fixture.investigation.id)
        assert len(decisions) == 1

        second = _materialised_proposal(
            fixture,
            await fixture.request(
                fixture.advisor(text="Correction demandée."),
                base_opinion_version=1,
                base_opinion_sha256=acceptance.opinion.content_sha256,
            ),
        )
        third = _materialised_proposal(
            fixture,
            await fixture.request(
                fixture.advisor(text="Note sur un autre paragraphe."),
                anchor=SECTION_ANCHOR,
                target_block_type=NoteTargetBlockType.SECTION,
                base_opinion_version=1,
                base_opinion_sha256=acceptance.opinion.content_sha256,
            ),
        )
        third_acceptance = await fixture.accept(third)
        assert third_acceptance.opinion.version == 2
        with pytest.raises(OpinionError) as stale:
            await fixture.accept(second)
        assert stale.value.code is OpinionErrorCode.STALE_NOTE_PROPOSAL
        async with fixture.uow_factory() as uow:
            stored = await uow.investigation_note_proposals.get(second.id)
        assert stored is not None and stored.status is NoteProposalStatus.STALE

    _run(scenario())


def test_note_acceptance_requires_bridge_provenance() -> None:
    """F04: a tampered persisted call cannot be accepted."""
    fixture = _Fixture()

    async def scenario() -> None:
        advisor = fixture.advisor(text="Texte initial.")
        proposal = _materialised_proposal(fixture, await fixture.request(advisor))
        call_observation = advisor.call_observations[0]
        payload = json.loads(fixture.harness.store.blobs[call_observation.blob_id])
        payload["output"]["blocks"][0]["anchor"] = SECTION_ANCHOR
        fixture.observations.replace_blob(fixture.harness.store, call_observation.id, payload)
        with pytest.raises(OpinionError) as refused:
            await fixture.accept(proposal)
        assert refused.value.code is OpinionErrorCode.BRIDGE_PROVENANCE_INVALID

    _run(scenario())


def test_note_proposal_forged_or_unusable_answer_never_materialises() -> None:
    """F05: a Bridge answer that is not usable cannot produce proposal text."""
    fixture = _Fixture()

    async def scenario() -> None:
        unusable = await fixture.request(fixture.advisor(usable=False))
        assert unusable.proposal.materialised is False
        with pytest.raises(NoteProposalError) as refused:
            await fixture.proposal_service.ingest_response(
                fixture.investigation.id,
                unusable.proposal.id,
                expected_status_version=unusable.proposal.status_version,
                actor="system:test",
            )
        assert refused.value.code is NoteProposalErrorCode.RESPONSE_UNUSABLE

        forged = await fixture.request(fixture.advisor(real_provenance=False))
        assert forged.proposal.materialised is False

    _run(scenario())


def test_proposal_uses_verified_findings_and_keeps_exact_bridge_ref() -> None:
    """F01/F02: the request carries verified findings; acceptance keeps its ref."""
    fixture = _Fixture()

    async def scenario() -> None:
        advisor = fixture.advisor(text="Texte fondé sur le finding vérifié.")
        result = await fixture.request(
            advisor,
            finding_refs=(fixture.harness.finding.id,),
            observation_refs=(fixture.harness.observation.id,),
            note_state=AnalystObservationState.INFERRED,
            note_relation=AnalystObservationRelation.CONFIRMS,
        )
        proposal = _materialised_proposal(fixture, result)
        assert advisor.requests[0].finding_refs == (fixture.harness.finding.id,)
        assert advisor.requests[0].evidence_refs == (fixture.harness.observation.id,)
        assert proposal.cited_finding_refs == (fixture.harness.finding.id,)
        assert proposal.cited_observation_refs == (fixture.harness.observation.id,)
        acceptance = await fixture.accept(proposal)
        note = acceptance.opinion.observations[0]
        assert note.finding_refs == (fixture.harness.finding.id,)
        assert note.evidence_refs == (fixture.harness.observation.id,)
        assert note.bridge_draft_ref is not None
        assert note.bridge_draft_ref.text_sha256 == proposal.proposed_text_sha256
        assert note.bridge_draft_ref.source_observation_id == (advisor.call_observations[0].id)

    _run(scenario())


def test_correction_request_creates_new_proposal_and_keeps_old_version() -> None:
    """F03: a correction is a new proposal and a new version; history is kept."""
    fixture = _Fixture()

    async def scenario() -> None:
        first = _materialised_proposal(
            fixture, await fixture.request(fixture.advisor(text="Première rédaction."))
        )
        first_acceptance = await fixture.accept(first)
        correction = _materialised_proposal(
            fixture,
            await fixture.request(
                fixture.advisor(text="Rédaction corrigée par Bridge."),
                base_opinion_version=1,
                base_opinion_sha256=first_acceptance.opinion.content_sha256,
            ),
        )
        correction_acceptance = await fixture.accept(correction)
        assert correction_acceptance.opinion.version == 2
        assert [note.text for note in correction_acceptance.opinion.observations] == [
            "Rédaction corrigée par Bridge."
        ]
        async with fixture.uow_factory() as uow:
            original = await uow.analyst_opinions.get(first_acceptance.opinion.id, version=1)
            first_row = await uow.investigation_note_proposals.get(first.id)
        assert original is not None
        assert original.observations[0].text == "Première rédaction."
        assert first_row is not None and first_row.status is NoteProposalStatus.ACCEPTED

    _run(scenario())


def test_notes_at_different_anchors_coexist() -> None:
    """F06: two accepted notes at different article anchors share one version."""
    fixture = _Fixture()

    async def scenario() -> None:
        lead = _materialised_proposal(
            fixture, await fixture.request(fixture.advisor(text="Note du chapeau."))
        )
        lead_acceptance = await fixture.accept(lead)
        section = _materialised_proposal(
            fixture,
            await fixture.request(
                fixture.advisor(text="Note de section."),
                anchor=SECTION_ANCHOR,
                target_block_type=NoteTargetBlockType.SECTION,
                base_opinion_version=1,
                base_opinion_sha256=lead_acceptance.opinion.content_sha256,
            ),
        )
        section_acceptance = await fixture.accept(section)
        notes = {note.anchor: note.text for note in section_acceptance.opinion.observations}
        assert notes == {
            LEAD_ANCHOR: "Note du chapeau.",
            SECTION_ANCHOR: "Note de section.",
        }

    _run(scenario())


def test_invalid_citation_blocks_acceptance() -> None:
    """F08: an advisory observation can never back an accepted note."""
    fixture = _Fixture()

    async def scenario() -> None:
        proposal = _materialised_proposal(
            fixture, await fixture.request(fixture.advisor(text="Note citée."))
        )
        tampered = replace(
            fixture.observations.items[fixture.harness.observation.id],
            source="advisor.consult",
            object_type="advisor_call",
        )
        assert is_advisory_observation(tampered)
        fixture.observations.items[tampered.id] = tampered
        stored = replace(
            proposal,
            cited_observation_refs=(tampered.id,),
            note_state=AnalystObservationState.INFERRED,
            note_relation=AnalystObservationRelation.QUALIFIES,
        )
        fixture.proposals._items[proposal.id] = stored
        with pytest.raises(OpinionError) as refused:
            await fixture.accept(stored)
        assert refused.value.code is OpinionErrorCode.CITATIONS_INVALID

    _run(scenario())


def test_rejected_proposal_is_terminal_and_retry_needs_a_new_key() -> None:
    fixture = _Fixture()

    async def scenario() -> None:
        proposal = _materialised_proposal(
            fixture, await fixture.request(fixture.advisor(text="À rejeter."))
        )
        rejected = await fixture.proposal_service.reject(
            fixture.investigation.id,
            proposal.id,
            expected_status_version=proposal.status_version,
            actor="analyst:human",
            reason="Pas la bonne formulation.",
            now=NOW,
        )
        assert rejected.status is NoteProposalStatus.REJECTED
        with pytest.raises(NoteProposalError) as stale:
            await fixture.proposal_service.ingest_response(
                fixture.investigation.id,
                proposal.id,
                expected_status_version=rejected.status_version,
                actor="system:test",
            )
        assert stale.value.code is NoteProposalErrorCode.INVALID_STATUS
        retry = await fixture.request(
            fixture.advisor(text="Nouvelle tentative."),
            idempotency_key="note-retry-1",
        )
        assert retry.proposal.id != proposal.id
        assert retry.proposal.status is NoteProposalStatus.PROPOSED
        replay = await fixture.request(
            fixture.advisor(text="Nouvelle tentative."),
            idempotency_key="note-retry-1",
        )
        assert replay.idempotent_replay is True
        assert replay.proposal.id == retry.proposal.id

    _run(scenario())


def test_review_proposal_keeps_review_reference() -> None:
    fixture = _Fixture()

    async def scenario() -> None:
        drafted = _materialised_proposal(
            fixture, await fixture.request(fixture.advisor(text="Note vérifiée."))
        )
        first = await fixture.accept(drafted)
        review = _materialised_proposal(
            fixture,
            await fixture.request(
                fixture.advisor(),
                consultation_type=NoteProposalConsultationType.REVIEW_TEXT,
                base_opinion_version=1,
                base_opinion_sha256=first.opinion.content_sha256,
            ),
        )
        reviewed = await fixture.accept(review)
        note = reviewed.opinion.observations[0]
        assert note.bridge_draft_ref is not None
        assert note.bridge_review_ref is not None
        assert note.bridge_review_ref.invocation_id == review.bridge_invocation_id
        assert note.bridge_review_ref.reviewed_text_sha256 == _sha256(note.text.encode("utf-8"))

    _run(scenario())


# ---------------------------------------------------------------------------
# API surface
# ---------------------------------------------------------------------------


class _FakeInvocationService:
    def __init__(self, fixture: _Fixture, advisor: _ScriptedAdvisor) -> None:
        self.fixture = fixture
        self.advisor = advisor
        self.calls: list[dict[str, Any]] = []
        self._by_key: dict[str, ToolInvocation] = {}

    async def invoke(self, **kwargs: Any) -> OperatorToolInvocationResult:
        self.calls.append(dict(kwargs))
        idempotency_key = kwargs["idempotency_key"]
        existing = self._by_key.get(idempotency_key)
        if existing is not None:
            return OperatorToolInvocationResult(
                investigation_id=self.fixture.investigation.id,
                working_state_version=kwargs["expected_version"],
                working_state_sha256="d" * 64,
                invocation=existing,
                error_code=None,
                job_id=None,
                idempotent_replay=True,
                output={},
            )
        arguments = kwargs["arguments"]
        from cti_app.application.opinion_note_proposals import AdvisorDraftingRequestV1

        request = AdvisorDraftingRequestV1(
            investigation_id=self.fixture.investigation.id,
            consultation_type=NoteProposalConsultationType(arguments["consultation_type"]),
            anchor=arguments["anchor"],
            bridge_block_type=OpinionBlockType(arguments["block_type"]),
            question=arguments["question"],
            evidence_refs=tuple(UUID(item) for item in arguments.get("evidence_refs", ())),
            finding_refs=(),
            expected_working_state_version=kwargs["expected_version"],
            expected_working_state_sha256=kwargs.get("expected_sha256"),
            actor_id=kwargs["actor_id"],
            requester_actor_kind=kwargs["requester_actor_kind"],
            idempotency_key=idempotency_key,
            purpose=kwargs["purpose"],
            context_task_id=kwargs.get("context_task_id"),
        )
        async with self.fixture.uow_factory() as uow:
            outcome = await self.advisor.request_draft(uow, request)
        invocation = await self.fixture.invocations.get(outcome.invocation_id)
        assert invocation is not None
        self._by_key[idempotency_key] = invocation
        return OperatorToolInvocationResult(
            investigation_id=self.fixture.investigation.id,
            working_state_version=kwargs["expected_version"],
            working_state_sha256="d" * 64,
            invocation=invocation,
            error_code=None,
            job_id=None,
            idempotent_replay=False,
            output={},
        )


class _FakeCompositionFactory:
    def __init__(self, fixture: _Fixture, invocation_service: _FakeInvocationService) -> None:
        self.fixture = fixture
        self.invocation_service = invocation_service
        self.artifact_store = fixture.harness.store
        self.article_snapshots = SimpleNamespace()

    def build(self, uow: Any, investigation_id: UUID, *, operator_mode: bool = False) -> Any:
        del uow, investigation_id, operator_mode
        return SimpleNamespace(operator_tool_invocations=self.invocation_service)


class _SyncClient:
    def __init__(self, app: FastAPI) -> None:
        self.app = app

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        async def send() -> httpx.Response:
            async with httpx.AsyncClient(
                transport=ASGITransport(app=self.app), base_url="http://test"
            ) as client:
                return await client.request(method, path, **kwargs)

        return asyncio.run(send())

    def get(self, path: str, **kwargs: Any) -> httpx.Response:
        return self._request("GET", path, **kwargs)

    def post(self, path: str, **kwargs: Any) -> httpx.Response:
        return self._request("POST", path, **kwargs)


def _api(
    fixture: _Fixture, advisor: _ScriptedAdvisor
) -> tuple[_SyncClient, _FakeInvocationService]:
    invocation_service = _FakeInvocationService(fixture, advisor)
    app = FastAPI()
    app.include_router(note_proposals_router)
    app.state.uow_factory = fixture.uow_factory
    app.state.investigation_composition_factory = _FakeCompositionFactory(
        fixture, invocation_service
    )
    app.state.opinion_note_proposal_service = fixture.proposal_service
    app.state.analyst_opinion_service = fixture.opinion_service
    app.state.production_artifact_store = fixture.harness.store
    app.state.identity_provider = LocalIdentityProvider("analyst:test")
    app.state.settings = Settings(_env_file=None)
    return _SyncClient(app), invocation_service


def _request_body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "anchor": LEAD_ANCHOR,
        "target_block_type": "lead",
        "bridge_block_type": "key_findings",
        "expected_working_state_version": 1,
    }
    body.update(overrides)
    return body


def test_task_drafting_links_context_task() -> None:
    """F4-D B3: a note requested for a task links its advisor invocation."""
    fixture = _Fixture()
    advisor = fixture.advisor(text="Texte rédigé pour la tâche.")
    client, invocation_service = _api(fixture, advisor)
    task_id = uuid4()
    path = f"/api/investigations/{fixture.investigation.id}/note-proposals"

    response = client.post(
        path,
        json=_request_body(task_id=str(task_id)),
        headers={"Idempotency-Key": "api-task-drafting-1"},
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["proposal"]["task_id"] == str(task_id)
    assert invocation_service.calls[0]["tool_name"] == "advisor.consult"
    assert invocation_service.calls[0]["context_task_id"] == task_id
    assert advisor.requests[0].context_task_id == task_id


def test_api_request_ingest_diff_accept_chain() -> None:
    fixture = _Fixture()
    text = "Texte API rédigé par Bridge."
    client, invocation_service = _api(fixture, fixture.advisor(text=text))
    path = f"/api/investigations/{fixture.investigation.id}/note-proposals"
    response = client.post(path, json=_request_body(), headers={"Idempotency-Key": "api-request-1"})
    assert response.status_code == 200, response.text
    created = response.json()
    assert created["materialised"] is True
    assert created["proposal"]["proposed_text"] == text
    assert created["proposal"]["status"] == "proposed"
    assert invocation_service.calls[0]["tool_name"] == "advisor.consult"
    proposal_id = created["proposal"]["id"]

    listing = client.get(path)
    assert listing.status_code == 200
    assert [item["id"] for item in listing.json()] == [proposal_id]

    diff = client.get(f"{path}/{proposal_id}")
    assert diff.status_code == 200, diff.text
    payload = diff.json()
    assert payload["proposed_text"] == text
    assert payload["current_note_text"] is None
    assert payload["text_changed"] is True
    assert payload["accept_ready"] is True
    assert payload["anchor"] == LEAD_ANCHOR

    accept = client.post(
        f"{path}/{proposal_id}/accept",
        json={
            "expected_status_version": created["proposal"]["status_version"],
            "expected_base_version": None,
            "expected_base_sha256": None,
        },
        headers={"Idempotency-Key": "api-accept-1"},
    )
    assert accept.status_code == 200, accept.text
    accepted = accept.json()
    assert accepted["opinion_version"] == 1
    assert accepted["proposal"]["status"] == "accepted"
    assert accepted["note_id"] == str(note_proposal_note_id(UUID(proposal_id)))


def test_api_refuses_body_text_and_forged_provenance_fields() -> None:
    fixture = _Fixture()
    client, _ = _api(fixture, fixture.advisor())
    path = f"/api/investigations/{fixture.investigation.id}/note-proposals"
    forged_request = client.post(
        path,
        json=_request_body(
            text="Texte forgé.",
            bridge_invocation_id=str(uuid4()),
            bridge_response_observation_id=str(uuid4()),
        ),
        headers={"Idempotency-Key": "api-forged-request"},
    )
    assert forged_request.status_code == 422, forged_request.text

    proposal = _run(fixture.request(fixture.advisor(text="Texte légitime."))).proposal
    forged_accept = client.post(
        f"{path}/{proposal.id}/accept",
        json={
            "expected_status_version": proposal.status_version,
            "expected_base_version": None,
            "expected_base_sha256": None,
            "text": "Texte forgé.",
        },
        headers={"Idempotency-Key": "api-forged-accept"},
    )
    assert forged_accept.status_code == 422, forged_accept.text


def test_api_stale_accept_is_refused_without_overwrite() -> None:
    fixture = _Fixture()
    client, _ = _api(fixture, fixture.advisor(text="Note initiale."))
    path = f"/api/investigations/{fixture.investigation.id}/note-proposals"
    first = client.post(path, json=_request_body(), headers={"Idempotency-Key": "api-first"}).json()
    first_accept = client.post(
        f"{path}/{first['proposal']['id']}/accept",
        json={
            "expected_status_version": first["proposal"]["status_version"],
            "expected_base_version": None,
            "expected_base_sha256": None,
        },
        headers={"Idempotency-Key": "api-accept-first"},
    )
    assert first_accept.status_code == 200, first_accept.text
    base_version = first_accept.json()["opinion_version"]
    base_sha = first_accept.json()["opinion_sha256"]

    second = client.post(
        path,
        json=_request_body(
            anchor=SECTION_ANCHOR,
            target_block_type="section",
            base_opinion_version=base_version,
            base_opinion_sha256=base_sha,
        ),
        headers={"Idempotency-Key": "api-second"},
    ).json()
    second_accept = client.post(
        f"{path}/{second['proposal']['id']}/accept",
        json={
            "expected_status_version": second["proposal"]["status_version"],
            "expected_base_version": base_version,
            "expected_base_sha256": base_sha,
        },
        headers={"Idempotency-Key": "api-accept-second"},
    )
    assert second_accept.status_code == 200, second_accept.text
    stale = client.post(
        f"{path}/{second['proposal']['id']}/accept",
        json={
            "expected_status_version": second["proposal"]["status_version"],
            "expected_base_version": base_version,
            "expected_base_sha256": base_sha,
        },
        headers={"Idempotency-Key": "api-accept-second-retry"},
    )
    assert stale.status_code == 409, stale.text
    assert stale.json()["detail"]["code"] == "stale_note_proposal"


# ---------------------------------------------------------------------------
# F4-D B5a — typed note content blocks
# ---------------------------------------------------------------------------

_AFTER_PARAGRAPH_BLOCK_ID = UUID(int=777)


def _v9_publication_with_elements(
    *,
    tables: tuple[Any, ...] = (),
    charts: tuple[Any, ...] = (),
    diagrams: tuple[Any, ...] = (),
    source_figures: tuple[Any, ...] = (),
) -> tuple[Any, Any]:
    """Build a V9 article whose after-paragraph elements resolve to lead:0001."""
    snapshot, references, extraction, synthesis = _canonical_inputs()
    enrichment = _enrichment_with(
        extraction=extraction,
        synthesis=synthesis,
        tables=tables,
        charts=charts,
        diagrams=diagrams,
        source_figures=source_figures,
    )
    base = build_publication_document_v5(
        snapshot=snapshot,
        references=references,
        extraction=extraction,
        synthesis=synthesis,
        editorial_enrichment=enrichment,
    )
    placements: list[ResolvedPublicationPlacementV1] = []
    position = 0
    for kind, elements in (
        ("table", base.document.tables),
        ("chart", base.document.charts),
        ("diagram", base.document.diagrams),
        ("figure", base.document.figures),
    ):
        for element in elements:
            if element.placement.kind is EnrichmentPlacementKind.AFTER_PARAGRAPH:
                placements.append(
                    ResolvedPublicationPlacementV1(
                        element_kind=kind,
                        element_key=element.key,
                        element_position=position,
                        anchor_block_id=_AFTER_PARAGRAPH_BLOCK_ID,
                        anchor_pointer="/lead/0",
                        anchor_canonical_position=0,
                        paragraph_kind="lead",
                        section_index=None,
                        paragraph_index=0,
                    )
                )
            position += 1
    v9 = PublicationDocumentV9(
        base_publication=PublicationDocumentV7(
            base_publication=_v6(base),
            publication_kind=PublicationKind.ARTICLE,
            routing_decision_hash="a" * 64,
            routing_policy_version="routing-policy-v1",
        ),
        article_format=build_article_format_v1(base, extraction),
        registry_hash="b" * 64,
        synthesis_canonical_sha256="c" * 64,
        enrichment_canonical_sha256="d" * 64,
        resolved_placements=tuple(placements),
    )
    return v9, base


def _after_paragraph_table(key: str = "note-linked-table") -> Any:
    _, _, extraction, _ = _canonical_inputs()
    return replace(
        _enrichment_table(extraction_evidence_refs_v1(extraction)[0], key=key),
        placement=EnrichmentPlacementV1(
            EnrichmentPlacementKind.AFTER_PARAGRAPH,
            anchor_block_id=_AFTER_PARAGRAPH_BLOCK_ID,
        ),
    )


def _append_observation(
    fixture: _Fixture,
    raw: bytes,
    *,
    source: str = "strings",
    object_type: str = "file",
    object_id: str = "sample.bin",
    investigation_id: UUID | None = None,
) -> EvidenceObservation:
    blob_id = uuid4()
    fixture.harness.store.blobs[blob_id] = raw
    observation = EvidenceObservation(
        investigation_id=investigation_id or fixture.investigation.id,
        kind=EvidenceKind.OTHER,
        blob_id=blob_id,
        sha256=_sha256(raw),
        source=source,
        source_version="test.v1",
        observed_at=NOW,
        object_type=object_type,
        object_id=object_id,
    )
    _run(fixture.observations.append_if_absent(observation))
    return observation


def _opinion_from_blocks(blocks: tuple[OpinionBlock, ...]) -> AnalystOpinionV1:
    return AnalystOpinionV1(
        investigation_id=uuid4(),
        synthesis_artifact_id=uuid4(),
        synthesis_sha256="1" * 64,
        base_publication_artifact_id=uuid4(),
        base_publication_sha256="2" * 64,
        blocks=blocks,
        author_id="analyst:author",
    )


def test_article_element_ref_pinned_and_stale_refused() -> None:
    """B5a: an M23 element ref resolves against the pinned V9 base, never a copy."""
    fixture = _Fixture()
    v9, base = _v9_publication_with_elements(tables=(_after_paragraph_table(),))
    fixture.install_document(v9)
    published = base.document.tables[0]
    element_sha256 = publication_element_sha256(base.document, kind="table", key=published.key)
    block = ArticleElementNoteBlockV1(
        kind=NoteArticleElementKind.TABLE,
        element_key=published.key,
        element_sha256=element_sha256,
        anchor=LEAD_ANCHOR,
    )
    proposal = _materialised_proposal(
        fixture, _run(fixture.request(fixture.advisor(), content_blocks=(block,)))
    )
    assert proposal.content_blocks == (block,)
    assert proposal.base_publication_sha256 == _sha256(fixture.harness.base_bytes)

    diff = _run(fixture.proposal_service.get(fixture.investigation.id, proposal.id))
    assert diff.content_blocks_valid is True
    resolved = diff.resolved_content_blocks[0]
    assert isinstance(resolved, ResolvedArticleElementBlockV1)
    assert resolved.kind is NoteArticleElementKind.TABLE
    assert resolved.element_sha256 == element_sha256
    assert resolved.anchor == LEAD_ANCHOR
    assert resolved.title == published.title

    _run(fixture.accept(proposal))
    head = _run(_head(fixture))
    note = next(item for item in head.observations if item.anchor == LEAD_ANCHOR)
    assert note.content_blocks == (block,)
    projection = next(
        item for item in head.blocks if item.block_type is OpinionBlockType.ANALYST_OBSERVATIONS
    )
    payload = json.loads(projection.text)
    assert payload["schema"] == "analyst-observations.v2"
    assert payload["items"][0]["content_blocks"] == [block.to_payload()]

    for stale in (
        replace(block, element_sha256="0" * 64),
        replace(block, anchor=SECTION_ANCHOR),
    ):
        advisor = fixture.advisor()
        with pytest.raises(NoteProposalError) as failure:
            _run(fixture.request(advisor, content_blocks=(stale,)))
        assert failure.value.code is NoteProposalErrorCode.NOTE_BLOCK_REF_STALE
        assert advisor.requests == []


def test_code_ref_matches_observation_bytes() -> None:
    """B5a: code is sliced from the observation blob, never supplied by Bridge."""
    raw = b"xxCreateFileWyy"
    fixture = _Fixture()
    observation = _append_observation(
        fixture,
        raw,
        source="native_analysis",
        object_type="native_analysis",
        object_id="sample.bin",
    )
    excerpt = raw[2:13]
    block = CanonicalCodeNoteBlockV1(
        observation_id=observation.id,
        blob_sha256=observation.sha256,
        offset=2,
        length=len(excerpt),
        excerpt_sha256=_sha256(excerpt),
        language="c",
    )
    proposal = _materialised_proposal(
        fixture, _run(fixture.request(fixture.advisor(), content_blocks=(block,)))
    )
    diff = _run(fixture.proposal_service.get(fixture.investigation.id, proposal.id))
    resolved = diff.resolved_content_blocks[0]
    assert isinstance(resolved, ResolvedCanonicalCodeBlockV1)
    assert resolved.excerpt == "CreateFileW"
    assert resolved.excerpt_sha256 == _sha256(excerpt)
    assert resolved.observation_id == observation.id

    _run(fixture.accept(proposal))
    head = _run(_head(fixture))
    assert head.observations[0].content_blocks == (block,)

    for stale in (
        replace(block, excerpt_sha256="f" * 64),
        replace(block, blob_sha256="e" * 64),
        replace(block, offset=len(raw) - 1),
    ):
        advisor = fixture.advisor()
        with pytest.raises(NoteProposalError) as failure:
            _run(fixture.request(advisor, content_blocks=(stale,)))
        assert failure.value.code is NoteProposalErrorCode.NOTE_CODE_REF_STALE
        assert advisor.requests == []


def test_acceptance_revalidates_code_block_against_observation_bytes() -> None:
    """B5a: acceptance re-reads the blob; tampered bytes are refused, not published."""
    raw = b"cmd.exe /c whoami"
    fixture = _Fixture()
    observation = _append_observation(fixture, raw)
    block = CanonicalCodeNoteBlockV1(
        observation_id=observation.id,
        blob_sha256=observation.sha256,
        offset=0,
        length=len(raw),
        excerpt_sha256=_sha256(raw),
        language="text",
    )
    proposal = _materialised_proposal(
        fixture, _run(fixture.request(fixture.advisor(), content_blocks=(block,)))
    )
    fixture.harness.store.blobs[observation.blob_id] = b"tampered bytes"
    with pytest.raises(OpinionError) as failure:
        _run(fixture.accept(proposal))
    assert failure.value.code is OpinionErrorCode.NOTE_CODE_REF_STALE
    assert _run(_head(fixture)) is None


def test_ioc_ref_rejects_advisory_observation() -> None:
    """B5a: IOC rows come only from canonical, non-advisory observations."""
    fixture = _Fixture()
    advisory = _append_observation(
        fixture,
        b'{"advisor":true}',
        source="advisor.consult",
        object_type="advisor_call",
        investigation_id=fixture.investigation.id,
    )
    block = IocTableNoteBlockV1(observation_ids=(advisory.id,), title=note_ioc_table_title(1))
    advisor = fixture.advisor()
    with pytest.raises(NoteProposalError) as failure:
        _run(fixture.request(advisor, content_blocks=(block,)))
    assert failure.value.code is NoteProposalErrorCode.NOTE_IOC_REF_UNRESOLVED
    assert advisor.requests == []

    foreign = _append_observation(fixture, b"foreign", investigation_id=uuid4())
    with pytest.raises(NoteProposalError) as failure:
        _run(
            fixture.request(
                fixture.advisor(),
                content_blocks=(
                    IocTableNoteBlockV1(
                        observation_ids=(foreign.id,), title=note_ioc_table_title(1)
                    ),
                ),
            )
        )
    assert failure.value.code is NoteProposalErrorCode.NOTE_IOC_REF_UNRESOLVED

    canonical = _append_observation(
        fixture,
        b"canonical evidence",
        source="VirusTotal",
        object_type="file",
        object_id="a" * 64,
    )
    valid = IocTableNoteBlockV1(observation_ids=(canonical.id,), title=note_ioc_table_title(1))
    proposal = _materialised_proposal(
        fixture, _run(fixture.request(fixture.advisor(), content_blocks=(valid,)))
    )
    diff = _run(fixture.proposal_service.get(fixture.investigation.id, proposal.id))
    resolved = diff.resolved_content_blocks[0]
    assert isinstance(resolved, ResolvedIocTableBlockV1)
    assert resolved.title == "Tableau IOC (1)"
    assert resolved.rows[0].observation_id == canonical.id
    assert resolved.rows[0].object_type == "file"
    assert resolved.rows[0].object_id == "a" * 64
    assert resolved.rows[0].source == "VirusTotal"
    assert resolved.rows[0].blob_sha256 == canonical.sha256


def test_ioc_ref_unresolved_refused() -> None:
    """B5a: one missing observation in an IOC table is a typed refusal."""
    context = NoteContentBlockContextV1(
        investigation_id=uuid4(),
        anchor=LEAD_ANCHOR,
        elements=(),
        observations={},
        blobs={},
        protocol_figures=(),
    )
    block = IocTableNoteBlockV1(observation_ids=(uuid4(),), title=note_ioc_table_title(1))
    with pytest.raises(NoteProposalError) as failure:
        validate_note_content_blocks((block,), context)
    assert failure.value.code is NoteProposalErrorCode.NOTE_IOC_REF_UNRESOLVED


def test_block_chart_without_evidence_refused() -> None:
    """B5a: a chart whose points carry no evidence is refused."""
    investigation_id = uuid4()
    chart_fact = NoteArticleElementFactV1(
        kind="chart",
        key="chart-1",
        element_sha256="a" * 64,
        anchor=LEAD_ANCHOR,
        evidence_ref_count=0,
        source_document_known=True,
        title="Chart",
        caption=None,
    )
    block = ArticleElementNoteBlockV1(
        kind=NoteArticleElementKind.CHART,
        element_key="chart-1",
        element_sha256="a" * 64,
        anchor=LEAD_ANCHOR,
    )

    def context(fact: NoteArticleElementFactV1) -> NoteContentBlockContextV1:
        return NoteContentBlockContextV1(
            investigation_id=investigation_id,
            anchor=LEAD_ANCHOR,
            elements=(fact,),
            observations={},
            blobs={},
            protocol_figures=(),
        )

    with pytest.raises(NoteProposalError) as failure:
        validate_note_content_blocks((block,), context(chart_fact))
    assert failure.value.code is NoteProposalErrorCode.NOTE_CHART_WITHOUT_EVIDENCE
    evidenced = replace(chart_fact, evidence_ref_count=1)
    assert validate_note_content_blocks((block,), context(evidenced))


def test_block_figure_with_unknown_source_refused() -> None:
    """B5a: a figure must name a source document known to the article."""
    figure_fact = NoteArticleElementFactV1(
        kind="figure",
        key="figure-1",
        element_sha256="b" * 64,
        anchor=LEAD_ANCHOR,
        evidence_ref_count=0,
        source_document_known=False,
        title=None,
        caption="Figure 1",
    )
    block = ArticleElementNoteBlockV1(
        kind=NoteArticleElementKind.FIGURE,
        element_key="figure-1",
        element_sha256="b" * 64,
        anchor=LEAD_ANCHOR,
    )

    def context(fact: NoteArticleElementFactV1) -> NoteContentBlockContextV1:
        return NoteContentBlockContextV1(
            investigation_id=uuid4(),
            anchor=LEAD_ANCHOR,
            elements=(fact,),
            observations={},
            blobs={},
            protocol_figures=(),
        )

    with pytest.raises(NoteProposalError) as failure:
        validate_note_content_blocks((block,), context(figure_fact))
    assert failure.value.code is NoteProposalErrorCode.NOTE_FIGURE_SOURCE_MISSING
    resolved = validate_note_content_blocks(
        (block,), context(replace(figure_fact, source_document_known=True))
    )
    assert isinstance(resolved[0], ResolvedArticleElementBlockV1)
    assert resolved[0].caption == "Figure 1"


def test_note_payload_v2_preserves_v1_readability() -> None:
    """B5a: v1 stays byte-identical; v2 adds content_blocks and stays readable."""
    note = AnalystObservationV1(
        id=uuid4(),
        anchor=LEAD_ANCHOR,
        text="Note historique sans bloc.",
        state=AnalystObservationState.UNRESOLVED,
        relation=AnalystObservationRelation.UNRESOLVED,
    )
    v1_blocks = opinion_blocks_with_observations((), (note,))
    assert json.loads(v1_blocks[-1].text) == {
        "schema": "analyst-observations.v1",
        "items": [note.to_payload()],
    }
    assert _opinion_from_blocks(v1_blocks).observations == (note,)

    observation_id = uuid4()
    block = IocTableNoteBlockV1(observation_ids=(observation_id,), title=note_ioc_table_title(1))
    second = replace(note, id=uuid4(), anchor=SECTION_ANCHOR, text="Note B.")
    note_with_block = replace(note, content_blocks=(block,))
    v2_blocks = opinion_blocks_with_observations((), (note_with_block, second))
    payload = json.loads(v2_blocks[-1].text)
    assert payload["schema"] == "analyst-observations.v2"
    assert payload["items"][0]["content_blocks"] == [block.to_payload()]
    assert payload["items"][1]["content_blocks"] == []
    opinion = _opinion_from_blocks(v2_blocks)
    assert opinion.observations == (note_with_block, second)
    assert opinion.content_sha256 == hashlib.sha256(opinion.canonical_bytes()).hexdigest()

    # A v1 projection may never smuggle blocks in place of an older version.
    forged = OpinionBlock(
        block_type=OpinionBlockType.ANALYST_OBSERVATIONS,
        text=canonical_json_bytes(
            {"schema": "analyst-observations.v1", "items": [note_with_block.to_payload()]}
        ).decode("utf-8"),
    )
    with pytest.raises(ValueError, match="v1"):
        _opinion_from_blocks((forged,))


def test_protocol_figure_block_resolves_against_the_opinion() -> None:
    """B5a: a protocol figure ref must match one view already carried by the opinion."""
    from cti_app.domain.analyst_opinion import (
        OpinionProtocolFigureV1,
        ProtocolFigureNoteBlockV1,
    )
    from cti_app.domain.protocol_model import ProtocolDiagramKind

    figure = OpinionProtocolFigureV1(
        kind=ProtocolDiagramKind.SEQUENCE,
        model_sha256="1" * 64,
        spec_sha256="2" * 64,
        asset_id=uuid4(),
        asset_sha256="3" * 64,
        preview_sha256="4" * 64,
        pin_sha256="5" * 64,
        compiler_name="compiler",
        compiler_version="1",
        compiler_policy_version="v1",
        caption="Séquence protocolaire",
    )
    block = ProtocolFigureNoteBlockV1(kind=ProtocolDiagramKind.SEQUENCE, asset_sha256="3" * 64)
    context = NoteContentBlockContextV1(
        investigation_id=uuid4(),
        anchor=LEAD_ANCHOR,
        elements=(),
        observations={},
        blobs={},
        protocol_figures=(figure,),
    )
    resolved = validate_note_content_blocks((block,), context)
    assert resolved[0].caption == "Séquence protocolaire"
    with pytest.raises(NoteProposalError) as failure:
        validate_note_content_blocks((replace(block, asset_sha256="9" * 64),), context)
    assert failure.value.code is NoteProposalErrorCode.NOTE_BLOCK_REF_STALE


def test_note_content_blocks_idempotent_replay_and_conflict() -> None:
    """B5a: the idempotency key covers the blocks, exactly like the rest of the body."""
    fixture = _Fixture()
    canonical = _append_observation(fixture, b"canonical evidence")
    block = IocTableNoteBlockV1(observation_ids=(canonical.id,), title=note_ioc_table_title(1))
    advisor = fixture.advisor()
    first = _run(
        fixture.request(advisor, content_blocks=(block,), idempotency_key="note-blocks-idem")
    )
    assert first.idempotent_replay is False
    replay = _run(
        fixture.request(advisor, content_blocks=(block,), idempotency_key="note-blocks-idem")
    )
    assert replay.idempotent_replay is True
    assert replay.proposal.id == first.proposal.id
    assert len(advisor.requests) == 1
    with pytest.raises(NoteProposalError) as failure:
        _run(
            fixture.request(
                fixture.advisor(), content_blocks=(), idempotency_key="note-blocks-idem"
            )
        )
    assert failure.value.code is NoteProposalErrorCode.IDEMPOTENCY_CONFLICT


def test_api_note_content_blocks_roundtrip_and_forged_excerpt_refused() -> None:
    """B5a: the route echoes blocks and the diff resolves them; forged text is 422."""
    fixture = _Fixture()
    raw = b"push ebp\nmov ebp, esp\n"
    observation = _append_observation(fixture, raw, object_type="native_analysis")
    block = CanonicalCodeNoteBlockV1(
        observation_id=observation.id,
        blob_sha256=observation.sha256,
        offset=0,
        length=len(raw),
        excerpt_sha256=_sha256(raw),
        language="asm",
    )
    client, _ = _api(fixture, fixture.advisor(text="Texte API avec code vérifié."))
    path = f"/api/investigations/{fixture.investigation.id}/note-proposals"
    response = client.post(
        path,
        json=_request_body(content_blocks=[block.to_payload()]),
        headers={"Idempotency-Key": "api-content-blocks"},
    )
    assert response.status_code == 200, response.text
    created = response.json()
    assert created["proposal"]["content_blocks"] == [block.to_payload()]
    diff = client.get(f"{path}/{created['proposal']['id']}")
    assert diff.status_code == 200, diff.text
    resolved = diff.json()["resolved_content_blocks"]
    assert resolved[0]["excerpt"] == raw.decode()
    assert resolved[0]["excerpt_sha256"] == _sha256(raw)

    forged = client.post(
        path,
        json=_request_body(content_blocks=[{**block.to_payload(), "excerpt": "push ebp (forgé)"}]),
        headers={"Idempotency-Key": "api-content-blocks-forged"},
    )
    assert forged.status_code == 422, forged.text
