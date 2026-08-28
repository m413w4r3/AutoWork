from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime
from types import TracebackType
from typing import Protocol, Self
from uuid import UUID

from cti_app.domain.analysis import SampleFeatureSetV1
from cti_app.domain.blobs import BlobRecord
from cti_app.domain.briefs import BriefDraft, BriefEvidencePack
from cti_app.domain.capabilities import CapabilitySet
from cti_app.domain.code_features import CodeFeatureSet
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
from cti_app.domain.discovery_cumulative import (
    DiscoveryIntake,
    DiscoveryMergeRun,
    DiscoverySnapshot,
    DiscoverySubjectIdentity,
    SubjectContribution,
    SubjectMergeEvent,
)
from cti_app.domain.editions import Edition, EditionAuditEvent, EditionStatus
from cti_app.domain.editorial import AnalystDecision, EditorialGroup, HumanDecision
from cti_app.domain.entities import ProvenanceEvent, Sample, SourceDocument, Subject
from cti_app.domain.goodware import GoodwareBaseline, GoodwareIndexArtifact, GoodwareSource
from cti_app.domain.invariants import (
    CandidateInvariant,
    FeatureMeasurements,
    InvariantCategory,
    InvariantProvenance,
    InvariantRejection,
    InvariantRejectionCause,
    InvariantStatus,
    InvariantTransition,
    InvariantType,
    ResolvedFeature,
)
from cti_app.domain.jobs import Job, JobEvent, JobOperationalMetrics
from cti_app.domain.model_conversations import (
    ConversationPurpose,
    ConversationStatus,
    ModelConversation,
    ModelConversationTurn,
)
from cti_app.domain.model_runs import ModelOutputRejection, ModelProvider, ModelRun
from cti_app.domain.production import (
    AnalystInputPack,
    AnalystInvestigation,
    EditionProductionBatch,
    EditionProductionBatchItem,
    ProductionArtifact,
    ProductionInputSnapshot,
    SampleAcquisitionAttempt,
    SubjectProductionRun,
)
from cti_app.domain.reference_corpus import ReferenceMember, ReferenceMemberDispute
from cti_app.domain.virustotal import VirusTotalFileView, VirusTotalObservation


class BlobRepository(Protocol):
    async def add(self, blob: BlobRecord) -> None: ...

    async def get(self, blob_id: UUID) -> BlobRecord | None: ...

    async def get_by_address(self, logical_bucket: str, sha256: str) -> BlobRecord | None: ...

    async def count_references(self, blob_id: UUID) -> int: ...

    async def delete(self, blob_id: UUID) -> None: ...


class GoodwareBaselineRepository(Protocol):
    async def get_by_baseline_fingerprint_sha256(
        self, baseline_fingerprint_sha256: str
    ) -> GoodwareBaseline | None: ...

    async def get(self, baseline_id: UUID) -> GoodwareBaseline | None: ...

    async def get_index_artifact(
        self,
        baseline_id: UUID,
        *,
        index_format_version: str,
        key_version: str,
    ) -> GoodwareIndexArtifact | None: ...

    async def add_if_absent(self, baseline: GoodwareBaseline) -> bool: ...
    async def add_sources(self, baseline_id: UUID, sources: Sequence[GoodwareSource]) -> None: ...
    async def add_index_artifact(self, artifact: GoodwareIndexArtifact) -> None: ...


class InvestigationGoodwareBaselineRepository(Protocol):
    async def get(self, investigation_id: UUID) -> UUID | None: ...
    async def add_if_absent(self, investigation_id: UUID, baseline_id: UUID) -> bool: ...


