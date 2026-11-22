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
    NoteProposalError,
    NoteProposalErrorCode,
    OpinionNoteProposalService,
)
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.config import Settings
from cti_app.domain.analyst_opinion import (
    AnalystObservationRelation,
    AnalystObservationState,
    OpinionBlockType,
    investigation_opinion_id,
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
from cti_app.domain.production import ProseAuthority
from cti_app.domain.publication import (
    PublicationEvidenceKind,
    PublicationEvidenceRefV1,
    PublicationParagraphV1,
    PublicationSectionKind,
    PublicationSectionV1,
)
from cti_app.domain.publication_document import serialize_publication_document
from tests.test_analyst_opinion_service import OpinionHarness, make_opinion_harness
from tests.test_publication_document_v6 import _base_v5

NOW = datetime(2026, 11, 22, 12, 0, tzinfo=UTC)
LEAD_ANCHOR = "lead:0001"
SECTION_ANCHOR = "section:0:paragraph:0001"


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def base_v5_with_section() -> Any:
    """The shared publication fixture with one section anchor available."""
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
    body = replace(
        base.document,
        sections=(
            PublicationSectionV1(
                kind=PublicationSectionKind.CAMPAIGN,
                heading="Campagne",
                paragraphs=(
                    PublicationParagraphV1(
                        text="Le paragraphe de section décrit la campagne observée.",
                        evidence_refs=(
                            PublicationEvidenceRefV1(
                                source_document_id=UUID("00000000-0000-0000-0000-000000000002"),
                                kind=PublicationEvidenceKind.FACT,
                                evidence_key="a" * 64,
                            ),
                        ),
                    ),
                ),
            ),
        ),
    )
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
    def __init__(self) -> None:
        harness = make_opinion_harness()
        self._extend_base_with_section(harness)
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
    def _extend_base_with_section(harness: OpinionHarness) -> None:
        extended = base_v5_with_section()
        base_bytes = ProductionArtifactStore.canonical_json_bytes(
            serialize_publication_document(extended)
        )
        harness.store.blobs[harness.base_artifact.canonical_blob_id] = base_bytes
        base_sha256 = _sha256(base_bytes)
        harness.artifacts.items = [
            replace(item, input_hash=base_sha256) if item.id == harness.base_artifact.id else item
            for item in harness.artifacts.items
        ]
        harness.base_artifact = replace(harness.base_artifact, input_hash=base_sha256)
        harness.base_bytes = base_bytes

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
        anchored_text: str | None = None,
        response: bool = True,
        advisor_status: str = "succeeded",
        usable: bool = True,
        real_provenance: bool = True,
        review_usable: bool = True,
    ) -> None:
        self.fixture = fixture
        self.text = text
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
                    "sentences": [{"text": text}],
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
