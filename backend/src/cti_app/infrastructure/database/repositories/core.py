"""RF-P3/R09: repositories for Blob, Subject, SourceDocument, Sample, Provenance —
foundational entities every other bounded context references. Owns the only
row/domain mappers for these rows."""

from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from cti_app.domain.blobs import BlobDescriptor, BlobRecord
from cti_app.domain.classification import TLP
from cti_app.domain.entities import ProvenanceEvent, Sample, SourceDocument, Subject
from cti_app.domain.errors import EntityNotFoundError
from cti_app.domain.virustotal import VirusTotalFileView, VirusTotalObservation
from cti_app.infrastructure.database.models.briefs import BriefEvidencePackRow
from cti_app.infrastructure.database.models.collection import (
    DerivedArtifactRow,
    SourceCollectionRow,
)
from cti_app.infrastructure.database.models.core import (
    BlobRow,
    ProvenanceEventRow,
    SampleRow,
    SourceDocumentRow,
    SubjectRow,
    VirusTotalFileViewRow,
    VirusTotalObservationRow,
)
from cti_app.infrastructure.database.models.model_execution import (
    ModelConversationTurnRow,
    ModelRunRow,
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
        virustotal_observation_count = await self._session.scalar(
            select(func.count())
            .select_from(VirusTotalObservationRow)
            .where(VirusTotalObservationRow.blob_id == blob_id)
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
            + int(virustotal_observation_count or 0)
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


class SqlAlchemyVirusTotalObservationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, observation: VirusTotalObservation) -> None:
        self._session.add(
            VirusTotalObservationRow(
                id=observation.id,
                subject_id=observation.subject_id,
                operation=observation.operation.value,
                capability=observation.capability.value,
                source_identifier=observation.source_identifier,
                safe_parameters=observation.safe_parameters,
                http_status=observation.http_status,
                blob_id=observation.blob_id,
                raw_sha256=observation.raw_sha256,
                raw_size=observation.raw_size,
                observed_at=observation.observed_at,
                input_cursor=observation.input_cursor,
                output_cursor=observation.output_cursor,
                observed_count=observation.observed_count,
                exhaustive=observation.exhaustive,
                page_order=observation.page_order,
                normalization_contract_version=observation.normalization_contract_version,
            )
        )
        await self._session.flush()


class SqlAlchemyVirusTotalFileViewRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add_if_absent(self, view: VirusTotalFileView) -> bool:
        if await self._session.scalar(
            select(VirusTotalFileViewRow.id).where(
                VirusTotalFileViewRow.observation_id == view.observation_id
            )
        ):
            return False
        self._session.add(
            VirusTotalFileViewRow(
                id=view.id,
                observation_id=view.observation_id,
                vt_file_id=view.vt_file_id,
                file_type=view.file_type,
                lookup_hash=view.lookup_hash,
                meaningful_name=view.meaningful_name,
                type_description=view.type_description,
                size=view.size,
                last_analysis_stats=view.last_analysis_stats,
                first_submission_date=view.first_submission_date,
                last_submission_date=view.last_submission_date,
                last_modification_date=view.last_modification_date,
                tags=list(view.tags),
            )
        )
        await self._session.flush()
        return True


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
