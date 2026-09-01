from collections.abc import Sequence
from datetime import datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from cti_app.application.persistence import ActiveSubjectProductionRunConflictError
from cti_app.application.production_read_model import BatchStatusItem
from cti_app.domain.editorial import AnalystDecision, AnalystDecisionTargetType, AnalystDecisionType
from cti_app.domain.production import (
    AnalystInputPack,
    AnalystInvestigation,
    EditionProductionBatch,
    EditionProductionBatchItem,
    ExtractionProfile,
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionArtifactStatus,
    ProductionBatchStatus,
    ProductionInputSnapshot,
    ProductionInputSource,
    ProductionReuseInvalidation,
    SampleAcquisitionAttempt,
    SampleAcquisitionOutcome,
    SampleAcquisitionReason,
    SourceExtraction,
    SourceExtractionStatus,
    SubjectProductionRun,
    SubjectProductionStage,
    SubjectProductionStatus,
)
from cti_app.infrastructure.database.models.editorial import EditorialGroupRow
from cti_app.infrastructure.database.models.production import (
    AnalystDecisionRow,
    AnalystInputPackRow,
    AnalystInvestigationRow,
    EditionProductionBatchItemRow,
    EditionProductionBatchRow,
    ProductionArtifactRow,
    ProductionInputSnapshotRow,
    ProductionReuseInvalidationRow,
    SampleAcquisitionAttemptRow,
    SourceExtractionRow,
    SubjectProductionRunRow,
)


class SqlAlchemyAnalystInvestigationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, investigation_id: UUID) -> AnalystInvestigation | None:
        row = await self._session.get(AnalystInvestigationRow, investigation_id)
        return _analyst_investigation_from_row(row) if row else None

    async def get_for_run(self, run_id: UUID) -> AnalystInvestigation | None:
        row = await self._session.scalar(
            select(AnalystInvestigationRow).where(
                AnalystInvestigationRow.production_run_id == run_id
            )
        )
        return _analyst_investigation_from_row(row) if row else None

    async def add(self, value: AnalystInvestigation) -> None:
        self._session.add(_analyst_investigation_row(value))
        # AnalystInputPackRow has no ORM relationship to order its insert
        # after this parent.  A handoff persists both in one UoW, so materialize
        # the parent before an FK child can be added.
        await self._session.flush()

    async def save(self, value: AnalystInvestigation) -> None:
        result = await self._session.execute(
            update(AnalystInvestigationRow)
            .where(
                AnalystInvestigationRow.id == value.id,
                AnalystInvestigationRow.version == value.version - 1,
            )
            .values(**_analyst_investigation_values(value))
        )
        if cast(Any, result).rowcount != 1:
            raise RuntimeError("stale analyst investigation write")


class SqlAlchemyAnalystDecisionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def append(self, value: AnalystDecision) -> None:
        self._session.add(
            AnalystDecisionRow(
                id=value.id,
                investigation_id=value.investigation_id,
                decision_type=value.decision_type.value,
                target_type=value.target_type.value,
                target_id=value.target_id,
                actor_id=value.actor_id,
                reason=value.reason,
                correlation_id=value.correlation_id,
                occurred_at=value.occurred_at,
            )
        )

    async def list_for_investigation(self, investigation_id: UUID) -> Sequence[AnalystDecision]:
        query = (
            select(AnalystDecisionRow)
            .where(AnalystDecisionRow.investigation_id == investigation_id)
            .order_by(AnalystDecisionRow.occurred_at)
        )
        rows = (await self._session.execute(query)).scalars()
        return [_analyst_decision_from_row(row) for row in rows]


class SqlAlchemyAnalystInputPackRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_for_investigation(self, investigation_id: UUID) -> AnalystInputPack | None:
        row = await self._session.scalar(
            select(AnalystInputPackRow).where(
                AnalystInputPackRow.investigation_id == investigation_id
            )
        )
        return _analyst_input_pack_from_row(row) if row else None

    async def append(self, value: AnalystInputPack) -> None:
        self._session.add(
            AnalystInputPackRow(
                id=value.id,
                investigation_id=value.investigation_id,
                blob_id=value.blob_id,
                sha256=value.sha256,
                schema_version=value.schema_version,
                created_at=value.created_at,
            )
        )


