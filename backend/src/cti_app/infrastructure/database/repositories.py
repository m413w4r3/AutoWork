from collections.abc import Sequence
from datetime import UTC, date, datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy import delete, func, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from cti_app.domain.blobs import BlobDescriptor, BlobRecord
from cti_app.domain.briefs import (
    BriefBlock,
    BriefDraft,
    BriefDraftStatus,
    BriefEvidencePack,
    BriefSentence,
)
from cti_app.domain.classification import TLP
from cti_app.domain.collection import (
    AttemptOutcome,
    Claim,
    ClaimKind,
    CollectionAttempt,
    CollectionPolicySnapshot,
    CollectionState,
    DerivedArtifact,
    Indicator,
    IndicatorKind,
    RejectedModelProposal,
    SourceCollection,
    SourceSpan,
)
from cti_app.domain.discovery import (
    CandidateTopic,
    DiscoveryBatch,
    DiscoveryBatchStatus,
    DiscoveryIocStatus,
    DiscoveryIocType,
    DiscoverySourceMode,
    IncompleteSourceCandidate,
    IocPresence,
    PeriodRelation,
    ProvisionalDiscoveryIoc,
    ProvisionalIocPublicationRelation,
    SourceCandidate,
    SourceRelationshipStatus,
    SourceRole,
    SourceVerificationStatus,
)
from cti_app.domain.editions import Edition, EditionAuditEvent, EditionStatus
from cti_app.domain.editorial import (
    CandidateReference,
    EditorialGroup,
    EditorialGroupStatus,
    EditorialScore,
    EditorialType,
    GroupingConfidence,
    GroupingOutcome,
    HumanDecision,
    HumanDecisionType,
)
from cti_app.domain.entities import ProvenanceEvent, Sample, SourceDocument, Subject
from cti_app.domain.errors import EntityNotFoundError
from cti_app.domain.jobs import Job, JobEvent, JobOperationalMetrics, JobStatus
from cti_app.domain.model_conversations import (
    ConversationPurpose,
    ConversationStatus,
    ConversationTransport,
    ConversationTurnStatus,
    ModelConversation,
    ModelConversationTurn,
)
from cti_app.domain.model_runs import (
    ModelOutputRejection,
    ModelProvider,
    ModelRole,
    ModelRun,
    ModelRunStatus,
    ModelUsage,
)
from cti_app.domain.production import (
    EditionProductionBatch,
    EditionProductionBatchItem,
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionArtifactStatus,
    SubjectProductionRun,
)
from cti_app.infrastructure.database.models import (
    BlobRow,
    BriefDraftRow,
    BriefEvidencePackRow,
    ClaimRow,
    CollectionAttemptRow,
    CollectionPolicySnapshotRow,
    DerivedArtifactRow,
    DiscoveryBatchRow,
    EditionAuditEventRow,
    EditionRow,
    EditorialGroupRow,
    HumanDecisionRow,
    IndicatorRow,
    JobEventRow,
    JobRow,
    ModelConversationRow,
    ModelConversationTurnRow,
    ModelOutputRejectionRow,
    ModelRunRow,
    ProvenanceEventRow,
    RejectedModelProposalRow,
    SampleRow,
    SourceCollectionRow,
    SourceDocumentRow,
    SubjectRow,
    EditionProductionBatchItemRow,
    EditionProductionBatchRow,
    ProductionArtifactRow,
    SubjectProductionRunRow,
)


class SqlAlchemyBlobRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, blob: BlobRecord) -> None:
        descriptor = blob.descriptor
        self._session.add(
            BlobRow(
                id=blob.id,
                sha256=descriptor.sha256,
                size=descriptor.size,
                mime_type=descriptor.mime_type,
                logical_bucket=descriptor.logical_bucket,
                object_key=descriptor.object_key,
                created_at=blob.created_at,
            )
        )
        await self._session.flush()

    async def get(self, blob_id: UUID) -> BlobRecord | None:
        row = await self._session.get(BlobRow, blob_id)
        return _blob_from_row(row) if row else None

    async def get_by_address(self, logical_bucket: str, sha256: str) -> BlobRecord | None:
        row = await self._session.scalar(
            select(BlobRow).where(
                BlobRow.logical_bucket == logical_bucket,
                BlobRow.sha256 == sha256,
            )
        )
        return _blob_from_row(row) if row else None

    async def count_references(self, blob_id: UUID) -> int:
        document_count = await self._session.scalar(
            select(func.count())
            .select_from(SourceDocumentRow)
            .where(SourceDocumentRow.blob_id == blob_id)
        )
        decoded_document_count = await self._session.scalar(
            select(func.count())
            .select_from(SourceDocumentRow)
            .where(SourceDocumentRow.decoded_blob_id == blob_id)
        )
        sample_count = await self._session.scalar(
            select(func.count()).select_from(SampleRow).where(SampleRow.blob_id == blob_id)
        )
        model_output_count = await self._session.scalar(
            select(func.count())
            .select_from(ModelRunRow)
            .where(ModelRunRow.output_references.contains([f"blob://{blob_id}"]))
        )
        artifact_count = await self._session.scalar(
            select(func.count())
            .select_from(DerivedArtifactRow)
            .where(DerivedArtifactRow.text_blob_id == blob_id)
        )
        decoded_source_count = await self._session.scalar(
            select(func.count())
            .select_from(SourceCollectionRow)
            .where(SourceCollectionRow.decoded_blob_id == blob_id)
        )
        brief_pack_count = await self._session.scalar(
            select(func.count())
            .select_from(BriefEvidencePackRow)
            .where(BriefEvidencePackRow.blob_id == blob_id)
        )
        conversation_input_count = await self._session.scalar(
            select(func.count())
            .select_from(ModelConversationTurnRow)
            .where(ModelConversationTurnRow.input_blob_reference == f"blob://{blob_id}")
        )
        conversation_output_count = await self._session.scalar(
            select(func.count())
            .select_from(ModelConversationTurnRow)
            .where(ModelConversationTurnRow.output_blob_reference == f"blob://{blob_id}")
        )
        return (
            int(document_count or 0)
            + int(decoded_document_count or 0)
            + int(sample_count or 0)
            + int(model_output_count or 0)
            + int(artifact_count or 0)
            + int(decoded_source_count or 0)
            + int(brief_pack_count or 0)
            + int(conversation_input_count or 0)
            + int(conversation_output_count or 0)
        )

    async def delete(self, blob_id: UUID) -> None:
        row = await self._session.get(BlobRow, blob_id)
        if row is not None:
            await self._session.delete(row)


class SqlAlchemySubjectRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, subject: Subject) -> None:
        self._session.add(
            SubjectRow(
                id=subject.id,
                external_id=subject.external_id,
                slug=subject.slug,
                tlp=subject.tlp.value,
                created_at=subject.created_at,
            )
        )
        await self._session.flush()

    async def get(self, subject_id: UUID) -> Subject | None:
        row = await self._session.get(SubjectRow, subject_id)
        if row is None:
            return None
        return Subject(
            id=row.id,
            external_id=row.external_id,
            slug=row.slug,
            tlp=TLP(row.tlp),
            created_at=row.created_at,
        )


class SqlAlchemySourceDocumentRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, document: SourceDocument) -> None:
        self._session.add(_source_document_to_row(document))
        await self._session.flush()

    async def get(self, document_id: UUID) -> SourceDocument | None:
        row = await self._session.get(SourceDocumentRow, document_id)
        return _source_document_from_row(row) if row else None

    async def save(self, document: SourceDocument) -> None:
        row = await self._session.get(SourceDocumentRow, document.id)
        if row is None:
            raise EntityNotFoundError(f"Source document {document.id} does not exist")
        values = _source_document_to_row(document)
        for column in (
            "original_name",
            "origin",
            "logical_filename",
            "source_collection_id",
            "source_candidate_id",
            "decoded_blob_id",
            "title",
            "publisher",
            "published_at",
            "final_url",
            "declared_mime_type",
            "detected_mime_type",
            "encoded_sha256",
            "decoded_sha256",
            "encoded_size",
            "decoded_size",
        ):
            setattr(row, column, getattr(values, column))
        await self._session.flush()

    async def list_for_subject(self, subject_id: UUID) -> Sequence[SourceDocument]:
        rows = await self._session.scalars(
            select(SourceDocumentRow)
            .where(SourceDocumentRow.subject_id == subject_id)
            .order_by(SourceDocumentRow.created_at, SourceDocumentRow.id)
        )
        return [_source_document_from_row(row) for row in rows]


class SqlAlchemySampleRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, sample: Sample) -> None:
        self._session.add(_sample_to_row(sample))
        await self._session.flush()

    async def get(self, sample_id: UUID) -> Sample | None:
        row = await self._session.get(SampleRow, sample_id)
        return _sample_from_row(row) if row else None

    async def list_for_subject(self, subject_id: UUID) -> Sequence[Sample]:
        rows = await self._session.scalars(
            select(SampleRow)
            .where(SampleRow.subject_id == subject_id)
            .order_by(SampleRow.created_at, SampleRow.id)
        )
        return [_sample_from_row(row) for row in rows]


class SqlAlchemyProvenanceRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def append(self, event: ProvenanceEvent) -> None:
        self._session.add(
            ProvenanceEventRow(
                id=event.id,
                subject_id=event.subject_id,
                aggregate_type=event.aggregate_type,
                aggregate_id=event.aggregate_id,
                event_type=event.event_type,
                payload=event.payload,
                tlp=event.tlp.value,
                actor_id=event.actor_id,
                occurred_at=event.occurred_at,
            )
        )
        await self._session.flush()

    async def list_for_aggregate(
        self, aggregate_type: str, aggregate_id: UUID
    ) -> Sequence[ProvenanceEvent]:
        rows = await self._session.scalars(
            select(ProvenanceEventRow)
            .where(
                ProvenanceEventRow.aggregate_type == aggregate_type,
                ProvenanceEventRow.aggregate_id == aggregate_id,
            )
            .order_by(ProvenanceEventRow.occurred_at, ProvenanceEventRow.id)
        )
        return [_provenance_from_row(row) for row in rows]


class SqlAlchemyJobRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add_if_absent(self, job: Job) -> bool:
        statement = (
            insert(JobRow)
            .values(**_job_values(job))
            .on_conflict_do_nothing(index_elements=[JobRow.idempotency_key])
            .returning(JobRow.id)
        )
        inserted_id = await self._session.scalar(statement)
        return inserted_id is not None

    async def get(self, job_id: UUID) -> Job | None:
        row = await self._session.get(JobRow, job_id)
        return _job_from_row(row) if row else None

    async def get_for_update(self, job_id: UUID) -> Job | None:
        row = await self._session.scalar(
            select(JobRow).where(JobRow.id == job_id).with_for_update()
        )
        return _job_from_row(row) if row else None

    async def get_by_idempotency_key(self, idempotency_key: str) -> Job | None:
        row = await self._session.scalar(
            select(JobRow).where(JobRow.idempotency_key == idempotency_key)
        )
        return _job_from_row(row) if row else None

    async def save(self, job: Job) -> None:
        row = await self._session.get(JobRow, job.id)
        if row is None:
            raise LookupError(f"Job {job.id} does not exist")
        for field_name, value in _job_values(job).items():
            setattr(row, field_name, value)
        await self._session.flush()

    async def list_abandoned(self, heartbeat_before: datetime) -> Sequence[Job]:
        rows = await self._session.scalars(
            select(JobRow)
            .where(
                JobRow.status == JobStatus.RUNNING.value,
                JobRow.heartbeat_at < heartbeat_before,
            )
            .order_by(JobRow.heartbeat_at, JobRow.id)
            .with_for_update(skip_locked=True)
        )
        return [_job_from_row(row) for row in rows]

    async def operational_metrics(self) -> JobOperationalMetrics:
        status_rows = await self._session.execute(
            select(JobRow.status, func.count()).group_by(JobRow.status)
        )
        counts = {status: 0 for status in JobStatus}
        counts.update({JobStatus(status): int(count) for status, count in status_rows})
        total = sum(counts.values())
        retry_waiting = int(
            await self._session.scalar(
                select(func.count())
                .select_from(JobRow)
                .where(
                    JobRow.status == JobStatus.QUEUED.value,
                    JobRow.next_retry_at.is_not(None),
                )
            )
            or 0
        )
        average_duration = await self._session.scalar(
            select(func.avg(func.extract("epoch", JobRow.finished_at - JobRow.started_at))).where(
                JobRow.started_at.is_not(None), JobRow.finished_at.is_not(None)
            )
        )
        failed = counts.get(JobStatus.FAILED, 0)
        terminal = sum(
            counts.get(status, 0)
            for status in (JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED)
        )
        return JobOperationalMetrics(
            total=total,
            counts_by_status=counts,
            retry_waiting=retry_waiting,
            average_duration_seconds=float(average_duration) if average_duration else None,
            failure_rate=failed / terminal if terminal else 0.0,
        )


class SqlAlchemyJobEventRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def append(self, event: JobEvent) -> None:
        self._session.add(
            JobEventRow(
                id=event.id,
                job_id=event.job_id,
                event_type=event.event_type,
                from_status=event.from_status.value if event.from_status else None,
                to_status=event.to_status.value,
                actor_id=event.actor_id,
                correlation_id=event.correlation_id,
                payload=event.payload,
                occurred_at=event.occurred_at,
            )
        )
        await self._session.flush()

    async def list_for_job(self, job_id: UUID) -> Sequence[JobEvent]:
        rows = await self._session.scalars(
            select(JobEventRow)
            .where(JobEventRow.job_id == job_id)
            .order_by(JobEventRow.occurred_at, JobEventRow.id)
        )
        return [_job_event_from_row(row) for row in rows]


class SqlAlchemyEditionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add_if_absent(self, edition: Edition) -> bool:
        statement = (
            insert(EditionRow)
            .values(**_edition_values(edition))
            .on_conflict_do_nothing(
                index_elements=[
                    EditionRow.country_code,
                    EditionRow.period_start,
                    EditionRow.period_end,
                ]
            )
            .returning(EditionRow.id)
        )
        return await self._session.scalar(statement) is not None

    async def get(self, edition_id: UUID) -> Edition | None:
        row = await self._session.get(EditionRow, edition_id)
        return _edition_from_row(row) if row else None

    async def get_by_logical_key(
        self, country_code: str, period_start: date, period_end: date
    ) -> Edition | None:
        row = await self._session.scalar(
            select(EditionRow).where(
                EditionRow.country_code == country_code,
                EditionRow.period_start == period_start,
                EditionRow.period_end == period_end,
            )
        )
        return _edition_from_row(row) if row else None

    async def update(self, edition: Edition, expected_version: int) -> bool:
        result = await self._session.execute(
            update(EditionRow)
            .where(EditionRow.id == edition.id, EditionRow.version == expected_version)
            .values(**_edition_values(edition))
            .returning(EditionRow.id)
        )
        return result.scalar_one_or_none() is not None

    async def delete(self, edition_id: UUID, expected_version: int) -> bool:
        """Delete the edition aggregate in dependency order in one transaction."""
        group_ids = list(
            await self._session.scalars(
                select(EditorialGroupRow.id).where(EditorialGroupRow.edition_id == edition_id)
            )
        )
        batch_rows = list(
            (
                await self._session.execute(
                    select(
                        DiscoveryBatchRow.id,
                        DiscoveryBatchRow.discovery_model_run_id,
                        DiscoveryBatchRow.structuring_model_run_id,
                    ).where(DiscoveryBatchRow.edition_id == edition_id)
                )
            ).all()
        )
        conversation_ids = list(
            await self._session.scalars(
                select(ModelConversationRow.id).where(ModelConversationRow.edition_id == edition_id)
            )
        )
        turn_model_run_ids = list(
            await self._session.scalars(
                select(ModelConversationTurnRow.model_run_id).where(
                    ModelConversationTurnRow.conversation_id.in_(conversation_ids)
                )
            )
        )
        collection_rows = list(
            (
                await self._session.execute(
                    select(SourceCollectionRow.id, SourceCollectionRow.fetch_job_id).where(
                        SourceCollectionRow.edition_id == edition_id
                    )
                )
            ).all()
        )
        collection_ids = [row.id for row in collection_rows]
        collection_job_ids = [row.fetch_job_id for row in collection_rows if row.fetch_job_id]
        claim_model_run_ids = list(
            await self._session.scalars(
                select(ClaimRow.model_run_id).where(
                    ClaimRow.edition_id == edition_id, ClaimRow.model_run_id.is_not(None)
                )
            )
        )
        draft_model_run_ids = list(
            await self._session.scalars(
                select(BriefDraftRow.model_run_id).where(BriefDraftRow.edition_id == edition_id)
            )
        )
        model_run_ids = {
            *turn_model_run_ids,
            *claim_model_run_ids,
            *draft_model_run_ids,
            *(row.discovery_model_run_id for row in batch_rows),
            *(row.structuring_model_run_id for row in batch_rows),
        }

        # Break the two explicit parent/head cycles before deleting their children.
        if conversation_ids:
            await self._session.execute(
                update(ModelConversationRow)
                .where(ModelConversationRow.id.in_(conversation_ids))
                .values(head_turn_id=None)
            )
            await self._session.execute(
                delete(ModelConversationTurnRow).where(
                    ModelConversationTurnRow.conversation_id.in_(conversation_ids)
                )
            )
        if collection_ids:
            await self._session.execute(
                update(SourceCollectionRow)
                .where(SourceCollectionRow.id.in_(collection_ids))
                .values(latest_attempt_id=None)
            )
            await self._session.execute(
                delete(CollectionAttemptRow).where(
                    CollectionAttemptRow.collection_id.in_(collection_ids)
                )
            )

        await self._session.execute(
            delete(BriefDraftRow).where(BriefDraftRow.edition_id == edition_id)
        )
        await self._session.execute(
            delete(BriefEvidencePackRow).where(BriefEvidencePackRow.edition_id == edition_id)
        )
        await self._session.execute(delete(ClaimRow).where(ClaimRow.edition_id == edition_id))
        await self._session.execute(
            delete(IndicatorRow).where(IndicatorRow.edition_id == edition_id)
        )
        await self._session.execute(
            delete(SourceCollectionRow).where(SourceCollectionRow.edition_id == edition_id)
        )
        await self._session.execute(
            delete(HumanDecisionRow).where(HumanDecisionRow.edition_id == edition_id)
        )
        if group_ids:
            await self._session.execute(
                update(EditorialGroupRow)
                .where(EditorialGroupRow.potential_historical_group_id.in_(group_ids))
                .values(potential_historical_group_id=None)
            )
        await self._session.execute(
            delete(EditorialGroupRow).where(EditorialGroupRow.edition_id == edition_id)
        )
        await self._session.execute(
            delete(DiscoveryBatchRow).where(DiscoveryBatchRow.edition_id == edition_id)
        )
        await self._session.execute(
            delete(ModelConversationRow).where(ModelConversationRow.edition_id == edition_id)
        )

        job_ids = set(collection_job_ids)
        job_ids.update(
            await self._session.scalars(
                select(JobRow.id).where(
                    JobRow.aggregate_type == "edition", JobRow.aggregate_id == edition_id
                )
            )
        )
        if job_ids:
            await self._session.execute(delete(JobEventRow).where(JobEventRow.job_id.in_(job_ids)))
            await self._session.execute(delete(JobRow).where(JobRow.id.in_(job_ids)))

        await self._session.execute(
            delete(ProvenanceEventRow).where(
                ProvenanceEventRow.aggregate_type == "edition",
                ProvenanceEventRow.aggregate_id == edition_id,
            )
        )

        # Model runs have no edition column. Remove only runs reached from this
        # aggregate which no surviving record still references.
        if model_run_ids:
            referenced_run_ids = set(
                await self._session.scalars(
                    select(ModelConversationTurnRow.model_run_id).where(
                        ModelConversationTurnRow.model_run_id.in_(model_run_ids)
                    )
                )
            )
            referenced_run_ids.update(
                await self._session.scalars(
                    select(DiscoveryBatchRow.discovery_model_run_id).where(
                        DiscoveryBatchRow.discovery_model_run_id.in_(model_run_ids)
                    )
                )
            )
            referenced_run_ids.update(
                await self._session.scalars(
                    select(DiscoveryBatchRow.structuring_model_run_id).where(
                        DiscoveryBatchRow.structuring_model_run_id.in_(model_run_ids)
                    )
                )
            )
            for model in (ClaimRow, RejectedModelProposalRow, BriefDraftRow):
                referenced_run_ids.update(
                    await self._session.scalars(
                        select(model.model_run_id).where(model.model_run_id.in_(model_run_ids))
                    )
                )
            deletable_run_ids = model_run_ids - referenced_run_ids
            if deletable_run_ids:
                await self._session.execute(
                    delete(ModelOutputRejectionRow).where(
                        ModelOutputRejectionRow.model_run_id.in_(deletable_run_ids)
                    )
                )
                await self._session.execute(
                    delete(ModelRunRow).where(ModelRunRow.id.in_(deletable_run_ids))
                )

        result = await self._session.execute(
            delete(EditionRow)
            .where(EditionRow.id == edition_id, EditionRow.version == expected_version)
            .returning(EditionRow.id)
        )
        return result.scalar_one_or_none() is not None

    async def list(
        self,
        *,
        offset: int,
        limit: int,
        country_code: str | None,
        period_start: date | None,
        period_end: date | None,
        status: EditionStatus | None,
    ) -> tuple[Sequence[Edition], int]:
        filters = []
        if country_code:
            filters.append(EditionRow.country_code == country_code)
        if period_start:
            filters.append(EditionRow.period_start >= period_start)
        if period_end:
            filters.append(EditionRow.period_end <= period_end)
        if status:
            filters.append(EditionRow.status == status.value)
        total = int(
            await self._session.scalar(select(func.count()).select_from(EditionRow).where(*filters))
            or 0
        )
        rows = await self._session.scalars(
            select(EditionRow)
            .where(*filters)
            .order_by(EditionRow.period_start.desc(), EditionRow.country_code, EditionRow.id)
            .offset(offset)
            .limit(limit)
        )
        return ([_edition_from_row(row) for row in rows], total)


class SqlAlchemyEditionAuditRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def append(self, event: EditionAuditEvent) -> None:
        self._session.add(
            EditionAuditEventRow(
                id=event.id,
                edition_id=event.edition_id,
                actor_id=event.actor_id,
                action=event.action,
                before=event.before,
                after=event.after,
                correlation_id=event.correlation_id,
                occurred_at=event.occurred_at,
            )
        )
        await self._session.flush()

    async def list_for_edition(self, edition_id: UUID) -> Sequence[EditionAuditEvent]:
        rows = await self._session.scalars(
            select(EditionAuditEventRow)
            .where(EditionAuditEventRow.edition_id == edition_id)
            .order_by(EditionAuditEventRow.occurred_at, EditionAuditEventRow.id)
        )
        return [_edition_audit_from_row(row) for row in rows]

    async def delete_for_edition(self, edition_id: UUID) -> None:
        await self._session.execute(text("SET LOCAL cti.allow_destructive_edition_delete = 'on'"))
        await self._session.execute(
            delete(EditionAuditEventRow).where(EditionAuditEventRow.edition_id == edition_id)
        )


class SqlAlchemyModelRunRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, run: ModelRun) -> None:
        self._session.add(ModelRunRow(**_model_run_values(run)))
        await self._session.flush()

    async def get(self, run_id: UUID) -> ModelRun | None:
        row = await self._session.get(ModelRunRow, run_id)
        return _model_run_from_row(row) if row else None

    async def get_for_update(self, run_id: UUID) -> ModelRun | None:
        row = await self._session.scalar(
            select(ModelRunRow).where(ModelRunRow.id == run_id).with_for_update()
        )
        return _model_run_from_row(row) if row else None

    async def save(self, run: ModelRun) -> None:
        row = await self._session.get(ModelRunRow, run.id)
        if row is None:
            raise LookupError(f"Model run {run.id} does not exist")
        for field_name, value in _model_run_values(run).items():
            setattr(row, field_name, value)


class SqlAlchemyModelOutputRejectionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def append(self, rejection: ModelOutputRejection) -> None:
        self._session.add(
            ModelOutputRejectionRow(
                id=rejection.id,
                model_run_id=rejection.model_run_id,
                path=list(rejection.path),
                error_type=rejection.error_type,
                value_sha256=rejection.value_sha256,
                raw_output_reference=rejection.raw_output_reference,
                created_at=rejection.created_at,
            )
        )

    async def list_for_run(self, run_id: UUID) -> list[ModelOutputRejection]:
        rows = (
            await self._session.scalars(
                select(ModelOutputRejectionRow)
                .where(ModelOutputRejectionRow.model_run_id == run_id)
                .order_by(ModelOutputRejectionRow.created_at)
            )
        ).all()
        return [
            ModelOutputRejection(
                id=row.id,
                model_run_id=row.model_run_id,
                path=tuple(row.path),
                error_type=row.error_type,
                value_sha256=row.value_sha256,
                raw_output_reference=row.raw_output_reference,
                created_at=row.created_at,
            )
            for row in rows
        ]
        await self._session.flush()


class SqlAlchemyModelConversationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, conversation: ModelConversation) -> None:
        self._session.add(ModelConversationRow(**_model_conversation_values(conversation)))
        await self._session.flush()

    async def get(self, conversation_id: UUID) -> ModelConversation | None:
        row = await self._session.get(ModelConversationRow, conversation_id)
        return _model_conversation_from_row(row) if row else None

    async def get_for_update(self, conversation_id: UUID) -> ModelConversation | None:
        row = await self._session.scalar(
            select(ModelConversationRow)
            .where(ModelConversationRow.id == conversation_id)
            .with_for_update()
        )
        return _model_conversation_from_row(row) if row else None

    async def save(self, conversation: ModelConversation) -> None:
        values = _model_conversation_values(conversation)
        values.pop("id")
        result = await self._session.execute(
            update(ModelConversationRow)
            .where(
                ModelConversationRow.id == conversation.id,
                ModelConversationRow.version == conversation.version - 1,
            )
            .values(**values)
        )
        if getattr(result, "rowcount", 0) != 1:
            raise LookupError(f"Conversation {conversation.id} absente ou version obsolète")

    async def list(
        self,
        *,
        edition_id: UUID | None,
        subject_id: UUID | None,
        purpose: ConversationPurpose | None,
        status: ConversationStatus | None,
        provider: ModelProvider | None,
    ) -> Sequence[ModelConversation]:
        filters = []
        if edition_id is not None:
            filters.append(ModelConversationRow.edition_id == edition_id)
        if subject_id is not None:
            filters.append(ModelConversationRow.subject_id == subject_id)
        if purpose is not None:
            filters.append(ModelConversationRow.purpose == purpose.value)
        if status is not None:
            filters.append(ModelConversationRow.status == status.value)
        if provider is not None:
            filters.append(ModelConversationRow.provider == provider.value)
        rows = await self._session.scalars(
            select(ModelConversationRow)
            .where(*filters)
            .order_by(ModelConversationRow.updated_at.desc(), ModelConversationRow.id)
        )
        return [_model_conversation_from_row(row) for row in rows]


class SqlAlchemyModelConversationTurnRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, turn: ModelConversationTurn) -> None:
        self._session.add(ModelConversationTurnRow(**_model_conversation_turn_values(turn)))
        await self._session.flush()

    async def get(self, turn_id: UUID) -> ModelConversationTurn | None:
        row = await self._session.get(ModelConversationTurnRow, turn_id)
        return _model_conversation_turn_from_row(row) if row else None

    async def get_by_idempotency_key(self, key: str) -> ModelConversationTurn | None:
        row = await self._session.scalar(
            select(ModelConversationTurnRow).where(ModelConversationTurnRow.idempotency_key == key)
        )
        return _model_conversation_turn_from_row(row) if row else None

    async def list_for_conversation(self, conversation_id: UUID) -> Sequence[ModelConversationTurn]:
        rows = await self._session.scalars(
            select(ModelConversationTurnRow)
            .where(ModelConversationTurnRow.conversation_id == conversation_id)
            .order_by(ModelConversationTurnRow.sequence)
        )
        return [_model_conversation_turn_from_row(row) for row in rows]

    async def save(self, turn: ModelConversationTurn) -> None:
        row = await self._session.get(ModelConversationTurnRow, turn.id)
        if row is None:
            raise LookupError(turn.id)
        for name, value in _model_conversation_turn_values(turn).items():
            setattr(row, name, value)
        await self._session.flush()


class SqlAlchemyDiscoveryBatchRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add_if_absent(self, batch: DiscoveryBatch) -> bool:
        statement = (
            insert(DiscoveryBatchRow)
            .values(**_discovery_batch_values(batch))
            .on_conflict_do_nothing(
                index_elements=[DiscoveryBatchRow.edition_id, DiscoveryBatchRow.request_hash]
            )
            .returning(DiscoveryBatchRow.id)
        )
        return await self._session.scalar(statement) is not None

    async def get(self, batch_id: UUID) -> DiscoveryBatch | None:
        row = await self._session.get(DiscoveryBatchRow, batch_id)
        return _discovery_batch_from_row(row) if row else None

    async def get_by_request_hash(
        self, edition_id: UUID, request_hash: str
    ) -> DiscoveryBatch | None:
        row = await self._session.scalar(
            select(DiscoveryBatchRow).where(
                DiscoveryBatchRow.edition_id == edition_id,
                DiscoveryBatchRow.request_hash == request_hash,
            )
        )
        return _discovery_batch_from_row(row) if row else None

    async def list_for_edition(self, edition_id: UUID) -> Sequence[DiscoveryBatch]:
        rows = await self._session.scalars(
            select(DiscoveryBatchRow)
            .where(DiscoveryBatchRow.edition_id == edition_id)
            .order_by(DiscoveryBatchRow.created_at, DiscoveryBatchRow.id)
        )
        return [_discovery_batch_from_row(row) for row in rows]

    async def save(self, batch: DiscoveryBatch) -> None:
        row = await self._session.get(DiscoveryBatchRow, batch.id)
        if row is None:
            raise LookupError(f"Discovery batch {batch.id} does not exist")
        batch.updated_at = datetime.now(UTC)
        for field_name, value in _discovery_batch_values(batch).items():
            setattr(row, field_name, value)
        await self._session.flush()


class SqlAlchemyEditorialGroupRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, group: EditorialGroup) -> None:
        self._session.add(EditorialGroupRow(**_editorial_group_values(group)))
        await self._session.flush()

    async def get(self, group_id: UUID) -> EditorialGroup | None:
        row = await self._session.get(EditorialGroupRow, group_id)
        return _editorial_group_from_row(row) if row else None

    async def get_for_update(self, group_id: UUID) -> EditorialGroup | None:
        row = await self._session.scalar(
            select(EditorialGroupRow).where(EditorialGroupRow.id == group_id).with_for_update()
        )
        return _editorial_group_from_row(row) if row else None

    async def list_for_edition(self, edition_id: UUID) -> Sequence[EditorialGroup]:
        rows = await self._session.scalars(
            select(EditorialGroupRow)
            .where(EditorialGroupRow.edition_id == edition_id)
            .order_by(EditorialGroupRow.created_at, EditorialGroupRow.id)
        )
        return [_editorial_group_from_row(row) for row in rows]

    async def list_historical(self, edition_id: UUID) -> Sequence[EditorialGroup]:
        edition = await self._session.get(EditionRow, edition_id)
        if edition is None:
            return []
        rows = await self._session.scalars(
            select(EditorialGroupRow)
            .join(EditionRow, EditionRow.id == EditorialGroupRow.edition_id)
            .where(
                EditionRow.country_code == edition.country_code,
                EditionRow.period_start < edition.period_start,
                EditorialGroupRow.status == EditorialGroupStatus.SELECTED.value,
            )
            .order_by(EditionRow.period_start.desc(), EditorialGroupRow.created_at.desc())
        )
        return [_editorial_group_from_row(row) for row in rows]

    async def get_by_subject(self, subject_id: UUID) -> EditorialGroup | None:
        row = await self._session.scalar(
            select(EditorialGroupRow).where(EditorialGroupRow.subject_id == subject_id)
        )
        return _editorial_group_from_row(row) if row else None

    async def save(self, group: EditorialGroup) -> None:
        row = await self._session.get(EditorialGroupRow, group.id)
        if row is None:
            raise LookupError(f"Editorial group {group.id} does not exist")
        for field_name, value in _editorial_group_values(group).items():
            setattr(row, field_name, value)
        await self._session.flush()


class SqlAlchemyHumanDecisionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def append(self, decision: HumanDecision) -> None:
        self._session.add(
            HumanDecisionRow(
                id=decision.id,
                edition_id=decision.edition_id,
                decision_type=decision.decision_type.value,
                group_ids=[str(item) for item in decision.group_ids],
                actor_id=decision.actor_id,
                correlation_id=decision.correlation_id,
                payload=decision.payload,
                occurred_at=decision.occurred_at,
            )
        )
        await self._session.flush()

    async def list_for_edition(self, edition_id: UUID) -> Sequence[HumanDecision]:
        rows = await self._session.scalars(
            select(HumanDecisionRow)
            .where(HumanDecisionRow.edition_id == edition_id)
            .order_by(HumanDecisionRow.occurred_at, HumanDecisionRow.id)
        )
        return [
            HumanDecision(
                id=row.id,
                edition_id=row.edition_id,
                decision_type=HumanDecisionType(row.decision_type),
                group_ids=tuple(UUID(item) for item in row.group_ids),
                actor_id=row.actor_id,
                correlation_id=row.correlation_id,
                payload=row.payload,
                occurred_at=row.occurred_at,
            )
            for row in rows
        ]


class SqlAlchemySourceCollectionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add_if_absent(self, collection: SourceCollection) -> bool:
        statement = (
            insert(SourceCollectionRow)
            .values(**_source_collection_values(collection))
            .on_conflict_do_nothing(
                index_elements=[
                    SourceCollectionRow.subject_id,
                    SourceCollectionRow.source_candidate_id,
                ]
            )
            .returning(SourceCollectionRow.id)
        )
        return await self._session.scalar(statement) is not None

    async def get(self, collection_id: UUID) -> SourceCollection | None:
        row = await self._session.get(SourceCollectionRow, collection_id)
        return _source_collection_from_row(row) if row else None

    async def get_for_update(self, collection_id: UUID) -> SourceCollection | None:
        row = await self._session.scalar(
            select(SourceCollectionRow)
            .where(SourceCollectionRow.id == collection_id)
            .with_for_update()
        )
        return _source_collection_from_row(row) if row else None

    async def get_by_candidate(
        self, subject_id: UUID, source_candidate_id: UUID
    ) -> SourceCollection | None:
        row = await self._session.scalar(
            select(SourceCollectionRow).where(
                SourceCollectionRow.subject_id == subject_id,
                SourceCollectionRow.source_candidate_id == source_candidate_id,
            )
        )
        return _source_collection_from_row(row) if row else None

    async def list_for_subject(self, subject_id: UUID) -> Sequence[SourceCollection]:
        rows = await self._session.scalars(
            select(SourceCollectionRow)
            .where(SourceCollectionRow.subject_id == subject_id)
            .order_by(SourceCollectionRow.created_at, SourceCollectionRow.id)
        )
        return [_source_collection_from_row(row) for row in rows]

    async def save(self, collection: SourceCollection) -> None:
        row = await self._session.get(SourceCollectionRow, collection.id)
        if row is None:
            raise LookupError(f"Source collection {collection.id} does not exist")
        for field_name, value in _source_collection_values(collection).items():
            setattr(row, field_name, value)
        await self._session.flush()


class SqlAlchemyCollectionAttemptRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def append(self, attempt: CollectionAttempt) -> None:
        self._session.add(CollectionAttemptRow(**_collection_attempt_values(attempt)))
        await self._session.flush()

    async def list_for_collection(self, collection_id: UUID) -> Sequence[CollectionAttempt]:
        rows = await self._session.scalars(
            select(CollectionAttemptRow)
            .where(CollectionAttemptRow.collection_id == collection_id)
            .order_by(CollectionAttemptRow.attempted_at, CollectionAttemptRow.id)
        )
        return [_collection_attempt_from_row(row) for row in rows]


