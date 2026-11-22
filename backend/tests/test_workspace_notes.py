"""ADR-0011 D6 (M24-WS2): human note annotations and note-driven actions.

Every oracle is offline: no host filesystem, no Bridge, no provider.  A note
revision is canonical ingest metadata; the note itself stays a non-evidence
work contribution until a canonical observation is imported through the 07
ImportService, which this lot never calls.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI

from cti_app.api.investigation_workspace_notes import router as notes_router
from cti_app.application.identity import Identity, IdentityScope, LocalIdentityProvider
from cti_app.application.investigation_persistence import (
    InMemoryAgentSessionRepository,
    InMemoryEvidenceObservationRepository,
    InMemoryFindingRepository,
    InMemoryInvestigationTaskRepository,
    InMemorySampleAssociationRepository,
    InMemoryToolInvocationRepository,
    InMemoryWorkContributionRepository,
    InMemoryWorkspaceBindingRepository,
    InMemoryWorkspaceFileRevisionRepository,
    InMemoryWorkspaceNoteAnnotationRepository,
)
from cti_app.application.operator_tool_invocation import OperatorToolInvocationError
from cti_app.application.workspace_agent import (
    WorkspaceBindingV1,
    WorkspaceFileRevisionV1,
    workspace_revision_id,
)
from cti_app.application.workspace_notes import (
    InMemoryNoteInstructionPort,
    NoteBridgeReviewOutcomeV1,
    NoteBridgeReviewRequestV1,
    NoteError,
    NoteErrorCode,
    NotePassageRangeV1,
    WorkspaceNoteService,
)
from cti_app.domain.agentic_operator import (
    ACTIVE_AGENT_SESSION_STATES,
    AgentSessionInstructionV1,
    AgentSessionState,
    InstructionState,
    OperatorAdapterKind,
)
from cti_app.domain.blobs import BlobDescriptor, BlobRecord
from cti_app.domain.classification import TLP
from cti_app.domain.collection import Indicator, IndicatorKind, SourceSpan
from cti_app.domain.entities import Sample
from cti_app.domain.investigation import (
    SampleAssociation,
    SampleOrigin,
    ToolInvocation,
    ToolInvocationActorKind,
    ToolInvocationStatus,
)
from cti_app.domain.investigation_task import (
    InvestigationTaskV1,
    ResultVerificationStatus,
    TaskActor,
    TaskActorKind,
    TaskPriority,
    TaskStatus,
)
from cti_app.domain.production import AnalystInvestigation, LoopBudget, ProseAuthority
from cti_app.domain.work_contribution import (
    WorkContributionCategory,
    WorkContributionImportStatus,
    WorkContributionMethod,
    WorkContributionObjectType,
    WorkContributionOrigin,
    WorkContributionV1,
)
from cti_app.domain.workspace_note_annotation import (
    NoteAnnotationKind,
    NoteIocType,
)
from tests.test_investigation_api import _SyncASGIClient

NOW = datetime(2026, 11, 22, 12, 0, tzinfo=UTC)
NOTE_PATH = "notes/humaines/hypothese.md"
NOTE_TEXT = "# Hypothèse\n\nLe chargeur utiliserait InternetOpenA.\n"
OTHER_SHA = "f" * 64


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _revision(
    workspace_id: str,
    path: str,
    content: bytes,
    *,
    observed_at: datetime = NOW,
    supersedes: str | None = None,
) -> WorkspaceFileRevisionV1:
    digest = _sha256(content)
    return WorkspaceFileRevisionV1(
        workspace_id=workspace_id,
        revision_id=workspace_revision_id(workspace_id=workspace_id, path=path, sha256=digest),
        path=path,
        sha256=digest,
        size=len(content),
        mode="0640",
        mtime_ns=1_700_000_000_000_000_000,
        observed_at=observed_at,
        origin="filesystem",
        supersedes_revision_id=supersedes,
    )


class _InvestigationRows:
    def __init__(self, investigation: AnalystInvestigation) -> None:
        self.investigation = investigation

    async def get(self, investigation_id: UUID) -> AnalystInvestigation | None:
        if investigation_id == self.investigation.id:
            return self.investigation
        return None


class _DecisionRecorder:
    def __init__(self) -> None:
        self.rows: list[Any] = []

    async def append(self, decision: Any) -> None:
        self.rows.append(decision)

    async def list_for_investigation(self, investigation_id: UUID) -> tuple[Any, ...]:
        return tuple(item for item in self.rows if item.investigation_id == investigation_id)


class _IndicatorRows:
    def __init__(self, items: tuple[Indicator, ...]) -> None:
        self.items = items

    async def list_for_subject(self, subject_id: UUID) -> tuple[Indicator, ...]:
        return tuple(item for item in self.items if item.subject_id == subject_id)


class _SampleRows:
    def __init__(self, items: tuple[Sample, ...]) -> None:
        self.items = {item.id: item for item in items}

    async def get(self, sample_id: UUID) -> Sample | None:
        return self.items.get(sample_id)


class _BlobRows:
    def __init__(self, items: tuple[BlobRecord, ...]) -> None:
        self.items = {item.id: item for item in items}

    async def get(self, blob_id: UUID) -> BlobRecord | None:
        return self.items.get(blob_id)


class _FakeHandoffRepository:
    """Only the read path the note view uses; handoff creation is lot 05A's."""

    def __init__(self) -> None:
        self.records: list[Any] = []

    async def list_for_investigation(
        self, investigation_id: UUID, *, task_id: UUID | None = None
    ) -> tuple[Any, ...]:
        del task_id
        return tuple(item for item in self.records if item.investigation_id == investigation_id)