class SqlAlchemySampleAcquisitionAttemptRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def find_successful(
        self, investigation_id: UUID, requested_hash: str
    ) -> SampleAcquisitionAttempt | None:
        row = await self._session.scalar(
            select(SampleAcquisitionAttemptRow).where(
                SampleAcquisitionAttemptRow.investigation_id == investigation_id,
                SampleAcquisitionAttemptRow.requested_hash == requested_hash,
                SampleAcquisitionAttemptRow.outcome == SampleAcquisitionOutcome.SUCCESS.value,
            )
        )
        return _sample_acquisition_attempt_from_row(row) if row else None

    async def append(self, attempt: SampleAcquisitionAttempt) -> None:
        self._session.add(
            SampleAcquisitionAttemptRow(
                id=attempt.id,
                investigation_id=attempt.investigation_id,
                requested_hash=attempt.requested_hash,
                hash_family=attempt.hash_family,
                reason=attempt.reason.value,
                outcome=attempt.outcome.value,
                sample_id=attempt.sample_id,
                error_code=attempt.error_code,
                occurred_at=attempt.occurred_at,
            )
        )
        await self._session.flush()


def _sample_acquisition_attempt_from_row(
    row: SampleAcquisitionAttemptRow,
) -> SampleAcquisitionAttempt:
    return SampleAcquisitionAttempt(
        id=row.id,
        investigation_id=row.investigation_id,
        requested_hash=row.requested_hash,
        hash_family=row.hash_family,
        reason=SampleAcquisitionReason(row.reason),
        outcome=SampleAcquisitionOutcome(row.outcome),
        sample_id=row.sample_id,
        error_code=row.error_code,
        occurred_at=row.occurred_at,
    )


class SqlAlchemySubjectProductionRunRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, run: SubjectProductionRun) -> None:
        row = SubjectProductionRunRow(
            id=run.id,
            subject_id=run.subject_id,
            edition_id=run.edition_id,
            status=run.status.value,
            current_stage=run.current_stage.value,
            references_conversation_id=run.references_conversation_id,
            synthesis_conversation_id=run.synthesis_conversation_id,
            run_number=run.run_number,
            pipeline_generation=run.pipeline_generation,
            research_date=run.research_date,
            force_recompute_from_stage=(
                run.force_recompute_from_stage.value
                if run.force_recompute_from_stage is not None
                else None
            ),
            error_code=run.error_code,
            error_message=run.error_message,
            error_details=run.error_details,
            extraction_progress=run.extraction_progress,
            started_at=run.started_at,
            finished_at=run.finished_at,
            created_at=run.created_at,
            updated_at=run.updated_at,
            version=run.version,
        )
        self._session.add(row)
        # Repository rows do not have ORM relationships, so SQLAlchemy cannot
        # infer this FK ordering when artifacts are added in the same UoW.
        try:
            await self._session.flush()
        except IntegrityError as exc:
            constraint_name = getattr(getattr(exc.orig, "diag", None), "constraint_name", None)
            constraint_name = constraint_name or getattr(exc.orig, "constraint_name", None)
            if constraint_name == "uq_subject_production_one_active_run":
                raise ActiveSubjectProductionRunConflictError from exc
            raise

    async def get(self, run_id: UUID) -> SubjectProductionRun | None:
        row = await self._session.get(SubjectProductionRunRow, run_id)
        return _subject_production_run_from_row(row) if row else None

    async def get_for_update(self, run_id: UUID) -> SubjectProductionRun | None:
        query = (
            select(SubjectProductionRunRow)
            .where(SubjectProductionRunRow.id == run_id)
            .with_for_update()
        )
        result = await self._session.execute(query)
        row = result.scalar_one_or_none()
        return _subject_production_run_from_row(row) if row else None

    async def save(self, run: SubjectProductionRun) -> None:
        stmt = (
            update(SubjectProductionRunRow)
            .where(SubjectProductionRunRow.id == run.id)
            .values(
                status=run.status.value,
                current_stage=run.current_stage.value,
                references_conversation_id=run.references_conversation_id,
                synthesis_conversation_id=run.synthesis_conversation_id,
                pipeline_generation=run.pipeline_generation,
                research_date=run.research_date,
                force_recompute_from_stage=(
                    run.force_recompute_from_stage.value
                    if run.force_recompute_from_stage is not None
                    else None
                ),
                error_code=run.error_code,
                error_message=run.error_message,
                error_details=run.error_details,
                extraction_progress=run.extraction_progress,
                started_at=run.started_at,
                finished_at=run.finished_at,
                updated_at=run.updated_at,
                version=run.version,
            )
        )
        await self._session.execute(stmt)

    async def get_current_for_subject(self, subject_id: UUID) -> SubjectProductionRun | None:
        query = (
            select(SubjectProductionRunRow)
            .where(SubjectProductionRunRow.subject_id == subject_id)
            .order_by(SubjectProductionRunRow.created_at.desc(), SubjectProductionRunRow.id.desc())
            .limit(1)
        )
        result = await self._session.execute(query)
        row = result.scalar_one_or_none()
        return _subject_production_run_from_row(row) if row else None

    async def get_latest_terminal_for_edition_subject(
        self, edition_id: UUID, subject_id: UUID
    ) -> SubjectProductionRun | None:
        query = (
            select(SubjectProductionRunRow)
            .where(
                (SubjectProductionRunRow.edition_id == edition_id)
                & (SubjectProductionRunRow.subject_id == subject_id)
                & SubjectProductionRunRow.status.in_(
                    [
                        SubjectProductionStatus.READY.value,
                        SubjectProductionStatus.NEEDS_REVIEW.value,
                        SubjectProductionStatus.FAILED.value,
                        SubjectProductionStatus.CANCELLED.value,
                    ]
                )
            )
            .order_by(SubjectProductionRunRow.created_at.desc(), SubjectProductionRunRow.id.desc())
            .limit(1)
        )
        result = await self._session.execute(query)
        row = result.scalar_one_or_none()
        return _subject_production_run_from_row(row) if row else None

    async def list_for_edition(self, edition_id: UUID) -> Sequence[SubjectProductionRun]:
        query = (
            select(SubjectProductionRunRow)
            .where(SubjectProductionRunRow.edition_id == edition_id)
            .order_by(SubjectProductionRunRow.created_at)
        )
        result = await self._session.execute(query)
        return [_subject_production_run_from_row(row) for row in result.scalars()]

    async def lock_creation_for_subject(self, subject_id: UUID) -> None:
        """Serialize run creation for one subject until this transaction ends."""
        await self._session.execute(
            select(func.pg_advisory_xact_lock(func.hashtext(str(subject_id))))
        )

    async def allocate_next_run_number(self, subject_id: UUID) -> int:
        """Return the next subject-local number after the creation lock is held."""
        maximum = await self._session.scalar(
            select(func.max(SubjectProductionRunRow.run_number)).where(
                SubjectProductionRunRow.subject_id == subject_id
            )
        )
        return int(maximum or 0) + 1


class SqlAlchemyProductionInputSnapshotRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, snapshot: ProductionInputSnapshot) -> None:
        self._session.add(
            ProductionInputSnapshotRow(
                id=snapshot.id,
                production_run_id=snapshot.production_run_id,
                subject_id=snapshot.subject_id,
                edition_id=snapshot.edition_id,
                editorial_group_id=snapshot.editorial_group_id,
                editorial_group_version=snapshot.editorial_group_version,
                subject_title=snapshot.subject_title,
                subject_description=snapshot.subject_description,
                actor_or_campaign=snapshot.actor_or_campaign,
                period_start=snapshot.period_start,
                period_end=snapshot.period_end,
                research_date=snapshot.research_date,
                core_sources=[source.payload() for source in snapshot.core_sources],
                input_hash=snapshot.input_hash,
                reuse_basis_hash=snapshot.reuse_basis_hash,
                captured_at=snapshot.captured_at,
            )
        )
        await self._session.flush()

    async def get(self, snapshot_id: UUID) -> ProductionInputSnapshot | None:
        row = await self._session.get(ProductionInputSnapshotRow, snapshot_id)
        return _production_input_snapshot_from_row(row) if row else None

    async def get_by_run(self, production_run_id: UUID) -> ProductionInputSnapshot | None:
        row = await self._session.scalar(
            select(ProductionInputSnapshotRow).where(
                ProductionInputSnapshotRow.production_run_id == production_run_id
            )
        )
        return _production_input_snapshot_from_row(row) if row else None


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
            reused_from_artifact_id=artifact.reused_from_artifact_id,
            artifact_metadata=artifact.metadata,
            created_at=artifact.created_at,
        )
        self._session.add(row)
        # Materialize the artifact before it can become an FK parent.
        # AnalystInvestigation.synthesis_artifact_id may reference it immediately.
        await self._session.flush()

    async def get(self, artifact_id: UUID) -> ProductionArtifact | None:
        row = await self._session.get(ProductionArtifactRow, artifact_id)
        return _production_artifact_from_row(row) if row else None

    async def get_current(self, run_id: UUID, stage: str) -> ProductionArtifact | None:
        query = (
            select(ProductionArtifactRow)
            .where(
                (ProductionArtifactRow.production_run_id == run_id)
                & (ProductionArtifactRow.stage == stage)
                & (ProductionArtifactRow.status != ProductionArtifactStatus.STALE.value)
            )
            .order_by(ProductionArtifactRow.version.desc())
            .limit(1)
        )
        result = await self._session.execute(query)
        row = result.scalar_one_or_none()
        return _production_artifact_from_row(row) if row else None

    async def find_reusable(
        self,
        *,
        edition_id: UUID,
        subject_id: UUID,
        stage: str,
        input_hash: str,
        not_before: datetime | None = None,
    ) -> ProductionArtifact | None:
        required_blob = {
            ProductionArtifactStage.REFERENCES.value: ProductionArtifactRow.canonical_blob_id,
            ProductionArtifactStage.EXTRACTION.value: ProductionArtifactRow.canonical_blob_id,
            ProductionArtifactStage.SYNTHESIS.value: ProductionArtifactRow.rendered_blob_id,
        }.get(stage)
        if required_blob is None:
            return None

        query = (
            select(ProductionArtifactRow)
            .join(
                SubjectProductionRunRow,
                SubjectProductionRunRow.id == ProductionArtifactRow.production_run_id,
            )
            .where(
                (SubjectProductionRunRow.edition_id == edition_id)
                & (SubjectProductionRunRow.subject_id == subject_id)
                & (ProductionArtifactRow.subject_id == subject_id)
                & SubjectProductionRunRow.status.in_(
                    [
                        SubjectProductionStatus.READY.value,
                        SubjectProductionStatus.NEEDS_REVIEW.value,
                        SubjectProductionStatus.FAILED.value,
                        SubjectProductionStatus.CANCELLED.value,
                    ]
                )
                & (ProductionArtifactRow.stage == stage)
                & (ProductionArtifactRow.input_hash == input_hash)
                & (ProductionArtifactRow.status == ProductionArtifactStatus.VERIFIED.value)
                & required_blob.is_not(None)
            )
            .order_by(ProductionArtifactRow.created_at.desc(), ProductionArtifactRow.id.desc())
            .limit(1)
        )
        if not_before is not None:
            query = query.where(ProductionArtifactRow.created_at > not_before)
        result = await self._session.execute(query)
        row = result.scalar_one_or_none()
        return _production_artifact_from_row(row) if row else None

    async def list_for_run(self, run_id: UUID) -> Sequence[ProductionArtifact]:
        query = (
            select(ProductionArtifactRow)
            .where(ProductionArtifactRow.production_run_id == run_id)
            .order_by(ProductionArtifactRow.stage, ProductionArtifactRow.version)
        )
        result = await self._session.execute(query)
        return [_production_artifact_from_row(row) for row in result.scalars()]

    async def mark_downstream_stale(self, run_id: UUID, stage: str) -> None:
        stages = [
            ProductionArtifactStage.REFERENCES.value,
            ProductionArtifactStage.EXTRACTION.value,
            ProductionArtifactStage.SYNTHESIS.value,
            ProductionArtifactStage.PUBLICATION.value,
        ]
        if stage not in stages:
            return

        stage_idx = stages.index(stage)
        downstream_stages = stages[stage_idx + 1 :]

        if downstream_stages:
            stmt = (
                update(ProductionArtifactRow)
                .where(
                    (ProductionArtifactRow.production_run_id == run_id)
                    & (ProductionArtifactRow.stage.in_(downstream_stages))
                )
                .values(status=ProductionArtifactStatus.STALE.value)
            )
            await self._session.execute(stmt)

    async def mark_from_stage_stale(self, run_id: UUID, stage: str) -> list[str]:
        """Mark selected production output and every downstream output stale."""
        pipeline = ["sources", "references", "extraction", "synthesis", "assembly"]
        if stage not in pipeline:
            return []
        artifact_stages = {
            "references": "references",
            "extraction": "extraction",
            "synthesis": "synthesis",
            "assembly": "publication",
        }
        affected = [
            artifact_stages[item]
            for item in pipeline[pipeline.index(stage) :]
            if item in artifact_stages
        ]
        if not affected:
            return []
        await self._session.execute(
            update(ProductionArtifactRow)
            .where(
                (ProductionArtifactRow.production_run_id == run_id)
                & (ProductionArtifactRow.stage.in_(affected))
                & (ProductionArtifactRow.status != ProductionArtifactStatus.STALE.value)
            )
            .values(status=ProductionArtifactStatus.STALE.value)
        )
        return affected