class ReferenceMemberRepository(Protocol):
    async def append(self, member: ReferenceMember) -> ReferenceMember: ...
    async def get(self, member_id: UUID) -> ReferenceMember | None: ...
    async def list(self) -> Sequence[ReferenceMember]: ...
    async def append_dispute(self, dispute: ReferenceMemberDispute) -> None: ...
    async def get_dispute(self, member_id: UUID) -> ReferenceMemberDispute | None: ...
    async def list_disputes(self, member_id: UUID) -> Sequence[ReferenceMemberDispute]: ...
    async def list_feature_members(
        self, feature_kind: str, normalized_value: str
    ) -> Sequence[tuple[UUID, str]]: ...
    async def count_benign_feature_occurrences(
        self, feature_kind: str, normalized_value: str
    ) -> int: ...
    async def list_feature_members_bulk(
        self, feature_kind: str, normalized_values: Sequence[str]
    ) -> Mapping[str, Sequence[tuple[UUID, str]]]: ...
    async def count_benign_feature_occurrences_bulk(
        self, feature_kind: str, normalized_values: Sequence[str]
    ) -> Mapping[str, int]: ...
    async def count_eligible_malware_samples(self) -> int: ...
    async def count_eligible_malware_samples_by_family(self) -> Mapping[str, int]: ...


class CapabilitySetRepository(Protocol):
    async def get(
        self, sample_id: UUID, tool_version: str, ruleset_sha256: str, parameters_sha256: str
    ) -> CapabilitySet | None: ...
    async def add_if_absent(self, capability_set: CapabilitySet, blob_id: UUID) -> bool: ...
    async def index(self, capability_set: CapabilitySet) -> None: ...

    async def list_for_samples(
        self, sample_ids: Sequence[UUID]
    ) -> Sequence[Mapping[str, object]]: ...


class CodeFeatureSetRepository(Protocol):
    async def get(
        self,
        sample_id: UUID,
        tool_version: str,
        escaper_compatibility_version: str,
        intel_pic_hash_escape_version: str,
        parameters_sha256: str,
    ) -> CodeFeatureSet | None: ...

    async def add_if_absent(self, feature_set: CodeFeatureSet, feature_blob_id: UUID) -> bool: ...

    async def index(self, feature_set: CodeFeatureSet) -> None: ...

    async def list_for_samples(
        self, sample_ids: Sequence[UUID]
    ) -> Sequence[Mapping[str, object]]: ...


class InvariantRepository(Protocol):
    async def lock_proposal(self, proposal_key: str) -> None: ...

    async def get_proposal_outcome(
        self, proposal_key: str
    ) -> tuple[CandidateInvariant | None, InvariantRejection | None]: ...

    async def resolve_provenance(
        self,
        *,
        provenance: InvariantProvenance,
        invariant_type: InvariantType,
        pattern: str,
    ) -> ResolvedFeature | None: ...

    async def measure_feature(
        self,
        *,
        feature_kind: str,
        normalized_value: str,
        snapshot_sample_ids: Sequence[UUID],
    ) -> FeatureMeasurements: ...

    async def measure_features_bulk(
        self,
        descriptors: Sequence[tuple[str, str]],
        snapshot_sample_ids: Sequence[UUID],
    ) -> Mapping[tuple[str, str], FeatureMeasurements]: ...

    async def add_invariant(self, invariant: CandidateInvariant) -> CandidateInvariant: ...

    async def get_invariant(self, invariant_id: UUID) -> CandidateInvariant | None: ...

    async def get_invariant_by_proposal_key(
        self, proposal_key: str
    ) -> CandidateInvariant | None: ...

    async def list_invariants(
        self,
        *,
        investigation_id: UUID | None = None,
        status: InvariantStatus | None = None,
        invariant_type: InvariantType | None = None,
        category: InvariantCategory | None = None,
    ) -> Sequence[CandidateInvariant]: ...

    async def add_rejection(self, rejection: InvariantRejection) -> InvariantRejection: ...

    async def transition(
        self,
        *,
        invariant_id: UUID,
        to_status: InvariantStatus,
        actor_id: str,
        occurred_at: datetime,
        reason: str,
    ) -> CandidateInvariant: ...

    async def list_transitions(self, invariant_id: UUID) -> Sequence[InvariantTransition]: ...

    async def list_rejections(
        self,
        *,
        investigation_id: UUID | None = None,
        cycle_number: int | None = None,
        cause: InvariantRejectionCause | None = None,
    ) -> Sequence[InvariantRejection]: ...

    async def rejection_statistics(
        self, *, investigation_id: UUID | None = None, cycle_number: int | None = None
    ) -> Mapping[str, int]: ...


class SubjectRepository(Protocol):
    async def add(self, subject: Subject) -> None: ...

    async def get(self, subject_id: UUID) -> Subject | None: ...


