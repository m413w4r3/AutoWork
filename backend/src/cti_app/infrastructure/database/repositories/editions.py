from collections.abc import Sequence
from datetime import date
from uuid import UUID

from sqlalchemy import delete, func, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from cti_app.domain.classification import TLP
from cti_app.domain.editions import Edition, EditionAuditEvent, EditionStatus
from cti_app.infrastructure.database.models import (
    BriefDraftRow,
    BriefEvidencePackRow,
    ClaimRow,
    CollectionAttemptRow,
    DiscoveryBatchRow,
    EditionAuditEventRow,
    EditionProductionBatchItemRow,
    EditionProductionBatchRow,
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
    ProductionArtifactRow,
    ProvenanceEventRow,
    RejectedModelProposalRow,
    SourceCollectionRow,
    SubjectProductionRunRow,
)


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

        # Production artifacts reference conversation turns and model runs, so
        # they have to go before those are deleted below.
        production_run_ids = list(
            await self._session.scalars(
                select(SubjectProductionRunRow.id).where(
                    SubjectProductionRunRow.edition_id == edition_id
                )
            )
        )
        batch_ids = list(
            await self._session.scalars(
                select(EditionProductionBatchRow.id).where(
                    EditionProductionBatchRow.edition_id == edition_id
                )
            )
        )
        if batch_ids:
            await self._session.execute(
                delete(EditionProductionBatchItemRow).where(
                    EditionProductionBatchItemRow.batch_id.in_(batch_ids)
                )
            )
        await self._session.execute(
            delete(EditionProductionBatchRow).where(
                EditionProductionBatchRow.edition_id == edition_id
            )
        )
        if production_run_ids:
            await self._session.execute(
                delete(ProductionArtifactRow).where(
                    ProductionArtifactRow.production_run_id.in_(production_run_ids)
                )
            )
        await self._session.execute(
            delete(SubjectProductionRunRow).where(SubjectProductionRunRow.edition_id == edition_id)
        )

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