class SqlAlchemySourceExtractionRepository:
    """Repository for the subject-independent Q2 checkpoint catalog."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_identity(
        self,
        *,
        source_content_sha256: str,
        profile: str,
        contract_version: str,
        prompt_version: str,
        parser_version: str,
        verifier_version: str,
    ) -> SourceExtraction | None:
        values = {
            "source_content_sha256": source_content_sha256,
            "profile": profile,
            "contract_version": contract_version,
            "prompt_version": prompt_version,
            "parser_version": parser_version,
            "verifier_version": verifier_version,
        }
        row = await self._session.scalar(
            select(SourceExtractionRow).where(
                *(getattr(SourceExtractionRow, key) == value for key, value in values.items())
            )
        )
        return _source_extraction_from_row(row) if row else None

    async def claim(self, extraction: SourceExtraction, *, force: bool = False) -> bool:
        values = _source_extraction_values(extraction)
        inserted_id = await self._session.scalar(
            insert(SourceExtractionRow)
            .values(**values)
            .on_conflict_do_nothing(constraint="uq_source_extractions_identity")
            .returning(SourceExtractionRow.id)
        )
        if inserted_id is not None:
            await self._session.flush()
            return True

        row = await self._session.scalar(
            select(SourceExtractionRow)
            .where(
                SourceExtractionRow.source_content_sha256 == extraction.source_content_sha256,
                SourceExtractionRow.profile == extraction.profile.value,
                SourceExtractionRow.contract_version == extraction.contract_version,
                SourceExtractionRow.prompt_version == extraction.prompt_version,
                SourceExtractionRow.parser_version == extraction.parser_version,
                SourceExtractionRow.verifier_version == extraction.verifier_version,
            )
            .with_for_update()
        )
        if row is None:
            # A concurrent transaction can only reach this branch if its
            # insert was rolled back. Let the caller retry the claim.
            return False
        if (
            row.status
            in {
                SourceExtractionStatus.VERIFIED.value,
                SourceExtractionStatus.RUNNING.value,
            }
            and not force
        ):
            return False

        for key, value in values.items():
            if key in {"id", "created_at"}:
                continue
            setattr(row, key, value)
        await self._session.flush()
        return True

    async def save(self, extraction: SourceExtraction) -> None:
        row = await self._session.get(SourceExtractionRow, extraction.id)
        if row is None:
            raise LookupError(f"Source extraction {extraction.id} does not exist")
        for key, value in _source_extraction_values(extraction).items():
            setattr(row, key, value)
        await self._session.flush()


class SqlAlchemyProductionReuseInvalidationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, invalidation: ProductionReuseInvalidation) -> None:
        self._session.add(
            ProductionReuseInvalidationRow(
                id=invalidation.id,
                edition_id=invalidation.edition_id,
                subject_id=invalidation.subject_id,
                from_stage=invalidation.from_stage.value,
                actor_id=invalidation.actor_id,
                correlation_id=invalidation.correlation_id,
                occurred_at=invalidation.occurred_at,
            )
        )
        await self._session.flush()

    async def list_for_subject(
        self, edition_id: UUID, subject_id: UUID
    ) -> Sequence[ProductionReuseInvalidation]:
        query = (
            select(ProductionReuseInvalidationRow)
            .where(
                (ProductionReuseInvalidationRow.edition_id == edition_id)
                & (ProductionReuseInvalidationRow.subject_id == subject_id)
            )
            .order_by(ProductionReuseInvalidationRow.occurred_at)
        )
        result = await self._session.execute(query)
        return [_production_reuse_invalidation_from_row(row) for row in result.scalars()]


class SqlAlchemyEditionProductionBatchRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, batch: EditionProductionBatch) -> None:
        row = EditionProductionBatchRow(
            id=batch.id,
            edition_id=batch.edition_id,
            status=batch.status.value,
            phase=batch.phase.value,
            next_dispatch_at=batch.next_dispatch_at,
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
        query = (
            select(EditionProductionBatchRow)
            .where(EditionProductionBatchRow.id == batch_id)
            .with_for_update()
        )
        result = await self._session.execute(query)
        row = result.scalar_one_or_none()
        return _edition_production_batch_from_row(row) if row else None

    async def get_latest_for_edition(self, edition_id: UUID) -> EditionProductionBatch | None:
        query = (
            select(EditionProductionBatchRow)
            .where(EditionProductionBatchRow.edition_id == edition_id)
            .order_by(
                EditionProductionBatchRow.created_at.desc(),
                EditionProductionBatchRow.id.desc(),
            )
            .limit(1)
        )
        result = await self._session.execute(query)
        row = result.scalar_one_or_none()
        return _edition_production_batch_from_row(row) if row else None

    async def save(self, batch: EditionProductionBatch) -> None:
        stmt = (
            update(EditionProductionBatchRow)
            .where(EditionProductionBatchRow.id == batch.id)
            .values(
                status=batch.status.value,
                phase=batch.phase.value,
                next_dispatch_at=batch.next_dispatch_at,
                started_at=batch.started_at,
                finished_at=batch.finished_at,
                version=batch.version,
            )
        )
        await self._session.execute(stmt)

    async def get_active_for_edition(self, edition_id: UUID) -> EditionProductionBatch | None:
        query = (
            select(EditionProductionBatchRow)
            .where(
                (EditionProductionBatchRow.edition_id == edition_id)
                & (
                    EditionProductionBatchRow.status.in_(
                        (ProductionBatchStatus.QUEUED.value, ProductionBatchStatus.RUNNING.value)
                    )
                )
            )
            .order_by(
                EditionProductionBatchRow.created_at.desc(),
                EditionProductionBatchRow.id.desc(),
            )
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
                auto_recovery_count=item.auto_recovery_count,
                created_at=item.created_at,
            )
            self._session.add(row)

    async def list_for_batch(self, batch_id: UUID) -> Sequence[EditionProductionBatchItem]:
        query = (
            select(EditionProductionBatchItemRow)
            .where(EditionProductionBatchItemRow.batch_id == batch_id)
            .order_by(EditionProductionBatchItemRow.position)
        )
        result = await self._session.execute(query)
        return [_edition_production_batch_item_from_row(row) for row in result.scalars()]

    async def get_by_run(self, run_id: UUID) -> EditionProductionBatchItem | None:
        query = select(EditionProductionBatchItemRow).where(
            EditionProductionBatchItemRow.production_run_id == run_id
        )
        result = await self._session.execute(query)
        row = result.scalars().first()
        return _edition_production_batch_item_from_row(row) if row else None

    async def save(self, item: EditionProductionBatchItem) -> None:
        await self._session.execute(
            update(EditionProductionBatchItemRow)
            .where(EditionProductionBatchItemRow.id == item.id)
            .values(auto_recovery_count=item.auto_recovery_count)
        )


class SqlAlchemyBatchStatusReadRepository:
    """Optimized, UI-only read model for production batch status."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_for_batch(self, batch_id: UUID) -> Sequence[BatchStatusItem]:
        # A subject can have historical editorial-group rows.  The scalar
        # subquery preserves one result row per batch item while retaining the
        # same group-title fallback as the transactional API used to have.
        group_title = (
            select(EditorialGroupRow.title)
            .where(EditorialGroupRow.subject_id == EditionProductionBatchItemRow.subject_id)
            .order_by(EditorialGroupRow.created_at.desc(), EditorialGroupRow.id.desc())
            .limit(1)
            .scalar_subquery()
        )
        query = (
            select(
                EditionProductionBatchItemRow.position.label("position"),
                EditionProductionBatchItemRow.subject_id.label("subject_id"),
                func.coalesce(
                    ProductionInputSnapshotRow.subject_title,
                    group_title,
                ).label("title"),
                EditionProductionBatchItemRow.production_run_id.label("run_id"),
                SubjectProductionRunRow.status.label("status"),
                SubjectProductionRunRow.current_stage.label("current_stage"),
                SubjectProductionRunRow.pipeline_generation.label("pipeline_generation"),
                EditionProductionBatchItemRow.auto_recovery_count.label("auto_recovery_count"),
                SubjectProductionRunRow.error_code.label("error_code"),
                SubjectProductionRunRow.error_message.label("error_message"),
                SubjectProductionRunRow.extraction_progress.label("extraction_progress"),
            )
            .select_from(EditionProductionBatchItemRow)
            .join(
                SubjectProductionRunRow,
                SubjectProductionRunRow.id == EditionProductionBatchItemRow.production_run_id,
            )
            .outerjoin(
                ProductionInputSnapshotRow,
                ProductionInputSnapshotRow.production_run_id
                == EditionProductionBatchItemRow.production_run_id,
            )
            .where(EditionProductionBatchItemRow.batch_id == batch_id)
            .order_by(EditionProductionBatchItemRow.position)
        )
        rows = (await self._session.execute(query)).mappings()
        return [
            BatchStatusItem(
                position=row["position"],
                subject_id=row["subject_id"],
                title=(row["title"] if row["title"] is not None else str(row["subject_id"])),
                run_id=row["run_id"],
                status=SubjectProductionStatus(row["status"]),
                current_stage=SubjectProductionStage(row["current_stage"]),
                pipeline_generation=row["pipeline_generation"],
                auto_recovery_count=row["auto_recovery_count"],
                error_code=row["error_code"],
                error_message=row["error_message"],
                extraction_progress=row.get("extraction_progress"),
            )
            for row in rows
        ]


