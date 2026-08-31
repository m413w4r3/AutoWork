from types import TracebackType

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cti_app.application.edition_review import EditionReviewReadRepository
from cti_app.application.persistence import (
    AnalystDecisionRepository,
    AnalystInputPackRepository,
    AnalystInvestigationRepository,
    BlobRepository,
    CapabilitySetRepository,
    ClaimRepository,
    CodeFeatureSetRepository,
    CollectionAttemptRepository,
    CollectionPolicySnapshotRepository,
    DerivedArtifactRepository,
    DiscoveryBatchRepository,
    DiscoveryIntakeRepository,
    DiscoveryMergeRunRepository,
    DiscoverySnapshotRepository,
    DiscoverySubjectIdentityRepository,
    EditionAuditRepository,
    EditionProductionBatchItemRepository,
    EditionProductionBatchRepository,
    EditionReleaseRepository,
    EditionRepository,
    EditorialGroupRepository,
    GoodwareBaselineRepository,
    HumanDecisionRepository,
    IndicatorRepository,
    InvariantRepository,
    InvestigationGoodwareBaselineRepository,
    JobEventRepository,
    JobRepository,
    ModelConversationRepository,
    ModelConversationTurnRepository,
    ModelOutputRejectionRepository,
    ModelRunRepository,
    ProductionArtifactRepository,
    ProductionInputSnapshotRepository,
    ProductionReuseInvalidationRepository,
    ProvenanceRepository,
    PublicationManifestEntryRepository,
    PublicationManifestExclusionRepository,
    PublicationManifestRepository,
    PublicationReviewDecisionRepository,
    ReferenceMemberRepository,
    RejectedModelProposalRepository,
    SampleAcquisitionAttemptRepository,
    SampleFeatureSetRepository,
    SampleRepository,
    SourceCollectionRepository,
    SourceDocumentRepository,
    SourceExtractionRepository,
    SubjectContributionRepository,
    SubjectMergeEventRepository,
    SubjectProductionRunRepository,
    SubjectRepository,
    VirusTotalFileViewRepository,
    VirusTotalObservationRepository,
)
from cti_app.application.production_read_model import BatchStatusReadRepository
from cti_app.infrastructure.database.repositories.collection import (
    SqlAlchemyClaimRepository,
    SqlAlchemyCollectionAttemptRepository,
    SqlAlchemyCollectionPolicySnapshotRepository,
    SqlAlchemyDerivedArtifactRepository,
    SqlAlchemyIndicatorRepository,
    SqlAlchemyRejectedModelProposalRepository,
    SqlAlchemySourceCollectionRepository,
)
from cti_app.infrastructure.database.repositories.core import (
    SqlAlchemyBlobRepository,
    SqlAlchemyCapabilitySetRepository,
    SqlAlchemyCodeFeatureSetRepository,
    SqlAlchemyGoodwareBaselineRepository,
    SqlAlchemyInvestigationGoodwareBaselineRepository,
    SqlAlchemyProvenanceRepository,
    SqlAlchemyReferenceMemberRepository,
    SqlAlchemySampleFeatureSetRepository,
    SqlAlchemySampleRepository,
    SqlAlchemySourceDocumentRepository,
    SqlAlchemySubjectRepository,
    SqlAlchemyVirusTotalFileViewRepository,
    SqlAlchemyVirusTotalObservationRepository,
)
from cti_app.infrastructure.database.repositories.discovery import (
    SqlAlchemyDiscoveryBatchRepository,
)
from cti_app.infrastructure.database.repositories.discovery_cumulative import (
    SqlAlchemyDiscoveryIntakeRepository,
    SqlAlchemyDiscoveryMergeRunRepository,
    SqlAlchemyDiscoverySnapshotRepository,
    SqlAlchemyDiscoverySubjectIdentityRepository,
    SqlAlchemySubjectContributionRepository,
    SqlAlchemySubjectMergeEventRepository,
)
from cti_app.infrastructure.database.repositories.edition_publication import (
    SqlAlchemyEditionReleaseRepository,
    SqlAlchemyPublicationManifestEntryRepository,
    SqlAlchemyPublicationManifestExclusionRepository,
    SqlAlchemyPublicationManifestRepository,
)
from cti_app.infrastructure.database.repositories.editions import (
    SqlAlchemyEditionAuditRepository,
    SqlAlchemyEditionRepository,
)
from cti_app.infrastructure.database.repositories.editorial import (
    SqlAlchemyEditorialGroupRepository,
    SqlAlchemyHumanDecisionRepository,
)
from cti_app.infrastructure.database.repositories.invariants import (
    SqlAlchemyInvariantRepository,
)
from cti_app.infrastructure.database.repositories.jobs import (
    SqlAlchemyJobEventRepository,
    SqlAlchemyJobRepository,
)
from cti_app.infrastructure.database.repositories.model_conversations import (
    SqlAlchemyModelConversationRepository,
    SqlAlchemyModelConversationTurnRepository,
)
from cti_app.infrastructure.database.repositories.model_runs import (
    SqlAlchemyModelOutputRejectionRepository,
    SqlAlchemyModelRunRepository,
)
from cti_app.infrastructure.database.repositories.production import (
    SqlAlchemyAnalystDecisionRepository,
    SqlAlchemyAnalystInputPackRepository,
    SqlAlchemyAnalystInvestigationRepository,
    SqlAlchemyBatchStatusReadRepository,
    SqlAlchemyEditionProductionBatchItemRepository,
    SqlAlchemyEditionProductionBatchRepository,
    SqlAlchemyProductionArtifactRepository,
    SqlAlchemyProductionInputSnapshotRepository,
    SqlAlchemyProductionReuseInvalidationRepository,
    SqlAlchemySampleAcquisitionAttemptRepository,
    SqlAlchemySourceExtractionRepository,
    SqlAlchemySubjectProductionRunRepository,
)
from cti_app.infrastructure.database.repositories.publication_review import (
    SqlAlchemyEditionReviewReadRepository,
    SqlAlchemyPublicationReviewDecisionRepository,
)