class SourceDocumentRepository(Protocol):
    async def add(self, document: SourceDocument) -> None: ...

    async def get(self, document_id: UUID) -> SourceDocument | None: ...

    async def save(self, document: SourceDocument) -> None: ...

    async def list_for_subject(self, subject_id: UUID) -> Sequence[SourceDocument]: ...


class SampleRepository(Protocol):
    async def add(self, sample: Sample) -> None: ...

    async def get(self, sample_id: UUID) -> Sample | None: ...

    async def list_for_subject(self, subject_id: UUID) -> Sequence[Sample]: ...

    async def get_by_subject_and_blob(self, subject_id: UUID, blob_id: UUID) -> Sample | None: ...
    async def save(self, sample: Sample) -> None: ...


class SampleFeatureSetRepository(Protocol):
    async def get(
        self, sample_id: UUID, extractor_version: str, parameters_sha256: str
    ) -> SampleFeatureSetV1 | None: ...
    async def add_if_absent(
        self, feature_set: SampleFeatureSetV1, feature_blob_id: UUID
    ) -> bool: ...
    async def index(self, feature_set: SampleFeatureSetV1) -> None: ...

    async def list_for_samples(
        self, sample_ids: Sequence[UUID]
    ) -> Sequence[Mapping[str, object]]: ...


class SampleAcquisitionAttemptRepository(Protocol):
    async def find_successful(
        self, investigation_id: UUID, requested_hash: str
    ) -> SampleAcquisitionAttempt | None: ...

    async def append(self, attempt: SampleAcquisitionAttempt) -> None: ...


class ProvenanceRepository(Protocol):
    async def append(self, event: ProvenanceEvent) -> None: ...

    async def list_for_aggregate(
        self, aggregate_type: str, aggregate_id: UUID
    ) -> Sequence[ProvenanceEvent]: ...


class VirusTotalObservationRepository(Protocol):
    async def add(self, observation: VirusTotalObservation) -> None: ...

    async def find_file_report_checkpoint(
        self, checkpoint_id: str, file_hash: str
    ) -> VirusTotalObservation | None: ...


class VirusTotalFileViewRepository(Protocol):
    async def add_if_absent(self, view: VirusTotalFileView) -> bool: ...


class JobRepository(Protocol):
    async def add_if_absent(self, job: Job) -> bool: ...

    async def get(self, job_id: UUID) -> Job | None: ...

    async def get_for_update(self, job_id: UUID) -> Job | None: ...

    async def get_by_idempotency_key(self, idempotency_key: str) -> Job | None: ...

    async def save(self, job: Job) -> None: ...

    async def list_abandoned(self, heartbeat_before: datetime) -> Sequence[Job]: ...

    async def list_for_aggregate(
        self, aggregate_type: str, aggregate_id: UUID, *, kind: str | None = None
    ) -> Sequence[Job]: ...

    async def operational_metrics(self) -> JobOperationalMetrics: ...


class JobEventRepository(Protocol):
    async def append(self, event: JobEvent) -> None: ...

    async def list_for_job(self, job_id: UUID) -> Sequence[JobEvent]: ...


class EditionRepository(Protocol):
    async def add_if_absent(self, edition: Edition) -> bool: ...

    async def get(self, edition_id: UUID) -> Edition | None: ...

    async def get_for_update(self, edition_id: UUID) -> Edition | None: ...

    async def get_by_logical_key(
        self, country_code: str, period_start: date, period_end: date
    ) -> Edition | None: ...

    async def update(self, edition: Edition, expected_version: int) -> bool: ...

    async def delete(self, edition_id: UUID, expected_version: int) -> bool: ...

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

    async def delete_for_edition(self, edition_id: UUID) -> None: ...


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


class DiscoveryIntakeRepository(Protocol):
    async def add_if_absent(self, intake: DiscoveryIntake) -> bool: ...

    async def get(self, intake_id: UUID) -> DiscoveryIntake | None: ...

    async def get_by_batch(self, batch_id: UUID) -> DiscoveryIntake | None: ...

    async def list_for_edition(self, edition_id: UUID) -> Sequence[DiscoveryIntake]: ...

    async def next_sequence(self, edition_id: UUID) -> int: ...