class _Uow:
    def __init__(self, fixture: WorkspaceNoteFixture) -> None:
        self.analyst_investigations = fixture.investigations
        self.analyst_decisions = fixture.decisions
        self.workspace_bindings = fixture.bindings
        self.workspace_file_revisions = fixture.revisions
        self.workspace_note_annotations = fixture.annotations
        self.investigation_tasks = fixture.tasks
        self.sample_associations = fixture.associations
        self.samples = fixture.samples
        self.blobs = fixture.blobs
        self.indicators = fixture.indicators
        self.work_contributions = fixture.contributions
        self.tool_invocations = fixture.invocations
        self.investigation_handoffs = fixture.handoffs
        self.agent_sessions = fixture.sessions
        self.evidence_observations = fixture.observations
        self.investigation_findings = fixture.findings

    async def __aenter__(self) -> _Uow:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def commit(self) -> None:
        return None

    async def rollback(self) -> None:
        return None


class _BridgePort:
    def __init__(
        self,
        *,
        response_observation_ids: tuple[UUID, ...] = (),
        error: Exception | None = None,
    ) -> None:
        self.requests: list[NoteBridgeReviewRequestV1] = []
        self.response_observation_ids = response_observation_ids
        self.error = error

    async def request_review(
        self, uow: Any, request: NoteBridgeReviewRequestV1
    ) -> NoteBridgeReviewOutcomeV1:
        del uow
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return NoteBridgeReviewOutcomeV1(
            invocation_id=uuid4(),
            invocation_status="succeeded",
            response_observation_ids=self.response_observation_ids,
            idempotent_replay=False,
        )


