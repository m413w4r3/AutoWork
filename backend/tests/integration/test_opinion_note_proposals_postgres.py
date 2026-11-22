"""PostgreSQL proof of the M24-08B note-proposal chain.

The real advisor tool persists its call and response-link observations through
the real PostgreSQL repositories from a **scripted** Bridge transport (no live
provider).  The chain under test is: request -> ingest -> diff -> accept ->
second note on another anchor -> correction -> stale refusal, plus a Bridge
outage that leaves the request pending and unacceptable.
"""

from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import UTC, date, datetime
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4

import pytest

from cti_app.application.analyst_opinion_service import (
    AnalystOpinionService,
    OpinionError,
    OpinionErrorCode,
)
from cti_app.application.bridge_investigator import (
    TurnSubmission,
    TurnSubmissionStatus,
)
from cti_app.application.investigation_advisor_tools import InvestigationAdvisorTool
from cti_app.application.opinion_note_proposals import (
    AdvisorDraftingOutcomeV1,
    NoteProposalError,
    NoteProposalErrorCode,
    OpinionNoteProposalService,
    advisor_consult_arguments,
)
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.domain.analyst_opinion import (
    AnalystObservationRelation,
    AnalystObservationState,
    OpinionBlockType,
)
from cti_app.domain.blobs import BlobDescriptor, BlobRecord
from cti_app.domain.classification import TLP
from cti_app.domain.editions import Edition
from cti_app.domain.entities import Subject
from cti_app.domain.investigation import (
    EvidenceKind,
    EvidenceObservation,
    FindingAssessment,
    FindingKind,
    InvestigationFinding,
    ToolInvocation,
)
from cti_app.domain.opinion_note_proposal import (
    NoteProposalConsultationType,
    NoteProposalStatus,
    NoteTargetBlockType,
)
from cti_app.domain.production import (
    AnalystInvestigation,
    LoopBudget,
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionArtifactStatus,
    ProductionRun,
    ProseAuthority,
)
from cti_app.domain.production_synthesis import production_synthesis_to_json
from cti_app.domain.publication_document import serialize_publication_document
from cti_app.infrastructure.database.session import (
    create_postgres_engine,
    create_session_factory,
)
from cti_app.infrastructure.database.uow import SqlAlchemyUnitOfWork
from tests.integration.edition_codes import reserve_edition_code
from tests.test_investigation_advisor_tools import Registry, make_context
from tests.test_opinion_note_proposals import base_v5_with_section
from tests.test_publication_builder_v4 import _canonical_inputs

pytestmark = pytest.mark.integration

LEAD_ANCHOR = "lead:0001"
SECTION_ANCHOR = "section:0:paragraph:0001"


class _Store:
    def __init__(self, uow_factory: Any) -> None:
        self._uow_factory = uow_factory
        self.blobs: dict[UUID, bytes] = {}

    async def read_bytes(self, blob_id: UUID, *, max_bytes: int | None = None) -> bytes:
        content = self.blobs[blob_id]
        if max_bytes is not None and len(content) > max_bytes:
            raise ValueError("Blob exceeds the requested read limit")
        return content

    async def put_bytes(self, content: bytes, *, bucket: str, mime_type: str) -> UUID:
        sha256 = hashlib.sha256(content).hexdigest()
        record = BlobRecord(
            descriptor=BlobDescriptor(
                sha256=sha256,
                size=len(content),
                mime_type=mime_type,
                logical_bucket=bucket,
            )
        )
        async with self._uow_factory() as uow:
            stored = cast(BlobRecord, await uow.blobs.add_if_absent(record))
            await uow.commit()
        self.blobs[stored.id] = content
        return stored.id

    async def put_canonical_json(self, payload: dict[str, Any], *, bucket: str) -> tuple[UUID, str]:
        encoded = ProductionArtifactStore.canonical_json_bytes(payload)
        blob_id = await self.put_bytes(encoded, bucket=bucket, mime_type="application/json")
        return blob_id, hashlib.sha256(encoded).hexdigest()


class _Snapshots:
    def __init__(self, subject_id: UUID, synthesis_sha256: str) -> None:
        self._snapshot = SimpleNamespace(
            subject_id=subject_id,
            external_llm_allowed=True,
            do_not_submit=False,
            tlp="clear",
            synthesis_sha256=synthesis_sha256,
            article=SimpleNamespace(
                short_summary="Résumé synthétique de l'article.",
                open_questions=(),
            ),
            article_passages=(
                SimpleNamespace(
                    claim_id="S1P1",
                    heading="Résumé",
                    text="S1P1. L'article décrit la campagne observable.",
                ),
            ),
        )

    async def resolve(self, synthesis_artifact_id: UUID) -> Any:
        del synthesis_artifact_id
        return self._snapshot