class DiscoverySubjectIdentityRepository(Protocol):
    async def add_many_if_absent(self, identities: Sequence[DiscoverySubjectIdentity]) -> None: ...

    async def get(self, subject_id: UUID) -> DiscoverySubjectIdentity | None: ...

    async def list_for_edition(self, edition_id: UUID) -> Sequence[DiscoverySubjectIdentity]: ...

    async def resolve_canonical_subject(self, subject_id: UUID) -> UUID: ...

    async def contribution_closure(self, subject_id: UUID) -> Sequence[SubjectContribution]: ...


class SubjectMergeEventRepository(Protocol):
    async def append_many(self, events: Sequence[SubjectMergeEvent]) -> None: ...

    async def list_for_edition(self, edition_id: UUID) -> Sequence[SubjectMergeEvent]: ...


class DiscoverySnapshotRepository(Protocol):
    async def append(self, snapshot: DiscoverySnapshot) -> None: ...

    async def get(self, snapshot_id: UUID) -> DiscoverySnapshot | None: ...

    async def get_for_intake(self, intake_id: UUID) -> DiscoverySnapshot | None: ...

    async def get_active(self, edition_id: UUID) -> DiscoverySnapshot | None: ...

    async def get_active_for_update(self, edition_id: UUID) -> DiscoverySnapshot | None: ...

    async def deactivate(self, snapshot_id: UUID) -> None: ...


class DiscoveryMergeRunRepository(Protocol):
    async def add_if_absent(self, run: DiscoveryMergeRun) -> bool: ...

    async def mark_resolved(self, run_id: UUID) -> None: ...

    async def get(self, run_id: UUID) -> DiscoveryMergeRun | None: ...

    async def get_by_input_hash(self, merge_input_hash: str) -> DiscoveryMergeRun | None: ...

    async def list_for_edition(self, edition_id: UUID) -> Sequence[DiscoveryMergeRun]: ...


