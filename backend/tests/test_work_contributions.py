"""Oracles for M24-07 workspace contribution imports (ADR-0011 D6, E01-E05).

The harness composes the canonical in-memory ports, the real
``ProductionEvidenceRecorder`` over an in-memory observation port, the real
``FindingService`` and a content-addressed fake artifact store.  PostgreSQL
rules (composite revision FK, partial unique index, CHECK constraints and
append-only triggers) live in
``tests/integration/test_work_contributions_postgres.py`` and
``tests/integration/test_investigation_coordination_schema_postgres.py``.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from cti_app.api.investigation_contributions import router as contributions_router
from cti_app.application.identity import Identity, IdentityScope, LocalIdentityProvider
from cti_app.application.investigation_governance import (
    InvestigationActor,
    SampleQualificationPermissionError,
    SampleQualificationService,
)
from cti_app.application.investigation_persistence import (
    InMemoryAgentSessionRepository,
    InMemoryEvidenceObservationRepository,
    InMemoryFindingRepository,
    InMemoryInvestigationTaskRepository,
    InMemorySampleAssociationRepository,
    InMemoryWorkContributionRepository,
    InMemoryWorkspaceBindingRepository,
    InMemoryWorkspaceFileRevisionRepository,
)
from cti_app.application.work_contributions import (
    IMPORTED_SUPPORTED_LIMITATION,
    ContributionAuthority,
    WorkContributionConflictError,
    WorkContributionService,
)
from cti_app.application.workspace_agent import (
    WorkspaceBindingV1,
    WorkspaceContentV1,
    WorkspaceFileRevisionV1,
    WorkspaceHashMismatchError,
    workspace_revision_id,
)
from cti_app.domain.agentic_operator import (
    AgentSessionState,
    AgentSessionV1,
    OperatorAdapterKind,
    OperatorCapabilitiesV1,
    OperatorProbeCommand,
)
from cti_app.domain.blobs import BlobDescriptor, BlobRecord
from cti_app.domain.investigation import (
    EvidenceKind,
    EvidenceObservation,
    FindingAssessment,
    FindingKind,
    QualificationActorKind,
    SampleOrigin,
    SampleQualification,
    is_advisory_observation,
)
from cti_app.domain.investigation_task import (
    InvestigationTaskV1,
    ResultVerificationStatus,
    TaskActor,
    TaskActorKind,
    TaskPriority,
    TaskStatus,
)
from cti_app.domain.work_contribution import (
    DEFAULT_MAX_CONTRIBUTION_BYTES,
    OBSERVATION_METADATA_PREFIX,
    WORK_CONTRIBUTION_KIND_MAP,
    ContributionDeclarationV1,
    ContributionValidationError,
    FindingDeclarationV1,
    ObservationDeclarationV1,
    WorkContributionCategory,
    WorkContributionImportStatus,
    WorkContributionMethod,
    WorkContributionObjectType,
    WorkContributionOrigin,
    WorkContributionV1,
    WorkContributionValidationCode,
    assert_contribution_blob_allowed,
    evidence_kind_for,
    observation_metadata_line,
)

NOW = datetime(2026, 11, 22, 12, 0, tzinfo=UTC)
INVESTIGATION_ID = uuid4()
FILE_SHA = "a" * 64
SAMPLE_SHA = "b" * 64
OTHER_SAMPLE_SHA = "c" * 64


def _capabilities() -> OperatorCapabilitiesV1:
    return OperatorCapabilitiesV1(
        adapter_kind=OperatorAdapterKind.CODEX_CLI,
        executable="/usr/local/bin/codex",
        version="codex-cli 1.2.3",
        modes=("exec",),
        supports_resume=True,
        supports_structured_output=True,
        supports_non_interactive=True,
        supports_stop=True,
        permission_modes=("workspace-write",),
        probed_at=NOW,
        probes=(
            OperatorProbeCommand(
                argv=("/usr/local/bin/codex", "--help"),
                exit_code=0,
                stdout_sha256="a" * 64,
                advertised_modes=("exec", "resume", "workspace-write"),
            ),
        ),
    )


class _BlobRepository:
    def __init__(self) -> None:
        self.records: dict[UUID, BlobRecord] = {}

    async def get(self, blob_id: UUID) -> BlobRecord | None:
        return self.records.get(blob_id)


class _ArtifactStore:
    """Content-addressed fake of ``ProductionArtifactStore.put_bytes``."""

    def __init__(self, blobs: _BlobRepository) -> None:
        self._blobs = blobs
        self._by_address: dict[tuple[str, str], BlobRecord] = {}
        self.puts: list[tuple[UUID, int]] = []

    async def put_bytes(self, content: bytes, *, bucket: str, mime_type: str) -> UUID:
        sha256 = hashlib.sha256(content).hexdigest()
        address = (bucket, sha256)
        existing = self._by_address.get(address)
        if existing is not None:
            self.puts.append((existing.id, len(content)))
            return existing.id
        record = BlobRecord(
            descriptor=BlobDescriptor(
                sha256=sha256,
                size=len(content),
                mime_type=mime_type,
                logical_bucket=bucket,
            )
        )
        self._by_address[address] = record
        self._blobs.records[record.id] = record
        self.puts.append((record.id, len(content)))
        return record.id


class _ArticleSnapshots:
    async def resolve(self, synthesis_artifact_id: UUID) -> Any:
        raise NotImplementedError("contribution imports never resolve the synthesis")

    async def get_claims(
        self, investigation_id: UUID, claim_ids: tuple[str, ...] = ()
    ) -> tuple[Any, ...]:
        return ()


class _WorkspaceAgent:
    def __init__(self) -> None:
        self.contents: dict[tuple[str, str], WorkspaceContentV1] = {}
        self.reads: list[tuple[str, str]] = []
        self.read_error: Exception | None = None

    async def read_content(self, workspace_id: str, *, path: str) -> WorkspaceContentV1:
        self.reads.append((workspace_id, path))
        if self.read_error is not None:
            raise self.read_error
        return self.contents[(workspace_id, path)]


class _ContributionUnitOfWork:
    def __init__(self, harness: _Harness) -> None:
        self.blobs = harness.blobs
        self.workspace_bindings = harness.bindings
        self.workspace_file_revisions = harness.revisions
        self.work_contributions = harness.contributions
        self.evidence_observations = harness.observations
        self.investigation_findings = harness.findings
        self.sample_associations = harness.associations
        self.investigation_tasks = harness.tasks
        self.agent_sessions = harness.sessions
        self.commits = 0

    async def __aenter__(self) -> _ContributionUnitOfWork:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        return None


class _DecisionRepository:
    def __init__(self) -> None:
        self.items: list[Any] = []

    async def append(self, decision: Any) -> None:
        self.items.append(decision)

    async def list_for_investigation(self, investigation_id: UUID) -> tuple[Any, ...]:
        return tuple(item for item in self.items if item.investigation_id == investigation_id)


class _G3UnitOfWork:
    def __init__(self, harness: _Harness) -> None:
        self.sample_associations = harness.associations
        self.evidence_observations = harness.observations
        self.analyst_decisions = _DecisionRepository()
        self.commits = 0

    async def __aenter__(self) -> _G3UnitOfWork:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def commit(self) -> None:
        self.commits += 1


class _Harness:
    def __init__(self, *, max_contribution_bytes: int = DEFAULT_MAX_CONTRIBUTION_BYTES) -> None:
        self.investigation_id = INVESTIGATION_ID
        self.workspace_id = "ws-" + uuid4().hex
        self.bindings = InMemoryWorkspaceBindingRepository()
        self.revisions = InMemoryWorkspaceFileRevisionRepository()
        self.contributions = InMemoryWorkContributionRepository()
        self.observations = InMemoryEvidenceObservationRepository()
        self.findings = InMemoryFindingRepository()
        self.associations = InMemorySampleAssociationRepository()
        self.tasks = InMemoryInvestigationTaskRepository(clock=lambda: NOW)
        self.sessions = InMemoryAgentSessionRepository()
        self.last_lease_session_id: UUID | None = None
        self.blobs = _BlobRepository()
        self.artifact_store = _ArtifactStore(self.blobs)
        self.snapshots = _ArticleSnapshots()
        self.agent = _WorkspaceAgent()
        self.uow = _ContributionUnitOfWork(self)
        self.g3_uow = _G3UnitOfWork(self)
        self.max_contribution_bytes = max_contribution_bytes
        self.service = WorkContributionService(
            uow_factory=lambda: self.uow,
            workspace_agent=self.agent,
            artifact_store=self.artifact_store,
            article_snapshots=self.snapshots,
            max_contribution_bytes=max_contribution_bytes,
            clock=lambda: NOW,
        )

    async def bind(self) -> None:
        stored, created = await self.bindings.add_if_absent(
            WorkspaceBindingV1(
                workspace_id=self.workspace_id,
                investigation_id=self.investigation_id,
                subject_id=uuid4(),
                delivery_code="IR202611",
                article_slug="bluemoon-cn",
                synthesis_sha256="d" * 64,
                created_at=NOW,
            )
        )
        assert created and stored.workspace_id == self.workspace_id

    async def add_revision(
        self,
        path: str,
        content: bytes,
        *,
        origin: str = "filesystem",
        task_id: UUID | None = None,
        declared_sha256: str | None = None,
        declared_size: int | None = None,
        content_sha256: str | None = None,
    ) -> WorkspaceFileRevisionV1:
        sha256 = declared_sha256 or hashlib.sha256(content).hexdigest()
        revision = WorkspaceFileRevisionV1(
            workspace_id=self.workspace_id,
            revision_id=workspace_revision_id(
                workspace_id=self.workspace_id, path=path, sha256=sha256
            ),
            path=path,
            sha256=sha256,
            size=len(content) if declared_size is None else declared_size,
            mode="0640",
            mtime_ns=1_700_000_000_000_000_000,
            observed_at=NOW,
            origin=origin,
            task_id=task_id,
        )
        await self.revisions.append_if_absent(revision)
        self.agent.contents[(self.workspace_id, path)] = WorkspaceContentV1(
            content=content,
            sha256=content_sha256 or sha256,
            size=len(content),
            mtime_ns=revision.mtime_ns,
        )
        return revision

    async def import_revision(
        self,
        revision: WorkspaceFileRevisionV1,
        declaration: ContributionDeclarationV1,
        *,
        idempotency_key: str,
        authority: ContributionAuthority | None = None,
    ) -> WorkContributionV1:
        return await self.service.import_revision(
            self.investigation_id,
            workspace_id=self.workspace_id,
            workspace_revision_id=revision.revision_id,
            declaration=declaration,
            authority=authority or ContributionAuthority(origin=WorkContributionOrigin.HOST),
            idempotency_key=idempotency_key,
        )

    async def add_observation(
        self,
        *,
        investigation_id: UUID | None = None,
        source: str = "AutoWork local analysis",
        object_type: str = "file",
        object_id: str = SAMPLE_SHA,
        kind: EvidenceKind = EvidenceKind.OTHER,
    ) -> EvidenceObservation:
        observation = EvidenceObservation(
            investigation_id=investigation_id or self.investigation_id,
            kind=kind,
            blob_id=uuid4(),
            sha256=hashlib.sha256(str(uuid4()).encode()).hexdigest(),
            source=source,
            source_version="test.v1",
            observed_at=NOW,
            object_type=object_type,
            object_id=object_id,
        )
        return await self.observations.append_if_absent(observation)

    async def add_task(
        self,
        *,
        task_id: UUID | None = None,
        lease: bool = False,
        lease_expires_at: datetime | None = None,
        session_id: UUID | None = None,
        fence: int = 1,
        status: TaskStatus = TaskStatus.RUNNING,
    ) -> InvestigationTaskV1:
        values: dict[str, Any] = {
            "id": task_id or uuid4(),
            "investigation_id": self.investigation_id,
            "task_key": "lead-" + uuid4().hex[:12],
            "title": "Inspect the loader",
            "question": "Which network gate is used?",
            "objective": "Identify the first-stage C2 gate.",
            "priority": TaskPriority.HIGH,
            "status": status,
            "status_version": 2,
            "result_verification_status": ResultVerificationStatus.UNVERIFIED,
            "created_by": TaskActor(kind=TaskActorKind.HUMAN, actor_id="analyst:test"),
            "created_at": NOW,
            "updated_at": NOW,
            "assigned_actor_kind": TaskActorKind.OPERATOR,
            "assigned_actor_id": "codex:operator-1",
        }
        if lease:
            session_id = session_id or uuid4()
            self.last_lease_session_id = session_id
            values.update(
                lease_id=uuid4(),
                lease_session_id=session_id,
                lease_fence=fence,
                lease_expires_at=lease_expires_at or NOW + timedelta(seconds=180),
                heartbeat_at=NOW,
            )
        return await self.tasks.add(InvestigationTaskV1(**values))

    async def add_agent_session(
        self,
        task: InvestigationTaskV1,
        *,
        session_id: UUID | None = None,
        state: AgentSessionState = AgentSessionState.RUNNING,
    ) -> UUID:
        """The active session row the agent authority claims (D7/D3)."""
        assert task.lease_session_id is not None
        session_id = session_id or task.lease_session_id
        session = AgentSessionV1(
            id=session_id,
            investigation_id=task.investigation_id,
            task_id=task.id,
            adapter_kind=OperatorAdapterKind.CODEX_CLI,
            state=state,
            state_version=2,
            capability_probe=_capabilities().to_payload(),
            capability_sha256=_capabilities().capability_sha256,
            probed_at=NOW,
            auth_secret_nonce=b"s" * 32,
            auth_secret_hash=hashlib.sha256(b"s" * 32).hexdigest(),
            auth_secret_issued_at=NOW,
            opened_at=NOW,
            idempotency_key=f"session-{session_id}",
            created_at=NOW,
            updated_at=NOW,
            host_session_ref=f"host-{session_id}",
            task_lease_id=task.lease_id,
            task_lease_fence=task.lease_fence,
        )
        stored, _created = await self.sessions.add(session)
        return stored.id


def _observation_declaration(
    *,
    object_type: WorkContributionObjectType = WorkContributionObjectType.FILE,
    method: WorkContributionMethod = WorkContributionMethod.STRING_EXTRACTION,
    object_id: str = SAMPLE_SHA,
    tool: str = "strings",
    tool_version: str = "2.42",
    parameters: dict[str, Any] | None = None,
) -> ContributionDeclarationV1:
    return ContributionDeclarationV1(
        category=WorkContributionCategory.OBSERVATION,
        observation=ObservationDeclarationV1(
            object_type=object_type,
            method=method,
            object_id=object_id,
            tool=tool,
            tool_version=tool_version,
            parameters=parameters or {"min_length": 6},
            limitations=("Sortie bornée à cette exécution.",),
        ),
    )


def _finding_declaration(
    *,
    statement: str,
    observation_refs: tuple[UUID, ...],
    assessment: FindingAssessment = FindingAssessment.QUALIFIED,
    kind: FindingKind = FindingKind.CAPABILITY,
    proposed_sample_sha256: str | None = None,
) -> ContributionDeclarationV1:
    return ContributionDeclarationV1(
        category=WorkContributionCategory.FINDING,
        finding=FindingDeclarationV1(
            kind=kind,
            assessment=assessment,
            statement=statement,
            observation_refs=observation_refs,
            proposed_sample_sha256=proposed_sample_sha256,
        ),
    )


def _work_declaration() -> ContributionDeclarationV1:
    return ContributionDeclarationV1(category=WorkContributionCategory.WORK_CONTRIBUTION)


def _row(**overrides: Any) -> WorkContributionV1:
    values: dict[str, Any] = {
        "investigation_id": uuid4(),
        "workspace_id": "ws-" + uuid4().hex,
        "workspace_revision_id": "rev-" + uuid4().hex[:32],
        "file_relpath": "notes/humaines/note.md",
        "file_sha256": FILE_SHA,
        "observed_at": NOW,
        "origin": WorkContributionOrigin.HOST,
        "idempotency_key": "attempt-0001",
        "import_status": WorkContributionImportStatus.IMPORTED,
        "content_blob_id": uuid4(),
        "content_sha256": FILE_SHA,
    }
    values.update(overrides)
    return WorkContributionV1(**values)


async def test_observation_import_uses_evidence_recorder_and_same_investigation_refs() -> None:
    harness = _Harness()
    await harness.bind()
    revision = await harness.add_revision(
        "results/strings.txt", b"GetProcAddress\nInternetOpenA\n", origin="agent"
    )

    contribution = await harness.import_revision(
        revision,
        _observation_declaration(),
        idempotency_key="observation-import-0001",
        authority=ContributionAuthority(
            origin=WorkContributionOrigin.HUMAN, actor_id="analyst:test"
        ),
    )

    assert contribution.import_status is WorkContributionImportStatus.IMPORTED
    assert contribution.category is WorkContributionCategory.OBSERVATION
    assert contribution.citable_as_evidence is True
    assert contribution.observation_id is not None
    assert contribution.content_blob_id is not None
    assert contribution.content_sha256 == revision.sha256
    observation = await harness.observations.get(contribution.observation_id)
    assert observation is not None
    assert observation.investigation_id == harness.investigation_id
    assert observation.source == "workspace_import"
    assert observation.source_version == "work-contribution.v1"
    assert observation.kind is EvidenceKind.BINARY_STRING
    assert observation.object_type == "file"
    assert observation.object_id == SAMPLE_SHA
    assert observation.blob_id == contribution.content_blob_id
    assert observation.sha256 == revision.sha256
    assert is_advisory_observation(observation) is False
    metadata = next(
        item for item in observation.limitations if item.startswith(OBSERVATION_METADATA_PREFIX)
    )
    assert '"method":"string_extraction"' in metadata
    assert '"tool":"strings"' in metadata
    assert '"tool_version":"2.42"' in metadata
    assert '"parameters":{"min_length":6}' in metadata

    # A finding may only cite observations of its own investigation.
    other = await harness.add_observation(investigation_id=uuid4())
    other_revision = await harness.add_revision("results/finding.md", b"hypothesis")
    rejected = await harness.import_revision(
        other_revision,
        _finding_declaration(statement="Cite another investigation", observation_refs=(other.id,)),
        idempotency_key="finding-other-investigation-0001",
    )
    assert rejected.import_status is WorkContributionImportStatus.REJECTED
    assert (
        rejected.validation_error_code
        is WorkContributionValidationCode.OBSERVATION_REF_INVESTIGATION_MISMATCH
    )
    assert rejected.finding_id is None


async def test_finding_import_never_auto_supported() -> None:
    harness = _Harness()
    await harness.bind()
    observation = await harness.add_observation()
    revision = await harness.add_revision("results/finding.md", b"# capability\n")

    contribution = await harness.import_revision(
        revision,
        _finding_declaration(
            statement="The loader imports InternetOpenA.",
            observation_refs=(observation.id,),
            assessment=FindingAssessment.SUPPORTED,
        ),
        idempotency_key="finding-supported-0001",
    )

    assert contribution.import_status is WorkContributionImportStatus.IMPORTED
    assert contribution.finding_id is not None
    finding = await harness.findings.get(contribution.finding_id)
    assert finding is not None
    assert finding.assessment is FindingAssessment.INCONCLUSIVE
    assert IMPORTED_SUPPORTED_LIMITATION in finding.limitations
    assert finding.observation_refs == (observation.id,)


async def test_new_sample_stays_candidate_until_human_g3() -> None:
    harness = _Harness()
    await harness.bind()
    observation = await harness.add_observation()
    revision = await harness.add_revision("results/new-sample.json", b'{"sha256":"c"}')

    contribution = await harness.import_revision(
        revision,
        _finding_declaration(
            statement="A new loader sample was observed.",
            observation_refs=(observation.id,),
            kind=FindingKind.NEW_SAMPLE,
            proposed_sample_sha256=OTHER_SAMPLE_SHA,
        ),
        idempotency_key="finding-new-sample-0001",
    )

    assert contribution.sample_association_id is not None
    association = await harness.associations.get(contribution.sample_association_id)
    assert association is not None
    assert association.sample_sha256 == OTHER_SAMPLE_SHA
    assert association.origin is SampleOrigin.MANUAL
    assert association.qualification is SampleQualification.CANDIDATE
    assert association.version == 1

    g3 = SampleQualificationService(uow_factory=lambda: harness.g3_uow)
    with pytest.raises(SampleQualificationPermissionError):
        await g3.qualify(
            association.id,
            SampleQualification.CONFIRMED,
            InvestigationActor(actor_id="codex:agent-1", kind=QualificationActorKind.AGENT),
            "agent cannot confirm membership",
            (),
        )
    confirmed = await g3.qualify(
        association.id,
        SampleQualification.CONFIRMED,
        InvestigationActor(actor_id="analyst:test", kind=QualificationActorKind.HUMAN),
        "human G3",
        (),
    )
    assert confirmed.qualification is SampleQualification.CONFIRMED
    assert confirmed.version == 2
    assert confirmed.changes[-1].actor_kind is QualificationActorKind.HUMAN


async def test_replay_same_workspace_revision_returns_existing_row() -> None:
    harness = _Harness()
    await harness.bind()
    revision = await harness.add_revision("results/replay.txt", b"same bytes")

    first = await harness.import_revision(
        revision,
        _observation_declaration(),
        idempotency_key="replay-attempt-0001",
    )
    replayed_key = await harness.import_revision(
        revision,
        _observation_declaration(),
        idempotency_key="replay-attempt-0001",
    )
    replayed_revision = await harness.import_revision(
        revision,
        _work_declaration(),
        idempotency_key="replay-attempt-0002",
    )
    assert replayed_key.id == first.id
    assert replayed_revision.id == first.id
    assert len(await harness.contributions.list_for_investigation(harness.investigation_id)) == 1
    assert len(await harness.observations.list_for_investigation(harness.investigation_id)) == 1
    assert len(harness.artifact_store.puts) == 1

    with pytest.raises(WorkContributionConflictError):
        await harness.service.import_revision(
            harness.investigation_id,
            workspace_id=harness.workspace_id,
            workspace_revision_id="rev-" + "0" * 32,
            declaration=_work_declaration(),
            authority=ContributionAuthority(origin=WorkContributionOrigin.HOST),
            idempotency_key="replay-attempt-0001",
        )


async def test_rejected_import_has_no_canonical_refs() -> None:
    harness = _Harness()
    await harness.bind()
    revision = await harness.add_revision("scripts/dropper.bin", b"MZ" + b"\x00" * 256)

    contribution = await harness.import_revision(
        revision,
        _work_declaration(),
        idempotency_key="rejected-pe-0001",
    )

    assert contribution.import_status is WorkContributionImportStatus.REJECTED
    assert contribution.validation_error_code is WorkContributionValidationCode.FORBIDDEN_FILE_TYPE
    assert contribution.content_blob_id is None
    assert contribution.observation_id is None
    assert contribution.finding_id is None
    assert contribution.sample_association_id is None
    assert harness.artifact_store.puts == []
    assert await harness.observations.list_for_investigation(harness.investigation_id) == ()
    assert await harness.findings.list_for_investigation(harness.investigation_id) == ()
    assert await harness.associations.list_for_investigation(harness.investigation_id) == ()


async def test_rejected_import_can_be_retried_with_new_attempt() -> None:
    harness = _Harness()
    await harness.bind()
    revision = await harness.add_revision("results/retry.json", b'{"ok":true}')
    missing = uuid4()

    rejected = await harness.import_revision(
        revision,
        _finding_declaration(statement="Cites a missing observation", observation_refs=(missing,)),
        idempotency_key="retry-attempt-0001",
    )
    retried = await harness.import_revision(
        revision,
        _work_declaration(),
        idempotency_key="retry-attempt-0002",
    )

    assert rejected.import_status is WorkContributionImportStatus.REJECTED
    assert (
        rejected.validation_error_code is WorkContributionValidationCode.OBSERVATION_REF_NOT_FOUND
    )
    assert retried.import_status is WorkContributionImportStatus.IMPORTED
    assert retried.id != rejected.id
    rows = await harness.contributions.list_for_investigation(harness.investigation_id)
    assert {row.id for row in rows} == {rejected.id, retried.id}
    active = await harness.contributions.get_non_rejected_for_revision(
        harness.workspace_id, revision.revision_id
    )
    assert active is not None and active.id == retried.id


def test_work_contribution_kind_mapping_closed() -> None:
    object_types = set(WorkContributionObjectType)
    expected: dict[tuple[WorkContributionObjectType, WorkContributionMethod], EvidenceKind] = {
        (WorkContributionObjectType.SAMPLE, WorkContributionMethod.HASH): EvidenceKind.OTHER,
        (WorkContributionObjectType.FILE, WorkContributionMethod.HASH): EvidenceKind.OTHER,
        (
            WorkContributionObjectType.SAMPLE,
            WorkContributionMethod.STATIC_ANALYSIS,
        ): EvidenceKind.BINARY_ANALYSIS,
        (
            WorkContributionObjectType.FILE,
            WorkContributionMethod.STATIC_ANALYSIS,
        ): EvidenceKind.BINARY_ANALYSIS,
        (
            WorkContributionObjectType.SAMPLE,
            WorkContributionMethod.STRING_EXTRACTION,
        ): EvidenceKind.BINARY_STRING,
        (
            WorkContributionObjectType.FILE,
            WorkContributionMethod.STRING_EXTRACTION,
        ): EvidenceKind.BINARY_STRING,
        (
            WorkContributionObjectType.SAMPLE,
            WorkContributionMethod.YARA_SCAN,
        ): EvidenceKind.YARA_MATCH,
        (
            WorkContributionObjectType.FILE,
            WorkContributionMethod.YARA_SCAN,
        ): EvidenceKind.YARA_MATCH,
        (
            WorkContributionObjectType.SAMPLE,
            WorkContributionMethod.DYNAMIC_BEHAVIOUR,
        ): EvidenceKind.OTHER,
        (
            WorkContributionObjectType.FILE,
            WorkContributionMethod.DYNAMIC_BEHAVIOUR,
        ): EvidenceKind.OTHER,
        (
            WorkContributionObjectType.HOST,
            WorkContributionMethod.DYNAMIC_BEHAVIOUR,
        ): EvidenceKind.OTHER,
    }
    network_objects = (
        WorkContributionObjectType.HOST,
        WorkContributionObjectType.IP,
        WorkContributionObjectType.DOMAIN,
        WorkContributionObjectType.URL,
    )
    for object_type in network_objects:
        expected[(object_type, WorkContributionMethod.NETWORK_CAPTURE_ANALYSIS)] = (
            EvidenceKind.OTHER
        )
        expected[(object_type, WorkContributionMethod.DNS_QUERY)] = EvidenceKind.OTHER
        expected[(object_type, WorkContributionMethod.HTTP_REQUEST)] = EvidenceKind.OTHER
    for object_type in object_types:
        expected[(object_type, WorkContributionMethod.NEGATIVE_CHECK)] = (
            EvidenceKind.NEGATIVE_RESULT
        )
    assert dict(WORK_CONTRIBUTION_KIND_MAP) == expected
    assert len(expected) == 29
    assert {
        EvidenceKind.VT_FILE_REPORT,
        EvidenceKind.VT_RELATIONSHIP,
        EvidenceKind.VT_BEHAVIOUR,
        EvidenceKind.SHODAN_OBSERVATION,
        EvidenceKind.CLAIM_SOURCE,
    }.isdisjoint(WORK_CONTRIBUTION_KIND_MAP.values())
    for pair, kind in expected.items():
        assert evidence_kind_for(*pair) is kind
    with pytest.raises(ContributionValidationError) as exc_info:
        evidence_kind_for(WorkContributionObjectType.SAMPLE, WorkContributionMethod.HTTP_REQUEST)
    assert exc_info.value.code is WorkContributionValidationCode.OBJECT_METHOD_NOT_ALLOWED


def test_work_contribution_blob_guard_refuses_executables_and_archives() -> None:
    for content in (
        b"MZ" + b"\x00" * 128,
        b"\x7fELF" + b"\x00" * 64,
        b"\xcf\xfa\xed\xfe" + b"\x00" * 64,
        b"\xca\xfe\xba\xbe" + b"\x00" * 64,
        b"PK\x03\x04" + b"\x00" * 64,
        b"\x1f\x8b" + b"\x00" * 64,
    ):
        with pytest.raises(ContributionValidationError) as exc_info:
            assert_contribution_blob_allowed(content)
        assert exc_info.value.code is WorkContributionValidationCode.FORBIDDEN_FILE_TYPE
    assert_contribution_blob_allowed(b"# Human note\n\nA hypothesis.\n")


def test_work_contribution_reverse_check_rejects_error_code_on_imported() -> None:
    with pytest.raises(ContributionValidationError) as exc_info:
        _row(validation_error_code=WorkContributionValidationCode.FORBIDDEN_FILE_TYPE)
    assert exc_info.value.code is WorkContributionValidationCode.INVALID_DECLARATION

    with pytest.raises(ContributionValidationError):
        _row(
            import_status=WorkContributionImportStatus.REJECTED,
            content_blob_id=None,
            content_sha256=None,
        )
    with pytest.raises(ContributionValidationError):
        _row(content_blob_id=None, content_sha256=None)

    rejected = _row(
        import_status=WorkContributionImportStatus.REJECTED,
        content_blob_id=None,
        content_sha256=None,
        validation_error_code=WorkContributionValidationCode.FORBIDDEN_FILE_TYPE,
    )
    assert rejected.import_status is WorkContributionImportStatus.REJECTED
    assert rejected.citable_as_evidence is False

    observation_id = uuid4()
    observation_import = _row(
        category=WorkContributionCategory.OBSERVATION, observation_id=observation_id
    )
    assert observation_import.observation_id == observation_id
    with pytest.raises(ContributionValidationError):
        _row(category=WorkContributionCategory.WORK_CONTRIBUTION, observation_id=observation_id)
    with pytest.raises(ContributionValidationError):
        _row(category=WorkContributionCategory.OBSERVATION)
    with pytest.raises(ContributionValidationError):
        _row(origin=WorkContributionOrigin.AGENT)
    with pytest.raises(ContributionValidationError):
        _row(origin=WorkContributionOrigin.HOST, lease_fence=1)


async def test_advisory_observation_not_importable_as_evidence() -> None:
    harness = _Harness()
    await harness.bind()
    advisory = await harness.add_observation(
        source="advisor.consult.response",
        object_type="advisor_response_received",
    )
    assert is_advisory_observation(advisory) is True
    revision = await harness.add_revision("results/finding.md", b"# finding")

    contribution = await harness.import_revision(
        revision,
        _finding_declaration(statement="Uses Bridge advice", observation_refs=(advisory.id,)),
        idempotency_key="advisory-finding-0001",
    )

    assert contribution.import_status is WorkContributionImportStatus.REJECTED
    assert (
        contribution.validation_error_code
        is WorkContributionValidationCode.ADVISORY_OBSERVATION_NOT_EVIDENCE
    )
    assert contribution.finding_id is None
    assert await harness.findings.list_for_investigation(harness.investigation_id) == ()


async def test_host_import_never_claims_lease_authority() -> None:
    harness = _Harness()
    await harness.bind()
    task = await harness.add_task()
    revision = await harness.add_revision(
        f"notes/agents/{task.id}/expired-note.md",
        b"A note observed after the agent lease expired.",
        origin="agent",
        task_id=task.id,
    )

    contribution = await harness.import_revision(
        revision,
        _work_declaration(),
        idempotency_key="host-import-0001",
        authority=ContributionAuthority(origin=WorkContributionOrigin.HOST),
    )

    assert contribution.origin is WorkContributionOrigin.HOST
    assert contribution.task_id is None
    assert contribution.lease_fence is None
    assert contribution.origin_actor_id is None
    assert contribution.category is WorkContributionCategory.WORK_CONTRIBUTION
    assert contribution.observation_id is None

    with pytest.raises(ValueError):
        ContributionAuthority(origin=WorkContributionOrigin.HOST, lease_fence=1)
    with pytest.raises(ValueError):
        ContributionAuthority(origin=WorkContributionOrigin.AGENT)


async def test_human_task_link_must_belong_to_the_investigation() -> None:
    harness = _Harness()
    await harness.bind()
    foreign_task = InvestigationTaskV1(
        investigation_id=uuid4(),
        task_key="lead-foreign",
        title="Foreign task",
        question="Other investigation",
        objective="Other investigation",
        priority=TaskPriority.NORMAL,
        status=TaskStatus.READY,
        status_version=1,
        result_verification_status=ResultVerificationStatus.UNVERIFIED,
        created_by=TaskActor(kind=TaskActorKind.HUMAN, actor_id="analyst:other"),
        created_at=NOW,
        updated_at=NOW,
    )
    await harness.tasks.add(foreign_task)
    revision = await harness.add_revision("notes/humaines/linked.md", b"# Linked\n")

    contribution = await harness.import_revision(
        revision,
        _work_declaration(),
        idempotency_key="human-task-link-0001",
        authority=ContributionAuthority(
            origin=WorkContributionOrigin.HUMAN, task_id=foreign_task.id
        ),
    )

    assert contribution.import_status is WorkContributionImportStatus.REJECTED
    assert (
        contribution.validation_error_code
        is WorkContributionValidationCode.TASK_NOT_IN_INVESTIGATION
    )
    assert contribution.task_id is None


async def test_internal_projection_is_skipped_without_reading_bytes() -> None:
    harness = _Harness()
    await harness.bind()
    revision = await harness.add_revision(
        ".autowork/file-index.json", b'{"schema":"workspace-file-index.v1"}'
    )

    contribution = await harness.import_revision(
        revision,
        _work_declaration(),
        idempotency_key="internal-projection-0001",
    )

    assert contribution.import_status is WorkContributionImportStatus.SKIPPED
    assert (
        contribution.validation_error_code is WorkContributionValidationCode.INTERNAL_WORKSPACE_PATH
    )
    assert contribution.content_blob_id is None
    assert harness.agent.reads == []
    assert harness.artifact_store.puts == []


async def test_changed_file_creates_new_revision_and_new_row() -> None:
    harness = _Harness()
    await harness.bind()
    first = await harness.add_revision("notes/humaines/note.md", b"# Note v1\n")
    second = await harness.add_revision("notes/humaines/note.md", b"# Note v2\n")
    assert first.revision_id != second.revision_id

    first_row = await harness.import_revision(
        first, _work_declaration(), idempotency_key="changed-file-0001"
    )
    second_row = await harness.import_revision(
        second, _work_declaration(), idempotency_key="changed-file-0002"
    )

    assert first_row.id != second_row.id
    assert first_row.file_sha256 != second_row.file_sha256
    assert len(await harness.contributions.list_for_investigation(harness.investigation_id)) == 2


async def test_agent_import_validates_task_lease_fence() -> None:
    harness = _Harness()
    await harness.bind()
    task = await harness.add_task(lease=True)
    session_id = await harness.add_agent_session(task)
    revision = await harness.add_revision(
        f"notes/agents/{task.id}/result.md", b"# Result\n", origin="agent", task_id=task.id
    )

    imported = await harness.import_revision(
        revision,
        _work_declaration(),
        idempotency_key="agent-import-0001",
        authority=ContributionAuthority(
            origin=WorkContributionOrigin.AGENT,
            actor_id="codex:operator-1",
            task_id=task.id,
            lease_fence=1,
            session_id=session_id,
        ),
    )
    assert imported.origin is WorkContributionOrigin.AGENT
    assert imported.task_id == task.id
    assert imported.lease_fence == 1
    assert imported.origin_actor_id == "codex:operator-1"

    stale = await harness.add_revision(
        f"notes/agents/{task.id}/stale.md", b"# Stale\n", origin="agent", task_id=task.id
    )
    with pytest.raises(Exception) as exc_info:
        await harness.import_revision(
            stale,
            _work_declaration(),
            idempotency_key="agent-import-0002",
            authority=ContributionAuthority(
                origin=WorkContributionOrigin.AGENT,
                actor_id="codex:operator-1",
                task_id=task.id,
                lease_fence=2,
                session_id=session_id,
            ),
        )
    assert getattr(exc_info.value, "details", {}).get("code") == "lease_fence_stale"

    other_task = await harness.add_task(lease=True)
    foreign = await harness.add_revision(
        f"notes/agents/{other_task.id}/foreign.md",
        b"# Other task\n",
        origin="agent",
        task_id=other_task.id,
    )
    rejected = await harness.import_revision(
        foreign,
        _work_declaration(),
        idempotency_key="agent-import-0003",
        authority=ContributionAuthority(
            origin=WorkContributionOrigin.AGENT,
            actor_id="codex:operator-1",
            task_id=task.id,
            lease_fence=1,
            session_id=session_id,
        ),
    )
    assert rejected.import_status is WorkContributionImportStatus.REJECTED
    assert (
        rejected.validation_error_code is WorkContributionValidationCode.TASK_NOT_IN_INVESTIGATION
    )


async def test_observation_metadata_records_exact_datum_and_is_bounded() -> None:
    declaration = ObservationDeclarationV1(
        object_type=WorkContributionObjectType.IP,
        method=WorkContributionMethod.DNS_QUERY,
        object_id="203.0.113.10",
        tool="dig",
        tool_version="9.18",
        parameters={"record_type": "A"},
    )
    line = observation_metadata_line(
        declaration=declaration,
        file_sha256="a" * 64,
        workspace_revision_id="rev-" + "0" * 32,
        file_origin="agent",
        observed_at=NOW,
    )
    assert line.startswith(OBSERVATION_METADATA_PREFIX)
    assert '"object_type":"ip"' in line
    assert '"method":"dns_query"' in line
    assert '"object_id":"203.0.113.10"' in line
    assert '"file_origin":"agent"' in line

    with pytest.raises(ContributionValidationError) as exc_info:
        ObservationDeclarationV1(
            object_type=WorkContributionObjectType.IP,
            method=WorkContributionMethod.HTTP_REQUEST,
            object_id="203.0.113.10",
            tool="curl",
            tool_version="8.0",
            parameters={"body": "x" * 2_000},
        )
    assert exc_info.value.code is WorkContributionValidationCode.OBSERVATION_METADATA_TOO_LARGE


async def test_workspace_content_hash_mismatch_is_rejected_not_imported() -> None:
    harness = _Harness()
    await harness.bind()
    revision = await harness.add_revision(
        "results/changed.txt", b"new bytes", declared_sha256="f" * 64
    )

    contribution = await harness.import_revision(
        revision,
        _work_declaration(),
        idempotency_key="changed-file-0001",
    )

    assert contribution.import_status is WorkContributionImportStatus.REJECTED
    assert (
        contribution.validation_error_code
        is WorkContributionValidationCode.WORKSPACE_REVISION_CONTENT_MISMATCH
    )
    assert contribution.content_sha256 is None


async def test_workspace_agent_transport_error_is_typed() -> None:
    harness = _Harness()
    await harness.bind()
    revision = await harness.add_revision("results/lost.txt", b"lost")
    harness.agent.read_error = WorkspaceHashMismatchError("content changed")

    with pytest.raises(Exception) as exc_info:
        await harness.import_revision(
            revision,
            _work_declaration(),
            idempotency_key="lost-content-0001",
        )
    assert getattr(exc_info.value, "status_code", None) == 422


class _OperatorClientIdentity:
    async def current(self) -> Identity:
        return Identity(
            actor_id="operator:operator-client",
            scopes=frozenset({IdentityScope.OPERATOR_CLIENT}),
        )


def _api_app(harness: _Harness, provider: Any) -> FastAPI:
    application = FastAPI()
    application.include_router(contributions_router)
    application.state.work_contribution_service = harness.service
    application.state.workspace_agent_port = harness.agent
    application.state.identity_provider = provider
    return application


async def test_contribution_routes_require_human_and_refuse_unsigned_agent() -> None:
    harness = _Harness()
    await harness.bind()
    revision = await harness.add_revision("notes/humaines/note.md", b"# Note\n")
    app = _api_app(harness, LocalIdentityProvider("dev-analyst"))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        refused_agent = await client.post(
            f"/api/investigations/{harness.investigation_id}/contributions",
            headers={"Idempotency-Key": "api-agent-0001"},
            json={
                "workspace_id": harness.workspace_id,
                "workspace_revision_id": revision.revision_id,
                "category": "work_contribution",
                "origin": "agent",
                "task_id": str(uuid4()),
                "lease_fence": 1,
            },
        )
        assert refused_agent.status_code == 403
        assert refused_agent.json()["detail"]["code"] == "operator_session_required"

        missing_key = await client.post(
            f"/api/investigations/{harness.investigation_id}/contributions",
            json={
                "workspace_id": harness.workspace_id,
                "workspace_revision_id": revision.revision_id,
            },
        )
        assert missing_key.status_code == 422

        imported = await client.post(
            f"/api/investigations/{harness.investigation_id}/contributions",
            headers={"Idempotency-Key": "api-host-0001"},
            json={
                "workspace_id": harness.workspace_id,
                "workspace_revision_id": revision.revision_id,
                "category": "work_contribution",
                "origin": "host",
            },
        )
        assert imported.status_code == 200, imported.text
        payload = imported.json()
        assert payload["import_status"] == "imported"
        assert payload["category"] == "work_contribution"
        assert payload["citable_as_evidence"] is False
        assert payload["origin"] == "host"

        listed = await client.get(f"/api/investigations/{harness.investigation_id}/contributions")
        assert listed.status_code == 200
        assert [item["id"] for item in listed.json()] == [payload["id"]]

        fetched = await client.get(
            f"/api/investigations/{harness.investigation_id}/contributions/{payload['id']}"
        )
        assert fetched.status_code == 200
        assert fetched.json()["id"] == payload["id"]

    operator_app = _api_app(harness, _OperatorClientIdentity())
    async with AsyncClient(
        transport=ASGITransport(app=operator_app), base_url="http://test"
    ) as client:
        refused = await client.get(f"/api/investigations/{harness.investigation_id}/contributions")
        assert refused.status_code == 403
        assert refused.json()["detail"]["code"] == "verified_human_operator_required"
