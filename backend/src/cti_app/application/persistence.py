from collections.abc import Sequence
from datetime import date, datetime
from types import TracebackType
from typing import Protocol, Self
from uuid import UUID

from cti_app.domain.blobs import BlobRecord
from cti_app.domain.briefs import BriefDraft, BriefEvidencePack
from cti_app.domain.collection import (
    Claim,
    CollectionAttempt,
    CollectionPolicySnapshot,
    DerivedArtifact,
    Indicator,
    RejectedModelProposal,
    SourceCollection,
)
from cti_app.domain.discovery import DiscoveryBatch
from cti_app.domain.editions import Edition, EditionAuditEvent, EditionStatus
from cti_app.domain.editorial import EditorialGroup, HumanDecision
from cti_app.domain.entities import ProvenanceEvent, Sample, SourceDocument, Subject
from cti_app.domain.jobs import Job, JobEvent, JobOperationalMetrics
from cti_app.domain.model_conversations import (
    ConversationPurpose,
    ConversationStatus,
    ModelConversation,
    ModelConversationTurn,
)
from cti_app.domain.model_runs import ModelOutputRejection, ModelProvider, ModelRun


class BlobRepository(Protocol):
    async def add(self, blob: BlobRecord) -> None: ...

    async def get(self, blob_id: UUID) -> BlobRecord | None: ...

    async def get_by_address(self, logical_bucket: str, sha256: str) -> BlobRecord | None: ...

    async def count_references(self, blob_id: UUID) -> int: ...

    async def delete(self, blob_id: UUID) -> None: ...


class SubjectRepository(Protocol):
    async def add(self, subject: Subject) -> None: ...

    async def get(self, subject_id: UUID) -> Subject | None: ...


class SourceDocumentRepository(Protocol):
    async def add(self, document: SourceDocument) -> None: ...

    async def get(self, document_id: UUID) -> SourceDocument | None: ...

    async def list_for_subject(self, subject_id: UUID) -> Sequence[SourceDocument]: ...


class SampleRepository(Protocol):
    async def add(self, sample: Sample) -> None: ...

    async def get(self, sample_id: UUID) -> Sample | None: ...

    async def list_for_subject(self, subject_id: UUID) -> Sequence[Sample]: ...


class ProvenanceRepository(Protocol):
    async def append(self, event: ProvenanceEvent) -> None: ...

    async def list_for_aggregate(
        self, aggregate_type: str, aggregate_id: UUID
    ) -> Sequence[ProvenanceEvent]: ...


class JobRepository(Protocol):
    async def add_if_absent(self, job: Job) -> bool: ...

    async def get(self, job_id: UUID) -> Job | None: ...

    async def get_for_update(self, job_id: UUID) -> Job | None: ...

    async def get_by_idempotency_key(self, idempotency_key: str) -> Job | None: ...

    async def save(self, job: Job) -> None: ...

    async def list_abandoned(self, heartbeat_before: datetime) -> Sequence[Job]: ...

    async def operational_metrics(self) -> JobOperationalMetrics: ...


class JobEventRepository(Protocol):
    async def append(self, event: JobEvent) -> None: ...

    async def list_for_job(self, job_id: UUID) -> Sequence[JobEvent]: ...


class EditionRepository(Protocol):
    async def add_if_absent(self, edition: Edition) -> bool: ...

    async def get(self, edition_id: UUID) -> Edition | None: ...

    async def get_by_logical_key(
        self, country_code: str, period_start: date, period_end: date
    ) -> Edition | None: ...

    async def update(self, edition: Edition, expected_version: int) -> bool: ...

    async def list(
        self,
        *,
        offset: int,
        limit: int,
        country_code: str | None,
        period_start: date | None,
        period_end: date | None,
        status: EditionStatus | None,
    ) -> tuple[Sequence[Edition], int]: ...


class EditionAuditRepository(Protocol):
    async def append(self, event: EditionAuditEvent) -> None: ...

    async def list_for_edition(self, edition_id: UUID) -> Sequence[EditionAuditEvent]: ...