def _subject_production_run_from_row(row: SubjectProductionRunRow) -> SubjectProductionRun:
    return SubjectProductionRun(
        id=row.id,
        subject_id=row.subject_id,
        edition_id=row.edition_id,
        status=SubjectProductionStatus(row.status),
        current_stage=SubjectProductionStage(row.current_stage),
        references_conversation_id=row.references_conversation_id,
        synthesis_conversation_id=row.synthesis_conversation_id,
        run_number=row.run_number,
        pipeline_generation=row.pipeline_generation,
        research_date=row.research_date,
        force_recompute_from_stage=(
            SubjectProductionStage(row.force_recompute_from_stage)
            if row.force_recompute_from_stage is not None
            else None
        ),
        error_code=row.error_code,
        error_message=row.error_message,
        error_details=row.error_details,
        extraction_progress=row.extraction_progress,
        started_at=row.started_at,
        finished_at=row.finished_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
        version=row.version,
    )


def _analyst_investigation_values(value: AnalystInvestigation) -> dict[str, object]:
    budget = value.budget
    return {
        "status": value.status.value,
        "current_stage": value.current_stage.value,
        "cycle_number": value.cycle_number,
        "input_pack_blob_id": value.input_pack_blob_id,
        "input_sha256": value.input_sha256,
        "pivot_conversation_id": value.pivot_conversation_id,
        "max_cycles": budget.max_cycles,
        "max_pivot_runs": budget.max_pivot_runs,
        "max_hits_acquired": budget.max_hits_acquired,
        "max_new_samples": budget.max_new_samples,
        "max_vt_read_units": budget.max_vt_read_units,
        "consumed_pivot_runs": budget.consumed_pivot_runs,
        "consumed_hits_acquired": budget.consumed_hits_acquired,
        "consumed_new_samples": budget.consumed_new_samples,
        "consumed_vt_read_units": budget.consumed_vt_read_units,
        "started_at": value.started_at,
        "finished_at": value.finished_at,
        "updated_at": value.updated_at,
        "version": value.version,
    }


