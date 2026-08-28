from collections.abc import Sequence
from typing import Any, cast
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from cti_app.domain.editorial import AnalystDecision, AnalystDecisionTargetType, AnalystDecisionType
from cti_app.domain.production import (
    AnalystInputPack,
    AnalystInvestigation,
    EditionProductionBatch,
    EditionProductionBatchItem,
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionArtifactStatus,
    ProductionInputSnapshot,
    ProductionInputSource,
    ProductionProfile,
    SampleAcquisitionAttempt,
    SampleAcquisitionOutcome,
    SampleAcquisitionReason,
    SubjectProductionRun,
    SubjectProductionStage,
    SubjectProductionStatus,
)
from cti_app.infrastructure.database.models.production import (
    AnalystDecisionRow,
    AnalystInputPackRow,
    AnalystInvestigationRow,
    EditionProductionBatchItemRow,
    EditionProductionBatchRow,
    ProductionArtifactRow,
    ProductionInputSnapshotRow,
    SampleAcquisitionAttemptRow,
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
            profile=run.profile.value,
            status=run.status.value,
            current_stage=run.current_stage.value,
            references_conversation_id=run.references_conversation_id,
            synthesis_conversation_id=run.synthesis_conversation_id,
            run_number=run.run_number,
            pipeline_generation=run.pipeline_generation,
            research_date=run.research_date,
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
        # Repository rows do not have ORM relationships, so SQLAlchemy cannot
        # infer this FK ordering when artifacts are added in the same UoW.
        await self._session.flush()

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
                error_code=run.error_code,
                error_message=run.error_message,
                error_details=run.error_details,
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
            .order_by(SubjectProductionRunRow.created_at.desc())
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

    async def allocate_next_run_number(self, subject_id: UUID) -> int:
        """Allocate a subject-local number while holding a transaction lock.

        The advisory lock is released by the surrounding UoW commit/rollback,
        so concurrent run creation cannot observe the same maximum number.
        """
        await self._session.execute(
            select(func.pg_advisory_xact_lock(func.hashtext(str(subject_id))))
        )
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
            ProductionArtifactStage.BRIEF.value,
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
            "assembly": "brief",
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


class SqlAlchemyEditionProductionBatchRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, batch: EditionProductionBatch) -> None:
        row = EditionProductionBatchRow(
            id=batch.id,
            edition_id=batch.edition_id,
            profile=batch.profile.value,
            status=batch.status,
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
            .order_by(EditionProductionBatchRow.created_at.desc())
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
                status=batch.status,
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
                & (EditionProductionBatchRow.status.in_(["queued", "running"]))
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


def _subject_production_run_from_row(row: SubjectProductionRunRow) -> SubjectProductionRun:
    return SubjectProductionRun(
        id=row.id,
        subject_id=row.subject_id,
        edition_id=row.edition_id,
        profile=ProductionProfile(row.profile),
        status=SubjectProductionStatus(row.status),
        current_stage=SubjectProductionStage(row.current_stage),
        references_conversation_id=row.references_conversation_id,
        synthesis_conversation_id=row.synthesis_conversation_id,
        run_number=row.run_number,
        pipeline_generation=row.pipeline_generation,
        research_date=row.research_date,
        error_code=row.error_code,
        error_message=row.error_message,
        error_details=row.error_details,
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
        metadata=row.artifact_metadata,
        created_at=row.created_at,
    )


def _edition_production_batch_from_row(row: EditionProductionBatchRow) -> EditionProductionBatch:
    from cti_app.domain.production import (
        EditionProductionBatch,
        ProductionBatchPhase,
        ProductionProfile,
    )

    return EditionProductionBatch(
        id=row.id,
        edition_id=row.edition_id,
        profile=ProductionProfile(row.profile),
        status=row.status,
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
        captured_at=row.captured_at,
    )