class SqlAlchemyUnitOfWork:
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
    subject_production_runs: SubjectProductionRunRepository
    production_input_snapshots: ProductionInputSnapshotRepository
    production_artifacts: ProductionArtifactRepository
    production_reuse_invalidations: ProductionReuseInvalidationRepository
    source_extractions: SourceExtractionRepository
    analyst_investigations: AnalystInvestigationRepository
    analyst_decisions: AnalystDecisionRepository
    analyst_input_packs: AnalystInputPackRepository
    edition_production_batches: EditionProductionBatchRepository
    edition_production_batch_items: EditionProductionBatchItemRepository
    batch_status_read_model: BatchStatusReadRepository
    edition_review_read_model: EditionReviewReadRepository
    publication_review_decisions: PublicationReviewDecisionRepository
    publication_manifests: PublicationManifestRepository
    publication_manifest_entries: PublicationManifestEntryRepository
    publication_manifest_exclusions: PublicationManifestExclusionRepository
    edition_releases: EditionReleaseRepository

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory
        self._session: AsyncSession | None = None
        self._committed = False

    async def __aenter__(self) -> "SqlAlchemyUnitOfWork":
        self._session = self._session_factory()
        self.blobs = SqlAlchemyBlobRepository(self._session)
        self.goodware_baselines = SqlAlchemyGoodwareBaselineRepository(self._session)
        self.investigation_goodware_baselines = SqlAlchemyInvestigationGoodwareBaselineRepository(
            self._session
        )
        self.reference_members = SqlAlchemyReferenceMemberRepository(self._session)
        self.capability_sets = SqlAlchemyCapabilitySetRepository(self._session)
        self.code_feature_sets = SqlAlchemyCodeFeatureSetRepository(self._session)
        self.invariants = SqlAlchemyInvariantRepository(self._session)
        self.subjects = SqlAlchemySubjectRepository(self._session)
        self.source_documents = SqlAlchemySourceDocumentRepository(self._session)
        self.samples = SqlAlchemySampleRepository(self._session)
        self.sample_feature_sets = SqlAlchemySampleFeatureSetRepository(self._session)
        self.sample_acquisition_attempts = SqlAlchemySampleAcquisitionAttemptRepository(
            self._session
        )
        self.provenance = SqlAlchemyProvenanceRepository(self._session)
        self.virustotal_observations = SqlAlchemyVirusTotalObservationRepository(self._session)
        self.virustotal_file_views = SqlAlchemyVirusTotalFileViewRepository(self._session)
        self.jobs = SqlAlchemyJobRepository(self._session)
        self.job_events = SqlAlchemyJobEventRepository(self._session)
        self.editions = SqlAlchemyEditionRepository(self._session)
        self.edition_audit = SqlAlchemyEditionAuditRepository(self._session)
        self.model_runs = SqlAlchemyModelRunRepository(self._session)
        self.model_output_rejections = SqlAlchemyModelOutputRejectionRepository(self._session)
        self.model_conversations = SqlAlchemyModelConversationRepository(self._session)
        self.model_conversation_turns = SqlAlchemyModelConversationTurnRepository(self._session)
        self.discovery_batches = SqlAlchemyDiscoveryBatchRepository(self._session)
        self.discovery_intakes = SqlAlchemyDiscoveryIntakeRepository(self._session)
        self.discovery_subject_identities = SqlAlchemyDiscoverySubjectIdentityRepository(
            self._session
        )
        self.subject_merge_events = SqlAlchemySubjectMergeEventRepository(self._session)
        self.discovery_snapshots = SqlAlchemyDiscoverySnapshotRepository(self._session)
        self.discovery_merge_runs = SqlAlchemyDiscoveryMergeRunRepository(self._session)
        self.subject_contributions = SqlAlchemySubjectContributionRepository(self._session)
        self.editorial_groups = SqlAlchemyEditorialGroupRepository(self._session)
        self.human_decisions = SqlAlchemyHumanDecisionRepository(self._session)
        self.source_collections = SqlAlchemySourceCollectionRepository(self._session)
        self.collection_attempts = SqlAlchemyCollectionAttemptRepository(self._session)
        self.collection_policy_snapshots = SqlAlchemyCollectionPolicySnapshotRepository(
            self._session
        )
        self.derived_artifacts = SqlAlchemyDerivedArtifactRepository(self._session)
        self.claims = SqlAlchemyClaimRepository(self._session)
        self.indicators = SqlAlchemyIndicatorRepository(self._session)
        self.rejected_model_proposals = SqlAlchemyRejectedModelProposalRepository(self._session)
        self.subject_production_runs = SqlAlchemySubjectProductionRunRepository(self._session)
        self.production_input_snapshots = SqlAlchemyProductionInputSnapshotRepository(self._session)
        self.production_artifacts = SqlAlchemyProductionArtifactRepository(self._session)
        self.source_extractions = SqlAlchemySourceExtractionRepository(self._session)
        self.production_reuse_invalidations = SqlAlchemyProductionReuseInvalidationRepository(
            self._session
        )
        self.analyst_investigations = SqlAlchemyAnalystInvestigationRepository(self._session)
        self.analyst_decisions = SqlAlchemyAnalystDecisionRepository(self._session)
        self.analyst_input_packs = SqlAlchemyAnalystInputPackRepository(self._session)
        self.edition_production_batches = SqlAlchemyEditionProductionBatchRepository(self._session)
        self.edition_production_batch_items = SqlAlchemyEditionProductionBatchItemRepository(
            self._session
        )
        self.batch_status_read_model = SqlAlchemyBatchStatusReadRepository(self._session)
        self.edition_review_read_model = SqlAlchemyEditionReviewReadRepository(self._session)
        self.publication_review_decisions = SqlAlchemyPublicationReviewDecisionRepository(
            self._session
        )
        self.publication_manifests = SqlAlchemyPublicationManifestRepository(self._session)
        self.publication_manifest_entries = SqlAlchemyPublicationManifestEntryRepository(
            self._session
        )
        self.publication_manifest_exclusions = SqlAlchemyPublicationManifestExclusionRepository(
            self._session
        )
        self.edition_releases = SqlAlchemyEditionReleaseRepository(self._session)
        self._committed = False
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        session = self._require_session()
        try:
            if exc_type is not None or not self._committed:
                await session.rollback()
        finally:
            await session.close()
            self._session = None

    async def commit(self) -> None:
        await self._require_session().commit()
        self._committed = True

    async def rollback(self) -> None:
        await self._require_session().rollback()
        self._committed = False

    def _require_session(self) -> AsyncSession:
        if self._session is None:
            raise RuntimeError("Unit of Work must be entered before use")
        return self._session
