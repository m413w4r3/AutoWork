from types import TracebackType

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cti_app.application.persistence import (
    BlobRepository,
    DiscoveryBatchRepository,
    EditionAuditRepository,
    EditionRepository,
    EditorialGroupRepository,
    HumanDecisionRepository,
    JobEventRepository,
    JobRepository,
    ModelRunRepository,
    ProvenanceRepository,
    SampleRepository,
    SourceDocumentRepository,
    SubjectRepository,
)
from cti_app.infrastructure.database.repositories import (
    SqlAlchemyBlobRepository,
    SqlAlchemyDiscoveryBatchRepository,
    SqlAlchemyEditionAuditRepository,
    SqlAlchemyEditionRepository,
    SqlAlchemyEditorialGroupRepository,
    SqlAlchemyHumanDecisionRepository,
    SqlAlchemyJobEventRepository,
    SqlAlchemyJobRepository,
    SqlAlchemyModelRunRepository,
    SqlAlchemyProvenanceRepository,
    SqlAlchemySampleRepository,
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
    discovery_batches: DiscoveryBatchRepository
    editorial_groups: EditorialGroupRepository
    human_decisions: HumanDecisionRepository

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
        self.discovery_batches = SqlAlchemyDiscoveryBatchRepository(self._session)
        self.editorial_groups = SqlAlchemyEditorialGroupRepository(self._session)
        self.human_decisions = SqlAlchemyHumanDecisionRepository(self._session)
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