class _ScriptedTransport:
    def __init__(self, responses: list[TurnSubmission]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def submit(self, **kwargs: Any) -> TurnSubmission:
        self.calls.append(kwargs)
        return self.responses.pop(0)


def _wire(text: str) -> str:
    return (
        "advisor-wire.v1\n"
        "TYPE: draft_text\n"
        "STATUS: succeeded\n"
        "UNIT: U1\n"
        f"S: {text}\n"
        "LIMITATION: aucune mesure additionnelle."
    )


def _submission(text: str | None) -> TurnSubmission:
    return TurnSubmission(
        TurnSubmissionStatus.SUCCEEDED,
        output_text=_wire(text) if text is not None else None,
        bridge_invocation_id=str(uuid4()),
    )


class _ScriptedAdvisorPort:
    """Route the drafting question through the real tool and a scripted transport."""

    def __init__(
        self,
        *,
        store: _Store,
        snapshots: _Snapshots,
        transport: _ScriptedTransport,
    ) -> None:
        self._store = store
        self._snapshots = snapshots
        self._transport = transport

    async def request_draft(
        self, uow: SqlAlchemyUnitOfWork, request: Any
    ) -> AdvisorDraftingOutcomeV1:
        none_get = SimpleNamespace(get=_none_async)
        tool = InvestigationAdvisorTool(
            transport=self._transport,
            settings=SimpleNamespace(
                advisor_disclosure_profile="standard",
                openai_bridge_min_submission_interval_seconds=0,
                openai_bridge_submission_jitter_fraction=0,
                openai_bridge_wait_timeout_research_seconds=7_200,
                investigation_max_tool_seconds=3_600,
                operator_advisor_checkpoint_tool_threshold=12,
            ),
            investigations=uow.analyst_investigations,
            observations=uow.evidence_observations,
            findings=uow.investigation_findings,
            investigation_states=none_get,
            opinions=uow.analyst_opinions,
            sample_associations=uow.sample_associations,
            samples=uow.samples,
            blobs=uow.blobs,
            article_snapshots=self._snapshots,
            artifact_store=self._store,
            registry=Registry(),
        )
        context = make_context(request.investigation_id, key=f"advisor:{uuid4()}")
        result = await tool.execute(advisor_consult_arguments(request), context)
        now = datetime.now(UTC)
        invocation = ToolInvocation(
            investigation_id=request.investigation_id,
            operation_key=f"advisor-{uuid4()}",
            tool_name="advisor.consult",
            schema_version="advisor-consult.v1",
            normalized_parameters={
                "consultation_type": request.consultation_type.value,
                "anchor": request.anchor,
            },
            purpose=request.purpose,
            status=result.status,
            response_refs=tuple(result.observation_refs),
            created_at=now,
            updated_at=now,
        )
        await uow.tool_invocations.add_if_absent(invocation)
        return AdvisorDraftingOutcomeV1(
            invocation_id=invocation.id,
            invocation_status=invocation.status,
            response_observation_ids=tuple(result.observation_refs),
            idempotent_replay=False,
        )


async def _none_async(_key: UUID) -> None:
    return None


def _request_kwargs(
    *,
    anchor: str,
    target: NoteTargetBlockType,
    advisor: _ScriptedAdvisorPort,
    base_version: int | None,
    base_sha256: str | None,
    key: str,
) -> dict[str, Any]:
    return {
        "anchor": anchor,
        "target_block_type": target,
        "bridge_block_type": OpinionBlockType.KEY_FINDINGS,
        "consultation_type": NoteProposalConsultationType.DRAFT_TEXT,
        "note_state": AnalystObservationState.UNRESOLVED,
        "note_relation": AnalystObservationRelation.UNRESOLVED,
        "base_opinion_version": base_version,
        "base_opinion_sha256": base_sha256,
        "task_id": None,
        "question": None,
        "expected_working_state_version": 1,
        "expected_working_state_sha256": None,
        "actor_id": "analyst:integration",
        "requester_actor_kind": _human_actor_kind(),
        "idempotency_key": key,
        "advisor": advisor,
    }


def _human_actor_kind() -> Any:
    from cti_app.domain.investigation import ToolInvocationActorKind

    return ToolInvocationActorKind.HUMAN


async def _accept(
    opinion_service: AnalystOpinionService,
    investigation_id: UUID,
    proposal: Any,
    *,
    key: str,
) -> Any:
    return await opinion_service.accept_note_proposal(
        investigation_id,
        proposal.id,
        expected_status_version=proposal.status_version,
        expected_base_version=proposal.base_opinion_version,
        expected_base_sha256=proposal.base_opinion_sha256,
        actor="analyst:integration",
        reason="Accept the Bridge note proposal",
        idempotency_key=key,
    )


@pytest.mark.asyncio
async def test_note_proposal_chain_on_postgres(migrated_postgres_url: str) -> None:
    engine = create_postgres_engine(migrated_postgres_url)
    session_factory = create_session_factory(engine)

    def uow_factory() -> SqlAlchemyUnitOfWork:
        return SqlAlchemyUnitOfWork(session_factory)

    base = base_v5_with_section()
    subject_id = uuid4()
    base = replace(base, document=replace(base.document, subject_id=subject_id))
    base_bytes = ProductionArtifactStore.canonical_json_bytes(serialize_publication_document(base))
    _, _, _, synthesis = _canonical_inputs()
    synthesis = replace(
        synthesis,
        subject_id=subject_id,
        title=base.document.title,
        publication_language=base.document.publication_language,
    )
    synthesis_bytes = ProductionArtifactStore.canonical_json_bytes(
        production_synthesis_to_json(synthesis)
    )
    suffix = uuid4().hex
    edition = Edition(
        country=f"Note proposal {suffix}",
        country_code=reserve_edition_code(),
        period_start=date(2026, 11, 1),
        period_end=date(2026, 11, 30),
        tlp=TLP.AMBER,
        languages=("en",),
    )
    subject = Subject(
        id=subject_id,
        edition_id=edition.id,
        title="Note proposal integration fixture",
        slug=f"note-proposal-{suffix}",
        tlp=TLP.AMBER,
    )
    run = ProductionRun(subject_id=subject.id, edition_id=edition.id)
    store = _Store(uow_factory)
    base_blob_id = await store.put_bytes(
        base_bytes, bucket="note-proposal-base", mime_type="application/json"
    )
    synthesis_blob_id = await store.put_bytes(
        synthesis_bytes, bucket="note-proposal-synthesis", mime_type="text/plain"
    )
    observation_bytes = b"note proposal integration observation"
    observation_blob_id = await store.put_bytes(
        observation_bytes, bucket="note-proposal-observation", mime_type="text/plain"
    )
    base_artifact = ProductionArtifact(
        production_run_id=run.id,
        subject_id=subject.id,
        stage=ProductionArtifactStage.PUBLICATION,
        version=1,
        input_hash=hashlib.sha256(base_bytes).hexdigest(),
        canonical_blob_id=base_blob_id,
        status=ProductionArtifactStatus.VERIFIED,
    )
    synthesis_artifact = ProductionArtifact(
        production_run_id=run.id,
        subject_id=subject.id,
        stage=ProductionArtifactStage.SYNTHESIS,
        version=1,
        input_hash=hashlib.sha256(synthesis_bytes).hexdigest(),
        canonical_blob_id=synthesis_blob_id,
        status=ProductionArtifactStatus.VERIFIED,
    )
    investigation = AnalystInvestigation(
        production_run_id=run.id,
        subject_id=subject.id,
        synthesis_artifact_id=synthesis_artifact.id,
        budget=LoopBudget(),
        prose_authority=ProseAuthority.BRIDGE_ONLY,
    )
    observation = EvidenceObservation(
        investigation_id=investigation.id,
        kind=EvidenceKind.BINARY_ANALYSIS,
        blob_id=observation_blob_id,
        sha256=hashlib.sha256(observation_bytes).hexdigest(),
        source="fixture-static-analysis",
        source_version="1",
        observed_at=datetime.now(UTC),
        object_type="sha256",
        object_id=hashlib.sha256(b"harmless note-proposal identity").hexdigest(),
        limitations=("Static observation only.",),
    )
    finding = InvestigationFinding(
        investigation_id=investigation.id,
        kind=FindingKind.CAPABILITY,
        assessment=FindingAssessment.SUPPORTED,
        statement="The fixture supports an anchored note.",
        observation_refs=(observation.id,),
        limitations=("Execution was not observed.",),
    )
    try:
        async with uow_factory() as uow:
            assert await uow.editions.add_if_absent(edition)
            await uow.subjects.add(subject)
            await uow.production_runs.add(run)
            await uow.production_artifacts.append(synthesis_artifact)
            await uow.production_artifacts.append(base_artifact)
            await uow.analyst_investigations.add(investigation)
            await uow.evidence_observations.append_if_absent(observation)
            await uow.investigation_findings.append_version(finding, expected_previous_id=None)
            await uow.commit()

        transport = _ScriptedTransport(
            [
                _submission("Note initiale rédigée par Bridge."),
                _submission("Note de section rédigée par Bridge."),
                _submission("Correction du chapeau par Bridge."),
                _submission("Deuxième note de section."),
                _submission(None),
            ]
        )
        advisor = _ScriptedAdvisorPort(
            store=store,
            snapshots=_Snapshots(subject.id, hashlib.sha256(synthesis_bytes).hexdigest()),
            transport=transport,
        )
        proposal_service = OpinionNoteProposalService(uow_factory=uow_factory, artifact_store=store)
        opinion_service = AnalystOpinionService(uow_factory=uow_factory, artifact_store=store)

        first = await proposal_service.request(
            investigation.id,
            **_request_kwargs(
                anchor=LEAD_ANCHOR,
                target=NoteTargetBlockType.LEAD,
                advisor=advisor,
                base_version=None,
                base_sha256=None,
                key="pg-request-1",
            ),
        )
        ingested = await proposal_service.ingest_response(
            investigation.id,
            first.proposal.id,
            expected_status_version=first.proposal.status_version,
            actor="system:integration",
        )
        assert ingested.materialised is True
        assert ingested.proposed_text == "Note initiale rédigée par Bridge."
        diff = await proposal_service.get(investigation.id, first.proposal.id)
        assert diff.accept_ready is True
        assert diff.proposal.proposed_text == "Note initiale rédigée par Bridge."
        assert diff.current_note_text is None
        first_acceptance = await _accept(
            opinion_service, investigation.id, ingested, key="pg-accept-1"
        )
        assert first_acceptance.opinion.version == 1
        assert len(first_acceptance.opinion.observations) == 1

        second = await proposal_service.request(
            investigation.id,
            **_request_kwargs(
                anchor=SECTION_ANCHOR,
                target=NoteTargetBlockType.SECTION,
                advisor=advisor,
                base_version=1,
                base_sha256=first_acceptance.opinion.content_sha256,
                key="pg-request-2",
            ),
        )
        second_acceptance = await _accept(
            opinion_service, investigation.id, second.proposal, key="pg-accept-2"
        )
        assert second_acceptance.opinion.version == 2
        assert {note.anchor for note in second_acceptance.opinion.observations} == {
            LEAD_ANCHOR,
            SECTION_ANCHOR,
        }

        correction = await proposal_service.request(
            investigation.id,
            **_request_kwargs(
                anchor=LEAD_ANCHOR,
                target=NoteTargetBlockType.LEAD,
                advisor=advisor,
                base_version=2,
                base_sha256=second_acceptance.opinion.content_sha256,
                key="pg-request-3",
            ),
        )
        fourth = await proposal_service.request(
            investigation.id,
            **_request_kwargs(
                anchor=SECTION_ANCHOR,
                target=NoteTargetBlockType.SECTION,
                advisor=advisor,
                base_version=2,
                base_sha256=second_acceptance.opinion.content_sha256,
                key="pg-request-4",
            ),
        )
        fourth_acceptance = await _accept(
            opinion_service, investigation.id, fourth.proposal, key="pg-accept-4"
        )
        assert fourth_acceptance.opinion.version == 3

        with pytest.raises(OpinionError) as stale:
            await _accept(
                opinion_service,
                investigation.id,
                correction.proposal,
                key="pg-accept-3",
            )
        assert stale.value.code is OpinionErrorCode.STALE_NOTE_PROPOSAL
        async with uow_factory() as uow:
            stored_correction = await uow.investigation_note_proposals.get(correction.proposal.id)
            accepted_proposals = await uow.investigation_note_proposals.list_for_investigation(
                investigation.id
            )
        assert stored_correction is not None
        assert stored_correction.status is NoteProposalStatus.STALE
        assert {item.id: item.status for item in accepted_proposals}[
            first.proposal.id
        ] is NoteProposalStatus.ACCEPTED
        assert transport.calls  # the real advisor tool called the scripted transport

        pending = await proposal_service.request(
            investigation.id,
            **_request_kwargs(
                anchor=LEAD_ANCHOR,
                target=NoteTargetBlockType.LEAD,
                advisor=advisor,
                base_version=3,
                base_sha256=fourth_acceptance.opinion.content_sha256,
                key="pg-request-5",
            ),
        )
        assert pending.proposal.materialised is False
        with pytest.raises(NoteProposalError) as unavailable:
            await proposal_service.ingest_response(
                investigation.id,
                pending.proposal.id,
                expected_status_version=pending.proposal.status_version,
                actor="system:integration",
            )
        assert unavailable.value.code in {
            NoteProposalErrorCode.RESPONSE_PENDING,
            NoteProposalErrorCode.RESPONSE_UNUSABLE,
            NoteProposalErrorCode.PROVENANCE_INVALID,
        }
        with pytest.raises(OpinionError):
            await _accept(opinion_service, investigation.id, pending.proposal, key="pg-accept-5")
    finally:
        await engine.dispose()
