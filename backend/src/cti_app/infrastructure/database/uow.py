from types import TracebackType

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cti_app.application.persistence import (
    BlobRepository,
    BriefDraftRepository,
    BriefEvidencePackRepository,
    ClaimRepository,
    CollectionAttemptRepository,
    CollectionPolicySnapshotRepository,
    DerivedArtifactRepository,
    DiscoveryBatchRepository,
    EditionAuditRepository,
    EditionRepository,
    EditorialGroupRepository,
    HumanDecisionRepository,
    IndicatorRepository,
    JobEventRepository,
    JobRepository,
    ModelConversationRepository,
    ModelConversationTurnRepository,
    ModelOutputRejectionRepository,
    ModelRunRepository,
    ProvenanceRepository,
    RejectedModelProposalRepository,
    SampleRepository,
    SourceCollectionRepository,
    SourceDocumentRepository,
    SubjectRepository,
)
from cti_app.infrastructure.database.repositories import (
    SqlAlchemyBlobRepository,
    SqlAlchemyBriefDraftRepository,
    SqlAlchemyBriefEvidencePackRepository,
    SqlAlchemyClaimRepository,
    SqlAlchemyCollectionAttemptRepository,
    SqlAlchemyCollectionPolicySnapshotRepository,
    SqlAlchemyDerivedArtifactRepository,
    SqlAlchemyDiscoveryBatchRepository,
    SqlAlchemyEditionAuditRepository,
    SqlAlchemyEditionRepository,
    SqlAlchemyEditorialGroupRepository,
    SqlAlchemyHumanDecisionRepository,
    SqlAlchemyIndicatorRepository,
    SqlAlchemyJobEventRepository,
    SqlAlchemyJobRepository,
    SqlAlchemyModelConversationRepository,
    SqlAlchemyModelConversationTurnRepository,
    SqlAlchemyModelOutputRejectionRepository,
    SqlAlchemyModelRunRepository,
    SqlAlchemyProvenanceRepository,
    SqlAlchemyRejectedModelProposalRepository,
    SqlAlchemySampleRepository,
    SqlAlchemySourceCollectionRepository,
    SqlAlchemySourceDocumentRepository,
    SqlAlchemySubjectRepository,
)


class SqlAlchemyUnitOfWork:
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

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory
        self._session: AsyncSession | None = None
        self._committed = False

    async def __aenter__(self) -> "SqlAlchemyUnitOfWork":
        self._session = self._session_factory()
        self.blobs = SqlAlchemyBlobRepository(self._session)
        self.subjects = SqlAlchemySubjectRepository(self._session)
        self.source_documents = SqlAlchemySourceDocumentRepository(self._session)
        self.samples = SqlAlchemySampleRepository(self._session)
        self.provenance = SqlAlchemyProvenanceRepository(self._session)
        self.jobs = SqlAlchemyJobRepository(self._session)
        self.job_events = SqlAlchemyJobEventRepository(self._session)
        self.editions = SqlAlchemyEditionRepository(self._session)
        self.edition_audit = SqlAlchemyEditionAuditRepository(self._session)
        self.model_runs = SqlAlchemyModelRunRepository(self._session)
        self.model_output_rejections = SqlAlchemyModelOutputRejectionRepository(self._session)
        self.model_conversations = SqlAlchemyModelConversationRepository(self._session)
        self.model_conversation_turns = SqlAlchemyModelConversationTurnRepository(self._session)
        self.discovery_batches = SqlAlchemyDiscoveryBatchRepository(self._session)
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
        self.brief_evidence_packs = SqlAlchemyBriefEvidencePackRepository(self._session)
        self.brief_drafts = SqlAlchemyBriefDraftRepository(self._session)
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