class SubjectContributionRepository(Protocol):
    async def append_many(self, contributions: Sequence[SubjectContribution]) -> None: ...

    async def list_for_subject(self, subject_id: UUID) -> Sequence[SubjectContribution]: ...

    async def list_recent_subject_ids(
        self, edition_id: UUID, *, minimum_snapshot_version: int
    ) -> Sequence[UUID]: ...


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

    async def get_by_canonical_url(
        self, subject_id: UUID, canonical_url: str
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
    goodware_baselines: GoodwareBaselineRepository
    investigation_goodware_baselines: InvestigationGoodwareBaselineRepository
    reference_members: ReferenceMemberRepository
    capability_sets: CapabilitySetRepository
    code_feature_sets: CodeFeatureSetRepository
    invariants: InvariantRepository
    subjects: SubjectRepository
    source_documents: SourceDocumentRepository
    samples: SampleRepository
    sample_feature_sets: SampleFeatureSetRepository
    sample_acquisition_attempts: SampleAcquisitionAttemptRepository
    provenance: ProvenanceRepository
    virustotal_observations: VirusTotalObservationRepository
    virustotal_file_views: VirusTotalFileViewRepository
    analyst_investigations: AnalystInvestigationRepository
    analyst_decisions: AnalystDecisionRepository
    analyst_input_packs: AnalystInputPackRepository
    jobs: JobRepository
    job_events: JobEventRepository
    editions: EditionRepository
    edition_audit: EditionAuditRepository
    model_runs: ModelRunRepository
    model_output_rejections: ModelOutputRejectionRepository
    model_conversations: ModelConversationRepository
    model_conversation_turns: ModelConversationTurnRepository
    discovery_batches: DiscoveryBatchRepository
    discovery_intakes: DiscoveryIntakeRepository
    discovery_subject_identities: DiscoverySubjectIdentityRepository
    subject_merge_events: SubjectMergeEventRepository
    discovery_snapshots: DiscoverySnapshotRepository
    discovery_merge_runs: DiscoveryMergeRunRepository
    subject_contributions: SubjectContributionRepository
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
    # Defined further down in this module.
    subject_production_runs: SubjectProductionRunRepository
    production_input_snapshots: ProductionInputSnapshotRepository
    production_artifacts: ProductionArtifactRepository
    edition_production_batches: EditionProductionBatchRepository
    edition_production_batch_items: EditionProductionBatchItemRepository

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


class SubjectProductionRunRepository(Protocol):
    async def add(self, run: SubjectProductionRun) -> None: ...

    async def get(self, run_id: UUID) -> SubjectProductionRun | None: ...

    async def get_for_update(self, run_id: UUID) -> SubjectProductionRun | None: ...

    async def save(self, run: SubjectProductionRun) -> None: ...

    async def get_current_for_subject(self, subject_id: UUID) -> SubjectProductionRun | None: ...

    async def list_for_edition(self, edition_id: UUID) -> Sequence[SubjectProductionRun]: ...

    async def allocate_next_run_number(self, subject_id: UUID) -> int: ...


class ProductionArtifactRepository(Protocol):
    async def append(self, artifact: ProductionArtifact) -> None: ...

    async def get(self, artifact_id: UUID) -> ProductionArtifact | None: ...

    async def get_current(self, run_id: UUID, stage: str) -> ProductionArtifact | None: ...

    async def list_for_run(self, run_id: UUID) -> Sequence[ProductionArtifact]: ...

    async def mark_downstream_stale(self, run_id: UUID, stage: str) -> None: ...

    async def mark_from_stage_stale(self, run_id: UUID, stage: str) -> list[str]: ...


class ProductionInputSnapshotRepository(Protocol):
    async def add(self, snapshot: ProductionInputSnapshot) -> None: ...

    async def get(self, snapshot_id: UUID) -> ProductionInputSnapshot | None: ...

    async def get_by_run(self, production_run_id: UUID) -> ProductionInputSnapshot | None: ...


class AnalystInvestigationRepository(Protocol):
    async def get(self, investigation_id: UUID) -> AnalystInvestigation | None: ...
    async def get_for_run(self, run_id: UUID) -> AnalystInvestigation | None: ...
    async def add(self, investigation: AnalystInvestigation) -> None: ...
    async def save(self, investigation: AnalystInvestigation) -> None: ...


class AnalystDecisionRepository(Protocol):
    async def append(self, decision: AnalystDecision) -> None: ...
    async def list_for_investigation(self, investigation_id: UUID) -> Sequence[AnalystDecision]: ...


class AnalystInputPackRepository(Protocol):
    async def get_for_investigation(self, investigation_id: UUID) -> AnalystInputPack | None: ...
    async def append(self, pack: AnalystInputPack) -> None: ...


class EditionProductionBatchRepository(Protocol):
    async def add(self, batch: EditionProductionBatch) -> None: ...

    async def get(self, batch_id: UUID) -> EditionProductionBatch | None: ...

    async def get_for_update(self, batch_id: UUID) -> EditionProductionBatch | None: ...

    async def save(self, batch: EditionProductionBatch) -> None: ...

    async def get_active_for_edition(self, edition_id: UUID) -> EditionProductionBatch | None: ...

    async def get_latest_for_edition(self, edition_id: UUID) -> EditionProductionBatch | None: ...


class EditionProductionBatchItemRepository(Protocol):
    async def append_many(self, items: Sequence[EditionProductionBatchItem]) -> None: ...

    async def list_for_batch(self, batch_id: UUID) -> Sequence[EditionProductionBatchItem]: ...

    async def get_by_run(self, run_id: UUID) -> EditionProductionBatchItem | None: ...


class ProductionUnitOfWork(Protocol):
    jobs: JobRepository
    editions: EditionRepository
    edition_audit: EditionAuditRepository
    subject_production_runs: SubjectProductionRunRepository
    production_input_snapshots: ProductionInputSnapshotRepository
    production_artifacts: ProductionArtifactRepository
    analyst_investigations: AnalystInvestigationRepository
    analyst_decisions: AnalystDecisionRepository
    analyst_input_packs: AnalystInputPackRepository
    discovery_batches: DiscoveryBatchRepository
    editorial_groups: EditorialGroupRepository
    source_collections: SourceCollectionRepository
    edition_production_batches: EditionProductionBatchRepository
    edition_production_batch_items: EditionProductionBatchItemRepository

    async def __aenter__(self) -> Self: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...

    async def commit(self) -> None: ...

    async def rollback(self) -> None: ...


class ProductionUnitOfWorkFactory(Protocol):
    def __call__(self) -> ProductionUnitOfWork: ...