def _analyst_investigation_row(value: AnalystInvestigation) -> AnalystInvestigationRow:
    return AnalystInvestigationRow(
        id=value.id,
        production_run_id=value.production_run_id,
        subject_id=value.subject_id,
        synthesis_artifact_id=value.synthesis_artifact_id,
        created_at=value.created_at,
        **_analyst_investigation_values(value),
    )


def _analyst_investigation_from_row(row: AnalystInvestigationRow) -> AnalystInvestigation:
    from cti_app.domain.production import (
        AnalystInvestigationStage,
        AnalystInvestigationStatus,
        LoopBudget,
    )

    return AnalystInvestigation(
        id=row.id,
        production_run_id=row.production_run_id,
        subject_id=row.subject_id,
        synthesis_artifact_id=row.synthesis_artifact_id,
        status=AnalystInvestigationStatus(row.status),
        current_stage=AnalystInvestigationStage(row.current_stage),
        cycle_number=row.cycle_number,
        input_pack_blob_id=row.input_pack_blob_id,
        input_sha256=row.input_sha256,
        pivot_conversation_id=row.pivot_conversation_id,
        budget=LoopBudget(
            max_cycles=row.max_cycles,
            max_pivot_runs=row.max_pivot_runs,
            max_hits_acquired=row.max_hits_acquired,
            max_new_samples=row.max_new_samples,
            max_vt_read_units=row.max_vt_read_units,
            consumed_pivot_runs=row.consumed_pivot_runs,
            consumed_hits_acquired=row.consumed_hits_acquired,
            consumed_new_samples=row.consumed_new_samples,
            consumed_vt_read_units=row.consumed_vt_read_units,
        ),
        started_at=row.started_at,
        finished_at=row.finished_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
        version=row.version,
    )