class ModelRunRepository(Protocol):
    async def add(self, run: ModelRun) -> None: ...

    async def get(self, run_id: UUID) -> ModelRun | None: ...

    async def get_for_update(self, run_id: UUID) -> ModelRun | None: ...

    async def save(self, run: ModelRun) -> None: ...


class ModelOutputRejectionRepository(Protocol):
    async def append(self, rejection: ModelOutputRejection) -> None: ...

    async def list_for_run(self, run_id: UUID) -> Sequence[ModelOutputRejection]: ...


class ModelConversationRepository(Protocol):
    async def add(self, conversation: ModelConversation) -> None: ...

    async def get(self, conversation_id: UUID) -> ModelConversation | None: ...

    async def get_for_update(self, conversation_id: UUID) -> ModelConversation | None: ...

    async def save(self, conversation: ModelConversation) -> None: ...

    async def list(
        self,
        *,
        edition_id: UUID | None,
        subject_id: UUID | None,
        purpose: ConversationPurpose | None,
        status: ConversationStatus | None,
        provider: ModelProvider | None,
    ) -> Sequence[ModelConversation]: ...


class ModelConversationTurnRepository(Protocol):
    async def add(self, turn: ModelConversationTurn) -> None: ...

    async def get(self, turn_id: UUID) -> ModelConversationTurn | None: ...

    async def get_by_idempotency_key(self, key: str) -> ModelConversationTurn | None: ...

    async def list_for_conversation(
        self, conversation_id: UUID
    ) -> Sequence[ModelConversationTurn]: ...

    async def save(self, turn: ModelConversationTurn) -> None: ...


class DiscoveryBatchRepository(Protocol):
    async def add_if_absent(self, batch: DiscoveryBatch) -> bool: ...

    async def get(self, batch_id: UUID) -> DiscoveryBatch | None: ...

    async def get_by_request_hash(
        self, edition_id: UUID, request_hash: str
    ) -> DiscoveryBatch | None: ...

    async def list_for_edition(self, edition_id: UUID) -> Sequence[DiscoveryBatch]: ...

    async def save(self, batch: DiscoveryBatch) -> None: ...


class EditorialGroupRepository(Protocol):
    async def add(self, group: EditorialGroup) -> None: ...

    async def get(self, group_id: UUID) -> EditorialGroup | None: ...

    async def get_for_update(self, group_id: UUID) -> EditorialGroup | None: ...

    async def list_for_edition(self, edition_id: UUID) -> Sequence[EditorialGroup]: ...

    async def list_historical(self, edition_id: UUID) -> Sequence[EditorialGroup]: ...

    async def get_by_subject(self, subject_id: UUID) -> EditorialGroup | None: ...

    async def save(self, group: EditorialGroup) -> None: ...


class HumanDecisionRepository(Protocol):
    async def append(self, decision: HumanDecision) -> None: ...

    async def list_for_edition(self, edition_id: UUID) -> Sequence[HumanDecision]: ...


class SourceCollectionRepository(Protocol):
    async def add_if_absent(self, collection: SourceCollection) -> bool: ...

    async def get(self, collection_id: UUID) -> SourceCollection | None: ...

    async def get_for_update(self, collection_id: UUID) -> SourceCollection | None: ...

    async def get_by_candidate(
        self, subject_id: UUID, source_candidate_id: UUID
    ) -> SourceCollection | None: ...

    async def list_for_subject(self, subject_id: UUID) -> Sequence[SourceCollection]: ...

    async def save(self, collection: SourceCollection) -> None: ...


class CollectionAttemptRepository(Protocol):
    async def append(self, attempt: CollectionAttempt) -> None: ...

    async def list_for_collection(self, collection_id: UUID) -> Sequence[CollectionAttempt]: ...


class CollectionPolicySnapshotRepository(Protocol):
    async def add_if_absent(self, snapshot: CollectionPolicySnapshot) -> bool: ...

    async def get(self, snapshot_id: str) -> CollectionPolicySnapshot | None: ...


class DerivedArtifactRepository(Protocol):
    async def append(self, artifact: DerivedArtifact) -> None: ...

    async def get(self, artifact_id: UUID) -> DerivedArtifact | None: ...