class SqlAlchemyCollectionPolicySnapshotRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add_if_absent(self, snapshot: CollectionPolicySnapshot) -> bool:
        statement = (
            insert(CollectionPolicySnapshotRow)
            .values(**_policy_snapshot_values(snapshot))
            .on_conflict_do_nothing(index_elements=[CollectionPolicySnapshotRow.id])
            .returning(CollectionPolicySnapshotRow.id)
        )
        return await self._session.scalar(statement) is not None

    async def get(self, snapshot_id: str) -> CollectionPolicySnapshot | None:
        row = await self._session.get(CollectionPolicySnapshotRow, snapshot_id)
        return _policy_snapshot_from_row(row) if row else None


class SqlAlchemyDerivedArtifactRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def append(self, artifact: DerivedArtifact) -> None:
        self._session.add(DerivedArtifactRow(**_derived_artifact_values(artifact)))
        await self._session.flush()

    async def get(self, artifact_id: UUID) -> DerivedArtifact | None:
        row = await self._session.get(DerivedArtifactRow, artifact_id)
        return _derived_artifact_from_row(row) if row else None


class SqlAlchemyClaimRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def append_many(self, claims: Sequence[Claim]) -> None:
        self._session.add_all([ClaimRow(**_claim_values(claim)) for claim in claims])
        await self._session.flush()

    async def get(self, claim_id: UUID) -> Claim | None:
        row = await self._session.get(ClaimRow, claim_id)
        return _claim_from_row(row) if row else None

    async def list_for_subject(self, subject_id: UUID) -> Sequence[Claim]:
        rows = await self._session.scalars(
            select(ClaimRow)
            .where(ClaimRow.subject_id == subject_id)
            .order_by(ClaimRow.created_at, ClaimRow.id)
        )
        return [_claim_from_row(row) for row in rows]


class SqlAlchemyIndicatorRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def append_many(self, indicators: Sequence[Indicator]) -> None:
        self._session.add_all(
            [IndicatorRow(**_indicator_values(indicator)) for indicator in indicators]
        )
        await self._session.flush()

    async def get(self, indicator_id: UUID) -> Indicator | None:
        row = await self._session.get(IndicatorRow, indicator_id)
        return _indicator_from_row(row) if row else None

    async def list_for_subject(self, subject_id: UUID) -> Sequence[Indicator]:
        rows = await self._session.scalars(
            select(IndicatorRow)
            .where(IndicatorRow.subject_id == subject_id)
            .order_by(IndicatorRow.created_at, IndicatorRow.id)
        )
        return [_indicator_from_row(row) for row in rows]


class SqlAlchemyRejectedModelProposalRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def append_many(self, proposals: Sequence[RejectedModelProposal]) -> None:
        self._session.add_all(
            [RejectedModelProposalRow(**_rejected_proposal_values(item)) for item in proposals]
        )
        await self._session.flush()


class SqlAlchemyBriefEvidencePackRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def append(self, pack: BriefEvidencePack) -> None:
        self._session.add(BriefEvidencePackRow(**_brief_pack_values(pack)))
        await self._session.flush()

    async def get(self, pack_id: UUID) -> BriefEvidencePack | None:
        row = await self._session.get(BriefEvidencePackRow, pack_id)
        return _brief_pack_from_row(row) if row else None

    async def get_current(self, subject_id: UUID) -> BriefEvidencePack | None:
        row = await self._session.scalar(
            select(BriefEvidencePackRow)
            .where(BriefEvidencePackRow.subject_id == subject_id)
            .order_by(BriefEvidencePackRow.version.desc())
            .limit(1)
        )
        return _brief_pack_from_row(row) if row else None

    async def get_by_hash(self, subject_id: UUID, content_hash: str) -> BriefEvidencePack | None:
        row = await self._session.scalar(
            select(BriefEvidencePackRow).where(
                BriefEvidencePackRow.subject_id == subject_id,
                BriefEvidencePackRow.content_hash == content_hash,
            )
        )
        return _brief_pack_from_row(row) if row else None

    async def list_for_subject(self, subject_id: UUID) -> Sequence[BriefEvidencePack]:
        rows = await self._session.scalars(
            select(BriefEvidencePackRow)
            .where(BriefEvidencePackRow.subject_id == subject_id)
            .order_by(BriefEvidencePackRow.version)
        )
        return [_brief_pack_from_row(row) for row in rows]


class SqlAlchemyBriefDraftRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def append(self, draft: BriefDraft) -> None:
        self._session.add(BriefDraftRow(**_brief_draft_values(draft)))
        await self._session.flush()

    async def get(self, draft_id: UUID) -> BriefDraft | None:
        row = await self._session.get(BriefDraftRow, draft_id)
        return _brief_draft_from_row(row) if row else None

    async def get_current(self, subject_id: UUID) -> BriefDraft | None:
        row = await self._session.scalar(
            select(BriefDraftRow)
            .where(BriefDraftRow.subject_id == subject_id)
            .order_by(BriefDraftRow.version.desc())
            .limit(1)
        )
        return _brief_draft_from_row(row) if row else None

    async def list_for_subject(self, subject_id: UUID) -> Sequence[BriefDraft]:
        rows = await self._session.scalars(
            select(BriefDraftRow)
            .where(BriefDraftRow.subject_id == subject_id)
            .order_by(BriefDraftRow.version)
        )
        return [_brief_draft_from_row(row) for row in rows]


def _blob_from_row(row: BlobRow) -> BlobRecord:
    return BlobRecord(
        id=row.id,
        descriptor=BlobDescriptor(
            sha256=row.sha256,
            size=row.size,
            mime_type=row.mime_type,
            logical_bucket=row.logical_bucket,
        ),
        created_at=row.created_at,
    )


def _source_document_to_row(document: SourceDocument) -> SourceDocumentRow:
    return SourceDocumentRow(
        id=document.id,
        subject_id=document.subject_id,
        blob_id=document.blob_id,
        original_name=document.original_name,
        origin=document.origin,
        acquired_at=document.acquired_at,
        license_restriction=document.license_restriction,
        tlp=document.tlp.value,
        do_not_submit=document.do_not_submit,
        external_llm_allowed=document.external_llm_allowed,
        logical_filename=document.logical_filename,
        source_collection_id=document.source_collection_id,
        source_candidate_id=document.source_candidate_id,
        decoded_blob_id=document.decoded_blob_id,
        title=document.title,
        publisher=document.publisher,
        published_at=document.published_at,
        final_url=document.final_url,
        declared_mime_type=document.declared_mime_type,
        detected_mime_type=document.detected_mime_type,
        encoded_sha256=document.encoded_sha256,
        decoded_sha256=document.decoded_sha256,
        encoded_size=document.encoded_size,
        decoded_size=document.decoded_size,
        created_at=document.created_at,
    )


def _source_document_from_row(row: SourceDocumentRow) -> SourceDocument:
    return SourceDocument(
        id=row.id,
        subject_id=row.subject_id,
        blob_id=row.blob_id,
        original_name=row.original_name,
        origin=row.origin,
        acquired_at=row.acquired_at,
        license_restriction=row.license_restriction,
        tlp=TLP(row.tlp),
        do_not_submit=row.do_not_submit,
        external_llm_allowed=row.external_llm_allowed,
        logical_filename=row.logical_filename,
        source_collection_id=row.source_collection_id,
        source_candidate_id=row.source_candidate_id,
        decoded_blob_id=row.decoded_blob_id,
        title=row.title,
        publisher=row.publisher,
        published_at=row.published_at,
        final_url=row.final_url,
        declared_mime_type=row.declared_mime_type,
        detected_mime_type=row.detected_mime_type,
        encoded_sha256=row.encoded_sha256,
        decoded_sha256=row.decoded_sha256,
        encoded_size=row.encoded_size,
        decoded_size=row.decoded_size,
        created_at=row.created_at,
    )


def _sample_to_row(sample: Sample) -> SampleRow:
    return SampleRow(
        id=sample.id,
        subject_id=sample.subject_id,
        blob_id=sample.blob_id,
        original_name=sample.original_name,
        origin=sample.origin,
        acquired_at=sample.acquired_at,
        license_restriction=sample.license_restriction,
        tlp=sample.tlp.value,
        do_not_submit=sample.do_not_submit,
        external_llm_allowed=sample.external_llm_allowed,
        created_at=sample.created_at,
    )


def _sample_from_row(row: SampleRow) -> Sample:
    return Sample(
        id=row.id,
        subject_id=row.subject_id,
        blob_id=row.blob_id,
        original_name=row.original_name,
        origin=row.origin,
        acquired_at=row.acquired_at,
        license_restriction=row.license_restriction,
        tlp=TLP(row.tlp),
        do_not_submit=row.do_not_submit,
        external_llm_allowed=row.external_llm_allowed,
        created_at=row.created_at,
    )


def _provenance_from_row(row: ProvenanceEventRow) -> ProvenanceEvent:
    return ProvenanceEvent(
        id=row.id,
        subject_id=row.subject_id,
        aggregate_type=row.aggregate_type,
        aggregate_id=row.aggregate_id,
        event_type=row.event_type,
        payload=row.payload,
        tlp=TLP(row.tlp),
        actor_id=row.actor_id,
        occurred_at=row.occurred_at,
    )


def _job_values(job: Job) -> dict[str, object]:
    return {
        "id": job.id,
        "kind": job.kind,
        "aggregate_type": job.aggregate_type,
        "aggregate_id": job.aggregate_id,
        "status": job.status.value,
        "progress_current": job.progress_current,
        "progress_total": job.progress_total,
        "user_message": job.user_message,
        "idempotency_key": job.idempotency_key,
        "attempt": job.attempt,
        "max_attempts": job.max_attempts,
        "next_retry_at": job.next_retry_at,
        "started_at": job.started_at,
        "finished_at": job.finished_at,
        "heartbeat_at": job.heartbeat_at,
        "error_code": job.error_code,
        "error_message": job.error_message,
        "error_details": job.error_details,
        "correlation_id": job.correlation_id,
        "input_parameters": job.input_parameters,
        "output_reference": job.output_reference,
        "cancellation_requested_at": job.cancellation_requested_at,
        "created_at": job.created_at,
        "updated_at": job.updated_at,
    }


