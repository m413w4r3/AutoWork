from collections.abc import Sequence
from datetime import date, datetime
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from cti_app.domain.blobs import BlobDescriptor, BlobRecord
from cti_app.domain.classification import TLP
from cti_app.domain.editions import Edition, EditionAuditEvent, EditionStatus
from cti_app.domain.entities import ProvenanceEvent, Sample, SourceDocument, Subject
from cti_app.domain.jobs import Job, JobEvent, JobOperationalMetrics, JobStatus
from cti_app.domain.model_runs import (
    ModelProvider,
    ModelRole,
    ModelRun,
    ModelRunStatus,
    ModelUsage,
)
from cti_app.infrastructure.database.models import (
    BlobRow,
    EditionAuditEventRow,
    EditionRow,
    JobEventRow,
    JobRow,
    ModelRunRow,
    ProvenanceEventRow,
    SampleRow,
    SourceDocumentRow,
    SubjectRow,
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
        sample_count = await self._session.scalar(
            select(func.count()).select_from(SampleRow).where(SampleRow.blob_id == blob_id)
        )
        model_output_count = await self._session.scalar(
            select(func.count())
            .select_from(ModelRunRow)
            .where(ModelRunRow.output_references.contains([f"blob://{blob_id}"]))
        )
        return int(document_count or 0) + int(sample_count or 0) + int(model_output_count or 0)

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
        await self._session.flush()


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
        started_at=row.started_at,
        finished_at=row.finished_at,
        updated_at=row.updated_at,
    )