class WorkspaceNoteFixture:
    def __init__(self) -> None:
        subject_id = uuid4()
        self.investigation = AnalystInvestigation(
            production_run_id=uuid4(),
            subject_id=subject_id,
            synthesis_artifact_id=uuid4(),
            budget=LoopBudget(),
            prose_authority=ProseAuthority.BRIDGE_ONLY,
        )
        self.workspace_id = "ws-" + uuid4().hex
        self.binding = WorkspaceBindingV1(
            workspace_id=self.workspace_id,
            investigation_id=self.investigation.id,
            subject_id=subject_id,
            delivery_code="IR202611",
            article_slug="bluemoon-cn",
            synthesis_sha256="a" * 64,
            created_at=NOW,
        )
        self.first = _revision(self.workspace_id, NOTE_PATH, NOTE_TEXT.encode())
        second_text = NOTE_TEXT + "\nAjout: la clé serait dans .data.\n"
        self.second = _revision(
            self.workspace_id,
            NOTE_PATH,
            second_text.encode(),
            observed_at=NOW + timedelta(minutes=5),
            supersedes=self.first.revision_id,
        )
        self.other_path_revision = _revision(
            self.workspace_id, "notes/humaines/autre.md", b"# Autre note\n"
        )

        self.investigations = _InvestigationRows(self.investigation)
        self.decisions = _DecisionRecorder()
        self.bindings = InMemoryWorkspaceBindingRepository()
        self.revisions = InMemoryWorkspaceFileRevisionRepository()
        self.annotations = InMemoryWorkspaceNoteAnnotationRepository()
        self.tasks = InMemoryInvestigationTaskRepository()
        self.associations = InMemorySampleAssociationRepository()
        self.contributions = InMemoryWorkContributionRepository()
        self.invocations = InMemoryToolInvocationRepository()
        self.sessions = InMemoryAgentSessionRepository()
        self.observations = InMemoryEvidenceObservationRepository()
        self.findings = InMemoryFindingRepository()
        self.handoffs = _FakeHandoffRepository()

        self.sample = Sample(
            subject_id=subject_id,
            blob_id=uuid4(),
            original_name="sample.bin",
            origin="seed",
            acquired_at=NOW,
            license_restriction=None,
            tlp=TLP.AMBER,
            do_not_submit=False,
            external_llm_allowed=True,
        )
        self.sample_blob = BlobRecord(
            descriptor=BlobDescriptor(
                sha256="b" * 64,
                size=16,
                mime_type="application/octet-stream",
                logical_bucket="samples",
            ),
            id=self.sample.blob_id,
        )
        self.samples = _SampleRows((self.sample,))
        self.blobs = _BlobRows((self.sample_blob,))
        self.association = SampleAssociation(
            investigation_id=self.investigation.id,
            sample_sha256="b" * 64,
            origin=SampleOrigin.SEED,
        )
        self.indicators = _IndicatorRows(
            (
                Indicator(
                    subject_id=subject_id,
                    edition_id=uuid4(),
                    source_document_id=uuid4(),
                    derived_artifact_id=uuid4(),
                    kind=IndicatorKind.DOMAIN,
                    original_value="evil.example",
                    normalized_value="evil.example",
                    span=SourceSpan(start=0, end=1),
                ),
            )
        )
        self.uow = _Uow(self)
        self.bridge = _BridgePort()
        self.instructions = InMemoryNoteInstructionPort()
        self.service = WorkspaceNoteService(
            uow_factory=lambda: self.uow,
            bridge=self.bridge,
            instructions=self.instructions,
        )

    async def seed(self) -> None:
        await self.bindings.add_if_absent(self.binding)
        for revision in (self.first, self.second, self.other_path_revision):
            await self.revisions.append_if_absent(revision)
        await self.associations.append_version(self.association, expected_version=0)

    async def annotate(self, revision_id: str | None = None, **overrides: Any) -> Any:
        values: dict[str, Any] = {
            "kind": NoteAnnotationKind.MARK_USEFUL,
            "actor_id": "analyst:human",
            "idempotency_key": f"note-annotation-{uuid4().hex}",
        }
        values.update(overrides)
        return await self.service.annotate(
            self.investigation.id, revision_id or self.first.revision_id, **values
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class _SessionRow:
    """Only the session facts the note service and its fake repository read."""

    investigation_id: UUID
    task_id: UUID
    state: AgentSessionState
    adapter_kind: OperatorAdapterKind = OperatorAdapterKind.CODEX_CLI
    created_at: datetime = NOW
    idempotency_key: str = "session-key"
    capability_sha256: str = "0" * 64
    host_session_ref: str | None = None
    handoff_id: UUID | None = None
    id: UUID = field(default_factory=uuid4)

    @property
    def is_active(self) -> bool:
        return self.state in ACTIVE_AGENT_SESSION_STATES


@pytest.fixture
def fixture() -> WorkspaceNoteFixture:
    import asyncio

    case = WorkspaceNoteFixture()
    asyncio.run(case.seed())
    return case


def test_note_stays_a_contribution_until_an_observation_is_imported(
    fixture: WorkspaceNoteFixture,
) -> None:
    """E01: the note view never promotes a human hypothesis to evidence."""
    import asyncio

    async def scenario() -> None:
        contribution = WorkContributionV1(
            investigation_id=fixture.investigation.id,
            workspace_id=fixture.workspace_id,
            workspace_revision_id=fixture.first.revision_id,
            file_relpath=NOTE_PATH,
            file_sha256=fixture.first.sha256,
            observed_at=NOW,
            origin=WorkContributionOrigin.HUMAN,
            idempotency_key="note-import-0001",
            category=WorkContributionCategory.WORK_CONTRIBUTION,
            content_blob_id=uuid4(),
            content_sha256=fixture.first.sha256,
            import_status=WorkContributionImportStatus.IMPORTED,
        )
        await fixture.contributions.append_if_absent(contribution)

        view = await fixture.service.list_note_view(fixture.investigation.id, NOTE_PATH)
        assert view.is_evidence is False
        assert view.unverified_hypothesis is True
        assert view.observation_imported is False
        assert view.latest.revision_id == fixture.second.revision_id
        assert [item.revision_id for item in view.versions] == [
            fixture.first.revision_id,
            fixture.second.revision_id,
        ]

        observation_id = uuid4()
        imported = WorkContributionV1(
            investigation_id=fixture.investigation.id,
            workspace_id=fixture.workspace_id,
            workspace_revision_id=fixture.second.revision_id,
            file_relpath=NOTE_PATH,
            file_sha256=fixture.second.sha256,
            observed_at=NOW,
            origin=WorkContributionOrigin.HUMAN,
            idempotency_key="note-import-0002",
            category=WorkContributionCategory.OBSERVATION,
            content_blob_id=uuid4(),
            content_sha256=fixture.second.sha256,
            import_status=WorkContributionImportStatus.IMPORTED,
            observation_id=observation_id,
        )
        await fixture.contributions.append_if_absent(imported)

        promoted = await fixture.service.list_note_view(fixture.investigation.id, NOTE_PATH)
        assert promoted.observation_imported is True
        assert promoted.is_evidence is True
        assert promoted.unverified_hypothesis is False

    asyncio.run(scenario())


def test_annotation_flows_validate_targets_and_replay_idempotently(
    fixture: WorkspaceNoteFixture,
) -> None:
    import asyncio

    async def scenario() -> None:
        task = InvestigationTaskV1(
            investigation_id=fixture.investigation.id,
            task_key="lead-1",
            title="Vérifier le chargeur",
            question="Le chargeur ouvre-t-il une URL ?",
            objective="",
            priority=TaskPriority.NORMAL,
            status=TaskStatus.READY,
            status_version=1,
            result_verification_status=ResultVerificationStatus.UNVERIFIED,
            created_by=TaskActor(kind=TaskActorKind.HUMAN, actor_id="analyst:human"),
            created_at=NOW,
            updated_at=NOW,
        )
        await fixture.tasks.add(task)

        linked = await fixture.annotate(
            kind=NoteAnnotationKind.LINK_TASK,
            task_id=task.id,
            idempotency_key="link-task-0001",
        )
        replay = await fixture.annotate(
            kind=NoteAnnotationKind.LINK_TASK,
            task_id=task.id,
            idempotency_key="link-task-0001",
        )
        assert replay == linked
        with pytest.raises(NoteError) as conflict:
            await fixture.annotate(
                kind=NoteAnnotationKind.LINK_TASK,
                task_id=uuid4(),
                idempotency_key="link-task-0001",
            )
        assert conflict.value.code is NoteErrorCode.IDEMPOTENCY_CONFLICT

        other_task = InvestigationTaskV1(
            investigation_id=uuid4(),
            task_key="lead-2",
            title="Autre enquête",
            question="?",
            objective="",
            priority=TaskPriority.NORMAL,
            status=TaskStatus.READY,
            status_version=1,
            result_verification_status=ResultVerificationStatus.UNVERIFIED,
            created_by=TaskActor(kind=TaskActorKind.HUMAN, actor_id="analyst:human"),
            created_at=NOW,
            updated_at=NOW,
        )
        await fixture.tasks.add(other_task)
        with pytest.raises(NoteError) as foreign_task:
            await fixture.annotate(
                kind=NoteAnnotationKind.LINK_TASK,
                task_id=other_task.id,
                idempotency_key="link-task-0002",
            )
        assert foreign_task.value.code is NoteErrorCode.TASK_NOT_IN_INVESTIGATION

        await fixture.annotate(
            kind=NoteAnnotationKind.LINK_IOC,
            ioc_value="evil.example",
            ioc_type=NoteIocType.DOMAIN,
            idempotency_key="link-ioc-0001",
        )
        with pytest.raises(NoteError) as foreign_ioc:
            await fixture.annotate(
                kind=NoteAnnotationKind.LINK_IOC,
                ioc_value="other.example",
                ioc_type=NoteIocType.DOMAIN,
                idempotency_key="link-ioc-0002",
            )
        assert foreign_ioc.value.code is NoteErrorCode.IOC_NOT_IN_INVESTIGATION

        await fixture.annotate(
            kind=NoteAnnotationKind.LINK_SAMPLE,
            sample_id=fixture.sample.id,
            idempotency_key="link-sample-0001",
        )
        with pytest.raises(NoteError) as foreign_sample:
            await fixture.annotate(
                kind=NoteAnnotationKind.LINK_SAMPLE,
                sample_id=uuid4(),
                idempotency_key="link-sample-0002",
            )
        assert foreign_sample.value.code is NoteErrorCode.SAMPLE_NOT_IN_INVESTIGATION

        marked = await fixture.annotate(
            kind=NoteAnnotationKind.MARK_USEFUL,
            idempotency_key="mark-useful-0001",
        )
        assert marked.kind is NoteAnnotationKind.MARK_USEFUL
        await fixture.annotate(
            kind=NoteAnnotationKind.UNMARK_USEFUL,
            idempotency_key="unmark-useful-0001",
        )
        author = await fixture.annotate(
            kind=NoteAnnotationKind.ATTRIBUTE_AUTHOR,
            detail="Analyste Dupont (attribution explicite)",
            idempotency_key="author-0001",
        )
        assert author.detail == "Analyste Dupont (attribution explicite)"

        view = await fixture.service.list_note_view(fixture.investigation.id, NOTE_PATH)
        assert task.id in view.linked_task_ids
        assert (NoteIocType.DOMAIN, "evil.example") in view.linked_iocs
        assert fixture.sample.id in view.linked_sample_ids
        assert view.useful is False  # the explicit unmark follows the mark
        assert view.attributed_authors == ("Analyste Dupont (attribution explicite)",)
        assert view.is_evidence is False

    asyncio.run(scenario())


def test_ask_bridge_uses_the_advisor_path_with_the_exact_revision(
    fixture: WorkspaceNoteFixture,
) -> None:
    import asyncio

    async def scenario() -> None:
        result = await fixture.service.ask_bridge_review(
            fixture.investigation.id,
            fixture.second.revision_id,
            question="Cette hypothèse est-elle soutenable ?",
            actor_id="analyst:human",
            idempotency_key="bridge-review-0001",
            note_excerpt="Le chargeur utiliserait InternetOpenA.",
            passage=NotePassageRangeV1(start=0, end=35),
            evidence_refs=(),
            finding_refs=(),
            expected_working_state_version=1,
        )
        assert result.pending is True  # no persisted response yet
        assert result.advisory_only is True
        assert result.is_evidence is False
        request = fixture.bridge.requests[-1]
        assert request.note_revision_id == fixture.second.revision_id
        assert request.note_sha256 == fixture.second.sha256
        assert request.consultation_type == "critique"
        assert fixture.second.revision_id in request.composed_question
        assert fixture.second.sha256 in request.composed_question
        assert "non probante" in request.composed_question
        assert "EXTRAIT DE NOTE HUMAINE NON VÉRIFIÉE" in request.composed_question

        with pytest.raises(NoteError) as citation:
            await fixture.service.ask_bridge_review(
                fixture.investigation.id,
                fixture.second.revision_id,
                question="Revue ?",
                actor_id="analyst:human",
                idempotency_key="bridge-review-0003",
                evidence_refs=(uuid4(),),
                expected_working_state_version=1,
            )
        assert citation.value.code is NoteErrorCode.INVALID_CITATION

    asyncio.run(scenario())


def test_ask_bridge_is_refused_when_the_advisor_gate_denies(
    fixture: WorkspaceNoteFixture,
) -> None:
    import asyncio

    async def scenario() -> None:
        fixture.bridge.error = OperatorToolInvocationError("external_llm_disallowed", 403)
        with pytest.raises(OperatorToolInvocationError):
            await fixture.service.ask_bridge_review(
                fixture.investigation.id,
                fixture.first.revision_id,
                question="Revue ?",
                actor_id="analyst:human",
                idempotency_key="bridge-review-0002",
                expected_working_state_version=1,
            )
        # Nothing was recorded locally and no answer text was fabricated.
        annotations = await fixture.annotations.list_for_path(fixture.investigation.id, NOTE_PATH)
        assert annotations == ()

    asyncio.run(scenario())


def test_ask_agent_resolves_the_latest_revision_at_send_time(
    fixture: WorkspaceNoteFixture,
) -> None:
    import asyncio

    async def scenario() -> None:
        task = InvestigationTaskV1(
            investigation_id=fixture.investigation.id,
            task_key="lead-3",
            title="Suite",
            question="Continuer ?",
            objective="",
            priority=TaskPriority.NORMAL,
            status=TaskStatus.RUNNING,
            status_version=1,
            result_verification_status=ResultVerificationStatus.UNVERIFIED,
            created_by=TaskActor(kind=TaskActorKind.HUMAN, actor_id="analyst:human"),
            created_at=NOW,
            updated_at=NOW,
        )
        await fixture.tasks.add(task)
        session = _SessionRow(
            investigation_id=fixture.investigation.id,
            task_id=task.id,
            host_session_ref="host-session-1",
            state=AgentSessionState.AWAITING_INSTRUCTION,
            idempotency_key="session-0001",
        )
        await fixture.sessions.add(session)

        continuation = await fixture.service.ask_agent_continue(
            fixture.investigation.id,
            fixture.first.revision_id,
            task_id=task.id,
            question="Poursuis la vérification.",
            actor_id="analyst:human",
            idempotency_key="ask-agent-0001",
        )
        assert continuation.note_revision_id == fixture.second.revision_id
        assert continuation.note_sha256 == fixture.second.sha256
        request = fixture.instructions.requests[-1]
        assert request.session_id == session.id
        assert fixture.second.revision_id in request.text
        assert fixture.second.sha256 in request.text
        assert "jamais une preuve" in request.text

    asyncio.run(scenario())


def test_ask_agent_refuses_without_an_active_session(
    fixture: WorkspaceNoteFixture,
) -> None:
    import asyncio

    async def scenario() -> None:
        task = InvestigationTaskV1(
            investigation_id=fixture.investigation.id,
            task_key="lead-4",
            title="Sans session",
            question="?",
            objective="",
            priority=TaskPriority.NORMAL,
            status=TaskStatus.READY,
            status_version=1,
            result_verification_status=ResultVerificationStatus.UNVERIFIED,
            created_by=TaskActor(kind=TaskActorKind.HUMAN, actor_id="analyst:human"),
            created_at=NOW,
            updated_at=NOW,
        )
        await fixture.tasks.add(task)
        with pytest.raises(NoteError) as refusal:
            await fixture.service.ask_agent_continue(
                fixture.investigation.id,
                fixture.first.revision_id,
                task_id=task.id,
                question="Poursuis.",
                actor_id="analyst:human",
                idempotency_key="ask-agent-0002",
            )
        assert refusal.value.code is NoteErrorCode.AGENT_SESSION_REQUIRED

    asyncio.run(scenario())


def test_verification_task_is_proposed_and_never_a_finding(
    fixture: WorkspaceNoteFixture,
) -> None:
    import asyncio

    async def scenario() -> None:
        result = await fixture.service.propose_verification_task(
            fixture.investigation.id,
            fixture.second.revision_id,
            passage=NotePassageRangeV1(start=10, end=40),
            question="Vérifier l'usage d'InternetOpenA.",
            actor_id="analyst:human",
            idempotency_key="verification-task-0001",
        )
        task = result.task
        assert task.status is TaskStatus.PROPOSED
        assert task.result_verification_status is ResultVerificationStatus.UNVERIFIED
        assert task.created_by.kind is TaskActorKind.HUMAN
        assert task.created_by.actor_id == "analyst:human"
        reference = task.execution_policy["note_reference"]
        assert reference["revision_id"] == fixture.second.revision_id
        assert reference["note_sha256"] == fixture.second.sha256
        assert reference["passage"] == {"start": 10, "end": 40}
        assert "Aucun texte de la note n'est copié comme fait" in task.question
        # A verification task is not evidence and creates no finding.
        assert await fixture.findings.list_for_investigation(fixture.investigation.id) == ()
        assert await fixture.observations.list_for_investigation(fixture.investigation.id) == ()

        replay = await fixture.service.propose_verification_task(
            fixture.investigation.id,
            fixture.second.revision_id,
            passage=NotePassageRangeV1(start=10, end=40),
            question="Vérifier l'usage d'InternetOpenA.",
            actor_id="analyst:human",
            idempotency_key="verification-task-0001",
        )
        assert replay.task.id == task.id
        assert replay.replayed is True

    asyncio.run(scenario())


def test_observation_proposal_writes_nothing_canonical(
    fixture: WorkspaceNoteFixture,
) -> None:
    import asyncio

    async def scenario() -> None:
        proposal = await fixture.service.propose_observation_conversion(
            fixture.investigation.id,
            fixture.second.revision_id,
            object_type=WorkContributionObjectType.FILE,
            method=WorkContributionMethod.STRING_EXTRACTION,
            tool="strings",
            tool_version="2.42",
            parameters={"encoding": "ascii"},
            passage=NotePassageRangeV1(start=0, end=20),
        )
        assert proposal.canonical_writes == ()
        assert proposal.is_evidence is False
        assert proposal.requires_human_decision is True
        assert proposal.declaration.object_id == fixture.second.sha256
        assert proposal.declaration.evidence_kind.value == "binary_string"
        assert any(
            item.startswith("source_note_reference:") for item in proposal.declaration.limitations
        )
        assert await fixture.contributions.list_for_investigation(fixture.investigation.id) == ()
        assert await fixture.observations.list_for_investigation(fixture.investigation.id) == ()

        with pytest.raises(NoteError) as refused_pair:
            await fixture.service.propose_observation_conversion(
                fixture.investigation.id,
                fixture.second.revision_id,
                object_type=WorkContributionObjectType.URL,
                method=WorkContributionMethod.STRING_EXTRACTION,
                tool="strings",
                object_id="http://evil.example/",
            )
        assert refused_pair.value.code is NoteErrorCode.INVALID_OBSERVATION_CONVERSION

    asyncio.run(scenario())


def test_view_lists_agent_usage_of_the_note(fixture: WorkspaceNoteFixture) -> None:
    import asyncio

    async def scenario() -> None:
        task = InvestigationTaskV1(
            investigation_id=fixture.investigation.id,
            task_key="lead-5",
            title="Usage",
            question="?",
            objective="",
            priority=TaskPriority.NORMAL,
            status=TaskStatus.READY,
            status_version=1,
            result_verification_status=ResultVerificationStatus.UNVERIFIED,
            created_by=TaskActor(kind=TaskActorKind.HUMAN, actor_id="analyst:human"),
            created_at=NOW,
            updated_at=NOW,
        )
        await fixture.tasks.add(task)
        session = _SessionRow(
            investigation_id=fixture.investigation.id,
            task_id=task.id,
            host_session_ref="host-session-2",
            state=AgentSessionState.AWAITING_INSTRUCTION,
            idempotency_key="session-0002",
        )
        await fixture.sessions.add(session)
        await fixture.service.ask_agent_continue(
            fixture.investigation.id,
            fixture.second.revision_id,
            task_id=task.id,
            question="Continue.",
            actor_id="analyst:human",
            idempotency_key="ask-agent-0003",
        )
        instruction_text = fixture.instructions.requests[-1].text
        instruction = AgentSessionInstructionV1(
            investigation_id=fixture.investigation.id,
            task_id=task.id,
            session_id=session.id,
            idempotency_key="ask-agent-0003",
            text=instruction_text,
            text_sha256=_sha256(instruction_text.encode()),
            state=InstructionState.QUEUED,
            created_by_kind="human",
            created_by_id="analyst:human",
            queued_at=NOW,
            created_at=NOW,
            updated_at=NOW,
        )
        await fixture.sessions.add_instruction(instruction)
        invocation = ToolInvocation(
            investigation_id=fixture.investigation.id,
            operation_key="advisor-consult-0001",
            tool_name="advisor.consult",
            schema_version="1",
            normalized_parameters={
                "consultation_type": "critique",
                "question": f"Revue de {fixture.second.revision_id} ({fixture.second.sha256})",
            },
            purpose="Revue de note",
            status=ToolInvocationStatus.SUCCEEDED,
            actor_kind=ToolInvocationActorKind.HUMAN,
            actor_id="analyst:human",
        )
        await fixture.invocations.add_if_absent(invocation)
        fixture.handoffs.records.append(
            SimpleNamespace(
                investigation_id=fixture.investigation.id,
                handoff=SimpleNamespace(
                    handoff_id=str(uuid4()),
                    task_id=str(task.id),
                    version=1,
                    files=(
                        SimpleNamespace(
                            file_relpath=NOTE_PATH,
                            workspace_revision_id=fixture.second.revision_id,
                            file_sha256=fixture.second.sha256,
                        ),
                    ),
                ),
            )
        )

        view = await fixture.service.list_note_view(fixture.investigation.id, NOTE_PATH)
        assert [item.session_id for item in view.instructions] == [session.id]
        assert [item.invocation_id for item in view.consultations] == [invocation.id]
        assert len(view.handoffs) == 1
        assert view.handoffs[0].version == 1

    asyncio.run(scenario())


def test_view_never_infers_an_author_from_the_folder(
    fixture: WorkspaceNoteFixture,
) -> None:
    import asyncio

    async def scenario() -> None:
        view = await fixture.service.list_note_view(fixture.investigation.id, NOTE_PATH)
        assert view.path == NOTE_PATH
        assert view.attributed_authors == ()

    asyncio.run(scenario())


class _OperatorClientIdentity:
    async def current(self) -> Identity:
        return Identity(
            actor_id="operator:shared",
            scopes=frozenset({IdentityScope.OPERATOR_CLIENT}),
        )


class _UnverifiedHumanIdentity:
    mode = "unverified"
    verified = False

    async def current(self) -> Identity:
        return Identity(actor_id="analyst:unverified")


def _api_client(fixture: WorkspaceNoteFixture) -> tuple[FastAPI, _SyncASGIClient]:
    app = FastAPI()
    app.include_router(notes_router)
    app.state.workspace_note_service = fixture.service
    app.state.identity_provider = LocalIdentityProvider("analyst:human")
    return app, _SyncASGIClient(app)


def test_note_routes_require_a_verified_human(fixture: WorkspaceNoteFixture) -> None:
    app, client = _api_client(fixture)
    path = f"/api/investigations/{fixture.investigation.id}/workspace/notes/{NOTE_PATH}/view"

    human = client.get(path)
    assert human.status_code == 200, human.text
    assert human.json()["is_evidence"] is False

    app.state.identity_provider = _OperatorClientIdentity()
    refused = client.get(path)
    assert refused.status_code == 403
    assert refused.json()["detail"]["code"] == "verified_human_operator_required"

    app.state.identity_provider = _UnverifiedHumanIdentity()
    unverified = client.get(path)
    assert unverified.status_code == 403

    app.state.identity_provider = LocalIdentityProvider("analyst:human")
    annotated = client.post(
        f"/api/investigations/{fixture.investigation.id}/workspace/notes/"
        f"{fixture.first.revision_id}/annotations",
        json={"kind": "mark_useful"},
        headers={"Idempotency-Key": "api-mark-useful-0001"},
    )
    assert annotated.status_code == 200, annotated.text
    assert annotated.json()["kind"] == "mark_useful"
    assert annotated.json()["actor_id"] == "analyst:human"


def test_a04_a05_view_lists_a_human_note_with_its_versions(
    fixture: WorkspaceNoteFixture,
) -> None:
    """A04/A05 read path: the canonical ingest exposes both note revisions."""
    import asyncio

    async def scenario() -> None:
        view = await fixture.service.list_note_view(fixture.investigation.id, NOTE_PATH)
        assert view.latest.revision_id == fixture.second.revision_id
        assert view.latest.sha256 == fixture.second.sha256
        assert view.latest.supersedes_revision_id == fixture.first.revision_id
        assert view.latest.origin == "filesystem"
        assert [item.sha256 for item in view.versions] == [
            fixture.first.sha256,
            fixture.second.sha256,
        ]
        with pytest.raises(NoteError) as missing:
            await fixture.service.list_note_view(
                fixture.investigation.id, "notes/humaines/absente.md"
            )
        assert missing.value.code is NoteErrorCode.NOTE_NOT_FOUND

    asyncio.run(scenario())