class ClaimRepository(Protocol):
    async def append_many(self, claims: Sequence[Claim]) -> None: ...

    async def get(self, claim_id: UUID) -> Claim | None: ...

    async def list_for_subject(self, subject_id: UUID) -> Sequence[Claim]: ...


class IndicatorRepository(Protocol):
    async def append_many(self, indicators: Sequence[Indicator]) -> None: ...

    async def get(self, indicator_id: UUID) -> Indicator | None: ...

    async def list_for_subject(self, subject_id: UUID) -> Sequence[Indicator]: ...


class RejectedModelProposalRepository(Protocol):
    async def append_many(self, proposals: Sequence[RejectedModelProposal]) -> None: ...


class BriefEvidencePackRepository(Protocol):
    async def append(self, pack: BriefEvidencePack) -> None: ...

    async def get(self, pack_id: UUID) -> BriefEvidencePack | None: ...

    async def get_current(self, subject_id: UUID) -> BriefEvidencePack | None: ...

    async def get_by_hash(
        self, subject_id: UUID, content_hash: str
    ) -> BriefEvidencePack | None: ...

    async def list_for_subject(self, subject_id: UUID) -> Sequence[BriefEvidencePack]: ...


class BriefDraftRepository(Protocol):
    async def append(self, draft: BriefDraft) -> None: ...

    async def get(self, draft_id: UUID) -> BriefDraft | None: ...

    async def get_current(self, subject_id: UUID) -> BriefDraft | None: ...

    async def list_for_subject(self, subject_id: UUID) -> Sequence[BriefDraft]: ...


class UnitOfWork(Protocol):
    blobs: BlobRepository
    subjects: SubjectRepository
    source_documents: SourceDocumentRepository
    samples: SampleRepository
    provenance: ProvenanceRepository
    jobs: JobRepository
    job_events: JobEventRepository
    editions: EditionRepository
    edition_audit: EditionAuditRepository
    model_runs: ModelRunRepository
    model_output_rejections: ModelOutputRejectionRepository
    model_conversations: ModelConversationRepository
    model_conversation_turns: ModelConversationTurnRepository
    discovery_batches: DiscoveryBatchRepository
    editorial_groups: EditorialGroupRepository
    human_decisions: HumanDecisionRepository
    source_collections: SourceCollectionRepository
    collection_attempts: CollectionAttemptRepository
    collection_policy_snapshots: CollectionPolicySnapshotRepository
    derived_artifacts: DerivedArtifactRepository
    claims: ClaimRepository
    indicators: IndicatorRepository
    rejected_model_proposals: RejectedModelProposalRepository
    brief_evidence_packs: BriefEvidencePackRepository
    brief_drafts: BriefDraftRepository

    async def __aenter__(self) -> Self: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...

    async def commit(self) -> None: ...

    async def rollback(self) -> None: ...


class UnitOfWorkFactory(Protocol):
    def __call__(self) -> UnitOfWork: ...


class JobUnitOfWork(Protocol):
    jobs: JobRepository
    job_events: JobEventRepository

    async def __aenter__(self) -> Self: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...

    async def commit(self) -> None: ...

    async def rollback(self) -> None: ...


class JobUnitOfWorkFactory(Protocol):
    def __call__(self) -> JobUnitOfWork: ...


class EditionUnitOfWork(Protocol):
    editions: EditionRepository
    edition_audit: EditionAuditRepository

    async def __aenter__(self) -> Self: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...

    async def commit(self) -> None: ...

    async def rollback(self) -> None: ...


class EditionUnitOfWorkFactory(Protocol):
    def __call__(self) -> EditionUnitOfWork: ...


class DiscoveryUnitOfWork(Protocol):
    discovery_batches: DiscoveryBatchRepository

    async def __aenter__(self) -> Self: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...

    async def commit(self) -> None: ...

    async def rollback(self) -> None: ...


class DiscoveryUnitOfWorkFactory(Protocol):
    def __call__(self) -> DiscoveryUnitOfWork: ...