def _job_from_row(row: JobRow) -> Job:
    return Job(
        id=row.id,
        kind=row.kind,
        aggregate_type=row.aggregate_type,
        aggregate_id=row.aggregate_id,
        status=JobStatus(row.status),
        progress_current=row.progress_current,
        progress_total=row.progress_total,
        user_message=row.user_message,
        idempotency_key=row.idempotency_key,
        attempt=row.attempt,
        max_attempts=row.max_attempts,
        next_retry_at=row.next_retry_at,
        started_at=row.started_at,
        finished_at=row.finished_at,
        heartbeat_at=row.heartbeat_at,
        error_code=row.error_code,
        error_message=row.error_message,
        error_details=row.error_details,
        correlation_id=row.correlation_id,
        input_parameters=row.input_parameters,
        output_reference=row.output_reference,
        cancellation_requested_at=row.cancellation_requested_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _job_event_from_row(row: JobEventRow) -> JobEvent:
    return JobEvent(
        id=row.id,
        job_id=row.job_id,
        event_type=row.event_type,
        from_status=JobStatus(row.from_status) if row.from_status else None,
        to_status=JobStatus(row.to_status),
        actor_id=row.actor_id,
        correlation_id=row.correlation_id,
        payload=row.payload,
        occurred_at=row.occurred_at,
    )


def _edition_values(edition: Edition) -> dict[str, object]:
    return {
        "id": edition.id,
        "country": edition.country,
        "country_code": edition.country_code,
        "period_start": edition.period_start,
        "period_end": edition.period_end,
        "tlp": edition.tlp.value,
        "languages": list(edition.languages),
        "target_major_articles": edition.target_major_articles,
        "target_briefs": edition.target_briefs,
        "previous_edition_id": edition.previous_edition_id,
        "source_profile": edition.source_profile,
        "status": edition.status.value,
        "version": edition.version,
        "created_at": edition.created_at,
        "updated_at": edition.updated_at,
    }


def _edition_from_row(row: EditionRow) -> Edition:
    return Edition(
        id=row.id,
        country=row.country,
        country_code=row.country_code,
        period_start=row.period_start,
        period_end=row.period_end,
        tlp=TLP(row.tlp),
        languages=tuple(row.languages),
        target_major_articles=row.target_major_articles,
        target_briefs=row.target_briefs,
        previous_edition_id=row.previous_edition_id,
        source_profile=row.source_profile,
        status=EditionStatus(row.status),
        version=row.version,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _edition_audit_from_row(row: EditionAuditEventRow) -> EditionAuditEvent:
    return EditionAuditEvent(
        id=row.id,
        edition_id=row.edition_id,
        actor_id=row.actor_id,
        action=row.action,
        before=row.before,
        after=row.after,
        correlation_id=row.correlation_id,
        occurred_at=row.occurred_at,
    )


def _model_conversation_values(conversation: ModelConversation) -> dict[str, object]:
    return {
        "id": conversation.id,
        "provider": conversation.provider.value,
        "transport": conversation.transport.value,
        "purpose": conversation.purpose.value,
        "edition_id": conversation.edition_id,
        "subject_id": conversation.subject_id,
        "title": conversation.title,
        "status": conversation.status.value,
        "external_id": conversation.external_id,
        "external_locator": conversation.external_locator,
        "expected_profile": conversation.expected_profile,
        "requested_model": conversation.requested_model,
        "head_turn_id": conversation.head_turn_id,
        "turn_count": conversation.turn_count,
        "created_at": conversation.created_at,
        "updated_at": conversation.updated_at,
        "last_used_at": conversation.last_used_at,
        "version": conversation.version,
    }


def _model_conversation_from_row(row: ModelConversationRow) -> ModelConversation:
    return ModelConversation(
        id=row.id,
        provider=ModelProvider(row.provider),
        transport=ConversationTransport(row.transport),
        purpose=ConversationPurpose(row.purpose),
        edition_id=row.edition_id,
        subject_id=row.subject_id,
        title=row.title,
        status=ConversationStatus(row.status),
        external_id=row.external_id,
        external_locator=row.external_locator,
        expected_profile=row.expected_profile,
        requested_model=row.requested_model,
        head_turn_id=row.head_turn_id,
        turn_count=row.turn_count,
        created_at=row.created_at,
        updated_at=row.updated_at,
        last_used_at=row.last_used_at,
        version=row.version,
    )


def _model_conversation_turn_values(turn: ModelConversationTurn) -> dict[str, object]:
    return {
        "id": turn.id,
        "conversation_id": turn.conversation_id,
        "sequence": turn.sequence,
        "parent_turn_id": turn.parent_turn_id,
        "model_run_id": turn.model_run_id,
        "input_blob_reference": turn.input_blob_reference,
        "input_sha256": turn.input_sha256,
        "output_blob_reference": turn.output_blob_reference,
        "output_sha256": turn.output_sha256,
        "status": turn.status.value,
        "external_turn_id": turn.external_turn_id,
        "idempotency_key": turn.idempotency_key,
        "correlation_id": turn.correlation_id,
        "error_code": turn.error_code,
        "error_message": turn.error_message,
        "error_details": turn.error_details,
        "created_at": turn.created_at,
        "started_at": turn.started_at,
        "finished_at": turn.finished_at,
    }


def _model_conversation_turn_from_row(
    row: ModelConversationTurnRow,
) -> ModelConversationTurn:
    return ModelConversationTurn(
        id=row.id,
        conversation_id=row.conversation_id,
        sequence=row.sequence,
        parent_turn_id=row.parent_turn_id,
        model_run_id=row.model_run_id,
        input_blob_reference=row.input_blob_reference,
        input_sha256=row.input_sha256,
        output_blob_reference=row.output_blob_reference,
        output_sha256=row.output_sha256,
        status=ConversationTurnStatus(row.status),
        external_turn_id=row.external_turn_id,
        idempotency_key=row.idempotency_key,
        correlation_id=row.correlation_id,
        error_code=row.error_code,
        error_message=row.error_message,
        error_details=row.error_details,
        created_at=row.created_at,
        started_at=row.started_at,
        finished_at=row.finished_at,
    )


def _model_run_values(run: ModelRun) -> dict[str, object]:
    return {
        "id": run.id,
        "provider": run.provider.value,
        "model_role": run.model_role.value,
        "requested_model": run.requested_model,
        "actual_model_version": run.actual_model_version,
        "prompt_template_id": run.prompt_template_id,
        "prompt_template_version": run.prompt_template_version,
        "authorized_input_hash": run.authorized_input_hash,
        "evidence_pack_hash": run.evidence_pack_hash,
        "parameters": run.parameters,
        "duration_ms": run.duration_ms,
        "usage": run.usage.snapshot() if run.usage else None,
        "status": run.status.value,
        "response_id": run.response_id,
        "output_references": list(run.output_references),
        "error_code": run.error_code,
        "error_message": run.error_message,
        "error_details": run.error_details,
        "raw_output_reference": run.raw_output_reference,
        "raw_output_sha256": run.raw_output_sha256,
        "raw_output_chars": run.raw_output_chars,
        "normalized_output_reference": run.normalized_output_reference,
        "normalized_output_sha256": run.normalized_output_sha256,
        "parser_stage": run.parser_stage,
        "serializer_version": run.serializer_version,
        "normalization_version": run.normalization_version,
        "json_error_line": run.json_error_line,
        "json_error_column": run.json_error_column,
        "validation_errors": list(run.validation_errors),
        "transformations": list(run.transformations),
        "citation_count": run.citation_count,
        "extracted_url_count": run.extracted_url_count,
        "visible_citations": list(run.visible_citations),
        "started_at": run.started_at,
        "finished_at": run.finished_at,
        "updated_at": run.updated_at,
    }


def _model_run_from_row(row: ModelRunRow) -> ModelRun:
    usage = row.usage
    return ModelRun(
        id=row.id,
        provider=ModelProvider(row.provider),
        model_role=ModelRole(row.model_role),
        requested_model=row.requested_model,
        actual_model_version=row.actual_model_version,
        prompt_template_id=row.prompt_template_id,
        prompt_template_version=row.prompt_template_version,
        authorized_input_hash=row.authorized_input_hash,
        evidence_pack_hash=row.evidence_pack_hash,
        parameters=row.parameters,
        duration_ms=row.duration_ms,
        usage=(
            ModelUsage(
                input_tokens=int(usage.get("input_tokens", 0)),
                output_tokens=int(usage.get("output_tokens", 0)),
                total_tokens=int(usage.get("total_tokens", 0)),
                estimated=bool(usage.get("estimated", False)),
            )
            if usage
            else None
        ),
        status=ModelRunStatus(row.status),
        response_id=row.response_id,
        output_references=tuple(row.output_references),
        error_code=row.error_code,
        error_message=row.error_message,
        error_details=row.error_details,
        raw_output_reference=row.raw_output_reference,
        raw_output_sha256=row.raw_output_sha256,
        raw_output_chars=row.raw_output_chars,
        normalized_output_reference=row.normalized_output_reference,
        normalized_output_sha256=row.normalized_output_sha256,
        parser_stage=row.parser_stage,
        serializer_version=row.serializer_version,
        normalization_version=row.normalization_version,
        json_error_line=row.json_error_line,
        json_error_column=row.json_error_column,
        validation_errors=tuple(row.validation_errors),
        transformations=tuple(row.transformations),
        citation_count=row.citation_count,
        extracted_url_count=row.extracted_url_count,
        visible_citations=tuple(row.visible_citations),
        started_at=row.started_at,
        finished_at=row.finished_at,
        updated_at=row.updated_at,
    )


def _discovery_batch_values(batch: DiscoveryBatch) -> dict[str, object]:
    return {
        "id": batch.id,
        "edition_id": batch.edition_id,
        "request_hash": batch.request_hash,
        "complementary_axis": batch.complementary_axis,
        "status": batch.status.value,
        "discovery_model_run_id": batch.discovery_model_run_id,
        "structuring_model_run_id": batch.structuring_model_run_id,
        "tlp": batch.tlp.value,
        "sensitivity": batch.sensitivity,
        "external_llm_allowed": batch.external_llm_allowed,
        "payload": {
            "report_sha256": batch.report_sha256,
            "parser_version": batch.parser_version,
            "parsing_status": batch.parsing_status,
            "parsing_warnings": list(batch.parsing_warnings),
            "unattached_visible_citations": list(batch.unattached_visible_citations),
            "parsing_revision": batch.parsing_revision,
            "supersedes_batch_id": (
                str(batch.supersedes_batch_id) if batch.supersedes_batch_id else None
            ),
            "replaced_by_batch_id": (
                str(batch.replaced_by_batch_id) if batch.replaced_by_batch_id else None
            ),
            "source_mode": batch.source_mode.value,
            "bridge_capabilities": batch.bridge_capabilities,
            "citation_count": batch.citation_count,
            "source_coverage_complete": batch.source_coverage_complete,
            "source_coverage_incomplete_reason": batch.source_coverage_incomplete_reason,
            "queries": list(batch.queries),
            "citations": list(batch.citations),
            "candidates": [_candidate_payload(candidate) for candidate in batch.candidates],
        },
        "created_at": batch.created_at,
        "updated_at": batch.updated_at,
    }


def _candidate_payload(candidate: CandidateTopic) -> dict[str, object]:
    return {
        "id": str(candidate.id),
        "title": candidate.title,
        "summary": candidate.summary,
        "novelty": candidate.novelty,
        "technical_potential": candidate.technical_potential,
        "event_date": candidate.event_date.isoformat() if candidate.event_date else None,
        "uncertainties": list(candidate.uncertainties),
        "relevance_reasons": list(candidate.relevance_reasons),
        "actors": list(candidate.actors),
        "campaigns": list(candidate.campaigns),
        "malware": list(candidate.malware),
        "cves": list(candidate.cves),
        "victims": list(candidate.victims),
        "sectors": list(candidate.sectors),
        "countries": list(candidate.countries),
        "iocs": list(candidate.iocs),
        "provisional_iocs": [_provisional_ioc_payload(ioc) for ioc in candidate.provisional_iocs],
        "likely_artifacts": list(candidate.likely_artifacts),
        "tlp": candidate.tlp.value,
        "sensitivity": candidate.sensitivity,
        "external_llm_allowed": candidate.external_llm_allowed,
        "editorial_status": candidate.editorial_status,
        "sources": [_source_payload(source) for source in candidate.sources],
        "incomplete_sources": [
            _incomplete_source_payload(source) for source in candidate.incomplete_sources
        ],
        "local_ref": candidate.local_ref,
        "actor_or_campaign": candidate.actor_or_campaign,
        "technical_potential_reason": candidate.technical_potential_reason,
        "parsing_warnings": list(candidate.parsing_warnings),
        "markdown_block": candidate.markdown_block,
        "context_only": candidate.context_only,
    }


def _source_payload(source: SourceCandidate) -> dict[str, object]:
    return {
        "id": str(source.id),
        "url": source.url,
        "raw_url": source.raw_url,
        "local_ref": source.local_ref,
        "source_ref": source.source_ref,
        "title": source.title,
        "publisher": source.publisher,
        "role": source.role.value,
        "published_at": source.published_at.isoformat() if source.published_at else None,
        "event_date": source.event_date.isoformat() if source.event_date else None,
        "citation": source.citation,
        "period_relation": source.period_relation.value,
        "ioc_presence": source.ioc_presence.value,
        "ioc_declared_count": source.ioc_declared_count,
        "ioc_visible_count": source.ioc_visible_count,
        "parsing_warnings": list(source.parsing_warnings),
        "markdown_block": source.markdown_block,
        "verification_status": source.verification_status.value,
        "relationship_status": source.relationship_status.value,
        "verification_changed_at": (
            source.verification_changed_at.isoformat() if source.verification_changed_at else None
        ),
        "verification_changed_by": source.verification_changed_by,
        "tlp": source.tlp.value,
        "sensitivity": source.sensitivity,
        "external_llm_allowed": source.external_llm_allowed,
    }


def _incomplete_source_payload(source: IncompleteSourceCandidate) -> dict[str, object]:
    return {
        "id": str(source.id),
        "title": source.title,
        "publisher": source.publisher,
        "raw_url": source.raw_url,
        "local_ref": source.local_ref,
        "published_at": source.published_at.isoformat() if source.published_at else None,
        "period_relation": source.period_relation.value,
        "role": source.role.value,
        "ioc_presence": source.ioc_presence.value,
        "ioc_declared_count": source.ioc_declared_count,
        "ioc_visible_count": source.ioc_visible_count,
        "parsing_warnings": list(source.parsing_warnings),
        "markdown_block": source.markdown_block,
    }


def _provisional_ioc_payload(ioc: ProvisionalDiscoveryIoc) -> dict[str, object]:
    return {
        "id": str(ioc.id),
        "raw_value": ioc.raw_value,
        "normalized_value": ioc.normalized_value,
        "declared_type": ioc.declared_type,
        "proposed_type": ioc.proposed_type.value,
        "status": ioc.status.value,
        "model_run_id": str(ioc.model_run_id) if ioc.model_run_id else None,
        "markdown_block": ioc.markdown_block,
        "warnings": list(ioc.warnings),
        "publication_relations": [
            {
                "publication_id": str(relation.publication_id),
                "publication_ref": relation.publication_ref,
                "raw_value": relation.raw_value,
                "markdown_block": relation.markdown_block,
            }
            for relation in ioc.publication_relations
        ],
    }


def _discovery_batch_from_row(row: DiscoveryBatchRow) -> DiscoveryBatch:
    payload = row.payload
    return DiscoveryBatch(
        id=row.id,
        edition_id=row.edition_id,
        request_hash=row.request_hash,
        complementary_axis=row.complementary_axis,
        status=DiscoveryBatchStatus(row.status),
        discovery_model_run_id=row.discovery_model_run_id,
        structuring_model_run_id=row.structuring_model_run_id,
        tlp=TLP(row.tlp),
        sensitivity=row.sensitivity,
        external_llm_allowed=row.external_llm_allowed,
        queries=tuple(payload.get("queries", [])),
        citations=tuple(payload.get("citations", [])),
        candidates=[_candidate_from_payload(item) for item in payload.get("candidates", [])],
        report_sha256=(str(payload["report_sha256"]) if payload.get("report_sha256") else None),
        parser_version=str(payload.get("parser_version", "legacy-model-structured")),
        parsing_status=str(payload.get("parsing_status", "completed")),
        parsing_warnings=_string_tuple(payload.get("parsing_warnings", [])),
        unattached_visible_citations=tuple(payload.get("unattached_visible_citations", [])),
        parsing_revision=int(payload.get("parsing_revision", 1)),
        supersedes_batch_id=(
            UUID(str(payload["supersedes_batch_id"]))
            if payload.get("supersedes_batch_id")
            else None
        ),
        replaced_by_batch_id=(
            UUID(str(payload["replaced_by_batch_id"]))
            if payload.get("replaced_by_batch_id")
            else None
        ),
        source_mode=DiscoverySourceMode(
            str(payload.get("source_mode", DiscoverySourceMode.VISIBLE_CITATIONS_ONLY.value))
        ),
        bridge_capabilities=cast(dict[str, object], payload.get("bridge_capabilities", {})),
        citation_count=int(payload.get("citation_count", len(payload.get("citations", [])))),
        source_coverage_complete=bool(payload.get("source_coverage_complete", False)),
        source_coverage_incomplete_reason=(
            str(payload["source_coverage_incomplete_reason"])
            if payload.get("source_coverage_incomplete_reason") is not None
            else None
        ),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _candidate_from_payload(value: dict[str, object]) -> CandidateTopic:
    event_date = value.get("event_date")
    return CandidateTopic(
        id=UUID(str(value["id"])),
        title=str(value["title"]),
        summary=str(value["summary"]),
        novelty=str(value["novelty"]),
        technical_potential=int(str(value["technical_potential"])),
        event_date=date.fromisoformat(str(event_date)) if event_date else None,
        uncertainties=_string_tuple(value.get("uncertainties", [])),
        relevance_reasons=_string_tuple(value.get("relevance_reasons", [])),
        actors=_string_tuple(value.get("actors", [])),
        campaigns=_string_tuple(value.get("campaigns", [])),
        malware=_string_tuple(value.get("malware", [])),
        cves=_string_tuple(value.get("cves", [])),
        victims=_string_tuple(value.get("victims", [])),
        sectors=_string_tuple(value.get("sectors", [])),
        countries=_string_tuple(value.get("countries", [])),
        iocs=_string_tuple(value.get("iocs", [])),
        provisional_iocs=[
            _provisional_ioc_from_payload(item)
            for item in cast(list[dict[str, object]], value.get("provisional_iocs", []))
        ],
        likely_artifacts=_string_tuple(value.get("likely_artifacts", [])),
        sources=[
            _source_from_payload(item)
            for item in cast(list[dict[str, object]], value.get("sources", []))
        ],
        incomplete_sources=[
            _incomplete_source_from_payload(item)
            for item in cast(list[dict[str, object]], value.get("incomplete_sources", []))
        ],
        tlp=TLP(str(value["tlp"])),
        sensitivity=str(value["sensitivity"]),
        external_llm_allowed=bool(value["external_llm_allowed"]),
        local_ref=str(value["local_ref"]) if value.get("local_ref") else None,
        actor_or_campaign=str(value.get("actor_or_campaign", "unknown")),
        technical_potential_reason=str(
            value.get("technical_potential_reason", "Non précisé dans le rapport de découverte.")
        ),
        parsing_warnings=_string_tuple(value.get("parsing_warnings", [])),
        markdown_block=(str(value["markdown_block"]) if value.get("markdown_block") else None),
        context_only=bool(value.get("context_only", False)),
        editorial_status=str(value.get("editorial_status", "proposed")),
    )


def _source_from_payload(value: dict[str, object]) -> SourceCandidate:
    published_at = value.get("published_at")
    event_date = value.get("event_date")
    changed_at = value.get("verification_changed_at")
    return SourceCandidate(
        id=UUID(str(value["id"])),
        url=str(value["url"]),
        title=str(value["title"]),
        publisher=str(value["publisher"]),
        role=SourceRole(str(value["role"])),
        published_at=date.fromisoformat(str(published_at)) if published_at else None,
        event_date=date.fromisoformat(str(event_date)) if event_date else None,
        citation=str(value["citation"]) if value.get("citation") is not None else None,
        raw_url=str(value["raw_url"]) if value.get("raw_url") else None,
        local_ref=str(value["local_ref"]) if value.get("local_ref") else None,
        period_relation=PeriodRelation(
            str(value.get("period_relation", PeriodRelation.UNKNOWN.value))
        ),
        ioc_presence=IocPresence(str(value.get("ioc_presence", IocPresence.UNKNOWN.value))),
        ioc_declared_count=(
            int(str(value["ioc_declared_count"]))
            if value.get("ioc_declared_count") is not None
            else None
        ),
        ioc_visible_count=(
            int(str(value["ioc_visible_count"]))
            if value.get("ioc_visible_count") is not None
            else None
        ),
        parsing_warnings=_string_tuple(value.get("parsing_warnings", [])),
        markdown_block=(str(value["markdown_block"]) if value.get("markdown_block") else None),
        verification_status=SourceVerificationStatus(str(value["verification_status"])),
        relationship_status=SourceRelationshipStatus(
            str(value.get("relationship_status", SourceRelationshipStatus.PROVISIONAL.value))
        ),
        verification_changed_at=(datetime.fromisoformat(str(changed_at)) if changed_at else None),
        verification_changed_by=(
            str(value["verification_changed_by"])
            if value.get("verification_changed_by") is not None
            else None
        ),
        tlp=TLP(str(value["tlp"])),
        sensitivity=str(value["sensitivity"]),
        external_llm_allowed=bool(value["external_llm_allowed"]),
    )


def _incomplete_source_from_payload(value: dict[str, object]) -> IncompleteSourceCandidate:
    published_at = value.get("published_at")
    return IncompleteSourceCandidate(
        id=UUID(str(value["id"])),
        title=str(value["title"]),
        publisher=str(value.get("publisher", "unknown")),
        raw_url=str(value["raw_url"]) if value.get("raw_url") else None,
        local_ref=str(value["local_ref"]) if value.get("local_ref") else None,
        published_at=date.fromisoformat(str(published_at)) if published_at else None,
        period_relation=PeriodRelation(
            str(value.get("period_relation", PeriodRelation.UNKNOWN.value))
        ),
        role=SourceRole(str(value.get("role", SourceRole.UNKNOWN.value))),
        ioc_presence=IocPresence(str(value.get("ioc_presence", IocPresence.UNKNOWN.value))),
        ioc_declared_count=(
            int(str(value["ioc_declared_count"]))
            if value.get("ioc_declared_count") is not None
            else None
        ),
        ioc_visible_count=(
            int(str(value["ioc_visible_count"]))
            if value.get("ioc_visible_count") is not None
            else None
        ),
        parsing_warnings=_string_tuple(value.get("parsing_warnings", [])),
        markdown_block=(str(value["markdown_block"]) if value.get("markdown_block") else None),
    )


def _provisional_ioc_from_payload(value: dict[str, object]) -> ProvisionalDiscoveryIoc:
    relations = cast(list[dict[str, object]], value.get("publication_relations", []))
    return ProvisionalDiscoveryIoc(
        id=UUID(str(value["id"])),
        raw_value=str(value["raw_value"]),
        normalized_value=(
            str(value["normalized_value"]) if value.get("normalized_value") is not None else None
        ),
        declared_type=str(value.get("declared_type", "unknown")),
        proposed_type=DiscoveryIocType(str(value.get("proposed_type", "unknown"))),
        status=DiscoveryIocStatus(str(value.get("status", "provisional_visible"))),
        publication_relations=tuple(
            ProvisionalIocPublicationRelation(
                publication_id=UUID(str(item["publication_id"])),
                publication_ref=str(item["publication_ref"]),
                raw_value=str(item["raw_value"]),
                markdown_block=str(item["markdown_block"]),
            )
            for item in relations
        ),
        model_run_id=(
            UUID(str(value["model_run_id"])) if value.get("model_run_id") is not None else None
        ),
        markdown_block=str(value.get("markdown_block", "")),
        warnings=_string_tuple(value.get("warnings", [])),
    )


def _string_tuple(value: object) -> tuple[str, ...]:
    return tuple(str(item) for item in cast(list[object], value))


def _editorial_group_values(group: EditorialGroup) -> dict[str, object]:
    return {
        "id": group.id,
        "edition_id": group.edition_id,
        "title": group.title,
        "outcome": group.outcome.value,
        "status": group.status.value,
        "source_relationship_status": group.source_relationship_status.value,
        "needs_source_verification": group.needs_source_verification,
        "needs_source_expansion": group.needs_source_expansion,
        "grouping_confidence": group.grouping_confidence.value,
        "grouping_justification": group.grouping_justification,
        "potential_historical_group_id": group.potential_historical_group_id,
        "editorial_type": group.editorial_type.value if group.editorial_type else None,
        "subject_id": group.subject_id,
        "payload": {
            "candidate_references": [
                {"batch_id": str(item.batch_id), "candidate_id": str(item.candidate_id)}
                for item in group.candidate_references
            ],
            "score": {
                "impact": group.score.impact,
                "novelty": group.score.novelty,
                "technical_depth": group.score.technical_depth,
                "hunting_potential": group.score.hunting_potential,
                "actionability": group.score.actionability,
                "source_quality": group.score.source_quality,
                "justifications": group.score.justifications,
            },
        },
        "version": group.version,
        "created_at": group.created_at,
        "updated_at": group.updated_at,
    }


def _editorial_group_from_row(row: EditorialGroupRow) -> EditorialGroup:
    payload = row.payload
    score = cast(dict[str, Any], payload["score"])
    references = cast(list[dict[str, str]], payload["candidate_references"])
    return EditorialGroup(
        id=row.id,
        edition_id=row.edition_id,
        title=row.title,
        candidate_references=tuple(
            CandidateReference(UUID(item["batch_id"]), UUID(item["candidate_id"]))
            for item in references
        ),
        outcome=GroupingOutcome(row.outcome),
        status=EditorialGroupStatus(row.status),
        score=EditorialScore(
            impact=int(score["impact"]),
            novelty=int(score["novelty"]),
            technical_depth=int(score["technical_depth"]),
            hunting_potential=int(score["hunting_potential"]),
            actionability=int(score["actionability"]),
            source_quality=int(score["source_quality"]),
            justifications=cast(dict[str, str], score["justifications"]),
        ),
        source_relationship_status=SourceRelationshipStatus(row.source_relationship_status),
        needs_source_verification=row.needs_source_verification,
        needs_source_expansion=row.needs_source_expansion,
        grouping_confidence=GroupingConfidence(row.grouping_confidence),
        grouping_justification=row.grouping_justification,
        potential_historical_group_id=row.potential_historical_group_id,
        editorial_type=EditorialType(row.editorial_type) if row.editorial_type else None,
        subject_id=row.subject_id,
        version=row.version,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _source_collection_values(collection: SourceCollection) -> dict[str, object]:
    return {
        "id": collection.id,
        "subject_id": collection.subject_id,
        "edition_id": collection.edition_id,
        "group_id": collection.group_id,
        "batch_id": collection.batch_id,
        "source_candidate_id": collection.source_candidate_id,
        "requested_url": collection.requested_url,
        "proposed_role": collection.proposed_role.value,
        "relationship_status": collection.relationship_status.value,
        "relationship_evidence": collection.relationship_evidence,
        "state": collection.state.value,
        "source_document_id": collection.source_document_id,
        "decoded_blob_id": collection.decoded_blob_id,
        "latest_attempt_id": collection.latest_attempt_id,
        "derived_artifact_id": collection.derived_artifact_id,
        "fetch_job_id": collection.fetch_job_id,
        "fetch_policy_snapshot_id": collection.fetch_policy_snapshot_id,
        "fetch_started_at": collection.fetch_started_at,
        "fetch_lease_expires_at": collection.fetch_lease_expires_at,
        "error_reason": collection.error_reason,
        "attempt_count": collection.attempt_count,
        "created_at": collection.created_at,
        "updated_at": collection.updated_at,
    }


def _source_collection_from_row(row: SourceCollectionRow) -> SourceCollection:
    return SourceCollection(
        id=row.id,
        subject_id=row.subject_id,
        edition_id=row.edition_id,
        group_id=row.group_id,
        batch_id=row.batch_id,
        source_candidate_id=row.source_candidate_id,
        requested_url=row.requested_url,
        proposed_role=SourceRole(row.proposed_role),
        relationship_status=SourceRelationshipStatus(row.relationship_status),
        relationship_evidence=row.relationship_evidence,
        state=CollectionState(row.state),
        source_document_id=row.source_document_id,
        decoded_blob_id=row.decoded_blob_id,
        latest_attempt_id=row.latest_attempt_id,
        derived_artifact_id=row.derived_artifact_id,
        fetch_job_id=row.fetch_job_id,
        fetch_policy_snapshot_id=row.fetch_policy_snapshot_id,
        fetch_started_at=row.fetch_started_at,
        fetch_lease_expires_at=row.fetch_lease_expires_at,
        error_reason=row.error_reason,
        attempt_count=row.attempt_count,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _collection_attempt_values(attempt: CollectionAttempt) -> dict[str, object]:
    return {
        "id": attempt.id,
        "collection_id": attempt.collection_id,
        "job_id": attempt.job_id,
        "configuration_id": attempt.policy_snapshot_id,
        "policy_snapshot_id": attempt.policy_snapshot_id,
        "requested_url": attempt.requested_url,
        "final_url": attempt.final_url,
        "redirect_chain": list(attempt.redirect_chain),
        "attempted_at": attempt.attempted_at,
        "completed_at": attempt.completed_at,
        "http_status": attempt.http_status,
        "declared_content_type": attempt.declared_content_type,
        "detected_content_type": attempt.detected_content_type,
        "size": attempt.encoded_size,
        "sha256": attempt.encoded_sha256,
        "encoded_size": attempt.encoded_size,
        "encoded_sha256": attempt.encoded_sha256,
        "decoded_size": attempt.decoded_size,
        "decoded_sha256": attempt.decoded_sha256,
        "content_encoding": attempt.content_encoding,
        "allowed_headers": attempt.allowed_headers,
        "outcome": attempt.outcome.value,
        "failure_reason": attempt.failure_reason,
    }


def _collection_attempt_from_row(row: CollectionAttemptRow) -> CollectionAttempt:
    return CollectionAttempt(
        id=row.id,
        collection_id=row.collection_id,
        job_id=row.job_id,
        policy_snapshot_id=row.policy_snapshot_id,
        requested_url=row.requested_url,
        final_url=row.final_url,
        redirect_chain=tuple(row.redirect_chain),
        attempted_at=row.attempted_at,
        completed_at=row.completed_at,
        http_status=row.http_status,
        declared_content_type=row.declared_content_type,
        detected_content_type=row.detected_content_type,
        encoded_size=row.encoded_size,
        encoded_sha256=row.encoded_sha256,
        decoded_size=row.decoded_size,
        decoded_sha256=row.decoded_sha256,
        content_encoding=row.content_encoding,
        allowed_headers=row.allowed_headers,
        outcome=AttemptOutcome(row.outcome),
        failure_reason=row.failure_reason,
    )


def _derived_artifact_values(artifact: DerivedArtifact) -> dict[str, object]:
    return {
        "id": artifact.id,
        "source_document_id": artifact.source_document_id,
        "text_blob_id": artifact.text_blob_id,
        "parser_name": artifact.parser_name,
        "parser_version": artifact.parser_version,
        "text_length": artifact.text_length,
        "publication_metadata": artifact.publication_metadata,
        "created_at": artifact.created_at,
    }


def _derived_artifact_from_row(row: DerivedArtifactRow) -> DerivedArtifact:
    return DerivedArtifact(
        id=row.id,
        source_document_id=row.source_document_id,
        text_blob_id=row.text_blob_id,
        parser_name=row.parser_name,
        parser_version=row.parser_version,
        text_length=row.text_length,
        publication_metadata=row.publication_metadata,
        created_at=row.created_at,
    )


def _claim_values(claim: Claim) -> dict[str, object]:
    return {
        "id": claim.id,
        "subject_id": claim.subject_id,
        "edition_id": claim.edition_id,
        "group_id": claim.group_id,
        "source_document_id": claim.source_document_id,
        "derived_artifact_id": claim.derived_artifact_id,
        "kind": claim.kind.value,
        "value": claim.value,
        "span_start": claim.span.start,
        "span_end": claim.span.end,
        "extraction_method": claim.extraction_method,
        "extraction_payload": claim.extraction_payload,
        "chunk_id": claim.chunk_id,
        "local_span_start": claim.local_span.start if claim.local_span else None,
        "local_span_end": claim.local_span.end if claim.local_span else None,
        "model_run_id": claim.model_run_id,
        "created_at": claim.created_at,
    }


def _claim_from_row(row: ClaimRow) -> Claim:
    return Claim(
        id=row.id,
        subject_id=row.subject_id,
        edition_id=row.edition_id,
        group_id=row.group_id,
        source_document_id=row.source_document_id,
        derived_artifact_id=row.derived_artifact_id,
        kind=ClaimKind(row.kind),
        value=row.value,
        span=SourceSpan(row.span_start, row.span_end),
        extraction_method=row.extraction_method,
        extraction_payload=row.extraction_payload,
        chunk_id=row.chunk_id,
        local_span=(
            SourceSpan(row.local_span_start, row.local_span_end)
            if row.local_span_start is not None and row.local_span_end is not None
            else None
        ),
        model_run_id=row.model_run_id,
        created_at=row.created_at,
    )


def _indicator_values(indicator: Indicator) -> dict[str, object]:
    return {
        "id": indicator.id,
        "subject_id": indicator.subject_id,
        "edition_id": indicator.edition_id,
        "group_id": indicator.group_id,
        "source_document_id": indicator.source_document_id,
        "derived_artifact_id": indicator.derived_artifact_id,
        "kind": indicator.kind.value,
        "original_value": indicator.original_value,
        "normalized_value": indicator.normalized_value,
        "span_start": indicator.span.start,
        "span_end": indicator.span.end,
        "created_at": indicator.created_at,
    }


def _indicator_from_row(row: IndicatorRow) -> Indicator:
    return Indicator(
        id=row.id,
        subject_id=row.subject_id,
        edition_id=row.edition_id,
        group_id=row.group_id,
        source_document_id=row.source_document_id,
        derived_artifact_id=row.derived_artifact_id,
        kind=IndicatorKind(row.kind),
        original_value=row.original_value,
        normalized_value=row.normalized_value,
        span=SourceSpan(row.span_start, row.span_end),
        created_at=row.created_at,
    )


def _brief_pack_values(pack: BriefEvidencePack) -> dict[str, object]:
    return {
        "id": pack.id,
        "subject_id": pack.subject_id,
        "edition_id": pack.edition_id,
        "group_id": pack.group_id,
        "version": pack.version,
        "content_hash": pack.content_hash,
        "object_hashes": list(pack.object_hashes),
        "sources": list(pack.sources),
        "claims": list(pack.claims),
        "indicators": list(pack.indicators),
        "normalized_entities": list(pack.normalized_entities),
        "uncertainties": list(pack.uncertainties),
        "human_decisions": list(pack.human_decisions),
        "blob_id": pack.blob_id,
        "created_by": pack.created_by,
        "created_at": pack.created_at,
    }


def _brief_pack_from_row(row: BriefEvidencePackRow) -> BriefEvidencePack:
    return BriefEvidencePack(
        id=row.id,
        subject_id=row.subject_id,
        edition_id=row.edition_id,
        group_id=row.group_id,
        version=row.version,
        content_hash=row.content_hash,
        object_hashes=tuple(row.object_hashes),
        sources=tuple(row.sources),
        claims=tuple(row.claims),
        indicators=tuple(row.indicators),
        normalized_entities=tuple(row.normalized_entities),
        uncertainties=tuple(row.uncertainties),
        human_decisions=tuple(row.human_decisions),
        blob_id=row.blob_id,
        created_by=row.created_by,
        created_at=row.created_at,
    )


def _brief_draft_values(draft: BriefDraft) -> dict[str, object]:
    return {
        "id": draft.id,
        "subject_id": draft.subject_id,
        "edition_id": draft.edition_id,
        "group_id": draft.group_id,
        "pack_id": draft.pack_id,
        "pack_hash": draft.pack_hash,
        "version": draft.version,
        "title": draft.title,
        "blocks": [
            {
                "id": str(block.id),
                "sentences": [
                    {
                        "id": str(sentence.id),
                        "text": sentence.text,
                        "factual": sentence.factual,
                        "claim_ids": [str(item) for item in sentence.claim_ids],
                        "indicator_ids": [str(item) for item in sentence.indicator_ids],
                    }
                    for sentence in block.sentences
                ],
            }
            for block in draft.blocks
        ],
        "limits": list(draft.limits),
        "source_ids": [str(item) for item in draft.source_ids],
        "model_run_id": draft.model_run_id,
        "provider": draft.provider,
        "status": draft.status.value,
        "parent_draft_id": draft.parent_draft_id,
        "regenerated_block_id": draft.regenerated_block_id,
        "created_at": draft.created_at,
    }


def _brief_draft_from_row(row: BriefDraftRow) -> BriefDraft:
    return BriefDraft(
        id=row.id,
        subject_id=row.subject_id,
        edition_id=row.edition_id,
        group_id=row.group_id,
        pack_id=row.pack_id,
        pack_hash=row.pack_hash,
        version=row.version,
        title=row.title,
        blocks=tuple(
            BriefBlock(
                id=UUID(str(block["id"])),
                sentences=tuple(
                    BriefSentence(
                        id=UUID(str(sentence["id"])),
                        text=str(sentence["text"]),
                        factual=bool(sentence["factual"]),
                        claim_ids=tuple(UUID(str(item)) for item in sentence["claim_ids"]),
                        indicator_ids=tuple(UUID(str(item)) for item in sentence["indicator_ids"]),
                    )
                    for sentence in block["sentences"]
                ),
            )
            for block in row.blocks
        ),
        limits=tuple(row.limits),
        source_ids=tuple(UUID(item) for item in row.source_ids),
        model_run_id=row.model_run_id,
        provider=row.provider,
        status=BriefDraftStatus(row.status),
        parent_draft_id=row.parent_draft_id,
        regenerated_block_id=row.regenerated_block_id,
        created_at=row.created_at,
    )


def _policy_snapshot_values(snapshot: CollectionPolicySnapshot) -> dict[str, object]:
    return {
        "id": snapshot.id,
        "max_redirects": snapshot.max_redirects,
        "timeout_seconds": snapshot.timeout_seconds,
        "max_download_bytes": snapshot.max_download_bytes,
        "max_expanded_bytes": snapshot.max_expanded_bytes,
        "max_decompression_ratio": snapshot.max_decompression_ratio,
        "user_agent": snapshot.user_agent,
        "allowed_domains": list(snapshot.allowed_domains),
        "blocked_domains": list(snapshot.blocked_domains),
        "collector_version": snapshot.collector_version,
        "extraction_limits": snapshot.extraction_limits,
        "created_at": snapshot.created_at,
    }


def _policy_snapshot_from_row(row: CollectionPolicySnapshotRow) -> CollectionPolicySnapshot:
    return CollectionPolicySnapshot(
        id=row.id,
        max_redirects=row.max_redirects,
        timeout_seconds=row.timeout_seconds,
        max_download_bytes=row.max_download_bytes,
        max_expanded_bytes=row.max_expanded_bytes,
        max_decompression_ratio=row.max_decompression_ratio,
        user_agent=row.user_agent,
        allowed_domains=tuple(row.allowed_domains),
        blocked_domains=tuple(row.blocked_domains),
        collector_version=row.collector_version,
        extraction_limits=row.extraction_limits,
        created_at=row.created_at,
    )


def _rejected_proposal_values(proposal: RejectedModelProposal) -> dict[str, object]:
    return {
        "id": proposal.id,
        "source_document_id": proposal.source_document_id,
        "derived_artifact_id": proposal.derived_artifact_id,
        "chunk_id": proposal.chunk_id,
        "category": proposal.category,
        "requested_kind": proposal.requested_kind,
        "reason": proposal.reason,
        "proposal_hash": proposal.proposal_hash,
        "model_run_id": proposal.model_run_id,
        "created_at": proposal.created_at,
    }


class SqlAlchemySubjectProductionRunRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, run: SubjectProductionRun) -> None:
        row = SubjectProductionRunRow(
            id=run.id,
            subject_id=run.subject_id,
            edition_id=run.edition_id,
            profile=run.profile.value,
            status=run.status.value,
            current_stage=run.current_stage.value,
            conversation_id=run.conversation_id,
            run_number=run.run_number,
            error_code=run.error_code,
            error_message=run.error_message,
            error_details=run.error_details,
            started_at=run.started_at,
            finished_at=run.finished_at,
            created_at=run.created_at,
            updated_at=run.updated_at,
            version=run.version,
        )
        self._session.add(row)

    async def get(self, run_id: UUID) -> SubjectProductionRun | None:
        row = await self._session.get(SubjectProductionRunRow, run_id)
        return _subject_production_run_from_row(row) if row else None

    async def get_for_update(self, run_id: UUID) -> SubjectProductionRun | None:
        query = select(SubjectProductionRunRow).where(
            SubjectProductionRunRow.id == run_id
        ).with_for_update()
        result = await self._session.execute(query)
        row = result.scalar_one_or_none()
        return _subject_production_run_from_row(row) if row else None

    async def save(self, run: SubjectProductionRun) -> None:
        stmt = update(SubjectProductionRunRow).where(
            SubjectProductionRunRow.id == run.id
        ).values(
            status=run.status.value,
            current_stage=run.current_stage.value,
            conversation_id=run.conversation_id,
            error_code=run.error_code,
            error_message=run.error_message,
            error_details=run.error_details,
            started_at=run.started_at,
            finished_at=run.finished_at,
            updated_at=run.updated_at,
            version=run.version,
        )
        await self._session.execute(stmt)

    async def get_current_for_subject(self, subject_id: UUID) -> SubjectProductionRun | None:
        query = (
            select(SubjectProductionRunRow)
            .where(SubjectProductionRunRow.subject_id == subject_id)
            .order_by(SubjectProductionRunRow.created_at.desc())
            .limit(1)
        )
        result = await self._session.execute(query)
        row = result.scalar_one_or_none()
        return _subject_production_run_from_row(row) if row else None

    async def list_for_edition(self, edition_id: UUID) -> Sequence[SubjectProductionRun]:
        query = select(SubjectProductionRunRow).where(
            SubjectProductionRunRow.edition_id == edition_id
        ).order_by(SubjectProductionRunRow.created_at)
        result = await self._session.execute(query)
        return [_subject_production_run_from_row(row) for row in result.scalars()]


class SqlAlchemyProductionArtifactRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def append(self, artifact: ProductionArtifact) -> None:
        row = ProductionArtifactRow(
            id=artifact.id,
            production_run_id=artifact.production_run_id,
            subject_id=artifact.subject_id,
            stage=artifact.stage.value,
            version=artifact.version,
            input_hash=artifact.input_hash,
            status=artifact.status.value,
            raw_blob_id=artifact.raw_blob_id,
            canonical_blob_id=artifact.canonical_blob_id,
            rendered_blob_id=artifact.rendered_blob_id,
            model_run_id=artifact.model_run_id,
            conversation_turn_id=artifact.conversation_turn_id,
            metadata=artifact.metadata,
            created_at=artifact.created_at,
        )
        self._session.add(row)

    async def get(self, artifact_id: UUID) -> ProductionArtifact | None:
        row = await self._session.get(ProductionArtifactRow, artifact_id)
        return _production_artifact_from_row(row) if row else None

    async def get_current(self, run_id: UUID, stage: str) -> ProductionArtifact | None:
        query = (
            select(ProductionArtifactRow)
            .where(
                (ProductionArtifactRow.production_run_id == run_id) &
                (ProductionArtifactRow.stage == stage) &
                (ProductionArtifactRow.status != ProductionArtifactStatus.STALE.value)
            )
            .order_by(ProductionArtifactRow.version.desc())
            .limit(1)
        )
        result = await self._session.execute(query)
        row = result.scalar_one_or_none()
        return _production_artifact_from_row(row) if row else None

    async def list_for_run(self, run_id: UUID) -> Sequence[ProductionArtifact]:
        query = select(ProductionArtifactRow).where(
            ProductionArtifactRow.production_run_id == run_id
        ).order_by(ProductionArtifactRow.stage, ProductionArtifactRow.version)
        result = await self._session.execute(query)
        return [_production_artifact_from_row(row) for row in result.scalars()]

    async def mark_downstream_stale(self, run_id: UUID, stage: str) -> None:
        # Get stage ordering
        stages = [
            ProductionArtifactStage.REFERENCES.value,
            ProductionArtifactStage.EXTRACTION.value,
            ProductionArtifactStage.SYNTHESIS.value,
            ProductionArtifactStage.BRIEF.value,
        ]
        if stage not in stages:
            return
        
        stage_idx = stages.index(stage)
        downstream_stages = stages[stage_idx + 1:]
        
        if downstream_stages:
            stmt = update(ProductionArtifactRow).where(
                (ProductionArtifactRow.production_run_id == run_id) &
                (ProductionArtifactRow.stage.in_(downstream_stages))
            ).values(status=ProductionArtifactStatus.STALE.value)
            await self._session.execute(stmt)


class SqlAlchemyEditionProductionBatchRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, batch: EditionProductionBatch) -> None:
        row = EditionProductionBatchRow(
            id=batch.id,
            edition_id=batch.edition_id,
            profile=batch.profile.value,
            status=batch.status,
            created_at=batch.created_at,
            started_at=batch.started_at,
            finished_at=batch.finished_at,
            version=batch.version,
        )
        self._session.add(row)

    async def get(self, batch_id: UUID) -> EditionProductionBatch | None:
        row = await self._session.get(EditionProductionBatchRow, batch_id)
        return _edition_production_batch_from_row(row) if row else None

    async def get_for_update(self, batch_id: UUID) -> EditionProductionBatch | None:
        query = select(EditionProductionBatchRow).where(
            EditionProductionBatchRow.id == batch_id
        ).with_for_update()
        result = await self._session.execute(query)
        row = result.scalar_one_or_none()
        return _edition_production_batch_from_row(row) if row else None

    async def save(self, batch: EditionProductionBatch) -> None:
        stmt = update(EditionProductionBatchRow).where(
            EditionProductionBatchRow.id == batch.id
        ).values(
            status=batch.status,
            started_at=batch.started_at,
            finished_at=batch.finished_at,
            version=batch.version,
        )
        await self._session.execute(stmt)

    async def get_active_for_edition(self, edition_id: UUID) -> EditionProductionBatch | None:
        query = (
            select(EditionProductionBatchRow)
            .where(
                (EditionProductionBatchRow.edition_id == edition_id) &
                (EditionProductionBatchRow.status.in_(["queued", "running"]))
            )
            .order_by(EditionProductionBatchRow.created_at.desc())
            .limit(1)
        )
        result = await self._session.execute(query)
        row = result.scalar_one_or_none()
        return _edition_production_batch_from_row(row) if row else None


class SqlAlchemyEditionProductionBatchItemRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def append_many(self, items: Sequence[EditionProductionBatchItem]) -> None:
        for item in items:
            row = EditionProductionBatchItemRow(
                id=item.id,
                batch_id=item.batch_id,
                subject_id=item.subject_id,
                production_run_id=item.production_run_id,
                position=item.position,
                created_at=item.created_at,
            )
            self._session.add(row)

    async def list_for_batch(self, batch_id: UUID) -> Sequence[EditionProductionBatchItem]:
        query = select(EditionProductionBatchItemRow).where(
            EditionProductionBatchItemRow.batch_id == batch_id
        ).order_by(EditionProductionBatchItemRow.position)
        result = await self._session.execute(query)
        return [_edition_production_batch_item_from_row(row) for row in result.scalars()]


def _subject_production_run_from_row(row: SubjectProductionRunRow) -> SubjectProductionRun:
    from cti_app.domain.production import (
        ProductionProfile,
        SubjectProductionRun,
        SubjectProductionStage,
        SubjectProductionStatus,
    )
    
    return SubjectProductionRun(
        id=row.id,
        subject_id=row.subject_id,
        edition_id=row.edition_id,
        profile=ProductionProfile(row.profile),
        status=SubjectProductionStatus(row.status),
        current_stage=SubjectProductionStage(row.current_stage),
        conversation_id=row.conversation_id,
        run_number=row.run_number,
        error_code=row.error_code,
        error_message=row.error_message,
        error_details=row.error_details,
        started_at=row.started_at,
        finished_at=row.finished_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
        version=row.version,
    )


def _production_artifact_from_row(row: ProductionArtifactRow) -> ProductionArtifact:
    from cti_app.domain.production import (
        ProductionArtifact,
        ProductionArtifactStage,
        ProductionArtifactStatus,
    )
    
    return ProductionArtifact(
        id=row.id,
        production_run_id=row.production_run_id,
        subject_id=row.subject_id,
        stage=ProductionArtifactStage(row.stage),
        version=row.version,
        input_hash=row.input_hash,
        status=ProductionArtifactStatus(row.status),
        raw_blob_id=row.raw_blob_id,
        canonical_blob_id=row.canonical_blob_id,
        rendered_blob_id=row.rendered_blob_id,
        model_run_id=row.model_run_id,
        conversation_turn_id=row.conversation_turn_id,
        metadata=row.metadata,
        created_at=row.created_at,
    )


def _edition_production_batch_from_row(row: EditionProductionBatchRow) -> EditionProductionBatch:
    from cti_app.domain.production import (
        EditionProductionBatch,
        ProductionProfile,
    )
    
    return EditionProductionBatch(
        id=row.id,
        edition_id=row.edition_id,
        profile=ProductionProfile(row.profile),
        status=row.status,
        created_at=row.created_at,
        started_at=row.started_at,
        finished_at=row.finished_at,
        version=row.version,
    )


def _edition_production_batch_item_from_row(row: EditionProductionBatchItemRow) -> EditionProductionBatchItem:
    from cti_app.domain.production import EditionProductionBatchItem
    
    return EditionProductionBatchItem(
        id=row.id,
        batch_id=row.batch_id,
        subject_id=row.subject_id,
        production_run_id=row.production_run_id,
        position=row.position,
        created_at=row.created_at,
    )