def _analyst_decision_from_row(row: AnalystDecisionRow) -> AnalystDecision:
    return AnalystDecision(
        id=row.id,
        investigation_id=row.investigation_id,
        decision_type=AnalystDecisionType(row.decision_type),
        target_type=AnalystDecisionTargetType(row.target_type),
        target_id=row.target_id,
        actor_id=row.actor_id,
        reason=row.reason,
        correlation_id=row.correlation_id,
        occurred_at=row.occurred_at,
    )


def _analyst_input_pack_from_row(row: AnalystInputPackRow) -> AnalystInputPack:
    return AnalystInputPack(
        id=row.id,
        investigation_id=row.investigation_id,
        blob_id=row.blob_id,
        sha256=row.sha256,
        schema_version=row.schema_version,
        created_at=row.created_at,
    )


def _source_extraction_values(extraction: SourceExtraction) -> dict[str, object]:
    return {
        "id": extraction.id,
        "canonical_url": extraction.canonical_url,
        "source_content_sha256": extraction.source_content_sha256,
        "profile": extraction.profile.value,
        "contract_version": extraction.contract_version,
        "prompt_version": extraction.prompt_version,
        "parser_version": extraction.parser_version,
        "verifier_version": extraction.verifier_version,
        "status": extraction.status.value,
        "canonical_blob_id": extraction.canonical_blob_id,
        "raw_blob_id": extraction.raw_blob_id,
        "model_run_id": extraction.model_run_id,
        "created_at": extraction.created_at,
    }


def _source_extraction_from_row(row: SourceExtractionRow) -> SourceExtraction:
    return SourceExtraction(
        id=row.id,
        canonical_url=row.canonical_url,
        source_content_sha256=row.source_content_sha256,
        profile=ExtractionProfile(row.profile),
        contract_version=row.contract_version,
        prompt_version=row.prompt_version,
        parser_version=row.parser_version,
        verifier_version=row.verifier_version,
        status=SourceExtractionStatus(row.status),
        canonical_blob_id=row.canonical_blob_id,
        raw_blob_id=row.raw_blob_id,
        model_run_id=row.model_run_id,
        created_at=row.created_at,
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
        reused_from_artifact_id=row.reused_from_artifact_id,
        metadata=row.artifact_metadata,
        created_at=row.created_at,
    )


def _edition_production_batch_from_row(row: EditionProductionBatchRow) -> EditionProductionBatch:
    from cti_app.domain.production import (
        EditionProductionBatch,
        ProductionBatchPhase,
        ProductionBatchStatus,
    )

    return EditionProductionBatch(
        id=row.id,
        edition_id=row.edition_id,
        status=ProductionBatchStatus(row.status),
        phase=ProductionBatchPhase(row.phase),
        next_dispatch_at=row.next_dispatch_at,
        created_at=row.created_at,
        started_at=row.started_at,
        finished_at=row.finished_at,
        version=row.version,
    )


def _edition_production_batch_item_from_row(
    row: EditionProductionBatchItemRow,
) -> EditionProductionBatchItem:
    from cti_app.domain.production import EditionProductionBatchItem

    return EditionProductionBatchItem(
        id=row.id,
        batch_id=row.batch_id,
        subject_id=row.subject_id,
        production_run_id=row.production_run_id,
        position=row.position,
        auto_recovery_count=row.auto_recovery_count,
        created_at=row.created_at,
    )


def _production_input_snapshot_from_row(
    row: ProductionInputSnapshotRow,
) -> ProductionInputSnapshot:
    return ProductionInputSnapshot(
        id=row.id,
        production_run_id=row.production_run_id,
        subject_id=row.subject_id,
        edition_id=row.edition_id,
        editorial_group_id=row.editorial_group_id,
        editorial_group_version=row.editorial_group_version,
        subject_title=row.subject_title,
        subject_description=row.subject_description,
        actor_or_campaign=row.actor_or_campaign,
        period_start=row.period_start,
        period_end=row.period_end,
        research_date=row.research_date,
        core_sources=tuple(ProductionInputSource.from_payload(item) for item in row.core_sources),
        input_hash=row.input_hash,
        reuse_basis_hash=row.reuse_basis_hash,
        captured_at=row.captured_at,
    )


def _production_reuse_invalidation_from_row(
    row: ProductionReuseInvalidationRow,
) -> ProductionReuseInvalidation:
    return ProductionReuseInvalidation(
        id=row.id,
        edition_id=row.edition_id,
        subject_id=row.subject_id,
        from_stage=SubjectProductionStage(row.from_stage),
        actor_id=row.actor_id,
        correlation_id=row.correlation_id,
        occurred_at=row.occurred_at,
    )
